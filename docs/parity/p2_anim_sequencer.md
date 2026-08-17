# Parity Audit P2 — Animation & Sequencer

Read-only feature-parity audit of `docs/spec/anim.md` and `docs/spec/sequencer.md`
against the implemented tool inventory (Python `MCP/UserTools/*` + C++ `Source/UnrealMCP/`).
Status legend: ✅ DONE · ⚠️ PARTIAL · ❌ MISSING · ⛔ BLOCKED · ❓ UNCERTAIN.

Implementing modules inspected: `anim_write.py`, `animation_read.py`, `skeleton_write.py`,
`sequencer_read.py`, `level_sequence_write.py`, `sequencer_write_ext.py`. No anim/sequencer
handlers found in C++ source.

## Animation (docs/spec/anim.md)

| spec feature | status | implementing tool / note |
|---|---|---|
| [ ] `create_anim_blueprint` | ❌ MISSING | No equivalent. AnimBlueprintFactory buildable but unimplemented. |
| [ ] `get_anim_blueprint_info` | ❌ MISSING | No AnimBP reader. |
| [ ] `list_anim_graphs` | ❌ MISSING | No equivalent. |
| [ ] `validate_anim_blueprint` | ❌ MISSING | No AnimBP compile/validate command. |
| [ ] `add_anim_state_machine` | ⛔ BLOCKED | AnimGraph/state-machine node authoring not exposed to stock UE Python. |
| [ ] `add_anim_state` | ⛔ BLOCKED | Same — AnimBP graph node authoring not exposed. |
| [ ] `add_anim_transition` | ⛔ BLOCKED | Same. |
| [ ] `set_anim_entry_state` | ⛔ BLOCKED | Same. |
| [ ] `get_anim_state_machine` | ❓ UNCERTAIN | No reader; state-machine introspection via reflection unverified. |
| [ ] `remove_anim_state` | ⛔ BLOCKED | AnimBP graph node authoring not exposed. |
| [ ] `remove_anim_transition` | ⛔ BLOCKED | Same. |
| [ ] `set_anim_transition_property` | ⛔ BLOCKED | Same. |
| [ ] `build_anim_state_machine` | ⛔ BLOCKED | Depends on unexposed state-machine node authoring. |
| [ ] `set_anim_node_pin_exposure` | ⛔ BLOCKED | AnimGraph node pin authoring not exposed to Python. |
| [ ] `bind_anim_node_function` | ⛔ BLOCKED | Same. |
| [ ] `add_anim_layer` | ⛔ BLOCKED | AnimBP layer graph authoring not exposed. |
| [ ] `create_anim_layer_interface` | ❌ MISSING | AnimLayerInterface asset creation unimplemented. |
| [ ] `create_blend_space` | ❌ MISSING | No BlendSpace authoring (factory buildable, unimplemented). |
| [ ] `set_blend_space_axis` | ❌ MISSING | No equivalent. |
| [ ] `add_blend_space_sample` | ❌ MISSING | No equivalent. |
| [ ] `remove_blend_space_sample` | ❌ MISSING | No equivalent. |
| [ ] `get_blend_space` | ❌ MISSING | No BlendSpace reader. |
| [ ] `validate_anim_asset` | ❌ MISSING | No dedicated validator; read tools only implicitly type-check. |
| [ ] `create_anim_montage` | ❌ MISSING | No montage creation (factory buildable, unimplemented). |
| [ ] `add_montage_slot` | ❌ MISSING | No slot authoring on montage. |
| [ ] `add_montage_segment` | ❌ MISSING | No segment authoring on montage. |
| [x] `add_montage_section` | ✅ DONE | `add_montage_section` (+ `set_montage_section_time`, `set_montage_section_next_section`, `remove_montage_section`). |
| [ ] `get_anim_montage` | ❌ MISSING | `get_anim_sequence_info` errors on AnimMontage; no montage reader. |
| [ ] `list_anim_notify_classes` | ❌ MISSING | No notify-class enumeration. |
| [ ] `create_anim_notify_class` | ❌ MISSING | No notify-class creation. |
| [x] `add_anim_notify_track` | ✅ DONE | `add_anim_notify_track`. |
| [x] `add_anim_notify` | ✅ DONE | `add_anim_notify`. |
| [x] `remove_anim_notify_track` | ✅ DONE | `remove_anim_notify_track`. |
| [ ] `remove_anim_notify` | ❌ MISSING | Can add but not remove an individual notify. |
| [~] `get_anim_notifies` | ⚠️ PARTIAL | `get_anim_sequence_info` returns notify event/track names only — no per-notify time/duration, no track filter. |
| [x] `add_anim_curve` | ✅ DONE | `add_anim_curve` (+ bonus `remove_anim_curve`). |
| [x] `set_anim_curve_key` | ✅ DONE | `set_anim_curve_key`. |
| [~] `get_anim_curves` | ⚠️ PARTIAL | `get_anim_sequence_info` returns float-curve names only — no key/value data. |
| [x] `add_anim_sync_marker` | ✅ DONE | `add_anim_sync_marker`. |
| [ ] `get_anim_slots` | ⛔ BLOCKED | USkeleton slot-group API absent in Python; needs C++ handler (per `skeleton_write.py` note). |
| [ ] `add_anim_slot` | ⛔ BLOCKED | Same. |
| [ ] `remove_anim_slot` | ⛔ BLOCKED | Same. |
| [ ] `rename_anim_slot` | ⛔ BLOCKED | Same. |
| [ ] `add_anim_slot_group` | ⛔ BLOCKED | Same. |
| [ ] `remove_anim_slot_group` | ⛔ BLOCKED | Same. |

