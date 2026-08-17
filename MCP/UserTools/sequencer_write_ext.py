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
