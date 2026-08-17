"""UserTools :: Widgets / UMG (WRITE)  (spec: docs/spec/widgets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE-wave module, the
mutating counterpart to widgets_read.py. Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py, and the create-tool shape mirrors ai_write.py.

What this build exposes (NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * WidgetBlueprint created non-interactively via
      unreal.AssetToolsHelpers.get_asset_tools().create_asset(
          name, package_path, unreal.WidgetBlueprint, unreal.WidgetBlueprintFactory())
    unreal.WidgetBlueprintFactory IS exposed on unreal.* (supported_class=WidgetBlueprint). Its
    parent-class PICKER dialog only fires from ConfigureProperties(), which the scripting create_asset
    path never calls -> NO modal EVER pops (bounded-probed live: a single create returned promptly).
    We set the factory's `parent_class` UPROPERTY to a resolved UserWidget subclass UP FRONT (default
    unreal.UserWidget) and `edit_after_new`=False so the UMG editor is never auto-opened. A fresh
    WidgetBlueprint has an EMPTY WidgetTree (RootWidget None) and generated class <Name>_C.

Implemented (validated live, session=agentA, editor left CLEAN, ledger depth 0):
  - create_widget_blueprint  (WRITE; ledgered generic op "create_asset"; inverse already in
                              editor_level.undo = close editors + delete asset [+ rmdir if we made the dir])

DEFERRED (editor-only in this build's Python surface; refused rather than shipping unrevertable ops) --
  Widget-TREE authoring (add/remove/reparent child widgets, set widget props, bindings, animations):
    Probed live and CONFIRMED unreachable from stock Python (matches widgets_read's documented limits):
      * UWidgetTree exposes NO construct_widget (the C++ UWidgetTree::ConstructWidget<T> template is
        not Python-exposed); its only method is the inherited Object.rename. There is no way to
        instantiate a UWidget that is properly outered/registered into the WidgetTree from Python.
      * UPanelWidget DOES expose add_child/insert_child/remove_child/clear_children, but add_child
        needs an already-tree-registered child UWidget to add (which requires the absent
        construct_widget); a bare unreal.new_object(UWidget) is not registered in WidgetTree.AllWidgets.
      * WidgetTree.RootWidget / AllWidgets and WidgetBlueprint.Animations are PROTECTED UPROPERTYs
        (get/set_editor_property refused) -> you cannot set the tree root or read/author animations.
      * Neither unreal.WidgetBlueprintLibrary nor unreal.WidgetBlueprintEditorLibrary is exposed in
        this build. This is the UMG WidgetBlueprint editor's job (UWidgetBlueprint / FWidgetBlueprintEditor
        / UWidgetTree editing) -> needs a C++ handler or the with-user editor wave. NOT attempted
        against the shared editor. No fake/unrevertable tree write is shipped.

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified `undo`).
It reuses the coordinator's already-folded generic "create_asset" inverse (close editors + delete_asset
[+ rmdir]); NO new ledger op is introduced, so there is nothing new for the coordinator to fold.
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
# success/user_code/code_obj (they are the C++ wrapper's own locals -> clobbering them wedges capture).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                    -> per-session undo stack (copied verbatim from editor_level.py).
    #   _close_editors(obj)          -> silently close any open asset editor for obj.
    #   _resolve_parent_class(spec)  -> (UClass, err): resolve a UserWidget subclass by friendly name
    #                                   (unreal.<Name>), /Script or generated-class path, or a
    #                                   WidgetBlueprint asset path; verify via CDO isinstance UserWidget.
    _WW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
def _is_userwidget_class(cls):
    # unreal.Class has no is_child_of in this build -> verify via the class default object.
    try:
        cdo = unreal.get_default_object(cls)
        return isinstance(cdo, unreal.UserWidget)
    except Exception:
        return None
def _resolve_parent_class(spec):
    if not spec:
        return unreal.UserWidget.static_class(), None
    cand = None
    # 1) friendly type name on the unreal.* module (e.g. "UserWidget")
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
    # 2) a class path (/Script/...Class or a generated-class object path)
    if cand is None:
        try:
            c = unreal.load_class(None, str(spec))
            if isinstance(c, unreal.Class):
                cand = c
        except Exception:
            cand = None
    # 3) a WidgetBlueprint asset path -> its generated class
    if cand is None:
        try:
            a = unreal.EditorAssetLibrary.load_asset(str(spec))
            if a is not None:
                g = unreal.BlueprintEditorLibrary.generated_class(a)
                if isinstance(g, unreal.Class):
                    cand = g
        except Exception:
            cand = None
    if cand is None:
        return None, "could not resolve parent_class '%s' to a UClass (tried unreal.<name>, load_class, WidgetBlueprint asset path)" % spec
    chk = _is_userwidget_class(cand)
    if chk is False:
        return None, "parent_class '%s' (%s) is not a UserWidget subclass" % (spec, cand.get_name())
    return cand, None
'''

    # ------------------------------------------------------------------ #
    # create_widget_blueprint — non-interactive WidgetBlueprint creation  #
    # ------------------------------------------------------------------ #
    _CREATE_WBP_BODY = _WW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
parent_spec = PARAMS.get("parent_class") or "UserWidget"
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
pcls, perr = _resolve_parent_class(parent_spec)
if perr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": perr}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    wb = None
    # NOTE: asset creation must NOT be wrapped in a ScopedEditorTransaction (PROTOCOL #5b) -- that
    # traps the new asset in the editor transaction buffer and blocks a later delete. Creation is
    # ledgered via the generic create_asset op instead. WidgetBlueprintFactory is non-modal on the
    # scripting create_asset path; we set parent_class up front and edit_after_new=False so no picker
    # and no auto-opened UMG editor.
    fac = unreal.WidgetBlueprintFactory()
    try:
        fac.set_editor_property("parent_class", pcls)
    except Exception:
        pass
    try:
        fac.set_editor_property("edit_after_new", False)
    except Exception:
        pass
    wb = at.create_asset(name, package_path, unreal.WidgetBlueprint, fac)
    if wb is None or not isinstance(wb, unreal.WidgetBlueprint):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(wb).__name__, asset_path)}))
    else:
        _close_editors(wb)
        # Save the new asset to disk (PROTOCOL #5c): persists it AND makes the create_asset undo-delete
        # reliable (a freshly-created unsaved asset resists the immediately-following delete = settle
        # timing; editor_level.undo also runs a follow-up separate-call sweep for that edge).
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        # Read back for proof.
        gname = None
        try:
            gc = unreal.BlueprintEditorLibrary.generated_class(wb)
            gname = gc.get_name() if gc else None
        except Exception:
            gname = None
        pshort = None; pfull = None
        try:
            pc = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(wb)
            if pc is not None:
                pshort = pc.get_name(); pfull = pc.get_path_name()
        except Exception:
            pass
        wt_found = False
        try:
            wt = unreal.find_object(wb, "WidgetTree")
            wt_found = wt is not None
        except Exception:
            wt_found = False
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": wb.get_name(),
            "asset_path": asset_path, "object_path": wb.get_path_name(),
            "class": wb.get_class().get_name(),
            "parent_class": pshort, "parent_class_path": pfull,
            "generated_class": gname,
            "widget_tree_found": wt_found, "widget_tree_empty": True,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_widget_blueprint(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                                parent_class: str = "UserWidget") -> str:
        """Create a new UMG WidgetBlueprint asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new WidgetBlueprint (e.g. 'WBP_MainMenu').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be
                      under a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate
                      folders are created as needed.
        parent_class: UserWidget subclass to parent the widget to (default 'UserWidget'). Accepts a
                      friendly engine type name (e.g. 'UserWidget'), a class path
                      ('/Script/UMG.UserWidget' or a generated-class object path), or the object path
                      of an existing UserWidget-derived WidgetBlueprint (its generated class is used).
                      Validated to be a UserWidget subclass before creation.

        Uses AssetTools.create_asset(..., unreal.WidgetBlueprint, unreal.WidgetBlueprintFactory()) with
        the factory's parent_class set up front and edit_after_new=False. The factory's parent-class
        PICKER only fires from ConfigureProperties(), which the scripting create_asset path never
        calls -> no modal, and the UMG editor is never auto-opened. The new WidgetBlueprint has an
        EMPTY widget tree (no root widget); inspect with widgets_read.get_widget_blueprint_info.
        Widget-TREE authoring (adding child widgets, props, bindings, animations) is editor-only in
        this build and is NOT provided here (see module docstring / DEFERRED).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already in
        editor_level.undo): close any editors for the asset, delete it, and remove package_path if we
        created it and it is now empty (the same generic inverse used by create_blueprint/create_blackboard)."""
        params = {"name": name, "package_path": package_path, "parent_class": parent_class}
        try:
            return json.dumps(_exec(_CREATE_WBP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_widget / remove_widget — widget-TREE authoring (via C++ #8)      #
    # ------------------------------------------------------------------ #
    # ENABLED 2026-08-15 by C++ #8 (MCPReflectionLibrary.AddWidgetToBlueprint / RemoveWidgetFromBlueprint).
    # Probe found construct+panel.add_child ARE Python-reachable but WidgetTree.RootWidget is not settable
    # from Python -> the C++ handler does construct + root + child add/remove AND keeps the compiler's
    # WidgetVariableNameToGuidMap in sync (OnVariableAdded/Removed + transient-rename). LESSON: verify via
    # the Output-Log delta (_query surfaces _log_warnings) not just the JSON return -- a bad write can
    # trip self-healing UMG-compiler ensures that a return-value/readback check would miss.
    _ADD_WIDGET_BODY = _WW_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_class_path = PARAMS["widget_class_path"]
widget_name = PARAMS["name"]; parent_name = PARAMS.get("parent_name") or ""
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
wb = EAL.load_asset(wbp_path)
if wb is None or not isinstance(wb, unreal.WidgetBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a WidgetBlueprint: %s" % wbp_path}))
elif mrl is None or not hasattr(mrl, "add_widget_to_blueprint"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_widget_to_blueprint unavailable (plugin DLL predates C++ #8; recompile needed)"}))
else:
    res = json.loads(mrl.add_widget_to_blueprint(wb, widget_class_path, widget_name, parent_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("added"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "AddWidgetToBlueprint returned not-added for '%s'" % widget_name}))
    else:
        try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
        except Exception: pass
        try: EAL.save_asset(wbp_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "add_widget", "wbp_path": wbp_path, "name": res.get("name") or widget_name})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": res.get("blueprint"),
            "added_widget": res.get("name") or widget_name, "is_root": res.get("is_root"),
            "parent": res.get("parent"), "class_path": widget_class_path, "ledger_depth": len(_ledger())}))
'''

    _REMOVE_WIDGET_BODY = _WW_HELPERS + r'''
wbp_path = PARAMS["wbp_path"]; widget_name = PARAMS["name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
wb = EAL.load_asset(wbp_path)
if wb is None or not isinstance(wb, unreal.WidgetBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a WidgetBlueprint: %s" % wbp_path}))
elif mrl is None or not hasattr(mrl, "remove_widget_from_blueprint"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_widget_from_blueprint unavailable (plugin DLL predates C++ #8; recompile needed)"}))
else:
    # Capture the widget's class + parent BEFORE removal for a best-effort undo (children of a removed
    # panel are NOT recoverable). Uses the read-side find_object walk.
    cls_path = None; parent_name = ""
    try:
        wt = unreal.find_object(wb, "WidgetTree")
        w = unreal.find_object(wt, widget_name) if wt is not None else None
        if w is not None:
            cls_path = w.get_class().get_path_name()
            par = w.get_parent()
            parent_name = par.get_name() if par is not None else ""
    except Exception:
        pass
    res = json.loads(mrl.remove_widget_from_blueprint(wb, widget_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("found"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "widget not found: %s" % widget_name}))
    else:
        try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
        except Exception: pass
        try: EAL.save_asset(wbp_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "remove_widget", "wbp_path": wbp_path, "name": widget_name,
                          "widget_class_path": cls_path, "parent_name": parent_name})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": res.get("blueprint"),
            "removed_widget": widget_name, "captured_class": cls_path, "removed": res.get("removed"),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_widget(ctx, wbp_path: str, widget_class_path: str, name: str, parent_name: str = "") -> str:
        """Add a widget to a WidgetBlueprint's tree. REQUIRES the C++ #8 handler
        (unreal.MCPReflectionLibrary.add_widget_to_blueprint); returns a clear error on an older DLL.

        wbp_path:          object/package path of the WidgetBlueprint asset.
        widget_class_path: class path of the widget to add, e.g. '/Script/UMG.CanvasPanel',
                           '/Script/UMG.TextBlock', '/Script/UMG.Button', '/Script/UMG.VerticalBox'.
        name:              name for the new widget.
        parent_name:       name of the parent PANEL widget to nest under. Leave empty ("") to set the new
                           widget as the tree ROOT (only valid on an empty tree) or to add under the root
                           panel. Saved + compiled after.

        Verify with widgets_read.get_widget_blueprint_info / list_widget_hierarchy (and this tool surfaces
        any UMG-compiler warnings via `_log_warnings`). Ledgered write op 'add_widget' {wbp_path, name}.
        Inverse (in editor_level.undo): remove_widget_from_blueprint(name) -> FAITHFUL."""
        params = {"wbp_path": wbp_path, "widget_class_path": widget_class_path, "name": name,
                  "parent_name": parent_name}
        try:
            return json.dumps(_exec(_ADD_WIDGET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_widget(ctx, wbp_path: str, name: str) -> str:
        """Remove a widget (by name) from a WidgetBlueprint's tree (clears the root if it is the root).
        REQUIRES the C++ #8 handler. Saved + compiled after.

        wbp_path: object/package path of the WidgetBlueprint asset.
        name:     name of the widget to remove.

        Ledgered write op 'remove_widget' {wbp_path, name, widget_class_path, parent_name} (class + parent
        captured before removal). Inverse (in editor_level.undo): add_widget_to_blueprint(class, name,
        parent) -- BEST-EFFORT: it re-adds the single widget of the same class/name/parent, but any CHILD
        widgets of a removed panel are NOT restored (they're gone with the parent). Use undo right after."""
        params = {"wbp_path": wbp_path, "name": name}
        try:
            return json.dumps(_exec(_REMOVE_WIDGET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
