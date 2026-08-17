"""UserTools :: Control Rig (READ)  (spec: docs/spec/controlrig.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). READ-ONLY batch.
NO asset editors are ever opened (no modals); nothing is mutated; no ledger. This introspects
UE5 Control Rig assets (procedural rigging / IK / locomotion): the rig HIERARCHY (URigHierarchy:
bones / controls / nulls / curves / sockets, each with name / type / parent / transform / — for
controls — shape + control settings) and, secondarily, the RigVM GRAPH (the solve graph: unit
nodes, their script-struct types, positions, and pins). The query convention, base64 PARAMS, and
Output-Log auto-capture are copied verbatim from editor_level.py (the gold standard), matching
statetree.py / eqs.py / ai_read.py's AssetRegistry-enumeration style.

PROBE FINDINGS (verified live vs TestMCPSetup, UE 5.8.1 -- what IS / ISN'T reflectable):
  * The ControlRig plugin is ENABLED (unreal.ControlRig / ControlRigBlueprint / RigHierarchy /
    RigVMGraph all exist). The PROJECT ships 3 real Control Rigs, all under
    /Game/Characters/Mannequins/Rigs: CR_Mannequin_Body (524 elements), CR_Mannequin_Procedural
    (176 VM nodes), CR_Mannequin_FootIK. The plugin content adds engine sample rigs under /ControlRig
    and /ControlRigSpline (module Root rigs + StandardFunctionLibrary / SplineFunctionLibrary).
  * Assets are UControlRigBlueprint (a Blueprint variant; module /Script/ControlRigDeveloper).
    Enumerated via AssetRegistry TopLevelAssetPath('/Script/ControlRigDeveloper','ControlRigBlueprint')
    with recursive_classes=True (covers the modular-module ControlRigBlueprint subclass too).
  * UNLIKE many plugin assets (EQS / Niagara), the Control Rig authorable data is FULLY value-readable
    through PUBLIC Python bindings -- NOTHING here is faked or metadata-only:
      - bp.get_hierarchy() -> URigHierarchy (VALUE-readable): num() == 524; get_all_keys(True) ->
        TArray<FRigElementKey> (each key.name + key.type, e.g. RigElementType.BONE/CONTROL/NULL/
        CURVE/SOCKET/CONNECTOR/REFERENCE). Per element: get_first_parent(key) / get_number_of_parents,
        get_global_transform(key) / get_local_transform(key) (-> FTransform .translation/.rotation/
        .scale3d), get_bone_type(key) (IMPORTED / USER), get_curve_value(key).
      - For CONTROL elements: get_control_settings(key) -> FRigControlSettings, whose fields read back
        real (control_type e.g. EULER_TRANSFORM/FLOAT/BOOL/POSITION/ROTATOR/SCALE/VECTOR2D/INTEGER,
        animation_type, shape_name, shape_visible, shape_color (FLinearColor), primary_axis,
        preferred_rotation_order, display_name, draw_limits, limit_enabled). Also
        get_control_preferred_euler_rotation_order(key).
      - bp.get_preview_mesh() -> the SkeletalMesh the rig previews on (real path).
      - bp.get_all_models() / get_default_model() -> TArray<URigVMGraph> (the solve graphs; the Body/
        Procedural rig has 2 models). Each: get_graph_name(), get_nodes() (176), get_local_variables().
        Each node (RigVMUnitNode / RigVMCommentNode / RigVMRerouteNode / ...): get_node_path(),
        get_node_title(), get_script_struct() (unit nodes -> e.g. RigUnit_BeginExecution),
        get_position(), get_pins() -> pins w/ get_name / get_direction / get_cpp_type /
        get_default_value / is_execute_context.
      - bp.get_available_rig_vm_structs() -> 852 authorable RigVM node script-struct TYPES (the full
        node palette, rig-independent) -- the RigVM node-class listing.
  * KNOWN LIMITS (reported in payloads, never hidden):
      - This reads the AUTHORABLE rig (hierarchy + RigVM model), which is the source-of-truth data.
        The COMPILED/baked runtime (URigVM bytecode, the generated UControlRig class instance state)
        is not executed/dumped here -- reading it would need evaluating the rig (a C++ runtime probe;
        DEFERRED-C++, same bucket as the profile/simulate spec items).
      - Native RigVM node struct TYPES are FScriptStructs (get_available_rig_vm_structs lists them by
        name); their full pin/arg schema is surfaced per-USE via get_control_rig_vm_graph (real pins),
        not as a standalone struct-schema dump (a per-struct pin-schema reader is DEFERRED-C++).
      - Property bindings between RigVM pins / hierarchy are present in the graph but not deeply
        resolved into a binding table here (a dedicated binding reader is DEFERRED).

Implemented (all read-only, no ledger):
  - list_control_rigs               (AssetRegistry enumerate ControlRigBlueprint assets)
  - get_control_rig_info            (class + preview mesh + modular flag + element counts by type + VM model/node counts)
  - get_control_rig_hierarchy       (rig elements: name/type/parent/transform; controls incl. control_type + shape)
  - get_control_rig_control         (one control: full FRigControlSettings + shape + preferred rotation order)
  - get_control_rig_vm_graph        (a solve graph's nodes: path/title/class/script_struct/position/pins)
  - list_control_rig_node_types     (authorable RigVM node script-struct TYPES; the node palette)
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.
# (This module parses enums/structs with pure str ops -- no regex -- to avoid backslashes.)


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _CR_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load(path):
    if not path:
        return None, "no asset path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    if not isinstance(obj, unreal.ControlRigBlueprint):
        return None, "asset is not a ControlRigBlueprint (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _enum_short(v):
    # unreal enum -> short name ("BONE" from RigElementType.BONE), robust fallback parse.
    if v is None:
        return None
    n = getattr(v, "name", None)
    if isinstance(n, str) and n:
        return n
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".", 1)[1].split(":", 1)[0].strip()
    return s
def _is_obj(v):
    return v is not None and not isinstance(v, str)
def _color(c):
    if c is None:
        return None
    return [round(float(c.r), 4), round(float(c.g), 4), round(float(c.b), 4), round(float(c.a), 4)]
def _xf(t, want_rot=False, want_scale=False):
    # FTransform -> compact dict (translation always; rotation/scale optional).
    if not _is_obj(t):
        return None
    tr = _try(lambda: t.translation)
    rec = {}
    if _is_obj(tr):
        rec["location"] = [round(tr.x, 3), round(tr.y, 3), round(tr.z, 3)]
    if want_rot:
        rot = _try(lambda: t.rotation.rotator())
        if _is_obj(rot):
            rec["rotation"] = [round(rot.pitch, 3), round(rot.yaw, 3), round(rot.roll, 3)]
    if want_scale:
        sc = _try(lambda: t.scale3d)
        if _is_obj(sc):
            rec["scale"] = [round(sc.x, 3), round(sc.y, 3), round(sc.z, 3)]
    return rec
def _key_name(k):
    return str(k.name) if k is not None else None
def _valid_key(k):
    # A "no parent" comes back as None or an invalid key (type NONE). Normalize.
    if k is None:
        return False
    return _enum_short(_try(lambda: k.type)) not in (None, "NONE")
def _parent_name(h, k):
    par = _try(lambda: h.get_first_parent(k))
    if _valid_key(par):
        return _key_name(par)
    return None
def _control_summary(h, k):
    # Compact per-CONTROL info for hierarchy listings.
    cs = _try(lambda: h.get_control_settings(k))
    if not _is_obj(cs):
        return None
    rec = {"control_type": _enum_short(_try(lambda: cs.get_editor_property("control_type"))),
           "animation_type": _enum_short(_try(lambda: cs.get_editor_property("animation_type"))),
           "shape_name": str(_try(lambda: cs.get_editor_property("shape_name")))}
    vis = _try(lambda: cs.get_editor_property("shape_visible"))
    if vis is not None:
        rec["shape_visible"] = bool(vis)
    return rec
def _control_full(h, k):
    cs = _try(lambda: h.get_control_settings(k))
    if not _is_obj(cs):
        return None
    dn = _try(lambda: cs.get_editor_property("display_name"))
    rec = {
        "control_type": _enum_short(_try(lambda: cs.get_editor_property("control_type"))),
        "animation_type": _enum_short(_try(lambda: cs.get_editor_property("animation_type"))),
        "shape_name": str(_try(lambda: cs.get_editor_property("shape_name"))),
        "shape_color": _color(_try(lambda: cs.get_editor_property("shape_color"))),
        "shape_visible": bool(_try(lambda: cs.get_editor_property("shape_visible"))),
        "shape_enabled": bool(_try(lambda: cs.get_editor_property("shape_enabled"))),
        "primary_axis": _enum_short(_try(lambda: cs.get_editor_property("primary_axis"))),
        "display_name": (None if str(dn) == "None" else str(dn)),
        "draw_limits": bool(_try(lambda: cs.get_editor_property("draw_limits"))),
        "preferred_rotation_order": _enum_short(_try(lambda: h.get_control_preferred_euler_rotation_order(k))),
    }
    return rec
def _first_cr_path():
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/ControlRigDeveloper", "ControlRigBlueprint")],
        recursive_paths=True, recursive_classes=True, package_paths=["/Game", "/ControlRig"])
    for a in (ar.get_assets(flt) or []):
        pkg = str(a.get_editor_property("package_name"))
        nm = str(a.get_editor_property("asset_name"))
        return pkg + "." + nm
    return None
'''

    # ------------------------------------------------------------------ #
    # list_control_rigs — AssetRegistry enumerate ControlRigBlueprint      #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _CR_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
include_plugin = bool(PARAMS.get("include_plugin", False))
max_results = int(PARAMS.get("max_results") or 200)
roots = [package_path]
if include_plugin and package_path == "/Game":
    roots = ["/Game", "/ControlRig", "/ControlRigSpline"]
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
err = None
try:
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/ControlRigDeveloper", "ControlRigBlueprint")],
        recursive_paths=True, recursive_classes=True, package_paths=roots)
    for a in (ar.get_assets(flt) or []):
        pkg = str(a.get_editor_property("package_name"))
        nm = str(a.get_editor_property("asset_name"))
        cls = str(_try(lambda a=a: a.get_editor_property("asset_class_path").asset_name)) or "ControlRigBlueprint"
        rows.append({"name": nm, "path": pkg + "." + nm, "asset_class": cls})
except Exception as e:
    err = str(e)
rows.sort(key=lambda r: r["path"].lower())
result = {"status": ("error" if err else "success"), "roots": roots,
          "count": len(rows), "returned": min(len(rows), max_results),
          "assets": rows[:max_results]}
if err:
    result["error"] = err
result["note"] = ("ControlRigBlueprint is a /Script/ControlRigDeveloper class (a Blueprint variant); "
                  "enumerated via AssetRegistry TopLevelAssetPath('/Script/ControlRigDeveloper',"
                  "'ControlRigBlueprint') with recursive_classes=True. Pass include_plugin=True to also "
                  "scan the engine sample rigs under /ControlRig + /ControlRigSpline. Follow up with "
                  "get_control_rig_info / get_control_rig_hierarchy on a path.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_control_rigs(ctx, path: str = None, include_plugin: bool = False,
                          max_results: int = 200) -> str:
        """Enumerate Control Rig assets (UControlRigBlueprint) via the AssetRegistry. Read-only.

        path:           content root to scan (default '/Game'); e.g. '/Game/Characters'.
        include_plugin: when scanning '/Game', also include the engine sample rigs under
                        /ControlRig + /ControlRigSpline (default False).
        max_results:    cap the assets listed (default 200; true 'count' is always reported).

        Returns {count, returned, assets:[{name, path, asset_class}]}. ControlRigBlueprint is a
        Blueprint variant. Follow up with get_control_rig_info(asset_path) for an overview, or
        get_control_rig_hierarchy(asset_path) for the bone/control/null element tree."""
        params = {"path": path, "include_plugin": include_plugin, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_control_rig_info — overview: mesh + counts + VM summary          #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
bp, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    info = {"status": "success", "path": bp.get_path_name(), "class": bp.get_class().get_name()}
    pm = _try(lambda: bp.get_preview_mesh())
    info["preview_mesh"] = pm.get_path_name() if _is_obj(pm) else None
    # Modular rig detection (the modular_rig_model holds modules for a modular CR).
    mrm = _try(lambda: bp.modular_rig_model)
    n_modules = 0
    if _is_obj(mrm):
        mods = _try(lambda: mrm.get_modules())
        if _is_obj(mods):
            n_modules = len(mods)
    info["is_modular"] = n_modules > 0
    info["module_count"] = n_modules
    # Hierarchy summary: element counts by type.
    h = _try(lambda: bp.get_hierarchy())
    if _is_obj(h):
        keys = _try(lambda: h.get_all_keys(True)) or []
        counts = {}
        for k in keys:
            t = _enum_short(_try(lambda k=k: k.type)) or "?"
            counts[t] = counts.get(t, 0) + 1
        info["element_count"] = len(keys)
        info["elements_by_type"] = counts
        roots = _try(lambda: h.get_root_elements()) or []
        info["root_elements"] = [_key_name(k) for k in roots]
    else:
        info["element_count"] = None
        info["note_hierarchy"] = "URigHierarchy unreachable for this asset."
    # RigVM model summary.
    models = _try(lambda: bp.get_all_models()) or []
    msum = []
    for g in models:
        nm = str(_try(lambda g=g: g.get_graph_name()))
        nodes = _try(lambda g=g: g.get_nodes()) or []
        msum.append({"graph_name": nm, "node_count": len(nodes)})
    info["vm_models"] = msum
    info["vm_model_count"] = len(msum)
    avs = _try(lambda: bp.get_available_rig_vm_structs())
    info["available_node_type_count"] = len(avs) if _is_obj(avs) else None
    info["reflection_note"] = (
        "All values read from PUBLIC Python bindings on UControlRigBlueprint (get_hierarchy / "
        "get_preview_mesh / get_all_models / get_available_rig_vm_structs) -- authorable rig data, "
        "not faked. The COMPILED runtime (URigVM bytecode / evaluated pose) is not executed here.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_control_rig_info(ctx, asset_path: str) -> str:
        """Overview of one Control Rig: preview mesh, modular flag, element counts by type, and the
        RigVM solve-graph summary. Read-only.

        asset_path: ControlRigBlueprint path, e.g.
                    '/Game/Characters/Mannequins/Rigs/CR_Mannequin_Body.CR_Mannequin_Body'.

        Returns: preview_mesh (SkeletalMesh path); is_modular + module_count; element_count and
        elements_by_type {BONE, CONTROL, NULL, CURVE, SOCKET, ...}; root_elements[]; vm_models
        [{graph_name, node_count}]; available_node_type_count (the RigVM node palette size).

        Use get_control_rig_hierarchy for the element tree, get_control_rig_vm_graph for graph nodes."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_control_rig_hierarchy — rig elements (bones/controls/nulls/...)  #
    # ------------------------------------------------------------------ #
    _HIER_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
