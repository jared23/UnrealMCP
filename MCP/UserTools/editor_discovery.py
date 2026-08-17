"""UserTools :: Editor / Discovery  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem + reflection,
UE 5.8). Follows the editor_level.py gold-standard conventions verbatim: snippets print
@@UMCP@@<json> on one line; params are injected as base64 JSON via _exec; _query wraps
every snippet with Output-Log auto-capture and surfaces new Warning/Error lines as
result["_log_warnings"]. Session id is injected as PARAMS["_session"] (parity with the
other modules) even though this module is almost entirely read-only.

Implemented:
  - search_classes     (read-only)  search UClasses (native reflection + Blueprint AssetRegistry)
  - get_mesh_info      (read-only)  native shape info for a StaticMesh asset
  - get_material_slots (read-only)  material element slots of a component/actor or StaticMesh asset
  - save_level         (benign write, NOT ledgered)  save the current level (non-destructive)

Notes learned live against TestMCPSetup (UE 5.8.1):
  - StaticMesh.Sockets is a PROTECTED UPROPERTY -> not readable from the asset via Python
    reflection. Per-asset socket enumeration is therefore best-effort only (get_sockets_by_tag).
  - Blueprint generated classes are unreal.Class *instances*, not python types, so python
    issubclass() cannot test them. base_class filtering for Blueprints uses the AssetRegistry
    "NativeParentClass" tag resolved to a native python class (robust when base_class is native).
  - A UClass' abstract flag is not exposed to stock Python reflection, BUT the native
    MCPReflectionLibrary.get_class_metadata_json C++ handler now surfaces it (plus parent_class,
    is_deprecated, is_blueprintable). search_classes enriches each RETURNED result with these.
    include_abstract is still accepted but advisory — abstract is reported, not filtered on.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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

    # ------------------------------------------------------------------ #
    # search_classes — search UClasses (native reflection + Blueprints)   #
    # ------------------------------------------------------------------ #
    _SEARCH_CLASSES_BODY = r'''
import unreal, json, warnings, inspect
warnings.simplefilter("ignore")
query = (PARAMS.get("query") or "").lower()
base_class = PARAMS.get("base_class")
max_results = int(PARAMS.get("max_results") or 200)
include_bp = PARAMS.get("include_blueprints")
include_bp = True if include_bp is None else bool(include_bp)
include_native = PARAMS.get("include_native")
include_native = True if include_native is None else bool(include_native)

# Resolve the requested base class to a native python type (used for issubclass filtering).
base_py = None
base_unresolved = False
if base_class:
    base_py = getattr(unreal, base_class, None)
    if not (inspect.isclass(base_py) and issubclass(base_py, unreal.Object)):
        base_py = None
        base_unresolved = True

results = []
native_scanned = 0
native_matched = 0
# --- Native classes: every unreal.* attribute that is a UObject subclass -------
if include_native:
    for n in dir(unreal):
        obj = getattr(unreal, n, None)
        if not (inspect.isclass(obj) and issubclass(obj, unreal.Object)):
            continue
        native_scanned += 1
        if base_py is not None and not issubclass(obj, base_py):
            continue
        if query and query not in n.lower():
            continue
        try:
            path = obj.static_class().get_path_name()
        except Exception:
            path = None
        native_matched += 1
        results.append({"name": n, "path": path, "kind": "native", "abstract": None})
        if len(results) >= max_results:
            break

# --- Blueprint classes: AssetRegistry scan of /Game ---------------------------
bp_scanned = 0
bp_matched = 0
bp_note = None
if include_bp and len(results) < max_results:
    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        far = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
            recursive_paths=True, package_paths=["/Game"])
        assets = ar.get_assets(far) or []
        for a in assets:
            bp_scanned += 1
            nm = str(a.get_editor_property("asset_name"))
            if query and query not in nm.lower():
                continue
            # base_class filter via the (cheap) NativeParentClass tag -> native ancestor
            if base_class:
                if base_py is None:
                    continue
                npc = None
                try:
                    npc = str(a.get_tag_value("NativeParentClass"))
                except Exception:
                    npc = None
                anc_py = None
                if npc:
                    short = npc.split("'")[1] if "'" in npc else npc
                    short = short.rstrip("'").split(".")[-1].split(":")[-1]
                    anc_py = getattr(unreal, short, None)
                if not (inspect.isclass(anc_py) and issubclass(anc_py, unreal.Object) and issubclass(anc_py, base_py)):
                    continue
            gpath = None
            try:
                gtag = str(a.get_tag_value("GeneratedClass"))
                gpath = gtag.split("'")[1] if "'" in gtag else gtag
            except Exception:
                gpath = None
            if gpath is None:
                try:
                    gpath = str(a.get_editor_property("package_name")) + "." + nm + "_C"
                except Exception:
                    gpath = None
            bp_matched += 1
            results.append({"name": nm + "_C", "path": gpath, "kind": "blueprint", "abstract": None})
            if len(results) >= max_results:
                break
    except Exception as e:
        bp_note = "blueprint scan failed: %s" % e

# --- Enrich the FINAL (filtered + max_results-capped) results with native class metadata ----
# ONE MCPReflectionLibrary.get_class_metadata_json call per RETURNED class (not per scanned
# class, to keep it cheap) fills in real is_abstract + parent_class/is_deprecated/is_blueprintable.
# Native rows resolve their UClass via getattr(unreal, name); Blueprint rows load the generated
# class from their path. Guarded per-row so a single failure never sinks the whole search.
meta_reached_any = False
for r in results:
    r["is_deprecated"] = None
    r["is_blueprintable"] = None
    r["parent_class"] = None
    cls_obj = None
    if r.get("kind") == "native":
        cand = getattr(unreal, r.get("name"), None)
        if inspect.isclass(cand) and issubclass(cand, unreal.Object):
            cls_obj = cand
    else:
        gp = r.get("path")
        if gp:
            try:
                cls_obj = unreal.load_object(None, gp)
            except Exception:
                cls_obj = None
    if cls_obj is None:
        r["abstract_note"] = "class object could not be resolved for metadata; abstract left null"
        continue
    try:
        cm = json.loads(unreal.MCPReflectionLibrary.get_class_metadata_json(cls_obj))
        r["abstract"] = cm.get("is_abstract")
        r["is_deprecated"] = cm.get("is_deprecated")
        r["is_blueprintable"] = cm.get("is_blueprintable")
        r["parent_class"] = cm.get("parent_class")
        meta_reached_any = True
    except Exception as e:
        r["abstract_note"] = "class metadata unavailable: %s" % e

out = {
    "status": "success",
    "query": PARAMS.get("query"),
    "base_class": base_class,
    "base_class_resolved": (base_py is not None) if base_class else None,
    "count": len(results),
    "results": results,
    "universe": {
        "native_object_subclasses_scanned": native_scanned,
        "native_matched": native_matched,
        "blueprints_scanned": bp_scanned,
        "blueprint_matched": bp_matched,
        "note": "native = classes exposed as attributes on the unreal module (python-facing UClasses); blueprints = Blueprint assets under /Game via AssetRegistry",
    },
    "abstract_note": ("'abstract' (is_abstract), parent_class, is_deprecated and is_blueprintable are populated per result from the native MCPReflectionLibrary C++ handler; include_abstract remains advisory (results are not filtered by abstractness)." if meta_reached_any else "MCPReflectionLibrary native helper unavailable; per-result 'abstract' left null (see each result's abstract_note)."),
    "truncated": len(results) >= max_results,
}
if base_unresolved:
    out["base_class_note"] = "base_class '%s' did not resolve to a native unreal.* class; native results unfiltered by it and blueprints excluded" % base_class
if bp_note:
    out["blueprint_note"] = bp_note
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def search_classes(ctx, query: str = None, base_class: str = None,
                       max_results: int = 200, include_abstract: bool = True,
                       include_native: bool = True, include_blueprints: bool = True) -> str:
        """Search UClasses in the running editor. Read-only.

        Enumerates two class universes and merges the matches:
          - native  : every class surfaced as an attribute on the `unreal` module
                      (python-facing UClasses; ~4900 UObject subclasses).
          - blueprint: Blueprint assets under /Game via the AssetRegistry (name + generated
                      class path). Set include_blueprints=False to skip.

        query:        case-insensitive substring on the class name.
        base_class:   restrict to subclasses of this class. Native results are filtered with
                      a true issubclass check. Blueprint results are filtered via the asset's
                      NativeParentClass tag resolved to a native class (robust when base_class
                      is a native class; a Blueprint base_class will not match Blueprints).
        max_results:  cap total results (default 200); response reports 'truncated'.
        include_abstract: ACCEPTED BUT ADVISORY — each RETURNED result now carries a real
                      'abstract' (is_abstract) plus parent_class, is_deprecated, and
                      is_blueprintable from the native MCPReflectionLibrary handler, but results
                      are still NOT filtered by abstractness (the flag is reported, not applied).
        include_native / include_blueprints: toggle each universe.

        The response's 'universe' block reports exactly how many classes were scanned/matched
        in each universe, so completeness is transparent rather than assumed."""
        params = {"query": query, "base_class": base_class, "max_results": max_results,
                  "include_native": include_native, "include_blueprints": include_blueprints}
        try:
            return json.dumps(_exec(_SEARCH_CLASSES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_mesh_info — native shape info for a StaticMesh asset            #
    # ------------------------------------------------------------------ #
    _MESH_INFO_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
path = PARAMS["mesh_path"]
m = unreal.EditorAssetLibrary.load_asset(path)
if m is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
else:
    cls = m.get_class().get_name()
    if not isinstance(m, unreal.StaticMesh):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a StaticMesh (got %s): %s" % (cls, path)}))
    else:
        esl = unreal.EditorStaticMeshLibrary
        info = {"status": "success", "mesh_path": m.get_path_name(), "class": cls}
        b = _try(lambda: m.get_bounds())
        if b is not None:
            be = b.box_extent; og = b.origin
            info["bounds"] = {
                "box_extent": [be.x, be.y, be.z],
                "origin": [og.x, og.y, og.z],
                "sphere_radius": _try(lambda: b.sphere_radius),
            }
        lods = _try(lambda: m.get_num_lods())
        if lods is None:
            lods = _try(lambda: esl.get_lod_count(m))
        info["num_lods"] = lods
        # Section 0 geometry counts (LOD 0).
        info["num_sections_lod0"] = _try(lambda: m.get_num_sections(0))
        info["num_triangles_lod0"] = _try(lambda: m.get_num_triangles(0))
        info["num_vertices_lod0"] = _try(lambda: m.get_num_vertices(0))
        info["num_uv_channels_lod0"] = _try(lambda: esl.get_num_uv_channels(m, 0))
        # Material slots (asset-level).
        slots = []
        sms = _try(lambda: m.get_editor_property("static_materials"), []) or []
        for i, sm in enumerate(sms):
            mi = _try(lambda: sm.get_editor_property("material_interface"))
            slots.append({
                "index": i,
                "slot_name": str(_try(lambda: sm.get_editor_property("material_slot_name"))),
                "material": (mi.get_path_name() if mi else None),
            })
        info["num_material_slots"] = len(slots)
        info["material_slots"] = slots
        if PARAMS.get("include_collision"):
            col = {
                "simple_collision_count": _try(lambda: esl.get_simple_collision_count(m)),
                "convex_collision_count": _try(lambda: esl.get_convex_collision_count(m)),
                "collision_complexity": _try(lambda: str(esl.get_collision_complexity(m))),
            }
            bs = _try(lambda: m.get_editor_property("body_setup"))
            col["has_body_setup"] = bs is not None
            if bs is not None:
                ag = _try(lambda: bs.get_editor_property("agg_geom"))
                if ag is not None:
                    col["primitives"] = {
                        "box": len(_try(lambda: ag.get_editor_property("box_elems"), []) or []),
                        "sphere": len(_try(lambda: ag.get_editor_property("sphere_elems"), []) or []),
                        "capsule": len(_try(lambda: ag.get_editor_property("sphyl_elems"), []) or []),
                        "convex": len(_try(lambda: ag.get_editor_property("convex_elems"), []) or []),
                    }
            info["collision"] = col
        if PARAMS.get("include_sockets"):
            if hasattr(unreal.MCPReflectionLibrary, "get_static_mesh_sockets_json"):
                # Native C++ handler reaches the PROTECTED StaticMesh.Sockets UPROPERTY that stock
                # Python reflection cannot. Returns EVERY socket (tagged or not) with full transform.
                sjs = _try(lambda: unreal.MCPReflectionLibrary.get_static_mesh_sockets_json(m))
                sd = _try(lambda: json.loads(sjs)) if sjs else None
                if isinstance(sd, dict):
                    socks = []
                    for s in (sd.get("sockets") or []):
                        socks.append({
                            "name": s.get("name"),
                            "tag": s.get("tag"),
                            "location": s.get("location"),
                            "rotation": s.get("rotation"),
                            "scale": s.get("scale"),
                        })
                    info["sockets"] = socks
                    info["socket_count"] = len(socks)
                    info["sockets_source"] = "MCPReflectionLibrary"
                else:
                    # Helper present but returned unparseable output -> fall back honestly.
                    tagged = _try(lambda: m.get_sockets_by_tag(""), []) or []
                    info["sockets"] = {
                        "tagged_socket_count": len(tagged),
                        "tagged_socket_names": [str(_try(lambda: s.get_editor_property("socket_name"))) for s in tagged],
                        "note": "StaticMesh.Sockets is protected in Python; only tag-matched sockets are enumerable from the asset. Untagged sockets are not listable here.",
                    }
                    info["sockets_source"] = "python_reflection_only"
            else:
                # StaticMesh.Sockets is a protected UPROPERTY (unreadable via python reflection);
                # get_sockets_by_tag only returns TAGGED sockets. Report best-effort + honest caveat.
                tagged = _try(lambda: m.get_sockets_by_tag(""), []) or []
                info["sockets"] = {
                    "tagged_socket_count": len(tagged),
                    "tagged_socket_names": [str(_try(lambda: s.get_editor_property("socket_name"))) for s in tagged],
                    "note": "StaticMesh.Sockets is protected in Python; only tag-matched sockets are enumerable from the asset. Untagged sockets are not listable here.",
                }
                info["sockets_source"] = "python_reflection_only"
        print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_mesh_info(ctx, mesh_path: str, include_collision: bool = False,
                      include_sockets: bool = False) -> str:
        """Native shape info for a StaticMesh asset. Read-only.

        mesh_path: asset path, e.g. '/Engine/BasicShapes/Cube.Cube'.

        Returns: bounds (box_extent, origin, sphere_radius), LOD count, LOD0 section /
        triangle / vertex / UV-channel counts, and material slots (index, slot_name,
        current material path).
        include_collision: add simple/convex collision counts, collision complexity, and
                           per-primitive counts (box/sphere/capsule/convex) from the body setup.
        include_sockets:   add socket info. When the native MCPReflectionLibrary.
                           get_static_mesh_sockets_json handler is present, returns the REAL,
                           complete socket list (name, tag, location, rotation, scale) reached
                           from the PROTECTED StaticMesh.Sockets UPROPERTY, with
                           sockets_source='MCPReflectionLibrary'. If that native helper is
                           unavailable it degrades to the prior best-effort tagged-socket
                           enumeration (sockets_source='python_reflection_only'; see the note in
                           the response — the asset's Sockets array is otherwise protected in
                           Python and only tagged sockets are listable).
        Errors cleanly if the asset is missing or is not a StaticMesh."""
        params = {"mesh_path": mesh_path, "include_collision": include_collision,
                  "include_sockets": include_sockets}
        try:
            return json.dumps(_exec(_MESH_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_material_slots — element slots of a component/actor or asset    #
    # ------------------------------------------------------------------ #
    _MATERIAL_SLOTS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
actor_label = PARAMS.get("actor") or PARAMS.get("actor_label")
asset_path = PARAMS.get("asset_path") or PARAMS.get("mesh_path")
comp_name = PARAMS.get("component")

def _slots_from_component(c):
    n = c.get_num_materials()
    names = list(c.get_material_slot_names() or [])
    out = []
    for i in range(n):
        mat = c.get_material(i)
        sn = names[i] if i < len(names) else None
        out.append({"index": i, "slot_name": (str(sn) if sn is not None else None),
                    "material": (mat.get_path_name() if mat else None)})
    return out

if actor_label:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    target = None
    for a in (eas.get_all_level_actors() or []):
        if a and (a.get_actor_label() == actor_label or a.get_name() == actor_label):
            target = a; break
    if target is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % actor_label}))
    else:
        comps = target.get_components_by_class(unreal.StaticMeshComponent) or []
        chosen = None
        if comp_name:
            for c in comps:
                if c.get_name() == comp_name:
                    chosen = c; break
            if chosen is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "StaticMeshComponent '%s' not found on actor %s" % (comp_name, actor_label),
                    "available_components": [c.get_name() for c in comps]}))
                comps = []
        elif comps:
            chosen = comps[0]
        if chosen is not None:
            sm = _try(lambda: chosen.get_editor_property("static_mesh"))
            print("@@UMCP@@" + json.dumps({"status": "success", "source": "actor_component",
                "actor": target.get_actor_label(), "actor_name": target.get_name(),
                "component": chosen.get_name(),
                "mesh": (sm.get_path_name() if sm else None),
                "num_slots": chosen.get_num_materials(), "slots": _slots_from_component(chosen)}))
        elif not comp_name:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "actor %s has no StaticMeshComponent" % actor_label}))
elif asset_path:
    m = unreal.EditorAssetLibrary.load_asset(asset_path)
    if m is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % asset_path}))
    elif not isinstance(m, unreal.StaticMesh):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a StaticMesh (got %s): %s" % (m.get_class().get_name(), asset_path)}))
    else:
        sms = _try(lambda: m.get_editor_property("static_materials"), []) or []
        slots = []
        for i, sm in enumerate(sms):
            mi = _try(lambda: sm.get_editor_property("material_interface"))
            slots.append({"index": i,
                "slot_name": str(_try(lambda: sm.get_editor_property("material_slot_name"))),
                "material": (mi.get_path_name() if mi else None)})
        print("@@UMCP@@" + json.dumps({"status": "success", "source": "static_mesh_asset",
            "mesh_path": m.get_path_name(), "num_slots": len(slots), "slots": slots}))
else:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "provide one of: actor/actor_label, or asset_path/mesh_path"}))
'''

    @mcp.tool()
    def get_material_slots(ctx, actor: str = None, actor_label: str = None,
                           asset_path: str = None, mesh_path: str = None,
                           component: str = None) -> str:
        """List the material element slots (index, slot_name, current material path) of a
        StaticMeshComponent on a placed actor, or of a StaticMesh asset. Read-only.

        Provide ONE target:
          actor / actor_label: actor display label (or unique internal name). Its first
                     StaticMeshComponent is used unless `component` names a specific one.
          asset_path / mesh_path: a StaticMesh asset path (reads its StaticMaterials array).
          component: (with actor) the StaticMeshComponent name to disambiguate multi-component actors.

        Component slots reflect override materials; asset slots reflect the mesh's defaults."""
        params = {"actor": actor, "actor_label": actor_label, "asset_path": asset_path,
                  "mesh_path": mesh_path, "component": component}
        try:
            return json.dumps(_exec(_MATERIAL_SLOTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # save_level — save the current level (benign write, NOT ledgered)    #
    # ------------------------------------------------------------------ #
    _SAVE_LEVEL_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = _try(lambda: ues.get_editor_world())
level_name = _try(lambda: world.get_name())
level_path = _try(lambda: world.get_path_name())
ok = False
err = None
try:
    res = les.save_current_level()
    ok = True if res is None else bool(res)
except Exception as e:
    err = str(e)
out = {"status": "success" if ok else "error", "saved": ok,
       "level_name": level_name, "level_path": level_path}
if err:
    out["message"] = err
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def save_level(ctx) -> str:
        """Save the current (persistent) level to disk. Benign, non-destructive write —
        it only flushes the in-memory level to its .umap file, so it is NOT ledgered and
        has no undo. Returns success + the level name/path. Read-only tools verify state;
        this is the one write here and should be used deliberately."""
        try:
            return json.dumps(_exec(_SAVE_LEVEL_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
