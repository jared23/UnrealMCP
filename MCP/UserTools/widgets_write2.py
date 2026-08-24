"""UserTools :: Widgets / UMG (WRITE, extension #2)  (spec: docs/spec/widgets.md)

Extends the widget-authoring surface (widgets_write.py owns create_widget_blueprint / add_widget /
remove_widget). This "2" module (bt_write2 / anim_write2 / curves_write_ext convention) adds the
MISSING widget-TREE EDIT writes that operate on widgets ALREADY in a WidgetBlueprint's tree and are
cleanly reachable from stock UE 5.8 Python (NO C++ handler needed) with a FAITHFUL, reversible inverse:

  - set_widget_property       set a reflected UPROPERTY on a named widget (text/tooltip/opacity/
                              is_enabled/visibility/color... ). Prior captured -> undo restores it.
  - set_widget_slot_property  set a layout/slot property on a widget's panel slot (CanvasPanelSlot
                              position/size/offsets/anchors/alignment/z_order/auto_size; box-slot
                              padding/size/horizontal_alignment/vertical_alignment...). Prior captured.
  - reparent_widget           move a widget under a different parent PANEL widget (move_widget). Prior
                              parent captured -> undo reparents it back.

REACHABILITY (probed live vs TestMCPSetup, UE 5.8.1 -- how these avoid the "editor-only" wall that
blocked the tree-CONSTRUCT ops in widgets_write.py):
  * A widget already in the tree is a named sub-object outered to the WidgetTree:
      wt = unreal.find_object(WidgetBlueprint, "WidgetTree"); w = unreal.find_object(wt, "<Name>").
    Editing an EXISTING widget's UPROPERTYs via set_editor_property WORKS and persists through
    BlueprintEditorLibrary.compile_blueprint + save (no construct_widget / protected RootWidget needed).
  * Slot layout values split by slot family:
      - CanvasPanelSlot: position/size/offsets/anchors/alignment are NOT plain UPROPERTYs (they live in
        an FAnchorData struct) -> reached via the slot GETTER/SETTER METHODS get_/set_position, _size,
        _offsets, _anchors, _alignment (+ z_order/auto_size which are also editor props).
      - Box/overlay/etc. slots (VerticalBoxSlot, HorizontalBoxSlot, ...): padding / size (SlateChildSize)
        / horizontal_alignment / vertical_alignment are direct UPROPERTYs (SETTER methods exist but NO
        getter methods) -> read via get_editor_property, write via the setter method or editor property.
    So slot access uses a DUAL path (method getter/setter preferred, editor-property fallback) that
    covers both families and symmetrically captures the prior value for undo.
  * Reparent: UPanelWidget.remove_child(w) + newParent.add_child(w) both work on tree-registered
    widgets (the widget stays in WidgetTree.AllWidgets; the variable-name->guid map is untouched since
    the name does not change) -> compile + save persists it.

Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM
from statetree_write.py / editor_level.py. Each tool is ONE mutate-op per call, ledgered with the info
its inverse needs. This module does NOT define its own `undo` tool (editor_level.py owns the single
unified `undo`); the ledger op schemas + inverses below are for the coordinator to fold into it.

Ledger ops introduced (coordinator folds these into editor_level.undo):
  {op: "widget_set_prop", wbp_path, widget_name, property, prior(serialized), restorable}
     inverse: load WBP; w = find_object(find_object(wb,"WidgetTree"), widget_name);
              w.set_editor_property(property, _wdes(w.get_editor_property(property), prior));
              compile_blueprint(wb) + save_asset(wbp_path).
  {op: "widget_set_slot_prop", wbp_path, widget_name, property, prior(serialized), via, restorable}
     inverse: load WBP; w = find widget; slot = w.slot;
              _slot_set(slot, property, _wdes(_slot_get(slot,property)[0], prior)); compile + save.
  {op: "widget_reparent", wbp_path, widget_name, prior_parent_name}
     inverse: load WBP; w = find widget; cur = w.get_parent(); cur.remove_child(w);
              prior = find widget prior_parent_name; prior.add_child(w); compile + save.
  (_wdes / _slot_get / _slot_set are the helpers in _WW2_HELPERS below; the coordinator can reuse them.)

DEFERRED (not shipped; no faithful stock-Python reversible path in this build):
  rename_widget (variable-name->guid map resync is editor/C++ only), duplicate_widget / replace_widget /
  wrap_widget / set_root_widget-on-nonempty-tree (need UWidgetTree construct/root C++), widget animations
  and navigation and named slots (protected UPROPERTYs / editor-only). Reported honestly, not faked.
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

# NOTE: execute_python wraps incoming code in triple-SINGLE-quotes before exec -> snippet bodies must
# contain NO ''' and NO stray backslashes; all data passes as base64. Never assign a local named
# sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/code_obj.


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
    _WW2_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_wb(path):
    obj = unreal.EditorAssetLibrary.load_asset(path) if path else None
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.WidgetBlueprint):
        return None, "not a WidgetBlueprint (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _find_widget(wb, name):
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
def _compile_save(wb, path):
    okc = True; oks = True
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(wb)
    except Exception:
        okc = False
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        oks = False
    return okc, oks
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _wser(v):
    # (json_form, restorable). Marker dicts are self-describing so _wdes can rebuild without a hint.
    if v is None:
        return (None, True)
    if isinstance(v, bool):
        return (v, True)
    if isinstance(v, (int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector2D):
        return ({"__v2__": [v.x, v.y]}, True)
    if isinstance(v, unreal.Vector):
        return ({"__v3__": [v.x, v.y, v.z]}, True)
    if isinstance(v, unreal.Margin):
        return ({"__margin__": [v.left, v.top, v.right, v.bottom]}, True)
    if isinstance(v, unreal.Anchors):
        return ({"__anchors__": [v.minimum.x, v.minimum.y, v.maximum.x, v.maximum.y]}, True)
    if isinstance(v, unreal.SlateChildSize):
        try:
            return ({"__childsize__": [float(v.get_editor_property("value")), _enum_name(v.get_editor_property("size_rule"))]}, True)
        except Exception:
            return (None, False)
    if isinstance(v, unreal.LinearColor):
        return ({"__lincolor__": [v.r, v.g, v.b, v.a]}, True)
    if isinstance(v, unreal.Color):
        return ({"__color__": [v.r, v.g, v.b, v.a]}, True)
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
def _v2(a):
    return unreal.Vector2D(float(a[0]), float(a[1]))
def _mk_margin(a):
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
def _mk_anchors(a):
    an = unreal.Anchors()
    if isinstance(a, dict):
        mn = a.get("min") or a.get("minimum") or [0, 0]
        mx = a.get("max") or a.get("maximum") or mn
        an.minimum = _v2(mn); an.maximum = _v2(mx); return an
    if isinstance(a, (list, tuple)):
        if len(a) == 4:
            an.minimum = unreal.Vector2D(float(a[0]), float(a[1])); an.maximum = unreal.Vector2D(float(a[2]), float(a[3])); return an
        if len(a) == 2:
            an.minimum = unreal.Vector2D(float(a[0]), float(a[1])); an.maximum = unreal.Vector2D(float(a[0]), float(a[1])); return an
    return None
def _mk_childsize(val, rule):
    scs = unreal.SlateChildSize()
    try:
        scs.set_editor_property("value", float(val))
    except Exception:
        pass
    if rule is not None:
        r = getattr(unreal.SlateSizeRule, str(rule), None)
        if r is None:
            r = getattr(unreal.SlateSizeRule, str(rule).upper(), None)
        if r is not None:
            try:
                scs.set_editor_property("size_rule", r)
            except Exception:
                pass
    return scs
def _enum_coerce(current, name):
    cls = type(current) if isinstance(current, unreal.EnumBase) else None
    if cls is None:
        return None
    for cand in (str(name), str(name).upper()):
        e = getattr(cls, cand, None)
        if e is not None:
            return e
    return None
def _wdes(current, value):
    # marker dicts first (undo path; self-describing)
    if isinstance(value, dict):
        if "__v2__" in value: return _v2(value["__v2__"])
        if "__v3__" in value:
            a = value["__v3__"]; return unreal.Vector(float(a[0]), float(a[1]), float(a[2]))
        if "__margin__" in value: return _mk_margin(value["__margin__"])
        if "__anchors__" in value: return _mk_anchors(value["__anchors__"])
        if "__childsize__" in value:
            a = value["__childsize__"]; return _mk_childsize(a[0], a[1] if len(a) > 1 else None)
        if "__lincolor__" in value:
            a = value["__lincolor__"]; return unreal.LinearColor(float(a[0]), float(a[1]), float(a[2]), float(a[3]))
        if "__color__" in value:
            a = value["__color__"]; return unreal.Color(r=int(a[0]), g=int(a[1]), b=int(a[2]), a=int(a[3]))
        if "__enum__" in value:
            e = _enum_coerce(current, value["__enum__"]); return e if e is not None else current
        if "__object__" in value:
            p = value["__object__"]; return unreal.EditorAssetLibrary.load_asset(p) if p else None
    # type-hinted from the current value (forward path)
    if isinstance(current, unreal.Vector2D) and isinstance(value, (list, tuple)):
        return _v2(value)
    if isinstance(current, unreal.Margin):
        return _mk_margin(value)
    if isinstance(current, unreal.Anchors):
        return _mk_anchors(value)
    if isinstance(current, unreal.SlateChildSize):
        if isinstance(value, dict):
            return _mk_childsize(value.get("value", 1.0), value.get("rule") or value.get("size_rule"))
        if isinstance(value, (int, float)):
            return _mk_childsize(value, None)
        return current
    if isinstance(current, unreal.LinearColor) and isinstance(value, (list, tuple)):
        aa = float(value[3]) if len(value) > 3 else 1.0
        return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
    if isinstance(current, unreal.Color) and isinstance(value, (list, tuple)):
        aa = int(value[3]) if len(value) > 3 else 255
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=aa)
    if isinstance(current, unreal.EnumBase):
        e = _enum_coerce(current, value); return e if e is not None else current
    if isinstance(current, unreal.Text):
        return str(value)
    if isinstance(current, unreal.Name):
        return unreal.Name(str(value))
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try: return int(value)
        except Exception: return current
    if isinstance(current, float):
        try: return float(value)
        except Exception: return current
    if isinstance(current, unreal.Object):
        if isinstance(value, str):
            return unreal.EditorAssetLibrary.load_asset(value) if value else None
        return value
    if current is None and isinstance(value, str):
        return value
    return value
def _slot_get(slot, prop):
    # prefer a getter METHOD (CanvasPanelSlot layout props); fall back to a UPROPERTY (box slots).
    g = getattr(slot, "get_" + prop, None)
    if g is not None:
        try:
            return g(), "method", None
        except Exception:
            pass
    try:
        return slot.get_editor_property(prop), "editorprop", None
    except Exception:
        return None, None, "slot %s has no readable property '%s'" % (slot.get_class().get_name(), prop)
def _slot_set(slot, prop, val):
    # prefer a setter METHOD (both families expose set_<prop>); fall back to a UPROPERTY.
    errm = "no setter method"
    s = getattr(slot, "set_" + prop, None)
    if s is not None:
        try:
            s(val); return True, "method", None
        except Exception as e:
            errm = str(e)
    try:
        slot.set_editor_property(prop, val); return True, "editorprop", None
    except Exception as e:
        return False, None, "set failed (method: %s; editorprop: %s)" % (errm, str(e))
def _is_descendant(node, maybe_ancestor):
    # True if maybe_ancestor is node itself or an ancestor of node (walk parents up).
    cur = node
    for _i in range(0, 500):
        if cur is None:
            return False
        if cur == maybe_ancestor:
            return True
        try:
            cur = cur.get_parent()
        except Exception:
            cur = None
    return False
'''

    # ------------------------------------------------------------------ #
    # set_widget_property — reflected UPROPERTY set on a named widget      #
    # ------------------------------------------------------------------ #
    _SET_PROP_BODY = _WW2_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
prop = PARAMS["property"]; value = PARAMS.get("value")
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        try:
            prior_raw = w.get_editor_property(prop); have = True
        except Exception as e:
            have = False
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such widget property '%s' (%s)" % (prop, str(e)[:100])}))
        if have:
            prior_json, restorable = _wser(prior_raw)
            if not restorable:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "property '%s' has a non-serializable prior (%s) -> refusing (would be unrevertable)" % (prop, type(prior_raw).__name__)}))
            else:
                try:
                    newv = _wdes(prior_raw, value)
                    w.set_editor_property(prop, newv)
                    after_json, _u = _wser(w.get_editor_property(prop))
                    okc, oks = _compile_save(wb, wbp_path)
                    _ledger().append({"op": "widget_set_prop", "wbp_path": wbp_path,
                        "widget_name": widget_name, "property": prop, "prior": prior_json, "restorable": True})
                    print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                        "widget": widget_name, "widget_class": w.get_class().get_name(), "property": prop,
                        "before": prior_json, "after": after_json, "compiled": okc, "saved": oks,
                        "ledger_depth": len(_ledger())}))
                except Exception as e:
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "set failed: %s" % str(e)[:160]}))
'''

    # ------------------------------------------------------------------ #
    # set_widget_slot_property — layout/slot prop on a widget's panel slot #
    # ------------------------------------------------------------------ #
    _SET_SLOT_BODY = _WW2_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
prop = PARAMS["property"]; value = PARAMS.get("value")
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        try:
            slot = w.slot
        except Exception:
            slot = None
        if slot is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget '%s' has no slot (is it the root / not in a panel?)" % widget_name}))
        else:
            prior_raw, via_get, gerr = _slot_get(slot, prop)
            if gerr:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": gerr}))
            else:
                prior_json, restorable = _wser(prior_raw)
                if not restorable:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "slot property '%s' has a non-serializable prior (%s) -> refusing (unrevertable)" % (prop, type(prior_raw).__name__)}))
                else:
                    newv = _wdes(prior_raw, value)
                    ok, via_set, serr = _slot_set(slot, prop, newv)
                    if not ok:
                        print("@@UMCP@@" + json.dumps({"status": "error", "message": serr}))
                    else:
                        after_raw, _vg, _e = _slot_get(slot, prop)
                        after_json, _u = _wser(after_raw)
                        okc, oks = _compile_save(wb, wbp_path)
                        _ledger().append({"op": "widget_set_slot_prop", "wbp_path": wbp_path,
                            "widget_name": widget_name, "property": prop, "prior": prior_json,
                            "via": via_set, "restorable": True})
                        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                            "widget": widget_name, "slot_class": slot.get_class().get_name(), "property": prop,
                            "before": prior_json, "after": after_json, "via_get": via_get, "via_set": via_set,
                            "compiled": okc, "saved": oks, "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # reparent_widget — move a widget under a different parent panel       #
    # ------------------------------------------------------------------ #
    _REPARENT_BODY = _WW2_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]; new_parent = PARAMS["new_parent_name"]
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    np = _find_widget(wb, new_parent)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    elif np is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new parent not found in tree: %s" % new_parent}))
    elif not isinstance(np, unreal.PanelWidget):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new parent '%s' (%s) is not a PanelWidget" % (new_parent, np.get_class().get_name())}))
    else:
        cur_parent = None
        try:
            cur_parent = w.get_parent()
        except Exception:
            cur_parent = None
        if cur_parent is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot reparent the ROOT widget '%s' (it has no parent)" % widget_name}))
        elif np == w:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot reparent a widget under itself"}))
        elif _is_descendant(np, w):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot reparent '%s' under its own descendant '%s' (would create a cycle)" % (widget_name, new_parent)}))
        else:
            prior_parent_name = cur_parent.get_name()
            if prior_parent_name == new_parent:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget '%s' is already a child of '%s'" % (widget_name, new_parent)}))
            else:
                removed = cur_parent.remove_child(w)
                added_slot = np.add_child(w)
                if added_slot is None:
                    # re-attach to the original parent to avoid orphaning on failure
                    try: cur_parent.add_child(w)
                    except Exception: pass
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_child to '%s' failed (panel may be full / single-child); reverted" % new_parent}))
                else:
                    okc, oks = _compile_save(wb, wbp_path)
                    _ledger().append({"op": "widget_reparent", "wbp_path": wbp_path,
                        "widget_name": widget_name, "prior_parent_name": prior_parent_name})
                    print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                        "widget": widget_name, "prior_parent": prior_parent_name, "new_parent": new_parent,
                        "removed_from_prior": bool(removed), "new_slot_class": added_slot.get_class().get_name(),
                        "compiled": okc, "saved": oks, "ledger_depth": len(_ledger())}))
'''

    # ============================ MCP TOOLS ============================ #

    @mcp.tool()
    def set_widget_property(ctx, wbp_path: str, widget_name: str, property: str, value=None) -> str:
        """Set a reflected UPROPERTY on a widget that ALREADY exists in a WidgetBlueprint's tree
        (pure Python; no C++ handler). Compiles + saves the WidgetBlueprint after.

        wbp_path:    object/package path of the WidgetBlueprint asset.
        widget_name: name of the widget in the tree (e.g. 'MyText', 'MyButton').
        property:    the widget UPROPERTY snake_case name, e.g. 'text' / 'tool_tip_text' (FText -> pass a
                     string), 'is_enabled' (bool), 'render_opacity' (float), 'visibility'
                     (SlateVisibility enum member: VISIBLE|HIDDEN|COLLAPSED|HIT_TEST_INVISIBLE|
                     SELF_HIT_TEST_INVISIBLE), color/vector props (pass a list).
        value:       new value. Coerced from the current value's type: FText<-string, bool<-bool/str,
                     numbers pass through, enum<-member-name string, Vector2D/Margin<-list, color<-[r,g,b,a].

        The prior value is captured (serialized) so undo restores it. If the prior value is an
        unserializable struct (e.g. an FSlateColor/binding) the set is REFUSED rather than shipping an
        unrevertable op. Verify with widgets_read.get_widget_blueprint_info; UMG-compiler warnings surface
        via `_log_warnings`. Ledgered op 'widget_set_prop' {wbp_path, widget_name, property, prior,
        restorable}; inverse (fold into editor_level.undo): re-set the property to the captured prior +
        compile + save."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name, "property": property, "value": value}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_widget_slot_property(ctx, wbp_path: str, widget_name: str, property: str, value=None) -> str:
        """Set a LAYOUT/SLOT property on a widget's panel slot (its position within its parent panel).
        Pure Python (dual method-getter/setter + editor-property path covering CanvasPanelSlot and
        box/overlay slots). Compiles + saves the WidgetBlueprint after.

        wbp_path:    object/package path of the WidgetBlueprint asset.
        widget_name: name of the widget whose slot to edit (must be a child of a panel, not the root).
        property:    slot property. CanvasPanelSlot: 'position' [x,y], 'size' [x,y], 'offsets'
                     [l,t,r,b] or a number, 'anchors' [minx,miny,maxx,maxy] or [x,y], 'alignment' [x,y],
                     'z_order' int, 'auto_size' bool. Box slots (Vertical/HorizontalBoxSlot):
                     'padding' [l,t,r,b] or number, 'size' {value:float, rule:AUTOMATIC|FILL},
                     'horizontal_alignment' (H_ALIGN_FILL|H_ALIGN_LEFT|H_ALIGN_CENTER|H_ALIGN_RIGHT),
                     'vertical_alignment' (V_ALIGN_FILL|V_ALIGN_TOP|V_ALIGN_CENTER|V_ALIGN_BOTTOM).
        value:       new value (see per-property forms above).

        The prior value is captured (serialized: Vector2D/Margin/Anchors/SlateChildSize/enum) so undo
        restores it; unserializable priors are REFUSED. Ledgered op 'widget_set_slot_prop' {wbp_path,
        widget_name, property, prior, via, restorable}; inverse (fold into editor_level.undo): re-apply
        the captured prior to the slot via the same dual getter/setter path + compile + save."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name, "property": property, "value": value}
        try:
            return json.dumps(_exec(_SET_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def reparent_widget(ctx, wbp_path: str, widget_name: str, new_parent_name: str) -> str:
        """Move a widget (move_widget) under a different parent PANEL widget in the same tree. Pure
        Python (UPanelWidget.remove_child + add_child). Compiles + saves the WidgetBlueprint after.

        wbp_path:        object/package path of the WidgetBlueprint asset.
        widget_name:     name of the widget to move (cannot be the tree ROOT).
        new_parent_name: name of the destination PANEL widget (CanvasPanel/VerticalBox/Overlay/...).

        Refused if the target is not a panel, is the widget itself, or is a descendant of the widget
        (cycle), or if the widget is already that panel's child. NOTE: the widget receives a fresh
        default slot of the destination panel's slot type -- its OLD slot layout values are not carried
        over (set them afterward with set_widget_slot_property). Ledgered op 'widget_reparent' {wbp_path,
        widget_name, prior_parent_name}; inverse (fold into editor_level.undo): remove from the current
        parent and add_child back to prior_parent_name + compile + save (best-effort: restores hierarchy
        position, not the pre-move slot layout)."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name, "new_parent_name": new_parent_name}
        try:
            return json.dumps(_exec(_REPARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
