# Spec: PCG — Procedural Content Generation (`/docs/reference/pcg`, ~90 cmds — later)

Clean-room. Big category (graph editing + component generation + partitioning + inspection).
Names + groups below (params-per-command to be filled when we build PCG). Implement over
`unreal.PCGGraph`, PCG subsystem/component APIs.

**Graph mgmt:** create_pcg_graph · create_pcg_graph_instance · create_pcg_graph_from_template · read_pcg_graph · validate_pcg_graph · layout_pcg_graph(topological/grid/compact) · build_pcg_graph (atomic nodes+edges+auto-layout)

**Nodes:** add_pcg_node · delete_pcg_node · get_pcg_node · get_pcg_node_connections · set_pcg_node_property · set_pcg_node_position · set_pcg_node_comment

**Wiring:** connect_pcg_pins · disconnect_pcg_pin · disconnect_pcg_edge · trace_pcg_connection(up/down/both)

**Discovery:** search_pcg_nodes · list_pcg_node_categories · list_pcg_advanced_nodes · list_pcg_templates · get_pcg_node_type_info · list_pcg_kernel_types

**Properties/params (graph + node):** get_pcg_property_valid_values · get_pcg_graph_parameters · get_pcg_graph_properties · set_pcg_graph_property · get_pcg_node_property · set_pcg_node_property_path(dotted) · list_pcg_node_property_paths · add/remove_pcg_node_property_array_element · set_pcg_node_property_class · add/remove_pcg_dynamic_input_pin

**Graph parameters:** add_pcg_graph_parameter · remove/rename_pcg_graph_parameter · set_pcg_graph_parameter_value · set_pcg_graph_parameter_type

**Subgraph:** set_pcg_subgraph_target · get/set/reset_pcg_subgraph_override

**Compute (HLSL):** create/get/set_pcg_compute_source · add/remove_pcg_compute_source_additional · list_pcg_compute_sources · set_pcg_custom_hlsl_kernel_type

**Specialized:** create_pcg_builder_settings · set_pcg_static_mesh_spawner_meshes(weighted)

**Component ops:** pcg_generate_component · pcg_cleanup_component · pcg_generate_local · pcg_cleanup_local(+_immediate) · pcg_cancel_component_generation · pcg_set_component_graph · pcg_get_generated_output · pcg_clear_pcg_link

**Partitioning:** pcg_is_partitioned · pcg_list_partition_actors · pcg_get_partition_actor_info · pcg_aggregate_partition_output

**Async scheduling:** pcg_schedule_graph · pcg_schedule_component · pcg_schedule_cleanup · pcg_schedule_refresh

**Bulk (editor):** pcg_generate_all_components · pcg_cleanup_all_components · pcg_refresh_all_components_filtered · pcg_refresh_runtime_gen_sources · pcg_dirty_runtime_gen_sources(5.8+)

**Cache:** pcg_flush_cache · pcg_build/clear_landscape_cache · pcg_runtime_generation_refresh · pcg_refresh_pcg_runtime_component

**Inspection/debug:** pcg_is_inspecting · pcg_enable/disable_node_inspection · pcg_was_node_executed · pcg_has_node_produced_data · pcg_get_node_inactive_pin_mask · pcg_did_node_trigger_gpu_to_cpu_readback / cpu_to_gpu_upload · pcg_node_applied_data_overrides · pcg_inspect_node_output · pcg_inspect_partition_node_output · pcg_list_executed_nodes · pcg_clear_inspection_data

**Misc:** add_pcg_comment
