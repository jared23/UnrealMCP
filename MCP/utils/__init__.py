"""Utility functions for the UnrealMCP bridge."""

import json
import socket
import sys
import os

# Try to get the port from MCPConstants
DEFAULT_PORT = 13377
DEFAULT_BUFFER_SIZE = 65536
DEFAULT_TIMEOUT = 10  # 10 second timeout

try:
    # Try to read the port from the C++ constants
    plugin_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."))
    constants_path = os.path.join(plugin_dir, "Source", "UnrealMCP", "Public", "MCPConstants.h")
    
    if os.path.exists(constants_path):
        with open(constants_path, 'r') as f:
            constants_content = f.read()
            
            # Extract port from MCPConstants
            port_match = constants_content.find("DEFAULT_PORT = ")
            if port_match != -1:
                port_line = constants_content[port_match:].split(';')[0]
                DEFAULT_PORT = int(port_line.split('=')[1].strip())
                
            # Extract buffer size from MCPConstants
            buffer_match = constants_content.find("DEFAULT_RECEIVE_BUFFER_SIZE = ")
            if buffer_match != -1:
                buffer_line = constants_content[buffer_match:].split(';')[0]
                DEFAULT_BUFFER_SIZE = int(buffer_line.split('=')[1].strip())
except Exception as e:
    # If anything goes wrong, use the defaults (which are already defined)
    print(f"Warning: Could not read constants from MCPConstants.h: {e}", file=sys.stderr)

# --- MCP bridge instrumentation (debug category: set_mcp_debug / get_mcp_token_stats) ---------------
# A tiny, always-safe traffic ledger for the live MCP-server process. Instrumentation is wrapped so it
# can NEVER break a command. `_MCP_DEBUG` gates extra per-command stderr logging (off by default).
_MCP_DEBUG = False
_MCP_STATS = {
    "commands": 0,          # total send_command calls that reached the wire
    "errors": 0,            # calls that raised
    "bytes_sent": 0,        # request bytes written
    "bytes_received": 0,    # response bytes read
    "by_command": {},       # per command_type: {count, bytes_sent, bytes_received}
}


def set_mcp_debug(enabled):
    """Toggle verbose per-command stderr logging in the bridge. Returns the prior value."""
    global _MCP_DEBUG
    prior = _MCP_DEBUG
    _MCP_DEBUG = bool(enabled)
    return prior


def get_mcp_debug():
    return _MCP_DEBUG


def get_mcp_stats():
    """Return a copy of the traffic ledger, with a derived approx-token estimate (~bytes/4)."""
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _MCP_STATS.items()}
    total_bytes = s.get("bytes_sent", 0) + s.get("bytes_received", 0)
    s["approx_tokens"] = total_bytes // 4
    return s


def reset_mcp_stats():
    """Zero the traffic ledger. Returns the pre-reset snapshot."""
    prior = get_mcp_stats()
    _MCP_STATS["commands"] = 0
    _MCP_STATS["errors"] = 0
    _MCP_STATS["bytes_sent"] = 0
    _MCP_STATS["bytes_received"] = 0
    _MCP_STATS["by_command"] = {}
    return prior


def _mcp_note(command_type, sent, received):
    """Best-effort counter update; never raises."""
    try:
        _MCP_STATS["commands"] += 1
        _MCP_STATS["bytes_sent"] += int(sent)
        _MCP_STATS["bytes_received"] += int(received)
        bc = _MCP_STATS["by_command"].setdefault(
            command_type, {"count": 0, "bytes_sent": 0, "bytes_received": 0})
        bc["count"] += 1
        bc["bytes_sent"] += int(sent)
        bc["bytes_received"] += int(received)
        if _MCP_DEBUG:
            print(f"[mcp] {command_type} sent={sent}B recv={received}B", file=sys.stderr)
    except Exception:
        pass


def send_command(command_type, params=None):
    """Send a command to the C++ MCP server and return the response."""
    _sent_bytes = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(DEFAULT_TIMEOUT)  # Set a timeout
            s.connect(("localhost", DEFAULT_PORT))  # Connect to Unreal C++ server
            command = {
                "type": command_type,
                "params": params or {}
            }
            _payload = json.dumps(command).encode('utf-8')
            _sent_bytes = len(_payload)
            s.sendall(_payload)
            
            # Read response with a buffer
            chunks = []
            response_data = b''
            
            # Wait for data with timeout
            while True:
                try:
                    chunk = s.recv(DEFAULT_BUFFER_SIZE)
                    if not chunk:  # Connection closed
                        break
                    chunks.append(chunk)
                    
                    # Try to parse what we have so far
                    response_data = b''.join(chunks)
                    try:
                        # If we can parse it as JSON, we have a complete response
                        json.loads(response_data.decode('utf-8'))
                        break
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we have some data but timed out, try to use what we have
                    if response_data:
                        break
                    raise
            
            if not response_data:
                raise Exception("No data received from server")

            _mcp_note(command_type, _sent_bytes, len(response_data))
            return json.loads(response_data.decode('utf-8'))
    except ConnectionRefusedError:
        _MCP_STATS["errors"] += 1
        print(f"Error: Could not connect to Unreal MCP server on localhost:{DEFAULT_PORT}.", file=sys.stderr)
        print("Make sure your Unreal Engine with MCP plugin is running.", file=sys.stderr)
        raise Exception("Failed to connect to Unreal MCP server: Connection refused")
    except socket.timeout:
        _MCP_STATS["errors"] += 1
        print("Error: Connection timed out while communicating with Unreal MCP server.", file=sys.stderr)
        raise Exception("Failed to communicate with Unreal MCP server: Connection timed out")
    except Exception as e:
        _MCP_STATS["errors"] += 1
        print(f"Error communicating with Unreal MCP server: {str(e)}", file=sys.stderr)
        raise Exception(f"Failed to communicate with Unreal MCP server: {str(e)}")

__all__ = ['send_command', 'set_mcp_debug', 'get_mcp_debug', 'get_mcp_stats', 'reset_mcp_stats']