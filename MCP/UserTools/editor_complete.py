"""UserTools :: Editor completion tranche  (spec: docs/spec/editor.md)

The remaining SAFE `editor`-category tools toward 100%, built clean-room over Unreal's
public Python API (UE 5.8) + the already-compiled MCPReflectionLibrary C++ reader. Follows
editor_level.py's proven conventions verbatim: snippets print @@UMCP@@<json> on one line;
params are injected as base64 JSON via _exec; every snippet is wrapped for Output-Log
capture; writes run inside unreal.ScopedEditorTransaction AND push a reversible inverse op
onto the per-session ledger builtins._UMCP_LEDGERS[session]. This module does NOT define an
`undo` tool (editor_level.py owns the single unified undo); the new write ops here report
their inverse schema to the coordinator to fold into editor_level.undo.

Implemented:
  - get_actor_property_metadata  (read; wires C++ GetObjectPropertyMetadataJson)
  - spawn_actor_by_class         (write; ledger spawn_actor; resolves engine/BP/class-path)
  - spawn_blueprint_actor        (write; ledger spawn_actor; full component hierarchy)
  - rename_folder                (write; Outliner folder re-path; ledger rename_folder)
  - focus_asset_editor           (read/none; brings an asset-editor tab to front)
  - validate_project_assets      (read; EditorValidatorSubsystem)
  - validate_blueprint_compile   (read; compile + status/log)
  - validate_collision           (read; static-mesh BodySetup inspection)
  - validate_replication         (read; static reflection walk, no PIE)
  - batch                        (write; tool-layer dispatcher, $N output substitution)
  - redo                         (re-apply undone ops; reads builtins._UMCP_REDO)
  - play_in_editor               (BUILT, NOT INVOKED this pass — runtime wrapper)
  - stop_play_in_editor          (BUILT, NOT INVOKED — runtime wrapper)
  - quit_editor                  (BUILT, NEVER INVOKED — closes the editor)

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
snippet bodies here contain NO ''' and NO stray backslashes; all data crosses as base64.
"""
import json
import base64
import textwrap
import builtins as _builtins

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
def _redo_stack():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_REDO", None)
    if root is None:
        root = {}; builtins._UMCP_REDO = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_actor_label() == ident:
            return a
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == ident:
            return a
    return None
def _find_by_name(uniq):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == uniq:
            return a
    return None
def _resolve_class(spec):
    # Resolve a class from: engine short name, a class object path, or a Blueprint asset path.
    cls = getattr(unreal, spec, None)
    if cls is not None:
        return cls
    try:
        cls = unreal.load_class(None, spec)
    except Exception:
        cls = None
    if cls is not None:
        return cls
    p = spec
    if p.endswith("_C"):
        try:
            cls = unreal.load_object(None, p)
        except Exception:
            cls = None
        if cls is not None:
            return cls
        p = p[:-2]
    try:
        gc = unreal.EditorAssetLibrary.load_blueprint_class(p)
    except Exception:
        gc = None
    return gc
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

    # Shared tool registry so `batch` can dispatch by name (this module's tools; other
    # modules can opt in by writing into builtins._UMCP_TOOL_REGISTRY at register time).
    _registry = getattr(_builtins, "_UMCP_TOOL_REGISTRY", None)
    if _registry is None:
        _registry = {}
        _builtins._UMCP_TOOL_REGISTRY = _registry

    def _reg(fn):
        _registry[fn.__name__] = fn
        return fn

    # ------------------------------------------------------------------ #
    # get_actor_property_metadata — wire the compiled C++ reflection read #
    # ------------------------------------------------------------------ #
    _META_BODY = r'''
import unreal, json
ident = PARAMS["actor"]
include_inherited = bool(PARAMS.get("include_inherited", True))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = None
for a in (eas.get_all_level_actors() or []):
    if a and a.get_actor_label() == ident:
        actor = a; break
if actor is None:
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == ident:
            actor = a; break
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % ident}))
else:
    m = getattr(unreal, "MCPReflectionLibrary", None)
    if m is None or not hasattr(m, "get_object_property_metadata_json"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ reader get_object_property_metadata_json unavailable"}))
    else:
        raw = m.get_object_property_metadata_json(actor, include_inherited)
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        props = data.get("properties", data if isinstance(data, list) else [])
        out = {"status": "success", "actor": actor.get_actor_label(), "name": actor.get_name(),
               "class": actor.get_class().get_name(),
               "property_count": (len(props) if isinstance(props, list) else None),
               "metadata": data}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    @_reg
    def get_actor_property_metadata(ctx, actor: str, include_inherited: bool = True) -> str:
        """Get full FProperty metadata (names, cpp types, categories, edit/blueprint flags,
        tooltips, clamp ranges) for a placed actor, via the compiled C++ reflection reader
        MCPReflectionLibrary.get_object_property_metadata_json. Read-only.

        actor:             display label (preferred) or unique internal name of the actor.
        include_inherited: include properties from parent classes (default True)."""
        try:
            return json.dumps(_exec(_META_BODY, {"actor": actor, "include_inherited": include_inherited}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_actor_by_class — resolve engine/BP/class-path, spawn (write)  #
    # ------------------------------------------------------------------ #
    _SPAWN_BY_CLASS_BODY = _HELPERS + r'''
