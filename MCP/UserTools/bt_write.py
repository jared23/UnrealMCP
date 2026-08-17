"""UserTools :: BehaviorTree node-graph authoring (WRITE)  (spec: docs/spec/ai.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Builds the RUNTIME BehaviorTree
node tree (root composite + child composites/tasks + services + child-slot decorators) directly, via
`unreal.new_object` + `FBTCompositeChild` wiring + `set_editor_property("root_node"/"children"/...)`.
Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM
from the gold-standard editor_level.py / niagara_write.py.

SUPERSEDES ai_write.py's old DEFERRED note ("no Python API to construct BT nodes") — PROVED wrong by a
live probe 2026-08-16: new_object(BTComposite_Selector/BTTask_*/BTService_*/BTDecorator_*, bt) + a
FBTCompositeChild with child_composite/child_task/decorators + set root_node all persist and read back
via ai_read.get_behavior_tree_info.

SCOPE (editor round-trip RESOLVED 2026-08-16 by C++ #11):
  * These build the RUNTIME node tree (RootNode + FBTCompositeChild children + services + child-slot
    decorators) directly. That alone is runtime-correct, reads back via reflection, and RUNS in PIE.
  * The BT EDITOR graph (UBehaviorTreeGraph) is not Python-reachable, so after authoring, call
    `sync_bt_editor_graph(bt_path)` ONCE (C++ #11 handler SyncBehaviorTreeEditorGraph) to rebuild the
    editor graph FROM the runtime tree -> the BT then opens/edits cleanly in the Behavior Tree editor
    (verified GREEN on Windows + coordinator: reuse, not wipe). Without that sync, opening the BT in the
    editor shows an empty graph and editing there would regenerate/wipe the runtime nodes.
  * Node addressing is a dot-separated CHILD-INDEX PATH from the root composite: "" or "root" = the
    root node; "0" = children[0]'s child_composite; "0.2" = root.children[0].composite.children[2].
    Only COMPOSITE nodes are addressable parents (tasks are leaves).

MODAL-SAFETY (learned live 2026-08-16 — see [[unrealmcp-command-buildout]] modal-poppers):
  * create_asset(BehaviorTree, BehaviorTreeFactory()) AUTO-OPENS the BT editor; the shipped
    ai_write.create_behavior_tree already _close_editors() after create. These node ops only load +
    mutate + save an EXISTING BT (no create, no delete) -> no modal.
  * A populated BT is finicky to hard-delete (delete-while-in-use modal / "potentially corrupt"). Undo
    here pops nodes (never deletes the asset); the asset delete only happens when create_behavior_tree
    is undone, and by then LIFO undo has emptied the tree. editor_level.undo also nulls root_node on a
    BehaviorTree before the create_asset sweep-delete as a defensive guard.

Implemented (each ONE mutate-op per call; ledgered; append-with-pop LIFO inverses):
  - set_bt_root            (op bt_set_root;    inverse: root_node -> None)
  - add_bt_child_composite (op bt_add_child;   inverse: pop parent.children[index])
  - add_bt_task            (op bt_add_child;   inverse: pop parent.children[index])
  - add_bt_service         (op bt_add_service; inverse: pop composite.services[index])
  - add_bt_decorator       (op bt_add_decorator; inverse: pop child-slot.decorators[index])

Undo: this module does NOT register its own `undo` (editor_level.py owns the unified one). The 5 ops
above are folded into editor_level.undo.
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

    # Shared Unreal-side helpers (prepended to bodies). No ''' / no backslash inside.
    _BT_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
COMPOSITES = {"selector": "BTComposite_Selector", "sequence": "BTComposite_Sequence",
              "simple_parallel": "BTComposite_SimpleParallel"}
def _node_class(spec):
    # spec: an unreal.* short name ("BTTask_Wait") OR a full class path ("/Script/AIModule.BTTask_Wait").
    t = getattr(unreal, spec, None)
    if t is not None:
        return t
    try:
        c = unreal.load_class(None, spec)
        if c is not None:
            return c
    except Exception:
        pass
    return None
def _resolve_comp(bt, path):
    # Walk composite children by dot-separated index path. "" / "root" -> root_node. Returns the
    # UBTCompositeNode at the path, or None if the path is invalid / points at a task leaf / no root.
    node = bt.get_editor_property("root_node")
    if node is None:
        return None
    ps = str(path if path is not None else "").strip()
    if ps == "" or ps == "root":
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
def _load_bt(p):
    bt = unreal.EditorAssetLibrary.load_asset(p)
    if bt is None or not isinstance(bt, unreal.BehaviorTree):
        return None
    return bt
'''

    # ------------------------------------------------------------------ #
    # set_bt_root                                                          #
    # ------------------------------------------------------------------ #
    _SET_ROOT_BODY = _BT_HELPERS + r'''
bt_path = PARAMS["bt_path"]; comp_type = str(PARAMS["composite_type"]).lower()
node_name = PARAMS.get("node_name") or ""
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
elif bt.get_editor_property("root_node") is not None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "root already set; undo/remove it first (set_bt_root only sets an empty root)"}))
elif comp_type not in COMPOSITES:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "bad composite_type '%s' (want selector|sequence|simple_parallel)" % comp_type}))
else:
    comp = unreal.new_object(getattr(unreal, COMPOSITES[comp_type]), bt)
    if node_name:
        comp.set_editor_property("node_name", node_name)
    bt.set_editor_property("root_node", comp)
    saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
    _ledger().append({"op": "bt_set_root", "asset_path": bt_path})
    print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
        "root_class": comp.get_class().get_name(), "root_path": "root", "saved": bool(saved),
        "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_bt_child_composite / add_bt_task (both append a FBTCompositeChild) #
    # ------------------------------------------------------------------ #
    _ADD_CHILD_BODY = _BT_HELPERS + r'''
bt_path = PARAMS["bt_path"]; parent_path = PARAMS.get("parent_path") or "root"
kind = PARAMS["kind"]; node_class = PARAMS["node_class"]; node_name = PARAMS.get("node_name") or ""
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
else:
    parent = _resolve_comp(bt, parent_path)
    if parent is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no composite at parent_path '%s' (root set? path valid? not a task leaf?)" % parent_path}))
    else:
        cls = _node_class(node_class)
        if cls is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown node class '%s'" % node_class}))
        else:
            obj = unreal.new_object(cls, bt)
            if node_name:
                obj.set_editor_property("node_name", node_name)
            ch = unreal.BTCompositeChild()
            if kind == "composite":
                ch.set_editor_property("child_composite", obj)
            else:
                ch.set_editor_property("child_task", obj)
            kids = list(parent.get_editor_property("children") or [])
            kids.append(ch)
            parent.set_editor_property("children", kids)
            idx = len(kids) - 1
            saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
            child_path = (str(parent_path) + "." + str(idx)) if str(parent_path) not in ("", "root") else str(idx)
            _ledger().append({"op": "bt_add_child", "asset_path": bt_path,
                              "parent_path": parent_path, "index": idx})
            print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                "kind": kind, "node_class": obj.get_class().get_name(), "index": idx,
                "child_path": child_path, "child_count": len(kids), "saved": bool(saved),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_bt_service (on a composite node)                                 #
    # ------------------------------------------------------------------ #
    _ADD_SERVICE_BODY = _BT_HELPERS + r'''
bt_path = PARAMS["bt_path"]; comp_path = PARAMS.get("composite_path") or "root"
svc_class = PARAMS["service_class"]
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
else:
    comp = _resolve_comp(bt, comp_path)
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no composite at path '%s'" % comp_path}))
    else:
        cls = _node_class(svc_class)
        if cls is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown service class '%s'" % svc_class}))
        else:
            svc = unreal.new_object(cls, bt)
            svcs = list(comp.get_editor_property("services") or [])
            svcs.append(svc)
            comp.set_editor_property("services", svcs)
            idx = len(svcs) - 1
            saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
            _ledger().append({"op": "bt_add_service", "asset_path": bt_path,
                              "composite_path": comp_path, "index": idx})
            print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                "composite_path": comp_path, "service_class": svc.get_class().get_name(),
                "index": idx, "service_count": len(svcs), "saved": bool(saved),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_bt_decorator (on a child slot of a composite)                    #
    # ------------------------------------------------------------------ #
    _ADD_DECORATOR_BODY = _BT_HELPERS + r'''
bt_path = PARAMS["bt_path"]; parent_path = PARAMS.get("parent_path") or "root"
child_index = int(PARAMS["child_index"]); dec_class = PARAMS["decorator_class"]
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
else:
    parent = _resolve_comp(bt, parent_path)
    if parent is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no composite at parent_path '%s'" % parent_path}))
    else:
        kids = list(parent.get_editor_property("children") or [])
        if child_index < 0 or child_index >= len(kids):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "child_index %d out of range (count %d)" % (child_index, len(kids))}))
        else:
            cls = _node_class(dec_class)
            if cls is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown decorator class '%s'" % dec_class}))
            else:
                dec = unreal.new_object(cls, bt)
                ch = kids[child_index]
                decos = list(ch.get_editor_property("decorators") or [])
                decos.append(dec)
                ch.set_editor_property("decorators", decos)
                kids[child_index] = ch
                parent.set_editor_property("children", kids)
                idx = len(decos) - 1
                saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
                _ledger().append({"op": "bt_add_decorator", "asset_path": bt_path,
                                  "parent_path": parent_path, "child_index": child_index, "index": idx})
                print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                    "parent_path": parent_path, "child_index": child_index,
                    "decorator_class": dec.get_class().get_name(), "index": idx,
                    "decorator_count": len(decos), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ----------------------------- tools ------------------------------- #
    @mcp.tool()
    def set_bt_root(ctx, bt_path: str, composite_type: str = "selector", node_name: str = "") -> str:
        """Set the ROOT composite of a BehaviorTree (only when it has no root yet).

        bt_path:        object/package path of the BehaviorTree asset.
        composite_type: selector | sequence | simple_parallel.
        node_name:      optional display name for the node.

        Builds the RUNTIME node (see module caveat: not editor-round-trippable). Saved after the edit.
        Ledgered 'bt_set_root' {asset_path}; inverse (editor_level.undo): root_node -> None (FAITHFUL,
        since set only succeeds on an empty root)."""
        params = {"bt_path": bt_path, "composite_type": composite_type, "node_name": node_name}
        try:
            return json.dumps(_exec(_SET_ROOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_bt_child_composite(ctx, bt_path: str, parent_path: str = "root",
                               composite_type: str = "selector", node_name: str = "") -> str:
        """Append a child COMPOSITE (selector/sequence/simple_parallel) under a parent composite.

        parent_path: dot-separated child-index path to the parent composite ("root" or "" = root; "0"
                     = root.children[0]'s composite; "0.2" = deeper). Only composites are valid parents.

        Returns the new child's index + child_path (use it as parent_path for deeper nodes). Saved.
        Ledgered 'bt_add_child' {asset_path,parent_path,index}; inverse: pop parent.children[index]
        (FAITHFUL under LIFO undo)."""
        params = {"bt_path": bt_path, "parent_path": parent_path, "kind": "composite",
                  "node_class": {"selector": "BTComposite_Selector", "sequence": "BTComposite_Sequence",
                                 "simple_parallel": "BTComposite_SimpleParallel"}.get(str(composite_type).lower(), ""),
                  "node_name": node_name}
        if not params["node_class"]:
            return json.dumps({"status": "error",
                               "message": "bad composite_type (want selector|sequence|simple_parallel)"}, indent=2)
        try:
            return json.dumps(_exec(_ADD_CHILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_bt_task(ctx, bt_path: str, parent_path: str = "root",
                    task_class: str = "BTTask_Wait", node_name: str = "") -> str:
        """Append a leaf TASK under a parent composite.

        task_class:  an AIModule task short name ('BTTask_Wait', 'BTTask_MoveTo', 'BTTask_RunBehavior')
                     or a full class path ('/Script/AIModule.BTTask_Wait', or a BP task path).
        parent_path: dot-separated child-index path to the parent composite.

        Saved. Ledgered 'bt_add_child' {asset_path,parent_path,index}; inverse: pop the child (FAITHFUL
        under LIFO undo)."""
        params = {"bt_path": bt_path, "parent_path": parent_path, "kind": "task",
                  "node_class": task_class, "node_name": node_name}
        try:
            return json.dumps(_exec(_ADD_CHILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_bt_service(ctx, bt_path: str, composite_path: str = "root",
                       service_class: str = "BTService_DefaultFocus") -> str:
        """Add a SERVICE to a composite node (services tick while their subtree is active).

        service_class:  an AIModule service short name ('BTService_DefaultFocus', 'BTService_RunEQS')
                        or a full class path.
        composite_path: dot-separated child-index path to the composite.

        Saved. Ledgered 'bt_add_service' {asset_path,composite_path,index}; inverse: pop the service."""
        params = {"bt_path": bt_path, "composite_path": composite_path, "service_class": service_class}
        try:
            return json.dumps(_exec(_ADD_SERVICE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_bt_decorator(ctx, bt_path: str, parent_path: str = "root", child_index: int = 0,
                         decorator_class: str = "BTDecorator_Blackboard") -> str:
        """Add a DECORATOR that guards entry to a specific child slot of a composite.

        parent_path: dot-separated child-index path to the composite that OWNS the child slot.
        child_index: which child slot (0-based) of that composite to guard.
        decorator_class: an AIModule decorator short name ('BTDecorator_Blackboard',
                        'BTDecorator_ForceSuccess', 'BTDecorator_Cooldown') or a full class path.

        Saved. Ledgered 'bt_add_decorator' {asset_path,parent_path,child_index,index}; inverse: pop the
        decorator from that child slot."""
        params = {"bt_path": bt_path, "parent_path": parent_path, "child_index": child_index,
                  "decorator_class": decorator_class}
        try:
            return json.dumps(_exec(_ADD_DECORATOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- editor-graph round-trip (C++ #11) ----------------------------- #
    # Rebuilds UBehaviorTreeGraph from the runtime RootNode so an MCP-authored BT opens/edits cleanly in
    # the Behavior Tree editor. hasattr-guarded -> clear error on a plugin build predating C++ #11.
    _SYNC_GRAPH_BODY = _BT_HELPERS + r'''
bt_path = PARAMS["bt_path"]
bt = _load_bt(bt_path)
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
elif mrl is None or not hasattr(mrl, "sync_behavior_tree_editor_graph"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ #11 handler sync_behavior_tree_editor_graph unavailable (plugin DLL predates C++ #11; recompile needed)"}))
else:
    res = json.loads(mrl.sync_behavior_tree_editor_graph(bt))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
        # Idempotent DISPLAY/round-trip sync of an already-persisted runtime tree -> not ledgered.
        print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
            "sync": res, "saved": bool(saved)}))
'''

    @mcp.tool()
    def sync_bt_editor_graph(ctx, bt_path: str) -> str:
        """Rebuild the BT EDITOR graph from the runtime RootNode so the asset is editor-round-trippable
        (C++ #11). Call ONCE after building/editing a BT with set_bt_root / add_bt_child_composite /
        add_bt_task / add_bt_service / add_bt_decorator. Without it, opening the BT in the Behavior Tree
        editor shows an empty graph and editing there wipes the runtime nodes. Idempotent; saved after the
        sync. REQUIRES the C++ #11 plugin handler (hasattr-guarded; clear error if absent)."""
        try:
            return json.dumps(_exec(_SYNC_GRAPH_BODY, {"bt_path": bt_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
