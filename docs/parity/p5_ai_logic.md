# Parity Audit — P5 AI / Logic categories

Read-only feature-parity audit of `docs/spec/{behaviortree,statetree,eqs,gas,pcg,mutable}.md`
against implemented MCP tools. Status legend: ✅ DONE · ⚠️ PARTIAL · ❌ MISSING · ⛔ BLOCKED · ❓ UNCERTAIN.
"Fold" = one implemented tool covers several spec features via parameters/rich output (counts as DONE).
Implementing modules: `MCP/UserTools/{ai_read,ai_write,bt_write,statetree,eqs,gas,gameplay_tags_read,gameplay_tags_write,pcg,mutable}.py`.

## Behavior Trees

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] create_behavior_tree | ✅ DONE | `create_behavior_tree` (ai_write) |
| [x] create_blackboard | ✅ DONE | `create_blackboard` (ai_write) |
| [x] set_bt_blackboard | ✅ DONE | `set_behavior_tree_blackboard` (ai_write) |
| [x] get_behavior_tree | ✅ DONE | `get_behavior_tree_info` (ai_read) |
| [x] get_blackboard | ✅ DONE | `get_blackboard_info` (ai_read) |
| [x] add_bt_node | ✅ DONE | fold: `add_bt_task` + `add_bt_child_composite` (bt_write) by node kind |
| [x] add_bt_subnode | ✅ DONE | fold: `add_bt_decorator` + `add_bt_service` (bt_write) |
| [x] add_blackboard_keys | ✅ DONE | `add_blackboard_key` (ai_write) |
| [ ] search_bt_nodes | ⚠️ PARTIAL | only generic `search_classes`; no BT-scoped ranked node search |
| [ ] get_bt_node_type_info | ⚠️ PARTIAL | only generic `search_classes`/`describe_object`; no BT node props/defaults tool |
| [ ] connect_bt_nodes | ⚠️ PARTIAL | parenting is set via `parent_path`/`child_index` at add-time; no standalone reconnect/reparent |
| [ ] compile_behavior_tree | ⚠️ PARTIAL | `sync_bt_editor_graph` rebuilds+saves editor graph (round-trip), not a true compile/validate |
| [ ] arrange_behavior_tree | ⚠️ PARTIAL | `sync_bt_editor_graph` does the graph rebuild/round-trip layout |
| [ ] validate_behavior_tree | ❌ MISSING | no validator |
| [ ] remove_bt_node | ❌ MISSING | no node removal tool |
| [ ] set_bt_node_property | ❌ MISSING | no BT-node property setter |
| [ ] add_bt_composite_decorator | ❌ MISSING | `add_bt_decorator` is single-slot only; no AND/OR/NOT composite decorator |
| [ ] create_bt_node_blueprint | ❌ MISSING | no BT node BP creation |
| [ ] list_blackboard_key_types | ❌ MISSING | no key-type enumerator |
| [ ] remove_blackboard_key | ❌ MISSING | no key removal |
| [ ] set_blackboard_key | ❌ MISSING | no key rename/retype |
| [ ] get_bt_runtime | ❌ MISSING | no PIE runtime inspection |
| [ ] control_bt_runtime | ❌ MISSING | no runtime start/stop/pause |
| [ ] set_bt_dynamic_subtree | ❌ MISSING | no dynamic subtree injection |

**Tally (24):** ✅ 8 · ⚠️ 5 · ❌ 11 · ⛔ 0

