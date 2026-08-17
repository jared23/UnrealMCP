"""UserTools :: Data Tables  (spec: docs/spec/datatables.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8), built on
`unreal.DataTableFunctionLibrary`, `EditorAssetLibrary.load_asset`, and the AssetRegistry.

This batch is READ-ONLY (query commands). Writes (create/add/update/delete/rename/
duplicate rows, CSV/JSON import) are DEFERRED — see module footer / report.

Serialization backbone: UDataTable rows are not exposed to Python as struct instances,
so per-row field values are read via DataTableFunctionLibrary.export_data_table_to_json_string,
which the engine serializes faithfully (object refs -> "/Script/...'/path'", enums/numbers/
bools/strings native). We parse that JSON and key rows by their "Name" entry. Column display
(export) names come from get_data_table_column_export_names; the mangled internal UPROPERTY
names (Name_GUID) from get_data_table_column_names — both are surfaced by the schema command.

Query convention (copied from editor_level.py): a snippet prints  @@UMCP@@<json>  on ONE
line; _query() finds that marker and parses the JSON after it. Every snippet is wrapped with
Output-Log auto-capture, so new Warning/Error lines surface as result['_log_warnings'].

Implemented (all read-only):
  - find_data_tables            (AssetRegistry search for UDataTable assets)
  - get_data_table_info         (row struct, row count, row names, columns)
  - get_data_table_schema       (column export names + internal names + inferred types)
  - get_data_table_rows         (all rows, serialized; row-name filter + pagination)
  - get_data_table_row          (one row by name)
  - find_data_table_rows        (rows whose field matches a value; substring/exact)
  - list_data_table_row_structs (UserDefinedStruct assets usable as row structs + in-use structs)

HARD CONSTRAINTS honored: snippet bodies contain NO triple-single-quotes and NO stray
backslashes; all params travel as base64 JSON via _exec.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim pattern from editor_level.py) ----------
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

    # Shared Unreal-side helpers for DataTables. No ''' / no backslashes.
    # _load_dt(path)         -> (dt, err): load an asset and verify it is a UDataTable.
    # _rows_json(dt)         -> list[dict]: parse the engine JSON export (rows keyed by "Name").
    # _infer_type(v)         -> str: JSON python-type label for schema hints.
    # _asset_records(...)    -> AssetRegistry -> list of DataTable records.
    _DT_HELPERS = r'''
import unreal, json, warnings, fnmatch
warnings.simplefilter("ignore")
_DFL = unreal.DataTableFunctionLibrary
def _load_dt(path):
    if not path:
        return None, "no data_table_path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.DataTable):
        return None, "asset is not a DataTable (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _row_names(dt):
    try:
        return [str(x) for x in _DFL.get_data_table_row_names(dt)]
    except Exception:
        return []
def _export_names(dt):
    try:
        return [str(x) for x in _DFL.get_data_table_column_export_names(dt)]
    except Exception:
        return []
def _column_names(dt):
    try:
        return [str(x) for x in _DFL.get_data_table_column_names(dt)]
    except Exception:
        return []
def _rows_json(dt):
    # Returns list of {row_name, fields{...}} using the engine JSON export as serializer.
    try:
        js = _DFL.export_data_table_to_json_string(dt)
    except Exception as e:
        return None, "json export failed: %s" % e
    if not js:
        return [], None
    try:
        arr = json.loads(js)
    except Exception as e:
        return None, "json parse failed: %s" % e
    rows = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        nm = entry.get("Name")
        fields = dict(entry)
        fields.pop("Name", None)
        rows.append({"row_name": str(nm) if nm is not None else None, "fields": fields})
    return rows, None
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
def _struct_of(dt):
    try:
        rs = _DFL.get_data_table_row_struct(dt)
    except Exception:
        rs = None
    if rs is None:
        return {"name": None, "path": None}
    return {"name": rs.get_name(), "path": rs.get_path_name()}
def _row_struct(dt):
    try:
        return _DFL.get_data_table_row_struct(dt)
    except Exception:
        return None
# --- Native C++ row-struct field reflection (MCPReflectionLibrary) -------------
# get_struct_fields_json(<UScriptStruct>) exposes real member cpp_type / flags /
# category / tooltip. hasattr-guarded: absence falls back to value-inferred types.
_HAS_MRL_STRUCT = (hasattr(unreal, "MCPReflectionLibrary") and
                   hasattr(unreal.MCPReflectionLibrary, "get_struct_fields_json"))
def _clean_field_name(nm):
    # Strip a UserDefinedStruct member's <Name>_<num>_<32-hex-guid> suffix to a display name.
    if not nm:
        return nm
    parts = nm.split("_")
    if len(parts) >= 3:
        last = parts[-1]; mid = parts[-2]
        if mid.isdigit() and len(last) >= 24 and all(c in "0123456789abcdefABCDEF" for c in last):
            return "_".join(parts[:-2])
    return nm
def _fields_via_reflection(struct):
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
    # find_data_tables — AssetRegistry search for UDataTable assets       #
    # ------------------------------------------------------------------ #
    _FIND_DT_BODY = _DT_HELPERS + r'''
path_filter = PARAMS.get("path_filter")
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
flt = unreal.ARFilter(class_names=["DataTable"], recursive_classes=True)
try:
    assets = ar.get_assets(flt)
except Exception as e:
    assets = []
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
out = []
total_scanned = 0
for a in assets:
    total_scanned += 1
    nm = str(a.asset_name)
    pkg = str(a.package_name)
    if not _match(nm, name_filter):
        continue
    if path_filter and not _match(pkg, path_filter):
        continue
    out.append({"name": nm, "path": pkg, "object_path": pkg + "." + nm})
    if max_results and len(out) >= int(max_results):
        break
out.sort(key=lambda r: r["path"])
print("@@UMCP@@" + json.dumps({"status": "success", "total_data_tables": total_scanned,
    "returned": len(out), "data_tables": out}))
'''

    @mcp.tool()
    def find_data_tables(ctx, path_filter: str = None, name_filter: str = None,
                         max_results: int = None) -> str:
        """Find UDataTable assets in the project via the AssetRegistry. Read-only.

        path_filter: case-insensitive substring/glob on the package path (e.g. '/Game/').
        name_filter: case-insensitive substring/glob on the asset name.
        max_results: cap the number returned.

        Returns each table's name, package path, and object path. With no filters it
        lists every DataTable (the response also reports the total scanned)."""
        params = {"path_filter": path_filter, "name_filter": name_filter,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_FIND_DT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_data_table_info — struct, row count, row names, columns          #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _DT_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    row_names = _row_names(dt)
    print("@@UMCP@@" + json.dumps({"status": "success",
        "data_table_path": dt.get_path_name(),
        "name": dt.get_name(),
        "row_struct": _struct_of(dt),
        "row_count": len(row_names),
        "row_names": row_names,
        "column_export_names": _export_names(dt),
        "column_internal_names": _column_names(dt)}))
'''

    @mcp.tool()
    def get_data_table_info(ctx, data_table_path: str) -> str:
        """Summarize a DataTable: its row struct (name + path), row count, all row names,
        and column names (both display/export names and mangled internal UPROPERTY names).
        Read-only.

        data_table_path: package or object path to the DataTable asset."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"data_table_path": data_table_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_data_table_schema — columns with names + inferred types          #
    # ------------------------------------------------------------------ #
    _SCHEMA_BODY = _DT_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    exports = _export_names(dt)
    # map export -> internal name
    exp_to_col = {}
    for en in exports:
        try:
            exp_to_col[en] = str(_DFL.get_data_table_column_name_from_export_name(dt, en))
        except Exception:
            exp_to_col[en] = None
    # infer types from the first row that has a non-null value per column
    rows, rerr = _rows_json(dt)
    type_hint = {}
    if rows:
        for en in exports:
            th = "unknown"
            for r in rows:
                if en in r["fields"]:
                    v = r["fields"][en]
                    th = _infer_type(v)
                    if v is not None and th != "null":
                        break
            type_hint[en] = th
    # Real field types from the row struct via the native C++ handler (authoritative),
    # matched to columns by field name / cleaned display name. Falls back to inference.
    refl = _fields_via_reflection(_row_struct(dt))
    refl_by_name = {}
    if refl:
        for f in refl:
            if f.get("name"):
                refl_by_name[f["name"]] = f
            if f.get("display_name"):
                refl_by_name.setdefault(f["display_name"], f)
    columns = []
    for en in exports:
        col = {"export_name": en, "internal_name": exp_to_col.get(en),
               "inferred_type": type_hint.get(en, "unknown")}
        rf = refl_by_name.get(en)
        if rf is None:
            inm = exp_to_col.get(en)
            if inm:
                rf = refl_by_name.get(inm) or refl_by_name.get(_clean_field_name(inm))
        if rf is not None:
            col["cpp_type"] = rf.get("cpp_type")
            col["field_name"] = rf.get("name")
            col["flags"] = rf.get("flags") or []
            if "category" in rf:
                col["category"] = rf["category"]
            if "tooltip" in rf:
                col["tooltip"] = rf["tooltip"]
        columns.append(col)
    types_source = "MCPReflectionLibrary" if refl else "value_inference"
    if refl:
        _note = ("cpp_type/flags/category/tooltip are REAL types read from the row struct via the "
                 "native MCPReflectionLibrary.get_struct_fields_json C++ FProperty handler "
                 "(authoritative); inferred_type is retained (derived from serialized row values). "
                 "field_name is the compiled member name (may carry a UE GUID suffix).")
    else:
        _note = ("inferred_type is derived from serialized JSON row values (UStruct field "
                 "types are not reflectable in Python); null when the table has no rows")
    print("@@UMCP@@" + json.dumps({"status": "success",
        "data_table_path": dt.get_path_name(),
        "row_struct": _struct_of(dt),
        "row_count": len(rows) if rows is not None else None,
        "column_count": len(columns),
        "types_source": types_source,
        "columns": columns,
        "note": _note}))
