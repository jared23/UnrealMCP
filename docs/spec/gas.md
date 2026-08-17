# Spec: GAS — Gameplay Ability System (`/docs/reference/gas`, ~25 cmds)

Clean-room. Gameplay Tags, Attributes/AttributeSets, Gameplay Effects, Abilities, Cues. Implement
over GAS + gameplay-tags editor APIs.

**Tags:** list_gameplay_tags · get_gameplay_tag · add_gameplay_tag(ini source) · remove_gameplay_tag · rename_gameplay_tag(redirector) · add_gameplay_tag_source · list_gameplay_tag_sources · validate_gameplay_tags

**Attributes/sets:** list_attribute_sets · list_attributes(set) · create_attribute_set(BP, optional attributes) · add_attribute(FGameplayAttributeData) · validate_attribute_set(replication,data types)

**Effects:** create_gameplay_effect(BP) · list_gameplay_effect_components · add_gameplay_effect_component · remove_gameplay_effect_component(by class) · set_gameplay_effect_modifier(attribute modifier) · validate_gameplay_effect

**Abilities/cues:** create_gameplay_ability(BP) · create_gameplay_cue_notify(bound to tag) · list_gameplay_cue_notifies · validate_gameplay_cues(tags vs notifies)

**Search/inspect:** search_gas(ranked across tags/attributes/effects/abilities) · get_ability_system_info(actor's ASC)
