"""UserTools :: PCG graph authoring  (spec: docs/spec/pcg.md — Wave 2)

Pure-Python authoring on the reflected UPCGGraph / UPCGNode surface (UE 5.8, PCG plugin loaded).
Every write mutates a PCGGraph ASSET (never a level) and records ONE inverse op on the per-session
agent ledger (builtins._UMCP_LEDGERS[session]); the coordinator folds each `pcg_*` op into
editor_level.undo. A non-validating save runs after each edit
(EditorLoadingAndSavingUtils.save_packages([graph.get_outermost()], False)).

NODE ADDRESSING (probed live 2026-08-19): a PCG node is addressed by node.get_name() (its stable
FName within the graph package, e.g. 'SurfaceSampler_0'), which survives save/reload — the same key
pcg.get_pcg_graph_info / get_pcg_node_info already use. The two implicit graph endpoints are NOT in
graph.nodes[]; address them with the reserved tokens 'input' / 'output' (resolved via
get_input_node() / get_output_node(); their real names are 'DefaultInputNode'/'DefaultOutputNode').

Reflected authoring API (all BlueprintCallable / probed live):
  - graph.add_node_of_type(<UPCGSettings subclass>) -> (UPCGNode, UPCGSettings)
  - graph.remove_node(node) ; graph.add_edge(from_node, from_pin, to_node, to_pin) -> node ;
    graph.remove_edge(from_node, from_pin, to_node, to_pin) -> bool ; graph.get_all_edges()
  - node.get_settings() ; node.set_node_position(x, y) [TWO floats] ; node.get_node_position()
  - PCGGraphParametersHelpers.set_<type>_parameter(graph_interface, name, value) [typed]
  - <PCGSubgraphSettings>.set_subgraph(graph)

RULES honored: no ''' and no backslashes in any snippet body; base64 PARAMS; per-session _ledger();
reserved locals (sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
success/user_code/code_obj) untouched. Scratch-only (MCP_PCG_* under /Game/MCP_Scratch).

NEW pcg_* ledger ops handed to the coordinator (op -> inverse), all pure-Python over the reflected
API (each fold: reload graph by path, resolve node by name, invert, save):
  - pcg_add_node{graph_path,node_name,settings_class} -> remove_node(node)
  - pcg_delete_node{graph_path,node_name,settings_class,position,props,edges} -> re-add node
      (add_node_of_type + restore position/props + re-wire captured edges) [LOSSY: new node name]
  - pcg_set_node_property{graph_path,node_name,prop,prior,had_prior} -> restore prior on settings
  - pcg_set_node_position{graph_path,node_name,prior_x,prior_y} -> set_node_position(prior)
  - pcg_connect{graph_path,from_node,from_pin,to_node,to_pin} -> remove_edge(...)
  - pcg_disconnect{graph_path,from_node,from_pin,to_node,to_pin} -> add_edge(...)
  - pcg_set_graph_property{graph_path,prop,prior,had_prior} -> restore prior on graph
  - pcg_layout{graph_path,prior_positions:{name:[x,y]}} -> restore each set_node_position
  - pcg_build{graph_path,node_names:[...]} -> remove_node for each (drops incident edges)
  - pcg_set_subgraph{graph_path,node_name,prior_subgraph} -> set_subgraph(prior)
  - pcg_set_graph_param{graph_path,param_name,param_type,prior,had_prior} -> set_<type>_parameter(prior)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

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


# Shared authoring helpers (prepended to every write body). No ''' / no backslashes.
_PCG_HELPERS = r'''
import unreal, json, builtins
warnings = None
try:
    import warnings as _wmod
    _wmod.simplefilter("ignore")
except Exception:
    pass

def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]

def _pcg_load_graph(p):
    a = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if a is None:
        return None, "asset not found: %s" % p
    if not isinstance(a, unreal.PCGGraph):
        cn = "?"
        try:
            cn = a.get_class().get_name()
        except Exception:
            pass
        return None, "asset is not a PCGGraph (class=%s): %s" % (cn, p)
    return a, None

_INPUT_TOKENS = ("input", "__input__", "in", "inputnode")
_OUTPUT_TOKENS = ("output", "__output__", "out", "outputnode")

def _pcg_resolve_node(g, node_id):
    if node_id is None:
        return None
    key = str(node_id)
    kl = key.lower()
    if kl in _INPUT_TOKENS:
        return g.get_input_node()
    if kl in _OUTPUT_TOKENS:
        return g.get_output_node()
    for n in list(g.get_editor_property("nodes") or []):
        try:
            if n.get_name() == key:
                return n
        except Exception:
            pass
    for getter in (g.get_input_node, g.get_output_node):
        try:
            nn = getter()
            if nn is not None and nn.get_name() == key:
                return nn
        except Exception:
            pass
    return None

def _pcg_pin_labels(node, prop):
    out = []
    try:
        for p in list(node.get_editor_property(prop) or []):
            try:
                out.append(str(p.get_editor_property("properties").get_editor_property("label")))
            except Exception:
                pass
    except Exception:
        pass
    return out

def _pcg_default_pin(node, prop):
    labels = _pcg_pin_labels(node, prop)
    return labels[0] if labels else ("Out" if prop == "output_pins" else "In")

def _pcg_node_pos(n):
    try:
        p = n.get_node_position()
    except Exception:
        return None
    if p is None:
        return None
    if hasattr(p, "x"):
        return [p.x, p.y]
    try:
        return [p[0], p[1]]
    except Exception:
        return None

def _pcg_save(g):
    try:
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([g.get_outermost()], False))
    except Exception:
        try:
            return bool(unreal.EditorAssetLibrary.save_asset(g.get_path_name().split(".")[0], only_if_is_dirty=False))
        except Exception:
            return False

def _pcg_enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s

def _pcg_settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Vector2D):
        return ([v.x, v.y], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _pcg_enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)

def _pcg_coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Vector2D) and isinstance(value, (list, tuple)) and len(value) >= 2:
        return unreal.Vector2D(float(value[0]), float(value[1]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if isinstance(current, unreal.EnumBase) and isinstance(value, dict) and "__enum__" in value:
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except Exception:
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except Exception:
            return current
    return value

def _pcg_resolve_settings_class(name):
    # accept 'PCGSurfaceSamplerSettings', 'SurfaceSampler', 'surface_sampler'...
    base = getattr(unreal, "PCGSettings", None)
    if base is None or not name:
        return None
    cand = getattr(unreal, name, None)
    def _ok(c):
        try:
            return isinstance(c, type) and issubclass(c, base) and c is not base
        except Exception:
            return False
    if _ok(cand):
        return cand
    compact = str(name).replace("_", "").replace(" ", "").lower()
    for nm in dir(unreal):
        c = getattr(unreal, nm, None)
        if not _ok(c):
            continue
        nml = nm.lower()
        if nml == compact or nml == "pcg" + compact or nml == compact + "settings" or nml == "pcg" + compact + "settings":
            return c
    for nm in dir(unreal):
        c = getattr(unreal, nm, None)
        if _ok(c) and compact in nm.lower():
            return c
    return None
'''


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

    def _scratch_guard(asset_path):
        if not asset_path:
            return "asset_path (graph_path) is required"
        if not asset_path.startswith("/Game/MCP_Scratch"):
            return "graph must be under /Game/MCP_Scratch (scratch-only); got %s" % asset_path
        return None

    # ------------------------------------------------------------------ #
    # 1. add_pcg_node                                                     #
    # ------------------------------------------------------------------ #
    _ADD_NODE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
cls_name = PARAMS.get("settings_class")
pos = PARAMS.get("position")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls = _pcg_resolve_settings_class(cls_name)
    if cls is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown PCGSettings class: %s" % cls_name}))
    else:
        r = g.add_node_of_type(cls)
        node = None; sett = None
        if isinstance(r, (list, tuple)):
            for x in r:
                if isinstance(x, unreal.PCGNode):
                    node = x
                elif isinstance(x, unreal.PCGSettings):
                    sett = x
        elif isinstance(r, unreal.PCGNode):
            node = r
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_node_of_type returned no node"}))
        else:
            if pos and isinstance(pos, (list, tuple)) and len(pos) >= 2:
                try:
                    node.set_node_position(float(pos[0]), float(pos[1]))
                except Exception:
                    pass
            if sett is None:
                sett = node.get_settings()
            _ledger().append({"op": "pcg_add_node", "graph_path": gp,
                              "node_name": node.get_name(), "settings_class": cls.__name__})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "node_name": node.get_name(), "node_class": node.get_class().get_name(),
                "settings_class": (sett.get_class().get_name() if sett else None),
                "position": _pcg_node_pos(node),
                "input_pins": _pcg_pin_labels(node, "input_pins"),
                "output_pins": _pcg_pin_labels(node, "output_pins"),
                "undo_op": "pcg_add_node", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_pcg_node(ctx, graph_path: str, settings_class: str, position: list = None) -> str:
        """Add a node to a PCGGraph (add_node_of_type on the resolved UPCGSettings subclass).
        Ledgered write (op 'pcg_add_node' -> `undo` removes the node).

        graph_path:     content path to the PCGGraph (scratch-only).
        settings_class: a PCGSettings subclass name (e.g. 'PCGSurfaceSamplerSettings'); short forms
                        like 'SurfaceSampler' are resolved too.
        position:       optional [x, y] canvas position.

        Returns the node's addressing id (node_name = node.get_name()) plus its pin labels. Set node
        values with set_pcg_node_property; wire it with connect_pcg_pins."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_ADD_NODE_BODY,
                {"graph_path": graph_path, "settings_class": settings_class, "position": position}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. delete_pcg_node                                                  #
    # ------------------------------------------------------------------ #
    _DELETE_NODE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None or node in (g.get_input_node(), g.get_output_node()):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "node not found or is a protected input/output node: %s" % node_id}))
    else:
        s = node.get_settings()
        settings_class = s.get_class().get_name() if s else None
        pos = _pcg_node_pos(node)
        # capture node-specific props for a best-effort re-add
        props = {}
        base = getattr(unreal, "PCGSettings", None)
        if s is not None and base is not None:
            base_names = set()
            for klass in base.__mro__:
                for nm2, val2 in vars(klass).items():
                    if not nm2.startswith("__") and type(val2).__name__ in ("getset_descriptor", "property"):
                        base_names.add(nm2)
            for nm2 in dir(type(s)):
                if nm2.startswith("__") or nm2 in base_names:
                    continue
                if type(getattr(type(s), nm2, None)).__name__ not in ("getset_descriptor", "property"):
                    continue
                try:
                    sv, ok = _pcg_settable(s.get_editor_property(nm2))
                    if ok:
                        props[nm2] = sv
                except Exception:
                    pass
        # capture incident edges (both directions) for best-effort re-wire
        edges = []
        this_name = node.get_name()
        for e in list(g.get_all_edges() or []):
            try:
                fn = e.get_input_node(); tn = e.get_output_node()
                fnn = fn.get_name() if fn else None
                tnn = tn.get_name() if tn else None
                if fnn == this_name or tnn == this_name:
                    edges.append({"from_node": fnn, "from_pin": str(e.get_input_pin_label()),
                                  "to_node": tnn, "to_pin": str(e.get_output_pin_label())})
            except Exception:
                pass
        g.remove_node(node)
        _ledger().append({"op": "pcg_delete_node", "graph_path": gp, "node_name": this_name,
                          "settings_class": settings_class, "position": pos,
                          "props": props, "edges": edges})
        _pcg_save(g)
        print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
            "deleted_node": this_name, "settings_class": settings_class,
            "captured_props": len(props), "captured_edges": len(edges),
            "node_count_after": len(list(g.get_editor_property("nodes") or [])),
            "undo_op": "pcg_delete_node",
            "undo_note": "re-add is best-effort (restored node gets a new internal name)",
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_pcg_node(ctx, graph_path: str, node_name: str) -> str:
        """Remove a node from a PCGGraph (remove_node). Ledgered write (op 'pcg_delete_node').

        graph_path: content path to the PCGGraph (scratch-only).
        node_name:  the node's addressing id (from add_pcg_node / get_pcg_graph_info).

        `undo` is BEST-EFFORT: it re-creates the node (same settings class, captured position and
        node-specific properties) and re-wires the captured incident edges, but the restored node
        receives a NEW internal name. The DefaultInput/Output nodes cannot be deleted."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_DELETE_NODE_BODY,
                {"graph_path": graph_path, "node_name": node_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. set_pcg_node_property                                            #
    # ------------------------------------------------------------------ #
    _SET_NODE_PROP_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
prop = PARAMS.get("property")
value = PARAMS.get("value")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found: %s" % node_id}))
    else:
        s = node.get_settings()
        if s is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "node has no settings object"}))
        else:
            had_prior = True; prior_settable = None
            try:
                cur = s.get_editor_property(prop)
                prior_settable, _ok = _pcg_settable(cur)
            except Exception as _e:
                had_prior = False; cur = None
            try:
                newv = _pcg_coerce(cur, value)
                s.set_editor_property(prop, newv)
            except Exception as _e:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "set_editor_property failed for %s: %s" % (prop, _e)}))
                newv = "__ERR__"
            if newv != "__ERR__":
                readback, _ok2 = _pcg_settable(s.get_editor_property(prop))
                _ledger().append({"op": "pcg_set_node_property", "graph_path": gp,
                                  "node_name": node.get_name(), "prop": prop,
                                  "prior": prior_settable, "had_prior": had_prior})
                _pcg_save(g)
                print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                    "node_name": node.get_name(), "property": prop,
                    "prior": prior_settable, "new_value": readback, "had_prior": had_prior,
                    "undo_op": "pcg_set_node_property", "ledger_depth": len(_ledger())}, default=str))
'''

    @mcp.tool()
    def set_pcg_node_property(ctx, graph_path: str, node_name: str, property: str, value) -> str:
        """Set a property on a PCG node's settings object (set_editor_property on node.get_settings()).
        Captures the prior value. Ledgered write (op 'pcg_set_node_property' -> `undo` restores prior).

        graph_path: content path to the PCGGraph (scratch-only).
        node_name:  the node's addressing id.
        property:   a settings UPROPERTY name (see get_pcg_node_type_info /
                    get_pcg_property_valid_values). Enums are passed by name.
        value:      the new value (primitive / [x,y,z] / enum name / asset path for object refs)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_NODE_PROP_BODY,
                {"graph_path": graph_path, "node_name": node_name,
                 "property": property, "value": value}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. set_pcg_node_position                                            #
    # ------------------------------------------------------------------ #
    _SET_NODE_POS_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
