# Parity Audit — P6 Foundation Categories (M1–R5)

Read-only feature-parity audit. Spec files in `docs/spec/*.md` are the checklist; this maps each
spec'd feature to its implementing tool (matched by CAPABILITY, not literal name; parameterized folds
count as DONE). Status legend: ✅ DONE · ⚠️ PARTIAL · ❌ MISSING · ⛔ BLOCKED · ❓ UNCERTAIN.

Method: registered tools enumerated via `grep 'def <name>(ctx' MCP/UserTools/*.py`; synonym greps run
across `MCP/` and `Source/` before declaring MISSING.

---

## World Building (`world.md`)

Advanced category (spec says "not M1"). Landscape/terrain/spline/HLOD/lighting-build/nav-build suites
are entirely unimplemented; a few foliage + world-partition + data-layer basics exist.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] create_landscape | ❌ MISSING | no landscape tools exist |
| [ ] sculpt_landscape | ❌ MISSING | — |
| [ ] paint_landscape_layer | ❌ MISSING | — |
| [ ] paint_landscape_layers | ❌ MISSING | — |
| [ ] get_landscape_info | ❌ MISSING | — |
| [ ] edit_terrain_region | ❌ MISSING | no terrain tools |
| [ ] generate_terrain | ❌ MISSING | — |
| [ ] apply_terrain_process | ❌ MISSING | — |
| [ ] regenerate_terrain_region | ❌ MISSING | — |
| [ ] validate_landscape | ❌ MISSING | — |
| [ ] add_landscape_spline | ❌ MISSING | — |
| [ ] clear_landscape_splines | ❌ MISSING | — |
| [ ] add_river | ❌ MISSING | — |
| [ ] set_spline_points | ❌ MISSING | no generic spline component tools |
| [ ] set_spline_point | ❌ MISSING | — |
| [ ] get_spline | ❌ MISSING | — |
| [ ] validate_spline | ❌ MISSING | — |
| [x] create_foliage_type | ✅ DONE | `add_foliage_type` (foliage_write.py) |
| [x] scatter_foliage | ✅ DONE | `scatter_foliage_in_box` (foliage_write.py) |
| [ ] clear_foliage_instances | ❌ MISSING | only add/scatter exist, no clear |
| [ ] remove_foliage_type | ❌ MISSING | — |
| [ ] get_foliage_info | ❌ MISSING | — |
| [ ] validate_foliage | ❌ MISSING | — |
| [~] create_volume (generic class_path) | ⚠️ PARTIAL | specific spawners exist (`spawn_blocking_volume`, `spawn_trigger_*`, `spawn_post_process_volume`, `spawn_nav_*`) but no generic brush-geo volume creator |
| [ ] validate_volume | ❌ MISSING | — |
| [x] add_component | ✅ DONE | `add_component_to_actor` (editor_actor_components.py) |
| [ ] list_editor_modes | ❌ MISSING | — |
| [ ] set_editor_mode | ❌ MISSING | — |
| [ ] build_lighting | ❌ MISSING | — |
| [ ] lighting_build_status | ❌ MISSING | — |
| [ ] recapture_sky | ❌ MISSING | — |
| [ ] update_reflection_captures | ❌ MISSING | — |
| [ ] validate_lighting | ❌ MISSING | — |
| [ ] build_navigation | ❌ MISSING | navigation_read.py is read-only (get_navmesh_info etc.) |
| [ ] navigation_build_status | ❌ MISSING | — |
| [ ] validate_navigation | ❌ MISSING | — |
| [x] get_world_partition_info | ✅ DONE | `get_world_partition_info` (level_introspection.py) |
| [~] list_world_partition_actors | ⚠️ PARTIAL | `query_actors` lists actors but not WP-unloaded actors w/o loading |
| [ ] load_world_partition_region | ❌ MISSING | — |
| [ ] unload_world_partition_region | ❌ MISSING | — |
| [ ] pin_world_partition_actors | ❌ MISSING | — |
| [ ] set_actor_spatially_loaded | ❌ MISSING | only referenced in WP-info output text |
| [ ] create_data_layer | ❌ MISSING | datalayers.py has no create/remove |
| [x] list_data_layers | ✅ DONE | `list_data_layers` (datalayers.py) |
| [x] assign_actors_to_data_layer | ✅ DONE | `add_actors_to_data_layer` / `remove_actors_from_data_layer` |
| [~] set_data_layer_state | ⚠️ PARTIAL | `set_data_layer_visibility` covers visibility only, not editor-load / initial-runtime-state |
| [ ] remove_data_layer | ❌ MISSING | — |
| [ ] create_hlod_layer | ❌ MISSING | no HLOD tools |
| [ ] list_hlod_layers | ❌ MISSING | — |
| [ ] set_actor_hlod_layer | ❌ MISSING | — |
| [ ] build_world_partition | ❌ MISSING | — |
| [ ] set_world_partition_settings | ❌ MISSING | — |
| [ ] set_runtime_grid | ❌ MISSING | only referenced in WP-info output text |
| [ ] set_actor_runtime_grid | ❌ MISSING | — |
| [ ] validate_world_partition | ❌ MISSING | — |

