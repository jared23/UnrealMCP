"""UserTools :: Object Properties  (spec: docs/spec/objects.md)

Clean-room reimplementation over Unreal's public Python reflection (UE 5.8). Unified
reflection commands that target ANY UObject via a selector, rather than duplicating the
actor-only tools in editor_level.py.

Target selectors (this batch):
  - actor       : display label OR unique internal name (via EditorActorSubsystem)
  - asset_path  : content path (via EditorAssetLibrary.load_asset), e.g. /Engine/BasicShapes/Cube.Cube
  - component   : optional; resolve a named component ON the actor and introspect that instead

Query convention (copied from editor_level.py): a snippet prints  @@UMCP@@<json>  on one
line; _query() finds that marker and parses the JSON after it. Params are injected as
base64 JSON via _exec() so they survive the handler's triple-single-quote wrapping. Every
snippet is wrapped with Output-Log delta capture (new Warning/Error lines -> _log_warnings).

Implemented (read-only):
  - describe_object     : introspect properties (filter + pagination + reachable type metadata)
  - get_object_property : read one value at a dotted / [indexed] path

Implemented (write; ledgered — Agent A):
  - set_object_property : set a single value at a dotted path (type-coerced from the current
                          value), plus array-element set / append / remove (read-modify-write
                          the whole TArray). Every mutation runs inside an
                          unreal.ScopedEditorTransaction and records an inverse op on the
                          per-session ledger at builtins._UMCP_LEDGERS[session] (session from
                          PARAMS["_session"], injected by _exec) so a future unified undo can
                          revert ONLY this writer's edits. Struct sub-paths (e.g.
                          body_instance.mass) are REFUSED with a clear message — the same
                          Python-reflection barrier that blocks the reads — rather than
                          silently no-op'ing. No `undo` tool is defined here (it lives in
                          editor_level.py); this module only records invertible ledger entries.

Type-metadata reachability (IMPORTANT — a finding for a future C++ handler):
Unreal's stock Python reflection exposes property metadata ONLY through each getset_descriptor's
__doc__ string, which looks like:  "(bool):  [Read-Write] <tooltip...>". From that we CAN derive:
  - ue_type  : the parenthesized Python-exposed type token (bool, StaticMeshComponent, Array[Name], Vector, Guid, ComponentMobility, ...)
  - access   : Read-Only vs Read-Write (the only editability flag exposed)
  - tooltip  : the human description
Plus, at runtime, py_type (Python class of the live value), the declaring class (owner) in the MRO,
and enum member names when the value is an EnumBase.
Newly REACHABLE via the native MCPReflectionLibrary C++ handler
(get_object_property_metadata_json), merged per-property by name when the helper is present:
  - cpp_type      : the true C++ type (int32/float/FVector/TObjectPtr<UStaticMeshComponent>/...).
  - category      : the UPROPERTY(Category=...) editor grouping.
  - clamp/ui_range: ClampMin/ClampMax and UIMin/UIMax metadata (only when the property declares them).
  - flags         : UPROPERTY flags (Edit/EditConst/BlueprintVisible/BlueprintReadOnly/Transient/Config/...).
These are merged onto the getset-descriptor-derived fields above. If the native helper is
unavailable the command degrades gracefully to the pre-helper behavior: those fields are omitted
and reported via a top-level 'metadata_unreachable' list. Class-level abstractness is NOT a
per-property attribute — see editor_discovery.search_classes / get_class_metadata_json for it.
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


# Shared Unreal-side helpers (no ''' and no backslashes — the handler wraps code in '''...''').
# Target resolution + a _ser value serializer + a _prop_names enumerator + a _meta doc parser.
_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")  # deprecated-alias reads spam DeprecationWarning; pure reads

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

def _load_asset(path):
    try:
        if not unreal.EditorAssetLibrary.does_asset_exist(path):
            return None
    except Exception:
        pass
    try:
        return unreal.EditorAssetLibrary.load_asset(path)
    except Exception:
        return None

def _resolve_target(params):
    # Returns (obj, kind, describe_str, err). kind in actor|asset. Applies component after.
    actor = params.get("actor")
    asset_path = params.get("asset_path")
    if actor:
        a = _resolve_actor(actor)
        if a is None:
            return None, None, None, "actor not found: %s" % actor
        return a, "actor", a.get_actor_label(), None
    if asset_path:
        o = _load_asset(asset_path)
        if o is None:
            return None, None, None, "asset not found / failed to load: %s" % asset_path
        return o, "asset", asset_path, None
    return None, None, None, "no target: provide actor or asset_path"

def _resolve_component(actor, comp_name):
    for c in (actor.get_components_by_class(unreal.ActorComponent) or []):
        if c and c.get_name() == comp_name:
            return c
    return None

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
    # Return (descriptor, owner_class_name) for a property name across the MRO.
    for klass in type(obj).__mro__:
        d = vars(klass).get(name)
        if d is not None and type(d).__name__ in ("getset_descriptor", "property"):
            return d, klass.__name__
    return None, None

def _meta(descriptor):
    # Parse a getset_descriptor.__doc__ of the form "(TypeToken):  [Read-Write] tooltip".
    # Returns dict(ue_type, access, tooltip). Everything is best-effort; missing -> None.
    doc = getattr(descriptor, "__doc__", None) if descriptor is not None else None
    ue_type = None; access = None; tooltip = None
    if isinstance(doc, str) and doc:
        s = doc
        if s.startswith("(") and ")" in s:
            close = s.index(")")
            ue_type = s[1:close].strip() or None
            s = s[close + 1:]
        # access flag in brackets
        for flag in ("[Read-Write]", "[Read-Only]", "[Write-Only]"):
            if flag in s:
                access = flag.strip("[]")
                s = s.replace(flag, "", 1)
                break
        tip = s.replace(chr(13), " ").replace(chr(10), " ").strip()
        tooltip = tip or None
    return {"ue_type": ue_type, "access": access, "tooltip": tooltip}

def _enum_values(v):
    t = type(v)
    out = []
    for m in dir(t):
        if m.startswith("_"):
            continue
        try:
            mv = getattr(t, m, None)
        except Exception:
            mv = None
        if isinstance(mv, unreal.EnumBase):
            out.append(m)
    return out

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
            if i >= _ARRAY_LIMIT:
                items.append("...(%d more)" % (len(v) - _ARRAY_LIMIT)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:_ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= _ARRAY_LIMIT:
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
'''


# --- Write-side coerce/ledger helpers (copied verbatim from editor_level.py) --------------
# Prepended to the set_object_property body so writes are symmetric + agent-scoped-undoable.
#   _ledger()  -> per-session undo stack at builtins._UMCP_LEDGERS[PARAMS["_session"]]
#   _settable(v) -> (json_value, restorable): Unreal value -> JSON form we can re-apply later
#   _coerce(current, value) -> Unreal value: build a settable value using current's type as hint
#                              (object paths -> load asset, lists -> Vector/Rotator/Color,
#                               enum name -> enum, primitives passthrough)
#   _resolve_actor / _find_by_name -> actor lookup by label/name
#   _descend(root, comp_name, path) -> (container, final_name, err): traverse component + dotted
#                              OBJECT paths; REFUSES struct sub-paths (which would not persist).
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


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS writer (bridge/agent process) so its undo ledger is isolated
    # from other concurrent writers. One value per OS process; separate agent-harness processes
    # get distinct ids. Override via utils["session"] if provided (validation passes "agentA").
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. New Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS as base64 JSON, run body in Unreal, return its MARKER payload.
        Adds _session for per-writer ledger isolation (copied from editor_level.py)."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # ------------------------------------------------------------------ #
    # describe_object — introspect properties (filter/pagination/meta)    #
    # ------------------------------------------------------------------ #
    _DESCRIBE_BODY = _HELPERS + r'''
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

_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 25)
depth = int(PARAMS.get("max_depth") or 1)
flt = PARAMS.get("filter")
comp_name = PARAMS.get("component")

obj, kind, desc_str, err = _resolve_target(PARAMS)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    container = obj
    container_kind = kind
    if comp_name:
        if kind != "actor":
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "component selector requires an actor target"}))
            container = None
        else:
            c = _resolve_component(obj, comp_name)
            if c is None:
                avail = [x.get_name() for x in (obj.get_components_by_class(unreal.ActorComponent) or [])]
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % comp_name, "available_components": avail}))
                container = None
            else:
                container = c
                container_kind = "component"
    if container is not None:
        # Native C++ reflection metadata (MCPReflectionLibrary) — reaches cpp_type / category /
        # clamp / ui-range / UPROPERTY flags that stock Python reflection cannot. ONE call for the
        # whole container; we index it by name (C++ name + snake_case + b-prefix-stripped snake for
        # bools) so the Python snake_case property names below can find their entry. Fully guarded:
        # if the helper is missing or errors, _meta_reached stays False and behavior is unchanged.
        _meta_by_name = {}
        _meta_reached = False
        try:
            _mjs = unreal.MCPReflectionLibrary.get_object_property_metadata_json(container, include_inherited=True)
            _mdata = json.loads(_mjs)
            for _mp in (_mdata.get("properties") or []):
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
        names = _prop_names(container)
        if flt:
            fl = flt.lower(); names = [n for n in names if fl in n.lower()]
        total = len(names)
        start = int(PARAMS.get("cursor") or 0)
        max_results = PARAMS.get("max_results")
        window = names[start:start + int(max_results)] if max_results else names[start:]
        props = []
        for n in window:
            descriptor, owner = _descriptor_for(container, n)
            m = _meta(descriptor)
            rec = {"name": n, "owner_class": owner,
                   "ue_type": m["ue_type"], "access": m["access"], "tooltip": m["tooltip"]}
            # Merge native FProperty metadata (only for props on THIS returned page). Absent
            # fields stay absent (no faking) when the name isn't found or the helper was missing.
            _nm = _meta_by_name.get(n)
            if _nm is not None:
                rec["cpp_type"] = _nm.get("cpp_type")
                if "category" in _nm:
                    rec["category"] = _nm.get("category")
                rec["flags"] = _nm.get("flags") or []
                if ("clamp_min" in _nm) or ("clamp_max" in _nm):
                    rec["clamp"] = {"min": _nm.get("clamp_min"), "max": _nm.get("clamp_max")}
                if ("ui_min" in _nm) or ("ui_max" in _nm):
                    rec["ui_range"] = {"min": _nm.get("ui_min"), "max": _nm.get("ui_max")}
            try:
                val = container.get_editor_property(n)
                rec["py_type"] = type(val).__name__
                rec["value"] = _ser(val, depth)
                if isinstance(val, unreal.EnumBase):
                    rec["enum_values"] = _enum_values(val)
            except Exception as e:
                rec["py_type"] = None
                rec["value"] = "<unreadable>"
            props.append(rec)
        nxt = start + len(window)
        result = {"status": "success",
                  "target": {"kind": container_kind, "selector": desc_str,
                             "class": container.get_class().get_name(),
                             "path_name": container.get_path_name()},
                  "total_properties": total, "returned": len(props),
                  "next_cursor": (str(nxt) if nxt < total else None),
                  "metadata_source": ("MCPReflectionLibrary" if _meta_reached else "python_reflection_only"),
                  "metadata_unreachable": ([] if _meta_reached else ["cpp_type", "category", "clamp", "flags"]),
                  "metadata_note": ("cpp_type/category/clamp+ui ranges/UPROPERTY flags are merged per-property from the native MCPReflectionLibrary C++ FProperty handler (present only on properties that declare them). Class-level abstractness is not a per-property attribute; use search_classes/get_class_metadata_json for it." if _meta_reached else "cpp_type/category/clamp-ranges/CPF_* flags are not exposed by stock Python reflection (getset_descriptor only carries ue_type + Read/Write access + tooltip); the MCPReflectionLibrary native helper was unavailable, so these fields are absent this call."),
                  "properties": props}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def describe_object(ctx, actor: str = None, asset_path: str = None,
                        component: str = None, filter: str = None,
                        max_results: int = None, cursor: int = 0,
                        max_depth: int = 1, array_element_limit: int = 25) -> str:
        """Introspect a UObject's reflected properties with filter, pagination, and the
        type-metadata that Python reflection can reach. Read-only.

        Target (choose one):
          actor:      actor display label (preferred) or unique internal name.
          asset_path: content path, e.g. '/Engine/BasicShapes/Cube.Cube'.
        component:    optional; introspect a named component on the actor instead.

        filter:       case-insensitive substring; only property names containing it.
        max_results/cursor: paginate; response returns 'next_cursor' (pass back as cursor).
        max_depth:    struct-expansion depth for values (default 1).
        array_element_limit: cap elements shown per array/set/map value.

        Per property returns: name, owner_class, ue_type (Python type token from the
        descriptor doc), access (Read-Only/Read-Write), tooltip, py_type, value
        (JSON-serialized), and enum_values when applicable. When the native
        MCPReflectionLibrary helper is present, each property is ALSO enriched with cpp_type
        (true C++ type), category (UPROPERTY grouping), flags (UPROPERTY flags array), and,
        where declared, clamp ({min,max}) and ui_range ({min,max}); 'metadata_source' reports
        which path was used. If the native helper is unavailable those fields are omitted and
        listed under 'metadata_unreachable' (graceful degradation to the prior behavior)."""
        params = {"actor": actor, "asset_path": asset_path, "component": component,
                  "filter": filter, "max_results": max_results, "cursor": cursor,
                  "max_depth": max_depth, "array_element_limit": array_element_limit}
        try:
            return json.dumps(_exec(_DESCRIBE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_object_property — read one value at a dotted / [indexed] path   #
    # ------------------------------------------------------------------ #
    _GET_PROP_BODY = _HELPERS + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 100)
depth = int(PARAMS.get("max_depth") or 2)

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

def _getprop(container, name):
    # Try the given name, then a CamelCase->snake_case fallback (spec paths use C++ names).
    try:
        return container.get_editor_property(name), None
    except Exception:
        pass
    sn = _snake(name)
    if sn != name:
        try:
            return container.get_editor_property(sn), None
        except Exception:
            pass
    return None, "no such property: %s" % name

def _parse_seg(seg):
    # seg like Tags[0] -> (Tags, 0) ; Tags -> (Tags, None)
    idx = None; name = seg
    if seg.endswith("]") and "[" in seg:
        b = seg.index("[")
        name = seg[:b]
        inner = seg[b + 1:-1]
        try:
            idx = int(inner)
        except Exception:
            idx = None
    return name, idx

obj, kind, desc_str, err = _resolve_target(PARAMS)
path = PARAMS.get("property_path")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property_path is required"}))
else:
    container = obj
    ok = True
    comp_name = PARAMS.get("component")
    if comp_name:
        if kind != "actor":
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "component selector requires an actor target"}))
            ok = False
        else:
            c = _resolve_component(obj, comp_name)
            if c is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "component not found: %s" % comp_name}))
                ok = False
            else:
                container = c
    if ok:
        segs = path.split(".")
        cur = container
        failed = None
        for i, seg in enumerate(segs):
            name, idx = _parse_seg(seg)
            if not isinstance(cur, unreal.Object):
                failed = "cannot descend into non-object at '%s' (struct sub-paths are not reliably reachable via Python reflection; read the whole struct with describe_object instead)" % segs[i - 1]
                break
            val, gerr = _getprop(cur, name)
            if gerr:
                failed = gerr
                break
            if idx is not None:
                try:
                    if isinstance(val, (unreal.Array, unreal.Set)):
                        seq = list(val)
                    else:
                        seq = val
                    val = seq[idx]
                except Exception:
                    failed = "index [%d] out of range or not indexable at '%s'" % (idx, name)
                    break
            cur = val
        if failed:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": failed}))
        else:
            print("@@UMCP@@" + json.dumps({"status": "success",
                "target": {"kind": ("component" if comp_name else kind), "selector": desc_str},
                "property_path": path,
                "py_type": type(cur).__name__,
                "value": _ser(cur, depth)}))
'''

    @mcp.tool()
    def get_object_property(ctx, property_path: str, actor: str = None,
                            asset_path: str = None, component: str = None,
                            max_depth: int = 2, array_element_limit: int = 100) -> str:
        """Read a single reflected value at a dotted / [indexed] path. Read-only.

        Target (choose one):
          actor:      actor display label (preferred) or unique internal name.
          asset_path: content path, e.g. '/Engine/BasicShapes/Cube.Cube'.
        component:    optional; read from a named component on the actor.

        property_path (required): a dotted path descending through UOBJECT properties, with
        optional array indexing, e.g.:
          'static_mesh'                         (single prop, on a component/asset)
          'static_mesh_component.static_mesh'   (descend into a component object)
          'tags[0]'                             (index into an array)
        Property names accept Python snake_case OR C++ CamelCase (auto-converted).
        NOTE: descending into STRUCT sub-fields (e.g. 'body_instance.mass') is not reliably
        reachable via stock Python reflection and returns a clear error — read the whole
        struct value with describe_object instead.

        Returns { value (JSON-serialized), py_type }."""
        params = {"property_path": property_path, "actor": actor, "asset_path": asset_path,
                  "component": component, "max_depth": max_depth,
                  "array_element_limit": array_element_limit}
        try:
            return json.dumps(_exec(_GET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_object_property — set / array-set / append / remove (ledgered) #
    # ------------------------------------------------------------------ #
    # Prepends _COERCE_HELPERS (session-aware _ledger/_settable/_coerce/_descend). Reuses the
    # same target resolution as the reads (actor|asset_path, optional component) and the same
    # struct-sub-path barrier (_descend REFUSES non-object intermediates -> clear error). Every
    # mutation runs inside unreal.ScopedEditorTransaction and pushes an inverse onto the caller's
    # session ledger. NB: no ''' and no backslashes in this body.
    _SET_OBJ_PROP_BODY = _COERCE_HELPERS + r'''
import warnings
warnings.simplefilter("ignore")

def _load_asset(path):
    try:
        if not unreal.EditorAssetLibrary.does_asset_exist(path):
            return None
    except Exception:
        pass
    try:
        return unreal.EditorAssetLibrary.load_asset(path)
    except Exception:
        return None

def _resolve_target(params):
    actor = params.get("actor")
    asset_path = params.get("asset_path")
    if actor:
        a = _resolve_actor(actor)
        if a is None:
            return None, None, None, "actor not found: %s" % actor
        return a, "actor", a.get_actor_label(), None
    if asset_path:
        o = _load_asset(asset_path)
        if o is None:
            return None, None, None, "asset not found / failed to load: %s" % asset_path
        return o, "asset", asset_path, None
    return None, None, None, "no target: provide actor or asset_path"

def _parse_index(seg):
    idx = None
    name = seg
    if seg.endswith("]") and "[" in seg:
        b = seg.index("[")
        name = seg[:b]
        inner = seg[b + 1:-1]
        try:
            idx = int(inner)
        except Exception:
            idx = None
    return name, idx

def _arr_json(pylist):
    out = []
    ok = True
    for e in pylist:
        j, r = _settable(e)
        out.append(j)
        if not r:
            ok = False
    return out, ok

mode = (PARAMS.get("mode") or "set").lower()
comp_name = PARAMS.get("component")
path = PARAMS.get("property_path")
obj, kind, desc_str, err = _resolve_target(PARAMS)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property_path is required"}))
elif comp_name and kind != "actor":
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "component selector requires an actor target"}))
elif mode not in ("set", "append", "remove"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "mode must be one of set|append|remove"}))
else:
    segs = path.split(".")
    last_name, last_idx = _parse_index(segs[-1])
    descend_path = ".".join(segs[:-1] + [last_name])
    cont, final, derr = _descend(obj, comp_name, descend_path)
    if derr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": derr}))
    else:
        try:
            cur_raw = cont.get_editor_property(final)
            have = True
        except Exception:
            have = False
        if not have:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property: %s" % final}))
        else:
            target_sel = {"actor": PARAMS.get("actor"), "asset_path": PARAMS.get("asset_path"), "component": comp_name}
            tgt_out = {"kind": ("component" if comp_name else kind), "selector": desc_str}
            if mode == "set" and last_idx is None:
                prior_json, restorable = _settable(cur_raw)
                newv = _coerce(cur_raw, PARAMS.get("value"))
                with unreal.ScopedEditorTransaction("MCP set_object_property"):
                    cont.set_editor_property(final, newv)
                after_json, _u = _settable(cont.get_editor_property(final))
                _ledger().append({"op": "set_object_property", "mode": "scalar",
                    "target": target_sel, "path": path, "prior": prior_json, "restorable": restorable})
                print("@@UMCP@@" + json.dumps({"status": "success", "kind": "scalar",
                    "target": tgt_out, "path": path, "before": prior_json, "after": after_json,
                    "restorable": restorable, "ledger_depth": len(_ledger())}))
            else:
                if not isinstance(cur_raw, unreal.Array):
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "property '%s' is not an array (py_type=%s); array ops require a TArray property" % (final, type(cur_raw).__name__)}))
                else:
                    pylist = list(cur_raw)
                    prior_arr, restorable = _arr_json(pylist)
                    op_err = None
                    if mode == "append":
                        hint = pylist[0] if pylist else None
                        pylist.append(_coerce(hint, PARAMS.get("value")))
                    elif mode == "remove":
                        if last_idx is None:
                            op_err = "remove requires an index in property_path, e.g. tags[0]"
                        elif last_idx < 0 or last_idx >= len(pylist):
                            op_err = "index [%s] out of range (len=%d)" % (last_idx, len(pylist))
                        else:
                            pylist.pop(last_idx)
                    else:
                        if last_idx is None:
                            op_err = "element set requires an index in property_path, e.g. tags[0]"
                        elif last_idx < 0 or last_idx >= len(pylist):
                            op_err = "index [%s] out of range (len=%d)" % (last_idx, len(pylist))
                        else:
                            pylist[last_idx] = _coerce(pylist[last_idx], PARAMS.get("value"))
                    if op_err:
                        print("@@UMCP@@" + json.dumps({"status": "error", "message": op_err}))
                    else:
                        with unreal.ScopedEditorTransaction("MCP set_object_property (array)"):
                            cont.set_editor_property(final, pylist)
                        after_arr, _u2 = _arr_json(list(cont.get_editor_property(final)))
                        _ledger().append({"op": "set_object_property", "mode": "array",
                            "target": target_sel, "path": descend_path,
                            "prior_array": prior_arr, "restorable": restorable})
                        print("@@UMCP@@" + json.dumps({"status": "success", "kind": "array", "array_op": mode,
                            "target": tgt_out, "path": path, "before": prior_arr, "after": after_arr,
                            "restorable": restorable, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_object_property(ctx, property_path: str, value=None, actor: str = None,
                            asset_path: str = None, component: str = None,
                            mode: str = "set") -> str:
        """Set a reflected property on any UObject target (ledgered write).

        Target (choose one):
          actor:      actor display label (preferred) or unique internal name.
          asset_path: content path, e.g. '/Engine/BasicShapes/Cube.Cube'.
        component:    optional; target a named component ON the actor instead.

        property_path (required): a dotted path through UOBJECT properties (accepts Python
        snake_case; C++ CamelCase is NOT auto-converted for writes). Examples:
          'static_mesh_component.static_mesh'  (descend into a component object, set an asset)
          'is_editor_only_actor'               (a bool on the actor)
          'tags[0]'                            (an array element — see mode)
        Descending into STRUCT sub-fields (e.g. 'body_instance.mass') is REFUSED with a clear
        error — stock Python reflection cannot write struct sub-paths reliably; read the whole
        struct with describe_object and set the whole struct property instead.

        mode:
          'set'    (default) — set the value at property_path. If the path ends in '[i]', sets
                    that array element; otherwise sets the scalar/object property.
          'append' — append `value` to the TArray at property_path.
          'remove' — remove the element at property_path's '[i]' index from the TArray.

        value is type-coerced from the CURRENT value: object/asset props accept an asset path
        string; Vector/Rotator/Color accept [..] lists; enums accept the member-name string;
        bool/number/string pass through. Array element/append use an existing element's type as
        the coercion hint.

        Returns { before, after, restorable, ledger_depth }. The prior value (or the whole prior
        array for array ops) is captured on the per-session ledger so a future unified undo can
        invert this edit; this module intentionally defines no `undo` tool of its own."""
        params = {"property_path": property_path, "value": value, "actor": actor,
                  "asset_path": asset_path, "component": component, "mode": mode}
        try:
            return json.dumps(_exec(_SET_OBJ_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