x = PARAMS.get("x"); y = PARAMS.get("y")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found: %s" % node_id}))
    else:
        prior = _pcg_node_pos(node) or [0.0, 0.0]
        try:
            node.set_node_position(float(x), float(y))
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_node_position failed: %s" % _e}))
        else:
            _ledger().append({"op": "pcg_set_node_position", "graph_path": gp,
                              "node_name": node.get_name(),
                              "prior_x": prior[0], "prior_y": prior[1]})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "node_name": node.get_name(), "prior_position": prior,
                "new_position": _pcg_node_pos(node),
                "undo_op": "pcg_set_node_position", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_pcg_node_position(ctx, graph_path: str, node_name: str, x: float, y: float) -> str:
        """Set a PCG node's canvas position (set_node_position takes two floats). Captures the prior
        position. Ledgered write (op 'pcg_set_node_position' -> `undo` restores it).

        graph_path/node_name: address the node. x, y: new canvas coordinates."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_NODE_POS_BODY,
                {"graph_path": graph_path, "node_name": node_name, "x": x, "y": y}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5. connect_pcg_pins                                                 #
    # ------------------------------------------------------------------ #
    _CONNECT_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
fid = PARAMS.get("from_node"); tid = PARAMS.get("to_node")
fpin = PARAMS.get("from_pin"); tpin = PARAMS.get("to_pin")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    fnode = _pcg_resolve_node(g, fid); tnode = _pcg_resolve_node(g, tid)
    if fnode is None or tnode is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "node not found (from=%s->%s, to=%s->%s)" % (fid, fnode is not None, tid, tnode is not None)}))
    else:
        if not fpin:
            fpin = _pcg_default_pin(fnode, "output_pins")
        if not tpin:
            tpin = _pcg_default_pin(tnode, "input_pins")
        before = len(list(g.get_all_edges() or []))
        g.add_edge(fnode, fpin, tnode, tpin)
        after = len(list(g.get_all_edges() or []))
        # store the caller's original tokens so the inverse re-resolves input/output faithfully
        _ledger().append({"op": "pcg_connect", "graph_path": gp,
                          "from_node": str(fid), "from_pin": fpin,
                          "to_node": str(tid), "to_pin": tpin})
        _pcg_save(g)
        print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
            "from_node": fnode.get_name(), "from_pin": fpin,
            "to_node": tnode.get_name(), "to_pin": tpin,
            "edge_count_before": before, "edge_count_after": after,
            "edge_added": after > before,
            "undo_op": "pcg_connect", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_pcg_pins(ctx, graph_path: str, from_node: str, to_node: str,
                         from_pin: str = None, to_pin: str = None) -> str:
        """Connect two PCG node pins (add_edge). Ledgered write (op 'pcg_connect' -> `undo` removes
        the edge).

        graph_path: content path to the PCGGraph (scratch-only).
        from_node / to_node: node addressing ids, or the reserved tokens 'input' / 'output' for the
                    graph's DefaultInput/Output endpoints.
        from_pin:   output pin label on from_node (defaults to its first output pin, usually 'Out').
        to_pin:     input pin label on to_node (defaults to its first input pin, usually 'In')."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_CONNECT_BODY,
                {"graph_path": graph_path, "from_node": from_node, "to_node": to_node,
                 "from_pin": from_pin, "to_pin": to_pin}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 6. disconnect_pcg_edge                                              #
    # ------------------------------------------------------------------ #
    _DISCONNECT_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
