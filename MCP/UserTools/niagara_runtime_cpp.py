"""UserTools :: Niagara / VFX module-stack + graph READS (+ module enable WRITE)  (spec: docs/spec/niagara.md)

DRAFT wiring for the NiagaraEditor-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Niagara2.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied
VERBATIM from the gold-standard niagara_write2.py / niagara_write3.py.

Fills the spec features that stock Python cannot reach (see niagara_write3.py header: get_all_emitters
returns only a lightweight NiagaraMinimalEmitterInfo; the emitter script graph + module stack are NOT
python-reachable -- they live in NiagaraEditor C++):

  READS (no ledger -- non-mutating):
    * get_niagara_modules        -- MCPReflectionLibrary.get_niagara_modules(system_path, emitter,
                                    script_usage, include_inputs): modules of an emitter's stack(s) in
                                    STACK ORDER (function name / node guid / script / enabled / valid /
                                    deprecated [+ inputs if include_inputs]).
    * get_niagara_module_inputs  -- inputs (name/type/hidden) of one module, via the exported
                                    GetStackFunctionInputs; optional input_filter substring.
    * get_niagara_graph_nodes    -- every node of an emitter's shared graph (emitter="" -> system graph);
                                    verbosity basic|detailed; optional type_filter.
    * get_niagara_node_info      -- full detail for one node by GUID incl. every pin + its linked endpoints.
    * trace_niagara_connection   -- BFS wire walk from a node/pin, upstream|downstream, bounded by max_depth.
    * validate_niagara_stack     -- STRUCTURAL stack validation (invalid script / deprecated / disabled
                                    module), min_severity-filtered. (Compile diagnostics: validate_niagara_graph.)

  WRITE (ledger -- reversible):
    * set_niagara_module_enabled -- enable/disable a module via the exported SetModuleIsEnabled. Captures
                                    prev_enabled; inverse folds into editor_level.undo.
    * reorder_niagara_module     -- move a module to a new stack index via FNiagaraStackGraphUtilities::
                                    MoveModule (the engine's own drag-drop reorder; needs a NIAGARAEDITOR_API
                                    export patch). Captures prev_index; inverse = reorder back to prev_index.
    * set_niagara_dynamic_input  -- set a module input to a DYNAMIC INPUT (a function-call feeding the input
                                    pin) via the exported GetOrCreateStackFunctionInputOverridePin +
                                    SetDynamicInputForFunctionInput. Fresh inputs only (see below).
    * set_niagara_stack_value    -- general stack-value setter: mode 'linked' binds the input to a parameter
                                    (SetLinkedParameterValueForFunctionInput); mode 'local' sets the override
                                    pin default literal. Captures had_override/prev_value.
    * set_niagara_curve          -- set a float-Curve-DI input's keys (SetDataInterfaceValueForFunctionInput +
                                    the public FRichCurve). Fresh curve-DI inputs only.

The three input setters (dynamic_input / stack_value-linked / curve) operate ONLY on inputs with NO existing
LINKED override (fresh inputs, or -- for local/curve -- inputs whose current value is a plain pin default):
clearing an existing linked override / editing a curve in place needs the non-exported
RemoveNodesForStackFunctionInputOverridePin / UNiagaraNodeInput::GetDataInterface, so those cases return an
honest guard error rather than a checkf crash. All are drafted in MCPReflection_Niagara3.cpp and are inert
until the plugin DLL is rebuilt (each hasattr-guards its future handler).

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The write's
inverse is appended to the shared per-session ledger for editor_level.undo to fold.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a
snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/
user_code/code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from niagara_write3.py) ---------
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
    return {"status": "error", "error": (fn + " requires the C++ NiagaraEditor handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Niagara2.cpp to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _MODULES_BODY = _HELP + r'''
rl = _mrl("get_niagara_modules")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_niagara_modules")))
else:
    res = _decode(rl.get_niagara_modules(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), bool(PARAMS.get("include_inputs", False))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_modules(ctx, system_path: str, emitter_name: str, script_usage: str = "all",
                            include_inputs: bool = False) -> str:
        """List an emitter's stack modules (function-call nodes) in STACK ORDER.

        system_path:    NiagaraSystem asset path (e.g. '/Game/VFX/NS_Foo.NS_Foo').
        emitter_name:   emitter handle name (required).
        script_usage:   'all' (default) or one of particle_spawn|particle_update|emitter_spawn|emitter_update.
        include_inputs: also emit each module's stack inputs (name/type/hidden). Heavier.

        Returns {system, emitter, module_count, usages:[{usage, has_output_node, modules:[{index,
        function_name, node_guid, script_path, script_name, enabled, enabled_state, valid_script,
        deprecated [, inputs]}]}]}. NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name,
                  "script_usage": script_usage, "include_inputs": include_inputs}
        try:
            return json.dumps(_exec(_MODULES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _MODULE_INPUTS_BODY = _HELP + r'''
rl = _mrl("get_niagara_module_inputs")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_niagara_module_inputs")))
else:
    res = _decode(rl.get_niagara_module_inputs(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], PARAMS.get("input_filter", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_module_inputs(ctx, system_path: str, emitter_name: str, module_name: str,
                                  script_usage: str = "all", input_filter: str = "") -> str:
        """List one module's stack inputs (name/type/hidden), via the exported GetStackFunctionInputs.

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name.
        module_name:  module's function name (or its called-script asset name).
        script_usage: 'all' (default, searches every usage) or a single usage.
        input_filter: optional case-insensitive substring narrowing the reported input names.

        Returns {system, emitter, module, usage, module_index, valid_script, input_count,
        inputs:[{name, type, hidden}]}. NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "script_usage": script_usage, "input_filter": input_filter}
        try:
            return json.dumps(_exec(_MODULE_INPUTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GRAPH_NODES_BODY = _HELP + r'''
rl = _mrl("get_niagara_graph_nodes")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_niagara_graph_nodes")))
else:
    res = _decode(rl.get_niagara_graph_nodes(PARAMS["system_path"], PARAMS.get("emitter_name", ""),
        PARAMS.get("verbosity", "basic"), PARAMS.get("type_filter", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_graph_nodes(ctx, system_path: str, emitter_name: str = "", verbosity: str = "basic",
                                type_filter: str = "") -> str:
        """Enumerate the nodes of an emitter's shared script graph.

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name; EMPTY -> the SYSTEM graph (system spawn/update).
        verbosity:    'basic' (guid/class/type[, function]) | 'detailed' (+title/pos/enabled/pin counts).
        type_filter:  optional case-insensitive substring over the node class / short type.

        Returns {system, scope, node_count, nodes:[{node_guid, node_class, node_type[, function_name,
        script_path][, title, pos_x, pos_y, enabled, num_input_pins, num_output_pins]}]}.
        NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name,
                  "verbosity": verbosity, "type_filter": type_filter}
        try:
            return json.dumps(_exec(_GRAPH_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _NODE_INFO_BODY = _HELP + r'''
rl = _mrl("get_niagara_node_info")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_niagara_node_info")))
else:
    res = _decode(rl.get_niagara_node_info(PARAMS["system_path"], PARAMS.get("emitter_name", ""),
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
    def get_niagara_node_info(ctx, system_path: str, node_guid: str, emitter_name: str = "") -> str:
        """Full detail for one graph node addressed by GUID.

        system_path:  NiagaraSystem asset path.
        node_guid:    the node's persistent GUID (from get_niagara_graph_nodes / get_niagara_modules).
        emitter_name: emitter handle name; EMPTY -> the SYSTEM graph.

        Returns {system, scope, node_guid, node_class, node_type, title, pos_x, pos_y, enabled,
        enabled_state[, function_name, script_path, valid_script, deprecated][, output_usage],
        pins:[{name, direction, category, is_parameter_map, linked_count[, default_value],
        linked_to:[{node_guid, node_type, pin}]}]}. NiagaraEditor-only; needs the C++ handler."""
        params = {"system_path": system_path, "node_guid": node_guid, "emitter_name": emitter_name}
        try:
            return json.dumps(_exec(_NODE_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _TRACE_BODY = _HELP + r'''
rl = _mrl("trace_niagara_connection")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("trace_niagara_connection")))
else:
    res = _decode(rl.trace_niagara_connection(PARAMS["system_path"], PARAMS.get("emitter_name", ""),
        PARAMS["node_guid"], PARAMS.get("pin_name", ""), PARAMS.get("direction", "upstream"),
        int(PARAMS.get("max_depth", 8))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def trace_niagara_connection(ctx, system_path: str, node_guid: str, emitter_name: str = "",
                                 pin_name: str = "", direction: str = "upstream", max_depth: int = 8) -> str:
        """Trace wire connections from a graph node, breadth-first.

        system_path:  NiagaraSystem asset path.
        node_guid:    start node's GUID.
        emitter_name: emitter handle name; EMPTY -> the SYSTEM graph.
        pin_name:     optional -- restrict the FIRST hop to this pin name only.
        direction:    'upstream' (default, follow input pins toward producers) | 'downstream'.
        max_depth:    hop levels to walk (1..64; default 8).

        Returns {system, scope, start_node_guid, direction, max_depth, hop_count,
        hops:[{depth, from_node_guid, from_pin, to_node_guid, to_node_type, to_pin}]}.
        NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "node_guid": node_guid, "emitter_name": emitter_name,
                  "pin_name": pin_name, "direction": direction, "max_depth": max_depth}
        try:
            return json.dumps(_exec(_TRACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _VALIDATE_BODY = _HELP + r'''
rl = _mrl("validate_niagara_stack")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("validate_niagara_stack")))
else:
    res = _decode(rl.validate_niagara_stack(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("min_severity", "all")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def validate_niagara_stack(ctx, system_path: str, emitter_name: str, min_severity: str = "all") -> str:
        """Structurally validate an emitter's stacks (module script validity / deprecation / enabled state).

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name.
        min_severity: 'all' (default) | 'info' | 'warning' | 'error' -- filters the returned issues (counts
                      are always full).

        Returns {system, emitter, module_count, error_count, warning_count, info_count, valid, min_severity,
        note, issues:[{severity, usage, module, message}]}. STRUCTURAL only -- compile diagnostics come from
        validate_niagara_graph / get_niagara_system_errors; the full view-model stack-issue set is not
        reachable headless. NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "min_severity": min_severity}
        try:
            return json.dumps(_exec(_VALIDATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # LIVE PARTICLE STATS (READ, no ledger). Needs a live PIE world -- reads a
    # spawned UNiagaraComponent on an actor via the C++ #NN handler
    # (MCPReflection_Niagara5.cpp: GetNiagaraParticleStatsJson). Sent LEAN (raw
    # code, no _wrap Output-Log footprint -- mirrors bt_write2's runtime path).
    # hasattr-guarded on get_niagara_particle_stats_json -> honest refusal until
    # the plugin DLL with MCPReflection_Niagara5.cpp lands.
    # ================================================================== #
    def _nia_lean_exec(body, params):
        p = dict(params or {})
        b64 = base64.b64encode(json.dumps(p).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        resp = send_command("execute_python", {"code": header + body})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    # Resolve the target Niagara actor in the live PIE world (by name/label, or the
    # first actor carrying a UNiagaraComponent) then leave it in _t.
    _NIA_RT_HEAD = (
        "import unreal, json\n"
        "ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
        "gw = ues.get_game_world() if les.is_in_play_in_editor() else None\n"
        "m = getattr(unreal, 'MCPReflectionLibrary', None)\n"
        "if gw is None:\n"
        "    print('@@UMCP@@' + json.dumps({'status':'error','message':'not in PIE - play_in_editor, poll get_pie_status until in_pie, then retry'}))\n"
        "elif m is None or not hasattr(m, 'get_niagara_particle_stats_json'):\n"
        "    print('@@UMCP@@' + json.dumps({'status':'error','message':'get_niagara_particle_stats requires the C++ NiagaraEditor handler (rebuild the UnrealMCP plugin DLL with MCPReflection_Niagara5.cpp to enable it)'}))\n"
        "else:\n"
        "    _name = PARAMS.get('actor') or ''\n"
        "    _t = None\n"
        "    _acts = unreal.GameplayStatics.get_all_actors_of_class(gw, unreal.Actor)\n"
        "    if _name:\n"
        "        for _a in _acts:\n"
        "            try:\n"
        "                if _a.get_name() == _name or (_name.lower() in _a.get_name().lower()) or _a.get_actor_label() == _name:\n"
        "                    _t = _a; break\n"
        "            except Exception:\n"
        "                pass\n"
        "    else:\n"
        "        for _a in _acts:\n"
        "            try:\n"
        "                if _a.get_components_by_class(unreal.NiagaraComponent):\n"
        "                    _t = _a; break\n"
        "            except Exception:\n"
        "                pass\n"
        "    if _t is None:\n"
        "        print('@@UMCP@@' + json.dumps({'status':'error','message':'no target actor with a NiagaraComponent found (actor=%s)' % (_name or '(first with a NiagaraComponent)')}))\n"
        "    else:\n")

    @mcp.tool()
    def get_niagara_particle_stats(ctx, actor: str = None, component: str = "") -> str:
        """Read LIVE per-emitter particle counts + execution state of a spawned Niagara effect (C++ #NN handler).

        REQUIRES PIE -- play_in_editor, poll get_pie_status until in_pie, then call this. Reads a live
        UNiagaraComponent on an actor in the running game world (no mutation; no ledger).

        actor:     substring/name/label match of the actor to read (empty = the first actor that carries a
                   UNiagaraComponent).
        component: optional -- restrict to a single component by exact/substring name (empty = ALL Niagara
                   components on the actor).

        Returns {status, actor, component_count, grand_total_particles, components:[{component, system_asset,
        system_path, component_exec_state, is_active, is_complete, is_paused, has_instance[, system_exec_state,
        age, tick_count, emitter_count, total_particles, emitters:[{index, emitter, particle_count,
        total_spawned, exec_state, sim_target, is_active, is_complete}]]}]}. Runtime-Niagara only; needs the
        C++ handler (inert until the plugin DLL is rebuilt with MCPReflection_Niagara5.cpp)."""
        try:
            body = _NIA_RT_HEAD + "        print('@@UMCP@@' + json.dumps(json.loads(m.get_niagara_particle_stats_json(_t, PARAMS.get('component') or ''))))\n"
            return json.dumps(_nia_lean_exec(body, {"actor": actor, "component": component}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITE (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    _SET_ENABLED_BODY = _HELP + r'''
rl = _mrl("set_niagara_module_enabled")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_niagara_module_enabled")))
else:
    res = _decode(rl.set_niagara_module_enabled(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], bool(PARAMS["enabled"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_module_enabled", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "script_usage": res.get("usage", PARAMS.get("script_usage", "")),
            "module": res.get("module", PARAMS["module_name"]), "prev_enabled": bool(res.get("prev_enabled"))})
        out = {"status": "success", "module": res.get("module"), "usage": res.get("usage"),
               "prev_enabled": bool(res.get("prev_enabled")), "enabled": bool(res.get("enabled")),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_niagara_module_enabled(ctx, system_path: str, emitter_name: str, module_name: str,
                                   enabled: bool, script_usage: str = "all") -> str:
        """Enable or disable a module in an emitter stack (recompiles + marks the package dirty).

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name.
        module_name:  module's function name (or its called-script asset name).
        enabled:      True to enable, False to disable.
        script_usage: 'all' (default, searches every usage) or a single usage.

        Uses the exported FNiagaraStackGraphUtilities::SetModuleIsEnabled (toggles every associated node +
        flags recompile). Captures prev_enabled and appends the inverse to the per-session ledger for
        editor_level.undo. Returns {module, usage, prev_enabled, enabled, ledger_depth}.
        NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "enabled": enabled, "script_usage": script_usage}
        try:
            return json.dumps(_exec(_SET_ENABLED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITE (ledger -- reversible). Move a module to a new stack index via the SAFE, non-crashing
    # ReorderNiagaraModuleV2 (MCPReflection_Niagara5.cpp). hasattr-guarded on reorder_niagara_module_v2 ->
    # inert until the DLL with MCPReflection_Niagara5.cpp lands.
    # ================================================================== #

    # RE-ENABLED 2026-08-18 against the CORRECTED handler: the prior C++ ReorderNiagaraModule passed the user's
    # new_index straight to FNiagaraStackGraphUtilities::MoveModule and only guarded new_index==prev_index. That
    # (a) hit MoveModule's degenerate pre-removal TargetModuleIndex in {S, S+1} -> a param-map self-loop cycle ->
    # NiagaraGraph::BuildTraversalHelper infinite recursion / editor stack-overflow crash on the next compile, and
    # (b) was off-by-one for rightward moves. ReorderNiagaraModuleV2 maps the desired FINAL index to the correct
    # pre-removal TargetModuleIndex (F<S -> F ; F>S -> F+1 ; F==S -> no-op), which is provably outside {S, S+1},
    # keeping the graph acyclic. Same already-export-patched MoveModule; no new engine patch. See
    # [[unrealmcp-deferred-backlog]].
    _REORDER_BODY = _HELP + r'''
rl = _mrl("reorder_niagara_module_v2")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("reorder_niagara_module_v2")))
else:
    res = _decode(rl.reorder_niagara_module_v2(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], int(PARAMS["new_index"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("moved"):
            _ledger().append({"op": "reorder_niagara_module", "asset_path": PARAMS["system_path"],
                "emitter": PARAMS["emitter_name"], "script_usage": res.get("usage", PARAMS.get("script_usage", "")),
                "module": res.get("module", PARAMS["module_name"]), "prev_index": res.get("prev_index")})
        out = {"status": "success", "module": res.get("module"), "usage": res.get("usage"),
               "prev_index": res.get("prev_index"), "new_index": res.get("new_index"),
               "module_count": res.get("module_count"), "moved": bool(res.get("moved")),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def reorder_niagara_module(ctx, system_path: str, emitter_name: str, module_name: str,
                               new_index: int, script_usage: str = "all") -> str:
        """Move a module to a new position (0-based FINAL new_index) in its emitter stack.

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name.
        module_name:  module's function name (or its called-script asset name).
        new_index:    0-based desired FINAL index within the usage stack (clamped to the stack size).
        script_usage: 'all' (default, searches every usage) or a single usage.

        Uses the engine's own drag-drop reorder (FNiagaraStackGraphUtilities::MoveModule) with the CORRECTED
        pre-removal index mapping (ReorderNiagaraModuleV2) so the param-map stays acyclic -- the prior in-place
        variant corrupted it into a cycle and crashed the editor on the next compile. Captures prev_index and
        appends the inverse to the per-session ledger for editor_level.undo. Returns {module, usage, prev_index,
        new_index, module_count, moved, ledger_depth}. NiagaraEditor-only; needs the C++ handler (inert until
        the plugin DLL is rebuilt with MCPReflection_Niagara5.cpp)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "new_index": new_index, "script_usage": script_usage}
        try:
            return json.dumps(_exec(_REORDER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_DYNINPUT_BODY = _HELP + r'''
rl = _mrl("set_niagara_dynamic_input")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_niagara_dynamic_input")))
else:
    res = _decode(rl.set_niagara_dynamic_input(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], PARAMS["input_name"],
        PARAMS["dynamic_input_script_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_dynamic_input", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "script_usage": res.get("usage", PARAMS.get("script_usage", "")),
            "module": res.get("module", PARAMS["module_name"]), "input": res.get("input", PARAMS["input_name"]),
            "had_override": bool(res.get("had_override")),
            "dynamic_input_node_guid": res.get("dynamic_input_node_guid")})
        out = {"status": "success", "module": res.get("module"), "usage": res.get("usage"),
               "input": res.get("input"), "dynamic_input": res.get("dynamic_input"),
               "dynamic_input_node_guid": res.get("dynamic_input_node_guid"),
               "had_override": bool(res.get("had_override")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_niagara_dynamic_input(ctx, system_path: str, emitter_name: str, module_name: str,
                                  input_name: str, dynamic_input_script_path: str,
                                  script_usage: str = "all") -> str:
        """Set a module input to a DYNAMIC INPUT (a function-call script feeding the input pin).

        system_path:               NiagaraSystem asset path.
        emitter_name:              emitter handle name.
        module_name:               module's function name (or its called-script asset name).
        input_name:                input name ('Foo' or 'Module.Foo', case-insensitive).
        dynamic_input_script_path: UNiagaraScript asset path of the dynamic input (e.g. a 'FloatFromCurve').
        script_usage:              'all' (default, searches every usage) or a single usage.

        Via the exported GetOrCreateStackFunctionInputOverridePin + SetDynamicInputForFunctionInput. FRESH
        inputs only -- an input that already carries a linked value/dynamic input returns a guard error (in-
        editor reset needed first). Captures had_override + the created dynamic-input node guid in the ledger.
        Returns {module, usage, input, dynamic_input, dynamic_input_node_guid, had_override, ledger_depth}.
        NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "input_name": input_name, "dynamic_input_script_path": dynamic_input_script_path,
                  "script_usage": script_usage}
        try:
            return json.dumps(_exec(_SET_DYNINPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_STACKVAL_BODY = _HELP + r'''
rl = _mrl("set_niagara_stack_value")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_niagara_stack_value")))
else:
    res = _decode(rl.set_niagara_stack_value(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], PARAMS["input_name"],
        PARAMS.get("mode", "local"), PARAMS.get("value_or_parameter", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_stack_value", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "script_usage": res.get("usage", PARAMS.get("script_usage", "")),
            "module": res.get("module", PARAMS["module_name"]), "input": res.get("input", PARAMS["input_name"]),
            "mode": res.get("mode"), "prev_value": res.get("prev_value"),
            "had_override": bool(res.get("had_override"))})
        out = {"status": "success", "module": res.get("module"), "usage": res.get("usage"),
               "input": res.get("input"), "mode": res.get("mode"), "value": res.get("value"),
               "prev_value": res.get("prev_value"), "had_override": bool(res.get("had_override")),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_niagara_stack_value(ctx, system_path: str, emitter_name: str, module_name: str,
                                input_name: str, mode: str = "local", value_or_parameter: str = "",
                                script_usage: str = "all") -> str:
        """Set a module input's stack value -- a LOCAL literal or a LINKED parameter.

        system_path:        NiagaraSystem asset path.
        emitter_name:       emitter handle name.
        module_name:        module's function name (or its called-script asset name).
        input_name:         input name ('Foo' or 'Module.Foo', case-insensitive).
        mode:               'local' (default) sets the override-pin default literal; 'linked' binds the input
                            to a parameter.
        value_or_parameter: for 'local', a Niagara pin-default literal (e.g. '1.5', '(X=1.000000,Y=0.000000,
                            Z=0.000000)'); for 'linked', a parameter name (e.g. 'Particles.Velocity',
                            'User.MyFloat', 'Engine.DeltaTime').
        script_usage:       'all' (default, searches every usage) or a single usage.

        'linked' uses the exported SetLinkedParameterValueForFunctionInput; 'local' writes the override pin
        default string (best-effort, no type coercion). FRESH inputs only for both -- an input already carrying
        a linked value/dynamic input returns a guard error. Captures prev_value + had_override in the ledger.
        Returns {module, usage, input, mode, value, prev_value, had_override, ledger_depth}. NiagaraEditor-only;
        needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "input_name": input_name, "mode": mode, "value_or_parameter": value_or_parameter,
                  "script_usage": script_usage}
        try:
            return json.dumps(_exec(_SET_STACKVAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_CURVE_BODY = _HELP + r'''
rl = _mrl("set_niagara_curve")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_niagara_curve")))
else:
    res = _decode(rl.set_niagara_curve(PARAMS["system_path"], PARAMS["emitter_name"],
        PARAMS.get("script_usage", ""), PARAMS["module_name"], PARAMS["input_name"],
        json.dumps(PARAMS.get("keys", []))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_curve", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "script_usage": res.get("usage", PARAMS.get("script_usage", "")),
            "module": res.get("module", PARAMS["module_name"]), "input": res.get("input", PARAMS["input_name"]),
            "had_override": bool(res.get("had_override"))})
        out = {"status": "success", "module": res.get("module"), "usage": res.get("usage"),
               "input": res.get("input"), "curve_type": res.get("curve_type"),
               "keys_written": res.get("keys_written"), "had_override": bool(res.get("had_override")),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_niagara_curve(ctx, system_path: str, emitter_name: str, module_name: str, input_name: str,
                          keys: list, script_usage: str = "all") -> str:
        """Set a module input's float-Curve keys (input type must be 'Curve for Floats').

        system_path:  NiagaraSystem asset path.
        emitter_name: emitter handle name.
        module_name:  module's function name (or its called-script asset name).
        input_name:   input name ('Foo' or 'Module.Foo', case-insensitive); its type MUST be a float Curve
                      data interface (UNiagaraDataInterfaceCurve).
        keys:         list of {time, value[, interp]} dicts; interp = cubic (default) | linear | constant.
        script_usage: 'all' (default, searches every usage) or a single usage.

        Creates the float Curve DI on the input's override pin (exported SetDataInterfaceValueForFunctionInput)
        and populates its FRichCurve. FRESH curve-DI inputs only -- an input that already has an override
        returns a guard error (in-editor reset needed for in-place edits); non-float-curve inputs are refused.
        Returns {module, usage, input, curve_type, keys_written, had_override, ledger_depth}. NiagaraEditor-
        only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "module_name": module_name,
                  "input_name": input_name, "keys": keys, "script_usage": script_usage}
        try:
            return json.dumps(_exec(_SET_CURVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
