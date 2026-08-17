# Parity Audit P3 — Niagara / VFX, Materials

Read-only feature-parity audit. Spec files audited: `docs/spec/niagara.md`, `docs/spec/materials.md`.

Legend: `[x]` DONE · `[ ]` not done · ✅ DONE · ⚠️ PARTIAL · ❌ MISSING · ⛔ BLOCKED · ❓ UNCERTAIN.
Implemented-tool inventory built from `grep -rhoE 'def [a-z_]+\(ctx' MCP` plus keyword greps over
`MCP/` and `Source/UnrealMCP/`. Modules inspected: `niagara_read.py`, `niagara_write.py`,
`material_write.py`, `material_graph_write.py`, `materials_assign.py`, `texture_write.py`,
`texture_read.py`, `Commands/commands_materials.py`, `editor_discovery.py`, and C++
`Source/UnrealMCP/Private/MCPCommandHandlers_Materials.cpp`.

**Key caveat (Niagara):** every authoring tool in `niagara_write.py` is a Python wrapper that
dispatches to a C++ handler and carries an explicit fallback message
`"C++ handler <name> unavailable (plugin DLL predates C++ #5/#10; recompile needed)"`. The tool
+ handler code exists in the repo, but on a shipped DLL that predates those C++ waves the calls
return "unavailable". Marked ✅ DONE here on a code-exists basis; live availability depends on the
recompiled Windows DLL and was NOT probed (read-only audit).

**Niagara has NO C++ handlers in `Source/UnrealMCP/`** (grep for niagara command strings returns
nothing). The authoring path is Python-side dispatch to an engine-patched DLL; no niagara handler
`.cpp` exists in this repo tree.

---

## Niagara / VFX

Tally: DONE 16 · PARTIAL 2 · MISSING 34 · BLOCKED 0 · UNCERTAIN 0 · TOTAL 52

### System
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `create_niagara_system` | ✅ DONE | `create_niagara_system` (niagara_write.py) — signature is `(name, package_path)`; the spec `template` arg is not supported (creates empty system only). |
| [x] | `get_niagara_system_info` | ✅ DONE | `get_niagara_system_info` (niagara_read.py) — returns emitters + user parameters + warmup/bounds/determinism. |
| [x] | `list_niagara_systems` | ✅ DONE | `list_niagara_assets` (niagara_read.py) — `kind` filter selects NiagaraSystem; folds list-systems + list-emitters. |
| [x] | `delete_niagara_system` | ✅ DONE | Generic `delete_asset` covers it; no niagara-specific delete tool. |
| [ ] | `compile_niagara_system` | ⚠️ PARTIAL | No standalone compile tool. Every `niagara_write` op auto-compiles + saves via internal `save_niagara_system` (sync compile in C++). No way to compile on demand without a mutation. |

### Emitters
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_niagara_emitter` | ✅ DONE | `add_emitter_to_system` (niagara_write.py) |
| [x] | `remove_niagara_emitter` | ✅ DONE | `remove_emitter_from_system` (niagara_write.py) |
| [x] | `get_niagara_emitters` | ✅ DONE | Folded into `get_niagara_system_info` (get_all_emitters); `get_niagara_emitter_info` for standalone emitter assets. |
| [ ] | `duplicate_niagara_emitter` | ❌ MISSING | No equivalent. |
| [ ] | `reorder_niagara_emitter` | ❌ MISSING | No equivalent. |

### Module stack
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_niagara_module` | ✅ DONE | `add_niagara_module_to_stack` (niagara_write.py) |
| [x] | `remove_niagara_module` | ✅ DONE | `remove_niagara_module_from_stack` (niagara_write.py) |
| [ ] | `get_niagara_modules` | ❌ MISSING | Stack/graph is editor-only and NOT exposed read-side (documented limit in niagara_read.py). |
| [ ] | `set_niagara_module_enabled` | ❌ MISSING | No equivalent. |
| [ ] | `reorder_niagara_module` | ❌ MISSING | No equivalent. |

### Module inputs
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `set_niagara_module_input` | ✅ DONE | `set_niagara_module_input` (niagara_write.py) — accepts JSON scalar values. |
| [ ] | `get_niagara_module_inputs` | ❌ MISSING | No read-side module input introspection. |
| [ ] | `set_niagara_dynamic_input` | ❌ MISSING | Dynamic inputs explicitly unsupported — `set_niagara_module_input` returns an error for them. |
| [ ] | `set_niagara_curve` | ❌ MISSING | No equivalent. |
| [ ] | `set_niagara_stack_value` | ❌ MISSING | No universal stack setter. |

