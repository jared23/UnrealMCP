# Spec: World Building (`/docs/reference/world`, 55 cmds — planned, advanced)

Interface reference (clean-room; params/shape only, no source). Ours to implement over Unreal's
Landscape, Foliage, WorldPartition, and lighting/nav build APIs. **Advanced category — not M1.**
Note: basic Level-Actor ops (spawn/select/transform/delete) are NOT here — they live on another
page (to confirm: `editor`/`core`). `req`/`opt`. `str`,`int`,`float`,`bool`.

### Landscape — create / sculpt / paint / terrain-gen
- `create_landscape` — new landscape at resolution — name(str,opt); section_size(int,opt); sections_per_component(int,opt); component_count_x/y(int,opt); location(str,opt); scale(str,opt); material(str,opt)
- `sculpt_landscape` — sculpt heights by shape — actor(str,opt); shape(str,req); height(float,opt); center(str,opt); radius(float,opt); seed(int,opt); additive(bool,opt)
- `paint_landscape_layer` — paint one weight layer — actor(str,opt); layer_name(str,req); layer_info_path(str,opt); rule(str,opt); threshold(float,opt); weight(int,opt); blend_range(float,opt); edge_noise(float,opt)
- `paint_landscape_layers` — paint all layers in one pass — actor(str,opt); layers(str,req); layer_info_path(str,opt); slope_smoothing(int,opt); edge_noise(float,opt)
- `get_landscape_info` — read resolution/scale/height-range/layers — actor(str,opt)
- `edit_terrain_region` — one op on a circular region — actor(str,opt); operation(str,req); center/radius/falloff/amount/height(float/str,opt); use_height(bool,opt); radius2(int,opt); iterations/step_height/feature_scale(float,opt); seed(int,opt)
- `generate_terrain` — full eroded terrain (ridged base + droplet erosion) — actor(str,opt); seed(int,opt); + ~30 tuning params: amplitude, sharpness, warp, feature_scale, erosion(+radius), talus_angle, thermal_iterations, layers, hydrology, channel_density, river_depth, uplift, hillslope_diffusion, detail(+scale/slope_bias), river_start/to/meander/valley_width, concavity, …
- `apply_terrain_process` — geomorphic process (glacial/snow/coastal/stratify) — actor(str,opt); process(str,req); + process-specific: equilibrium_line_altitude, mass_balance_gradient, flow_rate, glacial_erodibility, cirque_strength, iterations, snow_line, repose_angle_degrees, wind_direction/strength, sea_level, bench_width, tilt_degrees, …
- `regenerate_terrain_region` — seamless region regen (poisson/feather) — actor(str,opt); center(str,req); radius(float,req); blend(str,opt); seed(int,opt); amplitude/sharpness/warp/feature_scale/erosion/talus_angle(float,opt); thermal_iterations(int,opt); falloff_width(float,opt)
- `validate_landscape` — verify usable/configured — actor(str,opt)

### Landscape splines
- `add_landscape_spline` — non-destructive deform along path — actor(str,opt); points(str,req); width/side_falloff/end_falloff(float,opt); raise/lower/apply(bool,opt); layer_name(str,opt); height_offset(float,opt); point_spacing(float,opt); height_smoothing(int,opt)
- `clear_landscape_splines` — remove all — actor(str,opt)
- `add_river` — carve river along steepest descent — actor(str,opt); start(str,opt); from_peak(bool,opt); depth/width_scale/valley_width(float,opt); tributaries(bool,opt); to(str,opt); + ~20 hydraulic params (climb_penalty, flow_attraction, excavation_weight, channel_depth, carve, water(+depth/material), layers, …)

### Spline components (generic)
- `set_spline_points` — set/append control points — actor(str,req); component(str,opt); points(str,req); space(str,opt); point_type(str,opt); append(bool,opt); closed_loop(bool,opt)
- `set_spline_point` — edit one point (loc/tangents/rot/scale) — actor(str,req); component(str,opt); index(int,req); location/arrive_tangent/leave_tangent/rotation/scale(str,opt); point_type(str,opt); space(str,opt)
- `get_spline` — read points/length/sampled transforms — actor(str,req); component(str,opt); sample_count(int,opt)
- `validate_spline` — verify usable — actor(str,req); component(str,opt)

