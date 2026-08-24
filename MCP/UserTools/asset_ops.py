"""Bulk asset-management writes (spec: docs/spec/asset_ops.md) — move/delete/fixup/consolidate.

PURE PYTHON — no C++. Reachability verified live 2026-08-17 against UE 5.8.1:
  - move (reference-preserving): unreal.AssetToolsHelpers.get_asset_tools().rename_assets([FAssetRenameData])
    -> updates referencers; when the source is unreferenced the old package is removed cleanly (no
    redirector); when referenced it leaves a redirector at the old path so refs still resolve.
  - consolidate / replace-references: unreal.EditorAssetLibrary.consolidate_assets(target, [sources])
    (repoints referencers onto one target; the scripting-safe, NON-modal variant — no ObjectTools dialog).
  - redirector enumeration: AssetRegistry.get_assets_by_class(TopLevelAssetPath("/Script/CoreUObject",
    "ObjectRedirector")). NOTE AssetTools.fixup_referencers is NOT bound in 5.8 Python, so referenced
    redirectors cannot be re-pointed from Python; fixup_redirectors therefore only removes ORPHANED
    (zero-referencer) redirectors.

Reversibility (per PROTOCOL — every ledgered write has a faithful inverse):
  - move_assets    -> REVERSIBLE. Ledger op "asset_move_batch" captures {from,to} per item; inverse
                      rename_assets each back (to -> from), LIFO. (Asset renames are not ScopedEditorTransaction
                      undoable, so — like create_asset in statetree_write.py — reversibility is ledger-driven,
                      NOT transaction-wrapped.)
  - delete_assets  -> SOFT delete (REVERSIBLE): moves each asset into a hidden /Game/_MCP_Trash folder via
                      rename (NOT destroyed). Ledger op "asset_soft_delete" captures {from,to}; inverse renames
                      back. Deliberately avoids EditorAssetLibrary.delete_asset -> GC -> python311.dll crash path
                      (CRASH MODE #2/#3). A true purge is DEFERRED (needs offline/human or a C++ guarded delete).
  - fixup_redirectors / replace_references -> IRREVERSIBLE (delete redirector stubs / repoint+drop sources).
                      NOT ledgered. Both default to a dry-run PREVIEW and require confirm=True to mutate.

Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM from
statetree_write.py. NEVER touch a real asset — validate only on /Game/MCP_Scratch scratch duplicates.
"""

import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from statetree_write.py / editor_level.py) ---
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


