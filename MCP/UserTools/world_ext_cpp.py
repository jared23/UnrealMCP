"""UserTools :: WORLD C++-A infra ext -- editor modes + world-partition + nav build status (C++-backed)

Wires the WORLD "C++-A infra ext" C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_WorldExt.cpp. Every tool here reaches an object
that has NO BlueprintCallable / reflected Python surface in UE 5.8, so stock Python cannot touch it:

  * the LIVE UWorldPartition object is not exposed to editor Python -- the only handle is the C++ path
    GEditor->GetEditorWorldContext().World()->GetWorldPartition();
  * editor modes go through the non-UFUNCTION global FEditorModeTools (GLevelEditorModeTools());
  * UNavigationSystemV1::IsNavigationBuildInProgress()/GetNumRemainingBuildTasks() are not UFUNCTIONs.

  READS (no ledger -- non-mutating):
    * list_editor_modes           -- registered editor modes (id/name/active/visible/default/priority) via the
                                     UAssetEditorSubsystem registry + GLevelEditorModeTools(). Works on ANY level.
    * get_world_partition_settings-- live UWorldPartition streaming settings + runtime hash + (spatial hash) the
                                     EDITING grids array. Clean error if the map is not partitioned.
    * navigation_build_status     -- UNavigationSystemV1 build progress (building / remaining_tasks /
                                     running_tasks / supported_agents) for the editor world.
    * build_world_partition       -- HONEST build report (see below). Performs NO in-editor mutation, so no ledger.

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * set_editor_mode             -- activate an editor mode (empty/"default" restores the default mode set).
                                     Captures prev_mode. Ledger op "set_editor_mode".
    * set_world_partition_settings-- set editable UWorldPartition props (bEnableStreaming via the side-effecting
                                     SetEnableStreaming; others via reflection + PostEditChange). Captures a prev
                                     {prop: value} object. Ledger op "set_world_partition_settings".
    * set_runtime_grid            -- edit one spatial-hash EDITING grid (name/cell_size/loading_range/priority/
                                     block_on_slow_streaming/origin/debug_color). Captures a prev {field: value}
                                     object. Ledger op "set_runtime_grid".

build_world_partition is HONEST: a real WP build (streaming/HLOD/minimap/navmesh) is done by the
WorldPartitionBuilderCommandlet as a SEPARATE -run= process; there is no safe, persisting in-editor build
entry point (UWorldPartition::GenerateStreaming is an in-memory cook/PIE step that neither persists nor is safe
to fire under a live editor). The tool therefore returns the exact commandlet command line + readiness facts and
does NOT fake success (in_editor_build_performed is always false).

Each tool hasattr-guards its future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with MCPReflection_WorldExt.cpp -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from
the gold-standard niagara_runtime_cpp.py / projectsettings_cpp.py.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write appends
ONE ledger op whose inverse the coordinator folds into editor_level.undo:
    set_editor_mode              {prev_mode, mode}          -> set_editor_mode(prev_mode)  (empty prev restores default)
    set_world_partition_settings {prev:{prop:value}}        -> set_world_partition_settings(prev)
    set_runtime_grid             {grid_name, prev:{f:value}}-> set_runtime_grid(grid_name, **prev)

RISK (flagged): set_world_partition_settings / set_runtime_grid mutate the LIVE UWorldPartition of the open map
and MarkPackageDirty. Per [[unrealmcp-never-mutate-real-level]], run any live test ONLY on a throwaway WP-enabled
SCRATCH map, never the real level. set_editor_mode is non-destructive (viewport tool state) but still ledgered.

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

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py / projectsettings_cpp.py) ---
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
    return {"status": "error", "error": (fn + " requires the C++ handler in MCPReflection_WorldExt.cpp "
            "(rebuild the UnrealMCP plugin DLL to enable it).")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _LIST_MODES_BODY = _HELP + r'''
rl = _mrl("list_editor_modes_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_editor_modes")))
else:
    res = _decode(rl.list_editor_modes_json())
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_editor_modes(ctx) -> str:
        """List the registered LEVEL-EDITOR modes (Select / Landscape / Foliage / Mesh Paint / ...). Read-only.

        Enumerates the mode registry via UAssetEditorSubsystem::GetEditorModeInfoOrderedByPriority() and reads
        the live active state from the global FEditorModeTools (GLevelEditorModeTools()) -- neither is reachable
        from stock Python, hence the C++ handler. Works on ANY level (no World Partition required).

        Returns {mode_count, modes:[{id, name, is_active, is_visible, is_default_mode, priority}],
        active_mode_ids:[...], is_default_mode_active}. Pair a returned 'id' with set_editor_mode to activate it.
        Needs the C++ handler (inert until the plugin DLL is rebuilt with MCPReflection_WorldExt.cpp)."""
        try:
            return json.dumps(_exec(_LIST_MODES_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_WP_BODY = _HELP + r'''
rl = _mrl("get_world_partition_settings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_world_partition_settings")))
else:
    res = _decode(rl.get_world_partition_settings_json())
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def get_world_partition_settings(ctx) -> str:
        """Read the LIVE UWorldPartition settings of the open map (streaming + runtime hash + editing grids). Read-only.

        Resolves the editor world's UWorldPartition via the C++-only path Python lacks
        (GEditor->GetEditorWorldContext().World()->GetWorldPartition()). Returns a clean error
        'world is not partitioned' if the open map has no World Partition.

        Returns {world_partition_path, b_enable_streaming, supports_streaming, is_streaming_enabled,
        is_streaming_enabled_in_editor, is_initialized, is_main_world_partition, editor_name,
        server_streaming_mode, server_streaming_out_mode, data_layers_logic_operator, can_generate_streaming,
        runtime_hash_class, runtime_hash_class_path, is_spatial_hash[, num_generated_streaming_grids,
        editing_grid_count, editing_grids:[{grid_name, cell_size, loading_range, priority,
        block_on_slow_streaming}]]}. Needs the C++ handler (inert until built)."""
        try:
            return json.dumps(_exec(_GET_WP_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _NAV_STATUS_BODY = _HELP + r'''
rl = _mrl("navigation_build_status_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("navigation_build_status")))
else:
    res = _decode(rl.navigation_build_status_json())
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def navigation_build_status(ctx) -> str:
        """Read the navigation-build progress of the editor world (is it still building the navmesh?). Read-only.

        Reads UNavigationSystemV1::IsNavigationBuildInProgress() / GetNumRemainingBuildTasks() /
        GetNumRunningBuildTasks() -- none are UFUNCTIONs, so this needs the C++ handler. Poll this after a
        navmesh rebuild to know when 'building' drops to false / 'remaining_tasks' reaches 0.

        Returns {world, has_nav_system, building, remaining_tasks, running_tasks[, supported_agents]} (or a
        note if the editor world has no navigation system). Needs the C++ handler (inert until built)."""
        try:
            return json.dumps(_exec(_NAV_STATUS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _BUILD_WP_BODY = _HELP + r'''
rl = _mrl("build_world_partition_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("build_world_partition")))
else:
    res = _decode(rl.build_world_partition_json(PARAMS.get("builder", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def build_world_partition(ctx, builder: str = "streaming") -> str:
        """Report how to BUILD the open World Partition map (HONEST -- performs no in-editor build). No ledger.

        builder: 'streaming' (default) | 'hlod' | 'minimap' | 'navmesh' | 'landscape' | an explicit
                 commandlet -Builder class name.

        A real WP build is done by the WorldPartitionBuilderCommandlet as a SEPARATE -run= process; there is no
        safe, persisting in-editor build entry point (UWorldPartition::GenerateStreaming is an in-memory cook/PIE
        step that neither persists nor is safe under a live editor). This tool therefore does NOT perform (or
        fake) a build -- it resolves the WP, maps the builder to its commandlet class, and returns the exact
        command line to run plus readiness facts.

        Returns {world_partition_path, requested_builder, builder_class, in_editor_build_performed (always
        false), can_generate_streaming, is_streaming_enabled[, num_generated_streaming_grids], supported_via,
        command_line, note}. Clean error if the map is not partitioned. Needs the C++ handler (inert until built)."""
        try:
            return json.dumps(_exec(_BUILD_WP_BODY, {"builder": builder}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    _SET_MODE_BODY = _HELP + r'''
rl = _mrl("set_editor_mode_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_editor_mode")))
else:
    res = _decode(rl.set_editor_mode_json(PARAMS.get("mode_id", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_editor_mode",
            "prev_mode": res.get("prev_mode", ""), "mode": res.get("mode")})
        out = {"status": "success", "mode": res.get("mode"), "prev_mode": res.get("prev_mode"),
               "prev_modes": res.get("prev_modes"), "restored_default": bool(res.get("restored_default")),
               "is_active": bool(res.get("is_active")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def set_editor_mode(ctx, mode_id: str) -> str:
        """Activate a LEVEL-EDITOR mode by id (e.g. 'EM_Landscape', 'EM_Foliage'). REVERSIBLE ledgered write.

        mode_id: an editor mode id from list_editor_modes. Empty / 'default' / 'none' / 'EM_Default' restores
                 the DEFAULT mode set (deactivates the current tool mode).

        Calls GLevelEditorModeTools().ActivateMode (which itself deactivates incompatible / other-visible modes)
        -- the global FEditorModeTools is not Python-reachable, hence the C++ handler. Works on ANY level.
        Captures prev_mode (the prior primary non-default mode, '' if only the default was active) and appends
        the inverse to the per-session ledger for editor_level.undo. Returns {mode, prev_mode, prev_modes,
        restored_default, is_active, ledger_depth}.

        Ledgered op 'set_editor_mode' {prev_mode, mode}; inverse: set_editor_mode(prev_mode) (empty prev restores
        the default mode set). Needs the C++ handler (inert until the plugin DLL is rebuilt with
        MCPReflection_WorldExt.cpp)."""
        try:
            return json.dumps(_exec(_SET_MODE_BODY, {"mode_id": mode_id}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_WP_BODY = _HELP + r'''
rl = _mrl("set_world_partition_settings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_world_partition_settings")))
else:
    res = _decode(rl.set_world_partition_settings_json(PARAMS["settings_json"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_world_partition_settings", "prev": res.get("prev", {})})
        out = {"status": "success", "world_partition_path": res.get("world_partition_path"),
               "applied": res.get("applied"), "applied_count": res.get("applied_count"),
               "errors": res.get("errors"), "prev": res.get("prev"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def set_world_partition_settings(ctx, settings: dict) -> str:
        """Set editable UWorldPartition properties on the open map. REVERSIBLE ledgered write.

        settings: a flat dict {property: value}. Common keys:
                  bEnableStreaming (bool)         -- routed through the side-effecting SetEnableStreaming();
                  ServerStreamingMode (str)       -- 'ProjectDefault'|'Disabled'|'Enabled'|'EnabledInPIE';
                  ServerStreamingOutMode (str)    -- 'ProjectDefault'|'Disabled'|'Enabled';
                  DataLayersLogicOperator (str)   -- 'Or'|'And'.
                  Any other UWorldPartition UPROPERTY name is set via reflection. Struct/array values must be
                  passed as a UE ExportText STRING.

        Resolves the live UWorldPartition (C++-only path). Captures a prev {property: prior_value} object for the
        undo and MarkPackageDirty. Returns {world_partition_path, applied:[...], applied_count, errors:[...],
        prev:{...}, ledger_depth}. Clean error if the map is not partitioned.

        WARNING: mutates the LIVE World Partition of the open map. Run any live test ONLY on a throwaway
        WP-enabled SCRATCH map (see [[unrealmcp-never-mutate-real-level]]).

        Ledgered op 'set_world_partition_settings' {prev:{prop:value}}; inverse:
        set_world_partition_settings(prev). Needs the C++ handler (inert until built)."""
        params = {"settings_json": json.dumps(settings if isinstance(settings, dict) else {})}
        try:
            return json.dumps(_exec(_SET_WP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_GRID_BODY = _HELP + r'''
rl = _mrl("set_runtime_grid_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_runtime_grid")))
else:
    res = _decode(rl.set_runtime_grid_json(PARAMS["grid_json"]))
    if isinstance(res, dict) and res.get("error"):
        payload = {"status": "error", "message": res.get("error")}
        if res.get("available_grids") is not None:
            payload["available_grids"] = res.get("available_grids")
        print("@@UMCP@@" + json.dumps(payload))
    else:
        _ledger().append({"op": "set_runtime_grid", "grid_name": res.get("grid_name"),
            "prev": res.get("prev", {})})
        out = {"status": "success", "world_partition_path": res.get("world_partition_path"),
               "grid_name": res.get("grid_name"), "changed": res.get("changed"),
               "errors": res.get("errors"), "prev": res.get("prev"), "grid_count": res.get("grid_count"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def set_runtime_grid(ctx, grid_name: str, cell_size: int = None, loading_range: float = None,
                         priority: int = None, block_on_slow_streaming: bool = None,
                         origin: str = None, debug_color: str = None) -> str:
        """Edit one spatial-hash EDITING grid of the open World Partition map. REVERSIBLE ledgered write.

        grid_name:               the grid to edit (from get_world_partition_settings -> editing_grids).
        cell_size:               int cell size in cm (e.g. 12800).
        loading_range:           float loading range in cm (e.g. 25600).
        priority:                int grid priority.
        block_on_slow_streaming: bool -- block streaming when cells load too slowly.
        origin:                  UE ExportText FVector2D string, e.g. '(X=0.000,Y=0.000)'.
        debug_color:             UE ExportText FLinearColor string, e.g. '(R=1.0,G=0.0,B=0.0,A=1.0)'.

        Edits the private WITH_EDITORONLY_DATA UWorldPartitionRuntimeSpatialHash::Grids array (reflection;
        Python cannot reach it). Only the fields you pass are changed. Captures a prev {field: prior_value}
        object (+ a whole-array ExportText fallback) and MarkPackageDirty. Returns {world_partition_path,
        grid_name, changed:[...], errors:[...], prev:{...}, grid_count, ledger_depth}. If the grid_name is not
        found the error carries 'available_grids'. Requires a spatial-hash runtime hash.

        WARNING: mutates the LIVE World Partition of the open map. Run any live test ONLY on a throwaway
        WP-enabled SCRATCH map (see [[unrealmcp-never-mutate-real-level]]).

        Ledgered op 'set_runtime_grid' {grid_name, prev:{field:value}}; inverse:
        set_runtime_grid(grid_name, **prev). Needs the C++ handler (inert until built)."""
        grid = {"grid_name": grid_name}
        if cell_size is not None:
            grid["cell_size"] = cell_size
        if loading_range is not None:
            grid["loading_range"] = loading_range
        if priority is not None:
            grid["priority"] = priority
        if block_on_slow_streaming is not None:
            grid["block_on_slow_streaming"] = block_on_slow_streaming
        if origin is not None:
            grid["origin"] = origin
        if debug_color is not None:
            grid["debug_color"] = debug_color
        params = {"grid_json": json.dumps(grid)}
        try:
            return json.dumps(_exec(_SET_GRID_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # NO `undo` tool here; editor_level.py owns the unified `undo`. Ledger ops + inverses:
    #   set_editor_mode              {prev_mode, mode}           -> set_editor_mode(prev_mode)
    #   set_world_partition_settings {prev:{prop:value}}         -> set_world_partition_settings(prev)
    #   set_runtime_grid             {grid_name, prev:{f:value}} -> set_runtime_grid(grid_name, **prev)
