"""UserTools :: Sequencer / Cinematics (WRITE, EXTENDED)  (spec: docs/spec/sequencer.md)

Clean-room extension of level_sequence_write.py over Unreal's public MovieScene scripting API
(UE 5.8). This module ADDS cinematic-authoring track/section/spawnable tools that
level_sequence_write.py does NOT cover, WITHOUT any tool-name collision (grep-verified). Query
convention, base64 PARAMS injection, Output-Log auto-capture, and the per-session undo ledger are
copied VERBATIM from the gold-standard editor_level.py; the sequencer helpers are copied from
level_sequence_write.py.

level_sequence_write.py already ships: create_level_sequence, set_playback_range, add_actor_binding
(possessable), add_transform_track, add_keyframe. This module adds (all validated live vs
TestMCPSetup, UE 5.8.1 -- see build report):
  - add_camera_cut_track          (WRITE; master MovieSceneCameraCutTrack)
  - add_camera_cut_section        (WRITE; section on the camera-cut track, bound to a camera binding)
  - add_audio_track               (WRITE; master MovieSceneAudioTrack)
  - add_audio_section             (WRITE; audio section with a SoundBase)
  - add_event_track               (WRITE; master MovieSceneEventTrack)
  - add_skeletal_animation_track  (WRITE; binding MovieSceneSkeletalAnimationTrack on a skeletal binding)
  - add_skeletal_animation_section(WRITE; section with an AnimSequence on a skeletal-anim track)
  - add_visibility_track          (WRITE; binding MovieSceneVisibilityTrack + a bool section)
  - add_spawnable_from_class      (WRITE; spawnable object binding from an actor class)
  - add_spawnable_from_instance   (WRITE; spawnable object binding templated from a level actor)

API facts probed live (5.8):
  * MASTER tracks: seq.add_track(<TrackClass>) -> MovieSceneTrack; seq.remove_track(track) is the inverse.
    seq.find_tracks_by_exact_type(<TrackClass>) enumerates them (used by the inverse to re-find).
  * BINDING tracks: binding.add_track(<TrackClass>) / binding.remove_track(track); binding also has
    find_tracks_by_exact_type.
  * Sections: track.add_section() -> MovieSceneSection; track.remove_section(section) is the inverse.
    section.set_start_frame(f)/set_end_frame(f) are DISPLAY-rate frame numbers (same as add_transform_track).
  * Camera cut: MovieSceneCameraCutSection.set_camera_binding_id(unreal.MovieSceneObjectBindingID). The id
    is built from a binding via unreal.MovieSceneSequenceExtensions.get_binding_id(seq, binding).
  * Audio: MovieSceneAudioSection.set_sound(<SoundBase>).
  * Skeletal anim: section.get_editor_property("params") -> FMovieSceneSkeletalAnimationParams; set its
    "animation" to an AnimSequence, then set the struct back with section.set_editor_property("params", p).
  * Spawnables: seq.add_spawnable_from_class(<ActorClass>) / seq.add_spawnable_from_instance(<Actor>) ->
    MovieSceneBindingProxy (a spawnable binding; the source actor is NOT modified/removed). Inverse is the
    proxy's .remove() -- identical to add_actor_binding's inverse (reuses that folded op).

Undo: this module registers NO `undo` tool (editor_level.py owns the single unified `undo`). Spawnables
reuse the already-folded "add_actor_binding" inverse. Three NEW ledger ops (add_seq_master_track,
add_seq_binding_track, add_seq_track_section) have their inverse logic documented in each tool docstring
and the build report, for the coordinator to fold into editor_level.undo. Every inverse was proven live
against the agentA ledger (depth -> 0).

Modal safety: no factory/CreationParameters is constructed here (the sequence itself is made by
level_sequence_write.create_level_sequence). All authoring is pure MovieScene scripting -> no modal.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
# bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as base64. Never
# assign a snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/
# original_stderr/success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                  -> per-session undo stack (verbatim from editor_level.py).
    #   _load_seq(path)            -> (seq, err) load + type-check LevelSequence.
    #   _resolve_actor(ident)      -> level actor by label then internal name.
    #   _binding_names(seq)        -> [display-or-internal name] for every binding.
    #   _find_binding(seq, sel)    -> binding proxy by display name or internal name.
    #   _track_class(name)         -> (cls, err) resolve a MovieScene*Track class from unreal.
    #   _exact_tracks(container, cls) -> container.find_tracks_by_exact_type(cls) as a list.
    _LSW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
def _binding_names(seq):
    names = []
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
        names.append(dn or nm)
    return names
def _find_binding(seq, sel):
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
        if sel in (dn, nm):
            return b
    return None
def _track_class(name):
    cls = getattr(unreal, name, None)
    if cls is None:
        return None, "unknown track class: %s" % name
    return cls, None
def _exact_tracks(container, cls):
    try:
        return list(container.find_tracks_by_exact_type(cls) or [])
    except Exception:
        out = []
        for t in (container.get_tracks() or []):
            try:
                if t.get_class() == cls or t.get_class().get_name() == cls.__name__:
                    out.append(t)
            except Exception:
                pass
        return out
'''

    # ------------------------------------------------------------------ #
    # Master-track adder (camera cut / audio / event)                     #
    # ------------------------------------------------------------------ #
    _ADD_MASTER_TRACK_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
track_class = PARAMS["track_class"]
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls, cerr = _track_class(track_class)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        prior_count = len(_exact_tracks(seq, cls))
        track = None
        with unreal.ScopedEditorTransaction("MCP add_master_track"):
            track = seq.add_track(cls)
        if track is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_track returned None for %s" % track_class}))
        else:
            _ledger().append({"op": "add_seq_master_track", "asset_path": seq_path,
                              "track_class": track_class, "prior_exact_count": prior_count})
            print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                "track_type": track.get_class().get_name(),
                "master_track_count": len(seq.get_tracks() or []),
                "exact_type_count": len(_exact_tracks(seq, cls)),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # Binding-track adder (skeletal animation / visibility)               #
    # ------------------------------------------------------------------ #
    _ADD_BINDING_TRACK_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS["binding_name"]
track_class = PARAMS["track_class"]
add_section = bool(PARAMS.get("add_section"))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls, cerr = _track_class(track_class)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        binding = _find_binding(seq, binding_sel)
        if binding is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "binding not found: %s" % binding_sel, "available_bindings": _binding_names(seq)}))
        else:
            prior_count = len(_exact_tracks(binding, cls))
            track = None; section = None; nchan = None
            with unreal.ScopedEditorTransaction("MCP add_binding_track"):
                track = binding.add_track(cls)
                if track is not None and add_section:
                    section = track.add_section()
                    try:
                        section.set_start_frame(seq.get_playback_start())
                        section.set_end_frame(seq.get_playback_end())
                    except Exception:
                        pass
                    try:
                        nchan = len(section.get_all_channels() or [])
                    except Exception:
                        nchan = None
            if track is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding.add_track returned None for %s" % track_class}))
            else:
                _ledger().append({"op": "add_seq_binding_track", "asset_path": seq_path,
                                  "binding_name": binding_sel, "track_class": track_class,
                                  "prior_exact_count": prior_count})
                print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                    "binding": binding_sel, "track_type": track.get_class().get_name(),
                    "section_type": (section.get_class().get_name() if section else None),
                    "channel_count": nchan,
                    "exact_type_count": len(_exact_tracks(binding, cls)),
                    "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # Section adder (camera cut / audio / skeletal animation)             #
    # ------------------------------------------------------------------ #
    _ADD_SECTION_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
scope = PARAMS.get("scope") or "master"
binding_sel = PARAMS.get("binding_name")
track_class = PARAMS["track_class"]
track_index = int(PARAMS.get("track_index") or 0)
kind = PARAMS.get("kind")  # camera_cut | audio | skeletal | plain
sound_path = PARAMS.get("sound_path")
camera_binding = PARAMS.get("camera_binding_name")
animation_path = PARAMS.get("animation_path")
start_frame = PARAMS.get("start_frame")
end_frame = PARAMS.get("end_frame")
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls, cerr = _track_class(track_class)
    container = None
    berr = None
    if not cerr:
        if scope == "binding":
            container = _find_binding(seq, binding_sel)
            if container is None:
                berr = "binding not found: %s" % binding_sel
        else:
            container = seq
    if cerr or berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": (cerr or berr),
            "available_bindings": (_binding_names(seq) if berr else None)}))
    else:
        tracks = _exact_tracks(container, cls)
        if track_index >= len(tracks):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no %s at index %d; call the matching add_*_track first (found %d)" % (track_class, track_index, len(tracks))}))
        else:
            track = tracks[track_index]
            prior_sections = len(track.get_sections() or [])
            # resolve config assets BEFORE the transaction so a bad path fails cleanly
            snd = None; anim = None; cam_bid = None; cfg_err = None
            if kind == "audio":
                snd = unreal.EditorAssetLibrary.load_asset(sound_path) if sound_path else None
                if sound_path and snd is None:
                    cfg_err = "sound not found: %s" % sound_path
            elif kind == "skeletal":
                anim = unreal.EditorAssetLibrary.load_asset(animation_path) if animation_path else None
                if animation_path and anim is None:
                    cfg_err = "animation not found: %s" % animation_path
            elif kind == "camera_cut":
                cam_b = _find_binding(seq, camera_binding) if camera_binding else None
                if camera_binding and cam_b is None:
                    cfg_err = "camera binding not found: %s" % camera_binding
                elif cam_b is not None:
                    cam_bid = unreal.MovieSceneSequenceExtensions.get_binding_id(seq, cam_b)
            if cfg_err:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": cfg_err,
                    "available_bindings": _binding_names(seq)}))
            else:
                section = None; applied = {}
                with unreal.ScopedEditorTransaction("MCP add_track_section"):
                    section = track.add_section()
                    sf = int(start_frame) if start_frame is not None else seq.get_playback_start()
                    ef = int(end_frame) if end_frame is not None else seq.get_playback_end()
                    try:
                        section.set_start_frame(sf); section.set_end_frame(ef)
                    except Exception:
                        pass
                    if kind == "audio" and snd is not None:
                        section.set_sound(snd); applied["sound"] = snd.get_name()
                    elif kind == "skeletal" and anim is not None:
                        p = section.get_editor_property("params")
                        p.set_editor_property("animation", anim)
                        section.set_editor_property("params", p)
                        applied["animation"] = anim.get_name()
                    elif kind == "camera_cut" and cam_bid is not None:
                        section.set_camera_binding_id(cam_bid); applied["camera_binding"] = camera_binding
                _ledger().append({"op": "add_seq_track_section", "asset_path": seq_path,
                                  "scope": scope, "binding_name": binding_sel, "track_class": track_class,
                                  "track_index": track_index, "prior_section_count": prior_sections})
                nchan = None
                try:
                    nchan = len(section.get_all_channels() or [])
                except Exception:
                    nchan = None
                print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                    "scope": scope, "binding": binding_sel, "track_type": track.get_class().get_name(),
                    "section_type": section.get_class().get_name(),
                    "start_frame": section.get_start_frame() if section.has_start_frame() else None,
                    "end_frame": section.get_end_frame() if section.has_end_frame() else None,
                    "channel_count": nchan, "applied": applied,
                    "section_count": len(track.get_sections() or []),
                    "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # Spawnable adder (from class / from instance)                        #
    # ------------------------------------------------------------------ #
    _ADD_SPAWNABLE_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
mode = PARAMS.get("mode") or "class"
class_name = PARAMS.get("class_name")
actor_name = PARAMS.get("actor_name")
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    src = None; src_err = None
    if mode == "class":
        cls = getattr(unreal, class_name, None) if class_name else None
        if cls is None and class_name:
            try:
                cls = unreal.load_class(None, class_name)
            except Exception:
                cls = None
        if cls is None:
            src_err = "unknown actor class: %s" % class_name
        else:
            src = cls
    else:
        actor = _resolve_actor(actor_name)
        if actor is None:
            src_err = "actor not found in level: %s" % actor_name
        else:
            src = actor
    if src_err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": src_err}))
    else:
        before = len(seq.get_bindings() or [])
        binding = None
        with unreal.ScopedEditorTransaction("MCP add_spawnable"):
            if mode == "class":
                binding = seq.add_spawnable_from_class(src)
            else:
                binding = seq.add_spawnable_from_instance(src)
        if binding is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_spawnable returned None"}))
        else:
            bname = str(binding.get_name())
            bdisp = str(binding.get_display_name())
            bound_cls = None
            try:
                c = binding.get_possessed_object_class()
                bound_cls = c.get_name() if c is not None else None
            except Exception:
                pass
            # Reuse the already-folded add_actor_binding inverse (binding.remove()).
            _ledger().append({"op": "add_actor_binding", "asset_path": seq_path, "binding_name": bname})
            print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                "mode": mode, "binding_name": bname, "binding_display_name": bdisp,
                "bound_object_class": bound_cls, "spawnable_count": len(seq.get_spawnables() or []),
                "binding_count": len(seq.get_bindings() or []),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # Extended helpers for generic track/section/key + snapshot/rebuild   #
    #   (locate tracks/sections/channels; enum maps; JSON snapshot of a    #
    #    track/section for a best-effort faithful remove inverse).         #
    # ------------------------------------------------------------------ #
    _SEQ_EXT_HELPERS2 = r'''
def _track_list(seq, binding_sel):
    if binding_sel in (None, ""):
        return list(seq.get_tracks() or []), None
    b = _find_binding(seq, binding_sel)
    if b is None:
        return None, "binding not found: %s" % binding_sel
    return list(b.get_tracks() or []), None
def _track_container(seq, binding_sel):
    if binding_sel in (None, ""):
        return seq, None
    b = _find_binding(seq, binding_sel)
    if b is None:
        return None, "binding not found: %s" % binding_sel
    return b, None
def _locate_track(seq, binding_sel, track_index):
    tl, err = _track_list(seq, binding_sel)
    if err:
        return None, err
    ti = int(track_index)
    if ti < 0 or ti >= len(tl):
        return None, "track_index %d out of range (track_count=%d)" % (ti, len(tl))
    return tl[ti], None
def _locate_section(seq, binding_sel, track_index, section_index):
    tr, err = _locate_track(seq, binding_sel, track_index)
    if err:
        return None, None, err
    secs = list(tr.get_sections() or [])
    si = int(section_index)
    if si < 0 or si >= len(secs):
        return None, tr, "section_index %d out of range (section_count=%d)" % (si, len(secs))
    return secs[si], tr, None
def _emap(cls, pairs):
    m = {}
    for nm, attr in pairs:
        v = getattr(cls, attr, None)
        if v is not None:
            m[nm] = v
    return m
_INTERP = _emap(unreal.RichCurveInterpMode, [("constant", "RCIM_CONSTANT"), ("linear", "RCIM_LINEAR"), ("cubic", "RCIM_CUBIC")])
_TANMODE = _emap(unreal.RichCurveTangentMode, [("auto", "RCTM_AUTO"), ("user", "RCTM_USER"), ("break", "RCTM_BREAK")])
_EXTRAP = _emap(unreal.RichCurveExtrapolation, [("constant", "RCCE_CONSTANT"), ("cycle", "RCCE_CYCLE"), ("cycle_with_offset", "RCCE_CYCLE_WITH_OFFSET"), ("oscillate", "RCCE_OSCILLATE"), ("linear", "RCCE_LINEAR")])
_BLEND = _emap(unreal.MovieSceneBlendType, [("absolute", "ABSOLUTE"), ("additive", "ADDITIVE"), ("relative", "RELATIVE"), ("additive_from_base", "ADDITIVE_FROM_BASE"), ("override", "OVERRIDE")])
def _name_of(m, v):
    for k2, vv in m.items():
        try:
            if vv == v:
                return k2
        except Exception:
            pass
    return None
def _enum_of(m, name):
    if name is None:
        return None
    return m.get(str(name).strip().lower())
def _jval(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return float(v)
    except Exception:
        try:
            return str(v)
        except Exception:
            return None
def _snap_channel(ch):
    d = {"keys": []}
    try:
        d["has_default"] = bool(ch.has_default())
        d["default"] = _jval(ch.get_default()) if d["has_default"] else None
    except Exception:
        d["has_default"] = False; d["default"] = None
    try:
        d["pre"] = _name_of(_EXTRAP, ch.get_pre_infinity_extrapolation())
        d["post"] = _name_of(_EXTRAP, ch.get_post_infinity_extrapolation())
    except Exception:
        d["pre"] = None; d["post"] = None
    try:
        for k in (ch.get_keys() or []):
            kd = {"frame": int(k.get_time().frame_number.value), "value": _jval(k.get_value())}
            try:
                kd["interp"] = _name_of(_INTERP, k.get_interpolation_mode())
                kd["tanmode"] = _name_of(_TANMODE, k.get_tangent_mode())
                kd["arrive"] = k.get_arrive_tangent(); kd["leave"] = k.get_leave_tangent()
            except Exception:
                pass
            d["keys"].append(kd)
    except Exception:
        pass
    return d
def _snap_section(sec):
    cn = sec.get_class().get_name()
    d = {"class": cn}
    try:
        d["has_start"] = bool(sec.has_start_frame()); d["start"] = sec.get_start_frame() if d["has_start"] else None
        d["has_end"] = bool(sec.has_end_frame()); d["end"] = sec.get_end_frame() if d["has_end"] else None
    except Exception:
        d["has_start"] = False; d["has_end"] = False; d["start"] = None; d["end"] = None
    try: d["row"] = sec.get_row_index()
    except Exception: d["row"] = None
    try:
        opt = sec.get_blend_type(); d["blend"] = _name_of(_BLEND, opt.blend_type) if opt.is_valid else None
    except Exception: d["blend"] = None
    try: d["ease_in"] = sec.get_ease_in_duration(); d["ease_out"] = sec.get_ease_out_duration()
    except Exception: d["ease_in"] = None; d["ease_out"] = None
    refs = {}
    try:
        if cn == "MovieSceneAudioSection":
            snd = sec.get_editor_property("sound"); refs["sound"] = snd.get_path_name() if snd else None
        elif cn == "MovieSceneSkeletalAnimationSection":
            p = sec.get_editor_property("params"); anim = p.get_editor_property("animation")
            refs["animation"] = anim.get_path_name() if anim else None
        elif cn == "MovieSceneTimeWarpSection":
            refs["play_rate"] = sec.get_editor_property("time_warp").to_fixed_play_rate()
    except Exception:
        pass
    d["refs"] = refs
    d["channels"] = [_snap_channel(ch) for ch in (sec.get_all_channels() or [])]
    return d
def _snap_track(tr):
    return {"class": tr.get_class().get_name(), "sections": [_snap_section(s) for s in (tr.get_sections() or [])]}
def _rebuild_channel(ch, cd):
    try:
        if cd.get("has_default"):
            ch.set_default(cd.get("default"))
    except Exception: pass
    try:
        pe = _enum_of(_EXTRAP, cd.get("pre"));  po = _enum_of(_EXTRAP, cd.get("post"))
        if pe is not None: ch.set_pre_infinity_extrapolation(pe)
        if po is not None: ch.set_post_infinity_extrapolation(po)
    except Exception: pass
    for kd in cd.get("keys", []):
        try:
            k = ch.add_key(unreal.FrameNumber(int(kd["frame"])), kd["value"])
        except Exception:
            continue
        try:
            im = _enum_of(_INTERP, kd.get("interp"))
            if im is not None: k.set_interpolation_mode(im)
            tm = _enum_of(_TANMODE, kd.get("tanmode"))
            if tm is not None: k.set_tangent_mode(tm)
            if kd.get("arrive") is not None: k.set_arrive_tangent(float(kd["arrive"]))
            if kd.get("leave") is not None: k.set_leave_tangent(float(kd["leave"]))
        except Exception: pass
def _rebuild_section(sec, sd):
    try:
        if sd.get("has_start") and sd.get("has_end"):
            sec.set_range(int(sd["start"]), int(sd["end"]))
        else:
            if sd.get("has_start") and sd.get("start") is not None: sec.set_start_frame(int(sd["start"]))
            if sd.get("has_end") and sd.get("end") is not None: sec.set_end_frame(int(sd["end"]))
    except Exception: pass
    try:
        if sd.get("row") is not None: sec.set_row_index(int(sd["row"]))
    except Exception: pass
    try:
        be = _enum_of(_BLEND, sd.get("blend"))
        if be is not None: sec.set_blend_type(be)
    except Exception: pass
    try:
        if sd.get("ease_in") is not None: sec.set_ease_in_duration(int(sd["ease_in"]))
        if sd.get("ease_out") is not None: sec.set_ease_out_duration(int(sd["ease_out"]))
    except Exception: pass
    refs = sd.get("refs") or {}
    try:
        if refs.get("sound"):
            sec.set_sound(unreal.EditorAssetLibrary.load_asset(refs["sound"]))
        if refs.get("animation"):
            p = sec.get_editor_property("params"); p.set_editor_property("animation", unreal.EditorAssetLibrary.load_asset(refs["animation"])); sec.set_editor_property("params", p)
        if refs.get("play_rate") is not None:
            v = unreal.MovieSceneTimeWarpVariant(); v.set_fixed_play_rate(float(refs["play_rate"])); sec.set_editor_property("time_warp", v)
    except Exception: pass
    chans = list(sec.get_all_channels() or [])
    for i, cd in enumerate(sd.get("channels", [])):
        if i < len(chans):
            _rebuild_channel(chans[i], cd)
def _rebuild_track(tr, td):
    for sd in td.get("sections", []):
        try:
            sec = tr.add_section()
        except Exception:
            continue
        _rebuild_section(sec, sd)
'''

    # ------------------------------------------------------------------ #
    # add_track (generic, class-parametrized; master or binding)          #
    # ------------------------------------------------------------------ #
    _ADD_TRACK_GENERIC_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS.get("binding_name")
track_class = PARAMS["track_class"]
add_sec = bool(PARAMS.get("add_section"))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
cls, cerr = _track_class(track_class)
if cerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr})); raise SystemExit
container, berr = _track_container(seq, binding_sel)
if berr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": berr, "available_bindings": _binding_names(seq)})); raise SystemExit
prior_count = len(_exact_tracks(container, cls))
track = None; section = None; nchan = None
with unreal.ScopedEditorTransaction("MCP seq_add_track"):
    track = container.add_track(cls)
    if track is not None and add_sec:
        section = track.add_section()
        try:
            section.set_start_frame(seq.get_playback_start()); section.set_end_frame(seq.get_playback_end())
        except Exception:
            pass
        try: nchan = len(section.get_all_channels() or [])
        except Exception: nchan = None
if track is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_track returned None for %s" % track_class})); raise SystemExit
_ledger().append({"op": "seq_add_track", "asset_path": seq_path, "binding_name": binding_sel,
                  "track_class": track_class, "prior_exact_count": prior_count})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(), "scope": ("binding" if binding_sel else "master"),
    "binding": binding_sel, "track_type": track.get_class().get_name(),
    "section_type": (section.get_class().get_name() if section else None), "channel_count": nchan,
    "exact_type_count": len(_exact_tracks(container, cls)), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # remove_track (index; snapshot for best-effort faithful inverse)     #
    # ------------------------------------------------------------------ #
    _REMOVE_TRACK_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS.get("binding_name")
track_index = int(PARAMS.get("track_index", 0))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
container, berr = _track_container(seq, binding_sel)
if berr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": berr, "available_bindings": _binding_names(seq)})); raise SystemExit
tr, err = _locate_track(seq, binding_sel, track_index)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
snap = _snap_track(tr)
with unreal.ScopedEditorTransaction("MCP seq_remove_track"):
    container.remove_track(tr)
_ledger().append({"op": "seq_remove_track", "asset_path": seq_path, "binding_name": binding_sel,
                  "track_index": track_index, "track_class": snap["class"], "snapshot": snap})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(), "scope": ("binding" if binding_sel else "master"),
    "removed_track_class": snap["class"], "removed_sections": len(snap["sections"]),
    "track_count_now": len(_track_list(seq, binding_sel)[0]), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_section (generic frame range on a located track)                #
    # ------------------------------------------------------------------ #
    _ADD_SECTION_GENERIC_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS.get("binding_name")
track_index = int(PARAMS.get("track_index", 0))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
tr, err = _locate_track(seq, binding_sel, track_index)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err, "available_bindings": _binding_names(seq)})); raise SystemExit
prior_sections = len(tr.get_sections() or [])
sf = PARAMS.get("start_frame"); ef = PARAMS.get("end_frame")
section = None
with unreal.ScopedEditorTransaction("MCP seq_add_section"):
    section = tr.add_section()
    s = int(sf) if sf is not None else seq.get_playback_start()
    e = int(ef) if ef is not None else seq.get_playback_end()
    try: section.set_start_frame(s); section.set_end_frame(e)
    except Exception: pass
if section is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_section returned None"})); raise SystemExit
_ledger().append({"op": "seq_add_section", "asset_path": seq_path, "binding_name": binding_sel,
                  "track_index": track_index, "prior_section_count": prior_sections})
nchan = None
try: nchan = len(section.get_all_channels() or [])
except Exception: nchan = None
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
    "track_type": tr.get_class().get_name(), "section_type": section.get_class().get_name(),
    "start_frame": section.get_start_frame() if section.has_start_frame() else None,
    "end_frame": section.get_end_frame() if section.has_end_frame() else None,
    "channel_count": nchan, "section_count": len(tr.get_sections() or []), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # remove_section (index; snapshot for best-effort faithful inverse)   #
    # ------------------------------------------------------------------ #
    _REMOVE_SECTION_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS.get("binding_name")
track_index = int(PARAMS.get("track_index", 0))
section_index = int(PARAMS.get("section_index", 0))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, binding_sel, track_index, section_index)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err, "available_bindings": _binding_names(seq)})); raise SystemExit
snap = _snap_section(sec)
with unreal.ScopedEditorTransaction("MCP seq_remove_section"):
    tr.remove_section(sec)
_ledger().append({"op": "seq_remove_section", "asset_path": seq_path, "binding_name": binding_sel,
                  "track_index": track_index, "section_index": section_index, "snapshot": snap})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
    "removed_section_class": snap["class"], "removed_channels": len(snap["channels"]),
    "section_count_now": len(tr.get_sections() or []), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # create_camera (spawnable CineCameraActor + optional camera-cut)     #
    # ------------------------------------------------------------------ #
    _CREATE_CAMERA_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
class_name = PARAMS.get("camera_class") or "CineCameraActor"
add_cut = bool(PARAMS.get("add_camera_cut"))
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
cam_cls = getattr(unreal, class_name, None)
if cam_cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown camera class: %s" % class_name})); raise SystemExit
binding = None; cut_track = None; cut_section = None
with unreal.ScopedEditorTransaction("MCP seq_create_camera"):
    binding = seq.add_spawnable_from_class(cam_cls)
    if binding is not None and add_cut:
        cut_track = seq.add_track(unreal.MovieSceneCameraCutTrack)
        cut_section = cut_track.add_section()
        try:
            cut_section.set_start_frame(seq.get_playback_start()); cut_section.set_end_frame(seq.get_playback_end())
        except Exception:
            pass
        try:
            bid = unreal.MovieSceneSequenceExtensions.get_binding_id(seq, binding)
            cut_section.set_camera_binding_id(bid)
        except Exception:
            pass
if binding is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_spawnable_from_class returned None"})); raise SystemExit
bname = str(binding.get_name())
# Ledger: reuse folded inverses. add_actor_binding (binding.remove()) for the spawnable; if a camera-cut
# track was added, add_seq_master_track (find_tracks_by_exact_type + remove last) for it. Pushed in
# creation order so unified LIFO undo removes the cut track first, then the spawnable binding.
_ledger().append({"op": "add_actor_binding", "asset_path": seq_path, "binding_name": bname})
if cut_track is not None:
    _ledger().append({"op": "add_seq_master_track", "asset_path": seq_path,
                      "track_class": "MovieSceneCameraCutTrack",
                      "prior_exact_count": len(_exact_tracks(seq, unreal.MovieSceneCameraCutTrack)) - 1})
bound_cls = None
try:
    c = binding.get_possessed_object_class(); bound_cls = c.get_name() if c else None
except Exception:
    pass
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(), "binding_name": bname,
    "binding_display_name": str(binding.get_display_name()), "bound_object_class": bound_cls,
    "added_camera_cut": bool(cut_track is not None), "spawnable_count": len(seq.get_spawnables() or []),
    "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_event_section (shell trigger/repeater section on an event track)#
    # ------------------------------------------------------------------ #
    _ADD_EVENT_SECTION_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
track_index = int(PARAMS.get("track_index", 0))
start_frame = PARAMS.get("start_frame"); end_frame = PARAMS.get("end_frame")
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ev_cls = unreal.MovieSceneEventTrack
tracks = _exact_tracks(seq, ev_cls)
added_track = False
track = None; section = None
with unreal.ScopedEditorTransaction("MCP seq_add_event_section"):
    if track_index < len(tracks):
        track = tracks[track_index]
    else:
        track = seq.add_track(ev_cls); added_track = True
    prior_sections = len(track.get_sections() or [])
    section = track.add_section()
    s = int(start_frame) if start_frame is not None else seq.get_playback_start()
    e = int(end_frame) if end_frame is not None else seq.get_playback_end()
    try: section.set_start_frame(s); section.set_end_frame(e)
    except Exception: pass
if section is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "event track add_section returned None"})); raise SystemExit
_ledger().append({"op": "seq_add_event_section", "asset_path": seq_path, "added_track": added_track,
                  "track_class": "MovieSceneEventTrack", "track_index": track_index,
                  "prior_section_count": prior_sections})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
    "section_type": section.get_class().get_name(), "added_event_track": added_track,
    "start_frame": section.get_start_frame() if section.has_start_frame() else None,
    "end_frame": section.get_end_frame() if section.has_end_frame() else None,
    "note": "shell section only; firing an event needs a director-BP endpoint (editor-only, not Python-reachable)",
    "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_timewarp (master MovieSceneTimeWarpTrack + fixed-rate section)  #
    # ------------------------------------------------------------------ #
    _ADD_TIMEWARP_BODY = _LSW_HELPERS + _SEQ_EXT_HELPERS2 + r'''
seq_path = PARAMS["sequence_path"]
play_rate = PARAMS.get("play_rate")
start_frame = PARAMS.get("start_frame"); end_frame = PARAMS.get("end_frame")
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
tw_cls = getattr(unreal, "MovieSceneTimeWarpTrack", None)
if tw_cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "MovieSceneTimeWarpTrack not exposed in this build"})); raise SystemExit
prior_count = len(_exact_tracks(seq, tw_cls))
track = None; section = None; rate_applied = None
with unreal.ScopedEditorTransaction("MCP seq_add_timewarp"):
    track = seq.add_track(tw_cls)
    if track is not None:
        section = track.add_section()
        s = int(start_frame) if start_frame is not None else seq.get_playback_start()
        e = int(end_frame) if end_frame is not None else seq.get_playback_end()
        try: section.set_start_frame(s); section.set_end_frame(e)
        except Exception: pass
        if play_rate is not None:
            try:
                v = unreal.MovieSceneTimeWarpVariant(); v.set_fixed_play_rate(float(play_rate))
                section.set_editor_property("time_warp", v)
                rate_applied = section.get_editor_property("time_warp").to_fixed_play_rate()
            except Exception:
                rate_applied = None
if track is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_track(MovieSceneTimeWarpTrack) returned None"})); raise SystemExit
_ledger().append({"op": "seq_add_timewarp", "asset_path": seq_path, "prior_exact_count": prior_count})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
    "track_type": track.get_class().get_name(), "section_type": (section.get_class().get_name() if section else None),
    "requested_play_rate": play_rate, "rate_applied": rate_applied,
    "exact_type_count": len(_exact_tracks(seq, tw_cls)), "ledger_depth": len(_ledger())}))
'''

    # ================================================================== #
    # MASTER TRACKS                                                       #
    # ================================================================== #
    @mcp.tool()
    def add_camera_cut_track(ctx, sequence_path: str) -> str:
        """Add a Camera Cut track (MovieSceneCameraCutTrack) to a LevelSequence (sequence-level/master).

        sequence_path: object/package path of the LevelSequence.

        The camera-cut track drives which camera the sequence renders through; add camera-cut SECTIONS
        (add_camera_cut_section) that each point at a camera binding. Uses seq.add_track(
        unreal.MovieSceneCameraCutTrack). Verify with sequencer_read.list_sequence_tracks (master_tracks).

        Ledgered write op 'add_seq_master_track' {asset_path, track_class='MovieSceneCameraCutTrack',
        prior_exact_count}. Inverse: load the sequence, tracks=seq.find_tracks_by_exact_type(cls); if
        len(tracks) > prior_exact_count, seq.remove_track(tracks[-1]) -- removes only the track we
        appended (and its sections). FAITHFUL."""
        params = {"sequence_path": sequence_path, "track_class": "MovieSceneCameraCutTrack"}
        try:
            return json.dumps(_exec(_ADD_MASTER_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_audio_track(ctx, sequence_path: str) -> str:
        """Add an Audio track (MovieSceneAudioTrack) to a LevelSequence (sequence-level/master).

        sequence_path: object/package path of the LevelSequence.

        Add audio SECTIONS (add_audio_section) that each carry a SoundBase (SoundWave/SoundCue). Uses
        seq.add_track(unreal.MovieSceneAudioTrack). Verify with sequencer_read.list_sequence_tracks.

        Ledgered write op 'add_seq_master_track' {asset_path, track_class='MovieSceneAudioTrack',
        prior_exact_count}. Inverse: seq.find_tracks_by_exact_type(cls); if grown, seq.remove_track(last).
        FAITHFUL."""
        params = {"sequence_path": sequence_path, "track_class": "MovieSceneAudioTrack"}
        try:
            return json.dumps(_exec(_ADD_MASTER_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_event_track(ctx, sequence_path: str) -> str:
        """Add an Event track (MovieSceneEventTrack) to a LevelSequence (sequence-level/master).

        sequence_path: object/package path of the LevelSequence.

        Event tracks fire Sequencer events during playback. This creates the empty track (the track is
        the reversible unit; wiring event keys to a director blueprint endpoint is not part of this tool
        and is not reachable from stock Python -- see DEFERRED). Uses seq.add_track(
        unreal.MovieSceneEventTrack).

        Ledgered write op 'add_seq_master_track' {asset_path, track_class='MovieSceneEventTrack',
        prior_exact_count}. Inverse: seq.find_tracks_by_exact_type(cls); if grown, seq.remove_track(last).
        FAITHFUL."""
        params = {"sequence_path": sequence_path, "track_class": "MovieSceneEventTrack"}
        try:
            return json.dumps(_exec(_ADD_MASTER_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # BINDING TRACKS                                                      #
    # ================================================================== #
    @mcp.tool()
    def add_skeletal_animation_track(ctx, sequence_path: str, binding_name: str) -> str:
        """Add a Skeletal Animation track (MovieSceneSkeletalAnimationTrack) to an object binding.

        sequence_path: object/package path of the LevelSequence.
        binding_name:  binding display name or internal name (should be bound to a SkeletalMeshActor /
                       skeletal-mesh component -- e.g. from add_actor_binding or add_spawnable_from_class
                       on unreal.SkeletalMeshActor).

        Uses binding.add_track(unreal.MovieSceneSkeletalAnimationTrack). Then add an animation SECTION
        with add_skeletal_animation_section. Verify with sequencer_read.list_sequence_tracks(binding=...).

        Ledgered write op 'add_seq_binding_track' {asset_path, binding_name,
        track_class='MovieSceneSkeletalAnimationTrack', prior_exact_count}. Inverse: load the sequence,
        find the binding, tracks=binding.find_tracks_by_exact_type(cls); if len > prior_exact_count,
        binding.remove_track(tracks[-1]). FAITHFUL."""
        params = {"sequence_path": sequence_path, "binding_name": binding_name,
                  "track_class": "MovieSceneSkeletalAnimationTrack", "add_section": False}
        try:
            return json.dumps(_exec(_ADD_BINDING_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_visibility_track(ctx, sequence_path: str, binding_name: str) -> str:
        """Add a Visibility track (MovieSceneVisibilityTrack) + an initial bool section to a binding.

        sequence_path: object/package path of the LevelSequence.
        binding_name:  binding display name or internal name to animate visibility on.

        The visibility track is a bool property track pre-wired to the actor's hidden/visibility property;
        binding.add_track(unreal.MovieSceneVisibilityTrack) creates it and this tool adds one
        MovieSceneVisibilitySection (a single MovieSceneScriptingBoolChannel) spanning the playback range,
        ready for bool keys. Verify with sequencer_read.list_sequence_tracks(binding=...).

        Ledgered write op 'add_seq_binding_track' {asset_path, binding_name,
        track_class='MovieSceneVisibilityTrack', prior_exact_count}. Inverse: find binding, if
        find_tracks_by_exact_type(cls) grew, binding.remove_track(last) -- removes the track and its
        section. FAITHFUL."""
        params = {"sequence_path": sequence_path, "binding_name": binding_name,
                  "track_class": "MovieSceneVisibilityTrack", "add_section": True}
        try:
            return json.dumps(_exec(_ADD_BINDING_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # SECTIONS                                                            #
    # ================================================================== #
    @mcp.tool()
    def add_camera_cut_section(ctx, sequence_path: str, camera_binding_name: str,
                               start_frame: int = None, end_frame: int = None,
                               track_index: int = 0) -> str:
        """Add a section to the Camera Cut track that cuts to a given camera binding.

        sequence_path:       object/package path of the LevelSequence.
        camera_binding_name: binding (display or internal name) of the camera to cut to (a CameraActor/
                             CineCameraActor binding -- e.g. from add_spawnable_from_class or
                             add_actor_binding). Required.
        start_frame/end_frame: DISPLAY-rate frames for the cut (default = the sequence playback range).
        track_index:         which camera-cut track to add to (default 0). Add the track first with
                             add_camera_cut_track.

        Uses track.add_section(); the camera is bound via section.set_camera_binding_id(
        unreal.MovieSceneSequenceExtensions.get_binding_id(seq, camera_binding)). Verify with
        sequencer_read.list_sequence_tracks (the master camera-cut track's sections).

        Ledgered write op 'add_seq_track_section' {asset_path, scope='master', binding_name=None,
        track_class='MovieSceneCameraCutTrack', track_index, prior_section_count}. Inverse: load seq,
        track=seq.find_tracks_by_exact_type(cls)[track_index]; secs=track.get_sections(); if
        len(secs) > prior_section_count, track.remove_section(secs[-1]). FAITHFUL."""
        params = {"sequence_path": sequence_path, "scope": "master",
                  "track_class": "MovieSceneCameraCutTrack", "track_index": track_index,
                  "kind": "camera_cut", "camera_binding_name": camera_binding_name,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_SECTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_audio_section(ctx, sequence_path: str, sound_path: str,
                          start_frame: int = None, end_frame: int = None,
                          track_index: int = 0) -> str:
        """Add a section carrying a sound to the Audio track.

        sequence_path: object/package path of the LevelSequence.
        sound_path:    asset path of a SoundBase (SoundWave or SoundCue). Required; validated before write.
        start_frame/end_frame: DISPLAY-rate frames (default = the sequence playback range).
        track_index:   which audio track (default 0). Add the track first with add_audio_track.

        Uses track.add_section() then section.set_sound(<SoundBase>). Verify with
        sequencer_read.list_sequence_tracks (the master audio track's sections).

        Ledgered write op 'add_seq_track_section' {asset_path, scope='master', binding_name=None,
        track_class='MovieSceneAudioTrack', track_index, prior_section_count}. Inverse:
        track=seq.find_tracks_by_exact_type(cls)[track_index]; if get_sections() grew,
        track.remove_section(last). FAITHFUL."""
        params = {"sequence_path": sequence_path, "scope": "master",
                  "track_class": "MovieSceneAudioTrack", "track_index": track_index,
                  "kind": "audio", "sound_path": sound_path,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_SECTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_skeletal_animation_section(ctx, sequence_path: str, binding_name: str,
                                       animation_path: str, start_frame: int = None,
                                       end_frame: int = None, track_index: int = 0) -> str:
        """Add a section playing an AnimSequence to a binding's Skeletal Animation track.

        sequence_path:  object/package path of the LevelSequence.
        binding_name:   binding (display or internal name) that has a skeletal-animation track (call
                        add_skeletal_animation_track first).
        animation_path: asset path of an AnimSequence (validated before write). The section stores it in
                        FMovieSceneSkeletalAnimationParams.animation (no skeleton-compat check at author
                        time -- ensure it matches the bound skeletal mesh).
        start_frame/end_frame: DISPLAY-rate frames (default = the sequence playback range).
        track_index:    which skeletal-anim track on the binding (default 0).

        Uses track.add_section(); the animation is set via section.get_editor_property('params') ->
        params.set_editor_property('animation', anim) -> section.set_editor_property('params', params).
        Verify with sequencer_read.list_sequence_tracks(binding=...).

        Ledgered write op 'add_seq_track_section' {asset_path, scope='binding', binding_name,
        track_class='MovieSceneSkeletalAnimationTrack', track_index, prior_section_count}. Inverse: load
        seq, binding=find_binding(binding_name), track=binding.find_tracks_by_exact_type(cls)[track_index];
        if get_sections() grew, track.remove_section(last). FAITHFUL."""
        params = {"sequence_path": sequence_path, "scope": "binding", "binding_name": binding_name,
                  "track_class": "MovieSceneSkeletalAnimationTrack", "track_index": track_index,
                  "kind": "skeletal", "animation_path": animation_path,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_SECTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # SPAWNABLES                                                          #
    # ================================================================== #
    @mcp.tool()
    def add_spawnable_from_class(ctx, sequence_path: str, class_name: str) -> str:
        """Add a spawnable object binding to a LevelSequence from an actor class.

        sequence_path: object/package path of the LevelSequence.
        class_name:    engine actor class name (e.g. 'CameraActor', 'CineCameraActor',
                       'SkeletalMeshActor', 'PointLight') or a loadable class path.

        A spawnable binding owns a template actor that the sequence instantiates during playback and
        destroys afterward -- it does NOT place a persistent actor in the level. Uses
        seq.add_spawnable_from_class(<ActorClass>) -> MovieSceneBindingProxy. Pass the returned
        binding_name to add_transform_track / add_skeletal_animation_track / add_camera_cut_section, etc.
        Verify with sequencer_read.get_level_sequence_info (spawnable_count / bindings).

        Ledgered write op 'add_actor_binding' {asset_path, binding_name} (REUSES the already-folded
        add_actor_binding inverse). Inverse: load seq, find the binding whose get_name()==binding_name,
        call binding.remove() -- deletes the spawnable binding and its tracks. FAITHFUL."""
        params = {"sequence_path": sequence_path, "mode": "class", "class_name": class_name}
        try:
            return json.dumps(_exec(_ADD_SPAWNABLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_spawnable_from_instance(ctx, sequence_path: str, actor_name: str) -> str:
        """Add a spawnable object binding to a LevelSequence templated from an existing level actor.

        sequence_path: object/package path of the LevelSequence.
        actor_name:    display label (preferred) or internal name of an actor in the active level. The
                       actor is used only as a TEMPLATE (copied into the sequence); the source actor is
                       NOT modified or removed.

        Uses seq.add_spawnable_from_instance(<Actor>) -> MovieSceneBindingProxy (a spawnable that carries
        a copy of the actor's properties). Verify with sequencer_read.get_level_sequence_info.

        Ledgered write op 'add_actor_binding' {asset_path, binding_name} (REUSES the folded inverse).
        Inverse: find the binding whose get_name()==binding_name, binding.remove(). FAITHFUL (source
        actor untouched, so nothing to restore there)."""
        params = {"sequence_path": sequence_path, "mode": "instance", "actor_name": actor_name}
        try:
            return json.dumps(_exec(_ADD_SPAWNABLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # GENERIC TRACK / SECTION ADD + REMOVE                                #
    # ================================================================== #
    @mcp.tool()
    def add_track(ctx, sequence_path: str, track_class: str, binding_name: str = None,
                  add_section: bool = False) -> str:
        """Add a track of ANY MovieScene track class to a LevelSequence (master or a binding).

        sequence_path: object/package path of the LevelSequence.
        track_class:   MovieScene track class name (e.g. 'MovieSceneCameraCutTrack',
                       'MovieSceneAudioTrack', 'MovieSceneEventTrack', 'MovieSceneSkeletalAnimationTrack',
                       'MovieSceneVisibilityTrack', 'MovieScene3DTransformTrack', ...). Use
                       list_track_types to see available classes.
        binding_name:  omit for a master/root track (seq.add_track); else the binding's track
                       (binding.add_track). Display or internal name.
        add_section:   if True, also add an empty section spanning the playback range (for
                       section-bearing tracks).

        Generic complement to the specific adders (add_camera_cut_track/add_audio_track/etc.). Uses
        container.add_track(getattr(unreal, track_class)). Verify with
        sequencer_read.list_sequence_tracks.

        Ledgered write op 'seq_add_track' {asset_path, binding_name|null, track_class, prior_exact_count}.
        Inverse: load seq, resolve container (seq if binding_name null, else the binding);
        tracks=container.find_tracks_by_exact_type(cls); if len(tracks) > prior_exact_count,
        container.remove_track(tracks[-1]). FAITHFUL (removes only the appended track + its sections)."""
        params = {"sequence_path": sequence_path, "track_class": track_class,
                  "binding_name": binding_name, "add_section": add_section}
        try:
            return json.dumps(_exec(_ADD_TRACK_GENERIC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_track(ctx, sequence_path: str, track_index: int, binding_name: str = None) -> str:
        """Remove a track (by index) from a LevelSequence -- master/root or a binding's track.

        sequence_path: object/package path of the LevelSequence.
        track_index:   index into the track list (seq.get_tracks() if binding_name omitted, else
                       binding.get_tracks()).
        binding_name:  omit for master/root tracks; else the named binding.

        Before removal a JSON SNAPSHOT of the track is captured (class + every section's range/row/blend/
        easing, each channel's default/extrapolation and all keys with interp+tangents, and known asset
        refs: audio sound / skeletal animation / timewarp play-rate) so the removal can be rebuilt. Uses
        container.remove_track(track).

        Ledgered write op 'seq_remove_track' {asset_path, binding_name|null, track_index, track_class,
        snapshot}. Inverse: resolve container; tr=container.add_track(getattr(unreal, track_class)); then
        for each section in snapshot -> sec=tr.add_section() and re-apply range/row/blend/easing, channel
        defaults+extrapolation, re-add every key (value + interp/tangents), and re-set asset refs. BEST-
        EFFORT FAITHFUL: fully restores channel-based tracks (transform/visibility/property/float), audio,
        skeletal-animation and timewarp; residual gaps (documented) = camera-cut section binding-id and
        subsequence inner-sequence links are NOT restored (no scripting setter round-trip)."""
        params = {"sequence_path": sequence_path, "track_index": track_index, "binding_name": binding_name}
        try:
            return json.dumps(_exec(_REMOVE_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_section(ctx, sequence_path: str, track_index: int, binding_name: str = None,
                    start_frame: int = None, end_frame: int = None) -> str:
        """Add an empty section (frame range) to a located track.

        sequence_path: object/package path of the LevelSequence.
        track_index:   index into the track list (master if binding_name omitted, else the binding's).
        binding_name:  omit for master/root tracks; else the named binding.
        start_frame/end_frame: DISPLAY-rate frames for the new section (default = the playback range).

        Generic complement to add_camera_cut_section/add_audio_section/etc. Uses track.add_section() +
        set_start_frame/set_end_frame. Verify with sequencer_read.list_sequence_tracks.

        Ledgered write op 'seq_add_section' {asset_path, binding_name|null, track_index,
        prior_section_count}. Inverse: locate the track; secs=track.get_sections(); if
        len(secs) > prior_section_count, track.remove_section(secs[-1]). FAITHFUL."""
        params = {"sequence_path": sequence_path, "track_index": track_index, "binding_name": binding_name,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_SECTION_GENERIC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_section(ctx, sequence_path: str, track_index: int, section_index: int,
                       binding_name: str = None) -> str:
        """Remove a section (by index) from a located track.

        sequence_path: object/package path of the LevelSequence.
        track_index/section_index: locate the section (master if binding_name omitted, else the binding's).
        binding_name:  omit for master/root tracks; else the named binding.

        Before removal a JSON SNAPSHOT of the section is captured (class + range/row/blend/easing, each
        channel's default/extrapolation and all keys with interp+tangents, and known asset refs) so the
        removal can be rebuilt. Uses track.remove_section(section).

        Ledgered write op 'seq_remove_section' {asset_path, binding_name|null, track_index, section_index,
        snapshot}. Inverse: locate the track; sec=track.add_section(); re-apply range/row/blend/easing,
        channel defaults+extrapolation, re-add every key (value + interp/tangents), and re-set asset refs
        (sound/animation/play-rate). BEST-EFFORT FAITHFUL (same residual gaps as remove_track: camera-cut
        binding-id and subsequence links not restored)."""
        params = {"sequence_path": sequence_path, "track_index": track_index,
                  "section_index": section_index, "binding_name": binding_name}
        try:
            return json.dumps(_exec(_REMOVE_SECTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # CAMERA / EVENT / TIMEWARP                                           #
    # ================================================================== #
    @mcp.tool()
    def create_camera(ctx, sequence_path: str, camera_class: str = "CineCameraActor",
                      add_camera_cut: bool = False) -> str:
        """Create a camera bound into a LevelSequence (spawnable) + optionally a camera-cut section.

        sequence_path:  object/package path of the LevelSequence.
        camera_class:   camera actor class (default 'CineCameraActor'; 'CameraActor' also valid).
        add_camera_cut: if True, also add a MovieSceneCameraCutTrack + a section cutting to this camera
                        over the playback range (so the sequence renders through it).

        Composes existing primitives: seq.add_spawnable_from_class(<CameraClass>) creates a spawnable
        camera binding (no persistent level actor); with add_camera_cut it also adds the cut track/section
        wired via section.set_camera_binding_id(get_binding_id(seq, binding)). Verify with
        sequencer_read.get_level_sequence_info + list_sequence_tracks.

        Ledgered write ops (REUSE folded inverses, pushed in creation order for LIFO undo):
        'add_actor_binding' {asset_path, binding_name} -> inverse binding.remove(); and (if add_camera_cut)
        'add_seq_master_track' {asset_path, track_class='MovieSceneCameraCutTrack', prior_exact_count} ->
        inverse remove the appended cut track (+ its section). Undo removes the cut track first, then the
        camera binding. FAITHFUL (no new op type needed)."""
        params = {"sequence_path": sequence_path, "camera_class": camera_class,
                  "add_camera_cut": add_camera_cut}
        try:
            return json.dumps(_exec(_CREATE_CAMERA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_event_section(ctx, sequence_path: str, track_index: int = 0,
                          start_frame: int = None, end_frame: int = None) -> str:
        """Add a SHELL event section to a LevelSequence's Event track (adds the track if none exists).

        sequence_path: object/package path of the LevelSequence.
        track_index:   which MovieSceneEventTrack to add to (default 0); if none exists it is created.
        start_frame/end_frame: DISPLAY-rate frames (default = the playback range).

        Uses track.add_section() on a MovieSceneEventTrack -> a MovieSceneEventTriggerSection. CAP: wiring
        the section to a director-blueprint event ENDPOINT (a K2Node function entry) is editor-only and NOT
        reachable from stock Python, so this ships the section SHELL only -- a functionally-firing event is
        BLOCKED (documented). The section (and, if we created it, the track) is the reversible unit.

        Ledgered write op 'seq_add_event_section' {asset_path, added_track, track_class='MovieSceneEventTrack',
        track_index, prior_section_count}. Inverse: locate the event track; if get_sections() grew,
        track.remove_section(last); then if added_track, seq.remove_track(track). FAITHFUL."""
        params = {"sequence_path": sequence_path, "track_index": track_index,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_EVENT_SECTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_timewarp(ctx, sequence_path: str, play_rate: float = None,
                     start_frame: int = None, end_frame: int = None) -> str:
        """Add a Time Warp track (+ fixed play-rate section) to a LevelSequence (UE 5.8+).

        sequence_path: object/package path of the LevelSequence.
        play_rate:     optional constant play-rate multiplier (e.g. 2.0 = double speed, 0.5 = half). If
                       omitted the section is created at the default identity warp.
        start_frame/end_frame: DISPLAY-rate frames for the section (default = the playback range).

        Probed live: unreal.MovieSceneTimeWarpTrack IS Python-exposed in this 5.8 build (with
        MovieSceneTimeWarpSection + MovieSceneTimeWarpVariant), so this is a real PY tool (not blocked).
        Uses seq.add_track(unreal.MovieSceneTimeWarpTrack); track.add_section(); and, for play_rate, a
        MovieSceneTimeWarpVariant.set_fixed_play_rate(rate) assigned to the section's 'time_warp' editor
        property (read back via to_fixed_play_rate). Verify with sequencer_read.list_sequence_tracks.

        Ledgered write op 'seq_add_timewarp' {asset_path, prior_exact_count}. Inverse: load seq;
        tracks=seq.find_tracks_by_exact_type(unreal.MovieSceneTimeWarpTrack); if len > prior_exact_count,
        seq.remove_track(tracks[-1]) -- removes the track + its rate section. FAITHFUL."""
        params = {"sequence_path": sequence_path, "play_rate": play_rate,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_TIMEWARP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (not shipped -- refused-not-faked, with reasons):
    #   * Event track KEYS wired to a director-blueprint endpoint: MovieSceneEventTrack keys need a
    #     UMovieSceneEventSectionBase + a FMovieSceneEvent bound to a Sequencer Director blueprint
    #     function; that director-BP endpoint authoring is NOT reachable from stock Python (no scripting
    #     surface) -> only the empty event track is shipped (reversible). Keys deferred to a C++ batch.
    #   * Cinematic Shot / Subsequence tracks (MovieSceneCinematicShotTrack / MovieSceneSubTrack): would
    #     nest another LevelSequence; add_section on them needs set_sequence(sub_seq) and pulls in
    #     sub-sequence lifetime concerns -> deferred (reversible remove_track exists, but shipping without
    #     the section wiring would be a half-feature).
    #   * Generic property tracks (MovieSceneFloatTrack/ColorTrack/etc. on an arbitrary UPROPERTY): need
    #     track.set_property_name_and_path(name, path) with a property that resolves on the bound object;
    #     correctness is per-property and easy to get subtly wrong -> visibility (a fixed, known property)
    #     is shipped as the safe representative; arbitrary property tracks deferred.
    # This module registers NO `undo` tool. NEW ledger ops for the coordinator to fold into
    # editor_level.undo: add_seq_master_track, add_seq_binding_track, add_seq_track_section (schemas +
    # inverses in each docstring above and the build report). Spawnables reuse the folded add_actor_binding.
    # ------------------------------------------------------------------ #
