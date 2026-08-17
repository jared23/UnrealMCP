"""UserTools :: Mutable — Customizable Objects (READ)  (spec: docs/spec/mutable.md)

Clean-room reimplementation over Unreal's PUBLIC Python API (UE 5.8.1, Mutable / Customizable
Object plugin ENABLED). READ-ONLY batch. This module NEVER compiles/bakes/generates a
Customizable Object (that is expensive AND mutates the asset), never opens an asset editor
(no modals), never mutates the level, and records NO ledger entries.

The query convention, base64 PARAMS, and Output-Log auto-capture are copied verbatim from
editor_level.py (the gold standard), matching statetree.py / pcg.py's AssetRegistry-enumeration
+ honest-empty style.

PROBE FINDINGS (verified live vs TestMCPSetup, UE 5.8.1 -- what IS / ISN'T reflectable):
  * unreal.CustomizableObject, CustomizableObjectInstance, CustomizableObjectNode,
    CustomizableObjectMacroLibrary, CustomizableObjectSystem all EXIST (plugin enabled).
    CustomizableObject / CustomizableObjectInstance are /Script/CustomizableObject UObject
    assets; the graph node classes are /Script/CustomizableObjectEditor UEdGraphNode subclasses.
  * The PROJECT HAS ZERO CustomizableObject and ZERO CustomizableObjectInstance assets --
    verified via AssetRegistry over "/" AND the plugin roots "/Mutable" and "/CustomizableObject"
    (all empty; this template ships no Mutable sample content). So the object/parameter reads are
    validated against the honest not-found path plus the class-default-object (CDO), and the
    ENUMERATION tools are validated returning an honest empty set. Point get_customizable_object_*
    at a real CustomizableObject asset path in a project that has one to see full parameter data.
  * The CustomizableObject PUBLIC API is fully bound and invokes cleanly (verified on the CDO):
      - Parameters: get_parameter_count(); get_parameter_name(index)->FName;
        get_parameter_type_by_name(name)->unreal.MutableParameterType
        (BOOL/COLOR/FLOAT/INT/INSTANCED_STRUCT/MATERIAL/PROJECTOR/SKELETAL_MESH/TEXTURE/TRANSFORM/NONE);
        contains_parameter(name); is_parameter_multidimensional(name);
        get_parameter_ui_metadata(name)->FMutableParamUIMetadata (object_friendly_name,
        ui_section_name, ui_order, minimum_value, maximum_value, gameplay_tags, extra_information).
      - Per-type default getters: get_{bool,float,int,color,vector,texture,material,projector,
        transform,skeletal_mesh}_parameter_default_value(name).
      - INT/ENUM discrete options: get_int_parameter_group_type / get_int_parameter_option_ui_metadata
        / get_enum_parameter_num_values / get_enum_parameter_value (surfaced defensively when present).
      - States: get_state_count(); get_state_name(index); get_state_parameter_count(state);
        get_state_parameter_name(state, index); get_state_ui_metadata(name).
      - Components: get_component_count(); get_component_name(index).
      - is_compiled(); create_instance() and compile() EXIST but are NEVER called here (mutating).
  * Node classes: unreal.CustomizableObjectNode has 140 reflectable subclasses (two naming families,
    "CONode*" and "CustomizableObjectNode*"). They are UEdGraphNode subclasses in
    /Script/CustomizableObjectEditor; enumerable via UClass reflection with per-class metadata
    (parent_class / is_abstract) from MCPReflectionLibrary.get_class_metadata_json.
  * KNOWN LIMITS (reported in payloads, never hidden):
      - Reading a COMPILED CO's runtime mesh/texture output requires generating an instance
        (update_skeletal_mesh_async) which is a bake -- deliberately NOT done (read-only).
      - The authorable node GRAPH wiring (pins/edges inside a UCustomizableObject's source graph)
        lives on editor-only UEdGraphNode objects reached through the asset's editor graph; a full
        graph-walk reader is a separate C++/editor-graph concern (DEFERRED-C++). This module reads
        the compiled-facing PUBLIC surface (parameters/states/components) + the authorable node CLASS
        set, which is the stable, source-of-truth metadata reachable from the public Python API.

Implemented (all READ-ONLY, no ledger):
  - list_customizable_objects          (AssetRegistry enumerate CustomizableObject assets)
  - list_customizable_object_instances (AssetRegistry enumerate instances + assigned CO)
  - get_customizable_object_info       (parameter/state/component counts + summaries)
  - get_customizable_object_parameters (full per-parameter detail: type/default/ui/options)
  - list_mutable_node_classes          (authorable CustomizableObjectNode subclasses via reflection)
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
    _MU_HELPERS = r'''
import unreal, json, inspect, warnings
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
    return obj, None
def _refl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _class_meta(cls):
    m = _refl()
    if m is None or cls is None:
        return {}
    try:
        return json.loads(m.get_class_metadata_json(cls))
    except Exception:
        return {}
def _enum_name(v):
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
def _ser(v, depth=1):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
    if isinstance(v, unreal.Vector2D):
        return [round(v.x, 4), round(v.y, 4)]
    if isinstance(v, unreal.Rotator):
        return [round(v.pitch, 4), round(v.yaw, 4), round(v.roll, 4)]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return _enum_name(v)
    if isinstance(v, unreal.Transform):
        t = _try(lambda: v.translation); r = _try(lambda: v.rotation); s = _try(lambda: v.scale3d)
        return {"location": _ser(t) if t is not None else None,
                "rotation": _ser(r) if r is not None else None,
                "scale": _ser(s) if s is not None else None}
    if isinstance(v, unreal.Array):
        return [_ser(e, depth) for e in list(v)[:25]]
    if isinstance(v, unreal.Object):
        return {"__object__": _try(lambda: v.get_path_name()), "class": _try(lambda: v.get_class().get_name())}
    if isinstance(v, unreal.StructBase):
        return "<struct %s>" % type(v).__name__
    return str(v)
# Per-type CustomizableObject default-value getter names (called with the parameter name).
_DEFAULT_GETTERS = {
    "BOOL": "get_bool_parameter_default_value",
    "FLOAT": "get_float_parameter_default_value",
    "INT": "get_int_parameter_default_value",
    "COLOR": "get_color_parameter_default_value",
    "VECTOR": "get_vector_parameter_default_value",
    "TEXTURE": "get_texture_parameter_default_value",
    "MATERIAL": "get_material_parameter_default_value",
    "PROJECTOR": "get_projector_parameter_default_value",
    "TRANSFORM": "get_transform_parameter_default_value",
    "SKELETAL_MESH": "get_skeletal_mesh_parameter_default_value",
}
def _ui_meta(co, name):
    md = _try(lambda: co.get_parameter_ui_metadata(name))
    if md is None:
        return None
    rec = {}
    fn = _try(lambda: str(md.get_editor_property("object_friendly_name")))
    if fn:
        rec["friendly_name"] = fn
    sec = _try(lambda: str(md.get_editor_property("ui_section_name")))
    if sec:
        rec["section"] = sec
    order = _try(lambda: md.get_editor_property("ui_order"))
    if order is not None:
        rec["order"] = order
    mn = _try(lambda: md.get_editor_property("minimum_value"))
    mx = _try(lambda: md.get_editor_property("maximum_value"))
    if mn is not None or mx is not None:
        rec["range"] = [mn, mx]
    return rec or None
def _param_record(co, i, include_options=True, include_ui=True):
    name = str(_try(lambda: co.get_parameter_name(i)))
    rec = {"index": i, "name": name}
    rec["type"] = _enum_name(_try(lambda: co.get_parameter_type_by_name(name)))
    md = _try(lambda: co.is_parameter_multidimensional(name))
    if md is not None:
        rec["multidimensional"] = bool(md)
    g = _DEFAULT_GETTERS.get(rec["type"])
    if g is not None and hasattr(co, g):
        rec["default"] = _ser(_try(lambda g=g: getattr(co, g)(name)))
    if include_ui:
        ui = _ui_meta(co, name)
        if ui:
            rec["ui"] = ui
    if include_options and rec["type"] == "INT":
        # INT parameters are discrete option sets; surface option count/names defensively.
        num = _try(lambda: co.get_enum_parameter_num_values(name))
        if isinstance(num, int) and num >= 0:
            opts = []
            for oi in range(min(num, 64)):
                ov = _try(lambda oi=oi: co.get_enum_parameter_value(name, oi))
                opts.append(_ser(ov) if ov is not None else None)
            rec["option_count"] = num
            rec["options"] = opts
        gt = _enum_name(_try(lambda: co.get_int_parameter_group_type(name)))
        if gt:
            rec["group_type"] = gt
    return rec
'''

    # ------------------------------------------------------------------ #
    # list_customizable_objects — AssetRegistry enumerate CO assets        #
    # ------------------------------------------------------------------ #
    _LIST_CO_BODY = _MU_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = int(PARAMS.get("max_results") or 200)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
err = None
try:
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/CustomizableObject", "CustomizableObject")],
        recursive_paths=True, package_paths=[package_path])
    for a in (ar.get_assets(flt) or []):
        pkg = str(a.get_editor_property("package_name"))
        nm = str(a.get_editor_property("asset_name"))
        rows.append({"name": nm, "path": pkg + "." + nm})
except Exception as e:
    err = str(e)
rows.sort(key=lambda r: r["path"].lower())
result = {"status": ("error" if err else "success"), "path": package_path,
          "count": len(rows), "returned": min(len(rows), max_results),
          "assets": rows[:max_results]}
if err:
    result["error"] = err
if not rows:
    result["note"] = ("No CustomizableObject assets under this path. CustomizableObject is a "
                      "/Script/CustomizableObject class (Mutable plugin). If the whole project is "
                      "empty of them, the project ships no Mutable content; point this at a project "
                      "that authors Customizable Characters. Enumeration itself is working (honest empty).")
else:
    result["note"] = ("Use get_customizable_object_info / get_customizable_object_parameters on a "
                      "path for its exposed customization parameters, states, and components.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_customizable_objects(ctx, path: str = None, max_results: int = 200) -> str:
        """Enumerate Mutable CustomizableObject assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Characters'.
        max_results: cap the assets listed (default 200; true 'count' is always reported).

        Returns {count, returned, assets:[{name, path}]}. A CustomizableObject is a Mutable
        'Customizable Character' definition (/Script/CustomizableObject). If empty, the note says so
        honestly (this template project ships zero). Follow up with get_customizable_object_info or
        get_customizable_object_parameters on a path."""
        params = {"path": path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_CO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_customizable_object_instances — instances + assigned CO         #
    # ------------------------------------------------------------------ #
    _LIST_COI_BODY = _MU_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = int(PARAMS.get("max_results") or 200)
resolve = bool(PARAMS.get("resolve_object", True))
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
err = None
try:
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/CustomizableObject", "CustomizableObjectInstance")],
        recursive_paths=True, package_paths=[package_path])
    for a in (ar.get_assets(flt) or []):
        pkg = str(a.get_editor_property("package_name"))
        nm = str(a.get_editor_property("asset_name"))
        rec = {"name": nm, "path": pkg + "." + nm}
        if resolve:
            inst = _try(lambda p=rec["path"]: unreal.EditorAssetLibrary.load_asset(p))
            if inst is not None:
                co = _try(lambda: inst.get_customizable_object())
                rec["customizable_object"] = _try(lambda: co.get_path_name()) if co is not None else None
                rec["current_state"] = _try(lambda: str(inst.get_current_state()))
        rows.append(rec)
except Exception as e:
    err = str(e)
rows.sort(key=lambda r: r["path"].lower())
result = {"status": ("error" if err else "success"), "path": package_path,
          "count": len(rows), "returned": min(len(rows), max_results),
          "instances": rows[:max_results]}
if err:
    result["error"] = err
if not rows:
    result["note"] = ("No CustomizableObjectInstance assets under this path (honest empty; this "
                      "project ships none). An instance is a saved parameter preset that references a "
                      "CustomizableObject; when present, 'customizable_object' gives the assigned CO path.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_customizable_object_instances(ctx, path: str = None, max_results: int = 200,
                                           resolve_object: bool = True) -> str:
        """Enumerate CustomizableObjectInstance assets (saved parameter presets). Read-only.

        path:           content root to scan (default '/Game').
        max_results:    cap the instances listed (default 200; true 'count' is always reported).
        resolve_object: load each instance to report its assigned CustomizableObject path and
                        current state (default True; set False for a pure AssetRegistry listing).

        Returns {count, instances:[{name, path, customizable_object?, current_state?}]}. Honest empty
        note when the project has none."""
        params = {"path": path, "max_results": max_results, "resolve_object": resolve_object}
        try:
            return json.dumps(_exec(_LIST_COI_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_customizable_object_info — counts + summaries                    #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _MU_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.CustomizableObject):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a CustomizableObject (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    co = obj
    info = {"status": "success", "path": co.get_path_name(), "class": co.get_class().get_name()}
    info["is_compiled"] = _try(lambda: bool(co.is_compiled()))
    pc = _try(lambda: co.get_parameter_count(), 0) or 0
    info["parameter_count"] = pc
    params = []
    type_tally = {}
    for i in range(pc):
        nm = str(_try(lambda i=i: co.get_parameter_name(i)))
        ty = _enum_name(_try(lambda nm=nm: co.get_parameter_type_by_name(nm)))
        params.append({"name": nm, "type": ty})
        type_tally[ty] = type_tally.get(ty, 0) + 1
    info["parameters"] = params
    info["parameter_type_tally"] = type_tally
    sc = _try(lambda: co.get_state_count(), 0) or 0
    info["state_count"] = sc
    info["states"] = [str(_try(lambda i=i: co.get_state_name(i))) for i in range(sc)]
    cc = _try(lambda: co.get_component_count(), 0) or 0
    info["component_count"] = cc
    info["components"] = [str(_try(lambda i=i: co.get_component_name(i))) for i in range(cc)]
    info["note"] = (
        "Overview of a Mutable CustomizableObject: exposed customization PARAMETERS (name+type), "
        "STATES (runtime optimization states), and mesh COMPONENTS -- read via the public API "
        "(get_parameter_count / get_parameter_name / get_parameter_type_by_name / get_state_* / "
        "get_component_*). Read-only: no compile/bake is triggered (is_compiled reflects the asset's "
        "stored state). Use get_customizable_object_parameters for full per-parameter detail.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_customizable_object_info(ctx, asset_path: str) -> str:
        """Overview of one Mutable CustomizableObject: parameter/state/component summaries. Read-only.

        asset_path: CustomizableObject asset path, e.g. '/Game/Characters/CO_Hero.CO_Hero'.

        Returns: is_compiled; parameter_count + parameters[{name,type}] + parameter_type_tally;
        state_count + states[]; component_count + components[]. Types come from
        unreal.MutableParameterType (BOOL/COLOR/FLOAT/INT/MATERIAL/PROJECTOR/SKELETAL_MESH/TEXTURE/
        TRANSFORM/...). Never compiles or bakes the object. Use get_customizable_object_parameters
        for defaults, UI ranges, and INT option sets."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_customizable_object_parameters — full per-parameter detail       #
    # ------------------------------------------------------------------ #
    _PARAMS_BODY = _MU_HELPERS + r'''
path = PARAMS.get("asset_path")
include_options = bool(PARAMS.get("include_options", True))
include_ui = bool(PARAMS.get("include_ui", True))
name_filter = (PARAMS.get("filter") or "").strip().lower()
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.CustomizableObject):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a CustomizableObject (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    co = obj
    pc = _try(lambda: co.get_parameter_count(), 0) or 0
    rows = []
    for i in range(pc):
        rec = _param_record(co, i, include_options=include_options, include_ui=include_ui)
        if name_filter and name_filter not in (rec.get("name") or "").lower():
            continue
        rows.append(rec)
    result = {"status": "success", "path": co.get_path_name(),
              "parameter_count": pc, "returned": len(rows), "parameters": rows}
    result["note"] = (
        "Full per-parameter detail via the CustomizableObject public API: type "
        "(unreal.MutableParameterType), multidimensional flag, default value (per-type "
        "get_*_parameter_default_value), UI metadata (friendly name / section / order / numeric "
        "range from get_parameter_ui_metadata), and for INT parameters the discrete option set. "
        "Read-only; no compile/bake. Values are the asset's authored defaults, not a generated "
        "instance.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_customizable_object_parameters(ctx, asset_path: str, filter: str = None,
                                           include_options: bool = True,
                                           include_ui: bool = True) -> str:
        """Full customization-parameter detail for one CustomizableObject. Read-only.

        asset_path:      CustomizableObject asset path.
        filter:          case-insensitive substring; only parameters whose name contains it.
        include_options: for INT (discrete-option) parameters, include option count + option values
                         (default True).
        include_ui:      include UI metadata per parameter -- friendly name, section, order, numeric
                         range (default True).

        Returns parameters[]; each: {index, name, type, multidimensional?, default?, ui?, option_count?,
        options?, group_type?}. Types are unreal.MutableParameterType members. These are the asset's
        authored DEFAULTS (no instance generated, no compile/bake)."""
        params = {"asset_path": asset_path, "filter": filter,
                  "include_options": include_options, "include_ui": include_ui}
        try:
            return json.dumps(_exec(_PARAMS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_mutable_node_classes — authorable CO graph node classes         #
    # ------------------------------------------------------------------ #
    _NODES_BODY = _MU_HELPERS + r'''
