"""UserTools :: Mutable — Customizable Objects (READ, wave 2)  (spec: docs/spec/mutable.md)

Clean-room reimplementation over Unreal's PUBLIC Python API (UE 5.8, Mutable / Customizable
Object plugin LOADED). READ-ONLY batch #2 -- the companion to mutable.py. This module NEVER
compiles/bakes a CustomizableObject, never opens an asset editor (no modals), never mutates the
level or any asset, and records NO ledger entries. The query convention, base64 PARAMS injection,
and Output-Log auto-capture are copied verbatim from editor_level.py / mutable.py.

PROBE FINDINGS (verified live vs TestMCPSetup, UE 5.8.1):
  * A factory CustomizableObject (new_customizable_object + AddEssentialGraphNodes) compiles to
    get_state_count()==1 (state "Default"), get_parameter_count()==0, get_component_count()==0 --
    honest-empty until wave-3 graph authoring or an imported authored CO. These tools validate
    honestly against that (states -> 1 "Default"; params -> empty; nodes -> the 140 authorable
    classes).
  * unreal.CustomizableObjectNode has 140 reflectable subclasses in /Script/CustomizableObjectEditor,
    two naming families ("CONode*" and "CustomizableObjectNode*"). Their configuration lives on
    editor-only graph PINS, not UPROPERTYs, so a node CDO typically reflects 0 node-specific
    properties (honest); describe_mutable_node surfaces the reflectable property surface + class
    metadata and states the pin caveat.
  * The CustomizableObjectInstance public API exposes per-type
    get_<type>_parameter_selected_option(name) + get_current_state(); there is NO save/load_descriptor
    in 5.8, so copy_mutable_parameters reads the live per-parameter values directly (portable dict
    that mutable_write.paste_mutable_parameters consumes).

Implemented (all READ-ONLY, no ledger):
  - list_mutable_states        (co.get_state_* + per-state parameters + get_state_ui_metadata)
  - search_mutable_nodes       (rank/substring/family filter over the CustomizableObjectNode set)
  - describe_mutable_node       (resolve a node subclass by name, reflect its CDO UPROPERTYs + metadata)
  - list_mutable_node_categories(name-family grouping of the node classes -- the true category is a
                                 C++ virtual, not reflected; documented)
  - copy_mutable_parameters     (read a CustomizableObjectInstance's current parameter values, or a
                                 CustomizableObject's authored defaults, as a portable dict)
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
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


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

    # Shared Unreal-side helpers. No ''' / no backslashes in this block.
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
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try:
                d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception:
                d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return sorted(names)
# CustomizableObjectInstance per-type "current selected option" getters (name -> instance getter).
_INST_GETTERS = {
    "BOOL": "get_bool_parameter_selected_option",
    "FLOAT": "get_float_parameter_selected_option",
    "INT": "get_int_parameter_selected_option",
    "COLOR": "get_color_parameter_selected_option",
    "TEXTURE": "get_texture_parameter_selected_option",
    "MATERIAL": "get_material_parameter_selected_option",
    "PROJECTOR": "get_projector_parameter_selected_option",
    "TRANSFORM": "get_transform_parameter_selected_option",
    "SKELETAL_MESH": "get_skeletal_mesh_parameter_selected_option",
}
# CustomizableObject per-type authored-default getters (name -> CO getter).
_CO_DEFAULTS = {
    "BOOL": "get_bool_parameter_default_value",
    "FLOAT": "get_float_parameter_default_value",
    "INT": "get_int_parameter_default_value",
    "COLOR": "get_color_parameter_default_value",
    "TEXTURE": "get_texture_parameter_default_value",
    "MATERIAL": "get_material_parameter_default_value",
    "PROJECTOR": "get_projector_parameter_default_value",
    "TRANSFORM": "get_transform_parameter_default_value",
    "SKELETAL_MESH": "get_skeletal_mesh_parameter_default_value",
}
def _node_subclasses():
    base = getattr(unreal, "CustomizableObjectNode", None)
    out = []
    if base is None:
        return base, out
    for nm in dir(unreal):
        cand = getattr(unreal, nm, None)
        if inspect.isclass(cand) and issubclass(cand, unreal.Object) and cand is not base and issubclass(cand, base):
            out.append((nm, cand))
    return base, out
def _node_family(nm):
    if nm.startswith("CONode"):
        return "CONode"
    if nm.startswith("CustomizableObjectNode"):
        return "CustomizableObjectNode"
    return "Other"
# Coarse name-family category buckets (the true UI category is a C++ virtual, not reflected).
_CATEGORY_KEYWORDS = [
    ("Material", ["material"]),
    ("Mesh", ["mesh", "morph", "clip", "reshape", "skeletal"]),
    ("Texture/Image", ["texture", "image", "passthrough", "layout", "uv"]),
    ("Modifier", ["modifier", "modify", "extend", "edit", "remove", "transform"]),
    ("Parameter/Variation", ["parameter", "variation", "switch", "enum", "bool", "float", "int", "color", "vector", "scalar", "range"]),
    ("Object/Group/Structure", ["objectgroup", "group", "childobject", "object", "component", "root", "macro", "reroute", "comment", "table"]),
    ("Projector/Physics/Other", ["projector", "physics", "curve", "animation", "pose", "constant", "expression"]),
]
def _node_category(nm):
    low = nm.lower()
    for cat, keys in _CATEGORY_KEYWORDS:
        for k in keys:
            if k in low:
                return cat
    return "Uncategorized"
'''

    # ------------------------------------------------------------------ #
    # list_mutable_states — states + per-state parameters + ui metadata    #
    # ------------------------------------------------------------------ #
    _STATES_BODY = _MU_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.CustomizableObject):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a CustomizableObject (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    co = obj
    sc = _try(lambda: co.get_state_count(), 0) or 0
    states = []
    for i in range(sc):
        sname = str(_try(lambda i=i: co.get_state_name(i)))
        rec = {"index": i, "name": sname}
        spc = _try(lambda sname=sname: co.get_state_parameter_count(sname))
        if spc is None:
            spc = _try(lambda i=i: co.get_state_parameter_count(i))
        rec["parameter_count"] = spc if isinstance(spc, int) else 0
        pnames = []
        for j in range(rec["parameter_count"] or 0):
            pn = _try(lambda sname=sname, j=j: co.get_state_parameter_name(sname, j))
            if pn is None:
                pn = _try(lambda i=i, j=j: co.get_state_parameter_name(i, j))
            pnames.append(str(pn))
        rec["runtime_parameters"] = pnames
        md = _try(lambda sname=sname: co.get_state_ui_metadata(sname))
        if md is not None:
            uid = {}
            for pn in _prop_names(md):
                uid[pn] = _ser(_try(lambda pn=pn: md.get_editor_property(pn)))
            rec["ui_metadata"] = {k: v for k, v in uid.items() if v not in (None, "", [])}
        states.append(rec)
    result = {"status": "success", "path": co.get_path_name(),
              "state_count": sc, "states": states}
    result["note"] = (
        "Mutable runtime STATES of a CustomizableObject (get_state_count / get_state_name / "
        "get_state_parameter_count / get_state_parameter_name / get_state_ui_metadata). Each state is "
        "a runtime optimization profile listing the parameters kept mutable at runtime "
        "('runtime_parameters'). A freshly created factory CO has exactly one state 'Default' with no "
        "runtime parameters until the graph exposes some (wave-3 authoring). Read-only; no compile.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_mutable_states(ctx, asset_path: str) -> str:
        """List the runtime STATES of a Mutable CustomizableObject. Read-only.

        asset_path: CustomizableObject asset path, e.g. '/Game/Characters/CO_Hero.CO_Hero'.

        Returns state_count + states[{index, name, parameter_count, runtime_parameters[],
        ui_metadata?}]. A state is a runtime optimization profile; 'runtime_parameters' are the
        parameters it keeps mutable at runtime. Uses co.get_state_count / get_state_name /
        get_state_parameter_count / get_state_parameter_name / get_state_ui_metadata. A factory CO
        reports one state 'Default'. Never compiles or bakes."""
        try:
            return json.dumps(_exec(_STATES_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # search_mutable_nodes — rank/filter over the node-class set           #
    # ------------------------------------------------------------------ #
    _SEARCH_BODY = _MU_HELPERS + r'''
query = (PARAMS.get("query") or "").strip().lower()
category = (PARAMS.get("category") or "").strip().lower()
family = (PARAMS.get("family") or "").strip()
max_results = int(PARAMS.get("max_results") or 60)
base, subs = _node_subclasses()
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unreal.CustomizableObjectNode not found (Mutable plugin not loaded?)"}))
else:
    rows = []
    for nm, cand in subs:
        fam = _node_family(nm)
        cat = _node_category(nm)
        if family and fam != family:
            continue
        if category and category not in cat.lower():
            continue
        low = nm.lower()
        score = None
        if query:
            if low == query:
                score = 100
            elif low == ("conode" + query) or low == ("customizableobjectnode" + query):
                score = 95
            elif query in low:
                # earlier match + shorter name ranks higher
                score = 80 - low.index(query) - (len(low) - len(query)) * 0.1
            else:
                continue
        else:
            score = 0
        rows.append((score, {"name": nm, "class_path": "unreal." + nm, "family": fam, "category": cat}))
    rows.sort(key=lambda r: (-r[0], r[1]["name"].lower()))
    matched = len(rows)
    out = [r[1] for r in rows[:max_results]]
    result = {"status": "success", "query": PARAMS.get("query"), "category": PARAMS.get("category"),
              "family": PARAMS.get("family") or None, "total_node_classes": len(subs),
              "matched": matched, "returned": len(out), "nodes": out}
    result["note"] = (
        "Ranked search over the authorable CustomizableObjectNode subclasses (families 'CONode*' and "
        "'CustomizableObjectNode*'). 'category' is a coarse NAME-FAMILY bucket (the true editor "
        "category is a C++ virtual, not reflected). Use describe_mutable_node for one class's "
        "reflected property surface. Graph AUTHORING with these node types is wave-3 (C++).")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def search_mutable_nodes(ctx, query: str = None, category: str = None,
                             family: str = None, max_results: int = 60) -> str:
        """Search/rank the authorable Mutable graph node classes by name. Read-only.

        query:       case-insensitive substring on the class name (exact / prefix-normalized matches
                     rank first). Omit to list all (optionally filtered by category/family).
        category:    coarse name-family bucket filter, e.g. 'Material', 'Mesh', 'Texture', 'Modifier',
                     'Parameter', 'Object' (substring-matched; see list_mutable_node_categories).
        family:      exact naming family: 'CONode' or 'CustomizableObjectNode'.
        max_results: cap results (default 60; 'matched'/'total_node_classes' always reported).

        Returns nodes[{name, class_path, family, category}] ranked by match quality. These are the
        node TYPES used to wire a CustomizableObject source graph; per-asset graph wiring is wave-3
        (C++)."""
        params = {"query": query, "category": category, "family": family, "max_results": max_results}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # describe_mutable_node — reflect one node subclass' CDO               #
    # ------------------------------------------------------------------ #
    _DESCRIBE_BODY = _MU_HELPERS + r'''
name = (PARAMS.get("node_name") or "").strip()
max_props = int(PARAMS.get("max_props") or 60)
base, subs = _node_subclasses()
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unreal.CustomizableObjectNode not found (Mutable plugin not loaded?)"}))
else:
    by_name = {nm: cand for nm, cand in subs}
    by_name["CustomizableObjectNode"] = base
    cls = None
    resolved = None
    # exact, then prefix-normalized candidates
    cands = [name, "CONode" + name, "CustomizableObjectNode" + name]
    for c in cands:
        if c in by_name:
            cls = by_name[c]; resolved = c; break
    if cls is None:
        low = name.lower()
        matches = [nm for nm in by_name if low == nm.lower()]
        if not matches:
            matches = [nm for nm in by_name if low and low in nm.lower()]
        if len(matches) == 1:
            resolved = matches[0]; cls = by_name[resolved]
        elif len(matches) > 1:
            print("@@UMCP@@" + json.dumps({"status": "ambiguous", "query": name,
                "candidates": sorted(matches)[:40],
                "message": "multiple node classes match; pass an exact name"}))
            cls = None; resolved = "__ambiguous__"
    if cls is None and resolved != "__ambiguous__":
        print("@@UMCP@@" + json.dumps({"status": "error", "query": name,
            "message": "no CustomizableObjectNode subclass named %r" % name,
            "hint": "use search_mutable_nodes to find a class name"}))
    elif cls is not None:
        meta = _class_meta(cls)
        cdo = _try(lambda: cls.get_default_object())
        base_cdo = _try(lambda: base.get_default_object())
        base_props = set(_prop_names(base_cdo)) if base_cdo is not None else set()
        allp = _prop_names(cdo) if cdo is not None else []
        own = [p for p in allp if p not in base_props]
        prop_vals = {}
        for p in (own + [x for x in allp if x not in own])[:max_props]:
            if cdo is None:
                break
            prop_vals[p] = _ser(_try(lambda p=p: cdo.get_editor_property(p)))
        result = {"status": "success", "name": resolved, "class_path": "unreal." + resolved,
                  "family": _node_family(resolved), "category": _node_category(resolved),
                  "parent_class": meta.get("parent_class"),
                  "is_abstract": meta.get("is_abstract"),
                  "reflected_property_count": len(allp),
                  "node_specific_properties": own,
                  "properties": prop_vals}
        result["note"] = (
            "Reflection of a CustomizableObjectNode subclass' class-default object (get_default_object "
            "+ UPROPERTY walk, node-specific = declared beyond the UCustomizableObjectNode base). Most "
            "Mutable nodes hold their configuration on editor GRAPH PINS (edges/labels), not UPROPERTYs, "
            "so 'node_specific_properties' is often empty (honest) -- the base props "
            "(comment/enable/etc.) are still surfaced. Pin/edge reflection + graph authoring are "
            "wave-3 (C++). This is a class-level description, not a per-asset node instance.")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def describe_mutable_node(ctx, node_name: str, max_props: int = 60) -> str:
        """Describe one authorable Mutable node class by reflecting its class-default object. Read-only.

        node_name: class name; exact ('CustomizableObjectNodeObject') or short ('Object',
                   'MaterialConstant') -- resolved against both naming families.
        max_props: cap serialized properties (default 60).

        Returns {name, class_path, family, category, parent_class, is_abstract,
        reflected_property_count, node_specific_properties[], properties{name: value}}. Node config
        mostly lives on graph PINS (not UPROPERTYs), so node_specific_properties is often empty
        (honest); base props are still shown. Pin reflection + graph authoring are wave-3 (C++). Use
        search_mutable_nodes to find a class name."""
        params = {"node_name": node_name, "max_props": max_props}
        try:
            return json.dumps(_exec(_DESCRIBE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_mutable_node_categories — name-family grouping                  #
    # ------------------------------------------------------------------ #
    _CATEGORIES_BODY = _MU_HELPERS + r'''
base, subs = _node_subclasses()
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unreal.CustomizableObjectNode not found (Mutable plugin not loaded?)"}))
else:
    cats = {}
    fams = {}
    for nm, cand in subs:
        cat = _node_category(nm)
        fam = _node_family(nm)
        cats.setdefault(cat, []).append(nm)
        fams[fam] = fams.get(fam, 0) + 1
    cat_rows = []
    for cat, names in cats.items():
        cat_rows.append({"category": cat, "count": len(names), "examples": sorted(names)[:8]})
    cat_rows.sort(key=lambda r: (-r["count"], r["category"]))
    result = {"status": "success", "total_node_classes": len(subs),
              "family_counts": fams, "category_count": len(cat_rows), "categories": cat_rows}
    result["note"] = (
        "Coarse NAME-FAMILY grouping of the 140 authorable CustomizableObjectNode subclasses into "
        "buckets (Material / Mesh / Texture / Modifier / Parameter / Object / etc.) plus the two "
        "naming families ('CONode*', 'CustomizableObjectNode*'). IMPORTANT: this is a heuristic over "
        "class NAMES -- the TRUE editor palette category is a C++ virtual (GetNodeCategory / context-"
        "menu grouping) that is not reflected into Python, so treat these buckets as an approximation, "
        "not the authoritative palette taxonomy.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_mutable_node_categories(ctx) -> str:
        """Group the authorable Mutable node classes into coarse name-family categories. Read-only.

        Returns family_counts ({CONode, CustomizableObjectNode, Other}) + categories[{category, count,
        examples[]}]. This is a heuristic over class NAMES -- the true editor palette category is an
        unreflected C++ virtual, so the buckets approximate rather than reproduce the palette taxonomy
        (stated in the note). Use search_mutable_nodes(category=...) to list a bucket, or
        describe_mutable_node for one class."""
        try:
            return json.dumps(_exec(_CATEGORIES_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # copy_mutable_parameters — read current instance values / CO defaults #
    # ------------------------------------------------------------------ #
    _COPY_BODY = _MU_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif isinstance(obj, unreal.CustomizableObjectInstance):
    inst = obj
    co = _try(lambda: inst.get_customizable_object())
    if co is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "instance has no assigned CustomizableObject: %s" % path}))
    else:
        pc = _try(lambda: co.get_parameter_count(), 0) or 0
        rows = []
        for i in range(pc):
            nm = str(_try(lambda i=i: co.get_parameter_name(i)))
            ty = _enum_name(_try(lambda nm=nm: co.get_parameter_type_by_name(nm)))
            g = _INST_GETTERS.get(ty)
            val = None
            if g is not None and hasattr(inst, g):
                val = _ser(_try(lambda g=g, nm=nm: getattr(inst, g)(nm)))
            rows.append({"name": nm, "type": ty, "value": val})
        result = {"status": "success", "source": "instance", "instance_path": inst.get_path_name(),
                  "customizable_object": _try(lambda: co.get_path_name()),
                  "current_state": _try(lambda: str(inst.get_current_state())),
                  "parameter_count": pc, "parameters": rows}
        result["note"] = (
            "Portable snapshot of a CustomizableObjectInstance's CURRENT parameter values (per-type "
            "get_<type>_parameter_selected_option). UE 5.8 has no save/load_descriptor, so values are "
            "read directly. Feed this 'parameters' list (or a {name: value} subset) to "
            "mutable_write.paste_mutable_parameters to re-apply on another instance. Read-only.")
        print("@@UMCP@@" + json.dumps(result))
elif isinstance(obj, unreal.CustomizableObject):
    co = obj
    pc = _try(lambda: co.get_parameter_count(), 0) or 0
    rows = []
    for i in range(pc):
        nm = str(_try(lambda i=i: co.get_parameter_name(i)))
        ty = _enum_name(_try(lambda nm=nm: co.get_parameter_type_by_name(nm)))
        g = _CO_DEFAULTS.get(ty)
        val = None
        if g is not None and hasattr(co, g):
            val = _ser(_try(lambda g=g, nm=nm: getattr(co, g)(nm)))
        rows.append({"name": nm, "type": ty, "value": val})
    result = {"status": "success", "source": "customizable_object_defaults",
              "customizable_object": co.get_path_name(),
              "parameter_count": pc, "parameters": rows}
    result["note"] = (
        "Portable snapshot of a CustomizableObject's AUTHORED DEFAULT parameter values (per-type "
        "get_<type>_parameter_default_value). Pass a CustomizableObjectInstance path instead to read a "
        "live instance's current values. Read-only.")
    print("@@UMCP@@" + json.dumps(result))
else:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is neither a CustomizableObject nor a CustomizableObjectInstance (got %s): %s"
                   % (obj.get_class().get_name(), path)}))
'''

    @mcp.tool()
    def copy_mutable_parameters(ctx, asset_path: str) -> str:
        """Read a Mutable instance's current parameter values (or a CO's authored defaults). Read-only.

        asset_path: a CustomizableObjectInstance path (reads its CURRENT selected options) OR a
                    CustomizableObject path (reads its AUTHORED default values).

        Returns parameters[{name, type, value}] (+ instance current_state when reading an instance).
        UE 5.8 has no save/load_descriptor; values are read per-parameter via
        get_<type>_parameter_selected_option (instance) / get_<type>_parameter_default_value (CO).
        The result is a portable snapshot consumable by mutable_write.paste_mutable_parameters. A
        factory CO / its instance has zero parameters (honest empty). No ledger."""
        try:
            return json.dumps(_exec(_COPY_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
