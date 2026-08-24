"""UserTools :: Mutable (CustomizableObject) SOURCE-GRAPH secondary/composite tools (mutable Wave 3).

PURE PYTHON composed over the already-built + verified C++ #46 graph primitives exposed on
unreal.MCPReflectionLibrary (get_mutable_graph_json / get_mutable_node_json / add_mutable_node_json /
connect_mutable_nodes_json / disconnect_mutable_pin_json / delete_mutable_node_json /
set_mutable_node_property_json). NO new C++ -- every tool here just REPLAYS those primitives and
appends the SAME per-primitive inverse ledger entries the standalone mutable_graph_cpp.py tools
append, so editor_level.undo unwinds each with folds that ALREADY exist:
    delete_mutable_node        (inverse of an add)
    set_mutable_node_property  (inverse of a property set -- restores captured prior text)
    disconnect_mutable_pin     (inverse of a connect)
No NEW undo fold is introduced by this module.

Scaffolding (query convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo
ledger, NON-VALIDATING save) is copied from the gold-standard mutable_graph_cpp.py. The
non-validating save mirrors mutable_write.py._save_nonvalidating (the validating save path can
hard-crash on freshly-authored Mutable assets); NO CO compile is triggered (compile once, after
authoring, via compile_customizable_object).

TOOLS
  build_mutable_graph(co_path, nodes, connections)   -- whole-graph build in ONE call. `nodes` =
      [{id, node_class, x?, y?, properties?}]; `connections` = [{from_id, from_pin, to_id, to_pin}].
      Keeps a user-id -> GUID map (from_id/to_id may also be a raw GUID). Reports per-connection
      schema type-mismatch WITHOUT aborting the build. Ledgers one delete_mutable_node per created
      node, one set_mutable_node_property per property, one disconnect_mutable_pin per successful
      connection -- all EXISTING folds; undo unwinds each newest-first.
  layout_mutable_graph(co_path, positions=None, auto=True) -- set node graph positions via the
      reflected UEdGraphNode ints NodePosX / NodePosY (PROVEN settable through
      set_mutable_node_property_json). `positions` = {node_guid: [x,y]} or auto-grid all nodes.
      Ledgers a set_mutable_node_property per axis per node (existing fold).
  set_mutable_parameter_ui_metadata(co_path, node_guid, metadata) -- set a parameter node's
      ParamUIMetadata struct (proven on FloatParameter as import-text `(...)`). `metadata` = a full
      struct import-text STRING (controls every field) OR a {sub_field: scalar} dict (sets only the
      named fields; unspecified fields follow UE struct-ImportText semantics). Ledgers
      set_mutable_node_property (existing fold; captured prior restores exactly on undo).
  list_mutable_child_objects(co_path)                 -- READ (no ledger): walk the graph for group
      nodes (CustomizableObjectNodeObjectGroup) + object nodes (CustomizableObjectNodeObject) and
      report the child<->group<->parent wiring.
  link_mutable_child_object(co_path, group_guid, child_guid) -- connect a child object node's object
      OUTPUT pin into a group node's object-array INPUT pin (pins auto-detected by category). Ledgers
      disconnect_mutable_pin (existing fold).
  add_mutable_node_pin(co_path, node_guid)            -- PROBE-and-degrade: Mutable switch pins are
      enum-driven and array pins (group Objects / base Children) grow implicitly on connect, so no
      node exposes a settable pin-count UPROPERTY. If a node DOES expose an int count-like property,
      it is bumped (reusing set_mutable_node_property); otherwise the tool honestly reports the
      capability is node-type-gated (needs node-specific C++) and changes nothing.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO backslashes; all data crosses as base64. Never assign a
snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
success/user_code/code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from mutable_graph_cpp.py) --------
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
    _HELP = r'''
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ Mutable graph handlers on "
            "unreal.MCPReflectionLibrary (rebuild the UnrealMCP plugin DLL with MCPReflection_Mutable.cpp).")}
def _save_nonvalidating(co_path):
    try:
        a = unreal.EditorAssetLibrary.load_asset(co_path)
        if a is None:
            return False
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([a.get_outermost()], False))
    except Exception:
        return False
def _pv_to_text(v):
    # Property value -> ImportText string. Structs/containers: pass a ready struct-text STRING.
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return ""
    return str(v)
def _field_text(v):
    # One struct sub-field value -> struct import-text token (no backslash: quote with chr(34)).
    q = chr(34)
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    return q + str(v) + q
def _read_graph(co):
    rl = _mrl("get_mutable_graph_json")
    if rl is None:
        return None
    g = _decode(rl.get_mutable_graph_json(co))
    return g if isinstance(g, dict) else None
def _find_pin(node, direction, category, container=None):
    for p in (node.get("pins") or []):
        pt = p.get("pin_type", {}) or {}
        if p.get("direction") == direction and pt.get("category") == category and (container is None or pt.get("container") == container):
            return p.get("name")
    return None
'''

    # ================================================================== #
    # build_mutable_graph                                                 #
    # ================================================================== #
    _BUILD_BODY = _HELP + r'''
co = PARAMS["co_path"]
nodes = PARAMS.get("nodes") or []
conns = PARAMS.get("connections") or []
rl = _mrl("add_mutable_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("build_mutable_graph")))
else:
    led = _ledger()
    ledger_added = 0
    idmap = {}
    created = []
    node_errors = []
    prop_results = []
    for spec in nodes:
        if not isinstance(spec, dict):
            node_errors.append({"spec": str(spec)[:120], "error": "node spec must be an object"}); continue
        uid = spec.get("id")
        ncls = spec.get("node_class") or spec.get("class")
        if not ncls:
            node_errors.append({"id": uid, "error": "missing node_class"}); continue
        try:
            x = float(spec.get("x", 0.0)); y = float(spec.get("y", 0.0))
        except Exception:
            x = 0.0; y = 0.0
        r = _decode(rl.add_mutable_node_json(co, ncls, x, y))
        if isinstance(r, dict) and r.get("error"):
            node_errors.append({"id": uid, "node_class": ncls, "error": r.get("error")}); continue
        guid = r.get("node_guid")
        if uid is not None:
            idmap[str(uid)] = guid
        led.append({"op": "delete_mutable_node", "asset_path": co, "node_guid": guid}); ledger_added += 1
        rec = {"id": uid, "node_guid": guid, "class": r.get("class"), "title": r.get("title"), "x": x, "y": y}
        created.append(rec)
        # per-node properties (each reuses the set_mutable_node_property fold)
        props = spec.get("properties") or {}
        if isinstance(props, dict) and hasattr(rl, "set_mutable_node_property_json"):
            for pk in props:
                pr = _decode(rl.set_mutable_node_property_json(co, guid, pk, _pv_to_text(props[pk])))
                if isinstance(pr, dict) and pr.get("error"):
                    prop_results.append({"id": uid, "property": pk, "error": pr.get("error")})
                else:
                    led.append({"op": "set_mutable_node_property", "asset_path": co,
                        "node_guid": pr.get("node_guid") or guid, "property": pr.get("property") or pk,
                        "prev_value": pr.get("prev_value")}); ledger_added += 1
                    prop_results.append({"id": uid, "property": pr.get("property") or pk,
                        "prev_value": pr.get("prev_value"), "new_value": pr.get("new_value")})
    # connections -- schema rejections are reported, NOT aborted
    conn_results = []
    made = 0
    can_conn = hasattr(rl, "connect_mutable_nodes_json")
    for c in conns:
        if not isinstance(c, dict):
            conn_results.append({"spec": str(c)[:120], "connected": False, "error": "connection spec must be an object"}); continue
        fid = str(c.get("from_id")); tid = str(c.get("to_id"))
        fg = idmap.get(fid, fid); tg = idmap.get(tid, tid)
        fp = c.get("from_pin"); tp = c.get("to_pin")
        if not can_conn:
            conn_results.append({"from": fid, "to": tid, "connected": False, "error": "connect handler absent"}); continue
        cr = _decode(rl.connect_mutable_nodes_json(co, fg, fp, tg, tp))
        if isinstance(cr, dict) and cr.get("error"):
            conn_results.append({"from": fid, "to": tid, "from_pin": fp, "to_pin": tp,
                "connected": False, "error": cr.get("error")})
        else:
            led.append({"op": "disconnect_mutable_pin", "asset_path": co, "node_guid": fg,
                "pin": fp, "other_guid": tg, "other_pin": tp}); ledger_added += 1
            made += 1
            conn_results.append({"from": fid, "to": tid, "from_pin": fp, "to_pin": tp, "connected": True})
    _save_nonvalidating(co)
    result = {"status": "success", "co_path": co,
              "nodes_created": len(created), "node_errors": node_errors,
              "id_to_guid": idmap, "created": created,
              "properties_set": [p for p in prop_results if not p.get("error")],
              "property_errors": [p for p in prop_results if p.get("error")],
              "connections_made": made, "connections": conn_results,
              "ledger_added": ledger_added, "ledger_depth": len(led)}
    result["note"] = ("Built the graph by replaying the C++ primitives (add / set-property / connect). "
        "Appended per-primitive inverse ledger entries only -- delete_mutable_node per node, "
        "set_mutable_node_property per property, disconnect_mutable_pin per connection -- all EXISTING "
        "folds; NO new fold. editor_level.undo unwinds them newest-first (connections, then properties, "
        "then nodes). Connection schema rejections are reported (connected:false) without aborting the "
        "build. NO CO compile triggered; non-validating save done.")
    print("@@UMCP@@" + json.dumps(result))
gc.collect()
'''

    @mcp.tool()
    def build_mutable_graph(ctx, co_path: str, nodes, connections=None) -> str:
        """Build a whole CustomizableObject Source graph in ONE call (nodes + properties + wiring).

        co_path:     CustomizableObject asset path.
        nodes:       list of {id, node_class, x?, y?, properties?}. `id` is a caller-chosen handle used
                     by `connections` (a GUID may also be used directly). `node_class` is a
                     UCustomizableObjectNode subclass -- bare name ('CustomizableObjectNodeFloatConstant')
                     or full path. `properties` is {UPROPERTY: value} applied via the node property
                     handler (numbers/bools/strings; for struct props pass the struct import-text string).
        connections: list of {from_id, from_pin, to_id, to_pin}. from/to may be a node `id` from `nodes`
                     or a raw GUID. Wiring uses the CustomizableObject schema's TryCreateConnection --
                     an incompatible pair is reported (connected:false, schema reason) and does NOT
                     abort the rest of the build.

        Replays add_mutable_node / set_mutable_node_property / connect_mutable_nodes and appends the
        matching per-primitive inverse ledger entries (delete_mutable_node / set_mutable_node_property /
        disconnect_mutable_pin -- all EXISTING folds; NO new fold). undo unwinds each newest-first. No CO
        compile is triggered; non-validating save. Returns id_to_guid, created, connections (per-pair
        status), property results, and ledger depth."""
        params = {"co_path": co_path, "nodes": nodes, "connections": connections or []}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # layout_mutable_graph                                                #
    # ================================================================== #
    _LAYOUT_BODY = _HELP + r'''
co = PARAMS["co_path"]
positions = PARAMS.get("positions")
auto = bool(PARAMS.get("auto", True))
cols = int(PARAMS.get("cols", 4) or 4)
dx = float(PARAMS.get("dx", 360.0) or 360.0)
dy = float(PARAMS.get("dy", 260.0) or 260.0)
ox = float(PARAMS.get("origin_x", 0.0) or 0.0)
oy = float(PARAMS.get("origin_y", 0.0) or 0.0)
rl = _mrl("set_mutable_node_property_json")
g = _read_graph(co)
if rl is None or g is None:
    print("@@UMCP@@" + json.dumps(_defer("layout_mutable_graph")))
else:
    graph_nodes = g.get("nodes") or []
    targets = {}
    mode = None
    if isinstance(positions, dict) and positions:
        mode = "positions"
        for guid, xy in positions.items():
            try:
                targets[str(guid)] = [int(xy[0]), int(xy[1])]
            except Exception:
                pass
    elif auto:
        mode = "auto-grid"
        ordered = sorted(graph_nodes, key=lambda n: (int(n.get("y", 0) or 0), int(n.get("x", 0) or 0), str(n.get("node_guid"))))
        for i, n in enumerate(ordered):
            gx = ox + (i % cols) * dx
            gy = oy + (i // cols) * dy
            targets[str(n.get("node_guid"))] = [int(gx), int(gy)]
    else:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "provide positions={guid:[x,y]} or set auto=True"}))
        targets = None
    if targets is not None:
        led = _ledger()
        ledger_added = 0
        cur = {str(n.get("node_guid")): [n.get("x"), n.get("y")] for n in graph_nodes}
        positioned = []
        errors = []
        for guid, xy in targets.items():
            if guid not in cur:
                errors.append({"node_guid": guid, "error": "node not found in graph"}); continue
            frm = cur.get(guid)
            axis_ok = True
            for prop, val in (("NodePosX", xy[0]), ("NodePosY", xy[1])):
                pr = _decode(rl.set_mutable_node_property_json(co, guid, prop, str(int(val))))
                if isinstance(pr, dict) and pr.get("error"):
                    errors.append({"node_guid": guid, "property": prop, "error": pr.get("error")}); axis_ok = False
                else:
                    led.append({"op": "set_mutable_node_property", "asset_path": co,
                        "node_guid": pr.get("node_guid") or guid, "property": pr.get("property") or prop,
                        "prev_value": pr.get("prev_value")}); ledger_added += 1
            if axis_ok:
                positioned.append({"node_guid": guid, "from": frm, "to": xy})
        _save_nonvalidating(co)
        result = {"status": "success", "co_path": co, "mode": mode,
                  "positioned_count": len(positioned), "positioned": positioned,
                  "errors": errors, "ledger_added": ledger_added, "ledger_depth": len(led)}
        result["note"] = ("Set NodePosX/NodePosY (reflected UEdGraphNode ints, PROVEN settable via the "
            "property handler) per node. Ledgered a set_mutable_node_property per axis per node (EXISTING "
            "fold); undo restores each prior position. Cosmetic graph-editor layout only; non-validating "
            "save; no compile.")
        print("@@UMCP@@" + json.dumps(result))
gc.collect()
'''

    @mcp.tool()
    def layout_mutable_graph(ctx, co_path: str, positions=None, auto: bool = True,
                             cols: int = 4, dx: float = 360.0, dy: float = 260.0,
                             origin_x: float = 0.0, origin_y: float = 0.0) -> str:
        """Set CustomizableObject Source-graph node positions (cosmetic editor layout). Ledgered.

        co_path:   CustomizableObject asset path.
        positions: {node_guid: [x, y]} explicit placement. If omitted and auto=True, every node is
                   auto-arranged into a grid.
        auto:      when positions is omitted, grid-arrange all nodes (default True).
        cols/dx/dy/origin_x/origin_y: auto-grid shape (columns, x/y spacing, top-left origin).

        Positions are written to the reflected UEdGraphNode ints NodePosX / NodePosY via
        set_mutable_node_property_json (proven settable). Ledgers one set_mutable_node_property per axis
        per node (EXISTING fold) -- undo restores each prior position. Non-validating save; no compile.
        Returns positioned (from/to per node), errors, and ledger depth."""
        params = {"co_path": co_path, "positions": positions, "auto": auto, "cols": cols,
                  "dx": dx, "dy": dy, "origin_x": origin_x, "origin_y": origin_y}
        try:
            return json.dumps(_exec(_LAYOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_mutable_parameter_ui_metadata                                   #
    # ================================================================== #
    _PARAMUI_BODY = _HELP + r'''
co = PARAMS["co_path"]
guid = PARAMS["node_guid"]
metadata = PARAMS.get("metadata")
prop = PARAMS.get("property_name") or "ParamUIMetadata"
rl = _mrl("set_mutable_node_property_json")
if rl is None or not hasattr(rl, "get_mutable_node_json"):
    print("@@UMCP@@" + json.dumps(_defer("set_mutable_parameter_ui_metadata")))
else:
    nj = _decode(rl.get_mutable_node_json(co, guid))
    if isinstance(nj, dict) and nj.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": nj.get("error")}))
    else:
        props = (nj.get("properties") or {}) if isinstance(nj, dict) else {}
        if prop not in props:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "node has no %r property (not a parameter node?)" % prop,
                "available_properties": sorted(props.keys()),
                "hint": "ParamUIMetadata lives on parameter nodes (Float/Color/Int/Bool/Enum/Texture Parameter)."}))
        else:
            if isinstance(metadata, str):
                text = metadata
            elif isinstance(metadata, dict):
                text = "(" + ",".join(str(k) + "=" + _field_text(metadata[k]) for k in metadata) + ")"
            else:
                text = None
            if text is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "metadata must be a struct import-text string or a {sub_field: scalar} dict"}))
            else:
                pr = _decode(rl.set_mutable_node_property_json(co, guid, prop, text))
                if isinstance(pr, dict) and pr.get("error"):
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": pr.get("error"),
                        "attempted_text": text,
                        "hint": "pass a full struct import-text string like the value read back from get_mutable_node."}))
                else:
                    _save_nonvalidating(co)
                    led = _ledger()
                    led.append({"op": "set_mutable_node_property", "asset_path": co,
                        "node_guid": pr.get("node_guid") or guid, "property": pr.get("property") or prop,
                        "prev_value": pr.get("prev_value")})
                    nj2 = _decode(rl.get_mutable_node_json(co, guid))
                    readback = (nj2.get("properties") or {}).get(prop) if isinstance(nj2, dict) else None
                    result = {"status": "success", "co_path": co, "node_guid": guid, "property": prop,
                              "input_kind": ("string" if isinstance(metadata, str) else "dict"),
                              "applied_text": text, "prev_value": pr.get("prev_value"),
                              "new_value": pr.get("new_value"), "readback": readback,
                              "ledger_depth": len(led)}
                    result["note"] = ("Set the parameter node's ParamUIMetadata struct via ImportText on "
                        "the property handler. A dict sets ONLY the named sub-fields (unspecified fields "
                        "follow UE struct-ImportText semantics -- pass a full '(...)' string to control "
                        "every field, e.g. containers like GameplayTags). Ledgered set_mutable_node_property "
                        "(EXISTING fold); the captured prior restores the struct EXACTLY on undo. "
                        "Non-validating save; no compile.")
                    print("@@UMCP@@" + json.dumps(result))
gc.collect()
'''

    @mcp.tool()
    def set_mutable_parameter_ui_metadata(ctx, co_path: str, node_guid: str, metadata) -> str:
        """Set a parameter node's ParamUIMetadata struct (editor UI hints). Ledgered.

        co_path:   CustomizableObject asset path.
        node_guid: the parameter node's GUID (Float/Color/Int/Bool/Enum/Texture Parameter node).
        metadata:  EITHER a full struct import-text string (controls every field, e.g.
                   '(MaximumValue=5.000000,GameplayTags=())' -- copy the value read from get_mutable_node
                   and edit it) OR a {sub_field: scalar} dict (e.g. {'MaximumValue': 5.0}) which sets
                   only the named fields. Per UE struct-ImportText, fields you omit in a dict follow the
                   struct's ImportText defaulting -- pass a full string when you must preserve container
                   fields exactly.

        Verifies the node actually has ParamUIMetadata (refuses otherwise), sets it via
        set_mutable_node_property_json, and ledgers set_mutable_node_property (EXISTING fold; the captured
        prior text restores the struct exactly on undo). Non-validating save; no compile. Returns
        applied_text, prev_value, new_value, and a readback of the resulting struct."""
        params = {"co_path": co_path, "node_guid": node_guid, "metadata": metadata}
        try:
            return json.dumps(_exec(_PARAMUI_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_mutable_child_objects  (READ -- no ledger)                     #
    # ================================================================== #
    _LISTCHILD_BODY = _HELP + r'''
co = PARAMS["co_path"]
g = _read_graph(co)
if g is None:
    print("@@UMCP@@" + json.dumps(_defer("list_mutable_child_objects")))
elif isinstance(g, dict) and g.get("error"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": g.get("error")}))
else:
    graph_nodes = g.get("nodes") or []
    by_guid = {str(n.get("node_guid")): n for n in graph_nodes}
    GROUP = "CustomizableObjectNodeObjectGroup"
    OBJECT = "CustomizableObjectNodeObject"
    def _linked(node, direction, category, container=None):
        nm = _find_pin(node, direction, category, container)
        if nm is None:
            return nm, []
        for p in (node.get("pins") or []):
            if p.get("name") == nm and p.get("direction") == direction:
                return nm, [str(lk.get("node_guid")) for lk in (p.get("linked_to") or [])]
        return nm, []
    groups = []
    objects = []
    for n in graph_nodes:
        cls = n.get("class")
        guid = str(n.get("node_guid"))
        if cls == GROUP:
            in_pin, child_guids = _linked(n, "input", "object", "array")   # Objects <- child.Object
            out_pin, parent_guids = _linked(n, "output", "object")         # Group -> parent.Children
            groups.append({"node_guid": guid, "title": n.get("title"),
                           "objects_pin": in_pin, "group_pin": out_pin,
                           "child_object_guids": child_guids,
                           "child_objects": [{"node_guid": c, "title": (by_guid.get(c) or {}).get("title")} for c in child_guids],
                           "parent_object_guids": parent_guids})
        elif cls == OBJECT:
            ch_pin, child_group_guids = _linked(n, "input", "object", "array")  # Children <- group.Group
            obj_pin, member_group_guids = _linked(n, "output", "object")        # Object -> group.Objects
            objects.append({"node_guid": guid, "title": n.get("title"),
                            "is_base_object": (n.get("title") == "Base Object"),
                            "children_pin": ch_pin, "object_pin": obj_pin,
                            "child_group_guids": child_group_guids,
                            "member_of_group_guids": member_group_guids})
    result = {"status": "success", "co_path": co,
              "graph": g.get("graph"), "node_count": g.get("node_count"),
              "group_count": len(groups), "object_count": len(objects),
              "groups": groups, "objects": objects}
    result["note"] = ("Read-only walk of group nodes (CustomizableObjectNodeObjectGroup) and object "
        "nodes (CustomizableObjectNodeObject). Wiring convention: a child object's 'Object' output feeds "
        "a group's 'Objects' input; a group's 'Group' output feeds a parent object's 'Children' input. "
        "A factory CO has only the Base Object node (no groups/children) until authored. No ledger.")
    print("@@UMCP@@" + json.dumps(result))
gc.collect()
'''

    @mcp.tool()
    def list_mutable_child_objects(ctx, co_path: str) -> str:
        """List the group / child-object structure of a CustomizableObject Source graph. Read-only.

        co_path: CustomizableObject asset path.

        Walks the graph for group nodes (CustomizableObjectNodeObjectGroup) and object nodes
        (CustomizableObjectNodeObject) and reports the wiring: each group's child objects (its 'Objects'
        input) and parent object (its 'Group' output -> a parent's 'Children'), and per object whether it
        is the Base Object, the groups feeding its 'Children', and the groups it is a member of. A factory
        CO exposes only the Base Object node until the graph is authored. No ledger."""
        try:
            return json.dumps(_exec(_LISTCHILD_BODY, {"co_path": co_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # link_mutable_child_object  (connect -- reuses disconnect_mutable_pin fold)
    # ================================================================== #
    _LINKCHILD_BODY = _HELP + r'''
co = PARAMS["co_path"]
group_guid = PARAMS["group_guid"]
child_guid = PARAMS["child_guid"]
rl = _mrl("connect_mutable_nodes_json")
if rl is None or not hasattr(rl, "get_mutable_node_json"):
    print("@@UMCP@@" + json.dumps(_defer("link_mutable_child_object")))
else:
    gnode = _decode(rl.get_mutable_node_json(co, group_guid))
    cnode = _decode(rl.get_mutable_node_json(co, child_guid))
    gerr = gnode.get("error") if isinstance(gnode, dict) else "group node not read"
    cerr = cnode.get("error") if isinstance(cnode, dict) else "child node not read"
    if gerr or cerr:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not read node(s): group=%s child=%s" % (gerr, cerr)}))
    else:
        group_in = _find_pin(gnode, "input", "object", "array")     # group's 'Objects'
        child_out = _find_pin(cnode, "output", "object")            # child's 'Object'
        if group_in is None or child_out is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "compatible child-link pins not found",
                "group_class": gnode.get("class"), "child_class": cnode.get("class"),
                "group_object_array_input_pin": group_in, "child_object_output_pin": child_out,
                "hint": ("group_guid must be a CustomizableObjectNodeObjectGroup (object-array input pin) "
                         "and child_guid a CustomizableObjectNodeObject (object output pin). Add them with "
                         "add_mutable_node if the factory CO lacks them.")}))
        else:
            cr = _decode(rl.connect_mutable_nodes_json(co, child_guid, child_out, group_guid, group_in))
            if isinstance(cr, dict) and cr.get("error"):
                print("@@UMCP@@" + json.dumps({"status": "error", "connected": False,
                    "message": cr.get("error"), "from": [child_guid, child_out], "to": [group_guid, group_in]}))
            else:
                _save_nonvalidating(co)
                led = _ledger()
                led.append({"op": "disconnect_mutable_pin", "asset_path": co, "node_guid": child_guid,
                    "pin": child_out, "other_guid": group_guid, "other_pin": group_in})
                result = {"status": "success", "co_path": co, "connected": True,
                          "child_guid": child_guid, "child_pin": child_out,
                          "group_guid": group_guid, "group_pin": group_in,
                          "ledger_depth": len(led)}
                result["note"] = ("Linked the child object's %r output into the group's %r input via the "
                    "schema's TryCreateConnection. Ledgered disconnect_mutable_pin (EXISTING fold); undo "
                    "breaks exactly this link. Non-validating save; no compile." % (child_out, group_in))
                print("@@UMCP@@" + json.dumps(result))
gc.collect()
'''

    @mcp.tool()
    def link_mutable_child_object(ctx, co_path: str, group_guid: str, child_guid: str) -> str:
        """Link a child object node into a group node (child.Object -> group.Objects). Ledgered.

        co_path:    CustomizableObject asset path.
        group_guid: a CustomizableObjectNodeObjectGroup node (has an object-array input pin 'Objects').
        child_guid: a CustomizableObjectNodeObject node (has an object output pin 'Object').

        Auto-detects the group's object-array INPUT pin and the child's object OUTPUT pin (by pin
        category, so it is robust to pin renames) and wires them through the CustomizableObject schema's
        TryCreateConnection. On a schema rejection returns {connected:false, reason}. On success ledgers
        disconnect_mutable_pin (EXISTING fold) so undo breaks exactly this link. Non-validating save; no
        compile. Add the group/child nodes first with add_mutable_node if a factory CO lacks them."""
        params = {"co_path": co_path, "group_guid": group_guid, "child_guid": child_guid}
        try:
            return json.dumps(_exec(_LINKCHILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_mutable_node_pin  (PROBE-and-degrade)                           #
    # ================================================================== #
    _ADDPIN_BODY = _HELP + r'''
co = PARAMS["co_path"]
guid = PARAMS["node_guid"]
rl = _mrl("get_mutable_node_json")
if rl is None or not hasattr(rl, "set_mutable_node_property_json"):
    print("@@UMCP@@" + json.dumps(_defer("add_mutable_node_pin")))
else:
    nj = _decode(rl.get_mutable_node_json(co, guid))
    if isinstance(nj, dict) and nj.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": nj.get("error")}))
    else:
        props = (nj.get("properties") or {}) if isinstance(nj, dict) else {}
        pins_before = [(p.get("name"), p.get("direction")) for p in (nj.get("pins") or [])]
        # A settable INTEGER count-like property would let us grow pins by bumping it + reconstruct.
        cand = None
        for k in props:
            kl = k.lower()
            if any(t in kl for t in ("count", "numpins", "num_pins", "numelements", "elementcount", "numoptions")):
                try:
                    int(props[k]); cand = k; break
                except Exception:
                    pass
        if cand is not None:
            newv = int(props[cand]) + 1
            pr = _decode(rl.set_mutable_node_property_json(co, guid, cand, str(newv)))
            if isinstance(pr, dict) and pr.get("error"):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": pr.get("error"), "property": cand}))
            else:
                _save_nonvalidating(co)
                led = _ledger()
                led.append({"op": "set_mutable_node_property", "asset_path": co,
                    "node_guid": pr.get("node_guid") or guid, "property": pr.get("property") or cand,
                    "prev_value": pr.get("prev_value")})
                nj2 = _decode(rl.get_mutable_node_json(co, guid))
                pins_after = [(p.get("name"), p.get("direction")) for p in (nj2.get("pins") or [])]
                print("@@UMCP@@" + json.dumps({"status": "success", "co_path": co, "node_guid": guid,
                    "property": cand, "prev_value": pr.get("prev_value"), "new_value": pr.get("new_value"),
                    "pins_before": pins_before, "pins_after": pins_after, "ledger_depth": len(led),
                    "note": ("Grew pins by bumping the node's count property (reused set_mutable_node_property "
                             "fold; undo restores the prior count).")}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "unsupported", "co_path": co, "node_guid": guid,
                "node_class": nj.get("class"), "pins": pins_before,
                "reason": ("this node exposes no settable pin-count UPROPERTY"),
                "note": ("SKIPPED by design (probe-and-degrade). In Mutable, SWITCH-node data pins are "
                         "ENUM-DRIVEN -- connect an enum parameter with N options (via connect_mutable_nodes) "
                         "and the switch regenerates N pins; ARRAY pins (e.g. a group's 'Objects', a base "
                         "object's 'Children') grow IMPLICITLY when you connect_mutable_nodes / "
                         "link_mutable_child_object. Neither path uses an explicit add-pin, so a standalone "
                         "pin-add needs node-specific C++ and is not forced here.")}))
gc.collect()
'''

    @mcp.tool()
    def add_mutable_node_pin(ctx, co_path: str, node_guid: str) -> str:
        """Probe/attempt adding a dynamic pin to a Source-graph node (switch/group). Degrades honestly.

        co_path:   CustomizableObject asset path.
        node_guid: the node's GUID.

        Mutable does not expose a generic add-pin: SWITCH-node pins are enum-driven (connect an enum
        parameter with N options and the switch regenerates its data pins) and ARRAY pins (group
        'Objects', base 'Children') grow implicitly when you connect_mutable_nodes / link_mutable_child_object.
        If the node DOES expose a settable integer count-like UPROPERTY, this bumps it (reusing the
        set_mutable_node_property fold; undo restores it). Otherwise it returns status 'unsupported' with a
        note (node-type-gated; needs node-specific C++) and changes nothing."""
        params = {"co_path": co_path, "node_guid": node_guid}
        try:
            return json.dumps(_exec(_ADDPIN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
