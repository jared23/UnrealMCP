"""UserTools :: Editor / Level lifecycle  (spec: docs/spec/editor.md)

The deferred map-switch / level-lifecycle `editor`-category tools, built clean-room over
Unreal's public Python API (UE 5.8) following editor_level.py / editor_complete.py conventions
verbatim: snippets print @@UMCP@@<json> on one line; params cross as base64 JSON via _exec;
every snippet is wrapped for Output-Log capture. Ledgered writes push a reversible inverse op
onto the per-session ledger builtins._UMCP_LEDGERS[session]; this module does NOT define an
`undo` tool (editor_level.py owns the single unified undo).

Implemented:
  - create_level        (write; ledger create_asset for the new .umap; new level becomes loaded world)
  - open_level          (NAVIGATION; NOT ledgered — opening a level has no clean inverse)
  - save_level_as       (write; ledger create_asset for the new .umap; save_map re-points current world)
  - setup_default_scene (write; reuses op=spawn_actor per spawned actor -> existing undo destroys each)

PROTOCOL #6c CAVEAT: scripted level load/switch has historically been UNRELIABLE from the command-
handler tick (load_level can return success without the world tearing down within the call). Every
switch here VERIFIES the loaded world name after the call (get_editor_world) and reports honestly
whether the world actually changed.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies here contain NO ''' and NO stray backslashes; all data crosses as base64.
"""
import json
import base64
import textwrap

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

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


# Minimal Unreal-side helpers (prepended to write bodies). No ''' / no backslashes.
_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _world_name():
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    w = ues.get_editor_world()
    return (w.get_name() if w else None), (w.get_path_name() if w else None)
def _norm(package_path, name):
    pp = str(package_path).rstrip("/")
    nm = str(name).strip().lstrip("/")
    return pp, nm, pp + "/" + nm
'''


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None)
    if not session:
        import os
        session = "s" + str(os.getpid())

    def _query(code):
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
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # create_level — new empty (or template-based) level, then save      #
    # ------------------------------------------------------------------ #
    _CREATE_LEVEL_BODY = _HELPERS + r'''