## StateTree

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] get_statetree_info | ✅ DONE | `get_state_tree_info` (statetree) |
| [x] get_statetree_states | ✅ DONE | `get_state_tree_states` |
| [x] get_statetree_state | ✅ DONE | `get_state_tree_state` |
| [x] get_statetree_evaluators | ✅ DONE | fold: `get_state_tree_info` returns `evaluators[]` |
| [x] get_statetree_global_tasks | ✅ DONE | fold: `get_state_tree_info` returns `global_tasks[]` |
| [x] get_statetree_bindings | ✅ DONE | `get_state_tree_bindings` |
| [x] get_statetree_full_info | ✅ DONE | `get_state_tree_info` |
| [x] search_statetree_nodes | ✅ DONE | `list_state_tree_node_classes` (kind/path filter) + `list_state_tree_native_nodes` |
| [x] list_statetree_node_types | ✅ DONE | `list_state_tree_node_classes` / `list_state_tree_native_nodes` |
| [x] list_statetree_schemas | ✅ DONE | `list_state_tree_schemas` |
| [ ] export_statetree | ⚠️ PARTIAL | `get_state_tree_info`/`get_state_tree_states` emit structured JSON; no single full-export tool |
| [ ] get_statetree_parameters | ⚠️ PARTIAL | param values surface in `get_state_tree_state`; no dedicated parameters query |
| [ ] get_statetree_binding_sources | ⚠️ PARTIAL | `get_state_tree_bindings` lists all bindings; not a per-target source enumerator |
| [ ] get_statetree_transition_targets | ⚠️ PARTIAL | transitions shown inside `get_state_tree_state`; no target enumerator |
| [ ] get_statetree_node (by GUID) | ❌ MISSING | reads are by state name, not by node GUID |
| [ ] search_statetree_properties | ❌ MISSING | no property search |
| [ ] list_statetree_enum_values | ❌ MISSING | no enum-value listing |
| [ ] create_statetree | ❌ MISSING | read-only batch; no write API built |
| [ ] add_statetree_state | ❌ MISSING | read-only batch |
| [ ] add_statetree_task | ❌ MISSING | read-only batch |
| [ ] add_statetree_evaluator | ❌ MISSING | read-only batch |
| [ ] add_statetree_global_task | ❌ MISSING | read-only batch |
| [ ] add_statetree_condition | ❌ MISSING | read-only batch |
| [ ] add_statetree_consideration | ❌ MISSING | read-only batch |
| [ ] add_statetree_transition | ❌ MISSING | read-only batch |
| [ ] add_statetree_parameter | ❌ MISSING | read-only batch |
| [ ] add_statetree_binding | ❌ MISSING | read-only batch |
| [ ] bind_statetree_delegate | ❌ MISSING | read-only batch |
| [ ] bind_statetree_task_completion | ❌ MISSING | read-only batch |
| [ ] set_statetree_parameter | ❌ MISSING | read-only batch |
| [ ] set_statetree_state_property | ❌ MISSING | read-only batch |
| [ ] set_statetree_node_property | ❌ MISSING | read-only batch |
| [ ] set_statetree_transition_property | ❌ MISSING | read-only batch |
| [ ] set_statetree_schema | ❌ MISSING | read-only batch |
| [ ] set_statetree_component_tree | ❌ MISSING | read-only batch |
| [ ] set_statetree_color | ❌ MISSING | read-only batch |
| [ ] remove_statetree_state | ❌ MISSING | read-only batch |
| [ ] remove_statetree_node | ❌ MISSING | read-only batch |
| [ ] remove_statetree_transition | ❌ MISSING | read-only batch |
| [ ] remove_statetree_binding | ❌ MISSING | read-only batch |
| [ ] remove_statetree_parameter | ❌ MISSING | read-only batch |
| [ ] compile_statetree | ❌ MISSING | read-only batch; no compile |

**Tally (42):** ✅ 10 · ⚠️ 4 · ❌ 28 · ⛔ 0

## EQS (Environment Queries)

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] get_eqs_node_type_info | ✅ DONE | `get_env_query_node_info` (eqs) |
| [x] get_env_query | ✅ DONE | `get_env_query_info` + `get_env_query_config` (eqs) |
| [ ] search_eqs_nodes | ⚠️ PARTIAL | `list_env_query_generators`/`_tests`/`_contexts` list classes; no unified ranked keyword search |
| [ ] create_env_query | ❌ MISSING | read-only batch; no write API |
| [ ] add_eqs_option | ❌ MISSING | read-only batch |
| [ ] remove_eqs_option | ❌ MISSING | read-only batch |
| [ ] add_eqs_test | ❌ MISSING | read-only batch |
| [ ] remove_eqs_test | ❌ MISSING | read-only batch |
| [ ] set_eqs_node_property | ❌ MISSING | read-only batch |
| [ ] validate_env_query | ❌ MISSING | no validator |
| [ ] run_env_query | ❌ MISSING | no in-editor execution |

