"""UserTools :: Editor / Volumes & Navigation spawners  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py / spawn_extras.py conventions VERBATIM: base64-injected PARAMS,
the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block.

Reversible spawners for VOLUME / NAVIGATION actors (the counterpart to navigation_read.py:
placing a NavMeshBoundsVolume makes list_nav_actors / get_navmesh_info return real data).
These are distinct from spawn_extras.py (camera/trigger_box/text/decal/empty/static_mesh):

  - spawn_nav_bounds_volume    -> NavMeshBoundsVolume (defines the region a navmesh builds for;
                                  placing it auto-triggers a benign nav build; undo=delete clears it)
  - spawn_nav_modifier_volume  -> NavModifierVolume (marks an area as a nav area type / non-navigable)
  - spawn_blocking_volume      -> BlockingVolume (invisible collision blocker)
  - spawn_trigger_sphere       -> TriggerSphere (sphere-collision trigger)
  - spawn_trigger_capsule      -> TriggerCapsule (capsule-collision trigger)
  - spawn_post_process_volume  -> PostProcessVolume (unbound/global or bounded; priority) —
                                  complements rendering_read.py's post-process reads.

Write safety (agent-scoped undo): every spawner runs inside an `unreal.ScopedEditorTransaction`
AND records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - every actor CREATED -> {"op": "spawn_actor", "actor_name": <unique>, "label": <label>}
    editor_level.py's unified `undo` already deletes spawn_actor actors (destroying the actor
    reverts every property/scale configured here too, and deleting a NavMeshBoundsVolume clears
    the navmesh it triggered), so NO `undo` tool is defined here and NO new op/branch is needed.

Actor/component API notes (UE 5.8.1; probed live against TestMCPSetup / Lvl_ThirdPerson):
  - NavMeshBoundsVolume / NavModifierVolume / BlockingVolume / PostProcessVolume are AVolume
    subclasses: root == brush_component == a BrushComponent ("BrushComponent0"); the default
    brush is a 200x200x200 cube (get_actor_bounds extent [100,100,100]). BrushComponent exposes
    NO set_box_extent in Python, so box size is set via actor scale: set_actor_scale3d(extent/100)
    (default half-extent 100 -> scale S gives half-extent 100*S). The realized size is read back
    from get_actor_bounds and reported as 'result_bounds_extent'.
  - TriggerSphere: root == collision_component == SphereComponent ("CollisionComp"); radius via
    comp.set_sphere_radius(radius, update_overlaps) (default 40); read back 'sphere_radius'.
  - TriggerCapsule: root == collision_component == CapsuleComponent ("CollisionComp"); size via
    comp.set_capsule_size(radius, half_height, update_overlaps) (default 40 / 80); read back
    'capsule_radius' / 'capsule_half_height'.
  - PostProcessVolume: 'unbound' (bool; True = global/affects whole world) and 'priority' (float)
    are plain UPROPERTYs set via get/set_editor_property; when bounded, box size via actor scale.

No factories, no *CreationParameters, no create_asset, no modal-popping APIs — every actor is
placed with EditorActorSubsystem.spawn_actor_from_class. Prefix everything MCP_B_.
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

    # Volumes/nav-specific helpers. No ''' / no backslashes.
    # _resolve_class/_spawn: resolve class + spawn via EditorActorSubsystem (no modal).
    # _apply_box_extent: size an AVolume brush via actor scale (BrushComponent has no
    #   set_box_extent); default brush half-extent is 100 uu -> scale = extent/100. Reads
    #   back the realized extent from get_actor_bounds.
    _VOL_HELPERS = r'''
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
def _bounds_extent(actor):
    try:
        b = actor.get_actor_bounds(False)
        e = b[1]
        return [round(e.x, 2), round(e.y, 2), round(e.z, 2)]
    except Exception:
        return None
def _apply_box_extent(actor, box_extent):
    # AVolume brush default half-extent is 100 uu; BrushComponent exposes no set_box_extent,
    # so size the brush via actor scale (scale = target_extent / 100). Read realized extent back.
    if not box_extent:
        return {"box_extent_method": "default (no box_extent requested; 200x200x200 brush)",
                "result_bounds_extent": _bounds_extent(actor)}
    sc = unreal.Vector(float(box_extent[0]) / 100.0, float(box_extent[1]) / 100.0, float(box_extent[2]) / 100.0)
    actor.set_actor_scale3d(sc)
    return {"box_extent_method": "set_actor_scale3d (BrushComponent has no set_box_extent; scale=extent/100)",
            "requested_box_extent": [float(box_extent[0]), float(box_extent[1]), float(box_extent[2])],
            "applied_scale": [sc.x, sc.y, sc.z],
            "result_bounds_extent": _bounds_extent(actor)}
'''

    _VOL_PREFIX = _COERCE_HELPERS + _VOL_HELPERS

    # Common AVolume spawner body: resolves PARAMS["actor_class"], spawns, scales to box_extent,
    # ledgers spawn_actor. Used by nav-bounds / nav-modifier / blocking (identical shape).
    def _volume_body(class_name, tx_name):
        return _VOL_PREFIX + (r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
box_extent = PARAMS.get("box_extent")
cls = _resolve_class(%r)
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "%s class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction(%r):
        actor = _spawn(cls, loc, [0, 0, 0])
        if actor:
            actor.set_actor_label(name)
            applied = _apply_box_extent(actor, box_extent)
            root = _get_prop(actor, "root_component")
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "root_component": (root.get_class().get_name() if root else None),
                      "location": [l.x, l.y, l.z], "applied": applied,
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
''' % (class_name, class_name, tx_name))

    # ------------------------------------------------------------------ #
    # spawn_nav_bounds_volume — NavMeshBoundsVolume (nav region)           #
    # ------------------------------------------------------------------ #
    _NAV_BOUNDS_BODY = _volume_body("NavMeshBoundsVolume", "MCP spawn_nav_bounds_volume")

    @mcp.tool()
    def spawn_nav_bounds_volume(ctx, name: str, location: list = None,
                                box_extent: list = None) -> str:
        """Spawn a NavMeshBoundsVolume into the active level — it defines the world region a
        navmesh is generated for (this is what makes navigation_read's list_nav_actors /
        get_navmesh_info return real data).

        name:       display label for the new volume (required).
        location:   [x, y, z] world position (default [0,0,0]).
        box_extent: [x, y, z] half-extents (cm) of the navigable box. The AVolume BrushComponent
                    has no set_box_extent, so size is applied via actor scale (scale = extent/100,
                    default brush half-extent 100); the realized size is read back in
                    'applied.result_bounds_extent'. Omit for the default 200x200x200 brush.

        Placing this auto-triggers a (benign) navigation build for the covered region; the
        ledgered `undo` deletes the volume, which clears that navmesh again.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "box_extent": box_extent}
        try:
            return json.dumps(_exec(_NAV_BOUNDS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_nav_modifier_volume — NavModifierVolume (area modifier)        #
    # ------------------------------------------------------------------ #
    _NAV_MODIFIER_BODY = _volume_body("NavModifierVolume", "MCP spawn_nav_modifier_volume")

    @mcp.tool()
    def spawn_nav_modifier_volume(ctx, name: str, location: list = None,
                                  box_extent: list = None) -> str:
        """Spawn a NavModifierVolume into the active level — it marks the area it covers with a
        nav area class (e.g. NavArea_Null to make it non-navigable, or a cost modifier).

        name:       display label for the new volume (required).
        location:   [x, y, z] world position (default [0,0,0]).
        box_extent: [x, y, z] half-extents (cm); applied via actor scale (BrushComponent has no
                    set_box_extent; scale = extent/100). Realized size read back in
                    'applied.result_bounds_extent'. Omit for the default 200x200x200 brush.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "box_extent": box_extent}
        try:
            return json.dumps(_exec(_NAV_MODIFIER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_blocking_volume — BlockingVolume (invisible collision blocker) #
    # ------------------------------------------------------------------ #
    _BLOCKING_BODY = _volume_body("BlockingVolume", "MCP spawn_blocking_volume")

    @mcp.tool()
    def spawn_blocking_volume(ctx, name: str, location: list = None,
                              box_extent: list = None) -> str:
        """Spawn a BlockingVolume into the active level — an invisible box that blocks collision
        (pawns/physics) without a visible mesh.

        name:       display label for the new volume (required).
        location:   [x, y, z] world position (default [0,0,0]).
        box_extent: [x, y, z] half-extents (cm); applied via actor scale (BrushComponent has no
                    set_box_extent; scale = extent/100). Realized size read back in
                    'applied.result_bounds_extent'. Omit for the default 200x200x200 brush.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "box_extent": box_extent}
        try:
            return json.dumps(_exec(_BLOCKING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_trigger_sphere — TriggerSphere (sphere-collision trigger)      #
    # ------------------------------------------------------------------ #
    _TRIGGER_SPHERE_BODY = _VOL_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
radius = PARAMS.get("radius")
cls = _resolve_class("TriggerSphere")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "TriggerSphere class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_trigger_sphere"):
        actor = _spawn(cls, loc, [0, 0, 0])
        if actor:
            actor.set_actor_label(name)
            comp = _get_prop(actor, "collision_component")
            if comp is None:
                comp = _get_prop(actor, "root_component")
            applied = {}
            if comp is not None and radius is not None:
                try:
                    comp.set_sphere_radius(float(radius), True)
                except Exception:
                    try:
                        comp.set_editor_property("sphere_radius", float(radius))
                    except Exception:
                        pass
            if comp is not None:
                applied["sphere_radius"] = _get_prop(comp, "sphere_radius")
            applied["bounds_extent"] = _bounds_extent(actor)
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
    def spawn_trigger_sphere(ctx, name: str, location: list = None, radius: float = None) -> str:
        """Spawn a TriggerSphere (sphere-collision trigger volume) into the active level.

        name:     display label for the new trigger (required).
        location: [x, y, z] world position (default [0,0,0]).
        radius:   sphere collision radius (cm); applied via the SphereComponent's
                  set_sphere_radius (default 40). Read back in 'applied.sphere_radius'.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "radius": radius}
        try:
            return json.dumps(_exec(_TRIGGER_SPHERE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_trigger_capsule — TriggerCapsule (capsule-collision trigger)   #
    # ------------------------------------------------------------------ #
    _TRIGGER_CAPSULE_BODY = _VOL_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
radius = PARAMS.get("radius")
half_height = PARAMS.get("half_height")
cls = _resolve_class("TriggerCapsule")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "TriggerCapsule class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_trigger_capsule"):
        actor = _spawn(cls, loc, [0, 0, 0])
        if actor:
            actor.set_actor_label(name)
            comp = _get_prop(actor, "collision_component")
            if comp is None:
                comp = _get_prop(actor, "root_component")
            applied = {}
            if comp is not None and (radius is not None or half_height is not None):
                cur_r = _get_prop(comp, "capsule_radius")
                cur_hh = _get_prop(comp, "capsule_half_height")
                nr = float(radius) if radius is not None else float(cur_r)
                nhh = float(half_height) if half_height is not None else float(cur_hh)
                try:
                    comp.set_capsule_size(nr, nhh, True)
                except Exception:
                    try:
                        comp.set_editor_property("capsule_radius", nr)
                        comp.set_editor_property("capsule_half_height", nhh)
                    except Exception:
                        pass
            if comp is not None:
                applied["capsule_radius"] = _get_prop(comp, "capsule_radius")
                applied["capsule_half_height"] = _get_prop(comp, "capsule_half_height")
            applied["bounds_extent"] = _bounds_extent(actor)
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
    def spawn_trigger_capsule(ctx, name: str, location: list = None, radius: float = None,
                              half_height: float = None) -> str:
        """Spawn a TriggerCapsule (capsule-collision trigger volume) into the active level.

        name:        display label for the new trigger (required).
        location:    [x, y, z] world position (default [0,0,0]).
        radius:      capsule collision radius (cm) — default 40.
        half_height: capsule half-height (cm) — default 80.
                     Both applied together via the CapsuleComponent's set_capsule_size;
                     read back in 'applied.capsule_radius' / 'applied.capsule_half_height'.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "radius": radius, "half_height": half_height}
        try:
            return json.dumps(_exec(_TRIGGER_CAPSULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_post_process_volume — PostProcessVolume (unbound/priority)     #
    # ------------------------------------------------------------------ #
    _PPV_BODY = _VOL_PREFIX + r'''
name = PARAMS["name"]
loc = PARAMS.get("location") or [0, 0, 0]
box_extent = PARAMS.get("box_extent")
unbound = PARAMS.get("unbound")
priority = PARAMS.get("priority")
cls = _resolve_class("PostProcessVolume")
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PostProcessVolume class unavailable"}))
else:
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_post_process_volume"):
        actor = _spawn(cls, loc, [0, 0, 0])
        if actor:
            actor.set_actor_label(name)
            applied = _apply_box_extent(actor, box_extent)
            if unbound is not None:
                try:
                    actor.set_editor_property("unbound", bool(unbound))
                except Exception:
                    pass
            applied["unbound"] = _get_prop(actor, "unbound")
            if priority is not None:
                try:
                    actor.set_editor_property("priority", float(priority))
                except Exception:
                    pass
            applied["priority"] = _get_prop(actor, "priority")
            root = _get_prop(actor, "root_component")
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            l = actor.get_actor_location()
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "root_component": (root.get_class().get_name() if root else None),
                      "location": [l.x, l.y, l.z], "applied": applied,
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_post_process_volume(ctx, name: str, location: list = None, box_extent: list = None,
                                  unbound: bool = None, priority: float = None) -> str:
        """Spawn a PostProcessVolume into the active level (complements rendering_read's
        post-process reads).

        name:       display label for the new volume (required).
        location:   [x, y, z] world position (default [0,0,0]).
        box_extent: [x, y, z] half-extents (cm) of the affected box when NOT unbound; applied via
                    actor scale (BrushComponent has no set_box_extent; scale = extent/100). Realized
                    size read back in 'applied.result_bounds_extent'.
        unbound:    True = global (affects the whole world, ignoring bounds); False = bounded by the
                    box. Read back in 'applied.unbound'.
        priority:   float priority; when volumes overlap, the higher priority wins. Read back in
                    'applied.priority'.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"name": name, "location": location, "box_extent": box_extent,
                  "unbound": unbound, "priority": priority}
        try:
            return json.dumps(_exec(_PPV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