**Tally (55): ✅ 6 · ⚠️ 3 · ❌ 46**

---

## Editor / Level Actors (`editor.md`)

M1 core — largely DONE. Gaps: level-file lifecycle (create/open/save-as), PIE, dedicated
validate_* suite, batch, redo, quit_editor, a couple viewport helpers.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] get_actors_in_level | ✅ DONE | `get_actors_in_level` |
| [x] find_actors_by_name | ✅ DONE | folds into `find_actors` (name_pattern) |
| [x] find_actors | ✅ DONE | `find_actors` |
| [x] get_selected_actors | ✅ DONE | `get_selected_actors` |
| [x] spawn_actor | ✅ DONE | `spawn_actor` (editor_level.py) |
| [~] spawn_blueprint_actor | ⚠️ PARTIAL | `spawn_actor_from_class` accepts a loadable/BP class path, but no dedicated blueprint_path+scale hierarchy spawner |
| [x] spawn_actor_from_class | ✅ DONE | `spawn_actor_from_class` (editor_spawn.py) |
| [x] spawn_actor_by_class | ✅ DONE | folds into `spawn_actor_from_class` (accepts class path) |
| [x] spawn_actors_batch | ✅ DONE | `spawn_actors_batch` |
| [x] set_actor_transform | ✅ DONE | `set_actor_transform` |
| [x] delete_actor | ✅ DONE | `delete_actor` |
| [x] delete_actors_batch | ✅ DONE | `delete_actors_batch` |
| [x] get_actor_properties | ✅ DONE | `get_actor_properties` (include_metadata supported) |
| [x] get_actor_property_metadata | ✅ DONE | folds into `describe_object` + `get_actor_properties(include_metadata)` |
| [x] set_actor_property | ✅ DONE | `set_actor_property` (also `set_object_property`) |
| [x] create_folder | ✅ DONE | `create_folder` |
| [x] delete_folder | ✅ DONE | `delete_folder` |
| [x] list_folders | ✅ DONE | `list_folders` |
| [ ] rename_folder | ❌ MISSING | no rename_folder tool |
| [x] move_actors_to_folder | ✅ DONE | `move_actors_to_folder` |
| [x] group_actors | ✅ DONE | `group_actors` |
| [x] ungroup_actors | ✅ DONE | `ungroup_actors` |
| [x] attach_actors | ✅ DONE | `attach_actors` |
| [x] detach_actors | ✅ DONE | `detach_actors` |
| [x] focus_viewport | ✅ DONE | `focus_viewport` |
| [ ] focus_asset_editor | ❌ MISSING | — |
| [x] take_screenshot | ✅ DONE | `take_screenshot` |
| [ ] capture_top_down | ❌ MISSING | — |
| [x] get_world_info | ✅ DONE | `get_world_info` |
| [x] list_level_templates | ✅ DONE | `list_level_templates` |
| [x] save_level | ✅ DONE | `save_level` |
| [x] get_scene_map | ✅ DONE | `get_scene_map` |
| [ ] create_level | ❌ MISSING | only referenced in list_level_templates help text |
| [ ] open_level | ❌ MISSING | — |
| [ ] save_level_as | ❌ MISSING | — |
| [ ] setup_default_scene | ❌ MISSING | — |
| [ ] play_in_editor | ❌ MISSING | — |
| [ ] stop_play_in_editor | ❌ MISSING | — |
| [x] get_material_slots | ✅ DONE | `get_material_slots` |
| [x] set_materials_batch | ✅ DONE | `set_materials_batch` |
| [x] get_mesh_info | ✅ DONE | `get_mesh_info` |
| [x] search_classes | ✅ DONE | `search_classes` |
| [ ] wait_for_compilation | ❌ MISSING | `compile_blueprint` exists but no idle-wait tool |
| [~] read_message_log | ⚠️ PARTIAL | `get_log_tail` reads output log, not the named MessageLog channels (PIE/MapCheck/…) |
| [ ] validate_project_assets | ❌ MISSING | — |
| [ ] validate_blueprint_compile | ❌ MISSING | — |
| [ ] validate_collision | ❌ MISSING | — |
| [ ] validate_replication | ❌ MISSING | — |
| [x] list_editor_windows | ✅ DONE | `list_editor_windows` |
| [x] get_undo_history | ✅ DONE | `get_undo_history` |
| [x] undo | ✅ DONE | `undo` (agent-scoped) |
| [ ] redo | ❌ MISSING | `undo` exists, no `redo` |
| [ ] batch | ❌ MISSING | no multi-op single-undo batch tool |
| [ ] quit_editor | ❌ MISSING | — |

