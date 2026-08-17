"""UserTools :: Sequencer / Cinematics -- BINDINGS / PLAYBACK / STRUCTURE  (spec: docs/spec/sequencer.md)

Clean-room reimplementation over Unreal's public MovieSceneScripting Python API (UE 5.8.1). This is the
G5 layer that fills the remaining Tier-1 MISSING items from docs/parity/p2_anim_sequencer.md, complementing
the existing sequencer modules (do NOT duplicate their tools):
  * sequencer_read.py         -- read/introspection (folds list_bindings/list_sections/list_channels/list_keys/list_marked_frames)
  * level_sequence_write.py   -- create sequence, playback range, add_actor_binding (== add_possessable), transform track/keys
  * sequencer_write_ext.py    -- master/binding tracks, sections, spawnables
  * sequencer_edit.py (G1-A)  -- per-key / per-channel / per-section fine editing

Query convention, base64 PARAMS injection, Output-Log auto-capture, and the per-session undo ledger are
copied VERBATIM from the gold-standard editor_level.py. Every mutation runs inside an
unreal.ScopedEditorTransaction AND pushes a faithful inverse op onto the per-session ledger. This module
registers NO `undo` tool (editor_level.py owns the single unified undo); the NEW op schemas are documented
per-tool and handed to the coordinator (.mcp_coord/coordinator-inbox/g5_seq_undo_ops.md).

IMPLEMENTED (validated live on /Game/_scratch_g5, editor left CLEAN, ledger depth 0):
  Bindings:      rename_binding (op seq_rename_binding)
  Playback/disp: open_level_sequence (read/ui) . set_display_rate (seq_set_display_rate) .
                 set_tick_resolution (seq_set_tick_resolution) . set_evaluation_type (seq_set_evaluation_type) .
                 set_playhead (seq_set_playhead, editor-transient) . get_playhead (read) .
                 set_playback_state (seq_set_playback_state, editor-transient)
  Tracks:        add_property_track (seq_add_property_track, auto-detect) . list_track_types (read) .
                 list_animatable_properties (read)
  Specialized:   add_subsequence (seq_add_subsequence) . possess_component (REUSES op add_actor_binding) .
                 add_marked_frame (seq_add_marked_frame) . list_marked_frames (read) .
                 remove_marked_frame (seq_remove_marked_frame) . add_folder (seq_add_folder) .
                 add_to_folder (seq_add_to_folder)

DEFERRED (refused-not-faked -- see footer for the concrete UE 5.8 reason; these tools do NO editor I/O and
NO mutation, they return a structured {"status":"blocked","mutated":false,...}):
  remove_binding . convert_to_spawnable . convert_to_possessable . tag_binding . untag_binding

MovieSceneScripting gotchas discovered (UE 5.8.1):
  * Display rate / tick resolution / evaluation type live on the LevelSequence itself (seq.get/set_display_rate,
    seq.get/set_tick_resolution, seq.get/set_evaluation_type via unreal.FrameRate / unreal.MovieSceneEvaluationType) --
    NOT on seq.get_movie_scene() (the UMovieScene object exposes none of these to Python).
  * Marked frames also live on the sequence: seq.add_marked_frame(unreal.MovieSceneMarkedFrame),
    seq.get_marked_frames(), seq.delete_marked_frame(index). A MovieSceneMarkedFrame's frame is a FrameNumber
    struct (frame_number.value). The list is kept sorted by frame, so an add index is not stable.
  * Sequencer folders use the *_root_folder_to_sequence verbs (seq.add_root_folder_to_sequence(name),
    seq.get_root_folders_in_sequence(), seq.remove_root_folder_from_sequence(folder)) -- NOT add_folder /
    get_folders (those names do not exist), and NOT on UMovieScene. Folder children:
    folder.add_child_object_binding(binding_proxy) / remove_child_object_binding(binding_proxy) and
    folder.add_child_track(track) / remove_child_track(track).
  * binding.get_name() (internal FName) is STABLE across binding.set_display_name(...) -- so rename_binding's
    inverse locates the binding by its stable internal name and restores the prior display name. FAITHFUL.
  * Playhead / play state go through unreal.LevelSequenceEditorBlueprintLibrary (open_level_sequence,
    get/set_current_time, play/pause/is_playing, close_level_sequence) and require the sequence to be OPEN in
    the Sequencer editor; they are TRANSIENT editor state (not saved into the asset).
  * There is NO scripting path to convert possessable<->spawnable (FSequencer::ConvertToSpawnableInternal is
    editor-controller-only; absent from MovieSceneSequenceExtensions / SequencerTools /
    LevelSequenceEditorBlueprintLibrary). And AddBindingTag is not exposed to Python (only remove_binding_tag /
    find_binding_by_tag / find_bindings_by_tag / get_all_binding_tags are ScriptCallable).
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
# success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers. No triple-single-quote / no backslash.  #
    # ------------------------------------------------------------------ #
    _H = r'''
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
def _b_disp(b):
    try: return str(b.get_display_name())
    except Exception: return None
def _b_name(b):
    try: return str(b.get_name())
    except Exception: return None
def _binding_names(seq):
    out = []
    for b in (seq.get_bindings() or []):
        out.append(_b_disp(b) or _b_name(b))
    return out
def _find_binding(seq, sel):
    if sel in (None, ""):
        return None
    for b in (seq.get_bindings() or []):
        if sel in (_b_disp(b), _b_name(b)):
            return b
    return None
def _save(path):
    try: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception: pass
def _find_folder(seq, name):
    for f in (seq.get_root_folders_in_sequence() or []):
        try:
            if str(f.get_folder_name()) == str(name):
                return f
        except Exception:
            pass
    return None
def _resolve_bound_object(seq, binding):
    # best-effort: find the level actor whose label/name matches the binding internal/display name.
    tgt = _b_name(binding); disp = _b_disp(binding)
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in (eas.get_all_level_actors() or []):
            if a.get_name() in (tgt, disp) or a.get_actor_label() in (tgt, disp):
                return a
    except Exception:
        pass
    return None
# property-name -> MovieScene track class (auto-detect maps a python value type to a track class)
_PROP_TRACKS = {
    "float": "MovieSceneFloatTrack", "double": "MovieSceneDoubleTrack",
    "bool": "MovieSceneBoolTrack", "visibility": "MovieSceneVisibilityTrack",
    "int": "MovieSceneIntegerTrack", "integer": "MovieSceneIntegerTrack",
    "byte": "MovieSceneByteTrack", "enum": "MovieSceneEnumTrack",
    "string": "MovieSceneStringTrack", "color": "MovieSceneColorTrack",
    "vector": "MovieSceneDoubleVectorTrack", "transform": "MovieScene3DTransformTrack",
}
def _track_cls(name):
    cn = _PROP_TRACKS.get(str(name).strip().lower())
    return getattr(unreal, cn, None) if cn else None
def _auto_track_key(val, prop_name):
    pn = str(prop_name).lower()
    if isinstance(val, bool):
        return "visibility" if ("hidden" in pn or "visib" in pn) else "bool"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "float"
    if isinstance(val, (unreal.Color, unreal.LinearColor)):
        return "color"
    if isinstance(val, unreal.Vector):
        return "vector"
    if isinstance(val, unreal.EnumBase):
        return "enum"
    if isinstance(val, (str, unreal.Name, unreal.Text)):
        return "string"
    return None
def _eval_enum(name):
    s = str(name).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if s in ("framelocked", "locked"):
        return unreal.MovieSceneEvaluationType.FRAME_LOCKED, "FRAME_LOCKED"
    if s in ("withsubframes", "subframes", "withsubframe"):
        return unreal.MovieSceneEvaluationType.WITH_SUB_FRAMES, "WITH_SUB_FRAMES"
    return None, None
def _eval_name(v):
    s = str(v)
    if "." in s:
        s = s.split(".")[-1]
    if ":" in s:
        s = s.split(":")[0]
    return s.strip()
'''

    # ================================================================== #
    # BINDINGS                                                           #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # rename_binding                                                     #
    # ------------------------------------------------------------------ #
    _RENAME_BINDING_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
b = _find_binding(seq, PARAMS["binding"])
if b is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding not found: %s" % PARAMS["binding"],
        "available_bindings": _binding_names(seq)})); raise SystemExit
internal = _b_name(b)
prior = _b_disp(b)
new_name = str(PARAMS["new_name"])
with unreal.ScopedEditorTransaction("MCP seq_rename_binding"):
    b.set_display_name(new_name)
_save(PARAMS["sequence_path"])
_ledger().append({"op": "seq_rename_binding", "asset_path": PARAMS["sequence_path"],
                  "binding_name": internal, "prior_display_name": prior, "new_display_name": new_name})
print("@@UMCP@@" + json.dumps({"status": "success", "binding_internal_name": internal,
    "prior_display_name": prior, "new_display_name": _b_disp(b), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_binding(ctx, sequence_path: str, binding: str, new_name: str) -> str:
        """Rename a LevelSequence object binding's DISPLAY name.

        sequence_path: object/package path of the LevelSequence.
        binding:       current display name or internal name of the binding.
        new_name:      new display name.

        Uses binding.set_display_name(new_name). The binding's internal FName (get_name()) is unchanged --
        only the sequencer-visible display label changes. Ledgered op 'seq_rename_binding'
        {asset_path, binding_name(internal), prior_display_name, new_display_name}. Inverse: locate the
        binding by its stable internal name, set_display_name(prior_display_name). FAITHFUL."""
        params = {"sequence_path": sequence_path, "binding": binding, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # possess_component  (REUSES op add_actor_binding for undo)          #
    # ------------------------------------------------------------------ #
    _POSSESS_COMPONENT_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = None
an = PARAMS["actor_name"]
for a in (eas.get_all_level_actors() or []):
    if a.get_actor_label() == an or a.get_name() == an:
        actor = a; break
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found in level: %s" % an})); raise SystemExit
comp = None
cn = PARAMS["component_name"]
for c in (actor.get_components_by_class(unreal.ActorComponent) or []):
    if c.get_name() == cn:
        comp = c; break
if comp is None:
    names = [c.get_name() for c in (actor.get_components_by_class(unreal.ActorComponent) or [])]
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % cn,
        "available_components": names})); raise SystemExit
binding = None
with unreal.ScopedEditorTransaction("MCP seq_possess_component"):
    binding = seq.add_possessable(comp)
if binding is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_possessable returned None for component %s" % cn})); raise SystemExit
_save(PARAMS["sequence_path"])
bname = _b_name(binding)
_ledger().append({"op": "add_actor_binding", "asset_path": PARAMS["sequence_path"], "binding_name": bname})
bound_cls = None
try:
    c = binding.get_possessed_object_class(); bound_cls = c.get_name() if c else None
except Exception:
    pass
print("@@UMCP@@" + json.dumps({"status": "success", "actor": an, "component": cn,
    "binding_name": bname, "binding_display_name": _b_disp(binding), "bound_object_class": bound_cls,
    "binding_count": len(seq.get_bindings() or []), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def possess_component(ctx, sequence_path: str, actor_name: str, component_name: str) -> str:
        """Add a possessable binding for a COMPONENT of a level actor.

        sequence_path:  object/package path of the LevelSequence.
        actor_name:     display label or internal name of the actor that owns the component.
        component_name: internal name of the component (e.g. 'StaticMeshComponent0'). Use
                        get_actor_properties / describe_object to list an actor's components.

        Uses seq.add_possessable(<component>) -> a MovieSceneBindingProxy possessable bound to the component
        (so you can key component-level properties). This mutates ONLY the sequence asset (no level change).
        Ledgered op 'add_actor_binding' {asset_path, binding_name} (REUSES the existing add-binding inverse in
        editor_level.undo -- find the binding by internal name and binding.remove()). FAITHFUL."""
        params = {"sequence_path": sequence_path, "actor_name": actor_name, "component_name": component_name}
        try:
            return json.dumps(_exec(_POSSESS_COMPONENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # PLAYBACK / DISPLAY                                                 #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # open_level_sequence  (editor/ui, no ledger)                        #
    # ------------------------------------------------------------------ #
    _OPEN_SEQ_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
L = unreal.LevelSequenceEditorBlueprintLibrary
ok = False
try:
    ok = bool(L.open_level_sequence(seq))
except Exception as e:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "open failed: %s" % e})); raise SystemExit
cur = L.get_current_level_sequence()
print("@@UMCP@@" + json.dumps({"status": "success", "opened": ok,
    "current_sequence": (cur.get_name() if cur else None),
    "note": "Sequencer editor tab opened (transient UI; not a content mutation, no ledger entry)."}))
'''

    @mcp.tool()
    def open_level_sequence(ctx, sequence_path: str) -> str:
        """Open a LevelSequence in the Sequencer editor (required before set_playhead / set_playback_state).

        sequence_path: object/package path of the LevelSequence.

        Uses unreal.LevelSequenceEditorBlueprintLibrary.open_level_sequence(seq). This is a transient editor
        UI action (opens the Sequencer tab) -- NOT a content mutation, so it pushes NO ledger entry. Note an
        open asset-editor tab can block a later delete of the asset; close it with the Sequencer UI (or it is
        closed automatically when the level sequence is deleted)."""
        params = {"sequence_path": sequence_path}
        try:
            return json.dumps(_exec(_OPEN_SEQ_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_display_rate                                                   #
    # ------------------------------------------------------------------ #
    _SET_DISPLAY_RATE_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
num = PARAMS.get("numerator"); den = PARAMS.get("denominator"); fps = PARAMS.get("fps")
if num is None or den is None:
    if fps is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide fps or numerator+denominator"})); raise SystemExit
    f = float(fps)
    if abs(f - round(f)) < 1e-6:
        num = int(round(f)); den = 1
    else:
        num = int(round(f * 1000.0)); den = 1000
num = int(num); den = int(den)
if den <= 0 or num <= 0:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "numerator/denominator must be positive"})); raise SystemExit
pr = seq.get_display_rate()
prior_num = pr.numerator; prior_den = pr.denominator
with unreal.ScopedEditorTransaction("MCP seq_set_display_rate"):
    seq.set_display_rate(unreal.FrameRate(num, den))
_save(PARAMS["sequence_path"])
nr = seq.get_display_rate()
_ledger().append({"op": "seq_set_display_rate", "asset_path": PARAMS["sequence_path"],
                  "prior_num": prior_num, "prior_den": prior_den})
print("@@UMCP@@" + json.dumps({"status": "success", "prior": "%d/%d" % (prior_num, prior_den),
    "new": "%d/%d" % (nr.numerator, nr.denominator), "fps": float(nr.numerator) / float(nr.denominator),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_display_rate(ctx, sequence_path: str, fps: float = None,
                         numerator: int = None, denominator: int = None) -> str:
        """Set a LevelSequence's DISPLAY rate (the frame rate its frame numbers are expressed in).

        sequence_path: object/package path of the LevelSequence.
        fps:           frame rate as a number (e.g. 24, 29.97). Integer -> numerator/1; fractional ->
                       numerator/1000 approximation.
        numerator/denominator: exact FrameRate override (e.g. 24000/1001 for 23.976). Takes precedence over fps.

        Uses seq.set_display_rate(unreal.FrameRate(numerator, denominator)). Ledgered op 'seq_set_display_rate'
        {asset_path, prior_num, prior_den}. Inverse: seq.set_display_rate(FrameRate(prior_num, prior_den)). FAITHFUL."""
        params = {"sequence_path": sequence_path, "fps": fps, "numerator": numerator, "denominator": denominator}
        try:
            return json.dumps(_exec(_SET_DISPLAY_RATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_tick_resolution                                                #
    # ------------------------------------------------------------------ #
    _SET_TICK_RES_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
num = PARAMS.get("numerator"); den = PARAMS.get("denominator"); fps = PARAMS.get("fps")
if num is None or den is None:
    if fps is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide fps or numerator+denominator"})); raise SystemExit
    f = float(fps)
    if abs(f - round(f)) < 1e-6:
        num = int(round(f)); den = 1
    else:
        num = int(round(f * 1000.0)); den = 1000
num = int(num); den = int(den)
if den <= 0 or num <= 0:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "numerator/denominator must be positive"})); raise SystemExit
pr = seq.get_tick_resolution()
prior_num = pr.numerator; prior_den = pr.denominator
with unreal.ScopedEditorTransaction("MCP seq_set_tick_resolution"):
    seq.set_tick_resolution(unreal.FrameRate(num, den))
_save(PARAMS["sequence_path"])
nr = seq.get_tick_resolution()
_ledger().append({"op": "seq_set_tick_resolution", "asset_path": PARAMS["sequence_path"],
                  "prior_num": prior_num, "prior_den": prior_den})
print("@@UMCP@@" + json.dumps({"status": "success", "prior": "%d/%d" % (prior_num, prior_den),
    "new": "%d/%d" % (nr.numerator, nr.denominator), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_tick_resolution(ctx, sequence_path: str, fps: float = None,
                            numerator: int = None, denominator: int = None) -> str:
        """Set a LevelSequence's TICK RESOLUTION (the underlying high-res frame grid, e.g. 24000/1).

        sequence_path: object/package path of the LevelSequence.
        fps/numerator/denominator: same convention as set_display_rate (numerator/denominator wins).

        Uses seq.set_tick_resolution(unreal.FrameRate(...)). Tick resolution is the fine internal timebase;
        changing it can rescale internal key times, but restoring the prior FrameRate reverses it. Ledgered op
        'seq_set_tick_resolution' {asset_path, prior_num, prior_den}. Inverse: restore prior FrameRate. FAITHFUL."""
        params = {"sequence_path": sequence_path, "fps": fps, "numerator": numerator, "denominator": denominator}
        try:
            return json.dumps(_exec(_SET_TICK_RES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_evaluation_type                                                #
    # ------------------------------------------------------------------ #
    _SET_EVAL_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ev, evname = _eval_enum(PARAMS["evaluation_type"])
if ev is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "evaluation_type must be 'framelocked' or 'withsubframes' (got %s)" % PARAMS["evaluation_type"]})); raise SystemExit
prior = seq.get_evaluation_type()
prior_name = _eval_name(prior)
with unreal.ScopedEditorTransaction("MCP seq_set_evaluation_type"):
    seq.set_evaluation_type(ev)
_save(PARAMS["sequence_path"])
_ledger().append({"op": "seq_set_evaluation_type", "asset_path": PARAMS["sequence_path"], "prior_type": prior_name})
print("@@UMCP@@" + json.dumps({"status": "success", "prior": prior_name, "new": _eval_name(seq.get_evaluation_type()),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_evaluation_type(ctx, sequence_path: str, evaluation_type: str) -> str:
        """Set a LevelSequence's evaluation type: frame-locked or with-sub-frames.

        sequence_path:   object/package path of the LevelSequence.
        evaluation_type: 'framelocked' (unreal.MovieSceneEvaluationType.FRAME_LOCKED) or 'withsubframes'
                         (WITH_SUB_FRAMES). Accepts framelocked/frame_locked/locked and withsubframes/subframes.

        Uses seq.set_evaluation_type(unreal.MovieSceneEvaluationType.*). Ledgered op 'seq_set_evaluation_type'
        {asset_path, prior_type}. Inverse: seq.set_evaluation_type(getattr(MovieSceneEvaluationType, prior_type)). FAITHFUL."""
        params = {"sequence_path": sequence_path, "evaluation_type": evaluation_type}
        try:
            return json.dumps(_exec(_SET_EVAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_playhead  (editor-transient)                                   #
    # ------------------------------------------------------------------ #
    _SET_PLAYHEAD_BODY = _H + r'''
L = unreal.LevelSequenceEditorBlueprintLibrary
cur = L.get_current_level_sequence()
if cur is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no LevelSequence open in Sequencer -- call open_level_sequence first"})); raise SystemExit
frame = int(PARAMS["frame"])
prior = int(L.get_current_time())
L.set_current_time(frame)
_ledger().append({"op": "seq_set_playhead", "prior_frame": prior})
print("@@UMCP@@" + json.dumps({"status": "success", "current_sequence": cur.get_name(),
    "prior_frame": prior, "new_frame": int(L.get_current_time()),
    "note": "transient editor playhead (not saved into the asset)", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_playhead(ctx, sequence_path: str = None, frame: int = 0) -> str:
        """Move the Sequencer editor playhead to a frame (the currently open LevelSequence).

        sequence_path: informational only (the playhead applies to whichever sequence is open).
        frame:         target frame (display-rate frame number).

        Requires a LevelSequence open in the Sequencer (call open_level_sequence first). Uses
        unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(frame). This is TRANSIENT editor state
        (not saved into the asset). Ledgered op 'seq_set_playhead' {prior_frame}. Inverse: if a sequence is
        still open, set_current_time(prior_frame). FAITHFUL while the Sequencer stays open."""
        params = {"sequence_path": sequence_path, "frame": frame}
        try:
            return json.dumps(_exec(_SET_PLAYHEAD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_playhead  (read-only)                                          #
    # ------------------------------------------------------------------ #
    _GET_PLAYHEAD_BODY = _H + r'''
L = unreal.LevelSequenceEditorBlueprintLibrary
cur = L.get_current_level_sequence()
if cur is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no LevelSequence open in Sequencer -- call open_level_sequence first"})); raise SystemExit
lt = None
try: lt = int(L.get_current_local_time())
except Exception: pass
print("@@UMCP@@" + json.dumps({"status": "success", "current_sequence": cur.get_name(),
    "frame": int(L.get_current_time()), "local_frame": lt, "is_playing": bool(L.is_playing())}))
'''

    @mcp.tool()
    def get_playhead(ctx, sequence_path: str = None) -> str:
        """Read the Sequencer editor playhead (frame + play state) of the currently open LevelSequence.

        sequence_path: informational only. Requires a sequence open in the Sequencer.

        Uses unreal.LevelSequenceEditorBlueprintLibrary.get_current_time / get_current_local_time / is_playing.
        Read-only (no ledger)."""
        params = {"sequence_path": sequence_path}
        try:
            return json.dumps(_exec(_GET_PLAYHEAD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_playback_state  (editor-transient)                             #
    # ------------------------------------------------------------------ #
    _SET_PLAYSTATE_BODY = _H + r'''
L = unreal.LevelSequenceEditorBlueprintLibrary
cur = L.get_current_level_sequence()
if cur is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no LevelSequence open in Sequencer -- call open_level_sequence first"})); raise SystemExit
state = str(PARAMS["state"]).strip().lower()
if state not in ("play", "pause"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "state must be 'play' or 'pause'"})); raise SystemExit
prior_playing = bool(L.is_playing())
if state == "play":
    L.play()
else:
    L.pause()
_ledger().append({"op": "seq_set_playback_state", "prior_playing": prior_playing})
print("@@UMCP@@" + json.dumps({"status": "success", "current_sequence": cur.get_name(), "state": state,
    "prior_playing": prior_playing, "is_playing": bool(L.is_playing()),
    "note": "transient editor playback state", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_playback_state(ctx, sequence_path: str = None, state: str = "pause") -> str:
        """Play or pause the currently open LevelSequence in the Sequencer editor preview.

        sequence_path: informational only.
        state:         'play' or 'pause'.

        Requires a sequence open in the Sequencer. Uses unreal.LevelSequenceEditorBlueprintLibrary.play()/pause().
        TRANSIENT editor state. Ledgered op 'seq_set_playback_state' {prior_playing}. Inverse: if prior_playing
        play() else pause()."""
        params = {"sequence_path": sequence_path, "state": state}
        try:
            return json.dumps(_exec(_SET_PLAYSTATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # TRACKS                                                             #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # add_property_track  (auto-detect)                                  #
    # ------------------------------------------------------------------ #
    _ADD_PROP_TRACK_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
b = _find_binding(seq, PARAMS["binding"])
if b is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding not found: %s" % PARAMS["binding"],
        "available_bindings": _binding_names(seq)})); raise SystemExit
prop_name = str(PARAMS["property_name"])
prop_path = str(PARAMS.get("property_path") or prop_name)
type_key = PARAMS.get("property_type")
detected = None
if not type_key:
    obj = _resolve_bound_object(seq, b)
    if obj is not None:
        val = None; ok = False
        try:
            val = obj.get_editor_property(prop_name); ok = True
        except Exception:
            try:
                rc = obj.get_editor_property("root_component") if hasattr(obj, "get_editor_property") else None
                if rc is not None:
                    val = rc.get_editor_property(prop_name); ok = True
            except Exception:
                ok = False
        if ok:
            type_key = _auto_track_key(val, prop_name)
            detected = type_key
    if not type_key:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not auto-detect property type for '%s'; pass property_type explicitly" % prop_name,
            "supported_types": sorted(set(_PROP_TRACKS.keys()))})); raise SystemExit
cls = _track_cls(type_key)
cls_name = _PROP_TRACKS.get(str(type_key).strip().lower())
if cls is None or cls_name is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown property_type '%s'" % type_key,
        "supported_types": sorted(set(_PROP_TRACKS.keys()))})); raise SystemExit
prior_count = len([t for t in (b.get_tracks() or []) if t.get_class().get_name() == cls_name])
track = None
with unreal.ScopedEditorTransaction("MCP seq_add_property_track"):
    track = b.add_track(cls)
    if track is not None:
        try:
            track.set_property_name_and_path(prop_name, prop_path)
        except Exception:
            pass
if track is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_track returned None for %s" % cls_name})); raise SystemExit
_save(PARAMS["sequence_path"])
_ledger().append({"op": "seq_add_property_track", "asset_path": PARAMS["sequence_path"],
                  "binding_name": _b_name(b), "track_class": cls_name, "prior_count": prior_count})
pn = None
try: pn = str(track.get_property_name())
except Exception: pass
print("@@UMCP@@" + json.dumps({"status": "success", "binding": PARAMS["binding"], "track_class": cls_name,
    "property_name": pn, "property_path": prop_path, "auto_detected_type": detected,
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_property_track(ctx, sequence_path: str, binding: str, property_name: str,
                           property_type: str = None, property_path: str = None) -> str:
        """Add a property track to a binding, auto-detecting the property type when possible.

        sequence_path: object/package path of the LevelSequence.
        binding:       binding display or internal name.
        property_name: the object property to animate (e.g. 'Intensity', 'bHidden', 'RelativeLocation').
        property_type: optional explicit type: float/double/bool/visibility/int/integer/byte/enum/string/
                       color/vector/transform. If omitted, the tool resolves the bound level actor and reads
                       the property via reflection to pick the track class (bool->Bool, 'hidden'/'visib'->
                       Visibility, int->Integer, float->Float, Color->Color, Vector->DoubleVector, Enum->Enum,
                       str->String).
        property_path: optional dotted path (defaults to property_name).

        Uses binding.add_track(<MovieSceneXxxTrack>) then track.set_property_name_and_path(name, path).
        Ledgered op 'seq_add_property_track' {asset_path, binding_name, track_class, prior_count}. Inverse:
        find the binding, and if it now has more tracks of track_class than prior_count, remove_track(<last one>)
        -- removes the track, its section and keys. FAITHFUL (only the appended track is removed)."""
        params = {"sequence_path": sequence_path, "binding": binding, "property_name": property_name,
                  "property_type": property_type, "property_path": property_path}
        try:
            return json.dumps(_exec(_ADD_PROP_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_track_types  (read-only)                                      #
    # ------------------------------------------------------------------ #
    _LIST_TRACK_TYPES_BODY = _H + r'''
prop_tracks = ["MovieSceneFloatTrack", "MovieSceneDoubleTrack", "MovieSceneBoolTrack",
    "MovieSceneVisibilityTrack", "MovieSceneIntegerTrack", "MovieSceneByteTrack", "MovieSceneEnumTrack",
    "MovieSceneStringTrack", "MovieSceneColorTrack", "MovieSceneFloatVectorTrack", "MovieSceneDoubleVectorTrack",
    "MovieScene3DTransformTrack", "MovieSceneActorReferenceTrack"]
binding_tracks = ["MovieScene3DTransformTrack", "MovieSceneSkeletalAnimationTrack", "MovieSceneVisibilityTrack",
    "MovieSceneControlRigParameterTrack", "MovieSceneParticleTrack", "MovieSceneGeometryCacheTrack"]
root_tracks = ["MovieSceneCameraCutTrack", "MovieSceneAudioTrack", "MovieSceneEventTrack",
    "MovieSceneCinematicShotTrack", "MovieSceneSubTrack", "MovieSceneFadeTrack", "MovieSceneLevelVisibilityTrack",
    "MovieSceneMaterialParameterCollectionTrack", "MovieSceneSpawnTrack"]
def _present(lst):
    return [{"class": n, "available": hasattr(unreal, n)} for n in lst if hasattr(unreal, n)]
res = {"status": "success",
    "property_tracks": _present(prop_tracks),
    "binding_tracks": _present(binding_tracks),
    "root_tracks": _present(root_tracks),
    "note": "property_tracks are addable via add_property_track (map property_type -> class); binding_tracks attach to an object binding; root_tracks attach to the sequence (master)."}
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_track_types(ctx) -> str:
        """List the MovieScene track classes available in this build, grouped by where they attach.

        Returns property_tracks (animate a reflected property; add via add_property_track), binding_tracks
        (attach to an object binding, e.g. transform / skeletal animation / control rig), and root_tracks
        (attach to the sequence as master tracks, e.g. camera cut / audio / event / cinematic shot). Each entry
        is filtered by hasattr(unreal, <ClassName>) so the list reflects what is actually loaded. Read-only."""
        try:
            return json.dumps(_exec(_LIST_TRACK_TYPES_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_animatable_properties  (read-only, best-effort reflection)    #
    # ------------------------------------------------------------------ #
    _LIST_ANIM_PROPS_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
b = _find_binding(seq, PARAMS["binding"])
if b is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding not found: %s" % PARAMS["binding"],
        "available_bindings": _binding_names(seq)})); raise SystemExit
bound_cls = None
try:
    c = b.get_possessed_object_class(); bound_cls = c.get_name() if c else None
except Exception:
    pass
obj = _resolve_bound_object(seq, b)
# curated set of commonly-animated properties -- probe presence + reflected python type -> suggested track type
candidates = ["relative_location", "relative_rotation", "relative_scale3d", "hidden", "hidden_in_game",
    "intensity", "light_color", "attenuation_radius", "source_radius", "outer_cone_angle", "inner_cone_angle",
    "temperature", "indirect_lighting_intensity", "volumetric_scattering_intensity", "field_of_view",
    "current_focal_length", "opacity", "relative_location", "cast_shadow"]
found = []
if obj is not None:
    seen = set()
    targets = [obj]
    try:
        rc = obj.get_editor_property("root_component")
        if rc is not None: targets.append(rc)
    except Exception:
        pass
    for t in targets:
        for name in candidates:
            if name in seen: continue
            try:
                v = t.get_editor_property(name); seen.add(name)
                key = _auto_track_key(v, name)
                found.append({"property": name, "on": t.get_class().get_name(),
                    "python_type": type(v).__name__, "suggested_track_type": key})
            except Exception:
                pass
res = {"status": "success", "binding": PARAMS["binding"], "bound_object_class": bound_cls,
    "resolved_object": (obj.get_name() if obj is not None else None),
    "supported_property_types": sorted(set(_PROP_TRACKS.keys())),
    "detected_properties": found,
    "note": "detected_properties is a best-effort probe of common animatable properties on the bound object (stock UE Python does not expose an exhaustive FProperty enumeration); pass any property_name to add_property_track and it will resolve the type by reflection."}
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_animatable_properties(ctx, sequence_path: str, binding: str) -> str:
        """List animatable properties for a binding's bound object (best-effort) + the supported track-type catalog.

        sequence_path: object/package path of the LevelSequence.
        binding:       binding display or internal name.

        Resolves the bound level actor and probes a curated set of commonly-animated properties (transform,
        light, camera, rendering), reporting each present property's python type and the suggested track type,
        plus the full supported property-type catalog accepted by add_property_track. NOTE: stock UE Python does
        not expose an exhaustive per-object FProperty enumeration, so detected_properties is a best-effort sample
        -- add_property_track resolves the exact type for ANY property_name you pass by reflection. Read-only."""
        params = {"sequence_path": sequence_path, "binding": binding}
        try:
            return json.dumps(_exec(_LIST_ANIM_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # SPECIALIZED                                                        #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # add_subsequence  (cinematic shot track)                            #
    # ------------------------------------------------------------------ #
    _ADD_SUBSEQ_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sub, serr = _load_seq(PARAMS["subsequence_path"])
if serr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "subsequence: %s" % serr})); raise SystemExit
if sub.get_path_name() == seq.get_path_name():
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "a sequence cannot contain itself"})); raise SystemExit
start = int(PARAMS.get("start_frame", 0)); end = int(PARAMS.get("end_frame", 100))
if end <= start:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "end_frame must be > start_frame"})); raise SystemExit
existing = [t for t in (seq.get_tracks() or []) if t.get_class().get_name() == "MovieSceneCinematicShotTrack"]
added_track = False
with unreal.ScopedEditorTransaction("MCP seq_add_subsequence"):
    if existing:
        track = existing[0]
    else:
        track = seq.add_track(unreal.MovieSceneCinematicShotTrack); added_track = True
    prior_section_count = len(track.get_sections() or [])
    section = track.add_section()
    try: section.set_sequence(sub)
    except Exception: pass
    try: section.set_range(start, end)
    except Exception: pass
_save(PARAMS["sequence_path"])
new_section_index = len(track.get_sections() or []) - 1
_ledger().append({"op": "seq_add_subsequence", "asset_path": PARAMS["sequence_path"],
                  "added_track": added_track, "section_index": new_section_index,
                  "prior_section_count": prior_section_count})
sub_name = None
try: sub_name = section.get_sequence().get_name() if section.get_sequence() else None
except Exception: pass
print("@@UMCP@@" + json.dumps({"status": "success", "track_class": track.get_class().get_name(),
    "added_track": added_track, "section_class": section.get_class().get_name(),
    "subsequence": sub_name, "range": [start, end], "section_index": new_section_index,
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_subsequence(ctx, sequence_path: str, subsequence_path: str,
                        start_frame: int = 0, end_frame: int = 100) -> str:
        """Nest another LevelSequence as a shot on this sequence's Cinematic Shot (subsequence) track.

        sequence_path:    object/package path of the parent LevelSequence.
        subsequence_path: object/package path of the LevelSequence to nest.
        start_frame/end_frame: the shot's frame range on the parent (display-rate frames).

        Reuses the sequence's existing MovieSceneCinematicShotTrack or adds one (seq.add_track), then
        track.add_section() -> MovieSceneCinematicShotSection, section.set_sequence(sub), section.set_range.
        Ledgered op 'seq_add_subsequence' {asset_path, added_track, section_index, prior_section_count}.
        Inverse: if added_track, remove that shot track (removes all its sections); else remove the section we
        appended (the one at section_index / the last one). FAITHFUL (only what we added is removed)."""
        params = {"sequence_path": sequence_path, "subsequence_path": subsequence_path,
                  "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_ADD_SUBSEQ_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_marked_frame                                                   #
    # ------------------------------------------------------------------ #
    _ADD_MARKED_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
frame = int(PARAMS["frame"]); label = str(PARAMS.get("label") or "")
mf = unreal.MovieSceneMarkedFrame()
mf.set_editor_property("frame_number", unreal.FrameNumber(frame))
if label:
    mf.set_editor_property("label", label)
with unreal.ScopedEditorTransaction("MCP seq_add_marked_frame"):
    idx = seq.add_marked_frame(mf)
_save(PARAMS["sequence_path"])
marks = seq.get_marked_frames() or []
_ledger().append({"op": "seq_add_marked_frame", "asset_path": PARAMS["sequence_path"],
                  "frame": frame, "label": label})
print("@@UMCP@@" + json.dumps({"status": "success", "frame": frame, "label": label, "add_index": idx,
    "marked_frame_count": len(marks), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_marked_frame(ctx, sequence_path: str, frame: int, label: str = None) -> str:
        """Add a marked frame (a labelled bookmark on the timeline) to a LevelSequence.

        sequence_path: object/package path of the LevelSequence.
        frame:         frame number (display-rate) to mark.
        label:         optional label text.

        Uses seq.add_marked_frame(unreal.MovieSceneMarkedFrame(frame_number, label)). The marked-frame list is
        kept sorted by frame. Ledgered op 'seq_add_marked_frame' {asset_path, frame, label}. Inverse: scan
        get_marked_frames() for the entry matching frame (and label) and seq.delete_marked_frame(<its index>). FAITHFUL."""
        params = {"sequence_path": sequence_path, "frame": frame, "label": label}
        try:
            return json.dumps(_exec(_ADD_MARKED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_marked_frames  (read-only)                                    #
    # ------------------------------------------------------------------ #
    _LIST_MARKED_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
marks = seq.get_marked_frames() or []
rows = []
for i, m in enumerate(marks):
    fn = m.get_editor_property("frame_number")
    frame = int(fn.value) if hasattr(fn, "value") else int(fn.frame_number.value)
    lbl = None
    try: lbl = str(m.get_editor_property("label"))
    except Exception: pass
    rows.append({"index": i, "frame": frame, "label": lbl})
print("@@UMCP@@" + json.dumps({"status": "success", "sequence": seq.get_name(), "count": len(rows), "marked_frames": rows}))
'''

    @mcp.tool()
    def list_marked_frames(ctx, sequence_path: str) -> str:
        """List a LevelSequence's marked frames (index, frame, label), sorted by frame.

        sequence_path: object/package path of the LevelSequence.

        Uses seq.get_marked_frames(). Read-only. (get_level_sequence_info also surfaces marked frames; this is a
        focused, index-addressable view for use with remove_marked_frame.)"""
        params = {"sequence_path": sequence_path}
        try:
            return json.dumps(_exec(_LIST_MARKED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_marked_frame                                                #
    # ------------------------------------------------------------------ #
    _REMOVE_MARKED_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
marks = seq.get_marked_frames() or []
idx = PARAMS.get("index")
if idx is None:
    # locate by frame
    frame = int(PARAMS["frame"])
    idx = None
    for i, m in enumerate(marks):
        fn = m.get_editor_property("frame_number")
        f = int(fn.value) if hasattr(fn, "value") else int(fn.frame_number.value)
        if f == frame:
            idx = i; break
    if idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no marked frame at frame %d" % frame})); raise SystemExit
idx = int(idx)
if idx < 0 or idx >= len(marks):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "index %d out of range (count=%d)" % (idx, len(marks))})); raise SystemExit
m = marks[idx]
fn = m.get_editor_property("frame_number")
cap_frame = int(fn.value) if hasattr(fn, "value") else int(fn.frame_number.value)
cap_label = ""
try: cap_label = str(m.get_editor_property("label"))
except Exception: pass
cap_det = False; cap_inc = False
try: cap_det = bool(m.get_editor_property("is_determinism_fence"))
except Exception: pass
try: cap_inc = bool(m.get_editor_property("is_inclusive_time"))
except Exception: pass
with unreal.ScopedEditorTransaction("MCP seq_remove_marked_frame"):
    seq.delete_marked_frame(idx)
_save(PARAMS["sequence_path"])
_ledger().append({"op": "seq_remove_marked_frame", "asset_path": PARAMS["sequence_path"],
                  "marked": {"frame": cap_frame, "label": cap_label, "is_determinism_fence": cap_det, "is_inclusive_time": cap_inc}})
print("@@UMCP@@" + json.dumps({"status": "success", "removed_index": idx, "removed_frame": cap_frame,
    "removed_label": cap_label, "marked_frame_count": len(seq.get_marked_frames() or []),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_marked_frame(ctx, sequence_path: str, index: int = None, frame: int = None) -> str:
        """Remove a marked frame from a LevelSequence, by index or by frame.

        sequence_path: object/package path of the LevelSequence.
        index:         index into list_marked_frames (preferred).
        frame:         alternatively, the frame number of the mark to remove.

        Captures the mark's full state (frame, label, is_determinism_fence, is_inclusive_time) then
        seq.delete_marked_frame(index). Ledgered op 'seq_remove_marked_frame' {asset_path, marked:{...}}.
        Inverse: rebuild a MovieSceneMarkedFrame from the captured state and seq.add_marked_frame(it). FAITHFUL."""
        params = {"sequence_path": sequence_path, "index": index, "frame": frame}
        try:
            return json.dumps(_exec(_REMOVE_MARKED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_folder                                                         #
    # ------------------------------------------------------------------ #
    _ADD_FOLDER_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
name = str(PARAMS["name"])
if _find_folder(seq, name) is not None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "a root folder named '%s' already exists" % name})); raise SystemExit
folder = None
with unreal.ScopedEditorTransaction("MCP seq_add_folder"):
    folder = seq.add_root_folder_to_sequence(name)
    if folder is not None:
        col = PARAMS.get("color")
        if col and isinstance(col, (list, tuple)) and len(col) >= 3:
            try:
                a = float(col[3]) if len(col) > 3 else 1.0
                folder.set_folder_color(unreal.Color(r=int(col[0]), g=int(col[1]), b=int(col[2]), a=int(a)))
            except Exception:
                pass
if folder is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_root_folder_to_sequence returned None"})); raise SystemExit
_save(PARAMS["sequence_path"])
_ledger().append({"op": "seq_add_folder", "asset_path": PARAMS["sequence_path"], "folder_name": name})
print("@@UMCP@@" + json.dumps({"status": "success", "folder_name": str(folder.get_folder_name()),
    "root_folder_count": len(seq.get_root_folders_in_sequence() or []), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_folder(ctx, sequence_path: str, name: str, color: list = None) -> str:
        """Add a root organization folder to a LevelSequence's Sequencer outliner.

        sequence_path: object/package path of the LevelSequence.
        name:          folder name (must be unique among root folders).
        color:         optional [r,g,b] or [r,g,b,a] 0-255 folder tint.

        Uses seq.add_root_folder_to_sequence(name) (+ folder.set_folder_color). Ledgered op 'seq_add_folder'
        {asset_path, folder_name}. Inverse: find the root folder by name and
        seq.remove_root_folder_from_sequence(folder). FAITHFUL (the folder starts empty)."""
        params = {"sequence_path": sequence_path, "name": name, "color": color}
        try:
            return json.dumps(_exec(_ADD_FOLDER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_to_folder                                                      #
    # ------------------------------------------------------------------ #
    _ADD_TO_FOLDER_BODY = _H + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
folder = _find_folder(seq, PARAMS["folder_name"])
if folder is None:
    avail = [str(f.get_folder_name()) for f in (seq.get_root_folders_in_sequence() or [])]
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "root folder not found: %s" % PARAMS["folder_name"],
        "available_folders": avail})); raise SystemExit
binding_sel = PARAMS.get("binding")
track_index = PARAMS.get("track_index")
if binding_sel:
    b = _find_binding(seq, binding_sel)
    if b is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "binding not found: %s" % binding_sel,
            "available_bindings": _binding_names(seq)})); raise SystemExit
    with unreal.ScopedEditorTransaction("MCP seq_add_to_folder"):
        folder.add_child_object_binding(b)
    _save(PARAMS["sequence_path"])
    _ledger().append({"op": "seq_add_to_folder", "asset_path": PARAMS["sequence_path"],
                      "folder_name": PARAMS["folder_name"], "child_kind": "binding", "binding_name": _b_name(b)})
    print("@@UMCP@@" + json.dumps({"status": "success", "folder": PARAMS["folder_name"], "added_binding": binding_sel,
        "child_binding_count": len(folder.get_child_object_bindings() or []), "ledger_depth": len(_ledger())}))
elif track_index is not None:
    tracks = list(seq.get_tracks() or [])
    ti = int(track_index)
    if ti < 0 or ti >= len(tracks):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "track_index %d out of range (master track_count=%d)" % (ti, len(tracks))})); raise SystemExit
    tr = tracks[ti]
    with unreal.ScopedEditorTransaction("MCP seq_add_to_folder"):
        folder.add_child_track(tr)
    _save(PARAMS["sequence_path"])
    _ledger().append({"op": "seq_add_to_folder", "asset_path": PARAMS["sequence_path"],
                      "folder_name": PARAMS["folder_name"], "child_kind": "track", "track_index": ti})
    print("@@UMCP@@" + json.dumps({"status": "success", "folder": PARAMS["folder_name"], "added_track_index": ti,
        "track_class": tr.get_class().get_name(), "child_track_count": len(folder.get_child_tracks() or []),
        "ledger_depth": len(_ledger())}))
else:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide either binding or track_index"})); raise SystemExit
'''

    @mcp.tool()
    def add_to_folder(ctx, sequence_path: str, folder_name: str,
                      binding: str = None, track_index: int = None) -> str:
        """Move an object binding or a master (root) track into a Sequencer folder.

        sequence_path: object/package path of the LevelSequence.
        folder_name:   name of an existing root folder (see add_folder).
        binding:       binding display/internal name to file under the folder, OR
        track_index:   index into the sequence's MASTER tracks (seq.get_tracks()) to file under the folder.

        Uses folder.add_child_object_binding(<binding proxy>) or folder.add_child_track(<track>). Ledgered op
        'seq_add_to_folder' {asset_path, folder_name, child_kind, binding_name|track_index}. Inverse: re-find the
        folder by name and remove_child_object_binding(<binding>) / remove_child_track(<track>). FAITHFUL."""
        params = {"sequence_path": sequence_path, "folder_name": folder_name,
                  "binding": binding, "track_index": track_index}
        try:
            return json.dumps(_exec(_ADD_TO_FOLDER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # DEFERRED (refused-not-faked; no editor I/O, no mutation)           #
    # ================================================================== #
    def _blocked(name, reason, alternative=None):
        payload = {"status": "blocked", "tool": name, "mutated": False, "reason": reason}
        if alternative:
            payload["alternative"] = alternative
        return json.dumps(payload, indent=2)

    @mcp.tool()
    def remove_binding(ctx, sequence_path: str, binding: str) -> str:
        """DEFERRED (no faithful inverse): remove an object binding from a LevelSequence.

        binding.remove() is reachable, but removing an ARBITRARY binding destroys its tracks, sections, channel
        keys/defaults/extrapolation, per-key tangents, and (spawnable) its object template -- and
        MovieSceneBindingProxy exposes no export_text/import_text in Python, so the content cannot be
        reconstructed. Shipping an unrevertable mutation violates the reversibility rule, so this refuses and
        does NO editor I/O (same principled stance as G1-A's remove_track/remove_section). A binding YOU added
        is already reversible via the add-side undo (add_actor_binding / possess_component / add_spawnable_*)."""
        return _blocked("remove_binding",
            "No faithful inverse: removing a binding destroys its tracks/sections/keys/spawnable template and MovieSceneBindingProxy has no export_text in UE 5.8.",
            "Undo the original add (add_actor_binding / possess_component / add_spawnable_from_class/instance) to remove a binding you created.")

    @mcp.tool()
    def convert_to_spawnable(ctx, sequence_path: str, binding: str) -> str:
        """DEFERRED (not Python-reachable in UE 5.8): convert a possessable binding to a spawnable.

        Possessable<->spawnable conversion lives in the editor controller (FSequencer::ConvertToSpawnableInternal)
        and is NOT exposed to scripting -- there is no convert function on MovieSceneSequenceExtensions,
        SequencerTools, or LevelSequenceEditorBlueprintLibrary (verified live). Use add_spawnable_from_class /
        add_spawnable_from_instance to author a spawnable directly instead."""
        return _blocked("convert_to_spawnable",
            "No scripting API for possessable->spawnable conversion in UE 5.8 (FSequencer editor-controller only).",
            "add_spawnable_from_class / add_spawnable_from_instance to create a spawnable binding directly.")

    @mcp.tool()
    def convert_to_possessable(ctx, sequence_path: str, binding: str) -> str:
        """DEFERRED (not Python-reachable in UE 5.8): convert a spawnable binding to a possessable.

        Same reason as convert_to_spawnable -- spawnable<->possessable conversion is editor-controller-only
        (FSequencer) and absent from every scripting surface. Use add_possessable / possess_component to author a
        possessable directly instead."""
        return _blocked("convert_to_possessable",
            "No scripting API for spawnable->possessable conversion in UE 5.8 (FSequencer editor-controller only).",
            "add_actor_binding (add_possessable) / possess_component to create a possessable binding directly.")

    @mcp.tool()
    def tag_binding(ctx, sequence_path: str, binding: str, tag: str) -> str:
        """DEFERRED (not Python-reachable in UE 5.8): add a tag to a binding.

        UMovieSceneSequenceExtensions exposes remove_binding_tag / find_binding_by_tag / find_bindings_by_tag /
        get_all_binding_tags as script functions, but the ADD counterpart (AddBindingTag) is NOT a UFUNCTION
        callable from Python -- there is no add_binding_tag on the sequence, MovieSceneSequenceExtensions, or
        MovieSceneBindingExtensions (verified live). So a binding tag cannot be created from script."""
        return _blocked("tag_binding",
            "AddBindingTag is not exposed to Python in UE 5.8 (only remove/find/get binding-tag functions are ScriptCallable).")

    @mcp.tool()
    def untag_binding(ctx, sequence_path: str, binding: str, tag: str) -> str:
        """DEFERRED (irreversible -- the inverse is not reachable): remove a tag from a binding.

        seq.remove_binding_tag(binding, tag) IS reachable, but its inverse (re-adding the tag) is NOT -- AddBindingTag
        is not exposed to Python in UE 5.8 (see tag_binding). Since the removal could not be faithfully reversed,
        this refuses rather than ship an unrevertable mutation."""
        return _blocked("untag_binding",
            "remove_binding_tag is reachable but its inverse (add binding tag) is not exposed to Python in UE 5.8, so the removal is irreversible.")
