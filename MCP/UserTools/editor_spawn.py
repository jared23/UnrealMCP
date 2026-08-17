"""UserTools :: Editor / Spawn + Batch  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions verbatim: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block.

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records an inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session].
This module records ONLY the two op names that editor_level.py's unified `undo` already
inverts, so no `undo` tool is defined here:
  - every actor CREATED  -> {"op": "spawn_actor",  "actor_name": <unique>, "label": <label>}
                            (undo deletes that exact actor)
  - every actor SOFT-DELETED -> {"op": "delete_actor", "actor_name": <name>,
                            "prior_folder": <str>, "was_hidden": <bool>}
                            (undo restores folder + visibility)

Soft-delete convention matches editor_level.py: delete_actors_batch does NOT destroy; it
moves each actor to a hidden '_MCP_Trash' outliner folder and hides it in the editor.

Implemented:
  - duplicate_actor        (write; ledgered spawn_actor) — full copy via EditorActorSubsystem
  - spawn_actor_from_class (write; ledgered spawn_actor) — SCALAR coord params per spec
  - spawn_actors_batch     (write; one ledger spawn_actor PER created actor; single round trip)
  - delete_actors_batch    (write; one ledger delete_actor PER actor; SOFT delete)
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

    # ------------------------------------------------------------------ #
    # duplicate_actor — full copy of an existing actor (ledgered spawn)   #
    # ------------------------------------------------------------------ #
    _DUPLICATE_BODY = _COERCE_HELPERS + r'''
src = _resolve_actor(PARAMS["source_name"])
if src is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source actor not found: %s" % PARAMS["source_name"]}))
else:
    off = PARAMS.get("offset") or [0, 0, 0]
    new_label = PARAMS.get("name")
    loc = PARAMS.get("location"); rot = PARAMS.get("rotation"); scale = PARAMS.get("scale")
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "duplicate failed"}
    with unreal.ScopedEditorTransaction("MCP duplicate_actor"):
        dup = eas.duplicate_actor(src, None, unreal.Vector(float(off[0]), float(off[1]), float(off[2])))
        if dup:
            if new_label:
                dup.set_actor_label(new_label)
            if loc:
                dup.set_actor_location(unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])), False, False)
            if rot:
                dup.set_actor_rotation(unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])), False)
            if scale:
                dup.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
            uniq = dup.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": dup.get_actor_label()})
            dl = dup.get_actor_location()
            result = {"status": "success", "name": uniq, "label": dup.get_actor_label(),
                      "class": dup.get_class().get_name(), "source": src.get_actor_label(),
                      "location": [dl.x, dl.y, dl.z], "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def duplicate_actor(ctx, source_name: str, name: str = None,
                        location: list = None, rotation: list = None,
                        scale: list = None, offset: list = None) -> str:
        """Duplicate an existing placed actor (full copy: class, static mesh, materials,
        transform) via the editor's native duplicate, then optionally relabel/retransform.

        source_name: label (preferred) or unique internal name of the actor to copy.
        offset:      [x, y, z] spawn offset applied relative to the source (default [0,0,0]).
        location:    [x, y, z] absolute world position override (applied after offset).
        rotation:    [pitch, yaw, roll] absolute rotation override (degrees).
        scale:       [x, y, z] absolute scale override.
        name:        display label for the copy (default: engine-assigned).

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes the copy."""
        params = {"source_name": source_name, "name": name, "location": location,
                  "rotation": rotation, "scale": scale, "offset": offset}
        try:
            return json.dumps(_exec(_DUPLICATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_actor_from_class — spawn from class name, SCALAR coords        #
    # ------------------------------------------------------------------ #
    _SPAWN_FROM_CLASS_BODY = _COERCE_HELPERS + r'''
class_name = PARAMS["class_name"]
lx = PARAMS.get("location_x"); ly = PARAMS.get("location_y"); lz = PARAMS.get("location_z")
ry = PARAMS.get("rotation_yaw"); rp = PARAMS.get("rotation_pitch"); rr = PARAMS.get("rotation_roll")
name = PARAMS.get("name")
cls = getattr(unreal, class_name, None)
if cls is None:
    try: cls = unreal.load_class(None, class_name)
    except Exception: cls = None
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown class_name: %s" % class_name}))
else:
    location = unreal.Vector(float(lx or 0.0), float(ly or 0.0), float(lz or 0.0))
    rotation = unreal.Rotator(pitch=float(rp or 0.0), yaw=float(ry or 0.0), roll=float(rr or 0.0))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_actor_from_class"):
        actor = eas.spawn_actor_from_class(cls, location, rotation)
        if actor:
            if name:
                actor.set_actor_label(name)
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "location": [location.x, location.y, location.z],
                      "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_actor_from_class(ctx, class_name: str,
                               location_x: float = None, location_y: float = None, location_z: float = None,
                               rotation_yaw: float = None, rotation_pitch: float = None, rotation_roll: float = None,
                               name: str = None) -> str:
        """Spawn an actor from a class name using SCALAR coordinate params (per editor.md;
        note this differs from spawn_actor which takes json [x,y,z] coords).

        class_name:  engine class name (e.g. 'PointLight', 'CameraActor', 'StaticMeshActor')
                     or a loadable class path.
        location_x/y/z:      world position components (default 0).
        rotation_yaw/pitch/roll: rotation components in degrees (default 0).
        name:        optional display label for the new actor.

        Ledgered write: recorded as spawn_actor so editor_level's `undo` deletes it."""
        params = {"class_name": class_name,
                  "location_x": location_x, "location_y": location_y, "location_z": location_z,
                  "rotation_yaw": rotation_yaw, "rotation_pitch": rotation_pitch, "rotation_roll": rotation_roll,
                  "name": name}
        try:
            return json.dumps(_exec(_SPAWN_FROM_CLASS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_actors_batch — many actors from an array, single round trip   #
    # ------------------------------------------------------------------ #
    _SPAWN_BATCH_BODY = _COERCE_HELPERS + r'''
items = PARAMS.get("actors") or []
d_mesh = PARAMS.get("mesh")
d_type = PARAMS.get("type") or "StaticMeshActor"
name_prefix = PARAMS.get("name_prefix")
folder = PARAMS.get("folder")
return_names = bool(PARAMS.get("return_names"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
created = []
errors = []
with unreal.ScopedEditorTransaction("MCP spawn_actors_batch"):
    for i, it in enumerate(items):
        it = it or {}
        atype = it.get("type") or d_type
        cls = getattr(unreal, atype, None)
        if cls is None:
            try: cls = unreal.load_class(None, atype)
            except Exception: cls = None
        if cls is None:
            errors.append({"index": i, "message": "unknown type: %s" % atype}); continue
        loc = it.get("location") or [0, 0, 0]
        rot = it.get("rotation") or [0, 0, 0]
        scale = it.get("scale")
        location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
        rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
        actor = eas.spawn_actor_from_class(cls, location, rotation)
        if not actor:
            errors.append({"index": i, "message": "spawn failed"}); continue
        label = it.get("name")
        if not label and name_prefix:
            label = "%s%d" % (name_prefix, i)
        if label:
            actor.set_actor_label(label)
        if scale:
            actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
        mesh_path = it.get("mesh")
        if mesh_path is None:
            mesh_path = d_mesh
        if mesh_path is None and atype == "StaticMeshActor":
            mesh_path = "/Engine/BasicShapes/Cube.Cube"
        if mesh_path:
            comp = actor.get_editor_property("static_mesh_component")
            mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
            if comp and mesh:
                comp.set_static_mesh(mesh)
        if folder:
            actor.set_folder_path(unreal.Name(folder))
        uniq = actor.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label()})
        created.append({"index": i, "name": uniq, "label": actor.get_actor_label(), "class": actor.get_class().get_name()})
res = {"status": "success", "created_count": len(created), "error_count": len(errors),
       "errors": errors, "ledger_depth": len(_ledger())}
if return_names:
    res["created"] = created
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def spawn_actors_batch(ctx, actors: list, mesh: str = None, type: str = None,
                           name_prefix: str = None, folder: str = None,
                           return_names: bool = False) -> str:
        """Spawn many actors from an array in a SINGLE round trip (one editor transaction).

        actors:      list of dicts, each optionally {mesh, location [x,y,z],
                     rotation [pitch,yaw,roll], scale [x,y,z], name, type}.
        mesh:        default static-mesh asset path for items without their own 'mesh'
                     (StaticMeshActor items with no mesh fall back to /Engine/BasicShapes/Cube.Cube).
        type:        default actor class for items without their own 'type' (default StaticMeshActor).
        name_prefix: when an item has no 'name', label it '<name_prefix><index>'.
        folder:      outliner folder to place every spawned actor in.
        return_names: include the per-actor created list (name/label/class) in the response.

        Ledgered write: pushes ONE spawn_actor entry per created actor, so editor_level's
        `undo` deletes each. Per-item failures are collected in 'errors', not fatal."""
        params = {"actors": actors, "mesh": mesh, "type": type,
                  "name_prefix": name_prefix, "folder": folder, "return_names": return_names}
        try:
            return json.dumps(_exec(_SPAWN_BATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_actors_batch — SOFT delete many (hidden _MCP_Trash folder)   #
    # ------------------------------------------------------------------ #
    _DELETE_BATCH_BODY = _COERCE_HELPERS + r'''
name_prefix = PARAMS.get("name_prefix")
names = PARAMS.get("names")
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
targets = []
if names:
    want = set(names)
    for a in (eas.get_all_level_actors() or []):
        if a and (a.get_actor_label() in want or a.get_name() in want):
            targets.append(a)
elif name_prefix:
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_actor_label().startswith(name_prefix):
            targets.append(a)
deleted = []
skipped = 0
with unreal.ScopedEditorTransaction("MCP delete_actors_batch (soft)"):
    for a in targets:
        prior_folder = str(a.get_folder_path())
        if prior_folder == "_MCP_Trash":
            skipped += 1; continue
        was_hidden = a.is_temporarily_hidden_in_editor()
        a.set_folder_path(unreal.Name("_MCP_Trash"))
        a.set_is_temporarily_hidden_in_editor(True)
        _ledger().append({"op": "delete_actor", "actor_name": a.get_name(),
                          "prior_folder": prior_folder, "was_hidden": was_hidden})
        deleted.append({"name": a.get_name(), "label": a.get_actor_label(), "prior_folder": prior_folder})
print("@@UMCP@@" + json.dumps({"status": "success", "matched": len(targets),
    "deleted_count": len(deleted), "already_trashed_skipped": skipped,
    "note": "moved to hidden _MCP_Trash; undo restores. Not destroyed/purged.",
    "deleted": deleted, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_actors_batch(ctx, name_prefix: str = None, names: list = None) -> str:
        """Soft-delete many actors at once: move each to a hidden '_MCP_Trash' outliner
        folder and hide it in the editor (NOT destroyed, so restoration is perfect).

        name_prefix: soft-delete every actor whose label starts with this prefix.
        names:       explicit list of labels/internal names to soft-delete (takes precedence
                     over name_prefix when both are given).

        Ledgered write: pushes ONE delete_actor entry per actor (with prior folder +
        visibility), so editor_level's `undo` restores each. Actors already in _MCP_Trash
        are skipped."""
        params = {"name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_DELETE_BATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
