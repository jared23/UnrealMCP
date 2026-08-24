"""UserTools :: UMG MODEL-VIEW-VIEWMODEL (MVVM) authoring (spec: widgets "W-D" batch)

DRAFT wiring for the C++ #39 widget-MVVM round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_WidgetMVVM.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers (AND the beta ModelViewViewModel plugin is enabled) -- at which point
each tool AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS injection, Output-Log auto-capture,
per-session undo ledger) is copied VERBATIM from the gold-standard niagara_runtime_cpp.py /
widget_anim_cpp.py.

Fills the spec MVVM features that stock Python cannot reach: the MVVM data model (UMVVMBlueprintView hanging
off a UMVVMWidgetBlueprintExtension_View, its FMVVMBlueprintViewModelContext array + FMVVMBlueprintViewBinding
array with FMVVMBlueprintPropertyPath endpoints) lives in the beta ModelViewViewModel plugin's
ModelViewViewModel* modules. The blessed mutators are on UMVVMEditorSubsystem (an editor subsystem whose
non-UFUNCTION Set*ForBinding methods are C++-only), and building a property path needs a
UE::MVVM::FMVVMConstFieldVariant (a non-USTRUCT C++ type) -- none of which is Python-reachable. So all of it
runs through the C++ handlers.

  READS (no ledger -- non-mutating):
    * list_viewmodels                 -- get_mvvm_viewmodels_json(asset_path): every viewmodel
                                         {name, guid, class_path, creation_type, global_identifier,
                                         property_path, is_instanced, flags...}.
    * list_mvvm_bindings              -- get_mvvm_bindings_json(asset_path): every binding
                                         {binding_id, binding_mode, enabled, compile, source{...},
                                         destination{...}, has_*_conversion}.
    * list_mvvm_conversion_functions  -- get_mvvm_conversion_functions_json(asset_path, name_filter,
                                         max_results): the project's allowed MVVM conversion functions
                                         (UMVVMEditorSubsystem::GetConversionFunctions).

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * add_viewmodel            -- UMVVMEditorSubsystem::AddViewModel (validates NotifyFieldValueChanged) +
                                  optional rename. Inverse: remove_viewmodel(name).
    * remove_viewmodel         -- RemoveViewModel(name). Inverse: best-effort re-add (LOSSY: NEW guid, so
                                  bindings that referenced the old guid are NOT restored).
    * rename_viewmodel         -- RenameViewModel(old,new). Inverse: rename back new->old.
    * set_viewmodel_settings   -- creation_type / global_identifier / property_path / setter+getter /
                                  optional / expose_instance / class_path(reparent). Inverse: re-apply prior.
    * add_mvvm_binding         -- AddBinding + SetSourcePathForBinding + SetDestinationPathForBinding +
                                  SetBindingTypeForBinding. Inverse: remove_mvvm_binding(binding_id).
    * set_mvvm_binding         -- mode / execution_mode(or reset) / enabled / compile / conversion(best-
                                  effort). Inverse: re-apply the captured prior fields.
    * remove_mvvm_binding      -- RemoveBinding(binding_id). Inverse: best-effort re-create (LOSSY: NEW guid,
                                  conversion functions not restored).
    * set_variable_field_notify -- pure Kismet: mark/unmark a Blueprint member variable FieldNotify (works on
                                  ANY UBlueprint). Inverse: set back to prev_enabled.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write's
inverse is appended to the shared per-session ledger (builtins._UMCP_LEDGERS[session]) with ledger key
`asset_path`, for editor_level.undo to fold. The `op` names are mvvm_* so the coordinator wires the matching
inverse cases into editor_level.undo.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a
snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/
user_code/code_obj (the C++ wrapper's own names).
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
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
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
    return {"status": "error", "error": (fn + " requires the C++ widget-MVVM handler "
            "(deferred to a batched C++ round). Enable the beta ModelViewViewModel plugin and rebuild the "
            "UnrealMCP plugin DLL with MCPReflection_WidgetMVVM.cpp (adds ModelViewViewModel + "
            "ModelViewViewModelBlueprint + ModelViewViewModelEditor Build.cs deps) to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _LIST_VMS_BODY = _HELP + r'''
rl = _mrl("get_mvvm_viewmodels_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_viewmodels")))
else:
    res = _decode(rl.get_mvvm_viewmodels_json(PARAMS["asset_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_viewmodels(ctx, asset_path: str) -> str:
        """List every MVVM viewmodel declared on a Widget Blueprint's view.

        asset_path: Widget Blueprint asset path (e.g. '/Game/UI/WBP_Menu.WBP_Menu').

        Returns {blueprint, has_view, viewmodel_count, viewmodels:[{name, guid, class_path, class_name,
        creation_type, global_identifier, property_path, is_instanced, has_resolver,
        expose_instance_in_editor, create_setter, create_getter, optional, can_rename, can_remove}]}.
        MVVM-only; needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_VMS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_BINDINGS_BODY = _HELP + r'''
rl = _mrl("get_mvvm_bindings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_mvvm_bindings")))
else:
    res = _decode(rl.get_mvvm_bindings_json(PARAMS["asset_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_mvvm_bindings(ctx, asset_path: str) -> str:
        """List every MVVM binding on a Widget Blueprint's view.

        asset_path: Widget Blueprint asset path.

        Returns {blueprint, has_view, binding_count, bindings:[{binding_id, binding_mode, enabled, compile,
        override_execution_mode, execution_mode?, has_source_to_dest_conversion, has_dest_to_source_conversion,
        source:{kind, viewmodel_id?|widget?, path}, destination:{kind, widget?, path}}]}. MVVM-only; needs the
        C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_BINDINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_CONV_BODY = _HELP + r'''
