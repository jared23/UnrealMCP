"""UserTools :: Materials (slot-level assignment)  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8).
Follows the editor_level.py gold-standard conventions VERBATIM: snippets print
@@UMCP@@<json> on one line; params are injected as base64 JSON via _exec; _query wraps
every snippet with Output-Log auto-capture and surfaces new Warning/Error lines as
result["_log_warnings"]; per-session undo ledger at builtins._UMCP_LEDGERS[session]
(session injected via PARAMS["_session"] from _exec).

Implemented (both WRITE + ledgered under ONE op name "set_material"):
  - set_actor_material   set one element-slot override on a StaticMeshComponent
  - set_materials_batch  set many slots (explicit list, or all slots -> one material)
                         in a SINGLE ScopedEditorTransaction + ONE ledger entry

Material override model (UE 5.8): UStaticMeshComponent.get_material(i) returns the
component's override material at slot i if one is set, otherwise the StaticMesh's default
material for that slot (it is NOT None when there is no override). set_material(i, mat)
writes an override. We capture the PRIOR effective material path (or None only if the
slot truly resolves to no material) so the inverse re-applies it. NB: reverting a slot
that had NO explicit override re-creates an override equal to the former default — visually
identical, and for a throwaway/undo it is symmetric. See report note.

LEDGER op schema (NEW inverse op for the shared editor_level.undo to learn — ONE op name
covers BOTH commands here):
  {"op":"set_material","actor_name":<unique>,"component_name":<name>,
   "slots":[{"index":<int>,"prior":<material_path|null>}, ...]}
  invert (LIFO): for each slot -> comp.set_material(index, load_asset(prior) if prior else None)
No `undo` tool is defined here (defers to editor_level.py's unified agent-scoped undo).
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


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
def _mesh_comp(actor, comp_name):
    # Return (component, error). Default = the actor's 'static_mesh_component' UPROPERTY
    # (present on StaticMeshActor); else the named StaticMeshComponent; else the first SMC.
    smcs = actor.get_components_by_class(unreal.StaticMeshComponent) or []
    if comp_name:
        for c in smcs:
            if c.get_name() == comp_name:
                return c, None
        return None, "StaticMeshComponent not found: %s (available: %s)" % (
            comp_name, [c.get_name() for c in smcs])
    dc = None
    try:
        dc = actor.get_editor_property("static_mesh_component")
    except Exception:
        dc = None
    if dc is not None:
        return dc, None
    if smcs:
        return smcs[0], None
    return None, "actor has no StaticMeshComponent"
def _mat_path(m):
    return m.get_path_name() if m else None
'''

    # ------------------------------------------------------------------ #
    # set_actor_material — one slot override on a StaticMeshComponent      #
    # ------------------------------------------------------------------ #
    _SET_ACTOR_MATERIAL_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor"]}))
else:
    comp, cerr = _mesh_comp(actor, PARAMS.get("component"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        slot = int(PARAMS["slot"])
        num = comp.get_num_materials()
        if slot < 0 or slot >= num:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "slot %d out of range (component has %d slots)" % (slot, num)}))
        else:
            mat = unreal.EditorAssetLibrary.load_asset(PARAMS["material_path"])
            if mat is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "could not load material asset: %s" % PARAMS["material_path"]}))
            elif not isinstance(mat, unreal.MaterialInterface):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "asset is not a MaterialInterface (got %s): %s" % (
                        mat.get_class().get_name(), PARAMS["material_path"])}))
            else:
                prior_path = _mat_path(comp.get_material(slot))
                with unreal.ScopedEditorTransaction("MCP set_actor_material"):
                    comp.set_material(slot, mat)
                after_path = _mat_path(comp.get_material(slot))
                _ledger().append({"op": "set_material", "actor_name": actor.get_name(),
                    "component_name": comp.get_name(),
                    "slots": [{"index": slot, "prior": prior_path}]})
                print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
                    "label": actor.get_actor_label(), "component": comp.get_name(),
                    "slot": slot, "before": prior_path, "after": after_path,
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_material(ctx, actor: str, slot: int, material_path: str,
                           component: str = None) -> str:
        """Set the material override on a StaticMeshComponent element slot of a placed actor.

        actor:         actor display label (preferred) or unique internal name.
        slot:          element slot index (0-based) to override.
        material_path: asset path of the MaterialInterface to assign
                       (e.g. '/Engine/BasicShapes/BasicShapeMaterial').
        component:     optional StaticMeshComponent name to target; default is the actor's
                       'static_mesh_component' (or its first StaticMeshComponent).

        Ledgered write (op 'set_material'): the PRIOR effective material at that slot is
        captured so `undo` re-applies it. The mutation runs inside a ScopedEditorTransaction."""
        params = {"actor": actor, "slot": slot, "material_path": material_path,
                  "component": component}
        try:
            return json.dumps(_exec(_SET_ACTOR_MATERIAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_materials_batch — many slots in ONE transaction + ONE ledger    #
    # ------------------------------------------------------------------ #
    _SET_MATERIALS_BATCH_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS.get("actor"))
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS.get("actor")}))
else:
    comp, cerr = _mesh_comp(actor, PARAMS.get("component"))
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        num = comp.get_num_materials()
        assignments = PARAMS.get("assignments")
        all_slots = bool(PARAMS.get("all_slots"))
        material = PARAMS.get("material")
        # Build the (slot, material_path) work list.
        pairs = []
        err = None
        if all_slots:
            if not material:
                err = "all_slots requires 'material'"
            else:
                pairs = [(i, material) for i in range(num)]
        elif assignments:
            for a in assignments:
                if not isinstance(a, dict) or ("slot" not in a):
                    err = "each assignment needs {slot, material_path}"; break
                mp = a.get("material_path") or a.get("material")
                if mp is None:
                    err = "assignment for slot %s missing material_path" % a.get("slot"); break
                pairs.append((int(a["slot"]), mp))
        else:
            err = "provide 'assignments' [{slot, material_path}], or all_slots=True + material"
        if err:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
        else:
            # Pre-validate ranges + pre-load materials BEFORE mutating (atomic-or-nothing).
            loaded = {}
            for slot, mp in pairs:
                if slot < 0 or slot >= num:
                    err = "slot %d out of range (component has %d slots)" % (slot, num); break
                if mp not in loaded:
                    m = unreal.EditorAssetLibrary.load_asset(mp)
                    if m is None:
                        err = "could not load material asset: %s" % mp; break
                    if not isinstance(m, unreal.MaterialInterface):
                        err = "asset is not a MaterialInterface (got %s): %s" % (m.get_class().get_name(), mp); break
                    loaded[mp] = m
            if err:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
            else:
                touched = []
                applied = []
                with unreal.ScopedEditorTransaction("MCP set_materials_batch"):
                    for slot, mp in pairs:
                        prior_path = _mat_path(comp.get_material(slot))
                        comp.set_material(slot, loaded[mp])
                        touched.append({"index": slot, "prior": prior_path})
                        applied.append({"slot": slot, "before": prior_path,
                                        "after": _mat_path(comp.get_material(slot))})
                _ledger().append({"op": "set_material", "actor_name": actor.get_name(),
                    "component_name": comp.get_name(), "slots": touched})
                print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
                    "label": actor.get_actor_label(), "component": comp.get_name(),
                    "num_slots": num, "assigned": applied, "count": len(applied),
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_materials_batch(ctx, assignments: list = None, actor: str = None,
                            component: str = None, all_slots: bool = False,
                            material: str = None) -> str:
        """Assign materials to multiple element slots of one actor's StaticMeshComponent in a
        SINGLE transaction (recorded as ONE ledger entry, so one `undo` reverts them all).

        Two modes:
          - assignments: list of {slot, material_path} — set each named slot individually.
          - all_slots=True + material: set EVERY slot on the component to that one material.

        actor:      actor display label (preferred) or unique internal name.
        component:  optional StaticMeshComponent name; default 'static_mesh_component' / first SMC.

        Atomic-or-nothing: slot ranges and material assets are validated/loaded before any
        mutation, so a bad slot or path aborts the whole batch with no partial change.
        Ledgered write (op 'set_material'): the PRIOR material of every touched slot is captured
        so `undo` restores them all in one step."""
        params = {"assignments": assignments, "actor": actor, "component": component,
                  "all_slots": all_slots, "material": material}
        try:
            return json.dumps(_exec(_SET_MATERIALS_BATCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