'''

    @mcp.tool()
    def get_data_table_schema(ctx, data_table_path: str) -> str:
        """Get a DataTable's column schema: for each column its display/export name, mangled
        internal UPROPERTY name, and type. Read-only.

        When the native MCPReflectionLibrary.get_struct_fields_json handler is present
        (types_source == 'MCPReflectionLibrary'), each column also carries the REAL C++ type
        (cpp_type) plus UPROPERTY flags/category/tooltip read from the row struct, alongside the
        retained value-inferred type. Fallback (types_source == 'value_inference'): types are
        inferred from serialized row values (bool/int/float/string/array/struct). For an empty
        table columns are still listed (from the row struct); inferred_type is 'unknown' but
        cpp_type is still real when the handler is present.

        data_table_path: package or object path to the DataTable asset."""
        try:
            return json.dumps(_exec(_SCHEMA_BODY, {"data_table_path": data_table_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_data_table_rows — all rows, serialized (filter + pagination)     #
    # ------------------------------------------------------------------ #
    _ROWS_BODY = _DT_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rows, rerr = _rows_json(dt)
    if rerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": rerr}))
    else:
        wanted = PARAMS.get("row_names")
        if wanted:
            wset = set(str(x) for x in wanted)
            rows = [r for r in rows if r["row_name"] in wset]
        total = len(rows)
        start = int(PARAMS.get("cursor") or 0)
        max_rows = PARAMS.get("max_rows")
        window = rows[start:start + int(max_rows)] if max_rows else rows[start:]
        nxt = start + len(window)
        result = {}
        for r in window:
            result[r["row_name"]] = r["fields"]
        print("@@UMCP@@" + json.dumps({"status": "success",
            "data_table_path": dt.get_path_name(),
            "row_struct": _struct_of(dt),
            "total_rows": total, "returned": len(window),
            "next_cursor": (nxt if nxt < total else None),
            "rows": result}))
'''

    @mcp.tool()
    def get_data_table_rows(ctx, data_table_path: str, row_names: list = None,
                            max_rows: int = None, cursor: int = 0) -> str:
        """Read the rows of a DataTable as serialized field values. Read-only.

        data_table_path: package or object path to the DataTable asset.
        row_names:       optional list restricting output to these rows.
        max_rows/cursor: paginate; the response returns 'next_cursor' (pass it back as
                         cursor to continue) or null when exhausted.

        Rows are keyed by row name; each value is a dict of column export-name -> value.
        Object/asset references serialize as engine path strings; enums/numbers/bools/strings
        come through natively."""
        params = {"data_table_path": data_table_path, "row_names": row_names,
                  "max_rows": max_rows, "cursor": cursor}
        try:
            return json.dumps(_exec(_ROWS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_data_table_row — a single row by name                            #
    # ------------------------------------------------------------------ #
    _ROW_BODY = _DT_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    row_name = PARAMS.get("row_name")
    exists = False
    try:
        exists = _DFL.does_data_table_row_exist(dt, row_name)
    except Exception:
        exists = False
    if not exists:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "row not found: %s" % row_name,
            "available_rows": _row_names(dt)}))
    else:
        rows, rerr = _rows_json(dt)
        if rerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": rerr}))
        else:
            match = None
            for r in rows:
                if r["row_name"] == str(row_name):
                    match = r; break
            print("@@UMCP@@" + json.dumps({"status": "success",
                "data_table_path": dt.get_path_name(),
                "row_struct": _struct_of(dt),
                "row_name": str(row_name),
                "fields": (match["fields"] if match else {})}))
