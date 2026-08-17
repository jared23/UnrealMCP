"""UserTools :: Actor Transform Ops  (higher-level reversible transforms on placed actors)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py's proven conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, session resolution, and the
session-aware per-agent undo ledger (_COERCE_HELPERS block).

These commands compute new transforms for a SET of placed actors and apply them by
REUSING editor_level.py's `set_actor_transform` semantics: for EVERY actor moved, the
FULL prior transform (loc/rot/scale) is captured and pushed as a ledger entry that is
IDENTICAL in shape to editor_level.py's:

    {"op": "set_actor_transform", "actor_name": <unique>,
     "prior": {"loc": [x,y,z], "rot": [pitch,yaw,roll], "scale": [x,y,z]}}

editor_level.py's unified `undo` already inverts this op (it restores loc+rot+scale from
`prior`), so NO new undo branch is needed and NO `undo` tool is defined here. Each batch
of mutations runs inside ONE `unreal.ScopedEditorTransaction` and pushes ONE ledger entry
per actor moved (so `undo count=N` reverts them one-by-one, LIFO).

Implemented:
  - align_actors        (write; ledgered set_actor_transform) — match loc axes / rotation to a reference
  - distribute_actors   (write; ledgered set_actor_transform) — evenly space along an axis
  - snap_actors_to_floor(write; ledgered set_actor_transform) — line-trace down, drop onto first hit
  - mirror_actors       (write; ledgered set_actor_transform) — reflect across a pivot plane (negates scale on axis)

Read helpers reused from editor_level.py: verify results via get_actors_in_level / find_actors.
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

    # Local transform helpers shared by every command body in THIS module. No ''' / no backslashes.
    # _capture_prior(a) -> the exact dict shape editor_level.py stores under set_actor_transform.
    # _push_tx(a, prior) -> append the ledger op editor_level.py's undo already inverts.
    # _apply(a, loc, rot, scale) -> set only the components that are not None (world space).
    _TX_HELPERS = r'''
def _capture_prior(a):
    l = a.get_actor_location(); r = a.get_actor_rotation(); s = a.get_actor_scale3d()
    return {"loc": [l.x, l.y, l.z], "rot": [r.pitch, r.yaw, r.roll], "scale": [s.x, s.y, s.z]}
def _push_tx(a, prior):
    _ledger().append({"op": "set_actor_transform", "actor_name": a.get_name(), "prior": prior})
def _apply(a, loc, rot, scale):
    if loc is not None:
        a.set_actor_location(unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])), False, False)
    if rot is not None:
        a.set_actor_rotation(unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2])), False)
    if scale is not None:
        a.set_actor_scale3d(unreal.Vector(float(scale[0]), float(scale[1]), float(scale[2])))
def _resolve_set(names):
    found = []; missing = []
    for nm in (names or []):
        a = _resolve_actor(nm)
        if a is None:
            missing.append(nm)
        else:
            found.append(a)
    return found, missing
def _tx_report(a, prior):
    nl = a.get_actor_location(); nr = a.get_actor_rotation(); ns = a.get_actor_scale3d()
    return {"name": a.get_name(), "label": a.get_actor_label(),
            "before": prior,
            "after": {"loc": [round(nl.x,3), round(nl.y,3), round(nl.z,3)],
                      "rot": [round(nr.pitch,3), round(nr.yaw,3), round(nr.roll,3)],
                      "scale": [round(ns.x,3), round(ns.y,3), round(ns.z,3)]}}
'''

    # ------------------------------------------------------------------ #
    # align_actors — match location axes / rotation to a reference actor  #
    # ------------------------------------------------------------------ #
    _ALIGN_BODY = _COERCE_HELPERS + _TX_HELPERS + r'''
names = PARAMS.get("actor_names") or []
ref_spec = PARAMS.get("reference")
axes_in = PARAMS.get("axes") or []
axes = set(str(x).strip().lower() for x in axes_in)
if "rotation" in axes:
    axes.discard("rotation"); axes.update(["pitch", "yaw", "roll"])
loc_axes = axes & set(["x", "y", "z"])
rot_axes = axes & set(["pitch", "yaw", "roll"])
found, missing = _resolve_set(names)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
# Resolve the reference actor.
ref = None; ref_note = None
if ref_spec in ("first", "last") and found:
    ref = found[0] if ref_spec == "first" else found[-1]
elif ref_spec == "active":
    sel = eas.get_selected_level_actors() or []
    ref = sel[0] if sel else None
    ref_note = "no active selection" if ref is None else None
elif ref_spec:
    ref = _resolve_actor(ref_spec)
if ref is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "reference actor not resolved: %s" % str(ref_spec),
        "note": ref_note, "missing": missing}))
elif not loc_axes and not rot_axes:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no valid axes given; use any of x,y,z,pitch,yaw,roll or 'rotation'"}))
else:
    rl = ref.get_actor_location(); rr = ref.get_actor_rotation()
    moved = []; skipped = []
    with unreal.ScopedEditorTransaction("MCP align_actors"):
        for a in found:
            if a.get_name() == ref.get_name():
                skipped.append({"name": a.get_name(), "label": a.get_actor_label(), "reason": "is reference"})
                continue
            prior = _capture_prior(a)
            cl = a.get_actor_location(); cr = a.get_actor_rotation()
            new_loc = [rl.x if "x" in loc_axes else cl.x,
                       rl.y if "y" in loc_axes else cl.y,
                       rl.z if "z" in loc_axes else cl.z]
            new_rot = [rr.pitch if "pitch" in rot_axes else cr.pitch,
                       rr.yaw if "yaw" in rot_axes else cr.yaw,
                       rr.roll if "roll" in rot_axes else cr.roll]
            _apply(a, new_loc if loc_axes else None, new_rot if rot_axes else None, None)
            _push_tx(a, prior)
            moved.append(_tx_report(a, prior))
    print("@@UMCP@@" + json.dumps({"status": "success",
        "reference": {"name": ref.get_name(), "label": ref.get_actor_label()},
        "applied_axes": sorted(list(loc_axes | rot_axes)),
        "moved_count": len(moved), "moved": moved, "skipped": skipped,
        "missing": missing, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def align_actors(ctx, actor_names: list, reference: str, axes: list) -> str:
        """Align a set of placed actors to a reference actor on chosen axes (reversible).

        actor_names: labels (preferred) or unique internal names of actors to align.
        reference:   which actor to align TO — an actor label/name, or one of
                     'first' / 'last' (first/last of actor_names) / 'active'
                     (the active editor selection). The reference itself is never moved.
        axes:        which components to MATCH from the reference. Any of the location
                     axes 'x','y','z' and/or rotation components 'pitch','yaw','roll';
                     the shortcut 'rotation' expands to pitch+yaw+roll. e.g. ['z'] drops
                     everything to the reference's Z; ['x','y'] stacks a column;
                     ['rotation'] copies the reference's orientation.

        Ledgered write: pushes ONE set_actor_transform entry per moved actor (full prior
        transform captured), so editor_level's `undo` restores each exactly."""
        params = {"actor_names": actor_names, "reference": reference, "axes": axes}
        try:
            return json.dumps(_exec(_ALIGN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # distribute_actors — evenly space actors along an axis               #
    # ------------------------------------------------------------------ #
    _DISTRIBUTE_BODY = _COERCE_HELPERS + _TX_HELPERS + r'''
names = PARAMS.get("actor_names") or []
axis = str(PARAMS.get("axis") or "").strip().lower()
spacing = PARAMS.get("spacing")
idx = {"x": 0, "y": 1, "z": 2}.get(axis)
found, missing = _resolve_set(names)
if idx is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "axis must be x, y or z (got %s)" % axis}))
elif len(found) < 2:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "need at least 2 resolvable actors to distribute (found %d)" % len(found),
        "missing": missing}))
else:
    def _coord(a):
        l = a.get_actor_location(); return [l.x, l.y, l.z][idx]
    order = sorted(found, key=_coord)
    n = len(order)
    base = _coord(order[0])
    if spacing is None:
        span = _coord(order[-1]) - base
        step = span / float(n - 1)
        mode = "even_between_endpoints"
    else:
        step = float(spacing)
        mode = "fixed_spacing"
    moved = []
    with unreal.ScopedEditorTransaction("MCP distribute_actors"):
        for i, a in enumerate(order):
            target = base + i * step
            prior = _capture_prior(a)
            cl = a.get_actor_location()
            new_loc = [cl.x, cl.y, cl.z]
            new_loc[idx] = target
            # skip a true no-op to keep the ledger tight (first actor in even mode never moves)
            if abs(new_loc[idx] - [cl.x, cl.y, cl.z][idx]) < 1e-6:
                moved.append({"name": a.get_name(), "label": a.get_actor_label(),
                              "before": prior, "after": prior, "noop": True})
                continue
            _apply(a, new_loc, None, None)
            _push_tx(a, prior)
            rep = _tx_report(a, prior); rep["noop"] = False
            moved.append(rep)
    print("@@UMCP@@" + json.dumps({"status": "success", "axis": axis, "mode": mode,
        "spacing": (round(step, 4)), "anchor": {"name": order[0].get_name(), "coord": round(base, 4)},
        "count": n, "moved": moved, "missing": missing, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def distribute_actors(ctx, actor_names: list, axis: str, spacing: float = None) -> str:
        """Evenly space a set of placed actors along one world axis (reversible).

        actor_names: labels (preferred) or unique internal names (need at least 2).
        axis:        'x', 'y' or 'z' — the world axis to distribute along. Actors are
                     first SORTED by their current position on this axis.
        spacing:     optional fixed gap between consecutive actors (in cm). When given,
                     the lowest-positioned actor stays put and each following actor is
                     placed +spacing further along (negative spacing reverses direction).
                     When omitted, the two end actors stay put and the interior actors
                     are spread evenly between them.

        Only the chosen axis coordinate changes; the other two are preserved.
        Ledgered write: ONE set_actor_transform entry per actor actually moved (full prior
        transform captured), so editor_level's `undo` restores each exactly."""
        params = {"actor_names": actor_names, "axis": axis, "spacing": spacing}
        try:
            return json.dumps(_exec(_DISTRIBUTE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # snap_actors_to_floor — line-trace down, drop onto first hit         #
    # ------------------------------------------------------------------ #
    _SNAP_BODY = _COERCE_HELPERS + _TX_HELPERS + r'''
names = PARAMS.get("actor_names") or []
channel = str(PARAMS.get("trace_channel") or "visibility").strip().lower()
trace_map = {"visibility": unreal.TraceTypeQuery.TRACE_TYPE_QUERY1,
             "camera": unreal.TraceTypeQuery.TRACE_TYPE_QUERY2}
tq = trace_map.get(channel, unreal.TraceTypeQuery.TRACE_TYPE_QUERY1)
gap = float(PARAMS.get("gap") or 0.0)
found, missing = _resolve_set(names)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
elif not found:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no resolvable actors", "missing": missing}))
else:
    moved = []; no_hit = []
    with unreal.ScopedEditorTransaction("MCP snap_actors_to_floor"):
        for a in found:
            try:
                origin, extent = a.get_actor_bounds(False)
            except Exception as e:
                no_hit.append({"name": a.get_name(), "label": a.get_actor_label(), "reason": "no bounds: %s" % str(e)})
                continue
            loc = a.get_actor_location()
            bottom = origin.z - extent.z
            start = unreal.Vector(loc.x, loc.y, origin.z + extent.z + 50.0)
            end = unreal.Vector(loc.x, loc.y, origin.z - extent.z - 100000.0)
            hit = unreal.SystemLibrary.line_trace_single(world, start, end, tq, False, [a],
                                                         unreal.DrawDebugTrace.NONE, True)
            d = hit.to_dict() if hit is not None else None
            if not d or not bool(d.get("blocking_hit")):
                no_hit.append({"name": a.get_name(), "label": a.get_actor_label(), "reason": "no blocking hit below"})
                continue
            ip = d["impact_point"]
            new_z = ip.z + (loc.z - bottom) + gap
            prior = _capture_prior(a)
            _apply(a, [loc.x, loc.y, new_z], None, None)
            _push_tx(a, prior)
            he = d.get("hit_actor")
            rep = _tx_report(a, prior)
            rep["floor"] = {"z": round(ip.z, 3),
                            "hit_actor": (he.get_actor_label() if he else None)}
            moved.append(rep)
    print("@@UMCP@@" + json.dumps({"status": "success", "trace_channel": channel,
        "gap": gap, "moved_count": len(moved), "moved": moved,
        "no_hit": no_hit, "missing": missing, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def snap_actors_to_floor(ctx, actor_names: list, trace_channel: str = "visibility",
                             gap: float = 0.0) -> str:
        """Drop actors straight down onto the first surface beneath them (reversible).

        For each actor a downward line trace is cast (ignoring the actor itself) from just
        above its bounding box; the actor is then moved down/up so the BOTTOM of its
        bounding box rests on the impact point. Actors with nothing solid below are left
        untouched and reported under 'no_hit'.

        actor_names:   labels (preferred) or unique internal names.
        trace_channel: 'visibility' (default) or 'camera' — the collision trace channel.
        gap:           optional extra clearance (cm) left between the actor's bottom and the
                       surface (default 0 = flush).

        Uses the editor world + SystemLibrary.line_trace_single (HitResult read via
        to_dict, since its fields are protected). Ledgered write: ONE set_actor_transform
        entry per actor moved (full prior transform captured), so `undo` restores each."""
        params = {"actor_names": actor_names, "trace_channel": trace_channel, "gap": gap}
        try:
            return json.dumps(_exec(_SNAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # mirror_actors — reflect placement across a pivot plane on an axis   #
    # ------------------------------------------------------------------ #
    _MIRROR_BODY = _COERCE_HELPERS + _TX_HELPERS + r'''
names = PARAMS.get("actor_names") or []
axis = str(PARAMS.get("axis") or "").strip().lower()
pivot = PARAMS.get("pivot")
idx = {"x": 0, "y": 1, "z": 2}.get(axis)
found, missing = _resolve_set(names)
if idx is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "axis must be x, y or z (got %s)" % axis}))
elif not found:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no resolvable actors", "missing": missing}))
else:
    def _coord(a):
        l = a.get_actor_location(); return [l.x, l.y, l.z][idx]
    if pivot is None:
        coords = [_coord(a) for a in found]
        p = sum(coords) / float(len(coords))
        pivot_mode = "centroid"
    else:
        p = float(pivot); pivot_mode = "explicit"
    moved = []
    with unreal.ScopedEditorTransaction("MCP mirror_actors"):
        for a in found:
            prior = _capture_prior(a)
            cl = a.get_actor_location(); cs = a.get_actor_scale3d()
            new_loc = [cl.x, cl.y, cl.z]
            new_loc[idx] = 2.0 * p - new_loc[idx]
            new_scale = [cs.x, cs.y, cs.z]
            new_scale[idx] = -new_scale[idx]
            _apply(a, new_loc, None, new_scale)
            _push_tx(a, prior)
            moved.append(_tx_report(a, prior))
    print("@@UMCP@@" + json.dumps({"status": "success", "axis": axis,
        "pivot": round(p, 4), "pivot_mode": pivot_mode,
        "moved_count": len(moved), "moved": moved, "missing": missing,
        "note": "reflects location across the pivot plane and negates scale on that axis (UE's mirror convention); involution -> mirroring again with the same pivot restores.",
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def mirror_actors(ctx, actor_names: list, axis: str, pivot: float = None) -> str:
        """Mirror a set of placed actors across a plane perpendicular to one world axis
        (reversible; an involution).

        actor_names: labels (preferred) or unique internal names.
        axis:        'x', 'y' or 'z' — the plane normal to mirror across.
        pivot:       the coordinate ON that axis of the mirror plane. When omitted, the
                     centroid of the selected actors on that axis is used (the group mirrors
                     about its own center).

        Each actor's position is reflected across the plane (coord -> 2*pivot - coord) and
        its scale on that axis is NEGATED — matching Unreal's own mirror convention (negative
        scale flips the mesh; a Rotator cannot represent a reflection). This is an involution:
        calling it again with the same pivot returns the actors to their exact original state.

        Ledgered write: ONE set_actor_transform entry per actor (full prior transform,
        including scale, captured), so editor_level's `undo` restores each exactly."""
        params = {"actor_names": actor_names, "axis": axis, "pivot": pivot}
        try:
            return json.dumps(_exec(_MIRROR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
