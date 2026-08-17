# Spec: UMG Widgets (`/docs/reference/widgets`, ~50 cmds)

Clean-room. UMG (UserWidget) tree, layout, animation, data binding, MVVM. Implement over
`unreal.WidgetBlueprint`, UMG editor APIs.

**Tree ops:** `get_widget_tree` · `add_widget`(panel child, layout/props) · `remove_widget`(+descendants) · `move_widget`(reparent) · `rename_widget` · `duplicate_widget`(+children) · `replace_widget`(swap class, keep children) · `wrap_widget`(in panel) · `set_root_widget`

**Blueprint/asset:** `create_widget_blueprint`(from UserWidget) · `list_widget_types`(filter) · `get_widget_class_info`(hierarchy, slot behavior) · `set_widget_blueprint_parent`

**Properties/layout:** `get_widget_properties` · `set_widget_properties`(json) · `get_slot_properties`(anchors/padding/alignment) · `set_slot_properties` · `set_widget_is_variable`

**Navigation:** `set_widget_navigation`(directional focus) · `get_widget_navigation` · `clear_widget_navigation`

**Named slots:** `list_named_slots` · `get_named_slot_content` · `set_named_slot_content` · `clear_named_slot`

**Animation:** `create_widget_animation`(MovieScene) · `list_widget_animations` · `remove_widget_animation` · `add_animation_widget_binding` · `remove_animation_widget_binding` · `add_animation_track`(Opacity/Transform/Color) · `add_animation_key`(value) · `list_animation_tracks`

**Data binding (legacy):** `add_property_binding`(→function) · `remove_property_binding` · `list_property_bindings` · `list_widget_events`(BlueprintAssignable)

**MVVM:** `add_viewmodel` · `remove_viewmodel` · `rename_viewmodel` · `list_viewmodels` · `add_mvvm_binding`(vm→widget) · `set_mvvm_binding`(mode/execution/conversion) · `list_mvvm_conversion_functions` · `set_viewmodel_settings` · `set_variable_field_notify` · `remove_mvvm_binding`(guid) · `list_mvvm_bindings`

**UI components (5.8+):** `add_ui_component` · `remove_ui_component` · `list_ui_components`

**Editor:** `set_widget_editor_mode`(Designer/Graph/Preview)
