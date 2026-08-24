"""UserTools :: PCG graph-execution INSPECTION (C++ #51).

Thin, resolve-guarded wrappers over four C++ handlers on unreal.MCPReflectionLibrary that expose the
FPCGGraphExecutionInspection surface of a UPCGComponent (reached via Comp->GetExecutionState().GetInspection()):

    set_pcg_inspection_enabled_json(actor_path, enable) -> {is_inspecting}
    get_pcg_inspection_json(actor_path, node_name)      -> {is_inspecting, executed_nodes:[{node_name,node_type,
                                                            stack,executed,produced_data,inactive_pin_mask,
                                                            gpu_to_cpu_readback,cpu_to_gpu_upload,
                                                            data_overrides_applied}], ...}
    inspect_pcg_node_output_json(actor_path, node_name) -> {tagged_data:[{data_class,pin,tags,num_points}], ...}
    clear_pcg_inspection_json(actor_path)               -> {cleared}

WORKFLOW: enable inspection on a component BEFORE generating (pcg_generate_component, Wave 4); generation in
editor is ASYNC so wait for it to finish; then the executed-node records + per-node output become inspectable.

REVERSIBILITY: every tool here is a transient runtime read / enable-disable-clear -> NON-LEDGERED. Enable and
disable are the natural inverse pair (the C++ counter is reference-counted); clear is idempotent. NO
editor_level.undo folds -> this module adds ZERO undo risk.

SAFETY: reads only; the component lives on an actor in the (transient) level. The handlers resolve the actor in
the editor world and never save. Resolve-guarded: inert with {"deferred":true} until the DLL lands, then live.
"""
import json
import base64
import os

