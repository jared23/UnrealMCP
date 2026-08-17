#!/usr/bin/env bash
#
# macOS setup for the Unreal MCP Python bridge.
# (macOS equivalent of setup_unreal_mcp.bat — the bridge runs on the Mac and
#  connects to Unreal on the Windows machine via an SSH tunnel to localhost:13377.)
#
# Creates an isolated venv and installs the official `mcp` package into it.
# Does NOT modify any MCP-client config by default — pass --register <config.json>
# only once the tunnel + Unreal are actually up, so you don't register a server
# that can't connect yet.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/python_env"
REGISTER_CONFIG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --register) REGISTER_CONFIG="${2:-}"; shift 2 ;;
    -h|--help) echo "Usage: $0 [--register <mcp-client-config.json>]"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- Find a Python >= 3.10 (the `mcp` SDK requires it; system python3 is 3.9) ---
find_python() {
  local c ver major minor
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    ver="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
    major="${ver%%.*}"; minor="${ver##*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then echo "$c"; return 0; fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "ERROR: need Python >= 3.10 (the mcp SDK requires it)." >&2
  echo "       Found only: $(python3 --version 2>&1)" >&2
  echo "       Install one, e.g.:  brew install python@3.12" >&2
  exit 1
fi
echo "Using $("$PY" --version 2>&1) at $(command -v "$PY")"

# --- Create the venv (isolated; no --user, no sudo) ---
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtual environment at $VENV_DIR ..."
  "$PY" -m venv "$VENV_DIR"
else
  echo "Virtual environment already exists at $VENV_DIR"
fi

# --- Install the bridge's dependency (official mcp SDK) ---
echo "Installing dependencies from requirements.txt ..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"
"$VENV_DIR/bin/python" -c 'import mcp,sys; print("OK: mcp", getattr(mcp,"__version__","?"), "on Python", sys.version.split()[0])'

# --- Optional: register with an MCP client (only when explicitly asked) ---
if [ -n "$REGISTER_CONFIG" ]; then
  echo "Registering 'unreal' server in $REGISTER_CONFIG ..."
  "$VENV_DIR/bin/python" "$SCRIPT_DIR/temp_update_config.py" "$REGISTER_CONFIG" "$SCRIPT_DIR/run_unreal_mcp.sh"
fi

echo
echo "Setup complete."
echo "  Start the bridge with:  $SCRIPT_DIR/run_unreal_mcp.sh"
echo "  (Requires the SSH tunnel to Unreal, i.e. localhost:13377 reachable.)"