**Tally (11):** ✅ 2 · ⚠️ 1 · ❌ 8 · ⛔ 0
(Extras implemented beyond spec rows: `list_env_queries`.)

## GAS (Gameplay Ability System)

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] list_gameplay_tags | ✅ DONE | `list_gameplay_tags` (gameplay_tags_read) |
| [x] get_gameplay_tag | ✅ DONE | `get_gameplay_tag_info` (gameplay_tags_read) |
| [x] add_gameplay_tag | ✅ DONE | `add_gameplay_tag` (gameplay_tags_write, ini source) |
| [x] remove_gameplay_tag | ✅ DONE | `remove_gameplay_tag` (gameplay_tags_write) |
| [x] list_gameplay_tag_sources | ✅ DONE | `list_gameplay_tag_sources` (gameplay_tags_read) |
| [x] list_attribute_sets | ✅ DONE | `list_attribute_sets` (gas) |
| [x] list_attributes | ✅ DONE | fold: `get_attribute_set_info` lists a set's attributes (gas) |
| [x] list_gameplay_effect_components | ✅ DONE | fold: `get_gameplay_effect_info` returns GEComponents by class (gas) |
| [ ] rename_gameplay_tag | ❌ MISSING | no redirector-based rename |
| [ ] add_gameplay_tag_source | ❌ MISSING | no tag-source creation |
| [ ] validate_gameplay_tags | ❌ MISSING | no validator |
| [ ] create_attribute_set | ❌ MISSING | no write API |
| [ ] add_attribute | ❌ MISSING | no write API |
| [ ] validate_attribute_set | ❌ MISSING | no validator |
| [ ] create_gameplay_effect | ❌ MISSING | no write API |
| [ ] add_gameplay_effect_component | ❌ MISSING | no write API |
| [ ] remove_gameplay_effect_component | ❌ MISSING | no write API |
| [ ] set_gameplay_effect_modifier | ❌ MISSING | no write API |
| [ ] validate_gameplay_effect | ❌ MISSING | no validator |
| [ ] create_gameplay_ability | ❌ MISSING | no write API |
| [ ] create_gameplay_cue_notify | ❌ MISSING | no write API |
| [ ] list_gameplay_cue_notifies | ❌ MISSING | no cue-notify listing |
| [ ] validate_gameplay_cues | ❌ MISSING | no validator |
| [ ] search_gas | ❌ MISSING | no unified ranked search across tags/attrs/effects/abilities |
| [ ] get_ability_system_info | ❌ MISSING | no runtime ASC inspection on an actor |

**Tally (25):** ✅ 8 · ⚠️ 0 · ❌ 17 · ⛔ 0
(Extras implemented beyond spec rows: `list_gameplay_abilities`, `list_gameplay_effects`, `get_gameplay_ability_info`, `get_gameplay_effect_info` asset-level reads.)