MARKER = "@@UMCP@@"


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": code})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    _SET_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "set_pcg_inspection_enabled_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "set_pcg_inspection_enabled_json not built"}))
else:
    raw = fn(PARAMS.get("actor") or "", bool(PARAMS.get("enable", True)))
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _GET_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "get_pcg_inspection_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "get_pcg_inspection_json not built"}))
else:
    raw = fn(PARAMS.get("actor") or "", PARAMS.get("node") or "")
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _INSPECT_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "inspect_pcg_node_output_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "inspect_pcg_node_output_json not built"}))
else:
    raw = fn(PARAMS.get("actor") or "", PARAMS.get("node") or "")
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _CLEAR_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "clear_pcg_inspection_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "clear_pcg_inspection_json not built"}))
else:
    raw = fn(PARAMS.get("actor") or "")
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    # ---- client-side helpers over the shared _GET_BODY -------------------------------------------
    def _get(actor, node=None):
        return _exec(_GET_BODY, {"actor": actor, "node": node})

    def _records_for(res, node):
        recs = res.get("executed_nodes", []) if isinstance(res, dict) else []
        if node:
            recs = [r for r in recs if r.get("node_name") == node]
        return recs

    # =============================================================================================
    # Enable / disable / query inspection state
    # =============================================================================================
    @mcp.tool()
    def enable_pcg_node_inspection(ctx, actor: str) -> str:
        """Enable graph-execution inspection on the UPCGComponent of a level actor. MUST be called
        BEFORE pcg_generate_component so the executed-node data + per-node outputs are recorded.

        actor: actor identity in the current (transient) level — its label (e.g. 'PCGVolume'),
               internal name (e.g. 'PCGVolume_0'), or full path name. Must carry a UPCGComponent.

        Enable/disable are a reference-counted inverse pair -> NOT ledgered. Returns {is_inspecting}."""
        try:
            return json.dumps(_exec(_SET_BODY, {"actor": actor, "enable": True}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def disable_pcg_node_inspection(ctx, actor: str) -> str:
        """Disable graph-execution inspection on the UPCGComponent of a level actor (inverse of
        enable_pcg_node_inspection). NOT ledgered. Returns {is_inspecting}.

        actor: actor identity (label / internal name / full path) carrying a UPCGComponent."""
        try:
            return json.dumps(_exec(_SET_BODY, {"actor": actor, "enable": False}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pcg_is_inspecting(ctx, actor: str) -> str:
        """Report whether graph-execution inspection is currently enabled on a level actor's
        UPCGComponent. Read-only.

        actor: actor identity (label / internal name / full path) carrying a UPCGComponent.

        Returns {actor, is_inspecting}."""
        try:
            res = _get(actor)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            return json.dumps({"status": "success", "actor": res.get("actor"),
                               "is_inspecting": bool(res.get("is_inspecting"))}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # =============================================================================================
    # Executed-node enumeration + per-node flags (all served by get_pcg_inspection_json)
    # =============================================================================================
    @mcp.tool()
    def list_pcg_executed_nodes(ctx, actor: str) -> str:
        """List every (node, stack) pair the component executed in its last generation, with each
        node's inspection flags. Requires inspection to have been enabled BEFORE the generate.

        actor: actor identity (label / internal name / full path) carrying a UPCGComponent.

        Returns {is_inspecting, executed_stacks_generation, distinct_node_count, executed_record_count,
        executed_nodes:[{node_name, node_type, stack, executed, produced_data, inactive_pin_mask,
        gpu_to_cpu_readback, cpu_to_gpu_upload, data_overrides_applied}]}. Empty right after a generate
        usually means the async generation has not finished yet — wait and re-query."""
        try:
            return json.dumps(_get(actor), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_pcg_inspection(ctx, actor: str, node: str = None) -> str:
        """Full inspection readout for a component, optionally filtered to one node by its internal
        name. Same payload as list_pcg_executed_nodes but with an optional node filter.

        actor: actor identity (label / internal name / full path) carrying a UPCGComponent.
        node:  optional node internal name (from list_pcg_executed_nodes) to filter to."""
        try:
            return json.dumps(_get(actor, node), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def was_pcg_node_executed(ctx, actor: str, node: str) -> str:
        """Whether a task for the given node executed in the last generation (any stack).

        actor: actor identity carrying a UPCGComponent.  node: node internal name.
        Returns {node, executed (any-stack), per_stack:[{stack, executed}]}."""
        try:
            res = _get(actor, node)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            recs = _records_for(res, node)
            return json.dumps({"status": "success", "node": node,
                               "executed": any(r.get("executed") for r in recs),
                               "record_count": len(recs),
                               "per_stack": [{"stack": r.get("stack"), "executed": r.get("executed")} for r in recs]},
                              indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def has_pcg_node_produced_data(ctx, actor: str, node: str) -> str:
        """Whether the given node produced one or more data items in the last generation (any stack).

        actor: actor identity carrying a UPCGComponent.  node: node internal name.
        Returns {node, produced_data (any-stack), per_stack:[{stack, produced_data}]}."""
        try:
            res = _get(actor, node)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            recs = _records_for(res, node)
            return json.dumps({"status": "success", "node": node,
                               "produced_data": any(r.get("produced_data") for r in recs),
                               "record_count": len(recs),
                               "per_stack": [{"stack": r.get("stack"), "produced_data": r.get("produced_data")} for r in recs]},
                              indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_pcg_node_inactive_pin_mask(ctx, actor: str, node: str) -> str:
        """The bitmask of output pins deactivated (culled by a dynamic branch) for the given node in
        the last generation, per stack.

        actor: actor identity carrying a UPCGComponent.  node: node internal name.
        Returns {node, per_stack:[{stack, inactive_pin_mask}]}."""
        try:
            res = _get(actor, node)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            recs = _records_for(res, node)
            return json.dumps({"status": "success", "node": node,
                               "record_count": len(recs),
                               "per_stack": [{"stack": r.get("stack"), "inactive_pin_mask": r.get("inactive_pin_mask")} for r in recs]},
                              indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def did_pcg_node_trigger_gpu_transfer(ctx, actor: str, node: str) -> str:
        """Whether the given node triggered a GPU<->CPU data transfer (readback or upload) in the last
        generation, per stack. Relevant for GPU (compute) PCG nodes.

        actor: actor identity carrying a UPCGComponent.  node: node internal name.
        Returns {node, gpu_to_cpu_readback (any), cpu_to_gpu_upload (any), per_stack:[...]}."""
        try:
            res = _get(actor, node)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            recs = _records_for(res, node)
            return json.dumps({"status": "success", "node": node,
                               "gpu_to_cpu_readback": any(r.get("gpu_to_cpu_readback") for r in recs),
                               "cpu_to_gpu_upload": any(r.get("cpu_to_gpu_upload") for r in recs),
                               "record_count": len(recs),
                               "per_stack": [{"stack": r.get("stack"),
                                              "gpu_to_cpu_readback": r.get("gpu_to_cpu_readback"),
                                              "cpu_to_gpu_upload": r.get("cpu_to_gpu_upload")} for r in recs]},
                              indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pcg_node_applied_data_overrides(ctx, actor: str, node: str) -> str:
        """Whether the given node had data (parameter) overrides applied in the last generation, per stack.

        actor: actor identity carrying a UPCGComponent.  node: node internal name.
        Returns {node, data_overrides_applied (any), per_stack:[{stack, data_overrides_applied}]}."""
        try:
            res = _get(actor, node)
            if isinstance(res, dict) and res.get("status") == "error":
                return json.dumps(res, indent=2)
            recs = _records_for(res, node)
            return json.dumps({"status": "success", "node": node,
                               "data_overrides_applied": any(r.get("data_overrides_applied") for r in recs),
                               "record_count": len(recs),
                               "per_stack": [{"stack": r.get("stack"), "data_overrides_applied": r.get("data_overrides_applied")} for r in recs]},
                              indent=2)
        except Exception as e:
            return f"Error: {e}"

    # =============================================================================================
    # Node output inspection + clear
    # =============================================================================================
    @mcp.tool()
    def inspect_pcg_node_output(ctx, actor: str, node: str) -> str:
        """Summarize the data a node produced (the inspected FPCGDataCollection) in the last generation.

        actor: actor identity carrying a UPCGComponent.  node: node internal name (from
               list_pcg_executed_nodes).

        Returns {node_name, node_type, stack, inspected, tagged_data_count,
        tagged_data:[{data_class, pin, tags, num_points?}]}. Requires inspection to have been enabled
        before the generate; errors if the node is not among the executed nodes."""
        try:
            return json.dumps(_exec(_INSPECT_BODY, {"actor": actor, "node": node}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def clear_pcg_inspection(ctx, actor: str) -> str:
        """Clear a component's cached inspection data and per-node execution records. Idempotent,
        NOT ledgered.

        actor: actor identity (label / internal name / full path) carrying a UPCGComponent.
        Returns {actor, cleared}."""
        try:
            return json.dumps(_exec(_CLEAR_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"
