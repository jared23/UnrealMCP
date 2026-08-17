"""UserTools :: Skeleton / SkeletalMesh authoring (WRITE)  (spec: docs/spec/animation.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE batch; the mutating
counterpart to animation_read.py (which reads skeleton/mesh bones + sockets). Query convention,
base64 PARAMS injection, Output-Log auto-capture, and the per-session undo ledger are copied
VERBATIM from the gold-standard editor_level.py.

WHAT IS REACHABLE + REVERSIBLE in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * SkeletalMesh SOCKET authoring, via the fully-bound USkeletalMesh socket API:
      add_socket(socket, add_to_skeleton=False) / remove_socket(name)->bool /
      find_socket(name) / find_socket_and_index(name) / rename_socket(old,new)->bool /
      num_sockets() / get_socket_by_index(i).
    A USkeletalMeshSocket is created with unreal.new_object(unreal.SkeletalMeshSocket, mesh); its
    RelativeLocation/Rotation/Scale (and ForceAlwaysAnimated) ARE settable via set_editor_property,
    and its parent bone is set with set_socket_parent(mesh, bone) (validated against the skeleton).
    IMPORTANT NAMING QUIRK: SocketName AND BoneName are READ-ONLY UPROPERTYs (set_editor_property
    refused). A freshly add_socket()'d socket is AUTO-named "Socket" by the engine; we read that
    auto-name off the returned handle and rename_socket(auto_name -> desired_name) to name it. Bone
    is set via set_socket_parent (not the read-only bone_name). This is the ONLY route to author a
    named mesh socket from stock Python here.

  * All writes target MESH-OWNED sockets only (socket.get_outer() is the SkeletalMesh). num_sockets/
    get_socket_by_index enumerate BOTH the mesh's own sockets AND its skeleton's promoted sockets;
    remove/rename/transform tools REFUSE a SKELETON-owned socket (outer is a Skeleton) because it is
    SHARED across every mesh on that skeleton and mutating it corrupts the user's skeleton (and the
    Skeleton.Sockets UPROPERTY is C++-protected, so it cannot be cleanly captured/restored). We never
    promote to the skeleton (add_to_skeleton is forced False) for the same reversibility reason.

DEFERRED / refused-not-faked (no faithful reversible route in stock Python 5.8 -- reported honestly):
  * Skeleton (USkeleton) SOCKET authoring: USkeleton exposes NO add/remove-socket method, and its
    Sockets UPROPERTY is PROTECTED ("is protected and cannot be read"/set). new_object(SkeletalMeshSocket,
    skeleton) constructs a socket but there is no bound way to append it to the skeleton's socket
    array. => needs a C++ handler (e.g. MCPReflectionLibrary.AddSkeletonSocketJson mutating
    USkeleton::Sockets + PreEditChange/PostEditChange). The RELATED reachable capability -- promoting
    a mesh socket to the skeleton via add_socket(...,add_to_skeleton=True) -- is refused here because
    remove_socket only removes the mesh copy, leaving an unremovable skeleton-side residue (not
    faithfully reversible from Python).
  * VIRTUAL BONES (USkeleton.AddVirtualBone / RemoveVirtualBones): NO Python binding in this build --
    dir(unreal.Skeleton) exposes only copy_bones_from_skeleton; there is NO virtual_bones property and
    NO unreal.* virtual-bone symbol. => needs a C++ handler.
  * MONTAGE SLOTS / slot groups (USkeleton slot-group setup): NO Python binding (no slot methods on
    USkeleton; slot_group_names property absent). => needs a C++ handler.

Implemented (all validated live on a SCRATCH DUPLICATE of SKM_Manny_Simple, session=agentB, editor
left CLEAN, ledger depth 0):
  - add_skeletal_mesh_socket              (WRITE; ledgered op "add_skeletal_mesh_socket")
  - remove_skeletal_mesh_socket           (WRITE; ledgered op "remove_skeletal_mesh_socket")
  - set_skeletal_mesh_socket_transform    (WRITE; ledgered op "set_skeletal_mesh_socket_transform")
  - rename_skeletal_mesh_socket           (WRITE; ledgered op "rename_skeletal_mesh_socket")

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the ONE unified `undo`).
The four new op schemas + inverse logic are reported to the coordinator to fold into editor_level.undo.
Each mutation runs inside an unreal.ScopedEditorTransaction and the asset is saved (persist + reliable
undo-delete of the scratch dup per PROTOCOL #5c).
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

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
    #   _ledger()                  -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_mesh(path)           -> (SkeletalMesh|None, error|None).
    #   _bone_names(mesh)          -> list of skeleton reference-pose bone names (for validation).
    #   _find_mesh_socket(mesh,nm) -> the MESH-OWNED socket object with that name, else None.
    #   _skeleton_socket(mesh,nm)  -> True if a socket with that name exists but is SKELETON-owned.
    #   _vec/_rot                  -> rounded JSON serialization.
    _SKEL_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_mesh(path):
    if not path:
        return None, "no mesh_path given"
    obj = None
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as _e:
        return None, "failed to load asset: %s (%s)" % (path, str(_e)[:80])
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.SkeletalMesh):
        return None, "asset is not a SkeletalMesh (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _bone_names(mesh):
    try:
        sk = mesh.get_editor_property("skeleton")
        rp = sk.get_reference_pose()
        return [str(b) for b in rp.get_bone_names()]
    except Exception:
        return []
def _find_mesh_socket(mesh, name):
    for i in range(mesh.num_sockets()):
        s = mesh.get_socket_by_index(i)
        if s and str(s.get_editor_property("socket_name")) == name and isinstance(s.get_outer(), unreal.SkeletalMesh):
            return s
    return None
def _skeleton_socket(mesh, name):
    for i in range(mesh.num_sockets()):
        s = mesh.get_socket_by_index(i)
        if s and str(s.get_editor_property("socket_name")) == name and not isinstance(s.get_outer(), unreal.SkeletalMesh):
            return True
    return False
def _vec(v):
    return [round(v.x, 6), round(v.y, 6), round(v.z, 6)] if v is not None else None
def _rot(r):
    return [round(r.pitch, 6), round(r.yaw, 6), round(r.roll, 6)] if r is not None else None
def _mk_socket(mesh, bone, loc, rot, scale, faa):
    # Build + attach a mesh socket, name-agnostic; returns the auto-name assigned by the engine.
    s = unreal.new_object(unreal.SkeletalMeshSocket, mesh)
    s.set_socket_parent(mesh, unreal.Name(bone))
    if loc is not None:
        s.set_editor_property("relative_location", unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])))
    if rot is not None:
        s.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])))
    if scale is not None:
        s.set_editor_property("relative_scale", unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
    if faa is not None:
        try: s.set_editor_property("force_always_animated", bool(faa))
        except Exception: pass
    mesh.add_socket(s, False)
    return s, str(s.get_editor_property("socket_name"))
def _readback(mesh, name):
    s = _find_mesh_socket(mesh, name)
    if s is None:
        return None
    faa = None
    try: faa = bool(s.get_editor_property("force_always_animated"))
    except Exception: faa = None
    return {"socket_name": name, "bone": str(s.get_editor_property("bone_name")),
            "relative_location": _vec(s.get_editor_property("relative_location")),
            "relative_rotation": _rot(s.get_editor_property("relative_rotation")),
            "relative_scale": _vec(s.get_editor_property("relative_scale")),
            "force_always_animated": faa}
'''

    # ------------------------------------------------------------------ #
    # add_skeletal_mesh_socket — create + attach a mesh socket (ledgered)  #
    # ------------------------------------------------------------------ #
    _ADD_BODY = _SKEL_HELPERS + r'''
mesh, err = _load_mesh(PARAMS.get("mesh_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    sock_name = PARAMS.get("socket_name")
    bone = PARAMS.get("parent_bone")
    loc = PARAMS.get("location") or [0.0, 0.0, 0.0]
    rot = PARAMS.get("rotation") or [0.0, 0.0, 0.0]
    scale = PARAMS.get("scale") or [1.0, 1.0, 1.0]
    faa = PARAMS.get("force_always_animated")
    if not sock_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "socket_name is required"}))
    elif not bone:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent_bone is required"}))
    elif mesh.find_socket(unreal.Name(sock_name)) is not None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "a socket named '%s' already exists on this mesh or its skeleton (refusing to overwrite)" % sock_name}))
    else:
        bones = _bone_names(mesh)
        if bones and bone not in bones:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "bone '%s' not found on skeleton (sample: %s)" % (bone, bones[:8])}))
        else:
            with unreal.ScopedEditorTransaction("MCP add_skeletal_mesh_socket"):
                _s, auto = _mk_socket(mesh, bone, loc, rot, scale, faa)
                mesh.rename_socket(unreal.Name(auto), unreal.Name(sock_name))
            try: unreal.EditorAssetLibrary.save_asset(mesh.get_path_name(), only_if_is_dirty=False)
            except Exception: pass
            _ledger().append({"op": "add_skeletal_mesh_socket",
                              "mesh_path": mesh.get_path_name(), "socket_name": sock_name})
            rb = _readback(mesh, sock_name)
            print("@@UMCP@@" + json.dumps({"status": "success", "mesh": mesh.get_name(),
                "mesh_path": mesh.get_path_name(), "socket": rb, "auto_name_before_rename": auto,
                "num_sockets_total": mesh.num_sockets(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_skeletal_mesh_socket(ctx, mesh_path: str, socket_name: str, parent_bone: str,
                                 location: list = None, rotation: list = None, scale: list = None,
                                 force_always_animated: bool = None) -> str:
        """Add a new USkeletalMeshSocket to a SkeletalMesh asset (MESH-owned socket; NOT promoted to
        the shared skeleton). Reversible/ledgered write.

        mesh_path:     SkeletalMesh asset path, e.g.
                       '/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple.SKM_Manny_Simple'.
        socket_name:   name for the new socket (must not collide with any existing mesh OR skeleton
                       socket name -- refused if it does).
        parent_bone:   bone the socket attaches to (validated against the skeleton's bone list).
        location:      [x,y,z] relative location (default [0,0,0]).
        rotation:      [pitch,yaw,roll] relative rotation in degrees (default [0,0,0]).
        scale:         [x,y,z] relative scale (default [1,1,1]).
        force_always_animated: optional bool for the socket's ForceAlwaysAnimated flag.

        Mechanism (stock Python 5.8): new_object(SkeletalMeshSocket, mesh) -> set_socket_parent(mesh,
        bone) + set RelativeLocation/Rotation/Scale -> mesh.add_socket(socket, add_to_skeleton=False)
        -> rename_socket(engine-auto-name -> socket_name), since SocketName/BoneName are read-only.
        The asset is saved. Verify with animation_read.get_skeletal_mesh_info.

        Ledgered op 'add_skeletal_mesh_socket' {mesh_path, socket_name}. Inverse (fold into
        editor_level.undo): load mesh -> remove_socket(socket_name) -> save."""
        params = {"mesh_path": mesh_path, "socket_name": socket_name, "parent_bone": parent_bone,
                  "location": location, "rotation": rotation, "scale": scale,
                  "force_always_animated": force_always_animated}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_skeletal_mesh_socket — remove a mesh socket (ledgered)        #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _SKEL_HELPERS + r'''
mesh, err = _load_mesh(PARAMS.get("mesh_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    sock_name = PARAMS.get("socket_name")
    s = _find_mesh_socket(mesh, sock_name)
    if s is None:
        if _skeleton_socket(mesh, sock_name):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "socket '%s' is a SKELETON-owned socket (shared across all meshes on this "
                           "skeleton) -- refused. Only mesh-owned sockets can be reversibly removed." % sock_name}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "mesh-owned socket not found: %s" % sock_name}))
    else:
        # Capture full prior state for a faithful re-add on undo.
        faa = None
        try: faa = bool(s.get_editor_property("force_always_animated"))
        except Exception: faa = None
        prior = {"bone": str(s.get_editor_property("bone_name")),
                 "location": _vec(s.get_editor_property("relative_location")),
                 "rotation": _rot(s.get_editor_property("relative_rotation")),
                 "scale": _vec(s.get_editor_property("relative_scale")),
                 "force_always_animated": faa}
        with unreal.ScopedEditorTransaction("MCP remove_skeletal_mesh_socket"):
            removed = mesh.remove_socket(unreal.Name(sock_name))
        try: unreal.EditorAssetLibrary.save_asset(mesh.get_path_name(), only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "remove_skeletal_mesh_socket", "mesh_path": mesh.get_path_name(),
                          "socket_name": sock_name, "bone": prior["bone"], "location": prior["location"],
                          "rotation": prior["rotation"], "scale": prior["scale"],
                          "force_always_animated": faa})
        print("@@UMCP@@" + json.dumps({"status": "success", "mesh": mesh.get_name(),
            "mesh_path": mesh.get_path_name(), "removed": bool(removed), "socket_name": sock_name,
            "prior": prior, "num_sockets_total": mesh.num_sockets(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_skeletal_mesh_socket(ctx, mesh_path: str, socket_name: str) -> str:
        """Remove a MESH-owned socket from a SkeletalMesh asset. Reversible/ledgered write.

        mesh_path:   SkeletalMesh asset path.
        socket_name: the socket to remove. Must be a mesh-owned socket; a SKELETON-owned socket
                     (shared across every mesh on that skeleton) is REFUSED (it cannot be captured/
                     restored cleanly and mutating it corrupts the user's skeleton).

        The socket's full prior state (bone + relative location/rotation/scale + ForceAlwaysAnimated)
        is captured so undo re-creates it EXACTLY. The asset is saved.

        Ledgered op 'remove_skeletal_mesh_socket' {mesh_path, socket_name, bone, location, rotation,
        scale, force_always_animated}. Inverse (fold into editor_level.undo): load mesh -> new_object
        socket, set_socket_parent(bone) + set relative transform (+ force_always_animated) ->
        add_socket(False) -> rename engine-auto-name to socket_name -> save."""
        params = {"mesh_path": mesh_path, "socket_name": socket_name}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_skeletal_mesh_socket_transform — update a socket's relative xf   #
    # ------------------------------------------------------------------ #
    _SET_TX_BODY = _SKEL_HELPERS + r'''
mesh, err = _load_mesh(PARAMS.get("mesh_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    sock_name = PARAMS.get("socket_name")
    loc = PARAMS.get("location"); rot = PARAMS.get("rotation"); scale = PARAMS.get("scale")
    s = _find_mesh_socket(mesh, sock_name)
    if s is None:
        if _skeleton_socket(mesh, sock_name):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "socket '%s' is a SKELETON-owned socket (shared) -- refused. Only mesh-owned "
                           "sockets can be transformed reversibly." % sock_name}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "mesh-owned socket not found: %s" % sock_name}))
    elif loc is None and rot is None and scale is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide at least one of location/rotation/scale"}))
    else:
        prior = {"location": _vec(s.get_editor_property("relative_location")),
                 "rotation": _rot(s.get_editor_property("relative_rotation")),
                 "scale": _vec(s.get_editor_property("relative_scale"))}
        with unreal.ScopedEditorTransaction("MCP set_skeletal_mesh_socket_transform"):
            if loc is not None:
                s.set_editor_property("relative_location", unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])))
            if rot is not None:
                s.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])))
            if scale is not None:
                s.set_editor_property("relative_scale", unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
        try: unreal.EditorAssetLibrary.save_asset(mesh.get_path_name(), only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "set_skeletal_mesh_socket_transform", "mesh_path": mesh.get_path_name(),
                          "socket_name": sock_name, "prior": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "mesh": mesh.get_name(),
            "mesh_path": mesh.get_path_name(), "socket_name": sock_name, "before": prior,
            "after": _readback(mesh, sock_name), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_skeletal_mesh_socket_transform(ctx, mesh_path: str, socket_name: str,
                                           location: list = None, rotation: list = None,
                                           scale: list = None) -> str:
        """Update a MESH-owned socket's relative transform on a SkeletalMesh. Partial update -- only
        the components you pass change. Reversible/ledgered write.

        mesh_path:   SkeletalMesh asset path.
        socket_name: the mesh-owned socket to modify (a SKELETON-owned/shared socket is REFUSED).
        location:    [x,y,z] relative location.
        rotation:    [pitch,yaw,roll] relative rotation in degrees.
        scale:       [x,y,z] relative scale.

        The prior relative transform is captured so undo restores it exactly. The asset is saved.

        Ledgered op 'set_skeletal_mesh_socket_transform' {mesh_path, socket_name, prior:{location,
        rotation, scale}}. Inverse (fold into editor_level.undo): load mesh -> find mesh socket ->
        restore prior relative_location/rotation/scale -> save."""
        params = {"mesh_path": mesh_path, "socket_name": socket_name,
                  "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SET_TX_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_skeletal_mesh_socket — rename a mesh socket (ledgered)        #
    # ------------------------------------------------------------------ #
    _RENAME_BODY = _SKEL_HELPERS + r'''
mesh, err = _load_mesh(PARAMS.get("mesh_path"))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    old_name = PARAMS.get("old_name"); new_name = PARAMS.get("new_name")
    if not new_name:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_name is required"}))
    else:
        s = _find_mesh_socket(mesh, old_name)
        if s is None:
            if _skeleton_socket(mesh, old_name):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "socket '%s' is a SKELETON-owned socket (shared) -- refused. Only mesh-owned "
                               "sockets can be renamed here." % old_name}))
            else:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "mesh-owned socket not found: %s" % old_name}))
        elif mesh.find_socket(unreal.Name(new_name)) is not None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "a socket named '%s' already exists (refusing to collide)" % new_name}))
        else:
            with unreal.ScopedEditorTransaction("MCP rename_skeletal_mesh_socket"):
                ok = mesh.rename_socket(unreal.Name(old_name), unreal.Name(new_name))
            try: unreal.EditorAssetLibrary.save_asset(mesh.get_path_name(), only_if_is_dirty=False)
            except Exception: pass
            _ledger().append({"op": "rename_skeletal_mesh_socket", "mesh_path": mesh.get_path_name(),
                              "old_name": old_name, "new_name": new_name})
            print("@@UMCP@@" + json.dumps({"status": "success", "mesh": mesh.get_name(),
                "mesh_path": mesh.get_path_name(), "renamed": bool(ok),
                "old_name": old_name, "new_name": new_name, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_skeletal_mesh_socket(ctx, mesh_path: str, old_name: str, new_name: str) -> str:
        """Rename a MESH-owned socket on a SkeletalMesh. Reversible/ledgered write.

        mesh_path: SkeletalMesh asset path.
        old_name:  current socket name (must be a mesh-owned socket; a SKELETON-owned/shared socket
                   is REFUSED).
        new_name:  new socket name (must not collide with an existing mesh or skeleton socket).

        The asset is saved. Ledgered op 'rename_skeletal_mesh_socket' {mesh_path, old_name, new_name}.
        Inverse (fold into editor_level.undo): load mesh -> rename_socket(new_name -> old_name) -> save."""
        params = {"mesh_path": mesh_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # USkeleton authoring (C++ #13 handlers): skeleton sockets + virtual  #
    # bones. NOTE: these mutate the SHARED skeleton (affects every mesh   #
    # on it) — validate on a scratch DUPLICATE, never the real skeleton.  #
    # ------------------------------------------------------------------ #
    _SKEL_AUTHOR_BODY = _SKEL_HELPERS + r'''
_sk = unreal.EditorAssetLibrary.load_asset(PARAMS["skeleton_path"]) if PARAMS.get("skeleton_path") else None
action = PARAMS["action"]
if _sk is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % PARAMS.get("skeleton_path")}))
elif not isinstance(_sk, unreal.Skeleton):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a Skeleton (got %s): %s" % (_sk.get_class().get_name(), PARAMS.get("skeleton_path"))}))
else:
    _rl = getattr(unreal, "MCPReflectionLibrary", None)
    _fnmap = {"add_socket": "add_skeleton_socket", "remove_socket": "remove_skeleton_socket",
              "add_vb": "add_virtual_bone", "remove_vb": "remove_virtual_bone"}
    _fn = _fnmap[action]
    if _rl is None or not hasattr(_rl, _fn):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.%s unavailable — reload the MCP server after the C++ #13 rebuild" % _fn}))
    else:
        with unreal.ScopedEditorTransaction("MCP skeleton_authoring"):
            if action == "add_socket":
                _loc = PARAMS.get("location") or [0, 0, 0]
                _rot = PARAMS.get("rotation") or [0, 0, 0]
                _scl = PARAMS.get("scale") or [1, 1, 1]
                _res = json.loads(_rl.add_skeleton_socket(_sk, PARAMS["socket_name"], PARAMS["parent_bone"],
                    float(_loc[0]), float(_loc[1]), float(_loc[2]),
                    float(_rot[0]), float(_rot[1]), float(_rot[2]),
                    float(_scl[0]), float(_scl[1]), float(_scl[2])))
            elif action == "remove_socket":
                _res = json.loads(_rl.remove_skeleton_socket(_sk, PARAMS["socket_name"]))
            elif action == "add_vb":
                _res = json.loads(_rl.add_virtual_bone(_sk, PARAMS["source_bone"], PARAMS["target_bone"]))
            else:
                _res = json.loads(_rl.remove_virtual_bone(_sk, PARAMS["virtual_bone_name"]))
        if _res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": _res.get("error")}))
        else:
            try:
                unreal.EditorAssetLibrary.save_asset(PARAMS["skeleton_path"], only_if_is_dirty=False)
            except Exception:
                pass
            if action == "add_socket":
                _op = {"op": "add_skeleton_socket", "skeleton_path": PARAMS["skeleton_path"], "socket_name": PARAMS["socket_name"]}
            elif action == "remove_socket":
                _op = {"op": "remove_skeleton_socket", "skeleton_path": PARAMS["skeleton_path"], "socket_name": PARAMS["socket_name"],
                       "bone": _res.get("bone"), "location": _res.get("location"),
                       "rotation": _res.get("rotation"), "scale": _res.get("scale")}
            elif action == "add_vb":
                _op = {"op": "add_virtual_bone", "skeleton_path": PARAMS["skeleton_path"], "virtual_bone_name": _res.get("virtual_bone_name")}
            else:
                _op = {"op": "remove_virtual_bone", "skeleton_path": PARAMS["skeleton_path"],
                       "source": _res.get("source"), "target": _res.get("target")}
            _ledger().append(_op)
            _res["status"] = "success"; _res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(_res))
'''

    @mcp.tool()
    def add_skeleton_socket(ctx, skeleton_path: str, socket_name: str, parent_bone: str,
                            location: list = None, rotation: list = None, scale: list = None) -> str:
        """Add a socket to a USkeleton (C++ #13 handler). Ledgered, reversible. ⚠️ SHARED across every
        mesh on this skeleton — validate on a scratch DUPLICATE, not the real skeleton.

        skeleton_path: object path of a Skeleton asset.
        socket_name:   name for the new socket (refused if it already exists on the skeleton).
        parent_bone:   bone to attach to (validated against the skeleton).
        location/rotation/scale: [x,y,z] / [pitch,yaw,roll] / [x,y,z] relative transform (default identity).

        Ledgered op 'add_skeleton_socket' {skeleton_path, socket_name}; inverse (folded into
        editor_level.undo): remove_skeleton_socket."""
        params = {"skeleton_path": skeleton_path, "action": "add_socket", "socket_name": socket_name,
                  "parent_bone": parent_bone, "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SKEL_AUTHOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_skeleton_socket(ctx, skeleton_path: str, socket_name: str) -> str:
        """Remove a skeleton socket (C++ #13 handler). Ledgered, reversible. ⚠️ SHARED skeleton.

        skeleton_path: object path of a Skeleton asset.
        socket_name:   the socket to remove. Its bone + relative transform are captured for undo.

        Ledgered op 'remove_skeleton_socket' {skeleton_path, socket_name, bone, location, rotation, scale};
        inverse (folded into editor_level.undo): add_skeleton_socket with the captured state."""
        params = {"skeleton_path": skeleton_path, "action": "remove_socket", "socket_name": socket_name}
        try:
            return json.dumps(_exec(_SKEL_AUTHOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_virtual_bone(ctx, skeleton_path: str, source_bone: str, target_bone: str) -> str:
        """Add a virtual bone (source->target) to a USkeleton (C++ #13 handler). Ledgered, reversible.
        ⚠️ SHARED skeleton.

        skeleton_path: object path of a Skeleton asset.
        source_bone/target_bone: existing bones; the engine assigns the VB name ('VB <src>_<tgt>').

        Ledgered op 'add_virtual_bone' {skeleton_path, virtual_bone_name}; inverse (folded into
        editor_level.undo): remove_virtual_bone(virtual_bone_name)."""
        params = {"skeleton_path": skeleton_path, "action": "add_vb", "source_bone": source_bone, "target_bone": target_bone}
        try:
            return json.dumps(_exec(_SKEL_AUTHOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_virtual_bone(ctx, skeleton_path: str, virtual_bone_name: str) -> str:
        """Remove a virtual bone by name from a USkeleton (C++ #13 handler). Ledgered, reversible.
        ⚠️ SHARED skeleton.

        skeleton_path:     object path of a Skeleton asset.
        virtual_bone_name: the virtual bone to remove (e.g. 'VB hand_r_hand_l'). Its source/target are
                           captured for undo.

        Ledgered op 'remove_virtual_bone' {skeleton_path, source, target}; inverse (folded into
        editor_level.undo): add_virtual_bone(source, target)."""
        params = {"skeleton_path": skeleton_path, "action": "remove_vb", "virtual_bone_name": virtual_bone_name}
        try:
            return json.dumps(_exec(_SKEL_AUTHOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The write ops
    # (mesh sockets + C++ #13 skeleton sockets/virtual-bones) + inverse logic are reported to the
    # coordinator to fold into editor_level.undo.
