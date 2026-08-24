"""UserTools :: Material GRAPH authoring (WRITE2)  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). Second material-graph WRITE
wave. This is the graph-EDITING counterpart to material_graph_write.py (which only ADDS nodes /
wires): it deletes, duplicates, moves, auto-lays-out, batch-builds, and prunes a base Material's
EXPRESSION GRAPH. Query convention (@@UMCP@@<json> marker), base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py / material_graph_write.py. NO tool names overlap with material_graph_write.py.

Backbone (all reflected + verified live vs TestMCPSetup, UE 5.8.1, on scratch dup /Game/MCP_Scratch/MAT1_*):
  unreal.MaterialEditingLibrary (MEL):
    delete_material_expression(material, expression) -> None        (also disconnects the node)
    duplicate_material_expression(material, material_function, expression) -> MaterialExpression
        (material_function=None on a base Material; copies all node parameters)
    layout_material_expressions(material) -> None                   (grid auto-layout; positions only)
    delete_unused_expressions(material) -> None                     (removes nodes not reachable from
        any material output; recompile afterwards)
    create_material_expression / connect_material_expressions / connect_material_property /
    get_material_expressions / get_material_expression_input_names / get_material_expression_output_names /
    get_inputs_for_material_expression / get_input_node_output_name_for_material_expression /
    get_material_property_input_node / get_material_property_input_node_output_name /
    get_material_expression_node_position / recompile_material
  Node POSITION is the reflected editor property material_expression_editor_x / _y (there is NO
  set_material_expression_node_position in this build -- verified absent).

Full-state pre-capture (the reversibility backbone): a node is captured as
  {node_name, node_class, node_class_path, node_pos:[x,y], node_props:{name->tagged}, own_inputs:[...]}
where node_props is the union of dir(expr) names and a curated candidate list, each probed via
get_editor_property and passed through _settable (dir() alone MISSES const_a/const_b, parameter_name,
default_value on many nodes -- verified live, hence the curated superset). Only _settable-restorable
values are kept; ExpressionInput wire structs are captured separately as own_inputs, never as props.

Undo: this module registers NO `undo` tool (editor_level.py owns the ONE unified `undo`). It
introduces 6 new ledger ops; each tool docstring + the batch report give the exact self-describing
inverse for the coordinator to fold into editor_level.undo.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO backslash; all data crosses as base64. Never assign a
snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
success/user_code/code_obj (the C++ wrapper's own names). ONE material recompile per execute_python
call max -- delete/build/cleanup recompile once at the very end; duplicate/move/layout do NOT
recompile (position-only or a disconnected copy -> shader output unchanged).
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
    #   _load_material(path)      -> (mat, err) load + type-check a plain base Material.
    #   _find_expr(mat, name)     -> re-find an expression by its stable UObject name.
    #   _resolve_expr_class(spec) -> MaterialExpression subclass by short name / full class path.
    #   _resolve_property(spec)   -> EMaterialProperty (MP_*) enum member.
    #   _settable/_coerce         -> faithful capture/restore of node scalar/color/name/object values.
    #   _capture_props / _own_inputs / _consumers_of / _capture_node / _reachable_names -> full-state
    #     graph pre-capture used by delete / build(clear) / cleanup inverses.
    _MG_HELPERS = r'''
import unreal, json, builtins
_MEL = unreal.MaterialEditingLibrary
_MP_NAMES = [a for a in dir(unreal.MaterialProperty) if a.startswith("MP_") and a != "MP_MAX"]
_VALUE_CANDS = ["r","g","b","a","constant","default_value","parameter_name","group","sort_priority",
    "const_a","const_b","const_alpha","const_exponent","min_default","max_default","texture",
    "sampler_type","sampler_source","mip_value_mode","const_coordinate","coordinate_index",
    "u_tiling","v_tiling","mirror_u","mirror_v","un_mirror_u","un_mirror_v","speed_x","speed_y",
    "fractional_part","exponent","base_reflect_fraction","period","override_period","ignore_pause",
    "desc","slider_min","slider_max","primitive_data_index","bias","scale","func","time",
    "default_value_r","default_value_g","default_value_b","default_value_a","use_constant_alpha",
    "clamp_result","fresnel_exponent","p","tiling"]
_POS_PROPS = set(["material_expression_editor_x", "material_expression_editor_y"])
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
def _node_pos(e):
    try:
        p = _MEL.get_material_expression_node_position(e)
        return [int(p[0]), int(p[1])]
    except Exception:
        try:
            return [int(e.get_editor_property("material_expression_editor_x")), int(e.get_editor_property("material_expression_editor_y"))]
        except Exception:
            return [0, 0]
def _set_pos(e, x, y):
    try:
        e.set_editor_property("material_expression_editor_x", int(x))
        e.set_editor_property("material_expression_editor_y", int(y))
        return True
    except Exception:
        return False
def _capture_props(e):
    cand = set(_VALUE_CANDS)
    for nm in dir(e):
        if not nm.startswith("_"):
            cand.add(nm)
    props = {}
    for nm in sorted(cand):
        if nm in _POS_PROPS:
            continue
        try:
            v = e.get_editor_property(nm)
        except Exception:
            continue
        if callable(v):
            continue
        sv, restorable = _settable(v)
        if restorable:
            props[nm] = sv
    return props
def _own_inputs(mat, e):
    res = []
    innames = [str(x) for x in (_MEL.get_material_expression_input_names(e) or [])]
    ins = list(_MEL.get_inputs_for_material_expression(mat, e) or [])
    for i in range(len(innames)):
        src = ins[i] if i < len(ins) else None
        if src is not None:
            try:
                on = str(_MEL.get_input_node_output_name_for_material_expression(e, src))
            except Exception:
                on = ""
            res.append({"input": innames[i], "src_name": src.get_name(), "out_name": on})
    return res
def _consumers_of(mat, target):
    tn = target.get_name()
    expr_cons = []
    for e in (_MEL.get_material_expressions(mat) or []):
        if e.get_name() == tn:
            continue
        innames = [str(x) for x in (_MEL.get_material_expression_input_names(e) or [])]
        ins = list(_MEL.get_inputs_for_material_expression(mat, e) or [])
        for i in range(len(innames)):
            src = ins[i] if i < len(ins) else None
            if src is not None and src.get_name() == tn:
                try:
                    on = str(_MEL.get_input_node_output_name_for_material_expression(e, src))
                except Exception:
                    on = ""
                expr_cons.append({"consumer": e.get_name(), "input": innames[i], "out_name": on})
    prop_cons = []
    for a in _MP_NAMES:
        prop = getattr(unreal.MaterialProperty, a, None)
        if prop is None:
            continue
        try:
            n = _MEL.get_material_property_input_node(mat, prop)
        except Exception:
            n = None
        if n is not None and n.get_name() == tn:
            try:
                on = str(_MEL.get_material_property_input_node_output_name(mat, prop))
            except Exception:
                on = ""
            prop_cons.append({"material_property": a, "out_name": on})
    return expr_cons, prop_cons
def _capture_node(mat, e):
    return {"node_name": e.get_name(), "node_class": e.get_class().get_name(),
        "node_class_path": e.get_class().get_path_name(), "node_pos": _node_pos(e),
        "node_props": _capture_props(e), "own_inputs": _own_inputs(mat, e)}
def _reachable_names(mat):
    exprs = list(_MEL.get_material_expressions(mat) or [])
    allnames = [e.get_name() for e in exprs]
    seen = set()
    stack = []
    for a in _MP_NAMES:
        prop = getattr(unreal.MaterialProperty, a, None)
        if prop is None:
            continue
        try:
            n = _MEL.get_material_property_input_node(mat, prop)
        except Exception:
            n = None
        if n is not None and n.get_name() not in seen:
            seen.add(n.get_name()); stack.append(n)
    while stack:
        cur = stack.pop()
        try:
            ins = _MEL.get_inputs_for_material_expression(mat, cur) or []
        except Exception:
            ins = []
        for src in ins:
            if src is not None and src.get_name() not in seen:
                seen.add(src.get_name()); stack.append(src)
    return seen, allnames
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
'''

    # ------------------------------------------------------------------ #
    # delete_material_expression -- remove a node, capture full inverse    #
    # ------------------------------------------------------------------ #
    _DELETE_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    e = _find_expr(mat, PARAMS.get("node_name"))
    if e is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("node_name")}))
    else:
        cap = _capture_node(mat, e)
        expr_cons, prop_cons = _consumers_of(mat, e)
        with unreal.ScopedEditorTransaction("MCP delete_material_expression"):
            _MEL.delete_material_expression(mat, e)
            _MEL.recompile_material(mat)
        _save(mat_path)
        entry = {"op": "delete_material_expression", "asset_path": mat_path,
            "node_name": cap["node_name"], "node_class": cap["node_class"],
            "node_class_path": cap["node_class_path"], "node_pos": cap["node_pos"],
            "node_props": cap["node_props"], "own_inputs": cap["own_inputs"],
            "consumers": expr_cons, "prop_consumers": prop_cons}
        _ledger().append(entry)
        print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
            "asset_path": mat_path, "deleted": cap["node_name"], "node_class": cap["node_class"],
            "captured_props": sorted(cap["node_props"].keys()), "own_inputs": cap["own_inputs"],
            "expr_consumers": expr_cons, "prop_consumers": prop_cons,
            "num_expressions": _MEL.get_num_material_expressions(mat),
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_material_expression(ctx, material_path: str, node_name: str) -> str:
        """Delete one expression node from a base Material's graph (ledgered WRITE, FULLY reversible).

        material_path: object/package path of a base Material (NOT a MaterialInstance/Function).
        node_name:     stable UObject name of the node to delete (e.g. 'MaterialExpressionMultiply_0';
                       from add_material_expression / get_material_graph_nodes).

        BEFORE deleting, the node's full state is pre-captured so undo can recreate it faithfully:
        its class (short name + full class path), position, every _settable editor property, its OWN
        input wires, AND every downstream consumer -- both expression pins that read it (scanned via
        MEL.get_inputs_for_material_expression across all nodes) and material-property inputs it feeds
        (scanned via MEL.get_material_property_input_node over every EMaterialProperty). Then
        MEL.delete_material_expression (which also disconnects it) inside a ScopedEditorTransaction,
        recompile + save.

        Ledgered op 'delete_material_expression' {asset_path, node_name, node_class, node_class_path,
        node_pos:[x,y], node_props:{name->tagged}, own_inputs:[{input,src_name,out_name}],
        consumers:[{consumer,input,out_name}], prop_consumers:[{material_property,out_name}]}.
        Inverse (FAITHFUL, best-effort on node UObject name -- a recreated node may get a NEW name):
          mat=load; cls=getattr(unreal,node_class) or unreal.load_class(None,node_class_path)
          with ScopedEditorTransaction:
            ne=MEL.create_material_expression(mat, cls, node_pos[0], node_pos[1])
            for k,v in node_props: try ne.set_editor_property(k, _coerce(ne.get_editor_property(k), v))
            for w in own_inputs:      MEL.connect_material_expressions(_find(w.src_name), w.out_name, ne, w.input)
            for c in consumers:       MEL.connect_material_expressions(ne, c.out_name, _find(c.consumer), c.input)
            for c in prop_consumers:  MEL.connect_material_property(ne, c.out_name, _prop(c.material_property))
            MEL.recompile_material(mat)
          save.  (own_inputs sources + expr consumers still exist because only THIS node was removed.)"""
        params = {"material_path": material_path, "node_name": node_name}
        try:
            return json.dumps(_exec(_DELETE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # duplicate_material_expression -- copy a node (disconnected)           #
    # ------------------------------------------------------------------ #
    _DUPLICATE_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    src = _find_expr(mat, PARAMS.get("node_name"))
    if src is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("node_name")}))
    else:
        before = set(x.get_name() for x in (_MEL.get_material_expressions(mat) or []))
        sp = _node_pos(src)
        off = int(PARAMS.get("offset") if PARAMS.get("offset") is not None else 48)
        with unreal.ScopedEditorTransaction("MCP duplicate_material_expression"):
            dup = _MEL.duplicate_material_expression(mat, None, src)
            if dup is not None and off:
                _set_pos(dup, sp[0] + off, sp[1] + off)
        _save(mat_path)
        after = set(x.get_name() for x in (_MEL.get_material_expressions(mat) or []))
        new_names = sorted(after - before)
        new_name = dup.get_name() if dup is not None else (new_names[0] if new_names else None)
        if new_name is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "duplicate_material_expression returned None and no new node appeared"}))
        else:
            _ledger().append({"op": "duplicate_material_expression", "asset_path": mat_path,
                "source_name": src.get_name(), "new_name": new_name})
            print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
                "asset_path": mat_path, "source_name": src.get_name(), "new_name": new_name,
                "new_pos": _node_pos(_find_expr(mat, new_name)),
                "num_expressions": _MEL.get_num_material_expressions(mat),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def duplicate_material_expression(ctx, material_path: str, node_name: str,
                                      offset: int = 48) -> str:
        """Duplicate one expression node within the same base Material (ledgered WRITE, reversible).

        material_path: object/package path of a base Material.
        node_name:     stable UObject name of the node to duplicate.
        offset:        pixels to shift the copy down-right so it does not overlap the source (default
                       48; 0 = exact overlap). Cosmetic only.

        Uses MEL.duplicate_material_expression(material, None, expression) -- material_function=None on
        a base Material; it copies all node parameters. The copy is created DISCONNECTED (no input/
        output wires), so the shader output is unchanged -> NO recompile (avoids game-thread wedge);
        just save. The new node's stable name is captured by diffing the expression set.

        Ledgered op 'duplicate_material_expression' {asset_path, source_name, new_name}. Inverse
        (FAITHFUL): re-find new_name and MEL.delete_material_expression(mat, node); save (no recompile
        needed -- it was disconnected). Because the copy carries a stable unique name, the inverse is
        exact."""
        params = {"material_path": material_path, "node_name": node_name, "offset": offset}
        try:
            return json.dumps(_exec(_DUPLICATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # move_material_expression -- set a node's graph position              #
    # ------------------------------------------------------------------ #
    _MOVE_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    e = _find_expr(mat, PARAMS.get("node_name"))
    if e is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "expression not found: %s" % PARAMS.get("node_name")}))
    else:
        prior = _node_pos(e)
        nx = int(PARAMS.get("x") or 0)
        ny = int(PARAMS.get("y") or 0)
        with unreal.ScopedEditorTransaction("MCP move_material_expression"):
            ok = _set_pos(e, nx, ny)
        _save(mat_path)
        _ledger().append({"op": "move_material_expression", "asset_path": mat_path,
            "node_name": e.get_name(), "prior_x": prior[0], "prior_y": prior[1]})
        print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
            "asset_path": mat_path, "node_name": e.get_name(), "moved": bool(ok),
            "prior_pos": prior, "new_pos": _node_pos(e), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def move_material_expression(ctx, material_path: str, node_name: str, x: int, y: int) -> str:
        """Move an expression node to a new graph position (ledgered WRITE, reversible).

        material_path: object/package path of a base Material.
        node_name:     stable UObject name of the node to move.
        x, y:          new graph-editor coordinates (material_expression_editor_x / _y).

        Position is purely cosmetic, so this sets material_expression_editor_x / _y inside a
        ScopedEditorTransaction and saves -- NO recompile (avoids game-thread wedge). The prior x/y are
        captured.

        Ledgered op 'move_material_expression' {asset_path, node_name, prior_x, prior_y}. Inverse
        (FAITHFUL): re-find node, set material_expression_editor_x/_y back to prior_x/prior_y, save."""
        params = {"material_path": material_path, "node_name": node_name, "x": x, "y": y}
        try:
            return json.dumps(_exec(_MOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # layout_material_graph -- grid auto-layout, capture prior positions   #
    # ------------------------------------------------------------------ #
    _LAYOUT_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    prior = {}
    for e in (_MEL.get_material_expressions(mat) or []):
        prior[e.get_name()] = _node_pos(e)
    with unreal.ScopedEditorTransaction("MCP layout_material_graph"):
        _MEL.layout_material_expressions(mat)
    _save(mat_path)
    after = {}
    for e in (_MEL.get_material_expressions(mat) or []):
        after[e.get_name()] = _node_pos(e)
    _ledger().append({"op": "layout_material_graph", "asset_path": mat_path,
        "prior_positions": prior})
    print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
        "asset_path": mat_path, "node_count": len(prior), "prior_positions": prior,
        "after_positions": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def layout_material_graph(ctx, material_path: str) -> str:
        """Auto-arrange all expression nodes of a base Material into a grid (ledgered WRITE, reversible).

        material_path: object/package path of a base Material.

        Uses MEL.layout_material_expressions(material), which repositions every node (adds/removes
        nothing), inside a ScopedEditorTransaction, then saves -- NO recompile (positions only). Every
        node's PRIOR position is captured (keyed by stable name) for a faithful restore.

        Ledgered op 'layout_material_graph' {asset_path, prior_positions:{node_name:[x,y]}}. Inverse
        (FAITHFUL): for node_name,[x,y] in prior_positions: re-find node, set
        material_expression_editor_x/_y back to x/y; save. Node names are stable across layout, so the
        restore is exact."""
        params = {"material_path": material_path}
        try:
            return json.dumps(_exec(_LAYOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # build_material_graph -- batch create + connect in one transaction    #
    # ------------------------------------------------------------------ #
    _BUILD_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    nodes = PARAMS.get("nodes") or []
    conns = PARAMS.get("connections") or []
    clear_first = bool(PARAMS.get("clear_first"))
    errors = []
    cleared_nodes = []
    cleared_matprops = []
    # 1. optional full-graph capture + clear (fully reversible)
    if clear_first:
        for e in (_MEL.get_material_expressions(mat) or []):
            cleared_nodes.append(_capture_node(mat, e))
        for a in _MP_NAMES:
            prop = getattr(unreal.MaterialProperty, a, None)
            if prop is None:
                continue
            try:
                n = _MEL.get_material_property_input_node(mat, prop)
            except Exception:
                n = None
            if n is not None:
                try:
                    on = str(_MEL.get_material_property_input_node_output_name(mat, prop))
                except Exception:
                    on = ""
                cleared_matprops.append({"material_property": a, "src_name": n.get_name(), "out_name": on})
    label_to_name = {}
    created = []
    conn_priors = []
    def _res(ref):
        if ref is None:
            return None
        if ref in label_to_name:
            return _find_expr(mat, label_to_name[ref])
        return _find_expr(mat, ref)
    with unreal.ScopedEditorTransaction("MCP build_material_graph"):
        if clear_first:
            for e in list(_MEL.get_material_expressions(mat) or []):
                _MEL.delete_material_expression(mat, e)
        # 2. create nodes
        for i in range(len(nodes)):
            nd = nodes[i] or {}
            cls, cerr = _resolve_expr_class(nd.get("class") or nd.get("expression_class"))
            if cerr:
                errors.append("node[%d]: %s" % (i, cerr)); continue
            px = int(nd.get("x") if nd.get("x") is not None else (nd.get("node_pos_x") or 0))
            py = int(nd.get("y") if nd.get("y") is not None else (nd.get("node_pos_y") or 0))
            ne = _MEL.create_material_expression(mat, cls, px, py)
            if ne is None:
                errors.append("node[%d]: create returned None for %s" % (i, nd.get("class"))); continue
            nm = ne.get_name()
            created.append(nm)
            lbl = nd.get("name") or nd.get("label")
            if lbl:
                label_to_name[lbl] = nm
            label_to_name[nm] = nm
            props = nd.get("props") or {}
            for k in props:
                try:
                    ne.set_editor_property(k, _coerce(ne.get_editor_property(k), props[k]))
                except Exception as ex:
                    errors.append("node[%d].prop %s: %s" % (i, k, str(ex)[:80]))
        # 3. connections
        for i in range(len(conns)):
            cn = conns[i] or {}
            prop_spec = cn.get("property") or cn.get("material_property")
            frm = _res(cn.get("from") or cn.get("from_expr"))
            fout = cn.get("from_output") or ""
            if frm is None:
                errors.append("conn[%d]: from node not found: %s" % (i, cn.get("from") or cn.get("from_expr"))); continue
            if prop_spec is not None:
                prop, perr = _resolve_property(prop_spec)
                if perr:
                    errors.append("conn[%d]: %s" % (i, perr)); continue
                pnode = _MEL.get_material_property_input_node(mat, prop)
                pprior = None
                if pnode is not None:
                    try:
                        pprior = str(_MEL.get_material_property_input_node_output_name(mat, prop))
                    except Exception:
                        pprior = ""
                conn_priors.append({"kind": "prop", "material_property": str(prop_spec),
                    "had_prior": pnode is not None,
                    "prior_src_name": (pnode.get_name() if pnode is not None else None),
                    "prior_out_name": pprior})
                _MEL.connect_material_property(frm, fout, prop)
            else:
                to = _res(cn.get("to") or cn.get("to_expr"))
                tin = cn.get("to_input") or ""
                if to is None:
                    errors.append("conn[%d]: to node not found: %s" % (i, cn.get("to") or cn.get("to_expr"))); continue
                # capture prior on the destination pin
                names = [str(x) for x in (_MEL.get_material_expression_input_names(to) or [])]
                had = False; psrc = None; pout = None
                if tin in names:
                    idx = names.index(tin)
                    ins = list(_MEL.get_inputs_for_material_expression(mat, to) or [])
                    s = ins[idx] if idx < len(ins) else None
                    if s is not None:
                        had = True; psrc = s.get_name()
                        try:
                            pout = str(_MEL.get_input_node_output_name_for_material_expression(to, s))
                        except Exception:
                            pout = ""
                conn_priors.append({"kind": "expr", "to_expr": to.get_name(), "to_input": tin,
                    "had_prior": had, "prior_src_name": psrc, "prior_out_name": pout})
                _MEL.connect_material_expressions(frm, fout, to, tin)
        _MEL.recompile_material(mat)
    _save(mat_path)
    entry = {"op": "build_material_graph", "asset_path": mat_path, "created": created,
        "conn_priors": conn_priors, "cleared_first": clear_first,
        "cleared_nodes": cleared_nodes, "cleared_matprops": cleared_matprops}
    _ledger().append(entry)
    print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
        "asset_path": mat_path, "created": created, "label_map": label_to_name,
        "connections_made": len(conn_priors), "cleared_first": clear_first,
        "cleared_count": len(cleared_nodes), "errors": errors,
        "num_expressions": _MEL.get_num_material_expressions(mat),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def build_material_graph(ctx, material_path: str, nodes: list = None,
                             connections: list = None, clear_first: bool = False) -> str:
        """Batch-build a base Material's graph: create many nodes + wire them in ONE transaction
        (ledgered WRITE, reversible).

        material_path: object/package path of a base Material.
        nodes:         list of node specs. Each: {"class": <MaterialExpression short name or class
                       path>, "x": int, "y": int, "name": <optional label used by connections>,
                       "props": {<editor_property>: <value>}}. props are coerced like
                       set_material_expression_property (floats/ints/bools/strings pass through,
                       [r,g,b(,a)] -> LinearColor, a string on a texture/object prop loads that asset).
        connections:   list of wiring specs. Expression->expression: {"from": <label|name>,
                       "from_output": "", "to": <label|name>, "to_input": <pin>}. Expression->material
                       property: {"from": <label|name>, "from_output": "", "property": "MP_BASE_COLOR"}.
                       'from'/'to' resolve to a just-created node by its label, else an existing node by
                       stable name.
        clear_first:   if True, the ENTIRE existing graph is full-state captured then deleted before
                       building (so it is fully restorable).

        Everything runs inside one ScopedEditorTransaction with a SINGLE recompile at the end (never
        batch multiple recompiles -- it wedges the game thread), then save. Connections into
        pre-existing nodes / material properties capture their prior wiring.

        Ledgered op 'build_material_graph' {asset_path, created:[name...], conn_priors:[{kind:'expr',
        to_expr,to_input,had_prior,prior_src_name,prior_out_name} | {kind:'prop',material_property,
        had_prior,prior_src_name,prior_out_name}], cleared_first, cleared_nodes:[<full-state capture>],
        cleared_matprops:[{material_property,src_name,out_name}]}.
        Inverse (FAITHFUL; recreated nodes may get NEW UObject names):
          mat=load
          with ScopedEditorTransaction:
            for nm in created: e=_find(nm); if e: MEL.delete_material_expression(mat, e)
            for c in conn_priors:                         # restore pins that survived the build
              if c.kind=='expr': to=_find(c.to_expr)
                 if c.had_prior: MEL.connect_material_expressions(_find(c.prior_src_name), c.prior_out_name, to, c.to_input)
                 else:           MEL.disconnect_material_expressions(to, c.to_input)
              else: p=_prop(c.material_property)
                 if c.had_prior: MEL.connect_material_property(_find(c.prior_src_name), c.prior_out_name, p)
                 else:           MEL.disconnect_material_property(mat, p)
            if cleared_first:                             # recreate the whole prior graph
              m={} ; for cap in cleared_nodes:
                 cls=getattr(unreal,cap.node_class) or unreal.load_class(None,cap.node_class_path)
                 ne=MEL.create_material_expression(mat, cls, cap.node_pos[0], cap.node_pos[1])
                 for k,v in cap.node_props: try ne.set_editor_property(k, _coerce(ne.get_editor_property(k), v))
                 m[cap.node_name]=ne
              for cap in cleared_nodes:                   # wire own inputs via old->new map
                 for w in cap.own_inputs:
                    s=m.get(w.src_name) or _find(w.src_name)
                    if s: MEL.connect_material_expressions(s, w.out_name, m[cap.node_name], w.input)
              for mp in cleared_matprops:                 # wire material properties
                 s=m.get(mp.src_name) or _find(mp.src_name)
                 if s: MEL.connect_material_property(s, mp.out_name, _prop(mp.material_property))
            MEL.recompile_material(mat)
          save."""
        params = {"material_path": material_path, "nodes": nodes or [],
                  "connections": connections or [], "clear_first": clear_first}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # cleanup_material_graph -- prune unused nodes (dry_run default)        #
    # ------------------------------------------------------------------ #
    _CLEANUP_BODY = _MG_HELPERS + r'''
mat_path = PARAMS["material_path"]
mat, err = _load_material(mat_path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    dry = PARAMS.get("dry_run")
    dry = True if dry is None else bool(dry)
    reachable, allnames = _reachable_names(mat)
    removable = [n for n in allnames if n not in reachable]
    if dry:
        preview = []
        for e in (_MEL.get_material_expressions(mat) or []):
            if e.get_name() in removable:
                preview.append({"node_name": e.get_name(), "node_class": e.get_class().get_name()})
        print("@@UMCP@@" + json.dumps({"status": "success", "dry_run": True, "material": mat.get_name(),
            "asset_path": mat_path, "removable_count": len(removable),
            "removable": preview, "num_expressions": _MEL.get_num_material_expressions(mat),
            "note": "dry_run computed the removable set via reachability from material outputs; nothing changed (not ledgered)"}))
    else:
        # capture full state of EVERY current node BEFORE the real delete, then diff to find what UE removed
        cap_by_name = {}
        for e in (_MEL.get_material_expressions(mat) or []):
            cap_by_name[e.get_name()] = _capture_node(mat, e)
        before = set(cap_by_name.keys())
        with unreal.ScopedEditorTransaction("MCP cleanup_material_graph"):
            _MEL.delete_unused_expressions(mat)
            _MEL.recompile_material(mat)
        _save(mat_path)
        after = set(x.get_name() for x in (_MEL.get_material_expressions(mat) or []))
        removed_names = sorted(before - after)
        removed_caps = [cap_by_name[n] for n in removed_names if n in cap_by_name]
        if not removed_caps:
            print("@@UMCP@@" + json.dumps({"status": "success", "dry_run": False, "material": mat.get_name(),
                "asset_path": mat_path, "removed_count": 0, "removed": [],
                "note": "no unused expressions to remove; nothing changed (not ledgered)",
                "num_expressions": _MEL.get_num_material_expressions(mat)}))
        else:
            _ledger().append({"op": "cleanup_material_graph", "asset_path": mat_path,
                "removed_nodes": removed_caps})
            print("@@UMCP@@" + json.dumps({"status": "success", "dry_run": False, "material": mat.get_name(),
                "asset_path": mat_path, "removed_count": len(removed_caps),
                "removed": [{"node_name": c["node_name"], "node_class": c["node_class"]} for c in removed_caps],
                "num_expressions": _MEL.get_num_material_expressions(mat),
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def cleanup_material_graph(ctx, material_path: str, dry_run: bool = True) -> str:
        """Delete expression nodes not reachable from any material output (ledgered WRITE; dry_run
        default, reversible).

        material_path: object/package path of a base Material.
        dry_run:       True (DEFAULT) computes the removable set WITHOUT changing anything -- safe
                       preview. False actually prunes.

        dry_run=True: computes reachability from every EMaterialProperty input node through
        MEL.get_inputs_for_material_expression; nodes never reached are 'removable'. Returns the list;
        NOT ledgered, nothing mutated.
        dry_run=False: FULL-state captures every current node first (so whatever UE removes is
        recreatable), then MEL.delete_unused_expressions(material) inside a ScopedEditorTransaction +
        ONE recompile + save, then diffs node names to ledger exactly the removed nodes' captures.
        (Unused nodes only ever feed each other -- never a surviving node -- so restoring each removed
        node + its own_inputs fully restores their sub-graph; no consumer/material-property rewiring.)

        Ledgered op 'cleanup_material_graph' {asset_path, removed_nodes:[<full-state capture: node_name,
        node_class, node_class_path, node_pos, node_props, own_inputs>]}. Inverse (FAITHFUL; recreated
        nodes may get NEW names):
          mat=load
          with ScopedEditorTransaction:
            m={} ; for cap in removed_nodes:
               cls=getattr(unreal,cap.node_class) or unreal.load_class(None,cap.node_class_path)
               ne=MEL.create_material_expression(mat, cls, cap.node_pos[0], cap.node_pos[1])
               for k,v in cap.node_props: try ne.set_editor_property(k, _coerce(ne.get_editor_property(k), v))
               m[cap.node_name]=ne
            for cap in removed_nodes:
               for w in cap.own_inputs:
                  s=m.get(w.src_name) or _find(w.src_name)
                  if s: MEL.connect_material_expressions(s, w.out_name, m[cap.node_name], w.input)
            MEL.recompile_material(mat)
          save."""
        params = {"material_path": material_path, "dry_run": dry_run}
        try:
            return json.dumps(_exec(_CLEANUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NEW ledger ops for editor_level.undo to learn (see per-tool docstrings for the exact inverse).
    # All re-find expressions by stable UObject name via
    #   mat = EditorAssetLibrary.load_asset(asset_path)   # a base unreal.Material
    #   MEL = unreal.MaterialEditingLibrary
    #   def _find(m, nm): return next((e for e in (MEL.get_material_expressions(m) or []) if e and e.get_name()==nm), None)
    #   def _cls(cap):    return getattr(unreal, cap["node_class"], None) or unreal.load_class(None, cap["node_class_path"])
    #   def _prop(s):     return getattr(unreal.MaterialProperty, s if s.startswith("MP_") else "MP_"+s.upper())
    #   _coerce is identical to this module's helper (tagged {"__lincolor__"/"__color__"/"__name__"/"__object__"} or bare scalar/str).
    # Finish each inverse with MEL.recompile_material(mat) (EXCEPT move/layout/duplicate) +
    # EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False); mat=None.
    #
    # 1) op "delete_material_expression"  -> recreate node + rewire own_inputs + consumers + prop_consumers  (recompile)
    # 2) op "duplicate_material_expression" {new_name} -> MEL.delete_material_expression(mat, _find(new_name))  (NO recompile; was disconnected)
    # 3) op "move_material_expression" {node_name, prior_x, prior_y} -> set material_expression_editor_x/_y back  (NO recompile)
    # 4) op "layout_material_graph" {prior_positions} -> restore each node's material_expression_editor_x/_y  (NO recompile)
    # 5) op "build_material_graph" -> delete created + restore conn_priors + (if cleared_first) recreate cleared_nodes/matprops  (recompile)
    # 6) op "cleanup_material_graph" {removed_nodes} -> recreate each + rewire own_inputs among them  (recompile)
    #
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # ------------------------------------------------------------------ #