package_path = PARAMS["package_path"]
name = PARAMS["name"]
template = PARAMS.get("template")
partitioned = bool(PARAMS.get("partitioned", False))
pp, nm, asset_path = _norm(package_path, name)
if not nm:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
elif unreal.EditorAssetLibrary.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    before_name, before_path = _world_name()
    created_dir = not unreal.EditorAssetLibrary.does_directory_exist(pp)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ok = False
    err = None
    try:
        if template:
            ok = bool(les.new_level_from_template(asset_path, template))
        else:
            ok = bool(les.new_level(asset_path, partitioned))
    except Exception as e:
        err = str(e); ok = False
    if not ok:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_level failed", "detail": err,
                                       "asset_path": asset_path}))
    else:
        try:
            les.save_current_level()
        except Exception:
            pass
        exists = unreal.EditorAssetLibrary.does_asset_exist(asset_path)
        after_name, after_path = _world_name()
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": pp, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
                                       "package_path": pp, "created_dir": created_dir,
                                       "asset_exists": bool(exists),
                                       "world_before": before_name, "world_after": after_name,
                                       "world_after_path": after_path,
                                       "world_switched": (after_path != before_path),
                                       "is_now_loaded_world": (nm in str(after_name)),
                                       "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_level(ctx, package_path: str, name: str, template: str = None,
                     partitioned: bool = False) -> str:
        """Create a new level (.umap) and save it. With no template it is an empty level via
        LevelEditorSubsystem.new_level; with a template it uses new_level_from_template. The
        newly created level BECOMES the loaded editor world (expected). Ledgered write: records
        a create_asset op so the unified undo can remove the .umap (note: the loaded level cannot
        be deleted while it is current — undo must run after switching away).

        package_path: content directory for the new level, e.g. '/Game/MCP_Scratch'.
        name:         level asset name (no extension), e.g. 'MyLevel'.
        template:     optional template level asset path to clone (e.g. '/Engine/Maps/Templates/OpenWorld').
        partitioned:  create as a World Partition level (default False; ignored when template is set)."""
        params = {"package_path": package_path, "name": name, "template": template,
                  "partitioned": partitioned}
        try:
            return json.dumps(_exec(_CREATE_LEVEL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # open_level — load an existing level (NAVIGATION, not ledgered)      #
    # ------------------------------------------------------------------ #
    _OPEN_LEVEL_BODY = _HELPERS + r'''
level_path = str(PARAMS["level_path"])
if not unreal.EditorAssetLibrary.does_asset_exist(level_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "level asset does not exist: %s" % level_path}))
else:
    before_name, before_path = _world_name()
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ok = False
    err = None
    try:
        ok = bool(les.load_level(level_path))
    except Exception as e:
        err = str(e); ok = False
    after_name, after_path = _world_name()
    # PROTOCOL #6c: load_level may report success but not flush within the handler tick.
    # Report what get_editor_world actually says now, so the caller can trust the world state.
    switched = (after_path != before_path)
    leaf = level_path.rstrip("/").split("/")[-1].split(".")[-1]
    print("@@UMCP@@" + json.dumps({"status": ("success" if ok else "error"),
                                   "level_path": level_path, "load_level_returned": ok, "detail": err,
                                   "world_before": before_name, "world_after": after_name,
                                   "world_after_path": after_path,
                                   "world_switched": switched,
                                   "loaded_target": (leaf in str(after_name)),
                                   "note": "NAVIGATION only (not ledgered). If world_switched is false the switch did not flush within this call (PROTOCOL #6c)."}))
'''

    @mcp.tool()
    def open_level(ctx, level_path: str) -> str:
        """Open (load) an existing level via LevelEditorSubsystem.load_level. Pure NAVIGATION —
        NOT ledgered: opening a level has no clean inverse (the previously-loaded level is not
        restorable as a discrete op). Verifies whether the editor world actually switched
        (get_editor_world) and reports honestly — per PROTOCOL #6c a scripted load can return
        success without flushing the world within the handler tick.

        level_path: existing level asset path, e.g. '/Game/MCP_Scratch/MyLevel'."""
        try:
            return json.dumps(_exec(_OPEN_LEVEL_BODY, {"level_path": level_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # save_level_as — save current level under a new name (write)        #
    # ------------------------------------------------------------------ #
    _SAVE_AS_BODY = _HELPERS + r'''
new_package_path = PARAMS["new_package_path"]
new_name = PARAMS["new_name"]
pp, nm, asset_path = _norm(new_package_path, new_name)
if not nm:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_name is required"}))
elif unreal.EditorAssetLibrary.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    before_name, before_path = _world_name()
    created_dir = not unreal.EditorAssetLibrary.does_directory_exist(pp)
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = ues.get_editor_world()
    ok = False
    err = None
    try:
        ok = bool(unreal.EditorLoadingAndSavingUtils.save_map(world, asset_path))
    except Exception as e:
        err = str(e); ok = False
    exists = unreal.EditorAssetLibrary.does_asset_exist(asset_path)
    after_name, after_path = _world_name()
    if not ok and not exists:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "save_map failed", "detail": err,
                                       "asset_path": asset_path}))
    else:
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": pp, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
                                       "package_path": pp, "created_dir": created_dir,
                                       "asset_exists": bool(exists), "save_map_returned": ok,
                                       "world_before": before_name, "world_after": after_name,
                                       "world_after_path": after_path,
                                       "current_world_repointed": (after_path != before_path),
                                       "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def save_level_as(ctx, new_package_path: str, new_name: str) -> str:
        """Save the current editor level under a new name/path via
        EditorLoadingAndSavingUtils.save_map (a 'Save As'), creating a new .umap. This typically
        re-points the current editor world to the newly saved map. Ledgered write: records a
        create_asset op for the new .umap (undo removes it — after switching away, since a loaded
        level cannot be deleted).

        new_package_path: content directory for the saved copy, e.g. '/Game/MCP_Scratch'.
        new_name:         new level asset name (no extension), e.g. 'MyLevel_Copy'."""
        params = {"new_package_path": new_package_path, "new_name": new_name}
        try:
            return json.dumps(_exec(_SAVE_AS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # setup_default_scene — populate current level with a lit environment #
    # reuses op=spawn_actor per actor so editor_level.undo destroys each  #
    # ------------------------------------------------------------------ #
    _SETUP_SCENE_BODY = _HELPERS + r'''
floor = bool(PARAMS.get("floor", True))
sky = bool(PARAMS.get("sky", True))
light = bool(PARAMS.get("light", True))
clouds = bool(PARAMS.get("clouds", False))
post_process = bool(PARAMS.get("post_process", False))
player_start = bool(PARAMS.get("player_start", True))
sun_pitch = float(PARAMS.get("sun_pitch", -45.0))
sun_yaw = float(PARAMS.get("sun_yaw", 45.0))
sun_roll = float(PARAMS.get("sun_roll", 0.0))
floor_size = float(PARAMS.get("floor_size", 20.0))
folder = PARAMS.get("folder")
prefix = str(PARAMS.get("name_prefix") or "MCP_Scene_")
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
spawned = []
warnings = []
def _spawn(cls, loc, rot, label):
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    a = eas.spawn_actor_from_class(cls, location, rotation)
    if a:
        a.set_actor_label(label)
        if folder:
            try:
                a.set_folder_path(unreal.Name(folder))
            except Exception:
                pass
        uniq = a.get_name()
        _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": label})
        spawned.append({"actor_name": uniq, "label": label, "class": a.get_class().get_name()})
    return a
with unreal.ScopedEditorTransaction("MCP setup_default_scene"):
    if floor:
        fa = _spawn(unreal.StaticMeshActor, [0, 0, 0], [0, 0, 0], prefix + "Floor")
        if fa:
            try:
                comp = fa.get_editor_property("static_mesh_component")
                mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Plane.Plane")
                if comp and mesh:
                    comp.set_static_mesh(mesh)
                fa.set_actor_scale3d(unreal.Vector(floor_size, floor_size, 1.0))
                comp.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
            except Exception as e:
                warnings.append("floor mesh: %s" % e)
    if light:
        _spawn(unreal.DirectionalLight, [0, 0, 1000], [sun_pitch, sun_yaw, sun_roll], prefix + "Sun")
    if sky:
        _spawn(unreal.SkyLight, [0, 0, 800], [0, 0, 0], prefix + "SkyLight")
        _spawn(unreal.SkyAtmosphere, [0, 0, 0], [0, 0, 0], prefix + "SkyAtmosphere")
    if clouds:
        vc = getattr(unreal, "VolumetricCloud", None)
        if vc:
            _spawn(vc, [0, 0, 0], [0, 0, 0], prefix + "VolumetricCloud")
        else:
            warnings.append("VolumetricCloud class unavailable")
    if post_process:
        _spawn(unreal.PostProcessVolume, [0, 0, 0], [0, 0, 0], prefix + "PostProcess")
    if player_start:
        ps = getattr(unreal, "PlayerStart", None)
        if ps:
            _spawn(ps, [0, 0, 100], [0, 0, 0], prefix + "PlayerStart")
        else:
            warnings.append("PlayerStart class unavailable")
out = {"status": "success", "spawned_count": len(spawned), "spawned": spawned,
       "ledger_depth": len(_ledger())}
if warnings:
    out["warnings"] = warnings
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def setup_default_scene(ctx, floor: bool = True, sky: bool = True, light: bool = True,
                            clouds: bool = False, post_process: bool = False,
                            player_start: bool = True, sun_pitch: float = -45.0,
                            sun_yaw: float = 45.0, sun_roll: float = 0.0,
                            floor_size: float = 20.0, folder: str = None,
                            name_prefix: str = "MCP_Scene_") -> str:
        """Populate the CURRENT level with a default lit environment: a floor plane, a
        directional (sun) light, a sky light + sky atmosphere, and a player start (clouds /
        post-process optional). Each actor is spawned via EditorActorSubsystem and recorded as a
        separate op=spawn_actor on the ledger, so the unified undo destroys each one individually.
        Spawns into whatever level is currently loaded — run only on a scratch/disposable level.

        floor/sky/light/clouds/post_process/player_start: toggles for each element.
        sun_pitch/sun_yaw/sun_roll: directional-light rotation in degrees.
        floor_size:  uniform XY scale for the floor plane (default 20 => ~2000x2000 uu).
        folder:      optional Outliner folder to place the spawned actors under.
        name_prefix: label prefix for the spawned actors (default 'MCP_Scene_')."""
        params = {"floor": floor, "sky": sky, "light": light, "clouds": clouds,
                  "post_process": post_process, "player_start": player_start,
                  "sun_pitch": sun_pitch, "sun_yaw": sun_yaw, "sun_roll": sun_roll,
                  "floor_size": floor_size, "folder": folder, "name_prefix": name_prefix}
        try:
            return json.dumps(_exec(_SETUP_SCENE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
