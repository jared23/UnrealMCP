"""UserTools :: Widgets / UMG (WRITE + READ, extension #3)  (spec: docs/spec/widgets.md)

Third widget-authoring module. widgets_write.py owns create/add/remove; widgets_write2.py owns
set_widget_property / set_widget_slot_property / reparent_widget. This "#3" module fills the remaining
Tier-1, ASSET-based, cleanly-reversible gaps that were left MISSING/PARTIAL by the p4 parity audit and
are reachable from stock UE 5.8 Python (+ the already-shipped C++ #8 widget handlers) WITHOUT any new
C++ and WITHOUT ever opening the UMG editor / popping a modal:

READ (no ledger, no mutation):
  - get_widget_properties  per-widget UPROPERTY getter (parity was PARTIAL: only the whole-BP dump
                           existed). Enumerates the widget's reflected properties (via the C++ #1
                           MCPReflectionLibrary.get_object_property_metadata_json) and reads each live
                           VALUE (get_editor_property on the snake_case name), serialized with the same
                           self-describing marker-dict scheme widgets_write2 uses.
  - get_slot_properties    per-widget SLOT getter (parity was PARTIAL). Dual method-getter/editor-prop
                           path covering CanvasPanelSlot (position/size/offsets/anchors/alignment/
                           z_order/auto_size) and box/overlay slots (padding/size/alignment).
  - list_widget_types      widget-scoped class enumerator (parity: MISSING; generic search_classes is
                           not widget-scoped). Lists instantiable UWidget subclasses exposed on unreal.*
                           with each one's class_path (ready to feed add_widget) + is_panel flag.
  - get_widget_class_info  class metadata + slot behaviour for one widget class (parity: MISSING).
  - get_widget_navigation  a widget's directional focus-navigation rules (parity: MISSING). Reads the
                           UWidgetNavigation UPROPERTY (default None -> ESCAPE rules everywhere).

WRITE (ScopedEditorTransaction-free asset edits, compiled+saved, ledgered with a reversible inverse):
  - duplicate_widget       duplicate a widget (+ its whole child subtree) under a parent panel, copying
                           each widget's readable UPROPERTYs and slot layout best-effort. Built on the
                           C++ #8 add handler. Inverse = remove the duplicate root (remove_widget_from_
                           blueprint drops it + all its children) -> reuses the EXISTING 'add_widget'
                           ledger op already folded in editor_level.undo (NO new fold needed).

DEFERRED (probed / reasoned unreachable-or-unrevertable in this build; reported, not faked):
  - rename_widget : Object.rename DOES rename the widget, but recompile then trips HANDLED UMG-compiler
      ensures every time -- "WidgetVariableNameToGuidMap.Contains(Widget->GetFName())" +
      "SeenVariableNames.Contains(It.Key())" (WidgetBlueprintCompiler.cpp:781/815) -- because the rename
      desyncs the WidgetBlueprint's WidgetVariableNameToGuidMap, which is a PROTECTED member NOT reachable
      from Python (get_editor_property refuses both 'widget_variable_name_to_guid_map' and the raw name),
      and neither WidgetBlueprintLibrary nor WidgetBlueprintEditorLibrary is exposed. The compiler self-
      heals so the state round-trips, but per the "verify via Output Log" rule a write that trips ensures
      is a BAD write. (Earlier raw-snippet probes MISSED this -- raw snippets don't capture the Output Log;
      only the module _query path surfaced the ensures.) Needs a C++ handler (RenameWidgetInBlueprint) that
      moves the GUID-map entry, exactly as widgets_write2.py already documented. NOT shipped.
  - set_widget_is_variable : bIsVariable IS a UPROPERTY (it shows up in the C++ metadata reader) but
      get_editor_property REFUSES it live ("Failed to find property 'is_variable'") because it is not
      editor-exposed, and there is no C++ setter handler -> no reachable AND reversible path. Needs a
      small C++ handler (SetWidgetIsVariable) to ship faithfully.
  - set/clear_widget_navigation, list/get/set/clear named-slot content : reading navigation is shipped
      (get_widget_navigation), but MUTATING the UWidgetNavigation object + UserWidget named-slot content
      is editor/protected-UPROPERTY territory in this Python surface (same wall widgets_read documents) ->
      C++ handler required for a faithful reversible write.
  - widget ANIMATIONS (create/track/key), MVVM/viewmodels, property BINDINGS (K2 graph) : Tier-2. A
      UWidgetAnimation is a MovieScene asset; WidgetBlueprint.Animations is a protected UPROPERTY; MVVM
      has no clean stock API; bindings are graph edits. Out of scope for this asset-only, no-C++ batch.

Scaffolding (query convention, base64 PARAMS, Output-Log capture, per-session ledger) copied VERBATIM
from editor_level.py / widgets_write2.py. The read value-serializer + write priors reuse widgets_write2's
self-describing marker-dict encoding (__v2__/__margin__/__anchors__/__childsize__/__enum__/__lincolor__/
__color__/__object__) so anything captured decodes through editor_level.undo's existing _ww_desr.

Ledger ops (NO new fold needed -- duplicate reuses the EXISTING add_widget op already in editor_level.undo):
  {op: "add_widget", wbp_path, name(=duplicate root name)}   <-- ALREADY folded (widgets_write.add_widget)
     inverse: mrl.remove_widget_from_blueprint(wb, name) + compile + save (drops dup + all its children).
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
# contain NO ''' and NO stray backslashes (a lone backslash is silently eaten in transport -- proven
# live: a regex backreference "\\1" arrived as \x01). All data passes as base64. Never assign a local
# named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
# code_obj (they are the C++ wrapper's own locals).


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
    #   _ledger()                 per-session undo stack (copied verbatim from editor_level.py).
    #   _load_wb / _find_widget / _compile_save   as in widgets_write2.py.
    #   _to_snake(Pascal)         backslash-free PascalCase->snake_case (strips a leading b<Upper> flag).
    #   _wser(v)                  self-describing marker-dict value serializer (widgets_write2 scheme).
    #   _slot_get / slot getter list   dual method/editor-prop slot reader.
    #   _uniq_name(wt, base)      first tree-unused name from base, base_1, base_2, ...
    _WW3_HELPERS = r'''
import unreal, json, builtins, fnmatch
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
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
        elif c.isdigit():
            prev = s[i-1] if i > 0 else ""
            if prev != "" and prev.isalpha() and prev.islower():
                pass
            res.append(c)
        else:
            res.append(c)
    return "".join(res)
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _wser(v):
    # (json_form, restorable, kind). Marker dicts are self-describing so editor_level._ww_desr rebuilds.
    if v is None:
        return (None, True, "none")
    if isinstance(v, bool):
        return (v, True, "bool")
    if isinstance(v, int):
        return (v, True, "int")
    if isinstance(v, float):
        return (v, True, "float")
    if isinstance(v, str):
        return (v, True, "str")
    if isinstance(v, unreal.Vector2D):
        return ({"__v2__": [v.x, v.y]}, True, "Vector2D")
    if isinstance(v, unreal.Vector):
        return ({"__v3__": [v.x, v.y, v.z]}, True, "Vector")
    if isinstance(v, unreal.Margin):
        return ({"__margin__": [v.left, v.top, v.right, v.bottom]}, True, "Margin")
    if isinstance(v, unreal.Anchors):
        return ({"__anchors__": [v.minimum.x, v.minimum.y, v.maximum.x, v.maximum.y]}, True, "Anchors")
    if isinstance(v, unreal.SlateChildSize):
        try:
            return ({"__childsize__": [float(v.get_editor_property("value")), _enum_name(v.get_editor_property("size_rule"))]}, True, "SlateChildSize")
        except Exception:
            return (str(v)[:80], False, "SlateChildSize")
    if isinstance(v, unreal.LinearColor):
        return ({"__lincolor__": [v.r, v.g, v.b, v.a]}, True, "LinearColor")
    if isinstance(v, unreal.Color):
        return ({"__color__": [v.r, v.g, v.b, v.a]}, True, "Color")
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True, type(v).__name__)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_name(v)}, True, "enum")
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True, "object")
        except Exception:
            return (None, False, "object")
    return (str(v)[:120], False, type(v).__name__)
_SLOT_GETTERS = ["position", "size", "offsets", "anchors", "alignment", "z_order", "auto_size",
                 "padding", "horizontal_alignment", "vertical_alignment", "layer", "row", "column",
                 "row_span", "column_span", "nudge"]
def _slot_get(slot, prop):
    g = getattr(slot, "get_" + prop, None)
    if g is not None:
        try:
            return g(), "method"
        except Exception:
            pass
    try:
        return slot.get_editor_property(prop), "editorprop"
    except Exception:
        return None, None
def _slot_set(slot, prop, val):
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
def _uniq_name(wt, base):
    if wt is None:
        return base
    cand = base; i = 1
    while True:
        try:
            ex = unreal.find_object(wt, cand)
        except Exception:
            ex = None
        if ex is None:
            return cand
        cand = "%s_%d" % (base, i); i += 1
'''

    # ================================================================== #
    # READ: get_widget_properties                                        #
    # ================================================================== #
    _GET_PROPS_BODY = _WW3_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
name_filter = PARAMS.get("filter") or ""
include_inherited = PARAMS.get("include_inherited")
include_inherited = True if include_inherited is None else bool(include_inherited)
max_results = int(PARAMS.get("max_results") or 300)
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif _MRL is None or not hasattr(_MRL, "get_object_property_metadata_json"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ reflection reader get_object_property_metadata_json unavailable"}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        try:
            meta = json.loads(_MRL.get_object_property_metadata_json(w, include_inherited))
        except Exception as e:
            meta = {"properties": []}
        rows = []
        skipped = 0
        for p in meta.get("properties", []) or []:
            cpp_name = p.get("name")
            if not cpp_name or not _match(cpp_name, name_filter):
                continue
            if cpp_name.endswith("Delegate"):
                skipped += 1; continue
            snake = _to_snake(cpp_name)
            try:
                raw = w.get_editor_property(snake)
            except Exception:
                skipped += 1; continue
            jval, restorable, kind = _wser(raw)
            rows.append({"name": snake, "cpp_name": cpp_name, "kind": kind, "value": jval,
                         "cpp_type": p.get("cpp_type"), "category": p.get("category"),
                         "reversible": restorable})
            if len(rows) >= max_results:
                break
        rows.sort(key=lambda r: r["name"])
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
            "widget": widget_name, "widget_class": w.get_class().get_name(),
            "property_count": len(rows), "skipped": skipped, "properties": rows}))
'''

    # ================================================================== #
    # READ: get_slot_properties                                          #
    # ================================================================== #
    _GET_SLOT_BODY = _WW3_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        slot = None
        try:
            slot = w.slot
        except Exception:
            slot = None
        if slot is None:
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "widget": widget_name, "slot_class": None, "properties": {},
                "note": "widget has no slot (it is the tree ROOT or not inside a panel)"}))
        else:
            props = {}
            for prop in _SLOT_GETTERS:
                raw, via = _slot_get(slot, prop)
                if via is None:
                    continue
                jval, restorable, kind = _wser(raw)
                props[prop] = {"value": jval, "kind": kind, "via": via, "reversible": restorable}
            parent = None
            try:
                par = w.get_parent()
                parent = par.get_name() if par is not None else None
            except Exception:
                parent = None
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "widget": widget_name, "slot_class": slot.get_class().get_name(),
                "parent": parent, "property_count": len(props), "properties": props}))
'''

    # ================================================================== #
    # READ: list_widget_types                                            #
    # ================================================================== #
    _LIST_TYPES_BODY = _WW3_HELPERS + r'''
name_filter = PARAMS.get("filter") or ""
panels_only = bool(PARAMS.get("panels_only"))
max_results = int(PARAMS.get("max_results") or 500)
base = unreal.Widget
panel_base = unreal.PanelWidget
uw_base = unreal.UserWidget
rows = []
for nm in dir(unreal):
    o = getattr(unreal, nm, None)
    try:
        ok = isinstance(o, type) and issubclass(o, base) and o is not base
    except Exception:
        ok = False
    if not ok:
        continue
    try:
        is_panel = issubclass(o, panel_base)
    except Exception:
        is_panel = False
    if panels_only and not is_panel:
        continue
    if not _match(nm, name_filter):
        continue
    cpath = None
    try:
        cpath = o.static_class().get_path_name()
    except Exception:
        cpath = None
    try:
        is_uw = issubclass(o, uw_base)
    except Exception:
        is_uw = False
    rows.append({"name": nm, "class_path": cpath, "is_panel": is_panel, "is_user_widget": is_uw})
    if len(rows) >= max_results:
        break
rows.sort(key=lambda r: r["name"])
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(rows),
    "panels_only": panels_only, "filter": name_filter, "widget_types": rows,
    "note": "Instantiable UWidget subclasses exposed on unreal.*; class_path feeds add_widget. A few "
            "abstract intermediate bases (e.g. ContentWidget) may appear; abstract-ness is not filtered."}))
'''

    # ================================================================== #
    # READ: get_widget_class_info                                        #
    # ================================================================== #
    _CLASS_INFO_BODY = _WW3_HELPERS + r'''
spec = PARAMS.get("widget_class")
cls = None; resolved_via = None
obj = getattr(unreal, str(spec), None)
if isinstance(obj, type):
    try:
        cls = obj.static_class(); resolved_via = "unreal.%s" % spec
    except Exception:
        cls = None
if cls is None:
    try:
        c = unreal.load_class(None, str(spec))
        if isinstance(c, unreal.Class):
            cls = c; resolved_via = "load_class"
    except Exception:
        cls = None
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "could not resolve widget_class '%s' (tried unreal.<name> and load_class path)" % spec}))
else:
    cdo = None
    try:
        cdo = unreal.get_default_object(cls)
    except Exception:
        cdo = None
    is_widget = isinstance(cdo, unreal.Widget) if cdo is not None else None
    if is_widget is False:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "'%s' is not a UWidget subclass" % spec}))
    else:
        meta = {}
        if _MRL is not None and hasattr(_MRL, "get_class_metadata_json"):
            try:
                meta = json.loads(_MRL.get_class_metadata_json(cls))
            except Exception:
                meta = {}
        prop_count = None
        if cdo is not None and _MRL is not None and hasattr(_MRL, "get_object_property_metadata_json"):
            try:
                pm = json.loads(_MRL.get_object_property_metadata_json(cdo, True))
                prop_count = len(pm.get("properties", []) or [])
            except Exception:
                prop_count = None
        is_panel = isinstance(cdo, unreal.PanelWidget) if cdo is not None else None
        is_uw = isinstance(cdo, unreal.UserWidget) if cdo is not None else None
        print("@@UMCP@@" + json.dumps({"status": "success", "resolved_via": resolved_via,
            "name": cls.get_name(), "class_path": cls.get_path_name(),
            "parent_class": meta.get("parent_class"), "is_abstract": meta.get("is_abstract"),
            "is_blueprintable": meta.get("is_blueprintable"), "is_deprecated": meta.get("is_deprecated"),
            "is_panel": is_panel, "accepts_children": is_panel, "is_user_widget": is_uw,
            "property_count": prop_count}))
'''

    # ================================================================== #
    # READ: get_widget_navigation                                        #
    # ================================================================== #
    _GET_NAV_BODY = _WW3_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    w = _find_widget(wb, widget_name)
    if w is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    else:
        nav = None; nav_err = None
        try:
            nav = w.get_editor_property("navigation")
        except Exception as e:
            nav_err = str(e)[:120]
        if nav_err is not None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "navigation not readable: %s" % nav_err}))
        elif nav is None:
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "widget": widget_name, "navigation_set": False,
                "rules": {d: "Escape" for d in ["up", "down", "left", "right", "next", "previous"]},
                "note": "No custom navigation object -> every direction uses the default ESCAPE rule."}))
        else:
            dirs = ["up", "down", "left", "right", "next", "previous"]
            rules = {}
            for d in dirs:
                entry = {}
                try:
                    entry["rule"] = _enum_name(nav.get_editor_property(d + "_rule"))
                except Exception:
                    entry["rule"] = None
                try:
                    wtn = nav.get_editor_property(d)
                    entry["widget_to_focus"] = str(wtn) if wtn else None
                except Exception:
                    entry["widget_to_focus"] = None
                rules[d] = entry
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "widget": widget_name, "navigation_set": True, "rules": rules,
                "note": "Per-direction UWidgetNavigation rules (Escape/Stop/Wrap/Explicit/Custom/"
                        "CustomBoundary); widget_to_focus is the explicit target for Explicit rules."}))
'''

    # ================================================================== #
    # WRITE: duplicate_widget (+ children)                               #
    # ================================================================== #
    _DUPLICATE_BODY = _WW3_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["widget_name"]
new_name = PARAMS["new_name"]; parent_name = PARAMS.get("parent_name") or ""
wb, err = _load_wb(wbp_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif _MRL is None or not hasattr(_MRL, "add_widget_to_blueprint"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "C++ handler add_widget_to_blueprint unavailable (plugin DLL predates C++ #8)"}))
else:
    src = _find_widget(wb, widget_name)
    nn = str(new_name or "").strip()
    valid_ident = bool(nn) and (nn[0].isalpha() or nn[0] == "_") and all((ch.isalnum() or ch == "_") for ch in nn)
    wt = _widget_tree(wb)
    # default parent = the source widget's current parent
    if not parent_name:
        try:
            sp = src.get_parent() if src is not None else None
            parent_name = sp.get_name() if sp is not None else ""
        except Exception:
            parent_name = ""
    parent_w = _find_widget(wb, parent_name) if parent_name else None
    if src is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found in tree: %s" % widget_name}))
    elif not valid_ident:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_name '%s' is not a valid identifier" % new_name}))
    elif _find_widget(wb, nn) is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "a widget named '%s' already exists" % nn}))
    elif not parent_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot duplicate the ROOT widget without a target parent_name (duplicate would have no parent)"}))
    elif parent_w is None or not isinstance(parent_w, unreal.PanelWidget):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent '%s' not found or not a PanelWidget" % parent_name}))
    else:
        stats = {"widgets": 0, "props_copied": 0, "props_skipped": 0, "slots_copied": 0}
        def _copy_props(so, dw):
            try:
                meta = json.loads(_MRL.get_object_property_metadata_json(so, False))
            except Exception:
                meta = {"properties": []}
            for p in meta.get("properties", []) or []:
                cpp_name = p.get("name")
                if not cpp_name or cpp_name.endswith("Delegate"):
                    continue
                flags = p.get("flags") or []
                snake = _to_snake(cpp_name)
                try:
                    val = so.get_editor_property(snake)
                except Exception:
                    continue
                try:
                    dw.set_editor_property(snake, val); stats["props_copied"] += 1
                except Exception:
                    stats["props_skipped"] += 1
        def _copy_slot(so, dw):
            try:
                ss = so.slot; ds = dw.slot
            except Exception:
                return
            if ss is None or ds is None:
                return
            if ss.get_class().get_name() != ds.get_class().get_name():
                return
            for prop in _SLOT_GETTERS:
                raw, via = _slot_get(ss, prop)
                if via is None:
                    continue
                if _slot_set(ds, prop, raw):
                    stats["slots_copied"] += 1
        def _dup(node, dst_parent_name, want_name):
            cpath = node.get_class().get_path_name()
            r = json.loads(_MRL.add_widget_to_blueprint(wb, cpath, want_name, dst_parent_name))
            if isinstance(r, dict) and r.get("error"):
                return None, r.get("error")
            if not r.get("added"):
                return None, "add returned not-added for %s" % want_name
            dw = _find_widget(wb, want_name)
            if dw is None:
                return None, "could not find freshly-added %s" % want_name
            stats["widgets"] += 1
            _copy_props(node, dw)
            _copy_slot(node, dw)
            if isinstance(node, unreal.PanelWidget) and isinstance(dw, unreal.PanelWidget):
                try:
                    kn = node.get_children_count()
                except Exception:
                    kn = 0
                for i in range(kn):
                    try:
                        child = node.get_child_at(i)
                    except Exception:
                        child = None
                    if child is None:
                        continue
                    child_want = _uniq_name(wt, nn + "_" + child.get_name())
                    _cw, cerr = _dup(child, want_name, child_want)
                    if cerr:
                        return None, "child dup failed: %s" % cerr
            return dw, None
        top, derr = _dup(src, parent_name, nn)
        if derr or top is None:
            # best-effort rollback of a partial duplicate
            try: _MRL.remove_widget_from_blueprint(wb, nn)
            except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "duplicate failed: %s" % (derr or "unknown"), "partial_rolled_back": True}))
        else:
            okc, oks = _compile_save(wb, wbp_path)
            _ledger().append({"op": "add_widget", "wbp_path": wbp_path, "name": nn})
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": wb.get_name(),
                "source": widget_name, "duplicate": nn, "parent": parent_name,
                "source_class": src.get_class().get_name(),
                "widgets_created": stats["widgets"], "props_copied": stats["props_copied"],
                "props_skipped": stats["props_skipped"], "slots_copied": stats["slots_copied"],
                "compiled": okc, "saved": oks, "ledger_depth": len(_ledger())}))
'''

    # ============================ MCP TOOLS ============================ #

    @mcp.tool()
    def get_widget_properties(ctx, wbp_path: str, widget_name: str, filter: str = "",
                              include_inherited: bool = True, max_results: int = 300) -> str:
        """Read the reflected UPROPERTYs (name + live VALUE) of one widget in a WidgetBlueprint tree.
        Read-only (no UMG editor opened).

        wbp_path:          object/package path of the WidgetBlueprint asset.
        widget_name:       name of the widget in the tree (e.g. 'Title', 'Btn').
        filter:            optional case-insensitive substring/glob on the property name (snake or cpp).
        include_inherited: include base-class properties (default True).
        max_results:       cap on returned rows (default 300).

        Each row: {name (snake_case, usable with set_widget_property), cpp_name, kind, value (serialized
        with the same self-describing marker-dict scheme as widgets_write2: Vector2D/Margin/Anchors/
        SlateChildSize/enum/color/object), cpp_type, category, reversible}. Delegate properties and
        editor-unreadable props are skipped (counted in 'skipped'). Names come from the C++ reflection
        reader; each value is read live via get_editor_property on the snake_case name."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name, "filter": filter,
                  "include_inherited": include_inherited, "max_results": max_results}
        try:
            return json.dumps(_exec(_GET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_slot_properties(ctx, wbp_path: str, widget_name: str) -> str:
        """Read a widget's LAYOUT/SLOT properties (its placement within its parent panel). Read-only.

        wbp_path:    object/package path of the WidgetBlueprint asset.
        widget_name: name of the widget whose slot to read.

        Uses a dual method-getter/editor-property path covering CanvasPanelSlot (position/size/offsets/
        anchors/alignment/z_order/auto_size), box/overlay slots (padding/size/horizontal_alignment/
        vertical_alignment) and grid/uniform-grid slots (row/column/span/layer/nudge) where present.
        Returns {slot_class, parent, properties:{prop:{value(serialized), kind, via, reversible}}}. If the
        widget is the tree root / not inside a panel, slot_class is null and properties is empty. The
        property names + serialized values feed straight into set_widget_slot_property."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_GET_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_widget_types(ctx, filter: str = "", panels_only: bool = False, max_results: int = 500) -> str:
        """Enumerate instantiable UWidget subclasses (widget-scoped class list). Read-only.

        filter:      optional case-insensitive substring/glob on the class name (e.g. 'button', '*box').
        panels_only: only classes that can contain children (UPanelWidget subclasses).
        max_results: cap (default 500).

        Each row: {name, class_path, is_panel, is_user_widget}. class_path (e.g. '/Script/UMG.TextBlock')
        is ready to pass to add_widget. Source is the UWidget subclasses exposed on the unreal.* module
        (native widget types). A few abstract intermediate bases may appear (abstract-ness is not filtered;
        use get_widget_class_info to check is_abstract for a specific class)."""
        params = {"filter": filter, "panels_only": panels_only, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_TYPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_widget_class_info(ctx, widget_class: str) -> str:
        """Class-level metadata + slot behaviour for a single widget class. Read-only.

        widget_class: a friendly engine type name ('Button', 'TextBlock', 'CanvasPanel') or a class path
                      ('/Script/UMG.Button').

        Returns {name, class_path, parent_class, is_abstract, is_blueprintable, is_deprecated, is_panel,
        accepts_children (=is_panel), is_user_widget, property_count}. Resolves the class via unreal.<name>
        or load_class; verified to be a UWidget subclass. Class metadata comes from the C++
        get_class_metadata_json reader; property_count from the property-metadata reader on the CDO."""
        params = {"widget_class": widget_class}
        try:
            return json.dumps(_exec(_CLASS_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_widget_navigation(ctx, wbp_path: str, widget_name: str) -> str:
        """Read a widget's directional focus-navigation rules (gamepad/keyboard focus movement). Read-only.

        wbp_path:    object/package path of the WidgetBlueprint asset.
        widget_name: name of the widget in the tree.

        Returns per-direction (up/down/left/right/next/previous) rules. If the widget has no custom
        UWidgetNavigation object (navigation_set=false, the default for most widgets), every direction
        reports the default 'Escape' rule. When set, each direction is {rule (Escape/Stop/Wrap/Explicit/
        Custom/CustomBoundary), widget_to_focus (explicit target, if any)}. Reading is fully supported;
        MUTATING navigation needs a C++ handler (see module DEFERRED)."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_GET_NAV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def duplicate_widget(ctx, wbp_path: str, widget_name: str, new_name: str, parent_name: str = "") -> str:
        """Duplicate a widget AND its whole child subtree under a parent panel. Built on the C++ #8 add
        handler; copies each widget's readable UPROPERTYs and slot layout best-effort. Compiles + saves.

        wbp_path:    object/package path of the WidgetBlueprint asset.
        widget_name: name of the widget to duplicate.
        new_name:    name for the duplicate's ROOT (valid identifier; must not already exist). Duplicated
                     descendants are named '<new_name>_<originalChildName>' (de-duplicated if needed).
        parent_name: destination PANEL widget for the duplicate root. Empty -> the SOURCE widget's current
                     parent (duplicate as a sibling). Refused if the source is the tree root and no
                     parent_name is given, or if the target is not a PanelWidget.

        The duplicate is a best-effort copy (property/slot values that are not editor-settable are
        skipped; counts returned as props_copied/props_skipped/slots_copied). Undo is EXACT regardless:
        ledgered with the existing 'add_widget' op {wbp_path, name=new_name} whose inverse (already in
        editor_level.undo) removes the duplicate root -- which drops it and all its duplicated children.
        Surfaces UMG-compiler warnings via `_log_warnings`."""
        params = {"wbp_path": wbp_path, "widget_name": widget_name, "new_name": new_name,
                  "parent_name": parent_name}
        try:
            return json.dumps(_exec(_DUPLICATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
