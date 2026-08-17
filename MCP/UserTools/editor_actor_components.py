"""UserTools :: Editor / Actor Components  (spec: docs/spec/editor.md — "Components on level actors")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block (session-aware _ledger(), _resolve_actor,
_find_by_name, _descend, _settable/_coerce).

These commands WRITE instanced components on LEVEL ACTORS (not Blueprint assets). The
component editing API in this UE 5.8 build is the SubobjectDataSubsystem (Actor has NO
add_component_by_class in this build). Live-verified call sequence:
  sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
  handles = sods.k2_gather_subobject_data_for_instance(actor)   # handles[0] = actor node
  params = unreal.AddNewSubobjectParams(); params.parent_handle=handles[0];
           params.new_class=<Comp>; params.blueprint_context=None; conform_transform_to_parent=True
  new_handle, fail = sods.add_new_subobject(params)             # attaches under root component
  sods.rename_subobject(new_handle, "Name")                     # -> bool
  sods.k2_delete_subobject_from_instance(handles[0], new_handle) # context_handle + target -> count
  comp_obj = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(
                 sods.k2_find_subobject_data_from_handle(handle))  # actual instance component

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records an inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]
(keyed by PARAMS["_session"], injected by _exec). Reversibility is REQUIRED for every write.

IMPORTANT — NEW ledger op names (the shared `undo` in editor_level.py does NOT invert these
yet; the coordinator will add matching undo branches). This module therefore defines NO `undo`
tool. Each write records ONE ledger entry per call with enough prior state to invert:
  - add_component          -> {"op":"add_component","actor_name":<unique>,
                              "component_name":<final name>,"component_class":<class name>}
                             (invert: re-gather actor instance handles, find the handle whose
                              associated object name == component_name, delete it via
                              k2_delete_subobject_from_instance(handles[0], that_handle))
  - remove_component       -> {"op":"remove_component","actor_name":<unique>,
                              "component_class":<class name>,"component_name":<name>,
                              "attach_parent":<parent comp name or null>,
                              "transform":{"loc":[..],"rot":[..],"scale":[..]} (or null),
                              "props":{<name>:<settable json>, ...}}
                             (invert: re-gather actor handles, add_new_subobject of component_class
                              under the attach_parent handle (else root), rename the new component
                              to component_name, restore the relative transform, then diff-restore
                              each captured prop). FName-reservation SIDESTEP: remove renames the
                              victim to a junk name BEFORE k2_delete_subobject_from_instance, which
                              frees the original FName so the re-add reclaims it exactly (naive
                              delete-then-rename-back returns false; rename-victim-first -> true,
                              live-verified). Only instance-added components are removable; native/
                              inherited/root default subobjects are refused.
  - rename_component       -> {"op":"rename_component","actor_name":<unique>,
                              "old_name":<str>,"new_name":<str>}
                             (invert: find handle whose object name == new_name, rename to old_name)
  - set_component_transform-> {"op":"set_component_transform","actor_name":<unique>,
                              "component_name":<name>,
                              "prior":{"loc":[x,y,z],"rot":[p,y,r],"scale":[x,y,z]}}
                             (invert: set the component's relative_location/rotation/scale3d back)
  - set_component_property REUSES editor_level's existing op shape (already invertible by the
                              unified undo):
                              {"op":"set_object_property","mode":"scalar",
                               "target":{"actor":<label>,"asset_path":null,"component":<comp>},
                               "path":<prop>,"prior":<json>,"restorable":<bool>}

Implemented:
  - add_component_to_actor      (write; ledgered add_component)
  - remove_component_from_actor (write; ledgered remove_component; REVERSIBLE — rename-victim-to-junk
                                 before delete frees the FName so undo re-adds with the exact name)
  - rename_component            (write; ledgered rename_component)
  - set_component_transform     (write; ledgered set_component_transform; SceneComponent only)
  - set_component_property      (write; ledgered set_object_property — reuses existing op)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py) ------------------
# NB: no ''' and no stray backslashes in this code (the handler wraps code in '''...''').
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes ('''...''')
# before exec, so snippet bodies must contain NO ''' and NO stray backslashes. All data is
# passed as base64 JSON via _exec.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer so its undo ledger is isolated from other writers.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER payload.
        Any new Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS (base64 JSON) + _session, run the body in Unreal, return MARKER payload."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Session-aware helpers (_ledger/_resolve_actor/_find_by_name/_descend/_coerce/_settable)
    # copied VERBATIM from editor_level.py so this module is self-contained.
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
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
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off", ""):
                return False
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, str):
            try:
                return int(value.strip())
            except Exception:
                try:
                    return int(float(value.strip()))
                except Exception:
                    return value
        if isinstance(value, (int, float)):
            return int(value)
        return value
    if isinstance(current, float):
        if isinstance(value, str):
            try:
                return float(value.strip())
            except Exception:
                return value
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return value
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
def _find_by_name(uniq):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_name() == uniq:
            return a
    return None
def _descend(root, comp_name, path):
    container = root
    if comp_name:
        found = None
        for c in (root.get_components_by_class(unreal.ActorComponent) or []):
            if c.get_name() == comp_name:
                found = c; break
        if found is None:
            return None, None, "component not found: %s" % comp_name
        container = found
    segs = path.split(".")
    for s in segs[:-1]:
        nxt = container.get_editor_property(s)
        if not isinstance(nxt, unreal.Object):
            return None, None, "cannot descend into non-object '%s' (struct sub-paths unsupported)" % s
        container = nxt
    return container, segs[-1], None
'''

    # Component / SubobjectDataSubsystem helpers (appended to write bodies). No '''/no backslash.
    _COMP_HELPERS = r'''
def _sods():
    return unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
def _comp_from_handle(h):
    sods = _sods()
    d = sods.k2_find_subobject_data_from_handle(h)
    if d is None:
        return None, None
    return unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d), d
def _gather(actor):
    return _sods().k2_gather_subobject_data_for_instance(actor) or []
def _handle_for_comp(actor, comp_name):
    handles = _gather(actor)
    root = handles[0] if handles else None
    for h in handles:
        o, d = _comp_from_handle(h)
        if o is not None and o.get_name() == comp_name:
            return h, d, root
    return None, None, root
def _find_comp(actor, comp_name):
    for c in (actor.get_components_by_class(unreal.ActorComponent) or []):
        if c.get_name() == comp_name:
            return c
    return None
def _resolve_comp_class(name):
    cls = getattr(unreal, name, None)
    if cls is None:
        return None
    try:
        if not issubclass(cls, unreal.ActorComponent):
            return None
    except Exception:
        return None
    return cls
def _rel_tx(comp):
    l = comp.get_editor_property("relative_location")
    r = comp.get_editor_property("relative_rotation")
    s = comp.get_editor_property("relative_scale3d")
    return {"loc": [l.x, l.y, l.z], "rot": [r.pitch, r.yaw, r.roll], "scale": [s.x, s.y, s.z]}
def _apply_rel_tx(comp, tx):
    if not tx:
        return
    if tx.get("loc") is not None:
        v = tx["loc"]; comp.set_editor_property("relative_location", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
    if tx.get("rot") is not None:
        v = tx["rot"]; comp.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2])))
    if tx.get("scale") is not None:
        v = tx["scale"]; comp.set_editor_property("relative_scale3d", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
# Property snapshot for a faithful remove->re-add inverse. Captures only RESTORABLE simple
# props (bool/int/float/str/enum/vector/rotator/color/name) + ASSET object refs. It EXCLUDES
# the three relative-transform props (captured separately) and any INSTANCE object ref (path
# contains ":" -> a scene-graph pointer like attach_parent, not an asset) so restore never
# mangles the actor's component hierarchy. Restore is diff-based (see the undo inverse).
_SNAP_SKIP = ("relative_location", "relative_rotation", "relative_scale3d")
def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for nm, val in vars(klass).items():
            if nm.startswith("__") or nm in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(nm)
            seen.add(nm)
    return names
def _snapshot_props(comp):
    import warnings as _wn
    _wn.simplefilter("ignore")
    snap = {}
    for nm in _prop_names(comp):
        if nm in _SNAP_SKIP:
            continue
        try:
            v = comp.get_editor_property(nm)
        except Exception:
            continue
        jv, restorable = _settable(v)
        if not restorable:
            continue
        if isinstance(jv, dict) and "__object__" in jv and ":" in str(jv.get("__object__") or ""):
            continue  # instance/scene-graph ref, not an asset -> do not capture
        snap[nm] = jv
    return snap
'''

    # ------------------------------------------------------------------ #
    # add_component_to_actor — add an instanced component to a level actor#
    # ------------------------------------------------------------------ #
    _ADD_BODY = _COERCE_HELPERS + _COMP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    cls = _resolve_comp_class(PARAMS["component_class"])
    if cls is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown/invalid component_class (must be an unreal.ActorComponent subclass): %s" % PARAMS["component_class"]}))
    else:
        want_name = PARAMS.get("component_name")
        existing = _find_comp(actor, want_name) if want_name else None
        if existing is not None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "a component named '%s' already exists on this actor" % want_name}))
        else:
            handles = _gather(actor)
            root = handles[0] if handles else None
            if root is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not gather subobject data for actor"}))
            else:
                params = unreal.AddNewSubobjectParams()
                params.set_editor_property("parent_handle", root)
                params.set_editor_property("new_class", cls)
                params.set_editor_property("blueprint_context", None)
                params.set_editor_property("conform_transform_to_parent", True)
                with unreal.ScopedEditorTransaction("MCP add_component_to_actor"):
                    new_handle, fail = _sods().add_new_subobject(params)
                    fail_s = str(fail)
                    comp = None
                    if not fail_s:
                        comp, _d = _comp_from_handle(new_handle)
                        final_name = comp.get_name() if comp else None
                        if want_name and comp is not None and final_name != want_name:
                            if _sods().rename_subobject(new_handle, want_name):
                                final_name = comp.get_name()
                        # optional relative transform on scene components
                        tx = PARAMS.get("transform")
                        if comp is not None and tx and isinstance(comp, unreal.SceneComponent):
                            _apply_rel_tx(comp, tx)
                if fail_s or comp is None:
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_new_subobject failed: %s" % (fail_s or "no object")}))
                else:
                    _ledger().append({"op": "add_component", "actor_name": actor.get_name(),
                        "component_name": final_name, "component_class": comp.get_class().get_name()})
                    print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                        "label": actor.get_actor_label(), "component_name": final_name,
                        "component_class": comp.get_class().get_name(),
                        "is_scene_component": isinstance(comp, unreal.SceneComponent),
                        "requested_name": want_name, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_component_to_actor(ctx, actor_name: str, component_class: str,
                               component_name: str, transform: dict = None) -> str:
        """Add a new instanced component to a placed level actor (SubobjectDataSubsystem).
        Ledgered write.

        actor_name:      actor display label (preferred) or unique internal name.
        component_class: an unreal.ActorComponent subclass name, e.g. 'PointLightComponent',
                         'StaticMeshComponent', 'BoxComponent', 'SceneComponent'.
        component_name:  desired name for the new component (renamed after creation). If the
                         name is taken the call errors; the returned 'component_name' is the
                         actual final name.
        transform:       optional {location:[x,y,z], rotation:[p,y,r], scale:[x,y,z]} — accepts
                         keys loc/rot/scale too — applied as the component's RELATIVE transform
                         (only for SceneComponent subclasses).

        Records {op: add_component, actor_name, component_name, component_class}; the inverse
        deletes exactly that component from the actor instance."""
        tx = None
        if transform:
            tx = {"loc": transform.get("location") or transform.get("loc"),
                  "rot": transform.get("rotation") or transform.get("rot"),
                  "scale": transform.get("scale")}
        params = {"actor_name": actor_name, "component_class": component_class,
                  "component_name": component_name, "transform": tx}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_component_from_actor — REVERSIBLE instanced-component delete   #
    # ------------------------------------------------------------------ #
    # Now shipped as a real ledgered write. The FName-reservation problem (a naively deleted
    # component's FName stays reserved on the actor instance, so a later re-add cannot reclaim the
    # original name) is SIDESTEPPED by renaming the victim to a junk name BEFORE deleting: that
    # frees the original FName so the inverse re-add reclaims it exactly (live-verified: naive
    # rename-back returns false; rename-victim-first -> re-added component takes the original name
    # and rename_subobject returns true). Only INSTANCE-added components are removable; native/
    # inherited/root default subobjects are refused (they cannot be re-added faithfully and
    # removing them is destructive). Inverse captured BEFORE delete: class, name, attach parent,
    # relative transform, and a diff-restorable snapshot of simple + asset-ref properties.
    _REMOVE_BODY = _COERCE_HELPERS + _COMP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp_name = PARAMS["component_name"]
    handle, data, root = _handle_for_comp(actor, comp_name)
    L = unreal.SubobjectDataBlueprintFunctionLibrary
    if handle is None or data is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % comp_name}))
    else:
        comp = L.get_associated_object(data)
        if comp is None or not L.is_component(data):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "not an actor component: %s" % comp_name}))
        elif L.is_native_component(data) or L.is_inherited_component(data) or L.is_root_component(data) or L.is_default_scene_root(data):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing to remove '%s': it is a native/inherited/root default subobject (only instance-added components can be removed reversibly)" % comp_name,
                "is_native": bool(L.is_native_component(data)), "is_inherited": bool(L.is_inherited_component(data)),
                "is_root": bool(L.is_root_component(data))}))
        else:
            is_scene = isinstance(comp, unreal.SceneComponent)
            cls_name = comp.get_class().get_name()
            attach_parent = None
            if is_scene:
                pp = comp.get_attach_parent()
                if pp is not None and pp.get_name() != comp_name:
                    attach_parent = pp.get_name()
            tx = _rel_tx(comp) if is_scene else None
            props = _snapshot_props(comp)
            junk = "_MCP_rmstash_" + comp_name
            renamed = False; deleted = 0; rolled_back = False
            with unreal.ScopedEditorTransaction("MCP remove_component_from_actor"):
                renamed = bool(_sods().rename_subobject(handle, junk))
                jh, jd, jroot = _handle_for_comp(actor, comp.get_name())
                if jh is not None and jroot is not None:
                    deleted = _sods().k2_delete_subobject_from_instance(jroot, jh)
                # safety: if the delete did not take, restore the original name so we never
                # leave the component stranded under the junk name.
                if _find_comp(actor, comp_name) is not None and renamed:
                    pass
                elif _find_comp(actor, junk) is not None:
                    _sods().rename_subobject(jh, comp_name); rolled_back = True
            gone = _find_comp(actor, comp_name) is None and _find_comp(actor, junk) is None
            if not gone:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "remove failed (delete returned %s); component left intact" % deleted,
                    "rolled_back": rolled_back}))
            else:
                _ledger().append({"op": "remove_component", "actor_name": actor.get_name(),
                    "component_class": cls_name, "component_name": comp_name,
                    "attach_parent": attach_parent, "transform": tx, "props": props})
                print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                    "label": actor.get_actor_label(), "component_name": comp_name,
                    "component_class": cls_name, "attach_parent": attach_parent,
                    "was_scene_component": is_scene, "captured_props": len(props),
                    "note": "instanced component removed; undo re-adds it with the same name/class/transform/props",
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_component_from_actor(ctx, actor_name: str, component_name: str) -> str:
        """Remove an instance-added component from a placed level actor. Ledgered, REVERSIBLE write.

        actor_name:     actor display label (preferred) or unique internal name.
        component_name: the component to remove. Must be an INSTANCE-added component; native,
                        inherited, or root default subobjects are refused (they can't be re-added
                        faithfully). Errors if the component is not found.

        Uses SubobjectDataSubsystem. To keep the inverse faithful, the component is renamed to a
        junk name BEFORE deletion — this frees its FName so `undo` can re-add it with the EXACT
        original name (a naive delete reserves the name and breaks name-keyed undo).

        Records {op: remove_component, actor_name, component_class, component_name, attach_parent,
        transform, props}; the inverse re-adds the component (same class, under the same parent),
        restores the original name + relative transform, and diff-restores the captured simple/
        asset-ref properties. Opaque struct-valued properties are not captured (best-effort, matching
        the rest of the reflection layer)."""
        params = {"actor_name": actor_name, "component_name": component_name}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_component — rename a component on a level actor               #
    # ------------------------------------------------------------------ #
    _RENAME_BODY = _COERCE_HELPERS + _COMP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    old = PARAMS["old_name"]; new = PARAMS["new_name"]
    if _find_comp(actor, new) is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "a component named '%s' already exists on this actor" % new}))
    else:
        handle, data, root = _handle_for_comp(actor, old)
        if handle is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % old}))
        else:
            comp = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(data)
            ok = False
            with unreal.ScopedEditorTransaction("MCP rename_component"):
                ok = _sods().rename_subobject(handle, new)
            final_name = comp.get_name()
            if not ok or final_name != new:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "rename failed (returned %s; name now '%s')" % (ok, final_name)}))
            else:
                _ledger().append({"op": "rename_component", "actor_name": actor.get_name(),
                    "old_name": old, "new_name": new})
                print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                    "label": actor.get_actor_label(), "old_name": old, "new_name": final_name,
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_component(ctx, actor_name: str, old_name: str, new_name: str) -> str:
        """Rename a component on a placed level actor. Ledgered write.

        actor_name: actor display label (preferred) or unique internal name.
        old_name:   the component's current name.
        new_name:   the desired new name (errors if already used on this actor).

        Uses SubobjectDataSubsystem.rename_subobject. Records {op: rename_component,
        actor_name, old_name, new_name}; the inverse renames new_name back to old_name."""
        params = {"actor_name": actor_name, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_component_transform — set a scene component's relative transform #
    # ------------------------------------------------------------------ #
    _SET_TX_BODY = _COERCE_HELPERS + _COMP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _find_comp(actor, PARAMS["component_name"])
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % PARAMS["component_name"]}))
    elif not isinstance(comp, unreal.SceneComponent):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "component '%s' is not a SceneComponent (no transform)" % PARAMS["component_name"]}))
    else:
        prior = _rel_tx(comp)
        newtx = {"loc": PARAMS.get("location"), "rot": PARAMS.get("rotation"), "scale": PARAMS.get("scale")}
        with unreal.ScopedEditorTransaction("MCP set_component_transform"):
            _apply_rel_tx(comp, newtx)
        after = _rel_tx(comp)
        _ledger().append({"op": "set_component_transform", "actor_name": actor.get_name(),
            "component_name": PARAMS["component_name"], "prior": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
            "label": actor.get_actor_label(), "component_name": PARAMS["component_name"],
            "before": prior, "after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_component_transform(ctx, actor_name: str, component_name: str,
                                location: list = None, rotation: list = None,
                                scale: list = None) -> str:
        """Set a scene component's RELATIVE transform on a placed level actor. Partial update —
        only the parts you pass are changed. Ledgered write.

        actor_name:     actor display label (preferred) or unique internal name.
        component_name: the SceneComponent's name.
        location:       [x, y, z] relative location.
        rotation:       [pitch, yaw, roll] relative rotation (degrees).
        scale:          [x, y, z] relative scale.

        Records {op: set_component_transform, actor_name, component_name, prior:{loc,rot,scale}}
        capturing the full prior relative transform so `undo` restores it exactly."""
        params = {"actor_name": actor_name, "component_name": component_name,
                  "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SET_TX_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_component_property — reflected set on a named component          #
    # (reuses editor_level's set_object_property ledger op — invertible)  #
    # ------------------------------------------------------------------ #
    _SET_PROP_BODY = _COERCE_HELPERS + _COMP_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _find_comp(actor, PARAMS["component_name"])
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % PARAMS["component_name"]}))
    else:
        cont, final, err = _descend(comp, None, PARAMS["property_path"])
        if err:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
        else:
            try:
                prior_raw = cont.get_editor_property(final); have = True
            except Exception:
                have = False
            if not have:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property: %s" % final}))
            else:
                prior_json, restorable = _settable(prior_raw)
                newv = _coerce(prior_raw, PARAMS["value"])
                with unreal.ScopedEditorTransaction("MCP set_component_property"):
                    cont.set_editor_property(final, newv)
                after_json, _u = _settable(cont.get_editor_property(final))
                _ledger().append({"op": "set_object_property", "mode": "scalar",
                    "target": {"actor": actor.get_actor_label(), "asset_path": None,
                               "component": PARAMS["component_name"]},
                    "path": PARAMS["property_path"], "prior": prior_json, "restorable": restorable})
                print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                    "label": actor.get_actor_label(), "component": PARAMS["component_name"],
                    "path": PARAMS["property_path"], "before": prior_json, "after": after_json,
                    "restorable": restorable, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_component_property(ctx, actor_name: str, component_name: str,
                               property: str, value=None) -> str:
        """Set a reflected property on a named component of a placed level actor. Ledgered write.

        actor_name:     actor display label (preferred) or unique internal name.
        component_name: the component to target.
        property:       property name, or dotted path through nested component objects
                        (struct sub-paths are refused, matching set_actor_property).
        value:          new value; types are coerced from the current value (object/asset props
                        accept an asset path string; vectors/rotators/colors accept [..] lists;
                        enums accept the member name; bools/numbers/strings pass through).

        Records the EXISTING editor_level op {op: set_object_property, mode: scalar,
        target:{actor,component}, path, prior, restorable} so the unified `undo` already
        inverts it (the prior value is captured and re-applied)."""
        params = {"actor_name": actor_name, "component_name": component_name,
                  "property_path": property, "value": value}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