**Anim tally (45):** DONE 7 · PARTIAL 2 · MISSING 20 · BLOCKED 15 · UNCERTAIN 1

Note: `add_skeletal_mesh_socket`, `remove/set/rename_skeletal_mesh_socket`, `add_skeleton_socket`,
`add_virtual_bone`, `set_anim_rate_scale` are implemented but belong to the skeleton spec, not anim.md.

## Sequencer (docs/spec/sequencer.md)

| spec feature | status | implementing tool / note |
|---|---|---|
| [~] `create_level_sequence` | ⚠️ PARTIAL | `create_level_sequence(name,package_path)` — no `fps`/`force`/`display_rate` args. |
| [ ] `open_level_sequence` | ❌ MISSING | No editor-open command. |
| [x] `get_sequence_info` | ✅ DONE | `get_level_sequence_info`. |
| [x] `set_playback_range` | ✅ DONE | `set_playback_range` (start/end frames; no `duration_frames`/`use_seconds`). |
| [ ] `set_display_rate` | ❌ MISSING | No equivalent. |
| [ ] `set_tick_resolution` | ❌ MISSING | No equivalent. |
| [ ] `set_evaluation_type` | ❌ MISSING | No equivalent. |
| [ ] `set_playhead` | ❌ MISSING | No editor playhead control. |
| [ ] `get_playhead` | ❌ MISSING | No equivalent. |
| [ ] `set_playback_state` | ❌ MISSING | No play/pause control. |
| [x] `add_possessable` | ✅ DONE | `add_actor_binding`. |
| [x] `add_spawnable_from_class` | ✅ DONE | `add_spawnable_from_class`. |
| [x] `add_spawnable_from_actor` | ✅ DONE | `add_spawnable_from_instance`. |
| [x] `list_bindings` | ✅ DONE | Folded into `get_level_sequence_info` / `list_sequence_tracks` (binding enumeration; no filter args). |
| [ ] `rename_binding` | ❌ MISSING | No equivalent. |
| [ ] `remove_binding` | ❌ MISSING | No equivalent. |
| [ ] `convert_to_spawnable` | ❌ MISSING | No equivalent. |
| [ ] `convert_to_possessable` | ❌ MISSING | No equivalent. |
| [ ] `tag_binding` | ❌ MISSING | No equivalent. |
| [ ] `untag_binding` | ❌ MISSING | No equivalent. |
| [x] `get_binding` | ✅ DONE | Folded into `get_level_sequence_info` (per-binding tracks/sections/channels/keys). |
| [ ] `list_track_types` | ❌ MISSING | No track-type enumeration. |
| [x] `list_tracks` | ✅ DONE | `list_sequence_tracks`. |
| [~] `add_track` | ⚠️ PARTIAL | Only typed adders: `add_transform_track`, `add_skeletal_animation_track`, `add_visibility_track` — no generic add-by-type. |
| [x] `add_root_track` | ✅ DONE | `add_camera_cut_track`, `add_audio_track`, `add_event_track`. |
| [ ] `remove_track` | ❌ MISSING | No equivalent. |
| [~] `add_section` | ⚠️ PARTIAL | Sections auto-created with tracks + typed section adders (`add_audio_section`, `add_camera_cut_section`, `add_skeletal_animation_section`); no generic add-section w/ arbitrary range. |
| [x] `list_sections` | ✅ DONE | Folded into `list_sequence_tracks`/`get_level_sequence_info` (include_sections). |
| [ ] `set_section_range` | ❌ MISSING | No equivalent. |
| [ ] `remove_section` | ❌ MISSING | No equivalent. |
| [ ] `set_track_property` | ❌ MISSING | No universal track-property setter. |
| [ ] `set_section_property` | ❌ MISSING | No universal section-property setter. |
| [ ] `set_section_easing` | ❌ MISSING | No equivalent. |
| [ ] `set_section_blend_type` | ❌ MISSING | No equivalent. |
| [x] `list_channels` | ✅ DONE | Folded into `get_level_sequence_info`/`list_sequence_tracks` (per-section channels). |
| [~] `add_key` | ⚠️ PARTIAL | `add_keyframe` writes transform (loc/rot/scale) channels only; no generic key on any channel. |
| [x] `list_keys` | ✅ DONE | Folded into read tools (include_keys → frame/value/interp/tangents per channel). |
| [ ] `remove_key` | ❌ MISSING | No equivalent. |
| [ ] `set_channel_default` | ❌ MISSING | No equivalent. |
| [~] `set_key_value` | ⚠️ PARTIAL | `add_keyframe` overwrites an existing transform key at a frame; no generic set-by-channel/index. |
| [ ] `set_key_time` | ❌ MISSING | No equivalent. |
| [ ] `set_key_interpolation` | ❌ MISSING | No equivalent. |
| [ ] `set_key_tangent` | ❌ MISSING | No equivalent. |
| [ ] `set_channel_extrapolation` | ❌ MISSING | No pre/post-infinity control. |
| [~] `add_keys` (batch) | ⚠️ PARTIAL | `add_keyframe` writes multiple channels at ONE frame; no multi-frame batch. |
| [ ] `evaluate_channels` | ❌ MISSING | No channel evaluation at frame/time. |
| [x] `add_transform_keys` | ✅ DONE | `add_keyframe` (location/rotation/scale). |
| [ ] `list_animatable_properties` | ❌ MISSING | No equivalent. |
| [ ] `add_property_track` | ❌ MISSING | No auto-detect property track. |
| [x] `add_skeletal_animation` | ✅ DONE | `add_skeletal_animation_track` + `add_skeletal_animation_section`. |
| [~] `create_camera` | ⚠️ PARTIAL | `spawn_camera_actor` + `add_actor_binding` possible manually; no one-call cine-cam create+bind. |
| [x] `add_camera_cut` | ✅ DONE | `add_camera_cut_track` + `add_camera_cut_section`. |
| [ ] `render_sequence` | ⛔ BLOCKED | Movie Render Queue async/headless render unimplemented. |
| [ ] `render_status` | ⛔ BLOCKED | Depends on unimplemented render pipeline. |
| [x] `add_audio` | ✅ DONE | `add_audio_track` + `add_audio_section`. |
| [ ] `add_subsequence` | ❌ MISSING | No shot/subsequence track. |
| [~] `add_event_section` | ⚠️ PARTIAL | `add_event_track` exists; no `add_event_section` (repeater/trigger payload). |
| [ ] `add_timewarp` | ❌ MISSING | Not implemented (UE5.8+ feature). |
| [ ] `possess_component` | ❌ MISSING | No component-binding. |
| [ ] `add_marked_frame` | ❌ MISSING | No marked-frame authoring. |
| [x] `list_marked_frames` | ✅ DONE | Folded into `get_level_sequence_info` (returns marked frames). |
| [ ] `remove_marked_frame` | ❌ MISSING | No equivalent. |
| [ ] `add_folder` | ❌ MISSING | No sequencer folder authoring. |
| [ ] `add_to_folder` | ❌ MISSING | No equivalent. |

**Sequencer tally (64):** DONE 17 · PARTIAL 8 · MISSING 37 · BLOCKED 2 · UNCERTAIN 0
