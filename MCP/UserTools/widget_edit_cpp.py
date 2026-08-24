"""UserTools :: WIDGETS (UMG) "W-B" batch -- widget-tree ops + named slots + property bindings (C++-backed).

DRAFT wiring for the UMG/UMGEditor C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Widgets.cpp. Every tool here is hasattr-guarded on
a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin DLL is rebuilt with
those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS
injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from the gold-standard
niagara_runtime_cpp.py.

WHY C++ (these ops are not python-reachable):
  * UWidgetTree::RootWidget    -- set_editor_property refuses it (only C++ can point the tree root).
  * UWidget::bIsVariable       -- a UPROPERTY() uint8:1 bitfield with no Edit flags -> set refused.
  * UWidgetBlueprint::Bindings -- editor-only TArray<FDelegateEditorBinding>, not a python struct array.
  * INamedSlotInterface        -- UINTERFACE(CannotImplementInterfaceInBlueprint), a pure C++ interface.
  * FWidgetBlueprintEditorUtils::ReplaceWidgets -- the asset-level overload (no live editor tab).

TOOLS (12):
  TREE OPS (writes, ledgered):
    * rename_widget            -- rename a widget + carry its BP variable (OnVariableRenamed).
    * set_root_widget          -- point WidgetTree->RootWidget at a named widget.
    * set_widget_is_variable   -- flip bIsVariable (+ OnVariableAdded/Removed).
    * replace_widget           -- swap a widget's class (ReplaceWidgets, MaintainNameAndReferences).
    * wrap_widget              -- construct a panel + reparent the target under it.
  NAMED SLOTS:
    * list_named_slots         -- READ: INamedSlotInterface::GetSlotNames.
    * get_named_slot_content   -- READ: GetContentForSlot.
    * set_named_slot_content   -- WRITE: SetContentForSlot (captures prior).
    * clear_named_slot         -- WRITE: SetContentForSlot(name, None) (captures prior).
  PROPERTY BINDINGS:
    * add_property_binding     -- WRITE: append/overwrite an FDelegateEditorBinding.
    * remove_property_binding  -- WRITE: remove a binding (captures for re-add).
    * list_property_bindings   -- READ: the Bindings array.

Each WRITE finalizes with a compile + save (the C++ marks the blueprint structurally-modified; the compile
regenerates the widget class and the Output-Log capture surfaces any UMG-compiler ensure -- a plain readback
can miss those). Undo: this module registers NO own 'undo' tool (editor_level.py owns the ONE unified undo).
Each write appends its inverse op to the shared per-session ledger; the coordinator folds the matching elif
branches into editor_level.undo. LEDGER OPS + INVERSES (report to the coordinator):

  widget_rename         {asset_path, old_name, new_name}
        inverse = rename_widget_json(asset_path, new_name, old_name).                       FAITHFUL.
  widget_set_root       {asset_path, widget_name, prev_root, had_prev_root}
        inverse = if had_prev_root: set_root_widget_json(asset_path, prev_root); else LOSSY
        (the empty-tree pre-state cannot be restored via set_root -- leaves the new root; documented).
  widget_set_is_variable{asset_path, widget_name, prev_is_variable}
        inverse = set_widget_is_variable_json(asset_path, widget_name, prev_is_variable).    FAITHFUL.
  widget_replace        {asset_path, widget_name, old_class}
        inverse = replace_widget_json(asset_path, widget_name, old_class). LOSSY for non-class-compatible
        props (ReplaceWidgets only transfers compatible props; documented).
  widget_wrap           {asset_path, panel_name, child_name, was_root, parent_name, child_index}
        inverse = UNWRAP compound (reparent child out, then remove the now-empty panel):
          load wb; child = _ww_find_widget(wb, child_name); panel = _ww_find_widget(wb, panel_name);
          if child and panel: panel.remove_child(child);
            if was_root: mrl.set_root_widget_json(asset_path, child_name)      # RootWidget not python-settable
            elif parent_name: p = _ww_find_widget(wb, parent_name); p.insert_child_at(child_index, child)
          mrl.remove_widget_from_blueprint(wb, panel_name)  # panel now childless -> drops just the panel
          then compile + save.                                                              FAITHFUL (hierarchy).
  widget_set_named_slot {asset_path, host_name, slot_name, prev_content, had_prev_content}
        inverse = if had_prev_content: set_named_slot_content_json(host, slot, prev_content)
                  else clear_named_slot_json(host, slot).                                    FAITHFUL.
  widget_clear_named_slot{asset_path, host_name, slot_name, prev_content, had_prev_content}
        inverse = if had_prev_content: set_named_slot_content_json(host, slot, prev_content) else no-op. FAITHFUL.
  widget_add_binding    {asset_path, widget_name, property_name, had_existing, prev_function}
        inverse = if had_existing: add_property_binding_json(widget, property, prev_function)
                  else remove_property_binding_json(widget, property).                       FAITHFUL.
  widget_remove_binding {asset_path, widget_name, property_name, prev_function}
        inverse = add_property_binding_json(widget, property, prev_function).                FAITHFUL.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py) -----
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _HELP = r'''
import unreal, json, builtins, gc
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ UMG/UMGEditor handler "
            "(deferred to the WIDGETS W-B batch). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Widgets.cpp to enable it.")}
def _compile_save(path):
    try:
        wb = unreal.EditorAssetLibrary.load_asset(path)
        if wb is not None:
            unreal.BlueprintEditorLibrary.compile_blueprint(wb)
    except Exception:
        pass
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
'''

    # ================================================================== #
    # TREE OPS
    # ================================================================== #

    _RENAME_BODY = _HELP + r'''
rl = _mrl("rename_widget_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("rename_widget_json")))
else:
    res = _decode(rl.rename_widget_json(PARAMS["asset_path"], PARAMS["old_name"], PARAMS["new_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_rename", "asset_path": PARAMS["asset_path"],
            "old_name": res.get("old_name", PARAMS["old_name"]), "new_name": res.get("new_name", PARAMS["new_name"])})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def rename_widget(ctx, asset_path: str, old_name: str, new_name: str) -> str:
        """Rename a widget in a WidgetBlueprint's tree, carrying its BP variable + bindings (C++, ledgered).

        asset_path: WidgetBlueprint asset path (e.g. '/Game/UI/WBP_Foo.WBP_Foo').
        old_name:   current widget name.
        new_name:   new widget name (must be unique in the tree).

        Renames the template UObject (OnVariableRenamed keeps the compiler's WidgetVariableNameToGuidMap in
        sync) and fixes up desired-focus + property-binding ObjectName references. Ledgers op 'widget_rename';
        inverse = rename back. UMG/UMGEditor-only; inert until the plugin DLL is rebuilt with
        MCPReflection_Widgets.cpp."""
        params = {"asset_path": asset_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_ROOT_BODY = _HELP + r'''
rl = _mrl("set_root_widget_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_root_widget_json")))
else:
    res = _decode(rl.set_root_widget_json(PARAMS["asset_path"], PARAMS["widget_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_set_root", "asset_path": PARAMS["asset_path"],
            "widget_name": PARAMS["widget_name"], "prev_root": res.get("prev_root", ""),
            "had_prev_root": bool(res.get("had_prev_root"))})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def set_root_widget(ctx, asset_path: str, widget_name: str) -> str:
        """Point the WidgetTree's RootWidget at a named widget already in the tree (C++, ledgered).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the widget to make the tree root (must already exist in the tree).

        RootWidget is python-refused (set_editor_property fails), so this is C++-only. Captures the prior root
        name. Ledgers op 'widget_set_root'; inverse restores the prior root (LOSSY only when the tree
        previously had no root). UMG-only; inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_SET_ROOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_ISVAR_BODY = _HELP + r'''
rl = _mrl("set_widget_is_variable_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_widget_is_variable_json")))
else:
    res = _decode(rl.set_widget_is_variable_json(PARAMS["asset_path"], PARAMS["widget_name"], bool(PARAMS["is_variable"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_set_is_variable", "asset_path": PARAMS["asset_path"],
            "widget_name": PARAMS["widget_name"], "prev_is_variable": bool(res.get("prev_is_variable"))})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def set_widget_is_variable(ctx, asset_path: str, widget_name: str, is_variable: bool) -> str:
        """Set a widget's bIsVariable flag -- expose (or hide) it as a BP variable (C++, ledgered).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the widget to flag.
        is_variable: True to expose it as a variable, False to hide it.

        bIsVariable is a UPROPERTY() bitfield with no Edit flags -> python-refused. Toggling on calls
        OnVariableAdded, off calls OnVariableRemoved (keeps the GUID map in sync). Captures the prior value.
        Ledgers op 'widget_set_is_variable'; inverse restores the prior flag. UMG-only; inert until rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "is_variable": is_variable}
        try:
            return json.dumps(_exec(_SET_ISVAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REPLACE_BODY = _HELP + r'''
