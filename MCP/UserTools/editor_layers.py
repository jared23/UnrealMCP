"""UserTools :: Editor / Layers  (spec: docs/spec/editor.md — "Layers subsystem")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and a slim _COERCE_HELPERS block (session-aware _ledger(), _resolve_actor,
_find_by_name).

The Layers system is unreal.get_editor_subsystem(unreal.LayersSubsystem). Live-probed on
UE 5.8.1 (see API notes below) — several method names differ from older docs.

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines on stdout can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records an inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]
(keyed by PARAMS["_session"], injected by _exec). Reversibility is REQUIRED for every ledgered
write. This module defines NO `undo` tool (like the other Agent-A modules); the coordinator folds
matching inverse branches into editor_level.py's unified `undo`.

Implemented:
  - list_layers            (read-only)  — all layer names + per-layer actor counts
  - get_actor_layers       (read-only)  — the layers an actor belongs to (actor.Layers array)
  - create_layer           (write; ledgered create_layer)
  - delete_layer           (write; ledgered delete_layer; captures member actors first)
  - add_actors_to_layer    (write; ledgered add_actors_to_layer; only actually-added actors)
  - remove_actors_from_layer (write; ledgered remove_actors_from_layer; only actually-removed)
  - set_layer_visibility   (write; EPHEMERAL view toggle — NOT ledgered, see note)

NEW ledger op schemas (for editor_level.undo integration; resolve subsystem via
unreal.get_editor_subsystem(unreal.LayersSubsystem), actors via _find_by_name):
  create_layer:            {"op":"create_layer","layer_name":<str>}
     -> invert: ls.delete_layer(unreal.Name(layer_name))
  delete_layer:            {"op":"delete_layer","layer_name":<str>,"members":[<unique actor name>,...]}
     -> invert: ls.create_layer(unreal.Name(layer_name));
                actors=[_find_by_name(n) for n in members if _find_by_name(n)];
                if actors: ls.add_actors_to_layer(actors, unreal.Name(layer_name))
        (NOTE: prior visibility is NOT restored — recreated visible/default; layer visibility is a
         view-only flag that is unreadable from Python, see set_layer_visibility.)
  add_actors_to_layer:     {"op":"add_actors_to_layer","layer_name":<str>,"actor_names":[<unique>,...]}
     -> invert: ls.remove_actors_from_layer([_find_by_name(n) ...], unreal.Name(layer_name))
        (actor_names holds ONLY actors whose membership actually changed.)
  remove_actors_from_layer:{"op":"remove_actors_from_layer","layer_name":<str>,"actor_names":[<unique>,...]}
     -> invert: ls.add_actors_to_layer([_find_by_name(n) ...], unreal.Name(layer_name))
        (actor_names holds ONLY actors whose membership actually changed.)

INTENTIONALLY NOT LEDGERED (ephemeral): set_layer_visibility. The Layer's bIsVisible is a
viewport-only flag ("whether actors associated with the layer are visible in the viewport"),
NOT gameplay/level content, and it is PROTECTED/unreadable from Python so a prior value cannot
be captured for a faithful inverse. Mirroring editor_viewport.py's ephemeral ops, it is applied
without a ledger entry and reported as such.

API notes (live-verified on UE 5.8.1):
  * Subsystem: unreal.get_editor_subsystem(unreal.LayersSubsystem). There is NO get_all_layers().
    Enumerate via ls.add_all_layer_names_to() -> [FName] and ls.add_all_layers_to() -> [Layer].
    Despite the C++ out-param name, the Python bindings take NO argument and RETURN the array.
  * ls.create_layer(FName) -> Layer ; ls.delete_layer(FName) ; ls.get_layer(FName) -> Layer|None ;
    ls.get_actors_from_layer(FName) -> [Actor] ; ls.add_actors_to_layer([Actor], FName) ;
    ls.remove_actors_from_layer([Actor], FName) ; ls.set_layer_visibility(FName, bool).
  * The Layer UObject's LayerName / bIsVisible / ActorStats are all PROTECTED — unreadable from
    Python (get_editor_property raises "is protected"), even though MCPReflectionLibrary can list
    their metadata. So we read layer names from add_all_layer_names_to() and actor membership from
    the ACTOR side (actor.get_editor_property("layers") -> TArray<FName>), never from the Layer.
  * WORLD PARTITION LIMITATION (this project's Lvl_ThirdPerson): the level is World Partition
    (actors are external / package /Game/__ExternalActors__, a WorldDataLayers actor is present).
    The legacy Layers subsystem cannot associate World-Partition actors with layers:
    is_actor_valid_for_layer(actor) is False and add_actors_to_layer returns without changing the
    actor's Layers array. So create/delete of (empty) layer OBJECTS works and is fully reversible,
    but add/remove actor membership is a verified no-op in this world (0 changed, 0 ledgered).
    Data Layers (DataLayerEditorSubsystem) is the operative grouping system for WP levels.
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
# before exec, so any ''' or backslash in the code corrupts it. Pass all data as base64.


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

    # Slim session-aware helpers (subset of editor_level.py's _COERCE_HELPERS). No '''/no backslash.
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    # Per-session undo stack so concurrent agents never pop each other's entries.
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
'''

    # Layers-subsystem helpers (appended to bodies that need them). No '''/no backslash.
    _LAYER_HELPERS = r'''
def _layers():
    return unreal.get_editor_subsystem(unreal.LayersSubsystem)
def _layer_names(ls):
    try:
        return [str(n) for n in (ls.add_all_layer_names_to() or [])]
    except Exception:
        return []
def _layer_exists(ls, name):
    return name in _layer_names(ls)
def _actor_layer_set(actor):
    try:
        return set(str(x) for x in (actor.get_editor_property("layers") or []))
    except Exception:
        return set()
'''

    # ------------------------------------------------------------------ #
    # list_layers — all layer names + per-layer actor counts (read-only) #
    # ------------------------------------------------------------------ #
    _LIST_LAYERS_BODY = _LAYER_HELPERS + r'''
import unreal, json
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
flt = PARAMS.get("filter")
ls = _layers()
names = _layer_names(ls)
layers = []
total_assoc = 0
for nm in sorted(names):
    if flt and flt.lower() not in nm.lower():
        continue
    actors = _try(lambda nm=nm: ls.get_actors_from_layer(unreal.Name(nm)), []) or []
    cnt = len(actors)
    total_assoc += cnt
    layers.append({"name": nm, "actor_count": cnt})
print("@@UMCP@@" + json.dumps({"status": "success", "layer_count": len(layers),
    "total_layers_in_level": len(names), "filter": flt,
    "associated_actor_slots": total_assoc, "layers": layers,
    "note": "layer visibility is not reported (Layer.bIsVisible is protected/unreadable from Python)"}))
'''

    @mcp.tool()
    def list_layers(ctx, filter: str = None) -> str:
        """List every named layer in the active level, each with a count of actors currently
        associated with it. Read-only.

        Layer names come from LayersSubsystem.add_all_layer_names_to(); per-layer actor counts
        from get_actors_from_layer(name). Visibility is NOT reported because the Layer object's
        bIsVisible flag is protected and cannot be read from Python.

        filter: optional case-insensitive substring; only layer names containing it are returned.

        NOTE: in a World Partition level (like this project's Lvl_ThirdPerson) legacy layers cannot
        hold World-Partition actors, so counts will be 0 even if you add actors — see the module docstring."""
        try:
            return json.dumps(_exec(_LIST_LAYERS_BODY, {"filter": filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actor_layers — the layers an actor belongs to (read-only)      #
    # ------------------------------------------------------------------ #
    _GET_ACTOR_LAYERS_BODY = _COERCE_HELPERS + _LAYER_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    layers = sorted(_actor_layer_set(actor))
    valid = None
    try:
        valid = bool(_layers().is_actor_valid_for_layer(actor))
    except Exception:
        valid = None
    print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
        "label": actor.get_actor_label(), "layer_count": len(layers), "layers": layers,
        "is_valid_for_layer": valid,
        "note": "membership is read from the actor's Layers array; is_valid_for_layer=False means this actor (e.g. a World Partition external actor) cannot be associated with legacy layers"}))
'''

    @mcp.tool()
    def get_actor_layers(ctx, actor_name: str) -> str:
        """List the layers a given actor belongs to. Read-only.

        actor_name: the actor's display label (preferred) or unique internal name.

        Membership is read from the actor's own Layers array (actor.get_editor_property('layers')),
        which is the reliable source (the Layer object's members are protected). Also reports
        is_valid_for_layer: if False, the actor (e.g. a World Partition external actor) cannot be
        associated with legacy layers at all."""
        try:
            return json.dumps(_exec(_GET_ACTOR_LAYERS_BODY, {"actor_name": actor_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_layer — create a new empty layer (ledgered write)           #
    # ------------------------------------------------------------------ #
    _CREATE_LAYER_BODY = _COERCE_HELPERS + _LAYER_HELPERS + r'''
name = PARAMS["layer_name"]
ls = _layers()
if _layer_exists(ls, name):
    print("@@UMCP@@" + json.dumps({"status": "exists", "layer_name": name,
        "message": "layer already exists; not created and not ledgered",
        "ledger_depth": len(_ledger())}))
else:
    with unreal.ScopedEditorTransaction("MCP create_layer"):
        ls.create_layer(unreal.Name(name))
    created = _layer_exists(ls, name)
    if created:
        _ledger().append({"op": "create_layer", "layer_name": name})
    print("@@UMCP@@" + json.dumps({"status": "success" if created else "error",
        "layer_name": name, "created": created,
        "layer_count_now": len(_layer_names(ls)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_layer(ctx, layer_name: str) -> str:
        """Create a new (empty) layer in the active level. Ledgered write.

        layer_name: the name of the layer to create.

        Uses LayersSubsystem.create_layer(Name). If a layer with that name already exists it is
        left untouched and NOT ledgered (status='exists'). Records {op: create_layer, layer_name};
        the inverse deletes the layer."""
        try:
            return json.dumps(_exec(_CREATE_LAYER_BODY, {"layer_name": layer_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_layer — delete a layer, capturing members first (ledgered)  #
    # ------------------------------------------------------------------ #
    _DELETE_LAYER_BODY = _COERCE_HELPERS + _LAYER_HELPERS + r'''
name = PARAMS["layer_name"]
ls = _layers()
if not _layer_exists(ls, name):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "layer not found: %s" % name,
        "available_layers": _layer_names(ls)}))
else:
    members = []
    try:
        members = [a.get_name() for a in (ls.get_actors_from_layer(unreal.Name(name)) or []) if a]
    except Exception:
        members = []
    with unreal.ScopedEditorTransaction("MCP delete_layer"):
        ls.delete_layer(unreal.Name(name))
    gone = not _layer_exists(ls, name)
    if gone:
        _ledger().append({"op": "delete_layer", "layer_name": name, "members": members})
    print("@@UMCP@@" + json.dumps({"status": "success" if gone else "error",
        "layer_name": name, "deleted": gone, "captured_members": members,
        "member_count": len(members), "layer_count_now": len(_layer_names(ls)),
        "ledger_depth": len(_ledger()),
        "note": "undo recreates the layer + re-adds captured members (prior visibility not restored; recreated visible)"}))
'''

    @mcp.tool()
    def delete_layer(ctx, layer_name: str) -> str:
        """Delete a layer from the active level. Ledgered write (member actors captured first).

        layer_name: the name of the layer to delete.

        Before deleting, captures the layer's current member actors (get_actors_from_layer) as
        unique internal names, so the inverse can recreate the layer AND re-add exactly those
        actors. Records {op: delete_layer, layer_name, members:[...]}.

        CAVEAT: the layer's prior visibility flag is not captured (Layer.bIsVisible is unreadable
        from Python), so undo recreates the layer visible/default — visibility is a view-only flag."""
        try:
            return json.dumps(_exec(_DELETE_LAYER_BODY, {"layer_name": layer_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_actors_to_layer — associate actors with a layer (ledgered)     #
    # ------------------------------------------------------------------ #
    _ADD_ACTORS_BODY = _COERCE_HELPERS + _LAYER_HELPERS + r'''
name = PARAMS["layer_name"]
req = PARAMS.get("actor_names") or []
ls = _layers()
if not _layer_exists(ls, name):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "layer not found: %s (create it first with create_layer)" % name,
        "available_layers": _layer_names(ls)}))
else:
    resolved = []
    not_found = []
    for ident in req:
        a = _resolve_actor(ident)
        if a is None:
            not_found.append(ident)
        else:
            resolved.append(a)
    before = {a.get_name(): _actor_layer_set(a) for a in resolved}
    if resolved:
        with unreal.ScopedEditorTransaction("MCP add_actors_to_layer"):
            ls.add_actors_to_layer(resolved, unreal.Name(name))
    actually_added = []
    for a in resolved:
        after = _actor_layer_set(a)
        if name in after and name not in before.get(a.get_name(), set()):
            actually_added.append(a.get_name())
    if actually_added:
        _ledger().append({"op": "add_actors_to_layer", "layer_name": name, "actor_names": actually_added})
    print("@@UMCP@@" + json.dumps({"status": "success", "layer_name": name,
        "requested": len(req), "matched": len(resolved), "not_found": not_found,
        "added_count": len(actually_added), "added": actually_added,
        "ledger_depth": len(_ledger()),
        "note": "only actors whose membership actually changed are ledgered; in a World Partition level legacy layers cannot hold external actors so this may add 0"}))
'''

    @mcp.tool()
    def add_actors_to_layer(ctx, layer_name: str, actor_names: list) -> str:
        """Associate one or more actors with an existing layer. Ledgered write.

        layer_name:  the target layer (must already exist — use create_layer first).
        actor_names: list of actor display labels (preferred) or unique internal names.

        Uses LayersSubsystem.add_actors_to_layer([actors], Name). Only the actors whose Layers
        membership ACTUALLY changed are recorded {op: add_actors_to_layer, layer_name, actor_names:
        [...]}, so the inverse removes exactly those. Actors that were already members, or that
        cannot be added (World Partition external actors), are not ledgered.

        NOTE: in a World Partition level (this project's Lvl_ThirdPerson) this is a verified no-op
        (adds 0) because legacy layers cannot hold external actors."""
        params = {"layer_name": layer_name, "actor_names": actor_names}
        try:
            return json.dumps(_exec(_ADD_ACTORS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_actors_from_layer — dissociate actors from a layer (ledgered)#
    # ------------------------------------------------------------------ #
    _REMOVE_ACTORS_BODY = _COERCE_HELPERS + _LAYER_HELPERS + r'''
name = PARAMS["layer_name"]
req = PARAMS.get("actor_names") or []
ls = _layers()
if not _layer_exists(ls, name):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "layer not found: %s" % name,
        "available_layers": _layer_names(ls)}))
else:
    resolved = []
    not_found = []
    for ident in req:
        a = _resolve_actor(ident)
        if a is None:
            not_found.append(ident)
        else:
            resolved.append(a)
    before = {a.get_name(): _actor_layer_set(a) for a in resolved}
    if resolved:
        with unreal.ScopedEditorTransaction("MCP remove_actors_from_layer"):
            ls.remove_actors_from_layer(resolved, unreal.Name(name))
    actually_removed = []
    for a in resolved:
        after = _actor_layer_set(a)
        if name not in after and name in before.get(a.get_name(), set()):
            actually_removed.append(a.get_name())
    if actually_removed:
        _ledger().append({"op": "remove_actors_from_layer", "layer_name": name, "actor_names": actually_removed})
    print("@@UMCP@@" + json.dumps({"status": "success", "layer_name": name,
        "requested": len(req), "matched": len(resolved), "not_found": not_found,
        "removed_count": len(actually_removed), "removed": actually_removed,
        "ledger_depth": len(_ledger()),
        "note": "only actors whose membership actually changed are ledgered"}))
'''

    @mcp.tool()
    def remove_actors_from_layer(ctx, layer_name: str, actor_names: list) -> str:
        """Remove one or more actors from a layer. Ledgered write.

        layer_name:  the target layer (must exist).
        actor_names: list of actor display labels (preferred) or unique internal names.

        Uses LayersSubsystem.remove_actors_from_layer([actors], Name). Only the actors whose Layers
        membership ACTUALLY changed are recorded {op: remove_actors_from_layer, layer_name,
        actor_names: [...]}, so the inverse re-adds exactly those."""
        params = {"layer_name": layer_name, "actor_names": actor_names}
        try:
            return json.dumps(_exec(_REMOVE_ACTORS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_layer_visibility — EPHEMERAL viewport view toggle (NOT ledgered)#
    # ------------------------------------------------------------------ #
    _SET_VIS_BODY = _LAYER_HELPERS + r'''
import unreal, json
name = PARAMS["layer_name"]
visible = bool(PARAMS.get("visible", True))
ls = _layers()
if not _layer_exists(ls, name):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "layer not found: %s" % name,
        "available_layers": _layer_names(ls)}))
else:
    ls.set_layer_visibility(unreal.Name(name), visible)
    print("@@UMCP@@" + json.dumps({"status": "success", "layer_name": name, "visible": visible,
        "ledgered": False,
        "note": "EPHEMERAL viewport view toggle, NOT ledgered (Layer.bIsVisible is a view-only flag and is unreadable from Python, so no faithful prior can be captured; mirrors editor_viewport ephemeral ops)"}))
'''

    @mcp.tool()
    def set_layer_visibility(ctx, layer_name: str, visible: bool = True) -> str:
        """Show or hide a layer's actors in the editor viewport. EPHEMERAL — NOT ledgered.

        layer_name: the target layer (must exist).
        visible:    True to show, False to hide (default True).

        Uses LayersSubsystem.set_layer_visibility(Name, bool). This is a viewport-only view toggle
        (like editor_viewport's camera/focus ops), it does not change level/gameplay content, and
        the Layer's bIsVisible flag is protected/unreadable from Python so a prior value cannot be
        captured for a faithful inverse. It is therefore applied WITHOUT a ledger entry. To revert,
        call set_layer_visibility again with the desired state."""
        params = {"layer_name": layer_name, "visible": visible}
        try:
            return json.dumps(_exec(_SET_VIS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
