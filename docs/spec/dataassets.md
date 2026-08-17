# Spec: Data Assets (`/docs/reference/dataassets`, 5 cmds)

Clean-room. UDataAsset creation/discovery; edit values via the unified `objects` reflection cmds
(`get/set_object_property`). Implement over asset tools + reflection.

- `list_data_asset_classes` — enumerate UDataAsset subclasses — filter(str,opt); include_abstract(bool,opt)
- `create_data_asset` — instantiate subclass at path — class_name(str,req); asset_path(str,req); initial_properties(json,opt)
- `get_property_valid_types` — valid values for instanced/dropdown slots — class_name(str,req); property_path(str,req); filter(str,opt); include_abstract(bool,opt) *(always pass filter — else 200+ entries)*
- `search_class_paths` — query UClass paths — filter(str,opt); parent_class(str,opt); max_results(int,opt); include_properties(bool,opt)
- `list_data_assets` — list instances by path/class — path(str,opt); class_filter(str,opt); recursive(bool,opt); max_results(int,opt)
