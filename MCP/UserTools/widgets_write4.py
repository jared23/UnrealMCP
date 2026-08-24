"""UserTools :: Widgets / UMG (WRITE + READ, extension #4 -- "Batch W-A")  (spec: docs/spec/widgets.md)

Fourth widget-authoring module. widgets_write.py owns create/add/remove; widgets_write2.py owns
set_widget_property / set_widget_slot_property / reparent_widget; widgets_write3.py owns the READ
getters + duplicate_widget. This "#4" module (Batch W-A) fills the remaining Tier-1 gaps that were
DEFERRED by write2/write3 as "needs C++" but turned out to be reachable from stock UE 5.8 Python once
probed live -- NO new C++, NO opening the UMG editor / no modal:

WRITE (compiled + saved, ledgered with a faithful reversible inverse):
  - set_widget_blueprint_parent  reparent the WidgetBlueprint asset itself to a different UserWidget
                                 subclass (a WidgetBlueprint IS a Blueprint ->
                                 BlueprintEditorLibrary.reparent_blueprint). new_parent validated to be
                                 a UserWidget subclass first; prior parent class path captured.
  - set_widget_navigation        set ONE direction's focus-navigation rule (Escape/Stop/Wrap/Explicit/
                                 Custom/CustomBoundary; Explicit needs a target widget). PROBED LIVE and
                                 CONFIRMED Python-feasible: unreal.new_object(WidgetNavigation) + the
                                 whole-struct set of each FWidgetNavigationData (get the struct copy,
                                 set its 'rule'/'widget_to_focus', set the whole struct back on the nav
                                 object) + assign to the widget's 'navigation' editor-property round-trips
                                 through compile+save+RELOAD. Prior whole-navigation captured.
  - clear_widget_navigation      clear a widget's navigation to None (or reset ONE direction to Escape).
                                 Same captured-prior inverse as set_widget_navigation.
  - add_widget_configured        add a widget (via the shipped C++ #8 add handler) AND apply slot/layout
                                 properties at add-time (the shipped add_widget cannot). Reuses the
                                 existing 'add_widget' ledger inverse (remove the added widget by name);
                                 NO new fold needed for this one.

READ (no ledger, no mutation):
  - list_widget_events           enumerate a widget's BlueprintAssignable multicast-delegate events (the
                                 bindable events like OnClicked). DEFINITIVE classifier probed live: a
                                 metadata property is a bindable event iff its snake_case attribute on the
                                 widget instance isinstance()s unreal.MulticastDelegateBase (this cleanly
                                 excludes the FGet* property-binding '*Delegate' getters and plain struct
                                 UPROPERTYs; category strings vary too much to filter on -- 'Button|Event',
                                 'Events', 'Widget Event', 'TextBox|Event' all seen).

NAVIGATION FEASIBILITY VERDICT: Python-OK (decisive). write2/write3 had DEFERRED set/clear navigation as
"needs C++". Live probing (WA_NavProbe: up=Stop, down=Explicit->Btn2, clear-to-None, rebuild-from-capture)
proved the full write + reversible restore works from stock Python. No C++ handler is needed for widget
navigation. (write3's DEFERRED note for set/clear_widget_navigation is now SUPERSEDED by this module.)

Scaffolding (query convention, base64 PARAMS, @@UMCP@@ marker + Output-Log capture, per-session ledger,
session default) copied VERBATIM from widgets_write3.py. Slot-value coercion (_wser/_wdes/_slot_get/
_slot_set + the marker-dict makers) copied from widgets_write2.py so add_widget_configured's slot values
decode identically. NO triple-quotes / NO backslashes in snippet bodies; all data via base64. ONE
blueprint compile per execute_python call.

Ledger ops introduced (coordinator folds these into editor_level.undo):
  {op: "set_widget_bp_parent", asset_path, prior_parent}
     inverse: bp = load(asset_path); prior_cls = unreal.load_object(None, prior_parent);
              BlueprintEditorLibrary.reparent_blueprint(bp, prior_cls) + compile_blueprint(bp) + save.
     (This is STRUCTURALLY IDENTICAL to the already-folded "reparent_blueprint" op at editor_level.py
      ~L2398 -- which uses keys bp_path/prior_parent_path -- so that block is a copy-paste template;
      the coordinator may alternatively have this tool emit that existing op to avoid a new fold.)
  {op: "set_widget_nav", asset_path, widget_name, prior_nav_json}
     prior_nav_json is None (widget had no UWidgetNavigation) OR a dict {dir: {rule, widget_to_focus}}
        for dir in [up,down,left,right,next,previous] (rule = enum member name e.g. "STOP"/"ESCAPE"/
        "EXPLICIT"; widget_to_focus = target widget name or "").
     inverse: wb = load(asset_path); w = find widget; _nav_apply(w, prior_nav_json) + compile + save,
        where _nav_apply(w, None) sets navigation=None and _nav_apply(w, dict) rebuilds a fresh
        unreal.new_object(WidgetNavigation, outer=w), setting each direction's rule (+ widget_to_focus).
        Both set_widget_navigation and clear_widget_navigation use THIS op (captured whole-nav prior).
  (add_widget_configured reuses the EXISTING already-folded 'add_widget' op {wbp_path, name}: inverse
   remove_widget_from_blueprint(name) -> NO new fold needed.)

DEFERRED / notes (honest, not faked):
  - Navigation rule Custom/CustomBoundary: the rule is set faithfully, but the actual custom-navigation
    DELEGATE function is a K2 graph binding not reachable from Python -> the rule is inert until that
    function is wired in the editor. set_widget_navigation SETS the rule (reversible) and RETURNS a
    note; it does not fake the delegate.
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
# contain NO ''' and NO stray backslashes (a lone backslash is silently eaten in transport). All data
# passes as base64. Never assign a local named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (they are the C++ wrapper's own locals).


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
    #   _ledger / _load_wb / _find_widget / _compile_save / _to_snake / _enum_name : as widgets_write3.
    #   _resolve_parent_class / _is_userwidget_class : as widgets_write.py (reparent target validation).
    #   _wser / _wdes / _slot_get / _slot_set / marker-dict makers : as widgets_write2.py (slot values).
    #   _NAV_DIRS / _nav_rule_enum / _nav_capture / _nav_apply : navigation (probed feasible this batch).
    _WA_HELPERS = r'''
import unreal, json, builtins, fnmatch
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
_NAV_DIRS = ["up", "down", "left", "right", "next", "previous"]
_RULE_MAP = {"ESCAPE": "ESCAPE", "STOP": "STOP", "WRAP": "WRAP", "EXPLICIT": "EXPLICIT",
             "CUSTOM": "CUSTOM", "CUSTOMBOUNDARY": "CUSTOM_BOUNDARY", "CUSTOM_BOUNDARY": "CUSTOM_BOUNDARY"}
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = str(pat).lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _load_wb(path):
    obj = unreal.EditorAssetLibrary.load_asset(path) if path else None
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.WidgetBlueprint):
        return None, "not a WidgetBlueprint (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _widget_tree(wb):
    try:
        return unreal.find_object(wb, "WidgetTree")
    except Exception:
        return None
def _find_widget(wb, name):
    wt = _widget_tree(wb)
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
def _to_snake(pascal):
    s = str(pascal)
    if len(s) >= 2 and s[0] == "b" and s[1].isupper():
        s = s[1:]
    res = []
    n = len(s)
    for i in range(n):
        c = s[i]
        if c.isupper():
            prev = s[i-1] if i > 0 else ""
            nxt = s[i+1] if i+1 < n else ""
            if i > 0 and (prev.islower() or prev.isdigit() or (prev.isupper() and nxt != "" and nxt.islower())):
                res.append("_")
            res.append(c.lower())
        else:
            res.append(c)
    return "".join(res)
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _is_userwidget_class(cls):
    try:
        cdo = unreal.get_default_object(cls)
        return isinstance(cdo, unreal.UserWidget)
    except Exception:
        return None
def _resolve_parent_class(spec):
    if not spec:
        return None, "empty parent_class"
    cand = None
    obj = getattr(unreal, str(spec), None)
    if obj is not None:
        if isinstance(obj, unreal.Class):
            cand = obj
        else:
            sc = getattr(obj, "static_class", None)
            if sc is not None:
                try:
                    cand = obj.static_class()
                except Exception:
                    cand = None
    if cand is None:
        try:
            if unreal.EditorAssetLibrary.does_asset_exist(str(spec)):
                a = unreal.EditorAssetLibrary.load_asset(str(spec))
                if a is not None:
                    g = unreal.BlueprintEditorLibrary.generated_class(a)
                    if isinstance(g, unreal.Class):
                        cand = g
        except Exception:
            cand = None
    if cand is None:
        try:
            c = unreal.load_class(None, str(spec))
            if isinstance(c, unreal.Class):
                cand = c
        except Exception:
            cand = None
    if cand is None:
        return None, "could not resolve parent_class '%s' to a UClass (tried unreal.<name>, load_class, WidgetBlueprint asset path)" % spec
    chk = _is_userwidget_class(cand)
    if chk is False:
        return None, "parent_class '%s' (%s) is not a UserWidget subclass" % (spec, cand.get_name())
    return cand, None
def _wser(v):
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
def _nav_rule_enum(nm):
    key = str(nm).strip().upper().replace(" ", "")
    key = _RULE_MAP.get(key, key)
    return getattr(unreal.UINavigationRule, key, None)
def _nav_capture(w):
    try:
        nav = w.get_editor_property("navigation")
    except Exception:
        return None
    if nav is None:
        return None
    d = {}
    for dr in _NAV_DIRS:
        try:
            data = nav.get_editor_property(dr)
            wtf = str(data.get_editor_property("widget_to_focus"))
            if wtf == "None":
                wtf = ""
            d[dr] = {"rule": _enum_name(data.get_editor_property("rule")), "widget_to_focus": wtf}
        except Exception:
            d[dr] = {"rule": "ESCAPE", "widget_to_focus": ""}
    return d
def _nav_apply(w, d):
    if not d:
        w.set_editor_property("navigation", None)
        return True
    nav = unreal.new_object(unreal.WidgetNavigation, outer=w)
    for dr in _NAV_DIRS:
        spec = d.get(dr)
        if not spec:
            continue
        data = nav.get_editor_property(dr)
        r = _nav_rule_enum(spec.get("rule") or "Escape")
        if r is not None:
            data.set_editor_property("rule", r)
        wtf = spec.get("widget_to_focus") or ""
        if wtf:
            data.set_editor_property("widget_to_focus", wtf)
        nav.set_editor_property(dr, data)
    w.set_editor_property("navigation", nav)
    return True
'''

    # ================================================================== #
    # WRITE: set_widget_blueprint_parent                                 #
    # ================================================================== #
    _SET_BP_PARENT_BODY = _WA_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; new_parent_spec = PARAMS["new_parent_class"]
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    new_cls, cerr = _resolve_parent_class(new_parent_spec)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        prior = None
        try:
            prior = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(wb)
        except Exception:
            prior = None
        prior_path = prior.get_path_name() if prior is not None else None
        prior_name = prior.get_name() if prior is not None else None
        gen = None
        try:
            gen = unreal.BlueprintEditorLibrary.generated_class(wb)
        except Exception:
            gen = None
        new_path = new_cls.get_path_name()
        if gen is not None and gen.get_path_name() == new_path:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "refusing to reparent '%s' to its OWN generated class (circular)" % wb.get_name()}))
        elif prior_path is not None and prior_path == new_path:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget blueprint parent is already '%s' (no change)" % new_cls.get_name()}))
        else:
            try:
                unreal.BlueprintEditorLibrary.reparent_blueprint(wb, new_cls)
                okc, oks = _compile_save(wb, wbp_path)
                after = None
                try:
                    after = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(wb)
                except Exception:
                    after = None
                _ledger().append({"op": "set_widget_bp_parent", "asset_path": wbp_path, "prior_parent": prior_path})
                print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                    "asset_path": wbp_path, "prior_parent": prior_name, "prior_parent_path": prior_path,
                    "new_parent": new_cls.get_name(), "new_parent_path": new_path,
                    "after_parent": after.get_name() if after is not None else None,
                    "compiled": okc, "saved": oks, "ledger_depth": len(_ledger())}))
            except Exception as e:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "reparent failed: %s" % str(e)[:180]}))
'''

    # ================================================================== #
    # READ: list_widget_events                                           #
    # ================================================================== #
    _LIST_EVENTS_BODY = _WA_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
name_filter = PARAMS.get("filter") or ""
include_inherited = PARAMS.get("include_inherited")
include_inherited = True if include_inherited is None else bool(include_inherited)
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        is_mc_base = hasattr(unreal, "MulticastDelegateBase")
        meta = {"properties": []}
        if _MRL is not None and hasattr(_MRL, "get_object_property_metadata_json"):
            try:
                meta = json.loads(_MRL.get_object_property_metadata_json(w, include_inherited))
            except Exception:
                meta = {"properties": []}
        seen = set()
        events = []
        for p in meta.get("properties", []) or []:
            cpp_name = p.get("name")
            if not cpp_name or not _match(cpp_name, name_filter):
                continue
            snake = _to_snake(cpp_name)
            if snake in seen:
                continue
            o = getattr(w, snake, None)
            is_event = bool(is_mc_base) and (o is not None) and isinstance(o, unreal.MulticastDelegateBase)
            if not is_event:
                continue
            seen.add(snake)
            bound = None
            try:
                bound = bool(o.is_bound())
            except Exception:
                bound = None
            events.append({"name": snake, "cpp_name": cpp_name, "delegate_type": p.get("cpp_type"),
                           "category": p.get("category"), "owner_class": p.get("owner_class"),
                           "is_bound": bound})
        events.sort(key=lambda r: r["name"])
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
            "widget": widget_name, "widget_class": w.get_class().get_name(),
            "event_count": len(events), "events": events,
            "note": "BlueprintAssignable multicast events (bindable, e.g. on_clicked). Detected via "
                    "isinstance(getattr(widget, snake), unreal.MulticastDelegateBase); FGet* property-"
                    "binding '*_delegate' getters and plain UPROPERTYs are excluded. delegate_type names "
                    "the multicast delegate signature; is_bound reflects the CDO (design-time)."}))
'''

    # ================================================================== #
    # WRITE: set_widget_navigation                                       #
    # ================================================================== #
    _SET_NAV_BODY = _WA_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
direction = str(PARAMS.get("direction") or "").strip().lower()
rule_in = PARAMS.get("rule"); target = str(PARAMS.get("target_widget") or "").strip()
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif direction not in _NAV_DIRS:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "direction must be one of %s (got '%s')" % (_NAV_DIRS, direction)}))
else:
    rule_enum = _nav_rule_enum(rule_in)
    if rule_enum is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "rule '%s' is not a valid UINavigationRule (Escape/Stop/Wrap/Explicit/Custom/CustomBoundary)" % rule_in}))
    else:
        w = _find_widget(wb, widget_name)
        if w is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
        else:
            rname = _enum_name(rule_enum)
            need_target = (rname == "EXPLICIT")
            if need_target and not target:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "rule 'Explicit' requires target_widget (the widget to focus)"}))
            elif need_target and _find_widget(wb, target) is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "target_widget '%s' not found in tree" % target}))
            else:
                prior = _nav_capture(w)
                seed = dict(prior) if prior else {}
                wtf = target if rname in ("EXPLICIT", "CUSTOM", "CUSTOM_BOUNDARY") else ""
                seed[direction] = {"rule": rname, "widget_to_focus": wtf}
                try:
                    _nav_apply(w, seed)
                    okc, oks = _compile_save(wb, wbp_path)
                    wb2 = unreal.EditorAssetLibrary.load_asset(wbp_path)
                    w2 = _find_widget(wb2, widget_name)
                    after = _nav_capture(w2)
                    _ledger().append({"op": "set_widget_nav", "asset_path": wbp_path,
                        "widget_name": widget_name, "prior_nav_json": prior})
                    note = None
                    if rname in ("CUSTOM", "CUSTOM_BOUNDARY"):
                        note = "rule set, but the Custom navigation DELEGATE function is a K2 binding not settable from Python -> inert until wired in the editor."
                    print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                        "widget": widget_name, "direction": direction, "rule": rname,
                        "target_widget": wtf or None, "prior_navigation": prior,
                        "after_direction": (after or {}).get(direction), "navigation_now_set": after is not None,
                        "compiled": okc, "saved": oks, "ledger_depth": len(_ledger()), "note": note}))
                except Exception as e:
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "set navigation failed: %s" % str(e)[:200]}))
'''

    # ================================================================== #
    # WRITE: clear_widget_navigation                                     #
    # ================================================================== #
    _CLEAR_NAV_BODY = _WA_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
direction = str(PARAMS.get("direction") or "").strip().lower()
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif direction and direction not in _NAV_DIRS:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "direction (optional) must be empty (clear ALL) or one of %s (got '%s')" % (_NAV_DIRS, direction)}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        prior = _nav_capture(w)
        if prior is None:
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "widget": widget_name, "changed": False,
                "note": "widget already has no custom navigation (all directions default Escape); nothing to clear."}))
        else:
            try:
                if direction:
                    seed = dict(prior)
                    seed[direction] = {"rule": "ESCAPE", "widget_to_focus": ""}
                    _nav_apply(w, seed)
                    scope = "direction:%s" % direction
                else:
                    w.set_editor_property("navigation", None)
                    scope = "all"
                okc, oks = _compile_save(wb, wbp_path)
                wb2 = unreal.EditorAssetLibrary.load_asset(wbp_path)
                w2 = _find_widget(wb2, widget_name)
                after = _nav_capture(w2)
                _ledger().append({"op": "set_widget_nav", "asset_path": wbp_path,
                    "widget_name": widget_name, "prior_nav_json": prior})
                print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                    "widget": widget_name, "changed": True, "cleared": scope,
                    "prior_navigation": prior, "navigation_after": after,
                    "compiled": okc, "saved": oks, "ledger_depth": len(_ledger())}))
            except Exception as e:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "clear navigation failed: %s" % str(e)[:200]}))
'''

    # ================================================================== #
    # WRITE: add_widget_configured (add + slot props at add-time)         #
    # ================================================================== #
    _ADD_CONFIGURED_BODY = _WA_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_class_path = PARAMS["widget_class"]
widget_name = PARAMS["widget_name"]; parent_name = PARAMS.get("parent_name") or ""
slot_props = PARAMS.get("slot_properties") or {}
if not isinstance(slot_props, dict):
    slot_props = {}
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif _MRL is None or not hasattr(_MRL, "add_widget_to_blueprint"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler add_widget_to_blueprint unavailable (plugin DLL predates C++ #8)"}))
elif _find_widget(wb, widget_name) is not None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "a widget named '%s' already exists" % widget_name}))
else:
    res = json.loads(_MRL.add_widget_to_blueprint(wb, widget_class_path, widget_name, parent_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("added"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_widget_to_blueprint returned not-added for '%s'" % widget_name}))
    else:
        added_name = res.get("name") or widget_name
        w = _find_widget(wb, added_name)
        slot_results = []
        slot = None
        try:
            slot = w.slot if w is not None else None
        except Exception:
            slot = None
        if slot_props and slot is None:
            slot_results.append({"property": "*", "ok": False, "error": "widget has no slot (is root / parent not a panel) -> slot_properties ignored"})
        elif slot is not None:
            for prop, value in slot_props.items():
                prior_raw, via_get, gerr = _slot_get(slot, prop)
                if gerr:
                    slot_results.append({"property": prop, "ok": False, "error": gerr})
                    continue
                newv = _wdes(prior_raw, value)
                ok, via_set, serr = _slot_set(slot, prop, newv)
                if not ok:
                    slot_results.append({"property": prop, "ok": False, "error": serr})
                    continue
                after_raw, _vg, _e = _slot_get(slot, prop)
                after_json, _u = _wser(after_raw)
                slot_results.append({"property": prop, "ok": True, "via": via_set, "applied": after_json})
        okc, oks = _compile_save(wb, wbp_path)
        _ledger().append({"op": "add_widget", "wbp_path": wbp_path, "name": added_name})
        applied_ok = sum(1 for r in slot_results if r.get("ok"))
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": res.get("blueprint"),
            "added_widget": added_name, "class_path": widget_class_path,
            "is_root": res.get("is_root"), "parent": res.get("parent"),
            "slot_class": slot.get_class().get_name() if slot is not None else None,
            "slot_props_requested": len(slot_props), "slot_props_applied": applied_ok,
            "slot_results": slot_results, "compiled": okc, "saved": oks,
            "ledger_depth": len(_ledger())}))
'''

    # ============================ MCP TOOLS ============================ #

    @mcp.tool()
    def set_widget_blueprint_parent(ctx, widget_blueprint_path: str, new_parent_class: str) -> str:
        """Reparent a WidgetBlueprint ASSET to a different UserWidget subclass (a WidgetBlueprint IS a
        Blueprint -> BlueprintEditorLibrary.reparent_blueprint). Pure Python. Compiles + saves after.

        widget_blueprint_path: object/package path of the WidgetBlueprint asset.
        new_parent_class:      the new parent class. Accepts a friendly engine type name ('UserWidget'),
                               a class path ('/Script/UMG.UserWidget' or a generated-class object path),
                               or the object path of an existing UserWidget-derived WidgetBlueprint (its
                               generated class is used). VALIDATED to be a UserWidget subclass first;
                               refused if it is the WBP's own generated class (circular) or already the
                               current parent.

        The prior parent class path is captured. Ledgered op 'set_widget_bp_parent' {asset_path,
        prior_parent}; inverse (fold into editor_level.undo -- identical to the existing 'reparent_blueprint'
        inverse): load asset, prior_cls = unreal.load_object(None, prior_parent),
        BlueprintEditorLibrary.reparent_blueprint(bp, prior_cls) + compile + save. Surfaces UMG-compiler
        warnings via `_log_warnings`."""
        params = {"wbp_path": widget_blueprint_path, "new_parent_class": new_parent_class}
        try:
            return json.dumps(_exec(_SET_BP_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_widget_events(ctx, widget_blueprint_path: str, widget_name: str, filter: str = "",
                           include_inherited: bool = True) -> str:
        """List a widget's BlueprintAssignable multicast-delegate EVENTS (the bindable events like
        OnClicked / OnValueChanged you would wire in the UMG designer's event list). Read-only.

        widget_blueprint_path: object/package path of the WidgetBlueprint asset.
        widget_name:           name of the widget in the tree (e.g. 'Btn', 'MySlider').
        filter:                optional case-insensitive substring/glob on the event name.
        include_inherited:     include base-class events (default True).

        Each row: {name (snake_case, e.g. 'on_clicked'), cpp_name ('OnClicked'), delegate_type (the
        multicast signature, e.g. 'FOnButtonClickedEvent'), category, owner_class, is_bound}. Detection is
        exact: a property qualifies iff isinstance(getattr(widget, snake), unreal.MulticastDelegateBase) --
        this includes the true bindable events and EXCLUDES the FGet* property-binding '*Delegate' getters
        and plain UPROPERTYs. Names come from the C++ reflection metadata reader."""
        params = {"wbp_path": widget_blueprint_path, "widget_name": widget_name, "filter": filter,
                  "include_inherited": include_inherited}
        try:
            return json.dumps(_exec(_LIST_EVENTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_widget_navigation(ctx, widget_blueprint_path: str, widget_name: str, direction: str,
                              rule: str, target_widget: str = "") -> str:
        """Set ONE direction of a widget's focus-navigation rule (gamepad/keyboard focus movement). Pure
        Python (probed feasible: unreal.WidgetNavigation + whole-struct FWidgetNavigationData set +
        assign to the 'navigation' editor-property). Compiles + saves; verified to round-trip through reload.

        widget_blueprint_path: object/package path of the WidgetBlueprint asset.
        widget_name:           name of the widget in the tree.
        direction:             one of up | down | left | right | next | previous.
        rule:                  Escape | Stop | Wrap | Explicit | Custom | CustomBoundary (case-insensitive).
        target_widget:         REQUIRED for rule 'Explicit' -- the name of the widget to focus (validated
                               to exist in the tree). Ignored for Escape/Stop/Wrap.

        Other directions on the widget are PRESERVED (the new navigation object is seeded from the prior
        one). NOTE: rule 'Custom'/'CustomBoundary' sets the rule but the custom navigation DELEGATE
        function is a K2 binding not settable from Python -> it stays inert until wired in the editor (a
        `note` says so; the rule change is still reversible). The WHOLE prior navigation (all six
        directions, or None) is captured. Ledgered op 'set_widget_nav' {asset_path, widget_name,
        prior_nav_json}; inverse (fold into editor_level.undo): rebuild the widget's navigation from
        prior_nav_json (None -> set navigation None; dict -> new WidgetNavigation with each direction's
        rule + widget_to_focus) + compile + save."""
        params = {"wbp_path": widget_blueprint_path, "widget_name": widget_name, "direction": direction,
                  "rule": rule, "target_widget": target_widget}
        try:
            return json.dumps(_exec(_SET_NAV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def clear_widget_navigation(ctx, widget_blueprint_path: str, widget_name: str, direction: str = "") -> str:
        """Clear a widget's custom focus-navigation. Pure Python. Compiles + saves after.

        widget_blueprint_path: object/package path of the WidgetBlueprint asset.
        widget_name:           name of the widget in the tree.
        direction:             OPTIONAL. Empty (default) -> remove the whole UWidgetNavigation object
                               (every direction reverts to the default Escape). One of up|down|left|right|
                               next|previous -> reset just that direction to Escape (other directions kept).

        If the widget has no custom navigation already, this is a no-op (changed=false, nothing ledgered).
        Otherwise the WHOLE prior navigation is captured. Ledgered op 'set_widget_nav' {asset_path,
        widget_name, prior_nav_json} (same op as set_widget_navigation); inverse (fold into
        editor_level.undo): rebuild the widget's navigation from prior_nav_json + compile + save
        (restores the exact prior per-direction rules/targets)."""
        params = {"wbp_path": widget_blueprint_path, "widget_name": widget_name, "direction": direction}
        try:
            return json.dumps(_exec(_CLEAR_NAV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_widget_configured(ctx, widget_blueprint_path: str, widget_class: str, widget_name: str,
                              parent_name: str = "", slot_properties: dict = None) -> str:
        """Add a widget to a WidgetBlueprint's tree AND apply slot/layout properties at add-time (the
        shipped add_widget cannot set slot props during the add). Adds via the C++ #8 handler, then applies
        each slot property via the shipped set_slot_properties dual method/editor-property path, then does
        ONE compile + save. REQUIRES the C++ #8 handler.

        widget_blueprint_path: object/package path of the WidgetBlueprint asset.
        widget_class:          class path of the widget to add ('/Script/UMG.Button', '/Script/UMG.Border',
                               '/Script/UMG.TextBlock', ...).
        widget_name:           name for the new widget (must not already exist).
        parent_name:          name of the parent PANEL widget to nest under. Empty ("") sets the new widget
                               as the tree ROOT (only valid on an empty tree). Slot properties only apply
                               when the widget lands inside a panel (a root widget has no slot).
        slot_properties:       dict {slot_prop: value} applied after the add. Same forms as
                               set_widget_slot_property: CanvasPanelSlot 'position' [x,y], 'size' [x,y],
                               'offsets' [l,t,r,b], 'anchors' [minx,miny,maxx,maxy], 'alignment' [x,y],
                               'z_order' int, 'auto_size' bool; box slots 'padding', 'size'
                               {value,rule}, 'horizontal_alignment', 'vertical_alignment'. Per-property
                               results are returned in 'slot_results' (best-effort: an unsettable prop is
                               reported, it does not abort the add).

        Ledgered op REUSES the existing 'add_widget' {wbp_path, name} (already folded in editor_level.undo):
        inverse remove_widget_from_blueprint(name) -> removes the widget (and, if a panel, its children).
        NO new fold needed. Surfaces UMG-compiler warnings via `_log_warnings`."""
        params = {"wbp_path": widget_blueprint_path, "widget_class": widget_class, "widget_name": widget_name,
                  "parent_name": parent_name, "slot_properties": slot_properties or {}}
        try:
            return json.dumps(_exec(_ADD_CONFIGURED_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
