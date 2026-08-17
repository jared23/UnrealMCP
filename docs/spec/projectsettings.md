# Spec: Project Settings (`/docs/reference/projectsettings`, 3 cmds)

Clean-room. Mirrors the Project Settings window; persists to `Default*.ini`. Implement over
UDeveloperSettings reflection + config save.

- `search_project_settings` — fuzzy-search sections across containers — filter(str,opt); container(str,opt); max_results(int,opt)
- `get_project_settings` — read a section's properties + metadata — class_name/container/category/section/filter(str,opt); max_results(int,opt); include_metadata(bool,opt)
- `set_project_settings` — write + persist to ini (fires PostEditChangeProperty) — class_name/container/category/section/property_name(str,opt); property_value(json,opt); properties(json,opt). Supports enums, class refs, structs, arrays.
