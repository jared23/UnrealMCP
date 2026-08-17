"""UserTools :: Material GRAPH authoring (WRITE)  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Reversible authoring of a
Material's EXPRESSION GRAPH (create expression nodes, wire expression->expression and
expression->material-property connections, set values on created nodes). This is the graph-side
counterpart to material_write.py, which only touches MaterialInstanceConstant parameter OVERRIDES;
the two modules share NO tool names.

Query convention (@@UMCP@@<json> marker), base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py.

Backbone (all verified live vs TestMCPSetup, UE 5.8.1, on scratch dup /Game/MCP_Scratch/MCP_A_*):
  unreal.MaterialEditingLibrary (MEL):
    create_material_expression(material, expression_class, node_pos_x, node_pos_y) -> MaterialExpression
    delete_material_expression(material, expression) -> None  (also disconnects it)
    connect_material_expressions(from_expr, from_output_name, to_expr, to_input_name) -> bool
    disconnect_material_expressions(to_expr, to_input_name) -> bool
    connect_material_property(from_expr, from_output_name, EMaterialProperty) -> bool
    disconnect_material_property(material, EMaterialProperty) -> bool
    get_material_expressions(material) / get_material_expression_input_names(expr) /
    get_inputs_for_material_expression(material, expr) / get_input_node_output_name_for_material_expression /
    get_material_property_input_node(material, prop) / get_material_property_input_node_output_name /
    recompile_material(material)

Expression identity: UMaterialExpression exposes NO python-readable material_expression_guid
(get_editor_property refused). Its UObject name (e.g. "MaterialExpressionMultiply_0") IS stable and
unique within the material and persists across save/reload, so we key the ledger by get_name() and
re-find via get_material_expressions(). Prior connection state is fully readable (which source expr +
output feed a given input pin), so every connect/disconnect has a FAITHFUL inverse: restore the exact
prior source+output, or clear the pin if it was previously unconnected.

Implemented (all validated live, session=agentA_val, editor left CLEAN, ledger depth 0):
  - add_material_expression          (WRITE; op "add_material_expression")
  - set_material_expression_property (WRITE; op "set_material_expression_property")
  - connect_material_expressions     (WRITE; op "connect_material_expression")
  - disconnect_material_expressions  (WRITE; op "disconnect_material_expression")
  - connect_material_property        (WRITE; op "connect_material_property")
  - disconnect_material_property     (WRITE; op "disconnect_material_property")

DEFERRED (refused-not-faked): remove_material_expression of an ARBITRARY existing node. Deleting a
node loses its class, position, per-node values AND every connection into/out of it; reconstructing
all of that is best-effort, not faithful, so a general delete tool is refused. (Deleting a node WE
created is fully reversible and is reached by `undo` of add_material_expression.)

Undo: this module registers NO `undo` tool (editor_level.py owns the ONE unified `undo`). It
introduces 6 new ledger ops; each tool docstring + the build report give the exact inverse for the
coordinator to fold into editor_level.undo.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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
# snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never
# assign a snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/
# original_stderr/success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                 -> per-session undo stack (verbatim from editor_level.py).
    #   _load_material(path)      -> (mat, err) load + type-check a plain Material (not an instance).
    #   _find_expr(mat, name)     -> re-find an expression by its stable UObject name (ledger key).
    #   _resolve_expr_class(spec) -> resolve a MaterialExpression subclass by short name or path.
    #   _resolve_property(spec)   -> resolve an EMaterialProperty (MP_*) enum member.
    #   _read_input_conn(m,e,inp) -> prior state of an input pin {valid,connected,src_name,out_name}.
    #   _settable/_coerce         -> faithful capture/restore of node scalar/vector/name/texture values.
    _MG_HELPERS = r'''
import unreal, json, builtins
_MEL = unreal.MaterialEditingLibrary
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_material(path):
    if not path:
        return None, "no material path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.Material):
        return None, "asset is not a Material (got %s): %s -- graph authoring needs a base Material, not an instance/function" % (obj.get_class().get_name(), path)
    return obj, None
def _find_expr(mat, name):
    if not name:
        return None
    for e in (_MEL.get_material_expressions(mat) or []):
        if e and e.get_name() == name:
            return e
    return None
def _resolve_expr_class(spec):
    if not spec:
        return None, "no expression_class given"
    cls = getattr(unreal, spec, None)
    if cls is None and "." in spec:
        try:
            cls = unreal.load_class(None, spec)
        except Exception:
            cls = None
    if cls is None:
        short = spec if spec.startswith("MaterialExpression") else ("MaterialExpression" + spec)
        cls = getattr(unreal, short, None)
    if cls is None:
        return None, "unknown expression_class: %s (use e.g. MaterialExpressionConstant3Vector or a class path)" % spec
    return cls, None
def _resolve_property(spec):
    if spec is None:
        return None, "no material_property given"
    s = str(spec)
    name = s if s.startswith("MP_") else ("MP_" + s.upper())
    m = getattr(unreal.MaterialProperty, name, None)
    if m is None:
        return None, "unknown material_property: %s (e.g. MP_BASE_COLOR, MP_METALLIC, MP_ROUGHNESS, MP_EMISSIVE_COLOR, MP_NORMAL, MP_OPACITY)" % spec
    return m, None
def _read_input_conn(mat, to_expr, input_name):
    names = [str(x) for x in (_MEL.get_material_expression_input_names(to_expr) or [])]
    if input_name not in names:
        return {"valid": False, "input_names": names}
    idx = names.index(input_name)
    inputs = list(_MEL.get_inputs_for_material_expression(mat, to_expr) or [])
    src = inputs[idx] if idx < len(inputs) else None
    if src is None:
        return {"valid": True, "connected": False}
    try:
        outn = _MEL.get_input_node_output_name_for_material_expression(to_expr, src)
    except Exception:
        outn = ""
    return {"valid": True, "connected": True, "src_name": src.get_name(), "out_name": (str(outn) if outn is not None else "")}
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, bool):
        return (v, True)
    if isinstance(v, (int, float, str)):
        return (v, True)
    if isinstance(v, unreal.LinearColor):
        return ({"__lincolor__": [v.r, v.g, v.b, v.a]}, True)
    if isinstance(v, unreal.Color):
        return ({"__color__": [v.r, v.g, v.b, v.a]}, True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return ({"__name__": str(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if isinstance(value, dict) and "__lincolor__" in value:
        c = value["__lincolor__"]; return unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), float(c[3]))
    if isinstance(value, dict) and "__color__" in value:
        c = value["__color__"]; return unreal.Color(r=int(c[0]), g=int(c[1]), b=int(c[2]), a=int(c[3]))
    if isinstance(value, dict) and "__name__" in value:
        return unreal.Name(str(value["__name__"]))
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]; return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if value is None:
        return None
    if isinstance(current, unreal.LinearColor) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
    if isinstance(current, unreal.Color) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = int(value[3]) if len(value) > 3 else 255
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=aa)
    if isinstance(current, (unreal.Name, unreal.Text)) and isinstance(value, str):
        return unreal.Name(value)
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except Exception:
            return value
    if isinstance(current, float):
        try:
            return float(value)
        except Exception:
            return value
    return value
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
'''

    # ------------------------------------------------------------------ #
    # add_material_expression — create an expression node (ledgered)       #
    # ------------------------------------------------------------------ #
    _ADD_EXPR_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls, cerr = _resolve_expr_class(PARAMS.get("expression_class"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        px = int(PARAMS.get("node_pos_x") or 0)
        py = int(PARAMS.get("node_pos_y") or 0)
        with unreal.ScopedEditorTransaction("MCP add_material_expression"):
            expr = _MEL.create_material_expression(mat, cls, px, py)
            _MEL.recompile_material(mat)
        if expr is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_material_expression returned None for %s" % PARAMS.get("expression_class")}))
        else:
            nm = expr.get_name()
            _save(mat_path)
            _ledger().append({"op": "add_material_expression", "asset_path": mat_path,
                "expr_name": nm, "expr_class": expr.get_class().get_name()})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "asset_path": mat_path, "expr_name": nm, "expr_class": expr.get_class().get_name(),
                "position": list(_MEL.get_material_expression_node_position(expr)),
                "input_names": [str(x) for x in (_MEL.get_material_expression_input_names(expr) or [])],
                "output_names": [str(x) for x in (_MEL.get_material_expression_output_names(expr) or [])],
                "num_expressions": _MEL.get_num_material_expressions(mat),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_material_expression(ctx, material_path: str, expression_class: str,
                                node_pos_x: int = 0, node_pos_y: int = 0) -> str:
        """Create a new expression node inside a base Material's graph (ledgered WRITE).

        material_path:    object/package path of a base Material (NOT a MaterialInstance/Function).
        expression_class: MaterialExpression subclass, by short name ('MaterialExpressionConstant3Vector',
                          or just 'Constant3Vector' -- the 'MaterialExpression' prefix is added) or a
                          full class path. E.g. Constant, Constant3Vector, ScalarParameter,
                          VectorParameter, TextureSample, Multiply, Add, TextureCoordinate.
        node_pos_x/y:     graph editor position for the new node (cosmetic).

        Uses MaterialEditingLibrary.create_material_expression inside a ScopedEditorTransaction, then
        recompile_material + save. Returns the new node's stable name, its input/output pin names, and
        the expression count. Set values on it with set_material_expression_property; wire it with
        connect_material_expressions / connect_material_property.

        Ledgered op 'add_material_expression' {asset_path, expr_name, expr_class}. Inverse (FAITHFUL):
        re-find the expression by name via MEL.get_material_expressions and MEL.delete_material_expression
        (which also disconnects it), then recompile + save."""
        params = {"material_path": material_path, "expression_class": expression_class,
                  "node_pos_x": node_pos_x, "node_pos_y": node_pos_y}
        try:
            return json.dumps(_exec(_ADD_EXPR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_material_expression_property — set a value on a node (ledgered)  #
    # ------------------------------------------------------------------ #
    _SET_EXPR_PROP_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    expr = _find_expr(mat, PARAMS.get("expr_name"))
    if expr is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("expr_name")}))
    else:
        prop = PARAMS["property_name"]
        try:
            prior_raw = expr.get_editor_property(prop); have = True
        except Exception as e:
            have = False
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property '%s' on %s: %s" % (prop, expr.get_class().get_name(), e)}))
        if have:
            prior_json, restorable = _settable(prior_raw)
            newv = _coerce(prior_raw, PARAMS["value"])
            with unreal.ScopedEditorTransaction("MCP set_material_expression_property"):
                expr.set_editor_property(prop, newv)
                _MEL.recompile_material(mat)
            after_json, _u = _settable(expr.get_editor_property(prop))
            _save(mat_path)
            _ledger().append({"op": "set_material_expression_property", "asset_path": mat_path,
                "expr_name": expr.get_name(), "property_name": prop,
                "prior_value": prior_json, "restorable": restorable})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "expr_name": expr.get_name(), "property_name": prop,
                "before": prior_json, "after": after_json, "restorable": restorable,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_material_expression_property(ctx, material_path: str, expr_name: str,
                                         property_name: str, value=None) -> str:
        """Set a value property on an expression node (ledgered WRITE).

        material_path: object/package path of the base Material.
        expr_name:     stable node name returned by add_material_expression (e.g.
                       'MaterialExpressionConstant3Vector_0').
        property_name: reflected property on the node. Common ones: Constant's 'r' (float);
                       Constant3Vector's 'constant' ([r,g,b] or [r,g,b,a]); ScalarParameter's
                       'default_value' (float) and 'parameter_name' (string); VectorParameter's
                       'default_value' ([r,g,b,a]); TextureSample's 'texture' (Texture2D asset path).
        value:         new value. Coerced from the current value's type: floats/ints/bools/strings
                       pass through; [r,g,b(,a)] builds a LinearColor; a string on a texture/object
                       property loads that asset; a string on a Name property (parameter_name) is wrapped.

        Uses set_editor_property inside a ScopedEditorTransaction, then recompile + save. The prior
        value is captured for a faithful restore.

        Ledgered op 'set_material_expression_property' {asset_path, expr_name, property_name,
        prior_value, restorable}. Inverse (FAITHFUL when restorable): re-find the expression, coerce
        prior_value back to the property's type, set_editor_property, recompile + save."""
        params = {"material_path": material_path, "expr_name": expr_name,
                  "property_name": property_name, "value": value}
        try:
            return json.dumps(_exec(_SET_EXPR_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # connect_material_expressions — wire from_expr.out -> to_expr.in      #
    # ------------------------------------------------------------------ #
    _CONNECT_EXPR_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    frm = _find_expr(mat, PARAMS.get("from_expr"))
    to = _find_expr(mat, PARAMS.get("to_expr"))
    if frm is None or to is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "expression not found: from=%s(%s) to=%s(%s)" % (PARAMS.get("from_expr"), frm is not None, PARAMS.get("to_expr"), to is not None)}))
    else:
        to_input = PARAMS.get("to_input") or ""
        from_output = PARAMS.get("from_output") or ""
        prior = _read_input_conn(mat, to, to_input) if to_input else {"valid": True, "connected": False}
        if to_input and not prior.get("valid"):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no input pin named '%s' on %s" % (to_input, to.get_name()),
                "input_names": prior.get("input_names")}))
        else:
            with unreal.ScopedEditorTransaction("MCP connect_material_expressions"):
                ok = _MEL.connect_material_expressions(frm, from_output, to, to_input)
                _MEL.recompile_material(mat)
            _save(mat_path)
            after = _read_input_conn(mat, to, to_input) if to_input else None
            _ledger().append({"op": "connect_material_expression", "asset_path": mat_path,
                "to_expr": to.get_name(), "to_input": to_input,
                "had_prior": bool(prior.get("connected")),
                "prior_src_name": prior.get("src_name"), "prior_out_name": prior.get("out_name")})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "from_expr": frm.get_name(), "from_output": from_output,
                "to_expr": to.get_name(), "to_input": to_input, "connected": bool(ok),
                "prior": prior, "after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_material_expressions(ctx, material_path: str, from_expr: str, to_expr: str,
                                     to_input: str = "", from_output: str = "") -> str:
        """Wire one expression's output into another expression's input pin (ledgered WRITE).

        material_path: object/package path of the base Material.
        from_expr:     source node name (its output feeds the connection).
        to_expr:       destination node name (its input pin receives the connection).
        to_input:      name of the destination input pin (e.g. 'A'/'B' on a Multiply). Empty = first
                       input. See add_material_expression -> input_names for valid pins.
        from_output:   name of the source output pin (e.g. '' for the default RGB, or 'R'/'G'/'B' on a
                       Constant3Vector). Empty = first output.

        Uses MaterialEditingLibrary.connect_material_expressions inside a ScopedEditorTransaction, then
        recompile + save. The destination pin's PRIOR connection (source node + output, or unconnected)
        is captured for a faithful inverse.

        Ledgered op 'connect_material_expression' {asset_path, to_expr, to_input, had_prior,
        prior_src_name, prior_out_name}. Inverse (FAITHFUL): if had_prior -> reconnect the prior source
        (MEL.connect_material_expressions(prior_src, prior_out_name, to_expr, to_input)); else ->
        MEL.disconnect_material_expressions(to_expr, to_input). Then recompile + save."""
        params = {"material_path": material_path, "from_expr": from_expr, "to_expr": to_expr,
                  "to_input": to_input, "from_output": from_output}
        try:
            return json.dumps(_exec(_CONNECT_EXPR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # disconnect_material_expressions — clear an input pin (ledgered)      #
    # ------------------------------------------------------------------ #
    _DISCONNECT_EXPR_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    to = _find_expr(mat, PARAMS.get("to_expr"))
    if to is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("to_expr")}))
    else:
        to_input = PARAMS.get("to_input") or ""
        prior = _read_input_conn(mat, to, to_input)
        if not prior.get("valid"):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no input pin named '%s' on %s" % (to_input, to.get_name()),
                "input_names": prior.get("input_names")}))
        elif not prior.get("connected"):
            print("@@UMCP@@" + json.dumps({"status": "noop", "material": mat.get_name(),
                "to_expr": to.get_name(), "to_input": to_input,
                "message": "input pin was already disconnected; nothing changed (not ledgered)"}))
        else:
            with unreal.ScopedEditorTransaction("MCP disconnect_material_expressions"):
                ok = _MEL.disconnect_material_expressions(to, to_input)
                _MEL.recompile_material(mat)
            _save(mat_path)
            _ledger().append({"op": "disconnect_material_expression", "asset_path": mat_path,
                "to_expr": to.get_name(), "to_input": to_input,
                "prior_src_name": prior.get("src_name"), "prior_out_name": prior.get("out_name")})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "to_expr": to.get_name(), "to_input": to_input, "disconnected": bool(ok),
                "prior": prior, "after": _read_input_conn(mat, to, to_input),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def disconnect_material_expressions(ctx, material_path: str, to_expr: str,
                                        to_input: str = "") -> str:
        """Clear an expression's input pin, removing whatever feeds it (ledgered WRITE).

        material_path: object/package path of the base Material.
        to_expr:       destination node name whose input pin to clear.
        to_input:      name of the input pin (empty = first input).

        Uses MaterialEditingLibrary.disconnect_material_expressions inside a ScopedEditorTransaction,
        then recompile + save. If the pin was already unconnected this is a no-op and is NOT ledgered.
        The prior source (node + output) is captured for a faithful inverse.

        Ledgered op 'disconnect_material_expression' {asset_path, to_expr, to_input, prior_src_name,
        prior_out_name}. Inverse (FAITHFUL): re-find both nodes and
        MEL.connect_material_expressions(prior_src, prior_out_name, to_expr, to_input), then recompile
        + save (the op is only ledgered when a real connection existed, so restore is exact)."""
        params = {"material_path": material_path, "to_expr": to_expr, "to_input": to_input}
        try:
            return json.dumps(_exec(_DISCONNECT_EXPR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # connect_material_property — wire expr.out -> a material property in  #
    # ------------------------------------------------------------------ #
    _CONNECT_PROP_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    frm = _find_expr(mat, PARAMS.get("from_expr"))
    prop, perr = _resolve_property(PARAMS.get("material_property"))
    if frm is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("from_expr")}))
    elif perr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": perr}))
    else:
        from_output = PARAMS.get("from_output") or ""
        pnode = _MEL.get_material_property_input_node(mat, prop)
        prior_src = pnode.get_name() if pnode else None
        prior_out = None
        if pnode is not None:
            try:
                prior_out = str(_MEL.get_material_property_input_node_output_name(mat, prop))
            except Exception:
                prior_out = ""
        with unreal.ScopedEditorTransaction("MCP connect_material_property"):
            ok = _MEL.connect_material_property(frm, from_output, prop)
            _MEL.recompile_material(mat)
        _save(mat_path)
        anode = _MEL.get_material_property_input_node(mat, prop)
        _ledger().append({"op": "connect_material_property", "asset_path": mat_path,
            "material_property": str(PARAMS.get("material_property")),
            "had_prior": pnode is not None, "prior_src_name": prior_src, "prior_out_name": prior_out})
        print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
            "from_expr": frm.get_name(), "from_output": from_output,
            "material_property": str(PARAMS.get("material_property")), "connected": bool(ok),
            "prior_src_name": prior_src, "after_src_name": (anode.get_name() if anode else None),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_material_property(ctx, material_path: str, from_expr: str,
                                  material_property: str, from_output: str = "") -> str:
        """Wire an expression's output into one of the Material's property inputs (ledgered WRITE).

        material_path:     object/package path of the base Material.
        from_expr:         source node name whose output feeds the property.
        material_property: target property input, an EMaterialProperty. Give the short or full name:
                           'MP_BASE_COLOR'/'BASE_COLOR', MP_METALLIC, MP_ROUGHNESS, MP_SPECULAR,
                           MP_EMISSIVE_COLOR, MP_NORMAL, MP_OPACITY, MP_OPACITY_MASK,
                           MP_AMBIENT_OCCLUSION, MP_WORLD_POSITION_OFFSET, etc.
        from_output:       source output pin name (empty = first/default output; 'R'/'G'/'B' to take a
                           single channel of a Constant3Vector).

        Uses MaterialEditingLibrary.connect_material_property inside a ScopedEditorTransaction, then
        recompile + save. The property's PRIOR input node (+ output name) is captured for a faithful
        inverse. Verify with get_material_info / the material readers.

        Ledgered op 'connect_material_property' {asset_path, material_property, had_prior,
        prior_src_name, prior_out_name}. Inverse (FAITHFUL): if had_prior -> reconnect the prior source
        (MEL.connect_material_property(prior_src, prior_out_name, EMaterialProperty)); else ->
        MEL.disconnect_material_property(material, EMaterialProperty). Then recompile + save."""
        params = {"material_path": material_path, "from_expr": from_expr,
                  "material_property": material_property, "from_output": from_output}
        try:
            return json.dumps(_exec(_CONNECT_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # disconnect_material_property — clear a material property input       #
    # ------------------------------------------------------------------ #
    _DISCONNECT_PROP_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    prop, perr = _resolve_property(PARAMS.get("material_property"))
    if perr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": perr}))
    else:
        pnode = _MEL.get_material_property_input_node(mat, prop)
        if pnode is None:
            print("@@UMCP@@" + json.dumps({"status": "noop", "material": mat.get_name(),
                "material_property": str(PARAMS.get("material_property")),
                "message": "property input was already disconnected; nothing changed (not ledgered)"}))
        else:
            prior_src = pnode.get_name()
            try:
                prior_out = str(_MEL.get_material_property_input_node_output_name(mat, prop))
            except Exception:
                prior_out = ""
            with unreal.ScopedEditorTransaction("MCP disconnect_material_property"):
                ok = _MEL.disconnect_material_property(mat, prop)
                _MEL.recompile_material(mat)
            _save(mat_path)
            _ledger().append({"op": "disconnect_material_property", "asset_path": mat_path,
                "material_property": str(PARAMS.get("material_property")),
                "prior_src_name": prior_src, "prior_out_name": prior_out})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "material_property": str(PARAMS.get("material_property")), "disconnected": bool(ok),
                "prior_src_name": prior_src, "prior_out_name": prior_out,
                "after_src_name": (lambda n: n.get_name() if n else None)(_MEL.get_material_property_input_node(mat, prop)),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def disconnect_material_property(ctx, material_path: str, material_property: str) -> str:
        """Clear a Material's property input, removing whatever expression feeds it (ledgered WRITE).

        material_path:     object/package path of the base Material.
        material_property: the EMaterialProperty input to clear (e.g. MP_BASE_COLOR, MP_ROUGHNESS;
                           short names like 'BASE_COLOR' are accepted).

        Uses MaterialEditingLibrary.disconnect_material_property inside a ScopedEditorTransaction, then
        recompile + save. If the input was already disconnected this is a no-op and is NOT ledgered.
        The prior source node (+ output) is captured for a faithful inverse.

        Ledgered op 'disconnect_material_property' {asset_path, material_property, prior_src_name,
        prior_out_name}. Inverse (FAITHFUL): re-find the prior source node and
        MEL.connect_material_property(prior_src, prior_out_name, EMaterialProperty), then recompile +
        save (only ledgered when a real connection existed, so restore is exact)."""
        params = {"material_path": material_path, "material_property": material_property}
        try:
            return json.dumps(_exec(_DISCONNECT_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NEW ledger ops for editor_level.undo to learn (report to coordinator).
    # All re-find expressions by their stable UObject name via
    #   mat = EditorAssetLibrary.load_asset(asset_path)   # a base unreal.Material
    #   MEL = unreal.MaterialEditingLibrary
    #   def _find(m, nm): return next((e for e in (MEL.get_material_expressions(m) or []) if e and e.get_name()==nm), None)
    #   def _prop(s):     return getattr(unreal.MaterialProperty, s if s.startswith("MP_") else "MP_"+s.upper())
    # and finish each with MEL.recompile_material(mat) + EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False); mat=None
    #
    # 1) op "add_material_expression" {asset_path, expr_name, expr_class}
    #      e = _find(mat, expr_name)
    #      if e: with ScopedEditorTransaction: MEL.delete_material_expression(mat, e)   # also disconnects
    #
    # 2) op "set_material_expression_property" {asset_path, expr_name, property_name, prior_value, restorable}
    #      if not restorable: skip.  e=_find(mat,expr_name)
    #      with ScopedEditorTransaction: e.set_editor_property(property_name, _coerce(e.get_editor_property(property_name), prior_value))
    #      (prior_value uses the tagged forms {"__lincolor__":[r,g,b,a]}/{"__color__":..}/{"__name__":..}/{"__object__":path}
    #       or a bare scalar/str -- same _coerce as this module.)
    #
    # 3) op "connect_material_expression" {asset_path, to_expr, to_input, had_prior, prior_src_name, prior_out_name}
    #      to=_find(mat,to_expr)
    #      with ScopedEditorTransaction:
    #        if had_prior: MEL.connect_material_expressions(_find(mat, prior_src_name), prior_out_name or "", to, to_input)
    #        else:         MEL.disconnect_material_expressions(to, to_input)
    #
    # 4) op "disconnect_material_expression" {asset_path, to_expr, to_input, prior_src_name, prior_out_name}
    #      to=_find(mat,to_expr)
    #      with ScopedEditorTransaction: MEL.connect_material_expressions(_find(mat, prior_src_name), prior_out_name or "", to, to_input)
    #
    # 5) op "connect_material_property" {asset_path, material_property, had_prior, prior_src_name, prior_out_name}
    #      p=_prop(material_property)
    #      with ScopedEditorTransaction:
    #        if had_prior: MEL.connect_material_property(_find(mat, prior_src_name), prior_out_name or "", p)
    #        else:         MEL.disconnect_material_property(mat, p)
    #
    # 6) op "disconnect_material_property" {asset_path, material_property, prior_src_name, prior_out_name}
    #      p=_prop(material_property)
    #      with ScopedEditorTransaction: MEL.connect_material_property(_find(mat, prior_src_name), prior_out_name or "", p)
    #
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # ------------------------------------------------------------------ #
