"""UserTools :: World Partition actor-ops  (spec: docs/spec/editor.md -- "World Partition")

Clean-room reimplementation over Unreal's public Python API (UE 5.8), the World-Partition
actor-descriptor / streaming counterpart to datalayers.py. It mirrors editor_level.py's proven
conventions VERBATIM: base64-injected PARAMS, the Output-Log auto-capture wrapper, the @@UMCP@@
marker, the session-aware per-agent undo ledger, and a slim helpers block (session-aware
_ledger(), _resolve_actor, _find_by_name).

The operative API is the static library  unreal.WorldPartitionBlueprintLibrary  (all
BlueprintCallable). Its statics operate on ACTOR DESCRIPTORS (FWorldPartitionActorDesc), which
describe every partitioned actor WITHOUT loading it, plus load/unload/pin/unpin by GUID:
  * get_actor_descs() -> Array[ActorDesc]                (all descriptors in the partition)
  * get_intersecting_actor_descs(box:Box) -> Array[ActorDesc]  (descriptors intersecting a box)
  * load_actors(actors_to_load:Array[Guid]) -> None
  * unload_actors(actors_to_unload:Array[Guid]) -> None
  * pin_actors(actors_to_pin:Array[Guid]) -> None
  * unpin_actors(actors_to_unpin:Array[Guid]) -> None
  * get_editor_world_bounds() -> Box / get_runtime_world_bounds() -> Box
An ActorDesc exposes: guid (Guid), name, label, class_ (SoftObjectPath), native_class (Class),
bounds (Box), runtime_grid (Name), is_spatially_loaded (bool), data_layer_assets, actor_package,
actor_path, actor_is_editor_only. A Guid round-trips to/from a hex string via
guid.export_text() / (unreal.Guid(); g.import_text(hex)) -- this is how load/unload/pin ledgers
persist their inverse GUID sets across the process boundary (live-verified: load_actors accepts
rebuilt guids).

Per-actor WP properties are plain reflected UPROPERTYs (there is NO python-exposed C++ setter --
actor.set_is_spatially_loaded / set_hlod_layer do NOT exist on the python Actor, verified), so they
are written with actor.set_editor_property("is_spatially_loaded"/"runtime_grid", v) inside a
ScopedEditorTransaction with the prior value captured for the inverse.

Implemented:
  - list_world_partition_actors  (read-only)  -- descriptors (optionally box-filtered), no loading
  - validate_world_partition     (read-only)  -- editor-side descriptor facts / consistency summary
  - load_world_partition_region  (write; ledgered wp_load_actors;   inverse unload_actors)
  - unload_world_partition_region(write; ledgered wp_unload_actors; inverse load_actors)
  - pin_world_partition_actors   (write; ledgered wp_pin_actors / wp_unpin_actors; pin<->unpin)
  - set_actor_spatially_loaded   (write; ledgered set_actor_spatially_loaded; captures prior bool)
  - set_actor_runtime_grid       (write; ledgered set_actor_runtime_grid; captures prior FName)

This module defines NO `undo` tool (like the other Agent-B modules); the coordinator folds the
matching inverse branches into editor_level.py's unified `undo`.

NEW ledger op schemas (for editor_level.undo integration; resolve the library via
unreal.WorldPartitionBlueprintLibrary, guids via unreal.Guid()+import_text(hex), actors via
_find_by_name):
  wp_load_actors:   {"op":"wp_load_actors","guids":[<hex>,...],"box":[minx,miny,minz,maxx,maxy,maxz]|null}
     -> invert: wpbl.unload_actors([Guid.import_text(h) for h in guids])
  wp_unload_actors: {"op":"wp_unload_actors","guids":[<hex>,...],"box":[...]|null}
     -> invert: wpbl.load_actors([Guid.import_text(h) for h in guids])
  wp_pin_actors:    {"op":"wp_pin_actors","guids":[<hex>,...]}
     -> invert: wpbl.unpin_actors([Guid.import_text(h) for h in guids])
  wp_unpin_actors:  {"op":"wp_unpin_actors","guids":[<hex>,...]}
     -> invert: wpbl.pin_actors([Guid.import_text(h) for h in guids])
  set_actor_spatially_loaded: {"op":"set_actor_spatially_loaded","actor_name":<uniq>,"prior":<bool>}
     -> invert: a=_find_by_name(actor_name); a.set_editor_property("is_spatially_loaded", prior)
  set_actor_runtime_grid: {"op":"set_actor_runtime_grid","actor_name":<uniq>,"prior":<str>}
     -> invert: a=_find_by_name(actor_name); a.set_editor_property("runtime_grid", unreal.Name(prior))

REVERSIBILITY NOTE (load/unload/pin): the inverse operates on EXACTLY the GUIDs this op touched.
If some of those actors were already loaded/pinned before the op, the plain inverse still
load/unloads them; WP editor region streaming is coarse and this matches the region-op contract.
The captured GUID set (not the box) is the source of truth for the inverse, so a later change to
the descriptor set inside the box cannot desync the undo.

FIXTURE: these features REQUIRE a World-Partition level. Tests run on a WP scratch map created via
LevelEditorSubsystem.new_level_from_template("/Game/MCP_Scratch/WP1_Scratch",
"/Engine/Maps/Templates/OpenWorld") (get_editor_world_bounds returns a valid WP box there). A
non-WP level yields empty descriptor lists (list/validate report world_partition_present=False).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py) ------------------
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

    # Slim session-aware helpers (subset of editor_level.py's _COERCE_HELPERS). No '''/no backslash.
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
'''

    # World-Partition helpers (appended to bodies that need them). No '''/no backslash.
    _WP_HELPERS = r'''
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _wpbl():
    return unreal.WorldPartitionBlueprintLibrary
def _editor_world():
    w = _try(lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world())
    if w is None:
        w = _try(lambda: unreal.EditorLevelLibrary.get_editor_world())
    return w
def _wp_present():
    b = _try(lambda: _wpbl().get_editor_world_bounds())
    if b is None:
        return False
    try:
        return len(_wpbl().get_actor_descs() or []) >= 0
    except Exception:
        return False
def _box_from(spec):
    if spec is None:
        return None
    mn = mx = None
    if isinstance(spec, dict):
        mn = spec.get("min"); mx = spec.get("max")
    elif isinstance(spec, (list, tuple)) and len(spec) >= 6:
        mn = [spec[0], spec[1], spec[2]]; mx = [spec[3], spec[4], spec[5]]
    if not mn or not mx or len(mn) < 3 or len(mx) < 3:
        return None
    return unreal.Box(unreal.Vector(float(mn[0]), float(mn[1]), float(mn[2])),
                      unreal.Vector(float(mx[0]), float(mx[1]), float(mx[2])))
def _box_list(box):
    if box is None:
        return None
    return [box.min.x, box.min.y, box.min.z, box.max.x, box.max.y, box.max.z]
def _guid_text(g):
    return _try(lambda: str(g.export_text()))
def _guid_from_text(t):
    g = unreal.Guid()
    g.import_text(str(t))
    return g
def _descs_for(box):
    wpbl = _wpbl()
    if box is None:
        return list(wpbl.get_actor_descs() or [])
    return list(wpbl.get_intersecting_actor_descs(box) or [])
def _desc_rec(d):
    rec = {
        "name": _try(lambda: str(d.name)),
        "label": _try(lambda: str(d.label)),
        "guid": _guid_text(d.guid),
        "class": _try(lambda: str(d.class_)),
        "native_class": _try(lambda: (d.native_class.get_name() if d.native_class else None)),
        "is_spatially_loaded": _try(lambda: bool(d.is_spatially_loaded)),
        "runtime_grid": _try(lambda: str(d.runtime_grid)),
        "actor_is_editor_only": _try(lambda: bool(d.actor_is_editor_only)),
        "actor_package": _try(lambda: str(d.actor_package)),
    }
    b = _try(lambda: d.bounds)
    if b is not None:
        rec["bounds"] = {"min": [b.min.x, b.min.y, b.min.z], "max": [b.max.x, b.max.y, b.max.z]}
    dla = _try(lambda: list(d.data_layer_assets) or [], [])
    if dla:
        rec["data_layer_assets"] = [str(x) for x in dla]
    return rec
'''

    # ------------------------------------------------------------------ #
    # list_world_partition_actors -- descriptors (read-only)             #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _WP_HELPERS + r'''
import unreal, json
flt = (PARAMS.get("filter") or "").lower()
box = _box_from(PARAMS.get("region_box"))
present = _wp_present()
descs = _descs_for(box) if present else []
recs = []
for d in descs:
    r = _desc_rec(d)
    if flt:
        hay = " ".join(str(x) for x in (r.get("name"), r.get("label"), r.get("class"),
              r.get("native_class"), r.get("runtime_grid")) if x).lower()
        if flt not in hay:
            continue
    recs.append(r)
recs.sort(key=lambda r: (r.get("label") or r.get("name") or ""))
limit = PARAMS.get("max_results")
total = len(recs)
if limit:
    recs = recs[:int(limit)]
print("@@UMCP@@" + json.dumps({"status": "success",
    "world_partition_present": present,
    "region_box": _box_list(box),
    "total_matched": total, "returned": len(recs),
    "actor_desc_count": len(descs), "filter": PARAMS.get("filter"),
    "actors": recs,
    "note": "descriptors only (WorldPartitionBlueprintLibrary.get_actor_descs / get_intersecting_actor_descs); NO actors are loaded. guid is a hex string usable with load/unload/pin tools."}))
'''

    @mcp.tool()
    def list_world_partition_actors(ctx, filter: str = None, region_box=None,
                                    max_results: int = None) -> str:
        """List World Partition actor DESCRIPTORS in the active level. Read-only, NO loading.

        Enumerates FWorldPartitionActorDesc entries via
        WorldPartitionBlueprintLibrary.get_actor_descs() (whole world) or
        get_intersecting_actor_descs(box) when region_box is given. For each descriptor reports:
        name, label, guid (hex string -- usable directly with load/unload/pin tools), class,
        native_class, is_spatially_loaded, runtime_grid, bounds, data_layer_assets.

        filter:      optional case-insensitive substring matched against name/label/class/grid.
        region_box:  optional [minx,miny,minz,maxx,maxy,maxz] OR {"min":[x,y,z],"max":[x,y,z]}.
                     Omit to enumerate the whole partition.
        max_results: optional cap on returned rows (total_matched still reflects the full count).

        Requires a World-Partition level; on a non-WP level returns world_partition_present=False
        and an empty list."""
        params = {"filter": filter, "region_box": region_box, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_world_partition -- editor-side descriptor facts (read)    #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _WP_HELPERS + r'''
import unreal, json
present = _wp_present()
world = _editor_world()
if not present:
    print("@@UMCP@@" + json.dumps({"status": "success", "world_partition_present": False,
        "world": (world.get_name() if world else None),
        "note": "active level is not World-Partition enabled; no actor descriptors to validate"}))
else:
    wpbl = _wpbl()
    descs = list(wpbl.get_actor_descs() or [])
    eb = _try(lambda: wpbl.get_editor_world_bounds())
    rb = _try(lambda: wpbl.get_runtime_world_bounds())
    spatially = 0
    nonspatial = 0
    editor_only = 0
    no_guid = 0
    grids = {}
    classes = {}
    dl_refs = {}
    for d in descs:
        if _try(lambda d=d: bool(d.is_spatially_loaded)):
            spatially += 1
        else:
            nonspatial += 1
        if _try(lambda d=d: bool(d.actor_is_editor_only)):
            editor_only += 1
        if not _guid_text(d.guid):
            no_guid += 1
        g = _try(lambda d=d: str(d.runtime_grid)) or "None"
        grids[g] = grids.get(g, 0) + 1
        nc = _try(lambda d=d: (d.native_class.get_name() if d.native_class else None)) or "?"
        classes[nc] = classes.get(nc, 0) + 1
        for a in _try(lambda d=d: list(d.data_layer_assets) or [], []):
            k = str(a)
            dl_refs[k] = dl_refs.get(k, 0) + 1
    top_classes = sorted(classes.items(), key=lambda kv: -kv[1])[:15]
    issues = []
    if no_guid:
        issues.append("%d descriptor(s) have no resolvable guid" % no_guid)
    print("@@UMCP@@" + json.dumps({"status": "success", "world_partition_present": True,
        "world": (world.get_name() if world else None),
        "actor_desc_count": len(descs),
        "spatially_loaded": spatially, "non_spatially_loaded": nonspatial,
        "editor_only": editor_only, "descriptors_without_guid": no_guid,
        "runtime_grids": grids, "data_layer_asset_references": dl_refs,
        "native_class_histogram": dict(top_classes),
        "editor_world_bounds": ({"min": [eb.min.x, eb.min.y, eb.min.z], "max": [eb.max.x, eb.max.y, eb.max.z]} if eb else None),
        "runtime_world_bounds": ({"min": [rb.min.x, rb.min.y, rb.min.z], "max": [rb.max.x, rb.max.y, rb.max.z]} if rb else None),
        "issues": issues, "ok": (len(issues) == 0),
        "note": "editor-side descriptor consistency summary (read-only). Not a full WorldPartition::CheckForErrors commandlet run; reports descriptor facts reachable from WorldPartitionBlueprintLibrary."}))
'''

    @mcp.tool()
    def validate_world_partition(ctx) -> str:
        """Summarize / validate the active level's World Partition state. Read-only.

        Reports descriptor-level facts from WorldPartitionBlueprintLibrary: total actor
        descriptors, spatially-loaded vs not, editor-only count, descriptors missing a guid,
        a runtime-grid histogram, referenced data-layer assets, a native-class histogram, and
        the editor/runtime world bounds. 'issues'/'ok' flag descriptor anomalies.

        NOTE: this is an editor-side descriptor summary, NOT the full WorldPartition
        CheckForErrors commandlet. On a non-WP level returns world_partition_present=False."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # load_world_partition_region -- load descriptors in a box (write)   #
    # ------------------------------------------------------------------ #
    _LOAD_BODY = _COERCE_HELPERS + _WP_HELPERS + r'''
box = _box_from(PARAMS.get("region_box"))
if box is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "region_box is required as [minx,miny,minz,maxx,maxy,maxz] or {min:[..],max:[..]}; loading the whole world is refused (too heavy)"}))
elif not _wp_present():
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "active level is not World-Partition enabled"}))
else:
    wpbl = _wpbl()
    descs = _descs_for(box)
    guids = []
    texts = []
    for d in descs:
        t = _guid_text(d.guid)
        if t:
            guids.append(_guid_from_text(t)); texts.append(t)
    if not guids:
        print("@@UMCP@@" + json.dumps({"status": "success", "loaded_count": 0,
            "region_box": _box_list(box), "note": "no descriptors intersect the region; nothing loaded"}))
    else:
        with unreal.ScopedEditorTransaction("MCP load_world_partition_region"):
            wpbl.load_actors(guids)
        _ledger().append({"op": "wp_load_actors", "guids": texts, "box": _box_list(box)})
        print("@@UMCP@@" + json.dumps({"status": "success", "loaded_count": len(guids),
            "region_box": _box_list(box), "guids": texts[:50], "guid_total": len(texts),
            "ledger_depth": len(_ledger()),
            "note": "loaded via WorldPartitionBlueprintLibrary.load_actors(guids). Inverse (wp_load_actors) unloads exactly these guids."}))
'''

    @mcp.tool()
    def load_world_partition_region(ctx, region_box) -> str:
        """Load all World Partition actors whose descriptors intersect a region. Ledgered write.

        region_box: [minx,miny,minz,maxx,maxy,maxz] OR {"min":[x,y,z],"max":[x,y,z]} (REQUIRED --
                    loading the whole world is refused as too heavy).

        Resolves descriptors via get_intersecting_actor_descs(box), extracts their GUIDs, and
        calls WorldPartitionBlueprintLibrary.load_actors(guids). Records op 'wp_load_actors'
        {guids:[hex...], box} so the unified undo unloads EXACTLY those guids
        (unload_actors(rebuilt guids))."""
        try:
            return json.dumps(_exec(_LOAD_BODY, {"region_box": region_box}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # unload_world_partition_region -- unload descriptors in a box       #
    # ------------------------------------------------------------------ #
    _UNLOAD_BODY = _COERCE_HELPERS + _WP_HELPERS + r'''
box = _box_from(PARAMS.get("region_box"))
if box is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "region_box is required as [minx,miny,minz,maxx,maxy,maxz] or {min:[..],max:[..]}"}))
elif not _wp_present():
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "active level is not World-Partition enabled"}))
else:
    wpbl = _wpbl()
    descs = _descs_for(box)
    guids = []
    texts = []
    for d in descs:
        t = _guid_text(d.guid)
        if t:
            guids.append(_guid_from_text(t)); texts.append(t)
    if not guids:
        print("@@UMCP@@" + json.dumps({"status": "success", "unloaded_count": 0,
            "region_box": _box_list(box), "note": "no descriptors intersect the region; nothing unloaded"}))
    else:
        with unreal.ScopedEditorTransaction("MCP unload_world_partition_region"):
            wpbl.unload_actors(guids)
        _ledger().append({"op": "wp_unload_actors", "guids": texts, "box": _box_list(box)})
        print("@@UMCP@@" + json.dumps({"status": "success", "unloaded_count": len(guids),
            "region_box": _box_list(box), "guids": texts[:50], "guid_total": len(texts),
            "ledger_depth": len(_ledger()),
            "note": "unloaded via WorldPartitionBlueprintLibrary.unload_actors(guids). Inverse (wp_unload_actors) reloads exactly these guids."}))
'''

    @mcp.tool()
    def unload_world_partition_region(ctx, region_box) -> str:
        """Unload all World Partition actors whose descriptors intersect a region. Ledgered write.

        region_box: [minx,miny,minz,maxx,maxy,maxz] OR {"min":[x,y,z],"max":[x,y,z]} (REQUIRED).

        Resolves descriptors via get_intersecting_actor_descs(box), extracts their GUIDs, and
        calls WorldPartitionBlueprintLibrary.unload_actors(guids). Records op 'wp_unload_actors'
        {guids:[hex...], box} so the unified undo reloads EXACTLY those guids
        (load_actors(rebuilt guids))."""
        try:
            return json.dumps(_exec(_UNLOAD_BODY, {"region_box": region_box}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # pin_world_partition_actors -- pin/unpin actors by guid (write)     #
    # ------------------------------------------------------------------ #
    _PIN_BODY = _COERCE_HELPERS + _WP_HELPERS + r'''
pin = bool(PARAMS.get("pin", True))
req = PARAMS.get("guids") or []
if not _wp_present():
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "active level is not World-Partition enabled"}))
elif not req:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "guids is required (list of guid hex strings from list_world_partition_actors, or actor labels/names)"}))
else:
    wpbl = _wpbl()
    # Build a label/name -> guid-hex map from descriptors so callers can pass either.
    by_ident = {}
    for d in _descs_for(None):
        t = _guid_text(d.guid)
        if not t:
            continue
        by_ident[t] = t
        for k in (_try(lambda d=d: str(d.label)), _try(lambda d=d: str(d.name))):
            if k:
                by_ident.setdefault(k, t)
    texts = []
    not_found = []
    for ident in req:
        h = by_ident.get(str(ident))
        if h is None:
            # maybe already a valid hex not in the (box-less) desc set
            if isinstance(ident, str) and len(ident) >= 16 and all(c in "0123456789ABCDEFabcdef" for c in ident):
                h = ident
        if h is None:
            not_found.append(str(ident))
        elif h not in texts:
            texts.append(h)
    if not texts:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no requested identifiers resolved to descriptors",
            "not_found": not_found}))
    else:
        guids = [_guid_from_text(t) for t in texts]
        op = "wp_pin_actors" if pin else "wp_unpin_actors"
        with unreal.ScopedEditorTransaction("MCP pin_world_partition_actors"):
            if pin:
                wpbl.pin_actors(guids)
            else:
                wpbl.unpin_actors(guids)
        _ledger().append({"op": op, "guids": texts})
        print("@@UMCP@@" + json.dumps({"status": "success", "pinned": pin,
            "count": len(texts), "guids": texts, "not_found": not_found,
            "ledger_depth": len(_ledger()),
            "note": "pin_actors keeps actors loaded across streaming; op '%s' inverse is the opposite (unpin/pin) on exactly these guids." % op}))
'''

    @mcp.tool()
    def pin_world_partition_actors(ctx, guids: list, pin: bool = True) -> str:
        """Pin (or unpin) World Partition actors so they stay loaded across streaming. Ledgered write.

        guids: list of actor GUID hex strings (as returned by list_world_partition_actors) and/or
               actor labels/names, which are resolved to their descriptor GUIDs.
        pin:   True to pin (default) via WorldPartitionBlueprintLibrary.pin_actors; False to unpin
               via unpin_actors.

        Records op 'wp_pin_actors' (or 'wp_unpin_actors' when pin=False) {guids:[hex...]} so the
        unified undo applies the opposite op to exactly those guids."""
        params = {"guids": guids, "pin": pin}
        try:
            return json.dumps(_exec(_PIN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_spatially_loaded -- per-actor UPROPERTY (write)          #
    # ------------------------------------------------------------------ #
    _SET_SPATIAL_BODY = _COERCE_HELPERS + _WP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    want = bool(PARAMS.get("value", True))
    try:
        prior = bool(actor.get_editor_property("is_spatially_loaded"))
    except Exception as e:
        prior = None
    if prior is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "actor has no is_spatially_loaded property (not a WP-partitioned actor?): %s" % PARAMS["actor_name"]}))
    else:
        with unreal.ScopedEditorTransaction("MCP set_actor_spatially_loaded"):
            actor.set_editor_property("is_spatially_loaded", want)
        after = bool(actor.get_editor_property("is_spatially_loaded"))
        if after != prior:
            _ledger().append({"op": "set_actor_spatially_loaded",
                "actor_name": actor.get_name(), "prior": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
            "label": actor.get_actor_label(), "before": prior, "after": after,
            "changed": (after != prior), "ledger_depth": len(_ledger()),
            "note": "is_spatially_loaded set via set_editor_property (no python C++ SetIsSpatiallyLoaded exists). Inverse restores the captured prior bool."}))
'''

    @mcp.tool()
    def set_actor_spatially_loaded(ctx, actor_name: str, value: bool = True) -> str:
        """Set a World Partition actor's is_spatially_loaded flag. Ledgered write.

        actor_name: actor display label (preferred) or unique internal name.
        value:      True (default) makes the actor stream with the grid; False makes it always-loaded.

        Written via actor.set_editor_property('is_spatially_loaded', value) inside a transaction
        (no python-exposed C++ SetIsSpatiallyLoaded). Captures the prior bool; op
        'set_actor_spatially_loaded' {actor_name, prior} is only recorded if the value changed."""
        params = {"actor_name": actor_name, "value": value}
        try:
            return json.dumps(_exec(_SET_SPATIAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_runtime_grid -- per-actor FName UPROPERTY (write)        #
    # ------------------------------------------------------------------ #
    _SET_GRID_BODY = _COERCE_HELPERS + _WP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    grid = PARAMS.get("grid_name")
    grid = "None" if grid is None else str(grid)
    try:
        prior = str(actor.get_editor_property("runtime_grid"))
    except Exception as e:
        prior = None
    if prior is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "actor has no runtime_grid property (not a WP-partitioned actor?): %s" % PARAMS["actor_name"]}))
    else:
        with unreal.ScopedEditorTransaction("MCP set_actor_runtime_grid"):
            actor.set_editor_property("runtime_grid", unreal.Name(grid))
        after = str(actor.get_editor_property("runtime_grid"))
        if after != prior:
            _ledger().append({"op": "set_actor_runtime_grid",
                "actor_name": actor.get_name(), "prior": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
            "label": actor.get_actor_label(), "before": prior, "after": after,
            "changed": (after != prior), "ledger_depth": len(_ledger()),
            "note": "runtime_grid (FName) set via set_editor_property. 'None' clears it to the default grid. Inverse restores the captured prior FName."}))
'''

    @mcp.tool()
    def set_actor_runtime_grid(ctx, actor_name: str, grid_name: str = "None") -> str:
        """Set a World Partition actor's runtime_grid (the streaming grid it belongs to). Ledgered write.

        actor_name: actor display label (preferred) or unique internal name.
        grid_name:  target runtime grid FName; 'None' (default) clears to the default grid.

        Written via actor.set_editor_property('runtime_grid', Name(grid_name)) inside a
        transaction. Captures the prior FName as a string; op 'set_actor_runtime_grid'
        {actor_name, prior} is only recorded if the value changed."""
        params = {"actor_name": actor_name, "grid_name": grid_name}
        try:
            return json.dumps(_exec(_SET_GRID_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