## PCG (Procedural Content Generation)

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] read_pcg_graph | ✅ DONE | `get_pcg_graph_info` (pcg) |
| [x] get_pcg_node | ✅ DONE | `get_pcg_node_info` (pcg) |
| [x] get_pcg_node_connections | ✅ DONE | fold: `get_pcg_node_info` returns per-pin edge connections |
| [x] get_pcg_graph_parameters | ✅ DONE | fold: `get_pcg_graph_info` returns exposed graph parameters |
| [x] get_pcg_graph_properties | ✅ DONE | fold: `get_pcg_graph_info` (include_settings) returns node/graph settings |
| [x] get_pcg_node_property | ✅ DONE | fold: `get_pcg_node_info` returns reflected settings UPROPERTY values |
| [ ] search_pcg_nodes | ⚠️ PARTIAL | `list_pcg_node_classes`/`list_pcg_settings_classes` (name filter); no ranked search |
| [ ] list_pcg_node_categories | ⚠️ PARTIAL | approximated by `list_pcg_node_classes`; no true category grouping |
| [ ] create_pcg_graph | ❌ MISSING | read-only batch; no write API |
| [ ] create_pcg_graph_instance | ❌ MISSING | read-only batch |
| [ ] create_pcg_graph_from_template | ❌ MISSING | read-only batch |
| [ ] validate_pcg_graph | ❌ MISSING | no validator |
| [ ] layout_pcg_graph | ❌ MISSING | no layout write |
| [ ] build_pcg_graph | ❌ MISSING | no atomic build |
| [ ] add_pcg_node | ❌ MISSING | read-only batch |
| [ ] delete_pcg_node | ❌ MISSING | read-only batch |
| [ ] set_pcg_node_property | ❌ MISSING | read-only batch |
| [ ] set_pcg_node_position | ❌ MISSING | read-only batch |
| [ ] set_pcg_node_comment | ❌ MISSING | read-only batch |
| [ ] connect_pcg_pins | ❌ MISSING | read-only batch |
| [ ] disconnect_pcg_pin | ❌ MISSING | read-only batch |
| [ ] disconnect_pcg_edge | ❌ MISSING | read-only batch |
| [ ] trace_pcg_connection | ❌ MISSING | read-only batch |
| [ ] list_pcg_advanced_nodes | ❌ MISSING | no advanced-node listing |
| [ ] list_pcg_templates | ❌ MISSING | no template listing |
| [ ] get_pcg_node_type_info | ❌ MISSING | no per-class type-info tool (only in-graph node reads) |
| [ ] list_pcg_kernel_types | ❌ MISSING | no kernel-type listing |
| [ ] get_pcg_property_valid_values | ❌ MISSING | no valid-values query |
| [ ] set_pcg_graph_property | ❌ MISSING | read-only batch |
| [ ] set_pcg_node_property_path | ❌ MISSING | read-only batch |
| [ ] list_pcg_node_property_paths | ❌ MISSING | no property-path listing |
| [ ] add_pcg_node_property_array_element | ❌ MISSING | read-only batch |
| [ ] remove_pcg_node_property_array_element | ❌ MISSING | read-only batch |
| [ ] set_pcg_node_property_class | ❌ MISSING | read-only batch |
| [ ] add_pcg_dynamic_input_pin | ❌ MISSING | read-only batch |
| [ ] remove_pcg_dynamic_input_pin | ❌ MISSING | read-only batch |
| [ ] add_pcg_graph_parameter | ❌ MISSING | read-only batch |
| [ ] remove_pcg_graph_parameter | ❌ MISSING | read-only batch |
| [ ] rename_pcg_graph_parameter | ❌ MISSING | read-only batch |
| [ ] set_pcg_graph_parameter_value | ❌ MISSING | read-only batch |
| [ ] set_pcg_graph_parameter_type | ❌ MISSING | read-only batch |
| [ ] set_pcg_subgraph_target | ❌ MISSING | read-only batch |
| [ ] get_pcg_subgraph_override | ❌ MISSING | read-only batch |
| [ ] set_pcg_subgraph_override | ❌ MISSING | read-only batch |
| [ ] reset_pcg_subgraph_override | ❌ MISSING | read-only batch |
| [ ] create_pcg_compute_source | ❌ MISSING | no HLSL compute support |
| [ ] get_pcg_compute_source | ❌ MISSING | no HLSL compute support |
| [ ] set_pcg_compute_source | ❌ MISSING | no HLSL compute support |
| [ ] add_pcg_compute_source_additional | ❌ MISSING | no HLSL compute support |
| [ ] remove_pcg_compute_source_additional | ❌ MISSING | no HLSL compute support |
| [ ] list_pcg_compute_sources | ❌ MISSING | no HLSL compute support |
| [ ] set_pcg_custom_hlsl_kernel_type | ❌ MISSING | no HLSL compute support |
| [ ] create_pcg_builder_settings | ❌ MISSING | read-only batch |
| [ ] set_pcg_static_mesh_spawner_meshes | ❌ MISSING | read-only batch |
| [ ] pcg_generate_component | ❌ MISSING | no component generation ops |
| [ ] pcg_cleanup_component | ❌ MISSING | no component generation ops |
| [ ] pcg_generate_local | ❌ MISSING | no component generation ops |
| [ ] pcg_cleanup_local (+_immediate) | ❌ MISSING | no component generation ops |
| [ ] pcg_cancel_component_generation | ❌ MISSING | no component generation ops |
| [ ] pcg_set_component_graph | ❌ MISSING | no component generation ops |
| [ ] pcg_get_generated_output | ❌ MISSING | no component generation ops |
| [ ] pcg_clear_pcg_link | ❌ MISSING | no component generation ops |
| [ ] pcg_is_partitioned | ❌ MISSING | no partitioning ops |
| [ ] pcg_list_partition_actors | ❌ MISSING | no partitioning ops |
| [ ] pcg_get_partition_actor_info | ❌ MISSING | no partitioning ops |
| [ ] pcg_aggregate_partition_output | ❌ MISSING | no partitioning ops |
| [ ] pcg_schedule_graph | ❌ MISSING | no async scheduling |
| [ ] pcg_schedule_component | ❌ MISSING | no async scheduling |
| [ ] pcg_schedule_cleanup | ❌ MISSING | no async scheduling |
| [ ] pcg_schedule_refresh | ❌ MISSING | no async scheduling |
| [ ] pcg_generate_all_components | ❌ MISSING | no bulk editor ops |
| [ ] pcg_cleanup_all_components | ❌ MISSING | no bulk editor ops |
| [ ] pcg_refresh_all_components_filtered | ❌ MISSING | no bulk editor ops |
| [ ] pcg_refresh_runtime_gen_sources | ❌ MISSING | no bulk editor ops |
| [ ] pcg_dirty_runtime_gen_sources (5.8+) | ❌ MISSING | no bulk editor ops |
| [ ] pcg_flush_cache | ❌ MISSING | no cache ops |
| [ ] pcg_build_landscape_cache | ❌ MISSING | no cache ops |
| [ ] pcg_clear_landscape_cache | ❌ MISSING | no cache ops |
| [ ] pcg_runtime_generation_refresh | ❌ MISSING | no cache ops |
| [ ] pcg_refresh_pcg_runtime_component | ❌ MISSING | no cache ops |
| [ ] pcg_is_inspecting | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_enable_node_inspection | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_disable_node_inspection | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_was_node_executed | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_has_node_produced_data | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_get_node_inactive_pin_mask | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_did_node_trigger_gpu_to_cpu_readback | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_did_node_trigger_cpu_to_gpu_upload | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_node_applied_data_overrides | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_inspect_node_output | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_inspect_partition_node_output | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_list_executed_nodes | ❌ MISSING | no inspection/debug ops |
| [ ] pcg_clear_inspection_data | ❌ MISSING | no inspection/debug ops |
| [ ] add_pcg_comment | ❌ MISSING | read-only batch |

