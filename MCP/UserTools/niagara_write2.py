"""UserTools :: Niagara / VFX runtime + authoring (WRITE2)  (spec: docs/spec/niagara.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). Mutating counterpart to
niagara_read2.py. Query convention, base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py /
editor_actor_components.py / niagara_write.py.

REACHABLE from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1) -- shipped here:
  * spawn_niagara_effect  -- EditorActorSubsystem.spawn_actor_from_class(unreal.NiagaraActor); the
                             actor's 'niagara_component' UPROPERTY -> set_asset(system) [+ activate].
                             Inverse: EAS.destroy_actor.
  * control_niagara_effect -- activate / deactivate / reset_system on a placed UNiagaraComponent
                             (resolved by actor label). Inverse: restore prior active state.
  * add_niagara_component  -- SubobjectDataSubsystem.add_new_subobject(UNiagaraComponent) on an
                             existing level actor + set_asset(system). Inverse: delete that subobject
                             (same reversible instanced-component delete as remove_component_from_actor).

DEFERRED -- needs a C++ NiagaraEditor handler (NOT reachable from runtime Python here; refused rather
than shipping a fake/unrevertable write). Each is hasattr-guarded on a future MCPReflectionLibrary
method so it AUTO-ENABLES when the C++ lands; until then it returns the standard fallback:
  * set_niagara_renderer_property -- the renderer UNiagaraRendererProperties objects live in versioned
    emitter data (FVersionedNiagaraEmitterData) and are NOT python-reachable (no getter, no setter on
    MCPReflectionLibrary), so there is no way to read the prior value for a faithful inverse nor to
    persist+recompile the change. Editor-only.
  * set_niagara_renderer_binding -- same reason (FNiagaraVariableAttributeBinding lives on the same
    editor-only renderer object).
  * duplicate_niagara_emitter -- MCPReflectionLibrary exposes add_emitter_to_system (copies a standalone
    UNiagaraEmitter ASSET into a system) but NO within-system handle DUPLICATE (get_all_emitters returns
    minimal info, not the source emitter object of an existing handle), so a handle cannot be duplicated
    from Python. Editor-only.
  * reorder_niagara_emitter -- no MCPReflectionLibrary method to reorder emitter handles; the
    EmitterHandles array is not python-writable. Editor-only.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The op
inverses are reported to the coordinator to fold into editor_level.undo (see each tool docstring +
the batch report).

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
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
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
def _eas():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
def _resolve_actor(ident):
    if not ident:
        return None
    for a in (_eas().get_all_level_actors() or []):
        if a.get_actor_label() == ident or a.get_name() == ident:
            return a
    return None
def _niag_comp(actor):
    # ANiagaraActor exposes 'niagara_component'; otherwise first UNiagaraComponent on the actor.
    if isinstance(actor, unreal.NiagaraActor):
        c = _try(lambda: actor.get_editor_property("niagara_component"))
        if c is not None:
            return c
    comps = _try(lambda: list(actor.get_components_by_class(unreal.NiagaraComponent) or []), [])
    return comps[0] if comps else None
def _sods():
    return unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
def _comp_from_handle(h):
    d = _sods().k2_find_subobject_data_from_handle(h)
    if d is None:
        return None
    return unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d)
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _mkvec(v, d):
    if not v:
        return unreal.Vector(d[0], d[1], d[2])
    return unreal.Vector(float(v[0]), float(v[1]), float(v[2]))
def _mkrot(v):
    if not v:
        return unreal.Rotator(0.0, 0.0, 0.0)
    return unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2]))
'''

    # ------------------------------------------------------------------ #
    # spawn_niagara_effect                                                 #
    # ------------------------------------------------------------------ #
    _SPAWN_BODY = _HELP + r'''
system_path = PARAMS["system_path"]
loc = PARAMS.get("location"); rot = PARAMS.get("rotation")
label = PARAMS.get("label"); auto = bool(PARAMS.get("auto_activate", True))
sysobj = _try(lambda: unreal.EditorAssetLibrary.load_asset(system_path))
if sysobj is None or not isinstance(sysobj, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "not a NiagaraSystem: %s" % system_path}))
else:
    with unreal.ScopedEditorTransaction("MCP spawn_niagara_effect"):
        actor = _eas().spawn_actor_from_class(unreal.NiagaraActor, _mkvec(loc, [0, 0, 0]), _mkrot(rot))
    if actor is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "spawn_actor_from_class returned None"}))
    else:
        if label:
            _try(lambda: actor.set_actor_label(label))
        nc = _niag_comp(actor)
        set_ok = False
        if nc is not None:
            _try(lambda: nc.set_asset(sysobj))
            set_ok = (_try(lambda: nc.get_asset()) == sysobj)
            if auto:
                _try(lambda: nc.activate(True))
        _ledger().append({"op": "spawn_niagara_effect", "actor_name": actor.get_name(),
                          "actor_label": actor.get_actor_label(), "system_path": system_path})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor_name": actor.get_name(),
            "actor_label": actor.get_actor_label(), "system": sysobj.get_path_name(),
            "component": (nc.get_name() if nc else None), "asset_assigned": bool(set_ok),
            "is_active": bool(_try(lambda: nc.is_active(), False)) if nc else False,
            "location": _try(lambda: [round(actor.get_actor_location().x, 2),
                                      round(actor.get_actor_location().y, 2),
                                      round(actor.get_actor_location().z, 2)]),
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def spawn_niagara_effect(ctx, system_path: str, location: list = None, rotation: list = None,
                             label: str = None, auto_activate: bool = True) -> str:
        """Spawn a NiagaraActor into the CURRENT level from a NiagaraSystem asset. Ledgered write.

        system_path:   NiagaraSystem asset path (e.g. '/Game/MCP_Scratch/NS_Foo.NS_Foo').
        location:      optional [x,y,z] world location (default [0,0,0]).
        rotation:      optional [pitch,yaw,roll] (default [0,0,0]).
        label:         optional actor display label.
        auto_activate: activate the component after assigning the system (default True). NOTE one-shot
                       systems may report is_active False immediately (they complete without a world tick).

        Sets the system on the actor's UNiagaraComponent (ANiagaraActor.niagara_component). Ledgered op
        'spawn_niagara_effect' {actor_name, actor_label, system_path}; inverse: destroy the spawned actor
        (EditorActorSubsystem.destroy_actor by actor_name) -> FAITHFUL."""
        params = {"system_path": system_path, "location": location, "rotation": rotation,
                  "label": label, "auto_activate": auto_activate}
        try:
            return json.dumps(_exec(_SPAWN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # control_niagara_effect                                              #
    # ------------------------------------------------------------------ #
    _CONTROL_BODY = _HELP + r'''
ident = PARAMS["actor"]; action = (PARAMS["action"] or "").lower()
actor = _resolve_actor(ident)
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % ident}))
else:
    nc = _niag_comp(actor)
    if nc is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no UNiagaraComponent on actor: %s" % ident}))
    elif action not in ("activate", "deactivate", "reset"):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "action must be activate|deactivate|reset (got '%s')" % action}))
    else:
        prev_active = bool(_try(lambda: nc.is_active(), False))
        if action == "activate":
            _try(lambda: nc.activate(True))
        elif action == "deactivate":
            _try(lambda: nc.deactivate())
        else:
            _try(lambda: nc.reset_system())
        _ledger().append({"op": "control_niagara_effect", "actor_name": actor.get_name(),
                          "component_name": nc.get_name(), "action": action, "prev_active": prev_active})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_actor_label(),
            "actor_name": actor.get_name(), "component": nc.get_name(), "action": action,
            "prev_active": prev_active, "is_active": bool(_try(lambda: nc.is_active(), False)),
            "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def control_niagara_effect(ctx, actor: str, action: str) -> str:
        """Activate / deactivate / reset a placed Niagara component. Ledgered write.

        actor:  actor display label (preferred) or unique internal name of a NiagaraActor (or any actor
                carrying a UNiagaraComponent).
        action: 'activate' | 'deactivate' | 'reset' (reset -> reset_system).

        Captures the prior is_active state. Ledgered op 'control_niagara_effect'
        {actor_name, component_name, action, prev_active}; inverse: restore prev_active (activate if it
        was active, deactivate otherwise) -> FAITHFUL for activate/deactivate; for 'reset' the inverse is
        best-effort (reset has no captured prior sim state)."""
        params = {"actor": actor, "action": action}
        try:
            return json.dumps(_exec(_CONTROL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_niagara_component                                               #
    # ------------------------------------------------------------------ #
    _ADD_COMP_BODY = _HELP + r'''
ident = PARAMS["actor"]; want_name = PARAMS.get("component_name")
system_path = PARAMS.get("system_path")
actor = _resolve_actor(ident)
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % ident}))
else:
    sysobj = None
    if system_path:
        sysobj = _try(lambda: unreal.EditorAssetLibrary.load_asset(system_path))
        if sysobj is not None and not isinstance(sysobj, unreal.NiagaraSystem):
            sysobj = None
    if system_path and sysobj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a NiagaraSystem: %s" % system_path}))
    else:
        handles = _sods().k2_gather_subobject_data_for_instance(actor) or []
        root = handles[0] if handles else None
        if root is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not gather subobject data for actor"}))
        else:
            params = unreal.AddNewSubobjectParams()
            params.set_editor_property("parent_handle", root)
            params.set_editor_property("new_class", unreal.NiagaraComponent)
            params.set_editor_property("blueprint_context", None)
            params.set_editor_property("conform_transform_to_parent", True)
            with unreal.ScopedEditorTransaction("MCP add_niagara_component"):
                new_handle, fail = _sods().add_new_subobject(params)
                fail_s = str(fail)
                comp = None
                if not fail_s:
                    comp = _comp_from_handle(new_handle)
                    final_name = comp.get_name() if comp else None
                    if want_name and comp is not None and final_name != want_name:
                        if _sods().rename_subobject(new_handle, want_name):
                            final_name = comp.get_name()
                    if comp is not None and sysobj is not None:
                        _try(lambda: comp.set_asset(sysobj))
            if fail_s or comp is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_new_subobject failed: %s" % (fail_s or "no object")}))
            else:
                _ledger().append({"op": "add_niagara_component", "actor_name": actor.get_name(),
                    "component_name": final_name, "component_class": comp.get_class().get_name(),
                    "system_path": system_path})
                print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_actor_label(),
                    "actor_name": actor.get_name(), "component_name": final_name,
                    "component_class": comp.get_class().get_name(),
                    "system": (sysobj.get_path_name() if sysobj else None),
                    "asset_assigned": bool(sysobj is not None and _try(lambda: comp.get_asset()) == sysobj),
                    "ledger_depth": len(_ledger())}))
gc.collect()
'''

    @mcp.tool()
    def add_niagara_component(ctx, actor: str, component_name: str = None,
                             system_path: str = None) -> str:
        """Add a UNiagaraComponent to an existing level actor + optionally assign a system. Ledgered write.

        actor:          actor display label (preferred) or unique internal name.
        component_name: desired name for the new component (renamed after creation); if omitted, the
                        engine-assigned name is returned.
        system_path:    optional NiagaraSystem asset to assign to the new component (set_asset).

        Uses SubobjectDataSubsystem.add_new_subobject (the same reversible instanced-component path as
        add_component_to_actor). Ledgered op 'add_niagara_component'
        {actor_name, component_name, component_class, system_path}; inverse: delete exactly that
        instanced component from the actor (k2_delete_subobject_from_instance) -> FAITHFUL."""
        params = {"actor": actor, "component_name": component_name, "system_path": system_path}
        try:
            return json.dumps(_exec(_ADD_COMP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # DEFERRED -- need a C++ NiagaraEditor handler. Each is hasattr-guarded on a future
    # MCPReflectionLibrary method so it AUTO-ENABLES when the C++ lands; until then returns the
    # standard fallback. (Renderer objects + emitter-handle order live in versioned editor-only data.)
    # ================================================================== #
    _DEFER_RENDERER_PROP_BODY = _HELP + r'''
rl = _mrl("set_niagara_renderer_property")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "error": "set_niagara_renderer_property requires C++ NiagaraEditor handler (deferred to batched C++ round)"}))
else:
    res = json.loads(rl.set_niagara_renderer_property(PARAMS["system_path"], PARAMS["emitter_name"],
        int(PARAMS["renderer_index"]), PARAMS["property_name"], PARAMS["value_json"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_renderer_property", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "renderer_index": int(PARAMS["renderer_index"]),
            "property": PARAMS["property_name"], "prev_value_json": json.dumps(res.get("prev"))})
        print("@@UMCP@@" + json.dumps({"status": "success", "prev": res.get("prev"),
            "property": PARAMS["property_name"], "ledger_depth": len(_ledger())}))
'''

    _DEFER_RENDERER_BIND_BODY = _HELP + r'''
rl = _mrl("set_niagara_renderer_binding")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "error": "set_niagara_renderer_binding requires C++ NiagaraEditor handler (deferred to batched C++ round)"}))
else:
    res = json.loads(rl.set_niagara_renderer_binding(PARAMS["system_path"], PARAMS["emitter_name"],
        int(PARAMS["renderer_index"]), PARAMS["binding_name"], PARAMS["source_value"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_niagara_renderer_binding", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "renderer_index": int(PARAMS["renderer_index"]),
            "binding": PARAMS["binding_name"], "prev_source": res.get("prev")})
        print("@@UMCP@@" + json.dumps({"status": "success", "prev": res.get("prev"),
            "binding": PARAMS["binding_name"], "ledger_depth": len(_ledger())}))
'''

    _DEFER_DUP_EMITTER_BODY = _HELP + r'''
rl = _mrl("duplicate_niagara_emitter_handle")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "error": "duplicate_niagara_emitter requires C++ NiagaraEditor handler (deferred to batched C++ round)"}))
else:
    sysobj = _try(lambda: unreal.EditorAssetLibrary.load_asset(PARAMS["system_path"]))
    res = json.loads(rl.duplicate_niagara_emitter_handle(sysobj, PARAMS["emitter_name"], PARAMS.get("new_name") or ""))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "duplicate_niagara_emitter", "asset_path": PARAMS["system_path"],
            "handle_id": res.get("added_handle_id")})
        print("@@UMCP@@" + json.dumps({"status": "success", "added_handle_name": res.get("added_handle_name"),
            "added_handle_id": res.get("added_handle_id"), "ledger_depth": len(_ledger())}))
'''

    _DEFER_REORDER_EMITTER_BODY = _HELP + r'''
rl = _mrl("reorder_niagara_emitter_handle")
if rl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "error": "reorder_niagara_emitter requires C++ NiagaraEditor handler (deferred to batched C++ round)"}))
else:
    sysobj = _try(lambda: unreal.EditorAssetLibrary.load_asset(PARAMS["system_path"]))
    res = json.loads(rl.reorder_niagara_emitter_handle(sysobj, PARAMS["emitter_name"], int(PARAMS["new_index"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "reorder_niagara_emitter", "asset_path": PARAMS["system_path"],
            "emitter": PARAMS["emitter_name"], "prev_index": res.get("prev_index"),
            "new_index": int(PARAMS["new_index"])})
        print("@@UMCP@@" + json.dumps({"status": "success", "prev_index": res.get("prev_index"),
            "new_index": int(PARAMS["new_index"]), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_niagara_renderer_property(ctx, system_path: str, emitter_name: str, renderer_index: int,
                                      property_name: str, value_json: str) -> str:
        """Set a property on a named emitter's renderer (e.g. sort_mode, alignment). DEFERRED: the renderer
        UNiagaraRendererProperties objects live in versioned emitter data and are not reachable from runtime
        Python, so this needs a C++ NiagaraEditor handler (auto-enables via
        MCPReflectionLibrary.set_niagara_renderer_property when it lands; returns the standard fallback now).

        Intended ledger op 'set_niagara_renderer_property'
        {asset_path, emitter, renderer_index, property, prev_value_json}; inverse re-sets prev_value_json."""
        params = {"system_path": system_path, "emitter_name": emitter_name,
                  "renderer_index": renderer_index, "property_name": property_name, "value_json": value_json}
        try:
            return json.dumps(_exec(_DEFER_RENDERER_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_niagara_renderer_binding(ctx, system_path: str, emitter_name: str, renderer_index: int,
                                     binding_name: str, source_value: str) -> str:
        """Set a renderer variable binding (FNiagaraVariableAttributeBinding) on a named emitter's renderer.
        DEFERRED: same editor-only renderer object -- needs a C++ NiagaraEditor handler (auto-enables via
        MCPReflectionLibrary.set_niagara_renderer_binding; returns the standard fallback now).

        Intended ledger op 'set_niagara_renderer_binding'
        {asset_path, emitter, renderer_index, binding, prev_source}; inverse restores prev_source."""
        params = {"system_path": system_path, "emitter_name": emitter_name,
                  "renderer_index": renderer_index, "binding_name": binding_name, "source_value": source_value}
        try:
            return json.dumps(_exec(_DEFER_RENDERER_BIND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def duplicate_niagara_emitter(ctx, system_path: str, emitter_name: str, new_name: str = "") -> str:
        """Duplicate an emitter handle within a system under a new name. DEFERRED: MCPReflectionLibrary
        exposes add_emitter_to_system (from a standalone emitter ASSET) but no within-system handle
        duplicate, and get_all_emitters yields minimal info (not the source emitter object), so this needs
        a C++ NiagaraEditor handler (auto-enables via MCPReflectionLibrary.duplicate_niagara_emitter_handle;
        returns the standard fallback now).

        Intended ledger op 'duplicate_niagara_emitter' {asset_path, handle_id}; inverse:
        remove_emitter_from_system(handle_id)."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_DEFER_DUP_EMITTER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def reorder_niagara_emitter(ctx, system_path: str, emitter_name: str, new_index: int) -> str:
        """Change an emitter handle's index within the system. DEFERRED: the EmitterHandles array is not
        python-writable and there is no MCPReflectionLibrary reorder method, so this needs a C++
        NiagaraEditor handler (auto-enables via MCPReflectionLibrary.reorder_niagara_emitter_handle;
        returns the standard fallback now).

        Intended ledger op 'reorder_niagara_emitter' {asset_path, emitter, prev_index, new_index}; inverse:
        reorder back to prev_index."""
        params = {"system_path": system_path, "emitter_name": emitter_name, "new_index": new_index}
        try:
            return json.dumps(_exec(_DEFER_REORDER_EMITTER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
