"""UserTools :: Editor / Spawn Environment  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py / spawn_extras.py / volumes_write.py conventions VERBATIM:
base64-injected PARAMS, the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the
session-aware per-agent undo ledger, and the _COERCE_HELPERS block.

Reversible spawners for ATMOSPHERE / ENVIRONMENT actors NOT covered by spawn_extras
(camera/trigger/text/decal/empty/static-mesh), volumes_write (AVolume shapes) or the
native light spawners (editor_lights: point/spot/directional/rect):
  - spawn_sky_light                -> SkyLight            (SkyLightComponent)
  - spawn_sky_atmosphere           -> SkyAtmosphere       (SkyAtmosphereComponent)
  - spawn_exponential_height_fog   -> ExponentialHeightFog(ExponentialHeightFogComponent)
  - spawn_volumetric_cloud         -> VolumetricCloud     (VolumetricCloudComponent)
  - spawn_sphere_reflection_capture-> SphereReflectionCapture(SphereReflectionCaptureComponent)
  - spawn_box_reflection_capture   -> BoxReflectionCapture(BoxReflectionCaptureComponent)

All six resolve on unreal.* (getattr) with a /Script/Engine.<Class> load_class fallback,
spawn NON-MODAL via EditorActorSubsystem.spawn_actor_from_class(cls, location, rotation)
(no factory, no asset creation -> inherently non-modal), and expose the relevant environment
component as the actor's root_component (probed live 2026-08-15).

Write safety (agent-scoped undo): every spawner runs inside an `unreal.ScopedEditorTransaction`
AND records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - every actor CREATED -> {"op": "spawn_actor", "actor_name": <unique>, "label": <label>}
    editor_level.py's unified `undo` already deletes spawn_actor actors (destroying the actor
    reverts every component property configured here with it), so NO `undo` tool is defined
    here and NO new undo op/branch is needed.

Optional editor props (all float/bool, applied via the reused _coerce, read back into 'applied';
each read/write proven live). Omit any to leave the engine default:
  - sky_light: intensity, lower_hemisphere_is_black
  - exponential_height_fog: fog_density, fog_height_falloff
  - volumetric_cloud: layer_bottom_altitude, layer_height
  - sphere_reflection_capture: influence_radius, brightness
  - box_reflection_capture: brightness
  (sky_atmosphere: transform + label only.)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / spawn_extras.py) ------
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

    # Environment-spawner helpers. No ''' / no backslashes.
    # _resolve_env_class: getattr(unreal, name) then load_class(None, "/Script/Engine.<name>").
    # _spawn: EditorActorSubsystem.spawn_actor_from_class (no factory -> non-modal).
    # _apply_props: for each optional prop present in PARAMS, read current -> _coerce -> set ->
    #               record json-safe readback in 'applied' (the destroy-on-undo reverts all of it).
    _ENV_HELPERS = r'''
def _resolve_env_class(cname):
    cls = getattr(unreal, cname, None)
    if cls is None:
        try:
            cls = unreal.load_class(None, "/Script/Engine." + cname)
        except Exception:
            cls = None
    return cls
def _spawn(cls, loc, rot):
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return eas.spawn_actor_from_class(cls, location, rotation)
def _root_of(actor):
    try:
        return actor.get_editor_property("root_component")
    except Exception:
        return None
def _json_safe(v):
    sv, ok = _settable(v)
    return sv if ok else str(v)
def _apply_props(comp, prop_names):
    applied = {}
    for pn in prop_names:
        val = PARAMS.get(pn)
        if val is None:
            continue
        if comp is None:
            applied[pn] = "no-component"
            continue
        try:
            cur = comp.get_editor_property(pn)
        except Exception:
            applied[pn] = "unreadable"
            continue
        try:
            comp.set_editor_property(pn, _coerce(cur, val))
            applied[pn] = _json_safe(comp.get_editor_property(pn))
        except Exception:
            applied[pn] = "set-failed"
    return applied
def _spawn_env(target, name, loc, rot, prop_names):
    cls = _resolve_env_class(target)
    if cls is None:
        return {"status": "error", "message": "%s class unavailable" % target}
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_" + target):
        actor = _spawn(cls, loc, rot)
        if actor:
            actor.set_actor_label(name)
            comp = _root_of(actor)
            applied = _apply_props(comp, prop_names)
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location(); r = actor.get_actor_rotation()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "root_component": (comp.get_class().get_name() if comp is not None else None),
                      "location": [l.x, l.y, l.z], "rotation": [r.pitch, r.yaw, r.roll],
                      "applied": applied, "ledger_depth": len(_ledger())}
    return result
'''

    _ENV_PREFIX = _COERCE_HELPERS + _ENV_HELPERS

    # ------------------------------------------------------------------ #
    # spawn_sky_light — SkyLight (ambient image-based lighting)            #
    # ------------------------------------------------------------------ #
    _SKYLIGHT_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("SkyLight", name, loc, rot,
      ["intensity", "lower_hemisphere_is_black"])))
'''

    @mcp.tool()
    def spawn_sky_light(ctx, name: str = "MCP_B_SkyLight", location: list = None,
                        rotation: list = None, intensity: float = None,
                        lower_hemisphere_is_black: bool = None) -> str:
        """Spawn a SkyLight (captures the scene/HDRI for ambient image-based lighting).

        name:      display label for the new actor (default 'MCP_B_SkyLight').
        location:  [x, y, z] world position (default [0,0,0]).
        rotation:  [pitch, yaw, roll] degrees.
        intensity: optional SkyLightComponent intensity scale (read back in 'applied.intensity').
        lower_hemisphere_is_black: optional bool — zero the lower-hemisphere contribution.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "intensity": intensity, "lower_hemisphere_is_black": lower_hemisphere_is_black}
        try:
            return json.dumps(_exec(_SKYLIGHT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_sky_atmosphere — SkyAtmosphere (physical sky/atmosphere)       #
    # ------------------------------------------------------------------ #
    _SKYATMOS_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("SkyAtmosphere", name, loc, rot, [])))
