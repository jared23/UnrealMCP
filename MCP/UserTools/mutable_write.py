"""UserTools :: Mutable — Customizable Objects (WRITE)  (spec: docs/spec/mutable.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8, Mutable / Customizable Object
plugin LOADED). CREATE / COMPILE / INSTANCE / PARAMETER wave -- the mutating counterpart to
mutable.py + mutable_read2.py. Query convention, base64 PARAMS injection, Output-Log auto-capture,
and the per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py (and
mirror sound_write.py / ai_write.py).

VERIFIED LIVE (TestMCPSetup, UE 5.8.1) -- what these tools rely on:
  * unreal.CustomizableObjectEditorFunctionLibrary.new_customizable_object(FNewCustomizableObjectParameters)
    creates a CustomizableObject under a package path (AddEssentialGraphNodes -> immediately
    compilable). compile_customizable_object_synchronously(co) returns COMPLETED and co.is_compiled()
    -> True. A factory CO has get_parameter_count()==0, get_state_count()==1 ('Default'),
    get_component_count()==0 (honest-empty until wave-3 graph authoring).
  * A CustomizableObjectInstance is PERSISTED via AssetTools.create_asset(name, package_path,
    CustomizableObjectInstance, None) + instance.set_object(co) (verified: reloads by path with the CO
    assigned, and deletes cleanly). NOTE: duplicate_asset of a /Engine/Transient co.create_instance()
    was REJECTED -- it yields an UNLOADABLE, undeletable asset (verified live). So
    create_customizable_object_instance ships a real, ledgered, undoable asset creation via create_asset.
  * EditorValidatorSubsystem.validate_assets_with_settings([AssetData], ValidateAssetsSettings) ->
    (int, ValidateAssetsResults{num_checked/num_valid/num_invalid/num_warnings/...}). AssetData is
    fetched via AssetRegistry.get_assets_by_package_name(Name(package)). (validate_objects does not
    exist in 5.8.)
  * Instance parameters: per-type get_/set_<type>_parameter_selected_option(name[, value]) +
    set_default_values() + copy_parameters_from_instance(src) + get_/set_current_state(). The setters
    SILENTLY NO-OP on an unknown parameter name (get returns the type default), so every set path
    here first checks the parameter exists (co.contains_parameter) and refuses otherwise -- no
    meaningless ledger entry is ever written.
  * There is NO save/load_descriptor in 5.8; parameter snapshots are read/applied per-parameter.

Reversibility / undo: this module registers NO `undo` tool (editor_level.py owns the single unified
`undo`). Ledger ops it emits:
  - "create_asset" {asset_path, package_path, created_dir}  (create_customizable_object,
    create_customizable_object_instance) -- REUSES the generic created-asset inverse already folded
    into editor_level.undo (close editors + GC + delete [+ rmdir]). NO new fold needed.
  - "mutable_set_param"  {instance_path, param_name, param_type, range_index, prior_value}  -- NEW fold.
  - "mutable_set_params" {instance_path, priors:[{name,type,value,range_index}]}             -- NEW fold.
    (emitted by reset_mutable_parameters + paste_mutable_parameters)
  - "mutable_set_state"  {instance_path, prior_state}                                         -- NEW fold.
  The three NEW folds' exact inverse logic is documented at the bottom of this file for the
  coordinator to add to editor_level.undo.

NON-invertible side effects (deliberately NO ledger, documented in each docstring):
  - compile_customizable_object (compilation is not cleanly reversible; is_compiled is stored state).
  - validate_customizable_object (read-only validation).
  - update_mutable_instance (kicks off async skeletal-mesh generation; the generated resources are
    runtime/GPU, not an asset edit; completion needs a GPU/PIE context).

Every tool performs ONE operation per call, staying well under the execute_python socket window.
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

    # Shared Unreal-side helpers. No ''' / no backslashes in this block.
    _MW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load_inst(path):
    if not path:
        return None, "no instance path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.CustomizableObjectInstance):
        return None, "asset is not a CustomizableObjectInstance (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _load_co(path):
    if not path:
        return None, "no CustomizableObject path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.CustomizableObject):
        return None, "asset is not a CustomizableObject (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _save_nonvalidating(obj_or_path):
    # Non-validating persist (mirrors editor_level._st_save): save_packages skips the asset-VALIDATION
    # save path that can hard-crash on freshly-authored Mutable assets.
    try:
        a = obj_or_path
        if isinstance(obj_or_path, str):
            a = unreal.EditorAssetLibrary.load_asset(obj_or_path)
        if a is None:
            return False
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([a.get_outermost()], False))
    except Exception:
        return False
def _enum_name(v):
    if v is None:
        return None
    n = getattr(v, "name", None)
    if isinstance(n, str) and n:
        return n
    return str(v)
def _ser(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Object):
        return {"__object__": _try(lambda: v.get_path_name())}
    return str(v)
_INST_GETTERS = {
    "BOOL": "get_bool_parameter_selected_option",
    "FLOAT": "get_float_parameter_selected_option",
    "INT": "get_int_parameter_selected_option",
    "COLOR": "get_color_parameter_selected_option",
}
_INST_SETTERS = {
    "BOOL": "set_bool_parameter_selected_option",
    "FLOAT": "set_float_parameter_selected_option",
    "INT": "set_int_parameter_selected_option",
    "COLOR": "set_color_parameter_selected_option",
}
# Set/reset/paste support the scalar-ish types whose inverse is a single deterministic setter call.
# (TEXTURE/MATERIAL/PROJECTOR/TRANSFORM/SKELETAL_MESH are complex refs/structs -> read-surfaced by
# mutable_read2.copy_mutable_parameters but not settable here, so no un-restorable ledger is written.)
_SET_SUPPORTED = set(_INST_SETTERS.keys())
def _param_type(co, name):
    if not _try(lambda: co.contains_parameter(name), False):
        return None
    return _enum_name(_try(lambda: co.get_parameter_type_by_name(name)))
def _coerce(ty, value):
    if ty == "BOOL":
        return bool(value)
    if ty == "FLOAT":
        return float(value)
    if ty == "INT":
        return str(value)
    if ty == "COLOR":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            a = float(value[3]) if len(value) > 3 else 1.0
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), a)
        return None
    return None
def _get_value(inst, ty, name):
    g = _INST_GETTERS.get(ty)
    if g is None or not hasattr(inst, g):
        return None
    return _ser(_try(lambda: getattr(inst, g)(name)))
def _set_value(inst, ty, name, coerced):
    s = _INST_SETTERS.get(ty)
    if s is None:
        return False
    getattr(inst, s)(name, coerced)
    return True
'''

    # ------------------------------------------------------------------ #
    # create_customizable_object                                          #
    # ------------------------------------------------------------------ #
    _CREATE_CO_BODY = _MW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
asset_path = package_path + "/" + name
EFL = getattr(unreal, "CustomizableObjectEditorFunctionLibrary", None)
NP = getattr(unreal, "NewCustomizableObjectParameters", None)
if EFL is None or NP is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "Mutable editor API missing (CustomizableObjectEditorFunctionLibrary / NewCustomizableObjectParameters); is the Mutable plugin loaded?"}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    np = NP()
    np.set_editor_property("asset_name", name)
    np.set_editor_property("package_path", package_path)
    co = _try(lambda: EFL.new_customizable_object(np))
    if co is None or not isinstance(co, unreal.CustomizableObject):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "new_customizable_object returned %s for %s" % (type(co).__name__, asset_path)}))
    else:
        _close_editors(co)
        _save_nonvalidating(co)
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        result = {"status": "success", "name": co.get_name(), "asset_path": asset_path,
                  "object_path": co.get_path_name(), "class": co.get_class().get_name(),
                  "is_compiled": bool(_try(lambda: co.is_compiled(), False)),
                  "parameter_count": _try(lambda: co.get_parameter_count(), 0),
                  "state_count": _try(lambda: co.get_state_count(), 0),
                  "component_count": _try(lambda: co.get_component_count(), 0),
                  "created_dir": created_dir, "ledger_depth": len(_ledger())}
        result["note"] = ("Created an empty (essential-nodes) CustomizableObject via "
            "new_customizable_object. It is NOT compiled yet and exposes 0 parameters / 1 state "
            "('Default') until its source graph is authored (wave-3, C++). Compile it with "
            "compile_customizable_object. Ledgered 'create_asset' (undo = delete).")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def create_customizable_object(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new Mutable CustomizableObject asset non-interactively.

        name:         asset name for the new CustomizableObject (e.g. 'CO_Hero').
        package_path: content directory (default '/Game/MCP_Scratch'; keep scratch work under
                      /Game/MCP_Scratch). Intermediate folders are created as needed.

        Uses CustomizableObjectEditorFunctionLibrary.new_customizable_object(FNewCustomizableObjectParameters)
        which seeds the essential graph nodes -> an immediately compilable but EMPTY object (0
        parameters, 1 state 'Default', 0 components until the graph is authored in wave-3). Saved with
        the non-validating package save (avoids the validating-save crash path). Compile it with
        compile_customizable_object; inspect with mutable.get_customizable_object_info.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir} -- reuses the generic
        created-asset inverse already folded into editor_level.undo (close editors + GC + delete
        [+ rmdir if we created the dir]). NO new fold needed."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_CO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_customizable_object_instance                                 #
    # ------------------------------------------------------------------ #
    _CREATE_INST_BODY = _MW_HELPERS + r'''
co_path = PARAMS["co_path"]
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
asset_path = package_path + "/" + name
co, err = _load_co(co_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    at = unreal.AssetToolsHelpers.get_asset_tools()
    # Persist a real, RELOADABLE instance asset: create_asset(factory None) then assign the CO via
    # set_object. (duplicate_asset of a /Engine/Transient co.create_instance() yields an UNLOADABLE,
    # undeletable asset -- verified live; the factory-None create + set_object path reloads + deletes
    # cleanly and carries the CO's default parameters.)
    inst = _try(lambda: at.create_asset(name, package_path, unreal.CustomizableObjectInstance, None))
    if inst is None or not isinstance(inst, unreal.CustomizableObjectInstance):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset(CustomizableObjectInstance) returned %s for %s" % (type(inst).__name__, asset_path)}))
    else:
        assign_ok = _try(lambda: (inst.set_object(co), True)[1], False)
        if not assign_ok:
            if created_dir and EAL.does_directory_exist(package_path):
                try: EAL.delete_directory(package_path)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "failed to assign CustomizableObject to the new instance via set_object"}))
        else:
            _close_editors(inst)
            _save_nonvalidating(inst)
            assigned = _try(lambda: inst.get_customizable_object())
            assigned_path = _try(lambda: assigned.get_path_name()) if assigned is not None else None
            _ledger().append({"op": "create_asset", "asset_path": asset_path,
                              "package_path": package_path, "created_dir": created_dir})
            result = {"status": "success", "name": inst.get_name(), "asset_path": asset_path,
                      "object_path": inst.get_path_name(), "class": inst.get_class().get_name(),
                      "customizable_object": assigned_path,
                      "current_state": _try(lambda: str(inst.get_current_state())),
                      "created_dir": created_dir, "ledger_depth": len(_ledger())}
            result["note"] = ("Persisted a CustomizableObjectInstance asset (a saved parameter preset "
                "for the assigned CustomizableObject), created via AssetTools.create_asset + "
                "set_object(co) so it reloads and deletes cleanly. Set values with "
                "set_mutable_parameter / paste_mutable_parameters, switch runtime state with "
                "set_mutable_state, and generate a preview mesh with update_mutable_instance (GPU). "
                "Ledgered 'create_asset' (undo = delete).")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def create_customizable_object_instance(ctx, co_path: str, name: str,
                                            package_path: str = "/Game/MCP_Scratch") -> str:
        """Create + PERSIST a CustomizableObjectInstance asset for a CustomizableObject.

        co_path:      source CustomizableObject asset path.
        name:         asset name for the new instance (e.g. 'COI_Hero_Preset').
        package_path: content directory (default '/Game/MCP_Scratch').

        Persists a real, RELOADABLE instance asset via AssetTools.create_asset(name, package_path,
        CustomizableObjectInstance, None) + instance.set_object(co) (verified live: reloads by path
        with get_customizable_object() == the source CO, and deletes cleanly). NOTE: duplicating a
        transient co.create_instance() produces an UNLOADABLE/undeletable asset, so this path is used
        instead. The instance is a saved parameter preset -- set values with set_mutable_parameter,
        switch state with set_mutable_state, generate a preview skeletal mesh with
        update_mutable_instance (needs a GPU/PIE context).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir} -- reuses the generic
        created-asset inverse in editor_level.undo (delete on undo). NO new fold needed."""
        params = {"co_path": co_path, "name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_INST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # compile_customizable_object — sync compile, NON-invertible, no ledger#
    # ------------------------------------------------------------------ #
    _COMPILE_BODY = _MW_HELPERS + r'''
co_path = PARAMS["co_path"]
co, err = _load_co(co_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    EFL = getattr(unreal, "CustomizableObjectEditorFunctionLibrary", None)
    if EFL is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "CustomizableObjectEditorFunctionLibrary missing (Mutable plugin not loaded?)"}))
    else:
        was_compiled = bool(_try(lambda: co.is_compiled(), False))
        state = _try(lambda: EFL.compile_customizable_object_synchronously(co))
        now_compiled = bool(_try(lambda: co.is_compiled(), False))
        state_name = _enum_name(state) if state is not None else None
        ok = now_compiled and (state_name in (None, "COMPLETED") or "COMPLETED" in str(state_name))
        result = {"status": "success" if now_compiled else "error",
                  "co_path": co.get_path_name(), "compile_state": str(state) if state is not None else None,
                  "compile_state_name": state_name, "was_compiled": was_compiled,
                  "is_compiled": now_compiled,
                  "parameter_count": _try(lambda: co.get_parameter_count(), 0),
                  "state_count": _try(lambda: co.get_state_count(), 0),
                  "component_count": _try(lambda: co.get_component_count(), 0)}
        result["note"] = ("Synchronous compile via compile_customizable_object_synchronously "
            "(COMPLETED == success). Compilation is a NON-invertible build step -> NO ledger entry is "
            "written (is_compiled reflects stored state; there is nothing to undo). A factory CO "
            "compiles to 0 parameters / 1 state until its graph is authored (wave-3).")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def compile_customizable_object(ctx, co_path: str) -> str:
        """Compile a Mutable CustomizableObject synchronously. NOT undoable (no ledger).

        co_path: CustomizableObject asset path.

        Uses CustomizableObjectEditorFunctionLibrary.compile_customizable_object_synchronously(co)
        (default optimization NONE / texture-compression FAST). Returns compile_state
        (COMPLETED == success), is_compiled, and post-compile parameter/state/component counts. A
        factory (essential-nodes) CO compiles cleanly to 0 parameters / 1 state 'Default'.

        Compilation is a build step with no clean inverse, so this writes NO ledger entry (documented):
        undo cannot 'uncompile'. Deleting the object (via a create-undo) still works on a compiled CO."""
        try:
            return json.dumps(_exec(_COMPILE_BODY, {"co_path": co_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_customizable_object — EditorValidatorSubsystem, no ledger   #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _MW_HELPERS + r'''
co_path = PARAMS["co_path"]
compile_first = bool(PARAMS.get("compile_first"))
co, err = _load_co(co_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    compiled_now = None
    if compile_first:
        EFL = getattr(unreal, "CustomizableObjectEditorFunctionLibrary", None)
        if EFL is not None:
            _try(lambda: EFL.compile_customizable_object_synchronously(co))
            compiled_now = bool(_try(lambda: co.is_compiled(), False))
    vs = _try(lambda: unreal.get_editor_subsystem(unreal.EditorValidatorSubsystem))
    VAS = getattr(unreal, "ValidateAssetsSettings", None)
    if vs is None or VAS is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "EditorValidatorSubsystem / ValidateAssetsSettings unavailable"}))
    else:
        pkg = co.get_outermost().get_name()
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        ads = _try(lambda: list(ar.get_assets_by_package_name(unreal.Name(pkg)) or []), [])
        target = None
        for a in ads:
            if str(a.asset_name) == co.get_name():
                target = a; break
        if target is None and ads:
            target = ads[0]
        if target is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "could not resolve AssetData for %s (package %s)" % (co_path, pkg)}))
        else:
            settings = VAS()
            for p, v in [("show_if_no_failures", False), ("collect_per_asset_details", True),
                         ("unload_assets_loaded_for_validation", False)]:
                try: settings.set_editor_property(p, v)
                except Exception: pass
            res = _try(lambda: vs.validate_assets_with_settings([target], settings))
            summary = {}
            issues = []
            if isinstance(res, (tuple, list)) and len(res) >= 2:
                vr = res[1]
                for p in ("num_requested", "num_checked", "num_valid", "num_invalid",
                          "num_skipped", "num_warnings", "num_unable_to_validate",
                          "num_external_objects", "asset_limit_reached"):
                    summary[p] = _try(lambda p=p: vr.get_editor_property(p))
                details = _try(lambda: vr.get_editor_property("assets_details"))
                if details is not None:
                    try:
                        for k in list(details.keys())[:20]:
                            d = details[k]
                            rec = {"asset": str(k)}
                            rec["result"] = _enum_name(_try(lambda: d.get_editor_property("result")))
                            errs = _try(lambda: d.get_editor_property("errors"))
                            warns = _try(lambda: d.get_editor_property("warnings"))
                            rec["errors"] = [str(x) for x in (errs or [])][:20]
                            rec["warnings"] = [str(x) for x in (warns or [])][:20]
                            issues.append(rec)
                    except Exception:
                        pass
            is_valid = bool(summary.get("num_invalid") == 0 and summary.get("num_checked"))
            result = {"status": "success", "co_path": co.get_path_name(),
                      "is_compiled": bool(_try(lambda: co.is_compiled(), False)),
                      "compiled_first": compiled_now, "valid": is_valid,
                      "summary": summary, "details": issues}
            result["note"] = ("Ran the registered asset validators (incl. Mutable's "
                "AssetValidator_CustomizableObjects) via EditorValidatorSubsystem."
                "validate_assets_with_settings. 'valid' == num_invalid 0 with >=1 checked. Read-only "
                "(no ledger). Pass compile_first=True to compile before validating (some checks need a "
                "compiled object).")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def validate_customizable_object(ctx, co_path: str, compile_first: bool = False) -> str:
        """Validate a Mutable CustomizableObject with the asset-validation subsystem. Read-only.

        co_path:       CustomizableObject asset path.
        compile_first: compile the object synchronously before validating (default False; some
                       validators need a compiled object).

        Uses EditorValidatorSubsystem.validate_assets_with_settings([AssetData], ValidateAssetsSettings)
        (the 5.8 API; validate_objects does not exist). Returns valid (num_invalid == 0 with >=1
        checked), a summary (num_checked/num_valid/num_invalid/num_warnings/...), and per-asset
        errors/warnings. No ledger (validation does not mutate the asset)."""
        params = {"co_path": co_path, "compile_first": compile_first}
        try:
            return json.dumps(_exec(_VALIDATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_mutable_parameter — read one instance value, no ledger           #
    # ------------------------------------------------------------------ #
    _GET_PARAM_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
name = PARAMS["param_name"]
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    co = _try(lambda: inst.get_customizable_object())
    if co is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "instance has no assigned CustomizableObject"}))
    else:
        ty = _param_type(co, name)
        if ty is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "parameter not found on the assigned CustomizableObject: %r" % name,
                "hint": "use mutable.get_customizable_object_parameters to list valid names"}))
        else:
            g = _INST_GETTERS.get(ty)
            allget = {"BOOL": "get_bool_parameter_selected_option", "FLOAT": "get_float_parameter_selected_option",
                      "INT": "get_int_parameter_selected_option", "COLOR": "get_color_parameter_selected_option",
                      "TEXTURE": "get_texture_parameter_selected_option", "MATERIAL": "get_material_parameter_selected_option",
                      "PROJECTOR": "get_projector_parameter_selected_option", "TRANSFORM": "get_transform_parameter_selected_option",
                      "SKELETAL_MESH": "get_skeletal_mesh_parameter_selected_option"}
            gm = allget.get(ty)
            val = _ser(_try(lambda: getattr(inst, gm)(name))) if (gm and hasattr(inst, gm)) else None
            result = {"status": "success", "instance_path": inst.get_path_name(),
                      "param_name": name, "param_type": ty, "value": val,
                      "settable_here": ty in _SET_SUPPORTED}
            result["note"] = ("Current selected option for one instance parameter "
                "(get_<type>_parameter_selected_option). set_mutable_parameter can write BOOL/FLOAT/"
                "INT/COLOR; other types are read-only here. No ledger.")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_mutable_parameter(ctx, instance_path: str, param_name: str) -> str:
        """Read one parameter's current value on a CustomizableObjectInstance. Read-only.

        instance_path: CustomizableObjectInstance asset path.
        param_name:    parameter name (as listed by mutable.get_customizable_object_parameters).

        Returns {param_name, param_type, value, settable_here}. Uses the per-type
        get_<type>_parameter_selected_option. Refuses honestly if the parameter does not exist on the
        assigned CustomizableObject. No ledger."""
        params = {"instance_path": instance_path, "param_name": param_name}
        try:
            return json.dumps(_exec(_GET_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_mutable_parameter — ledger mutable_set_param                     #
    # ------------------------------------------------------------------ #
    _SET_PARAM_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
name = PARAMS["param_name"]
value = PARAMS.get("value")
range_index = int(PARAMS.get("range_index", -1))
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    co = _try(lambda: inst.get_customizable_object())
    if co is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "instance has no assigned CustomizableObject"}))
    else:
        ty = _param_type(co, name)
        if ty is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "parameter not found on the assigned CustomizableObject: %r" % name,
                "hint": "use mutable.get_customizable_object_parameters to list valid names"}))
        elif ty not in _SET_SUPPORTED:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "set not supported for parameter type %s via this tool (supported: %s)" % (ty, sorted(_SET_SUPPORTED)),
                "hint": "BOOL/FLOAT/INT/COLOR are settable; complex types (TEXTURE/MATERIAL/PROJECTOR/TRANSFORM/SKELETAL_MESH) are read-only here"}))
        else:
            coerced = _coerce(ty, value)
            if coerced is None and ty != "BOOL":
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "could not coerce value %r for %s parameter %r" % (value, ty, name)}))
            else:
                prior = _get_value(inst, ty, name)
                ok = _try(lambda: _set_value(inst, ty, name, coerced), False)
                _save_nonvalidating(inst)
                newval = _get_value(inst, ty, name)
                _ledger().append({"op": "mutable_set_param", "instance_path": inst.get_path_name(),
                                  "param_name": name, "param_type": ty, "range_index": range_index,
                                  "prior_value": prior})
                result = {"status": "success" if ok else "error", "instance_path": inst.get_path_name(),
                          "param_name": name, "param_type": ty, "prior_value": prior,
                          "new_value": newval, "ledger_depth": len(_ledger())}
                result["note"] = ("Set one instance parameter (set_<type>_parameter_selected_option) and "
                    "saved the instance. Ledgered NEW op 'mutable_set_param' {instance_path, param_name, "
                    "param_type, range_index, prior_value}; inverse re-applies prior_value via the same "
                    "per-type setter and re-saves.")
                print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def set_mutable_parameter(ctx, instance_path: str, param_name: str, value,
                              range_index: int = -1) -> str:
        """Set one parameter on a CustomizableObjectInstance (BOOL/FLOAT/INT/COLOR). Ledgered.

        instance_path: CustomizableObjectInstance asset path.
        param_name:    parameter name (must exist on the assigned CO; refused otherwise -- the UE
                       setters silently no-op on unknown names, so this guards to keep the ledger
                       faithful).
        value:         BOOL -> true/false; FLOAT -> number; INT -> option name string; COLOR ->
                       [r,g,b] or [r,g,b,a] floats.
        range_index:   multidimensional-parameter range index (default -1 = single value).

        Complex types (TEXTURE/MATERIAL/PROJECTOR/TRANSFORM/SKELETAL_MESH) are read-only here (their
        inverse is not a single deterministic setter) -- read them with copy_mutable_parameters.

        Ledgered NEW write op 'mutable_set_param' {instance_path, param_name, param_type, range_index,
        prior_value}. Inverse: reload the instance, re-apply prior_value via the matching
        set_<type>_parameter_selected_option, and re-save (see fold notes at file end)."""
        params = {"instance_path": instance_path, "param_name": param_name,
                  "value": value, "range_index": range_index}
        try:
            return json.dumps(_exec(_SET_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # reset_mutable_parameters — ledger mutable_set_params                 #
    # ------------------------------------------------------------------ #
    _RESET_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    co = _try(lambda: inst.get_customizable_object())
    if co is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "instance has no assigned CustomizableObject"}))
    else:
        pc = _try(lambda: co.get_parameter_count(), 0) or 0
        names = [str(_try(lambda i=i: co.get_parameter_name(i))) for i in range(pc)]
        types = {n: _param_type(co, n) for n in names}
        unsupported = sorted({t for n, t in types.items() if t not in _SET_SUPPORTED})
        if pc == 0:
            print("@@UMCP@@" + json.dumps({"status": "success", "instance_path": inst.get_path_name(),
                "parameter_count": 0, "reset_count": 0, "ledgered": False,
                "note": "no parameters to reset (factory CO / empty instance); nothing changed, no ledger entry."}))
        elif unsupported:
            print("@@UMCP@@" + json.dumps({"status": "error", "instance_path": inst.get_path_name(),
                "message": "instance has parameters of set-unsupported types %s; reset refused to keep undo faithful" % unsupported,
                "hint": "reset/paste restore only BOOL/FLOAT/INT/COLOR; this instance mixes complex types"}))
        else:
            priors = []
            for n in names:
                t = types[n]
                priors.append({"name": n, "type": t, "value": _get_value(inst, t, n), "range_index": -1})
            _try(lambda: inst.set_default_values())
            _save_nonvalidating(inst)
            _ledger().append({"op": "mutable_set_params", "instance_path": inst.get_path_name(),
                              "priors": priors})
            newvals = {n: _get_value(inst, types[n], n) for n in names}
            result = {"status": "success", "instance_path": inst.get_path_name(),
                      "parameter_count": pc, "reset_count": len(priors), "ledgered": True,
                      "new_values": newvals, "ledger_depth": len(_ledger())}
            result["note"] = ("Reset all instance parameters to their defaults (set_default_values) and "
                "saved. Ledgered NEW op 'mutable_set_params' {instance_path, priors:[{name,type,value,"
                "range_index}]}; inverse re-applies each prior via its per-type setter and re-saves.")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def reset_mutable_parameters(ctx, instance_path: str) -> str:
        """Reset every parameter on a CustomizableObjectInstance to its default. Ledgered.

        instance_path: CustomizableObjectInstance asset path.

        Calls instance.set_default_values() (resets all parameters), then saves. Refuses if the
        instance has any parameter of a set-unsupported type (TEXTURE/MATERIAL/PROJECTOR/TRANSFORM/
        SKELETAL_MESH) so the recorded inverse stays faithful. A factory CO / empty instance has no
        parameters -> nothing to do, no ledger entry.

        Ledgered NEW write op 'mutable_set_params' {instance_path, priors:[{name,type,value,
        range_index}]} (shared with paste_mutable_parameters). Inverse: reload the instance, re-apply
        each prior via its per-type setter, and re-save."""
        try:
            return json.dumps(_exec(_RESET_BODY, {"instance_path": instance_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # paste_mutable_parameters — ledger mutable_set_params                 #
    # ------------------------------------------------------------------ #
    _PASTE_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
values = PARAMS.get("values")
source_path = PARAMS.get("source_instance_path")
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    co = _try(lambda: inst.get_customizable_object())
    if co is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "instance has no assigned CustomizableObject"}))
    elif values is None and not source_path:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "provide either values ({name: value}) or source_instance_path"}))
    else:
        # Normalize values: accept {name:value} OR a list of {name,value[,type]} (copy_mutable_parameters output).
        vmap = {}
        if isinstance(values, dict):
            vmap = dict(values)
        elif isinstance(values, list):
            for row in values:
                if isinstance(row, dict) and "name" in row:
                    vmap[row["name"]] = row.get("value")
        source = None
        source_err = None
        if source_path:
            source, source_err = _load_inst(source_path)
        if source_err:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "source_instance_path invalid: %s" % source_err}))
        else:
            if source is not None:
                # copy mode: every parameter is potentially affected -> require all supported
                pc = _try(lambda: co.get_parameter_count(), 0) or 0
                names = [str(_try(lambda i=i: co.get_parameter_name(i))) for i in range(pc)]
            else:
                names = list(vmap.keys())
            types = {}
            bad = []
            unknown = []
            for n in names:
                t = _param_type(co, n)
                if t is None:
                    unknown.append(n); continue
                if t not in _SET_SUPPORTED:
                    bad.append((n, t)); continue
                types[n] = t
            if unknown and source is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "unknown parameter(s) not on the assigned CO: %s" % unknown,
                    "hint": "use mutable.get_customizable_object_parameters for valid names"}))
            elif bad:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "set-unsupported parameter type(s): %s; paste refused to keep undo faithful" % bad,
                    "hint": "paste restores only BOOL/FLOAT/INT/COLOR"}))
            elif not types:
                print("@@UMCP@@" + json.dumps({"status": "success", "instance_path": inst.get_path_name(),
                    "applied_count": 0, "ledgered": False,
                    "note": "no applicable parameters (empty instance or empty values); nothing changed, no ledger."}))
            else:
                priors = []
                for n in types:
                    priors.append({"name": n, "type": types[n], "value": _get_value(inst, types[n], n), "range_index": -1})
                applied = 0
                if source is not None:
                    _try(lambda: inst.copy_parameters_from_instance(source))
                    applied = len(types)
                else:
                    for n in types:
                        c = _coerce(types[n], vmap.get(n))
                        if c is None and types[n] != "BOOL":
                            continue
                        if _try(lambda n=n, c=c: _set_value(inst, types[n], n, c), False):
                            applied += 1
                _save_nonvalidating(inst)
                _ledger().append({"op": "mutable_set_params", "instance_path": inst.get_path_name(),
                                  "priors": priors})
                newvals = {n: _get_value(inst, types[n], n) for n in types}
                result = {"status": "success", "instance_path": inst.get_path_name(),
                          "mode": "copy_from_instance" if source is not None else "values",
                          "applied_count": applied, "captured_priors": len(priors), "ledgered": True,
                          "new_values": newvals, "ledger_depth": len(_ledger())}
                result["note"] = ("Applied parameter values (from a {name:value} map / "
                    "copy_mutable_parameters output, or copied from source_instance_path via "
                    "copy_parameters_from_instance) and saved. Ledgered NEW op 'mutable_set_params' "
                    "{instance_path, priors:[...]}; inverse re-applies each prior per-type and re-saves.")
                print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def paste_mutable_parameters(ctx, instance_path: str, values=None,
                                 source_instance_path: str = None) -> str:
        """Apply parameter values onto a CustomizableObjectInstance. Ledgered.

        instance_path:        target CustomizableObjectInstance asset path.
        values:               a {param_name: value} dict OR the 'parameters' list produced by
                              copy_mutable_parameters (each {name, type?, value}). BOOL/FLOAT/INT/COLOR
                              only.
        source_instance_path: alternatively, copy ALL parameters from another instance via
                              instance.copy_parameters_from_instance (both must share the CO).

        Provide exactly one of values / source_instance_path. Unknown parameter names and
        set-unsupported types are refused (keeps the inverse faithful). An empty instance / empty
        values changes nothing and writes no ledger entry.

        Ledgered NEW write op 'mutable_set_params' {instance_path, priors:[{name,type,value,
        range_index}]} (shared with reset_mutable_parameters). Inverse: reload, re-apply each prior
        per-type, re-save."""
        params = {"instance_path": instance_path, "values": values,
                  "source_instance_path": source_instance_path}
        try:
            return json.dumps(_exec(_PASTE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_mutable_state — ledger mutable_set_state                         #
    # ------------------------------------------------------------------ #
    _SET_STATE_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
state = PARAMS["state_name"]
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    co = _try(lambda: inst.get_customizable_object())
    valid_states = []
    if co is not None:
        sc = _try(lambda: co.get_state_count(), 0) or 0
        valid_states = [str(_try(lambda i=i: co.get_state_name(i))) for i in range(sc)]
    if valid_states and state not in valid_states:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown state %r; valid states: %s" % (state, valid_states),
            "hint": "use list_mutable_states on the assigned CustomizableObject"}))
    else:
        prior = str(_try(lambda: inst.get_current_state()))
        _try(lambda: inst.set_current_state(state))
        _save_nonvalidating(inst)
        now = str(_try(lambda: inst.get_current_state()))
        _ledger().append({"op": "mutable_set_state", "instance_path": inst.get_path_name(),
                          "prior_state": prior})
        result = {"status": "success", "instance_path": inst.get_path_name(),
                  "prior_state": prior, "current_state": now, "valid_states": valid_states,
                  "ledger_depth": len(_ledger())}
        result["note"] = ("Switched the instance's active runtime STATE (set_current_state) and saved. "
            "Ledgered NEW op 'mutable_set_state' {instance_path, prior_state}; inverse calls "
            "set_current_state(prior_state) and re-saves.")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def set_mutable_state(ctx, instance_path: str, state_name: str) -> str:
        """Set the active runtime STATE on a CustomizableObjectInstance. Ledgered.

        instance_path: CustomizableObjectInstance asset path.
        state_name:    a state name from the assigned CO (list_mutable_states). Unknown states are
                       refused so the inverse stays faithful.

        Calls instance.set_current_state(state_name), then saves. A factory CO exposes one state
        'Default'.

        Ledgered NEW write op 'mutable_set_state' {instance_path, prior_state}. Inverse: reload the
        instance, call set_current_state(prior_state), and re-save."""
        params = {"instance_path": instance_path, "state_name": state_name}
        try:
            return json.dumps(_exec(_SET_STATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # update_mutable_instance — kick off async generation, no ledger       #
    # ------------------------------------------------------------------ #
    _UPDATE_BODY = _MW_HELPERS + r'''
inst_path = PARAMS["instance_path"]
inst, err = _load_inst(inst_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    kicked = _try(lambda: inst.update_skeletal_mesh_async(), "__raised__")
    ok = kicked != "__raised__"
    comp_names = [str(x) for x in (_try(lambda: inst.get_component_names(), []) or [])]
    has_mesh = bool(_try(lambda: inst.has_any_skeletal_mesh(), False))
    result = {"status": "success" if ok else "error", "instance_path": inst.get_path_name(),
              "update_kicked_off": ok, "component_names": comp_names,
              "has_any_skeletal_mesh": has_mesh,
              "current_state": str(_try(lambda: inst.get_current_state()))}
    result["note"] = ("Kicked off asynchronous skeletal-mesh generation "
        "(update_skeletal_mesh_async). This is a TRANSIENT runtime build, not an asset edit -> NO "
        "ledger. Completion (the generated meshes/textures) requires a GPU + running render/streaming "
        "context (PIE or an interactive editor); in a headless/RT-limited context the kick-off returns "
        "but generated resources may not be ready. A factory CO with 0 components produces no mesh "
        "(honest). Poll instance.get_component_mesh_skeletal_mesh after generation in an interactive "
        "session.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def update_mutable_instance(ctx, instance_path: str) -> str:
        """Kick off async skeletal-mesh generation for a CustomizableObjectInstance. No ledger.

        instance_path: CustomizableObjectInstance asset path.

        Calls instance.update_skeletal_mesh_async() to build the runtime mesh/textures from the
        current parameters + state. This is a transient runtime build (not an asset mutation) -> no
        ledger. COMPLETION needs a GPU + render/streaming context (PIE or interactive editor); the
        kick-off returns immediately and generated resources may not be ready in a headless/RT-limited
        run (documented honestly). A factory CO with 0 components generates no mesh."""
        try:
            return json.dumps(_exec(_UPDATE_BODY, {"instance_path": instance_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # UNDO FOLDS FOR THE COORDINATOR (add to editor_level.py `undo`)      #
    # ------------------------------------------------------------------ #
    # This module registers NO `undo` tool. Two ops reuse the existing generic "create_asset" inverse
    # (create_customizable_object, create_customizable_object_instance) -> NO new fold. THREE new ops
    # need folds. Add these branches to editor_level.undo (all restore + non-validating save):
    #
    #   elif op == "mutable_set_param":
    #       inst = load(entry["instance_path"]); co = inst.get_customizable_object()
    #       ty = entry["param_type"]; nm = entry["param_name"]; pv = entry["prior_value"]
    #       SET = {"BOOL":"set_bool_parameter_selected_option","FLOAT":"set_float_parameter_selected_option",
    #              "INT":"set_int_parameter_selected_option","COLOR":"set_color_parameter_selected_option"}
    #       coerce: BOOL->bool(pv); FLOAT->float(pv); INT->str(pv);
    #               COLOR->unreal.LinearColor(pv[0],pv[1],pv[2],pv[3] if len>3 else 1.0)
    #       getattr(inst, SET[ty])(nm, coerced); save_packages([inst.get_outermost()], False)
    #
    #   elif op == "mutable_set_params":   # reset_mutable_parameters + paste_mutable_parameters
    #       inst = load(entry["instance_path"])
    #       for p in entry["priors"]:  # each {name,type,value,range_index}
    #           apply p exactly as mutable_set_param above (same SET table + coerce)
    #       save_packages([inst.get_outermost()], False)
    #
    #   elif op == "mutable_set_state":
    #       inst = load(entry["instance_path"]); inst.set_current_state(entry["prior_state"])
    #       save_packages([inst.get_outermost()], False)
    #
    # (Prior values are stored JSON-plain: bool/float/str/[r,g,b,a]. Only BOOL/FLOAT/INT/COLOR ever
    #  reach these folds -- set/reset/paste refuse other types so the inverse is always well-defined.)
    # ================================================================== #
