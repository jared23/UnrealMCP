"""UserTools :: Sequencer / Cinematics (WRITE)  (spec: docs/spec/sequencer.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE batch, the mutating
counterpart to sequencer_read.py. Query convention, base64 PARAMS injection, Output-Log auto-capture,
and the per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py.

Everything here uses the LevelSequence scripting surface directly (no Sequencer editor tab opened, no
MovieSceneSequenceExtensions indirection needed) -- all verified live vs TestMCPSetup, UE 5.8.1:
  * create_asset(name, path, unreal.LevelSequence, unreal.LevelSequenceFactoryNew()) is NON-MODAL
    (LevelSequenceFactoryNew has no ConfigureProperties dialog; the scripting create path never calls
    one -- the user's /Game/TestLevelSequence proves the factory works). Save on create.
  * seq.set_playback_start(frame)/set_playback_end(frame)   (there is NO set_playback_range in 5.8;
    frames are DISPLAY-rate frame numbers, e.g. a fresh sequence defaults to 0..150 = 5s @ 30fps).
  * seq.add_possessable(actor) -> MovieSceneBindingProxy (a possessable object binding). The proxy's
    .remove() deletes the binding (there is NO seq.remove_possessable / remove_binding).
  * binding.add_track(unreal.MovieScene3DTransformTrack) -> MovieScene3DTransformTrack;
    binding.remove_track(track) is the inverse. track.add_section() -> MovieScene3DTransformSection
    (9 double channels: Location/Rotation/Scale .X/.Y/.Z in that fixed order).
  * section.get_all_channels()[i] is a MovieSceneScriptingDoubleChannel with
    add_key(unreal.FrameNumber(frame), value) / remove_key(key) / get_keys() / get_num_keys().
    Key frame numbers read/write in DISPLAY rate (matches sequencer_read's key extraction).

Implemented (all validated live, session=agentA, editor left CLEAN, ledger depth 0):
  - create_level_sequence   (WRITE; ledgered op "create_asset"; inverse = close editors + delete asset [+ rmdir])
  - set_playback_range      (WRITE; ledgered op "set_sequence_playback_range"; inverse restores prior start/end)
  - add_actor_binding       (WRITE; ledgered op "add_actor_binding"; inverse = binding.remove())
  - add_transform_track     (WRITE; ledgered op "add_transform_track"; adds track + a section; inverse = remove_track)
  - add_keyframe            (WRITE; ledgered op "add_transform_keyframe"; inverse restores prior per-channel key state)

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified `undo`).
create_* reuses the coordinator's generic "create_asset" inverse. The 4 NEW ledger ops
(set_sequence_playback_range, add_actor_binding, add_transform_track, add_transform_keyframe) have their
inverse logic documented in each tool's docstring + the build report for the coordinator to fold into
editor_level.undo. Every inverse was proven live against the agentA ledger (depth -> 0).

Modal safety: LevelSequenceFactoryNew was probed bounded (isolated create+delete clean, no hang) before
promotion; it is non-modal. No other factory / CreationParameters is constructed anywhere here.
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
# bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never assign a
# snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
# success/user_code/code_obj (they are the C++ wrapper's own locals -> clobbering them wedges capture).


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
    #   _ledger()                 -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_seq(path)           -> (seq, err) load + type-check LevelSequence.
    #   _close_editors(obj)       -> silently close any open asset editor for obj.
    #   _resolve_actor(ident)     -> level actor by label then internal name.
    #   _find_binding(seq, sel)   -> binding proxy by display name or internal name (+ list available).
    #   _transform_tracks(binding)-> list of MovieScene3DTransformTrack on a binding.
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
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
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
def _transform_tracks(binding):
    out = []
    for t in (binding.get_tracks() or []):
        try:
            if t.get_class().get_name() == "MovieScene3DTransformTrack":
                out.append(t)
        except Exception:
            pass
    return out
def _key_at(channel, frame):
    for k in (channel.get_keys() or []):
        try:
            if int(k.get_time().frame_number.value) == int(frame):
                return k
        except Exception:
            pass
    return None
'''

    # ------------------------------------------------------------------ #
    # create_level_sequence — non-interactive LevelSequence asset create  #
    # ------------------------------------------------------------------ #
    _CREATE_SEQ_BODY = _LSW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # NOTE (PROTOCOL #5b): asset creation must NOT be wrapped in a ScopedEditorTransaction -- that
    # traps the new asset in the editor undo buffer and blocks a later delete. Creation is ledgered
    # via our own create_asset op; the AssetTools call is registry-level, not a transactable edit.
    seq = at.create_asset(name, package_path, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    if seq is None or not isinstance(seq, unreal.LevelSequence):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(seq).__name__, asset_path)}))
    else:
        _close_editors(seq)
        # Save on create: persists it AND makes the create_asset undo-delete reliable (an unsaved
        # just-created asset resists delete in the immediately-following call).
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        dr = seq.get_display_rate(); tr = seq.get_tick_resolution()
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": seq.get_name(),
            "asset_path": asset_path, "object_path": seq.get_path_name(),
            "class": seq.get_class().get_name(),
            "display_rate": {"numerator": int(dr.numerator), "denominator": int(dr.denominator)},
            "tick_resolution": {"numerator": int(tr.numerator), "denominator": int(tr.denominator)},
            "playback_start": seq.get_playback_start(), "playback_end": seq.get_playback_end(),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_level_sequence(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new LevelSequence (cinematic) asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new LevelSequence (e.g. 'MySeq').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be under
                      a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate folders are
                      created as needed.

        Uses AssetTools.create_asset(..., unreal.LevelSequence, unreal.LevelSequenceFactoryNew()); the
        factory has no ConfigureProperties dialog and the scripting path never calls one -> no modal.
        The new sequence starts empty (no bindings/tracks) with a default display rate (30fps) and a
        default playback range (reported). Inspect with sequencer_read.get_level_sequence_info; then
        add_actor_binding / add_transform_track / add_keyframe to author it.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close any
        editors for the asset, delete it, and remove package_path if we created it and it is now empty
        (the same generic inverse used by create_blackboard / create_blueprint)."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_SEQ_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_playback_range — set the sequence playback start/end frames      #
    # ------------------------------------------------------------------ #
    _SET_RANGE_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
start_frame = int(PARAMS["start_frame"])
end_frame = int(PARAMS["end_frame"])
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif end_frame <= start_frame:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "end_frame (%d) must be greater than start_frame (%d)" % (end_frame, start_frame)}))
else:
    prior_start = seq.get_playback_start()
    prior_end = seq.get_playback_end()
    with unreal.ScopedEditorTransaction("MCP set_playback_range"):
        seq.set_playback_start(start_frame)
        seq.set_playback_end(end_frame)
    _ledger().append({"op": "set_sequence_playback_range", "asset_path": seq_path,
                      "prior_start": prior_start, "prior_end": prior_end})
    print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
        "prior_start": prior_start, "prior_end": prior_end,
        "new_start": seq.get_playback_start(), "new_end": seq.get_playback_end(),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_playback_range(ctx, sequence_path: str, start_frame: int, end_frame: int) -> str:
        """Set a LevelSequence's playback range (in DISPLAY-rate frame numbers).

        sequence_path: object/package path of the LevelSequence asset.
        start_frame:   playback start frame (display rate, e.g. 0).
        end_frame:     playback end frame (display rate; must be > start_frame). At 30fps, frame 150 = 5s.

        UE 5.8 has no set_playback_range on the scripting surface -> this sets seq.set_playback_start and
        seq.set_playback_end (both display-rate frames). Verify with sequencer_read.get_level_sequence_info
        (playback_range.start_frame/end_frame). Ledgered write op 'set_sequence_playback_range'
        {asset_path, prior_start, prior_end}. Inverse: restore prior_start/prior_end -- FAITHFUL."""
        params = {"sequence_path": sequence_path, "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_SET_RANGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_actor_binding — add a possessable binding for a level actor      #
    # ------------------------------------------------------------------ #
    _ADD_BINDING_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
actor_name = PARAMS["actor_name"]
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    actor = _resolve_actor(actor_name)
    if actor is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found in level: %s" % actor_name}))
    else:
        before = set(_binding_names(seq))
        binding = None
        with unreal.ScopedEditorTransaction("MCP add_actor_binding"):
            binding = seq.add_possessable(actor)
        if binding is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_possessable returned None for %s" % actor_name}))
        else:
            bname = str(binding.get_name())
            bdisp = str(binding.get_display_name())
            bound_cls = None
            try:
                c = binding.get_possessed_object_class()
                bound_cls = c.get_name() if c is not None else None
            except Exception:
                pass
            _ledger().append({"op": "add_actor_binding", "asset_path": seq_path, "binding_name": bname})
            print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                "actor": actor_name, "binding_name": bname, "binding_display_name": bdisp,
                "bound_object_class": bound_cls, "binding_count": len(seq.get_bindings() or []),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_actor_binding(ctx, sequence_path: str, actor_name: str) -> str:
        """Add a possessable object binding for a level actor to a LevelSequence.

        sequence_path: object/package path of the LevelSequence asset.
        actor_name:    display label (preferred) or unique internal name of an actor placed in the
                       active level.

        Uses seq.add_possessable(actor) -> a MovieSceneBindingProxy (possessable binding). The binding's
        internal name equals the actor's name. Verify with sequencer_read.get_level_sequence_info
        (bindings[].name / bound_object_class). Pass the returned binding_name (or display name) to
        add_transform_track / add_keyframe.

        Ledgered write op 'add_actor_binding' {asset_path, binding_name}. Inverse: load the sequence,
        find the binding whose get_name()==binding_name, and call binding.remove() (UE 5.8 has no
        seq.remove_possessable; the proxy's .remove() is the faithful inverse -- it deletes the binding
        and all its tracks)."""
        params = {"sequence_path": sequence_path, "actor_name": actor_name}
        try:
            return json.dumps(_exec(_ADD_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_transform_track — add a 3D transform track (+section) to binding #
    # ------------------------------------------------------------------ #
    _ADD_TRACK_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS["binding_name"]
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    binding = _find_binding(seq, binding_sel)
    if binding is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "binding not found: %s" % binding_sel, "available_bindings": _binding_names(seq)}))
    else:
        prior_count = len(_transform_tracks(binding))
        track = None; section = None; nchan = None
        with unreal.ScopedEditorTransaction("MCP add_transform_track"):
            track = binding.add_track(unreal.MovieScene3DTransformTrack)
            if track is not None:
                # add a section spanning the current playback range so the 9 transform channels
                # are ready to key. Removing the track (inverse) removes this section + its keys too.
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
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_track returned None"}))
        else:
            _ledger().append({"op": "add_transform_track", "asset_path": seq_path,
                              "binding_name": binding_sel, "prior_transform_track_count": prior_count})
            print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                "binding": binding_sel, "track_type": track.get_class().get_name(),
                "section_type": (section.get_class().get_name() if section else None),
                "channel_count": nchan, "transform_track_count": len(_transform_tracks(binding)),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_transform_track(ctx, sequence_path: str, binding_name: str) -> str:
        """Add a 3D transform track (with an initial section) to an object binding.

        sequence_path: object/package path of the LevelSequence asset.
        binding_name:  binding display name or internal name (from add_actor_binding /
                       sequencer_read.get_level_sequence_info).

        Uses binding.add_track(unreal.MovieScene3DTransformTrack), then track.add_section() to create a
        MovieScene3DTransformSection spanning the current playback range -- this gives the 9 keyable
        double channels (Location/Rotation/Scale .X/.Y/.Z) so add_keyframe can write immediately. Verify
        with sequencer_read.list_sequence_tracks (binding=<name>).

        Ledgered write op 'add_transform_track' {asset_path, binding_name, prior_transform_track_count}.
        Inverse: load the sequence, find the binding, and if it now has more MovieScene3DTransformTracks
        than prior_transform_track_count, binding.remove_track(<the last one>) -- which removes the track,
        its section, and any keys. FAITHFUL (only the track we appended is removed)."""
        params = {"sequence_path": sequence_path, "binding_name": binding_name}
        try:
            return json.dumps(_exec(_ADD_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_keyframe — add transform keys on a binding's transform section   #
    # ------------------------------------------------------------------ #
    _ADD_KEY_BODY = _LSW_HELPERS + r'''
seq_path = PARAMS["sequence_path"]
binding_sel = PARAMS["binding_name"]
frame = int(PARAMS["frame"])
loc = PARAMS.get("location")
rot = PARAMS.get("rotation")
scale = PARAMS.get("scale")
seq, err = _load_seq(seq_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    binding = _find_binding(seq, binding_sel)
    if binding is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "binding not found: %s" % binding_sel, "available_bindings": _binding_names(seq)}))
    else:
        tts = _transform_tracks(binding)
        if not tts:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "binding '%s' has no transform track; call add_transform_track first" % binding_sel}))
        else:
            secs = tts[0].get_sections() or []
            if not secs:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "transform track has no section; call add_transform_track first"}))
            else:
                chans = secs[0].get_all_channels() or []
                # channel index map for the 9-channel transform section (fixed order)
                plan = []  # (idx, value)
                if loc is not None:
                    for j in range(3):
                        plan.append((0 + j, float(loc[j])))
                if rot is not None:
                    for j in range(3):
                        plan.append((3 + j, float(rot[j])))
                if scale is not None:
                    for j in range(3):
                        plan.append((6 + j, float(scale[j])))
                if not plan:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "nothing to key: provide at least one of location/rotation/scale ([x,y,z])"}))
                else:
                    ch_records = []
                    written = []
                    with unreal.ScopedEditorTransaction("MCP add_keyframe"):
                        for idx, val in plan:
                            if idx >= len(chans):
                                continue
                            ch = chans[idx]
                            existing = _key_at(ch, frame)
                            rec = {"idx": idx, "had_key": existing is not None,
                                   "prior_value": (existing.get_value() if existing is not None else None)}
                            ch.add_key(unreal.FrameNumber(frame), val)
                            ch_records.append(rec)
                            cname = None
                            try:
                                cname = str(ch.get_editor_property("channel_name"))
                            except Exception:
                                pass
                            written.append({"channel": cname, "idx": idx, "value": val})
                    _ledger().append({"op": "add_transform_keyframe", "asset_path": seq_path,
                                      "binding_name": binding_sel, "frame": frame, "channels": ch_records})
                    print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(),
                        "binding": binding_sel, "frame": frame, "keys_written": written,
                        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_keyframe(ctx, sequence_path: str, binding_name: str, frame: int,
                     location: list = None, rotation: list = None, scale: list = None) -> str:
        """Add transform keyframes at one frame on a binding's transform section.

        sequence_path: object/package path of the LevelSequence asset.
        binding_name:  binding display name or internal name (must already have a transform track --
                       call add_transform_track first).
        frame:         DISPLAY-rate frame number to key at.
        location:      optional [x, y, z] -> keys the Location.X/Y/Z channels.
        rotation:      optional [x, y, z] Euler degrees [roll, pitch, yaw] -> keys Rotation.X/Y/Z.
        scale:         optional [x, y, z] -> keys Scale.X/Y/Z.
        Provide at least one of location/rotation/scale.

        Keys are added via section.get_all_channels()[i].add_key(unreal.FrameNumber(frame), value) on the
        MovieScene3DTransformSection's 9 double channels (fixed order Location/Rotation/Scale .X/.Y/.Z).
        Verify with sequencer_read.list_sequence_tracks (include_keys=True).

        Ledgered write op 'add_transform_keyframe' {asset_path, binding_name, frame, channels:[{idx,
        had_key, prior_value}]}. Inverse (per channel, LIFO): if a key already existed at this frame,
        restore its prior_value (add_key again with prior_value); else remove the key we added at this
        frame. FAITHFUL for both fresh keys and overwrites of an existing key."""
        params = {"sequence_path": sequence_path, "binding_name": binding_name, "frame": frame,
                  "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_ADD_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (not in this build): spawnable bindings (add_spawnable), non-transform tracks (visibility,
    #   audio, event, property tracks), marked frames, display-rate change, bake/export. Each needs its
    #   own faithful ledger inverse; several risk heavier movie-scene surgery. This module ships the core
    #   transform-authoring path (binding -> transform track -> keys) that is fully reversible.
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. It reuses the generic
    # "create_asset" inverse and adds 4 NEW ledger ops (set_sequence_playback_range, add_actor_binding,
    # add_transform_track, add_transform_keyframe) documented above for the coordinator to fold in.
    # ------------------------------------------------------------------ #