fid = PARAMS.get("from_node"); tid = PARAMS.get("to_node")
fpin = PARAMS.get("from_pin"); tpin = PARAMS.get("to_pin")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    fnode = _pcg_resolve_node(g, fid); tnode = _pcg_resolve_node(g, tid)
    if fnode is None or tnode is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found (from=%s to=%s)" % (fid, tid)}))
    else:
        if not fpin:
            fpin = _pcg_default_pin(fnode, "output_pins")
        if not tpin:
            tpin = _pcg_default_pin(tnode, "input_pins")
        before = len(list(g.get_all_edges() or []))
        ok = g.remove_edge(fnode, fpin, tnode, tpin)
        after = len(list(g.get_all_edges() or []))
        if after < before:
            _ledger().append({"op": "pcg_disconnect", "graph_path": gp,
                              "from_node": str(fid), "from_pin": fpin,
                              "to_node": str(tid), "to_pin": tpin})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "from_node": fnode.get_name(), "from_pin": fpin,
                "to_node": tnode.get_name(), "to_pin": tpin,
                "removed": True, "edge_count_before": before, "edge_count_after": after,
                "undo_op": "pcg_disconnect", "ledger_depth": len(_ledger())}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "from_node": fnode.get_name(), "to_node": tnode.get_name(),
                "removed": False, "note": "no matching edge to remove (not ledgered)",
                "remove_edge_return": str(ok)}))