rl = _mrl("replace_widget_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("replace_widget_json")))
else:
    res = _decode(rl.replace_widget_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["new_class"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_replace", "asset_path": PARAMS["asset_path"],
            "widget_name": res.get("widget", PARAMS["widget_name"]), "old_class": res.get("old_class", "")})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def replace_widget(ctx, asset_path: str, widget_name: str, new_class: str) -> str:
        """Replace a widget with a new class, keeping its name + references (C++, ledgered).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the widget to replace.
        new_class:   the new widget class path (e.g. '/Script/UMG.Button', '/Script/UMG.VerticalBox').

        Uses the asset-level FWidgetBlueprintEditorUtils::ReplaceWidgets (MaintainNameAndReferences) -- no live
        editor tab required. CAVEAT: if the widget is referenced as a variable in the event graph, the engine
        pops a confirmation MODAL; verify against a graph-unreferenced widget. Captures the old class. Ledgers
        op 'widget_replace'; inverse replaces back (LOSSY for non-class-compatible props). Inert until rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "new_class": new_class}
        try:
            return json.dumps(_exec(_REPLACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _WRAP_BODY = _HELP + r'''
rl = _mrl("wrap_widget_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("wrap_widget_json")))
else:
    res = _decode(rl.wrap_widget_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["panel_class"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_wrap", "asset_path": PARAMS["asset_path"],
            "panel_name": res.get("panel_name"), "child_name": res.get("target", PARAMS["widget_name"]),
            "was_root": bool(res.get("was_root")), "parent_name": res.get("parent", ""),
            "child_index": res.get("child_index")})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def wrap_widget(ctx, asset_path: str, widget_name: str, panel_class: str) -> str:
        """Wrap a widget inside a newly-constructed panel, reparenting it under the panel (C++, ledgered).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the widget to wrap.
        panel_class: the container panel class path (must be a UPanelWidget subclass, e.g.
                     '/Script/UMG.SizeBox', '/Script/UMG.Border', '/Script/UMG.VerticalBox').

        Constructs the panel, splices it into the target's parent slot (or makes it the new tree root if the
        target was root), then reparents the target under the panel. The new panel's variable GUID is
        registered (C++ #8 discipline). Ledgers op 'widget_wrap'; inverse = unwrap (reparent child out, then
        remove the empty panel). UMG-only; inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "panel_class": panel_class}
        try:
            return json.dumps(_exec(_WRAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # NAMED SLOTS
    # ================================================================== #

    _LIST_SLOTS_BODY = _HELP + r'''
rl = _mrl("list_named_slots_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_named_slots_json")))
else:
    res = _decode(rl.list_named_slots_json(PARAMS["asset_path"], PARAMS["widget_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_named_slots(ctx, asset_path: str, widget_name: str) -> str:
        """List the named slots a widget exposes via INamedSlotInterface (READ; C++).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the host widget (must implement INamedSlotInterface, e.g. a NamedSlot, or a UserWidget
                     with exposed named slots).

        Returns {widget, widget_class, slot_count, slot_names:[...]}. INamedSlotInterface is a pure C++
        interface (not python-reachable). Read-only; no ledger. Inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_LIST_SLOTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_SLOT_BODY = _HELP + r'''
rl = _mrl("get_named_slot_content_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_named_slot_content_json")))
else:
    res = _decode(rl.get_named_slot_content_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["slot_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_named_slot_content(ctx, asset_path: str, widget_name: str, slot_name: str) -> str:
        """Get the widget currently held in a named slot (READ; C++).

        asset_path:  WidgetBlueprint asset path.
        widget_name: the host widget implementing INamedSlotInterface.
        slot_name:   the named slot to read.

        Returns {widget, slot, has_content, content?, content_class?}. Read-only; no ledger. Inert until
        the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "slot_name": slot_name}
        try:
            return json.dumps(_exec(_GET_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_SLOT_BODY = _HELP + r'''
rl = _mrl("set_named_slot_content_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_named_slot_content_json")))
else:
    res = _decode(rl.set_named_slot_content_json(PARAMS["asset_path"], PARAMS["host_name"],
        PARAMS["slot_name"], PARAMS["content_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_set_named_slot", "asset_path": PARAMS["asset_path"],
            "host_name": res.get("host", PARAMS["host_name"]), "slot_name": res.get("slot", PARAMS["slot_name"]),
            "prev_content": res.get("prev_content", ""), "had_prev_content": bool(res.get("had_prev_content"))})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def set_named_slot_content(ctx, asset_path: str, host_name: str, slot_name: str, content_name: str) -> str:
        """Put an existing tree widget into a host's named slot (C++, ledgered).

        asset_path:   WidgetBlueprint asset path.
        host_name:    the host widget implementing INamedSlotInterface.
        slot_name:    the named slot to fill (must be one the host exposes).
        content_name: an existing widget in the tree to move into the slot (detached from its current parent).

        Captures the slot's prior content. Ledgers op 'widget_set_named_slot'; inverse restores the prior
        content (or clears the slot if it was empty). UMG-only; inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "host_name": host_name, "slot_name": slot_name,
                  "content_name": content_name}
        try:
            return json.dumps(_exec(_SET_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _CLEAR_SLOT_BODY = _HELP + r'''
rl = _mrl("clear_named_slot_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("clear_named_slot_json")))
else:
    res = _decode(rl.clear_named_slot_json(PARAMS["asset_path"], PARAMS["host_name"], PARAMS["slot_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_clear_named_slot", "asset_path": PARAMS["asset_path"],
            "host_name": res.get("host", PARAMS["host_name"]), "slot_name": res.get("slot", PARAMS["slot_name"]),
            "prev_content": res.get("prev_content", ""), "had_prev_content": bool(res.get("had_prev_content"))})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def clear_named_slot(ctx, asset_path: str, host_name: str, slot_name: str) -> str:
        """Clear a host's named slot -- set its content to nothing (C++, ledgered).

        asset_path: WidgetBlueprint asset path.
        host_name:  the host widget implementing INamedSlotInterface.
        slot_name:  the named slot to clear.

        Captures the slot's prior content (the widget is left in the tree, just unslotted). Ledgers op
        'widget_clear_named_slot'; inverse re-slots the prior content (no-op if the slot was already empty).
        UMG-only; inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path, "host_name": host_name, "slot_name": slot_name}
        try:
            return json.dumps(_exec(_CLEAR_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # PROPERTY BINDINGS
    # ================================================================== #

    _ADD_BINDING_BODY = _HELP + r'''
rl = _mrl("add_property_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_property_binding_json")))
else:
    res = _decode(rl.add_property_binding_json(PARAMS["asset_path"], PARAMS["widget_name"],
        PARAMS["property_name"], PARAMS["function_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_add_binding", "asset_path": PARAMS["asset_path"],
            "widget_name": res.get("widget", PARAMS["widget_name"]),
            "property_name": res.get("property", PARAMS["property_name"]),
            "had_existing": bool(res.get("had_existing")), "prev_function": res.get("prev_function", "")})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def add_property_binding(ctx, asset_path: str, widget_name: str, property_name: str,
                             function_name: str) -> str:
        """Bind a widget property to a BP function (append/overwrite an FDelegateEditorBinding; C++, ledgered).

        asset_path:    WidgetBlueprint asset path.
        widget_name:   the widget whose property is being bound (must be a variable widget on the UserWidget).
        property_name: the widget property to drive (e.g. 'Text', 'Visibility', 'ColorAndOpacity').
        function_name: the BP function that returns the property's value (must already exist on the blueprint).

        Bindings are keyed on (widget, property); an existing binding for that pair is overwritten (its prior
        function is captured). Ledgers op 'widget_add_binding'; inverse restores the prior binding, or removes
        the added one if there was none. NOTE: this writes the editor-side binding; a compile generates the
        wrapper -- an invalid function surfaces as a compile diagnostic in _log_warnings. Inert until rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name,
                  "property_name": property_name, "function_name": function_name}
        try:
            return json.dumps(_exec(_ADD_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_BINDING_BODY = _HELP + r'''
rl = _mrl("remove_property_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_property_binding_json")))
else:
    res = _decode(rl.remove_property_binding_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["property_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("found"):
        res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "widget_remove_binding", "asset_path": PARAMS["asset_path"],
            "widget_name": res.get("widget", PARAMS["widget_name"]),
            "property_name": res.get("property", PARAMS["property_name"]),
            "prev_function": res.get("prev_function", "")})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def remove_property_binding(ctx, asset_path: str, widget_name: str, property_name: str) -> str:
        """Remove a widget property binding (C++, ledgered).

        asset_path:    WidgetBlueprint asset path.
        widget_name:   the bound widget.
        property_name: the bound property to unbind.

        Captures the removed binding's function for a faithful re-add. Ledgers op 'widget_remove_binding'
        (only when a binding was actually found + removed); inverse re-adds it. UMG-only; inert until rebuilt."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "property_name": property_name}
        try:
            return json.dumps(_exec(_REMOVE_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_BINDINGS_BODY = _HELP + r'''
rl = _mrl("list_property_bindings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_property_bindings_json")))
else:
    res = _decode(rl.list_property_bindings_json(PARAMS["asset_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_property_bindings(ctx, asset_path: str) -> str:
        """List every property binding on a WidgetBlueprint (READ; C++).

        asset_path: WidgetBlueprint asset path.

        Returns {blueprint, binding_count, bindings:[{object_name, property_name, function_name,
        source_property, kind, member_guid}]}. Reads the editor-only Bindings array (not a python-reachable
        struct array). Read-only; no ledger. Inert until the plugin DLL is rebuilt."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_BINDINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
