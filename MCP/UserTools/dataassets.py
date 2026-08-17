"""UserTools :: Data Assets  (spec: docs/spec/dataassets.md)

Clean-room reimplementation over Unreal's PUBLIC Python API (UE 5.8):
`unreal.AssetRegistryHelpers` (Asset Registry), `unreal.EditorAssetLibrary`,
`unreal.PrimaryAssetType` / `unreal.PrimaryAssetId`, and (hasattr-guarded) the
native `unreal.MCPReflectionLibrary` FProperty reflection helper.

Scope: UDataAsset / UPrimaryDataAsset discovery + AssetManager primary-asset READS.
This module is READ-ONLY (no writes, no ledger, no undo tool).

Executed in the editor's interpreter via the plugin's `execute_python` handler,
which returns captured stdout under result["output"] (CRLF). Query convention
(copied verbatim from editor_level.py / assets.py): a snippet prints @@UMCP@@<json>
on ONE line; _query() finds the marker and parses the JSON after it. Params are
injected as base64 JSON (via _exec) to survive the handler wrapping code in
triple-single-quotes.

PROBE findings on THIS project (TestMCPSetup, live 2026-08-16):
  - 23 UDataAsset-subclass instances under /Game, ALL engine subclasses
    (InputAction, InputMappingContext, StateTree, BlackboardData, EnvQuery) -
    there are NO plain custom UDataAssets and ZERO UPrimaryDataAsset instances.
  - `unreal.AssetManager` (the UAssetManager singleton with .get()) is NOT bound
    in Python; neither is `unreal.AssetManagerSettings`. So the CONFIGURED
    PrimaryAssetTypesToScan list cannot be enumerated directly.
  - `unreal.PrimaryAssetType(name).get_primary_asset_id_list()` IS bound and routes
    into the C++ UAssetManager - it works, but returns 0 ids for every candidate
    type (Map / PrimaryAssetLabel / DataAsset / ...), i.e. the AssetManager has no
    registered/scanned primary assets in this template project.
  - Plain UDataAssets (e.g. InputAction) do NOT expose get_primary_asset_id();
    only UPrimaryDataAsset subclasses self-report a PrimaryAssetId. There is no
    Python-reachable UAssetManager::GetPrimaryAssetIdForObject for the general case.
These limitations are reported honestly by the tools below (not faked / not empty-
faked): the enumeration + property reads stand on their own real data.

What this module deliberately does NOT duplicate:
  - Generic asset listing / ARFilter search / dir listing -> assets.py
    (find_assets, list_assets). Here we always constrain to UDataAsset subclasses.
  - Generic full UPROPERTY reflection dump -> objects.py (describe_object) and
    assets.py (get_asset_properties). get_data_asset_info is the data-asset-FRAMED
    convenience: it gates on is-a-UDataAsset, adds is_primary_data_asset +
    primary_asset_id + class hierarchy, and returns a concise typed property view.
  - UClass enumeration / parent-class queries -> the native `search_classes` tool.

NB: snippet bodies must contain NO triple-single-quotes and NO stray backslashes
(the handler wraps incoming code in '''...''' before exec).
"""
import json
import base64
import textwrap

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / assets.py) -------
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


# Shared Unreal-side helpers (prepended to bodies that need them).
# _ser(v, depth): JSON-safe serialization of any Unreal value.
# _prop_names(obj): reflected UPROPERTY names across the MRO.
# _ad_class(ad): asset class short-name from an AssetData row (no asset load).
# _snake(s): C++ camel/Pascal name -> snake_case (to index native metadata by
#            the Python snake property names). No ''' and no backslashes here.
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
def _ad_class(ad):
    try:
        cp = ad.get_editor_property("asset_class_path")
        return str(cp.asset_name)
    except Exception:
        try: return str(ad.asset_class)
        except Exception: return None
