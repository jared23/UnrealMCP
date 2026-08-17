"""UserTools :: World Partition / Data Layers  (spec: docs/spec/editor.md — "Data Layers")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
the World-Partition counterpart to editor_layers.py. It mirrors editor_level.py's proven
conventions VERBATIM: base64-injected PARAMS, the Output-Log auto-capture wrapper, the
@@UMCP@@ marker, the session-aware per-agent undo ledger, and a slim _COERCE_HELPERS block
(session-aware _ledger(), _resolve_actor, _find_by_name).

The operative grouping system for a World Partition level is the Data Layers system, reached
via  unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)  (NOT the legacy
LayersSubsystem, which cannot associate WP external actors — see editor_layers.py). Data Layers
are objects (UDataLayerInstance) that live under the level's WorldDataLayers actor; each
instance is backed by a UDataLayerAsset (a content asset). All add/remove/get/visibility/delete
calls operate on the DataLayerInstance object (not a name/asset), so this module resolves a
user-supplied identifier (asset path / asset name / short name / full name) to the live instance.

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that marker and
parses the JSON after it, so stray engine log lines on stdout can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records an inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]
(keyed by PARAMS["_session"], injected by _exec). Reversibility is REQUIRED for every ledgered
write. This module defines NO `undo` tool (like the other Agent-B modules); the coordinator folds
the matching inverse branches into editor_level.py's unified `undo`.

Implemented:
  - list_data_layers        (read-only)  — every DataLayerInstance + asset, runtime/editor state,
                                           visibility, initial state, per-layer actor count
  - get_actor_data_layers   (read-only)  — which data layers an actor belongs to
  - add_actors_to_data_layer   (write; ledgered add_actors_to_data_layer; only actually-added)
  - remove_actors_from_data_layer (write; ledgered remove_actors_from_data_layer; only removed)
  - set_data_layer_visibility  (write; EPHEMERAL editor-view toggle — NOT ledgered, see note)

DEFERRED (see report):
  - create_data_layer / delete_data_layer. A DataLayerInstance requires a UDataLayerAsset. Creating
    a NEW DataLayerAsset needs an asset factory (AssetTools.create_asset / a factory) which risks a
    modal against the shared editor — DEFERRED per HARD RULE. (Creating an INSTANCE from an ALREADY-
    EXISTING DataLayerAsset via DataLayerEditorSubsystem.create_data_layer_instance is non-modal and
    reversible via delete_data_layer; it is used, guarded, only for internal write validation and is
    not shipped as a user tool this round.)

NEW ledger op schemas (for editor_level.undo integration; resolve the subsystem via
unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem), the instance via the stored
asset path / names, actors via _find_by_name):
  add_actors_to_data_layer:
      {"op":"add_actors_to_data_layer","dl_asset":<str|null>,"dl_short":<str|null>,
       "dl_full":<str|null>,"actor_names":[<unique actor name>,...]}
     -> invert: di = <resolve instance by dl_asset|dl_full|dl_short>;
                actors = [_find_by_name(n) for n in actor_names if _find_by_name(n)];
                if di and actors: dls.remove_actors_from_data_layer(actors, di)
        (actor_names holds ONLY actors whose membership actually changed.)
  remove_actors_from_data_layer:
      {"op":"remove_actors_from_data_layer","dl_asset":<str|null>,"dl_short":<str|null>,
       "dl_full":<str|null>,"actor_names":[<unique>,...]}
     -> invert: di = <resolve instance>;
                actors = [_find_by_name(n) for n in actor_names if _find_by_name(n)];
                if di and actors: dls.add_actors_to_data_layer(actors, di)
        (actor_names holds ONLY actors whose membership actually changed.)

INTENTIONALLY NOT LEDGERED (ephemeral): set_data_layer_visibility. A DataLayerInstance's IsVisible
is a per-editor-session VIEW flag (it is not saved with the level and resets on reload), the same
class of transient editor state as editor_viewport's camera ops and editor_layers' visibility. It
is applied WITHOUT a ledger entry and reported as such (the response DOES include the readable
before/after, unlike legacy layers, but the state is still ephemeral). The PERSISTENT counterpart
would be set_data_layer_is_initially_visible (a saved property), which is not toggled here.

API notes (live-verified on UE 5.8.1, DataLayerEditorSubsystem):
  * Enumerate ALL instances with dls.get_all_data_layers() -> Array[DataLayerInstance] (NO argument).
    dls.get_data_layer_instances(assets) and dls.get_data_layer_instance(asset) MAP assets->instances
    (they REQUIRE a DataLayerAsset argument — they are not enumerators).
  * add/remove/get all take the INSTANCE object:
      dls.add_actors_to_data_layer(actors:[Actor], data_layer:DataLayerInstance) -> bool
          (True iff >=1 actor was actually added; False if all already belonged)
      dls.remove_actors_from_data_layer(actors:[Actor], data_layer:DataLayerInstance) -> bool
      dls.get_actors_from_data_layer(data_layer:DataLayerInstance) -> Array[Actor]
      dls.is_actor_valid_for_data_layer_instances(actor, [DataLayerInstance]) -> bool
      dls.set_data_layer_visibility(data_layer:DataLayerInstance, is_visible:bool)
      dls.delete_data_layer(data_layer:DataLayerInstance)
      dls.create_data_layer_instance(parameters:DataLayerCreationParameters) -> DataLayerInstance
  * THIS LEVEL (Lvl_ThirdPerson): a WorldDataLayers actor is present but get_all_data_layers()
    returns 0 instances — there are NO data layers to test against and no DataLayerAsset is
    instanced. Creating one requires an asset (modal risk) so, exactly like editor_layers.py's WP
    no-op case, add/remove are implemented correct + reversible-by-construction and their guard/
    read paths validated, but positive add/remove is only demonstrated if an existing DataLayerAsset
    is found (no-asset, non-modal instance creation) — see the report.
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

    # Data-Layer subsystem helpers (appended to bodies that need them). No '''/no backslash.
    # Instance reader methods are wrapped in _try() so an exact-name mismatch across engine
    # builds degrades to null rather than raising.
    _DL_HELPERS = r'''
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _dls():
    return unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
def _all_instances():
    return list(_dls().get_all_data_layers() or [])
def _dl_asset(di):
    a = _try(lambda: di.get_asset())
    if a is None:
        a = _try(lambda: di.get_data_layer_asset())
    if a is None:
        a = _try(lambda: di.get_editor_property("data_layer_asset"))
    return a
def _dl_asset_path(di):
    a = _dl_asset(di)
    return _try(lambda: a.get_path_name()) if a is not None else None
def _dl_asset_name(di):
    a = _dl_asset(di)
    return _try(lambda: a.get_name()) if a is not None else None
def _dl_short(di):
    for m in ("get_data_layer_short_name", "get_data_layer_instance_name"):
        f = getattr(di, m, None)
        if f is not None:
            v = _try(lambda f=f: str(f()))
            if v:
                return v
    n = _dl_asset_name(di)
    if n:
        return n
    return _try(lambda: di.get_name())
def _dl_full(di):
    return _try(lambda: str(di.get_data_layer_full_name()))
def _dl_idents(di):
    s = set()
    for v in (_dl_short(di), _dl_full(di), _dl_asset_path(di), _dl_asset_name(di), _try(lambda: di.get_name())):
        if v:
            s.add(str(v))
    return s
def _dl_matches(di, ident):
    idset = _dl_idents(di)
    if ident in idset:
        return True
    for c in idset:
        if c.split(".")[-1] == ident or c.split("/")[-1] == ident:
            return True
    return False
def _resolve_dl(ident):
    hits = [di for di in _all_instances() if _dl_matches(di, ident)]
    return hits
def _actor_names_in_dl(di):
    try:
        return set(a.get_name() for a in (_dls().get_actors_from_data_layer(di) or []) if a)
    except Exception:
        return set()
def _dl_record(di):
    dls = _dls()
    actors = _try(lambda: list(dls.get_actors_from_data_layer(di) or []), []) or []
    return {
        "short_name": _dl_short(di),
        "full_name": _dl_full(di),
        "instance_object": _try(lambda: di.get_name()),
        "asset": _dl_asset_path(di),
        "asset_name": _dl_asset_name(di),
        "is_runtime": _try(lambda: bool(di.is_runtime())),
        "initial_runtime_state": _try(lambda: str(di.get_initial_runtime_state())),
        "is_visible": _try(lambda: bool(di.is_visible())),
        "is_initially_visible": _try(lambda: bool(di.is_initially_visible())),
        "is_loaded_in_editor": _try(lambda: bool(di.is_loaded_in_editor())),
        "is_locked": _try(lambda: bool(di.is_locked())),
        "actor_count": len(actors),
    }
def _wdl_present():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_class().get_name() == "WorldDataLayers":
            return True
    return False
'''

    # ------------------------------------------------------------------ #
    # list_data_layers — enumerate DataLayerInstances (read-only)        #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _DL_HELPERS + r'''
import unreal, json
flt = PARAMS.get("filter")
insts = _all_instances()
layers = []
for di in insts:
    rec = _dl_record(di)
    if flt:
        hay = " ".join(str(x) for x in (rec.get("short_name"), rec.get("full_name"), rec.get("asset"), rec.get("asset_name")) if x).lower()
        if flt.lower() not in hay:
            continue
    layers.append(rec)
layers.sort(key=lambda r: (r.get("short_name") or ""))
print("@@UMCP@@" + json.dumps({"status": "success",
    "data_layer_count": len(layers), "total_instances_in_level": len(insts),
    "world_data_layers_present": _wdl_present(), "filter": flt, "data_layers": layers,
    "note": "Data Layers is the operative WP grouping system (DataLayerEditorSubsystem). Each instance is backed by a DataLayerAsset; add/remove/visibility operate on the instance object."}))
'''

    @mcp.tool()
    def list_data_layers(ctx, filter: str = None) -> str:
        """List every World Partition Data Layer instance in the active level. Read-only.

        For each DataLayerInstance reports: short/full name, backing DataLayerAsset (path + name),
        runtime flag + initial runtime state, editor visibility + initial visibility, editor-loaded
        state, and the count of actors currently associated. Enumerated via
        DataLayerEditorSubsystem.get_all_data_layers().

        filter: optional case-insensitive substring matched against name/asset.

        NOTE: this project's Lvl_ThirdPerson currently has a WorldDataLayers actor but ZERO data
        layer instances, so this returns an empty list until data layers are created."""
        try:
            return json.dumps(_exec(_LIST_BODY, {"filter": filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actor_data_layers — which data layers an actor belongs to      #
    # ------------------------------------------------------------------ #
    _GET_ACTOR_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
actor = _resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    uniq = actor.get_name()
    insts = _all_instances()
    member_of = []
    for di in insts:
        if uniq in _actor_names_in_dl(di):
            member_of.append({"short_name": _dl_short(di), "full_name": _dl_full(di), "asset": _dl_asset_path(di)})
    valid = _try(lambda: bool(_dls().is_actor_valid_for_data_layer_instances(actor, insts)))
    print("@@UMCP@@" + json.dumps({"status": "success", "name": uniq,
        "label": actor.get_actor_label(), "data_layer_count": len(member_of),
        "data_layers": member_of, "is_valid_for_data_layers": valid,
        "total_instances_in_level": len(insts),
        "note": "membership tested via get_actors_from_data_layer per instance; is_valid_for_data_layers=False means this actor cannot be associated with data layers"}))
'''

    @mcp.tool()
    def get_actor_data_layers(ctx, actor_name: str) -> str:
        """List the World Partition Data Layers a given actor belongs to. Read-only.

        actor_name: the actor's display label (preferred) or unique internal name.

        Membership is determined by scanning every DataLayerInstance's actor list
        (get_actors_from_data_layer). Also reports is_valid_for_data_layers: if False, the actor
        cannot be associated with data layers at all."""
        try:
            return json.dumps(_exec(_GET_ACTOR_BODY, {"actor_name": actor_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_actors_to_data_layer — associate actors (ledgered write)       #
    # ------------------------------------------------------------------ #
    _ADD_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
ident = PARAMS["data_layer"]
req = PARAMS.get("actor_names") or []
dls = _dls()
hits = _resolve_dl(ident)
if not hits:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "data layer not found: %s" % ident,
        "available": [_dl_short(di) for di in _all_instances()]}))
elif len(hits) > 1:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "ambiguous data layer identifier '%s' matched %d instances; use the asset path" % (ident, len(hits)),
        "matches": [_dl_asset_path(di) or _dl_short(di) for di in hits]}))
else:
    di = hits[0]
    resolved = []
    not_found = []
    for a_ident in req:
        a = _resolve_actor(a_ident)
        if a is None:
            not_found.append(a_ident)
        else:
            resolved.append(a)
    before = _actor_names_in_dl(di)
    changed_flag = None
    if resolved:
        with unreal.ScopedEditorTransaction("MCP add_actors_to_data_layer"):
            changed_flag = _try(lambda: bool(dls.add_actors_to_data_layer(resolved, di)))
    after = _actor_names_in_dl(di)
    actually_added = [a.get_name() for a in resolved if a.get_name() in after and a.get_name() not in before]
    if actually_added:
        _ledger().append({"op": "add_actors_to_data_layer",
            "dl_asset": _dl_asset_path(di), "dl_short": _dl_short(di), "dl_full": _dl_full(di),
            "actor_names": actually_added})
    print("@@UMCP@@" + json.dumps({"status": "success", "data_layer": _dl_short(di),
        "asset": _dl_asset_path(di), "requested": len(req), "matched": len(resolved),
        "not_found": not_found, "subsystem_reported_change": changed_flag,
        "added_count": len(actually_added), "added": actually_added,
        "ledger_depth": len(_ledger()),
        "note": "only actors whose membership actually changed are ledgered"}))
'''

    @mcp.tool()
    def add_actors_to_data_layer(ctx, data_layer: str, actor_names: list) -> str:
        """Associate one or more actors with an EXISTING World Partition Data Layer. Ledgered write.

        data_layer:  identifier of the target data layer (its DataLayerAsset path, asset name,
                     short name, or full name). Must already exist.
        actor_names: list of actor display labels (preferred) or unique internal names.

        Uses DataLayerEditorSubsystem.add_actors_to_data_layer([actors], instance). Only the actors
        whose membership ACTUALLY changed (verified via get_actors_from_data_layer before/after) are
        recorded {op: add_actors_to_data_layer, dl_asset/dl_short/dl_full, actor_names:[...]}, so the
        inverse removes exactly those. Actors already members are not ledgered."""
        params = {"data_layer": data_layer, "actor_names": actor_names}
        try:
            return json.dumps(_exec(_ADD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_actors_from_data_layer — dissociate actors (ledgered write) #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _COERCE_HELPERS + _DL_HELPERS + r'''
ident = PARAMS["data_layer"]
req = PARAMS.get("actor_names") or []
dls = _dls()
hits = _resolve_dl(ident)
if not hits:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "data layer not found: %s" % ident,
        "available": [_dl_short(di) for di in _all_instances()]}))
elif len(hits) > 1:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "ambiguous data layer identifier '%s' matched %d instances; use the asset path" % (ident, len(hits)),
        "matches": [_dl_asset_path(di) or _dl_short(di) for di in hits]}))
else:
    di = hits[0]
    resolved = []
    not_found = []
    for a_ident in req:
        a = _resolve_actor(a_ident)
        if a is None:
            not_found.append(a_ident)
        else:
            resolved.append(a)
    before = _actor_names_in_dl(di)
    changed_flag = None
    if resolved:
        with unreal.ScopedEditorTransaction("MCP remove_actors_from_data_layer"):
            changed_flag = _try(lambda: bool(dls.remove_actors_from_data_layer(resolved, di)))
    after = _actor_names_in_dl(di)
    actually_removed = [a.get_name() for a in resolved if a.get_name() not in after and a.get_name() in before]
    if actually_removed:
        _ledger().append({"op": "remove_actors_from_data_layer",
            "dl_asset": _dl_asset_path(di), "dl_short": _dl_short(di), "dl_full": _dl_full(di),
            "actor_names": actually_removed})
    print("@@UMCP@@" + json.dumps({"status": "success", "data_layer": _dl_short(di),
        "asset": _dl_asset_path(di), "requested": len(req), "matched": len(resolved),
        "not_found": not_found, "subsystem_reported_change": changed_flag,
        "removed_count": len(actually_removed), "removed": actually_removed,
        "ledger_depth": len(_ledger()),
        "note": "only actors whose membership actually changed are ledgered"}))
'''

    @mcp.tool()
    def remove_actors_from_data_layer(ctx, data_layer: str, actor_names: list) -> str:
        """Remove one or more actors from a World Partition Data Layer. Ledgered write.

        data_layer:  identifier of the target data layer (asset path/name, short/full name). Must exist.
        actor_names: list of actor display labels (preferred) or unique internal names.

        Uses DataLayerEditorSubsystem.remove_actors_from_data_layer([actors], instance). Only the
        actors whose membership ACTUALLY changed are recorded {op: remove_actors_from_data_layer,
        dl_asset/dl_short/dl_full, actor_names:[...]}, so the inverse re-adds exactly those."""
        params = {"data_layer": data_layer, "actor_names": actor_names}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_data_layer_visibility — EPHEMERAL editor-view toggle (NOT ledgered)
    # ------------------------------------------------------------------ #
    _SET_VIS_BODY = _DL_HELPERS + r'''
import unreal, json
ident = PARAMS["data_layer"]
visible = bool(PARAMS.get("visible", True))
dls = _dls()
hits = _resolve_dl(ident)
if not hits:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "data layer not found: %s" % ident,
        "available": [_dl_short(di) for di in _all_instances()]}))
elif len(hits) > 1:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "ambiguous data layer identifier '%s' matched %d instances; use the asset path" % (ident, len(hits)),
        "matches": [_dl_asset_path(di) or _dl_short(di) for di in hits]}))
else:
    di = hits[0]
    before = _try(lambda: bool(di.is_visible()))
    dls.set_data_layer_visibility(di, visible)
    after = _try(lambda: bool(di.is_visible()))
    print("@@UMCP@@" + json.dumps({"status": "success", "data_layer": _dl_short(di),
        "asset": _dl_asset_path(di), "before": before, "after": after, "requested": visible,
        "ledgered": False,
        "note": "EPHEMERAL editor-view toggle, NOT ledgered (DataLayerInstance.IsVisible is a transient per-session view flag not saved with the level; mirrors editor_viewport/editor_layers ephemeral ops). To revert, call again with the desired state. The persistent counterpart is set_data_layer_is_initially_visible (not toggled here)."}))
'''

    @mcp.tool()
    def set_data_layer_visibility(ctx, data_layer: str, visible: bool = True) -> str:
        """Show or hide a World Partition Data Layer's actors in the editor. EPHEMERAL — NOT ledgered.

        data_layer: identifier of the target data layer (asset path/name, short/full name). Must exist.
        visible:    True to show, False to hide (default True).

        Uses DataLayerEditorSubsystem.set_data_layer_visibility(instance, bool). This is a transient
        per-editor-session VIEW flag (not saved with the level, resets on reload), so like
        editor_viewport's camera ops it is applied WITHOUT a ledger entry. The response DOES report
        the readable before/after. To revert, call again with the desired state; the persistent
        saved counterpart is the data layer's InitiallyVisible property (not changed here)."""
        params = {"data_layer": data_layer, "visible": visible}
        try:
            return json.dumps(_exec(_SET_VIS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