'''

    @mcp.tool()
    def get_data_table_row(ctx, data_table_path: str, row_name: str) -> str:
        """Read a single DataTable row by name, as serialized field values. Read-only.

        data_table_path: package or object path to the DataTable asset.
        row_name:        the row's name.

        If the row is absent the response lists the available row names."""
        params = {"data_table_path": data_table_path, "row_name": row_name}
        try:
            return json.dumps(_exec(_ROW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_data_table_rows — rows whose field matches a value              #
    # ------------------------------------------------------------------ #
    _FINDROWS_BODY = _DT_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    field = PARAMS.get("field")
    value = PARAMS.get("value")
    exact = bool(PARAMS.get("exact"))
    max_results = PARAMS.get("max_results")
    rows, rerr = _rows_json(dt)
    if rerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": rerr}))
    else:
        exports = _export_names(dt)
        if field and field not in exports:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no such column: %s" % field, "columns": exports}))
        else:
            needle = str(value).lower() if value is not None else None
            matched = []
            for r in rows:
                cols = [field] if field else list(r["fields"].keys())
                hit = False
                for c in cols:
                    if c not in r["fields"]:
                        continue
                    cell = r["fields"][c]
                    cell_s = json.dumps(cell) if isinstance(cell, (list, dict)) else str(cell)
                    if needle is None:
                        hit = True
                    elif exact:
                        if cell_s == str(value):
                            hit = True
                    elif needle in cell_s.lower():
                        hit = True
                    if hit:
                        break
                if hit:
                    matched.append({"row_name": r["row_name"], "fields": r["fields"]})
                    if max_results and len(matched) >= int(max_results):
                        break
            print("@@UMCP@@" + json.dumps({"status": "success",
                "data_table_path": dt.get_path_name(),
                "field": field, "value": value, "exact": exact,
                "total_rows": len(rows), "matched": len(matched), "rows": matched}))
'''

    @mcp.tool()
    def find_data_table_rows(ctx, data_table_path: str, field: str = None,
                             value=None, exact: bool = False,
                             max_results: int = None) -> str:
        """Find rows in a DataTable whose field matches a value. Read-only.

        data_table_path: package or object path to the DataTable asset.
        field:           column export-name to test; if omitted, matches against ANY column.
        value:           value to look for. Default (substring, case-insensitive) matches on
                         the cell's serialized string; set exact=True for an exact string match.
                         Omit value to return all rows that have the given field.
        max_results:     cap the number of matched rows.

        Returns matched rows with their full serialized field values."""
        params = {"data_table_path": data_table_path, "field": field, "value": value,
                  "exact": exact, "max_results": max_results}
        try:
            return json.dumps(_exec(_FINDROWS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_data_table_row_structs — usable row structs                     #
    # ------------------------------------------------------------------ #
    _ROWSTRUCTS_BODY = _DT_HELPERS + r'''
filt = PARAMS.get("filter")
include_user_defined = PARAMS.get("include_user_defined")
if include_user_defined is None:
    include_user_defined = True
max_results = PARAMS.get("max_results")
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
structs = []
seen = set()
# (a) UserDefinedStruct content assets (the only structs the AssetRegistry enumerates)
if include_user_defined:
    try:
        assets = ar.get_assets(unreal.ARFilter(class_names=["UserDefinedStruct"], recursive_classes=True))
    except Exception:
        assets = []
    for a in assets:
        nm = str(a.asset_name); pkg = str(a.package_name)
        if not _match(nm, filt):
            continue
        key = pkg + "." + nm
        if key in seen:
            continue
        seen.add(key)
        structs.append({"name": nm, "path": pkg + "." + nm, "source": "user_defined_struct"})
# (b) structs actually in use as row structs by existing DataTables (covers native ones)
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
    nm = rs.get_name(); pth = rs.get_path_name()
    if not _match(nm, filt):
        continue
    if pth in seen:
        continue
    seen.add(pth)
    structs.append({"name": nm, "path": pth, "source": "in_use_by_data_table"})
structs.sort(key=lambda s: s["name"])
total = len(structs)
if max_results:
    structs = structs[:int(max_results)]
print("@@UMCP@@" + json.dumps({"status": "success", "total": total,
    "returned": len(structs), "row_structs": structs,
    "note": "AssetRegistry enumerates UserDefinedStruct content assets; native FTableRowBase "
            "structs are not enumerable in Python, so those are surfaced only when already in "
            "use by a DataTable"}))
'''

    @mcp.tool()
    def list_data_table_row_structs(ctx, filter: str = None,
                                    include_user_defined: bool = True,
                                    max_results: int = None) -> str:
        """List struct types usable as DataTable row structs. Read-only.

        filter:               case-insensitive substring/glob on the struct name.
        include_user_defined: include UserDefinedStruct content assets (default True).
        max_results:          cap the number returned.

        Combines UserDefinedStruct assets (from the AssetRegistry) with any structs already
        in use as row structs by existing DataTables (this is how native FTableRowBase structs
        surface, since Python cannot enumerate native structs). Each entry notes its source."""
        params = {"filter": filter, "include_user_defined": include_user_defined,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_ROWSTRUCTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (writes; not implemented this batch):
    #   create_data_table, add_data_table_row, update_data_table_row,
    #   delete_data_table_row, rename_data_table_row, duplicate_data_table_row,
    #   and CSV/JSON import (fill_data_table_from_csv/json_*).
    # DataTableFunctionLibrary exposes remove_data_table_row / rename_data_table_row /
    #   fill_* for these; a write batch must add ScopedEditorTransaction + a per-session
    #   ledger inverse (round-trip via export/import) and auto-save.
    # ------------------------------------------------------------------ #
