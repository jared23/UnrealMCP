"""UserTools :: Editor / Level  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8).
Commands are executed in the editor's interpreter via the plugin's `execute_python`
handler, which returns captured stdout under result["output"] (CRLF).

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines on stdout can't corrupt it.

Write safety (agent-scoped undo): every mutation runs inside an `unreal.ScopedEditorTransaction`
(atomic + editor-undoable) AND records an inverse op on a PER-SESSION agent ledger at
`builtins._UMCP_LEDGERS[session]` (persists across execute_python calls, which each get fresh
globals). The session id is injected via PARAMS["_session"] (from _exec; one per writer process),
so concurrent agents never pop each other's entries. `undo` pops the caller's own session ledger
LIFO and reverts ONLY that writer's edits, never the user's manual work or another agent's.

Implemented:
  - get_world_info       (read-only)
  - get_actors_in_level  (read-only)
  - find_actors          (read-only; filtered)
  - get_actor_properties (read-only; full reflection dump)
  - get_log_tail         (read-only; read the editor Output Log remotely)
  - spawn_actor          (write; ledgered)
  - set_actor_transform  (write; ledgered)
  - set_actor_property   (write; ledgered; reflection set with dotted component paths)
  - set_actor_label      (write; ledgered; rename an actor's display label)
  - delete_actor         (write; ledgered; SOFT delete -> hidden _MCP_Trash folder)
  - undo                 (agent-scoped revert of our own edits)

Soft-delete convention: delete_actor does NOT destroy; it moves the actor to a hidden
'_MCP_Trash' outliner folder and hides it in the editor. undo restores folder+visibility.
Listings (get_actors_in_level) hide _MCP_Trash actors unless include_trashed=True.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture -------------------------------------------------
# The plugin runs our snippet on the Windows box and mirrors the editor Output Log
# to <Project>/Saved/Logs/<Project>.log. We wrap every snippet so it records the log
# size before running and, in a finally, flushes (unreal.log_flush()) and reads the
# appended bytes — surfacing any new Warning/Error lines as @@UMCP_LOG@@ (attached to
# the result as "_log_warnings"). This is how a remote instance "sees" the Output Log.
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
# before exec, so any ''' or backslash in the code corrupts it. Never embed raw data with
# '''...'''; pass parameters as base64 (its alphabet has no quote/backslash/triple-quote).
# Snippet bodies themselves must also avoid ''' and stray backslashes.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer (bridge/agent process) so its undo ledger is isolated
    # from other concurrent writers. One value per OS process (all modules in a bridge share it);
    # separate agent-harness processes get distinct ids. Override via utils["session"] if provided.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. Any new Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run
        the body in Unreal, and return its MARKER payload. Adds _session for ledger isolation."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to bodies that need them). No ''' / no backslashes.
    # _settable(v)->(json_value, restorable): convert an Unreal value to a JSON form we can
    #   later re-apply. _coerce(current, value)->unreal value: build a settable value from JSON
    #   using current's type as a hint (object paths -> load asset, lists -> Vector/Rotator/Color,
    #   enum name -> enum). These make set/undo symmetric. _descend traverses component + dotted
    #   object paths (refuses struct sub-paths, which wouldn't persist).
    _COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    # Per-session undo stack so concurrent agents never pop each other's entries.
    # Session id arrives in PARAMS["_session"] (injected by _exec from the bridge/harness).
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
    # get_world_info — level/world summary of the active editor world     #
    # ------------------------------------------------------------------ #
    _GET_WORLD_INFO = r'''
import unreal, json
def _try(fn, default=None):
    try: return fn()
    except Exception: return default
info = {}
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
info["world_name"] = _try(lambda: world.get_name())
info["world_path"] = _try(lambda: world.get_path_name())
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = _try(lambda: eas.get_all_level_actors(), []) or []
info["actor_count"] = len(actors)
info["engine_version"] = _try(lambda: unreal.SystemLibrary.get_engine_version())
info["is_play_in_editor"] = _try(lambda: world.is_play_in_editor(), False)
info["is_world_partition"] = _try(lambda: world.get_editor_property("world_partition")) is not None
info["persistent_level"] = _try(lambda: world.get_outer().get_name()) or info["world_name"]
info["game_mode"] = _try(lambda: world.get_editor_property("authority_game_mode").get_class().get_name())
print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_world_info(ctx) -> str:
        """Get a summary of the active editor world/level: world name and package path,
        placed-actor count, engine version, play-in-editor state, world-partition flag,
        persistent level, and game mode (if any). Read-only."""
        try:
            info = _query(_GET_WORLD_INFO)
            return json.dumps(info, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actors_in_level — every placed actor with transform + folder    #
    # ------------------------------------------------------------------ #
    _GET_ACTORS = r'''
import unreal, json
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _vec(v):
    return [round(v.x,3), round(v.y,3), round(v.z,3)] if v is not None else None
include_trashed = bool(PARAMS.get("include_trashed"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
out = []
trashed = 0
for a in actors:
    if not a: continue
    f = _try(lambda: str(a.get_folder_path())) or ""
    folder = "" if f in ("None", "") else f
    if folder == "_MCP_Trash":
        trashed += 1
        if not include_trashed:
            continue
    rot = _try(lambda: a.get_actor_rotation())
    out.append({
        "name":   _try(lambda: a.get_name()),
        "label":  _try(lambda: a.get_actor_label()),
        "class":  _try(lambda: a.get_class().get_name()),
        "location": _vec(_try(lambda: a.get_actor_location())),
        "rotation": ([round(rot.pitch,3), round(rot.yaw,3), round(rot.roll,3)] if rot is not None else None),
        "folder": folder,
    })
print("@@UMCP@@" + json.dumps({"count": len(out), "trashed_hidden": (0 if include_trashed else trashed), "actors": out}))
'''

    @mcp.tool()
    def get_actors_in_level(ctx, include_trashed: bool = False) -> str:
        """List every actor placed in the active level, each with its internal name,
        display label, class, world location [x,y,z], rotation [pitch,yaw,roll], and
        outliner folder. Read-only. Use this to verify state after edits.

        include_trashed: include soft-deleted actors (in _MCP_Trash); default False.
        The response reports 'trashed_hidden' = how many were omitted."""
        try:
            data = _exec(_GET_ACTORS, {"include_trashed": include_trashed})
            return json.dumps(data, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # find_actors — filtered search (name/label/class/tag), read-only     #
    # ------------------------------------------------------------------ #
    _FIND_ACTORS_BODY = r'''
import unreal, json, fnmatch
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
name_pat = PARAMS.get("name_pattern")
label_pat = PARAMS.get("label_pattern")
class_filter = PARAMS.get("class_filter")
tag = PARAMS.get("tag")
exact_class = bool(PARAMS.get("exact_class"))
max_results = PARAMS.get("max_results")
include_tx = bool(PARAMS.get("include_transform"))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
cls_obj = getattr(unreal, class_filter, None) if (class_filter and not exact_class) else None
out = []
for a in (eas.get_all_level_actors() or []):
    if not a:
        continue
    nm = a.get_name(); lb = a.get_actor_label(); cn = a.get_class().get_name()
    if not _match(nm, name_pat):
        continue
    if not _match(lb, label_pat):
        continue
    if class_filter:
        if exact_class:
            if cn != class_filter:
                continue
        elif cls_obj is not None:
            if not isinstance(a, cls_obj):
                continue
        elif class_filter.lower() not in cn.lower():
            continue
    if tag:
        try:
            has = a.actor_has_tag(unreal.Name(tag))
        except Exception:
            has = False
        if not has:
            continue
    rec = {"name": nm, "label": lb, "class": cn}
    if include_tx:
        l = a.get_actor_location(); r = a.get_actor_rotation(); s = a.get_actor_scale3d()
        rec["location"] = [l.x, l.y, l.z]
        rec["rotation"] = [r.pitch, r.yaw, r.roll]
        rec["scale"] = [s.x, s.y, s.z]
    out.append(rec)
    if max_results and len(out) >= int(max_results):
        break
print("@@UMCP@@" + json.dumps({"count": len(out), "actors": out}))
'''

    @mcp.tool()
    def find_actors(ctx, name_pattern: str = None, label_pattern: str = None,
                    class_filter: str = None, tag: str = None,
                    exact_class: bool = False, max_results: int = None,
                    include_transform: bool = False) -> str:
        """Find actors in the active level by flexible filters (all optional, AND-combined).

        name_pattern:  match on unique internal name (substring or glob, case-insensitive).
        label_pattern: match on display label (substring or glob, case-insensitive).
        class_filter:  class name; by default matches the class or any subclass; set
                       exact_class=True to require the exact class name.
        tag:           only actors carrying this actor tag.
        max_results:   cap the number returned.
        include_transform: include location/rotation/scale per actor.
        Read-only."""
        params = {"name_pattern": name_pattern, "label_pattern": label_pattern,
                  "class_filter": class_filter, "tag": tag, "exact_class": exact_class,
                  "max_results": max_results, "include_transform": include_transform}
        try:
            return json.dumps(_exec(_FIND_ACTORS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_actor_properties — full reflection dump (read-only)             #
    # ------------------------------------------------------------------ #
    _GET_PROPS_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")  # deprecated-alias reads spam DeprecationWarning; this is a pure read
ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 25)
def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return sorted(names)
def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        items = []
        for i, e in enumerate(v):
            if i >= ARRAY_LIMIT:
                items.append("...(%d more)" % (len(v) - ARRAY_LIMIT)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= ARRAY_LIMIT:
                break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try: d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception: d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
def _dump(obj, flt, depth, max_entries, cursor):
    names = _prop_names(obj)
    if flt:
        f = flt.lower(); names = [n for n in names if f in n.lower()]
    total = len(names)
    start = int(cursor or 0)
    window = names[start:start + int(max_entries)] if max_entries else names[start:]
    props = {}
    for n in window:
        try: props[n] = _ser(obj.get_editor_property(n), depth)
        except Exception: props[n] = "<unreadable>"
    nxt = start + len(window)
    return props, total, (nxt if nxt < total else None)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
target = None
for a in (eas.get_all_level_actors() or []):
    if a and (a.get_actor_label() == PARAMS["actor_label"] or a.get_name() == PARAMS["actor_label"]):
        target = a; break
if target is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_label"]}))
else:
    depth = int(PARAMS.get("max_depth") or 1)
    props, total, nxt = _dump(target, PARAMS.get("filter"), depth, PARAMS.get("max_entries"), PARAMS.get("cursor"))
    result = {"status": "success", "actor_label": target.get_actor_label(),
              "name": target.get_name(), "class": target.get_class().get_name(),
              "total_properties": total, "returned": len(props), "next_cursor": nxt, "properties": props}
    if PARAMS.get("include_components"):
        comps = {}
        for c in (target.get_components_by_class(unreal.ActorComponent) or []):
            cp, ctot, _ = _dump(c, PARAMS.get("filter"), depth, 20, 0)
            comps[c.get_name()] = {"class": c.get_class().get_name(), "total_properties": ctot, "properties": cp}
        result["components"] = comps
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_actor_properties(ctx, actor_label: str, filter: str = None,
                             include_components: bool = False, max_depth: int = 1,
                             array_element_limit: int = 25, max_entries: int = 60,
                             cursor: int = 0) -> str:
        """Dump an actor's reflected UPROPERTYs (full reflection). Read-only.

        actor_label:        actor's display label (preferred) or unique internal name.
        filter:             case-insensitive substring; only property names containing it.
        include_components: also dump each component's properties (under 'components').
        max_depth:          how deep to expand nested structs (default 1; deeper = bigger).
        array_element_limit: cap elements shown per array/set/map.
        max_entries/cursor: paginate the actor's own property list; response returns
                            'next_cursor' (pass it back as cursor to continue) or null.

        Values are JSON-serialized (vectors→[x,y,z], object refs→{__object__: path}, enums→str).
        Unreadable properties (delegates, some editor-only) are marked '<unreadable>'."""
        params = {"actor_label": actor_label, "filter": filter,
                  "include_components": include_components, "max_depth": max_depth,
                  "array_element_limit": array_element_limit,
                  "max_entries": max_entries, "cursor": cursor}
        try:
            return json.dumps(_exec(_GET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_log_tail — read the editor Output Log remotely (read-only)      #
    # ------------------------------------------------------------------ #
    _LOG_TAIL_BODY = r'''
import unreal, os, json
d = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
main = None
for f in os.listdir(d):
    if f.endswith(".log") and "-backup-" not in f:
        main = os.path.join(d, f); break
unreal.log_flush()
lines = []
if main:
    data = open(main, "rb").read().decode("utf-8", "replace")
    lines = data.splitlines()
n = int(PARAMS.get("lines") or 100)
contains = PARAMS.get("contains")
level = (PARAMS.get("level") or "").lower()
sel = lines
if level == "warning":
    sel = [l for l in sel if ": Warning:" in l]
elif level == "error":
    sel = [l for l in sel if ": Error:" in l]
elif level in ("warning+", "issues"):
    sel = [l for l in sel if (": Warning:" in l or ": Error:" in l)]
if contains:
    c = contains.lower(); sel = [l for l in sel if c in l.lower()]
tail = sel[-n:]
print("@@UMCP@@" + json.dumps({"status": "success",
    "log_file": os.path.basename(main) if main else None,
    "total_lines": len(lines), "matched": len(sel), "returned": len(tail), "lines": tail}))
'''

    @mcp.tool()
    def get_log_tail(ctx, lines: int = 100, contains: str = None, level: str = None) -> str:
        """Read the tail of the editor's Output Log (the live .log on the Windows box).
        Use this to see warnings/errors from recent operations. Read-only.

        lines:    max lines to return (from the end, after filtering).
        contains: case-insensitive substring filter.
        level:    'warning' | 'error' | 'issues' (warnings+errors) to filter by severity."""
        params = {"lines": lines, "contains": contains, "level": level}
        try:
            return json.dumps(_exec(_LOG_TAIL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # spawn_actor — place an actor in the level (ledgered write)          #
    # ------------------------------------------------------------------ #
    _SPAWN_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
actor_type = PARAMS.get("actor_type") or "StaticMeshActor"
loc = PARAMS.get("location") or [0, 0, 0]
rot = PARAMS.get("rotation") or [0, 0, 0]
scale = PARAMS.get("scale")
mesh_path = PARAMS.get("static_mesh")
cls = getattr(unreal, actor_type, None)
if cls is None:
    try: cls = unreal.load_class(None, actor_type)
    except Exception: cls = None
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown actor_type: %s" % actor_type}))
else:
    location = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
    rotation = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    result = {"status": "error", "message": "spawn failed"}
    with unreal.ScopedEditorTransaction("MCP spawn_actor"):
        actor = eas.spawn_actor_from_class(cls, location, rotation)
        if actor:
            actor.set_actor_label(name)
            if scale:
                actor.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
            if mesh_path is None and actor_type == "StaticMeshActor":
                mesh_path = "/Engine/BasicShapes/Cube.Cube"
            if mesh_path:
                comp = actor.get_editor_property("static_mesh_component")
                mesh = unreal.EditorAssetLibrary.load_asset(mesh_path)
                if comp and mesh:
                    comp.set_static_mesh(mesh)
            uniq = actor.get_name()
            _ledger().append({"op": "spawn_actor", "actor_name": uniq, "label": name})
            result = {"status": "success", "name": uniq, "label": actor.get_actor_label(),
                      "class": actor.get_class().get_name(),
                      "location": [location.x, location.y, location.z],
                      "ledger_depth": len(_ledger())}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def spawn_actor(ctx, name: str, actor_type: str = "StaticMeshActor",
                    location: list = None, rotation: list = None,
                    scale: list = None, static_mesh: str = None) -> str:
        """Spawn an actor into the active level and give it a display label.

        name:        display label for the new actor (required).
        actor_type:  engine class name (default 'StaticMeshActor') e.g. 'PointLight',
                     'CameraActor', or a loadable class path.
        location:    [x, y, z] world position (default [0,0,0]).
        rotation:    [pitch, yaw, roll] in degrees (default [0,0,0]).
        scale:       [x, y, z] scale (optional).
        static_mesh: asset path for StaticMeshActor (default '/Engine/BasicShapes/Cube.Cube'
                     when a StaticMeshActor is spawned without one).

        Ledgered write: recorded on the agent ledger so `undo` can delete it later."""
        params = {"name": name, "actor_type": actor_type, "location": location,
                  "rotation": rotation, "scale": scale, "static_mesh": static_mesh}
        try:
            return json.dumps(_exec(_SPAWN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_transform — move/rotate/scale a placed actor (ledgered)   #
    # ------------------------------------------------------------------ #
    _SET_TRANSFORM_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
loc = PARAMS.get("location"); rot = PARAMS.get("rotation"); scale = PARAMS.get("scale")
a = _resolve_actor(name)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % name}))
else:
    l = a.get_actor_location(); r = a.get_actor_rotation(); s = a.get_actor_scale3d()
    prior = {"loc": [l.x, l.y, l.z], "rot": [r.pitch, r.yaw, r.roll], "scale": [s.x, s.y, s.z]}
    with unreal.ScopedEditorTransaction("MCP set_actor_transform"):
        if loc:
            a.set_actor_location(unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])), False, False)
        if rot:
            a.set_actor_rotation(unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])), False)
        if scale:
            a.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
    _ledger().append({"op": "set_actor_transform", "actor_name": a.get_name(), "prior": prior})
    nl = a.get_actor_location(); nr = a.get_actor_rotation(); ns = a.get_actor_scale3d()
    print("@@UMCP@@" + json.dumps({"status": "success", "name": a.get_name(), "label": a.get_actor_label(),
        "transform": {"loc": [nl.x, nl.y, nl.z], "rot": [nr.pitch, nr.yaw, nr.roll], "scale": [ns.x, ns.y, ns.z]},
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_transform(ctx, name: str, location: list = None,
                            rotation: list = None, scale: list = None) -> str:
        """Move/rotate/scale a placed actor. Partial update — only the components you pass
        are changed.

        name:     actor's display label (preferred) or unique internal name.
        location: [x, y, z] world position.
        rotation: [pitch, yaw, roll] in degrees.
        scale:    [x, y, z] scale.

        Ledgered write: the prior transform is captured so `undo` restores it exactly."""
        params = {"name": name, "location": location, "rotation": rotation, "scale": scale}
        try:
            return json.dumps(_exec(_SET_TRANSFORM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_property — set a reflected property (ledgered write)       #
    # ------------------------------------------------------------------ #
    _SET_PROP_BODY = _COERCE_HELPERS + r'''
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = _resolve_actor(PARAMS["actor_label"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_label"]}))
else:
    cont, final, err = _descend(actor, PARAMS.get("component_name"), PARAMS["property_path"])
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        try:
            prior_raw = cont.get_editor_property(final); have = True
        except Exception:
            have = False
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property: %s" % final}))
        if have:
            prior_json, restorable = _settable(prior_raw)
            newv = _coerce(prior_raw, PARAMS["property_value"])
            with unreal.ScopedEditorTransaction("MCP set_actor_property"):
                cont.set_editor_property(final, newv)
            after_json, _u = _settable(cont.get_editor_property(final))
            _ledger().append({"op": "set_actor_property", "actor_name": actor.get_name(),
                "component_name": PARAMS.get("component_name"), "path": PARAMS["property_path"],
                "prior": prior_json, "restorable": restorable})
            print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
                "label": actor.get_actor_label(), "path": PARAMS["property_path"],
                "before": prior_json, "after": after_json, "restorable": restorable,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_property(ctx, actor_label: str, property_path: str,
                           property_value=None, component_name: str = None) -> str:
        """Set a reflected property on a placed actor (ledgered write).

        actor_label:   actor's display label (preferred) or unique internal name.
        property_path: property name, or dotted path through component objects
                       (e.g. 'static_mesh_component.static_mesh'). Struct sub-paths
                       (e.g. body_instance.mass...) are not supported and are refused.
        property_value: the new value. Types are coerced from the current value:
                       object/asset props accept an asset path string; vectors/rotators/
                       colors accept [..] lists; enums accept the member name string;
                       bools/numbers/strings pass through.
        component_name: optional component to target instead of the actor.

        The prior value is captured so `undo` restores it (best-effort for enums)."""
        params = {"actor_label": actor_label, "property_path": property_path,
                  "property_value": property_value, "component_name": component_name}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_actor_label — rename an actor's display label (ledgered)         #
    # ------------------------------------------------------------------ #
    _SET_LABEL_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS["label"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["label"]}))
else:
    old = actor.get_actor_label()
    with unreal.ScopedEditorTransaction("MCP set_actor_label"):
        actor.set_actor_label(PARAMS["new_label"])
    _ledger().append({"op": "set_actor_label", "actor_name": actor.get_name(), "prior_label": old})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
        "old_label": old, "new_label": actor.get_actor_label(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_actor_label(ctx, label: str, new_label: str) -> str:
        """Rename an actor's display label (the name shown in the World Outliner).

        label:     the actor's current label (or unique internal name).
        new_label: the new display label.
        Ledgered write: the old label is captured so `undo` restores it."""
        try:
            return json.dumps(_exec(_SET_LABEL_BODY, {"label": label, "new_label": new_label}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # delete_actor — SOFT delete: hide in _MCP_Trash folder (ledgered)     #
    # ------------------------------------------------------------------ #
    _DELETE_BODY = _COERCE_HELPERS + r'''
actor = _resolve_actor(PARAMS["name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["name"]}))
else:
    prior_folder = str(actor.get_folder_path())
    was_hidden = actor.is_temporarily_hidden_in_editor()
    with unreal.ScopedEditorTransaction("MCP delete_actor (soft)"):
        actor.set_folder_path(unreal.Name("_MCP_Trash"))
        actor.set_is_temporarily_hidden_in_editor(True)
    _ledger().append({"op": "delete_actor", "actor_name": actor.get_name(),
        "prior_folder": prior_folder, "was_hidden": was_hidden})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": actor.get_name(),
        "label": actor.get_actor_label(), "soft_deleted": True,
        "note": "moved to hidden _MCP_Trash; undo restores. Not destroyed/purged.",
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def delete_actor(ctx, name: str) -> str:
        """Soft-delete an actor: move it to a hidden '_MCP_Trash' outliner folder and hide
        it in the editor (it is NOT destroyed, so restoration is perfect). It stops showing
        in get_actors_in_level (unless include_trashed=True). `undo` restores it.

        name: the actor's display label (preferred) or unique internal name."""
        try:
            return json.dumps(_exec(_DELETE_BODY, {"name": name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # undo — revert our own recent edits, LIFO (agent-scoped)             #
    # ------------------------------------------------------------------ #
    _UNDO_BODY = _COERCE_HELPERS + r'''
count = int(PARAMS.get("count", 1))
led = _ledger()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
undone = []
def _save_niag(sysobj, ap):
    # NiagaraSystem structural/param undo must save via the C++ #10 save_niagara_system (sync compile +
    # C++ save) or the FortniteMain custom-version error drops the save (same bug the forward ops hit).
    m = getattr(unreal, "MCPReflectionLibrary", None)
    if m is not None and sysobj is not None and hasattr(m, "save_niagara_system"):
        try:
            m.save_niagara_system(sysobj); return
        except Exception:
            pass
    try:
        unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception:
        pass
def _bt_resolve(bt, path):
    # Walk a BehaviorTree's composite children by dot-index path ("" / "root" -> root_node). Mirrors
    # bt_write.py._resolve_comp so the BT-node undo inverses can re-find the parent to pop from.
    node = bt.get_editor_property("root_node") if bt is not None else None
    if node is None:
        return None
    ps = str(path if path is not None else "").strip()
    if ps == "" or ps == "root":
        return node
    for tok in ps.split("."):
        try:
            i = int(tok)
        except Exception:
            return None
        kids = list(node.get_editor_property("children") or [])
        if i < 0 or i >= len(kids):
            return None
        node = kids[i].get_editor_property("child_composite")
        if node is None:
            return None
    return node
def _eqs_norm(c):
    # cross-module (eqs_write.py C++#15/#17): normalize a short class name to /Script/AIModule.<Name>.
    return c if (c and ("/" in c or "." in c)) else (("/Script/AIModule." + c) if c else c)
def _eqs_pkg(p):
    seg = p.split("/")[-1]
    return p.rsplit(".", 1)[0] if "." in seg else p
def _eqs_save(p):
    try: unreal.EditorAssetLibrary.save_asset(_eqs_pkg(p), only_if_is_dirty=False)
    except Exception: pass
def _eqs_setp(o, loc, pn, v):
    # array-form value (the C++ #15 Windows fix); handler unwraps + returns bare prior in "prev".
    m = getattr(unreal, "MCPReflectionLibrary", None)
    try: return json.loads(m.set_env_query_node_property(o, loc, pn, json.dumps([v])))
    except Exception as e: return {"error": str(e)}
_MG = getattr(unreal, "MaterialEditingLibrary", None)
def _mg_mat(ap):
    # cross-module (material_graph_write.py) undo: load a base Material (not an instance).
    m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    return m if isinstance(m, unreal.Material) else None
def _mg_find(mat, nm):
    if mat is None or not nm or _MG is None:
        return None
    for e in (_MG.get_material_expressions(mat) or []):
        if e and e.get_name() == nm:
            return e
    return None
def _mg_prop(s):
    if s is None:
        return None
    s = str(s)
    return getattr(unreal.MaterialProperty, s if s.startswith("MP_") else "MP_" + s.upper(), None)
def _mg_coerce(val):
    if isinstance(val, dict):
        if "__lincolor__" in val:
            c = val["__lincolor__"]; return unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), float(c[3]))
        if "__color__" in val:
            c = val["__color__"]; return unreal.Color(r=int(c[0]), g=int(c[1]), b=int(c[2]), a=int(c[3]))
        if "__name__" in val:
            return unreal.Name(str(val["__name__"]))
        if "__object__" in val:
            p = val["__object__"]; return unreal.EditorAssetLibrary.load_asset(p) if p else None
    return val
def _mg_finish(mat, ap):
    if _MG is not None and mat is not None:
        try: _MG.recompile_material(mat)
        except Exception: pass
    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
    except Exception: pass
def _crg_ctrl(ap, gname):
    # cross-module (controlrig_graph_write.py) undo: resolve the RigVM controller for the named graph.
    bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
    if bp is None or not isinstance(bp, unreal.ControlRigBlueprint):
        return None
    try:
        if not gname or str(gname) == "RigVMModel":
            return bp.get_controller()
        for _m in (bp.get_all_models() or []):
            if _m and str(_m.get_graph_name()) == str(gname):
                return bp.get_controller(_m)
        # Function/collapse contained graphs are NOT returned by get_all_models(); they hold the
        # graph-scoped local variables (and the variable nodes referencing them). Search the function
        # library's contained graphs by name (mirrors controlrig_graph_write._get_controller). R11 fix.
        try:
            _lib = bp.get_local_function_library()
            for _fn in (_lib.get_nodes() or []):
                try:
                    _cg = _fn.get_contained_graph()
                except Exception:
                    _cg = None
                if _cg is not None and str(_cg.get_graph_name()) == str(gname):
                    return bp.get_controller(_cg)
        except Exception:
            pass
        # A specific graph was requested but not found anywhere -> fail safe (skip with a note in the
        # undo branch) rather than silently mutating the wrong (top) graph.
        return None
    except Exception:
        return None
# --- sequencer_edit.py (G1-A) undo helpers: locator + enum maps + key-state restorer ---
_SEQE_INTERP = {"constant": "RCIM_CONSTANT", "linear": "RCIM_LINEAR", "cubic": "RCIM_CUBIC"}
_SEQE_TANMODE = {"auto": "RCTM_AUTO", "user": "RCTM_USER", "break": "RCTM_BREAK", "smart_auto": "RCTM_SMART_AUTO"}
_SEQE_TANWMODE = {"none": "RCTWM_WEIGHTED_NONE", "arrive": "RCTWM_WEIGHTED_ARRIVE", "leave": "RCTWM_WEIGHTED_LEAVE", "both": "RCTWM_WEIGHTED_BOTH"}
_SEQE_EXTRAP = {"constant": "RCCE_CONSTANT", "cycle": "RCCE_CYCLE", "cycle_with_offset": "RCCE_CYCLE_WITH_OFFSET", "oscillate": "RCCE_OSCILLATE", "linear": "RCCE_LINEAR", "none": "RCCE_NONE"}
_SEQE_BLEND = {"absolute": "ABSOLUTE", "additive": "ADDITIVE", "relative": "RELATIVE", "additive_from_base": "ADDITIVE_FROM_BASE", "override": "OVERRIDE"}
def _seqe_load(path):
    _o = unreal.EditorAssetLibrary.load_asset(path) if path else None
    return _o if isinstance(_o, unreal.LevelSequence) else None
def _seqe_enum(cls_name, member, default_member=None):
    _c = getattr(unreal, cls_name, None)
    if _c is None:
        return None
    _v = getattr(_c, str(member), None)
    if _v is None and default_member is not None:
        _v = getattr(_c, default_member, None)
    return _v
def _seqe_tracks(seq, binding):
    if seq is None:
        return []
    if binding in (None, ""):
        return list(seq.get_tracks())
    for _b in seq.get_bindings():
        try:
            if str(_b.get_display_name()) == str(binding) or str(_b.get_name()) == str(binding):
                return list(_b.get_tracks())
        except Exception:
            continue
    return []
def _seqe_sec(seq, binding, ti, si):
    _tks = _seqe_tracks(seq, binding)
    if ti is None or int(ti) >= len(_tks):
        return None, None
    _tr = _tks[int(ti)]
    _secs = list(_tr.get_sections())
    if si is None or int(si) >= len(_secs):
        return _tr, None
    return _tr, _secs[int(si)]
def _seqe_ch(sec, ci):
    if sec is None or ci is None:
        return None
    _chs = list(sec.get_all_channels())
    if int(ci) >= len(_chs):
        return None
    return _chs[int(ci)]
def _seqe_key(ch, frame):
    if ch is None or frame is None:
        return None
    for _k in ch.get_keys():
        try:
            if int(_k.get_time().frame_number.value) == int(frame):
                return _k
        except Exception:
            continue
    return None
def _seqe_restore_key(k, st):
    if k is None or not st:
        return
    if st.get("tangent_mode") is not None:
        _v = _seqe_enum("RichCurveTangentMode", _SEQE_TANMODE.get(str(st.get("tangent_mode")), "RCTM_AUTO"))
        if _v is not None:
            try: k.set_tangent_mode(_v)
            except Exception: pass
    if st.get("tangent_weight_mode") is not None:
        _v = _seqe_enum("RichCurveTangentWeightMode", _SEQE_TANWMODE.get(str(st.get("tangent_weight_mode")), "RCTWM_WEIGHTED_NONE"))
        if _v is not None:
            try: k.set_tangent_weight_mode(_v)
            except Exception: pass
    if st.get("interp") is not None:
        _v = _seqe_enum("RichCurveInterpMode", _SEQE_INTERP.get(str(st.get("interp")), "RCIM_CUBIC"))
        if _v is not None:
            try: k.set_interpolation_mode(_v)
            except Exception: pass
    for _fld, _setter in (("arrive", "set_arrive_tangent"), ("leave", "set_leave_tangent"),
                          ("arrive_weight", "set_arrive_tangent_weight"), ("leave_weight", "set_leave_tangent_weight")):
        if st.get(_fld) is not None:
            try: getattr(k, _setter)(float(st.get(_fld)))
            except Exception: pass
# --- input_write.py (G2-B) undo helpers: tagged-value coerce + trigger/modifier + mapping rebuild ---
def _iw_snake(pn):
    _s = ""
    for _ch in (pn or ""):
        if _ch.isupper() and _s and not _s.endswith("_"): _s += "_"
        _s += _ch.lower()
    return _s
def _iw_coerce_val(v):
    if isinstance(v, dict):
        if "__enum__" in v:
            _et = getattr(unreal, v.get("__enum__"), None)
            return getattr(_et, v.get("token"), None) if _et else None
        if "__name__" in v: return unreal.Name(v.get("__name__"))
        if "__vec__" in v: return unreal.Vector(*v.get("__vec__"))
        if "__vec2__" in v: return unreal.Vector2D(*v.get("__vec2__"))
        if "__object__" in v: return unreal.EditorAssetLibrary.load_asset(v.get("__object__"))
        if "__skip__" in v: return None
    return v
def _iw_set(o, pn, val):
    for _cand in (pn, _iw_snake(pn)):
        try: o.set_editor_property(_cand, val); return
        except Exception: continue
def _iw_rebuild_tm(spec, outer):
    if not spec: return None
    _lc = unreal.load_class(None, spec.get("class_path"))
    if _lc is None: return None
    _inst = unreal.new_object(_lc, outer)
    for _pn, _sv in (spec.get("props") or {}).items():
        if isinstance(_sv, dict) and "__skip__" in _sv: continue
        _iw_set(_inst, _pn, _iw_coerce_val(_sv))
    return _inst
def _iw_rebuild_mapping(spec, imc):
    _m = unreal.EnhancedActionKeyMapping()
    if spec.get("action_path"):
        _a = unreal.EditorAssetLibrary.load_asset(spec.get("action_path"))
        if _a is not None: _m.set_editor_property("action", _a)
    if spec.get("key_name"):
        _kk = unreal.Key(); _kk.set_editor_property("key_name", unreal.Name(spec.get("key_name"))); _m.set_editor_property("key", _kk)
    _m.set_editor_property("triggers",  [x for x in (_iw_rebuild_tm(s, imc) for s in (spec.get("triggers")  or [])) if x is not None])
    _m.set_editor_property("modifiers", [x for x in (_iw_rebuild_tm(s, imc) for s in (spec.get("modifiers") or [])) if x is not None])
    return _m
def _iw_dkm(imc):
    _d = imc.get_editor_property("default_key_mappings"); return _d, list(_d.get_editor_property("mappings") or [])
def _iw_setdkm(imc, d, arr):
    d.set_editor_property("mappings", arr); imc.set_editor_property("default_key_mappings", d)
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _node_class(spec):
    t = getattr(unreal, spec, None) if isinstance(spec, str) else None
    if t is not None:
        return t
    c = _try(lambda: unreal.load_class(None, spec))
    if c is not None:
        return c
    return _try(lambda: unreal.load_class(None, "/Script/AIModule." + str(spec)))
def _make_node(bt, ns):
    cls = _node_class(ns.get("class"))
    if cls is None:
        return None
    obj = unreal.new_object(cls, bt)
    if ns.get("name"):
        obj.set_editor_property("node_name", ns["name"])
    if ns.get("kind") == "composite":
        svcs = []
        for s in ns.get("services", []):
            sc = _node_class(s.get("class"))
            if sc is None:
                continue
            so = unreal.new_object(sc, bt)
            if s.get("name"):
                so.set_editor_property("node_name", s["name"])
            svcs.append(so)
        obj.set_editor_property("services", svcs)
        obj.set_editor_property("children", [_make_child(bt, cs) for cs in ns.get("children", [])])
    return obj
def _make_child(bt, cs):
    ch = unreal.BTCompositeChild()
    decos = []
    for d in cs.get("decorators", []):
        dc = _node_class(d.get("class"))
        if dc is None:
            continue
        do = unreal.new_object(dc, bt)
        if d.get("name"):
            do.set_editor_property("node_name", d["name"])
        decos.append(do)
    ch.set_editor_property("decorators", decos)
    ops = []
    for ex in cs.get("decorator_ops", []):
        lo = unreal.BTDecoratorLogic(); lo.import_text(ex); ops.append(lo)
    if ops:
        ch.set_editor_property("decorator_ops", ops)
    node = cs.get("child")
    if node is not None:
        n = _make_node(bt, node)
        if node.get("kind") == "composite":
            ch.set_editor_property("child_composite", n)
        else:
            ch.set_editor_property("child_task", n)
    return ch
_KT_MAP_G7 = {"bool": "BlackboardKeyType_Bool", "int": "BlackboardKeyType_Int", "float": "BlackboardKeyType_Float",
              "vector": "BlackboardKeyType_Vector", "object": "BlackboardKeyType_Object", "name": "BlackboardKeyType_Name",
              "string": "BlackboardKeyType_String", "enum": "BlackboardKeyType_Enum", "rotator": "BlackboardKeyType_Rotator",
              "class": "BlackboardKeyType_Class"}
def _keytype_cls(short):
    if not short:
        return None
    cn = short if str(short).startswith("BlackboardKeyType_") else _KT_MAP_G7.get(str(short).lower())
    if cn is None:
        return None
    return _try(lambda: unreal.load_object(None, "/Script/AIModule." + cn))
for _ in range(count):
    if not led:
        break
    entry = led.pop()
    op = entry.get("op")
    if op == "spawn_actor":
        target = _find_by_name(entry["actor_name"])
        if target:
            with unreal.ScopedEditorTransaction("MCP undo spawn_actor"):
                eas.destroy_actor(target)
            undone.append({**entry, "result": "deleted"})
        else:
            undone.append({**entry, "result": "already-absent"})
    elif op == "set_actor_transform":
        target = _find_by_name(entry["actor_name"])
        if target:
            p = entry["prior"]
            with unreal.ScopedEditorTransaction("MCP undo set_actor_transform"):
                target.set_actor_location(unreal.Vector(p["loc"][0], p["loc"][1], p["loc"][2]), False, False)
                target.set_actor_rotation(unreal.Rotator(pitch=p["rot"][0], yaw=p["rot"][1], roll=p["rot"][2]), False)
                target.set_actor_scale3d(unreal.Vector(p["scale"][0], p["scale"][1], p["scale"][2]))
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent"})
    elif op == "set_actor_label":
        target = _find_by_name(entry["actor_name"])
        if target:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_label"):
                target.set_actor_label(entry["prior_label"])
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent"})
    elif op == "delete_actor":
        target = _find_by_name(entry["actor_name"])
        if target:
            pf = entry.get("prior_folder", "")
            with unreal.ScopedEditorTransaction("MCP undo delete_actor"):
                target.set_folder_path(unreal.Name("" if pf in ("None", "") else pf))
                target.set_is_temporarily_hidden_in_editor(bool(entry.get("was_hidden", False)))
            undone.append({**entry, "result": "restored"})
        else:
            undone.append({**entry, "result": "actor-absent (purged?)"})
    elif op == "set_actor_property":
        if not entry.get("restorable", True):
            undone.append({**entry, "result": "not-restorable (opaque struct); skipped"})
            continue
        target = _find_by_name(entry["actor_name"])
        if target is None:
            undone.append({**entry, "result": "actor-absent"}); continue
        cont, final, err = _descend(target, entry.get("component_name"), entry["path"])
        if err:
            undone.append({**entry, "result": err})
        else:
            newv = _coerce(cont.get_editor_property(final), entry["prior"])
            with unreal.ScopedEditorTransaction("MCP undo set_actor_property"):
                cont.set_editor_property(final, newv)
            undone.append({**entry, "result": "restored"})
    elif op == "select_actors" or op == "deselect_all_actors":
        # cross-module (editor_selection.py): restore the prior viewport selection
        names = entry.get("prior_selection", []) or []
        restore = [a for a in (_find_by_name(n) for n in names) if a]
        eas2 = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        with unreal.ScopedEditorTransaction("MCP undo selection"):
            eas2.set_selected_level_actors(restore)
        undone.append({**entry, "result": "selection-restored"})
    elif op == "set_object_property":
        # cross-module (objects.py). Scalar restore is supported; array restore is deferred.
        if entry.get("mode") != "scalar" or not entry.get("restorable", True):
            undone.append({**entry, "result": "skipped (array/opaque undo not yet supported)"})
            continue
        tsel = entry.get("target", {}) or {}
        root = None
        if tsel.get("actor"):
            root = _resolve_actor(tsel["actor"])
        elif tsel.get("asset_path"):
            try: root = unreal.EditorAssetLibrary.load_asset(tsel["asset_path"])
            except Exception: root = None
        if root is None:
            undone.append({**entry, "result": "target-absent"}); continue
        cont, final, err = _descend(root, tsel.get("component"), entry["path"])
        if err:
            undone.append({**entry, "result": err})
        else:
            newv = _coerce(cont.get_editor_property(final), entry["prior"])
            with unreal.ScopedEditorTransaction("MCP undo set_object_property"):
                cont.set_editor_property(final, newv)
            undone.append({**entry, "result": "restored"})
    elif op == "move_to_folder":
        # cross-module (editor_organize.py): restore each actor's prior outliner folder
        with unreal.ScopedEditorTransaction("MCP undo move_to_folder"):
            for mv in entry.get("moves", []) or []:
                t = _find_by_name(mv.get("actor_name"))
                if t and hasattr(t, "set_folder_path"):
                    pf = mv.get("prior_folder", "")
                    t.set_folder_path(unreal.Name("" if pf in ("None", "") else pf))
        undone.append({**entry, "result": "folders-restored"})
    elif op == "attach_actors" or op == "detach_actors":
        # restore each child's prior parent (re-attach), or detach if it had none
        key = "attaches" if op == "attach_actors" else "detaches"
        with unreal.ScopedEditorTransaction("MCP undo attach/detach"):
            for it in entry.get(key, []) or []:
                c = _find_by_name(it.get("child"))
                if not c or not hasattr(c, "attach_to_actor"):
                    continue
                pp = it.get("prior_parent")
                if pp:
                    p = _find_by_name(pp)
                    if p:
                        c.attach_to_actor(p, "", unreal.AttachmentRule.KEEP_WORLD,
                                          unreal.AttachmentRule.KEEP_WORLD, unreal.AttachmentRule.KEEP_WORLD, False)
                elif hasattr(c, "detach_from_actor"):
                    c.detach_from_actor(unreal.DetachmentRule.KEEP_WORLD,
                                        unreal.DetachmentRule.KEEP_WORLD, unreal.DetachmentRule.KEEP_WORLD)
        undone.append({**entry, "result": "attachment-reverted"})
    elif op == "group_actors" or op == "ungroup_actors":
        # inverse of group is ungroup (and vice-versa) on the recorded members
        members = [m for m in (_find_by_name(n) for n in (entry.get("members", []) or [])) if m]
        gu = unreal.ActorGroupingUtils.get_default_object()
        with unreal.ScopedEditorTransaction("MCP undo grouping"):
            if members:
                if op == "group_actors":
                    gu.ungroup_actors(members)
                else:
                    gu.group_actors(members)
        undone.append({**entry, "result": ("ungrouped" if op == "group_actors" else "regrouped")})
    elif op == "set_material":
        # cross-module (materials_assign.py): restore each touched slot's prior material
        target = _find_by_name(entry.get("actor_name"))
        comp = None
        if target and hasattr(target, "get_components_by_class"):
            cn = entry.get("component_name")
            for c in (target.get_components_by_class(unreal.ActorComponent) or []):
                if c.get_name() == cn:
                    comp = c; break
        if comp is None:
            undone.append({**entry, "result": "component-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_material"):
                for sl in entry.get("slots", []) or []:
                    prior = sl.get("prior")
                    mat = unreal.EditorAssetLibrary.load_asset(prior) if prior else None
                    comp.set_material(int(sl["index"]), mat)
            undone.append({**entry, "result": "materials-restored"})
    elif op == "add_component":
        # cross-module (editor_actor_components.py): delete the instanced component we added
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            root = handles[0] if handles else None
            cname = entry.get("component_name")
            victim = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == cname:
                    victim = h; break
            if victim is None or root is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo add_component"):
                    sods.k2_delete_subobject_from_instance(root, victim)
                undone.append({**entry, "result": "component-removed"})
    elif op == "rename_component":
        # cross-module (editor_actor_components.py): rename the component back to old_name
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            new_name = entry.get("new_name"); old_name = entry.get("old_name")
            h_found = None
            for h in handles:
                d = sods.k2_find_subobject_data_from_handle(h)
                o = unreal.SubobjectDataBlueprintFunctionLibrary.get_associated_object(d) if d else None
                if o is not None and o.get_name() == new_name:
                    h_found = h; break
            if h_found is None:
                undone.append({**entry, "result": "component-absent"})
            else:
                ok = sods.rename_subobject(h_found, old_name)
                undone.append({**entry, "result": ("renamed-back" if ok else "rename-failed")})
    elif op == "set_component_transform":
        # cross-module (editor_actor_components.py): restore the component's relative transform
        target = _find_by_name(entry.get("actor_name"))
        comp = None
        if target and hasattr(target, "get_components_by_class"):
            cn = entry.get("component_name")
            for c in (target.get_components_by_class(unreal.ActorComponent) or []):
                if c.get_name() == cn:
                    comp = c; break
        if comp is None:
            undone.append({**entry, "result": "component-absent"})
        else:
            prior = entry.get("prior") or {}
            with unreal.ScopedEditorTransaction("MCP undo set_component_transform"):
                if prior.get("loc") is not None:
                    v = prior["loc"]; comp.set_editor_property("relative_location", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                if prior.get("rot") is not None:
                    v = prior["rot"]; comp.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2])))
                if prior.get("scale") is not None:
                    v = prior["scale"]; comp.set_editor_property("relative_scale3d", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
            undone.append({**entry, "result": "transform-restored"})
    elif op == "set_actor_mobility":
        # cross-module (editor_actor_edit.py): restore the root component's prior mobility
        target = _find_by_name(entry.get("actor_name"))
        root = target.get_editor_property("root_component") if target else None
        if root is None:
            undone.append({**entry, "result": "actor-or-root-absent"})
        else:
            pm = getattr(unreal.ComponentMobility, entry.get("prior_mobility", "STATIC"), None)
            with unreal.ScopedEditorTransaction("MCP undo set_actor_mobility"):
                if pm is not None:
                    root.set_editor_property("mobility", pm)
            undone.append({**entry, "result": "mobility-restored"})
    elif op == "set_actor_tags":
        # cross-module (editor_actor_edit.py): restore the FULL prior Actor.Tags array
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_tags"):
                target.set_editor_property("tags", [unreal.Name(s) for s in (entry.get("prior_tags") or [])])
            undone.append({**entry, "result": "tags-restored"})
    elif op == "set_actor_collision_enabled":
        # cross-module (editor_actor_edit.py): restore prior collision-enabled state
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_actor_collision_enabled"):
                target.set_actor_enable_collision(bool(entry.get("prior_enabled")))
            undone.append({**entry, "result": "collision-restored"})
    elif op == "create_layer":
        # cross-module (editor_layers.py): delete the layer we created
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        ls.delete_layer(unreal.Name(entry.get("layer_name")))
        undone.append({**entry, "result": "layer-deleted"})
    elif op == "delete_layer":
        # cross-module (editor_layers.py): recreate the layer + re-add its prior members
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        ls.create_layer(lname)
        actors = [a for a in (_find_by_name(n) for n in (entry.get("members") or [])) if a]
        if actors:
            ls.add_actors_to_layer(actors, lname)
        undone.append({**entry, "result": "layer-recreated"})
    elif op == "add_actors_to_layer":
        # cross-module (editor_layers.py): remove from the layer the actors we added
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
        if actors:
            ls.remove_actors_from_layer(actors, lname)
        undone.append({**entry, "result": "actors-removed-from-layer"})
    elif op == "remove_actors_from_layer":
        # cross-module (editor_layers.py): re-add to the layer the actors we removed
        ls = unreal.get_editor_subsystem(unreal.LayersSubsystem)
        lname = unreal.Name(entry.get("layer_name"))
        actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
        if actors:
            ls.add_actors_to_layer(actors, lname)
        undone.append({**entry, "result": "actors-re-added-to-layer"})
    elif op == "set_world_setting":
        # cross-module (world_settings.py): restore the prior WorldSettings scalar/bool value
        if not entry.get("restorable", True):
            undone.append({**entry, "result": "not-restorable-skipped"})
        else:
            ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
            world = ues.get_editor_world() if ues else None
            if world is None:
                world = unreal.EditorLevelLibrary.get_editor_world()
            ws = world.get_world_settings() if world else None
            if ws is None:
                undone.append({**entry, "result": "world-settings-absent"})
            else:
                prop = entry.get("property")
                cur = ws.get_editor_property(prop)
                with unreal.ScopedEditorTransaction("MCP undo set_world_setting"):
                    ws.set_editor_property(prop, _coerce(cur, entry.get("prior")))
                undone.append({**entry, "result": "world-setting-restored"})
    elif op == "duplicate_asset":
        # cross-module (assets_write.py): delete the duplicate we created (+ dir if we made it)
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is not None:
            try:
                aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                if aes:
                    aes.close_all_editors_for_asset(obj)
            except Exception:
                pass
        unreal.SystemLibrary.collect_garbage()
        deleted = unreal.EditorAssetLibrary.delete_asset(ap) if ap else False
        cd = entry.get("created_dir")
        if deleted and cd:
            try:
                if not (unreal.EditorAssetLibrary.list_assets(cd, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cd)
            except Exception:
                pass
        undone.append({**entry, "result": ("duplicate-deleted" if deleted else "delete-failed")})
    elif op == "rename_asset":
        # cross-module (assets_write.py): rename back to the original path
        frm = entry.get("from_path"); to = entry.get("to_path")
        ok = unreal.EditorAssetLibrary.rename_asset(to, frm) if (frm and to) else False
        cd = entry.get("created_dir")
        if ok and cd:
            try:
                if not (unreal.EditorAssetLibrary.list_assets(cd, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cd)
            except Exception:
                pass
        undone.append({**entry, "result": ("rename-reverted" if ok else "revert-failed")})
    elif op == "create_folder":
        # cross-module (assets_write.py): remove the topmost folder we created, if still empty
        cr = entry.get("created_root")
        res = "no-dir-created"
        if cr:
            try:
                if unreal.EditorAssetLibrary.does_directory_exist(cr) and not (unreal.EditorAssetLibrary.list_assets(cr, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(cr); res = "folder-removed"
                else:
                    res = "folder-not-empty-kept"
            except Exception:
                res = "folder-remove-failed"
        undone.append({**entry, "result": res})
    elif op == "delete_folder":
        # cross-module (assets_write.py): recreate the empty folder we deleted
        p = entry.get("path")
        ok = unreal.EditorAssetLibrary.make_directory(p) if p else False
        undone.append({**entry, "result": ("folder-recreated" if ok else "recreate-failed")})
    elif op == "soft_delete_asset":
        # cross-module (assets_write.py): move the asset back out of the trash folder
        orig = entry.get("original_path"); trash = entry.get("trash_path")
        ok = unreal.EditorAssetLibrary.rename_asset(trash, orig) if (orig and trash) else False
        if ok and entry.get("created_trash_dir") and trash:
            try:
                trash_dir = trash.rsplit("/", 1)[0]
                if not (unreal.EditorAssetLibrary.list_assets(trash_dir, recursive=True) or []):
                    unreal.EditorAssetLibrary.delete_directory(trash_dir)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-restored" if ok else "restore-failed")})
    elif op == "add_actors_to_data_layer" or op == "remove_actors_from_data_layer":
        # cross-module (datalayers.py): reverse a WP data-layer membership change.
        # Resolve the instance from the live set by asset path -> full name -> short name.
        dls = unreal.get_editor_subsystem(unreal.DataLayerEditorSubsystem)
        instances = list(dls.get_all_data_layers() or [])
        def _dl_asset_path(di):
            a = None
            for m in ("get_asset", "get_data_layer_asset"):
                try:
                    a = getattr(di, m)()
                    if a:
                        break
                except Exception:
                    a = None
            try:
                return a.get_path_name() if a else None
            except Exception:
                return None
        def _dl_short(di):
            for m in ("get_data_layer_short_name", "get_data_layer_instance_name"):
                f = getattr(di, m, None)
                if f:
                    try:
                        v = str(f())
                        if v:
                            return v
                    except Exception:
                        pass
            try:
                return di.get_name()
            except Exception:
                return None
        def _dl_full(di):
            try:
                return str(di.get_data_layer_full_name())
            except Exception:
                return None
        target = None
        if entry.get("dl_asset"):
            for di in instances:
                if _dl_asset_path(di) == entry.get("dl_asset"):
                    target = di; break
        if target is None:
            for di in instances:
                if (entry.get("dl_full") and _dl_full(di) == entry.get("dl_full")) or \
                   (entry.get("dl_short") and _dl_short(di) == entry.get("dl_short")):
                    target = di; break
        if target is None:
            undone.append({**entry, "result": "data-layer-absent"})
        else:
            actors = [a for a in (_find_by_name(n) for n in (entry.get("actor_names") or [])) if a]
            if actors:
                with unreal.ScopedEditorTransaction("MCP undo data_layer membership"):
                    if op == "add_actors_to_data_layer":
                        dls.remove_actors_from_data_layer(actors, target)
                    else:
                        dls.add_actors_to_data_layer(actors, target)
            undone.append({**entry, "result": ("actors-removed-from-dl" if op == "add_actors_to_data_layer" else "actors-re-added-to-dl")})
    elif op == "create_asset":
        # GENERIC created-asset inverse (ai_write.py + future create_* commands): delete the asset
        # we created. IMPORTANT: null the loaded ref before GC+delete (a live `obj` local keeps the
        # asset referenced → delete fails). Close any auto-opened editor first; retry once after GC.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                _o = unreal.EditorAssetLibrary.load_asset(ap)
                if _o is not None:
                    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                    if aes:
                        aes.close_all_editors_for_asset(_o)
                    # BehaviorTree guard (2026-08-16): null the hand-built root so its node subobjects
                    # release before delete — a POPULATED BT otherwise trips the "asset in use" force-
                    # delete MODAL + "potentially corrupt" (see [[unrealmcp-command-buildout]] modal-poppers).
                    # LIFO undo usually empties the tree first; this makes even a direct create-undo safe.
                    if isinstance(_o, unreal.BehaviorTree):
                        try:
                            _o.set_editor_property("root_node", None)
                            unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                        except Exception:
                            pass
                _o = None
        except Exception:
            pass
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        if not deleted and entry.get("created_dir") and pkg:
            # Fallback for a freshly-created unsaved asset that resists per-asset delete inside the
            # multi-op undo snippet: force-remove the whole scratch dir we created (all its assets).
            try:
                unreal.SystemLibrary.collect_garbage()
                unreal.EditorAssetLibrary.delete_directory(pkg)
                deleted = not unreal.EditorAssetLibrary.does_asset_exist(ap)
            except Exception:
                pass
        elif deleted and entry.get("created_dir") and pkg:
            try:
                remaining = unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []
                if not remaining:
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-deleted" if deleted else "delete-failed")})
    elif op == "add_blackboard_key":
        # cross-module (ai_write.py): remove the key we added from the BlackboardData
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bb is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            kn = str(entry.get("key_name"))
            keys = bb.get_editor_property("keys") or []
            kept = [k for k in keys if str(k.get_editor_property("entry_name")) != kn]
            with unreal.ScopedEditorTransaction("MCP undo add_blackboard_key"):
                bb.set_editor_property("keys", kept)
            undone.append({**entry, "result": "key-removed"})
            keys = None; kept = None; bb = None  # release refs so a later create_asset delete can succeed
    elif op == "set_behavior_tree_blackboard":
        # cross-module (ai_write.py): restore the BT's prior blackboard asset
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bt is None:
            undone.append({**entry, "result": "behavior-tree-absent"})
        else:
            pb = entry.get("prior_blackboard")
            prior = unreal.EditorAssetLibrary.load_asset(pb) if pb else None
            with unreal.ScopedEditorTransaction("MCP undo set_behavior_tree_blackboard"):
                bt.set_editor_property("blackboard_asset", prior)
            undone.append({**entry, "result": "blackboard-restored"})
            bt = None; prior = None  # release refs so a later create_asset delete can succeed
    elif op == "set_material_instance_param":
        # cross-module (material_write.py): restore a MIC param override (or clear it if it wasn't set)
        ap = entry.get("asset_path")
        mic = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if mic is None:
            undone.append({**entry, "result": "material-instance-absent"})
        else:
            G = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
            MEL = unreal.MaterialEditingLibrary
            nm = entry.get("parameter_name"); kind = entry.get("param_kind"); prior = entry.get("prior_value")
            with unreal.ScopedEditorTransaction("MCP undo set_material_instance_param"):
                if entry.get("was_overridden"):
                    if kind == "scalar":
                        MEL.set_material_instance_scalar_parameter_value(mic, nm, float(prior), G)
                    elif kind == "vector":
                        aa = float(prior[3]) if (isinstance(prior, (list, tuple)) and len(prior) > 3) else 1.0
                        MEL.set_material_instance_vector_parameter_value(mic, nm, unreal.LinearColor(float(prior[0]), float(prior[1]), float(prior[2]), aa), G)
                    elif kind == "texture":
                        tex = unreal.EditorAssetLibrary.load_asset(prior) if prior else None
                        MEL.set_material_instance_texture_parameter_value(mic, nm, tex, G)
                else:
                    MEL.set_material_instance_parameter_override(mic, nm, False, G)
                MEL.update_material_instance(mic)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            mic = None
            undone.append({**entry, "result": "param-restored"})
    elif op == "set_sequence_playback_range":
        # cross-module (level_sequence_write.py): restore prior playback start/end
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_sequence_playback_range"):
                seq.set_playback_start(int(entry.get("prior_start")))
                seq.set_playback_end(int(entry.get("prior_end")))
            undone.append({**entry, "result": "range-restored"})
            seq = None
    elif op == "add_actor_binding":
        # cross-module (level_sequence_write.py): remove the binding we added
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name"))
            tgt = None
            for b in (seq.get_bindings() or []):
                dn = None
                try:
                    dn = str(b.get_display_name())
                except Exception:
                    dn = None
                if dn == bn or (hasattr(b, "get_name") and str(b.get_name()) == bn):
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                tgt.remove()
                undone.append({**entry, "result": "binding-removed"})
            seq = None
    elif op == "add_transform_track":
        # cross-module (level_sequence_write.py): remove the transform track we added
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name"))
            tgt = None
            for b in (seq.get_bindings() or []):
                try:
                    if str(b.get_display_name()) == bn:
                        tgt = b; break
                except Exception:
                    pass
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                tt = [t for t in (tgt.get_tracks() or []) if t.get_class().get_name() == "MovieScene3DTransformTrack"]
                if len(tt) > int(entry.get("prior_transform_track_count", 0)):
                    tgt.remove_track(tt[-1])
                    undone.append({**entry, "result": "track-removed"})
                else:
                    undone.append({**entry, "result": "track-count-unchanged"})
            seq = None
    elif op == "add_transform_keyframe":
        # cross-module (level_sequence_write.py): restore/remove the keys we set at `frame`
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); frame = int(entry.get("frame"))
            tgt = None
            for b in (seq.get_bindings() or []):
                try:
                    if str(b.get_display_name()) == bn:
                        tgt = b; break
                except Exception:
                    pass
            secs = []
            if tgt is not None:
                for t in (tgt.get_tracks() or []):
                    if t.get_class().get_name() == "MovieScene3DTransformTrack":
                        secs = t.get_sections() or []; break
            chans = secs[0].get_all_channels() if secs else []
            if not chans:
                undone.append({**entry, "result": "section-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo add_transform_keyframe"):
                    for cs in (entry.get("channels") or []):
                        idx = int(cs.get("idx"))
                        if idx >= len(chans):
                            continue
                        ch = chans[idx]
                        for k in (ch.get_keys() or []):
                            if int(k.get_time().frame_number.value) == frame:
                                if cs.get("had_key"):
                                    k.set_value(cs.get("prior_value"))
                                else:
                                    ch.remove_key(k)
                                break
                undone.append({**entry, "result": "keyframe-reverted"})
            seq = None
    elif op == "create_blueprint":
        # cross-module (blueprints_write.py): delete the blueprint asset we created
        # (and its dir if we created it empty). Close any auto-opened editor first, null the
        # loaded ref before GC+delete (a live local keeps it referenced), retry once.
        ap = entry.get("asset_path")
        try:
            if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
                _o = unreal.EditorAssetLibrary.load_asset(ap)
                if _o is not None:
                    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                    if aes:
                        aes.close_all_editors_for_asset(_o)
                _o = None
        except Exception:
            pass
        unreal.SystemLibrary.collect_garbage()
        deleted = False
        if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
            deleted = unreal.EditorAssetLibrary.delete_asset(ap)
            if not deleted:
                unreal.SystemLibrary.collect_garbage()
                deleted = unreal.EditorAssetLibrary.delete_asset(ap)
        elif ap:
            deleted = True
        pkg = entry.get("package_path")
        if deleted and entry.get("created_dir") and pkg:
            try:
                remaining = unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []
                if not remaining:
                    unreal.EditorAssetLibrary.delete_directory(pkg)
            except Exception:
                pass
        undone.append({**entry, "result": ("asset-deleted" if deleted else "delete-failed")})
    elif op == "add_bp_function":
        # cross-module (blueprints_write.py): remove the (empty) function graph we added
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            unreal.BlueprintEditorLibrary.remove_function_graph(bp, entry.get("func_name"))
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "function-removed"})
    elif op == "reparent_blueprint":
        # cross-module (blueprints_write.py): reparent back to the prior parent class
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        prior_cls = None
        pp = entry.get("prior_parent_path")
        if pp:
            try:
                prior_cls = unreal.load_object(None, pp)
            except Exception:
                prior_cls = None
        if bp is None or prior_cls is None:
            undone.append({**entry, "result": "blueprint-or-parent-absent"})
        else:
            unreal.BlueprintEditorLibrary.reparent_blueprint(bp, prior_cls)
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "reparented-back"})
    elif op == "add_wave_player_to_cue":
        # cross-module (sound_write.py): clear the wave-player node we set as the cue's first_node.
        # FAITHFUL: add_wave_player_to_cue refuses any cue whose first_node is already set, so the
        # prior state is always empty -> restoring first_node=None is exact. Save to persist.
        ap = entry.get("asset_path")
        cue = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if cue is None:
            undone.append({**entry, "result": "cue-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_wave_player_to_cue"):
                cue.set_editor_property("first_node", None)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            undone.append({**entry, "result": "wave-player-cleared"})
            cue = None
    elif op == "add_input_mapping":
        # cross-module (input_write.py): remove the one key->action mapping we added to the IMC.
        # FAITHFUL: add_mapping_to_context refuses duplicates, so the (action,key) pair had 0 prior
        # matches -> unmap_key removes exactly what we added. Save to persist.
        ap = entry.get("asset_path")
        imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        ia = unreal.EditorAssetLibrary.load_asset(entry.get("action_path")) if entry.get("action_path") else None
        if imc is None or ia is None:
            undone.append({**entry, "result": "imc-or-action-absent"})
        else:
            key = unreal.Key()
            key.set_editor_property("key_name", unreal.Name(entry.get("key_name")))
            with unreal.ScopedEditorTransaction("MCP undo add_input_mapping"):
                imc.unmap_key(ia, key)
            try:
                unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception:
                pass
            undone.append({**entry, "result": "mapping-removed"})
            imc = None; ia = None
    elif op == "set_curve_keys":
        # cross-module (curves_write.py): restore the curve's prior key state via the C++ #4 handler.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "set_curve_keys_json"):
            undone.append({**entry, "result": "curve-or-handler-absent"})
        else:
            mrl.set_curve_keys_json(obj, json.dumps({"channels": entry.get("prior_channels") or []}))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "curve-keys-restored"})
            obj = None
    elif op == "add_struct_field":
        # cross-module (structs_write.py): remove the field we added (matched by friendly name).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "remove_struct_field"):
            undone.append({**entry, "result": "struct-or-handler-absent"})
        else:
            mrl.remove_struct_field(obj, entry.get("field_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "struct-field-removed"})
            obj = None
    elif op == "remove_struct_field":
        # cross-module (structs_write.py): re-add the removed field (same name + type; new GUID).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ft = entry.get("field_type")
        if obj is None or mrl is None or not hasattr(mrl, "add_struct_field") or not ft:
            undone.append({**entry, "result": "struct-handler-or-type-absent"})
        else:
            mrl.add_struct_field(obj, entry.get("field_name"), ft)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "struct-field-re-added"})
            obj = None
    elif op == "add_enum_entry":
        # cross-module (structs_write.py): remove the enumerator we appended (by index).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        idx = entry.get("index")
        if obj is None or mrl is None or not hasattr(mrl, "remove_enum_entry") or idx is None:
            undone.append({**entry, "result": "enum-or-handler-absent"})
        else:
            mrl.remove_enum_entry(obj, int(idx))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "enum-entry-removed"})
            obj = None
    elif op == "remove_enum_entry":
        # cross-module (structs_write.py): re-add the removed enumerator (APPENDS at end; best-effort).
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if obj is None or mrl is None or not hasattr(mrl, "add_enum_entry"):
            undone.append({**entry, "result": "enum-or-handler-absent"})
        else:
            mrl.add_enum_entry(obj, entry.get("prior_display_name") or "")
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "enum-entry-re-added"})
            obj = None
    elif op == "add_emitter":
        # cross-module (niagara_write.py): remove the emitter handle we added (by id). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        hid = entry.get("handle_id")
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_emitter_from_system") or not hid:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_emitter_from_system(sysobj, str(hid))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-removed"})
            sysobj = None
    elif op == "remove_emitter":
        # cross-module (niagara_write.py): re-add the removed emitter from its captured source (best-effort;
        # a copied emitter has no recoverable source, so undo only works if source_emitter_path was given).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        sp = entry.get("source_emitter_path")
        src = unreal.EditorAssetLibrary.load_asset(sp) if sp else None
        if sysobj is None or mrl is None or not hasattr(mrl, "add_emitter_to_system"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif src is None:
            undone.append({**entry, "result": "cannot-restore (no source_emitter_path captured)"})
        else:
            mrl.add_emitter_to_system(sysobj, src, "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-re-added"})
            sysobj = None; src = None
    elif op == "add_user_param":
        # cross-module (niagara_write.py, C++ #10): remove the user parameter we added. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_user_parameter"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_user_parameter(sysobj, entry.get("param"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-removed"})
            sysobj = None
    elif op == "set_user_param":
        # cross-module (niagara_write.py, C++ #10): restore prior value (captured before the set). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pj = entry.get("prev_value_json", "null")
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_user_parameter_value"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif pj is None or pj == "null":
            undone.append({**entry, "result": "no-prior-value-skipped"})
        else:
            mrl.set_niagara_user_parameter_value(sysobj, entry.get("param"), pj)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-restored"})
            sysobj = None
    elif op == "remove_user_param":
        # cross-module (niagara_write.py, C++ #10): re-add with captured type + value. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        vj = entry.get("value_json", "null")
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_user_parameter"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.add_niagara_user_parameter(sysobj, entry.get("param"), entry.get("type"))
            if vj is not None and vj != "null" and hasattr(mrl, "set_niagara_user_parameter_value"):
                mrl.set_niagara_user_parameter_value(sysobj, entry.get("param"), vj)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "user-param-re-added"})
            sysobj = None
    elif op == "rename_niagara_emitter":
        # cross-module (niagara_write.py, C++ #10): rename back new_name -> old_name. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "rename_niagara_emitter_handle"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.rename_niagara_emitter_handle(sysobj, entry.get("new_name"), entry.get("old_name"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "emitter-renamed-back"})
            sysobj = None
    elif op == "add_niagara_renderer":
        # cross-module (niagara_write.py, C++ #10): remove the renderer we appended (by index).
        # FAITHFUL if the list did not shift (undo unwinds LIFO, so it holds in practice).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ri = entry.get("renderer_index")
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_renderer") or ri is None:
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_renderer(sysobj, entry.get("emitter"), int(ri))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "renderer-removed"})
            sysobj = None
    elif op == "remove_niagara_renderer":
        # cross-module (niagara_write.py, C++ #10): re-add a renderer of the same TYPE. BEST-EFFORT
        # (default props; re-appends at end — custom material/mesh/bindings are not restored).
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        rt = entry.get("renderer_type") or ""
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_renderer"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif not rt:
            undone.append({**entry, "result": "cannot-restore (no renderer_type captured)"})
        else:
            mrl.add_niagara_renderer(sysobj, entry.get("emitter"), rt)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "renderer-re-added (best-effort)"})
            sysobj = None
    elif op == "add_module":
        # cross-module (niagara_write.py, C++ #10 area C): remove the module node we added (by guid). FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if sysobj is None or mrl is None or not hasattr(mrl, "remove_niagara_module_from_stack"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.remove_niagara_module_from_stack(sysobj, entry.get("emitter_name"),
                                                 entry.get("script_usage"), entry.get("node_guid"))
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-removed"})
            sysobj = None
    elif op == "set_module_input":
        # cross-module (niagara_write.py, C++ #10 area C): restore prior scalar input value. FAITHFUL.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        pv = entry.get("prior_value")
        if sysobj is None or mrl is None or not hasattr(mrl, "set_niagara_module_input"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        else:
            mrl.set_niagara_module_input(sysobj, entry.get("emitter_name"), entry.get("script_usage"),
                                         entry.get("module_name"), entry.get("input_name"), pv if pv is not None else "")
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-input-restored"})
            sysobj = None
    elif op == "remove_module":
        # cross-module (niagara_write.py, C++ #10 area C): re-add the module. BEST-EFFORT (new guid, default
        # inputs) and only if module_script_path was captured; otherwise cannot restore.
        ap = entry.get("asset_path")
        sysobj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        mp = entry.get("module_script_path") or ""
        if sysobj is None or mrl is None or not hasattr(mrl, "add_niagara_module_to_stack"):
            undone.append({**entry, "result": "system-or-handler-absent"})
        elif not mp:
            undone.append({**entry, "result": "cannot-restore (no module_script_path captured)"})
        else:
            mrl.add_niagara_module_to_stack(sysobj, entry.get("emitter_name"), entry.get("script_usage"), mp)
            _save_niag(sysobj, ap)
            undone.append({**entry, "result": "module-re-added (best-effort, new guid)"})
            sysobj = None
    elif op == "bt_set_root":
        # cross-module (bt_write.py): clear the root composite we set. FAITHFUL (set only on empty root).
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bt is None:
            undone.append({**entry, "result": "bt-absent"})
        else:
            bt.set_editor_property("root_node", None)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-root-cleared"})
    elif op == "bt_add_child":
        # cross-module (bt_write.py): pop the child (composite or task) we appended. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        idx = entry.get("index")
        if bt is None or parent is None or idx is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= idx < len(kids):
                del kids[idx]
                parent.set_editor_property("children", kids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-child-removed"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "bt_add_service":
        # cross-module (bt_write.py): pop the service we appended to a composite. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        comp = _bt_resolve(bt, entry.get("composite_path")) if bt is not None else None
        idx = entry.get("index")
        if bt is None or comp is None or idx is None:
            undone.append({**entry, "result": "bt-or-composite-absent"})
        else:
            svcs = list(comp.get_editor_property("services") or [])
            if 0 <= idx < len(svcs):
                del svcs[idx]
                comp.set_editor_property("services", svcs)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-service-removed"})
            else:
                undone.append({**entry, "result": "bt-service-index-stale"})
    elif op == "bt_add_decorator":
        # cross-module (bt_write.py): pop the decorator we appended to a child slot. FAITHFUL under LIFO.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        ci = entry.get("child_index"); idx = entry.get("index")
        if bt is None or parent is None or ci is None or idx is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= ci < len(kids):
                ch = kids[ci]
                decos = list(ch.get_editor_property("decorators") or [])
                if 0 <= idx < len(decos):
                    del decos[idx]
                    ch.set_editor_property("decorators", decos)
                    kids[ci] = ch
                    parent.set_editor_property("children", kids)
                    try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                    except Exception: pass
                    undone.append({**entry, "result": "bt-decorator-removed"})
                else:
                    undone.append({**entry, "result": "bt-decorator-index-stale"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "set_cvar":
        # cross-module (console.py): restore the cvar to its captured prior value. FAITHFUL (value
        # restore; the engine's LastSetBy tag reads 'Console' after a programmatic set — cosmetic).
        nm = entry.get("name"); pv = entry.get("prior_value")
        if nm is None or pv is None:
            undone.append({**entry, "result": "cvar-entry-incomplete"})
        else:
            try:
                unreal.SystemLibrary.execute_console_command(None, str(nm) + " " + str(pv))
                unreal.log_flush()
                undone.append({**entry, "result": "cvar-restored"})
            except Exception as _e:
                undone.append({**entry, "result": "cvar-restore-failed"})
    elif op == "datatable_add_row" or op == "datatable_duplicate_row":
        # cross-module (datatable_write.py): the row was absent before this op, so remove it. FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        rn = entry.get("row_name") or entry.get("new_row_name")
        if dt is None or not isinstance(dt, unreal.DataTable) or rn is None:
            undone.append({**entry, "result": "datatable-or-row-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo datatable add/dup row"):
                dt.modify()
                unreal.DataTableFunctionLibrary.remove_data_table_row(dt, rn)
            undone.append({**entry, "result": "datatable-row-removed"})
    elif op == "datatable_remove_row":
        # cross-module (datatable_write.py): re-insert the captured prior row at its original index via the
        # JSON round-trip (restores values AND order). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pe = entry.get("prior_entry"); pidx = entry.get("prior_index")
        if dt is None or not isinstance(dt, unreal.DataTable) or pe is None:
            undone.append({**entry, "result": "datatable-or-prior-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            _ins = int(pidx) if isinstance(pidx, int) and pidx >= 0 else len(_arr)
            if _ins > len(_arr):
                _ins = len(_arr)
            _arr = _arr[:_ins] + [dict(pe)] + _arr[_ins:]
            with unreal.ScopedEditorTransaction("MCP undo datatable remove row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-restored"})
    elif op == "datatable_update_row":
        # cross-module (datatable_write.py): restore the full captured prior row values via the JSON
        # round-trip (field-for-field). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        rn = entry.get("row_name"); pe = entry.get("prior_entry")
        if dt is None or not isinstance(dt, unreal.DataTable) or pe is None or rn is None:
            undone.append({**entry, "result": "datatable-or-prior-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            for _i in range(len(_arr)):
                if str(_arr[_i].get("Name")) == str(rn):
                    _arr[_i] = dict(pe); break
            with unreal.ScopedEditorTransaction("MCP undo datatable update row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-reverted"})
    elif op == "datatable_rename_row":
        # cross-module (datatable_write.py): rename new_name back to old_name via the JSON round-trip
        # (fields + order preserved). FAITHFUL.
        ap = entry.get("asset_path")
        dt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        on = entry.get("old_name"); nn = entry.get("new_name")
        if dt is None or not isinstance(dt, unreal.DataTable) or on is None or nn is None:
            undone.append({**entry, "result": "datatable-or-names-absent"})
        else:
            _dfl = unreal.DataTableFunctionLibrary
            try:
                _arr = json.loads(_dfl.export_data_table_to_json_string(dt) or "[]")
            except Exception:
                _arr = []
            _arr = [e for e in _arr if isinstance(e, dict)]
            for _i in range(len(_arr)):
                if str(_arr[_i].get("Name")) == str(nn):
                    _arr[_i] = dict(_arr[_i]); _arr[_i]["Name"] = on; break
            with unreal.ScopedEditorTransaction("MCP undo datatable rename row"):
                dt.modify()
                _dfl.fill_data_table_from_json_string(dt, json.dumps(_arr))
            undone.append({**entry, "result": "datatable-row-renamed-back"})
    elif op == "add_anim_notify_track":
        # cross-module (anim_write.py): remove the empty track we added. FAITHFUL (created empty).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_notify_track"):
                unreal.AnimationLibrary.remove_animation_notify_track(anim, tn)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-track-removed"})
    elif op == "remove_anim_notify_track":
        # cross-module (anim_write.py): re-add the (empty) track. FAITHFUL structurally (color -> default).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_notify_track"):
                unreal.AnimationLibrary.add_animation_notify_track(anim, tn, unreal.LinearColor(1.0, 1.0, 1.0, 1.0))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-track-readded"})
    elif op == "add_anim_notify":
        # cross-module (anim_write.py): no per-event removal API -> clear the track and rebuild its prior
        # events. FAITHFUL for the added notify; siblings restored by class/time/duration (custom values ->
        # class defaults, documented).
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            _AL = unreal.AnimationLibrary
            with unreal.ScopedEditorTransaction("MCP undo add_anim_notify"):
                _AL.remove_animation_notify_events_by_track(anim, tn)
                for _pe in (entry.get("prior_events") or []):
                    _cp = _pe.get("class_path")
                    _cls = None
                    if _cp:
                        try: _cls = unreal.load_object(None, _cp)
                        except Exception: _cls = None
                    if _cls is None:
                        continue
                    if _pe.get("kind") == "state":
                        _AL.add_animation_notify_state_event(anim, tn, float(_pe.get("time") or 0.0), float(_pe.get("duration") or 0.0), _cls)
                    else:
                        _AL.add_animation_notify_event(anim, tn, float(_pe.get("time") or 0.0), _cls)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-notify-track-rebuilt"})
    elif op == "add_anim_sync_marker":
        # cross-module (anim_write.py): remove the marker by name (add enforced name uniqueness). FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        mn = entry.get("marker_name")
        if anim is None or mn is None:
            undone.append({**entry, "result": "anim-or-marker-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_sync_marker"):
                unreal.AnimationLibrary.remove_animation_sync_markers_by_name(anim, mn)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-marker-removed"})
    elif op == "add_anim_curve":
        # cross-module (anim_write.py): remove the empty float curve we created. FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_anim_curve"):
                unreal.AnimationLibrary.remove_curve(anim, cn, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-curve-removed"})
    elif op == "remove_anim_curve":
        # cross-module (anim_write.py): re-create the float curve and restore its keys (time+value). FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            _AL = unreal.AnimationLibrary
            _RCTF = unreal.RawCurveTrackTypes.RCT_FLOAT
            _pk = entry.get("prior_keys") or []
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_curve"):
                if not _AL.does_curve_exist(anim, cn, _RCTF):
                    _AL.add_curve(anim, cn, _RCTF, False)
                if _pk:
                    _AL.add_float_curve_keys(anim, cn, [float(k.get("time") or 0.0) for k in _pk], [float(k.get("value") or 0.0) for k in _pk])
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-curve-restored"})
    elif op == "set_anim_curve_key":
        # cross-module (anim_write.py): curve was new -> remove it; else rebuild the prior keys. FAITHFUL.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cn = entry.get("curve_name")
        if anim is None or cn is None:
            undone.append({**entry, "result": "anim-or-curve-absent"})
        else:
            _AL = unreal.AnimationLibrary
            _RCTF = unreal.RawCurveTrackTypes.RCT_FLOAT
            _pk = entry.get("prior_keys") or []
            with unreal.ScopedEditorTransaction("MCP undo set_anim_curve_key"):
                if _AL.does_curve_exist(anim, cn, _RCTF):
                    _AL.remove_curve(anim, cn, False)
                if entry.get("existed"):
                    _AL.add_curve(anim, cn, _RCTF, False)
                    if _pk:
                        _AL.add_float_curve_keys(anim, cn, [float(k.get("time") or 0.0) for k in _pk], [float(k.get("value") or 0.0) for k in _pk])
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-key-reverted"})
    elif op == "set_anim_rate_scale":
        # cross-module (anim_write.py): restore the prior rate scale. FAITHFUL scalar.
        ap = entry.get("asset_path")
        anim = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pr = entry.get("prior")
        if anim is None or pr is None:
            undone.append({**entry, "result": "anim-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_anim_rate_scale"):
                unreal.AnimationLibrary.set_rate_scale(anim, float(pr))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "anim-rate-restored"})
    elif op == "add_foliage_instances":
        # cross-module (foliage_write.py): remove all instances of this (unique MCP_A_) foliage type, then
        # destroy the IFA only if THIS add created it and it is now empty. FAITHFUL (type is single-use).
        ap = entry.get("foliage_type_path")
        ftype = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        _world = _ues.get_editor_world() if _ues else None
        if ftype is None or _world is None:
            undone.append({**entry, "result": "type-or-world-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_foliage_instances"):
                unreal.InstancedFoliageActor.get_default_object().remove_all_instances(_world, ftype)
            if entry.get("created_ifa"):
                names = set(entry.get("ifa_names") or [])
                for a in (eas.get_all_level_actors() or []):
                    if a and a.get_name() in names and a.get_class().get_name() == "InstancedFoliageActor":
                        tot = 0
                        for c in (a.get_components_by_class(unreal.InstancedStaticMeshComponent) or []):
                            try: tot += c.get_instance_count()
                            except Exception: pass
                        if tot == 0:
                            eas.destroy_actor(a)
            undone.append({**entry, "result": "foliage-instances-removed"})
            ftype = None
    elif op == "add_rig_element":
        # cross-module (controlrig_write.py): remove the hierarchy element we added. FAITHFUL (leaf add).
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("key_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=et)
            with unreal.ScopedEditorTransaction("MCP undo add_rig_element"):
                bp.get_hierarchy_controller().remove_element(_key, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-element-removed"})
    elif op == "remove_rig_element":
        # cross-module (controlrig_write.py): rebuild the removed leaf element from captured state
        # (parent, transform, and for controls the FRigControlSettings/Value via import_text). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint):
            undone.append({**entry, "result": "cr-absent"})
        else:
            hc = bp.get_hierarchy_controller(); h = bp.get_hierarchy()
            short = str(entry.get("element_type") or "").lower()
            nm = unreal.Name(str(entry.get("name")))
            _pn = entry.get("parent_name")
            if _pn:
                _pet = getattr(unreal.RigElementType, str(entry.get("parent_type") or "bone").upper(), unreal.RigElementType.BONE)
                pk = unreal.RigElementKey(name=unreal.Name(str(_pn)), type=_pet)
            else:
                pk = unreal.RigElementKey()
            _ident = unreal.Transform()
            newkey = None
            with unreal.ScopedEditorTransaction("MCP undo remove_rig_element"):
                if short == "bone":
                    newkey = hc.add_bone(nm, pk, _ident, False, unreal.RigBoneType.USER, False, False)
                elif short == "null":
                    newkey = hc.add_null(nm, pk, _ident, False, False, False)
                elif short == "control":
                    _cs = unreal.RigControlSettings()
                    if entry.get("settings_txt"): _cs.import_text(entry.get("settings_txt"))
                    _val = unreal.RigControlValue()
                    if entry.get("value_txt"): _val.import_text(entry.get("value_txt"))
                    newkey = hc.add_control(nm, pk, _cs, _val, False, False)
                if newkey is not None:
                    _x = entry.get("in_local_xf")
                    if _x:
                        _t = unreal.Transform()
                        _t.set_editor_property("translation", unreal.Vector(_x["t"][0], _x["t"][1], _x["t"][2]))
                        _t.set_editor_property("rotation", unreal.Quat(_x["q"][0], _x["q"][1], _x["q"][2], _x["q"][3]))
                        _t.set_editor_property("scale3d", unreal.Vector(_x["s"][0], _x["s"][1], _x["s"][2]))
                        h.set_local_transform(newkey, _t, False, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("cr-element-restored" if newkey is not None else "cr-restore-unsupported-type")})
    elif op == "set_rig_control_settings":
        # cross-module (controlrig_write.py): restore the exact prior FRigControlSettings. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_settings_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _s = unreal.RigControlSettings(); _s.import_text(pt)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_settings"):
                bp.get_hierarchy_controller().set_control_settings(_key, _s, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-settings-restored"})
    elif op == "set_rig_element_transform":
        # cross-module (controlrig_write.py): restore the captured prior transform. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("key_type") or "").upper(), None)
        px = entry.get("prior_xf")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None or px is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=et)
            _t = unreal.Transform()
            _t.set_editor_property("translation", unreal.Vector(px["t"][0], px["t"][1], px["t"][2]))
            _t.set_editor_property("rotation", unreal.Quat(px["q"][0], px["q"][1], px["q"][2], px["q"][3]))
            _t.set_editor_property("scale3d", unreal.Vector(px["s"][0], px["s"][1], px["s"][2]))
            _init = bool(entry.get("initial"))
            with unreal.ScopedEditorTransaction("MCP undo set_rig_element_transform"):
                if str(entry.get("space")) == "global":
                    h.set_global_transform(_key, _t, _init, True, False, False)
                else:
                    h.set_local_transform(_key, _t, _init, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-transform-restored"})
    elif op == "set_rig_control_value":
        # cross-module (controlrig_write.py G1-B): restore the control's captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pv = entry.get("prior_value_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pv is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _v = unreal.RigControlValue(); _v.import_text(pv)
            _vt = getattr(unreal.RigControlValueType, str(entry.get("value_type") or "current").upper(), unreal.RigControlValueType.CURRENT)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_value"):
                h.set_control_value(_key, _v, _vt, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-value-restored"})
    elif op == "set_rig_control_offset":
        # cross-module (controlrig_write.py G1-B): re-import the control's captured element text
        # (restores offset/settings/value of that control). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_element_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_offset"):
                bp.get_hierarchy_controller().import_from_text(pt, True, False, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-offset-restored"})
    elif op == "set_rig_control_shape":
        # cross-module (controlrig_write.py G1-B): restore prior FRigControlSettings + local shape transform. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pst = entry.get("prior_settings_txt"); psx = entry.get("prior_shape_xf")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pst is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            h = bp.get_hierarchy(); hc = bp.get_hierarchy_controller()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("key_name"))), type=unreal.RigElementType.CONTROL)
            _ns = unreal.RigControlSettings(); _ns.import_text(pst)
            with unreal.ScopedEditorTransaction("MCP undo set_rig_control_shape"):
                hc.set_control_settings(_key, _ns, False)
                if psx:
                    _t = unreal.Transform()
                    _t.set_editor_property("translation", unreal.Vector(psx["t"][0], psx["t"][1], psx["t"][2]))
                    _t.set_editor_property("rotation", unreal.Quat(psx["q"][0], psx["q"][1], psx["q"][2], psx["q"][3]))
                    _t.set_editor_property("scale3d", unreal.Vector(psx["s"][0], psx["s"][1], psx["s"][2]))
                    h.set_control_shape_transform(_key, _t, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-control-shape-restored"})
    elif op == "set_rig_element_parent":
        # cross-module (controlrig_write.py G1-B): restore the element's prior parent (or unparent if it was root). FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        cet = getattr(unreal.RigElementType, str(entry.get("child_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or cet is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            hc = bp.get_hierarchy_controller()
            _child = unreal.RigElementKey(name=unreal.Name(str(entry.get("child_name"))), type=cet)
            _mg = bool(entry.get("maintain_global"))
            _ppn = entry.get("prior_parent_name")
            with unreal.ScopedEditorTransaction("MCP undo set_rig_element_parent"):
                if _ppn:
                    _ppet = getattr(unreal.RigElementType, str(entry.get("prior_parent_type") or "bone").upper(), unreal.RigElementType.BONE)
                    _pk = unreal.RigElementKey(name=unreal.Name(str(_ppn)), type=_ppet)
                    hc.set_parent(_child, _pk, _mg, False, False)
                else:
                    hc.remove_all_parents(_child, _mg, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-parent-restored"})
    elif op == "rename_rig_element":
        # cross-module (controlrig_write.py G1-B): rename the element back to its old name. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        et = getattr(unreal.RigElementType, str(entry.get("element_type") or "").upper(), None)
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or et is None:
            undone.append({**entry, "result": "cr-or-type-absent"})
        else:
            hc = bp.get_hierarchy_controller()
            _key = unreal.RigElementKey(name=unreal.Name(str(entry.get("new_name"))), type=et)
            with unreal.ScopedEditorTransaction("MCP undo rename_rig_element"):
                hc.rename_element(_key, unreal.Name(str(entry.get("old_name"))), False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-element-renamed-back"})
    elif op == "restore_rig_hierarchy":
        # cross-module (controlrig_write.py G1-B): only emitted by build_rig_hierarchy(clear_existing=True).
        # Wipe the rebuilt hierarchy + re-import the captured original. FAITHFUL.
        ap = entry.get("asset_path")
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        pt = entry.get("prior_hierarchy_txt")
        if bp is None or not isinstance(bp, unreal.ControlRigBlueprint) or pt is None:
            undone.append({**entry, "result": "cr-or-prior-absent"})
        else:
            hc = bp.get_hierarchy_controller(); h = bp.get_hierarchy()
            with unreal.ScopedEditorTransaction("MCP undo restore_rig_hierarchy"):
                for _k in reversed(list(h.get_all_keys(True))):
                    hc.remove_element(_k, False, False)
                hc.import_from_text(pt, False, True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "cr-hierarchy-restored"})
    elif op == "seq_set_key_value":
        # cross-module (sequencer_edit.py G1-A): restore the key's prior value. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_value"):
                _k.set_value(entry.get("prior_value"))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-value-restored"})
    elif op == "seq_set_key_time":
        # cross-module (sequencer_edit.py G1-A): move the key back to its prior frame. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("new_frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_time"):
                _k.set_time(unreal.FrameNumber(int(entry.get("prior_frame"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-time-restored"})
    elif op == "seq_set_key_interp":
        # cross-module (sequencer_edit.py G1-A): restore prior interp + tangent mode. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_interp"):
                _iv = _seqe_enum("RichCurveInterpMode", _SEQE_INTERP.get(str(entry.get("prior_interp")), "RCIM_CUBIC"))
                if _iv is not None: _k.set_interpolation_mode(_iv)
                _tv = _seqe_enum("RichCurveTangentMode", _SEQE_TANMODE.get(str(entry.get("prior_tangent_mode")), "RCTM_AUTO"))
                if _tv is not None: _k.set_tangent_mode(_tv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-interp-restored"})
    elif op == "seq_set_key_tangent":
        # cross-module (sequencer_edit.py G1-A): restore full prior key tangent state. FAITHFUL (USER/BREAK exact).
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _k = _seqe_key(_seqe_ch(_sec, entry.get("channel_index")), entry.get("frame"))
        if _k is None:
            undone.append({**entry, "result": "seq-key-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_key_tangent"):
                _seqe_restore_key(_k, entry.get("prior") or {})
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-tangent-restored"})
    elif op == "seq_remove_key":
        # cross-module (sequencer_edit.py G1-A): re-add the removed key + restore its full state. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        _ks = entry.get("key_state") or {}
        if _ch is None or _ks.get("frame") is None:
            undone.append({**entry, "result": "seq-channel-or-state-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_remove_key"):
                _nk = _ch.add_key(unreal.FrameNumber(int(_ks.get("frame"))), _ks.get("value"))
                _seqe_restore_key(_nk, _ks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "key-readded"})
    elif op == "seq_set_channel_default":
        # cross-module (sequencer_edit.py G1-A): restore prior channel default (or clear it). FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        if _ch is None:
            undone.append({**entry, "result": "seq-channel-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_channel_default"):
                if entry.get("had_default"):
                    _ch.set_default(entry.get("prior_default"))
                else:
                    try: _ch.remove_default()
                    except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "channel-default-restored"})
    elif op == "seq_set_channel_extrap":
        # cross-module (sequencer_edit.py G1-A): restore prior pre/post-infinity extrapolation. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _ch = _seqe_ch(_sec, entry.get("channel_index"))
        if _ch is None:
            undone.append({**entry, "result": "seq-channel-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_channel_extrap"):
                _pre = _seqe_enum("RichCurveExtrapolation", _SEQE_EXTRAP.get(str(entry.get("prior_pre")), "RCCE_CONSTANT"))
                _post = _seqe_enum("RichCurveExtrapolation", _SEQE_EXTRAP.get(str(entry.get("prior_post")), "RCCE_CONSTANT"))
                if _pre is not None: _ch.set_pre_infinity_extrapolation(_pre)
                if _post is not None: _ch.set_post_infinity_extrapolation(_post)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "channel-extrap-restored"})
    elif op == "seq_set_section_range":
        # cross-module (sequencer_edit.py G1-A): restore the section's prior frame range. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None:
            undone.append({**entry, "result": "seq-section-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_range"):
                if entry.get("prior_has_start") and entry.get("prior_has_end"):
                    _sec.set_range(int(entry.get("prior_start")), int(entry.get("prior_end")))
                else:
                    if entry.get("prior_has_start"):
                        _sec.set_start_frame(int(entry.get("prior_start")))
                    else:
                        try: _sec.set_start_frame_bounded(False)
                        except Exception: pass
                    if entry.get("prior_has_end"):
                        _sec.set_end_frame(int(entry.get("prior_end")))
                    else:
                        try: _sec.set_end_frame_bounded(False)
                        except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-range-restored"})
    elif op == "seq_set_section_easing":
        # cross-module (sequencer_edit.py G1-A): restore prior ease-in/out durations. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None:
            undone.append({**entry, "result": "seq-section-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_easing"):
                if entry.get("prior_ease_in") is not None: _sec.set_ease_in_duration(int(entry.get("prior_ease_in")))
                if entry.get("prior_ease_out") is not None: _sec.set_ease_out_duration(int(entry.get("prior_ease_out")))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-easing-restored"})
    elif op == "seq_set_section_blend":
        # cross-module (sequencer_edit.py G1-A): restore prior blend type (raw enum, not the Optional wrapper). FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        if _sec is None or not entry.get("prior_valid"):
            undone.append({**entry, "result": "seq-section-or-prior-absent"})
        else:
            _bv = _seqe_enum("MovieSceneBlendType", _SEQE_BLEND.get(str(entry.get("prior_blend_type")), "ABSOLUTE"))
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_blend"):
                if _bv is not None: _sec.set_blend_type(_bv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-blend-restored"})
    elif op == "seq_set_section_property":
        # cross-module (sequencer_edit.py G1-A): restore a universal section editor-property. FAITHFUL (scalar/enum/vector/color/object).
        seq = _seqe_load(entry.get("asset_path"))
        _tr, _sec = _seqe_sec(seq, entry.get("binding"), entry.get("track_index"), entry.get("section_index"))
        _pn = entry.get("property_name")
        if _sec is None or not _pn:
            undone.append({**entry, "result": "seq-section-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_section_property"):
                _nv = _coerce(_sec.get_editor_property(_pn), entry.get("prior_value"))
                _sec.set_editor_property(_pn, _nv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "section-property-restored"})
    elif op == "seq_set_track_property":
        # cross-module (sequencer_edit.py G1-A): restore a universal track editor-property. FAITHFUL.
        seq = _seqe_load(entry.get("asset_path"))
        _tks = _seqe_tracks(seq, entry.get("binding"))
        _ti = entry.get("track_index")
        _tr = _tks[int(_ti)] if (_ti is not None and int(_ti) < len(_tks)) else None
        _pn = entry.get("property_name")
        if _tr is None or not _pn:
            undone.append({**entry, "result": "seq-track-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_track_property"):
                _nv = _coerce(_tr.get_editor_property(_pn), entry.get("prior_value"))
                _tr.set_editor_property(_pn, _nv)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "track-property-restored"})
    elif op == "set_curve_table":
        # cross-module (curves_write_ext.py G2-A): whole-table restore from prior JSON snapshot (json import
        # is a full REPLACE). Shared by set/delete/rename row + import_curve_table. FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is None or not isinstance(obj, unreal.CurveTable):
            undone.append({**entry, "result": "curve-table-absent"})
        else:
            # True REPLACE: clear ALL current rows via remove_curve_table_row (json_to_curve_table
            # does NOT reliably clear, and "[]" FAILS to parse for a CurveTable), then re-import the
            # prior rows only if the prior table was non-empty (valid JSON; an empty prior is the
            # non-JSON sentinel "No data in row curve!"). G2 re-verify fix #2.
            _dtfl = unreal.DataTableFunctionLibrary
            _pj = entry.get("prior_json") or ""
            _pjs = str(_pj).strip()
            _prior_is_json = _pjs.startswith("[") or _pjs.startswith("{")
            with unreal.ScopedEditorTransaction("MCP undo set_curve_table"):
                for _rn in list(_dtfl.get_curve_table_row_names(obj) or []):
                    _dtfl.remove_curve_table_row(obj, _rn)
                if _prior_is_json:
                    _dtfl.json_to_curve_table(obj, _pj)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            _rows_now = len(list(_dtfl.get_curve_table_row_names(obj) or []))
            undone.append({**entry, "result": "curve-table-restored", "rows": _rows_now})
    elif op == "set_curve_atlas":
        # cross-module (curves_write_ext.py G2-A): restore prior texture_size + gradient_curves (set fires
        # PostEditChangeProperty -> atlas texture rebuild). FAITHFUL.
        ap = entry.get("asset_path")
        obj = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if obj is None or not isinstance(obj, unreal.CurveLinearColorAtlas):
            undone.append({**entry, "result": "curve-atlas-absent"})
        else:
            _pc = []
            for _p in (entry.get("prior_curves") or []):
                _po = unreal.EditorAssetLibrary.load_asset(_p)
                if _po is not None and isinstance(_po, unreal.CurveLinearColor):
                    _pc.append(_po)
            with unreal.ScopedEditorTransaction("MCP undo set_curve_atlas"):
                try: obj.set_editor_property("texture_size", int(entry.get("prior_width") or 64))
                except Exception: pass
                try: obj.set_editor_property("gradient_curves", _pc)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "curve-atlas-restored"})
    elif op == "set_input_action_properties":
        # cross-module (input_write.py G2-B): restore each captured prior InputAction property. FAITHFUL.
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_input_action_properties"):
                for _pn, _sv in (entry.get("prior") or {}).items(): _iw_set(ia, _pn, _iw_coerce_val(_sv))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ia-props-restored"})
    elif op == "add_input_action_tm":
        # cross-module (input_write.py G2-B): delete the trigger/modifier we appended. FAITHFUL. (add trigger+modifier)
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _field = entry.get("field"); _idx = entry.get("index")
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _arr = list(ia.get_editor_property(_field) or [])
            if _idx is not None and 0 <= _idx < len(_arr):
                with unreal.ScopedEditorTransaction("MCP undo add_input_action_tm"):
                    del _arr[_idx]; ia.set_editor_property(_field, _arr)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "ia-tm-removed"})
            else:
                undone.append({**entry, "result": "index-gone"})
    elif op == "remove_input_action_tm":
        # cross-module (input_write.py G2-B): rebuild + reinsert the removed trigger/modifier. FAITHFUL.
        ap = entry.get("asset_path"); ia = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _field = entry.get("field"); _idx = entry.get("index")
        if ia is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _arr = list(ia.get_editor_property(_field) or [])
            _inst = _iw_rebuild_tm(entry.get("item"), ia)
            with unreal.ScopedEditorTransaction("MCP undo remove_input_action_tm"):
                _arr.insert(min(int(_idx), len(_arr)), _inst); ia.set_editor_property(_field, _arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ia-tm-restored"})
    elif op == "imc_remove_mapping":
        # cross-module (input_write.py G2-B): rebuild + re-insert the removed IMC mapping. FAITHFUL.
        ap = entry.get("asset_path"); imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if imc is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _d, _arr = _iw_dkm(imc); _idx = entry.get("index")
            _m = _iw_rebuild_mapping(entry.get("mapping") or {}, imc)
            with unreal.ScopedEditorTransaction("MCP undo imc_remove_mapping"):
                _arr.insert(min(int(_idx), len(_arr)), _m); _iw_setdkm(imc, _d, _arr)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "imc-mapping-reinserted"})
    elif op == "imc_replace_mapping":
        # cross-module (input_write.py G2-B): restore the prior mapping snapshot at index. FAITHFUL.
        # (set_key_mapping + all 4 per-mapping trigger/modifier ops)
        ap = entry.get("asset_path"); imc = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if imc is None:
            undone.append({**entry, "result": "asset-absent"})
        else:
            _d, _arr = _iw_dkm(imc); _idx = entry.get("index")
            if _idx is not None and 0 <= _idx < len(_arr):
                with unreal.ScopedEditorTransaction("MCP undo imc_replace_mapping"):
                    _arr[int(_idx)] = _iw_rebuild_mapping(entry.get("prior") or {}, imc); _iw_setdkm(imc, _d, _arr)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "imc-mapping-restored"})
            else:
                undone.append({**entry, "result": "index-gone"})
    elif op == "gas_set_modifiers":
        # cross-module (gas_write.py G4): rebuild the effect's prior FGameplayModifierInfo array on the CDO
        # (import_text each) + recompile + save. FAITHFUL. (validated inverse pattern — no txn wrapper)
        ap = entry.get("asset_path"); _prior = entry.get("prior") or []
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _gcls = unreal.BlueprintEditorLibrary.generated_class(bp) if bp else None
        _cdo = unreal.get_default_object(_gcls) if _gcls else None
        if _cdo is None:
            undone.append({**entry, "result": "effect-absent"})
        else:
            _rebuilt = []
            for _txt in _prior:
                _m = unreal.GameplayModifierInfo()
                if _txt:
                    try: _m.import_text(_txt)
                    except Exception: pass
                _rebuilt.append(_m)
            try: _cdo.set_editor_property("modifiers", _rebuilt)
            except Exception: pass
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            _aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
            if _aes:
                try: _aes.close_all_editors_for_asset(bp)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "modifiers-restored", "count": len(_rebuilt)})
    elif op == "gas_set_ge_components":
        # cross-module (gas_write.py G4): rebuild the effect's prior ge_components array as inline CDO
        # subobjects (new_object + import_text) + recompile + save. FAITHFUL (reverses add AND remove).
        ap = entry.get("asset_path"); _prior = entry.get("prior") or []
        bp = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _gcls = unreal.BlueprintEditorLibrary.generated_class(bp) if bp else None
        _cdo = unreal.get_default_object(_gcls) if _gcls else None
        if _cdo is None:
            undone.append({**entry, "result": "effect-absent"})
        else:
            _rebuilt = []
            for _rec in _prior:
                _cp = _rec.get("class_path"); _txt = _rec.get("text")
                _cc = None
                try: _cc = unreal.load_class(None, _cp)
                except Exception: _cc = None
                if _cc is None and _cp:
                    _cn = _cp.split(".")[-1].split(":")[-1]
                    _cc = getattr(unreal, _cn, None)
                if _cc is not None:
                    _nm = getattr(_cc, "__name__", None) or _cc.get_name()
                    try:
                        _comp = unreal.new_object(_cc, _cdo, unreal.Name("MCP_GEC_R_" + _nm))
                    except Exception:
                        try: _comp = unreal.new_object(_cc, _cdo)
                        except Exception: _comp = None
                    if _comp is not None:
                        if _txt:
                            try: _comp.import_text(_txt)
                            except Exception: pass
                        _rebuilt.append(_comp)
            try: _cdo.set_editor_property("ge_components", _rebuilt)
            except Exception: pass
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            _aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
            if _aes:
                try: _aes.close_all_editors_for_asset(bp)
                except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "ge-components-restored", "count": len(_rebuilt)})
    elif op == "seq_rename_binding":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); tgt = None
            for b in (seq.get_bindings() or []):
                if str(b.get_name()) == bn:
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_rename_binding"):
                    tgt.set_display_name(entry.get("prior_display_name"))
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "binding-renamed-back"})
            seq = None
    elif op == "seq_set_display_rate":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_display_rate"):
                seq.set_display_rate(unreal.FrameRate(int(entry.get("prior_num")), int(entry.get("prior_den"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "display-rate-restored"})
            seq = None
    elif op == "seq_set_tick_resolution":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo seq_set_tick_resolution"):
                seq.set_tick_resolution(unreal.FrameRate(int(entry.get("prior_num")), int(entry.get("prior_den"))))
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "tick-resolution-restored"})
            seq = None
    elif op == "seq_set_evaluation_type":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            ev = getattr(unreal.MovieSceneEvaluationType, str(entry.get("prior_type")), None)
            with unreal.ScopedEditorTransaction("MCP undo seq_set_evaluation_type"):
                if ev is not None: seq.set_evaluation_type(ev)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "evaluation-type-restored"})
            seq = None
    elif op == "seq_add_property_track":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            bn = str(entry.get("binding_name")); tgt = None
            for b in (seq.get_bindings() or []):
                if str(b.get_name()) == bn:
                    tgt = b; break
            if tgt is None:
                undone.append({**entry, "result": "binding-absent"})
            else:
                cls_name = str(entry.get("track_class"))
                same = [t for t in (tgt.get_tracks() or []) if t.get_class().get_name() == cls_name]
                if len(same) > int(entry.get("prior_count", 0)):
                    with unreal.ScopedEditorTransaction("MCP undo seq_add_property_track"):
                        tgt.remove_track(same[-1])
                    undone.append({**entry, "result": "property-track-removed"})
                else:
                    undone.append({**entry, "result": "track-count-unchanged"})
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            seq = None
    elif op == "seq_add_subsequence":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            shot = [t for t in (seq.get_tracks() or []) if t.get_class().get_name() == "MovieSceneCinematicShotTrack"]
            if not shot:
                undone.append({**entry, "result": "shot-track-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_add_subsequence"):
                    if entry.get("added_track"):
                        seq.remove_track(shot[0])
                    else:
                        secs = list(shot[0].get_sections() or [])
                        si = int(entry.get("section_index", len(secs) - 1))
                        if 0 <= si < len(secs):
                            shot[0].remove_section(secs[si])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "subsequence-removed"})
            seq = None
    elif op == "seq_add_marked_frame":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            frame = int(entry.get("frame")); lbl = entry.get("label") or ""; idx = None
            for i, m in enumerate(seq.get_marked_frames() or []):
                fn = m.get_editor_property("frame_number")
                f = int(fn.value) if hasattr(fn, "value") else int(fn.frame_number.value)
                if f == frame and str(m.get_editor_property("label")) == lbl:
                    idx = i; break
            with unreal.ScopedEditorTransaction("MCP undo seq_add_marked_frame"):
                if idx is not None: seq.delete_marked_frame(idx)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("marked-frame-removed" if idx is not None else "marked-frame-absent")})
            seq = None
    elif op == "seq_remove_marked_frame":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            mk = entry.get("marked") or {}
            mf = unreal.MovieSceneMarkedFrame()
            mf.set_editor_property("frame_number", unreal.FrameNumber(int(mk.get("frame"))))
            if mk.get("label"): mf.set_editor_property("label", mk.get("label"))
            try: mf.set_editor_property("is_determinism_fence", bool(mk.get("is_determinism_fence")))
            except Exception: pass
            try: mf.set_editor_property("is_inclusive_time", bool(mk.get("is_inclusive_time")))
            except Exception: pass
            with unreal.ScopedEditorTransaction("MCP undo seq_remove_marked_frame"):
                seq.add_marked_frame(mf)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "marked-frame-readded"})
            seq = None
    elif op == "seq_add_folder":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            nm = str(entry.get("folder_name")); tgt = None
            for f in (seq.get_root_folders_in_sequence() or []):
                if str(f.get_folder_name()) == nm:
                    tgt = f; break
            with unreal.ScopedEditorTransaction("MCP undo seq_add_folder"):
                if tgt is not None: seq.remove_root_folder_from_sequence(tgt)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("folder-removed" if tgt is not None else "folder-absent")})
            seq = None
    elif op == "seq_add_to_folder":
        seq = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if seq is None:
            undone.append({**entry, "result": "sequence-absent"})
        else:
            nm = str(entry.get("folder_name")); fld = None
            for f in (seq.get_root_folders_in_sequence() or []):
                if str(f.get_folder_name()) == nm:
                    fld = f; break
            if fld is None:
                undone.append({**entry, "result": "folder-absent"})
            else:
                with unreal.ScopedEditorTransaction("MCP undo seq_add_to_folder"):
                    if entry.get("child_kind") == "binding":
                        bn = str(entry.get("binding_name")); b = None
                        for bb in (seq.get_bindings() or []):
                            if str(bb.get_name()) == bn or str(bb.get_display_name()) == bn:
                                b = bb; break
                        if b is not None: fld.remove_child_object_binding(b)
                    elif entry.get("child_kind") == "track":
                        tl = list(seq.get_tracks() or []); ti = int(entry.get("track_index"))
                        if 0 <= ti < len(tl): fld.remove_child_track(tl[ti])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "folder-child-removed"})
            seq = None
    elif op == "seq_set_playhead":
        L = unreal.LevelSequenceEditorBlueprintLibrary
        if L.get_current_level_sequence() is not None:
            L.set_current_time(int(entry.get("prior_frame")))
            undone.append({**entry, "result": "playhead-restored"})
        else:
            undone.append({**entry, "result": "no-sequence-open"})
    elif op == "seq_set_playback_state":
        L = unreal.LevelSequenceEditorBlueprintLibrary
        if L.get_current_level_sequence() is not None:
            if entry.get("prior_playing"): L.play()
            else: L.pause()
            undone.append({**entry, "result": "playback-state-restored"})
        else:
            undone.append({**entry, "result": "no-sequence-open"})
    elif op == "set_blend_space_axis":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            axis = int(entry.get("axis")); pr = entry.get("prior") or {}
            bp = list(bs.get_editor_property("blend_parameters") or [])
            if 0 <= axis < len(bp):
                p = bp[axis]
                with unreal.ScopedEditorTransaction("MCP undo set_blend_space_axis"):
                    p.set_editor_property("display_name", pr.get("display_name"))
                    p.set_editor_property("min", float(pr.get("min")))
                    p.set_editor_property("max", float(pr.get("max")))
                    p.set_editor_property("grid_num", int(pr.get("grid_num")))
                    bp[axis] = p
                    bs.set_editor_property("blend_parameters", bp)
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            undone.append({**entry, "result": "axis-restored"})
            bs = None
    elif op == "add_blend_space_sample":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            pc = int(entry.get("prior_count"))
            samples = list(bs.get_editor_property("sample_data") or [])
            if len(samples) > pc:
                with unreal.ScopedEditorTransaction("MCP undo add_blend_space_sample"):
                    bs.set_editor_property("sample_data", samples[:pc])
                try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
                except Exception: pass
            undone.append({**entry, "result": "sample-truncated"})
            bs = None
    elif op == "remove_blend_space_sample":
        bs = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if bs is None:
            undone.append({**entry, "result": "blendspace-absent"})
        else:
            sm = entry.get("sample") or {}; idx = int(entry.get("index"))
            an = unreal.EditorAssetLibrary.load_asset(sm.get("animation")) if sm.get("animation") else None
            s = unreal.BlendSample()
            s.set_editor_property("animation", an)
            s.set_editor_property("sample_value", unreal.Vector(float(sm.get("x") or 0.0), float(sm.get("y") or 0.0), 0.0))
            s.set_editor_property("rate_scale", float(sm.get("rate_scale") or 1.0))
            samples = list(bs.get_editor_property("sample_data") or [])
            if idx < 0 or idx > len(samples):
                idx = len(samples)
            samples.insert(idx, s)
            with unreal.ScopedEditorTransaction("MCP undo remove_blend_space_sample"):
                bs.set_editor_property("sample_data", samples)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "sample-reinserted"})
            bs = None
    elif op == "add_montage_slot":
        mont = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if mont is None:
            undone.append({**entry, "result": "montage-absent"})
        else:
            sn = str(entry.get("slot_name"))
            tracks = [t for t in (mont.get_editor_property("slot_anim_tracks") or []) if str(t.get_editor_property("slot_name")) != sn]
            with unreal.ScopedEditorTransaction("MCP undo add_montage_slot"):
                mont.set_editor_property("slot_anim_tracks", tracks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "slot-removed"})
            mont = None
    elif op == "add_montage_segment":
        mont = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        if mont is None:
            undone.append({**entry, "result": "montage-absent"})
        else:
            sn = str(entry.get("slot_name")); pc = int(entry.get("prior_segment_count"))
            tracks = list(mont.get_editor_property("slot_anim_tracks") or [])
            for i, t in enumerate(tracks):
                if str(t.get_editor_property("slot_name")) == sn:
                    atk = t.get_editor_property("anim_track")
                    segs = list(atk.get_editor_property("anim_segments") or [])
                    if len(segs) > pc:
                        atk.set_editor_property("anim_segments", segs[:pc])
                        tracks[i].set_editor_property("anim_track", atk)
                    break
            with unreal.ScopedEditorTransaction("MCP undo add_montage_segment"):
                mont.set_editor_property("slot_anim_tracks", tracks)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "segment-truncated"})
            mont = None
    elif op == "remove_anim_notify":
        anim = unreal.EditorAssetLibrary.load_asset(entry.get("asset_path"))
        tn = entry.get("track_name")
        if anim is None or tn is None:
            undone.append({**entry, "result": "anim-or-track-absent"})
        else:
            _AL = unreal.AnimationLibrary
            with unreal.ScopedEditorTransaction("MCP undo remove_anim_notify"):
                _AL.remove_animation_notify_events_by_track(anim, tn)
                for _pe in (entry.get("prior_events") or []):
                    _cp = _pe.get("class_path"); _cls = None
                    if _cp:
                        try: _cls = unreal.load_object(None, _cp)
                        except Exception: _cls = None
                    if _cls is None:
                        continue
                    if _pe.get("kind") == "state":
                        _AL.add_animation_notify_state_event(anim, tn, float(_pe.get("time") or 0.0), float(_pe.get("duration") or 0.0), _cls)
                    else:
                        _AL.add_animation_notify_event(anim, tn, float(_pe.get("time") or 0.0), _cls)
            try: unreal.EditorAssetLibrary.save_asset(entry.get("asset_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "notify-track-rebuilt"})
            anim = None
    elif op == "rename_gameplay_tag":
        # cross-module (gas_write.py C++#16): rename the tag back (self-inverse; INI redirector). FAITHFUL.
        m = getattr(unreal, "MCPReflectionLibrary", None)
        ot = entry.get("old_tag"); nt = entry.get("new_tag")
        if m is None or not hasattr(m, "rename_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            try: r = json.loads(m.rename_gameplay_tag(nt, ot))
            except Exception as e: r = {"error": str(e)}
            undone.append({**entry, "result": "tag-renamed-back", "ok": bool(isinstance(r, dict) and r.get("renamed"))})
    elif op == "eqs_add_option":
        # cross-module (eqs_write.py C++#15): remove the appended option. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.remove_env_query_option(o, int(entry.get("option_index"))))
            except Exception as e: r = {"error": str(e)}
            _eqs_save(ap)
            undone.append({**entry, "result": "option-removed", "ok": bool(isinstance(r, dict) and r.get("removed"))})
    elif op == "eqs_add_test":
        # cross-module (eqs_write.py C++#15): remove the appended test. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.remove_env_query_test(o, int(entry.get("option_index")), int(entry.get("test_index"))))
            except Exception as e: r = {"error": str(e)}
            _eqs_save(ap)
            undone.append({**entry, "result": "test-removed", "ok": bool(isinstance(r, dict) and r.get("removed"))})
    elif op == "eqs_set_node_property":
        # cross-module (eqs_write.py C++#15): re-set the captured prior value. FAITHFUL.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        if o is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            r = _eqs_setp(o, entry.get("node_locator"), entry.get("prop_name"), entry.get("prev"))
            _eqs_save(ap)
            undone.append({**entry, "result": "prop-restored", "ok": bool(isinstance(r, dict) and r.get("set"))})
    elif op == "eqs_readd_test":
        # cross-module (eqs_write.py C++#15): re-append the removed test + replay config. BEST-EFFORT.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            oi = int(entry.get("option_index"))
            try: r = json.loads(m.add_env_query_test(o, oi, _eqs_norm(entry.get("test_class"))))
            except Exception as e: r = {"error": str(e)}
            tj = r.get("test_index") if isinstance(r, dict) else None
            okc = 0
            if tj is not None:
                for pn, pv in (entry.get("config") or {}).items():
                    rr = _eqs_setp(o, "option:%d/test:%d" % (oi, int(tj)), pn, pv)
                    if isinstance(rr, dict) and rr.get("set"): okc += 1
            _eqs_save(ap)
            undone.append({**entry, "result": "test-replayed", "test_index": tj, "props_ok": okc})
    elif op == "eqs_readd_option":
        # cross-module (eqs_write.py C++#15): re-append the removed option + generator/tests. BEST-EFFORT.
        ap = entry.get("asset_path")
        o = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        m = getattr(unreal, "MCPReflectionLibrary", None)
        if o is None or m is None:
            undone.append({**entry, "result": "query-absent"})
        else:
            try: r = json.loads(m.add_env_query_option(o, _eqs_norm(entry.get("generator_class"))))
            except Exception as e: r = {"error": str(e)}
            oi = r.get("option_index") if isinstance(r, dict) else None
            gok = 0; tcount = 0
            if oi is not None:
                oi = int(oi)
                for pn, pv in (entry.get("generator_config") or {}).items():
                    rr = _eqs_setp(o, "option:%d/generator" % oi, pn, pv)
                    if isinstance(rr, dict) and rr.get("set"): gok += 1
                for tc in (entry.get("tests") or []):
                    try: tr = json.loads(m.add_env_query_test(o, oi, _eqs_norm(tc.get("test_class"))))
                    except Exception: tr = None
                    tj = tr.get("test_index") if isinstance(tr, dict) else None
                    if tj is not None:
                        tcount += 1
                        for pn, pv in (tc.get("config") or {}).items():
                            _eqs_setp(o, "option:%d/test:%d" % (oi, int(tj)), pn, pv)
            _eqs_save(ap)
            undone.append({**entry, "result": "option-replayed", "option_index": oi, "gen_props_ok": gok, "tests_replayed": tcount})
    elif op == "bt_remove_node":
        # cross-module (bt_write2.py): rebuild the snapshotted child slot at index.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        idx = entry.get("index"); snap = entry.get("snapshot")
        if bt is None or parent is None or idx is None or snap is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            newch = _make_child(bt, snap)
            if idx > len(kids):
                idx = len(kids)
            kids.insert(idx, newch)
            parent.set_editor_property("children", kids)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-node-restored"})
    elif op == "bt_set_node_prop":
        # cross-module (bt_write2.py): restore prior value on the node at node_path.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        node = None
        if bt is not None:
            npth = str(entry.get("node_path") or "").strip()
            if npth in ("", "root"):
                node = bt.get_editor_property("root_node")
            else:
                toks = npth.split(".")
                par = _bt_resolve(bt, ".".join(toks[:-1]) if len(toks) > 1 else "root")
                if par is not None:
                    kk = list(par.get_editor_property("children") or [])
                    ii = _try(lambda: int(toks[-1]))
                    if ii is not None and 0 <= ii < len(kk):
                        node = kk[ii].get_editor_property("child_composite") or kk[ii].get_editor_property("child_task")
        if node is None:
            undone.append({**entry, "result": "bt-node-absent"})
        else:
            prop = entry.get("property"); prior = entry.get("prior")
            cur = _try(lambda: node.get_editor_property(prop))
            with unreal.ScopedEditorTransaction("MCP undo bt_set_node_prop"):
                node.set_editor_property(prop, _coerce(cur, prior))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bt-prop-restored"})
    elif op == "bt_add_comp_decorator":
        # cross-module (bt_write2.py): drop the decorators we appended + restore prior decorator_ops.
        ap = entry.get("asset_path")
        bt = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        parent = _bt_resolve(bt, entry.get("parent_path")) if bt is not None else None
        ci = entry.get("child_index"); base = entry.get("prior_decorator_count"); pops = entry.get("prior_ops") or []
        if bt is None or parent is None or ci is None or base is None:
            undone.append({**entry, "result": "bt-or-parent-absent"})
        else:
            kids = list(parent.get_editor_property("children") or [])
            if 0 <= ci < len(kids):
                ch = kids[ci]
                decos = list(ch.get_editor_property("decorators") or [])
                ch.set_editor_property("decorators", decos[:base])
                ops = []
                for ex in pops:
                    lo = unreal.BTDecoratorLogic(); lo.import_text(ex); ops.append(lo)
                ch.set_editor_property("decorator_ops", ops)
                kids[ci] = ch
                parent.set_editor_property("children", kids)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bt-comp-decorator-reverted"})
            else:
                undone.append({**entry, "result": "bt-child-index-stale"})
    elif op == "bb_remove_key":
        # cross-module (bt_write2.py): re-insert the removed BlackboardData key at its index.
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        snap = entry.get("snapshot")
        if bb is None or snap is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            ktcls = _keytype_cls(snap.get("type"))
            keys = list(bb.get_editor_property("keys") or [])
            entry_obj = unreal.BlackboardEntry()
            entry_obj.set_editor_property("entry_name", unreal.Name(str(snap.get("name"))))
            if ktcls is not None:
                entry_obj.set_editor_property("key_type", unreal.new_object(ktcls, bb))
            if snap.get("category"):
                try: entry_obj.set_editor_property("entry_category", unreal.Name(str(snap.get("category"))))
                except Exception: pass
            if snap.get("description"):
                try: entry_obj.set_editor_property("entry_description", str(snap.get("description")))
                except Exception: pass
            i = snap.get("index", len(keys))
            if i > len(keys):
                i = len(keys)
            with unreal.ScopedEditorTransaction("MCP undo bb_remove_key"):
                keys.insert(i, entry_obj)
                bb.set_editor_property("keys", keys)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "bb-key-restored"})
            keys = None; entry_obj = None; bb = None
    elif op == "bb_set_key":
        # cross-module (bt_write2.py): restore prior name + type on the key at index.
        ap = entry.get("asset_path")
        bb = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        idx = entry.get("index")
        if bb is None or idx is None:
            undone.append({**entry, "result": "blackboard-absent"})
        else:
            keys = list(bb.get_editor_property("keys") or [])
            if 0 <= idx < len(keys):
                e = keys[idx]
                ktcls = _keytype_cls(entry.get("prior_type"))
                with unreal.ScopedEditorTransaction("MCP undo bb_set_key"):
                    e.set_editor_property("entry_name", unreal.Name(str(entry.get("prior_name"))))
                    if ktcls is not None:
                        e.set_editor_property("key_type", unreal.new_object(ktcls, bb))
                    keys[idx] = e
                    bb.set_editor_property("keys", keys)
                try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
                except Exception: pass
                undone.append({**entry, "result": "bb-key-reverted"})
            else:
                undone.append({**entry, "result": "bb-index-stale"})
            keys = None; bb = None
    elif op == "add_seq_master_track":
        # cross-module (sequencer_write_ext.py): remove the master track we appended (if the exact-type
        # count grew). FAITHFUL — removes only our track (+ its sections).
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        if seq is None or _cls is None:
            undone.append({**entry, "result": "sequence-or-class-absent"})
        else:
            _ts = list(seq.find_tracks_by_exact_type(_cls) or [])
            if len(_ts) > int(entry.get("prior_exact_count") or 0):
                with unreal.ScopedEditorTransaction("MCP undo add_seq_master_track"):
                    seq.remove_track(_ts[-1])
                undone.append({**entry, "result": "seq-master-track-removed"})
            else:
                undone.append({**entry, "result": "seq-master-track-count-stale"})
            seq = None
    elif op == "add_seq_binding_track":
        # cross-module (sequencer_write_ext.py): remove the binding track we appended. FAITHFUL.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        _bn = str(entry.get("binding_name"))
        _b = None
        if seq is not None:
            for b in (seq.get_bindings() or []):
                _dn = None
                try: _dn = str(b.get_display_name())
                except Exception: _dn = None
                if _dn == _bn or (hasattr(b, "get_name") and str(b.get_name()) == _bn):
                    _b = b; break
        if seq is None or _cls is None or _b is None:
            undone.append({**entry, "result": "sequence-binding-or-class-absent"})
        else:
            _ts = list(_b.find_tracks_by_exact_type(_cls) or [])
            if len(_ts) > int(entry.get("prior_exact_count") or 0):
                with unreal.ScopedEditorTransaction("MCP undo add_seq_binding_track"):
                    _b.remove_track(_ts[-1])
                undone.append({**entry, "result": "seq-binding-track-removed"})
            else:
                undone.append({**entry, "result": "seq-binding-track-count-stale"})
            seq = None
    elif op == "add_seq_track_section":
        # cross-module (sequencer_write_ext.py): remove the section we appended to a track. FAITHFUL.
        ap = entry.get("asset_path")
        seq = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _cls = getattr(unreal, str(entry.get("track_class") or ""), None)
        _cont = None
        if seq is not None:
            if str(entry.get("scope")) == "binding":
                _bn = str(entry.get("binding_name"))
                for b in (seq.get_bindings() or []):
                    _dn = None
                    try: _dn = str(b.get_display_name())
                    except Exception: _dn = None
                    if _dn == _bn or (hasattr(b, "get_name") and str(b.get_name()) == _bn):
                        _cont = b; break
            else:
                _cont = seq
        if seq is None or _cls is None or _cont is None:
            undone.append({**entry, "result": "sequence-track-or-binding-absent"})
        else:
            _ts = list(_cont.find_tracks_by_exact_type(_cls) or [])
            _ti = int(entry.get("track_index") or 0)
            if _ti < len(_ts):
                _secs = list(_ts[_ti].get_sections() or [])
                if len(_secs) > int(entry.get("prior_section_count") or 0):
                    with unreal.ScopedEditorTransaction("MCP undo add_seq_track_section"):
                        _ts[_ti].remove_section(_secs[-1])
                    undone.append({**entry, "result": "seq-section-removed"})
                else:
                    undone.append({**entry, "result": "seq-section-count-stale"})
            else:
                undone.append({**entry, "result": "seq-track-index-stale"})
            seq = None
    elif op == "add_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): remove the mesh socket we added. FAITHFUL (created new).
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = entry.get("socket_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or sn is None:
            undone.append({**entry, "result": "mesh-or-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_skeletal_mesh_socket"):
                mesh.remove_socket(unreal.Name(str(sn)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-removed"})
    elif op == "remove_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): re-create the removed mesh socket from captured state
        # (SocketName/BoneName are read-only → build, add, rename the engine auto-name). FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = entry.get("socket_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or sn is None:
            undone.append({**entry, "result": "mesh-or-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo remove_skeletal_mesh_socket"):
                _s = unreal.new_object(unreal.SkeletalMeshSocket, mesh)
                _s.set_socket_parent(mesh, unreal.Name(str(entry.get("bone") or "")))
                _lo = entry.get("location"); _ro = entry.get("rotation"); _sc = entry.get("scale")
                if _lo is not None:
                    _s.set_editor_property("relative_location", unreal.Vector(float(_lo[0]), float(_lo[1]), float(_lo[2])))
                if _ro is not None:
                    _s.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(_ro[0]), yaw=float(_ro[1]), roll=float(_ro[2])))
                if _sc is not None:
                    _s.set_editor_property("relative_scale", unreal.Vector(float(_sc[0]), float(_sc[1]), float(_sc[2])))
                if entry.get("force_always_animated") is not None:
                    try: _s.set_editor_property("force_always_animated", bool(entry.get("force_always_animated")))
                    except Exception: pass
                mesh.add_socket(_s, False)
                _auto = str(_s.get_editor_property("socket_name"))
                if _auto != str(sn):
                    mesh.rename_socket(unreal.Name(_auto), unreal.Name(str(sn)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-restored"})
    elif op == "set_skeletal_mesh_socket_transform":
        # cross-module (skeleton_write.py): restore the socket's captured prior relative transform. FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        sn = str(entry.get("socket_name") or "")
        pr = entry.get("prior") or {}
        _sk = None
        if mesh is not None and isinstance(mesh, unreal.SkeletalMesh):
            for _i in range(mesh.num_sockets()):
                _c = mesh.get_socket_by_index(_i)
                if _c and str(_c.get_editor_property("socket_name")) == sn and isinstance(_c.get_outer(), unreal.SkeletalMesh):
                    _sk = _c; break
        if _sk is None:
            undone.append({**entry, "result": "mesh-socket-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_skeletal_mesh_socket_transform"):
                _lo = pr.get("location"); _ro = pr.get("rotation"); _sc = pr.get("scale")
                if _lo is not None:
                    _sk.set_editor_property("relative_location", unreal.Vector(float(_lo[0]), float(_lo[1]), float(_lo[2])))
                if _ro is not None:
                    _sk.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(_ro[0]), yaw=float(_ro[1]), roll=float(_ro[2])))
                if _sc is not None:
                    _sk.set_editor_property("relative_scale", unreal.Vector(float(_sc[0]), float(_sc[1]), float(_sc[2])))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-xf-restored"})
    elif op == "rename_skeletal_mesh_socket":
        # cross-module (skeleton_write.py): rename the socket back new_name→old_name. FAITHFUL.
        ap = entry.get("mesh_path")
        mesh = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        on = entry.get("old_name"); nn = entry.get("new_name")
        if mesh is None or not isinstance(mesh, unreal.SkeletalMesh) or on is None or nn is None:
            undone.append({**entry, "result": "mesh-or-names-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo rename_skeletal_mesh_socket"):
                mesh.rename_socket(unreal.Name(str(nn)), unreal.Name(str(on)))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skm-socket-renamed-back"})
    elif op == "set_texture_property":
        # cross-module (texture_write.py): restore the full prior texture-settings snapshot. Two passes
        # converge any single-step constraint cascade (e.g. compression forcing srgb off). FAITHFUL.
        ap = entry.get("asset_path")
        tex = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        prior = entry.get("prior") or {}
        if tex is None or not isinstance(tex, unreal.Texture2D) or not prior:
            undone.append({**entry, "result": "texture-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_texture_property"):
                for _pass in range(2):
                    for fname, meta in prior.items():
                        k = meta.get("kind"); v = meta.get("value")
                        if k == "enum":
                            _cls = getattr(unreal, meta.get("enum_type") or "", None)
                            _nv = getattr(_cls, str(v), None) if _cls is not None else None
                            if _nv is not None:
                                try: tex.set_editor_property(fname, _nv)
                                except Exception: pass
                        elif k == "bool":
                            try: tex.set_editor_property(fname, bool(v))
                            except Exception: pass
                        elif k == "int":
                            try: tex.set_editor_property(fname, int(v))
                            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "texture-settings-restored"})
    elif op == "add_material_expression":
        # cross-module (material_graph_write.py): delete the expression node we created (also disconnects). FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        e = _mg_find(mat, entry.get("expr_name"))
        if mat is None or e is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_material_expression"):
                _MG.delete_material_expression(mat, e)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-expr-deleted"})
    elif op == "set_material_expression_property":
        # cross-module (material_graph_write.py): restore the node property's captured prior value. FAITHFUL when restorable.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        e = _mg_find(mat, entry.get("expr_name"))
        if mat is None or e is None or not entry.get("restorable", True):
            undone.append({**entry, "result": "material-expr-absent-or-unrestorable"})
        else:
            pn = entry.get("property_name")
            with unreal.ScopedEditorTransaction("MCP undo set_material_expression_property"):
                try:
                    _cur = e.get_editor_property(pn)
                    _v = _mg_coerce(entry.get("prior_value"))
                    if isinstance(_cur, unreal.LinearColor) and isinstance(_v, (list, tuple)) and len(_v) >= 3:
                        _v = unreal.LinearColor(float(_v[0]), float(_v[1]), float(_v[2]), float(_v[3]) if len(_v) > 3 else 1.0)
                    elif isinstance(_cur, (int, float)) and not isinstance(_cur, bool) and isinstance(_v, (int, float, str)):
                        _v = float(_v) if isinstance(_cur, float) else int(_v)
                    e.set_editor_property(pn, _v)
                except Exception: pass
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-expr-prop-restored"})
    elif op == "connect_material_expression":
        # cross-module (material_graph_write.py): reconnect the prior source, or clear the pin if none. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        to = _mg_find(mat, entry.get("to_expr"))
        if mat is None or to is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            _ti = entry.get("to_input") or ""
            with unreal.ScopedEditorTransaction("MCP undo connect_material_expression"):
                if entry.get("had_prior"):
                    _src = _mg_find(mat, entry.get("prior_src_name"))
                    if _src is not None:
                        _MG.connect_material_expressions(_src, entry.get("prior_out_name") or "", to, _ti)
                else:
                    _MG.disconnect_material_expressions(to, _ti)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-conn-reverted"})
    elif op == "disconnect_material_expression":
        # cross-module (material_graph_write.py): reconnect the captured prior source. FAITHFUL (only ledgered when connected).
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        to = _mg_find(mat, entry.get("to_expr"))
        _src = _mg_find(mat, entry.get("prior_src_name")) if mat is not None else None
        if mat is None or to is None or _src is None or _MG is None:
            undone.append({**entry, "result": "material-or-expr-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo disconnect_material_expression"):
                _MG.connect_material_expressions(_src, entry.get("prior_out_name") or "", to, entry.get("to_input") or "")
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-conn-restored"})
    elif op == "connect_material_property":
        # cross-module (material_graph_write.py): reconnect the prior source into the property, or clear it. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _p = _mg_prop(entry.get("material_property"))
        if mat is None or _p is None or _MG is None:
            undone.append({**entry, "result": "material-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo connect_material_property"):
                if entry.get("had_prior"):
                    _src = _mg_find(mat, entry.get("prior_src_name"))
                    if _src is not None:
                        _MG.connect_material_property(_src, entry.get("prior_out_name") or "", _p)
                else:
                    _MG.disconnect_material_property(mat, _p)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-prop-conn-reverted"})
    elif op == "disconnect_material_property":
        # cross-module (material_graph_write.py): reconnect the captured prior source into the property. FAITHFUL.
        ap = entry.get("asset_path"); mat = _mg_mat(ap)
        _p = _mg_prop(entry.get("material_property"))
        _src = _mg_find(mat, entry.get("prior_src_name")) if mat is not None else None
        if mat is None or _p is None or _src is None or _MG is None:
            undone.append({**entry, "result": "material-or-prop-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo disconnect_material_property"):
                _MG.connect_material_property(_src, entry.get("prior_out_name") or "", _p)
            _mg_finish(mat, ap); mat = None
            undone.append({**entry, "result": "material-prop-conn-restored"})
    elif op == "add_montage_section":
        # cross-module (anim_write.py, C++ #13): remove the montage section we added. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "remove_montage_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.remove_montage_section(_m, entry.get("section_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-removed"})
    elif op == "remove_montage_section":
        # cross-module (anim_write.py, C++ #13): re-add the section at its prior time + next-link. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "add_montage_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.add_montage_section(_m, entry.get("section_name"), float(entry.get("prior_start_time") or 0.0))
            _nx = entry.get("next_section_name")
            if _nx and str(_nx) not in ("None", "") and hasattr(_rl, "set_montage_section_next_section"):
                _rl.set_montage_section_next_section(_m, entry.get("section_name"), str(_nx))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-restored"})
    elif op == "set_montage_section_time":
        # cross-module (anim_write.py, C++ #13): restore the section's prior start time. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        pt = entry.get("prior_start_time")
        if _m is None or _rl is None or pt is None or not hasattr(_rl, "set_montage_section_time"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _rl.set_montage_section_time(_m, entry.get("section_name"), float(pt))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-section-time-restored"})
    elif op == "set_montage_section_next_section":
        # cross-module (anim_write.py, C++ #13): restore the section's prior next-link. FAITHFUL.
        ap = entry.get("asset_path")
        _m = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _m is None or _rl is None or not hasattr(_rl, "set_montage_section_next_section"):
            undone.append({**entry, "result": "montage-or-handler-absent"})
        else:
            _pn = entry.get("prior_next_section")
            _rl.set_montage_section_next_section(_m, entry.get("section_name"),
                "" if (_pn is None or str(_pn) == "None") else str(_pn))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "montage-next-restored"})
    elif op == "add_skeleton_socket":
        # cross-module (skeleton_write.py, C++ #13): remove the skeleton socket we added. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "remove_skeleton_socket"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.remove_skeleton_socket(_s, entry.get("socket_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skeleton-socket-removed"})
    elif op == "remove_skeleton_socket":
        # cross-module (skeleton_write.py, C++ #13): re-add the socket with its captured bone + transform. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "add_skeleton_socket"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _lo = entry.get("location") or [0, 0, 0]
            _ro = entry.get("rotation") or [0, 0, 0]
            _sc = entry.get("scale") or [1, 1, 1]
            _rl.add_skeleton_socket(_s, entry.get("socket_name"), entry.get("bone") or "",
                float(_lo[0]), float(_lo[1]), float(_lo[2]),
                float(_ro[0]), float(_ro[1]), float(_ro[2]),
                float(_sc[0]), float(_sc[1]), float(_sc[2]))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "skeleton-socket-restored"})
    elif op == "add_virtual_bone":
        # cross-module (skeleton_write.py, C++ #13): remove the virtual bone we added. FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "remove_virtual_bone"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.remove_virtual_bone(_s, entry.get("virtual_bone_name"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "virtual-bone-removed"})
    elif op == "remove_virtual_bone":
        # cross-module (skeleton_write.py, C++ #13): re-add the virtual bone (engine re-assigns same name). FAITHFUL.
        ap = entry.get("skeleton_path")
        _s = unreal.EditorAssetLibrary.load_asset(ap) if ap else None
        _rl = getattr(unreal, "MCPReflectionLibrary", None)
        if _s is None or _rl is None or not hasattr(_rl, "add_virtual_bone"):
            undone.append({**entry, "result": "skeleton-or-handler-absent"})
        else:
            _rl.add_virtual_bone(_s, entry.get("source"), entry.get("target"))
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "virtual-bone-restored"})
    elif op == "add_rig_vm_node":
        # cross-module (controlrig_graph_write.py): remove the RigVM node we added (unit/comment). FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_node"):
                _c.remove_node_by_name(unreal.Name(str(entry.get("node_name"))), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-node-removed"})
    elif op == "set_rig_vm_pin_default":
        # cross-module (controlrig_graph_write.py): restore the pin's captured prior default. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        pv = entry.get("prior_value")
        if _c is None or pv is None:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_vm_pin_default"):
                _c.set_pin_default_value(entry.get("pin_path"), pv, True, True, False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-pin-default-restored"})
    elif op == "set_rig_vm_node_position":
        # cross-module (controlrig_graph_write.py): restore the node's captured prior position. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        pp = entry.get("prior_pos")
        if _c is None or not pp:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo set_rig_vm_node_position"):
                _c.set_node_position_by_name(unreal.Name(str(entry.get("node_name"))), unreal.Vector2D(float(pp[0]), float(pp[1])), True, False, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-node-position-restored"})
    elif op == "add_rig_vm_link":
        # cross-module (controlrig_graph_write.py): break the link + reconnect the input's prior sources. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_link"):
                _c.break_link(entry.get("output_pin"), entry.get("input_pin"), True, False)
                for _s in (entry.get("prior_sources") or []):
                    _c.add_link(_s, entry.get("input_pin"), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-link-reverted"})
    elif op == "break_rig_vm_link":
        # cross-module (controlrig_graph_write.py): re-add the link we broke. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo break_rig_vm_link"):
                _c.add_link(entry.get("output_pin"), entry.get("input_pin"), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-link-restored"})
    elif op == "remove_rig_vm_node":
        # cross-module (controlrig_graph_write.py): re-add the removed node (kind-aware) + its pin defaults. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        _pos = entry.get("position")
        if _c is None or not _pos:
            undone.append({**entry, "result": "cr-graph-or-prior-absent"})
        else:
            _v2 = unreal.Vector2D(float(_pos[0]), float(_pos[1]))
            _kind = entry.get("node_kind"); _nm = str(entry.get("node_name"))
            with unreal.ScopedEditorTransaction("MCP undo remove_rig_vm_node"):
                if _kind == "unit":
                    _c.add_unit_node_from_struct_path(entry.get("struct_path"), entry.get("method") or "Execute", _v2, _nm, True, False)
                elif _kind == "comment":
                    _cm = entry.get("comment") or {}
                    _sz = _cm.get("size") or [100, 100]; _cl = _cm.get("color") or [1, 1, 1, 1]
                    _c.add_comment_node(_cm.get("text") or "", _v2, unreal.Vector2D(float(_sz[0]), float(_sz[1])),
                        unreal.LinearColor(float(_cl[0]), float(_cl[1]), float(_cl[2]), float(_cl[3])), _nm, True, False)
                for _pp, _vv in (entry.get("pin_defaults") or {}).items():
                    _c.set_pin_default_value(_pp, _vv, True, True, False, False, True)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": ("rigvm-node-restored" if _kind in ("unit", "comment") else "rigvm-node-restore-unsupported")})
    elif op == "add_rig_vm_local_variable":
        # cross-module (controlrig_graph_write.py): remove the graph-scoped local variable we declared.
        # Local variables live on FUNCTION/COLLAPSE graphs -> _crg_ctrl (R11 fix) resolves those. FAITHFUL.
        ap = entry.get("asset_path"); _c = _crg_ctrl(ap, entry.get("graph_name"))
        if _c is None:
            undone.append({**entry, "result": "cr-graph-or-controller-absent"})
        else:
            with unreal.ScopedEditorTransaction("MCP undo add_rig_vm_local_variable"):
                _c.remove_local_variable(unreal.Name(str(entry.get("var_name"))), True, False)
            try: unreal.EditorAssetLibrary.save_asset(ap, only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "rigvm-local-variable-removed"})
    elif op == "add_event_dispatcher":
        # cross-module (blueprints_write.py): remove the event dispatcher we added (by name). FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        if bp is None:
            undone.append({**entry, "result": "blueprint-absent"})
        else:
            unreal.BlueprintEditorLibrary.remove_event_dispatcher(bp, unreal.Name(entry.get("name")))
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            undone.append({**entry, "result": "dispatcher-removed"})
    elif op == "remove_component":
        # cross-module (editor_actor_components.py): re-add the instanced component we removed
        target = _find_by_name(entry.get("actor_name"))
        if target is None:
            undone.append({**entry, "result": "actor-absent"})
        else:
            sods = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            L = unreal.SubobjectDataBlueprintFunctionLibrary
            handles = sods.k2_gather_subobject_data_for_instance(target) or []
            root = handles[0] if handles else None
            parent_h = root
            ap = entry.get("attach_parent")
            if ap:
                for h in handles:
                    d = sods.k2_find_subobject_data_from_handle(h)
                    o = L.get_associated_object(d) if d else None
                    if o is not None and o.get_name() == ap:
                        parent_h = h; break
            cls = getattr(unreal, entry.get("component_class", ""), None)
            if cls is None or parent_h is None:
                undone.append({**entry, "result": "class-or-parent-unresolved"})
            else:
                p = unreal.AddNewSubobjectParams()
                p.set_editor_property("parent_handle", parent_h)
                p.set_editor_property("new_class", cls)
                p.set_editor_property("blueprint_context", None)
                p.set_editor_property("conform_transform_to_parent", True)
                with unreal.ScopedEditorTransaction("MCP undo remove_component"):
                    nh, fail = sods.add_new_subobject(p)
                    comp = None
                    if not str(fail):
                        d2 = sods.k2_find_subobject_data_from_handle(nh)
                        comp = L.get_associated_object(d2) if d2 else None
                        want = entry.get("component_name")
                        if comp is not None and want and comp.get_name() != want:
                            sods.rename_subobject(nh, want)
                        tx = entry.get("transform")
                        if comp is not None and tx and isinstance(comp, unreal.SceneComponent):
                            if tx.get("loc") is not None:
                                v = tx["loc"]; comp.set_editor_property("relative_location", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                            if tx.get("rot") is not None:
                                v = tx["rot"]; comp.set_editor_property("relative_rotation", unreal.Rotator(pitch=float(v[0]), yaw=float(v[1]), roll=float(v[2])))
                            if tx.get("scale") is not None:
                                v = tx["scale"]; comp.set_editor_property("relative_scale3d", unreal.Vector(float(v[0]), float(v[1]), float(v[2])))
                        if comp is not None:
                            import warnings as _wn2; _wn2.simplefilter("ignore")
                            for pn, pv in (entry.get("props") or {}).items():
                                try:
                                    cur = comp.get_editor_property(pn); cj, _rr = _settable(cur)
                                    if cj != pv:
                                        comp.set_editor_property(pn, _coerce(cur, pv))
                                except Exception:
                                    pass
                undone.append({**entry, "result": ("re-added:" + (comp.get_name() if comp else "none")) if comp is not None else ("add-failed:" + str(fail))})
    elif op == "add_gameplay_tag":
        # cross-module (gameplay_tags_write.py): remove the tag we added (INI back to net-zero). FAITHFUL.
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "remove_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            mrl.remove_gameplay_tag(entry.get("tag"))
            undone.append({**entry, "result": "tag-removed"})
    elif op == "remove_gameplay_tag":
        # cross-module (gameplay_tags_write.py): re-add the tag we removed (best-effort; DevComment only
        # restored if it was captured on removal).
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if mrl is None or not hasattr(mrl, "add_gameplay_tag"):
            undone.append({**entry, "result": "handler-absent"})
        else:
            mrl.add_gameplay_tag(entry.get("tag"), entry.get("comment") or "")
            undone.append({**entry, "result": "tag-re-added"})
    elif op == "add_blueprint_variable":
        # cross-module (blueprints_write.py): remove the variable we added (by name). FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if bp is None or mrl is None or not hasattr(mrl, "remove_blueprint_variable"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        else:
            mrl.remove_blueprint_variable(bp, entry.get("var_name"))
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            undone.append({**entry, "result": "variable-removed"})
    elif op == "remove_blueprint_variable":
        # cross-module (blueprints_write.py): re-add the removed variable (same name + type; new default).
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        vt = entry.get("var_type")
        if bp is None or mrl is None or not hasattr(mrl, "add_blueprint_variable"):
            undone.append({**entry, "result": "blueprint-or-handler-absent"})
        elif not vt:
            undone.append({**entry, "result": "cannot-restore (type not captured)"})
        else:
            mrl.add_blueprint_variable(bp, entry.get("var_name"), vt)
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            undone.append({**entry, "result": "variable-re-added"})
    elif op == "add_widget":
        # cross-module (widgets_write.py): remove the widget we added (by name). FAITHFUL.
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        if wb is None or mrl is None or not hasattr(mrl, "remove_widget_from_blueprint"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        else:
            mrl.remove_widget_from_blueprint(wb, entry.get("name"))
            try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("wbp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "widget-removed"})
    elif op == "remove_widget":
        # cross-module (widgets_write.py): re-add the removed widget (same class/name/parent; best-effort
        # -- child widgets of a removed panel are NOT restored).
        wb = unreal.EditorAssetLibrary.load_asset(entry.get("wbp_path")) if entry.get("wbp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        cp = entry.get("widget_class_path")
        if wb is None or mrl is None or not hasattr(mrl, "add_widget_to_blueprint"):
            undone.append({**entry, "result": "widgetbp-or-handler-absent"})
        elif not cp:
            undone.append({**entry, "result": "cannot-restore (class not captured)"})
        else:
            mrl.add_widget_to_blueprint(wb, cp, entry.get("name"), entry.get("parent_name") or "")
            try: unreal.BlueprintEditorLibrary.compile_blueprint(wb)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("wbp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "widget-re-added"})
    elif op == "add_event_override":
        # cross-module (blueprints_write.py): remove the exact event node we added, by guid. FAITHFUL.
        bp = unreal.EditorAssetLibrary.load_asset(entry.get("bp_path")) if entry.get("bp_path") else None
        mrl = getattr(unreal, "MCPReflectionLibrary", None)
        ng = entry.get("node_guid")
        if bp is None or mrl is None or not hasattr(mrl, "remove_event_node_by_guid") or not ng:
            undone.append({**entry, "result": "blueprint-handler-or-guid-absent"})
        else:
            mrl.remove_event_node_by_guid(bp, ng)
            try: unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            except Exception: pass
            try: unreal.EditorAssetLibrary.save_asset(entry.get("bp_path"), only_if_is_dirty=False)
            except Exception: pass
            undone.append({**entry, "result": "event-node-removed"})
    else:
        led.append(entry)
        undone.append({"op": op, "result": "no-inverse-known; stopped"})
        break
print("@@UMCP@@" + json.dumps({"status": "success", "undone": undone, "ledger_depth": len(led)}))
'''

    _CREATE_ASSET_SWEEP_BODY = '''
import unreal, json
targets = PARAMS.get("targets") or []
out = []
for t in targets:
    ap = t.get("asset_path"); pkg = t.get("package_path"); cd = t.get("created_dir")
    unreal.SystemLibrary.collect_garbage()
    ok = False
    if ap and unreal.EditorAssetLibrary.does_asset_exist(ap):
        ok = unreal.EditorAssetLibrary.delete_asset(ap)
        if not ok and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
            try:
                unreal.EditorAssetLibrary.delete_directory(pkg)
                ok = not unreal.EditorAssetLibrary.does_asset_exist(ap)
            except Exception:
                pass
    elif ap:
        ok = True
    if ok and cd and pkg and unreal.EditorAssetLibrary.does_directory_exist(pkg):
        try:
            if not (unreal.EditorAssetLibrary.list_assets(pkg, recursive=True) or []):
                unreal.EditorAssetLibrary.delete_directory(pkg)
        except Exception:
            pass
    out.append({"asset_path": ap, "swept": ok})
print("@@UMCP@@" + json.dumps({"status": "success", "swept": out}))
'''

    @mcp.tool()
    def undo(ctx, count: int = 1) -> str:
        """Revert the most recent edits WE made, newest first (agent-scoped).

        Pops up to `count` entries off the agent ledger and applies their inverse
        (e.g. a spawn_actor is undone by deleting that exact actor). Only our own
        recorded edits are touched; your manual editor changes are never affected.
        Stops early if it hits an op with no known inverse."""
        try:
            res = _exec(_UNDO_BODY, {"count": count})
            # Follow-up sweep (separate execute_python call): a just-created asset can resist delete
            # in the same undo pass (engine settle timing) — a later call always succeeds. Re-delete
            # any create_asset that reported delete-failed, and update its result in place.
            failed = [{"asset_path": u.get("asset_path"), "package_path": u.get("package_path"),
                       "created_dir": u.get("created_dir")}
                      for u in (res.get("undone") or [])
                      if u.get("op") == "create_asset" and u.get("result") == "delete-failed"]
            if failed:
                sweep = _exec(_CREATE_ASSET_SWEEP_BODY, {"targets": failed})
                ok_paths = {s.get("asset_path") for s in (sweep.get("swept") or []) if s.get("swept")}
                for u in res.get("undone") or []:
                    if (u.get("op") == "create_asset" and u.get("result") == "delete-failed"
                            and u.get("asset_path") in ok_paths):
                        u["result"] = "asset-deleted (post-sweep)"
            return json.dumps(res, indent=2)
        except Exception as e:
            return f"Error: {e}"
