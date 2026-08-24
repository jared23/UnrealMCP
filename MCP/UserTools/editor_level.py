"""UserTools :: Editor / Level  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8).
Commands are executed in the editor's interpreter via the plugin's `execute_python`
handler, which returns captured stdout under result["output"] (CRLF).

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines on stdout can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
(atomic + editor-undoable) AND records an inverse op on a PER-SESSION agent ledger at
`builtins._UMCP_LEDGERS[session]` (persists across execute_python calls, which each get fresh
globals). The session id is injected via PARAMS["_session"] (from _exec; one per writer process),
so concurrent agents never pop each other's entries. `undo` pops the caller's own session ledger
LIFO and reverts ONLY that writer's edits, never the user's manual work or another agent's.

Implemented:
  - get_world_info       (read-only)
  - get_actors_in_level  (read-only)
  - find_actors          (read-only; filtered)
  - get_actor_properties (read-only; full reflection dump)
  - get_log_tail         (read-only; read the editor Output Log remotely)
  - spawn_actor          (write; ledgered)
  - set_actor_transform  (write; ledgered)
  - set_actor_property   (write; ledgered; reflection set with dotted component paths)
  - set_actor_label      (write; ledgered; rename an actor's display label)
  - delete_actor         (write; ledgered; SOFT delete -> hidden _MCP_Trash folder)
  - undo                 (agent-scoped revert of our own edits)

Soft-delete convention: delete_actor does NOT destroy; it moves the actor to a hidden
'_MCP_Trash' outliner folder and hides it in the editor. undo restores folder+visibility.
Listings (get_actors_in_level) hide _MCP_Trash actors unless include_trashed=True.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture -------------------------------------------------
# The plugin runs our snippet on the Windows box and mirrors the editor Output Log
# to <Project>/Saved/Logs/<Project>.log. We wrap every snippet so it records the log
# size before running and, in a finally, flushes (unreal.log_flush()) and reads the
# appended bytes — surfacing any new Warning/Error lines as @@UMCP_LOG@@ (attached to
# the result as "_log_warnings"). This is how a remote instance "sees" the Output Log.
# NB: no ''' and no stray backslashes in this code (the handler wraps code in '''...''').
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes ('''...''')
# before exec, so any ''' or backslash in the code corrupts it. Never embed raw data with
# '''...'''; pass parameters as base64 (its alphabet has no quote/backslash/triple-quote).
# Snippet bodies themselves must also avoid ''' and stray backslashes.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer (bridge/agent process) so its undo ledger is isolated
    # from other concurrent writers. One value per OS process (all modules in a bridge share it);
    # separate agent-harness processes get distinct ids. Override via utils["session"] if provided.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. Any new Warning/Error log lines are attached as result['_log_warnings']."""
        resp = send_command("execute_python", {"code": _wrap(code)})
        # Drain the editor's Python cyclic-GC heap after each op (esp. StateTree undo folds) so a UE GC's
        # PyGC_Collect (FPythonScriptPlugin::OnPreGarbageCollect) can't AV traversing our leftover cyclic
        # garbage. Root cause localized 2026-08-18; see statetree_write2.py _query for details.
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run
        the body in Unreal, and return its MARKER payload. Adds _session for ledger isolation."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ================= LEAN StateTree UNDO path (crash #2 fix, 2026-08-18) =================
    # Re-calling the StateTree C++ handlers from inside the giant _UNDO_BODY (the heavy _COERCE_HELPERS + huge
    # elif chain = a big Python-heap footprint) detonates crash #2 (a residual PyGC AV) on a LATER command. So
    # st_* inverses are applied via SMALL, data-driven, no-`def` snippets instead: peek the top ledger entry,
    # compute the inverse C++ call(s) CLIENT-side, apply them + repair + save + pop in one lean command. Non-st
    # ops still use _UNDO_BODY. Root-cause writeup: [[statetree-authoring-recipe]].
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

    # Client-side: map an st_* ledger entry -> ([[handler_name, args], ...], token). No editor call.
    def _st_inverse(e):
        op = e.get("op")
        if op == "st_set_node_property":
            return [["set_state_tree_node_property_json", [e.get("state_name") or "", e.get("kind"),
                     e.get("index"), e.get("prop"), str(e.get("prev")), e.get("container") or ""]]], "st-node-property-restored"
        if op == "st_set_transition_property":
            return [["set_state_tree_transition_property_json", [e.get("state_name"), e.get("index"),
                     e.get("prop"), str(e.get("prev"))]]], "st-transition-property-restored"
        if op == "st_set_color":
            return [["set_state_tree_color_json", [e.get("state_name"), "", str(e.get("prev"))]]], "st-color-restored"
        if op == "st_add_parameter":
            return [["remove_state_tree_parameter", [e.get("name")]]], "st-parameter-removed"
        if op == "st_set_parameter":
            return [["set_state_tree_parameter", [e.get("name"), str(e.get("prev"))]]], "st-parameter-restored"
        if op == "st_remove_parameter":
            calls = [["add_state_tree_parameter", [e.get("name"), e.get("type") or "float"]]]
            if e.get("value") is not None:
                calls.append(["set_state_tree_parameter", [e.get("name"), str(e.get("value"))]])
            return calls, "st-parameter-re-added"
        if op == "st_add_binding":
            return [["remove_state_tree_binding", [e.get("target_struct_id"), e.get("target_property")]]], "st-binding-removed"
        if op == "st_add_task_completion":  # C++ #21: inverse = remove the binding at the listener target
            return [["remove_state_tree_binding", [e.get("target_struct_id"), e.get("target_property")]]], "st-task-completion-removed"
        if op == "st_bind_delegate":        # C++ #21: inverse = remove the property binding at the listener path
            return [["remove_state_tree_binding", [e.get("listener_struct_id"), e.get("listener_path")]]], "st-delegate-removed"
        if op == "st_remove_binding":
            if e.get("source_struct_id"):
                return [["add_state_tree_binding", [e.get("source_struct_id"), e.get("source_property"),
                         e.get("target_struct_id"), e.get("target_property")]]], "st-binding-re-added"
            return [], "st-binding-source-absent"
        return None, None

    # Client-side: map a pcg_* SCHEMA/PIN ledger entry -> (graph_path, [[handler_name, args],...], token).
    # These inverses call the Wave-5 C++ handlers, which fire UPCGGraph::NotifyGraphChanged ->
    # FCoreUObjectDelegates::OnObjectPropertyChanged.Broadcast on a standalone graph. Broadcasting that
    # from INSIDE the giant (deeply-if/elif-nested) _UNDO_BODY overflows the CPython C-stack (python311
    # crash). Routing them through a LEAN snippet (shallow stack) is safe -- same fix as the st_* ops.
    def _pcg_schema_inverse(e):
        op = e.get("op"); gp = e.get("graph_path")
        if op == "pcg_remove_graph_parameter":
            return gp, [["remove_pcg_graph_parameter_json", [gp, e.get("name")]]], "pcg-graph-param-add-undone"
        if op == "pcg_add_graph_parameter":
            _ty = e.get("type"); _vto = e.get("value_type_object") or ""
            if _ty == "struct":
                _sm = {"Vector2D": "vector2d", "Vector": "vector", "Rotator": "rotator",
                       "Transform": "transform", "Quat": "quat", "LinearColor": "linearcolor"}
                _ty = _sm.get(_vto.split(".")[-1], "vector")
            return gp, [["add_pcg_graph_parameter_json", [gp, e.get("name"), _ty]]], "pcg-graph-param-remove-undone"
        if op == "pcg_rename_graph_parameter":
            return gp, [["rename_pcg_graph_parameter_json", [gp, e.get("old_name"), e.get("new_name")]]], "pcg-graph-param-rename-undone"
        if op == "pcg_remove_dynamic_input_pin":
            _pi = e.get("pin_index")
            _pi = int(_pi) if _pi is not None else -1
            return gp, [["remove_pcg_dynamic_input_pin_json", [gp, e.get("node_name"), _pi]]], "pcg-dynamic-pin-add-undone"
        if op == "pcg_add_dynamic_input_pin":
            return gp, [["add_pcg_dynamic_input_pin_json", [gp, e.get("node_name")]]], "pcg-dynamic-pin-remove-undone"
        return None, None, None

    # Lean peek: return the top ledger entry (for this session) without mutating it.
    _ST_PEEK = r'''
import json, builtins
_root = getattr(builtins, "_UMCP_LEDGERS", None)
_sid = PARAMS.get("_session", "default")
_led = _root.get(_sid) if isinstance(_root, dict) else None
print("@@UMCP@@" + json.dumps({"entry": (_led[-1] if _led else None), "depth": (len(_led) if _led is not None else 0)}))
'''

    # Lean apply: run the precomputed inverse call(s) on the StateTree, repair, save, pop the top ledger entry.
    _ST_UNDO_APPLY = r'''
import unreal, json, builtins
_p = PARAMS
_st = unreal.EditorAssetLibrary.load_asset(_p["asset_path"])
_m = getattr(unreal, "MCPReflectionLibrary", None)
if _st is None or _m is None:
    print("@@UMCP@@" + json.dumps({"status": "success", "result": "statetree-or-handler-absent"}))
else:
    with unreal.ScopedEditorTransaction("MCP undo " + _p.get("_op", "st")):
        for _c in _p["_calls"]:
            _h = getattr(_m, _c[0], None)
            if _h is not None:
                _h(_st, *_c[1])
    if hasattr(_m, "repair_state_tree_nodes"):
        try:
            _m.repair_state_tree_nodes(_st)
        except Exception:
            pass
    unreal.EditorLoadingAndSavingUtils.save_packages([_st.get_outermost()], False)
    _root = getattr(builtins, "_UMCP_LEDGERS", None)
    _sid = _p.get("_session", "default")
    _led = _root.get(_sid) if isinstance(_root, dict) else None
    if _led:
        _led.pop()
    print("@@UMCP@@" + json.dumps({"status": "success", "result": _p.get("_token"), "ledger_depth": (len(_led) if _led is not None else 0)}))
'''

    # Lean apply for pcg_* SCHEMA/PIN inverses: run the Wave-5 C++ handler(s) in a SHALLOW script (NOT the
    # giant _UNDO_BODY), save the graph, pop the ledger, record redo. Avoids the python311 C-stack overflow.
    _PCG_SCHEMA_UNDO = r'''
import unreal, json, builtins
_p = PARAMS
_m = getattr(unreal, "MCPReflectionLibrary", None)
_gp = _p.get("graph_path")
if _m is None:
    print("@@UMCP@@" + json.dumps({"status": "success", "result": "pcg-handler-absent"}))
else:
    _err = None
    for _c in _p["_calls"]:
        _h = getattr(_m, _c[0], None)
        if _h is not None:
            try:
                _rr = _h(*_c[1])
            except Exception as _e:
                _err = str(_e)[:120]
    _g = unreal.EditorAssetLibrary.load_asset(_gp) if _gp else None
    if _g is not None:
        try:
            unreal.EditorLoadingAndSavingUtils.save_packages([_g.get_outermost()], False)
        except Exception:
            pass
    _root = getattr(builtins, "_UMCP_LEDGERS", None)
    _sid = _p.get("_session", "default")
    _led = _root.get(_sid) if isinstance(_root, dict) else None
    if _led:
        _led.pop()
    try:
        _rroot = getattr(builtins, "_UMCP_REDO", None)
        if _rroot is None:
            _rroot = {}; builtins._UMCP_REDO = _rroot
        _rroot.setdefault(_sid, []).append(_p.get("_entry"))
    except Exception:
        pass
    _res = _p.get("_token") if _err is None else ("restore-failed: " + _err)
    print("@@UMCP@@" + json.dumps({"status": "success", "result": _res, "ledger_depth": (len(_led) if _led is not None else 0)}))
'''

    # Lean apply for the BP-based st_set_component_tree inverse (subobject loop inline; no `def`).
    _ST_UNDO_COMPTREE = r'''
import unreal, json, builtins
_p = PARAMS
_e = _p["entry"]
_bp = unreal.EditorAssetLibrary.load_asset(_e.get("blueprint_path"))
_m = getattr(unreal, "MCPReflectionLibrary", None)
_tok = "blueprint-or-handler-absent"
if _bp is not None and _m is not None and hasattr(_m, "set_state_tree_component_tree_json"):
    _subsys = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    _handles = _subsys.k2_gather_subobject_data_for_blueprint(_bp) or []
    _cn = _e.get("component_name")
    _comp = None
    for _h in _handles:
        try:
            _d = _subsys.k2_find_subobject_data_from_handle(_h)
            _o = unreal.SubobjectDataBlueprintFunctionLibrary.get_object(_d) if _d else None
        except Exception:
            _o = None
        if _o is not None and isinstance(_o, unreal.StateTreeComponent) and _o.get_name() == _cn:
            _comp = _o
            break
    if _comp is None:
        _tok = "component-absent"
    else:
        _pst = _e.get("prev_state_tree")
        _tree = unreal.EditorAssetLibrary.load_asset(_pst) if (_pst and _pst != "None") else None
        with unreal.ScopedEditorTransaction("MCP undo st_set_component_tree"):
            _m.set_state_tree_component_tree_json(_comp, _e.get("property_name"), _tree, "")
            try:
                unreal.BlueprintEditorLibrary.compile_blueprint(_bp)
            except Exception:
                pass
        unreal.EditorLoadingAndSavingUtils.save_packages([_bp.get_outermost()], False)
        _tok = "st-component-tree-restored"
_root = getattr(builtins, "_UMCP_LEDGERS", None)
_sid = _p.get("_session", "default")
_led = _root.get(_sid) if isinstance(_root, dict) else None
if _led:
    _led.pop()
print("@@UMCP@@" + json.dumps({"status": "success", "result": _tok, "ledger_depth": (len(_led) if _led is not None else 0)}))
'''

    # Shared Unreal-side helpers (prepended to bodies that need them). No ''' / no backslashes.
    # _settable(v)->(json_value, restorable): convert an Unreal value to a JSON form we can
    #   later re-apply. _coerce(current, value)->unreal value: build a settable value from JSON
    #   using current's type as a hint (object paths -> load asset, lists -> Vector/Rotator/Color,
    #   enum name -> enum). These make set/undo symmetric. _descend traverses component + dotted
    #   object paths (refuses struct sub-paths, which wouldn't persist).
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    # Per-session undo stack so concurrent agents never pop each other's entries.
    # Session id arrives in PARAMS["_session"] (injected by _exec from the bridge/harness).
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
def _find_by_name(uniq):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == uniq:
            return a
    return None
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
'''

    # ------------------------------------------------------------------ #
    # get_world_info — level/world summary of the active editor world     #
    # ------------------------------------------------------------------ #
    _GET_WORLD_INFO = r'''
import unreal, json
def _try(fn, default=None):
    try: return fn()
    except Exception: return default
info = {}
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
info["world_name"] = _try(lambda: world.get_name())
info["world_path"] = _try(lambda: world.get_path_name())
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = _try(lambda: eas.get_all_level_actors(), []) or []
info["actor_count"] = len(actors)
info["engine_version"] = _try(lambda: unreal.SystemLibrary.get_engine_version())
info["is_play_in_editor"] = _try(lambda: world.is_play_in_editor(), False)
info["is_world_partition"] = _try(lambda: world.get_editor_property("world_partition")) is not None
info["persistent_level"] = _try(lambda: world.get_outer().get_name()) or info["world_name"]
info["game_mode"] = _try(lambda: world.get_editor_property("authority_game_mode").get_class().get_name())
print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_world_info(ctx) -> str:
        """Get a summary of the active editor world/level: world name and package path,
        placed-actor count, engine version, play-in-editor state, world-partition flag,
        persistent level, and game mode (if any). Read-only."""
        try:
            info = _query(_GET_WORLD_INFO)
            return json.dumps(info, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actors_in_level — every placed actor with transform + folder    #
    # ------------------------------------------------------------------ #
    _GET_ACTORS = r'''
import unreal, json
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _vec(v):
    return [round(v.x,3), round(v.y,3), round(v.z,3)] if v is not None else None
include_trashed = bool(PARAMS.get("include_trashed"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
out = []
trashed = 0
for a in actors:
    if not a: continue
    f = _try(lambda: str(a.get_folder_path())) or ""
    folder = "" if f in ("None", "") else f
    if folder == "_MCP_Trash":
        trashed += 1
        if not include_trashed:
            continue
    rot = _try(lambda: a.get_actor_rotation())
    out.append({
        "name":   _try(lambda: a.get_name()),
        "label":  _try(lambda: a.get_actor_label()),
        "class":  _try(lambda: a.get_class().get_name()),
        "location": _vec(_try(lambda: a.get_actor_location())),
        "rotation": ([round(rot.pitch,3), round(rot.yaw,3), round(rot.roll,3)] if rot is not None else None),
        "folder": folder,
    })
print("@@UMCP@@" + json.dumps({"count": len(out), "trashed_hidden": (0 if include_trashed else trashed), "actors": out}))
'''

    @mcp.tool()
    def get_actors_in_level(ctx, include_trashed: bool = False) -> str:
        """List every actor placed in the active level, each with its internal name,
        display label, class, world location [x,y,z], rotation [pitch,yaw,roll], and
        outliner folder. Read-only. Use this to verify state after edits.

        include_trashed: include soft-deleted actors (in _MCP_Trash); default False.
        The response reports 'trashed_hidden' = how many were omitted."""
        try:
            data = _exec(_GET_ACTORS, {"include_trashed": include_trashed})
            return json.dumps(data, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_actors — filtered search (name/label/class/tag), read-only     #
    # ------------------------------------------------------------------ #
    _FIND_ACTORS_BODY = r'''
import unreal, json, fnmatch
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
name_pat = PARAMS.get("name_pattern")
label_pat = PARAMS.get("label_pattern")
class_filter = PARAMS.get("class_filter")
tag = PARAMS.get("tag")
exact_class = bool(PARAMS.get("exact_class"))
max_results = PARAMS.get("max_results")
include_tx = bool(PARAMS.get("include_transform"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
cls_obj = getattr(unreal, class_filter, None) if (class_filter and not exact_class) else None
out = []
for a in (eas.get_all_level_actors() or []):
    if not a:
        continue
    nm = a.get_name(); lb = a.get_actor_label(); cn = a.get_class().get_name()
    if not _match(nm, name_pat):
        continue
    if not _match(lb, label_pat):
        continue
    if class_filter:
        if exact_class:
            if cn != class_filter:
                continue
        elif cls_obj is not None:
            if not isinstance(a, cls_obj):
                continue
        elif class_filter.lower() not in cn.lower():
            continue
    if tag:
        try:
            has = a.actor_has_tag(unreal.Name(tag))
        except Exception:
            has = False
        if not has:
            continue
    rec = {"name": nm, "label": lb, "class": cn}
    if include_tx:
        l = a.get_actor_location(); r = a.get_actor_rotation(); s = a.get_actor_scale3d()
        rec["location"] = [l.x, l.y, l.z]
        rec["rotation"] = [r.pitch, r.yaw, r.roll]
        rec["scale"] = [s.x, s.y, s.z]
    out.append(rec)
    if max_results and len(out) >= int(max_results):
        break
print("@@UMCP@@" + json.dumps({"count": len(out), "actors": out}))
'''

    @mcp.tool()
    def find_actors(ctx, name_pattern: str = None, label_pattern: str = None,
                    class_filter: str = None, tag: str = None,
                    exact_class: bool = False, max_results: int = None,
                    include_transform: bool = False) -> str:
        """Find actors in the active level by flexible filters (all optional, AND-combined).

        name_pattern:  match on unique internal name (substring or glob, case-insensitive).
        label_pattern: match on display label (substring or glob, case-insensitive).
        class_filter:  class name; by default matches the class or any subclass; set
                       exact_class=True to require the exact class name.
        tag:           only actors carrying this actor tag.
        max_results:   cap the number returned.
        include_transform: include location/rotation/scale per actor.
        Read-only."""
        params = {"name_pattern": name_pattern, "label_pattern": label_pattern,
                  "class_filter": class_filter, "tag": tag, "exact_class": exact_class,
                  "max_results": max_results, "include_transform": include_transform}
        try:
            return json.dumps(_exec(_FIND_ACTORS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actor_properties — full reflection dump (read-only)             #
    # ------------------------------------------------------------------ #
    _GET_PROPS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")  # deprecated-alias reads spam DeprecationWarning; this is a pure read
ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 25)
def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return sorted(names)
def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        items = []
        for i, e in enumerate(v):
            if i >= ARRAY_LIMIT:
                items.append("...(%d more)" % (len(v) - ARRAY_LIMIT)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= ARRAY_LIMIT:
                break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try: d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception: d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
def _dump(obj, flt, depth, max_entries, cursor):
    names = _prop_names(obj)
    if flt:
        f = flt.lower(); names = [n for n in names if f in n.lower()]
    total = len(names)
    start = int(cursor or 0)
    window = names[start:start + int(max_entries)] if max_entries else names[start:]
    props = {}
    for n in window:
        try: props[n] = _ser(obj.get_editor_property(n), depth)
        except Exception: props[n] = "<unreadable>"
    nxt = start + len(window)
    return props, total, (nxt if nxt < total else None)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
target = None
for a in (eas.get_all_level_actors() or []):
    if a and (a.get_actor_label() == PARAMS["actor_label"] or a.get_name() == PARAMS["actor_label"]):
        target = a; break
if target is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_label"]}))
else:
    depth = int(PARAMS.get("max_depth") or 1)
    props, total, nxt = _dump(target, PARAMS.get("filter"), depth, PARAMS.get("max_entries"), PARAMS.get("cursor"))
    result = {"status": "success", "actor_label": target.get_actor_label(),
              "name": target.get_name(), "class": target.get_class().get_name(),
              "total_properties": total, "returned": len(props), "next_cursor": nxt, "properties": props}
    if PARAMS.get("include_components"):
        comps = {}
        for c in (target.get_components_by_class(unreal.ActorComponent) or []):
            cp, ctot, _ = _dump(c, PARAMS.get("filter"), depth, 20, 0)
            comps[c.get_name()] = {"class": c.get_class().get_name(), "total_properties": ctot, "properties": cp}
        result["components"] = comps
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_actor_properties(ctx, actor_label: str, filter: str = None,
                             include_components: bool = False, max_depth: int = 1,
                             array_element_limit: int = 25, max_entries: int = 60,
                             cursor: int = 0) -> str:
        """Dump an actor's reflected UPROPERTYs (full reflection). Read-only.

        actor_label:        actor's display label (preferred) or unique internal name.
        filter:             case-insensitive substring; only property names containing it.
        include_components: also dump each component's properties (under 'components').
        max_depth:          how deep to expand nested structs (default 1; deeper = bigger).
        array_element_limit: cap elements shown per array/set/map.
        max_entries/cursor: paginate the actor's own property list; response returns
                            'next_cursor' (pass it back as cursor to continue) or null.

        Values are JSON-serialized (vectors→[x,y,z], object refs→{__object__: path}, enums→str).
        Unreadable properties (delegates, some editor-only) are marked '<unreadable>'."""
        params = {"actor_label": actor_label, "filter": filter,
                  "include_components": include_components, "max_depth": max_depth,
                  "array_element_limit": array_element_limit,
                  "max_entries": max_entries, "cursor": cursor}
        try:
            return json.dumps(_exec(_GET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_log_tail — read the editor Output Log remotely (read-only)      #
    # ------------------------------------------------------------------ #
    _LOG_TAIL_BODY = r'''
import unreal, os, json
d = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
main = None
for f in os.listdir(d):
    if f.endswith(".log") and "-backup-" not in f:
        main = os.path.join(d, f); break
unreal.log_flush()
lines = []
if main:
    data = open(main, "rb").read().decode("utf-8", "replace")
    lines = data.splitlines()
n = int(PARAMS.get("lines") or 100)
contains = PARAMS.get("contains")
level = (PARAMS.get("level") or "").lower()
sel = lines
if level == "warning":
    sel = [l for l in sel if ": Warning:" in l]
elif level == "error":
    sel = [l for l in sel if ": Error:" in l]
elif level in ("warning+", "issues"):
    sel = [l for l in sel if (": Warning:" in l or ": Error:" in l)]
if contains:
    c = contains.lower(); sel = [l for l in sel if c in l.lower()]
tail = sel[-n:]
print("@@UMCP@@" + json.dumps({"status": "success",
    "log_file": os.path.basename(main) if main else None,
    "total_lines": len(lines), "matched": len(sel), "returned": len(tail), "lines": tail}))
'''

    @mcp.tool()
    def get_log_tail(ctx, lines: int = 100, contains: str = None, level: str = None) -> str:
        """Read the tail of the editor's Output Log (the live .log on the Windows box).
        Use this to see warnings/errors from recent operations. Read-only.

        lines:    max lines to return (from the end, after filtering).
        contains: case-insensitive substring filter.
        level:    'warning' | 'error' | 'issues' (warnings+errors) to filter by severity."""
        params = {"lines": lines, "contains": contains, "level": level}
        try:
            return json.dumps(_exec(_LOG_TAIL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_actor — place an actor in the level (ledgered write)          #
    # ------------------------------------------------------------------ #
    _SPAWN_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
actor_type = PARAMS.get("actor_type") or "StaticMeshActor"
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
scale = PARAMS.get("scale")
mesh_path = PARAMS.get("static_mesh")
cls = getattr(unreal, actor_type, None)
if cls is None:
    try: cls = unreal.load_class(None, actor_type)
    except Exception: cls = None
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown actor_type: %s" % actor_type}))
else:
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_actor"):
        actor = eas.spawn_actor_from_class(cls, location, rotation)
        if actor:
            actor.set_actor_label(name)
            if scale:
                actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
            if mesh_path is None and actor_type == "StaticMeshActor":
                mesh_path = "/Engine/BasicShapes/Cube.Cube"
            if mesh_path:
                comp = actor.get_editor_property("static_mesh_component")
                mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
                if comp and mesh:
                    comp.set_static_mesh(mesh)
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": name})
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "location": [location.x, location.y, location.z],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_actor(ctx, name: str, actor_type: str = "StaticMeshActor",
                    location: list = None, rotation: list = None,
                    scale: list = None, static_mesh: str = None) -> str:
        """Spawn an actor into the active level and give it a display label.

        name:        display label for the new actor (required).
        actor_type:  engine class name (default 'StaticMeshActor') e.g. 'PointLight',
                     'CameraActor', or a loadable class path.
        location:    [x, y, z] world position (default [0,0,0]).
        rotation:    [pitch, yaw, roll] in degrees (default [0,0,0]).
        scale:       [x, y, z] scale (optional).
        static_mesh: asset path for StaticMeshActor (default '/Engine/BasicShapes/Cube.Cube'
                     when a StaticMeshActor is spawned without one).

        Ledgered write: recorded on the agent ledger so `undo` can delete it later."""
        params = {"name": name, "actor_type": actor_type, "location": location,
                  "rotation": rotation, "scale": scale, "static_mesh": static_mesh}
        try:
            return json.dumps(_exec(_SPAWN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_transform — move/rotate/scale a placed actor (ledgered)   #
    # ------------------------------------------------------------------ #
    _SET_TRANSFORM_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
loc = PARAMS.get("location"); rot = PARAMS.get("rotation"); scale = PARAMS.get("scale")
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
else:
    l = a.get_actor_location(); r = a.get_actor_rotation(); s = a.get_actor_scale3d()
    prior = {"loc": [l.x, l.y, l.z], "rot": [r.pitch, r.yaw, r.roll], "scale": [s.x, s.y, s.z]}
    with unreal.ScopedEditorTransaction("MCP set_actor_transform"):
        if loc:
            a.set_actor_location(unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])), False, False)
        if rot:
            a.set_actor_rotation(unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])), False)
        if scale:
            a.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
    _ledger().append({"op": "set_actor_transform", "actor_name": a.get_name(), "prior": prior})
    nl = a.get_actor_location(); nr = a.get_actor_rotation(); ns = a.get_actor_scale3d()
    print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(), "label": a.get_actor_label(),
        "transform": {"loc": [nl.x, nl.y, nl.z], "rot": [nr.pitch, nr.yaw, nr.roll], "scale": [ns.x, ns.y, ns.z]},
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_transform(ctx, name: str, location: list = None,
                            rotation: list = None, scale: list = None) -> str:
        """Move/rotate/scale a placed actor. Partial update — only the components you pass
        are changed.

        name:     actor's display label (preferred) or unique internal name.
        location: [x, y, z] world position.
        rotation: [pitch, yaw, roll] in degrees.
        scale:    [x, y, z] scale.

        Ledgered write: the prior transform is captured so `undo` restores it exactly."""
        params = {"name": name, "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SET_TRANSFORM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_property — set a reflected property (ledgered write)       #
    # ------------------------------------------------------------------ #
    _SET_PROP_BODY = _COERCE_HELPERS + r'''
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = _resolve_actor(PARAMS["actor_label"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_label"]}))
else:
    cont, final, err = _descend(actor, PARAMS.get("component_name"), PARAMS["property_path"])
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        try:
            prior_raw = cont.get_editor_property(final); have = True
        except Exception:
            have = False
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property: %s" % final}))
        if have:
            prior_json, restorable = _settable(prior_raw)
            newv = _coerce(prior_raw, PARAMS["property_value"])
            with unreal.ScopedEditorTransaction("MCP set_actor_property"):
                cont.set_editor_property(final, newv)
            after_json, _u = _settable(cont.get_editor_property(final))
            _ledger().append({"op": "set_actor_property", "actor_name": actor.get_name(),
                "component_name": PARAMS.get("component_name"), "path": PARAMS["property_path"],
                "prior": prior_json, "restorable": restorable})
            print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
                "label": actor.get_actor_label(), "path": PARAMS["property_path"],
                "before": prior_json, "after": after_json, "restorable": restorable,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_property(ctx, actor_label: str, property_path: str,
                           property_value=None, component_name: str = None) -> str:
        """Set a reflected property on a placed actor (ledgered write).

        actor_label:   actor's display label (preferred) or unique internal name.
        property_path: property name, or dotted path through component objects
                       (e.g. 'static_mesh_component.static_mesh'). Struct sub-paths
                       (e.g. body_instance.mass...) are not supported and are refused.
        property_value: the new value. Types are coerced from the current value:
                       object/asset props accept an asset path string; vectors/rotators/
                       colors accept [..] lists; enums accept the member name string;
                       bools/numbers/strings pass through.
        component_name: optional component to target instead of the actor.

        The prior value is captured so `undo` restores it (best-effort for enums)."""
        params = {"actor_label": actor_label, "property_path": property_path,
                  "property_value": property_value, "component_name": component_name}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_label — rename an actor's display label (ledgered)         #
    # ------------------------------------------------------------------ #
    _SET_LABEL_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS["label"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["label"]}))
else:
    old = actor.get_actor_label()
    with unreal.ScopedEditorTransaction("MCP set_actor_label"):
        actor.set_actor_label(PARAMS["new_label"])
    _ledger().append({"op": "set_actor_label", "actor_name": actor.get_name(), "prior_label": old})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
        "old_label": old, "new_label": actor.get_actor_label(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_label(ctx, label: str, new_label: str) -> str:
        """Rename an actor's display label (the name shown in the World Outliner).

        label:     the actor's current label (or unique internal name).
        new_label: the new display label.
        Ledgered write: the old label is captured so `undo` restores it."""
        try:
            return json.dumps(_exec(_SET_LABEL_BODY, {"label": label, "new_label": new_label}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_actor — SOFT delete: hide in _MCP_Trash folder (ledgered)     #
    # ------------------------------------------------------------------ #
    _DELETE_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS["name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["name"]}))
else:
    prior_folder = str(actor.get_folder_path())
    was_hidden = actor.is_temporarily_hidden_in_editor()
    with unreal.ScopedEditorTransaction("MCP delete_actor (soft)"):
        actor.set_folder_path(unreal.Name("_MCP_Trash"))
        actor.set_is_temporarily_hidden_in_editor(True)
    _ledger().append({"op": "delete_actor", "actor_name": actor.get_name(),
        "prior_folder": prior_folder, "was_hidden": was_hidden})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
        "label": actor.get_actor_label(), "soft_deleted": True,
        "note": "moved to hidden _MCP_Trash; undo restores. Not destroyed/purged.",
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_actor(ctx, name: str) -> str:
        """Soft-delete an actor: move it to a hidden '_MCP_Trash' outliner folder and hide
        it in the editor (it is NOT destroyed, so restoration is perfect). It stops showing
        in get_actors_in_level (unless include_trashed=True). `undo` restores it.

        name: the actor's display label (preferred) or unique internal name."""
        try:
            return json.dumps(_exec(_DELETE_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # undo — revert our own recent edits, LIFO (agent-scoped)             #
    # ------------------------------------------------------------------ #
    _UNDO_BODY = _COERCE_HELPERS + r'''
count = int(PARAMS.get("count", 1))
led = _ledger()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
undone = []
def _save_niag(sysobj, ap):
    # NiagaraSystem structural/param undo must save via the C++ #10 save_niagara_system (sync compile +
    # C++ save) or the FortniteMain custom-version error drops the save (same bug the forward ops hit).
    m = getattr(unreal, "MCPReflectionLibrary", None)
    if m is not None and sysobj is not None and hasattr(m, "save_niagara_system"):
        try:
            m.save_niagara_system(sysobj); return
        except Exception:
            pass
    try:
        unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception:
        pass
def _st_save(p):
    # Non-validating save for StateTree undo folds. EditorAssetLibrary.save_asset triggers asset VALIDATION
    # (InternalPromptForCheckoutAndSave) which hard-crashes on MCP-written StateTrees; save_packages persists
    # without it. (Root-caused 2026-08-18.)
    try:
        a = unreal.EditorAssetLibrary.load_asset(p)
        if a is None:
            return False
        # C++ #20 THE FIX: repair malformed nodes (empty Instance struct + zero ID from import_text authoring)
        # before saving -- that malformation crashes the StateTree compiler/serializer on save. Idempotent;
        # guarded until the C++ handler lands.
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
def _bt_resolve(bt, path):
    # Walk a BehaviorTree's composite children by dot-index path ("" / "root" -> root_node). Mirrors
    # bt_write.py._resolve_comp so the BT-node undo inverses can re-find the parent to pop from.
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
def _eqs_norm(c):
    # cross-module (eqs_write.py C++#15/#17): normalize a short class name to /Script/AIModule.<Name>.
    return c if (c and ("/" in c or "." in c)) else (("/Script/AIModule." + c) if c else c)
def _eqs_pkg(p):
    seg = p.split("/")[-1]
    return p.rsplit(".", 1)[0] if "." in seg else p
def _eqs_save(p):
    try: unreal.EditorAssetLibrary.save_asset(_eqs_pkg(p), only_if_is_dirty=False)
    except Exception: pass
def _eqs_setp(o, loc, pn, v):
    # array-form value (the C++ #15 Windows fix); handler unwraps + returns bare prior in "prev".
    m = getattr(unreal, "MCPReflectionLibrary", None)
    try: return json.loads(m.set_env_query_node_property(o, loc, pn, json.dumps([v])))
    except Exception as e: return {"error": str(e)}
_MG = getattr(unreal, "MaterialEditingLibrary", None)
def _mg_mat(ap):
    # cross-module (material_graph_write.py) undo: load a base Material (not an instance).
    m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    return m if isinstance(m, unreal.Material) else None
def _mg_find(mat, nm):
    if mat is None or not nm or _MG is None:
        return None
    for e in (_MG.get_material_expressions(mat) or []):
        if e and e.get_name() == nm:
            return e
    return None
def _mg_prop(s):
    if s is None:
        return None
    s = str(s)
    return getattr(unreal.MaterialProperty, s if s.startswith("MP_") else "MP_" + s.upper(), None)
def _mg_coerce(val):
    if isinstance(val, dict):
        if "__lincolor__" in val:
            c = val["__lincolor__"]; return unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), float(c[3]))
        if "__color__" in val:
            c = val["__color__"]; return unreal.Color(r=int(c[0]), g=int(c[1]), b=int(c[2]), a=int(c[3]))
        if "__name__" in val:
            return unreal.Name(str(val["__name__"]))
        if "__object__" in val:
            p = val["__object__"]; return unreal.EditorAssetLibrary.load_asset(p) if p else None
    return val
def _mg_finish(mat, ap):
    if _MG is not None and mat is not None:
        try: _MG.recompile_material(mat)
        except Exception: pass
    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception: pass
def _mf_fn(ap):
    # cross-module (material_function_write.py) undo: load a base MaterialFunction (not an instance).
    m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    return m if isinstance(m, unreal.MaterialFunction) else None
def _mf_mfi(ap):
    # cross-module (material_function_write.py) undo: load a MaterialFunctionInstance.
    m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    return m if isinstance(m, unreal.MaterialFunctionInstance) else None
def _mf_find(mf, nm):
    if mf is None or not nm or _MG is None:
        return None
    for e in (_MG.get_material_function_expressions(mf) or []):
        if e and e.get_name() == nm:
            return e
    return None
def _mf_update(mf, ap):
    if _MG is not None and mf is not None:
        try: _MG.update_material_function(mf)
        except Exception: pass
    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception: pass
def _mf_save(ap):
    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception: pass
def _crg_ctrl(ap, gname):
    # cross-module (controlrig_graph_write.py) undo: resolve the RigVM controller for the named graph.
    bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    if bp is None or not isinstance(bp, unreal.ControlRigBlueprint):
        return None
    try:
        if not gname or str(gname) == "RigVMModel":
            return bp.get_controller()
        for _m in (bp.get_all_models() or []):
            if _m and str(_m.get_graph_name()) == str(gname):
                return bp.get_controller(_m)
        # Function/collapse contained graphs are NOT returned by get_all_models(); they hold the
        # graph-scoped local variables (and the variable nodes referencing them). Search the function
        # library's contained graphs by name (mirrors controlrig_graph_write._get_controller). R11 fix.
        try:
            _lib = bp.get_local_function_library()
            for _fn in (_lib.get_nodes() or []):
                try:
                    _cg = _fn.get_contained_graph()
                except Exception:
                    _cg = None
                if _cg is not None and str(_cg.get_graph_name()) == str(gname):
                    return bp.get_controller(_cg)
        except Exception:
            pass
        # A specific graph was requested but not found anywhere -> fail safe (skip with a note in the
        # undo branch) rather than silently mutating the wrong (top) graph.
        return None
    except Exception:
        return None
def _modular_ctrl(ap):
    # cross-module (controlrig_cpp.py) undo: resolve the UModularRigController for a MODULAR control rig.
    bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    if bp is None or not isinstance(bp, unreal.ControlRigBlueprint):
        return None
    try:
        return bp.get_modular_rig_controller()
    except Exception:
        return None
# --- sequencer_edit.py (G1-A) undo helpers: locator + enum maps + key-state restorer ---
_SEQE_INTERP = {"constant": "RCIM_CONSTANT", "linear": "RCIM_LINEAR", "cubic": "RCIM_CUBIC"}
_SEQE_TANMODE = {"auto": "RCTM_AUTO", "user": "RCTM_USER", "break": "RCTM_BREAK", "smart_auto": "RCTM_SMART_AUTO"}
_SEQE_TANWMODE = {"none": "RCTWM_WEIGHTED_NONE", "arrive": "RCTWM_WEIGHTED_ARRIVE", "leave": "RCTWM_WEIGHTED_LEAVE", "both": "RCTWM_WEIGHTED_BOTH"}
_SEQE_EXTRAP = {"constant": "RCCE_CONSTANT", "cycle": "RCCE_CYCLE", "cycle_with_offset": "RCCE_CYCLE_WITH_OFFSET", "oscillate": "RCCE_OSCILLATE", "linear": "RCCE_LINEAR", "none": "RCCE_NONE"}
_SEQE_BLEND = {"absolute": "ABSOLUTE", "additive": "ADDITIVE", "relative": "RELATIVE", "additive_from_base": "ADDITIVE_FROM_BASE", "override": "OVERRIDE"}
def _seqe_load(path):
    _o = unreal.EditorAssetLibrary.load_asset(path) if path else None
    return _o if isinstance(_o, unreal.LevelSequence) else None
def _seqe_enum(cls_name, member, default_member=None):
    _c = getattr(unreal, cls_name, None)
    if _c is None:
        return None
    _v = getattr(_c, str(member), None)
    if _v is None and default_member is not None:
        _v = getattr(_c, default_member, None)
    return _v
def _seqe_tracks(seq, binding):
    if seq is None:
        return []
    if binding in (None, ""):
        return list(seq.get_tracks())
    for _b in seq.get_bindings():
        try:
            if str(_b.get_display_name()) == str(binding) or str(_b.get_name()) == str(binding):
                return list(_b.get_tracks())
        except Exception:
            continue
    return []
def _seqe_sec(seq, binding, ti, si):
    _tks = _seqe_tracks(seq, binding)
    if ti is None or int(ti) >= len(_tks):
        return None, None
    _tr = _tks[int(ti)]
    _secs = list(_tr.get_sections())
    if si is None or int(si) >= len(_secs):
        return _tr, None
    return _tr, _secs[int(si)]
def _seqe_ch(sec, ci):
    if sec is None or ci is None:
        return None
    _chs = list(sec.get_all_channels())
    if int(ci) >= len(_chs):
        return None
    return _chs[int(ci)]
def _seqe_key(ch, frame):
    if ch is None or frame is None:
        return None
    for _k in ch.get_keys():
        try:
            if int(_k.get_time().frame_number.value) == int(frame):
                return _k
        except Exception:
            continue
    return None
def _seqe_restore_key(k, st):
    if k is None or not st:
        return
    if st.get("tangent_mode") is not None:
        _v = _seqe_enum("RichCurveTangentMode", _SEQE_TANMODE.get(str(st.get("tangent_mode")), "RCTM_AUTO"))
        if _v is not None:
            try: k.set_tangent_mode(_v)
            except Exception: pass
    if st.get("tangent_weight_mode") is not None:
        _v = _seqe_enum("RichCurveTangentWeightMode", _SEQE_TANWMODE.get(str(st.get("tangent_weight_mode")), "RCTWM_WEIGHTED_NONE"))
        if _v is not None:
            try: k.set_tangent_weight_mode(_v)
            except Exception: pass
    if st.get("interp") is not None:
        _v = _seqe_enum("RichCurveInterpMode", _SEQE_INTERP.get(str(st.get("interp")), "RCIM_CUBIC"))
        if _v is not None:
            try: k.set_interpolation_mode(_v)
            except Exception: pass
    for _fld, _setter in (("arrive", "set_arrive_tangent"), ("leave", "set_leave_tangent"),
                          ("arrive_weight", "set_arrive_tangent_weight"), ("leave_weight", "set_leave_tangent_weight")):
        if st.get(_fld) is not None:
            try: getattr(k, _setter)(float(st.get(_fld)))
            except Exception: pass
# --- input_write.py (G2-B) undo helpers: tagged-value coerce + trigger/modifier + mapping rebuild ---
def _iw_snake(pn):
    _s = ""
    for _ch in (pn or ""):
        if _ch.isupper() and _s and not _s.endswith("_"): _s += "_"
        _s += _ch.lower()
    return _s
def _iw_coerce_val(v):
    if isinstance(v, dict):
        if "__enum__" in v:
            _et = getattr(unreal, v.get("__enum__"), None)
            return getattr(_et, v.get("token"), None) if _et else None
        if "__name__" in v: return unreal.Name(v.get("__name__"))
        if "__vec__" in v: return unreal.Vector(*v.get("__vec__"))
        if "__vec2__" in v: return unreal.Vector2D(*v.get("__vec2__"))
        if "__object__" in v: return unreal.EditorAssetLibrary.load_asset(v.get("__object__"))
        if "__skip__" in v: return None
    return v
def _iw_set(o, pn, val):
    for _cand in (pn, _iw_snake(pn)):
        try: o.set_editor_property(_cand, val); return
        except Exception: continue
def _iw_rebuild_tm(spec, outer):
    if not spec: return None
    _lc = unreal.load_class(None, spec.get("class_path"))
    if _lc is None: return None
    _inst = unreal.new_object(_lc, outer)
    for _pn, _sv in (spec.get("props") or {}).items():
        if isinstance(_sv, dict) and "__skip__" in _sv: continue
        _iw_set(_inst, _pn, _iw_coerce_val(_sv))
    return _inst
def _iw_rebuild_mapping(spec, imc):
    _m = unreal.EnhancedActionKeyMapping()
    if spec.get("action_path"):
        _a = unreal.EditorAssetLibrary.load_asset(spec.get("action_path"))
        if _a is not None: _m.set_editor_property("action", _a)
    if spec.get("key_name"):
        _kk = unreal.Key(); _kk.set_editor_property("key_name", unreal.Name(spec.get("key_name"))); _m.set_editor_property("key", _kk)
    _m.set_editor_property("triggers",  [x for x in (_iw_rebuild_tm(s, imc) for s in (spec.get("triggers")  or [])) if x is not None])
    _m.set_editor_property("modifiers", [x for x in (_iw_rebuild_tm(s, imc) for s in (spec.get("modifiers") or [])) if x is not None])
    return _m
def _iw_dkm(imc):
    _d = imc.get_editor_property("default_key_mappings"); return _d, list(_d.get_editor_property("mappings") or [])
def _iw_setdkm(imc, d, arr):
    d.set_editor_property("mappings", arr); imc.set_editor_property("default_key_mappings", d)
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _node_class(spec):
    t = getattr(unreal, spec, None) if isinstance(spec, str) else None
    if t is not None:
        return t
    c = _try(lambda: unreal.load_class(None, spec))
    if c is not None:
        return c
    return _try(lambda: unreal.load_class(None, "/Script/AIModule." + str(spec)))
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
        lo = unreal.BTDecoratorLogic(); lo.import_text(ex); ops.append(lo)
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
_KT_MAP_G7 = {"bool": "BlackboardKeyType_Bool", "int": "BlackboardKeyType_Int", "float": "BlackboardKeyType_Float",
              "vector": "BlackboardKeyType_Vector", "object": "BlackboardKeyType_Object", "name": "BlackboardKeyType_Name",
              "string": "BlackboardKeyType_String", "enum": "BlackboardKeyType_Enum", "rotator": "BlackboardKeyType_Rotator",
              "class": "BlackboardKeyType_Class"}
def _keytype_cls(short):
    if not short:
        return None
    cn = short if str(short).startswith("BlackboardKeyType_") else _KT_MAP_G7.get(str(short).lower())
    if cn is None:
        return None
    return _try(lambda: unreal.load_object(None, "/Script/AIModule." + cn))
# ---- StateTree undo helpers (statetree_write.py inverses; see statetree-authoring-recipe) ----
ST_PROP = {"task": "tasks", "condition": "enter_conditions", "consideration": "considerations",
           "evaluator": "evaluators", "global_task": "global_tasks"}
def _st_find_ed(st):
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
def _st_iter(ed):
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
def _st_find(ed, name):
    if not name:
        return None
    for s in _st_iter(ed):
        try:
            if str(s.get_editor_property("name")) == name:
                return s
        except Exception:
            pass
    return None
def _st_append(obj, prop, item):
    arr = obj.get_editor_property(prop); arr.append(item); obj.set_editor_property(prop, arr)
def _st_import_node(export_text):
    en = unreal.StateTreeEditorNode()
    try:
        en.import_text(export_text)
    except Exception:
        pass
    return en
def _st_owner(ed, kind, state_name):
    if kind in ("evaluator", "global_task"):
        return ed
    return _st_find(ed, state_name)
def _st_rebuild_state(ed, snap):
    s = unreal.new_object(unreal.StateTreeState, ed)
    s.set_editor_property("name", snap.get("name"))
    for key, prop in (("tasks", "tasks"), ("conditions", "enter_conditions"), ("considerations", "considerations")):
        for tx in (snap.get(key) or []):
            if tx:
                _st_append(s, prop, _st_import_node(tx))
    for cs in (snap.get("children") or []):
        _st_append(s, "children", _st_rebuild_state(ed, cs))
    return s
def _st_import_transition(tx):
    tr = unreal.StateTreeTransition()
    try:
        tr.import_text(tx)
    except Exception:
        pass
    return tr
ST_STATE_ENUM = {"type": "StateTreeStateType", "selection_behavior": "StateTreeStateSelectionBehavior"}
def _st_coerce_state(prop, prior_s):
    if prop in ST_STATE_ENUM:
        e = getattr(unreal, ST_STATE_ENUM[prop], None)
        return getattr(e, str(prior_s), None) if e is not None else prior_s
    if prop == "weight":
        try:
            return float(prior_s)
        except Exception:
            return 0.0
    return str(prior_s)
# ---- asset_ops.py undo helpers (reference-preserving rename inverse) ----
def _ao_dirof(p):
    return str(p).rsplit("/", 1)[0]
def _ao_nameof(p):
    return str(p).rsplit("/", 1)[-1]
def _ao_rename(src, dst_dir, nm):
    o = unreal.EditorAssetLibrary.load_asset(src)
    if o is None:
        return False
    ard = unreal.AssetRenameData()
    ard.set_editor_property("asset", o)
    ard.set_editor_property("new_package_path", dst_dir)
    ard.set_editor_property("new_name", nm)
    ok = unreal.AssetToolsHelpers.get_asset_tools().rename_assets([ard])
    try:
        unreal.EditorAssetLibrary.save_asset(dst_dir + "/" + nm, only_if_is_dirty=False)
    except Exception:
        pass
    return bool(ok)
# ---- widgets_write2.py undo helpers (ported verbatim; self-describing marker-dict priors) ----
def _ww_find_widget(wb, name):
    try:
        wt = unreal.find_object(wb, "WidgetTree")
    except Exception:
        wt = None
    if wt is None:
        return None
    try:
        w = unreal.find_object(wt, name)
    except Exception:
        w = None
    return w if isinstance(w, unreal.Widget) else None
def _ww_compile_save(wb, path):
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(wb)
    except Exception:
        pass
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
def _ww_v2(a):
    return unreal.Vector2D(float(a[0]), float(a[1]))
def _ww_mk_margin(a):
    if isinstance(a, (int, float)):
        return unreal.Margin(float(a), float(a), float(a), float(a))
    if isinstance(a, (list, tuple)):
        if len(a) == 4:
            return unreal.Margin(float(a[0]), float(a[1]), float(a[2]), float(a[3]))
        if len(a) == 2:
            return unreal.Margin(float(a[0]), float(a[1]), float(a[0]), float(a[1]))
        if len(a) == 1:
            return unreal.Margin(float(a[0]), float(a[0]), float(a[0]), float(a[0]))
    return None
def _ww_mk_anchors(a):
    an = unreal.Anchors()
    if isinstance(a, (list, tuple)):
        if len(a) == 4:
            an.minimum = unreal.Vector2D(float(a[0]), float(a[1])); an.maximum = unreal.Vector2D(float(a[2]), float(a[3])); return an
        if len(a) == 2:
            an.minimum = unreal.Vector2D(float(a[0]), float(a[1])); an.maximum = unreal.Vector2D(float(a[0]), float(a[1])); return an
    return None
def _ww_mk_childsize(val, rule):
    scs = unreal.SlateChildSize()
    try:
        scs.set_editor_property("value", float(val))
    except Exception:
        pass
    if rule is not None:
        r = getattr(unreal.SlateSizeRule, str(rule), None) or getattr(unreal.SlateSizeRule, str(rule).upper(), None)
        if r is not None:
            try:
                scs.set_editor_property("size_rule", r)
            except Exception:
                pass
    return scs
def _ww_enum_coerce(current, name):
    cls = type(current) if isinstance(current, unreal.EnumBase) else None
    if cls is None:
        return None
    for cand in (str(name), str(name).upper()):
        e = getattr(cls, cand, None)
        if e is not None:
            return e
    return None
def _ww_desr(current, value):
    if isinstance(value, dict):
        if "__v2__" in value: return _ww_v2(value["__v2__"])
        if "__v3__" in value:
            a = value["__v3__"]; return unreal.Vector(float(a[0]), float(a[1]), float(a[2]))
        if "__margin__" in value: return _ww_mk_margin(value["__margin__"])
        if "__anchors__" in value: return _ww_mk_anchors(value["__anchors__"])
        if "__childsize__" in value:
            a = value["__childsize__"]; return _ww_mk_childsize(a[0], a[1] if len(a) > 1 else None)
        if "__lincolor__" in value:
            a = value["__lincolor__"]; return unreal.LinearColor(float(a[0]), float(a[1]), float(a[2]), float(a[3]))
        if "__color__" in value:
            a = value["__color__"]; return unreal.Color(r=int(a[0]), g=int(a[1]), b=int(a[2]), a=int(a[3]))
        if "__enum__" in value:
            e = _ww_enum_coerce(current, value["__enum__"]); return e if e is not None else current
        if "__object__" in value:
            p = value["__object__"]; return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(current, unreal.EnumBase):
        e = _ww_enum_coerce(current, value); return e if e is not None else current
    if isinstance(current, unreal.Text):
        return str(value)
    if isinstance(current, unreal.Name):
        return unreal.Name(str(value))
    if isinstance(current, bool):
        return bool(value)
    return value
def _ww_slot_get(slot, prop):
    g = getattr(slot, "get_" + prop, None)
    if g is not None:
        try:
            return g()
        except Exception:
            pass
    try:
        return slot.get_editor_property(prop)
    except Exception:
        return None
def _ww_slot_set(slot, prop, val):
    s = getattr(slot, "set_" + prop, None)
    if s is not None:
        try:
            s(val); return True
        except Exception:
            pass
    try:
        slot.set_editor_property(prop, val); return True
    except Exception:
        return False
def _scg_nname(n):
    try:
        return n.get_name()
    except Exception:
        return None
def _scg_children(n):
    try:
        return list(n.get_editor_property("child_nodes") or [])
    except Exception:
        return []
def _scg_load_cue(path):
    try:
        o = unreal.load_object(None, path)
        if o is not None:
            return o
    except Exception:
        pass
    try:
        return unreal.EditorAssetLibrary.load_asset(path)
    except Exception:
        return None
def _scg_resolve(cue, ident):
    if not ident or cue is None:
        return None
    pth = str(ident) if ":" in str(ident) else (cue.get_path_name() + ":" + str(ident))
    try:
        o = unreal.load_object(None, pth)
        if isinstance(o, unreal.SoundNode):
            return o
    except Exception:
        pass
    found = {"n": None}
    seen = set()
    def _w(n):
        if n is None or found["n"] is not None:
            return
        nm = _scg_nname(n)
        if nm in seen:
            return
        seen.add(nm)
        if nm == str(ident):
            found["n"] = n
            return
        for c in _scg_children(n):
            _w(c)
    try:
        _w(cue.get_editor_property("first_node"))
    except Exception:
        pass
    return found["n"]
def _scg_write_children(node, names, cue):
    arr = unreal.Array(unreal.SoundNode)
    for nm in (names or []):
        if nm is None:
            continue
        r = _scg_resolve(cue, nm)
        if r is not None:
            try:
                arr.append(r)
            except Exception:
                pass
    node.set_editor_property("child_nodes", arr)
def _scg_restore_prop(node, key, cap):
    k = cap.get("kind")
    if k == "none":
        node.set_editor_property(key, None)
    elif k == "scalar":
        node.set_editor_property(key, cap.get("value"))
    elif k == "enum":
        node.set_editor_property(key, getattr(getattr(unreal, cap.get("enum_type")), cap.get("member")))
    elif k == "object":
        _p = cap.get("path")
        node.set_editor_property(key, unreal.EditorAssetLibrary.load_asset(_p) if _p else None)
    elif k in ("name", "text"):
        node.set_editor_property(key, cap.get("value"))
def _scg_save(cue):
    try:
        unreal.EditorAssetLibrary.save_asset(cue.get_path_name(), only_if_is_dirty=False)
    except Exception:
        pass
def _mg_save(path):
    try:
        _o = unreal.EditorAssetLibrary.load_asset(path)
        if _o is not None:
            unreal.EditorLoadingAndSavingUtils.save_packages([_o.get_outermost()], False)
    except Exception:
        pass
def _ms_fobb(path):
    # load a MetaSound + return an edit-in-place builder (find_or_begin_building), or None.
    try:
        ms = unreal.EditorAssetLibrary.load_asset(path)
        if ms is None:
            return None
        es = unreal.get_editor_subsystem(unreal.MetaSoundEditorSubsystem)
        b, r = es.find_or_begin_building(ms)
        return b
    except Exception:
        return None
def _ms_node(guid):
    h = unreal.MetaSoundNodeHandle()
    h.import_text("(NodeID=" + str(guid) + ")")
    return h
def _ms_lit(text):
    lit = unreal.MetasoundFrontendLiteral()
    try:
        if text:
            lit.import_text(text)
    except Exception:
        pass
    return lit
def _ms_save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
_PCG_IN_TOK = ("input", "__input__", "in", "inputnode")
_PCG_OUT_TOK = ("output", "__output__", "out", "outputnode")
def _pcg_load_graph(p):
    a = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if a is None or not isinstance(a, unreal.PCGGraph):
        return None
    return a
def _pcg_resolve_node(g, node_id):
    if node_id is None or g is None:
        return None
    key = str(node_id); kl = key.lower()
    if kl in _PCG_IN_TOK:
        return g.get_input_node()
    if kl in _PCG_OUT_TOK:
        return g.get_output_node()
    for n in list(g.get_editor_property("nodes") or []):
        try:
            if n.get_name() == key:
                return n
        except Exception:
            pass
    for getter in (g.get_input_node, g.get_output_node):
        try:
            nn = getter()
            if nn is not None and nn.get_name() == key:
                return nn
        except Exception:
            pass
    return None
def _pcg_save(g):
    try:
        unreal.EditorLoadingAndSavingUtils.save_packages([g.get_outermost()], False)
    except Exception:
        pass
def _pcg_enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _pcg_coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        _p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(_p) if _p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Vector2D) and isinstance(value, (list, tuple)) and len(value) >= 2:
        return unreal.Vector2D(float(value[0]), float(value[1]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        _aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), _aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(_aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        _obj = None
        try:
            _obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            _obj = None
        if _obj is not None:
            return _obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except Exception:
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except Exception:
            return current
    return value
def _pcg_resolve_settings_class(name):
    base = getattr(unreal, "PCGSettings", None)
    if base is None or not name:
        return None
    def _ok(c):
        try:
            return isinstance(c, type) and issubclass(c, base) and c is not base
        except Exception:
            return False
    cand = getattr(unreal, name, None)
    if _ok(cand):
        return cand
    compact = str(name).replace("_", "").replace(" ", "").lower()
    for nm in dir(unreal):
        c = getattr(unreal, nm, None)
        if _ok(c) and nm.lower() in (compact, "pcg" + compact, compact + "settings", "pcg" + compact + "settings"):
            return c
    for nm in dir(unreal):
        c = getattr(unreal, nm, None)
        if _ok(c) and compact in nm.lower():
            return c
    return None
def _sq_find_binding(seq, sel):
    for b in (seq.get_bindings() or []):
        dn = None; nm = None
        try:
            dn = str(b.get_display_name())
        except Exception:
            pass
        try:
            nm = str(b.get_name())
        except Exception:
            pass
        if sel in (dn, nm):
            return b
    return None
def _sq_container(seq, binding_name):
    if binding_name in (None, ""):
        return seq
    return _sq_find_binding(seq, binding_name)
def _sq_exact_tracks(container, cls):
    try:
        return list(container.find_tracks_by_exact_type(cls) or [])
    except Exception:
        out = []
        for t in (container.get_tracks() or []):
            try:
                if t.get_class() == cls or t.get_class().get_name() == cls.__name__:
                    out.append(t)
            except Exception:
                pass
        return out
def _sq_track_list(seq, binding_sel):
    if binding_sel in (None, ""):
        return list(seq.get_tracks() or []), None
    b = _sq_find_binding(seq, binding_sel)
    if b is None:
        return None, "binding not found: %s" % binding_sel
    return list(b.get_tracks() or []), None
def _sq_locate_track(seq, binding_sel, track_index):
    tl, err = _sq_track_list(seq, binding_sel)
    if err:
        return None, err
    ti = int(track_index)
    if ti < 0 or ti >= len(tl):
        return None, "track_index %d out of range (track_count=%d)" % (ti, len(tl))
    return tl[ti], None
def _sq_locate_section(seq, binding_sel, track_index, section_index):
    tr, err = _sq_locate_track(seq, binding_sel, track_index)
    if err:
        return None, None, err
    secs = list(tr.get_sections() or [])
    si = int(section_index)
    if si < 0 or si >= len(secs):
        return None, tr, "section_index %d out of range (section_count=%d)" % (si, len(secs))
    return secs[si], tr, None
def _sq_emap(cls, pairs):
    m = {}
    for nm, attr in pairs:
        v = getattr(cls, attr, None)
        if v is not None:
            m[nm] = v
    return m
_SQ_INTERP = _sq_emap(unreal.RichCurveInterpMode, [("constant", "RCIM_CONSTANT"), ("linear", "RCIM_LINEAR"), ("cubic", "RCIM_CUBIC")])
_SQ_TANMODE = _sq_emap(unreal.RichCurveTangentMode, [("auto", "RCTM_AUTO"), ("user", "RCTM_USER"), ("break", "RCTM_BREAK")])
_SQ_EXTRAP = _sq_emap(unreal.RichCurveExtrapolation, [("constant", "RCCE_CONSTANT"), ("cycle", "RCCE_CYCLE"), ("cycle_with_offset", "RCCE_CYCLE_WITH_OFFSET"), ("oscillate", "RCCE_OSCILLATE"), ("linear", "RCCE_LINEAR")])
_SQ_BLEND = _sq_emap(unreal.MovieSceneBlendType, [("absolute", "ABSOLUTE"), ("additive", "ADDITIVE"), ("relative", "RELATIVE"), ("additive_from_base", "ADDITIVE_FROM_BASE"), ("override", "OVERRIDE")])
def _sq_name_of(m, v):
    for k2, vv in m.items():
        try:
            if vv == v:
                return k2
        except Exception:
            pass
    return None
def _sq_enum_of(m, name):
    if name is None:
        return None
    return m.get(str(name).strip().lower())
def _sq_jval(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return float(v)
    except Exception:
        try:
            return str(v)
        except Exception:
            return None
def _sq_rebuild_channel(ch, cd):
    try:
        if cd.get("has_default"):
            ch.set_default(cd.get("default"))
    except Exception:
        pass
    try:
        pe = _sq_enum_of(_SQ_EXTRAP, cd.get("pre")); po = _sq_enum_of(_SQ_EXTRAP, cd.get("post"))
        if pe is not None: ch.set_pre_infinity_extrapolation(pe)
        if po is not None: ch.set_post_infinity_extrapolation(po)
    except Exception:
        pass
    for kd in cd.get("keys", []):
        try:
            k = ch.add_key(unreal.FrameNumber(int(kd["frame"])), kd["value"])
        except Exception:
            continue
        try:
            im = _sq_enum_of(_SQ_INTERP, kd.get("interp"))
            if im is not None: k.set_interpolation_mode(im)
            tm = _sq_enum_of(_SQ_TANMODE, kd.get("tanmode"))
            if tm is not None: k.set_tangent_mode(tm)
            if kd.get("arrive") is not None: k.set_arrive_tangent(float(kd["arrive"]))
            if kd.get("leave") is not None: k.set_leave_tangent(float(kd["leave"]))
        except Exception:
            pass
def _sq_rebuild_section(sec, sd):
    try:
        if sd.get("has_start") and sd.get("has_end"):
            sec.set_range(int(sd["start"]), int(sd["end"]))
        else:
            if sd.get("has_start") and sd.get("start") is not None: sec.set_start_frame(int(sd["start"]))
            if sd.get("has_end") and sd.get("end") is not None: sec.set_end_frame(int(sd["end"]))
    except Exception:
        pass
    try:
        if sd.get("row") is not None: sec.set_row_index(int(sd["row"]))
    except Exception:
        pass
    try:
        be = _sq_enum_of(_SQ_BLEND, sd.get("blend"))
        if be is not None: sec.set_blend_type(be)
    except Exception:
        pass
    try:
        if sd.get("ease_in") is not None: sec.set_ease_in_duration(int(sd["ease_in"]))
        if sd.get("ease_out") is not None: sec.set_ease_out_duration(int(sd["ease_out"]))
    except Exception:
        pass
    refs = sd.get("refs") or {}
    try:
        if refs.get("sound"):
            sec.set_sound(unreal.EditorAssetLibrary.load_asset(refs["sound"]))
        if refs.get("animation"):
            p = sec.get_editor_property("params"); p.set_editor_property("animation", unreal.EditorAssetLibrary.load_asset(refs["animation"])); sec.set_editor_property("params", p)
        if refs.get("play_rate") is not None:
            v = unreal.MovieSceneTimeWarpVariant(); v.set_fixed_play_rate(float(refs["play_rate"])); sec.set_editor_property("time_warp", v)
    except Exception:
        pass
    chans = list(sec.get_all_channels() or [])
    for i, cd in enumerate(sd.get("channels", [])):
        if i < len(chans):
            _sq_rebuild_channel(chans[i], cd)
def _sq_rebuild_track(tr, td):
    for sd in td.get("sections", []):
        try:
            sec = tr.add_section()
        except Exception:
            continue
        _sq_rebuild_section(sec, sd)
def _sq_keyframe_val(k):
    return int(k.get_time().frame_number.value)
def _sq_inv_add_key(ent):
    seq = unreal.EditorAssetLibrary.load_asset(ent["asset_path"])
    if seq is None:
        return
    sec, tr, err = _sq_locate_section(seq, ent.get("binding"), ent.get("track_index", 0), ent.get("section_index", 0))
    if err or sec is None:
        return
    chans = list(sec.get_all_channels() or [])
    ci = int(ent["channel_index"])
    if ci < 0 or ci >= len(chans):
        return
    ch = chans[ci]
    frame = int(ent["frame"])
    target = None
    for k in (ch.get_keys() or []):
        if _sq_keyframe_val(k) == frame:
            target = k; break
    if ent.get("had_key"):
        ch.add_key(unreal.FrameNumber(frame), ent.get("prior_value"))
    else:
        if target is not None:
            ch.remove_key(target)
for _ in range(count):
    if not led:
        break
    entry = led.pop()
    op = entry.get("op")
    if op == "spawn_actor":
        target = _find_by_name(entry["actor_name"])
        if target:
            with unreal.ScopedEditorTransaction("MCP undo spawn_actor"):
                eas.destroy_actor(target)
            undone.append({**entry, "result": "deleted"})
        else:
            undone.append({**entry, "result": "already-absent"})
    elif op == "set_actor_transform":
        target = _find_by_name(entry["actor_name"])
        if target:
            p = entry["prior"]
            with unreal.ScopedEditorTransaction("MCP undo set_actor_transform"):
                target.set_actor_location(unreal.Vector(p["loc"][0], p["loc"][1], p["loc"][2]), False, False)
                target.set_actor_rotation(unreal.Rotator(pitch=p["rot"][0], yaw=p["rot"][1], roll=p["rot"][2]), False)
                target.set_actor_scale3d(unreal.Vector(p["scale"][0], p["scale"][1], p["scale"][2]))
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent"})
    elif op == "set_actor_label":
        target = _find_by_name(entry["actor_name"])
        if target:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_label"):
                target.set_actor_label(entry["prior_label"])
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent"})
    elif op == "delete_actor":
        target = _find_by_name(entry["actor_name"])
        if target:
            pf = entry.get("prior_folder", "")
            with unreal.ScopedEditorTransaction("MCP undo delete_actor"):
                target.set_folder_path(unreal.Name("" if pf in ("None", "") else pf))
                target.set_is_temporarily_hidden_in_editor(bool(entry.get("was_hidden", False)))
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent (purged?)"})
    elif op == "set_actor_property":
        if not entry.get("restorable", True):
            undone.append({**entry, "result": "not-restorable (opaque struct); skipped"})
            continue
        target = _find_by_name(entry["actor_name"])
        if target is None:
            undone.append({**entry, "result": "actor-absent"}); continue
        cont, final, err = _descend(target, entry.get("component_name"), entry["path"])
        if err:
            undone.append({**entry, "result": err})
        else:
            newv = _coerce(cont.get_editor_property(final), entry["prior"])
            with unreal.ScopedEditorTransaction("MCP undo set_actor_property"):
                cont.set_editor_property(final, newv)
            undone.append({**entry, "result": "restored"})
    elif op == "select_actors" or op == "deselect_all_actors":
        # cross-module (editor_selection.py): restore the prior viewport selection
        names = entry.get("prior_selection", []) or []
        restore = [a for a in (_find_by_name(n) for n in names) if a]
        eas2 = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        with unreal.ScopedEditorTransaction("MCP undo selection"):
            eas2.set_selected_level_actors(restore)
        undone.append({**entry, "result": "selection-restored"})
    elif op == "set_object_property":
        # cross-module (objects.py). Scalar restore is supported; array restore is deferred.
        if entry.get("mode") != "scalar" or not entry.get("restorable", True):
            undone.append({**entry, "result": "skipped (array/opaque undo not yet supported)"})
            continue
        tsel = entry.get("target", {}) or {}
        root = None
        if tsel.get("actor"):
            root = _resolve_actor(tsel["actor"])
        elif tsel.get("asset_path"):
            try: root = unreal.EditorAssetLibrary.load_asset(tsel["asset_path"])
            except Exception: root = None
        if root is None:
            undone.append({**entry, "result": "target-absent"}); continue
        cont, final, err = _descend(root, tsel.get("component"), entry["path"])
        if err:
            undone.append({**entry, "result": err})
        else:
            newv = _coerce(cont.get_editor_property(final), entry["prior"])
            with unreal.ScopedEditorTransaction("MCP undo set_object_property"):
                cont.set_editor_property(final, newv)
            undone.append({**entry, "result": "restored"})
    elif op == "move_to_folder":
        # cross-module (editor_organize.py): restore each actor's prior outliner folder
        with unreal.ScopedEditorTransaction("MCP undo move_to_folder"):
            for mv in entry.get("moves", []) or []:
                t = _find_by_name(mv.get("actor_name"))
                if t and hasattr(t, "set_folder_path"):
                    pf = mv.get("prior_folder", "")
                    t.set_folder_path(unreal.Name("" if pf in ("None", "") else pf))
        undone.append({**entry, "result": "folders-restored"})
    elif op == "attach_actors" or op == "detach_actors":
        # restore each child's prior parent (re-attach), or detach if it had none
        key = "attaches" if op == "attach_actors" else "detaches"
        with unreal.ScopedEditorTransaction("MCP undo attach/detach"):
            for it in entry.get(key, []) or []:
                c = _find_by_name(it.get("child"))
                if not c or not hasattr(c, "attach_to_actor"):
                    continue
                pp = it.get("prior_parent")
                if pp:
                    p = _find_by_name(pp)
                    if p:
                        c.attach_to_actor(p, "", unreal.AttachmentRule.KEEP_WORLD,
                                          unreal.AttachmentRule.KEEP_WORLD, unreal.AttachmentRule.KEEP_WORLD, False)
                elif hasattr(c, "detach_from_actor"):
                    c.detach_from_actor(unreal.DetachmentRule.KEEP_WORLD,
                                        unreal.DetachmentRule.KEEP_WORLD, unreal.DetachmentRule.KEEP_WORLD)
        undone.append({**entry, "result": "attachment-reverted"})
    elif op == "group_actors" or op == "ungroup_actors":
        # inverse of group is ungroup (and vice-versa) on the recorded members
        members = [m for m in (_find_by_name(n) for n in (entry.get("members", []) or [])) if m]
        gu = unreal.ActorGroupingUtils.get_default_object()
        with unreal.ScopedEditorTransaction("MCP undo grouping"):
            if members:
                if op == "group_actors":
                    gu.ungroup_actors(members)
                else:
                    gu.group_actors(members)
        undone.append({**entry, "result": ("ungrouped" if op == "group_actors" else "regrouped")})
    elif op == "set_material":
        # cross-module (materials_assign.py): restore each touched slot's prior material
        target = _find_by_name(entry.get("actor_name"))
        comp = None
        if target and hasattr(target, "get_components_by_class"):
            cn = entry.get("component_name")
            for c in (target.get_components_by_class(unreal.ActorComponent) or []):
                if c.get_name() == cn:
                    comp = c; break
        if comp is None:
            undone.append({**entry, "result": "component-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_material"):
                for sl in entry.get("slots", []) or []:
                    prior = sl.get("prior")
                    mat = unreal.EditorAssetLibrary.load_asset(prior) if prior else None
                    comp.set_material(int(sl["index"]), mat)
            undone.append({**entry, "result": "materials-restored"})
    elif op == "add_component":
        # cross-module (editor_actor_components.py): delete the instanced component we added
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            root = handles[0] if handles else None
            cname = entry.get("component_name")
            victim = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == cname:
                    victim = h; break
            if victim is None or root is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo add_component"):
                    sods.k2_delete_subobject_from_instance(root, victim)
                undone.append({**entry, "result": "component-removed"})
    elif op == "rename_component":
        # cross-module (editor_actor_components.py): rename the component back to old_name
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            new_name = entry.get("new_name"); old_name = entry.get("old_name")
            h_found = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == new_name:
                    h_found = h; break
            if h_found is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                ok = sods.rename_subobject(h_found, old_name)
                undone.append({**entry, "result": ("renamed-back" if ok else "rename-failed")})
    elif op == "set_component_transform":
        # cross-module (editor_actor_components.py): restore the component's relative transform
        target = _find_by_name(entry.get("actor_name"))
        comp = None
        if target and hasattr(target, "get_components_by_class"):
            cn = entry.get("component_name")
            for c in (target.get_components_by_class(unreal.ActorComponent) or []):
                if c.get_name() == cn:
                    comp = c; break
        if comp is None:
            undone.append({**entry, "result": "component-absent"})
        else:
            prior = entry.get("prior") or {}
            with unreal.ScopedEditorTransaction("MCP undo set_component_transform"):
                if prior.get("loc") is not None:
                    v = prior["loc"]; comp.set_editor_property("relative_location", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                if prior.get("rot") is not None:
                    v = prior["rot"]; comp.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2])))
                if prior.get("scale") is not None:
                    v = prior["scale"]; comp.set_editor_property("relative_scale3d", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
            undone.append({**entry, "result": "transform-restored"})
    elif op == "set_actor_mobility":
        # cross-module (editor_actor_edit.py): restore the root component's prior mobility
        target = _find_by_name(entry.get("actor_name"))
        root = target.get_editor_property("root_component") if target else None
        if root is None:
            undone.append({**entry, "result": "actor-or-root-absent"})
        else:
            pm = getattr(unreal.ComponentMobility, entry.get("prior_mobility", "STATIC"), None)
            with unreal.ScopedEditorTransaction("MCP undo set_actor_mobility"):
                if pm is not None:
                    root.set_editor_property("mobility", pm)
            undone.append({**entry, "result": "mobility-restored"})
    elif op == "set_actor_tags":
        # cross-module (editor_actor_edit.py): restore the FULL prior Actor.Tags array
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_tags"):
                target.set_editor_property("tags", [unreal.Name(s) for s in (entry.get("prior_tags") or [])])
            undone.append({**entry, "result": "tags-restored"})
    elif op == "set_actor_collision_enabled":
        # cross-module (editor_actor_edit.py): restore prior collision-enabled state
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_collision_enabled"):
                target.set_actor_enable_collision(bool(entry.get("prior_enabled")))
            undone.append({**entry, "result": "collision-restored"})
    elif op == "create_layer":
        # cross-module (editor_layers.py): delete the layer we created
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        ls.delete_layer(unreal.Name(entry.get("layer_name")))
        undone.append({**entry, "result": "layer-deleted"})
    elif op == "delete_layer":
        # cross-module (editor_layers.py): recreate the layer + re-add its prior members
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        ls.create_layer(lname)
        actors = [a for a in (_find_by_name(n) for n in (entry.get("members") or [])) if a]
        if actors:
            ls.add_actors_to_layer(actors, lname)
        undone.append({**entry, "result": "layer-recreated"})
    elif op == "add_actors_to_layer":
        # cross-module (editor_layers.py): remove from the layer the actors we added
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
        if actors:
            ls.remove_actors_from_layer(actors, lname)
        undone.append({**entry, "result": "actors-removed-from-layer"})
    elif op == "remove_actors_from_layer":
        # cross-module (editor_layers.py): re-add to the layer the actors we removed
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
        if actors:
            ls.add_actors_to_layer(actors, lname)
        undone.append({**entry, "result": "actors-re-added-to-layer"})
    elif op == "set_world_setting":
        # cross-module (world_settings.py): restore the prior WorldSettings scalar/bool value
        if not entry.get("restorable", True):
            undone.append({**entry, "result": "not-restorable-skipped"})
        else:
            ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
            world = ues.get_editor_world() if ues else None
            if world is None:
                world = unreal.EditorLevelLibrary.get_editor_world()
            ws = world.get_world_settings() if world else None
            if ws is None:
                undone.append({**entry, "result": "world-settings-absent"})
            else:
                prop = entry.get("property")
                cur = ws.get_editor_property(prop)
                with unreal.ScopedEditorTransaction("MCP undo set_world_setting"):
                    ws.set_editor_property(prop, _coerce(cur, entry.get("prior")))
                undone.append({**entry, "result": "world-setting-restored"})
    elif op == "duplicate_asset":
        # cross-module (assets_write.py): delete the duplicate we created (+ dir if we made it)
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is not None:
            try:
                aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                if aes:
                    aes.close_all_editors_for_asset(obj)
            except Exception:
                pass
        unreal.SystemLibrary.collect_garbage()
        deleted = unreal.EditorAssetLibrary.delete_asset(ap) if ap else False
        cd = entry.get("created_dir")
        if deleted and cd:
            try:
                if not (unreal.EditorAssetLibrary.list_assets(cd, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cd)
            except Exception:
                pass
        undone.append({**entry, "result": ("duplicate-deleted" if deleted else "delete-failed")})
    elif op == "rename_asset":
        # cross-module (assets_write.py): rename back to the original path
        frm = entry.get("from_path"); to = entry.get("to_path")
        ok = unreal.EditorAssetLibrary.rename_asset(to, frm) if (frm and to) else False
        cd = entry.get("created_dir")
        if ok and cd:
            try:
                if not (unreal.EditorAssetLibrary.list_assets(cd, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cd)
            except Exception:
                pass
        undone.append({**entry, "result": ("rename-reverted" if ok else "revert-failed")})
    elif op == "create_folder":
        # cross-module (assets_write.py): remove the topmost folder we created, if still empty
        cr = entry.get("created_root")
        res = "no-dir-created"
        if cr:
            try:
                if unreal.EditorAssetLibrary.does_directory_exist(cr) and not (unreal.EditorAssetLibrary.list_assets(cr, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cr); res = "folder-removed"
                else:
                    res = "folder-not-empty-kept"
            except Exception:
                res = "folder-remove-failed"
        undone.append({**entry, "result": res})
    elif op == "delete_folder":
        # cross-module (assets_write.py): recreate the empty folder we deleted
        p = entry.get("path")
        ok = unreal.EditorAssetLibrary.make_directory(p) if p else False
        undone.append({**entry, "result": ("folder-recreated" if ok else "recreate-failed")})
    elif op == "soft_delete_asset":
        # cross-module (assets_write.py): move the asset back out of the trash folder
        orig = entry.get("original_path"); trash = entry.get("trash_path")
        ok = unreal.EditorAssetLibrary.rename_asset(trash, orig) if (orig and trash) else False
        if ok and entry.get("created_trash_dir") and trash:
            try:
                trash_dir = trash.rsplit("/", 1)[0]
                if not (unreal.EditorAssetLibrary.list_assets(trash_dir, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(trash_dir)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-restored" if ok else "restore-failed")})
    elif op == "add_actors_to_data_layer" or op == "remove_actors_from_data_layer":
        # cross-module (datalayers.py): reverse a WP data-layer membership change.
        # Resolve the instance from the live set by asset path -> full name -> short name.
        dls = unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
        instances = list(dls.get_all_data_layers() or [])
        def _dl_asset_path(di):
            a = None
            for m in ("get_asset", "get_data_layer_asset"):
                try:
                    a = getattr(di, m)()
                    if a:
                        break
                except Exception:
                    a = None
            try:
                return a.get_path_name() if a else None
            except Exception:
                return None
        def _dl_short(di):
            for m in ("get_data_layer_short_name", "get_data_layer_instance_name"):
                f = getattr(di, m, None)
                if f:
                    try:
                        v = str(f())
                        if v:
                            return v
                    except Exception:
                        pass
            try:
                return di.get_name()
            except Exception:
                return None
        def _dl_full(di):
            try:
                return str(di.get_data_layer_full_name())
            except Exception:
                return None
        target = None
        if entry.get("dl_asset"):
            for di in instances:
                if _dl_asset_path(di) == entry.get("dl_asset"):
                    target = di; break
        if target is None:
            for di in instances:
                if (entry.get("dl_full") and _dl_full(di) == entry.get("dl_full")) or \
                   (entry.get("dl_short") and _dl_short(di) == entry.get("dl_short")):
                    target = di; break
        if target is None:
            undone.append({**entry, "result": "data-layer-absent"})
        else:
            actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
            if actors:
                with unreal.ScopedEditorTransaction("MCP undo data_layer membership"):
                    if op == "add_actors_to_data_layer":
                        dls.remove_actors_from_data_layer(actors, target)
                    else:
                        dls.add_actors_to_data_layer(actors, target)
            undone.append({**entry, "result": ("actors-removed-from-dl" if op == "add_actors_to_data_layer" else "actors-re-added-to-dl")})
    elif op == "create_asset":
        # GENERIC created-asset inverse (ai_write.py + future create_* commands): delete the asset
        # we created. IMPORTANT: null the loaded ref before GC+delete (a live `obj` local keeps the
        # asset referenced → delete fails). Close any auto-opened editor first; retry once after GC.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                _o = unreal.EditorAssetLibrary.load_asset(ap)
                if _o is not None:
                    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                    if aes:
                        aes.close_all_editors_for_asset(_o)
                    # BehaviorTree guard (2026-08-16): null the hand-built root so its node subobjects
                    # release before delete — a POPULATED BT otherwise trips the "asset in use" force-
                    # delete MODAL + "potentially corrupt" (see [[unrealmcp-command-buildout]] modal-poppers).
                    # LIFO undo usually empties the tree first; this makes even a direct create-undo safe.
                    if isinstance(_o, unreal.BehaviorTree):
                        try:
                            _o.set_editor_property("root_node", None)
                            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                        except Exception:
                            pass
                _o = None
        except Exception:
            pass
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        if not deleted and entry.get("created_dir") and pkg:
            # Fallback for a freshly-created unsaved asset that resists per-asset delete inside the
            # multi-op undo snippet: force-remove the whole scratch dir we created (all its assets).
            try:
                unreal.SystemLibrary.collect_garbage()
                unreal.EditorAssetLibrary.delete_directory(pkg)
                deleted = not unreal.EditorAssetLibrary.does_asset_exist(ap)
            except Exception:
                pass
        elif deleted and entry.get("created_dir") and pkg:
            try:
                remaining = unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []
                if not remaining:
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-deleted" if deleted else "delete-failed")})
    elif op == "add_blackboard_key":
        # cross-module (ai_write.py): remove the key we added from the BlackboardData
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bb is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            kn = str(entry.get("key_name"))
            keys = bb.get_editor_property("keys") or []
            kept = [k for k in keys if str(k.get_editor_property("entry_name")) != kn]
            with unreal.ScopedEditorTransaction("MCP undo add_blackboard_key"):
                bb.set_editor_property("keys", kept)
            undone.append({**entry, "result": "key-removed"})
            keys = None; kept = None; bb = None  # release refs so a later create_asset delete can succeed
    elif op == "set_behavior_tree_blackboard":
        # cross-module (ai_write.py): restore the BT's prior blackboard asset
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bt is None:
            undone.append({**entry, "result": "behavior-tree-absent"})
        else:
            pb = entry.get("prior_blackboard")
            prior = unreal.EditorAssetLibrary.load_asset(pb) if pb else None
            with unreal.ScopedEditorTransaction("MCP undo set_behavior_tree_blackboard"):
                bt.set_editor_property("blackboard_asset", prior)
            undone.append({**entry, "result": "blackboard-restored"})
            bt = None; prior = None  # release refs so a later create_asset delete can succeed
    elif op == "set_material_instance_param":
        # cross-module (material_write.py): restore a MIC param override (or clear it if it wasn't set)
        ap = entry.get("asset_path")
        mic = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mic is None:
            undone.append({**entry, "result": "material-instance-absent"})
        else:
            G = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
            MEL = unreal.MaterialEditingLibrary
            nm = entry.get("parameter_name"); kind = entry.get("param_kind"); prior = entry.get("prior_value")
            with unreal.ScopedEditorTransaction("MCP undo set_material_instance_param"):
                if entry.get("was_overridden"):
                    if kind == "scalar":
                        MEL.set_material_instance_scalar_parameter_value(mic, nm, float(prior), G)
                    elif kind == "vector":
                        aa = float(prior[3]) if (isinstance(prior, (list, tuple)) and len(prior) > 3) else 1.0
                        MEL.set_material_instance_vector_parameter_value(mic, nm, unreal.LinearColor(float(prior[0]), float(prior[1]), float(prior[2]), aa), G)
                    elif kind == "texture":
                        tex = unreal.EditorAssetLibrary.load_asset(prior) if prior else None
                        MEL.set_material_instance_texture_parameter_value(mic, nm, tex, G)
                else:
                    MEL.set_material_instance_parameter_override(mic, nm, False, G)
                MEL.update_material_instance(mic)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            mic = None
            undone.append({**entry, "result": "param-restored"})
    elif op == "set_sequence_playback_range":
        # cross-module (level_sequence_write.py): restore prior playback start/end
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_sequence_playback_range"):
                seq.set_playback_start(int(entry.get("prior_start")))
                seq.set_playback_end(int(entry.get("prior_end")))
            undone.append({**entry, "result": "range-restored"})
            seq = None
    elif op == "add_actor_binding":
        # cross-module (level_sequence_write.py): remove the binding we added
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name"))
            tgt = None
            for b in (seq.get_bindings() or []):
                dn = None
                try:
                    dn = str(b.get_display_name())
                except Exception:
                    dn = None
                if dn == bn or (hasattr(b, "get_name") and str(b.get_name()) == bn):
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                tgt.remove()
                undone.append({**entry, "result": "binding-removed"})
            seq = None
    elif op == "add_transform_track":
        # cross-module (level_sequence_write.py): remove the transform track we added
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name"))
            tgt = None
            for b in (seq.get_bindings() or []):
                try:
                    if str(b.get_display_name()) == bn:
                        tgt = b; break
                except Exception:
                    pass
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                tt = [t for t in (tgt.get_tracks() or []) if t.get_class().get_name() == "MovieScene3DTransformTrack"]
                if len(tt) > int(entry.get("prior_transform_track_count", 0)):
                    tgt.remove_track(tt[-1])
                    undone.append({**entry, "result": "track-removed"})
                else:
                    undone.append({**entry, "result": "track-count-unchanged"})
            seq = None
    elif op == "add_transform_keyframe":
        # cross-module (level_sequence_write.py): restore/remove the keys we set at `frame`
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); frame = int(entry.get("frame"))
            tgt = None
            for b in (seq.get_bindings() or []):
                try:
                    if str(b.get_display_name()) == bn:
                        tgt = b; break
                except Exception:
                    pass
            secs = []
            if tgt is not None:
                for t in (tgt.get_tracks() or []):
                    if t.get_class().get_name() == "MovieScene3DTransformTrack":
                        secs = t.get_sections() or []; break
            chans = secs[0].get_all_channels() if secs else []
            if not chans:
                undone.append({**entry, "result": "section-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo add_transform_keyframe"):
                    for cs in (entry.get("channels") or []):
                        idx = int(cs.get("idx"))
                        if idx >= len(chans):
                            continue
                        ch = chans[idx]
                        for k in (ch.get_keys() or []):
                            if int(k.get_time().frame_number.value) == frame:
                                if cs.get("had_key"):
                                    k.set_value(cs.get("prior_value"))
                                else:
                                    ch.remove_key(k)
                                break
                undone.append({**entry, "result": "keyframe-reverted"})
            seq = None
    elif op == "create_blueprint":
        # cross-module (blueprints_write.py): delete the blueprint asset we created
        # (and its dir if we created it empty). Close any auto-opened editor first, null the
        # loaded ref before GC+delete (a live local keeps it referenced), retry once.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                _o = unreal.EditorAssetLibrary.load_asset(ap)
                if _o is not None:
                    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                    if aes:
                        aes.close_all_editors_for_asset(_o)
                _o = None
        except Exception:
            pass
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        if deleted and entry.get("created_dir") and pkg:
            try:
                remaining = unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []
                if not remaining:
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-deleted" if deleted else "delete-failed")})
    elif op == "add_bp_function":
        # cross-module (blueprints_write.py): remove the (empty) function graph we added
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            unreal.BlueprintEditorLibrary.remove_function_graph(bp, entry.get("func_name"))
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "function-removed"})
    elif op == "reparent_blueprint":
        # cross-module (blueprints_write.py): reparent back to the prior parent class
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        prior_cls = None
        pp = entry.get("prior_parent_path")
        if pp:
            try:
                prior_cls = unreal.load_object(None, pp)
            except Exception:
                prior_cls = None
        if bp is None or prior_cls is None:
            undone.append({**entry, "result": "blueprint-or-parent-absent"})
        else:
            unreal.BlueprintEditorLibrary.reparent_blueprint(bp, prior_cls)
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "reparented-back"})
    elif op == "add_wave_player_to_cue":
        # cross-module (sound_write.py): clear the wave-player node we set as the cue's first_node.
        # FAITHFUL: add_wave_player_to_cue refuses any cue whose first_node is already set, so the
        # prior state is always empty -> restoring first_node=None is exact. Save to persist.
        ap = entry.get("asset_path")
        cue = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if cue is None:
            undone.append({**entry, "result": "cue-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_wave_player_to_cue"):
                cue.set_editor_property("first_node", None)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            undone.append({**entry, "result": "wave-player-cleared"})
            cue = None
    elif op == "add_input_mapping":
        # cross-module (input_write.py): remove the one key->action mapping we added to the IMC.
        # FAITHFUL: add_mapping_to_context refuses duplicates, so the (action,key) pair had 0 prior
        # matches -> unmap_key removes exactly what we added. Save to persist.
        ap = entry.get("asset_path")
        imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ia = unreal.EditorAssetLibrary.load_asset(entry.get("action_path")) if entry.get("action_path") else None
        if imc is None or ia is None:
            undone.append({**entry, "result": "imc-or-action-absent"})
        else:
            key = unreal.Key()
            key.set_editor_property("key_name", unreal.Name(entry.get("key_name")))
            with unreal.ScopedEditorTransaction("MCP undo add_input_mapping"):
                imc.unmap_key(ia, key)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            undone.append({**entry, "result": "mapping-removed"})
            imc = None; ia = None
    elif op == "set_curve_keys":
        # cross-module (curves_write.py): restore the curve's prior key state via the C++ #4 handler.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_curve_keys_json"):
            undone.append({**entry, "result": "curve-or-handler-absent"})
        else:
            mrl.set_curve_keys_json(obj, json.dumps({"channels": entry.get("prior_channels") or []}))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "curve-keys-restored"})
            obj = None
    elif op == "add_struct_field":
        # cross-module (structs_write.py): remove the field we added (matched by friendly name).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_struct_field"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            mrl.remove_struct_field(obj, entry.get("field_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "struct-field-removed"})
            obj = None
    elif op == "remove_struct_field":
        # cross-module (structs_write.py): re-add the removed field (same name + type; new GUID).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ft = entry.get("field_type")
        if obj is None or mrl is None or not hasattr(mrl, "add_struct_field") or not ft:
            undone.append({**entry, "result": "struct-handler-or-type-absent"})
        else:
            mrl.add_struct_field(obj, entry.get("field_name"), ft)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "struct-field-re-added"})
            obj = None
    elif op == "add_enum_entry":
        # cross-module (structs_write.py): remove the enumerator we appended (by index).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        idx = entry.get("index")
        if obj is None or mrl is None or not hasattr(mrl, "remove_enum_entry") or idx is None:
            undone.append({**entry, "result": "enum-or-handler-absent"})
        else:
            mrl.remove_enum_entry(obj, int(idx))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "enum-entry-removed"})
            obj = None
    elif op == "remove_enum_entry":
        # cross-module (structs_write.py): re-add the removed enumerator (APPENDS at end; best-effort).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_enum_entry"):
            undone.append({**entry, "result": "enum-or-handler-absent"})
        else:
            mrl.add_enum_entry(obj, entry.get("prior_display_name") or "")
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "enum-entry-re-added"})
            obj = None
    elif op == "add_emitter":
        # cross-module (niagara_write.py): remove the emitter handle we added (by id). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        hid = entry.get("handle_id")
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_emitter_from_system") or not hid:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_emitter_from_system(sysobj, str(hid))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-removed"})
            sysobj = None
    elif op == "remove_emitter":
        # cross-module (niagara_write.py): re-add the removed emitter from its captured source (best-effort;
        # a copied emitter has no recoverable source, so undo only works if source_emitter_path was given).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        sp = entry.get("source_emitter_path")
        src = unreal.EditorAssetLibrary.load_asset(sp) if sp else None
        if sysobj is None or mrl is None or not hasattr(mrl, "add_emitter_to_system"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif src is None:
            undone.append({**entry, "result": "cannot-restore (no source_emitter_path captured)"})
        else:
            mrl.add_emitter_to_system(sysobj, src, "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-re-added"})
            sysobj = None; src = None
    elif op == "set_niagara_renderer_property":
        # cross-module (niagara_write2.py, C++ #19): re-set the renderer property to the captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_renderer_property"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.set_niagara_renderer_property(ap, entry.get("emitter"), int(entry.get("renderer_index") or 0),
                entry.get("property"), entry.get("prev_value_json") or "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-renderer-property-restored"})
            sysobj = None
    elif op == "set_niagara_renderer_binding":
        # cross-module (niagara_write2.py, C++ #19): restore prior renderer binding source. FAITHFUL (best-effort).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_renderer_binding"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.set_niagara_renderer_binding(ap, entry.get("emitter"), int(entry.get("renderer_index") or 0),
                entry.get("binding"), entry.get("prev_source") or "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-renderer-binding-restored"})
            sysobj = None
    elif op == "duplicate_niagara_emitter":
        # cross-module (niagara_write2.py, C++ #19): remove the duplicated emitter handle (by id). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        hid = entry.get("handle_id")
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_emitter_from_system") or not hid:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_emitter_from_system(sysobj, str(hid))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-duplicate-emitter-removed"})
            sysobj = None
    elif op == "reorder_niagara_emitter":
        # cross-module (niagara_write2.py, C++ #19): reorder the handle back to its prior index. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pi = entry.get("prev_index")
        if sysobj is None or mrl is None or not hasattr(mrl, "reorder_niagara_emitter_handle") or pi is None:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.reorder_niagara_emitter_handle(sysobj, entry.get("emitter"), int(pi))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-reorder-restored"})
            sysobj = None
    elif op == "set_niagara_module_enabled":
        # cross-module (niagara_runtime_cpp.py, C++ #24): re-set the module's prior enabled flag. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_module_enabled"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.set_niagara_module_enabled(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), bool(entry.get("prev_enabled")))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-module-enabled-restored"})
            sysobj = None
    elif op == "reorder_niagara_module":
        # cross-module (niagara_runtime_cpp.py, C++ #29): reorder the module back to its prior index via the SAFE
        # V2 handler (correct index math; the old MoveModule-in-place ReorderNiagaraModule crashed and is retired). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pi = entry.get("prev_index")
        if sysobj is None or mrl is None or not hasattr(mrl, "reorder_niagara_module_v2") or pi is None:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.reorder_niagara_module_v2(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), int(pi))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-module-reorder-restored"})
            sysobj = None
    elif op == "set_niagara_dynamic_input":
        # cross-module (niagara_runtime_cpp.py, C++ #26): clear the created dynamic-input override. FAITHFUL (fresh input).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "clear_niagara_input_override"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif entry.get("had_override"):
            undone.append({**entry, "result": "had-prior-override-skipped-lossy"})
        else:
            mrl.clear_niagara_input_override(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), entry.get("input"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-dynamic-input-cleared"})
            sysobj = None
    elif op == "set_niagara_stack_value":
        # cross-module (niagara_runtime_cpp.py, C++ #26): re-set a prior LOCAL value, else clear the fresh override. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None:
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif entry.get("had_override") and entry.get("prev_value") not in (None, "") and hasattr(mrl, "set_niagara_stack_value"):
            mrl.set_niagara_stack_value(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), entry.get("input"), "local", entry.get("prev_value"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-stack-value-restored"})
            sysobj = None
        elif hasattr(mrl, "clear_niagara_input_override"):
            mrl.clear_niagara_input_override(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), entry.get("input"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-stack-value-cleared"})
            sysobj = None
        else:
            undone.append({**entry, "result": "handler-absent"})
    elif op == "set_niagara_curve":
        # cross-module (niagara_runtime_cpp.py, C++ #26): clear the created curve-DI override. FAITHFUL (fresh input).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "clear_niagara_input_override"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif entry.get("had_override"):
            undone.append({**entry, "result": "had-prior-override-skipped-lossy"})
        else:
            mrl.clear_niagara_input_override(ap, entry.get("emitter"), entry.get("script_usage") or "",
                entry.get("module"), entry.get("input"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "niagara-curve-cleared"})
            sysobj = None
    elif op == "niagara_add_scratch_pad":
        # cross-module (niagara_graph_cpp.py, C++ #27): remove the scratch-pad script we added (by name). FAITHFUL (best-effort).
        ap = entry.get("system_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        nm = str(entry.get("script_name") or "")
        if sysobj is None or not nm:
            undone.append({**entry, "result": "system-or-name-absent"})
        else:
            try:
                _sp = list(sysobj.get_editor_property("scratch_pad_scripts") or [])
                _keep = [s for s in _sp if s is not None and str(s.get_name()) != nm]
                if len(_keep) != len(_sp):
                    sysobj.set_editor_property("scratch_pad_scripts", _keep)
                    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                    except Exception: pass
                    undone.append({**entry, "result": "niagara-scratch-pad-removed"})
                else:
                    undone.append({**entry, "result": "scratch-pad-not-found"})
            except Exception:
                undone.append({**entry, "result": "scratch-pad-remove-failed"})
            sysobj = None
    elif op == "niagara_add_graph_node":
        # cross-module (niagara_graph_cpp.py, C++ #27): delete the graph node we added (by guid). FAITHFUL.
        sp = entry.get("script_path"); mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if not sp or mrl is None or not hasattr(mrl, "delete_niagara_graph_node") or not entry.get("node_guid"):
            undone.append({**entry, "result": "script-or-handler-absent"})
        else:
            mrl.delete_niagara_graph_node(sp, str(entry.get("node_guid")))
            try: unreal.EditorAssetLibrary.save_asset(sp, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "niagara-graph-node-removed"})
    elif op == "niagara_build_graph":
        # cross-module (niagara_graph_cpp.py, C++ #27): delete each node the build created. FAITHFUL.
        sp = entry.get("script_path"); mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if not sp or mrl is None or not hasattr(mrl, "delete_niagara_graph_node"):
            undone.append({**entry, "result": "script-or-handler-absent"})
        else:
            _bn = 0
            for _g in (entry.get("node_guids") or []):
                try: mrl.delete_niagara_graph_node(sp, str(_g)); _bn = _bn + 1
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(sp, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "niagara-build-graph-reverted-" + str(_bn)})
    elif op == "niagara_delete_graph_node":
        # cross-module (niagara_graph_cpp.py, C++ #27): no clean programmatic re-create; editor native undo only.
        undone.append({**entry, "result": "delete-graph-node-non-invertible-native-undo-only"})
    elif op == "niagara_layout_graph":
        # cross-module (niagara_graph_cpp.py, C++ #27): restore the captured prior node positions. FAITHFUL.
        sp = entry.get("script_path"); mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pp = entry.get("prior_positions") or {}
        if not sp or mrl is None or not hasattr(mrl, "layout_niagara_graph") or not pp:
            undone.append({**entry, "result": "script-or-handler-absent"})
        else:
            mrl.layout_niagara_graph(sp, json.dumps({"restore_positions": pp}))
            try: unreal.EditorAssetLibrary.save_asset(sp, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "niagara-layout-restored"})
    elif op == "add_user_param":
        # cross-module (niagara_write.py, C++ #10): remove the user parameter we added. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_user_parameter"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_user_parameter(sysobj, entry.get("param"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-removed"})
            sysobj = None
    elif op == "set_user_param":
        # cross-module (niagara_write.py, C++ #10): restore prior value (captured before the set). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pj = entry.get("prev_value_json", "null")
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_user_parameter_value"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif pj is None or pj == "null":
            undone.append({**entry, "result": "no-prior-value-skipped"})
        else:
            mrl.set_niagara_user_parameter_value(sysobj, entry.get("param"), pj)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-restored"})
            sysobj = None
    elif op == "remove_user_param":
        # cross-module (niagara_write.py, C++ #10): re-add with captured type + value. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        vj = entry.get("value_json", "null")
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_user_parameter"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.add_niagara_user_parameter(sysobj, entry.get("param"), entry.get("type"))
            if vj is not None and vj != "null" and hasattr(mrl, "set_niagara_user_parameter_value"):
                mrl.set_niagara_user_parameter_value(sysobj, entry.get("param"), vj)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-re-added"})
            sysobj = None
    elif op == "rename_niagara_emitter":
        # cross-module (niagara_write.py, C++ #10): rename back new_name -> old_name. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "rename_niagara_emitter_handle"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.rename_niagara_emitter_handle(sysobj, entry.get("new_name"), entry.get("old_name"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-renamed-back"})
            sysobj = None
    elif op == "add_niagara_renderer":
        # cross-module (niagara_write.py, C++ #10): remove the renderer we appended (by index).
        # FAITHFUL if the list did not shift (undo unwinds LIFO, so it holds in practice).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ri = entry.get("renderer_index")
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_renderer") or ri is None:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_renderer(sysobj, entry.get("emitter"), int(ri))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "renderer-removed"})
            sysobj = None
    elif op == "remove_niagara_renderer":
        # cross-module (niagara_write.py, C++ #10): re-add a renderer of the same TYPE. BEST-EFFORT
        # (default props; re-appends at end — custom material/mesh/bindings are not restored).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        rt = entry.get("renderer_type") or ""
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_renderer"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif not rt:
            undone.append({**entry, "result": "cannot-restore (no renderer_type captured)"})
        else:
            mrl.add_niagara_renderer(sysobj, entry.get("emitter"), rt)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "renderer-re-added (best-effort)"})
            sysobj = None
    elif op == "add_module":
        # cross-module (niagara_write.py, C++ #10 area C): remove the module node we added (by guid). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_module_from_stack"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_module_from_stack(sysobj, entry.get("emitter_name"),
                                                 entry.get("script_usage"), entry.get("node_guid"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-removed"})
            sysobj = None
    elif op == "set_module_input":
        # cross-module (niagara_write.py, C++ #10 area C): restore prior scalar input value. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pv = entry.get("prior_value")
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_module_input"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.set_niagara_module_input(sysobj, entry.get("emitter_name"), entry.get("script_usage"),
                                         entry.get("module_name"), entry.get("input_name"), pv if pv is not None else "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-input-restored"})
            sysobj = None
    elif op == "remove_module":
        # cross-module (niagara_write.py, C++ #10 area C): re-add the module. BEST-EFFORT (new guid, default
        # inputs) and only if module_script_path was captured; otherwise cannot restore.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        mp = entry.get("module_script_path") or ""
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_module_to_stack"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif not mp:
            undone.append({**entry, "result": "cannot-restore (no module_script_path captured)"})
        else:
            mrl.add_niagara_module_to_stack(sysobj, entry.get("emitter_name"), entry.get("script_usage"), mp)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-re-added (best-effort, new guid)"})
            sysobj = None
    elif op == "bt_set_root":
        # cross-module (bt_write.py): clear the root composite we set. FAITHFUL (set only on empty root).
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bt is None:
            undone.append({**entry, "result": "bt-absent"})
        else:
            bt.set_editor_property("root_node", None)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-root-cleared"})
    elif op == "bt_add_child":
        # cross-module (bt_write.py): pop the child (composite or task) we appended. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        idx = entry.get("index")
        if bt is None or parent is None or idx is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= idx < len(kids):
                del kids[idx]
                parent.set_editor_property("children", kids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-child-removed"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "bt_add_service":
        # cross-module (bt_write.py): pop the service we appended to a composite. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        comp = _bt_resolve(bt, entry.get("composite_path")) if bt is not None else None
        idx = entry.get("index")
        if bt is None or comp is None or idx is None:
            undone.append({**entry, "result": "bt-or-composite-absent"})
        else:
            svcs = list(comp.get_editor_property("services") or [])
            if 0 <= idx < len(svcs):
                del svcs[idx]
                comp.set_editor_property("services", svcs)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-service-removed"})
            else:
                undone.append({**entry, "result": "bt-service-index-stale"})
    elif op == "bt_add_decorator":
        # cross-module (bt_write.py): pop the decorator we appended to a child slot. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        ci = entry.get("child_index"); idx = entry.get("index")
        if bt is None or parent is None or ci is None or idx is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= ci < len(kids):
                ch = kids[ci]
                decos = list(ch.get_editor_property("decorators") or [])
                if 0 <= idx < len(decos):
                    del decos[idx]
                    ch.set_editor_property("decorators", decos)
                    kids[ci] = ch
                    parent.set_editor_property("children", kids)
                    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                    except Exception: pass
                    undone.append({**entry, "result": "bt-decorator-removed"})
                else:
                    undone.append({**entry, "result": "bt-decorator-index-stale"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "set_cvar":
        # cross-module (console.py): restore the cvar to its captured prior value. FAITHFUL (value
        # restore; the engine's LastSetBy tag reads 'Console' after a programmatic set — cosmetic).
        nm = entry.get("name"); pv = entry.get("prior_value")
        if nm is None or pv is None:
            undone.append({**entry, "result": "cvar-entry-incomplete"})
        else:
            try:
                unreal.SystemLibrary.execute_console_command(None, str(nm) + " " + str(pv))
                unreal.log_flush()
                undone.append({**entry, "result": "cvar-restored"})
            except Exception as _e:
                undone.append({**entry, "result": "cvar-restore-failed"})
    elif op == "datatable_add_row" or op == "datatable_duplicate_row":
        # cross-module (datatable_write.py): the row was absent before this op, so remove it. FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        rn = entry.get("row_name") or entry.get("new_row_name")
        if dt is None or not isinstance(dt, unreal.DataTable) or rn is None:
            undone.append({**entry, "result": "datatable-or-row-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo datatable add/dup row"):
                dt.modify()
                unreal.DataTableFunctionLibrary.remove_data_table_row(dt, rn)
            undone.append({**entry, "result": "datatable-row-removed"})
    elif op == "datatable_remove_row":
        # cross-module (datatable_write.py): re-insert the captured prior row at its original index via the
        # JSON round-trip (restores values AND order). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pe = entry.get("prior_entry"); pidx = entry.get("prior_index")
        if dt is None or not isinstance(dt, unreal.DataTable) or pe is None:
            undone.append({**entry, "result": "datatable-or-prior-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            _ins = int(pidx) if isinstance(pidx, int) and pidx >= 0 else len(_arr)
            if _ins > len(_arr):
                _ins = len(_arr)
            _arr = _arr[:_ins] + [dict(pe)] + _arr[_ins:]
            with unreal.ScopedEditorTransaction("MCP undo datatable remove row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-restored"})
    elif op == "datatable_update_row":
        # cross-module (datatable_write.py): restore the full captured prior row values via the JSON
        # round-trip (field-for-field). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        rn = entry.get("row_name"); pe = entry.get("prior_entry")
        if dt is None or not isinstance(dt, unreal.DataTable) or pe is None or rn is None:
            undone.append({**entry, "result": "datatable-or-prior-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            for _i in range(len(_arr)):
                if str(_arr[_i].get("Name")) == str(rn):
                    _arr[_i] = dict(pe); break
            with unreal.ScopedEditorTransaction("MCP undo datatable update row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-reverted"})
    elif op == "datatable_rename_row":
        # cross-module (datatable_write.py): rename new_name back to old_name via the JSON round-trip
        # (fields + order preserved). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        on = entry.get("old_name"); nn = entry.get("new_name")
        if dt is None or not isinstance(dt, unreal.DataTable) or on is None or nn is None:
            undone.append({**entry, "result": "datatable-or-names-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            for _i in range(len(_arr)):
                if str(_arr[_i].get("Name")) == str(nn):
                    _arr[_i] = dict(_arr[_i]); _arr[_i]["Name"] = on; break
            with unreal.ScopedEditorTransaction("MCP undo datatable rename row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-renamed-back"})
    elif op == "add_anim_notify_track":
        # cross-module (anim_write.py): remove the empty track we added. FAITHFUL (created empty).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_notify_track"):
                unreal.AnimationLibrary.remove_animation_notify_track(anim, tn)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-track-removed"})
    elif op == "remove_anim_notify_track":
        # cross-module (anim_write.py): re-add the (empty) track. FAITHFUL structurally (color -> default).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_notify_track"):
                unreal.AnimationLibrary.add_animation_notify_track(anim, tn, unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-track-readded"})
    elif op == "add_anim_notify":
        # cross-module (anim_write.py): no per-event removal API -> clear the track and rebuild its prior
        # events. FAITHFUL for the added notify; siblings restored by class/time/duration (custom values ->
        # class defaults, documented).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            _AL = unreal.AnimationLibrary
            with unreal.ScopedEditorTransaction("MCP undo add_anim_notify"):
                _AL.remove_animation_notify_events_by_track(anim, tn)
                for _pe in (entry.get("prior_events") or []):
                    _cp = _pe.get("class_path")
                    _cls = None
                    if _cp:
                        try: _cls = unreal.load_object(None, _cp)
                        except Exception: _cls = None
                    if _cls is None:
                        continue
                    if _pe.get("kind") == "state":
                        _AL.add_animation_notify_state_event(anim, tn, float(_pe.get("time") or 0.0), float(_pe.get("duration") or 0.0), _cls)
                    else:
                        _AL.add_animation_notify_event(anim, tn, float(_pe.get("time") or 0.0), _cls)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-notify-track-rebuilt"})
    elif op == "add_anim_sync_marker":
        # cross-module (anim_write.py): remove the marker by name (add enforced name uniqueness). FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mn = entry.get("marker_name")
        if anim is None or mn is None:
            undone.append({**entry, "result": "anim-or-marker-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_sync_marker"):
                unreal.AnimationLibrary.remove_animation_sync_markers_by_name(anim, mn)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-marker-removed"})
    elif op == "add_anim_curve":
        # cross-module (anim_write.py): remove the empty float curve we created. FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_curve"):
                unreal.AnimationLibrary.remove_curve(anim, cn, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-curve-removed"})
    elif op == "remove_anim_curve":
        # cross-module (anim_write.py): re-create the float curve and restore its keys (time+value). FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            _AL = unreal.AnimationLibrary
            _RCTF = unreal.RawCurveTrackTypes.RCT_FLOAT
            _pk = entry.get("prior_keys") or []
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_curve"):
                if not _AL.does_curve_exist(anim, cn, _RCTF):
                    _AL.add_curve(anim, cn, _RCTF, False)
                if _pk:
                    _AL.add_float_curve_keys(anim, cn, [float(k.get("time") or 0.0) for k in _pk], [float(k.get("value") or 0.0) for k in _pk])
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-curve-restored"})
    elif op == "set_anim_curve_key":
        # cross-module (anim_write.py): curve was new -> remove it; else rebuild the prior keys. FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            _AL = unreal.AnimationLibrary
            _RCTF = unreal.RawCurveTrackTypes.RCT_FLOAT
            _pk = entry.get("prior_keys") or []
            with unreal.ScopedEditorTransaction("MCP undo set_anim_curve_key"):
                if _AL.does_curve_exist(anim, cn, _RCTF):
                    _AL.remove_curve(anim, cn, False)
                if entry.get("existed"):
                    _AL.add_curve(anim, cn, _RCTF, False)
                    if _pk:
                        _AL.add_float_curve_keys(anim, cn, [float(k.get("time") or 0.0) for k in _pk], [float(k.get("value") or 0.0) for k in _pk])
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-key-reverted"})
    elif op == "set_anim_rate_scale":
        # cross-module (anim_write.py): restore the prior rate scale. FAITHFUL scalar.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pr = entry.get("prior")
        if anim is None or pr is None:
            undone.append({**entry, "result": "anim-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_anim_rate_scale"):
                unreal.AnimationLibrary.set_rate_scale(anim, float(pr))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-rate-restored"})
    elif op == "add_foliage_instances":
        # cross-module (foliage_write.py): remove all instances of this (unique MCP_A_) foliage type, then
        # destroy the IFA only if THIS add created it and it is now empty. FAITHFUL (type is single-use).
        ap = entry.get("foliage_type_path")
        ftype = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        _world = _ues.get_editor_world() if _ues else None
        if ftype is None or _world is None:
            undone.append({**entry, "result": "type-or-world-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_foliage_instances"):
                unreal.InstancedFoliageActor.get_default_object().remove_all_instances(_world, ftype)
            if entry.get("created_ifa"):
                names = set(entry.get("ifa_names") or [])
                for a in (eas.get_all_level_actors() or []):
                    if a and a.get_name() in names and a.get_class().get_name() == "InstancedFoliageActor":
                        tot = 0
                        for c in (a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
                            try: tot += c.get_instance_count()
                            except Exception: pass
                        if tot == 0:
                            eas.destroy_actor(a)
            undone.append({**entry, "result": "foliage-instances-removed"})
            ftype = None
    elif op == "add_rig_element":
        # cross-module (controlrig_write.py): remove the hierarchy element we added. FAITHFUL (leaf add).
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("key_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=et)
            with unreal.ScopedEditorTransaction("MCP undo add_rig_element"):
                bp.get_hierarchy_controller().remove_element(_key, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-element-removed"})
    elif op == "remove_rig_element":
        # cross-module (controlrig_write.py): rebuild the removed leaf element from captured state
        # (parent, transform, and for controls the FRigControlSettings/Value via import_text). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint):
            undone.append({**entry, "result": "cr-absent"})
        else:
            hc = bp.get_hierarchy_controller(); h = bp.get_hierarchy()
            short = str(entry.get("element_type") or "").lower()
            nm = unreal.Name(str(entry.get("name")))
            _pn = entry.get("parent_name")
            if _pn:
                _pet = getattr(unreal.RigElementType, str(entry.get("parent_type") or "bone").upper(), unreal.RigElementType.BONE)
                pk = unreal.RigElementKey(name=unreal.Name(str(_pn)), type=_pet)
            else:
                pk = unreal.RigElementKey()
            _ident = unreal.Transform()
            newkey = None
            with unreal.ScopedEditorTransaction("MCP undo remove_rig_element"):
                if short == "bone":
                    newkey = hc.add_bone(nm, pk, _ident, False, unreal.RigBoneType.USER, False, False)
                elif short == "null":
                    newkey = hc.add_null(nm, pk, _ident, False, False, False)
                elif short == "control":
                    _cs = unreal.RigControlSettings()
                    if entry.get("settings_txt"): _cs.import_text(entry.get("settings_txt"))
                    _val = unreal.RigControlValue()
                    if entry.get("value_txt"): _val.import_text(entry.get("value_txt"))
                    newkey = hc.add_control(nm, pk, _cs, _val, False, False)
                if newkey is not None:
                    _x = entry.get("in_local_xf")
                    if _x:
                        _t = unreal.Transform()
                        _t.set_editor_property("translation", unreal.Vector(_x["t"][0], _x["t"][1], _x["t"][2]))
                        _t.set_editor_property("rotation", unreal.Quat(_x["q"][0], _x["q"][1], _x["q"][2], _x["q"][3]))
                        _t.set_editor_property("scale3d", unreal.Vector(_x["s"][0], _x["s"][1], _x["s"][2]))
                        h.set_local_transform(newkey, _t, False, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("cr-element-restored" if newkey is not None else "cr-restore-unsupported-type")})
    elif op == "set_rig_control_settings":
        # cross-module (controlrig_write.py): restore the exact prior FRigControlSettings. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_settings_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _s = unreal.RigControlSettings(); _s.import_text(pt)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_settings"):
                bp.get_hierarchy_controller().set_control_settings(_key, _s, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-settings-restored"})
    elif op == "set_rig_element_transform":
        # cross-module (controlrig_write.py): restore the captured prior transform. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("key_type") or "").upper(), None)
        px = entry.get("prior_xf")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None or px is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=et)
            _t = unreal.Transform()
            _t.set_editor_property("translation", unreal.Vector(px["t"][0], px["t"][1], px["t"][2]))
            _t.set_editor_property("rotation", unreal.Quat(px["q"][0], px["q"][1], px["q"][2], px["q"][3]))
            _t.set_editor_property("scale3d", unreal.Vector(px["s"][0], px["s"][1], px["s"][2]))
            _init = bool(entry.get("initial"))
            with unreal.ScopedEditorTransaction("MCP undo set_rig_element_transform"):
                if str(entry.get("space")) == "global":
                    h.set_global_transform(_key, _t, _init, True, False, False)
                else:
                    h.set_local_transform(_key, _t, _init, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-transform-restored"})
    elif op == "set_rig_control_value":
        # cross-module (controlrig_write.py G1-B): restore the control's captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pv = entry.get("prior_value_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pv is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _v = unreal.RigControlValue(); _v.import_text(pv)
            _vt = getattr(unreal.RigControlValueType, str(entry.get("value_type") or "current").upper(), unreal.RigControlValueType.CURRENT)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_value"):
                h.set_control_value(_key, _v, _vt, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-value-restored"})
    elif op == "set_rig_control_offset":
        # cross-module (controlrig_write.py G1-B): re-import the control's captured element text
        # (restores offset/settings/value of that control). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_element_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_offset"):
                bp.get_hierarchy_controller().import_from_text(pt, True, False, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-offset-restored"})
    elif op == "set_rig_control_shape":
        # cross-module (controlrig_write.py G1-B): restore prior FRigControlSettings + local shape transform. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pst = entry.get("prior_settings_txt"); psx = entry.get("prior_shape_xf")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pst is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy(); hc = bp.get_hierarchy_controller()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _ns = unreal.RigControlSettings(); _ns.import_text(pst)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_shape"):
                hc.set_control_settings(_key, _ns, False)
                if psx:
                    _t = unreal.Transform()
                    _t.set_editor_property("translation", unreal.Vector(psx["t"][0], psx["t"][1], psx["t"][2]))
                    _t.set_editor_property("rotation", unreal.Quat(psx["q"][0], psx["q"][1], psx["q"][2], psx["q"][3]))
                    _t.set_editor_property("scale3d", unreal.Vector(psx["s"][0], psx["s"][1], psx["s"][2]))
                    h.set_control_shape_transform(_key, _t, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-shape-restored"})
    elif op == "set_rig_element_parent":
        # cross-module (controlrig_write.py G1-B): restore the element's prior parent (or unparent if it was root). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cet = getattr(unreal.RigElementType, str(entry.get("child_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or cet is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            hc = bp.get_hierarchy_controller()
            _child = unreal.RigElementKey(name=unreal.Name(str(entry.get("child_name"))), type=cet)
            _mg = bool(entry.get("maintain_global"))
            _ppn = entry.get("prior_parent_name")
            with unreal.ScopedEditorTransaction("MCP undo set_rig_element_parent"):
                if _ppn:
                    _ppet = getattr(unreal.RigElementType, str(entry.get("prior_parent_type") or "bone").upper(), unreal.RigElementType.BONE)
                    _pk = unreal.RigElementKey(name=unreal.Name(str(_ppn)), type=_ppet)
                    hc.set_parent(_child, _pk, _mg, False, False)
                else:
                    hc.remove_all_parents(_child, _mg, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-parent-restored"})
    elif op == "rename_rig_element":
        # cross-module (controlrig_write.py G1-B): rename the element back to its old name. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("element_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            hc = bp.get_hierarchy_controller()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("new_name"))), type=et)
            with unreal.ScopedEditorTransaction("MCP undo rename_rig_element"):
                hc.rename_element(_key, unreal.Name(str(entry.get("old_name"))), False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-element-renamed-back"})
    elif op == "restore_rig_hierarchy":
        # cross-module (controlrig_write.py G1-B): only emitted by build_rig_hierarchy(clear_existing=True).
        # Wipe the rebuilt hierarchy + re-import the captured original. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_hierarchy_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            hc = bp.get_hierarchy_controller(); h = bp.get_hierarchy()
            with unreal.ScopedEditorTransaction("MCP undo restore_rig_hierarchy"):
                for _k in reversed(list(h.get_all_keys(True))):
                    hc.remove_element(_k, False, False)
                hc.import_from_text(pt, False, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-hierarchy-restored"})
    elif op == "seq_set_key_value":
        # cross-module (sequencer_edit.py G1-A): restore the key's prior value. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_value"):
                _k.set_value(entry.get("prior_value"))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-value-restored"})
    elif op == "seq_set_key_time":
        # cross-module (sequencer_edit.py G1-A): move the key back to its prior frame. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("new_frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_time"):
                _k.set_time(unreal.FrameNumber(int(entry.get("prior_frame"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-time-restored"})
    elif op == "seq_set_key_interp":
        # cross-module (sequencer_edit.py G1-A): restore prior interp + tangent mode. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_interp"):
                _iv = _seqe_enum("RichCurveInterpMode", _SEQE_INTERP.get(str(entry.get("prior_interp")), "RCIM_CUBIC"))
                if _iv is not None: _k.set_interpolation_mode(_iv)
                _tv = _seqe_enum("RichCurveTangentMode", _SEQE_TANMODE.get(str(entry.get("prior_tangent_mode")), "RCTM_AUTO"))
                if _tv is not None: _k.set_tangent_mode(_tv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-interp-restored"})
    elif op == "seq_set_key_tangent":
        # cross-module (sequencer_edit.py G1-A): restore full prior key tangent state. FAITHFUL (USER/BREAK exact).
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_tangent"):
                _seqe_restore_key(_k, entry.get("prior") or {})
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-tangent-restored"})
    elif op == "seq_remove_key":
        # cross-module (sequencer_edit.py G1-A): re-add the removed key + restore its full state. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        _ks = entry.get("key_state") or {}
        if _ch is None or _ks.get("frame") is None:
            undone.append({**entry, "result": "seq-channel-or-state-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_remove_key"):
                _nk = _ch.add_key(unreal.FrameNumber(int(_ks.get("frame"))), _ks.get("value"))
                _seqe_restore_key(_nk, _ks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-readded"})
    elif op == "seq_set_channel_default":
        # cross-module (sequencer_edit.py G1-A): restore prior channel default (or clear it). FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        if _ch is None:
            undone.append({**entry, "result": "seq-channel-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_channel_default"):
                if entry.get("had_default"):
                    _ch.set_default(entry.get("prior_default"))
                else:
                    try: _ch.remove_default()
                    except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "channel-default-restored"})
    elif op == "seq_set_channel_extrap":
        # cross-module (sequencer_edit.py G1-A): restore prior pre/post-infinity extrapolation. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        if _ch is None:
            undone.append({**entry, "result": "seq-channel-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_channel_extrap"):
                _pre = _seqe_enum("RichCurveExtrapolation", _SEQE_EXTRAP.get(str(entry.get("prior_pre")), "RCCE_CONSTANT"))
                _post = _seqe_enum("RichCurveExtrapolation", _SEQE_EXTRAP.get(str(entry.get("prior_post")), "RCCE_CONSTANT"))
                if _pre is not None: _ch.set_pre_infinity_extrapolation(_pre)
                if _post is not None: _ch.set_post_infinity_extrapolation(_post)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "channel-extrap-restored"})
    elif op == "seq_set_section_range":
        # cross-module (sequencer_edit.py G1-A): restore the section's prior frame range. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None:
            undone.append({**entry, "result": "seq-section-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_range"):
                if entry.get("prior_has_start") and entry.get("prior_has_end"):
                    _sec.set_range(int(entry.get("prior_start")), int(entry.get("prior_end")))
                else:
                    if entry.get("prior_has_start"):
                        _sec.set_start_frame(int(entry.get("prior_start")))
                    else:
                        try: _sec.set_start_frame_bounded(False)
                        except Exception: pass
                    if entry.get("prior_has_end"):
                        _sec.set_end_frame(int(entry.get("prior_end")))
                    else:
                        try: _sec.set_end_frame_bounded(False)
                        except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-range-restored"})
    elif op == "seq_set_section_easing":
        # cross-module (sequencer_edit.py G1-A): restore prior ease-in/out durations. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None:
            undone.append({**entry, "result": "seq-section-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_easing"):
                if entry.get("prior_ease_in") is not None: _sec.set_ease_in_duration(int(entry.get("prior_ease_in")))
                if entry.get("prior_ease_out") is not None: _sec.set_ease_out_duration(int(entry.get("prior_ease_out")))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-easing-restored"})
    elif op == "seq_set_section_blend":
        # cross-module (sequencer_edit.py G1-A): restore prior blend type (raw enum, not the Optional wrapper). FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None or not entry.get("prior_valid"):
            undone.append({**entry, "result": "seq-section-or-prior-absent"})
        else:
            _bv = _seqe_enum("MovieSceneBlendType", _SEQE_BLEND.get(str(entry.get("prior_blend_type")), "ABSOLUTE"))
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_blend"):
                if _bv is not None: _sec.set_blend_type(_bv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-blend-restored"})
    elif op == "seq_set_section_property":
        # cross-module (sequencer_edit.py G1-A): restore a universal section editor-property. FAITHFUL (scalar/enum/vector/color/object).
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _pn = entry.get("property_name")
        if _sec is None or not _pn:
            undone.append({**entry, "result": "seq-section-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_property"):
                _nv = _coerce(_sec.get_editor_property(_pn), entry.get("prior_value"))
                _sec.set_editor_property(_pn, _nv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-property-restored"})
    elif op == "seq_set_track_property":
        # cross-module (sequencer_edit.py G1-A): restore a universal track editor-property. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tks = _seqe_tracks(seq, entry.get("binding"))
        _ti = entry.get("track_index")
        _tr = _tks[int(_ti)] if (_ti is not None and int(_ti) < len(_tks)) else None
        _pn = entry.get("property_name")
        if _tr is None or not _pn:
            undone.append({**entry, "result": "seq-track-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_track_property"):
                _nv = _coerce(_tr.get_editor_property(_pn), entry.get("prior_value"))
                _tr.set_editor_property(_pn, _nv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "track-property-restored"})
    elif op == "set_curve_table":
        # cross-module (curves_write_ext.py G2-A): whole-table restore from prior JSON snapshot (json import
        # is a full REPLACE). Shared by set/delete/rename row + import_curve_table. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is None or not isinstance(obj, unreal.CurveTable):
            undone.append({**entry, "result": "curve-table-absent"})
        else:
            # True REPLACE: clear ALL current rows via remove_curve_table_row (json_to_curve_table
            # does NOT reliably clear, and "[]" FAILS to parse for a CurveTable), then re-import the
            # prior rows only if the prior table was non-empty (valid JSON; an empty prior is the
            # non-JSON sentinel "No data in row curve!"). G2 re-verify fix #2.
            _dtfl = unreal.DataTableFunctionLibrary
            _pj = entry.get("prior_json") or ""
            _pjs = str(_pj).strip()
            _prior_is_json = _pjs.startswith("[") or _pjs.startswith("{")
            with unreal.ScopedEditorTransaction("MCP undo set_curve_table"):
                for _rn in list(_dtfl.get_curve_table_row_names(obj) or []):
                    _dtfl.remove_curve_table_row(obj, _rn)
                if _prior_is_json:
                    _dtfl.json_to_curve_table(obj, _pj)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            _rows_now = len(list(_dtfl.get_curve_table_row_names(obj) or []))
            undone.append({**entry, "result": "curve-table-restored", "rows": _rows_now})
    elif op == "set_curve_atlas":
        # cross-module (curves_write_ext.py G2-A): restore prior texture_size + gradient_curves (set fires
        # PostEditChangeProperty -> atlas texture rebuild). FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is None or not isinstance(obj, unreal.CurveLinearColorAtlas):
            undone.append({**entry, "result": "curve-atlas-absent"})
        else:
            _pc = []
            for _p in (entry.get("prior_curves") or []):
                _po = unreal.EditorAssetLibrary.load_asset(_p)
                if _po is not None and isinstance(_po, unreal.CurveLinearColor):
                    _pc.append(_po)
            with unreal.ScopedEditorTransaction("MCP undo set_curve_atlas"):
                try: obj.set_editor_property("texture_size", int(entry.get("prior_width") or 64))
                except Exception: pass
                try: obj.set_editor_property("gradient_curves", _pc)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "curve-atlas-restored"})
    elif op == "set_input_action_properties":
        # cross-module (input_write.py G2-B): restore each captured prior InputAction property. FAITHFUL.
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_input_action_properties"):
                for _pn, _sv in (entry.get("prior") or {}).items(): _iw_set(ia, _pn, _iw_coerce_val(_sv))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ia-props-restored"})
    elif op == "add_input_action_tm":
        # cross-module (input_write.py G2-B): delete the trigger/modifier we appended. FAITHFUL. (add trigger+modifier)
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _field = entry.get("field"); _idx = entry.get("index")
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _arr = list(ia.get_editor_property(_field) or [])
            if _idx is not None and 0 <= _idx < len(_arr):
                with unreal.ScopedEditorTransaction("MCP undo add_input_action_tm"):
                    del _arr[_idx]; ia.set_editor_property(_field, _arr)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "ia-tm-removed"})
            else:
                undone.append({**entry, "result": "index-gone"})
    elif op == "remove_input_action_tm":
        # cross-module (input_write.py G2-B): rebuild + reinsert the removed trigger/modifier. FAITHFUL.
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _field = entry.get("field"); _idx = entry.get("index")
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _arr = list(ia.get_editor_property(_field) or [])
            _inst = _iw_rebuild_tm(entry.get("item"), ia)
            with unreal.ScopedEditorTransaction("MCP undo remove_input_action_tm"):
                _arr.insert(min(int(_idx), len(_arr)), _inst); ia.set_editor_property(_field, _arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ia-tm-restored"})
    elif op == "imc_remove_mapping":
        # cross-module (input_write.py G2-B): rebuild + re-insert the removed IMC mapping. FAITHFUL.
        ap = entry.get("asset_path"); imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if imc is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _d, _arr = _iw_dkm(imc); _idx = entry.get("index")
            _m = _iw_rebuild_mapping(entry.get("mapping") or {}, imc)
            with unreal.ScopedEditorTransaction("MCP undo imc_remove_mapping"):
                _arr.insert(min(int(_idx), len(_arr)), _m); _iw_setdkm(imc, _d, _arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "imc-mapping-reinserted"})
    elif op == "imc_replace_mapping":
        # cross-module (input_write.py G2-B): restore the prior mapping snapshot at index. FAITHFUL.
        # (set_key_mapping + all 4 per-mapping trigger/modifier ops)
        ap = entry.get("asset_path"); imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if imc is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _d, _arr = _iw_dkm(imc); _idx = entry.get("index")
            if _idx is not None and 0 <= _idx < len(_arr):
                with unreal.ScopedEditorTransaction("MCP undo imc_replace_mapping"):
                    _arr[int(_idx)] = _iw_rebuild_mapping(entry.get("prior") or {}, imc); _iw_setdkm(imc, _d, _arr)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "imc-mapping-restored"})
            else:
                undone.append({**entry, "result": "index-gone"})
    elif op == "gas_set_modifiers":
        # cross-module (gas_write.py G4): rebuild the effect's prior FGameplayModifierInfo array on the CDO
        # (import_text each) + recompile + save. FAITHFUL. (validated inverse pattern — no txn wrapper)
        ap = entry.get("asset_path"); _prior = entry.get("prior") or []
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _gcls = unreal.BlueprintEditorLibrary.generated_class(bp) if bp else None
        _cdo = unreal.get_default_object(_gcls) if _gcls else None
        if _cdo is None:
            undone.append({**entry, "result": "effect-absent"})
        else:
            _rebuilt = []
            for _txt in _prior:
                _m = unreal.GameplayModifierInfo()
                if _txt:
                    try: _m.import_text(_txt)
                    except Exception: pass
                _rebuilt.append(_m)
            try: _cdo.set_editor_property("modifiers", _rebuilt)
            except Exception: pass
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            _aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
            if _aes:
                try: _aes.close_all_editors_for_asset(bp)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modifiers-restored", "count": len(_rebuilt)})
    elif op == "gas_set_ge_components":
        # cross-module (gas_write.py G4): rebuild the effect's prior ge_components array as inline CDO
        # subobjects (new_object + import_text) + recompile + save. FAITHFUL (reverses add AND remove).
        ap = entry.get("asset_path"); _prior = entry.get("prior") or []
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _gcls = unreal.BlueprintEditorLibrary.generated_class(bp) if bp else None
        _cdo = unreal.get_default_object(_gcls) if _gcls else None
        if _cdo is None:
            undone.append({**entry, "result": "effect-absent"})
        else:
            _rebuilt = []
            for _rec in _prior:
                _cp = _rec.get("class_path"); _txt = _rec.get("text")
                _cc = None
                try: _cc = unreal.load_class(None, _cp)
                except Exception: _cc = None
                if _cc is None and _cp:
                    _cn = _cp.split(".")[-1].split(":")[-1]
                    _cc = getattr(unreal, _cn, None)
                if _cc is not None:
                    _nm = getattr(_cc, "__name__", None) or _cc.get_name()
                    try:
                        _comp = unreal.new_object(_cc, _cdo, unreal.Name("MCP_GEC_R_" + _nm))
                    except Exception:
                        try: _comp = unreal.new_object(_cc, _cdo)
                        except Exception: _comp = None
                    if _comp is not None:
                        if _txt:
                            try: _comp.import_text(_txt)
                            except Exception: pass
                        _rebuilt.append(_comp)
            try: _cdo.set_editor_property("ge_components", _rebuilt)
            except Exception: pass
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            _aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
            if _aes:
                try: _aes.close_all_editors_for_asset(bp)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ge-components-restored", "count": len(_rebuilt)})
    elif op == "seq_rename_binding":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); tgt = None
            for b in (seq.get_bindings() or []):
                if str(b.get_name()) == bn:
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_rename_binding"):
                    tgt.set_display_name(entry.get("prior_display_name"))
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "binding-renamed-back"})
            seq = None
    elif op == "seq_set_display_rate":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_display_rate"):
                seq.set_display_rate(unreal.FrameRate(int(entry.get("prior_num")), int(entry.get("prior_den"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "display-rate-restored"})
            seq = None
    elif op == "seq_set_tick_resolution":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_tick_resolution"):
                seq.set_tick_resolution(unreal.FrameRate(int(entry.get("prior_num")), int(entry.get("prior_den"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "tick-resolution-restored"})
            seq = None
    elif op == "seq_set_evaluation_type":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            ev = getattr(unreal.MovieSceneEvaluationType, str(entry.get("prior_type")), None)
            with unreal.ScopedEditorTransaction("MCP undo seq_set_evaluation_type"):
                if ev is not None: seq.set_evaluation_type(ev)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "evaluation-type-restored"})
            seq = None
    elif op == "seq_add_property_track":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); tgt = None
            for b in (seq.get_bindings() or []):
                if str(b.get_name()) == bn:
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                cls_name = str(entry.get("track_class"))
                same = [t for t in (tgt.get_tracks() or []) if t.get_class().get_name() == cls_name]
                if len(same) > int(entry.get("prior_count", 0)):
                    with unreal.ScopedEditorTransaction("MCP undo seq_add_property_track"):
                        tgt.remove_track(same[-1])
                    undone.append({**entry, "result": "property-track-removed"})
                else:
                    undone.append({**entry, "result": "track-count-unchanged"})
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            seq = None
    elif op == "seq_add_subsequence":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            shot = [t for t in (seq.get_tracks() or []) if t.get_class().get_name() == "MovieSceneCinematicShotTrack"]
            if not shot:
                undone.append({**entry, "result": "shot-track-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_add_subsequence"):
                    if entry.get("added_track"):
                        seq.remove_track(shot[0])
                    else:
                        secs = list(shot[0].get_sections() or [])
                        si = int(entry.get("section_index", len(secs) - 1))
                        if 0 <= si < len(secs):
                            shot[0].remove_section(secs[si])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "subsequence-removed"})
            seq = None
    elif op == "seq_add_marked_frame":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            frame = int(entry.get("frame")); lbl = entry.get("label") or ""; idx = None
            for i, m in enumerate(seq.get_marked_frames() or []):
                fn = m.get_editor_property("frame_number")
                f = int(fn.value) if hasattr(fn, "value") else int(fn.frame_number.value)
                if f == frame and str(m.get_editor_property("label")) == lbl:
                    idx = i; break
            with unreal.ScopedEditorTransaction("MCP undo seq_add_marked_frame"):
                if idx is not None: seq.delete_marked_frame(idx)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("marked-frame-removed" if idx is not None else "marked-frame-absent")})
            seq = None
    elif op == "seq_remove_marked_frame":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            mk = entry.get("marked") or {}
            mf = unreal.MovieSceneMarkedFrame()
            mf.set_editor_property("frame_number", unreal.FrameNumber(int(mk.get("frame"))))
            if mk.get("label"): mf.set_editor_property("label", mk.get("label"))
            try: mf.set_editor_property("is_determinism_fence", bool(mk.get("is_determinism_fence")))
            except Exception: pass
            try: mf.set_editor_property("is_inclusive_time", bool(mk.get("is_inclusive_time")))
            except Exception: pass
            with unreal.ScopedEditorTransaction("MCP undo seq_remove_marked_frame"):
                seq.add_marked_frame(mf)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "marked-frame-readded"})
            seq = None
    elif op == "seq_add_folder":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            nm = str(entry.get("folder_name")); tgt = None
            for f in (seq.get_root_folders_in_sequence() or []):
                if str(f.get_folder_name()) == nm:
                    tgt = f; break
            with unreal.ScopedEditorTransaction("MCP undo seq_add_folder"):
                if tgt is not None: seq.remove_root_folder_from_sequence(tgt)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("folder-removed" if tgt is not None else "folder-absent")})
            seq = None
    elif op == "seq_add_to_folder":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            nm = str(entry.get("folder_name")); fld = None
            for f in (seq.get_root_folders_in_sequence() or []):
                if str(f.get_folder_name()) == nm:
                    fld = f; break
            if fld is None:
                undone.append({**entry, "result": "folder-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_add_to_folder"):
                    if entry.get("child_kind") == "binding":
                        bn = str(entry.get("binding_name")); b = None
                        for bb in (seq.get_bindings() or []):
                            if str(bb.get_name()) == bn or str(bb.get_display_name()) == bn:
                                b = bb; break
                        if b is not None: fld.remove_child_object_binding(b)
                    elif entry.get("child_kind") == "track":
                        tl = list(seq.get_tracks() or []); ti = int(entry.get("track_index"))
                        if 0 <= ti < len(tl): fld.remove_child_track(tl[ti])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "folder-child-removed"})
            seq = None
    elif op == "seq_set_playhead":
        L = unreal.LevelSequenceEditorBlueprintLibrary
        if L.get_current_level_sequence() is not None:
            L.set_current_time(int(entry.get("prior_frame")))
            undone.append({**entry, "result": "playhead-restored"})
        else:
            undone.append({**entry, "result": "no-sequence-open"})
    elif op == "seq_set_playback_state":
        L = unreal.LevelSequenceEditorBlueprintLibrary
        if L.get_current_level_sequence() is not None:
            if entry.get("prior_playing"): L.play()
            else: L.pause()
            undone.append({**entry, "result": "playback-state-restored"})
        else:
            undone.append({**entry, "result": "no-sequence-open"})
    elif op == "set_blend_space_axis":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            axis = int(entry.get("axis")); pr = entry.get("prior") or {}
            bp = list(bs.get_editor_property("blend_parameters") or [])
            if 0 <= axis < len(bp):
                p = bp[axis]
                with unreal.ScopedEditorTransaction("MCP undo set_blend_space_axis"):
                    p.set_editor_property("display_name", pr.get("display_name"))
                    p.set_editor_property("min", float(pr.get("min")))
                    p.set_editor_property("max", float(pr.get("max")))
                    p.set_editor_property("grid_num", int(pr.get("grid_num")))
                    bp[axis] = p
                    bs.set_editor_property("blend_parameters", bp)
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            undone.append({**entry, "result": "axis-restored"})
            bs = None
    elif op == "add_blend_space_sample":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            pc = int(entry.get("prior_count"))
            samples = list(bs.get_editor_property("sample_data") or [])
            if len(samples) > pc:
                with unreal.ScopedEditorTransaction("MCP undo add_blend_space_sample"):
                    bs.set_editor_property("sample_data", samples[:pc])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            undone.append({**entry, "result": "sample-truncated"})
            bs = None
    elif op == "remove_blend_space_sample":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            sm = entry.get("sample") or {}; idx = int(entry.get("index"))
            an = unreal.EditorAssetLibrary.load_asset(sm.get("animation")) if sm.get("animation") else None
            s = unreal.BlendSample()
            s.set_editor_property("animation", an)
            s.set_editor_property("sample_value", unreal.Vector(float(sm.get("x") or 0.0), float(sm.get("y") or 0.0), 0.0))
            s.set_editor_property("rate_scale", float(sm.get("rate_scale") or 1.0))
            samples = list(bs.get_editor_property("sample_data") or [])
            if idx < 0 or idx > len(samples):
                idx = len(samples)
            samples.insert(idx, s)
            with unreal.ScopedEditorTransaction("MCP undo remove_blend_space_sample"):
                bs.set_editor_property("sample_data", samples)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "sample-reinserted"})
            bs = None
    elif op == "add_montage_slot":
        mont = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if mont is None:
            undone.append({**entry, "result": "montage-absent"})
        else:
            sn = str(entry.get("slot_name"))
            tracks = [t for t in (mont.get_editor_property("slot_anim_tracks") or []) if str(t.get_editor_property("slot_name")) != sn]
            with unreal.ScopedEditorTransaction("MCP undo add_montage_slot"):
                mont.set_editor_property("slot_anim_tracks", tracks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "slot-removed"})
            mont = None
    elif op == "add_montage_segment":
        mont = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if mont is None:
            undone.append({**entry, "result": "montage-absent"})
        else:
            sn = str(entry.get("slot_name")); pc = int(entry.get("prior_segment_count"))
            tracks = list(mont.get_editor_property("slot_anim_tracks") or [])
            for i, t in enumerate(tracks):
                if str(t.get_editor_property("slot_name")) == sn:
                    atk = t.get_editor_property("anim_track")
                    segs = list(atk.get_editor_property("anim_segments") or [])
                    if len(segs) > pc:
                        atk.set_editor_property("anim_segments", segs[:pc])
                        tracks[i].set_editor_property("anim_track", atk)
                    break
            with unreal.ScopedEditorTransaction("MCP undo add_montage_segment"):
                mont.set_editor_property("slot_anim_tracks", tracks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "segment-truncated"})
            mont = None
    elif op == "remove_anim_notify":
        anim = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            _AL = unreal.AnimationLibrary
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_notify"):
                _AL.remove_animation_notify_events_by_track(anim, tn)
                for _pe in (entry.get("prior_events") or []):
                    _cp = _pe.get("class_path"); _cls = None
                    if _cp:
                        try: _cls = unreal.load_object(None, _cp)
                        except Exception: _cls = None
                    if _cls is None:
                        continue
                    if _pe.get("kind") == "state":
                        _AL.add_animation_notify_state_event(anim, tn, float(_pe.get("time") or 0.0), float(_pe.get("duration") or 0.0), _cls)
                    else:
                        _AL.add_animation_notify_event(anim, tn, float(_pe.get("time") or 0.0), _cls)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "notify-track-rebuilt"})
            anim = None
    elif op == "rename_gameplay_tag":
        # cross-module (gas_write.py C++#16): rename the tag back (self-inverse; INI redirector). FAITHFUL.
        m = getattr(unreal, "MCPReflectionLibrary", None)
        ot = entry.get("old_tag"); nt = entry.get("new_tag")
        if m is None or not hasattr(m, "rename_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            try: r = json.loads(m.rename_gameplay_tag(nt, ot))
            except Exception as e: r = {"error": str(e)}
            undone.append({**entry, "result": "tag-renamed-back", "ok": bool(isinstance(r, dict) and r.get("renamed"))})
    elif op == "eqs_add_option":
        # cross-module (eqs_write.py C++#15): remove the appended option. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.remove_env_query_option(o, int(entry.get("option_index"))))
            except Exception as e: r = {"error": str(e)}
            _eqs_save(ap)
            undone.append({**entry, "result": "option-removed", "ok": bool(isinstance(r, dict) and r.get("removed"))})
    elif op == "eqs_add_test":
        # cross-module (eqs_write.py C++#15): remove the appended test. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.remove_env_query_test(o, int(entry.get("option_index")), int(entry.get("test_index"))))
            except Exception as e: r = {"error": str(e)}
            _eqs_save(ap)
            undone.append({**entry, "result": "test-removed", "ok": bool(isinstance(r, dict) and r.get("removed"))})
    elif op == "eqs_set_node_property":
        # cross-module (eqs_write.py C++#15): re-set the captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if o is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            r = _eqs_setp(o, entry.get("node_locator"), entry.get("prop_name"), entry.get("prev"))
            _eqs_save(ap)
            undone.append({**entry, "result": "prop-restored", "ok": bool(isinstance(r, dict) and r.get("set"))})
    elif op == "eqs_readd_test":
        # cross-module (eqs_write.py C++#15): re-append the removed test + replay config. BEST-EFFORT.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            oi = int(entry.get("option_index"))
            try: r = json.loads(m.add_env_query_test(o, oi, _eqs_norm(entry.get("test_class"))))
            except Exception as e: r = {"error": str(e)}
            tj = r.get("test_index") if isinstance(r, dict) else None
            okc = 0
            if tj is not None:
                for pn, pv in (entry.get("config") or {}).items():
                    rr = _eqs_setp(o, "option:%d/test:%d" % (oi, int(tj)), pn, pv)
                    if isinstance(rr, dict) and rr.get("set"): okc += 1
            _eqs_save(ap)
            undone.append({**entry, "result": "test-replayed", "test_index": tj, "props_ok": okc})
    elif op == "eqs_readd_option":
        # cross-module (eqs_write.py C++#15): re-append the removed option + generator/tests. BEST-EFFORT.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.add_env_query_option(o, _eqs_norm(entry.get("generator_class"))))
            except Exception as e: r = {"error": str(e)}
            oi = r.get("option_index") if isinstance(r, dict) else None
            gok = 0; tcount = 0
            if oi is not None:
                oi = int(oi)
                for pn, pv in (entry.get("generator_config") or {}).items():
                    rr = _eqs_setp(o, "option:%d/generator" % oi, pn, pv)
                    if isinstance(rr, dict) and rr.get("set"): gok += 1
                for tc in (entry.get("tests") or []):
                    try: tr = json.loads(m.add_env_query_test(o, oi, _eqs_norm(tc.get("test_class"))))
                    except Exception: tr = None
                    tj = tr.get("test_index") if isinstance(tr, dict) else None
                    if tj is not None:
                        tcount += 1
                        for pn, pv in (tc.get("config") or {}).items():
                            _eqs_setp(o, "option:%d/test:%d" % (oi, int(tj)), pn, pv)
            _eqs_save(ap)
            undone.append({**entry, "result": "option-replayed", "option_index": oi, "gen_props_ok": gok, "tests_replayed": tcount})
    elif op == "bt_remove_node":
        # cross-module (bt_write2.py): rebuild the snapshotted child slot at index.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        idx = entry.get("index"); snap = entry.get("snapshot")
        if bt is None or parent is None or idx is None or snap is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            newch = _make_child(bt, snap)
            if idx > len(kids):
                idx = len(kids)
            kids.insert(idx, newch)
            parent.set_editor_property("children", kids)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-node-restored"})
    elif op == "bt_set_node_prop":
        # cross-module (bt_write2.py): restore prior value on the node at node_path.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        node = None
        if bt is not None:
            npth = str(entry.get("node_path") or "").strip()
            if npth in ("", "root"):
                node = bt.get_editor_property("root_node")
            else:
                toks = npth.split(".")
                par = _bt_resolve(bt, ".".join(toks[:-1]) if len(toks) > 1 else "root")
                if par is not None:
                    kk = list(par.get_editor_property("children") or [])
                    ii = _try(lambda: int(toks[-1]))
                    if ii is not None and 0 <= ii < len(kk):
                        node = kk[ii].get_editor_property("child_composite") or kk[ii].get_editor_property("child_task")
        if node is None:
            undone.append({**entry, "result": "bt-node-absent"})
        else:
            prop = entry.get("property"); prior = entry.get("prior")
            cur = _try(lambda: node.get_editor_property(prop))
            with unreal.ScopedEditorTransaction("MCP undo bt_set_node_prop"):
                node.set_editor_property(prop, _coerce(cur, prior))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-prop-restored"})
    elif op == "bt_add_comp_decorator":
        # cross-module (bt_write2.py): drop the decorators we appended + restore prior decorator_ops.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        ci = entry.get("child_index"); base = entry.get("prior_decorator_count"); pops = entry.get("prior_ops") or []
        if bt is None or parent is None or ci is None or base is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= ci < len(kids):
                ch = kids[ci]
                decos = list(ch.get_editor_property("decorators") or [])
                ch.set_editor_property("decorators", decos[:base])
                ops = []
                for ex in pops:
                    lo = unreal.BTDecoratorLogic(); lo.import_text(ex); ops.append(lo)
                ch.set_editor_property("decorator_ops", ops)
                kids[ci] = ch
                parent.set_editor_property("children", kids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-comp-decorator-reverted"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "bt_reparent":
        # cross-module (bt_write3.py): move the reparented child slot back to its old parent/index. FAITHFUL
        # (the exact slot with its wrapped node + decorators + decorator_ops round-trips). Same-parent vs
        # cross-parent split; uses the recorded final new_index and pop-then-insert so indices stay correct.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        src = _bt_resolve(bt, entry.get("new_parent_path")) if bt is not None else None
        dst = _bt_resolve(bt, entry.get("old_parent_path")) if bt is not None else None
        sidx = entry.get("new_index"); didx = entry.get("old_index")
        npp = str(entry.get("new_parent_path") or "").strip() or "root"
        opp = str(entry.get("old_parent_path") or "").strip() or "root"
        if bt is None or src is None or dst is None or sidx is None or didx is None:
            undone.append({**entry, "result": "bt-reparent-target-absent"})
        elif npp == opp:
            kids = list(src.get_editor_property("children") or [])
            if 0 <= sidx < len(kids):
                slot = kids.pop(sidx)
                ins = didx if 0 <= didx <= len(kids) else len(kids)
                kids.insert(ins, slot)
                src.set_editor_property("children", kids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-reparented-back"})
            else:
                undone.append({**entry, "result": "bt-reparent-index-stale"})
        else:
            skids = list(src.get_editor_property("children") or [])
            if 0 <= sidx < len(skids):
                slot = skids.pop(sidx)
                dkids = list(dst.get_editor_property("children") or [])
                ins = didx if 0 <= didx <= len(dkids) else len(dkids)
                dkids.insert(ins, slot)
                src.set_editor_property("children", skids)
                dst.set_editor_property("children", dkids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-reparented-back"})
            else:
                undone.append({**entry, "result": "bt-reparent-index-stale"})
        bt = None; src = None; dst = None
    elif op == "bb_remove_key":
        # cross-module (bt_write2.py): re-insert the removed BlackboardData key at its index.
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        snap = entry.get("snapshot")
        if bb is None or snap is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            ktcls = _keytype_cls(snap.get("type"))
            keys = list(bb.get_editor_property("keys") or [])
            entry_obj = unreal.BlackboardEntry()
            entry_obj.set_editor_property("entry_name", unreal.Name(str(snap.get("name"))))
            if ktcls is not None:
                entry_obj.set_editor_property("key_type", unreal.new_object(ktcls, bb))
            if snap.get("category"):
                try: entry_obj.set_editor_property("entry_category", unreal.Name(str(snap.get("category"))))
                except Exception: pass
            if snap.get("description"):
                try: entry_obj.set_editor_property("entry_description", str(snap.get("description")))
                except Exception: pass
            i = snap.get("index", len(keys))
            if i > len(keys):
                i = len(keys)
            with unreal.ScopedEditorTransaction("MCP undo bb_remove_key"):
                keys.insert(i, entry_obj)
                bb.set_editor_property("keys", keys)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bb-key-restored"})
            keys = None; entry_obj = None; bb = None
    elif op == "bb_set_key":
        # cross-module (bt_write2.py): restore prior name + type on the key at index.
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        idx = entry.get("index")
        if bb is None or idx is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            keys = list(bb.get_editor_property("keys") or [])
            if 0 <= idx < len(keys):
                e = keys[idx]
                ktcls = _keytype_cls(entry.get("prior_type"))
                with unreal.ScopedEditorTransaction("MCP undo bb_set_key"):
                    e.set_editor_property("entry_name", unreal.Name(str(entry.get("prior_name"))))
                    if ktcls is not None:
                        e.set_editor_property("key_type", unreal.new_object(ktcls, bb))
                    keys[idx] = e
                    bb.set_editor_property("keys", keys)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bb-key-reverted"})
            else:
                undone.append({**entry, "result": "bb-index-stale"})
            keys = None; bb = None
    elif op == "add_seq_master_track":
        # cross-module (sequencer_write_ext.py): remove the master track we appended (if the exact-type
        # count grew). FAITHFUL — removes only our track (+ its sections).
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        if seq is None or _cls is None:
            undone.append({**entry, "result": "sequence-or-class-absent"})
        else:
            _ts = list(seq.find_tracks_by_exact_type(_cls) or [])
            if len(_ts) > int(entry.get("prior_exact_count") or 0):
                with unreal.ScopedEditorTransaction("MCP undo add_seq_master_track"):
                    seq.remove_track(_ts[-1])
                undone.append({**entry, "result": "seq-master-track-removed"})
            else:
                undone.append({**entry, "result": "seq-master-track-count-stale"})
            seq = None
    elif op == "add_seq_binding_track":
        # cross-module (sequencer_write_ext.py): remove the binding track we appended. FAITHFUL.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        _bn = str(entry.get("binding_name"))
        _b = None
        if seq is not None:
            for b in (seq.get_bindings() or []):
                _dn = None
                try: _dn = str(b.get_display_name())
                except Exception: _dn = None
                if _dn == _bn or (hasattr(b, "get_name") and str(b.get_name()) == _bn):
                    _b = b; break
        if seq is None or _cls is None or _b is None:
            undone.append({**entry, "result": "sequence-binding-or-class-absent"})
        else:
            _ts = list(_b.find_tracks_by_exact_type(_cls) or [])
            if len(_ts) > int(entry.get("prior_exact_count") or 0):
                with unreal.ScopedEditorTransaction("MCP undo add_seq_binding_track"):
                    _b.remove_track(_ts[-1])
                undone.append({**entry, "result": "seq-binding-track-removed"})
            else:
                undone.append({**entry, "result": "seq-binding-track-count-stale"})
            seq = None
    elif op == "add_seq_track_section":
        # cross-module (sequencer_write_ext.py): remove the section we appended to a track. FAITHFUL.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        _cont = None
        if seq is not None:
            if str(entry.get("scope")) == "binding":
                _bn = str(entry.get("binding_name"))
                for b in (seq.get_bindings() or []):
                    _dn = None
                    try: _dn = str(b.get_display_name())
                    except Exception: _dn = None
                    if _dn == _bn or (hasattr(b, "get_name") and str(b.get_name()) == _bn):
                        _cont = b; break
            else:
                _cont = seq
        if seq is None or _cls is None or _cont is None:
            undone.append({**entry, "result": "sequence-track-or-binding-absent"})
        else:
            _ts = list(_cont.find_tracks_by_exact_type(_cls) or [])
            _ti = int(entry.get("track_index") or 0)
            if _ti < len(_ts):
                _secs = list(_ts[_ti].get_sections() or [])
                if len(_secs) > int(entry.get("prior_section_count") or 0):
                    with unreal.ScopedEditorTransaction("MCP undo add_seq_track_section"):
                        _ts[_ti].remove_section(_secs[-1])
                    undone.append({**entry, "result": "seq-section-removed"})
                else:
                    undone.append({**entry, "result": "seq-section-count-stale"})
            else:
                undone.append({**entry, "result": "seq-track-index-stale"})
            seq = None
    elif op == "add_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): remove the mesh socket we added. FAITHFUL (created new).
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = entry.get("socket_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or sn is None:
            undone.append({**entry, "result": "mesh-or-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_skeletal_mesh_socket"):
                mesh.remove_socket(unreal.Name(str(sn)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-removed"})
    elif op == "remove_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): re-create the removed mesh socket from captured state
        # (SocketName/BoneName are read-only → build, add, rename the engine auto-name). FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = entry.get("socket_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or sn is None:
            undone.append({**entry, "result": "mesh-or-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_skeletal_mesh_socket"):
                _s = unreal.new_object(unreal.SkeletalMeshSocket, mesh)
                _s.set_socket_parent(mesh, unreal.Name(str(entry.get("bone") or "")))
                _lo = entry.get("location"); _ro = entry.get("rotation"); _sc = entry.get("scale")
                if _lo is not None:
                    _s.set_editor_property("relative_location", unreal.Vector(float(_lo[0]), float(_lo[1]), float(_lo[2])))
                if _ro is not None:
                    _s.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(_ro[0]), yaw=float(_ro[1]), roll=float(_ro[2])))
                if _sc is not None:
                    _s.set_editor_property("relative_scale", unreal.Vector(float(_sc[0]), float(_sc[1]), float(_sc[2])))
                if entry.get("force_always_animated") is not None:
                    try: _s.set_editor_property("force_always_animated", bool(entry.get("force_always_animated")))
                    except Exception: pass
                mesh.add_socket(_s, False)
                _auto = str(_s.get_editor_property("socket_name"))
                if _auto != str(sn):
                    mesh.rename_socket(unreal.Name(_auto), unreal.Name(str(sn)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-restored"})
    elif op == "set_skeletal_mesh_socket_transform":
        # cross-module (skeleton_write.py): restore the socket's captured prior relative transform. FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = str(entry.get("socket_name") or "")
        pr = entry.get("prior") or {}
        _sk = None
        if mesh is not None and isinstance(mesh, unreal.SkeletalMesh):
            for _i in range(mesh.num_sockets()):
                _c = mesh.get_socket_by_index(_i)
                if _c and str(_c.get_editor_property("socket_name")) == sn and isinstance(_c.get_outer(), unreal.SkeletalMesh):
                    _sk = _c; break
        if _sk is None:
            undone.append({**entry, "result": "mesh-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_skeletal_mesh_socket_transform"):
                _lo = pr.get("location"); _ro = pr.get("rotation"); _sc = pr.get("scale")
                if _lo is not None:
                    _sk.set_editor_property("relative_location", unreal.Vector(float(_lo[0]), float(_lo[1]), float(_lo[2])))
                if _ro is not None:
                    _sk.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(_ro[0]), yaw=float(_ro[1]), roll=float(_ro[2])))
                if _sc is not None:
                    _sk.set_editor_property("relative_scale", unreal.Vector(float(_sc[0]), float(_sc[1]), float(_sc[2])))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-xf-restored"})
    elif op == "rename_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): rename the socket back new_name→old_name. FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        on = entry.get("old_name"); nn = entry.get("new_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or on is None or nn is None:
            undone.append({**entry, "result": "mesh-or-names-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo rename_skeletal_mesh_socket"):
                mesh.rename_socket(unreal.Name(str(nn)), unreal.Name(str(on)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-renamed-back"})
    elif op == "set_texture_property":
        # cross-module (texture_write.py): restore the full prior texture-settings snapshot. Two passes
        # converge any single-step constraint cascade (e.g. compression forcing srgb off). FAITHFUL.
        ap = entry.get("asset_path")
        tex = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        prior = entry.get("prior") or {}
        if tex is None or not isinstance(tex, unreal.Texture2D) or not prior:
            undone.append({**entry, "result": "texture-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_texture_property"):
                for _pass in range(2):
                    for fname, meta in prior.items():
                        k = meta.get("kind"); v = meta.get("value")
                        if k == "enum":
                            _cls = getattr(unreal, meta.get("enum_type") or "", None)
                            _nv = getattr(_cls, str(v), None) if _cls is not None else None
                            if _nv is not None:
                                try: tex.set_editor_property(fname, _nv)
                                except Exception: pass
                        elif k == "bool":
                            try: tex.set_editor_property(fname, bool(v))
                            except Exception: pass
                        elif k == "int":
                            try: tex.set_editor_property(fname, int(v))
                            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "texture-settings-restored"})
    elif op == "add_material_expression":
        # cross-module (material_graph_write.py): delete the expression node we created (also disconnects). FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        e = _mg_find(mat, entry.get("expr_name"))
        if mat is None or e is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_material_expression"):
                _MG.delete_material_expression(mat, e)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-expr-deleted"})
    elif op == "set_material_expression_property":
        # cross-module (material_graph_write.py): restore the node property's captured prior value. FAITHFUL when restorable.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        e = _mg_find(mat, entry.get("expr_name"))
        if mat is None or e is None or not entry.get("restorable", True):
            undone.append({**entry, "result": "material-expr-absent-or-unrestorable"})
        else:
            pn = entry.get("property_name")
            with unreal.ScopedEditorTransaction("MCP undo set_material_expression_property"):
                try:
                    _cur = e.get_editor_property(pn)
                    _v = _mg_coerce(entry.get("prior_value"))
                    if isinstance(_cur, unreal.LinearColor) and isinstance(_v, (list, tuple)) and len(_v) >= 3:
                        _v = unreal.LinearColor(float(_v[0]), float(_v[1]), float(_v[2]), float(_v[3]) if len(_v) > 3 else 1.0)
                    elif isinstance(_cur, (int, float)) and not isinstance(_cur, bool) and isinstance(_v, (int, float, str)):
                        _v = float(_v) if isinstance(_cur, float) else int(_v)
                    e.set_editor_property(pn, _v)
                except Exception: pass
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-expr-prop-restored"})
    elif op == "connect_material_expression":
        # cross-module (material_graph_write.py): reconnect the prior source, or clear the pin if none. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        to = _mg_find(mat, entry.get("to_expr"))
        if mat is None or to is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            _ti = entry.get("to_input") or ""
            with unreal.ScopedEditorTransaction("MCP undo connect_material_expression"):
                if entry.get("had_prior"):
                    _src = _mg_find(mat, entry.get("prior_src_name"))
                    if _src is not None:
                        _MG.connect_material_expressions(_src, entry.get("prior_out_name") or "", to, _ti)
                else:
                    _MG.disconnect_material_expressions(to, _ti)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-conn-reverted"})
    elif op == "disconnect_material_expression":
        # cross-module (material_graph_write.py): reconnect the captured prior source. FAITHFUL (only ledgered when connected).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        to = _mg_find(mat, entry.get("to_expr"))
        _src = _mg_find(mat, entry.get("prior_src_name")) if mat is not None else None
        if mat is None or to is None or _src is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo disconnect_material_expression"):
                _MG.connect_material_expressions(_src, entry.get("prior_out_name") or "", to, entry.get("to_input") or "")
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-conn-restored"})
    elif op == "connect_material_property":
        # cross-module (material_graph_write.py): reconnect the prior source into the property, or clear it. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _p = _mg_prop(entry.get("material_property"))
        if mat is None or _p is None or _MG is None:
            undone.append({**entry, "result": "material-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo connect_material_property"):
                if entry.get("had_prior"):
                    _src = _mg_find(mat, entry.get("prior_src_name"))
                    if _src is not None:
                        _MG.connect_material_property(_src, entry.get("prior_out_name") or "", _p)
                else:
                    _MG.disconnect_material_property(mat, _p)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-prop-conn-reverted"})
    elif op == "disconnect_material_property":
        # cross-module (material_graph_write.py): reconnect the captured prior source into the property. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _p = _mg_prop(entry.get("material_property"))
        _src = _mg_find(mat, entry.get("prior_src_name")) if mat is not None else None
        if mat is None or _p is None or _src is None or _MG is None:
            undone.append({**entry, "result": "material-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo disconnect_material_property"):
                _MG.connect_material_property(_src, entry.get("prior_out_name") or "", _p)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-prop-conn-restored"})
    elif op == "add_montage_section":
        # cross-module (anim_write.py, C++ #13): remove the montage section we added. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "remove_montage_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.remove_montage_section(_m, entry.get("section_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-removed"})
    elif op == "remove_montage_section":
        # cross-module (anim_write.py, C++ #13): re-add the section at its prior time + next-link. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "add_montage_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.add_montage_section(_m, entry.get("section_name"), float(entry.get("prior_start_time") or 0.0))
            _nx = entry.get("next_section_name")
            if _nx and str(_nx) not in ("None", "") and hasattr(_rl, "set_montage_section_next_section"):
                _rl.set_montage_section_next_section(_m, entry.get("section_name"), str(_nx))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-restored"})
    elif op == "set_montage_section_time":
        # cross-module (anim_write.py, C++ #13): restore the section's prior start time. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        pt = entry.get("prior_start_time")
        if _m is None or _rl is None or pt is None or not hasattr(_rl, "set_montage_section_time"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.set_montage_section_time(_m, entry.get("section_name"), float(pt))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-time-restored"})
    elif op == "set_montage_section_next_section":
        # cross-module (anim_write.py, C++ #13): restore the section's prior next-link. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "set_montage_section_next_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _pn = entry.get("prior_next_section")
            _rl.set_montage_section_next_section(_m, entry.get("section_name"),
                "" if (_pn is None or str(_pn) == "None") else str(_pn))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-next-restored"})
    elif op == "add_skeleton_socket":
        # cross-module (skeleton_write.py, C++ #13): remove the skeleton socket we added. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "remove_skeleton_socket"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.remove_skeleton_socket(_s, entry.get("socket_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skeleton-socket-removed"})
    elif op == "remove_skeleton_socket":
        # cross-module (skeleton_write.py, C++ #13): re-add the socket with its captured bone + transform. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "add_skeleton_socket"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _lo = entry.get("location") or [0, 0, 0]
            _ro = entry.get("rotation") or [0, 0, 0]
            _sc = entry.get("scale") or [1, 1, 1]
            _rl.add_skeleton_socket(_s, entry.get("socket_name"), entry.get("bone") or "",
                float(_lo[0]), float(_lo[1]), float(_lo[2]),
                float(_ro[0]), float(_ro[1]), float(_ro[2]),
                float(_sc[0]), float(_sc[1]), float(_sc[2]))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skeleton-socket-restored"})
    elif op == "add_virtual_bone":
        # cross-module (skeleton_write.py, C++ #13): remove the virtual bone we added. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "remove_virtual_bone"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.remove_virtual_bone(_s, entry.get("virtual_bone_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "virtual-bone-removed"})
    elif op == "remove_virtual_bone":
        # cross-module (skeleton_write.py, C++ #13): re-add the virtual bone (engine re-assigns same name). FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "add_virtual_bone"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.add_virtual_bone(_s, entry.get("source"), entry.get("target"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "virtual-bone-restored"})
    elif op == "add_rig_vm_node":
        # cross-module (controlrig_graph_write.py): remove the RigVM node we added (unit/comment). FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_node"):
                _c.remove_node_by_name(unreal.Name(str(entry.get("node_name"))), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-node-removed"})
    elif op == "set_rig_vm_pin_default":
        # cross-module (controlrig_graph_write.py): restore the pin's captured prior default. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        pv = entry.get("prior_value")
        if _c is None or pv is None:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_vm_pin_default"):
                _c.set_pin_default_value(entry.get("pin_path"), pv, True, True, False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-pin-default-restored"})
    elif op == "set_rig_vm_node_position":
        # cross-module (controlrig_graph_write.py): restore the node's captured prior position. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        pp = entry.get("prior_pos")
        if _c is None or not pp:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_vm_node_position"):
                _c.set_node_position_by_name(unreal.Name(str(entry.get("node_name"))), unreal.Vector2D(float(pp[0]), float(pp[1])), True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-node-position-restored"})
    elif op == "add_rig_vm_link":
        # cross-module (controlrig_graph_write.py): break the link + reconnect the input's prior sources. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_link"):
                _c.break_link(entry.get("output_pin"), entry.get("input_pin"), True, False)
                for _s in (entry.get("prior_sources") or []):
                    _c.add_link(_s, entry.get("input_pin"), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-link-reverted"})
    elif op == "break_rig_vm_link":
        # cross-module (controlrig_graph_write.py): re-add the link we broke. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo break_rig_vm_link"):
                _c.add_link(entry.get("output_pin"), entry.get("input_pin"), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-link-restored"})
    elif op == "remove_rig_vm_node":
        # cross-module (controlrig_graph_write.py): re-add the removed node (kind-aware) + its pin defaults. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        _pos = entry.get("position")
        if _c is None or not _pos:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            _v2 = unreal.Vector2D(float(_pos[0]), float(_pos[1]))
            _kind = entry.get("node_kind"); _nm = str(entry.get("node_name"))
            with unreal.ScopedEditorTransaction("MCP undo remove_rig_vm_node"):
                if _kind == "unit":
                    _c.add_unit_node_from_struct_path(entry.get("struct_path"), entry.get("method") or "Execute", _v2, _nm, True, False)
                elif _kind == "comment":
                    _cm = entry.get("comment") or {}
                    _sz = _cm.get("size") or [100, 100]; _cl = _cm.get("color") or [1, 1, 1, 1]
                    _c.add_comment_node(_cm.get("text") or "", _v2, unreal.Vector2D(float(_sz[0]), float(_sz[1])),
                        unreal.LinearColor(float(_cl[0]), float(_cl[1]), float(_cl[2]), float(_cl[3])), _nm, True, False)
                for _pp, _vv in (entry.get("pin_defaults") or {}).items():
                    _c.set_pin_default_value(_pp, _vv, True, True, False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("rigvm-node-restored" if _kind in ("unit", "comment") else "rigvm-node-restore-unsupported")})
    elif op == "add_rig_vm_local_variable":
        # cross-module (controlrig_graph_write.py): remove the graph-scoped local variable we declared.
        # Local variables live on FUNCTION/COLLAPSE graphs -> _crg_ctrl (R11 fix) resolves those. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_local_variable"):
                _c.remove_local_variable(unreal.Name(str(entry.get("var_name"))), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-local-variable-removed"})
    elif op == "collapse_rig_vm_nodes":
        # cross-module (controlrig_graph_write.py): remove the collapse node + re-import the original subgraph. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        _snap = entry.get("snapshot")
        if _c is None or not _snap:
            undone.append({**entry, "result": "cr-graph-or-snapshot-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo collapse_rig_vm_nodes"):
                try: _c.remove_node_by_name(unreal.Name(str(entry.get("collapse_node_name"))), True, False)
                except Exception: pass
                _c.import_nodes_from_text(_snap, True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-collapse-reverted"})
    elif op == "expand_rig_vm_node":
        # cross-module (controlrig_graph_write.py): remove the expanded children + re-import the library node. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        _snap = entry.get("snapshot")
        if _c is None or not _snap:
            undone.append({**entry, "result": "cr-graph-or-snapshot-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo expand_rig_vm_node"):
                for _en in (entry.get("expanded_names") or []):
                    try: _c.remove_node_by_name(unreal.Name(str(_en)), True, False)
                    except Exception: pass
                _c.import_nodes_from_text(_snap, True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-expand-reverted"})
    elif op == "promote_rig_vm_node":
        # cross-module (controlrig_graph_write.py): remove the promoted node + re-import the original node. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        _snap = entry.get("snapshot")
        if _c is None or not _snap:
            undone.append({**entry, "result": "cr-graph-or-snapshot-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo promote_rig_vm_node"):
                try: _c.remove_node_by_name(unreal.Name(str(entry.get("new_node_name"))), True, False)
                except Exception: pass
                _c.import_nodes_from_text(_snap, True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-promote-reverted"})
    elif op == "add_rig_module":
        # cross-module (controlrig_cpp.py): delete the module we installed (ALSO the mirror_rig_module inverse). FAITHFUL.
        ap = entry.get("asset_path"); _mc = _modular_ctrl(ap)
        if _mc is None:
            undone.append({**entry, "result": "cr-modular-controller-absent"})
        else:
            try: _mc.delete_module(unreal.Name(str(entry.get("module_name"))), True)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modular-module-deleted"})
    elif op == "connect_rig_module_connector":
        # cross-module (controlrig_cpp.py): disconnect the connector we connected (prior target not captured). BEST-EFFORT.
        ap = entry.get("asset_path"); _mc = _modular_ctrl(ap)
        if _mc is None:
            undone.append({**entry, "result": "cr-modular-controller-absent"})
        else:
            _ck = unreal.RigElementKey(type=unreal.RigElementType.CONNECTOR, name=unreal.Name(str(entry.get("connector_name"))))
            try: _mc.disconnect_connector(_ck, True)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modular-connector-disconnected(best-effort)"})
    elif op == "auto_connect_rig_modules":
        # cross-module (controlrig_cpp.py): LOSSY inverse — disconnect the listed modules' connectors. BEST-EFFORT.
        ap = entry.get("asset_path"); _mc = _modular_ctrl(ap)
        if _mc is None:
            undone.append({**entry, "result": "cr-modular-controller-absent"})
        else:
            _dn = 0
            for _mn in (entry.get("module_names") or []):
                try:
                    for _k in (_mc.get_connectors_for_module(unreal.Name(str(_mn))) or []):
                        try: _mc.disconnect_connector(_k, True); _dn = _dn + 1
                        except Exception: pass
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modular-autoconnect-reverted-lossy-" + str(_dn)})
    elif op == "set_rig_module_config":
        # cross-module (controlrig_cpp.py): reset the config override we set (prior value not captured). FAITHFUL.
        ap = entry.get("asset_path"); _mc = _modular_ctrl(ap)
        if _mc is None:
            undone.append({**entry, "result": "cr-modular-controller-absent"})
        else:
            try: _mc.reset_config_value_in_module(unreal.Name(str(entry.get("module_name"))), unreal.Name(str(entry.get("variable_name"))), True)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modular-config-reset"})
    elif op == "bind_rig_module_variable":
        # cross-module (controlrig_cpp.py): unbind the module variable we bound. FAITHFUL.
        ap = entry.get("asset_path"); _mc = _modular_ctrl(ap)
        if _mc is None:
            undone.append({**entry, "result": "cr-modular-controller-absent"})
        else:
            try: _mc.un_bind_module_variable(unreal.Name(str(entry.get("module_name"))), unreal.Name(str(entry.get("variable_name"))), True)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modular-variable-unbound"})
    elif op == "add_event_dispatcher":
        # cross-module (blueprints_write.py): remove the event dispatcher we added (by name). FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            unreal.BlueprintEditorLibrary.remove_event_dispatcher(bp, unreal.Name(entry.get("name")))
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "dispatcher-removed"})
    elif op == "remove_component":
        # cross-module (editor_actor_components.py): re-add the instanced component we removed
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            L = unreal.SubobjectDataBlueprintFunctionLibrary
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            root = handles[0] if handles else None
            parent_h = root
            ap = entry.get("attach_parent")
            if ap:
                for h in handles:
                    d = sods.k2_find_subobject_data_from_handle(h)
                    o = L.get_associated_object(d) if d else None
                    if o is not None and o.get_name() == ap:
                        parent_h = h; break
            cls = getattr(unreal, entry.get("component_class", ""), None)
            if cls is None or parent_h is None:
                undone.append({**entry, "result": "class-or-parent-unresolved"})
            else:
                p = unreal.AddNewSubobjectParams()
                p.set_editor_property("parent_handle", parent_h)
                p.set_editor_property("new_class", cls)
                p.set_editor_property("blueprint_context", None)
                p.set_editor_property("conform_transform_to_parent", True)
                with unreal.ScopedEditorTransaction("MCP undo remove_component"):
                    nh, fail = sods.add_new_subobject(p)
                    comp = None
                    if not str(fail):
                        d2 = sods.k2_find_subobject_data_from_handle(nh)
                        comp = L.get_associated_object(d2) if d2 else None
                        want = entry.get("component_name")
                        if comp is not None and want and comp.get_name() != want:
                            sods.rename_subobject(nh, want)
                        tx = entry.get("transform")
                        if comp is not None and tx and isinstance(comp, unreal.SceneComponent):
                            if tx.get("loc") is not None:
                                v = tx["loc"]; comp.set_editor_property("relative_location", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                            if tx.get("rot") is not None:
                                v = tx["rot"]; comp.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2])))
                            if tx.get("scale") is not None:
                                v = tx["scale"]; comp.set_editor_property("relative_scale3d", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                        if comp is not None:
                            import warnings as _wn2; _wn2.simplefilter("ignore")
                            for pn, pv in (entry.get("props") or {}).items():
                                try:
                                    cur = comp.get_editor_property(pn); cj, _rr = _settable(cur)
                                    if cj != pv:
                                        comp.set_editor_property(pn, _coerce(cur, pv))
                                except Exception:
                                    pass
                undone.append({**entry, "result": ("re-added:" + (comp.get_name() if comp else "none")) if comp is not None else ("add-failed:" + str(fail))})
    elif op == "add_gameplay_tag":
        # cross-module (gameplay_tags_write.py): remove the tag we added (INI back to net-zero). FAITHFUL.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "remove_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            mrl.remove_gameplay_tag(entry.get("tag"))
            undone.append({**entry, "result": "tag-removed"})
    elif op == "remove_gameplay_tag":
        # cross-module (gameplay_tags_write.py): re-add the tag we removed (best-effort; DevComment only
        # restored if it was captured on removal).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "add_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            mrl.add_gameplay_tag(entry.get("tag"), entry.get("comment") or "")
            undone.append({**entry, "result": "tag-re-added"})
    elif op == "add_blueprint_variable":
        # cross-module (blueprints_write.py): remove the variable we added (by name). FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if bp is None or mrl is None or not hasattr(mrl, "remove_blueprint_variable"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            mrl.remove_blueprint_variable(bp, entry.get("var_name"))
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            undone.append({**entry, "result": "variable-removed"})
    elif op == "remove_blueprint_variable":
        # cross-module (blueprints_write.py): re-add the removed variable (same name + type; new default).
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        vt = entry.get("var_type")
        if bp is None or mrl is None or not hasattr(mrl, "add_blueprint_variable"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        elif not vt:
            undone.append({**entry, "result": "cannot-restore (type not captured)"})
        else:
            mrl.add_blueprint_variable(bp, entry.get("var_name"), vt)
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            undone.append({**entry, "result": "variable-re-added"})
    elif op == "add_widget":
        # cross-module (widgets_write.py): remove the widget we added (by name). FAITHFUL.
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if wb is None or mrl is None or not hasattr(mrl, "remove_widget_from_blueprint"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            mrl.remove_widget_from_blueprint(wb, entry.get("name"))
            try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("wbp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "widget-removed"})
    elif op == "remove_widget":
        # cross-module (widgets_write.py): re-add the removed widget (same class/name/parent; best-effort
        # -- child widgets of a removed panel are NOT restored).
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        cp = entry.get("widget_class_path")
        if wb is None or mrl is None or not hasattr(mrl, "add_widget_to_blueprint"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif not cp:
            undone.append({**entry, "result": "cannot-restore (class not captured)"})
        else:
            mrl.add_widget_to_blueprint(wb, cp, entry.get("name"), entry.get("parent_name") or "")
            try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("wbp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "widget-re-added"})
    elif op == "add_event_override":
        # cross-module (blueprints_write.py): remove the exact event node we added, by guid. FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ng = entry.get("node_guid")
        if bp is None or mrl is None or not hasattr(mrl, "remove_event_node_by_guid") or not ng:
            undone.append({**entry, "result": "blueprint-handler-or-guid-absent"})
        else:
            mrl.remove_event_node_by_guid(bp, ng)
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("bp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "event-node-removed"})
    elif op == "st_create":
        # cross-module (statetree_write.py): delete the created StateTree asset. FAITHFUL.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                unreal.EditorAssetLibrary.delete_asset(ap)
            undone.append({**entry, "result": "statetree-deleted"})
        except Exception as e:
            undone.append({**entry, "result": "delete-failed", "err": str(e)})
    elif op == "st_add_state":
        # cross-module (statetree_write.py): remove the state we added, by name. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        target = _st_find(ed, entry.get("state_name")) if ed is not None else None
        if ed is None or target is None:
            undone.append({**entry, "result": "state-absent"})
        else:
            subs = list(ed.get_editor_property("sub_trees") or [])
            if target in subs:
                ed.set_editor_property("sub_trees", [x for x in subs if x != target])
            else:
                for s in _st_iter(ed):
                    kk = list(s.get_editor_property("children") or [])
                    if target in kk:
                        s.set_editor_property("children", [x for x in kk if x != target]); break
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "state-removed"})
    elif op == "st_add_node":
        # cross-module (statetree_write.py): pop the node we appended at (state,kind,index). FAITHFUL.
        ap = entry.get("asset_path"); kind = entry.get("kind"); idx = entry.get("index")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        owner = _st_owner(ed, kind, entry.get("state_name")) if ed is not None else None
        if owner is None or kind not in ST_PROP or idx is None:
            undone.append({**entry, "result": "node-owner-absent"})
        else:
            arr = list(owner.get_editor_property(ST_PROP[kind]) or [])
            if 0 <= idx < len(arr):
                del arr[idx]; owner.set_editor_property(ST_PROP[kind], arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "node-removed"})
    elif op == "st_remove_node":
        # cross-module (statetree_write.py): re-import the removed node at its index. FAITHFUL.
        ap = entry.get("asset_path"); kind = entry.get("kind"); idx = entry.get("index"); tx = entry.get("export_text")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        owner = _st_owner(ed, kind, entry.get("state_name")) if ed is not None else None
        if owner is None or kind not in ST_PROP or not tx:
            undone.append({**entry, "result": "node-owner-or-capture-absent"})
        else:
            arr = list(owner.get_editor_property(ST_PROP[kind]) or [])
            if idx is None or idx > len(arr):
                idx = len(arr)
            arr.insert(idx, _st_import_node(tx)); owner.set_editor_property(ST_PROP[kind], arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "node-restored"})
    elif op == "st_remove_state":
        # cross-module (statetree_write.py): rebuild the snapshotted state subtree under its parent. FAITHFUL.
        ap = entry.get("asset_path"); snap = entry.get("snapshot"); parent = entry.get("parent")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        if ed is None or not snap:
            undone.append({**entry, "result": "editor-data-or-snapshot-absent"})
        else:
            news = _st_rebuild_state(ed, snap)
            par = _st_find(ed, parent) if (parent and parent != "(root)") else None
            if par is not None:
                _st_append(par, "children", news)
            else:
                _st_append(ed, "sub_trees", news)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "state-rebuilt"})
    elif op == "st_add_transition":
        # cross-module (statetree_write.py): remove the transition we appended at (state,index). FAITHFUL.
        ap = entry.get("asset_path"); idx = entry.get("index")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        state = _st_find(ed, entry.get("state_name")) if ed is not None else None
        if state is None or idx is None:
            undone.append({**entry, "result": "state-absent"})
        else:
            arr = list(state.get_editor_property("transitions") or [])
            if 0 <= idx < len(arr):
                del arr[idx]; state.set_editor_property("transitions", arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "transition-removed"})
    elif op == "st_remove_transition":
        # cross-module (statetree_write.py): re-import the removed transition at its index. FAITHFUL.
        ap = entry.get("asset_path"); idx = entry.get("index"); tx = entry.get("export_text")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        state = _st_find(ed, entry.get("state_name")) if ed is not None else None
        if state is None or not tx:
            undone.append({**entry, "result": "state-or-capture-absent"})
        else:
            arr = list(state.get_editor_property("transitions") or [])
            if idx is None or idx > len(arr):
                idx = len(arr)
            arr.insert(idx, _st_import_transition(tx)); state.set_editor_property("transitions", arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "transition-restored"})
    elif op == "st_set_state_prop":
        # cross-module (statetree_write.py): restore the prior state property value. FAITHFUL.
        ap = entry.get("asset_path"); prop = entry.get("property")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        state = _st_find(ed, entry.get("state_name")) if ed is not None else None
        if state is None or not prop:
            undone.append({**entry, "result": "state-absent"})
        else:
            try:
                state.set_editor_property(prop, _st_coerce_state(prop, entry.get("prior")))
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                undone.append({**entry, "result": "state-prop-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)})
    elif op == "st_set_schema":
        # cross-module (statetree_write.py): restore the prior schema class (or leave if none captured). FAITHFUL.
        ap = entry.get("asset_path"); pc = entry.get("prior_schema_class")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ed = _st_find_ed(st) if st is not None else None
        if ed is None:
            undone.append({**entry, "result": "editor-data-absent"})
        else:
            cls = getattr(unreal, pc, None) if pc else None
            if cls is not None:
                ed.set_editor_property("schema", unreal.new_object(cls, ed))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("schema-restored" if cls is not None else "no-prior-schema")})
    elif op == "asset_move_batch":
        # cross-module (asset_ops.py): rename each moved asset back (to -> from), LIFO. FAITHFUL.
        n = 0
        for it in reversed(entry.get("moves") or []):
            if _ao_rename(it.get("to"), _ao_dirof(it.get("from")), _ao_nameof(it.get("from"))):
                n += 1
        undone.append({**entry, "result": "asset-move-reverted", "restored": n})
    elif op == "asset_soft_delete":
        # cross-module (asset_ops.py): rename each asset back out of _MCP_Trash (to -> from). FAITHFUL.
        n = 0
        for it in reversed(entry.get("items") or []):
            if _ao_rename(it.get("to"), _ao_dirof(it.get("from")), _ao_nameof(it.get("from"))):
                n += 1
        undone.append({**entry, "result": "asset-soft-delete-restored", "restored": n})
    elif op == "widget_set_prop":
        # cross-module (widgets_write2.py): re-set the widget UPROPERTY to its captured prior. FAITHFUL.
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        w = _ww_find_widget(wb, entry.get("widget_name")) if wb is not None else None
        prop = entry.get("property")
        if w is None or not prop:
            undone.append({**entry, "result": "widget-absent"})
        else:
            try:
                w.set_editor_property(prop, _ww_desr(w.get_editor_property(prop), entry.get("prior")))
                _ww_compile_save(wb, entry.get("wbp_path"))
                undone.append({**entry, "result": "widget-prop-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "widget_set_slot_prop":
        # cross-module (widgets_write2.py): re-apply the captured prior to the widget's slot. FAITHFUL.
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        w = _ww_find_widget(wb, entry.get("widget_name")) if wb is not None else None
        prop = entry.get("property")
        slot = w.slot if (w is not None and hasattr(w, "slot")) else None
        if slot is None or not prop:
            undone.append({**entry, "result": "widget-or-slot-absent"})
        else:
            _ww_slot_set(slot, prop, _ww_desr(_ww_slot_get(slot, prop), entry.get("prior")))
            _ww_compile_save(wb, entry.get("wbp_path"))
            undone.append({**entry, "result": "widget-slot-prop-restored"})
    elif op == "widget_reparent":
        # cross-module (widgets_write2.py): reparent the widget back under its prior parent. FAITHFUL (hierarchy).
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        w = _ww_find_widget(wb, entry.get("widget_name")) if wb is not None else None
        prior_parent = _ww_find_widget(wb, entry.get("prior_parent_name")) if wb is not None else None
        if w is None or prior_parent is None or not isinstance(prior_parent, unreal.PanelWidget):
            undone.append({**entry, "result": "widget-or-prior-parent-absent"})
        else:
            try:
                cur = w.get_parent()
                if cur is not None:
                    cur.remove_child(w)
                prior_parent.add_child(w)
                _ww_compile_save(wb, entry.get("wbp_path"))
                undone.append({**entry, "result": "widget-reparented-back"})
            except Exception as e:
                undone.append({**entry, "result": "reparent-failed", "err": str(e)[:120]})
    elif op == "mi_set_parent":
        # cross-module (materials_write2.py): restore the MIC's prior parent material. FAITHFUL.
        ap = entry.get("asset_path")
        mic = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mic is None:
            undone.append({**entry, "result": "material-instance-absent"})
        else:
            MEL = unreal.MaterialEditingLibrary
            pp = entry.get("prior_parent_path")
            par = unreal.EditorAssetLibrary.load_asset(pp) if pp else None
            with unreal.ScopedEditorTransaction("MCP undo mi_set_parent"):
                MEL.set_material_instance_parent(mic, par)
                MEL.update_material_instance(mic)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            mic = None
            undone.append({**entry, "result": "mi-parent-restored"})
    elif op == "mi_clear_param":
        # cross-module (materials_write2.py): re-apply the prior override value that was cleared. FAITHFUL.
        ap = entry.get("asset_path")
        mic = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mic is None:
            undone.append({**entry, "result": "material-instance-absent"})
        else:
            G = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
            MEL = unreal.MaterialEditingLibrary
            nm = entry.get("parameter_name"); kind = entry.get("param_kind"); prior = entry.get("prior_value")
            with unreal.ScopedEditorTransaction("MCP undo mi_clear_param"):
                if kind == "scalar":
                    MEL.set_material_instance_scalar_parameter_value(mic, nm, float(prior), G)
                elif kind == "vector":
                    aa = float(prior[3]) if (isinstance(prior, (list, tuple)) and len(prior) > 3) else 1.0
                    MEL.set_material_instance_vector_parameter_value(mic, nm, unreal.LinearColor(float(prior[0]), float(prior[1]), float(prior[2]), aa), G)
                elif kind == "texture":
                    tex = unreal.EditorAssetLibrary.load_asset(prior) if prior else None
                    MEL.set_material_instance_texture_parameter_value(mic, nm, tex, G)
                elif kind == "static_switch":
                    MEL.set_material_instance_static_switch_parameter_value(mic, nm, bool(prior), G)
                MEL.update_material_instance(mic)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            mic = None
            undone.append({**entry, "result": "mi-param-reoverridden"})
    elif op == "mi_set_static_switch":
        # cross-module (materials_write2.py): restore prior static-switch value, or clear override if it was unset before. FAITHFUL.
        ap = entry.get("asset_path")
        mic = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mic is None:
            undone.append({**entry, "result": "material-instance-absent"})
        else:
            G = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
            MEL = unreal.MaterialEditingLibrary
            nm = entry.get("parameter_name"); prior = entry.get("prior_value")
            with unreal.ScopedEditorTransaction("MCP undo mi_set_static_switch"):
                if entry.get("was_overridden"):
                    MEL.set_material_instance_static_switch_parameter_value(mic, nm, bool(prior), G)
                else:
                    MEL.set_material_instance_parameter_override(mic, nm, False, G)
                MEL.update_material_instance(mic)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            mic = None
            undone.append({**entry, "result": "mi-static-switch-restored"})
    elif op == "set_material_property":
        # cross-module (materials_write2.py): restore a base Material render property (one recompile). FAITHFUL.
        ap = entry.get("asset_path")
        mat = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mat is None:
            undone.append({**entry, "result": "material-absent"})
        else:
            prop = entry.get("property"); kind = entry.get("kind"); prior = entry.get("prior")
            pv = prior
            if kind == "enum":
                ecls = getattr(unreal, entry.get("enum_class"), None)
                pv = getattr(ecls, prior, None) if ecls is not None else None
            if kind == "enum" and pv is None:
                undone.append({**entry, "result": "enum-member-unresolved"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo set_material_property"):
                    mat.set_editor_property(prop, pv)
                try:
                    unreal.MaterialEditingLibrary.recompile_material(mat)
                except Exception:
                    pass
                try:
                    unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception:
                    pass
                mat = None
                undone.append({**entry, "result": "material-property-restored"})
    elif op == "set_bp_var_props":
        # cross-module (blueprints_write2.py): restore prior variable category/replication. FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            BEL = unreal.BlueprintEditorLibrary
            vn = unreal.Name(entry.get("var_name"))
            with unreal.ScopedEditorTransaction("MCP undo set_bp_var_props"):
                if entry.get("changed_category"):
                    BEL.set_blueprint_variable_category(bp, vn, unreal.Text(str(entry.get("prior_category"))))
                if entry.get("changed_replication") and entry.get("prior_replication"):
                    rv = getattr(unreal.BlueprintVariableReplication, entry.get("prior_replication"), None)
                    if rv is not None:
                        BEL.set_blueprint_variable_replication(bp, vn, rv)
            try:
                BEL.compile_blueprint(bp)
            except Exception:
                pass
            try:
                unreal.EditorAssetLibrary.save_asset(entry.get("bp_path"), only_if_is_dirty=False)
            except Exception:
                pass
            bp = None
            undone.append({**entry, "result": "bp-var-props-restored"})
    elif op == "set_bp_class_default":
        # cross-module (blueprints_write2.py): restore prior CDO default value (one compile). FAITHFUL when restorable.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        elif not entry.get("restorable"):
            undone.append({**entry, "result": "prior-not-restorable; skipped"})
        else:
            BEL = unreal.BlueprintEditorLibrary
            gcls = BEL.generated_class(bp)
            cdo = unreal.get_default_object(gcls) if gcls is not None else None
            prop = entry.get("property")
            if cdo is None:
                undone.append({**entry, "result": "cdo-unresolvable"})
            else:
                try:
                    cur = cdo.get_editor_property(prop)
                    with unreal.ScopedEditorTransaction("MCP undo set_bp_class_default"):
                        cdo.set_editor_property(prop, _coerce(cur, entry.get("prior")))
                    try:
                        BEL.compile_blueprint(bp)
                    except Exception:
                        pass
                    try:
                        unreal.EditorAssetLibrary.save_asset(entry.get("bp_path"), only_if_is_dirty=False)
                    except Exception:
                        pass
                    undone.append({**entry, "result": "bp-class-default-restored"})
                except Exception as e:
                    undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
                cdo = None
    elif op == "rename_bp_function":
        # cross-module (blueprints_write2.py): rename the function graph back. FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            BEL = unreal.BlueprintEditorLibrary
            g = None
            try:
                g = BEL.find_graph(bp, entry.get("new_name"))
            except Exception:
                g = None
            if g is None:
                undone.append({**entry, "result": "renamed-graph-not-found"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo rename_bp_function"):
                    BEL.rename_graph(g, entry.get("old_name"))
                try:
                    BEL.compile_blueprint(bp)
                except Exception:
                    pass
                try:
                    unreal.EditorAssetLibrary.save_asset(entry.get("bp_path"), only_if_is_dirty=False)
                except Exception:
                    pass
                bp = None
                undone.append({**entry, "result": "bp-function-renamed-back"})
    elif op == "seq_add_track":
        # cross-module (sequencer_write_ext.py): remove the generic track we appended (master or binding).
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            cont = _sq_container(seq, entry.get("binding_name"))
            cls = getattr(unreal, str(entry.get("track_class") or ""), None)
            if cont is None or cls is None:
                undone.append({**entry, "result": "seq-container-or-class-absent"})
            else:
                ts = _sq_exact_tracks(cont, cls)
                if len(ts) > int(entry.get("prior_exact_count") or 0):
                    with unreal.ScopedEditorTransaction("MCP undo seq_add_track"):
                        cont.remove_track(ts[-1])
                    undone.append({**entry, "result": "seq-add-track-undone"})
                else:
                    undone.append({**entry, "result": "seq-add-track-count-stale"})
            seq = None
    elif op == "seq_remove_track":
        # cross-module (sequencer_write_ext.py): re-add the removed track and rebuild it from snapshot.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        snap = entry.get("snapshot") or {}
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            cont = _sq_container(seq, entry.get("binding_name"))
            cls = getattr(unreal, str(entry.get("track_class") or ""), None)
            if cont is None or cls is None:
                undone.append({**entry, "result": "seq-container-or-class-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_remove_track"):
                    tr = cont.add_track(cls)
                    if tr is not None:
                        _sq_rebuild_track(tr, snap)
                undone.append({**entry, "result": "seq-track-rebuilt"})
            seq = None
    elif op == "seq_add_section":
        # cross-module (sequencer_write_ext.py): remove the section we appended to the located track.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            tr, err = _sq_locate_track(seq, entry.get("binding_name"), entry.get("track_index", 0))
            if err or tr is None:
                undone.append({**entry, "result": "seq-track-absent"})
            else:
                secs = list(tr.get_sections() or [])
                if len(secs) > int(entry.get("prior_section_count") or 0):
                    with unreal.ScopedEditorTransaction("MCP undo seq_add_section"):
                        tr.remove_section(secs[-1])
                    undone.append({**entry, "result": "seq-add-section-undone"})
                else:
                    undone.append({**entry, "result": "seq-add-section-count-stale"})
            seq = None
    elif op == "seq_remove_section":
        # cross-module (sequencer_write_ext.py): re-add the removed section and rebuild it from snapshot.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        snap = entry.get("snapshot") or {}
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            tr, err = _sq_locate_track(seq, entry.get("binding_name"), entry.get("track_index", 0))
            if err or tr is None:
                undone.append({**entry, "result": "seq-track-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_remove_section"):
                    sec = tr.add_section()
                    if sec is not None:
                        _sq_rebuild_section(sec, snap)
                undone.append({**entry, "result": "seq-section-rebuilt"})
            seq = None
    elif op == "seq_add_event_section":
        # cross-module (sequencer_write_ext.py): remove the event section (and the event track if we added it).
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            ev_cls = getattr(unreal, "MovieSceneEventTrack", None)
            ts = _sq_exact_tracks(seq, ev_cls) if ev_cls is not None else []
            ti = int(entry.get("track_index") or 0)
            tr = ts[ti] if ti < len(ts) else (ts[-1] if ts else None)
            if tr is None:
                undone.append({**entry, "result": "seq-event-track-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_add_event_section"):
                    secs = list(tr.get_sections() or [])
                    if len(secs) > int(entry.get("prior_section_count") or 0):
                        tr.remove_section(secs[-1])
                    if entry.get("added_track"):
                        seq.remove_track(tr)
                undone.append({**entry, "result": "seq-event-section-undone"})
            seq = None
    elif op == "seq_add_timewarp":
        # cross-module (sequencer_write_ext.py): remove the timewarp track (+ its rate section) we appended.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            cls = getattr(unreal, "MovieSceneTimeWarpTrack", None)
            ts = _sq_exact_tracks(seq, cls) if cls is not None else []
            if cls is not None and len(ts) > int(entry.get("prior_exact_count") or 0):
                with unreal.ScopedEditorTransaction("MCP undo seq_add_timewarp"):
                    seq.remove_track(ts[-1])
                undone.append({**entry, "result": "seq-timewarp-undone"})
            else:
                undone.append({**entry, "result": "seq-timewarp-count-stale"})
            seq = None
    elif op == "seq_add_key":
        # cross-module (sequencer_edit.py): restore the key that add_key created or overwrote.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            seq = None
            with unreal.ScopedEditorTransaction("MCP undo seq_add_key"):
                _sq_inv_add_key(entry)
            undone.append({**entry, "result": "seq-add-key-undone"})
    elif op == "seq_add_keys_batch":
        # cross-module (sequencer_edit.py): restore every key the batch created or overwrote (reverse order).
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "seq-asset-absent"})
        else:
            seq = None
            with unreal.ScopedEditorTransaction("MCP undo seq_add_keys_batch"):
                for ent in reversed(entry.get("entries", []) or []):
                    ee = dict(ent)
                    ee["asset_path"] = entry.get("asset_path")
                    ee["binding"] = entry.get("binding")
                    ee["track_index"] = entry.get("track_index", 0)
                    ee["section_index"] = entry.get("section_index", 0)
                    _sq_inv_add_key(ee)
            undone.append({**entry, "result": "seq-add-keys-batch-undone"})
    elif op == "rename_folder":
        # cross-module (editor_complete.py): move the outliner subtree back from new -> old. FAITHFUL
        # (re-path each moved actor, matched by get_name(), preserving the nested suffix).
        old = entry.get("old"); new = entry.get("new"); moved = entry.get("moved") or []
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if eas is None or old is None or new is None:
            undone.append({**entry, "result": "editor-actor-subsystem-or-fields-absent"})
        else:
            mset = set(moved)
            acted = 0
            with unreal.ScopedEditorTransaction("MCP undo rename_folder"):
                for a in (eas.get_all_level_actors() or []):
                    if not a:
                        continue
                    try:
                        if a.get_name() not in mset:
                            continue
                        fp = str(a.get_folder_path() or "")
                    except Exception:
                        continue
                    if fp == new:
                        a.set_folder_path(unreal.Name(old)); acted += 1
                    elif fp.startswith(new + "/"):
                        a.set_folder_path(unreal.Name(old + fp[len(new):])); acted += 1
            undone.append({**entry, "result": "folder-renamed-back", "moved_back": acted})
    elif op == "rename_struct_field":
        # cross-module (structs_write.py): rename the field back (new_name -> old) via C++ RenameStructField. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "rename_struct_field"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo rename_struct_field"):
                mrl.rename_struct_field(obj, entry.get("name"), entry.get("prior_name"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            obj = None
            undone.append({**entry, "result": "struct-field-renamed-back"})
    elif op == "change_struct_field_type":
        # cross-module (structs_write.py): restore the field's prior type via C++ ChangeStructFieldType. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "change_struct_field_type"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo change_struct_field_type"):
                mrl.change_struct_field_type(obj, entry.get("name"), json.dumps(entry.get("prior_type_json") or {}))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            obj = None
            undone.append({**entry, "result": "struct-field-type-restored"})
    elif op == "set_struct_field_default":
        # cross-module (structs_write.py): restore the field's prior default via C++ SetStructFieldDefault. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_struct_field_default"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_struct_field_default"):
                mrl.set_struct_field_default(obj, entry.get("name"), entry.get("prior_default") or "")
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            obj = None
            undone.append({**entry, "result": "struct-field-default-restored"})
    elif op == "set_struct_field_tooltip":
        # cross-module (structs_write.py): restore the field's prior tooltip via C++ SetStructFieldTooltip. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_struct_field_tooltip"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_struct_field_tooltip"):
                mrl.set_struct_field_tooltip(obj, entry.get("name"), entry.get("prior_tooltip") or "")
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            obj = None
            undone.append({**entry, "result": "struct-field-tooltip-restored"})
    elif op == "add_anim_slot":
        # cross-module (anim_slots_write.py): if slot existed restore prior group, else remove it. FAITHFUL.
        ap = entry.get("skeleton_path")
        sk = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sk is None or mrl is None or not hasattr(mrl, "add_skeleton_slot"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_slot"):
                if entry.get("existed"):
                    mrl.add_skeleton_slot(sk, entry.get("slot_name"), entry.get("prior_group"))
                else:
                    mrl.remove_skeleton_slot(sk, entry.get("slot_name"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            sk = None
            undone.append({**entry, "result": "anim-slot-add-undone"})
    elif op == "remove_anim_slot":
        # cross-module (anim_slots_write.py): re-add the removed slot to its prior group. FAITHFUL.
        ap = entry.get("skeleton_path")
        sk = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sk is None or mrl is None or not hasattr(mrl, "add_skeleton_slot"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_slot"):
                mrl.add_skeleton_slot(sk, entry.get("slot_name"), entry.get("prior_group"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            sk = None
            undone.append({**entry, "result": "anim-slot-restored"})
    elif op == "rename_anim_slot":
        # cross-module (anim_slots_write.py): rename the slot back. FAITHFUL.
        ap = entry.get("skeleton_path")
        sk = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sk is None or mrl is None or not hasattr(mrl, "rename_skeleton_slot"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo rename_anim_slot"):
                mrl.rename_skeleton_slot(sk, entry.get("new_name"), entry.get("old_name"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            sk = None
            undone.append({**entry, "result": "anim-slot-renamed-back"})
    elif op == "add_anim_slot_group":
        # cross-module (anim_slots_write.py): remove the group we added (only if we actually added it). FAITHFUL.
        ap = entry.get("skeleton_path")
        sk = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sk is None or mrl is None or not hasattr(mrl, "remove_skeleton_slot_group"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        elif not entry.get("added"):
            undone.append({**entry, "result": "anim-slot-group-was-preexisting; noop"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_slot_group"):
                mrl.remove_skeleton_slot_group(sk, entry.get("group_name"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            sk = None
            undone.append({**entry, "result": "anim-slot-group-add-undone"})
    elif op == "remove_anim_slot_group":
        # cross-module (anim_slots_write.py): re-add the group + all its captured slots. FAITHFUL.
        ap = entry.get("skeleton_path")
        sk = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sk is None or mrl is None or not hasattr(mrl, "add_skeleton_slot_group"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_slot_group"):
                mrl.add_skeleton_slot_group(sk, entry.get("group_name"))
                for s in (entry.get("prior_slots") or []):
                    mrl.add_skeleton_slot(sk, s, entry.get("group_name"))
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            sk = None
            undone.append({**entry, "result": "anim-slot-group-restored"})
    elif op == "create_outliner_folder":
        # cross-module (editor_folders_write.py): delete the folder we created (only if we created it). FAITHFUL (empty).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "delete_outliner_folder"):
            undone.append({**entry, "result": "handler-absent"})
        elif not entry.get("created"):
            undone.append({**entry, "result": "outliner-folder-was-preexisting; noop"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo create_outliner_folder"):
                mrl.delete_outliner_folder(entry.get("folder_path"))
            undone.append({**entry, "result": "outliner-folder-create-undone"})
    elif op == "delete_outliner_folder":
        # cross-module (editor_folders_write.py): recreate the folder (faithful for EMPTY folders only).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "create_outliner_folder"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo delete_outliner_folder"):
                mrl.create_outliner_folder(entry.get("folder_path"))
            undone.append({**entry, "result": "outliner-folder-recreated"})
    elif op in ("anim_add_state", "anim_add_transition", "anim_set_entry_state", "anim_set_transition_property",
                "anim_remove_state", "anim_remove_transition", "anim_set_node_pin_exposure",
                "anim_bind_node_function", "anim_build_state_machine", "anim_create_layer_interface",
                "anim_add_state_machine", "anim_add_layer"):
        # cross-module (anim_statemachine_write.py): AnimGraph inverses re-call the C++ handler with captured priors.
        # Some edges are documented-lossy (rule-graph / prior-null entry not restored). No compile here (GC-safe) — save only.
        ap = entry.get("asset_path")
        ab = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or (ab is None and op != "anim_create_layer_interface"):
            undone.append({**entry, "result": "animbp-or-handler-absent"})
        else:
            _m = entry.get("machine"); _res = "anim-inverse-applied"
            try:
                with unreal.ScopedEditorTransaction("MCP undo " + op):
                    if op == "anim_add_state":
                        mrl.remove_anim_state(ab, _m, entry.get("state"))
                    elif op == "anim_add_transition":
                        mrl.remove_anim_transition(ab, _m, entry.get("from_state"), entry.get("to_state"))
                    elif op == "anim_add_state_machine":
                        # C++ #19: faithful remover (DestroyNode drops the SM node + its sub-graph + recompiles).
                        if hasattr(mrl, "remove_anim_state_machine_node"):
                            mrl.remove_anim_state_machine_node(ab, _m); _res = "anim-state-machine-removed"
                        else:
                            _res = "anim-state-machine-remover-absent; deferred"
                    elif op == "anim_add_layer":
                        # C++ #19: faithful remover (RemoveGraph on the named anim layer graph).
                        if hasattr(mrl, "remove_anim_layer_node"):
                            mrl.remove_anim_layer_node(ab, entry.get("layer_name")); _res = "anim-layer-removed"
                        else:
                            _res = "anim-layer-remover-absent; deferred"
                    elif op == "anim_set_entry_state":
                        if entry.get("prior_entry_state"):
                            mrl.set_anim_entry_state(ab, _m, entry.get("prior_entry_state"))
                        else:
                            _res = "anim-entry-prior-null; not-recleared"
                    elif op == "anim_set_transition_property":
                        mrl.set_anim_transition_property(ab, _m, entry.get("from_state"), entry.get("to_state"), entry.get("property"), entry.get("prior_value"))
                    elif op == "anim_remove_state":
                        mrl.add_anim_state(ab, _m, entry.get("state"))
                        for _tr in (entry.get("prior_transitions") or []):
                            try:
                                mrl.add_anim_transition(ab, _m, _tr.get("from_state") or _tr.get("from"), _tr.get("to_state") or _tr.get("to"))
                            except Exception:
                                pass
                        _res = "anim-state-readded; rules-lossy"
                    elif op == "anim_remove_transition":
                        mrl.add_anim_transition(ab, _m, entry.get("from_state"), entry.get("to_state"))
                        for _pk, _pv in (("PriorityOrder", entry.get("prior_priority_order")), ("CrossfadeDuration", entry.get("prior_crossfade_duration")), ("bDisabled", entry.get("prior_disabled"))):
                            if _pv is not None:
                                try:
                                    mrl.set_anim_transition_property(ab, _m, entry.get("from_state"), entry.get("to_state"), _pk, json.dumps(_pv))
                                except Exception:
                                    pass
                        _res = "anim-transition-readded; rules-lossy"
                    elif op == "anim_set_node_pin_exposure":
                        mrl.set_anim_node_pin_exposure(ab, entry.get("node_guid"), entry.get("property"), bool(entry.get("prior_exposed")))
                    elif op == "anim_bind_node_function":
                        mrl.bind_anim_node_function(ab, entry.get("node_guid"), entry.get("slot"), entry.get("prior_function") or "")
                    elif op == "anim_build_state_machine":
                        _spec = entry.get("spec") or {}
                        for _tr in (_spec.get("transitions") or []):
                            try:
                                mrl.remove_anim_transition(ab, _m, _tr.get("from"), _tr.get("to"))
                            except Exception:
                                pass
                        for _st in (_spec.get("states") or []):
                            try:
                                mrl.remove_anim_state(ab, _m, _st if isinstance(_st, str) else _st.get("name"))
                            except Exception:
                                pass
                        _res = "anim-build-partly-undone; machine-node-deferred"
                    elif op == "anim_create_layer_interface":
                        _pp = entry.get("package_path")
                        if _pp and unreal.EditorAssetLibrary.does_asset_exist(_pp):
                            _nm = _pp.split("/")[-1].split(".")[0]
                            unreal.EditorAssetLibrary.rename_asset(_pp, "/Game/MCP_Scratch/_MCP_Trash/" + _nm)
                        _res = "anim-layer-interface-trashed"
                if ab is not None and ap:
                    try:
                        unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                    except Exception:
                        pass
            except Exception as _e:
                _res = "anim-inverse-failed: " + str(_e)[:100]
            ab = None
            undone.append({**entry, "result": _res})
    elif op == "set_rig_preview_mesh":
        # cross-module (controlrig_write2.py): restore the CR blueprint's prior preview mesh. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None:
            undone.append({**entry, "result": "controlrig-absent"})
        else:
            _pm = entry.get("prior_mesh_path")
            _mesh = unreal.EditorAssetLibrary.load_asset(_pm) if _pm else None
            with unreal.ScopedEditorTransaction("MCP undo set_rig_preview_mesh"):
                try:
                    bp.set_preview_mesh(_mesh, True)
                except Exception:
                    pass
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            bp = None
            undone.append({**entry, "result": "rig-preview-mesh-restored"})
    elif op == "create_rig_vm_function":
        # cross-module (controlrig_write2.py): remove the RigVM function we added to the library. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None:
            undone.append({**entry, "result": "controlrig-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo create_rig_vm_function"):
                try:
                    _lib = bp.get_local_function_library()
                    bp.get_controller(_lib).remove_function_from_library(unreal.Name(entry.get("function_name")), True, False)
                except Exception:
                    pass
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            bp = None
            undone.append({**entry, "result": "rig-vm-function-removed"})
    elif op == "select_rig_elements":
        # cross-module (controlrig_write2.py): restore the prior hierarchy selection (transient; best-effort).
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None:
            undone.append({**entry, "result": "controlrig-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo select_rig_elements"):
                try:
                    _hc = bp.get_hierarchy_controller()
                    _hc.clear_selection()
                    for _k in (entry.get("prior_keys") or []):
                        try:
                            _et = getattr(unreal.RigElementType, str(_k.get("type")).upper(), None)
                            if _et is not None:
                                _hc.select_element(unreal.RigElementKey(type=_et, name=unreal.Name(_k.get("name"))), True)
                        except Exception:
                            pass
                except Exception:
                    pass
            bp = None
            undone.append({**entry, "result": "rig-selection-restored"})
    elif op == "set_rig_autosave":
        # cross-module (controlrig_write2.py): restore the prior auto-VM-recompile flag. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None:
            undone.append({**entry, "result": "controlrig-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_autosave"):
                try:
                    bp.set_auto_vm_recompile(bool(entry.get("prior_value")))
                except Exception:
                    pass
            bp = None
            undone.append({**entry, "result": "rig-autosave-restored"})
    elif op == "spawn_niagara_effect":
        # cross-module (niagara_write2.py): destroy the NiagaraActor we spawned. FAITHFUL.
        target = _find_by_name(entry.get("actor_name"))
        if target:
            with unreal.ScopedEditorTransaction("MCP undo spawn_niagara_effect"):
                eas.destroy_actor(target)
            undone.append({**entry, "result": "niagara-actor-destroyed"})
        else:
            undone.append({**entry, "result": "already-absent"})
    elif op == "control_niagara_effect":
        # cross-module (niagara_write2.py): restore prior is_active state. FAITHFUL for activate/deactivate;
        # best-effort for reset (no captured prior sim state).
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            comp = None
            cname = entry.get("component_name")
            try:
                comps = list(target.get_components_by_class(unreal.NiagaraComponent) or [])
            except Exception:
                comps = []
            for c in comps:
                if c.get_name() == cname:
                    comp = c; break
            if comp is None and comps:
                comp = comps[0]
            if comp is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo control_niagara_effect"):
                    try:
                        if entry.get("prev_active"):
                            comp.activate(True)
                        else:
                            comp.deactivate()
                    except Exception:
                        pass
                undone.append({**entry, "result": "niagara-active-restored"})
    elif op == "add_niagara_component":
        # cross-module (niagara_write2.py): delete the instanced UNiagaraComponent we added. FAITHFUL.
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            root = handles[0] if handles else None
            cname = entry.get("component_name")
            victim = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == cname:
                    victim = h; break
            if victim is None or root is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo add_niagara_component"):
                    sods.k2_delete_subobject_from_instance(root, victim)
                undone.append({**entry, "result": "niagara-component-removed"})
    elif op == "st_set_node_property":
        # cross-module (statetree_write2.py, C++ #18): re-set node property to captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "set_state_tree_node_property_json"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_set_node_property"):
                mrl.set_state_tree_node_property_json(st, entry.get("state_name") or "", entry.get("kind"),
                    entry.get("index"), entry.get("prop"), str(entry.get("prev")), entry.get("container") or "")
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-node-property-restored"})
    elif op == "st_set_transition_property":
        # cross-module (statetree_write2.py, C++ #18): restore prior transition property. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "set_state_tree_transition_property_json"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_set_transition_property"):
                mrl.set_state_tree_transition_property_json(st, entry.get("state_name"),
                    entry.get("index"), entry.get("prop"), str(entry.get("prev")))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-transition-property-restored"})
    elif op == "st_set_component_tree":
        # cross-module (statetree_write2.py, C++ #18): restore the component's prior StateTree. FAITHFUL.
        bpp = entry.get("blueprint_path")
        bp = unreal.EditorAssetLibrary.load_asset(bpp) if bpp else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if bp is None or mrl is None or not hasattr(mrl, "set_state_tree_component_tree_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_blueprint(bp) or []
            cname = entry.get("component_name"); comp = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == cname:
                    comp = o; break
            if comp is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                pst = entry.get("prev_state_tree")
                tree = unreal.EditorAssetLibrary.load_asset(pst) if (pst and pst != "None") else None
                with unreal.ScopedEditorTransaction("MCP undo st_set_component_tree"):
                    mrl.set_state_tree_component_tree_json(comp, entry.get("property_name"), tree, "")
                    try:
                        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
                    except Exception:
                        pass
                _st_save(bpp)
                undone.append({**entry, "result": "st-component-tree-restored"})
    elif op == "st_set_color":
        # cross-module (statetree_write2.py, C++ #18): restore prior color guid. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "set_state_tree_color_json"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_set_color"):
                mrl.set_state_tree_color_json(st, entry.get("state_name"), "", str(entry.get("prev")))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-color-restored"})
    elif op == "st_add_parameter":
        # cross-module (statetree_write2.py, C++ #18): remove the parameter we added. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "remove_state_tree_parameter"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_add_parameter"):
                mrl.remove_state_tree_parameter(st, entry.get("name"))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-parameter-removed"})
    elif op == "st_set_parameter":
        # cross-module (statetree_write2.py, C++ #18): restore prior parameter value. FAITHFUL for scalars.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "set_state_tree_parameter"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_set_parameter"):
                mrl.set_state_tree_parameter(st, entry.get("name"), str(entry.get("prev")))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-parameter-restored"})
    elif op == "st_remove_parameter":
        # cross-module (statetree_write2.py, C++ #18): re-add with captured type + value. FAITHFUL for scalars
        # (struct/enum/object type-objects not round-tripped -- documented lossy edge).
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "add_state_tree_parameter"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            pt = entry.get("type") or "float"
            with unreal.ScopedEditorTransaction("MCP undo st_remove_parameter"):
                mrl.add_state_tree_parameter(st, entry.get("name"), pt)
                pv = entry.get("value")
                if pv is not None and hasattr(mrl, "set_state_tree_parameter"):
                    try:
                        mrl.set_state_tree_parameter(st, entry.get("name"), str(pv))
                    except Exception:
                        pass
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-parameter-re-added"})
    elif op == "st_add_binding":
        # cross-module (statetree_write2.py, C++ #18): remove the binding we added. FAITHFUL for a fresh add.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "remove_state_tree_binding"):
            undone.append({**entry, "result": "statetree-or-handler-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_add_binding"):
                mrl.remove_state_tree_binding(st, entry.get("target_struct_id"), entry.get("target_property"))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-binding-removed"})
    elif op == "st_remove_binding":
        # cross-module (statetree_write2.py, C++ #18): re-add the removed binding from captured source. FAITHFUL.
        ap = entry.get("asset_path")
        st = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if st is None or mrl is None or not hasattr(mrl, "add_state_tree_binding") or not entry.get("source_struct_id"):
            undone.append({**entry, "result": "statetree-or-handler-or-source-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo st_remove_binding"):
                mrl.add_state_tree_binding(st, entry.get("source_struct_id"), entry.get("source_property"),
                    entry.get("target_struct_id"), entry.get("target_property"))
            _st_save(ap)
            st = None
            undone.append({**entry, "result": "st-binding-re-added"})
    elif op == "set_mpc_param":
        # cross-module (materials_write3.py): reverse an MPC parameter add/update/delete. FAITHFUL
        # (a re-added entry gets a fresh GUID -- name + default restored; Id is a protected field).
        ap = entry.get("asset_path")
        mpc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mpc is None:
            undone.append({**entry, "result": "mpc-absent"})
        else:
            kind = entry.get("param_kind"); nm = entry.get("parameter_name")
            prop = "scalar_parameters" if kind == "scalar" else "vector_parameters"
            arr = list(mpc.get_editor_property(prop) or [])
            idx = -1
            for i, e in enumerate(arr):
                if str(e.get_editor_property("parameter_name")) == nm:
                    idx = i; break
            pv = entry.get("prior_value")
            with unreal.ScopedEditorTransaction("MCP undo set_mpc_param"):
                if entry.get("deleted"):
                    ne = unreal.CollectionScalarParameter() if kind == "scalar" else unreal.CollectionVectorParameter()
                    ne.set_editor_property("parameter_name", nm)
                    ne.set_editor_property("default_value", float(pv) if kind == "scalar" else unreal.LinearColor(pv[0], pv[1], pv[2], pv[3]))
                    arr.append(ne); mpc.set_editor_property(prop, arr)
                    r = "mpc-param-re-added"
                elif entry.get("existed"):
                    if idx >= 0:
                        arr[idx].set_editor_property("default_value", float(pv) if kind == "scalar" else unreal.LinearColor(pv[0], pv[1], pv[2], pv[3]))
                        mpc.set_editor_property(prop, arr)
                    r = "mpc-param-restored"
                else:
                    if idx >= 0:
                        del arr[idx]; mpc.set_editor_property(prop, arr)
                    r = "mpc-param-removed"
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            mpc = None
            undone.append({**entry, "result": r})
    elif op == "implement_blueprint_interface":
        # cross-module (blueprints_iface_write.py, C++ #19): remove the interface we added. FAITHFUL.
        bpp = entry.get("blueprint")
        bp = unreal.EditorAssetLibrary.load_asset(bpp) if bpp else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if bp is None or mrl is None or not hasattr(mrl, "remove_blueprint_interface"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.remove_blueprint_interface(bp, entry.get("interface"))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(bpp, only_if_is_dirty=False)
            bp = None
            undone.append({**entry, "result": "bp-interface-removed"})
    elif op == "remove_blueprint_interface":
        # cross-module (blueprints_iface_write.py, C++ #19): re-add the interface. LOSSY (graphs not restored).
        bpp = entry.get("blueprint")
        bp = unreal.EditorAssetLibrary.load_asset(bpp) if bpp else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if bp is None or mrl is None or not hasattr(mrl, "implement_blueprint_interface"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.implement_blueprint_interface(bp, entry.get("interface"))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(bpp, only_if_is_dirty=False)
            bp = None
            undone.append({**entry, "result": "bp-interface-re-added; graphs-lossy"})
    elif op == "delete_material_expression":
        # cross-module (material_graph_write2.py): recreate the deleted node + rewire own_inputs, expr consumers, prop consumers. FAITHFUL (recompile).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        if mat is None or _MG is None:
            undone.append({**entry, "result": "material-absent"})
        else:
            _cls = getattr(unreal, entry.get("node_class") or "", None)
            if _cls is None:
                try: _cls = unreal.load_class(None, entry.get("node_class_path") or "")
                except Exception: _cls = None
            if _cls is None:
                undone.append({**entry, "result": "expr-class-unresolved"})
            else:
                _pos = entry.get("node_pos") or [0, 0]
                with unreal.ScopedEditorTransaction("MCP undo delete_material_expression"):
                    _ne = _MG.create_material_expression(mat, _cls, int(_pos[0]), int(_pos[1]))
                    if _ne is not None:
                        for _k, _v in (entry.get("node_props") or {}).items():
                            try: _ne.set_editor_property(_k, _mg_coerce(_v))
                            except Exception: pass
                        for _w in (entry.get("own_inputs") or []):
                            _s = _mg_find(mat, _w.get("src_name"))
                            if _s is not None:
                                try: _MG.connect_material_expressions(_s, _w.get("out_name") or "", _ne, _w.get("input") or "")
                                except Exception: pass
                        for _c in (entry.get("consumers") or []):
                            _co = _mg_find(mat, _c.get("consumer"))
                            if _co is not None:
                                try: _MG.connect_material_expressions(_ne, _c.get("out_name") or "", _co, _c.get("input") or "")
                                except Exception: pass
                        for _pc in (entry.get("prop_consumers") or []):
                            _p = _mg_prop(_pc.get("material_property"))
                            if _p is not None:
                                try: _MG.connect_material_property(_ne, _pc.get("out_name") or "", _p)
                                except Exception: pass
                    _MG.recompile_material(mat)
                _mg_finish(mat, ap); mat = None
                undone.append({**entry, "result": "material-expr-recreated"})
    elif op == "duplicate_material_expression":
        # cross-module (material_graph_write2.py): delete the disconnected copy we created. FAITHFUL (no recompile -- it was disconnected).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _e = _mg_find(mat, entry.get("new_name"))
        if mat is None or _e is None or _MG is None:
            undone.append({**entry, "result": "material-or-dup-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo duplicate_material_expression"):
                _MG.delete_material_expression(mat, _e)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            mat = None
            undone.append({**entry, "result": "material-dup-deleted"})
    elif op == "move_material_expression":
        # cross-module (material_graph_write2.py): restore the node's prior graph position. FAITHFUL (no recompile).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _e = _mg_find(mat, entry.get("node_name"))
        if mat is None or _e is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo move_material_expression"):
                try:
                    _e.set_editor_property("material_expression_editor_x", int(entry.get("prior_x") or 0))
                    _e.set_editor_property("material_expression_editor_y", int(entry.get("prior_y") or 0))
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            mat = None
            undone.append({**entry, "result": "material-expr-moved-back"})
    elif op == "layout_material_graph":
        # cross-module (material_graph_write2.py): restore every node's prior position. FAITHFUL (no recompile).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        if mat is None:
            undone.append({**entry, "result": "material-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo layout_material_graph"):
                for _nm, _xy in (entry.get("prior_positions") or {}).items():
                    _e = _mg_find(mat, _nm)
                    if _e is not None and _xy:
                        try:
                            _e.set_editor_property("material_expression_editor_x", int(_xy[0]))
                            _e.set_editor_property("material_expression_editor_y", int(_xy[1]))
                        except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            mat = None
            undone.append({**entry, "result": "material-layout-restored"})
    elif op == "build_material_graph":
        # cross-module (material_graph_write2.py): delete created nodes + restore conn_priors + (if cleared_first) recreate the whole prior graph. FAITHFUL (recompile).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        if mat is None or _MG is None:
            undone.append({**entry, "result": "material-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo build_material_graph"):
                for _nm in (entry.get("created") or []):
                    _e = _mg_find(mat, _nm)
                    if _e is not None:
                        try: _MG.delete_material_expression(mat, _e)
                        except Exception: pass
                for _c in (entry.get("conn_priors") or []):
                    if _c.get("kind") == "expr":
                        _to = _mg_find(mat, _c.get("to_expr"))
                        if _to is not None:
                            try:
                                if _c.get("had_prior"):
                                    _s = _mg_find(mat, _c.get("prior_src_name"))
                                    if _s is not None:
                                        _MG.connect_material_expressions(_s, _c.get("prior_out_name") or "", _to, _c.get("to_input") or "")
                                else:
                                    _MG.disconnect_material_expressions(_to, _c.get("to_input") or "")
                            except Exception: pass
                    else:
                        _p = _mg_prop(_c.get("material_property"))
                        if _p is not None:
                            try:
                                if _c.get("had_prior"):
                                    _s = _mg_find(mat, _c.get("prior_src_name"))
                                    if _s is not None:
                                        _MG.connect_material_property(_s, _c.get("prior_out_name") or "", _p)
                                else:
                                    _MG.disconnect_material_property(mat, _p)
                            except Exception: pass
                if entry.get("cleared_first"):
                    _m = {}
                    for _cap in (entry.get("cleared_nodes") or []):
                        _cc = getattr(unreal, _cap.get("node_class") or "", None)
                        if _cc is None:
                            try: _cc = unreal.load_class(None, _cap.get("node_class_path") or "")
                            except Exception: _cc = None
                        if _cc is None:
                            continue
                        _cpos = _cap.get("node_pos") or [0, 0]
                        _ne = _MG.create_material_expression(mat, _cc, int(_cpos[0]), int(_cpos[1]))
                        if _ne is not None:
                            for _k, _v in (_cap.get("node_props") or {}).items():
                                try: _ne.set_editor_property(_k, _mg_coerce(_v))
                                except Exception: pass
                            _m[_cap.get("node_name")] = _ne
                    for _cap in (entry.get("cleared_nodes") or []):
                        _dst = _m.get(_cap.get("node_name"))
                        if _dst is None:
                            continue
                        for _w in (_cap.get("own_inputs") or []):
                            _s = _m.get(_w.get("src_name")) or _mg_find(mat, _w.get("src_name"))
                            if _s is not None:
                                try: _MG.connect_material_expressions(_s, _w.get("out_name") or "", _dst, _w.get("input") or "")
                                except Exception: pass
                    for _mp in (entry.get("cleared_matprops") or []):
                        _s = _m.get(_mp.get("src_name")) or _mg_find(mat, _mp.get("src_name"))
                        _p = _mg_prop(_mp.get("material_property"))
                        if _s is not None and _p is not None:
                            try: _MG.connect_material_property(_s, _mp.get("out_name") or "", _p)
                            except Exception: pass
                _MG.recompile_material(mat)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-build-reverted"})
    elif op == "cleanup_material_graph":
        # cross-module (material_graph_write2.py): recreate each removed node + rewire own_inputs among them. FAITHFUL (recompile).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        if mat is None or _MG is None:
            undone.append({**entry, "result": "material-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo cleanup_material_graph"):
                _m = {}
                for _cap in (entry.get("removed_nodes") or []):
                    _cc = getattr(unreal, _cap.get("node_class") or "", None)
                    if _cc is None:
                        try: _cc = unreal.load_class(None, _cap.get("node_class_path") or "")
                        except Exception: _cc = None
                    if _cc is None:
                        continue
                    _cpos = _cap.get("node_pos") or [0, 0]
                    _ne = _MG.create_material_expression(mat, _cc, int(_cpos[0]), int(_cpos[1]))
                    if _ne is not None:
                        for _k, _v in (_cap.get("node_props") or {}).items():
                            try: _ne.set_editor_property(_k, _mg_coerce(_v))
                            except Exception: pass
                        _m[_cap.get("node_name")] = _ne
                for _cap in (entry.get("removed_nodes") or []):
                    _dst = _m.get(_cap.get("node_name"))
                    if _dst is None:
                        continue
                    for _w in (_cap.get("own_inputs") or []):
                        _s = _m.get(_w.get("src_name")) or _mg_find(mat, _w.get("src_name"))
                        if _s is not None:
                            try: _MG.connect_material_expressions(_s, _w.get("out_name") or "", _dst, _w.get("input") or "")
                            except Exception: pass
                _MG.recompile_material(mat)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-cleanup-restored"})
    elif op == "mf_build_graph":
        # cross-module (material_function_write.py): delete each created function expression. FAITHFUL (update_material_function).
        ap = entry.get("asset_path"); mf = _mf_fn(ap)
        if mf is None or _MG is None:
            undone.append({**entry, "result": "material-function-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo mf_build_graph"):
                for _nm in (entry.get("node_names") or []):
                    _e = _mf_find(mf, _nm)
                    if _e is not None:
                        try: _MG.delete_material_expression_in_function(mf, _e)
                        except Exception: pass
            _mf_update(mf, ap); mf = None
            undone.append({**entry, "result": "mf-build-deleted"})
    elif op == "mf_layout":
        # cross-module (material_function_write.py): restore each expression's prior position. FAITHFUL (no update).
        ap = entry.get("asset_path"); mf = _mf_fn(ap)
        if mf is None:
            undone.append({**entry, "result": "material-function-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo mf_layout"):
                for _pp in (entry.get("prior_positions") or []):
                    _e = _mf_find(mf, _pp.get("name"))
                    if _e is not None:
                        try:
                            _e.set_editor_property("material_expression_editor_x", int(_pp.get("x") or 0))
                            _e.set_editor_property("material_expression_editor_y", int(_pp.get("y") or 0))
                        except Exception: pass
            _mf_save(ap); mf = None
            undone.append({**entry, "result": "mf-layout-restored"})
    elif op == "mf_add_node":
        # cross-module (material_function_write.py): delete the FunctionInput/Output node we added. FAITHFUL (update_material_function).
        ap = entry.get("asset_path"); mf = _mf_fn(ap)
        _e = _mf_find(mf, entry.get("node_name"))
        if mf is None or _e is None or _MG is None:
            undone.append({**entry, "result": "material-function-or-node-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo mf_add_node"):
                try: _MG.delete_material_expression_in_function(mf, _e)
                except Exception: pass
            _mf_update(mf, ap); mf = None
            undone.append({**entry, "result": "mf-node-deleted"})
    elif op == "mf_set_node_props":
        # cross-module (material_function_write.py): restore each prior prop (input_type via FunctionInputType member name). FAITHFUL (update_material_function).
        ap = entry.get("asset_path"); mf = _mf_fn(ap)
        _e = _mf_find(mf, entry.get("node_name"))
        if mf is None or _e is None:
            undone.append({**entry, "result": "material-function-or-node-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo mf_set_node_props"):
                for _k, _v in (entry.get("prior_props") or {}).items():
                    try:
                        if _k == "input_type":
                            _fit = getattr(unreal.FunctionInputType, str(_v), None)
                            if _fit is not None:
                                _e.set_editor_property("input_type", _fit)
                        else:
                            _e.set_editor_property(_k, _v)
                    except Exception: pass
            _mf_update(mf, ap); mf = None
            undone.append({**entry, "result": "mf-node-props-restored"})
    elif op == "mf_cleanup":
        # cross-module (material_function_write.py): recreate each removed node + rewire captured inputs. Best-effort (update_material_function).
        ap = entry.get("asset_path"); mf = _mf_fn(ap)
        if mf is None or _MG is None:
            undone.append({**entry, "result": "material-function-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo mf_cleanup"):
                _m = {}
                for _rc in (entry.get("removed") or []):
                    _cc = getattr(unreal, _rc.get("class") or "", None)
                    if _cc is None:
                        continue
                    _ne = _MG.create_material_expression_in_function(mf, _cc, int(_rc.get("x") or 0), int(_rc.get("y") or 0))
                    if _ne is not None:
                        for _k, _v in (_rc.get("props") or {}).items():
                            try: _ne.set_editor_property(_k, _v)
                            except Exception: pass
                        _m[_rc.get("name")] = _ne
                for _rc in (entry.get("removed") or []):
                    _dst = _m.get(_rc.get("name"))
                    if _dst is None:
                        continue
                    for _ed in (_rc.get("inputs") or []):
                        _src = _m.get(_ed.get("from")) or _mf_find(mf, _ed.get("from"))
                        if _src is not None:
                            try: _MG.connect_material_expressions(_src, "", _dst, _ed.get("pin") or "")
                            except Exception: pass
            _mf_update(mf, ap); mf = None
            undone.append({**entry, "result": "mf-cleanup-restored"})
    elif op == "set_mfi_param":
        # cross-module (material_function_write.py): restore prior override value, or drop the entry if it did not exist before. FAITHFUL (no update).
        ap = entry.get("asset_path"); mfi = _mf_mfi(ap)
        _pt = entry.get("param_type")
        _prop = {"scalar": "scalar_parameter_values", "vector": "vector_parameter_values", "texture": "texture_parameter_values"}.get(_pt)
        if mfi is None or _prop is None:
            undone.append({**entry, "result": "material-function-instance-absent"})
        else:
            _arr = list(mfi.get_editor_property(_prop) or [])
            _nm = entry.get("parameter_name")
            _idx = -1
            for _i in range(len(_arr)):
                _pi = None
                try: _pi = _arr[_i].get_editor_property("parameter_info")
                except Exception: _pi = None
                if _pi is not None and str(_pi.get_editor_property("name")) == _nm:
                    _idx = _i; break
            with unreal.ScopedEditorTransaction("MCP undo set_mfi_param"):
                if entry.get("existed"):
                    if _idx >= 0:
                        _pv = entry.get("prior_value")
                        if _pt == "scalar":
                            _val = float(_pv) if _pv is not None else 0.0
                        elif _pt == "vector":
                            _val = unreal.LinearColor(float(_pv[0]), float(_pv[1]), float(_pv[2]), float(_pv[3])) if _pv else None
                        else:
                            _val = unreal.EditorAssetLibrary.load_asset(_pv) if _pv else None
                        try: _arr[_idx].set_editor_property("parameter_value", _val)
                        except Exception: pass
                        mfi.set_editor_property(_prop, _arr)
                else:
                    if _idx >= 0:
                        _arr.pop(_idx)
                        mfi.set_editor_property(_prop, _arr)
            _mf_save(ap); mfi = None
            undone.append({**entry, "result": "mfi-param-reverted"})
    elif op == "set_bp_var_flags":
        # cross-module (blueprints_write3.py): restore the member variable's prior editing flags via
        # BlueprintEditorLibrary (prior holds ONLY the settable flags that changed). FAITHFUL. Ledger key blueprint_path.
        bpp = entry.get("blueprint_path")
        bp = unreal.EditorAssetLibrary.load_asset(bpp) if bpp else None
        _bel = getattr(unreal, "BlueprintEditorLibrary", None)
        if bp is None or _bel is None:
            undone.append({**entry, "result": "blueprint-or-lib-absent"})
        else:
            prior = entry.get("prior") or {}
            vn = entry.get("variable_name")
            with unreal.ScopedEditorTransaction("MCP undo set_bp_var_flags"):
                if "instance_editable" in prior:
                    _bel.set_blueprint_variable_instance_editable(bp, unreal.Name(vn), bool(prior.get("instance_editable")))
                if "expose_on_spawn" in prior:
                    _bel.set_blueprint_variable_expose_on_spawn(bp, unreal.Name(vn), bool(prior.get("expose_on_spawn")))
                if "expose_to_cinematics" in prior:
                    _bel.set_blueprint_variable_expose_to_cinematics(bp, unreal.Name(vn), bool(prior.get("expose_to_cinematics")))
                try:
                    _bel.compile_blueprint(bp)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(bpp, only_if_is_dirty=False)
            bp = None
            undone.append({**entry, "result": "bp-var-flags-restored"})
    elif op == "add_blueprint_component":
        # cross-module (blueprint_components_cpp.py SCS): delete the component node we added. FAITHFUL (C++ handler compiles + marks dirty).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "delete_blueprint_component_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.delete_blueprint_component_json(ap, entry.get("component"))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-component-deleted"})
    elif op == "set_blueprint_component_property":
        # cross-module (blueprint_components_cpp.py SCS): re-apply the captured prior ExportText value (array-wrapped). FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_blueprint_component_property_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.set_blueprint_component_property_json(ap, entry.get("component"), entry.get("property"), json.dumps([entry.get("prev")]))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-component-property-restored"})
    elif op == "delete_blueprint_component":
        # cross-module (blueprint_components_cpp.py SCS): re-add the node (class+parent) + re-apply each prop_snapshot entry.
        # LOSSY: promoted children do NOT un-promote back under it; container/complex props not snapshotted.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_component_to_blueprint_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            cn = entry.get("component")
            try:
                mrl.add_component_to_blueprint_json(ap, entry.get("component_class") or "", cn, entry.get("parent") or "")
            except Exception:
                pass
            if hasattr(mrl, "set_blueprint_component_property_json"):
                for _pk, _pv in (entry.get("prop_snapshot") or {}).items():
                    try:
                        mrl.set_blueprint_component_property_json(ap, cn, _pk, json.dumps([_pv]))
                    except Exception:
                        pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-component-re-added; children-promotion-lossy"})
    elif op == "reparent_blueprint_component":
        # cross-module (blueprint_components_cpp.py SCS): reparent the node back to its prior parent (empty => root). FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "reparent_blueprint_component_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.reparent_blueprint_component_json(ap, entry.get("component"), entry.get("prior_parent") or "")
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-component-reparented-back"})
    elif op == "set_blueprint_root_component":
        # cross-module (blueprint_components_cpp.py SCS): promote the prior root back to root, then reparent this
        # node back to its prior parent. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_blueprint_root_component_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            _pr = entry.get("prior_root") or ""
            if _pr:
                try:
                    mrl.set_blueprint_root_component_json(ap, _pr)
                except Exception:
                    pass
            _npp = entry.get("node_prior_parent") or ""
            if _npp and hasattr(mrl, "reparent_blueprint_component_json"):
                try:
                    mrl.reparent_blueprint_component_json(ap, entry.get("component"), _npp)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-root-component-restored"})
    elif op == "delete_blueprint_node":
        # cross-module (blueprint_graph_cpp.py K2): delete the node we added (by guid). FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "delete_blueprint_node_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.delete_blueprint_node_json(ap, entry.get("graph_name") or "", entry.get("node_guid"))
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-node-deleted"})
    elif op == "break_blueprint_node_link":
        # cross-module (blueprint_graph_cpp.py K2): break the specific link we created. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "break_blueprint_node_link_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.break_blueprint_node_link_json(ap, entry.get("graph_name") or "", entry.get("node_guid"),
                    entry.get("pin"), entry.get("other_guid") or "", entry.get("other_pin") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-node-link-broken"})
    elif op == "set_blueprint_pin_default":
        # cross-module (blueprint_graph_cpp.py K2): restore the pin's prior default (object pin uses prev_object,
        # else prev_value). FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_blueprint_pin_default_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            if entry.get("is_object_pin"):
                _pv = entry.get("prev_object")
            else:
                _pv = entry.get("prev_value")
            try:
                mrl.set_blueprint_pin_default_json(ap, entry.get("graph_name") or "", entry.get("node_guid"),
                    entry.get("pin"), str(_pv if _pv is not None else ""))
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-pin-default-restored"})
    elif op == "reconnect_blueprint_links":
        # cross-module (blueprint_graph_cpp.py K2): reconnect each captured broken endpoint to (node_guid, pin). FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "connect_blueprint_nodes_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            _gn = entry.get("graph_name") or ""
            _ng = entry.get("node_guid"); _pin = entry.get("pin")
            for _b in (entry.get("broken") or []):
                try:
                    mrl.connect_blueprint_nodes_json(ap, _gn, _ng, _pin, _b.get("other_guid"), _b.get("other_pin"))
                except Exception:
                    pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-node-links-reconnected"})
    elif op == "readd_blueprint_node":
        # cross-module (blueprint_graph_cpp.py K2): re-create the deleted node from captured {kind,class,x,y,...},
        # re-apply captured input-pin defaults, reconnect captured links. LOSSY (best-effort; conversion nodes +
        # internal node state do NOT round-trip).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        cap = entry.get("captured") or {}
        if obj is None or mrl is None or not hasattr(mrl, "add_blueprint_node_json") or not cap:
            undone.append({**entry, "result": "blueprint-or-handler-or-capture-absent"})
        else:
            _gn = entry.get("graph_name") or ""
            _spec = {_k: _v for _k, _v in cap.items() if _k != "pins"}
            _newg = None
            try:
                _r = mrl.add_blueprint_node_json(ap, _gn, json.dumps(_spec))
                _rd = json.loads(_r) if isinstance(_r, str) else _r
                if isinstance(_rd, dict):
                    _newg = _rd.get("node_guid")
            except Exception:
                _newg = None
            if _newg:
                if hasattr(mrl, "set_blueprint_pin_default_json"):
                    for _p in (cap.get("pins") or []):
                        if str(_p.get("direction")) != "input":
                            continue
                        _dv = _p.get("default_object") or _p.get("default_value")
                        if _dv:
                            try:
                                mrl.set_blueprint_pin_default_json(ap, _gn, _newg, _p.get("name"), str(_dv))
                            except Exception:
                                pass
                if hasattr(mrl, "connect_blueprint_nodes_json"):
                    for _p in (cap.get("pins") or []):
                        _outp = str(_p.get("direction")) == "output"
                        for _lk in (_p.get("linked_to") or []):
                            try:
                                if _outp:
                                    mrl.connect_blueprint_nodes_json(ap, _gn, _newg, _p.get("name"), _lk.get("node_guid"), _lk.get("pin_name"))
                                else:
                                    mrl.connect_blueprint_nodes_json(ap, _gn, _lk.get("node_guid"), _lk.get("pin_name"), _newg, _p.get("name"))
                            except Exception:
                                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": ("bp-node-re-added; lossy" if _newg else "bp-node-readd-failed")})
    elif op == "set_blueprint_node_property":
        # cross-module (blueprint_graph_cpp.py K2): restore the node UPROPERTY's prior ExportText value. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_blueprint_node_property_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            _prv = entry.get("prev_value")
            try:
                mrl.set_blueprint_node_property_json(ap, entry.get("graph_name") or "", entry.get("node_guid"),
                    entry.get("property"), str(_prv if _prv is not None else ""))
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-node-property-restored"})
    elif op == "delete_blueprint_function":
        # cross-module (blueprint_func_cpp.py): the forward CREATED/overrode a function graph; delete it. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "delete_blueprint_function_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.delete_blueprint_function_json(ap, entry.get("function_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-function-deleted"})
    elif op == "remove_function_pin":
        # cross-module (blueprint_func_cpp.py): remove the input/output signature pin the forward add created
        # (is_output selects entry-output vs result-input). FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_function_pin_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.remove_function_pin_json(ap, entry.get("function_name") or "", entry.get("pin_name") or "", bool(entry.get("is_output")))
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-function-pin-removed"})
    elif op == "set_function_properties":
        # cross-module (blueprint_func_cpp.py): re-apply the captured PRIOR function flags+metadata (props holds prior).
        # FAITHFUL for the touched keys (a metadata key absent before is restored to '' -- see module docstring). compile after.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_function_properties_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.set_function_properties_json(ap, entry.get("function_name") or "", json.dumps(entry.get("props") or {}))
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-function-properties-restored"})
    elif op == "remove_local_variable":
        # cross-module (blueprint_func_cpp.py): remove the function-scoped local var the forward create added. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_local_variable_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.remove_local_variable_json(ap, entry.get("function_name") or "", entry.get("var_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-local-variable-removed"})
    elif op == "readd_blueprint_function":
        # cross-module (blueprint_func_cpp.py): re-create the deleted function as an EMPTY graph, then re-add each
        # captured input/output signature pin. LOSSY: body/wiring is NOT captured, and struct/object/enum pin types do
        # not round-trip (captured type carries sub_category_object; the re-add parser wants type_path) -- scalars re-add.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        cap = entry.get("captured") or {}
        if obj is None or mrl is None or not hasattr(mrl, "create_blueprint_function_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            _fn = entry.get("function_name") or ""
            try:
                mrl.create_blueprint_function_graph_json(ap, _fn, "")
            except Exception:
                pass
            if hasattr(mrl, "add_function_input_json"):
                for _p in (cap.get("inputs") or []):
                    try:
                        mrl.add_function_input_json(ap, _fn, _p.get("name") or "", json.dumps(_p.get("type") or {}))
                    except Exception:
                        pass
            if hasattr(mrl, "add_function_output_json"):
                for _p in (cap.get("outputs") or []):
                    try:
                        mrl.add_function_output_json(ap, _fn, _p.get("name") or "", json.dumps(_p.get("type") or {}))
                    except Exception:
                        pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-function-re-added; signature-only-lossy"})
    elif op == "delete_event_graph":
        # cross-module (blueprint_func_cpp.py): the forward CREATED an event graph (ubergraph page); delete it. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "delete_event_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.delete_event_graph_json(ap, entry.get("graph_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-event-graph-deleted"})
    elif op == "rename_event_graph":
        # cross-module (blueprint_func_cpp.py): rename the event graph back (old_name/new_name were pre-swapped at ledger time). FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "rename_event_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.rename_event_graph_json(ap, entry.get("old_name") or "", entry.get("new_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-event-graph-renamed-back"})
    elif op == "recreate_event_graph":
        # cross-module (blueprint_func_cpp.py): the forward DELETED an event graph; re-create an EMPTY graph of the same
        # name. LOSSY: node contents are NOT restored. compile after.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "create_event_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.create_event_graph_json(ap, entry.get("graph_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-event-graph-recreated; empty-graph-lossy"})
    elif op == "remove_event_dispatcher_input":
        # cross-module (blueprint_func_cpp.py): remove the dispatcher-signature param the forward add created. FAITHFUL (compile after).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_event_dispatcher_input_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.remove_event_dispatcher_input_json(ap, entry.get("dispatcher_name") or "", entry.get("pin_name") or "")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-dispatcher-input-removed"})
    elif op == "restore_blueprint_graph":
        # cross-module (blueprint_builders_cpp.py): re-import the captured PRIOR-graph build-spec via build mode
        # (document-pattern wipe-and-rebuild). snapshot_json is ALREADY a JSON string. LOSSY: only reconstructable node
        # kinds round-trip (unsupported kinds skipped -- see module docstring). compile after.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "build_blueprint_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.build_blueprint_graph_json(ap, entry.get("graph_name") or "", entry.get("snapshot_json") or "{}", "build")
            except Exception:
                pass
            if hasattr(mrl, "compile_blueprint_by_path"):
                try:
                    mrl.compile_blueprint_by_path(ap)
                except Exception:
                    pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-graph-restored; unsupported-kinds-lossy"})
    elif op == "restore_blueprint_node_positions":
        # cross-module (blueprint_builders_cpp.py): re-arrange the graph to the captured prior positions via the SAME
        # arrange handler with restore_positions. FAITHFUL. Node positions serialize directly -- NO compile.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "arrange_blueprint_graph_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.arrange_blueprint_graph_json(ap, entry.get("graph_name") or "", json.dumps({"restore_positions": entry.get("positions") or {}}))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-node-positions-restored"})
    elif op == "set_blueprint_variable_flags":
        # cross-module (blueprint_builders_cpp.py): re-apply the captured PRIOR variable flags (flags holds the prior
        # 6-key dict). FAITHFUL. Var flags serialize directly -- NO compile.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_blueprint_variable_flags_json"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            try:
                mrl.set_blueprint_variable_flags_json(ap, entry.get("variable_name") or "", json.dumps(entry.get("flags") or {}))
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            obj = None
            undone.append({**entry, "result": "bp-variable-flags-restored"})
    elif op == "set_widget_bp_parent":
        # cross-module (widgets_write4.py "W-A"): reparent the WidgetBlueprint ASSET back to its prior parent
        # class. FAITHFUL. (Structurally identical to reparent_blueprint but keys are asset_path/prior_parent.)
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        prior_cls = None
        pp = entry.get("prior_parent")
        if pp:
            try:
                prior_cls = unreal.load_object(None, pp)
            except Exception:
                prior_cls = None
        if obj is None or prior_cls is None:
            undone.append({**entry, "result": "widgetbp-or-prior-parent-absent"})
        else:
            try:
                unreal.BlueprintEditorLibrary.reparent_blueprint(obj, prior_cls)
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-bp-reparented-back"})
            except Exception as e:
                undone.append({**entry, "result": "reparent-failed", "err": str(e)[:120]})
    elif op == "set_widget_nav":
        # cross-module (widgets_write4.py "W-A"): rebuild the widget's UWidgetNavigation from the captured
        # whole-nav prior (None -> clear to default Escape; dict -> per-direction rule + widget_to_focus).
        # FAITHFUL. Python-native (probed feasible: new_object(WidgetNavigation) + whole-struct set).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        w = _ww_find_widget(obj, entry.get("widget_name")) if obj is not None else None
        if w is None:
            undone.append({**entry, "result": "widget-absent"})
        else:
            try:
                prior = entry.get("prior_nav_json")
                if not prior:
                    w.set_editor_property("navigation", None)
                else:
                    _ndirs = ["up", "down", "left", "right", "next", "previous"]
                    _rmap = {"ESCAPE": "ESCAPE", "STOP": "STOP", "WRAP": "WRAP", "EXPLICIT": "EXPLICIT",
                             "CUSTOM": "CUSTOM", "CUSTOMBOUNDARY": "CUSTOM_BOUNDARY",
                             "CUSTOM_BOUNDARY": "CUSTOM_BOUNDARY"}
                    nav = unreal.new_object(unreal.WidgetNavigation, outer=w)
                    for dr in _ndirs:
                        spec = prior.get(dr)
                        if not spec:
                            continue
                        data = nav.get_editor_property(dr)
                        _rk = str(spec.get("rule") or "ESCAPE").strip().upper().replace(" ", "")
                        _rk = _rmap.get(_rk, _rk)
                        r = getattr(unreal.UINavigationRule, _rk, None)
                        if r is not None:
                            data.set_editor_property("rule", r)
                        wtf = spec.get("widget_to_focus") or ""
                        if wtf:
                            data.set_editor_property("widget_to_focus", wtf)
                        nav.set_editor_property(dr, data)
                    w.set_editor_property("navigation", nav)
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-nav-restored"})
            except Exception as e:
                undone.append({**entry, "result": "nav-restore-failed", "err": str(e)[:120]})
    elif op == "widget_rename":
        # cross-module (widget_edit_cpp.py "W-B"): rename the widget back (new_name -> old_name). FAITHFUL.
        # The *_json handlers MarkModified only -> compile via _ww_compile_save to regenerate the widget class.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "rename_widget_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.rename_widget_json(ap, entry.get("new_name"), entry.get("old_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-renamed-back"})
            except Exception as e:
                undone.append({**entry, "result": "rename-failed", "err": str(e)[:120]})
    elif op == "widget_set_root":
        # cross-module (widget_edit_cpp.py "W-B"): point RootWidget back at the prior root. LOSSY when the tree
        # previously had NO root (the empty-tree pre-state is not restorable via set_root -> new root left in place).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_root_widget_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif not entry.get("had_prev_root") or not entry.get("prev_root"):
            undone.append({**entry, "result": "no-prior-root (lossy); left as-is"})
        else:
            try:
                mrl.set_root_widget_json(ap, entry.get("prev_root"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-root-restored"})
            except Exception as e:
                undone.append({**entry, "result": "set-root-failed", "err": str(e)[:120]})
    elif op == "widget_set_is_variable":
        # cross-module (widget_edit_cpp.py "W-B"): restore the prior bIsVariable flag. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_widget_is_variable_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.set_widget_is_variable_json(ap, entry.get("widget_name"), bool(entry.get("prev_is_variable")))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-is-variable-restored"})
            except Exception as e:
                undone.append({**entry, "result": "set-is-variable-failed", "err": str(e)[:120]})
    elif op == "widget_replace":
        # cross-module (widget_edit_cpp.py "W-B"): replace the widget back to its prior class. LOSSY -- ReplaceWidgets
        # only transfers class-compatible properties, so props unique to the swapped-in class are not recovered.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        oc = entry.get("old_class")
        if obj is None or mrl is None or not hasattr(mrl, "replace_widget_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif not oc:
            undone.append({**entry, "result": "cannot-restore (old class not captured)"})
        else:
            try:
                mrl.replace_widget_json(ap, entry.get("widget_name"), oc)
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-replaced-back; props-lossy"})
            except Exception as e:
                undone.append({**entry, "result": "replace-failed", "err": str(e)[:120]})
    elif op == "widget_wrap":
        # cross-module (widget_edit_cpp.py "W-B"): UNWRAP compound -- reparent the child out of the wrapper panel,
        # then drop the now-empty panel. FAITHFUL (hierarchy). RootWidget is not python-settable -> C++ set_root.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        child = _ww_find_widget(obj, entry.get("child_name")) if obj is not None else None
        panel = _ww_find_widget(obj, entry.get("panel_name")) if obj is not None else None
        if obj is None or mrl is None or not hasattr(mrl, "remove_widget_from_blueprint"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif child is None or panel is None:
            undone.append({**entry, "result": "wrapper-panel-or-child-absent"})
        else:
            try:
                try:
                    panel.remove_child(child)
                except Exception:
                    pass
                if entry.get("was_root"):
                    if hasattr(mrl, "set_root_widget_json"):
                        mrl.set_root_widget_json(ap, entry.get("child_name"))
                elif entry.get("parent_name"):
                    p = _ww_find_widget(obj, entry.get("parent_name"))
                    ci = entry.get("child_index")
                    if p is not None and isinstance(p, unreal.PanelWidget):
                        try:
                            p.insert_child_at(int(ci) if ci is not None else 0, child)
                        except Exception:
                            p.add_child(child)
                mrl.remove_widget_from_blueprint(obj, entry.get("panel_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-unwrapped"})
            except Exception as e:
                undone.append({**entry, "result": "unwrap-failed", "err": str(e)[:120]})
    elif op == "widget_set_named_slot":
        # cross-module (widget_edit_cpp.py "W-B"): restore the slot's prior content, or clear it if it was empty. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        host = entry.get("host_name"); slot = entry.get("slot_name")
        if obj is None or mrl is None or not hasattr(mrl, "set_named_slot_content_json") or not hasattr(mrl, "clear_named_slot_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                if entry.get("had_prev_content"):
                    mrl.set_named_slot_content_json(ap, host, slot, entry.get("prev_content") or "")
                else:
                    mrl.clear_named_slot_json(ap, host, slot)
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "named-slot-restored"})
            except Exception as e:
                undone.append({**entry, "result": "named-slot-restore-failed", "err": str(e)[:120]})
    elif op == "widget_clear_named_slot":
        # cross-module (widget_edit_cpp.py "W-B"): re-slot the prior content (no-op if the slot was already empty). FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        host = entry.get("host_name"); slot = entry.get("slot_name")
        if obj is None or mrl is None or not hasattr(mrl, "set_named_slot_content_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif not entry.get("had_prev_content"):
            undone.append({**entry, "result": "slot-was-empty; no-op"})
        else:
            try:
                mrl.set_named_slot_content_json(ap, host, slot, entry.get("prev_content") or "")
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "named-slot-content-re-slotted"})
            except Exception as e:
                undone.append({**entry, "result": "named-slot-restore-failed", "err": str(e)[:120]})
    elif op == "widget_add_binding":
        # cross-module (widget_edit_cpp.py "W-B"): restore the overwritten prior binding, or remove the added one. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        wn = entry.get("widget_name"); pn = entry.get("property_name")
        if obj is None or mrl is None or not hasattr(mrl, "add_property_binding_json") or not hasattr(mrl, "remove_property_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                if entry.get("had_existing"):
                    mrl.add_property_binding_json(ap, wn, pn, entry.get("prev_function") or "")
                else:
                    mrl.remove_property_binding_json(ap, wn, pn)
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "binding-restored"})
            except Exception as e:
                undone.append({**entry, "result": "binding-restore-failed", "err": str(e)[:120]})
    elif op == "widget_remove_binding":
        # cross-module (widget_edit_cpp.py "W-B"): re-add the removed binding with its captured function. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_property_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.add_property_binding_json(ap, entry.get("widget_name"), entry.get("property_name"), entry.get("prev_function") or "")
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "binding-re-added"})
            except Exception as e:
                undone.append({**entry, "result": "binding-re-add-failed", "err": str(e)[:120]})
    elif op == "wanim_create_animation":
        # cross-module (widget_anim_cpp.py "W-C"): drop the created UWidgetAnimation. FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_widget_animation_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_widget_animation_json(ap, entry.get("anim_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-animation-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-animation-failed", "err": str(e)[:120]})
    elif op == "wanim_remove_animation":
        # cross-module (widget_anim_cpp.py "W-C"): best-effort re-create the animation. LOSSY -- tracks/bindings/keys NOT restored.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "create_widget_animation_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.create_widget_animation_json(ap, entry.get("anim_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "widget-animation-recreated; tracks/bindings/keys-lossy"})
            except Exception as e:
                undone.append({**entry, "result": "recreate-animation-failed", "err": str(e)[:120]})
    elif op == "wanim_add_binding":
        # cross-module (widget_anim_cpp.py "W-C"): remove the added widget binding. FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_animation_widget_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_animation_widget_binding_json(ap, entry.get("anim_name"), entry.get("widget_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "animation-binding-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-binding-failed", "err": str(e)[:120]})
    elif op == "wanim_remove_binding":
        # cross-module (widget_anim_cpp.py "W-C"): re-add the removed widget binding (fresh GUID; tracks/keys NOT restored). LOSSY.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_animation_widget_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.add_animation_widget_binding_json(ap, entry.get("anim_name"), entry.get("widget_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "animation-binding-re-added; fresh-guid"})
            except Exception as e:
                undone.append({**entry, "result": "re-add-binding-failed", "err": str(e)[:120]})
    elif op == "uicomp_add":
        # cross-module (widget_uicomp_cpp.py "W-E"): detach the added UI component. FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_ui_component_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_ui_component_json(ap, entry.get("widget_name"), entry.get("component_class"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "ui-component-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-ui-component-failed", "err": str(e)[:120]})
    elif op == "uicomp_remove":
        # cross-module (widget_uicomp_cpp.py "W-E"): re-attach the removed UI component. LOSSY -- authored prop values NOT preserved.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_ui_component_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.add_ui_component_json(ap, entry.get("widget_name"), entry.get("component_class"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "ui-component-re-added; props-lossy"})
            except Exception as e:
                undone.append({**entry, "result": "re-add-ui-component-failed", "err": str(e)[:120]})
    elif op == "wanim_add_track":
        # cross-module (widget_anim_cpp.py "W-C"): remove the added property track (drops its sections). FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_animation_track_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_animation_track_json(ap, entry.get("anim_name"), entry.get("widget_name"), entry.get("track_type"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "animation-track-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-track-failed", "err": str(e)[:120]})
    elif op == "wanim_add_key":
        # cross-module (widget_anim_cpp.py "W-C"): reverse the key add. Fresh key -> remove it; if it REPLACED a
        # prior key (had_key) -> re-key the captured prev_value. time_seconds = frame * tick_den / tick_num. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        _tnum = entry.get("tick_resolution_num"); _tden = entry.get("tick_resolution_den")
        _ci = int(entry.get("channel_index") or 0)
        if obj is None or mrl is None or entry.get("frame") is None or not _tnum:
            undone.append({**entry, "result": "widgetbp-or-handler-or-frame-absent"})
        else:
            try:
                _tsec = float(entry.get("frame")) * float(_tden) / float(_tnum)
                if entry.get("had_key"):
                    if hasattr(mrl, "add_animation_key_json"):
                        mrl.add_animation_key_json(ap, entry.get("anim_name"), entry.get("widget_name"), entry.get("track_type"), _tsec, float(entry.get("prev_value")), _ci, "cubic")
                        _ww_compile_save(obj, ap)
                        undone.append({**entry, "result": "animation-key-prev-value-restored"})
                    else:
                        undone.append({**entry, "result": "add-key-handler-absent"})
                else:
                    if hasattr(mrl, "remove_animation_key_json"):
                        mrl.remove_animation_key_json(ap, entry.get("anim_name"), entry.get("widget_name"), entry.get("track_type"), _ci, _tsec)
                        _ww_compile_save(obj, ap)
                        undone.append({**entry, "result": "animation-key-removed"})
                    else:
                        undone.append({**entry, "result": "remove-key-handler-absent"})
            except Exception as e:
                undone.append({**entry, "result": "reverse-key-failed", "err": str(e)[:120]})
    elif op == "mvvm_add_viewmodel":
        # cross-module (widget_mvvm_cpp.py "W-D"): remove the added viewmodel. FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_mvvm_viewmodel_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_mvvm_viewmodel_json(ap, entry.get("name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-viewmodel-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-viewmodel-failed", "err": str(e)[:120]})
    elif op == "mvvm_remove_viewmodel":
        # cross-module (widget_mvvm_cpp.py "W-D"): best-effort re-add the viewmodel. LOSSY -- NEW guid, so bindings that referenced the old guid are NOT restored.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_mvvm_viewmodel_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.add_mvvm_viewmodel_json(ap, entry.get("class_path"), entry.get("name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-viewmodel-re-added; new-guid"})
            except Exception as e:
                undone.append({**entry, "result": "re-add-viewmodel-failed", "err": str(e)[:120]})
    elif op == "mvvm_rename_viewmodel":
        # cross-module (widget_mvvm_cpp.py "W-D"): rename the viewmodel back new->old. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "rename_mvvm_viewmodel_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.rename_mvvm_viewmodel_json(ap, entry.get("new_name"), entry.get("old_name"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-viewmodel-renamed-back"})
            except Exception as e:
                undone.append({**entry, "result": "rename-viewmodel-failed", "err": str(e)[:120]})
    elif op == "mvvm_set_viewmodel_settings":
        # cross-module (widget_mvvm_cpp.py "W-D"): re-apply the captured prior viewmodel settings. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_mvvm_viewmodel_settings_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.set_mvvm_viewmodel_settings_json(ap, entry.get("name"), json.dumps(entry.get("prev") or {}))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-viewmodel-settings-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-viewmodel-settings-failed", "err": str(e)[:120]})
    elif op == "mvvm_add_binding":
        # cross-module (widget_mvvm_cpp.py "W-D"): remove the added binding. FAITHFUL (structure).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_mvvm_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.remove_mvvm_binding_json(ap, entry.get("binding_id"))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-binding-removed"})
            except Exception as e:
                undone.append({**entry, "result": "remove-binding-failed", "err": str(e)[:120]})
    elif op == "mvvm_set_binding":
        # cross-module (widget_mvvm_cpp.py "W-D"): re-apply the captured prior binding fields. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_mvvm_binding_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.set_mvvm_binding_json(ap, entry.get("binding_id"), json.dumps(entry.get("prev") or {}))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-binding-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-binding-failed", "err": str(e)[:120]})
    elif op == "mvvm_remove_binding":
        # cross-module (widget_mvvm_cpp.py "W-D"): best-effort re-create the binding from its descriptor. LOSSY -- NEW guid, conversion functions NOT restored.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_mvvm_binding_json") or not entry.get("descriptor"):
            undone.append({**entry, "result": "widgetbp-or-handler-or-descriptor-absent"})
        else:
            try:
                mrl.add_mvvm_binding_json(ap, json.dumps(entry.get("descriptor")))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-binding-re-created; new-guid"})
            except Exception as e:
                undone.append({**entry, "result": "re-create-binding-failed", "err": str(e)[:120]})
    elif op == "mvvm_set_field_notify":
        # cross-module (widget_mvvm_cpp.py "W-D"): set the variable's FieldNotify back to prev_enabled. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_variable_field_notify_json"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            try:
                mrl.set_variable_field_notify_json(ap, entry.get("variable"), bool(entry.get("prev_enabled")))
                _ww_compile_save(obj, ap)
                undone.append({**entry, "result": "mvvm-field-notify-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-field-notify-failed", "err": str(e)[:120]})
    elif op == "set_project_setting":
        # cross-module (projectsettings_cpp.py, C++ #41): re-set the developer-setting's prior value + re-persist.
        # had_prior True -> faithful restore of prev; had_prior False -> best-effort re-set of prev (the added
        # ini key is not cleanly removable without a clear handler). ledger key is settings_class, not an asset.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        sc = entry.get("settings_class"); prop = entry.get("property")
        if mrl is None or not hasattr(mrl, "set_developer_setting_json") or not sc or not prop:
            undone.append({**entry, "result": "handler-or-op-absent"})
        else:
            try:
                mrl.set_developer_setting_json(sc, prop, json.dumps([entry.get("prev")]))
                undone.append({**entry, "result": ("project-setting-restored" if entry.get("had_prior") else "project-setting-reset-best-effort")})
            except Exception as e:
                undone.append({**entry, "result": "restore-project-setting-failed", "err": str(e)[:120]})
    elif op == "clear_foliage_instances":
        # cross-module (foliage_write.py): re-add the exact instances we cleared, at their captured
        # world-space transforms, into the level's InstancedFoliageActor. FAITHFUL (transforms captured pre-clear).
        ap = entry.get("foliage_type_path")
        ftype = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        _world = _ues.get_editor_world() if _ues else None
        if ftype is None or _world is None:
            undone.append({**entry, "result": "type-or-world-absent"})
        else:
            tfs = []
            for it in (entry.get("transforms") or []):
                loc = it.get("location") or [0.0, 0.0, 0.0]
                rot = it.get("rotation") or [0.0, 0.0, 0.0]
                scl = it.get("scale") or [1.0, 1.0, 1.0]
                _t = unreal.Transform()
                _t.translation = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
                _t.rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])).quaternion()
                _t.scale3d = unreal.Vector(float(scl[0]), float(scl[1]), float(scl[2]))
                tfs.append(_t)
            if tfs:
                with unreal.ScopedEditorTransaction("MCP undo clear_foliage_instances"):
                    unreal.InstancedFoliageActor.get_default_object().add_instances(_world, ftype, tfs)
            undone.append({**entry, "result": "foliage-instances-re-added", "instances_readded": len(tfs)})
            ftype = None
    elif op == "remove_foliage_type":
        # cross-module (foliage_write.py): un-soft-delete the FoliageType (rename trash -> original), then
        # re-add the exact captured instances. FAITHFUL (asset moved intact; world-space xforms captured).
        orig = entry.get("original_path"); trash = entry.get("trash_path")
        _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        _world = _ues.get_editor_world() if _ues else None
        moved_back = False
        if orig and unreal.EditorAssetLibrary.does_asset_exist(orig):
            moved_back = True  # already at original path
        elif orig and trash and unreal.EditorAssetLibrary.does_asset_exist(trash):
            moved_back = unreal.EditorAssetLibrary.rename_asset(trash, orig)
        ftype = unreal.EditorAssetLibrary.load_asset(orig) if orig else None
        readded = 0
        if ftype is not None and _world is not None:
            tfs = []
            for it in (entry.get("transforms") or []):
                loc = it.get("location") or [0.0, 0.0, 0.0]
                rot = it.get("rotation") or [0.0, 0.0, 0.0]
                scl = it.get("scale") or [1.0, 1.0, 1.0]
                _t = unreal.Transform()
                _t.translation = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
                _t.rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])).quaternion()
                _t.scale3d = unreal.Vector(float(scl[0]), float(scl[1]), float(scl[2]))
                tfs.append(_t)
            if tfs:
                with unreal.ScopedEditorTransaction("MCP undo remove_foliage_type"):
                    unreal.InstancedFoliageActor.get_default_object().add_instances(_world, ftype, tfs)
                readded = len(tfs)
        if entry.get("created_trash_dir"):
            try:
                _td = "/Game/_MCP_Trash"
                if unreal.EditorAssetLibrary.does_directory_exist(_td) and not (unreal.EditorAssetLibrary.list_assets(_td, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(_td)
            except Exception:
                pass
        ftype = None
        undone.append({**entry, "result": ("foliage-type-restored" if moved_back else "foliage-type-rename-failed"), "instances_readded": readded})
    elif op == "set_spline_points":
        # cross-module (spline_write.py): rebuild the WHOLE prior point array (locations, types, custom
        # tangents, closed-loop) on the actor's USplineComponent. FAITHFUL (prior whole-array captured).
        actor = _find_by_name(entry.get("actor_name"))
        comp = None
        if actor is not None:
            _comps = actor.get_components_by_class(unreal.SplineComponent) or []
            _cn = entry.get("component_name")
            for _c in _comps:
                if _c.get_name() == _cn:
                    comp = _c; break
            if comp is None and _comps:
                comp = _comps[0]
        if comp is None:
            undone.append({**entry, "result": "actor-or-spline-absent"})
        else:
            _cs = unreal.SplineCoordinateSpace.LOCAL if str(entry.get("coordinate_space")).lower() == "local" else unreal.SplineCoordinateSpace.WORLD
            prior = entry.get("prior") or {}
            pts = prior.get("points") or []
            with unreal.ScopedEditorTransaction("MCP undo set_spline_points"):
                comp.modify()
                comp.clear_spline_points(False)
                for _p in pts:
                    _loc = _p.get("location") or [0.0, 0.0, 0.0]
                    comp.add_spline_point(unreal.Vector(float(_loc[0]), float(_loc[1]), float(_loc[2])), _cs, False)
                for _i, _p in enumerate(pts):
                    _tn = str(_p.get("type") or "CURVE").upper()
                    comp.set_spline_point_type(_i, getattr(unreal.SplinePointType, _tn, unreal.SplinePointType.CURVE), False)
                    if _tn == "CURVE_CUSTOM_TANGENT":
                        _arr = _p.get("arrive") or [0.0, 0.0, 0.0]; _lev = _p.get("leave") or [0.0, 0.0, 0.0]
                        comp.set_tangents_at_spline_point(_i, unreal.Vector(float(_arr[0]), float(_arr[1]), float(_arr[2])), unreal.Vector(float(_lev[0]), float(_lev[1]), float(_lev[2])), _cs, False)
                comp.set_closed_loop(bool(prior.get("closed_loop")), False)
                comp.update_spline()
            undone.append({**entry, "result": "spline-points-restored", "points": len(pts)})
    elif op == "set_spline_point":
        # cross-module (spline_write.py): restore the ONE prior point (location, type, custom tangents)
        # on the actor's USplineComponent. FAITHFUL (single-point prior captured).
        actor = _find_by_name(entry.get("actor_name"))
        comp = None
        if actor is not None:
            _comps = actor.get_components_by_class(unreal.SplineComponent) or []
            _cn = entry.get("component_name")
            for _c in _comps:
                if _c.get_name() == _cn:
                    comp = _c; break
            if comp is None and _comps:
                comp = _comps[0]
        idx = int(entry.get("index", -1))
        if comp is None or idx < 0 or idx >= comp.get_number_of_spline_points():
            undone.append({**entry, "result": "spline-or-index-absent"})
        else:
            _cs = unreal.SplineCoordinateSpace.LOCAL if str(entry.get("coordinate_space")).lower() == "local" else unreal.SplineCoordinateSpace.WORLD
            prior = entry.get("prior") or {}
            _loc = prior.get("location") or [0.0, 0.0, 0.0]
            _tn = str(prior.get("type") or "CURVE").upper()
            with unreal.ScopedEditorTransaction("MCP undo set_spline_point"):
                comp.modify()
                comp.set_location_at_spline_point(idx, unreal.Vector(float(_loc[0]), float(_loc[1]), float(_loc[2])), _cs, False)
                comp.set_spline_point_type(idx, getattr(unreal.SplinePointType, _tn, unreal.SplinePointType.CURVE), False)
                if _tn == "CURVE_CUSTOM_TANGENT":
                    _arr = prior.get("arrive") or [0.0, 0.0, 0.0]; _lev = prior.get("leave") or [0.0, 0.0, 0.0]
                    comp.set_tangents_at_spline_point(idx, unreal.Vector(float(_arr[0]), float(_arr[1]), float(_arr[2])), unreal.Vector(float(_lev[0]), float(_lev[1]), float(_lev[2])), _cs, False)
                comp.update_spline()
            undone.append({**entry, "result": "spline-point-restored", "index": idx})
    elif op == "set_editor_mode":
        # cross-module (world_ext_cpp.py, C++ WorldExt): re-activate the prior editor mode via the C++
        # handler (empty prev_mode restores the default mode set). FAITHFUL.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_editor_mode_json"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            try:
                mrl.set_editor_mode_json(entry.get("prev_mode") or "")
                undone.append({**entry, "result": "editor-mode-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-editor-mode-failed", "err": str(e)[:120]})
    elif op == "set_world_partition_settings":
        # cross-module (world_ext_cpp.py, C++ WorldExt): re-apply the captured prior WP settings
        # {prop:value} via the C++ handler. FAITHFUL (prior scalar/ExportText values captured).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_world_partition_settings_json"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            try:
                mrl.set_world_partition_settings_json(json.dumps(entry.get("prev") or {}))
                undone.append({**entry, "result": "world-partition-settings-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-wp-settings-failed", "err": str(e)[:120]})
    elif op == "set_runtime_grid":
        # cross-module (world_ext_cpp.py, C++ WorldExt): re-apply the captured prior grid fields via the
        # C++ handler (grid_name + prev {field:value}). FAITHFUL. Requires a spatial-hash WP map.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_runtime_grid_json"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            try:
                _payload = {"grid_name": entry.get("grid_name")}
                _payload.update(entry.get("prev") or {})
                mrl.set_runtime_grid_json(json.dumps(_payload))
                undone.append({**entry, "result": "runtime-grid-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-runtime-grid-failed", "err": str(e)[:120]})
    elif op == "wp_load_actors":
        # cross-module (wp_write.py): the forward loaded these guids; inverse UNLOADS exactly them.
        wpbl = getattr(unreal, "WorldPartitionBlueprintLibrary", None)
        if wpbl is None:
            undone.append({**entry, "result": "wp-library-absent"})
        else:
            def _wf_guid(t):
                g = unreal.Guid()
                g.import_text(str(t))
                return g
            guids = []
            for t in (entry.get("guids") or []):
                try:
                    guids.append(_wf_guid(t))
                except Exception:
                    pass
            with unreal.ScopedEditorTransaction("MCP undo wp_load_actors"):
                try:
                    wpbl.unload_actors(guids)
                except Exception:
                    pass
            undone.append({**entry, "result": "wp-actors-unloaded", "guid_count": len(guids)})
    elif op == "wp_unload_actors":
        # cross-module (wp_write.py): the forward unloaded these guids; inverse RELOADS exactly them.
        wpbl = getattr(unreal, "WorldPartitionBlueprintLibrary", None)
        if wpbl is None:
            undone.append({**entry, "result": "wp-library-absent"})
        else:
            def _wf_guid(t):
                g = unreal.Guid()
                g.import_text(str(t))
                return g
            guids = []
            for t in (entry.get("guids") or []):
                try:
                    guids.append(_wf_guid(t))
                except Exception:
                    pass
            with unreal.ScopedEditorTransaction("MCP undo wp_unload_actors"):
                try:
                    wpbl.load_actors(guids)
                except Exception:
                    pass
            undone.append({**entry, "result": "wp-actors-reloaded", "guid_count": len(guids)})
    elif op == "wp_pin_actors":
        # cross-module (wp_write.py): the forward pinned these guids; inverse UNPINS exactly them.
        wpbl = getattr(unreal, "WorldPartitionBlueprintLibrary", None)
        if wpbl is None:
            undone.append({**entry, "result": "wp-library-absent"})
        else:
            def _wf_guid(t):
                g = unreal.Guid()
                g.import_text(str(t))
                return g
            guids = []
            for t in (entry.get("guids") or []):
                try:
                    guids.append(_wf_guid(t))
                except Exception:
                    pass
            with unreal.ScopedEditorTransaction("MCP undo wp_pin_actors"):
                try:
                    wpbl.unpin_actors(guids)
                except Exception:
                    pass
            undone.append({**entry, "result": "wp-actors-unpinned", "guid_count": len(guids)})
    elif op == "wp_unpin_actors":
        # cross-module (wp_write.py): the forward unpinned these guids; inverse PINS exactly them.
        wpbl = getattr(unreal, "WorldPartitionBlueprintLibrary", None)
        if wpbl is None:
            undone.append({**entry, "result": "wp-library-absent"})
        else:
            def _wf_guid(t):
                g = unreal.Guid()
                g.import_text(str(t))
                return g
            guids = []
            for t in (entry.get("guids") or []):
                try:
                    guids.append(_wf_guid(t))
                except Exception:
                    pass
            with unreal.ScopedEditorTransaction("MCP undo wp_unpin_actors"):
                try:
                    wpbl.pin_actors(guids)
                except Exception:
                    pass
            undone.append({**entry, "result": "wp-actors-repinned", "guid_count": len(guids)})
    elif op == "set_actor_spatially_loaded":
        # cross-module (wp_write.py): restore the captured prior is_spatially_loaded bool. FAITHFUL.
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_spatially_loaded"):
                try:
                    target.set_editor_property("is_spatially_loaded", bool(entry.get("prior")))
                except Exception:
                    pass
            undone.append({**entry, "result": "spatially-loaded-restored"})
    elif op == "set_actor_runtime_grid":
        # cross-module (wp_write.py): restore the captured prior runtime_grid FName. FAITHFUL.
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_runtime_grid"):
                try:
                    target.set_editor_property("runtime_grid", unreal.Name(str(entry.get("prior"))))
                except Exception:
                    pass
            undone.append({**entry, "result": "runtime-grid-restored"})
    elif op == "create_data_layer":
        # cross-module (datalayer_write.py): delete the instance we created, then the backing
        # DataLayerAsset, then the scratch dir if this call made it. CUSTOM op (not generic create_asset).
        ap = entry.get("asset_path")
        dls = unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
        def _wf_dl_asset_path(di):
            a = None
            for m in ("get_asset", "get_data_layer_asset"):
                try:
                    a = getattr(di, m)()
                    if a:
                        break
                except Exception:
                    a = None
            if a is None:
                try:
                    a = di.get_editor_property("data_layer_asset")
                except Exception:
                    a = None
            try:
                return a.get_path_name() if a else None
            except Exception:
                return None
        di = None
        for _di in list(dls.get_all_data_layers() or []):
            if _wf_dl_asset_path(_di) == ap:
                di = _di
                break
        with unreal.ScopedEditorTransaction("MCP undo create_data_layer"):
            if di is not None:
                try:
                    dls.delete_data_layer(di)
                except Exception:
                    pass
        di = None
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        cd = entry.get("created_dir")
        if deleted and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
            try:
                if not (unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("data-layer-deleted" if deleted else "instance-removed; asset-delete-failed")})
    elif op == "remove_data_layer":
        # cross-module (datalayer_write.py): recreate the instance from the persisted asset and restore
        # the 3 captured state flags. FAITHFUL (asset preserved; flags captured).
        ap = entry.get("asset_path")
        asset = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        dls = unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
        if asset is None:
            undone.append({**entry, "result": "data-layer-asset-absent"})
        else:
            di = None
            with unreal.ScopedEditorTransaction("MCP undo remove_data_layer"):
                try:
                    p = unreal.DataLayerCreationParameters()
                    p.set_editor_property("data_layer_asset", asset)
                    di = dls.create_data_layer_instance(p)
                except Exception:
                    di = None
                if di is not None:
                    rs = entry.get("initial_runtime_state")
                    if rs is not None:
                        try:
                            _rk = str(rs).split(".")[-1].split(":")[0].strip().upper()
                            dls.set_data_layer_initial_runtime_state(di, getattr(unreal.DataLayerRuntimeState, _rk))
                        except Exception:
                            pass
                    if entry.get("is_initially_visible") is not None:
                        try:
                            dls.set_data_layer_is_initially_visible(di, bool(entry.get("is_initially_visible")))
                        except Exception:
                            pass
                    if entry.get("is_loaded_in_editor") is not None:
                        try:
                            dls.set_data_layer_is_loaded_in_editor(di, bool(entry.get("is_loaded_in_editor")), False)
                        except Exception:
                            pass
            undone.append({**entry, "result": ("data-layer-recreated" if di is not None else "recreate-failed")})
    elif op == "set_data_layer_state":
        # cross-module (datalayer_write.py): restore each captured prior state flag on the resolved
        # instance (only changed flags were ledgered). FAITHFUL.
        dls = unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
        def _wf_dl_asset_path(di):
            a = None
            for m in ("get_asset", "get_data_layer_asset"):
                try:
                    a = getattr(di, m)()
                    if a:
                        break
                except Exception:
                    a = None
            if a is None:
                try:
                    a = di.get_editor_property("data_layer_asset")
                except Exception:
                    a = None
            try:
                return a.get_path_name() if a else None
            except Exception:
                return None
        def _wf_dl_short(di):
            for m in ("get_data_layer_short_name", "get_data_layer_instance_name"):
                f = getattr(di, m, None)
                if f is not None:
                    try:
                        v = str(f())
                        if v:
                            return v
                    except Exception:
                        pass
            try:
                return di.get_name()
            except Exception:
                return None
        def _wf_dl_full(di):
            try:
                return str(di.get_data_layer_full_name())
            except Exception:
                return None
        instances = list(dls.get_all_data_layers() or [])
        target = None
        if entry.get("dl_asset"):
            for di in instances:
                if _wf_dl_asset_path(di) == entry.get("dl_asset"):
                    target = di
                    break
        if target is None:
            for di in instances:
                _mf = entry.get("dl_full") and _wf_dl_full(di) == entry.get("dl_full")
                _ms = entry.get("dl_short") and _wf_dl_short(di) == entry.get("dl_short")
                if _mf or _ms:
                    target = di
                    break
        if target is None:
            undone.append({**entry, "result": "data-layer-absent"})
        else:
            prior = entry.get("prior") or {}
            with unreal.ScopedEditorTransaction("MCP undo set_data_layer_state"):
                if prior.get("initial_runtime_state") is not None:
                    try:
                        _rk = str(prior.get("initial_runtime_state")).split(".")[-1].split(":")[0].strip().upper()
                        dls.set_data_layer_initial_runtime_state(target, getattr(unreal.DataLayerRuntimeState, _rk))
                    except Exception:
                        pass
                if prior.get("is_initially_visible") is not None:
                    try:
                        dls.set_data_layer_is_initially_visible(target, bool(prior.get("is_initially_visible")))
                    except Exception:
                        pass
                if prior.get("is_loaded_in_editor") is not None:
                    try:
                        dls.set_data_layer_is_loaded_in_editor(target, bool(prior.get("is_loaded_in_editor")), False)
                    except Exception:
                        pass
            undone.append({**entry, "result": "data-layer-state-restored"})
    elif op == "create_hlod_layer":
        # cross-module (hlod_write.py): delete the HLODLayer asset we created (and the scratch dir if
        # this call made it). CUSTOM op; same delete mechanics as generic create_asset.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                _o = unreal.EditorAssetLibrary.load_asset(ap)
                if _o is not None:
                    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                    if aes:
                        aes.close_all_editors_for_asset(_o)
                _o = None
        except Exception:
            pass
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        cd = entry.get("created_dir")
        if deleted and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
            try:
                if not (unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("hlod-layer-deleted" if deleted else "delete-failed")})
    elif op == "set_actor_hlod_layer":
        # cross-module (hlod_write.py): restore the actor's prior hlod_layer assignment. FAITHFUL.
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            pp = entry.get("prior_path")
            prior_asset = unreal.EditorAssetLibrary.load_asset(pp) if pp else None
            with unreal.ScopedEditorTransaction("MCP undo set_actor_hlod_layer"):
                try:
                    target.set_editor_property("hlod_layer", prior_asset)
                except Exception:
                    pass
            undone.append({**entry, "result": "hlod-layer-restored"})
    elif op == "landscape_set_height_region":
        # cross-module (landscape_write.py): re-write the captured prior height region via the C++
        # edit-data bridge. FAITHFUL (whole prior region captured pre-write).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "landscape_set_height_region_json"):
            undone.append({**entry, "result": "landscape-handler-absent"})
        else:
            try:
                _r = mrl.landscape_set_height_region_json(entry.get("actor_name"),
                    int(entry.get("x")), int(entry.get("y")), int(entry.get("w")), int(entry.get("h")),
                    entry.get("prev_b64") or "", False)
                _rj = json.loads(_r) if isinstance(_r, str) else {}
                if isinstance(_rj, dict) and _rj.get("error"):
                    undone.append({**entry, "result": "restore-failed", "err": str(_rj.get("error"))[:120]})
                else:
                    undone.append({**entry, "result": "landscape-height-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "landscape_paint_weight_region":
        # cross-module (landscape_write.py): re-paint the captured prior weight region via the C++
        # edit-data bridge. FAITHFUL (bridge-returned prior weights captured).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "landscape_paint_weight_region_json"):
            undone.append({**entry, "result": "landscape-handler-absent"})
        else:
            try:
                _r = mrl.landscape_paint_weight_region_json(entry.get("actor_name"), entry.get("layer_name"),
                    int(entry.get("x")), int(entry.get("y")), int(entry.get("w")), int(entry.get("h")),
                    entry.get("prev_b64") or "", entry.get("layer_info_path") or "")
                _rj = json.loads(_r) if isinstance(_r, str) else {}
                if isinstance(_rj, dict) and _rj.get("error"):
                    undone.append({**entry, "result": "restore-failed", "err": str(_rj.get("error"))[:120]})
                else:
                    undone.append({**entry, "result": "landscape-weight-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "set_breakpoint":
        # cross-module (debug_write.py): a breakpoint was created/enabled. Inverse: if one existed before,
        # restore its prior enabled state; otherwise remove it. Transient editor state (cosmetic undo).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_blueprint_breakpoint_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                if entry.get("prior_exists"):
                    mrl.set_blueprint_breakpoint_json(entry.get("blueprint_path"), entry.get("graph") or "",
                        entry.get("node_guid"), bool(entry.get("prior_enabled")))
                    undone.append({**entry, "result": "breakpoint-restored"})
                else:
                    mrl.remove_blueprint_breakpoint_json(entry.get("blueprint_path"), entry.get("graph") or "",
                        entry.get("node_guid"), False)
                    undone.append({**entry, "result": "breakpoint-removed"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "remove_breakpoint":
        # cross-module (debug_write.py): a single-node breakpoint was removed. Inverse: re-create it with its
        # captured prior enabled state (only ledgered when one actually existed).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_blueprint_breakpoint_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                if entry.get("prior_exists"):
                    mrl.set_blueprint_breakpoint_json(entry.get("blueprint_path"), entry.get("graph") or "",
                        entry.get("node_guid"), bool(entry.get("prior_enabled")))
                    undone.append({**entry, "result": "breakpoint-recreated"})
                else:
                    undone.append({**entry, "result": "nothing-to-restore"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "set_pin_watch":
        # cross-module (debug_write.py): a pin watch was added/removed. Inverse: restore the prior watched
        # state (add if it was watched before, remove otherwise).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_blueprint_pin_watch_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                _restore_remove = not bool(entry.get("prior_watched"))
                mrl.set_blueprint_pin_watch_json(entry.get("blueprint_path"), entry.get("graph") or "",
                    entry.get("node_guid"), entry.get("pin_name"), _restore_remove)
                undone.append({**entry, "result": "pin-watch-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "set_debug_object":
        # cross-module (debug_write.py): the debug object changed. Inverse: restore the prior object (or clear
        # if there was none).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_blueprint_debug_object_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                _prior = entry.get("prior_instance_path")
                if _prior:
                    mrl.set_blueprint_debug_object_json(entry.get("blueprint_path"), _prior, False)
                    undone.append({**entry, "result": "debug-object-restored"})
                else:
                    mrl.set_blueprint_debug_object_json(entry.get("blueprint_path"), "", True)
                    undone.append({**entry, "result": "debug-object-cleared"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "set_bt_breakpoint":
        # cross-module (bt_debug_write.py): a BT breakpoint was set. Inverse: if one existed before, restore its
        # prior enabled state; otherwise remove it. Transient editor state (cosmetic undo).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_bt_breakpoint_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                if entry.get("prior_present"):
                    mrl.set_bt_breakpoint_json(entry.get("bt_path"), entry.get("node_id"),
                        bool(entry.get("prior_enabled")))
                    undone.append({**entry, "result": "bt-breakpoint-restored"})
                else:
                    mrl.remove_bt_breakpoint_json(entry.get("bt_path"), entry.get("node_id"), False)
                    undone.append({**entry, "result": "bt-breakpoint-removed"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "remove_bt_breakpoint":
        # cross-module (bt_debug_write.py): one or more BT breakpoints were cleared. Inverse: re-set each cleared
        # node with its captured prior enabled state.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "set_bt_breakpoint_json"):
            undone.append({**entry, "result": "debug-handler-absent"})
        else:
            try:
                _n = 0
                for _c in (entry.get("cleared") or []):
                    _nid = _c.get("node_id") or _c.get("node_guid")
                    if _nid:
                        mrl.set_bt_breakpoint_json(entry.get("bt_path"), _nid, bool(_c.get("prior_enabled")))
                        _n += 1
                undone.append({**entry, "result": "bt-breakpoints-restored", "restored": _n})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "mutable_set_param":
        # cross-module (mutable_write.py): re-apply the captured prior value of ONE instance parameter,
        # then non-validating save. Only BOOL/FLOAT/INT/COLOR reach here (setters refuse other types).
        try:
            _mi = unreal.EditorAssetLibrary.load_asset(entry.get("instance_path"))
            if _mi is None:
                undone.append({**entry, "result": "instance-not-found"})
            else:
                _ty = entry.get("param_type"); _nm = entry.get("param_name"); _pv = entry.get("prior_value")
                if _ty == "BOOL":
                    _mi.set_bool_parameter_selected_option(_nm, bool(_pv))
                elif _ty == "FLOAT":
                    _mi.set_float_parameter_selected_option(_nm, float(_pv))
                elif _ty == "INT":
                    _mi.set_int_parameter_selected_option(_nm, str(_pv))
                elif _ty == "COLOR":
                    _cc = unreal.LinearColor(float(_pv[0]), float(_pv[1]), float(_pv[2]),
                        float(_pv[3]) if isinstance(_pv, (list, tuple)) and len(_pv) > 3 else 1.0)
                    _mi.set_color_parameter_selected_option(_nm, _cc)
                unreal.EditorLoadingAndSavingUtils.save_packages([_mi.get_outermost()], False)
                undone.append({**entry, "result": "mutable-param-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "mutable_set_params":
        # cross-module (mutable_write.py): reset/paste captured MANY priors. Re-apply each, then save once.
        try:
            _mi = unreal.EditorAssetLibrary.load_asset(entry.get("instance_path"))
            if _mi is None:
                undone.append({**entry, "result": "instance-not-found"})
            else:
                for _p in (entry.get("priors") or []):
                    _ty = _p.get("type"); _nm = _p.get("name"); _pv = _p.get("value")
                    try:
                        if _ty == "BOOL":
                            _mi.set_bool_parameter_selected_option(_nm, bool(_pv))
                        elif _ty == "FLOAT":
                            _mi.set_float_parameter_selected_option(_nm, float(_pv))
                        elif _ty == "INT":
                            _mi.set_int_parameter_selected_option(_nm, str(_pv))
                        elif _ty == "COLOR":
                            _cc = unreal.LinearColor(float(_pv[0]), float(_pv[1]), float(_pv[2]),
                                float(_pv[3]) if isinstance(_pv, (list, tuple)) and len(_pv) > 3 else 1.0)
                            _mi.set_color_parameter_selected_option(_nm, _cc)
                    except Exception:
                        pass
                unreal.EditorLoadingAndSavingUtils.save_packages([_mi.get_outermost()], False)
                undone.append({**entry, "result": "mutable-params-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "mutable_set_state":
        # cross-module (mutable_write.py): restore the instance's prior active runtime state, then save.
        try:
            _mi = unreal.EditorAssetLibrary.load_asset(entry.get("instance_path"))
            if _mi is None:
                undone.append({**entry, "result": "instance-not-found"})
            else:
                _mi.set_current_state(entry.get("prior_state"))
                unreal.EditorLoadingAndSavingUtils.save_packages([_mi.get_outermost()], False)
                undone.append({**entry, "result": "mutable-state-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "sound_cue_add_node":
        # cross-module (soundcue_graph.py): a node was added + wired. Inverse: restore the prior wiring at
        # its placement (root -> prior first_node; parent -> prior child_nodes; floating -> orphan, no-op).
        try:
            _cue = _scg_load_cue(entry.get("asset_path"))
            if _cue is None:
                undone.append({**entry, "result": "cue-not-found"})
            else:
                _pl = entry.get("placement")
                if _pl == "root":
                    _pf = entry.get("prior_first_node_name")
                    _cue.set_editor_property("first_node", _scg_resolve(_cue, _pf) if _pf else None)
                elif _pl == "parent":
                    _pn = _scg_resolve(_cue, entry.get("parent_name"))
                    if _pn is not None:
                        _scg_write_children(_pn, entry.get("prior_children_names"), _cue)
                _scg_save(_cue)
                undone.append({**entry, "result": "sound-cue-add-node-reverted"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "sound_cue_connect":
        # cross-module (soundcue_graph.py): a child was wired into a parent's child_nodes. Inverse: restore
        # the parent's prior child array.
        try:
            _cue = _scg_load_cue(entry.get("asset_path"))
            if _cue is None:
                undone.append({**entry, "result": "cue-not-found"})
            else:
                _pn = _scg_resolve(_cue, entry.get("parent_name"))
                if _pn is not None:
                    _scg_write_children(_pn, entry.get("prior_children_names"), _cue)
                _scg_save(_cue)
                undone.append({**entry, "result": "sound-cue-connect-reverted"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "sound_cue_remove_node":
        # cross-module (soundcue_graph.py): a node was detached from first_node + parents. Inverse: re-wire it
        # (the node subobject persists, so re-resolving by name restores the exact prior wiring).
        try:
            _cue = _scg_load_cue(entry.get("asset_path"))
            if _cue is None:
                undone.append({**entry, "result": "cue-not-found"})
            else:
                if entry.get("was_root"):
                    _cue.set_editor_property("first_node", _scg_resolve(_cue, entry.get("node_name")))
                for _pr in (entry.get("parents") or []):
                    _pn = _scg_resolve(_cue, _pr.get("parent_name"))
                    if _pn is not None:
                        _scg_write_children(_pn, _pr.get("prior_children_names"), _cue)
                _scg_save(_cue)
                undone.append({**entry, "result": "sound-cue-remove-node-reverted"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "sound_cue_set_node_props":
        # cross-module (soundcue_graph.py): reflected props were set with faithful prior capture. Inverse:
        # restore each captured prior value by its kind.
        try:
            _cue = _scg_load_cue(entry.get("asset_path"))
            if _cue is None:
                undone.append({**entry, "result": "cue-not-found"})
            else:
                _nd = _scg_resolve(_cue, entry.get("node_name"))
                if _nd is None:
                    undone.append({**entry, "result": "node-not-found"})
                else:
                    for _k, _cap in (entry.get("prior") or {}).items():
                        try:
                            _scg_restore_prop(_nd, _k, _cap)
                        except Exception:
                            pass
                    _scg_save(_cue)
                    undone.append({**entry, "result": "sound-cue-props-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "delete_mutable_node":
        # cross-module (mutable_graph_cpp.py): a node was ADDED -> inverse deletes it.
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if _mrl is None or not hasattr(_mrl, "delete_mutable_node_json"):
            undone.append({**entry, "result": "mutable-handler-absent"})
        else:
            try:
                _mrl.delete_mutable_node_json(entry.get("asset_path"), entry.get("node_guid"))
                _mg_save(entry.get("asset_path"))
                undone.append({**entry, "result": "mutable-node-deleted"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "disconnect_mutable_pin":
        # cross-module (mutable_graph_cpp.py): a link was MADE -> inverse breaks that specific link.
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if _mrl is None or not hasattr(_mrl, "disconnect_mutable_pin_json"):
            undone.append({**entry, "result": "mutable-handler-absent"})
        else:
            try:
                _mrl.disconnect_mutable_pin_json(entry.get("asset_path"), entry.get("node_guid"),
                    entry.get("pin"), entry.get("other_guid") or "", entry.get("other_pin") or "")
                _mg_save(entry.get("asset_path"))
                undone.append({**entry, "result": "mutable-pin-disconnected"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "reconnect_mutable_links":
        # cross-module (mutable_graph_cpp.py): a pin's links were BROKEN -> inverse reconnects each captured endpoint.
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if _mrl is None or not hasattr(_mrl, "connect_mutable_nodes_json"):
            undone.append({**entry, "result": "mutable-handler-absent"})
        else:
            try:
                _n = 0
                for _b in (entry.get("broken") or []):
                    try:
                        _mrl.connect_mutable_nodes_json(entry.get("asset_path"), entry.get("node_guid"),
                            entry.get("pin"), _b.get("other_guid"), _b.get("other_pin"))
                        _n += 1
                    except Exception:
                        pass
                _mg_save(entry.get("asset_path"))
                undone.append({**entry, "result": "mutable-links-reconnected", "reconnected": _n})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "readd_mutable_node":
        # cross-module (mutable_graph_cpp.py): a node was DELETED -> inverse re-adds it (LOSSY: class+pos+links
        # best-effort; internal per-node state does not round-trip).
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if _mrl is None or not hasattr(_mrl, "add_mutable_node_json"):
            undone.append({**entry, "result": "mutable-handler-absent"})
        else:
            try:
                _cap = entry.get("captured") or {}
                _r = _mrl.add_mutable_node_json(entry.get("asset_path"), _cap.get("node_class"),
                    float(_cap.get("x", 0.0)), float(_cap.get("y", 0.0)))
                _rj = json.loads(_r) if isinstance(_r, str) else {}
                _newg = _rj.get("node_guid")
                if _newg and hasattr(_mrl, "connect_mutable_nodes_json"):
                    for _pin in (_cap.get("pins") or []):
                        for _lk in (_pin.get("linked_to") or []):
                            try:
                                _mrl.connect_mutable_nodes_json(entry.get("asset_path"), _newg,
                                    _pin.get("name"), _lk.get("node_guid"), _lk.get("pin_name"))
                            except Exception:
                                pass
                _mg_save(entry.get("asset_path"))
                undone.append({**entry, "result": "mutable-node-readded", "lossy": True, "new_guid": _newg})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "set_mutable_node_property":
        # cross-module (mutable_graph_cpp.py): a node property was SET -> inverse restores the captured prior text.
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if _mrl is None or not hasattr(_mrl, "set_mutable_node_property_json"):
            undone.append({**entry, "result": "mutable-handler-absent"})
        else:
            try:
                _pv = entry.get("prev_value")
                _mrl.set_mutable_node_property_json(entry.get("asset_path"), entry.get("node_guid"),
                    entry.get("property"), _pv if _pv is not None else "")
                _mg_save(entry.get("asset_path"))
                undone.append({**entry, "result": "mutable-prop-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_add_node":
        # cross-module (metasound_write.py): a node was added -> inverse removes it (by reconstructed handle).
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _b.remove_node(_ms_node(entry.get("node_id")), True)
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-node-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_connect":
        # cross-module: a link was made -> inverse disconnects the target input.
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _th = _ms_node(entry.get("to_node_id"))
                _ih, _ri = _b.find_node_input_by_name(_th, entry.get("to_input_name"))
                _b.disconnect_node_input(_ih)
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-disconnected"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_disconnect":
        # cross-module: an edge was broken -> inverse reconnects output->input.
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _oh, _ro = _b.find_node_output_by_name(_ms_node(entry.get("from_node_id")), entry.get("from_output_name"))
                _ih, _ri = _b.find_node_input_by_name(_ms_node(entry.get("to_node_id")), entry.get("to_input_name"))
                _b.connect_nodes(_oh, _ih)
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-reconnected"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_set_node_input_default":
        # cross-module: a node input literal was set -> restore prior (or clear if none).
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _ih, _ri = _b.find_node_input_by_name(_ms_node(entry.get("node_id")), entry.get("input_name"))
                if entry.get("had_prior"):
                    _b.set_node_input_default(_ih, _ms_lit(entry.get("prior_literal")))
                else:
                    _b.remove_node_input_default(_ih)
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-input-default-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_add_graph_input":
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _b.remove_graph_input(entry.get("input_name")); _ms_save(_ap)
                undone.append({**entry, "result": "metasound-graph-input-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_add_graph_output":
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _b.remove_graph_output(entry.get("output_name")); _ms_save(_ap)
                undone.append({**entry, "result": "metasound-graph-output-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_add_variable":
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _b.remove_graph_variable(entry.get("variable_name")); _ms_save(_ap)
                undone.append({**entry, "result": "metasound-variable-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_set_graph_input_default":
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                if entry.get("had_prior"):
                    _b.set_graph_input_default(entry.get("input_name"), _ms_lit(entry.get("prior_literal")))
                else:
                    _b.reset_graph_input_defaults(entry.get("input_name"))
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-graph-input-default-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_remove_graph_member":
        # cross-module: a graph member was removed -> re-add best-effort (LOSSY: connections not restored).
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                _k = entry.get("kind"); _nm = entry.get("name"); _dt = entry.get("data_type")
                _lit = _ms_lit(entry.get("default_literal"))
                if _k == "input" and _dt:
                    _b.add_graph_input_node(_nm, _dt, _lit, False)
                elif _k == "output" and _dt:
                    _b.add_graph_output_node(_nm, _dt, _lit, False)
                elif _k == "variable" and _dt:
                    _b.add_graph_variable(_nm, _dt, _lit)
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-member-readded", "lossy": True})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "metasound_set_interface":
        try:
            _ap = entry.get("asset_path"); _b = _ms_fobb(_ap)
            if _b is None:
                undone.append({**entry, "result": "metasound-builder-absent"})
            else:
                if entry.get("added"):
                    _b.remove_interface(entry.get("interface_name"))
                else:
                    _b.add_interface(entry.get("interface_name"))
                _ms_save(_ap)
                undone.append({**entry, "result": "metasound-interface-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "audio_set_sound_class_parent":
        # cross-module (audio_reparent.py): a SoundClass was reparented -> restore its prior parent (child_classes
        # on the parents + the child's parent_class pointer).
        try:
            _child = unreal.EditorAssetLibrary.load_asset(entry.get("child_path"))
            if _child is None:
                undone.append({**entry, "result": "sound-class-not-found"})
            else:
                _pp = entry.get("prior_parent_path")
                _prior = unreal.EditorAssetLibrary.load_asset(_pp) if _pp else None
                _cur = None
                try:
                    _cur = _child.get_editor_property("parent_class")
                except Exception:
                    _cur = None
                _cpath = _child.get_path_name()
                _touched = [_child]
                if _cur is not None and (_prior is None or _cur.get_path_name() != _prior.get_path_name()):
                    try:
                        _ok = [c for c in (_cur.get_editor_property("child_classes") or [])
                               if c is not None and c.get_path_name() != _cpath]
                        _cur.set_editor_property("child_classes", _ok); _touched.append(_cur)
                    except Exception:
                        pass
                if _prior is not None:
                    try:
                        _nk = [c for c in (_prior.get_editor_property("child_classes") or [])
                               if c is not None and c.get_path_name() != _cpath]
                        _nk.append(_child)
                        _prior.set_editor_property("child_classes", _nk); _touched.append(_prior)
                    except Exception:
                        pass
                try:
                    _child.set_editor_property("parent_class", _prior)
                except Exception:
                    pass
                for _o in _touched:
                    try:
                        unreal.EditorAssetLibrary.save_asset(_o.get_path_name(), only_if_is_dirty=False)
                    except Exception:
                        pass
                undone.append({**entry, "result": "sound-class-parent-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "audio_set_effect_chain":
        # cross-module (audio_reparent.py): a submix effect chain was set -> restore the prior chain.
        try:
            _sm = unreal.EditorAssetLibrary.load_asset(entry.get("submix_path"))
            if _sm is None:
                undone.append({**entry, "result": "submix-not-found"})
            else:
                _arr = unreal.Array(unreal.SoundEffectSubmixPreset)
                for _p in (entry.get("prior_chain_paths") or []):
                    try:
                        _o = unreal.EditorAssetLibrary.load_asset(_p)
                        if _o is not None:
                            _arr.append(_o)
                    except Exception:
                        pass
                _sm.set_editor_property("submix_effect_chain", _arr)
                try:
                    unreal.EditorAssetLibrary.save_asset(_sm.get_path_name(), only_if_is_dirty=False)
                except Exception:
                    pass
                undone.append({**entry, "result": "effect-chain-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "audio_set_submix_parent":
        # cross-module (audio_cpp.py, C++ #47): re-run the SAME C++ submix-parent setter with the captured
        # prior parent path (empty -> detach to root). The C++ handler writes the EditConst parent_submix/
        # child_submixes that Python cannot. FAITHFUL.
        _mrl = getattr(unreal, "MCPReflectionLibrary", None)
        _sp = entry.get("submix_path")
        if not _sp or _mrl is None or not hasattr(_mrl, "set_submix_parent_json"):
            undone.append({**entry, "result": "submix-or-handler-absent"})
        else:
            try:
                _res = _mrl.set_submix_parent_json(_sp, entry.get("prior_parent_path") or "")
                try:
                    _rj = json.loads(_res) if isinstance(_res, str) else {}
                    for _tp in (_rj.get("touched_paths") or []):
                        unreal.EditorAssetLibrary.save_asset(_tp, only_if_is_dirty=False)
                except Exception:
                    pass
                undone.append({**entry, "result": "submix-parent-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_add_node":
        # cross-module (pcg_write.py): a node was added -> inverse removes it.
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                _n = _pcg_resolve_node(_g, entry.get("node_name"))
                if _n is not None:
                    _g.remove_node(_n)
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-node-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_delete_node":
        # cross-module (pcg_write.py): a node was deleted -> re-add it best-effort (LOSSY: new internal name).
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                _cls = _pcg_resolve_settings_class(entry.get("settings_class"))
                _nn = None; _ss = None
                if _cls is not None:
                    _r = _g.add_node_of_type(_cls)
                    if isinstance(_r, (list, tuple)):
                        for _x in _r:
                            if isinstance(_x, unreal.PCGNode):
                                _nn = _x
                            elif isinstance(_x, unreal.PCGSettings):
                                _ss = _x
                    elif isinstance(_r, unreal.PCGNode):
                        _nn = _r
                if _nn is not None:
                    _pos = entry.get("position")
                    if _pos and len(_pos) >= 2:
                        try:
                            _nn.set_node_position(float(_pos[0]), float(_pos[1]))
                        except Exception:
                            pass
                    if _ss is None:
                        _ss = _nn.get_settings()
                    for _pk, _pv in (entry.get("props") or {}).items():
                        try:
                            _ss.set_editor_property(_pk, _pcg_coerce(_ss.get_editor_property(_pk), _pv))
                        except Exception:
                            pass
                    _oldname = entry.get("node_name")
                    for _e in (entry.get("edges") or []):
                        try:
                            _fn = _e.get("from_node"); _tn = _e.get("to_node")
                            _fnode = _nn if _fn == _oldname else _pcg_resolve_node(_g, _fn)
                            _tnode = _nn if _tn == _oldname else _pcg_resolve_node(_g, _tn)
                            if _fnode is not None and _tnode is not None:
                                _g.add_edge(_fnode, _e.get("from_pin"), _tnode, _e.get("to_pin"))
                        except Exception:
                            pass
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-node-readded", "lossy": True})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_node_property":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
            _s = _n.get_settings() if _n is not None else None
            if _s is None:
                undone.append({**entry, "result": "pcg-node-absent"})
            else:
                if entry.get("had_prior"):
                    _prop = entry.get("prop")
                    _s.set_editor_property(_prop, _pcg_coerce(_s.get_editor_property(_prop), entry.get("prior")))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-node-property-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_node_position":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
            if _n is None:
                undone.append({**entry, "result": "pcg-node-absent"})
            else:
                _n.set_node_position(float(entry.get("prior_x", 0.0)), float(entry.get("prior_y", 0.0)))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-node-position-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_connect":
        # a connection was made -> inverse removes the edge.
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                _fn = _pcg_resolve_node(_g, entry.get("from_node"))
                _tn = _pcg_resolve_node(_g, entry.get("to_node"))
                if _fn is not None and _tn is not None:
                    _g.remove_edge(_fn, entry.get("from_pin"), _tn, entry.get("to_pin"))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-edge-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_disconnect":
        # an edge was removed -> inverse re-adds it.
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                _fn = _pcg_resolve_node(_g, entry.get("from_node"))
                _tn = _pcg_resolve_node(_g, entry.get("to_node"))
                if _fn is not None and _tn is not None:
                    _g.add_edge(_fn, entry.get("from_pin"), _tn, entry.get("to_pin"))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-edge-readded"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_graph_property":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                if entry.get("had_prior"):
                    _prop = entry.get("prop")
                    _g.set_editor_property(_prop, _pcg_coerce(_g.get_editor_property(_prop), entry.get("prior")))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-graph-property-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_layout":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                for _nm, _pos in (entry.get("prior_positions") or {}).items():
                    try:
                        _n = _pcg_resolve_node(_g, _nm)
                        if _n is not None and _pos and len(_pos) >= 2:
                            _n.set_node_position(float(_pos[0]), float(_pos[1]))
                    except Exception:
                        pass
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-layout-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_build":
        # a whole graph was built -> inverse removes the created nodes (drops incident edges).
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            if _g is None:
                undone.append({**entry, "result": "pcg-graph-absent"})
            else:
                for _nm in (entry.get("node_names") or []):
                    try:
                        _n = _pcg_resolve_node(_g, _nm)
                        if _n is not None:
                            _g.remove_node(_n)
                    except Exception:
                        pass
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-build-reverted"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_subgraph":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
            _s = _n.get_settings() if _n is not None else None
            if _s is None or not hasattr(_s, "set_subgraph"):
                undone.append({**entry, "result": "pcg-subgraph-node-absent"})
            else:
                _pp = entry.get("prior_subgraph")
                _tgt = unreal.EditorAssetLibrary.load_asset(_pp) if _pp else None
                _s.set_subgraph(_tgt)
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-subgraph-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_graph_param":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            _H = getattr(unreal, "PCGGraphParametersHelpers", None)
            if _g is None or _H is None:
                undone.append({**entry, "result": "pcg-graph-or-helper-absent"})
            else:
                if entry.get("had_prior"):
                    _setter = getattr(_H, entry.get("set_method"), None)
                    if _setter is not None:
                        _setter(_g, entry.get("param_name"), entry.get("prior"))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-graph-param-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_compute_source":
        try:
            _a = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
            if _a is None:
                undone.append({**entry, "result": "pcg-compute-source-absent"})
            else:
                if entry.get("had_prior"):
                    _a.set_editor_property("source", entry.get("prior_source") or "")
                _pcg_save(_a)
                undone.append({**entry, "result": "pcg-compute-source-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_add_compute_source_additional":
        try:
            _a = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
            if _a is None:
                undone.append({**entry, "result": "pcg-compute-source-absent"})
            else:
                _rp = entry.get("ref_path")
                _keep = []
                for _e in list(_a.get_editor_property("additional_sources") or []):
                    _drop = False
                    try:
                        if isinstance(_e, unreal.Object) and _e is not None and _e.get_path_name().split(".")[0] == _rp:
                            _drop = True
                    except Exception:
                        _drop = False
                    if not _drop:
                        _keep.append(_e)
                _a.set_editor_property("additional_sources", _keep)
                _pcg_save(_a)
                undone.append({**entry, "result": "pcg-compute-additional-removed"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_remove_compute_source_additional":
        try:
            _a = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
            _ref = unreal.EditorAssetLibrary.load_asset(entry.get("ref_path")) if entry.get("ref_path") else None
            if _a is None or _ref is None:
                undone.append({**entry, "result": "pcg-compute-source-or-ref-absent"})
            else:
                _arr = list(_a.get_editor_property("additional_sources") or [])
                _idx = entry.get("index")
                if _idx is None or _idx < 0 or _idx > len(_arr):
                    _arr.append(_ref)
                else:
                    _arr.insert(_idx, _ref)
                _a.set_editor_property("additional_sources", _arr)
                _pcg_save(_a)
                undone.append({**entry, "result": "pcg-compute-additional-reinserted"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_hlsl_kernel_type":
        try:
            _g = _pcg_load_graph(entry.get("graph_path"))
            _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
            if _g is None or _n is None:
                undone.append({**entry, "result": "pcg-graph-or-node-absent"})
            else:
                _s = _n.get_settings()
                if _s is not None and entry.get("had_prior"):
                    _cur = _s.get_editor_property("kernel_type")
                    _s.set_editor_property("kernel_type", _pcg_coerce(_cur, entry.get("prior")))
                _pcg_save(_g)
                undone.append({**entry, "result": "pcg-hlsl-kernel-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_subgraph_override":
        if (entry.get("via") or "").startswith("param:"):
            # DISABLED (2026-08-19): programmatically restoring a bag-backed subgraph USER-PARAMETER
            # override crashes the editor -- a Python-interpreter stack overflow through the PCG param
            # marshalling path (EXCEPTION_ACCESS_VIOLATION in python311.dll, reproduced twice, both
            # via reset_editor_property AND the typed PCGGraphParametersHelpers setter). The param-leg
            # override undo is a documented NO-OP: the forward best-effort override stays applied.
            # (set/reset_pcg_subgraph_override remain PARTIAL.) The prop-leg inverse below is verified safe.
            undone.append({**entry, "result": "pcg-subgraph-override-param-noop"})
        else:
            try:
                _g = _pcg_load_graph(entry.get("graph_path"))
                _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
                _inst = None
                if _n is not None:
                    _sset = _n.get_settings()
                    if _sset is not None:
                        try:
                            _inst = _sset.get_editor_property("subgraph_instance")
                        except Exception:
                            _inst = None
                if _inst is None:
                    undone.append({**entry, "result": "pcg-subgraph-instance-absent"})
                else:
                    _prop = entry.get("property")
                    _po = entry.get("prior_overridden") or ""
                    _was = ("OVERRIDDEN" in _po) or (_po == "True")
                    if not _was:
                        try:
                            _inst.reset_editor_property(_prop)
                        except Exception:
                            pass
                    elif entry.get("had_prior"):
                        try:
                            _cur = _inst.get_editor_property(_prop)
                            _inst.set_editor_property(_prop, _pcg_coerce(_cur, entry.get("prior_value")))
                        except Exception:
                            pass
                    _pcg_save(_g)
                    undone.append({**entry, "result": "pcg-subgraph-override-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_reset_subgraph_override":
        if (entry.get("via") or "").startswith("param:"):
            # DISABLED (2026-08-19): see pcg_set_subgraph_override above -- programmatically re-applying a
            # bag-backed subgraph user-parameter value crashes the editor (python311 stack overflow).
            # Param-leg reset undo is a documented NO-OP. The prop-leg inverse below is verified safe.
            undone.append({**entry, "result": "pcg-subgraph-reset-param-noop"})
        else:
            try:
                _g = _pcg_load_graph(entry.get("graph_path"))
                _n = _pcg_resolve_node(_g, entry.get("node_name")) if _g is not None else None
                _inst = None
                if _n is not None:
                    _sset = _n.get_settings()
                    if _sset is not None:
                        try:
                            _inst = _sset.get_editor_property("subgraph_instance")
                        except Exception:
                            _inst = None
                if _inst is None:
                    undone.append({**entry, "result": "pcg-subgraph-instance-absent"})
                else:
                    _prop = entry.get("property")
                    _po = entry.get("prior_overridden") or ""
                    _was = ("OVERRIDDEN" in _po) or (_po == "True")
                    if _was and entry.get("had_prior"):
                        try:
                            _cur = _inst.get_editor_property(_prop)
                            _inst.set_editor_property(_prop, _pcg_coerce(_cur, entry.get("prior_value")))
                        except Exception:
                            pass
                    else:
                        try:
                            _inst.reset_editor_property(_prop)
                        except Exception:
                            pass
                    _pcg_save(_g)
                    undone.append({**entry, "result": "pcg-subgraph-reset-restored"})
            except Exception as e:
                undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_set_component_graph":
        try:
            _eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            _all = list(_eas.get_all_level_actors()) if _eas is not None else []
            _ap = entry.get("actor_path"); _an = entry.get("actor_name"); _al = entry.get("actor_label")
            _act = None
            for _a in _all:
                try:
                    if _ap and _a.get_path_name() == _ap:
                        _act = _a; break
                except Exception:
                    pass
            if _act is None:
                for _a in _all:
                    try:
                        if _an and _a.get_name() == _an:
                            _act = _a; break
                        if _al and _a.get_actor_label() == _al:
                            _act = _a; break
                    except Exception:
                        pass
            _comp = None
            if _act is not None:
                try:
                    _cs = _act.get_components_by_class(unreal.PCGComponent)
                    if _cs and len(list(_cs)) > 0:
                        _comp = list(_cs)[0]
                except Exception:
                    _comp = None
                if _comp is None:
                    try:
                        _cc = _act.get_editor_property("pcg_component")
                        if isinstance(_cc, unreal.PCGComponent):
                            _comp = _cc
                    except Exception:
                        _comp = None
            if _comp is None:
                undone.append({**entry, "result": "pcg-component-absent; no-op"})
            else:
                _pg = entry.get("prior_graph")
                _obj = unreal.EditorAssetLibrary.load_asset(_pg) if _pg else None
                _comp.set_graph(_obj)
                undone.append({**entry, "result": "pcg-component-graph-restored"})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_remove_graph_parameter":
        try:
            _fn = getattr(unreal.MCPReflectionLibrary, "remove_pcg_graph_parameter_json", None)
            if _fn is None:
                undone.append({**entry, "result": "pcg-schema-handler-absent"})
            else:
                _r = _fn(entry.get("graph_path"), entry.get("name"))
                _g = _pcg_load_graph(entry.get("graph_path"))
                if _g is not None:
                    _pcg_save(_g)
                undone.append({**entry, "result": "pcg-graph-param-add-undone", "handler": str(_r)[:100]})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_add_graph_parameter":
        try:
            _fn = getattr(unreal.MCPReflectionLibrary, "add_pcg_graph_parameter_json", None)
            if _fn is None:
                undone.append({**entry, "result": "pcg-schema-handler-absent"})
            else:
                _ty = entry.get("type")
                _vto = entry.get("value_type_object") or ""
                if _ty == "struct":
                    _sm = {"Vector2D": "vector2d", "Vector": "vector", "Rotator": "rotator", "Transform": "transform", "Quat": "quat", "LinearColor": "linearcolor"}
                    _ty = _sm.get(_vto.split(".")[-1], "vector")
                _r = _fn(entry.get("graph_path"), entry.get("name"), _ty)
                _g = _pcg_load_graph(entry.get("graph_path"))
                if _g is not None:
                    _pcg_save(_g)
                undone.append({**entry, "result": "pcg-graph-param-remove-undone", "handler": str(_r)[:100]})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_rename_graph_parameter":
        try:
            _fn = getattr(unreal.MCPReflectionLibrary, "rename_pcg_graph_parameter_json", None)
            if _fn is None:
                undone.append({**entry, "result": "pcg-schema-handler-absent"})
            else:
                _r = _fn(entry.get("graph_path"), entry.get("old_name"), entry.get("new_name"))
                _g = _pcg_load_graph(entry.get("graph_path"))
                if _g is not None:
                    _pcg_save(_g)
                undone.append({**entry, "result": "pcg-graph-param-rename-undone", "handler": str(_r)[:100]})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_remove_dynamic_input_pin":
        try:
            _fn = getattr(unreal.MCPReflectionLibrary, "remove_pcg_dynamic_input_pin_json", None)
            if _fn is None:
                undone.append({**entry, "result": "pcg-schema-handler-absent"})
            else:
                _pi = entry.get("pin_index")
                _pi = int(_pi) if _pi is not None else -1
                _r = _fn(entry.get("graph_path"), entry.get("node_name"), _pi)
                _g = _pcg_load_graph(entry.get("graph_path"))
                if _g is not None:
                    _pcg_save(_g)
                undone.append({**entry, "result": "pcg-dynamic-pin-add-undone", "handler": str(_r)[:100]})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    elif op == "pcg_add_dynamic_input_pin":
        try:
            _fn = getattr(unreal.MCPReflectionLibrary, "add_pcg_dynamic_input_pin_json", None)
            if _fn is None:
                undone.append({**entry, "result": "pcg-schema-handler-absent"})
            else:
                _r = _fn(entry.get("graph_path"), entry.get("node_name"))
                _g = _pcg_load_graph(entry.get("graph_path"))
                if _g is not None:
                    _pcg_save(_g)
                undone.append({**entry, "result": "pcg-dynamic-pin-remove-undone", "handler": str(_r)[:100]})
        except Exception as e:
            undone.append({**entry, "result": "restore-failed", "err": str(e)[:120]})
    else:
        led.append(entry)
        undone.append({"op": op, "result": "no-inverse-known; stopped"})
        break
    try:
        _rroot = getattr(builtins, "_UMCP_REDO", None)
        if _rroot is None:
            _rroot = {}
            builtins._UMCP_REDO = _rroot
        _rsid = PARAMS.get("_session", "default")
        _rroot.setdefault(_rsid, []).append(entry)
    except Exception:
        pass
print("@@UMCP@@" + json.dumps({"status": "success", "undone": undone, "ledger_depth": len(led)}))
'''

    _CREATE_ASSET_SWEEP_BODY = '''
import unreal, json
targets = PARAMS.get("targets") or []
out = []
for t in targets:
    ap = t.get("asset_path"); pkg = t.get("package_path"); cd = t.get("created_dir")
    unreal.SystemLibrary.collect_garbage()
    ok = False
    if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
        ok = unreal.EditorAssetLibrary.delete_asset(ap)
        if not ok and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
            try:
                unreal.EditorAssetLibrary.delete_directory(pkg)
                ok = not unreal.EditorAssetLibrary.does_asset_exist(ap)
            except Exception:
                pass
    elif ap:
        ok = True
    if ok and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
        try:
            if not (unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []):
                unreal.EditorAssetLibrary.delete_directory(pkg)
        except Exception:
            pass
    out.append({"asset_path": ap, "swept": ok})
print("@@UMCP@@" + json.dumps({"status": "success", "swept": out}))
'''

    @mcp.tool()
    def undo(ctx, count: int = 1) -> str:
        """Revert the most recent edits WE made, newest first (agent-scoped).

        Pops up to `count` entries off the agent ledger and applies their inverse
        (e.g. a spawn_actor is undone by deleting that exact actor). Only our own
        recorded edits are touched; your manual editor changes are never affected.
        Stops early if it hits an op with no known inverse."""
        try:
            undone = []
            while len(undone) < count:
                peek = _lean_exec(_ST_PEEK, {})
                entry = peek.get("entry") if isinstance(peek, dict) else None
                if entry is None:
                    break
                op = entry.get("op", "")
                # --- StateTree ops: apply the inverse via a LEAN snippet (avoids crash #2 in _UNDO_BODY) ---
                if op == "st_set_component_tree":
                    r = _lean_exec(_ST_UNDO_COMPTREE, {"entry": entry})
                    undone.append({**entry, "result": (r.get("result") if isinstance(r, dict) else None)})
                    continue
                if isinstance(op, str) and op.startswith("st_"):
                    calls, token = _st_inverse(entry)
                    if calls is not None:
                        r = _lean_exec(_ST_UNDO_APPLY, {"asset_path": entry.get("asset_path"),
                            "_op": op, "_calls": calls, "_token": token})
                        undone.append({**entry, "result": (r.get("result") if isinstance(r, dict) else token)})
                        continue
                    # unknown st_* op -> fall through to the legacy body below
                # --- PCG schema/pin ops: apply the inverse via a LEAN snippet (avoids the python311 C-stack
                #     overflow that _UNDO_BODY + the graph-changed broadcast triggers) ---
                if isinstance(op, str) and op in ("pcg_remove_graph_parameter", "pcg_add_graph_parameter",
                        "pcg_rename_graph_parameter", "pcg_remove_dynamic_input_pin", "pcg_add_dynamic_input_pin"):
                    _pgp, _pcalls, _ptok = _pcg_schema_inverse(entry)
                    if _pcalls is not None:
                        r = _lean_exec(_PCG_SCHEMA_UNDO, {"graph_path": _pgp, "_calls": _pcalls,
                            "_token": _ptok, "_entry": entry})
                        undone.append({**entry, "result": (r.get("result") if isinstance(r, dict) else _ptok)})
                        continue
                # --- non-StateTree op: undo exactly ONE via the legacy _UNDO_BODY, then create_asset sweep ---
                rb = _exec(_UNDO_BODY, {"count": 1})
                ub = rb.get("undone") if isinstance(rb, dict) else None
                if not ub:
                    break
                failed = [{"asset_path": u.get("asset_path"), "package_path": u.get("package_path"),
                           "created_dir": u.get("created_dir")}
                          for u in ub if u.get("op") == "create_asset" and u.get("result") == "delete-failed"]
                if failed:
                    sweep = _exec(_CREATE_ASSET_SWEEP_BODY, {"targets": failed})
                    ok_paths = {s.get("asset_path") for s in (sweep.get("swept") or []) if s.get("swept")}
                    for u in ub:
                        if (u.get("op") == "create_asset" and u.get("result") == "delete-failed"
                                and u.get("asset_path") in ok_paths):
                            u["result"] = "asset-deleted (post-sweep)"
                undone.extend(ub)
            return json.dumps({"undone": undone, "count": len(undone)}, indent=2)
        except Exception as e:
            return f"Error: {e}"
