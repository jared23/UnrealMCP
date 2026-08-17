"""UserTools :: Editor / Spawn Extras  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py / editor_spawn.py / editor_lights.py conventions VERBATIM:
base64-injected PARAMS, the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the
session-aware per-agent undo ledger, and the _COERCE_HELPERS block.

Reversible spawners for common actor types NOT covered elsewhere (editor_spawn covers
duplicate/from-class/batch; editor_lights covers the 4 lights):
  - spawn_camera_actor       -> CineCameraActor (falls back to CameraActor)
  - spawn_trigger_box        -> TriggerBox (box collision sized by box_extent)
  - spawn_text_render_actor  -> TextRenderActor showing `text`
  - spawn_decal_actor        -> DecalActor (optional decal material + size)
  - spawn_empty_actor        -> bare Actor with a SceneComponent root (grouping/attach root)
  - spawn_static_mesh_actor  -> StaticMeshActor with a given mesh asset assigned

Write safety (agent-scoped undo): every spawner runs inside an `unreal.ScopedEditorTransaction`
AND records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - every actor CREATED -> {"op": "spawn_actor", "actor_name": <unique>, "label": <label>}
    editor_level.py's unified `undo` already deletes spawn_actor actors (destroying the actor
    reverts every component property configured here too), so NO `undo` tool is defined here
    and NO new undo op/branch is needed.

Actor/component API notes (UE 5.8.1; component structure probed live):
  - CineCameraActor / CameraActor: spawn cleanly, no extra config (transform + label only).
  - TriggerBox: root/collision component at actor.get_editor_property("collision_component")
    is a BoxComponent; size via comp.set_box_extent(Vector, update_overlaps) (falls back to
    set_editor_property("box_extent", Vector)); read back get_editor_property("box_extent").
  - TextRenderActor: component at actor.get_editor_property("text_render") (TextRenderComponent);
    set text via comp.set_text(str/Text), color via comp.set_text_render_color(unreal.Color)
    (FColor 0-255 RGBA), size via comp.set_world_size(float).
  - DecalActor: root DecalComponent at actor.get_editor_property("decal") (probed live as
    "NewDecalComponent"); material via actor.set_decal_material(MaterialInterface); size via
    comp.set_editor_property("decal_size", Vector).
  - Actor (empty): spawns WITH a SceneComponent root ("DefaultSceneRoot") in 5.8 -> a bare Actor
    works as a grouping/attach root; no StaticMeshActor fallback needed.
  - StaticMeshActor: mesh via comp = get_editor_property("static_mesh_component");
    comp.set_static_mesh(load_asset(mesh_path)).
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

    # Spawn-extras-specific helpers. No ''' / no backslashes.
    # _spawn(cls_name, loc, rot): resolve class + spawn via EditorActorSubsystem (no modal).
    # _try_get_prop / _vec3: small readback helpers used to build the 'applied' proof dict.
    _EXTRA_HELPERS = r'''
def _resolve_class(cname):
    cls = getattr(unreal, cname, None)
    if cls is None:
        try:
            cls = unreal.load_class(None, cname)
        except Exception:
            cls = None
    return cls
def _spawn(cls, loc, rot):
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return eas.spawn_actor_from_class(cls, location, rotation)
def _get_prop(obj, name):
    try:
        return obj.get_editor_property(name)
    except Exception:
        return None
def _vec3(v):
    if v is None:
        return None
    try:
        return [v.x, v.y, v.z]
    except Exception:
        return None
def _root_of(actor):
    try:
        return actor.get_editor_property("root_component")
    except Exception:
        return None
'''

    _EXTRA_PREFIX = _COERCE_HELPERS + _EXTRA_HELPERS

    # ------------------------------------------------------------------ #
    # spawn_camera_actor — CineCameraActor (fallback CameraActor)          #
    # ------------------------------------------------------------------ #
    _CAMERA_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
prefer = PARAMS.get("camera_type") or "CineCameraActor"
order = [prefer, "CameraActor", "CineCameraActor"]
seen = []
cls = None
chosen = None
for cn in order:
    if cn in seen:
        continue
    seen.append(cn)
    c = _resolve_class(cn)
    if c is not None:
        cls = c; chosen = cn; break
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no camera class available (tried %s)" % seen}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_camera_actor"):
        actor = _spawn(cls, loc, rot)
        if actor:
            actor.set_actor_label(name)
            uniq = actor.get_name()
            root = _root_of(actor)
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location(); r = actor.get_actor_rotation()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(), "requested_class": chosen,
                      "root_component": (root.get_class().get_name() if root else None),
                      "location": [l.x, l.y, l.z], "rotation": [r.pitch, r.yaw, r.roll],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_camera_actor(ctx, name: str, location: list = None, rotation: list = None,
                           camera_type: str = "CineCameraActor") -> str:
        """Spawn a camera actor into the active level (CineCameraActor by default, falling
        back to CameraActor if the requested class is unavailable).

        name:        display label for the new camera (required).
        location:    [x, y, z] world position (default [0,0,0]).
        rotation:    [pitch, yaw, roll] degrees — the camera looks down its +X.
        camera_type: 'CineCameraActor' (default) or 'CameraActor'.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation, "camera_type": camera_type}
        try:
            return json.dumps(_exec(_CAMERA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_trigger_box — TriggerBox with box collision sized by extent    #
    # ------------------------------------------------------------------ #
    _TRIGGER_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
extent = PARAMS.get("box_extent")
cls = _resolve_class("TriggerBox")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "TriggerBox class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_trigger_box"):
        actor = _spawn(cls, loc, rot)
        if actor:
            actor.set_actor_label(name)
            comp = _get_prop(actor, "collision_component")
            if comp is None:
                comp = _root_of(actor)
            applied = {}
            if comp is not None and extent:
                ev = unreal.Vector(float(extent[0]), float(extent[1]), float(extent[2]))
                did = False
                try:
                    comp.set_box_extent(ev, True); did = True
                except Exception:
                    did = False
                if not did:
                    try:
                        comp.set_editor_property("box_extent", ev)
                    except Exception:
                        pass
            if comp is not None:
                applied["box_extent"] = _vec3(_get_prop(comp, "box_extent"))
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "comp_class": (comp.get_class().get_name() if comp is not None else None),
                      "location": [l.x, l.y, l.z], "applied": applied,
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_trigger_box(ctx, name: str, location: list = None, rotation: list = None,
                          box_extent: list = None) -> str:
        """Spawn a TriggerBox (box collision volume) into the active level.

        name:       display label for the new trigger (required).
        location:   [x, y, z] world position (default [0,0,0]).
        rotation:   [pitch, yaw, roll] degrees.
        box_extent: [x, y, z] half-extents (cm) of the box collision; applied via the box
                    component's set_box_extent (read back in 'applied.box_extent').

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation, "box_extent": box_extent}
        try:
            return json.dumps(_exec(_TRIGGER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_text_render_actor — TextRenderActor showing `text`             #
    # ------------------------------------------------------------------ #
    _TEXT_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
text = PARAMS.get("text") or ""
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
color = PARAMS.get("color")
world_size = PARAMS.get("world_size")
cls = _resolve_class("TextRenderActor")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "TextRenderActor class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_text_render_actor"):
        actor = _spawn(cls, loc, rot)
        if actor:
            actor.set_actor_label(name)
            comp = _get_prop(actor, "text_render")
            if comp is None:
                comp = _root_of(actor)
            applied = {}
            if comp is not None:
                try:
                    comp.set_text(unreal.Text.cast(text))
                except Exception:
                    try:
                        comp.set_text(text)
                    except Exception:
                        try:
                            comp.set_editor_property("text", text)
                        except Exception:
                            pass
                applied["text"] = str(_get_prop(comp, "text"))
                if color:
                    aa = int(color[3]) if len(color) > 3 else 255
                    # NB: unreal.Color POSITIONAL args are B,G,R,A ordered -> use keywords so
                    # color=[r,g,b] maps to the real red/green/blue channels (verified live).
                    col = unreal.Color(r=int(color[0]), g=int(color[1]), b=int(color[2]), a=aa)
                    try:
                        comp.set_text_render_color(col)
                    except Exception:
                        try:
                            comp.set_editor_property("text_render_color", col)
                        except Exception:
                            pass
                    trc = _get_prop(comp, "text_render_color")
                    if trc is not None:
                        applied["text_render_color"] = [trc.r, trc.g, trc.b, trc.a]
                if world_size is not None:
                    try:
                        comp.set_world_size(float(world_size))
                    except Exception:
                        try:
                            comp.set_editor_property("world_size", float(world_size))
                        except Exception:
                            pass
                    applied["world_size"] = _get_prop(comp, "world_size")
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "comp_class": (comp.get_class().get_name() if comp is not None else None),
                      "location": [l.x, l.y, l.z], "applied": applied,
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_text_render_actor(ctx, name: str, text: str, location: list = None,
                                rotation: list = None, color: list = None,
                                world_size: float = None) -> str:
        """Spawn a TextRenderActor that displays `text` in the level (3D world text).

        name:       display label for the new actor (required).
        text:       the string to display (required).
        location:   [x, y, z] world position (default [0,0,0]).
        rotation:   [pitch, yaw, roll] degrees — text faces down the actor's +X.
        color:      FColor [r, g, b] or [r, g, b, a], each 0-255 (default alpha 255);
                    applied via set_text_render_color (read back in 'applied.text_render_color').
        world_size: text height in world units (cm).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it (which
        reverts the text/color/size configured here with it)."""
        params = {"name": name, "text": text, "location": location, "rotation": rotation,
                  "color": color, "world_size": world_size}
        try:
            return json.dumps(_exec(_TEXT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_decal_actor — DecalActor with optional decal material          #
    # ------------------------------------------------------------------ #
    _DECAL_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
mat_path = PARAMS.get("material_path")
decal_size = PARAMS.get("decal_size")
cls = _resolve_class("DecalActor")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "DecalActor class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_decal_actor"):
        actor = _spawn(cls, loc, rot)
        if actor:
            actor.set_actor_label(name)
            comp = _get_prop(actor, "decal")
            if comp is None:
                comp = _root_of(actor)
            applied = {}
            mat_err = None
            if mat_path:
                mat = None
                try:
                    mat = unreal.EditorAssetLibrary.load_asset(mat_path)
                except Exception:
                    mat = None
                if mat is None:
                    mat_err = "material not found: %s" % mat_path
                elif not isinstance(mat, unreal.MaterialInterface):
                    mat_err = "asset is not a MaterialInterface: %s" % mat_path
                else:
                    did = False
                    try:
                        actor.set_decal_material(mat); did = True
                    except Exception:
                        did = False
                    if not did and comp is not None:
                        try:
                            comp.set_decal_material(mat)
                        except Exception:
                            pass
            cur_mat = None
            try:
                cur_mat = actor.get_decal_material()
            except Exception:
                cur_mat = _get_prop(comp, "decal_material") if comp is not None else None
            applied["decal_material"] = (cur_mat.get_path_name() if cur_mat is not None else None)
            if comp is not None and decal_size:
                sv = unreal.Vector(float(decal_size[0]), float(decal_size[1]), float(decal_size[2]))
                try:
                    comp.set_editor_property("decal_size", sv)
                except Exception:
                    pass
            if comp is not None:
                applied["decal_size"] = _vec3(_get_prop(comp, "decal_size"))
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "comp_class": (comp.get_class().get_name() if comp is not None else None),
                      "location": [l.x, l.y, l.z], "applied": applied,
                      "material_error": mat_err, "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_decal_actor(ctx, name: str, material_path: str = None, location: list = None,
                          rotation: list = None, decal_size: list = None) -> str:
        """Spawn a DecalActor (projects a material onto surfaces) into the active level.

        name:          display label for the new decal (required).
        material_path: optional decal MaterialInterface asset path to project
                       (e.g. a material set to Deferred Decal domain). Omit for a bare decal.
        location:      [x, y, z] world position (default [0,0,0]).
        rotation:      [pitch, yaw, roll] degrees — the decal projects down the actor's -X.
        decal_size:    [x, y, z] decal box size in cm (read back in 'applied.decal_size').

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "material_path": material_path, "location": location,
                  "rotation": rotation, "decal_size": decal_size}
        try:
            return json.dumps(_exec(_DECAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_empty_actor — bare Actor with a SceneComponent root            #
    # ------------------------------------------------------------------ #
    _EMPTY_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
cls = _resolve_class("Actor")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "Actor class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_empty_actor"):
        actor = _spawn(cls, loc, rot)
        fallback = False
        if not actor:
            # Defensive: if a bare Actor won't spawn cleanly, use an empty StaticMeshActor
            smc = _resolve_class("StaticMeshActor")
            if smc is not None:
                actor = _spawn(smc, loc, rot); fallback = True
        if actor:
            actor.set_actor_label(name)
            root = _root_of(actor)
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "root_component": (root.get_class().get_name() if root else None),
                      "used_static_mesh_fallback": fallback,
                      "location": [l.x, l.y, l.z], "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_empty_actor(ctx, name: str, location: list = None, rotation: list = None) -> str:
        """Spawn a bare, empty Actor (a SceneComponent root, no visible mesh) — a good
        grouping/attach root for organizing other actors under one transform.

        name:     display label for the new actor (required).
        location: [x, y, z] world position (default [0,0,0]).
        rotation: [pitch, yaw, roll] degrees.

        In UE 5.8 a bare Actor spawns with a 'DefaultSceneRoot' SceneComponent, so it works
        directly as an attach root ('root_component' in the response confirms; the response
        also reports 'used_static_mesh_fallback' = false unless a bare Actor could not spawn).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "rotation": rotation}
        try:
            return json.dumps(_exec(_EMPTY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_static_mesh_actor — StaticMeshActor with a given mesh asset    #
    # ------------------------------------------------------------------ #
    _STATIC_MESH_BODY = _EXTRA_PREFIX + r'''
name = PARAMS["name"]
mesh_path = PARAMS.get("mesh_path")
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
scale = PARAMS.get("scale")
if not mesh_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "mesh_path is required"}))
else:
    mesh = None
    try:
        mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
    except Exception:
        mesh = None
    if mesh is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "static mesh not found: %s" % mesh_path}))
    elif not isinstance(mesh, unreal.StaticMesh):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a StaticMesh: %s" % mesh_path}))
    else:
        cls = _resolve_class("StaticMeshActor")
        result = {"status": "error", "message": "spawn failed"}
        with unreal.ScopedEditorTransaction("MCP spawn_static_mesh_actor"):
            actor = _spawn(cls, loc, rot)
            if actor:
                actor.set_actor_label(name)
                if scale:
                    actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
                comp = _get_prop(actor, "static_mesh_component")
                applied = {}
                if comp is not None:
                    comp.set_static_mesh(mesh)
                    cur = _get_prop(comp, "static_mesh")
                    applied["static_mesh"] = (cur.get_path_name() if cur is not None else None)
                uniq = actor.get_name()
                _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
                l = actor.get_actor_location(); s = actor.get_actor_scale3d()
                result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                          "class": actor.get_class().get_name(),
                          "comp_class": (comp.get_class().get_name() if comp is not None else None),
                          "location": [l.x, l.y, l.z], "scale": [s.x, s.y, s.z],
                          "applied": applied, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_static_mesh_actor(ctx, name: str, mesh_path: str, location: list = None,
                                rotation: list = None, scale: list = None) -> str:
        """Spawn a StaticMeshActor with a specific static-mesh asset assigned.

        Distinct from editor_spawn's class-based spawn: this takes a mesh ASSET PATH and
        assigns it to the actor's StaticMeshComponent.

        name:      display label for the new actor (required).
        mesh_path: StaticMesh asset path to assign (required),
                   e.g. '/Engine/BasicShapes/Sphere.Sphere'.
        location:  [x, y, z] world position (default [0,0,0]).
        rotation:  [pitch, yaw, roll] degrees.
        scale:     [x, y, z] scale (optional).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "mesh_path": mesh_path, "location": location,
                  "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_STATIC_MESH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
