# Spec: Control Rig (`/docs/reference/controlrig`, ~120 cmds — **high relevance: rigging/locomotion**)

Clean-room interface. Big category; broad coverage below (expand params when we build it). Implement
over `unreal.ControlRigBlueprint`, RigHierarchy, RigVM, control-rig editor APIs. Physics/motion probes
and deformation validation are especially useful for our animation/RL locomotion work.

**Rig creation:** `create_control_rig`(asset_path,modular) · `create_control_rig_from_skeleton`(asset_path,skeleton_path,import_curves) · `create_control_rig_module`(asset_path,skeleton_path) · `set_rig_preview_mesh`(mesh_path)

**Hierarchy/elements:** `add_rig_bone`(name,parent,transform) · `add_rig_null`(name,parent) · `add_rig_socket`(name,parent,transform) · `add_rig_curve`(name,value) · `set_rig_element_parent`(name,parent,maintain_global) · `set_rig_element_transform`(name,transform,space) · `remove_rig_element`(name,type) · `rename_rig_element`(name,new_name) · `build_rig_hierarchy`(elements,clear_existing) · `get_rig_hierarchy`(filter,include_transforms)

**Controls:** `add_rig_control`(name,control_type,parent,value) · `add_rig_animation_channel`(name,parent_control,control_type) · `set_rig_control_settings`(name,settings) · `set_rig_control_value`(name,value,value_type) · `move_rig_control`(name,transform,relative) · `set_rig_control_offset`(name,transform) · `set_rig_control_shape`(name,shape_name,shape_color) · `list_rig_control_shapes`(filter) · `fit_rig_control_shapes`(controls,margin) · `validate_rig_gizmos`(mesh_path,live)

**RigVM graph:** `add_rig_vm_node`(node_kind,node_type,pos_x,pos_y) · `connect_rig_vm_nodes`(source_node,source_pin,target_node,target_pin) · `disconnect_rig_vm_nodes` · `set_rig_vm_pin_default`(node,pin,value) · `set_rig_vm_node_position` · `remove_rig_vm_node` · `build_rig_vm_graph`(nodes,connections,auto_layout) · `layout_rig_vm_graph`(spacing_x,spacing_y) · `get_rig_vm_graph_nodes`(verbosity,type_filter) · `search_rig_vm_nodes`(filter,max_results)

**Functions:** `create_rig_vm_function`(name,mutable,is_public) · `add_rig_vm_function_node`(function_name,pos) · `collapse_rig_vm_nodes`(node_names,collapse_name) · `promote_rig_vm_node`(node,direction) · `expand_rig_vm_node`(node)

**Modular rigs:** `add_rig_module`(module_name,module_asset_path) · `connect_rig_module_connector`(connector,target_name) · `auto_connect_rig_modules`(module_names,replace_existing) · `set_rig_module_config`(module_name,path,value) · `bind_rig_module_variable`(module_name,variable,source_path) · `mirror_rig_module`(mirror_axis,search,replace) · `list_rig_modules` · `list_rig_module_assets`(filter,tag,detailed)

**Physics/validation:** `validate_rig_physics`(deviation_threshold_cm) · `validate_rig_deformation`(scale_tolerance,shear_tolerance_deg) · `start_rig_physics_probe`(control,shake_cm,settle_frames) · `get_rig_physics_probe_report`(residual_threshold_cm,max_bones) · `measure_mesh_penetration`(chain_filter,body_filter,margin_cm) · `get_skeletal_bone_bounds`(mesh_path,filter,weight_mode) · `fit_rig_chain_collision`(module_name,margin_cm,shape)

**Motion/perf testing:** `start_rig_motion_capture`(bones,frames) · `get_rig_motion_report`(max_bones) · `simulate_rig`(anim_sequence,frames,channels,segment_pairs) · `profile_rig`(frames,delta_time,top_nodes) · `play_rig_preview_animation`(anim_path,solve_mode,loop) · `stop_rig_preview_animation`

**Analysis/debug:** `validate_rig_controls`(include_bones,max_results) · `validate_rig_graph`(graph_name,include_runtime_log) · `analyze_rig_io`(frames,delta_time) · `analyze_rig_control_impact`(controls,offset_cm) · `get_rig_pose`(bones,threshold_cm) · `analyze_rig_module_asset`(module_asset_path,graph_filter) · `export_control_rig`(inline)

**Misc:** `select_rig_elements`(names,select,clear_selection) · `place_rig_pole_vector`(control,root_bone,mid_bone,end_bone) · `add_rig_jiggle_bob`(flesh_bone,module_name,strength) · `set_rig_autosave`(enabled)
