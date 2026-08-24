"""UserTools :: Asset Management (COMPLETION)  (spec: docs/spec/assets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Finishes the ASSETS
category: the 6 tools not covered by assets.py (READ) / assets_write.py / asset_ops.py.
Query convention, base64 PARAMS injection, Output-Log auto-capture and the per-session undo
ledger are copied VERBATIM from the gold-standard editor_level.py. All programmatic / non-modal
(imports use AssetImportTask.automated=True so no factory dialog can wedge the shared editor).

Tools:
  - set_asset_property (WRITE; ledgered)  reflection set on a loaded asset. REUSES the existing
        'set_object_property' ledger op (mode=scalar, target={asset_path}) which editor_level.undo
        ALREADY folds for asset targets -> NO new undo branch needed.
  - open_asset        (non-mutating) AssetEditorSubsystem.open_editor_for_assets. No ledger.
  - save_all          (benign) EditorLoadingAndSavingUtils.save_dirty_packages. No ledger.
  - import_asset      (WRITE; ledgered) AssetImportTask -> AssetTools.import_asset_tasks. Each
        imported asset is ledgered as the existing generic 'create_asset' op -> editor_level.undo
        ALREADY deletes it (gc.collect + close-editor + dir sweep). NO new undo branch needed.
  - import_assets_batch (WRITE; ledgered) same, over a list of files (or dir+extensions).
  - sync_browser      (non-mutating) EditorAssetLibrary.sync_browser_to_objects (folders via
        EditorUtilityLibrary.sync_browser_to_folders). No ledger.

BINDINGS CONFIRMED LIVE (5.8, this build):
  unreal.AssetImportTask (props: filename, destination_path, destination_name, replace_existing,
    automated, save, options, factory, imported_object_paths, result) -> YES
  AssetToolsHelpers.get_asset_tools().import_asset_tasks -> YES
  EditorLoadingAndSavingUtils.save_dirty_packages -> YES
  AssetEditorSubsystem.open_editor_for_assets -> YES
  EditorAssetLibrary.sync_browser_to_objects -> YES ; EditorUtilityLibrary.sync_browser_to_folders -> YES
  (AssetTools.sync_browser_to_assets -> NOT bound in 5.8; EAL.sync_browser_to_objects is the path.)

UNDO FOLD BURDEN FOR THE COORDINATOR: NONE. set_asset_property reuses 'set_object_property';
import_asset / import_assets_batch reuse 'create_asset'. Both branches are already live in
editor_level.undo (verified via a real editor_level.undo round-trip in this build).

NB: snippet bodies contain NO triple-single-quotes and NO stray backslashes (the plugin wraps
incoming code in triple-single-quotes before exec). All data is passed as base64 JSON via _exec.
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

    # Shared Unreal-side helpers — the session-aware _ledger/_settable/_coerce/_descend copied
    # VERBATIM from editor_level.py so set_asset_property's inverse is symmetric with the
    # editor_level.undo 'set_object_property' branch. No triple-single-quote / no backslash.
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
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off", ""):
                return False
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, str):
            try:
                return int(value.strip())
            except Exception:
                try:
                    return int(float(value.strip()))
                except Exception:
                    return value
        if isinstance(value, (int, float)):
            return int(value)
        return value
    if isinstance(current, float):
        if isinstance(value, str):
            try:
                return float(value.strip())
            except Exception:
                return value
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return value
def _descend(root, comp_name, path):
    container = root
    if comp_name:
        found = None
        for c in (root.get_components_by_class(unreal.ActorComponent) or []):
            if c.get_name() == comp_name:
                found = c; break
        if found is None:
            return None, None, "component not found: %s" % comp_name
        container = found
    segs = path.split(".")
    for s in segs[:-1]:
        nxt = container.get_editor_property(s)
        if not isinstance(nxt, unreal.Object):
            return None, None, "cannot descend into non-object '%s' (struct sub-paths unsupported)" % s
        container = nxt
    return container, segs[-1], None
def _pkgpath(p):
    p = str(p).rstrip("/")
    last = p.split("/")[-1]
    if "." in last:
        return p.rsplit(".", 1)[0]
    return p
def _topmost_new(d):
    d = str(d).rstrip("/")
    parts = [s for s in d.split("/") if s != ""]
    cur = ""
    for seg in parts:
        cur = cur + "/" + seg
        if not unreal.EditorAssetLibrary.does_directory_exist(cur):
            return cur
    return None
'''

    # ------------------------------------------------------------------ #
    # set_asset_property — reflection set on a loaded asset (ledgered)    #
    #   REUSES the 'set_object_property' op (scalar, asset target) which  #
    #   editor_level.undo already folds -> no new undo branch.            #
    # ------------------------------------------------------------------ #
    _SET_ASSET_PROP_BODY = _COERCE_HELPERS + r'''
asset_path = _pkgpath(PARAMS["asset_path"])
prop_path = PARAMS.get("property_path")
if not prop_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property_path is required"}))
elif not unreal.EditorAssetLibrary.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % asset_path}))
else:
    obj = unreal.EditorAssetLibrary.load_asset(asset_path)
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "failed to load asset: %s" % asset_path}))
    else:
        cont, final, derr = _descend(obj, None, prop_path)
        if derr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": derr}))
        else:
            try:
                cur_raw = cont.get_editor_property(final); have = True
            except Exception:
                have = False
            if not have:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property: %s" % final}))
            else:
                prior_json, restorable = _settable(cur_raw)
                newv = _coerce(cur_raw, PARAMS.get("value"))
                with unreal.ScopedEditorTransaction("MCP set_asset_property"):
                    cont.set_editor_property(final, newv)
                after_json, _u = _settable(cont.get_editor_property(final))
                do_save = PARAMS.get("save")
                saved = None
                if do_save:
                    try:
                        saved = bool(unreal.EditorAssetLibrary.save_asset(asset_path, False))
                    except Exception as _e:
                        saved = "error: %s" % _e
                # Ledger under the EXISTING 'set_object_property' op so editor_level.undo inverts it
                # (its branch loads target.asset_path, _descend, _coerce(prior), set_editor_property).
                _ledger().append({"op": "set_object_property", "mode": "scalar",
                    "target": {"actor": None, "asset_path": asset_path, "component": None},
                    "path": prop_path, "prior": prior_json, "restorable": restorable})
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
                    "property_path": prop_path, "before": prior_json, "after": after_json,
                    "restorable": restorable, "saved": saved, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_asset_property(ctx, asset_path: str, property_path: str, value=None,
                           save: bool = False) -> str:
        """Set a reflected UPROPERTY on an asset (ledgered write).

        asset_path:    package or object path of the asset (e.g. '/Game/Meshes/SM_Foo' or
                       '/Engine/EngineMaterials/DefaultMaterial.DefaultMaterial').
        property_path: dotted path through UOBJECT properties (Python snake_case). Descending into
                       STRUCT sub-fields is REFUSED (stock Python can't write struct sub-paths) —
                       set the whole struct property instead.
        value:         type-coerced from the CURRENT value (object props accept an asset path
                       string; Vector/Rotator/Color accept [..] lists; enums accept the member-name
                       string; bool/number/string pass through).
        save:          if True, persist the asset after the set (default False).

        This is the asset-scoped convenience form of objects.set_object_property; it records the
        SAME 'set_object_property' scalar ledger op (target={asset_path}) so the unified
        editor_level.undo already inverts it (prior value restored via _coerce). Returns
        { before, after, restorable, saved, ledger_depth }. No new undo fold is required."""
        params = {"asset_path": asset_path, "property_path": property_path,
                  "value": value, "save": save}
        try:
            return json.dumps(_exec(_SET_ASSET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # open_asset — open the asset editor for one or more assets (no undo) #
    # ------------------------------------------------------------------ #
    _OPEN_ASSET_BODY = r'''
import unreal, json
paths = PARAMS.get("asset_paths") or []
if isinstance(paths, str):
    paths = [paths]
loaded = []
missing = []
for p in paths:
    pp = str(p).rstrip("/")
    if "." in pp.split("/")[-1]:
        pp = pp.rsplit(".", 1)[0]
    if not unreal.EditorAssetLibrary.does_asset_exist(pp):
        missing.append(pp); continue
    o = unreal.EditorAssetLibrary.load_asset(pp)
    if o is not None:
        loaded.append(o)
opened = False
if loaded:
    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    opened = bool(aes.open_editor_for_assets(loaded))
print("@@UMCP@@" + json.dumps({"status": ("success" if loaded else "error"),
    "opened": opened, "opened_count": len(loaded),
    "opened_paths": [o.get_path_name() for o in loaded], "missing": missing,
    "message": (None if loaded else "no valid assets to open")}))
'''

    @mcp.tool()
    def open_asset(ctx, asset_paths) -> str:
        """Open the asset editor (tab) for one or more assets and bring it to front. Non-mutating;
        NOT ledgered (opening a tab changes no content).

        asset_paths: a single content path string, or a list of paths (package or object paths).

        Uses unreal.AssetEditorSubsystem.open_editor_for_assets. Returns { opened, opened_count,
        opened_paths, missing }."""
        try:
            return json.dumps(_exec(_OPEN_ASSET_BODY, {"asset_paths": asset_paths}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # save_all — save all dirty packages (benign; NOT ledgered)          #
    # ------------------------------------------------------------------ #
    _SAVE_ALL_BODY = r'''
import unreal, json
save_maps = PARAMS.get("save_map_packages")
save_content = PARAMS.get("save_content_packages")
save_maps = True if save_maps is None else bool(save_maps)
save_content = True if save_content is None else bool(save_content)
# EditorLoadingAndSavingUtils.save_dirty_packages(save_map_packages, save_content_packages) -> bool
ok = None
try:
    ok = bool(unreal.EditorLoadingAndSavingUtils.save_dirty_packages(save_maps, save_content))
except Exception as _e:
    ok = "error: %s" % _e
print("@@UMCP@@" + json.dumps({"status": "success", "saved": ok,
    "save_map_packages": save_maps, "save_content_packages": save_content,
    "note": "Persists all dirty packages; benign, not ledgered (nothing to revert)."}))
'''

    @mcp.tool()
    def save_all(ctx, save_map_packages: bool = True, save_content_packages: bool = True) -> str:
        """Save all currently-dirty packages (assets and/or maps) to disk. Benign write —
        NOT ledgered (persisting current state has nothing to revert).

        save_map_packages:     include dirty level/map packages (default True).
        save_content_packages: include dirty asset packages (default True).

        Uses unreal.EditorLoadingAndSavingUtils.save_dirty_packages. Returns { saved }.
        NB: this is non-interactive (no save-prompt) and writes only what is already dirty."""
        params = {"save_map_packages": save_map_packages, "save_content_packages": save_content_packages}
        try:
            return json.dumps(_exec(_SAVE_ALL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # import shared body — build AssetImportTask(s), run, ledger each     #
    #   imported asset under the EXISTING generic 'create_asset' op.      #
    # ------------------------------------------------------------------ #
    _IMPORT_BODY = _COERCE_HELPERS + r'''
import os as _os
def _norm_dest(d):
    d = str(d or "/Game").rstrip("/")
    if not d.startswith("/"):
        d = "/Game/" + d.lstrip("/")
    return d

specs = PARAMS.get("specs") or []
replace_existing = bool(PARAMS.get("replace_existing"))
do_save = PARAMS.get("save")
do_save = True if do_save is None else bool(do_save)

bad = []
tasks = []
task_meta = []
for sp in specs:
    fn = sp.get("filename")
    dest = _norm_dest(sp.get("destination_path"))
    dname = sp.get("destination_name")
    if not fn or not _os.path.isfile(fn):
        bad.append({"filename": fn, "reason": "source file not found on the editor host"})
        continue
    t = unreal.AssetImportTask()
    t.set_editor_property("filename", fn)
    t.set_editor_property("destination_path", dest)
    if dname:
        t.set_editor_property("destination_name", dname)
    t.set_editor_property("replace_existing", replace_existing)
    t.set_editor_property("automated", True)
    t.set_editor_property("save", do_save)
    tasks.append(t)
    task_meta.append({"filename": fn, "destination_path": dest,
                      "created_root": _topmost_new(dest)})

imported_all = []
if tasks:
    at = unreal.AssetToolsHelpers.get_asset_tools()
    at.import_asset_tasks(tasks)
    for t, meta in zip(tasks, task_meta):
        objpaths = list(t.get_editor_property("imported_object_paths") or [])
        for op in objpaths:
            pkg = _pkgpath(op)
            # Reuse the already-folded generic 'create_asset' inverse (delete on undo).
            _ledger().append({"op": "create_asset", "asset_path": pkg,
                              "package_path": meta["destination_path"],
                              "created_dir": meta["created_root"]})
            imported_all.append({"object_path": op, "package_path": pkg,
                                 "source": meta["filename"]})

status = "success" if imported_all else ("error" if (bad and not tasks) else "success")
print("@@UMCP@@" + json.dumps({"status": status, "imported_count": len(imported_all),
    "imported": imported_all, "skipped": bad, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def import_asset(ctx, filename: str, destination_path: str = "/Game",
                     destination_name: str = None, replace_existing: bool = False,
                     save: bool = True) -> str:
        """Import a single source file into the content browser as an asset (programmatic, no dialog).

        filename:         absolute path to the source file ON THE EDITOR HOST (e.g. a .png/.fbx/.wav).
        destination_path: content folder to import into (default '/Game'). Intermediate folders made.
        destination_name: optional asset name (defaults to the source file's base name).
        replace_existing: overwrite an existing asset of the same name (default False).
        save:             save the imported asset after import (default True).

        Builds an unreal.AssetImportTask with automated=True (suppresses factory dialogs) and runs
        AssetToolsHelpers.get_asset_tools().import_asset_tasks. Each resulting asset is ledgered under
        the existing generic 'create_asset' op {asset_path, package_path, created_dir}, so the unified
        editor_level.undo deletes it on undo (no new undo branch). Returns { imported, skipped }.
        NB: requires a real source file — if the path is not a file it is reported under 'skipped'."""
        spec = {"filename": filename, "destination_path": destination_path,
                "destination_name": destination_name}
        params = {"specs": [spec], "replace_existing": replace_existing, "save": save}
        try:
            return json.dumps(_exec(_IMPORT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def import_assets_batch(ctx, files: list = None, source_directory: str = None,
                            extensions: list = None, destination_path: str = "/Game",
                            replace_existing: bool = False, save: bool = True) -> str:
        """Import many source files at once (programmatic, no dialog).

        Provide EITHER:
          files:            a list of source file paths, or a list of {filename, destination_path,
                            destination_name} dicts (per-file destination overrides the default).
          source_directory: a host directory to scan; every file whose extension is in `extensions`
                            (e.g. ['.png','.fbx']) is imported. If `extensions` is omitted, all files
                            are attempted.
        destination_path: default content folder for files that don't specify their own (default '/Game').
        replace_existing: overwrite existing assets of the same name (default False).
        save:             save each imported asset (default True).

        Builds one unreal.AssetImportTask per file (automated=True; no dialog) and runs them in a
        single import_asset_tasks call. Each imported asset is ledgered under the existing generic
        'create_asset' op so editor_level.undo deletes them on undo (no new undo branch). Missing
        source files are reported under 'skipped'. Returns { imported_count, imported, skipped }."""
        specs = []
        if files:
            for f in files:
                if isinstance(f, dict):
                    specs.append({"filename": f.get("filename"),
                                  "destination_path": f.get("destination_path") or destination_path,
                                  "destination_name": f.get("destination_name")})
                else:
                    specs.append({"filename": f, "destination_path": destination_path,
                                  "destination_name": None})
        if source_directory:
            try:
                exts = [e.lower() if e.startswith(".") else "." + e.lower() for e in (extensions or [])]
                for entry in sorted(os.listdir(source_directory)):
                    full = os.path.join(source_directory, entry)
                    if not os.path.isfile(full):
                        continue
                    if exts and os.path.splitext(entry)[1].lower() not in exts:
                        continue
                    specs.append({"filename": full, "destination_path": destination_path,
                                  "destination_name": None})
            except Exception as e:
                return f"Error: could not scan source_directory: {e}"
        if not specs:
            return json.dumps({"status": "error",
                               "message": "provide `files` and/or `source_directory`"}, indent=2)
        params = {"specs": specs, "replace_existing": replace_existing, "save": save}
        try:
            return json.dumps(_exec(_IMPORT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # sync_browser — select/reveal assets (or folders) in Content Browser #
    # ------------------------------------------------------------------ #
    _SYNC_BODY = r'''
import unreal, json
asset_paths = PARAMS.get("asset_paths") or []
folder_paths = PARAMS.get("folder_paths") or []
if isinstance(asset_paths, str):
    asset_paths = [asset_paths]
if isinstance(folder_paths, str):
    folder_paths = [folder_paths]
loaded = []
missing = []
for p in asset_paths:
    pp = str(p).rstrip("/")
    if "." in pp.split("/")[-1]:
        pp = pp.rsplit(".", 1)[0]
    if not unreal.EditorAssetLibrary.does_asset_exist(pp):
        missing.append(pp); continue
    o = unreal.EditorAssetLibrary.load_asset(pp)
    if o is not None:
        loaded.append(o)
synced_assets = False
synced_folders = False
if loaded:
    unreal.EditorAssetLibrary.sync_browser_to_objects([o.get_path_name() for o in loaded])
    synced_assets = True
if folder_paths:
    try:
        unreal.EditorUtilityLibrary.sync_browser_to_folders([str(f).rstrip("/") for f in folder_paths])
        synced_folders = True
    except Exception:
        synced_folders = False
print("@@UMCP@@" + json.dumps({"status": ("success" if (synced_assets or synced_folders) else "error"),
    "synced_assets": synced_assets, "synced_asset_count": len(loaded),
    "synced_folders": synced_folders, "folder_count": len(folder_paths),
    "missing": missing,
    "message": (None if (synced_assets or synced_folders) else "nothing to sync (no valid assets/folders)")}))
'''

    @mcp.tool()
    def sync_browser(ctx, asset_paths=None, folder_paths=None) -> str:
        """Sync (select/reveal) the Content Browser to specific assets and/or folders. Non-mutating;
        NOT ledgered (navigation only).

        asset_paths:  a single path or a list of asset paths to select in the Content Browser.
        folder_paths: a single path or a list of content folders to reveal.

        Assets use unreal.EditorAssetLibrary.sync_browser_to_objects (the bound 5.8 path;
        AssetTools.sync_browser_to_assets is NOT exposed to Python here). Folders use
        unreal.EditorUtilityLibrary.sync_browser_to_folders. Returns { synced_assets,
        synced_asset_count, synced_folders, missing }."""
        params = {"asset_paths": asset_paths or [], "folder_paths": folder_paths or []}
        try:
            return json.dumps(_exec(_SYNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # UNDO FOLD: this module introduces NO new ledger op. It reuses two   #
    # branches already live in editor_level.undo:                         #
    #   set_asset_property   -> 'set_object_property' (scalar, asset tgt)  #
    #   import_asset/_batch   -> 'create_asset' (per imported asset)       #
    # open_asset / save_all / sync_browser are non-mutating (no ledger).  #
    # ------------------------------------------------------------------ #
