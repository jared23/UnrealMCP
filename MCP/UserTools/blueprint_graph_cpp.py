"""UserTools :: Blueprint K2 GRAPH-CORE reader + node primitives (spec: docs/spec/blueprint_graph.md)

DRAFT wiring for the BlueprintGraph/UnrealEd-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_BlueprintGraph.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM
from the gold-standard niagara_runtime_cpp.py.

These are the reader + node primitives that every higher-level blueprint-graph tool composes on:

  READS (no ledger -- non-mutating):
    * get_blueprint_graph     -- full walk of one UEdGraph: per node {class, title, node_guid, x, y,
                                 pins:[{name, direction, pin_type, default_value, linked_to:[{node_guid,
                                 pin_name}]}]}. graph_name ""/"EventGraph" -> the ubergraph; else a named
                                 function graph.
    * search_blueprint_nodes  -- compact live-node enumeration; with_pins adds pin detail (no link walk).
    * describe_blueprint_node -- one node's full pins/props (+links) by GUID.

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * add_blueprint_node          -- spec-driven K2 node create (call_function/variable_get/variable_set/
                                     branch/cast/knot/event/custom_event). Ledger op 'delete_blueprint_node'
                                     (inverse = delete the created node by guid).
    * connect_blueprint_nodes     -- Schema->TryCreateConnection. TryCreateConnection SILENTLY REJECTS
                                     incompatible pins; the handler returns the reason as an ERROR (no
                                     ledger) so a rejected wire never masquerades as success. On success,
                                     ledger op 'break_blueprint_node_link' (inverse = break that link).
    * set_blueprint_pin_default   -- Schema->TrySetDefaultValue / TrySetDefaultObject. Captures prior
                                     value+object. Ledger op 'set_blueprint_pin_default' (inverse = restore
                                     prior).
    * break_blueprint_node_link   -- break one specific link (other_guid+other_pin) or ALL links on a pin.
                                     Captures broken endpoints. Ledger op 'reconnect_blueprint_links'
                                     (inverse = reconnect each captured endpoint).
    * delete_blueprint_node       -- generalized RemoveEventNodeByGuid: remove ANY node by GUID. Captures a
                                     LOSSY re-add spec (class+kind+pos+pin defaults+links). Ledger op
                                     'readd_blueprint_node' (inverse = re-add + relink + re-default;
                                     BEST-EFFORT, see LOSSINESS below).
    * set_blueprint_node_property -- set an FProperty on the UK2Node (e.g. a literal node's value). Captures
                                     prior. Ledger op 'set_blueprint_node_property' (inverse = restore prior).

  COMPILE (finalize -- no ledger):
    * compile_blueprint_graph     -- deferred single compile. The C++ writes do NOT compile per-node (they
                                     MarkBlueprintAsStructurallyModified only); call this ONCE after a batch
                                     of edits. Prefers the C++ compile_blueprint_by_path handler; falls back
                                     to stock unreal.BlueprintEditorLibrary.compile_blueprint.

LOSSINESS (delete inverse): re-add reconstructs only {node class/kind, position, pin default values,
pin links}. Internal node state (advanced pins, expanded defaults, per-node UPROPERTY tweaks, auto-
generated pin ids) is NOT preserved, and a connection that the schema satisfied with an inserted
conversion/promotion node is reported (inserted_conversion_node) but NOT auto-removed by a plain
break-link inverse. Treat delete-undo as best-effort structural restoration, not a byte-exact rollback.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write
appends its inverse descriptor to the shared per-session ledger for editor_level.undo to fold. The op names
above are the CONTRACT the coordinator wires into the fold.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a snippet
local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py) -----
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
    return {"status": "error", "error": (fn + " requires the C++ BlueprintGraph handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_BlueprintGraph.cpp to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _GRAPH_BODY = _HELP + r'''
rl = _mrl("get_blueprint_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_blueprint_graph")))
else:
    res = _decode(rl.get_blueprint_graph_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_blueprint_graph(ctx, blueprint_path: str, graph_name: str = "") -> str:
        """Full walk of one Blueprint graph: every node with pins, pin types, defaults, and links.

        blueprint_path: Blueprint asset path (e.g. '/Game/BP_Foo.BP_Foo' or '/Game/BP_Foo').
        graph_name:     '' or 'EventGraph' -> the ubergraph (event graph); else a named function graph.

        Returns {blueprint, blueprint_path, graph, node_count, nodes:[{class, title, node_guid, x, y,
        pins:[{name, direction, pin_type:{category, sub_category?, sub_category_object?, container,
        is_reference, is_const, value_type?}, default_value, default_object?, default_text?, pin_id,
        linked_to:[{node_guid, pin_name}]}]}]}. BlueprintGraph-only; needs the C++ handler (inert until
        the plugin DLL is rebuilt with MCPReflection_BlueprintGraph.cpp)."""
        params = {"blueprint_path": blueprint_path, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_GRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SEARCH_BODY = _HELP + r'''
rl = _mrl("search_blueprint_nodes_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("search_blueprint_nodes")))
else:
    res = _decode(rl.search_blueprint_nodes_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        bool(PARAMS.get("with_pins", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def search_blueprint_nodes(ctx, blueprint_path: str, graph_name: str = "", with_pins: bool = False) -> str:
        """Compact enumeration of a graph's live nodes; with_pins adds pin detail (no link walk).

        blueprint_path: Blueprint asset path.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.
        with_pins:      also emit each node's pins (name/direction/pin_type/default). Heavier.

        Returns {blueprint, graph, node_count, nodes:[{class, title, node_guid, x, y[, pins:[...]]}]}.
        BlueprintGraph-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "graph_name": graph_name, "with_pins": with_pins}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _DESCRIBE_BODY = _HELP + r'''
rl = _mrl("describe_blueprint_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("describe_blueprint_node")))
else:
    res = _decode(rl.describe_blueprint_node_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def describe_blueprint_node(ctx, blueprint_path: str, node_guid: str, graph_name: str = "") -> str:
        """Full detail for one node addressed by GUID (all pins, types, defaults, links).

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID (from get_blueprint_graph / search_blueprint_nodes).
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Returns {blueprint, graph, class, title, node_guid, x, y, enabled, pins:[{name, direction,
        pin_type, default_value, default_object?, default_text?, pin_id, linked_to:[...]}]}.
        BlueprintGraph-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_DESCRIBE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    # add_blueprint_node -> inverse op 'delete_blueprint_node' (delete the created node by guid).
    _ADD_BODY = _HELP + r'''