'''

    @mcp.tool()
    def disconnect_pcg_edge(ctx, graph_path: str, from_node: str, to_node: str,
                            from_pin: str = None, to_pin: str = None) -> str:
        """Remove an edge between two PCG node pins (remove_edge). Ledgered write only if an edge was
        actually removed (op 'pcg_disconnect' -> `undo` re-adds the edge).

        graph_path / from_node / to_node / from_pin / to_pin: as connect_pcg_pins."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_DISCONNECT_BODY,
                {"graph_path": graph_path, "from_node": from_node, "to_node": to_node,
                 "from_pin": from_pin, "to_pin": to_pin}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 7. set_pcg_graph_property                                           #
    # ------------------------------------------------------------------ #
    _SET_GRAPH_PROP_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
prop = PARAMS.get("property")
value = PARAMS.get("value")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    had_prior = True; prior_settable = None; cur = None
    try:
        cur = g.get_editor_property(prop)
        prior_settable, _ok = _pcg_settable(cur)
    except Exception:
        had_prior = False
    try:
        newv = _pcg_coerce(cur, value)
        g.set_editor_property(prop, newv)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_editor_property failed for %s: %s" % (prop, _e)}))
        newv = "__ERR__"
    if newv != "__ERR__":
        readback, _ok2 = _pcg_settable(g.get_editor_property(prop))
        _ledger().append({"op": "pcg_set_graph_property", "graph_path": gp, "prop": prop,
                          "prior": prior_settable, "had_prior": had_prior})
        _pcg_save(g)
        print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp, "property": prop,
            "prior": prior_settable, "new_value": readback, "had_prior": had_prior,
            "undo_op": "pcg_set_graph_property", "ledger_depth": len(_ledger())}, default=str))
'''

    @mcp.tool()
    def set_pcg_graph_property(ctx, graph_path: str, property: str, value) -> str:
        """Set a graph-level property on a PCGGraph (set_editor_property on the graph, e.g.
        'category', 'description'). Captures prior. Ledgered write (op 'pcg_set_graph_property' ->
        `undo` restores prior).

        graph_path: content path to the PCGGraph (scratch-only).
        property:   a PCGGraph UPROPERTY name. value: the new value."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_GRAPH_PROP_BODY,
                {"graph_path": graph_path, "property": property, "value": value}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. layout_pcg_graph                                                 #
    # ------------------------------------------------------------------ #
    _LAYOUT_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
x_spacing = float(PARAMS.get("x_spacing") or 320.0)
y_spacing = float(PARAMS.get("y_spacing") or 180.0)
x0 = float(PARAMS.get("x0") or 0.0)
y0 = float(PARAMS.get("y0") or 0.0)
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    nodes = list(g.get_editor_property("nodes") or [])
    inn = g.get_input_node(); onn = g.get_output_node()
    # simple topological depth (columns) via edge source->target; fall back to insertion order.
    name_to_node = {}
    for n in nodes:
        try:
            name_to_node[n.get_name()] = n
        except Exception:
            pass
    succ = {}; indeg = {}
    for nm in name_to_node:
        succ[nm] = []; indeg[nm] = 0
    for e in list(g.get_all_edges() or []):
        try:
            fn = e.get_input_node(); tn = e.get_output_node()
            fnn = fn.get_name() if fn else None; tnn = tn.get_name() if tn else None
            if fnn in name_to_node and tnn in name_to_node:
                succ[fnn].append(tnn); indeg[tnn] = indeg.get(tnn, 0) + 1
        except Exception:
            pass
    # Kahn layering
    layer = {}
    frontier = [nm for nm in name_to_node if indeg.get(nm, 0) == 0]
    seen = set()
    cur_layer = 0
    while frontier:
        nxt = []
        for nm in frontier:
            if nm in seen:
                continue
            seen.add(nm)
            layer[nm] = max(layer.get(nm, 0), cur_layer)
            for s2 in succ.get(nm, []):
                if s2 not in seen:
                    nxt.append(s2)
        frontier = nxt
        cur_layer += 1
        if cur_layer > len(name_to_node) + 2:
            break
    for nm in name_to_node:
        layer.setdefault(nm, 0)
    # prior positions (nodes + endpoints) for undo
    prior_positions = {}
    for nm, n in name_to_node.items():
        prior_positions[nm] = _pcg_node_pos(n) or [0.0, 0.0]
    for endp in (inn, onn):
        if endp is not None:
            try:
                prior_positions[endp.get_name()] = _pcg_node_pos(endp) or [0.0, 0.0]
            except Exception:
                pass
    # assign positions per layer
    col_counts = {}
    applied = {}
    for nm in sorted(name_to_node.keys(), key=lambda k: (layer[k], k)):
        col = layer[nm]
        row = col_counts.get(col, 0); col_counts[col] = row + 1
        px = x0 + col * x_spacing; py = y0 + row * y_spacing
        try:
            name_to_node[nm].set_node_position(px, py)
            applied[nm] = [px, py]
        except Exception:
            pass
    # place input at far left, output at far right
    max_layer = max(layer.values()) if layer else 0
    if inn is not None:
        try:
            inn.set_node_position(x0 - x_spacing, y0)
            applied[inn.get_name()] = [x0 - x_spacing, y0]
        except Exception:
            pass
    if onn is not None:
        try:
            onn.set_node_position(x0 + (max_layer + 1) * x_spacing, y0)
            applied[onn.get_name()] = [x0 + (max_layer + 1) * x_spacing, y0]
        except Exception:
            pass
    _ledger().append({"op": "pcg_layout", "graph_path": gp, "prior_positions": prior_positions})
    _pcg_save(g)
    print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
        "nodes_positioned": len(applied), "layers": (max_layer + 1),
        "applied_positions": applied,
        "undo_op": "pcg_layout", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def layout_pcg_graph(ctx, graph_path: str, x_spacing: float = 320.0,
                         y_spacing: float = 180.0, x0: float = 0.0, y0: float = 0.0) -> str:
        """Auto-layout a PCGGraph: computes a left-to-right topological (Kahn-layered) grid from the
        edges and applies set_node_position to every node (input at far left, output at far right).
        Captures all prior positions. Ledgered write (op 'pcg_layout' -> `undo` restores them).

        graph_path: content path to the PCGGraph (scratch-only).
        x_spacing/y_spacing: column/row pitch. x0/y0: origin."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_LAYOUT_BODY,
                {"graph_path": graph_path, "x_spacing": x_spacing, "y_spacing": y_spacing,
                 "x0": x0, "y0": y0}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. build_pcg_graph — atomic add nodes + edges + layout              #
    # ------------------------------------------------------------------ #
    _BUILD_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
