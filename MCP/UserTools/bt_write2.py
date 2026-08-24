"""UserTools :: BehaviorTree authoring EXTENSION (WRITE + read helpers)  (spec: docs/spec/behaviortree.md)

Clean-room UE 5.8 Python. Companion to bt_write.py (root/composite/task/service/single-decorator) and
ai_write.py (create_behavior_tree/create_blackboard/add_blackboard_key/set_behavior_tree_blackboard).
This module adds the remaining Python-REACHABLE BehaviorTree + Blackboard spec features that bt_write.py
/ ai_write.py do not cover. Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session
ledger, value coercion) copied VERBATIM from the gold-standard editor_level.py / bt_write.py.

Everything here operates on the RUNTIME node tree via reflection (root_node -> BTCompositeChild children
/ services / decorators / decorator_ops), the same reflection surface proven authorable by bt_write.py.
After a structural edit, call bt_write.sync_bt_editor_graph(bt_path) ONCE to rebuild the editor graph.

Node addressing = dot-separated CHILD-INDEX PATH from the root composite (identical to bt_write.py):
  "" / "root" = root node; "0" = root.children[0]; "0.2" = root.children[0].composite.children[2].
A composite PATH addresses a composite node (walk descends child_composite each step). A NODE path
addresses a child slot's node (composite OR task): its parent = path minus last token, index = last token.

Implemented (mutating ops are ledgered with FAITHFUL inverses folded into editor_level.undo):
  WRITE (ledgered):
  - remove_bt_node          (op bt_remove_node;    inverse: rebuild snapshotted child slot at index)
  - set_bt_node_property    (op bt_set_node_prop;  inverse: restore prior value; refuses unreadable props)
  - add_bt_composite_decorator (op bt_add_comp_decorator; inverse: truncate decorators + restore ops)
  - remove_blackboard_key   (op bb_remove_key;     inverse: re-insert snapshotted key at index)
  - set_blackboard_key      (op bb_set_key;        inverse: restore prior entry_name + key_type)
  READ (no editor opened, no ledger):
  - list_blackboard_key_types  (enumerate BlackboardKeyType_* classes)
  - validate_behavior_tree     (structural validator)
  - search_bt_nodes            (ranked keyword search over BT Composite/Task/Service/Decorator classes)
  - get_bt_node_type_info      (kind + editable properties/defaults for a BT node class)

NOT duplicated (already shipped): add_blackboard_key (ai_write.py), set_bt_root / add_bt_child_composite
/ add_bt_task / add_bt_service / add_bt_decorator / sync_bt_editor_graph (bt_write.py).

DEFERRED (need PIE / a running game world -> forbidden by PROTOCOL; not faked):
  - get_bt_runtime, control_bt_runtime, set_bt_dynamic_subtree  (all runtime-only; see stubs at bottom).

DELETE/UNDO safety (PROTOCOL): remove_bt_node snapshots class + node_name + structure + child-slot
decorator logic ops (NOT arbitrary per-node UPROPERTY values -- those are re-applied faithfully for the
node_name field only, matching what the authoring tools set; a value set via set_bt_node_property before a
remove is NOT restored -- documented). All snapshots read ONLY class-name / node_name / decorator_ops
export_text (proven-safe reflection; no walk of uninitialized nested structs, per CRASH MODE #3).
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
    _BT2_HELPERS = r'''
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
def _load_bt(p):
    bt = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if bt is None or not isinstance(bt, unreal.BehaviorTree):
        return None
    return bt
def _resolve_comp(bt, path):
    node = bt.get_editor_property("root_node") if bt is not None else None
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
def _parent_and_index(bt, node_path):
    # Resolve a NODE path -> (parent_composite, child_index). node_path must not be root.
    ps = str(node_path if node_path is not None else "").strip()
    if ps == "" or ps == "root":
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
def _node_at(bt, node_path):
    # Resolve a NODE path -> the node object (composite or task). "" / "root" -> root_node.
    ps = str(node_path if node_path is not None else "").strip()
    if ps == "" or ps == "root":
        return bt.get_editor_property("root_node")
    parent, idx = _parent_and_index(bt, node_path)
    if parent is None or idx is None:
        return None
    kids = list(parent.get_editor_property("children") or [])
    if idx < 0 or idx >= len(kids):
        return None
    ch = kids[idx]
    c = ch.get_editor_property("child_composite")
    if c is not None:
        return c
    return ch.get_editor_property("child_task")
# --- structural snapshot (read-only, proven-safe reflection) ---
def _snap_node(n):
    cls = _try(lambda: n.get_class().get_name())
    nm = str(_try(lambda: n.get_editor_property("node_name"), ""))
    if isinstance(n, unreal.BTCompositeNode):
        svcs = []
        for s in (_try(lambda: n.get_editor_property("services"), []) or []):
            svcs.append({"class": _try(lambda: s.get_class().get_name()),
                         "name": str(_try(lambda: s.get_editor_property("node_name"), ""))})
        kids = [_snap_child(ch) for ch in (_try(lambda: n.get_editor_property("children"), []) or [])]
        return {"kind": "composite", "class": cls, "name": nm, "services": svcs, "children": kids}
    return {"kind": "task", "class": cls, "name": nm}
def _snap_child(ch):
    decos = []
    for d in (_try(lambda: ch.get_editor_property("decorators"), []) or []):
        decos.append({"class": _try(lambda: d.get_class().get_name()),
                      "name": str(_try(lambda: d.get_editor_property("node_name"), ""))})
    ops = [str(_try(lambda: o.export_text(), "")) for o in (_try(lambda: ch.get_editor_property("decorator_ops"), []) or [])]
    comp = _try(lambda: ch.get_editor_property("child_composite"))
    tsk = _try(lambda: ch.get_editor_property("child_task"))
    node = _snap_node(comp) if comp is not None else (_snap_node(tsk) if tsk is not None else None)
    return {"decorators": decos, "decorator_ops": ops, "child": node}
# --- structural rebuild (from a snapshot; mirror of the undo-branch rebuild) ---
def _make_node(bt, ns):
    cls = _node_class(ns.get("class"))
    if cls is None:
        return None
    obj = unreal.new_object(cls, bt)
    if ns.get("name"):
        obj.set_editor_property("node_name", ns["name"])
    if ns.get("kind") == "composite":
        svcs = []
        for s in ns.get("services", []):
            sc = _node_class(s.get("class"))
            if sc is None:
                continue
            so = unreal.new_object(sc, bt)
            if s.get("name"):
                so.set_editor_property("node_name", s["name"])
            svcs.append(so)
        obj.set_editor_property("services", svcs)
        obj.set_editor_property("children", [_make_child(bt, cs) for cs in ns.get("children", [])])
    return obj
def _make_child(bt, cs):
    ch = unreal.BTCompositeChild()
    decos = []
    for d in cs.get("decorators", []):
        dc = _node_class(d.get("class"))
        if dc is None:
            continue
        do = unreal.new_object(dc, bt)
        if d.get("name"):
            do.set_editor_property("node_name", d["name"])
        decos.append(do)
    ch.set_editor_property("decorators", decos)
    ops = []
    for ex in cs.get("decorator_ops", []):
        lo = unreal.BTDecoratorLogic()
        lo.import_text(ex)
        ops.append(lo)
    if ops:
        ch.set_editor_property("decorator_ops", ops)
    node = cs.get("child")
    if node is not None:
        n = _make_node(bt, node)
        if node.get("kind") == "composite":
            ch.set_editor_property("child_composite", n)
        else:
            ch.set_editor_property("child_task", n)
    return ch
# --- value (de)serialization for set_bt_node_property (subset of editor_level _COERCE_HELPERS) ---
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
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(value))
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
                return _try(lambda: int(float(value.strip())), value)
        if isinstance(value, (int, float)):
            return int(value)
        return value
    if isinstance(current, float):
        if isinstance(value, str):
            return _try(lambda: float(value.strip()), value)
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return value
_KT_MAP = {"bool": "BlackboardKeyType_Bool", "int": "BlackboardKeyType_Int",
           "integer": "BlackboardKeyType_Int", "float": "BlackboardKeyType_Float",
           "vector": "BlackboardKeyType_Vector", "object": "BlackboardKeyType_Object",
           "name": "BlackboardKeyType_Name", "string": "BlackboardKeyType_String",
           "enum": "BlackboardKeyType_Enum", "rotator": "BlackboardKeyType_Rotator",
           "class": "BlackboardKeyType_Class"}
def _resolve_keytype(spec):
    if spec is None:
        return None, None, "no key_type given"
    short = _KT_MAP.get(str(spec).strip().lower())
    if short is None:
        return None, None, "unknown key_type '%s'; valid: Bool, Int, Float, Vector, Object, Name, String, Enum, Rotator, Class" % spec
    cpath = "/Script/AIModule." + short
    cls = _try(lambda: unreal.load_object(None, cpath))
    if not isinstance(cls, unreal.Class):
        return None, None, "could not load key-type class %s" % cpath
    return cls, short, None
def _kt_short(kt):
    if kt is None:
        return None
    cn = _try(lambda: kt.get_class().get_name())
    if cn and cn.startswith("BlackboardKeyType_"):
        return cn[len("BlackboardKeyType_"):]
    return cn
def _load_bb(p):
    bb = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if bb is None or not isinstance(bb, unreal.BlackboardData):
        return None
    return bb
'''

    # ================================================================== #
    # remove_bt_node                                                      #
    # ================================================================== #
    _REMOVE_NODE_BODY = _BT2_HELPERS + r'''
bt_path = PARAMS["bt_path"]; node_path = PARAMS["node_path"]
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
elif str(node_path).strip() in ("", "root"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "cannot remove the root via remove_bt_node; undo set_bt_root (root_node -> None) instead"}))
else:
    parent, idx = _parent_and_index(bt, node_path)
    if parent is None or idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "invalid node_path '%s'" % node_path}))
    else:
        kids = list(parent.get_editor_property("children") or [])
        if idx < 0 or idx >= len(kids):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "child_index %d out of range (count %d)" % (idx, len(kids))}))
        else:
            snap = _snap_child(kids[idx])
            with unreal.ScopedEditorTransaction("MCP remove_bt_node"):
                del kids[idx]
                parent.set_editor_property("children", kids)
            saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
            toks = str(node_path).split(".")
            parent_path = ".".join(toks[:-1]) if len(toks) > 1 else "root"
            _ledger().append({"op": "bt_remove_node", "asset_path": bt_path,
                              "parent_path": parent_path, "index": idx, "snapshot": snap})
            print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                "removed_node_path": node_path, "removed": snap.get("child"),
                "child_count": len(kids), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_bt_node(ctx, bt_path: str, node_path: str) -> str:
        """Remove a node (composite or task) + its whole subtree from a BehaviorTree by dot-index path.

        node_path: dot-separated child-index path to the CHILD SLOT to remove ('0' = root.children[0],
                   '0.2' = deeper). The root cannot be removed here (undo set_bt_root instead).

        Snapshots the removed child slot (its child node's class + node_name + nested services/children,
        plus the slot's decorators and decorator_ops logic) so undo can faithfully rebuild it at the same
        index. Saved after the edit; call sync_bt_editor_graph after. Ledgered 'bt_remove_node'
        {asset_path, parent_path, index, snapshot}; inverse (editor_level.undo): rebuild the snapshotted
        child slot and re-insert at index.

        FAITHFULNESS: class + node_name + tree structure + decorator logic are restored; arbitrary
        per-node UPROPERTY values previously set via set_bt_node_property are NOT captured in the snapshot
        (only node_name is), so re-apply those after an undo if you had set them."""
        try:
            return json.dumps(_exec(_REMOVE_NODE_BODY, {"bt_path": bt_path, "node_path": node_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_bt_node_property                                                #
    # ================================================================== #
    _SET_NODE_PROP_BODY = _BT2_HELPERS + r'''
bt_path = PARAMS["bt_path"]; node_path = PARAMS["node_path"]
prop = PARAMS["property"]; value = PARAMS["value"]
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
else:
    node = _node_at(bt, node_path)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no node at node_path '%s'" % node_path}))
    else:
        try:
            current = node.get_editor_property(prop)
            readable = True
        except Exception as e:
            current = None; readable = False; read_err = str(e)
        if not readable:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "property '%s' is not readable on %s (%s); refusing an unreversible set" % (prop, node.get_class().get_name(), read_err)}))
        else:
            prior_ser, ok = _settable(current)
            if not ok:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "property '%s' current value is a %s that cannot be snapshotted for a faithful undo; refused" % (prop, type(current).__name__)}))
            else:
                coerced = _coerce(current, value)
                try:
                    with unreal.ScopedEditorTransaction("MCP set_bt_node_property"):
                        node.set_editor_property(prop, coerced)
                    setok = True; set_err = None
                except Exception as e:
                    setok = False; set_err = str(e)
                if not setok:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "set_editor_property('%s') failed: %s" % (prop, set_err)}))
                else:
                    now = _try(lambda: node.get_editor_property(prop))
                    now_ser, _ = _settable(now)
                    saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
                    _ledger().append({"op": "bt_set_node_prop", "asset_path": bt_path,
                                      "node_path": node_path, "property": prop, "prior": prior_ser})
                    print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                        "node_path": node_path, "node_class": node.get_class().get_name(),
                        "property": prop, "prior_value": prior_ser, "new_value": now_ser,
                        "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_bt_node_property(ctx, bt_path: str, node_path: str, property: str, value) -> str:
        """Set a UPROPERTY on a BT node (composite/task/root) addressed by dot-index path.

        node_path: dot-index path to the node ('root', '0', '0.2'); property: the node's editor-property
                   name (snake_case, e.g. 'wait_time', 'node_name', 'flow_abort_mode'); value: the new
                   value (number/bool/string; a list [x,y,z] for Vector/Rotator; an asset path for an
                   object property; an enum member name string for enums).

        Reads the CURRENT value first for a faithful undo; if the property is unreadable OR its value is a
        struct that cannot be snapshotted, the set is REFUSED (never ships an unreversible mutation). Use
        get_bt_node_type_info to discover editable property names. Saved. Ledgered 'bt_set_node_prop'
        {asset_path, node_path, property, prior}; inverse: restore prior value."""
        params = {"bt_path": bt_path, "node_path": node_path, "property": property, "value": value}
        try:
            return json.dumps(_exec(_SET_NODE_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_bt_composite_decorator (AND/OR/NOT logic over decorators)       #
    # ================================================================== #
    _ADD_COMP_DEC_BODY = _BT2_HELPERS + r'''
bt_path = PARAMS["bt_path"]; parent_path = PARAMS.get("parent_path") or "root"
child_index = int(PARAMS["child_index"]); operator = str(PARAMS["operator"]).strip().lower()
dec_classes = PARAMS["decorator_classes"]
_OPMAP = {"and": "And", "or": "Or", "not": "Not"}
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
elif operator not in _OPMAP:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "operator must be AND, OR, or NOT (got '%s')" % operator}))
elif not isinstance(dec_classes, list) or len(dec_classes) == 0:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "decorator_classes must be a non-empty list"}))
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
            ch = kids[child_index]
            prior_decos = list(ch.get_editor_property("decorators") or [])
            prior_ops = [str(_try(lambda: o.export_text(), "")) for o in (ch.get_editor_property("decorator_ops") or [])]
            base = len(prior_decos)
            bad = None
            newobjs = []
            for spec in dec_classes:
                cls = _node_class(spec)
                if cls is None:
                    bad = spec; break
                if not (inspect.isclass(cls) and issubclass(cls, unreal.BTDecorator)):
                    bad = spec; break
                newobjs.append(unreal.new_object(cls, bt))
            total = base + len(newobjs)
            if bad is not None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown or non-decorator class '%s'" % bad}))
            elif operator == "not" and total != 1:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "NOT is unary: requires exactly one decorator on the slot (found base=%d + new=%d)" % (base, len(newobjs))}))
            else:
                with unreal.ScopedEditorTransaction("MCP add_bt_composite_decorator"):
                    decos = prior_decos + newobjs
                    ch.set_editor_property("decorators", decos)
                    ops = []
                    def _mkop(text):
                        lo = unreal.BTDecoratorLogic(); lo.import_text(text); return lo
                    if operator == "not":
                        ops.append(_mkop("(Operation=Not,Number=1)"))
                        ops.append(_mkop("(Operation=Test,Number=0)"))
                    else:
                        ops.append(_mkop("(Operation=%s,Number=%d)" % (_OPMAP[operator], total)))
                        for i in range(total):
                            ops.append(_mkop("(Operation=Test,Number=%d)" % i))
                    ch.set_editor_property("decorator_ops", ops)
                    kids[child_index] = ch
                    parent.set_editor_property("children", kids)
                saved = unreal.EditorAssetLibrary.save_asset(bt_path, only_if_is_dirty=False)
                _ledger().append({"op": "bt_add_comp_decorator", "asset_path": bt_path,
                                  "parent_path": parent_path, "child_index": child_index,
                                  "prior_decorator_count": base, "prior_ops": prior_ops})
                after_ops = [str(_try(lambda: o.export_text(), "")) for o in (ch.get_editor_property("decorator_ops") or [])]
                print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
                    "parent_path": parent_path, "child_index": child_index, "operator": _OPMAP[operator],
                    "added_decorators": [o.get_class().get_name() for o in newobjs],
                    "decorator_count": total, "decorator_ops": after_ops,
                    "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_bt_composite_decorator(ctx, bt_path: str, parent_path: str = "root", child_index: int = 0,
                                   operator: str = "AND", decorator_classes: list = None) -> str:
        """Add decorators to a child slot and combine them with AND/OR/NOT logic (FBTDecoratorLogic ops).

        parent_path: dot-index path to the composite owning the child slot; child_index: which child
                     slot (0-based); operator: AND | OR | NOT; decorator_classes: list of BTDecorator_*
                     short names or class paths to add (NOT requires the slot end up with exactly one
                     decorator).

        Appends the new decorators to the slot and REBUILDS the slot's decorator_ops as <operator> over
        ALL decorators now on the slot (prefix form: [(Op,count)] + [(Test,i)...]; NOT -> [(Not,1),(Test,
        0)]). AND with an empty ops list is the engine default; this writes explicit ops for OR/NOT/AND
        alike. Saved; call sync_bt_editor_graph after. Ledgered 'bt_add_comp_decorator' {asset_path,
        parent_path, child_index, prior_decorator_count, prior_ops}; inverse: truncate decorators back to
        prior_decorator_count and restore decorator_ops from prior_ops (FAITHFUL)."""
        params = {"bt_path": bt_path, "parent_path": parent_path, "child_index": child_index,
                  "operator": operator, "decorator_classes": decorator_classes or []}
        try:
            return json.dumps(_exec(_ADD_COMP_DEC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # remove_blackboard_key                                               #
    # ================================================================== #
    _REMOVE_KEY_BODY = _BT2_HELPERS + r'''
bb_path = PARAMS["blackboard_path"]; key_name = PARAMS["key_name"]
bb = _load_bb(bb_path)
if bb is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BlackboardData: %s" % bb_path}))
else:
    keys = list(bb.get_editor_property("keys") or [])
    found = -1
    for i, e in enumerate(keys):
        if str(e.get_editor_property("entry_name")) == str(key_name):
            found = i; break
    if found < 0:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no key named '%s'" % key_name,
            "keys": [str(e.get_editor_property("entry_name")) for e in keys]}))
    else:
        e = keys[found]
        snap = {"name": str(e.get_editor_property("entry_name")),
                "type": _kt_short(e.get_editor_property("key_type")),
                "index": found,
                "category": str(_try(lambda: e.get_editor_property("entry_category"), "")),
                "description": str(_try(lambda: e.get_editor_property("entry_description"), ""))}
        with unreal.ScopedEditorTransaction("MCP remove_blackboard_key"):
            del keys[found]
            bb.set_editor_property("keys", keys)
        saved = unreal.EditorAssetLibrary.save_asset(bb_path, only_if_is_dirty=False)
        _ledger().append({"op": "bb_remove_key", "asset_path": bb_path, "snapshot": snap})
        print("@@UMCP@@" + json.dumps({"status": "success", "blackboard": bb.get_name(),
            "removed_key": snap["name"], "removed_type": snap["type"], "index": found,
            "key_count": len(keys), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_blackboard_key(ctx, blackboard_path: str, key_name: str) -> str:
        """Remove a key from a BlackboardData asset by name.

        Snapshots the removed entry (name, type, index, category, description) so undo re-inserts it at
        the same index with the same type. Verify with ai_read.get_blackboard_info. Saved. Ledgered
        'bb_remove_key' {asset_path, snapshot}; inverse (editor_level.undo): rebuild the key-type
        subobject + FBlackboardEntry and re-insert at snapshot.index (FAITHFUL)."""
        params = {"blackboard_path": blackboard_path, "key_name": key_name}
        try:
            return json.dumps(_exec(_REMOVE_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_blackboard_key (rename and/or retype an existing key)           #
    # ================================================================== #
    _SET_KEY_BODY = _BT2_HELPERS + r'''
bb_path = PARAMS["blackboard_path"]; key_name = PARAMS["key_name"]
new_name = PARAMS.get("new_name"); new_type = PARAMS.get("new_type")
bb = _load_bb(bb_path)
if bb is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BlackboardData: %s" % bb_path}))
elif not new_name and not new_type:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "nothing to change: pass new_name and/or new_type"}))
else:
    keys = list(bb.get_editor_property("keys") or [])
    names = [str(e.get_editor_property("entry_name")) for e in keys]
    found = -1
    for i, nm in enumerate(names):
        if nm == str(key_name):
            found = i; break
    if found < 0:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no key named '%s'" % key_name, "keys": names}))
    elif new_name and str(new_name) != str(key_name) and str(new_name) in names:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "a key named '%s' already exists" % new_name}))
    else:
        e = keys[found]
        prior_name = str(e.get_editor_property("entry_name"))
        prior_type = _kt_short(e.get_editor_property("key_type"))
        ktcls = short = kerr = None
        if new_type:
            ktcls, short, kerr = _resolve_keytype(new_type)
        if kerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_blackboard_key"):
                if new_name:
                    e.set_editor_property("entry_name", unreal.Name(str(new_name)))
                if new_type:
                    e.set_editor_property("key_type", unreal.new_object(ktcls, bb))
                keys[found] = e
                bb.set_editor_property("keys", keys)
            saved = unreal.EditorAssetLibrary.save_asset(bb_path, only_if_is_dirty=False)
            _ledger().append({"op": "bb_set_key", "asset_path": bb_path, "index": found,
                              "prior_name": prior_name, "prior_type": prior_type})
            after = bb.get_editor_property("keys")[found]
            print("@@UMCP@@" + json.dumps({"status": "success", "blackboard": bb.get_name(),
                "index": found, "prior_name": prior_name, "prior_type": prior_type,
                "new_name": str(after.get_editor_property("entry_name")),
                "new_type": _kt_short(after.get_editor_property("key_type")),
                "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_blackboard_key(ctx, blackboard_path: str, key_name: str,
                           new_name: str = None, new_type: str = None) -> str:
        """Rename and/or retype an existing BlackboardData key (spec set_blackboard_key(name,new_name,type)).

        key_name: current key name; new_name: optional new name (must not collide); new_type: optional new
                  type (Bool|Int|Float|Vector|Object|Name|String|Enum|Rotator|Class). At least one of
                  new_name / new_type is required. Retype rebuilds the key_type subobject (Enum/Class keys
                  lose sub-config; set that separately). NOTE: BlackboardData keys hold no per-key default
                  VALUE in the asset (defaults are runtime), so this is rename/retype, not default-set.
                  Saved. Ledgered 'bb_set_key' {asset_path, index, prior_name, prior_type}; inverse:
                  restore prior_name + prior_type at index (FAITHFUL)."""
        params = {"blackboard_path": blackboard_path, "key_name": key_name,
                  "new_name": new_name, "new_type": new_type}
        try:
            return json.dumps(_exec(_SET_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_blackboard_key_types (read-only)                               #
    # ================================================================== #
    _LIST_KT_BODY = _BT2_HELPERS + r'''
filt = (PARAMS.get("filter") or "").strip().lower()
# BlackboardKeyType_* subclasses are NOT exposed as unreal.* attributes -> resolve each by
# loading /Script/AIModule.BlackboardKeyType_<Type> (only ones that actually load are reported).
_KNOWN = ["Bool", "Int", "Float", "Vector", "Object", "Name", "String", "Enum", "Rotator", "Class",
          "NativeEnum"]
rows = []
for short in _KNOWN:
    cn = "BlackboardKeyType_" + short
    cpath = "/Script/AIModule." + cn
    cls = _try(lambda: unreal.load_object(None, cpath))
    if not isinstance(cls, unreal.Class):
        continue
    if filt and filt not in short.lower():
        continue
    rows.append({"short": short, "class": cn, "class_path": cpath})
rows.sort(key=lambda r: r["short"])
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(rows), "key_types": rows,
    "accepted_by_writers": sorted(list({"Bool", "Int", "Float", "Vector", "Object", "Name", "String", "Enum", "Rotator", "Class"})),
    "note": "Types accepted by add_blackboard_key / set_blackboard_key are the friendly names in accepted_by_writers (case-insensitive). Enum/Class keys need sub-config (enum_type / base_class) set separately to be fully usable."}))
'''

    @mcp.tool()
    def list_blackboard_key_types(ctx, filter: str = None) -> str:
        """List available BlackboardKeyType_* classes (Bool/Int/Float/Vector/Object/Name/String/Enum/
        Rotator/Class + any others present). Read-only; no editor opened. filter = case-insensitive
        substring on the short type name. 'accepted_by_writers' lists the friendly names the write tools
        (add_blackboard_key / set_blackboard_key) accept."""
        try:
            return json.dumps(_exec(_LIST_KT_BODY, {"filter": filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_behavior_tree (read-only structural validator)             #
    # ================================================================== #
    _VALIDATE_BODY = _BT2_HELPERS + r'''
bt_path = PARAMS["bt_path"]
bt = _load_bt(bt_path)
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a BehaviorTree: %s" % bt_path}))
else:
    issues = []; warnings = []
    counts = {"composites": 0, "tasks": 0, "services": 0, "decorators": 0, "empty_slots": 0}
    bb = _try(lambda: bt.get_editor_property("blackboard_asset"))
    if bb is None:
        warnings.append("no blackboard_asset linked (blackboard decorators/tasks will have no keys to bind)")
    root = _try(lambda: bt.get_editor_property("root_node"))
    def _walk(node, path):
        counts["composites"] += 1
        for s in (_try(lambda: node.get_editor_property("services"), []) or []):
            counts["services"] += 1
        kids = _try(lambda: node.get_editor_property("children"), []) or []
        if len(kids) == 0:
            warnings.append("composite at '%s' (%s) has no children (dead branch)" % (path, _try(lambda: node.get_class().get_name())))
        for i, ch in enumerate(kids):
            cp = (path + "." + str(i)) if path not in ("", "root") else str(i)
            decs = _try(lambda: ch.get_editor_property("decorators"), []) or []
            counts["decorators"] += len(decs)
            ops = _try(lambda: ch.get_editor_property("decorator_ops"), []) or []
            if len(ops) > 0 and len(decs) == 0:
                issues.append("child slot '%s' has decorator_ops but no decorators" % cp)
            comp = _try(lambda: ch.get_editor_property("child_composite"))
            tsk = _try(lambda: ch.get_editor_property("child_task"))
            if comp is not None and tsk is not None:
                issues.append("child slot '%s' has BOTH a composite and a task (only one allowed)" % cp)
            if comp is None and tsk is None:
                counts["empty_slots"] += 1
                issues.append("child slot '%s' is empty (no child_composite and no child_task)" % cp)
            elif comp is not None:
                _walk(comp, cp)
            else:
                counts["tasks"] += 1
    if root is None:
        issues.append("BehaviorTree has no root_node (empty tree) -- set one with set_bt_root")
    else:
        if not isinstance(root, unreal.BTCompositeNode):
            issues.append("root_node is not a composite (%s)" % _try(lambda: root.get_class().get_name()))
        else:
            _walk(root, "root")
    valid = len(issues) == 0
    print("@@UMCP@@" + json.dumps({"status": "success", "behavior_tree": bt.get_name(),
        "valid": valid, "issue_count": len(issues), "issues": issues,
        "warning_count": len(warnings), "warnings": warnings, "counts": counts,
        "blackboard_asset": (bb.get_path_name() if bb else None),
        "note": "Structural validation over the runtime node tree (root present; each child slot has exactly one of composite/task; no orphan decorator_ops; composites non-empty). Not a semantic/compile check of node config."}))
'''

    @mcp.tool()
    def validate_behavior_tree(ctx, bt_path: str) -> str:
        """Structurally validate a BehaviorTree (read-only; no editor opened).

        Checks: root_node present and is a composite; every child slot has exactly one of
        child_composite / child_task (flags empty or double-filled slots); no decorator_ops without
        decorators; composites are non-empty (warning). Returns valid (bool), issues[] (hard problems),
        warnings[] (soft), and counts. NOT a semantic compile of node configuration."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"bt_path": bt_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # search_bt_nodes (read-only ranked search over BT node classes)      #
    # ================================================================== #
    _SEARCH_BODY = _BT2_HELPERS + r'''
import inspect
query = (PARAMS.get("query") or "").strip()
queries = PARAMS.get("queries") or ([query] if query else [])
kind = (PARAMS.get("kind") or "").strip().lower()
max_results = int(PARAMS.get("max_results") or 40)
include_bp = bool(PARAMS.get("include_blueprint"))
KINDS = {"composite": "BTCompositeNode", "task": "BTTaskNode", "service": "BTService", "decorator": "BTDecorator"}
want = [kind] if kind in KINDS else list(KINDS.keys())
bases = {k: getattr(unreal, KINDS[k], None) for k in want}
ql = [q.lower() for q in queries if q]
def _score(name):
    nl = name.lower()
    if not ql:
        return 1
    best = 0
    for q in ql:
        if nl == q:
            best = max(best, 100)
        elif nl.startswith(q):
            best = max(best, 60)
        elif q in nl:
            best = max(best, 30)
    return best
rows = []
native_scanned = 0
for nm in dir(unreal):
    c = getattr(unreal, nm, None)
    if not inspect.isclass(c):
        continue
    knd = None
    for k, b in bases.items():
        if b is not None and issubclass(c, b) and c is not b:
            knd = k; break
    if knd is None:
        continue
    native_scanned += 1
    sc = _score(nm)
    if sc <= 0:
        continue
    rows.append({"name": nm, "kind": knd, "class_path": "/Script/AIModule." + nm,
                 "is_blueprint": False, "score": sc})
bp_scanned = 0
bp_note = None
if include_bp:
    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        assets = ar.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "Blueprint"), False) or []
        for a in assets:
            bp_scanned += 1
            npc = None
            try:
                tv = a.get_tag_value("NativeParentClass")
                npc = str(tv) if tv else None
            except Exception:
                npc = None
            if not npc:
                continue
            knd = None
            for k, base_short in KINDS.items():
                if base_short in npc:
                    knd = k; break
            if knd is None:
                continue
            nm = str(a.asset_name)
            sc = _score(nm)
            if sc <= 0:
                continue
            try:
                pn = str(a.package_name) + "." + nm
            except Exception:
                pn = nm
            rows.append({"name": nm, "kind": knd, "class_path": pn, "is_blueprint": True, "score": sc})
    except Exception as e:
        bp_note = "blueprint scan failed: %s" % e
rows.sort(key=lambda r: (-r["score"], len(r["name"]), r["name"]))
truncated = len(rows) > max_results
rows = rows[:max_results]
out = {"status": "success", "queries": queries, "kind": (kind if kind in KINDS else "all"),
       "count": len(rows), "results": rows, "truncated": truncated,
       "native_scanned": native_scanned, "blueprint_scanned": bp_scanned}
if bp_note:
    out["blueprint_note"] = bp_note
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def search_bt_nodes(ctx, query: str = None, queries: list = None, kind: str = None,
                        max_results: int = 40, include_blueprint: bool = False) -> str:
        """Ranked keyword search over BehaviorTree node classes (Composite/Task/Service/Decorator).

        query / queries: one or more case-insensitive substrings (exact > prefix > contains ranking; any
                         query matching scores the class). Empty query returns all (ranked by name).
        kind:            restrict to composite | task | service | decorator (default: all four).
        include_blueprint: also scan /Game Blueprint assets whose NativeParentClass is a BT node class.
        Read-only; no editor opened. Returns results[] {name, kind, class_path, is_blueprint, score}."""
        params = {"query": query, "queries": queries, "kind": kind,
                  "max_results": max_results, "include_blueprint": include_blueprint}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_bt_node_type_info (read-only class reflection)                  #
    # ================================================================== #
    _TYPE_INFO_BODY = _BT2_HELPERS + r'''
node_class = PARAMS["node_class"]
filt = (PARAMS.get("filter") or "").strip().lower()
editable_only = bool(PARAMS.get("editable_only", True))
cls = _node_class(node_class)
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if cls is None or not (inspect.isclass(cls) and issubclass(cls, unreal.BTNode)):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unknown BT node class '%s' (want a BTComposite_/BTTask_/BTService_/BTDecorator_ class name or path)" % node_class}))
else:
    # kind
    knd = "node"
    for k, b in {"composite": "BTCompositeNode", "task": "BTTaskNode", "service": "BTService", "decorator": "BTDecorator"}.items():
        bb = getattr(unreal, b, None)
        if bb is not None and issubclass(cls, bb):
            knd = k; break
    # cls is a Python class object -> .get_name() is unbound; get the name from the CDO instead.
    cdo = _try(lambda: unreal.get_default_object(cls))
    clsname = _try(lambda: cdo.get_class().get_name()) or (node_class if isinstance(node_class, str) else str(node_class))
    info = {"status": "success", "node_class": clsname, "kind": knd,
            "class_path": "/Script/AIModule." + clsname}
    if mrl is not None and hasattr(mrl, "get_class_metadata_json"):
        info["metadata"] = _try(lambda: json.loads(mrl.get_class_metadata_json(cls)))
    props = []
    if mrl is not None and hasattr(mrl, "get_object_property_metadata_json"):
        if cdo is not None:
            pm = _try(lambda: json.loads(mrl.get_object_property_metadata_json(cdo)))
            for p in ((pm or {}).get("properties") or []):
                flags = p.get("flags") or []
                if editable_only and "Edit" not in flags:
                    continue
                if filt and filt not in str(p.get("name", "")).lower():
                    continue
                props.append({"name": p.get("name"), "cpp_type": p.get("cpp_type"),
                              "category": p.get("category"), "owner_class": p.get("owner_class"),
                              "tooltip": p.get("tooltip"), "flags": flags})
    info["property_count"] = len(props)
    info["properties"] = props
    info["note"] = ("Properties reflected from the class CDO via MCPReflectionLibrary. 'Edit'-flagged props "
                    "are settable with set_bt_node_property (use the snake_case name). editable_only=false "
                    "includes transient/internal props. Some 5.8 props are FValueOrBBKey_* structs (bind a "
                    "blackboard key or a literal) and may not be set as a plain scalar.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_bt_node_type_info(ctx, node_class: str, filter: str = None, editable_only: bool = True) -> str:
        """Kind + editable properties/defaults for a BehaviorTree node class (read-only; no editor).

        node_class:    a BT node class short name ('BTTask_Wait', 'BTDecorator_Blackboard') or class path.
        filter:        case-insensitive substring on property name.
        editable_only: only 'Edit'-flagged (author-settable) properties (default True; False = all,
                       incl. transient/internal).

        Returns kind (composite/task/service/decorator), class metadata (parent/abstract/blueprintable),
        and properties[] {name, cpp_type, category, owner_class, tooltip, flags}. Property names feed
        set_bt_node_property (snake_case)."""
        params = {"node_class": node_class, "filter": filter, "editable_only": editable_only}
        try:
            return json.dumps(_exec(_TYPE_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # DEFERRED runtime tools (PIE-only; PROTOCOL forbids PIE) -> honest refusals #
    # ================================================================== #
    _DEFERRED = {
        "get_bt_runtime": "Inspect a RUNNING BehaviorTreeComponent on an actor: needs a live game world "
                          "(PIE/standalone) with a possessed AIController ticking the tree. PROTOCOL "
                          "forbids launching PIE against the shared editor. No asset-side equivalent.",
        "control_bt_runtime": "Start/stop/pause a RUNNING BehaviorTreeComponent: runtime-only (needs a "
                              "live BrainComponent in PIE). PROTOCOL forbids PIE.",
        "set_bt_dynamic_subtree": "Inject a subtree at a runtime gameplay tag on a RUNNING tree "
                                  "(UBehaviorTreeComponent::SetDynamicSubtree): runtime-only, needs PIE. "
                                  "PROTOCOL forbids PIE.",
    }

    # ---- C++ #23: BT RUNTIME (needs a live PIE world). Sent LEAN (raw code, no _wrap footprint). ----
    def _bt_lean_exec(body, params):
        p = dict(params or {})
        b64 = base64.b64encode(json.dumps(p).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        resp = send_command("execute_python", {"code": header + body})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    # Resolve the target actor in the live PIE world (by name/label, or the first AIController) then leave it in _t.
    _RT_HEAD = (
        "import unreal, json\n"
        "ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
        "gw = ues.get_game_world() if les.is_in_play_in_editor() else None\n"
        "m = getattr(unreal, 'MCPReflectionLibrary', None)\n"
        "if gw is None:\n"
        "    print('@@UMCP@@' + json.dumps({'status':'error','message':'not in PIE — play_in_editor, poll get_pie_status until in_pie, then retry'}))\n"
        "elif m is None:\n"
        "    print('@@UMCP@@' + json.dumps({'status':'error','message':'MCPReflectionLibrary unavailable'}))\n"
        "else:\n"
        "    _name = PARAMS.get('actor') or ''\n"
        "    _t = None\n"
        "    _acts = unreal.GameplayStatics.get_all_actors_of_class(gw, unreal.Actor)\n"
        "    if _name:\n"
        "        for _a in _acts:\n"
        "            try:\n"
        "                if _a.get_name() == _name or (_name.lower() in _a.get_name().lower()) or _a.get_actor_label() == _name:\n"
        "                    _t = _a; break\n"
        "            except Exception:\n"
        "                pass\n"
        "    else:\n"
        "        for _a in _acts:\n"
        "            if isinstance(_a, unreal.AIController):\n"
        "                _t = _a; break\n"
        "    if _t is None:\n"
        "        print('@@UMCP@@' + json.dumps({'status':'error','message':'no target actor/AIController found (actor=%s)' % (_name or '(first AIController)')}))\n"
        "    else:\n")

    @mcp.tool()
    def get_bt_runtime(ctx, actor: str = None, include_blackboard: bool = True) -> str:
        """Inspect a RUNNING BehaviorTreeComponent on an actor in the live PIE world (C++ #23 handler):
        is_running, active_node, active_tasks/trees, and (optional) the blackboard snapshot.

        REQUIRES PIE — play_in_editor, poll get_pie_status until in_pie, then call this.
        actor: substring/name/label match (empty = the first AIController). include_blackboard: default true."""
        try:
            body = _RT_HEAD + "        print('@@UMCP@@' + json.dumps(json.loads(m.get_behavior_tree_runtime_json(_t, bool(PARAMS.get('include_blackboard', True))))))\n"
            return json.dumps(_bt_lean_exec(body, {"actor": actor, "include_blackboard": include_blackboard}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def control_bt_runtime(ctx, action: str = "start", actor: str = None) -> str:
        """Control a RUNNING BehaviorTree on an actor in the live PIE world (C++ #23 handler).
        action: start | restart | stop | pause | resume. REQUIRES PIE (see get_bt_runtime).
        actor: substring/name/label match (empty = the first AIController)."""
        try:
            body = _RT_HEAD + "        print('@@UMCP@@' + json.dumps(json.loads(m.control_behavior_tree_runtime_json(_t, PARAMS.get('action') or 'start'))))\n"
            return json.dumps(_bt_lean_exec(body, {"actor": actor, "action": action}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_bt_dynamic_subtree(ctx, inject_tag: str = None, subtree: str = None, actor: str = None) -> str:
        """Inject a dynamic subtree at a runtime gameplay tag on a RUNNING BehaviorTreeComponent (C++ #23:
        UBehaviorTreeComponent::SetDynamicSubtree). REQUIRES PIE (see get_bt_runtime).
        inject_tag: a REGISTERED gameplay tag. subtree: a /Game BehaviorTree asset path (empty = clear).
        actor: substring/name/label match (empty = the first AIController)."""
        try:
            body = _RT_HEAD + (
                "        _sub = unreal.EditorAssetLibrary.load_asset(PARAMS.get('subtree')) if PARAMS.get('subtree') else None\n"
                "        print('@@UMCP@@' + json.dumps(json.loads(m.set_behavior_tree_dynamic_subtree_json(_t, PARAMS.get('inject_tag') or '', _sub))))\n")
            return json.dumps(_bt_lean_exec(body, {"actor": actor, "inject_tag": inject_tag, "subtree": subtree}), indent=2)
        except Exception as e:
            return f"Error: {e}"
