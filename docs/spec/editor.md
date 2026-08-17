# Spec: Editor / Level Actors (`/docs/reference/editor`, 54 cmds — **M1 CORE**)

Interface reference (clean-room; params/shape only, no source). **This is the M1 foundation** —
our first commands should match these names/signatures. Implement over `unreal.EditorActorSubsystem`,
`EditorLevelLibrary`, `UnrealEditorSubsystem`, `LevelEditorSubsystem`, reflection. `req`/`opt`.

### Discovery & selection
- `get_actors_in_level` — list all actors — (none)
- `find_actors_by_name` — by name pattern — pattern(str,req)
- `find_actors` — flexible search — name_pattern(str,opt); label_pattern(str,opt); class_filter(str,opt); tag(str,opt); exact_class(bool,opt); max_results(int,opt); include_transform(bool,opt)
- `get_selected_actors` — current viewport selection — (none)

### Spawning
- `spawn_actor` — spawn in level — name(str,req); actor_type(str,opt,default StaticMeshActor); location(json[x,y,z],opt); rotation(json[p,y,r],opt); scale(json,opt); static_mesh(str,opt)
- `spawn_blueprint_actor` — BP instance w/ full hierarchy — blueprint_path(str,req); location/rotation/scale(json,opt); name(str,opt)
- `spawn_actor_from_class` — from class name (scalar coords) — class_name(str,req); location_x/y/z(float,opt); rotation_yaw/pitch/roll(float,opt)
- `spawn_actor_by_class` — any AActor subclass (path/BP/short) — class_path(str,req); name(str,opt); location/rotation/scale(json,opt)
- `spawn_actors_batch` — many from array — actors(json[{mesh,location,rotation,scale,name,type}],req); mesh(str,opt); type(str,opt); name_prefix(str,opt); folder(str,opt); return_names(bool,opt)

### Transform / delete
- `set_actor_transform` — set loc/rot/scale — name(str,req); location/rotation/scale(json,opt)
- `delete_actor` — delete one — name(str,req)
- `delete_actors_batch` — by prefix or list — name_prefix(str,opt); names(json,opt)

### Properties (dotted-path reflection)
- `get_actor_properties` — read props of placed actor — actor_label(str,req); filter(str,opt); include_components(bool,opt); flat(bool,opt); max_depth(int,opt); include_metadata(bool,opt); expand_arrays(bool,opt); array_element_limit(int,opt); category(str,opt); include_inherited(bool,opt); max_entries(int,opt); cursor(int,opt)
- `get_actor_property_metadata` — type/clamp/enum metadata — actor_label(str,req); property_path(str,opt); filter(str,opt); category(str,opt); depth(int,opt); expand_enums(bool,opt); include_inherited(bool,opt); descend_into_objects(bool,opt); max_entries(int,opt); cursor(int,opt); component_name(str,opt)
- `set_actor_property` — set at nested path — actor_label(str,req); property_path(str,req); property_value(json,req); component_name(str,opt)

### Folders / grouping / attachment
- `create_folder`(folder,req) · `delete_folder`(folder,req; recursive,opt) · `list_folders`(filter,opt) · `rename_folder`(folder,req; new_folder,req)
- `move_actors_to_folder`(folder,req; name_prefix,opt; names,opt)
- `group_actors` / `ungroup_actors`(name_prefix,opt; names,opt)
- `attach_actors`(parent,req; name_prefix,opt; names,opt) · `detach_actors`(name_prefix,opt; names,opt) — keep world transform

### Viewport / camera / screenshots
- `focus_viewport` — frame actor / move camera — actor(str,opt); direction(str,opt: top/bottom/front/back/left/right/iso); pitch/yaw(float,opt); fill(float,opt 0-1); distance(float,opt); location(str,opt); rotation(str,opt); realtime(bool,opt)
- `focus_asset_editor` — bring tab to front — asset_path(str,req)
- `take_screenshot` — viewport/window/asset editor → PNG — file_path(str,opt); mode(str,opt: viewport/window); asset_path(str,opt); window(str,opt); focus(bool,opt)
- `capture_top_down` — off-screen ortho top-down PNG — file_path(str,opt); resolution(int,opt); padding(float,opt); area_width(float,opt); ortho_width(float,opt); pitch(float,opt); yaw(float,opt); center(str,opt)

### Level management
- `get_world_info`(none) · `list_level_templates`(none) · `save_level`(none)
- `get_scene_map` — clustered level understanding — class_filter(str,opt); label_filter(str,opt); region_grid(bool,opt); grid_size(int,opt); cell_size(float,opt); drill(int,opt); page(int,opt); page_size(int,opt); export(bool,opt)
- `create_level`(asset_path,req; template,opt; partitioned,opt) · `open_level`(asset_path,req) · `save_level_as`(asset_path,req)
- `setup_default_scene` — drop lit environment — preset(str,opt: clear/noon/sunset/overcast/snowy/night); sky/floor/clouds/post_process/player_start(bool,opt); sun_pitch/yaw/roll(float,opt); floor_size(float,opt); folder(str,opt)

### Play / simulation
- `play_in_editor`(new_window,opt; simulate,opt) · `stop_play_in_editor`(none)

### Materials (slot-level) / mesh / class discovery
- `get_material_slots` — list element slots — actor/actor_label/blueprint_path/asset_path/component/component_name/mesh_path(str,opt)
- `set_materials_batch` — assign per slot — assignments(json,opt); actor/blueprint_path/asset_path/name_prefix/component(str,opt); slot(int,opt); slot_name(str,opt); all_slots(bool,opt); material(str,opt)
- `get_mesh_info` — native shape/bounds/pivot — mesh_path(str,req); include_collision(bool,opt); include_sockets(bool,opt)
- `search_classes` — search UClasses — query(str,opt); base_class(str,opt); max_results(int,opt); include_abstract(bool,opt)

### Validation / compilation (value-adds)
- `wait_for_compilation`(timeout_seconds,opt) — block until compile idle (read metrics only after)
- `read_message_log`(log_name,req: PIE/BlueprintLog/AssetCheck/MapCheck/LoadErrors; severity,opt; max_results,opt)
- `validate_project_assets`(path,opt; assets,opt; filter,opt; usecase,opt; max_assets,opt; max_results,opt; load_assets,opt; skip_excluded_directories,opt)
- `validate_blueprint_compile`(blueprint_path,req; max_results,opt)
- `validate_collision`(path,opt; filter,opt; max_results,opt)
- `validate_replication`(class_path,req; max_results,opt) — static multiplayer-bug check, no PIE

### Editor UI / undo / batch / control
- `list_editor_windows`(none)
- `get_undo_history`(max_results,opt; filter,opt; scope,opt: all/mine/others; details,opt)
- `undo`(count,opt; scope,opt: mine/any) · `redo`(count,opt; scope,opt) — agent can revert only its own work
- `batch` — many commands, one round trip + one undo — steps(json[{command,params}],req); atomic(bool,opt); continue_on_error(bool,opt); dry_run(bool,opt); include_results(bool,opt). Supports `"$2.asset_path"` output substitution (data only, not scripting).
- `quit_editor`(save,opt)

## → Impact on M1
Re-align our M1 command list (docs/drafts/m1_core_reference.py) to THESE names/signatures. Adopt as design wins: **`batch`** (atomic multi-op + single undo), **agent-scoped `undo`** (revert only our edits), **`get_scene_map`** (cheap level context), **`find_actors`** rich filters, and the **`validate_*`** suite for read-after-write confidence.
