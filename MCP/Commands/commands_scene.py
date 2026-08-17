"""Scene-related commands for Unreal Engine.

This module contains all scene-related commands for the UnrealMCP bridge,
including getting scene information, creating, modifying, and deleting objects.
"""

import sys
import os
import json
from mcp.server.mcpserver import Context

# Import send_command from the parent module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unreal_mcp_bridge import send_command

def register_all(mcp):
    """Register all scene-related commands with the MCP server."""
    
    @mcp.tool()
    def get_scene_info(ctx: Context) -> str:
        """Get detailed information about the current Unreal scene."""
        try:
            response = send_command("get_scene_info")
            if response["status"] == "success":
                return json.dumps(response["result"], indent=2)
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error getting scene info: {str(e)}"

    @mcp.tool()
    def create_object(ctx: Context, type: str, location: list = None, label: str = None) -> str:
        """[DEPRECATED] Create a new object in the Unreal scene.

        DEPRECATED — prefer `spawn_actor_from_class`, which resolves actors by display
        label, returns JSON, and is on the undo ledger. This legacy tool resolves by
        internal name only, returns a plain string, and is NOT undoable.

        Args:
            type: The type of object to create (e.g., 'StaticMeshActor', 'PointLight', etc.)
            location: Optional 3D location as [x, y, z]
            label: Optional label for the object
        """
        try:
            params = {"type": type}
            if location:
                params["location"] = location
            if label:
                params["label"] = label
            response = send_command("create_object", params)
            if response["status"] == "success":
                return f"[DEPRECATED tool — prefer spawn_actor_from_class] Created object: {response['result']['name']} with label: {response['result']['label']}"
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error creating object: {str(e)}"

    @mcp.tool()
    def modify_object(ctx: Context, name: str, location: list = None, rotation: list = None, scale: list = None) -> str:
        """[DEPRECATED] Modify an existing object's transform in the Unreal scene.

        DEPRECATED — prefer `set_actor_transform` (label-resolved, JSON, undoable) or the
        higher-level `align_actors`/`distribute_actors`/`snap_actors_to_floor`. This legacy
        tool resolves by internal name only, returns a plain string, and is NOT undoable.

        Args:
            name: The name of the object to modify
            location: Optional 3D location as [x, y, z]
            rotation: Optional rotation as [pitch, yaw, roll]
            scale: Optional scale as [x, y, z]
        """
        try:
            params = {"name": name}
            if location:
                params["location"] = location
            if rotation:
                params["rotation"] = rotation
            if scale:
                params["scale"] = scale
            response = send_command("modify_object", params)
            if response["status"] == "success":
                return f"[DEPRECATED tool — prefer set_actor_transform] Modified object: {response['result']['name']}"
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error modifying object: {str(e)}"

    @mcp.tool()
    def delete_object(ctx: Context, name: str) -> str:
        """[DEPRECATED] Delete an object from the Unreal scene.

        DEPRECATED and UNSAFE — prefer `delete_actor`, which soft-deletes to a trash folder
        and is fully undoable. This legacy tool resolves by internal name only, returns a
        plain string, and performs a HARD delete that is NOT on the undo ledger.

        Args:
            name: The name of the object to delete
        """
        try:
            response = send_command("delete_object", {"name": name})
            if response["status"] == "success":
                return f"[DEPRECATED tool — prefer delete_actor (undoable soft-delete)] Deleted object: {name}"
            else:
                return f"Error: {response['message']}"
        except Exception as e:
            return f"Error deleting object: {str(e)}" 