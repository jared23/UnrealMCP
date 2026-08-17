"""UserTools :: Control Rig (WRITE)  (spec: docs/spec/controlrig.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). The reversible WRITE
counterpart to controlrig.py (READ). Authors the rig HIERARCHY (URigHierarchy) of a
UControlRigBlueprint via its PUBLIC editing controllers -- NOTHING here is faked or C++-gated:
    bp.get_hierarchy_controller() -> URigHierarchyController  (add/remove/settings/rename)
    bp.get_hierarchy()            -> URigHierarchy            (transform getters/setters)
Query convention, base64 PARAMS injection, Output-Log auto-capture and the per-session undo
ledger are copied VERBATIM from the gold-standard editor_level.py / curves_write.py.

WHY THIS IS REACHABLE WITHOUT C++ (verified live vs TestMCPSetup, UE 5.8.1):
  * RigHierarchyController exposes add_bone / add_null / add_control / remove_element /
    set_control_settings / set_display_name / rename_element / set_parent (all real, callable).
  * URigHierarchy exposes set_local_transform / set_global_transform (initial or current) plus
    get_local/global_transform, get_control_settings, get_control_value, get_children.
  * FRigControlSettings AND FRigControlValue both roundtrip FAITHFULLY through export_text() /
    import_text() -- this is what makes settings-changes and control-removal fully reversible
    (we capture the prior text and re-import it to restore the exact prior struct).

REVERSIBILITY (every tool ledgers a faithful inverse; this module registers NO `undo` tool --
editor_level.py owns the ONE unified `undo`; the op schemas below are reported to the coordinator
to fold in). Each mutation runs inside unreal.ScopedEditorTransaction AND saves the asset (so the
authoring change persists and the inverse-delete/restore is reliable).
  - add_rig_control / add_rig_null / add_rig_bone -> op "add_rig_element"
        {asset_path, key_name, key_type} ; inverse = remove_element(key) + save.
  - remove_rig_element -> op "remove_rig_element"
        {asset_path, element_type, name, parent_name, parent_type, in_local_xf,
         bone_type?, settings_txt?, value_txt?, init_txt?} ; inverse = re-add the element
        (add_bone/add_null/add_control from captured state) + restore local transform + save.
        REFUSED (not shipped as a mutation) when the target has children -- removing a non-leaf
        reparents its children, which a single re-add cannot faithfully restore.
  - set_rig_control_settings -> op "set_rig_control_settings"
        {asset_path, key_name, prior_settings_txt} ; inverse = import_text(prior) + set_control_settings.
  - set_rig_element_transform -> op "set_rig_element_transform"
        {asset_path, key_name, key_type, space, initial, prior_xf} ; inverse = re-set the transform.

DEFERRED / NOT shipped (documented, not faked):
  - RigVM solve-GRAPH authoring (add/remove/connect RigVM nodes on bp.get_controller()) is a
    separate large surface (RigVMController) -- NOT in this hierarchy-authoring module.
  - The COMPILED/evaluated runtime (URigVM bytecode / posed skeleton) is not written here
    (needs a C++ runtime probe -- same DEFERRED-C++ bucket as controlrig.py's read notes).
  - Non-leaf removal (see above) is refused rather than shipped un-faithfully.

Implemented (all validated live on a SCRATCH DUPLICATE, editor left CLEAN, ledger depth 0):
  - add_rig_control          (WRITE; ledgered "add_rig_element")
  - add_rig_null             (WRITE; ledgered "add_rig_element")
  - add_rig_bone             (WRITE; ledgered "add_rig_element")
  - remove_rig_element       (WRITE; ledgered "remove_rig_element"; leaf-only, faithful reconstruct)
  - set_rig_control_settings (WRITE; ledgered "set_rig_control_settings"; faithful via export_text)
  - set_rig_element_transform(WRITE; ledgered "set_rig_element_transform"; faithful prior capture)
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
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed
# as base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
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
_ETYPES = {"bone": unreal.RigElementType.BONE, "null": unreal.RigElementType.NULL,
           "control": unreal.RigElementType.CONTROL, "curve": unreal.RigElementType.CURVE,
           "socket": unreal.RigElementType.SOCKET, "connector": unreal.RigElementType.CONNECTOR,
           "reference": unreal.RigElementType.REFERENCE}
def _etype(s):
    return _ETYPES.get(str(s or "").strip().lower())
def _etype_short(k):
    return str(k.type).split(".")[-1].split(":")[0].strip()
def _ctype(s):
    m = {"float": unreal.RigControlType.FLOAT, "scalefloat": unreal.RigControlType.SCALE_FLOAT,
         "bool": unreal.RigControlType.BOOL, "integer": unreal.RigControlType.INTEGER,
         "int": unreal.RigControlType.INTEGER, "vector2d": unreal.RigControlType.VECTOR2D,
         "position": unreal.RigControlType.POSITION, "scale": unreal.RigControlType.SCALE,
         "rotator": unreal.RigControlType.ROTATOR,
         "eulertransform": unreal.RigControlType.EULER_TRANSFORM,
         "transform": unreal.RigControlType.EULER_TRANSFORM}
    return m.get(str(s or "").strip().lower().replace("_", "").replace(" ", ""))
def _atype(s):
    m = {"animationcontrol": unreal.RigControlAnimationType.ANIMATION_CONTROL,
         "animation": unreal.RigControlAnimationType.ANIMATION_CONTROL,
         "proxycontrol": unreal.RigControlAnimationType.PROXY_CONTROL,
         "proxy": unreal.RigControlAnimationType.PROXY_CONTROL,
         "visualcue": unreal.RigControlAnimationType.VISUAL_CUE,
         "visual": unreal.RigControlAnimationType.VISUAL_CUE}
    a = getattr(unreal, "RigControlAnimationType", None)
    return m.get(str(s or "").strip().lower().replace("_", "").replace(" ", ""), (a.ANIMATION_CONTROL if a else None))
def _resolve_key(h, name, etype):
    want = str(name)
    for k in (h.get_all_keys(True) or []):
        if str(k.name) == want and (etype is None or k.type == etype):
            return k
    return None
def _parent_key(name, etype_str):
    if not name:
        return unreal.RigElementKey()
    et = _etype(etype_str) or unreal.RigElementType.BONE
    return unreal.RigElementKey(name=unreal.Name(str(name)), type=et)
def _mk_xf(loc, rot, scale, base=None):
    t = base if base is not None else unreal.Transform()
    if loc is not None:
        t.set_editor_property("translation", unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])))
    if rot is not None:
        q = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])).quaternion()
        t.set_editor_property("rotation", q)
    if scale is not None:
        t.set_editor_property("scale3d", unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
    return t
def _xf_to_dict(t):
    tr = t.translation; q = t.rotation; s = t.scale3d
    return {"t": [tr.x, tr.y, tr.z], "q": [q.x, q.y, q.z, q.w], "s": [s.x, s.y, s.z]}
def _dict_to_xf(d):
    t = unreal.Transform()
    t.set_editor_property("translation", unreal.Vector(d["t"][0], d["t"][1], d["t"][2]))
    t.set_editor_property("rotation", unreal.Quat(d["q"][0], d["q"][1], d["q"][2], d["q"][3]))
    t.set_editor_property("scale3d", unreal.Vector(d["s"][0], d["s"][1], d["s"][2]))
    return t
def _xf_report(t):
    tr = t.translation; r = t.rotation.rotator(); s = t.scale3d
    return {"location": [round(tr.x, 3), round(tr.y, 3), round(tr.z, 3)],
            "rotation": [round(r.pitch, 3), round(r.yaw, 3), round(r.roll, 3)],
            "scale": [round(s.x, 3), round(s.y, 3), round(s.z, 3)]}
def _default_ctrl_value(h, ct):
    RCT = unreal.RigControlType
    if ct in (RCT.FLOAT, RCT.SCALE_FLOAT):
        return h.make_control_value_from_float(0.0)
    if ct == RCT.BOOL:
        return h.make_control_value_from_bool(False)
    if ct == RCT.INTEGER:
        return h.make_control_value_from_int(0)
    if ct == RCT.VECTOR2D:
        return h.make_control_value_from_vector2d(unreal.Vector2D(0.0, 0.0))
    if ct == RCT.POSITION:
        return h.make_control_value_from_vector(unreal.Vector(0.0, 0.0, 0.0))
    if ct == RCT.SCALE:
        return h.make_control_value_from_vector(unreal.Vector(1.0, 1.0, 1.0))
    if ct == RCT.ROTATOR:
        return h.make_control_value_from_rotator(unreal.Rotator(0.0, 0.0, 0.0))
    if ct == RCT.EULER_TRANSFORM:
        return h.make_control_value_from_euler_transform(unreal.EulerTransform())
    return h.make_control_value_from_float(0.0)
def _rcvt(s):
    m = {"current": unreal.RigControlValueType.CURRENT, "initial": unreal.RigControlValueType.INITIAL,
         "minimum": unreal.RigControlValueType.MINIMUM, "min": unreal.RigControlValueType.MINIMUM,
         "maximum": unreal.RigControlValueType.MAXIMUM, "max": unreal.RigControlValueType.MAXIMUM}
    return m.get(str(s or "current").strip().lower(), unreal.RigControlValueType.CURRENT)
def _rcvt_token(s):
    t = str(s or "current").strip().lower()
    if t == "min":
        t = "minimum"
    if t == "max":
        t = "maximum"
    return t if t in ("current", "initial", "minimum", "maximum") else "current"
def _make_ctrl_value(h, ct, v):
    RCT = unreal.RigControlType
    if ct in (RCT.FLOAT, RCT.SCALE_FLOAT):
        return h.make_control_value_from_float(float(v if v is not None else 0.0))
    if ct == RCT.BOOL:
        return h.make_control_value_from_bool(bool(v))
    if ct == RCT.INTEGER:
        return h.make_control_value_from_int(int(v if v is not None else 0))
    if ct == RCT.VECTOR2D:
        return h.make_control_value_from_vector2d(unreal.Vector2D(float(v[0]), float(v[1])))
    if ct in (RCT.POSITION, RCT.SCALE):
        d = v if v is not None else ([1.0, 1.0, 1.0] if ct == RCT.SCALE else [0.0, 0.0, 0.0])
        return h.make_control_value_from_vector(unreal.Vector(float(d[0]), float(d[1]), float(d[2])))
    if ct == RCT.ROTATOR:
        d = v if v is not None else [0.0, 0.0, 0.0]
        return h.make_control_value_from_rotator(unreal.Rotator(float(d[0]), float(d[1]), float(d[2])))
    if ct == RCT.EULER_TRANSFORM:
        return h.make_control_value_from_euler_transform(unreal.EulerTransform())
    return h.make_control_value_from_float(float(v if v is not None else 0.0))
def _float_from_value(h, ct, val):
    RCT = unreal.RigControlType
    try:
        if ct in (RCT.FLOAT, RCT.SCALE_FLOAT):
            return h.get_float_from_control_value(val)
        if ct == RCT.BOOL:
            return h.get_bool_from_control_value(val)
        if ct == RCT.INTEGER:
            return h.get_int_from_control_value(val)
    except Exception:
        return None
    return None
'''

    # ------------------------------------------------------------------ #
    # add_rig_bone / add_rig_null — shared body (parameterized)           #
    # ------------------------------------------------------------------ #
    _ADD_BN_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
kind = PARAMS.get("kind")
name = PARAMS.get("name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a name"}))
else:
    hc = bp.get_hierarchy_controller()
    h = bp.get_hierarchy()
    pk = _parent_key(PARAMS.get("parent_name"), PARAMS.get("parent_type"))
    if PARAMS.get("parent_name") and not h.contains(pk):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "parent not found: %s (%s)" % (PARAMS.get("parent_name"), PARAMS.get("parent_type"))}))
    else:
        xf = _mk_xf(PARAMS.get("location"), PARAMS.get("rotation"), PARAMS.get("scale"))
        in_global = bool(PARAMS.get("in_global", False))
        with unreal.ScopedEditorTransaction("MCP add_rig_" + kind):
            if kind == "bone":
                bt = unreal.RigBoneType.USER
                key = hc.add_bone(unreal.Name(name), pk, xf, in_global, bt, False, False)
            else:
                key = hc.add_null(unreal.Name(name), pk, xf, in_global, False, False)
        real_name = str(key.name)
        _ledger().append({"op": "add_rig_element", "asset_path": path,
                          "key_name": real_name, "key_type": _etype_short(key)})
        _save_cr(path)
        lt = _try(lambda: h.get_local_transform(key, False))
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "kind": kind, "requested_name": name, "name": real_name, "type": _etype_short(key),
            "parent": (str(pk.name) if PARAMS.get("parent_name") else None),
            "local_transform": (_xf_report(lt) if lt is not None else None),
            "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_bone(ctx, asset_path: str, name: str, parent_name: str = None,
                     parent_type: str = "bone", location: list = None,
                     rotation: list = None, scale: list = None,
                     in_global: bool = False) -> str:
        """Add a BONE element to a Control Rig's hierarchy (ledgered, reversible write).

        asset_path:  ControlRigBlueprint path (e.g. '/Game/.../CR_Mannequin_Body.CR_Mannequin_Body').
        name:        suggested bone name (the namespace may adjust it; the real name is returned).
        parent_name/parent_type: optional parent element (parent_type default 'bone'). Omit for a root.
        location/rotation/scale: initial transform ([x,y,z] / [pitch,yaw,roll] / [x,y,z]); default identity.
        in_global:   True if the transform is in global space (default False = local/parent space).

        Added as a USER bone via RigHierarchyController.add_bone. The asset is saved.
        Ledgered op 'add_rig_element' {asset_path,key_name,key_type}; inverse removes the element."""
        params = {"asset_path": asset_path, "kind": "bone", "name": name,
                  "parent_name": parent_name, "parent_type": parent_type,
                  "location": location, "rotation": rotation, "scale": scale, "in_global": in_global}
        try:
            return json.dumps(_exec(_ADD_BN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_rig_null(ctx, asset_path: str, name: str, parent_name: str = None,
                     parent_type: str = "null", location: list = None,
                     rotation: list = None, scale: list = None,
                     in_global: bool = False) -> str:
        """Add a NULL (space) element to a Control Rig's hierarchy (ledgered, reversible write).

        asset_path:  ControlRigBlueprint path.
        name:        suggested null name (namespace may adjust; real name returned).
        parent_name/parent_type: optional parent (parent_type default 'null'). Omit for a root.
        location/rotation/scale: initial transform; default identity.
        in_global:   True if the transform is global (default False = local).

        Added via RigHierarchyController.add_null. Nulls are transform-only spaces (no shape/settings).
        The asset is saved. Ledgered op 'add_rig_element'; inverse removes the element."""
        params = {"asset_path": asset_path, "kind": "null", "name": name,
                  "parent_name": parent_name, "parent_type": parent_type,
                  "location": location, "rotation": rotation, "scale": scale, "in_global": in_global}
        try:
            return json.dumps(_exec(_ADD_BN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_control — add a Control element                             #
    # ------------------------------------------------------------------ #
    _ADD_CTRL_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a name"}))
else:
    ct = _ctype(PARAMS.get("control_type") or "float")
    if ct is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown control_type: %s (use Float/Bool/Integer/Vector2D/Position/Scale/Rotator/EulerTransform)" % PARAMS.get("control_type")}))
    else:
        hc = bp.get_hierarchy_controller()
        h = bp.get_hierarchy()
        pk = _parent_key(PARAMS.get("parent_name"), PARAMS.get("parent_type"))
        if PARAMS.get("parent_name") and not h.contains(pk):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "parent not found: %s (%s)" % (PARAMS.get("parent_name"), PARAMS.get("parent_type"))}))
        else:
            cs = unreal.RigControlSettings()
            cs.set_editor_property("control_type", ct)
            at = _atype(PARAMS.get("animation_type"))
            if at is not None:
                cs.set_editor_property("animation_type", at)
            dn = PARAMS.get("display_name")
            if dn:
                cs.set_editor_property("display_name", unreal.Name(str(dn)))
            val = _default_ctrl_value(h, ct)
            with unreal.ScopedEditorTransaction("MCP add_rig_control"):
                key = hc.add_control(unreal.Name(name), pk, cs, val, False, False)
            real_name = str(key.name)
            _ledger().append({"op": "add_rig_element", "asset_path": path,
                              "key_name": real_name, "key_type": _etype_short(key)})
            _save_cr(path)
            csr = _try(lambda: h.get_control_settings(key))
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "requested_name": name, "name": real_name, "type": _etype_short(key),
                "control_type": (str(csr.get_editor_property("control_type")).split(".")[-1].split(":")[0] if csr else None),
                "parent": (str(pk.name) if PARAMS.get("parent_name") else None),
                "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_control(ctx, asset_path: str, name: str, control_type: str = "Float",
                        parent_name: str = None, parent_type: str = "null",
                        animation_type: str = "AnimationControl",
                        display_name: str = None) -> str:
        """Add a CONTROL element to a Control Rig's hierarchy (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        name:         suggested control name (namespace may adjust; real name returned).
        control_type: Float | Bool | Integer | Vector2D | Position | Scale | Rotator | EulerTransform
                      (default Float). The control is seeded with a sensible default value for the type.
        parent_name/parent_type: optional parent (parent_type default 'null'). Omit for a root control.
        animation_type: AnimationControl | ProxyControl | VisualCue (default AnimationControl).
        display_name:   optional friendly display name.

        Added via RigHierarchyController.add_control with an FRigControlSettings + default
        FRigControlValue. The asset is saved. Ledgered op 'add_rig_element'; inverse removes it.
        Shape/limit/color tuning after creation: use set_rig_control_settings; transform: set_rig_element_transform."""
        params = {"asset_path": asset_path, "name": name, "control_type": control_type,
                  "parent_name": parent_name, "parent_type": parent_type,
                  "animation_type": animation_type, "display_name": display_name}
        try:
            return json.dumps(_exec(_ADD_CTRL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_rig_element — leaf-only; faithful reconstruct inverse         #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
etype = _etype(PARAMS.get("element_type"))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif etype is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "provide element_type: bone|null|control|socket|connector|reference"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, etype)
    if key is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s named %r in %s" % (PARAMS.get("element_type"), str(name), path)}))
    else:
        kids = _try(lambda: h.get_children(key, False)) or []
        if len(kids) > 0:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing to remove %r: it has %d child element(s) %s. Removing a non-leaf reparents its children, which is not faithfully reversible by a single re-add. Remove the children first." % (str(name), len(kids), [str(c.name) for c in kids][:10])}))
        else:
            short = _etype_short(key)
            par = _try(lambda: h.get_first_parent(key))
            has_par = par is not None and str(par.type).split(".")[-1].split(":")[0].strip() not in ("NONE", "")
            cap = {"op": "remove_rig_element", "asset_path": path, "element_type": short.lower(),
                   "name": str(key.name),
                   "parent_name": (str(par.name) if has_par else None),
                   "parent_type": (str(par.type).split(".")[-1].split(":")[0].strip().lower() if has_par else None),
                   "in_local_xf": _xf_to_dict(h.get_local_transform(key, False))}
            if short == "BONE":
                cap["bone_type"] = str(_try(lambda: h.get_bone_type(key))).split(".")[-1].split(":")[0].strip()
            if short == "CONTROL":
                cap["settings_txt"] = h.get_control_settings(key).export_text()
                cap["value_txt"] = h.get_control_value(key, unreal.RigControlValueType.CURRENT).export_text()
                cap["init_txt"] = h.get_control_value(key, unreal.RigControlValueType.INITIAL).export_text()
            with unreal.ScopedEditorTransaction("MCP remove_rig_element"):
                ok = hc.remove_element(key, False, False)
            if not ok:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "remove_element returned False for %r" % str(name)}))
            else:
                _ledger().append(cap)
                _save_cr(path)
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "removed": cap["name"], "type": short, "reconstructable": True,
                    "captured": [k for k in ("parent_name", "bone_type", "settings_txt", "value_txt") if k in cap],
                    "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_rig_element(ctx, asset_path: str, name: str, element_type: str) -> str:
        """Remove a LEAF element (bone/null/control/socket/...) from a Control Rig hierarchy
        (ledgered, faithfully reversible write).

        asset_path:   ControlRigBlueprint path.
        name:         element name to remove.
        element_type: bone | null | control | socket | connector | reference (required to disambiguate;
                      names can repeat across types).

        REFUSES if the element has children (removing a non-leaf reparents its children, which a single
        re-add cannot faithfully restore -- remove the children first). Before removal the full prior
        state is captured -- parent, local transform, bone_type, and (for controls) the FRigControlSettings
        + current/initial FRigControlValue via export_text -- so the inverse re-adds an identical element.
        The asset is saved. Ledgered op 'remove_rig_element'; inverse rebuilds the element + transform."""
        params = {"asset_path": asset_path, "name": name, "element_type": element_type}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_control_settings — adjust an existing control's settings     #
    # ------------------------------------------------------------------ #
    _SET_SETTINGS_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("control_name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, unreal.RigElementType.CONTROL)
    if key is None:
        controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no CONTROL named %r in %s" % (str(name), path), "available_controls": controls[:200]}))
    else:
        prior_txt = h.get_control_settings(key).export_text()
        ns = unreal.RigControlSettings()
        ns.import_text(prior_txt)
        changed = []
        if PARAMS.get("display_name") is not None:
            ns.set_editor_property("display_name", unreal.Name(str(PARAMS.get("display_name")))); changed.append("display_name")
        if PARAMS.get("shape_name") is not None:
            ns.set_editor_property("shape_name", unreal.Name(str(PARAMS.get("shape_name")))); changed.append("shape_name")
        if PARAMS.get("shape_visible") is not None:
            ns.set_editor_property("shape_visible", bool(PARAMS.get("shape_visible"))); changed.append("shape_visible")
        if PARAMS.get("draw_limits") is not None:
            ns.set_editor_property("draw_limits", bool(PARAMS.get("draw_limits"))); changed.append("draw_limits")
        if PARAMS.get("shape_color") is not None:
            c = PARAMS.get("shape_color")
            a = float(c[3]) if len(c) > 3 else 1.0
            ns.set_editor_property("shape_color", unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), a)); changed.append("shape_color")
        if not changed:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no settings provided (set one of display_name/shape_name/shape_visible/draw_limits/shape_color)"}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_rig_control_settings"):
                ok = hc.set_control_settings(key, ns, False)
            _ledger().append({"op": "set_rig_control_settings", "asset_path": path,
                              "key_name": str(key.name), "prior_settings_txt": prior_txt})
            _save_cr(path)
            after = h.get_control_settings(key)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "control": str(key.name), "set_ok": bool(ok), "changed": changed,
                "display_name": str(after.get_editor_property("display_name")),
                "shape_visible": bool(after.get_editor_property("shape_visible")),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_control_settings(ctx, asset_path: str, control_name: str,
                                 display_name: str = None, shape_name: str = None,
                                 shape_visible: bool = None, draw_limits: bool = None,
                                 shape_color: list = None) -> str:
        """Adjust an existing Control's FRigControlSettings (ledgered, faithfully reversible write).

        asset_path:   ControlRigBlueprint path.
        control_name: the CONTROL element name.
        display_name: new friendly display name.
        shape_name:   gizmo shape name (e.g. 'Circle_Thick', 'Box_Thick').
        shape_visible: show/hide the control's gizmo (bool).
        draw_limits:  draw limit bounds (bool).
        shape_color:  gizmo color [r,g,b] or [r,g,b,a] (0..1 linear).

        Only the fields you pass are changed; the rest are preserved. The FULL prior FRigControlSettings
        is captured via export_text before the edit, so the inverse restores the exact prior settings
        (control_type and every other field). The asset is saved. Ledgered op 'set_rig_control_settings'."""
        params = {"asset_path": asset_path, "control_name": control_name,
                  "display_name": display_name, "shape_name": shape_name,
                  "shape_visible": shape_visible, "draw_limits": draw_limits, "shape_color": shape_color}
        try:
            return json.dumps(_exec(_SET_SETTINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_element_transform — set an element's transform (ledgered)    #
    # ------------------------------------------------------------------ #
    _SET_XF_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
etype = _etype(PARAMS.get("element_type"))
space = str(PARAMS.get("space") or "local").strip().lower()
initial = bool(PARAMS.get("initial", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif etype is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "provide element_type: bone|null|control|socket|..."}))
elif space not in ("local", "global"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "space must be 'local' or 'global'"}))
else:
    h = bp.get_hierarchy()
    key = _resolve_key(h, name, etype)
    if key is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s named %r in %s" % (PARAMS.get("element_type"), str(name), path)}))
    else:
        def _get():
            return h.get_global_transform(key, initial) if space == "global" else h.get_local_transform(key, initial)
        cur = _get()
        prior = _xf_to_dict(cur)
        newxf = _mk_xf(PARAMS.get("location"), PARAMS.get("rotation"), PARAMS.get("scale"), base=_dict_to_xf(prior))
        with unreal.ScopedEditorTransaction("MCP set_rig_element_transform"):
            if space == "global":
                h.set_global_transform(key, newxf, initial, True, False, False)
            else:
                h.set_local_transform(key, newxf, initial, True, False, False)
        _ledger().append({"op": "set_rig_element_transform", "asset_path": path,
                          "key_name": str(key.name), "key_type": _etype_short(key),
                          "space": space, "initial": initial, "prior_xf": prior})
        _save_cr(path)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "name": str(key.name), "type": _etype_short(key), "space": space, "initial": initial,
            "before": _xf_report(_dict_to_xf(prior)), "after": _xf_report(_get()),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_element_transform(ctx, asset_path: str, name: str, element_type: str,
                                  location: list = None, rotation: list = None,
                                  scale: list = None, space: str = "local",
                                  initial: bool = False) -> str:
        """Set an element's transform on a Control Rig (ledgered, faithfully reversible write).

        asset_path:   ControlRigBlueprint path.
        name:         element name.
        element_type: bone | null | control | socket | ... (required to resolve the key).
        location/rotation/scale: [x,y,z] / [pitch,yaw,roll] / [x,y,z]. Only the components you pass are
                      changed; the others keep their current value.
        space:        'local' (parent space, default) or 'global' (world space).
        initial:      True to set the INITIAL (rest/ref-pose) transform; False (default) the CURRENT one.

        The full prior transform is captured (translation + quaternion rotation + scale) so the inverse
        restores it exactly. The asset is saved. Ledgered op 'set_rig_element_transform'."""
        params = {"asset_path": asset_path, "name": name, "element_type": element_type,
                  "location": location, "rotation": rotation, "scale": scale,
                  "space": space, "initial": initial}
        try:
            return json.dumps(_exec(_SET_XF_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # G1-B additions: CONTROLS depth + HIERARCHY depth                    #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # set_rig_control_value — set a control's value (ledgered)            #
    # ------------------------------------------------------------------ #
    _SET_VALUE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("control_name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    key = _resolve_key(h, name, unreal.RigElementType.CONTROL)
    if key is None:
        controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no CONTROL named %r in %s" % (str(name), path), "available_controls": controls[:200]}))
    elif PARAMS.get("value") is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a value"}))
    else:
        vt = _rcvt(PARAMS.get("value_type"))
        vtok = _rcvt_token(PARAMS.get("value_type"))
        cs = h.get_control_settings(key)
        ct = cs.get_editor_property("control_type")
        prior_txt = h.get_control_value(key, vt).export_text()
        newv = _make_ctrl_value(h, ct, PARAMS.get("value"))
        with unreal.ScopedEditorTransaction("MCP set_rig_control_value"):
            h.set_control_value(key, newv, vt, False, False)
        _ledger().append({"op": "set_rig_control_value", "asset_path": path,
                          "key_name": str(key.name), "value_type": vtok, "prior_value_txt": prior_txt})
        _save_cr(path)
        after = h.get_control_value(key, vt)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "control": str(key.name), "control_type": str(ct).split(".")[-1].split(":")[0].strip(),
            "value_type": vtok, "scalar_after": _float_from_value(h, ct, after),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_control_value(ctx, asset_path: str, control_name: str, value,
                              value_type: str = "current") -> str:
        """Set a Control's VALUE (the animatable channel value) on a Control Rig (ledgered, reversible).

        asset_path:   ControlRigBlueprint path.
        control_name: the CONTROL element name (also works for animation channels, which are controls).
        value:        the new value, typed to the control: Float/ScaleFloat -> number; Bool -> true/false;
                      Integer -> int; Vector2D -> [x,y]; Position/Scale -> [x,y,z]; Rotator -> [pitch,yaw,roll].
                      (Transform controls: use set_rig_element_transform / set_rig_control_offset instead.)
        value_type:   current (default) | initial | minimum | maximum.

        Distinct from set_rig_element_transform: this writes the control's FRigControlValue channel (what an
        animator keys), not the element's bone/space transform. The full prior FRigControlValue is captured
        via export_text so the inverse restores it exactly. The asset is saved.
        Ledgered op 'set_rig_control_value' {asset_path,key_name,value_type,prior_value_txt}."""
        params = {"asset_path": asset_path, "control_name": control_name,
                  "value": value, "value_type": value_type}
        try:
            return json.dumps(_exec(_SET_VALUE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_control_offset — set a control's OFFSET transform           #
    # ------------------------------------------------------------------ #
    _SET_OFFSET_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("control_name")
initial = bool(PARAMS.get("initial", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, unreal.RigElementType.CONTROL)
    if key is None:
        controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no CONTROL named %r in %s" % (str(name), path), "available_controls": controls[:200]}))
    else:
        prior_txt = hc.export_to_text([key])
        g_before = h.get_global_control_offset_transform(key, initial)
        xf = _mk_xf(PARAMS.get("location"), PARAMS.get("rotation"), PARAMS.get("scale"))
        with unreal.ScopedEditorTransaction("MCP set_rig_control_offset"):
            h.set_control_offset_transform(key, xf, initial, True, False, False)
        _ledger().append({"op": "set_rig_control_offset", "asset_path": path,
                          "key_name": str(key.name), "prior_element_txt": prior_txt})
        _save_cr(path)
        g_after = h.get_global_control_offset_transform(key, initial)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "control": str(key.name), "initial": initial,
            "offset_global_before": _xf_report(g_before), "offset_global_after": _xf_report(g_after),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_control_offset(ctx, asset_path: str, control_name: str, location: list = None,
                               rotation: list = None, scale: list = None, initial: bool = False) -> str:
        """Set a Control's OFFSET transform (the control's parent-space zero pose) on a Control Rig
        (ledgered, faithfully reversible).

        asset_path:   ControlRigBlueprint path.
        control_name: the CONTROL element name.
        location/rotation/scale: [x,y,z] / [pitch,yaw,roll] / [x,y,z]. The offset is set ABSOLUTELY;
                      unspecified components default to identity (loc 0, rot 0, scale 1).
        initial:      True to set the INITIAL offset (default False = current).

        The control offset is the transform between the control's parent and the control's neutral pose
        (moving it re-poses the gizmo without changing the keyed value). The full prior element state is
        captured via RigHierarchyController.export_to_text and the inverse restores it via import_from_text
        (replace) -- confirmed faithful for offset. The asset is saved.
        Ledgered op 'set_rig_control_offset' {asset_path,key_name,prior_element_txt}."""
        params = {"asset_path": asset_path, "control_name": control_name,
                  "location": location, "rotation": rotation, "scale": scale, "initial": initial}
        try:
            return json.dumps(_exec(_SET_OFFSET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_control_shape — gizmo shape name/color + shape transform    #
    # ------------------------------------------------------------------ #
    _SET_SHAPE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("control_name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, unreal.RigElementType.CONTROL)
    if key is None:
        controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no CONTROL named %r in %s" % (str(name), path), "available_controls": controls[:200]}))
    else:
        prior_set = h.get_control_settings(key).export_text()
        prior_shape_xf = _xf_to_dict(h.get_local_control_shape_transform(key, False))
        ns = unreal.RigControlSettings()
        ns.import_text(prior_set)
        changed = []
        if PARAMS.get("shape_name") is not None:
            ns.set_editor_property("shape_name", unreal.Name(str(PARAMS.get("shape_name")))); changed.append("shape_name")
        if PARAMS.get("shape_color") is not None:
            c = PARAMS.get("shape_color")
            a = float(c[3]) if len(c) > 3 else 1.0
            ns.set_editor_property("shape_color", unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), a)); changed.append("shape_color")
        has_xf = any(PARAMS.get(k) is not None for k in ("location", "rotation", "scale"))
        if not changed and not has_xf:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no shape change provided (set shape_name/shape_color and/or location/rotation/scale)"}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_rig_control_shape"):
                if changed:
                    ns.set_editor_property("shape_visible", True)
                    hc.set_control_settings(key, ns, False)
                if has_xf:
                    sxf = _mk_xf(PARAMS.get("location"), PARAMS.get("rotation"), PARAMS.get("scale"))
                    h.set_control_shape_transform(key, sxf, False, False)
                    changed.append("shape_transform")
            _ledger().append({"op": "set_rig_control_shape", "asset_path": path, "key_name": str(key.name),
                              "prior_settings_txt": prior_set, "prior_shape_xf": prior_shape_xf})
            _save_cr(path)
            after = h.get_control_settings(key)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "control": str(key.name), "changed": changed,
                "shape_name": str(after.get_editor_property("shape_name")),
                "shape_transform": _xf_report(h.get_local_control_shape_transform(key, False)),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_control_shape(ctx, asset_path: str, control_name: str, shape_name: str = None,
                              shape_color: list = None, location: list = None,
                              rotation: list = None, scale: list = None) -> str:
        """Assign a Control's gizmo SHAPE -- name, color, and/or the shape (gizmo) transform
        (ledgered, faithfully reversible).

        asset_path:   ControlRigBlueprint path.
        control_name: the CONTROL element name.
        shape_name:   gizmo shape name from a shape library (see list_rig_control_shapes,
                      e.g. 'Circle_Thick', 'Box_Thick', 'Diamond_Solid').
        shape_color:  gizmo color [r,g,b] or [r,g,b,a] (0..1 linear).
        location/rotation/scale: the shape (gizmo) transform relative to the control -- resizes/repositions
                      the drawn gizmo only (not the control's value or offset).

        Unlike set_rig_control_settings (general settings) this is the shape-specific tool and additionally
        writes the gizmo transform (set_control_shape_transform), which settings alone cannot. Setting a
        shape_name/color also forces the gizmo visible. The full prior FRigControlSettings (export_text)
        plus the prior local shape transform are captured; the inverse restores both exactly. Asset saved.
        Ledgered op 'set_rig_control_shape' {asset_path,key_name,prior_settings_txt,prior_shape_xf}."""
        params = {"asset_path": asset_path, "control_name": control_name, "shape_name": shape_name,
                  "shape_color": shape_color, "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SET_SHAPE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_rig_control_shapes — enumerate gizmo shape libraries (READ)    #
    # ------------------------------------------------------------------ #
    _LIST_SHAPES_BODY = _CR_HELPERS + r'''
ar = unreal.AssetRegistryHelpers.get_asset_registry()
flt = str(PARAMS.get("filter") or "").strip().lower()
libpath = PARAMS.get("library_path")
libs = []
if libpath:
    lib = _try(lambda: unreal.EditorAssetLibrary.load_asset(libpath))
    if lib is not None:
        libs.append((str(lib.get_name()), lib))
else:
    f = unreal.ARFilter(class_names=["ControlRigShapeLibrary"], recursive_classes=True)
    for a in (_try(lambda: ar.get_assets(f)) or []):
        p = str(a.package_name) + "." + str(a.asset_name)
        lib = _try(lambda p=p: unreal.EditorAssetLibrary.load_asset(p))
        if lib is not None:
            libs.append((str(a.asset_name), lib))
result = []
total = 0
for (nm, lib) in libs:
    shapes = _try(lambda lib=lib: lib.get_editor_property("shapes")) or []
    names = []
    for s in shapes:
        snm = str(_try(lambda s=s: s.get_editor_property("shape_name")))
        if flt and flt not in snm.lower():
            continue
        names.append(snm)
    total += len(names)
    dflt = _try(lambda lib=lib: lib.get_editor_property("default_shape"))
    result.append({"library": nm, "path": lib.get_path_name(),
                   "default_shape": str(_try(lambda dflt=dflt: dflt.get_editor_property("shape_name"))) if dflt else None,
                   "shape_count": len(shapes), "shapes": names})
print("@@UMCP@@" + json.dumps({"status": "success", "library_count": len(result),
    "matched_shapes": total, "filter": (PARAMS.get("filter") or None), "libraries": result}))
'''

    @mcp.tool()
    def list_rig_control_shapes(ctx, filter: str = None, library_path: str = None) -> str:
        """List available Control-Rig gizmo SHAPE names from the project's shape libraries (READ-ONLY).

        filter:       optional case-insensitive substring to match shape names (e.g. 'circle').
        library_path: optional single ControlRigShapeLibrary asset path to enumerate; if omitted, every
                      ControlRigShapeLibrary in the project is scanned (via the Asset Registry).

        Returns each library's name/path, its default_shape, shape_count, and the (filtered) shape name
        list -- the valid values for set_rig_control_shape's shape_name. Read-only: no ledger entry."""
        params = {"filter": filter, "library_path": library_path}
        try:
            return json.dumps(_exec(_LIST_SHAPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_animation_channel — add an animation channel under a control#
    # ------------------------------------------------------------------ #
    _ADD_CHANNEL_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
pname = PARAMS.get("parent_control")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a name"}))
elif not pname:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide parent_control (an existing CONTROL name)"}))
else:
    ct = _ctype(PARAMS.get("control_type") or "float")
    if ct is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown control_type: %s (Float/Bool/Integer/Vector2D/Position/Scale/Rotator)" % PARAMS.get("control_type")}))
    else:
        h = bp.get_hierarchy()
        hc = bp.get_hierarchy_controller()
        pk = _resolve_key(h, pname, unreal.RigElementType.CONTROL)
        if pk is None:
            controls = [str(k.name) for k in (h.get_all_keys(True) or []) if _etype_short(k) == "CONTROL"]
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "parent_control not found: %r" % str(pname), "available_controls": controls[:200]}))
        else:
            cs = unreal.RigControlSettings()
            cs.set_editor_property("control_type", ct)
            cs.set_editor_property("animation_type", unreal.RigControlAnimationType.ANIMATION_CHANNEL)
            dn = PARAMS.get("display_name")
            if dn:
                cs.set_editor_property("display_name", unreal.Name(str(dn)))
            with unreal.ScopedEditorTransaction("MCP add_rig_animation_channel"):
                key = hc.add_animation_channel(unreal.Name(name), pk, cs, False, False)
            real_name = str(key.name)
            _ledger().append({"op": "add_rig_element", "asset_path": path,
                              "key_name": real_name, "key_type": _etype_short(key)})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "requested_name": name, "name": real_name, "type": _etype_short(key),
                "parent_control": str(pk.name),
                "control_type": str(ct).split(".")[-1].split(":")[0].strip(),
                "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_animation_channel(ctx, asset_path: str, name: str, parent_control: str,
                                  control_type: str = "Float", display_name: str = None) -> str:
        """Add an ANIMATION CHANNEL to a Control Rig, nested under an existing control (ledgered, reversible).

        asset_path:     ControlRigBlueprint path.
        name:           suggested channel name (namespace may adjust; real name returned).
        parent_control: name of the existing CONTROL that hosts this channel.
        control_type:   Float | Bool | Integer | Vector2D | Position | Scale | Rotator (default Float).
        display_name:   optional friendly display name.

        An animation channel is a lightweight animatable value (no gizmo) attached to a control -- e.g. a
        'FKIK blend' float on a hand control. Added via RigHierarchyController.add_animation_channel with an
        FRigControlSettings whose animation_type is ANIMATION_CHANNEL. Set its value with
        set_rig_control_value. The asset is saved. Ledgered op 'add_rig_element'; inverse removes it."""
        params = {"asset_path": asset_path, "name": name, "parent_control": parent_control,
                  "control_type": control_type, "display_name": display_name}
        try:
            return json.dumps(_exec(_ADD_CHANNEL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_socket — add a SOCKET element                               #
    # ------------------------------------------------------------------ #
    _ADD_SOCKET_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a name"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    pk = _parent_key(PARAMS.get("parent_name"), PARAMS.get("parent_type"))
    if PARAMS.get("parent_name") and not h.contains(pk):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "parent not found: %s (%s)" % (PARAMS.get("parent_name"), PARAMS.get("parent_type"))}))
    else:
        in_global = bool(PARAMS.get("in_global", True))
        xf = _mk_xf(PARAMS.get("location"), PARAMS.get("rotation"), PARAMS.get("scale"))
        c = PARAMS.get("color") or [1.0, 1.0, 1.0, 1.0]
        col = [float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) > 3 else 1.0]
        desc = str(PARAMS.get("description") or "")
        with unreal.ScopedEditorTransaction("MCP add_rig_socket"):
            key = hc.add_socket(unreal.Name(name), pk, xf, in_global, col, desc, False, False)
        real_name = str(key.name)
        _ledger().append({"op": "add_rig_element", "asset_path": path,
                          "key_name": real_name, "key_type": _etype_short(key)})
        _save_cr(path)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "requested_name": name, "name": real_name, "type": _etype_short(key),
            "parent": (str(pk.name) if PARAMS.get("parent_name") else None),
            "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_socket(ctx, asset_path: str, name: str, parent_name: str = None,
                       parent_type: str = "bone", location: list = None, rotation: list = None,
                       scale: list = None, color: list = None, description: str = None,
                       in_global: bool = True) -> str:
        """Add a SOCKET element to a Control Rig's hierarchy (ledgered, reversible write).

        asset_path:  ControlRigBlueprint path.
        name:        suggested socket name (namespace may adjust; real name returned).
        parent_name/parent_type: parent element (parent_type default 'bone'; sockets usually parent to a bone).
        location/rotation/scale: socket transform ([x,y,z]/[pitch,yaw,roll]/[x,y,z]); default identity.
        color:       socket display color [r,g,b] or [r,g,b,a] (0..1); default white.
        description: optional socket description string.
        in_global:   True (default) if the transform is global; False for parent-local.

        Sockets are named attachment points (e.g. weapon/prop mounts) added via
        RigHierarchyController.add_socket. The asset is saved. Ledgered op 'add_rig_element'; inverse removes it."""
        params = {"asset_path": asset_path, "name": name, "parent_name": parent_name,
                  "parent_type": parent_type, "location": location, "rotation": rotation,
                  "scale": scale, "color": color, "description": description, "in_global": in_global}
        try:
            return json.dumps(_exec(_ADD_SOCKET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_curve — add a CURVE element                                 #
    # ------------------------------------------------------------------ #
    _ADD_CURVE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a name"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    val = float(PARAMS.get("value") if PARAMS.get("value") is not None else 0.0)
    with unreal.ScopedEditorTransaction("MCP add_rig_curve"):
        key = hc.add_curve(unreal.Name(name), val, False, False)
        _try(lambda: h.set_curve_value(key, val, False))
    real_name = str(key.name)
    _ledger().append({"op": "add_rig_element", "asset_path": path,
                      "key_name": real_name, "key_type": _etype_short(key)})
    _save_cr(path)
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "requested_name": name, "name": real_name, "type": _etype_short(key),
        "value": _try(lambda: h.get_curve_value(key)),
        "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_curve(ctx, asset_path: str, name: str, value: float = 0.0) -> str:
        """Add a CURVE element (named float channel) to a Control Rig's hierarchy (ledgered, reversible).

        asset_path: ControlRigBlueprint path.
        name:       suggested curve name (namespace may adjust; real name returned).
        value:      initial curve value (default 0.0).

        Rig curves are named scalar inputs (e.g. morph-target / blendshape drivers, or values imported from a
        skeleton's curves) added via RigHierarchyController.add_curve. The asset is saved.
        Ledgered op 'add_rig_element'; inverse removes it."""
        params = {"asset_path": asset_path, "name": name, "value": value}
        try:
            return json.dumps(_exec(_ADD_CURVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_element_parent — reparent an element (ledgered)             #
    # ------------------------------------------------------------------ #
    _SET_PARENT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
etype = _etype(PARAMS.get("element_type"))
maintain = bool(PARAMS.get("maintain_global", True))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif etype is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "provide element_type: bone|null|control|socket|curve|..."}))
elif not PARAMS.get("parent_name"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide parent_name (target parent element)"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, etype)
    new_pk = _parent_key(PARAMS.get("parent_name"), PARAMS.get("parent_type"))
    if key is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s named %r in %s" % (PARAMS.get("element_type"), str(name), path)}))
    elif not h.contains(new_pk):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "parent not found: %s (%s)" % (PARAMS.get("parent_name"), PARAMS.get("parent_type"))}))
    else:
        prior_par = _try(lambda: h.get_first_parent(key))
        has_prior = prior_par is not None and str(prior_par.type).split(".")[-1].split(":")[0].strip() not in ("NONE", "")
        with unreal.ScopedEditorTransaction("MCP set_rig_element_parent"):
            ok = hc.set_parent(key, new_pk, maintain, False, False)
        _ledger().append({"op": "set_rig_element_parent", "asset_path": path,
                          "child_name": str(key.name), "child_type": _etype_short(key).lower(),
                          "prior_parent_name": (str(prior_par.name) if has_prior else None),
                          "prior_parent_type": (str(prior_par.type).split(".")[-1].split(":")[0].strip().lower() if has_prior else None),
                          "maintain_global": maintain})
        _save_cr(path)
        now_par = _try(lambda: h.get_first_parent(key))
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "child": str(key.name), "set_ok": bool(ok), "maintain_global": maintain,
            "prior_parent": (str(prior_par.name) if has_prior else None),
            "new_parent": (str(now_par.name) if now_par is not None else None),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_element_parent(ctx, asset_path: str, name: str, element_type: str,
                               parent_name: str, parent_type: str = "bone",
                               maintain_global: bool = True) -> str:
        """Reparent an element under a new parent in a Control Rig hierarchy (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        name:         element to reparent.
        element_type: bone | null | control | socket | curve | connector (required to resolve the child).
        parent_name/parent_type: the NEW parent element (parent_type default 'bone').
        maintain_global: True (default) keeps the child's world transform by adjusting its local transform;
                      False keeps the local transform (world pose moves).

        Uses RigHierarchyController.set_parent. The prior first-parent is captured so the inverse reparents
        back (or removes all parents if the element was previously a root). The asset is saved.
        Ledgered op 'set_rig_element_parent' {asset_path,child_name,child_type,prior_parent_name,prior_parent_type,maintain_global}."""
        params = {"asset_path": asset_path, "name": name, "element_type": element_type,
                  "parent_name": parent_name, "parent_type": parent_type, "maintain_global": maintain_global}
        try:
            return json.dumps(_exec(_SET_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_rig_element — rename an element (ledgered)                   #
    # ------------------------------------------------------------------ #
    _RENAME_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
name = PARAMS.get("name")
new_name = PARAMS.get("new_name")
etype = _etype(PARAMS.get("element_type"))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif etype is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "provide element_type: bone|null|control|socket|curve|..."}))
elif not new_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide new_name"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    key = _resolve_key(h, name, etype)
    if key is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s named %r in %s" % (PARAMS.get("element_type"), str(name), path)}))
    else:
        old_name = str(key.name)
        with unreal.ScopedEditorTransaction("MCP rename_rig_element"):
            nk = hc.rename_element(key, unreal.Name(str(new_name)), False, False, True)
        real_new = str(nk.name)
        _ledger().append({"op": "rename_rig_element", "asset_path": path,
                          "element_type": _etype_short(nk).lower(),
                          "old_name": old_name, "new_name": real_new})
        _save_cr(path)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
            "type": _etype_short(nk), "old_name": old_name, "requested_name": str(new_name),
            "new_name": real_new, "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_rig_element(ctx, asset_path: str, name: str, element_type: str, new_name: str) -> str:
        """Rename an element in a Control Rig hierarchy (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        name:         current element name.
        element_type: bone | null | control | socket | curve | connector (required to resolve the element).
        new_name:     desired new name (the namespace may adjust it on collision; the real name is returned).

        Uses RigHierarchyController.rename_element (child links are preserved). The old and (real) new names
        are captured so the inverse renames back. The asset is saved.
        Ledgered op 'rename_rig_element' {asset_path,element_type,old_name,new_name}."""
        params = {"asset_path": asset_path, "name": name, "element_type": element_type, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # build_rig_hierarchy — batch-build elements (ledgered)              #
    # ------------------------------------------------------------------ #
    _BUILD_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
elements = PARAMS.get("elements") or []
clear_existing = bool(PARAMS.get("clear_existing", False))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(elements, list) or not elements:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide elements: a non-empty list of element specs"}))
else:
    h = bp.get_hierarchy()
    hc = bp.get_hierarchy_controller()
    created = []
    errors = []
    prior_txt = None
    cleared = False
    with unreal.ScopedEditorTransaction("MCP build_rig_hierarchy"):
        if clear_existing and h.num() > 0:
            allk = list(h.get_all_keys(True))
            prior_txt = hc.export_to_text(allk)
            for k in reversed(allk):
                _try(lambda k=k: hc.remove_element(k, False, False))
            cleared = True
        for i, spec in enumerate(elements):
            et = str(spec.get("type", "")).strip().lower()
            nm = spec.get("name")
            if not nm:
                errors.append({"index": i, "error": "missing name"}); continue
            pk = _parent_key(spec.get("parent"), spec.get("parent_type"))
            if spec.get("parent") and not h.contains(pk):
                errors.append({"index": i, "name": nm, "error": "parent not found: %s" % spec.get("parent")}); continue
            xf = _mk_xf(spec.get("location"), spec.get("rotation"), spec.get("scale"))
            ing = bool(spec.get("in_global", False))
            key = None
            try:
                if et == "bone":
                    key = hc.add_bone(unreal.Name(str(nm)), pk, xf, ing, unreal.RigBoneType.USER, False, False)
                elif et == "null":
                    key = hc.add_null(unreal.Name(str(nm)), pk, xf, ing, False, False)
                elif et == "socket":
                    c = spec.get("color") or [1.0, 1.0, 1.0, 1.0]
                    col = [float(c[0]), float(c[1]), float(c[2]), float(c[3]) if len(c) > 3 else 1.0]
                    key = hc.add_socket(unreal.Name(str(nm)), pk, xf, bool(spec.get("in_global", True)), col, str(spec.get("description") or ""), False, False)
                elif et == "curve":
                    key = hc.add_curve(unreal.Name(str(nm)), float(spec.get("value") or 0.0), False, False)
                elif et == "control":
                    ct = _ctype(spec.get("control_type") or "float")
                    if ct is None:
                        errors.append({"index": i, "name": nm, "error": "bad control_type: %s" % spec.get("control_type")}); continue
                    cs = unreal.RigControlSettings()
                    cs.set_editor_property("control_type", ct)
                    at = _atype(spec.get("animation_type"))
                    if at is not None:
                        cs.set_editor_property("animation_type", at)
                    val = _make_ctrl_value(h, ct, spec.get("value"))
                    key = hc.add_control(unreal.Name(str(nm)), pk, cs, val, False, False)
                else:
                    errors.append({"index": i, "name": nm, "error": "unknown type: %s (bone/null/control/socket/curve)" % et}); continue
            except Exception as _e:
                errors.append({"index": i, "name": nm, "error": str(_e)[:160]}); continue
            if key is not None:
                created.append({"name": str(key.name), "type": _etype_short(key), "requested": str(nm)})
    if cleared:
        _ledger().append({"op": "restore_rig_hierarchy", "asset_path": path, "prior_hierarchy_txt": prior_txt})
    for c in created:
        _ledger().append({"op": "add_rig_element", "asset_path": path,
                          "key_name": c["name"], "key_type": c["type"]})
    _save_cr(path)
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
        "cleared_existing": cleared, "created_count": len(created), "created": created,
        "errors": errors, "element_count": h.num(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def build_rig_hierarchy(ctx, asset_path: str, elements: list, clear_existing: bool = False) -> str:
        """Batch-build multiple elements into a Control Rig hierarchy in one call (ledgered, reversible).

        asset_path:     ControlRigBlueprint path.
        elements:       ordered list of element spec dicts. Each: {type, name, ...}. Parents must already exist
                        or be added earlier in the same list. Per-type keys:
                          type='bone'|'null': parent, parent_type, location, rotation, scale, in_global
                          type='socket':     parent, parent_type, location, rotation, scale, in_global, color, description
                          type='curve':      value
                          type='control':    parent, parent_type, control_type, value, animation_type
        clear_existing: False (default) appends to the current hierarchy; True first removes every existing
                        element (its full state is serialized first for a faithful restore).

        Elements are added in list order via the RigHierarchyController. Each created element ledgers its own
        'add_rig_element' inverse (removed LIFO on undo); when clear_existing removed prior elements, a single
        'restore_rig_hierarchy' op is ledgered first (undone last) to re-import them. Per-element failures are
        collected in 'errors' without aborting the batch. The asset is saved."""
        params = {"asset_path": asset_path, "elements": elements, "clear_existing": clear_existing}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The op schemas
    # (add_rig_element / remove_rig_element / set_rig_control_settings / set_rig_element_transform /
    #  set_rig_control_value / set_rig_control_offset / set_rig_control_shape / set_rig_element_parent /
    #  rename_rig_element / restore_rig_hierarchy) are reported to the coordinator to fold their inverses
    # into editor_level.undo.
