# Spec: Enhanced Input (`/docs/reference/input`, 21 cmds)

Clean-room. UInputAction + UInputMappingContext (UE5 Enhanced Input). Implement over the input
asset APIs. `req`/`opt`.

**Actions:** `create_input_action`(asset_path,value_type,properties) · `get_input_action`(asset_path) · `set_input_action_properties`(value_type,properties) · `add_input_action_trigger`(trigger_type,properties) · `add_input_action_modifier`(modifier_type,properties) · `remove_input_action_trigger`(index) · `remove_input_action_modifier`(index) · `list_input_actions`(path,filter,recursive,max_results)

**Mapping contexts:** `create_input_mapping_context`(asset_path,description) · `get_input_mapping_context`(asset_path) · `add_key_mapping`(context_path,action_path,key,triggers,modifiers) · `remove_key_mapping`(context_path,index | action_path,key) · `set_key_mapping`(context_path,index,key,action_path) · `add_mapping_trigger`(mapping_index,trigger_type,properties) · `add_mapping_modifier`(mapping_index,modifier_type,properties) · `remove_mapping_trigger`(mapping_index,trigger_index) · `remove_mapping_modifier`(mapping_index,modifier_index) · `list_input_mapping_contexts`(path,filter,recursive,max_results)

**Discovery:** `list_trigger_types`(filter) · `list_modifier_types`(filter) · `list_input_keys`(filter,category,max_results)
