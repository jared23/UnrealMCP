"""UserTools :: World Settings  (spec: docs/spec/world_settings.md)

Clean-room reimplementation over Unreal's public Python reflection (UE 5.8). Read + a
single reversible scalar/bool/enum write against the active level's AWorldSettings, the
per-level actor returned by  world.get_world_settings()  where
  world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
          (falls back to unreal.EditorLevelLibrary.get_editor_world()).

Query convention (copied from editor_level.py): a snippet prints  @@UMCP@@<json>  on one
line; _query() finds that marker and parses the JSON after it. Params are injected as
base64 JSON via _exec() so they survive the handler's triple-single-quote wrapping. Every
snippet is wrapped with Output-Log delta capture (new Warning/Error lines -> _log_warnings).

Implemented:
  - get_world_settings   (read-only; reflection dump of the WorldSettings actor, values +
                          native cpp_type/category/flags metadata + a curated highlights block)
  - get_game_mode        (read-only; the level's DefaultGameMode override class, if any)
  - set_world_setting    (write; ledgered; ONE reflected scalar/bool/enum/vector property.
                          Captures the prior value and records an inverse op so a unified
                          undo restores WorldSettings byte-identical. Object/class refs
                          (game mode, physics-volume class, nav config, sound mix), arrays,
                          maps and opaque structs are REFUSED — not trivially reversible.)
  - undo                 (LOCAL agent-scoped revert of THIS module's set_world_setting op so
                          validation can leave WorldSettings byte-identical + ledger depth 0.
                          Any other op is re-pushed and it stops — it never touches another
                          module's ledger entry.)

DEFERRED (see report): set_game_mode_override — reversible in principle (capture prior
DefaultGameMode class, restore it), but DEFERRED: the value is a TSubclassOf<AGameModeBase>
(a UClass), which editor_level's _coerce does NOT handle (it loads asset PATHS into UObjects,
not UClasses), and changing a level's authoritative default game mode is a broad gameplay
setting. Per the safety brief (defer heavy/side-effectful or non-trivially-coercible writes),
this ships read-only via get_game_mode.

Write safety: every mutation runs inside unreal.ScopedEditorTransaction (atomic + editor-
undoable) AND records an inverse op on the per-session ledger at
builtins._UMCP_LEDGERS[PARAMS["_session"]] (session injected by _exec) so concurrent agents
never pop each other's entries.

Ledger op introduced by this module (for the coordinator to fold into editor_level.undo):
  {"op": "set_world_setting", "property": <python_prop_name>, "prior": <settable_json>,
   "restorable": <bool>}
  Inverse: ws = <editor world>.get_world_settings(); cur = ws.get_editor_property(property);
           ws.set_editor_property(property, _coerce(cur, prior))  inside a ScopedEditorTransaction.
  (WorldSettings is neither an actor/asset/component, so this needs its own branch rather than
   reusing set_object_property's actor/asset target schema.)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim pattern from editor_level.py) ----------
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


# --- Write-side coerce/ledger helpers (copied VERBATIM from editor_level.py) ---------------
# _ledger() -> per-session undo stack; _settable(v) -> (json, restorable); _coerce(current, value)
# -> Unreal value using current's type as a hint. No ''' and no backslashes below.
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
def _get_editor_world():
    w = None
    try:
        w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception:
        w = None
    if w is None:
        try:
            w = unreal.EditorLevelLibrary.get_editor_world()
        except Exception:
            w = None
    return w
def _get_ws():
    w = _get_editor_world()
    if w is None:
        return None
    try:
        return w.get_world_settings()
    except Exception:
        return None
def _snake(n):
    out = []
    for i, ch in enumerate(n):
        if ch.isupper():
            if i > 0:
                out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)
def _resolve_ws_prop(ws, name):
    # Accept a python snake_case name, a C++ CamelCase name, or a bBool name. Return the
    # working python property name (or None). Tries: as-given, snake(name), and, for bool
    # UPROPERTYs, snake with the leading 'b' stripped.
    cands = [name]
    sn = _snake(name)
    if sn != name:
        cands.append(sn)
    if len(name) > 1 and name[0] == "b" and name[1:2].isupper():
        cands.append(_snake(name[1:]))
    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        try:
            ws.get_editor_property(c)
            return c
        except Exception:
            continue
    return None
'''


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

    # ------------------------------------------------------------------ #
    # get_world_settings — reflection dump of the WorldSettings actor      #
    # ------------------------------------------------------------------ #
    _GET_WS_BODY = _COERCE_HELPERS + r'''
import warnings
warnings.simplefilter("ignore")

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

def _descriptor_for(obj, name):
    for klass in type(obj).__mro__:
        d = vars(klass).get(name)
        if d is not None and type(d).__name__ in ("getset_descriptor", "property"):
            return d, klass.__name__
    return None, None

def _meta(descriptor):
    doc = getattr(descriptor, "__doc__", None) if descriptor is not None else None
    ue_type = None; access = None; tooltip = None
    if isinstance(doc, str) and doc:
        s = doc
        if s.startswith("(") and ")" in s:
            close = s.index(")")
            ue_type = s[1:close].strip() or None
            s = s[close + 1:]
        for flag in ("[Read-Write]", "[Read-Only]", "[Write-Only]"):
            if flag in s:
                access = flag.strip("[]")
                s = s.replace(flag, "", 1)
                break
        tip = s.replace(chr(13), " ").replace(chr(10), " ").strip()
        tooltip = tip or None
    return {"ue_type": ue_type, "access": access, "tooltip": tooltip}

_ARR = 25
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
            if i >= _ARR:
                items.append("...(%d more)" % (len(v) - _ARR)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:_ARR]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= _ARR:
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

world = _get_editor_world()
ws = _get_ws()
if ws is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world / WorldSettings unavailable"}))
else:
    depth = int(PARAMS.get("max_depth") or 1)
    flt = PARAMS.get("filter")
    # native FProperty metadata for the whole object, indexed by C++ name + snake + b-strip snake
    _meta_by_name = {}
    _meta_reached = False
    try:
        _mjs = unreal.MCPReflectionLibrary.get_object_property_metadata_json(ws, include_inherited=True)
        _md = json.loads(_mjs)
        for _mp in (_md.get("properties") or []):
            _cn = _mp.get("name")
            if not _cn:
                continue
            _meta_by_name[_cn] = _mp
            _meta_by_name.setdefault(_snake(_cn), _mp)
            if len(_cn) > 1 and _cn[0] == "b" and _cn[1:2].isupper():
                _meta_by_name.setdefault(_snake(_cn[1:]), _mp)
        _meta_reached = True
    except Exception:
        _meta_by_name = {}
        _meta_reached = False
    names = _prop_names(ws)
    if flt:
        fl = flt.lower(); names = [n for n in names if fl in n.lower()]
    total = len(names)
    start = int(PARAMS.get("cursor") or 0)
    max_results = PARAMS.get("max_results")
    window = names[start:start + int(max_results)] if max_results else names[start:]
    props = []
    for n in window:
        descriptor, owner = _descriptor_for(ws, n)
        m = _meta(descriptor)
        rec = {"name": n, "owner_class": owner, "ue_type": m["ue_type"],
               "access": m["access"], "tooltip": m["tooltip"]}
        _nm = _meta_by_name.get(n)
        if _nm is not None:
            rec["cpp_type"] = _nm.get("cpp_type")
            if "category" in _nm:
                rec["category"] = _nm.get("category")
            rec["flags"] = _nm.get("flags") or []
        try:
            val = ws.get_editor_property(n)
            rec["py_type"] = type(val).__name__
            rec["value"] = _ser(val, depth)
        except Exception:
            rec["py_type"] = None
            rec["value"] = "<unreadable>"
        props.append(rec)
    nxt = start + len(window)
    # curated highlights (common scalar/bool settings), read directly by python name
    highlights = {}
    for hn in ["kill_z", "world_to_meters", "global_gravity_z", "global_gravity_set",
               "enable_navigation_system", "default_color_scale", "default_game_mode",
               "default_physics_volume_class", "kill_z_damage_type"]:
        try:
            highlights[hn] = _ser(ws.get_editor_property(hn), 1)
        except Exception:
            pass
    result = {"status": "success",
              "world_name": (world.get_name() if world else None),
              "world_path": (world.get_path_name() if world else None),
              "world_settings_class": ws.get_class().get_name(),
              "world_settings_path": ws.get_path_name(),
              "total_properties": total, "returned": len(props),
              "next_cursor": (str(nxt) if nxt < total else None),
              "metadata_source": ("MCPReflectionLibrary" if _meta_reached else "python_reflection_only"),
              "highlights": highlights,
              "properties": props}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_world_settings(ctx, filter: str = None, max_results: int = None,
                           cursor: int = 0, max_depth: int = 1) -> str:
        """Dump the active level's WorldSettings (AWorldSettings actor) via reflection. Read-only.

        Returns the world name/path, the WorldSettings class + object path, a 'highlights'
        block of common settings (kill_z, world_to_meters, global_gravity_z + its override
        bool, enable_navigation_system, default_color_scale, default_game_mode,
        default_physics_volume_class, kill_z_damage_type), and a full paginated property list.

        Each property carries: name (python), owner_class, ue_type, access (Read-Only/Read-Write),
        tooltip, py_type, value (JSON-serialized), and — when the native MCPReflectionLibrary
        helper is present — cpp_type / category / flags (UPROPERTY flags).

        filter:      case-insensitive substring on property name.
        max_results/cursor: paginate; response returns 'next_cursor' (pass back as cursor).
        max_depth:   struct-expansion depth for values (default 1)."""
        params = {"filter": filter, "max_results": max_results, "cursor": cursor,
                  "max_depth": max_depth}
        try:
            return json.dumps(_exec(_GET_WS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_game_mode — the level's DefaultGameMode override class (read)    #
    # ------------------------------------------------------------------ #
    _GET_GM_BODY = _COERCE_HELPERS + r'''
import warnings
warnings.simplefilter("ignore")
ws = _get_ws()
if ws is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world / WorldSettings unavailable"}))
else:
    gm = None
    try:
        gm = ws.get_editor_property("default_game_mode")
    except Exception as e:
        gm = None
    if gm is None:
        print("@@UMCP@@" + json.dumps({"status": "success", "has_override": False,
            "default_game_mode": None,
            "note": "This level has no DefaultGameMode override; the project default game mode is used at runtime."}))
    else:
        info = {"status": "success", "has_override": True}
        try: info["class_name"] = gm.get_name()
        except Exception: info["class_name"] = None
        try: info["class_path"] = gm.get_path_name()
        except Exception: info["class_path"] = None
        try: info["py_type"] = type(gm).__name__
        except Exception: info["py_type"] = None
        info["default_game_mode"] = info.get("class_path")
        print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_game_mode(ctx) -> str:
        """Read the active level's DefaultGameMode override (the WorldSettings
        'default_game_mode' TSubclassOf<AGameModeBase>). Read-only.

        Returns has_override plus the game mode class name + full class path when set, or
        has_override=False when the level defers to the project's default game mode.

        NOTE: there is no set_game_mode_override tool — see the module docstring. Writing a
        TSubclassOf<AGameModeBase> requires UClass coercion (not the asset-path->UObject
        coercion the write path supports) and changing a level's authoritative game mode is a
        broad gameplay setting, so it is intentionally deferred to read-only."""
        try:
            return json.dumps(_exec(_GET_GM_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_world_setting — ONE reversible scalar/bool/enum/vector (write)   #
    # ------------------------------------------------------------------ #
    _SET_WS_BODY = _COERCE_HELPERS + r'''
import warnings
warnings.simplefilter("ignore")
ws = _get_ws()
prop_in = PARAMS.get("property")
if ws is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world / WorldSettings unavailable"}))
elif not prop_in:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property is required"}))
else:
    resolved = _resolve_ws_prop(ws, prop_in)
    if resolved is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no such WorldSettings property (tried python/snake/bBool variants): %s" % prop_in}))
    else:
        cur = ws.get_editor_property(resolved)
        # Only benign, trivially-reversible value kinds. Object/Class refs, arrays, maps and
        # opaque structs are refused (not faithfully coercible/reversible from a JSON scalar).
        ALLOWED = (bool, int, float, str, unreal.Name, unreal.Text, unreal.Vector,
                   unreal.Rotator, unreal.Vector2D, unreal.LinearColor, unreal.Color, unreal.EnumBase)
        if not isinstance(cur, ALLOWED):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing to write non-scalar property '%s' (py_type=%s): only bool/number/string/name/enum/vector/rotator/color are reversibly settable here; object/class refs (e.g. game mode, physics-volume class, nav config), arrays, maps and opaque structs are not." % (resolved, type(cur).__name__),
                "property": resolved, "current_py_type": type(cur).__name__}))
        else:
            prior_json, restorable = _settable(cur)
            newv = _coerce(cur, PARAMS.get("value"))
            with unreal.ScopedEditorTransaction("MCP set_world_setting"):
                ws.set_editor_property(resolved, newv)
            after_json, _u = _settable(ws.get_editor_property(resolved))
            _ledger().append({"op": "set_world_setting", "property": resolved,
                              "prior": prior_json, "restorable": restorable})
            print("@@UMCP@@" + json.dumps({"status": "success", "property": resolved,
                "before": prior_json, "after": after_json, "restorable": restorable,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_world_setting(ctx, property: str, value=None) -> str:
        """Set a SINGLE reflected scalar/bool/enum/vector property on the level's WorldSettings
        (ledgered write). The prior value is captured so a unified undo restores it exactly.

        property: WorldSettings property name — python snake_case (e.g. 'kill_z',
                  'world_to_meters', 'global_gravity_z', 'global_gravity_set',
                  'default_color_scale'), or the C++/bBool name (auto-converted).
        value:    the new value, type-coerced from the CURRENT value: bool/number/string
                  pass through; enums accept the member-name string; Vector/Rotator/Color
                  accept [..] lists.

        Only benign, trivially-reversible kinds are allowed. Object/class references (game
        mode, physics-volume class, navigation config, sound mix), arrays, maps and opaque
        structs are REFUSED with a clear message — they are not faithfully reversible from a
        JSON scalar. Use get_world_settings first to see writable properties + current values.

        Returns { before, after, restorable, ledger_depth }. Records op 'set_world_setting'
        {property, prior, restorable} on the per-session ledger for the unified undo."""
        params = {"property": property, "value": value}
        try:
            return json.dumps(_exec(_SET_WS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
