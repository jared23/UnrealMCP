"""UserTools :: Animation Blueprint AnimGraph authoring (WRITE) -- state machines / states /
transitions / layers.  (C++ #18 Group-3 handlers on unreal.MCPReflectionLibrary)

Thin Python wrappers over the 13 LIVE C++ AnimGraph handlers (MCPReflection_AnimGraph.cpp). Each
mutator authors editor-only EdGraph objects (UAnimGraphNode_*/UAnimStateNode/UAnimStateTransitionNode)
directly in C++, captures its own prior_* state, and calls MarkBlueprintAsStructurallyModified. The
Python side just resolves the AnimBlueprint, wraps the call in unreal.ScopedEditorTransaction, and
pushes an inverse op onto the per-session undo ledger (builtins._UMCP_LEDGERS[session]) for the ONE
unified editor_level.undo to fold.

Query convention (@@UMCP@@ marker + Output-Log auto-capture), base64 PARAMS injection, and the
per-session _ledger() are copied VERBATIM from the gold-standard editor_level.py / anim_write.py.

IMPORTANT contract note: the C++ handlers return {"status":"ok", ...} on success and
{"status":"error","error":...} on failure -- this module normalizes "ok" -> "success" for the
Python-facing return and treats an "error" status (or any "error" field) as a failure.

GC-CAUTION (learned from the StateTree crashes): AnimBP compile + GC can purge editor graph state and
probing a stale wrapper HARD-CRASHES python311.dll (uncatchable). So this module NEVER calls
compile_blueprint and NEVER probes editor internals. Each mutating tool takes save=True by default
(a plain package save, NOT a compile) so standalone calls persist; pass save=False to batch a
GC-free tight sequence and save once at the end.

The 13 tools (all hasattr-guarded on unreal.MCPReflectionLibrary):
  add_anim_state_machine / add_anim_state / add_anim_transition / set_anim_entry_state /
  get_anim_state_machine_json (read) / remove_anim_state / remove_anim_transition /
  set_anim_transition_property / build_anim_state_machine / set_anim_node_pin_exposure /
  bind_anim_node_function / add_anim_layer / create_anim_layer_interface

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The op
inverses are reported to the coordinator to fold into editor_level.undo (see each tool docstring and
the module report). Every inverse re-calls the SAME C++ handler with the captured prior -- NO
export_text of nested structs.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py / anim_write.py) -------------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


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
    # Shared Unreal-side helpers + the single dispatch body. No triple-   #
    # single-quote / no backslash inside this r-string.                   #
    # ------------------------------------------------------------------ #
    _ASM_BODY = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_abp(path):
    if not path:
        return None, "no anim_blueprint_path given"
    try:
        o = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "failed to load: %s (%s)" % (path, str(e)[:140])
    if o is None:
        return None, "asset not found: %s" % path
    if not isinstance(o, unreal.AnimBlueprint):
        return None, "asset is not an AnimBlueprint (got %s): %s" % (o.get_class().get_name(), path)
    return o, None
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
def _is_err(r):
    return (not isinstance(r, dict)) or r.get("status") == "error" or bool(r.get("error"))
def _emit(d):
    print("@@UMCP@@" + json.dumps(d))

_action = PARAMS["action"]
_fnmap = {
    "add_state_machine": "add_anim_state_machine",
    "add_state": "add_anim_state",
    "add_transition": "add_anim_transition",
    "set_entry_state": "set_anim_entry_state",
    "get_state_machine": "get_anim_state_machine_json",
    "remove_state": "remove_anim_state",
    "remove_transition": "remove_anim_transition",
    "set_transition_property": "set_anim_transition_property",
    "build_state_machine": "build_anim_state_machine",
    "set_node_pin_exposure": "set_anim_node_pin_exposure",
    "bind_node_function": "bind_anim_node_function",
    "add_layer": "add_anim_layer",
    "create_layer_interface": "create_anim_layer_interface",
}
_rl = getattr(unreal, "MCPReflectionLibrary", None)
_fn = _fnmap.get(_action)
if _rl is None or _fn is None or not hasattr(_rl, _fn):
    _emit({"status": "error",
           "message": "MCPReflectionLibrary.%s unavailable -- reload the MCP server after the C++ #18 (AnimGraph) rebuild" % (_fn or _action)})
elif _action == "create_layer_interface":
    with unreal.ScopedEditorTransaction("MCP anim create_layer_interface"):
        _res = json.loads(_rl.create_anim_layer_interface(PARAMS["package_path"], PARAMS["asset_name"]))
    if _is_err(_res):
        _emit({"status": "error", "message": _res.get("error", "create_anim_layer_interface failed"), "raw": _res})
    else:
        _apath = _res.get("path")
        _save(_apath)
        _ledger().append({"op": "anim_create_layer_interface", "asset_path": _apath,
                          "package_path": _res.get("package")})
        _res["status"] = "success"; _res["ledger_depth"] = len(_ledger())
        _emit(_res)
else:
    _abp, _err = _load_abp(PARAMS.get("anim_blueprint_path"))
    if _err:
        _emit({"status": "error", "message": _err})
    elif _action == "get_state_machine":
        _res = json.loads(_rl.get_anim_state_machine_json(_abp, PARAMS["machine"]))
        if _is_err(_res):
            _emit({"status": "error", "message": _res.get("error", "get_anim_state_machine_json failed"), "raw": _res})
        else:
            _res["status"] = "success"
            _emit(_res)
    else:
        with unreal.ScopedEditorTransaction("MCP anim " + _action):
            if _action == "add_state_machine":
                _res = json.loads(_rl.add_anim_state_machine(_abp, PARAMS["machine"]))
            elif _action == "add_state":
                _res = json.loads(_rl.add_anim_state(_abp, PARAMS["machine"], PARAMS["state"]))
            elif _action == "add_transition":
                _res = json.loads(_rl.add_anim_transition(_abp, PARAMS["machine"], PARAMS["from_state"], PARAMS["to_state"]))
            elif _action == "set_entry_state":
                _res = json.loads(_rl.set_anim_entry_state(_abp, PARAMS["machine"], PARAMS["state"]))
            elif _action == "remove_state":
                _res = json.loads(_rl.remove_anim_state(_abp, PARAMS["machine"], PARAMS["state"]))
            elif _action == "remove_transition":
                _res = json.loads(_rl.remove_anim_transition(_abp, PARAMS["machine"], PARAMS["from_state"], PARAMS["to_state"]))
            elif _action == "set_transition_property":
                _res = json.loads(_rl.set_anim_transition_property(_abp, PARAMS["machine"], PARAMS["from_state"], PARAMS["to_state"], PARAMS["property"], PARAMS["value_json"]))
            elif _action == "build_state_machine":
                _res = json.loads(_rl.build_anim_state_machine(_abp, PARAMS["machine"], PARAMS["spec_json"]))
            elif _action == "set_node_pin_exposure":
                _res = json.loads(_rl.set_anim_node_pin_exposure(_abp, PARAMS["node_guid"], PARAMS["property"], bool(PARAMS["expose"])))
            elif _action == "bind_node_function":
                _res = json.loads(_rl.bind_anim_node_function(_abp, PARAMS["node_guid"], PARAMS["slot"], PARAMS.get("function") or ""))
            elif _action == "add_layer":
                _res = json.loads(_rl.add_anim_layer(_abp, PARAMS["layer"]))
            else:
                _res = {"status": "error", "error": "unknown action: %s" % _action}
        if _is_err(_res):
            _emit({"status": "error", "message": _res.get("error", "%s failed" % _action), "raw": _res})
        else:
            _ap = PARAMS.get("anim_blueprint_path")
            if _action == "add_state_machine":
                _op = {"op": "anim_add_state_machine", "asset_path": _ap, "machine": _res.get("state_machine_name"),
                       "node_guid": _res.get("node_guid")}
            elif _action == "add_state":
                _op = {"op": "anim_add_state", "asset_path": _ap, "machine": PARAMS["machine"], "state": _res.get("state_name")}
            elif _action == "add_transition":
                _op = {"op": "anim_add_transition", "asset_path": _ap, "machine": PARAMS["machine"],
                       "from_state": PARAMS["from_state"], "to_state": PARAMS["to_state"]}
            elif _action == "set_entry_state":
                _op = {"op": "anim_set_entry_state", "asset_path": _ap, "machine": PARAMS["machine"],
                       "prior_entry_state": _res.get("prior_entry_state")}
            elif _action == "remove_state":
                _op = {"op": "anim_remove_state", "asset_path": _ap, "machine": PARAMS["machine"],
                       "state": PARAMS["state"], "prior_transitions": _res.get("prior_transitions")}
            elif _action == "remove_transition":
                _op = {"op": "anim_remove_transition", "asset_path": _ap, "machine": PARAMS["machine"],
                       "from_state": PARAMS["from_state"], "to_state": PARAMS["to_state"],
                       "prior_priority_order": _res.get("prior_priority_order"),
                       "prior_crossfade_duration": _res.get("prior_crossfade_duration"),
                       "prior_disabled": _res.get("prior_disabled")}
            elif _action == "set_transition_property":
                _op = {"op": "anim_set_transition_property", "asset_path": _ap, "machine": PARAMS["machine"],
                       "from_state": PARAMS["from_state"], "to_state": PARAMS["to_state"],
                       "property": PARAMS["property"], "prior_value": _res.get("prior_value")}
            elif _action == "build_state_machine":
                _op = {"op": "anim_build_state_machine", "asset_path": _ap, "machine": PARAMS["machine"],
                       "spec": PARAMS.get("spec_json"), "states_added": _res.get("states_added"),
                       "transitions_added": _res.get("transitions_added")}
            elif _action == "set_node_pin_exposure":
                _op = {"op": "anim_set_node_pin_exposure", "asset_path": _ap, "node_guid": PARAMS["node_guid"],
                       "property": PARAMS["property"], "prior_exposed": _res.get("prior_exposed")}
            elif _action == "bind_node_function":
                _op = {"op": "anim_bind_node_function", "asset_path": _ap, "node_guid": PARAMS["node_guid"],
                       "slot": PARAMS["slot"], "prior_function": _res.get("prior_function")}
            elif _action == "add_layer":
                _op = {"op": "anim_add_layer", "asset_path": _ap, "layer_name": _res.get("layer_name")}
            else:
                _op = None
            if _op is not None:
                _ledger().append(_op)
            if PARAMS.get("save"):
                _save(_ap)
            _res["status"] = "success"; _res["ledger_depth"] = len(_ledger())
            _emit(_res)
'''

    def _run(action, params):
        p = dict(params or {}); p["action"] = action
        try:
            return json.dumps(_exec(_ASM_BODY, p), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    #  1) add_anim_state_machine                                          #
    # ================================================================== #
    @mcp.tool()
    def add_anim_state_machine(ctx, anim_blueprint_path: str, machine: str, save: bool = True) -> str:
        """Place a UAnimGraphNode_StateMachine in the AnimBlueprint's top-level AnimGraph. Ledgered write.

        anim_blueprint_path: object path of a UAnimBlueprint, e.g. '/Game/MCP_Scratch/ABP_Foo.ABP_Foo'.
        machine:             name for the new state-machine (the EditorStateMachineGraph is renamed to it).
        save:                persist the AnimBP package after the edit (default True; NOT a compile).

        Calls MCPReflectionLibrary.add_anim_state_machine (C++ #18). Returns the actual
        state_machine_name + node_guid.

        Ledgered op 'anim_add_state_machine' {asset_path, machine, node_guid}. INVERSE = DEFERRED: there
        is no C++ handler to remove a whole state-machine node (only remove_anim_state/transition exist),
        and there is no Python-reachable FBlueprintEditorUtils::RemoveGraph. A 'remove_anim_state_machine'
        C++ handler (DestroyNode on the SM node by guid) is required for a faithful inverse -- flagged to
        the coordinator as a deferred edge."""
        return _run("add_state_machine", {"anim_blueprint_path": anim_blueprint_path, "machine": machine, "save": save})

    # ================================================================== #
    #  2) add_anim_state                                                  #
    # ================================================================== #
    @mcp.tool()
    def add_anim_state(ctx, anim_blueprint_path: str, machine: str, state: str, save: bool = True) -> str:
        """Add a UAnimStateNode (with its inner bound graph) to a state machine. Ledgered write.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name (create via add_anim_state_machine).
        state:               name for the new state (refused if it already exists in the machine).
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.add_anim_state (C++ #18).

        Ledgered op 'anim_add_state' {asset_path, machine, state}. INVERSE (fold into editor_level.undo):
        remove_anim_state(machine, state) -- FAITHFUL (the state is created new + empty)."""
        return _run("add_state", {"anim_blueprint_path": anim_blueprint_path, "machine": machine, "state": state, "save": save})

    # ================================================================== #
    #  3) add_anim_transition                                             #
    # ================================================================== #
    @mcp.tool()
    def add_anim_transition(ctx, anim_blueprint_path: str, machine: str, from_state: str, to_state: str, save: bool = True) -> str:
        """Link FromState -> ToState with a UAnimStateTransitionNode. Ledgered write.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name.
        from_state/to_state: existing state names (refused if a transition between them already exists).
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.add_anim_transition (C++ #18).

        Ledgered op 'anim_add_transition' {asset_path, machine, from_state, to_state}. INVERSE (fold into
        editor_level.undo): remove_anim_transition(machine, from_state, to_state) -- FAITHFUL (newly
        created; the transition rule graph is empty)."""
        return _run("add_transition", {"anim_blueprint_path": anim_blueprint_path, "machine": machine,
                                       "from_state": from_state, "to_state": to_state, "save": save})

    # ================================================================== #
    #  4) set_anim_entry_state                                            #
    # ================================================================== #
    @mcp.tool()
    def set_anim_entry_state(ctx, anim_blueprint_path: str, machine: str, state: str, save: bool = True) -> str:
        """Point the state-machine entry node at a state. Ledgered write. Captures prior_entry_state.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name.
        state:               the state to make the entry (existing state).
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.set_anim_entry_state (C++ #18); returns prior_entry_state (null if the
        entry was previously unset).

        Ledgered op 'anim_set_entry_state' {asset_path, machine, prior_entry_state}. INVERSE (fold into
        editor_level.undo): set_anim_entry_state(machine, prior_entry_state) IF prior_entry_state is
        non-null -- FAITHFUL. LOSSY EDGE: if prior_entry_state was null (no entry) the handler cannot
        clear the entry link, so undo leaves it pointing at the last state (documented)."""
        return _run("set_entry_state", {"anim_blueprint_path": anim_blueprint_path, "machine": machine, "state": state, "save": save})

    # ================================================================== #
    #  5) get_anim_state_machine_json  (READ)                             #
    # ================================================================== #
    @mcp.tool()
    def get_anim_state_machine_json(ctx, anim_blueprint_path: str, machine: str) -> str:
        """Read a state machine's states / transitions / entry_state (read-only, no ledger).

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             state-machine name.

        Calls MCPReflectionLibrary.get_anim_state_machine_json (C++ #18). Returns state_count,
        transition_count, entry_state, states[] (name/guid/always_reset_on_entry), transitions[]
        (from/to/guid/priority_order/crossfade_duration/disabled)."""
        return _run("get_state_machine", {"anim_blueprint_path": anim_blueprint_path, "machine": machine})

    # ================================================================== #
    #  6) remove_anim_state                                               #
    # ================================================================== #
    @mcp.tool()
    def remove_anim_state(ctx, anim_blueprint_path: str, machine: str, state: str, save: bool = True) -> str:
        """Delete a UAnimStateNode (and its bound graph). Ledgered write. Captures outgoing transitions.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name.
        state:               the state to remove.
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.remove_anim_state (C++ #18); returns prior_transitions[] (the state's
        connected from/to pairs).

        Ledgered op 'anim_remove_state' {asset_path, machine, state, prior_transitions}. INVERSE (fold
        into editor_level.undo): add_anim_state(machine, state) + re-add each prior transition via
        add_anim_transition. BEST-EFFORT / LOSSY: the state's inner bound-graph pose and each transition
        rule graph + crossfade/priority are NOT restored (only structure) -- documented lossy edge."""
        return _run("remove_state", {"anim_blueprint_path": anim_blueprint_path, "machine": machine, "state": state, "save": save})

    # ================================================================== #
    #  7) remove_anim_transition                                          #
    # ================================================================== #
    @mcp.tool()
    def remove_anim_transition(ctx, anim_blueprint_path: str, machine: str, from_state: str, to_state: str, save: bool = True) -> str:
        """Delete the transition linking two states. Ledgered write. Captures crossfade/priority/disabled.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name.
        from_state/to_state: the transition endpoints.
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.remove_anim_transition (C++ #18); returns prior_priority_order,
        prior_crossfade_duration, prior_disabled.

        Ledgered op 'anim_remove_transition' {asset_path, machine, from_state, to_state,
        prior_priority_order, prior_crossfade_duration, prior_disabled}. INVERSE (fold into
        editor_level.undo): add_anim_transition(from,to) + set_anim_transition_property for PriorityOrder,
        CrossfadeDuration, bDisabled. FAITHFUL for those 3 scalar props; the transition RULE graph logic
        is not restored (documented lossy edge)."""
        return _run("remove_transition", {"anim_blueprint_path": anim_blueprint_path, "machine": machine,
                                          "from_state": from_state, "to_state": to_state, "save": save})

    # ================================================================== #
    #  8) set_anim_transition_property                                    #
    # ================================================================== #
    @mcp.tool()
    def set_anim_transition_property(ctx, anim_blueprint_path: str, machine: str, from_state: str, to_state: str,
                                     property: str, value_json: str, save: bool = True) -> str:
        """Set a UPROPERTY on a transition node via reflection. Ledgered write. Captures prior_value.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             existing state-machine name.
        from_state/to_state: the transition endpoints.
        property:            UAnimStateTransitionNode property name, e.g. 'CrossfadeDuration',
                             'PriorityOrder', 'bDisabled', 'BlendMode', 'LogicType'.
        value_json:          the value as a JSON scalar / ExportText string (e.g. '0.25', 'true', '3').
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.set_anim_transition_property (C++ #18, ImportText_Direct); returns
        prior_value (the ExportText form of the prior).

        Ledgered op 'anim_set_transition_property' {asset_path, machine, from_state, to_state, property,
        prior_value}. INVERSE (fold into editor_level.undo): set_anim_transition_property(property,
        prior_value) -- FAITHFUL scalar/enum restore (prior_value round-trips through ImportText)."""
        return _run("set_transition_property", {"anim_blueprint_path": anim_blueprint_path, "machine": machine,
                                                "from_state": from_state, "to_state": to_state,
                                                "property": property, "value_json": value_json, "save": save})

    # ================================================================== #
    #  9) build_anim_state_machine  (composite)                           #
    # ================================================================== #
    @mcp.tool()
    def build_anim_state_machine(ctx, anim_blueprint_path: str, machine: str, spec_json: str, save: bool = True) -> str:
        """Composite: create the machine (if absent) + states + transitions + entry from one JSON spec.

        anim_blueprint_path: object path of a UAnimBlueprint.
        machine:             state-machine name (created if missing, reused if present).
        spec_json:           JSON like {"states":["Idle","Run"], "entry":"Idle",
                             "transitions":[{"from":"Idle","to":"Run"}]}.
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.build_anim_state_machine (C++ #18); returns states_added,
        transitions_added, and partial_errors[] for any skipped items.

        Ledgered op 'anim_build_state_machine' {asset_path, machine, spec, states_added,
        transitions_added}. INVERSE (fold into editor_level.undo): remove each added transition, then each
        added state (parsed from spec). The machine NODE itself is NOT removed (same deferred edge as
        anim_add_state_machine) -- documented. If the machine pre-existed, undo leaves it in place."""
        return _run("build_state_machine", {"anim_blueprint_path": anim_blueprint_path, "machine": machine,
                                            "spec_json": spec_json, "save": save})

    # ================================================================== #
    # 10) set_anim_node_pin_exposure                                      #
    # ================================================================== #
    @mcp.tool()
    def set_anim_node_pin_exposure(ctx, anim_blueprint_path: str, node_guid: str, property: str, expose: bool, save: bool = True) -> str:
        """Toggle an optional property's input pin on an AnimGraph node. Ledgered write. Captures prior.

        anim_blueprint_path: object path of a UAnimBlueprint.
        node_guid:           NodeGuid of a UAnimGraphNode_Base (from add_anim_state_machine or a graph read).
        property:            an OPTIONAL (exposable) pin property name on that node.
        expose:              True to show the pin, False to hide it.
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.set_anim_node_pin_exposure (C++ #18); returns prior_exposed. NOTE:
        errors (structured, not a crash) if the property is not an optional pin on that node.

        Ledgered op 'anim_set_node_pin_exposure' {asset_path, node_guid, property, prior_exposed}. INVERSE
        (fold into editor_level.undo): set_anim_node_pin_exposure(node_guid, property, prior_exposed) --
        FAITHFUL boolean restore."""
        return _run("set_node_pin_exposure", {"anim_blueprint_path": anim_blueprint_path, "node_guid": node_guid,
                                              "property": property, "expose": expose, "save": save})

    # ================================================================== #
    # 11) bind_anim_node_function                                         #
    # ================================================================== #
    @mcp.tool()
    def bind_anim_node_function(ctx, anim_blueprint_path: str, node_guid: str, slot: str, function: str = "", save: bool = True) -> str:
        """Bind (or clear) a thread-safe function on an AnimGraph node slot. Ledgered write. Captures prior.

        anim_blueprint_path: object path of a UAnimBlueprint.
        node_guid:           NodeGuid of a UAnimGraphNode_Base.
        slot:                one of 'initial_update' | 'become_relevant' | 'update'.
        function:            member function name on the AnimBP's generated class; empty string CLEARS the binding.
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.bind_anim_node_function (C++ #18, SetSelfMember); returns prior_function
        (null if the slot was unbound).

        Ledgered op 'anim_bind_node_function' {asset_path, node_guid, slot, prior_function}. INVERSE (fold
        into editor_level.undo): bind_anim_node_function(node_guid, slot, prior_function or '') -- FAITHFUL
        (re-binds the prior member, or clears if it was unbound)."""
        return _run("bind_node_function", {"anim_blueprint_path": anim_blueprint_path, "node_guid": node_guid,
                                           "slot": slot, "function": function, "save": save})

    # ================================================================== #
    # 12) add_anim_layer                                                  #
    # ================================================================== #
    @mcp.tool()
    def add_anim_layer(ctx, anim_blueprint_path: str, layer: str, save: bool = True) -> str:
        """Add a new animation layer graph (UAnimationGraph) to the AnimBlueprint. Ledgered write.

        anim_blueprint_path: object path of a UAnimBlueprint.
        layer:               name for the new layer graph (refused if a graph of that name exists).
        save:                persist the AnimBP package (default True).

        Calls MCPReflectionLibrary.add_anim_layer (C++ #18, CreateNewGraph + AddDomainSpecificGraph);
        returns the actual layer_name.

        Ledgered op 'anim_add_layer' {asset_path, layer_name}. INVERSE = DEFERRED: removing the layer
        needs FBlueprintEditorUtils::RemoveGraph, which is NOT Python-reachable and has no C++ handler. A
        small 'remove_anim_layer' C++ handler (RemoveGraph by name) is required for a faithful inverse --
        flagged to the coordinator as a deferred edge."""
        return _run("add_layer", {"anim_blueprint_path": anim_blueprint_path, "layer": layer, "save": save})

    # ================================================================== #
    # 13) create_anim_layer_interface                                     #
    # ================================================================== #
    @mcp.tool()
    def create_anim_layer_interface(ctx, package_path: str, asset_name: str) -> str:
        """Create a new Anim Layer Interface asset (BPTYPE_Interface AnimBlueprint). Ledgered create.

        package_path: content directory, e.g. '/Game/MCP_Scratch'.
        asset_name:   asset name, e.g. 'ALI_Locomotion'.

        Calls MCPReflectionLibrary.create_anim_layer_interface (C++ #18); the Python side SAVES the new
        package (the C++ leaves it unsaved) so the asset persists and the soft-delete inverse is reliable.

        Ledgered op 'anim_create_layer_interface' {asset_path, package_path}. INVERSE (fold into
        editor_level.undo): WORLD-SAFE soft-delete = rename the asset to /Game/_MCP_Trash/<name> (never
        delete_asset -- avoids the delete->GC->python311.dll crash path)."""
        return _run("create_layer_interface", {"package_path": package_path, "asset_name": asset_name})

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The 13 op inverses
    # above are reported to the coordinator to fold into editor_level.undo (see module docstring / report).
