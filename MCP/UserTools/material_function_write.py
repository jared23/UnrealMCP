"""UserTools :: Material Functions -- MATERIALS batch M3  (spec: docs/spec/materials.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Fills the material-FUNCTION
authoring gap: graph build / layout / input+output pins / function-instance creation + parameters.
Everything runs through unreal.MaterialEditingLibrary's *_in_function family, which is fully reflected
into Python -- NO C++ handler needed. Verified live vs TestMCPSetup on /Game/MCP_Scratch/MAT3_*.

VERIFIED LIVE (the critical assumptions, before this module was written):
  * connect_material_expressions(from, from_out, to, to_in) WORKS on function-owned expressions:
      returns True on a valid pin pairing, False on a bogus pin name (so the bool is meaningful).
      This is the same call the material-graph writer uses -- there is NO separate "in_function"
      connect variant, and none is needed.
  * create_material_expression_in_function / get_material_function_expressions /
      delete_material_expression_in_function / update_material_function /
      layout_material_function_expressions are all present + reflected.
  * get_inputs_for_material_function_expression(mf, expr) returns the list of CONNECTED upstream
      expressions (unconnected pins omitted) -> used for reachability in cleanup.
  * MaterialExpressionFunctionInput props input_name(FName) / input_type(FunctionInputType) /
      sort_priority(int) are settable; FunctionOutput props output_name(FName) / sort_priority(int).
      Enum class name is exactly unreal.FunctionInputType (members FUNCTION_INPUT_SCALAR /
      FUNCTION_INPUT_VECTOR3 / FUNCTION_INPUT_TEXTURE2D / FUNCTION_INPUT_BOOL / ...).
  * expression positions live on material_expression_editor_x / material_expression_editor_y (int).
  * AssetTools.create_asset(name, path, MaterialFunctionInstance, MaterialFunctionInstanceFactory())
      makes a function INSTANCE non-modally; set editor-property 'parent' to a MaterialFunction.
      Override arrays scalar_parameter_values / vector_parameter_values / texture_parameter_values
      hold Scalar/Vector/TextureParameterValue structs whose parameter_info.name identifies the param
      and whose parameter_value carries the value (FScalarParameterValue pattern -- same shape MPC uses).

Scaffolding (base64 PARAMS injection, @@UMCP@@ marker + Output-Log auto-capture, per-session
builtins._UMCP_LEDGERS ledger, session default) is copied VERBATIM from materials_write3.py /
editor_level.py. This module registers NO `undo` tool (editor_level.py owns the ONE unified undo);
each write's ledger op + intended inverse is documented per-tool + in the batch report for the
coordinator to fold into editor_level.undo.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never
assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the C++ wrapper's own names). At most ONE
update_material_function / layout per execute_python call (batching wedges the game thread).
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
_MEL = unreal.MaterialEditingLibrary
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load(path):
    return _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
def _load_fn(path):
    if not path:
        return None, "no function path given"
    obj = _load(path)
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.MaterialFunction):
        return None, "asset is not a MaterialFunction (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _load_mfi(path):
    if not path:
        return None, "no instance path given"
    obj = _load(path)
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.MaterialFunctionInstance):
        return None, "asset is not a MaterialFunctionInstance (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _load_parent_fn(path):
    if not path:
        return None, "no parent function path given"
    obj = _load(path)
    if obj is None:
        return None, "parent not found: %s" % path
    ok = isinstance(obj, unreal.MaterialFunction) or isinstance(obj, unreal.MaterialFunctionInstance)
    iface = getattr(unreal, "MaterialFunctionInterface", None)
    if not ok and iface is not None:
        ok = isinstance(obj, iface)
    if not ok:
        return None, "parent is not a MaterialFunction/Instance (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _save(path):
    _try(lambda: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False))
def _exprs(mf):
    return list(_try(lambda: _MEL.get_material_function_expressions(mf), []) or [])
def _find_expr(mf, nm):
    for e in _exprs(mf):
        if e.get_name() == nm:
            return e
    return None
def _pos(e):
    return [int(_try(lambda: e.get_editor_property("material_expression_editor_x"), 0) or 0),
            int(_try(lambda: e.get_editor_property("material_expression_editor_y"), 0) or 0)]
def _set_pos(e, x, y):
    _try(lambda: e.set_editor_property("material_expression_editor_x", int(x)))
    _try(lambda: e.set_editor_property("material_expression_editor_y", int(y)))
def _enum_member(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s.strip("<>").strip()
def _resolve_fit(token):
    cls = unreal.FunctionInputType
    t = str(token).strip()
    if hasattr(cls, t):
        return getattr(cls, t)
    up = "FUNCTION_INPUT_" + t.upper()
    if hasattr(cls, up):
        return getattr(cls, up)
    return None
def _resolve_expr_cls(token):
    c = getattr(unreal, str(token), None)
    if c is None:
        c = getattr(unreal, "MaterialExpression" + str(token), None)
    return c
def _jsonify(v):
    if v is None or isinstance(v, bool) or isinstance(v, int) or isinstance(v, float) or isinstance(v, str):
        return v
    return str(v)
def _in_names(e):
    return [str(x) for x in (_try(lambda: _MEL.get_material_expression_input_names(e), []) or [])]
def _in_conn(mf, e):
    res = []
    for x in (_try(lambda: _MEL.get_inputs_for_material_function_expression(mf, e), []) or []):
        res.append(x.get_name() if x is not None else None)
    return res
_CAPT_PROPS = ["r", "g", "b", "a", "const_a", "const_b", "parameter_name", "default_value",
               "input_name", "output_name", "sort_priority", "group", "description",
               "bias", "scale", "period", "min_default", "max_default"]
def _capture_props(e):
    caught = {}
    for k in _CAPT_PROPS:
        try:
            v = e.get_editor_property(k)
        except Exception:
            continue
        if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float) or isinstance(v, str):
            caught[k] = v
        elif k in ("parameter_name", "input_name", "output_name", "group"):
            caught[k] = str(v)
    return caught
def _make_pv(ptype, nm, val):
    if ptype == "scalar":
        pv = unreal.ScalarParameterValue()
    elif ptype == "vector":
        pv = unreal.VectorParameterValue()
    else:
        pv = unreal.TextureParameterValue()
    info = pv.get_editor_property("parameter_info")
    info.set_editor_property("name", nm)
    info.set_editor_property("association", unreal.MaterialParameterAssociation.GLOBAL_PARAMETER)
    info.set_editor_property("index", -1)
    pv.set_editor_property("parameter_info", info)
    pv.set_editor_property("parameter_value", val)
    return pv
def _read_pv(entry, ptype):
    v = entry.get_editor_property("parameter_value")
    if ptype == "scalar":
        return float(v)
    if ptype == "vector":
        return [v.r, v.g, v.b, v.a] if v is not None else None
    return (v.get_path_name() if v is not None else None)
def _mfi_prop(ptype):
    return {"scalar": "scalar_parameter_values", "vector": "vector_parameter_values",
            "texture": "texture_parameter_values"}[ptype]
'''

    # ------------------------------------------------------------------ #
    # 1. build_material_function_graph                                    #
    # ------------------------------------------------------------------ #
    _BUILD_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    nodes = PARAMS.get("nodes") or []
    conns = PARAMS.get("connections") or []
    name_map = {}
    created = []
    node_results = []
    for nd in nodes:
        key = nd.get("name")
        if key is None:
            key = nd.get("id")
        if key is None:
            key = nd.get("key")
        clstok = nd.get("class")
        if clstok is None:
            clstok = nd.get("type")
        cls = _resolve_expr_cls(clstok)
        if cls is None:
            node_results.append({"name": key, "status": "error", "message": "unknown expression class: %s" % clstok})
            continue
        x = int(nd.get("x", 0)); y = int(nd.get("y", 0))
        expr = _try(lambda: _MEL.create_material_expression_in_function(mf, cls, x, y))
        if expr is None:
            node_results.append({"name": key, "status": "error", "message": "create returned None for %s" % clstok})
            continue
        ue = expr.get_name()
        created.append(ue)
        if key is not None:
            name_map[key] = expr
        applied = {}
        for pk, pv in (nd.get("props") or {}).items():
            if pk == "input_type":
                fit = _resolve_fit(pv)
                if fit is not None:
                    _try(lambda: expr.set_editor_property("input_type", fit))
                applied[pk] = _enum_member(_try(lambda: expr.get_editor_property("input_type")))
            else:
                _try(lambda: expr.set_editor_property(pk, pv))
                applied[pk] = _jsonify(_try(lambda: expr.get_editor_property(pk)))
        node_results.append({"name": key, "ue_name": ue, "class": expr.get_class().get_name(),
                             "pos": [x, y], "props_applied": applied, "status": "created"})
    conn_results = []
    for c in conns:
        fk = c.get("from")
        if fk is None:
            fk = c.get("from_node")
        tk = c.get("to")
        if tk is None:
            tk = c.get("to_node")
        fp = c.get("from_pin") or ""
        tp = c.get("to_pin") or ""
        fe = name_map.get(fk); te = name_map.get(tk)
        if fe is None or te is None:
            conn_results.append({"from": fk, "to": tk, "status": "error", "message": "unknown node key(s)"})
            continue
        ok = bool(_try(lambda: _MEL.connect_material_expressions(fe, fp, te, tp)))
        conn_results.append({"from": fk, "to": tk, "from_pin": fp, "to_pin": tp, "connected": ok})
    _MEL.update_material_function(mf)
    _save(PARAMS["function_path"])
    if created:
        _ledger().append({"op": "mf_build_graph", "asset_path": PARAMS["function_path"],
            "object_path": mf.get_path_name(), "node_names": created})
    print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
        "object_path": mf.get_path_name(), "created_count": len(created), "created_names": created,
        "nodes": node_results, "connections": conn_results, "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def build_material_function_graph(ctx, function_path: str, nodes: list,
                                      connections: list = None) -> str:
        """Author a graph inside a MaterialFunction: create expression nodes and wire them. Ledgered write.

        function_path: object/package path of an existing MaterialFunction asset.
        nodes:         list of node dicts. Each: {"name": <key used in connections>,
                       "class": <expression class, e.g. "MaterialExpressionConstant" or shorthand
                       "Constant" / "Multiply" / "FunctionOutput">, "x": int, "y": int,
                       "props": {optional editor-props, e.g. {"r": 0.5} or {"parameter_name": "Tint"}}}.
                       For a FunctionInput node's props, "input_type" accepts a FunctionInputType member
                       name ("FUNCTION_INPUT_SCALAR") or shorthand ("scalar", "vector3", "texture2d").
        connections:   list of {"from": <node key>, "to": <node key>, "from_pin": "" , "to_pin": <input
                       pin name, e.g. "A"/"B"; "" for a single unnamed input like FunctionOutput>}.

        Uses create_material_expression_in_function per node, connect_material_expressions per edge
        (VERIFIED to work on function-owned expressions -- returns True on a valid pin pairing, False
        otherwise), then ONE update_material_function + save. Reports per-node and per-connection status.

        Ledgered op 'mf_build_graph' {asset_path, object_path, node_names:[UE names of every created
        expression, creation order]}. Inverse (FAITHFUL): reload the function, for each node_name find
        the expression by get_name() and delete_material_expression_in_function; then
        update_material_function + save."""
        params = {"function_path": function_path, "nodes": nodes or [], "connections": connections or []}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. layout_material_function_graph                                   #
    # ------------------------------------------------------------------ #
    _LAYOUT_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    prior = []
    for e in _exprs(mf):
        p = _pos(e)
        prior.append({"name": e.get_name(), "x": p[0], "y": p[1]})
    _MEL.layout_material_function_expressions(mf)
    _save(PARAMS["function_path"])
    after = []
    for e in _exprs(mf):
        p = _pos(e)
        after.append({"name": e.get_name(), "x": p[0], "y": p[1]})
    moved = sum(1 for i in range(min(len(prior), len(after)))
                if prior[i]["x"] != after[i]["x"] or prior[i]["y"] != after[i]["y"])
    _ledger().append({"op": "mf_layout", "asset_path": PARAMS["function_path"],
        "object_path": mf.get_path_name(), "prior_positions": prior})
    print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
        "object_path": mf.get_path_name(), "node_count": len(after), "moved_count": moved,
        "after_positions": after, "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def layout_material_function_graph(ctx, function_path: str) -> str:
        """Auto-arrange the expression nodes of a MaterialFunction graph. Ledgered write.

        function_path: object/package path of a MaterialFunction.

        Captures every expression's prior (x, y), calls
        MaterialEditingLibrary.layout_material_function_expressions(mf), then saves. Positions live on
        material_expression_editor_x / material_expression_editor_y.

        Ledgered op 'mf_layout' {asset_path, object_path, prior_positions:[{name, x, y}, ...]}.
        Inverse (FAITHFUL): reload, for each prior_positions entry find the expression by get_name()
        and restore material_expression_editor_x / _y; then save (no recompile needed)."""
        try:
            return json.dumps(_exec(_LAYOUT_BODY, {"function_path": function_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. add_material_function_input                                      #
    # ------------------------------------------------------------------ #
    _ADD_INPUT_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    fit = _resolve_fit(PARAMS.get("input_type") or "scalar")
    if fit is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown input_type '%s' (try scalar|vector2|vector3|vector4|bool|static_bool|texture2d|material_attributes or a FUNCTION_INPUT_* member)" % PARAMS.get("input_type")}))
    else:
        x = int(PARAMS.get("x", 0)); y = int(PARAMS.get("y", 0))
        expr = _try(lambda: _MEL.create_material_expression_in_function(mf, unreal.MaterialExpressionFunctionInput, x, y))
        if expr is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_material_expression_in_function returned None"}))
        else:
            _try(lambda: expr.set_editor_property("input_name", PARAMS["input_name"]))
            _try(lambda: expr.set_editor_property("input_type", fit))
            _try(lambda: expr.set_editor_property("sort_priority", int(PARAMS.get("sort_priority", 0))))
            _MEL.update_material_function(mf)
            _save(PARAMS["function_path"])
            ue = expr.get_name()
            _ledger().append({"op": "mf_add_node", "asset_path": PARAMS["function_path"],
                "object_path": mf.get_path_name(), "node_name": ue})
            print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
                "object_path": mf.get_path_name(), "node_name": ue, "kind": "FunctionInput",
                "input_name": str(_try(lambda: expr.get_editor_property("input_name"))),
                "input_type": _enum_member(_try(lambda: expr.get_editor_property("input_type"))),
                "sort_priority": int(_try(lambda: expr.get_editor_property("sort_priority"), 0) or 0),
                "pos": [x, y], "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def add_material_function_input(ctx, function_path: str, input_name: str,
                                    input_type: str = "scalar", sort_priority: int = 0,
                                    x: int = 0, y: int = 0) -> str:
        """Add a FunctionInput pin node to a MaterialFunction. Ledgered write.

        function_path: object/package path of a MaterialFunction.
        input_name:    name of the new input pin.
        input_type:    FunctionInputType. Accepts a member name ("FUNCTION_INPUT_SCALAR") or shorthand:
                       scalar | vector2 | vector3 | vector4 | bool | static_bool | texture2d |
                       texture_cube | material_attributes (any FUNCTION_INPUT_* member via upper-casing).
        sort_priority: input ordering priority (int).
        x, y:          graph position (default 0,0; run layout_material_function_graph to auto-arrange).

        Creates a MaterialExpressionFunctionInput, sets input_name/input_type/sort_priority, then ONE
        update_material_function + save.

        Ledgered op 'mf_add_node' {asset_path, object_path, node_name}. Inverse (FAITHFUL): reload, find
        the expression by get_name()==node_name, delete_material_expression_in_function, then
        update_material_function + save."""
        params = {"function_path": function_path, "input_name": input_name, "input_type": input_type,
                  "sort_priority": sort_priority, "x": x, "y": y}
        try:
            return json.dumps(_exec(_ADD_INPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. add_material_function_output                                     #
    # ------------------------------------------------------------------ #
    _ADD_OUTPUT_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    x = int(PARAMS.get("x", 0)); y = int(PARAMS.get("y", 0))
    expr = _try(lambda: _MEL.create_material_expression_in_function(mf, unreal.MaterialExpressionFunctionOutput, x, y))
    if expr is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_material_expression_in_function returned None"}))
    else:
        _try(lambda: expr.set_editor_property("output_name", PARAMS["output_name"]))
        _try(lambda: expr.set_editor_property("sort_priority", int(PARAMS.get("sort_priority", 0))))
        _MEL.update_material_function(mf)
        _save(PARAMS["function_path"])
        ue = expr.get_name()
        _ledger().append({"op": "mf_add_node", "asset_path": PARAMS["function_path"],
            "object_path": mf.get_path_name(), "node_name": ue})
        print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
            "object_path": mf.get_path_name(), "node_name": ue, "kind": "FunctionOutput",
            "output_name": str(_try(lambda: expr.get_editor_property("output_name"))),
            "sort_priority": int(_try(lambda: expr.get_editor_property("sort_priority"), 0) or 0),
            "pos": [x, y], "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def add_material_function_output(ctx, function_path: str, output_name: str,
                                     sort_priority: int = 0, x: int = 0, y: int = 0) -> str:
        """Add a FunctionOutput pin node to a MaterialFunction. Ledgered write.

        function_path: object/package path of a MaterialFunction.
        output_name:   name of the new output pin.
        sort_priority: output ordering priority (int).
        x, y:          graph position (default 0,0).

        Creates a MaterialExpressionFunctionOutput, sets output_name/sort_priority, then ONE
        update_material_function + save. A material function needs at least one FunctionOutput to compile.

        Ledgered op 'mf_add_node' {asset_path, object_path, node_name}. Inverse (FAITHFUL): reload, find
        by get_name()==node_name, delete_material_expression_in_function, then update_material_function
        + save. (Shared op with add_material_function_input -- same inverse.)"""
        params = {"function_path": function_path, "output_name": output_name,
                  "sort_priority": sort_priority, "x": x, "y": y}
        try:
            return json.dumps(_exec(_ADD_OUTPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5 + 6. set_material_function_input / set_material_function_output   #
    # ------------------------------------------------------------------ #
    _SET_NODE_PROPS_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
expect = PARAMS["expect_class"]
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    e = _find_expr(mf, PARAMS["node_name"])
    if e is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no expression named '%s' in this function" % PARAMS["node_name"],
            "available": [x.get_name() for x in _exprs(mf)]}))
    elif e.get_class().get_name() != expect:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "expression '%s' is a %s, not a %s" % (PARAMS["node_name"], e.get_class().get_name(), expect)}))
    else:
        props = PARAMS.get("props") or {}
        prior = {}
        applied = {}
        for pk, pv in props.items():
            if pk == "input_type":
                prior[pk] = _enum_member(_try(lambda: e.get_editor_property("input_type")))
                fit = _resolve_fit(pv)
                if fit is not None:
                    _try(lambda: e.set_editor_property("input_type", fit))
                applied[pk] = _enum_member(_try(lambda: e.get_editor_property("input_type")))
            else:
                prior[pk] = _jsonify(_try(lambda: e.get_editor_property(pk)))
                _try(lambda: e.set_editor_property(pk, pv))
                applied[pk] = _jsonify(_try(lambda: e.get_editor_property(pk)))
        _MEL.update_material_function(mf)
        _save(PARAMS["function_path"])
        _ledger().append({"op": "mf_set_node_props", "asset_path": PARAMS["function_path"],
            "object_path": mf.get_path_name(), "node_name": PARAMS["node_name"], "prior_props": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
            "object_path": mf.get_path_name(), "node_name": PARAMS["node_name"],
            "class": expect, "prior_props": prior, "applied": applied, "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def set_material_function_input(ctx, function_path: str, node_name: str, props: dict) -> str:
        """Set editor properties on a FunctionInput node. Ledgered write.

        function_path: object/package path of a MaterialFunction.
        node_name:     UE name of the FunctionInput expression (e.g. 'MaterialExpressionFunctionInput_0';
                       see get_material_function_info).
        props:         dict of editor properties to set, e.g. {"input_name": "Tint",
                       "input_type": "vector3", "sort_priority": 2, "description": "..."}. 'input_type'
                       accepts a FunctionInputType member name or shorthand (scalar/vector3/...).

        Reads the prior value of each named prop, applies the new values, then ONE
        update_material_function + save. Errors if node_name is not a MaterialExpressionFunctionInput.

        Ledgered op 'mf_set_node_props' {asset_path, object_path, node_name, prior_props:{key: prior}}
        (input_type prior stored as its FunctionInputType member NAME). Inverse (FAITHFUL): reload, find
        the expression by get_name()==node_name, for each prior_props key restore the value --
        input_type via getattr(unreal.FunctionInputType, prior), any other key via
        set_editor_property(key, prior); then update_material_function + save."""
        params = {"function_path": function_path, "node_name": node_name, "props": props or {},
                  "expect_class": "MaterialExpressionFunctionInput"}
        try:
            return json.dumps(_exec(_SET_NODE_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_material_function_output(ctx, function_path: str, node_name: str, props: dict) -> str:
        """Set editor properties on a FunctionOutput node. Ledgered write.

        function_path: object/package path of a MaterialFunction.
        node_name:     UE name of the FunctionOutput expression (e.g. 'MaterialExpressionFunctionOutput_0';
                       see get_material_function_info).
        props:         dict of editor properties, e.g. {"output_name": "Result", "sort_priority": 1,
                       "description": "..."}.

        Reads the prior value of each named prop, applies the new values, then ONE
        update_material_function + save. Errors if node_name is not a MaterialExpressionFunctionOutput.

        Ledgered op 'mf_set_node_props' {asset_path, object_path, node_name, prior_props:{key: prior}}.
        Inverse (FAITHFUL, shared with set_material_function_input): reload, find by
        get_name()==node_name, restore each prior_props key via set_editor_property(key, prior); then
        update_material_function + save."""
        params = {"function_path": function_path, "node_name": node_name, "props": props or {},
                  "expect_class": "MaterialExpressionFunctionOutput"}
        try:
            return json.dumps(_exec(_SET_NODE_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 7. get_material_function_info  (READ)                               #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    inputs = []; outputs = []; all_nodes = []
    for e in _exprs(mf):
        cn = e.get_class().get_name(); p = _pos(e)
        all_nodes.append({"name": e.get_name(), "class": cn, "x": p[0], "y": p[1]})
        if cn == "MaterialExpressionFunctionInput":
            inputs.append({"name": e.get_name(),
                "input_name": str(_try(lambda: e.get_editor_property("input_name"))),
                "input_type": _enum_member(_try(lambda: e.get_editor_property("input_type"))),
                "sort_priority": int(_try(lambda: e.get_editor_property("sort_priority"), 0) or 0),
                "x": p[0], "y": p[1]})
        elif cn == "MaterialExpressionFunctionOutput":
            outputs.append({"name": e.get_name(),
                "output_name": str(_try(lambda: e.get_editor_property("output_name"))),
                "sort_priority": int(_try(lambda: e.get_editor_property("sort_priority"), 0) or 0),
                "x": p[0], "y": p[1]})
    print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
        "object_path": mf.get_path_name(),
        "description": str(_try(lambda: mf.get_editor_property("description")) or ""),
        "expression_count": len(all_nodes), "input_count": len(inputs), "output_count": len(outputs),
        "inputs": inputs, "outputs": outputs, "expressions": all_nodes}))
'''

    @mcp.tool()
    def get_material_function_info(ctx, function_path: str) -> str:
        """Enumerate a MaterialFunction's expressions, inputs, and outputs. Read-only.

        function_path: object/package path of a MaterialFunction.

        Returns the full expression list ({name, class, x, y}), plus the FunctionInput nodes
        ({name, input_name, input_type, sort_priority, x, y}) and FunctionOutput nodes ({name,
        output_name, sort_priority, x, y}), and the function description. No ledger. The 'name' field is
        the UE object name to pass to set_material_function_input/output / connections."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"function_path": function_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. validate_material_function  (READ-ish; single update)           #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    exprs = _exprs(mf)
    outs = [e for e in exprs if e.get_class().get_name() == "MaterialExpressionFunctionOutput"]
    ins = [e for e in exprs if e.get_class().get_name() == "MaterialExpressionFunctionInput"]
    _MEL.update_material_function(mf)
    issues = []
    if len(outs) < 1:
        issues.append("no FunctionOutput node -- a material function requires at least one output to compile")
    seen = {}
    for e in outs:
        onm = str(_try(lambda: e.get_editor_property("output_name")) or "")
        seen[onm] = seen.get(onm, 0) + 1
    for onm, cnt in seen.items():
        if cnt > 1:
            issues.append("duplicate output_name '%s' (%d outputs share it)" % (onm, cnt))
    valid = len(issues) == 0
    print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
        "object_path": mf.get_path_name(), "valid": valid, "expression_count": len(exprs),
        "output_count": len(outs), "input_count": len(ins), "issues": issues}))