**Tally (54): ✅ 35 · ⚠️ 2 · ❌ 17**

---

## Asset Management (`assets.md`)

Reads + core writes DONE; import + a few CB-navigation writes still deferred (see stale note in
assets.py header — save/delete/duplicate/rename actually live in assets_write.py and ARE done).

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] find_assets | ✅ DONE | `find_assets` (assets.py) |
| [x] list_assets | ✅ DONE | `list_assets` |
| [x] get_asset_info | ✅ DONE | `get_asset_info` |
| [x] get_asset_properties | ✅ DONE | `get_asset_properties` |
| [ ] set_asset_property | ❌ MISSING | explicitly deferred; use `set_object_property` for asset props as workaround |
| [x] find_references | ✅ DONE | `find_references` |
| [ ] open_asset | ❌ MISSING | deferred |
| [x] save_asset | ✅ DONE | `save_asset` (assets_write.py) |
| [ ] save_all | ❌ MISSING | deferred |
| [x] delete_asset | ✅ DONE | `delete_asset` (assets_write.py) |
| [x] duplicate_asset | ✅ DONE | `duplicate_asset` (assets_write.py) |
| [x] rename_asset | ✅ DONE | `rename_asset` (assets_write.py) |
| [ ] import_asset | ❌ MISSING | deferred |
| [ ] import_assets_batch | ❌ MISSING | deferred |
| [x] get_selected_assets | ✅ DONE | `get_selected_assets` |
| [ ] sync_browser | ❌ MISSING | deferred |

**Tally (16): ✅ 10 · ❌ 6**

---

## Bulk Asset Ops (`asset_ops.md`)

Entirely unimplemented. `delete_asset` (single) exists but no bulk/consolidate/redirector-fixup tools.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] move_assets | ❌ MISSING | — |
| [ ] delete_assets (bulk) | ❌ MISSING | only single `delete_asset` exists |
| [ ] fixup_redirectors | ❌ MISSING | — |
| [ ] find_replacement_candidates | ❌ MISSING | — |
| [ ] replace_references | ❌ MISSING | — |

**Tally (5): ✅ 0 · ❌ 5**

---

## Object Properties (`objects.md`)

Fully DONE — the unified reflection family.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] describe_object | ✅ DONE | `describe_object` (objects.py) |
| [x] get_object_property | ✅ DONE | `get_object_property` |
| [x] set_object_property | ✅ DONE | `set_object_property` |

**Tally (3): ✅ 3**

---

## Core (`core.md`)

Only `execute_python` implemented; multi-editor connect/session infra is unimplemented (spec itself
notes these matter only for addressing multiple editors, and dump_command_schema isn't relevant to
the Python-UserTools approach).

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] health_check | ❌ MISSING | — |
| [x] execute_python | ✅ DONE | `execute_python` (Commands/commands_python.py) |
| [ ] dump_command_schema | ❌ MISSING | not relevant to our approach (per spec note) |
| [ ] list_editors | ❌ MISSING | multi-editor infra |
| [ ] connect_editor | ❌ MISSING | multi-editor infra |
| [ ] disconnect_editor | ❌ MISSING | multi-editor infra |
| [ ] connection_status | ❌ MISSING | multi-editor infra |

**Tally (7): ✅ 1 · ❌ 6**

---

## Console Commands (`console.md`)

Cvar tooling is strong; general (non-cvar) command metadata/execution is intentionally limited —
console.py deliberately refuses to blind-run arbitrary exec commands.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] list_console_commands | ✅ DONE | `list_console_objects` (commands + cvars, filter/max_results) |
| [~] get_console_command_info | ⚠️ PARTIAL | `get_console_variable` returns rich metadata for cvars only, not exec commands |
| [~] console_command_exists | ⚠️ PARTIAL | `console_variable_exists` detects cvars only (states it misses pure exec commands) |
| [ ] execute_console_command (general) | ❌ MISSING | `set_console_variable` runs only `name value` cvar sets; no general command runner (by design). Workaround: `execute_python` |

