# Spec: Behavior Trees (`/docs/reference/behaviortree`, 24 cmds — **game-AI relevant**)

Clean-room interface. BTs + Blackboards. Implement over `unreal.BehaviorTree`, `BlackboardData`,
AI editor APIs. `req`/`opt`.

**Tree/blackboard mgmt:** `create_behavior_tree`(name,path,blackboard,create_blackboard) · `create_blackboard`(name,path,parent) · `set_bt_blackboard`(behavior_tree,blackboard,parent) · `get_behavior_tree`(behavior_tree,verbosity) · `get_blackboard`(blackboard,filter,include_inherited) · `validate_behavior_tree`(behavior_tree)

**Nodes:** `search_bt_nodes`(query,queries,kind,max_results,include_blueprint) · `get_bt_node_type_info`(node_class,kind,filter,behavior_tree) · `add_bt_node`(behavior_tree,node_class,parent,properties,nodes) · `add_bt_subnode`(node_class,parent,sub_nodes — decorators/services) · `connect_bt_nodes`(parent,child,child_index) · `remove_bt_node`(node,recursive) · `set_bt_node_property`(node,property,value,properties)

**Decorators/logic:** `add_bt_composite_decorator`(operator AND/OR/NOT,children) · `create_bt_node_blueprint`(name,kind,path,parent_class)

**Blackboard keys:** `list_blackboard_key_types`(filter) · `add_blackboard_keys`(name,type,keys) · `remove_blackboard_key`(name) · `set_blackboard_key`(name,new_name,type)

**Runtime:** `compile_behavior_tree` · `arrange_behavior_tree` · `get_bt_runtime`(actor,include_blackboard — inspect running trees in PIE) · `control_bt_runtime`(action start/stop/pause,actor) · `set_bt_dynamic_subtree`(inject_tag,subtree,actor)
