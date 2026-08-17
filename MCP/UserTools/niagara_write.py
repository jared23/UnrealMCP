"""UserTools :: Niagara (WRITE)  (spec: docs/spec/niagara.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE-wave module, the
mutating counterpart to niagara_read.py. Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py / reference create-wave module ai_write.py.

What this build exposes (NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * NiagaraSystem via unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, pkg,
                       unreal.NiagaraSystem, unreal.NiagaraSystemFactoryNew())
    NiagaraSystemFactoryNew shows a template/emitter WIZARD in interactive creation, but the scripting
    create_asset path never calls ConfigureProperties -> no modal pops (bounded-probed 2026-08-15 with
    the user watching: an isolated create+read+delete completed in one shot, no dialog). A fresh system
    is created EMPTY (0 emitters).

DEFERRED to the Wave-3 C++ batch (editor-only in this build's Python surface -- refused rather than
shipping a fake/unrevertable write): Niagara AUTHORING beyond the empty-system create --
  - add/remove EMITTERS to a system (emitter handles live in FVersionedNiagaraEmitter data; the
    top-level UNiagaraEmitter aliases are DEPRECATED and there is no Python builder to attach an
    emitter handle to a system -- niagara_read documents this).
  - add/remove/configure MODULES, RENDERERS, or SCRATCH scripts.
  - set USER PARAMETERS / expose parameters on a system.
  These are the Niagara editor's job (UNiagaraSystemEditorData / FNiagaraEditorModule) and need a C++
  handler in the consolidated Wave-3 recompile, then live testing. get_all_emitters / get_all_user_parameters
  (NiagaraFunctionLibrary) are READ-only and already wired into niagara_read.

Implemented (validated live, editor left CLEAN, ledger depth 0):
  - create_niagara_system  (WRITE; ledgered op "create_asset")

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the ONE unified `undo`).
create_niagara_system pushes the shared generic {op:create_asset, asset_path, package_path, created_dir}
whose inverse (close editors + GC + delete_asset [+ rmdir if we made the dir]) already exists in
editor_level.undo (folded during CREATE-WAVE1) -- nothing new to fold.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
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

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
    _NIAGARA_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
def _save_niagara(sysobj, system_path):
    # Persist a mutated NiagaraSystem. Prefers the C++ #10 handler save_niagara_system (SYNCHRONOUS
    # compile + PostEditChange + UPackage::SavePackage in C++) which DEFEATS the "FortniteMain custom
    # version ... Package will not be saved" failure that made every Python save path (save_asset /
    # save_packages) return False and silently drop the change on reload (QA 2026-08-15; root cause:
    # C++ #5's async RequestCompile still outstanding at save-time). Verified live 2026-08-16: 5/5
    # saves {compiled:true, saved:true}, 0 FortniteMain errors. Falls back to save_asset on a DLL that
    # predates C++ #10 (that path is the one that fails, but we now surface its result, never swallow it).
    mrl = getattr(unreal, "MCPReflectionLibrary", None)
    if mrl is not None and hasattr(mrl, "save_niagara_system"):
        try:
            r = json.loads(mrl.save_niagara_system(sysobj))
            if isinstance(r, dict) and not r.get("error"):
                return {"saved": bool(r.get("saved")), "compiled": bool(r.get("compiled")), "save_path": "cpp"}
            return {"saved": False, "compiled": False, "save_error": (r or {}).get("error"), "save_path": "cpp"}
        except Exception as e:
            return {"saved": False, "compiled": False, "save_error": str(e), "save_path": "cpp"}
    try:
        ok = unreal.EditorAssetLibrary.save_asset(system_path, only_if_is_dirty=False)
        return {"saved": bool(ok), "compiled": None, "save_path": "python"}
    except Exception as e:
        return {"saved": False, "compiled": None, "save_error": str(e), "save_path": "python"}
'''

    # ------------------------------------------------------------------ #
    # create_niagara_system — non-interactive NiagaraSystem asset creation #
    # ------------------------------------------------------------------ #
    _CREATE_NS_BODY = _NIAGARA_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if not hasattr(unreal, "NiagaraSystem") or not hasattr(unreal, "NiagaraSystemFactoryNew"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "Niagara classes not available (NiagaraSystem/NiagaraSystemFactoryNew missing)"}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # Do NOT wrap create_asset in a ScopedEditorTransaction (PROTOCOL #5b) -- it would trap the new
    # asset in the editor undo buffer and block a later delete. Ledgered via our own create_asset op.
    ns = at.create_asset(name, package_path, unreal.NiagaraSystem, unreal.NiagaraSystemFactoryNew())
    if ns is None or not isinstance(ns, unreal.NiagaraSystem):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(ns).__name__, asset_path)}))
    else:
        _close_editors(ns)
        # Save on create (PROTOCOL #5c): persists it + makes the undo-delete reliable.
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        # Readback: emitter count via the READ-only NiagaraFunctionLibrary (fresh system = 0 emitters).
        emitter_count = None
        try:
            ems = unreal.NiagaraFunctionLibrary.get_all_emitters(ns)
            emitter_count = len(ems) if ems is not None else None
        except Exception:
            emitter_count = None
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": ns.get_name(),
            "asset_path": asset_path, "object_path": ns.get_path_name(),
            "class": ns.get_class().get_name(), "emitter_count": emitter_count,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_niagara_system(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new (empty) NiagaraSystem asset non-interactively (NO modal / factory wizard).

        name:         asset name for the new NiagaraSystem (e.g. 'NS_Sparks').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be under
                      a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate folders are
                      created as needed.

        Uses AssetTools.create_asset(..., unreal.NiagaraSystem, unreal.NiagaraSystemFactoryNew()). The
        factory shows a template/emitter wizard in the interactive editor, but the scripting create_asset
        path never calls ConfigureProperties -> no modal pops (bounded-probed live 2026-08-15). The system
        is created EMPTY (0 emitters); inspect it with niagara_read.get_niagara_system_info.

        NOTE: adding EMITTERS is now supported via add_emitter_to_system (C++ #5 handler). Deeper
        authoring -- MODULES, RENDERERS, USER PARAMETERS -- remains editor-only and deferred to a later
        C++ batch. This command creates the (empty) system asset; populate it with add_emitter_to_system.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close any
        editors for the asset, delete it, and remove package_path if we created it and it is now empty
        (the same generic create_asset inverse used by every create-wave command)."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_NS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_emitter_to_system / remove_emitter_from_system (via C++ #5 handler) #
    # ------------------------------------------------------------------ #
    # ENABLED 2026-08-15 by C++ #5 (MCPReflectionLibrary.AddEmitterToSystem/RemoveEmitterFromSystem).
    # hasattr-guarded. The added handle takes the SOURCE emitter's name (the copy API takes no name);
    # the tool returns the REAL added_handle_name/id -> ledger keys off the id.
    _ADD_EMITTER_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; source_path = PARAMS["source_emitter_path"]
handle_name = PARAMS.get("handle_name") or ""
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
src = EAL.load_asset(source_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif src is None or not isinstance(src, unreal.NiagaraEmitter):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraEmitter: %s" % source_path}))
elif mrl is None or not hasattr(mrl, "add_emitter_to_system"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_emitter_to_system unavailable (plugin DLL predates C++ #5; recompile needed)"}))
else:
    res = json.loads(mrl.add_emitter_to_system(sysobj, src, handle_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "add_emitter", "asset_path": system_path,
                          "handle_id": res.get("added_handle_id")})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "source_emitter": src.get_name(), "added_handle_name": res.get("added_handle_name"),
            "added_handle_id": res.get("added_handle_id"), "emitter_count": res.get("emitter_count"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"):
            _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _REMOVE_EMITTER_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; handle = PARAMS["handle_name_or_id"]
source_path = PARAMS.get("source_emitter_path") or ""
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "remove_emitter_from_system"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_emitter_from_system unavailable (plugin DLL predates C++ #5; recompile needed)"}))
else:
    res = json.loads(mrl.remove_emitter_from_system(sysobj, str(handle)))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("removed"):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no emitter handle matching '%s'" % handle, "emitter_count": res.get("emitter_count")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "remove_emitter", "asset_path": system_path,
                          "handle_id": str(handle), "source_emitter_path": source_path})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "removed_handle": str(handle), "emitter_count": res.get("emitter_count"),
            "source_captured": bool(source_path), "saved": _sv.get("saved"),
            "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"):
            _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    @mcp.tool()
    def add_emitter_to_system(ctx, system_path: str, source_emitter_path: str,
                              handle_name: str = "") -> str:
        """Add a COPY of an emitter to a NiagaraSystem. REQUIRES the C++ #5 handler
        (unreal.MCPReflectionLibrary.add_emitter_to_system); returns a clear error on an older DLL.

        system_path:         object/package path of the NiagaraSystem asset.
        source_emitter_path: object/package path of a UNiagaraEmitter asset to copy in.
        handle_name:         NOTE -- currently NOT applied: the Niagara copy API names the new handle
                             after the SOURCE emitter. The tool returns the REAL added_handle_name/id.

        Saved after the edit. Verify with niagara_read.get_niagara_system_info (emitter list).

        Ledgered write op 'add_emitter' {asset_path, handle_id}. Inverse (in editor_level.undo):
        remove_emitter_from_system(system, handle_id) -> FAITHFUL (removes exactly the handle we added)."""
        params = {"system_path": system_path, "source_emitter_path": source_emitter_path,
                  "handle_name": handle_name}
        try:
            return json.dumps(_exec(_ADD_EMITTER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_emitter_from_system(ctx, system_path: str, handle_name_or_id: str,
                                   source_emitter_path: str = "") -> str:
        """Remove an emitter handle (by name OR id) from a NiagaraSystem. REQUIRES the C++ #5 handler.

        system_path:         object/package path of the NiagaraSystem asset.
        handle_name_or_id:   the emitter handle's name or id (as returned by add_emitter_to_system).
        source_emitter_path: OPTIONAL -- pass the source emitter's asset path to make this UNDOABLE.
                             The removed handle is a copy, so undo needs the source to re-add it.

        Saved after the edit. Ledgered write op 'remove_emitter' {asset_path, handle_id,
        source_emitter_path}. Inverse (in editor_level.undo): if source_emitter_path was provided,
        add_emitter_to_system(system, source) -- best-effort (the re-added handle gets a NEW id);
        otherwise undo reports it cannot restore (a copied emitter has no recoverable source)."""
        params = {"system_path": system_path, "handle_name_or_id": handle_name_or_id,
                  "source_emitter_path": source_emitter_path}
        try:
            return json.dumps(_exec(_REMOVE_EMITTER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # C++ #10 (Deeper Niagara) — user params (A) + emitter rename/renderers (B)
    # ENABLED 2026-08-16 (C++ #10 A/B/D built+verified on Windows). hasattr-guarded. All saves route
    # through _save_niagara -> save_niagara_system (sync compile + C++ save; defeats the FortniteMain
    # save-fail, confirmed live 5/5). Module authoring (C) is DEFERRED (NiagaraEditor stack utils not
    # *_API-exported in stock UE); those tools are added only after the engine-patched DLL lands.
    # ================================================================== #

    # ---- A: user parameters -------------------------------------------- #
    _ADD_USERPARAM_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; param = PARAMS["param_name"]; type_name = PARAMS["type_name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "add_niagara_user_parameter"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_niagara_user_parameter unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.add_niagara_user_parameter(sysobj, param, type_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "add_user_param", "asset_path": system_path,
                          "param": res.get("param"), "type": res.get("type")})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "param": res.get("param"), "type": res.get("type"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _SET_USERPARAM_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; param = PARAMS["param_name"]; value_json = PARAMS["value_json"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "set_niagara_user_parameter_value"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler set_niagara_user_parameter_value unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.set_niagara_user_parameter_value(sysobj, param, value_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        prev_json = json.dumps(res.get("prev"))
        _ledger().append({"op": "set_user_param", "asset_path": system_path,
                          "param": res.get("param"), "prev_value_json": prev_json})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "param": res.get("param"), "set": True, "prev": res.get("prev"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _REMOVE_USERPARAM_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; param = PARAMS["param_name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "remove_niagara_user_parameter"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_niagara_user_parameter unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.remove_niagara_user_parameter(sysobj, param))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("removed"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no user parameter matching '%s'" % param}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "remove_user_param", "asset_path": system_path,
                          "param": res.get("param"), "type": res.get("type"),
                          "value_json": json.dumps(res.get("value"))})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "param": res.get("param"), "removed": True, "type": res.get("type"), "value": res.get("value"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    @mcp.tool()
    def add_niagara_user_parameter(ctx, system_path: str, param_name: str, type_name: str) -> str:
        """Add a User.* parameter to a NiagaraSystem (C++ #10). Clear error on a pre-#10 DLL.

        param_name: name (the 'User.' prefix is added automatically if omitted).
        type_name:  bool | int | float | vector2 | vector | vector4 | linearcolor | quat.

        Saved via save_niagara_system; response carries saved/compiled. Verify with niagara_read
        (get_all_user_parameters). Ledgered 'add_user_param' {asset_path,param,type};
        inverse remove_niagara_user_parameter -> FAITHFUL."""
        params = {"system_path": system_path, "param_name": param_name, "type_name": type_name}
        try:
            return json.dumps(_exec(_ADD_USERPARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_niagara_user_parameter_value(ctx, system_path: str, param_name: str, value_json: str) -> str:
        """Set an existing User.* parameter's default value on a NiagaraSystem (C++ #10).

        value_json: MUST be ARRAY-form JSON -- the UE JSON reader rejects a bare top-level scalar.
                    Use '[2.5]' (float/int), '[true]'? no -> bool uses '[1]'/'[0]'? -- pass the value
                    wrapped in an array: float/int '[2.5]' / '[3]', vector '[1,0,0]', vector4/linearcolor
                    '[1,0,0,1]', quat '[0,0,0,1]'. (Confirmed live 2026-08-16: '[2.5]' and '[1,2,3]' OK,
                    bare '2.5' rejected.)

        Saved via save_niagara_system. The C++ returns the PRIOR value, ledgered for undo. Ledgered
        'set_user_param' {asset_path,param,prev_value_json}; inverse re-sets prev_value_json."""
        params = {"system_path": system_path, "param_name": param_name, "value_json": value_json}
        try:
            return json.dumps(_exec(_SET_USERPARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_niagara_user_parameter(ctx, system_path: str, param_name: str) -> str:
        """Remove a User.* parameter from a NiagaraSystem (C++ #10).

        param_name: name (with or without the 'User.' prefix).

        Saved via save_niagara_system. The C++ returns the removed type+value so undo re-adds it exactly.
        Ledgered 'remove_user_param' {asset_path,param,type,value_json}; inverse:
        add_niagara_user_parameter then set_niagara_user_parameter_value(value_json)."""
        params = {"system_path": system_path, "param_name": param_name}
        try:
            return json.dumps(_exec(_REMOVE_USERPARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- B: emitter-handle rename + renderers -------------------------- #
    _RENAME_EMITTER_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; old_name = PARAMS["old_name"]; new_name = PARAMS["new_name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "rename_niagara_emitter_handle"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler rename_niagara_emitter_handle unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.rename_niagara_emitter_handle(sysobj, old_name, new_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        applied = res.get("new_name") or new_name
        _ledger().append({"op": "rename_niagara_emitter", "asset_path": system_path,
                          "old_name": old_name, "new_name": applied})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "old_name": res.get("old_name"), "new_name": applied,
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _ADD_RENDERER_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; emitter_name = PARAMS["emitter_name"]; renderer_type = PARAMS["renderer_type"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "add_niagara_renderer"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_niagara_renderer unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.add_niagara_renderer(sysobj, emitter_name, renderer_type))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        count = res.get("renderer_count")
        landed_index = (count - 1) if isinstance(count, int) and count > 0 else None
        _ledger().append({"op": "add_niagara_renderer", "asset_path": system_path,
                          "emitter": emitter_name, "renderer_index": landed_index,
                          "renderer_type": renderer_type})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "emitter": res.get("emitter"), "renderer_class": res.get("renderer_class"),
            "renderer_index": landed_index, "renderer_count": count,
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _REMOVE_RENDERER_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; emitter_name = PARAMS["emitter_name"]; renderer_index = int(PARAMS["renderer_index"])
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "remove_niagara_renderer"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_niagara_renderer unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.remove_niagara_renderer(sysobj, emitter_name, renderer_index))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        removed_class = res.get("removed_renderer_class") or ""
        rtype = ""
        for kw in ("Sprite", "Mesh", "Ribbon", "Light"):
            if kw.lower() in removed_class.lower():
                rtype = kw.lower(); break
        _ledger().append({"op": "remove_niagara_renderer", "asset_path": system_path,
                          "emitter": emitter_name, "renderer_index": renderer_index,
                          "renderer_type": rtype, "removed_renderer_class": removed_class})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "emitter": res.get("emitter"), "removed_renderer_class": removed_class, "renderer_type": rtype,
            "renderer_count": res.get("renderer_count"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    @mcp.tool()
    def rename_niagara_emitter(ctx, system_path: str, old_name: str, new_name: str) -> str:
        """Rename an emitter HANDLE on a NiagaraSystem (C++ #10). The system may de-duplicate the name;
        the tool returns the REAL applied name. Saved via save_niagara_system. Ledgered
        'rename_niagara_emitter' {asset_path,old_name,new_name}; inverse rename back -> FAITHFUL."""
        params = {"system_path": system_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_EMITTER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_niagara_renderer(ctx, system_path: str, emitter_name: str, renderer_type: str) -> str:
        """Add a renderer to a named emitter on a NiagaraSystem (C++ #10).

        renderer_type: sprite | mesh | ribbon | light (case-insensitive).

        Renderers append -> lands at index (renderer_count - 1). Saved via save_niagara_system.
        Ledgered 'add_niagara_renderer' {asset_path,emitter,renderer_index,renderer_type}; inverse
        remove_niagara_renderer(renderer_index) -> FAITHFUL if the list did not shift (LIFO undo)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "renderer_type": renderer_type}
        try:
            return json.dumps(_exec(_ADD_RENDERER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_niagara_renderer(ctx, system_path: str, emitter_name: str, renderer_index: int) -> str:
        """Remove the renderer at renderer_index on a named emitter (C++ #10). Saved via
        save_niagara_system. Ledgered 'remove_niagara_renderer'
        {asset_path,emitter,renderer_index,renderer_type,removed_renderer_class}; inverse
        add_niagara_renderer(renderer_type) -- BEST-EFFORT (default props, re-appends at end)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "renderer_index": renderer_index}
        try:
            return json.dumps(_exec(_REMOVE_RENDERER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- C: modules on the emitter script stack ------------------------ #
    # ENABLED 2026-08-16 by the engine-patched DLL (NiagaraEditor stack utils now NIAGARAEDITOR_API-exported
    # on the WINDOWS source engine). add/remove module FEASIBLE; set_module_input PARTIAL (scalar-only).
    # DEPENDENCY: module handlers link only against the patched Windows engine (see docs/board/memory).
    _ADD_MODULE_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; emitter = PARAMS["emitter_name"]
usage = PARAMS["script_usage"]; module_path = PARAMS["module_script_path"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "add_niagara_module_to_stack"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_niagara_module_to_stack unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.add_niagara_module_to_stack(sysobj, emitter, usage, module_path))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "add_module", "asset_path": system_path, "emitter_name": emitter,
                          "script_usage": usage, "node_guid": res.get("node_guid")})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "emitter": res.get("emitter"), "usage": res.get("usage"),
            "added_module": res.get("added_module"), "node_guid": res.get("node_guid"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _SET_MODULE_INPUT_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; emitter = PARAMS["emitter_name"]
usage = PARAMS["script_usage"]; module = PARAMS["module_name"]
inp = PARAMS["input_name"]; value_json = PARAMS["value_json"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "set_niagara_module_input"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler set_niagara_module_input unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.set_niagara_module_input(sysobj, emitter, usage, module, inp, value_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "set_module_input", "asset_path": system_path, "emitter_name": emitter,
                          "script_usage": usage, "module_name": module, "input_name": inp,
                          "prior_value": res.get("prior_value"), "new_value_json": value_json})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "emitter": res.get("emitter"), "module": res.get("module"), "input": res.get("input"),
            "set": res.get("set"), "prior_value": res.get("prior_value"),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    _REMOVE_MODULE_BODY = _NIAGARA_HELPERS + r'''
system_path = PARAMS["system_path"]; emitter = PARAMS["emitter_name"]
usage = PARAMS["script_usage"]; node_guid = PARAMS["node_guid"]
module_path = PARAMS.get("module_script_path") or ""
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
sysobj = EAL.load_asset(system_path)
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
elif mrl is None or not hasattr(mrl, "remove_niagara_module_from_stack"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_niagara_module_from_stack unavailable (plugin DLL predates C++ #10; recompile needed)"}))
else:
    res = json.loads(mrl.remove_niagara_module_from_stack(sysobj, emitter, usage, node_guid))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("removed"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no module node with guid '%s' in that stack" % node_guid}))
    else:
        _sv = _save_niagara(sysobj, system_path)
        _ledger().append({"op": "remove_module", "asset_path": system_path, "emitter_name": emitter,
                          "script_usage": usage, "node_guid": node_guid, "module_script_path": module_path})
        _out = {"status": "success" if _sv.get("saved") else "warning", "system": res.get("system"),
            "emitter": res.get("emitter"), "removed": res.get("removed"), "node_guid": res.get("node_guid"),
            "module_recreatable": bool(module_path),
            "saved": _sv.get("saved"), "compiled": _sv.get("compiled"), "ledger_depth": len(_ledger())}
        if _sv.get("save_error"): _out["save_error"] = _sv["save_error"]
        print("@@UMCP@@" + json.dumps(_out))
'''

    @mcp.tool()
    def add_niagara_module_to_stack(ctx, system_path: str, emitter_name: str, script_usage: str,
                                    module_script_path: str) -> str:
        """Add a module (a Niagara Module UNiagaraScript at module_script_path) to a named emitter's script
        stack (C++ #10, area C). REQUIRES the engine-patched Windows DLL (module handlers link only against it).

        script_usage: particle_spawn | particle_update | emitter_spawn | emitter_update (case-insensitive).
        Saved + recompiled via save_niagara_system. Ledgered 'add_module' {asset_path,emitter_name,script_usage,
        node_guid}; inverse remove_niagara_module_from_stack(...,node_guid) -> FAITHFUL."""
        params = {"system_path": system_path, "emitter_name": emitter_name,
                  "script_usage": script_usage, "module_script_path": module_script_path}
        try:
            return json.dumps(_exec(_ADD_MODULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_niagara_module_input(ctx, system_path: str, emitter_name: str, script_usage: str,
                                 module_name: str, input_name: str, value_json: str) -> str:
        """PARTIAL / FRAGILE (C++ #10, area C). Set a SCALAR local value input (float/int/bool) on a placed
        module. Does NOT handle vector/struct/dynamic/data-interface or already-overridden (connected) inputs
        -- those return an error. value_json is a bare JSON scalar ('1.5','3','true'). Saved + recompiled.
        Ledgered 'set_module_input' {...,prior_value,new_value_json}; inverse re-sets prior_value."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "script_usage": script_usage,
                  "module_name": module_name, "input_name": input_name, "value_json": value_json}
        try:
            return json.dumps(_exec(_SET_MODULE_INPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_niagara_module_from_stack(ctx, system_path: str, emitter_name: str, script_usage: str,
                                         node_guid: str, module_script_path: str = "") -> str:
        """Remove a module node (by node_guid from add_niagara_module_to_stack) from a named emitter's stack
        (C++ #10, area C). REQUIRES the engine-patched Windows DLL. Saved + recompiled.

        module_script_path: OPTIONAL -- pass the module's asset path to make undo best-effort re-addable
        (re-add gets a NEW node_guid + default inputs; overrides are lost). Ledgered 'remove_module'
        {asset_path,emitter_name,script_usage,node_guid,module_script_path}; inverse: if module_script_path
        given, add_niagara_module_to_stack(...) -- else undo reports it cannot restore."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "script_usage": script_usage,
                  "node_guid": node_guid, "module_script_path": module_script_path}
        try:
            return json.dumps(_exec(_REMOVE_MODULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
