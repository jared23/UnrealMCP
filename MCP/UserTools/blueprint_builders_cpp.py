"""UserTools :: Blueprint GRAPH-BUILDERS + typed-asset CREATORS + type-registry + var-flag setter
(spec: docs/spec/blueprint_builders.md)

DRAFT wiring for the BlueprintGraph/UnrealEd-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_BlueprintMisc.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM
from the gold-standard niagara_runtime_cpp.py / blueprint_graph_cpp.py / niagara_graph_cpp.py.

These COMPOSE the shipped K2 node primitives (add/connect/set-pin from blueprint_graph_cpp.py) into
higher-level, one-transaction tools plus the typed-asset creators, the native type registry, and the
last variable-flag setter:

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * build_blueprint_graph        -- batch build a graph from a spec {nodes, connections, pin_defaults}.
                                      Modes build|merge|sync. Ledger op 'restore_blueprint_graph' carrying
                                      the C++ before-snapshot (a build-spec of the PRIOR graph); inverse =
                                      re-import that snapshot via build mode (document-pattern rollback).
    * arrange_blueprint_graph      -- columnar auto-layout (depth-from-source). Captures prior positions.
                                      Ledger op 'restore_blueprint_node_positions'; inverse = re-arrange
                                      with restore_positions (the SAME handler serves the undo).
    * create_blueprint_interface   -- BPTYPE_Interface UBlueprint (UInterface parent) -- Python's create-
                                      with-parent CANNOT set BPTYPE_Interface. Ledger op 'create_asset'
                                      (generic delete inverse).
    * create_typed_blueprint       -- general typed-BP creator (normal|function_library|macro_library|
                                      const|interface). Ledger op 'create_asset' (generic delete inverse).
    * set_blueprint_variable_flags -- set ALL variable flags incl. the 3 with no BlueprintEditorLibrary
                                      setter (private / blueprint_read_only / config). Captures prior flags.
                                      Ledger op 'set_blueprint_variable_flags'; inverse = restore prior flags.

  READ (no ledger -- non-mutating):
    * get_type_registry            -- TObjectRange walk of UClass/UScriptStruct/UEnum with name/engine
                                      filters + a Max cap. Full-fidelity native-type coverage that Python's
                                      AssetRegistry-only search_types lacks.

DOCUMENT-PATTERN UNDO (build_blueprint_graph): the C++ captures GetBlueprintGraphJson-equivalent state as a
BUILD-SPEC ('before_snapshot') BEFORE mutating, and returns it. This module ledgers it as
'restore_blueprint_graph {asset_path, graph_name, snapshot_json}'. The coordinator folds that op by calling
build_blueprint_graph_json(asset_path, graph_name, snapshot_json, 'build') -- which wipes the current
(post-build) graph and rebuilds the captured PRIOR state. LOSSY: only the reconstructable node kinds
(call_function/variable_get/variable_set/branch/cast/knot/event/custom_event) round-trip; other node classes
are captured as kind 'unsupported' and skipped on rebuild, and links to persistent scaffolding (function
entry/result) restore only when build-mode leaves that scaffolding in place. Faithful for event-graph builds
of the supported kinds; best-effort structural restore otherwise.

Undo: this module registers NO own 'undo' tool (editor_level.py owns the ONE unified 'undo'). Each write
appends its inverse descriptor to the shared per-session ledger for editor_level.undo to fold. The op names
above are the CONTRACT the coordinator wires into the fold ('create_asset' is already handled generically).

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

# --- Output Log auto-capture (copied verbatim from blueprint_graph_cpp.py) ------
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
EAL = unreal.EditorAssetLibrary
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
def _pkgpath(p):
    return p.split(".", 1)[0] if isinstance(p, str) else p
def _save(p):
    try:
        return bool(EAL.save_asset(_pkgpath(p), only_if_is_dirty=False))
    except Exception:
        return False
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ BlueprintMisc handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_BlueprintMisc.cpp to enable it.")}
'''

    # ================================================================== #
    # 1) build_blueprint_graph (write; ledger op 'restore_blueprint_graph' -> re-import snapshot via build).
    # ================================================================== #
    _BUILD_BODY = _HELP + r'''
rl = _mrl("build_blueprint_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("build_blueprint_graph")))
else:
    res = _decode(rl.build_blueprint_graph_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        json.dumps(PARAMS.get("spec", {})), PARAMS.get("mode", "build")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        snap = res.get("before_snapshot", {})
        _ledger().append({"op": "restore_blueprint_graph", "asset_path": PARAMS["blueprint_path"],
            "graph_name": res.get("graph", PARAMS.get("graph_name", "")),
            "snapshot_json": json.dumps(snap)})
        out = dict(res)
        out.pop("before_snapshot", None)
        out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def build_blueprint_graph(ctx, blueprint_path: str, spec: dict, graph_name: str = "",
                              mode: str = "build") -> str:
        """Batch-build a Blueprint graph from a spec (nodes + connections + pin defaults) in ONE transaction.

        blueprint_path: Blueprint asset path (e.g. '/Game/BP_Foo.BP_Foo').
        spec:           {"nodes":[{"id":"n1","kind":"event","event_name":"ReceiveBeginPlay","x":0,"y":0}, ...],
                         "connections":[{"from_id":"n1","from_pin":"then","to_id":"n2","to_pin":"execute"}, ...],
                         "pin_defaults":[{"node_id":"n2","pin":"InString","value":"Hello"}, ...]}
                        node 'kind' is one of call_function|variable_get|variable_set|branch|cast|knot|event|
                        custom_event (same spec fields as add_blueprint_node); 'id' is your own token used to
                        wire connections/pin_defaults (it need not be a real node guid).
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.
        mode:           'build' (wipe every user-deletable node, then create) | 'merge' (add to the existing
                        graph) | 'sync' (keep nodes whose guid matches a spec 'id', remove the rest, create new).

        Composes the shipped K2 node primitives; TryCreateConnection SILENTLY rejects incompatible pins, so
        rejects are RETURNED (rejected_connections) rather than raised, and per-node build failures are
        collected (node_errors) so a partial build still returns. Marks the Blueprint structurally modified
        (NO compile -- call compile_blueprint_graph once after). Appends inverse op 'restore_blueprint_graph'
        carrying a before-snapshot of the PRIOR graph (document-pattern rollback; LOSSY for unsupported node
        kinds). Returns {mode, removed_count, created_count, connected_count, pin_defaults_set, created:[...],
        node_errors:[...], rejected_connections:[...], pin_default_errors:[...], ledger_depth}. BlueprintMisc-
        only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "spec": spec, "graph_name": graph_name, "mode": mode}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 2) arrange_blueprint_graph (write; ledger op 'restore_blueprint_node_positions' -> re-arrange w/ restore).
    # ================================================================== #
    _ARRANGE_BODY = _HELP + r'''
rl = _mrl("arrange_blueprint_graph_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("arrange_blueprint_graph")))
else:
    opts = {}
    if PARAMS.get("column_width"):
        opts["column_width"] = PARAMS["column_width"]
    if PARAMS.get("row_height"):
        opts["row_height"] = PARAMS["row_height"]
    res = _decode(rl.arrange_blueprint_graph_json(PARAMS["blueprint_path"], PARAMS.get("graph_name", ""),
        json.dumps(opts)))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        prior = res.get("prior_positions", {})
        if prior:
            _ledger().append({"op": "restore_blueprint_node_positions", "asset_path": PARAMS["blueprint_path"],
                "graph_name": res.get("graph", PARAMS.get("graph_name", "")), "positions": prior})
        out = dict(res)
        out.pop("prior_positions", None)
        out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def arrange_blueprint_graph(ctx, blueprint_path: str, graph_name: str = "",
                                column_width: int = 0, row_height: int = 0) -> str:
        """Auto-layout a Blueprint graph into columns by exec/data depth from the entry/event nodes.

        blueprint_path: Blueprint asset path.
        graph_name:     '' or 'EventGraph' -> the ubergraph; else a named function graph.
        column_width:   optional column spacing in graph units (0 -> handler default 320).
        row_height:     optional row spacing in graph units (0 -> handler default 180).

        Assigns NodePosX/Y: column = longest input-chain depth from a source (entry/event nodes at column 0,
        downstream nodes stepping right), rows stacking within a column (cycle-guarded, mirrors the shipped
        LayoutNiagaraGraph). Captures prior positions and appends inverse op 'restore_blueprint_node_positions'
        (the coordinator folds it by re-calling this handler with those positions). Returns {node_count,
        moved_count, column_width, row_height, ledger_depth}. BlueprintMisc-only; needs the C++ handler (inert
        until built)."""
        params = {"blueprint_path": blueprint_path, "graph_name": graph_name,
                  "column_width": column_width, "row_height": row_height}
        try:
            return json.dumps(_exec(_ARRANGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 3) create_blueprint_interface (write; ledger op 'create_asset' -> generic delete inverse).
    # ================================================================== #
    _CREATE_IFACE_BODY = _HELP + r'''
