"""UserTools :: Control Rig (WRITE, tranche 2)  (spec: docs/spec/controlrig.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). A THIRD Control-Rig module
alongside controlrig_write.py (HIERARCHY authoring) and controlrig_graph_write.py (RigVM GRAPH node
authoring). This module fills the remaining Python-reachable [PY] gaps: rig CREATION (factory, never
duplicate), RigVM FUNCTION-library authoring, batch graph build / auto-layout, in-graph node search,
modular/validation/export READS, element selection, and the autosave setting.

Query convention, base64 PARAMS injection, Output-Log auto-capture and the per-session undo ledger
are copied VERBATIM from the gold-standard editor_level.py / controlrig_write.py.

CRASH-SAFETY (learned the hard way, see .mcp_coord/PROTOCOL.md + memory):
  * NEVER duplicate_asset a ControlRigBlueprint -> UControlRigBlueprint::PostDuplicate hard-crash.
    All creation goes through AssetToolsHelpers.get_asset_tools().create_asset(ControlRigBlueprintFactory).
  * create_* tools are NOT wrapped in a ScopedEditorTransaction (that traps the new asset in the undo
    buffer and blocks the inverse-delete); they SAVE on creation and ledger op 'create_asset' (whose
    faithful inverse -- delete the asset -- already lives in editor_level.undo).
  * No find_all_object_of_class / no protected-internal probing / compile+save kept out of tight
    mutate loops.

REVERSIBILITY (every WRITE ledgers a faithful inverse; this module registers NO `undo` tool --
editor_level.py owns the ONE unified `undo`; new op schemas are reported to the coordinator to fold):
  - create_control_rig / create_control_rig_from_skeleton -> op 'create_asset'
        {asset_path, package_path, created_dir} ; inverse = delete the asset (already folded).
  - set_rig_preview_mesh -> NEW op 'set_rig_preview_mesh'
        {asset_path, prior_mesh_path} ; inverse = set_preview_mesh(load(prior) or None) + save.
  - create_rig_vm_function -> NEW op 'create_rig_vm_function'
        {asset_path, function_name} ; inverse = function-library controller remove_function_from_library + save.
  - add_rig_vm_function_node -> op 'add_rig_vm_node' (REUSED; already folded)
        {asset_path, graph_name, node_name} ; inverse = remove_node_by_name.
  - build_rig_vm_graph -> REUSES ops 'add_rig_vm_node' + 'add_rig_vm_link' (already folded; one ledger
        entry per created node/link, undone LIFO).
  - layout_rig_vm_graph -> REUSES op 'set_rig_vm_node_position' (already folded; one entry per moved node).
  - select_rig_elements -> NEW op 'select_rig_elements'
        {asset_path, prior_keys:[{name,type}]} ; inverse = restore the prior selection set.
  - set_rig_autosave -> NEW op 'set_rig_autosave'
        {asset_path, prior_value} ; inverse = set_auto_vm_recompile(prior) + save.

READ-ONLY (no ledger): search_rig_vm_nodes, list_rig_modules, list_rig_module_assets,
  validate_rig_controls, validate_rig_graph, validate_rig_gizmos, export_control_rig.

DEFERRED / NOT shipped here (documented, not faked):
  - collapse_rig_vm_nodes / promote_rig_vm_node / expand_rig_vm_node: graph-RESTRUCTURING ops.
    collapse<->expand round-trips rename nodes and drop exact per-node layout/pin state, so a single
    op has no faithful inverse. (The URigVMController methods exist -- collapse_nodes / expand_library_node
    / promote_* -- but shipping them reversibly needs a captured full-subgraph snapshot: a later C++/
    snapshot batch.)
  - Modular-rig WRITES (add_rig_module / connect_rig_module_connector / auto_connect_rig_modules / ...):
    driven by UModularRigController on a module ASSET; the connector/binding surface is broad and the
    scratch fixture is non-modular -> C++/blocked batch. list_rig_modules / list_rig_module_assets are
    shipped as READS.
  - Physics/deformation/penetration/bone-bounds, motion-capture/simulate/profile, play/stop preview
    animation, get_rig_pose: need skeletal-mesh GEOMETRY or a runtime/PIE evaluation of the compiled VM
    -> the C++/runtime-probe bucket.
  - place_rig_pole_vector / fit_rig_control_shapes / add_rig_jiggle_bob: geometry-dependent placement
    (pole vectors and gizmo fitting want real bone/mesh geometry to be meaningful) -> deferred with the
    geometry bucket.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


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
def _split_path(asset_path):
    s = str(asset_path or "")
    if not s.startswith("/"):
        return None, None
    if "." in s.split("/")[-1]:
        s = s.rsplit(".", 1)[0]
    if "/" not in s:
        return None, None
    pkg, name = s.rsplit("/", 1)
    if not pkg or not name:
        return None, None
    return pkg, name
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if aes and obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _etype_short(k):
    return str(k.type).split(".")[-1].split(":")[0].strip()
_ETYPES = {"bone": unreal.RigElementType.BONE, "null": unreal.RigElementType.NULL,
           "control": unreal.RigElementType.CONTROL, "curve": unreal.RigElementType.CURVE,
           "socket": unreal.RigElementType.SOCKET, "connector": unreal.RigElementType.CONNECTOR,
           "reference": unreal.RigElementType.REFERENCE}
def _etype(s):
    return _ETYPES.get(str(s or "").strip().lower())
def _ckey(name, etype_str):
    et = _etype(etype_str) or unreal.RigElementType.CONTROL
    return unreal.RigElementKey(name=unreal.Name(str(name)), type=et)
def _get_controller(bp, graph_name):
    if not graph_name:
        c = _try(lambda: bp.get_controller())
        m = _try(lambda: bp.get_default_model())
        if c is None:
            return None, None, "no default RigVM controller on this asset"
        return c, m, None
    models = _try(lambda: bp.get_all_models(), []) or []
    want = str(graph_name).strip().lower()
    names = []
    tgt = None
    for g in models:
        nm = str(_try(lambda g=g: g.get_graph_name()))
        names.append(nm)
        if nm.lower() == want:
            tgt = g; break
    if tgt is None:
        lib = _try(lambda: bp.get_local_function_library())
        for fn in (_try(lambda: lib.get_nodes(), []) or []):
            cg = _try(lambda fn=fn: fn.get_contained_graph())
            if cg is None:
                continue
            nm = str(_try(lambda cg=cg: cg.get_graph_name()))
            names.append(nm)
            if nm.lower() == want:
                tgt = cg; break
    if tgt is None:
        return None, None, "no RigVM graph named %r (available: %s)" % (graph_name, names)
    c = _try(lambda: bp.get_controller(tgt))
    if c is None:
        return None, None, "could not obtain controller for graph %r" % graph_name
    return c, tgt, None
def _model_of(ctrl, bp, model):
    if model is not None:
        return model
    return _try(lambda: ctrl.get_graph()) or _try(lambda: bp.get_default_model())
def _find_node(model, node_name):
    if model is None:
        return None
    want = str(node_name)
    for n in (_try(lambda: model.get_nodes(), []) or []):
        if str(_try(lambda n=n: n.get_node_path())) == want or str(_try(lambda n=n: n.get_name())) == want:
            return n
    return None
def _resolve_struct_path(bp, name_or_path):
    s = str(name_or_path or "").strip()
    if not s:
        return None
    if "." in s or "/" in s:
        return s
    for st in (_try(lambda: bp.get_available_rig_vm_structs(), []) or []):
        nm = _try(lambda st=st: st.get_name())
        if nm and str(nm) == s:
            return str(_try(lambda st=st: st.get_path_name()))
    return None
def _pos2d(v):
    if not v:
        return unreal.Vector2D(0.0, 0.0)
    return unreal.Vector2D(float(v[0]), float(v[1]))
def _pos_list(p):
    if p is None:
        return None
    return [round(float(p.x), 2), round(float(p.y), 2)]
'''

    # ================================================================== #
    # create_control_rig — factory create (NEVER duplicate)              #
    # ================================================================== #
    _CREATE_CR_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
modular = bool(PARAMS.get("modular", False))
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
package_path, name = _split_path(asset_path)
if package_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path must be a full path like /Game/Dir/Name"}))
elif EAL.does_asset_exist(asset_path.rsplit(".", 1)[0] if "." in str(asset_path).split("/")[-1] else asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    fac = unreal.ControlRigBlueprintFactory()
    obj = at.create_asset(name, package_path, unreal.ControlRigBlueprint, fac)
    if obj is None or not isinstance(obj, unreal.ControlRigBlueprint):
        if created_dir and EAL.does_directory_exist(package_path):
            _try(lambda: EAL.delete_directory(package_path))
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        made_module = False
        if modular:
            made_module = bool(_try(lambda: (obj.turn_into_control_rig_module(), True)[1], False))
        full = package_path + "/" + name
        _try(lambda: EAL.save_asset(full, only_if_is_dirty=False))
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": package_path, "created_dir": created_dir})
        models = _try(lambda: obj.get_all_models(), []) or []
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": full, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "modular_requested": modular, "made_module": made_module,
            "models": [str(_try(lambda m=m: m.get_graph_name())) for m in models],
            "element_count": _try(lambda: obj.get_hierarchy().num(), 0),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_control_rig(ctx, asset_path: str, modular: bool = False) -> str:
        """Create a NEW, empty Control Rig Blueprint asset via the factory (ledgered, reversible write).

        asset_path: full destination path '/Game/Dir/Name' (an object suffix, if given, is stripped).
        modular:    True to additionally turn it into a Control Rig MODULE (turn_into_control_rig_module);
                    default False = a standard Control Rig.

        Created via AssetToolsHelpers.get_asset_tools().create_asset with ControlRigBlueprintFactory --
        NEVER via duplicate_asset (duplicating a ControlRigBlueprint hard-crashes the editor). The asset
        is saved on creation. Ledgered op 'create_asset' {asset_path,package_path,created_dir}; the inverse
        (delete the created asset) already lives in editor_level.undo. Author its hierarchy with
        add_rig_bone/null/control (controlrig_write.py) and its solve graph with the RigVM tools."""
        params = {"asset_path": asset_path, "modular": modular}
        try:
            return json.dumps(_exec(_CREATE_CR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_control_rig_from_skeleton — create + import skeleton bones  #
    # ================================================================== #
    _CREATE_FROM_SKEL_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
skeleton_path = PARAMS.get("skeleton_path")
import_curves = bool(PARAMS.get("import_curves", True))
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
package_path, name = _split_path(asset_path)
skel = _try(lambda: EAL.load_asset(skeleton_path)) if skeleton_path else None
if package_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path must be a full path like /Game/Dir/Name"}))
elif not skeleton_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide skeleton_path (a Skeleton or SkeletalMesh asset)"}))
elif skel is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "skeleton/skeletal-mesh not found: %s" % skeleton_path}))
elif not isinstance(skel, (unreal.Skeleton, unreal.SkeletalMesh)):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "skeleton_path must be a Skeleton or SkeletalMesh (got %s)" % skel.get_class().get_name()}))
elif EAL.does_asset_exist(asset_path.rsplit(".", 1)[0] if "." in str(asset_path).split("/")[-1] else asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    fac = unreal.ControlRigBlueprintFactory()
    obj = at.create_asset(name, package_path, unreal.ControlRigBlueprint, fac)
    if obj is None or not isinstance(obj, unreal.ControlRigBlueprint):
        if created_dir and EAL.does_directory_exist(package_path):
            _try(lambda: EAL.delete_directory(package_path))
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        hc = obj.get_hierarchy_controller()
        h = obj.get_hierarchy()
        is_mesh = isinstance(skel, unreal.SkeletalMesh)
        bones = []
        curves = []
        if is_mesh:
            bones = _try(lambda: hc.import_bones_from_skeletal_mesh(skel, "None", True, True, False, False), []) or []
            _try(lambda: obj.set_preview_mesh(skel, True))
            if import_curves:
                curves = _try(lambda: hc.import_curves_from_skeletal_mesh(skel, "None", False, False), []) or []
        else:
            bones = _try(lambda: hc.import_bones(skel, "None", True, True, False, False, False), []) or []
            if import_curves:
                curves = _try(lambda: hc.import_curves(skel, "None", False, False), []) or []
        full = package_path + "/" + name
        _try(lambda: EAL.save_asset(full, only_if_is_dirty=False))
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": full, "object_path": obj.get_path_name(),
            "source": skeleton_path, "source_kind": ("skeletal_mesh" if is_mesh else "skeleton"),
            "preview_mesh_set": is_mesh, "imported_bone_count": len(bones), "imported_curve_count": len(curves),
            "element_count": _try(lambda: h.num(), 0), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_control_rig_from_skeleton(ctx, asset_path: str, skeleton_path: str,
                                         import_curves: bool = True) -> str:
        """Create a Control Rig and import a Skeleton/SkeletalMesh's bone hierarchy into it
        (ledgered, reversible write).

        asset_path:    full destination path '/Game/Dir/Name'.
        skeleton_path: a Skeleton OR SkeletalMesh asset path. A SkeletalMesh additionally sets the rig's
                       preview mesh; a bare Skeleton imports the ref-skeleton bones only.
        import_curves: True (default) also imports the skeleton's animation curves as rig curve elements.

        Created via ControlRigBlueprintFactory (NEVER duplicate_asset), then the ref-skeleton bones are
        imported via RigHierarchyController.import_bones / import_bones_from_skeletal_mesh. The asset is
        saved. Ledgered op 'create_asset' {asset_path,package_path,created_dir}; the inverse (delete the
        whole created asset) already lives in editor_level.undo."""
        params = {"asset_path": asset_path, "skeleton_path": skeleton_path, "import_curves": import_curves}
        try:
            return json.dumps(_exec(_CREATE_FROM_SKEL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_rig_preview_mesh — set the CR's preview skeletal mesh          #
    # ================================================================== #
    _SET_PREVIEW_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
mesh_path = PARAMS.get("mesh_path")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    mesh = None
    if mesh_path:
        mesh = _try(lambda: unreal.EditorAssetLibrary.load_asset(mesh_path))
        if mesh is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "mesh not found: %s" % mesh_path}))
            mesh = "MISS"
        elif not isinstance(mesh, unreal.SkeletalMesh):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "mesh_path must be a SkeletalMesh (got %s)" % mesh.get_class().get_name()}))
            mesh = "MISS"
    if mesh != "MISS":
        prior = _try(lambda: bp.get_preview_mesh())
        prior_path = str(prior.get_path_name()) if prior is not None else None
        with unreal.ScopedEditorTransaction("MCP set_rig_preview_mesh"):
            bp.set_preview_mesh(mesh, True)
        _ledger().append({"op": "set_rig_preview_mesh", "asset_path": path, "prior_mesh_path": prior_path})
        _save_cr(path)
        after = _try(lambda: bp.get_preview_mesh())
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "prior_mesh": prior_path, "new_mesh": (str(after.get_path_name()) if after is not None else None),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_preview_mesh(ctx, asset_path: str, mesh_path: str = None) -> str:
        """Set (or clear) a Control Rig's PREVIEW skeletal mesh (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        mesh_path:  a SkeletalMesh asset path to preview against; omit/None to CLEAR the preview mesh.

        The preview mesh is what the Control Rig editor poses/draws against (it does not change the rig
        hierarchy). Set via UControlRigBlueprint.set_preview_mesh. The prior preview mesh is captured so
        the inverse restores it exactly. The asset is saved. Ledgered op 'set_rig_preview_mesh'
        {asset_path,prior_mesh_path}."""
        params = {"asset_path": asset_path, "mesh_path": mesh_path}
        try:
            return json.dumps(_exec(_SET_PREVIEW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_rig_vm_function — add a function to the local library       #
    # ================================================================== #
    _CREATE_FUNC_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
fname = PARAMS.get("name")
mutable = bool(PARAMS.get("mutable", False))
is_public = bool(PARAMS.get("is_public", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not fname:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a function name"}))
else:
    lib = _try(lambda: bp.get_local_function_library())
    libctrl = _try(lambda: bp.get_controller(lib)) if lib is not None else None
    if libctrl is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not obtain the function-library controller"}))
    else:
        existing = [str(_try(lambda n=n: n.get_node_path())) for n in (_try(lambda: lib.get_functions(), []) or [])]
        node = None
        addhint = ""
        try:
            with unreal.ScopedEditorTransaction("MCP create_rig_vm_function"):
                node = libctrl.add_function_to_library(unreal.Name(str(fname)), mutable, unreal.Vector2D(0.0, 0.0), True, False)
        except Exception as _e:
            addhint = str(_e)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "add_function_to_library returned None for %r" % fname, "hint": addhint}))
        else:
            real = str(_try(lambda: node.get_node_path()) or fname)
            if is_public:
                _try(lambda: libctrl.mark_function_as_public(unreal.Name(real), True, False))
            _ledger().append({"op": "create_rig_vm_function", "asset_path": path, "function_name": real})
            _save_cr(path)
            cg = _try(lambda: node.get_contained_graph())
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "function_name": real, "node_class": type(node).__name__, "mutable": mutable,
                "is_public": bool(_try(lambda: libctrl.is_function_public(unreal.Name(real)), is_public)),
                "contained_graph": str(_try(lambda: cg.get_graph_name())) if cg is not None else None,
                "function_count": len(_try(lambda: lib.get_functions(), []) or []),
                "prior_functions": existing, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_rig_vm_function(ctx, asset_path: str, name: str, mutable: bool = False,
                               is_public: bool = False) -> str:
        """Create a new RigVM FUNCTION in a Control Rig's local function library (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        name:       the new function's name (adjusted on collision; the real name is returned).
        mutable:    True makes it a mutable (execute-context) function; False (default) a pure function.
        is_public:  True marks it public (usable from other assets); default False (local).

        Added via URigVMController.add_function_to_library on the function-library controller. The function
        gets its own contained graph (author nodes into it with graph_name = the returned contained_graph
        via the RigVM node tools, and place call sites with add_rig_vm_function_node). The asset is saved.
        Ledgered op 'create_rig_vm_function' {asset_path,function_name}; inverse = remove_function_from_library."""
        params = {"asset_path": asset_path, "name": name, "mutable": mutable, "is_public": is_public}
        try:
            return json.dumps(_exec(_CREATE_FUNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_rig_vm_function_node — place a function-reference (call) node   #
    # ================================================================== #
    _ADD_FUNCNODE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
fname = PARAMS.get("function_name")
node_name = PARAMS.get("node_name") or ""
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not fname:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide function_name (an existing library function)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        lib = _try(lambda: bp.get_local_function_library())
        fnode = _try(lambda: lib.find_function(unreal.Name(str(fname)))) if lib is not None else None
        if fnode is None:
            avail = [str(_try(lambda n=n: n.get_node_path())) for n in (_try(lambda: lib.get_functions(), []) or [])] if lib is not None else []
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no library function named %r" % fname, "available_functions": avail}))
        else:
            model = _model_of(ctrl, bp, model)
            gname = str(_try(lambda: model.get_graph_name()))
            pos = _pos2d(PARAMS.get("position"))
            node = None
            addhint = ""
            try:
                with unreal.ScopedEditorTransaction("MCP add_rig_vm_function_node"):
                    node = ctrl.add_function_reference_node(fnode, pos, str(node_name), True, False)
            except Exception as _e:
                addhint = str(_e)
            if node is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "add_function_reference_node returned None for %r" % fname, "hint": addhint}))
            else:
                nn = str(_try(lambda: node.get_node_path()))
                _ledger().append({"op": "add_rig_vm_node", "asset_path": path,
                                  "graph_name": gname, "node_name": nn})
                _save_cr(path)
                pins = [str(_try(lambda p=p: p.get_name())) for p in (_try(lambda: node.get_pins(), []) or [])]
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "node_name": nn, "node_class": type(node).__name__,
                    "function_name": str(fname), "title": str(_try(lambda: node.get_node_title())),
                    "position": _pos_list(_try(lambda: node.get_position())), "pins": pins,
                    "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_function_node(ctx, asset_path: str, function_name: str, position: list = None,
                                 node_name: str = None, graph_name: str = None) -> str:
        """Place a function-reference (call) node for an existing library function on a Control Rig
        solve graph (ledgered, reversible write).

        asset_path:    ControlRigBlueprint path.
        function_name: name of a function already in the local library (see create_rig_vm_function).
        position:      [x, y] canvas position (default [0,0]).
        node_name:     suggested node name (graph may adjust; real name returned).
        graph_name:    which model/graph to place the call in (default = the default model).

        Resolves the library function via RigVMFunctionLibrary.find_function, then adds a reference node
        via URigVMController.add_function_reference_node. The asset is saved. Ledgered op 'add_rig_vm_node'
        {asset_path,graph_name,node_name} (same schema/inverse as other node adds -> remove_node_by_name)."""
        params = {"asset_path": asset_path, "function_name": function_name, "position": position,
                  "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_FUNCNODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # build_rig_vm_graph — batch add unit nodes + links                 #
    # ================================================================== #
    _BUILD_GRAPH_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
nodes = PARAMS.get("nodes") or []
connections = PARAMS.get("connections") or []
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(nodes, list) or not nodes:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide nodes: a non-empty list of {struct, node_name?, position?, method?} specs"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        auto = bool(PARAMS.get("auto_layout", False))
        spacing_x = float(PARAMS.get("spacing_x", 350.0))
        created = []
        node_errors = []
        link_results = []
        with unreal.ScopedEditorTransaction("MCP build_rig_vm_graph"):
            for i, spec in enumerate(nodes):
                struct = spec.get("struct")
                if not struct:
                    node_errors.append({"index": i, "error": "missing struct"}); continue
                sp = _resolve_struct_path(bp, struct)
                if not sp:
                    node_errors.append({"index": i, "struct": struct, "error": "could not resolve struct"}); continue
                method = spec.get("method") or "Execute"
                if auto and not spec.get("position"):
                    pos = unreal.Vector2D(float(i) * spacing_x, 0.0)
                else:
                    pos = _pos2d(spec.get("position"))
                nm = spec.get("node_name") or ""
                node = _try(lambda sp=sp, method=method, pos=pos, nm=nm: ctrl.add_unit_node_from_struct_path(sp, method, pos, nm, True, False))
                if node is None:
                    node_errors.append({"index": i, "struct": struct, "error": "add_unit_node_from_struct_path returned None"}); continue
                nn = str(_try(lambda node=node: node.get_node_path()))
                created.append({"index": i, "node_name": nn, "requested": nm})
                _ledger().append({"op": "add_rig_vm_node", "asset_path": path, "graph_name": gname, "node_name": nn})
            for j, conn in enumerate(connections):
                op = conn.get("output_pin") or conn.get("from")
                ip = conn.get("input_pin") or conn.get("to")
                if not op or not ip:
                    link_results.append({"index": j, "error": "missing output_pin/input_pin"}); continue
                ip_pin = _try(lambda ip=ip: model.find_pin(str(ip)))
                prior_sources = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda ip_pin=ip_pin: ip_pin.get_linked_source_pins(), []) or [])] if ip_pin is not None else []
                if str(op) in prior_sources:
                    link_results.append({"index": j, "output_pin": str(op), "input_pin": str(ip), "result": "already-linked"}); continue
                ok = _try(lambda op=op, ip=ip: ctrl.add_link(str(op), str(ip), True, False), False)
                ip2 = _try(lambda ip=ip: model.find_pin(str(ip)))
                after = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda ip2=ip2: ip2.get_linked_source_pins(), []) or [])] if ip2 is not None else []
                if str(op) in after:
                    _ledger().append({"op": "add_rig_vm_link", "asset_path": path, "graph_name": gname,
                                      "output_pin": str(op), "input_pin": str(ip), "prior_sources": prior_sources})
                    link_results.append({"index": j, "output_pin": str(op), "input_pin": str(ip), "result": "linked"})
                else:
                    link_results.append({"index": j, "output_pin": str(op), "input_pin": str(ip), "result": "failed", "ok": bool(ok)})
        _save_cr(path)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "graph_name": gname, "created_count": len(created), "created": created,
            "node_errors": node_errors, "links": link_results,
            "node_count": len(_try(lambda: model.get_nodes(), []) or []), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def build_rig_vm_graph(ctx, asset_path: str, nodes: list, connections: list = None,
                           auto_layout: bool = False, spacing_x: float = 350.0,
                           graph_name: str = None) -> str:
        """Batch-build a RigVM solve graph -- add many unit nodes and wire them -- in one call
        (ledgered, reversible write).

        asset_path:  ControlRigBlueprint path.
        nodes:       list of node specs. Each: {struct, node_name?, position?, method?}. 'struct' is a
                     RigVM node struct name (see list_control_rig_node_types) or full object path.
        connections: optional list of link specs. Each: {output_pin, input_pin} (aliases 'from'/'to'),
                     pin paths 'NodeName.PinName'. Nodes are added first, so links may reference them.
        auto_layout: True lays nodes left-to-right at spacing_x intervals when a spec omits 'position'.
        spacing_x:   horizontal spacing used by auto_layout (default 350).
        graph_name:  which model/graph (default = the default model).

        Each created node ledgers its own 'add_rig_vm_node' inverse and each link its own 'add_rig_vm_link'
        inverse (all already folded into editor_level.undo, undone LIFO). Per-item failures are collected
        (node_errors / links[].result) without aborting the batch. The asset is saved."""
        params = {"asset_path": asset_path, "nodes": nodes, "connections": connections or [],
                  "auto_layout": auto_layout, "spacing_x": spacing_x, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_BUILD_GRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # layout_rig_vm_graph — auto-position all nodes on a grid            #
    # ================================================================== #
    _LAYOUT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
spacing_x = float(PARAMS.get("spacing_x", 350.0))
spacing_y = float(PARAMS.get("spacing_y", 250.0))
per_row = int(PARAMS.get("per_row", 4))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        allnodes = _try(lambda: model.get_nodes(), []) or []
        moved = []
        if per_row < 1:
            per_row = 1
        with unreal.ScopedEditorTransaction("MCP layout_rig_vm_graph"):
            for i, n in enumerate(allnodes):
                nn = str(_try(lambda n=n: n.get_node_path()))
                prior = _pos_list(_try(lambda n=n: n.get_position()))
                col = i % per_row
                row = i // per_row
                newpos = unreal.Vector2D(float(col) * spacing_x, float(row) * spacing_y)
                ok = _try(lambda nn=nn, newpos=newpos: ctrl.set_node_position_by_name(unreal.Name(nn), newpos, True, False, False), False)
                _ledger().append({"op": "set_rig_vm_node_position", "asset_path": path, "graph_name": gname,
                                  "node_name": nn, "prior_pos": prior})
                moved.append({"node_name": nn, "before": prior, "after": [round(float(col) * spacing_x, 2), round(float(row) * spacing_y, 2)]})
        _save_cr(path)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "graph_name": gname, "moved_count": len(moved), "per_row": per_row,
            "spacing": [spacing_x, spacing_y], "moved": moved[:200], "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def layout_rig_vm_graph(ctx, asset_path: str, spacing_x: float = 350.0, spacing_y: float = 250.0,
                            per_row: int = 4, graph_name: str = None) -> str:
        """Auto-lay-out a RigVM graph -- reposition every node onto a tidy grid (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        spacing_x:  horizontal spacing between columns (default 350).
        spacing_y:  vertical spacing between rows (default 250).
        per_row:    how many nodes per row before wrapping (default 4).
        graph_name: which model/graph (default = the default model).

        Purely cosmetic (layout only; does not change the solve). Each node's prior position is captured
        and ledgered as 'set_rig_vm_node_position' (already folded into editor_level.undo), so undo restores
        the exact previous layout node-by-node. The asset is saved."""
        params = {"asset_path": asset_path, "spacing_x": spacing_x, "spacing_y": spacing_y,
                  "per_row": per_row, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_LAYOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # search_rig_vm_nodes — filter nodes within a graph (READ)          #
    # ================================================================== #
    _SEARCH_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
flt = str(PARAMS.get("filter") or "").strip().lower()
max_results = int(PARAMS.get("max_results", 50))
all_graphs = bool(PARAMS.get("all_graphs", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    graphs = []
    if all_graphs:
        for m in (_try(lambda: bp.get_all_models(), []) or []):
            graphs.append(m)
    else:
        ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
        if cerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
            graphs = None
        else:
            graphs = [_model_of(ctrl, bp, model)]
    if graphs is not None:
        matches = []
        total = 0
        for model in graphs:
            gname = str(_try(lambda model=model: model.get_graph_name()))
            for n in (_try(lambda model=model: model.get_nodes(), []) or []):
                total += 1
                nn = str(_try(lambda n=n: n.get_node_path()))
                title = str(_try(lambda n=n: n.get_node_title()) or "")
                cls = type(n).__name__
                ss = _try(lambda n=n: n.get_script_struct()) if hasattr(n, "get_script_struct") else None
                struct = str(_try(lambda ss=ss: ss.get_name())) if ss is not None else ""
                hay = (nn + " " + title + " " + cls + " " + struct).lower()
                if flt and flt not in hay:
                    continue
                if len(matches) < max_results:
                    matches.append({"graph_name": gname, "node_name": nn, "title": title,
                                    "node_class": cls, "script_struct": struct or None,
                                    "position": _pos_list(_try(lambda n=n: n.get_position()))})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "filter": (PARAMS.get("filter") or None), "scanned_nodes": total,
            "match_count": len(matches), "truncated": len(matches) >= max_results,
            "matches": matches}))
'''

    @mcp.tool()
    def search_rig_vm_nodes(ctx, asset_path: str, filter: str = None, max_results: int = 50,
                            graph_name: str = None, all_graphs: bool = False) -> str:
        """Search the nodes WITHIN a Control Rig solve graph by a substring filter (READ-ONLY).

        asset_path:  ControlRigBlueprint path.
        filter:      case-insensitive substring matched against each node's path, title, class, and
                     script-struct name. Omit to list all nodes (up to max_results).
        max_results: cap on returned matches (default 50).
        graph_name:  which model/graph to search (default = the default model). Ignored if all_graphs.
        all_graphs:  True searches every model returned by get_all_models.

        Distinct from list_control_rig_node_types (which enumerates AVAILABLE node types to add): this
        searches the nodes ALREADY PRESENT in the graph. Read-only: no ledger entry."""
        params = {"asset_path": asset_path, "filter": filter, "max_results": max_results,
                  "graph_name": graph_name, "all_graphs": all_graphs}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_rig_modules — modules on a modular rig (READ)                #
    # ================================================================== #
    _LIST_MODULES_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    is_module = bool(_try(lambda: bp.is_control_rig_module(), False))
    mrc = _try(lambda: bp.get_modular_rig_controller())
    mrm = _try(lambda: bp.get_editor_property("modular_rig_model"))
    modules = []
    if mrm is not None:
        mods = _try(lambda: mrm.get_editor_property("modules"), None)
        if mods is None:
            mods = _try(lambda: mrm.get_modules(), []) if hasattr(mrm, "get_modules") else []
        for m in (mods or []):
            entry = {}
            entry["name"] = str(_try(lambda m=m: m.get_editor_property("name")) or _try(lambda m=m: m.name) or "")
            entry["path"] = str(_try(lambda m=m: m.get_editor_property("path")) or "")
            entry["class"] = type(m).__name__
            modules.append(entry)
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "is_control_rig_module": is_module, "has_modular_controller": mrc is not None,
        "module_count": len(modules), "modules": modules,
        "note": ("this Control Rig is not modular; module authoring is a separate C++/UModularRigController surface" if not modules and not is_module else None)}))
'''

    @mcp.tool()
    def list_rig_modules(ctx, asset_path: str) -> str:
        """List the modules installed on a MODULAR Control Rig (READ-ONLY).

        asset_path: ControlRigBlueprint path.

        Reads the asset's ModularRigModel (via get_modular_rig_controller / the modular_rig_model property)
        and lists each installed module's name/path. For a standard (non-modular) Control Rig this returns
        an empty module list with is_control_rig_module=False. Read-only: no ledger entry. Module WRITE
        authoring (add/connect/configure modules) is a separate C++/UModularRigController surface (deferred)."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_MODULES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_rig_module_assets — CR module assets in project (READ)       #
    # ================================================================== #
    _LIST_MODULE_ASSETS_BODY = _CR_HELPERS + r'''
flt = str(PARAMS.get("filter") or "").strip().lower()
detailed = bool(PARAMS.get("detailed", False))
ar = unreal.AssetRegistryHelpers.get_asset_registry()
f = unreal.ARFilter(class_names=["ControlRigBlueprint"], recursive_classes=True)
assets = _try(lambda: ar.get_assets(f), []) or []
results = []
scanned = 0
for a in assets:
    scanned += 1
    pkg = str(a.package_name)
    aname = str(a.asset_name)
    if flt and flt not in (pkg + "/" + aname).lower():
        continue
    entry = {"name": aname, "path": pkg + "." + aname}
    if detailed:
        full = pkg + "." + aname
        obj = _try(lambda full=full: unreal.EditorAssetLibrary.load_asset(full))
        entry["is_module"] = bool(_try(lambda obj=obj: obj.is_control_rig_module(), False)) if obj is not None else None
    results.append(entry)
# When detailed, optionally keep only modules
only_modules = bool(PARAMS.get("only_modules", False))
if detailed and only_modules:
    results = [r for r in results if r.get("is_module")]
print("@@UMCP@@" + json.dumps({"status": "success", "filter": (PARAMS.get("filter") or None),
    "detailed": detailed, "only_modules": only_modules, "scanned": scanned,
    "count": len(results), "assets": results[:500]}))
'''

    @mcp.tool()
    def list_rig_module_assets(ctx, filter: str = None, detailed: bool = False,
                               only_modules: bool = False) -> str:
        """List Control Rig assets in the project (optionally only module assets) via the Asset Registry
        (READ-ONLY).

        filter:       optional case-insensitive substring matched against each asset's package/name.
        detailed:     True loads each asset to report is_module (whether it's a Control Rig MODULE).
                      Slower; leave False for a fast registry-only listing.
        only_modules: with detailed=True, keep only assets where is_module is True.

        Enumerates every ControlRigBlueprint via the Asset Registry (no load unless detailed). Read-only:
        no ledger entry."""
        params = {"filter": filter, "detailed": detailed, "only_modules": only_modules}
        try:
            return json.dumps(_exec(_LIST_MODULE_ASSETS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_rig_controls — audit control settings (READ)             #
    # ================================================================== #
    _VALIDATE_CONTROLS_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
include_bones = bool(PARAMS.get("include_bones", False))
max_results = int(PARAMS.get("max_results", 200))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    keys = _try(lambda: h.get_all_keys(True), []) or []
    controls = []
    issues = []
    counts = {"control": 0, "bone": 0, "null": 0, "curve": 0, "socket": 0, "other": 0}
    for k in keys:
        short = _etype_short(k).lower()
        counts[short] = counts.get(short, 0) + 1 if short in counts else counts["other"] + 0
        if short not in counts:
            counts["other"] += 1
        if short == "control":
            cs = _try(lambda k=k: h.get_control_settings(k))
            nm = str(k.name)
            ct = str(_try(lambda cs=cs: cs.get_editor_property("control_type"))).split(".")[-1].split(":")[0] if cs is not None else None
            shape = str(_try(lambda cs=cs: cs.get_editor_property("shape_name"))) if cs is not None else None
            vis = bool(_try(lambda cs=cs: cs.get_editor_property("shape_visible"), False)) if cs is not None else False
            par = _try(lambda k=k: h.get_first_parent(k))
            has_par = par is not None and _etype_short(par) != "None"
            if len(controls) < max_results:
                controls.append({"name": nm, "control_type": ct, "shape_name": shape,
                                 "shape_visible": vis, "parented": has_par})
            if cs is None:
                issues.append({"control": nm, "issue": "no settings struct"})
            if not shape or shape in ("None", ""):
                issues.append({"control": nm, "issue": "empty shape_name"})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "element_count": len(keys), "counts": counts, "control_count": counts.get("control", 0),
        "issue_count": len(issues), "issues": issues[:max_results],
        "controls": (controls if not include_bones else controls), "ok": len(issues) == 0}))