### Foliage
- `create_foliage_type` — foliage-type asset from mesh — mesh_path(str,req); asset_path(str,req)
- `scatter_foliage` — reproducible pattern placement — foliage_type(str,req); pattern(str,opt); center(str,opt); radius(float,opt); count(int,opt); spacing(float,opt); seed(int,opt); spline_actor(str,opt); spline_offset(float,opt); trace_height(float,opt)
- `clear_foliage_instances` — remove instances, keep types — foliage_type(str,opt)
- `remove_foliage_type` — remove type entirely — foliage_type(str,req)
- `get_foliage_info` — list types + instance counts — filter(str,opt); max_results(int,opt)
- `validate_foliage` — verify renders — foliage_type(str,opt)

### Volumes / components / editor modes
- `create_volume` — spawn volume w/ real brush geo — class_path(str,req); name(str,opt); location/rotation/size(str,opt); unbound(bool,opt); folder(str,opt)
- `validate_volume` — verify brush geo — actor_label(str,req)
- `add_component` — add component to placed actor — actor(str,req); component_class(str,opt); component_type(str,opt); component_name(str,opt); attach_parent(str,opt)
- `list_editor_modes` — list modes + active — filter(str,opt); include_hidden(bool,opt)
- `set_editor_mode` — switch level-editor mode — mode_id(str,req)

### Lighting & navigation builds
- `build_lighting` — start Lightmass build — quality(str,opt); current_level_only(bool,opt)
- `lighting_build_status` — poll progress — pump(bool,opt)
- `recapture_sky` — recapture sky lights — actor(str,opt)
- `update_reflection_captures` — flush/rebake reflections — full_build(bool,opt)
- `validate_lighting` — verify built — (none)
- `build_navigation` — start async navmesh build — (none)
- `navigation_build_status` — poll navmesh build — (none)
- `validate_navigation` — verify pathable — test_point(str,opt); project_extent(str,opt)

### World Partition / Data Layers / HLOD
- `get_world_partition_info` — grids/data-layers/loaded counts — (none)
- `list_world_partition_actors` — list actors w/o loading — filter/class_path/region(str,opt); loaded_only(bool,opt); max_results(int,opt)
- `load_world_partition_region` / `unload_world_partition_region` — region(str,opt); center(str,opt); radius(float,opt); name(str,opt)
- `pin_world_partition_actors` — keep loaded regardless of camera — filter/class_path(str,opt); unpin(bool,opt); max_results(int,opt)
- `set_actor_spatially_loaded` — stream vs always-load — actor_label(str,req); spatially_loaded(bool,opt)
- `create_data_layer` — asset+instance — asset_path(str,req); layer_type(str,opt); debug_color(str,opt); supports_actor_filters(bool,opt); parent(str,opt)
- `list_data_layers` — list w/ type/parent/state/counts — filter(str,opt)
- `assign_actors_to_data_layer` — add/remove matched actors — data_layer(str,req); filter/class_path(str,opt); remove(bool,opt); max_results(int,opt)
- `set_data_layer_state` — visibility/editor-load/runtime — data_layer(str,req); visible/loaded_in_editor/initial_runtime_state(str,opt)
- `remove_data_layer` — remove instance — data_layer(str,req)
- `create_hlod_layer` — HLOD layer asset — asset_path(str,req); layer_type(str,opt); cell_size(int,opt); loading_range(float,opt); spatially_loaded(bool,opt); parent_layer(str,opt)
- `list_hlod_layers` — list — filter(str,opt)
- `set_actor_hlod_layer` — assign to matched actors — filter/class_path(str,opt); hlod_layer(str,opt); max_results(int,opt)
- `build_world_partition` — run partition builder — builder(str,req); confirm(bool,opt)
- `set_world_partition_settings` — set UWorldPartition prop — property(str,req); value(str,req)
- `set_runtime_grid` — add/edit/remove named grid — grid_name(str,req); cell_size(int,opt); loading_range(float,opt); priority(int,opt); remove(bool,opt)
- `set_actor_runtime_grid` — place actors on grid — filter/class_path(str,opt); grid_name(str,req); max_results(int,opt)
- `validate_world_partition` — verify configured — (none)
