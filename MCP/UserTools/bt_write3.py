"""UserTools :: BehaviorTree authoring EXTENSION 3 (WRITE)  (spec: docs/spec/behaviortree.md)

Clean-room UE 5.8 Python. Third companion to bt_write.py (root/composite/task/service/single-decorator)
and bt_write2.py (remove/set-prop/composite-decorator/blackboard). This module ships the last two
Python-REACHABLE BehaviorTree spec features that drive the behavior-trees category to 100%:

  - connect_bt_nodes         (op bt_reparent; inverse: move the slot back to old_parent/old_index)
  - create_bt_node_blueprint (op create_asset; generic create-asset inverse already in editor_level.undo)

Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM
from the gold-standard editor_level.py / bt_write2.py. Everything operates on the RUNTIME node tree via
reflection (root_node -> BTCompositeChild children), the SAME reflection surface proven authorable by
bt_write.py / bt_write2.py. After a structural reparent, call bt_write.sync_bt_editor_graph(bt_path) ONCE
to rebuild the editor graph from the runtime tree.

Node addressing = dot-separated CHILD-INDEX PATH from the root composite (identical to bt_write.py):
  "" / "root" = root node; "0" = root.children[0]; "0.2" = root.children[0].composite.children[2].
Only COMPOSITE nodes are valid parents (tasks are leaves).

connect_bt_nodes moves an existing child SLOT (its wrapped composite/task node + its decorators +
decorator_ops all travel with it, since FBTCompositeChild round-trips through get/set of the parent's
children array — the same technique bt_write2.add_bt_decorator uses). It captures the prior parent + index
and ledgers 'bt_reparent' so the exact move is reversible. Guards: cannot reparent the root; refuses a
move that would put a composite under itself or one of its own descendants (cycle); validates the new
parent is a composite and the destination index is in range.

create_bt_node_blueprint creates a Blueprint SUBCLASS of BTTask_BlueprintBase / BTService_BlueprintBase /
BTDecorator_BlueprintBase (per kind), or an explicit parent_class override, via
unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, path, unreal.Blueprint, factory) where
factory = unreal.BlueprintFactory() with parent_class PRE-SET -> NON-MODAL (BlueprintFactory only pops the
"pick parent class" dialog when parent_class is None). Saved on create. Ledgered generic 'create_asset'
{asset_path, package_path, created_dir} -> editor_level.undo already deletes it (no new fold).

Undo: this module does NOT register its own `undo` (editor_level.py owns the unified one). create_asset is
already folded; bt_reparent's inverse is reported to the coordinator for folding into editor_level.undo.
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
# success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers (prepended to bodies). No ''' / no backslash. #
    # ------------------------------------------------------------------ #
    _BT3_HELPERS = r'''
import unreal, json, builtins, inspect
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _node_class(spec):
    t = getattr(unreal, spec, None) if isinstance(spec, str) else None
    if t is not None:
        return t
    try:
        c = unreal.load_class(None, spec)
        if c is not None:
            return c
    except Exception:
        pass
    try:
        c = unreal.load_class(None, "/Script/AIModule." + str(spec))
        if c is not None:
            return c
    except Exception:
        pass
    return None
def _uclass(spec):
    # Resolve to a real UClass object (for BlueprintFactory.parent_class). A path-like spec (contains
    # "/" or ".") is tried as-is; a bare short name is only tried with AIModule / Engine prefixes so we
    # never call load_object on a bare name (which logs a benign "Failed to find object" warning).
    if not isinstance(spec, str) or not spec:
        return None
    cands = [spec] if ("/" in spec or "." in spec) else ["/Script/AIModule." + spec, "/Script/Engine." + spec]
    for path in cands:
        c = _try(lambda p=path: unreal.load_object(None, p))
        if isinstance(c, unreal.Class):
            return c
    c = _try(lambda: unreal.load_class(None, spec))
    if isinstance(c, unreal.Class):
        return c
    return None
def _load_bt(p):
    bt = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if bt is None or not isinstance(bt, unreal.BehaviorTree):
        return None
    return bt
def _norm_path(p):
    s = str(p if p is not None else "").strip()
    return "root" if s in ("", "root") else s
def _resolve_comp(bt, path):
    node = bt.get_editor_property("root_node") if bt is not None else None
    if node is None:
        return None
    ps = _norm_path(path)
    if ps == "root":
        return node
    for tok in ps.split("."):
        try:
            i = int(tok)
        except Exception:
            return None
        kids = list(node.get_editor_property("children") or [])
        if i < 0 or i >= len(kids):
            return None
        node = kids[i].get_editor_property("child_composite")
        if node is None:
            return None
    return node
def _parent_and_index(bt, node_path):
    ps = _norm_path(node_path)
    if ps == "root":
        return None, None
    toks = ps.split(".")
    parent = _resolve_comp(bt, ".".join(toks[:-1]) if len(toks) > 1 else "root")
    if parent is None:
        return None, None
    try:
        idx = int(toks[-1])
    except Exception:
        return None, None
    return parent, idx
def _same_node(a, b):
    if a is None or b is None:
        return False
    pa = _try(lambda: a.get_path_name())
    pb = _try(lambda: b.get_path_name())
    if pa is not None and pb is not None:
        return pa == pb
    return a == b
def _collect_composites(node, acc):
    # Collect this composite node + all descendant composites (proven-safe reflection walk). Tasks are
    # leaves and are not collected. Used only for the reparent cycle guard.
    if node is None or not isinstance(node, unreal.BTCompositeNode):
        return
    acc.append(node)
    for ch in (_try(lambda: node.get_editor_property("children"), []) or []):
        sub = _try(lambda: ch.get_editor_property("child_composite"))
        if sub is not None:
            _collect_composites(sub, acc)
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if aes and obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
'''

    # ================================================================== #
    # connect_bt_nodes  (reparent a child slot under a new composite)     #
    # ================================================================== #
    _CONNECT_BODY = _BT3_HELPERS + r'''
bt_path = PARAMS["bt_path"]; node_path = PARAMS["node_path"]
new_parent_path = _norm_path(PARAMS.get("new_parent_path") or "root")
new_index = PARAMS.get("new_index")
new_index = -1 if new_index is None else int(new_index)
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
elif _norm_path(node_path) == "root":
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "cannot reparent the root node; connect_bt_nodes moves a child slot, not the root"}))
else:
    old_parent, old_index = _parent_and_index(bt, node_path)
    if old_parent is None or old_index is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "invalid node_path '%s'" % node_path}))
    else:
        old_kids = list(old_parent.get_editor_property("children") or [])
        if old_index < 0 or old_index >= len(old_kids):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "child_index %d out of range at parent (count %d)" % (old_index, len(old_kids))}))
        else:
            new_parent = _resolve_comp(bt, new_parent_path)
            if new_parent is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "no composite at new_parent_path '%s' (root set? path valid? not a task leaf?)" % new_parent_path}))
            else:
                toks = _norm_path(node_path).split(".")
                old_parent_path = ".".join(toks[:-1]) if len(toks) > 1 else "root"
                moving_node = (_try(lambda: old_kids[old_index].get_editor_property("child_composite"))
                               or _try(lambda: old_kids[old_index].get_editor_property("child_task")))
                # cycle guard: reparenting a composite under itself / its own descendant is illegal
                subtree = []
                _collect_composites(moving_node, subtree)
                cyclic = any(_same_node(c, new_parent) for c in subtree)
                if cyclic:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "refused: new_parent_path '%s' is the moving node or one of its descendants (would create a cycle)" % new_parent_path}))
                else:
                    same = _same_node(old_parent, new_parent)
                    if same:
                        kids = list(new_parent.get_editor_property("children") or [])
                        slot = kids.pop(old_index)
                        insert_at = len(kids) if (new_index < 0 or new_index > len(kids)) else new_index
                        kids.insert(insert_at, slot)
                        with unreal.ScopedEditorTransaction("MCP connect_bt_nodes"):
                            new_parent.set_editor_property("children", kids)
                        final_index = insert_at
                        new_count = len(kids)
                    else:
                        okids = list(old_parent.get_editor_property("children") or [])
                        slot = okids.pop(old_index)
                        nkids = list(new_parent.get_editor_property("children") or [])
                        insert_at = len(nkids) if (new_index < 0 or new_index > len(nkids)) else new_index
                        nkids.insert(insert_at, slot)
                        with unreal.ScopedEditorTransaction("MCP connect_bt_nodes"):
                            old_parent.set_editor_property("children", okids)
                            new_parent.set_editor_property("children", nkids)
                        final_index = insert_at
                        new_count = len(nkids)
                    saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
                    new_child_path = (new_parent_path + "." + str(final_index)) if new_parent_path != "root" else str(final_index)
                    _ledger().append({"op": "bt_reparent", "asset_path": bt_path,
                                      "old_parent_path": old_parent_path, "old_index": old_index,
                                      "new_parent_path": new_parent_path, "new_index": final_index})
                    moved_class = _try(lambda: moving_node.get_class().get_name())
                    print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                        "moved_class": moved_class, "from_parent_path": old_parent_path, "from_index": old_index,
                        "new_parent_path": new_parent_path, "new_index": final_index,
                        "new_child_path": new_child_path, "new_parent_child_count": new_count,
                        "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_bt_nodes(ctx, bt_path: str, node_path: str, new_parent_path: str = "root",
                         new_index: int = -1) -> str:
        """Re-parent an existing BehaviorTree child node (composite or task) under a new composite parent.

        bt_path:         object/package path of the BehaviorTree asset.
        node_path:       dot-index path to the child SLOT to move ('0' = root.children[0], '0.2' deeper).
                         The root cannot be moved.
        new_parent_path: dot-index path to the destination composite ('root', '0', ...). Must be a
                         composite (tasks are leaves).
        new_index:       insertion index in the destination's children (-1 or out-of-range = append).

        The moved slot carries its wrapped node + decorators + decorator_ops. Refuses a move that would
        put a composite under itself or one of its descendants (cycle). Saved after the edit; call
        bt_write.sync_bt_editor_graph(bt_path) once after to rebuild the editor graph. Ledgered
        'bt_reparent' {asset_path, old_parent_path, old_index, new_parent_path, new_index}; inverse
        (editor_level.undo): move the slot back from new_parent/new_index to old_parent/old_index
        (FAITHFUL)."""
        params = {"bt_path": bt_path, "node_path": node_path,
                  "new_parent_path": new_parent_path, "new_index": new_index}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_bt_node_blueprint  (BP subclass of a BT *_BlueprintBase)     #
    # ================================================================== #
    _CREATE_BP_BODY = _BT3_HELPERS + r'''
name = PARAMS["name"]
kind = str(PARAMS.get("kind") or "task").strip().lower()
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
parent_class_spec = PARAMS.get("parent_class")
_KINDBASE = {"task": "BTTask_BlueprintBase", "service": "BTService_BlueprintBase",
             "decorator": "BTDecorator_BlueprintBase"}
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if kind not in _KINDBASE:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "bad kind '%s' (want task|service|decorator)" % kind}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    parent_cls = _uclass(parent_class_spec) if parent_class_spec else _uclass(_KINDBASE[kind])
    if parent_cls is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not resolve parent class '%s'" % (parent_class_spec or _KINDBASE[kind])}))
    else:
        created_dir = not EAL.does_directory_exist(package_path)
        # BlueprintFactory with parent_class PRE-SET is non-modal (no pick-parent dialog). Do NOT wrap
        # create_asset in a ScopedEditorTransaction (traps the new asset -> blocks a later delete).
        factory = unreal.BlueprintFactory()
        factory.set_editor_property("parent_class", parent_cls)
        bp = at.create_asset(name, package_path, unreal.Blueprint, factory)
        if bp is None or not isinstance(bp, unreal.Blueprint):
            if created_dir and EAL.does_directory_exist(package_path):
                try: EAL.delete_directory(package_path)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "create_asset returned %s for %s" % (type(bp).__name__, asset_path)}))
        else:
            _close_editors(bp)
            try: EAL.save_asset(asset_path, only_if_is_dirty=False)  # persist + reliable undo-delete
            except Exception: pass
            pc = _try(lambda: bp.get_editor_property("parent_class"))
            pc_name = _try(lambda: pc.get_name()) if pc is not None else _try(lambda: parent_cls.get_name())
            _ledger().append({"op": "create_asset", "asset_path": asset_path,
                              "package_path": package_path, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success", "name": bp.get_name(),
                "asset_path": asset_path, "object_path": bp.get_path_name(),
                "kind": kind, "parent_class": pc_name, "created_dir": created_dir,
                "saved": True, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_bt_node_blueprint(ctx, name: str, kind: str = "task",
                                 package_path: str = "/Game/MCP_Scratch",
                                 parent_class: str = None) -> str:
        """Create a Blueprint subclass of a BehaviorTree *_BlueprintBase node class (task/service/decorator).

        name:         asset name for the new Blueprint.
        kind:         task | service | decorator -> parent BTTask_BlueprintBase / BTService_BlueprintBase /
                      BTDecorator_BlueprintBase.
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid root
                      ('/Game', '/Engine', a plugin root) -- never '/Temp'.
        parent_class: optional explicit parent-class override (a class short name or path, e.g. a project
                      base task); when omitted, the kind's *_BlueprintBase is used.

        Uses AssetTools.create_asset(..., unreal.Blueprint, BlueprintFactory) with parent_class PRE-SET so
        NO modal pops. The resulting BP can be dropped into a BT via add_bt_task / add_bt_service /
        add_bt_decorator by its '/Game/.../<name>.<name>_C' class path. Saved on create. Ledgered generic
        'create_asset' {asset_path, package_path, created_dir}; inverse (editor_level.undo already folded):
        close editors + delete the asset [+ rmdir if we created the dir]."""
        params = {"name": name, "kind": kind, "package_path": package_path, "parent_class": parent_class}
        try:
            return json.dumps(_exec(_CREATE_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
