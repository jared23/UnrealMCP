# Spec: Blueprint Structs (`/docs/reference/structs`, 6 cmds)

Clean-room. UserDefinedStruct assets. Supports object refs, containers (array/set/map), enums,
instanced structs. Commands auto-save + recompile.

- `create_blueprint_struct` — create struct asset (optional members atomically) — name; path; members(json,opt)
- `add_blueprint_struct_variable` — add member (complex types) — struct_path; name; type; type_path(opt); default(opt)
- `set_blueprint_struct_variable` — modify member name/type/default/tooltip — struct_path; name; new_name/type/default/tooltip(opt)
- `remove_blueprint_struct_variable` — delete member (min 1 remains) — struct_path; name
- `describe_blueprint_struct` — list members (name,guid,type,default,tooltip) — struct_path
- `list_blueprint_structs` — search struct assets (ranked, paginated) — filter; max_results; cursor