**Tally (4): ✅ 1 · ⚠️ 2 · ❌ 1**

---

## Debug (`debug.md`)

Entirely unimplemented as spec'd. NOTE: repo's `debug.py` implements visual `debug_draw_*` gizmos —
a DIFFERENT feature set, not the Blueprint/BT debugger (breakpoints/watches/call-stack/live-instance)
this spec describes.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] set_mcp_debug | ❌ MISSING | — |
| [ ] get_mcp_token_stats | ❌ MISSING | — |
| [ ] set_breakpoint | ❌ MISSING | — |
| [ ] remove_breakpoint | ❌ MISSING | — |
| [ ] list_breakpoints | ❌ MISSING | — |
| [ ] get_debug_state | ❌ MISSING | — |
| [ ] debug_step | ❌ MISSING | — |
| [ ] get_call_stack | ❌ MISSING | — |
| [ ] get_execution_trace | ❌ MISSING | — |
| [ ] list_debug_objects | ❌ MISSING | — |
| [ ] set_debug_object | ❌ MISSING | — |
| [ ] inspect_debug_value | ❌ MISSING | — |
| [ ] set_pin_watch | ❌ MISSING | — |
| [ ] list_pin_watches | ❌ MISSING | — |
| [ ] set_bt_breakpoint | ❌ MISSING | — |
| [ ] remove_bt_breakpoint | ❌ MISSING | — |
| [ ] list_bt_breakpoints | ❌ MISSING | — |

**Tally (17): ✅ 0 · ❌ 17**

---

## Profiling (`profiling.md`)

Live frame-timing DONE; the Unreal Insights .utrace suite (UE5.7+) is unimplemented.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] performance_start_trace | ❌ MISSING | Insights trace suite absent |
| [ ] performance_stop_trace | ❌ MISSING | — |
| [ ] performance_analyze_insight | ❌ MISSING | — |
| [ ] performance_list_channels | ❌ MISSING | — |
| [ ] performance_toggle_channel | ❌ MISSING | — |
| [ ] performance_trace_bookmark | ❌ MISSING | — |
| [ ] performance_trace_snapshot | ❌ MISSING | — |
| [ ] performance_trace_screenshot | ❌ MISSING | — |
| [ ] performance_trace_object | ❌ MISSING | — |
| [x] performance_live_start | ✅ DONE | `performance_live_start` (profiling.py) |
| [x] performance_live_stop | ✅ DONE | `performance_live_stop` |

**Tally (11): ✅ 2 · ❌ 9**

---

## Project Settings (`projectsettings.md`)

Read DONE; the two write/search tools are unimplemented (the anticipated mutation gap).

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] search_project_settings | ❌ MISSING | `get_project_settings` with no section returns a catalog, but no fuzzy cross-container search tool |
| [x] get_project_settings | ✅ DONE | `get_project_settings` (project_info.py) — reflection read w/ metadata |
| [ ] set_project_settings | ❌ MISSING | no ini-persisting write tool |

**Tally (3): ✅ 1 · ❌ 2**

---

## Data Tables (`datatables.md`)

Nearly complete; only table creation missing.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] create_data_table | ❌ MISSING | referenced only in a code comment; no creator tool |
| [x] list_data_table_row_structs | ✅ DONE | `list_data_table_row_structs` |
| [x] get_data_table_rows | ✅ DONE | `get_data_table_rows` |
| [x] get_data_table_row | ✅ DONE | `get_data_table_row` |
| [x] get_data_table_schema | ✅ DONE | `get_data_table_schema` |
| [x] add_data_table_row | ✅ DONE | `add_data_table_row` |
| [x] update_data_table_row | ✅ DONE | `update_data_table_row` |
| [x] delete_data_table_row | ✅ DONE | `remove_data_table_row` |
| [x] rename_data_table_row | ✅ DONE | `rename_data_table_row` |
| [x] duplicate_data_table_row | ✅ DONE | `duplicate_data_table_row` |

**Tally (10): ✅ 9 · ❌ 1**

---

## Data Assets (`dataassets.md`)

Discovery present; creation and instanced-slot valid-types helper are gaps.

| spec feature | status | implementing tool / note |
|---|---|---|
| [~] list_data_asset_classes | ⚠️ PARTIAL | `search_classes(base_class=DataAsset)` gives class enumeration but not a dedicated DataAsset-subclass lister |
| [❓] create_data_asset | ❓ UNCERTAIN | base-plugin `create_object` is a generic UObject creator; unverified whether it correctly packages/saves a UDataAsset subclass at a path with initial_properties |
| [ ] get_property_valid_types | ❌ MISSING | `describe_object` exposes enum values but not valid instanced/dropdown class lists |
| [x] search_class_paths | ✅ DONE | `search_classes` (editor_discovery.py) — UClass path query |
| [x] list_data_assets | ✅ DONE | `list_data_assets` (dataassets.py) |

