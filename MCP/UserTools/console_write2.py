"""UserTools :: Console (arbitrary command execution)  (spec: docs/spec/console.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Companion to console.py:
it ships the ONE general escape-hatch console.py deliberately withheld -- execute_console_command,
which runs an ARBITRARY console command line and captures the Output-Log delta around the call.

This lives in a SEPARATE module (not folded into console.py) because console.py was being actively
extended by another agent at build time; a separate file avoids a write collision. It reuses
console.py's exact console log-delta capture pattern (_con_echo) -- snapshot the live .log size, run
the command via unreal.SystemLibrary.execute_console_command, flush, and return the appended lines
with timestamp prefixes stripped and MCP/script noise filtered.

Reversibility: an arbitrary console command has NO faithful inverse (e.g. 'stat unit', 'r.SetRes',
'DumpConsoleCommands' all differ), so per PROTOCOL this tool is NON-ledgered and NON-reversible -- it
records NOTHING on the undo ledger and there is nothing for the coordinator to fold into
editor_level.undo. For a REVERSIBLE console-variable change, use console.set_console_variable (which
captures the prior value and ledgers a 'set_cvar' inverse).

SAFETY: obviously-catastrophic editor-shutdown commands ('quit' / 'exit') are REFUSED before anything
is sent to the editor -- they would close the shared editor and hang every other agent. All other
commands are allowed (this is an intentional escape hatch).

HARD CONSTRAINTS honored: snippet bodies contain NO triple-single-quotes and NO stray backslashes;
all params travel as base64 JSON via _exec; no reserved local names are assigned.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py / console.py) ---
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

    # Console log-delta capture (same mechanism as console.py's _con_echo). No triple-single-quote /
    # no backslash. _con_logfile -> live .log path; _con_strip_ts -> drop [timestamp][frame] prefix;
    # _con_echo(cmd) -> snapshot .log, run cmd via SystemLibrary.execute_console_command, return the
    # cleaned appended lines.
    _EXEC_HELPERS = r'''
import unreal, json, os
def _con_logfile():
    try:
        d = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
        for f in os.listdir(d):
            if f.endswith(".log") and "-backup-" not in f:
                return os.path.join(d, f)
    except Exception:
        return None
    return None
def _con_strip_ts(ln):
    if ln.startswith("["):
        b = ln.rfind("]")
        if b != -1:
            return ln[b + 1:]
    return ln
def _con_echo(cmd):
    lf = _con_logfile()
    unreal.log_flush()
    s0 = os.path.getsize(lf) if lf else 0
    unreal.SystemLibrary.execute_console_command(None, cmd)
    unreal.log_flush()
    if not lf:
        return []
    fh = open(lf, "rb"); fh.seek(s0); delta = fh.read().decode("utf-8", "replace"); fh.close()
    out = []
    for ln in delta.splitlines():
        p = _con_strip_ts(ln)
        s = p.strip()
        if not s:
            continue
        if "mcp_temp_script" in s or s.startswith("Cmd:") or s.startswith("LogMCP:") or s.startswith("LogPython:"):
            continue
        out.append(p)
    return out
'''

    # ------------------------------------------------------------------ #
    # execute_console_command — run an arbitrary console command (NON-rev) #
    # ------------------------------------------------------------------ #
    _EXEC_CMD_BODY = _EXEC_HELPERS + r'''
command = PARAMS["command"]
lines = _con_echo(command)
print("@@UMCP@@" + json.dumps({"status": "success", "command": command,
    "output_lines": lines, "line_count": len(lines),
    "note": ("Executed via SystemLibrary.execute_console_command; output is the delta captured from "
             "the editor Output Log around the call (timestamp prefixes stripped, MCP/script lines "
             "filtered). Many commands act silently and produce NO log output -- an empty output_lines "
             "does NOT mean the command failed. NON-reversible (no undo).")}))
'''

    @mcp.tool()
    def execute_console_command(ctx, command: str) -> str:
        """Execute an ARBITRARY Unreal console command and capture its Output-Log output.
        NON-reversible (NOT ledgered) -- an arbitrary console command has no faithful inverse.

        command: the full console command line, e.g. 'stat none', 'r.ScreenPercentage 80',
                 'obj list class=Texture2D', 'DumpConsoleCommands', 'Slate.EnableTooltips 1'.

        Runs the command via unreal.SystemLibrary.execute_console_command and returns the lines the
        engine appended to the Output Log around the call (the same log-delta capture console.py's cvar
        tools use; timestamp prefixes stripped, MCP/script noise filtered). Many commands act silently
        and produce no log output -- an empty output_lines is NOT an error.

        SAFETY: this is a general escape hatch with NO undo. Obviously-catastrophic shutdown commands
        ('quit' / 'exit') are REFUSED before anything reaches the editor (they would close the shared
        editor and hang every other agent); run those yourself outside the shared session. For a
        REVERSIBLE console-variable change, prefer console.set_console_variable (which captures the
        prior value and ledgers a 'set_cvar' inverse). This tool records NO ledger op (nothing to fold
        into editor_level.undo)."""
        cmd = (command or "").strip()
        if not cmd:
            return json.dumps({"status": "error", "message": "command is required"})
        first = cmd.split()[0].lower().lstrip("~")
        _BLOCKED = {"quit", "exit"}
        if first in _BLOCKED:
            return json.dumps({"status": "refused", "command": command,
                "message": ("refusing to execute '%s': it would shut down the shared editor and hang "
                            "every other agent. Run editor-shutdown commands yourself outside the "
                            "shared session. All other console commands are allowed." % first)}, indent=2)
        try:
            return json.dumps(_exec(_EXEC_CMD_BODY, {"command": cmd}), indent=2)
        except Exception as e:
            return f"Error: {e}"
