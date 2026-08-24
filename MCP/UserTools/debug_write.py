"""UserTools :: Blueprint DEBUGGER integration (spec: debug category — breakpoints/watches/state/trace).

DRAFT wiring for the Blueprint-debugger C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Debug.cpp (block "C++ #44"). Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention,
base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from the
gold-standard blueprint_graph_cpp.py.

These expose the editor's Blueprint debugger, which the stock Python API cannot reach. node_id == the
node's NodeGuid string (the SAME identity the blueprint-graph tools emit).

  WAVE 1 -- editor-only (safe with or without PIE):
    READS (no ledger):
      * list_blueprint_breakpoints    -- a BP's breakpoints {node_guid, node_title, enabled, location_description}.
      * list_blueprint_pin_watches     -- watched pins {node_guid, pin_name, path?}; values=true adds live value.
      * list_blueprint_debug_objects   -- live instances of the BP's generated class {path, name, world, ...}.
    WRITES (ledger -- reversible; inverse folds into editor_level.undo):
      * set_blueprint_breakpoint       -- create/enable a breakpoint on a node. Ledger op 'set_breakpoint'.
      * remove_blueprint_breakpoint    -- remove one node's breakpoint (or ALL). Ledger op 'remove_breakpoint'.
      * set_blueprint_pin_watch        -- add/remove a pin watch. Ledger op 'set_pin_watch'.
      * set_blueprint_debug_object     -- set/clear the object being debugged. Ledger op 'set_debug_object'.

  WAVE 2 -- PIE readers (no ledger; degrade cleanly to {debugging:false}/nulls outside PIE, NEVER error):
      * get_blueprint_debug_state      -- current instruction / most-recent breakpoint / stepping / world.
      * get_blueprint_execution_trace  -- the FKismetTraceSample ring buffer -> source nodes (readable AFTER a resume).
      * get_blueprint_call_stack       -- FFrame::GetScriptCallstack frames (only while a script frame is live/halted).
      * inspect_blueprint_debug_value  -- property tree of the set debug object (needs a debug object / PIE).

LEDGER CONTRACT (the coordinator folds the INVERSE into editor_level.undo):
  op 'set_breakpoint'   {blueprint_path, graph, node_guid, prior_enabled, prior_exists}
      -> inverse: prior_exists ? re-create @node_guid with prior_enabled : remove @node_guid.
  op 'remove_breakpoint'{blueprint_path, graph, node_guid, prior_enabled, prior_exists}
      -> inverse: re-create @node_guid with prior_enabled (only ledgered for a single-node remove, not clear-all).
  op 'set_pin_watch'    {blueprint_path, graph, node_guid, pin_name, removed, prior_watched}
      -> inverse: re-issue set_blueprint_pin_watch with `remove` flipped (restores prior_watched).
  op 'set_debug_object' {blueprint_path, prior_instance_path}
      -> inverse: prior_instance_path ? set back to it : clear.
  NOTE: breakpoint/watch/debug-object state is per-user editor / transient (like debug.py draws); undo is
  cosmetic. Clearing ALL breakpoints is NOT individually reversible (no per-node prior captured) -> not ledgered.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write
appends its inverse descriptor to the shared per-session ledger for editor_level.undo to fold.

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
    return {"status": "error", "error": (fn + " requires the C++ Blueprint-debugger handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Debug.cpp to enable it.")}
'''

    # ================================================================== #
    # WAVE 1 READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _LIST_BP_BODY = _HELP + r'''
rl = _mrl("list_blueprint_breakpoints_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_blueprint_breakpoints")))
else:
    res = _decode(rl.list_blueprint_breakpoints_json(PARAMS["blueprint_path"], PARAMS.get("filter", ""),
        int(PARAMS.get("max_results", 200))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_blueprint_breakpoints(ctx, blueprint_path: str, filter: str = "", max_results: int = 200) -> str:
        """List a Blueprint's breakpoints (editor-only; works with or without PIE).

        blueprint_path: Blueprint asset path (e.g. '/Game/BP_Foo.BP_Foo').
        filter:         case-insensitive substring over each breakpoint's node title (''=all).
        max_results:    cap on returned rows (<=0 = no cap).

        Returns {blueprint, breakpoint_count, returned, breakpoints:[{node_guid, node_title, enabled,
        location_description, graph?}]}. Needs the C++ handler (inert until the plugin DLL is rebuilt)."""
        params = {"blueprint_path": blueprint_path, "filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_WATCH_BODY = _HELP + r'''
rl = _mrl("list_blueprint_pin_watches_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_blueprint_pin_watches")))
else:
    res = _decode(rl.list_blueprint_pin_watches_json(PARAMS["blueprint_path"],
        bool(PARAMS.get("values", False)), int(PARAMS.get("max_results", 200))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_blueprint_pin_watches(ctx, blueprint_path: str, values: bool = False, max_results: int = 200) -> str:
        """List a Blueprint's watched pins; values=true also reads each live value (needs a debug object / PIE).

        blueprint_path: Blueprint asset path.
        values:         also emit each pin's current value via GetWatchText. Outside PIE / with no debug
                        object set, value is null with a value_note (never an error).
        max_results:    cap on returned rows (<=0 = no cap).

        Returns {blueprint, has_debug_object, debug_object?, watch_count, returned,
        watches:[{node_guid, pin_name, path?, value?, value_note?}]}. Needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "values": values, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_WATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_DBGOBJ_BODY = _HELP + r'''
rl = _mrl("list_blueprint_debug_objects_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_blueprint_debug_objects")))
else:
    res = _decode(rl.list_blueprint_debug_objects_json(PARAMS["blueprint_path"], PARAMS.get("filter", ""),
        int(PARAMS.get("max_results", 200))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_blueprint_debug_objects(ctx, blueprint_path: str, filter: str = "", max_results: int = 200) -> str:
        """List live instances of a Blueprint's generated class (candidate debug objects).

        blueprint_path: Blueprint asset path.
        filter:         case-insensitive substring over instance name/path (''=all).
        max_results:    cap on returned rows (<=0 = no cap).

        Outside PIE the only instances are editor-world placed actors; when none exist the CDO is emitted
        (is_cdo=true). Returns {blueprint, generated_class, pie_active, cdo_fallback, current_debug_object?,
        instance_count, returned, objects:[{path, name, world, world_type, in_debugging_world, is_current}]}.
        Needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_DBGOBJ_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WAVE 1 WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    # set_blueprint_breakpoint -> ledger op 'set_breakpoint' (inverse restores prior / removes if none existed).
    _SET_BP_BODY = _HELP + r'''
