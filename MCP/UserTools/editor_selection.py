"""UserTools :: Editor / Selection  (spec: docs/spec/editor.md — "Discovery & selection")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8).
Commands run in the editor's interpreter via the plugin's `execute_python` handler,
which returns captured stdout under result["output"] (CRLF).

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines can't corrupt it.

Write safety (agent-scoped undo): each mutation runs inside an `unreal.ScopedEditorTransaction`
(atomic + editor-undoable) AND records an inverse op on a PER-SESSION agent ledger at
`builtins._UMCP_LEDGERS[session]` (persists across execute_python calls, which each get fresh
globals). The session id is injected via PARAMS["_session"] (from _exec; one per writer process),
so concurrent agents never pop each other's entries.

Selection inverse: every write captures the PRIOR selection (list of unique actor names) so a
future unified `undo` can restore the viewport selection exactly. This module deliberately does
NOT define an `undo` tool (editor_level.py owns that name); its ledger entries carry
{"op": ..., "prior_selection": [names...]} which the shared undo dispatch can invert by
re-selecting those actors.

Implemented:
  - get_selected_actors   (read-only)
  - select_actors         (write; ledgered; replace or additive)
  - deselect_all_actors   (write; ledgered)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied from editor_level.py) --------------------
# Wraps every snippet so it records the editor .log size before running and, in a
# finally, flushes + reads the appended bytes, surfacing new Warning/Error lines as
# @@UMCP_LOG@@ (attached to the result as "_log_warnings").
# NB: no ''' and no stray backslashes in this code (the handler wraps code in '''...''').
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes ('''...''')
# before exec, so any ''' or backslash in the code corrupts it. Pass all parameters as base64
# (its alphabet has no quote/backslash/triple-quote). Snippet bodies also avoid ''' / backslashes.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer so its undo ledger is isolated from other concurrent
    # writers. One value per OS process; override via utils["session"] if provided.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. Any new Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run the
        body in Unreal, and return its MARKER payload. Adds _session for ledger isolation."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to write bodies). No ''' / no backslashes.
    # Provides the session-aware _ledger() and _resolve_actor / _find_by_name so selection
    # writes can resolve actors by label/name and push reversible ledger entries.
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    # Per-session undo stack so concurrent agents never pop each other's entries.
    # Session id arrives in PARAMS["_session"] (injected by _exec from the bridge/harness).
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
def _find_by_name(uniq):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == uniq:
            return a
    return None
'''

    # ------------------------------------------------------------------ #
    # get_selected_actors — current viewport selection (read-only)        #
    # ------------------------------------------------------------------ #
    _GET_SELECTED_BODY = r'''
import unreal, json
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sel = eas.get_selected_level_actors() or []
out = []
for a in sel:
    if not a: continue
    out.append({
        "name":  _try(lambda: a.get_name()),
        "label": _try(lambda: a.get_actor_label()),
        "class": _try(lambda: a.get_class().get_name()),
    })
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(out), "actors": out}))
'''

    @mcp.tool()
    def get_selected_actors(ctx) -> str:
        """List the actors currently selected in the level viewport / World Outliner.
        Each entry has the actor's unique internal name, display label, and class.
        Read-only."""
        try:
            return json.dumps(_query(_GET_SELECTED_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # select_actors — select actors by label/name (ledgered write)        #
    # ------------------------------------------------------------------ #
    _SELECT_BODY = _COERCE_HELPERS + r'''
idents = PARAMS.get("actors") or []
additive = bool(PARAMS.get("additive"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
# Capture the PRIOR selection (unique names) as the ledger inverse.
prior = eas.get_selected_level_actors() or []
prior_names = []
for a in prior:
    if a:
        n = a.get_name()
        if n not in prior_names:
            prior_names.append(n)
resolved = []
resolved_names = []
not_found = []
for ident in idents:
    a = _resolve_actor(ident)
    if a is None:
        not_found.append(ident)
    else:
        n = a.get_name()
        if n not in resolved_names:
            resolved_names.append(n)
            resolved.append(a)
# Build the new selection set (additive extends the prior selection).
new_actors = []
new_names = []
if additive:
    for a in prior:
        if a:
            n = a.get_name()
            if n not in new_names:
                new_names.append(n); new_actors.append(a)
for a in resolved:
    n = a.get_name()
    if n not in new_names:
        new_names.append(n); new_actors.append(a)
with unreal.ScopedEditorTransaction("MCP select_actors"):
    eas.set_selected_level_actors(new_actors)
_ledger().append({"op": "select_actors", "prior_selection": prior_names})
after = eas.get_selected_level_actors() or []
after_out = [{"name": a.get_name(), "label": a.get_actor_label(),
              "class": a.get_class().get_name()} for a in after if a]
print("@@UMCP@@" + json.dumps({"status": "success", "additive": additive,
    "requested": idents, "not_found": not_found,
    "prior_selection": prior_names, "selected_count": len(after_out),
    "selected": after_out, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def select_actors(ctx, actors: list, additive: bool = False) -> str:
        """Select actors in the level viewport / World Outliner by label or unique name.

        actors:   list of actor display labels or unique internal names to select.
        additive: if True, ADD these to the current selection; if False (default),
                  REPLACE the current selection with exactly these actors.

        Idempotently de-duplicates; labels resolve first, then names. Unresolved
        identifiers are reported under 'not_found' (they don't abort the others).

        Ledgered write: the PRIOR selection (unique names) is captured so a unified
        undo can restore the viewport selection exactly."""
        params = {"actors": actors, "additive": additive}
        try:
            return json.dumps(_exec(_SELECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # deselect_all_actors — clear the selection (ledgered write)          #
    # ------------------------------------------------------------------ #
    _DESELECT_BODY = _COERCE_HELPERS + r'''
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
prior = eas.get_selected_level_actors() or []
prior_names = []
for a in prior:
    if a:
        n = a.get_name()
        if n not in prior_names:
            prior_names.append(n)
with unreal.ScopedEditorTransaction("MCP deselect_all_actors"):
    eas.set_selected_level_actors([])
_ledger().append({"op": "deselect_all_actors", "prior_selection": prior_names})
after = eas.get_selected_level_actors() or []
print("@@UMCP@@" + json.dumps({"status": "success",
    "prior_selection": prior_names, "prior_count": len(prior_names),
    "selected_count": len([a for a in after if a]),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def deselect_all_actors(ctx) -> str:
        """Clear the level viewport / World Outliner selection (select nothing).

        Ledgered write: the PRIOR selection (unique names) is captured so a unified
        undo can restore it."""
        try:
            return json.dumps(_exec(_DESELECT_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