# NOTE: execute_python wraps incoming code in triple-SINGLE-quotes before exec -> snippet bodies must contain
# NO ''' and NO stray backslashes; all data passes as base64. Never name a local sys/unreal/traceback/
# output_file/error_file/original_stdout/original_stderr/success/user_code/code_obj.


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

    # ---- Unreal-side shared helpers (prepended to every body; no ''' / no backslash) ----
    _ASSET_HELPERS = r'''
import unreal, json, builtins, gc
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
_EAL = unreal.EditorAssetLibrary
_AT = unreal.AssetToolsHelpers.get_asset_tools()
_AR = unreal.AssetRegistryHelpers.get_asset_registry()
_TRASH = "/Game/_MCP_Trash"
def _pkg(p):
    # normalize an object path (/Game/X.X) or package path to a package path (/Game/X)
    p = str(p)
    seg = p.rsplit("/", 1)[-1]
    if "." in seg:
        return p.rsplit(".", 1)[0]
    return p
def _obj_path(pkg_path):
    nm = pkg_path.rsplit("/", 1)[-1]
    return pkg_path + "." + nm
def _dir_of(pkg_path):
    return pkg_path.rsplit("/", 1)[0]
def _name_of(pkg_path):
    return pkg_path.rsplit("/", 1)[-1]
def _class_name(pkg_path):
    try:
        ad = _AR.get_asset_by_object_path(_obj_path(pkg_path))
        if ad is not None and ad.is_valid():
            return str(ad.asset_class_path.asset_name)
    except Exception:
        pass
    o = _EAL.load_asset(pkg_path)
    return o.get_class().get_name() if o is not None else None
def _refs(pkg_path):
    try:
        return [str(x) for x in list(_EAL.find_package_referencers_for_asset(pkg_path, False) or [])]
    except Exception:
        return []
def _rename(src_pkg, dest_dir, new_name):
    # single reference-preserving move via AssetTools.rename_assets. Returns (ok, dest_pkg, err).
    o = _EAL.load_asset(src_pkg)
    if o is None:
        return (False, None, "load failed: %s" % src_pkg)
    ard = unreal.AssetRenameData()
    ard.set_editor_property("asset", o)
    ard.set_editor_property("new_package_path", dest_dir)
    ard.set_editor_property("new_name", new_name)
    ok = _AT.rename_assets([ard])
    return (bool(ok), dest_dir + "/" + new_name, None)
def _uniq_trash_name(name):
    cand = name; n = 1
    while _EAL.does_asset_exist(_TRASH + "/" + cand):
        cand = "%s_%d" % (name, n); n += 1
    return cand
'''

    # ------------------------------------------------------------------ #
    # move_assets — reference-preserving batch relocate/rename (REVERSIBLE)#
    # ------------------------------------------------------------------ #
    _MOVE_BODY = _ASSET_HELPERS + r'''
moves = PARAMS.get("moves") or []
if not isinstance(moves, list) or not moves:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "moves must be a non-empty list of {source,dest,new_name}"}))
else:
    plan = []; errs = []
    for m in moves:
        src = _pkg(m.get("source") or "")
        dest_dir = (m.get("dest") or "").rstrip("/") or None
        new_name = m.get("new_name") or None
        if not src or not _EAL.does_asset_exist(src):
            errs.append("source does not exist: %s" % src); continue
        d = dest_dir if dest_dir else _dir_of(src)
        nm = new_name if new_name else _name_of(src)
        dest_pkg = d + "/" + nm
        if dest_pkg == src:
            errs.append("no-op (dest == source): %s" % src); continue
        if _EAL.does_asset_exist(dest_pkg):
            errs.append("destination already exists: %s" % dest_pkg); continue
        plan.append({"from": src, "dir": d, "name": nm, "to": dest_pkg})
    if errs:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "aborted (no partial move)", "problems": errs}))
    else:
        done = []
        for pl in plan:
            ok, dest_pkg, err = _rename(pl["from"], pl["dir"], pl["name"])
            if not ok:
                # best-effort rollback of already-moved items, then report
                for d2 in reversed(done):
                    _rename(d2["to"], _dir_of(d2["from"]), _name_of(d2["from"]))
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "rename failed for %s (%s); rolled back %d" % (pl["from"], err, len(done))}))
                done = None; break
            _EAL.save_asset(dest_pkg, only_if_is_dirty=False)
            done.append(pl)
        if done is not None:
            _ledger().append({"op": "asset_move_batch", "moves": [{"from": d["from"], "to": d["to"]} for d in done]})
            print("@@UMCP@@" + json.dumps({"status": "success", "moved": len(done),
                "items": [{"from": d["from"], "to": d["to"]} for d in done], "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # delete_assets — SOFT delete (move to hidden /Game/_MCP_Trash) REVERSIBLE
    # ------------------------------------------------------------------ #
    _DELETE_BODY = _ASSET_HELPERS + r'''
