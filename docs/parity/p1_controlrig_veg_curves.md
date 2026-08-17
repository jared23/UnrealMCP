# Parity Audit P1 — Control Rig, Procedural Vegetation, Curves

Read-only feature-parity audit. Spec files audited: `docs/spec/controlrig.md`,
`docs/spec/procvegetation.md`, `docs/spec/curves.md`.

Legend: `[x]` DONE · `[ ]` not done · ✅ DONE · ⚠️ PARTIAL · ❌ MISSING · ⛔ BLOCKED · ❓ UNCERTAIN.
Implemented-tool inventory built from `grep -rhoE 'def [a-z_]+\(ctx' MCP` plus keyword greps over
`MCP/` and `Source/UnrealMCP/`. Category modules inspected: `controlrig.py`, `controlrig_write.py`,
`controlrig_graph_write.py`, `curves_read.py`, `curves_write.py`, `foliage_write.py`, `pcg.py`.

---

## Control Rig

Tally: DONE 15 · PARTIAL 1 · MISSING 55 · BLOCKED 0 · UNCERTAIN 0 · TOTAL 71

### Rig creation
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `create_control_rig` | ❌ MISSING | No exposed create tool. Factory is used internally only by `controlrig_graph_write.py` scratch fixture; not surfaced as a command. |
| [ ] | `create_control_rig_from_skeleton` | ❌ MISSING | No equivalent. |
| [ ] | `create_control_rig_module` | ❌ MISSING | No equivalent. |
| [ ] | `set_rig_preview_mesh` | ❌ MISSING | No equivalent. |

### Hierarchy / elements
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_rig_bone` | ✅ DONE | `add_rig_bone` (controlrig_write.py) |
| [x] | `add_rig_null` | ✅ DONE | `add_rig_null` (controlrig_write.py) |
| [ ] | `add_rig_socket` | ❌ MISSING | No socket element add. |
| [ ] | `add_rig_curve` | ❌ MISSING | No rig-curve element add. |
| [ ] | `set_rig_element_parent` | ❌ MISSING | No reparent-element command. |
| [x] | `set_rig_element_transform` | ✅ DONE | `set_rig_element_transform` (controlrig_write.py) |
| [x] | `remove_rig_element` | ✅ DONE | `remove_rig_element` (controlrig_write.py) |
| [ ] | `rename_rig_element` | ❌ MISSING | No rename-element command. |
| [ ] | `build_rig_hierarchy` | ❌ MISSING | No batch hierarchy builder. |
| [x] | `get_rig_hierarchy` | ✅ DONE | `get_control_rig_hierarchy` (controlrig.py) |

### Controls
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_rig_control` | ✅ DONE | `add_rig_control` (controlrig_write.py) |
| [ ] | `add_rig_animation_channel` | ❌ MISSING | No animation-channel add. |
| [x] | `set_rig_control_settings` | ✅ DONE | `set_rig_control_settings` (controlrig_write.py) |
| [ ] | `set_rig_control_value` | ❌ MISSING | No control-value/channel setter (distinct from element transform). |
| [x] | `move_rig_control` | ✅ DONE | `set_rig_element_transform` — controls are rig elements; transform setter covers the move-control capability. |
| [ ] | `set_rig_control_offset` | ❌ MISSING | No offset-transform setter. |
| [ ] | `set_rig_control_shape` | ❌ MISSING | No shape/gizmo assignment. |
| [ ] | `list_rig_control_shapes` | ❌ MISSING | No shape-library listing. |
| [ ] | `fit_rig_control_shapes` | ❌ MISSING | No auto-fit. |
| [ ] | `validate_rig_gizmos` | ❌ MISSING | No gizmo validation. |

