"""UserTools :: Foliage / Procedural-vegetation placement  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8.1),
mirroring editor_level.py / volumes_write.py conventions VERBATIM: base64-injected PARAMS,
the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block.

Reversible WRITE tools for foliage placement (the write counterpart to a foliage read path):
placing foliage instances makes the level's InstancedFoliageActor hold real foliage data.

  - add_foliage_type       -> create a FoliageType_InstancedStaticMesh ASSET pointing at a
                              static mesh (in /Game/MCP_Scratch, prefixed MCP_A_). Reversible
                              via the EXISTING unified undo op `create_asset` (deletes the asset).
  - add_foliage_instances  -> place N instances of a foliage type at explicit transforms into
                              the active level's InstancedFoliageActor (auto-created if none).
  - scatter_foliage_in_box -> scatter K instances of a foliage type at random positions within
                              a box (procedural vegetation convenience). Same write body/inverse.

Foliage API notes (UE 5.8.1; probed live against TestMCPSetup / Lvl_Combat):
  - unreal.FoliageType_InstancedStaticMesh is the ISM foliage type; its static mesh is the
    editor property `mesh`. Created non-interactively via AssetTools.create_asset(...,
    FoliageType_InstancedStaticMesh, FoliageType_InstancedStaticMeshFactory()) -- the factory is
    NON-modal (bounded-probed: create/set-mesh/save/delete all return cleanly). We SAVE on create
    (reliable undo-delete) and set `mesh` AFTER create (no factory config prompt).
  - Placement: AInstancedFoliageActor.add_instances(world_context_object, foliage_type,
    transforms:[Transform]) -- callable on the CDO (unreal.InstancedFoliageActor.
    get_default_object()); it routes via the world context and AUTO-CREATES the level's IFA if
    none exists. Inverse: remove_all_instances(world_context_object, foliage_type) -- removes all
    instances of THAT type (type-scoped, so safe alongside other foliage). Because we use UNIQUE
    MCP_A_ foliage-type assets, remove_all of the type == remove exactly what we added.
  - When add creates the IFA (level had none), the empty IFA persists after remove_all; the undo
    destroys that IFA too (only if we created it AND it is empty) so the level is left EXACTLY as
    found. If an IFA already existed (user had foliage), we NEVER destroy it -- only remove our
    instances of our type.
  - Verification count = sum of InstancedStaticMeshComponent.get_instance_count() over IFA
    components whose static_mesh == the type's mesh. (FoliageStatistics.foliage_overlapping_box_count
    proved unreliable in-editor -- returned 0 for freshly-added instances -- so it is NOT used.)

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - add_foliage_type       -> op "create_asset" {asset_path, package_path, created_dir:None}
                              (EXISTING editor_level.undo branch -> deletes the asset).
  - add_foliage_instances  -> op "add_foliage_instances" {foliage_type_path, count, created_ifa,
    / scatter_foliage_in_box   ifa_names, prior_type_count}  (NEW editor_level.undo branch ->
                              remove_all_instances(type) + destroy created empty IFA).
LIFO ordering matters: add the type, THEN add instances; undo pops instances FIRST (removes them,
releasing the type reference) THEN deletes the now-unreferenced type asset. No `undo` tool is
defined here (editor_level.py owns the ONE unified undo). Prefix everything MCP_A_.
"""
import json
import base64
import textwrap
import os
import random

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
'''

    # Shared foliage helpers (prepended to foliage write bodies). No ''' / no backslashes.
    _FOLIAGE_HELPERS = r'''
def _fol_world():
    _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return _ues.get_editor_world() if _ues else None
def _fol_ifas():
    _eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return [a for a in (_eas.get_all_level_actors() or []) if a and a.get_class().get_name() == "InstancedFoliageActor"]
def _fol_type_count(mesh):
    _tot = 0
    _mp = mesh.get_path_name() if mesh else None
    for _a in _fol_ifas():
        for _c in (_a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
            try:
                _sm = _c.get_editor_property("static_mesh")
            except Exception:
                _sm = None
            if _sm is not None and _mp is not None and _sm.get_path_name() == _mp:
                try:
                    _tot += _c.get_instance_count()
                except Exception:
                    pass
    return _tot
def _fol_build_transforms(items):
    _out = []
    for _it in items:
        if isinstance(_it, (list, tuple)):
            _loc = _it; _rot = None; _scl = None
        else:
            _loc = _it.get("location") or _it.get("loc") or [0, 0, 0]
            _rot = _it.get("rotation") or _it.get("rot")
            _scl = _it.get("scale")
        _t = unreal.Transform()
        _t.translation = unreal.Vector(float(_loc[0]), float(_loc[1]), float(_loc[2]))
        if _rot:
            _t.rotation = unreal.Rotator(pitch=float(_rot[0]), yaw=float(_rot[1]), roll=float(_rot[2])).quaternion()
        if _scl is not None:
            if isinstance(_scl, (list, tuple)):
                _t.scale3d = unreal.Vector(float(_scl[0]), float(_scl[1]), float(_scl[2]))
            else:
                _s = float(_scl); _t.scale3d = unreal.Vector(_s, _s, _s)
        _out.append(_t)
    return _out
'''

    # ------------------------------------------------------------------ #
    # add_foliage_type — create a FoliageType_InstancedStaticMesh asset    #
    # ------------------------------------------------------------------ #
    _ADD_TYPE_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
