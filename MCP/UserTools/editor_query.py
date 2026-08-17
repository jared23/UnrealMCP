"""UserTools :: Editor / Query  (spec: docs/spec/editor.md)

Clean-room, READ-ONLY introspection commands over Unreal's public Python API
(subsystem-based, UE 5.8). Executed in the editor's interpreter via the plugin's
`execute_python` handler; results are captured from stdout.

Query convention (copied verbatim from editor_level.py): a snippet prints
  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it, so stray engine log lines can't corrupt the payload. Every snippet is
wrapped with Output-Log delta capture, surfacing new Warning/Error lines as
result['_log_warnings'].

Commands (all READ-ONLY; no writes, no ledger mutation):
  - get_scene_map        — compact clustered "level understanding" summary
  - list_level_templates — available new-level templates (engine World maps + Empty)
  - list_editor_windows  — reachable editor/viewport metadata (honest limitations)
  - get_undo_history     — read the per-session agent undo ledger (builtins._UMCP_LEDGERS)

get_undo_history is a pure READ of the ledger at builtins._UMCP_LEDGERS (populated by
the writer modules). scope 'mine' (this session's ledger, from PARAMS["_session"]),
'others', or 'all'. It never pops or mutates any ledger.
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
    # Session id identifies THIS reader/writer process; used as the default ledger key for
    # get_undo_history scope='mine'. One value per OS process. Override via utils["session"].
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
        the body in Unreal, and return its MARKER payload. Adds _session (used by
        get_undo_history scope='mine')."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # get_scene_map — compact clustered level-understanding summary       #
    # ------------------------------------------------------------------ #
    _SCENE_MAP_BODY = r'''
import unreal, json, warnings, math
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
class_filter = (PARAMS.get("class_filter") or "").lower()
label_filter = (PARAMS.get("label_filter") or "").lower()
region_grid = bool(PARAMS.get("region_grid"))
grid_size = int(PARAMS.get("grid_size") or 4)
cell_size = PARAMS.get("cell_size")
drill = int(PARAMS.get("drill") if PARAMS.get("drill") is not None else 3)
page = max(1, int(PARAMS.get("page") or 1))
page_size = max(1, int(PARAMS.get("page_size") or 20))
export = bool(PARAMS.get("export"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
all_actors = eas.get_all_level_actors() or []
matched = []
for a in all_actors:
    if not a:
        continue
    folder = _try(lambda: str(a.get_folder_path())) or ""
    if folder == "_MCP_Trash":
        continue
    cn = _try(lambda: a.get_class().get_name()) or "?"
    lb = _try(lambda: a.get_actor_label()) or ""
    if class_filter and class_filter not in cn.lower():
        continue
    if label_filter and label_filter not in lb.lower():
        continue
    loc = _try(lambda: a.get_actor_location())
    matched.append({"name": _try(lambda: a.get_name()), "label": lb, "class": cn,
                    "loc": [loc.x, loc.y, loc.z] if loc else [0.0, 0.0, 0.0]})
bounds = None
if matched:
    xs = [m["loc"][0] for m in matched]; ys = [m["loc"][1] for m in matched]; zs = [m["loc"][2] for m in matched]
    mn = [min(xs), min(ys), min(zs)]; mx = [max(xs), max(ys), max(zs)]
    bounds = {"min": [round(v, 1) for v in mn], "max": [round(v, 1) for v in mx],
              "size": [round(mx[i]-mn[i], 1) for i in range(3)],
              "center": [round((mx[i]+mn[i])/2.0, 1) for i in range(3)]}
def _cell_of(loc):
    x, y = loc[0], loc[1]
    if cell_size:
        cs = float(cell_size)
        return (int(math.floor(x/cs)), int(math.floor(y/cs)))
    mnx, mny = bounds["min"][0], bounds["min"][1]
    sx = bounds["size"][0] or 1.0; sy = bounds["size"][1] or 1.0
    ix = min(grid_size-1, max(0, int((x-mnx)/sx*grid_size)))
    iy = min(grid_size-1, max(0, int((y-mny)/sy*grid_size)))
    return (ix, iy)
clusters = {}
if region_grid and matched:
    for m in matched:
        cx, cy = _cell_of(m["loc"])
        key = "cell_%d_%d" % (cx, cy)
        c = clusters.setdefault(key, {"key": key, "count": 0, "classes": {}, "sample": []})
        c["count"] += 1
        c["classes"][m["class"]] = c["classes"].get(m["class"], 0) + 1
        if len(c["sample"]) < drill:
            c["sample"].append({"label": m["label"], "class": m["class"], "loc": [round(v,1) for v in m["loc"]]})
    cluster_by = "region_grid"
else:
    for m in matched:
        key = m["class"]
        c = clusters.setdefault(key, {"key": key, "count": 0, "sample": []})
        c["count"] += 1
        if len(c["sample"]) < drill:
            c["sample"].append({"label": m["label"], "loc": [round(v,1) for v in m["loc"]]})
    cluster_by = "class"
cluster_list = sorted(clusters.values(), key=lambda c: -c["count"])
if cluster_by == "region_grid":
    for c in cluster_list:
        top = sorted(c["classes"].items(), key=lambda kv: -kv[1])
        c["classes"] = {k: v for k, v in top}
total_clusters = len(cluster_list)
start = (page-1)*page_size
window = cluster_list[start:start+page_size]
nxt = page+1 if start+page_size < total_clusters else None
hist = {}
for m in matched:
    hist[m["class"]] = hist.get(m["class"], 0) + 1
hist = {k: v for k, v in sorted(hist.items(), key=lambda kv: -kv[1])}
result = {"status": "success",
          "world": _try(lambda: world.get_name()),
          "total_actors": len([a for a in all_actors if a]),
          "matched": len(matched),
          "bounds": bounds,
          "cluster_by": cluster_by,
          "grid": ({"grid_size": grid_size, "cell_size": cell_size} if cluster_by == "region_grid" else None),
          "class_histogram": hist,
          "total_clusters": total_clusters,
          "page": page, "page_size": page_size, "returned": len(window), "next_page": nxt,
          "clusters": window}
if export:
    result["flat"] = [{"label": m["label"], "class": m["class"], "loc": [round(v,1) for v in m["loc"]]} for m in matched[:500]]
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_scene_map(ctx, class_filter: str = None, label_filter: str = None,
                      region_grid: bool = False, grid_size: int = 4,
                      cell_size: float = None, drill: int = 3,
                      page: int = 1, page_size: int = 20, export: bool = False) -> str:
        """Compact, clustered "level understanding" summary of the active level. Read-only.

        Groups placed actors into clusters and returns per-cluster counts plus a few
        representative actors, so you can grasp a level without a full actor dump.
        Soft-deleted actors (in _MCP_Trash) are excluded.

        class_filter:  case-insensitive substring on class name (e.g. 'Light', 'StaticMesh').
        label_filter:  case-insensitive substring on display label.
        region_grid:   if True, cluster by spatial grid cell (X/Y) instead of by class;
                       each cell also reports its class breakdown. If False (default),
                       cluster by class.
        grid_size:     N for an NxN grid over the actor bounds (used when cell_size is unset).
        cell_size:     world units per grid cell; when set, cells are floor(loc/cell_size)
                       (absolute grid) and grid_size is ignored.
        drill:         number of representative actors to include per cluster (default 3).
        page/page_size: paginate the (count-descending) cluster list; response returns
                       'next_page' (pass it back as page) or null.
        export:        also include a compact flat list of matched actors (capped at 500).

        Always returns a global 'class_histogram' and world 'bounds' for quick context."""
        params = {"class_filter": class_filter, "label_filter": label_filter,
                  "region_grid": region_grid, "grid_size": grid_size, "cell_size": cell_size,
                  "drill": drill, "page": page, "page_size": page_size, "export": export}
        try:
            return json.dumps(_exec(_SCENE_MAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_level_templates — available new-level templates (read-only)    #
    # ------------------------------------------------------------------ #
    _LIST_TEMPLATES_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
FRIENDLY = {"Template_Default": "Default (Basic)", "OpenWorld": "Open World",
            "TimeOfDay_Default": "Time of Day", "VR-Basic": "VR Basic"}
roots = ["/Engine/Maps/Templates"]
ar = unreal.AssetRegistryHelpers.get_asset_registry()
tmpls = []
seen = set()
for root in roots:
    try:
        paths = unreal.EditorAssetLibrary.list_assets(root, recursive=True, include_folder=False) or []
    except Exception:
        paths = []
    for p in paths:
        base = p.split(".")[0]
        if base in seen:
            continue
        short = base.split("/")[-1]
        data = ar.get_asset_by_object_path(base + "." + short)
        cn = str(data.asset_class_path.asset_name) if (data and data.is_valid()) else "?"
        if cn != "World":
            continue
        seen.add(base)
        tmpls.append({"name": FRIENDLY.get(short, short), "map_path": base,
                      "template_id": short, "source": "engine_template_map"})
tmpls.sort(key=lambda t: t["template_id"])
tmpls.insert(0, {"name": "Empty Level", "map_path": None, "template_id": "Empty",
                 "source": "synthetic (LevelEditorSubsystem.new_level, no template)"})
result = {"status": "success", "count": len(tmpls), "templates": tmpls,
          "derivation": "Enumerated World assets under /Engine/Maps/Templates via the "
                        "AssetRegistry (there is no Python API that returns the New-Level "
                        "dialog's template list directly). 'Empty' is the engine blank-level "
                        "option created by LevelEditorSubsystem.new_level with no template."}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_level_templates(ctx) -> str:
        """List the new-level templates available in this engine build. Read-only.

        Returns the engine template World maps found under /Engine/Maps/Templates
        (each with a friendly name, map_path, and template_id usable with create_level),
        plus a synthetic 'Empty' entry for a blank level. Includes a 'derivation' note
        explaining how the list was obtained (there is no direct Python API for the
        New-Level dialog's template list, so it is derived from the AssetRegistry)."""
        try:
            return json.dumps(_query(_LIST_TEMPLATES_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_editor_windows — reachable editor/viewport metadata (read-only)#
    # ------------------------------------------------------------------ #
    _LIST_WINDOWS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
win = {"reachable": {}, "limitations": []}
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
win["reachable"]["level_editor_open"] = True
win["reachable"]["active_world"] = _try(lambda: world.get_name())
win["reachable"]["active_world_path"] = _try(lambda: world.get_path_name())
win["reachable"]["is_play_in_editor"] = _try(lambda: bool(world.is_play_in_editor()))
sz = _try(lambda: ues.get_level_viewport_size())
win["reachable"]["level_viewport_size"] = ([sz.x, sz.y] if sz is not None else None)
cam = _try(lambda: ues.get_level_viewport_camera_info())
if cam is not None:
    loc, rot = cam
    win["reachable"]["level_viewport_camera"] = {"loc": [loc.x, loc.y, loc.z],
                                                  "rot": [rot.pitch, rot.yaw, rot.roll]}
else:
    win["reachable"]["level_viewport_camera"] = None
win["limitations"] = [
    "Slate tab/window enumeration (FGlobalTabmanager) is not exposed to the Python API in UE 5.8, so open editor tabs/windows cannot be listed generically.",
    "AssetEditorSubsystem.get_all_edited_assets() is unavailable in this build, so currently-open asset-editor tabs cannot be enumerated either.",
    "Only the level-editor viewport/world metadata reported under 'reachable' is obtainable from Python."]
result = {"status": "partial", "windows": win,
          "note": "Honest metadata report: the Python API cannot enumerate arbitrary editor windows/tabs; see limitations."}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_editor_windows(ctx) -> str:
        """List open editor windows/tabs — as far as the Python API allows. Read-only.

        The UE 5.8 Python API does NOT expose Slate's tab/window manager, so arbitrary
        editor tabs cannot be enumerated. This returns the level-editor/viewport/world
        metadata that IS reachable (active world, PIE state, viewport size and camera)
        under 'reachable', and states exactly what is not reachable under 'limitations'
        (status is 'partial' to signal the honest limitation)."""
        try:
            return json.dumps(_query(_LIST_WINDOWS_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_undo_history — read the per-session agent ledger (read-only)     #
    # ------------------------------------------------------------------ #
    _UNDO_HISTORY_BODY = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
session = PARAMS.get("_session", "default")
scope = (PARAMS.get("scope") or "mine").lower()
max_results = PARAMS.get("max_results")
flt = (PARAMS.get("filter") or "").lower()
details = bool(PARAMS.get("details"))
ledgers = getattr(builtins, "_UMCP_LEDGERS", None) or {}
if scope == "mine":
    sessions = [session]
elif scope == "others":
    sessions = [s for s in ledgers.keys() if s != session]
else:
    sessions = list(ledgers.keys())
KEYS = ["op", "actor_name", "label", "path", "component_name", "prior_label",
        "prior_folder", "mode", "restorable"]
entries = []
for sid in sessions:
    led = ledgers.get(sid, []) or []
    for idx in range(len(led)-1, -1, -1):
        e = led[idx]
        if not isinstance(e, dict):
            e = {"op": str(e)}
        op = str(e.get("op", "?"))
        if flt and flt not in op.lower():
            continue
        if details:
            rec = dict(e)
        else:
            rec = {k: e.get(k) for k in KEYS if k in e}
        rec["_session"] = sid
        rec["_depth_index"] = idx
        entries.append(rec)
if max_results:
    entries = entries[:int(max_results)]
result = {"status": "success", "scope": scope, "session": session,
          "sessions_seen": list(ledgers.keys()),
          "ledger_depths": {s: len(ledgers.get(s, []) or []) for s in ledgers.keys()},
          "returned": len(entries), "entries": entries}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_undo_history(ctx, max_results: int = 50, filter: str = None,
                         scope: str = "mine", details: bool = False) -> str:
        """Read the agent undo ledger (builtins._UMCP_LEDGERS), most-recent first. Read-only.

        The writer modules record each reversible edit on a PER-SESSION ledger. This command
        reads that ledger without mutating it (it does NOT undo anything).

        scope:       'mine'   -> only this session's ledger (PARAMS['_session']; default),
                     'others' -> every OTHER session's ledger,
                     'all'    -> every session's ledger.
        max_results: cap the number of entries returned (default 50).
        filter:      case-insensitive substring matched against the op name (e.g. 'spawn').
        details:     if True, return the full ledger entry; otherwise a compact set of
                     key fields (op, actor_name, label, path, ...).

        Each entry is tagged with its owning '_session' and its '_depth_index' in that
        ledger. Also reports 'ledger_depths' for every session seen. Never pops/edits
        the ledger, so it is safe to call concurrently with writers."""
        params = {"max_results": max_results, "filter": filter, "scope": scope, "details": details}
        try:
            return json.dumps(_exec(_UNDO_HISTORY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
