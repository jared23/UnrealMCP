"""UserTools :: PCG creation + discovery  (spec: docs/spec/pcg.md — Wave 1)

Pure-Python PCG authoring for UE 5.8 (PCG plugin loaded — ProceduralVegetation depends on it).
This module owns the Wave-1 CREATE tools (reversible via the EXISTING editor_level.undo
`create_asset` branch — NO new fold) and the Wave-1 DISCOVERY reads (no ledger).

Companion: pcg_write.py owns Wave-2 graph authoring (NEW pcg_* undo folds).

Reflection facts probed live (2026-08-19, TestMCPSetup):
  - unreal.PCGGraph / PCGGraphFactory / PCGGraphInstance / PCGGraphInstanceFactory /
    PCGBuilderSettingsFactory / PCGComputeSourceFactory / PCGGraphParametersHelpers all present.
  - Create a graph: AssetTools.create_asset(name, pkg, unreal.PCGGraph, PCGGraphFactory())
    -> a fresh PCGGraph with a DefaultInputNode + DefaultOutputNode (reachable ONLY via
    get_input_node()/get_output_node(), NOT in the `nodes` array).
  - PCGGraphInstanceFactory().supported_class -> unreal.PCGGraphInstance; set its source graph
    with set_editor_property("graph", <PCGGraph>). PCGBuilderSettingsFactory().supported_class
    -> a PCGBuilderSettings class (the CLASS itself is NOT bound on unreal.*, resolved via the
    factory). `.supported_class` is an ATTRIBUTE (the UClass), not a method.
  - Node type = a UPCGSettings subclass (251 reflectable). add_node_of_type returns a
    (UPCGNode, UPCGSettings) tuple; the node's addressing id is node.get_name() ('SurfaceSampler_0').

REUSE: the _HELPERS reflection block + _wrap/_query/_exec scaffold + name-family categorizer are
modeled on pcg.py / procvegetation.py verbatim. No ''' and no backslashes inside any snippet body;
all data crosses as base64 PARAMS; reserved locals (sys/unreal/traceback/output_file/...) untouched.
"""
import json
import base64
import textwrap
import os
import time

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from pcg.py / editor_level.py) ----------
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


# Session-aware ledger helper (verbatim from foliage_write.py / editor_level.py).
_COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
'''

# Reflection block (copied from pcg.py's _HELPERS).
_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")

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

def _base_prop_names(base_cls):
    out = set()
    for klass in base_cls.__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__"):
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                out.add(name)
    return out

def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        items = []
        for i, e in enumerate(v):
            if i >= _ARRAY_LIMIT:
                items.append("...(%d more)" % (len(v) - _ARRAY_LIMIT)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:_ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= _ARRAY_LIMIT:
                break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try: d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception: d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
'''

# ------------------------------------------------------------------------- #
# Host-side PCG node-name -> family categorizer (name-family grouping; PCG   #
# palette category metadata is WITH_EDITOR / not reflectable off a CDO).     #
# Ordered; first substring match wins; unmatched -> "Other".                 #
# ------------------------------------------------------------------------- #
_PCG_FAMILIES = [
    ("Sampler", ["Sampler", "Sampling"]),
    ("Spawner", ["Spawner", "Spawn"]),
    ("Filter", ["Filter"]),
    ("Attribute", ["Attribute", "Metadata"]),
    ("Spatial", ["Spatial", "Intersection", "Union", "Difference", "Projection"]),
    ("Density", ["Density"]),
    ("Point", ["Point"]),
    ("Spline", ["Spline"]),
    ("Landscape", ["Landscape"]),
    ("Mesh", ["Mesh", "Collision"]),
    ("Texture", ["Texture"]),
    ("Noise", ["Noise"]),
    ("Transform", ["Transform", "Copy", "Offset"]),
    ("Math", ["Math", "Operation", "Boolean", "Compare", "Add", "Multiply"]),
    ("Bounds", ["Bounds", "Volume", "Box"]),
    ("Subgraph", ["Subgraph", "Loop", "RecursiveGraph"]),
    ("Grammar", ["Grammar", "Split", "SubdivideSegment"]),
    ("Data", ["DataFrom", "GetActorData", "GetLandscapeData", "LoadData", "DataAsset"]),
    ("Actor", ["Actor"]),
    ("Debug", ["Debug", "Visualize"]),
    ("Control", ["Branch", "Select", "Gate", "Switch", "MultiSelect"]),
    ("Partition", ["Partition", "Grid", "Hierarchical"]),
    ("Input", ["Input", "GetPCGComponentData"]),
    ("Custom", ["CustomHLSL", "Compute", "Kernel"]),
]