mesh_path = PARAMS["static_mesh"]
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
if not name.startswith("MCP_A_"):
    name = "MCP_A_" + name
full = pkg + "/" + name
mesh = unreal.EditorAssetLibrary.load_asset(mesh_path) if mesh_path else None
if mesh is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "static_mesh not found: %s" % mesh_path}))
elif unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
    at = unreal.AssetToolsHelpers.get_asset_tools()
    # NB: create_asset is NOT wrapped in a transaction (that would trap the asset in the undo
    # buffer and block a later delete). The FoliageType_InstancedStaticMeshFactory is non-modal.
    asset = at.create_asset(name, pkg, unreal.FoliageType_InstancedStaticMesh, unreal.FoliageType_InstancedStaticMeshFactory())
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % full}))
    else:
        asset.set_editor_property("mesh", mesh)
        applied = {}
        for _k, _prop in (("density", "density"), ("radius", "radius"), ("random_yaw", "random_yaw"),
                          ("align_to_normal", "align_to_normal"), ("z_offset", "z_offset"),
                          ("ground_slope_angle", "ground_slope_angle")):
            _v = PARAMS.get(_k)
            if _v is not None:
                try:
                    asset.set_editor_property(_prop, _v)
                    applied[_prop] = _v
                except Exception:
                    pass
        # Optional uniform scale range via the FoliageScaling struct on `scaling`/scale_x.
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full, "package_path": pkg, "created_dir": None})
        rb = asset.get_editor_property("mesh")
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(),
            "mesh": (rb.get_path_name() if rb else None), "applied_props": applied,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_foliage_type(ctx, name: str, static_mesh: str, density: float = None,
                         radius: float = None, random_yaw: bool = None,
                         align_to_normal: bool = None, z_offset: float = None,
                         ground_slope_angle: float = None,
                         package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a FoliageType_InstancedStaticMesh asset that points at a static mesh, so it can
        be placed with add_foliage_instances / scatter_foliage_in_box.

        name:        asset name (auto-prefixed 'MCP_A_' if not already). Created under package_path.
        static_mesh: asset path of the static mesh this foliage type instances (e.g.
                     '/Engine/BasicShapes/Cube.Cube' or a project mesh path). Required.
        density/radius/random_yaw/align_to_normal/z_offset/ground_slope_angle: optional FoliageType
                     tuning properties (applied best-effort; ignored if the property rejects the value).
        package_path: content folder for the asset (default '/Game/MCP_Scratch').

        Ledgered write (op 'create_asset'): `undo` deletes the created asset. Saved on create so the
        undo-delete is reliable. Add the type BEFORE adding its instances so LIFO undo removes the
        instances first, then deletes the now-unreferenced type."""
        params = {"name": name, "static_mesh": static_mesh, "density": density,
                  "radius": radius, "random_yaw": random_yaw, "align_to_normal": align_to_normal,
                  "z_offset": z_offset, "ground_slope_angle": ground_slope_angle,
                  "package_path": package_path}
        try:
            return json.dumps(_exec(_ADD_TYPE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_foliage_instances — place instances at explicit transforms      #
    # (shared write body; scatter_foliage_in_box reuses it)               #
    # ------------------------------------------------------------------ #
    _ADD_INSTANCES_BODY = _COERCE_HELPERS + _FOLIAGE_HELPERS + r'''
type_path = PARAMS["foliage_type_path"]
items = PARAMS.get("transforms") or []
ftype = unreal.EditorAssetLibrary.load_asset(type_path) if type_path else None
world = _fol_world()
if ftype is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "foliage_type not found: %s" % type_path}))
elif not isinstance(ftype, unreal.FoliageType):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a FoliageType: %s" % type_path}))
elif world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
elif not items:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no transforms supplied"}))
else:
    mesh = ftype.get_editor_property("mesh")
    prior_type_count = _fol_type_count(mesh)
    before_ifa = set(a.get_name() for a in _fol_ifas())
    tfs = _fol_build_transforms(items)
    cdo = unreal.InstancedFoliageActor.get_default_object()
    with unreal.ScopedEditorTransaction("MCP add_foliage_instances"):
        cdo.add_instances(world, ftype, tfs)
    after = _fol_ifas()
    after_names = [a.get_name() for a in after]
    new_ifas = [n for n in after_names if n not in before_ifa]
    created_ifa = len(new_ifas) > 0
    after_type_count = _fol_type_count(mesh)
    _ledger().append({"op": "add_foliage_instances", "foliage_type_path": type_path,
        "count": len(tfs), "created_ifa": created_ifa, "ifa_names": new_ifas,
        "prior_type_count": prior_type_count})
    print("@@UMCP@@" + json.dumps({"status": "success", "foliage_type": type_path,
        "mesh": (mesh.get_path_name() if mesh else None),
        "instances_added": len(tfs),
        "type_instance_count_before": prior_type_count,
        "type_instance_count_after": after_type_count,
        "created_ifa": created_ifa, "ifa_names": after_names,
        "undo_op": "add_foliage_instances", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_foliage_instances(ctx, foliage_type_path: str, transforms: list) -> str:
        """Place instances of an existing foliage type into the active level's InstancedFoliageActor
        (auto-created if the level has none).

        foliage_type_path: asset path of a FoliageType (e.g. created by add_foliage_type).
        transforms:        list of instance placements. Each item is either a plain [x, y, z]
                           location, or a dict {"location":[x,y,z], "rotation":[pitch,yaw,roll]
                           (optional), "scale": number or [x,y,z] (optional)}.

        Ledgered write (op 'add_foliage_instances'): `undo` removes all instances of this type
        (type-scoped) and, if this call created the level's IFA, destroys that (now-empty) IFA so
        the level is left exactly as found. Faithful because MCP_A_ foliage types are unique/single
        -use (type_instance_count_before is reported; for a fresh type it is 0)."""
        params = {"foliage_type_path": foliage_type_path, "transforms": transforms}
        try:
            return json.dumps(_exec(_ADD_INSTANCES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def scatter_foliage_in_box(ctx, foliage_type_path: str, center: list, extent: list,
                               count: int, seed: int = None, random_yaw: bool = True,
                               scale_min: float = 1.0, scale_max: float = 1.0) -> str:
        """Scatter `count` instances of a foliage type at random positions within an axis-aligned
        box (procedural-vegetation convenience). Positions are computed host-side (deterministic
        with `seed`) then placed via the same reversible path as add_foliage_instances.

        foliage_type_path: asset path of a FoliageType (e.g. created by add_foliage_type).
        center:            [x, y, z] box center in world space.
        extent:            [ex, ey, ez] half-size of the box (instances land within center +/- extent).
        count:             number of instances to scatter.
        seed:              optional RNG seed for reproducible layouts.
        random_yaw:        randomize each instance's yaw 0..360 (default True).
        scale_min/scale_max: uniform-scale range per instance (default 1.0 / 1.0).

        Ledgered write (op 'add_foliage_instances'): same inverse as add_foliage_instances."""
        try:
            if count is None or int(count) <= 0:
                return "Error: count must be a positive integer"
            rng = random.Random(seed)
            cx, cy, cz = float(center[0]), float(center[1]), float(center[2])
            ex, ey, ez = float(extent[0]), float(extent[1]), float(extent[2])
            items = []
            for _ in range(int(count)):
                loc = [cx + rng.uniform(-ex, ex), cy + rng.uniform(-ey, ey), cz + rng.uniform(-ez, ez)]
                item = {"location": loc}
                if random_yaw:
                    item["rotation"] = [0.0, rng.uniform(0.0, 360.0), 0.0]
                if scale_min != 1.0 or scale_max != 1.0:
                    item["scale"] = rng.uniform(float(scale_min), float(scale_max))
                items.append(item)
            params = {"foliage_type_path": foliage_type_path, "transforms": items}
            return json.dumps(_exec(_ADD_INSTANCES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # FOLIAGE COMPLETION — clear/remove (reversible) + read/validate      #
    # ================================================================== #
    # Extra foliage helpers (append to the reused _FOLIAGE_HELPERS). No ''' / no backslashes.
    _FOLIAGE_HELPERS2 = r'''
def _fol_tf_key(loc, rot, scl):
    return "%.2f_%.2f_%.2f_%.2f_%.2f_%.2f_%.3f_%.3f_%.3f" % (loc.x, loc.y, loc.z, rot.pitch, rot.yaw, rot.roll, scl.x, scl.y, scl.z)
def _fol_snapshot(mesh):
    # Per-component snapshot of every instance transform for foliage ISM comps of THIS mesh.
    # Keyed by component path so a before/after diff around a type-scoped remove yields EXACTLY the
    # removed (target-type) instances -- correct even when two FoliageTypes share the same mesh
    # (each FoliageType has its own component; no Python type back-ref exists on the component).
    mp = mesh.get_path_name() if mesh else None
    snap = {}
    for a in _fol_ifas():
        for c in (a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
            try:
                sm = c.get_editor_property("static_mesh")
            except Exception:
                sm = None
            if sm is None or mp is None or sm.get_path_name() != mp:
                continue
            lst = []
            try:
                cnt = c.get_instance_count()
            except Exception:
                cnt = 0
            for i in range(cnt):
                try:
                    t = c.get_instance_transform(i, True)
                except Exception:
                    continue
                loc = t.translation; scl = t.scale3d; rot = t.rotation.rotator()
                lst.append({"location": [loc.x, loc.y, loc.z],
                            "rotation": [rot.pitch, rot.yaw, rot.roll],
                            "scale": [scl.x, scl.y, scl.z],
                            "_k": _fol_tf_key(loc, rot, scl)})
            snap[c.get_path_name()] = lst
    return snap
def _fol_removed(before, after):
    out = []
    for key, lst in before.items():
        akeys = set(x["_k"] for x in after.get(key, []))
        for it in lst:
            if it["_k"] not in akeys:
                out.append({"location": it["location"], "rotation": it["rotation"], "scale": it["scale"]})
    return out
def _fol_types_in_level():
    info = {}
    for a in _fol_ifas():
        an = a.get_name()
        for c in (a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
            try:
                sm = c.get_editor_property("static_mesh")
            except Exception:
                sm = None
            mp = sm.get_path_name() if sm is not None else "<none>"
            try:
                cnt = c.get_instance_count()
            except Exception:
                cnt = 0
            rec = info.get(mp)
            if rec is None:
                rec = {"static_mesh": mp, "total_instances": 0, "components": 0, "ifas": []}
                info[mp] = rec
            rec["total_instances"] += cnt
            rec["components"] += 1
            if an not in rec["ifas"]:
                rec["ifas"].append(an)
    return info
'''

    _FOL_EXT_PREFIX = _COERCE_HELPERS + _FOLIAGE_HELPERS + _FOLIAGE_HELPERS2

    # ------------------------------------------------------------------ #
    # clear_foliage_instances — remove all instances of a type (revers.)  #
    # ------------------------------------------------------------------ #
    _CLEAR_INSTANCES_BODY = _FOL_EXT_PREFIX + r'''
type_path = PARAMS["foliage_type_path"]
ftype = unreal.EditorAssetLibrary.load_asset(type_path) if type_path else None
world = _fol_world()
if ftype is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "foliage_type not found: %s" % type_path}))
elif not isinstance(ftype, unreal.FoliageType):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a FoliageType: %s" % type_path}))
elif world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    mesh = ftype.get_editor_property("mesh")
    mesh_count_before = _fol_type_count(mesh)
    before = _fol_snapshot(mesh)
    cdo = unreal.InstancedFoliageActor.get_default_object()
    with unreal.ScopedEditorTransaction("MCP clear_foliage_instances"):
        cdo.remove_all_instances(world, ftype)
    after = _fol_snapshot(mesh)
    removed = _fol_removed(before, after)
    mesh_count_after = _fol_type_count(mesh)
    if not removed:
        print("@@UMCP@@" + json.dumps({"status": "success", "foliage_type": type_path,
            "mesh": (mesh.get_path_name() if mesh else None), "instances_cleared": 0,
            "note": "no instances of this type in the level; nothing to clear", "ledgered": False}))
    else:
        _ledger().append({"op": "clear_foliage_instances", "foliage_type_path": type_path,
            "count": len(removed), "transforms": removed})
        print("@@UMCP@@" + json.dumps({"status": "success", "foliage_type": type_path,
            "mesh": (mesh.get_path_name() if mesh else None),
            "instances_cleared": len(removed),
            "mesh_instance_count_before": mesh_count_before,
            "mesh_instance_count_after": mesh_count_after,
            "undo_op": "clear_foliage_instances", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def clear_foliage_instances(ctx, foliage_type_path: str) -> str:
        """Remove ALL placed instances of a foliage type from the active level (type-scoped: other
        foliage types are untouched). The FoliageType asset itself is kept.

        foliage_type_path: asset path of a FoliageType whose instances to clear.

        Ledgered write (op 'clear_foliage_instances'): BEFORE removing, every instance transform
        (location/rotation/scale, world space) is captured, so `undo` re-adds them exactly via
        InstancedFoliageActor.add_instances. Clearing when there are no instances is a no-op (not
        ledgered)."""
        params = {"foliage_type_path": foliage_type_path}
        try:
            return json.dumps(_exec(_CLEAR_INSTANCES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_foliage_type — clear instances + soft-delete the type asset  #
    # ------------------------------------------------------------------ #
    _REMOVE_TYPE_BODY = _FOL_EXT_PREFIX + r'''
type_path = PARAMS["foliage_type_path"]
orig_pkg = type_path.split(".")[0]
name = orig_pkg.split("/")[-1]
trash_dir = "/Game/_MCP_Trash"
trash_pkg = trash_dir + "/" + name
ftype = unreal.EditorAssetLibrary.load_asset(type_path) if type_path else None
world = _fol_world()
if ftype is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "foliage_type not found: %s" % type_path}))
elif not isinstance(ftype, unreal.FoliageType):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a FoliageType: %s" % type_path}))
elif world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    mesh = ftype.get_editor_property("mesh")
    mesh_path = mesh.get_path_name() if mesh else None
    # capture a few tuning props for reporting (asset is soft-deleted intact, so restore = rename back)
    props = {}
    for pn in ("density", "radius", "random_yaw", "align_to_normal", "z_offset", "ground_slope_angle"):
        try:
            props[pn] = str(ftype.get_editor_property(pn))
        except Exception:
            pass
    created_trash_dir = not unreal.EditorAssetLibrary.does_directory_exist(trash_dir)
    if created_trash_dir:
        unreal.EditorAssetLibrary.make_directory(trash_dir)
    # 1) remove instances (releases refs from the level's IFA), capturing EXACTLY this type's
    #    instances via a before/after per-component diff; 2) soft-delete asset via rename to trash.
    before = _fol_snapshot(mesh)
    cdo = unreal.InstancedFoliageActor.get_default_object()
    with unreal.ScopedEditorTransaction("MCP remove_foliage_type (clear instances)"):
        cdo.remove_all_instances(world, ftype)
    after = _fol_snapshot(mesh)
    captured = _fol_removed(before, after)
    prior_count = len(captured)
    ftype = None
    moved = False
    if unreal.EditorAssetLibrary.does_asset_exist(trash_pkg):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "trash target already exists: %s (aborting to avoid overwrite)" % trash_pkg}))
    else:
        moved = unreal.EditorAssetLibrary.rename_asset(orig_pkg, trash_pkg)
        if not moved:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "soft-delete rename failed (%s -> %s); instances already cleared" % (orig_pkg, trash_pkg)}))
        else:
            _ledger().append({"op": "remove_foliage_type", "original_path": orig_pkg,
                "trash_path": trash_pkg, "created_trash_dir": created_trash_dir,
                "mesh_path": mesh_path, "count": prior_count, "transforms": captured})
            print("@@UMCP@@" + json.dumps({"status": "success", "foliage_type": orig_pkg,
                "soft_deleted_to": trash_pkg, "mesh": mesh_path,
                "instances_cleared": prior_count, "captured_props": {k: str(v) for k, v in props.items()},
                "undo_op": "remove_foliage_type", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_foliage_type(ctx, foliage_type_path: str) -> str:
        """Fully remove a foliage type: clear all its placed instances, then SOFT-DELETE the
        FoliageType asset (renamed into /Game/_MCP_Trash -- never hard-deleted).

        foliage_type_path: asset path of the FoliageType to remove.

        Ledgered write (op 'remove_foliage_type'): captures the instance transforms and the trash
        location, so `undo` renames the asset back to its original path AND re-adds every instance.
        Aborts (before deleting) if the trash target already exists."""
        params = {"foliage_type_path": foliage_type_path}
        try:
            return json.dumps(_exec(_REMOVE_TYPE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_foliage_info — READ: types + instance counts in the level       #
    # ------------------------------------------------------------------ #
    _GET_FOLIAGE_INFO_BODY = _FOL_EXT_PREFIX + r'''
world = _fol_world()
ifas = _fol_ifas()
types = _fol_types_in_level()
total = 0
for rec in types.values():
    total += rec["total_instances"]
print("@@UMCP@@" + json.dumps({"status": "success",
    "world": (world.get_name() if world else None),
    "instanced_foliage_actors": [a.get_name() for a in ifas],
    "ifa_count": len(ifas),
    "foliage_types": list(types.values()),
    "distinct_meshes": len(types),
    "total_instances": total}))
'''

    @mcp.tool()
    def get_foliage_info(ctx) -> str:
        """READ all foliage in the active level: the InstancedFoliageActor(s) present, and per
        distinct static-mesh a record {static_mesh, total_instances, components, ifas}, plus totals.

        Read-only (not ledgered)."""
        try:
            return json.dumps(_exec(_GET_FOLIAGE_INFO_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_foliage — READ: foliage health                             #
    # ------------------------------------------------------------------ #
    _VALIDATE_FOLIAGE_BODY = _FOL_EXT_PREFIX + r'''
world = _fol_world()
ifas = _fol_ifas()
types = _fol_types_in_level()
warnings = []
issues = []
empty_ifas = []
for a in ifas:
    tot = 0
    for c in (a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
        try:
            tot += c.get_instance_count()
        except Exception:
            pass
    if tot == 0:
        empty_ifas.append(a.get_name())
if empty_ifas:
    warnings.append("empty InstancedFoliageActor(s) with 0 instances: %s" % empty_ifas)
missing_mesh = [rec for rec in types.values() if rec["static_mesh"] == "<none>"]
if missing_mesh:
    issues.append("%d foliage component(s) have no static_mesh assigned" % len(missing_mesh))
zero_types = [rec["static_mesh"] for rec in types.values() if rec["total_instances"] == 0 and rec["static_mesh"] != "<none>"]
if zero_types:
    warnings.append("foliage component(s) present with 0 instances for mesh(es): %s" % zero_types)
total = 0
for rec in types.values():
    total += rec["total_instances"]
print("@@UMCP@@" + json.dumps({"status": "success",
    "world": (world.get_name() if world else None),
    "ifa_count": len(ifas), "distinct_meshes": len(types), "total_instances": total,
    "empty_ifas": empty_ifas,
    "valid": (len(issues) == 0), "issues": issues, "warnings": warnings}))
'''

    @mcp.tool()
    def validate_foliage(ctx) -> str:
        """READ / health-check foliage in the active level: flags foliage components with no
        static_mesh (hard issue), empty InstancedFoliageActors, and components with 0 instances
        (warnings). Reports IFA count, distinct meshes, and total instance count.

        Read-only (not ledgered). 'valid' is False when any hard issue is present."""
        try:
            return json.dumps(_exec(_VALIDATE_FOLIAGE_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
