# Spec: Asset Management (`/docs/reference/assets`, 16 cmds — **M1**)

Interface reference (clean-room). Implement over `unreal.EditorAssetLibrary`, `AssetRegistryHelpers`,
`AssetToolsHelpers`. `req`/`opt`.

- `find_assets` — search Asset Registry — class_type(str,opt); path(str,opt); name_pattern(str,opt); recursive(bool,opt); max_results(int,opt)
- `list_assets` — list a Content Browser dir — path(str,opt); class_filter(str,opt); recursive(bool,opt)
- `get_asset_info` — metadata (class, package, properties) — asset_path(str,req)
- `get_asset_properties` — all editable properties — asset_path(str,req)
- `set_asset_property` — set one property — asset_path(str,req); property_name(str,req); property_value(str,req)
- `find_references` — dependents/dependencies — asset_path(str,req); direction(str,opt: dependents/dependencies/both)
- `open_asset` — open in editor — asset_path(str,req)
- `save_asset` — save one — asset_path(str,req)
- `save_all` — save all dirty assets — (none)
- `delete_asset` — delete asset/dir (checks refs) — asset_path(str,req); force(bool,opt)
- `duplicate_asset` — copy to new location — source_path(str,req); dest_path(str,req); dest_name(str,req)
- `rename_asset` — rename/move (auto-fix refs) — source_path(str,req); dest_path(str,req)
- `import_asset` — import external file — source_file(str,req); destination_path(str,req); destination_name(str,opt); replace_existing(bool,opt)
- `import_assets_batch` — batch import — destination_path(str,req); files(json,opt); source_directory(str,opt); extensions(json,opt); replace_existing(bool,opt)
- `get_selected_assets` — Content Browser selection — (none)
- `sync_browser` — navigate CB to an asset — asset_path(str,req)

Note: their `duplicate_asset` takes dest_path + dest_name separately (our draft merged them) — match this shape.
