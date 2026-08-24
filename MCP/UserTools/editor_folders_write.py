"""UserTools :: Editor World-Outliner FOLDER create/delete (WRITE)  (spec: docs/spec/editor.md)

Clean-room Python wiring for the C++ #18 outliner-folder handlers on unreal.MCPReflectionLibrary
(GROUP 2). LevelEditorSubsystem has no create-EMPTY-folder binding; the C++ handlers use
FActorFolders::Get().CreateFolder / DeleteFolder on the editor world, so an empty folder can be
created/removed without needing an actor to host it.

Query convention, base64 PARAMS injection, Output-Log auto-capture, and the per-session ledger are
copied VERBATIM from the gold-standard editor_level.py / editor_levels.py. This module registers NO
own `undo` tool (editor_level.py owns the ONE unified `undo`).

Implemented:
  - create_outliner_folder  (WRITE; op "create_outliner_folder"; captures created flag)
  - delete_outliner_folder  (WRITE; op "delete_outliner_folder")

Undo (reported to the coordinator to fold into editor_level.undo):
  create_outliner_folder -> if created: delete_outliner_folder(folder_path); else no-op (already existed).
  delete_outliner_folder -> create_outliner_folder(folder_path) — faithful for an EMPTY folder.

DEFERRED EDGE: deleting a POPULATED folder re-parents its child actors up to the parent folder
(FActorFolders::DeleteFolder behaviour). create_outliner_folder cannot restore that child->folder
membership, so the inverse is faithful only for empty folders. delete_outliner_folder therefore does
NOT capture/restore child membership; a populated-folder delete's actor re-parenting is not reverted.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies here contain NO triple-single-quote and NO stray backslashes; all data crosses as base64.
Never assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the wrapper's own names).
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


# Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
_FOLDER_HELPERS = r'''
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
def _reflib(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
'''


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
    # create_outliner_folder — create an EMPTY World-Outliner folder     #
    # ------------------------------------------------------------------ #
    _CREATE_FOLDER_BODY = _FOLDER_HELPERS + r'''
rl = _reflib("create_outliner_folder")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary.create_outliner_folder unavailable — reload the MCP server after the C++ #18 rebuild"}))
else:
    with unreal.ScopedEditorTransaction("MCP create_outliner_folder"):
        res = json.loads(rl.create_outliner_folder(PARAMS["folder_path"]))
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps(res))
    else:
        _ledger().append({"op": "create_outliner_folder", "folder_path": PARAMS["folder_path"],
                          "created": bool(res.get("created"))})
        res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def create_outliner_folder(ctx, folder_path: str) -> str:
        """Create an empty World-Outliner (level) folder in the current editor world (C++ #18
        create_outliner_folder, via FActorFolders). If the folder already exists this is a no-op
        (created=false) and undo does nothing. Ledgered, reversible.

        folder_path: outliner folder path, e.g. 'MCP_TestFolder' or 'Enemies/Ranged' (nested by '/').

        Ledgered op 'create_outliner_folder' {folder_path, created}; inverse (folded into
        editor_level.undo): if created -> delete_outliner_folder(folder_path); else no-op."""
        try:
            return json.dumps(_exec(_CREATE_FOLDER_BODY, {"folder_path": folder_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_outliner_folder — delete a World-Outliner folder            #
    # ------------------------------------------------------------------ #
    _DELETE_FOLDER_BODY = _FOLDER_HELPERS + r'''
rl = _reflib("delete_outliner_folder")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary.delete_outliner_folder unavailable — reload the MCP server after the C++ #18 rebuild"}))
else:
    with unreal.ScopedEditorTransaction("MCP delete_outliner_folder"):
        res = json.loads(rl.delete_outliner_folder(PARAMS["folder_path"]))
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps(res))
    else:
        _ledger().append({"op": "delete_outliner_folder", "folder_path": PARAMS["folder_path"]})
        res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def delete_outliner_folder(ctx, folder_path: str) -> str:
        """Delete a World-Outliner (level) folder in the current editor world (C++ #18
        delete_outliner_folder, via FActorFolders). Refused if the folder is absent. Ledgered; the
        inverse re-creates the (empty) folder.

        folder_path: outliner folder path to remove, e.g. 'MCP_TestFolder'.

        Ledgered op 'delete_outliner_folder' {folder_path}; inverse (folded into editor_level.undo):
        create_outliner_folder(folder_path). DEFERRED EDGE: deleting a POPULATED folder re-parents its
        child actors up one level (FActorFolders); that child re-parenting is NOT captured and so is not
        reverted by the inverse — the inverse is faithful only for empty folders."""
        try:
            return json.dumps(_exec(_DELETE_FOLDER_BODY, {"folder_path": folder_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
