"""UserTools :: Data Tables (WRITE)  (spec: docs/spec/datatables.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE-wave module, the
mutating counterpart to datatables.py (READ). Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py / the shipped write module curves_write.py.

Row-authoring mechanism (verified live vs TestMCPSetup, UE 5.8.1):
  UDataTable rows are NOT exposed to Python as struct instances, and the 5.8 Python API has NO
  add_data_table_row / rename_data_table_row and no native C++ row-writer (MCPReflectionLibrary
  exposes only struct-field helpers, not row helpers). So row authoring goes through the engine's
  own JSON serializer -- the SAME backbone datatables.py READ uses:
    * READ current rows: DataTableFunctionLibrary.export_data_table_to_json_string(dt)
        -> a JSON array of {"Name": <row>, <export-col>: <value>, ...} dicts.
    * WRITE: mutate that array in memory, then
        DataTableFunctionLibrary.fill_data_table_from_json_string(dt, json.dumps(array)).
      fill_* has REPLACE semantics (it empties + recreates every row from the array), and the
      export->import round-trip is faithful (proven live: reimporting the exact exported JSON
      leaves row names/values/order identical). Single-row removal uses the direct
      DataTableFunctionLibrary.remove_data_table_row(dt, name).
  Because we mutate the JSON representation, incoming field values are coerced to the row struct's
  field types by the ENGINE's authoritative JSON importer (not Python reflection) -- rows aren't
  struct instances in Python, so this is the only faithful coercion path (same reason the READ
  module serializes via JSON export). Field keys are the column EXPORT/display names (exactly as
  they appear in datatables.get_data_table_rows / get_data_table_schema.export_name).

Write safety: every mutation runs inside an unreal.ScopedEditorTransaction and calls dt.modify()
to mark the package dirty, but does NOT auto-save (the user's table is left dirty for them to save
or discard). Each mutation records a precise inverse op on the per-session agent ledger
(builtins._UMCP_LEDGERS[session]); the FULL prior row state is captured for remove/update so the
inverse restores it byte-for-byte. This module registers NO `undo` tool -- editor_level.py owns
the ONE unified `undo`; the 5 inverse ops below are reported to the coordinator to fold there.

Implemented (all validated live; session-aware; editor left CLEAN, ledger depth 0):
  - add_data_table_row       (WRITE; ledgered op "datatable_add_row")
  - remove_data_table_row    (WRITE; ledgered op "datatable_remove_row")
  - update_data_table_row    (WRITE; ledgered op "datatable_update_row")
  - rename_data_table_row    (WRITE; ledgered op "datatable_rename_row")
  - duplicate_data_table_row (WRITE; ledgered op "datatable_duplicate_row")

HARD CONSTRAINTS honored: snippet bodies contain NO triple-single-quotes and NO stray backslashes;
all params travel as base64 JSON via _exec; no reserved local names are assigned.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-single-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


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

    # Shared Unreal-side helpers (prepended to write bodies). No triple-single-quote / no backslash.
    #   _ledger()          -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_dt(path)     -> (dt, err): load + verify a UDataTable.
    #   _export_list(dt)   -> list[dict]: the engine JSON export parsed to row dicts (each with Name).
    #   _import_list(dt,l) -> bool: replace the whole table from a row-dict list (fill_* semantics).
    #   _find_idx(l, name) -> int index of the row dict whose Name==name, else -1.
    #   _mark(dt)          -> dt.modify() (dirty the package WITHOUT saving).
    _DTW_HELPERS = r'''
import unreal, json, builtins
_DFL = unreal.DataTableFunctionLibrary
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
def _row_exists(dt, name):
    try:
        return bool(_DFL.does_data_table_row_exist(dt, name))
    except Exception:
        return name in _row_names(dt)
def _export_list(dt):
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
    out = []
    for e in arr:
        if isinstance(e, dict):
            out.append(dict(e))
    return out, None
def _import_list(dt, lst):
    return bool(_DFL.fill_data_table_from_json_string(dt, json.dumps(lst)))
def _find_idx(lst, name):
    for i, e in enumerate(lst):
        if str(e.get("Name")) == str(name):
            return i
    return -1
def _mark(dt):
    try:
        dt.modify()
    except Exception:
        pass
'''

    # ------------------------------------------------------------------ #
    # add_data_table_row — author a new row (JSON round-trip; ledgered)    #
    # ------------------------------------------------------------------ #
    _ADD_BODY = _DTW_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    row_name = str(PARAMS.get("row_name") or "").strip()
    fields = PARAMS.get("fields") or {}
    if not row_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "row_name is required"}))
    elif _row_exists(dt, row_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "row already exists: %s (use update_data_table_row)" % row_name}))
    else:
        cols = _export_names(dt)
        bad = [k for k in fields.keys() if k not in cols]
        if bad:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "unknown column(s): %s" % bad, "valid_columns": cols}))
        else:
            lst, lerr = _export_list(dt)
            if lerr:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
            else:
                entry = {"Name": row_name}
                for k, v in fields.items():
                    entry[k] = v
                new_lst = list(lst) + [entry]
                with unreal.ScopedEditorTransaction("MCP add_data_table_row"):
                    _mark(dt)
                    ok = _import_list(dt, new_lst)
                if not _row_exists(dt, row_name):
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "import did not create row (fill returned %s)" % ok}))
                else:
                    _ledger().append({"op": "datatable_add_row",
                        "asset_path": dt.get_path_name(), "row_name": row_name})
                    written, werr = _export_list(dt)
                    got = None
                    for e in (written or []):
                        if str(e.get("Name")) == row_name:
                            got = {k: v for k, v in e.items() if k != "Name"}; break
                    print("@@UMCP@@" + json.dumps({"status": "success",
                        "data_table_path": dt.get_path_name(), "row_name": row_name,
                        "row_count": len(_row_names(dt)), "fields": got,
                        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_data_table_row(ctx, data_table_path: str, row_name: str, fields: dict = None) -> str:
        """Add a new row to a DataTable (ledgered write). Refuses if the row already exists.

        data_table_path: package or object path to the DataTable asset.
        row_name:        name for the new row (unique within the table).
        fields:          dict of column -> value. Keys are the column EXPORT/display names
                         (exactly as shown by get_data_table_schema.export_name /
                         get_data_table_rows). Omitted columns take the row-struct defaults.
                         Values are coerced to the field types by the engine's JSON importer:
                         numbers/bools/strings pass through; enums as the member name string;
                         object/asset refs as the engine path string; nested structs as dicts.

        Mechanism: the table is rewritten from its JSON export plus this row (the 5.8 Python API
        has no add_data_table_row; fill_data_table_from_json_string is the faithful path). The
        table is marked dirty but NOT saved.

        Ledgered op 'datatable_add_row' {asset_path, row_name}. Inverse (fold into
        editor_level.undo): DataTableFunctionLibrary.remove_data_table_row(dt, row_name) inside a
        transaction + dt.modify() -- the row was absent before, so removal is a FAITHFUL inverse."""
        params = {"data_table_path": data_table_path, "row_name": row_name, "fields": fields or {}}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_data_table_row — delete a row (captures prior; ledgered)      #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _DTW_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    row_name = str(PARAMS.get("row_name") or "").strip()
    if not _row_exists(dt, row_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "row not found: %s" % row_name, "available_rows": _row_names(dt)}))
    else:
        lst, lerr = _export_list(dt)
        if lerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
        else:
            idx = _find_idx(lst, row_name)
            prior_entry = dict(lst[idx]) if idx >= 0 else {"Name": row_name}
            with unreal.ScopedEditorTransaction("MCP remove_data_table_row"):
                _mark(dt)
                _DFL.remove_data_table_row(dt, row_name)
            if _row_exists(dt, row_name):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "remove_data_table_row did not remove %s" % row_name}))
            else:
                _ledger().append({"op": "datatable_remove_row", "asset_path": dt.get_path_name(),
                    "row_name": row_name, "prior_entry": prior_entry, "prior_index": idx})
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "data_table_path": dt.get_path_name(), "row_name": row_name,
                    "removed_fields": {k: v for k, v in prior_entry.items() if k != "Name"},
                    "row_count": len(_row_names(dt)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_data_table_row(ctx, data_table_path: str, row_name: str) -> str:
        """Remove a row from a DataTable (ledgered write). Refuses if the row is absent.

        data_table_path: package or object path to the DataTable asset.
        row_name:        the row to remove.

        Uses DataTableFunctionLibrary.remove_data_table_row. The FULL prior row (all field
        values + its position) is captured before removal so the inverse restores it exactly.
        The table is marked dirty but NOT saved.

        Ledgered op 'datatable_remove_row' {asset_path, row_name, prior_entry, prior_index}.
        Inverse (fold into editor_level.undo): re-insert prior_entry at prior_index via the JSON
        round-trip (export list -> insert -> fill_data_table_from_json_string) -- FAITHFUL restore
        of the row's values AND its original order."""
        params = {"data_table_path": data_table_path, "row_name": row_name}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # update_data_table_row — partial field update (captures prior)       #
    # ------------------------------------------------------------------ #
    _UPDATE_BODY = _DTW_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    row_name = str(PARAMS.get("row_name") or "").strip()
    fields = PARAMS.get("fields") or {}
    if not _row_exists(dt, row_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "row not found: %s" % row_name, "available_rows": _row_names(dt)}))
    elif not fields:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no fields to update"}))
    else:
        cols = _export_names(dt)
        bad = [k for k in fields.keys() if k not in cols]
        if bad:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "unknown column(s): %s" % bad, "valid_columns": cols}))
        else:
            lst, lerr = _export_list(dt)
            if lerr:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
            else:
                idx = _find_idx(lst, row_name)
                prior_entry = dict(lst[idx])
                new_entry = dict(prior_entry)
                for k, v in fields.items():
                    new_entry[k] = v
                lst[idx] = new_entry
                with unreal.ScopedEditorTransaction("MCP update_data_table_row"):
                    _mark(dt)
                    _import_list(dt, lst)
                written, werr = _export_list(dt)
                got = None
                for e in (written or []):
                    if str(e.get("Name")) == row_name:
                        got = {k: v for k, v in e.items() if k != "Name"}; break
                _ledger().append({"op": "datatable_update_row", "asset_path": dt.get_path_name(),
                    "row_name": row_name, "prior_entry": prior_entry})
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "data_table_path": dt.get_path_name(), "row_name": row_name,
                    "updated_columns": list(fields.keys()),
                    "before": {k: v for k, v in prior_entry.items() if k != "Name"},
                    "after": got, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def update_data_table_row(ctx, data_table_path: str, row_name: str, fields: dict) -> str:
        """Update fields of an existing DataTable row (partial; ledgered write). Only the columns
        you pass are changed; the rest keep their current values. Refuses if the row is absent.

        data_table_path: package or object path to the DataTable asset.
        row_name:        the row to update.
        fields:          dict of column -> new value (column EXPORT/display names). Values are
                         coerced by the engine's JSON importer (see add_data_table_row).

        Mechanism: the row's current values are read from the JSON export, the given fields are
        merged, and the table is rewritten via fill_data_table_from_json_string. Marked dirty, NOT
        saved.

        Ledgered op 'datatable_update_row' {asset_path, row_name, prior_entry}. Inverse (fold into
        editor_level.undo): export list, replace the row whose Name==row_name with prior_entry
        (the FULL captured prior row), fill_data_table_from_json_string -- FAITHFUL field-for-field
        restore."""
        params = {"data_table_path": data_table_path, "row_name": row_name, "fields": fields or {}}
        try:
            return json.dumps(_exec(_UPDATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_data_table_row — change a row's name (ledgered)              #
    # ------------------------------------------------------------------ #
    _RENAME_BODY = _DTW_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    old_name = str(PARAMS.get("old_row_name") or "").strip()
    new_name = str(PARAMS.get("new_row_name") or "").strip()
    if not new_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_row_name is required"}))
    elif not _row_exists(dt, old_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "row not found: %s" % old_name, "available_rows": _row_names(dt)}))
    elif _row_exists(dt, new_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "target row name already exists: %s" % new_name}))
    else:
        lst, lerr = _export_list(dt)
        if lerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
        else:
            idx = _find_idx(lst, old_name)
            lst[idx] = dict(lst[idx]); lst[idx]["Name"] = new_name
            with unreal.ScopedEditorTransaction("MCP rename_data_table_row"):
                _mark(dt)
                _import_list(dt, lst)
            if _row_exists(dt, new_name) and not _row_exists(dt, old_name):
                _ledger().append({"op": "datatable_rename_row", "asset_path": dt.get_path_name(),
                    "old_name": old_name, "new_name": new_name})
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "data_table_path": dt.get_path_name(), "old_row_name": old_name,
                    "new_row_name": new_name, "row_count": len(_row_names(dt)),
                    "ledger_depth": len(_ledger())}))
            else:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "rename did not take effect (old=%s new=%s)" % (
                        _row_exists(dt, old_name), _row_exists(dt, new_name))}))
