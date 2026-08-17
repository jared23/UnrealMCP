"""Material-related commands for Unreal Engine.

This module contains all material-related commands for the UnrealMCP bridge,
including creation, modification, and querying of materials.
"""

import sys
import os
import json
import importlib.util
import importlib
from mcp.server.mcpserver import Context

# Import send_command from the parent module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unreal_mcp_bridge import send_command

def register_all(mcp):
    """Register all material-related commands with the MCP server."""
    
    # Create material command
    @mcp.tool()
    def create_material(ctx: Context, package_path: str, name: str, properties: dict = None) -> str:
        """Create a new material in the Unreal project.
        
        Args:
            package_path: The path where the material should be created (e.g., '/Game/Materials')
            name: The name of the material
            properties: Optional dictionary of material properties to set. Can include:
                - shading_model: str (e.g., "DefaultLit", "Unlit", "Subsurface", etc.)
                - blend_mode: str (e.g., "Opaque", "Masked", "Translucent", etc.)
                - two_sided: bool
                - dithered_lod_transition: bool
                - cast_contact_shadow: bool
                - base_color: list[float] (RGBA values 0-1)
                - metallic: float (0-1)
                - roughness: float (0-1)
        """
        try:
            params = {
                "package_path": package_path,
                "name": name
            }
            if properties:
                params["properties"] = properties
            response = send_command("create_material", params)
            if response["status"] == "success":
                return f"Created material: {response['result']['name']} at path: {response['result']['path']}"
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error creating material: {str(e)}"

    # Modify material command
    @mcp.tool()
    def modify_material(ctx: Context, path: str, properties: dict) -> str:
        """Modify an existing material's properties.
        
        Args:
            path: The full path to the material (e.g., '/Game/Materials/MyMaterial')
            properties: Dictionary of material properties to set. Can include:
                - shading_model: str (e.g., "DefaultLit", "Unlit", "Subsurface", etc.)
                - blend_mode: str (e.g., "Opaque", "Masked", "Translucent", etc.)
                - two_sided: bool
                - dithered_lod_transition: bool
                - cast_contact_shadow: bool
                - base_color: list[float] (RGBA values 0-1)
                - metallic: float (0-1)
                - roughness: float (0-1)
        """
        try:
            params = {
                "path": path,
                "properties": properties
            }
            response = send_command("modify_material", params)
            if response["status"] == "success":
                return f"Modified material: {response['result']['name']} at path: {response['result']['path']}"
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error modifying material: {str(e)}"

    # Get material info command
    @mcp.tool()
    def get_material_info(ctx: Context, path: str) -> dict:
        """Get information about a material.
        
        Args:
            path: The full path to the material (e.g., '/Game/Materials/MyMaterial')
            
        Returns:
            Dictionary containing material information including:
                - name: str
                - path: str
                - shading_model: str
                - blend_mode: str
                - two_sided: bool
                - dithered_lod_transition: bool
                - cast_contact_shadow: bool
                - base_color: list[float]
                - metallic: float
                - roughness: float
        """
        try:
            params = {"path": path}
            response = send_command("get_material_info", params)
            if response.get("status") == "success":
                return response["result"]
            # Native handler supports base UMaterial only. On failure, probe the asset so we
            # can return an honest, specific message (e.g. MaterialInstanceConstant / not found)
            # instead of a blind "Failed to load material". The bridge process has no `unreal`
            # module, so inspect via execute_python. Read-only; no '''/backslash in the code.
            probe_code = (
                "import unreal, json\n"
                "p = " + json.dumps(path) + "\n"
                "exists = unreal.EditorAssetLibrary.does_asset_exist(p)\n"
                "cls = None\n"
                "parent = None\n"
                "if exists:\n"
                "    a = unreal.EditorAssetLibrary.load_asset(p)\n"
                "    cls = a.get_class().get_name() if a else None\n"
                "    try:\n"
                "        if isinstance(a, unreal.MaterialInstance):\n"
                "            par = a.get_editor_property(\"parent\")\n"
                "            parent = par.get_path_name() if par else None\n"
                "    except Exception:\n"
                "        pass\n"
                "print(\"@@MI@@\" + json.dumps({\"exists\": exists, \"class\": cls, \"parent\": parent}))\n"
            )
            info = {}
            probe = send_command("execute_python", {"code": probe_code})
            if isinstance(probe, dict) and probe.get("status") == "success":
                out = (probe.get("result", {}) or {}).get("output", "") or ""
                for line in out.replace("\r\n", "\n").splitlines():
                    if "@@MI@@" in line:
                        try:
                            info = json.loads(line.split("@@MI@@", 1)[1])
                        except Exception:
                            info = {}
                        break
            if info and not info.get("exists"):
                return {"error": "asset not found", "path": path}
            cls = info.get("class")
            if cls and cls != "Material":
                return {
                    "error": "unsupported material type",
                    "path": path,
                    "asset_class": cls,
                    "parent_material": info.get("parent"),
                    "note": ("get_material_info supports base UMaterial only. For a %s, use "
                             "get_material_slots (assigned materials) or get_object_property / "
                             "describe_object on the asset for parameter overrides." % cls),
                }
            return {"error": response.get("message", "get_material_info failed"), "path": path}
        except Exception as e:
            return {"error": str(e)}