def _pcg_family(cls_name):
    for cat, kws in _PCG_FAMILIES:
        for kw in kws:
            if kw in cls_name:
                return cat
    return "Other"


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
    # Shared: settings-class list snippet + host-side ranking helpers      #
    # ------------------------------------------------------------------ #
    _SETTINGS_CLASSES_BODY = r'''
import unreal, json
base = getattr(unreal, "PCGSettings", None)
out = []
if base is not None:
    for nm in dir(unreal):
        obj = getattr(unreal, nm, None)
        try:
            if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
                out.append(nm)
        except Exception:
            pass
out = sorted(out)
print("@@UMCP@@" + json.dumps({"status": "success", "has_base": base is not None,
    "total": len(out), "classes": out}))
'''

    def _pcg_class_list():
        res = _exec(_SETTINGS_CLASSES_BODY, {})
        if not res.get("has_base"):
            raise RuntimeError("unreal.PCGSettings not found — PCG plugin not loaded")
        return res.get("classes") or []

    def _rank(names, terms):
        # search_pv_nodes-style ranking: exact > startswith > word-boundary > substring.
        scored = []
        low_terms = [t.lower() for t in terms if t]
        for n in names:
            nl = n.lower()
            score = 0
            for t in low_terms:
                if nl == t or nl == t + "settings" or nl == "pcg" + t + "settings":
                    score += 100
                elif nl.startswith(t) or nl.startswith("pcg" + t):
                    score += 40
                elif t in nl:
                    score += 15
            if not low_terms:
                score = 1
            if score > 0:
                scored.append((score, n))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [n for _s, n in scored]

    # ------------------------------------------------------------------ #
    # 1. create_pcg_graph — PCGGraphFactory                               #
    # ------------------------------------------------------------------ #
    _CREATE_GRAPH_BODY = _COERCE_HELPERS + r'''
name = PARAMS.get("name")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
full = pkg + "/" + name
created_dir = None
if unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    at = unreal.AssetToolsHelpers.get_asset_tools()
    asset = at.create_asset(name, pkg, unreal.PCGGraph, unreal.PCGGraphFactory())
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % full}))
    else:
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        _in = asset.get_input_node(); _on = asset.get_output_node()
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(), "package_path": pkg, "created_dir": created_dir,
            "input_node": (_in.get_name() if _in else None),
            "output_node": (_on.get_name() if _on else None),
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    def _norm_target(asset_path, name, package_path, default_prefix):
        if asset_path:
            ap = asset_path.split(".")[0]
            parts = ap.split("/")
            pkg = "/".join(parts[:-1]); nm = parts[-1]
        else:
            pkg = package_path or "/Game/MCP_Scratch"; nm = name
        if not nm:
            nm = default_prefix + str(int(time.time()))[-6:]
        if not nm.startswith("MCP_"):
            nm = "MCP_PCG_" + nm
        return pkg, nm

    @mcp.tool()
    def create_pcg_graph(ctx, name: str = None, asset_path: str = None,
                         package_path: str = "/Game/MCP_Scratch") -> str:
        """Create an empty PCGGraph asset (via PCGGraphFactory) with its DefaultInput/Output nodes.
        Scratch-only. Ledgered write (op 'create_asset' — the EXISTING editor_level.undo branch;
        `undo` deletes the asset). Saved on create so the undo-delete is reliable.

        name:         asset name; auto-prefixed 'MCP_PCG_' for ownership. Auto-generated if omitted.
        asset_path:   optional full '/Game/MCP_Scratch/<Name>' (overrides name/package_path).
        package_path: content folder; MUST be under '/Game/MCP_Scratch'."""
        try:
            pkg, nm = _norm_target(asset_path, name, package_path, "MCP_PCG_G_")
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg}, indent=2)
            return json.dumps(_exec(_CREATE_GRAPH_BODY, {"name": nm, "package_path": pkg}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. create_pcg_graph_instance — PCGGraphInstanceFactory + source     #
    # ------------------------------------------------------------------ #
    _CREATE_INSTANCE_BODY = _COERCE_HELPERS + r'''
