# Spec: Bulk Asset Ops (`/docs/reference/asset_ops`, 5 cmds)

Clean-room. Bulk move/delete/consolidate with reference fixup. Implement over `EditorAssetLibrary`,
`AssetToolsHelpers`, redirector fixup.

- `move_assets` — relocate/rename many, update refs, manage redirectors — moves(json[{source,dest,new_name}],req); fixup(bool,opt)
- `delete_assets` — delete many w/ ref check — asset_paths(json,req); force(bool,opt)
- `fixup_redirectors` — resolve orphaned redirectors — path(str,opt); recursive(bool,opt)
- `find_replacement_candidates` — find compatible assets to substitute — asset_path(str,req); name_filter(str,opt); path(str,opt); recursive(bool,opt); max_results(int,opt)
- `replace_references` — consolidate refs onto one target — source_paths(json,req); target_path(str,req; same class); delete_after(bool,opt)
