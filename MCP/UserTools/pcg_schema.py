"""UserTools :: PCG GRAPH-PARAMETER SCHEMA + DYNAMIC-INPUT-PIN authoring  (spec: docs/spec/pcg.md — Wave 5)

The FINAL PCG slice. Thin Python wiring over the C++ handlers drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_PCG.cpp (block #48). Every tool is
resolve-guarded on a future unreal.MCPReflectionLibrary.<snake>_json method, so this module is INERT
until the plugin DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding
(base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied from the
gold-standard mutable_graph_cpp.py; the save mirrors pcg_compute.py._pcg_save.

WHY C++ (both legs are unreachable from Python):
  (A) SCHEMA. UPCGGraph.user_parameters is a reflected FInstancedPropertyBag, but
      unreal.InstancedPropertyBag exposes ONLY generic struct methods (import_text / export_text /
      to_dict / get / set_editor_property) -- NO add/remove/rename-PROPERTY surface, and NO way to
      ENUMERATE the descriptors. So defining/enumerating graph parameters is C++ only. (Wave 3's
      PCGGraphParametersHelpers already does typed VALUE get/set on the same bag; this module is SCHEMA
      only and never duplicates it.)
  (B) DYNAMIC PINS. UPCGSettingsWithDynamicInputs::OnUserAdd/RemoveDynamicInputPin are WITH_EDITOR
      PCG_API methods with no BlueprintCallable/Python surface. (PCGSubsystem is unbound too.)

  READS (no ledger -- non-mutating; work on ANY graph, not just scratch):
    * list_pcg_graph_parameters  -- enumerate the graph's user_parameters bag: [{name, type,
                                    value_type_object?, container, id}]. THE read Python cannot do.
    * get_pcg_graph_parameter    -- one desc + its best-effort serialized default value.

  WRITES (ledger -- reversible; inverse folds into editor_level.undo; each does a save). SCRATCH-ONLY:
    * add_pcg_graph_parameter    -- UPCGGraph::AddUserParameters({desc}). type string -> EPropertyBagPropertyType.
    * remove_pcg_graph_parameter -- UpdateUserParametersStruct(Bag -> Bag.RemovePropertyByName(name)).
    * rename_pcg_graph_parameter -- UPCGGraphInterface::RenameUserParameter(old, new).
    * add_pcg_dynamic_input_pin  -- Settings->OnUserAddDynamicInputPin() (adds a default source pin).
    * remove_pcg_dynamic_input_pin -- Settings->OnUserRemoveDynamicInputPin(Node, abs_index), guarded by
                                    CanUserRemoveDynamicInputPin FIRST (the engine method has check()s
                                    that crash the editor on a bad index).

NEW pcg_* ledger ops handed to the coordinator (each op below is the INVERSE the fold performs; the fields
listed are what editor_level.undo needs). The coordinator folds these five into editor_level.undo:

  - pcg_remove_graph_parameter{graph_path, name}
        <- recorded by add_pcg_graph_parameter. FOLD: call remove_pcg_graph_parameter_json(graph_path,
           name) (i.e. rl.remove_pcg_graph_parameter_json). LOSSLESS (removes a just-added param).
  - pcg_add_graph_parameter{graph_path, name, type, value_type_object, container, value_serialized}
        <- recorded by remove_pcg_graph_parameter. FOLD: call add_pcg_graph_parameter_json(graph_path,
           name, type) to restore the SCHEMA (LOSSLESS). The stored default value is only restorable via
           the Wave-3 typed setter (PCGGraphParametersHelpers.set_<type>_parameter with value_serialized)
           -- value restore is OPTIONAL/best-effort (LOSSY); schema restore is exact.
  - pcg_rename_graph_parameter{graph_path, old_name, new_name}
        <- recorded by rename_pcg_graph_parameter with old_name/new_name SWAPPED. FOLD: call
           rename_pcg_graph_parameter_json(graph_path, old_name, new_name) (renames back). LOSSLESS.
  - pcg_remove_dynamic_input_pin{graph_path, node_name, pin_index}
        <- recorded by add_pcg_dynamic_input_pin (pin_index == the new pin's absolute index). FOLD: call
           remove_pcg_dynamic_input_pin_json(graph_path, node_name, pin_index). LOSSLESS. (Not ledgered
           if the add was a no-op, i.e. new_pin_index == -1.)
  - pcg_add_dynamic_input_pin{graph_path, node_name}
        <- recorded by remove_pcg_dynamic_input_pin. FOLD: call add_pcg_dynamic_input_pin_json(graph_path,
           node_name) -- re-adds a DEFAULT source pin (LOSSY: a removed custom-configured pin is not
           byte-restored; dynamic source pins are homogeneous so this is usually exact).

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a snippet
local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj (the C++ wrapper's own names). Method resolution normalizes underscores so the "PCG" acronym's
snake-casing (add_pcg_graph_parameter_json) can't miss.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from mutable_graph_cpp.py) --------
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

    def _norm(p):
        return p.split(".")[0] if p else p

    def _scratch_guard(graph_path):
        if not graph_path:
            return "graph_path is required"
        if not _norm(graph_path).startswith("/Game/MCP_Scratch"):
            return "graph must be under /Game/MCP_Scratch (scratch-only); got %s" % graph_path
        return None

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    # _mrl(camel) resolves the reflected handler by normalizing underscores, so the "PCG" acronym's
    # snake-casing (add_pcg_graph_parameter_json vs any other placement) can't cause a miss.
    _HELP = r'''
import unreal, json, builtins, gc
try:
    import warnings as _wmod
    _wmod.simplefilter("ignore")
except Exception:
    pass

def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]

def _norm(p):
    return p.split(".")[0] if p else p

def _mrl(camel):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None:
        return None, None
    want = camel.replace("_", "").lower()
    for nm in dir(rl):
        if nm.replace("_", "").lower() == want:
            return rl, nm
    return None, None

def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}

def _defer(camel):
    return {"status": "error", "error": (camel + " requires the C++ PCG schema handler (PCG Wave 5). "
            "Rebuild the UnrealMCP plugin DLL with MCPReflection_PCG.cpp (Build.cs += PCG) to enable it.")}

def _pcg_save(graph_path):
    try:
        a = unreal.EditorAssetLibrary.load_asset(graph_path)
        if a is None:
            return False
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([a.get_outermost()], False))
    except Exception:
        return False
'''

    # ================================================================== #
    # READS (no ledger). Each is resolve-guarded -> inert until the DLL lands.
    # ================================================================== #

    _LIST_BODY = _HELP + r'''
rl, fn = _mrl("ListPCGGraphParametersJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_pcg_graph_parameters")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_pcg_graph_parameters(ctx, graph_path: str) -> str:
        """Enumerate a PCGGraph's user-parameters SCHEMA (the graph's `user_parameters` property bag).

        graph_path: content path to the PCGGraph asset.

        Returns {graph, graph_path, parameter_count, parameters:[{name, type, value_type_object?,
        value_type_object_name?, container, id}]}. Read-only, no ledger; works on any graph. Python
        CANNOT enumerate the bag (unreal.InstancedPropertyBag has no descriptor surface) -- needs the C++
        handler (inert until the plugin DLL is rebuilt with MCPReflection_PCG.cpp)."""
        try:
            return json.dumps(_exec(_LIST_BODY, {"graph_path": graph_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_BODY = _HELP + r'''
rl, fn = _mrl("GetPCGGraphParameterJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_pcg_graph_parameter")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_pcg_graph_parameter(ctx, graph_path: str, name: str) -> str:
        """Read one PCGGraph user-parameter descriptor + its best-effort serialized default value.

        graph_path: content path to the PCGGraph asset.
        name:       the parameter name (from list_pcg_graph_parameters).

        Returns {graph, graph_path, name, type, value_type_object?, container, id, value_serialized?}.
        Read-only, no ledger. Needs the C++ handler (inert until built)."""
        try:
            return json.dumps(_exec(_GET_BODY, {"graph_path": graph_path, "name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    # add_pcg_graph_parameter -> inverse op 'pcg_remove_graph_parameter' (remove the added param).
    _ADD_PARAM_BODY = _HELP + r'''
rl, fn = _mrl("AddPCGGraphParameterJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_pcg_graph_parameter")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["name"], PARAMS["type"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _pcg_save(PARAMS["graph_path"])
        _ledger().append({"op": "pcg_remove_graph_parameter", "graph_path": _norm(PARAMS["graph_path"]),
            "name": res.get("name")})
        out = dict(res); out["status"] = "success"; out["undo_op"] = "pcg_remove_graph_parameter"
        out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_pcg_graph_parameter(ctx, graph_path: str, name: str, type: str) -> str:
        """Add a typed user parameter to a PCGGraph's SCHEMA (UPCGGraph::AddUserParameters).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        name:       the new parameter name (sanitized by the bag; the response reports the ACTUAL name).
        type:       one of bool, byte, int32, int64, float, double, name, string, text, vector, vector2d,
                    rotator, transform, quat, linearcolor, object, softobject, class, softclass.

        Refuses to overwrite an existing same-named parameter. Ledgered write (op
        'pcg_remove_graph_parameter' -> `undo` removes the added parameter, LOSSLESS). Saves on success.
        Returns {name, requested_name, type, added, parameters:[...], ledger_depth}. Needs the C++ handler
        (inert until built)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not name:
                return json.dumps({"status": "error", "message": "name is required"}, indent=2)
            if not type:
                return json.dumps({"status": "error", "message": "type is required"}, indent=2)
            return json.dumps(_exec(_ADD_PARAM_BODY,
                {"graph_path": graph_path, "name": name, "type": type}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # remove_pcg_graph_parameter -> inverse op 'pcg_add_graph_parameter' (re-add captured schema).
    _REMOVE_PARAM_BODY = _HELP + r'''
rl, fn = _mrl("RemovePCGGraphParameterJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_pcg_graph_parameter")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _pcg_save(PARAMS["graph_path"])
        cap = res.get("captured", {}) if isinstance(res, dict) else {}
        _ledger().append({"op": "pcg_add_graph_parameter", "graph_path": _norm(PARAMS["graph_path"]),
            "name": cap.get("name"), "type": cap.get("type"),
            "value_type_object": cap.get("value_type_object"), "container": cap.get("container"),
            "value_serialized": cap.get("value_serialized"), "lossy_value": True})
        out = dict(res); out["status"] = "success"; out["undo_op"] = "pcg_add_graph_parameter"
        out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_pcg_graph_parameter(ctx, graph_path: str, name: str) -> str:
        """Remove a user parameter from a PCGGraph's SCHEMA
        (UPCGGraph::UpdateUserParametersStruct -> RemovePropertyByName).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        name:       the parameter to remove (from list_pcg_graph_parameters).

        Captures {name, type, value_type_object, container, value_serialized} BEFORE removal. Ledgered
        write (op 'pcg_add_graph_parameter' -> `undo` re-adds the parameter; SCHEMA restore is LOSSLESS,
        the stored default VALUE restore is best-effort via the Wave-3 typed setter, LOSSY). Saves on
        success. Returns {name, removed, captured, value_restore_is_lossy, parameters:[...], ledger_depth}.
        Needs the C++ handler (inert until built)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not name:
                return json.dumps({"status": "error", "message": "name is required"}, indent=2)
            return json.dumps(_exec(_REMOVE_PARAM_BODY,
                {"graph_path": graph_path, "name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # rename_pcg_graph_parameter -> inverse op 'pcg_rename_graph_parameter' (rename back).
    _RENAME_PARAM_BODY = _HELP + r'''
rl, fn = _mrl("RenamePCGGraphParameterJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("rename_pcg_graph_parameter")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["old_name"], PARAMS["new_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _pcg_save(PARAMS["graph_path"])
        # inverse renames the ACTUAL new name back to the original old name.
        _ledger().append({"op": "pcg_rename_graph_parameter", "graph_path": _norm(PARAMS["graph_path"]),
            "old_name": res.get("new_name"), "new_name": res.get("old_name")})
        out = dict(res); out["status"] = "success"; out["undo_op"] = "pcg_rename_graph_parameter"
        out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def rename_pcg_graph_parameter(ctx, graph_path: str, old_name: str, new_name: str) -> str:
        """Rename a PCGGraph user parameter (UPCGGraphInterface::RenameUserParameter).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        old_name:   the existing parameter name.
        new_name:   the new name (sanitized by the bag; the response reports the ACTUAL new name).

        Ledgered write (op 'pcg_rename_graph_parameter' -> `undo` renames back, LOSSLESS). Saves on
        success. Returns {old_name, requested_new_name, new_name, renamed, parameters:[...], ledger_depth}.
        Needs the C++ handler (inert until built)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not old_name or not new_name:
                return json.dumps({"status": "error", "message": "old_name and new_name are required"}, indent=2)
            return json.dumps(_exec(_RENAME_PARAM_BODY,
                {"graph_path": graph_path, "old_name": old_name, "new_name": new_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # add_pcg_dynamic_input_pin -> inverse op 'pcg_remove_dynamic_input_pin' (remove the new pin).
    _ADD_PIN_BODY = _HELP + r'''
rl, fn = _mrl("AddPCGDynamicInputPinJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_pcg_dynamic_input_pin")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["node_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _pcg_save(PARAMS["graph_path"])
        idx = res.get("new_pin_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        if res.get("added") and idx is not None and idx >= 0:
            _ledger().append({"op": "pcg_remove_dynamic_input_pin", "graph_path": _norm(PARAMS["graph_path"]),
                "node_name": res.get("node_name"), "pin_index": idx})
            res["undo_op"] = "pcg_remove_dynamic_input_pin"
        else:
            res["undo_op"] = None
            res["note"] = "add was a no-op (settings rejected the pin) -- not ledgered"
        out = dict(res); out["status"] = "success"; out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_pcg_dynamic_input_pin(ctx, graph_path: str, node_name: str) -> str:
        """Add a dynamic input (source) pin to a PCG node whose settings derive from
        UPCGSettingsWithDynamicInputs (Settings->OnUserAddDynamicInputPin).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        node_name:  a dynamic-input node's addressing id (its UPCGNode object name, else its title).

        Adds one DEFAULT source pin and reconstructs the node. Ledgered write (op
        'pcg_remove_dynamic_input_pin' -> `undo` removes that pin, LOSSLESS) -- unless the settings
        rejected the add (new_pin_index == -1), in which case nothing is ledgered. Saves on success.
        Returns {node_name, settings_class, static_input_pins, dynamic_before, dynamic_after,
        new_pin_index, added, ledger_depth}. Needs the C++ handler (inert until built)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not node_name:
                return json.dumps({"status": "error", "message": "node_name is required"}, indent=2)
            return json.dumps(_exec(_ADD_PIN_BODY,
                {"graph_path": graph_path, "node_name": node_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # remove_pcg_dynamic_input_pin -> inverse op 'pcg_add_dynamic_input_pin' (re-add a default pin, LOSSY).
    _REMOVE_PIN_BODY = _HELP + r'''
rl, fn = _mrl("RemovePCGDynamicInputPinJson")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_pcg_dynamic_input_pin")))
else:
    res = _decode(getattr(rl, fn)(PARAMS["graph_path"], PARAMS["node_name"], int(PARAMS.get("pin_index", -1))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _pcg_save(PARAMS["graph_path"])
        _ledger().append({"op": "pcg_add_dynamic_input_pin", "graph_path": _norm(PARAMS["graph_path"]),
            "node_name": res.get("node_name"), "lossy": True})
        out = dict(res); out["status"] = "success"; out["undo_op"] = "pcg_add_dynamic_input_pin"
        out["ledger_depth"] = len(_ledger())
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_pcg_dynamic_input_pin(ctx, graph_path: str, node_name: str, pin_index: int = -1) -> str:
        """Remove a dynamic input (source) pin from a PCG node whose settings derive from
        UPCGSettingsWithDynamicInputs (Settings->OnUserRemoveDynamicInputPin).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        node_name:  a dynamic-input node's addressing id (its UPCGNode object name, else its title).
        pin_index:  the ABSOLUTE input-pin index (static + dynamic) of the pin to remove; the valid range
                    is [static_input_pins, static_input_pins + dynamic_input_pins). DEFAULT -1 removes the
                    LAST dynamic pin. The C++ handler GUARDS on CanUserRemoveDynamicInputPin first (a bad
                    index is refused with an error, never a crash).

        Ledgered write (op 'pcg_add_dynamic_input_pin' -> `undo` re-adds a DEFAULT source pin, best-effort
        LOSSY: dynamic source pins are homogeneous so this is usually exact). Saves on success. Returns
        {node_name, removed_pin_index, removed_pin_label?, static_input_pins, dynamic_before, dynamic_after,
        removed, inverse_is_lossy, ledger_depth}. Needs the C++ handler (inert until built)."""
        try:
            gerr = _scratch_guard(graph_path)
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not node_name:
                return json.dumps({"status": "error", "message": "node_name is required"}, indent=2)
            return json.dumps(_exec(_REMOVE_PIN_BODY,
                {"graph_path": graph_path, "node_name": node_name, "pin_index": pin_index}), indent=2)
        except Exception as e:
            return f"Error: {e}"
