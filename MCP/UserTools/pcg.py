"""UserTools :: PCG — Procedural Content Generation (READ-ONLY)  (spec: docs/spec/pcg.md)

Clean-room reimplementation over Unreal's PUBLIC PCG Python API (UE 5.8, PCG plugin
enabled). Reads only — this module NEVER triggers a PCG generation/regeneration/cleanup
(that would mutate the level), NEVER writes, and records no ledger entries.

Query convention (copied verbatim from editor_level.py / objects.py): a snippet prints
@@UMCP@@<json> on one line; _query() finds that marker and parses the JSON after it.
Params are injected as base64 JSON via _exec() so they survive the handler's triple-
single-quote wrapping. Every snippet is wrapped with Output-Log delta capture (new
Warning/Error lines are attached as result['_log_warnings']).

Reflection surface discovered on this project (live probe, 2026-08-15):
  - unreal.PCGGraph : property `nodes` (list[UPCGNode]); get_all_edges() -> list[UPCGEdge];
                      get_input_node()/get_output_node(); property `user_parameters`
                      (InstancedPropertyBag, .to_dict()); title / override_title.
  - unreal.PCGNode  : get_settings() -> UPCGSettings; get_node_position() -> (x, y) tuple;
                      properties `input_pins` / `output_pins` (list[UPCGPin]);
                      property `node_title` (override FName, usually None).
  - unreal.PCGPin   : property `properties` (FPCGPinProperties: .label); property `edges`
                      (list[UPCGEdge]); is_connected(); get_tooltip().
  - unreal.PCGEdge  : get_input_node()/get_input_pin_label() = SOURCE (upstream) side;
                      get_output_node()/get_output_pin_label() = TARGET (downstream) side.
  - unreal.PCGComponent : get_graph(); property `seed`; property `generated`;
                      is_component_partitioned(); property `generation_trigger`.
  - unreal.PCGVolume    : property `pcg_component`.
Each PCG *node type* is a UPCGSettings subclass (245 reflectable on this build); there are
only 4 UPCGNode subclasses (most nodes are the generic UPCGNode + a settings object).

Implemented (all READ-ONLY):
  - list_pcg_graphs            : AssetRegistry enumerate PCGGraph assets {name, path}
  - get_pcg_graph_info         : a graph's nodes (+settings class, key settings, position),
                                 input/output nodes, edge connections, user parameters
  - get_pcg_node_info          : one node's full settings reflection + pins/connections
  - list_pcg_components_in_level : placed PCGComponents / PCGVolumes + assigned graph/seed/
                                 generated/partitioned state (never generates)
  - list_pcg_settings_classes  : reflectable UPCGSettings subclasses (authorable node types)
  - list_pcg_node_classes      : reflectable UPCGNode subclasses
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim pattern from editor_level.py) ----------
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


# Shared Unreal-side helpers (no ''' and no backslashes — the handler wraps code in '''...''').
# A value serializer + a property-name enumerator + owner-tracking so we can separate a
# settings object's node-specific UPROPERTYs from the ones inherited from the UPCGSettings base.
_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")  # deprecated-alias reads spam DeprecationWarning; pure reads

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
    # Names declared anywhere on base_cls.__mro__ (used to subtract inherited props).
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

def _node_position(n):
    try:
        p = n.get_node_position()
    except Exception:
        return None
    if p is None:
        return None
    if hasattr(p, "x") and hasattr(p, "y"):
        return [p.x, p.y]
    try:
        return [p[0], p[1]]
    except Exception:
        return None

def _settings_of(node):
    try:
        return node.get_settings()
    except Exception:
        return None

def _key_settings(settings, limit):
    # Node-specific UPROPERTYs (declared on the concrete settings class, not inherited from
    # the UPCGSettings base) plus seed/use_seed/enabled if present. Values are serialized.
    if settings is None:
        return None, None
    base = getattr(unreal, "PCGSettings", None)
    base_names = _base_prop_names(base) if base is not None else set()
    allnames = _prop_names(settings)
    specific = [n for n in allnames if n not in base_names]
    for common in ("seed", "use_seed", "enabled"):
        if common in allnames and common not in specific:
            specific.append(common)
    specific = sorted(set(specific))[:int(limit)]
    out = {}
    for n in specific:
        try:
            out[n] = _ser(settings.get_editor_property(n), 1)
        except Exception:
            out[n] = "<unreadable>"
    return out, settings.get_class().get_name()

def _pin_labels(node, prop):
    try:
        pins = list(node.get_editor_property(prop) or [])
    except Exception:
        return []
    out = []
    for p in pins:
        lbl = None
        try:
            lbl = str(p.get_editor_property("properties").get_editor_property("label"))
        except Exception:
            lbl = None
        rec = {"label": lbl}
        try:
            rec["connected"] = bool(p.is_connected())
        except Exception:
            pass
        out.append(rec)
    return out
'''


def register_tools(mcp, utils):
    send_command = utils["send_command"]

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. New Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS as base64 JSON, run body in Unreal, return its MARKER payload.
        Read-only module: no _session/ledger is injected."""
        params = dict(params or {})
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # list_pcg_graphs — AssetRegistry enumerate PCGGraph assets           #
    # ------------------------------------------------------------------ #
    _LIST_GRAPHS_BODY = r'''
import unreal, json
path_filter = PARAMS.get("path_filter")
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
graphs = []
err = None
try:
    tlap = unreal.TopLevelAssetPath("/Script/PCG", "PCGGraph")
    assets = ar.get_assets_by_class(tlap, search_sub_classes=True) or []
except Exception as e:
    assets = []
    err = str(e)
for a in assets:
    name = str(a.asset_name)
    pkg = str(a.package_name)
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    if name_filter and name_filter.lower() not in name.lower():
        continue
    graphs.append({"name": name, "path": pkg,
                   "class": str(a.asset_class_path.asset_name)})
graphs.sort(key=lambda g: g["path"])
total = len(graphs)
if max_results:
    graphs = graphs[:int(max_results)]
res = {"status": "success", "total": total, "returned": len(graphs), "graphs": graphs}
if err:
    res["registry_error"] = err
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_pcg_graphs(ctx, path_filter: str = None, name_filter: str = None,
                        max_results: int = None) -> str:
        """List PCGGraph assets in the project via the Asset Registry. Read-only.

        path_filter: case-insensitive substring on the package path (e.g. '/Game/', '/PCG/').
        name_filter: case-insensitive substring on the asset name.
        max_results: cap the number returned (after filtering); 'total' reports the full count.

        Each entry: {name, path (package path), class}. Includes PCGGraph subclasses."""
        params = {"path_filter": path_filter, "name_filter": name_filter,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_GRAPHS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_pcg_graph_info — nodes + settings + edges + parameters          #
    # ------------------------------------------------------------------ #
    _GRAPH_INFO_BODY = _HELPERS + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 25)
asset_path = PARAMS.get("asset_path")
max_nodes = PARAMS.get("max_nodes")
key_limit = int(PARAMS.get("key_settings_limit") or 12)
include_edges = bool(PARAMS.get("include_edges", True))
include_settings = bool(PARAMS.get("include_settings", True))

def _exists(p):
    try:
        return unreal.EditorAssetLibrary.does_asset_exist(p)
    except Exception:
        return True

if not asset_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path is required"}))
elif not _exists(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    g = unreal.EditorAssetLibrary.load_asset(asset_path)
    if g is None or not isinstance(g, unreal.PCGGraph):
        cn = (g.get_class().get_name() if g is not None else None)
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a PCGGraph (class=%s): %s" % (cn, asset_path)}))
    else:
        nodes = list(g.get_editor_property("nodes") or [])
        node_recs = []
        shown = nodes if not max_nodes else nodes[:int(max_nodes)]
        for n in shown:
            rec = {"name": None, "settings_class": None}
            try:
                rec["name"] = n.get_name()
            except Exception:
                pass
            try:
                ot = str(n.get_editor_property("node_title"))
                rec["override_title"] = None if ot in ("None", "") else ot
            except Exception:
                pass
            rec["position"] = _node_position(n)
            s = _settings_of(n)
            if s is not None:
                rec["settings_class"] = s.get_class().get_name()
                if include_settings:
                    ks, _sc = _key_settings(s, key_limit)
                    rec["key_settings"] = ks
            try:
                rec["input_pins"] = _pin_labels(n, "input_pins")
                rec["output_pins"] = _pin_labels(n, "output_pins")
            except Exception:
                pass
            node_recs.append(rec)

        def _nname(x):
            try:
                return x.get_name()
            except Exception:
                return None
        inn = None; onn = None
        try:
            inn = _nname(g.get_input_node())
        except Exception:
            pass
        try:
            onn = _nname(g.get_output_node())
        except Exception:
            pass

        edges = []
        edge_total = 0
        if include_edges:
            try:
                alledges = list(g.get_all_edges() or [])
                edge_total = len(alledges)
                for e in alledges[: _ARRAY_LIMIT * 4]:
                    try:
                        edges.append({
                            "from_node": _nname(e.get_input_node()),
                            "from_pin": str(e.get_input_pin_label()),
                            "to_node": _nname(e.get_output_node()),
                            "to_pin": str(e.get_output_pin_label()),
                        })
                    except Exception:
                        pass
            except Exception:
                edge_total = None

        params_out = None
        try:
            up = g.get_editor_property("user_parameters")
            if up is not None and hasattr(up, "to_dict"):
                params_out = up.to_dict()
        except Exception:
            params_out = None

        res = {"status": "success", "asset_path": asset_path,
               "graph_name": g.get_name(),
               "node_count": len(nodes), "nodes_returned": len(node_recs),
               "input_node": inn, "output_node": onn,
               "edge_count": edge_total, "edges": edges,
               "graph_parameters": params_out,
               "nodes": node_recs}
        print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def get_pcg_graph_info(ctx, asset_path: str, max_nodes: int = None,
                           include_edges: bool = True, include_settings: bool = True,
                           key_settings_limit: int = 12,
                           array_element_limit: int = 25) -> str:
        """Inspect a PCGGraph asset: its nodes, their settings classes and key settings,
        pin labels, the input/output nodes, edge connections, and exposed graph parameters.
        Read-only (loads the asset; never generates).

        asset_path: content path to the PCGGraph, e.g. '/PCG/Subgraphs/CutSplines'.
        max_nodes:  cap nodes returned (node_count reports the full total).
        include_edges:    include the graph's edge list (from_node/from_pin -> to_node/to_pin).
        include_settings: include each node's key_settings (node-specific UPROPERTYs + seed).
        key_settings_limit: cap key_settings entries per node.

        Per node: {name, settings_class, override_title, position [x,y], key_settings,
        input_pins/output_pins (label + connected)}. Edges are directed source->target
        (PCGEdge names the upstream side 'input' and the downstream side 'output')."""
        params = {"asset_path": asset_path, "max_nodes": max_nodes,
                  "include_edges": include_edges, "include_settings": include_settings,
                  "key_settings_limit": key_settings_limit,
                  "array_element_limit": array_element_limit}
        try:
            return json.dumps(_exec(_GRAPH_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_pcg_node_info — one node's full settings + pins/connections     #
    # ------------------------------------------------------------------ #
    _NODE_INFO_BODY = _HELPERS + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 50)
asset_path = PARAMS.get("asset_path")
node_name = PARAMS.get("node_name")
depth = int(PARAMS.get("max_depth") or 1)

def _exists(p):
    try:
        return unreal.EditorAssetLibrary.does_asset_exist(p)
    except Exception:
        return True

if not asset_path or not node_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path and node_name are required"}))
elif not _exists(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    g = unreal.EditorAssetLibrary.load_asset(asset_path)
    if g is None or not isinstance(g, unreal.PCGGraph):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a PCGGraph: %s" % asset_path}))
    else:
        nodes = list(g.get_editor_property("nodes") or [])
        target = None
        for n in nodes:
            try:
                if n.get_name() == node_name:
                    target = n; break
            except Exception:
                pass
        if target is None:
            avail = []
            for n in nodes[:60]:
                try: avail.append(n.get_name())
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "node not found: %s" % node_name, "available_nodes": avail}))
        else:
            s = _settings_of(target)
            settings = {}
            settings_class = None
            if s is not None:
                settings_class = s.get_class().get_name()
                for pn in _prop_names(s):
                    try:
                        settings[pn] = _ser(s.get_editor_property(pn), depth)
                    except Exception:
                        settings[pn] = "<unreadable>"

            def _pins_full(prop):
                out = []
                try:
                    pins = list(target.get_editor_property(prop) or [])
                except Exception:
                    return out
                for p in pins:
                    rec = {}
                    try:
                        rec["label"] = str(p.get_editor_property("properties").get_editor_property("label"))
                    except Exception:
                        rec["label"] = None
                    try:
                        rec["connected"] = bool(p.is_connected())
                    except Exception:
                        pass
                    try:
                        rec["tooltip"] = str(p.get_tooltip())
                    except Exception:
                        pass
                    conns = []
                    try:
                        for e in list(p.get_editor_property("edges") or []):
                            try:
                                conns.append({
                                    "from_node": e.get_input_node().get_name() if e.get_input_node() else None,
                                    "from_pin": str(e.get_input_pin_label()),
                                    "to_node": e.get_output_node().get_name() if e.get_output_node() else None,
                                    "to_pin": str(e.get_output_pin_label()),
                                })
                            except Exception:
                                pass
                    except Exception:
                        pass
                    rec["edges"] = conns
                    out.append(rec)
                return out

            res = {"status": "success", "asset_path": asset_path,
                   "node_name": target.get_name(),
                   "node_class": target.get_class().get_name(),
                   "settings_class": settings_class,
                   "position": _node_position(target),
                   "input_pins": _pins_full("input_pins"),
                   "output_pins": _pins_full("output_pins"),
                   "settings": settings}
            print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def get_pcg_node_info(ctx, asset_path: str, node_name: str,
                          max_depth: int = 1, array_element_limit: int = 50) -> str:
        """Inspect ONE node inside a PCGGraph: its node class, settings class, position, all
        reflected settings UPROPERTYs (values), and its input/output pins with per-pin edge
        connections. Read-only.

        asset_path: content path to the PCGGraph.
        node_name:  the node's unique internal name (from get_pcg_graph_info, e.g.
                    'SplineSampler_14'). On miss, returns 'available_nodes'.
        max_depth:  struct-expansion depth for settings values (default 1).

        Pin edges are directed source->target (from_node/from_pin -> to_node/to_pin)."""
        params = {"asset_path": asset_path, "node_name": node_name,
                  "max_depth": max_depth, "array_element_limit": array_element_limit}
        try:
            return json.dumps(_exec(_NODE_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_pcg_components_in_level — placed PCGComponents / PCGVolumes     #
    # ------------------------------------------------------------------ #
    _COMPONENTS_BODY = r'''