spec_nodes = PARAMS.get("nodes") or []
spec_edges = PARAMS.get("edges") or []
do_layout = bool(PARAMS.get("layout", True))
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    id_to_name = {}
    created = []
    problems = []
    x_spacing = 320.0; y_spacing = 180.0
    col = 0
    for spec in spec_nodes:
        lid = str(spec.get("id"))
        cls = _pcg_resolve_settings_class(spec.get("class"))
        if cls is None:
            problems.append("unknown class for id %s: %s" % (lid, spec.get("class"))); continue
        r = g.add_node_of_type(cls)
        node = None; sett = None
        if isinstance(r, (list, tuple)):
            for x in r:
                if isinstance(x, unreal.PCGNode):
                    node = x
                elif isinstance(x, unreal.PCGSettings):
                    sett = x
        elif isinstance(r, unreal.PCGNode):
            node = r
        if node is None:
            problems.append("add failed for id %s" % lid); continue
        if sett is None:
            sett = node.get_settings()
        # apply props
        for pk, pv in (spec.get("props") or {}).items():
            try:
                curv = sett.get_editor_property(pk)
                sett.set_editor_property(pk, _pcg_coerce(curv, pv))
            except Exception as _e:
                problems.append("prop %s on %s: %s" % (pk, lid, _e))
        # position
        pos = spec.get("position")
        if pos and isinstance(pos, (list, tuple)) and len(pos) >= 2:
            try:
                node.set_node_position(float(pos[0]), float(pos[1]))
            except Exception:
                pass
        elif do_layout:
            try:
                node.set_node_position(col * x_spacing, 0.0)
            except Exception:
                pass
            col += 1
        id_to_name[lid] = node.get_name()
        created.append(node.get_name())
    # edges
    edges_made = []
    for e in spec_edges:
        fid = e.get("from"); tid = e.get("to")
        fnode = _pcg_resolve_node(g, id_to_name.get(str(fid), fid))
        tnode = _pcg_resolve_node(g, id_to_name.get(str(tid), tid))
        if fnode is None or tnode is None:
            problems.append("edge endpoint missing (%s->%s)" % (fid, tid)); continue
        fpin = e.get("from_pin") or _pcg_default_pin(fnode, "output_pins")
        tpin = e.get("to_pin") or _pcg_default_pin(tnode, "input_pins")
        try:
            g.add_edge(fnode, fpin, tnode, tpin)
            edges_made.append({"from": fnode.get_name(), "from_pin": fpin,
                               "to": tnode.get_name(), "to_pin": tpin})
        except Exception as _e:
            problems.append("edge %s->%s: %s" % (fid, tid, _e))
    _ledger().append({"op": "pcg_build", "graph_path": gp, "node_names": created})
    _pcg_save(g)
    print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
        "nodes_created": len(created), "id_to_name": id_to_name,
        "edges_created": len(edges_made), "edges": edges_made,
        "edge_count_total": len(list(g.get_all_edges() or [])),
        "problems": problems,
        "undo_op": "pcg_build", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def build_pcg_graph(ctx, graph_path: str, nodes: list, edges: list = None,
                        layout: bool = True) -> str:
        """Atomically build out a PCGGraph in one call: add many nodes, set their properties, wire
        edges, and lay them out. Ledgered as a SINGLE op ('pcg_build' -> `undo` removes every node
        it created, which also drops all incident edges).

        graph_path: content path to the PCGGraph (scratch-only).
        nodes: list of {id (local ref), class (PCGSettings name), props? {prop:value}, position? [x,y]}.
        edges: list of {from, to, from_pin?, to_pin?} where from/to are local ids (or 'input'/'output').
        layout: auto-spread nodes left-to-right when a node has no explicit position (default True).

        Returns id_to_name (local id -> real node addressing id) for follow-up edits."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not nodes:
                return json.dumps({"status": "error", "message": "nodes list is required"}, indent=2)
            return json.dumps(_exec(_BUILD_BODY,
                {"graph_path": graph_path, "nodes": nodes, "edges": edges or [],
                 "layout": layout}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 10. set_pcg_subgraph_target                                         #
    # ------------------------------------------------------------------ #
    _SET_SUBGRAPH_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
target = PARAMS.get("subgraph")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found: %s" % node_id}))
    else:
        s = node.get_settings()
        if s is None or not hasattr(s, "set_subgraph"):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "node is not a subgraph node (settings has no set_subgraph): %s" % (s.get_class().get_name() if s else None)}))
        else:
            # The 'Subgraph' UPROPERTY is protected/unreadable; the assigned target is the inner
            # `graph` of the settings' subgraph_instance (a UPCGGraphInstance). Capture/read there.
            def _ig_path(setobj):
                try:
                    _inst = setobj.get_editor_property("subgraph_instance")
                    if _inst is None:
                        return None
                    _ig = _inst.get_editor_property("graph")
                    return _ig.get_path_name().split(".")[0] if _ig else None
                except Exception:
                    return None
            prior_path = _ig_path(s)
            tgt = unreal.EditorAssetLibrary.load_asset(target) if target else None
            if target and (tgt is None or not isinstance(tgt, unreal.PCGGraph)):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "subgraph target is not a PCGGraph: %s" % target}))
            else:
                s.set_subgraph(tgt)
                _ledger().append({"op": "pcg_set_subgraph", "graph_path": gp,
                                  "node_name": node.get_name(), "prior_subgraph": prior_path})
                _pcg_save(g)
                rb = _ig_path(s)
                print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                    "node_name": node.get_name(), "subgraph": target, "subgraph_readback": rb,
                    "prior_subgraph": prior_path,
                    "undo_op": "pcg_set_subgraph", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_pcg_subgraph_target(ctx, graph_path: str, node_name: str, subgraph: str) -> str:
        """Set the target graph of a PCG subgraph node (PCGSubgraphSettings.set_subgraph). Captures
        the prior target. Ledgered write (op 'pcg_set_subgraph' -> `undo` restores it).

        graph_path: content path to the host PCGGraph (scratch-only).
        node_name:  a subgraph node's addressing id (add one first with
                    add_pcg_node graph_path 'PCGSubgraphSettings').
        subgraph:   content path to the target PCGGraph to nest."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_SUBGRAPH_BODY,
                {"graph_path": graph_path, "node_name": node_name, "subgraph": subgraph}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 11. set_pcg_graph_parameter_value — PCGGraphParametersHelpers        #
    # ------------------------------------------------------------------ #
    _SET_GRAPH_PARAM_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
