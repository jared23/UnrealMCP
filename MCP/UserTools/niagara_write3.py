"""UserTools :: Niagara / VFX compile + compile-driven inspect (WRITE3)  (spec: docs/spec/niagara.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). Third Niagara wave. Fills the
last spec features that ARE reachable from stock Python in this build: on-demand system COMPILE plus
the compile-driven INSPECT/VALIDATE tools (compile status + Output-Log diagnostic scan). Query
convention, base64 PARAMS injection, and Output-Log auto-capture are copied VERBATIM from the
gold-standard editor_level.py / niagara_write2.py / niagara_read2.py.

REACHABLE from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1) -- shipped here:
  * compile_niagara_system     -- unreal.MCPReflectionLibrary.compile_niagara_system(system): synchronous
                                  recompile of a NiagaraSystem. Fills the System-group PARTIAL (there was
                                  no on-demand compile that did not also mutate authored state). This is a
                                  NON-authoring, idempotent derived-data rebuild -> NO ledger entry (there
                                  is nothing to reverse; re-compiling from the same source is a no-op). It
                                  may mark the package dirty (compiled data), exactly like the editor's own
                                  Compile button; it does NOT save.
  * get_niagara_system_errors  -- runs ONE synchronous compile and surfaces the Niagara error/warning lines
                                  from the Output-Log delta (the same capture editor_level uses), filtered
                                  by severity. Read-oriented (compiles to force fresh diagnostics; no save,
                                  no ledger). The engine exposes NO python-readable compile-status property
                                  on UNiagaraSystem (probed: system has no compile-ish editor property), so
                                  a compile pass + log scan is the only stock-Python route -- stated in the
                                  payload, never hidden.
  * validate_niagara_graph     -- compile-based validity verdict {valid, compiled, error_count,
                                  warning_count}. Same internal compile+scan as get_niagara_system_errors,
                                  surfaced as a boolean pass/fail. Read-oriented; no save, no ledger.

Why these carry NO undo op: none of them writes authored/serialized content. compile regenerates derived
data only; the two inspect tools compile-to-read. They are reported to the coordinator as non-ledgered
(nothing to fold into editor_level.undo).

NOT reachable from stock Python here (reported to the coordinator for a batched C++ / NiagaraEditor round,
NOT faked): the module-stack READS (get_niagara_modules / get_niagara_module_inputs), graph READS
(get_niagara_graph_nodes / get_niagara_node_info / trace_niagara_connection), stack VALIDATE
(validate_niagara_stack / fix_niagara_stack_issue), and all graph/scratch-pad AUTHORING. Root cause
(probed live 2026-08-18): NiagaraFunctionLibrary.get_all_emitters returns a lightweight
`NiagaraMinimalEmitterInfo` struct exposing only emitter_name / is_enabled / used_renderer_materials|meshes
-- the emitter's script props, source graph, and module stack are NOT on it, and UNiagaraSystem exposes no
python-reachable emitter-handle / script-source accessor. Those live in NiagaraEditor C++.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never
assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the C++ wrapper's own names).
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

    # ----- Python-side diagnostic classification (shared by the inspect tools) -----
    def _classify(result, severity):
        """Split the captured Output-Log delta into Niagara error/warning issues, filtered by severity."""
        want = (severity or "all").lower()
        raw = result.get("_log_warnings") or []
        issues = []
        for ln in raw:
            low = ln.lower()
            # keep only Niagara-relevant compile diagnostics (shared editor may log unrelated lines)
            if "niagara" not in low and "compil" not in low:
                continue
            if ": Error:" in ln:
                sev = "error"
            elif ": Warning:" in ln:
                sev = "warning"
            else:
                continue
            if want != "all" and sev != want:
                continue
            issues.append({"severity": sev, "message": ln.strip()[:500]})
        err = sum(1 for i in issues if i["severity"] == "error")
        warn = sum(1 for i in issues if i["severity"] == "warning")
        return issues, err, warn

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _HELP = r'''
import unreal, json, warnings, gc
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load_system(path):
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, ("asset not found: %s" % path)
    if not isinstance(obj, unreal.NiagaraSystem):
        return None, ("asset is not a NiagaraSystem (got %s): %s" % (obj.get_class().get_name(), path))
    return obj, None
def _mrl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _compile(sysobj, wait):
    # One recompile per call (batching multiple recompiles in ONE execute_python wedges the game thread).
    mrl = _mrl()
    if mrl is None or not hasattr(mrl, "compile_niagara_system"):
        return None, "C++ handler compile_niagara_system unavailable (plugin DLL predates the niagara C++ wave; recompile needed)"
    raw = None
    try:
        raw = mrl.compile_niagara_system(sysobj, bool(wait))
    except TypeError:
        raw = mrl.compile_niagara_system(sysobj)
    try:
        res = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        res = {"raw": str(raw)[:400]}
    if isinstance(res, dict) and res.get("error"):
        return None, res.get("error")
    return res, None
'''

    # ------------------------------------------------------------------ #
    # compile_niagara_system                                              #
    # ------------------------------------------------------------------ #
    _COMPILE_BODY = _HELP + r'''
system_path = PARAMS["system_path"]; wait = bool(PARAMS.get("wait_for_completion", True))
sysobj, err = _load_system(system_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    res, cerr = _compile(sysobj, wait)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        print("@@UMCP@@" + json.dumps({"status": "success", "system": sysobj.get_path_name(),
            "compiled": bool(res.get("compiled")) if isinstance(res, dict) else None,
            "waited": bool(res.get("waited")) if isinstance(res, dict) else None,
            "note": "On-demand synchronous recompile (MCPReflectionLibrary.compile_niagara_system). "
                    "Non-authoring / idempotent: rebuilds derived compiled data only, does NOT change "
                    "authored source and does NOT save (may mark the package dirty, like the editor's "
                    "Compile button). No undo entry (nothing to reverse)."}))
gc.collect()
'''

    @mcp.tool()
    def compile_niagara_system(ctx, system_path: str, wait_for_completion: bool = True) -> str:
        """Recompile a NiagaraSystem on demand (synchronous). Fills the spec System-group compile feature.

        system_path:         NiagaraSystem asset path (e.g. '/Game/VFX/NS_Foo.NS_Foo').
        wait_for_completion: request a synchronous compile (the C++ handler compiles + waits; the response
                             carries the real 'waited' flag).

        Uses unreal.MCPReflectionLibrary.compile_niagara_system. This is a NON-authoring, idempotent
        derived-data rebuild -- it does not change authored source and does not save (it may leave the
        package dirty with fresh compiled data, exactly like the editor Compile button). Therefore it
        pushes NO undo op (there is nothing to reverse). ONE recompile per call by design (batching
        recompiles in a single call wedges the game thread). Returns {compiled, waited}."""
        params = {"system_path": system_path, "wait_for_completion": wait_for_completion}
        try:
            return json.dumps(_exec(_COMPILE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_niagara_system_errors                                           #
    # ------------------------------------------------------------------ #
    _ERRORS_BODY = _HELP + r'''
system_path = PARAMS["system_path"]
sysobj, err = _load_system(system_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    res, cerr = _compile(sysobj, True)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        # The Output-Log delta (error/warning lines) is captured by the _wrap try/finally and returned
        # to the Python layer as _log_warnings; classification/severity-filter happens there.
        print("@@UMCP@@" + json.dumps({"status": "success", "system": sysobj.get_path_name(),
            "compiled": bool(res.get("compiled")) if isinstance(res, dict) else None}))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_system_errors(ctx, system_path: str, severity: str = "all") -> str:
        """Report a NiagaraSystem's compile diagnostics (errors / warnings). Read-oriented (compiles to
        force fresh diagnostics; no save, no ledger).

        system_path: NiagaraSystem asset path.
        severity:    'all' (default) | 'error' | 'warning' -- filters the returned issues.

        The engine exposes no python-readable compile-status property on UNiagaraSystem, so this runs ONE
        synchronous compile and scans the Output-Log delta for Niagara error/warning lines (the same
        capture editor_level uses). Returns {compiled, error_count, warning_count, issues:[{severity,
        message}]}. A clean system returns an empty issue list. Diagnostics are Output-Log-derived and
        filtered to Niagara-related lines -- best-effort, and SAID SO in the payload."""
        params = {"system_path": system_path, "severity": severity}
        try:
            result = _exec(_ERRORS_BODY, params)
            if isinstance(result, dict) and result.get("status") == "success":
                issues, err, warn = _classify(result, severity)
                result["issues"] = issues
                result["error_count"] = err
                result["warning_count"] = warn
                result["severity_filter"] = (severity or "all").lower()
                result["note"] = ("Diagnostics are derived from the Output-Log delta during ONE "
                                  "synchronous compile and filtered to Niagara-related lines (no "
                                  "python-readable compile-status property exists); best-effort. An "
                                  "empty issues list means the compile logged no Niagara errors/warnings.")
                result.pop("_log_warnings", None)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_niagara_graph                                              #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def validate_niagara_graph(ctx, system_path: str) -> str:
        """Validate a NiagaraSystem's graph by compiling it and reporting a pass/fail verdict. Read-oriented
        (compiles to validate; no save, no ledger).

        system_path: NiagaraSystem asset path.

        Runs ONE synchronous compile and returns {valid, compiled, error_count, warning_count, issues}.
        'valid' is True when the system compiled and no Niagara error lines were logged. Same
        compile+Output-Log-scan route as get_niagara_system_errors (the module stack/graph itself is not
        python-reachable in this build -- validate_niagara_stack / graph-node reads need C++). Best-effort;
        SAID SO in the payload."""
        params = {"system_path": system_path, "severity": "all"}
        try:
            result = _exec(_ERRORS_BODY, params)
            if isinstance(result, dict) and result.get("status") == "success":
                issues, err, warn = _classify(result, "all")
                compiled = bool(result.get("compiled"))
                result["issues"] = issues
                result["error_count"] = err
                result["warning_count"] = warn
                result["valid"] = bool(compiled and err == 0)
                result["note"] = ("Validity = compiled AND no Niagara error lines in the Output-Log delta "
                                  "during a synchronous compile (no python-readable compile-status property "
                                  "exists); best-effort. Stack-level validation (validate_niagara_stack) and "
                                  "graph-node reads are NiagaraEditor-only and need a C++ handler.")
                result.pop("_log_warnings", None)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error: {e}"