rl = _mrl("get_mvvm_conversion_functions_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_mvvm_conversion_functions")))
else:
    res = _decode(rl.get_mvvm_conversion_functions_json(PARAMS["asset_path"], PARAMS.get("name_filter", ""),
        int(PARAMS.get("max_results", 200))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_mvvm_conversion_functions(ctx, asset_path: str, name_filter: str = "",
                                       max_results: int = 200) -> str:
        """List the MVVM conversion functions available to a Widget Blueprint.

        asset_path:  Widget Blueprint asset path.
        name_filter: optional case-insensitive substring over the function name (empty = all).
        max_results: cap on emitted entries (default 200; `matched` still counts the full set).

        Uses UMVVMEditorSubsystem::GetConversionFunctions (respects the project's conversion-function filter
        developer setting). Returns {blueprint, total_available, matched, returned, name_filter,
        functions:[{name, display_name, category, is_function, is_node, function_path?|node_class?}]}.
        MVVM-only; needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "name_filter": name_filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_CONV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    _ADD_VM_BODY = _HELP + r'''
rl = _mrl("add_mvvm_viewmodel_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_viewmodel")))
else:
    res = _decode(rl.add_mvvm_viewmodel_json(PARAMS["asset_path"], PARAMS["viewmodel_class"],
        PARAMS.get("desired_name", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("added"):
            _ledger().append({"op": "mvvm_add_viewmodel", "asset_path": PARAMS["asset_path"],
                "name": res.get("name"), "guid": res.get("guid"), "class_path": res.get("class_path")})
        out = {"status": "success", "added": bool(res.get("added")), "name": res.get("name"),
               "guid": res.get("guid"), "class_path": res.get("class_path"), "note": res.get("note"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_viewmodel(ctx, asset_path: str, viewmodel_class: str, desired_name: str = "") -> str:
        """Add an MVVM viewmodel to a Widget Blueprint's view.

        asset_path:      Widget Blueprint asset path.
        viewmodel_class: the viewmodel UClass -- a native class path ('/Script/Module.UMyVM'), a generated
                         class path ('/Game/UI/VM_Foo.VM_Foo_C') or a Blueprint asset path ('/Game/UI/VM_Foo').
                         MUST implement NotifyFieldValueChanged (e.g. derive from UMVVMViewModelBase), else
                         the add is rejected with an honest error.
        desired_name:    optional -- rename the auto-generated viewmodel name to this (best-effort; keeps the
                         auto name + a note if the rename is rejected).

        Creates/ensures the view (RequestView) then UMVVMEditorSubsystem::AddViewModel. Ledgered inverse =
        remove_viewmodel(name). Returns {added, name, guid, class_path, note, ledger_depth}. MVVM-only;
        needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "viewmodel_class": viewmodel_class, "desired_name": desired_name}
        try:
            return json.dumps(_exec(_ADD_VM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_VM_BODY = _HELP + r'''
rl = _mrl("remove_mvvm_viewmodel_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_viewmodel")))
else:
    res = _decode(rl.remove_mvvm_viewmodel_json(PARAMS["asset_path"], PARAMS["viewmodel_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("removed"):
            _ledger().append({"op": "mvvm_remove_viewmodel", "asset_path": PARAMS["asset_path"],
                "name": res.get("name"), "class_path": res.get("class_path"),
                "creation_type": res.get("creation_type"),
                "global_identifier": res.get("global_identifier"),
                "property_path": res.get("property_path")})
        out = {"status": "success", "removed": bool(res.get("removed")), "name": res.get("name"),
               "class_path": res.get("class_path"), "viewmodel_count": res.get("viewmodel_count"),
               "note": res.get("note"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_viewmodel(ctx, asset_path: str, viewmodel_name: str) -> str:
        """Remove an MVVM viewmodel (by name) from a Widget Blueprint's view.

        asset_path:     Widget Blueprint asset path.
        viewmodel_name: the viewmodel's name (must be removable -- bCanRemove).

        Ledgered inverse = best-effort re-add (LOSSY: the re-added viewmodel gets a NEW guid, so any bindings
        that referenced the old guid are NOT restored). Returns {removed, name, class_path, viewmodel_count,
        note, ledger_depth}. MVVM-only; needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "viewmodel_name": viewmodel_name}
        try:
            return json.dumps(_exec(_REMOVE_VM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _RENAME_VM_BODY = _HELP + r'''
rl = _mrl("rename_mvvm_viewmodel_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("rename_viewmodel")))
else:
    res = _decode(rl.rename_mvvm_viewmodel_json(PARAMS["asset_path"], PARAMS["old_name"], PARAMS["new_name"]))
    if isinstance(res, dict) and res.get("error") and not res.get("old_name"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("renamed"):
            _ledger().append({"op": "mvvm_rename_viewmodel", "asset_path": PARAMS["asset_path"],
                "old_name": res.get("old_name"), "new_name": res.get("new_name")})
        out = {"status": "success" if res.get("renamed") else "error", "renamed": bool(res.get("renamed")),
               "old_name": res.get("old_name"), "new_name": res.get("new_name"),
               "message": res.get("error"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def rename_viewmodel(ctx, asset_path: str, old_name: str, new_name: str) -> str:
        """Rename an MVVM viewmodel (fixes up variable references).

        asset_path: Widget Blueprint asset path.
        old_name:   current viewmodel name.
        new_name:   new name (validated by the Kismet name validator; must be unique + legal).

        Uses UMVVMEditorSubsystem::RenameViewModel (which also ReplaceVariableReferences). Ledgered inverse =
        rename back new->old. Returns {renamed, old_name, new_name, message, ledger_depth}. MVVM-only; needs
        the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_VM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_VM_SETTINGS_BODY = _HELP + r'''
rl = _mrl("set_mvvm_viewmodel_settings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_viewmodel_settings")))
else:
    res = _decode(rl.set_mvvm_viewmodel_settings_json(PARAMS["asset_path"], PARAMS["viewmodel_name"],
        json.dumps(PARAMS.get("settings", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("set"):
            _ledger().append({"op": "mvvm_set_viewmodel_settings", "asset_path": PARAMS["asset_path"],
                "name": res.get("name"), "prev": res.get("prev", {})})
        out = {"status": "success", "set": bool(res.get("set")), "name": res.get("name"),
               "prev": res.get("prev"), "new": res.get("new"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_viewmodel_settings(ctx, asset_path: str, viewmodel_name: str, settings: dict) -> str:
        """Set MVVM viewmodel settings (creation type, identifiers, flags, or reparent its class).

        asset_path:     Widget Blueprint asset path.
        viewmodel_name: the viewmodel to modify.
        settings:       a dict of any of: creation_type ('Manual'|'CreateInstance'|'GlobalViewModelCollection'|
                        'PropertyPath'|'Resolver'|'Context'), global_identifier (str), property_path (str),
                        create_setter (bool), create_getter (bool), optional (bool), expose_instance (bool),
                        class_path (str -> reparent to a new viewmodel class via ReparentViewModel).

        Captures the prior value of each changed field. Ledgered inverse = re-apply the captured prior fields.
        Returns {set, name, prev, new, ledger_depth}. MVVM-only; needs the C++ handler + the ModelViewViewModel
        plugin (inert until built)."""
        params = {"asset_path": asset_path, "viewmodel_name": viewmodel_name, "settings": settings or {}}
        try:
            return json.dumps(_exec(_SET_VM_SETTINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_BINDING_BODY = _HELP + r'''
rl = _mrl("add_mvvm_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_mvvm_binding")))
else:
    res = _decode(rl.add_mvvm_binding_json(PARAMS["asset_path"], json.dumps(PARAMS.get("spec", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("added"):
            _ledger().append({"op": "mvvm_add_binding", "asset_path": PARAMS["asset_path"],
                "binding_id": res.get("binding_id")})
        out = {"status": "success", "added": bool(res.get("added")), "binding_id": res.get("binding_id"),
               "mode": res.get("mode"), "source_viewmodel": res.get("source_viewmodel"),
               "source_path": res.get("source_path"), "destination_widget": res.get("destination_widget"),
               "destination_path": res.get("destination_path"), "binding_count": res.get("binding_count"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_mvvm_binding(ctx, asset_path: str, spec: dict) -> str:
        """Add an MVVM binding (viewmodel property -> widget property).

        asset_path: Widget Blueprint asset path.
        spec:       {'source': {'viewmodel': '<name>' | 'viewmodel_id': '<guid>', 'path': 'Prop.Sub'},
                     'destination': {'widget': '<widget name>' | 'self': true, 'path': 'Prop.Sub'},
                     'mode': 'OneWayToDestination'}
                    source.path resolves against the viewmodel class; destination.path against the named
                    widget's class (or the UserWidget itself when 'self' is true). Dotted sub-paths walk
                    object/struct sub-properties. mode is an EMVVMBindingMode name (OneTimeToDestination |
                    OneWayToDestination | TwoWay | OneWayToSource); default OneWayToDestination.

        Resolves + validates BOTH endpoints BEFORE mutating, then AddBinding + SetSourcePathForBinding +
        SetDestinationPathForBinding + SetBindingTypeForBinding (the blessed UMVVMEditorSubsystem path).
        Ledgered inverse = remove_mvvm_binding(binding_id). Returns {added, binding_id, mode,
        source_viewmodel, source_path, destination_widget, destination_path, binding_count, ledger_depth}.
        MVVM-only; needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "spec": spec or {}}
        try:
            return json.dumps(_exec(_ADD_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_BINDING_BODY = _HELP + r'''
rl = _mrl("set_mvvm_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_mvvm_binding")))
else:
    res = _decode(rl.set_mvvm_binding_json(PARAMS["asset_path"], PARAMS["binding_id"],
        json.dumps(PARAMS.get("spec", {}))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("set"):
            _ledger().append({"op": "mvvm_set_binding", "asset_path": PARAMS["asset_path"],
                "binding_id": res.get("binding_id"), "prev": res.get("prev", {})})
        out = {"status": "success", "set": bool(res.get("set")), "binding_id": res.get("binding_id"),
               "prev": res.get("prev"), "new": res.get("new"),
               "conversion_note": res.get("conversion_note"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_mvvm_binding(ctx, asset_path: str, binding_id: str, spec: dict) -> str:
        """Modify an existing MVVM binding's mode / execution mode / enabled / compile / conversion.

        asset_path: Widget Blueprint asset path.
        binding_id: the binding's GUID (from list_mvvm_bindings / add_mvvm_binding).
        spec:       any of: mode (EMVVMBindingMode name), execution_mode (EMVVMExecutionMode name) OR
                    reset_execution_mode (bool -> clear the override), enabled (bool), compile (bool),
                    conversion_function (str 'Package.Class:Function' -- BEST-EFFORT simple source->dest
                    UFunction; it clears the source path + generates a wrapper graph).

        Uses UMVVMEditorSubsystem setters (SetBindingTypeForBinding / OverrideExecutionModeForBinding /
        ResetExecutionModeForBinding / SetEnabledForBinding / SetCompileForBinding /
        SetSourceToDestinationConversionFunction). Captures each changed field's prior value. Ledgered
        inverse = re-apply the prior fields. Returns {set, binding_id, prev, new, conversion_note,
        ledger_depth}. MVVM-only; needs the C++ handler + the ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "binding_id": binding_id, "spec": spec or {}}
        try:
            return json.dumps(_exec(_SET_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_BINDING_BODY = _HELP + r'''
rl = _mrl("remove_mvvm_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_mvvm_binding")))
else:
    res = _decode(rl.remove_mvvm_binding_json(PARAMS["asset_path"], PARAMS["binding_id"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("removed"):
            _ledger().append({"op": "mvvm_remove_binding", "asset_path": PARAMS["asset_path"],
                "binding_id": PARAMS["binding_id"], "descriptor": res.get("descriptor", {})})
        out = {"status": "success", "removed": bool(res.get("removed")), "binding_id": res.get("binding_id"),
               "binding_count": res.get("binding_count"), "descriptor": res.get("descriptor"),
               "note": res.get("note"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_mvvm_binding(ctx, asset_path: str, binding_id: str) -> str:
        """Remove an MVVM binding (by GUID) from a Widget Blueprint's view.

        asset_path: Widget Blueprint asset path.
        binding_id: the binding's GUID.

        Uses UMVVMEditorSubsystem::RemoveBinding (resolved to the live array element by GUID). Captures a
        descriptor (mode + source/destination endpoints) for the inverse. Ledgered inverse = best-effort
        re-create a similar binding (LOSSY: NEW guid, conversion functions not restored). Returns {removed,
        binding_id, binding_count, descriptor, note, ledger_depth}. MVVM-only; needs the C++ handler + the
        ModelViewViewModel plugin (inert until built)."""
        params = {"asset_path": asset_path, "binding_id": binding_id}
        try:
            return json.dumps(_exec(_REMOVE_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_FIELD_NOTIFY_BODY = _HELP + r'''
rl = _mrl("set_variable_field_notify_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_variable_field_notify")))
else:
    res = _decode(rl.set_variable_field_notify_json(PARAMS["asset_path"], PARAMS["variable_name"],
        bool(PARAMS["enable"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("changed"):
            _ledger().append({"op": "mvvm_set_field_notify", "asset_path": PARAMS["asset_path"],
                "variable": res.get("variable"), "prev_enabled": bool(res.get("prev_enabled"))})
        out = {"status": "success", "variable": res.get("variable"), "enabled": bool(res.get("enabled")),
               "prev_enabled": bool(res.get("prev_enabled")), "changed": bool(res.get("changed")),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_variable_field_notify(ctx, asset_path: str, variable_name: str, enable: bool = True) -> str:
        """Mark (or unmark) a Blueprint member variable as FieldNotify.

        asset_path:    Blueprint asset path -- ANY UBlueprint (a viewmodel BP, a widget BP, an actor BP). A
                       FieldNotify variable broadcasts changes so MVVM bindings can observe it.
        variable_name: the member variable's name.
        enable:        True to add the FieldNotify metadata; False to remove it.

        Pure Kismet (no MVVM module dependency): FBlueprintEditorUtils::SetBlueprintVariableMetaData /
        RemoveBlueprintVariableMetaData with FBlueprintMetadata::MD_FieldNotify (mirrors the engine's own
        variable FieldNotify toggle). Captures prev_enabled. Ledgered inverse = set back to prev_enabled.
        Returns {variable, enabled, prev_enabled, changed, ledger_depth}. Needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "variable_name": variable_name, "enable": enable}
        try:
            return json.dumps(_exec(_SET_FIELD_NOTIFY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
