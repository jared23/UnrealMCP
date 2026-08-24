"""UserTools :: WIDGET UI-COMPONENTS "W-E" batch -- attach/detach/list UUIComponents on a WBP (C++-backed).

DRAFT wiring for the UMG/UMGEditor C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_WidgetUIComp.cpp. Every tool here is hasattr-guarded
on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin DLL is rebuilt with
those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS
injection, Output-Log auto-capture, per-session undo ledger, compile+save finalize) is copied VERBATIM from
the sibling widget_edit_cpp.py (the W-B batch).

WHAT A "UI COMPONENT" IS (UE 5.8 BETA feature):
  A UUIComponent subclass (e.g. UNavigationUIComponent) attached to a single UMG widget inside a
  WidgetBlueprint. The attachment is authored on an editor-only UUIComponentWidgetBlueprintExtension hung off
  the WBP; at compile time it is duplicated into a UUIComponentWidgetBlueprintGeneratedClassExtension on the
  generated class, so it ships at runtime. None of this is python-reachable -- the container + extension are
  UMG/UMGEditor C++ (UUIComponentContainer::AddComponent/RemoveComponent/ForEachComponent), so this batch is
  C++-only. The C++ reuses FUIComponentUtils::AddComponent/RemoveComponent (the same entry point the UMG
  Designer uses) with a NULL FWidgetBlueprintEditor* -> the persistent asset-level path with the transient
  live-preview mirror skipped.

TOOLS (3):
  WRITES (ledgered):
    * add_ui_component     -- attach a UUIComponent subclass to a named widget (FUIComponentUtils::AddComponent).
    * remove_ui_component  -- detach a UUIComponent (by class) from a named widget (FUIComponentUtils::RemoveComponent).
  READ (no ledger):
    * list_ui_components   -- enumerate every authored (widget_name, component_class) on the WBP.

Each WRITE finalizes with a compile + save (the C++ marks the blueprint structurally-modified; the compile
regenerates the widget class + duplicates the container onto the generated class, and the Output-Log capture
surfaces any UMG-compiler ensure -- a plain readback can miss those). Undo: this module registers NO own
'undo' tool (editor_level.py owns the ONE unified undo). Each write appends its inverse op to the shared
per-session ledger; the coordinator folds the matching elif branches into editor_level.undo. LEDGER OPS +
INVERSES (report to the coordinator):

  uicomp_add    {asset_path, widget_name, component_class}
        inverse = remove_ui_component_json(asset_path, widget_name, component_class).           FAITHFUL (structure).
  uicomp_remove {asset_path, widget_name, component_class}
        inverse = add_ui_component_json(asset_path, widget_name, component_class). LOSSY: any authored
        property values on the removed archetype are not preserved -- the re-add creates a fresh default
        component (documented; same shape as widget_replace's lossy inverse).

component_class in BOTH ledger entries is the CONCRETE class path the C++ resolved/removed (res['component_class']),
never a base class the caller may have passed -- so the inverse targets the exact class.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from widget_edit_cpp.py) ----------
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
            "(deferred to the WIDGET UI-COMPONENTS W-E batch). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_WidgetUIComp.cpp to enable it.")}
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
    # WRITES (ledgered)
    # ================================================================== #

    _ADD_BODY = _HELP + r'''
rl = _mrl("add_ui_component_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_ui_component_json")))
else:
    res = _decode(rl.add_ui_component_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["component_class"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "uicomp_add", "asset_path": PARAMS["asset_path"],
            "widget_name": res.get("widget", PARAMS["widget_name"]),
            "component_class": res.get("component_class", PARAMS["component_class"])})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def add_ui_component(ctx, asset_path: str, widget_name: str, component_class: str) -> str:
        """Attach a UI component (a UUIComponent subclass) to a named widget in a WidgetBlueprint (C++, ledgered).

        asset_path:      WidgetBlueprint asset path (e.g. '/Game/UI/WBP_Foo.WBP_Foo').
        widget_name:     the target widget in the tree the component attaches to (must already exist).
        component_class: the UUIComponent subclass path to attach (e.g. '/Script/UMG.NavigationUIComponent',
                         or a BP component '/Game/UI/BP_MyComp.BP_MyComp_C'). Must be concrete (not abstract);
                         adding a base/derived of an already-present component on the same widget is rejected.

        UI components (UE 5.8 beta) are authored on the editor-only UUIComponentWidgetBlueprintExtension via
        FUIComponentUtils::AddComponent (the UMG Designer's own path, driven headless with a null editor); the
        subsequent compile duplicates the container onto the generated class so it ships at runtime. Ledgers op
        'uicomp_add'; inverse = remove the component. UMG/UMGEditor-only; inert until the plugin DLL is rebuilt
        with MCPReflection_WidgetUIComp.cpp."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "component_class": component_class}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_BODY = _HELP + r'''
rl = _mrl("remove_ui_component_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_ui_component_json")))
else:
    res = _decode(rl.remove_ui_component_json(PARAMS["asset_path"], PARAMS["widget_name"], PARAMS["component_class"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _compile_save(PARAMS["asset_path"])
        _ledger().append({"op": "uicomp_remove", "asset_path": PARAMS["asset_path"],
            "widget_name": res.get("widget", PARAMS["widget_name"]),
            "component_class": res.get("component_class", PARAMS["component_class"])})
        res["status"] = "success"; res["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def remove_ui_component(ctx, asset_path: str, widget_name: str, component_class: str) -> str:
        """Detach a UI component (by class) from a named widget in a WidgetBlueprint (C++, ledgered).

        asset_path:      WidgetBlueprint asset path.
        widget_name:     the widget the component is attached to.
        component_class: the UUIComponent subclass to remove (matched by IsChildOf; the exact concrete class is
                         resolved + captured so removal actually fires and the re-add is faithful).

        Uses FUIComponentUtils::RemoveComponent (null editor -> persistent path). Ledgers op 'uicomp_remove';
        inverse = re-add the captured concrete class. LOSSY: authored property values on the removed archetype
        are not preserved -- the inverse re-adds a fresh default component. UMG/UMGEditor-only; inert until the
        plugin DLL is rebuilt with MCPReflection_WidgetUIComp.cpp."""
        params = {"asset_path": asset_path, "widget_name": widget_name, "component_class": component_class}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # READ (no ledger)
    # ================================================================== #

    _LIST_BODY = _HELP + r'''
rl = _mrl("list_ui_components_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_ui_components_json")))
else:
    res = _decode(rl.list_ui_components_json(PARAMS["asset_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_ui_components(ctx, asset_path: str) -> str:
        """List every UI component authored on a WidgetBlueprint (READ; C++).

        asset_path: WidgetBlueprint asset path.

        Returns {blueprint, has_extension, component_count, components:[{widget_name, component_class,
        component_var}]}. Walks the widget tree and reads each widget's components via the public
        Extension->GetComponentsFor. NOTE: orphaned components (target widget deleted, not yet compiled away)
        are omitted. Read-only; no ledger. Inert until the plugin DLL is rebuilt with
        MCPReflection_WidgetUIComp.cpp."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