### Renderers
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_niagara_renderer` | ✅ DONE | `add_niagara_renderer` (niagara_write.py) |
| [x] | `remove_niagara_renderer` | ✅ DONE | `remove_niagara_renderer` (niagara_write.py) |
| [ ] | `get_niagara_renderer_info` | ⚠️ PARTIAL | Renderer materials + meshes surface via `get_niagara_system_info` (get_all_emitters). No renderer-property-level introspection nor standalone renderer-info tool. |
| [ ] | `set_niagara_renderer_property` | ❌ MISSING | No equivalent. |
| [ ] | `set_niagara_renderer_binding` | ❌ MISSING | No equivalent. |

### User params
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_niagara_user_parameter` | ✅ DONE | `add_niagara_user_parameter` (niagara_write.py) |
| [x] | `get_niagara_user_parameters` | ✅ DONE | Folded into `get_niagara_system_info` (get_all_user_parameters). |
| [x] | `set_niagara_user_parameter` | ✅ DONE | `set_niagara_user_parameter_value` (niagara_write.py) |
| [x] | `remove_niagara_user_parameter` | ✅ DONE | `remove_niagara_user_parameter` (niagara_write.py) |

### Graph / script
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `create_niagara_scratch_pad_module` | ❌ MISSING | No equivalent. |
| [ ] | `create_niagara_module_asset` | ❌ MISSING | No equivalent. |
| [ ] | `build_niagara_graph` | ❌ MISSING | No equivalent. |
| [ ] | `add_niagara_graph_node` | ❌ MISSING | No equivalent. |
| [ ] | `delete_niagara_graph_node` | ❌ MISSING | No equivalent. |
| [ ] | `layout_niagara_graph` | ❌ MISSING | No equivalent. |

### Inspect / validate
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `get_niagara_graph_nodes` | ❌ MISSING | Graph read-side not exposed. |
| [ ] | `get_niagara_node_info` | ❌ MISSING | No equivalent. |
| [ ] | `trace_niagara_connection` | ❌ MISSING | No equivalent. |
| [ ] | `validate_niagara_graph` | ❌ MISSING | No equivalent. |
| [ ] | `validate_niagara_stack` | ❌ MISSING | No equivalent. |
| [ ] | `fix_niagara_stack_issue` | ❌ MISSING | No equivalent. |
| [ ] | `get_niagara_system_errors` | ❌ MISSING | No equivalent (compile status returned inline on writes, not queryable). |
| [ ] | `get_niagara_particle_stats` | ❌ MISSING | No equivalent. |

### Runtime
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `spawn_niagara_effect` | ❌ MISSING | No equivalent. |
| [ ] | `control_niagara_effect` | ❌ MISSING | No equivalent. |
| [ ] | `add_niagara_component` | ❌ MISSING | No niagara-specific component adder. |
| [ ] | `get_niagara_actors` | ❌ MISSING | No equivalent. |

### Discovery
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `list_niagara_asset_types` | ❌ MISSING | No equivalent. |
| [ ] | `list_niagara_modules` | ❌ MISSING | No equivalent. |
| [ ] | `list_niagara_emitter_templates` | ❌ MISSING | No equivalent. |
| [ ] | `list_niagara_data_interfaces` | ❌ MISSING | No equivalent. |
| [ ] | `list_niagara_parameter_types` | ❌ MISSING | No equivalent. |

---

## Materials

Tally: DONE 9 · PARTIAL 2 · MISSING 40 · BLOCKED 0 · UNCERTAIN 0 · TOTAL 51

Related non-spec coverage present: `connect_material_property` / `disconnect_material_property`
(wire an expression to a material output pin, e.g. BaseColor), `get_material_slots`
(editor_discovery.py), `set_actor_material` / `set_materials_batch` (actor mesh assignment).

### Create / manage
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `create_material` | ✅ DONE | `create_material` (commands_materials.py + C++ MCPCommandHandlers_Materials.cpp) |
| [x] | `create_material_instance` | ✅ DONE | `create_material_instance` (material_write.py) — sets parent internally via MEL. |
| [ ] | `create_material_function` | ❌ MISSING | No equivalent. |
| [ ] | `create_material_function_instance` | ❌ MISSING | No equivalent. |
| [ ] | `create_material_parameter_collection` | ❌ MISSING | No equivalent. |

### Graph build / layout
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `build_material_graph` | ❌ MISSING | No batch graph builder; only per-node `add_material_expression`. |
| [ ] | `build_material_function_graph` | ❌ MISSING | No equivalent. |
| [ ] | `layout_material_graph` | ❌ MISSING | No equivalent. |
| [ ] | `layout_material_expressions` | ❌ MISSING | No equivalent. |
| [ ] | `layout_material_function_graph` | ❌ MISSING | No equivalent. |

