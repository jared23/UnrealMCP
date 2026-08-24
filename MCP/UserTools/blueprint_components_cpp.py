"""UserTools :: Blueprint SCS (Simple Construction Script) COMPONENT authoring  (spec: docs/spec/blueprints.md)

DRAFT wiring for the Blueprint-SCS C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_BlueprintSCS.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied
VERBATIM from the gold-standard niagara_runtime_cpp.py.

Fills the spec gap that stock Python + list_blueprint_components CANNOT reach: the SCS node tree that
holds a Blueprint's DEFAULT components and their ATTACH HIERARCHY (UBlueprint.SimpleConstructionScript
-> USCS_Node tree is editor-only data the Python API cannot walk or edit).

  READ (no ledger -- non-mutating):
    * get_blueprint_scs          -- MCPReflectionLibrary.get_blueprint_scs_json(blueprint_path): every
                                    USCS_Node {name, component_class, parent_name, is_root, is_scene_root,
                                    child_names, template summary (scene transform / mobility / attach socket)}.

  WRITE (ledger -- reversible; inverse folds into editor_level.undo):
    * add_component_to_blueprint    -- CreateNode + AddNode/AddChildNode. Ledger op add_blueprint_component;
                                       inverse = delete the created node by name.
    * set_blueprint_component_property -- set an FProperty on the node's ComponentTemplate archetype; captures
                                       the PRIOR value (ExportText). Ledger op set_blueprint_component_property;
                                       inverse = re-apply the prior value.
    * delete_blueprint_component    -- RemoveNodeAndPromoteChildren. Captures class+parent+prop snapshot. Ledger
                                       op delete_blueprint_component; inverse = re-add + re-apply the snapshot
                                       (LOSSY: promoted children do NOT un-promote -- see the tool doc).
    * reparent_blueprint_component  -- RemoveNode + AddChildNode (empty new_parent => promote to root). Captures
                                       prior parent. Ledger op reparent_blueprint_component; inverse = reparent back.
    * set_blueprint_root_component  -- promote a scene node to the scene root (old root becomes its child).
                                       Captures prior root + the node's prior parent. Ledger op
                                       set_blueprint_root_component; inverse = restore prior root + reparent node back.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write's
inverse is appended to the shared per-session ledger for editor_level.undo to fold. The C++ handlers already
compile the Blueprint (FKismetEditorUtilities::CompileBlueprint) + mark the package dirty; editor_level.undo
saves the asset after applying the inverse.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a snippet
local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py) -----
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

    # Client-side helper: normalize a caller value into the ARRAY-WRAPPED JSON string the C++ setter wants.
    # UE's JSON reader rejects a bare scalar at document root, so the C++ handler unwraps a single-element
    # array. Caller passes value_json as a JSON value ("true", "1.5", "\"EnumName\"", "\"(X=1,Y=2,Z=3)\"").
    def _wrap_value(value_json):
        try:
            return json.dumps([json.loads(value_json)])
        except Exception:
            return json.dumps([value_json])

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _HELP = r'''
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ Blueprint-SCS handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_BlueprintSCS.cpp to enable it.")}
'''

    # ================================================================== #
    # READ (no ledger). hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _GET_SCS_BODY = _HELP + r'''
rl = _mrl("get_blueprint_scs_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_blueprint_scs_json")))
else:
    res = _decode(rl.get_blueprint_scs_json(PARAMS["blueprint_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_blueprint_scs(ctx, blueprint_path: str) -> str:
        """Walk a Blueprint's Simple Construction Script (default-component tree + attach hierarchy).

        blueprint_path: Blueprint asset path (e.g. '/Game/BP/BP_Foo.BP_Foo' or '/Game/BP/BP_Foo').

        Returns {blueprint, path, parent_class, node_count, scene_root, nodes:[{name, component_class,
        parent_name, is_root, is_scene_root, is_default_scene_root, parent_is_native, child_names,
        has_template, template_name, is_scene_component[, relative_location, relative_rotation,
        relative_scale, mobility][, attach_socket]}]}. This is the attach hierarchy that
        list_blueprint_components cannot reach. Editor-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path}
        try:
            return json.dumps(_exec(_GET_SCS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITE (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    _ADD_BODY = _HELP + r'''
rl = _mrl("add_component_to_blueprint_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_component_to_blueprint_json")))
else:
    res = _decode(rl.add_component_to_blueprint_json(PARAMS["blueprint_path"], PARAMS["component_class"],
        PARAMS.get("component_name", ""), PARAMS.get("parent_component_name", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "add_blueprint_component", "asset_path": PARAMS["blueprint_path"],
            "component": res.get("component")})
        out = {"status": "success", "blueprint": res.get("blueprint"), "component": res.get("component"),
               "component_class": res.get("component_class"), "parent": res.get("parent"),
               "is_root": bool(res.get("is_root")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_component_to_blueprint(ctx, blueprint_path: str, component_class: str, component_name: str = "",
                                   parent_component_name: str = "") -> str:
        """Add a component node to a Blueprint's SCS (recompiles + marks the package dirty).

        blueprint_path:        Blueprint asset path.
        component_class:       component class path ('/Script/Engine.StaticMeshComponent'), a bare engine class
                               name ('StaticMeshComponent'), or a BP-component class/asset path. MUST be a
                               UActorComponent subclass (non-abstract).
        component_name:        desired variable name (empty = engine auto-name). May be auto-suffixed if taken;
                               the ACTUAL created name is returned.
        parent_component_name: attach under this existing SCS node (empty = add as a root). A scene component
                               needs a SceneComponent parent.

        Ledger op add_blueprint_component; inverse = delete the created node by name. Returns {blueprint,
        component (actual name), component_class, parent, is_root, ledger_depth}. Editor-only; needs the C++
        handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "component_class": component_class,
                  "component_name": component_name, "parent_component_name": parent_component_name}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_PROP_BODY = _HELP + r'''
rl = _mrl("set_blueprint_component_property_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_component_property_json")))
else:
    res = _decode(rl.set_blueprint_component_property_json(PARAMS["blueprint_path"], PARAMS["component_name"],
        PARAMS["property_name"], PARAMS["value_json"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_blueprint_component_property", "asset_path": PARAMS["blueprint_path"],
            "component": res.get("component", PARAMS["component_name"]),
            "property": res.get("property", PARAMS["property_name"]), "prev": res.get("prev")})
        out = {"status": "success", "blueprint": res.get("blueprint"), "component": res.get("component"),
               "property": res.get("property"), "set": bool(res.get("set")), "prev": res.get("prev"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_component_property(ctx, blueprint_path: str, component_name: str, property_name: str,
                                         value_json: str) -> str:
        """Set a property on a Blueprint component's template archetype (captures the prior value for undo).

        blueprint_path: Blueprint asset path.
        component_name: SCS node / component variable name.
        property_name:  FProperty name on the component class (e.g. 'StaticMesh', 'RelativeLocation',
                        'bVisible', 'Mobility').
        value_json:     the new value as a JSON value -- a scalar ('true', '1.5'), an enum name
                        ('"Movable"'), an object path ('"/Game/Meshes/SM_Cube.SM_Cube"'), or a UE ExportText
                        string for a struct ('"(X=1.0,Y=2.0,Z=3.0)"'). Wrapped array-side automatically.

        Ledger op set_blueprint_component_property; inverse = re-apply the captured prior ExportText value.
        Returns {blueprint, component, property, set, prev (ExportText string), ledger_depth}. Editor-only;
        needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "component_name": component_name,
                  "property_name": property_name, "value_json": _wrap_value(value_json)}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _DELETE_BODY = _HELP + r'''
rl = _mrl("delete_blueprint_component_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("delete_blueprint_component_json")))
else:
    res = _decode(rl.delete_blueprint_component_json(PARAMS["blueprint_path"], PARAMS["component_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "delete_blueprint_component", "asset_path": PARAMS["blueprint_path"],
            "component": res.get("component", PARAMS["component_name"]),
            "component_class": res.get("component_class"), "parent": res.get("parent"),
            "prop_snapshot": res.get("prop_snapshot") or {}})
        out = {"status": "success", "blueprint": res.get("blueprint"), "component": res.get("component"),
               "removed": bool(res.get("removed")), "component_class": res.get("component_class"),
               "parent": res.get("parent"), "was_root": bool(res.get("was_root")),
               "promoted_children": res.get("promoted_children") or [],
               "prop_snapshot": res.get("prop_snapshot") or {}, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def delete_blueprint_component(ctx, blueprint_path: str, component_name: str) -> str:
        """Delete a component from a Blueprint's SCS, promoting its children to its parent.

        blueprint_path: Blueprint asset path.
        component_name: SCS node / component variable name (cannot be the engine DefaultSceneRoot).

        Uses RemoveNodeAndPromoteChildren: any child components are re-parented to the deleted node's parent.
        Ledger op delete_blueprint_component; inverse = re-add the node (class+parent) + re-apply a snapshot of
        its non-default simple properties. LOSSY: promoted children do NOT un-promote back under it on undo, and
        container/complex properties are not snapshotted. Returns {blueprint, component, removed,
        component_class, parent, was_root, promoted_children, prop_snapshot, ledger_depth}. Editor-only; needs
        the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "component_name": component_name}
        try:
            return json.dumps(_exec(_DELETE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REPARENT_BODY = _HELP + r'''
rl = _mrl("reparent_blueprint_component_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("reparent_blueprint_component_json")))
else:
    res = _decode(rl.reparent_blueprint_component_json(PARAMS["blueprint_path"], PARAMS["component_name"],
        PARAMS.get("new_parent_name", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("reparented"):
            _ledger().append({"op": "reparent_blueprint_component", "asset_path": PARAMS["blueprint_path"],
                "component": res.get("component", PARAMS["component_name"]),
                "prior_parent": res.get("prior_parent") or ""})
        out = {"status": "success", "blueprint": res.get("blueprint"), "component": res.get("component"),
               "new_parent": res.get("new_parent"), "prior_parent": res.get("prior_parent"),
               "reparented": bool(res.get("reparented")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def reparent_blueprint_component(ctx, blueprint_path: str, component_name: str,
                                     new_parent_name: str = "") -> str:
        """Move a Blueprint SCS component under a new parent (empty new_parent => promote to root).

        blueprint_path:  Blueprint asset path.
        component_name:  SCS node / component variable name to move (cannot be the DefaultSceneRoot).
        new_parent_name: existing SCS node to become the new parent; EMPTY promotes the node to a root. A scene
                         component needs a SceneComponent parent; a descendant cannot become the parent (cycle).

        Ledger op reparent_blueprint_component; inverse = reparent back to the prior parent. Returns {blueprint,
        component, new_parent, prior_parent, reparented, ledger_depth}. Editor-only; needs the C++ handler
        (inert until built)."""
        params = {"blueprint_path": blueprint_path, "component_name": component_name,
                  "new_parent_name": new_parent_name}
        try:
            return json.dumps(_exec(_REPARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_ROOT_BODY = _HELP + r'''
rl = _mrl("set_blueprint_root_component_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_blueprint_root_component_json")))
else:
    res = _decode(rl.set_blueprint_root_component_json(PARAMS["blueprint_path"], PARAMS["component_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("changed"):
            _ledger().append({"op": "set_blueprint_root_component", "asset_path": PARAMS["blueprint_path"],
                "component": res.get("component", PARAMS["component_name"]),
                "prior_root": res.get("prior_root") or "",
                "node_prior_parent": res.get("node_prior_parent") or ""})
        out = {"status": "success", "blueprint": res.get("blueprint"), "component": res.get("component"),
               "prior_root": res.get("prior_root"), "node_prior_parent": res.get("node_prior_parent"),
               "changed": bool(res.get("changed")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_blueprint_root_component(ctx, blueprint_path: str, component_name: str) -> str:
        """Promote a scene component to the Blueprint's scene root (old root becomes its child).

        blueprint_path: Blueprint asset path.
        component_name: SCS node / component variable name; MUST be a SceneComponent. The current scene root
                        (which must be an SCS node -- a native/inherited root is refused) becomes a child of the
                        new root; the new root's relative transform is reset (as the editor does).

        Ledger op set_blueprint_root_component; inverse = restore the prior root, then reparent this node back
        to its prior parent. Returns {blueprint, component, prior_root, node_prior_parent, changed,
        ledger_depth}. Editor-only; needs the C++ handler (inert until built)."""
        params = {"blueprint_path": blueprint_path, "component_name": component_name}
        try:
            return json.dumps(_exec(_SET_ROOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
