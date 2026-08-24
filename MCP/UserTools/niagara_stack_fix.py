"""UserTools :: Niagara stack-issue list + auto-fix (C++ #50).

get_niagara_stack_issues / fix_niagara_stack_issue.

Niagara "stack issues" are the auto-fixable validation errors in the emitter/system stack. The C++ handlers
(MCPReflectionLibrary.get_niagara_stack_issues_json / fix_niagara_stack_issue_json) reach the editor's stack
view-model(s) via TObjectIterator once the system editor is open, and execute the fix delegates (the same thing
the UI "Fix issue" button does). fix is a repair op -> NON-LEDGERED (a fix delegate does arbitrary graph surgery;
not generically invertible). Each tool auto-opens the system editor in a separate bridge call so the stack
view-model gets a tick to build before we look for it. Resolve-guarded: inert until the DLL lands, then auto-enables.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"


def _wrap(code):
    return code


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": code})
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

    _OPEN_BODY = r'''
import unreal, json
sp = PARAMS.get("system_path")
sysobj = unreal.load_asset(sp) if sp else None
if sysobj is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load NiagaraSystem: %s" % sp}))
elif not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a NiagaraSystem: %s" % sp}))
else:
    try:
        unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).open_editor_for_assets([sysobj])
        print("@@UMCP@@" + json.dumps({"status": "success"}))
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "open_editor failed: %s" % _e}))
'''

    _LIST_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "get_niagara_stack_issues_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "get_niagara_stack_issues_json not built"}))
else:
    raw = fn(PARAMS.get("system_path"))
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _FIX_BODY = r'''
import unreal, json
M = getattr(unreal, "MCPReflectionLibrary", None)
fn = getattr(M, "fix_niagara_stack_issue_json", None) if M is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True, "message": "fix_niagara_stack_issue_json not built"}))
else:
    raw = fn(PARAMS.get("system_path"), PARAMS.get("issue_identifier") or "",
             int(PARAMS.get("fix_index", 0)), bool(PARAMS.get("fix_all", True)))
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    def _ensure_open(system_path):
        return _exec(_OPEN_BODY, {"system_path": system_path})

    @mcp.tool()
    def get_niagara_stack_issues(ctx, system_path: str) -> str:
        """List a Niagara system's editor stack issues (the emitter/system-stack validation notes,
        warnings and errors), including which ones are auto-fixable. Opens the system editor if needed.

        system_path: content path to the UNiagaraSystem.

        Each issue: {entry, severity (error/warning/info), short/long_description, identifier, entry_key,
        can_be_dismissed, fix_count, fixes:[{description, style, is_valid}]}. Pass a fixable issue's
        identifier (or fix_all=true) to fix_niagara_stack_issue."""
        try:
            o = _ensure_open(system_path)
            if o.get("status") != "success":
                return json.dumps(o, indent=2)
            return json.dumps(_exec(_LIST_BODY, {"system_path": system_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def fix_niagara_stack_issue(ctx, system_path: str, issue_identifier: str = None, fix_index: int = 0,
                               fix_all: bool = True) -> str:
        """Apply the auto-fix for a Niagara stack issue (the same action as the editor's "Fix issue"
        button). Opens the system editor if needed, executes the fix delegate(s), refreshes the stack,
        and reports issues_before/issues_after (self-verifying). Repair op -> NOT ledgered.

        system_path:     content path to the UNiagaraSystem (SHOULD be under /Game/... -- avoid engine content).
        issue_identifier: target a specific issue by its 'identifier' (from get_niagara_stack_issues).
                          Many Niagara issues have an EMPTY identifier -> use fix_all instead.
        fix_index:       which fix to apply when an issue has multiple fixes (default 0).
        fix_all:         apply every fixable ('fix'-style) issue in the stack (default True). This is the
                         robust path since issue identifiers are often empty.

        Returns {fixes_attempted, fixes_failed, issues_before, issues_after, fixable_before, fixable_after,
        applied:[{entry, fix_description, executed}]}."""
        try:
            o = _ensure_open(system_path)
            if o.get("status") != "success":
                return json.dumps(o, indent=2)
            return json.dumps(_exec(_FIX_BODY, {"system_path": system_path, "issue_identifier": issue_identifier,
                                                "fix_index": fix_index, "fix_all": fix_all}), indent=2)
        except Exception as e:
            return f"Error: {e}"