**Tally (5): ✅ 2 · ⚠️ 1 · ❓ 1 · ❌ 1**

---

## Blueprint Structs (`structs.md`)

Create/add/remove/describe/list DONE; in-place member modification missing.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] create_blueprint_struct | ✅ DONE | `create_user_defined_struct` (structs_write.py) |
| [x] add_blueprint_struct_variable | ✅ DONE | `add_struct_field` |
| [ ] set_blueprint_struct_variable | ❌ MISSING | no set/modify field tool (only add/remove) — can't rename/retype/re-default a member in place |
| [x] remove_blueprint_struct_variable | ✅ DONE | `remove_struct_field` |
| [x] describe_blueprint_struct | ✅ DONE | `describe_blueprint_struct` |
| [x] list_blueprint_structs | ✅ DONE | `list_blueprint_structs` |

**Tally (6): ✅ 5 · ❌ 1**

---

## Enhanced Input (`input.md`)

Create + read + basic key-mapping DONE; trigger/modifier editing, mapping mutation (remove/set), and
discovery listers are unimplemented — the largest foundation gap after world/debug/profiling.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] create_input_action | ✅ DONE | `create_input_action` (input_write.py) |
| [x] get_input_action | ✅ DONE | `describe_input_action` (input_read.py) |
| [ ] set_input_action_properties | ❌ MISSING | — |
| [ ] add_input_action_trigger | ❌ MISSING | — |
| [ ] add_input_action_modifier | ❌ MISSING | — |
| [ ] remove_input_action_trigger | ❌ MISSING | — |
| [ ] remove_input_action_modifier | ❌ MISSING | — |
| [x] list_input_actions | ✅ DONE | `list_input_actions` |
| [x] create_input_mapping_context | ✅ DONE | `create_input_mapping_context` |
| [x] get_input_mapping_context | ✅ DONE | `describe_input_mapping_context` |
| [~] add_key_mapping | ⚠️ PARTIAL | `add_mapping_to_context` adds key→action, but no inline triggers/modifiers params |
| [ ] remove_key_mapping | ❌ MISSING | — |
| [ ] set_key_mapping | ❌ MISSING | — |
| [ ] add_mapping_trigger | ❌ MISSING | — |
| [ ] add_mapping_modifier | ❌ MISSING | — |
| [ ] remove_mapping_trigger | ❌ MISSING | — |
| [ ] remove_mapping_modifier | ❌ MISSING | — |
| [x] list_input_mapping_contexts | ✅ DONE | `list_input_mapping_contexts` |
| [ ] list_trigger_types | ❌ MISSING | — |
| [ ] list_modifier_types | ❌ MISSING | — |
| [ ] list_input_keys | ❌ MISSING | — |

**Tally (21): ✅ 6 · ⚠️ 1 · ❌ 14**

---

## Grand Total

| category | total | ✅ | ⚠️ | ❓ | ❌ |
|---|---|---|---|---|---|
| world | 55 | 6 | 3 | 0 | 46 |
| editor | 54 | 35 | 2 | 0 | 17 |
| assets | 16 | 10 | 0 | 0 | 6 |
| asset_ops | 5 | 0 | 0 | 0 | 5 |
| objects | 3 | 3 | 0 | 0 | 0 |
| core | 7 | 1 | 0 | 0 | 6 |
| console | 4 | 1 | 2 | 0 | 1 |
| debug | 17 | 0 | 0 | 0 | 17 |
| profiling | 11 | 2 | 0 | 0 | 9 |
| projectsettings | 3 | 1 | 0 | 0 | 2 |
| datatables | 10 | 9 | 0 | 0 | 1 |
| dataassets | 5 | 2 | 1 | 1 | 1 |
| structs | 6 | 5 | 0 | 0 | 1 |
| input | 21 | 6 | 1 | 0 | 14 |
| **TOTAL** | **217** | **81** | **9** | **1** | **126** |

Note: `world`, `debug`, and `profiling` (advanced / Insights-tracing / BP-debugger categories) account
for 72 of the 126 MISSING. The genuinely-foundation M1 categories (editor, assets, objects, datatables,
structs, dataassets) are largely DONE; the notable foundation gaps are input-action/mapping editing,
projectsettings mutation, asset_ops bulk tools, and assets import/CB-navigation writes.