type_filter = (PARAMS.get("element_type") or "all").strip().lower()
include_tx = bool(PARAMS.get("include_transforms", False))
max_results = int(PARAMS.get("max_results") or 2000)
bp, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = _try(lambda: bp.get_hierarchy())
    if not _is_obj(h):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "URigHierarchy unreachable for %s" % path}))
    else:
        keys = _try(lambda: h.get_all_keys(True)) or []
        rows = []
        truncated = False
        total_match = 0
        for k in keys:
            t = _enum_short(_try(lambda k=k: k.type)) or "?"
            if type_filter not in ("all", "") and t.lower() != type_filter:
                continue
            total_match += 1
            if len(rows) >= max_results:
                truncated = True
                continue
            rec = {"name": _key_name(k), "type": t, "parent": _parent_name(h, k)}
            npar = _try(lambda k=k: h.get_number_of_parents(k))
            if isinstance(npar, int) and npar > 1:
                rec["num_parents"] = npar
            if t == "BONE":
                rec["bone_type"] = _enum_short(_try(lambda k=k: h.get_bone_type(k)))
            elif t == "CONTROL":
                cs = _control_summary(h, k)
                if cs:
                    rec["control"] = cs
            elif t == "CURVE":
                cv = _try(lambda k=k: h.get_curve_value(k))
                if cv is not None:
                    rec["curve_value"] = round(float(cv), 5)
            if include_tx:
                gt = _try(lambda k=k: h.get_global_transform(k))
                lt = _try(lambda k=k: h.get_local_transform(k))
                rec["global_transform"] = _xf(gt, want_rot=True, want_scale=True)
                rec["local_transform"] = _xf(lt, want_rot=True, want_scale=True)
            rows.append(rec)
        result = {"status": "success", "path": bp.get_path_name(),
                  "element_type": type_filter, "match_count": total_match,
                  "returned": len(rows), "truncated": truncated, "elements": rows}
        result["note"] = (
            "Rig elements from URigHierarchy.get_all_keys(traverse=True) (depth-first). Per element: "
            "name, type (BONE/CONTROL/NULL/CURVE/SOCKET/CONNECTOR/REFERENCE), first parent, and "
            "num_parents when a control has multiple (space switching). BONE -> bone_type (IMPORTED/"
            "USER); CONTROL -> control{control_type, animation_type, shape_name, shape_visible}; CURVE "
            "-> curve_value. include_transforms=True adds global_transform + local_transform "
            "(location/rotation/scale). All values are real (public RigHierarchy getters). For one "
            "control's FULL settings use get_control_rig_control.")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_control_rig_hierarchy(ctx, asset_path: str, element_type: str = "all",
                                  include_transforms: bool = False, max_results: int = 2000) -> str:
        """The Control Rig's element hierarchy (bones / controls / nulls / curves / sockets). Read-only.

        asset_path:         ControlRigBlueprint path (e.g. '.../CR_Mannequin_Body.CR_Mannequin_Body').
        element_type:       filter to one type: 'bone' | 'control' | 'null' | 'curve' | 'socket' |
                            'connector' | 'reference' | 'all' (default 'all').
        include_transforms: add each element's global + local transform (location/rotation/scale).
                            Off by default (large rigs have 500+ elements).
        max_results:        cap elements returned (default 2000; 'truncated' + 'match_count' report).

        Each element: {name, type, parent, num_parents?, and by type: bone_type | control{control_type,
        animation_type, shape_name, shape_visible} | curve_value}. Values come from the public
        URigHierarchy getters (get_all_keys / get_first_parent / get_control_settings / get_*_transform)
        -- not faked. For a single control's full FRigControlSettings, use get_control_rig_control."""
        params = {"asset_path": asset_path, "element_type": element_type,
                  "include_transforms": include_transforms, "max_results": max_results}
        try:
            return json.dumps(_exec(_HIER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_control_rig_control — one control's full settings + transforms   #
    # ------------------------------------------------------------------ #
    _CONTROL_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
want = (PARAMS.get("control_name") or "").strip()
want_lc = want.lower()
bp, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = _try(lambda: bp.get_hierarchy())
    if not _is_obj(h):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "URigHierarchy unreachable for %s" % path}))
    else:
        keys = _try(lambda: h.get_all_keys(True)) or []
        controls = [k for k in keys if _enum_short(_try(lambda k=k: k.type)) == "CONTROL"]
        match = None
        for k in controls:
            if _key_name(k).lower() == want_lc:
                match = k
                break
        if match is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no CONTROL named %r in %s" % (want, path),
                "available_controls": [_key_name(k) for k in controls][:200]}))
        else:
            k = match
            info = {"status": "success", "path": bp.get_path_name(),
                    "name": _key_name(k), "type": "CONTROL",
                    "parent": _parent_name(h, k)}
            npar = _try(lambda: h.get_number_of_parents(k))
            if isinstance(npar, int):
                info["num_parents"] = npar
            settings = _control_full(h, k)
            if settings:
                info["settings"] = settings
            # Transforms: initial/current global + local + control offset + shape transform.
            info["global_transform"] = _xf(_try(lambda: h.get_global_transform(k)), True, True)
            info["local_transform"] = _xf(_try(lambda: h.get_local_transform(k)), True, True)
            info["offset_transform"] = _xf(_try(lambda: h.get_global_control_offset_transform(k, True)), True, True)
            info["shape_transform"] = _xf(_try(lambda: h.get_global_control_shape_transform(k, True)), True, True)
            info["note"] = (
                "Full FRigControlSettings for one control (control_type, animation_type, shape_name, "
                "shape_color[r,g,b,a], shape_visible/enabled, primary_axis, display_name, draw_limits, "
                "preferred_rotation_order) plus global/local/offset/shape transforms. All from public "
                "URigHierarchy getters -- real values, not faked.")
            print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_control_rig_control(ctx, asset_path: str, control_name: str) -> str:
        """Full settings for ONE Control Rig control: FRigControlSettings + transforms. Read-only.

        asset_path:   ControlRigBlueprint path.
        control_name: the control element Name (case-insensitive; an error lists available_controls
                      when nothing matches).

        Returns settings{control_type, animation_type, shape_name, shape_color[r,g,b,a], shape_visible,
        shape_enabled, primary_axis, display_name, draw_limits, preferred_rotation_order} plus
        global_transform, local_transform, offset_transform, shape_transform (each location/rotation/
        scale). Values from the public URigHierarchy control getters -- real, not faked."""
        if not control_name:
            return "Error: provide control_name."
        params = {"asset_path": asset_path, "control_name": control_name}
        try:
            return json.dumps(_exec(_CONTROL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_control_rig_vm_graph — a solve graph's nodes (RigVM)             #
    # ------------------------------------------------------------------ #
    _VM_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
graph_name = PARAMS.get("graph_name")
include_pins = bool(PARAMS.get("include_pins", False))
max_nodes = int(PARAMS.get("max_nodes") or 500)
bp, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    models = _try(lambda: bp.get_all_models()) or []
    if not models:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no RigVM models on %s" % path}))
    else:
        names = [str(_try(lambda g=g: g.get_graph_name())) for g in models]
        g = None
        if graph_name:
            for gg, nm in zip(models, names):
                if nm.lower() == graph_name.strip().lower():
                    g = gg
                    break
            if g is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "no RigVM graph named %r" % graph_name, "available_graphs": names}))
        if graph_name and g is None:
            pass
        else:
            if g is None:
                g = _try(lambda: bp.get_default_model()) or models[0]
            nodes = _try(lambda: g.get_nodes()) or []
            rows = []
            truncated = False
            hist = {}
            for n in nodes:
                cls = type(n).__name__
                hist[cls] = hist.get(cls, 0) + 1
                if len(rows) >= max_nodes:
                    truncated = True
                    continue
                rec = {"path": str(_try(lambda n=n: n.get_node_path())),
                       "title": str(_try(lambda n=n: n.get_node_title())),
                       "node_class": cls}
                ss = _try(lambda n=n: n.get_script_struct())
                if _is_obj(ss):
                    rec["script_struct"] = ss.get_name()
                pos = _try(lambda n=n: n.get_position())
                if _is_obj(pos):
                    rec["position"] = [round(pos.x, 1), round(pos.y, 1)]
                if include_pins:
                    pins = _try(lambda n=n: n.get_pins()) or []
                    prec = []
                    for pin in pins:
                        prec.append({
                            "name": str(_try(lambda pin=pin: pin.get_name())),
                            "direction": _enum_short(_try(lambda pin=pin: pin.get_direction())),
                            "cpp_type": str(_try(lambda pin=pin: pin.get_cpp_type())),
                            "is_execute": bool(_try(lambda pin=pin: pin.is_execute_context())),
                            "default": str(_try(lambda pin=pin: pin.get_default_value())),
                        })
                    rec["pins"] = prec
                rows.append(rec)
            result = {"status": "success", "path": bp.get_path_name(),
                      "graph_name": str(_try(lambda: g.get_graph_name())),
                      "available_graphs": names, "node_count": len(nodes),
                      "returned": len(rows), "truncated": truncated,
                      "node_class_histogram": hist, "nodes": rows}
            result["note"] = (
                "RigVM solve-graph nodes (the authorable rig logic). Per node: path, title, node_class "
                "(RigVMUnitNode / RigVMCommentNode / RigVMRerouteNode / ...), script_struct (unit nodes, "
                "e.g. RigUnit_BeginExecution), position. include_pins=True adds each pin {name, "
                "direction, cpp_type, is_execute, default}. From public RigVMGraph/RigVMNode getters -- "
                "real graph data. Use list_control_rig_node_types for the full authorable node palette.")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_control_rig_vm_graph(ctx, asset_path: str, graph_name: str = None,
                                 include_pins: bool = False, max_nodes: int = 500) -> str:
        """The RigVM solve-graph nodes of a Control Rig (the rig's authorable logic). Read-only.

        asset_path:   ControlRigBlueprint path.
        graph_name:   which model to read (see 'available_graphs'); default = the default model.
        include_pins: add each node's pins {name, direction, cpp_type, is_execute, default}.
                      Off by default (graphs can have 150+ nodes).
        max_nodes:    cap nodes returned (default 500; 'truncated' + 'node_count' report).

        Returns nodes[]: {path, title, node_class, script_struct?, position, pins?} plus a
        node_class_histogram and available_graphs. From public RigVMGraph / RigVMNode / RigVMPin
        getters -- real graph data. For the full authorable node palette, use
        list_control_rig_node_types."""
        params = {"asset_path": asset_path, "graph_name": graph_name,
                  "include_pins": include_pins, "max_nodes": max_nodes}
        try:
            return json.dumps(_exec(_VM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_control_rig_node_types — authorable RigVM node struct palette   #
    # ------------------------------------------------------------------ #
    _NODES_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
flt = (PARAMS.get("filter") or "").strip().lower()
max_results = int(PARAMS.get("max_results") or 300)
if not path:
    path = _first_cr_path()
if not path:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no ControlRigBlueprint found to query the node palette against"}))
else:
    bp, err = _load(path)
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        avs = _try(lambda: bp.get_available_rig_vm_structs()) or []
        names = []
        for s in avs:
            nm = _try(lambda s=s: s.get_name())
            if nm:
                names.append(str(nm))
        names = sorted(set(names), key=lambda x: x.lower())
        if flt:
            names = [n for n in names if flt in n.lower()]
        total = len(names)
        result = {"status": "success", "source_asset": bp.get_path_name(),
                  "filter": flt, "total": total, "returned": min(total, max_results),
                  "node_types": names[:max_results]}
        result["note"] = (
            "Authorable RigVM node script-struct TYPES from bp.get_available_rig_vm_structs() -- the "
            "full node palette (rig-independent; ~852 total). These are FScriptStruct type names (e.g. "
            "RigUnit_* solve units, RigVMFunction_* math). Their per-node pin/arg schema is surfaced "
            "per-USE via get_control_rig_vm_graph(include_pins=True); a standalone per-struct pin-schema "
            "dump would need a C++ handler (DEFERRED-C++). Pass a filter substring to narrow.")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_control_rig_node_types(ctx, asset_path: str = None, filter: str = None,
                                    max_results: int = 300) -> str:
        """List the authorable RigVM node types (the Control Rig node palette). Read-only.

        asset_path:  any ControlRigBlueprint to query against (the palette is rig-independent);
                     if omitted, the first Control Rig found is used.
        filter:      case-insensitive substring to narrow the ~852 node types (e.g. 'ik', 'spring',
                     'bone', 'RigUnit').
        max_results: cap names returned (default 300; true 'total' is always reported).

        Returns node_types[] = FScriptStruct type names (RigUnit_* solve units, RigVMFunction_* math,
        etc.), from the public bp.get_available_rig_vm_structs(). For a specific node's actual pins,
        read a graph that uses it via get_control_rig_vm_graph(include_pins=True)."""
        params = {"asset_path": asset_path, "filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- get_control_rig_vm (C++ #12: compiled RigVM summary via generated-class CDO) ----
    _VMDUMP_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
bp, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not hasattr(unreal, "MCPReflectionLibrary") or not hasattr(unreal.MCPReflectionLibrary, "get_control_rig_vm_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary.get_control_rig_vm_json unavailable — reload the MCP server after the C++ #12 rebuild."}))
else:
    try:
        data = json.loads(unreal.MCPReflectionLibrary.get_control_rig_vm_json(bp))
        data["status"] = "error" if "error" in data else "success"
        data["note"] = ("Compiled RigVM summary from the generated-class CDO (URigVMHost.GetVM) in C++ — "
                        "instruction_count / opcode_histogram / statistics / external_variables. STATIC "
                        "compiled state only; the rig is NOT ticked/posed. Per-node names: get_control_rig_vm_graph.")
        print("@@UMCP@@" + json.dumps(data))
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "get_control_rig_vm_json failed: %s" % _e}))
'''

    @mcp.tool()
    def get_control_rig_vm(ctx, asset_path: str) -> str:
        """Compiled RigVM summary of a Control Rig (C++ #12 reader). Read-only, static (no ticking).

        asset_path: a ControlRigBlueprint path (e.g. '.../CR_Mannequin_FootIK.CR_Mannequin_FootIK').

        Returns vm_present, instruction_count, opcode_histogram{op:count}, statistics{BytesForCDO/
        BytesPerInstance/BytesForCaching...}, external_variables[{name,type,is_array}] — the bytecode
        instruction/opcode data that stock Python get_vm() cannot expose. Requires the C++ #12 plugin
        build; falls back with a clear message otherwise."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_VMDUMP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- get_rig_vm_struct_pins (C++ #12: per-struct RigVM pin schema) ----
    _PINS_BODY = _CR_HELPERS + r'''
