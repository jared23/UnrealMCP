"""UserTools :: Materials (WRITE3)  (spec: docs/spec/materials.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). Third materials WRITE wave.
Fills the genuinely-MISSING parity gaps that material_write.py / material_graph_write.py /
materials_assign.py / materials_write2.py (prior wave) do NOT cover: MaterialFunction + MPC asset
creation and MPC parameter authoring. Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py / material_write.py / niagara_write2.py.

REACHABLE from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1) -- shipped here:
  * create_material_function -- AssetTools.create_asset(..., MaterialFunction, MaterialFunctionFactoryNew).
      Non-modal (the scripting create_asset path never raises ConfigureProperties -- verified live).
      Ledgered generic "create_asset"; inverse = close editors + delete asset (+ rmdir).
  * create_material_parameter_collection -- AssetTools.create_asset(..., MaterialParameterCollection,
      MaterialParameterCollectionFactoryNew). Non-modal (verified live). Ledgered generic "create_asset".
  * set_material_parameter_collection_parameter -- add or update one Scalar/Vector entry on an MPC via
      the asset's ScalarParameters / VectorParameters editor-property arrays (new entries get a fresh
      GUID). Captures existed + prior default. Inverse: if existed -> restore prior default; else remove.
  * delete_material_parameter_collection_parameter -- remove one Scalar/Vector entry from an MPC.
      Captures the removed entry (default value + guid). Inverse: re-add it. Reversible.

WIRED to C++ (hasattr-guarded -- inert until the plugin DLL is rebuilt, then AUTO-ENABLES):
  * add_material_comments -- material graph comment nodes (UMaterialExpressionComment size/text/color) live
      in the material's expression collection EditorComments (NOT the expressions list), which stock Python +
      MaterialEditingLibrary cannot author -- so this calls MCPReflectionLibrary.add_material_comment_json
      (MCPReflection_Materials.cpp). Reversible: inverse = remove_material_comment_json(path, comment_name).

NOTE: set_material_instance_parent / clear_material_instance_parameter / set_material_instance_static_switch /
set_material_render_property / get_material_instance_parameters / get_material_render_properties already
ship in materials_write2.py; NOT duplicated here.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Creation
reuses the coordinator's generic "create_asset" inverse. ONE new ledger op "set_mpc_param" (covers both
the setter and the delete via a "deleted" flag) is introduced; its inverse is documented per-tool + in
the batch report for the coordinator to fold into editor_level.undo.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign
a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
success/user_code/code_obj (the C++ wrapper's own names). One material recompile per execute_python call
max -- none of these tools recompile a material graph.
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
def _load_typed(path, tyname):
    if not path:
        return None, "no asset path given"
    obj = _load(path)
    if obj is None:
        return None, "asset not found: %s" % path
    cn = obj.get_class().get_name()
    if cn != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, cn, path)
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
'''

    # ------------------------------------------------------------------ #
    # create_material_function                                            #
    # ------------------------------------------------------------------ #
    _CREATE_MF_BODY = _HELP + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    mf = at.create_asset(name, package_path, unreal.MaterialFunction, unreal.MaterialFunctionFactoryNew())
    if mf is None or not isinstance(mf, unreal.MaterialFunction):
        if created_dir and EAL.does_directory_exist(package_path):
            _try(lambda: EAL.delete_directory(package_path))
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(mf).__name__, asset_path)}))
    else:
        if PARAMS.get("description"):
            _try(lambda: mf.set_editor_property("description", PARAMS["description"]))
        _close_editors(mf)
        _save(asset_path)
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
            "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": mf.get_name(),
            "asset_path": asset_path, "object_path": mf.get_path_name(),
            "class": mf.get_class().get_name(), "created_dir": created_dir,
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def create_material_function(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                                 description: str = None) -> str:
        """Create a new (empty) MaterialFunction asset non-interactively (NO modal). Ledgered write.

        name:         asset name for the new material function (e.g. 'MF_Blend').
        package_path: content directory (default '/Game/MCP_Scratch'); intermediate folders created.
        description:  optional Description text set on the function.

        Uses AssetTools.create_asset(..., unreal.MaterialFunction, unreal.MaterialFunctionFactoryNew()).
        The scripting create path never raises the ConfigureProperties dialog (verified live). Creates
        an EMPTY function -- input/output pin + graph authoring needs the MaterialEditor (deferred to C++).
        Saved on creation.

        Ledgered generic op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close editors,
        delete the asset, and rmdir package_path if we created it and it is now empty (same generic inverse
        as create_blueprint / create_material_instance)."""
        params = {"name": name, "package_path": package_path, "description": description}
        try:
            return json.dumps(_exec(_CREATE_MF_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_material_parameter_collection                                #
    # ------------------------------------------------------------------ #
    _CREATE_MPC_BODY = _HELP + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    mpc = at.create_asset(name, package_path, unreal.MaterialParameterCollection,
                          unreal.MaterialParameterCollectionFactoryNew())
    if mpc is None or not isinstance(mpc, unreal.MaterialParameterCollection):
        if created_dir and EAL.does_directory_exist(package_path):
            _try(lambda: EAL.delete_directory(package_path))
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(mpc).__name__, asset_path)}))
    else:
        _close_editors(mpc)
        _save(asset_path)
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
            "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": mpc.get_name(),
            "asset_path": asset_path, "object_path": mpc.get_path_name(),
            "class": mpc.get_class().get_name(), "created_dir": created_dir,
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def create_material_parameter_collection(ctx, name: str,
                                             package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new (empty) MaterialParameterCollection (MPC) asset non-interactively. Ledgered write.

        name:         asset name (e.g. 'MPC_Global').
        package_path: content directory (default '/Game/MCP_Scratch'); intermediate folders created.

        Uses AssetTools.create_asset(..., unreal.MaterialParameterCollection,
        unreal.MaterialParameterCollectionFactoryNew()) -- non-modal (verified live). Add scalar/vector
        entries with set_material_parameter_collection_parameter. Saved on creation.

        Ledgered generic op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close editors,
        delete the asset, rmdir package_path if created + now empty."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_MPC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_material_parameter_collection_parameter                         #
    # ------------------------------------------------------------------ #
    _SET_MPC_BODY = _HELP + r'''
mpc_path = PARAMS["mpc_path"]
kind = (PARAMS["param_kind"] or "").lower()
nm = PARAMS["parameter_name"]
mpc, err = _load_typed(mpc_path, "MaterialParameterCollection")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif kind not in ("scalar", "vector"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "param_kind must be scalar|vector, got %s" % kind}))
else:
    prop = "scalar_parameters" if kind == "scalar" else "vector_parameters"
    arr = _try(lambda: list(mpc.get_editor_property(prop)), []) or []
    idx = -1
    for i in range(len(arr)):
        if str(_try(lambda: arr[i].get_editor_property("parameter_name"))) == nm:
            idx = i
            break
    existed = idx >= 0
    prior_value = None
    if kind == "scalar":
        newval = float(PARAMS["value"])
        if existed:
            prior_value = _try(lambda: arr[idx].get_editor_property("default_value"))
            arr[idx].set_editor_property("default_value", newval)
        else:
            entry = unreal.CollectionScalarParameter()
            entry.set_editor_property("parameter_name", nm)
            entry.set_editor_property("default_value", newval)
            arr.append(entry)  # ctor auto-assigns a fresh Id GUID (protected field, not Python-settable)
        shown = newval
    else:
        v = PARAMS["value"]
        lc = unreal.LinearColor(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
        if existed:
            c = _try(lambda: arr[idx].get_editor_property("default_value"))
            prior_value = [c.r, c.g, c.b, c.a] if c is not None else None
            arr[idx].set_editor_property("default_value", lc)
        else:
            entry = unreal.CollectionVectorParameter()
            entry.set_editor_property("parameter_name", nm)
            entry.set_editor_property("default_value", lc)
            arr.append(entry)  # ctor auto-assigns a fresh Id GUID (protected field, not Python-settable)
        shown = [lc.r, lc.g, lc.b, lc.a]
    with unreal.ScopedEditorTransaction("MCP set_mpc_param"):
        mpc.set_editor_property(prop, arr)
    _save(mpc_path)
    _ledger().append({"op": "set_mpc_param", "asset_path": mpc_path, "object_path": mpc.get_path_name(),
        "param_kind": kind, "parameter_name": nm, "existed": existed, "prior_value": prior_value,
        "deleted": False})
    print("@@UMCP@@" + json.dumps({"status": "success", "mpc": mpc.get_name(), "param_kind": kind,
        "parameter_name": nm, "existed": existed, "prior_value": prior_value, "new_value": shown,
        "count": len(arr), "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def set_material_parameter_collection_parameter(ctx, mpc_path: str, param_kind: str,
                                                    parameter_name: str, value) -> str:
        """Add or update one Scalar or Vector entry on a MaterialParameterCollection. Ledgered write.

        mpc_path:       object/package path of a MaterialParameterCollection.
        param_kind:     'scalar' | 'vector'.
        parameter_name: entry name (added if new, updated in place if it exists).
        value:          scalar -> a float; vector -> [r,g,b] or [r,g,b,a] (alpha defaults 1.0).

        Edits the asset's ScalarParameters / VectorParameters array (new entries get a fresh GUID) inside a
        ScopedEditorTransaction, then saves. Captures whether the entry existed + its prior default.

        Ledgered op 'set_mpc_param' {asset_path, object_path, param_kind, parameter_name, existed,
        prior_value, deleted:False}. Inverse (FAITHFUL): if existed -> restore prior_value on that entry;
        else -> remove the entry by name."""
        if param_kind and param_kind.lower() == "vector":
            v = list(value or [])
            if len(v) == 3:
                v = v + [1.0]
            if len(v) != 4:
                return json.dumps({"status": "error",
                    "message": "vector value must be [r,g,b] or [r,g,b,a], got %r" % (value,)}, indent=2)
            value = v
        params = {"mpc_path": mpc_path, "param_kind": param_kind,
                  "parameter_name": parameter_name, "value": value}
        try:
            return json.dumps(_exec(_SET_MPC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_material_parameter_collection_parameter                      #
    # ------------------------------------------------------------------ #
    _DEL_MPC_BODY = _HELP + r'''
mpc_path = PARAMS["mpc_path"]
nm = PARAMS["parameter_name"]
kind = PARAMS.get("param_kind")
mpc, err = _load_typed(mpc_path, "MaterialParameterCollection")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    kinds = [kind] if kind in ("scalar", "vector") else ["scalar", "vector"]
    found_kind = None
    removed = None
    for k in kinds:
        prop = "scalar_parameters" if k == "scalar" else "vector_parameters"
        arr = _try(lambda: list(mpc.get_editor_property(prop)), []) or []
        idx = -1
        for i in range(len(arr)):
            if str(_try(lambda: arr[i].get_editor_property("parameter_name"))) == nm:
                idx = i
                break
        if idx >= 0:
            e = arr[idx]
            if k == "scalar":
                removed = {"default_value": _try(lambda: e.get_editor_property("default_value"))}
            else:
                c = _try(lambda: e.get_editor_property("default_value"))
                removed = {"default_value": ([c.r, c.g, c.b, c.a] if c is not None else None)}
            del arr[idx]
            with unreal.ScopedEditorTransaction("MCP delete_mpc_param"):
                mpc.set_editor_property(prop, arr)
            found_kind = k
            break
    if found_kind is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no scalar/vector parameter named '%s' on MPC" % nm}))
    else:
        _save(mpc_path)
        _ledger().append({"op": "set_mpc_param", "asset_path": mpc_path, "object_path": mpc.get_path_name(),
            "param_kind": found_kind, "parameter_name": nm, "existed": True,
            "prior_value": removed.get("default_value"), "deleted": True})
        print("@@UMCP@@" + json.dumps({"status": "success", "mpc": mpc.get_name(),
            "param_kind": found_kind, "parameter_name": nm, "removed": removed,
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def delete_material_parameter_collection_parameter(ctx, mpc_path: str, parameter_name: str,
                                                       param_kind: str = None) -> str:
        """Remove one Scalar or Vector entry from a MaterialParameterCollection. Ledgered write.

        mpc_path:       object/package path of a MaterialParameterCollection.
        parameter_name: entry name to remove.
        param_kind:     'scalar' | 'vector'. Optional -- both arrays are searched if omitted.

        Removes the matching entry from the ScalarParameters / VectorParameters array inside a
        ScopedEditorTransaction, then saves. Captures the removed entry's default value.

        Ledgered op 'set_mpc_param' {asset_path, object_path, param_kind, parameter_name, existed:True,
        prior_value, deleted:True}. Inverse (FAITHFUL, name+default): re-add an entry of param_kind with
        parameter_name + prior_value. NB the FGuid Id is a protected field (not Python-settable); a
        re-added entry receives a fresh auto-generated GUID, which is functionally equivalent."""
        params = {"mpc_path": mpc_path, "parameter_name": parameter_name, "param_kind": param_kind}
        try:
            return json.dumps(_exec(_DEL_MPC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_material_comments -- WIRED to the C++ handler add_material_comment_json                     #
    # (MCPReflection_Materials.cpp). hasattr-guarded so it stays inert (honest fallback) until the    #
    # plugin DLL is rebuilt with that handler, then AUTO-ENABLES. Reversible.                         #
    # ================================================================== #
    _ADD_COMMENT_BODY = _HELP + r'''
rl = getattr(unreal, "MCPReflectionLibrary", None)
if rl is None or not hasattr(rl, "add_material_comment_json"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "error": "add_material_comments requires the C++ handler add_material_comment_json (deferred to the batched C++ round; rebuild the UnrealMCP plugin DLL with MCPReflection_Materials.cpp to enable it)"}))
else:
    raw = rl.add_material_comment_json(PARAMS["material_path"], PARAMS.get("text") or "",
        int(PARAMS.get("x", 0)), int(PARAMS.get("y", 0)),
        int(PARAMS.get("size_x", 400)), int(PARAMS.get("size_y", 200)),
        PARAMS.get("color") or "")
    res = raw if isinstance(raw, dict) else json.loads(raw)
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save(PARAMS["material_path"])
        _ledger().append({"op": "add_material_comment", "asset_path": PARAMS["material_path"],
            "object_path": res.get("material_path"), "comment_name": res.get("comment_name"),
            "index": res.get("index")})
        print("@@UMCP@@" + json.dumps({"status": "success", "comment_name": res.get("comment_name"),
            "index": res.get("index"), "comment_count": res.get("comment_count"),
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def add_material_comments(ctx, material_path: str, text: str, x: int = 0, y: int = 0,
                              size_x: int = 400, size_y: int = 200, color=None) -> str:
        """Add a comment box node to a BASE Material graph. Ledgered, reversible write.

        material_path: object/package path of a base Material (UMaterial -- not an instance/function).
        text:          comment text.
        x, y:          graph position (MaterialExpressionEditorX/Y).
        size_x, size_y:comment box size (default 400x100 if <=0).
        color:         optional [r,g,b] or [r,g,b,a] linear color for the box (default engine white).

        A material graph comment (UMaterialExpressionComment) lives in the material's expression
        collection EditorComments (NOT the expressions list), which stock Python + MaterialEditingLibrary
        cannot author, so this calls the C++ handler MCPReflectionLibrary.add_material_comment_json. The
        comment persists and shows in the Material editor on next open (the graph is rebuilt from
        EditorComments). Saved after the add.

        Ledgered op 'add_material_comment' {asset_path, object_path, comment_name, index}. Inverse
        (FAITHFUL): MCPReflectionLibrary.remove_material_comment_json(asset_path, comment_name) -- removes
        that comment from EditorComments. hasattr-guarded (inert until the plugin DLL is rebuilt)."""
        color_str = ""
        if color:
            c = list(color)
            if len(c) == 3:
                c = c + [1.0]
            if len(c) >= 4:
                color_str = ",".join(str(float(v)) for v in c[:4])
        params = {"material_path": material_path, "text": text, "x": x, "y": y,
                  "size_x": size_x, "size_y": size_y, "color": color_str}
        try:
            return json.dumps(_exec(_ADD_COMMENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
