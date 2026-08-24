"""StateTree AUTHORING writes (create asset + states + native task/evaluator/condition/consideration/
global-task nodes + removes).

PURE PYTHON — no C++ (verdict cpp18_statetree_writer_handoff.md). Foundation live-verified 2026-08-17:
  - create: unreal.AssetTools.create_asset(name, dir, unreal.StateTree, None)  [factory None = NON-modal;
    the StateTreeFactory's schema is not Python-settable, so we create bare + build editor data ourselves].
  - editor data: unreal.new_object(unreal.StateTreeEditorData, st, "StateTreeEditorData") — persists across
    save/reload and is found by name (the UStateTree.EditorData UPROPERTY is editor-only / Python-filtered,
    so it is NOT set via set_editor_property; the named subobject survives serialization regardless).
  - native node: whole-struct import on FStateTreeEditorNode -> en.import_text("(Node=/Script/<Module>.<Type>)")
    (en.node cannot be set directly: "cannot be edited on instances"; importing the WHOLE struct works).
  - graph surface: StateTreeEditorData.sub_trees/evaluators/global_tasks + StateTreeState.name/tasks/
    enter_conditions/considerations/children/transitions/selection_behavior/type are all get/set_editor_property.

Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM from
bt_write.py. Each tool is ONE mutate-op per call, ledgered with the info its inverse needs (undo folds land in
editor_level.undo in a follow-up pass). NEVER touch a real asset — validate on scratch duplicates.
"""

import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from bt_write.py / editor_level.py) ---
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
        # Drain the editor's Python cyclic-GC heap after each op so a UE GC's PyGC_Collect can't AV on our
        # leftover garbage. See statetree_write2.py _query for the full root-cause note. (2026-08-18)
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
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
    _ST_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
SCHEMAS = {"component": "StateTreeComponentSchema", "ai": "StateTreeAIComponentSchema",
           "brain": "BrainComponentStateTreeSchema", "camera": "CameraDirectorStateTreeSchema"}
KIND_ARRAY = {"task": ("state", "tasks"), "condition": ("state", "enter_conditions"),
              "consideration": ("state", "considerations"), "evaluator": ("ed", "evaluators"),
              "global_task": ("ed", "global_tasks")}
def _load_st(p):
    st = unreal.EditorAssetLibrary.load_asset(p)
    if st is None or not isinstance(st, unreal.StateTree):
        return None
    return st
def _st_save(p):
    # Non-validating save. EditorAssetLibrary.save_asset triggers InternalPromptForCheckoutAndSave ->
    # asset VALIDATION, which hard-crashes (AV) on MCP-written StateTrees. save_packages persists without it.
    try:
        a = unreal.EditorAssetLibrary.load_asset(p)
        if a is None:
            return False
        # C++ #20 THE FIX: import_text node authoring leaves the editor node's Instance struct EMPTY + ID zero;
        # the StateTree compiler/serializer AVs on that during save. repair_state_tree_nodes reallocs each node's
        # Instance to match its type + stamps fresh GUIDs (mirrors the editor). Idempotent; guarded until C++ lands.
        if isinstance(a, unreal.StateTree):
            _rl = getattr(unreal, "MCPReflectionLibrary", None)
            if _rl is not None and hasattr(_rl, "repair_state_tree_nodes"):
                try:
                    _rl.repair_state_tree_nodes(a)
                except Exception:
                    pass
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([a.get_outermost()], False))
    except Exception:
        return False
def _find_ed(st):
    for i in range(0, 16):
        try:
            o = unreal.find_object(st, "StateTreeEditorData_%d" % i)
            if o is not None:
                return o
        except Exception:
            pass
    try:
        o = unreal.find_object(st, "StateTreeEditorData")
        if o is not None:
            return o
    except Exception:
        pass
    return None
def _ensure_ed(st):
    ed = _find_ed(st)
    if ed is None:
        ed = unreal.new_object(unreal.StateTreeEditorData, st, "StateTreeEditorData")
    return ed
