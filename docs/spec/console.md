# Spec: Console Commands (`/docs/reference/console`, 4 cmds)

Clean-room. Engine console commands + cvars via IConsoleManager. Implement over
`unreal.SystemLibrary.execute_console_command` + console manager introspection.

- `list_console_commands` — list engine+custom commands/cvars — filter(str,opt); max_results(int,opt,0=unlimited); include_help(bool,opt); include_values(bool,opt); type_filter(all/command/variable)
- `get_console_command_info` — metadata for one by exact name — name(str,req)
- `console_command_exists` — existence check (misses FSelfRegisteringExec/stat/show) — name(str,req)
- `execute_console_command` — run any console command — command(str,req e.g. `r.ScreenPercentage 80`); capture_logs(bool,opt); flush_threaded(bool,opt). ⚠ side effects (e.g. `Quit` closes editor)
