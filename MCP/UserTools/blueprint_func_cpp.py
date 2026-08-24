"""UserTools :: Blueprint FUNCTION + EVENT-GRAPH authoring (spec: docs/spec/blueprint_func.md)

DRAFT wiring for the BlueprintGraph/UnrealEd-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_BlueprintFunc.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention,
base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from the
gold-standard niagara_runtime_cpp.py / blueprint_graph_cpp.py.

These extend the empty add_blueprint_function + no-arg add_event_dispatcher to full function/event authoring:

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * create_blueprint_function     -- new user function graph (+ optional return pin). Ledger op
                                       'delete_blueprint_function' (inverse = RemoveGraph the function).
    * add_function_input            -- CreateUserDefinedPin on the FunctionEntry (function INPUT). Ledger op
                                       'remove_function_pin' {is_output:false}.
    * add_function_output           -- find/create the FunctionResult; CreateUserDefinedPin (function OUTPUT).
                                       Ledger op 'remove_function_pin' {is_output:true}.
    * set_function_properties       -- FunctionEntry pure/const/access + category/tooltip/keywords/metadata.
                                       Captures prior. Ledger op 'set_function_properties' {props:<prior>}.
    * create_local_variable         -- FBlueprintEditorUtils::AddLocalVariable (function-scope). Ledger op
                                       'remove_local_variable'.
    * delete_blueprint_function     -- RemoveGraph a function graph. Captures a LOSSY re-add spec (name +
                                       signature pins; NOT the body). Ledger op 'readd_blueprint_function'.
    * override_blueprint_function   -- override a parent UFUNCTION as a function graph (extends the
                                       event-only override to non-event functions). Ledger op
                                       'delete_blueprint_function'.
    * create_event_graph            -- CreateNewGraph + UbergraphPages.Add (a new event-graph tab). Ledger op
                                       'delete_event_graph'.
    * rename_event_graph            -- FBlueprintEditorUtils::RenameGraph. Ledger op 'rename_event_graph'
                                       (inverse = rename back).
    * delete_event_graph            -- RemoveGraph an ubergraph page. LOSSY re-add (empty graph, same name).
                                       Ledger op 'recreate_event_graph'.
    * add_event_dispatcher_input    -- add a param pin to a multicast-delegate dispatcher's SIGNATURE graph
                                       entry. Ledger op 'remove_event_dispatcher_input'.

COMPILE: the C++ writes MarkBlueprintAsStructurallyModified only (NO per-call compile -- compiling a
half-built signature is wasteful + crash-prone). Batch your edits then finalize ONCE with the
blueprint_graph_cpp.compile_blueprint_graph tool (or unreal.BlueprintEditorLibrary.compile_blueprint).

LOSSINESS: delete_blueprint_function / delete_event_graph re-adds reconstruct ONLY the signature (function)
or an empty graph (event) -- the graph BODY (nodes + wiring) is NOT captured, so a delete-undo is structural
signature restoration, not a byte-exact rollback. set_function_properties metadata-restore re-sets prior
values for the keys it touched; a metadata key that did not exist before is restored to '' (not truly unset).

Undo: this module registers NO own 'undo' tool (editor_level.py owns the ONE unified 'undo'). Each write
appends its inverse descriptor (ledger key 'asset_path') to the shared per-session ledger for
editor_level.undo to fold. The op names above are the CONTRACT the coordinator wires into the fold; the
folds call the C++ inverse handlers: delete_blueprint_function_json / remove_function_pin_json (is_output) /
set_function_properties_json(json.dumps(props)) / remove_local_variable_json / create_blueprint_function_graph_json
(+ re-add pins) / delete_event_graph_json / rename_event_graph_json / create_event_graph_json /
remove_event_dispatcher_input_json.

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
    return {"status": "error", "error": (fn + " requires the C++ BlueprintFunc handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_BlueprintFunc.cpp to enable it.")}
'''

    # ================================================================== #
    # 1) create_blueprint_function -> inverse op 'delete_blueprint_function'
    # ================================================================== #
    _CREATE_FUNC_BODY = _HELP + r'''
rl = _mrl("create_blueprint_function_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_blueprint_function")))
else:
    rt = PARAMS.get("return_type")
    rt_json = "" if rt in (None, "") else (json.dumps(rt) if not isinstance(rt, str) else rt)
    res = _decode(rl.create_blueprint_function_graph_json(PARAMS["blueprint_path"], PARAMS["function_name"], rt_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "delete_blueprint_function", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_blueprint_function(ctx, blueprint_path: str, function_name: str, return_type=None) -> str:
        """Create a new user Blueprint FUNCTION graph (optionally seeding a single return pin).

        blueprint_path: Blueprint asset path (e.g. '/Game/BP_Foo.BP_Foo' or '/Game/BP_Foo').
        function_name:  name for the new function (must not collide with any existing graph).
        return_type:    optional -- a type spec for a single 'ReturnValue' output pin. Either a bare scalar
                        token ('int','float','bool','name','string','vector','transform', ...) or a dict
                        {"category":"object|class|struct|enum|softobject|...","type_path":"<Class/Struct/Enum>",
                        "container":"none|array|set|map"[,"value":{...} for map]}. Omit for a void function.

        Uses AddFunctionGraph<UClass>(bp, graph, /*bIsUserCreated*/true, nullptr) then, if return_type is set,
        FindOrCreateFunctionResultNode + CreateUserDefinedPin. Marks the Blueprint structurally modified (NO
        compile -- finalize once via compile_blueprint_graph). Appends inverse op 'delete_blueprint_function'.
        Returns {function, has_return, return_pin?, return_type?, created, ledger_depth}. BlueprintFunc-only;
        needs the C++ handler (inert until the plugin DLL is rebuilt with MCPReflection_BlueprintFunc.cpp)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name, "return_type": return_type}
        try:
            return json.dumps(_exec(_CREATE_FUNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 2) add_function_input -> inverse op 'remove_function_pin' (is_output=false)
    # ================================================================== #
    _ADD_INPUT_BODY = _HELP + r'''
rl = _mrl("add_function_input_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_function_input")))
else:
    ty = PARAMS.get("type", "")
    ty_json = ty if isinstance(ty, str) else json.dumps(ty)
    res = _decode(rl.add_function_input_json(PARAMS["blueprint_path"], PARAMS["function_name"],
        PARAMS["pin_name"], ty_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "remove_function_pin", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"]),
            "pin_name": res.get("input", PARAMS["pin_name"]), "is_output": False})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_function_input(ctx, blueprint_path: str, function_name: str, pin_name: str, type) -> str:
        """Add an INPUT parameter to a Blueprint function (a pin on the FunctionEntry node).

        blueprint_path: Blueprint asset path.
        function_name:  name of an existing function graph.
        pin_name:       parameter name (must be unique among the function's inputs).
        type:           a type spec -- bare scalar token or {"category":...,"type_path":...,"container":...}
                        (same grammar as create_blueprint_function's return_type / add_blueprint_variable).

        Entry OUTPUT pins ARE the function inputs, so this creates an EGPD_Output user pin and reconstructs
        the entry (ReconstructNode + HandleParameterDefaultValueChanged). Marks structurally modified (NO
        compile). Appends inverse op 'remove_function_pin' (is_output=false). Returns {function, input, type,
        added, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name,
                  "pin_name": pin_name, "type": type}
        try:
            return json.dumps(_exec(_ADD_INPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 3) add_function_output -> inverse op 'remove_function_pin' (is_output=true)
    # ================================================================== #
    _ADD_OUTPUT_BODY = _HELP + r'''
rl = _mrl("add_function_output_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_function_output")))
else:
    ty = PARAMS.get("type", "")
    ty_json = ty if isinstance(ty, str) else json.dumps(ty)
    res = _decode(rl.add_function_output_json(PARAMS["blueprint_path"], PARAMS["function_name"],
        PARAMS["pin_name"], ty_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "remove_function_pin", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"]),
            "pin_name": res.get("output", PARAMS["pin_name"]), "is_output": True})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_function_output(ctx, blueprint_path: str, function_name: str, pin_name: str, type) -> str:
        """Add an OUTPUT (return) parameter to a Blueprint function (a pin on the FunctionResult node).

        blueprint_path: Blueprint asset path.
        function_name:  name of an existing function graph.
        pin_name:       output name (must be unique among the function's outputs).
        type:           a type spec -- bare scalar token or {"category":...,"type_path":...,"container":...}.

        Finds or creates the FunctionResult node, then creates an EGPD_Input user pin on EVERY result node
        (a function may have several) and reconstructs them. Marks structurally modified (NO compile). Appends
        inverse op 'remove_function_pin' (is_output=true). Returns {function, output, type, result_node_count,
        added, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name,
                  "pin_name": pin_name, "type": type}
        try:
            return json.dumps(_exec(_ADD_OUTPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 4) set_function_properties -> inverse op 'set_function_properties' (restore prior props)
    # ================================================================== #
    _SET_PROPS_BODY = _HELP + r'''
rl = _mrl("set_function_properties_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_function_properties")))
else:
    res = _decode(rl.set_function_properties_json(PARAMS["blueprint_path"], PARAMS["function_name"],
        json.dumps(PARAMS.get("props", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_function_properties", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"]), "props": res.get("prior", {})})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_function_properties(ctx, blueprint_path: str, function_name: str, props: dict) -> str:
        """Set a Blueprint function's flags + metadata (pure/const/access/category/tooltip/keywords/metadata).

        blueprint_path: Blueprint asset path.
        function_name:  name of an existing function graph.
        props:          any subset of:
                          {"pure": bool}            -- FUNC_BlueprintPure (a pure function has no exec pins)
                          {"const": bool}           -- FUNC_Const
                          {"access": "public|protected|private"}
                          {"category": "<str>"}     -- palette category
                          {"tooltip": "<str>"}      -- function tooltip
                          {"keywords": "<str>"}     -- search keywords
                          {"metadata": {"<Key>":"<Value>", ...}}  -- arbitrary declared-function metadata

        Flags are set on the FunctionEntry ExtraFlags (the persistent source; the UFunction is regenerated on
        compile). Captures PRIOR pure/const/access/category/tooltip/keywords + prior values of touched metadata
        keys; appends inverse op 'set_function_properties' carrying those prior props. Marks structurally
        modified (NO compile). Returns {function, applied, prior, ledger_depth}. NOTE: a metadata key absent
        before is restored to '' by the inverse (not truly unset). BlueprintFunc-only; needs the C++ handler
        (inert until built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name, "props": props}
        try:
            return json.dumps(_exec(_SET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 5) create_local_variable -> inverse op 'remove_local_variable'
    # ================================================================== #
    _CREATE_LOCAL_BODY = _HELP + r'''
rl = _mrl("create_local_variable_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_local_variable")))
else:
    ty = PARAMS.get("type", "")
    ty_json = ty if isinstance(ty, str) else json.dumps(ty)
    res = _decode(rl.create_local_variable_json(PARAMS["blueprint_path"], PARAMS["function_name"],
        PARAMS["var_name"], ty_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "remove_local_variable", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"]),
            "var_name": res.get("local_variable", PARAMS["var_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_local_variable(ctx, blueprint_path: str, function_name: str, var_name: str, type) -> str:
        """Create a function-scoped LOCAL variable inside a Blueprint function.

        blueprint_path: Blueprint asset path.
        function_name:  name of an existing function graph.
        var_name:       local variable name (must be unique within the function).
        type:           a type spec -- bare scalar token or {"category":...,"type_path":...,"container":...}.

        Uses FBlueprintEditorUtils::AddLocalVariable (stored on the FunctionEntry LocalVariables). Marks
        structurally modified (NO compile). Appends inverse op 'remove_local_variable'. Returns {function,
        local_variable, type, created, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until
        built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name,
                  "var_name": var_name, "type": type}
        try:
            return json.dumps(_exec(_CREATE_LOCAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 6) delete_blueprint_function -> inverse op 'readd_blueprint_function' (LOSSY: signature only)
    # ================================================================== #
    _DELETE_FUNC_BODY = _HELP + r'''
rl = _mrl("delete_blueprint_function_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_blueprint_function")))
else:
    res = _decode(rl.delete_blueprint_function_json(PARAMS["blueprint_path"], PARAMS["function_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "readd_blueprint_function", "asset_path": PARAMS["blueprint_path"],
            "function_name": res.get("function", PARAMS["function_name"]),
            "captured": res.get("captured", {})})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_blueprint_function(ctx, blueprint_path: str, function_name: str) -> str:
        """Delete a Blueprint FUNCTION graph (RemoveGraph).

        blueprint_path: Blueprint asset path.
        function_name:  name of the function graph to remove.

        Captures a LOSSY re-add spec (function name + input/output signature pins ONLY -- the graph BODY is
        NOT captured). Marks structurally modified (NO compile). Appends inverse op 'readd_blueprint_function'
        whose fold re-creates an EMPTY function of the same name + re-adds the captured input/output pins (no
        node wiring). Returns {function, removed, captured, inverse_is_lossy, ledger_depth}. BlueprintFunc-only;
        needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name}
        try:
            return json.dumps(_exec(_DELETE_FUNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 7) override_blueprint_function -> inverse op 'delete_blueprint_function'
    # ================================================================== #
    _OVERRIDE_BODY = _HELP + r'''
rl = _mrl("override_blueprint_function_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("override_blueprint_function")))
else:
    res = _decode(rl.override_blueprint_function_json(PARAMS["blueprint_path"], PARAMS["function_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("created"):
            _ledger().append({"op": "delete_blueprint_function", "asset_path": PARAMS["blueprint_path"],
                "function_name": res.get("function", PARAMS["function_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def override_blueprint_function(ctx, blueprint_path: str, function_name: str) -> str:
        """Override an overridable PARENT function as a Blueprint function graph (non-event override).

        blueprint_path: Blueprint asset path.
        function_name:  name of an overridable parent UFUNCTION (a BlueprintNativeEvent / BlueprintImplementable
                        function, or an interface function). NOT for simple events like BeginPlay -- those are
                        event-node overrides (use add_event_override).

        Mirrors UBlueprintEditorLibrary::AddFunctionOverride: ConformImplementedInterfaces +
        GetOverrideFunctionClass + AddFunctionGraph<UClass>(bp, graph, /*bIsUserCreated*/false, parentClass)
        so the terminator nodes inherit the parent signature and a CallParentFunction node is emitted. Refuses
        if the function is already overridden as an EVENT node (mutually exclusive) and no-ops (created=false)
        if the graph already exists. Marks structurally modified (NO compile). Appends inverse op
        'delete_blueprint_function' when it created a new graph. Returns {function, override_source_class,
        created, already_present?, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until
        built)."""
        params = {"blueprint_path": blueprint_path, "function_name": function_name}
        try:
            return json.dumps(_exec(_OVERRIDE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 8) create_event_graph -> inverse op 'delete_event_graph'
    # ================================================================== #
    _CREATE_EVENTGRAPH_BODY = _HELP + r'''
rl = _mrl("create_event_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_event_graph")))
else:
    res = _decode(rl.create_event_graph_json(PARAMS["blueprint_path"], PARAMS["graph_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "delete_event_graph", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS["graph_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_event_graph(ctx, blueprint_path: str, graph_name: str) -> str:
        """Create a new EVENT graph (an additional ubergraph page / event-graph tab).

        blueprint_path: Blueprint asset path.
        graph_name:     name for the new event graph (must not collide with any existing graph).

        Uses CreateNewGraph(UEdGraphSchema_K2) + UbergraphPages.Add (mirrors the AddEventNode graph-creation
        precedent). Marks structurally modified (NO compile). Appends inverse op 'delete_event_graph'. Returns
        {graph, created, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_CREATE_EVENTGRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 9) rename_event_graph -> inverse op 'rename_event_graph' (swapped)
    # ================================================================== #
    _RENAME_EVENTGRAPH_BODY = _HELP + r'''
rl = _mrl("rename_event_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("rename_event_graph")))
else:
    res = _decode(rl.rename_event_graph_json(PARAMS["blueprint_path"], PARAMS["old_name"], PARAMS["new_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "rename_event_graph", "asset_path": PARAMS["blueprint_path"],
            "old_name": res.get("new_name", PARAMS["new_name"]),
            "new_name": res.get("old_name", PARAMS["old_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def rename_event_graph(ctx, blueprint_path: str, old_name: str, new_name: str) -> str:
        """Rename an EVENT graph (ubergraph page).

        blueprint_path: Blueprint asset path.
        old_name:       current event-graph name.
        new_name:       new name (must not collide with any existing graph).

        Uses FBlueprintEditorUtils::RenameGraph. Marks structurally modified (NO compile). Appends inverse op
        'rename_event_graph' with old/new swapped (rename back). Returns {old_name, new_name, renamed,
        ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_EVENTGRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 10) delete_event_graph -> inverse op 'recreate_event_graph' (LOSSY: empty graph)
    # ================================================================== #
    _DELETE_EVENTGRAPH_BODY = _HELP + r'''
rl = _mrl("delete_event_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_event_graph")))
else:
    res = _decode(rl.delete_event_graph_json(PARAMS["blueprint_path"], PARAMS["graph_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "recreate_event_graph", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS["graph_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_event_graph(ctx, blueprint_path: str, graph_name: str) -> str:
        """Delete an EVENT graph (ubergraph page) via RemoveGraph.

        blueprint_path: Blueprint asset path.
        graph_name:     name of the event graph to remove.

        LOSSY: only the graph name is captured -- the nodes are NOT. Marks structurally modified (NO compile).
        Appends inverse op 'recreate_event_graph' whose fold re-creates an EMPTY event graph of the same name
        (node contents are NOT restored). Returns {graph, removed_node_count, removed, inverse_is_lossy,
        ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_DELETE_EVENTGRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 11) add_event_dispatcher_input -> inverse op 'remove_event_dispatcher_input'
    # ================================================================== #
    _ADD_DISPATCHER_INPUT_BODY = _HELP + r'''
rl = _mrl("add_event_dispatcher_input_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_event_dispatcher_input")))
else:
    ty = PARAMS.get("type", "")
    ty_json = ty if isinstance(ty, str) else json.dumps(ty)
    res = _decode(rl.add_event_dispatcher_input_json(PARAMS["blueprint_path"], PARAMS["dispatcher_name"],
        PARAMS["pin_name"], ty_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "remove_event_dispatcher_input", "asset_path": PARAMS["blueprint_path"],
            "dispatcher_name": res.get("dispatcher", PARAMS["dispatcher_name"]),
            "pin_name": res.get("param", PARAMS["pin_name"])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_event_dispatcher_input(ctx, blueprint_path: str, dispatcher_name: str, pin_name: str, type) -> str:
        """Add a parameter to an event dispatcher's (multicast-delegate) SIGNATURE.

        blueprint_path: Blueprint asset path.
        dispatcher_name: name of an existing event dispatcher (created via add_event_dispatcher).
        pin_name:        parameter name (must be unique among the dispatcher's params).
        type:            a type spec -- bare scalar token or {"category":...,"type_path":...,"container":...}.

        Resolves the dispatcher's signature graph (GetDelegateSignatureGraphByName), then adds an EGPD_Output
        user pin to its entry node (dispatcher params ARE entry outputs) and reconstructs. Marks structurally
        modified (NO compile). Appends inverse op 'remove_event_dispatcher_input'. Returns {dispatcher, param,
        type, added, ledger_depth}. BlueprintFunc-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "dispatcher_name": dispatcher_name,
                  "pin_name": pin_name, "type": type}
        try:
            return json.dumps(_exec(_ADD_DISPATCHER_INPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
