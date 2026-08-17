"""UserTools :: Asset Management  (spec: docs/spec/assets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8):
`unreal.EditorAssetLibrary`, `unreal.AssetRegistryHelpers` (Asset Registry),
`unreal.EditorUtilityLibrary` (Content Browser selection).

Executed in the editor's interpreter via the plugin's `execute_python` handler,
which returns captured stdout under result["output"] (CRLF).

Query convention (copied from editor_level.py): a snippet prints  @@UMCP@@<json>
on ONE line; _query() finds that marker and parses the JSON after it, so stray
engine log lines on stdout can't corrupt it. Params are injected as base64 JSON
(via _exec) to survive the handler wrapping code in triple-single-quotes.

This module is READ-ONLY (M1 batch). Implemented:
  - find_assets          (Asset Registry search via ARFilter)
  - list_assets          (list a Content Browser directory)
  - get_asset_info       (class / package / object-path metadata)
  - get_asset_properties (full reflected UPROPERTY dump of the loaded asset)
  - get_selected_assets  (current Content Browser selection)
  - find_references      (dependencies / dependents via Asset Registry)

Deferred writes (NOT implemented this round): set_asset_property, save_asset,
save_all, delete_asset, duplicate_asset, rename_asset, import_asset,
import_assets_batch, open_asset, sync_browser.

NB: snippet bodies must contain NO triple-single-quotes and NO stray backslashes
(the handler wraps incoming code in '''...''' before exec).
"""
import json
import base64
import textwrap

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


# Shared Unreal-side serialization helpers (prepended to bodies that need them).
# _ser(v, depth) turns any Unreal value into a JSON-safe form; _prop_names(obj)
# lists reflected UPROPERTY names across the MRO. No ''' and no backslashes here.
_SER_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return sorted(names)
def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        items = []
        for i, e in enumerate(v):
            if i >= 25:
                items.append("...(%d more)" % (len(v) - 25)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:25]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= 25:
                break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try: d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception: d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
def _pkg_of(asset_path):
    p = str(asset_path)
    if "." in p.split("/")[-1]:
        return p.rsplit(".", 1)[0]
    return p
def _ad_class(ad):
    try:
        cp = ad.get_editor_property("asset_class_path")
        return str(cp.asset_name)
    except Exception:
        try: return str(ad.asset_class)
        except Exception: return None
'''


def register_tools(mcp, utils):
    send_command = utils["send_command"]

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
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # find_assets — Asset Registry search via ARFilter (read-only)        #
    # ------------------------------------------------------------------ #
    _FIND_ASSETS_BODY = _SER_HELPERS + r'''
import fnmatch
class_type = PARAMS.get("class_type")
path = PARAMS.get("path")
name_pattern = PARAMS.get("name_pattern")
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
kw = {"recursive_paths": recursive}
if class_type:
    kw["class_names"] = [class_type]
# An ARFilter with neither package_paths nor class_names matches nothing, so
# default the search root to /Game (project content) when no path is given.
kw["package_paths"] = [path] if path else ["/Game"]
flt = unreal.ARFilter(**kw)
ads = ar.get_assets(flt) or []
def _match(nm, pat):
    if not pat:
        return True
    nm = (nm or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(nm, p) or (p in nm)
out = []
total_scanned = 0
for ad in ads:
    total_scanned += 1
    nm = str(ad.get_editor_property("asset_name"))
    if not _match(nm, name_pattern):
        continue
    out.append({
        "name": nm,
        "class": _ad_class(ad),
        "package_name": str(ad.get_editor_property("package_name")),
        "object_path": str(ad.get_full_name()).split(" ", 1)[-1],
    })
    if max_results and len(out) >= int(max_results):
        break
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(out),
    "scanned": total_scanned, "assets": out}))
