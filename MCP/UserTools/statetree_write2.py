"""StateTree completion batch 2 — 16 statetree tools (6 READ + 4 property WRITE + 6 params/bindings/compile).

Finishes the "statetree" tool category. The 4 property WRITERS + all param/binding/compile tools now call the
C++ #18 reflection handlers on `unreal.MCPReflectionLibrary` (hasattr-guarded). This REPLACES the previous
Python writers that HARD-CRASHED the shared interpreter: export_text/import_text of a nested FInstancedStruct
(a StateTree node's Node/Instance) raised EXCEPTION_ACCESS_VIOLATION inside python311.dll — uncatchable from
Python. The C++ path does the same edits via FProperty reflection with every nested struct/object null-guarded,
so NO export_text/import_text of nested instanced structs happens anywhere in this module now.

The 6 READERS are UNCHANGED from the validated batch (they never touched the faulting nested-struct import path;
they only export_text leaf nodes for display). Their scaffolding (query convention, base64 PARAMS, Output-Log
capture, per-session ledger) is copied VERBATIM from statetree_write.py.

Each WRITE is ONE reversible op per call, wrapped in unreal.ScopedEditorTransaction and ledgered with the info
its inverse needs. Every inverse RE-CALLS the same C++ handler with the captured prior ("prev") — no nested
import. Undo-fold schemas are reported to the coordinator for editor_level.undo; NO local undo tool is defined.
compile_statetree is a build step (regenerates compiled data), not a reversible edit, so it is not ledgered.

C++ handlers used (snake_case on unreal.MCPReflectionLibrary): set_state_tree_node_property_json,
set_state_tree_transition_property_json, set_state_tree_component_tree_json, set_state_tree_color_json,
get_state_tree_parameters_json, add_state_tree_parameter, set_state_tree_parameter, remove_state_tree_parameter,
add_state_tree_binding, remove_state_tree_binding, compile_state_tree (+ get_state_tree_bindings_json reader).
NEVER touch a real asset — validate on scratch under /Game/MCP_Scratch with the MCP_ST3_ prefix.
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
# output_file/error_file/original_stdout/original_stderr/success/user_code/code_obj. Quote char via chr(34).


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        # BEFORE-drain (2026-08-18, second pass): the after-drain alone was INSUFFICIENT — the writer's own
        # save (_st_save -> save_packages) triggers a UE GC MID-op, so PyGC_Collect runs on prior-op cyclic
        # garbage BEFORE this op's after-drain can fire (same reason blueprints_iface_write drains before+after
        # for CompileBlueprint). Drain first so the heap is clean going into this op's mid-op save-GC.
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
        resp = send_command("execute_python", {"code": _wrap(code)})
        # ROOT-CAUSE FIX (2026-08-18): StateTree ops leave cyclic garbage in the editor's PYTHON heap; when a
        # UE GC fires between bridge calls it runs Python's PyGC_Collect via FPythonScriptPlugin::OnPreGarbageCollect
        # and AVs (reading 0x...7383) traversing that garbage. Draining Python's cyclic garbage right after each
        # op — as its OWN call, so the op's exec frame has unwound and the garbage is unreferenced — keeps that
        # heap clean so the next UE GC has nothing bad to traverse. (Localized via in-handler forced-GC C++ stack;
        # proven via the co_stbisect gc.collect-between-ops bisection: full flow 9/9 survives.)
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

    # ================= LEAN WRITER PATH (crash #2 fix, 2026-08-18) =================
    # The residual PyGC access-violation that survived every gc-drain strategy is triggered by the heavy
    # _ST_HELPERS block (25 nested `def`s => a Python-heap footprint) combined with a C++-reflection handler
    # call + save. A FULLY-INLINE snippet with ZERO `def`s is stable (soak-proven 20/20). So the C++-handler
    # WRITERS route through this lean path (no _wrap log-capture, no _ST_HELPERS); the 6 READERS keep _query/
    # _ST_HELPERS (read-only, never crashed). Root-cause writeup: [[statetree-authoring-recipe]].
    def _lean_query(code):
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
        resp = send_command("execute_python", {"code": code})
        try:
            send_command("execute_python", {"code": "import gc\ngc.collect()"})
        except Exception:
            pass
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _lean_exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _lean_query(header + body)

    def _gwrite(cfg):
        return _lean_exec(_GENERIC_WRITE_BODY, cfg)

    # ONE data-driven, fully-inline (no `def`) snippet handling every simple StateTree C++-handler WRITER:
    # load asset -> ScopedEditorTransaction(handler(_st, *_args)) -> on success repair_state_tree_nodes(_st) +
    # save_packages + append a ledger entry (_ledger_op + _ledger_extra{input fields} + _ledger_from_result[])
    # -> print success payload (_result_keys from result + _echo extras). The repair call is what makes the
    # persisted tree well-formed (crash #1 fix); the inline form is what avoids crash #2.
    _GENERIC_WRITE_BODY = r'''
import unreal, json, builtins
_p = PARAMS
_st = unreal.EditorAssetLibrary.load_asset(_p["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, _p["_handler"], None) if _m is not None else None
if _st is None or not isinstance(_st, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % _p["asset_path"]}))
elif _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler %s unavailable on unreal.MCPReflectionLibrary" % _p["_handler"]}))
else:
    with unreal.ScopedEditorTransaction(_p.get("_txn", "MCP statetree write")):
        _res = json.loads(_fn(_st, *_p["_args"]))
    if _res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": _res.get("error") or _res.get("message") or "handler error", "handler": _res}))
    else:
        if hasattr(_m, "repair_state_tree_nodes"):
            try:
                _m.repair_state_tree_nodes(_st)
            except Exception:
                pass
        _saved = bool(unreal.EditorLoadingAndSavingUtils.save_packages([_st.get_outermost()], False))
        _root = getattr(builtins, "_UMCP_LEDGERS", None)
        if _root is None:
            _root = {}
            builtins._UMCP_LEDGERS = _root
        _sid = _p.get("_session", "default")
        if _sid not in _root:
            _root[_sid] = []
        _led = _root[_sid]
        _entry = {"op": _p["_ledger_op"], "asset_path": _p["asset_path"]}
        _ex = _p.get("_ledger_extra", {})
        for _k in _ex:
            _entry[_k] = _ex[_k]
        for _k in _p.get("_ledger_from_result", []):
            _entry[_k] = _res.get(_k)
        _led.append(_entry)
        _out = {"status": "success", "asset_path": _p["asset_path"], "saved": bool(_saved), "ledger_depth": len(_led)}
        for _k in _p.get("_result_keys", []):
            _out[_k] = _res.get(_k)
        _ec = _p.get("_echo", {})
        for _k in _ec:
            _out[_k] = _ec[_k]
        print("@@UMCP@@" + json.dumps(_out))
'''

    # Lean read: a StateTree's root parameters via the C++ handler (a heavy _ST_HELPERS reader that ALSO calls a
    # C++ reflection handler is a crash-#2 trigger, same as the writers — so this read is lean too).
    _GET_PARAMS_LEAN = r'''
import unreal, json
_st = unreal.EditorAssetLibrary.load_asset(PARAMS["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, "get_state_tree_parameters_json", None) if _m is not None else None
if _st is None or not isinstance(_st, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % PARAMS["asset_path"]}))
elif _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler get_state_tree_parameters_json unavailable"}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(_fn(_st))))
'''

    # Lean read (C++ #21): valid binding SOURCES for a target struct (GetBindableStructs).
    _BINDING_SOURCES_LEAN = r'''
import unreal, json
_st = unreal.EditorAssetLibrary.load_asset(PARAMS["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, "get_state_tree_binding_sources_json", None) if _m is not None else None
if _st is None or not isinstance(_st, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % PARAMS["asset_path"]}))
elif _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler get_state_tree_binding_sources_json unavailable"}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(_fn(_st, PARAMS["target_struct_id"]))))
'''

    # Lean read: dump a StateTree's editor bindings (client parses to capture a binding's source before remove).
    _READ_BINDINGS_LEAN = r'''
import unreal, json
_st = unreal.EditorAssetLibrary.load_asset(PARAMS["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, "get_state_tree_bindings_json", None) if _m is not None else None
if _st is None or _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "bindings": []}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(_fn(_st))))
'''

    # Lean component-tree writer: assign/clear a Blueprint UStateTreeComponent's FStateTreeReference. Saves the
    # BLUEPRINT (not a StateTree), so no repair is needed. Inline (loop, no `def`) to stay under the crash-#2 footprint.
    _SET_COMP_TREE_LEAN = r'''
import unreal, json, builtins
_p = PARAMS
_bp = unreal.EditorAssetLibrary.load_asset(_p["blueprint_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, "set_state_tree_component_tree_json", None) if _m is not None else None
if _bp is None or not isinstance(_bp, unreal.Blueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % _p["blueprint_path"]}))
elif _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_component_tree_json unavailable"}))
else:
    _cn = _p.get("component_name") or ""
    _subsys = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    _handles = _subsys.k2_gather_subobject_data_for_blueprint(_bp)
    _comp = None
    _names = []
    for _h in _handles:
        try:
            _data = _subsys.k2_find_subobject_data_from_handle(_h)
            _obj = unreal.SubobjectDataBlueprintFunctionLibrary.get_object(_data)
        except Exception:
            _obj = None
        if _obj is None or not isinstance(_obj, unreal.StateTreeComponent):
            continue
        _names.append(_obj.get_name())
        if _cn:
            if _cn.lower() in _obj.get_name().lower():
                _comp = _obj
                break
        else:
            _comp = _obj
            break
    if _comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no StateTreeComponent in %s; found: %s" % (_p["blueprint_path"], _names)}))
    else:
        _tp = _p.get("statetree_path") or ""
        _tree = None
        if _tp:
            _tree = unreal.EditorAssetLibrary.load_asset(_tp)
            if _tree is None or not isinstance(_tree, unreal.StateTree):
                _tree = "ERR"
        if _tree == "ERR":
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "statetree_path not a StateTree: %s" % _tp}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_statetree_component_tree"):
                _res = json.loads(_fn(_comp, _p.get("property_name") or "", (_tree if _tree is not None else None), ""))
            if _res.get("status") != "success":
                print("@@UMCP@@" + json.dumps({"status": "error", "message": _res.get("error") or _res.get("message") or "handler error", "handler": _res}))
            else:
                try:
                    unreal.BlueprintEditorLibrary.compile_blueprint(_bp)
                except Exception:
                    pass
                _saved = bool(unreal.EditorLoadingAndSavingUtils.save_packages([_bp.get_outermost()], False))
                _root = getattr(builtins, "_UMCP_LEDGERS", None)
                if _root is None:
                    _root = {}
                    builtins._UMCP_LEDGERS = _root
                _sid = _p.get("_session", "default")
                if _sid not in _root:
                    _root[_sid] = []
                _led = _root[_sid]
                _led.append({"op": "st_set_component_tree", "blueprint_path": _p["blueprint_path"],
                    "component_name": _comp.get_name(), "property_name": _res.get("property"),
                    "prev_state_tree": _res.get("prev_state_tree")})
                print("@@UMCP@@" + json.dumps({"status": "success", "blueprint_path": _p["blueprint_path"],
                    "component": _comp.get_name(), "property": _res.get("property"),
                    "statetree": _res.get("state_tree"), "prev_state_tree": _res.get("prev_state_tree"),
                    "saved": bool(_saved), "ledger_depth": len(_led)}))
'''

    # Lean compile: repair first (so the compiler sees well-formed nodes), compile, then save the compiled data.
    # NOT ledgered (compile regenerates data, not a reversible edit).
    _COMPILE_LEAN = r'''
import unreal, json
_st = unreal.EditorAssetLibrary.load_asset(PARAMS["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
_fn = getattr(_m, "compile_state_tree", None) if _m is not None else None
if _st is None or not isinstance(_st, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % PARAMS["asset_path"]}))
elif _fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler compile_state_tree unavailable"}))
else:
    if hasattr(_m, "repair_state_tree_nodes"):
        try:
            _m.repair_state_tree_nodes(_st)
        except Exception:
            pass
    with unreal.ScopedEditorTransaction("MCP compile_statetree"):
        _res = json.loads(_fn(_st))
    if _res.get("compiled"):
        try:
            unreal.EditorLoadingAndSavingUtils.save_packages([_st.get_outermost()], False)
        except Exception:
            pass
    print("@@UMCP@@" + json.dumps(_res))
'''

    # ---- Unreal-side shared helpers (prepended to every READER body; no ''' / no backslash) ----
    _ST_HELPERS = r'''
import unreal, json, builtins
Q = chr(34)
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
    # asset VALIDATION, which hard-crashes (AV) on MCP-written StateTrees. save_packages persists the
    # package identically WITHOUT the validation pass. (Root-caused 2026-08-18: save-validation = the crash.)
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
def _iter_states(ed):
    out = []
    def rec(s, path):
        out.append((s, path))
        i = 0
        for c in list(s.get_editor_property("children") or []):
            if c is not None:
                rec(c, path + "/" + str(c.get_editor_property("name")))
            i += 1
    j = 0
    for r in list(ed.get_editor_property("sub_trees") or []):
        if r is not None:
            rec(r, str(r.get_editor_property("name")))
        j += 1
    return out
def _find_state(ed, name):
    if not name:
        return None
    for s, _p in _iter_states(ed):
        try:
            if str(s.get_editor_property("name")) == name:
                return s
        except Exception:
            pass
    return None
def _owner_of(ed, kind, state_name):
    scope, prop = KIND_ARRAY[kind]
    if scope == "ed":
        return ed, prop
    return _find_state(ed, state_name), prop
def _node_text(en):
    try:
        n = en.get_editor_property("node")
        return n.export_text() if n is not None else ""
    except Exception:
        return ""
def _node_type(en):
    t = _node_text(en)
    head = t.split("(", 1)[0].strip()
    if head.startswith("/Script/"):
        return head.split(".")[-1], head
    try:
        io = en.get_editor_property("instance_object")
        if io is not None:
            return io.get_class().get_name(), None
    except Exception:
        pass
    return (head or None), (head if head.startswith("/Script/") else None)
def _en_id(en):
    try:
        return en.get_editor_property("id").export_text()
    except Exception:
        return ""
def _norm_guid(g):
    return "".join([c for c in str(g or "") if c not in "-{} "]).upper()
def _split_top(body):
    segs = []; depth = 0; inq = False; cur = []
    for ch in body:
        if ch == Q:
            inq = not inq; cur.append(ch); continue
        if not inq:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                segs.append("".join(cur)); cur = []; continue
        cur.append(ch)
    if cur:
        segs.append("".join(cur))
    return segs
def _fields_of(body):
    out = []
    for s in _split_top(body):
        eq = s.find("=")
        if eq > 0:
            out.append((s[:eq].strip(), s[eq+1:]))
    return out
def _set_field(body, field, newval):
    outsegs = []; done = False
    for s in _split_top(body):
        eq = s.find("=")
        if eq > 0 and s[:eq].strip() == field:
            prior = s[eq+1:]; nv = newval
            if prior.startswith(Q) and not nv.startswith(Q):
                nv = Q + nv + Q
            outsegs.append(field + "=" + nv); done = True
        else:
            outsegs.append(s)
    return ",".join(outsegs), done
def _node_body(node_text):
    i = node_text.find("(")
    if i < 0:
        return None, None, None
    return node_text[:i], node_text[i+1:node_text.rfind(")")], node_text.rfind(")")
def _enum_dump(e):
    vals = []
    if e is None:
        return vals
    for a in dir(e):
        if a[:1].isalpha() and a.upper() == a:
            try:
                m = getattr(e, a)
                vals.append({"name": a, "value": int(m.value)})
            except Exception:
                pass
    vals.sort(key=lambda r: r["value"])
    return vals
def _find_bp_component(bp, comp_name):
    subsys = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    handles = subsys.k2_gather_subobject_data_for_blueprint(bp)
    found = None; names = []
    for h in handles:
        try:
            data = subsys.k2_find_subobject_data_from_handle(h)
            obj = unreal.SubobjectDataBlueprintFunctionLibrary.get_object(data)
        except Exception:
            obj = None
        if obj is None or not isinstance(obj, unreal.StateTreeComponent):
            continue
        names.append(obj.get_name())
        if comp_name:
            if comp_name.lower() in obj.get_name().lower():
                found = obj; break
        else:
            found = obj; break
    return found, names
def _refl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _cpp(fname):
    m = _refl()
    return getattr(m, fname, None) if m is not None else None
'''

    # ============================ READERS (6, no ledger) ============================ #

    # ---- get_statetree_node (by GUID) ---------------------------------------------- #
    _GET_NODE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; guid = _norm_guid(PARAMS.get("guid"))
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif not guid:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no guid given"}))
else:
    ed = _find_ed(st)
    hits = []
    # editor nodes on every state (tasks/conditions/considerations) + editor-data (evaluators/global_tasks)
    def scan_nodes(owner, scope, state_name):
        for kind, (sc, prop) in KIND_ARRAY.items():
            if sc != scope:
                continue
            arr = list(owner.get_editor_property(prop) or [])
            for i, en in enumerate(arr):
                if _norm_guid(_en_id(en)) == guid:
                    tn, tp = _node_type(en)
                    hits.append({"match": "node", "kind": kind, "state": state_name,
                                 "index": i, "node_type": tn, "type_path": tp,
                                 "id": _norm_guid(_en_id(en)), "config": _node_text(en)[:600]})
    if ed is not None:
        scan_nodes(ed, "ed", None)
        for s, path in _iter_states(ed):
            sname = str(s.get_editor_property("name"))
            scan_nodes(s, "state", sname)
            # state itself
            try:
                if _norm_guid(s.get_editor_property("id").export_text()) == guid:
                    hits.append({"match": "state", "state": sname, "path": path,
                                 "type": str(s.get_editor_property("type"))})
            except Exception:
                pass
            # transitions
            for i, tr in enumerate(list(s.get_editor_property("transitions") or [])):
                try:
                    if _norm_guid(tr.get_editor_property("id").export_text()) == guid:
                        hits.append({"match": "transition", "state": sname, "index": i,
                                     "config": tr.export_text()[:600]})
                except Exception:
                    pass
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "guid": guid,
        "match_count": len(hits), "matches": hits,
        "note": "Searched editor-node ids (tasks/conditions/considerations/evaluators/global_tasks), state ids, and transition ids. GUID compared normalized (dashes/braces stripped, upper)."}))
'''

    # ---- get_statetree_full_info --------------------------------------------------- #
    _FULL_INFO_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; verbosity = int(PARAMS.get("verbosity") or 1); cfg = 600 if verbosity >= 2 else 160
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _find_ed(st)
    info = {"status": "success", "asset_path": st.get_path_name(), "class": st.get_class().get_name()}
    if ed is None:
        info["editor_data"] = None
    else:
        info["editor_data_class"] = ed.get_class().get_name()
        sch = ed.get_editor_property("schema")
        info["schema"] = sch.get_class().get_name() if sch is not None else None
        # evaluators + global tasks
        for prop in ("evaluators", "global_tasks"):
            arr = list(ed.get_editor_property(prop) or [])
            info[prop] = [{"index": i, "node_type": _node_type(en)[0], "id": _norm_guid(_en_id(en)),
                           "config": (_node_text(en)[:cfg] if verbosity >= 2 else None)} for i, en in enumerate(arr)]
        # color palette
        pal = []
        try:
            for c in ed.get_editor_property("colors"):
                t = c.export_text(); k = t.find("ID="); e2 = t.find(")", k)
                pal.append({"display_name": str(c.get_editor_property("display_name")),
                            "id": (t[k+3:e2] if k >= 0 else None)})
        except Exception:
            pass
        info["colors"] = pal
        # state hierarchy
        def sdesc(s):
            d = {"name": str(s.get_editor_property("name")),
                 "type": str(s.get_editor_property("type")).split(".")[-1].split(":")[0].strip(),
                 "id": _norm_guid(s.get_editor_property("id").export_text()),
                 "tasks": [_node_type(x)[0] for x in list(s.get_editor_property("tasks") or [])],
                 "enter_condition_count": len(list(s.get_editor_property("enter_conditions") or [])),
                 "consideration_count": len(list(s.get_editor_property("considerations") or [])),
                 "transition_count": len(list(s.get_editor_property("transitions") or []))}
            kids = [c for c in list(s.get_editor_property("children") or []) if c is not None]
            if kids:
                d["children"] = [sdesc(c) for c in kids]
            return d
        roots = [sdesc(r) for r in list(ed.get_editor_property("sub_trees") or []) if r is not None]
        info["states"] = roots
        info["total_state_count"] = len(_iter_states(ed))
    # bindings are fetched SEPARATELY by the tool via a lean read (a C++ reflection handler call inside this
    # heavy _ST_HELPERS body is a crash-#2 trigger); leave a placeholder the tool overwrites.
    info["bindings"] = {"note": "merged by tool via lean read"}
    info["parameters_note"] = "Root parameters via get_statetree_parameters (lean C++ read)."
    print("@@UMCP@@" + json.dumps(info))
'''

    # ---- search_statetree_nodes ---------------------------------------------------- #
    _SEARCH_NODES_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; query = (PARAMS.get("query") or "").lower()
category = (PARAMS.get("category") or "all").lower()
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _find_ed(st); hits = []
    kinds = list(KIND_ARRAY.keys()) if category == "all" else [category]
    if any(k not in KIND_ARRAY for k in kinds):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad category '%s' (task|condition|consideration|evaluator|global_task|all)" % category}))
    else:
        def scan(owner, scope, sname):
            for kind in kinds:
                sc, prop = KIND_ARRAY[kind]
                if sc != scope:
                    continue
                for i, en in enumerate(list(owner.get_editor_property(prop) or [])):
                    tn, tp = _node_type(en)
                    hay = ((tn or "") + " " + (_node_text(en) or "")).lower()
                    if not query or query in hay:
                        hits.append({"kind": kind, "state": sname, "index": i,
                                     "node_type": tn, "type_path": tp, "id": _norm_guid(_en_id(en))})
        if ed is not None:
            scan(ed, "ed", None)
            for s, _p in _iter_states(ed):
                scan(s, "state", str(s.get_editor_property("name")))
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "query": query,
            "category": category, "match_count": len(hits), "matches": hits}))
'''

    # ---- search_statetree_properties ----------------------------------------------- #
    _SEARCH_PROPS_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; pq = (PARAMS.get("property_query") or "").lower()
vq = (PARAMS.get("value_query") or "").lower()
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _find_ed(st); hits = []
    def scan(owner, scope, sname):
        for kind, (sc, prop) in KIND_ARRAY.items():
            if sc != scope:
                continue
            for i, en in enumerate(list(owner.get_editor_property(prop) or [])):
                tn, _tp = _node_type(en)
                _pre, body, _e = _node_body(_node_text(en))
                if body is None:
                    continue
                for fname, fval in _fields_of(body):
                    if pq and pq not in fname.lower():
                        continue
                    if vq and vq not in str(fval).lower():
                        continue
                    hits.append({"kind": kind, "state": sname, "index": i, "node_type": tn,
                                 "property": fname, "value": str(fval)[:200]})
    if ed is not None:
        scan(ed, "ed", None)
        for s, _p in _iter_states(ed):
            scan(s, "state", str(s.get_editor_property("name")))
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "property_query": pq, "value_query": vq, "match_count": len(hits), "matches": hits[:400],
        "note": "Properties + configured VALUES parsed from each node FInstancedStruct export_text (top-level fields). Schema field-lists would need MCPReflectionLibrary.get_struct_fields_json with the inner UScriptStruct (not reachable from an InstancedStruct in Python); export_text gives the real configured values instead."}))
'''

    # ---- list_statetree_transition_targets ----------------------------------------- #
    _TRANS_TARGETS_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]
st = _load_st(asset_path)
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
else:
    ed = _find_ed(st); targets = []
    if ed is not None:
        for s, path in _iter_states(ed):
            targets.append({"name": str(s.get_editor_property("name")), "path": path,
                            "id": _norm_guid(s.get_editor_property("id").export_text()),
                            "type": str(s.get_editor_property("type")).split(".")[-1].split(":")[0].strip()})
    lt = getattr(unreal, "StateTreeTransitionType", None)
    link_types = _enum_dump(lt) if lt is not None else [
        {"name": "NONE", "value": 0}, {"name": "NEXT_STATE", "value": 1},
        {"name": "NEXT_SELECTABLE_STATE", "value": 2}, {"name": "GOTO_STATE", "value": 3},
        {"name": "SUCCEEDED", "value": 4}, {"name": "FAILED", "value": 5}]
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
        "state_count": len(targets), "states": targets, "link_types": link_types,
        "note": "GOTO_STATE transitions target a named state (use name+id); NEXT_STATE/NEXT_SELECTABLE_STATE/SUCCEEDED/FAILED/NONE need no explicit target."}))
'''

    # ---- list_statetree_enum_values ------------------------------------------------ #
    _ENUM_VALUES_BODY = _ST_HELPERS + r'''
category = (PARAMS.get("category") or "all").lower()
CAT = {"trigger": "StateTreeTransitionTrigger", "priority": "StateTreeTransitionPriority",
       "selection_behavior": "StateTreeStateSelectionBehavior", "state_type": "StateTreeStateType",
       "link_type": "StateTreeTransitionType"}
if category != "all" and category not in CAT:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "bad category '%s' (want %s|all)" % (category, "|".join(sorted(CAT)))}))
else:
    cats = list(CAT.keys()) if category == "all" else [category]
    out = {}
    for c in cats:
        e = getattr(unreal, CAT[c], None)
        out[c] = {"enum": CAT[c], "values": _enum_dump(e), "available": e is not None}
    print("@@UMCP@@" + json.dumps({"status": "success", "category": category, "enums": out}))
'''

    # ============================ WRITERS (4, reversible) ============================ #

    # ---- set_statetree_node_property ----------------------------------------------- #
    _SET_NODE_PROP_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; kind = PARAMS["kind"]; index = int(PARAMS["index"])
state_name = PARAMS.get("state_name") or ""; prop = PARAMS["property"]; value = str(PARAMS["value"])
container = PARAMS.get("container") or ""
st = _load_st(asset_path); fn = _cpp("set_state_tree_node_property_json")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_node_property_json unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP set_statetree_node_property"):
        raw = fn(st, state_name, kind, index, prop, value, container)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_set_node_property", "asset_path": asset_path, "state_name": state_name,
            "kind": kind, "index": index, "container": res.get("container"), "prop": prop, "prev": res.get("prev")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "kind": kind,
            "state": res.get("state"), "index": index, "property": prop, "container": res.get("container"),
            "prev": res.get("prev"), "value": value, "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- set_statetree_transition_property ----------------------------------------- #
    _SET_TRANS_PROP_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; state_name = PARAMS["state_name"]; index = int(PARAMS["index"])
prop = PARAMS["property"]; value = str(PARAMS["value"])
st = _load_st(asset_path); fn = _cpp("set_state_tree_transition_property_json")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_transition_property_json unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP set_statetree_transition_property"):
        raw = fn(st, state_name, index, prop, value)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_set_transition_property", "asset_path": asset_path,
            "state_name": state_name, "index": index, "prop": prop, "prev": res.get("prev")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "state": state_name,
            "index": index, "property": prop, "prev": res.get("prev"), "value": value,
            "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- set_statetree_component_tree ---------------------------------------------- #
    _SET_COMP_TREE_BODY = _ST_HELPERS + r'''
bp_path = PARAMS["blueprint_path"]; tree_path = PARAMS.get("statetree_path") or ""
comp_name = PARAMS.get("component_name") or ""; prop_name = PARAMS.get("property_name") or ""
bp = unreal.EditorAssetLibrary.load_asset(bp_path); fn = _cpp("set_state_tree_component_tree_json")
if bp is None or not isinstance(bp, unreal.Blueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a Blueprint: %s" % bp_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_component_tree_json unavailable on unreal.MCPReflectionLibrary"}))
else:
    comp, names = _find_bp_component(bp, comp_name)
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no StateTreeComponent%s in %s; found components: %s" % ((" named '%s'" % comp_name if comp_name else ""), bp_path, names)}))
    else:
        tree = None
        if tree_path:
            tree = unreal.EditorAssetLibrary.load_asset(tree_path)
            if tree is None or not isinstance(tree, unreal.StateTree):
                tree = "ERR"
        if tree == "ERR":
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "statetree_path not a StateTree: %s" % tree_path}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_statetree_component_tree"):
                raw = fn(comp, prop_name, (tree if tree is not None else None), "")
            res = json.loads(raw)
            if res.get("status") != "success":
                print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
            else:
                try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
                except Exception: pass
                saved = _st_save(bp_path)
                _ledger().append({"op": "st_set_component_tree", "blueprint_path": bp_path,
                    "component_name": comp.get_name(), "property_name": res.get("property"),
                    "prev_state_tree": res.get("prev_state_tree")})
                print("@@UMCP@@" + json.dumps({"status": "success", "blueprint_path": bp_path,
                    "component": comp.get_name(), "property": res.get("property"),
                    "statetree": res.get("state_tree"), "prev_state_tree": res.get("prev_state_tree"),
                    "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- set_statetree_color ------------------------------------------------------- #
    _SET_COLOR_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; state_name = PARAMS["state_name"]; color = str(PARAMS.get("color") or "").strip()
st = _load_st(asset_path); fn = _cpp("set_state_tree_color_json")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_color_json unavailable on unreal.MCPReflectionLibrary"}))
else:
    ZERO = "00000000-0000-0000-0000-000000000000"
    ng = _norm_guid(color); color_name = ""; color_guid = ""
    if color.lower() in ("default", "default color", "none", ""):
        color_guid = ZERO
    elif len(ng) == 32 and all(ch in "0123456789ABCDEF" for ch in ng):
        color_guid = color
    else:
        color_name = color
    with unreal.ScopedEditorTransaction("MCP set_statetree_color"):
        raw = fn(st, state_name, color_name, color_guid)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_set_color", "asset_path": asset_path, "state_name": state_name,
            "prev": res.get("prev")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "state": state_name,
            "color": color, "color_name": res.get("color_name"), "color_guid": res.get("color_guid"),
            "prev": res.get("prev"), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ============================ NEW C++ TOOLS (params/bindings/compile) ============================ #

    # ---- get_statetree_parameters (read) ------------------------------------------- #
    _GET_PARAMS_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]
st = _load_st(asset_path); fn = _cpp("get_state_tree_parameters_json")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler get_state_tree_parameters_json unavailable on unreal.MCPReflectionLibrary"}))
else:
    print("@@UMCP@@" + json.dumps(json.loads(fn(st))))
'''

    # ---- add_statetree_parameter (write) ------------------------------------------- #
    _ADD_PARAM_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; name = PARAMS["name"]; type_name = PARAMS["type_name"]
st = _load_st(asset_path); fn = _cpp("add_state_tree_parameter")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler add_state_tree_parameter unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP add_statetree_parameter"):
        raw = fn(st, name, type_name)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_add_parameter", "asset_path": asset_path, "name": name})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "name": name,
            "type": res.get("type"), "count": res.get("count"), "saved": bool(saved),
            "ledger_depth": len(_ledger())}))
'''

    # ---- set_statetree_parameter (write) ------------------------------------------- #
    _SET_PARAM_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; name = PARAMS["name"]; value = str(PARAMS["value"])
st = _load_st(asset_path); fn = _cpp("set_state_tree_parameter")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler set_state_tree_parameter unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP set_statetree_parameter"):
        raw = fn(st, name, value)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_set_parameter", "asset_path": asset_path, "name": name, "prev": res.get("prev")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "name": name,
            "value": value, "prev": res.get("prev"), "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- remove_statetree_parameter (write) ---------------------------------------- #
    _REMOVE_PARAM_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; name = PARAMS["name"]
st = _load_st(asset_path); fn = _cpp("remove_state_tree_parameter")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler remove_state_tree_parameter unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP remove_statetree_parameter"):
        raw = fn(st, name)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_remove_parameter", "asset_path": asset_path, "name": name,
            "type": res.get("type"), "value": res.get("value")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path, "name": name,
            "type": res.get("type"), "value": res.get("value"), "count": res.get("count"),
            "saved": bool(saved), "ledger_depth": len(_ledger()),
            "inverse_note": "faithful re-add for scalar types; struct/enum/object type-objects not round-tripped"}))
'''

    # ---- add_statetree_binding (write) --------------------------------------------- #
    _ADD_BINDING_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; src_id = PARAMS["source_struct_id"]; src_path = PARAMS["source_path"]
tgt_id = PARAMS["target_struct_id"]; tgt_path = PARAMS["target_path"]
st = _load_st(asset_path); fn = _cpp("add_state_tree_binding")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler add_state_tree_binding unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP add_statetree_binding"):
        raw = fn(st, src_id, src_path, tgt_id, tgt_path)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_add_binding", "asset_path": asset_path,
            "target_struct_id": res.get("target_struct_id"), "target_property": res.get("target_property")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
            "source_struct_id": res.get("source_struct_id"), "source_property": res.get("source_property"),
            "target_struct_id": res.get("target_struct_id"), "target_property": res.get("target_property"),
            "binding_count": res.get("binding_count"), "replaced_existing": res.get("replaced_existing"),
            "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- remove_statetree_binding (write) ------------------------------------------ #
    _REMOVE_BINDING_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]; tgt_id = PARAMS["target_struct_id"]; tgt_path = PARAMS["target_path"]
st = _load_st(asset_path); fn = _cpp("remove_state_tree_binding")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler remove_state_tree_binding unavailable on unreal.MCPReflectionLibrary"}))
else:
    # capture the source of the target binding BEFORE removal so the inverse can re-add it
    src_id = ""; src_path = ""
    rd = _cpp("get_state_tree_bindings_json")
    if rd is not None:
        try:
            bd = json.loads(rd(st)); tnorm = _norm_guid(tgt_id)
            for b in (bd.get("bindings") or []):
                if _norm_guid(b.get("target_struct_id")) == tnorm and (b.get("target_property") or "") == tgt_path:
                    src_id = b.get("source_struct_id") or ""; src_path = b.get("source_property") or ""; break
        except Exception:
            pass
    with unreal.ScopedEditorTransaction("MCP remove_statetree_binding"):
        raw = fn(st, tgt_id, tgt_path)
    res = json.loads(raw)
    if res.get("status") != "success":
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error") or res.get("message") or "handler error", "handler": res}))
    else:
        saved = _st_save(asset_path)
        _ledger().append({"op": "st_remove_binding", "asset_path": asset_path,
            "source_struct_id": src_id, "source_property": src_path,
            "target_struct_id": res.get("target_struct_id"), "target_property": res.get("target_property")})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": asset_path,
            "target_struct_id": res.get("target_struct_id"), "target_property": res.get("target_property"),
            "removed": res.get("removed"), "binding_count": res.get("binding_count"),
            "captured_source_struct_id": src_id, "captured_source_property": src_path,
            "saved": bool(saved), "ledger_depth": len(_ledger())}))
'''

    # ---- compile_statetree (build step; not ledgered) ------------------------------ #
    _COMPILE_BODY = _ST_HELPERS + r'''
asset_path = PARAMS["asset_path"]
st = _load_st(asset_path); fn = _cpp("compile_state_tree")
if st is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a StateTree: %s" % asset_path}))
elif fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler compile_state_tree unavailable on unreal.MCPReflectionLibrary"}))
else:
    with unreal.ScopedEditorTransaction("MCP compile_statetree"):
        res = json.loads(fn(st))
    if res.get("compiled"):
        _st_save(asset_path)
    print("@@UMCP@@" + json.dumps(res))
'''

    # ============================ MCP TOOL WRAPPERS ============================ #

    @mcp.tool()
    def get_statetree_node(ctx, asset_path: str, guid: str) -> str:
        """Find a StateTree node/state/transition by GUID. Read-only.

        Searches editor-node ids (tasks/enter-conditions/considerations/evaluators/global-tasks), state ids,
        and transition ids. GUID matched normalized (dashes/braces stripped, uppercased). Returns matches[]
        with {match, kind, state, index, node_type, type_path, id, config}."""
        try:
            return json.dumps(_exec(_GET_NODE_BODY, {"asset_path": asset_path, "guid": guid}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_statetree_full_info(ctx, asset_path: str, verbosity: int = 1) -> str:
        """Aggregate one StateTree's full info: schema, evaluators/global-tasks, color palette, the recursive
        state hierarchy (name/type/id/task-types/counts), total_state_count, and editor property bindings
        (via C++ #14 reader if present). Read-only.

        verbosity: 1 = summary (default); 2 = include per-node config export text. Root parameters
        (property bag) are Python-filtered -> reported as DEFERRED-C++."""
        try:
            info = _exec(_FULL_INFO_BODY, {"asset_path": asset_path, "verbosity": verbosity})
            # merge bindings via a LEAN C++ read (kept out of the heavy walk body to avoid crash #2)
            if isinstance(info, dict):
                try:
                    bd = _lean_exec(_READ_BINDINGS_LEAN, {"asset_path": asset_path})
                    if isinstance(bd, dict):
                        info["bindings"] = bd.get("bindings", bd)
                except Exception as _e:
                    info["bindings"] = {"error": str(_e)[:120]}
            return json.dumps(info, indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def search_statetree_nodes(ctx, asset_path: str, query: str = "", category: str = "all") -> str:
        """Search nodes across a StateTree by type/config substring. Read-only.

        query:    case-insensitive substring matched against node type name + export text (empty = all).
        category: task|condition|consideration|evaluator|global_task|all (default all).
        Returns matches[] {kind, state, index, node_type, type_path, id}."""
        try:
            return json.dumps(_exec(_SEARCH_NODES_BODY, {"asset_path": asset_path, "query": query,
                "category": category}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def search_statetree_properties(ctx, asset_path: str, property_query: str = "",
                                    value_query: str = "") -> str:
        """Search node CONFIGURED properties (name/value) across a StateTree. Read-only.

        property_query: case-insensitive substring on the field name (empty = all fields).
        value_query:    case-insensitive substring on the field value (empty = any).
        Fields + values are parsed from each node's FInstancedStruct export_text (real configured values).
        Returns matches[] {kind, state, index, node_type, property, value}."""
        try:
            return json.dumps(_exec(_SEARCH_PROPS_BODY, {"asset_path": asset_path,
                "property_query": property_query, "value_query": value_query}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_statetree_transition_targets(ctx, asset_path: str) -> str:
        """List valid transition targets (all states: name/path/id/type) + the transition LinkType enum
        values for a StateTree. Read-only. GOTO_STATE targets a named state; NEXT_STATE / SUCCEEDED /
        FAILED / NONE need no explicit target."""
        try:
            return json.dumps(_exec(_TRANS_TARGETS_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_statetree_enum_values(ctx, category: str = "all") -> str:
        """List StateTree enum values by category. Read-only.

        category: trigger (StateTreeTransitionTrigger) | priority (StateTreeTransitionPriority) |
        selection_behavior (StateTreeStateSelectionBehavior) | state_type (StateTreeStateType) |
        link_type (StateTreeTransitionType) | all (default). Each entry: {name, value}."""
        try:
            return json.dumps(_exec(_ENUM_VALUES_BODY, {"category": category}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_statetree_node_property(ctx, asset_path: str, kind: str, index: int, property: str,
                                    value: str, state_name: str = "", container: str = "") -> str:
        """Set ONE configured property on a native StateTree node via the C++ #18 reflection handler
        (FProperty ImportText on the reflected node struct; the Python nested-struct import that crashed the
        interpreter is GONE). Reversible.

        kind:       task|condition|consideration|single_task (need state_name) | evaluator|global_task (tree-level).
        index:      node index within that array (see search_statetree_nodes / get_statetree_full_info).
        property:   the node's property name (FProperty), e.g. Text, FontScale, bEnabled.
        value:      export-text token for the property (numbers/enums raw, strings as the property expects).
        container:  auto (default) | node | instance | instance_object — which sub-struct of the editor node
                    holds the property; auto tries node->instance->instance_object.
        Undo op st_set_node_property re-calls the handler with the captured prior value on the same container."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "set_state_tree_node_property_json",
                "_args": [state_name, kind, int(index), property, str(value), container],
                "_txn": "MCP set_statetree_node_property", "_ledger_op": "st_set_node_property",
                "_ledger_extra": {"state_name": state_name, "kind": kind, "index": int(index), "prop": property},
                "_ledger_from_result": ["container", "prev"],
                "_result_keys": ["state", "container", "prev"],
                "_echo": {"kind": kind, "index": int(index), "property": property, "value": str(value)}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_statetree_transition_property(ctx, asset_path: str, state_name: str, index: int,
                                          property: str, value: str) -> str:
        """Set ONE property on a state's transition via the C++ #18 handler (FProperty ImportText on
        FStateTreeTransition). Reversible.

        property: a FStateTreeTransition property, e.g. Trigger (OnStateCompleted|OnTick|...),
                  Priority (Low|Normal|Medium|High|Critical). value = export-text token.
        Undo op st_set_transition_property re-calls the handler with the captured prior value."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "set_state_tree_transition_property_json",
                "_args": [state_name, int(index), property, str(value)],
                "_txn": "MCP set_statetree_transition_property", "_ledger_op": "st_set_transition_property",
                "_ledger_extra": {"state_name": state_name, "index": int(index), "prop": property},
                "_ledger_from_result": ["prev"],
                "_result_keys": ["prev"],
                "_echo": {"state": state_name, "index": int(index), "property": property, "value": str(value)}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_statetree_component_tree(ctx, blueprint_path: str, statetree_path: str = "",
                                     component_name: str = "", property_name: str = "") -> str:
        """Assign (or clear) the StateTree asset on a Blueprint's UStateTreeComponent template via the C++ #18
        handler (locates the FStateTreeReference property by type and repoints it). Reversible.

        blueprint_path: an Actor Blueprint containing a StateTreeComponent (template located via the
                        SubobjectDataSubsystem; the BP is compiled + saved after the C++ edit).
        statetree_path: the StateTree asset to assign; empty string CLEARS the reference.
        component_name: substring to pick a specific component (default: first StateTreeComponent).
        property_name:  optional explicit FStateTreeReference property name (default: first by type).
        Undo op st_set_component_tree re-calls the handler pointing back at the captured prior StateTree."""
        try:
            return json.dumps(_lean_exec(_SET_COMP_TREE_LEAN, {"blueprint_path": blueprint_path,
                "statetree_path": statetree_path, "component_name": component_name,
                "property_name": property_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_statetree_color(ctx, asset_path: str, state_name: str, color: str) -> str:
        """Set a state's editor color by referencing a palette color via the C++ #18 handler. Reversible.

        color: a palette color DisplayName (case-insensitive), or 'default' for the default/no color, or a
               raw 32-hex color GUID. The palette lives on UStateTreeEditorData.Colors; list it via
               get_statetree_full_info. Undo op st_set_color re-calls the handler with the captured prior GUID."""
        try:
            _ng = "".join(c for c in str(color or "") if c not in "-{} ").upper()
            _cname = ""
            _cguid = ""
            if str(color).lower() in ("default", "default color", "none", ""):
                _cguid = "00000000-0000-0000-0000-000000000000"
            elif len(_ng) == 32 and all(ch in "0123456789ABCDEF" for ch in _ng):
                _cguid = color
            else:
                _cname = color
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "set_state_tree_color_json",
                "_args": [state_name, _cname, _cguid],
                "_txn": "MCP set_statetree_color", "_ledger_op": "st_set_color",
                "_ledger_extra": {"state_name": state_name},
                "_ledger_from_result": ["prev"],
                "_result_keys": ["color_name", "color_guid", "prev"],
                "_echo": {"state": state_name, "color": color}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_statetree_parameters(ctx, asset_path: str) -> str:
        """List a StateTree's root parameters (FInstancedPropertyBag) via the C++ #18 reader. Read-only.
        Returns parameters[] {name, type, id, value} + parameters_guid (the source struct id for bindings)."""
        try:
            return json.dumps(_lean_exec(_GET_PARAMS_LEAN, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_statetree_parameter(ctx, asset_path: str, name: str, type_name: str) -> str:
        """Add a root parameter to a StateTree's property bag via the C++ #18 handler. Reversible.

        type_name: bool|byte|int|int64|float|double|name|string|text | vector|vector2|vector4|rotator|quat|
                   transform|linearcolor|guid.
        Undo op st_add_parameter removes the parameter (remove_state_tree_parameter)."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "add_state_tree_parameter",
                "_args": [name, type_name],
                "_txn": "MCP add_statetree_parameter", "_ledger_op": "st_add_parameter",
                "_ledger_extra": {"name": name},
                "_result_keys": ["type", "count"],
                "_echo": {"name": name}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_statetree_parameter(ctx, asset_path: str, name: str, value: str) -> str:
        """Set a root parameter's value (serialized-string) via the C++ #18 handler. Reversible.
        Undo op st_set_parameter restores the captured prior serialized value."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "set_state_tree_parameter",
                "_args": [name, str(value)],
                "_txn": "MCP set_statetree_parameter", "_ledger_op": "st_set_parameter",
                "_ledger_extra": {"name": name},
                "_ledger_from_result": ["prev"],
                "_result_keys": ["prev"],
                "_echo": {"name": name, "value": str(value)}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_statetree_parameter(ctx, asset_path: str, name: str) -> str:
        """Remove a root parameter via the C++ #18 handler (captures its type + value). Reversible for
        scalar types (undo re-adds then sets the value); struct/enum/object type-objects are not round-tripped.
        Undo op st_remove_parameter re-adds the parameter and restores its value."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "remove_state_tree_parameter",
                "_args": [name],
                "_txn": "MCP remove_statetree_parameter", "_ledger_op": "st_remove_parameter",
                "_ledger_extra": {"name": name},
                "_ledger_from_result": ["type", "value"],
                "_result_keys": ["type", "value", "count"],
                "_echo": {"name": name, "inverse_note": "faithful re-add for scalar types; struct/enum/object type-objects not round-tripped"}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_statetree_binding(ctx, asset_path: str, source_struct_id: str, source_path: str,
                              target_struct_id: str, target_path: str) -> str:
        """Add a property binding (source -> target) to a StateTree's editor bindings via the C++ #18 handler.
        Reversible. struct ids are GUIDs (parameters_guid from get_statetree_parameters, or a node/state id).
        Undo op st_add_binding removes the binding at the target path. NOTE: if replaced_existing is true the
        add overwrote a prior binding at that target whose source was not captured -> the undo (a plain remove)
        is lossy in that case; a fresh add is exactly reversible."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "add_state_tree_binding",
                "_args": [source_struct_id, source_path, target_struct_id, target_path],
                "_txn": "MCP add_statetree_binding", "_ledger_op": "st_add_binding",
                "_ledger_from_result": ["target_struct_id", "target_property"],
                "_result_keys": ["source_struct_id", "source_property", "target_struct_id", "target_property",
                                 "binding_count", "replaced_existing"]}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_statetree_binding(ctx, asset_path: str, target_struct_id: str, target_path: str) -> str:
        """Remove the property binding at a target path via the C++ #18 handler. Reversible: the source is
        captured (via the C++ bindings reader) BEFORE removal so the undo can re-add it.
        Undo op st_remove_binding re-adds the captured source->target binding."""
        try:
            # capture the target binding's SOURCE client-side (lean read) so the undo can re-add it
            bd = _lean_exec(_READ_BINDINGS_LEAN, {"asset_path": asset_path})
            src_id = ""
            src_path = ""
            tn = "".join(c for c in str(target_struct_id or "") if c not in "-{} ").upper()
            for b in ((bd.get("bindings") or []) if isinstance(bd, dict) else []):
                bn = "".join(c for c in str(b.get("target_struct_id") or "") if c not in "-{} ").upper()
                if bn == tn and (b.get("target_property") or "") == target_path:
                    src_id = b.get("source_struct_id") or ""
                    src_path = b.get("source_property") or ""
                    break
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "remove_state_tree_binding",
                "_args": [target_struct_id, target_path],
                "_txn": "MCP remove_statetree_binding", "_ledger_op": "st_remove_binding",
                "_ledger_extra": {"source_struct_id": src_id, "source_property": src_path},
                "_ledger_from_result": ["target_struct_id", "target_property"],
                "_result_keys": ["target_struct_id", "target_property", "removed", "binding_count"],
                "_echo": {"captured_source_struct_id": src_id, "captured_source_property": src_path}}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def compile_statetree(ctx, asset_path: str) -> str:
        """Compile a StateTree via the C++ #18 handler (UStateTreeEditingSubsystem::CompileStateTree — the
        canonical headless compile). NOT a reversible edit (regenerates compiled data), so NOT ledgered.
        Returns {compiled, error_count, warning_count, messages[]}."""
        try:
            return json.dumps(_lean_exec(_COMPILE_LEAN, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ==================== C++ #21: the last 3 StateTree features -> 100% ==================== #

    @mcp.tool()
    def get_statetree_binding_sources(ctx, asset_path: str, target_struct_id: str) -> str:
        """List the valid binding SOURCES for a target struct (root/state parameters, evaluators, global tasks,
        preceding tasks, context data...) via the C++ #21 handler (UStateTreeEditorData::GetBindableStructs).
        Read-only. target_struct_id = a node/state/parameters GUID (from get_statetree_parameters / a node id).
        Returns sources[] {name, id, struct} — feed a source's id + a property path into add_statetree_binding."""
        try:
            return json.dumps(_lean_exec(_BINDING_SOURCES_LEAN,
                {"asset_path": asset_path, "target_struct_id": target_struct_id}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def bind_statetree_task_completion(ctx, asset_path: str, source_task_id: str, target_struct_id: str,
                                       target_path: str, condition: str = "completes") -> str:
        """Bind a TASK's completion (a generated delegate) to a target listener path via the C++ #21 handler
        (FStateTreeEditorPropertyBindings::AddTaskCompletionBinding). Reversible.

        source_task_id: the broadcasting task node's GUID (from search_statetree_nodes / get_statetree_full_info).
        target_struct_id + target_path: the listener struct GUID + property path.
        condition: succeeds | fails | completes (default 'completes' = on success OR failure).
        Undo op st_add_task_completion removes the binding at the target."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "add_state_tree_task_completion_binding",
                "_args": [source_task_id, target_struct_id, target_path, condition],
                "_txn": "MCP bind_statetree_task_completion", "_ledger_op": "st_add_task_completion",
                "_ledger_from_result": ["target_struct_id", "target_property"],
                "_result_keys": ["source_task_id", "target_struct_id", "target_property", "condition",
                                 "added", "binding_count"]}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def bind_statetree_delegate(ctx, asset_path: str, dispatcher_struct_id: str, dispatcher_path: str,
                                listener_struct_id: str, listener_path: str) -> str:
        """Bind a delegate DISPATCHER (source) to a LISTENER (target) via the C++ #21 handler (a property binding
        of an FStateTreeDelegateDispatcher-typed source path to an FStateTreeDelegateListener-typed target path).
        Reversible. Undo op st_bind_delegate removes the binding at the listener path."""
        try:
            return json.dumps(_gwrite({
                "asset_path": asset_path, "_handler": "bind_state_tree_delegate",
                "_args": [dispatcher_struct_id, dispatcher_path, listener_struct_id, listener_path],
                "_txn": "MCP bind_statetree_delegate", "_ledger_op": "st_bind_delegate",
                "_ledger_from_result": ["listener_struct_id", "listener_path"],
                "_result_keys": ["dispatcher_struct_id", "dispatcher_path", "listener_struct_id",
                                 "listener_path", "binding_count", "replaced_existing"]}), indent=2)
        except Exception as e:
            return f"Error: {e}"