### Nodes
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_material_expression` | ✅ DONE | `add_material_expression` (material_graph_write.py) — auto recompiles + saves. |
| [ ] | `add_material_comments` | ❌ MISSING | No equivalent. |
| [ ] | `add_material_function_input` | ❌ MISSING | No material-function authoring at all. |
| [ ] | `add_material_function_output` | ❌ MISSING | No equivalent. |
| [x] | `connect_material_expressions` | ✅ DONE | `connect_material_expressions` (material_graph_write.py) |
| [x] | `disconnect_material_expression` | ✅ DONE | `disconnect_material_expressions` (material_graph_write.py) |
| [ ] | `delete_material_expression` | ❌ MISSING | DEFERRED — present only in module docstrings/comments as "refused-not-faked"; no registered tool. |
| [ ] | `move_material_expression` | ❌ MISSING | Only referenced in comments; not a tool. |
| [ ] | `duplicate_material_expression` | ❌ MISSING | No equivalent. |
| [x] | `set_material_expression_property` | ✅ DONE | `set_material_expression_property` (material_graph_write.py) |

### Properties / params
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `set_material_properties` | ✅ DONE | `modify_material` (commands_materials.py) covers shading_model/blend_mode/two_sided/dithered_lod_transition/base_color/metallic/roughness. Advanced spec sub-args (opacity_mask_clip_value, translucency_lighting_mode, refraction_method, allow_negative_emissive_color) not confirmed exposed. |
| [x] | `set_material_instance_parameter` | ✅ DONE | Parameterized-fold: `set_material_instance_scalar` / `set_material_instance_vector` / `set_material_instance_texture` (material_write.py). |
| [ ] | `set_material_instance_parent` | ❌ MISSING | Only an internal MEL call inside `create_material_instance`; not a standalone tool. |
| [ ] | `clear_material_instance_parameter` | ❌ MISSING | No equivalent. |
| [ ] | `set_material_function_instance_parameter` | ❌ MISSING | No equivalent. |
| [ ] | `set_material_function_input` / `set_material_function_output` | ❌ MISSING | No material-function authoring. |
| [ ] | `set_material_parameter_collection_parameter` | ❌ MISSING | No MPC support. |
| [ ] | `set_material_layers` | ❌ MISSING | No material-layers support. |

### Inspect
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `get_material_info` | ✅ DONE | `get_material_info` (commands_materials.py + C++ handler) |
| [ ] | `get_material_errors` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_graph_nodes` | ❌ MISSING | No graph read-side enumeration tool. |
| [ ] | `get_material_expression_info` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_property_connections` | ❌ MISSING | No equivalent. |
| [ ] | `get_available_material_pins` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_instance_parameters` | ❌ MISSING | No equivalent (typed setters exist; no reader). |
| [ ] | `get_expression_type_info` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_function_info` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_function_instance_parameters` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_layers` | ❌ MISSING | No equivalent. |
| [ ] | `get_material_parameter_collection` | ❌ MISSING | No equivalent. |

### Compile / validate / cleanup
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `recompile_material` | ❌ MISSING | `MEL.recompile_material` is invoked internally by graph-write ops; no standalone recompile tool. |
| [ ] | `apply_material` | ❌ MISSING | No material-editor apply tool. `set_actor_material` (materials_assign.py) assigns a material to an actor mesh slot — different capability. |
| [ ] | `material_stats` | ❌ MISSING | No equivalent. |
| [ ] | `validate_material_graph` | ❌ MISSING | No equivalent. |
| [ ] | `validate_material_function` | ❌ MISSING | No equivalent. |
| [ ] | `trace_material_connection` | ❌ MISSING | No equivalent. |
| [ ] | `cleanup_material_graph` | ❌ MISSING | No equivalent. |
| [ ] | `cleanup_material_function` | ❌ MISSING | No equivalent. |

### Search / MPC
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `list_material_expression_types` | ⚠️ PARTIAL | No dedicated tool; generic `search_classes` can enumerate MaterialExpression subclasses. |
| [ ] | `search_material_functions` | ⚠️ PARTIAL | No dedicated tool; generic `find_assets` with a MaterialFunction class filter can locate them. |
| [ ] | `delete_material_parameter_collection_parameter` | ❌ MISSING | No MPC support. |

---

## Totals

- Niagara: DONE 16 · PARTIAL 2 · MISSING 34 · TOTAL 52
- Materials: DONE 9 · PARTIAL 2 · MISSING 40 · TOTAL 51
- **Combined: DONE 25 · PARTIAL 4 · MISSING 74 · TOTAL 103**