import unreal, json
include_volumes = bool(PARAMS.get("include_volumes", True))

def _graph_of(comp):
    # Assigned graph asset path, read-only. PCGComponent.get_graph() -> UPCGGraph|None.
    try:
        g = comp.get_graph()
    except Exception:
        g = None
    if g is None:
        return None
    try:
        return g.get_path_name()
    except Exception:
        return str(g)

def _comp_rec(actor, comp):
    rec = {"actor": None, "component": None, "class": None,
           "graph": None, "seed": None, "is_generated": None,
           "is_partitioned": None, "generation_trigger": None}
    try: rec["actor"] = actor.get_actor_label()
    except Exception: pass
    try: rec["component"] = comp.get_name()
    except Exception: pass
    try: rec["class"] = comp.get_class().get_name()
    except Exception: pass
    rec["graph"] = _graph_of(comp)
    try: rec["seed"] = int(comp.get_editor_property("seed"))
    except Exception: pass
    try: rec["is_generated"] = bool(comp.get_editor_property("generated"))
    except Exception: pass
    try: rec["is_partitioned"] = bool(comp.is_component_partitioned())
    except Exception: pass
    try: rec["generation_trigger"] = str(comp.get_editor_property("generation_trigger"))
    except Exception: pass
    return rec

eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
comps = []
volumes = []
pcg_comp_cls = getattr(unreal, "PCGComponent", None)
for a in actors:
    if a is None:
        continue
    cn = a.get_class().get_name()
    is_vol = isinstance(a, unreal.PCGVolume) if getattr(unreal, "PCGVolume", None) else (cn == "PCGVolume")
    if is_vol and include_volumes:
        vrec = {"actor": a.get_actor_label(), "class": cn, "graph": None, "component": None}
        try:
            pc = a.get_editor_property("pcg_component")
            if pc is not None:
                vrec["component"] = pc.get_name()
                vrec["graph"] = _graph_of(pc)
                try: vrec["is_generated"] = bool(pc.get_editor_property("generated"))
                except Exception: pass
        except Exception:
            pass
        volumes.append(vrec)
    for c in (a.get_components_by_class(unreal.ActorComponent) or []):
        try:
            if pcg_comp_cls is not None and isinstance(c, pcg_comp_cls):
                comps.append(_comp_rec(a, c))
        except Exception:
            pass