'''

    @mcp.tool()
    def find_assets(ctx, class_type: str = None, path: str = None,
                    name_pattern: str = None, recursive: bool = True,
                    max_results: int = None) -> str:
        """Search the Asset Registry for assets (read-only).

        class_type:   filter by asset class short name (e.g. 'StaticMesh',
                      'Material', 'Blueprint'). Optional; omit for any class.
        path:         package path to search under (e.g. '/Game/Characters' or
                      '/Engine/BasicShapes'). Optional; omit to search everywhere.
        name_pattern: case-insensitive substring or glob on the asset name.
        recursive:    recurse into subdirectories of `path` (default True).
        max_results:  cap the number of assets returned.

        Returns each asset's name, class, package_name and object_path."""
        params = {"class_type": class_type, "path": path,
                  "name_pattern": name_pattern, "recursive": recursive,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_FIND_ASSETS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_assets — list a Content Browser directory (read-only)         #
    # ------------------------------------------------------------------ #
    _LIST_ASSETS_BODY = _SER_HELPERS + r'''
path = PARAMS.get("path") or "/Game"
class_filter = PARAMS.get("class_filter")
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
eal = unreal.EditorAssetLibrary
if not eal.does_directory_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "directory not found: %s" % path}))
else:
    paths = eal.list_assets(path, recursive, False) or []
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    out = []
    cf = class_filter.lower() if class_filter else None
    for p in paths:
        sp = str(p)
        ad = eal.find_asset_data(sp)
        cls = _ad_class(ad) if ad else None
        if cf is not None and (cls or "").lower() != cf:
            continue
        out.append({"object_path": sp, "class": cls})
    print("@@UMCP@@" + json.dumps({"status": "success", "path": path,
        "count": len(out), "assets": out}))
'''

    @mcp.tool()
    def list_assets(ctx, path: str = None, class_filter: str = None,
                    recursive: bool = True) -> str:
        """List the assets in a Content Browser directory (read-only).

        path:         directory to list (default '/Game'). Must be a package path.
        class_filter: only include assets whose class short name matches exactly
                      (e.g. 'StaticMesh'); case-insensitive. Optional.
        recursive:    recurse into subdirectories (default True).

        Returns each asset's object_path and class."""
        params = {"path": path, "class_filter": class_filter, "recursive": recursive}
        try:
            return json.dumps(_exec(_LIST_ASSETS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_asset_info — class / package / object-path metadata            #
    # ------------------------------------------------------------------ #
    _GET_INFO_BODY = _SER_HELPERS + r'''
