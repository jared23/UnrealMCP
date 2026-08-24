"""UserTools :: World Partition HLOD  (spec: docs/spec/editor.md -- "HLOD")

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Create/list HLODLayer assets
and assign one to an actor. Mirrors editor_level.py's conventions VERBATIM (base64 PARAMS,
Output-Log capture, @@UMCP@@ marker, session-aware _ledger(), ScopedEditorTransaction on writes).

A UHLODLayer is a content asset describing how World Partition builds Hierarchical LODs for a
runtime cell (its cell_size / loading_range / layer_type). Created non-modally via:
  asset = AssetToolsHelpers.get_asset_tools().create_asset(name, path, unreal.HLODLayer,
          unreal.HLODLayerFactory())
then reflected props are set with asset.set_editor_property(...). Live-verified settable props:
  cell_size (int)         loading_range (float)
  layer_type (unreal.HLODLayerType: INSTANCING | MESH_MERGE | MESH_SIMPLIFY | MESH_APPROXIMATE
              | CUSTOM | CUSTOM_HLOD_ACTOR)
  parent_layer (HLODLayer)     is_spatially_loaded (bool)
(HLODLayer props are reachable ONLY via get_/set_editor_property, not as python getset descriptors.)

An actor's HLOD layer is the reflected UPROPERTY  hlod_layer  (there is NO python-exposed C++
SetHLODLayer on the python Actor, verified), set via actor.set_editor_property("hlod_layer", asset).

Implemented:
  - create_hlod_layer   (write; ledgered create_hlod_layer -> delete asset)
  - list_hlod_layers    (read-only; AssetRegistry scan for UHLODLayer assets + their props)
  - set_actor_hlod_layer(write; ledgered set_actor_hlod_layer -> restore prior hlod_layer)

This module defines NO `undo` tool; the coordinator folds the inverse branches into
editor_level.py's unified `undo`.

NEW ledger op schemas:
  create_hlod_layer:
      {"op":"create_hlod_layer","asset_path":<str>,"package_path":<str>,"created_dir":<str|null>}
     -> invert: unreal.EditorAssetLibrary.delete_asset(asset_path);
                if created_dir: unreal.EditorAssetLibrary.delete_directory(created_dir)
     (Same delete-asset mechanics as the existing 'create_asset' op, but named so the coordinator
      folds a dedicated branch; either branch works -- the inverse is identical.)
  set_actor_hlod_layer:
      {"op":"set_actor_hlod_layer","actor_name":<uniq>,"prior_path":<str|null>}
     -> invert: a = _find_by_name(actor_name);
                a.set_editor_property("hlod_layer", (load_asset(prior_path) if prior_path else None))

FIXTURE: HLOD assets are level-agnostic (createable/assignable outside WP), but assignment is only
meaningful on a WP actor, so tests run on the WP scratch map ("/Game/MCP_Scratch/WP1_Scratch").
Validation HLODLayer assets are soft-deleted by rename into /Game/_MCP_Trash (never delete_asset)
per the scratch policy; create_hlod_layer's own ledger inverse DOES delete_asset (undo of a
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
'''

    _HLOD_HELPERS = r'''
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _hlod_type_enum(name):
    if not name:
        return None
    key = str(name).split(".")[-1].split(":")[0].strip().upper()
    return _try(lambda: getattr(unreal.HLODLayerType, key))
def _hlod_props(asset):
    return {
        "cell_size": _try(lambda: asset.get_editor_property("cell_size")),
        "loading_range": _try(lambda: asset.get_editor_property("loading_range")),
        "layer_type": _try(lambda: str(asset.get_editor_property("layer_type")).split(".")[-1].split(":")[0]),
        "is_spatially_loaded": _try(lambda: bool(asset.get_editor_property("is_spatially_loaded"))),
        "parent_layer": _try(lambda: (asset.get_editor_property("parent_layer").get_path_name() if asset.get_editor_property("parent_layer") else None)),
    }
'''

    # ------------------------------------------------------------------ #
    # create_hlod_layer -- new HLODLayer asset (write)                   #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _COERCE_HELPERS + _HLOD_HELPERS + r'''
name = str(PARAMS.get("name") or "").strip()
package_path = str(PARAMS.get("path") or "/Game/MCP_Scratch").rstrip("/")
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
else:
    full = package_path + "/" + name
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
    else:
        want_type = PARAMS.get("layer_type")
        if want_type is not None and _hlod_type_enum(want_type) is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "invalid layer_type '%s'; expected one of INSTANCING/MESH_MERGE/MESH_SIMPLIFY/MESH_APPROXIMATE/CUSTOM/CUSTOM_HLOD_ACTOR" % want_type}))
        else:
            dir_existed = unreal.EditorAssetLibrary.does_directory_exist(package_path)
            tools = unreal.AssetToolsHelpers.get_asset_tools()
            fac = unreal.HLODLayerFactory()
            asset = tools.create_asset(name, package_path, unreal.HLODLayer, fac)
            if asset is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for HLODLayer"}))
            else:
                if PARAMS.get("cell_size") is not None:
                    _try(lambda: asset.set_editor_property("cell_size", int(PARAMS.get("cell_size"))))
                if PARAMS.get("loading_range") is not None:
                    _try(lambda: asset.set_editor_property("loading_range", float(PARAMS.get("loading_range"))))
                if want_type is not None:
                    _try(lambda: asset.set_editor_property("layer_type", _hlod_type_enum(want_type)))
                if PARAMS.get("parent_layer"):
                    pl = _try(lambda: unreal.EditorAssetLibrary.load_asset(PARAMS.get("parent_layer")))
                    if pl is not None:
                        _try(lambda: asset.set_editor_property("parent_layer", pl))
                try: unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
                except Exception: pass
                created_dir = None if dir_existed else package_path
                _ledger().append({"op": "create_hlod_layer", "asset_path": asset.get_path_name(),
                    "package_path": package_path, "created_dir": created_dir})
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "asset_path": asset.get_path_name(), "name": name,
                    "props": _hlod_props(asset), "ledger_depth": len(_ledger()),
                    "note": "created via HLODLayerFactory + plain create_asset (no modal) then set_editor_property. Inverse deletes the asset (and the dir if this call made it)."}))
'''

    @mcp.tool()
    def create_hlod_layer(ctx, name: str, path: str = "/Game/MCP_Scratch",
                          cell_size: int = None, loading_range: float = None,
                          layer_type: str = None, parent_layer: str = None) -> str:
        """Create a new World Partition HLODLayer asset. Ledgered write.

        name:          asset name for the new UHLODLayer.
        path:          destination content folder (default /Game/MCP_Scratch).
        cell_size:     (optional) int runtime-cell size for this HLOD layer.
        loading_range: (optional) float loading range.
        layer_type:    (optional) INSTANCING | MESH_MERGE | MESH_SIMPLIFY | MESH_APPROXIMATE |
                       CUSTOM | CUSTOM_HLOD_ACTOR.
        parent_layer:  (optional) content path to another HLODLayer to set as parent.

        Created non-modally via AssetTools.create_asset(name, path, HLODLayer, HLODLayerFactory()),
        then the provided properties are applied with set_editor_property. Records op
        'create_hlod_layer' {asset_path, package_path, created_dir}; the unified undo deletes the
        created asset (and the scratch dir if this call created it)."""
        params = {"name": name, "path": path, "cell_size": cell_size,
                  "loading_range": loading_range, "layer_type": layer_type,
                  "parent_layer": parent_layer}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_hlod_layers -- AssetRegistry scan (read-only)                 #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _HLOD_HELPERS + r'''
import unreal, json
ar = unreal.AssetRegistryHelpers.get_asset_registry()
found = []
try:
    ar_filter = unreal.ARFilter(class_names=["HLODLayer"], recursive_classes=True,
                                recursive_paths=True, package_paths=["/Game"])
    datas = ar.get_assets(ar_filter) or []
except Exception:
    datas = []
seen = set()
for ad in datas:
    path = _try(lambda ad=ad: str(ad.get_editor_property("package_name")))
    obj_path = None
    try:
        obj_path = str(ad.package_name) + "." + str(ad.asset_name)
    except Exception:
        obj_path = path
    if obj_path in seen:
        continue
    seen.add(obj_path)
    asset = _try(lambda op=obj_path: unreal.EditorAssetLibrary.load_asset(op))
    rec = {"path": obj_path, "name": _try(lambda ad=ad: str(ad.asset_name))}
    if asset is not None:
        rec.update(_hlod_props(asset))
    found.append(rec)
found.sort(key=lambda r: r.get("path") or "")
print("@@UMCP@@" + json.dumps({"status": "success", "hlod_layer_count": len(found),
    "hlod_layers": found,
    "note": "AssetRegistry scan for UHLODLayer assets under /Game; per-asset cell_size/loading_range/layer_type/is_spatially_loaded/parent_layer read via reflection."}))
'''

    @mcp.tool()
    def list_hlod_layers(ctx) -> str:
        """List every World Partition HLODLayer asset under /Game. Read-only.

        Scans the AssetRegistry for UHLODLayer assets and, for each, reports its object path, name,
        and reflected properties: cell_size, loading_range, layer_type, is_spatially_loaded,
        parent_layer."""
        try:
            return json.dumps(_exec(_LIST_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_hlod_layer -- assign an HLODLayer to an actor (write)    #
    # ------------------------------------------------------------------ #
    _SET_BODY = _COERCE_HELPERS + _HLOD_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    layer_path = PARAMS.get("hlod_layer_path")
    layer_path = None if layer_path in (None, "", "None") else str(layer_path)
    try:
        prior = actor.get_editor_property("hlod_layer")
        prior_path = prior.get_path_name() if prior else None
        has_prop = True
    except Exception:
        prior_path = None
        has_prop = False
    if not has_prop:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "actor has no hlod_layer property (not a WP-partitioned actor?): %s" % PARAMS["actor_name"]}))
    else:
        new_asset = None
        if layer_path is not None:
            new_asset = _try(lambda: unreal.EditorAssetLibrary.load_asset(layer_path))
            if new_asset is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load HLODLayer asset: %s" % layer_path}))
                new_asset = "__ERR__"
        if new_asset != "__ERR__":
            with unreal.ScopedEditorTransaction("MCP set_actor_hlod_layer"):
                actor.set_editor_property("hlod_layer", new_asset)
            after = actor.get_editor_property("hlod_layer")
            after_path = after.get_path_name() if after else None
            if after_path != prior_path:
                _ledger().append({"op": "set_actor_hlod_layer",
                    "actor_name": actor.get_name(), "prior_path": prior_path})
            print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                "label": actor.get_actor_label(), "before": prior_path, "after": after_path,
                "changed": (after_path != prior_path), "ledger_depth": len(_ledger()),
                "note": "hlod_layer set via set_editor_property (no python C++ SetHLODLayer exists). Pass hlod_layer_path=None to clear. Inverse restores the captured prior asset path."}))
'''

    @mcp.tool()
    def set_actor_hlod_layer(ctx, actor_name: str, hlod_layer_path: str = None) -> str:
        """Assign (or clear) a World Partition actor's HLODLayer. Ledgered write.

        actor_name:      actor display label (preferred) or unique internal name.
        hlod_layer_path: content path to an HLODLayer asset, or None/'None' to clear the assignment.

        Written via actor.set_editor_property('hlod_layer', asset) inside a transaction (no
        python-exposed C++ SetHLODLayer). Captures the prior HLODLayer path; op
        'set_actor_hlod_layer' {actor_name, prior_path} is recorded only if the value changed, so
        the unified undo restores exactly the prior assignment."""
        params = {"actor_name": actor_name, "hlod_layer_path": hlod_layer_path}
        try:
            return json.dumps(_exec(_SET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
