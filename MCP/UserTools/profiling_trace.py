"""UserTools :: Profiling / Unreal Insights trace control  (spec: docs/spec/profiling.md)

Clean-room module over Unreal's public Python API (UE 5.8). Companion to profiling.py: it drives the
engine's Unreal Insights trace system through its Trace.* console commands (via
unreal.SystemLibrary.execute_console_command) and captures each command's Output-Log delta -- the SAME
log-capture mechanism profiling.py's stat-backed reads use.

This lives in a SEPARATE module (not folded into profiling.py) so the large gold profiling.py is left
untouched and there is no write-collision with any agent editing it.

Tools (all NON-ledgered runtime commands -- trace state is engine session state, not a UObject edit, so
there is nothing to reverse and NOTHING for the coordinator to fold into editor_level.undo):
  - performance_start_trace(channels_or_preset, file_path)  -> Trace.Enable <ch> + Trace.File <path>
  - performance_stop_trace()                                -> Trace.Stop
  - performance_toggle_channel(channel, enable)             -> Trace.Enable / Trace.Disable <channel>
  - performance_trace_bookmark(name)                        -> Trace.Bookmark <name>
  - performance_trace_snapshot(file_path)                   -> Trace.Snapshot / Trace.SnapshotFile <path>
  - performance_list_channels()                             -> Trace.Status (+ enabled/available parse)

start_trace and stop_trace are a PAIR: start_trace opens a .utrace file destination and enables the
requested channels (this adds real capture overhead and grows a file on disk until stopped); call
stop_trace to end it. A meaningful .utrace needs the trace to run for a while (frames must elapse);
these tools confirm the commands execute and Trace.Status reports the connection -- they do not by
themselves produce a long capture.

HARD CONSTRAINTS honored: snippet bodies contain NO triple-single-quotes and NO stray backslashes; all
params travel as base64 JSON via _exec; no reserved local names are assigned.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py / profiling.py) ---
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

# Named channel presets for performance_start_trace's channels_or_preset (case-insensitive).
_PRESETS = {
    "default": "cpu,gpu,frame,bookmark,log",
    "cpu": "cpu,frame,bookmark,log",
    "gpu": "gpu,frame,bookmark,log",
    "memory": "memalloc,memtag,callstack,module,metadata,frame,bookmark",
    "loadtime": "loadtime,assetloadtime,frame,bookmark,log",
    "animation": "animation,cpu,frame,bookmark,log",
    "niagara": "niagara,cpu,gpu,frame,bookmark,log",
}


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

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash.
    #   _tlogfile()      -> live editor .log path.
    #   _tstrip(ln)      -> drop the [timestamp][frame] prefix.
    #   _techo(cmd)      -> snapshot .log, run a console command, return the cleaned appended lines.
    #   _content(ln)     -> strip a leading "LogConsoleResponse: Display:" prefix from a status line.
    #   _split_ch(s)     -> split a comma/space channel list into a clean token list.
    #   _parse_status(lines) -> {connection, memory_used, enabled_channels, available_channels}.
    _TRACE_HELPERS = r'''
import unreal, json, os
def _tlogfile():
    try:
        d = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
        for f in os.listdir(d):
            if f.endswith(".log") and "-backup-" not in f:
                return os.path.join(d, f)
    except Exception:
        return None
    return None
def _tstrip(ln):
    if ln.startswith("["):
        b = ln.rfind("]")
        if b != -1:
            return ln[b + 1:]
    return ln
def _techo(cmd):
    lf = _tlogfile()
    unreal.log_flush()
    s0 = os.path.getsize(lf) if lf else 0
    unreal.SystemLibrary.execute_console_command(None, cmd)
    unreal.log_flush()
    if not lf:
        return []
    fh = open(lf, "rb"); fh.seek(s0); delta = fh.read().decode("utf-8", "replace"); fh.close()
    out = []
    for ln in delta.splitlines():
        p = _tstrip(ln)
        s = p.strip()
        if not s:
            continue
        if "mcp_temp_script" in s or s.startswith("Cmd:") or s.startswith("LogMCP:") or s.startswith("LogPython:"):
            continue
        out.append(p)
    return out
def _content(ln):
    s = ln
    if "LogConsoleResponse: Display:" in s:
        s = s.split("LogConsoleResponse: Display:", 1)[1]
    return s.strip()
def _split_ch(s):
    parts = []
    for tok in s.replace(",", " ").split():
        t = tok.strip()
        if t:
            parts.append(t)
    return parts
def _parse_status(lines):
    connection = None; memory = None
    enabled = []; available = []
    section = None
    for raw in lines:
        s = _content(raw)
        if s.startswith("Trace status") or s.startswith("---"):
            section = None
            continue
        if s.startswith("Connection:"):
            connection = s.split(":", 1)[1].strip(); section = None; continue
        if s.startswith("Memory Used:"):
            memory = s.split(":", 1)[1].strip(); section = None; continue
        if s.startswith("Enabled channels:"):
            section = "enabled"; enabled += _split_ch(s.split(":", 1)[1]); continue
        if s.startswith("Available channels:"):
            section = "available"; available += _split_ch(s.split(":", 1)[1]); continue
        # a different labelled field (Block Pool:, Emitted:, ...) ends any channel section
        if ":" in s:
            section = None; continue
        # otherwise a wrapped continuation of the current channel list
        if section == "enabled":
            enabled += _split_ch(s)
        elif section == "available":
            available += _split_ch(s)
    return {"connection": connection, "memory_used": memory,
            "enabled_channels": sorted(set(enabled)), "available_channels": sorted(set(available))}
'''

    # ------------------------------------------------------------------ #
    # performance_start_trace — enable channels + open a .utrace file      #
    # ------------------------------------------------------------------ #
    _START_BODY = _TRACE_HELPERS + r'''
channels = PARAMS.get("channels") or ""
file_path = PARAMS.get("file_path") or ""
enable_lines = _techo("Trace.Enable " + channels) if channels else []
if not file_path:
    base = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()) + "Profiling/"
    try:
        if not os.path.isdir(base):
            os.makedirs(base)
    except Exception:
        pass
    file_path = base + "MCP_" + str(int(unreal.SystemLibrary.get_frame_count())) + ".utrace"
file_lines = _techo("Trace.File " + file_path)
status = _parse_status(_techo("Trace.Status"))
print("@@UMCP@@" + json.dumps({"status": "success", "channels_requested": channels,
    "file_path": file_path, "enable_output": enable_lines, "file_output": file_lines,
    "trace_status": status,
    "note": ("Trace.Enable set the channels then Trace.File opened a .utrace destination. Connection "
             "in trace_status shows where trace data is going (a path == tracing to that file). This "
             "adds real capture overhead and grows the file until performance_stop_trace is called. "
             "NON-reversible runtime command (no undo). A useful capture needs the trace to run for a "
             "while so frames elapse.")}))
'''

    @mcp.tool()
    def performance_start_trace(ctx, channels_or_preset: str = "default", file_path: str = "") -> str:
        """Start an Unreal Insights trace: enable channels then write to a .utrace file. NON-reversible
        (not ledgered -- trace state is engine session state).

        channels_or_preset: a named preset (default/cpu/gpu/memory/loadtime/animation/niagara) OR an
                            explicit channel list (comma or space separated, e.g. 'cpu,gpu,frame').
                            Presets: default=cpu,gpu,frame,bookmark,log; cpu=cpu,frame,bookmark,log;
                            gpu=gpu,frame,bookmark,log; memory=memalloc,memtag,callstack,module,metadata,
                            frame,bookmark; loadtime=loadtime,assetloadtime,frame,bookmark,log.
        file_path:          absolute .utrace output path. Empty -> auto path under the project's
                            Saved/Profiling/ (MCP_<framecount>.utrace).

        Runs Trace.Enable <channels> then Trace.File <path>, then reads Trace.Status back so the
        response shows the resulting Connection (a path there means tracing is live to that file).
        Pair with performance_stop_trace to end it. Real capture overhead applies while running; a
        meaningful capture needs the trace to run across many frames."""
        ch = (channels_or_preset or "").strip()
        channels = _PRESETS.get(ch.lower(), ch)
        params = {"channels": channels, "file_path": (file_path or "").strip()}
        try:
            r = _exec(_START_BODY, params)
            if isinstance(r, dict):
                r["preset_resolved"] = _PRESETS.get(ch.lower()) is not None
                r["channels_or_preset"] = channels_or_preset
            return json.dumps(r, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # performance_stop_trace — stop the active trace                       #
    # ------------------------------------------------------------------ #
    _STOP_BODY = _TRACE_HELPERS + r'''
lines = _techo("Trace.Stop")
status = _parse_status(_techo("Trace.Status"))
stopped = any("stopped" in ln.lower() for ln in lines)
print("@@UMCP@@" + json.dumps({"status": "success", "stop_output": lines,
    "stopped": stopped, "trace_status": status,
    "note": ("Trace.Stop ends any active trace connection (the .utrace file is finalized). If nothing "
             "was tracing, stop_output is empty and Connection stays 'Not tracing' -- not an error. "
             "NON-reversible runtime command.")}))
'''

    @mcp.tool()
    def performance_stop_trace(ctx) -> str:
        """Stop the active Unreal Insights trace (Trace.Stop). NON-reversible (not ledgered).

        Ends any running trace connection and finalizes the .utrace file. Returns whether the engine
        reported 'Tracing stopped.' plus the post-stop Trace.Status (Connection should read 'Not
        tracing'). If no trace was running this is a harmless no-op (stopped=false)."""
        try:
            return json.dumps(_exec(_STOP_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # performance_toggle_channel — enable/disable one trace channel        #
    # ------------------------------------------------------------------ #
    _TOGGLE_BODY = _TRACE_HELPERS + r'''
channel = str(PARAMS.get("channel") or "").strip()
enable = bool(PARAMS.get("enable"))
if not channel:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "channel is required"}))
else:
    cmd = ("Trace.Enable " if enable else "Trace.Disable ") + channel
    lines = _techo(cmd)
    status = _parse_status(_techo("Trace.Status"))
    now_enabled = channel.lower() in [c.lower() for c in status.get("enabled_channels", [])]
    print("@@UMCP@@" + json.dumps({"status": "success", "channel": channel,
        "requested_enable": enable, "command": cmd, "command_output": lines,
        "now_enabled": now_enabled, "enabled_channels": status.get("enabled_channels"),
        "note": ("Toggled a single Unreal Insights trace channel. now_enabled reflects the channel's "
                 "state in Trace.Status afterward. NON-reversible runtime command (channels are engine "
                 "session state); call again with enable flipped to revert.")}))
'''

    @mcp.tool()
    def performance_toggle_channel(ctx, channel: str, enable: bool = True) -> str:
        """Enable or disable a single Unreal Insights trace channel. NON-reversible (not ledgered).

        channel: the channel name (e.g. 'Cpu', 'Gpu', 'Frame', 'LoadTime', 'MemAlloc', 'Niagara').
                 Use performance_list_channels to see enabled/available channels.
        enable:  True -> Trace.Enable <channel>; False -> Trace.Disable <channel> (default True).

        Returns the resulting enabled-channels set and whether the channel is now enabled. Channels are
        engine session state; to revert, call again with the opposite 'enable'."""
        params = {"channel": channel, "enable": enable}
        try:
            return json.dumps(_exec(_TOGGLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # performance_trace_bookmark — emit a named bookmark into the trace    #
    # ------------------------------------------------------------------ #
    _BOOKMARK_BODY = _TRACE_HELPERS + r'''
name = str(PARAMS.get("name") or "").strip()
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
else:
    lines = _techo("Trace.Bookmark " + name)
    print("@@UMCP@@" + json.dumps({"status": "success", "name": name,
        "command": "Trace.Bookmark " + name, "command_output": lines,
        "note": ("Emitted a named bookmark marker into the trace timeline (visible in Unreal Insights). "
                 "The command is silent (no log output) -- an empty command_output is normal. Requires "
                 "the Bookmark channel to be enabled to appear in a capture. NON-reversible.")}))
'''

    @mcp.tool()
    def performance_trace_bookmark(ctx, name: str) -> str:
        """Emit a named bookmark into the Unreal Insights trace timeline (Trace.Bookmark). NON-reversible.

        name: the bookmark label (appears as a marker on the Insights timeline).

        Useful to mark 'before'/'after' points in a running capture. The command is silent (produces no
        log output). For the bookmark to be captured, the 'Bookmark' channel must be enabled (it is in
        the default preset). Not ledgered (nothing to undo)."""
        try:
            return json.dumps(_exec(_BOOKMARK_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # performance_trace_snapshot — snapshot the in-memory trace buffer     #
    # ------------------------------------------------------------------ #
    _SNAPSHOT_BODY = _TRACE_HELPERS + r'''
file_path = str(PARAMS.get("file_path") or "").strip()
if file_path:
    cmd = "Trace.SnapshotFile " + file_path
else:
    cmd = "Trace.Snapshot"
lines = _techo(cmd)
status = _parse_status(_techo("Trace.Status"))
print("@@UMCP@@" + json.dumps({"status": "success", "command": cmd, "file_path": file_path or None,
    "command_output": lines, "trace_status": status,
    "note": ("Trace.SnapshotFile writes the current in-memory trace buffer to a .utrace file; "
             "Trace.Snapshot (no path) flushes the buffer to the active trace connection. Both are "
             "silent on success (empty command_output is normal). NON-reversible runtime command.")}))
'''

    @mcp.tool()
    def performance_trace_snapshot(ctx, file_path: str = "") -> str:
        """Snapshot the current in-memory Unreal Insights trace buffer. NON-reversible (not ledgered).

        file_path: absolute .utrace output path -> runs Trace.SnapshotFile <path> (writes the buffered
                   trace data to that file, no active trace connection required). Empty -> runs
                   Trace.Snapshot, which flushes the buffer to the ACTIVE trace connection (needs one).

        Returns the command run and the post-command Trace.Status. Both snapshot commands are silent on
        success (empty command_output is normal). Use file_path to capture a standalone snapshot without
        having started a streaming trace."""
        try:
            return json.dumps(_exec(_SNAPSHOT_BODY, {"file_path": file_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # performance_list_channels — Trace.Status enabled/available parse     #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _TRACE_HELPERS + r'''
raw = _techo("Trace.Status")
status = _parse_status(raw)
print("@@UMCP@@" + json.dumps({"status": "success",
    "connection": status.get("connection"), "memory_used": status.get("memory_used"),
    "enabled_channels": status.get("enabled_channels"),
    "available_channels": status.get("available_channels"),
    "enabled_count": len(status.get("enabled_channels") or []),
    "available_count": len(status.get("available_channels") or []),
    "raw_status": raw,
    "note": ("Parsed from the engine's Trace.Status console output. 'connection' == the active trace "
             "destination ('Not tracing' when idle). enabled/available_channels are the Unreal Insights "
             "trace channels; available_channels are those NOT currently enabled. Best-effort parse of "
             "the wrapped status text; raw_status carries the unparsed lines.")}))
'''

    @mcp.tool()
    def performance_list_channels(ctx) -> str:
        """List Unreal Insights trace channels and the current trace status (Trace.Status). Read-only.

        Returns connection (the active trace destination, or 'Not tracing'), memory_used, the currently
        enabled_channels, and the available_channels (registered but not enabled), each parsed from the
        engine's Trace.Status output (which wraps across log lines -- parsed best-effort; raw_status
        holds the unparsed text). Feed a channel name into performance_toggle_channel or
        performance_start_trace."""
        try:
            return json.dumps(_exec(_LIST_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