### RigVM graph
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `add_rig_vm_node` | ✅ DONE | `add_rig_vm_unit_node` (+ `add_rig_vm_comment_node`, `add_rig_vm_variable_node`) covers node-kind-parameterized adds. |
| [x] | `connect_rig_vm_nodes` | ✅ DONE | `add_rig_vm_link` (controlrig_graph_write.py) |
| [x] | `disconnect_rig_vm_nodes` | ✅ DONE | `break_rig_vm_link` (controlrig_graph_write.py) |
| [x] | `set_rig_vm_pin_default` | ✅ DONE | `set_rig_vm_pin_default_value` |
| [x] | `set_rig_vm_node_position` | ✅ DONE | `set_rig_vm_node_position` |
| [x] | `remove_rig_vm_node` | ✅ DONE | `remove_rig_vm_node` |
| [ ] | `build_rig_vm_graph` | ❌ MISSING | No batch nodes+connections builder. |
| [ ] | `layout_rig_vm_graph` | ❌ MISSING | No auto-layout command. |
| [x] | `get_rig_vm_graph_nodes` | ✅ DONE | `get_control_rig_vm_graph` / `get_control_rig_vm` |
| [ ] | `search_rig_vm_nodes` | ⚠️ PARTIAL | `list_control_rig_node_types` (+ `get_rig_vm_struct_pins`) enumerates available node types, but no filtered/max-results search within a graph as spec'd. |

### Functions
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `create_rig_vm_function` | ❌ MISSING | No equivalent. |
| [ ] | `add_rig_vm_function_node` | ❌ MISSING | No equivalent. |
| [ ] | `collapse_rig_vm_nodes` | ❌ MISSING | No equivalent. |
| [ ] | `promote_rig_vm_node` | ❌ MISSING | No equivalent. |
| [ ] | `expand_rig_vm_node` | ❌ MISSING | No equivalent. |

### Modular rigs
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `add_rig_module` | ❌ MISSING | No modular-rig support. |
| [ ] | `connect_rig_module_connector` | ❌ MISSING | No equivalent. |
| [ ] | `auto_connect_rig_modules` | ❌ MISSING | No equivalent. |
| [ ] | `set_rig_module_config` | ❌ MISSING | No equivalent. |
| [ ] | `bind_rig_module_variable` | ❌ MISSING | No equivalent. |
| [ ] | `mirror_rig_module` | ❌ MISSING | No equivalent. |
| [ ] | `list_rig_modules` | ❌ MISSING | No equivalent. |
| [ ] | `list_rig_module_assets` | ❌ MISSING | No equivalent. |

### Physics / validation
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `validate_rig_physics` | ❌ MISSING | No equivalent. |
| [ ] | `validate_rig_deformation` | ❌ MISSING | No equivalent. |
| [ ] | `start_rig_physics_probe` | ❌ MISSING | No equivalent. |
| [ ] | `get_rig_physics_probe_report` | ❌ MISSING | No equivalent. |
| [ ] | `measure_mesh_penetration` | ❌ MISSING | No equivalent. |
| [ ] | `get_skeletal_bone_bounds` | ❌ MISSING | No equivalent. |
| [ ] | `fit_rig_chain_collision` | ❌ MISSING | No equivalent. |

### Motion / perf testing
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `start_rig_motion_capture` | ❌ MISSING | No equivalent. |
| [ ] | `get_rig_motion_report` | ❌ MISSING | No equivalent. |
| [ ] | `simulate_rig` | ❌ MISSING | No equivalent. |
| [ ] | `profile_rig` | ❌ MISSING | No equivalent. |
| [ ] | `play_rig_preview_animation` | ❌ MISSING | No equivalent. |
| [ ] | `stop_rig_preview_animation` | ❌ MISSING | No equivalent. |

### Analysis / debug
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `validate_rig_controls` | ❌ MISSING | No equivalent. |
| [ ] | `validate_rig_graph` | ❌ MISSING | No equivalent. |
| [ ] | `analyze_rig_io` | ❌ MISSING | No equivalent. |
| [ ] | `analyze_rig_control_impact` | ❌ MISSING | No equivalent. |
| [ ] | `get_rig_pose` | ❌ MISSING | No pose-sampling tool (hierarchy read exists but not pose delta). |
| [ ] | `analyze_rig_module_asset` | ❌ MISSING | No equivalent. |
| [ ] | `export_control_rig` | ❌ MISSING | No equivalent. |

### Misc
| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `select_rig_elements` | ❌ MISSING | No equivalent. |
| [ ] | `place_rig_pole_vector` | ❌ MISSING | No equivalent. |
| [ ] | `add_rig_jiggle_bob` | ❌ MISSING | No equivalent. |
| [ ] | `set_rig_autosave` | ❌ MISSING | No equivalent. |

---

## Procedural Vegetation

