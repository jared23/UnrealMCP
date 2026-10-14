# UnrealMCP domain split — connect only the tools a session needs

The plugin exposes ~1,058 tools. Loading all of them into one MCP session costs
~468k tokens of tool definitions and, more importantly, degrades tool-selection
accuracy badly (selection quality falls off a cliff past a few dozen tools). This
split lets a session load **only the domains it needs** — e.g. an animation
session sees ~412 tools instead of 1,058.

## How it works

One environment variable, `UNREALMCP_DOMAINS`, tells the bridge which domains to
register. It's a comma-separated list of domain names. **If it is unset, every
tool loads — identical to the original behavior** (fully backward compatible).

Each of the ~1,058 tools belongs to exactly one domain (see `domain_manifest.py`).
`core` is an ordinary domain that you list explicitly — that keeps every tool in
exactly one server, so connecting several servers never duplicates a tool.

Domains: `core anim rig world vfx blueprint material audio ui mutable misc`.

| domain | tools | what's in it |
|---|---|---|
| `core` | 30 | always-useful: batch, undo/redo, screenshot, viewport, playhead, asset lookup, save, engine/project info, object reflection, console/python exec |
| `anim` | ~104 | animation, sequencer, montage, curves, keys, tracks |
| `rig` | ~86 | control rig, skeleton, deformation |
| `world` | ~132 | actors, spawning, layers, lighting build, volumes, world partition, HLOD, nav, debug draw |
| `vfx` | ~152 | niagara, pcg, foliage, landscape, procedural vegetation |
| `blueprint` | ~244 | blueprint graphs/components, BT, statetree, EQS, GAS, gameplay tags, data tables/assets, input |
| `material` | ~71 | material graphs, instances, functions, textures |
| `audio` | ~48 | sound cues, metasound, submix, attenuation |
| `ui` | ~57 | UMG widgets, MVVM |
| `mutable` | ~33 | customizable-object (character customization) system |
| `misc` | ~41 | asset move/delete, profiling/trace, console vars, physics, project settings, mcp meta |

## Ready-to-paste server presets

Each server entry is the **same** `command` + `args` you already use for the single
`unreal` server — only the server **name** and the `env` block differ. Copy your
existing entry's `command`/`args` (the venv python + `unreal_mcp_bridge.py` path),
then add the `env` shown below. Connect only the server(s) you need for the session.

```jsonc
{
  "mcpServers": {
    // Animation / rigging / sequencer session  (~412 tools)
    "unreal-anim": {
      "command": "<path>\\MCP\\python_env\\Scripts\\python.exe",
      "args": ["<path>\\MCP\\unreal_mcp_bridge.py"],
      "env": { "UNREALMCP_DOMAINS": "core,anim,rig,world" }
    },

    // VFX session: niagara / pcg / foliage / landscape  (~344 tools)
    "unreal-vfx": {
      "command": "<path>\\MCP\\python_env\\Scripts\\python.exe",
      "args": ["<path>\\MCP\\unreal_mcp_bridge.py"],
      "env": { "UNREALMCP_DOMAINS": "core,vfx,world" }
    },

    // Gameplay session: blueprint / BT / statetree / EQS / GAS / input  (~437 tools)
    "unreal-gameplay": {
      "command": "<path>\\MCP\\python_env\\Scripts\\python.exe",
      "args": ["<path>\\MCP\\unreal_mcp_bridge.py"],
      "env": { "UNREALMCP_DOMAINS": "core,blueprint,world" }
    },

    // Focused single-domain sessions
    "unreal-material": { "command": "...", "args": ["..."], "env": { "UNREALMCP_DOMAINS": "core,material" } },
    "unreal-ui":       { "command": "...", "args": ["..."], "env": { "UNREALMCP_DOMAINS": "core,ui" } },
    "unreal-audio":    { "command": "...", "args": ["..."], "env": { "UNREALMCP_DOMAINS": "core,audio" } },
    "unreal-mutable":  { "command": "...", "args": ["..."], "env": { "UNREALMCP_DOMAINS": "core,mutable" } }
  }
}
```

Rules of thumb:
- **List `core` in every preset** — that's where undo/screenshot/save/batch live.
- **Add a domain when you need it**, e.g. `core,anim,rig,world,material` if an anim
  session also needs material tweaks.
- **`misc`** (asset move/delete, profiling, console vars) is off by default; add it
  when you need those: `...,misc`.
- **Full surface**: leave `env` empty / omit `UNREALMCP_DOMAINS` to load all 1,058.

### Composable alternative (advanced)

If you prefer à-la-carte, run a dedicated core server plus pure single-domain
servers and connect several at once — no duplication, because `core` lives only in
the core server:

```jsonc
"unreal-core": { "env": { "UNREALMCP_DOMAINS": "core" } },   // 30 tools
"unreal-anim-only": { "env": { "UNREALMCP_DOMAINS": "anim" } },
"unreal-rig-only":  { "env": { "UNREALMCP_DOMAINS": "rig"  } }
```

## Verifying the win

The token/selection improvement can only be confirmed from inside a session:

1. Reconfigure MCP to connect a preset (e.g. `unreal-anim`) instead of the full server.
2. Start a **fresh** session and run `/context`.
3. Confirm the "unreal" tool line shows ~412 tools / ~180k tokens, not ~1,058 / ~468k.

If the number didn't move, the env var isn't reaching the bridge — check the
server's `env` block and that the session connected the preset, not the old server.

## Multiple bridges, one editor

Every server is a thin bridge process that connects to the **same** C++ server
(port 13377) inside the editor, with a fresh socket per command. Running several
bridges at once is safe — the C++ side is stateless request/response.