name = (PARAMS.get("struct_name") or "").strip()
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide struct_name"}))
elif not hasattr(unreal, "MCPReflectionLibrary") or not hasattr(unreal.MCPReflectionLibrary, "get_rig_vm_struct_pins_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary.get_rig_vm_struct_pins_json unavailable — reload the MCP server after the C++ #12 rebuild."}))
else:
    try:
        data = json.loads(unreal.MCPReflectionLibrary.get_rig_vm_struct_pins_json(name))
        data["status"] = "error" if "error" in data else "success"
        data["note"] = ("Per-struct pin schema for one RigVM node struct (direction from RigVM Input/Output/"
                        "Visible metadata; default_value from the struct default instance). Complements "
                        "list_control_rig_node_types (names only) + get_control_rig_vm_graph (pins per-use).")
        print("@@UMCP@@" + json.dumps(data))
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "get_rig_vm_struct_pins_json failed: %s" % _e}))
'''

    @mcp.tool()
    def get_rig_vm_struct_pins(ctx, struct_name: str) -> str:
        """Pin schema for one RigVM node struct (C++ #12 reader). Read-only.

        struct_name: a RigVM node struct, bare name or full path (e.g. 'RigUnit_SetTransform',
                     'RigVMFunction_MathQuaternionSlerp', '/Script/RigVM.RigVMFunction_MathVectorAdd').

        Returns pins[] each {name, cpp_type, direction (input|output|io|visible|hidden), is_pin,
        is_array, default_value, category?, tooltip?}. Resolve struct names via
        list_control_rig_node_types. Requires the C++ #12 plugin build; falls back otherwise."""
        params = {"struct_name": struct_name}
        try:
            return json.dumps(_exec(_PINS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
