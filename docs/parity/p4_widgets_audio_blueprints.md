# Parity Audit P4 — Widgets · Audio · Blueprints

Read-only feature-parity audit of `docs/spec/{widgets,audio,blueprints}.md` against the actual
implementation (`MCP/UserTools/*.py`, `Source/UnrealMCP/`). Match by capability, not literal name.
Legend: `[x]` = ✅ DONE, `[ ]` = ⚠️ PARTIAL / ❌ MISSING / ⛔ BLOCKED / ❓ UNCERTAIN.

Implementation modules found: `widgets_read.py`, `widgets_write.py` (widgets); `audio_read.py`,
`sound_write.py` (audio); `blueprints_read.py`, `blueprints_write.py` + C++
`MCPCommandHandlers_Blueprints.cpp` (blueprints). No MetaSound module exists. No Blueprint
graph-node module exists.

## Widgets

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] get_widget_tree | ✅ DONE | `list_widget_hierarchy` (nested tree, slots, text) |
| [ ] add_widget (panel child, layout/props) | ⚠️ PARTIAL | `add_widget` adds by class under parent, but NO layout/slot props at add time |
| [x] remove_widget (+descendants) | ✅ DONE | `remove_widget` |
| [ ] move_widget (reparent) | ❌ MISSING | no reparent-in-tree tool |
| [ ] rename_widget | ❌ MISSING | — |
| [ ] duplicate_widget (+children) | ❌ MISSING | — |
| [ ] replace_widget (swap class) | ❌ MISSING | — |
| [ ] wrap_widget (in panel) | ❌ MISSING | — |
| [ ] set_root_widget | ❌ MISSING | `add_widget` sets root only on an empty tree; no dedicated setter |
| [x] create_widget_blueprint | ✅ DONE | `create_widget_blueprint` |
| [ ] list_widget_types (filter) | ❌ MISSING | no widget-class enumerator (generic `search_classes` not widget-scoped) |
| [ ] get_widget_class_info | ❌ MISSING | — |
| [ ] set_widget_blueprint_parent | ❓ UNCERTAIN | `reparent_blueprint` exists for BP assets; unverified whether it accepts WidgetBlueprints |
| [ ] get_widget_properties | ⚠️ PARTIAL | readable via `get_widget_blueprint_info` (whole-BP); no per-widget getter |
| [ ] set_widget_properties (json) | ❌ MISSING | — |
| [ ] get_slot_properties | ⚠️ PARTIAL | slot info surfaced by `list_widget_hierarchy` (include_slots); no dedicated getter |
| [ ] set_slot_properties | ❌ MISSING | — |
| [ ] set_widget_is_variable | ❌ MISSING | — |
| [ ] set_widget_navigation | ❌ MISSING | — |
| [ ] get_widget_navigation | ❌ MISSING | — |
| [ ] clear_widget_navigation | ❌ MISSING | — |
| [ ] list_named_slots | ❌ MISSING | — |
| [ ] get_named_slot_content | ❌ MISSING | — |
| [ ] set_named_slot_content | ❌ MISSING | — |
| [ ] clear_named_slot | ❌ MISSING | — |
| [ ] create_widget_animation | ❌ MISSING | — |
| [ ] list_widget_animations | ❌ MISSING | — |
| [ ] remove_widget_animation | ❌ MISSING | — |
| [ ] add_animation_widget_binding | ❌ MISSING | — |
| [ ] remove_animation_widget_binding | ❌ MISSING | — |
| [ ] add_animation_track | ❌ MISSING | — |
| [ ] add_animation_key | ❌ MISSING | — |
| [ ] list_animation_tracks | ❌ MISSING | — |
| [ ] add_property_binding | ❌ MISSING | — |
| [ ] remove_property_binding | ❌ MISSING | — |
| [ ] list_property_bindings | ❌ MISSING | — |
| [ ] list_widget_events | ❌ MISSING | — |
| [ ] add_viewmodel | ❌ MISSING | no MVVM support at all |
| [ ] remove_viewmodel | ❌ MISSING | — |
| [ ] rename_viewmodel | ❌ MISSING | — |
| [ ] list_viewmodels | ❌ MISSING | — |
| [ ] add_mvvm_binding | ❌ MISSING | — |
| [ ] set_mvvm_binding | ❌ MISSING | — |
| [ ] list_mvvm_conversion_functions | ❌ MISSING | — |
| [ ] set_viewmodel_settings | ❌ MISSING | — |
| [ ] set_variable_field_notify | ❌ MISSING | — |
| [ ] remove_mvvm_binding | ❌ MISSING | — |
| [ ] list_mvvm_bindings | ❌ MISSING | — |
| [ ] add_ui_component | ❌ MISSING | — |
| [ ] remove_ui_component | ❌ MISSING | — |
| [ ] list_ui_components | ❌ MISSING | — |
| [ ] set_widget_editor_mode | ❌ MISSING | — |