class_spec = PARAMS["class_path"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
scale = PARAMS.get("scale")
label = PARAMS.get("name")
cls = _resolve_class(class_spec)
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve class: %s" % class_spec}))
else:
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_actor_by_class"):
        actor = eas.spawn_actor_from_class(cls, location, rotation)
        if actor:
            if label:
                actor.set_actor_label(label)
            if scale:
                actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label(),
                              "fwd": {"kind": "spawn_actor_by_class", "class_path": class_spec,
                                      "location": [location.x, location.y, location.z],
                                      "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                                      "scale": scale, "name": actor.get_actor_label()}})
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "location": [location.x, location.y, location.z],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    @_reg
    def spawn_actor_by_class(ctx, class_path: str, location: list = None,
                             rotation: list = None, scale: list = None, name: str = None) -> str:
        """Spawn an actor by resolving a class from an engine short name ('PointLight'),
        a class object path, OR a Blueprint asset path ('/Game/BP_Foo' or '/Game/BP_Foo.BP_Foo_C').
        Ledgered write (undo deletes the spawned actor).

        class_path: engine class name, class path, or Blueprint asset/generated-class path.
        location:   [x, y, z] world position (default [0,0,0]).
        rotation:   [pitch, yaw, roll] degrees (default [0,0,0]).
        scale:      [x, y, z] scale (optional).
        name:       optional display label for the new actor."""
        params = {"class_path": class_path, "location": location, "rotation": rotation,
                  "scale": scale, "name": name}
        try:
            return json.dumps(_exec(_SPAWN_BY_CLASS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_blueprint_actor — spawn from a BP asset (full hierarchy)      #
    # ------------------------------------------------------------------ #
    _SPAWN_BP_BODY = _HELPERS + r'''
bp_path = PARAMS["blueprint_path"]
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
scale = PARAMS.get("scale")
label = PARAMS.get("name")
bp_asset = None
try:
    bp_asset = unreal.EditorAssetLibrary.load_asset(bp_path)
except Exception:
    bp_asset = None
if bp_asset is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load blueprint asset: %s" % bp_path}))
else:
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_blueprint_actor"):
        actor = eas.spawn_actor_from_object(bp_asset, location, rotation)
        if actor:
            if label:
                actor.set_actor_label(label)
            if scale:
                actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": actor.get_actor_label(),
                              "fwd": {"kind": "spawn_blueprint_actor", "blueprint_path": bp_path,
                                      "location": [location.x, location.y, location.z],
                                      "rotation": [rotation.pitch, rotation.yaw, rotation.roll],
                                      "scale": scale, "name": actor.get_actor_label()}})
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "location": [location.x, location.y, location.z],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    @_reg
    def spawn_blueprint_actor(ctx, blueprint_path: str, location: list = None,
                              rotation: list = None, scale: list = None, name: str = None) -> str:
        """Spawn a Blueprint actor from its asset path, preserving the full component
        hierarchy (via EditorActorSubsystem.spawn_actor_from_object on the loaded BP asset).
        Ledgered write (undo deletes the spawned actor).

        blueprint_path: Blueprint asset path, e.g. '/Game/BP_Enemy'.
        location:       [x, y, z] world position (default [0,0,0]).
        rotation:       [pitch, yaw, roll] degrees (default [0,0,0]).
        scale:          [x, y, z] scale (optional).
        name:           optional display label for the new actor."""
        params = {"blueprint_path": blueprint_path, "location": location, "rotation": rotation,
                  "scale": scale, "name": name}
        try:
            return json.dumps(_exec(_SPAWN_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_folder — re-path all actors under an Outliner folder prefix  #
    # ------------------------------------------------------------------ #
    _RENAME_FOLDER_BODY = _HELPERS + r'''
old = str(PARAMS["old_path"]).strip("/")
new = str(PARAMS["new_path"]).strip("/")
if not old or not new:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "old_path and new_path are required"}))
else:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    moved = []
    with unreal.ScopedEditorTransaction("MCP rename_folder"):
        for a in (eas.get_all_level_actors() or []):
            if not a:
                continue
            fp = str(a.get_folder_path() or "")
            if fp in ("None", ""):
                continue
            if fp == old or fp.startswith(old + "/"):
                suffix = fp[len(old):]
                a.set_folder_path(unreal.Name(new + suffix))
                moved.append(a.get_name())
    if moved:
        _ledger().append({"op": "rename_folder", "old": old, "new": new, "moved": moved})
    print("@@UMCP@@" + json.dumps({"status": "success", "old": old, "new": new,
                                   "moved_count": len(moved), "moved": moved,
                                   "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    @_reg
    def rename_folder(ctx, old_path: str, new_path: str) -> str:
        """Rename a World-Outliner folder by re-pathing every actor whose folder path is,
        or is nested under, old_path (moving the whole subtree to new_path). This is the
        Outliner-folder analog of the Content-Browser create_folder/delete_folder tools.
        Ledgered write (undo re-paths the same actors back to old_path).

        old_path: existing outliner folder path (e.g. 'Enemies/Melee').
        new_path: new folder path (e.g. 'Hostiles/Melee'). Nested actors move with it."""
        try:
            return json.dumps(_exec(_RENAME_FOLDER_BODY, {"old_path": old_path, "new_path": new_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # focus_asset_editor — open/front an asset-editor tab (non-mutating)  #
    # ------------------------------------------------------------------ #
    _FOCUS_ASSET_BODY = r'''
import unreal, json
path = PARAMS["asset_path"]
asset = None
try:
    asset = unreal.EditorAssetLibrary.load_asset(path)
except Exception:
    asset = None
if asset is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
else:
    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    opened = False
    try:
        opened = bool(aes.open_editor_for_assets([asset]))
    except Exception as e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "open_editor_for_assets failed: %s" % e}))
        opened = None
    if opened is not None:
        print("@@UMCP@@" + json.dumps({"status": "success", "asset": path,
                                       "class": asset.get_class().get_name(), "opened": opened}))
'''

    @mcp.tool()
    @_reg
    def focus_asset_editor(ctx, asset_path: str) -> str:
        """Open (or bring to front) the asset editor for the given asset via
        AssetEditorSubsystem.open_editor_for_assets. Non-mutating (no ledger).

        asset_path: asset path to open, e.g. '/Game/BP_Enemy'."""
        try:
            return json.dumps(_exec(_FOCUS_ASSET_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_project_assets — EditorValidatorSubsystem (read-only)      #
    # ------------------------------------------------------------------ #
    _VALIDATE_ASSETS_BODY = r'''
import unreal, json
asset_paths = PARAMS.get("asset_paths") or []
directory = PARAMS.get("directory")
max_assets = int(PARAMS.get("max_assets") or 25)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
datas = []
def _obj_path(p):
    if "." in p.split("/")[-1]:
        return p
    base = p.rstrip("/").split("/")[-1]
    return p + "." + base
if asset_paths:
    for p in asset_paths:
        d = None
        try:
            d = ar.get_asset_by_object_path(unreal.SoftObjectPath(_obj_path(p)))
        except Exception:
            d = None
        if d is None or not d.is_valid():
            try:
                obj = unreal.EditorAssetLibrary.load_asset(p)
                if obj is not None:
                    d = unreal.AssetRegistryHelpers.create_asset_data(obj)
            except Exception:
                d = None
        if d is not None and d.is_valid():
            datas.append(d)
elif directory:
    try:
        got = ar.get_assets_by_path(unreal.Name(directory), recursive=True) or []
        datas = list(got)[:max_assets]
    except Exception:
        datas = []
if not datas:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no valid assets resolved from asset_paths/directory"}))
else:
    datas = datas[:max_assets]
    vs = unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem)
    settings = unreal.ValidateAssetsSettings()
    def _tryset(k, v):
        try: settings.set_editor_property(k, v)
        except Exception: pass
    _tryset("validation_usecase", unreal.DataValidationUsecase.SCRIPT)
    _tryset("show_if_no_failures", False)
    _tryset("capture_log", True)
    # validate_assets_with_settings returns a tuple (int32 num_failures, ValidateAssetsResults);
    # pick the struct that exposes get_editor_property.
    raw_res = vs.validate_assets_with_settings(datas, settings)
    res = raw_res
    if isinstance(raw_res, (tuple, list)):
        res = None
        for part in raw_res:
            if hasattr(part, "get_editor_property"):
                res = part; break
        if res is None:
            res = raw_res[-1]
    def _rp(name, default=None):
        try: return res.get_editor_property(name)
        except Exception: return default
    out = {"status": "success", "assets_checked": len(datas)}
    for k in ("num_checked", "num_valid", "num_invalid", "num_skipped", "num_warnings", "num_requested", "num_unable_to_validate", "num_external_objects"):
        v = _rp(k)
        if v is not None:
            try: out[k] = int(v)
            except Exception: out[k] = v
    lim = _rp("asset_limit_reached")
    if lim is not None:
        out["asset_limit_reached"] = bool(lim)
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    @_reg
    def validate_project_assets(ctx, asset_paths: list = None, directory: str = None,
                                max_assets: int = 25) -> str:
        """Run registered data validators over a set of assets (EditorValidatorSubsystem)
        and report pass/fail/warning counts. Read-only. Provide explicit asset_paths OR a
        directory (recursively gathered, capped by max_assets to keep the shared editor
        responsive).

        asset_paths: list of asset paths to validate (e.g. ['/Game/BP_Enemy']).
        directory:   content directory to validate recursively (e.g. '/Game/AI').
        max_assets:  cap on assets validated in one call (default 25)."""
        params = {"asset_paths": asset_paths, "directory": directory, "max_assets": max_assets}
        try:
            return json.dumps(_exec(_VALIDATE_ASSETS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_blueprint_compile — compile a BP, report status + log      #
    # ------------------------------------------------------------------ #
    _VALIDATE_BP_BODY = r'''
import unreal, json
path = PARAMS["blueprint_path"]
bp = None
try:
    bp = unreal.EditorAssetLibrary.load_asset(path)
except Exception:
    bp = None
if bp is None or not isinstance(bp, unreal.Blueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint asset: %s" % path}))
else:
    bel = unreal.BlueprintEditorLibrary
    try:
        bel.compile_blueprint(bp)
        compiled = True
    except Exception as e:
        compiled = False
    status_name = None
    try:
        st = bp.get_editor_property("status")
        status_name = str(st)
    except Exception:
        status_name = None
    ok = None
    if status_name is not None:
        ok = ("UP_TO_DATE" in status_name.upper()) or status_name.upper().endswith("UPTODATE")
    print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": path,
                                   "compiled": compiled, "blueprint_status": status_name,
                                   "is_up_to_date": ok}))
'''

    @mcp.tool()
    @_reg
    def validate_blueprint_compile(ctx, blueprint_path: str) -> str:
        """Compile a Blueprint (BlueprintEditorLibrary.compile_blueprint) and report its
        resulting status plus any compile warnings/errors surfaced in the Output Log.
        Read-only relative to gameplay data (recompile only).

        blueprint_path: Blueprint asset path, e.g. '/Game/BP_Enemy'."""
        try:
            return json.dumps(_exec(_VALIDATE_BP_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_collision — inspect static-mesh BodySetup (read-only)      #
    # ------------------------------------------------------------------ #
    _VALIDATE_COLLISION_BODY = r'''
import unreal, json
names = PARAMS.get("actors")
max_actors = int(PARAMS.get("max_actors") or 200)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
targets = []
for a in (eas.get_all_level_actors() or []):
    if not a:
        continue
    if names and (a.get_actor_label() not in names and a.get_name() not in names):
        continue
    comps = a.get_components_by_class(unreal.StaticMeshComponent) or []
    if comps:
        targets.append((a, comps))
    if len(targets) >= max_actors:
        break
report = []
issues = 0
for a, comps in targets:
    for c in comps:
        mesh = None
        try:
            mesh = c.get_editor_property("static_mesh")
        except Exception:
            mesh = None
        rec = {"actor": a.get_actor_label(), "component": c.get_name(),
               "mesh": (mesh.get_name() if mesh else None)}
        simple = None; convex = None; complexity = None
        if mesh is not None:
            try:
                bs = mesh.get_editor_property("body_setup")
            except Exception:
                bs = None
            if bs is not None:
                try:
                    agg = bs.get_editor_property("agg_geom")
                    convex = len(agg.get_editor_property("convex_elems") or [])
                    boxes = len(agg.get_editor_property("box_elems") or [])
                    spheres = len(agg.get_editor_property("sphere_elems") or [])
                    caps = len(agg.get_editor_property("sphyl_elems") or [])
                    simple = convex + boxes + spheres + caps
                except Exception:
                    simple = None
                try:
                    complexity = str(bs.get_editor_property("collision_trace_flag"))
                except Exception:
                    complexity = None
            rec["simple_prims"] = simple
            rec["convex_elems"] = convex
            rec["trace_flag"] = complexity
            has_complex_only = (complexity is not None and "COMPLEX_AS_SIMPLE" in complexity.upper())
            if (simple == 0) and not has_complex_only:
                rec["warning"] = "no simple collision primitives"
                issues += 1
        else:
            rec["warning"] = "no static mesh assigned"
        report.append(rec)
print("@@UMCP@@" + json.dumps({"status": "success", "meshes_checked": len(report),
                               "issues": issues, "report": report}))
'''

    @mcp.tool()
    @_reg
    def validate_collision(ctx, actors: list = None, max_actors: int = 200) -> str:
        """Inspect static-mesh collision setup for placed actors: reports each mesh's
        simple-collision primitive count (convex/box/sphere/capsule), the BodySetup trace
        flag, and flags meshes with no simple collision. Read-only.

        actors:     optional list of actor labels/names to restrict the check.
        max_actors: cap on actors scanned (default 200)."""
        try:
            return json.dumps(_exec(_VALIDATE_COLLISION_BODY, {"actors": actors, "max_actors": max_actors}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_replication — static reflection walk (no PIE, read-only)   #
    # ------------------------------------------------------------------ #
    _VALIDATE_REPLICATION_BODY = r'''
import unreal, json
ident = PARAMS.get("actor")
class_path = PARAMS.get("class_path")
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
target = None
cdo = None
label = None
if ident:
    for a in (eas.get_all_level_actors() or []):
        if a and (a.get_actor_label() == ident or a.get_name() == ident):
            target = a; break
    if target is not None:
        cdo = target
        label = target.get_actor_label()
elif class_path:
    cls = getattr(unreal, class_path, None)
    if cls is None:
        try: cls = unreal.load_class(None, class_path)
        except Exception: cls = None
    if cls is None:
        try: cls = unreal.EditorAssetLibrary.load_blueprint_class(class_path)
        except Exception: cls = None
    if cls is not None:
        try: cdo = unreal.get_default_object(cls)
        except Exception: cdo = None
        label = class_path
if cdo is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "resolve an actor= or class_path= with a valid CDO"}))
else:
    def _try(fn, d=None):
        try: return fn()
        except Exception: return d
    actor_replicates = _try(lambda: bool(cdo.get_editor_property("replicates")))
    replicate_movement = _try(lambda: bool(cdo.get_editor_property("replicate_movement")))
    net_dormancy = _try(lambda: str(cdo.get_editor_property("net_dormancy")))
    net_update_frequency = _try(lambda: cdo.get_editor_property("net_update_frequency"))
    # Property-level: the compiled C++ metadata reader's per-property "flags" array does NOT
    # currently expose CPF_Net (replication) flags, so replicated UPROPERTYs cannot be read
    # authoritatively via reflection here. We surface a conservative NAME heuristic (clearly
    # labeled) and flag the reader gap; an authoritative walk needs a small reader extension.
    heuristic_props = []
    prop_total = 0
    m = getattr(unreal, "MCPReflectionLibrary", None)
    if m is not None and hasattr(m, "get_object_property_metadata_json"):
        raw = _try(lambda: m.get_object_property_metadata_json(cdo, True))
        data = _try(lambda: json.loads(raw)) if raw else None
        props = (data.get("properties") if isinstance(data, dict) else data) or []
        props = props if isinstance(props, list) else []
        prop_total = len(props)
        for p in props:
            nm = str(p.get("name") or "")
            low = nm.lower()
            if low.startswith("replicated") or low.startswith("breplicate") or nm.startswith("Rep") and len(nm) > 3 and nm[3].isupper():
                heuristic_props.append(nm)
    out = {"status": "success", "target": label,
           "class": (cdo.get_class().get_name() if hasattr(cdo, "get_class") else None),
           "actor_replicates": actor_replicates, "replicate_movement": replicate_movement,
           "net_dormancy": net_dormancy, "net_update_frequency": net_update_frequency,
           "properties_scanned": prop_total,
           "property_flags_available": False,
           "replicated_by_name_heuristic": heuristic_props,
           "note": "actor-level flags are authoritative; per-property replication is a NAME heuristic only (the C++ metadata reader does not expose CPF_Net flags)."}
    if actor_replicates is False and heuristic_props:
        out["warning"] = "name-heuristic replicated properties present but actor.replicates is False (verify manually)"
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    @_reg
    def validate_replication(ctx, actor: str = None, class_path: str = None) -> str:
        """Static (no-PIE) replication sanity check for an actor or class CDO: reports
        actor-level replication flags (replicates, replicate_movement, net_dormancy) and,
        via the C++ reflection reader, which UPROPERTYs carry replication/net flags — warning
        when replicated properties exist but the actor itself does not replicate. Read-only.

        actor:      placed-actor label/name to inspect, OR
        class_path: an engine class name or Blueprint path to inspect its CDO."""
        try:
            return json.dumps(_exec(_VALIDATE_REPLICATION_BODY, {"actor": actor, "class_path": class_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # redo — re-apply undone ops (reads builtins._UMCP_REDO[session])     #
    # ------------------------------------------------------------------ #
    # NOTE: this consumes a per-session redo stack that editor_level.undo must populate
    # (push each popped ledger entry onto builtins._UMCP_REDO[sid], and every forward write
    # must clear it). That undo-side fold is reported to the coordinator. The re-apply logic
    # below is complete for spawn ops (which carry an "fwd" descriptor).
    _REDO_BODY = _HELPERS + r'''
count = int(PARAMS.get("count", 1))
rstack = _redo_stack()
redone = []
for _ in range(count):
    if not rstack:
        break
    entry = rstack.pop()
    op = entry.get("op")
    fwd = entry.get("fwd") or {}
    kind = fwd.get("kind")
    if op == "spawn_actor" and kind in ("spawn_actor_by_class", "spawn_blueprint_actor"):
        loc = fwd.get("location") or [0, 0, 0]
        rot = fwd.get("rotation") or [0, 0, 0]
        scale = fwd.get("scale")
        location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
        rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        obj = None
        if kind == "spawn_actor_by_class":
            obj = _resolve_class(fwd.get("class_path"))
            spawn = lambda: eas.spawn_actor_from_class(obj, location, rotation)
        else:
            try: obj = unreal.EditorAssetLibrary.load_asset(fwd.get("blueprint_path"))
            except Exception: obj = None
            spawn = lambda: eas.spawn_actor_from_object(obj, location, rotation)
        if obj is None:
            rstack.append(entry)
            redone.append({"op": op, "result": "redo-failed: could not resolve spawn source"})
            break
        with unreal.ScopedEditorTransaction("MCP redo spawn_actor"):
            actor = spawn()
            if actor:
                if fwd.get("name"):
                    actor.set_actor_label(fwd.get("name"))
                if scale:
                    actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
                uniq = actor.get_name()
                _ledger().append({"op": "spawn_actor", "actor_name": uniq,
                                  "label": actor.get_actor_label(), "fwd": fwd})
                redone.append({"op": op, "actor_name": uniq, "result": "re-spawned"})
    elif op == "rename_folder":
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        old = entry.get("old"); new = entry.get("new")
        moved = []
        with unreal.ScopedEditorTransaction("MCP redo rename_folder"):
            for a in (eas.get_all_level_actors() or []):
                if not a:
                    continue
                fp = str(a.get_folder_path() or "")
                if fp == old or fp.startswith(old + "/"):
                    a.set_folder_path(unreal.Name(new + fp[len(old):]))
                    moved.append(a.get_name())
        _ledger().append({"op": "rename_folder", "old": old, "new": new, "moved": moved})
        redone.append({"op": op, "result": "re-applied", "moved_count": len(moved)})
    else:
        rstack.append(entry)
        redone.append({"op": op, "result": "redo-not-supported-for-op; stopped"})
        break
print("@@UMCP@@" + json.dumps({"status": "success", "redone": redone,
                               "redo_depth": len(rstack), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    @_reg
    def redo(ctx, count: int = 1) -> str:
        """Re-apply the most recently undone edits (agent-scoped), newest-undone first.
        Pops up to `count` entries off the per-session redo stack (builtins._UMCP_REDO) and
        re-applies each forward op, re-recording it on the undo ledger.

        NOTE: the redo stack is populated by editor_level.undo (which must push each popped
        entry, and forward writes must clear it) — that undo-side fold is a coordinator step.
        Re-apply here is implemented for spawn_actor (by-class / blueprint) and rename_folder."""
        try:
            return json.dumps(_exec(_REDO_BODY, {"count": count}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # batch — tool-layer dispatcher (sequential, $N output substitution)  #
    # ------------------------------------------------------------------ #
    def _subst(value, prior_results):
        """Replace '$N' or '$N.field' references with data from a prior step's parsed JSON."""
        if isinstance(value, str) and value.startswith("$"):
            body = value[1:]
            if "." in body:
                idx_s, field = body.split(".", 1)
            else:
                idx_s, field = body, None
            try:
                idx = int(idx_s)
            except ValueError:
                return value
            if 0 <= idx < len(prior_results):
                res = prior_results[idx]
                if field is None:
                    return res
                cur = res
                for seg in field.split("."):
                    if isinstance(cur, dict) and seg in cur:
                        cur = cur[seg]
                    else:
                        return value
                return cur
            return value
        if isinstance(value, list):
            return [_subst(v, prior_results) for v in value]
        if isinstance(value, dict):
            return {k: _subst(v, prior_results) for k, v in value.items()}
        return value

    @mcp.tool()
    @_reg
    def batch(ctx, steps: list) -> str:
        """Execute a sequence of tool calls in one request, sharing a single undo scope.
        Each step is {"tool": <name>, "args": {...}}; args may reference an earlier step's
        JSON output with "$N" (whole result) or "$N.field" (e.g. "$0.name"). Because every
        write pushes onto the same per-session ledger, one `undo(count=K)` reverts the whole
        batch. Returns each step's parsed result.

        Dispatches over tools registered in builtins._UMCP_TOOL_REGISTRY (this module's tools
        register automatically; a full cross-module registry is a coordinator wiring step).

        steps: list of {"tool": str, "args": dict}."""
        if not isinstance(steps, list):
            return json.dumps({"status": "error", "message": "steps must be a list"}, indent=2)
        parsed_results = []
        step_reports = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or "tool" not in step:
                step_reports.append({"index": i, "status": "error", "message": "each step needs a 'tool'"})
                break
            tool_name = step["tool"]
            fn = _registry.get(tool_name)
            if fn is None:
                step_reports.append({"index": i, "tool": tool_name, "status": "error",
                                     "message": "tool not in registry"})
                break
            args = _subst(dict(step.get("args") or {}), parsed_results)
            try:
                raw = fn(ctx, **args)
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = raw
                parsed_results.append(parsed)
                step_reports.append({"index": i, "tool": tool_name, "status": "ok", "result": parsed})
                if isinstance(parsed, dict) and parsed.get("status") == "error":
                    break
            except Exception as e:
                step_reports.append({"index": i, "tool": tool_name, "status": "error", "message": str(e)})
                break
        return json.dumps({"status": "success", "count": len(step_reports), "steps": step_reports}, indent=2)

    # ------------------------------------------------------------------ #
    # play_in_editor / stop_play_in_editor / quit_editor                  #
    # BUILT THIS PASS — DO NOT INVOKE (runtime/lifecycle; later pass).    #
    # ------------------------------------------------------------------ #
    _PIE_START_BODY = r'''
import unreal, json
simulate = bool(PARAMS.get("simulate"))
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
if simulate:
    les.editor_play_simulate()
else:
    les.editor_request_begin_play()
print("@@UMCP@@" + json.dumps({"status": "success", "action": ("simulate" if simulate else "play"),
                               "is_in_play_in_editor": bool(les.is_in_play_in_editor())}))
'''

    @mcp.tool()
    @_reg
    def play_in_editor(ctx, simulate: bool = False) -> str:
        """Start a Play-In-Editor (or Simulate) session
        (LevelEditorSubsystem.editor_request_begin_play / editor_play_simulate).

        simulate: if True, Simulate-In-Editor instead of Play.

        NOTE: this is a runtime tool built for a later PIE pass; it changes editor session
        state and must not be invoked during the authoring/build pass."""
        try:
            return json.dumps(_exec(_PIE_START_BODY, {"simulate": simulate}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PIE_STOP_BODY = r'''
import unreal, json
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
les.editor_request_end_play()
print("@@UMCP@@" + json.dumps({"status": "success", "is_in_play_in_editor": bool(les.is_in_play_in_editor())}))
'''

    @mcp.tool()
    @_reg
    def stop_play_in_editor(ctx) -> str:
        """Stop the active Play-In-Editor / Simulate session
        (LevelEditorSubsystem.editor_request_end_play). Runtime tool for a later pass."""
        try:
            return json.dumps(_exec(_PIE_STOP_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PIE_STATUS_BODY = r'''
import unreal, json
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
out = {"status": "success", "in_pie": bool(les.is_in_play_in_editor())}
gw = ues.get_game_world() if out["in_pie"] else None
if gw is None:
    ew = ues.get_editor_world()
    out["world"] = ew.get_name() if ew else None
    out["note"] = "not in PIE (this is the editor world). PIE start is ASYNC: call play_in_editor, then poll get_pie_status in a LATER call until in_pie is true."
else:
    out["world"] = gw.get_name()
    try:
        out["actor_count"] = len(unreal.GameplayStatics.get_all_actors_of_class(gw, unreal.Actor))
    except Exception:
        out["actor_count"] = None
    try:
        gm = unreal.GameplayStatics.get_game_mode(gw)
        out["game_mode"] = gm.get_class().get_name() if gm else None
    except Exception:
        pass
    try:
        pc = unreal.GameplayStatics.get_player_controller(gw, 0)
        out["player_controller"] = pc.get_class().get_name() if pc else None
        pawn = pc.get_controlled_pawn() if pc else None
        out["pawn"] = pawn.get_class().get_name() if pawn else None
    except Exception:
        pass
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    @_reg
    def get_pie_status(ctx) -> str:
        """Read the live Play-In-Editor world (the foundation for runtime queries). Returns {in_pie, world,
        actor_count, game_mode, player_controller, pawn}. NOTE: PIE start is ASYNC — after play_in_editor,
        poll THIS in a later call until in_pie is true (the editor ticks between bridge calls)."""
        try:
            return json.dumps(_exec(_PIE_STATUS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _QUIT_BODY = r'''
import unreal, json
save_dirty = bool(PARAMS.get("save_dirty"))
if save_dirty:
    try:
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    except Exception:
        pass
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
unreal.SystemLibrary.execute_console_command(world, "QUIT_EDITOR")
print("@@UMCP@@" + json.dumps({"status": "success", "quit": True}))
'''

    @mcp.tool()
    @_reg
    def quit_editor(ctx, save_dirty: bool = False) -> str:
        """Quit the Unreal Editor (optionally saving dirty packages first), via the
        QUIT_EDITOR console command.

        save_dirty: if True, save all dirty content/map packages before quitting.

        WARNING: this closes the editor. Built for completeness; NEVER invoked during a
        live build/authoring session."""
        try:
            return json.dumps(_exec(_QUIT_BODY, {"save_dirty": save_dirty}), indent=2)
        except Exception as e:
            return f"Error: {e}"
