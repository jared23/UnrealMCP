"""UserTools :: Console (variables + commands)  (spec: docs/spec/console.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Console-variable /
console-object reads + ONE reversible cvar set. Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py / curves_write.py.

PROBE FINDINGS (live vs TestMCPSetup, UE 5.8.1) -- what the console API IS / ISN'T reachable:
  REACHABLE (read-only, no execution):
    * unreal.SystemLibrary.get_console_variable_{float,int,bool,string}_value(name)
        -> pure IConsoleManager::FindConsoleVariable reads; return 0/""/False if the name is
           NOT a registered cvar (so a NON-EMPTY string value is a safe, no-side-effect
           existence signal that never executes a command).
    * unreal.ConsoleVariablesEditorFunctionLibrary.get_console_variable_source_by_name(name)
        -> the cvar's LastSetBy provenance ("Scalability"/"Constructor"/... ; "None" when the
           name is unregistered OR only at its code default).
  REACHABLE via the engine echoing to the Output Log (we snapshot the .log delta around the call):
    * execute_console_command(None, "<cvar>")       -> logs  <name> = "<value>"   LastSetBy: <src>
    * execute_console_command(None, "<cvar> ?")     -> logs  HELP for '<name>': <help...> HISTORY
                                                       <src>: <value> ...   <name> = "<value>" ...
      Querying a cvar (bare or with ?) is READ-ONLY -- it never changes the value (verified) --
      so we only ever echo a name AFTER the read-only string getter has confirmed it is a
      registered cvar. We NEVER blind-execute an arbitrary name (that could run a destructive
      exec command like Quit).
  NOT REACHABLE from stock Python (documented, NOT faked):
    * A COMPLETE cvar enumeration. IConsoleManager::ForEachConsoleObjectThatContains is not bound.
      `DumpConsoleCommands` dumps a name list to the log but it is INCOMPLETE for cvars (e.g.
      r.ShadowQuality is absent from the dump in this build) -- so list_console_objects is
      offered as a best-effort discovery aid, honestly labelled as the engine's DumpConsoleCommands
      output (registered exec commands + a SUBSET of cvars; NOT the full cvar set).
    * Existence checking of pure EXEC COMMANDS (stat/show/FSelfRegisteringExec) without running
      them -- there is no read-only "does this command exist" API; probing by execution has side
      effects. console_variable_exists therefore detects CVARS only (refused to fake command probing).
    * Structured min/max metadata -- ranges only appear inside the free-text help ("0..5"); we
      return the raw help lines rather than fabricating parsed min/max.
    * A general, REVERSIBLE `execute_console_command` (run ANY command) -- an arbitrary command has
      no faithful inverse (e.g. `Quit`, `stat unit`), so per PROTOCOL it is NOT shipped here. Only
      the reversible cvar SET is a write; everything else is read-only.

Implemented (all validated live, session=agent process, editor left CLEAN, ledger depth 0):
  - get_console_variable    (read-only; value 4 ways + set_by + default + help + history)
  - console_variable_exists (read-only; cvar existence, no execution)
  - list_console_objects    (read-only; DumpConsoleCommands dump, prefix-filtered; incomplete-cvar note)
  - set_console_variable    (WRITE; reversible; ledgered op "set_cvar")

Undo: this module registers NO `undo` tool (editor_level.py owns the unified `undo`). set_console_variable
pushes a NEW ledger op "set_cvar" {name, prior_value, prior_set_by} whose inverse re-sets the cvar to
prior_value -- reported to the coordinator to fold into editor_level.undo.
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

    # Shared Unreal-side console helpers (prepended to bodies). No triple-single-quote / no backslash.
    #   _ledger()          -> per-session undo stack (copied verbatim from editor_level.py).
    #   _con_logfile()     -> path of the live editor .log.
    #   _con_strip_ts(ln)  -> strip a leading [timestamp][frame] prefix from a log line.
    #   _con_echo(cmd)     -> snapshot the .log, run a console command, return the cleaned delta lines.
    #                          ONLY call for names already confirmed to be registered cvars (read-only).
    #   _con_typed(name)   -> {string,float,int,bool} typed reads via SystemLibrary (pure, no execution).
    #   _con_source(name)  -> LastSetBy provenance via ConsoleVariablesEditorFunctionLibrary (or None).
    #   _con_is_cvar(name) -> read-only existence signal: typed string value is non-empty.
    #   _con_parse(name,lines) -> parse a "<name> ? " echo into {value,set_by,help,history,default}.
    _CON_HELPERS = r'''
import unreal, json, builtins, os
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
def _con_typed(name):
    sl = unreal.SystemLibrary
    d = {}
    try: d["string"] = sl.get_console_variable_string_value(name)
    except Exception: d["string"] = None
    try: d["float"] = sl.get_console_variable_float_value(name)
    except Exception: d["float"] = None
    try: d["int"] = sl.get_console_variable_int_value(name)
    except Exception: d["int"] = None
    try: d["bool"] = sl.get_console_variable_bool_value(name)
    except Exception: d["bool"] = None
    return d
def _con_source(name):
    cvefl = getattr(unreal, "ConsoleVariablesEditorFunctionLibrary", None)
    if cvefl is None:
        return None
    try:
        v = cvefl.get_console_variable_source_by_name(name)
        s = str(v)
        return None if s in ("None", "") else s
    except Exception:
        return None
def _con_is_cvar(name):
    try:
        sv = unreal.SystemLibrary.get_console_variable_string_value(name)
    except Exception:
        sv = ""
    return (sv is not None and sv != "")
def _con_parse(name, lines):
    value = None; set_by = None; help_lines = []; history = {}
    in_help = False; in_hist = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("HELP for"):
            in_help = True; in_hist = False; continue
        if s == "HISTORY":
            in_help = False; in_hist = True; continue
        # the canonical value line:  <name> = "<value>"      LastSetBy: <src>
        if " = " in s and s.split(" = ", 1)[0].strip() == name:
            part = s.split(" = ", 1)[1]
            if "LastSetBy:" in part:
                vp, sp = part.split("LastSetBy:", 1)
                set_by = sp.strip()
                value = vp.strip().strip('"').strip()
            else:
                value = part.strip().strip('"')
            in_help = False; in_hist = False
            continue
        if in_help:
            if s:
                help_lines.append(s)
        elif in_hist:
            if ":" in s:
                k, v = s.split(":", 1)
                history[k.strip()] = v.strip()
    return {"value": value, "set_by": set_by, "help": help_lines, "history": history,
            "default": history.get("Constructor")}
'''

    # ------------------------------------------------------------------ #
    # get_console_variable — full read of one cvar (read-only)            #
    # ------------------------------------------------------------------ #
    _GET_CVAR_BODY = _CON_HELPERS + r'''
name = PARAMS["name"]
typed = _con_typed(name)
source = _con_source(name)
registered = _con_is_cvar(name)
result = {"status": "success", "name": name, "exists": registered,
          "value_typed": typed, "set_by_source": source}
if not registered:
    result["note"] = ("not a readable console variable: the read-only string getter is empty. "
                      "It may be an unregistered name, a pure exec command, or (rarely) a genuinely "
                      "empty-string cvar. No console execution was attempted (safety: an arbitrary "
                      "name could be a destructive command).")
else:
    parsed = _con_parse(name, _con_echo(name + " ?"))
    canonical = parsed.get("value")
    if canonical is None:
        canonical = typed.get("string")
    result["value"] = canonical
    result["set_by"] = parsed.get("set_by") or source
    result["default"] = parsed.get("default")
    result["help"] = parsed.get("help")
    result["history"] = parsed.get("history")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_console_variable(ctx, name: str) -> str:
        """Read a console variable (cvar) in full. Read-only, no side effects.

        name: exact cvar name, e.g. 'r.ScreenPercentage', 't.MaxFPS', 'r.ShadowQuality'.

        Returns: exists (bool), value (canonical string as the engine reports it), value_typed
        (the same cvar read four ways: string/float/int/bool), set_by (LastSetBy provenance, e.g.
        'Scalability'/'Constructor'/'Console'), default (the Constructor value from the cvar HISTORY),
        help (the cvar's description lines; any value range appears here as free text), and history
        (per-source value stack).

        Existence is decided by the read-only typed string getter (never executes anything). Value/
        set_by/default/help are gathered by asking the engine to echo '<name> ?' to the Output Log,
        which we do ONLY after existence is confirmed (querying a cvar is read-only). If the name is
        not a registered cvar, exists=false and NO console command is executed (an arbitrary name
        could be a destructive exec command, so we never blind-run it)."""
        try:
            return json.dumps(_exec(_GET_CVAR_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # console_variable_exists — read-only cvar existence check            #
    # ------------------------------------------------------------------ #
    _CVAR_EXISTS_BODY = _CON_HELPERS + r'''
name = PARAMS["name"]
registered = _con_is_cvar(name)
result = {"status": "success", "name": name, "exists": registered,
          "detects": "console variables only (not pure exec commands)"}
if registered:
    typed = _con_typed(name)
    result["value"] = typed.get("string")
    result["set_by_source"] = _con_source(name)
else:
    result["note"] = ("read-only string getter is empty. This means NOT a registered cvar; it may "
                      "still be an exec command (stat/show/FSelfRegisteringExec) -- those cannot be "
                      "existence-checked without executing them, so are reported here as not-a-cvar.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def console_variable_exists(ctx, name: str) -> str:
        """Check whether a name is a registered console VARIABLE (cvar). Read-only, no execution.

        name: exact cvar name to test.

        Uses the read-only typed string getter (non-empty => the cvar is registered). Returns exists
        plus, when it exists, the current value and set-by source.

        Limitation (not faked): this detects CVARS only. Pure exec commands (stat/show/quit/
        FSelfRegisteringExec) have no read-only existence API -- probing them means running them, which
        has side effects -- so a command name returns exists=false with an explanatory note rather than
        a risky execution."""
        try:
            return json.dumps(_exec(_CVAR_EXISTS_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_console_objects — best-effort discovery via DumpConsoleCommands #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _CON_HELPERS + r'''
prefix = PARAMS.get("prefix")
max_results = int(PARAMS.get("max_results") or 200)
annotate_values = bool(PARAMS.get("annotate_values"))
lines = _con_echo("DumpConsoleCommands")
# DumpConsoleCommands prints, after a "DumpConsoleCommands: <filter>" / "Cmd: <filter>" header, one
# bare console-object name per line. We take names as the cleaned lines that are single tokens.
names = []
for ln in lines:
    s = ln.strip()
    if not s or " " in s:
        continue
    if s.startswith("DumpConsoleCommands") or s.startswith("HELP for") or s == "HISTORY":
        continue
    names.append(s)
# de-dup, keep order
seen = set(); uniq = []
for n in names:
    if n not in seen:
        seen.add(n); uniq.append(n)
total_dumped = len(uniq)
if prefix:
    pl = prefix.lower()
    uniq = [n for n in uniq if n.lower().startswith(pl)]
matched = len(uniq)
truncated = matched > max_results
window = uniq[:max_results]
if annotate_values:
    items = []
    for n in window:
        if _con_is_cvar(n):
            items.append({"name": n, "is_cvar": True, "value": _con_typed(n).get("string")})
        else:
            items.append({"name": n, "is_cvar": False})
    payload_names = items
else:
    payload_names = window
print("@@UMCP@@" + json.dumps({"status": "success",
    "source": "engine DumpConsoleCommands dump",
    "incomplete_cvars": True,
    "note": ("This is the engine's DumpConsoleCommands output: registered exec commands plus a SUBSET "
             "of cvars. It is NOT a complete cvar enumeration (many r.*/sg.* cvars are omitted by the "
             "engine dump). Use get_console_variable / console_variable_exists to inspect a known cvar "
             "by exact name. Full cvar enumeration is not reachable from stock Python."),
    "total_dumped": total_dumped, "matched": matched, "returned": len(window),
    "truncated": truncated, "prefix": prefix, "objects": payload_names}))
'''

    @mcp.tool()
    def list_console_objects(ctx, prefix: str = None, max_results: int = 200,
                             annotate_values: bool = False) -> str:
        """List console objects the engine reports via DumpConsoleCommands (best-effort discovery).
        Read-only.

        prefix:          case-insensitive name prefix filter (e.g. 'r.', 'AssetManager.'). Optional.
        max_results:     cap the number of names returned (default 200).
        annotate_values: for each returned name, tag whether it resolves as a cvar (read-only) and, if
                         so, its current string value. Slower (one read per name); bound it with a
                         tight prefix + small max_results.

        IMPORTANT LIMITATION (documented, not faked): this returns the engine's DumpConsoleCommands
        dump, which lists registered exec COMMANDS plus only a SUBSET of cvars -- it is NOT a complete
        console-variable enumeration (many r.*/sg.* cvars are absent from the dump, and stock Python
        exposes no ForEachConsoleObject binding to enumerate them). For a known cvar, query it by exact
        name with get_console_variable. The response carries incomplete_cvars=true and a note."""
        params = {"prefix": prefix, "max_results": max_results, "annotate_values": annotate_values}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_console_variable — reversible cvar write (ledgered op "set_cvar")#
    # ------------------------------------------------------------------ #
    _SET_CVAR_BODY = _CON_HELPERS + r'''
name = PARAMS["name"]
new_value = PARAMS["value"]
if not _con_is_cvar(name):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": ("refusing to set '%s': not a registered console variable (read-only string getter "
                    "is empty). Only cvars are settable here -- an arbitrary name could be an exec "
                    "command, and 'name value' would EXECUTE it with side effects. Verify with "
                    "console_variable_exists first." % name)}))
else:
    prior_value = _con_typed(name).get("string")
    prior_set_by = _con_source(name)
    # Apply via the canonical console path. Setting a cvar is not an editor-transaction operation
    # (cvars are engine session state, not UObject edits), so there is no ScopedEditorTransaction to
    # wrap -- reversibility is provided entirely by the ledger inverse (re-set to prior_value).
    unreal.SystemLibrary.execute_console_command(None, name + " " + str(new_value))
    unreal.log_flush()
    after_value = _con_typed(name).get("string")
    after_set_by = _con_source(name)
    _ledger().append({"op": "set_cvar", "name": name,
                      "prior_value": prior_value, "prior_set_by": prior_set_by})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": name,
        "prior_value": prior_value, "prior_set_by": prior_set_by,
        "requested_value": str(new_value), "value": after_value, "set_by": after_set_by,
        "note": ("Reversible: undo re-sets the cvar to prior_value. The exact VALUE is restored "
                 "faithfully; the LastSetBy provenance tag becomes 'Console' after any programmatic "
                 "set/restore (engine-managed, not restorable via public API)."),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_console_variable(ctx, name: str, value) -> str:
        """Set a console variable to a new value. REVERSIBLE ledgered write.

        name:  exact cvar name (must be a registered cvar; refused otherwise -- see below).
        value: the new value (number or string; applied as text via the console, so '80', '1', 'true'
               all work per the cvar's type).

        Safety: the target is first confirmed to be a registered cvar with the read-only string getter.
        If it is not a cvar, the set is REFUSED (an arbitrary name could be an exec command, and
        'name value' would execute it). The prior value and set-by source are captured before applying.

        Ledgered write op 'set_cvar' {name, prior_value, prior_set_by}. Inverse (to fold into
        editor_level.undo): execute '<name> <prior_value>' -- re-sets the cvar to its captured prior
        value. cvars are session state, so restoring the prior value is a faithful revert; the engine's
        LastSetBy tag will read 'Console' after restore (provenance is engine-managed, not settable)."""
        try:
            return json.dumps(_exec(_SET_CVAR_BODY, {"name": name, "value": value}), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # ------------------------------------------------------------------ #
    # get_console_command_info / console_command_exists — cvar OR command #
    # Backed by MCPReflectionLibrary.get_console_object_info_json         #
    # (IConsoleManager::FindConsoleObject -> cvars AND registered exec    #
    # commands). Falls back to the cvar-only string getter until the      #
    # plugin DLL is rebuilt with MCPReflection_SmallCats.cpp.             #
    # ------------------------------------------------------------------ #
    _OBJ_INFO_BODY = _CON_HELPERS + r'''
name = PARAMS["name"]
rl = getattr(unreal, "MCPReflectionLibrary", None)
if rl is not None and hasattr(rl, "get_console_object_info_json"):
    try:
        info = json.loads(rl.get_console_object_info_json(name))
    except Exception as _e:
        info = {"error": "handler raised: %s" % _e}
    if isinstance(info, dict) and info.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "name": name, "message": info.get("error")}))
    else:
        info["status"] = "success"
        info["source"] = "IConsoleManager.FindConsoleObject (cvars + registered exec commands)"
        print("@@UMCP@@" + json.dumps(info))
else:
    # Fallback: no C++ handler yet -> cvar-only detection (registered exec COMMANDS not detectable here).
    registered = _con_is_cvar(name)
    out = {"status": "success", "name": name, "found": registered,
           "is_variable": registered, "is_command": False, "degraded": True,
           "note": ("MCPReflectionLibrary.get_console_object_info_json not present (rebuild the UnrealMCP "
                    "plugin DLL with MCPReflection_SmallCats.cpp). Falling back to the cvar-only string "
                    "getter: registered exec COMMANDS cannot be detected until the C++ handler lands.")}
    if registered:
        parsed = _con_parse(name, _con_echo(name + " ?"))
        out["current_value"] = parsed.get("value") or _con_typed(name).get("string")
        out["set_by"] = parsed.get("set_by") or _con_source(name)
        out["help"] = parsed.get("help")
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def get_console_command_info(ctx, name: str) -> str:
        """Metadata for ONE console object (cvar OR registered exec command) by exact name. Read-only.

        name: exact console-object name, e.g. 'r.ScreenPercentage' (cvar) or 'Slate.CrashReporterPreview'
              / a registered IConsoleCommand.

        Backed by unreal.MCPReflectionLibrary.get_console_object_info_json, which resolves the object via
        IConsoleManager::FindConsoleObject — so it now covers BOTH cvars and registered exec commands
        (the old cvar-only path missed commands). Returns: found, is_variable, is_command, help (the
        console object's GetHelp), flags (notable EConsoleVariableFlags: ReadOnly/Cheat/Scalability/...),
        is_enabled, and — for cvars only — current_value, set_by (LastSetBy provenance) and the
        is_bool/int/float/string type hints. No console execution occurs (pure lookup).

        Not detectable (documented, not faked): FSelfRegisteringExec / stat / show style commands are NOT
        IConsoleObjects, so found=false for those. Until the plugin DLL is rebuilt with
        MCPReflection_SmallCats.cpp this degrades to a cvar-only check (degraded=true in the payload)."""
        try:
            return json.dumps(_exec(_OBJ_INFO_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def console_command_exists(ctx, name: str) -> str:
        """Check whether a name is a registered console object — cvar OR exec command. Read-only, no run.

        name: exact console-object name to test.

        Existence = IConsoleManager::FindConsoleObject(name) != null (via the C++ handler), which covers
        registered cvars AND registered IConsoleCommands. Returns exists, is_variable, is_command.

        Limitation (not faked): FSelfRegisteringExec / stat / show style commands are not console objects
        and cannot be existence-checked without running them, so they report exists=false. Until the
        plugin DLL is rebuilt with MCPReflection_SmallCats.cpp this degrades to a cvar-only check
        (degraded=true; registered exec commands then also report exists=false)."""
        try:
            info = _exec(_OBJ_INFO_BODY, {"name": name})
            slim = {"status": info.get("status", "success"), "name": name,
                    "exists": bool(info.get("found")),
                    "is_variable": bool(info.get("is_variable")),
                    "is_command": bool(info.get("is_command"))}
            if info.get("degraded"):
                slim["degraded"] = True
                slim["note"] = info.get("note")
            if info.get("_log_warnings"):
                slim["_log_warnings"] = info.get("_log_warnings")
            return json.dumps(slim, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. set_console_variable
    # adds ONE new ledger op "set_cvar" {name, prior_value, prior_set_by}; inverse = execute
    # "<name> <prior_value>". Reported to the coordinator to fold into editor_level.undo.