**Widgets tally:** 52 features — ✅ 3 DONE · ⚠️ 3 PARTIAL · ❓ 1 UNCERTAIN · ❌ 45 MISSING.

## Audio

Spec is overwhelmingly MetaSound graph authoring (#1–22). **No MetaSound authoring tools exist** —
`list_audio_assets` can only enumerate `MetaSoundSource` assets via AssetRegistry; there is no
create/read-graph/add-node/connect/build/validate for MetaSound. Sound Cue support is limited to
creating a cue and attaching a single WavePlayer node.

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] search_metasound_nodes | ❌ MISSING | no MetaSound module |
| [ ] describe_metasound_node | ❌ MISSING | — |
| [ ] list_metasound_datatypes | ❌ MISSING | — |
| [ ] list_metasound_interfaces | ❌ MISSING | — |
| [ ] create_metasound | ❌ MISSING | — |
| [ ] get_metasound_info | ❌ MISSING | — |
| [ ] get_metasound_graph | ❌ MISSING | — |
| [ ] add_metasound_node | ❌ MISSING | — |
| [ ] remove_metasound_node | ❌ MISSING | — |
| [ ] connect_metasound_nodes | ❌ MISSING | — |
| [ ] disconnect_metasound_nodes | ❌ MISSING | — |
| [ ] set_metasound_node_input_default | ❌ MISSING | — |
| [ ] add_metasound_graph_input | ❌ MISSING | — |
| [ ] add_metasound_graph_output | ❌ MISSING | — |
| [ ] remove_metasound_graph_member | ❌ MISSING | — |
| [ ] set_metasound_graph_input_default | ❌ MISSING | — |
| [ ] add_metasound_variable | ❌ MISSING | — |
| [ ] add_metasound_variable_node | ❌ MISSING | — |
| [ ] set_metasound_interface | ❌ MISSING | — |
| [ ] build_metasound_graph | ❌ MISSING | — |
| [ ] layout_metasound_graph | ❌ MISSING | — |
| [ ] validate_metasound | ❌ MISSING | — |
| [ ] list_sound_cue_node_types | ❌ MISSING | no Sound Cue node-class enumerator |
| [ ] create_sound_cue (seed w/ waves) | ⚠️ PARTIAL | `create_sound_cue` creates empty cue; no `sound_waves` seed param (waves added via `add_wave_player_to_cue`) |
| [ ] get_sound_cue_graph | ⚠️ PARTIAL | `get_sound_cue_info` summarizes cue + first_node; not a full node-tree traversal |
| [ ] add_sound_cue_node (by class) | ⚠️ PARTIAL | only `add_wave_player_to_cue` (WavePlayer); no generic add-node-by-class |
| [ ] remove_sound_cue_node | ❌ MISSING | — |
| [ ] connect_sound_cue_nodes | ❌ MISSING | — |
| [ ] build_sound_cue_graph | ❌ MISSING | — |
| [ ] get_sound_cue_node | ❌ MISSING | — |
| [ ] set_sound_cue_node_properties | ❌ MISSING | — |
| [ ] layout_sound_cue_graph | ❌ MISSING | — |
| [ ] validate_sound_cue | ❌ MISSING | — |
| [ ] export_audio | ❌ MISSING | — |
| [ ] list_audio_asset_types | ❌ MISSING | `list_audio_assets` lists asset instances, not creatable classes |
| [ ] create_audio_asset (by class) | ⚠️ PARTIAL | specific creators exist (`create_sound_cue`/`_class`/`_mix`/`_attenuation`); no generic by-class; no Submix/MetaSound creation |
| [ ] get_audio_asset_info | ⚠️ PARTIAL | kind-specific getters (`get_sound_wave_info`/`get_sound_cue_info`/`get_attenuation_info`); no generic getter; no Submix/SoundClass/SoundMix getter |
| [ ] set_submix_parent | ❌ MISSING | no submix tools |
| [ ] set_sound_class_parent | ❌ MISSING | `create_sound_class` exists but no re-parent op |
| [ ] set_audio_effect_chain | ❌ MISSING | — |

**Audio tally:** 40 features — ✅ 0 DONE · ⚠️ 5 PARTIAL · ❌ 35 MISSING (incl. all 22 MetaSound features).

## Blueprints

Core asset ops + variable create/delete + dispatcher + event-override are implemented. **The entire
document-pattern graph layer and all node-level ops are absent** (no `build_graph`, `add_blueprint_node`,
`connect_blueprint_nodes`, `set_pin_default`, `search_nodes`, etc. — confirmed by tree-wide grep).
Component tools operate on spawned actor instances, not on the Blueprint asset's component tree.