name = PARAMS.get("name")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
source = PARAMS.get("source_graph")
full = pkg + "/" + name
created_dir = None
src = unreal.EditorAssetLibrary.load_asset(source) if source else None
if source and (src is None or not isinstance(src, unreal.PCGGraph)):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source_graph is not a PCGGraph: %s" % source}))
elif unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    at = unreal.AssetToolsHelpers.get_asset_tools()
    gifac = unreal.PCGGraphInstanceFactory()
    gicls = gifac.supported_class
    asset = at.create_asset(name, pkg, gicls, gifac)
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % full}))
    else:
        set_ok = None
        if src is not None:
            try:
                asset.set_editor_property("graph", src); set_ok = "graph"
            except Exception as _e:
                set_ok = "err:" + str(_e)
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        _rb = None
        try:
            _g = asset.get_editor_property("graph"); _rb = _g.get_name() if _g else None
        except Exception:
            pass
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(), "source_graph": source,
            "source_set_via": set_ok, "source_readback": _rb,
            "package_path": pkg, "created_dir": created_dir,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_pcg_graph_instance(ctx, name: str = None, source_graph: str = None,
                                  asset_path: str = None,
                                  package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a PCGGraphInstance asset (via PCGGraphInstanceFactory) that references a source
        PCGGraph (its exposed parameters can then be overridden per-instance). Scratch-only.
        Ledgered write (op 'create_asset'; `undo` deletes the asset).

        source_graph: content path to the PCGGraph this instance wraps (set as the 'graph' property
                      after create). Optional (an instance with no source graph is still valid).
        name/asset_path/package_path: as create_pcg_graph."""
        try:
            pkg, nm = _norm_target(asset_path, name, package_path, "MCP_PCG_I_")
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg}, indent=2)
            return json.dumps(_exec(_CREATE_INSTANCE_BODY,
                {"name": nm, "package_path": pkg, "source_graph": source_graph}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. create_pcg_graph_from_template — factory TemplateGraph / dup     #
    # ------------------------------------------------------------------ #
    _CREATE_FROM_TEMPLATE_BODY = _COERCE_HELPERS + r'''
name = PARAMS.get("name")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
template = PARAMS.get("template")
full = pkg + "/" + name
created_dir = None
tmpl = unreal.EditorAssetLibrary.load_asset(template) if template else None
if tmpl is None or not isinstance(tmpl, unreal.PCGGraph):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "template is not a PCGGraph: %s" % template}))
elif unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    tmpl_nodes = len(list(tmpl.get_editor_property("nodes") or []))
    asset = None; mode = None
    # Preferred: PCGGraphFactory with a template-graph property set (faithful "create from template").
    at = unreal.AssetToolsHelpers.get_asset_tools()
    fac = unreal.PCGGraphFactory()
    tmpl_prop = None
    for _cand in dir(fac):
        if _cand.startswith("__"):
            continue
        if "template" in _cand.lower() and "graph" in _cand.lower():
            tmpl_prop = _cand; break
    if tmpl_prop is None:
        for _cand in dir(fac):
            if not _cand.startswith("__") and "template" in _cand.lower():
                tmpl_prop = _cand; break
    if tmpl_prop is not None:
        try:
            fac.set_editor_property(tmpl_prop, tmpl)
            asset = at.create_asset(name, pkg, unreal.PCGGraph, fac)
            mode = "factory:" + tmpl_prop
        except Exception:
            asset = None
    if asset is None or (tmpl_nodes > 0 and len(list(asset.get_editor_property("nodes") or [])) == 0):
        # Fallback: duplicate the template (a PCG template graph IS a normal PCGGraph).
        if asset is not None:
            try:
                unreal.EditorAssetLibrary.delete_asset(full)
            except Exception:
                pass
        asset = unreal.EditorAssetLibrary.duplicate_asset(template, full)
        mode = "duplicate"
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create-from-template failed for %s" % full}))
    else:
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(), "template": template, "mode": mode,
            "template_node_count": tmpl_nodes,
            "node_count": len(list(asset.get_editor_property("nodes") or [])),
            "package_path": pkg, "created_dir": created_dir,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_pcg_graph_from_template(ctx, template: str, name: str = None,
                                       asset_path: str = None,
                                       package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new PCGGraph seeded from an existing template PCGGraph. Tries PCGGraphFactory's
        template-graph property first; falls back to duplicating the template (a PCG template graph
        is itself a normal PCGGraph). Scratch-only. Ledgered write (op 'create_asset').

        template:     content path to the source/template PCGGraph. Required.
        name/asset_path/package_path: as create_pcg_graph."""
        try:
            if not template:
                return json.dumps({"status": "error", "message": "template is required"}, indent=2)
            pkg, nm = _norm_target(asset_path, name, package_path, "MCP_PCG_T_")
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg}, indent=2)
            return json.dumps(_exec(_CREATE_FROM_TEMPLATE_BODY,
                {"name": nm, "package_path": pkg, "template": template}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. create_pcg_builder_settings — PCGBuilderSettingsFactory          #
    # ------------------------------------------------------------------ #
    _CREATE_BUILDER_BODY = _COERCE_HELPERS + r'''
name = PARAMS.get("name")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
full = pkg + "/" + name
created_dir = None
fac_cls = getattr(unreal, "PCGBuilderSettingsFactory", None)
if fac_cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGBuilderSettingsFactory not present on this build (reflected-gated)"}))
elif unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    at = unreal.AssetToolsHelpers.get_asset_tools()
    fac = fac_cls()
    bcls = fac.supported_class
    asset = None; err = None
    try:
        asset = at.create_asset(name, pkg, bcls, fac)
    except Exception as _e:
        err = str(_e)
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned None for %s" % full, "detail": err,
            "supported_class": (bcls.get_name() if bcls else None)}))
    else:
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(), "package_path": pkg, "created_dir": created_dir,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_pcg_builder_settings(ctx, name: str = None, asset_path: str = None,
                                    package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a PCGBuilderSettings asset (via PCGBuilderSettingsFactory) — configuration for a
        PCG graph builder/generation workflow. Scratch-only. Ledgered write (op 'create_asset').
        Reflected-gated: returns a clear error if the factory is absent on this build.

        name/asset_path/package_path: as create_pcg_graph."""
        try:
            pkg, nm = _norm_target(asset_path, name, package_path, "MCP_PCG_B_")
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg}, indent=2)
            return json.dumps(_exec(_CREATE_BUILDER_BODY, {"name": nm, "package_path": pkg}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5. search_pcg_nodes — rank/filter over the 251 PCG*Settings         #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def search_pcg_nodes(ctx, filter: str = None, filters: list = None,
                         category: str = None, max_results: int = 100) -> str:
        """Search the authorable PCG node types (reflectable UPCGSettings subclasses) with relevance
        ranking (exact > prefix > substring). Read-only, no ledger.

        filter:      case-insensitive name term (e.g. 'Sampler', 'StaticMesh', 'Attribute').
        filters:     list of terms (OR).
        category:    restrict to a name-family category (see list_pcg_node_categories).
        max_results: cap returned; 'total_matched' reports the full count.

        Each entry: {class, category}. Pass to add_pcg_node as the settings_class."""
        try:
            names = _pcg_class_list()
            terms = []
            if filter:
                terms.append(str(filter))
            if filters:
                terms.extend([str(x) for x in filters])
            if terms:
                ranked = _rank(names, terms)
            else:
                ranked = sorted(names)
            if category:
                cl = str(category).lower()
                ranked = [n for n in ranked if _pcg_family(n).lower() == cl]
            total = len(ranked)
            capped = ranked[:int(max_results)] if max_results else ranked
            nodes = [{"class": n, "category": _pcg_family(n)} for n in capped]
            return json.dumps({"status": "success", "base_class": "PCGSettings",
                               "total_matched": total, "returned": len(nodes),
                               "category": category, "nodes": nodes}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 6. list_pcg_node_categories — name-family grouping                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def list_pcg_node_categories(ctx, filter: str = None) -> str:
        """Group the PCG node types into name-family CATEGORIES (Sampler, Spawner, Filter, Attribute,
        Spatial, Density, Point, Spline, Landscape, Mesh, Subgraph, ...). Read-only, no ledger.

        filter: case-insensitive substring to narrow category NAMES.

        NOTE: PCG palette category metadata is WITH_EDITOR / not reflectable off a settings CDO, so
        this is a NAME-family grouping over the ~251 PCGSettings subclasses. Each: {count, classes}."""
        try:
            names = _pcg_class_list()
            cats = {}
            for n in names:
                fam = _pcg_family(n)
                rec = cats.setdefault(fam, {"count": 0, "classes": []})
                rec["count"] += 1
                rec["classes"].append(n)
            if filter:
                f = str(filter).lower()
                cats = {k: v for k, v in cats.items() if f in k.lower()}
            ordered = {k: cats[k] for k in sorted(cats.keys())}
            return json.dumps({"status": "success", "base_class": "PCGSettings",
                               "total_classes": sum(v["count"] for v in cats.values()),
                               "category_count": len(ordered),
                               "grouping": "name-family (PCG category metadata not reflectable)",
                               "categories": ordered}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 7. get_pcg_node_type_info — reflect a settings CDO                   #
    # ------------------------------------------------------------------ #
    _NODE_TYPE_INFO_BODY = _HELPERS + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 50)
cls_name = PARAMS.get("class_name")
depth = int(PARAMS.get("max_depth") or 1)
max_props = int(PARAMS.get("max_properties") or 300)
base = getattr(unreal, "PCGSettings", None)
cls = getattr(unreal, cls_name, None) if cls_name else None
ok = False
try:
    ok = isinstance(cls, type) and issubclass(cls, base) and cls is not base
except Exception:
    ok = False
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGSettings not found (plugin not loaded)"}))
elif not ok:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "not a PCGSettings subclass: %s" % cls_name}))
else:
    cdo = unreal.get_default_object(cls)
    base_names = _base_prop_names(base)
    allp = _prop_names(cdo)
    specific = [p for p in allp if p not in base_names]
    props = {}
    for pn in allp[:max_props]:
        try:
            props[pn] = _ser(cdo.get_editor_property(pn), depth)
        except Exception:
            props[pn] = "<unreadable>"
    print("@@UMCP@@" + json.dumps({"status": "success", "class": cls_name,
        "settings_class": cdo.get_class().get_name(),
        "property_count": len(allp), "node_specific_property_count": len(specific),
        "node_specific_properties": specific,
        "properties": props, "properties_truncated": len(allp) > max_props,
        "pins_note": "pins are computed per placed graph-node, not on the CDO; inspect a placed node via get_pcg_node_info."},
        default=str))
'''

    @mcp.tool()
    def get_pcg_node_type_info(ctx, class_name: str, max_depth: int = 1,
                               max_properties: int = 300) -> str:
        """Reflect a PCG node type's settings CLASS default object: its UPROPERTYs (values), split
        into node-specific vs inherited-from-PCGSettings. Read-only, no ledger.

        class_name: a PCGSettings subclass name (e.g. 'PCGSurfaceSamplerSettings').
        max_depth:  struct-expansion depth for values.

        Use node_specific_properties as the settable prop names for set_pcg_node_property."""
        try:
            return json.dumps(_exec(_NODE_TYPE_INFO_BODY,
                {"class_name": class_name, "max_depth": max_depth,
                 "max_properties": max_properties}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. get_pcg_property_valid_values — enum/meta reflect                 #
    # ------------------------------------------------------------------ #
    _VALID_VALUES_BODY = _HELPERS + r'''
cls_name = PARAMS.get("class_name")
prop = PARAMS.get("property")
base = getattr(unreal, "PCGSettings", None)
cls = getattr(unreal, cls_name, None) if cls_name else None
ok = False
try:
    ok = isinstance(cls, type) and issubclass(cls, base) and cls is not base
except Exception:
    ok = False
if not ok:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a PCGSettings subclass: %s" % cls_name}))
else:
    cdo = unreal.get_default_object(cls)
    def _describe(pn):
        rec = {"property": pn}
        try:
            cur = cdo.get_editor_property(pn)
        except Exception as _e:
            rec["error"] = "unreadable: %s" % _e
            return rec
        rec["current"] = _ser(cur, 0)
        rec["python_type"] = type(cur).__name__
        if isinstance(cur, unreal.EnumBase):
            et = type(cur)
            vals = []
            for k in dir(et):
                if k.startswith("__"):
                    continue
                try:
                    v = getattr(et, k)
                    if isinstance(v, et):
                        vals.append(k)
                except Exception:
                    pass
            rec["kind"] = "enum"; rec["enum_type"] = et.__name__
            rec["valid_values"] = sorted(set(vals))
        elif isinstance(cur, bool):
            rec["kind"] = "bool"; rec["valid_values"] = [True, False]
        elif isinstance(cur, (int, float)):
            rec["kind"] = "numeric"
        elif isinstance(cur, str) or isinstance(cur, (unreal.Name, unreal.Text)):
            rec["kind"] = "string"
        elif isinstance(cur, unreal.Object) or cur is None:
            rec["kind"] = "object-ref"
        else:
            rec["kind"] = type(cur).__name__
        return rec
    if prop:
        res = {"status": "success", "class": cls_name, "detail": _describe(prop)}
    else:
        base_names = _base_prop_names(base)
        specific = [p for p in _prop_names(cdo) if p not in base_names]
        res = {"status": "success", "class": cls_name,
               "properties": [_describe(p) for p in specific]}
    print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def get_pcg_property_valid_values(ctx, class_name: str, property: str = None) -> str:
        """Reflect the valid/allowed values of a PCG node settings property. For enum properties the
        full set of enum entries is returned; for bool -> [True, False]; numeric/string/object-ref
        are classified. Read-only, no ledger.

        class_name: a PCGSettings subclass name.
        property:   a specific settings property; omit to describe all node-specific properties.

        Feeds set_pcg_node_property (enum values are passed by name)."""
        try:
            return json.dumps(_exec(_VALID_VALUES_BODY,
                {"class_name": class_name, "property": property}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. validate_pcg_graph — Python heuristic (dangling / connectivity)  #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _HELPERS + r'''
asset_path = PARAMS.get("asset_path")
g = unreal.EditorAssetLibrary.load_asset(asset_path) if asset_path else None
if g is None or not isinstance(g, unreal.PCGGraph):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a PCGGraph: %s" % asset_path}))
else:
    nodes = list(g.get_editor_property("nodes") or [])
    issues = []
    warnings_l = []
    inn = g.get_input_node(); onn = g.get_output_node()

    def _pinset(node, prop):
        s = set()
        try:
            for p in list(node.get_editor_property(prop) or []):
                try:
                    s.add(str(p.get_editor_property("properties").get_editor_property("label")))
                except Exception:
                    pass
        except Exception:
            pass
        return s

    def _connected(node, prop):
        try:
            for p in list(node.get_editor_property(prop) or []):
                try:
                    if p.is_connected():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    edges = list(g.get_all_edges() or [])
    edge_count = len(edges)
    # dangling edge endpoints
    dangling = 0
    for e in edges:
        try:
            if e.get_input_node() is None or e.get_output_node() is None:
                dangling += 1
        except Exception:
            dangling += 1
    if dangling:
        issues.append("%d edge(s) have a missing endpoint node" % dangling)

    # orphan nodes (no input AND no output connection)
    orphans = []
    for n in nodes:
        has_in = _connected(n, "input_pins")
        has_out = _connected(n, "output_pins")
        if not has_in and not has_out:
            try:
                orphans.append(n.get_name())
            except Exception:
                pass
    if orphans:
        warnings_l.append("orphan node(s) with no connected pins: %s" % orphans)

    # output node reachability (is anything feeding the graph output?)
    out_fed = _connected(onn, "input_pins") if onn is not None else False
    if not out_fed and nodes:
        warnings_l.append("graph output node has no incoming connection (graph produces nothing)")
    in_used = _connected(inn, "output_pins") if inn is not None else False
    if not in_used and nodes:
        warnings_l.append("graph input node has no outgoing connection (nodes ignore graph input)")

    res = {"status": "success", "asset_path": asset_path, "graph_name": g.get_name(),
           "node_count": len(nodes), "edge_count": edge_count,
           "input_node": (inn.get_name() if inn else None),
           "output_node": (onn.get_name() if onn else None),
           "output_is_fed": out_fed, "input_is_used": in_used,
           "dangling_edges": dangling, "orphan_nodes": orphans,
           "valid": (len(issues) == 0), "issues": issues, "warnings": warnings_l}
    print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def validate_pcg_graph(ctx, asset_path: str) -> str:
        """Heuristically validate a PCGGraph (pure-Python, no generation): flags edges with a missing
        endpoint (hard issue), orphan nodes with no connected pins, and whether the graph output is
        fed / graph input is used (warnings). Read-only, no ledger.

        asset_path: content path to the PCGGraph.

        'valid' is False only on a hard structural issue (dangling edge)."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 10. list_pcg_templates — AssetRegistry (bIsTemplate/bExposeToLibrary)#
    # ------------------------------------------------------------------ #
    _LIST_TEMPLATES_BODY = r'''
import unreal, json
path_filter = PARAMS.get("path_filter")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
out = []
templates = []
tag_seen = False
err = None
try:
    tlap = unreal.TopLevelAssetPath("/Script/PCG", "PCGGraph")
    assets = ar.get_assets_by_class(tlap, search_sub_classes=True) or []
except Exception as e:
    assets = []; err = str(e)
for a in assets:
    pkg = str(a.package_name)
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    rec = {"name": str(a.asset_name), "path": pkg}
    is_tmpl = None
    for tagn in ("bIsTemplate", "IsTemplate", "bExposeToLibrary", "ExposeToLibrary"):
        try:
            tv = a.get_tag_value(tagn)
        except Exception:
            tv = None
        if tv is not None:
            tag_seen = True
            sv = str(tv).lower()
            if sv in ("true", "1"):
                is_tmpl = True
            elif is_tmpl is None:
                is_tmpl = False
    out.append({**rec, "is_template": is_tmpl})
    if is_tmpl:
        templates.append(rec)
res = {"status": "success", "template_tag_reflected": tag_seen,
       "total_graphs": len(out), "templates": templates,
       "note": ("PCGGraph template tags (bIsTemplate/bExposeToLibrary) are AssetRegistrySearchable; "
                "if template_tag_reflected is false the tag is not indexed on this build — all graphs listed with is_template=null.")}
if not tag_seen:
    res["all_graphs"] = out
if err:
    res["registry_error"] = err
print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def list_pcg_templates(ctx, path_filter: str = None) -> str:
        """List PCGGraph assets flagged as templates (AssetRegistry bIsTemplate / bExposeToLibrary
        tags). Read-only, no ledger. Reflected-gated: if the tag is not indexed on this build,
        'template_tag_reflected' is false and all graphs are returned with is_template=null.

        path_filter: case-insensitive package-path substring."""
        try:
            return json.dumps(_exec(_LIST_TEMPLATES_BODY, {"path_filter": path_filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"