'''

    @mcp.tool()
    def rename_data_table_row(ctx, data_table_path: str, old_row_name: str, new_row_name: str) -> str:
        """Rename a DataTable row, preserving its field values and position (ledgered write).
        Refuses if the source row is absent or the target name already exists.

        data_table_path: package or object path to the DataTable asset.
        old_row_name:    the row's current name.
        new_row_name:    the new name.

        Mechanism: the 5.8 Python API has no rename_data_table_row, so the row's "Name" is changed
        in the JSON export and the table is rewritten via fill_data_table_from_json_string (fields
        and order preserved). Marked dirty, NOT saved.

        Ledgered op 'datatable_rename_row' {asset_path, old_name, new_name}. Inverse (fold into
        editor_level.undo): the same JSON round-trip renaming new_name back to old_name -- FAITHFUL."""
        params = {"data_table_path": data_table_path, "old_row_name": old_row_name,
                  "new_row_name": new_row_name}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # duplicate_data_table_row — copy a row under a new name (ledgered)    #
    # ------------------------------------------------------------------ #
    _DUP_BODY = _DTW_HELPERS + r'''
dt, err = _load_dt(PARAMS.get("data_table_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    src = str(PARAMS.get("source_row_name") or "").strip()
    new_name = str(PARAMS.get("new_row_name") or "").strip()
    if not new_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_row_name is required"}))
    elif not _row_exists(dt, src):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "source row not found: %s" % src, "available_rows": _row_names(dt)}))
    elif _row_exists(dt, new_name):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "target row name already exists: %s" % new_name}))
    else:
        lst, lerr = _export_list(dt)
        if lerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
        else:
            idx = _find_idx(lst, src)
            entry = dict(lst[idx]); entry["Name"] = new_name
            new_lst = lst[:idx + 1] + [entry] + lst[idx + 1:]
            with unreal.ScopedEditorTransaction("MCP duplicate_data_table_row"):
                _mark(dt)
                _import_list(dt, new_lst)
            if not _row_exists(dt, new_name):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "duplicate did not create row: %s" % new_name}))
            else:
                _ledger().append({"op": "datatable_duplicate_row", "asset_path": dt.get_path_name(),
                    "new_row_name": new_name})
                written, werr = _export_list(dt)
                got = None
                for e in (written or []):
                    if str(e.get("Name")) == new_name:
                        got = {k: v for k, v in e.items() if k != "Name"}; break
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "data_table_path": dt.get_path_name(), "source_row_name": src,
                    "new_row_name": new_name, "fields": got,
                    "row_count": len(_row_names(dt)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def duplicate_data_table_row(ctx, data_table_path: str, source_row_name: str,
                                 new_row_name: str) -> str:
        """Duplicate a DataTable row under a new name (ledgered write). The copy carries all of the
        source row's field values and is inserted right after it. Refuses if the source is absent
        or the target name already exists.

        data_table_path: package or object path to the DataTable asset.
        source_row_name: the row to copy.
        new_row_name:    name for the new copy.

        Mechanism: the source row's values are read from the JSON export, copied under the new
        name, and the table is rewritten via fill_data_table_from_json_string. Marked dirty, NOT
        saved.

        Ledgered op 'datatable_duplicate_row' {asset_path, new_row_name}. Inverse (fold into
        editor_level.undo): DataTableFunctionLibrary.remove_data_table_row(dt, new_row_name) in a
        transaction + dt.modify() -- the new row didn't exist before, so removal is FAITHFUL."""
        params = {"data_table_path": data_table_path, "source_row_name": source_row_name,
                  "new_row_name": new_row_name}
        try:
            return json.dumps(_exec(_DUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # Five inverse ops to fold into editor_level.undo (reported to the coordinator):
    #   datatable_add_row       {asset_path,row_name}                      -> remove_data_table_row
    #   datatable_remove_row    {asset_path,row_name,prior_entry,prior_index} -> re-insert via fill_*
    #   datatable_update_row    {asset_path,row_name,prior_entry}          -> restore row via fill_*
    #   datatable_rename_row    {asset_path,old_name,new_name}             -> rename back via fill_*
    #   datatable_duplicate_row {asset_path,new_row_name}                  -> remove_data_table_row
    # ------------------------------------------------------------------ #