'''

    @mcp.tool()
    def spawn_sky_atmosphere(ctx, name: str = "MCP_B_SkyAtmosphere", location: list = None,
                             rotation: list = None) -> str:
        """Spawn a SkyAtmosphere (physically-based sky / aerial perspective) into the level.

        name:     display label for the new actor (default 'MCP_B_SkyAtmosphere').
        location: [x, y, z] world position (default [0,0,0]).
        rotation: [pitch, yaw, roll] degrees.

        Transform + label only (the atmosphere's rich props are struct/color-typed and left at
        engine defaults; tune them afterward via set_object_property on the component).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation}
        try:
            return json.dumps(_exec(_SKYATMOS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_exponential_height_fog — ExponentialHeightFog                  #
    # ------------------------------------------------------------------ #
    _FOG_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("ExponentialHeightFog", name, loc, rot,
      ["fog_density", "fog_height_falloff"])))
'''

    @mcp.tool()
    def spawn_exponential_height_fog(ctx, name: str = "MCP_B_HeightFog", location: list = None,
                                     rotation: list = None, fog_density: float = None,
                                     fog_height_falloff: float = None) -> str:
        """Spawn an ExponentialHeightFog (global height-based atmospheric fog) into the level.

        name:               display label (default 'MCP_B_HeightFog').
        location:           [x, y, z] world position (default [0,0,0]) — fog is global; the
                            Z position sets the fog height origin.
        rotation:           [pitch, yaw, roll] degrees.
        fog_density:        optional ExponentialHeightFogComponent fog_density (default ~0.02;
                            read back in 'applied.fog_density').
        fog_height_falloff: optional height falloff rate (default ~0.2).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "fog_density": fog_density, "fog_height_falloff": fog_height_falloff}
        try:
            return json.dumps(_exec(_FOG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_volumetric_cloud — VolumetricCloud                             #
    # ------------------------------------------------------------------ #
    _CLOUD_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("VolumetricCloud", name, loc, rot,
      ["layer_bottom_altitude", "layer_height"])))
'''

    @mcp.tool()
    def spawn_volumetric_cloud(ctx, name: str = "MCP_B_VolumetricCloud", location: list = None,
                               rotation: list = None, layer_bottom_altitude: float = None,
                               layer_height: float = None) -> str:
        """Spawn a VolumetricCloud (raymarched volumetric cloud layer) into the level.

        name:                  display label (default 'MCP_B_VolumetricCloud').
        location:              [x, y, z] world position (default [0,0,0]) — cloud is global.
        rotation:              [pitch, yaw, roll] degrees.
        layer_bottom_altitude: optional VolumetricCloudComponent layer_bottom_altitude in km
                               (default ~5.0; read back in 'applied.layer_bottom_altitude').
        layer_height:          optional cloud layer thickness in km (default ~10.0).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "layer_bottom_altitude": layer_bottom_altitude, "layer_height": layer_height}
        try:
            return json.dumps(_exec(_CLOUD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_sphere_reflection_capture — SphereReflectionCapture            #
    # ------------------------------------------------------------------ #
    _SPHERE_CAP_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("SphereReflectionCapture", name, loc, rot,
      ["influence_radius", "brightness"])))
'''

    @mcp.tool()
    def spawn_sphere_reflection_capture(ctx, name: str = "MCP_B_SphereReflCap",
                                        location: list = None, rotation: list = None,
                                        influence_radius: float = None,
                                        brightness: float = None) -> str:
        """Spawn a SphereReflectionCapture (captures a spherical reflection probe) into the level.

        name:             display label (default 'MCP_B_SphereReflCap').
        location:         [x, y, z] world position (default [0,0,0]).
        rotation:         [pitch, yaw, roll] degrees.
        influence_radius: optional SphereReflectionCaptureComponent influence_radius in cm
                          (default ~3000; read back in 'applied.influence_radius').
        brightness:       optional reflection brightness multiplier (default 1.0).

        Note: the probe must be re-captured ('Build > Reflection Captures' or moving it) to
        reflect scene changes; spawning places the actor with its settings applied.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation,
                  "influence_radius": influence_radius, "brightness": brightness}
        try:
            return json.dumps(_exec(_SPHERE_CAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_box_reflection_capture — BoxReflectionCapture                  #
    # ------------------------------------------------------------------ #
    _BOX_CAP_BODY = _ENV_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
print("@@UMCP@@" + json.dumps(_spawn_env("BoxReflectionCapture", name, loc, rot,
      ["brightness"])))
'''

    @mcp.tool()
    def spawn_box_reflection_capture(ctx, name: str = "MCP_B_BoxReflCap", location: list = None,
                                     rotation: list = None, brightness: float = None) -> str:
        """Spawn a BoxReflectionCapture (captures a box-shaped reflection probe) into the level.

        name:       display label (default 'MCP_B_BoxReflCap').
        location:   [x, y, z] world position (default [0,0,0]).
        rotation:   [pitch, yaw, roll] degrees.
        brightness: optional BoxReflectionCaptureComponent brightness multiplier (default 1.0;
                    read back in 'applied.brightness'). Box extent follows the actor scale.

        Note: the probe must be re-captured to reflect scene changes.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation, "brightness": brightness}
        try:
            return json.dumps(_exec(_BOX_CAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
