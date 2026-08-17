"""UserTools :: Curves EXTENSION (WRITE)  (spec: docs/spec/curves.md)

Clean-room completion of the Curves surface, the counterpart to curves_write.py (which owns
create_curve_* + set_curve_keys) and curves_read.py (list/get/evaluate). Query convention, base64
PARAMS injection, Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from
the gold-standard editor_level.py.

This module fills every MISSING curve feature from docs/parity/p1_controlrig_veg_curves.md:

  CURVE-ASSET key ops (via the C++ #4 handler unreal.MCPReflectionLibrary get/set_curve_keys_json --
  the curve's FRichCurve is protected to stock Python, so keys go through the compiled handler):
    * delete_curve_keys  -- remove keys by time / clear a whole channel
    * import_curve       -- load keys from JSON or CSV

  CURVE-TABLE ops (via unreal.DataTableFunctionLibrary -- 5.8 exposes a FULL CurveTable API as a
  static function library; the CurveTable's RowMap itself is C++-protected on the UObject, but the
  library reaches it):
    * create_curve_table        (CurveTable or CompositeCurveTable via factory; NON-MODAL)
    * get_curve_table           (READ: row names, per-row keys, evaluations, JSON)
    * set_curve_table_row       (add/replace a simple-curve row)
    * delete_curve_table_row
    * rename_curve_table_row
    * import_curve_table        (bulk REPLACE from JSON or CSV)

  CURVE-ATLAS ops (UCurveLinearColorAtlas.gradient_curves + texture_size ARE public editor
  properties; setting them fires PostEditChangeProperty which rebuilds the atlas texture):
    * create_curve_atlas        (via CurveLinearColorAtlasFactory; NON-MODAL)
    * set_curve_atlas           (add/remove/replace gradient curves; resize)

Live API facts verified vs TestMCPSetup / UE 5.8.1 (2026-08-17):
  - unreal.DataTableFunctionLibrary methods: curve_table_to_json(ct)->str;
    json_to_curve_table(ct, str)->Array[str] (problems; [] == ok; FULL REPLACE of the table);
    get_curve_table_row_names(ct)->Array[Name]; get_curve_table_keys(ct, row)->Array[SimpleCurveKey]
    (SimpleCurveKey.get_editor_property('time'/'value')); evaluate_curve_table_row(ct, row, x, ctx)
    ->(EvaluateCurveTableResult, float); add_simple_curve_to_table(ct, row)->bool;
    add_curve_table_key(ct, row, time, value)->bool; remove_curve_table_row(ct, row)->bool;
    rename_curve_table_row(ct, old, new)->bool; set_curve_table_row_default(ct, row, val)->bool.
  - CurveTableFactory / CompositeCurveTableFactory / CurveLinearColorAtlasFactory create
    non-interactively (no ConfigureProperties dialog -> no modal; each bounded-probed create+delete).
    A factory-fresh CurveTable is seeded with ONE default row named 'Curve' (removed by
    create_curve_table so the new table starts empty).
  - The CurveTable JSON shape is an array of row objects: [{"Name":"Row1","0":10,"1":20}, ...].

REVERSIBILITY (every write pushes an inverse onto the per-session ledger; editor_level.py owns the
ONE unified `undo`, this module registers none):
  - delete_curve_keys / import_curve REUSE the existing op "set_curve_keys" {asset_path,
    prior_channels} (prior full key state captured via get_curve_keys_json -> faithful restore).
  - create_curve_table / create_curve_atlas REUSE the generic op "create_asset" {asset_path,
    package_path, created_dir} (inverse deletes the whole asset).
  - set_curve_table_row / delete_curve_table_row / rename_curve_table_row / import_curve_table share
    ONE NEW op "set_curve_table" {asset_path, prior_json} (prior full-table snapshot via
    curve_table_to_json; inverse = json_to_curve_table(obj, prior_json), a full REPLACE -> faithful).
  - set_curve_atlas is ONE NEW op "set_curve_atlas" {asset_path, prior_width, prior_curves:[paths]}
    (inverse re-applies texture_size + gradient_curves).
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
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

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
    _HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
def _split_path(asset_path):
    ap = (asset_path or "").rstrip("/")
    if "/" not in ap:
        return None, None
    return ap.rsplit("/", 1)[0], ap.rsplit("/", 1)[1]
def _dtfl():
    return unreal.DataTableFunctionLibrary
def _mrl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _rownames(obj):
    return [str(n) for n in (_dtfl().get_curve_table_row_names(obj) or [])]
def _INTERP(s):
    s = (s or "Cubic").lower()
    if s.startswith("lin"): return unreal.RichCurveInterpMode.RCIM_LINEAR
    if s.startswith("con"): return unreal.RichCurveInterpMode.RCIM_CONSTANT
    return unreal.RichCurveInterpMode.RCIM_CUBIC
'''

    # ================================================================== #
    # delete_curve_keys — remove keys by time / clear a channel          #
    # ================================================================== #
    _DELETE_KEYS_BODY = _HELPERS + r'''
curve_path = PARAMS["curve_path"]
times = PARAMS.get("times")
clear_all = bool(PARAMS.get("all"))
channels_req = PARAMS.get("channels")
eps = float(PARAMS.get("epsilon") or 1e-06)
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(curve_path)
mrl = _mrl()
if obj is None or not isinstance(obj, unreal.CurveBase):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a curve asset: %s" % curve_path}))
elif mrl is None or not hasattr(mrl, "set_curve_keys_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler set_curve_keys_json unavailable (plugin DLL predates C++ #4)"}))
elif not clear_all and not times:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "nothing to delete: pass all=True to clear, or times=[..] to remove specific keys"}))
else:
    prior = json.loads(mrl.get_curve_keys_json(obj))
    prior_ch = prior.get("channels") or []
    n = len(prior_ch)
    if channels_req is None:
        targets = list(range(n))
    else:
        targets = [int(i) for i in channels_req if 0 <= int(i) < n]
    del_times = [float(t) for t in (times or [])]
    prior_channels = []
    for i, ch in enumerate(prior_ch):
        prior_channels.append({"index": i, "keys": ch.get("keys") or []})
    payload = []
    removed = 0
    for i in targets:
        old_keys = prior_ch[i].get("keys") or []
        if clear_all:
            removed += len(old_keys)
            payload.append({"index": i, "keys": []})
        else:
            keep = []
            for k in old_keys:
                kt = float(k.get("time"))
                if any(abs(kt - dt) <= eps for dt in del_times):
                    removed += 1
                else:
                    keep.append(k)
            payload.append({"index": i, "keys": keep})
    res = json.loads(mrl.set_curve_keys_json(obj, json.dumps({"channels": payload})))
    try: EAL.save_asset(curve_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_keys", "asset_path": curve_path, "prior_channels": prior_channels})
    after = json.loads(mrl.get_curve_keys_json(obj))
    total_after = sum(int(c.get("key_count") or len(c.get("keys") or [])) for c in (after.get("channels") or []))
    print("@@UMCP@@" + json.dumps({"status": "success", "curve": obj.get_name(), "asset_path": curve_path,
        "channels_targeted": targets, "keys_removed": removed, "cleared_all": clear_all,
        "total_keys_after": total_after, "channels_written": res.get("channels_written"),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_curve_keys(ctx, curve_path: str, times: list = None, all: bool = False,
                          channels: list = None, epsilon: float = 1e-06) -> str:
        """Remove keyframes from a curve asset (CurveFloat / CurveVector / CurveLinearColor), or clear
        a whole channel. REQUIRES the C++ #4 handler (unreal.MCPReflectionLibrary.set_curve_keys_json);
        the curve's FRichCurve is protected to stock Python.

        curve_path: object/package path of the curve asset.
        times:      list of key times to delete (matched within +/- epsilon). Ignored if all=True.
        all:        True -> clear ALL keys from the targeted channels (ignores times).
        channels:   optional list of channel indices to act on (CurveFloat->[0]; CurveVector->0/1/2;
                    CurveLinearColor->0/1/2/3). Omit to act on ALL channels.
        epsilon:    time-match tolerance for `times` (default 1e-6).

        The asset is saved after the edit. Verify with curves_read.get_curve_info (include_keys).

        Ledgered write op 'set_curve_keys' {asset_path, prior_channels} (REUSES the existing op --
        prior FULL key state captured via get_curve_keys_json -> faithful restore of every channel)."""
        params = {"curve_path": curve_path, "times": times, "all": all,
                  "channels": channels, "epsilon": epsilon}
        try:
            return json.dumps(_exec(_DELETE_KEYS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # import_curve — load keys from JSON or CSV                          #
    # ================================================================== #
    _IMPORT_CURVE_BODY = _HELPERS + r'''
curve_path = PARAMS["curve_path"]
fmt = (PARAMS.get("format") or "json").lower()
data = PARAMS.get("data") or ""
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(curve_path)
mrl = _mrl()
if obj is None or not isinstance(obj, unreal.CurveBase):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a curve asset: %s" % curve_path}))
elif mrl is None or not hasattr(mrl, "set_curve_keys_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler set_curve_keys_json unavailable (plugin DLL predates C++ #4)"}))
else:
    channels_payload = None
    err = None
    if fmt == "json":
        try:
            parsed = json.loads(data)
        except Exception as e:
            parsed = None; err = "invalid JSON: %s" % str(e)[:120]
        if parsed is not None:
            if isinstance(parsed, dict) and isinstance(parsed.get("channels"), list):
                channels_payload = parsed["channels"]
            elif isinstance(parsed, dict) and isinstance(parsed.get("keys"), list):
                channels_payload = [{"index": 0, "keys": parsed["keys"]}]
            elif isinstance(parsed, list):
                channels_payload = [{"index": 0, "keys": parsed}]
            else:
                err = "JSON must be {channels:[..]} or {keys:[..]} or a bare keys list"
    elif fmt == "csv":
        rows = []
        for raw in data.replace(chr(13), "").split(chr(10)):
            line = raw.strip()
            if line:
                rows.append([c.strip() for c in line.split(",")])
        if rows:
            def _isnum(s):
                try: float(s); return True
                except Exception: return False
            start = 0
            if rows[0] and not _isnum(rows[0][0]):
                start = 1  # skip a header row (non-numeric first cell)
            ncols = max((len(r) for r in rows[start:]), default=0)
            nchan = max(0, ncols - 1)
            chans = [[] for _ in range(nchan)]
            for r in rows[start:]:
                if not r or not _isnum(r[0]):
                    continue
                t = float(r[0])
                for ci in range(nchan):
                    if ci + 1 < len(r) and _isnum(r[ci + 1]):
                        chans[ci].append({"time": t, "value": float(r[ci + 1])})
            channels_payload = [{"index": ci, "keys": chans[ci]} for ci in range(nchan)]
        else:
            err = "empty CSV"
    else:
        err = "unknown format '%s' (use 'json' or 'csv')" % fmt

    if err is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    elif not channels_payload:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no channels/keys parsed from data"}))
    else:
        prior = json.loads(mrl.get_curve_keys_json(obj))
        prior_channels = [{"index": i, "keys": ch.get("keys") or []} for i, ch in enumerate(prior.get("channels") or [])]
        res = json.loads(mrl.set_curve_keys_json(obj, json.dumps({"channels": channels_payload})))
        try: EAL.save_asset(curve_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "set_curve_keys", "asset_path": curve_path, "prior_channels": prior_channels})
        after = json.loads(mrl.get_curve_keys_json(obj))
        total_after = sum(int(c.get("key_count") or len(c.get("keys") or [])) for c in (after.get("channels") or []))
        print("@@UMCP@@" + json.dumps({"status": "success", "curve": obj.get_name(), "asset_path": curve_path,
            "format": fmt, "channels_written": res.get("channels_written"), "keys_written": res.get("keys_written"),
            "total_keys_after": total_after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def import_curve(ctx, curve_path: str, data: str, format: str = "json") -> str:
        """Import keyframes into a curve asset from JSON or CSV. REPLACES the keys of every channel
        present in the data (channels not present are left untouched). REQUIRES the C++ #4 handler
        (unreal.MCPReflectionLibrary.set_curve_keys_json).

        curve_path: object/package path of a CurveFloat / CurveVector / CurveLinearColor asset.
        data:       the payload string.
        format:     'json' (default) or 'csv'.
          JSON accepts:  {"channels":[{"index":0,"keys":[{"time":0,"value":1,"interp_mode":"Cubic",
                         "tangent_mode":"Auto","arrive_tangent":0,"leave_tangent":0}, ...]}, ...]}
                         -- or a shorthand {"keys":[...]} / a bare [...] list (applied to channel 0).
          CSV accepts:   one key per line 'time,val0[,val1,val2,...]' (column 0 = time, columns 1.. map
                         to channel 0,1,2,...). A non-numeric first row is treated as a header and skipped.

        The asset is saved after import. Ledgered write op 'set_curve_keys' {asset_path, prior_channels}
        (REUSES the existing op -> faithful full restore of the affected channels)."""
        params = {"curve_path": curve_path, "data": data, "format": format}
        try:
            return json.dumps(_exec(_IMPORT_CURVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_curve_table — CurveTable or CompositeCurveTable (factory)   #
    # ================================================================== #
    _CREATE_TABLE_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
composite = bool(PARAMS.get("composite"))
parent_tables = PARAMS.get("parent_tables") or []
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
package_path, name = _split_path(asset_path)
if package_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path must be a full path like /Game/Dir/Name"}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    if composite:
        cls, fac = unreal.CompositeCurveTable, unreal.CompositeCurveTableFactory()
    else:
        cls, fac = unreal.CurveTable, unreal.CurveTableFactory()
    obj = at.create_asset(name, package_path, cls, fac)
    if obj is None or not isinstance(obj, cls):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        parents_set = []
        if composite and parent_tables:
            loaded = []
            for p in parent_tables:
                po = EAL.load_asset(p)
                if po is not None and isinstance(po, unreal.CurveTable):
                    loaded.append(po); parents_set.append(p)
            if loaded:
                try: obj.set_editor_property("parent_tables", loaded)
                except Exception: pass
        elif not composite:
            # a factory-fresh CurveTable is seeded with a default row 'Curve' -> remove so it starts empty
            for rn in _rownames(obj):
                try: _dtfl().remove_curve_table_row(obj, unreal.Name(rn))
                except Exception: pass
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": asset_path, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "composite": composite,
            "parent_tables": parents_set, "row_names": _rownames(obj),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_curve_table(ctx, asset_path: str, composite: bool = False,
                           parent_tables: list = None) -> str:
        """Create a new CurveTable (or CompositeCurveTable) asset non-interactively (NO modal).

        asset_path:    full content path for the new asset, e.g. '/Game/Data/DifficultyCurves'.
                       Intermediate folders are created as needed. NEVER '/Temp/'.
        composite:     False (default) -> a normal CurveTable (rows authored via set_curve_table_row /
                       import_curve_table). True -> a CompositeCurveTable that layers parent_tables.
        parent_tables: (composite only) list of CurveTable asset paths to reference, in order.

        A normal CurveTable is created EMPTY (the factory's default 'Curve' row is removed). Uses
        AssetTools.create_asset with CurveTableFactory / CompositeCurveTableFactory (no dialog).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already in
        editor_level.undo): close editors + delete asset [+ rmdir if we made the dir]. Saved on create."""
        params = {"asset_path": asset_path, "composite": composite, "parent_tables": parent_tables}
        try:
            return json.dumps(_exec(_CREATE_TABLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_curve_table — read rows / keys / evaluations                  #
    # ================================================================== #
    _GET_TABLE_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
flt = PARAMS.get("filter")
include_keys = PARAMS.get("include_keys")
include_keys = True if include_keys is None else bool(include_keys)
eval_times = PARAMS.get("eval_times") or []
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveTable asset: %s" % asset_path}))
else:
    D = _dtfl()
    all_names = _rownames(obj)
    names = [n for n in all_names if (not flt or str(flt).lower() in n.lower())]
    rows = []
    for nm in names:
        row = {"name": nm}
        if include_keys:
            keys = []
            try:
                for k in (D.get_curve_table_keys(obj, unreal.Name(nm)) or []):
                    keys.append({"time": round(float(k.get_editor_property("time")), 6),
                                 "value": round(float(k.get_editor_property("value")), 6)})
            except Exception:
                keys = []
            row["keys"] = keys
            row["key_count"] = len(keys)
        if eval_times:
            evs = []
            for t in eval_times:
                try:
                    r = D.evaluate_curve_table_row(obj, unreal.Name(nm), float(t), "get_curve_table")
                    evs.append({"time": round(float(t), 6), "value": round(float(r[1]), 6),
                                "result": str(r[0]).split(".")[-1]})
                except Exception as e:
                    evs.append({"time": round(float(t), 6), "error": str(e)[:80]})
            row["evaluations"] = evs
        rows.append(row)
    out = {"status": "success", "asset_path": obj.get_path_name(),
           "class": obj.get_class().get_name(),
           "is_composite": isinstance(obj, unreal.CompositeCurveTable),
           "row_count": len(all_names), "returned": len(rows), "rows": rows}
    if PARAMS.get("as_json"):
        try: out["json"] = D.curve_table_to_json(obj)
        except Exception: out["json"] = None
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def get_curve_table(ctx, asset_path: str, filter: str = None, include_keys: bool = True,
                        eval_times: list = None, as_json: bool = False) -> str:
        """Read a CurveTable / CompositeCurveTable. Loads it; no editor opened. Read-only.

        asset_path:   the CurveTable asset path.
        filter:       optional case-insensitive substring to restrict which row names are returned.
        include_keys: include each row's simple-curve keys [{time,value}] (default True). RichCurve
                      rows return an empty key list (only SimpleCurve rows expose keys via Python).
        eval_times:   optional list of times; each row is also evaluated at those times
                      (via DataTableFunctionLibrary.evaluate_curve_table_row).
        as_json:      also include the table's full native JSON export.

        Returns row_count, and per-row {name, keys, key_count, evaluations}."""
        params = {"asset_path": asset_path, "filter": filter, "include_keys": include_keys,
                  "eval_times": eval_times, "as_json": as_json}
        try:
            return json.dumps(_exec(_GET_TABLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_curve_table_row — add / replace a simple-curve row            #
    # ================================================================== #
    _SET_ROW_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
row_name = PARAMS["row_name"]
keys_in = PARAMS.get("keys") or []
default_value = PARAMS.get("default_value")
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveTable asset: %s" % asset_path}))
elif isinstance(obj, unreal.CompositeCurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "cannot author rows on a CompositeCurveTable (edit its parent_tables instead)"}))
else:
    D = _dtfl()
    prior_json = D.curve_table_to_json(obj)
    rn = unreal.Name(row_name)
    existed = row_name in _rownames(obj)
    if existed:
        D.remove_curve_table_row(obj, rn)
    D.add_simple_curve_to_table(obj, rn)
    written = 0
    for k in keys_in:
        if isinstance(k, dict):
            t = float(k.get("time")); v = float(k.get("value"))
        else:
            t = float(k[0]); v = float(k[1])
        if D.add_curve_table_key(obj, rn, t, v):
            written += 1
    if default_value is not None:
        try: D.set_curve_table_row_default(obj, rn, float(default_value))
        except Exception: pass
    try: EAL.save_asset(asset_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_table", "asset_path": asset_path, "prior_json": prior_json})
    keys_after = []
    try:
        for k in (D.get_curve_table_keys(obj, rn) or []):
            keys_after.append([round(float(k.get_editor_property("time")), 6),
                               round(float(k.get_editor_property("value")), 6)])
    except Exception:
        keys_after = []
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "row_name": row_name, "replaced_existing": existed, "keys_written": written,
        "keys_after": keys_after, "row_count": len(_rownames(obj)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_curve_table_row(ctx, asset_path: str, row_name: str, keys: list = None,
                            default_value: float = None) -> str:
        """Add or replace a simple-curve row in a CurveTable.

        asset_path:    the CurveTable asset path (NOT a CompositeCurveTable).
        row_name:      the row to create/overwrite (if it exists it is fully replaced).
        keys:          list of keyframes for the row: [{"time":0.0,"value":10.0}, ...] or [[t,v], ...].
        default_value: optional default/fallback value for the row.

        Rows are SimpleCurves (time/value keys, linear interpolation between them); per-key tangents
        are not part of the CurveTable simple-curve model. The asset is saved after the edit.

        Ledgered write op 'set_curve_table' {asset_path, prior_json} (NEW op, shared by all CurveTable
        row mutations). Inverse: json_to_curve_table(obj, prior_json) -> full-table faithful restore."""
        params = {"asset_path": asset_path, "row_name": row_name, "keys": keys,
                  "default_value": default_value}
        try:
            return json.dumps(_exec(_SET_ROW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # delete_curve_table_row                                            #
    # ================================================================== #
    _DEL_ROW_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
row_name = PARAMS["row_name"]
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveTable asset: %s" % asset_path}))
elif row_name not in _rownames(obj):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "row not found: %s (rows: %s)" % (row_name, ", ".join(_rownames(obj)))}))
else:
    D = _dtfl()
    prior_json = D.curve_table_to_json(obj)
    ok = D.remove_curve_table_row(obj, unreal.Name(row_name))
    try: EAL.save_asset(asset_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_table", "asset_path": asset_path, "prior_json": prior_json})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "row_name": row_name, "removed": bool(ok), "row_count": len(_rownames(obj)),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_curve_table_row(ctx, asset_path: str, row_name: str) -> str:
        """Remove a row from a CurveTable.

        asset_path: the CurveTable asset path.
        row_name:   the row to delete (errors if absent).

        The asset is saved after the edit. Ledgered write op 'set_curve_table' {asset_path, prior_json}
        (shared CurveTable op). Inverse: json_to_curve_table(obj, prior_json) -> full restore."""
        params = {"asset_path": asset_path, "row_name": row_name}
        try:
            return json.dumps(_exec(_DEL_ROW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # rename_curve_table_row                                            #
    # ================================================================== #
    _RENAME_ROW_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
row_name = PARAMS["row_name"]
new_name = PARAMS["new_name"]
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveTable asset: %s" % asset_path}))
elif row_name not in _rownames(obj):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "row not found: %s (rows: %s)" % (row_name, ", ".join(_rownames(obj)))}))
elif new_name in _rownames(obj):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "target row name already exists: %s" % new_name}))
else:
    D = _dtfl()
    prior_json = D.curve_table_to_json(obj)
    ok = D.rename_curve_table_row(obj, unreal.Name(row_name), unreal.Name(new_name))
    try: EAL.save_asset(asset_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_table", "asset_path": asset_path, "prior_json": prior_json})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "row_name": row_name, "new_name": new_name, "renamed": bool(ok),
        "row_names": _rownames(obj), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_curve_table_row(ctx, asset_path: str, row_name: str, new_name: str) -> str:
        """Rename a row in a CurveTable.

        asset_path: the CurveTable asset path.
        row_name:   the existing row name (errors if absent).
        new_name:   the new row name (errors if it already exists).

        The asset is saved after the edit. Ledgered write op 'set_curve_table' {asset_path, prior_json}
        (shared CurveTable op). Inverse: json_to_curve_table(obj, prior_json) -> full restore."""
        params = {"asset_path": asset_path, "row_name": row_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_ROW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # import_curve_table — bulk REPLACE from JSON or CSV                #
    # ================================================================== #
    _IMPORT_TABLE_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
fmt = (PARAMS.get("format") or "json").lower()
data = PARAMS.get("data") or ""
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveTable asset: %s" % asset_path}))
elif isinstance(obj, unreal.CompositeCurveTable):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot import rows into a CompositeCurveTable"}))
else:
    D = _dtfl()
    prior_json = D.curve_table_to_json(obj)
    err = None
    problems = None
    if fmt == "json":
        problems = list(D.json_to_curve_table(obj, data) or [])
    elif fmt == "csv":
        # Parse a curve-table CSV: header row = 'Name,<t0>,<t1>,...'; each row = 'RowName,v0,v1,...'.
        rows = []
        for raw in data.replace(chr(13), "").split(chr(10)):
            line = raw.strip()
            if line:
                rows.append([c.strip() for c in line.split(",")])
        if len(rows) < 2:
            err = "CSV needs a header row of times plus at least one data row"
        else:
            def _isnum(s):
                try: float(s); return True
                except Exception: return False
            times = [float(x) for x in rows[0][1:] if _isnum(x)]
            # clear existing rows first (full REPLACE)
            for rn in _rownames(obj):
                D.remove_curve_table_row(obj, unreal.Name(rn))
            problems = []
            for r in rows[1:]:
                if not r or not r[0]:
                    continue
                nm = unreal.Name(r[0])
                D.add_simple_curve_to_table(obj, nm)
                for ci, t in enumerate(times):
                    if ci + 1 < len(r) and _isnum(r[ci + 1]):
                        D.add_curve_table_key(obj, nm, float(t), float(r[ci + 1]))
    else:
        err = "unknown format '%s' (use 'json' or 'csv')" % fmt

    if err is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "set_curve_table", "asset_path": asset_path, "prior_json": prior_json})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "format": fmt,
            "problems": problems, "row_names": _rownames(obj), "row_count": len(_rownames(obj)),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def import_curve_table(ctx, asset_path: str, data: str, format: str = "json") -> str:
        """Bulk-REPLACE the contents of a CurveTable from JSON or CSV. Every existing row is discarded
        and replaced by the imported data.

        asset_path: the CurveTable asset path (NOT a CompositeCurveTable).
        data:       the payload string.
        format:     'json' (default) or 'csv'.
          JSON: the native CurveTable format -- an array of row objects
                [{"Name":"Row1","0":10,"1":20}, {"Name":"Row2","0":5,"1":7}]  (keys are '<time>':value).
                Returns any parser 'problems' ([] == clean).
          CSV:  first row is a header 'Name,<t0>,<t1>,...' of key TIMES; each subsequent row is
                'RowName,v0,v1,...' of values at those times.

        The asset is saved after import. Ledgered write op 'set_curve_table' {asset_path, prior_json}
        (shared CurveTable op). Inverse: json_to_curve_table(obj, prior_json) -> full restore."""
        params = {"asset_path": asset_path, "data": data, "format": format}
        try:
            return json.dumps(_exec(_IMPORT_TABLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_curve_atlas — texture atlas from gradient curves          #
    # ================================================================== #
    _CREATE_ATLAS_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
width = PARAMS.get("width")
gradient_curves = PARAMS.get("gradient_curves") or []
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
package_path, name = _split_path(asset_path)
if package_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path must be a full path like /Game/Dir/Name"}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    obj = at.create_asset(name, package_path, unreal.CurveLinearColorAtlas, unreal.CurveLinearColorAtlasFactory())
    if obj is None or not isinstance(obj, unreal.CurveLinearColorAtlas):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        if width:
            try: obj.set_editor_property("texture_size", int(width))
            except Exception: pass
        loaded = []
        added = []
        skipped = []
        for p in gradient_curves:
            po = EAL.load_asset(p)
            if po is not None and isinstance(po, unreal.CurveLinearColor):
                loaded.append(po); added.append(p)
            else:
                skipped.append(p)
        if loaded:
            try: obj.set_editor_property("gradient_curves", loaded)
            except Exception: pass
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        cur = []
        try: cur = [c.get_path_name() for c in (obj.get_editor_property("gradient_curves") or [])]
        except Exception: cur = []
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": asset_path, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(),
            "texture_size": obj.get_editor_property("texture_size"),
            "gradient_curves": cur, "added": added, "skipped_non_linear_color": skipped,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_curve_atlas(ctx, asset_path: str, width: int = 256,
                           gradient_curves: list = None) -> str:
        """Create a CurveLinearColorAtlas (a texture baked from a set of gradient CurveLinearColor
        curves, one gradient per texture row) non-interactively (NO modal).

        asset_path:      full content path for the new atlas, e.g. '/Game/FX/GradientAtlas'.
        width:           texture_size (square; the atlas texture is width x width; default 256).
        gradient_curves: list of CurveLinearColor asset paths to bake into the atlas, in row order.
                         Non-CurveLinearColor paths are skipped and reported.

        Uses AssetTools.create_asset with CurveLinearColorAtlasFactory (no dialog). Setting
        gradient_curves fires PostEditChangeProperty which rebuilds the texture.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already in
        editor_level.undo): close editors + delete asset [+ rmdir if we made the dir]. Saved on create."""
        params = {"asset_path": asset_path, "width": width, "gradient_curves": gradient_curves}
        try:
            return json.dumps(_exec(_CREATE_ATLAS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_curve_atlas — add / remove / replace curves; resize          #
    # ================================================================== #
    _SET_ATLAS_BODY = _HELPERS + r'''
asset_path = PARAMS["asset_path"]
width = PARAMS.get("width")
add_curves = PARAMS.get("add_curves") or []
remove_curves = PARAMS.get("remove_curves") or []
set_curves = PARAMS.get("set_curves")
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(asset_path)
if obj is None or not isinstance(obj, unreal.CurveLinearColorAtlas):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a CurveLinearColorAtlas asset: %s" % asset_path}))
else:
    prior_width = obj.get_editor_property("texture_size")
    prior_curves = []
    for c in (obj.get_editor_property("gradient_curves") or []):
        try: prior_curves.append(c.get_path_name())
        except Exception: pass
    def _load_clc(p):
        po = EAL.load_asset(p)
        return po if (po is not None and isinstance(po, unreal.CurveLinearColor)) else None
    skipped = []
    if set_curves is not None:
        new_list = []
        for p in set_curves:
            po = _load_clc(p)
            if po is not None: new_list.append(po)
            else: skipped.append(p)
    else:
        # start from current, apply remove then add (compare by object path)
        cur_objs = list(obj.get_editor_property("gradient_curves") or [])
        rm_norm = set()
        for p in remove_curves:
            po = _load_clc(p)
            if po is not None: rm_norm.add(po.get_path_name())
            else: rm_norm.add(str(p))
        new_list = [c for c in cur_objs if c is not None and c.get_path_name() not in rm_norm]
        have = set(c.get_path_name() for c in new_list if c is not None)
        for p in add_curves:
            po = _load_clc(p)
            if po is None:
                skipped.append(p)
            elif po.get_path_name() not in have:
                new_list.append(po); have.add(po.get_path_name())
    obj.set_editor_property("gradient_curves", new_list)
    if width:
        obj.set_editor_property("texture_size", int(width))
    try: EAL.save_asset(asset_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_atlas", "asset_path": asset_path,
                      "prior_width": prior_width, "prior_curves": prior_curves})
    cur = []
    try: cur = [c.get_path_name() for c in (obj.get_editor_property("gradient_curves") or [])]
    except Exception: cur = []
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "texture_size": obj.get_editor_property("texture_size"), "gradient_curves": cur,
        "skipped_non_linear_color": skipped, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_curve_atlas(ctx, asset_path: str, width: int = None, add_curves: list = None,
                        remove_curves: list = None, set_curves: list = None) -> str:
        """Modify an existing CurveLinearColorAtlas: resize and/or change its gradient curves.

        asset_path:    the CurveLinearColorAtlas asset path.
        width:         optional new texture_size (square). Omit to leave unchanged.
        set_curves:    if given, REPLACES the entire gradient-curve list with these CurveLinearColor
                       paths (in order). When set_curves is provided, add_curves/remove_curves are ignored.
        add_curves:    CurveLinearColor paths to append (skipped if already present).
        remove_curves: CurveLinearColor paths to remove from the list.

        Non-CurveLinearColor paths are skipped and reported. Setting gradient_curves rebuilds the
        texture (PostEditChangeProperty). The asset is saved after the edit.

        Ledgered write op 'set_curve_atlas' {asset_path, prior_width, prior_curves} (NEW op). Inverse:
        set texture_size=prior_width and gradient_curves=[load prior_curves] -> faithful restore."""
        params = {"asset_path": asset_path, "width": width, "add_curves": add_curves,
                  "remove_curves": remove_curves, "set_curves": set_curves}
        try:
            return json.dumps(_exec(_SET_ATLAS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # NEW undo ops to fold into editor_level.undo: 'set_curve_table' {asset_path, prior_json} and
    # 'set_curve_atlas' {asset_path, prior_width, prior_curves}. delete_curve_keys/import_curve REUSE
    # 'set_curve_keys'; create_curve_table/create_curve_atlas REUSE 'create_asset'. (See module footer
    # + .mcp_coord/coordinator-inbox for exact inverse calls.)
