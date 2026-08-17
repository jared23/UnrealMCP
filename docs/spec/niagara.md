# Spec: Niagara / VFX (`/docs/reference/niagara`, 109 cmds — later milestone)

Clean-room interface. Big category; grouped extraction below (representative — expand to full 109
params when we build Niagara). Implement over `unreal.NiagaraSystem`, emitter/module editor APIs.

**System:** `create_niagara_system`(asset_path,template) · `get_niagara_system_info`(system_path,include,filter) · `list_niagara_systems`(path,name_filter,max_results) · `delete_niagara_system`(force) · `compile_niagara_system`(wait_for_completion)

**Emitters:** `add_niagara_emitter`(system_path,emitter_path/template,emitter_name) · `remove_niagara_emitter` · `get_niagara_emitters`(filter) · `duplicate_niagara_emitter`(new_name) · `reorder_niagara_emitter`(new_index)

**Module stack:** `add_niagara_module`(emitter_name,module_path,script_usage) · `remove_niagara_module` · `get_niagara_modules`(script_usage,include_inputs) · `set_niagara_module_enabled`(enabled) · `reorder_niagara_module`(new_index)

**Module inputs:** `set_niagara_module_input`(module_name,input_name,value) · `get_niagara_module_inputs`(input_filter) · `set_niagara_dynamic_input`(dynamic_input_type) · `set_niagara_curve`(curve_type,keys) · `set_niagara_stack_value`(kind,value) — universal setter

**Renderers:** `add_niagara_renderer`(renderer_type) · `remove_niagara_renderer`(renderer_index) · `get_niagara_renderer_info` · `set_niagara_renderer_property`(property,value,renderer_index) · `set_niagara_renderer_binding`(binding_name,attribute)

**User params:** `add_niagara_user_parameter`(parameter_name,parameter_type) · `get_niagara_user_parameters`(filter) · `set_niagara_user_parameter`(value,actor_name/system_path) · `remove_niagara_user_parameter`

**Graph/script:** `create_niagara_scratch_pad_module`(script_usage,module_type) · `create_niagara_module_asset`(asset_path,module_type,category) · `build_niagara_graph`(nodes,connections,clear_existing) · `add_niagara_graph_node`(node_type,op_name/function_script,pos_x,pos_y) · `delete_niagara_graph_node`(node_index/node_id) · `layout_niagara_graph`(all_graphs,route_knots)

**Inspect/validate:** `get_niagara_graph_nodes`(verbosity,type_filter) · `get_niagara_node_info` · `trace_niagara_connection`(direction,max_depth) · `validate_niagara_graph` · `validate_niagara_stack`(min_severity) · `fix_niagara_stack_issue`(all,issue_key/match) · `get_niagara_system_errors`(severity) · `get_niagara_particle_stats`

**Runtime:** `spawn_niagara_effect`(location,rotation,auto_activate) · `control_niagara_effect`(actor_name,action) · `add_niagara_component`(actor_name,relative_location) · `get_niagara_actors`(system_filter)

**Discovery:** `list_niagara_asset_types` · `list_niagara_modules` · `list_niagara_emitter_templates` · `list_niagara_data_interfaces` · `list_niagara_parameter_types`
