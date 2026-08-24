"""UserTools :: Niagara GRAPH / SCRIPT AUTHORING writers  (spec: docs/spec/niagara.md)

DRAFT wiring for the NiagaraEditor-C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Niagara4.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied
VERBATIM from the gold-standard niagara_runtime_cpp.py.

Fills six "graph/script authoring" spec features that stock Python cannot reach (they live in
NiagaraEditor C++): the emitter/system script graph + module/dynamic-input authoring are not
python-reachable.

  WRITES (ledger -- reversible):
    * create_niagara_scratch_pad_module -- add a scratch-pad module/dynamic-input script to a
                                    NiagaraSystem (data-level: factory-init + ScratchPadScripts.Add;
                                    NO system view model). Inverse: remove the scratch script by name.
    * create_niagara_module_asset  -- create a standalone UNiagaraScript module/dynamic_input/function
                                    ASSET via the engine's Niagara script factory. Inverse: delete asset
                                    (reuses editor_level.undo's GENERIC "create_asset" op).
    * add_niagara_graph_node       -- add ONE node (function_call | input) to a script graph.
                                    Inverse: delete_niagara_graph_node(script_path, node_guid).
    * build_niagara_graph          -- batch create nodes + links in a script graph. Inverse: delete each
                                    created node_guid.
    * delete_niagara_graph_node    -- remove a node (refuses the output node). Reversible via the editor's
                                    native undo (Modify-wrapped); descriptor captured for audit.
    * layout_niagara_graph         -- self-contained columnar auto-layout. Inverse: restore prior positions
                                    (layout_niagara_graph with options {"restore_positions": ...}).

The graph tools target a STANDALONE UNiagaraScript asset (script_path) -- the SAFE authoring surface --
not an emitter's live compiled stack graph (that is add/remove_niagara_module_to_stack in niagara_write).

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write's
inverse is appended to the shared per-session ledger for editor_level.undo to fold (create_asset already
handled generically; the niagara_* ops are documented for the coordinator to fold).

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
    return {"status": "error", "error": (fn + " requires the C++ NiagaraEditor handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Niagara4.cpp to enable it.")}
'''

    # ================================================================== #
    # 1) create_niagara_scratch_pad_module (write; ledger). Inverse: remove scratch script by name.
    # ================================================================== #

    _SCRATCH_BODY = _HELP + r'''
rl = _mrl("create_niagara_scratch_pad_module")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_niagara_scratch_pad_module")))
else:
    sysobj = EAL.load_asset(PARAMS["system_path"])
    if sysobj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load NiagaraSystem: " + PARAMS["system_path"]}))
    else:
        res = _decode(rl.create_niagara_scratch_pad_module(sysobj, PARAMS.get("script_name", ""),
            PARAMS.get("script_type", "dynamic_input")))
        if isinstance(res, dict) and res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
        else:
            saved = _save(PARAMS["system_path"])
            _ledger().append({"op": "niagara_add_scratch_pad", "system_path": PARAMS["system_path"],
                "script_name": res.get("script_name")})
            out = {"status": "success", "system": res.get("system"), "script_name": res.get("script_name"),
                   "script_path": res.get("script_path"), "script_type": res.get("script_type"),
                   "scratch_pad_count": res.get("scratch_pad_count"), "saved": saved,
                   "ledger_depth": len(_ledger())}
            print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_niagara_scratch_pad_module(ctx, system_path: str, script_name: str = "",
                                          script_type: str = "dynamic_input") -> str:
        """Add a scratch-pad module/dynamic-input script to a NiagaraSystem (data-level, no view model).

        system_path: NiagaraSystem asset path (e.g. '/Game/VFX/NS_Foo.NS_Foo').
        script_name: desired scratch script name (uniquified). Default 'ScratchModule'.
        script_type: 'dynamic_input' (default) or 'module'.

        Uses the engine's Niagara script factory to fully initialize the script, outers it to the system,
        and appends it to UNiagaraSystem.ScratchPadScripts. The system editor rebuilds its scratch-pad view
        models from this array on next open. Returns {system, script_name, script_path, script_type,
        scratch_pad_count, saved}. Inverse: remove the scratch script by name (native editor undo).
        NiagaraEditor-only; needs the C++ handler (inert until built)."""
        params = {"system_path": system_path, "script_name": script_name, "script_type": script_type}
        try:
            return json.dumps(_exec(_SCRATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 2) create_niagara_module_asset (write; ledger op "create_asset" -> generic delete inverse).
    # ================================================================== #

    _MODULE_ASSET_BODY = _HELP + r'''
rl = _mrl("create_niagara_module_asset")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_niagara_module_asset")))
else:
    res = _decode(rl.create_niagara_module_asset(PARAMS["package_path"], PARAMS["asset_name"],
        PARAMS.get("script_type", "module")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        pkg = res.get("package")
        saved = _save(pkg) if pkg else False
        _ledger().append({"op": "create_asset", "asset_path": pkg, "package_path": pkg})
        out = {"status": "success", "asset": res.get("asset"), "package": pkg,
               "asset_path": res.get("asset_path"), "script_type": res.get("script_type"),
               "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_niagara_module_asset(ctx, package_path: str, asset_name: str,
                                    script_type: str = "module") -> str:
        """Create a standalone UNiagaraScript asset (module/dynamic_input/function) via the engine factory.

        package_path: destination folder (e.g. '/Game/VFX/Modules').
        asset_name:   new asset name (e.g. 'MyModule').
        script_type:  'module' (default) | 'dynamic_input' | 'function'.

        Creates + fully initializes the script via the engine's own Niagara script factory, then saves the
        package. Returns {asset, package, asset_path, script_type, saved}. Inverse: delete the created asset
        (reuses editor_level.undo's generic 'create_asset' op). NiagaraEditor-only; needs the C++ handler."""
        params = {"package_path": package_path, "asset_name": asset_name, "script_type": script_type}
        try:
            return json.dumps(_exec(_MODULE_ASSET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 3) add_niagara_graph_node (write; ledger). Inverse: delete_niagara_graph_node(script_path, node_guid).
    # ================================================================== #

    _ADD_NODE_BODY = _HELP + r'''
rl = _mrl("add_niagara_graph_node")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_niagara_graph_node")))
else:
    res = _decode(rl.add_niagara_graph_node(PARAMS["script_path"], PARAMS["node_spec_json"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        saved = _save(PARAMS["script_path"])
        _ledger().append({"op": "niagara_add_graph_node", "script_path": PARAMS["script_path"],
            "node_guid": res.get("node_guid")})
        out = {"status": "success", "script": res.get("script"), "node_guid": res.get("node_guid"),
               "node_class": res.get("node_class"), "node_type": res.get("node_type"),
               "node_count": res.get("node_count"), "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_niagara_graph_node(ctx, script_path: str, node_spec_json: str) -> str:
        """Add ONE node to a standalone UNiagaraScript's editor graph.

        script_path:    UNiagaraScript asset path (a module/dynamic_input/function/scratch script).
        node_spec_json: JSON object describing the node. Supported kinds:
          function_call: {"kind":"function_call","script_path":"/...","pos_x":0,"pos_y":0}
          input:         {"kind":"input","input_name":"MyInput","input_type":"float",
                          "input_usage":"parameter","pos_x":0,"pos_y":0}
          input_type in bool|int|float|vector2|vector|vector4|linearcolor|quat|position.

        Returns {script, node_guid, node_class, node_type, node_count, saved}. Inverse:
        delete_niagara_graph_node(script_path, node_guid). NiagaraEditor-only; needs the C++ handler."""
        params = {"script_path": script_path, "node_spec_json": node_spec_json}
        try:
            return json.dumps(_exec(_ADD_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 4) build_niagara_graph (write; ledger). Inverse: delete each created node_guid.
    # ================================================================== #

    _BUILD_BODY = _HELP + r'''
rl = _mrl("build_niagara_graph")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("build_niagara_graph")))
else:
    res = _decode(rl.build_niagara_graph(PARAMS["script_path"], PARAMS["spec_json"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        saved = _save(PARAMS["script_path"])
        guids = [c.get("node_guid") for c in (res.get("created") or []) if c.get("node_guid")]
        _ledger().append({"op": "niagara_build_graph", "script_path": PARAMS["script_path"],
            "node_guids": guids})
        out = {"status": "success", "script": res.get("script"), "nodes_created": res.get("nodes_created"),
               "created": res.get("created"), "node_errors": res.get("node_errors"),
               "links_made": res.get("links_made"), "link_errors": res.get("link_errors"),
               "node_count": res.get("node_count"), "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def build_niagara_graph(ctx, script_path: str, spec_json: str) -> str:
        """Batch-build nodes + links in a standalone UNiagaraScript's editor graph.

        script_path: UNiagaraScript asset path.
        spec_json:   JSON: {"nodes":[{"id":"a","kind":"input","input_name":"X","input_type":"float"},
                     {"id":"b","kind":"function_call","script_path":"/..."}],
                     "links":[{"from_node":"a","from_pin":"","to_node":"b","to_pin":""}]}.
                     Node 'id' is a local handle used by links. Pins resolve by name (empty -> first pin of
                     that direction). Links use the Niagara schema, so incompatible pins are rejected.

        Returns {script, nodes_created, created:[{id,node_guid,node_type}], node_errors, links_made,
        link_errors, node_count, saved}. Inverse: delete each created node_guid. NiagaraEditor-only."""
        params = {"script_path": script_path, "spec_json": spec_json}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 5) delete_niagara_graph_node (write; ledger). Reversible via native editor undo (Modify-wrapped).
    # ================================================================== #

    _DELETE_NODE_BODY = _HELP + r'''
rl = _mrl("delete_niagara_graph_node")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_niagara_graph_node")))
else:
    res = _decode(rl.delete_niagara_graph_node(PARAMS["script_path"], PARAMS["node_guid"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        saved = _save(PARAMS["script_path"])
        _ledger().append({"op": "niagara_delete_graph_node", "script_path": PARAMS["script_path"],
            "node_guid": res.get("node_guid"), "node_class": res.get("node_class"),
            "pos_x": res.get("pos_x"), "pos_y": res.get("pos_y")})
        out = {"status": "success", "script": res.get("script"), "removed": res.get("removed"),
               "node_guid": res.get("node_guid"), "node_class": res.get("node_class"),
               "node_type": res.get("node_type"), "node_count": res.get("node_count"),
               "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_niagara_graph_node(ctx, script_path: str, node_guid: str) -> str:
        """Remove a node from a standalone UNiagaraScript's editor graph (breaks its links first).

        script_path: UNiagaraScript asset path.
        node_guid:   the node's GUID (from add_niagara_graph_node / build_niagara_graph / a graph reader).

        REFUSES to delete the graph's output node. Modify-wrapped so the editor's native undo can restore
        it. Returns {script, removed, node_guid, node_class, node_type, node_count, saved}. NiagaraEditor-
        only; needs the C++ handler (inert until built)."""
        params = {"script_path": script_path, "node_guid": node_guid}
        try:
            return json.dumps(_exec(_DELETE_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # 6) layout_niagara_graph (write; ledger). Inverse: restore prior_positions.
    # ================================================================== #

    _LAYOUT_BODY = _HELP + r'''
rl = _mrl("layout_niagara_graph")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("layout_niagara_graph")))
else:
    res = _decode(rl.layout_niagara_graph(PARAMS["script_path"], PARAMS.get("options_json", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        saved = _save(PARAMS["script_path"])
        if res.get("mode") != "restore":
            _ledger().append({"op": "niagara_layout_graph", "script_path": PARAMS["script_path"],
                "prior_positions": res.get("prior_positions") or {}})
        out = {"status": "success", "script": res.get("script"), "mode": res.get("mode"),
               "nodes_moved": res.get("nodes_moved"), "saved": saved, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def layout_niagara_graph(ctx, script_path: str, options_json: str = "") -> str:
        """Auto-layout a standalone UNiagaraScript's editor graph (columnar: output right, sources left).

        script_path:  UNiagaraScript asset path.
        options_json: optional JSON: {"column_width":300,"row_height":140}. To UNDO a prior layout, pass
                      {"restore_positions":{"<guid>":[x,y],...}} (the layout return carries prior_positions).

        Returns {script, mode, nodes_moved, saved}. Self-contained (no engine RelayoutGraph dependency).
        Inverse: layout_niagara_graph with the captured prior_positions. NiagaraEditor-only; needs the C++
        handler (inert until built)."""
        params = {"script_path": script_path, "options_json": options_json}
        try:
            return json.dumps(_exec(_LAYOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
