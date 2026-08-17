# Spec: Data Tables (`/docs/reference/datatables`, 10 cmds)

Clean-room. UDataTable CRUD by row struct. Implement over `unreal.DataTableFunctionLibrary` +
editor. All auto-save. Call `get_data_table_schema` before add/update to validate field types.

- `create_data_table` — new table with row struct — asset_path(req); row_struct(req)
- `list_data_table_row_structs` — search usable row structs — filter(opt); include_user_defined(opt); max_results(opt)
- `get_data_table_rows` — all rows — data_table_path(req)
- `get_data_table_row` — one row by name — data_table_path(req); row_name(req)
- `get_data_table_schema` — column names/types — data_table_path(req)
- `add_data_table_row` — insert row — data_table_path(req); row_name(req); data(json,opt)
- `update_data_table_row` — modify fields — data_table_path(req); row_name(req); data(json,req)
- `delete_data_table_row` — remove — data_table_path(req); row_name(req)
- `rename_data_table_row` — data_table_path(req); old_row_name(req); new_row_name(req)
- `duplicate_data_table_row` — data_table_path(req); source_row_name(req); new_row_name(req)