Tally: DONE 0 · PARTIAL 0 · MISSING 9 · BLOCKED 0 · UNCERTAIN 0 · TOTAL 9

No procedural-vegetation code anywhere (grep for `procedural_vegetation`, `pv_node`, `vegetation`
over `MCP/` and `Source/UnrealMCP/` returned nothing; `foliage_write.py`/`pcg.py` are unrelated
foliage/PCG features). UE 5.8+ feature — may effectively be BLOCKED pending that engine version,
but recorded as MISSING since no equivalent capability exists.

| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [ ] | `search_pv_nodes` | ❌ MISSING | No PV code. |
| [ ] | `list_pv_node_categories` | ❌ MISSING | No PV code. |
| [ ] | `describe_pv_nodes` | ❌ MISSING | No PV code. |
| [ ] | `export_pv_schema` | ❌ MISSING | No PV code. |
| [ ] | `create_procedural_vegetation` | ❌ MISSING | No PV code. |
| [ ] | `list_pv_samples` | ❌ MISSING | No PV code. |
| [ ] | `export_procedural_vegetation` | ❌ MISSING | No PV code. |
| [ ] | `export_pcg_graph_config` | ❌ MISSING | No such tool (PCG read tools exist but no config exporter). |
| [ ] | `preview_pv_node` | ❌ MISSING | No PV code. |

---

## Curves

Tally: DONE 13 · PARTIAL 0 · MISSING 0 · BLOCKED 0 · UNCERTAIN 0 · TOTAL 13

CurveFloat/Vector/LinearColor asset authoring plus the full CurveTable and CurveAtlas families and
key delete/import are all implemented (G2-A, 2026-08-17: curves_write_ext.py). CurveTable ops go via
`unreal.DataTableFunctionLibrary` (5.8's static CurveTable API — the UObject RowMap is C++-protected);
CurveAtlas via `UCurveLinearColorAtlas.gradient_curves`+`texture_size`; key delete/import via the
`MCPReflectionLibrary` get/set_curve_keys_json handler.

| x | spec feature | status | implementing tool / note |
|---|---|---|---|
| [x] | `create_curve` | ✅ DONE | `create_curve_float` / `create_curve_vector` / `create_curve_linear_color` (curves_write.py) — curve_type split across three tools. |
| [x] | `get_curve` | ✅ DONE | `get_curve_info` + `evaluate_curve` (curves_read.py) cover keys/settings/ranges and eval. |
| [x] | `set_curve_keys` | ✅ DONE | `set_curve_keys` (curves_write.py) |
| [x] | `delete_curve_keys` | ✅ DONE | `delete_curve_keys` (curves_write_ext.py) — remove by time / clear channel via MCPReflectionLibrary.set_curve_keys_json; op reuses set_curve_keys. |
| [x] | `import_curve` | ✅ DONE | `import_curve` (curves_write_ext.py) — JSON or CSV; op reuses set_curve_keys. |
| [x] | `create_curve_table` | ✅ DONE | `create_curve_table` (curves_write_ext.py) — CurveTable or CompositeCurveTable via factory; op reuses create_asset. |
| [x] | `get_curve_table` | ✅ DONE | `get_curve_table` (curves_write_ext.py) — READ: row names/keys/evals/json via DataTableFunctionLibrary. |
| [x] | `set_curve_table_row` | ✅ DONE | `set_curve_table_row` (curves_write_ext.py) — simple-curve rows; op set_curve_table. |
| [x] | `delete_curve_table_row` | ✅ DONE | `delete_curve_table_row` (curves_write_ext.py) — op set_curve_table. |
| [x] | `rename_curve_table_row` | ✅ DONE | `rename_curve_table_row` (curves_write_ext.py) — op set_curve_table. |
| [x] | `import_curve_table` | ✅ DONE | `import_curve_table` (curves_write_ext.py) — JSON native / CSV, full REPLACE; op set_curve_table. |
| [x] | `create_curve_atlas` | ✅ DONE | `create_curve_atlas` (curves_write_ext.py) — CurveLinearColorAtlasFactory + gradient_curves; op reuses create_asset. |
| [x] | `set_curve_atlas` | ✅ DONE | `set_curve_atlas` (curves_write_ext.py) — add/remove/replace curves, resize; op set_curve_atlas. |