rl = _mrl("set_blueprint_breakpoint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_breakpoint")))
else:
    res = _decode(rl.set_blueprint_breakpoint_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"], bool(PARAMS.get("enabled", True))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_breakpoint", "asset_path": PARAMS["blueprint_path"],
            "blueprint_path": PARAMS["blueprint_path"], "graph": res.get("graph", PARAMS.get("graph_name", "")),
            "node_guid": res.get("node_guid", PARAMS["node_guid"]),
            "prior_enabled": bool(res.get("prior_enabled", False)), "prior_exists": bool(res.get("prior_exists", False))})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_breakpoint(ctx, blueprint_path: str, node_guid: str, enabled: bool = True,
                                 graph_name: str = "") -> str:
        """Create (or re-enable) a breakpoint on a Blueprint node, addressed by NodeGuid.

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID (from get_blueprint_graph / search_blueprint_nodes).
        enabled:        breakpoint enabled state.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Dup-safe: creates the breakpoint only if the node has none yet, else just sets its enabled flag.
        Captures prior state and appends inverse op 'set_breakpoint'. Returns {node_guid, node_title, graph,
        enabled, created, prior_exists, prior_enabled, ledger_depth}. Editor-only; needs the C++ handler
        (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "enabled": enabled,
                  "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SET_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # remove_blueprint_breakpoint -> ledger op 'remove_breakpoint' (inverse re-creates the removed breakpoint).
    # Only ledgered for a single-node remove that actually removed one (clear-all is not individually reversible).
    _REMOVE_BP_BODY = _HELP + r'''
rl = _mrl("remove_blueprint_breakpoint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_blueprint_breakpoint")))
else:
    res = _decode(rl.remove_blueprint_breakpoint_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS.get("node_guid", ""), bool(PARAMS.get("remove_all", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("removed") and not res.get("cleared_all"):
            _ledger().append({"op": "remove_breakpoint", "asset_path": PARAMS["blueprint_path"],
                "blueprint_path": PARAMS["blueprint_path"], "graph": res.get("graph", PARAMS.get("graph_name", "")),
                "node_guid": res.get("node_guid", PARAMS.get("node_guid", "")),
                "prior_enabled": bool(res.get("prior_enabled", False)), "prior_exists": bool(res.get("prior_exists", False))})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_blueprint_breakpoint(ctx, blueprint_path: str, node_guid: str = "", remove_all: bool = False,
                                    graph_name: str = "") -> str:
        """Remove one node's breakpoint (by NodeGuid), or ALL breakpoints in the Blueprint (remove_all=true).

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID (ignored when remove_all=true).
        remove_all:     clear every breakpoint in the Blueprint.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Captures the prior enabled state (single-node case) and appends inverse op 'remove_breakpoint'
        (re-create). Clearing ALL is NOT individually reversible (not ledgered). Returns {node_guid?,
        removed, removed_count, cleared_all, prior_exists?, prior_enabled?, ledger_depth}. Editor-only;
        needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "remove_all": remove_all,
                  "graph_name": graph_name}
        try:
            return json.dumps(_exec(_REMOVE_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # set_blueprint_pin_watch -> ledger op 'set_pin_watch' (inverse re-issues with `remove` flipped).
    # Only ledgered when the watch state actually changed.
    _SET_WATCH_BODY = _HELP + r'''
rl = _mrl("set_blueprint_pin_watch_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_pin_watch")))
else:
    res = _decode(rl.set_blueprint_pin_watch_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        PARAMS["node_guid"], PARAMS["pin_name"], bool(PARAMS.get("remove", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _changed = bool(res.get("watched")) != bool(res.get("prior_watched"))
        if _changed:
            _ledger().append({"op": "set_pin_watch", "asset_path": PARAMS["blueprint_path"],
                "blueprint_path": PARAMS["blueprint_path"], "graph": res.get("graph", PARAMS.get("graph_name", "")),
                "node_guid": res.get("node_guid", PARAMS["node_guid"]), "pin_name": res.get("pin_name", PARAMS["pin_name"]),
                "removed": bool(PARAMS.get("remove", False)), "prior_watched": bool(res.get("prior_watched", False))})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_pin_watch(ctx, blueprint_path: str, node_guid: str, pin_name: str, remove: bool = False,
                                graph_name: str = "") -> str:
        """Add (or remove) a pin watch on a Blueprint node's pin.

        blueprint_path: Blueprint asset path.
        node_guid:      the node's GUID.
        pin_name:       the pin's name.
        remove:         remove the watch instead of adding it.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.

        Add is CanWatchPin-guarded (rejects unwatchable pins with an error). Captures prior watched state
        and appends inverse op 'set_pin_watch' (re-issue with `remove` flipped) when the state changes.
        Returns {node_guid, pin_name, removed, did_remove, prior_watched, watched, ledger_depth}.
        Editor-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "node_guid": node_guid, "pin_name": pin_name,
                  "remove": remove, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SET_WATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # set_blueprint_debug_object -> ledger op 'set_debug_object' (inverse restores prior / clears).
    # Only ledgered when the debug object actually changed.
    _SET_DBGOBJ_BODY = _HELP + r'''
rl = _mrl("set_blueprint_debug_object_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_debug_object")))
else:
    res = _decode(rl.set_blueprint_debug_object_json(PARAMS["blueprint_path"], PARAMS.get("instance", ""),
        bool(PARAMS.get("clear", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _prior = res.get("prior_instance_path")
        _new = res.get("instance_path")
        if _prior != _new:
            _ledger().append({"op": "set_debug_object", "asset_path": PARAMS["blueprint_path"],
                "blueprint_path": PARAMS["blueprint_path"], "prior_instance_path": _prior})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_debug_object(ctx, blueprint_path: str, instance: str = "", clear: bool = False) -> str:
        """Set (or clear) the object being debugged for a Blueprint.

        blueprint_path: Blueprint asset path.
        instance:       object path OR name of a live instance of the BP's generated class (a PIE actor).
        clear:          clear the debug object (ignores `instance`).

        Captures the prior debug-object path and appends inverse op 'set_debug_object' (restore/clear) when
        it changes. Returns {blueprint, cleared, prior_instance_path, instance_path, ledger_depth}.
        Editor-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "instance": instance, "clear": clear}
        try:
            return json.dumps(_exec(_SET_DBGOBJ_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WAVE 2 PIE READERS (no ledger). Degrade cleanly outside PIE; never error.
    # ================================================================== #

    _STATE_BODY = _HELP + r'''
rl = _mrl("get_blueprint_debug_state_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_blueprint_debug_state")))
else:
    res = _decode(rl.get_blueprint_debug_state_json(PARAMS.get("detail", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_blueprint_debug_state(ctx, detail: str = "") -> str:
        """Current Blueprint-debugger state: current instruction, most-recent breakpoint, stepping, world.

        detail: 'full' also adds the execution-trace sample count.

        Degrades to {debugging:false, ...nulls} outside a live debugging session (never errors). Returns
        {debugging, single_stepping, world, world_type?, current_node, most_recent_breakpoint,
        trace_sample_count?}. Editor-only; needs the C++ handler (inert until built)."""
        params = {"detail": detail}
        try:
            return json.dumps(_exec(_STATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _TRACE_BODY = _HELP + r'''
rl = _mrl("get_blueprint_execution_trace_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_blueprint_execution_trace")))
else:
    res = _decode(rl.get_blueprint_execution_trace_json(PARAMS.get("blueprint_path", ""),
        int(PARAMS.get("max_results", 100))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_blueprint_execution_trace(ctx, blueprint_path: str = "", max_results: int = 100) -> str:
        """The Blueprint execution trace (newest-first ring buffer) mapped to source nodes.

        blueprint_path: optional filter -- only samples whose context is an instance of this BP's class.
        max_results:    cap on returned samples (<=0 = no cap).

        The MOST usable runtime reader: the ring fills during PIE and stays readable AFTER a resume. Empty
        (never errors) when nothing has executed. Returns {debugging, sample_count, returned, note?,
        samples:[{node_guid, node_title?, graph?, context, function, offset, observation_time}]}.
        Editor-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "max_results": max_results}
        try:
            return json.dumps(_exec(_TRACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _CALLSTACK_BODY = _HELP + r'''
rl = _mrl("get_blueprint_call_stack_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_blueprint_call_stack")))
else:
    res = _decode(rl.get_blueprint_call_stack_json(int(PARAMS.get("max_results", 100))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_blueprint_call_stack(ctx, max_results: int = 100) -> str:
        """The active Blueprint script call stack (FFrame::GetScriptCallstack), split into frames.

        max_results: cap on returned frames (<=0 = no cap).

        Populated ONLY while a script frame is executing/halted (e.g. at a breakpoint); empty otherwise
        (never errors). Returns {debugging, frame_count, note?, frames:[str]}. Editor-only; needs the C++
        handler (inert until built)."""
        params = {"max_results": max_results}
        try:
            return json.dumps(_exec(_CALLSTACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _INSPECT_BODY = _HELP + r'''
rl = _mrl("inspect_blueprint_debug_value_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("inspect_blueprint_debug_value")))
else:
    res = _decode(rl.inspect_blueprint_debug_value_json(PARAMS["blueprint_path"], PARAMS.get("path", ""),
        PARAMS.get("filter", ""), int(PARAMS.get("depth", 1)), int(PARAMS.get("max_results", 100))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def inspect_blueprint_debug_value(ctx, blueprint_path: str, path: str = "", filter: str = "",
                                      depth: int = 1, max_results: int = 100) -> str:
        """Inspect the property tree of a Blueprint's set debug object (needs a debug object / PIE).

        blueprint_path: Blueprint asset path.
        path:           dotted property path (e.g. 'Health' or 'MyStruct.SubField'); '' = all top-level.
        filter:         case-insensitive substring on top-level property name (only when path is empty).
        depth:          how many child levels to expand (0..8).
        max_results:    cap on children/top-level rows.

        Degrades to {debugging:false, note} when no debug object is set (never errors). Returns {blueprint,
        debugging, debug_object, debug_class, returned, values:[{name, value, type, children?}]}. Editor-only;
        needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "path": path, "filter": filter, "depth": depth,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_INSPECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