**Tally (~95 spec rows):** ✅ 6 · ⚠️ 2 · ❌ 87 · ⛔ 0
(Extras implemented beyond spec rows: `list_pcg_graphs`, `list_pcg_components_in_level`, `list_pcg_settings_classes`, `list_pcg_node_classes`.)

## Mutable (Customizable Characters)

Known context CONFIRMED: `mutable.py` header states the project ships ZERO CustomizableObject and
ZERO CustomizableObjectInstance assets, and the module is a deliberate READ-ONLY batch that never
compiles/bakes/generates. Instance-/asset-dependent writes are classified ⛔ BLOCKED on that basis;
pure graph-authoring and macro tools that were simply never built are ❌ MISSING.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] list_mutable_parameters | ✅ DONE | `get_customizable_object_parameters` (filter/include_options) (mutable) |
| [ ] get_mutable_graph | ⚠️ PARTIAL | `get_customizable_object_info` gives asset overview; no full graph node/pin export |
| [ ] search_mutable_nodes | ⚠️ PARTIAL | `list_mutable_node_classes` (filter); no ranked keyword/category search |
| [ ] describe_mutable_node | ⚠️ PARTIAL | `list_mutable_node_classes` lists classes; no per-class property detail |
| [ ] list_mutable_node_categories | ⚠️ PARTIAL | approximated by `list_mutable_node_classes`; no category grouping |
| [ ] list_mutable_states | ⚠️ PARTIAL | states surface in `get_customizable_object_info`; no dedicated states tool |
| [ ] list_mutable_child_objects | ⚠️ PARTIAL | child links surface in `get_customizable_object_info`; no dedicated tool |
| [ ] create_customizable_object | ⛔ BLOCKED | read-only batch; 0 assets to author against |
| [ ] create_customizable_object_instance | ⛔ BLOCKED | 0 CustomizableObjects to instance |
| [ ] compile_customizable_object | ⛔ BLOCKED | module never compiles/bakes; 0 assets |
| [ ] validate_customizable_object | ⛔ BLOCKED | requires compile; 0 assets |
| [ ] get_mutable_parameter (instance) | ⛔ BLOCKED | 0 CustomizableObjectInstance assets |
| [ ] set_mutable_parameter | ⛔ BLOCKED | instance write; 0 instances to validate against |
| [ ] reset_mutable_parameters | ⛔ BLOCKED | instance write; 0 instances |
| [ ] set_mutable_state | ⛔ BLOCKED | instance write; 0 instances |
| [ ] update_mutable_instance | ⛔ BLOCKED | instance update; 0 instances |
| [ ] create_mutable_macro_library | ❌ MISSING | no macro-library write |
| [ ] add_mutable_node | ❌ MISSING | no graph-authoring write API |
| [ ] connect_mutable_nodes | ❌ MISSING | no graph-authoring write API |
| [ ] disconnect_mutable_pin | ❌ MISSING | no graph-authoring write API |
| [ ] delete_mutable_node | ❌ MISSING | no graph-authoring write API |
| [ ] build_mutable_graph | ❌ MISSING | no graph-authoring write API |
| [ ] layout_mutable_graph | ❌ MISSING | no graph-authoring write API |
| [ ] get_mutable_node (by GUID) | ❌ MISSING | no by-GUID node reader |
| [ ] set_mutable_node_property | ❌ MISSING | no node property write |
| [ ] add_mutable_node_pin | ❌ MISSING | no pin write |
| [ ] copy_mutable_parameters | ❌ MISSING | not built |
| [ ] paste_mutable_parameters | ❌ MISSING | not built |
| [ ] link_mutable_child_object | ❌ MISSING | hierarchy write not built |
| [ ] export_mutable_object | ❌ MISSING | no export tool |
| [ ] set_mutable_parameter_ui_metadata | ❌ MISSING | no UI-metadata write |
| [ ] list_mutable_macros | ❌ MISSING | no macro read |
| [ ] add_mutable_macro | ❌ MISSING | no macro write |
| [ ] remove_mutable_macro | ❌ MISSING | no macro write |
| [ ] set_mutable_macro_io | ❌ MISSING | no macro write |
| [ ] add_mutable_macro_instance | ❌ MISSING | no macro write |

**Tally (36):** ✅ 1 · ⚠️ 6 · ❌ 20 · ⛔ 9
(Extras implemented beyond spec rows: `list_customizable_objects`, `list_customizable_object_instances`, `get_customizable_object_info`.)

## Grand totals

| category | spec rows | ✅ DONE | ⚠️ PARTIAL | ❌ MISSING | ⛔ BLOCKED |
|---|---|---|---|---|---|
| Behavior Trees | 24 | 8 | 5 | 11 | 0 |
| StateTree | 42 | 10 | 4 | 28 | 0 |
| EQS | 11 | 2 | 1 | 8 | 0 |
| GAS | 25 | 8 | 0 | 17 | 0 |
| PCG | ~95 | 6 | 2 | 87 | 0 |
| Mutable | 36 | 1 | 6 | 20 | 9 |
| **Total** | **~233** | **35** | **18** | **171** | **9** |

Overall pattern: every category has working READ/inspect coverage; the large MISSING counts are almost
entirely WRITE/authoring/runtime features that were deliberately deferred (StateTree, EQS, GAS, PCG were
read-only batches). Mutable writes are BLOCKED specifically by the project having zero Mutable assets.
