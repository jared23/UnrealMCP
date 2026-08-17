"""UserTools :: Animation / Skeletal (READ)  (spec: docs/spec/animation.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset/anim editors are ever opened (no modals); nothing is mutated; no ledger. This is
the SKELETAL / animation counterpart to editor_discovery.get_mesh_info (which covers STATIC
meshes). Query convention + base64 PARAMS + Output-Log auto-capture are copied verbatim from
editor_level.py (the gold standard).

What IS reachable from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * AssetRegistry enumerates SkeletalMesh / Skeleton / AnimSequence / AnimBlueprint /
    AnimMontage / BlendSpace via class_paths=[TopLevelAssetPath("/Script/Engine", <Kind>)]
    (5.8 removed ARFilter.class_names; asset path is package_name + "." + asset_name).
  * SkeletalMesh: get_bounds(); skeleton/physics_asset/materials editor properties; num_sockets()
    + get_socket_by_index() (SkeletalMeshSocket: socket_name, bone_name, relative_*). LOD count
    via unreal.EditorSkeletalMeshLibrary.get_lod_count; per-LOD VERTEX count via
    EditorSkeletalMeshLibrary.get_num_verts.
  * Skeleton: get_reference_pose() returns an AnimPose exposing get_bone_names() (ordered by bone
    index) and get_socket_names(). Bone PARENT indices are resolved from a skeletal mesh's
    get_bone_parent() (the preview mesh from AnimationLibrary.get_skeleton_preview_mesh, or a mesh
    found via AssetRegistry that targets this skeleton, or the passed-in mesh itself).
  * AnimSequence: unreal.AnimationLibrary.get_sequence_length / get_num_frames / get_num_keys /
    get_rate_scale; target skeleton via the 'skeleton' editor property; notify events + tracks via
    get_animation_notify_event_names / get_animation_notify_track_names / get_animation_notify_events;
    curve names via get_animation_curve_names.

Known limits (reported honestly in payloads, NOT hidden):
  * SkeletalMesh TRIANGLE counts are not exposed to Python here (EditorSkeletalMeshLibrary offers
    get_num_verts but no triangle-count equivalent) -> num_triangles left null with a note.
  * Skeleton.Sockets is a PROTECTED UPROPERTY (unreadable via reflection, same as StaticMesh.Sockets
    and SkeletalMesh handled natively elsewhere). Skeleton sockets are therefore read via the
    reference-pose AnimPose.get_socket_names() (names only). For the mannequin the skeleton itself
    carries 0 sockets (the 5 sockets live on the SkeletalMesh -> get_skeletal_mesh_info surfaces
    those with full transforms).
  * AnimSequence frame RATE has no direct getter here; it is derived as num_frames / length_seconds
    (reported as frame_rate_fps with derived=True) and cross-checked with get_time_at_frame(1).
  * There is no AnimBlueprint/AnimMontage/BlendSpace deep-introspection command in this batch; they
    are enumerable via list_animation_assets. Their state graphs / blend samples are editor-only.

Implemented (all read-only):
  - list_animation_assets   (AssetRegistry enumerate the 6 skeletal/anim asset kinds)
  - get_skeletal_mesh_info  (bounds, LODs, verts, materials, physics asset, skeleton, sockets)
  - get_skeleton_info       (bone count, bone hierarchy name+parent_index, skeleton sockets)
  - get_anim_sequence_info  (length, frames/keys, derived frame rate, target skeleton, notifies)
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

# Class kinds this module enumerates (all live in /Script/Engine in UE 5.8).
_ANIM_KINDS = ["SkeletalMesh", "Skeleton", "AnimSequence", "AnimBlueprint", "AnimMontage", "BlendSpace"]


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
    _ANIM_HELPERS = r'''
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
def _vec(v):
    return [round(v.x, 4), round(v.y, 4), round(v.z, 4)] if v is not None else None
def _rot(r):
    return [round(r.pitch, 4), round(r.yaw, 4), round(r.roll, 4)] if r is not None else None
'''

    # ------------------------------------------------------------------ #
    # list_animation_assets — enumerate skeletal/anim asset kinds         #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _ANIM_HELPERS + r'''
kinds_all = ["SkeletalMesh", "Skeleton", "AnimSequence", "AnimBlueprint", "AnimMontage", "BlendSpace"]
req_kind = PARAMS.get("kind")
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 200
# resolve requested kind (case-insensitive) or use all
kinds = kinds_all
kind_note = None
if req_kind:
    match = None
    for k in kinds_all:
        if k.lower() == str(req_kind).lower():
            match = k; break
    if match is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown kind '%s'; valid: %s" % (req_kind, ", ".join(kinds_all))}))
        kinds = None
    else:
        kinds = [match]
if kinds is not None:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    result = {"status": "success", "path": package_path, "kind": (kinds[0] if req_kind else None),
              "by_kind": {}, "total": 0}
    grand = 0
    for k in kinds:
        try:
            flt = unreal.ARFilter(
                class_paths=[unreal.TopLevelAssetPath("/Script/Engine", k)],
                recursive_paths=True, package_paths=[package_path])
            assets = ar.get_assets(flt) or []
        except Exception as e:
            result["by_kind"][k] = {"count": 0, "error": str(e), "assets": []}
            continue
        rows = []
        for a in assets:
            pkg = str(a.get_editor_property("package_name"))
            nm = str(a.get_editor_property("asset_name"))
            rows.append({"name": nm, "path": pkg + "." + nm})
        rows.sort(key=lambda r: r["path"].lower())
        grand += len(rows)
        result["by_kind"][k] = {"count": len(rows),
                                "returned": min(len(rows), max_results),
                                "assets": rows[:max_results]}
    result["total"] = grand
    result["note"] = ("Enumerated via AssetRegistry class_paths=TopLevelAssetPath('/Script/Engine', "
                      "<Kind>) (UE5.8 removed ARFilter.class_names). AnimSequence excludes montages "
                      "(AnimMontage is a distinct class). Each kind lists up to max_results assets "
                      "(name + object path); 'count' is the true total for that kind under 'path'.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_animation_assets(ctx, path: str = None, kind: str = None,
                              max_results: int = 200) -> str:
        """Enumerate skeletal/animation assets via the AssetRegistry (fast; no asset load).
        Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Characters'.
        kind:        restrict to ONE of: SkeletalMesh, Skeleton, AnimSequence, AnimBlueprint,
                     AnimMontage, BlendSpace (case-insensitive). Omit to enumerate all six.
        max_results: cap the assets listed PER KIND (default 200); each kind's true 'count'
                     is always reported even if the list is capped.

        Returns per-kind {count, returned, assets:[{name, path}]} plus a grand 'total'.
        AnimSequence is a distinct class from AnimMontage, so montages are only under
        the AnimMontage kind (not double-counted)."""
        params = {"path": path, "kind": kind, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_skeletal_mesh_info — bounds/LODs/verts/materials/skeleton/sockets #
    # ------------------------------------------------------------------ #
    _SKM_BODY = _ANIM_HELPERS + r'''
path = PARAMS.get("path")
include_sockets = PARAMS.get("include_sockets")
include_sockets = True if include_sockets is None else bool(include_sockets)
m, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(m, unreal.SkeletalMesh):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a SkeletalMesh (got %s): %s" % (m.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": m.get_path_name(), "class": m.get_class().get_name()}
    b = _try(lambda: m.get_bounds())
    if b is not None:
        be = b.box_extent; og = b.origin
        info["bounds"] = {"box_extent": _vec(be), "origin": _vec(og),
                          "sphere_radius": _try(lambda: round(b.sphere_radius, 4))}
    # LOD count + per-LOD vertex counts via EditorSkeletalMeshLibrary
    esml = getattr(unreal, "EditorSkeletalMeshLibrary", None)
    lod_count = _try(lambda: esml.get_lod_count(m)) if esml else None
    info["num_lods"] = lod_count
    lods = []
    if esml and isinstance(lod_count, int):
        for i in range(lod_count):
            lods.append({"lod": i, "num_vertices": _try(lambda: esml.get_num_verts(m, i))})
    info["lods"] = lods
    info["num_vertices_lod0"] = (lods[0]["num_vertices"] if lods else None)
    info["num_triangles_lod0"] = None
    info["triangles_note"] = ("Triangle counts are not exposed to Python in this build "
                              "(EditorSkeletalMeshLibrary provides get_num_verts but no triangle "
                              "equivalent); num_triangles left null.")
    # skeleton + physics asset (editor properties)
    sk = _try(lambda: m.get_editor_property("skeleton"))
    info["skeleton"] = _try(lambda: sk.get_path_name()) if sk else None
    pa = _try(lambda: m.get_editor_property("physics_asset"))
    info["physics_asset"] = _try(lambda: pa.get_path_name()) if pa else None
    spa = _try(lambda: m.get_editor_property("shadow_physics_asset"))
    info["shadow_physics_asset"] = _try(lambda: spa.get_path_name()) if spa else None
    # material slots (SkeletalMaterial structs: material_slot_name + material_interface)
    slots = []
    mats = _try(lambda: m.get_editor_property("materials"), []) or []
    for i, sm in enumerate(mats):
        mi = _try(lambda: sm.get_editor_property("material_interface"))
        slots.append({"index": i,
                      "slot_name": str(_try(lambda: sm.get_editor_property("material_slot_name"))),
                      "material": (mi.get_path_name() if mi else None)})
    info["num_material_slots"] = len(slots)
    info["material_slots"] = slots
    # sockets on the skeletal mesh (SkeletalMeshSocket with full relative transform)
    if include_sockets:
        n = _try(lambda: m.num_sockets(), 0) or 0
        socks = []
        for i in range(int(n)):
            s = _try(lambda: m.get_socket_by_index(i))
            if s is None:
                continue
            socks.append({
                "name": str(_try(lambda: s.get_editor_property("socket_name"))),
                "bone": str(_try(lambda: s.get_editor_property("bone_name"))),
                "relative_location": _vec(_try(lambda: s.get_editor_property("relative_location"))),
                "relative_rotation": _rot(_try(lambda: s.get_editor_property("relative_rotation"))),
                "relative_scale": _vec(_try(lambda: s.get_editor_property("relative_scale"))),
            })
        info["socket_count"] = len(socks)
        info["sockets"] = socks
        info["sockets_source"] = "SkeletalMesh.num_sockets/get_socket_by_index"
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_skeletal_mesh_info(ctx, path: str, include_sockets: bool = True) -> str:
        """Native info for a SkeletalMesh asset (loads it; no editor opened). Read-only.

        path:            SkeletalMesh asset path, e.g.
                         '/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple.SKM_Manny_Simple'.
        include_sockets: include the mesh's sockets (default True).

        Returns: bounds (box_extent, origin, sphere_radius); num_lods and per-LOD vertex counts
        (via EditorSkeletalMeshLibrary); the referenced Skeleton and (shadow) physics asset paths;
        material slots (index, slot_name, current material); and sockets (name, bone, relative
        location/rotation/scale). NOTE: triangle counts are not reachable from Python in this build
        (num_triangles is null with a note). Errors if the asset is missing or not a SkeletalMesh."""
        params = {"path": path, "include_sockets": include_sockets}
        try:
            return json.dumps(_exec(_SKM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_skeleton_info — bone count, hierarchy, skeleton sockets          #
    # ------------------------------------------------------------------ #
    _SKEL_BODY = _ANIM_HELPERS + r'''
path = PARAMS.get("path")
include_hierarchy = PARAMS.get("include_hierarchy")
include_hierarchy = True if include_hierarchy is None else bool(include_hierarchy)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    # Resolve the Skeleton and (optionally) a SkeletalMesh to read bone parents from.
    skeleton = None
    mesh = None
    if isinstance(obj, unreal.Skeleton):
        skeleton = obj
    elif isinstance(obj, unreal.SkeletalMesh):
        mesh = obj
        skeleton = _try(lambda: obj.get_editor_property("skeleton"))
    if skeleton is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a Skeleton or SkeletalMesh (got %s): %s" % (obj.get_class().get_name(), path)}))
    else:
        result = {"status": "success", "skeleton": skeleton.get_path_name(),
                  "given_asset": obj.get_path_name(), "given_class": obj.get_class().get_name()}
        # reference pose -> ordered bone names + socket names
        rp = _try(lambda: skeleton.get_reference_pose())
        bones = _try(lambda: [str(b) for b in rp.get_bone_names()]) if rp is not None else None
        result["bone_count"] = len(bones) if isinstance(bones, list) else None
        result["skeleton_socket_names"] = _try(lambda: [str(s) for s in rp.get_socket_names()], []) if rp is not None else []
        result["sockets_note"] = ("Skeleton.Sockets is a protected UPROPERTY (unreadable via "
                                  "reflection); skeleton socket NAMES are read from the reference-pose "
                                  "AnimPose. Mesh-attached sockets (with transforms) are on the "
                                  "SkeletalMesh -> see get_skeletal_mesh_info.")
        # bone hierarchy (name + parent_index): needs a skeletal mesh's get_bone_parent.
        if include_hierarchy and isinstance(bones, list):
            src_mesh = mesh
            mesh_source = "passed-in SkeletalMesh"
            if src_mesh is None:
                pm = _try(lambda: unreal.AnimationLibrary.get_skeleton_preview_mesh(skeleton))
                if pm is not None:
                    src_mesh = pm; mesh_source = "AnimationLibrary.get_skeleton_preview_mesh"
            if src_mesh is None:
                # last resort: find any SkeletalMesh in /Game whose skeleton is this one
                try:
                    ar = unreal.AssetRegistryHelpers.get_asset_registry()
                    flt = unreal.ARFilter(
                        class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "SkeletalMesh")],
                        recursive_paths=True, package_paths=["/Game"])
                    for a in (ar.get_assets(flt) or []):
                        cand = _try(lambda: unreal.EditorAssetLibrary.load_asset(
                            str(a.get_editor_property("package_name")) + "." + str(a.get_editor_property("asset_name"))))
                        csk = _try(lambda: cand.get_editor_property("skeleton")) if cand else None
                        if csk is not None and csk.get_path_name() == skeleton.get_path_name():
                            src_mesh = cand; mesh_source = "AssetRegistry SkeletalMesh referencing this skeleton"
                            break
                except Exception:
                    src_mesh = None
            if src_mesh is not None:
                idx = {b: i for i, b in enumerate(bones)}
                hierarchy = []
                for i, b in enumerate(bones):
                    p = _try(lambda: str(src_mesh.get_bone_parent(b)))
                    pidx = idx.get(p, -1) if (p and p != "None") else -1
                    hierarchy.append({"index": i, "name": b,
                                      "parent_index": pidx,
                                      "parent": (p if (p and p != "None") else None)})
                result["bone_hierarchy"] = hierarchy
                result["hierarchy_source"] = mesh_source
            else:
                result["bone_hierarchy"] = None
                result["hierarchy_note"] = ("Bone NAMES/COUNT come from the skeleton reference pose, but "
                                            "parent indices need a SkeletalMesh's get_bone_parent and no "
                                            "mesh (preview or referencing) was found for this skeleton.")
                result["bone_names"] = bones
        elif isinstance(bones, list):
            result["bone_names"] = bones
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_skeleton_info(ctx, path: str, include_hierarchy: bool = True) -> str:
        """Bone + socket info for a Skeleton (or the skeleton of a SkeletalMesh). Read-only.

        path:              a Skeleton asset path, OR a SkeletalMesh path (its skeleton is used),
                           e.g. '/Game/Characters/Mannequins/Meshes/SK_Mannequin.SK_Mannequin'.
        include_hierarchy: include the full bone hierarchy (name + parent_index). Default True.

        Returns bone_count and (with include_hierarchy) bone_hierarchy: an index-ordered list of
        {index, name, parent_index, parent} (parent_index -1 == root). Bone names come from the
        skeleton's reference pose (AnimPose.get_bone_names); parent indices are resolved from a
        SkeletalMesh's get_bone_parent (the passed-in mesh, the skeleton's preview mesh, or a mesh
        found via AssetRegistry that targets this skeleton). skeleton_socket_names lists sockets on
        the SKELETON (its Sockets UPROPERTY is protected, so names are read from the reference pose;
        mesh-attached sockets with transforms are on the SkeletalMesh via get_skeletal_mesh_info)."""
        params = {"path": path, "include_hierarchy": include_hierarchy}
        try:
            return json.dumps(_exec(_SKEL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_anim_sequence_info — length, frames/keys, rate, skeleton, notifies #
    # ------------------------------------------------------------------ #
    _ANIM_SEQ_BODY = _ANIM_HELPERS + r'''
path = PARAMS.get("path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.AnimSequence):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an AnimSequence (got %s): %s. Montages/BlendSpaces are distinct classes." % (obj.get_class().get_name(), path)}))
else:
    al = unreal.AnimationLibrary
    info = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name()}
    length = _try(lambda: al.get_sequence_length(obj))
    num_frames = _try(lambda: al.get_num_frames(obj))
    num_keys = _try(lambda: al.get_num_keys(obj))
    info["length_seconds"] = round(length, 6) if isinstance(length, (int, float)) else length
    info["num_frames"] = num_frames
    info["num_keys"] = num_keys
    info["rate_scale"] = _try(lambda: al.get_rate_scale(obj))
    # frame rate: no direct getter -> derive from frames/length, cross-check via get_time_at_frame(1)
    fps = None
    if isinstance(num_frames, int) and isinstance(length, (int, float)) and length > 0:
        fps = round(num_frames / length, 4)
    ft = _try(lambda: al.get_time_at_frame(obj, 1))
    fps_check = round(1.0 / ft, 4) if isinstance(ft, (int, float)) and ft > 0 else None
    info["frame_rate_fps"] = fps if fps is not None else fps_check
    info["frame_rate_derived"] = True
    info["frame_rate_note"] = ("No direct frame-rate getter in this build; frame_rate_fps is derived "
                               "as num_frames/length_seconds (cross-checked with get_time_at_frame(1)).")
    info["frame_rate_crosscheck_fps"] = fps_check
    # target skeleton
    sk = _try(lambda: obj.get_editor_property("skeleton"))
    info["target_skeleton"] = _try(lambda: sk.get_path_name()) if sk else None
    # additive flag (best-effort)
    info["additive_anim_type"] = str(_try(lambda: obj.get_editor_property("additive_anim_type")))
    # notifies
    notify_names = _try(lambda: [str(x) for x in al.get_animation_notify_event_names(obj)], []) or []
    notify_tracks = _try(lambda: [str(x) for x in al.get_animation_notify_track_names(obj)], []) or []
    notify_events = _try(lambda: al.get_animation_notify_events(obj), []) or []
    info["notify_count"] = len(notify_events) if notify_events is not None else len(notify_names)
    info["notify_event_names"] = notify_names
    info["notify_track_names"] = notify_tracks
    # curves
    curves = []
    rct = getattr(unreal, "RawCurveTrackTypes", None)
    if rct is not None:
        curves = _try(lambda: [str(x) for x in al.get_animation_curve_names(obj, rct.RCT_FLOAT)], []) or []
    info["float_curve_count"] = len(curves)
    info["float_curve_names"] = curves
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_anim_sequence_info(ctx, path: str) -> str:
        """Info for an AnimSequence asset (loads it; no editor opened). Read-only.

        path: AnimSequence asset path, e.g.
              '/Game/Characters/Mannequins/Anims/Unarmed/MM_Idle.MM_Idle'.

        Returns: length_seconds, num_frames, num_keys, rate_scale, a DERIVED frame_rate_fps
        (num_frames/length, cross-checked via get_time_at_frame(1); no direct getter exists here),
        the target_skeleton path, additive_anim_type, notify_count with notify_event_names and
        notify_track_names, and float curve names. Errors if the asset is missing or not an
        AnimSequence (AnimMontage / BlendSpace are distinct classes — see list_animation_assets)."""
        try:
            return json.dumps(_exec(_ANIM_SEQ_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