| spec feature | status | implementing tool / note |
|---|---|---|
| [x] create_blueprint | ✅ DONE | `create_blueprint` (+ C++ handler) |
| [ ] export_blueprint (sections/inheritance/values) | ⚠️ PARTIAL | `get_blueprint_info` introspects; not the sectioned doc-pattern export |
| [x] compile_blueprint | ✅ DONE | `compile_blueprint` |
| [x] reparent_blueprint | ✅ DONE | `reparent_blueprint` |
| [ ] open_blueprint_graph | ❌ MISSING | no graph layer |
| [ ] export_graph | ❌ MISSING | — |
| [ ] build_graph | ❌ MISSING | — |
| [ ] apply_graph_patch | ❌ MISSING | — |
| [ ] diff_graph | ❌ MISSING | — |
| [ ] arrange_blueprint_graph | ❌ MISSING | — |
| [ ] build_blueprint_graph (atomic) | ❌ MISSING | — |
| [x] create_blueprint_variable | ✅ DONE | `add_blueprint_variable` |
| [ ] get_blueprint_variable_details | ⚠️ PARTIAL | `list_blueprint_variables` lists; no per-variable details getter |
| [ ] set_blueprint_variable_properties | ❌ MISSING | create-only; no property setter |
| [x] delete_blueprint_variable | ✅ DONE | `remove_blueprint_variable` |
| [ ] get_blueprint_class_defaults | ❌ MISSING | — |
| [ ] set_blueprint_class_defaults | ❌ MISSING | — |
| [ ] create_blueprint_function | ⚠️ PARTIAL | `add_blueprint_function` creates an EMPTY graph only; no return_type / inputs / outputs |
| [ ] create_local_variable | ❌ MISSING | — |
| [ ] create_event_graph | ❌ MISSING | — |
| [ ] create_custom_event | ❌ MISSING | — |
| [ ] add_function_input | ❌ MISSING | — |
| [ ] add_function_output | ❌ MISSING | — |
| [ ] set_function_properties (pure/const/category) | ❌ MISSING | — |
| [ ] delete_blueprint_function | ❌ MISSING | — |
| [ ] rename_blueprint_function | ❌ MISSING | — |
| [x] override_blueprint_function | ✅ DONE | `add_event_override` (overrides inherited events/functions; needs C++ #9 handler) |
| [ ] rename_event_graph | ❌ MISSING | — |
| [ ] delete_event_graph | ❌ MISSING | — |
| [ ] get_blueprint_function_details (include_graph) | ⚠️ PARTIAL | `list_blueprint_functions` lists; no per-function details/graph |
| [ ] add_component_to_blueprint | ⚠️ PARTIAL | `add_component_to_actor` adds to spawned actor instances; not to the BP asset component tree |
| [ ] set_blueprint_component_property | ⚠️ PARTIAL | `set_component_property` operates on actor instances, not the BP asset |
| [ ] delete_component | ⚠️ PARTIAL | `remove_component_from_actor` (instance-level) |
| [ ] reparent_component | ❌ MISSING | — |
| [ ] set_root_component | ❌ MISSING | — |
| [ ] create_blueprint_interface | ❌ MISSING | — |
| [ ] implement_blueprint_interface | ❌ MISSING | — |
| [ ] remove_blueprint_interface | ❌ MISSING | — |
| [ ] list_blueprint_interfaces | ❌ MISSING | — |
| [x] create_event_dispatcher | ✅ DONE | `add_event_dispatcher` |
| [ ] create_blueprint_function_library | ❌ MISSING | — |
| [ ] search_nodes | ❌ MISSING | no node layer |
| [ ] describe_node | ❌ MISSING | — |
| [ ] add_blueprint_node | ❌ MISSING | — |
| [ ] connect_blueprint_nodes | ❌ MISSING | — |
| [ ] set_pin_default | ❌ MISSING | (a `set_pin_default` exists only for Control Rig, not Blueprints) |
| [ ] break_node_link | ❌ MISSING | — |
| [ ] insert_node_in_exec | ❌ MISSING | — |
| [ ] delete_blueprint_node | ❌ MISSING | — |
| [ ] set_blueprint_node_property | ❌ MISSING | — |
| [x] search_parent_classes | ✅ DONE | `search_classes` (class search w/ filter) |
| [ ] search_types (kind) | ⚠️ PARTIAL | `search_classes` covers classes; struct/enum/etc. only via separate finders, no unified kind-typed search |
| [x] export_object | ✅ DONE | `describe_object` / `get_object_property` |
| [x] export_asset | ✅ DONE | `get_asset_info` / `get_asset_properties` |
| [x] export_actor | ✅ DONE | `get_actor_properties` |

**Blueprints tally:** 55 features — ✅ 11 DONE · ⚠️ 8 PARTIAL · ❌ 36 MISSING.

---

### Grand total (P4)
147 spec features — ✅ 14 DONE · ⚠️ 16 PARTIAL · ❓ 1 UNCERTAIN · ❌ 116 MISSING.