name_filter = (PARAMS.get("filter") or "").strip().lower()
max_results = int(PARAMS.get("max_results") or 400)
base = getattr(unreal, "CustomizableObjectNode", None)
classes = []
seen = set()
if base is not None:
    cm0 = _class_meta(base)
    classes.append({"name": "CustomizableObjectNode", "class_path": "unreal.CustomizableObjectNode",
                    "is_base": True, "parent_class": cm0.get("parent_class"),
                    "is_abstract": cm0.get("is_abstract")})
    seen.add("CustomizableObjectNode")
    for nm in dir(unreal):
        cand = getattr(unreal, nm, None)
        if inspect.isclass(cand) and issubclass(cand, unreal.Object) and cand is not base and issubclass(cand, base):
            if nm in seen:
                continue
            seen.add(nm)
            cm = _class_meta(cand)
            classes.append({"name": nm, "class_path": "unreal." + nm,
                            "parent_class": cm.get("parent_class"),
                            "is_abstract": cm.get("is_abstract")})
classes.sort(key=lambda r: (not r.get("is_base", False), r["name"].lower()))
total = len(classes)
if name_filter:
    classes = [c for c in classes if name_filter in c["name"].lower()]
matched = len(classes)
result = {"status": "success", "base_class": "CustomizableObjectNode",
          "total_class_count": total, "matched": matched,
          "returned": min(matched, max_results), "classes": classes[:max_results]}
