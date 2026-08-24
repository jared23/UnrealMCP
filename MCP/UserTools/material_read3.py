"""UserTools :: Materials (READ3 -- reads/compile/stats + graph validation)  (spec: docs/spec/materials.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.x). READ / COMPILE-only counterpart
to materials_write3.py. Query convention, base64 PARAMS injection and Output-Log auto-capture are copied
VERBATIM from the gold-standard editor_level.py / materials_read2.py.

Everything here is driven by unreal.MaterialEditingLibrary (MEL), which is fully reflected into Python
(every method is a UFUNCTION). MEL is stateless -- it operates on a live UMaterial that we hold loaded
for the duration of the call, so the returned UMaterialExpression pointers stay valid.

SIDE EFFECTS / LEDGER:
  * The pure reads -- get_material_property_connections, get_available_material_pins,
    trace_material_connection, material_stats -- mutate NOTHING and register NO ledger op / NO undo.
  * recompile_material / get_material_errors / validate_material_graph call
    MEL.recompile_material(mat): this triggers a shader recompile (transient GPU work) but does not
    modify the asset on disk and registers NO ledger op (there is nothing to undo -- a recompile is
    idempotent). Documented on each tool.
  * apply_material additionally calls EditorAssetLibrary.save_asset (writes the .uasset) +
    MEL.refresh_material_editor. save is not itself undoable; still no ledger op is registered.

CONCURRENCY NOTE: exactly ONE MEL.recompile_material runs per execute_python call -- batching several
recompiles into one snippet wedges the game thread. Each tool below issues at most one recompile.

HEADLESS CAVEAT: MEL.get_statistics(mat) returns an FMaterialStatistics whose instruction/sampler
counters populate only while that material's editor window is OPEN. In a headless editor they read 0.
list_shaders / get_num_shader_types return real data regardless. material_stats documents this.

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
# EMaterialValueType is NOT reflected as unreal.MaterialValueType; map the stable low float flags and
# fall back to a hex label for texture / bool / composite bitmasks (which shift across engine versions).
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
def _load(path):
    return _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
def _mat_props():
    skip = ("MP_MAX", "MP_LAST_CUSTOMIZED_U_VS")
    return [p for p in dir(unreal.MaterialProperty) if p.startswith("MP_") and p not in skip]
def _emit(obj):
    print("@@UMCP@@" + json.dumps(obj))
def _not_material(mat, path):
    if mat is None:
        _emit({"status": "error", "message": "asset not found: %s" % path}); return True
    if not isinstance(mat, unreal.Material):
        _emit({"status": "error", "message": "asset is not a Material (got %s): %s" % (mat.get_class().get_name(), path)}); return True
    return False
'''

    # ------------------------------------------------------------------ #
    # get_material_property_connections                                   #
    # ------------------------------------------------------------------ #
    _PROP_CONN_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if not _not_material(mat, path):
    conns = []
    for pn in _mat_props():
        prop = getattr(unreal.MaterialProperty, pn)
        node = _try(lambda: _MEL.get_material_property_input_node(mat, prop))
        if node is None:
            continue
        oname = _try(lambda: _MEL.get_material_property_input_node_output_name(mat, prop), "")
        disp = _try(lambda: str(prop.get_display_name()))
        conns.append({"property": pn, "display_name": disp, "node": node.get_name(),
                      "node_class": node.get_class().get_name(),
                      "output": (str(oname) if oname else "")})
    _emit({"status": "success", "material": mat.get_name(), "object_path": mat.get_path_name(),
           "connected_count": len(conns), "connections": conns})
gc.collect()
'''

    @mcp.tool()
    def get_material_property_connections(ctx, material_path: str) -> str:
        """What expression feeds each material output (BaseColor, Roughness, Normal, ...). READ-ONLY.

        material_path: object/package path of a Material (base material, not an instance).

        Loops the EMaterialProperty members and calls MaterialEditingLibrary.get_material_property_
        input_node (+ _output_name) for each. Returns connections [{property, display_name, node,
        node_class, output}] only for outputs that actually have a node wired in (unconnected material
        outputs are omitted). Mutates nothing; no ledger."""
        try:
            return json.dumps(_exec(_PROP_CONN_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_available_material_pins                                         #
    # ------------------------------------------------------------------ #
    _PINS_BODY = _HELP + r'''
path = PARAMS["material_path"]
want = PARAMS["node_name"]
mat = _load(path)
if not _not_material(mat, path):
    exprs = _try(lambda: list(_MEL.get_material_expressions(mat)), []) or []
    found = None
    for e in exprs:
        if e is not None and e.get_name() == want:
            found = e
            break
    if found is None:
        _emit({"status": "error", "message": "no expression named '%s' in material" % want,
               "available": [e.get_name() for e in exprs if e is not None]})
    else:
        innames = [str(x) for x in (_try(lambda: list(_MEL.get_material_expression_input_names(found)), []) or [])]
        outnames = [str(x) for x in (_try(lambda: list(_MEL.get_material_expression_output_names(found)), []) or [])]
        itypes = _try(lambda: list(_MEL.get_material_expression_input_types(found)), []) or []
        inputs = []
        for i in range(len(innames)):
            t = itypes[i] if i < len(itypes) else None
            inputs.append({"name": innames[i], "type": (_vt(t) if t is not None else None),
                           "type_raw": (int(t) & 4294967295 if t is not None else None)})
        _emit({"status": "success", "material": mat.get_name(), "node": found.get_name(),
               "node_class": found.get_class().get_name(), "inputs": inputs,
               "input_names": innames, "output_names": outnames,
               "input_count": len(innames), "output_count": len(outnames)})
    exprs = None
    found = None
gc.collect()
'''

    @mcp.tool()
    def get_available_material_pins(ctx, material_path: str, node_name: str) -> str:
        """Input + output pins (and input value-types) of one expression node. READ-ONLY.

        material_path: object/package path of a Material.
        node_name:     internal name of the expression (as returned by get_material_graph_nodes).

        Uses MaterialEditingLibrary.get_material_expression_input_names / _output_names / _input_types.
        Returns inputs [{name, type, type_raw}] paired by index, plus flat input_names / output_names.
        'type' is a best-effort EMaterialValueType label (MCT_Float, MCT_Texture2D, ...); the exact
        enum is not reflected into Python, so texture/bool/composite masks fall back to a hex label and
        the raw int is always given in type_raw. Mutates nothing; no ledger."""
        try:
            return json.dumps(_exec(_PINS_BODY, {"material_path": material_path, "node_name": node_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # trace_material_connection                                           #
    # ------------------------------------------------------------------ #
    _TRACE_BODY = _HELP + r'''
path = PARAMS["material_path"]
start = PARAMS["node_name"]
direction = (PARAMS.get("direction") or "upstream").lower()
max_depth = int(PARAMS.get("max_depth") or 8)
mat = _load(path)
if not _not_material(mat, path):
    exprs = _try(lambda: list(_MEL.get_material_expressions(mat)), []) or []
    names = set(e.get_name() for e in exprs if e is not None)
    # upstream adjacency: node -> [input node names]
    adj_up = {}
    for e in exprs:
        if e is None:
            continue
        ins = _try(lambda: list(_MEL.get_inputs_for_material_expression(mat, e)), []) or []
        adj_up[e.get_name()] = [i.get_name() for i in ins if i is not None]
    # downstream adjacency (reverse of adj_up) + material outputs as terminal consumers
    adj_down = {}
    for k, vs in adj_up.items():
        for v in vs:
            adj_down.setdefault(v, []).append(k)
    for pn in _mat_props():
        prop = getattr(unreal.MaterialProperty, pn)
        node = _try(lambda: _MEL.get_material_property_input_node(mat, prop))
        if node is not None:
            adj_down.setdefault(node.get_name(), []).append("Material." + pn)
    if start not in names:
        _emit({"status": "error", "message": "no expression named '%s' in material" % start,
               "available": sorted(names)})
    else:
        adj = adj_down if direction == "downstream" else adj_up
        edges = []
        visited = {start: 0}
        frontier = [start]
        depth = 0
        while frontier and depth < max_depth:
            nxt = []
            for cur in frontier:
                for nb in adj.get(cur, []):
                    edge = {"from": cur, "to": nb, "depth": depth + 1} if direction == "downstream" else {"from": nb, "to": cur, "depth": depth + 1}
                    edges.append(edge)
                    if nb not in visited:
                        visited[nb] = depth + 1
                        if nb in names:
                            nxt.append(nb)
            frontier = nxt
            depth += 1
        reached = [{"node": n, "depth": d} for n, d in sorted(visited.items(), key=lambda kv: (kv[1], kv[0])) if n != start]
        _emit({"status": "success", "material": mat.get_name(), "start": start,
               "direction": direction, "max_depth": max_depth,
               "reached_count": len(reached), "reached": reached, "edges": edges,
               "truncated": bool(frontier)})
    exprs = None
gc.collect()
'''

    @mcp.tool()
    def trace_material_connection(ctx, material_path: str, node_name: str,
                                  direction: str = "upstream", max_depth: int = 8) -> str:
        """Breadth-first trace of a node's connections through the material graph. READ-ONLY.

        material_path: object/package path of a Material.
        node_name:     internal name of the expression to trace from.
        direction:     'upstream' (nodes feeding this one, via get_inputs_for_material_expression) or
                       'downstream' (nodes/material-outputs this one feeds, via reverse scan). Default upstream.
        max_depth:     BFS depth bound (default 8).

        Returns reached [{node, depth}] and edges [{from, to, depth}]; downstream terminals include
        synthetic 'Material.MP_*' consumers for direct material-output wiring. 'truncated' is true if the
        depth bound cut the walk short. Mutates nothing; no ledger."""
        try:
            params = {"material_path": material_path, "node_name": node_name,
                      "direction": direction, "max_depth": max_depth}
            return json.dumps(_exec(_TRACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # material_stats                                                      #
    # ------------------------------------------------------------------ #
    _STATS_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if not _not_material(mat, path):
    st = _try(lambda: _MEL.get_statistics(mat))
    stats = {}
    if st is not None:
        raw = _try(lambda: st.to_dict(), {}) or {}
        for k, v in raw.items():
            stats[str(k)] = (v if isinstance(v, (int, float, bool)) else str(v))
    nshader = _try(lambda: _MEL.get_num_shader_types(mat))
    shaders = []
    for s in (_try(lambda: list(_MEL.list_shaders(mat)), []) or []):
        d = _try(lambda: s.to_dict(), {}) or {}
        shaders.append({str(k): str(v) for k, v in d.items()})
    _emit({"status": "success", "material": mat.get_name(), "object_path": mat.get_path_name(),
           "statistics": stats, "num_shader_types": nshader, "shader_count": len(shaders),
           "shaders": shaders,
           "note": "FMaterialStatistics instruction/sampler counters populate only when the material editor window is open for this asset; headless they read 0. Shader lists are always accurate."})
gc.collect()
'''

    @mcp.tool()
    def material_stats(ctx, material_path: str) -> str:
        """Shader/instruction statistics for a Material. READ-ONLY (no recompile).

        material_path: object/package path of a Material.

        Returns MaterialEditingLibrary.get_statistics(mat) (FMaterialStatistics: num_pixel_shader_
        instructions, num_vertex_shader_instructions, num_samplers, num_pixel/vertex_texture_samples,
        num_virtual_texture_samples, num_uv_scalars, num_interpolator_scalars) plus get_num_shader_types
        and list_shaders [{vertex_factory_name, shader_type_name}]. HEADLESS CAVEAT: the FMaterial-
        Statistics counters are 0 unless the material's editor window is open; the shader lists are
        always accurate. Mutates nothing; no ledger; no recompile."""
        try:
            return json.dumps(_exec(_STATS_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # recompile_material                                                  #
    # ------------------------------------------------------------------ #
    _RECOMPILE_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if not _not_material(mat, path):
    errs = _try(lambda: list(_MEL.recompile_material(mat)), None)
    if errs is None:
        _emit({"status": "error", "message": "recompile_material raised for %s" % path})
    else:
        errs = [str(x) for x in errs]
        _emit({"status": "success", "material": mat.get_name(), "object_path": mat.get_path_name(),
               "error_count": len(errs), "errors": errs,
               "side_effect": "triggered a shader recompile (transient GPU work; asset not modified on disk)"})
gc.collect()
'''

    @mcp.tool()
    def recompile_material(ctx, material_path: str) -> str:
        """Recompile a Material's shaders and return any compiler errors. COMPILE (no ledger).

        material_path: object/package path of a Material.

        Calls MaterialEditingLibrary.recompile_material(mat), whose return is a TArray<FString> of
        compiler error strings (empty = clean). SIDE EFFECT: triggers a shader recompile (transient GPU
        work); it does NOT modify the .uasset on disk and registers NO ledger op (a recompile is
        idempotent, nothing to undo). Issues exactly one recompile."""
        try:
            return json.dumps(_exec(_RECOMPILE_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_material_errors                                                 #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def get_material_errors(ctx, material_path: str) -> str:
        """Compiler errors for a Material. COMPILE side effect (no ledger).

        material_path: object/package path of a Material.

        Thin framing over MaterialEditingLibrary.recompile_material -- returns errors [] and error_count.
        SIDE EFFECT: obtaining the errors requires a recompile, so this DOES trigger one shader compile
        (transient GPU work, asset not modified on disk). No ledger op. Use material_stats for a pure,
        compile-free read."""
        try:
            r = _exec(_RECOMPILE_BODY, {"material_path": material_path})
            if isinstance(r, dict) and r.get("status") == "success":
                r["has_errors"] = r.get("error_count", 0) > 0
                r["side_effect"] = "recompiled shaders to obtain errors (asset not modified on disk)"
            return json.dumps(r, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_material_graph                                            #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if not _not_material(mat, path):
    errs = [str(x) for x in (_try(lambda: list(_MEL.recompile_material(mat)), []) or [])]
    exprs = _try(lambda: list(_MEL.get_material_expressions(mat)), []) or []
    names = set(e.get_name() for e in exprs if e is not None)
    adj_up = {}
    for e in exprs:
        if e is None:
            continue
        ins = _try(lambda: list(_MEL.get_inputs_for_material_expression(mat, e)), []) or []
        adj_up[e.get_name()] = [i.get_name() for i in ins if i is not None]
    # roots = nodes wired directly into a material property output
    roots = []
    for pn in _mat_props():
        prop = getattr(unreal.MaterialProperty, pn)
        node = _try(lambda: _MEL.get_material_property_input_node(mat, prop))
        if node is not None:
            roots.append(node.get_name())
    reachable = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in reachable or cur not in names:
            continue
        reachable.add(cur)
        for up in adj_up.get(cur, []):
            if up not in reachable:
                stack.append(up)
    orphans = sorted(names - reachable)
    _emit({"status": "success", "material": mat.get_name(), "object_path": mat.get_path_name(),
           "compile_error_count": len(errs), "compile_errors": errs,
           "node_count": len(names), "root_count": len(set(roots)),
           "reachable_count": len(reachable), "orphan_count": len(orphans), "orphans": orphans,
           "valid": (len(errs) == 0 and len(orphans) == 0),
           "side_effect": "recompiled shaders during validation (asset not modified on disk)"})
    exprs = None
gc.collect()
'''

    @mcp.tool()
    def validate_material_graph(ctx, material_path: str) -> str:
        """Recompile + orphan-node audit of a Material graph. COMPILE side effect (no ledger).

        material_path: object/package path of a Material.

        Recompiles the material (collecting compiler errors), then walks upstream from every node wired
        into a material-property output (get_material_property_input_node) via get_inputs_for_material_
        expression to compute the reachable set; nodes not reachable are reported as orphans. Returns
        compile_errors, node_count, reachable_count, orphans and a 'valid' flag (no errors AND no
        orphans). SIDE EFFECT: one shader recompile (asset not modified on disk). No ledger op."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # apply_material                                                     #
    # ------------------------------------------------------------------ #
    _APPLY_BODY = _HELP + r'''
path = PARAMS["material_path"]
mat = _load(path)
if not _not_material(mat, path):
    errs = [str(x) for x in (_try(lambda: list(_MEL.recompile_material(mat)), []) or [])]
    saved = bool(_try(lambda: unreal.EditorAssetLibrary.save_asset(path, True), False))
    refreshed = True
    try:
        _MEL.refresh_material_editor(mat)
    except Exception:
        refreshed = False
    _emit({"status": "success", "material": mat.get_name(), "object_path": mat.get_path_name(),
           "compile_error_count": len(errs), "compile_errors": errs,
           "saved": saved, "refreshed": refreshed,
           "side_effect": "recompiled shaders + saved the .uasset to disk + refreshed any open material editor"})
gc.collect()
'''

    @mcp.tool()
    def apply_material(ctx, material_path: str) -> str:
        """Recompile, save, and refresh a Material -- commit edits to disk. COMPILE + SAVE (no ledger).

        material_path: object/package path of a Material.

        Runs MaterialEditingLibrary.recompile_material, then EditorAssetLibrary.save_asset (writes the
        .uasset), then MaterialEditingLibrary.refresh_material_editor (repaints any open editor).
        Returns compile_errors, saved, refreshed. SIDE EFFECTS: one shader recompile AND a disk write of
        the asset. No ledger op is registered (a save is not an undoable transaction). Call after graph
        edits to persist + re-render them."""
        try:
            return json.dumps(_exec(_APPLY_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
