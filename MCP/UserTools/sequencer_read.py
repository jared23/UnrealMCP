"""UserTools :: Sequencer / Cinematics (READ)  (spec: docs/spec/sequencer.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset editors are ever opened (opening a Sequencer tab is heavy and could modal-stall the
shared editor) — everything here is AssetRegistry enumeration plus reflection over the loaded
LevelSequence object and its MovieScene / bindings / tracks / sections. Query convention +
base64 PARAMS + Output-Log capture are copied verbatim from editor_level.py (the gold standard).

What IS reachable from stock Python in this build (verified live via class reflection):
  * AssetRegistry enumerates `LevelSequence` assets. In 5.8 LevelSequence lives in the
    /Script/LevelSequence module, so the class_paths ARFilter API is required:
    unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/LevelSequence",
    "LevelSequence")], recursive_paths=True). class_names=["LevelSequence"] does NOT resolve it.
  * The loaded `unreal.LevelSequence` exposes the whole scripting surface directly (no
    MovieSceneSequenceExtensions indirection needed):
      seq.get_display_rate() / seq.get_tick_resolution()   -> unreal.FrameRate (.numerator/.denominator)
      seq.get_playback_start() / seq.get_playback_end()     -> int (frame numbers, tick resolution)
      seq.get_playback_start_seconds()/..._end_seconds()    -> float
      seq.get_playback_range()                              -> SequencerScriptingRange
      seq.get_bindings()                                    -> [MovieSceneBindingProxy]
      seq.get_tracks()                                      -> [MovieSceneTrack]  (master/root tracks)
      seq.get_movie_scene()                                 -> MovieScene
      seq.get_marked_frames()                               -> [MovieSceneMarkedFrame]
      seq.get_possessables()/get_spawnables()
    Binding proxy: get_display_name(), get_name(), get_possessed_object_class(), get_tracks(),
      get_sorting_order(), get_parent(), get_child_possessables().
    Track: get_display_name(), get_class().get_name(), get_sections().
    Section: get_class().get_name(), get_start_frame()/get_end_frame() (+ _seconds),
      has_start_frame()/has_end_frame(), is_active(), is_locked(), get_row_index().

Per-section CHANNEL keyframe data IS expanded (this was previously thought to need C++ — it does
NOT):
  * section.get_all_channels() returns concrete MovieSceneScripting*Channel objects (Double/Bool/
    Integer/...), NOT opaque handles. Each channel exposes get_num_keys()/get_keys(), and each key
    object exposes get_time() (-> FrameTime: .frame_number.value + .sub_frame), get_value(),
    get_interpolation_mode(), and (float channels only) get_arrive_tangent()/get_leave_tangent()
    (+ _weight). We read all of these directly from stock Python — no MovieSceneChannelProxy C++,
    no FRichCurve protected-member workaround. Each channel row carries {type, name?, num_keys,
    keys?}; keys are included when include_keys is set (default True) and capped at 1000/channel
    (keys_truncated flag if exceeded). Every getter is guarded, so bool/integer channels (which
    have no tangents) degrade gracefully.
  * Binding/track/section/channel EXTRACTION is validated live against a real sequence:
    /Game/TestLevelSequence (binding SM_Cube2 -> Transform track -> MovieScene3DTransformSection,
    9 channels x 3 keys each). Point it at any LevelSequence asset and it will populate.

Implemented (all read-only):
  - list_level_sequences   (AssetRegistry LevelSequence assets via class_paths API; counts + paths)
  - get_level_sequence_info (rates, playback range frames+seconds, bindings, master tracks, marks)
  - list_sequence_tracks   (tracks + section count/type for whole sequence or a named binding)

Deferred (writes; NOT implemented this batch): create_level_sequence, add/remove binding,
  add/remove track, add/remove section, set playback range / display rate, add spawnable/
  possessable, key channels, bake/export. These mutate the MovieScene and would need
  ScopedEditorTransaction + a per-session ledger inverse (and several risk a creation modal).
  No ledger / no `undo` tool here (this batch performs no mutations).
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

    # Shared Unreal-side helpers for sequencer reads. No triple-single-quote / no backslash here.
    #   _load_seq(path) -> (seq, err): load + verify it is a LevelSequence.
    #   _framerate(fr)  -> {numerator, denominator, fps} from an unreal.FrameRate.
    #   _class_name(o)  -> best-effort class short name of an object (or None).
    #   _channel_info(ch, with_keys) -> {type, name?, num_keys, keys?[, keys_truncated]}.
    #   _section_info(s, with_keys) -> {type, frames, active/locked, channel_count, channels[]}.
    #   _track_info(t, with_sections, with_keys) -> {type, display_name, section_count, sections?}.
    #   _binding_row(b, with_tracks, with_sections, with_keys) -> {..., track_count, tracks?}.
    _SEQ_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
_LS_PATH = unreal.TopLevelAssetPath("/Script/LevelSequence", "LevelSequence")
def _load_seq(path):
    if not path:
        return None, "no sequence_path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.LevelSequence):
        return None, "asset is not a LevelSequence (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _framerate(fr):
    if fr is None:
        return None
    try:
        num = int(fr.numerator); den = int(fr.denominator)
    except Exception:
        try:
            num = int(fr.get_editor_property("numerator")); den = int(fr.get_editor_property("denominator"))
        except Exception:
            return None
    fps = (float(num) / float(den)) if den else None
    return {"numerator": num, "denominator": den, "fps": (round(fps, 6) if fps is not None else None)}
def _class_name(o):
    if o is None:
        return None
    try:
        return o.get_name()
    except Exception:
        try:
            return str(o)
        except Exception:
            return None
def _bound_class_name(b):
    try:
        c = b.get_possessed_object_class()
        return c.get_name() if c is not None else None
    except Exception:
        return None
def _short_interp(v):
    s = None
    try:
        s = getattr(v, "name", None)
    except Exception:
        s = None
    if not s:
        try:
            s = str(v)
        except Exception:
            return None
    for sep in (".", ":"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    s = s.strip().rstrip(">").strip()
    if "_" in s:
        s = s.split("_", 1)[-1]
    return s or None
def _key_val(v):
    if isinstance(v, float):
        return round(v, 6)
    return v
def _channel_info(ch, with_keys):
    d = {"type": None, "num_keys": None}
    try:
        d["type"] = ch.get_class().get_name()
    except Exception:
        pass
    nm = None
    try:
        nm = ch.get_editor_property("channel_name")
    except Exception:
        nm = None
    if nm is None:
        try:
            nm = ch.get_name()
        except Exception:
            nm = None
    if nm is not None:
        sn = str(nm)
        if sn and sn != "None":
            d["name"] = sn
    try:
        d["num_keys"] = ch.get_num_keys()
    except Exception:
        d["num_keys"] = None
    if with_keys:
        raw = []
        try:
            raw = list(ch.get_keys() or [])
        except Exception:
            raw = []
        if len(raw) > 1000:
            raw = raw[:1000]
            d["keys_truncated"] = True
        keys = []
        for k in raw:
            kd = {}
            try:
                ft = k.get_time()
                kd["frame"] = int(ft.frame_number.value)
                try:
                    sf = float(ft.sub_frame)
                    if sf:
                        kd["sub_frame"] = round(sf, 6)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                kd["value"] = _key_val(k.get_value())
            except Exception:
                pass
            try:
                kd["interp"] = _short_interp(k.get_interpolation_mode())
            except Exception:
                pass
            try:
                at = k.get_arrive_tangent()
                kd["arrive_tangent"] = round(at, 6) if isinstance(at, float) else at
            except Exception:
                pass
            try:
                lt = k.get_leave_tangent()
                kd["leave_tangent"] = round(lt, 6) if isinstance(lt, float) else lt
            except Exception:
                pass
            keys.append(kd)
        d["keys"] = keys
    return d
def _section_info(s, with_keys):
    r = {"type": None, "start_frame": None, "end_frame": None,
         "start_seconds": None, "end_seconds": None, "row_index": None,
         "is_active": None, "is_locked": None}
    try:
        r["type"] = s.get_class().get_name()
    except Exception:
        pass
    try:
        r["start_frame"] = (s.get_start_frame() if s.has_start_frame() else None)
    except Exception:
        pass
    try:
        r["end_frame"] = (s.get_end_frame() if s.has_end_frame() else None)
    except Exception:
        pass
    try:
        r["start_seconds"] = (round(s.get_start_frame_seconds(), 6) if s.has_start_frame() else None)
    except Exception:
        pass
    try:
        r["end_seconds"] = (round(s.get_end_frame_seconds(), 6) if s.has_end_frame() else None)
    except Exception:
        pass
    try:
        r["row_index"] = s.get_row_index()
    except Exception:
        pass
    try:
        r["is_active"] = bool(s.is_active())
    except Exception:
        pass
    try:
        r["is_locked"] = bool(s.is_locked())
    except Exception:
        pass
    try:
        chans = s.get_all_channels() or []
        r["channel_count"] = len(chans)
        r["channels"] = [_channel_info(c, with_keys) for c in chans]
    except Exception:
        r["channel_count"] = None
    return r
def _track_info(t, with_sections, with_keys):
    r = {"type": None, "display_name": None, "section_count": 0}
    try:
        r["type"] = t.get_class().get_name()
    except Exception:
        pass
    try:
        dn = t.get_display_name()
        r["display_name"] = str(dn) if dn is not None else None
    except Exception:
        r["display_name"] = None
    secs = []
    try:
        secs = list(t.get_sections() or [])
    except Exception:
        secs = []
    r["section_count"] = len(secs)
    if with_sections:
        r["sections"] = [_section_info(s, with_keys) for s in secs]
    return r
def _binding_row(b, with_tracks, with_sections, with_keys):
    r = {"name": None, "display_name": None, "bound_object_class": None,
         "track_count": 0, "sorting_order": None}
    try:
        r["name"] = str(b.get_name())
    except Exception:
        pass
    try:
        dn = b.get_display_name()
        r["display_name"] = str(dn) if dn is not None else None
    except Exception:
        pass
    r["bound_object_class"] = _bound_class_name(b)
    try:
        r["sorting_order"] = b.get_sorting_order()
    except Exception:
        pass
    tracks = []
    try:
        tracks = list(b.get_tracks() or [])
    except Exception:
        tracks = []
    r["track_count"] = len(tracks)
    if with_tracks:
        r["tracks"] = [_track_info(t, with_sections, with_keys) for t in tracks]
    return r
'''

    # ------------------------------------------------------------------ #
    # list_level_sequences — AssetRegistry LevelSequence enumeration       #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _SEQ_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
assets = []
scan_error = None
try:
    flt = unreal.ARFilter(class_paths=[_LS_PATH], recursive_paths=recursive,
                          package_paths=[package_path])
    assets = ar.get_assets(flt) or []
except Exception as e:
    scan_error = str(e)
rows = []
seen = set()
for d in assets:
    try:
        pkg = str(d.get_editor_property("package_name"))
        nm = str(d.get_editor_property("asset_name"))
    except Exception:
        continue
    full = pkg + "." + nm
    if full in seen:
        continue
    if name_filter and name_filter.lower() not in nm.lower():
        continue
    seen.add(full)
    rows.append({"name": nm, "path": full, "package": pkg})
rows.sort(key=lambda r: r["name"].lower())
total = len(rows)
if max_results:
    rows = rows[:int(max_results)]
result = {"status": "success", "class": "LevelSequence",
          "search_path": package_path, "recursive": recursive,
          "total": total, "returned": len(rows), "level_sequences": rows,
          "note": "Enumerated via AssetRegistry class_paths API "
                  "(TopLevelAssetPath /Script/LevelSequence.LevelSequence); in UE 5.8 "
                  "LevelSequence is NOT in /Script/Engine, so class_names=['LevelSequence'] "
                  "does not resolve it. path/full are object paths for get_level_sequence_info."}
if scan_error:
    result["scan_error"] = scan_error
if total == 0:
    result["hint"] = ("No LevelSequence assets under this path. This project (Third Person "
                      "template) ships with none anywhere (/Game and /Engine both empty).")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_level_sequences(ctx, path: str = "/Game", recursive: bool = True,
                             name_filter: str = None, max_results: int = None) -> str:
        """List LevelSequence (cinematic) assets under a content path. Read-only; no asset load.

        path:        content root to scan (default '/Game'); e.g. '/Game/Cinematics'.
        recursive:   recurse into sub-paths (default True).
        name_filter: case-insensitive substring on the sequence name.
        max_results: cap the number returned.

        Enumerated via the AssetRegistry class_paths API. In UE 5.8 LevelSequence lives in the
        /Script/LevelSequence module (not /Script/Engine), so this uses TopLevelAssetPath rather
        than a bare class name. Each row is name, path (object path), package. Pass a row's 'path'
        to get_level_sequence_info / list_sequence_tracks."""
        params = {"path": path, "recursive": recursive,
                  "name_filter": name_filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_level_sequence_info — rates, playback range, bindings, tracks    #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _SEQ_HELPERS + r'''
path = PARAMS.get("sequence_path")
with_tracks = PARAMS.get("include_binding_tracks")
with_tracks = True if with_tracks is None else bool(with_tracks)
with_keys = PARAMS.get("include_keys")
with_keys = True if with_keys is None else bool(with_keys)
seq, err = _load_seq(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    display_rate = _framerate(seq.get_display_rate()) if hasattr(seq, "get_display_rate") else None
    tick_res = _framerate(seq.get_tick_resolution()) if hasattr(seq, "get_tick_resolution") else None
    # playback range: frames (tick-resolution frame numbers) + seconds
    pb_start = pb_end = None
    pb_start_s = pb_end_s = None
    try:
        pb_start = seq.get_playback_start()
    except Exception:
        pass
    try:
        pb_end = seq.get_playback_end()
    except Exception:
        pass
    try:
        pb_start_s = round(seq.get_playback_start_seconds(), 6)
    except Exception:
        pass
    try:
        pb_end_s = round(seq.get_playback_end_seconds(), 6)
    except Exception:
        pass
    duration_s = (round(pb_end_s - pb_start_s, 6) if (pb_start_s is not None and pb_end_s is not None) else None)
    # bindings
    bindings = []
    binding_error = None
    try:
        for b in (seq.get_bindings() or []):
            bindings.append(_binding_row(b, with_tracks, with_tracks, with_keys))
    except Exception as e:
        binding_error = str(e)
    # master / root tracks (non-binding tracks on the movie scene)
    master_tracks = []
    master_error = None
    try:
        for t in (seq.get_tracks() or []):
            master_tracks.append(_track_info(t, with_keys, with_keys))
    except Exception as e:
        master_error = str(e)
    # possessable/spawnable split + marked frames
    n_poss = n_spawn = None
    try:
        n_poss = len(seq.get_possessables() or [])
    except Exception:
        pass
    try:
        n_spawn = len(seq.get_spawnables() or [])
    except Exception:
        pass
    marks = []
    try:
        for m in (seq.get_marked_frames() or []):
            try:
                marks.append({"label": str(m.label), "frame": int(m.frame_number.value)})
            except Exception:
                pass
    except Exception:
        marks = []
    result = {"status": "success",
              "level_sequence": {"name": seq.get_name(), "path": seq.get_path_name()},
              "display_rate": display_rate, "tick_resolution": tick_res,
              "playback_range": {
                  "start_frame": pb_start, "end_frame": pb_end,
                  "start_seconds": pb_start_s, "end_seconds": pb_end_s,
                  "duration_seconds": duration_s,
                  "note": "start/end_frame are tick-resolution frame numbers; divide by "
                          "tick_resolution.fps for seconds (or use *_seconds)."},
              "binding_count": len(bindings),
              "possessable_count": n_poss, "spawnable_count": n_spawn,
              "bindings": bindings,
              "master_track_count": len(master_tracks),
              "master_tracks": master_tracks,
              "marked_frames": marks,
              "note": "Read directly off the loaded unreal.LevelSequence (no Sequencer editor "
                      "opened). Bindings are object bindings (possessables + spawnables); each "
                      "carries its bound object class and its property tracks. master_tracks are "
                      "sequence-level (root) tracks not attached to a binding. Per-section channels "
                      "carry real keyframe data (frame/value/interp/tangents) when include_keys is set "
                      "(default True); channel_count is always present."}
    if binding_error:
        result["binding_error"] = binding_error
    if master_error:
        result["master_track_error"] = master_error
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_level_sequence_info(ctx, sequence_path: str,
                                include_binding_tracks: bool = True,
                                include_keys: bool = True) -> str:
        """Summarize a LevelSequence: rates, playback range, bindings, master tracks. Read-only
        (loads the asset; no Sequencer editor opened).

        sequence_path:          object path, e.g. '/Game/Cinematics/MySeq.MySeq'.
        include_binding_tracks: include each binding's track list (default True; set False for a
                                lighter summary with track counts only). When on, each track's
                                sections + channels are expanded too.
        include_keys:           expand each section channel's real keyframe list (frame/value/interp/
                                tangents), capped at 1000 keys per channel (default True). Set False
                                to keep just channel_count / num_keys.

        Returns display_rate and tick_resolution (as numerator/denominator/fps), the playback range
        in both frame numbers (tick resolution) and seconds (+ duration), possessable/spawnable
        counts, every object binding (name, display name, bound object class, track count, and its
        tracks when requested), the sequence-level master/root tracks, and any marked frames."""
        params = {"sequence_path": sequence_path, "include_binding_tracks": include_binding_tracks,
                  "include_keys": include_keys}
        try:
            return json.dumps(_exec(_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_sequence_tracks — tracks (+section count/type) for seq/binding  #
    # ------------------------------------------------------------------ #
    _TRACKS_BODY = _SEQ_HELPERS + r'''
path = PARAMS.get("sequence_path")
binding_sel = PARAMS.get("binding")
with_sections = PARAMS.get("include_sections")
with_sections = True if with_sections is None else bool(with_sections)
with_keys = PARAMS.get("include_keys")
with_keys = True if with_keys is None else bool(with_keys)
seq, err = _load_seq(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    scope = None
    tracks_out = []
    if binding_sel:
        # find the binding by display name or internal name
        target = None
        available = []
        try:
            for b in (seq.get_bindings() or []):
                dn = None; nm = None
                try:
                    dn = str(b.get_display_name())
                except Exception:
                    pass
                try:
                    nm = str(b.get_name())
                except Exception:
                    pass
                available.append(dn or nm)
                if binding_sel in (dn, nm):
                    target = b
                    break
        except Exception as e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding enumeration failed: %s" % e}))
            target = "ERRORED"
        if target is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "binding not found: %s" % binding_sel,
                "available_bindings": available}))
        elif target != "ERRORED":
            scope = {"kind": "binding", "binding": binding_sel,
                     "bound_object_class": _bound_class_name(target)}
            try:
                for t in (target.get_tracks() or []):
                    tracks_out.append(_track_info(t, with_sections, with_keys))
            except Exception as e:
                scope["track_error"] = str(e)
            result = {"status": "success",
                      "level_sequence": {"name": seq.get_name(), "path": seq.get_path_name()},
                      "scope": scope, "track_count": len(tracks_out), "tracks": tracks_out,
                      "note": "Tracks for one object binding. section_count is per track; set "
                              "include_sections for per-section type/frame range/channels, and "
                              "include_keys (default True) for each channel's real keyframes."}
            print("@@UMCP@@" + json.dumps(result))
    else:
        # whole sequence: master/root tracks + a per-binding roll-up of tracks
        scope = {"kind": "sequence"}
        try:
            for t in (seq.get_tracks() or []):
                tracks_out.append(_track_info(t, with_sections, with_keys))
        except Exception as e:
            scope["master_track_error"] = str(e)
        binding_tracks = []
        try:
            for b in (seq.get_bindings() or []):
                row = {"binding": None, "bound_object_class": _bound_class_name(b), "tracks": []}
                try:
                    row["binding"] = str(b.get_display_name())
                except Exception:
                    try:
                        row["binding"] = str(b.get_name())
                    except Exception:
                        pass
                for t in (b.get_tracks() or []):
                    row["tracks"].append(_track_info(t, with_sections, with_keys))
                binding_tracks.append(row)
        except Exception as e:
            scope["binding_track_error"] = str(e)
        total_tracks = len(tracks_out) + sum(len(r["tracks"]) for r in binding_tracks)
        result = {"status": "success",
                  "level_sequence": {"name": seq.get_name(), "path": seq.get_path_name()},
                  "scope": scope,
                  "master_track_count": len(tracks_out),
                  "master_tracks": tracks_out,
                  "binding_track_groups": binding_tracks,
                  "total_track_count": total_tracks,
                  "note": "Whole-sequence view: master_tracks are sequence-level (root) tracks; "
                          "binding_track_groups lists tracks under each object binding. Pass "
                          "binding=<name> to scope to one binding. include_sections adds per-section "
                          "type / frame range / channels; include_keys (default True) adds each "
                          "channel's real keyframes (frame/value/interp/tangents)."}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_sequence_tracks(ctx, sequence_path: str, binding: str = None,
                             include_sections: bool = True,
                             include_keys: bool = True) -> str:
        """List the tracks of a LevelSequence — for the whole sequence or one object binding.
        Read-only (loads the asset; no Sequencer editor opened).

        sequence_path:    object path of the LevelSequence.
        binding:          optional binding display name or internal name; scope tracks to just that
                          binding. Omit for the whole sequence (master/root tracks + a per-binding
                          roll-up). If not found, the error lists available_bindings.
        include_sections: include per-section detail (type, start/end frame + seconds, row index,
                          active/locked, channel_count, channels) for each track (default True).
        include_keys:     for each section channel, expand its real keyframe list (frame, value,
                          interp, arrive/leave tangents on float channels), capped at 1000 keys per
                          channel with a keys_truncated flag (default True). Set False for just
                          channel_count / per-channel num_keys.

        Each track reports its type (MovieScene*Track class name), display name, and section_count.
        Each section reports its channels; every channel carries type/name/num_keys, plus the full
        key list when include_keys is set. Key times come from FrameTime (frame_number.value, in
        tick resolution) and values are read straight off the scripting channel — no C++ needed."""
        params = {"sequence_path": sequence_path, "binding": binding,
                  "include_sections": include_sections, "include_keys": include_keys}
        try:
            return json.dumps(_exec(_TRACKS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (writes; not implemented this batch): create_level_sequence,
    #   add/remove object binding, add/remove track, add/remove section, key
    #   channels, set playback range / display rate, add spawnable/possessable,
    #   bake/export. These mutate the MovieScene and need ScopedEditorTransaction
    #   + a per-session ledger inverse (several also risk an asset-creation modal).
    #   No `undo` tool is defined here (read-only batch).
