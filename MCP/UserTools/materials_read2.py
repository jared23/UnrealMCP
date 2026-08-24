"""UserTools :: Materials (READ2)  (spec: docs/spec/materials.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). Pure READ counterpart to
materials_write2.py. Query convention, base64 PARAMS injection and Output-Log auto-capture are copied
VERBATIM from the gold-standard editor_level.py / niagara_write2.py. These tools mutate nothing and
register no ledger op.

NOTE: get_material_instance_parameters + get_material_render_properties already ship in
materials_write2.py (prior wave); NOT duplicated here.

REACHABLE from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1) -- shipped here:
  * get_material_parameter_collection -- enumerate an MPC's ScalarParameters + VectorParameters arrays
      (name + default value + guid) via the asset's editor properties.
  * get_material_graph_nodes -- enumerate a Material's expressions (class, name, editor x/y) via
      MaterialEditingLibrary.get_material_expressions (returns live UMaterialExpression subobjects; the
      parent material is held loaded for the duration so the pointers stay valid).
  * get_material_expression_info -- detail for one named expression (class + editor pos + a small set of
      common scalar/string props read defensively).

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never
assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the C++ wrapper's own names).
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
import unreal, json, gc
_MEL = unreal.MaterialEditingLibrary
_GLOBAL = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load(path):
    return _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
'''

    # ------------------------------------------------------------------ #
    # get_material_parameter_collection                                   #
    # ------------------------------------------------------------------ #
    _GET_MPC_BODY = _HELP + r'''
path = PARAMS["mpc_path"]
mpc = _load(path)
if mpc is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif not isinstance(mpc, unreal.MaterialParameterCollection):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a MaterialParameterCollection (got %s): %s" % (mpc.get_class().get_name(), path)}))
else:
    sps = _try(lambda: list(mpc.get_editor_property("scalar_parameters")), []) or []
    vps = _try(lambda: list(mpc.get_editor_property("vector_parameters")), []) or []
    scal = []
    for e in sps:
        scal.append({"name": str(_try(lambda: e.get_editor_property("parameter_name"))),
                     "default_value": _try(lambda: e.get_editor_property("default_value"))})
    vec = []
    for e in vps:
        c = _try(lambda: e.get_editor_property("default_value"))
        vec.append({"name": str(_try(lambda: e.get_editor_property("parameter_name"))),
                    "default_value": ([c.r, c.g, c.b, c.a] if c is not None else None)})
    print("@@UMCP@@" + json.dumps({"status": "success", "mpc": mpc.get_name(),
        "object_path": mpc.get_path_name(), "scalar_parameters": scal, "vector_parameters": vec,
        "scalar_count": len(scal), "vector_count": len(vec)}))
gc.collect()
'''

    @mcp.tool()
    def get_material_parameter_collection(ctx, mpc_path: str) -> str:
        """Enumerate a MaterialParameterCollection's scalar + vector entries. READ-ONLY.

        mpc_path: object/package path of a MaterialParameterCollection asset.

        Returns scalar_parameters [{name, default_value(float)}] and vector_parameters
        [{name, default_value([r,g,b,a])}] read from the asset's ScalarParameters / VectorParameters
        arrays. (The per-entry FGuid Id is a protected field, not Python-readable, so it is omitted.)
        Companion to set_material_parameter_collection_parameter."""
        params = {"mpc_path": mpc_path}
        try:
            return json.dumps(_exec(_GET_MPC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_material_graph_nodes                                             #
    # ------------------------------------------------------------------ #
    _GET_NODES_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if mat is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif not isinstance(mat, unreal.Material):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a Material (got %s): %s" % (mat.get_class().get_name(), path)}))
else:
    exprs = _try(lambda: list(_MEL.get_material_expressions(mat)), []) or []
    nodes = []
    for e in exprs:
        if e is None:
            continue
        nodes.append({"name": e.get_name(), "class": e.get_class().get_name(),
                      "x": _try(lambda: e.get_editor_property("material_expression_editor_x")),
                      "y": _try(lambda: e.get_editor_property("material_expression_editor_y"))})
    print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
        "object_path": mat.get_path_name(), "node_count": len(nodes), "nodes": nodes}))
    exprs = None
gc.collect()
'''

    @mcp.tool()
    def get_material_graph_nodes(ctx, material_path: str) -> str:
        """Enumerate a Material's graph expression nodes. READ-ONLY.

        material_path: object/package path of a Material (base material, not an instance).

        Returns nodes [{name, class, x, y}] for every UMaterialExpression in the material graph, via
        MaterialEditingLibrary.get_material_expressions. The parent material is held loaded while the
        expression pointers are read, so they stay valid. For per-node detail use
        get_material_expression_info."""
        params = {"material_path": material_path}
        try:
            return json.dumps(_exec(_GET_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_material_expression_info                                         #
    # ------------------------------------------------------------------ #
    _GET_EXPR_BODY = _HELP + r'''
path = PARAMS["material_path"]
want = PARAMS["expression_name"]
mat = _load(path)
if mat is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif not isinstance(mat, unreal.Material):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a Material (got %s): %s" % (mat.get_class().get_name(), path)}))
else:
    exprs = _try(lambda: list(_MEL.get_material_expressions(mat)), []) or []
    found = None
    for e in exprs:
        if e is not None and e.get_name() == want:
            found = e
            break
    if found is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no expression named '%s' in material" % want,
            "available": [e.get_name() for e in exprs if e is not None]}))
    else:
        props = {}
        for pn in ("parameter_name", "default_value", "constant", "r", "g", "b", "a",
                   "desc", "material_expression_editor_x", "material_expression_editor_y"):
            v = _try(lambda: found.get_editor_property(pn))
            if v is not None:
                try:
                    json.dumps(v)
                    props[pn] = v
                except Exception:
                    props[pn] = str(v)
        print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
            "expression_name": found.get_name(), "class": found.get_class().get_name(),
            "properties": props}))
    exprs = None
    found = None
gc.collect()
'''

    @mcp.tool()
    def get_material_expression_info(ctx, material_path: str, expression_name: str) -> str:
        """Detail for one expression node in a Material graph. READ-ONLY.

        material_path:   object/package path of a Material.
        expression_name: internal name of the expression (as returned by get_material_graph_nodes).

        Returns {class, expression_name, properties} where properties is a defensively-read subset of
        common expression fields (parameter_name, default_value, constant, r/g/b/a, desc, editor x/y);
        only fields present on that expression class + JSON-encodable are included."""
        params = {"material_path": material_path, "expression_name": expression_name}
        try:
            return json.dumps(_exec(_GET_EXPR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
