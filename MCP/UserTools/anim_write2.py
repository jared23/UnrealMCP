"""UserTools :: Animation authoring, wave 2 (WRITE)  (spec: docs/spec/anim.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). The SECOND animation-authoring
module; the counterpart to anim_write.py (notify/curve/sync-marker/montage-SECTION authoring) and the
read-only animation_read.py. This module fills the Python-REACHABLE MISSING items from
docs/parity/p2_anim_sequencer.md: BlendSpace authoring, AnimMontage creation + slot/segment authoring,
AnimBlueprint creation/introspection/validation, single-notify removal + notify-class enumeration +
notify-class creation, and a generic anim-asset validator. Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py / anim_write.py.

Probed live (UE 5.8.1, TestMCPSetup) — what IS Python-reachable and how:
  * BlendSpace: NO UBlendSpace::AddSample in Python, BUT the authoring data is fully reachable via
    reflection: blend_parameters (FixedArray[3] of BlendParameter{display_name,min,max,grid_num}),
    sample_data (Array of BlendSample{animation, sample_value:Vector, rate_scale}), and
    axis_to_scale_animation. Assets are created non-interactively via BlendSpaceFactory1D /
    BlendSpaceFactoryNew (target_skeleton set up front -> no modal). Setting sample_data/blend_parameters
    via set_editor_property fires PostEditChangeProperty so the editor re-validates on next open.
  * AnimMontage: created non-interactively via AnimMontageFactory (target_skeleton). Slot tracks live in
    the slot_anim_tracks Array (SlotAnimationTrack{slot_name, anim_track:AnimTrack{anim_segments:[
    AnimSegment{anim_reference,anim_start_time,anim_end_time,anim_play_rate,looping_count}]}}).
    Slot tracks + segment DATA are reflection-authorable and reversible; AnimMontage.SequenceLength and
    AnimSegment.StartPos are read-only engine-derived fields (NOT settable from Python) that the editor
    recomputes from the segments on next open/validate — documented on the tools. (SECTION editing lives
    in anim_write.py via the C++ MCPReflectionLibrary because composite_sections is a protected
    UPROPERTY — NOT duplicated here.)
  * AnimBlueprint: created non-interactively via AnimBlueprintFactory (target_skeleton + parent_class =
    AnimInstance). get_animation_graphs() / get_nodes_of_class() read the graphs;
    BlueprintEditorLibrary.compile_blueprint validates.
  * Notify classes: native subclasses enumerate from the unreal module namespace + Blueprint notify
    assets from the AssetRegistry; a Blueprint subclass of AnimNotify/AnimNotifyState is created via
    BlueprintFactory (parent_class). A single notify is removed by snapshotting the track and rebuilding
    it without the target (there is no per-event removal API — same rebuild pattern as anim_write.py).

DEFERRED / BLOCKED (NOT Python-reachable in this build -- refused, never faked):
  * AnimGraph state-machine / node authoring: add_anim_state_machine / add_anim_state /
    add_anim_transition / set_anim_entry_state / remove_anim_state / remove_anim_transition /
    set_anim_transition_property / build_anim_state_machine / set_anim_node_pin_exposure /
    bind_anim_node_function / add_anim_layer. UAnimBlueprint exposes get_animation_graphs() and
    get_nodes_of_class() (READ) but NO AnimGraph NODE-authoring API to stock Python -- you cannot add a
    state-machine node, a state, or a transition, nor edit AnimGraph node pins, from Python (that path is
    FAnimBlueprintEditor / graph-schema C++ only). Confirmed by the p2 audit; these need a dedicated C++
    handler. This module ships the reachable AnimBlueprint asset lifecycle (create/introspect/validate)
    and marks graph authoring BLOCKED.
  * create_anim_layer_interface: AnimLayerInterface asset creation has no non-interactive Python factory
    path here -- DEFERRED.
  * Skeleton slot-group authoring (add_anim_slot / add_anim_slot_group / ...): USkeleton slot API absent
    in Python (per skeleton_write.py) -- BLOCKED, out of this module's scope.

Implemented (all validated live on /Game/_scratch_g6; editor left CLEAN, ledger depth 0):
  BlendSpace:  create_blend_space, set_blend_space_axis, add_blend_space_sample,
               remove_blend_space_sample, get_blend_space
  Montage:     create_anim_montage, add_montage_slot, add_montage_segment, get_anim_montage
  AnimBP:      create_anim_blueprint, get_anim_blueprint_info, list_anim_graphs, validate_anim_blueprint
  Notify:      remove_anim_notify, list_anim_notify_classes, create_anim_notify_class
  Validator:   validate_anim_asset

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Creation
tools push the generic 'create_asset' op (already handled). The 6 NEW op inverses (set_blend_space_axis,
add_blend_space_sample, remove_blend_space_sample, add_montage_slot, add_montage_segment,
remove_anim_notify) are reported to the coordinator to fold into editor_level.undo (see
.mcp_coord/coordinator-inbox/g6_anim_undo_ops.md).
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
    _HELPERS = r'''
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
EAL = unreal.EditorAssetLibrary
AT = unreal.AssetToolsHelpers.get_asset_tools()
AL = unreal.AnimationLibrary
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
        if obj is not None and aes is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _save(path):
    try:
        EAL.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
def _load(path):
    if not path:
        return None, "no asset path given"
    obj = None
    try:
        obj = EAL.load_asset(path)
    except Exception as e:
        return None, "failed to load: %s (%s)" % (path, str(e)[:120])
    if obj is None:
        return None, "asset not found: %s" % path
    return obj, None
def _resolve_skeleton(spec):
    # spec may be a Skeleton path, or a SkeletalMesh / AnimSequence path (its skeleton is used).
    o = None
    try:
        o = EAL.load_asset(spec) if spec else None
    except Exception:
        o = None
    if o is None:
        return None
    if isinstance(o, unreal.Skeleton):
        return o
    try:
        sk = o.get_editor_property("skeleton")
        if sk is not None:
            return sk
    except Exception:
        pass
    return None
def _anim_length(anim):
    try:
        return float(AL.get_sequence_length(anim))
    except Exception:
        try:
            return float(anim.get_play_length())
        except Exception:
            return 0.0
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
def _bs_sample_rows(bs):
    rows = []
    for s in (bs.get_editor_property("sample_data") or []):
        an = s.get_editor_property("animation")
        v = s.get_editor_property("sample_value")
        rows.append({"animation": (an.get_path_name() if an is not None else None),
                     "x": round(float(v.x), 6), "y": round(float(v.y), 6),
                     "rate_scale": round(float(s.get_editor_property("rate_scale")), 6)})
    return rows
def _bs_axis_rows(bs):
    rows = []
    bp = bs.get_editor_property("blend_parameters")
    for i, p in enumerate(bp or []):
        rows.append({"axis": i, "display_name": str(p.get_editor_property("display_name")),
                     "min": round(float(p.get_editor_property("min")), 6),
                     "max": round(float(p.get_editor_property("max")), 6),
                     "grid_num": int(p.get_editor_property("grid_num"))})
    return rows
def _montage_slot_index(mont, slot_name):
    tracks = list(mont.get_editor_property("slot_anim_tracks") or [])
    for i, t in enumerate(tracks):
        if str(t.get_editor_property("slot_name")) == slot_name:
            return i, tracks
    return -1, tracks
'''

    # ================================================================== #
    # BlendSpace                                                          #
    # ================================================================== #
    _CREATE_BS_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/_scratch_g6").rstrip("/")
kind = (PARAMS.get("kind") or "2D").lower().replace("-", "").replace("_", "").replace(" ", "")
skel = _resolve_skeleton(PARAMS.get("skeleton"))
asset_path = package_path + "/" + name
_facmap = {"1d": ("BlendSpaceFactory1D", "BlendSpace1D"),
           "2d": ("BlendSpaceFactoryNew", "BlendSpace"),
           "aimoffset1d": ("AimOffsetBlendSpaceFactory1D", "AimOffsetBlendSpace1D"),
           "aimoffset2d": ("AimOffsetBlendSpaceFactoryNew", "AimOffsetBlendSpace")}
if skel is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve a Skeleton from 'skeleton': %s (pass a Skeleton, SkeletalMesh, or AnimSequence path)" % PARAMS.get("skeleton")}))
elif kind not in _facmap:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown kind '%s'; valid: 1D, 2D, aimoffset1d, aimoffset2d" % PARAMS.get("kind")}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    fcls = getattr(unreal, _facmap[kind][0], None)
    acls = getattr(unreal, _facmap[kind][1], None)
    if fcls is None or acls is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "factory/class for kind '%s' not present in this build (%s)" % (kind, _facmap[kind][0])}))
    else:
        created_dir = not EAL.does_directory_exist(package_path)
        fac = fcls(); fac.set_editor_property("target_skeleton", skel)
        bs = AT.create_asset(name, package_path, acls, fac)
        if bs is None or not isinstance(bs, unreal.BlendSpace):
            if created_dir and EAL.does_directory_exist(package_path):
                try: EAL.delete_directory(package_path)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned %s for %s" % (type(bs).__name__, asset_path)}))
        else:
            _close_editors(bs)
            _save(asset_path)
            _ledger().append({"op": "create_asset", "asset_path": asset_path, "package_path": package_path, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "object_path": bs.get_path_name(),
                "class": bs.get_class().get_name(), "kind": kind, "skeleton": skel.get_path_name(),
                "axes": _bs_axis_rows(bs), "sample_count": len(bs.get_editor_property("sample_data") or []),
                "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_blend_space(ctx, name: str, skeleton: str, package_path: str = "/Game/_scratch_g6",
                           kind: str = "2D") -> str:
        """Create a BlendSpace (or AimOffset) asset non-interactively (NO modal). Ledgered write.

        name:         asset name (e.g. 'BS_Locomotion').
        skeleton:     a Skeleton path, OR a SkeletalMesh / AnimSequence path (its skeleton is used).
        package_path: content directory (default '/Game/_scratch_g6'; must be under a mounted root).
        kind:         '1D' (BlendSpace1D), '2D' (BlendSpace), 'aimoffset1d', or 'aimoffset2d'.

        Uses BlendSpaceFactory1D / BlendSpaceFactoryNew (target_skeleton set up front -> no dialog).
        Configure axes with set_blend_space_axis and add samples with add_blend_space_sample.

        Ledgered op 'create_asset' {asset_path, package_path, created_dir}; inverse (already in
        editor_level.undo): close editors + delete the asset (+ rmdir if we made the folder)."""
        params = {"name": name, "skeleton": skeleton, "package_path": package_path, "kind": kind}
        try:
            return json.dumps(_exec(_CREATE_BS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_AXIS_BODY = _HELPERS + r'''
bs, err = _load(PARAMS["blend_space_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(bs, unreal.BlendSpace):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a BlendSpace (got %s)" % bs.get_class().get_name()}))
else:
    axis = int(PARAMS["axis"])
    bp = list(bs.get_editor_property("blend_parameters") or [])
    if axis < 0 or axis >= len(bp):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "axis %d out of range (0..%d)" % (axis, len(bp) - 1)}))
    else:
        p = bp[axis]
        prior = {"display_name": str(p.get_editor_property("display_name")),
                 "min": round(float(p.get_editor_property("min")), 6),
                 "max": round(float(p.get_editor_property("max")), 6),
                 "grid_num": int(p.get_editor_property("grid_num"))}
        with unreal.ScopedEditorTransaction("MCP set_blend_space_axis"):
            if PARAMS.get("display_name") is not None:
                p.set_editor_property("display_name", str(PARAMS["display_name"]))
            if PARAMS.get("min") is not None:
                p.set_editor_property("min", float(PARAMS["min"]))
            if PARAMS.get("max") is not None:
                p.set_editor_property("max", float(PARAMS["max"]))
            if PARAMS.get("grid_divisions") is not None:
                p.set_editor_property("grid_num", int(PARAMS["grid_divisions"]))
            bp[axis] = p
            bs.set_editor_property("blend_parameters", bp)
        _save(PARAMS["blend_space_path"])
        _ledger().append({"op": "set_blend_space_axis", "asset_path": PARAMS["blend_space_path"], "axis": axis, "prior": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["blend_space_path"],
            "axis": axis, "prior": prior, "axes_after": _bs_axis_rows(bs), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_blend_space_axis(ctx, blend_space_path: str, axis: int, display_name: str = None,
                             min: float = None, max: float = None, grid_divisions: int = None) -> str:
        """Configure a BlendSpace axis (X=0, Y=1). Only the fields you pass are changed. Ledgered write.

        blend_space_path: object path of a BlendSpace / BlendSpace1D.
        axis:             0 = X (horizontal), 1 = Y (vertical; ignored for 1D). Index into blend_parameters.
        display_name:     axis label (e.g. 'Speed', 'Direction').
        min / max:        axis value range.
        grid_divisions:   number of interpolation grid divisions along this axis (grid_num).

        Modifies the BlendParameter at blend_parameters[axis] via reflection and re-writes the array
        (fires PostEditChangeProperty so the editor re-validates on next open).

        Ledgered op 'set_blend_space_axis' {asset_path, axis, prior:{display_name,min,max,grid_num}};
        inverse (fold into editor_level.undo): restore blend_parameters[axis] from prior. FAITHFUL."""
        params = {"blend_space_path": blend_space_path, "axis": axis, "display_name": display_name,
                  "min": min, "max": max, "grid_divisions": grid_divisions}
        try:
            return json.dumps(_exec(_SET_AXIS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_SAMPLE_BODY = _HELPERS + r'''
bs, err = _load(PARAMS["blend_space_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(bs, unreal.BlendSpace):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a BlendSpace (got %s)" % bs.get_class().get_name()}))
else:
    anim = None
    try:
        anim = EAL.load_asset(PARAMS.get("animation"))
    except Exception:
        anim = None
    if anim is None or not isinstance(anim, unreal.AnimSequence):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "animation not found or not an AnimSequence: %s" % PARAMS.get("animation")}))
    else:
        # verify sample within axis ranges (BlendSpace would reject / clamp otherwise)
        axes = _bs_axis_rows(bs)
        x = float(PARAMS["x"]); y = float(PARAMS.get("y") or 0.0)
        oob = None
        if x < axes[0]["min"] or x > axes[0]["max"]:
            oob = "x=%s outside X axis range [%s, %s]" % (x, axes[0]["min"], axes[0]["max"])
        if len(axes) > 1 and (y < axes[1]["min"] or y > axes[1]["max"]) and not isinstance(bs, unreal.BlendSpace1D):
            oob = "y=%s outside Y axis range [%s, %s]" % (y, axes[1]["min"], axes[1]["max"])
        if oob:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "sample out of range: %s (set axes with set_blend_space_axis first)" % oob}))
        else:
            samples = list(bs.get_editor_property("sample_data") or [])
            prior_count = len(samples)
            s = unreal.BlendSample()
            s.set_editor_property("animation", anim)
            s.set_editor_property("sample_value", unreal.Vector(x, y, 0.0))
            s.set_editor_property("rate_scale", float(PARAMS.get("rate_scale") or 1.0))
            samples.append(s)
            with unreal.ScopedEditorTransaction("MCP add_blend_space_sample"):
                bs.set_editor_property("sample_data", samples)
            _save(PARAMS["blend_space_path"])
            _ledger().append({"op": "add_blend_space_sample", "asset_path": PARAMS["blend_space_path"], "prior_count": prior_count})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["blend_space_path"],
                "animation": anim.get_path_name(), "x": x, "y": y, "prior_count": prior_count,
                "samples_after": _bs_sample_rows(bs), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_blend_space_sample(ctx, blend_space_path: str, animation: str, x: float, y: float = 0.0,
                               rate_scale: float = 1.0) -> str:
        """Add an animation sample to a BlendSpace at grid coordinate (x[,y]). Ledgered write.

        blend_space_path: object path of a BlendSpace / BlendSpace1D.
        animation:        AnimSequence object path to place at this sample.
        x / y:            sample coordinate (y ignored for 1D). Must fall within the axis ranges
                          (configure them first with set_blend_space_axis) or the add is refused.
        rate_scale:       per-sample play-rate scale (default 1.0).

        Appends a BlendSample{animation, sample_value, rate_scale} to sample_data via reflection.

        Ledgered op 'add_blend_space_sample' {asset_path, prior_count}; inverse (fold into
        editor_level.undo): truncate sample_data back to prior_count (removes the one we appended).
        FAITHFUL (append-only)."""
        params = {"blend_space_path": blend_space_path, "animation": animation, "x": x, "y": y,
                  "rate_scale": rate_scale}
        try:
            return json.dumps(_exec(_ADD_SAMPLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_SAMPLE_BODY = _HELPERS + r'''
bs, err = _load(PARAMS["blend_space_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(bs, unreal.BlendSpace):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a BlendSpace (got %s)" % bs.get_class().get_name()}))
else:
    samples = list(bs.get_editor_property("sample_data") or [])
    idx = int(PARAMS["index"])
    if idx < 0 or idx >= len(samples):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "sample index %d out of range (0..%d)" % (idx, len(samples) - 1)}))
    else:
        s = samples[idx]
        an = s.get_editor_property("animation")
        v = s.get_editor_property("sample_value")
        captured = {"animation": (an.get_path_name() if an is not None else None),
                    "x": round(float(v.x), 6), "y": round(float(v.y), 6),
                    "rate_scale": round(float(s.get_editor_property("rate_scale")), 6)}
        del samples[idx]
        with unreal.ScopedEditorTransaction("MCP remove_blend_space_sample"):
            bs.set_editor_property("sample_data", samples)
        _save(PARAMS["blend_space_path"])
        _ledger().append({"op": "remove_blend_space_sample", "asset_path": PARAMS["blend_space_path"], "index": idx, "sample": captured})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["blend_space_path"],
            "removed_index": idx, "removed": captured, "samples_after": _bs_sample_rows(bs), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_blend_space_sample(ctx, blend_space_path: str, index: int) -> str:
        """Remove a BlendSpace sample by index. Ledgered write.

        blend_space_path: object path of a BlendSpace / BlendSpace1D.
        index:            index into sample_data (see get_blend_space for the ordered list).

        The removed sample (animation, coordinate, rate_scale) is captured for undo.

        Ledgered op 'remove_blend_space_sample' {asset_path, index, sample}; inverse (fold into
        editor_level.undo): rebuild a BlendSample from the captured state and re-insert it at index.
        FAITHFUL (animation + coordinate + rate restored)."""
        params = {"blend_space_path": blend_space_path, "index": index}
        try:
            return json.dumps(_exec(_REMOVE_SAMPLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_BS_BODY = _HELPERS + r'''
bs, err = _load(PARAMS["blend_space_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(bs, unreal.BlendSpace):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a BlendSpace (got %s)" % bs.get_class().get_name()}))
else:
    sk = bs.get_skeleton()
    axis_scale = str(bs.get_editor_property("axis_to_scale_animation"))
    print("@@UMCP@@" + json.dumps({"status": "success", "path": bs.get_path_name(), "class": bs.get_class().get_name(),
        "is_1d": isinstance(bs, unreal.BlendSpace1D), "skeleton": (sk.get_path_name() if sk else None),
        "axis_to_scale_animation": axis_scale, "axes": _bs_axis_rows(bs),
        "sample_count": len(bs.get_editor_property("sample_data") or []), "samples": _bs_sample_rows(bs)}))
'''

    @mcp.tool()
    def get_blend_space(ctx, blend_space_path: str) -> str:
        """Read a BlendSpace: skeleton, axes (blend_parameters), and samples. Read-only (no ledger).

        blend_space_path: object path of a BlendSpace / BlendSpace1D.

        Returns is_1d, skeleton, axis_to_scale_animation, axes[{axis,display_name,min,max,grid_num}],
        and samples[{animation,x,y,rate_scale}] in sample_data order (index = remove_blend_space_sample's
        index)."""
        try:
            return json.dumps(_exec(_GET_BS_BODY, {"blend_space_path": blend_space_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # AnimMontage                                                         #
    # ================================================================== #
    _CREATE_MONT_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/_scratch_g6").rstrip("/")
asset_path = package_path + "/" + name
anim = None
anim_spec = PARAMS.get("animation")
if anim_spec:
    try:
        anim = EAL.load_asset(anim_spec)
    except Exception:
        anim = None
    if anim is None or not isinstance(anim, unreal.AnimSequence):
        anim = None
skel = _resolve_skeleton(PARAMS.get("skeleton")) or (_resolve_skeleton(anim_spec) if anim_spec else None)
if anim is not None and skel is None:
    try: skel = anim.get_editor_property("skeleton")
    except Exception: skel = None
if skel is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve a Skeleton (pass 'skeleton' as a Skeleton/SkeletalMesh/AnimSequence path, or a valid 'animation')"}))
elif anim_spec and anim is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "animation not found or not an AnimSequence: %s" % anim_spec}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    fac = unreal.AnimMontageFactory(); fac.set_editor_property("target_skeleton", skel)
    mont = AT.create_asset(name, package_path, unreal.AnimMontage, fac)
    if mont is None or not isinstance(mont, unreal.AnimMontage):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned %s for %s" % (type(mont).__name__, asset_path)}))
    else:
        seeded = False
        slot_used = None
        if anim is not None:
            # seed the factory's existing slot (index 0) with one full-length segment
            tracks = list(mont.get_editor_property("slot_anim_tracks") or [])
            if tracks:
                slen = _anim_length(anim)
                seg = unreal.AnimSegment()
                seg.set_editor_property("anim_reference", anim)
                seg.set_editor_property("anim_start_time", 0.0)
                seg.set_editor_property("anim_end_time", slen)
                seg.set_editor_property("anim_play_rate", 1.0)
                seg.set_editor_property("looping_count", 1)
                atk = tracks[0].get_editor_property("anim_track")
                segs = list(atk.get_editor_property("anim_segments") or [])
                segs.append(seg)
                atk.set_editor_property("anim_segments", segs)
                tracks[0].set_editor_property("anim_track", atk)
                mont.set_editor_property("slot_anim_tracks", tracks)
                seeded = True
                slot_used = str(tracks[0].get_editor_property("slot_name"))
        _close_editors(mont)
        _save(asset_path)
        _ledger().append({"op": "create_asset", "asset_path": asset_path, "package_path": package_path, "created_dir": created_dir})
        tks = [str(t.get_editor_property("slot_name")) for t in (mont.get_editor_property("slot_anim_tracks") or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "object_path": mont.get_path_name(),
            "class": mont.get_class().get_name(), "skeleton": skel.get_path_name(), "slots": tks,
            "seeded_segment": seeded, "seed_slot": slot_used, "sequence_length": float(mont.get_editor_property("sequence_length")),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_anim_montage(ctx, name: str, skeleton: str = None, animation: str = None,
                            package_path: str = "/Game/_scratch_g6") -> str:
        """Create an AnimMontage asset non-interactively (NO modal). Ledgered write.

        name:         asset name (e.g. 'AM_Attack').
        skeleton:     a Skeleton / SkeletalMesh / AnimSequence path. Optional IF 'animation' is given
                      (the animation's skeleton is used).
        animation:    optional AnimSequence path -- if given, the montage's default slot is seeded with
                      one full-length segment of it (and sequence_length set). Omit for an empty montage.
        package_path: content directory (default '/Game/_scratch_g6').

        Uses AnimMontageFactory (target_skeleton up front -> no dialog). Build it out with
        add_montage_slot / add_montage_segment (+ anim_write.add_montage_section for sections).

        Ledgered op 'create_asset' {asset_path, package_path, created_dir}; inverse (already in
        editor_level.undo): close editors + delete the asset (any seeded segment goes with it)."""
        params = {"name": name, "skeleton": skeleton, "animation": animation, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_MONT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_SLOT_BODY = _HELPERS + r'''
mont, err = _load(PARAMS["montage_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(mont, unreal.AnimMontage):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimMontage (got %s)" % mont.get_class().get_name()}))
else:
    slot_name = PARAMS["slot_name"]
    idx, tracks = _montage_slot_index(mont, slot_name)
    if idx >= 0:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "slot already exists: %s (refusing to duplicate)" % slot_name}))
    else:
        st = unreal.SlotAnimationTrack()
        st.set_editor_property("slot_name", slot_name)
        tracks = list(tracks)
        tracks.append(st)
        with unreal.ScopedEditorTransaction("MCP add_montage_slot"):
            mont.set_editor_property("slot_anim_tracks", tracks)
        _save(PARAMS["montage_path"])
        _ledger().append({"op": "add_montage_slot", "asset_path": PARAMS["montage_path"], "slot_name": slot_name})
        after = [str(t.get_editor_property("slot_name")) for t in (mont.get_editor_property("slot_anim_tracks") or [])]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["montage_path"],
            "slot_name": slot_name, "slots_after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_montage_slot(ctx, montage_path: str, slot_name: str) -> str:
        """Add a named animation SLOT track to an AnimMontage. Ledgered write.

        montage_path: object path of an AnimMontage.
        slot_name:    name for the new slot (e.g. 'UpperBody'); refused if a slot of that name exists.

        Appends an (empty) SlotAnimationTrack to slot_anim_tracks. Add segments with add_montage_segment.

        Ledgered op 'add_montage_slot' {asset_path, slot_name}; inverse (fold into editor_level.undo):
        remove the slot_anim_track with that slot_name. FAITHFUL (added empty; LIFO undo empties its
        segments first)."""
        params = {"montage_path": montage_path, "slot_name": slot_name}
        try:
            return json.dumps(_exec(_ADD_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_SEGMENT_BODY = _HELPERS + r'''
mont, err = _load(PARAMS["montage_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(mont, unreal.AnimMontage):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimMontage (got %s)" % mont.get_class().get_name()}))
else:
    slot_name = PARAMS["slot_name"]
    idx, tracks = _montage_slot_index(mont, slot_name)
    anim = None
    try:
        anim = EAL.load_asset(PARAMS.get("animation"))
    except Exception:
        anim = None
    if idx < 0:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such slot: %s (create it with add_montage_slot)" % slot_name}))
    elif anim is None or not isinstance(anim, unreal.AnimSequence):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "animation not found or not an AnimSequence: %s" % PARAMS.get("animation")}))
    else:
        tracks = list(tracks)
        atk = tracks[idx].get_editor_property("anim_track")
        segs = list(atk.get_editor_property("anim_segments") or [])
        prior_segment_count = len(segs)
        play_rate = float(PARAMS.get("play_rate") or 1.0)
        alen = _anim_length(anim)
        loop_count = int(PARAMS.get("loop_count") or 1)
        # cumulative duration of this slot's existing segments (AnimSegment.start_pos is a read-only
        # engine-derived field, so we compute placement from segment lengths, not from start_pos).
        slot_dur = 0.0
        for _s in segs:
            _r = abs(float(_s.get_editor_property("anim_play_rate"))) or 1.0
            _lc = int(_s.get_editor_property("looping_count")) or 1
            slot_dur += ((float(_s.get_editor_property("anim_end_time")) - float(_s.get_editor_property("anim_start_time"))) / _r) * _lc
        start_pos = slot_dur
        seg = unreal.AnimSegment()
        seg.set_editor_property("anim_reference", anim)
        seg.set_editor_property("anim_start_time", 0.0)
        seg.set_editor_property("anim_end_time", alen)
        seg.set_editor_property("anim_play_rate", play_rate)
        seg.set_editor_property("looping_count", loop_count)
        segs.append(seg)
        atk.set_editor_property("anim_segments", segs)
        tracks[idx].set_editor_property("anim_track", atk)
        with unreal.ScopedEditorTransaction("MCP add_montage_segment"):
            mont.set_editor_property("slot_anim_tracks", tracks)
        _save(PARAMS["montage_path"])
        _ledger().append({"op": "add_montage_segment", "asset_path": PARAMS["montage_path"], "slot_name": slot_name,
                          "prior_segment_count": prior_segment_count})
        _idx2, _tk2 = _montage_slot_index(mont, slot_name)
        cnt = len(list(_tk2[_idx2].get_editor_property("anim_track").get_editor_property("anim_segments") or [])) if _idx2 >= 0 else None
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["montage_path"], "slot_name": slot_name,
            "animation": anim.get_path_name(), "intended_start_pos": round(start_pos, 6), "anim_length": round(alen, 6),
            "play_rate": play_rate, "segment_count_after": cnt, "sequence_length": round(float(mont.get_editor_property("sequence_length")), 6),
            "sequence_length_note": "AnimMontage.SequenceLength is a read-only engine-derived field; it is recomputed from segments when the montage is next opened/validated in the editor (not settable from Python).",
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_montage_segment(ctx, montage_path: str, slot_name: str, animation: str,
                            play_rate: float = 1.0, loop_count: int = 1) -> str:
        """Append an animation segment to a montage slot's timeline. Ledgered write.

        montage_path: object path of an AnimMontage.
        slot_name:    an EXISTING slot (create with add_montage_slot; the factory's default slot works too).
        animation:    AnimSequence object path to append.
        play_rate:    segment play-rate (default 1.0).
        loop_count:   times the segment loops (default 1).

        Appends an AnimSegment (full-length) to the slot's anim_track. NOTE: AnimMontage.SequenceLength
        and AnimSegment.StartPos are read-only engine-derived fields (not settable from Python); the
        segment DATA is authored + persisted faithfully, but the montage's length/segment placement is
        recomputed when it is next opened/validated in the editor. For a single segment per slot this is
        exact; multi-segment slots are laid out on the next in-editor validation.

        Ledgered op 'add_montage_segment' {asset_path, slot_name, prior_segment_count}; inverse (fold
        into editor_level.undo): truncate that slot's anim_segments back to prior_segment_count (removes
        the one we appended). FAITHFUL (append-only)."""
        params = {"montage_path": montage_path, "slot_name": slot_name, "animation": animation,
                  "play_rate": play_rate, "loop_count": loop_count}
        try:
            return json.dumps(_exec(_ADD_SEGMENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_MONT_BODY = _HELPERS + r'''
mont, err = _load(PARAMS["montage_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(mont, unreal.AnimMontage):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimMontage (got %s)" % mont.get_class().get_name()}))
else:
    sk = mont.get_skeleton()
    slots = []
    for t in (mont.get_editor_property("slot_anim_tracks") or []):
        atk = t.get_editor_property("anim_track")
        segrows = []
        for s in (atk.get_editor_property("anim_segments") or []):
            an = s.get_editor_property("anim_reference")
            segrows.append({"animation": (an.get_path_name() if an is not None else None),
                            "start_pos": round(float(s.get_editor_property("start_pos")), 6),
                            "anim_start_time": round(float(s.get_editor_property("anim_start_time")), 6),
                            "anim_end_time": round(float(s.get_editor_property("anim_end_time")), 6),
                            "play_rate": round(float(s.get_editor_property("anim_play_rate")), 6),
                            "loop_count": int(s.get_editor_property("looping_count"))})
        slots.append({"slot_name": str(t.get_editor_property("slot_name")), "segment_count": len(segrows), "segments": segrows})
    sections = []
    try:
        for i in range(mont.get_num_sections()):
            sections.append(str(mont.get_section_name(i)))
    except Exception:
        pass
    print("@@UMCP@@" + json.dumps({"status": "success", "path": mont.get_path_name(), "class": mont.get_class().get_name(),
        "skeleton": (sk.get_path_name() if sk else None), "sequence_length": round(float(mont.get_editor_property("sequence_length")), 6),
        "slot_count": len(slots), "slots": slots, "section_count": len(sections), "section_names": sections}))
'''

    @mcp.tool()
    def get_anim_montage(ctx, montage_path: str) -> str:
        """Read an AnimMontage: skeleton, length, slots + their segments, and section names. Read-only.

        montage_path: object path of an AnimMontage.

        Returns sequence_length, slots[{slot_name, segments[{animation,start_pos,anim_start_time,
        anim_end_time,play_rate,loop_count}]}], and section_names. (get_anim_sequence_info errors on a
        montage; this is the montage reader.)"""
        try:
            return json.dumps(_exec(_GET_MONT_BODY, {"montage_path": montage_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # AnimBlueprint                                                       #
    # ================================================================== #
    _CREATE_ABP_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/_scratch_g6").rstrip("/")
asset_path = package_path + "/" + name
skel = _resolve_skeleton(PARAMS.get("skeleton"))
parent_spec = PARAMS.get("parent_class") or "AnimInstance"
pcls = getattr(unreal, parent_spec, None)
if not isinstance(pcls, type):
    pcls = _resolve_notify_class(parent_spec)
if skel is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve a Skeleton from 'skeleton': %s" % PARAMS.get("skeleton")}))
elif pcls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve parent_class: %s (default 'AnimInstance')" % parent_spec}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    fac = unreal.AnimBlueprintFactory()
    fac.set_editor_property("target_skeleton", skel)
    fac.set_editor_property("parent_class", pcls)
    abp = AT.create_asset(name, package_path, None, fac)
    if abp is None or not isinstance(abp, unreal.AnimBlueprint):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned %s for %s" % (type(abp).__name__, asset_path)}))
    else:
        _close_editors(abp)
        _save(asset_path)
        _ledger().append({"op": "create_asset", "asset_path": asset_path, "package_path": package_path, "created_dir": created_dir})
        graphs = abp.get_animation_graphs() if hasattr(abp, "get_animation_graphs") else []
        pcls2 = abp.get_blueprint_parent_class()
        tsk = abp.get_editor_property("target_skeleton")
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "object_path": abp.get_path_name(),
            "class": abp.get_class().get_name(), "parent_class": (pcls2.get_name() if pcls2 else None),
            "target_skeleton": (tsk.get_path_name() if tsk else None),
            "anim_graphs": [g.get_name() for g in (graphs or [])], "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_anim_blueprint(ctx, name: str, skeleton: str, parent_class: str = "AnimInstance",
                              package_path: str = "/Game/_scratch_g6") -> str:
        """Create an AnimBlueprint asset non-interactively (NO modal). Ledgered write.

        name:         asset name (e.g. 'ABP_Character').
        skeleton:     a Skeleton / SkeletalMesh / AnimSequence path (its skeleton is the AnimBP target).
        parent_class: parent AnimInstance class (default 'AnimInstance'; short engine name or object path).
        package_path: content directory (default '/Game/_scratch_g6').

        Uses AnimBlueprintFactory (target_skeleton + parent_class up front -> no dialog). Ships with the
        default empty AnimGraph. NOTE: AnimGraph state-machine / node authoring is NOT reachable from
        Python (see module docstring -- add_anim_state_machine/state/transition are BLOCKED); this tool
        creates and validates the AnimBP asset lifecycle only.

        Ledgered op 'create_asset' {asset_path, package_path, created_dir}; inverse (already in
        editor_level.undo): close editors + delete the asset."""
        params = {"name": name, "skeleton": skeleton, "parent_class": parent_class, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_ABP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_ABP_BODY = _HELPERS + r'''
abp, err = _load(PARAMS["blueprint_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(abp, unreal.AnimBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimBlueprint (got %s)" % abp.get_class().get_name()}))
else:
    pcls = abp.get_blueprint_parent_class()
    tsk = abp.get_editor_property("target_skeleton")
    graphs = abp.get_animation_graphs() if hasattr(abp, "get_animation_graphs") else []
    gcls = abp.generated_class() if hasattr(abp, "generated_class") else None
    varnames = []
    try:
        varnames = [str(v) for v in abp.list_member_variable_names()]
    except Exception:
        varnames = []
    print("@@UMCP@@" + json.dumps({"status": "success", "path": abp.get_path_name(), "class": abp.get_class().get_name(),
        "parent_class": (pcls.get_name() if pcls else None), "generated_class": (gcls.get_name() if gcls else None),
        "target_skeleton": (tsk.get_path_name() if tsk else None),
        "anim_graph_count": len(graphs or []), "anim_graphs": [g.get_name() for g in (graphs or [])],
        "member_variable_count": len(varnames), "member_variables": varnames[:100],
        "note": "AnimGraph NODE contents (state machines/states/transitions) are not enumerable via stock Python (get_nodes_of_class reads node objects but graph authoring is C++-only)."}))
'''

    @mcp.tool()
    def get_anim_blueprint_info(ctx, blueprint_path: str) -> str:
        """Read an AnimBlueprint: parent/generated class, target skeleton, graphs, variables. Read-only.

        blueprint_path: object path of an AnimBlueprint.

        Returns parent_class, generated_class, target_skeleton, anim_graphs (names), and
        member_variables. (Individual AnimGraph NODE authoring/introspection is not reachable from
        Python -- see note in the payload and the module docstring.)"""
        try:
            return json.dumps(_exec(_GET_ABP_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_GRAPHS_BODY = _HELPERS + r'''
abp, err = _load(PARAMS["blueprint_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(abp, unreal.AnimBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimBlueprint (got %s)" % abp.get_class().get_name()}))
else:
    rows = []
    for g in (abp.get_animation_graphs() if hasattr(abp, "get_animation_graphs") else []) or []:
        nodes = []
        try:
            nodes = g.get_nodes() if hasattr(g, "get_nodes") else []
        except Exception:
            nodes = []
        rows.append({"name": g.get_name(), "class": g.get_class().get_name(), "node_count": len(nodes or [])})
    # also list non-anim function graphs if available
    print("@@UMCP@@" + json.dumps({"status": "success", "path": abp.get_path_name(),
        "anim_graph_count": len(rows), "anim_graphs": rows}))
'''

    @mcp.tool()
    def list_anim_graphs(ctx, blueprint_path: str) -> str:
        """List the animation graphs of an AnimBlueprint (name, class, node count). Read-only.

        blueprint_path: object path of an AnimBlueprint.

        Returns anim_graphs[{name, class, node_count}] from get_animation_graphs(). (Editing these
        graphs' nodes is not reachable from Python -- see the module docstring.)"""
        try:
            return json.dumps(_exec(_LIST_GRAPHS_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _VALIDATE_ABP_BODY = _HELPERS + r'''
abp, err = _load(PARAMS["blueprint_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(abp, unreal.AnimBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimBlueprint (got %s)" % abp.get_class().get_name()}))
else:
    BEL = getattr(unreal, "BlueprintEditorLibrary", None)
    if BEL is None or not hasattr(BEL, "compile_blueprint"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "BlueprintEditorLibrary.compile_blueprint unavailable in this build"}))
    else:
        BEL.compile_blueprint(abp)
        status = None
        try:
            status = str(abp.get_editor_property("status"))
        except Exception:
            status = None
        upd = None
        try:
            upd = str(unreal.BlueprintStatus.BS_UP_TO_DATE)
        except Exception:
            upd = None
        ok = (status is not None and "UP_TO_DATE" in status) or (status is not None and "3" in status)
        print("@@UMCP@@" + json.dumps({"status": "success", "path": abp.get_path_name(),
            "compiled": True, "blueprint_status": status, "up_to_date": bool(ok),
            "note": "compile_blueprint recompiles in place; it does not alter blueprint source (no ledger entry)."}))
'''

    @mcp.tool()
    def validate_anim_blueprint(ctx, blueprint_path: str) -> str:
        """Compile an AnimBlueprint and report its status. Read-only-ish (recompile, no ledger).

        blueprint_path: object path of an AnimBlueprint.

        Uses BlueprintEditorLibrary.compile_blueprint then reads the blueprint 'status'. Returns
        blueprint_status and up_to_date. Compiling recompiles in place and does not change source, so no
        ledger entry is pushed."""
        try:
            return json.dumps(_exec(_VALIDATE_ABP_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # Notify: remove, list classes, create class                         #
    # ================================================================== #
    _REMOVE_NOTIFY_BODY = _HELPERS + r'''
anim, err = _load(PARAMS["anim_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(anim, unreal.AnimSequenceBase):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not an AnimSequence/AnimMontage (got %s)" % anim.get_class().get_name()}))
else:
    track = PARAMS["track_name"]
    tracks = [str(t) for t in (AL.get_animation_notify_track_names(anim) or [])]
    if track not in tracks:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such notify track: %s (existing: %s)" % (track, tracks)}))
    else:
        prior_events = _capture_track_events(anim, track)
        index = int(PARAMS["index"])
        if index < 0 or index >= len(prior_events):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "notify index %d out of range on track '%s' (0..%d)" % (index, track, len(prior_events) - 1)}))
        else:
            removed = prior_events[index]
            keep = [e for i, e in enumerate(prior_events) if i != index]
            with unreal.ScopedEditorTransaction("MCP remove_anim_notify"):
                AL.remove_animation_notify_events_by_track(anim, track)
                for pe in keep:
                    cp = pe.get("class_path"); cls = None
                    if cp:
                        try: cls = unreal.load_object(None, cp)
                        except Exception: cls = None
                    if cls is None:
                        continue
                    if pe.get("kind") == "state":
                        AL.add_animation_notify_state_event(anim, track, float(pe.get("time") or 0.0), float(pe.get("duration") or 0.0), cls)
                    else:
                        AL.add_animation_notify_event(anim, track, float(pe.get("time") or 0.0), cls)
            _save(PARAMS["anim_path"])
            _ledger().append({"op": "remove_anim_notify", "asset_path": PARAMS["anim_path"], "track_name": track, "prior_events": prior_events})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": PARAMS["anim_path"], "track_name": track,
                "removed_index": index, "removed": removed, "events_on_track_after": _capture_track_events(anim, track),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_anim_notify(ctx, anim_path: str, track_name: str, index: int) -> str:
        """Remove ONE notify from a track by index (rebuilds the track without it). Ledgered write.

        anim_path:  object path of an AnimSequence or AnimMontage.
        track_name: the notify track to edit.
        index:      index of the notify to remove within that track (see get_anim_sequence_info /
                    the events_on_track list; index is the trigger-order position captured here).

        There is no per-event removal API, so this snapshots the track, clears it, and re-adds every
        event except the one at index (class/time/duration preserved; custom per-notify property VALUES
        reset to class defaults -- documented caveat).

        Ledgered op 'remove_anim_notify' {asset_path, track_name, prior_events}; inverse (fold into
        editor_level.undo): clear the track and rebuild ALL prior_events (restores the removed notify).
        FAITHFUL for structure/time/class."""
        params = {"anim_path": anim_path, "track_name": track_name, "index": index}
        try:
            return json.dumps(_exec(_REMOVE_NOTIFY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_NOTIFY_CLASSES_BODY = _HELPERS + r'''
kind = (PARAMS.get("kind") or "all").lower()
filt = (PARAMS.get("filter") or "").lower()
max_results = int(PARAMS.get("max_results") or 200)
want_notify = kind in ("all", "notify")
want_state = kind in ("all", "state", "notifystate")
native = []
for nm in dir(unreal):
    if not nm.startswith("AnimNotify"):
        continue
    if nm in ("AnimNotify", "AnimNotifyState", "AnimNotifyEvent", "AnimNotifyEventReference"):
        continue
    c = getattr(unreal, nm, None)
    if not isinstance(c, type):
        continue
    is_state = False
    try:
        is_state = issubclass(c, unreal.AnimNotifyState)
    except Exception:
        is_state = nm.startswith("AnimNotifyState")
    is_notify = False
    try:
        is_notify = issubclass(c, unreal.AnimNotify)
    except Exception:
        is_notify = nm.startswith("AnimNotify_")
    if is_state and not want_state:
        continue
    if (not is_state) and is_notify and not want_notify:
        continue
    if not (is_state or is_notify):
        continue
    if filt and filt not in nm.lower():
        continue
    cpath = None
    try:
        cpath = unreal.get_default_object(c).get_class().get_path_name()
    except Exception:
        cpath = None
    native.append({"name": nm, "kind": ("state" if is_state else "notify"), "source": "native", "class_path": cpath})
# Blueprint notify assets via AssetRegistry (Blueprint whose parent is AnimNotify/AnimNotifyState)
bp_rows = []
try:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    flt = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")], recursive_paths=True, package_paths=["/Game"])
    for a in (ar.get_assets(flt) or []):
        tags = a.get_tag_value("ParentClass") if hasattr(a, "get_tag_value") else None
        pc = str(tags or "")
        if "AnimNotify" not in pc:
            continue
        nm = str(a.get_editor_property("asset_name"))
        is_state = "AnimNotifyState" in pc
        if is_state and not want_state:
            continue
        if (not is_state) and not want_notify:
            continue
        if filt and filt not in nm.lower():
            continue
        bp_rows.append({"name": nm, "kind": ("state" if is_state else "notify"), "source": "blueprint",
                        "class_path": str(a.get_editor_property("package_name")) + "." + nm + "_C"})
except Exception:
    pass
native.sort(key=lambda r: r["name"].lower())
bp_rows.sort(key=lambda r: r["name"].lower())
allrows = (native + bp_rows)[:max_results]
print("@@UMCP@@" + json.dumps({"status": "success", "kind": kind, "filter": PARAMS.get("filter"),
    "native_count": len(native), "blueprint_count": len(bp_rows), "returned": len(allrows), "classes": allrows}))
'''

    @mcp.tool()
    def list_anim_notify_classes(ctx, kind: str = "all", filter: str = None, max_results: int = 200) -> str:
        """Enumerate AnimNotify / AnimNotifyState classes (native + Blueprint). Read-only.

        kind:        'all' (default), 'notify' (AnimNotify point notifies), or 'state'
                     (AnimNotifyState duration notifies).
        filter:      case-insensitive substring on the class name.
        max_results: cap the returned list (default 200).

        Native classes come from the unreal module namespace (issubclass check against AnimNotify /
        AnimNotifyState); Blueprint notifies come from the AssetRegistry (Blueprints whose ParentClass
        tag references AnimNotify*). Each row: {name, kind, source, class_path}. The class_path/short
        name is what add_anim_notify's notify_class accepts."""
        params = {"kind": kind, "filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_NOTIFY_CLASSES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _CREATE_NOTIFY_CLASS_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/_scratch_g6").rstrip("/")
asset_path = package_path + "/" + name
kind = (PARAMS.get("kind") or "notify").lower()
parent = unreal.AnimNotifyState if kind in ("state", "notifystate") else unreal.AnimNotify
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    fac = unreal.BlueprintFactory(); fac.set_editor_property("parent_class", parent)
    bp = AT.create_asset(name, package_path, None, fac)
    if bp is None or not isinstance(bp, unreal.Blueprint):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned %s for %s" % (type(bp).__name__, asset_path)}))
    else:
        _close_editors(bp)
        _save(asset_path)
        _ledger().append({"op": "create_asset", "asset_path": asset_path, "package_path": package_path, "created_dir": created_dir})
        pcls = bp.get_blueprint_parent_class()
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "object_path": bp.get_path_name(),
            "class": bp.get_class().get_name(), "kind": ("state" if parent is unreal.AnimNotifyState else "notify"),
            "parent_class": (pcls.get_name() if pcls else None), "generated_class_path": asset_path + "_C",
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_anim_notify_class(ctx, name: str, kind: str = "notify",
                                 package_path: str = "/Game/_scratch_g6") -> str:
        """Create a Blueprint subclass of AnimNotify or AnimNotifyState non-interactively. Ledgered write.

        name:         asset name (e.g. 'ANS_Footstep' or 'AN_Footstep').
        kind:         'notify' (subclass AnimNotify -- point notify) or 'state' (subclass
                      AnimNotifyState -- duration notify). Default 'notify'.
        package_path: content directory (default '/Game/_scratch_g6').

        Uses BlueprintFactory (parent_class = AnimNotify / AnimNotifyState -> no dialog). The generated
        class path ('<asset>_C') is usable as add_anim_notify's notify_class.

        Ledgered op 'create_asset' {asset_path, package_path, created_dir}; inverse (already in
        editor_level.undo): close editors + delete the asset."""
        params = {"name": name, "kind": kind, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_NOTIFY_CLASS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_anim_asset — generic anim-asset integrity check (read)    #
    # ================================================================== #
    _VALIDATE_ASSET_BODY = _HELPERS + r'''
obj, err = _load(PARAMS["asset_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls = obj.get_class().get_name()
    issues = []
    info = {"status": "success", "path": obj.get_path_name(), "class": cls}
    skel = None
    if isinstance(obj, unreal.AnimSequenceBase):
        info["asset_kind"] = "montage" if isinstance(obj, unreal.AnimMontage) else "anim_sequence"
        try: skel = obj.get_editor_property("skeleton")
        except Exception: skel = None
        if skel is None:
            issues.append("no target skeleton set")
        if isinstance(obj, unreal.AnimMontage):
            nslots = len(obj.get_editor_property("slot_anim_tracks") or [])
            if nslots == 0:
                issues.append("montage has no slot tracks")
            nseg = 0
            for t in (obj.get_editor_property("slot_anim_tracks") or []):
                atk = t.get_editor_property("anim_track")
                for s in (atk.get_editor_property("anim_segments") or []):
                    nseg += 1
                    if s.get_editor_property("anim_reference") is None:
                        issues.append("slot '%s' has a segment with no animation" % str(t.get_editor_property("slot_name")))
            info["slot_count"] = nslots; info["segment_count"] = nseg
            if float(obj.get_editor_property("sequence_length")) <= 0.0 and nseg > 0:
                issues.append("sequence_length is 0 but segments exist")
        else:
            info["length_seconds"] = round(float(AL.get_sequence_length(obj)), 6)
            if info["length_seconds"] <= 0.0:
                issues.append("sequence length is 0")
    elif isinstance(obj, unreal.BlendSpace):
        info["asset_kind"] = "blend_space"
        skel = obj.get_skeleton()
        if skel is None:
            issues.append("no skeleton set")
        samples = obj.get_editor_property("sample_data") or []
        info["sample_count"] = len(samples)
        if len(samples) == 0:
            issues.append("blend space has no samples")
        for i, s in enumerate(samples):
            if s.get_editor_property("animation") is None:
                issues.append("sample %d has no animation" % i)
    elif isinstance(obj, unreal.AnimBlueprint):
        info["asset_kind"] = "anim_blueprint"
        try: skel = obj.get_editor_property("target_skeleton")
        except Exception: skel = None
        if skel is None:
            issues.append("no target skeleton set")
        info["anim_graph_count"] = len(obj.get_animation_graphs() or []) if hasattr(obj, "get_animation_graphs") else None
    else:
        info["asset_kind"] = "unknown"
        issues.append("not a recognized animation asset (AnimSequence/AnimMontage/BlendSpace/AnimBlueprint)")
    info["skeleton"] = (skel.get_path_name() if skel is not None else None)
    info["valid"] = (len(issues) == 0)
    info["issue_count"] = len(issues)
    info["issues"] = issues
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def validate_anim_asset(ctx, asset_path: str) -> str:
        """Validate an animation asset's basic integrity. Read-only (no ledger).

        asset_path: object path of an AnimSequence, AnimMontage, BlendSpace, or AnimBlueprint.

        Type-detects the asset and checks: a target skeleton is set; montages have slot tracks and every
        segment references an animation and sequence_length is consistent; blend spaces have samples each
        with an animation; anim sequences have non-zero length; anim blueprints have a skeleton + graphs.
        Returns {asset_kind, skeleton, valid, issues[]}."""
        try:
            return json.dumps(_exec(_VALIDATE_ASSET_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The 6 NEW op inverses
    # (set_blend_space_axis, add_blend_space_sample, remove_blend_space_sample, add_montage_slot,
    # add_montage_segment, remove_anim_notify) are reported to the coordinator via
    # .mcp_coord/coordinator-inbox/g6_anim_undo_ops.md to fold into editor_level.undo. Creation tools push
    # the generic 'create_asset' op, already handled by editor_level.undo.
