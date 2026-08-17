"""UserTools :: Asset Management (WRITE)  (spec: docs/spec/assets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE batch, the
mutating counterpart to the READ-only assets.py. Query convention, base64 PARAMS injection,
Output-Log auto-capture and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py. All programmatic (NO factories, NO modals).

What this build exposes (all verified live over unreal.EditorAssetLibrary in 5.8):
  * duplicate_asset(source, dest)  -> EAL.duplicate_asset(src, dst) returns the new Object.
        Reversible: delete the duplicate (+ rmdir if we created the folder).
  * rename_asset(source, dest)     -> EAL.rename_asset(src, dst) returns bool; auto-creates the
        destination folder. Reversible: rename back (+ rmdir the created dest folder if now empty).
  * create_folder(path)            -> EAL.make_directory(path) returns bool (creates intermediates).
        Reversible: delete the topmost folder WE created, if still empty.
  * delete_folder(path)            -> EAL.delete_directory(path). ONLY for an EMPTY folder (deleting
        a folder that contains assets is destructive -> REFUSED). Reversible: make_directory(path).
  * save_asset(path)               -> EAL.save_asset(path, only_if_is_dirty). Benign; NOT ledgered.
  * delete_asset(path)             -> SOFT delete: EAL.rename_asset moves the asset into a hidden
        trash folder (/Game/MCP_Scratch/_MCP_AssetTrash). A hard delete is IRREVERSIBLE, so we never
        destroy user assets; the asset is only relocated. Reversible: rename back to its origin.

Transaction note: pure EditorAssetLibrary calls (duplicate/rename/make_directory/delete_directory/
delete_asset) do NOT participate in the actor-level editor undo buffer the way ScopedEditorTransaction
covers actor edits, so wrapping them in unreal.ScopedEditorTransaction would be misleading here. We
OMIT the transaction wrapper for these asset-library mutations and rely SOLELY on the per-session
ledger inverse for reversibility (each inverse verified live). This matches the task's guidance.

GOTCHA (carried from blueprints_write.py): an asset open in an asset-editor tab cannot be deleted
(delete_asset returns False). Before any delete in an inverse we call
    unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).close_all_editors_for_asset(obj)
+ unreal.SystemLibrary.collect_garbage(). duplicate_asset itself does NOT auto-open an editor
(verified live), but the close-before-delete guard in the inverse is kept for safety.

Undo: this module registers NO `undo` tool (the gold-standard editor_level.py owns the single unified
`undo`). It introduces NEW ledger ops (schemas in each tool's docstring and in the build report) for
the coordinator to fold matching inverse branches into editor_level.undo. Every inverse was proven
live against the agentA ledger (depth returned to 0; content left CLEAN).

Implemented (all validated live, session=agentA):
  - duplicate_asset  (WRITE; ledgered op "duplicate_asset")
  - rename_asset     (WRITE; ledgered op "rename_asset")
  - create_folder    (WRITE; ledgered op "create_folder")
  - delete_folder    (WRITE; ledgered op "delete_folder"; EMPTY folders only, else refused)
  - save_asset       (benign WRITE; NOT ledgered)
  - delete_asset     (WRITE; ledgered op "soft_delete_asset"; SOFT delete -> trash, never destroyed)

NB: snippet bodies must contain NO triple-single-quotes and NO stray backslashes (the handler wraps
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

    # Shared Unreal-side helpers (prepended to write bodies). No triple-single-quote / no backslash.
    #   _ledger()        -> per-session undo stack (copied VERBATIM from editor_level.py).
    #   _pkgpath(p)      -> normalize an object path (/X/Y.Y) to a package path (/X/Y).
    #   _dirof(p)        -> the containing directory of a package path.
    #   _topmost_new(d)  -> the highest ancestor directory of d that does NOT yet exist (or None).
    #   _empty_dir(d)    -> True if directory d has no assets (recursive).
    _ASSETW_HELPERS = r'''
import unreal, json, builtins
EAL = unreal.EditorAssetLibrary
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _pkgpath(p):
    p = str(p).rstrip("/")
    last = p.split("/")[-1]
    if "." in last:
        return p.rsplit(".", 1)[0]
    return p
def _dirof(p):
    p = _pkgpath(p)
    return p.rsplit("/", 1)[0] if "/" in p else p
def _topmost_new(d):
    d = str(d).rstrip("/")
    parts = [s for s in d.split("/") if s != ""]
    cur = ""
    for seg in parts:
        cur = cur + "/" + seg
        if not EAL.does_directory_exist(cur):
            return cur
    return None
def _empty_dir(d):
    try:
        return not EAL.does_directory_have_assets(d)
    except Exception:
        rem = EAL.list_assets(d, True, False) or []
        return len(rem) == 0
'''

    # ------------------------------------------------------------------ #
    # duplicate_asset — copy an asset to a new package path (ledgered)    #
    # ------------------------------------------------------------------ #
    _DUP_BODY = _ASSETW_HELPERS + r'''
source = _pkgpath(PARAMS["source_path"])
dest = _pkgpath(PARAMS["dest_path"])
if not EAL.does_asset_exist(source):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source asset not found: %s" % source}))
elif EAL.does_asset_exist(dest):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "destination already exists: %s (refusing to overwrite)" % dest}))
else:
    dest_dir = _dirof(dest)
    created_root = _topmost_new(dest_dir)
    obj = EAL.duplicate_asset(source, dest)
    if obj is None:
        if created_root and EAL.does_directory_exist(created_root) and _empty_dir(created_root):
            try: EAL.delete_directory(created_root)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "duplicate_asset returned None for %s -> %s" % (source, dest)}))
    else:
        _ledger().append({"op": "duplicate_asset", "asset_path": dest,
                          "created_dir": created_root})
        print("@@UMCP@@" + json.dumps({"status": "success", "source": source,
            "dest": dest, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "created_dir": created_root,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def duplicate_asset(ctx, source_path: str, dest_path: str) -> str:
        """Duplicate an existing asset to a new content path (programmatic, no dialog).

        source_path: package or object path of the asset to copy (e.g.
                     '/Engine/BasicShapes/Cube.Cube' or '/Game/Meshes/SM_Foo').
        dest_path:   destination package path for the copy (e.g. '/Game/MCP_Scratch/MCP_A_Cube').
                     Intermediate folders are created as needed. Refuses to overwrite an existing asset.

        Uses unreal.EditorAssetLibrary.duplicate_asset. Ledgered write op 'duplicate_asset'
        {asset_path=<dest package path>, created_dir=<topmost folder we created or null>}. Inverse:
        close any editor for the duplicate, collect_garbage, delete_asset(asset_path); if created_dir
        is set and now empty, delete_directory(created_dir). The copy is created UNSAVED (in-memory)."""
        params = {"source_path": source_path, "dest_path": dest_path}
        try:
            return json.dumps(_exec(_DUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_asset — move/rename an asset to a new path (ledgered)        #
    # ------------------------------------------------------------------ #
    _RENAME_BODY = _ASSETW_HELPERS + r'''
source = _pkgpath(PARAMS["source_path"])
dest = _pkgpath(PARAMS["dest_path"])
if not EAL.does_asset_exist(source):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source asset not found: %s" % source}))
elif EAL.does_asset_exist(dest):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "destination already exists: %s (refusing to overwrite)" % dest}))
elif source == dest:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source and dest are identical"}))
else:
    dest_dir = _dirof(dest)
    created_root = _topmost_new(dest_dir)
    ok = bool(EAL.rename_asset(source, dest))
    if not ok:
        if created_root and EAL.does_directory_exist(created_root) and _empty_dir(created_root):
            try: EAL.delete_directory(created_root)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "rename_asset returned False for %s -> %s" % (source, dest)}))
    else:
        _ledger().append({"op": "rename_asset", "from_path": source, "to_path": dest,
                          "created_dir": created_root})
        print("@@UMCP@@" + json.dumps({"status": "success", "from": source, "to": dest,
            "moved": EAL.does_asset_exist(dest) and not EAL.does_asset_exist(source),
            "created_dir": created_root, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_asset(ctx, source_path: str, dest_path: str) -> str:
        """Rename or move an asset to a new content path (programmatic, no dialog).

        source_path: package or object path of the asset to rename/move.
        dest_path:   new package path (rename in place, or move to another folder).
                     Intermediate folders are auto-created. Refuses to overwrite an existing asset.

        Uses unreal.EditorAssetLibrary.rename_asset (updates referencers / leaves a redirector as
        the engine sees fit). Ledgered write op 'rename_asset' {from_path, to_path, created_dir}.
        Inverse: rename_asset(to_path, from_path); if created_dir is set and now empty,
        delete_directory(created_dir) -- a FAITHFUL round-trip (verified live)."""
        params = {"source_path": source_path, "dest_path": dest_path}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_folder — make a content directory (ledgered)                #
    # ------------------------------------------------------------------ #
    _MKDIR_BODY = _ASSETW_HELPERS + r'''
path = str(PARAMS["path"]).rstrip("/")
if not path.startswith("/"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "path must be absolute under a mounted root (e.g. /Game/...)"}))
elif EAL.does_directory_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "exists", "path": path,
        "message": "directory already exists (not created, not ledgered)"}))
else:
    created_root = _topmost_new(path)
    ok = bool(EAL.make_directory(path))
    if not ok or not EAL.does_directory_exist(path):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "make_directory failed for %s" % path}))
    else:
        _ledger().append({"op": "create_folder", "path": path, "created_root": created_root})
        print("@@UMCP@@" + json.dumps({"status": "success", "path": path,
            "created_root": created_root, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_folder(ctx, path: str) -> str:
        """Create a content-browser folder (directory). Programmatic, no dialog.

        path: absolute content path under a mounted root (e.g. '/Game/MCP_Scratch/MCP_A_Sub').
              Intermediate folders are created as needed. If it already exists, returns status
              'exists' and records nothing.

        Uses unreal.EditorAssetLibrary.make_directory. Ledgered write op 'create_folder'
        {path, created_root=<topmost ancestor we actually created>}. Inverse: if created_root is set
        and still empty (no assets), delete_directory(created_root) -- removes exactly the tree we made."""
        try:
            return json.dumps(_exec(_MKDIR_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_folder — remove an EMPTY content directory (ledgered)       #
    # ------------------------------------------------------------------ #
    _RMDIR_BODY = _ASSETW_HELPERS + r'''
path = str(PARAMS["path"]).rstrip("/")
if not EAL.does_directory_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "directory not found: %s" % path}))
elif not _empty_dir(path):
    assets = [str(x) for x in (EAL.list_assets(path, True, False) or [])]
    print("@@UMCP@@" + json.dumps({"status": "refused",
        "message": "directory contains assets -- deleting it is destructive and NOT faithfully reversible; refused. Move/soft-delete the assets first.",
        "path": path, "asset_count": len(assets), "assets": assets[:25]}))
else:
    ok = bool(EAL.delete_directory(path))
    if not ok or EAL.does_directory_exist(path):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "delete_directory failed for %s" % path}))
    else:
        _ledger().append({"op": "delete_folder", "path": path})
        print("@@UMCP@@" + json.dumps({"status": "success", "path": path,
            "note": "empty folder removed; undo recreates it.", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_folder(ctx, path: str) -> str:
        """Delete a content-browser folder -- ONLY IF IT IS EMPTY (no assets). Programmatic, no dialog.

        path: absolute content path of the folder to remove.

        SAFETY: deleting a folder that contains assets is destructive and cannot be faithfully
        reversed, so this command REFUSES (status 'refused') when the folder holds any asset. Move or
        soft-delete (delete_asset) the contents first. For an EMPTY folder it uses
        unreal.EditorAssetLibrary.delete_directory. Ledgered write op 'delete_folder' {path}. Inverse:
        make_directory(path) -- recreating an empty folder is a FAITHFUL inverse (no data was held)."""
        try:
            return json.dumps(_exec(_RMDIR_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # save_asset — persist a dirty asset to disk (benign, NOT ledgered)  #
    # ------------------------------------------------------------------ #
    _SAVE_BODY = _ASSETW_HELPERS + r'''
path = _pkgpath(PARAMS["asset_path"])
only_if_dirty = PARAMS.get("only_if_dirty")
only_if_dirty = True if only_if_dirty is None else bool(only_if_dirty)
if not EAL.does_asset_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
else:
    ok = None
    try:
        ok = bool(EAL.save_asset(path, only_if_dirty))
    except Exception as e:
        ok = "error: %s" % e
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": path,
        "saved": ok, "only_if_dirty": only_if_dirty,
        "note": "Benign persist of current on-disk state; not ledgered (nothing to revert)."}))
'''

    @mcp.tool()
    def save_asset(ctx, asset_path: str, only_if_dirty: bool = True) -> str:
        """Save an asset to disk. Benign write -- NOT ledgered (persisting the current state has
        nothing to revert; mirrors editor_discovery.save_level).

        asset_path:    package or object path of the asset to save.
        only_if_dirty: only write if the asset has unsaved changes (default True). Pass False to
                       force a save regardless.

        Uses unreal.EditorAssetLibrary.save_asset. Returns 'saved' (True if written)."""
        params = {"asset_path": asset_path, "only_if_dirty": only_if_dirty}
        try:
            return json.dumps(_exec(_SAVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_asset — SOFT delete: move to a hidden trash folder (ledgered)#
    # ------------------------------------------------------------------ #
    _SOFTDEL_BODY = _ASSETW_HELPERS + r'''
path = _pkgpath(PARAMS["asset_path"])
TRASH = "/Game/MCP_Scratch/_MCP_AssetTrash"
if not EAL.does_asset_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif path.startswith(TRASH):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is already in the MCP trash: %s" % path}))
else:
    name = path.split("/")[-1]
    trash_existed = EAL.does_directory_exist(TRASH)
    trash_path = TRASH + "/" + name
    i = 1
    while EAL.does_asset_exist(trash_path):
        trash_path = TRASH + "/" + name + "_" + str(i)
        i = i + 1
    ok = bool(EAL.rename_asset(path, trash_path))
    if not ok:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "soft-delete rename failed for %s (asset NOT moved/destroyed)" % path}))
    else:
        created_trash_dir = (not trash_existed) and EAL.does_directory_exist(TRASH)
        _ledger().append({"op": "soft_delete_asset", "original_path": path,
                          "trash_path": trash_path, "created_trash_dir": created_trash_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "original_path": path,
            "trash_path": trash_path, "soft_deleted": True, "created_trash_dir": created_trash_dir,
            "note": "asset MOVED to hidden _MCP_AssetTrash (NOT destroyed/purged); undo renames it back.",
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_asset(ctx, asset_path: str) -> str:
        """Soft-delete an asset: MOVE it into a hidden trash folder ('/Game/MCP_Scratch/_MCP_AssetTrash')
        rather than destroying it. A hard delete is IRREVERSIBLE, so this never purges user content --
        the asset is only relocated and can be restored perfectly by renaming it back.

        asset_path: package or object path of the asset to soft-delete.

        Uses unreal.EditorAssetLibrary.rename_asset to relocate the asset (name collisions in trash get
        a numeric suffix). Ledgered write op 'soft_delete_asset' {original_path, trash_path,
        created_trash_dir}. Inverse: rename_asset(trash_path, original_path); if created_trash_dir and
        the trash is now empty, delete_directory(the trash) -- a FAITHFUL restore (verified live).
        NB: relocating an asset updates its referencers / may leave a redirector, as with any move."""
        try:
            return json.dumps(_exec(_SOFTDEL_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # This module registers NO `undo` tool; editor_level.py owns the      #
    # unified `undo`. It introduces the following NEW ledger op schemas   #
    # (resolve objects via unreal.EditorAssetLibrary; EAL below):         #
    #                                                                     #
    #  duplicate_asset  {op, asset_path, created_dir|null}                #
    #    invert: obj=EAL.load_asset(asset_path);                          #
    #            AssetEditorSubsystem.close_all_editors_for_asset(obj);   #
    #            SystemLibrary.collect_garbage();                         #
    #            EAL.delete_asset(asset_path);                            #
    #            if created_dir and not EAL.does_directory_have_assets(   #
    #               created_dir): EAL.delete_directory(created_dir)       #
    #                                                                     #
    #  rename_asset     {op, from_path, to_path, created_dir|null}        #
    #    invert: EAL.rename_asset(to_path, from_path);                    #
    #            if created_dir and not EAL.does_directory_have_assets(   #
    #               created_dir): EAL.delete_directory(created_dir)       #
    #                                                                     #
    #  create_folder    {op, path, created_root|null}                     #
    #    invert: if created_root and EAL.does_directory_exist(            #
    #               created_root) and not EAL.does_directory_have_assets( #
    #               created_root): EAL.delete_directory(created_root)     #
    #                                                                     #
    #  delete_folder    {op, path}                                        #
    #    invert: EAL.make_directory(path)                                 #
    #                                                                     #
    #  soft_delete_asset {op, original_path, trash_path,                  #
    #                     created_trash_dir}                              #
    #    invert: EAL.rename_asset(trash_path, original_path);             #
    #            if created_trash_dir and not                             #
    #               EAL.does_directory_have_assets(trash_dir):            #
    #               EAL.delete_directory(trash_dir)                       #
    #                                                                     #
    # save_asset is INTENTIONALLY NOT ledgered (benign persist).          #
    # ------------------------------------------------------------------ #