gc.collect()
'''

    @mcp.tool()
    def validate_material_function(ctx, function_path: str) -> str:
        """Validate a MaterialFunction (>=1 output + refresh). Read-ish (single update, no ledger).

        function_path: object/package path of a MaterialFunction.

        Confirms the function has at least one FunctionOutput (required to compile), flags duplicate
        output names, and calls update_material_function to refresh/recompile. Returns
        {valid, expression_count, output_count, input_count, issues}. Does NOT save or ledger."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"function_path": function_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. cleanup_material_function  (dry_run default True)                #
    # ------------------------------------------------------------------ #
    _CLEANUP_BODY = _HELP + r'''
mf, err = _load_fn(PARAMS["function_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    exprs = _exprs(mf)
    by_name = {e.get_name(): e for e in exprs}
    roots = [e for e in exprs if e.get_class().get_name() == "MaterialExpressionFunctionOutput"]
    reachable = set()
    stack = [e.get_name() for e in roots]
    while stack:
        nm = stack.pop()
        if nm in reachable:
            continue
        reachable.add(nm)
        cur = by_name.get(nm)
        if cur is None:
            continue
        for up in _in_conn(mf, cur):
            if up is not None and up not in reachable:
                stack.append(up)
    protected = ("MaterialExpressionFunctionInput", "MaterialExpressionFunctionOutput")
    removable = []
    for e in exprs:
        if e.get_name() in reachable:
            continue
        if e.get_class().get_name() in protected:
            continue
        removable.append(e)
    captured = []
    for e in removable:
        p = _pos(e)
        pins = _in_names(e); conn = _in_conn(mf, e)
        edges = []
        for i in range(len(conn)):
            pin = pins[i] if i < len(pins) else ""
            if pin == "None":
                pin = ""
            edges.append({"pin": pin, "from": conn[i]})
        captured.append({"name": e.get_name(), "class": e.get_class().get_name(),
            "x": p[0], "y": p[1], "props": _capture_props(e), "inputs": edges})
    if PARAMS.get("dry_run", True):
        print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
            "object_path": mf.get_path_name(), "dry_run": True, "total_expressions": len(exprs),
            "reachable_count": len(reachable), "removable_count": len(removable),
            "would_remove": [c["name"] for c in captured], "detail": captured}))
    else:
        removed = []
        for e in removable:
            _try(lambda: _MEL.delete_material_expression_in_function(mf, e))
            removed.append(e.get_name())
        _MEL.update_material_function(mf)
        _save(PARAMS["function_path"])
        if captured:
            _ledger().append({"op": "mf_cleanup", "asset_path": PARAMS["function_path"],
                "object_path": mf.get_path_name(), "removed": captured})
        print("@@UMCP@@" + json.dumps({"status": "success", "function": mf.get_name(),
            "object_path": mf.get_path_name(), "dry_run": False, "removed": removed,
            "removed_count": len(removed), "detail": captured, "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def cleanup_material_function(ctx, function_path: str, dry_run: bool = True) -> str:
        """Remove expressions unreachable from any FunctionOutput. Ledgered write (only when dry_run=False).

        function_path: object/package path of a MaterialFunction.
        dry_run:       default True -- only REPORT what would be removed. Pass False to actually delete.

        Computes reachability by reverse-traversing from every FunctionOutput node via
        get_inputs_for_material_function_expression (there is no DeleteUnusedExpressions variant for
        functions). Any non-IO expression not reachable from an output is removable. FunctionInput and
        FunctionOutput nodes are ALWAYS preserved (structural pins), never removed. When dry_run=False,
        deletes each via delete_material_expression_in_function, then ONE update_material_function + save.

        Ledgered op 'mf_cleanup' {asset_path, object_path, removed:[{name, class, x, y, props, inputs:
        [{pin, from}]}, ...]} (full node-state capture per removed node). Inverse (best-effort recreate):
        reload; PASS 1 -- for each removed entry, create_material_expression_in_function(mf,
        getattr(unreal, class), x, y), apply captured 'props' via set_editor_property, map old name ->
        new expr; PASS 2 -- for each removed entry, for each captured input {pin, from}, resolve 'from'
        (a recreated node by old-name map, else a surviving node by get_name()) and
        connect_material_expressions(from_expr, '', new_expr, pin); then update_material_function + save.
        NOTE: recreated nodes receive fresh auto-numbered UE names; scalar props (Constant r, parameter
        names, default_value, ...) restore exactly; struct-valued props (e.g. Constant3Vector 'constant'
        LinearColor) are NOT captured, so pass dry_run=True first to see what a cleanup would drop."""
        params = {"function_path": function_path, "dry_run": bool(dry_run)}
        try:
            return json.dumps(_exec(_CLEANUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 10. create_material_function_instance                               #
    # ------------------------------------------------------------------ #
    _CREATE_MFI_BODY = _HELP + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    parent, perr = _load_parent_fn(PARAMS["parent_function_path"])
    if perr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": perr}))
    else:
        created_dir = not EAL.does_directory_exist(package_path)
        mfi = at.create_asset(name, package_path, unreal.MaterialFunctionInstance, unreal.MaterialFunctionInstanceFactory())
        if mfi is None or not isinstance(mfi, unreal.MaterialFunctionInstance):
            if created_dir and EAL.does_directory_exist(package_path):
                _try(lambda: EAL.delete_directory(package_path))
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "create_asset returned %s for %s" % (type(mfi).__name__, asset_path)}))
        else:
            _try(lambda: mfi.set_editor_property("parent", parent))
            after = _try(lambda: mfi.get_editor_property("parent"))
            _close_editors(mfi)
            _save(asset_path)
            _ledger().append({"op": "create_asset", "asset_path": asset_path,
                "package_path": package_path, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success", "name": mfi.get_name(),
                "asset_path": asset_path, "object_path": mfi.get_path_name(),
                "class": mfi.get_class().get_name(),
                "parent": (after.get_path_name() if after else None),
                "created_dir": created_dir, "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def create_material_function_instance(ctx, name: str, path: str,
                                          parent_function_path: str) -> str:
        """Create a MaterialFunctionInstance parented to an existing MaterialFunction. Ledgered write.

        name:                 asset name for the new function instance (e.g. 'MFI_BlendTuned').
        path:                 content directory (e.g. '/Game/MCP_Scratch'); intermediate folders created.
        parent_function_path: object/package path of the parent MaterialFunction (or MaterialFunction
                              Instance) this instance overrides.

        Uses AssetTools.create_asset(name, path, unreal.MaterialFunctionInstance,
        unreal.MaterialFunctionInstanceFactory()) -- non-modal (verified live) -- then sets the 'parent'
        editor property. Override its scalar/vector/texture parameters with
        set_material_function_instance_parameter. Saved on creation.

        Ledgered generic op 'create_asset' {asset_path, package_path, created_dir} -- reuses the
        coordinator's existing create_asset inverse (close editors, delete_asset, rmdir package_path if
        we created it and it is now empty). Same shape as create_material_function /
        create_material_instance."""
        params = {"name": name, "path": path, "parent_function_path": parent_function_path}
        try:
            return json.dumps(_exec(_CREATE_MFI_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 11. set_material_function_instance_parameter                        #
    # ------------------------------------------------------------------ #
    _SET_MFI_PARAM_BODY = _HELP + r'''
ptype = (PARAMS.get("param_type") or "").lower()
nm = PARAMS["param_name"]
mfi, err = _load_mfi(PARAMS["instance_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif ptype not in ("scalar", "vector", "texture"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "param_type must be scalar|vector|texture, got %s" % ptype}))
else:
    prop = _mfi_prop(ptype)
    arr = list(_try(lambda: mfi.get_editor_property(prop), []) or [])
    idx = -1
    for i in range(len(arr)):
        pi = _try(lambda: arr[i].get_editor_property("parameter_info"))
        if pi is not None and str(_try(lambda: pi.get_editor_property("name"))) == nm:
            idx = i
            break
    existed = idx >= 0
    prior_value = None
    if existed:
        prior_value = _read_pv(arr[idx], ptype)
    if ptype == "scalar":
        newval = float(PARAMS["value"])
        if existed:
            arr[idx].set_editor_property("parameter_value", newval)
        else:
            arr.append(_make_pv("scalar", nm, newval))
        shown = newval
    elif ptype == "vector":
        v = PARAMS["value"]
        lc = unreal.LinearColor(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
        if existed:
            arr[idx].set_editor_property("parameter_value", lc)
        else:
            arr.append(_make_pv("vector", nm, lc))
        shown = [lc.r, lc.g, lc.b, lc.a]
    else:
        tex = _load(PARAMS["value"]) if PARAMS.get("value") else None
        if existed:
            arr[idx].set_editor_property("parameter_value", tex)
        else:
            arr.append(_make_pv("texture", nm, tex))
        shown = (tex.get_path_name() if tex is not None else None)
    with unreal.ScopedEditorTransaction("MCP set_mfi_param"):
        mfi.set_editor_property(prop, arr)
    _save(PARAMS["instance_path"])
    _ledger().append({"op": "set_mfi_param", "asset_path": PARAMS["instance_path"],
        "object_path": mfi.get_path_name(), "param_type": ptype, "parameter_name": nm,
        "existed": existed, "prior_value": prior_value, "deleted": False})
    print("@@UMCP@@" + json.dumps({"status": "success", "instance": mfi.get_name(),
        "param_type": ptype, "parameter_name": nm, "existed": existed, "prior_value": prior_value,
        "new_value": shown, "count": len(arr), "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def set_material_function_instance_parameter(ctx, instance_path: str, param_name: str,
                                                 param_type: str, value) -> str:
        """Set one scalar/vector/texture override on a MaterialFunctionInstance. Ledgered write.

        instance_path: object/package path of a MaterialFunctionInstance.
        param_name:    parameter name (matched against parameter_info.name; added if new, updated if
                       present).
        param_type:    'scalar' | 'vector' | 'texture'.
        value:         scalar -> float; vector -> [r,g,b] or [r,g,b,a] (alpha defaults 1.0); texture ->
                       an object/package path to a Texture (or null to clear the override to None).

        Edits the instance's scalar_parameter_values / vector_parameter_values / texture_parameter_values
        array (each entry a Scalar/Vector/TextureParameterValue whose parameter_info.name is the param,
        association GLOBAL_PARAMETER, index -1), inside a ScopedEditorTransaction, then saves. Captures
        whether the entry existed and its prior value.

        Ledgered op 'set_mfi_param' {asset_path, object_path, param_type, parameter_name, existed,
        prior_value, deleted:False}. Inverse (FAITHFUL): reload; find the entry in the param_type array
        by parameter_info.name; if existed -> restore prior_value on that entry's parameter_value
        (scalar float; vector unreal.LinearColor(*prior_value); texture EditorAssetLibrary.load_asset(
        prior_value) or None), set the array back; else -> remove that entry by name; then save."""
        if param_type and param_type.lower() == "vector":
            v = list(value or [])
            if len(v) == 3:
                v = v + [1.0]
            if len(v) != 4:
                return json.dumps({"status": "error",
                    "message": "vector value must be [r,g,b] or [r,g,b,a], got %r" % (value,)}, indent=2)
            value = v
        params = {"instance_path": instance_path, "param_name": param_name,
                  "param_type": param_type, "value": value}
        try:
            return json.dumps(_exec(_SET_MFI_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 12. get_material_function_instance_parameters  (READ)               #
    # ------------------------------------------------------------------ #
    _GET_MFI_PARAMS_BODY = _HELP + r'''
mfi, err = _load_mfi(PARAMS["instance_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    parent = _try(lambda: mfi.get_editor_property("parent"))
    result = {"status": "success", "instance": mfi.get_name(), "object_path": mfi.get_path_name(),
              "parent": (parent.get_path_name() if parent else None), "parameters": {}}
    total = 0
    for ptype in ("scalar", "vector", "texture"):
        prop = _mfi_prop(ptype)
        rows = []
        for entry in (_try(lambda: mfi.get_editor_property(prop), []) or []):
            pi = _try(lambda: entry.get_editor_property("parameter_info"))
            rows.append({"name": (str(_try(lambda: pi.get_editor_property("name"))) if pi is not None else None),
                         "value": _read_pv(entry, ptype)})
            total += 1
        result["parameters"][ptype] = rows
    result["override_count"] = total
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_material_function_instance_parameters(ctx, instance_path: str) -> str:
        """List a MaterialFunctionInstance's scalar/vector/texture parameter overrides. Read-only.

        instance_path: object/package path of a MaterialFunctionInstance.

        Returns the parent path plus the override arrays grouped by kind (scalar / vector / texture),
        each entry {name, value} read from scalar_parameter_values / vector_parameter_values /
        texture_parameter_values. Companion reader for set_material_function_instance_parameter. No
        ledger."""
        try:
            return json.dumps(_exec(_GET_MFI_PARAMS_BODY, {"instance_path": instance_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
