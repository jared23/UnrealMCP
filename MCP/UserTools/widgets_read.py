"""UserTools :: Widgets / UMG (READ)  (spec: docs/spec/widgets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO UMG / Widget editor is ever opened (no modals). A WidgetBlueprint IS a Blueprint, so
this mirrors blueprints_read.py's introspection + honest-limitation style: AssetRegistry tag
reads, BlueprintEditorLibrary parent/graph queries, and reflection over the generated class CDO
via unreal.MCPReflectionLibrary. Query convention + base64 PARAMS + Output-Log capture are copied
verbatim from editor_level.py (the gold standard).

HOW THE WIDGET TREE IS REACHED (verified live in this build):
  The editable WidgetTree (UWidgetTree) and its RootWidget/AllWidgets/Animations are PROTECTED
  UPROPERTYs -> get_editor_property refuses them, and UWidgetTree exposes no UFUNCTION getters.
  BUT the widget archetype instances are named sub-objects OUTERED to the WidgetTree, so:
    * unreal.find_object(widget_blueprint, "WidgetTree")  -> the UWidgetTree object.
    * unreal.find_object(widget_tree, "<WidgetName>")     -> the archetype UWidget instance.
    * a "variable" widget (Is Variable / BindWidget) is a UPROPERTY on the generated class, so
      MCPReflectionLibrary CDO metadata gives us at least one name to bootstrap from.
    * UWidget.get_parent() walks UP (root's parent is None); UPanelWidget.get_children_count()/
      get_child_at(i) walk DOWN. So from any one named widget we find the root, then recurse the
      WHOLE tree -- including non-variable widgets, their widget class, slot type, and TextBlock
      text. This recovers the full hierarchy with stock Python (no C++ handler needed).

Known limits (reported honestly in payloads, NOT hidden):
  * ANIMATIONS: WidgetBlueprint.Animations is a PROTECTED UPROPERTY and animations do not surface
    as generated-class UPROPERTYs -> animation names are NOT reachable from stock Python here
    (would need a C++ handler over UWidgetBlueprint::Animations). Reported as animations_reachable
    = False with the reason.
  * ENTRY POINT: the tree walk bootstraps from a "variable" widget. A WidgetBlueprint with ZERO
    variable widgets gives no Python-reachable entry (RootWidget itself is protected), so its tree
    cannot be reconstructed -> reported as tree_reachable = False. (All 21 WidgetBlueprints in this
    project have >=1 variable widget, so this is an edge case, surfaced honestly when hit.)
  * SLOT layout: slot CLASS is always available; slot layout values (position/size/padding/
    alignment) are read best-effort via the slot's public getters and may be partial per slot type.
  * BP function-GRAPH internals (nodes/wiring) are editor-only (same as blueprints_read) -> we
    surface graph names, not node graphs.

Implemented (all read-only):
  - list_widget_blueprints    (AssetRegistry WidgetBlueprint assets; counts + paths)
  - get_widget_blueprint_info  (parent/generated class, full widget tree, named widgets, anims note)
  - list_widget_hierarchy      (focused nested parent->children layout tree)

Deferred (writes; NOT this batch): create_widget_blueprint, add/remove/reparent widget,
  set widget property, add/rename animation, bind/unbind widget. These need editor-only UMG
  authoring API (UWidgetBlueprint / WidgetTree editing / FWidgetBlueprintEditor) -> a C++ handler
  plus ScopedEditorTransaction + per-session ledger inverse. No ledger / no `undo` tool here
  (this batch performs no mutations).
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this text.
    #   _load_wb(path)        -> (wb, err): load + verify it is a WidgetBlueprint.
    #   _widget_tree(wb)      -> UWidgetTree object via find_object (or None).
    #   _var_widgets(wb, wt)  -> {name: widget_instance} for every "variable" widget on the
    #                            generated class (bootstrap entry points into the tree).
    #   _root_from(w)         -> topmost ancestor (RootWidget) by walking get_parent().
    #   _slot_info(w)         -> {slot_class, ...best-effort layout} for a widget's slot.
    #   _txt(w)               -> visible text for text-bearing widgets (or None).
    #   _walk(w, depth, maxd, varset, opts) -> nested node dict.
    _WIDGET_HELPERS = r'''
import unreal, json, warnings, fnmatch
warnings.simplefilter("ignore")
_BEL = unreal.BlueprintEditorLibrary
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
_HAS_MRL = _MRL is not None
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _load_wb(path):
    if not path:
        return None, "no widget_blueprint path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.WidgetBlueprint):
        cn = obj.get_class().get_name()
        hint = "" if not isinstance(obj, unreal.Blueprint) else " (it is a Blueprint; use blueprints_read tools)"
        return None, "asset is not a WidgetBlueprint (got %s)%s: %s" % (cn, hint, path)
    return obj, None
def _widget_tree(wb):
    try:
        wt = unreal.find_object(wb, "WidgetTree")
        return wt
    except Exception:
        return None
def _gen(wb):
    try:
        return _BEL.generated_class(wb)
    except Exception:
        return None
def _cdo(gcls):
    if gcls is None:
        return None
    try:
        return unreal.get_default_object(gcls)
    except Exception:
        return None
def _meta_props(cdo):
    if cdo is None or not _HAS_MRL:
        return []
    try:
        d = json.loads(_MRL.get_object_property_metadata_json(cdo))
        return d.get("properties", []) or []
    except Exception:
        return []
def _var_widgets(wb, wt, gname, props):
    # variable widgets = UPROPERTYs owned by the generated class that resolve (by name) to a
    # UWidget archetype under the WidgetTree. Returns dict name->instance (insertion order).
    out = {}
    if wt is None:
        return out
    for p in props:
        if p.get("owner_class") != gname:
            continue
        nm = p.get("name")
        if not nm or nm == "UberGraphFrame":
            continue
        try:
            w = unreal.find_object(wt, nm)
        except Exception:
            w = None
        if w is not None and isinstance(w, unreal.Widget):
            out[nm] = w
    return out
def _root_from(w):
    cur = w
    for _i in range(500):
        try:
            p = cur.get_parent()
        except Exception:
            p = None
        if p is None:
            return cur
        cur = p
    return cur
def _v2(v):
    try:
        return [round(v.x, 4), round(v.y, 4)]
    except Exception:
        return None
def _margin(m):
    try:
        return {"left": m.left, "top": m.top, "right": m.right, "bottom": m.bottom}
    except Exception:
        return None
def _enum(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _slot_info(w):
    info = {}
    try:
        s = w.slot
    except Exception:
        s = None
    if s is None:
        return None
    try:
        info["slot_class"] = s.get_class().get_name()
    except Exception:
        info["slot_class"] = None
    getters = {"position": "get_position", "size": "get_size", "alignment": "get_alignment",
               "padding": "get_padding", "h_align": "get_horizontal_alignment",
               "v_align": "get_vertical_alignment", "offsets": "get_offsets"}
    for key, fn in getters.items():
        f = getattr(s, fn, None)
        if f is None:
            continue
        try:
            val = f()
        except Exception:
            continue
        if isinstance(val, unreal.Vector2D):
            info[key] = _v2(val)
        elif isinstance(val, unreal.Margin):
            info[key] = _margin(val)
        elif isinstance(val, unreal.EnumBase):
            info[key] = _enum(val)
        else:
            info[key] = str(val)[:60]
    return info
def _txt(w):
    for fn in ("get_text",):
        f = getattr(w, fn, None)
        if f is None:
            continue
        try:
            t = f()
            t = str(t)
            return t if t != "" else None
        except Exception:
            return None
    return None
def _walk(w, depth, maxd, varset, opts):
    node = {"name": w.get_name(), "class": w.get_class().get_name()}
    node["is_variable"] = w.get_name() in varset
    if isinstance(w, unreal.UserWidget):
        node["is_user_widget"] = True
    if opts.get("text"):
        t = _txt(w)
        if t is not None:
            node["text"] = t[:200]
    if opts.get("slots"):
        si = _slot_info(w)
        if si:
            node["slot"] = si
    if isinstance(w, unreal.PanelWidget):
        if depth >= maxd:
            try:
                node["children_truncated"] = w.get_children_count()
            except Exception:
                node["children_truncated"] = None
        else:
            kids = []
            try:
                n = w.get_children_count()
            except Exception:
                n = 0
            for i in range(n):
                try:
                    c = w.get_child_at(i)
                except Exception:
                    c = None
                if c is not None:
                    kids.append(_walk(c, depth + 1, maxd, varset, opts))
            node["children"] = kids
    return node
def _count_nodes(node):
    total = 1
    for c in node.get("children", []) or []:
        total += _count_nodes(c)
    return total
'''

    # ------------------------------------------------------------------ #
    # list_widget_blueprints — AssetRegistry WidgetBlueprint assets       #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _WIDGET_HELPERS + r'''
package_path = PARAMS.get("path")
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
include_subclasses = bool(PARAMS.get("include_subclasses"))
name_filter = PARAMS.get("filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
seen = set()
def _add(pkg, nm, acls):
    full = pkg + "." + nm
    if full in seen:
        return
    if name_filter and not _match(nm, name_filter):
        return
    seen.add(full)
    rows.append({"name": nm, "path": full, "package": pkg, "asset_class": acls})
used = "class_paths(/Script/UMGEditor.WidgetBlueprint)"
try:
    tlap = unreal.TopLevelAssetPath("/Script/UMGEditor", "WidgetBlueprint")
    kwargs = {"class_paths": [tlap], "recursive_paths": recursive}
    if package_path:
        kwargs["package_paths"] = [package_path]
    assets = ar.get_assets(unreal.ARFilter(**kwargs))
except Exception as _e:
    assets = []
    used = "class_paths-failed:%s" % _e
if include_subclasses or not assets:
    # widen via Blueprint recursive_classes and keep assets whose class name contains 'Widget'
    # (catches WidgetBlueprint + EditorUtilityWidgetBlueprint). Fallback if class_paths yields 0.
    used = used + " + Blueprint-scan(Widget*)"
    try:
        kw2 = {"class_names": ["Blueprint"], "recursive_classes": True}
        if package_path:
            kw2["package_paths"] = [package_path]; kw2["recursive_paths"] = recursive
        a2 = ar.get_assets(unreal.ARFilter(**kw2))
    except Exception:
        a2 = []
    for a in a2:
        acp = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
        if "Widget" in acp and (include_subclasses or acp == "WidgetBlueprint"):
            _add(str(a.package_name), str(a.asset_name), acp)
for a in assets:
    acp = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
    _add(str(a.package_name), str(a.asset_name), acp)
rows.sort(key=lambda r: (r["package"].lower(), r["name"].lower()))
total = len(rows)
by_class = {}
for r in rows:
    by_class[r["asset_class"]] = by_class.get(r["asset_class"], 0) + 1
if max_results:
    rows = rows[:int(max_results)]
print("@@UMCP@@" + json.dumps({"status": "success",
    "path": package_path or "(all mounted roots)", "recursive": recursive,
    "total": total, "returned": len(rows), "count_by_class": by_class,
    "widget_blueprints": rows, "source": used,
    "note": "Enumerated via AssetRegistry class_paths TopLevelAssetPath(/Script/UMGEditor, "
            "WidgetBlueprint). include_subclasses=True also lists EditorUtilityWidgetBlueprint "
            "etc. via a Blueprint recursive_classes scan. No asset is loaded."}))
'''

    @mcp.tool()
    def list_widget_blueprints(ctx, path: str = None, recursive: bool = True,
                               include_subclasses: bool = False, filter: str = None,
                               max_results: int = None) -> str:
        """List UMG WidgetBlueprint assets via the AssetRegistry (fast; no asset load). Read-only.

        path:               content root to scan (e.g. '/Game', '/Game/Variant_Combat/UI');
                            default scans all mounted roots.
        recursive:          recurse into sub-paths (default True).
        include_subclasses: also list WidgetBlueprint subclasses like EditorUtilityWidgetBlueprint
                            (default False: exact WidgetBlueprint only).
        filter:             case-insensitive substring/glob on the asset name.
        max_results:        cap the number returned (the 'total'/'count_by_class' still reflect all).

        Returns total, count_by_class, and rows of {name, path, package, asset_class}."""
        params = {"path": path, "recursive": recursive, "include_subclasses": include_subclasses,
                  "filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_widget_blueprint_info — parent, tree, named widgets, anims      #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _WIDGET_HELPERS + r'''
path = PARAMS.get("path")
max_depth = int(PARAMS.get("max_depth") or 40)
wb, err = _load_wb(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(wb)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    props = _meta_props(cdo)
    wt = _widget_tree(wb)
    varmap = _var_widgets(wb, wt, gname, props)
    varset = set(varmap.keys())
    # parent class
    p_short = None; p_full = None
    try:
        pc = _BEL.get_blueprint_parent_class(wb)
        if pc is not None:
            p_short = pc.get_name(); p_full = pc.get_path_name()
    except Exception:
        pass
    # named/variable widgets listing
    named = []
    for nm, w in varmap.items():
        named.append({"name": nm, "widget_class": w.get_class().get_name(),
                      "is_user_widget": isinstance(w, unreal.UserWidget)})
    named.sort(key=lambda r: r["name"].lower())
    # full widget tree (bootstrap from variable widgets -> root -> recurse)
    opts = {"slots": True, "text": True}
    tree = None; tree_reachable = False; roots = []; widget_count = None
    seen_roots = {}
    for nm, w in varmap.items():
        r = _root_from(w)
        seen_roots[r.get_name()] = r
    if seen_roots:
        tree_reachable = True
        troots = list(seen_roots.values())
        if len(troots) == 1:
            tree = _walk(troots[0], 0, max_depth, varset, opts)
            widget_count = _count_nodes(tree)
        else:
            trees = [_walk(r, 0, max_depth, varset, opts) for r in troots]
            tree = {"multiple_roots": True, "roots": trees}
            widget_count = sum(_count_nodes(t) for t in trees)
        roots = [r.get_name() for r in troots]
    # graph names (same source as blueprints_read)
    try:
        graphs = [str(x) for x in _BEL.list_graph_names(wb)]
    except Exception:
        graphs = None
    # animations: protected -> not reachable
    anim_reachable = False
    try:
        wb.get_editor_property("animations"); anim_reachable = True
    except Exception:
        anim_reachable = False
    result = {"status": "success",
              "widget_blueprint": {"name": wb.get_name(), "path": wb.get_path_name()},
              "asset_class": wb.get_class().get_name(),
              "generated_class": gname,
              "parent_class": p_short, "parent_class_path": p_full,
              "root_widget": (roots[0] if len(roots) == 1 else roots),
              "widget_count": widget_count,
              "named_widgets": named,
              "named_widget_count": len(named),
              "widget_tree": tree,
              "tree_reachable": tree_reachable,
              "graphs": graphs,
              "animations_reachable": anim_reachable,
              "animations": None,
              "note": "Widget tree is reconstructed from Python: find_object(WidgetBlueprint, "
                      "WidgetTree) then a variable widget bootstraps into the tree; get_parent() "
                      "finds the RootWidget and UPanelWidget.get_child_at() recurses down (covers "
                      "non-variable widgets too). named_widgets are 'Is Variable'/BindWidget widgets "
                      "(UPROPERTYs on the generated class). LIMITS: WidgetBlueprint.Animations is a "
                      "protected UPROPERTY and animation names do not surface as UPROPERTYs -> NOT "
                      "reachable from stock Python (animations_reachable=false). If a WidgetBlueprint "
                      "has zero variable widgets there is no Python-reachable entry point -> "
                      "tree_reachable=false (RootWidget itself is protected)."}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_widget_blueprint_info(ctx, path: str, max_depth: int = 40) -> str:
        """Introspect a UMG WidgetBlueprint (loads it; no UMG editor opened). Read-only.

        path:      object path, e.g. '/Game/Variant_Combat/UI/UI_LifeBar.UI_LifeBar'.
        max_depth: max widget-tree recursion depth (default 40).

        Returns the parent class (name+path), generated class, the root widget name, the FULL
        widget tree (nested {name, class, is_variable, slot, text, children} for every widget,
        including non-variable ones), the named/variable widgets (name + widget_class), the total
        widget_count, and graph names. HONEST LIMITS surfaced in the payload: animations are a
        protected UPROPERTY and NOT reachable from Python (animations_reachable=false); a
        WidgetBlueprint with no variable widgets has no reachable tree entry point
        (tree_reachable=false)."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"path": path, "max_depth": max_depth}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_widget_hierarchy — focused nested parent->children layout tree #
    # ------------------------------------------------------------------ #
    _HIER_BODY = _WIDGET_HELPERS + r'''
path = PARAMS.get("path")
max_depth = int(PARAMS.get("max_depth") or 40)
include_slots = PARAMS.get("include_slots")
include_slots = True if include_slots is None else bool(include_slots)
include_text = PARAMS.get("include_text")
include_text = True if include_text is None else bool(include_text)
wb, err = _load_wb(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(wb)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    props = _meta_props(cdo)
    wt = _widget_tree(wb)
    varmap = _var_widgets(wb, wt, gname, props)
    varset = set(varmap.keys())
    seen_roots = {}
    for nm, w in varmap.items():
        r = _root_from(w)
        seen_roots[r.get_name()] = r
    opts = {"slots": include_slots, "text": include_text}
    if not seen_roots:
        print("@@UMCP@@" + json.dumps({"status": "partial",
            "widget_blueprint": {"name": wb.get_name(), "path": wb.get_path_name()},
            "tree_reachable": False, "hierarchy": None,
            "message": "No variable widget to bootstrap the tree; RootWidget is a protected "
                       "UPROPERTY so the hierarchy is not reachable from stock Python for this "
                       "WidgetBlueprint (needs a C++ UWidgetTree handler)."}))
    else:
        troots = list(seen_roots.values())
        if len(troots) == 1:
            hier = _walk(troots[0], 0, max_depth, varset, opts)
            count = _count_nodes(hier)
            roots = troots[0].get_name()
        else:
            trees = [_walk(r, 0, max_depth, varset, opts) for r in troots]
            hier = {"multiple_roots": True, "roots": trees}
            count = sum(_count_nodes(t) for t in trees)
            roots = [r.get_name() for r in troots]
        print("@@UMCP@@" + json.dumps({"status": "success",
            "widget_blueprint": {"name": wb.get_name(), "path": wb.get_path_name()},
            "root_widget": roots, "widget_count": count, "max_depth": max_depth,
            "tree_reachable": True, "hierarchy": hier,
            "note": "Nested layout tree (parent->children) via get_parent()/UPanelWidget."
                    "get_child_at(). Each node: name, class, is_variable, optional slot (class + "
                    "best-effort position/size/padding/alignment) and text (for text widgets). "
                    "Leaf/content widgets have no 'children' key; non-panel widgets never recurse."}))
'''

    @mcp.tool()
    def list_widget_hierarchy(ctx, path: str, max_depth: int = 40,
                              include_slots: bool = True, include_text: bool = True) -> str:
        """Get a WidgetBlueprint's nested widget layout tree (parent -> children). Read-only.

        path:          object path of the WidgetBlueprint asset.
        max_depth:     max recursion depth (default 40; deeper nodes report children_truncated).
        include_slots: include each widget's slot class + best-effort layout (position/size/
                       padding/alignment) (default True).
        include_text:  include visible text for text-bearing widgets (default True).

        A focused companion to get_widget_blueprint_info: just the hierarchy. Each node is
        {name, class, is_variable, [slot], [text], children}. Returns status 'partial' with
        tree_reachable=false if the WidgetBlueprint has no variable widget to bootstrap from
        (RootWidget is protected in Python)."""
        params = {"path": path, "max_depth": max_depth,
                  "include_slots": include_slots, "include_text": include_text}
        try:
            return json.dumps(_exec(_HIER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (writes; not implemented this batch): create_widget_blueprint,
    #   add/remove/reparent widget, set widget property, add/rename/remove
    #   animation, bind/unbind widget. These need the editor-only UMG authoring
    #   API (UWidgetBlueprint / WidgetTree editing / FWidgetBlueprintEditor) -> a
    #   C++ handler, plus ScopedEditorTransaction + per-session ledger inverse +
    #   recompile. No `undo` tool is defined here (read-only batch).
