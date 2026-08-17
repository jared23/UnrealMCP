# Spec: Animation (`/docs/reference/anim`, ~48 cmds — **high relevance to our game**)

Clean-room interface. Anim Blueprints, state machines, blend spaces, montages, notifies, curves,
skeleton slots. Directly useful for our locomotion/animation focus. Implement over `unreal` anim
APIs (`AnimBlueprint`, `BlendSpace`, `AnimMontage`, `AnimSequence`, skeleton).

**Anim Blueprint:** `create_anim_blueprint`(name,path,skeleton,template) · `get_anim_blueprint_info`(blueprint_path,depth) · `list_anim_graphs`(filter,kind) · `validate_anim_blueprint`(compile)

**State machines:** `add_anim_state_machine`(graph,name,pos_x/y) · `add_anim_state`(name,kind) · `add_anim_transition`(from,to,crossfade_duration) · `set_anim_entry_state`(state) · `get_anim_state_machine`(depth) · `remove_anim_state` · `remove_anim_transition`(from,to) · `set_anim_transition_property`(from,to,property,value) · `build_anim_state_machine`(name,states json,transitions json) — one-call build (supports dry-run)

**Nodes/layers:** `set_anim_node_pin_exposure`(node_id,property,show) · `bind_anim_node_function`(node_id,binding,function) · `add_anim_layer`(name) · `create_anim_layer_interface`(name,path,layers)

**Blend spaces / aim offsets:** `create_blend_space`(name,path,skeleton,kind) · `set_blend_space_axis`(axis,display_name,min,max,grid_divisions) · `add_blend_space_sample`(animation,x,y,rate_scale) · `remove_blend_space_sample`(index) · `get_blend_space` · `validate_anim_asset`(asset_path)

**Montages:** `create_anim_montage`(name,path,animation,skeleton) · `add_montage_slot`(slot) · `add_montage_segment`(animation,slot,play_rate) · `add_montage_section`(name,start_time,next_section) · `get_anim_montage`

**Notifies:** `list_anim_notify_classes`(kind,filter,max_results) · `create_anim_notify_class`(name,path,kind,notify_name,color) · `add_anim_notify_track`(track,color) · `add_anim_notify`(notify_class,time,track,duration) · `remove_anim_notify_track` · `remove_anim_notify`(index/name) · `get_anim_notifies`(track,details)

**Curves/sync:** `add_anim_curve`(name,curve_type) · `set_anim_curve_key`(name,time,value) · `get_anim_curves` · `add_anim_sync_marker`(name,time,track)

**Skeleton slots:** `get_anim_slots`(skeleton_path) · `add_anim_slot`(slot,group) · `remove_anim_slot` · `rename_anim_slot`(new_name) · `add_anim_slot_group`(group) · `remove_anim_slot_group`
