# Spec: StateTree (`/docs/reference/statetree`, 42 cmds — game-AI, UE5 state system)

Clean-room interface. UE5 StateTree (hierarchical state + utility AI). Implement over
`unreal.StateTree` editor APIs. Nodes referenced by GUID; graphs export/compile like our doc pattern.

**Query:** `get_statetree_info` · `export_statetree`(full JSON) · `get_statetree_states`(hierarchy) · `get_statetree_state`(tasks/transitions) · `get_statetree_evaluators` · `get_statetree_global_tasks` · `get_statetree_parameters` · `get_statetree_node`(by GUID) · `get_statetree_bindings` · `get_statetree_full_info`(verbosity) · `search_statetree_nodes`(class/category) · `list_statetree_node_types` · `get_statetree_binding_sources`(target) · `search_statetree_properties` · `list_statetree_schemas` · `get_statetree_transition_targets` · `list_statetree_enum_values`(category)

**Create:** `create_statetree`(schema) · `add_statetree_state` · `add_statetree_task`(state) · `add_statetree_evaluator`(global) · `add_statetree_global_task` · `add_statetree_condition`(state/transition) · `add_statetree_consideration`(utility) · `add_statetree_transition`(trigger,target) · `add_statetree_parameter` · `add_statetree_binding`(source→target) · `bind_statetree_delegate`(dispatcher→listener) · `bind_statetree_task_completion`

**Modify:** `set_statetree_parameter`(value) · `set_statetree_state_property` · `set_statetree_node_property` · `set_statetree_transition_property` · `set_statetree_schema` · `set_statetree_component_tree`(assign to component) · `set_statetree_color`(theme)

**Delete:** `remove_statetree_state`(+children) · `remove_statetree_node`(GUID) · `remove_statetree_transition` · `remove_statetree_binding` · `remove_statetree_parameter`

**Build:** `compile_statetree`(returns errors)
