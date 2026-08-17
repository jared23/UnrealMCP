"""
Bridge module connecting Unreal Engine to MCP (Model Context Protocol).

This module serves as a bridge between the Unreal Engine MCP plugin and
the MCP server provided by the 'mcp' Python package. It handles the communication
between Claude for Desktop and Unreal Engine through the MCP protocol.

Requirements:
    - Python 3.7+
    - MCP package (pip install mcp>=0.1.0)
    - Running Unreal Engine with the UnrealMCP plugin enabled

The bridge connects to the Unreal Engine plugin (which acts as the actual MCP server)
and exposes MCP functionality to Claude for Desktop. This allows Claude to interact
with Unreal Engine through natural language commands.
"""

import json
import socket
import sys
import os
import importlib.util
import importlib

# Try to get the port from MCPConstants
DEFAULT_PORT = 13377
DEFAULT_BUFFER_SIZE = 65536
DEFAULT_TIMEOUT = 10  # 10 second timeout

try:
    # Try to read the port from the C++ constants
    plugin_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
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
    # No need to redefine DEFAULT_PORT and DEFAULT_BUFFER_SIZE here


print(f"Using port: {DEFAULT_PORT}", file=sys.stderr)
print(f"Using buffer size: {DEFAULT_BUFFER_SIZE}", file=sys.stderr)

# Check for local python_modules directory first
local_modules_path = os.path.join(os.path.dirname(__file__), "python_modules")
if os.path.exists(local_modules_path):
    print(f"Found local python_modules directory: {local_modules_path}", file=sys.stderr)
    sys.path.insert(0, local_modules_path)
    print(f"Added local python_modules to sys.path", file=sys.stderr)

# Try to import MCP
mcp_spec = importlib.util.find_spec("mcp")
if mcp_spec is None:
    print("Error: The 'mcp' package is not installed.", file=sys.stderr)
    print("Please install it using one of the following methods:", file=sys.stderr)
    print("1. Run setup_unreal_mcp.bat to install it globally", file=sys.stderr)
    print("2. Run: pip install mcp", file=sys.stderr)
    print("3. Run: pip install mcp -t ./python_modules", file=sys.stderr)
    sys.exit(1)

try:
    # mcp 2.0.0: FastMCP was renamed/moved to mcp.server.mcpserver.MCPServer
    from mcp.server.mcpserver import MCPServer
except ImportError as e:
    print(f"Error importing MCPServer from mcp package: {e}", file=sys.stderr)
    print("This bridge targets mcp >= 2.0 (mcp.server.mcpserver.MCPServer).", file=sys.stderr)
    print("Installed mcp is missing it — check the venv (setup_unreal_mcp.sh).", file=sys.stderr)
    sys.exit(1)

import inspect as _inspect
import functools as _functools


def _strip_ctx_wrapper(func):
    """Our tool functions follow the FastMCP `def tool(ctx, ...)` pattern with an untyped
    leading `ctx`. Under MCPServer that `ctx` would leak into the client-facing schema as a
    REQUIRED argument (and break calls). This strips a leading ctx/context parameter and
    passes None for it at call time, leaving the module code unchanged."""
    sig = _inspect.signature(func)
    params = list(sig.parameters.values())
    if not params or params[0].name not in ("ctx", "context"):
        return func
    # If the leading param is TYPED (e.g. `ctx: Context`), leave it — MCPServer injects the
    # real Context and hides it from the schema. Only strip our untyped `ctx` convention.
    if params[0].annotation is not _inspect.Parameter.empty:
        return func
    ctx_name = params[0].name
    new_sig = sig.replace(parameters=params[1:])

    @_functools.wraps(func)
    def wrapper(**kwargs):
        return func(None, **kwargs)

    wrapper.__signature__ = new_sig
    ann = dict(getattr(func, "__annotations__", {}))
    ann.pop(ctx_name, None)
    wrapper.__annotations__ = ann
    return wrapper


class _ToolShim:
    """FastMCP-compatible facade over MCPServer. The Commands/ and UserTools/ modules register
    via `@mcp.tool()`; this captures those registrations and, on run(), adds ctx-stripped
    wrappers to the real MCPServer. Any other attribute access delegates to the real server."""

    def __init__(self, real):
        self._real = real
        self._captured = []

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")
        description = kwargs.get("description")

        def deco(fn):
            self._captured.append((fn, name, description))
            return fn
        return deco

    def flush(self):
        for fn, name, description in self._captured:
            wrapper = _strip_ctx_wrapper(fn)
            self._real.add_tool(
                wrapper,
                name=name or fn.__name__,
                description=description or (fn.__doc__ or "").strip() or None,
            )
        self._captured = []

    def run(self, *args, **kwargs):
        self.flush()
        return self._real.run(*args, **kwargs)

    def __getattr__(self, item):
        # _real/_captured live in __dict__, so this only fires for delegated attrs
        return getattr(self._real, item)


