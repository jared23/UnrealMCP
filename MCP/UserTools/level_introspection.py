"""UserTools :: Level / Introspection  (spec: docs/spec/editor.md)

Clean-room, READ-ONLY level introspection over Unreal's public Python API
(subsystem-based, UE 5.8). Executed in the editor's interpreter via the plugin's
`execute_python` handler; results are captured from stdout.

Query convention (copied verbatim from editor_level.py / editor_query.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it, so stray engine log lines can't corrupt the payload. Every snippet is wrapped
with Output-Log delta capture, surfacing new Warning/Error lines as result['_log_warnings'].

Complementary to editor_query.py (get_scene_map / list_level_templates / list_editor_windows /
get_undo_history) — this module adds spatial + structural reads that editor_query does NOT do:

  - get_level_bounds       — combined world-space AABB of all/filtered level actors
                             (aggregates actor.get_actor_bounds(); origin/extent/min/max/center).
                             editor_query.get_scene_map reports a POINT-cloud bound from actor
                             LOCATIONS only; this uses true rendered/collision bounds boxes.
  - get_selection_bounds   — combined AABB of the CURRENT editor selection (0 selected -> note).
  - count_actors_by_class  — focused, sortable class histogram with totals + optional base-class
                             filter (get_scene_map's histogram is a side value of a spatial map;
                             this is the dedicated, paginable, base-class-filterable primitive).
  - query_actors           — folder-aware multi-criteria finder (name/class/tag/FOLDER) returning
                             label+class+location+FOLDER path. (Named query_actors, not find_actors,
                             to avoid a hard MCP name collision with editor_level.find_actors, which
                             it supersets by adding a folder filter + per-actor folder/location.)
  - get_world_partition_info — is this a World Partition level? editor/runtime world bounds, actor
                             descriptor stats (spatially-loaded count, runtime-grid histogram),
                             data-layer instance count, WorldDataLayers presence. Honest about what
                             WP exposes to Python (runtime streaming state is cook/PIE-only).
  - list_streaming_levels  — classic streaming sublevels via EditorLevelUtils.get_levels(); for a WP
                             level reports none + that WP replaces classic sublevels with runtime grids.

All READ-ONLY: no writes, no ledger mutation, no asset/actor creation, no modal-popping APIs.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / editor_query.py) ---
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
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. Any new Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run
        the body in Unreal, and return its MARKER payload."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # get_level_bounds — combined world-space AABB of level actors        #
    # ------------------------------------------------------------------ #
    _LEVEL_BOUNDS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
name_contains = (PARAMS.get("name_contains") or "").lower()
class_name = (PARAMS.get("class_name") or "").lower()
include_nonspatial = bool(PARAMS.get("include_nonspatial"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
mn = [None, None, None]
mx = [None, None, None]
included = 0
skipped_nonspatial = 0
skipped_filter = 0
skipped_trashed = 0
sample = []
widest = None
for a in actors:
    if not a:
        continue
    folder = _try(lambda: str(a.get_folder_path())) or ""
    if folder == "_MCP_Trash":
        skipped_trashed += 1
        continue
    lb = _try(lambda: a.get_actor_label()) or ""
    cn = _try(lambda: a.get_class().get_name()) or ""
    if name_contains and name_contains not in lb.lower() and name_contains not in (_try(lambda: a.get_name()) or "").lower():
        skipped_filter += 1
        continue
    if class_name and class_name not in cn.lower():
        skipped_filter += 1
        continue
    b = _try(lambda: a.get_actor_bounds(False))
    if not (isinstance(b, tuple) and len(b) == 2):
        continue
    origin, extent = b
    o = [origin.x, origin.y, origin.z]
    e = [extent.x, extent.y, extent.z]
    # A truly non-spatial actor (WorldDataLayers, GameMode helpers) reports origin=[0,0,0]
    # AND extent=[0,0,0]; skip those by default so they do not drag bounds toward the origin.
    if (not include_nonspatial) and o == [0.0, 0.0, 0.0] and e == [0.0, 0.0, 0.0]:
        skipped_nonspatial += 1
        continue
    amn = [o[i] - e[i] for i in range(3)]
    amx = [o[i] + e[i] for i in range(3)]
    for i in range(3):
        mn[i] = amn[i] if mn[i] is None else min(mn[i], amn[i])
        mx[i] = amx[i] if mx[i] is None else max(mx[i], amx[i])
    included += 1
    emag = max(abs(e[0]), abs(e[1]), abs(e[2]))
    if widest is None or emag > widest["extent_magnitude"]:
        widest = {"label": lb, "class": cn, "extent_magnitude": round(emag, 1),
                  "extent": [round(v, 1) for v in e]}
    if len(sample) < 3:
        sample.append({"label": lb, "class": cn})
if included == 0:
    print("@@UMCP@@" + json.dumps({"status": "empty", "message": "no actors matched (or all non-spatial)",
        "included": 0, "skipped_nonspatial": skipped_nonspatial, "skipped_filter": skipped_filter,
        "skipped_trashed": skipped_trashed}))
else:
    mn = [round(v, 3) for v in mn]
    mx = [round(v, 3) for v in mx]
    center = [round((mn[i] + mx[i]) / 2.0, 3) for i in range(3)]
    extent = [round((mx[i] - mn[i]) / 2.0, 3) for i in range(3)]
    size = [round(mx[i] - mn[i], 3) for i in range(3)]
    print("@@UMCP@@" + json.dumps({"status": "success", "included_actors": included,
        "skipped_nonspatial": skipped_nonspatial, "skipped_filter": skipped_filter,
        "skipped_trashed": skipped_trashed,
        "min": mn, "max": mx, "center": center, "origin": center, "extent": extent, "size": size,
        "sample": sample, "widest_actor": widest,
        "note": "AABB aggregated from actor.get_actor_bounds(only_colliding_components=False) "
                "boxes (min=origin-extent, max=origin+extent). origin==center of the combined box. "
                "'widest_actor' is the single largest bounds contributor (e.g. a sky sphere often "
                "dominates the level AABB); filter it out via class_name/name_contains for the "
                "playable-area bounds."}))
'''

    @mcp.tool()
    def get_level_bounds(ctx, name_contains: str = None, class_name: str = None,
                         include_nonspatial: bool = False) -> str:
        """Combined world-space bounding box (AABB) of all (or filtered) level actors. Read-only.

        Aggregates each actor's true rendered/collision box via
        actor.get_actor_bounds(only_colliding_components=False) -> (origin, extent), then
        unions min=origin-extent / max=origin+extent across actors. Returns min, max, center
        (== origin), extent (half-size), and size (full-size). This is distinct from
        get_scene_map's bounds, which are a point cloud of actor LOCATIONS only.

        name_contains:      case-insensitive substring on label or internal name.
        class_name:         case-insensitive substring on class name.
        include_nonspatial: include actors whose box is degenerate at the origin
                            (e.g. WorldDataLayers, managers); default False so they do not
                            drag the combined box toward [0,0,0].
        Soft-deleted (_MCP_Trash) actors are always excluded. Reports how many were skipped."""
        params = {"name_contains": name_contains, "class_name": class_name,
                  "include_nonspatial": include_nonspatial}
        try:
            return json.dumps(_exec(_LEVEL_BOUNDS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_selection_bounds — AABB of the current editor selection         #
    # ------------------------------------------------------------------ #
    _SELECTION_BOUNDS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sel = eas.get_selected_level_actors() or []
sel = [a for a in sel if a]
if not sel:
    print("@@UMCP@@" + json.dumps({"status": "empty", "selected": 0,
        "message": "no actors are currently selected in the editor; nothing to bound"}))
else:
    mn = [None, None, None]
    mx = [None, None, None]
    used = 0
    members = []
    for a in sel:
        b = _try(lambda a=a: a.get_actor_bounds(False))
        lb = _try(lambda a=a: a.get_actor_label()) or ""
        cn = _try(lambda a=a: a.get_class().get_name()) or ""
        if not (isinstance(b, tuple) and len(b) == 2):
            members.append({"label": lb, "class": cn, "bounded": False})
            continue
        origin, extent = b
        o = [origin.x, origin.y, origin.z]
        e = [extent.x, extent.y, extent.z]
        amn = [o[i] - e[i] for i in range(3)]
        amx = [o[i] + e[i] for i in range(3)]
        for i in range(3):
            mn[i] = amn[i] if mn[i] is None else min(mn[i], amn[i])
            mx[i] = amx[i] if mx[i] is None else max(mx[i], amx[i])
        used += 1
        members.append({"label": lb, "class": cn, "bounded": True})
    if used == 0:
        print("@@UMCP@@" + json.dumps({"status": "empty", "selected": len(sel),
            "message": "selection has no boundable actors", "members": members}))
    else:
        mn = [round(v, 3) for v in mn]
        mx = [round(v, 3) for v in mx]
        center = [round((mn[i] + mx[i]) / 2.0, 3) for i in range(3)]
        extent = [round((mx[i] - mn[i]) / 2.0, 3) for i in range(3)]
        size = [round(mx[i] - mn[i], 3) for i in range(3)]
        print("@@UMCP@@" + json.dumps({"status": "success", "selected": len(sel),
            "bounded_actors": used, "min": mn, "max": mx, "center": center, "origin": center,
            "extent": extent, "size": size, "members": members,
            "note": "AABB unioned over the current editor selection's get_actor_bounds() boxes."}))
'''

    @mcp.tool()
    def get_selection_bounds(ctx) -> str:
        """Combined world-space bounding box (AABB) of the CURRENT editor selection. Read-only.

        Reads EditorActorSubsystem.get_selected_level_actors() and unions each selected
        actor's get_actor_bounds() box. If nothing is selected, returns a clean 'empty' note
        (selected=0) rather than an error. Returns min/max/center(=origin)/extent/size plus a
        per-member list (each flagged whether it contributed a bound)."""
        try:
            return json.dumps(_exec(_SELECTION_BOUNDS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # count_actors_by_class — sortable class histogram with totals        #
    # ------------------------------------------------------------------ #
    _COUNT_BY_CLASS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
base_class = PARAMS.get("base_class")
include_trashed = bool(PARAMS.get("include_trashed"))
max_rows = PARAMS.get("max_rows")
base_cls = getattr(unreal, base_class, None) if base_class else None
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
hist = {}
total = 0
trashed = 0
base_filtered = 0
base_unresolved = (base_class is not None and base_cls is None)
for a in actors:
    if not a:
        continue
    folder = _try(lambda: str(a.get_folder_path())) or ""
    if folder == "_MCP_Trash" and not include_trashed:
        trashed += 1
        continue
    if base_cls is not None:
        if not isinstance(a, base_cls):
            base_filtered += 1
            continue
    cn = _try(lambda: a.get_class().get_name()) or "?"
    hist[cn] = hist.get(cn, 0) + 1
    total += 1
rows = sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))
if max_rows:
    rows = rows[:int(max_rows)]
result = {"status": "success", "total_actors": total, "distinct_classes": len(hist),
          "counted_by": ("subclasses of " + base_class) if base_cls is not None else "all classes",
          "trashed_excluded": (0 if include_trashed else trashed),
          "histogram": [{"class": k, "count": v} for k, v in rows]}
if base_class is not None:
    result["base_class"] = base_class
    result["base_class_resolved"] = (base_cls is not None)
    result["base_class_filtered_out"] = base_filtered
    if base_unresolved:
        result["note"] = "base_class '%s' is not a known unreal.* class name; no base filter applied results shown -> returned 0. Use a Python-facing class name (e.g. 'Light', 'StaticMeshActor')." % base_class
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def count_actors_by_class(ctx, base_class: str = None, include_trashed: bool = False,
                              max_rows: int = None) -> str:
        """Histogram of actor counts per class in the active level, count-descending. Read-only.

        A focused, sortable primitive (distinct from get_scene_map, where the histogram is a
        side value of a spatial cluster map). Returns total_actors, distinct_classes, and a
        [{class, count}] list.

        base_class:      optional engine class NAME (e.g. 'Light', 'StaticMeshActor'); only
                         actors that are that class or a subclass are counted. If the name is
                         not a Python-facing unreal.* class, a clean note is returned.
        include_trashed: include soft-deleted (_MCP_Trash) actors; default False.
        max_rows:        cap the number of histogram rows returned (totals are still full-set)."""
        params = {"base_class": base_class, "include_trashed": include_trashed, "max_rows": max_rows}
        try:
            return json.dumps(_exec(_COUNT_BY_CLASS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # query_actors — folder-aware multi-criteria finder (read-only)       #
    # ------------------------------------------------------------------ #
    _QUERY_ACTORS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
name_contains = (PARAMS.get("name_contains") or "").lower()
class_name = (PARAMS.get("class_name") or "").lower()
tag = PARAMS.get("tag")
folder = PARAMS.get("folder")
folder_l = (folder or "").lower()
folder_exact = bool(PARAMS.get("folder_exact"))
limit = PARAMS.get("limit")
include_trashed = bool(PARAMS.get("include_trashed"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
out = []
scanned = 0
truncated = False
for a in actors:
    if not a:
        continue
    fpath = _try(lambda: str(a.get_folder_path())) or ""
    fpath = "" if fpath in ("None",) else fpath
    if fpath == "_MCP_Trash" and not include_trashed:
        continue
    scanned += 1
    lb = _try(lambda: a.get_actor_label()) or ""
    nm = _try(lambda: a.get_name()) or ""
    cn = _try(lambda: a.get_class().get_name()) or ""
    if name_contains and name_contains not in lb.lower() and name_contains not in nm.lower():
        continue
    if class_name and class_name not in cn.lower():
        continue
    if folder is not None:
        if folder_exact:
            if fpath != folder:
                continue
        else:
            if folder_l not in fpath.lower():
                continue
    if tag:
        has = _try(lambda: a.actor_has_tag(unreal.Name(tag)), False)
        if not has:
            continue
    loc = _try(lambda: a.get_actor_location())
    out.append({"label": lb, "name": nm, "class": cn,
                "location": ([round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)] if loc else None),
                "folder": fpath})
    if limit and len(out) >= int(limit):
        truncated = True
        break
print("@@UMCP@@" + json.dumps({"status": "success", "matched": len(out), "scanned": scanned,
    "truncated": truncated,
    "criteria": {"name_contains": PARAMS.get("name_contains"), "class_name": PARAMS.get("class_name"),
                 "tag": tag, "folder": folder, "folder_exact": folder_exact, "limit": limit},
    "actors": out}))
'''

    @mcp.tool()
    def query_actors(ctx, name_contains: str = None, class_name: str = None,
                     tag: str = None, folder: str = None, folder_exact: bool = False,
                     limit: int = None, include_trashed: bool = False) -> str:
        """Folder-aware multi-criteria actor finder. Read-only. All filters are AND-combined.

        Returns each match's label, internal name, class, world location [x,y,z], and outliner
        FOLDER path. Named query_actors (not find_actors) to avoid an MCP name collision with
        editor_level.find_actors, which this supersets by adding a folder filter and returning
        each actor's folder + location. Complementary to editor_query.get_scene_map (spatial map).

        name_contains: case-insensitive substring on label or internal name.
        class_name:    case-insensitive substring on class name.
        tag:           only actors carrying this actor tag.
        folder:        outliner folder path filter (substring by default; set folder_exact=True
                       to require an exact match; use '' with folder_exact=True for root/no-folder).
        limit:         cap results (response reports 'truncated' if the cap was hit).
        include_trashed: include soft-deleted (_MCP_Trash) actors; default False."""
        params = {"name_contains": name_contains, "class_name": class_name, "tag": tag,
                  "folder": folder, "folder_exact": folder_exact, "limit": limit,
                  "include_trashed": include_trashed}
        try:
            return json.dumps(_exec(_QUERY_ACTORS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_world_partition_info — WP facts reachable from Python           #
    # ------------------------------------------------------------------ #
    _WP_INFO_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _box(b):
    if b is None:
        return None
    return {"min": [round(b.min.x, 1), round(b.min.y, 1), round(b.min.z, 1)],
            "max": [round(b.max.x, 1), round(b.max.y, 1), round(b.max.z, 1)],
            "is_valid": bool(getattr(b, "is_valid", False))}
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
wpbl = unreal.WorldPartitionBlueprintLibrary
# --- signals of World Partition ---
wdl_present = any((a and a.get_class().get_name() == "WorldDataLayers")
                  for a in (eas.get_all_level_actors() or []))
dlm = _try(lambda: world.get_data_layer_manager())
descs = _try(lambda: wpbl.get_actor_descs(), None)
descs = descs if descs is not None else []
desc_count = len(descs)
is_wp = bool(wdl_present or (desc_count > 0) or (dlm is not None))
result = {"status": "success", "world": _try(lambda: world.get_name()),
          "world_path": _try(lambda: world.get_path_name()),
          "is_world_partition": is_wp,
          "detection_signals": {"world_data_layers_actor_present": wdl_present,
                                "actor_descriptor_count": desc_count,
                                "data_layer_manager_present": dlm is not None},
          "editor_world_bounds": _box(_try(lambda: wpbl.get_editor_world_bounds())),
          "runtime_world_bounds": _box(_try(lambda: wpbl.get_runtime_world_bounds()))}
# --- actor descriptor stats (WP one-file-per-actor descriptors, incl. unloaded) ---
spatial = 0
editor_only = 0
grids = {}
classes = {}
for d in descs:
    if _try(lambda d=d: bool(d.is_spatially_loaded)) is True:
        spatial += 1
    if _try(lambda d=d: bool(d.actor_is_editor_only)) is True:
        editor_only += 1
    g = _try(lambda d=d: str(d.runtime_grid)) or "None"
    grids[g] = grids.get(g, 0) + 1
    nc = _try(lambda d=d: d.native_class.get_name()) or "?"
    classes[nc] = classes.get(nc, 0) + 1
top_classes = sorted(classes.items(), key=lambda kv: -kv[1])[:8]
result["actor_descriptors"] = {"total": desc_count, "spatially_loaded": spatial,
    "editor_only": editor_only, "runtime_grid_histogram": grids,
    "top_native_classes": [{"class": k, "count": v} for k, v in top_classes]}
# --- data layers (via DataLayerManager, non-modal) ---
if dlm is not None:
    insts = _try(lambda: dlm.get_data_layer_instances(), None)
    result["data_layers"] = {"instance_count": (len(insts) if insts else 0),
                             "manager_present": True}
else:
    result["data_layers"] = {"instance_count": 0, "manager_present": False}
# --- honest limitations ---
result["limitations"] = [
    "Runtime streaming state (which grid cells/streaming sources are active) is a cook/PIE-time "
    "concept; in the editor world all actor descriptors here report runtime_grid='None' and "
    "is_spatially_loaded reflects only the actor's editor descriptor flag, not live streaming.",
    "unreal.WorldPartitionSubsystem is not registered as an editor subsystem in this build, so the "
    "live runtime WorldPartition object (grid/cell layout, active streaming sources) is not reachable "
    "from editor Python; descriptor-level data (bounds, class, data layers, runtime grid) IS.",
    "The World UObject exposes no 'world_partition' property to Python (get_editor_property fails); "
    "WP presence is inferred from the signals in 'detection_signals'."]
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_world_partition_info(ctx) -> str:
        """Report whether the active level is World Partition and what WP data Python can reach.
        Read-only. Distinct from editor_query (which does not touch WP).

        Returns: is_world_partition (with the detection_signals it was inferred from),
        editor_world_bounds and runtime_world_bounds (the WP grid extent, as Box min/max),
        actor_descriptor stats (total one-file-per-actor descriptors incl. unloaded, how many are
        spatially_loaded / editor_only, a runtime-grid histogram, and top native classes),
        and data-layer instance_count (via the non-modal DataLayerManager). Includes an honest
        'limitations' list: runtime streaming/grid-cell state and the live WorldPartition object
        are cook/PIE-time only and not reachable from editor Python in UE 5.8."""
        try:
            return json.dumps(_query(_WP_INFO_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_streaming_levels — classic sublevels (EditorLevelUtils)        #
    # ------------------------------------------------------------------ #
    _STREAMING_LEVELS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
levels = _try(lambda: unreal.EditorLevelUtils.get_levels(world), []) or []
persistent_name = _try(lambda: world.get_name())
out = []
persistent_count = 0
streaming_count = 0
for lv in levels:
    nm = _try(lambda lv=lv: lv.get_name()) or "?"
    cn = _try(lambda lv=lv: lv.get_class().get_name()) or "?"
    owning = _try(lambda lv=lv: lv.get_outer().get_name())
    is_persistent = (nm == "PersistentLevel")
    if is_persistent:
        persistent_count += 1
    else:
        streaming_count += 1
    out.append({"name": nm, "class": cn, "owning_world": owning, "is_persistent": is_persistent})
# WP detection (classic streaming is replaced by WP one-file-per-actor + runtime grids)
wdl_present = any((a and a.get_class().get_name() == "WorldDataLayers")
                  for a in (eas.get_all_level_actors() or []))
desc_count = len(_try(lambda: unreal.WorldPartitionBlueprintLibrary.get_actor_descs(), []) or [])
is_wp = bool(wdl_present or desc_count > 0)
result = {"status": "success", "world": persistent_name,
          "level_count": len(out), "persistent_levels": persistent_count,
          "streaming_sublevels": streaming_count, "levels": out,
          "is_world_partition": is_wp}
if streaming_count == 0:
    if is_wp:
        result["note"] = ("No classic streaming sublevels. This is a World Partition level: WP "
                          "replaces manually-added streaming sublevels with one-file-per-actor "
                          "external actors + runtime grids (see get_world_partition_info). Only the "
                          "persistent level is enumerable via EditorLevelUtils.get_levels().")
    else:
        result["note"] = ("No streaming sublevels are loaded in this world; only the persistent "
                          "level is present.")
result["derivation"] = ("Enumerated via EditorLevelUtils.get_levels(editor_world). The World UObject "
                        "'Levels' array is protected in Python, so this is the reachable path; it lists "
                        "currently-loaded levels (persistent + any loaded streaming sublevels).")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_streaming_levels(ctx) -> str:
        """List classic streaming sublevels in the active world. Read-only.

        Enumerates EditorLevelUtils.get_levels(editor_world) (the World's own 'Levels' array is
        protected in Python). Each entry: name, class, owning_world, is_persistent. Reports
        persistent_levels vs streaming_sublevels counts. If there are no streaming sublevels and
        the level is World Partition, says so explicitly (WP replaces classic sublevels with
        one-file-per-actor external actors + runtime grids; see get_world_partition_info)."""
        try:
            return json.dumps(_query(_STREAMING_LEVELS_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"
