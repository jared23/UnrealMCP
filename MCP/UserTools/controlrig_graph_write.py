"""UserTools :: Control Rig RigVM GRAPH — NODE authoring (WRITE)  (spec: docs/spec/controlrig.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). The reversible WRITE
counterpart to controlrig.py (READ) for the RigVM solve GRAPH. Where controlrig_write.py authors
the rig HIERARCHY (URigHierarchyController), THIS module authors the RigVM graph's NODES, pin
defaults, and LINKS via the graph controller:
    bp.get_controller()                 -> URigVMController on the DEFAULT model (RigVMModel)
    bp.get_controller_by_name(name)     -> URigVMController on a named model (e.g. 'FootTrace')
All authoring is fully PUBLIC Python -- NOTHING here is faked or C++-gated (verified live vs
TestMCPSetup, UE 5.8.1). Query convention, base64 PARAMS injection, Output-Log auto-capture and the
per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py /
controlrig_write.py / material_graph_write.py.

CRASH-SAFETY: the scratch fixture MUST be created via the ControlRigBlueprintFactory
(AssetToolsHelpers.get_asset_tools().create_asset). NEVER duplicate_asset a ControlRigBlueprint --
its PostDuplicate hard-crashes the engine (EXCEPTION_ACCESS_VIOLATION in
UControlRigBlueprint::PostDuplicate). Delete is safe (not PostDuplicate).

SCOPE: this SINGLE module owns the full reversible graph surface -- NODE lifecycle, PIN DEFAULTS, and
LINK/connection authoring (add_link / break_link) + comment nodes.

WHY REVERSIBLE (URigVMController surface, all callable + verified live):
  * add_unit_node_from_struct_path(struct_path, method, position, node_name) -> RigVMUnitNode;
    add_comment_node(text, pos, size, color, name) -> RigVMCommentNode. Nodes we add are keyed by their
    stable node name (like material_graph_write.py keyed expressions by get_name()). Inverse of any
    add = remove_node_by_name.
  * set_pin_default_value(pin_path, value_str, resize_arrays) / get_pin_default_value(pin_path):
    the prior default string is captured, so the inverse re-applies it faithfully.
  * set_node_position_by_name(node_name, Vector2D) with the node's prior get_position() captured.
  * add_link(output_pin, input_pin) / break_link(output_pin, input_pin): an input pin's source links
    are fully readable (RigVMPin.get_linked_source_pins), so every add/break has a FAITHFUL inverse --
    add captures the input pin's PRIOR sources (restore = break ours + re-add priors); break re-adds
    the exact (output->input) link. Only ledgered when a real change occurred (idempotent no-ops skip).
  * remove_node_by_name(node_name): shipped ONLY for nodes WE created this session AND with NO links
    (removing a linked/pre-existing node loses its type/pins/LINKS, which a single re-add cannot
    faithfully restore). For a self-created isolated node the full re-add spec (struct/method or
    variable spec + position + captured pin defaults) is recorded, so the inverse re-adds an identical
    node. A standalone remove of an ARBITRARY/pre-existing node is REFUSED, not faked.

REVERSIBILITY: every mutation runs inside unreal.ScopedEditorTransaction AND pushes an inverse op
onto the per-session ledger. This module registers NO `undo` tool -- editor_level.py owns the ONE
unified `undo`; the op schemas below are reported to the coordinator to fold in. The asset is saved
after each edit so the authoring persists and the inverse is reliable.

Ledger op schemas (inverse logic self-contained; resolve controller via graph_name):
  - add_rig_vm_node          {asset_path, graph_name, node_name}                -> remove_node_by_name
  - set_rig_vm_pin_default   {asset_path, graph_name, pin_path, prior_value}    -> set_pin_default_value(prior)
  - set_rig_vm_node_position {asset_path, graph_name, node_name, prior_pos[x,y]}-> set_node_position_by_name(prior)
  - remove_rig_vm_node       {asset_path, graph_name, node_kind, node_name, struct_path?, method?,
                              var_spec?, position[x,y], pin_defaults{path:val}} -> re-add node + restore
  - add_rig_vm_link          {asset_path, graph_name, output_pin, input_pin, prior_sources[]}
                              -> break_link(output,input) + re-add each prior_source -> input
  - break_rig_vm_link        {asset_path, graph_name, output_pin, input_pin}    -> add_link(output,input)

DEFERRED / NOT shipped (documented, not faked):
  - Removal of an ARBITRARY/pre-existing or LINKED node (loses type/pins/links; no faithful inverse;
    break the links first, then remove).
  - Template/wildcard/injected/function-reference node authoring beyond unit + variable + comment
    nodes (broad surface; the shipped node kinds cover the reversible core).

Implemented (validated live on a FACTORY-created SCRATCH CR; editor left CLEAN, ledger depth 0):
  - add_rig_vm_unit_node      (WRITE; ledgered "add_rig_vm_node")
  - add_rig_vm_comment_node   (WRITE; ledgered "add_rig_vm_node")
  - set_rig_vm_pin_default_value (WRITE; ledgered "set_rig_vm_pin_default"; faithful prior capture)
  - set_rig_vm_node_position  (WRITE; ledgered "set_rig_vm_node_position"; faithful prior capture)
  - add_rig_vm_link           (WRITE; ledgered "add_rig_vm_link"; prior input-pin sources captured)
  - break_rig_vm_link         (WRITE; ledgered "break_rig_vm_link"; re-add exact link on inverse)
  - remove_rig_vm_node        (WRITE; ledgered "remove_rig_vm_node"; self-created unit/comment, unlinked)
  - add_rig_vm_local_variable (WRITE; ledgered "add_rig_vm_local_variable"; inverse remove_local_variable)   [R11]
  - add_rig_vm_variable_node  (WRITE; ledgered "add_rig_vm_node"; getter/setter for an existing var)          [R11]

R11 -- LOCAL-VARIABLE + VARIABLE-NODE authoring (was R10-DEFERRED, now SHIPPED): the R10 deferral was
that add_variable_node RAISES "variable does not exist" without a pre-declared member/local variable.
R11 ships the enabling declarer alongside it. KEY LIVE FINDING (UE 5.8.1): local variables are
graph-scoped to FUNCTION/COLLAPSE graphs -- URigVMController.add_local_variable on the TOP-level
RigVMModel is a silent no-op (returns an empty descriptor; graph.get_local_variables() stays empty).
On a function graph it works and get_local_variables() reflects it. Function/collapse contained graphs
are NOT returned by bp.get_all_models(), so _get_controller was extended to also resolve them via
bp.get_local_function_library().get_nodes()[i].get_contained_graph(). add_rig_vm_variable_node then
places a getter/setter on that same graph referencing the (member or local) variable.
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
def _get_controller(bp, graph_name):
    # Default model controller, or a named-model controller. Returns (controller, model, err).
    if not graph_name:
        c = _try(lambda: bp.get_controller())
        m = _try(lambda: bp.get_default_model())
        if c is None:
            return None, None, "no default RigVM controller on this asset"
        return c, m, None
    models = _try(lambda: bp.get_all_models(), []) or []
    names = [str(_try(lambda g=g: g.get_graph_name())) for g in models]
    want = str(graph_name).strip().lower()
    tgt = None
    for g, nm in zip(models, names):
        if nm.lower() == want:
            tgt = g; break
    if tgt is None:
        # Function/collapse contained graphs are NOT in get_all_models(); they hold local variables.
        # Search the function library's function-definition contained graphs by name.
        lib = _try(lambda: bp.get_local_function_library())
        for fn in (_try(lambda: lib.get_nodes(), []) or []):
            cg = _try(lambda fn=fn: fn.get_contained_graph())
            if cg is None:
                continue
            nm = str(_try(lambda cg=cg: cg.get_graph_name()))
            names.append(nm)
            if nm.lower() == want:
                tgt = g = cg; break
    if tgt is None:
        return None, None, "no RigVM graph named %r (available: %s)" % (graph_name, names)
    c = _try(lambda: bp.get_controller(tgt))
    if c is None:
        c = _try(lambda: bp.get_controller_by_name(unreal.Name(str(graph_name))))
    if c is None:
        return None, None, "could not obtain controller for graph %r" % graph_name
    return c, tgt, None
def _model_of(ctrl, bp, model):
    # Prefer the passed model; else the controller's own graph.
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
def _node_has_links(node):
    # True if any pin (recursively) has a source or target link.
    def _pin_linked(p):
        srcs = _try(lambda: p.get_linked_source_pins(), []) or []
        tgts = _try(lambda: p.get_linked_target_pins(), []) or []
        if srcs or tgts:
            return True
        for sub in (_try(lambda: p.get_sub_pins(), []) or []):
            if _pin_linked(sub):
                return True
        return False
    for p in (_try(lambda: node.get_pins(), []) or []):
        if _pin_linked(p):
            return True
    return False
def _resolve_struct_path(bp, name_or_path):
    # Return a struct object-path string for a RigVM node struct, resolving a bare name via the
    # asset's authorable node palette. A value already containing '.'/'/' is treated as a path.
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
def _input_pin_defaults(node):
    # Capture {pin_path: default_str} for top-level input/io pins (used to faithfully re-add).
    out = {}
    for p in (_try(lambda: node.get_pins(), []) or []):
        d = _try(lambda p=p: p.get_direction())
        ds = str(d).split(".")[-1].split(":")[0].strip().upper() if d is not None else ""
        if ds in ("OUTPUT",):
            continue
        if bool(_try(lambda p=p: p.is_execute_context(), False)):
            continue
        pp = str(_try(lambda p=p: p.get_pin_path()))
        dv = _try(lambda p=p: p.get_default_value())
        if dv is not None and dv != "":
            out[pp] = str(dv)
    return out
def _local_var_names(model):
    # Names of the graph-scoped local variables on a RigVM graph (function/collapse graphs only).
    lvs = _try(lambda: model.get_local_variables(True))
    if lvs is None:
        lvs = _try(lambda: model.get_local_variables(), []) or []
    out = []
    for d in (lvs or []):
        nm = _try(lambda d=d: d.get_editor_property("name"))
        if nm is None:
            nm = _try(lambda d=d: d.name)
        out.append(str(nm))
    return out
def _resolve_cpp_type_object(cpp_type):
    # Primitive types (float/int32/bool/FString/FName) need no object; a path-like cpp_type
    # (struct/enum/object) is best-effort resolved to its UObject via load_object.
    s = str(cpp_type or "")
    if "/" in s or "." in s:
        return _try(lambda: unreal.load_object(None, s))
    return None
'''

    # ------------------------------------------------------------------ #
    # add_rig_vm_unit_node — add a RigVM unit (struct) node               #
    # ------------------------------------------------------------------ #
    _ADD_UNIT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
struct = PARAMS.get("struct")
method = PARAMS.get("method") or "Execute"
node_name = PARAMS.get("node_name") or ""
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not struct:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide a struct (RigVM node struct name or object path)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        sp = _resolve_struct_path(bp, struct)
        if not sp:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "could not resolve RigVM struct %r (use list_control_rig_node_types for names)" % struct}))
        else:
            model = _model_of(ctrl, bp, model)
            gname = str(_try(lambda: model.get_graph_name()))
            pos = _pos2d(PARAMS.get("position"))
            with unreal.ScopedEditorTransaction("MCP add_rig_vm_unit_node"):
                node = ctrl.add_unit_node_from_struct_path(sp, method, pos, node_name, True, False)
            if node is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "add_unit_node_from_struct_path returned None for %s (method %r)" % (sp, method)}))
            else:
                nn = str(_try(lambda: node.get_node_path()))
                _ledger().append({"op": "add_rig_vm_node", "asset_path": path,
                                  "graph_name": gname, "node_name": nn})
                _save_cr(path)
                pins = [str(_try(lambda p=p: p.get_name())) for p in (_try(lambda: node.get_pins(), []) or [])]
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "node_name": nn, "node_class": type(node).__name__,
                    "script_struct": str(_try(lambda: node.get_script_struct().get_name())),
                    "title": str(_try(lambda: node.get_node_title())),
                    "position": _pos_list(_try(lambda: node.get_position())),
                    "pins": pins, "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_unit_node(ctx, asset_path: str, struct: str, method: str = "Execute",
                             position: list = None, node_name: str = None,
                             graph_name: str = None) -> str:
        """Add a RigVM UNIT (struct) node to a Control Rig's solve graph (ledgered, reversible write).

        asset_path:  ControlRigBlueprint path (e.g. '/Game/.../CR_Mannequin_FootIK.CR_Mannequin_FootIK').
        struct:      the RigVM node struct: a bare name (e.g. 'RigVMFunction_MathFloatMul',
                     'RigUnit_SetBoneTransform') resolved via the asset's node palette, or a full
                     struct object path ('/Script/RigVM.RigVMFunction_MathFloatAdd').
        method:      the RIGVM_METHOD to invoke (default 'Execute'; nearly all RigUnits use 'Execute').
        position:    [x, y] canvas position (default [0,0]).
        node_name:   suggested node name (the graph may adjust it; the real name is returned).
        graph_name:  which model/graph to author in (default = the default model 'RigVMModel'); see
                     get_control_rig_vm_graph's available_graphs.

        Added via URigVMController.add_unit_node_from_struct_path. The asset is saved. Ledgered op
        'add_rig_vm_node' {asset_path,graph_name,node_name}; inverse = remove_node_by_name. Use
        list_control_rig_node_types for valid struct names; set_rig_vm_pin_default_value to seed pins."""
        params = {"asset_path": asset_path, "struct": struct, "method": method,
                  "position": position, "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_UNIT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_vm_pin_default_value — set a pin's default (ledgered)       #
    # ------------------------------------------------------------------ #
    _SET_PIN_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
pin_path = PARAMS.get("pin_path")
value = PARAMS.get("value")
resize = bool(PARAMS.get("resize_arrays", True))
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not pin_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide pin_path (e.g. 'NodeName.PinName')"}))
elif value is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide value (string form)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        pin = _try(lambda: model.find_pin(str(pin_path)))
        if pin is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no pin at path %r in graph %r" % (pin_path, gname)}))
        else:
            prior = str(_try(lambda: ctrl.get_pin_default_value(str(pin_path))))
            with unreal.ScopedEditorTransaction("MCP set_rig_vm_pin_default_value"):
                ok = ctrl.set_pin_default_value(str(pin_path), str(value), resize, True, False, False, True)
            after = str(_try(lambda: ctrl.get_pin_default_value(str(pin_path))))
            _ledger().append({"op": "set_rig_vm_pin_default", "asset_path": path,
                              "graph_name": gname, "pin_path": str(pin_path), "prior_value": prior})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "pin_path": str(pin_path), "set_ok": bool(ok),
                "before": prior, "after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_vm_pin_default_value(ctx, asset_path: str, pin_path: str, value: str,
                                     resize_arrays: bool = True, graph_name: str = None) -> str:
        """Set a RigVM pin's default value on a Control Rig solve graph (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        pin_path:     dotted pin path 'NodeName.PinName' (sub-pins: 'NodeName.Struct.Member'), as
                      shown by get_control_rig_vm_graph(include_pins=True) pin names under the node path.
        value:        the new default value in string form (engine-parsed by pin type: '1.5' for a
                      float, 'true' for a bool, '(X=1.0,Y=2.0,Z=3.0)' for a vector, a name for an enum).
        resize_arrays: for array pins, grow/shrink the array to match the value (default True).
        graph_name:   which model/graph (default = the default model).

        The prior default is captured via get_pin_default_value so the inverse restores it exactly.
        The asset is saved. Ledgered op 'set_rig_vm_pin_default' {asset_path,graph_name,pin_path,prior_value}."""
        params = {"asset_path": asset_path, "pin_path": pin_path, "value": value,
                  "resize_arrays": resize_arrays, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SET_PIN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_rig_vm_node_position — move a node on the canvas (ledgered)     #
    # ------------------------------------------------------------------ #
    _SET_POS_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
node_name = PARAMS.get("node_name")
position = PARAMS.get("position")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not node_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide node_name"}))
elif not position:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide position [x,y]"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        node = _find_node(model, node_name)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no node named %r in graph %r" % (node_name, gname)}))
        else:
            prior = _pos_list(_try(lambda: node.get_position()))
            with unreal.ScopedEditorTransaction("MCP set_rig_vm_node_position"):
                ok = ctrl.set_node_position_by_name(unreal.Name(str(node_name)), _pos2d(position), True, False, False)
            _ledger().append({"op": "set_rig_vm_node_position", "asset_path": path,
                              "graph_name": gname, "node_name": str(node_name), "prior_pos": prior})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "node_name": str(node_name), "set_ok": bool(ok),
                "before": prior, "after": _pos_list(_try(lambda: node.get_position())),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_rig_vm_node_position(ctx, asset_path: str, node_name: str, position: list,
                                 graph_name: str = None) -> str:
        """Set a RigVM node's canvas position on a Control Rig solve graph (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        node_name:  the node's name/path (as returned by add_rig_vm_unit_node or
                    get_control_rig_vm_graph 'path').
        position:   [x, y] new canvas position.
        graph_name: which model/graph (default = the default model).

        The prior position is captured so the inverse restores it exactly. Cosmetic (layout only; does
        not affect solve). The asset is saved. Ledgered op 'set_rig_vm_node_position'."""
        params = {"asset_path": asset_path, "node_name": node_name, "position": position,
                  "graph_name": graph_name}
        try:
            return json.dumps(_exec(_SET_POS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_rig_vm_node — self-created + unlinked only (faithful re-add) #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
node_name = PARAMS.get("node_name")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not node_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide node_name"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        node = _find_node(model, node_name)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no node named %r in graph %r" % (node_name, gname)}))
        else:
            nn = str(_try(lambda: node.get_node_path()))
            # Guard 1: only nodes WE created this session (present in ledger as add_rig_vm_node, net).
            created = set()
            for e in _ledger():
                if e.get("op") == "add_rig_vm_node" and e.get("asset_path") == path and e.get("graph_name") == gname:
                    created.add(e.get("node_name"))
            if nn not in created:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "refusing to remove %r: not created by this session. A standalone remove of a pre-existing node loses its type/pins/links with no faithful inverse -- removal is only reversible for nodes THIS session added." % nn}))
            # Guard 2: no links (removing a linked node drops links -- Agent B's connection domain).
            elif _node_has_links(node):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "refusing to remove %r: it has pin link(s). Removing a linked node cannot faithfully restore its connections via a single re-add. Break its links first (break_rig_vm_link), then remove." % nn}))
            else:
                # Capture the faithful re-add spec.
                kind = "unit" if isinstance(node, unreal.RigVMUnitNode) else ("comment" if isinstance(node, unreal.RigVMCommentNode) else "other")
                cap = {"op": "remove_rig_vm_node", "asset_path": path, "graph_name": gname,
                       "node_kind": kind, "node_name": nn,
                       "position": _pos_list(_try(lambda: node.get_position())),
                       "pin_defaults": _input_pin_defaults(node)}
                if kind == "unit":
                    ss = _try(lambda: node.get_script_struct())
                    cap["struct_path"] = str(_try(lambda: ss.get_path_name())) if ss is not None else None
                    cap["method"] = str(_try(lambda: node.get_method_name()) or "Execute")
                elif kind == "comment":
                    _col = _try(lambda: node.get_node_color())
                    cap["comment"] = {
                        "text": str(_try(lambda: node.get_comment_text()) or ""),
                        "size": _pos_list(_try(lambda: node.get_size())),
                        "color": ([round(float(_col.r), 4), round(float(_col.g), 4), round(float(_col.b), 4), round(float(_col.a), 4)] if _col is not None else [0.0, 0.0, 0.0, 1.0])}
                if kind == "other":
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "refusing to remove %r: node kind %s is not a unit or comment node this module can faithfully re-add." % (nn, type(node).__name__)}))
                else:
                    with unreal.ScopedEditorTransaction("MCP remove_rig_vm_node"):
                        ok = ctrl.remove_node_by_name(unreal.Name(nn), True, False)
                    if not ok:
                        print("@@UMCP@@" + json.dumps({"status": "error", "message": "remove_node_by_name returned False for %r" % nn}))
                    else:
                        _ledger().append(cap)
                        _save_cr(path)
                        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                            "graph_name": gname, "removed": nn, "node_kind": kind, "reconstructable": True,
                            "captured_pins": list(cap["pin_defaults"].keys()),
                            "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_rig_vm_node(ctx, asset_path: str, node_name: str, graph_name: str = None) -> str:
        """Remove a RigVM node from a Control Rig solve graph (ledgered, reversible write).
        SELF-CREATED + UNLINKED nodes only.

        asset_path: ControlRigBlueprint path.
        node_name:  the node's name/path.
        graph_name: which model/graph (default = the default model).

        REFUSES unless the node was added by THIS session (a standalone remove of a pre-existing node
        loses its type/pins/links with no faithful inverse) AND has no pin links (removing a linked
        node cannot restore its connections via a single re-add; break
        them first with break_rig_vm_link). For an eligible node the full re-add spec (struct/method
        or variable spec + position + input-pin defaults) is captured, so the inverse re-adds an
        identical node. The asset is saved. Ledgered op 'remove_rig_vm_node'."""
        params = {"asset_path": asset_path, "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_vm_comment_node — add an annotation comment box (ledgered)  #
    # ------------------------------------------------------------------ #
    _ADD_COMMENT_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
text = str(PARAMS.get("comment_text") or "")
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
        pos = _pos2d(PARAMS.get("position"))
        size = PARAMS.get("size") or [400.0, 300.0]
        sz = unreal.Vector2D(float(size[0]), float(size[1]))
        col = PARAMS.get("color") or [0.0, 0.0, 0.0, 1.0]
        alpha = float(col[3]) if len(col) > 3 else 1.0
        lc = unreal.LinearColor(float(col[0]), float(col[1]), float(col[2]), alpha)
        node_name = PARAMS.get("node_name") or ""
        with unreal.ScopedEditorTransaction("MCP add_rig_vm_comment_node"):
            node = ctrl.add_comment_node(text, pos, sz, lc, node_name, True, False)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_comment_node returned None"}))
        else:
            nn = str(_try(lambda: node.get_node_path()))
            _ledger().append({"op": "add_rig_vm_node", "asset_path": path,
                              "graph_name": gname, "node_name": nn})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "node_name": nn, "node_class": type(node).__name__,
                "comment_text": text, "title": str(_try(lambda: node.get_node_title())),
                "position": _pos_list(_try(lambda: node.get_position())),
                "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_comment_node(ctx, asset_path: str, comment_text: str, position: list = None,
                                size: list = None, color: list = None, node_name: str = None,
                                graph_name: str = None) -> str:
        """Add a COMMENT (annotation) node to a Control Rig's solve graph (ledgered, reversible write).

        asset_path:   ControlRigBlueprint path.
        comment_text: the annotation text shown in the comment box.
        position:     [x, y] canvas position of the box's corner (default [0,0]).
        size:         [width, height] of the box (default [400, 300]).
        color:        [r, g, b] or [r, g, b, a] LinearColor 0..1 (default black, alpha 1).
        node_name:    suggested node name (graph may adjust; real name returned).
        graph_name:   which model/graph (default = the default model).

        Comment nodes are purely cosmetic (annotation; no solve effect). Added via
        URigVMController.add_comment_node. The asset is saved. Ledgered op 'add_rig_vm_node'
        {asset_path,graph_name,node_name}; inverse = remove_node_by_name (same as node adds)."""
        params = {"asset_path": asset_path, "comment_text": comment_text, "position": position,
                  "size": size, "color": color, "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_COMMENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_vm_link — wire output pin -> input pin (ledgered)           #
    # ------------------------------------------------------------------ #
    _ADD_LINK_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
out_pin = PARAMS.get("output_pin")
in_pin = PARAMS.get("input_pin")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not out_pin or not in_pin:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide output_pin and input_pin (e.g. 'NodeA.Result','NodeB.A')"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        op_pin = _try(lambda: model.find_pin(str(out_pin)))
        ip_pin = _try(lambda: model.find_pin(str(in_pin)))
        if op_pin is None or ip_pin is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "pin not found: output=%s(%s) input=%s(%s)" % (out_pin, op_pin is not None, in_pin, ip_pin is not None)}))
        else:
            prior_sources = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda: ip_pin.get_linked_source_pins(), []) or [])]
            if str(out_pin) in prior_sources:
                print("@@UMCP@@" + json.dumps({"status": "noop", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "output_pin": str(out_pin), "input_pin": str(in_pin),
                    "message": "link already exists; nothing changed (not ledgered)"}))
            else:
                with unreal.ScopedEditorTransaction("MCP add_rig_vm_link"):
                    ok = ctrl.add_link(str(out_pin), str(in_pin), True, False)
                ip2 = _try(lambda: model.find_pin(str(in_pin)))
                after_sources = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda: ip2.get_linked_source_pins(), []) or [])]
                if not ok or str(out_pin) not in after_sources:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "add_link did not create the link (ok=%s); check pin directions/type compatibility" % bool(ok),
                        "after_sources": after_sources}))
                else:
                    _ledger().append({"op": "add_rig_vm_link", "asset_path": path, "graph_name": gname,
                        "output_pin": str(out_pin), "input_pin": str(in_pin), "prior_sources": prior_sources})
                    _save_cr(path)
                    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                        "graph_name": gname, "output_pin": str(out_pin), "input_pin": str(in_pin),
                        "linked": bool(ok), "prior_sources": prior_sources, "after_sources": after_sources,
                        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_link(ctx, asset_path: str, output_pin: str, input_pin: str,
                        graph_name: str = None) -> str:
        """Wire a RigVM output pin into an input pin on a Control Rig solve graph (ledgered, reversible).

        asset_path: ControlRigBlueprint path.
        output_pin: source pin path 'NodeName.PinName' (the pin whose value flows out, e.g. 'MulA.Result').
        input_pin:  destination pin path 'NodeName.PinName' (the receiving input, e.g. 'MulB.A'). Sub-pins
                    are addressable ('Node.Struct.Member'). See get_control_rig_vm_graph(include_pins=True).
        graph_name: which model/graph (default = the default model).

        Uses URigVMController.add_link. If the exact link already exists this is a no-op (not ledgered).
        The destination input pin's PRIOR source link(s) are captured for a faithful inverse. The asset
        is saved. Ledgered op 'add_rig_vm_link' {asset_path,graph_name,output_pin,input_pin,prior_sources}.
        Inverse: break_link(output,input) then re-add each prior_source -> input_pin."""
        params = {"asset_path": asset_path, "output_pin": output_pin, "input_pin": input_pin,
                  "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_LINK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # break_rig_vm_link — remove an existing link (ledgered)              #
    # ------------------------------------------------------------------ #
    _BREAK_LINK_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
out_pin = PARAMS.get("output_pin")
in_pin = PARAMS.get("input_pin")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not out_pin or not in_pin:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide output_pin and input_pin"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        ip_pin = _try(lambda: model.find_pin(str(in_pin)))
        if ip_pin is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "input pin not found: %s" % in_pin}))
        else:
            sources = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda: ip_pin.get_linked_source_pins(), []) or [])]
            if str(out_pin) not in sources:
                print("@@UMCP@@" + json.dumps({"status": "noop", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "output_pin": str(out_pin), "input_pin": str(in_pin),
                    "current_sources": sources,
                    "message": "no such link (%s -> %s); nothing changed (not ledgered)" % (out_pin, in_pin)}))
            else:
                with unreal.ScopedEditorTransaction("MCP break_rig_vm_link"):
                    ok = ctrl.break_link(str(out_pin), str(in_pin), True, False)
                ip2 = _try(lambda: model.find_pin(str(in_pin)))
                after_sources = [str(_try(lambda sp=sp: sp.get_pin_path())) for sp in (_try(lambda: ip2.get_linked_source_pins(), []) or [])]
                _ledger().append({"op": "break_rig_vm_link", "asset_path": path, "graph_name": gname,
                    "output_pin": str(out_pin), "input_pin": str(in_pin)})
                _save_cr(path)
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "output_pin": str(out_pin), "input_pin": str(in_pin),
                    "broken": bool(ok), "after_sources": after_sources, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def break_rig_vm_link(ctx, asset_path: str, output_pin: str, input_pin: str,
                          graph_name: str = None) -> str:
        """Remove a link between two RigVM pins on a Control Rig solve graph (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        output_pin: source pin path 'NodeName.PinName' of the existing link.
        input_pin:  destination pin path 'NodeName.PinName' of the existing link.
        graph_name: which model/graph (default = the default model).

        Uses URigVMController.break_link. If the link does not currently exist this is a no-op (not
        ledgered). The asset is saved. Ledgered op 'break_rig_vm_link'
        {asset_path,graph_name,output_pin,input_pin}. Inverse: add_link(output,input) restores the link."""
        params = {"asset_path": asset_path, "output_pin": output_pin, "input_pin": input_pin,
                  "graph_name": graph_name}
        try:
            return json.dumps(_exec(_BREAK_LINK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_vm_local_variable — declare a graph-scoped local variable   #
    # ------------------------------------------------------------------ #
    _ADD_LOCALVAR_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
var_name = PARAMS.get("var_name")
cpp_type = PARAMS.get("cpp_type")
default_value = PARAMS.get("default_value")
if default_value is None:
    default_value = ""
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not var_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide var_name"}))
elif not cpp_type:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide cpp_type (e.g. 'float','int32','bool','FString','FName', or a struct/enum/object path)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    elif not hasattr(ctrl, "add_local_variable"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "URigVMController.add_local_variable not available in this engine build"}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        cto = _resolve_cpp_type_object(cpp_type)
        before = _local_var_names(model)
        desc = None
        addhint = ""
        try:
            with unreal.ScopedEditorTransaction("MCP add_rig_vm_local_variable"):
                desc = ctrl.add_local_variable(unreal.Name(str(var_name)), str(cpp_type), cto, str(default_value), True, False)
        except Exception as _e:
            addhint = str(_e)
        after = _local_var_names(model)
        got_name = str(_try(lambda: desc.get_editor_property("name"))) if desc is not None else ""
        if str(var_name) not in after or got_name != str(var_name):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "add_local_variable was a no-op on graph %r (empty descriptor). Local variables are graph-scoped to FUNCTION/COLLAPSE graphs -- the top-level RigVMModel does not hold local variables. Pass graph_name of a function graph." % gname,
                "hint": addhint, "local_vars": after}))
        else:
            _ledger().append({"op": "add_rig_vm_local_variable", "asset_path": path,
                              "graph_name": gname, "var_name": str(var_name)})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "var_name": str(var_name), "cpp_type": str(cpp_type),
                "default_value": str(default_value),
                "descriptor_cpp_type": str(_try(lambda: desc.get_editor_property("cpp_type"))),
                "descriptor_default": str(_try(lambda: desc.get_editor_property("default_value"))),
                "local_vars_before": before, "local_vars_after": after,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_local_variable(ctx, asset_path: str, var_name: str, cpp_type: str,
                                  default_value: str = "", graph_name: str = None) -> str:
        """Declare a graph-scoped LOCAL VARIABLE on a Control Rig RigVM graph (ledgered, reversible write).

        asset_path:    ControlRigBlueprint path.
        var_name:      the new local variable's name.
        cpp_type:      the variable's C++ type string: a primitive ('float','int32','bool','FString',
                       'FName') needs no type object; a struct/enum/object type is given as an object
                       path (e.g. '/Script/CoreUObject.Vector') and best-effort resolved.
        default_value: the initial value in string form (default '').
        graph_name:    which graph to declare the variable on. Local variables are graph-scoped and are
                       ONLY held by FUNCTION/COLLAPSE graphs -- the top-level RigVMModel (graph_name None)
                       does NOT hold local variables and add is a no-op there (returns an error telling
                       you to pass a function graph). Pass the function graph's name.

        Added via URigVMController.add_local_variable. Presence is verified via graph.get_local_variables
        before ledgering (an empty/no-op descriptor is reported as an error, not ledgered). The asset is
        saved. Ledgered op 'add_rig_vm_local_variable' {asset_path,graph_name,var_name}; inverse =
        remove_local_variable(var_name). Pair with add_rig_vm_variable_node to place getter/setter nodes."""
        params = {"asset_path": asset_path, "var_name": var_name, "cpp_type": cpp_type,
                  "default_value": default_value, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_ADD_LOCALVAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_rig_vm_variable_node — getter/setter for an EXISTING variable   #
    # ------------------------------------------------------------------ #
    _ADD_VARNODE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
var_name = PARAMS.get("var_name")
cpp_type = PARAMS.get("cpp_type")
is_getter = bool(PARAMS.get("is_getter", True))
node_name = PARAMS.get("node_name") or ""
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not var_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide var_name (an EXISTING member or local variable)"}))
elif not cpp_type:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide cpp_type matching the variable's type (e.g. 'float')"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    elif not hasattr(ctrl, "add_variable_node"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "URigVMController.add_variable_node not available in this engine build"}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        cto = _resolve_cpp_type_object(cpp_type)
        pos = _pos2d(PARAMS.get("position"))
        node = None
        addhint = ""
        try:
            with unreal.ScopedEditorTransaction("MCP add_rig_vm_variable_node"):
                node = ctrl.add_variable_node(unreal.Name(str(var_name)), str(cpp_type), cto, is_getter, "", pos, str(node_name), True, False)
        except Exception as _e:
            addhint = str(_e)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "add_variable_node returned None for %r on graph %r -- the variable must ALREADY exist as a member or local variable on that graph (declare a local var first with add_rig_vm_local_variable)." % (var_name, gname),
                "hint": addhint}))
        else:
            nn = str(_try(lambda: node.get_node_path()))
            _ledger().append({"op": "add_rig_vm_node", "asset_path": path,
                              "graph_name": gname, "node_name": nn})
            _save_cr(path)
            pins = [str(_try(lambda p=p: p.get_name())) for p in (_try(lambda: node.get_pins(), []) or [])]
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "node_name": nn, "node_class": type(node).__name__,
                "var_name": str(var_name), "cpp_type": str(cpp_type),
                "is_getter": bool(_try(lambda: node.is_getter(), is_getter)),
                "title": str(_try(lambda: node.get_node_title())),
                "position": _pos_list(_try(lambda: node.get_position())),
                "pins": pins, "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_rig_vm_variable_node(ctx, asset_path: str, var_name: str, cpp_type: str,
                                 is_getter: bool = True, position: list = None,
                                 graph_name: str = None, node_name: str = None) -> str:
        """Add a variable GETTER/SETTER node referencing an EXISTING variable on a Control Rig RigVM
        graph (ledgered, reversible write).

        asset_path: ControlRigBlueprint path.
        var_name:   the variable to reference. It MUST already exist as a member variable or a graph
                    local variable on the target graph (declare a local first with
                    add_rig_vm_local_variable) -- otherwise the engine raises 'variable does not exist'
                    and this returns an error.
        cpp_type:   the variable's C++ type string (must match the declaration, e.g. 'float'); a
                    struct/enum/object type is given as an object path and best-effort resolved.
        is_getter:  True adds a GETTER (reads the variable, value flows out); False adds a SETTER
                    (writes the variable). Default True.
        position:   [x, y] canvas position (default [0,0]).
        graph_name: which graph to author in (default = the default model). For a LOCAL variable this
                    must be the same function/collapse graph the variable is declared on.
        node_name:  suggested node name (graph may adjust; real name returned).

        Added via URigVMController.add_variable_node. The asset is saved. Ledgered as op 'add_rig_vm_node'
        {asset_path,graph_name,node_name} (same schema as unit/comment adds; inverse = remove_node_by_name)."""
        params = {"asset_path": asset_path, "var_name": var_name, "cpp_type": cpp_type,
                  "is_getter": is_getter, "position": position, "graph_name": graph_name,
                  "node_name": node_name}
        try:
            return json.dumps(_exec(_ADD_VARNODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # collapse_rig_vm_nodes — group nodes into a collapse (library) node  #
    # ------------------------------------------------------------------ #
    _COLLAPSE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
node_names = PARAMS.get("node_names") or []
collapse_name = str(PARAMS.get("collapse_name") or "Collapse")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not node_names:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide node_names (a list of node names/paths to collapse)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        keys = [unreal.Name(str(n)) for n in node_names]
        snap = _try(lambda: ctrl.export_nodes_to_text(keys, True)) or ""
        coll = None
        with unreal.ScopedEditorTransaction("MCP collapse_rig_vm_nodes"):
            coll = _try(lambda: ctrl.collapse_nodes(keys, collapse_name, True, False, False))
        if coll is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "collapse_nodes returned None (unknown node names, or nodes not collapsible together)"}))
        else:
            cn = str(_try(lambda: coll.get_node_path()))
            _ledger().append({"op": "collapse_rig_vm_nodes", "asset_path": path, "graph_name": gname,
                              "collapse_node_name": cn, "snapshot": snap})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "collapse_node_name": cn, "collapsed": [str(n) for n in node_names],
                "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                "snapshot_chars": len(snap), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def collapse_rig_vm_nodes(ctx, asset_path: str, node_names: list, collapse_name: str = "Collapse",
                              graph_name: str = None) -> str:
        """Collapse several RigVM nodes into a single COLLAPSE (library) node (ledgered, FAITHFUL undo).

        asset_path:    ControlRigBlueprint path.
        node_names:    list of node names/paths (on the same graph) to group.
        collapse_name: desired name for the new collapse node (graph may adjust; real name returned).
        graph_name:    which graph to author in (default = the default model).

        Snapshots the exact source subgraph (export_nodes_to_text incl. exterior links) BEFORE collapsing,
        so the inverse losslessly restores every node+pin+link. Ledgers op 'collapse_rig_vm_nodes'
        {asset_path, graph_name, collapse_node_name, snapshot}; inverse = remove the collapse node +
        import_nodes_from_text(snapshot)."""
        params = {"asset_path": asset_path, "node_names": node_names,
                  "collapse_name": collapse_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_COLLAPSE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # expand_rig_vm_node — expand a collapse/function node back to nodes  #
    # ------------------------------------------------------------------ #
    _EXPAND_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
node_name = str(PARAMS.get("node_name") or "")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not node_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide node_name (the collapse/function-ref node to expand)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        snap = _try(lambda: ctrl.export_nodes_to_text([unreal.Name(node_name)], True)) or ""
        exp = None
        with unreal.ScopedEditorTransaction("MCP expand_rig_vm_node"):
            exp = _try(lambda: ctrl.expand_library_node(unreal.Name(node_name), True, False))
        if exp is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "expand_library_node returned None (node %r is not a collapse/function-reference node?)" % node_name}))
        else:
            expanded = [str(_try(lambda n=n: n.get_node_path())) for n in (exp or [])]
            _ledger().append({"op": "expand_rig_vm_node", "asset_path": path, "graph_name": gname,
                              "snapshot": snap, "expanded_names": expanded})
            _save_cr(path)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                "graph_name": gname, "expanded_node": node_name, "expanded_names": expanded,
                "node_count": len(_try(lambda: model.get_nodes(), []) or []),
                "snapshot_chars": len(snap), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def expand_rig_vm_node(ctx, asset_path: str, node_name: str, graph_name: str = None) -> str:
        """Expand a COLLAPSE or FUNCTION-REFERENCE node back into its constituent nodes (ledgered, FAITHFUL undo).

        asset_path: ControlRigBlueprint path.
        node_name:  the collapse/function-reference node to expand.
        graph_name: which graph the node lives on (default = the default model).

        Snapshots the library node (export_nodes_to_text incl. exterior links) BEFORE expanding. Ledgers op
        'expand_rig_vm_node' {asset_path, graph_name, snapshot, expanded_names}; inverse = remove the
        expanded child nodes + import_nodes_from_text(snapshot) to restore the single library node."""
        params = {"asset_path": asset_path, "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_EXPAND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # promote_rig_vm_node — collapse<->function-reference promotion       #
    # ------------------------------------------------------------------ #
    _PROMOTE_BODY = _CR_HELPERS + r'''
path = PARAMS.get("asset_path")
node_name = str(PARAMS.get("node_name") or "")
bp, err = _load_cr(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not node_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide node_name (a collapse OR function-reference node)"}))
else:
    ctrl, model, cerr = _get_controller(bp, PARAMS.get("graph_name"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        model = _model_of(ctrl, bp, model)
        gname = str(_try(lambda: model.get_graph_name()))
        node = _find_node(model, node_name)
        if node is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "node %r not found on graph %r" % (node_name, gname)}))
        else:
            cls = type(node).__name__
            snap = _try(lambda: ctrl.export_nodes_to_text([unreal.Name(node_name)], True)) or ""
            new = None; kind = "unsupported"
            with unreal.ScopedEditorTransaction("MCP promote_rig_vm_node"):
                if "Collapse" in cls:
                    new = _try(lambda: ctrl.promote_collapse_node_to_function_reference_node(unreal.Name(node_name), True, False))
                    kind = "collapse->function_reference"
                elif "FunctionReference" in cls:
                    new = _try(lambda: ctrl.promote_function_reference_node_to_collapse_node(unreal.Name(node_name), True, False))
                    kind = "function_reference->collapse"
            if kind == "unsupported" or not new:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "node %r is a %s -- promote needs a collapse or function-reference node" % (node_name, cls)}))
            else:
                nn = str(new)
                _ledger().append({"op": "promote_rig_vm_node", "asset_path": path, "graph_name": gname,
                                  "snapshot": snap, "new_node_name": nn})
                _save_cr(path)
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": bp.get_path_name(),
                    "graph_name": gname, "promotion": kind, "from_node": node_name, "new_node_name": nn,
                    "snapshot_chars": len(snap), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def promote_rig_vm_node(ctx, asset_path: str, node_name: str, graph_name: str = None) -> str:
        """Promote a COLLAPSE node to a FUNCTION-REFERENCE node (reusable function), or vice-versa
        (ledgered, FAITHFUL undo).

        asset_path: ControlRigBlueprint path.
        node_name:  a collapse node (-> promoted to a function reference) OR a function-reference node
                    (-> demoted back to an inline collapse node). The node's class decides the direction.
        graph_name: which graph the node lives on (default = the default model).

        Snapshots the node (export_nodes_to_text incl. exterior links) BEFORE promoting. Ledgers op
        'promote_rig_vm_node' {asset_path, graph_name, snapshot, new_node_name}; inverse = remove the
        promoted node + import_nodes_from_text(snapshot)."""
        params = {"asset_path": asset_path, "node_name": node_name, "graph_name": graph_name}
        try:
            return json.dumps(_exec(_PROMOTE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The op schemas
    # (add_rig_vm_node / set_rig_vm_pin_default / set_rig_vm_node_position / remove_rig_vm_node /
    # add_rig_vm_link / break_rig_vm_link) are reported to the coordinator to fold their inverses into
    # editor_level.undo. Inverse logic (self-contained; resolve controller = bp.get_controller() or
    # the named-model controller via graph_name):
    #   ctrl = <controller for graph_name>; model = ctrl.get_graph()
    #   add_rig_vm_node       -> ctrl.remove_node_by_name(unreal.Name(node_name), True, False)
    #                            (add_rig_vm_variable_node ALSO ledgers this op -- same inverse)
    #   add_rig_vm_local_variable {asset_path, graph_name, var_name}
    #                         -> ctrl.remove_local_variable(unreal.Name(var_name), True, False)
    # !! REQUIRED coordinator change to editor_level._crg_ctrl: it currently resolves graph_name only
    #    via bp.get_all_models(), which does NOT include FUNCTION/COLLAPSE contained graphs. Local
    #    variables (and variable nodes referencing them) live on function graphs, so _crg_ctrl MUST be
    #    extended to also search bp.get_local_function_library().get_nodes()[i].get_contained_graph()
    #    by get_graph_name() and return bp.get_controller(that_graph); otherwise both
    #    add_rig_vm_local_variable AND add_rig_vm_node (for variable nodes on function graphs) resolve
    #    to the WRONG (top) controller and their inverses silently fail to find the var/node.
    #   set_rig_vm_pin_default-> ctrl.set_pin_default_value(pin_path, prior_value, True, True, False, False, True)
    #   set_rig_vm_node_position -> ctrl.set_node_position_by_name(unreal.Name(node_name), Vector2D(prior_pos), True, False, False)
    #   remove_rig_vm_node    -> re-add: node_kind "unit" -> add_unit_node_from_struct_path(struct_path,
    #                            method, Vector2D(position), node_name,...); node_kind "comment" ->
    #                            add_comment_node(comment.text, Vector2D(position), Vector2D(comment.size),
    #                            LinearColor(comment.color), node_name,...); then set each captured
    #                            pin_defaults[path]=val via set_pin_default_value
    #   add_rig_vm_link       -> ctrl.break_link(output_pin, input_pin, True, False); for src in prior_sources:
    #                            ctrl.add_link(src, input_pin, True, False)
    #   break_rig_vm_link     -> ctrl.add_link(output_pin, input_pin, True, False)
    # Finish every inverse with unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False).
