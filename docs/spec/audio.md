# Spec: Audio (category 25 — planned)

Interface reference for our clean-room reimplementation. Derived from the *published command/parameter
reference* (interface spec only — no source seen or copied). Ours to implement over Unreal's Audio /
MetaSound Python + editor-scripting API. Status: **planned** (not yet scheduled; here for the roadmap).

Naming to match: `verb_noun`. `req`=required, `opt`=optional. Graph builders follow our
document-based pattern (whole graph as one `nodes`+`connections` doc, auto-layout, validate-after).

| # | Command | Does | Params |
|---|---|---|---|
| 1 | `search_metasound_nodes` | Search MetaSound node classes by keyword (ranked) | query(str,opt); queries(strlist,opt); category(str,opt); data_type(str,opt); max_results(int,opt); include_deprecated(bool,opt); include_all_versions(bool,opt) |
| 2 | `describe_metasound_node` | Full pin schema + metadata for a node | node_class(str,opt); node_classes(strlist,opt); major_version(int,opt) |
| 3 | `list_metasound_datatypes` | List registered MetaSound data types | filter(str,opt); max_results(int,opt); array_only(bool,opt) |
| 4 | `list_metasound_interfaces` | List interfaces w/ version + member counts | filter(str,opt); max_results(int,opt) |
| 5 | `create_metasound` | Create MetaSound Source or Patch asset | name(str,req); path(str,req); type(str,opt); output_format(str,opt); one_shot(bool,opt); author(str,opt) |
| 6 | `get_metasound_info` | Summarize type, interfaces, structure | metasound_path(str,req) |
| 7 | `get_metasound_graph` | Read graph: nodes, connections, variables | metasound_path(str,req); include_defaults(bool,opt); include_positions(bool,opt); max_results(int,opt) |
| 8 | `add_metasound_node` | Add node by class name | metasound_path(str,req); node_class(str,req); major_version(int,opt); position(str,opt) |
| 9 | `remove_metasound_node` | Remove node | metasound_path(str,req); node_id(str,req) |
| 10 | `connect_metasound_nodes` | Wire two nodes by pin names | metasound_path(str,req); from_node(str,req); from_output(str,req); to_node(str,req); to_input(str,req) |
| 11 | `disconnect_metasound_nodes` | Clear a node input connection | metasound_path(str,req); to_node(str,req); to_input(str,req) |
| 12 | `set_metasound_node_input_default` | Set literal on unconnected input | metasound_path(str,req); node_id(str,req); input_name(str,req); value(str,req) |
| 13 | `add_metasound_graph_input` | Add interface input | metasound_path(str,req); name(str,req); data_type(str,req); default_value(str,opt); is_constructor(bool,opt) |
| 14 | `add_metasound_graph_output` | Add interface output | metasound_path(str,req); name(str,req); data_type(str,req); default_value(str,opt); is_constructor(bool,opt) |
| 15 | `remove_metasound_graph_member` | Remove input/output/variable | metasound_path(str,req); name(str,req); kind(str,req) |
| 16 | `set_metasound_graph_input_default` | Set graph input default | metasound_path(str,req); name(str,req); value(str,req) |
| 17 | `add_metasound_variable` | Declare a graph variable | metasound_path(str,req); name(str,req); data_type(str,req); default_value(str,opt) |
| 18 | `add_metasound_variable_node` | Add get/set accessor node | metasound_path(str,req); name(str,req); accessor(str,opt) |
| 19 | `set_metasound_interface` | Add/remove an interface | metasound_path(str,req); interface_name(str,req); add(bool,opt) |
| 20 | `build_metasound_graph` | Build whole graph in one call (our doc pattern) | metasound_path(str,req); nodes(json,opt); connections(json,opt); graph_inputs(json,opt); graph_outputs(json,opt); variables(json,opt); clear_existing(bool,opt); auto_layout(bool,opt) |
| 21 | `layout_metasound_graph` | Re-position L→R with routing | metasound_path(str,req); route_knots(bool,opt) |
| 22 | `validate_metasound` | Check for faults (silent outputs, missing defaults) | metasound_path(str,req) |
| 23 | `list_sound_cue_node_types` | List Sound Cue node classes | filter(str,opt); max_results(int,opt) |
| 24 | `create_sound_cue` | Create Sound Cue, seed with waves | name(str,req); path(str,req); sound_waves(strlist,opt) |
| 25 | `get_sound_cue_graph` | Read Sound Cue node tree | sound_cue_path(str,req) |
| 26 | `add_sound_cue_node` | Add node by class | sound_cue_path(str,req); node_class(str,req); sound_wave(str,opt); is_root(bool,opt); position(str,opt) |
| 27 | `remove_sound_cue_node` | Remove node | sound_cue_path(str,req); node_id(str,req) |
| 28 | `connect_sound_cue_nodes` | Attach child to parent slot | sound_cue_path(str,req); parent_node(str,req); child_node(str,req); child_index(int,opt) |
| 29 | `build_sound_cue_graph` | Build whole tree in one call | sound_cue_path(str,req); nodes(json,opt); connections(json,opt); clear_existing(bool,opt); auto_layout(bool,opt) |
| 30 | `get_sound_cue_node` | List editable props of one node | sound_cue_path(str,req); node_id(str,req) |
| 31 | `set_sound_cue_node_properties` | Set reflected props on node | sound_cue_path(str,req); node_id(str,req); properties(obj,req) |
| 32 | `layout_sound_cue_graph` | Re-position tree R→L from root | sound_cue_path(str,req) |
| 33 | `validate_sound_cue` | Check for faults (missing root, broken pins) | sound_cue_path(str,req) |
| 34 | `export_audio` | Export asset state to JSON file | asset_path(str,req) |
| 35 | `list_audio_asset_types` | List creatable audio asset classes | filter(str,opt); kind(str,opt); max_results(int,opt) |
| 36 | `create_audio_asset` | Create any audio asset by class + props | class_name(str,req); name(str,req); path(str,req); properties(json,opt) |
| 37 | `get_audio_asset_info` | Report identity + hierarchy | asset_path(str,req) |
| 38 | `set_submix_parent` | Re-parent a Sound Submix | asset_path(str,req); parent_path(str,opt) |
| 39 | `set_sound_class_parent` | Re-parent a Sound Class | asset_path(str,req); parent_path(str,opt) |
| 40 | `set_audio_effect_chain` | Set ordered effect chain | asset_path(str,req); effects(strlist,req) |
