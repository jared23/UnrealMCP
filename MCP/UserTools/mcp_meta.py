"""UserTools :: MCP bridge meta / self-instrumentation  (spec: docs/spec/debug.md — MCP meta)

The two `debug`-category MCP-meta tools. These are about the MCP BRIDGE itself (not the Unreal
editor): a verbose-logging toggle and a traffic/token-usage read-out. They touch NO editor state,
so they need NO execute_python round-trip and carry NO undo ledger (runtime flags, exactly like the
debug_draw_* helpers skip the ledger).

Both read/write a tiny always-safe traffic ledger that lives in `utils/__init__.py` and is updated
inside the shared `send_command` (bytes sent/received + per-command counts; instrumentation is wrapped
so it can never break a command). Because that ledger lives in the LIVE MCP-server process, the numbers
reflect real bridge traffic through the registered mcp__unreal-mcp__* tools; a separate process (e.g. the
locked verification harness) has its own zeroed ledger.
"""
import json

try:
    import utils as _utils
except Exception:  # pragma: no cover - utils is always importable in the bridge
    _utils = None


def register_tools(mcp, utils):
    # Prefer the live utils module (where the shared send_command + ledger live); fall back to the
    # utils dict's send_command module if needed. The meta tools do NOT send commands themselves.
    umod = _utils

    @mcp.tool()
    def set_mcp_debug(ctx, enabled: bool = True) -> str:
        """Toggle verbose per-command stderr logging in the MCP bridge (off by default).

        When enabled, every bridge `send_command` prints its command type + request/response byte sizes
        to stderr — useful for diagnosing which tool calls are heavy or failing. This is a runtime flag
        on the live MCP-server process; it is NOT persisted and is NOT undoable (nothing in the editor or
        an asset changes).

        enabled: True to turn verbose logging on, False to turn it off (default True).

        Returns the new + prior state."""
        try:
            if umod is None or not hasattr(umod, "set_mcp_debug"):
                return json.dumps({"status": "error",
                                   "message": "bridge instrumentation unavailable (utils.set_mcp_debug missing)"})
            prior = umod.set_mcp_debug(bool(enabled))
            return json.dumps({"status": "success", "enabled": bool(enabled), "prior": bool(prior)}, indent=2)
        except Exception as e:
            return "Error: %s" % e

    @mcp.tool()
    def get_mcp_token_stats(ctx, reset: bool = False) -> str:
        """Read the MCP bridge's traffic / approximate-token usage for the current server session.

        Reports total commands, error count, bytes sent/received, an approximate token estimate
        (~total_bytes/4), and a per-command-type breakdown — accumulated since the MCP server started
        (or since the last reset). These are LIVE-server counters; a separate verification process sees
        its own zeroed ledger.

        reset: if True, zero the ledger after reading (returns the pre-reset snapshot).

        Read-only (not undoable)."""
        try:
            if umod is None or not hasattr(umod, "get_mcp_stats"):
                return json.dumps({"status": "error",
                                   "message": "bridge instrumentation unavailable (utils.get_mcp_stats missing)"})
            stats = umod.reset_mcp_stats() if reset else umod.get_mcp_stats()
            out = {"status": "success", "reset": bool(reset), "debug_logging": bool(umod.get_mcp_debug())}
            out.update(stats)
            return json.dumps(out, indent=2)
        except Exception as e:
            return "Error: %s" % e
