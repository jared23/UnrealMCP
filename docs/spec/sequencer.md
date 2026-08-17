# Spec: Sequencer / Cinematics (`/docs/reference/sequencer`, ~65 cmds)

Clean-room interface. Implement over `unreal.LevelSequence`, `MovieSceneSequenceExtensions`,
`SequencerTools`, Movie Render Queue. `req`/`opt` implied by usage.

**Sequence:** `create_level_sequence`(asset_path,force,fps,display_rate_numerator/denominator) · `open_level_sequence`(asset_path) · `get_sequence_info`(asset_path)

**Playback/display:** `set_playback_range`(start_frame,end_frame,duration_frames,use_seconds) · `set_display_rate`(fps,numerator,denominator) · `set_tick_resolution`(…) · `set_evaluation_type`(framelocked/withsubframes) · `set_playhead`(frame/time) · `get_playhead` · `set_playback_state`(play/pause)

**Bindings:** `add_possessable`(actor) · `add_spawnable_from_class` · `add_spawnable_from_actor` · `list_bindings`(filter type/name) · `rename_binding` · `remove_binding`(guid) · `convert_to_spawnable` · `convert_to_possessable` · `tag_binding` · `untag_binding` · `get_binding`(tracks/sections/channels)

**Tracks/sections:** `list_track_types` · `list_tracks` · `add_track`(binding) · `add_root_track`(camera cuts/audio) · `remove_track`(index) · `add_section`(frame/time range) · `list_sections` · `set_section_range` · `remove_section`(index) · `set_track_property`(universal) · `set_section_property`(universal) · `set_section_easing`(ease-in/out) · `set_section_blend_type`(absolute/additive/relative)

**Channels/keys:** `list_channels` · `add_key` · `list_keys` · `remove_key`(index) · `set_channel_default` · `set_key_value` · `set_key_time` · `set_key_interpolation` · `set_key_tangent` · `set_channel_extrapolation`(pre/post-infinity) · `add_keys`(batch) · `evaluate_channels`(at frame/time)

**Transform/anim:** `add_transform_keys`(loc/rot/scale) · `list_animatable_properties` · `add_property_track`(auto-detect) · `add_skeletal_animation`(loop)

**Camera/render:** `create_camera`(cine cam bound) · `add_camera_cut`(auto cut track) · `render_sequence`(Movie Render Queue, async) · `render_status`

**Specialized:** `add_audio`(master track) · `add_subsequence`(shot track) · `add_event_section`(repeater/trigger) · `add_timewarp`(rate, UE5.8+) · `possess_component` · `add_marked_frame`(label) · `list_marked_frames` · `remove_marked_frame` · `add_folder`(color) · `add_to_folder`
