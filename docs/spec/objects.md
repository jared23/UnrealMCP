# Spec: Object Properties (`/docs/reference/objects`, 3 cmds — **M1**)

Interface reference (clean-room). Three UNIFIED reflection commands that target any object via a
selector (one of: blueprint / actor / asset / component / widget). Implement over UObject reflection
(`get_editor_property`/`set_editor_property`, FProperty walking). MCP params use underscores
(`blueprint_path`), not the CLI's hyphens.

**Design win to adopt:** collapse our separate `get/set_actor_property` + `get/set_object_property`
into this one family with a target selector — less duplication, works everywhere.

- `describe_object` — introspect properties w/ filter/pagination/type-metadata
  - target: blueprint_path(str,opt) | actor(str,opt) | asset_path(str,opt); component(str,opt); widget(str,opt)
  - property_path(str,opt); filter(str,opt); category(str,opt); max_results(int,opt); cursor(str,opt); max_depth(int,opt)
  - → returns cpp_type, ue_type, category, flags, clamp ranges, enum values, current values
- `get_object_property` — read one value at dotted/indexed path
  - target (blueprint_path|actor|asset_path,opt); component(str,opt); property_path(str,req) e.g. `BodyInstance.bEnableGravity`, `Tags[0]`
  - → single value as JSON
- `set_object_property` — set / append / remove at dotted/indexed paths
  - target (blueprint_path|actor|asset_path,opt); component(str,opt); widget(str,opt); slot(bool,opt: target widget layout slot)
  - property_path(str,opt) + value(json,opt) for single; properties(map path→value,opt) for batch
  - remove_path(str,opt) / remove_paths(array,opt) for array-element deletion
  - → { applied[], removed[], errors[] }
