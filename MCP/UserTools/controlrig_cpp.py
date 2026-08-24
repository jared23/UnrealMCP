"""UserTools :: Control Rig (C++-backed + modular-rig writes)  (spec: docs/spec/controlrig.md)

Closes the LAST Control-Rig spec features from the C++/runtime bucket that controlrig_write3.py deferred.
Three feature groups, and this module is HONEST about which actually need the C++ plugin:

  (1) get_skeletal_bone_bounds  -> NEEDS C++. Per-bone WEIGHTED bounds from a SkeletalMesh's render
        geometry (position + skin-weight vertex buffers, resolved through each section's BoneMap to the
        RefSkeleton). There is NO Python API for this (stock Python only exposes the whole-mesh bounds).
        Backed by UMCPReflectionLibrary.GetSkeletalBoneBoundsJson (MCPReflection_ControlRig.cpp).
        HASATTR-GUARDED -> INERT until the plugin is rebuilt with that handler. Read-only (no ledger).

  (2) collapse/expand/promote FAITHFUL UNDO  -> NEEDS NO C++ (verified). URigVMController.
        ExportNodesToText / ImportNodesFromText / CanImportNodesFromText are UFUNCTION(BlueprintCallable)
        (RigVMController.h:638-655), so a LOSSLESS snapshot inverse is a pure-Python recipe. This module
        ships NO collapse/expand tool (those FORWARD ops live in controlrig_graph_write.py); instead it
        documents the exact snapshot recipe for the coordinator to FOLD into those existing tools' undo.
        See the SNAPSHOT RECIPE block at the bottom of this file.

  (3) modular-rig writes: add_rig_module / connect_rig_module_connector / auto_connect_rig_modules /
        set_rig_module_config / bind_rig_module_variable / mirror_rig_module  -> NEED NO C++ (verified).
        UModularRigController exposes these ops as UFUNCTION(BlueprintCallable) (ModularRigController.h:
        ConnectConnectorToElement, AutoConnectModules, SetConfigValueInModule[deprecated FString overload],
        BindModuleVariable, MirrorModule, DeleteModule, plus the deprecated AddModule(name,class,parent,undo)).
        The controller is reachable via UControlRigBlueprint.get_modular_rig_controller(), and FRigElementKey /
        FRigVMMirrorSettings are BlueprintType (Python-constructible). So these are Python-NATIVE.
        CAVEAT: the PREFERRED AddModuleFromAssetReference / the newer SetConfigValueInModule(FControlRigOverrideValue)
        / GetPossibleBindings are NOT UFUNCTIONs (not Python-bound); this module uses the BlueprintCallable
        equivalents (add_module by class; the string set_config overload). Drafted as ACTIVE tools
        (controller-guarded, NOT hasattr-guarded). *** DRAFT / UNVERIFIED LIVE *** — the coordinator should
        smoke-test each against a real modular rig + module asset.

Query convention, base64 PARAMS injection, Output-Log auto-capture and the per-session undo ledger are
copied VERBATIM from controlrig_write3.py.

REVERSIBILITY -- NEW op schemas this module introduces (reported to the coordinator to fold into
editor_level.undo; read-only tools ledger nothing):
  add_rig_module              op 'add_rig_module'   {asset_path, module_name}
        inverse = load bp; bp.get_modular_rig_controller().delete_module(module_name, True); save.
  connect_rig_module_connector op 'connect_rig_module_connector'
        {asset_path, connector_name, prior_target_name?, prior_target_type?}
        inverse = reconnect prior target if captured, else disconnect_connector(connector_key). (best-effort)
  auto_connect_rig_modules    op 'auto_connect_rig_modules' {asset_path, module_names}
        inverse = LOSSY: disconnect each listed module's connectors (documented lossy — auto-connect has no
        exact inverse; the pre-state is not captured here).
  set_rig_module_config       op 'set_rig_module_config' {asset_path, module_name, variable_name, had_override, prior_value?}
        inverse = if had_override: set_config_value_in_module(prior_value) else reset_config_value_in_module.
  bind_rig_module_variable    op 'bind_rig_module_variable' {asset_path, module_name, variable_name}
        inverse = un_bind_module_variable(module_name, variable_name, True).
  mirror_rig_module           op 'add_rig_module' {asset_path, module_name}  (REUSES add_rig_module inverse:
        mirror creates a NEW module -> inverse is delete that module).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from controlrig_write3.py) ----------
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
# bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as base64.


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
    _CR_HELPERS = r'''
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
def _load_cr(path):
    if not path:
        return None, "no asset path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    if not isinstance(obj, unreal.ControlRigBlueprint):
        return None, "asset is not a ControlRigBlueprint (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _save_cr(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        return True
    except Exception:
        return False
_ETYPES = {"bone": unreal.RigElementType.BONE, "null": unreal.RigElementType.NULL,
           "control": unreal.RigElementType.CONTROL, "curve": unreal.RigElementType.CURVE,
           "socket": unreal.RigElementType.SOCKET, "connector": unreal.RigElementType.CONNECTOR,
           "reference": unreal.RigElementType.REFERENCE}
def _etype(s):
    return _ETYPES.get(str(s or "").strip().lower())
def _modular_controller(bp):
    # UControlRigBlueprint.get_modular_rig_controller() -> UModularRigController (already used from Python).
    ctrl = _try(lambda: bp.get_modular_rig_controller())
    return ctrl
'''

    # ================================================================== #
    # (1) get_skeletal_bone_bounds  — C++-backed (hasattr-guarded)       #
    # ================================================================== #
    _BONE_BOUNDS_BODY = _CR_HELPERS + r'''
mesh_path = PARAMS.get("mesh_path")
bone_filter = str(PARAMS.get("filter") or "")
weight_mode = str(PARAMS.get("weight_mode") or "dominant")
lod = int(PARAMS.get("lod") or 0)
min_weight = float(PARAMS.get("min_weight") or 0.0)
if not hasattr(unreal, "MCPReflectionLibrary") or not hasattr(unreal.MCPReflectionLibrary, "get_skeletal_bone_bounds_json"):
    print("@@UMCP@@" + json.dumps({"status": "error", "not_built": True,
        "message": "MCPReflectionLibrary.get_skeletal_bone_bounds_json is not present -- rebuild the UnrealMCP plugin (MCPReflection_ControlRig.cpp) first."}))
else:
    mesh = _try(lambda: unreal.EditorAssetLibrary.load_asset(mesh_path))
    if mesh is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "skeletal mesh not found: %s" % mesh_path}))
    elif not isinstance(mesh, unreal.SkeletalMesh):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset is not a SkeletalMesh (got %s): %s" % (mesh.get_class().get_name(), mesh_path)}))
    else:
        raw = unreal.MCPReflectionLibrary.get_skeletal_bone_bounds_json(mesh, bone_filter, weight_mode, lod, min_weight)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"status": "error", "message": "handler returned non-JSON", "raw": raw[:400]}
        print("@@UMCP@@" + json.dumps(parsed))
'''

    @mcp.tool()
    def get_skeletal_bone_bounds(ctx, mesh_path: str, filter: str = None,
                                 weight_mode: str = "dominant", lod: int = 0,
                                 min_weight: float = 0.0) -> str:
        """Per-bone WEIGHTED bounds of a SkeletalMesh's render geometry (READ-ONLY; C++-backed).

        mesh_path:   SkeletalMesh asset path.
        filter:      optional comma-separated case-insensitive substring tokens; only bones whose name
                     contains a token are emitted (e.g. "spine,arm"). Empty = every skinned bone.
        weight_mode: 'dominant' (default) -> each vertex counts ONLY toward its highest-weight bone (tight,
                     non-overlapping boxes, best for gizmo fitting); 'any' -> each vertex counts toward EVERY
                     bone whose weight >= min_weight (looser, overlapping boxes).
        lod:         which LOD's geometry to sample (default 0 = highest detail; clamped to valid range).
        min_weight:  normalized 0..1 influence threshold (default 0).

        For each bone returns component-space AND bone-local bounds {min,max,center,extent,radius}, the
        contributing vertex/influence counts, and the bone's ref-pose location. Computed by iterating the
        skin-weight + position vertex buffers and resolving section-local bone indices through the section
        BoneMap -- geometry the stock Python API cannot reach. INERT (returns not_built) until the UnrealMCP
        plugin is rebuilt with MCPReflection_ControlRig.cpp. Read-only: no ledger entry."""
        params = {"mesh_path": mesh_path, "filter": filter, "weight_mode": weight_mode,
                  "lod": lod, "min_weight": min_weight}
        try:
            return json.dumps(_exec(_BONE_BOUNDS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) add_rig_module — install a MODULE asset onto a modular rig     #
    # ================================================================== #
    _ADD_MODULE_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
module_asset_path = PARAMS.get("module_asset_path")
module_name = str(PARAMS.get("module_name") or "")
parent_module = str(PARAMS.get("parent_module") or "")
bp, err = _load_cr(asset_path)
mod_bp, merr = _load_cr(module_asset_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif merr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "module asset: " + merr}))
elif not module_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "module_name is required"}))
else:
    ctrl = _modular_controller(bp)
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        # NOTE: AddModuleFromAssetReference (the preferred API) is NOT a UFUNCTION -> not Python-bound.
        # The Python-reachable path is the deprecated-but-BlueprintCallable AddModule(name, class, parent, undo),
        # feeding the module blueprint's generated class (a subclass of UControlRig).
        module_class = _try(lambda: mod_bp.generated_class())
        if module_class is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "module '%s' has no generated class (compile it first?)" % module_asset_path}))
        else:
            new_name = _try(lambda: ctrl.add_module(unreal.Name(module_name), module_class, unreal.Name(parent_module), True))
            new_name_s = str(new_name) if new_name is not None else ""
            if not new_name_s or new_name_s.lower() in ("none", "nname_none"):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_module returned None (name clash? invalid parent '%s'? module not connectable?)" % parent_module}))
            else:
                _save_cr(asset_path)
                _ledger().append({"op": "add_rig_module", "asset_path": asset_path, "module_name": new_name_s})
                modules = [str(m) for m in (_try(lambda: ctrl.get_all_modules(), []) or [])]
                connectors = [str(k.name) for k in (_try(lambda: ctrl.get_connectors_for_module(unreal.Name(new_name_s)), []) or [])]
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "module_asset": module_asset_path, "module_name": new_name_s, "parent_module": parent_module or None,
                    "module_count": len(modules), "modules": modules,
                    "connectors": connectors, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_module(ctx, asset_path: str, module_asset_path: str, module_name: str,
                       parent_module: str = "") -> str:
        """Install a Control-Rig MODULE asset onto a MODULAR rig (DRAFT, ledgered, reversible write).

        asset_path:        the MODULAR Control Rig blueprint (target) to install into.
        module_asset_path: the Control Rig MODULE asset to install (must be is_control_rig_module=True).
        module_name:       desired module instance name (the controller may sanitize/uniquify it; the
                           actual name is returned as module_name).
        parent_module:     optional parent module instance name for nesting ('' = top level).

        Uses controller.add_module(name, module_bp.generated_class(), parent, True). (The preferred
        AddModuleFromAssetReference is NOT a UFUNCTION, so it is not Python-bound; add_module is the
        deprecated-but-BlueprintCallable equivalent that takes the module's generated class.) Ledgers op
        'add_rig_module' {asset_path, module_name}; inverse = controller.delete_module. DRAFT/unverified-live:
        confirm against a real modular rig."""
        params = {"asset_path": asset_path, "module_asset_path": module_asset_path,
                  "module_name": module_name, "parent_module": parent_module}
        try:
            return json.dumps(_exec(_ADD_MODULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) connect_rig_module_connector                                   #
    # ================================================================== #
    _CONNECT_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
connector_name = str(PARAMS.get("connector_name") or "")
target_name = str(PARAMS.get("target_name") or "")
target_type = str(PARAMS.get("target_type") or "bone")
bp, err = _load_cr(asset_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not (connector_name and target_name):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "connector_name and target_name are required"}))
else:
    ctrl = _modular_controller(bp)
    tet = _etype(target_type) or unreal.RigElementType.BONE
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        ckey = unreal.RigElementKey(type=unreal.RigElementType.CONNECTOR, name=unreal.Name(connector_name))
        tkey = unreal.RigElementKey(type=tet, name=unreal.Name(target_name))
        ok = bool(_try(lambda: ctrl.connect_connector_to_element(ckey, tkey, True, True, True), False))
        if not ok:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "connect_connector_to_element failed (unknown connector '%s' / target '%s', or invalid connection)" % (connector_name, target_name)}))
        else:
            _save_cr(asset_path)
            _ledger().append({"op": "connect_rig_module_connector", "asset_path": asset_path,
                              "connector_name": connector_name})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "connector": connector_name, "target": target_name, "target_type": target_type,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_rig_module_connector(ctx, asset_path: str, connector_name: str, target_name: str,
                                     target_type: str = "bone") -> str:
        """Connect a module CONNECTOR element to a target element on a modular rig (DRAFT, ledgered).

        asset_path:     the MODULAR Control Rig blueprint.
        connector_name: the CONNECTOR element key NAME (namespaced, e.g. 'ModuleName:Root'). Enumerate a
                        module's connectors via analyze_rig_module_asset / add_rig_module's 'connectors'.
        target_name:    the element to connect it to (usually a bone).
        target_type:    target element type: bone (default) | control | null | socket | curve | reference.

        Calls controller.connect_connector_to_element(connectorKey, targetKey, True, True, True). Ledgers
        op 'connect_rig_module_connector'; inverse = controller.disconnect_connector(connectorKey)
        (best-effort -- the prior target is not captured). DRAFT/unverified-live."""
        params = {"asset_path": asset_path, "connector_name": connector_name,
                  "target_name": target_name, "target_type": target_type}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) auto_connect_rig_modules                                       #
    # ================================================================== #
    _AUTOCONNECT_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
module_names = PARAMS.get("module_names") or []
replace_existing = bool(PARAMS.get("replace_existing", True))
bp, err = _load_cr(asset_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ctrl = _modular_controller(bp)
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        names = [str(n) for n in module_names] if module_names else [str(m) for m in (_try(lambda: ctrl.get_all_modules(), []) or [])]
        name_fn = [unreal.Name(n) for n in names]
        ok = bool(_try(lambda: ctrl.auto_connect_modules(name_fn, replace_existing, True), False))
        if not ok:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "auto_connect_modules returned False (no resolvable secondary connectors, or names not found)", "modules": names}))
        else:
            _save_cr(asset_path)
            _ledger().append({"op": "auto_connect_rig_modules", "asset_path": asset_path, "module_names": names})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "modules": names, "replace_existing": replace_existing,
                "note": "LOSSY undo: auto-connect has no exact inverse; the inverse disconnects these modules' connectors.",
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def auto_connect_rig_modules(ctx, asset_path: str, module_names: list = None,
                                 replace_existing: bool = True) -> str:
        """Auto-resolve secondary connectors for modules on a modular rig (DRAFT, ledgered, LOSSY undo).

        asset_path:       the MODULAR Control Rig blueprint.
        module_names:     modules to auto-connect (omit/empty = every module on the rig).
        replace_existing: replace already-resolved connections (default True).

        Calls controller.auto_connect_modules([names], replace_existing, True). Ledgers op
        'auto_connect_rig_modules'; the inverse is LOSSY (disconnects the listed modules' connectors -- the
        pre-connection state is not captured). DRAFT/unverified-live."""
        params = {"asset_path": asset_path, "module_names": module_names or [],
                  "replace_existing": replace_existing}
        try:
            return json.dumps(_exec(_AUTOCONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) set_rig_module_config                                          #
    # ================================================================== #
    _SETCONFIG_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
module_name = str(PARAMS.get("module_name") or "")
variable_name = str(PARAMS.get("variable_name") or "")
value = str(PARAMS.get("value") if PARAMS.get("value") is not None else "")
bp, err = _load_cr(asset_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not (module_name and variable_name):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "module_name and variable_name are required"}))
else:
    ctrl = _modular_controller(bp)
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        # BlueprintCallable (deprecated) string overload: set_config_value_in_module(module, variable, value, undo).
        ok = bool(_try(lambda: ctrl.set_config_value_in_module(unreal.Name(module_name), unreal.Name(variable_name), value, True), False))
        if not ok:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_config_value_in_module returned False (unknown module '%s' / variable '%s' / unparseable value)" % (module_name, variable_name)}))
        else:
            _save_cr(asset_path)
            _ledger().append({"op": "set_rig_module_config", "asset_path": asset_path,
                              "module_name": module_name, "variable_name": variable_name, "had_override": False})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "module_name": module_name, "variable_name": variable_name, "value": value,
                "note": "undo resets this config override (prior value not captured).",
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_module_config(ctx, asset_path: str, module_name: str, variable_name: str, value: str) -> str:
        """Set a module CONFIG variable override on a modular rig (DRAFT, ledgered).

        asset_path:    the MODULAR Control Rig blueprint.
        module_name:   the module instance name.
        variable_name: the config variable to override.
        value:         the new value as a UE-exported string (scalar or struct ExportText form).

        Calls controller.set_config_value_in_module(module, variable, value, True) (the BlueprintCallable
        string overload). Ledgers op 'set_rig_module_config'; inverse = controller.reset_config_value_in_module
        (prior value is not captured, so undo clears the override). DRAFT/unverified-live."""
        params = {"asset_path": asset_path, "module_name": module_name,
                  "variable_name": variable_name, "value": value}
        try:
            return json.dumps(_exec(_SETCONFIG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) bind_rig_module_variable                                       #
    # ================================================================== #
    _BINDVAR_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
module_name = str(PARAMS.get("module_name") or "")
variable_name = str(PARAMS.get("variable_name") or "")
source_path = str(PARAMS.get("source_path") or "")
bp, err = _load_cr(asset_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not (module_name and variable_name and source_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "module_name, variable_name and source_path are required"}))
else:
    ctrl = _modular_controller(bp)
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        ok = bool(_try(lambda: ctrl.bind_module_variable(unreal.Name(module_name), unreal.Name(variable_name), source_path, True), False))
        if not ok:
            poss = [str(p) for p in (_try(lambda: ctrl.get_possible_bindings(unreal.Name(module_name), unreal.Name(variable_name)), []) or [])]
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "bind_module_variable returned False (bad module/variable, or incompatible source '%s')" % source_path, "possible_bindings": poss[:50]}))
        else:
            _save_cr(asset_path)
            _ledger().append({"op": "bind_rig_module_variable", "asset_path": asset_path,
                              "module_name": module_name, "variable_name": variable_name})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "module_name": module_name, "variable_name": variable_name, "source_path": source_path,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def bind_rig_module_variable(ctx, asset_path: str, module_name: str, variable_name: str,
                                 source_path: str) -> str:
        """Bind a module variable to a source (another module's variable) on a modular rig (DRAFT, ledgered).

        asset_path:    the MODULAR Control Rig blueprint.
        module_name:   the module instance name whose variable is being bound.
        variable_name: the module variable to bind.
        source_path:   the bind source path (e.g. 'OtherModule:Variable'); on failure the tool returns the
                       controller's get_possible_bindings list to help.

        Calls controller.bind_module_variable(module, variable, source_path, True). Ledgers op
        'bind_rig_module_variable'; inverse = controller.un_bind_module_variable(module, variable, True).
        DRAFT/unverified-live."""
        params = {"asset_path": asset_path, "module_name": module_name,
                  "variable_name": variable_name, "source_path": source_path}
        try:
            return json.dumps(_exec(_BINDVAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # (3) mirror_rig_module                                              #
    # ================================================================== #
    _MIRROR_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
module_name = str(PARAMS.get("module_name") or "")
mirror_axis = str(PARAMS.get("mirror_axis") or "X").upper()
flip_axis = str(PARAMS.get("flip_axis") or "Z").upper()
search = str(PARAMS.get("search") or "")
replace = str(PARAMS.get("replace") or "")
bp, err = _load_cr(asset_path)
_AXT = getattr(unreal, "AxisType", None)
_AXIS = {"X": getattr(_AXT, "X", None), "Y": getattr(_AXT, "Y", None), "Z": getattr(_AXT, "Z", None)} if _AXT is not None else {}
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not module_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "module_name is required"}))
else:
    ctrl = _modular_controller(bp)
    if ctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no modular rig controller (is '%s' a MODULAR control rig?)" % asset_path}))
    else:
        settings = unreal.RigVMMirrorSettings()
        # Enum property names are best-effort; struct still valid if the enum set fails.
        _try(lambda: settings.set_editor_property("mirror_axis", _AXIS.get(mirror_axis)) if _AXIS.get(mirror_axis) is not None else None)
        _try(lambda: settings.set_editor_property("axis_to_flip", _AXIS.get(flip_axis)) if _AXIS.get(flip_axis) is not None else None)
        _try(lambda: settings.set_editor_property("search_string", search))
        _try(lambda: settings.set_editor_property("replace_string", replace))
        new_name = _try(lambda: ctrl.mirror_module(unreal.Name(module_name), settings, True))
        new_name_s = str(new_name) if new_name is not None else ""
        if not new_name_s or new_name_s.lower() in ("none", "nname_none"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "mirror_module returned None (unknown module '%s', or search/replace produced no distinct name)" % module_name}))
        else:
            _save_cr(asset_path)
            # REUSES the add_rig_module inverse (delete the mirrored module).
            _ledger().append({"op": "add_rig_module", "asset_path": asset_path, "module_name": new_name_s})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "source_module": module_name, "mirrored_module": new_name_s,
                "mirror_axis": mirror_axis, "flip_axis": flip_axis, "search": search, "replace": replace,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def mirror_rig_module(ctx, asset_path: str, module_name: str, mirror_axis: str = "X",
                          flip_axis: str = "Z", search: str = "", replace: str = "") -> str:
        """Mirror a module on a modular rig, producing a new mirrored module (DRAFT, ledgered, reversible).

        asset_path:  the MODULAR Control Rig blueprint.
        module_name: the module instance to mirror.
        mirror_axis: axis to mirror against: X (default) | Y | Z.
        flip_axis:   axis to flip for rotations: Z (default) | X | Y.
        search/replace: name substring swap for the new module (e.g. 'Left' -> 'Right').

        Builds an FRigVMMirrorSettings and calls controller.mirror_module(module, settings, True). Ledgers
        op 'add_rig_module' with the NEW module name (mirror creates a module, so the inverse is delete_module
        -- reuses the add_rig_module inverse). Axis enum = unreal.AxisType (X/Y/Z); getattr-guarded so a
        missing member degrades to leaving the settings default rather than raising."""
        params = {"asset_path": asset_path, "module_name": module_name, "mirror_axis": mirror_axis,
                  "flip_axis": flip_axis, "search": search, "replace": replace}
        try:
            return json.dumps(_exec(_MIRROR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================================================ #
    # SNAPSHOT RECIPE (spec feature 2) — collapse/expand/promote FAITHFUL UNDO, no C++ required.        #
    # ------------------------------------------------------------------------------------------------ #
    # URigVMController exposes (all UFUNCTION(BlueprintCallable), RigVMController.h):                   #
    #   export_nodes_to_text(node_names: [str], include_exterior_links: bool) -> str                   #
    #   can_import_nodes_from_text(text: str) -> bool                                                   #
    #   import_nodes_from_text(text: str, setup_undo: bool, print_python: bool) -> [FName]              #
    #   collapse_nodes(node_names, name, setup_undo, print_python, is_aggregate) -> URigVMCollapseNode  #
    #   expand_library_node(node_name, setup_undo, print_python) -> [URigVMNode]                        #
    #   promote_collapse_node_to_function_reference_node(node_name, ...) -> FName                       #
    #   promote_function_reference_node_to_collapse_node(node_name, ...) -> FName                       #
    #                                                                                                   #
    # FAITHFUL (lossless) inverse pattern to FOLD into controlrig_graph_write.py's collapse/expand/     #
    # promote tools (get the graph's controller via bp.get_controller_by_name(graph) or                #
    # bp.get_controller()):                                                                             #
    #                                                                                                   #
    #   # BEFORE collapse_nodes(names):                                                                 #
    #   snapshot = controller.export_nodes_to_text(names, True)   # True = include exterior links       #
    #   collapse = controller.collapse_nodes(names, desired_name, True)                                 #
    #   ledger op 'collapse_rig_vm_nodes' {asset_path, graph, collapse_node_name: collapse.get_node_path#
    #             ().split('.')[-1], snapshot}                                                           #
    #                                                                                                   #
    #   # INVERSE (undo) — remove the produced collapse node, then re-import the original subgraph:     #
    #   controller.remove_node_by_name(unreal.Name(collapse_node_name), True, False)  # (Name, undo, py) #
    #   controller.import_nodes_from_text(snapshot, True)    # restores nodes+pins+defaults+positions+  #
    #                                                        # links EXACTLY (exterior links included)  #
    #                                                                                                   #
    # expand_library_node inverse mirrors it: snapshot the library node itself before expanding         #
    #   (export_nodes_to_text([lib_node], True)); on undo remove the expanded child nodes (the FName    #
    #   array expand_library_node returned) and import_nodes_from_text(snapshot). promote_* is a rename-#
    #   in-place: snapshot before, and on undo remove the promoted node + import the snapshot.           #
    #                                                                                                   #
    # This is LOSSLESS (positions, pin defaults, internal + exterior links all round-trip through the   #
    # text), so NO C++ snapshot helper is needed. Recommend the coordinator fold these snapshots into   #
    # the existing forward-op tools rather than adding new tools here.                                  #
    # ================================================================================================ #