asset_path = PARAMS["asset_path"]
eal = unreal.EditorAssetLibrary
if not eal.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    ad = eal.find_asset_data(asset_path)
    info = {"status": "success"}
    info["name"] = str(ad.get_editor_property("asset_name"))
    info["class"] = _ad_class(ad)
    info["package_name"] = str(ad.get_editor_property("package_name"))
    info["object_path"] = str(ad.get_full_name()).split(" ", 1)[-1]
    try: info["package_path"] = str(ad.get_editor_property("package_path"))
    except Exception: info["package_path"] = None
    try: info["is_redirector"] = bool(ad.is_redirector())
    except Exception: info["is_redirector"] = None
    try:
        disk = unreal.Paths.convert_relative_path_to_full(unreal.SystemLibrary.get_system_path(eal.load_asset(asset_path)))
        info["disk_path"] = disk or None
    except Exception:
        info["disk_path"] = None
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_asset_info(ctx, asset_path: str) -> str:
        """Get metadata for one asset (read-only): name, class, package_name,
        object_path, package_path, redirector flag and on-disk path.

        asset_path: package or object path (e.g. '/Engine/BasicShapes/Cube' or
                    '/Game/Characters/.../SKM_Manny')."""
        try:
            return json.dumps(_exec(_GET_INFO_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_asset_properties — full reflected UPROPERTY dump (read-only)    #
    # ------------------------------------------------------------------ #
    _GET_PROPS_BODY = _SER_HELPERS + r'''
asset_path = PARAMS["asset_path"]
flt = PARAMS.get("filter")
depth = int(PARAMS.get("max_depth") or 1)
max_entries = PARAMS.get("max_entries")
cursor = int(PARAMS.get("cursor") or 0)
eal = unreal.EditorAssetLibrary
if not eal.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    obj = eal.load_asset(asset_path)
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % asset_path}))
    else:
        names = _prop_names(obj)
        if flt:
            f = flt.lower(); names = [n for n in names if f in n.lower()]
        total = len(names)
        window = names[cursor:cursor + int(max_entries)] if max_entries else names[cursor:]
        props = {}
        for n in window:
            try: props[n] = _ser(obj.get_editor_property(n), depth)
            except Exception: props[n] = "<unreadable>"
        nxt = cursor + len(window)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
            "class": obj.get_class().get_name(), "total_properties": total,
            "returned": len(props), "next_cursor": (nxt if nxt < total else None),
            "properties": props}))
'''

    @mcp.tool()
    def get_asset_properties(ctx, asset_path: str, filter: str = None,
                             max_depth: int = 1, max_entries: int = 80,
                             cursor: int = 0) -> str:
        """Dump an asset's reflected UPROPERTYs (full reflection, read-only).

        asset_path:  package or object path of the asset to load.
        filter:      case-insensitive substring; only property names containing it.
        max_depth:   how deep to expand nested structs (default 1).
        max_entries/cursor: paginate the property list; response returns
                     'next_cursor' (pass back as cursor to continue) or null.

        Values are JSON-serialized (vectors->[x,y,z], object refs->{__object__: path},
        enums->str). Unreadable properties are marked '<unreadable>'."""
        params = {"asset_path": asset_path, "filter": filter, "max_depth": max_depth,
                  "max_entries": max_entries, "cursor": cursor}
        try:
            return json.dumps(_exec(_GET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_selected_assets — current Content Browser selection (read-only)#
    # ------------------------------------------------------------------ #
    _GET_SELECTED_BODY = _SER_HELPERS + r'''
sel = unreal.EditorUtilityLibrary.get_selected_asset_data() or []
out = []
for ad in sel:
    out.append({
        "name": str(ad.get_editor_property("asset_name")),
        "class": _ad_class(ad),
        "package_name": str(ad.get_editor_property("package_name")),
        "object_path": str(ad.get_full_name()).split(" ", 1)[-1],
    })
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(out), "assets": out}))
'''

    @mcp.tool()
    def get_selected_assets(ctx) -> str:
        """Get the assets currently selected in the editor's Content Browser (read-only).
        Returns each selected asset's name, class, package_name and object_path.
        Returns count 0 if nothing is selected."""
        try:
            return json.dumps(_exec(_GET_SELECTED_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_references — dependencies / dependents (read-only)            #
    # ------------------------------------------------------------------ #
    _FIND_REFS_BODY = _SER_HELPERS + r'''
asset_path = PARAMS["asset_path"]
direction = (PARAMS.get("direction") or "both").lower()
max_results = PARAMS.get("max_results")
eal = unreal.EditorAssetLibrary
pkg = _pkg_of(asset_path)
if not eal.does_asset_exist(asset_path) and not eal.does_directory_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    opts = unreal.AssetRegistryDependencyOptions(
        include_soft_package_references=True,
        include_hard_package_references=True)
    def _cap(lst):
        lst = [str(x) for x in (lst or [])]
        if max_results:
            return lst[:int(max_results)]
        return lst
    result = {"status": "success", "asset_path": asset_path, "package_name": pkg,
              "direction": direction}
    if direction in ("dependencies", "both"):
        result["dependencies"] = _cap(ar.get_dependencies(pkg, opts))
        result["dependencies_count"] = len(result["dependencies"])
    if direction in ("dependents", "referencers", "both"):
        result["dependents"] = _cap(ar.get_referencers(pkg, opts))
        result["dependents_count"] = len(result["dependents"])
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def find_references(ctx, asset_path: str, direction: str = "both",
                        max_results: int = None) -> str:
        """Find an asset's references in the Asset Registry (read-only).

        asset_path: package or object path of the asset.
        direction:  'dependencies' (packages this asset references),
                    'dependents' (packages that reference this asset; alias
                    'referencers'), or 'both' (default).
        max_results: cap each returned list.

        Returns package-name lists for the requested direction(s)."""
        params = {"asset_path": asset_path, "direction": direction,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_FIND_REFS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
