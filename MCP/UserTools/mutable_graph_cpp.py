"""UserTools :: Mutable (CustomizableObject) SOURCE-GRAPH reader + node primitives (mutable Wave 3).

DRAFT wiring for the CustomizableObject/CustomizableObjectEditor-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Mutable.cpp. Every tool here is hasattr-guarded on a
future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin DLL is rebuilt with those
handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS injection,
Output-Log auto-capture, per-session undo ledger) is copied from the gold-standard blueprint_graph_cpp.py;
the NON-VALIDATING save mirrors mutable_write.py._save_nonvalidating (the validating save path can hard-crash
on freshly-authored Mutable assets).

WHY C++: UCustomizableObject::Source is a private WITH_EDITORONLY_DATA UEdGraph reached only via
GetPrivate()->GetSource() (both CUSTOMIZABLEOBJECT_API but NON-reflected -> unreachable from Python). The
graph node classes (UCustomizableObjectNode) + schema (UEdGraphSchema_CustomizableObject) live in the
CustomizableObjectEditor module with no BlueprintCallable authoring surface. So graph read + ALL graph
authoring is C++.

  READS (no ledger -- non-mutating):
    * get_mutable_graph  -- full walk of the CO Source graph: per node {class, class_path, title, node_guid,
                            x, y, pins:[{name, direction, pin_type, default_value, is_orphaned, linked_to:
                            [{node_guid, pin_name}]}]}. THE READ that proves Source is reachable.
    * get_mutable_node   -- one node's pins (+links) + a compact set of reflected FProperties.

  WRITES (ledger -- reversible; inverse folds into editor_level.undo; each does a NON-VALIDATING save):
    * add_mutable_node          -- resolve node class -> NewObject -> Mutable node lifecycle (CreateNewGuid /
                                   PostPlacedNewNode / BeginConstruct / ReconstructNode). Ledger op
                                   'delete_mutable_node' (inverse = delete the created node by guid).
    * connect_mutable_nodes     -- Schema->TryCreateConnection. A REJECTED wire returns status error (with the
                                   schema's reason) and is NOT ledgered. On success, ledger op
                                   'disconnect_mutable_pin' (inverse = break that specific link).
    * disconnect_mutable_pin    -- break one specific link (other_guid+other_pin) or ALL links on a pin.
                                   Captures broken endpoints. Ledger op 'reconnect_mutable_links'
                                   (inverse = reconnect each captured endpoint).
    * delete_mutable_node       -- remove ANY node by GUID. Captures a LOSSY re-add spec (class + pos + pin
                                   links). Ledger op 'readd_mutable_node' (inverse = re-add + reconnect links;
                                   BEST-EFFORT, see LOSSINESS below).
    * set_mutable_node_property -- set an FProperty on the node (e.g. a parameter node's ParameterName).
                                   Captures prior. Ledger op 'set_mutable_node_property' (inverse = restore prior).

COMPILE: intentionally NOT triggered by these graph edits (a CO compile is expensive + separate). After
authoring a graph, call compile_customizable_object (mutable_write.py) ONCE to regenerate params/mesh. The
undo folds likewise restore the graph + non-validating save WITHOUT compiling.

LOSSINESS (delete inverse): re-add reconstructs only {node class, position, pin links}. Mutable input VALUES
usually come from constant/parameter NODES (not pin literals), so pin defaults rarely matter; but internal
per-node UPROPERTY state and auto-generated pin ids do NOT round-trip, and a connection the schema satisfied
by reconstructing/adapting a node is not specially undone. Treat delete-undo as best-effort structural
restoration, not a byte-exact rollback.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write
appends its inverse descriptor to the shared per-session ledger for editor_level.undo to fold. The op names
above are the CONTRACT the coordinator wires into the fold (see MCPReflection_Mutable.cpp report for the
exact fold branches).

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

# --- Output Log auto-capture (copied verbatim from blueprint_graph_cpp.py) -----
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
    return {"status": "error", "error": (fn + " requires the C++ Mutable graph handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Mutable.cpp to enable it.")}
def _save_nonvalidating(co_path):
    # Mirror mutable_write.py._save_nonvalidating: save_packages skips the asset-VALIDATION save path that
    # can hard-crash on freshly-authored Mutable assets.
    try:
        a = unreal.EditorAssetLibrary.load_asset(co_path)
        if a is None:
            return False
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([a.get_outermost()], False))
    except Exception:
        return False
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _GRAPH_BODY = _HELP + r'''
rl = _mrl("get_mutable_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_mutable_graph")))
else:
    res = _decode(rl.get_mutable_graph_json(PARAMS["co_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_mutable_graph(ctx, co_path: str) -> str:
        """Full walk of a CustomizableObject's Source graph: every node with pins, pin types, and links.

        co_path: CustomizableObject asset path (e.g. '/Game/MCP_Scratch/CO_Foo.CO_Foo' or '/Game/.../CO_Foo').

        Returns {customizable_object, object_path, graph, graph_class, node_count, nodes:[{class, class_path,
        title, node_guid, x, y, pins:[{name, direction, pin_type:{category, sub_category?,
        sub_category_object?, container, is_reference, is_const}, default_value, default_object?, pin_id,
        is_orphaned, linked_to:[{node_guid, pin_name}]}]}]}. CustomizableObjectEditor-only; needs the C++
        handler (inert until the plugin DLL is rebuilt with MCPReflection_Mutable.cpp)."""
        params = {"co_path": co_path}
        try:
            return json.dumps(_exec(_GRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _NODE_BODY = _HELP + r'''
rl = _mrl("get_mutable_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_mutable_node")))
else:
    res = _decode(rl.get_mutable_node_json(PARAMS["co_path"], PARAMS["node_guid"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_mutable_node(ctx, co_path: str, node_guid: str) -> str:
        """Full detail for one Source-graph node addressed by GUID (pins + links + reflected properties).

        co_path:   CustomizableObject asset path.
        node_guid: the node's GUID (from get_mutable_graph).

        Returns {customizable_object, graph, class, class_path, title, node_guid, x, y, pins:[...],
        properties:{<UPROPERTY name>: <exported text>}}. CustomizableObjectEditor-only; needs the C++ handler
        (inert until built)."""
        params = {"co_path": co_path, "node_guid": node_guid}
        try:
            return json.dumps(_exec(_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # Each forward write does a NON-VALIDATING save on success.
    # ================================================================== #

    # add_mutable_node -> inverse op 'delete_mutable_node' (delete the created node by guid).
    _ADD_BODY = _HELP + r'''
rl = _mrl("add_mutable_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_mutable_node")))
else:
    res = _decode(rl.add_mutable_node_json(PARAMS["co_path"], PARAMS["node_class"],
        float(PARAMS.get("x", 0.0)), float(PARAMS.get("y", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save_nonvalidating(PARAMS["co_path"])
        _ledger().append({"op": "delete_mutable_node", "asset_path": PARAMS["co_path"],
            "node_guid": res.get("node_guid")})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_mutable_node(ctx, co_path: str, node_class: str, x: float = 0.0, y: float = 0.0) -> str:
        """Create one node in a CustomizableObject Source graph (Mutable node lifecycle + positions + guid).

        co_path:    CustomizableObject asset path.
        node_class: the UCustomizableObjectNode subclass -- full path preferred
                    (e.g. '/Script/CustomizableObjectEditor.CustomizableObjectNodeObjectGroup') or the bare
                    class name ('CustomizableObjectNodeObjectGroup'). List candidates with
                    list_mutable_node_classes / search_mutable_nodes.
        x, y:       graph position.

        Resolves the class (validated concrete + a UCustomizableObjectNode), NewObjects it, then runs the
        Mutable node lifecycle (CreateNewGuid / PostPlacedNewNode / BeginConstruct / ReconstructNode) so pins
        are allocated. Marks the package dirty + non-validating save (NO CO compile -- call
        compile_customizable_object once after authoring). Appends inverse op 'delete_mutable_node'. Returns
        {node_guid, class, class_path, title, x, y, pins:[...], added, ledger_depth}.
        CustomizableObjectEditor-only; needs the C++ handler (inert until built)."""
        params = {"co_path": co_path, "node_class": node_class, "x": x, "y": y}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # connect_mutable_nodes -> inverse op 'disconnect_mutable_pin' (break that specific link).
    # A REJECTED connection surfaces as status error (with the schema's reason) and is NOT ledgered.
    _CONNECT_BODY = _HELP + r'''
rl = _mrl("connect_mutable_nodes_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("connect_mutable_nodes")))
else:
    res = _decode(rl.connect_mutable_nodes_json(PARAMS["co_path"], PARAMS["from_guid"], PARAMS["from_pin"],
        PARAMS["to_guid"], PARAMS["to_pin"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "connected": False, "message": res.get("error")}))
    else:
        _save_nonvalidating(PARAMS["co_path"])
        _ledger().append({"op": "disconnect_mutable_pin", "asset_path": PARAMS["co_path"],
            "node_guid": PARAMS["from_guid"], "pin": PARAMS["from_pin"],
            "other_guid": PARAMS["to_guid"], "other_pin": PARAMS["to_pin"]})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def connect_mutable_nodes(ctx, co_path: str, from_guid: str, from_pin: str, to_guid: str,
                              to_pin: str) -> str:
        """Wire two Source-graph pins via the CustomizableObject schema's TryCreateConnection.

        co_path:            CustomizableObject asset path.
        from_guid/from_pin: source node GUID + pin name.
        to_guid/to_pin:     target node GUID + pin name.

        TryCreateConnection SILENTLY REJECTS incompatible pins/directions -- a rejection returns
        {status:'error', connected:false, message:<schema reason>} and is NOT ledgered. On success returns
        {connected:true, message, ledger_depth}, non-validating saves, and appends inverse op
        'disconnect_mutable_pin'. CustomizableObjectEditor-only; needs the C++ handler (inert until built)."""
        params = {"co_path": co_path, "from_guid": from_guid, "from_pin": from_pin,
                  "to_guid": to_guid, "to_pin": to_pin}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # disconnect_mutable_pin -> inverse op 'reconnect_mutable_links' (reconnect each captured endpoint).
    _DISCONNECT_BODY = _HELP + r'''
rl = _mrl("disconnect_mutable_pin_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("disconnect_mutable_pin")))
else:
    res = _decode(rl.disconnect_mutable_pin_json(PARAMS["co_path"], PARAMS["node_guid"], PARAMS["pin_name"],
        PARAMS.get("other_guid", ""), PARAMS.get("other_pin", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save_nonvalidating(PARAMS["co_path"])
        if res.get("broken_count"):
            _ledger().append({"op": "reconnect_mutable_links", "asset_path": PARAMS["co_path"],
                "node_guid": res.get("node_guid"), "pin": res.get("pin"), "broken": res.get("broken", [])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def disconnect_mutable_pin(ctx, co_path: str, node_guid: str, pin_name: str,
                              other_guid: str = "", other_pin: str = "") -> str:
        """Break one specific link on a Source-graph pin, or ALL links if other_guid is omitted.

        co_path:              CustomizableObject asset path.
        node_guid:            the node's GUID.
        pin_name:             the pin whose link(s) to break.
        other_guid/other_pin: the specific endpoint to disconnect; BOTH empty -> break every link on the pin.

        Captures the broken endpoints, non-validating saves, and appends inverse op 'reconnect_mutable_links'.
        Returns {node_guid, pin, broken_count, broken:[{other_guid, other_pin}], ledger_depth}.
        CustomizableObjectEditor-only; needs the C++ handler (inert until built)."""
        params = {"co_path": co_path, "node_guid": node_guid, "pin_name": pin_name,
                  "other_guid": other_guid, "other_pin": other_pin}
        try:
            return json.dumps(_exec(_DISCONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # delete_mutable_node -> inverse op 'readd_mutable_node' (LOSSY re-add + reconnect links).
    _DELETE_BODY = _HELP + r'''
rl = _mrl("delete_mutable_node_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_mutable_node")))
else:
    res = _decode(rl.delete_mutable_node_json(PARAMS["co_path"], PARAMS["node_guid"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save_nonvalidating(PARAMS["co_path"])
        _ledger().append({"op": "readd_mutable_node", "asset_path": PARAMS["co_path"],
            "node_guid": res.get("node_guid"), "captured": res.get("captured", {}), "lossy": True})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_mutable_node(ctx, co_path: str, node_guid: str) -> str:
        """Remove ANY node from a CustomizableObject Source graph by GUID.

        co_path:   CustomizableObject asset path.
        node_guid: the node's GUID.

        Captures a LOSSY re-add spec (node class, position, pin links) and appends inverse op
        'readd_mutable_node'. The inverse re-creates the node and reconnects captured links BEST-EFFORT --
        internal per-node state does NOT round-trip (inverse_is_lossy=true). Non-validating saves. Returns
        {node_guid, removed, captured, inverse_is_lossy, ledger_depth}. CustomizableObjectEditor-only; needs
        the C++ handler (inert until built)."""
        params = {"co_path": co_path, "node_guid": node_guid}
        try:
            return json.dumps(_exec(_DELETE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # set_mutable_node_property -> inverse op 'set_mutable_node_property' (restore captured prior text).
    _SETPROP_BODY = _HELP + r'''
rl = _mrl("set_mutable_node_property_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_mutable_node_property")))
else:
    res = _decode(rl.set_mutable_node_property_json(PARAMS["co_path"], PARAMS["node_guid"],
        PARAMS["property_name"], str(PARAMS.get("value", ""))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save_nonvalidating(PARAMS["co_path"])
        _ledger().append({"op": "set_mutable_node_property", "asset_path": PARAMS["co_path"],
            "node_guid": res.get("node_guid"), "property": res.get("property"),
            "prev_value": res.get("prev_value")})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_mutable_node_property(ctx, co_path: str, node_guid: str, property_name: str, value: str) -> str:
        """Set a UPROPERTY on a Source-graph node (e.g. a parameter node's ParameterName); captures prior.

        co_path:       CustomizableObject asset path.
        node_guid:     the node's GUID.
        property_name: the UPROPERTY name on the node's class (from get_mutable_node.properties).
        value:         the new value as ImportText-parseable text (numbers, 'true'/'false', quoted strings,
                       struct/enum text).

        Uses FProperty ExportText (capture prior) + ImportText (set); a parse failure returns an error and
        changes nothing. Reconstructs the node afterward (the property may drive pin shape). Non-validating
        saves. Appends inverse op 'set_mutable_node_property'. Returns {node_guid, property, prev_value,
        new_value, ledger_depth}. CustomizableObjectEditor-only; needs the C++ handler (inert until built)."""
        params = {"co_path": co_path, "node_guid": node_guid, "property_name": property_name, "value": value}
        try:
            return json.dumps(_exec(_SETPROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
