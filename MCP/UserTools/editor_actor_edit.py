"""UserTools :: Editor / Actor-level edits  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ one-line marker, the session-aware
per-agent undo ledger at builtins._UMCP_LEDGERS[session], and the _COERCE_HELPERS block.

These commands mutate ACTOR-LEVEL state on a placed level actor (as opposed to a reflected
UPROPERTY set, which editor_level.set_actor_property / objects.set_object_property already
cover generically). Each provides correct dedicated semantics + validation.

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that marker
and parses the JSON after it, so stray engine log lines can't corrupt it.

Write safety (agent-scoped undo): every ledgered mutation runs inside an
`unreal.ScopedEditorTransaction` AND records an inverse op on the PER-SESSION agent ledger
(session from PARAMS["_session"], injected by _exec). No `undo` tool is defined here — this
module defers to editor_level.py's unified agent-scoped `undo`, and records only invertible
entries. THREE NEW op names are introduced (for the coordinator to fold into that undo):

  - set_actor_mobility  -> {"op": "set_actor_mobility", "actor_name": <unique>,
                            "prior_mobility": "STATIC"|"STATIONARY"|"MOVABLE"}
        inverse: resolve actor; root=actor.get_editor_property("root_component");
                 root.set_editor_property("mobility", getattr(unreal.ComponentMobility, prior_mobility))

  - set_actor_tags      -> {"op": "set_actor_tags", "actor_name": <unique>,
                            "prior_tags": [<str>, ...]}         (FULL prior Actor.Tags array)
        inverse: resolve actor; actor.set_editor_property("tags",
                 [unreal.Name(s) for s in prior_tags])         (whole-array restore)
        BOTH add_actor_tags and remove_actor_tags push this ONE op (read-modify-write the
        whole TArray, exactly like objects.set_object_property does for arrays).

  - set_actor_collision_enabled -> {"op": "set_actor_collision_enabled",
                            "actor_name": <unique>, "prior_enabled": <bool>}
        inverse: resolve actor; actor.set_actor_enable_collision(prior_enabled)

EPHEMERAL (intentionally NOT ledgered), mirroring editor_viewport.py:
  - set_actor_hidden_in_editor uses set_is_temporarily_hidden_in_editor — the editor's
    "temporarily hidden" (H-key) VIEW state. It is transient editor-only visualization (not
    saved with the level, reset on reload), so it is treated as an ephemeral view toggle:
    no ScopedEditorTransaction meaning for persistence, no ledger entry, no undo. The command
    still returns before/after so callers can flip it back explicitly.

Implemented:
  - set_actor_mobility          (write; ledgered set_actor_mobility)
  - add_actor_tags              (write; ledgered set_actor_tags — whole-array capture)
  - remove_actor_tags           (write; ledgered set_actor_tags — whole-array capture)
  - set_actor_collision_enabled (write; ledgered set_actor_collision_enabled)
  - set_actor_hidden_in_editor  (write; EPHEMERAL view toggle; NOT ledgered)

Deferred:
  - set_actor_transform_relative — NOT distinct enough to justify a new command. For an
    unattached actor the relative transform equals the world transform already handled by
    editor_level.set_actor_transform; for an attached actor the root component's RELATIVE
    transform is already exposed by editor_actor_components.set_component_transform (targeting
    the root scene component). Adding it here would duplicate both, so it is deferred.
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
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER payload."""
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

    # Shared Unreal-side helpers (prepended to write bodies). Verbatim from editor_level.py.
    # Defines the session-aware _ledger(), _settable/_coerce, _resolve_actor/_find_by_name, _descend.
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

    # ------------------------------------------------------------------ #
    # set_actor_mobility — Static/Stationary/Movable on root (ledgered)   #
    # ------------------------------------------------------------------ #
    _SET_MOBILITY_BODY = _COERCE_HELPERS + r'''
_MOBS = {"STATIC": unreal.ComponentMobility.STATIC,
         "STATIONARY": unreal.ComponentMobility.STATIONARY,
         "MOVABLE": unreal.ComponentMobility.MOVABLE}
name = PARAMS["actor_name"]
want = (PARAMS.get("mobility") or "").strip().upper()
if want == "MOVEABLE":
    want = "MOVABLE"
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
elif want not in _MOBS:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "mobility must be one of Static|Stationary|Movable (got %r)" % PARAMS.get("mobility")}))
else:
    root = a.get_editor_property("root_component")
    if root is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor has no root component: %s" % name}))
    else:
        prior_enum = root.get_editor_property("mobility")
        prior = _enum_name(prior_enum)
        with unreal.ScopedEditorTransaction("MCP set_actor_mobility"):
            root.set_editor_property("mobility", _MOBS[want])
        after = _enum_name(root.get_editor_property("mobility"))
        _ledger().append({"op": "set_actor_mobility", "actor_name": a.get_name(), "prior_mobility": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
            "label": a.get_actor_label(), "root_component": root.get_name(),
            "before": prior, "after": after, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_mobility(ctx, actor_name: str, mobility: str) -> str:
        """Set a placed actor's root-component mobility (ledgered write).

        actor_name: actor display label (preferred) or unique internal name.
        mobility:   'Static' | 'Stationary' | 'Movable' (case-insensitive).

        Sets ComponentMobility on the actor's root SceneComponent (e.g. a StaticMeshActor's
        StaticMeshComponent). Static actors can't move at runtime; Movable can; Stationary is
        the middle ground (movable-lighting-static-position). The prior mobility is captured so
        `undo` (editor_level's unified undo) restores it exactly.

        Overlap note: editor_level.set_actor_property / objects.set_object_property could set the
        'mobility' enum generically; this command adds validation + clear before/after semantics."""
        params = {"actor_name": actor_name, "mobility": mobility}
        try:
            return json.dumps(_exec(_SET_MOBILITY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_actor_tags — append to Actor.Tags (ledgered; whole-array cap)   #
    # ------------------------------------------------------------------ #
    _ADD_TAGS_BODY = _COERCE_HELPERS + r'''
