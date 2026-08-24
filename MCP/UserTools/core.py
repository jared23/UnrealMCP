"""UserTools :: Core (bridge/editor health + tool-schema introspection)

Clean-room module over Unreal's public Python API (UE 5.8) + the MCP bridge's own tool registry.
Two cheap, READ-ONLY diagnostics that do not touch the undo ledger:

  - health_check()        -> one tiny execute_python round-trip that proves the whole chain is live:
                             bridge_connected (socket round-trip ok), python_plugin (execute_python
                             ran), editor_alive, plus engine_version + editor_world. Cheapest win.
  - dump_command_schema() -> enumerate the MCP tools REGISTERED on this bridge and their signatures.
                             NOT an execute_python snippet: it introspects the `mcp` object handed to
                             register_tools. In the real bridge that is the _ToolShim wrapper around an
                             mcp MCPServer; the flushed tools live in mcp._real._tool_manager._tools as
                             Tool objects carrying {name, description, parameters(JSON schema)}. Pending
                             (not-yet-flushed) registrations are read from the shim's _captured list by
                             introspecting each function's signature. Best-effort with a source note if
                             the registry is not cleanly reachable.

Neither tool mutates anything, wraps a ScopedEditorTransaction, or records a ledger op -- there is
NOTHING for the coordinator to fold into editor_level.undo.

HARD CONSTRAINTS honored: the health_check snippet body contains NO triple-single-quotes and NO stray
backslashes; params travel as base64 JSON via _exec; no reserved local names are assigned.
"""
import json
import base64
import textwrap
import os
import inspect

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