'''

    @mcp.tool()
    def validate_rig_controls(ctx, asset_path: str, include_bones: bool = False,
                              max_results: int = 200) -> str:
        """Audit a Control Rig's CONTROLS for setup issues (READ-ONLY).

        asset_path:    ControlRigBlueprint path.
        include_bones: (reserved) currently controls are always reported; bones counted in 'counts'.
        max_results:   cap on reported controls/issues (default 200).

        Walks the hierarchy, reports per-control control_type / shape_name / shape_visible / whether it is
        parented, and flags issues (missing settings struct, empty shape_name). Returns element counts by
        type and ok=True when no issues. Read-only: no ledger entry."""
        params = {"asset_path": asset_path, "include_bones": include_bones, "max_results": max_results}
        try:
            return json.dumps(_exec(_VALIDATE_CONTROLS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_rig_graph — compile status + node/link sanity (READ)     #
    # ================================================================== #
    _VALIDATE_GRAPH_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    status = str(_try(lambda: bp.get_editor_property("status")))
    models = _try(lambda: bp.get_all_models(), []) or []
    graphs = []
    total_nodes = 0
    issues = []
    for m in models:
        gname = str(_try(lambda m=m: m.get_graph_name()))
        nodes = _try(lambda m=m: m.get_nodes(), []) or []
        total_nodes += len(nodes)
        node_names = []
        for n in nodes:
            nn = str(_try(lambda n=n: n.get_node_path()))
            node_names.append(nn)
            errmsg = _try(lambda n=n: n.get_editor_property("node_error_message"))
            if errmsg:
                issues.append({"graph": gname, "node": nn, "error": str(errmsg)[:200]})
        graphs.append({"graph_name": gname, "node_count": len(nodes)})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "blueprint_status": status.split(".")[-1].split(":")[0].strip(),
        "graph_count": len(graphs), "graphs": graphs, "total_nodes": total_nodes,
        "issue_count": len(issues), "issues": issues[:200], "ok": len(issues) == 0}))
'''

    @mcp.tool()
    def validate_rig_graph(ctx, asset_path: str) -> str:
        """Validate a Control Rig's RigVM graph(s) -- blueprint compile status + per-node error messages
        (READ-ONLY).

        asset_path: ControlRigBlueprint path.

        Reports the blueprint's compile status, enumerates every model/graph with node counts, and collects
        any per-node error messages. Returns ok=True when no node errors are present. Read-only: no ledger
        entry (does NOT force a recompile)."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_VALIDATE_GRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_rig_gizmos — control shapes vs shape libraries (READ)    #
    # ================================================================== #
    _VALIDATE_GIZMOS_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    lf = unreal.ARFilter(class_names=["ControlRigShapeLibrary"], recursive_classes=True)
    known = set()
    libcount = 0
    for a in (_try(lambda: ar.get_assets(lf), []) or []):
        p = str(a.package_name) + "." + str(a.asset_name)
        lib = _try(lambda p=p: unreal.EditorAssetLibrary.load_asset(p))
        if lib is None:
            continue
        libcount += 1
        for s in (_try(lambda lib=lib: lib.get_editor_property("shapes"), []) or []):
            snm = str(_try(lambda s=s: s.get_editor_property("shape_name")))
            if snm:
                known.add(snm)
    h = bp.get_hierarchy()
    controls = [k for k in (_try(lambda: h.get_all_keys(True), []) or []) if _etype_short(k) == "CONTROL"]
    checked = []
    unknown = []
    for k in controls:
        cs = _try(lambda k=k: h.get_control_settings(k))
        if cs is None:
            continue
        vis = bool(_try(lambda cs=cs: cs.get_editor_property("shape_visible"), False))
        shape = str(_try(lambda cs=cs: cs.get_editor_property("shape_name")))
        entry = {"control": str(k.name), "shape_name": shape, "shape_visible": vis,
                 "known": (shape in known) if known else None}
        checked.append(entry)
        if vis and known and shape not in known and shape not in ("None", ""):
            unknown.append({"control": str(k.name), "shape_name": shape})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "shape_library_count": libcount, "known_shape_count": len(known),
        "control_count": len(controls), "controls": checked[:300],
        "unknown_shape_count": len(unknown), "unknown_shapes": unknown[:200],
        "ok": len(unknown) == 0}))
'''

    @mcp.tool()
    def validate_rig_gizmos(ctx, asset_path: str) -> str:
        """Validate a Control Rig's control GIZMOS -- each visible control's shape_name vs the project's
        shape libraries (READ-ONLY).

        asset_path: ControlRigBlueprint path.

        Loads every ControlRigShapeLibrary in the project, collects the set of known shape names, then
        checks each CONTROL's shape_name. Flags visible controls whose shape_name is not found in any
        library (a gizmo that would fail to draw). Returns ok=True when none are unknown. Read-only: no
        ledger entry."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_VALIDATE_GIZMOS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # export_control_rig — serialize hierarchy (+ graph summary) (READ) #
    # ================================================================== #
    _EXPORT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
