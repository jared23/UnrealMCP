# Spec: Core (`/docs/reference/core`, 7 cmds — infra)

Interface reference (clean-room). Connection/session + python + diagnostics. Our fork already
implements `execute_python`; the connect/list ones matter if we ever address multiple editors.

- `health_check` — report CLI, license, editor-bridge status — (none)
- `execute_python` — run arbitrary Python in the running editor — code(str,req)  *(already in our fork)*
- `dump_command_schema` — dump reflection-generated schema for reflected commands — name(str,opt: one command; else all)
- `list_editors` — list running editors (pid, project, engine, port) — (none)
- `connect_editor` — pick which running editor later commands hit — project(str,opt); port(int,opt)
- `disconnect_editor` — clear active editor selection — (none)
- `connection_status` — show active selection / which editor commands would hit — (none)

Note: their `dump_command_schema` mirrors a CLI↔C++ schema-drift check — not relevant to our
Python-UserTools approach, but the *pattern* (a `list_commands`/schema introspection tool) is a nice
self-documentation add for our MCP later.
