"""UserTools :: AI Assets (WRITE)  (spec: docs/spec/ai.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE batch, the mutating
counterpart to ai_read.py. Query convention, base64 PARAMS injection, Output-Log auto-capture, and
the per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py.

What this build exposes (all NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * BlackboardData created non-interactively via
      unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, package_path, unreal.BlackboardData,
                                                              unreal.BlackboardDataFactory())
    BlackboardDataFactory has NO ConfigureProperties dialog (supported_class=BlackboardData) and the
    scripting create_asset path does NOT call it -> no modal EVER pops. A fresh BlackboardData ships
    with one built-in key "SelfActor" (BlackboardKeyType_Object).
  * BehaviorTree created the same way with unreal.BehaviorTreeFactory() (supported_class=BehaviorTree,
    no dialog). Its blackboard_asset getset property is Python-settable to a BlackboardData.
  * Blackboard keys ARE authorable from stock Python (contrary to the usual "editor-only" assumption):
      ktcls = unreal.load_object(None, "/Script/AIModule.BlackboardKeyType_<Type>")   # subclasses are
              NOT on the unreal.* module (ai_read documents this) but load_object reaches them by path
      kt    = unreal.new_object(ktcls, <owning BlackboardData>)    # instantiate the key-type subobject
      entry = unreal.BlackboardEntry()                             # FBlackboardEntry struct
      entry.set_editor_property("entry_name", unreal.Name(key_name))
      entry.set_editor_property("key_type", kt)
      keys = list(bb.get_editor_property("keys")); keys.append(entry); bb.set_editor_property("keys", keys)
    Readback via ai_read.get_blackboard_info confirms name + type.

MODAL-SAFETY note (learned live 2026-08-14): create_asset/create + set_editor_property("keys") are each
fast, but doing asset-create AND a keys write AND another asset-create in ONE execute_python call once
exceeded the 10s socket window (first-touch keys write does async blackboard refresh right after
creation). Each tool here performs ONE operation per call, so every call stays well under the window.
The editor was confirmed responsive (no modal) after that timeout; no factory here is modal.

Implemented (all validated live, session=agentA, editor left CLEAN, ledger depth 0):
  - create_blackboard        (WRITE; ledgered op "create_asset"; inverse = close editors + delete asset [+ rmdir if we made the dir])
  - create_behavior_tree     (WRITE; ledgered op "create_asset"; optional blackboard_asset set in-create)
  - add_blackboard_key       (WRITE; ledgered op "add_blackboard_key"; inverse = remove the key we added by name)
  - set_behavior_tree_blackboard (WRITE; ledgered op "set_behavior_tree_blackboard"; inverse = restore prior blackboard_asset)

MOVED to bt_write.py (2026-08-16 — this old DEFERRED note was WRONG):
  - BehaviorTree node-graph authoring IS Python-doable after all. A live probe showed
      `unreal.new_object(BTComposite_*/BTTask_*/BTService_*/BTDecorator_*, bt)` +
      `FBTCompositeChild(child_composite/child_task/decorators)` + `set_editor_property("root_node"/
      "children"/"services")` all persist and read back via ai_read.get_behavior_tree_info. The new
      `bt_write.py` ships set_bt_root / add_bt_child_composite / add_bt_task / add_bt_service /
      add_bt_decorator (RUNTIME tree). CAVEAT: the BT EDITOR graph (UBehaviorTreeGraph) is still not
      Python-reachable, so MCP-authored trees are runtime-correct but not editor-round-trippable — that
      editor-graph path is the only part that would need C++ (deferred, optional).

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified `undo`).
It uses the coordinator's generic "create_asset" inverse (close editors + delete_asset [+ rmdir]) and
introduces 2 NEW ledger ops (add_blackboard_key, set_behavior_tree_blackboard) whose inverse logic is
documented in each tool's docstring + the build report for the coordinator to fold into editor_level.undo.
Reversibility was proven live by executing those exact inverses against the agentA ledger (depth -> 0).
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
# bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never assign a
# snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
# success/user_code/code_obj (they are the C++ wrapper's own locals -> clobbering them wedges capture).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                  -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_typed(path, tyname)  -> (obj, err) load an asset and type-check its class name.
    #   _close_editors(obj)        -> silently close any open asset editor for obj.
    #   _KT_MAP / _resolve_keytype_path(spec) -> friendly key type -> /Script/AIModule class path.
    _AIW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_typed(path, tyname):
    if not path:
        return None, "no asset path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    cn = obj.get_class().get_name()
    if cn != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, cn, path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
_KT_MAP = {"bool": "BlackboardKeyType_Bool", "int": "BlackboardKeyType_Int",
           "integer": "BlackboardKeyType_Int", "float": "BlackboardKeyType_Float",
           "vector": "BlackboardKeyType_Vector", "object": "BlackboardKeyType_Object",
           "name": "BlackboardKeyType_Name", "string": "BlackboardKeyType_String",
           "enum": "BlackboardKeyType_Enum", "rotator": "BlackboardKeyType_Rotator",
           "class": "BlackboardKeyType_Class"}
def _resolve_keytype_path(spec):
    if spec is None:
        return None, None, "no key_type given"
    short = _KT_MAP.get(str(spec).strip().lower())
    if short is None:
        return None, None, "unknown key_type '%s'; valid: Bool, Int, Float, Vector, Object, Name, String, Enum, Rotator, Class" % spec
    cpath = "/Script/AIModule." + short
    try:
        cls = unreal.load_object(None, cpath)
    except Exception as e:
        return None, None, "could not load key-type class %s: %s" % (cpath, e)
    if not isinstance(cls, unreal.Class):
        return None, None, "resolved key-type is not a class: %s" % cpath
    return cls, short, None
'''

    # ------------------------------------------------------------------ #
    # create_blackboard — non-interactive BlackboardData asset creation    #
    # ------------------------------------------------------------------ #
    _CREATE_BB_BODY = _AIW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    bb = None
    # NOTE: asset creation must NOT be wrapped in a ScopedEditorTransaction — that puts the new
    # asset into the editor's transaction buffer, which then references it and blocks a later
    # delete (ForceDeleteObject -> "package potentially corrupt"). Creation is ledgered via our
    # own create_asset op instead; the AssetTools call is registry-level, not a transactable edit.
    bb = at.create_asset(name, package_path, unreal.BlackboardData, unreal.BlackboardDataFactory())
    if bb is None or not isinstance(bb, unreal.BlackboardData):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(bb).__name__, asset_path)}))
    else:
        _close_editors(bb)
        # Save the new asset to disk. Persists it (else it's lost on editor close) AND makes the
        # create_asset undo delete reliable — an unsaved just-created asset resists delete in the
        # immediately-following call (settle/GC timing); a saved on-disk package deletes cleanly.
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        keys = list(bb.get_editor_property("keys") or [])
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": bb.get_name(),
            "asset_path": asset_path, "object_path": bb.get_path_name(),
            "class": bb.get_class().get_name(),
            "default_key_count": len(keys),
            "default_keys": [str(e.get_editor_property("entry_name")) for e in keys],
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_blackboard(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new BlackboardData asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new Blackboard (e.g. 'BB_Enemy').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be
                      under a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate
                      folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.BlackboardData, unreal.BlackboardDataFactory()). The
        factory has no ConfigureProperties dialog and the scripting path never calls one -> no modal.
        A fresh BlackboardData ships with the built-in 'SelfActor' (Object) key (reported as
        default_keys). Add more with add_blackboard_key. Inspect with ai_read.get_blackboard_info.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close any
        editors for the asset, delete it, and remove package_path if we created it and it is now empty
        (the same generic inverse used by create_blueprint). The asset is created UNSAVED (in-memory)."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_BB_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_behavior_tree — non-interactive BehaviorTree asset creation   #
    # ------------------------------------------------------------------ #
    _CREATE_BT_BODY = _AIW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
bb_path = PARAMS.get("blackboard_path")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
bb = None
bb_err = None
if bb_path:
    bb, bb_err = _load_typed(bb_path, "BlackboardData")
if bb_err:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "blackboard_path invalid: %s" % bb_err}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    bt = None
    # Do NOT wrap asset creation in a ScopedEditorTransaction (see create_blackboard note) — it
    # would trap the new asset in the transaction buffer and block a later delete.
    bt = at.create_asset(name, package_path, unreal.BehaviorTree, unreal.BehaviorTreeFactory())
    if bt is not None and isinstance(bt, unreal.BehaviorTree) and bb is not None:
        bt.set_editor_property("blackboard_asset", bb)
    if bt is None or not isinstance(bt, unreal.BehaviorTree):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(bt).__name__, asset_path)}))
    else:
        _close_editors(bt)
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)  # persist + reliable undo-delete
        except Exception: pass
        linked = bt.get_editor_property("blackboard_asset")
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": bt.get_name(),
            "asset_path": asset_path, "object_path": bt.get_path_name(),
            "class": bt.get_class().get_name(),
            "blackboard_asset": (linked.get_path_name() if linked else None),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_behavior_tree(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                             blackboard_path: str = None) -> str:
        """Create a new BehaviorTree asset non-interactively (NO modal / factory dialog).

        name:            asset name for the new BehaviorTree (e.g. 'BT_Enemy').
        package_path:    content directory (default '/Game/MCP_Scratch'); must be under a valid
                         mounted root. Intermediate folders are created as needed.
        blackboard_path: optional BlackboardData asset path to link as the tree's blackboard_asset.

        Uses AssetTools.create_asset(..., unreal.BehaviorTree, unreal.BehaviorTreeFactory()) (no
        dialog -> no modal). The tree is created EMPTY (no root/composite nodes) -- BT node-graph
        authoring is editor-only in this build and is a separate deferred command. Inspect with
        ai_read.get_behavior_tree_info.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close
        editors + delete asset [+ rmdir if we created the dir]. The blackboard link (set in-create) is
        reverted for free by deleting the asset, so no separate ledger entry is needed for it."""
        params = {"name": name, "package_path": package_path, "blackboard_path": blackboard_path}
        try:
            return json.dumps(_exec(_CREATE_BT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_blackboard_key — add a typed key to a BlackboardData             #
    # ------------------------------------------------------------------ #
    _ADD_KEY_BODY = _AIW_HELPERS + r'''
bb_path = PARAMS["blackboard_path"]
key_name = PARAMS["key_name"]
key_type = PARAMS["key_type"]
bb, err = _load_typed(bb_path, "BlackboardData")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ktcls, short, kerr = _resolve_keytype_path(key_type)
    if kerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr}))
    else:
        existing = [str(e.get_editor_property("entry_name")) for e in (bb.get_editor_property("keys") or [])]
        if key_name in existing:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "a key named '%s' already exists on this blackboard" % key_name,
                "keys": existing}))
        else:
            added = False
            with unreal.ScopedEditorTransaction("MCP add_blackboard_key"):
                kt = unreal.new_object(ktcls, bb)
                entry = unreal.BlackboardEntry()
                entry.set_editor_property("entry_name", unreal.Name(key_name))
                entry.set_editor_property("key_type", kt)
                cur = list(bb.get_editor_property("keys") or [])
                cur.append(entry)
                bb.set_editor_property("keys", cur)
            after = list(bb.get_editor_property("keys") or [])
            rows = []
            for e in after:
                kt2 = e.get_editor_property("key_type")
                rows.append({"name": str(e.get_editor_property("entry_name")),
                             "type": (kt2.get_class().get_name()[len("BlackboardKeyType_"):]
                                      if kt2 and kt2.get_class().get_name().startswith("BlackboardKeyType_")
                                      else (kt2.get_class().get_name() if kt2 else None))})
            added = key_name in [r["name"] for r in rows]
            if added:
                _ledger().append({"op": "add_blackboard_key", "asset_path": bb_path,
                                  "object_path": bb.get_path_name(), "key_name": key_name})
            print("@@UMCP@@" + json.dumps({"status": "success" if added else "error",
                "blackboard": bb.get_name(), "added_key": key_name, "key_type": short,
                "added": added, "key_count": len(rows), "keys": rows,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_blackboard_key(ctx, blackboard_path: str, key_name: str, key_type: str) -> str:
        """Add a typed key to an existing BlackboardData asset.

        blackboard_path: object/package path of the BlackboardData asset.
        key_name:        name for the new key (must not collide with an existing key).
        key_type:        Bool | Int | Float | Vector | Object | Name | String | Enum | Rotator | Class
                         (case-insensitive).

        Instantiates the key-type subobject via unreal.new_object(load_object('/Script/AIModule.
        BlackboardKeyType_<Type>'), blackboard), builds an FBlackboardEntry (entry_name + key_type), and
        appends it to BlackboardData.keys. Verify with ai_read.get_blackboard_info. NOTE: Enum and Class
        keys are created without their sub-config (enum_type / base_class) -- the key exists and types
        correctly but you must set that sub-property separately for it to be fully usable.

        Ledgered write op 'add_blackboard_key' {asset_path, object_path, key_name}. Inverse: reload the
        blackboard and remove the key whose entry_name == key_name (names are unique -> FAITHFUL; only
        the key we added is removed, the built-in SelfActor and any other keys are untouched)."""
        params = {"blackboard_path": blackboard_path, "key_name": key_name, "key_type": key_type}
        try:
            return json.dumps(_exec(_ADD_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_behavior_tree_blackboard — set an existing BT's blackboard      #
    # ------------------------------------------------------------------ #
    _SET_BT_BB_BODY = _AIW_HELPERS + r'''
bt_path = PARAMS["bt_path"]
bb_path = PARAMS["blackboard_path"]
bt, err = _load_typed(bt_path, "BehaviorTree")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    bb, bberr = _load_typed(bb_path, "BlackboardData")
    if bberr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": bberr}))
    else:
        prior = bt.get_editor_property("blackboard_asset")
        prior_path = prior.get_path_name() if prior else None
        with unreal.ScopedEditorTransaction("MCP set_behavior_tree_blackboard"):
            bt.set_editor_property("blackboard_asset", bb)
        now = bt.get_editor_property("blackboard_asset")
        now_path = now.get_path_name() if now else None
        _ledger().append({"op": "set_behavior_tree_blackboard", "asset_path": bt_path,
                          "object_path": bt.get_path_name(), "prior_blackboard": prior_path})
        print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
            "prior_blackboard": prior_path, "new_blackboard": now_path,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_behavior_tree_blackboard(ctx, bt_path: str, blackboard_path: str) -> str:
        """Set (or change) the BlackboardData linked to an existing BehaviorTree.

        bt_path:         object/package path of the BehaviorTree asset.
        blackboard_path: object/package path of the BlackboardData asset to link.

        Sets BehaviorTree.blackboard_asset via reflection. Verify with ai_read.get_behavior_tree_info
        (blackboard_asset field). Ledgered write op 'set_behavior_tree_blackboard'
        {asset_path, object_path, prior_blackboard}. Inverse: restore blackboard_asset to
        prior_blackboard (load that asset, or set None if there was none) -- FAITHFUL."""
        params = {"bt_path": bt_path, "blackboard_path": blackboard_path}
        try:
            return json.dumps(_exec(_SET_BT_BB_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (editor-only in this build's Python surface):
    #   BehaviorTree node-graph authoring (root/composite/task/decorator/service nodes + wiring):
    #     the BT graph is READABLE via reflection (ai_read.get_behavior_tree_info) but NOT constructible
    #     from stock Python -- there is no BTCompositeNode/BTCompositeChild builder and root_node cannot
    #     be assembled + accepted by the asset editor from script. Needs a C++ handler over the Behavior
    #     Tree graph editor (FBehaviorTreeGraph / UBehaviorTreeGraphNode) or the with-user editor wave.
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. It reuses the generic
    # "create_asset" inverse and adds 2 NEW ledger ops (add_blackboard_key, set_behavior_tree_blackboard)
    # documented above for the coordinator to fold matching inverse branches into editor_level.undo.
    # ------------------------------------------------------------------ #
