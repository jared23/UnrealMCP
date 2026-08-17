# Spec: Curves (`/docs/reference/curves`, 13 cmds)

Clean-room. CurveFloat/Vector/LinearColor, CurveTables, CurveAtlas. Implement over curve asset APIs.

- `create_curve` — float/vector/linear-color curve asset — asset_path; curve_type; save
- `get_curve` — keys/settings/ranges — asset_path; sub_curve; eval_times; as_json
- `set_curve_keys` — write/update keys — asset_path; keys(json); mode; infinity settings
- `delete_curve_keys` — remove keys/clear sub-curve — asset_path; times(json); all
- `import_curve` — load from JSON/CSV — asset_path; format; data
- `create_curve_table` — curve/composite table — asset_path; composite; parent_tables
- `get_curve_table` — read rows — asset_path; filter; include_keys; eval_times
- `set_curve_table_row` — add/update row — asset_path; row_name; keys; curve_type
- `delete_curve_table_row` — asset_path; row_name
- `rename_curve_table_row` — asset_path; row_name; new_name
- `import_curve_table` — bulk replace from CSV/JSON — asset_path; format; data; interp
- `create_curve_atlas` — texture atlas from gradient curves — asset_path; width; gradient_curves
- `set_curve_atlas` — modify atlas — asset_path; width; add_curves; remove_curves; set_curves