rl = _mrl("add_blueprint_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_blueprint_node")))
else:
    res = _decode(rl.add_blueprint_node_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        json.dumps(PARAMS.get("node_spec", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "delete_blueprint_node", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "node_guid": res.get("node_guid")})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_blueprint_node(ctx, blueprint_path: str, node_spec: dict, graph_name: str = "") -> str:
        """Create one K2 node in a Blueprint graph from a spec (AllocateDefaultPins + positions + guid).

        blueprint_path: Blueprint asset path.
        node_spec:      dict picking the K2 node kind (+ optional 'x','y'):
                          {"kind":"call_function","class":"<class path/name>","function":"<FuncName>"}
                          {"kind":"variable_get","var_name":"<Var>"}   {"kind":"variable_set","var_name":"<Var>"}
                          {"kind":"branch"}                            {"kind":"cast","class":"<TargetClass>"}
                          {"kind":"knot"}                              {"kind":"event","event_name":"ReceiveBeginPlay"[,"class":"..."]}
                          {"kind":"custom_event","event_name":"<Name>"}
                        NOTE: an override 'event' uses the ENGINE function name (BeginPlay -> 'ReceiveBeginPlay',
                        Tick -> 'ReceiveTick'); 'class' defaults to the Blueprint's parent class.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Marks the Blueprint structurally modified (NO per-node compile -- call compile_blueprint_graph once
        after the batch). Appends inverse op 'delete_blueprint_node' to the ledger. Returns {node_guid, class,
        title, x, y, pins:[...], added, ledger_depth}. BlueprintGraph-only; needs the C++ handler (inert
        until built)."""
        params = {"blueprint_path": blueprint_path, "node_spec": node_spec, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # connect_blueprint_nodes -> inverse op 'break_blueprint_node_link' (break that specific link).
    # A REJECTED connection surfaces as status error (with the schema's reason) and is NOT ledgered.
    _CONNECT_BODY = _HELP + r'''
rl = _mrl("connect_blueprint_nodes_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("connect_blueprint_nodes")))
else:
    res = _decode(rl.connect_blueprint_nodes_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["from_guid"], PARAMS["from_pin"], PARAMS["to_guid"], PARAMS["to_pin"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "connected": False, "message": res.get("error")}))
    else:
        _ledger().append({"op": "break_blueprint_node_link", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")),
            "node_guid": PARAMS["from_guid"], "pin": PARAMS["from_pin"],
            "other_guid": PARAMS["to_guid"], "other_pin": PARAMS["to_pin"]})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def connect_blueprint_nodes(ctx, blueprint_path: str, from_guid: str, from_pin: str, to_guid: str,
                                to_pin: str, graph_name: str = "") -> str:
        """Wire two pins via the K2 schema's TryCreateConnection (resolved by node-guid + pin-name).

        blueprint_path: Blueprint asset path.
        from_guid/from_pin: source node GUID + pin name.
        to_guid/to_pin:     target node GUID + pin name.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        TryCreateConnection SILENTLY REJECTS incompatible pins/directions -- a rejection returns
        {status:'error', connected:false, message:<schema reason>} and is NOT ledgered (nothing to undo).
        On success returns {connected:true, message, inserted_conversion_node, ledger_depth} and appends
        inverse op 'break_blueprint_node_link'. If inserted_conversion_node is true the schema added a
        conversion/promotion node that the break-link inverse will NOT remove. BlueprintGraph-only; needs
        the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "from_guid": from_guid, "from_pin": from_pin,
                  "to_guid": to_guid, "to_pin": to_pin, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # set_blueprint_pin_default -> inverse op 'set_blueprint_pin_default' (restore captured prior).
    _SETDEFAULT_BODY = _HELP + r'''
rl = _mrl("set_blueprint_pin_default_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_pin_default")))
else:
    res = _decode(rl.set_blueprint_pin_default_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"], PARAMS["pin_name"], str(PARAMS.get("value", ""))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_blueprint_pin_default", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "node_guid": res.get("node_guid"),
            "pin": res.get("pin"), "is_object_pin": bool(res.get("is_object_pin")),
            "prev_value": res.get("prev_value"), "prev_object": res.get("prev_object")})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_pin_default(ctx, blueprint_path: str, node_guid: str, pin_name: str, value: str,
                                  graph_name: str = "") -> str:
        """Set a pin's default value (or default object for object/class pins) via the K2 schema.

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID.
        pin_name:       the pin's name (e.g. 'InString' on a PrintString node).
        value:          the literal string (scalar/struct-text); for object/class pins, an asset/class path
                        which the handler resolves and sets as the default object.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Uses Schema->TrySetDefaultValue / TrySetDefaultObject (each validates; the handler re-reads the
        ACTUAL post-set default). Captures prior value+object and appends inverse op
        'set_blueprint_pin_default'. Returns {node_guid, pin, is_object_pin, prev_value, prev_object,
        new_value, new_object, ledger_depth}. BlueprintGraph-only; needs the C++ handler (inert until
        built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "pin_name": pin_name,
                  "value": value, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SETDEFAULT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # break_blueprint_node_link -> inverse op 'reconnect_blueprint_links' (reconnect each captured endpoint).
    _BREAK_BODY = _HELP + r'''
rl = _mrl("break_blueprint_node_link_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("break_blueprint_node_link")))
else:
    res = _decode(rl.break_blueprint_node_link_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"], PARAMS["pin_name"], PARAMS.get("other_guid", ""), PARAMS.get("other_pin", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("broken_count"):
            _ledger().append({"op": "reconnect_blueprint_links", "asset_path": PARAMS["blueprint_path"],
                "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "node_guid": res.get("node_guid"),
                "pin": res.get("pin"), "broken": res.get("broken", [])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def break_blueprint_node_link(ctx, blueprint_path: str, node_guid: str, pin_name: str,
                                  other_guid: str = "", other_pin: str = "", graph_name: str = "") -> str:
        """Break one specific link on a pin, or ALL links if other_guid is omitted.

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID.
        pin_name:       the pin whose link(s) to break.
        other_guid/other_pin: the specific endpoint to disconnect; BOTH empty -> break every link on the pin.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Captures the broken endpoints and appends inverse op 'reconnect_blueprint_links' (reconnect each).
        Returns {node_guid, pin, broken_count, broken:[{other_guid, other_pin}], ledger_depth}.
        BlueprintGraph-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "pin_name": pin_name,
                  "other_guid": other_guid, "other_pin": other_pin, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_BREAK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # delete_blueprint_node -> inverse op 'readd_blueprint_node' (LOSSY re-add + relink + re-default).
    _DELETE_BODY = _HELP + r'''
rl = _mrl("delete_blueprint_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_blueprint_node")))
else:
    res = _decode(rl.delete_blueprint_node_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "readd_blueprint_node", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "node_guid": res.get("node_guid"),
            "captured": res.get("captured", {}), "lossy": True})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_blueprint_node(ctx, blueprint_path: str, node_guid: str, graph_name: str = "") -> str:
        """Remove ANY node from a graph by GUID (generalized RemoveEventNodeByGuid).

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Captures a LOSSY re-add spec (class/kind, position, pin default values, pin links) and appends
        inverse op 'readd_blueprint_node'. The inverse re-creates the node, re-applies pin defaults, and
        reconnects captured links BEST-EFFORT -- internal node state and inserted conversion nodes do NOT
        round-trip (inverse_is_lossy=true). Returns {node_guid, removed, captured, inverse_is_lossy,
        ledger_depth}. BlueprintGraph-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_DELETE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # set_blueprint_node_property -> inverse op 'set_blueprint_node_property' (restore captured prior text).
    _SETPROP_BODY = _HELP + r'''
