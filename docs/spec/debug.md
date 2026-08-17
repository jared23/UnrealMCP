# Spec: Debug (`/docs/reference/debug`, 17 cmds)

Clean-room. Blueprint + Behavior Tree debugging: breakpoints, watches, call stack, execution trace,
live instance inspection. Implement over blueprint-debugger + Kismet debug APIs.

**MCP meta:** `set_mcp_debug`(enabled) · `get_mcp_token_stats`

**Blueprint breakpoints:** `set_breakpoint`(blueprint_path,graph_name,node_id,enabled) · `remove_breakpoint`(blueprint_path,graph_name,node_id,all) · `list_breakpoints`(blueprint_path,filter,max_results)

**Execution control:** `get_debug_state`(detail) · `debug_step`(action) · `get_call_stack`(max_results) · `get_execution_trace`(blueprint_path,max_results)

**Live instances:** `list_debug_objects`(blueprint_path,filter,max_results) · `set_debug_object`(blueprint_path,instance,clear) · `inspect_debug_value`(blueprint_path,path,filter,depth,max_results)

**Pin watches:** `set_pin_watch`(blueprint_path,graph_name,node_id,pin_name,remove) · `list_pin_watches`(blueprint_path,values,max_results)

**Behavior Tree breakpoints:** `set_bt_breakpoint`(behavior_tree_path,node_id,enabled) · `remove_bt_breakpoint`(behavior_tree_path,node_id,all) · `list_bt_breakpoints`(behavior_tree_path,max_results)
