"""UserTools :: Blueprint Structs  (spec: docs/spec/structs.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.

What a UserDefinedStruct asset exposes to stock Python is limited (learned live):
  * The AssetRegistry DOES enumerate `UserDefinedStruct` content assets -> list/find works.
  * Native C++ FStructs (e.g. FVector, FTableRowBase subclasses) are NOT enumerable as assets;
    they only surface indirectly (e.g. when in use as a DataTable row struct).
  * A UserDefinedStruct's FIELD schema (member names/types/defaults) is NOT reachable via stock
    reflection: the struct asset object only reflects UserDefinedStruct's own UPROPERTYs (Guid,
    StructFlags, ...), not the user-defined members. The new C++ helper
    `unreal.MCPReflectionLibrary` targets a UClass/UObject (get_class_metadata_json /
    get_object_property_metadata_json) and CANNOT nativize a UStruct as a Class (verified live:
    'Cannot nativize UserDefinedStruct as Class') -> it does NOT help for struct fields.

So field discovery here is done via the one reliable, modal-free path: if the struct is used as
the row struct of any DataTable, the engine's DataTable JSON serializer
(DataTableFunctionLibrary.export_data_table_to_json_string) + the column export/internal names
expose the member names (and let us infer member types from serialized values). For structs not
used by any DataTable, member schema is reported as unreachable-from-Python with the precise
reason (a C++ FProperty/TFieldIterator handler over the UUserDefinedStruct is required).

Implemented (all read-only):
  - list_blueprint_structs   (AssetRegistry: UserDefinedStruct assets; ranked/paginated)
  - find_structs             (alias-style richer finder over the same source)
  - describe_blueprint_struct (member names/types/defaults where reachable; honest limits)
  - find_struct_usage        (which DataTables use a given struct as their row struct)

Deferred (writes; NOT implemented this batch): create_blueprint_struct,
  add_blueprint_struct_variable, set_blueprint_struct_variable, remove_blueprint_struct_variable.
No ledger / no `undo` tool here (this batch performs no mutations).
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

    # Shared Unreal-side helpers for structs. No ''' / no backslashes in this body.
    # _match(s, pat)      -> substring/glob (case-insensitive) name match.
    # _load_struct(path)  -> (struct, err): load an asset and verify it is a UScriptStruct.
    # _infer_type(v)      -> JSON python-type label for member type hints.
    # _tables_using(struct) -> list of {name, path, dt} DataTables whose row struct == struct.
    # _fields_via_datatable(struct) -> {fields, source, via_data_table} or None.
    _STRUCT_HELPERS = r'''
import unreal, json, warnings, fnmatch
warnings.simplefilter("ignore")
_DFL = unreal.DataTableFunctionLibrary
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _load_struct(path):
    if not path:
        return None, "no struct_path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.ScriptStruct):
        return None, "asset is not a struct (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _infer_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "struct"
    return type(v).__name__
def _tables_using(struct):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        dts = ar.get_assets(unreal.ARFilter(class_names=["DataTable"], recursive_classes=True))
    except Exception:
        dts = []
    spath = struct.get_path_name()
    out = []
    for a in dts:
        pkg = str(a.package_name)
        try:
            dt = unreal.EditorAssetLibrary.load_asset(pkg)
            rs = _DFL.get_data_table_row_struct(dt) if dt else None
        except Exception:
            dt = None; rs = None
        if rs is not None and rs.get_path_name() == spath:
            out.append({"name": dt.get_name(), "path": pkg, "dt": dt})
    return out
def _fields_via_datatable(struct):
    tables = _tables_using(struct)
    if not tables:
        return None, []
    dt = tables[0]["dt"]
    try:
        exp = [str(x) for x in _DFL.get_data_table_column_export_names(dt)]
    except Exception:
        exp = []
    try:
        intn = [str(x) for x in _DFL.get_data_table_column_names(dt)]
    except Exception:
        intn = []
    types = {}; defaults = {}
    try:
        js = _DFL.export_data_table_to_json_string(dt)
        arr = json.loads(js) if js else []
    except Exception:
        arr = []
    if arr and isinstance(arr[0], dict):
        row = arr[0]
        for k, v in row.items():
            if k == "Name":
                continue
            types[k] = _infer_type(v)
            defaults[k] = v
    fields = []
    for i, name in enumerate(exp):
        fields.append({
            "name": name,
            "internal_name": (intn[i] if i < len(intn) else None),
            "type": types.get(name, "unknown"),
            "sample_value": defaults.get(name),
        })
    meta = {"source": "datatable_serialization",
            "via_data_table": tables[0]["path"],
            "also_used_by": [t["path"] for t in tables[1:]]}
    return meta, fields
def _struct_identity(struct):
    ident = {"name": struct.get_name(), "path": struct.get_path_name()}
    try:
        g = struct.get_editor_property("guid")
        ident["guid"] = str(g)
    except Exception:
        ident["guid"] = None
    return ident
# --- Native C++ struct-field reflection (MCPReflectionLibrary) -----------------
# get_struct_fields_json(<UScriptStruct>) -> JSON string with real member cpp_type,
# UPROPERTY flags, and (where declared) category/tooltip. hasattr-guarded so absence
# falls back to the value-inference path unchanged.
_HAS_MRL_STRUCT = (hasattr(unreal, "MCPReflectionLibrary") and
                   hasattr(unreal.MCPReflectionLibrary, "get_struct_fields_json"))
def _clean_field_name(nm):
    # UserDefinedStruct members carry a UE suffix: <Name>_<num>_<32-hex-guid>.
    # Strip it to a display name; native struct names have no suffix (returned as-is).
    if not nm:
        return nm
    parts = nm.split("_")
    if len(parts) >= 3:
        last = parts[-1]; mid = parts[-2]
        if mid.isdigit() and len(last) >= 24 and all(c in "0123456789abcdefABCDEF" for c in last):
            return "_".join(parts[:-2])
    return nm
def _fields_via_reflection(struct):
    # Returns authoritative field list from the C++ handler, or None if unavailable.
    if struct is None or not _HAS_MRL_STRUCT:
        return None
    try:
        js = unreal.MCPReflectionLibrary.get_struct_fields_json(struct)
        d = json.loads(js) if isinstance(js, str) else js
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("fields"), list):
        return None
    out = []
    for f in d["fields"]:
        if not isinstance(f, dict):
            continue
        nm = f.get("name")
        rec = {"name": nm, "display_name": _clean_field_name(nm),
               "cpp_type": f.get("cpp_type"), "flags": f.get("flags") or []}
        if f.get("category") is not None:
            rec["category"] = f.get("category")
        if f.get("tooltip") is not None:
            rec["tooltip"] = f.get("tooltip")
        out.append(rec)
    return out
'''

    # ------------------------------------------------------------------ #
    # list_blueprint_structs — UserDefinedStruct assets (ranked/paginated) #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _STRUCT_HELPERS + r'''
filt = PARAMS.get("filter")
path_filter = PARAMS.get("path_filter")
max_results = PARAMS.get("max_results")
cursor = int(PARAMS.get("cursor") or 0)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
try:
    assets = ar.get_assets(unreal.ARFilter(class_names=["UserDefinedStruct"], recursive_classes=True))
except Exception as e:
    assets = []
rows = []
seen = set()
for a in assets:
    nm = str(a.asset_name); pkg = str(a.package_name)
    full = pkg + "." + nm
    if filt and not _match(nm, filt):
        continue
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    if full in seen:
        continue
    seen.add(full)
    # simple relevance rank: exact ci name match first, then prefix, then contains
    rank = 3
    if filt:
        fl = filt.lower(); nl = nm.lower()
        if nl == fl: rank = 0
        elif nl.startswith(fl): rank = 1
        else: rank = 2
    rows.append({"name": nm, "path": full, "package": pkg, "_rank": rank})
rows.sort(key=lambda r: (r["_rank"], r["name"].lower()))
for r in rows:
    r.pop("_rank", None)
total = len(rows)
window = rows[cursor:cursor + int(max_results)] if max_results else rows[cursor:]
nxt = cursor + len(window)
print("@@UMCP@@" + json.dumps({"status": "success", "class": "UserDefinedStruct",
    "total": total, "returned": len(window), "next_cursor": (nxt if nxt < total else None),
    "structs": window,
    "note": "Only UserDefinedStruct content assets are enumerable. Native C++ FStructs "
            "(FVector, FTableRowBase subclasses, ...) are not asset-enumerable in Python; "
            "use find_struct_usage / list_data_table_row_structs to surface in-use native structs."}))
'''

    @mcp.tool()
    def list_blueprint_structs(ctx, filter: str = None, path_filter: str = None,
                               max_results: int = None, cursor: int = 0) -> str:
        """List UserDefinedStruct (Blueprint struct) assets in the project. Read-only.

        filter:      case-insensitive substring/glob on the struct name (also ranks results).
        path_filter: case-insensitive substring on the package path (e.g. '/Game').
        max_results/cursor: paginate; response returns 'next_cursor' (pass back as cursor) or null.

        Note: only UserDefinedStruct *content assets* are enumerable via the AssetRegistry.
        Native C++ structs are not asset-enumerable in Python."""
        params = {"filter": filter, "path_filter": path_filter,
                  "max_results": max_results, "cursor": cursor}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_structs — richer finder (adds in-use native structs, optional) #
    # ------------------------------------------------------------------ #
    _FIND_BODY = _STRUCT_HELPERS + r'''
filt = PARAMS.get("filter")
include_in_use_native = bool(PARAMS.get("include_in_use_native"))
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
structs = []
seen = set()
try:
    assets = ar.get_assets(unreal.ARFilter(class_names=["UserDefinedStruct"], recursive_classes=True))
except Exception:
    assets = []
for a in assets:
    nm = str(a.asset_name); pkg = str(a.package_name)
    if not _match(nm, filt):
        continue
    full = pkg + "." + nm
    if full in seen:
        continue
    seen.add(full)
    structs.append({"name": nm, "path": full, "source": "user_defined_struct"})
# native structs currently in use as a DataTable row struct (only way they surface in Python)
if include_in_use_native:
    try:
        dts = ar.get_assets(unreal.ARFilter(class_names=["DataTable"], recursive_classes=True))
    except Exception:
        dts = []
    for a in dts:
        try:
            dt = unreal.EditorAssetLibrary.load_asset(str(a.package_name))
            rs = _DFL.get_data_table_row_struct(dt) if dt else None
        except Exception:
            rs = None
        if rs is None:
            continue
        pth = rs.get_path_name()
        if pth in seen or "UserDefined" in rs.get_class().get_name():
            continue
        if not _match(rs.get_name(), filt):
            continue
        seen.add(pth)
        structs.append({"name": rs.get_name(), "path": pth, "source": "in_use_by_data_table"})
structs.sort(key=lambda s: s["name"].lower())
total = len(structs)
if max_results:
    structs = structs[:int(max_results)]
print("@@UMCP@@" + json.dumps({"status": "success", "total": total,
    "returned": len(structs), "structs": structs}))
'''

    @mcp.tool()
    def find_structs(ctx, filter: str = None, include_in_use_native: bool = False,
                     max_results: int = None) -> str:
        """Find struct types. Read-only.

        filter:                case-insensitive substring/glob on the struct name.
        include_in_use_native: also include native C++ structs currently used as a DataTable
                               row struct (the only way native structs are visible to Python).
        max_results:           cap the number returned.

        Each entry notes its 'source' (user_defined_struct | in_use_by_data_table)."""
        params = {"filter": filter, "include_in_use_native": include_in_use_native,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_FIND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # describe_blueprint_struct — member names/types/defaults where reachable
    # ------------------------------------------------------------------ #
    _DESCRIBE_BODY = _STRUCT_HELPERS + r'''
path = PARAMS.get("struct_path")
struct, err = _load_struct(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ident = _struct_identity(struct)
    is_user_defined = "UserDefined" in struct.get_class().get_name()
    # Authoritative field schema via the native C++ FProperty handler (real cpp_type +
    # flags + category/tooltip). Falls back to DataTable value-inference if unavailable.
    refl_fields = _fields_via_reflection(struct)
    meta, inf_fields = _fields_via_datatable(struct)
    result = {"status": "success", "struct": ident,
              "class": struct.get_class().get_name(),
              "is_user_defined": is_user_defined}
    if refl_fields is not None:
        result["field_source"] = "MCPReflectionLibrary"
        result["field_count"] = len(refl_fields)
        result["fields"] = refl_fields
        result["field_note"] = ("Member names, real C++ types (cpp_type), UPROPERTY flags and "
            "category/tooltip come from the native MCPReflectionLibrary.get_struct_fields_json "
            "C++ FProperty handler over the UScriptStruct (authoritative). 'name' is the compiled "
            "member name (UserDefinedStruct members carry a UE GUID suffix, e.g. Mesh_6_F250...); "
            "'display_name' strips that suffix. This no longer depends on the struct being used by "
            "a DataTable.")
        if meta is not None:
            # Augment (not replace): note DataTable usage + keep the value-inferred view/sample values.
            result["via_data_table"] = meta["via_data_table"]
            if meta.get("also_used_by"):
                result["also_used_by"] = meta["also_used_by"]
            result["inferred_fields"] = inf_fields
    else:
        # Fallback: existing DataTable-inference behavior, UNCHANGED.
        result["field_count"] = len(inf_fields)
        result["fields"] = inf_fields
        if meta is None:
            result["field_source"] = "unavailable"
            result["field_note"] = ("Member schema is not reachable from stock Python: a "
                "UserDefinedStruct asset reflects only its OWN UPROPERTYs (Guid/StructFlags), not "
                "its user-defined members, and MCPReflectionLibrary targets a UClass/UObject (it "
                "cannot nativize a UStruct as a Class). This struct is not used by any DataTable, so "
                "the DataTable-JSON serializer cannot surface its members either. Precise member "
                "names+types+defaults require a C++ handler (TFieldIterator<FProperty> over the "
                "UUserDefinedStruct).")
        else:
            result["field_source"] = meta["source"]
            result["via_data_table"] = meta["via_data_table"]
            if meta.get("also_used_by"):
                result["also_used_by"] = meta["also_used_by"]
            result["field_note"] = ("Member names + internal names come from the DataTable column "
                "API; member TYPES are INFERRED from a serialized row value (string/int/float/bool/"
                "array/struct), not read from FProperty. 'sample_value' is a real row value, not the "
                "struct's compiled default. Exact declared types/defaults need a C++ FProperty handler.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def describe_blueprint_struct(ctx, struct_path: str) -> str:
        """Describe a struct's members. Read-only.

        struct_path: object path of a struct asset, e.g.
                     '/DatasmithContent/Datasmith/AreaLightsStruct.AreaLightsStruct'.

        Returns the struct identity (name/path/guid) and its member fields. When the native
        MCPReflectionLibrary.get_struct_fields_json C++ handler is present (field_source ==
        'MCPReflectionLibrary'), fields carry REAL cpp_type + UPROPERTY flags + category/tooltip,
        with 'name' (compiled member name; UserDefinedStruct members keep a UE GUID suffix) and a
        cleaned 'display_name' — and this works even for structs not used by any DataTable.
        Fallback (helper absent): member names come from the DataTable column API with types
        INFERRED from serialized row values, and structs unused by any DataTable report
        field_source 'unavailable' (a C++ FProperty handler is required)."""
        try:
            return json.dumps(_exec(_DESCRIBE_BODY, {"struct_path": struct_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_struct_usage — which DataTables use this struct as row struct   #
    # ------------------------------------------------------------------ #
    _USAGE_BODY = _STRUCT_HELPERS + r'''
path = PARAMS.get("struct_path")
struct, err = _load_struct(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    tables = _tables_using(struct)
    print("@@UMCP@@" + json.dumps({"status": "success",
        "struct": _struct_identity(struct),
        "data_table_count": len(tables),
        "data_tables": [{"name": t["name"], "path": t["path"]} for t in tables],
        "note": "Scans every DataTable asset and compares its row struct path. This is also how "
                "a native FTableRowBase struct's membership is discovered (native structs are not "
                "asset-enumerable in Python)."}))
'''

    @mcp.tool()
    def find_struct_usage(ctx, struct_path: str) -> str:
        """Find which DataTables use a given struct as their row struct. Read-only.

        struct_path: object path of the struct asset. Returns the matching DataTables
        (name + path). Useful because a struct-in-use is also the only way its members
        become recoverable in Python (via the DataTable JSON serializer)."""
        try:
            return json.dumps(_exec(_USAGE_BODY, {"struct_path": struct_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (writes; not implemented this batch):
    #   create_blueprint_struct, add_blueprint_struct_variable,
    #   set_blueprint_struct_variable, remove_blueprint_struct_variable.
    # These require editing UUserDefinedStruct member descriptions (the editor-only
    # UUserDefinedStructEditorData / FStructureEditorUtils API), which is NOT exposed to
    # Python and would need a C++ handler, plus ScopedEditorTransaction + per-session ledger
    # inverse + recompile/auto-save. No `undo` tool is defined here (this batch is read-only).