result["note"] = (
    "Authorable Mutable graph node classes: the UCustomizableObjectNode base + its unreal.*-namespace "
    "subclasses (UEdGraphNode subclasses in /Script/CustomizableObjectEditor; two naming families, "
    "'CONode*' and 'CustomizableObjectNode*'). These are the node TYPES a designer wires into a "
    "CustomizableObject source graph. Enumerated via UClass reflection with metadata "
    "(parent_class / is_abstract) from MCPReflectionLibrary.get_class_metadata_json. The per-asset "
    "graph WIRING (pins/edges of a specific CO) is an editor-graph walk not exposed here (DEFERRED-C++).")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_mutable_node_classes(ctx, filter: str = None, max_results: int = 400) -> str:
        """List authorable Mutable graph node classes (CustomizableObjectNode subclasses). Read-only.

        filter:      case-insensitive substring on class name (e.g. 'material', 'mesh', 'modifier').
        max_results: cap the classes returned (default 400; 'total_class_count'/'matched' always reported).

        Returns classes[] (the CustomizableObjectNode base + namespace subclasses), each with
        parent_class and is_abstract. These are the node TYPES used to author a Customizable Object's
        source graph (families 'CONode*' and 'CustomizableObjectNode*'). Enumerated via UClass
        reflection -- the per-asset graph wiring is not walked here (see note)."""
        params = {"filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
