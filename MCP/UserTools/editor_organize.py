"""UserTools :: Editor / Organize  (spec: docs/spec/editor.md — "Folders / grouping / attachment")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo
ledger, and the _COERCE_HELPERS block (session-aware _ledger(), _resolve_actor, _find_by_name).

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines on stdout can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
AND records an inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]
(keyed by PARAMS["_session"], injected by _exec). Reversibility is REQUIRED for every write.

IMPORTANT — NEW ledger op names (the shared `undo` in editor_level.py does NOT invert these
yet; the coordinator will add matching undo branches). This module therefore defines NO `undo`
tool. Each write records ONE ledger entry per call with enough prior state to invert:
  - move_actors_to_folder -> {"op": "move_to_folder",
                              "moves": [{"actor_name": <unique>, "prior_folder": <str>}, ...]}
                             (invert: set each actor's folder back to prior_folder)
  - attach_actors         -> {"op": "attach_actors",
                              "attaches": [{"child": <unique>, "prior_parent": <unique|null>}, ...]}
                             (invert: re-attach to prior_parent, or detach if prior_parent is null)
  - detach_actors         -> {"op": "detach_actors",
                              "detaches": [{"child": <unique>, "prior_parent": <unique>}, ...]}
                             (invert: re-attach each child to prior_parent, KEEP_WORLD)
  - group_actors          -> {"op": "group_actors",
                              "group_actor_name": <unique>, "members": [<unique>, ...]}
                             (invert: ungroup those members)
  - ungroup_actors        -> {"op": "ungroup_actors", "members": [<unique>, ...]}
                             (invert: re-group those members)

Implemented:
  - list_folders            (read-only)  — distinct outliner folder paths + per-folder counts
  - move_actors_to_folder   (write; ledgered move_to_folder)
  - group_actors            (write; ledgered group_actors)
  - ungroup_actors          (write; ledgered ungroup_actors)
  - attach_actors           (write; ledgered attach_actors) — keep world transform
  - detach_actors           (write; ledgered detach_actors) — keep world transform

API notes (live-verified on UE 5.8.1):
  * Grouping utility is reached via unreal.ActorGroupingUtils.get_default_object() (a CDO with
    .group_actors([...]) / .ungroup_actors([...]) / .set_grouping_active(bool)). The
    get_actor_grouping_utils() accessor named in some docs does NOT exist in this build.
  * An actor's current parent is read via get_attach_parent_actor() (NOT get_attached_parent_actor).
  * detach_from_actor() takes DetachmentRule.KEEP_WORLD (attach takes AttachmentRule.KEEP_WORLD).
  * A GroupActor's membership (its 'GroupActors' property) is PROTECTED and not readable from
    Python, and grouping is NOT scene attachment. So ungroup records the matched members; its
    inverse re-groups exactly those (see ungroup_actors docstring caveat).
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

    # Shared Unreal-side helpers (prepended to write bodies). Verbatim from editor_level.py.
    # Defines session-aware _ledger(), _settable/_coerce, _resolve_actor/_find_by_name, _descend.
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

    # Actor-matching helper (appended to write bodies). names (label OR internal name) takes
    # precedence over name_prefix (label startswith). Guards non-actor level entries. No '''/no backslash.
    _MATCH_HELPER = r'''
def _match_targets():
    names = PARAMS.get("names")
    name_prefix = PARAMS.get("name_prefix")
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    res = []
    if names:
        want = set(names)
        for a in (eas.get_all_level_actors() or []):
            try:
                if a and (a.get_actor_label() in want or a.get_name() in want):
                    res.append(a)
            except Exception:
                pass
    elif name_prefix:
        for a in (eas.get_all_level_actors() or []):
            try:
                if a and a.get_actor_label().startswith(name_prefix):
                    res.append(a)
            except Exception:
                pass
    return res
'''

    # ------------------------------------------------------------------ #
    # list_folders — distinct outliner folder paths + per-folder counts   #
    # ------------------------------------------------------------------ #
    _LIST_FOLDERS_BODY = r'''
import unreal, json
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
flt = PARAMS.get("filter")
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
counts = {}
unfoldered = 0
for a in (eas.get_all_level_actors() or []):
    if not a:
        continue
    f = _try(lambda: str(a.get_folder_path()))
    f = "" if (f is None or f in ("None",)) else f
    if f == "":
        unfoldered += 1
        continue
    counts[f] = counts.get(f, 0) + 1
folders = []
for path in sorted(counts.keys()):
    if flt and flt.lower() not in path.lower():
        continue
    folders.append({"folder": path, "actor_count": counts[path]})
print("@@UMCP@@" + json.dumps({"status": "success", "folder_count": len(folders),
    "unfoldered_actor_count": unfoldered, "filter": flt, "folders": folders}))
'''

    @mcp.tool()
    def list_folders(ctx, filter: str = None) -> str:
        """List the distinct World Outliner folder paths used in the active level, each with a
        count of how many actors sit directly in it. Read-only.

        Folder paths are derived from each actor's get_folder_path() (e.g. 'Playground',
        'Lighting', or nested 'Env/Rocks'). Actors with no folder are reported separately as
        'unfoldered_actor_count'.

        filter: optional case-insensitive substring; only folder paths containing it are returned."""
        try:
            return json.dumps(_exec(_LIST_FOLDERS_BODY, {"filter": filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # move_actors_to_folder — set each matched actor's outliner folder    #
    # ------------------------------------------------------------------ #
    _MOVE_BODY = _COERCE_HELPERS + _MATCH_HELPER + r'''
folder = PARAMS["folder"]
targets = _match_targets()
moved = []
with unreal.ScopedEditorTransaction("MCP move_actors_to_folder"):
    for a in targets:
        prior = str(a.get_folder_path())
        prior = "" if prior in ("None",) else prior
        a.set_folder_path(unreal.Name(folder))
        moved.append({"actor_name": a.get_name(), "label": a.get_actor_label(),
                      "prior_folder": prior, "new_folder": str(a.get_folder_path())})
if moved:
    _ledger().append({"op": "move_to_folder",
        "moves": [{"actor_name": m["actor_name"], "prior_folder": m["prior_folder"]} for m in moved]})
print("@@UMCP@@" + json.dumps({"status": "success", "folder": folder, "matched": len(targets),
    "moved_count": len(moved), "moved": moved, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def move_actors_to_folder(ctx, folder: str, name_prefix: str = None,
                              names: list = None) -> str:
        """Move matched actors into a World Outliner folder (set_folder_path). Ledgered write.

        folder:      destination outliner folder path (e.g. 'Playground' or nested 'Env/Rocks').
        name_prefix: match every actor whose display label starts with this prefix.
        names:       explicit list of labels/internal names (takes precedence over name_prefix).

        Records ONE ledger entry {op: move_to_folder, moves: [{actor_name, prior_folder}, ...]}
        capturing each actor's PRIOR folder, so the move can be inverted exactly."""
        params = {"folder": folder, "name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_MOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # group_actors — group matched actors into a GroupActor               #
    # ------------------------------------------------------------------ #
    _GROUP_BODY = _COERCE_HELPERS + _MATCH_HELPER + r'''
targets = _match_targets()
if len(targets) < 2:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "group_actors needs at least 2 matched actors; matched %d" % len(targets),
        "matched": len(targets)}))
else:
    agu = unreal.ActorGroupingUtils.get_default_object()
    if not agu.is_grouping_active():
        agu.set_grouping_active(True)
    member_names = [a.get_name() for a in targets]
    group_name = None
    with unreal.ScopedEditorTransaction("MCP group_actors"):
        grp = agu.group_actors(targets)
        if grp is not None:
            group_name = grp.get_name()
    _ledger().append({"op": "group_actors", "group_actor_name": group_name, "members": member_names})
    print("@@UMCP@@" + json.dumps({"status": "success", "matched": len(targets),
        "group_actor_name": group_name, "members": member_names, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def group_actors(ctx, name_prefix: str = None, names: list = None) -> str:
        """Group matched actors into a single World Outliner GroupActor (editor grouping).
        Ledgered write. Requires at least 2 matched actors.

        name_prefix: match every actor whose display label starts with this prefix.
        names:       explicit list of labels/internal names (takes precedence over name_prefix).

        Uses unreal.ActorGroupingUtils.get_default_object().group_actors([...]) and ensures
        grouping is active. Records {op: group_actors, group_actor_name, members: [...]}; the
        inverse ungroups exactly those members."""
        params = {"name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_GROUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # ungroup_actors — dissolve the group(s) of the matched actors        #
    # ------------------------------------------------------------------ #
    _UNGROUP_BODY = _COERCE_HELPERS + _MATCH_HELPER + r'''
targets = _match_targets()
if not targets:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no actors matched", "matched": 0}))
else:
    agu = unreal.ActorGroupingUtils.get_default_object()
    member_names = [a.get_name() for a in targets]
    with unreal.ScopedEditorTransaction("MCP ungroup_actors"):
        agu.ungroup_actors(targets)
    _ledger().append({"op": "ungroup_actors", "members": member_names})
    print("@@UMCP@@" + json.dumps({"status": "success", "matched": len(targets),
        "members": member_names, "ledger_depth": len(_ledger()),
        "note": "inverse re-groups exactly these matched members (group membership is not readable from Python)"}))
'''

    @mcp.tool()
    def ungroup_actors(ctx, name_prefix: str = None, names: list = None) -> str:
        """Ungroup matched actors — dissolves the editor GroupActor(s) they belong to.
        Ledgered write.

        name_prefix: match every actor whose display label starts with this prefix.
        names:       explicit list of labels/internal names (takes precedence over name_prefix).

        Uses unreal.ActorGroupingUtils.get_default_object().ungroup_actors([...]). Records
        {op: ungroup_actors, members: [...]}; the inverse re-groups exactly those members.
        CAVEAT: a GroupActor's full membership is not readable from Python, so if only a subset
        of a larger group is passed, the whole group dissolves but only the passed subset is
        recorded for re-grouping."""
        params = {"name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_UNGROUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # attach_actors — attach matched children to a parent (keep world)    #
    # ------------------------------------------------------------------ #
    _ATTACH_BODY = _COERCE_HELPERS + _MATCH_HELPER + r'''
parent = _resolve_actor(PARAMS["parent"])
if parent is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent not found: %s" % PARAMS["parent"]}))
else:
    targets = _match_targets()
    KW = unreal.AttachmentRule.KEEP_WORLD
    attaches = []
    results = []
    with unreal.ScopedEditorTransaction("MCP attach_actors"):
        for a in targets:
            if a.get_name() == parent.get_name():
                results.append({"child": a.get_name(), "result": "skipped (is parent)"}); continue
            if not hasattr(a, "attach_to_actor"):
                results.append({"child": a.get_name(), "result": "skipped (not attachable)"}); continue
            pp = None
            try:
                cur = a.get_attach_parent_actor()
                pp = cur.get_name() if cur else None
            except Exception:
                pp = None
            ok = a.attach_to_actor(parent, "", KW, KW, KW, False)
            now = a.get_attach_parent_actor()
            # Authoritative: the engine may abort (e.g. STATIC child -> MOVABLE parent) yet
            # return without error. Trust the post-attach parent, not the call's return value.
            really = bool(now) and now.get_name() == parent.get_name()
            if really:
                attaches.append({"child": a.get_name(), "prior_parent": pp})
            results.append({"child": a.get_name(), "label": a.get_actor_label(),
                            "prior_parent": pp, "attached": really,
                            "engine_returned": bool(ok),
                            "parent_now": now.get_name() if now else None})
    if attaches:
        _ledger().append({"op": "attach_actors", "attaches": attaches})
    failed = [r for r in results if r.get("attached") is False and "skipped" not in str(r.get("result", ""))]
    print("@@UMCP@@" + json.dumps({"status": "success", "parent": parent.get_name(),
        "parent_label": parent.get_actor_label(), "matched": len(targets),
        "attached_count": len(attaches), "failed_count": len(failed),
        "results": results, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def attach_actors(ctx, parent: str, name_prefix: str = None, names: list = None) -> str:
        """Attach matched actors to a parent actor, preserving each child's world transform.
        Ledgered write.

        parent:      the parent actor's display label (preferred) or unique internal name.
        name_prefix: match every child whose display label starts with this prefix.
        names:       explicit list of labels/internal names (takes precedence over name_prefix).

        Uses child.attach_to_actor(parent, "", KEEP_WORLD x3, weld=False). Captures each child's
        PRIOR parent (get_attach_parent_actor). Records {op: attach_actors, attaches:
        [{child, prior_parent|null}, ...]}; the inverse re-attaches to prior_parent, or detaches
        if there was none. The parent itself and non-attachable entries are skipped."""
        params = {"parent": parent, "name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_ATTACH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # detach_actors — detach matched children (keep world transform)      #
    # ------------------------------------------------------------------ #
    _DETACH_BODY = _COERCE_HELPERS + _MATCH_HELPER + r'''
targets = _match_targets()
DKW = unreal.DetachmentRule.KEEP_WORLD
detaches = []
results = []
with unreal.ScopedEditorTransaction("MCP detach_actors"):
    for a in targets:
        if not hasattr(a, "detach_from_actor"):
            results.append({"child": a.get_name(), "result": "skipped (not attachable)"}); continue
        pp = None
        try:
            cur = a.get_attach_parent_actor()
            pp = cur.get_name() if cur else None
        except Exception:
            pp = None
        if pp is None:
            results.append({"child": a.get_name(), "label": a.get_actor_label(),
                            "result": "skipped (no parent)"}); continue
        a.detach_from_actor(DKW, DKW, DKW)
        detaches.append({"child": a.get_name(), "prior_parent": pp})
        now = a.get_attach_parent_actor()
        results.append({"child": a.get_name(), "label": a.get_actor_label(),
                        "prior_parent": pp, "detached": True,
                        "parent_now": now.get_name() if now else None})
if detaches:
    _ledger().append({"op": "detach_actors", "detaches": detaches})
print("@@UMCP@@" + json.dumps({"status": "success", "matched": len(targets),
    "detached_count": len(detaches), "results": results, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def detach_actors(ctx, name_prefix: str = None, names: list = None) -> str:
        """Detach matched actors from their current parent, preserving world transform.
        Ledgered write.

        name_prefix: match every child whose display label starts with this prefix.
        names:       explicit list of labels/internal names (takes precedence over name_prefix).

        Uses child.detach_from_actor(KEEP_WORLD x3). Captures each child's PRIOR parent so the
        move is reversible. Records {op: detach_actors, detaches: [{child, prior_parent}, ...]};
        the inverse re-attaches each child to prior_parent. Actors with no parent are skipped."""
        params = {"name_prefix": name_prefix, "names": names}
        try:
            return json.dumps(_exec(_DETACH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