name = PARAMS["actor_name"]
add = PARAMS.get("tags") or []
skip_existing = bool(PARAMS.get("skip_existing", True))
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
elif not isinstance(add, list) or not add:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "tags must be a non-empty list of strings"}))
else:
    cur = [str(t) for t in (a.get_editor_property("tags") or [])]
    prior = list(cur)
    added = []
    newlist = list(cur)
    for t in add:
        ts = str(t)
        if skip_existing and ts in newlist:
            continue
        newlist.append(ts); added.append(ts)
    if not added:
        print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
            "label": a.get_actor_label(), "note": "no new tags (all already present)",
            "before": prior, "after": prior, "added": [], "ledger_depth": len(_ledger())}))
    else:
        with unreal.ScopedEditorTransaction("MCP add_actor_tags"):
            a.set_editor_property("tags", [unreal.Name(s) for s in newlist])
        after = [str(t) for t in (a.get_editor_property("tags") or [])]
        _ledger().append({"op": "set_actor_tags", "actor_name": a.get_name(), "prior_tags": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
            "label": a.get_actor_label(), "before": prior, "after": after, "added": added,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_actor_tags(ctx, actor_name: str, tags: list, skip_existing: bool = True) -> str:
        """Add one or more actor-level tags to a placed actor's Actor.Tags array (ledgered write).

        actor_name:    actor display label (preferred) or unique internal name.
        tags:          list of tag strings to add (FName tags on the actor itself, not a
                       component). These are what find_actors(tag=...) / actor_has_tag() match.
        skip_existing: when True (default), tags already present are not duplicated.

        Read-modify-write of the whole Tags TArray. The FULL prior Tags array is captured on the
        ledger so `undo` restores it wholesale (op 'set_actor_tags')."""
        params = {"actor_name": actor_name, "tags": tags, "skip_existing": skip_existing}
        try:
            return json.dumps(_exec(_ADD_TAGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_actor_tags — drop from Actor.Tags (ledgered; whole-array)    #
    # ------------------------------------------------------------------ #
    _REMOVE_TAGS_BODY = _COERCE_HELPERS + r'''
name = PARAMS["actor_name"]
drop = PARAMS.get("tags") or []
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
elif not isinstance(drop, list) or not drop:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "tags must be a non-empty list of strings"}))
else:
    cur = [str(t) for t in (a.get_editor_property("tags") or [])]
    prior = list(cur)
    dropset = set(str(t) for t in drop)
    newlist = [t for t in cur if t not in dropset]
    removed = [t for t in cur if t in dropset]
    if not removed:
        print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
            "label": a.get_actor_label(), "note": "no matching tags to remove",
            "before": prior, "after": prior, "removed": [], "ledger_depth": len(_ledger())}))
    else:
        with unreal.ScopedEditorTransaction("MCP remove_actor_tags"):
            a.set_editor_property("tags", [unreal.Name(s) for s in newlist])
        after = [str(t) for t in (a.get_editor_property("tags") or [])]
        _ledger().append({"op": "set_actor_tags", "actor_name": a.get_name(), "prior_tags": prior})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
            "label": a.get_actor_label(), "before": prior, "after": after, "removed": removed,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_actor_tags(ctx, actor_name: str, tags: list) -> str:
        """Remove one or more actor-level tags from a placed actor's Actor.Tags array (ledgered).

        actor_name: actor display label (preferred) or unique internal name.
        tags:       list of tag strings to remove; tags not present are ignored.

        Read-modify-write of the whole Tags TArray. The FULL prior Tags array is captured on the
        ledger so `undo` restores it wholesale (op 'set_actor_tags', shared with add_actor_tags)."""
        params = {"actor_name": actor_name, "tags": tags}
        try:
            return json.dumps(_exec(_REMOVE_TAGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_collision_enabled — SetActorEnableCollision (ledgered)    #
    # ------------------------------------------------------------------ #
    _SET_COLLISION_BODY = _COERCE_HELPERS + r'''
name = PARAMS["actor_name"]
enabled = bool(PARAMS.get("enabled"))
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
else:
    prior = bool(a.get_actor_enable_collision())
    with unreal.ScopedEditorTransaction("MCP set_actor_collision_enabled"):
        a.set_actor_enable_collision(enabled)
    after = bool(a.get_actor_enable_collision())
    _ledger().append({"op": "set_actor_collision_enabled", "actor_name": a.get_name(), "prior_enabled": prior})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
        "label": a.get_actor_label(), "before": prior, "after": after,
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_collision_enabled(ctx, actor_name: str, enabled: bool) -> str:
        """Enable/disable collision on a placed actor (ledgered write).

        actor_name: actor display label (preferred) or unique internal name.
        enabled:    True to enable actor collision, False to disable.

        Calls Actor.SetActorEnableCollision, which toggles collision across the actor's
        primitive components. The prior enabled state is captured so `undo` restores it
        (op 'set_actor_collision_enabled')."""
        params = {"actor_name": actor_name, "enabled": enabled}
        try:
            return json.dumps(_exec(_SET_COLLISION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_hidden_in_editor — EPHEMERAL view toggle (NOT ledgered)   #
    # ------------------------------------------------------------------ #
    # set_is_temporarily_hidden_in_editor is the editor's transient "temporarily hidden"
    # (H-key) VIEW state: not saved with the level, reset on reload. Mirroring editor_viewport.py,
    # this ephemeral view toggle is deliberately NOT ledgered and there is NO undo entry — callers
    # flip it back explicitly. before/after are returned so the prior state is never lost.
    _SET_HIDDEN_BODY = _COERCE_HELPERS + r'''
name = PARAMS["actor_name"]
hidden = bool(PARAMS.get("hidden"))
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
else:
    prior = bool(a.is_temporarily_hidden_in_editor())
    a.set_is_temporarily_hidden_in_editor(hidden)
    after = bool(a.is_temporarily_hidden_in_editor())
    print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(),
        "label": a.get_actor_label(), "before": prior, "after": after,
        "ephemeral": True, "ledgered": False,
        "note": "temporarily-hidden is transient editor VIEW state (not saved); not ledgered. Flip back explicitly."}))
'''

    @mcp.tool()
    def set_actor_hidden_in_editor(ctx, actor_name: str, hidden: bool) -> str:
        """Show/hide a placed actor in the editor viewport (EPHEMERAL; NOT ledgered).

        actor_name: actor display label (preferred) or unique internal name.
        hidden:     True to temporarily hide in the editor, False to show.

        Uses Actor.SetIsTemporarilyHiddenInEditor — the editor's transient 'temporarily hidden'
        (H-key) view state. This is editor-only visualization that is NOT saved with the level
        and resets on reload, so (like editor_viewport's camera ops) it is intentionally NOT
        recorded on the undo ledger and `undo` will not revert it. The response returns
        before/after; pass the prior value to flip it back."""
        params = {"actor_name": actor_name, "hidden": hidden}
        try:
            return json.dumps(_exec(_SET_HIDDEN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