def _iter_states(ed):
    # depth-first over sub_trees + children; yields StateTreeState objects
    out = []
    def rec(s):
        out.append(s)
        for c in list(s.get_editor_property("children") or []):
            if c is not None:
                rec(c)
    for r in list(ed.get_editor_property("sub_trees") or []):
        if r is not None:
            rec(r)
    return out
def _find_state(ed, name):
    if not name:
        return None
    for s in _iter_states(ed):
        try:
            if str(s.get_editor_property("name")) == name:
                return s
        except Exception:
            pass
    return None
def _type_path(node_type):
    t = str(node_type or "")
    if t.startswith("/Script/"):
        return t
    return "/Script/StateTreeModule." + t
def _append_prop(obj, prop, item):
    arr = obj.get_editor_property(prop)
    arr.append(item)
    obj.set_editor_property(prop, arr)
def _pascal(s):
    # SNAKE_CASE enum name -> export PascalCase (ON_STATE_COMPLETED -> OnStateCompleted). Inputs without an
    # underscore are assumed already-PascalCase (OnTick / GotoState / Normal) and returned unchanged.
    s = str(s or "")
    if "_" in s:
        return "".join(w.capitalize() for w in s.split("_"))
    return s
STATE_ENUM_PROP = {"type": "StateTreeStateType", "selection_behavior": "StateTreeStateSelectionBehavior"}
def _coerce_state_val(prop, value):
    if prop in STATE_ENUM_PROP:
        e = getattr(unreal, STATE_ENUM_PROP[prop], None)
        return getattr(e, str(value)) if e is not None else value
    if prop == "weight":
        return float(value)
    return str(value)
def _state_desc(s):
    tks = list(s.get_editor_property("tasks") or [])
    return {"name": str(s.get_editor_property("name")),
            "task_count": len(tks),
            "child_count": len(list(s.get_editor_property("children") or [])),
            "condition_count": len(list(s.get_editor_property("enter_conditions") or [])),
            "consideration_count": len(list(s.get_editor_property("considerations") or []))}
def _node_export(en):
    try:
        return en.export_text()
    except Exception:
        return ""
def _snapshot_state(s):
    # recursive capture for faithful remove_state undo: name + whole-node export_text arrays + children.
    snap = {"name": str(s.get_editor_property("name")),
            "tasks": [_node_export(x) for x in list(s.get_editor_property("tasks") or [])],
            "conditions": [_node_export(x) for x in list(s.get_editor_property("enter_conditions") or [])],
            "considerations": [_node_export(x) for x in list(s.get_editor_property("considerations") or [])],
            "children": []}
    try: snap["type"] = str(s.get_editor_property("type"))
    except Exception: snap["type"] = None
    try: snap["selection"] = str(s.get_editor_property("selection_behavior"))
    except Exception: snap["selection"] = None
    for c in list(s.get_editor_property("children") or []):
        if c is not None:
            snap["children"].append(_snapshot_state(c))
    return snap