def _tool_records_from_mcp(mcp):
    """Best-effort introspection of the tools registered on the bridge `mcp` object.
    Returns (records, sources). records: list of {name, description, params, [schema]}.
    sources: list of the registry access paths that yielded tools (for the response note)."""
    records = {}
    sources = []

    def _params_from_schema(schema):
        schema = schema or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out = []
        for pn, pv in props.items():
            pv = pv if isinstance(pv, dict) else {}
            out.append({"name": pn, "type": pv.get("type"),
                        "required": pn in required,
                        "default": pv.get("default")})
        return out

    def _params_from_fn(fn):
        out = []
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            return out
        for p in sig.parameters.values():
            if p.name in ("ctx", "context", "self"):
                continue
            has_default = p.default is not inspect.Parameter.empty
            default = None
            if has_default:
                try:
                    json.dumps(p.default)
                    default = p.default
                except (TypeError, ValueError):
                    default = str(p.default)
            ann = None
            if p.annotation is not inspect.Parameter.empty:
                ann = getattr(p.annotation, "__name__", None) or str(p.annotation)
            out.append({"name": p.name, "type": ann,
                        "required": not has_default, "default": default})
        return out

    # 1) The flushed real registry: mcp(._real)._tool_manager._tools -> Tool objects with a JSON schema.
    server = getattr(mcp, "_real", None) or mcp
    tm = getattr(server, "_tool_manager", None)
    if tm is not None:
        tools = None
        try:
            tools = tm.list_tools()
        except Exception:
            tools = None
        if tools is None:
            tdict = getattr(tm, "_tools", None)
            tools = list(tdict.values()) if isinstance(tdict, dict) else None
        if tools:
            sources.append("mcp._real._tool_manager (flushed Tool registry, full JSON schema)")
            for t in tools:
                nm = getattr(t, "name", None)
                if not nm:
                    continue
                schema = getattr(t, "parameters", None)
                rec = {"name": nm,
                       "description": (getattr(t, "description", None) or "").strip(),
                       "params": _params_from_schema(schema)}
                if isinstance(schema, dict):
                    rec["schema"] = schema
                records[nm] = rec

    # 2) Pending (not-yet-flushed) registrations on the shim: _captured = [(fn, name, description), ...].
    captured = getattr(mcp, "_captured", None)
    if isinstance(captured, (list, tuple)) and captured:
        added = 0
        for item in captured:
            try:
                fn = item[0]; nm = item[1]; desc = item[2]
            except (TypeError, IndexError):
                continue
            nm = nm or getattr(fn, "__name__", None)
            if not nm or nm in records:
                continue
            records[nm] = {"name": nm,
                           "description": ((desc or getattr(fn, "__doc__", "") or "").strip()),
                           "params": _params_from_fn(fn)}
            added += 1
        if added:
            sources.append("mcp._captured (pending pre-flush registrations, signature-derived params)")

    return list(records.values()), sources


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
    # health_check — prove bridge + python plugin + editor are alive       #
    # ------------------------------------------------------------------ #
    _HEALTH_BODY = r'''
import unreal, json
ver = None
try:
    ver = unreal.SystemLibrary.get_engine_version()
except Exception:
    ver = None
world = None
try:
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    w = ues.get_editor_world()
    world = w.get_name() if w else None
except Exception:
    world = None
content_dir = None
try:
    content_dir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
except Exception:
    content_dir = None
print("@@UMCP@@" + json.dumps({"status": "success", "engine_version": ver,
    "editor_world": world, "project_content_dir": content_dir}))
'''

    @mcp.tool()
    def health_check(ctx) -> str:
        """Quick liveness check of the whole MCP chain. Read-only, no side effects.

        Runs a tiny execute_python round-trip. If it succeeds, the full path is proven live:
          bridge_connected  -> the socket round-trip to the C++ MCP server completed
          python_plugin     -> the editor's Python plugin ran the snippet
          editor_alive      -> the editor process is up and its game thread serviced the call
        and it also returns engine_version, editor_world (the loaded level's world name), and the
        project content dir. If the round-trip fails/times out, returns bridge_connected=false with
        the error (nothing is assumed alive). This is the cheapest way to confirm the editor + bridge
        are ready before running a heavier batch."""
        try:
            r = _exec(_HEALTH_BODY, {})
            ok = isinstance(r, dict) and r.get("status") == "success"
            out = {"status": "success" if ok else "error",
                   "bridge_connected": True, "python_plugin": bool(ok),
                   "editor_alive": bool(ok),
                   "engine_version": r.get("engine_version") if ok else None,
                   "editor_world": r.get("editor_world") if ok else None,
                   "project_content_dir": r.get("project_content_dir") if ok else None}
            if isinstance(r, dict) and r.get("_log_warnings"):
                out["_log_warnings"] = r["_log_warnings"]
            return json.dumps(out, indent=2)
        except Exception as e:
            return json.dumps({"status": "error", "bridge_connected": False,
                "python_plugin": False, "editor_alive": False,
                "message": "health round-trip failed: %s" % str(e)[:200]}, indent=2)

    # ------------------------------------------------------------------ #
    # dump_command_schema — enumerate registered MCP tools + signatures    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def dump_command_schema(ctx, filter: str = "", include_schema: bool = False,
                            max_results: int = None) -> str:
        """Enumerate the MCP tools registered on THIS bridge and their signatures. Read-only.

        filter:         case-insensitive substring on the tool name (e.g. 'niagara', 'spawn'). Empty=all.
        include_schema: include each tool's full JSON-schema 'parameters' object (verbose). Default
                        False -> a compact params list [{name, type, required, default}] per tool.
        max_results:    optional cap on the number of tools returned (count still reflects all matches).

        NOT an execute_python call: this introspects the bridge's own tool registry. In the live bridge
        the `mcp` object is a _ToolShim over an mcp MCPServer, whose flushed tools live in
        mcp._real._tool_manager as Tool objects carrying {name, description, parameters(JSON schema)};
        this reads those directly. It also merges any pending pre-flush registrations from the shim's
        _captured list (params derived from the function signature). The response's 'sources' lists
        which access paths yielded tools; if none are reachable it returns an empty list with a note
        (best-effort, not faked)."""
        try:
            records, sources = _tool_records_from_mcp(mcp)
            flt = (filter or "").lower()
            if flt:
                records = [r for r in records if flt in r["name"].lower()]
            records.sort(key=lambda r: r["name"])
            total = len(records)
            if max_results:
                records = records[:int(max_results)]
            if not include_schema:
                for r in records:
                    r.pop("schema", None)
            note = None
            if not sources:
                note = ("could not reach the bridge tool registry from the mcp object (neither "
                        "mcp._real._tool_manager nor a _captured list was found). This can happen if "
                        "dump_command_schema is invoked outside the normal bridge (e.g. a bare mock "
                        "mcp). Best-effort empty list.")
            return json.dumps({"status": "success", "tool_count_total": total,
                "returned": len(records), "filter": filter or None,
                "sources": sources, "note": note, "tools": records}, indent=2)
        except Exception as e:
            return json.dumps({"status": "error",
                "message": "dump_command_schema introspection failed: %s" % str(e)[:200]}, indent=2)