pname = PARAMS.get("param_name")
ptype = (PARAMS.get("param_type") or "").lower()
value = PARAMS.get("value")
g, err = _pcg_load_graph(gp)
H = getattr(unreal, "PCGGraphParametersHelpers", None)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif H is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGGraphParametersHelpers not present (reflected-gated)"}))
else:
    # verify the parameter exists (schema creation is not in scope)
    exists = False
    try:
        up = g.get_editor_property("user_parameters")
        d = up.to_dict() if (up is not None and hasattr(up, "to_dict")) else {}
        exists = pname in (d or {})
    except Exception:
        d = {}
    typemap = {
        "float": ("get_float_parameter", "set_float_parameter", float),
        "double": ("get_double_parameter", "set_double_parameter", float),
        "int": ("get_int32_parameter", "set_int32_parameter", int),
        "int32": ("get_int32_parameter", "set_int32_parameter", int),
        "int64": ("get_int64_parameter", "set_int64_parameter", int),
        "bool": ("get_bool_parameter", "set_bool_parameter", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)),
        "string": ("get_string_parameter", "set_string_parameter", str),
        "name": ("get_name_parameter", "set_name_parameter", str),
    }
    if ptype not in typemap:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unsupported param_type '%s' (float/double/int/int64/bool/string/name)" % ptype}))
    elif not exists:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "graph parameter '%s' does not exist (defining new graph parameters is a Wave-5 C++ item); existing: %s" % (pname, list((d or {}).keys()))}))
    else:
        getn, setn, conv = typemap[ptype]
        getter = getattr(H, getn, None); setter = getattr(H, setn, None)
        prior = None; had_prior = False
        if getter is not None:
            try:
                prior = getter(g, pname); had_prior = True
            except Exception:
                had_prior = False
        try:
            setter(g, pname, conv(value))
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_%s_parameter failed: %s" % (ptype, _e)}))
            setter = None
        if setter is not None:
            newv = None
            if getter is not None:
                try:
                    newv = getter(g, pname)
                except Exception:
                    pass
            _ledger().append({"op": "pcg_set_graph_param", "graph_path": gp, "param_name": pname,
                              "param_type": ptype, "set_method": setn, "prior": prior,
                              "had_prior": had_prior})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "param_name": pname, "param_type": ptype, "prior": prior, "new_value": newv,
                "had_prior": had_prior,
                "undo_op": "pcg_set_graph_param", "ledger_depth": len(_ledger())}, default=str))
