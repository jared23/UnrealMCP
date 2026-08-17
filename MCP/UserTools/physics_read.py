"""UserTools :: Physics (READ)  (spec: docs/spec/physics.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset editors are opened (no modals); nothing is mutated; no ledger. Query convention +
base64 PARAMS + Output-Log auto-capture are copied verbatim from editor_level.py (the gold
standard), matching the AssetRegistry-enumeration style of animation_read.py.

What IS reachable from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * AssetRegistry enumerates PhysicsAsset / PhysicalMaterial via
    class_paths=[TopLevelAssetPath("/Script/Engine", <Kind>)] (5.8 removed ARFilter.class_names;
    asset path = package_name + "." + asset_name).
  * PhysicsAsset.get_constraints(includes_terminated) -> Array of ConstraintInstanceAccessor.
    ConstraintInstanceBlueprintLibrary reads each accessor:
      - get_attached_body_names(acc) -> (acc, Bone1:Name, Bone2:Name)   [the joint's two bones]
      - get_angular_limits(acc) -> (acc, swing1_motion, swing1_angle, swing2_motion,
                                    swing2_angle, twist_motion, twist_angle)
      - get_linear_limits(acc)  -> (acc, x_motion, y_motion, z_motion, limit)
    (UE BP libraries return the accessor itself as the FIRST tuple element; real values follow.)
  * PhysicalMaterial exposes friction / static_friction / restitution / density /
    friction_combine_mode / restitution_combine_mode / surface_type / raise_mass_to_power /
    sleep_*_velocity_threshold / strength as reflected editor properties.
  * Collision profiles/channels: the live UCollisionProfile UObject is NOT exposed to Python
    (unreal.CollisionProfile does not exist; there is no get_default_object route), so named
    profiles are parsed from the merged config INIs ([/Script/Engine.CollisionProfile] in
    BaseEngine.ini + any DefaultEngine.ini overrides) and the built-in ECollisionChannel enum.

Known limits (reported honestly in payloads, NOT hidden):
  * PhysicsAsset PHYSICS BODIES are NOT enumerable from Python here. SkeletalBodySetups (the body
    array), DefaultSkelMesh and PreviewSkeletalMesh are PROTECTED UPROPERTYs (empty reflection
    flags; get_editor_property refuses them), and PhysicsAsset exposes no body accessor method.
    Therefore per-body geometry primitive counts (sphere/box/capsule/convex) are UNREACHABLE.
    get_physics_asset_info reports the body-bearing bones DERIVED from the constraint graph (the
    union of every joint's two bones) as an approximation, clearly flagged, plus a reliable
    constraint count/list. A future C++ MCPReflectionLibrary.get_physics_bodies_json(UPhysicsAsset)
    handler (same pattern as get_struct_fields_json / get_static_mesh_sockets_json) would expose
    real bodies + agg-geom primitives.
  * The PhysicsAsset's target SkeletalMesh/Skeleton are read by REVERSE LOOKUP (a SkeletalMesh
    whose physics_asset property points back at this PA), since DefaultSkelMesh is protected.
  * This project defines ZERO PhysicalMaterial assets under /Game and none are AssetRegistry-
    indexed even engine-wide; get_physical_material_info is demonstrated on the always-loadable
    /Engine/EngineMaterials/DefaultPhysicalMaterial.

Implemented (all read-only):
  - list_physics_assets       (AssetRegistry enumerate PhysicsAsset)
  - get_physics_asset_info    (target mesh/skeleton, solver type, constraint list, derived bodies)
  - list_physical_materials   (AssetRegistry enumerate PhysicalMaterial)
  - get_physical_material_info (friction/restitution/density/combine modes/surface type/...)
  - list_collision_profiles   (named profiles + descriptions from config INIs + channel enum)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _PHYS_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load(path):
    if not path:
        return None, "no asset path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    return obj, None
def _enum(v):
    if v is None:
        return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _num(v):
    if isinstance(v, bool) or v is None:
        return v
    try:
        return round(float(v), 6)
    except Exception:
        return str(v)
'''

    # ------------------------------------------------------------------ #
    # list_physics_assets — enumerate PhysicsAsset via AssetRegistry      #
    # ------------------------------------------------------------------ #
    _LIST_PA_BODY = _PHYS_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 500
ar = unreal.AssetRegistryHelpers.get_asset_registry()
flt = unreal.ARFilter(
    class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "PhysicsAsset")],
    recursive_paths=True, package_paths=[package_path])
assets = _try(lambda: ar.get_assets(flt), []) or []
rows = []
for a in assets:
    pkg = str(a.get_editor_property("package_name"))
    nm = str(a.get_editor_property("asset_name"))
    rows.append({"name": nm, "path": pkg + "." + nm})
rows.sort(key=lambda r: r["path"].lower())
result = {"status": "success", "path": package_path, "count": len(rows),
          "returned": min(len(rows), max_results), "assets": rows[:max_results],
          "note": ("Enumerated via AssetRegistry class_paths=TopLevelAssetPath('/Script/Engine', "
                   "'PhysicsAsset'). 'count' is the true total under 'path'.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_physics_assets(ctx, path: str = None, max_results: int = 500) -> str:
        """Enumerate PhysicsAsset assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Characters'.
        max_results: cap the assets listed (default 500); the true 'count' is always reported.

        Returns {count, returned, assets:[{name, path}]}. A PhysicsAsset (e.g. PA_Mannequin)
        holds the ragdoll/collision bodies + joints for a SkeletalMesh — feed a path to
        get_physics_asset_info for its constraint graph."""
        params = {"path": path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_PA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_physics_asset_info — target mesh/skeleton, solver, constraints   #
    # ------------------------------------------------------------------ #
    _PA_INFO_BODY = _PHYS_HELPERS + r'''
path = PARAMS.get("path")
max_constraints = PARAMS.get("max_constraints")
max_constraints = int(max_constraints) if max_constraints else 500
pa, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(pa, unreal.PhysicsAsset):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a PhysicsAsset (got %s): %s" % (pa.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": pa.get_path_name(), "class": pa.get_class().get_name()}
    # solver type is a readable editor property
    info["solver_type"] = _enum(_try(lambda: pa.get_editor_property("solver_type")))
    # Target SkeletalMesh/Skeleton: DefaultSkelMesh is a PROTECTED UPROPERTY -> reverse lookup a
    # SkeletalMesh whose physics_asset points back at this PA (AssetRegistry, /Game).
    target_mesh = None
    target_skeleton = None
    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        mflt = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "SkeletalMesh")],
            recursive_paths=True, package_paths=["/Game"])
        for a in (ar.get_assets(mflt) or []):
            mp = str(a.get_editor_property("package_name")) + "." + str(a.get_editor_property("asset_name"))
            m = _try(lambda: unreal.EditorAssetLibrary.load_asset(mp))
            mpa = _try(lambda: m.get_editor_property("physics_asset")) if m else None
            if mpa is not None and mpa.get_path_name() == pa.get_path_name():
                target_mesh = mp
                sk = _try(lambda: m.get_editor_property("skeleton"))
                target_skeleton = sk.get_path_name() if sk else None
                break
    except Exception:
        pass
    info["target_skeletal_mesh"] = target_mesh
    info["target_skeleton"] = target_skeleton
    info["target_source"] = ("reverse lookup: a /Game SkeletalMesh whose physics_asset == this PA "
                             "(DefaultSkelMesh/PreviewSkeletalMesh are protected UPROPERTYs)")
    # Constraints (fully reachable): each is a ConstraintInstanceAccessor read via
    # ConstraintInstanceBlueprintLibrary. Tuple returns lead with the accessor itself.
    cibl = unreal.ConstraintInstanceBlueprintLibrary
    cons = _try(lambda: pa.get_constraints(False), []) or []
    clist = []
    body_bones = []
    seen = set()
    for c in cons:
        rec = {}
        nm = _try(lambda: cibl.get_attached_body_names(c))
        b1 = str(nm[1]) if nm is not None and len(nm) > 2 else None
        b2 = str(nm[2]) if nm is not None and len(nm) > 2 else None
        rec["bone1"] = b1
        rec["bone2"] = b2
        for b in (b1, b2):
            if b and b != "None" and b not in seen:
                seen.add(b); body_bones.append(b)
        al = _try(lambda: cibl.get_angular_limits(c))
        if al is not None and len(al) >= 7:
            rec["angular"] = {"swing1_motion": _enum(al[1]), "swing1_limit_deg": _num(al[2]),
                              "swing2_motion": _enum(al[3]), "swing2_limit_deg": _num(al[4]),
                              "twist_motion": _enum(al[5]), "twist_limit_deg": _num(al[6])}
        ll = _try(lambda: cibl.get_linear_limits(c))
        if ll is not None and len(ll) >= 5:
            rec["linear"] = {"x_motion": _enum(ll[1]), "y_motion": _enum(ll[2]),
                             "z_motion": _enum(ll[3]), "limit": _num(ll[4])}
        clist.append(rec)
        if len(clist) >= max_constraints:
            break
    info["constraint_count"] = len(cons)
    info["constraints_returned"] = len(clist)
    info["constraints"] = clist
    info["constraints_source"] = "PhysicsAsset.get_constraints + ConstraintInstanceBlueprintLibrary"
    # derived_body_bones is the constraint-graph approximation, retained in BOTH paths for reference.
    info["derived_body_bone_count"] = len(body_bones)
    info["derived_body_bones"] = body_bones
    _bodies_fallback_note = ("PhysicsAsset physics BODIES are not enumerable from Python in this build: "
                             "SkeletalBodySetups is a protected UPROPERTY and PhysicsAsset exposes no "
                             "body accessor. 'derived_body_bones' is the UNION of every constraint's two "
                             "bones (an approximation of the body set; the root body and any body with no "
                             "joint are not captured). Per-body geometry primitive counts "
                             "(sphere/box/capsule/convex) are UNREACHABLE without a C++ handler "
                             "(MCPReflectionLibrary.get_physics_bodies_json).")
    # Bodies: prefer the native C++ handler (reaches the PROTECTED SkeletalBodySetups UPROPERTY and
    # returns real bodies + per-primitive agg-geom). Same hasattr-guarded pattern as get_mesh_info's
    # get_static_mesh_sockets_json wiring: C++ path authoritative, prior derived approximation kept as
    # honest fallback when the native binding is absent or returns unparseable output.
    _mrl = getattr(unreal, "MCPReflectionLibrary", None)
    if _mrl is not None and hasattr(_mrl, "get_physics_bodies_json"):
        _bjs = _try(lambda: unreal.MCPReflectionLibrary.get_physics_bodies_json(pa))
        _bd = _try(lambda: json.loads(_bjs)) if _bjs else None
        if isinstance(_bd, dict) and isinstance(_bd.get("bodies"), list):
            _bodies = _bd.get("bodies")
            info["bodies_reachable"] = True
            info["bodies_source"] = "MCPReflectionLibrary"
            info["body_count"] = _bd.get("body_count", len(_bodies))
            info["bodies"] = _bodies
            info["bodies_note"] = ("Real physics bodies with per-primitive agg-geom (sphere/box/capsule/"
                                   "convex/tapered_capsule + geometry) via the native "
                                   "MCPReflectionLibrary.get_physics_bodies_json handler, which reaches "
                                   "the protected SkeletalBodySetups UPROPERTY. 'derived_body_bones' (the "
                                   "constraint-graph approximation) is retained above for reference.")
        else:
            # Helper present but returned unparseable/empty output -> fall back honestly.
            info["bodies_reachable"] = False
            info["bodies_source"] = "unavailable"
            info["bodies_note"] = _bodies_fallback_note
    else:
        # Native handler absent (module still loads/works) -> honest derived-only fallback.
        info["bodies_reachable"] = False
        info["bodies_source"] = "unavailable"
        info["bodies_note"] = _bodies_fallback_note
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_physics_asset_info(ctx, path: str, max_constraints: int = 500) -> str:
        """Info for a PhysicsAsset (loads it; no editor opened). Read-only.

        path:            PhysicsAsset path, e.g.
                         '/Game/Characters/Mannequins/Rigs/PA_Mannequin.PA_Mannequin'.
        max_constraints: cap the constraints listed (default 500); constraint_count is always exact.

        Returns: solver_type; target_skeletal_mesh + target_skeleton (found by reverse lookup, since
        the PA's DefaultSkelMesh is a protected property); constraint_count and a per-constraint list
        {bone1, bone2, angular:{swing1/swing2/twist motion+limit_deg}, linear:{x/y/z motion, limit}};
        and derived_body_bones (the union of all joint bones, an APPROXIMATION of the physics bodies).

        PHYSICS BODIES: when the native MCPReflectionLibrary.get_physics_bodies_json handler is present,
        returns the REAL bodies reached from the PROTECTED SkeletalBodySetups UPROPERTY —
        bodies_reachable=true, bodies_source='MCPReflectionLibrary', body_count, and bodies:[{bone,
        sphere_count/box_count/capsule_count/convex_count/tapered_capsule_count, primitives:[per-shape
        geometry: sphere{center,radius} | box{center,extent} | capsule{center,radius,length} |
        convex{vertex_count}]}]. If that native helper is unavailable it degrades honestly to the prior
        behavior (bodies_reachable=false, bodies_source='unavailable', derived_body_bones only; see
        bodies_note). Errors if the asset is missing or not a PhysicsAsset."""
        params = {"path": path, "max_constraints": max_constraints}
        try:
            return json.dumps(_exec(_PA_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_physical_materials — enumerate PhysicalMaterial via AssetRegistry #
    # ------------------------------------------------------------------ #
    _LIST_PM_BODY = _PHYS_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 500
ar = unreal.AssetRegistryHelpers.get_asset_registry()
flt = unreal.ARFilter(
    class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "PhysicalMaterial")],
    recursive_paths=True, package_paths=[package_path])
assets = _try(lambda: ar.get_assets(flt), []) or []
rows = []
for a in assets:
    pkg = str(a.get_editor_property("package_name"))
    nm = str(a.get_editor_property("asset_name"))
    rows.append({"name": nm, "path": pkg + "." + nm})
rows.sort(key=lambda r: r["path"].lower())
result = {"status": "success", "path": package_path, "count": len(rows),
          "returned": min(len(rows), max_results), "assets": rows[:max_results],
          "engine_default": "/Engine/EngineMaterials/DefaultPhysicalMaterial.DefaultPhysicalMaterial",
          "note": ("Enumerated via AssetRegistry class_paths=TopLevelAssetPath('/Script/Engine', "
                   "'PhysicalMaterial'). NOTE: the engine's built-in DefaultPhysicalMaterial is NOT "
                   "AssetRegistry-indexed but is always loadable directly (see 'engine_default') and "
                   "readable via get_physical_material_info.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_physical_materials(ctx, path: str = None, max_results: int = 500) -> str:
        """Enumerate PhysicalMaterial assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game'.
        max_results: cap the assets listed (default 500); the true 'count' is always reported.

        Returns {count, returned, assets:[{name, path}]} plus 'engine_default' (the always-loadable
        /Engine DefaultPhysicalMaterial). NOTE: engine built-in physical materials are not
        AssetRegistry-indexed, so a project with no custom ones reports count 0 — get_physical_material_info
        still works on the engine default path."""
        params = {"path": path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_PM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_physical_material_info — friction/restitution/density/etc.       #
    # ------------------------------------------------------------------ #
    _PM_INFO_BODY = _PHYS_HELPERS + r'''
path = PARAMS.get("path")
pm, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(pm, unreal.PhysicalMaterial):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a PhysicalMaterial (got %s): %s" % (pm.get_class().get_name(), path)}))
else:
    def gp(name):
        return pm.get_editor_property(name)
    info = {"status": "success", "path": pm.get_path_name(), "class": pm.get_class().get_name()}
    info["friction"] = _num(_try(lambda: gp("friction")))
    info["static_friction"] = _num(_try(lambda: gp("static_friction")))
    info["restitution"] = _num(_try(lambda: gp("restitution")))
    info["density"] = _num(_try(lambda: gp("density")))
    info["friction_combine_mode"] = _enum(_try(lambda: gp("friction_combine_mode")))
    info["restitution_combine_mode"] = _enum(_try(lambda: gp("restitution_combine_mode")))
    info["override_friction_combine_mode"] = _try(lambda: bool(gp("override_friction_combine_mode")))
    info["override_restitution_combine_mode"] = _try(lambda: bool(gp("override_restitution_combine_mode")))
    info["surface_type"] = _enum(_try(lambda: gp("surface_type")))
    info["raise_mass_to_power"] = _num(_try(lambda: gp("raise_mass_to_power")))
    info["strength_class"] = _try(lambda: gp("strength").get_class().get_name())
    info["sleep_linear_velocity_threshold"] = _num(_try(lambda: gp("sleep_linear_velocity_threshold")))
    info["sleep_angular_velocity_threshold"] = _num(_try(lambda: gp("sleep_angular_velocity_threshold")))
    info["sleep_counter_threshold"] = _num(_try(lambda: gp("sleep_counter_threshold")))
    dc = _try(lambda: gp("debug_color"))
    info["debug_color"] = ([dc.r, dc.g, dc.b, dc.a] if dc is not None else None)
    info["note"] = ("Values via reflected editor properties on UPhysicalMaterial. 'strength' is a "
                    "PhysicalMaterialStrength sub-object; sleep thresholds gate simulation sleep.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_physical_material_info(ctx, path: str) -> str:
        """Info for a PhysicalMaterial asset (loads it; no editor opened). Read-only.

        path: PhysicalMaterial path, e.g. the engine default
              '/Engine/EngineMaterials/DefaultPhysicalMaterial.DefaultPhysicalMaterial'.

        Returns friction, static_friction, restitution, density, friction/restitution combine modes
        (+ their override flags), surface_type, raise_mass_to_power, strength, the sleep velocity/
        counter thresholds, and debug_color. Errors if the asset is missing or not a PhysicalMaterial."""
        try:
            return json.dumps(_exec(_PM_INFO_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_collision_profiles — named profiles + channels from config      #
    # ------------------------------------------------------------------ #
    _COLLISION_BODY = _PHYS_HELPERS + r'''
import os
def _qval(s, key):
    k = key + "=" + chr(34)
    i = s.find(k)
    if i < 0:
        return None
    i += len(k)
    j = s.find(chr(34), i)
    return s[i:j] if j >= i else None
def _uval(s, key):
    k = key + "="
    i = s.find(k)
    if i < 0:
        return None
    i += len(k)
    j = i
    while j < len(s) and s[j] not in ",)":
        j += 1
    return s[i:j]
def _parse_section(text):
    profiles = []
    channels = []
    insec = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            insec = (s == "[/Script/Engine.CollisionProfile]")
            continue
        if not insec:
            continue
        if s.startswith("+Profiles=") or s.startswith("Profiles="):
            profiles.append({"name": _qval(s, "Name"),
                             "collision_enabled": _uval(s, "CollisionEnabled"),
                             "object_type": _qval(s, "ObjectTypeName"),
                             "description": _qval(s, "HelpMessage")})
        elif s.startswith("+DefaultChannelResponses=") or s.startswith("DefaultChannelResponses="):
            channels.append({"channel": _uval(s, "Channel"),
                             "name": _qval(s, "Name"),
                             "default_response": _uval(s, "DefaultResponse"),
                             "trace_type": _uval(s, "bTraceType")})
    return profiles, channels
result = {"status": "success"}
result["uobject_note"] = ("The live UCollisionProfile UObject is not exposed to Python (unreal."
                          "CollisionProfile does not exist; no get_default_object route). Named "
                          "profiles/channels are parsed from the merged config INIs, which is the "
                          "reachable equivalent.")
sources = []
all_profiles = {}
order = []
custom_channels = []
edir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_config_dir())
pdir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir())
for label, fp in [("engine:BaseEngine.ini", os.path.join(edir, "BaseEngine.ini")),
                  ("project:DefaultEngine.ini", os.path.join(pdir, "DefaultEngine.ini"))]:
    if os.path.exists(fp):
        txt = open(fp, "r", encoding="utf-8", errors="replace").read()
        profs, chans = _parse_section(txt)
        sources.append({"source": label, "profiles_found": len(profs), "channels_found": len(chans)})
        for p in profs:
            nm = p.get("name")
            if not nm:
                continue
            if nm not in all_profiles:
                order.append(nm)
            all_profiles[nm] = p  # later (project) source overrides
        for c in chans:
            custom_channels.append(c)
result["sources"] = sources
result["profiles"] = [all_profiles[n] for n in order]
result["profile_count"] = len(order)
result["custom_channels"] = custom_channels
result["custom_channel_count"] = len(custom_channels)
# built-in engine trace/object channels from the ECollisionChannel enum
builtins_ch = []
cc = getattr(unreal, "CollisionChannel", None)
if cc is not None:
    for a in dir(cc):
        if a.startswith("ECC_"):
            builtins_ch.append(a)
result["builtin_channels"] = builtins_ch
result["note"] = ("Named collision profiles + descriptions parsed from [/Script/Engine.CollisionProfile] "
                  "in BaseEngine.ini (engine defaults) merged with DefaultEngine.ini (project overrides, "
                  "if any). 'custom_channels' are project/engine-declared trace/object channels; "
                  "'builtin_channels' are the fixed ECollisionChannel enum members.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_collision_profiles(ctx) -> str:
        """List the project's collision profiles + channels. Read-only.

        The live UCollisionProfile UObject is NOT exposed to Python in this build, so named profiles
        are parsed from the merged config: [/Script/Engine.CollisionProfile] in the engine's
        BaseEngine.ini (engine defaults, e.g. NoCollision/BlockAll/Pawn/PhysicsActor with their
        HelpMessage descriptions) plus any DefaultEngine.ini project overrides.

        Returns profiles:[{name, collision_enabled, object_type, description}], custom_channels
        (project/engine-declared trace+object channels), builtin_channels (the fixed
        ECollisionChannel enum), and 'sources' (which INI each came from). This project defines no
        custom collision profiles, so it reports the engine baseline."""
        try:
            return json.dumps(_exec(_COLLISION_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