res = {"status": "success",
       "pcg_component_count": len(comps), "pcg_components": comps,
       "pcg_volume_count": len(volumes), "pcg_volumes": volumes,
       "note": "Read-only enumeration; no generation was triggered."}
print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def list_pcg_components_in_level(ctx, include_volumes: bool = True) -> str:
        """List placed PCGComponents (and PCGVolume actors) in the active level, with each
        one's assigned graph asset, seed, generated flag, partitioned flag, and generation
        trigger. STRICTLY read-only — never triggers a PCG generation/cleanup.

        include_volumes: also list PCGVolume actors and their embedded PCG component.

        Per component: {actor, component, class, graph, seed, is_generated, is_partitioned,
        generation_trigger}. On a level with no PCG (e.g. a hand-built combat map) both lists
        are empty."""
        params = {"include_volumes": include_volumes}
        try:
            return json.dumps(_exec(_COMPONENTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_pcg_settings_classes — authorable node/settings types          #
    # ------------------------------------------------------------------ #
    _SETTINGS_CLASSES_BODY = r'''
import unreal, json
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
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
if name_filter:
    f = name_filter.lower()
    out = [n for n in out if f in n.lower()]
out = sorted(out)
total = len(out)
if max_results:
    out = out[:int(max_results)]
print("@@UMCP@@" + json.dumps({"status": "success", "base_class": "PCGSettings",
    "total": total, "returned": len(out), "settings_classes": out}))
'''

    @mcp.tool()
    def list_pcg_settings_classes(ctx, name_filter: str = None, max_results: int = None) -> str:
        """List the reflectable UPCGSettings subclasses on this build — the authorable PCG
        node types (each PCG node is backed by a settings object). Read-only reflection.

        name_filter: case-insensitive substring on the class name (e.g. 'Sampler', 'Attribute').
        max_results: cap the number returned; 'total' reports the full count.

        Returns Python-exposed class names, e.g. 'PCGSurfaceSamplerSettings'."""
        params = {"name_filter": name_filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_SETTINGS_CLASSES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_pcg_node_classes — reflectable UPCGNode subclasses             #
    # ------------------------------------------------------------------ #
    _NODE_CLASSES_BODY = r'''
import unreal, json
name_filter = PARAMS.get("name_filter")
base = getattr(unreal, "PCGNode", None)
out = []
if base is not None:
    for nm in dir(unreal):
        obj = getattr(unreal, nm, None)
        try:
            if isinstance(obj, type) and issubclass(obj, base):
                out.append(nm)
        except Exception:
            pass
if name_filter:
    f = name_filter.lower()
    out = [n for n in out if f in n.lower()]
out = sorted(out)
print("@@UMCP@@" + json.dumps({"status": "success", "base_class": "PCGNode",
    "total": len(out), "node_classes": out,
    "note": "Most PCG nodes are the generic UPCGNode paired with a UPCGSettings object; use list_pcg_settings_classes for the authorable node/settings types."}))
'''

    @mcp.tool()
    def list_pcg_node_classes(ctx, name_filter: str = None) -> str:
        """List the reflectable UPCGNode subclasses on this build (PCGNode plus specialized
        node classes such as subgraph/spawn-actor nodes). Read-only reflection.

        name_filter: case-insensitive substring on the class name.

        Note: most PCG nodes are the generic UPCGNode carrying a UPCGSettings object, so this
        list is short; use list_pcg_settings_classes for the full authorable node catalog."""
        params = {"name_filter": name_filter}
        try:
            return json.dumps(_exec(_NODE_CLASSES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