# Initialize the MCP server (mcp 2.0.0), wrapped so existing tool modules work unchanged.
_real_mcp = MCPServer(
    "UnrealMCP",
    description="Unreal Engine integration through the Model Context Protocol",
    version="0.2.0",
)
mcp = _ToolShim(_real_mcp)

def send_command(command_type, params=None, timeout=DEFAULT_TIMEOUT):
    """Send a command to the C++ MCP server and return the response.
    
    Args:
        command_type: The type of command to send
        params: Optional parameters for the command
        timeout: Timeout in seconds (default: DEFAULT_TIMEOUT)
    
    Returns:
        The JSON response from the server
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)  # Set a timeout
            s.connect(("localhost", DEFAULT_PORT))  # Connect to Unreal C++ server
            command = {
                "type": command_type,
                "params": params or {}
            }
            s.sendall(json.dumps(command).encode('utf-8'))
            
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
                
            return json.loads(response_data.decode('utf-8'))
    except ConnectionRefusedError:
        print(f"Error: Could not connect to Unreal MCP server on localhost:{DEFAULT_PORT}.", file=sys.stderr)
        print("Make sure your Unreal Engine with MCP plugin is running.", file=sys.stderr)
        raise Exception("Failed to connect to Unreal MCP server: Connection refused")
    except socket.timeout:
        print("Error: Connection timed out while communicating with Unreal MCP server.", file=sys.stderr)
        raise Exception("Failed to communicate with Unreal MCP server: Connection timed out")
    except Exception as e:
        print(f"Error communicating with Unreal MCP server: {str(e)}", file=sys.stderr)
        raise Exception(f"Failed to communicate with Unreal MCP server: {str(e)}")

# All commands have been moved to separate modules in the Commands directory

def load_commands():
    """Load all commands from the Commands directory structure."""
    commands_dir = os.path.join(os.path.dirname(__file__), 'Commands')
    if not os.path.exists(commands_dir):
        print(f"Commands directory not found at: {commands_dir}", file=sys.stderr)
        return

    # First, load Python files directly in the Commands directory
    for filename in os.listdir(commands_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            try:
                module_name = f"Commands.{filename[:-3]}"  # Remove .py extension
                module = importlib.import_module(module_name)
                if hasattr(module, 'register_all'):
                    module.register_all(mcp)
                    print(f"Registered commands from module: {filename}", file=sys.stderr)
                else:
                    print(f"Warning: {filename} has no register_all function", file=sys.stderr)
            except Exception as e:
                print(f"Error loading module {filename}: {e}", file=sys.stderr)

    # Then, load command categories from subdirectories
    for category in os.listdir(commands_dir):
        category_path = os.path.join(commands_dir, category)
        if os.path.isdir(category_path) and not category.startswith('__'):
            try:
                # Try to load the category's __init__.py which should have register_all
                module_name = f"Commands.{category}"
                module = importlib.import_module(module_name)
                if hasattr(module, 'register_all'):
                    module.register_all(mcp)
                    print(f"Registered commands from category: {category}", file=sys.stderr)
                else:
                    print(f"Warning: {category} has no register_all function", file=sys.stderr)
            except Exception as e:
                print(f"Error loading category {category}: {e}", file=sys.stderr)

def load_user_tools():
    """Load user-defined tools from the UserTools directory."""
    user_tools_dir = os.path.join(os.path.dirname(__file__), 'UserTools')
    if not os.path.exists(user_tools_dir):
        print(f"User tools directory not found at: {user_tools_dir}", file=sys.stderr)
        return

    for filename in os.listdir(user_tools_dir):
        if filename.endswith('.py') and filename != '__init__.py':
            module_name = filename[:-3]
            try:
                spec = importlib.util.spec_from_file_location(module_name, os.path.join(user_tools_dir, filename))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, 'register_tools'):
                    from utils import send_command
                    module.register_tools(mcp, {'send_command': send_command})
                    print(f"Loaded user tool: {module_name}", file=sys.stderr)
                else:
                    print(f"Warning: {filename} has no register_tools function", file=sys.stderr)
            except Exception as e:
                print(f"Error loading user tool {filename}: {str(e)}", file=sys.stderr)

def main():
    """Main entry point for the Unreal MCP bridge."""
    print("Starting Unreal MCP bridge...", file=sys.stderr)
    try:
        load_commands()  # Load built-in commands
        load_user_tools()  # Load user-defined tools
        mcp.run()  # Start the MCP bridge
    except Exception as e:
        print(f"Error starting MCP bridge: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main() 