rl = _mrl("set_blueprint_node_property_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_node_property")))
else:
    res = _decode(rl.set_blueprint_node_property_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"], PARAMS["property_name"], str(PARAMS.get("value", ""))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_blueprint_node_property", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "node_guid": res.get("node_guid"),
            "property": res.get("property"), "prev_value": res.get("prev_value")})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_node_property(ctx, blueprint_path: str, node_guid: str, property_name: str, value: str,
                                    graph_name: str = "") -> str:
        """Set an FProperty on a UK2Node (e.g. a literal node's value); captures prior; reconstructs pins.

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID.
        property_name:  the UPROPERTY name on the node's class.
        value:          the new value as ImportText-parseable text (numbers, 'true'/'false', struct/enum text).
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Uses FProperty ExportText (capture prior) + ImportText (set); a parse failure returns an error and
        changes nothing. Reconstructs the node afterward (the property may drive pin shape). Appends inverse
        op 'set_blueprint_node_property'. Returns {node_guid, property, prev_value, new_value, ledger_depth}.
        BlueprintGraph-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "property_name": property_name,
                  "value": value, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SETPROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # COMPILE (no ledger). Deferred single finalize after a batch of writes.
    # Prefers the C++ compile_blueprint_by_path; falls back to the stock
    # unreal.BlueprintEditorLibrary.compile_blueprint (no C++ dependency).
    # ================================================================== #
    _COMPILE_BODY = _HELP + r'''
rl = _mrl("compile_blueprint_by_path")
if rl is not None:
    res = _decode(rl.compile_blueprint_by_path(PARAMS["blueprint_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out["status"] = "success"; out["via"] = "cpp"
        print("@@UMCP@@" + json.dumps(out))
else:
    bel = getattr(unreal, "BlueprintEditorLibrary", None)
    bp = unreal.load_asset(PARAMS["blueprint_path"]) if hasattr(unreal, "load_asset") else None
    if bel is None or not hasattr(bel, "compile_blueprint") or bp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": ("compile_blueprint_graph needs either "
            "the C++ compile_blueprint_by_path handler or unreal.BlueprintEditorLibrary.compile_blueprint "
            "(+ a loadable blueprint_path)")}))
    else:
        bel.compile_blueprint(bp)
        print("@@UMCP@@" + json.dumps({"status": "success", "via": "blueprint_editor_library",
            "blueprint": bp.get_name()}))
gc.collect()
'''

    @mcp.tool()
    def compile_blueprint_graph(ctx, blueprint_path: str) -> str:
        """Compile a Blueprint once, to finalize a batch of graph edits (writes do NOT compile per-node).

        blueprint_path: Blueprint asset path.

        The C++ node-primitive writes only MarkBlueprintAsStructurallyModified; call this ONCE after a batch
        to actually compile. Prefers the C++ compile_blueprint_by_path handler (returns {compile_status,
        up_to_date, has_errors}); if that handler is not built yet, falls back to stock
        unreal.BlueprintEditorLibrary.compile_blueprint. Returns {status, via, ...}."""
        params = {"blueprint_path": blueprint_path}
        try:
            return json.dumps(_exec(_COMPILE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
