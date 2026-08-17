"""UserTools :: Curves (WRITE)  (spec: docs/spec/curves.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE-wave module, the
mutating counterpart to curves_read.py. Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py / reference create-wave module ai_write.py.

What this build exposes (all NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * CurveFloat        via unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, pkg,
                          unreal.CurveFloat, unreal.CurveFloatFactory())
  * CurveVector       via ... unreal.CurveVector, unreal.CurveVectorFactory()
  * CurveLinearColor  via ... unreal.CurveLinearColor, unreal.CurveLinearColorFactory()
    None of the three factories has a ConfigureProperties dialog and the scripting create_asset
    path never calls one -> no modal EVER pops (each bounded-probed with an isolated create+delete).
    A fresh curve has ZERO authored keys (empty FRichCurve(s)); its evaluator returns 0 everywhere.

DEFERRED (NOT reachable from stock Python in this build -- refused rather than shipping a fake/
unrevertable write): per-key AUTHORING on a curve asset (add_curve_key / set_curve_key /
remove_curve_key). Investigated definitively live (2026-08-14):
  - UCurveFloat exposes NO key mutator (dir() -> only get_float_value / modify / editor-property
    accessors; no add_key/set_keys/get_rich_curve).
  - The asset's FRichCurve lives in a PROTECTED UPROPERTY (UCurveFloat.FloatCurve /
    UCurveVector.FloatCurves / UCurveLinearColor.FloatCurves). get_editor_property("FloatCurve") is
    refused "is protected and cannot be read" AND set_editor_property("FloatCurve", <RichCurve>) is
    refused "is protected and cannot be set" (snake spelling "float_curve" -> "Failed to find
    property"). So the built RichCurve can NOT be attached to the asset.
  - unreal.RichCurve / unreal.RichCurveKey structs DO exist and are fully authorable IN MEMORY
    (RichCurve.get/set_editor_property("keys") works; RichCurveKey has time/value/interp_mode/
    tangent_mode/tangent_weight_mode; unreal.RichCurveInterpMode = RCIM_CONSTANT/LINEAR/CUBIC) --
    but there is no route to write that struct back onto the asset's protected FRichCurve.
  - MCPReflectionLibrary exposes only get_curve_keys_json (READ, wired into curves_read.get_curve_info);
    there is NO curve WRITE helper.
  => Key authoring needs a C++ handler (e.g. MCPReflectionLibrary.SetCurveKeysJson(UCurveBase*, json)
     mutating the FRichCurve via FRichCurve::SetKeys / AddKey, then MarkPackageDirty) -- the same
     C++-round-trip pattern that unblocked struct fields / mesh sockets / curve-key READS (C++ #3).
     Ledger schema when that handler lands (documented for the coordinator / Wave-3 recompile):
       op "set_curve_keys" {asset_path, channel_index, prior_keys:[{time,value,interp_mode,
       tangent_mode,arrive_tangent,leave_tangent}]} -> inverse: rewrite that channel's FRichCurve
       from prior_keys (prior read via get_curve_keys_json before the edit -> FAITHFUL full restore).

Implemented (all validated live, session=agentA, editor left CLEAN, ledger depth 0):
  - create_curve_float        (WRITE; ledgered op "create_asset")
  - create_curve_vector       (WRITE; ledgered op "create_asset")
  - create_curve_linear_color (WRITE; ledgered op "create_asset")

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the ONE unified `undo`).
All three creates push the shared generic {op:create_asset, asset_path, package_path, created_dir}
whose inverse (close editors + GC + delete_asset [+ rmdir if we made the dir]) already exists in
editor_level.undo (folded during CREATE-WAVE1) -- nothing new to fold for the create commands.
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
    #   _ledger()             -> per-session undo stack (copied verbatim from editor_level.py).
    #   _close_editors(obj)   -> silently close any open asset editor for obj (copied from ai_write.py).
    #   _CURVE_MAP            -> kind name -> (class, factory class, channel count, channel names).
    _CURVE_HELPERS = r'''
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
def _curve_map(kind):
    if kind == "CurveFloat":
        return unreal.CurveFloat, unreal.CurveFloatFactory, 1, ["Value"]
    if kind == "CurveVector":
        return unreal.CurveVector, unreal.CurveVectorFactory, 3, ["X", "Y", "Z"]
    if kind == "CurveLinearColor":
        return unreal.CurveLinearColor, unreal.CurveLinearColorFactory, 4, ["R", "G", "B", "A"]
    return None, None, 0, []
'''

    # ------------------------------------------------------------------ #
    # Shared create body for all 3 curve kinds (parameterized by PARAMS["kind"]) #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _CURVE_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
kind = PARAMS["kind"]
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
cls, faccls, nchan, chan_names = _curve_map(kind)
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown curve kind: %s" % kind}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # Do NOT wrap create_asset in a ScopedEditorTransaction (PROTOCOL #5b) -- it would trap the new
    # asset in the editor undo buffer and block a later delete. Ledgered via our own create_asset op.
    obj = at.create_asset(name, package_path, cls, faccls())
    if obj is None or not isinstance(obj, cls):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        # Save on create (PROTOCOL #5c): persists it + makes the undo-delete reliable.
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        # Readback: overall time/value range + real authored key count (0 for a fresh curve) via the
        # native C++ reader if compiled in (same handler curves_read.get_curve_info uses).
        tr = None
        try: tr = obj.get_time_range()
        except Exception: tr = None
        key_count = None
        keys_source = "sampling_only"
        if hasattr(unreal.MCPReflectionLibrary, "get_curve_keys_json"):
            try:
                kd = json.loads(unreal.MCPReflectionLibrary.get_curve_keys_json(obj))
                if isinstance(kd, dict) and isinstance(kd.get("channels"), list):
                    key_count = sum(int(c.get("key_count") or len(c.get("keys") or [])) for c in kd["channels"])
                    keys_source = "MCPReflectionLibrary"
            except Exception:
                pass
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": asset_path, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "kind": kind,
            "num_channels": nchan, "channel_names": chan_names,
            "time_range": ([round(tr[0], 6), round(tr[1], 6)] if tr is not None else None),
            "key_count": key_count, "keys_source": keys_source,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_curve_float(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new CurveFloat asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new curve (e.g. 'MyFalloffCurve').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be
                      under a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate
                      folders are created as needed. NEVER use '/Temp/' (validation-error modal).

        Uses AssetTools.create_asset(..., unreal.CurveFloat, unreal.CurveFloatFactory()). The factory
        has no ConfigureProperties dialog and the scripting path never calls one -> no modal. The
        curve is created EMPTY (zero authored keys; its evaluator returns 0 everywhere). Inspect with
        curves_read.get_curve_info / evaluate_curve. Per-key authoring (add/set/remove key) is DEFERRED
        (the FRichCurve UPROPERTY is C++-protected for both read and write -- see module docstring).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already in
        editor_level.undo): close editors + delete asset [+ rmdir if we created the dir]. Saved on create."""
        params = {"name": name, "package_path": package_path, "kind": "CurveFloat"}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def create_curve_vector(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new CurveVector asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new curve.
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid mounted
                      root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.CurveVector, unreal.CurveVectorFactory()) (no dialog
        -> no modal). A CurveVector has 3 channels (X, Y, Z); it is created EMPTY. Inspect/sample with
        curves_read (get_vector_value evaluator). Per-key authoring is DEFERRED (protected FRichCurves).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close editors
        + delete asset [+ rmdir if we created the dir]. Saved on create."""
        params = {"name": name, "package_path": package_path, "kind": "CurveVector"}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def create_curve_linear_color(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new CurveLinearColor asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new curve.
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid mounted
                      root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.CurveLinearColor, unreal.CurveLinearColorFactory())
        (no dialog -> no modal). A CurveLinearColor has 4 channels (R, G, B, A) and the factory seeds
        a DEFAULT gradient (2 keys per channel = 8 keys, a 0->1 ramp), unlike CurveFloat/CurveVector
        which are created empty. Inspect/sample with curves_read (get_linear_color_value evaluator).
        Per-key authoring is
        DEFERRED (protected FRichCurves -- see module docstring).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close editors
        + delete asset [+ rmdir if we created the dir]. Saved on create."""
        params = {"name": name, "package_path": package_path, "kind": "CurveLinearColor"}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_curve_keys — author keyframes on a curve asset (via C++ #4 handler) #
    # ------------------------------------------------------------------ #
    # ENABLED 2026-08-15 by C++ #4 (MCPReflectionLibrary.SetCurveKeysJson): the curve asset's FRichCurve
    # is C++-protected to stock Python (get/set_editor_property both refuse), so key authoring goes through
    # the compiled handler. hasattr-guarded so the module still loads if the DLL predates C++ #4.
    _SET_CURVE_KEYS_BODY = _CURVE_HELPERS + r'''
curve_path = PARAMS["curve_path"]
channels_in = PARAMS.get("channels") or []
EAL = unreal.EditorAssetLibrary
obj = EAL.load_asset(curve_path)
if obj is None or not isinstance(obj, unreal.CurveBase):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a curve asset: %s" % curve_path}))
elif not hasattr(unreal.MCPReflectionLibrary, "set_curve_keys_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler set_curve_keys_json unavailable (plugin DLL predates C++ #4; recompile needed)"}))
else:
    # Capture prior keys (indexed by channel position) for a FAITHFUL undo.
    prior_channels = []
    try:
        pr = json.loads(unreal.MCPReflectionLibrary.get_curve_keys_json(obj))
        for i, ch in enumerate(pr.get("channels") or []):
            prior_channels.append({"index": i, "keys": ch.get("keys") or []})
    except Exception:
        prior_channels = []
    res = json.loads(unreal.MCPReflectionLibrary.set_curve_keys_json(obj, json.dumps({"channels": channels_in})))
    try: EAL.save_asset(curve_path, only_if_is_dirty=False)
    except Exception: pass
    _ledger().append({"op": "set_curve_keys", "asset_path": curve_path, "prior_channels": prior_channels})
    after = {}
    try: after = json.loads(unreal.MCPReflectionLibrary.get_curve_keys_json(obj))
    except Exception: after = {}
    total_after = sum(int(c.get("key_count") or len(c.get("keys") or [])) for c in (after.get("channels") or []))
    print("@@UMCP@@" + json.dumps({"status": "success", "curve": obj.get_name(), "asset_path": curve_path,
        "channels_written": res.get("channels_written"), "keys_written": res.get("keys_written"),
        "total_keys_after": total_after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_curve_keys(ctx, curve_path: str, channels: list) -> str:
        """Author (overwrite) keyframes on a curve asset. REQUIRES the C++ #4 handler
        (unreal.MCPReflectionLibrary.set_curve_keys_json) -- the curve's FRichCurve is protected to
        stock Python. Returns a clear error if the plugin DLL predates C++ #4.

        curve_path: object/package path of a CurveFloat / CurveVector / CurveLinearColor asset.
        channels:   list of channel edits -- only the channels you list are rewritten (each is fully
                    reset then rebuilt from its keys). Channel indices: CurveFloat -> 0 (Value);
                    CurveVector -> 0/1/2 (X/Y/Z); CurveLinearColor -> 0/1/2/3 (R/G/B/A). Shape:
                      [ {"index": 0, "keys": [
                           {"time": 0.0, "value": 1.0,
                            "interp_mode": "Cubic"|"Linear"|"Constant",   # optional, default Cubic
                            "tangent_mode": "Auto"|"User"|"Break",        # optional, default Auto
                            "arrive_tangent": 0.0, "leave_tangent": 0.0}, # optional
                           ... ] }, ... ]

        The asset is saved after the edit. Verify with curves_read.get_curve_info (include_keys).

        Ledgered write op 'set_curve_keys' {asset_path, prior_channels}. Inverse (in editor_level.undo):
        re-apply set_curve_keys_json with prior_channels (the FULL prior key state, captured via
        get_curve_keys_json before the edit) -> FAITHFUL restore of every listed channel."""
        params = {"curve_path": curve_path, "channels": channels}
        try:
            return json.dumps(_exec(_SET_CURVE_KEYS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The 3 create commands
    # reuse the generic "create_asset" inverse; set_curve_keys adds the 'set_curve_keys' op (folded there).
