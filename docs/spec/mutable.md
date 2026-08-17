# Spec: Mutable — Customizable Characters (`/docs/reference/mutable`, ~35 cmds — later)

Clean-room. Customizable Object graphs, instances, parameters, macro libraries. Implement over the
Mutable plugin editor APIs.

**Assets:** create_customizable_object(path) · create_customizable_object_instance(object,instance) · create_mutable_macro_library(path)

**Graph:** get_mutable_graph(object,macro,filter,max_results,verbosity) · add_mutable_node(object,node_type,pos,from_node_guid,from_pin) · connect_mutable_nodes(from_node_guid,from_pin,to_node_guid,to_pin) · disconnect_mutable_pin(node_guid,pin,direction) · delete_mutable_node(node_guid) · build_mutable_graph(nodes,connections,layout,dry_run,continue_on_error) · layout_mutable_graph(insert_reroutes,remove_existing_reroutes)

**Nodes:** search_mutable_nodes(queries,filter,category,max_results) · describe_mutable_node(node_types,include_properties) · list_mutable_node_categories(include_nodes) · get_mutable_node(node_guids,include_pins,include_properties) · set_mutable_node_property(properties) · add_mutable_node_pin(node_guid,count)

**Compile/validate:** compile_customizable_object(force,optimization_level,texture_compression,gather_references) · validate_customizable_object(compile,report_unconnected_inputs)

**Parameters/states:** list_mutable_parameters(filter,include_options) · get_mutable_parameter(instance,names,range_index) · set_mutable_parameter(instance,parameters) · reset_mutable_parameters(names) · copy_mutable_parameters · paste_mutable_parameters(descriptor) · list_mutable_states · set_mutable_state(instance,state)

**Hierarchy:** link_mutable_child_object(parent,children,unlink) · list_mutable_child_objects(group)

**Update/export:** update_mutable_instance(wait,timeout_seconds,force_high_priority) · export_mutable_object(file_path,include_graph,include_parameters,include_referenced_assets) · set_mutable_parameter_ui_metadata(edits)

**Macro library:** list_mutable_macros(filter,include_io) · add_mutable_macro(names,description) · remove_mutable_macro(names) · set_mutable_macro_io(macro,variables) · add_mutable_macro_instance(macro_library,macro,pos)
