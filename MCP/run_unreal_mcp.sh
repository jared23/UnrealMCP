#!/usr/bin/env bash
#
# macOS launcher for the Unreal MCP Python bridge.
# (macOS equivalent of run_unreal_mcp.bat.)
#
# The bridge speaks the MCP stdio transport, so STDOUT must stay clean for the
# protocol — all human-facing logging goes to STDERR.
#
# It connects to Unreal on localhost:13377. If the editor runs on a different
# machine, forward the port over SSH, e.g.:
#     ssh -N -L 13377:localhost:13377 <user>@<remote-host>
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/python_env"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "ERROR: Python environment not found. Run setup_unreal_mcp.sh first." >&2
  exit 1
fi

echo "Starting Unreal MCP bridge (expects Unreal reachable at localhost:13377)..." >&2
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/unreal_mcp_bridge.py" "$@"
