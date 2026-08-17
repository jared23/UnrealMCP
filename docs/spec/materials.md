# Spec: Materials (`/docs/reference/materials`, 52 cmds — high value)

Clean-room interface. Graph-document pattern (build/layout/validate/cleanup). Implement over
`unreal.MaterialEditingLibrary`, `MaterialInstanceConstant`, MPC APIs. Note our fork already has
basic `create_material`/`modify_material`/`get_material_info` — supersede/extend with these.

**Create/manage:** `create_material`(name,path,blend_mode,shading_model,domain,two_sided,opacity_mask_clip_value,force) · `create_material_instance`(parent_path,name,path,scalar_params,vector_params,texture_params,force) · `create_material_function`(name,path,description,expose_to_library,function_type,force) · `create_material_function_instance`(name,parent_path,path,force) · `create_material_parameter_collection`(name,path,force)

**Graph build/layout:** `build_material_graph`(material_path,nodes,connections,clear_existing) · `build_material_function_graph`(function_path,nodes,connections,clear_existing) · `layout_material_graph`(material_path) · `layout_material_expressions`(alias) · `layout_material_function_graph`(function_path)

**Nodes:** `add_material_expression`(material_path,node) · `add_material_comments`(comments) · `add_material_function_input`(function_path,input_name,input_type,description,sort_priority,use_preview_as_default,preview_value,pos_x,pos_y) · `add_material_function_output`(output_name,description,sort_priority,pos_x,pos_y) · `connect_material_expressions`(from_node/from_node_guid,to_node/to_node_guid,to_pin,from_pin) · `disconnect_material_expression`(node_index/node_guid,input_pin) · `delete_material_expression`(node_index/node_guid) · `move_material_expression`(…,pos_x,pos_y) · `duplicate_material_expression`(…,offset_x,offset_y) · `set_material_expression_property`(…,property_name,property_value)

**Properties/params:** `set_material_properties`(material_path, blend_mode,shading_model,two_sided,opacity_mask_clip_value,dithered_lod_transition,allow_negative_emissive_color,translucency_lighting_mode,refraction_method,recompile) · `set_material_instance_parameter`(param_name,param_type,value) · `set_material_instance_parent`(parent_path) · `clear_material_instance_parameter`(param_name) · `set_material_function_instance_parameter`(instance_path,param_name,param_type,value) · `set_material_function_input`/`set_material_function_output`(…) · `set_material_parameter_collection_parameter`(collection_path,param_name,param_type,value) · `set_material_layers`(material_path,layers,blends)

**Inspect:** `get_material_info`(include) · `get_material_errors`(recompile) · `get_material_graph_nodes`(type_filter,verbosity) · `get_material_expression_info`(node_index/node_guid) · `get_material_property_connections` · `get_available_material_pins` · `get_material_instance_parameters` · `get_expression_type_info`(type_name,function_path) · `get_material_function_info`(verbosity,type_filter) · `get_material_function_instance_parameters` · `get_material_layers` · `get_material_parameter_collection`

**Compile/validate/cleanup:** `recompile_material` · `apply_material` · `material_stats`(filter,max_results) · `validate_material_graph` · `validate_material_function` · `trace_material_connection`(direction,max_depth) · `cleanup_material_graph`(mode,dry_run) · `cleanup_material_function`(dry_run)

**Search/MPC:** `list_material_expression_types`(filter,max_results,include_details) · `search_material_functions`(filter,path,max_results,include_engine) · `delete_material_parameter_collection_parameter`(collection_path,param_name)