inline = bool(PARAMS.get("inline", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    keys = _try(lambda: h.get_all_keys(True), []) or []
    hierarchy_txt = _try(lambda: hc.export_to_text(list(keys))) if keys else ""
    elements = [{"name": str(k.name), "type": _etype_short(k)} for k in keys]
    models = _try(lambda: bp.get_all_models(), []) or []
    graphs = []
    for m in models:
        gname = str(_try(lambda m=m: m.get_graph_name()))
        nodes = _try(lambda m=m: m.get_nodes(), []) or []
        graphs.append({"graph_name": gname, "node_count": len(nodes),
                       "nodes": [str(_try(lambda n=n: n.get_node_path())) for n in nodes][:200]})
    out = {"status": "success", "asset_path": bp.get_path_name(),
           "element_count": len(elements), "elements": elements[:500],
           "hierarchy_text_len": len(str(hierarchy_txt or "")),
           "graph_count": len(graphs), "graphs": graphs}
    if inline:
        out["hierarchy_text"] = str(hierarchy_txt or "")
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def export_control_rig(ctx, asset_path: str, inline: bool = False) -> str:
        """Export a Control Rig's structure -- hierarchy elements + serialized text + graph node summary
        (READ-ONLY).

        asset_path: ControlRigBlueprint path.
        inline:     True includes the full serialized hierarchy TEXT (RigHierarchyController.export_to_text)
                    in the response; default False returns only its length plus the element/graph summary.

        Provides a portable snapshot: every hierarchy element (name+type), the export_to_text blob (the same
        text import_from_text consumes), and each RigVM graph's node list. Read-only: no ledger entry."""
        params = {"asset_path": asset_path, "inline": inline}
        try:
            return json.dumps(_exec(_EXPORT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # select_rig_elements — set the hierarchy selection (ledgered)      #
    # ================================================================== #
    _SELECT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
names = PARAMS.get("names") or []
select = bool(PARAMS.get("select", True))
clear_selection = bool(PARAMS.get("clear_selection", True))
element_type = PARAMS.get("element_type") or "control"
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(names, list) or not names:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide names: a non-empty list of element names"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    prior = _try(lambda: h.get_selected_keys(), []) or []
    prior_caps = [{"name": str(k.name), "type": _etype_short(k).lower()} for k in prior]
    # resolve requested keys against the hierarchy (match by name across types, prefer requested type)
    allkeys = _try(lambda: h.get_all_keys(True), []) or []
    want_type = _etype(element_type)
    resolved = []
    missing = []
    for nm in names:
        found = None
        for k in allkeys:
            if str(k.name) == str(nm) and (want_type is None or k.type == want_type):
                found = k; break
        if found is None:
            for k in allkeys:
                if str(k.name) == str(nm):
                    found = k; break
        if found is None:
            missing.append(str(nm))
        else:
            resolved.append(found)
    with unreal.ScopedEditorTransaction("MCP select_rig_elements"):
        if clear_selection:
            _try(lambda: hc.clear_selection())
        for k in resolved:
            _try(lambda k=k: hc.select_element(k, select, False))
    _ledger().append({"op": "select_rig_elements", "asset_path": path, "prior_keys": prior_caps})
    now = _try(lambda: h.get_selected_keys(), []) or []
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "requested": [str(n) for n in names], "resolved_count": len(resolved), "missing": missing,
        "select": select, "cleared": clear_selection,
        "now_selected": [str(k.name) for k in now], "prior_selected": [c["name"] for c in prior_caps],
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def select_rig_elements(ctx, asset_path: str, names: list, element_type: str = "control",
                            select: bool = True, clear_selection: bool = True) -> str:
        """Set the SELECTION of elements in a Control Rig hierarchy (ledgered, reversible write).

        asset_path:      ControlRigBlueprint path.
        names:           list of element names to (de)select.
        element_type:    preferred type to disambiguate names (bone|null|control|socket|curve; default
                         'control'); falls back to any type with a matching name.
        select:          True selects the named elements, False deselects them (default True).
        clear_selection: True (default) clears the current selection first (so only 'names' end up selected).

        Uses RigHierarchyController.select_element / clear_selection. The FULL prior selection set is
        captured so the inverse restores exactly what was selected before. Selection is editor state (not
        persisted to the asset), so nothing is saved. Ledgered op 'select_rig_elements'
        {asset_path,prior_keys}."""
        params = {"asset_path": asset_path, "names": names, "element_type": element_type,
                  "select": select, "clear_selection": clear_selection}
        try:
            return json.dumps(_exec(_SELECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_rig_autosave — toggle auto VM recompile (ledgered)            #
    # ================================================================== #
    _SET_AUTOSAVE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
enabled = PARAMS.get("enabled")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif enabled is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide enabled (true/false)"}))
else:
    prior = bool(_try(lambda: bp.get_auto_vm_recompile(), True))
    with unreal.ScopedEditorTransaction("MCP set_rig_autosave"):
        bp.set_auto_vm_recompile(bool(enabled))
    _ledger().append({"op": "set_rig_autosave", "asset_path": path, "prior_value": prior})
    _save_cr(path)
    after = bool(_try(lambda: bp.get_auto_vm_recompile(), prior))
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "prior_value": prior, "new_value": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_autosave(ctx, asset_path: str, enabled: bool) -> str:
        """Toggle a Control Rig's AUTO VM-RECOMPILE (autosave-of-solve) setting (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        enabled:    True enables automatic VM recompilation on edits; False disables it (edits require a
                    manual recompile).

        Set via UControlRigBlueprint.set_auto_vm_recompile. The prior value is captured so the inverse
        restores it. The asset is saved. Ledgered op 'set_rig_autosave' {asset_path,prior_value}."""
        params = {"asset_path": asset_path, "enabled": enabled}
        try:
            return json.dumps(_exec(_SET_AUTOSAVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. NEW op schemas to
    # fold (inverse logic self-contained; finish each with
    # unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)):
    #   set_rig_preview_mesh   {asset_path, prior_mesh_path}
    #       -> bp = load(asset_path); m = load(prior_mesh_path) if prior_mesh_path else None;
    #          bp.set_preview_mesh(m, True); save.
    #   create_rig_vm_function {asset_path, function_name}
    #       -> bp = load(asset_path); libctrl = bp.get_controller(bp.get_local_function_library());
    #          libctrl.remove_function_from_library(unreal.Name(function_name), True, False); save.
    #   select_rig_elements    {asset_path, prior_keys:[{name,type}]}
    #       -> bp = load(asset_path); hc = bp.get_hierarchy_controller(); hc.clear_selection();
    #          for c in prior_keys: hc.select_element(RigElementKey(Name(c.name), _ETYPES[c.type]), True, False).
    #          (selection is editor state; save optional/no-op.)
    #   set_rig_autosave       {asset_path, prior_value}
    #       -> bp = load(asset_path); bp.set_auto_vm_recompile(bool(prior_value)); save.
    # REUSED (already folded) ops: create_asset (create_control_rig[_from_skeleton]); add_rig_vm_node
    #   (add_rig_vm_function_node + build_rig_vm_graph nodes); add_rig_vm_link (build_rig_vm_graph links);
    #   set_rig_vm_node_position (layout_rig_vm_graph).
