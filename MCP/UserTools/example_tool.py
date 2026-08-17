# Example UserTools module — intentionally registers NO tools.
#
# This file used to ship two placeholder tools that leaked into the live toolset:
#   - my_custom_tool  -> returned "Hello from custom tool!"  (pure placeholder)
#   - get_actor_count -> reported a DIFFERENT total (e.g. 72) than get_world_info /
#                        count_actors_by_class (64), because native get_scene_info counts
#                        hidden/editor-only/system actors too. Confusing duplicate.
# Both were removed (2026-08-14) after live QA flagged them. Keep this file as the
# reference skeleton for authoring a UserTools module — copy MCP/UserTools/editor_level.py
# (the gold standard) for real work.


def register_tools(mcp, utils):
    # Intentionally empty: no tools registered from the example module.
    return