def _snake(s):
    o = []
    for i, ch in enumerate(s):
        if ch.isupper() and i > 0 and ((not s[i-1].isupper()) or (i + 1 < len(s) and s[i+1].islower())):
            o.append("_")
        o.append(ch.lower())
    return "".join(o)
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
    # list_data_assets — enumerate UDataAsset(/UPrimaryDataAsset) insts   #
    # ------------------------------------------------------------------ #
    _LIST_DA_BODY = _SER_HELPERS + r'''
path = PARAMS.get("path") or "/Game"
class_filter = PARAMS.get("class_filter")
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
primary_only = bool(PARAMS.get("primary_only"))
max_results = PARAMS.get("max_results")
base = "PrimaryDataAsset" if primary_only else "DataAsset"
ar = unreal.AssetRegistryHelpers.get_asset_registry()
# Match the base class AND all subclasses via a class-path filter (no asset load).
tp = unreal.TopLevelAssetPath("/Script/Engine", base)
flt = unreal.ARFilter(class_paths=[tp], recursive_classes=True,
                      package_paths=[path], recursive_paths=recursive)
ads = ar.get_assets(flt) or []
cf = class_filter.lower() if class_filter else None
out = []
scanned = 0
classes = {}
for ad in ads:
    scanned += 1
    cls = _ad_class(ad)
    if cf is not None and cf not in (cls or "").lower():
        continue
    classes[cls] = classes.get(cls, 0) + 1
    if not (max_results and len(out) >= int(max_results)):
        out.append({"name": str(ad.get_editor_property("asset_name")),
                    "path": str(ad.get_editor_property("package_name")),
                    "class": cls})
print("@@UMCP@@" + json.dumps({"status": "success", "base_class": base, "path": path,
    "recursive": recursive, "scanned": scanned, "matched": sum(classes.values()),
    "returned": len(out), "class_counts": classes, "data_assets": out}))
'''

    @mcp.tool()
    def list_data_assets(ctx, path: str = None, class_filter: str = None,
                         primary_only: bool = False, recursive: bool = True,
                         max_results: int = None) -> str:
        """List UDataAsset instances in the project via the Asset Registry (read-only,
        no asset load). Enumerates the UDataAsset base class AND all subclasses
        (InputAction, InputMappingContext, StateTree, BlackboardData, EnvQuery, and any
        custom UDataAsset/UPrimaryDataAsset). Distinct from assets.find_assets/list_assets:
        this is constrained to the UDataAsset hierarchy and reports per-class counts.

        path:         package path to search under (default '/Game').
        class_filter: case-insensitive substring on the asset's class short name
                      (e.g. 'Input', 'StateTree'). Optional.
        primary_only: restrict to UPrimaryDataAsset subclasses only (default False).
        recursive:    recurse into subdirectories (default True).
        max_results:  cap the number of assets returned (counts still reflect all matches).

        Returns each asset's {name, path, class}, plus 'class_counts' and 'scanned'."""
        params = {"path": path, "class_filter": class_filter, "primary_only": primary_only,
                  "recursive": recursive, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_DA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_data_asset_info — data-asset-framed load + typed property view  #
    # ------------------------------------------------------------------ #
    _GET_DA_BODY = _SER_HELPERS + r'''
asset_path = PARAMS["asset_path"]
flt = PARAMS.get("filter")
depth = int(PARAMS.get("max_depth") or 1)
max_props = PARAMS.get("max_properties")
cursor = int(PARAMS.get("cursor") or 0)
eal = unreal.EditorAssetLibrary
if not eal.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    obj = eal.load_asset(asset_path)
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "failed to load asset: %s" % asset_path}))
    elif not isinstance(obj, unreal.DataAsset):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "not a UDataAsset: %s" % asset_path,
            "class": obj.get_class().get_name(),
            "hint": "use assets.get_asset_properties or objects.describe_object for non-DataAsset objects"}))
    else:
        is_primary = isinstance(obj, unreal.PrimaryDataAsset)
        hierarchy = []
        for k in type(obj).__mro__:
            nm = getattr(k, "__name__", None)
            if nm and nm != "object" and nm not in hierarchy:
                hierarchy.append(nm)
        primary_id = None
        primary_id_valid = None
        if hasattr(obj, "get_primary_asset_id"):
            try:
                _pid = obj.get_primary_asset_id()
                if _pid is not None:
                    primary_id_valid = bool(_pid.is_valid())
                    primary_id = _pid.to_string()
            except Exception:
                primary_id = None
        # Native FProperty metadata (cpp_type / category / UPROPERTY flags), fully guarded.
        meta_by_name = {}
        meta_reached = False
        if hasattr(unreal, "MCPReflectionLibrary") and hasattr(unreal.MCPReflectionLibrary, "get_object_property_metadata_json"):
            try:
                _md = json.loads(unreal.MCPReflectionLibrary.get_object_property_metadata_json(obj, include_inherited=True))
                for _mp in (_md.get("properties") or []):
                    _cn = _mp.get("name")
                    if not _cn:
                        continue
                    meta_by_name[_cn] = _mp
                    meta_by_name.setdefault(_snake(_cn), _mp)
                    if len(_cn) > 1 and _cn[0] == "b" and _cn[1:2].isupper():
                        meta_by_name.setdefault(_snake(_cn[1:]), _mp)
                meta_reached = True
            except Exception:
                meta_by_name = {}
                meta_reached = False
        names = _prop_names(obj)
        if flt:
            fl = flt.lower(); names = [n for n in names if fl in n.lower()]
        total = len(names)
        window = names[cursor:cursor + int(max_props)] if max_props else names[cursor:]
        props = []
        for n in window:
            rec = {"name": n}
            _nm = meta_by_name.get(n)
            if _nm is not None:
                rec["cpp_type"] = _nm.get("cpp_type")
                if "category" in _nm:
                    rec["category"] = _nm.get("category")
                if _nm.get("flags"):
                    rec["flags"] = _nm.get("flags")
            try:
                val = obj.get_editor_property(n)
                rec["py_type"] = type(val).__name__
                rec["value"] = _ser(val, depth)
            except Exception:
                rec["py_type"] = None
                rec["value"] = "<unreadable>"
            props.append(rec)
        nxt = cursor + len(window)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
            "name": obj.get_name(), "class": obj.get_class().get_name(),
            "is_data_asset": True, "is_primary_data_asset": bool(is_primary),
            "primary_asset_id": primary_id, "primary_asset_id_valid": primary_id_valid,
            "class_hierarchy": hierarchy,
            "metadata_source": ("MCPReflectionLibrary" if meta_reached else "python_reflection_only"),
            "total_properties": total, "returned": len(props),
            "next_cursor": (nxt if nxt < total else None), "properties": props}))
'''

    @mcp.tool()
    def get_data_asset_info(ctx, asset_path: str, filter: str = None,
                            max_properties: int = 80, cursor: int = 0,
                            max_depth: int = 1) -> str:
        """Load a UDataAsset and return a data-asset-framed summary (read-only).

        Refuses non-UDataAsset targets (use assets.get_asset_properties /
        objects.describe_object for those). Adds, beyond a generic property dump:
        is_primary_data_asset, primary_asset_id (only self-reported by UPrimaryDataAsset
        subclasses; null for plain UDataAssets, which have no Python-reachable id), and
        the Python class_hierarchy.

        asset_path:     package or object path of the data asset.
        filter:         case-insensitive substring; only property names containing it.
        max_properties/cursor: paginate the property list; response returns 'next_cursor'
                        (pass it back as cursor) or null.
        max_depth:      struct-expansion depth for values (default 1).

        Per property: name, and (when the native MCPReflectionLibrary helper is present)
        cpp_type / category / flags, plus py_type and the JSON-serialized value."""
        params = {"asset_path": asset_path, "filter": filter,
                  "max_properties": max_properties, "cursor": cursor, "max_depth": max_depth}
        try:
            return json.dumps(_exec(_GET_DA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_primary_asset_types — AssetManager primary-type probe          #
    # ------------------------------------------------------------------ #
    _LIST_PT_BODY = r'''
import unreal, json
extra = PARAMS.get("candidate_types") or []
defaults = ["Map", "PrimaryAssetLabel"]
cands = []
for t in (defaults + list(extra)):
    if t and t not in cands:
        cands.append(t)
results = []
total_ids = 0
for t in cands:
    rec = {"type": t}
    try:
        pt = unreal.PrimaryAssetType(t)
        ids = pt.get_primary_asset_id_list()
        n = len(ids) if ids is not None else 0
        rec["registered_id_count"] = n
        total_ids += n
    except Exception as e:
        rec["error"] = str(e)
    results.append(rec)
configured = [r for r in results if r.get("registered_id_count", 0) > 0]
print("@@UMCP@@" + json.dumps({"status": "success",
    "note": "The UAssetManager singleton and UAssetManagerSettings are NOT exposed to Python in this build, so the configured PrimaryAssetTypesToScan list cannot be enumerated directly. This probes each candidate type via unreal.PrimaryAssetType.get_primary_asset_id_list() (a real AssetManager query) and reports how many primary assets are registered per type. Pass candidate_types to probe additional type names.",
    "candidates_probed": cands, "types_with_registered_assets": len(configured),
    "total_registered_ids": total_ids, "results": results}))
'''

    @mcp.tool()
    def list_primary_asset_types(ctx, candidate_types: list = None) -> str:
        """Probe the AssetManager for registered PrimaryAssetTypes (read-only).

        The UAssetManager singleton / UAssetManagerSettings are not Python-reachable in
        this build, so the *configured* PrimaryAssetTypesToScan list cannot be enumerated
        directly. Instead this queries a candidate set of type names via
        unreal.PrimaryAssetType.get_primary_asset_id_list() (a genuine AssetManager call)
        and reports how many primary assets are registered for each. Honest empty result
        when nothing is registered (the common template-project case).

        candidate_types: optional extra type-name strings to probe (defaults always include
                         'Map' and 'PrimaryAssetLabel').

        Returns per-type registered_id_count plus totals."""
        params = {"candidate_types": candidate_types or []}
        try:
            return json.dumps(_exec(_LIST_PT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_primary_asset_ids — ids registered for a primary asset type     #
    # ------------------------------------------------------------------ #
    _GET_PIDS_BODY = r'''
import unreal, json
t = PARAMS["primary_asset_type"]
max_results = PARAMS.get("max_results")
try:
    pt = unreal.PrimaryAssetType(t)
except Exception as e:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad primary asset type '%s': %s" % (t, e)}))
else:
    ids = pt.get_primary_asset_id_list() or []
    out = []
    for pid in ids:
        try:
            out.append(pid.to_string())
        except Exception:
            out.append(str(pid))
        if max_results and len(out) >= int(max_results):
            break
    note = None
    if len(ids) == 0:
        note = "No primary assets are registered for this type. Either the type is not configured in the AssetManager, or no assets have been scanned for it (this template project registers none)."
    print("@@UMCP@@" + json.dumps({"status": "success", "primary_asset_type": t,
        "count": len(ids), "returned": len(out), "primary_asset_ids": out, "note": note}))
'''

    @mcp.tool()
    def get_primary_asset_ids(ctx, primary_asset_type: str, max_results: int = None) -> str:
        """List the PrimaryAssetIds registered for a given PrimaryAssetType (read-only),
        via unreal.PrimaryAssetType(type).get_primary_asset_id_list() (a real AssetManager
        query). Returns each id as a 'Type:Name' string.

        primary_asset_type: the type name (e.g. 'Map', 'PrimaryAssetLabel').
        max_results:        cap the number returned (count still reflects all).

        Honestly returns count 0 (with a note) when nothing is registered for the type."""
        params = {"primary_asset_type": primary_asset_type, "max_results": max_results}
        try:
            return json.dumps(_exec(_GET_PIDS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_primary_asset_id — resolve one asset's self-reported PrimaryId  #
    # ------------------------------------------------------------------ #
    _GET_PID_BODY = r'''
import unreal, json
asset_path = PARAMS["asset_path"]
eal = unreal.EditorAssetLibrary
if not eal.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    obj = eal.load_asset(asset_path)
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "failed to load asset: %s" % asset_path}))
    else:
        cls = obj.get_class().get_name()
        is_da = isinstance(obj, unreal.DataAsset)
        is_primary = isinstance(obj, unreal.PrimaryDataAsset)
        if hasattr(obj, "get_primary_asset_id"):
            try:
                pid = obj.get_primary_asset_id()
                res = {"status": "success", "asset_path": asset_path, "class": cls,
                       "is_data_asset": bool(is_da), "is_primary_data_asset": bool(is_primary),
                       "primary_asset_id": pid.to_string(), "is_valid": bool(pid.is_valid())}
                try:
                    res["primary_asset_type"] = str(pid.get_editor_property("primary_asset_type"))
                    res["primary_asset_name"] = str(pid.get_editor_property("primary_asset_name"))
                except Exception:
                    pass
                print("@@UMCP@@" + json.dumps(res))
            except Exception as e:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "get_primary_asset_id failed: %s" % e, "class": cls}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
                "class": cls, "is_data_asset": bool(is_da), "is_primary_data_asset": False,
                "primary_asset_id": None,
                "note": "This asset does not self-report a PrimaryAssetId (only UPrimaryDataAsset subclasses do). The general UAssetManager::GetPrimaryAssetIdForObject path is not exposed to Python in this build, so no id can be resolved for a plain UDataAsset."}))
'''

    @mcp.tool()
    def get_primary_asset_id(ctx, asset_path: str) -> str:
        """Resolve an asset's PrimaryAssetId (read-only).

        Only UPrimaryDataAsset subclasses self-report a PrimaryAssetId (via
        get_primary_asset_id()); for those this returns the 'Type:Name' id, its validity,
        and the parsed type/name. Plain UDataAssets have no Python-reachable id (the
        general UAssetManager resolver isn't bound) - reported honestly with a note.

        asset_path: package or object path of the asset."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_GET_PID_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
