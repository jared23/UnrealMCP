"""UserTools :: Blueprint INTERFACE authoring + variable-flag reader (C++ #19).

Thin Python dispatch to the C++ handlers on unreal.MCPReflectionLibrary (hasattr-guarded, auto-enable when the
plugin DLL carries them): ImplementBlueprintInterface / RemoveBlueprintInterface / ListBlueprintInterfaces /
GetBlueprintVariableFlagsJson. The two WRITE tools ledger a reversible op; inverses fold into editor_level.undo
(implement<->remove). remove_blueprint_interface's inverse is LOSSY (RemoveInterface drops the interface's
function graphs; re-add restores empty graphs) — documented in the tool + the fold.

Snippet convention copied from niagara_write2.py: base64 PARAMS, Output-Log capture wrap, per-session ledger.
No triple-single-quote / no backslash inside snippet r-strings.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

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
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        # Drain BEFORE the op too: blueprint ops call CompileBlueprint mid-op, which triggers a UE GC (-> Python
        # PyGC_Collect) BEFORE this op's own after-drain can run, so it would hit cyclic garbage left by the prior
        # op. Draining here first clears that. (See after-drain below for the full root-cause note.)
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
        resp = send_command("execute_python", {"code": _wrap(code)})
        # Drain the editor's Python cyclic-GC heap after each op. Blueprint ops call CompileBlueprint, which
        # triggers a UE GC -> Python's PyGC_Collect (FPythonScriptPlugin::OnPreGarbageCollect); if prior ops left
        # cyclic garbage in the Python heap it AVs (reading the "full" 0x...6618 string as a pointer). Draining as
        # its own post-op call keeps that heap clean. Same root cause + fix as the StateTree writers (2026-08-18).
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
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

    _HELP = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _cpp(name):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, name):
        return None
    return getattr(rl, name)
def _bp(p):
    b = unreal.EditorAssetLibrary.load_asset(p)
    return b if (b is not None and isinstance(b, unreal.Blueprint)) else None
'''

    _IMPL_BODY = _HELP + r'''
bp_path = PARAMS["blueprint_path"]; iface = PARAMS["interface_path"]
bp = _bp(bp_path); fn = _cpp("implement_blueprint_interface")
if bp is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % bp_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ implement_blueprint_interface unavailable"}))
else:
    res = json.loads(fn(bp, iface))
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or "handler error", "handler": res}))
    else:
        actual = res.get("interface") or iface
        _ledger().append({"op": "implement_blueprint_interface", "blueprint": bp_path, "interface": actual})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp_path, "interface": actual,
            "implemented_count": res.get("implemented_count"), "interfaces": res.get("interfaces"),
            "ledger_depth": len(_ledger())}))
'''

    _REMOVE_BODY = _HELP + r'''
bp_path = PARAMS["blueprint_path"]; iface = PARAMS["interface_path"]
bp = _bp(bp_path); fn = _cpp("remove_blueprint_interface")
if bp is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % bp_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ remove_blueprint_interface unavailable"}))
else:
    res = json.loads(fn(bp, iface))
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or "handler error", "handler": res}))
    else:
        actual = res.get("interface") or iface
        _ledger().append({"op": "remove_blueprint_interface", "blueprint": bp_path, "interface": actual})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp_path, "interface": actual,
            "implemented_count": res.get("implemented_count"), "interfaces": res.get("interfaces"),
            "inverse_note": "LOSSY: re-add restores an EMPTY interface implementation (graphs not preserved)",
            "ledger_depth": len(_ledger())}))
'''

    _LIST_BODY = _HELP + r'''
bp_path = PARAMS["blueprint_path"]
bp = _bp(bp_path); fn = _cpp("list_blueprint_interfaces")
if bp is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % bp_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ list_blueprint_interfaces unavailable"}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(fn(bp))))
'''

    _VARFLAGS_BODY = _HELP + r'''
bp_path = PARAMS["blueprint_path"]; var = PARAMS["var_name"]
bp = _bp(bp_path); fn = _cpp("get_blueprint_variable_flags_json")
if bp is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % bp_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ get_blueprint_variable_flags_json unavailable"}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(fn(bp, var))))
'''

    @mcp.tool()
    def implement_blueprint_interface(ctx, blueprint_path: str, interface_path: str) -> str:
        """Add an interface (native '/Script/...' or BP '/Game/...' path) to a Blueprint + recompile. Reversible
        (inverse = remove_blueprint_interface). Ledgered op 'implement_blueprint_interface' {blueprint, interface}."""
        try:
            return json.dumps(_exec(_IMPL_BODY, {"blueprint_path": blueprint_path, "interface_path": interface_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_blueprint_interface(ctx, blueprint_path: str, interface_path: str) -> str:
        """Remove an implemented interface from a Blueprint + recompile. Ledgered op 'remove_blueprint_interface'
        {blueprint, interface}. INVERSE IS LOSSY: RemoveInterface drops the interface's function graphs, so undo
        re-adds an EMPTY implementation (graph bodies not restored)."""
        try:
            return json.dumps(_exec(_REMOVE_BODY, {"blueprint_path": blueprint_path, "interface_path": interface_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_blueprint_interfaces(ctx, blueprint_path: str) -> str:
        """List a Blueprint's implemented interfaces: [{path, name, graphs}]. Read-only."""
        try:
            return json.dumps(_exec(_LIST_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_blueprint_variable_flags(ctx, blueprint_path: str, var_name: str) -> str:
        """Read a member variable's flags (setters exist but had no getters): instance_editable /
        blueprint_read_only / expose_on_spawn / private / expose_to_cinematics / config_variable / category /
        replication. Read-only. Unblocks reversible flag setters."""
        try:
            return json.dumps(_exec(_VARFLAGS_BODY, {"blueprint_path": blueprint_path, "var_name": var_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"
