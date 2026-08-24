"""UserTools :: BehaviorTree BREAKPOINT wiring (debug category, Wave 4)  (spec: docs/spec/debug.md)

DRAFT wiring for the BehaviorTree-breakpoint C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_BTDebug.cpp. Every tool here is hasattr-guarded on
a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin DLL is rebuilt with
those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS
injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from the gold-standard
blueprint_graph_cpp.py / niagara_runtime_cpp.py.

Three tools -- BT breakpoints live in the EDITOR graph (UBehaviorTreeGraph), not the runtime tree:

  READ (no ledger):
    * list_bt_breakpoints   -- walk the BT's editor graph, emit every node that carries a breakpoint:
                               {node_id, node_guid, node_title, node_class, enabled}.

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * set_bt_breakpoint     -- place/enable/disable a breakpoint on one node (resolved by node_id). Captures
                               prior {present, enabled}. Ledger op 'set_bt_breakpoint'.
    * remove_bt_breakpoint  -- clear one node's breakpoint (by node_id) or EVERY node's (remove_all).
                               Captures each cleared node's prior enabled state. Ledger op
                               'remove_bt_breakpoint'.

NODE IDENTITY (node_id): matched to get_behavior_tree_info (ai_read.py) -- that reader emits each node's
`name` = get_editor_property("node_name"), i.e. the runtime UBTNode's NodeName. So node_id is that NodeName
(the C++ resolves it via reflection on the graph node's NodeInstance for an exact match). A node's stable
UEdGraphNode GUID is ALSO accepted as node_id and is returned by every tool as node_guid, so a
list -> remove round-trip is unambiguous even when a NodeName is blank or duplicated.

TRANSIENT (by design): BT breakpoints are stored in transient, non-UPROPERTY bitfields on the graph node --
they are NOT saved with the asset and are cleared on editor restart. list_bt_breakpoints right after a
restart is therefore empty; the writes do NOT dirty the package. "Undo" is cosmetic; the ledger is recorded
only for cross-tool consistency (like debug.py's draw ledger).

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write
appends its inverse descriptor to the shared per-session ledger for editor_level.undo to fold:
  * set_bt_breakpoint   -> op 'set_bt_breakpoint'    {bt_path, node_id, node_guid, prior_present, prior_enabled}
                           fold: prior_present ? re-set(prior_enabled) : remove that node's breakpoint.
  * remove_bt_breakpoint -> op 'remove_bt_breakpoint' {bt_path, all, cleared:[{node_id, node_guid, prior_enabled}]}
                           fold: re-set each cleared node with its prior_enabled.

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

# --- Output Log auto-capture (copied verbatim from blueprint_graph_cpp.py) ----
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
    return {"status": "error", "error": (fn + " requires the C++ BT-breakpoint handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_BTDebug.cpp to enable it.")}
'''

    # ================================================================== #
    # READ (no ledger). Hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #
    _LIST_BODY = _HELP + r'''
rl = _mrl("list_bt_breakpoints_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_bt_breakpoints")))
else:
    res = _decode(rl.list_bt_breakpoints_json(PARAMS["behavior_tree_path"], int(PARAMS.get("max_results", 200))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_bt_breakpoints(ctx, behavior_tree_path: str, max_results: int = 200) -> str:
        """List every node in a BehaviorTree's editor graph that currently carries a breakpoint.

        behavior_tree_path: BehaviorTree asset path (e.g. '/Game/AI/BT_Enemy.BT_Enemy' or '/Game/AI/BT_Enemy').
        max_results:        cap on entries returned (default 200; <=0 means no cap).

        Returns {behavior_tree, behavior_tree_path, breakpoint_count, breakpoints:[{node_id, node_guid,
        node_title, node_class, enabled}], truncated, transient, note}. node_id is the node's NodeName
        (matching get_behavior_tree_info's `name`); node_guid is the stable editor-graph GUID.

        BT breakpoints are TRANSIENT (session-only, not saved with the asset) -- this list is EMPTY after an
        editor restart by design. Needs the C++ handler (inert until the plugin DLL is rebuilt with
        MCPReflection_BTDebug.cpp)."""
        params = {"behavior_tree_path": behavior_tree_path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    # set_bt_breakpoint -> inverse op 'set_bt_breakpoint' (prior_present ? restore enabled : remove).
    _SET_BODY = _HELP + r'''
rl = _mrl("set_bt_breakpoint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_bt_breakpoint")))
else:
    res = _decode(rl.set_bt_breakpoint_json(PARAMS["behavior_tree_path"], PARAMS["node_id"],
        bool(PARAMS.get("enabled", True))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_bt_breakpoint", "bt_path": PARAMS["behavior_tree_path"],
            "node_id": res.get("node_id"), "node_guid": res.get("node_guid"),
            "prior_present": bool(res.get("prior_present")), "prior_enabled": bool(res.get("prior_enabled"))})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_bt_breakpoint(ctx, behavior_tree_path: str, node_id: str, enabled: bool = True) -> str:
        """Place (or enable/disable) a breakpoint on one BehaviorTree node in its editor graph.

        behavior_tree_path: BehaviorTree asset path.
        node_id:            the node's NodeName (as get_behavior_tree_info emits it via `name`) OR the
                            editor-graph node_guid from list_bt_breakpoints. Only Task/Composite nodes accept
                            breakpoints (CanPlaceBreakpoints()); a decorator/service/root node_id is rejected.
        enabled:            breakpoint enabled state (default True). Set False for a disabled breakpoint.

        Sets the transient debugger bitfields bHasBreakpoint / bIsBreakpointEnabled directly on the graph
        node. Captures prior {present, enabled} and appends inverse op 'set_bt_breakpoint'. Returns
        {behavior_tree, behavior_tree_path, node_id, node_guid, node_title, node_class, prior_present,
        prior_enabled, now_present, now_enabled, set, transient, ledger_depth}.

        TRANSIENT: the breakpoint is NOT saved with the asset and is cleared on editor restart; the package
        is not dirtied. Needs the C++ handler (inert until built with MCPReflection_BTDebug.cpp)."""
        params = {"behavior_tree_path": behavior_tree_path, "node_id": node_id, "enabled": enabled}
        try:
            return json.dumps(_exec(_SET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # remove_bt_breakpoint -> inverse op 'remove_bt_breakpoint' (re-set each cleared node's prior_enabled).
    _REMOVE_BODY = _HELP + r'''
rl = _mrl("remove_bt_breakpoint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_bt_breakpoint")))
else:
    res = _decode(rl.remove_bt_breakpoint_json(PARAMS["behavior_tree_path"], PARAMS.get("node_id", ""),
        bool(PARAMS.get("remove_all", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("cleared_count"):
            _ledger().append({"op": "remove_bt_breakpoint", "bt_path": PARAMS["behavior_tree_path"],
                "all": bool(PARAMS.get("remove_all", False)), "cleared": res.get("cleared", [])})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_bt_breakpoint(ctx, behavior_tree_path: str, node_id: str = "", remove_all: bool = False) -> str:
        """Clear one BehaviorTree node's breakpoint (by node_id) or EVERY node's (remove_all).

        behavior_tree_path: BehaviorTree asset path.
        node_id:            NodeName or node_guid of the node to clear (ignored when remove_all is True).
        remove_all:         True -> clear breakpoints on every node in the editor graph.

        Captures each cleared node's prior enabled state and appends inverse op 'remove_bt_breakpoint'
        (re-sets each on undo). Returns {behavior_tree, behavior_tree_path, all, removed, cleared_count,
        cleared:[{node_id, node_guid, node_title, node_class, prior_enabled}], transient, ledger_depth}.

        TRANSIENT: BT breakpoints are session-only (not saved); the package is not dirtied. Needs the C++
        handler (inert until built with MCPReflection_BTDebug.cpp)."""
        params = {"behavior_tree_path": behavior_tree_path, "node_id": node_id, "remove_all": remove_all}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
