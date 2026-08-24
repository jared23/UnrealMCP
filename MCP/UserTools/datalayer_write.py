"""UserTools :: World Partition Data Layers -- create/remove/state  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). The WRITE-side companion to
datalayers.py (which ships list/get/add/remove-actors/visibility). This module ships the three
lifecycle/state tools that datalayers.py DEFERRED: creating a NEW DataLayerAsset + instance,
removing an instance, and setting the persistent editor/runtime state flags. It mirrors
editor_level.py's conventions VERBATIM (base64 PARAMS, Output-Log capture, @@UMCP@@ marker,
session-aware _ledger(), ScopedEditorTransaction on writes).

A World Partition data layer is a UDataLayerInstance (lives under the level's WorldDataLayers
actor) backed by a UDataLayerAsset (a content asset). The subsystem is
  unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem).
Create is TWO steps (live-verified non-modal):
  1. asset  = AssetToolsHelpers.get_asset_tools().create_asset(name, path, unreal.DataLayerAsset,
              unreal.DataLayerFactory())          # plain create_asset -> no ConfigureProperties modal
  2. params = unreal.DataLayerCreationParameters(); params.set_editor_property("data_layer_asset", asset)
     inst   = dls.create_data_layer_instance(params) -> UDataLayerInstance
State setters (live-verified signatures):
  dls.set_data_layer_is_loaded_in_editor(inst, is_loaded, is_from_user_change) -> bool
  dls.set_data_layer_initial_runtime_state(inst, unreal.DataLayerRuntimeState.<ACTIVATED|LOADED|UNLOADED>)
  dls.set_data_layer_is_initially_visible(inst, is_visible)
Removal:
  dls.delete_data_layer(inst)  -- removes the INSTANCE; the backing DataLayerAsset persists.

Implemented:
  - create_data_layer   (write; ledgered create_data_layer -> delete instance + delete asset)
  - remove_data_layer   (write; ledgered remove_data_layer -> recreate instance from asset + restore state)
  - set_data_layer_state (write; ledgered set_data_layer_state -> restore captured prior flags)

This module defines NO `undo` tool; the coordinator folds the inverse branches into
editor_level.py's unified `undo`.

NEW ledger op schemas (resolve subsystem via DataLayerEditorSubsystem; instance by scanning
get_all_data_layers() for the one whose asset path matches; asset via EditorAssetLibrary):
  create_data_layer:
      {"op":"create_data_layer","asset_path":<str>,"package_path":<str>,"created_dir":<str|null>}
     -> invert: di = <instance whose asset path == asset_path>; if di: dls.delete_data_layer(di);
                unreal.EditorAssetLibrary.delete_asset(asset_path);
                if created_dir: unreal.EditorAssetLibrary.delete_directory(created_dir)
  remove_data_layer:
      {"op":"remove_data_layer","asset_path":<str>,"initial_runtime_state":<str>,
       "is_initially_visible":<bool>,"is_loaded_in_editor":<bool>}
     -> invert: asset = load_asset(asset_path); p = unreal.DataLayerCreationParameters();
                p.set_editor_property("data_layer_asset", asset);
                di = dls.create_data_layer_instance(p);
                dls.set_data_layer_initial_runtime_state(di, DataLayerRuntimeState[<initial_runtime_state>]);
                dls.set_data_layer_is_initially_visible(di, is_initially_visible);
                dls.set_data_layer_is_loaded_in_editor(di, is_loaded_in_editor, False)
  set_data_layer_state:
      {"op":"set_data_layer_state","dl_asset":<str|null>,"dl_short":<str|null>,"dl_full":<str|null>,
       "prior":{"is_loaded_in_editor":<bool?>,"initial_runtime_state":<str?>,"is_initially_visible":<bool?>}}
     -> invert: di = <resolve instance by dl_asset|dl_full|dl_short>;
                for each key present in prior, call the matching setter with the prior value.

FIXTURE: data layers REQUIRE a World-Partition level. Tests run on the WP scratch map
("/Game/MCP_Scratch/WP1_Scratch" from /Engine/Maps/Templates/OpenWorld). DataLayerAssets created
for validation are soft-deleted by rename into /Game/_MCP_Trash (never delete_asset) per the
scratch policy; create_data_layer's own ledger inverse DOES delete_asset (its intent is undo of a
just-created asset).
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
'''

    # Data-Layer helpers (subset of datalayers.py's _DL_HELPERS + resolve). No '''/no backslash.
    _DL_HELPERS = r'''
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _dls():
    return unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
def _all_instances():
    return list(_dls().get_all_data_layers() or [])
def _dl_asset(di):
    a = _try(lambda: di.get_asset())
    if a is None:
        a = _try(lambda: di.get_data_layer_asset())
    if a is None:
        a = _try(lambda: di.get_editor_property("data_layer_asset"))
    return a
def _dl_asset_path(di):
    a = _dl_asset(di)
    return _try(lambda: a.get_path_name()) if a is not None else None
def _dl_asset_name(di):
    a = _dl_asset(di)
    return _try(lambda: a.get_name()) if a is not None else None
def _dl_short(di):
    for m in ("get_data_layer_short_name", "get_data_layer_instance_name"):
        f = getattr(di, m, None)
        if f is not None:
            v = _try(lambda f=f: str(f()))
            if v:
                return v
    n = _dl_asset_name(di)
    if n:
        return n
    return _try(lambda: di.get_name())
def _dl_full(di):
    return _try(lambda: str(di.get_data_layer_full_name()))
def _dl_idents(di):
    s = set()
    for v in (_dl_short(di), _dl_full(di), _dl_asset_path(di), _dl_asset_name(di), _try(lambda: di.get_name())):
        if v:
            s.add(str(v))
    return s
def _dl_matches(di, ident):
    idset = _dl_idents(di)
    if ident in idset:
        return True
    for c in idset:
        if c.split(".")[-1] == ident or c.split("/")[-1] == ident:
            return True
    return False
def _resolve_dl(ident):
    return [di for di in _all_instances() if _dl_matches(di, ident)]
def _dl_state(di):
    return {
        "is_loaded_in_editor": _try(lambda: bool(di.is_loaded_in_editor())),
        "initial_runtime_state": _try(lambda: str(di.get_initial_runtime_state()).split(".")[-1].split(":")[0]),
        "is_initially_visible": _try(lambda: bool(di.is_initially_visible())),
    }
def _runtime_state_enum(name):
    if not name:
        return None
    key = str(name).split(".")[-1].split(":")[0].strip().upper()
    return _try(lambda: getattr(unreal.DataLayerRuntimeState, key))
def _dl_record(di):
    r = {"short_name": _dl_short(di), "full_name": _dl_full(di),
         "instance_object": _try(lambda: di.get_name()),
         "asset": _dl_asset_path(di), "asset_name": _dl_asset_name(di),
         "is_runtime": _try(lambda: bool(di.is_runtime()))}
    r.update(_dl_state(di))
    return r
'''

    # ------------------------------------------------------------------ #
    # create_data_layer -- new DataLayerAsset + instance (write)         #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
name = str(PARAMS.get("name") or "").strip()
package_path = str(PARAMS.get("path") or "/Game/MCP_Scratch").rstrip("/")
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
else:
    full = package_path + "/" + name
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
    else:
        dir_existed = unreal.EditorAssetLibrary.does_directory_exist(package_path)
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        fac = unreal.DataLayerFactory()
        asset = tools.create_asset(name, package_path, unreal.DataLayerAsset, fac)
        if asset is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for DataLayerAsset"}))
        else:
            try: unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
            except Exception: pass
            dls = _dls()
            di = None
            inst_err = None
            try:
                p = unreal.DataLayerCreationParameters()
                p.set_editor_property("data_layer_asset", asset)
                di = dls.create_data_layer_instance(p)
            except Exception as e:
                inst_err = str(e)[:200]
            created_dir = None if dir_existed else package_path
            _ledger().append({"op": "create_data_layer", "asset_path": asset.get_path_name(),
                "package_path": package_path, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success",
                "asset_path": asset.get_path_name(), "name": name,
                "instance_created": (di is not None),
                "instance_object": (di.get_name() if di is not None else None),
                "instance_error": inst_err,
                "state": (_dl_state(di) if di is not None else None),
                "ledger_depth": len(_ledger()),
                "note": "created DataLayerAsset via DataLayerFactory + plain create_asset (no modal), then instance via create_data_layer_instance(DataLayerCreationParameters). Inverse deletes the instance then the asset (and the dir if this call made it)."}))
'''

    @mcp.tool()
    def create_data_layer(ctx, name: str, path: str = "/Game/MCP_Scratch") -> str:
        """Create a new World Partition Data Layer: a DataLayerAsset + its instance. Ledgered write.

        name: asset name for the new UDataLayerAsset.
        path: destination content folder (default /Game/MCP_Scratch).

        Two non-modal steps: AssetTools.create_asset(name, path, DataLayerAsset, DataLayerFactory())
        then DataLayerEditorSubsystem.create_data_layer_instance(DataLayerCreationParameters with
        data_layer_asset set). Requires a World-Partition level (a WorldDataLayers actor must exist
        for the instance step; the asset is created regardless).

        Records op 'create_data_layer' {asset_path, package_path, created_dir}; the unified undo
        deletes the instance, then the asset, then the scratch dir if this call created it."""
        params = {"name": name, "path": path}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_data_layer -- delete an instance (write)                    #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
ident = str(PARAMS.get("name_or_asset") or "").strip()
if not ident:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name_or_asset is required"}))
else:
    hits = _resolve_dl(ident)
    if not hits:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "data layer instance not found: %s" % ident,
            "available": [_dl_short(di) for di in _all_instances()]}))
    elif len(hits) > 1:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "ambiguous identifier '%s' matched %d instances; use the asset path" % (ident, len(hits)),
            "matches": [_dl_asset_path(di) or _dl_short(di) for di in hits]}))
    else:
        di = hits[0]
        asset_path = _dl_asset_path(di)
        state = _dl_state(di)
        short = _dl_short(di)
        dls = _dls()
        if not asset_path:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "instance '%s' has no resolvable backing asset path; refusing to remove (inverse would be unrecoverable)" % short}))
        else:
            with unreal.ScopedEditorTransaction("MCP remove_data_layer"):
                dls.delete_data_layer(di)
            still = bool(_resolve_dl(ident))
            _ledger().append({"op": "remove_data_layer", "asset_path": asset_path,
                "initial_runtime_state": state.get("initial_runtime_state"),
                "is_initially_visible": state.get("is_initially_visible"),
                "is_loaded_in_editor": state.get("is_loaded_in_editor")})
            print("@@UMCP@@" + json.dumps({"status": "success", "removed": short,
                "asset_path": asset_path, "captured_state": state,
                "instance_still_present": still, "ledger_depth": len(_ledger()),
                "note": "delete_data_layer removes the INSTANCE only; the DataLayerAsset persists. Inverse recreates the instance from that asset and restores the captured state flags."}))
'''

    @mcp.tool()
    def remove_data_layer(ctx, name_or_asset: str) -> str:
        """Remove a World Partition Data Layer INSTANCE. Ledgered write (asset is preserved).

        name_or_asset: identifier of the target data layer (its DataLayerAsset path, asset name,
                       short name, or full name).

        Uses DataLayerEditorSubsystem.delete_data_layer(instance), which removes the instance from
        the WorldDataLayers actor but leaves the backing DataLayerAsset intact. Captures the asset
        path + the instance's state flags (initial_runtime_state, is_initially_visible,
        is_loaded_in_editor). Records op 'remove_data_layer' so the unified undo recreates the
        instance from the asset and restores those flags. Refuses if the instance has no resolvable
        backing asset (the inverse would be unrecoverable)."""
        try:
            return json.dumps(_exec(_REMOVE_BODY, {"name_or_asset": name_or_asset}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_data_layer_state -- persistent editor/runtime flags (write)    #
    # ------------------------------------------------------------------ #
    _SET_STATE_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
ident = str(PARAMS.get("name") or "").strip()
if not ident:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
else:
    hits = _resolve_dl(ident)
    if not hits:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "data layer instance not found: %s" % ident,
            "available": [_dl_short(di) for di in _all_instances()]}))
    elif len(hits) > 1:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "ambiguous identifier '%s' matched %d instances; use the asset path" % (ident, len(hits)),
            "matches": [_dl_asset_path(di) or _dl_short(di) for di in hits]}))
    else:
        di = hits[0]
        dls = _dls()
        want_loaded = PARAMS.get("is_loaded_in_editor")
        want_runtime = PARAMS.get("initial_runtime_state")
        want_visible = PARAMS.get("is_initially_visible")
        if want_runtime is not None and _runtime_state_enum(want_runtime) is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "invalid initial_runtime_state '%s'; expected one of ACTIVATED/LOADED/UNLOADED" % want_runtime}))
        else:
            before = _dl_state(di)
            prior = {}
            applied = {}
            with unreal.ScopedEditorTransaction("MCP set_data_layer_state"):
                if want_runtime is not None:
                    dls.set_data_layer_initial_runtime_state(di, _runtime_state_enum(want_runtime))
                if want_visible is not None:
                    dls.set_data_layer_is_initially_visible(di, bool(want_visible))
                if want_loaded is not None:
                    dls.set_data_layer_is_loaded_in_editor(di, bool(want_loaded), True)
            after = _dl_state(di)
            for k in ("is_loaded_in_editor", "initial_runtime_state", "is_initially_visible"):
                if before.get(k) != after.get(k):
                    prior[k] = before.get(k)
                    applied[k] = after.get(k)
            if prior:
                _ledger().append({"op": "set_data_layer_state",
                    "dl_asset": _dl_asset_path(di), "dl_short": _dl_short(di), "dl_full": _dl_full(di),
                    "prior": prior})
            print("@@UMCP@@" + json.dumps({"status": "success", "data_layer": _dl_short(di),
                "asset": _dl_asset_path(di), "before": before, "after": after,
                "changed": applied, "ledger_depth": len(_ledger()),
                "note": "persistent state flags via set_data_layer_initial_runtime_state / set_data_layer_is_initially_visible / set_data_layer_is_loaded_in_editor. Only changed flags are ledgered; inverse restores their captured prior values."}))
'''

    @mcp.tool()
    def set_data_layer_state(ctx, name: str, is_loaded_in_editor: bool = None,
                             initial_runtime_state: str = None,
                             is_initially_visible: bool = None) -> str:
        """Set a World Partition Data Layer's persistent editor/runtime state flags. Ledgered write.

        name:                   identifier of the target data layer (asset path/name, short/full name).
        is_loaded_in_editor:    (optional) load/unload the layer's actors in the editor.
        initial_runtime_state:  (optional) 'ACTIVATED' | 'LOADED' | 'UNLOADED' -- the saved initial
                                runtime state.
        is_initially_visible:   (optional) the saved initial editor visibility.

        Applies whichever arguments are provided via the DataLayerEditorSubsystem setters
        (set_data_layer_initial_runtime_state / set_data_layer_is_initially_visible /
        set_data_layer_is_loaded_in_editor). Captures each changed flag's prior value; op
        'set_data_layer_state' {dl ids, prior:{...}} records only the flags that actually changed,
        so the unified undo restores them exactly.

        (Distinct from set_data_layer_visibility in datalayers.py, which toggles the EPHEMERAL
        per-session view flag; these are the PERSISTENT saved properties.)"""
        params = {"name": name, "is_loaded_in_editor": is_loaded_in_editor,
                  "initial_runtime_state": initial_runtime_state,
                  "is_initially_visible": is_initially_visible}
        try:
            return json.dumps(_exec(_SET_STATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
