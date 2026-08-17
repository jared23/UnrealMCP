"""UserTools :: Animation authoring (WRITE)  (spec: docs/spec/animation.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). The mutating counterpart to
animation_read.py. Query convention, base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py / curves_write.py.

All authoring goes through unreal.AnimationLibrary (UAnimationBlueprintLibrary), which drives the
AnimSequence's IAnimationDataController internally (opens/closes its own change bracket per call).
Every mutation is additionally wrapped in unreal.ScopedEditorTransaction and the asset is saved so
the edit persists, and an inverse op is pushed on the per-session ledger for cross-session `undo`.

What IS reachable + FAITHFULLY reversible from stock Python in this build (verified live vs
TestMCPSetup, UE 5.8.1, on a scratch duplicate of MM_Idle):
  * Notify TRACKS   : add_animation_notify_track / remove_animation_notify_track
  * Notifies        : add_animation_notify_event (point) / add_animation_notify_state_event
                      (duration); removal is by-track (remove_animation_notify_events_by_track) --
                      there is NO single-event-by-handle removal, so the faithful inverse of one add
                      is: snapshot the target track's events, then on undo clear the track and rebuild
                      exactly the prior events (class + time + duration).
  * Sync markers    : add_animation_sync_marker / remove_animation_sync_markers_by_name
  * Float curves    : add_curve / remove_curve / add_float_curve_key(s) / get_float_keys
  * Rate scale      : set_rate_scale / get_rate_scale

DEFERRED (NOT reachable from stock Python in this build -- refused rather than shipping a fake or
unrevertable write):
  * AnimMontage SECTION editing (add/remove/set section time). UAnimMontage.CompositeSections is a
    PROTECTED UPROPERTY -- get_editor_property("composite_sections") is refused ("Failed to find
    property"); there is NO MontageEditorLibrary in this build and AnimMontage exposes only READ
    section accessors (get_num_sections / get_section_name / get_section_index / is_valid_section_name
    / get_section_length). Adding/moving a composite section needs a C++ handler mutating the montage's
    FCompositeSection array (+ RefreshBranchingPointMarkers) -- same C++-round-trip pattern used for
    curve keys / struct fields. Ledger schema when that lands (for the coordinator):
      op "add_montage_section" {asset_path, section_name} -> inverse: remove that section by name.
      op "set_montage_section_time" {asset_path, section_name, prior_start_time} -> inverse: restore.
  * Per-notify custom UPROPERTY deep-copy on OTHER events sharing a track (see add_anim_notify note).

Implemented (all validated live, session=agentA_val, editor left CLEAN, ledger depth 0):
  - add_anim_notify_track     (WRITE; op "add_anim_notify_track")
  - remove_anim_notify_track  (WRITE; op "remove_anim_notify_track"; EMPTY tracks only -- refuses to
                               destroy a populated track, which would not be faithfully reversible)
  - add_anim_notify           (WRITE; op "add_anim_notify"; point or notify-state)
  - add_anim_sync_marker      (WRITE; op "add_anim_sync_marker"; refuses duplicate marker names)
  - add_anim_curve            (WRITE; op "add_anim_curve"; float curve)
  - remove_anim_curve         (WRITE; op "remove_anim_curve"; captures keys for restore)
  - set_anim_curve_key        (WRITE; op "set_anim_curve_key"; add/overwrite a float key)
  - set_anim_rate_scale       (WRITE; op "set_anim_rate_scale")

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The 8
op inverses above are reported to the coordinator to fold into editor_level.undo.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
_LOG_HEAD = (
    "import unreal as _uu, os as _oo, json as _jj\n"
    "def _umcp_logmain():\n"
    "    d=_uu.Paths.convert_relative_path_to_full(_uu.Paths.project_log_dir())\n"
    "    for f in _oo.listdir(d):\n"
    "        if f.endswith('.log') and '-backup-' not in f:\n"
    "            return _oo.path.join(d,f)\n"
    "    return None\n"
    "try:\n"
    "    _umcp_main=_umcp_logmain(); _umcp_s0=_oo.path.getsize(_umcp_main) if _umcp_main else 0\n"
    "except Exception:\n"
    "    _umcp_main=None; _umcp_s0=0\n"
    "try:\n"
)
_LOG_TAILER = (
    "\nfinally:\n"
    "    try:\n"
    "        _uu.log_flush()\n"
    "        if _umcp_main:\n"
    "            _fh=open(_umcp_main,'rb'); _fh.seek(_umcp_s0); _dd=_fh.read().decode('utf-8','replace'); _fh.close()\n"
    "            _ww=[ln for ln in _dd.splitlines() if (': Warning:' in ln or ': Error:' in ln) and 'UMCP' not in ln and 'LogMCP:' not in ln]\n"
    "            if _ww: print('@@UMCP_LOG@@'+_jj.dumps(_ww[-50:]))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _wrap(code):
    """Wrap a snippet with Output-Log delta capture (try/finally)."""
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": _wrap(code)})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        lines = out.splitlines()
        warns = []
        for line in lines:
            if LOG_MARKER in line:
                try:
                    warns = json.loads(line.split(LOG_MARKER, 1)[1])
                except Exception:
                    pass
        for line in reversed(lines):
            if MARKER in line:
                result = json.loads(line.split(MARKER, 1)[1])
                if warns and isinstance(result, dict):
                    result["_log_warnings"] = warns
                return result
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
    #   _ledger()          -> per-session undo stack (copied verbatim from editor_level.py).
    #   _close_editors(obj)-> silently close any open asset editor for obj.
    #   _load_anim(path,base_ok) -> (obj, err); type-checked AnimSequence (or AnimSequenceBase).
    #   _save(path)        -> persist the asset (only_if_is_dirty=False).
    #   _resolve_notify_class(spec) -> UClass from short name or object path.
    #   _capture_track_events(anim, track) -> [{kind, class_path, time, duration}] (for faithful undo).
    _ANIMW_HELPERS = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
AL = unreal.AnimationLibrary
RCT = unreal.RawCurveTrackTypes
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _load_anim(path, base_ok):
    if not path:
        return None, "no asset path given"
    obj = None
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "failed to load: %s (%s)" % (path, str(e)[:120])
    if obj is None:
        return None, "asset not found: %s" % path
    want = unreal.AnimSequenceBase if base_ok else unreal.AnimSequence
    if not isinstance(obj, want):
        kind = "AnimSequence/AnimMontage" if base_ok else "AnimSequence"
        return None, "asset is not an %s (got %s): %s" % (kind, obj.get_class().get_name(), path)
    return obj, None
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
def _resolve_notify_class(spec):
    if not spec:
        return None
    c = getattr(unreal, spec, None)
    if isinstance(c, type):
        return c
    for loader in (lambda: unreal.load_object(None, spec), lambda: unreal.load_class(None, spec)):
        try:
            r = loader()
            if r is not None:
                return r
        except Exception:
            pass
    return None
def _capture_track_events(anim, track):
    out = []
    for e in (AL.get_animation_notify_events_for_track(anim, track) or []):
        rec = {"time": round(AL.get_anim_notify_event_trigger_time(e), 6),
               "duration": round(AL.get_anim_notify_event_duration(e), 6)}
        n = e.get_editor_property("notify")
        nsc = e.get_editor_property("notify_state_class")
        if n is not None:
            rec["kind"] = "notify"; rec["class_path"] = n.get_class().get_path_name()
        elif nsc is not None:
            cls = nsc if isinstance(nsc, unreal.Class) else nsc.get_class()
            rec["kind"] = "state"; rec["class_path"] = cls.get_path_name()
        else:
            rec["kind"] = "unknown"; rec["class_path"] = None
        out.append(rec)
    return out
'''

    # ------------------------------------------------------------------ #
    # add_anim_notify_track — add an (empty) notify track                 #
    # ------------------------------------------------------------------ #
    _ADD_TRACK_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    track = PARAMS["track_name"]
    tracks = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
    if track in tracks:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "notify track already exists: %s (refusing to duplicate)" % track}))
    else:
        col = PARAMS.get("color") or [1.0, 1.0, 1.0, 1.0]
        lc = unreal.LinearColor(float(col[0]), float(col[1]), float(col[2]),
                                float(col[3]) if len(col) > 3 else 1.0)
        with unreal.ScopedEditorTransaction("MCP add_anim_notify_track"):
            AL.add_animation_notify_track(anim, track, lc)
        _save(PARAMS["anim_path"])
        _ledger().append({"op": "add_anim_notify_track", "asset_path": PARAMS["anim_path"], "track_name": track})
        after = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
            "track_name": track, "tracks_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_anim_notify_track(ctx, anim_path: str, track_name: str, color: list = None) -> str:
        """Add a new (empty) notify track to an AnimSequence / AnimMontage. Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage, e.g.
                    '/Game/Characters/Mannequins/Anims/Unarmed/MM_Idle.MM_Idle'.
        track_name: name for the new notify track (refused if a track of that name already exists).
        color:      optional [r,g,b,a] track display color, floats 0..1 (default white).

        Uses AnimationLibrary.add_animation_notify_track. The asset is saved. Verify with
        animation_read.get_anim_sequence_info (notify_track_names).

        Ledgered op 'add_anim_notify_track' {asset_path, track_name}. Inverse (fold into
        editor_level.undo): remove_animation_notify_track(track_name) + save -- FAITHFUL (the track is
        created empty, so removing it removes nothing else)."""
        params = {"anim_path": anim_path, "track_name": track_name, "color": color}
        try:
            return json.dumps(_exec(_ADD_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_anim_notify_track — remove an EMPTY notify track             #
    # ------------------------------------------------------------------ #
    _REMOVE_TRACK_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    track = PARAMS["track_name"]
    tracks = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
    if track not in tracks:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such notify track: %s" % track}))
    else:
        n_events = len(AL.get_animation_notify_events_for_track(anim, track) or [])
        n_markers = len(AL.get_animation_sync_markers_for_track(anim, track) or [])
        if n_events or n_markers:
            print("@@UMCP@@" + json.dumps({"status": "refused",
                "message": ("track '%s' is not empty (%d notify events, %d sync markers); removing it "
                            "would destroy that content and is not faithfully reversible. Remove its "
                            "contents first." % (track, n_events, n_markers))}))
        else:
            with unreal.ScopedEditorTransaction("MCP remove_anim_notify_track"):
                AL.remove_animation_notify_track(anim, track)
            _save(PARAMS["anim_path"])
            _ledger().append({"op": "remove_anim_notify_track", "asset_path": PARAMS["anim_path"], "track_name": track})
            after = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
                "track_name": track, "tracks_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_anim_notify_track(ctx, anim_path: str, track_name: str) -> str:
        """Remove an EMPTY notify track from an AnimSequence / AnimMontage. Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        track_name: notify track to remove. REFUSED if the track holds any notify events or sync
                    markers (removing a populated track is destructive and not faithfully reversible;
                    remove its contents first).

        Uses AnimationLibrary.remove_animation_notify_track. The asset is saved.

        Ledgered op 'remove_anim_notify_track' {asset_path, track_name}. Inverse (fold into
        editor_level.undo): add_animation_notify_track(track_name, white) + save. NOTE: a notify
        track's display COLOR is not readable via Python, so undo re-creates the (empty) track with
        the default color -- structurally faithful, color cosmetic."""
        params = {"anim_path": anim_path, "track_name": track_name}
        try:
            return json.dumps(_exec(_REMOVE_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_anim_notify — place a notify (point) or notify-state (duration) #
    # ------------------------------------------------------------------ #
    _ADD_NOTIFY_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    track = PARAMS["track_name"]
    tracks = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
    if track not in tracks:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no such notify track: %s (existing: %s). Create it with add_anim_notify_track." % (track, tracks)}))
    else:
        spec = PARAMS.get("notify_class") or "AnimNotify"
        cls = _resolve_notify_class(spec)
        if cls is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve notify class: %s" % spec}))
        else:
            t = float(PARAMS["time"])
            dur = float(PARAMS.get("duration") or 0.0)
            is_state = dur > 0.0
            prior_events = _capture_track_events(anim, track)
            with unreal.ScopedEditorTransaction("MCP add_anim_notify"):
                if is_state:
                    obj = AL.add_animation_notify_state_event(anim, track, t, dur, cls)
                else:
                    obj = AL.add_animation_notify_event(anim, track, t, cls)
            _save(PARAMS["anim_path"])
            _ledger().append({"op": "add_anim_notify", "asset_path": PARAMS["anim_path"],
                              "track_name": track, "prior_events": prior_events})
            evs = _capture_track_events(anim, track)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
                "track_name": track, "kind": ("state" if is_state else "notify"),
                "notify_class": (obj.get_class().get_path_name() if obj is not None else cls.get_path_name()),
                "time": t, "duration": dur, "events_on_track_after": evs,
                "prior_event_count": len(prior_events), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_anim_notify(ctx, anim_path: str, track_name: str, time: float,
                        notify_class: str = "AnimNotify", duration: float = 0.0) -> str:
        """Place an AnimNotify (point) or AnimNotifyState (with duration) on a notify track of an
        AnimSequence / AnimMontage. Ledgered write.

        anim_path:    object path of an AnimSequence or AnimMontage.
        track_name:   an EXISTING notify track (create one first with add_anim_notify_track).
        time:         trigger time in seconds.
        notify_class: notify class -- short engine name (e.g. 'AnimNotify', 'AnimNotify_PlaySound')
                      or full object path (e.g. '/Script/Engine.AnimNotify'). Default 'AnimNotify'.
        duration:     if > 0, an AnimNotifyState of this length is added; if 0, a point AnimNotify.

        Uses AnimationLibrary.add_animation_notify_event / add_animation_notify_state_event. Saved.

        Ledgered op 'add_anim_notify' {asset_path, track_name, prior_events}. Inverse (fold into
        editor_level.undo): remove_animation_notify_events_by_track(track), then re-add each of
        prior_events (class_path + time [+ duration]) -- there is no single-event removal API, so the
        faithful inverse snapshots the whole track and rebuilds its prior events. This removes exactly
        the notify we added; any pre-existing events on that track are recreated at the same
        class/time/duration (custom per-notify property VALUES reset to class defaults -- documented
        caveat; adding onto an empty or agent-owned track is perfectly faithful)."""
        params = {"anim_path": anim_path, "track_name": track_name, "time": time,
                  "notify_class": notify_class, "duration": duration}
        try:
            return json.dumps(_exec(_ADD_NOTIFY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_anim_sync_marker — add a sync marker (unique name)              #
    # ------------------------------------------------------------------ #
    _ADD_MARKER_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], False)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    marker = PARAMS["marker_name"]
    track = PARAMS["track_name"]
    tracks = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
    existing = [str(m) for m in (AL.get_unique_marker_names(anim) or [])]
    if track not in tracks:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no such notify track: %s (existing: %s)" % (track, tracks)}))
    elif marker in existing:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": ("sync marker name already exists: %s. This tool refuses duplicate names so its "
                        "undo (remove-by-name) stays exact." % marker)}))
    else:
        t = float(PARAMS["time"])
        with unreal.ScopedEditorTransaction("MCP add_anim_sync_marker"):
            AL.add_animation_sync_marker(anim, marker, t, track)
        _save(PARAMS["anim_path"])
        _ledger().append({"op": "add_anim_sync_marker", "asset_path": PARAMS["anim_path"], "marker_name": marker})
        after = [str(m) for m in (AL.get_unique_marker_names(anim) or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
            "marker_name": marker, "track_name": track, "time": t,
            "marker_names_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_anim_sync_marker(ctx, anim_path: str, marker_name: str, time: float, track_name: str) -> str:
        """Add an animation sync marker to an AnimSequence at a time, on a notify track. Ledgered write.

        anim_path:   object path of an AnimSequence (sync markers are an AnimSequence feature).
        marker_name: marker name. REFUSED if a marker of that name already exists on the sequence (so
                     the undo, remove-by-name, stays exact -- markers do not expose a per-marker track
                     index in this build, so removing only the one we added otherwise isn't possible).
        time:        marker time in seconds.
        track_name:  an EXISTING notify track to host the marker.

        Uses AnimationLibrary.add_animation_sync_marker. Saved. Verify with
        animation_read.get_anim_sequence_info / get_unique_marker_names.

        Ledgered op 'add_anim_sync_marker' {asset_path, marker_name}. Inverse (fold into
        editor_level.undo): remove_animation_sync_markers_by_name(marker_name) + save -- FAITHFUL (the
        name had zero prior occurrences, enforced above)."""
        params = {"anim_path": anim_path, "marker_name": marker_name, "time": time, "track_name": track_name}
        try:
            return json.dumps(_exec(_ADD_MARKER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_anim_curve — add a (float) animation curve                     #
    # ------------------------------------------------------------------ #
    _ADD_CURVE_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    name = PARAMS["curve_name"]
    if AL.does_curve_exist(anim, name, RCT.RCT_FLOAT):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "float curve already exists: %s (refusing to duplicate)" % name}))
    else:
        with unreal.ScopedEditorTransaction("MCP add_anim_curve"):
            AL.add_curve(anim, name, RCT.RCT_FLOAT, False)
        _save(PARAMS["anim_path"])
        _ledger().append({"op": "add_anim_curve", "asset_path": PARAMS["anim_path"], "curve_name": name})
        after = [str(c) for c in (AL.get_animation_curve_names(anim, RCT.RCT_FLOAT) or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
            "curve_name": name, "float_curves_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_anim_curve(ctx, anim_path: str, curve_name: str) -> str:
        """Add a new (empty) FLOAT animation curve to an AnimSequence / AnimMontage. Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        curve_name: name for the new float curve (refused if a float curve of that name exists).

        Uses AnimationLibrary.add_curve(RCT_FLOAT). Saved. Author key values with set_anim_curve_key.
        Verify with animation_read.get_anim_sequence_info (float_curve_names).

        Ledgered op 'add_anim_curve' {asset_path, curve_name}. Inverse (fold into editor_level.undo):
        remove_curve(curve_name) + save -- FAITHFUL (we created it new + empty)."""
        params = {"anim_path": anim_path, "curve_name": curve_name}
        try:
            return json.dumps(_exec(_ADD_CURVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_anim_curve — remove a float curve (captures keys for undo)   #
    # ------------------------------------------------------------------ #
    _REMOVE_CURVE_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    name = PARAMS["curve_name"]
    if not AL.does_curve_exist(anim, name, RCT.RCT_FLOAT):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such float curve: %s" % name}))
    else:
        tk, vk = AL.get_float_keys(anim, name)
        prior_keys = [{"time": round(float(tk[i]), 6), "value": round(float(vk[i]), 6)} for i in range(len(tk))]
        with unreal.ScopedEditorTransaction("MCP remove_anim_curve"):
            AL.remove_curve(anim, name, False)
        _save(PARAMS["anim_path"])
        _ledger().append({"op": "remove_anim_curve", "asset_path": PARAMS["anim_path"],
                          "curve_name": name, "prior_keys": prior_keys})
        after = [str(c) for c in (AL.get_animation_curve_names(anim, RCT.RCT_FLOAT) or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
            "curve_name": name, "removed_key_count": len(prior_keys),
            "float_curves_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_anim_curve(ctx, anim_path: str, curve_name: str) -> str:
        """Remove a FLOAT animation curve from an AnimSequence / AnimMontage. Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        curve_name: the float curve to remove (its key times+values are captured for undo).

        Uses AnimationLibrary.remove_curve. Saved.

        Ledgered op 'remove_anim_curve' {asset_path, curve_name, prior_keys}. Inverse (fold into
        editor_level.undo): add_curve(curve_name, RCT_FLOAT) + add_float_curve_keys(prior times/values)
        + save. Restores every key TIME and VALUE faithfully; per-key interpolation/tangent metadata is
        not exposed by get_float_keys so it returns to default (documented -- exact for curves authored
        through these tools)."""
        params = {"anim_path": anim_path, "curve_name": curve_name}
        try:
            return json.dumps(_exec(_REMOVE_CURVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_anim_curve_key — add/overwrite a float key on a curve           #
    # ------------------------------------------------------------------ #
    _SET_KEY_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    name = PARAMS["curve_name"]
    t = float(PARAMS["time"]); v = float(PARAMS["value"])
    existed = AL.does_curve_exist(anim, name, RCT.RCT_FLOAT)
    prior_keys = []
    if existed:
        tk, vk = AL.get_float_keys(anim, name)
        prior_keys = [{"time": round(float(tk[i]), 6), "value": round(float(vk[i]), 6)} for i in range(len(tk))]
    with unreal.ScopedEditorTransaction("MCP set_anim_curve_key"):
        if not existed:
            AL.add_curve(anim, name, RCT.RCT_FLOAT, False)
        AL.add_float_curve_key(anim, name, t, v)
    _save(PARAMS["anim_path"])
    _ledger().append({"op": "set_anim_curve_key", "asset_path": PARAMS["anim_path"],
                      "curve_name": name, "existed": bool(existed), "prior_keys": prior_keys})
    tk2, vk2 = AL.get_float_keys(anim, name)
    keys_after = [{"time": round(float(tk2[i]), 6), "value": round(float(vk2[i]), 6)} for i in range(len(tk2))]
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
        "curve_name": name, "curve_created": (not existed), "time": t, "value": v,
        "keys_after": keys_after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_anim_curve_key(ctx, anim_path: str, curve_name: str, time: float, value: float) -> str:
        """Add or overwrite a FLOAT curve key on an AnimSequence / AnimMontage curve (the curve is
        created if it does not exist). Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        curve_name: float curve name (created via add_curve if missing).
        time:       key time in seconds.
        value:      key value. A key already at that time is updated.

        Uses AnimationLibrary.add_float_curve_key (+ add_curve if the curve is new). Saved. Verify with
        animation_read / get_float_keys.

        Ledgered op 'set_anim_curve_key' {asset_path, curve_name, existed, prior_keys}. Inverse (fold
        into editor_level.undo): if the curve did NOT exist before -> remove_curve(curve_name); else
        remove_curve + add_curve + add_float_curve_keys(prior times/values). Restores prior key
        times+values faithfully (interp/tangent -> default, as get_float_keys omits it)."""
        params = {"anim_path": anim_path, "curve_name": curve_name, "time": time, "value": value}
        try:
            return json.dumps(_exec(_SET_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_anim_rate_scale — set the sequence play-rate scale              #
    # ------------------------------------------------------------------ #
    _SET_RATE_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    prior = float(AL.get_rate_scale(anim))
    newv = float(PARAMS["rate_scale"])
    with unreal.ScopedEditorTransaction("MCP set_anim_rate_scale"):
        AL.set_rate_scale(anim, newv)
    _save(PARAMS["anim_path"])
    _ledger().append({"op": "set_anim_rate_scale", "asset_path": PARAMS["anim_path"], "prior": prior})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"],
        "prior_rate_scale": prior, "rate_scale_after": float(AL.get_rate_scale(anim)),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_anim_rate_scale(ctx, anim_path: str, rate_scale: float) -> str:
        """Set the play-rate scale of an AnimSequence / AnimMontage. Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        rate_scale: new rate scale (1.0 = normal; 2.0 = twice as fast; must be > 0 for sane playback).

        Uses AnimationLibrary.set_rate_scale (prior read via get_rate_scale). Saved.

        Ledgered op 'set_anim_rate_scale' {asset_path, prior}. Inverse (fold into editor_level.undo):
        set_rate_scale(prior) + save -- FAITHFUL scalar restore."""
        params = {"anim_path": anim_path, "rate_scale": rate_scale}
        try:
            return json.dumps(_exec(_SET_RATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # AnimMontage composite-section authoring (C++ #13 handlers)          #
    # ------------------------------------------------------------------ #
    _MONTAGE_BODY = _ANIMW_HELPERS + r'''
anim, err = _load_anim(PARAMS["anim_path"], True)
action = PARAMS["action"]
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(anim, unreal.AnimMontage):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an AnimMontage (got %s): %s" % (anim.get_class().get_name(), PARAMS["anim_path"])}))
else:
    _rl = getattr(unreal, "MCPReflectionLibrary", None)
    _fnmap = {"add": "add_montage_section", "remove": "remove_montage_section",
              "set_time": "set_montage_section_time", "set_next": "set_montage_section_next_section"}
    _fn = _fnmap[action]
    if _rl is None or not hasattr(_rl, _fn):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.%s unavailable — reload the MCP server after the C++ #13 rebuild" % _fn}))
    else:
        _sec = PARAMS["section_name"]
        with unreal.ScopedEditorTransaction("MCP montage_section"):
            if action == "add":
                _res = json.loads(_rl.add_montage_section(anim, _sec, float(PARAMS.get("start_time") or 0.0)))
            elif action == "remove":
                _res = json.loads(_rl.remove_montage_section(anim, _sec))
            elif action == "set_time":
                _res = json.loads(_rl.set_montage_section_time(anim, _sec, float(PARAMS.get("start_time") or 0.0)))
            else:
                _res = json.loads(_rl.set_montage_section_next_section(anim, _sec, PARAMS.get("next_section") or ""))
        if _res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": _res.get("error")}))
        else:
            _save(PARAMS["anim_path"])
            if action == "add":
                _op = {"op": "add_montage_section", "asset_path": PARAMS["anim_path"], "section_name": _sec}
            elif action == "remove":
                _op = {"op": "remove_montage_section", "asset_path": PARAMS["anim_path"], "section_name": _sec,
                       "prior_start_time": _res.get("prior_start_time"), "next_section_name": _res.get("next_section_name")}
            elif action == "set_time":
                _op = {"op": "set_montage_section_time", "asset_path": PARAMS["anim_path"], "section_name": _sec,
                       "prior_start_time": _res.get("prior_start_time")}
            else:
                _op = {"op": "set_montage_section_next_section", "asset_path": PARAMS["anim_path"], "section_name": _sec,
                       "prior_next_section": _res.get("prior_next_section")}
            _ledger().append(_op)
            _res["status"] = "success"; _res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(_res))
'''

    @mcp.tool()
    def add_montage_section(ctx, anim_path: str, section_name: str, start_time: float = 0.0) -> str:
        """Add a composite section to an AnimMontage at a time (C++ #13 handler). Ledgered, reversible.

        anim_path:    object path of an AnimMontage.
        section_name: name for the new section (refused if it already exists).
        start_time:   section start time in seconds.

        Calls MCPReflectionLibrary.add_montage_section (reflection write into the protected
        CompositeSections array + time-sort). Ledgered op 'add_montage_section' {asset_path,
        section_name}; inverse (folded into editor_level.undo): remove_montage_section."""
        params = {"anim_path": anim_path, "action": "add", "section_name": section_name, "start_time": start_time}
        try:
            return json.dumps(_exec(_MONTAGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_montage_section(ctx, anim_path: str, section_name: str) -> str:
        """Remove a composite section from an AnimMontage (C++ #13 handler). Ledgered, reversible.

        anim_path:    object path of an AnimMontage.
        section_name: the section to remove. Its prior start time + next-section link are captured for undo.

        Ledgered op 'remove_montage_section' {asset_path, section_name, prior_start_time, next_section_name};
        inverse (folded into editor_level.undo): add_montage_section(prior_start_time) + set next-link."""
        params = {"anim_path": anim_path, "action": "remove", "section_name": section_name}
        try:
            return json.dumps(_exec(_MONTAGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_montage_section_time(ctx, anim_path: str, section_name: str, start_time: float) -> str:
        """Move a composite section to a new start time (C++ #13 handler). Ledgered, reversible.

        anim_path:    object path of an AnimMontage.
        section_name: the section to move.
        start_time:   new start time in seconds (sections are re-sorted by time).

        Ledgered op 'set_montage_section_time' {asset_path, section_name, prior_start_time}; inverse
        (folded into editor_level.undo): set_montage_section_time(prior_start_time)."""
        params = {"anim_path": anim_path, "action": "set_time", "section_name": section_name, "start_time": start_time}
        try:
            return json.dumps(_exec(_MONTAGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_montage_section_next_section(ctx, anim_path: str, section_name: str, next_section: str) -> str:
        """Set a composite section's NextSectionName link (C++ #13 handler). Ledgered, reversible.

        anim_path:    object path of an AnimMontage.
        section_name: the section whose next-link to set.
        next_section: the section to chain to (empty string clears the link).

        Ledgered op 'set_montage_section_next_section' {asset_path, section_name, prior_next_section};
        inverse (folded into editor_level.undo): set_montage_section_next_section(prior_next_section)."""
        params = {"anim_path": anim_path, "action": "set_next", "section_name": section_name, "next_section": next_section}
        try:
            return json.dumps(_exec(_MONTAGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. Its op inverses
    # are reported to the coordinator to fold into editor_level.undo (see module docstring / board).
