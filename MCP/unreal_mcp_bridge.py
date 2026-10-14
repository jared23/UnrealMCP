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


def _init_domain_filter():
    """Read UNREALMCP_DOMAINS and load the file->domain manifest.

    Returns (requested_frozenset_or_None, resolve_fn_or_None). None means 'no filter'
    -> register every tool exactly as before, so an unconfigured session is unchanged.
    When set (e.g. UNREALMCP_DOMAINS="anim,rig,world"), only tools whose domain is in
    the set are registered; 'core' is always included whether or not it is listed.
    """
    raw = os.environ.get("UNREALMCP_DOMAINS", "").strip()
    if not raw or raw.lower() in ("*", "all"):
        return None, None
    # 'core' is an explicit domain, not auto-added, so every tool lives in exactly one
    # server (per the spec). Self-contained workflow entries list 'core' themselves.
    requested = frozenset(
        d.strip().lower() for d in raw.replace(";", ",").split(",") if d.strip()
    )
    dm = None
    try:
        import domain_manifest as dm  # MCP dir is on sys.path when run from there
    except Exception:
        try:
            _mp = os.path.join(os.path.dirname(__file__), "domain_manifest.py")
            _spec = importlib.util.spec_from_file_location("domain_manifest", _mp)
            dm = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(dm)
        except Exception as e:
            print(f"UNREALMCP_DOMAINS={raw!r} set but domain_manifest.py failed to load "
                  f"({e}); loading ALL tools.", file=sys.stderr)
            return None, None
    print(f"[UnrealMCP] domain filter active: {sorted(requested)}", file=sys.stderr)
    return requested, dm.resolve_domain


class _ToolShim:
    """FastMCP-compatible facade over MCPServer. The Commands/ and UserTools/ modules register
    via `@mcp.tool()`; this captures those registrations and, on run(), adds ctx-stripped
    wrappers to the real MCPServer. Any other attribute access delegates to the real server."""

    def __init__(self, real):
        self._real = real
        self._captured = []
        # Optional domain filter (UNREALMCP_DOMAINS). None -> register everything.
        self._requested, self._resolve = _init_domain_filter()
        # Optional router mode (UNREALMCP_ROUTERS): collapse verb families into per-domain routers.
        self._routers = os.environ.get("UNREALMCP_ROUTERS", "").strip().lower() in ("1", "true", "yes", "on")

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")
        description = kwargs.get("description")

        def deco(fn):
            self._captured.append((fn, name, description))
            return fn
        return deco

    def _flush_routers(self):
        """Router mode: register per-domain enum routers instead of individual tools."""
        try:
            import mcp_routers
        except Exception:
            _mp = os.path.join(os.path.dirname(__file__), "mcp_routers.py")
            _spec = importlib.util.spec_from_file_location("mcp_routers", _mp)
            mcp_routers = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(mcp_routers)
        resolve = self._resolve
        if resolve is None:
            try:
                import domain_manifest as _dm
            except Exception:
                _mp = os.path.join(os.path.dirname(__file__), "domain_manifest.py")
                _spec = importlib.util.spec_from_file_location("domain_manifest", _mp)
                _dm = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_dm)
            resolve = _dm.resolve_domain
        groups = mcp_routers.build_router_groups(self._captured, resolve, self._requested)
        added = 0
        for (domain, rverb), entries in sorted(groups.items()):
            router, desc = mcp_routers.make_router(domain, rverb, entries)
            self._real.add_tool(router, name=router.__name__, description=desc)
            added += 1
        self._captured = []
        print(f"[UnrealMCP] router mode: {added} routers "
              f"(domains={sorted(self._requested) if self._requested else 'all'})", file=sys.stderr)

    def flush(self):
        if self._routers:
            self._flush_routers()
            return
        added = 0
        skipped = 0
        for fn, name, description in self._captured:
            tool_name = name or fn.__name__
            if self._requested is not None and self._resolve is not None:
                # Resolve on the ORIGINAL fn (its __code__ points at the real tool file);
                # the ctx-stripped wrapper below would point at this bridge instead.
                try:
                    co = getattr(fn, "__code__", None)
                    domain = self._resolve(co.co_filename if co is not None else "", tool_name)
                except Exception:
                    domain = "misc"
                if domain not in self._requested:
                    skipped += 1
                    continue
            wrapper = _strip_ctx_wrapper(fn)
            self._real.add_tool(
                wrapper,
                name=tool_name,
                description=description or (fn.__doc__ or "").strip() or None,
            )
            added += 1
        self._captured = []
        if self._requested is not None:
            print(f"[UnrealMCP] registered {added} tools, skipped {skipped} "
                  f"(domains={sorted(self._requested)})", file=sys.stderr)

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