'''

    # ------------------------------------------------------------------ #
    # create_statetree                                                    #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; schema = str(PARAMS.get("schema") or "component").lower()
name = asset_path.rsplit("/", 1)[-1]; pkg = asset_path.rsplit("/", 1)[0]
at = unreal.AssetToolsHelpers.get_asset_tools()
if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
elif schema not in SCHEMAS:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad schema '%s' (want %s)" % (schema, "|".join(sorted(SCHEMAS)))}))
else:
    st = at.create_asset(name, pkg, unreal.StateTree, None)
    if st is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % asset_path}))
    else:
        ed = _ensure_ed(st)
        sch_cls = getattr(unreal, SCHEMAS[schema], None)
        if sch_cls is not None:
            ed.set_editor_property("schema", unreal.new_object(sch_cls, ed))
        # C++ #19 GATING FIX: root the editor data via the real UStateTree::EditorData pointer while it is still
        # alive (just created). Otherwise it is unreferenced -> a later GC purges it and the next ed-touching C++
        # handler AVs (proven: set_statetree_color undo crashed the editor). hasattr-guarded: no-op until rebuild.
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        _linked = None
        if _rl is not None and hasattr(_rl, "ensure_state_tree_editor_data"):
            try:
                _linked = json.loads(_rl.ensure_state_tree_editor_data(st))
            except Exception:
                _linked = None
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_create", "asset_path": asset_path})
        print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
            "asset_path": asset_path, "schema": SCHEMAS[schema], "saved": bool(saved),
            "ed_linked": _linked, "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_statetree_state                                                 #
    # ------------------------------------------------------------------ #
    _ADD_STATE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; name = PARAMS["name"]; parent = PARAMS.get("parent") or ""
selection = PARAMS.get("selection_behavior"); state_type = PARAMS.get("state_type")
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _ensure_ed(st)
    if _find_state(ed, name) is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "state name already exists: %s" % name}))
    else:
        par_state = _find_state(ed, parent) if parent else None
        if parent and par_state is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no parent state named '%s'" % parent}))
        else:
            s = unreal.new_object(unreal.StateTreeState, ed)
            s.set_editor_property("name", name)
            if selection:
                try: s.set_editor_property("selection_behavior", getattr(unreal.StateTreeStateSelectionBehavior, str(selection)))
                except Exception: pass
            if state_type:
                try: s.set_editor_property("type", getattr(unreal.StateTreeStateType, str(state_type)))
                except Exception: pass
            if par_state is not None:
                _append_prop(par_state, "children", s)
            else:
                _append_prop(ed, "sub_trees", s)
            saved = _st_save(asset_path)
            _ledger().append({"op": "st_add_state", "asset_path": asset_path, "state_name": name, "parent": parent})
            print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
                "state": _state_desc(s), "parent": parent or "(root)", "saved": bool(saved),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add node (task / evaluator / condition / consideration / global_task) #
    # ------------------------------------------------------------------ #
    _ADD_NODE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; kind = PARAMS["kind"]; node_type = PARAMS["node_type"]
state_name = PARAMS.get("state_name") or ""; config = PARAMS.get("config") or ""
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif kind not in KIND_ARRAY:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad kind '%s' (want %s)" % (kind, "|".join(sorted(KIND_ARRAY)))}))
else:
    ed = _ensure_ed(st)
    scope, prop = KIND_ARRAY[kind]
    owner = ed
    if scope == "state":
        owner = _find_state(ed, state_name)
        if owner is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s' for %s" % (state_name, kind)}))
            owner = "ERR"
    if owner != "ERR":
        en = unreal.StateTreeEditorNode()
        imp = "(Node=" + _type_path(node_type) + config + ")"
        ok = en.import_text(imp)
        node = en.get_editor_property("node")
        head = (node.export_text() if node is not None else "").split("(", 1)[0].strip()
        if not ok or "/Script/" not in head:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "import_text failed for %s (bad node_type/config?)" % imp, "node_head": head}))
        else:
            _append_prop(owner, prop, en)
            idx = len(owner.get_editor_property(prop)) - 1
            saved = _st_save(asset_path)
            _ledger().append({"op": "st_add_node", "asset_path": asset_path, "kind": kind,
                "state_name": state_name, "index": idx, "node_type": head})
            print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
                "kind": kind, "node_type": head, "state": state_name or "(editor-data)", "index": idx,
                "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # remove_statetree_state (by name; captures export_text for undo)     #
    # ------------------------------------------------------------------ #
    _REMOVE_STATE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; name = PARAMS["name"]
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _ensure_ed(st)
    target = _find_state(ed, name)
    if target is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s'" % name}))
    else:
        snap = _snapshot_state(target)
        # search sub_trees + every state's children for the target; filter it out
        removed_from = None
        subs = list(ed.get_editor_property("sub_trees") or [])
        if target in subs:
            subs = [x for x in subs if x != target]; ed.set_editor_property("sub_trees", subs); removed_from = "(root)"
        else:
            for s in _iter_states(ed):
                kids = list(s.get_editor_property("children") or [])
                if target in kids:
                    s.set_editor_property("children", [x for x in kids if x != target]); removed_from = str(s.get_editor_property("name")); break
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_remove_state", "asset_path": asset_path, "state_name": name,
            "parent": removed_from, "snapshot": snap})
        print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
            "removed_state": name, "from": removed_from, "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # remove_statetree_node (state+kind+index)                            #
    # ------------------------------------------------------------------ #
    _REMOVE_NODE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; kind = PARAMS["kind"]; index = int(PARAMS["index"])
state_name = PARAMS.get("state_name") or ""
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif kind not in KIND_ARRAY:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad kind '%s'" % kind}))
else:
    ed = _ensure_ed(st)
    scope, prop = KIND_ARRAY[kind]
    owner = ed if scope == "ed" else _find_state(ed, state_name)
    if owner is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s'" % state_name}))
    else:
        arr = list(owner.get_editor_property(prop) or [])
        if index < 0 or index >= len(arr):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "index %d out of range [0,%d)" % (index, len(arr))}))
        else:
            rem = arr[index]
            captured = ""
            try: captured = rem.export_text()
            except Exception: pass
            del arr[index]
            owner.set_editor_property(prop, arr)
            saved = _st_save(asset_path)
            _ledger().append({"op": "st_remove_node", "asset_path": asset_path, "kind": kind,
                "state_name": state_name, "index": index, "export_text": captured})
            print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
                "kind": kind, "index": index, "remaining": len(arr), "saved": bool(saved),
                "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # add_statetree_transition (whole-struct import_text; State cannot be set on instances) #
    # ------------------------------------------------------------------ #
    _ADD_TRANSITION_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; state_name = PARAMS["state_name"]; trigger = PARAMS["trigger"]
target = PARAMS.get("target") or ""; priority = PARAMS.get("priority") or "Normal"; link_type = PARAMS.get("link_type") or "GotoState"
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _ensure_ed(st); state = _find_state(ed, state_name)
    if state is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s'" % state_name}))
    else:
        q = chr(34); lt = _pascal(link_type)
        statepart = None
        if lt == "GotoState":
            tgt = _find_state(ed, target)
            if tgt is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "no target state '%s' for GotoState" % target}))
            else:
                tid = tgt.get_editor_property("id").export_text()
                statepart = "State=(Name=" + q + str(target) + q + ",ID=" + tid + ",LinkType=GotoState)"
        else:
            statepart = "State=(LinkType=" + lt + ")"
        if statepart is not None:
            imp = "(Trigger=" + _pascal(trigger) + ",Priority=" + _pascal(priority) + "," + statepart + ")"
            tr = unreal.StateTreeTransition()
            ok = tr.import_text(imp)
            if not ok:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "transition import_text failed", "import": imp}))
            else:
                _append_prop(state, "transitions", tr)
                idx = len(state.get_editor_property("transitions")) - 1
                saved = _st_save(asset_path)
                _ledger().append({"op": "st_add_transition", "asset_path": asset_path, "state_name": state_name, "index": idx})
                print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(), "state": state_name,
                    "trigger": _pascal(trigger), "link_type": lt, "target": target, "index": idx,
                    "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    _REMOVE_TRANSITION_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; state_name = PARAMS["state_name"]; index = int(PARAMS["index"])
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _ensure_ed(st); state = _find_state(ed, state_name)
    if state is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s'" % state_name}))
    else:
        arr = list(state.get_editor_property("transitions") or [])
        if index < 0 or index >= len(arr):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "transition index %d out of range [0,%d)" % (index, len(arr))}))
        else:
            captured = ""
            try: captured = arr[index].export_text()
            except Exception: pass
            del arr[index]; state.set_editor_property("transitions", arr)
            saved = _st_save(asset_path)
            _ledger().append({"op": "st_remove_transition", "asset_path": asset_path, "state_name": state_name,
                "index": index, "export_text": captured})
            print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(), "state": state_name,
                "index": index, "remaining": len(arr), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    _SET_STATE_PROP_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; state_name = PARAMS["state_name"]; prop = PARAMS["property"]; value = PARAMS["value"]
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _ensure_ed(st); state = _find_state(ed, state_name)
    if state is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no state named '%s'" % state_name}))
    elif prop not in ("name", "weight", "type", "selection_behavior"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "unsupported state property '%s' (name|weight|type|selection_behavior)" % prop}))
    else:
        try:
            prior = state.get_editor_property(prop)
            if prop in STATE_ENUM_PROP:
                prior_s = str(prior).split(".")[-1].split(":")[0].strip().strip(">").strip()  # clean enum NAME
            elif prop == "weight":
                prior_s = str(float(prior))
            else:
                prior_s = str(prior)
            state.set_editor_property(prop, _coerce_state_val(prop, value))
            saved = _st_save(asset_path)
            _ledger().append({"op": "st_set_state_prop", "asset_path": asset_path, "state_name": state_name,
                "property": prop, "prior": prior_s})
            print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(), "state": state_name,
                "property": prop, "new_value": str(state.get_editor_property(prop)), "prior": prior_s,
                "saved": bool(saved), "ledger_depth": len(_ledger())}))
        except Exception as e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "set failed: %s" % str(e)}))
'''

    _SET_SCHEMA_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; schema = str(PARAMS.get("schema") or "").lower()
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif schema not in SCHEMAS:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad schema '%s' (want %s)" % (schema, "|".join(sorted(SCHEMAS)))}))
else:
    ed = _ensure_ed(st)
    ps = ed.get_editor_property("schema")
    prior_cls = ps.get_class().get_name() if ps is not None else None
    ed.set_editor_property("schema", unreal.new_object(getattr(unreal, SCHEMAS[schema]), ed))
    saved = _st_save(asset_path)
    _ledger().append({"op": "st_set_schema", "asset_path": asset_path, "prior_schema_class": prior_cls})
    print("@@UMCP@@" + json.dumps({"status": "success", "state_tree": st.get_name(),
        "schema": SCHEMAS[schema], "prior_schema": prior_cls, "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ============================ MCP TOOLS ============================ #

    @mcp.tool()
    def create_statetree(ctx, asset_path: str, schema: str = "component") -> str:
        """Create a new StateTree asset with an editor-data schema. schema: component|ai|brain|camera.
        Non-modal (factory None + manual editor data). Reversible (undo deletes the asset)."""
        try:
            return json.dumps(_exec(_CREATE_BODY, {"asset_path": asset_path, "schema": schema}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_statetree_state(ctx, asset_path: str, name: str, parent: str = "",
                            selection_behavior: str = None, state_type: str = None) -> str:
        """Add a state named `name` under `parent` (empty = a root sub-tree). Optional selection_behavior
        (e.g. TRY_ENTER_STATES) + state_type (e.g. STATE). Reversible."""
        return json.dumps(_exec(_ADD_STATE_BODY, {"asset_path": asset_path, "name": name, "parent": parent,
            "selection_behavior": selection_behavior, "state_type": state_type}), indent=2)

    def _add_node(asset_path, kind, node_type, state_name, config):
        return json.dumps(_exec(_ADD_NODE_BODY, {"asset_path": asset_path, "kind": kind,
            "node_type": node_type, "state_name": state_name, "config": config}), indent=2)

    @mcp.tool()
    def add_statetree_task(ctx, asset_path: str, state_name: str, node_type: str, config: str = "") -> str:
        """Add a native task node to a state. node_type: bare (StateTreeModule) or full /Script/<Mod>.<Type>.
        config: optional export-text paren body, e.g. (Text="hi"). Reversible."""
        return _add_node(asset_path, "task", node_type, state_name, config)

    @mcp.tool()
    def add_statetree_condition(ctx, asset_path: str, state_name: str, node_type: str, config: str = "") -> str:
        """Add a native enter-condition node to a state. Reversible."""
        return _add_node(asset_path, "condition", node_type, state_name, config)

    @mcp.tool()
    def add_statetree_consideration(ctx, asset_path: str, state_name: str, node_type: str, config: str = "") -> str:
        """Add a native utility consideration node to a state. Reversible."""
        return _add_node(asset_path, "consideration", node_type, state_name, config)

    @mcp.tool()
    def add_statetree_evaluator(ctx, asset_path: str, node_type: str, config: str = "") -> str:
        """Add a native evaluator node at the tree level (editor data). Reversible."""
        return _add_node(asset_path, "evaluator", node_type, "", config)

    @mcp.tool()
    def add_statetree_global_task(ctx, asset_path: str, node_type: str, config: str = "") -> str:
        """Add a native global task node at the tree level (editor data). Reversible."""
        return _add_node(asset_path, "global_task", node_type, "", config)

    @mcp.tool()
    def remove_statetree_state(ctx, asset_path: str, name: str) -> str:
        """Remove a state (and its subtree) by name. Reversible."""
        return json.dumps(_exec(_REMOVE_STATE_BODY, {"asset_path": asset_path, "name": name}), indent=2)

    @mcp.tool()
    def remove_statetree_node(ctx, asset_path: str, kind: str, index: int, state_name: str = "") -> str:
        """Remove a node by kind (task|condition|consideration|evaluator|global_task) + index. Reversible."""
        return json.dumps(_exec(_REMOVE_NODE_BODY, {"asset_path": asset_path, "kind": kind,
            "index": index, "state_name": state_name}), indent=2)

    @mcp.tool()
    def add_statetree_transition(ctx, asset_path: str, state_name: str, trigger: str, target: str = "",
                                 priority: str = "Normal", link_type: str = "GotoState") -> str:
        """Add a transition to a state. trigger: ON_STATE_COMPLETED|ON_STATE_SUCCEEDED|ON_STATE_FAILED|ON_TICK|
        ON_EVENT|ON_DELEGATE. link_type GOTO_STATE needs `target` (a state name); NEXT_STATE|SUCCEEDED|FAILED etc.
        need no target. priority: LOW|NORMAL|MEDIUM|HIGH|CRITICAL. Reversible."""
        return json.dumps(_exec(_ADD_TRANSITION_BODY, {"asset_path": asset_path, "state_name": state_name,
            "trigger": trigger, "target": target, "priority": priority, "link_type": link_type}), indent=2)

    @mcp.tool()
    def remove_statetree_transition(ctx, asset_path: str, state_name: str, index: int) -> str:
        """Remove a transition from a state by index. Reversible (re-imported from capture)."""
        return json.dumps(_exec(_REMOVE_TRANSITION_BODY, {"asset_path": asset_path, "state_name": state_name,
            "index": index}), indent=2)

    @mcp.tool()
    def set_statetree_state_property(ctx, asset_path: str, state_name: str, property: str, value: str) -> str:
        """Set a state property: name | weight | type (STATE|GROUP|SUBTREE|LINKED|LINKED_ASSET) |
        selection_behavior (TRY_ENTER_STATE|TRY_SELECT_CHILDREN_IN_ORDER|...). Reversible."""
        return json.dumps(_exec(_SET_STATE_PROP_BODY, {"asset_path": asset_path, "state_name": state_name,
            "property": property, "value": value}), indent=2)

    @mcp.tool()
    def set_statetree_schema(ctx, asset_path: str, schema: str) -> str:
        """Set the StateTree schema: component|ai|brain|camera. Reversible."""
        return json.dumps(_exec(_SET_SCHEMA_BODY, {"asset_path": asset_path, "schema": schema}), indent=2)
