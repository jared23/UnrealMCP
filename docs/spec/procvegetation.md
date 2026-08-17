# Spec: Procedural Vegetation (`/docs/reference/procvegetation`, 9 cmds — UE 5.8+)

Clean-room. UE 5.8 procedural vegetation graphs. Implement over the PV editor APIs.

**Discovery/schema:** `search_pv_nodes`(filter,filters,category,max_results) · `list_pv_node_categories`(filter) · `describe_pv_nodes`(node,nodes,include_pins,include_properties,max_depth,max_properties,category) · `export_pv_schema`(nodes,name,max_depth)

**Asset mgmt:** `create_procedural_vegetation`(asset_path,sample) · `list_pv_samples`(filter,max_results) · `export_procedural_vegetation`(asset_path,force_append — handles modal confirm) · `export_pcg_graph_config`(graph_path,name,max_depth)

**Editing/preview:** `preview_pv_node`(graph_path,node_name/node_index)