paths = PARAMS.get("asset_paths") or []
force = bool(PARAMS.get("force"))
if not isinstance(paths, list) or not paths:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_paths must be a non-empty list"}))
else:
    moved = []; skipped = []
    for raw in paths:
        src = _pkg(raw or "")
        if not src or not _EAL.does_asset_exist(src):
            skipped.append({"path": src, "reason": "does not exist"}); continue
        if src.startswith(_TRASH):
            skipped.append({"path": src, "reason": "already in trash"}); continue
        rf = _refs(src)
        if rf and not force:
            skipped.append({"path": src, "reason": "referenced (pass force=True to soft-delete anyway)", "referencers": rf}); continue
        nm = _uniq_trash_name(_name_of(src))
        ok, dest_pkg, err = _rename(src, _TRASH, nm)
        if not ok:
            skipped.append({"path": src, "reason": "rename-to-trash failed: %s" % err}); continue
        _EAL.save_asset(dest_pkg, only_if_is_dirty=False)
        moved.append({"from": src, "to": dest_pkg})
    if moved:
        _ledger().append({"op": "asset_soft_delete", "items": moved})
    print("@@UMCP@@" + json.dumps({"status": "success" if moved else "noop", "soft_deleted": len(moved),
        "items": moved, "skipped": skipped, "trash": _TRASH,
        "note": "soft delete: assets moved to hidden _MCP_Trash (NOT destroyed); undo restores. Hard purge is deferred.",
        "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # fixup_redirectors — remove ORPHANED redirectors (IRREVERSIBLE; confirm)
    # ------------------------------------------------------------------ #
    _FIXUP_BODY = _ASSET_HELPERS + r'''
base = (PARAMS.get("path") or "/Game").rstrip("/") or "/Game"
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
confirm = bool(PARAMS.get("confirm"))
try:
    reds = _AR.get_assets_by_class(unreal.TopLevelAssetPath("/Script/CoreUObject", "ObjectRedirector"), False)
except Exception as e:
    reds = []
orphaned = []; referenced = []
for r in (reds or []):
    pkg = str(r.package_name)
    if pkg == base or pkg.startswith(base + "/"):
        if not recursive and _dir_of(pkg) != base:
            continue
        rf = _refs(pkg)
        if rf:
            referenced.append({"redirector": pkg, "referencers": rf})
        else:
            orphaned.append(pkg)
if not confirm:
    print("@@UMCP@@" + json.dumps({"status": "preview", "confirm_required": True,
        "would_delete_orphaned": orphaned, "still_referenced_left_alone": referenced,
        "note": "dry-run. Pass confirm=True to delete the orphaned redirectors. Referenced redirectors cannot be re-pointed from Python (AssetTools.fixup_referencers is not bound in 5.8); they are left untouched.",
        "reversible": False}))
else:
    gc.collect()
    deleted = []; failed = []
    for pkg in orphaned:
        try:
            if _EAL.delete_asset(pkg):
                deleted.append(pkg)
            else:
                failed.append(pkg)
        except Exception as e:
            failed.append(pkg + " :: " + str(e))
    print("@@UMCP@@" + json.dumps({"status": "success", "deleted_orphaned": deleted, "failed": failed,
        "still_referenced_left_alone": referenced, "reversible": False, "ledgered": False,
        "note": "IRREVERSIBLE: orphaned redirector stubs removed; not added to the undo ledger."}))
'''

    # ------------------------------------------------------------------ #
    # find_replacement_candidates — same-class substitution search (READ-ONLY)
    # ------------------------------------------------------------------ #
    _FIND_CAND_BODY = _ASSET_HELPERS + r'''
asset_path = _pkg(PARAMS.get("asset_path") or "")
name_filter = (PARAMS.get("name_filter") or "").lower()
base = PARAMS.get("path")
base = base.rstrip("/") if base else _dir_of(asset_path)
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
max_results = int(PARAMS.get("max_results") or 50)
if not asset_path or not _EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path does not exist: %s" % asset_path}))
else:
    want = _class_name(asset_path)
    cands = []
    try:
        ads = _AR.get_assets_by_path(unreal.Name(base), recursive, False)
    except Exception:
        ads = []
    for ad in (ads or []):
        try:
            pkg = str(ad.package_name)
            cls = str(ad.asset_class_path.asset_name)
        except Exception:
            continue
        if cls != want:
            continue
        if pkg == asset_path:
            continue
        nm = _name_of(pkg)
        if name_filter and name_filter not in nm.lower():
            continue
        cands.append({"path": pkg, "name": nm, "class": cls})
        if len(cands) >= max_results:
            break
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "class": want,
        "search_path": base, "recursive": recursive, "count": len(cands), "candidates": cands}))
'''

    # ------------------------------------------------------------------ #
    # replace_references — consolidate referencers onto one target (IRREVERSIBLE; confirm)
    # ------------------------------------------------------------------ #
    _REPLACE_BODY = _ASSET_HELPERS + r'''
sources = [_pkg(s) for s in (PARAMS.get("source_paths") or [])]
target = _pkg(PARAMS.get("target_path") or "")
delete_after = PARAMS.get("delete_after")
delete_after = True if delete_after is None else bool(delete_after)
confirm = bool(PARAMS.get("confirm"))
if not target or not _EAL.does_asset_exist(target):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "target_path does not exist: %s" % target}))
elif not sources:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "source_paths must be a non-empty list"}))
else:
    tcls = _class_name(target)
    problems = []; plan = []
    for s in sources:
        if s == target:
            problems.append("source == target: %s" % s); continue
        if not _EAL.does_asset_exist(s):
            problems.append("source does not exist: %s" % s); continue
        scls = _class_name(s)
        if scls != tcls:
            problems.append("class mismatch %s (%s) != target (%s)" % (s, scls, tcls)); continue
        plan.append({"source": s, "referencers": _refs(s)})
    if problems:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "aborted", "target_class": tcls, "problems": problems}))
    elif not confirm:
        print("@@UMCP@@" + json.dumps({"status": "preview", "confirm_required": True, "target": target,
            "target_class": tcls, "would_consolidate": plan, "delete_after": delete_after,
            "note": "dry-run. Pass confirm=True to repoint all referencers onto target and drop the sources.",
            "reversible": False}))
    else:
        gc.collect()
        to = _EAL.load_asset(target)
        src_objs = [_EAL.load_asset(p["source"]) for p in plan]
        ret = _EAL.consolidate_assets(to, src_objs)
        results = []
        for p in plan:
            s = p["source"]
            still = _EAL.does_asset_exist(s)
            purged = False
            if still and delete_after:
                gc.collect()
                try:
                    purged = bool(_EAL.delete_asset(s))
                except Exception:
                    purged = False
                still = _EAL.does_asset_exist(s)
            results.append({"source": s, "referencers_repointed": p["referencers"], "still_exists": still, "purged": purged})
        print("@@UMCP@@" + json.dumps({"status": "success", "consolidate_ret": bool(ret), "target": target,
            "results": results, "reversible": False, "ledgered": False,
            "note": "IRREVERSIBLE: referencers repointed onto target; sources dropped. Not added to the undo ledger."}))
'''

    # ============================ MCP TOOLS ============================ #

    @mcp.tool()
    def move_assets(ctx, moves: list, fixup: bool = False) -> str:
        """Relocate/rename many assets at once, updating all referencers (batch move).

        moves: list of {source, dest, new_name}. source = existing asset package path
               (/Game/.../Name). dest = destination FOLDER package path (optional; keeps the
               source folder when omitted). new_name = optional new asset name (keeps the source
               name when omitted). Destinations must not already exist; if any item is invalid the
               whole batch is aborted (no partial move).
        fixup: accepted for spec-compatibility; unreferenced moves already leave no redirector, and
               referenced moves leave a redirector so refs keep resolving. Run fixup_redirectors to
               sweep orphaned redirectors afterward.

        REVERSIBLE (ledger op 'asset_move_batch'): undo renames every item back (to -> from), LIFO."""
        try:
            return json.dumps(_exec(_MOVE_BODY, {"moves": moves, "fixup": fixup}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def delete_assets(ctx, asset_paths: list, force: bool = False) -> str:
        """Soft-delete many assets: move each into a hidden '/Game/_MCP_Trash' folder (NOT destroyed,
        so restoration is perfect). Referenced assets are skipped unless force=True (a redirector is
        left at the old path so referencers still resolve).

        asset_paths: list of asset package paths to remove.
        force:       soft-delete even assets that still have referencers.

        REVERSIBLE (ledger op 'asset_soft_delete'): undo renames each back (to -> from). This
        deliberately avoids EditorAssetLibrary.delete_asset's delete->GC->crash path; a true purge is
        DEFERRED (needs offline/human or a C++ guarded delete)."""
        try:
            return json.dumps(_exec(_DELETE_BODY, {"asset_paths": asset_paths, "force": force}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def fixup_redirectors(ctx, path: str = "/Game", recursive: bool = True, confirm: bool = False) -> str:
        """Resolve orphaned ObjectRedirectors under a content path by deleting the ones nothing
        references (standard redirector cleanup). Dry-run PREVIEW unless confirm=True.

        path:      content root/folder to scan (default '/Game').
        recursive: recurse into sub-folders (default True).
        confirm:   False (default) returns a preview of what would be deleted; True performs it.

        IRREVERSIBLE and NOT ledgered. Referenced redirectors are left untouched (re-pointing their
        referencers needs AssetTools.fixup_referencers, which is not bound in UE 5.8 Python)."""
        try:
            return json.dumps(_exec(_FIXUP_BODY, {"path": path, "recursive": recursive, "confirm": confirm}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def find_replacement_candidates(ctx, asset_path: str, name_filter: str = None,
                                    path: str = None, recursive: bool = True,
                                    max_results: int = 50) -> str:
        """Find assets of the same class as `asset_path` that could substitute for it (e.g. inputs to
        replace_references). Read-only.

        asset_path:  the asset whose class to match (required).
        name_filter: case-insensitive substring the candidate name must contain.
        path:        folder to search (default = asset_path's own folder).
        recursive:   recurse into sub-folders (default True).
        max_results: cap the number returned (default 50)."""
        params = {"asset_path": asset_path, "name_filter": name_filter, "path": path,
                  "recursive": recursive, "max_results": max_results}
        try:
            return json.dumps(_exec(_FIND_CAND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def replace_references(ctx, source_paths: list, target_path: str,
                           delete_after: bool = True, confirm: bool = False) -> str:
        """Consolidate references: repoint every referencer of the source assets onto a single target
        asset (same class), then drop the sources. Dry-run PREVIEW unless confirm=True.

        source_paths: list of asset package paths to replace (must be the same class as target).
        target_path:  the surviving asset every reference is redirected to.
        delete_after: purge any source that still lingers after consolidation (default True).
        confirm:      False (default) returns a preview; True performs the consolidation.

        IRREVERSIBLE and NOT ledgered (consolidation repoints referencers and removes the sources)."""
        params = {"source_paths": source_paths, "target_path": target_path,
                  "delete_after": delete_after, "confirm": confirm}
        try:
            return json.dumps(_exec(_REPLACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
