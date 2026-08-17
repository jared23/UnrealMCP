"""UserTools :: Editor / Lights  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py / editor_spawn.py conventions VERBATIM: base64-injected PARAMS,
the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block.

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines can't corrupt it.

Write safety (agent-scoped undo): every spawner runs inside an `unreal.ScopedEditorTransaction`
AND records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - every light actor CREATED -> {"op": "spawn_actor", "actor_name": <unique>, "label": <label>}
    editor_level.py's unified `undo` already deletes spawn_actor actors (destroying the actor
    reverts every light-component property set here too), so NO `undo` tool is defined here.

Light API notes (verified live on UE 5.8.1):
  - Every light actor exposes its light component at actor.get_editor_property("light_component")
    (PointLight/SpotLight also have point_/spot_light_component aliases; we use light_component
    uniformly). Component classes: PointLightComponent / SpotLightComponent /
    DirectionalLightComponent / RectLightComponent.
  - light_color is an FColor (0-255, sRGB-encoded). You CANNOT set it from a LinearColor via
    set_editor_property (nativize error). Use comp.set_light_color(unreal.LinearColor(r,g,b,a))
    which takes LINEAR 0-1 and stores the sRGB-encoded FColor. We read light_color back as
    [r,g,b,a] ints (0-255) for proof; a linear primary like [1,0,0] reads back [255,0,0,255].
  - intensity / attenuation_radius / inner_cone_angle / outer_cone_angle / source_width /
    source_height are plain float UPROPERTYs set via set_editor_property and read back exactly.
  - DirectionalLight has NO attenuation_radius (infinite/parallel light) — omitted by design.

Implemented (all WRITE; ledgered as spawn_actor):
  - spawn_point_light        (location, intensity?, color?, attenuation_radius?)
  - spawn_spot_light         (location, rotation?, intensity?, color?, inner/outer_cone_angle?, attenuation_radius?)
  - spawn_directional_light  (rotation?, intensity?, color?)
  - spawn_rect_light         (location, rotation?, intensity?, color?, source_width?, source_height?, attenuation_radius?)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py) ------------------
# NB: no ''' and no stray backslashes in this code (the handler wraps code in '''...''').
_LOG_HEAD = (
    "import unreal as _uu, os as _oo, json as _jj\n"
    "def _umcp_logmain():\n"
    "    d=_uu.Paths.convert_relative_path_to_full(_uu.Paths.project_log_dir())\n"
    "    for f in _oo.listdir(d):\n"
    "        if f.endswith('.log') and '-backup-' not in f:\n"
    "            return _oo.path.join(d,f)\n"
    "    return None\n"
    "try:\n"
    "    _umcp_main=_umcp_logmain(); _umcp_s0=_oo.path.getsize(_umcp_main) if _umcp_main else 0\n"
    "except Exception:\n"
    "    _umcp_main=None; _umcp_s0=0\n"
    "try:\n"
)
_LOG_TAILER = (
    "\nfinally:\n"
    "    try:\n"
    "        _uu.log_flush()\n"
    "        if _umcp_main:\n"
    "            _fh=open(_umcp_main,'rb'); _fh.seek(_umcp_s0); _dd=_fh.read().decode('utf-8','replace'); _fh.close()\n"
    "            _ww=[ln for ln in _dd.splitlines() if (': Warning:' in ln or ': Error:' in ln) and 'UMCP' not in ln and 'LogMCP:' not in ln]\n"
    "            if _ww: print('@@UMCP_LOG@@'+_jj.dumps(_ww[-50:]))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _wrap(code):
    """Wrap a snippet with Output-Log delta capture (try/finally)."""
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes ('''...''')
# before exec, so any ''' or backslash in the code corrupts it. Pass all data as base64.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer so its undo ledger is isolated from other writers.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER payload."""
        resp = send_command("execute_python", {"code": _wrap(code)})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        lines = out.splitlines()
        warns = []
        for line in lines:
            if LOG_MARKER in line:
                try:
                    warns = json.loads(line.split(LOG_MARKER, 1)[1])
                except Exception:
                    pass
        for line in reversed(lines):
            if MARKER in line:
                result = json.loads(line.split(MARKER, 1)[1])
                if warns and isinstance(result, dict):
                    result["_log_warnings"] = warns
                return result
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _exec(body, params):
        """Inject PARAMS (base64 JSON) + _session, run the body in Unreal, return MARKER payload."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to write bodies). Verbatim from editor_level.py.
    # Defines the session-aware _ledger(), _settable/_coerce, _resolve_actor/_find_by_name, _descend.
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    # Per-session undo stack so concurrent agents never pop each other's entries.
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off", ""):
                return False
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, str):
            try:
                return int(value.strip())
            except Exception:
                try:
                    return int(float(value.strip()))
                except Exception:
                    return value
        if isinstance(value, (int, float)):
            return int(value)
        return value
    if isinstance(current, float):
        if isinstance(value, str):
            try:
                return float(value.strip())
            except Exception:
                return value
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return value
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
def _find_by_name(uniq):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == uniq:
            return a
    return None
def _descend(root, comp_name, path):
    container = root
    if comp_name:
        found = None
        for c in (root.get_components_by_class(unreal.ActorComponent) or []):
            if c.get_name() == comp_name:
                found = c; break
        if found is None:
            return None, None, "component not found: %s" % comp_name
        container = found
    segs = path.split(".")
    for s in segs[:-1]:
        nxt = container.get_editor_property(s)
        if not isinstance(nxt, unreal.Object):
            return None, None, "cannot descend into non-object '%s' (struct sub-paths unsupported)" % s
        container = nxt
    return container, segs[-1], None
'''

    # Light-specific helpers appended after _COERCE_HELPERS. No ''' / no backslashes.
    # _light_comp(actor): return the light component (uniform light_component property).
    # _apply_intensity/_apply_color/_apply_float: set + read back, recording into an 'applied' dict.
    _LIGHT_HELPERS = r'''
def _light_comp(actor):
    comp = None
    try:
        comp = actor.get_editor_property("light_component")
    except Exception:
        comp = None
    return comp
def _apply_intensity(comp, applied, val):
    if val is not None:
        comp.set_editor_property("intensity", float(val))
        applied["intensity"] = comp.get_editor_property("intensity")
def _apply_color(comp, applied, col):
    # col is LINEAR [r,g,b] or [r,g,b,a] in 0-1. set_light_color takes a LinearColor and stores
    # the sRGB-encoded FColor; we read light_color back as [r,g,b,a] ints (0-255) for proof.
    if col:
        aa = float(col[3]) if len(col) > 3 else 1.0
        comp.set_light_color(unreal.LinearColor(float(col[0]), float(col[1]), float(col[2]), aa))
        lc = comp.get_editor_property("light_color")
        applied["light_color"] = [lc.r, lc.g, lc.b, lc.a]
def _apply_float(comp, applied, prop, val):
    if val is not None:
        comp.set_editor_property(prop, float(val))
        applied[prop] = comp.get_editor_property(prop)
'''

    _LIGHT_PREFIX = _COERCE_HELPERS + _LIGHT_HELPERS

    # ------------------------------------------------------------------ #
    # spawn_point_light — omni light at a location (ledgered spawn)        #
    # ------------------------------------------------------------------ #
    _POINT_BODY = _LIGHT_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
rotation = unreal.Rotator()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
result = {"status": "error", "message": "spawn failed"}
with unreal.ScopedEditorTransaction("MCP spawn_point_light"):
    actor = eas.spawn_actor_from_class(unreal.PointLight, location, rotation)
    if actor:
        actor.set_actor_label(name)
        comp = _light_comp(actor)
        applied = {}
        if comp is not None:
            _apply_intensity(comp, applied, PARAMS.get("intensity"))
            _apply_color(comp, applied, PARAMS.get("color"))
            _apply_float(comp, applied, "attenuation_radius", PARAMS.get("attenuation_radius"))
        uniq = actor.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
        result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                  "class": actor.get_class().get_name(),
                  "comp_class": (comp.get_class().get_name() if comp is not None else None),
                  "location": [location.x, location.y, location.z],
                  "applied": applied, "ledger_depth": len(_ledger())}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_point_light(ctx, name: str, location: list = None, intensity: float = None,
                          color: list = None, attenuation_radius: float = None) -> str:
        """Spawn a PointLight (omni-directional) into the active level and configure it.

        name:               display label for the new light (required).
        location:           [x, y, z] world position (default [0,0,0]).
        intensity:          brightness (candelas/lumens per its intensity units).
        color:              LINEAR [r,g,b] or [r,g,b,a], each 0-1 (set via set_light_color;
                            read back as 0-255 sRGB FColor in 'applied.light_color').
        attenuation_radius: light reach in cm.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it (which
        reverts all component settings with it)."""
        params = {"name": name, "location": location, "intensity": intensity,
                  "color": color, "attenuation_radius": attenuation_radius}
        try:
            return json.dumps(_exec(_POINT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_spot_light — cone light at a location/orientation (ledgered)  #
    # ------------------------------------------------------------------ #
    _SPOT_BODY = _LIGHT_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
result = {"status": "error", "message": "spawn failed"}
with unreal.ScopedEditorTransaction("MCP spawn_spot_light"):
    actor = eas.spawn_actor_from_class(unreal.SpotLight, location, rotation)
    if actor:
        actor.set_actor_label(name)
        comp = _light_comp(actor)
        applied = {}
        if comp is not None:
            _apply_intensity(comp, applied, PARAMS.get("intensity"))
            _apply_color(comp, applied, PARAMS.get("color"))
            _apply_float(comp, applied, "inner_cone_angle", PARAMS.get("inner_cone_angle"))
            _apply_float(comp, applied, "outer_cone_angle", PARAMS.get("outer_cone_angle"))
            _apply_float(comp, applied, "attenuation_radius", PARAMS.get("attenuation_radius"))
        uniq = actor.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
        result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                  "class": actor.get_class().get_name(),
                  "comp_class": (comp.get_class().get_name() if comp is not None else None),
                  "location": [location.x, location.y, location.z],
                  "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                  "applied": applied, "ledger_depth": len(_ledger())}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_spot_light(ctx, name: str, location: list = None, rotation: list = None,
                         intensity: float = None, color: list = None,
                         inner_cone_angle: float = None, outer_cone_angle: float = None,
                         attenuation_radius: float = None) -> str:
        """Spawn a SpotLight (cone) into the active level and configure it.

        name:               display label for the new light (required).
        location:           [x, y, z] world position (default [0,0,0]).
        rotation:           [pitch, yaw, roll] degrees — the cone points down the actor's +X.
        intensity:          brightness.
        color:              LINEAR [r,g,b] or [r,g,b,a], each 0-1 (read back as 0-255 sRGB).
        inner_cone_angle:   full-bright inner cone half-angle in degrees.
        outer_cone_angle:   falloff outer cone half-angle in degrees.
        attenuation_radius: light reach in cm.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "intensity": intensity, "color": color,
                  "inner_cone_angle": inner_cone_angle, "outer_cone_angle": outer_cone_angle,
                  "attenuation_radius": attenuation_radius}
        try:
            return json.dumps(_exec(_SPOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_directional_light — parallel/sun light (ledgered spawn)       #
    # ------------------------------------------------------------------ #
    _DIRECTIONAL_BODY = _LIGHT_PREFIX + r'''
name = PARAMS["name"]
rot = PARAMS.get("rotation") or [0, 0, 0]
location = unreal.Vector(0.0, 0.0, 0.0)
rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
result = {"status": "error", "message": "spawn failed"}
with unreal.ScopedEditorTransaction("MCP spawn_directional_light"):
    actor = eas.spawn_actor_from_class(unreal.DirectionalLight, location, rotation)
    if actor:
        actor.set_actor_label(name)
        comp = _light_comp(actor)
        applied = {}
        if comp is not None:
            _apply_intensity(comp, applied, PARAMS.get("intensity"))
            _apply_color(comp, applied, PARAMS.get("color"))
        uniq = actor.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
        result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                  "class": actor.get_class().get_name(),
                  "comp_class": (comp.get_class().get_name() if comp is not None else None),
                  "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                  "applied": applied, "ledger_depth": len(_ledger())}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_directional_light(ctx, name: str, rotation: list = None,
                                intensity: float = None, color: list = None) -> str:
        """Spawn a DirectionalLight (parallel 'sun' light) into the active level and configure it.

        name:      display label for the new light (required).
        rotation:  [pitch, yaw, roll] degrees — sets the sun direction (only orientation matters
                   for a directional light; it has no position or attenuation).
        intensity: brightness in lux.
        color:     LINEAR [r,g,b] or [r,g,b,a], each 0-1 (read back as 0-255 sRGB).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "rotation": rotation, "intensity": intensity, "color": color}
        try:
            return json.dumps(_exec(_DIRECTIONAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_rect_light — area/rectangle light (ledgered spawn)            #
    # ------------------------------------------------------------------ #
    _RECT_BODY = _LIGHT_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
result = {"status": "error", "message": "spawn failed"}
with unreal.ScopedEditorTransaction("MCP spawn_rect_light"):
    actor = eas.spawn_actor_from_class(unreal.RectLight, location, rotation)
    if actor:
        actor.set_actor_label(name)
        comp = _light_comp(actor)
        applied = {}
        if comp is not None:
            _apply_intensity(comp, applied, PARAMS.get("intensity"))
            _apply_color(comp, applied, PARAMS.get("color"))
            _apply_float(comp, applied, "source_width", PARAMS.get("source_width"))
            _apply_float(comp, applied, "source_height", PARAMS.get("source_height"))
            _apply_float(comp, applied, "attenuation_radius", PARAMS.get("attenuation_radius"))
        uniq = actor.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
        result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                  "class": actor.get_class().get_name(),
                  "comp_class": (comp.get_class().get_name() if comp is not None else None),
                  "location": [location.x, location.y, location.z],
                  "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                  "applied": applied, "ledger_depth": len(_ledger())}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_rect_light(ctx, name: str, location: list = None, rotation: list = None,
                         intensity: float = None, color: list = None,
                         source_width: float = None, source_height: float = None,
                         attenuation_radius: float = None) -> str:
        """Spawn a RectLight (area/rectangle light) into the active level and configure it.

        name:               display label for the new light (required).
        location:           [x, y, z] world position (default [0,0,0]).
        rotation:           [pitch, yaw, roll] degrees — the rectangle emits down the actor's +X.
        intensity:          brightness.
        color:              LINEAR [r,g,b] or [r,g,b,a], each 0-1 (read back as 0-255 sRGB).
        source_width:       width of the emissive rectangle in cm.
        source_height:      height of the emissive rectangle in cm.
        attenuation_radius: light reach in cm.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "intensity": intensity, "color": color,
                  "source_width": source_width, "source_height": source_height,
                  "attenuation_radius": attenuation_radius}
        try:
            return json.dumps(_exec(_RECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
