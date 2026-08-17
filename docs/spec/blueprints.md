# Spec: Blueprints (`/docs/reference/blueprints`, 56 cmds — high value, ~M2)

Clean-room interface. Graphs use our **document pattern** (export → edit doc → build/patch, auto-layout,
validate). Implement over `unreal.BlueprintEditorLibrary`, K2 graph subsystems, `KismetEditorUtilities`.

**Core:** `create_blueprint`(name,path,parent_class) · `export_blueprint`(blueprint_path; sections,inheritance,values) · `compile_blueprint`(blueprint_path) · `reparent_blueprint`(blueprint_path,new_parent_class)

**Graph mgmt:** `open_blueprint_graph`(blueprint_path,graph) · `export_graph`(asset,domain,graph) · `build_graph`(asset,domain,nodes,connections) · `apply_graph_patch`(asset,mode=merge/sync,nodes) · `diff_graph`(asset,nodes,connections) · `arrange_blueprint_graph`(blueprint_path,function_name) · `build_blueprint_graph`(blueprint_path,nodes,connections,clear_graph) — atomic

**Variables & CDO:** `create_blueprint_variable`(blueprint_path,variable_name,variable_type,type_path) · `get_blueprint_variable_details` · `set_blueprint_variable_properties`(…,properties) · `delete_blueprint_variable` · `get_blueprint_class_defaults`(blueprint_path,filter,include_inherited) · `set_blueprint_class_defaults`(blueprint_path,property_path,value)

**Functions & events:** `create_blueprint_function`(blueprint_path,function_name,return_type) · `create_local_variable` · `create_event_graph`(graph_name) · `create_custom_event`(name,inputs,replication — via build_blueprint_graph) · `add_function_input`/`add_function_output`(param_name,param_type) · `set_function_properties`(pure,const,category) · `delete_blueprint_function` · `rename_blueprint_function`(old,new) · `override_blueprint_function`(function_name) · `rename_event_graph` · `delete_event_graph` · `get_blueprint_function_details`(…,include_graph)

**Components:** `add_component_to_blueprint`(blueprint_path/actor,component_class,component_name) · `set_blueprint_component_property`(component_name,property_path,value) · `delete_component` · `reparent_component`(component_name,new_parent) · `set_root_component`(component_name)

**Interfaces & dispatchers:** `create_blueprint_interface`(name,path) · `implement_blueprint_interface`(interface,compile) · `remove_blueprint_interface`(interface,preserve_functions) · `list_blueprint_interfaces` · `create_event_dispatcher`(name,inputs) · `create_blueprint_function_library`(name,path)

**Nodes:** `search_nodes`(filter,blueprint_path,max_results,with_pins) · `describe_node`(node_id/class/function) · `add_blueprint_node`(node_id/node_type,pos_x,pos_y) · `connect_blueprint_nodes`(source_node_id,source_pin_name,target_node_id,target_pin_name) · `set_pin_default`(node_id,pin_name,value) · `break_node_link`(node_id,pin_name,target_node_id) · `insert_node_in_exec`(from_node_id,insert_node_id) · `delete_blueprint_node`(node_id) · `set_blueprint_node_property`(node_id,property_name,property_value)

**Discovery / export:** `search_parent_classes`(filter,max_results,include_blueprint_classes) · `search_types`(filter,kind,max_results) · `export_object`(object,path,filter,depth) · `export_asset`(asset,depth,sections) · `export_actor`(actor,depth,filter)
