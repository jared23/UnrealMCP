"""UserTools :: Materials (DISCOVERY -- expression-type + material-function catalog)  (spec: docs/spec/materials.md)

Pure reflection / AssetRegistry reads over Unreal's public Python API (UE 5.8.x). Answers "what kinds of
material expression nodes exist, and what pins/properties does each have" and "what MaterialFunction
assets are in the project" WITHOUT mutating any real asset.

get_expression_type_info builds a THROWAWAY expression on a TRANSIENT material (unreal.new_object(
unreal.Material) -- never saved, never an asset, GC'd at end of call), reads its pins/properties, then
discards it. No /Game asset is created and there is no ledger op.

Query convention, base64 PARAMS injection and Output-Log auto-capture are copied VERBATIM from the
gold-standard editor_level.py / materials_read2.py.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies below contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never
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
_MVT = {1: "MCT_Float1", 2: "MCT_Float2", 3: "MCT_Float2", 4: "MCT_Float3", 8: "MCT_Float4",
        15: "MCT_Float", 16: "MCT_Texture2D", 32: "MCT_TextureCube", 64: "MCT_Texture2DArray",
        128: "MCT_VolumeTexture", 256: "MCT_StaticBool", 512: "MCT_Bool"}
def _vt(i):
    try:
        i = int(i)
    except Exception:
        return str(i)
    u = i & 4294967295
    return _MVT.get(u, "MCT(0x%X)" % u)
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _emit(obj):
    print("@@UMCP@@" + json.dumps(obj))
def _resolve_expr_class(name):
    base = unreal.MaterialExpression
    for cand in (name, "MaterialExpression" + name):
        obj = getattr(unreal, cand, None)
        if isinstance(obj, type):
            try:
                if issubclass(obj, base):
                    return cand, obj
            except Exception:
                pass
    return None, None
def _expr_prop_names(inst):
    # reflected UPROPERTYs surface as non-callable, non-underscore attributes on the instance
    out = []
    for nm in dir(inst):
        if nm.startswith("_"):
            continue
        v = getattr(inst, nm, None)
        if callable(v):
            continue
        out.append(nm)
    return out
'''

    # ------------------------------------------------------------------ #
    # get_expression_type_info                                           #
    # ------------------------------------------------------------------ #
    _TYPE_INFO_BODY = _HELP + r'''
name = PARAMS["expression_class"]
cand, cls = _resolve_expr_class(name)
if cls is None:
    _emit({"status": "error", "message": "no unreal.MaterialExpression subclass matching '%s'" % name})
else:
    tm = _try(lambda: unreal.new_object(unreal.Material))
    expr = _try(lambda: _MEL.create_material_expression(tm, cls, 0, 0)) if tm is not None else None
    if expr is None:
        _emit({"status": "error", "message": "could not instantiate %s on a transient material" % cand})
    else:
        innames = [str(x) for x in (_try(lambda: list(_MEL.get_material_expression_input_names(expr)), []) or [])]
        outnames = [str(x) for x in (_try(lambda: list(_MEL.get_material_expression_output_names(expr)), []) or [])]
        itypes = _try(lambda: list(_MEL.get_material_expression_input_types(expr)), []) or []
        inputs = []
        for i in range(len(innames)):
            t = itypes[i] if i < len(itypes) else None
            inputs.append({"name": innames[i], "type": (_vt(t) if t is not None else None),
                           "type_raw": (int(t) & 4294967295 if t is not None else None)})
        props = []
        for pn in _expr_prop_names(expr):
            val = _try(lambda: getattr(expr, pn))
            try:
                json.dumps(val)
                jv = val
            except Exception:
                jv = str(val)
            props.append({"name": pn, "default": jv})
        parents = []
        try:
            b = cls.__bases__[0]
            while b is not None and b.__name__.startswith("MaterialExpression") and b.__name__ != cand:
                parents.append(b.__name__)
                if b.__name__ == "MaterialExpression":
                    break
                b = b.__bases__[0]
        except Exception:
            pass
        _emit({"status": "success", "expression_class": cand,
               "inputs": inputs, "input_names": innames, "output_names": outnames,
               "input_count": len(innames), "output_count": len(outnames),
               "property_count": len(props), "properties": props,
               "parent_classes": parents,
               "note": "instantiated on a throwaway transient material (never saved); discarded after read"})
        # discard the throwaway
        _try(lambda: _MEL.delete_material_expression(tm, expr))
    tm = None
    expr = None
gc.collect()
'''

    @mcp.tool()
    def get_expression_type_info(ctx, expression_class: str) -> str:
        """Pins + reflected properties of a material-expression CLASS. READ-ONLY (no asset touched).

        expression_class: class name with or without the 'MaterialExpression' prefix
                          (e.g. 'Constant3Vector', 'MaterialExpressionTextureSample').

        Resolves the unreal.MaterialExpression subclass via reflection, instantiates ONE throwaway
        instance on a transient (never-saved) material, and reads get_material_expression_input_names /
        _output_names / _input_types plus the reflected editor properties (name + default value), then
        discards the instance. Returns inputs [{name, type, type_raw}], output_names, properties
        [{name, default}] and the parent_classes chain. No /Game asset is created; no ledger."""
        try:
            return json.dumps(_exec(_TYPE_INFO_BODY, {"expression_class": expression_class}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_material_expression_types                                     #
    # ------------------------------------------------------------------ #
    _LIST_TYPES_BODY = _HELP + r'''
flt = (PARAMS.get("filter") or "").lower()
base = unreal.MaterialExpression
types = []
for nm in dir(unreal):
    if not nm.startswith("MaterialExpression") or nm == "MaterialExpression":
        continue
    obj = getattr(unreal, nm, None)
    if not isinstance(obj, type):
        continue
    ok = _try(lambda: issubclass(obj, base), False)
    if not ok:
        continue
    if flt and flt not in nm.lower():
        continue
    types.append({"class": nm, "short_name": nm[len("MaterialExpression"):]})
types.sort(key=lambda d: d["class"])
_emit({"status": "success", "filter": (PARAMS.get("filter") or ""), "count": len(types), "types": types,
       "note": "per-type pins + properties via get_expression_type_info(<class>)"})
gc.collect()
'''

    @mcp.tool()
    def list_material_expression_types(ctx, filter: str = "") -> str:
        """Catalog every material-expression node class available via reflection. READ-ONLY.

        filter: optional case-insensitive substring on the class name (e.g. 'texture', 'constant').

        Enumerates unreal.MaterialExpression subclasses (the abstract base itself is excluded). Returns
        types [{class, short_name}] sorted by class name plus a count. This list is intentionally
        lightweight (no instantiation); call get_expression_type_info(class) for a specific type's pins
        and properties. Mutates nothing; no ledger."""
        try:
            return json.dumps(_exec(_LIST_TYPES_BODY, {"filter": filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # search_material_functions                                          #
    # ------------------------------------------------------------------ #
    _SEARCH_FN_BODY = _HELP + r'''
name_filter = (PARAMS.get("name_filter") or "").lower()
include_engine = bool(PARAMS.get("include_engine"))
path = (PARAMS.get("path") or "").strip()
cap = int(PARAMS.get("cap") or 500)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
assets = _try(lambda: list(ar.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "MaterialFunction"), True)), []) or []
funcs = []
total = 0
for a in assets:
    pkg = str(a.package_name)
    nm = str(a.asset_name)
    if not include_engine and (pkg.startswith("/Engine/") or pkg.startswith("/Script/")):
        continue
    if path and not pkg.startswith(path):
        continue
    if name_filter and name_filter not in nm.lower() and name_filter not in pkg.lower():
        continue
    total += 1
    if len(funcs) < cap:
        cls = _try(lambda: str(a.asset_class_path.asset_name))
        funcs.append({"name": nm, "path": pkg, "class": cls})
_emit({"status": "success", "name_filter": (PARAMS.get("name_filter") or ""),
       "include_engine": include_engine, "path": path,
       "match_count": total, "returned": len(funcs), "truncated": total > len(funcs),
       "functions": funcs})
gc.collect()
'''

    @mcp.tool()
    def search_material_functions(ctx, name_filter: str = "", include_engine: bool = False,
                                  path: str = "") -> str:
        """Find MaterialFunction assets via the AssetRegistry. READ-ONLY.

        name_filter:    optional case-insensitive substring matched against asset name or package path.
        include_engine: include /Engine and /Script functions (default False -> project content only).
                        The engine ships ~1000+ MaterialFunctions, so leave this off unless you need them.
        path:           optional package-path prefix to restrict the search (e.g. '/Game/Materials').

        Queries the AssetRegistry for MaterialFunction assets (search_sub_classes=True, so
        MaterialFunctionInstance etc. are included). Returns functions [{name, path, class}], match_count
        and a truncated flag (results are capped at 500). Mutates nothing; no ledger."""
        try:
            params = {"name_filter": name_filter, "include_engine": include_engine, "path": path}
            return json.dumps(_exec(_SEARCH_FN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
