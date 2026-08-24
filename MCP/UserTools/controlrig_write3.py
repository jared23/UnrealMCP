"""UserTools :: Control Rig (WRITE, tranche 3)  (spec: docs/spec/controlrig.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). A FOURTH Control-Rig module
alongside controlrig_write.py (HIERARCHY authoring), controlrig_graph_write.py (RigVM GRAPH authoring)
and controlrig_write2.py (creation / functions / batch-graph / validation reads). This module fills the
LAST Python-reachable [PY] gaps that tranche 2 left on the table:

  - create_control_rig_module   : the dedicated "create + (optional) import-skeleton + turn-into-module"
                                  factory tool the spec lists distinctly from create_control_rig(modular).
  - analyze_rig_module_asset    : a READ that summarizes a Control-Rig MODULE asset's own structure --
                                  its exposed CONNECTORS / SOCKETS, graphs (filtered), preview mesh,
                                  function count (distinct from list_rig_modules, which reads modules
                                  INSTALLED on a modular RIG).
  - place_rig_pole_vector       : compute an IK pole-vector position from a root/mid/end bone chain's
                                  stored global transforms and set a control there (pure static geometry
                                  off the rig hierarchy -- NO mesh vertices, NO runtime/PIE eval needed).

Query convention, base64 PARAMS injection, Output-Log auto-capture and the per-session undo ledger are
copied VERBATIM from the gold-standard editor_level.py / controlrig_write2.py.

CRASH-SAFETY (see .mcp_coord/PROTOCOL.md + memory):
  * NEVER duplicate_asset a ControlRigBlueprint -> UControlRigBlueprint::PostDuplicate hard-crash. All
    creation goes through AssetToolsHelpers.get_asset_tools().create_asset(ControlRigBlueprintFactory).
  * create_* tools are NOT wrapped in a ScopedEditorTransaction (that traps the new asset in the undo
    buffer and blocks the inverse-delete); they SAVE on creation and ledger op 'create_asset'.

REVERSIBILITY -- this module introduces NO NEW op schemas; every WRITE REUSES an inverse already folded
into editor_level.undo (this module registers NO `undo` tool):
  - create_control_rig_module -> op 'create_asset' {asset_path, package_path, created_dir}
        inverse = delete the created asset (ALREADY folded; same as create_control_rig).
  - place_rig_pole_vector     -> op 'set_rig_element_transform'
        {asset_path, key_name, key_type, space, initial, prior_xf{t,q,s}}
        inverse = restore the control's prior global transform (ALREADY folded; same schema/format as
        controlrig_write.py.set_rig_element_transform).

READ-ONLY (no ledger): analyze_rig_module_asset.

DEFERRED / NOT shipped here (documented, not faked -- reported to the coordinator for a C++/runtime batch):
  - Modular-rig WRITES (add_rig_module / connect_rig_module_connector / auto_connect_rig_modules /
    set_rig_module_config / bind_rig_module_variable / mirror_rig_module): driven by UModularRigController
    installing module ASSETS onto a modular rig; broad connector/binding surface -> C++/modular batch.
  - collapse/promote/expand RigVM subgraph: round-trips rename nodes + drop per-node layout/pin state;
    a single op has no faithful inverse without a full-subgraph snapshot -> C++/snapshot batch.
  - Physics/deformation/penetration/bone-bounds, motion-capture/simulate/profile, play/stop preview
    animation, get_rig_pose, analyze_rig_io, analyze_rig_control_impact: need skeletal-mesh render
    GEOMETRY (per-bone weighted bounds have NO Python API -- only whole-mesh SkeletalMesh.get_bounds)
    or a runtime/PIE evaluation of the compiled VM (bp.create_rig_vm_host executed against a world)
    -> the C++/runtime-probe bucket.
  - fit_rig_control_shapes / add_rig_jiggle_bob: gizmo fitting wants per-bone mesh bounds; jiggle wants
    a physics/module setup -> geometry/module bucket.
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
# bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as base64.
# Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/
# original_stderr/success/user_code/code_obj (the wrapper's own names).


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
import unreal, json, builtins, math
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
def _resolve_key(h, name, etype):
    want = str(name)
    for k in (h.get_all_keys(True) or []):
        if str(k.name) == want and (etype is None or k.type == etype):
            return k
    return None
def _xf_to_dict(t):
    tr = t.translation; q = t.rotation; s = t.scale3d
    return {"t": [tr.x, tr.y, tr.z], "q": [q.x, q.y, q.z, q.w], "s": [s.x, s.y, s.z]}
def _xf_report(t):
    tr = t.translation; r = t.rotation.rotator(); s = t.scale3d
    return {"location": [round(tr.x, 3), round(tr.y, 3), round(tr.z, 3)],
            "rotation": [round(r.pitch, 3), round(r.yaw, 3), round(r.roll, 3)],
            "scale": [round(s.x, 3), round(s.y, 3), round(s.z, 3)]}
def _vsub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _vadd(a, b):
    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])
def _vscale(a, s):
    return (a[0]*s, a[1]*s, a[2]*s)
def _vdot(a, b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]
def _vlen(a):
    return math.sqrt(_vdot(a, a))
'''

    # ================================================================== #
    # create_control_rig_module — create + import + turn into module     #
    # ================================================================== #
    _CREATE_MODULE_BODY = _CR_HELPERS + r'''
asset_path = PARAMS.get("asset_path")
skeleton_path = PARAMS.get("skeleton_path")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
package_path, name = _split_path(asset_path)
skel = _try(lambda: EAL.load_asset(skeleton_path)) if skeleton_path else None
exists_check = asset_path.rsplit(".", 1)[0] if ("." in str(asset_path).split("/")[-1]) else asset_path
if package_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset_path must be a full path like /Game/Dir/Name"}))
elif skeleton_path and skel is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "skeleton/skeletal-mesh not found: %s" % skeleton_path}))
elif skeleton_path and not isinstance(skel, (unreal.Skeleton, unreal.SkeletalMesh)):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "skeleton_path must be a Skeleton or SkeletalMesh (got %s)" % skel.get_class().get_name()}))
elif EAL.does_asset_exist(exists_check):
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
        h = obj.get_hierarchy()
        # A MODULE does NOT persist a bone hierarchy -- turn_into_control_rig_module resets the hierarchy
        # to the primary Root CONNECTOR + SOCKET (the target skeleton is resolved via connectors when the
        # module is INSTALLED on a modular rig). So we do NOT import bones (they would be discarded).
        # A SkeletalMesh is still useful as the module's PREVIEW mesh (it survives the conversion).
        preview_set = False
        note = None
        if isinstance(skel, unreal.SkeletalMesh):
            preview_set = bool(_try(lambda: (obj.set_preview_mesh(skel, True), True)[1], False))
        elif isinstance(skel, unreal.Skeleton):
            note = "bare Skeleton given: a module stores no bone hierarchy and set_preview_mesh needs a SkeletalMesh, so no preview was set; the skeleton is a resolve-time target only."
        made_module = bool(_try(lambda: (obj.turn_into_control_rig_module(), True)[1], False))
        is_module = bool(_try(lambda: obj.is_control_rig_module(), False))
        full = package_path + "/" + name
        _try(lambda: EAL.save_asset(full, only_if_is_dirty=False))
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": package_path, "created_dir": created_dir})
        keys = _try(lambda: h.get_all_keys(True), []) or []
        connectors = [str(k.name) for k in keys if _etype_short(k) == "CONNECTOR"]
        sockets = [str(k.name) for k in keys if _etype_short(k) == "SOCKET"]
        prev = _try(lambda: obj.get_preview_mesh())
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": full, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "made_module": made_module, "is_control_rig_module": is_module,
            "source": skeleton_path, "source_kind": (None if skel is None else ("skeletal_mesh" if isinstance(skel, unreal.SkeletalMesh) else "skeleton")),
            "preview_mesh_set": preview_set, "preview_mesh": (str(prev.get_path_name()) if prev is not None else None),
            "element_count": len(keys), "connectors": connectors, "sockets": sockets,
            "note": note, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_control_rig_module(ctx, asset_path: str, skeleton_path: str = None) -> str:
        """Create a Control Rig MODULE asset -- factory-create a Control Rig then convert it into a
        modular-rig MODULE (ledgered, reversible write).

        asset_path:    full destination path '/Game/Dir/Name' (an object suffix, if given, is stripped).
        skeleton_path: OPTIONAL Skeleton or SkeletalMesh path. A SkeletalMesh is set as the module's
                       PREVIEW mesh (it survives the conversion). A bare Skeleton is advisory only (a
                       module keeps no bone hierarchy and set_preview_mesh needs a SkeletalMesh). Omit for
                       a bare module (just the auto-created primary 'Root' connector/socket).

        Distinct from create_control_rig(modular=True): this is the spec's dedicated
        create_control_rig_module(asset_path, skeleton_path). Created via ControlRigBlueprintFactory
        (NEVER duplicate_asset), then converted via UControlRigBlueprint.turn_into_control_rig_module --
        which resets the persistent hierarchy to the primary Root CONNECTOR + SOCKET (the module's target
        skeleton is resolved via connectors when the module is INSTALLED on a modular rig, so bones are
        NOT imported into the persistent hierarchy -- that is by design, verified live). The asset is
        saved. Ledgered op 'create_asset' {asset_path,package_path,created_dir}; the inverse (delete the
        whole created asset) already lives in editor_level.undo. Module WRITE authoring
        (install/connect/bind modules onto a modular rig) is a separate C++/UModularRigController surface
        (deferred)."""
        params = {"asset_path": asset_path, "skeleton_path": skeleton_path}
        try:
            return json.dumps(_exec(_CREATE_MODULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # analyze_rig_module_asset — summarize a MODULE asset (READ)         #
    # ================================================================== #
    _ANALYZE_MODULE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
graph_filter = str(PARAMS.get("graph_filter") or "").strip().lower()
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    is_module = bool(_try(lambda: bp.is_control_rig_module(), False))
    has_mrc = _try(lambda: bp.get_modular_rig_controller()) is not None
    h = bp.get_hierarchy()
    keys = _try(lambda: h.get_all_keys(True), []) or []
    counts = {}
    connectors = []
    sockets = []
    for k in keys:
        short = _etype_short(k)
        counts[short] = counts.get(short, 0) + 1
        if short == "CONNECTOR":
            par = _try(lambda k=k: h.get_first_parent(k))
            pname = str(par.name) if par is not None else None
            if pname in (None, "None", ""):
                pname = None
            connectors.append({"name": str(k.name), "parent": pname})
        elif short == "SOCKET":
            sockets.append({"name": str(k.name)})
    models = _try(lambda: bp.get_all_models(), []) or []
    graphs = []
    total_nodes = 0
    for m in models:
        gname = str(_try(lambda m=m: m.get_graph_name()))
        nodes = _try(lambda m=m: m.get_nodes(), []) or []
        total_nodes += len(nodes)
        if graph_filter and graph_filter not in gname.lower():
            continue
        graphs.append({"graph_name": gname, "node_count": len(nodes)})
    lib = _try(lambda: bp.get_local_function_library())
    func_count = len(_try(lambda: lib.get_functions(), []) or []) if lib is not None else 0
    prev = _try(lambda: bp.get_preview_mesh())
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "is_control_rig_module": is_module, "has_modular_controller": has_mrc,
        "element_count": len(keys), "counts": counts,
        "connector_count": len(connectors), "connectors": connectors,
        "socket_count": len(sockets), "sockets": sockets,
        "graph_count": len(graphs), "graphs": graphs, "total_nodes": total_nodes,
        "function_count": func_count,
        "preview_mesh": (str(prev.get_path_name()) if prev is not None else None),
        "note": (None if is_module else "this Control Rig is not a module (is_control_rig_module=False); reported as a plain Control Rig")}))
'''

    @mcp.tool()
    def analyze_rig_module_asset(ctx, asset_path: str, graph_filter: str = None) -> str:
        """Analyze a Control Rig MODULE asset -- summarize its exposed connectors/sockets, graphs, and
        structure (READ-ONLY).

        asset_path:   ControlRigBlueprint path (ideally a MODULE; a plain Control Rig is reported too).
        graph_filter: optional case-insensitive substring; only graphs whose name contains it are listed
                      (node counts still summed across ALL graphs in total_nodes).

        Reads the asset's own module structure: is_control_rig_module, the primary/secondary CONNECTOR
        elements (name + parent) and SOCKET elements a module exposes, element counts by type, each RigVM
        graph's node count, local-function count, and the preview mesh. Distinct from list_rig_modules
        (which reads modules INSTALLED on a modular RIG's ModularRigModel) -- this reads a single module
        ASSET's own definition. Read-only: no ledger entry."""
        params = {"asset_path": asset_path, "graph_filter": graph_filter}
        try:
            return json.dumps(_exec(_ANALYZE_MODULE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # place_rig_pole_vector — compute IK pole pos from a bone chain      #
    # ================================================================== #
    _POLE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
control_name = PARAMS.get("control")
root_bone = PARAMS.get("root_bone")
mid_bone = PARAMS.get("mid_bone")
end_bone = PARAMS.get("end_bone")
distance = PARAMS.get("distance")
initial = bool(PARAMS.get("initial", False))
control_type = PARAMS.get("control_type") or "control"
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not (control_name and root_bone and mid_bone and end_bone):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide control, root_bone, mid_bone, end_bone"}))
else:
    h = bp.get_hierarchy()
    cet = _etype(control_type) or unreal.RigElementType.CONTROL
    ckey = _resolve_key(h, control_name, cet)
    rkey = _resolve_key(h, root_bone, unreal.RigElementType.BONE)
    mkey = _resolve_key(h, mid_bone, unreal.RigElementType.BONE)
    ekey = _resolve_key(h, end_bone, unreal.RigElementType.BONE)
    missing = []
    if ckey is None: missing.append("control:" + str(control_name))
    if rkey is None: missing.append("root_bone:" + str(root_bone))
    if mkey is None: missing.append("mid_bone:" + str(mid_bone))
    if ekey is None: missing.append("end_bone:" + str(end_bone))
    if missing:
        bones = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "BONE"]
        controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve: %s" % ", ".join(missing),
            "available_bones": bones[:100], "available_controls": controls[:100]}))
    else:
        def _pos(key):
            t = h.get_global_transform(key, initial).translation
            return (t.x, t.y, t.z)
        rp = _pos(rkey); mp = _pos(mkey); ep = _pos(ekey)
        chord = _vsub(ep, rp)
        chord_len2 = _vdot(chord, chord)
        if chord_len2 < 1e-8:
            proj = rp
        else:
            tparam = _vdot(_vsub(mp, rp), chord) / chord_len2
            proj = _vadd(rp, _vscale(chord, tparam))
        bend = _vsub(mp, proj)
        bend_len = _vlen(bend)
        upper = _vlen(_vsub(mp, rp))
        lower = _vlen(_vsub(ep, mp))
        if distance is None:
            dist = (upper + lower) * 0.5
            if dist < 1e-4:
                dist = 1.0
        else:
            dist = float(distance)
        if bend_len < 1e-4:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "bone chain is (near) straight/collinear; pole-vector direction is undefined. Bend the mid bone or pass an explicit direction via a posed chain.",
                "root_pos": [round(v,3) for v in rp], "mid_pos": [round(v,3) for v in mp], "end_pos": [round(v,3) for v in ep]}))
        else:
            bdir = _vscale(bend, 1.0 / bend_len)
            pole = _vadd(mp, _vscale(bdir, dist))
            prior_t = h.get_global_transform(ckey, initial)
            prior_xf = _xf_to_dict(prior_t)
            newxf = unreal.Transform()
            newxf.set_editor_property("translation", unreal.Vector(pole[0], pole[1], pole[2]))
            newxf.set_editor_property("rotation", prior_t.rotation)
            newxf.set_editor_property("scale3d", prior_t.scale3d)
            with unreal.ScopedEditorTransaction("MCP place_rig_pole_vector"):
                h.set_global_transform(ckey, newxf, initial, True, False, False)
            _ledger().append({"op": "set_rig_element_transform", "asset_path": path,
                              "key_name": str(ckey.name), "key_type": _etype_short(ckey),
                              "space": "global", "initial": initial, "prior_xf": prior_xf})
            _save_cr(path)
            after = h.get_global_transform(ckey, initial)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "control": str(ckey.name), "root_bone": str(rkey.name), "mid_bone": str(mkey.name),
                "end_bone": str(ekey.name), "initial": initial,
                "root_pos": [round(v,3) for v in rp], "mid_pos": [round(v,3) for v in mp], "end_pos": [round(v,3) for v in ep],
                "bend_direction": [round(v,4) for v in bdir], "distance": round(dist,3),
                "pole_position": [round(v,3) for v in pole],
                "control_before": _xf_report(prior_t), "control_after": _xf_report(after),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def place_rig_pole_vector(ctx, asset_path: str, control: str, root_bone: str, mid_bone: str,
                              end_bone: str, distance: float = None, initial: bool = False,
                              control_type: str = "control") -> str:
        """Place an IK POLE-VECTOR control from a root/mid/end bone chain (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        control:      the CONTROL element to move to the computed pole-vector position.
        root_bone / mid_bone / end_bone: the 3-bone IK chain (e.g. thigh / calf / foot). Their stored
                      global transforms define the plane and bend direction.
        distance:     how far to place the pole out from the mid joint along the bend direction (cm);
                      default = half the chain length ((|root-mid|+|mid-end|)/2).
        initial:      True uses/sets the INITIAL (ref-pose) transforms; default False = current.
        control_type: element type of 'control' (default 'control').

        Computes the pole position purely from the chain's STORED bone transforms in the rig hierarchy
        (project the mid joint onto the root->end line, take the perpendicular 'bend' direction, step
        'distance' out from the mid joint) -- NO mesh geometry and NO runtime/PIE evaluation. A (near)
        straight/collinear chain has an undefined bend direction and is refused (not faked). The control's
        prior global transform is captured (t/q/s) and the move ledgers op 'set_rig_element_transform'
        (same schema as controlrig_write.py; inverse already folded into editor_level.undo restores it
        exactly). The asset is saved."""
        params = {"asset_path": asset_path, "control": control, "root_bone": root_bone,
                  "mid_bone": mid_bone, "end_bone": end_bone, "distance": distance,
                  "initial": initial, "control_type": control_type}
        try:
            return json.dumps(_exec(_POLE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. It introduces NO NEW
    # op schemas -- every WRITE REUSES an already-folded inverse:
    #   create_control_rig_module -> op 'create_asset' {asset_path, package_path, created_dir}  (folded)
    #   place_rig_pole_vector     -> op 'set_rig_element_transform'
    #        {asset_path, key_name, key_type, space:'global', initial, prior_xf:{t,q,s}}         (folded)
    # READ-ONLY (no ledger): analyze_rig_module_asset.