rl = _mrl("create_blueprint_interface_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_blueprint_interface")))
else:
    res = _decode(rl.create_blueprint_interface_json(PARAMS["name"], PARAMS["path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        pkg = res.get("package")
        saved = _save(pkg) if pkg else False
        _ledger().append({"op": "create_asset", "asset_path": pkg, "package_path": pkg})
        out = {"status": "success", "blueprint": res.get("blueprint"), "package": pkg,
               "asset_path": res.get("asset_path"), "blueprint_type": res.get("blueprint_type"),
               "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_blueprint_interface(ctx, name: str, path: str) -> str:
        """Create a Blueprint Interface asset (BPTYPE_Interface) -- unreachable from stock Python create-with-parent.

        name: new asset name (e.g. 'BPI_Interactable').
        path: destination content folder (e.g. '/Game/Blueprints/Interfaces').

        Uses FKismetEditorUtilities::CreateBlueprint(UInterface, ..., BPTYPE_Interface, UBlueprint,
        UBlueprintGeneratedClass) -- the ONLY way to author a real Blueprint Interface headless. Saves the
        package and appends inverse op 'create_asset' (generic delete). Returns {blueprint, package, asset_path,
        blueprint_type, saved, ledger_depth}. BlueprintMisc-only; needs the C++ handler (inert until built)."""
        params = {"name": name, "path": path}
        try:
            return json.dumps(_exec(_CREATE_IFACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 4) create_typed_blueprint (write; ledger op 'create_asset' -> generic delete inverse).
    # ================================================================== #
    _CREATE_TYPED_BODY = _HELP + r'''
rl = _mrl("create_typed_blueprint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_typed_blueprint")))
else:
    res = _decode(rl.create_typed_blueprint_json(PARAMS["name"], PARAMS["path"],
        PARAMS.get("parent_class", ""), PARAMS.get("blueprint_type", "normal")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        pkg = res.get("package")
        saved = _save(pkg) if pkg else False
        _ledger().append({"op": "create_asset", "asset_path": pkg, "package_path": pkg})
        out = {"status": "success", "blueprint": res.get("blueprint"), "package": pkg,
               "asset_path": res.get("asset_path"), "parent_class": res.get("parent_class"),
               "blueprint_type": res.get("blueprint_type"), "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_typed_blueprint(ctx, name: str, path: str, parent_class: str = "",
                               blueprint_type: str = "normal") -> str:
        """Create a typed Blueprint asset -- normal / function_library / macro_library / const / interface.

        name:           new asset name (e.g. 'BPL_MathHelpers').
        path:           destination content folder (e.g. '/Game/Blueprints/Libraries').
        parent_class:   parent UClass path/name (e.g. '/Script/Engine.Actor', 'Actor'). EMPTY -> a sensible
                        default for the type (function_library -> BlueprintFunctionLibrary; interface ->
                        UInterface; macro_library/const -> Object; normal -> Actor).
        blueprint_type: 'normal' (default) | 'function_library' | 'macro_library' | 'const' | 'interface'.

        Backs the function-library fallback (Python's create-with-parent cannot set BPTYPE_FunctionLibrary /
        MacroLibrary / Const / Interface). Saves the package and appends inverse op 'create_asset' (generic
        delete). Returns {blueprint, package, asset_path, parent_class, blueprint_type, saved, ledger_depth}.
        BlueprintMisc-only; needs the C++ handler (inert until built)."""
        params = {"name": name, "path": path, "parent_class": parent_class, "blueprint_type": blueprint_type}
        try:
            return json.dumps(_exec(_CREATE_TYPED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 5) get_type_registry (READ; no ledger).
    # ================================================================== #
    _REGISTRY_BODY = _HELP + r'''
rl = _mrl("get_type_registry_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_type_registry")))
else:
    res = _decode(rl.get_type_registry_json(PARAMS.get("kind", "class"), PARAMS.get("query", ""),
        bool(PARAMS.get("include_engine", True)), int(PARAMS.get("max", 500))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_type_registry(ctx, kind: str = "class", query: str = "", include_engine: bool = True,
                          max: int = 500) -> str:
        """Enumerate the LIVE native type registry -- UClass / UScriptStruct / UEnum -- with filters + a cap.

        kind:           'class' (default) | 'struct' | 'enum'.
        query:          optional case-insensitive substring over the type NAME ('' -> all).
        include_engine: True (default) includes engine/plugin native types; False keeps only the game project's
                        own C++ types and asset-defined types (a native type whose module is not the project
                        module is treated as 'engine').
        max:            cap on returned entries (default 500; total_matched + truncated report the full count).

        Walks TObjectRange<UClass/UScriptStruct/UEnum> -- full-fidelity native-type coverage that Python's
        AssetRegistry-only search_types cannot reach (native structs/enums/abstract classes never hit the asset
        registry). Read-only (no ledger). Returns {kind, query, include_engine, max, total_matched, count,
        truncated, types:[{name, path, package[, parent, is_abstract, is_native, is_interface | num_values]}]}.
        BlueprintMisc-only; needs the C++ handler (inert until built)."""
        params = {"kind": kind, "query": query, "include_engine": include_engine, "max": max}
        try:
            return json.dumps(_exec(_REGISTRY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 6) set_blueprint_variable_flags (write; ledger op 'set_blueprint_variable_flags' -> restore prior flags).
    # ================================================================== #
    _SET_FLAGS_BODY = _HELP + r'''
rl = _mrl("set_blueprint_variable_flags_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_variable_flags")))
else:
    res = _decode(rl.set_blueprint_variable_flags_json(PARAMS["blueprint_path"], PARAMS["variable_name"],
        json.dumps(PARAMS.get("flags", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_blueprint_variable_flags", "asset_path": PARAMS["blueprint_path"],
            "variable_name": res.get("var", PARAMS["variable_name"]),
            "flags": res.get("prior_flags", {})})
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_variable_flags(ctx, blueprint_path: str, variable_name: str, flags: dict) -> str:
        """Set Blueprint member-variable flags -- INCLUDING private / blueprint_read_only / config (no BEL setter).

        blueprint_path: Blueprint asset path.
        variable_name:  the member variable's name.
        flags:          dict of the flags to change (all keys OPTIONAL; only provided keys are applied):
                          {"instance_editable": bool, "blueprint_read_only": bool, "config": bool,
                           "expose_on_spawn": bool, "private": bool, "expose_to_cinematics": bool}

        Toggles FBPVariableDescription::PropertyFlags bits (CPF_DisableEditOnInstance / CPF_BlueprintReadOnly /
        CPF_Config / CPF_ExposeOnSpawn / CPF_Interp / CPF_Edit) plus the private/expose_on_spawn METADATA the
        editor uses, then MarkBlueprintAsStructurallyModified. Companion to get_blueprint_variable_flags.
        Captures prior flags and appends inverse op 'set_blueprint_variable_flags' (restore prior). Returns
        {blueprint, var, applied:[...], prior_flags:{...}, new_flags:{...}, ledger_depth}. BlueprintMisc-only;
        needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "variable_name": variable_name, "flags": flags}
        try:
            return json.dumps(_exec(_SET_FLAGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