'''

    @mcp.tool()
    def set_pcg_graph_parameter_value(ctx, graph_path: str, param_name: str,
                                      param_type: str, value) -> str:
        """Set the value of an EXISTING exposed graph parameter (PCGGraphParametersHelpers typed
        setters). Captures prior. Ledgered write (op 'pcg_set_graph_param' -> `undo` restores prior).

        graph_path: content path to the PCGGraph (scratch-only).
        param_name: the exposed user-parameter name (must already exist in user_parameters).
        param_type: one of float/double/int/int64/bool/string/name.
        value:      the new value.

        Reflected-gated: DEFINING new graph parameters (schema) is a Wave-5 C++ item — this only sets
        values of parameters that already exist; returns a clear error otherwise."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_GRAPH_PARAM_BODY,
                {"graph_path": graph_path, "param_name": param_name,
                 "param_type": param_type, "value": value}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 12. trace_pcg_connection — READ (no ledger)                         #
    # ------------------------------------------------------------------ #
    _TRACE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
direction = (PARAMS.get("direction") or "both").lower()
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found: %s" % node_id}))
    else:
        this_name = node.get_name()
        upstream = []; downstream = []
        for e in list(g.get_all_edges() or []):
            try:
                fn = e.get_input_node(); tn = e.get_output_node()
                fnn = fn.get_name() if fn else None; tnn = tn.get_name() if tn else None
                rec = {"from_node": fnn, "from_pin": str(e.get_input_pin_label()),
                       "to_node": tnn, "to_pin": str(e.get_output_pin_label())}
                if tnn == this_name:
                    upstream.append(rec)
                if fnn == this_name:
                    downstream.append(rec)
            except Exception:
                pass
        res = {"status": "success", "graph_path": gp, "node_name": this_name}
        if direction in ("both", "upstream", "in"):
            res["upstream"] = upstream
        if direction in ("both", "downstream", "out"):
            res["downstream"] = downstream
        res["upstream_count"] = len(upstream); res["downstream_count"] = len(downstream)
        print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def trace_pcg_connection(ctx, graph_path: str, node_name: str, direction: str = "both") -> str:
        """Trace the edges incident to a PCG node — its upstream (incoming) and/or downstream
        (outgoing) connections. Read-only, no ledger.

        graph_path: content path to the PCGGraph.
        node_name:  node addressing id (or 'input'/'output').
        direction:  'both' (default), 'upstream'/'in', or 'downstream'/'out'."""
        try:
            return json.dumps(_exec(_TRACE_BODY,
                {"graph_path": graph_path, "node_name": node_name, "direction": direction}), indent=2)
        except Exception as e:
            return f"Error: {e}"
