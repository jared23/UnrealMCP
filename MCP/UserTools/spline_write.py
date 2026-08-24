"""UserTools :: Spline component authoring  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8),
mirroring editor_level.py / volumes_write.py / foliage_write.py conventions VERBATIM: base64
-injected PARAMS, the Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware
per-agent undo ledger, and the _COERCE_HELPERS block.

Reversible WRITE tools + READ inspectors for USplineComponent geometry on placed level actors
(a USplineComponent has ~20 script get/set methods; these tools drive the point-array subset):

  - set_spline_points  -> replace the WHOLE point array (positions, per-point type + custom
                          tangents, closed-loop). Captures the PRIOR whole array for a faithful
                          inverse. Ledgered op "set_spline_points".
  - set_spline_point   -> edit ONE point (location / arrive+leave tangents / point-type) in place.
                          Captures the PRIOR single point. Ledgered op "set_spline_point".
  - get_spline         -> READ: point count, per-point transforms/tangents/types, spline length,
                          closed-loop, plus N evenly-sampled world transforms. Non-ledgered.
  - validate_spline    -> READ: structural health (>=2 points, finite tangents, self-consistency,
                          zero-length segments). Non-ledgered.

Spline API notes (UE 5.8.1; probed live against TestMCPSetup scratch level):
  - Coordinate space is unreal.SplineCoordinateSpace.LOCAL / .WORLD. In THIS build the tangent
    GETTERS require a coordinate space: get_arrive_tangent_at_spline_point(index, coordinate_space)
    and get_leave_tangent_at_spline_point(index, coordinate_space).
  - Point types (unreal.SplinePointType): LINEAR, CURVE, CURVE_CLAMPED, CURVE_CUSTOM_TANGENT,
    CONSTANT. str(pt) reads "<SplinePointType.CURVE: 1>" -> name parsed between '.' and ':'.
  - Writers: set_spline_points(points, coordinate_space, update_spline); add_spline_point(position,
    coordinate_space, update_spline); clear_spline_points(update_spline); set_spline_point_type(
    index, type, update_spline); set_tangents_at_spline_point(index, arrive, leave, coordinate_space,
    update_spline) -- note this last one FORCES the point's type to CURVE_CUSTOM_TANGENT. So a
    faithful restore only re-applies captured tangents when the captured type is CURVE_CUSTOM_TANGENT;
    for LINEAR/CURVE/CURVE_CLAMPED/CONSTANT the (position + type) fully determine the geometry.
  - set_closed_loop(closed, update_spline); update_spline() commits a batch edited with update=False.
  - Actor/component resolution: an actor's spline is found via
    actor.get_components_by_class(unreal.SplineComponent); component_name=None picks the first one.

Write safety (agent-scoped undo): every mutation runs inside an unreal.ScopedEditorTransaction AND
records ONE inverse op on the PER-SESSION agent ledger at builtins._UMCP_LEDGERS[session]:
  - set_spline_points -> op "set_spline_points" {actor_name, component_name, coordinate_space,
                         prior:{points:[{location,arrive,leave,type}], closed_loop}}
                         (NEW editor_level.undo branch -> clear + rebuild the prior array).
  - set_spline_point  -> op "set_spline_point"  {actor_name, component_name, coordinate_space,
                         index, prior:{location,arrive,leave,type}}
                         (NEW editor_level.undo branch -> re-apply the prior single point).
No `undo` tool is defined here (editor_level.py owns the ONE unified undo). Coordinate space is
persisted in the ledger so the inverse restores in the SAME space it captured. Prefix N/A (edits
existing actors; no assets created).
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
    # Defines the session-aware _ledger().
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
'''

    # Spline-specific helpers (prepended to spline bodies). No ''' / no backslashes.
    _SPLINE_HELPERS = r'''
def _spl_resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
def _spl_find(actor, comp_name):
    comps = actor.get_components_by_class(unreal.SplineComponent) or []
    if not comps:
        return None
    if not comp_name:
        return comps[0]
    for c in comps:
        if c.get_name() == comp_name:
            return c
    return None
def _spl_list_names(actor):
    return [c.get_name() for c in (actor.get_components_by_class(unreal.SplineComponent) or [])]
def _spl_cs(name):
    if str(name).lower() == "local":
        return unreal.SplineCoordinateSpace.LOCAL
    return unreal.SplineCoordinateSpace.WORLD
def _spl_type_name(pt):
    s = str(pt)
    return s.split(".")[-1].split(":")[0].split(">")[0].strip()
def _spl_type_from_name(nm):
    try:
        return getattr(unreal.SplinePointType, str(nm).upper())
    except Exception:
        return unreal.SplinePointType.CURVE
def _spl_vec(v):
    return unreal.Vector(float(v[0]), float(v[1]), float(v[2]))
def _spl_capture_point(comp, i, cs):
    loc = comp.get_location_at_spline_point(i, cs)
    at = comp.get_arrive_tangent_at_spline_point(i, cs)
    lt = comp.get_leave_tangent_at_spline_point(i, cs)
    return {"index": i, "location": [loc.x, loc.y, loc.z],
            "arrive": [at.x, at.y, at.z], "leave": [lt.x, lt.y, lt.z],
            "type": _spl_type_name(comp.get_spline_point_type(i))}
def _spl_capture_all(comp, cs):
    n = comp.get_number_of_spline_points()
    pts = []
    for i in range(n):
        p = _spl_capture_point(comp, i, cs)
        p.pop("index", None)
        pts.append(p)
    return {"points": pts, "closed_loop": comp.is_closed_loop()}
'''

    _SPL_PREFIX = _COERCE_HELPERS + _SPLINE_HELPERS

    # ------------------------------------------------------------------ #
    # set_spline_points — replace the whole point array (reversible)       #
    # ------------------------------------------------------------------ #
    _SET_POINTS_BODY = _SPL_PREFIX + r'''
actor = _spl_resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _spl_find(actor, PARAMS.get("component_name"))
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no SplineComponent named %r on actor (splines: %s)" % (PARAMS.get("component_name"), _spl_list_names(actor))}))
    else:
        items = PARAMS.get("points") or []
        if not items:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no points supplied"}))
        else:
            cs = _spl_cs(PARAMS.get("coordinate_space") or "world")
            csname = "local" if cs == unreal.SplineCoordinateSpace.LOCAL else "world"
            prior = _spl_capture_all(comp, cs)
            closed_req = PARAMS.get("closed_loop")
            with unreal.ScopedEditorTransaction("MCP set_spline_points"):
                comp.modify()
                comp.clear_spline_points(False)
                norm = []
                for it in items:
                    if isinstance(it, (list, tuple)):
                        loc = it; typ = None; arr = None; lev = None
                    else:
                        loc = it.get("location") or it.get("loc") or [0, 0, 0]
                        typ = it.get("type"); arr = it.get("arrive_tangent") or it.get("arrive")
                        lev = it.get("leave_tangent") or it.get("leave")
                    norm.append((loc, typ, arr, lev))
                    comp.add_spline_point(_spl_vec(loc), cs, False)
                for i, (loc, typ, arr, lev) in enumerate(norm):
                    if typ is not None:
                        comp.set_spline_point_type(i, _spl_type_from_name(typ), False)
                    if arr is not None and lev is not None:
                        comp.set_tangents_at_spline_point(i, _spl_vec(arr), _spl_vec(lev), cs, False)
                if closed_req is not None:
                    comp.set_closed_loop(bool(closed_req), False)
                comp.update_spline()
            _ledger().append({"op": "set_spline_points", "actor_name": actor.get_name(),
                "component_name": comp.get_name(), "coordinate_space": csname, "prior": prior})
            after = _spl_capture_all(comp, cs)
            print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                "component": comp.get_name(), "coordinate_space": csname,
                "points_before": len(prior["points"]), "points_after": len(after["points"]),
                "spline_length": round(comp.get_spline_length(), 3),
                "closed_loop": comp.is_closed_loop(),
                "undo_op": "set_spline_points", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_spline_points(ctx, actor_name: str, points: list, component_name: str = None,
                          coordinate_space: str = "world", closed_loop: bool = None) -> str:
        """Replace the ENTIRE point array of a USplineComponent on a placed level actor.

        actor_name:       actor label or unique name that owns the spline.
        points:           list of points. Each item is either a plain [x, y, z] location, or a dict
                          {"location":[x,y,z], "type": one of
                          LINEAR|CURVE|CURVE_CLAMPED|CURVE_CUSTOM_TANGENT|CONSTANT (optional),
                          "arrive_tangent":[x,y,z] & "leave_tangent":[x,y,z] (optional; supplying
                          both forces the point type to CURVE_CUSTOM_TANGENT)}.
        component_name:   name of the SplineComponent (default: the actor's first SplineComponent).
        coordinate_space: 'world' (default) or 'local' — the space for point locations AND tangents.
        closed_loop:      optional; set the spline's closed-loop flag.

        Ledgered write (op 'set_spline_points'): captures the PRIOR whole array (locations, tangents,
        types, closed-loop) so `undo` rebuilds the spline exactly as found."""
        params = {"actor_name": actor_name, "points": points, "component_name": component_name,
                  "coordinate_space": coordinate_space, "closed_loop": closed_loop}
        try:
            return json.dumps(_exec(_SET_POINTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_spline_point — edit ONE point in place (reversible)             #
    # ------------------------------------------------------------------ #
    _SET_POINT_BODY = _SPL_PREFIX + r'''
actor = _spl_resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _spl_find(actor, PARAMS.get("component_name"))
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no SplineComponent named %r on actor (splines: %s)" % (PARAMS.get("component_name"), _spl_list_names(actor))}))
    else:
        idx = int(PARAMS["index"])
        n = comp.get_number_of_spline_points()
        if idx < 0 or idx >= n:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "index %d out of range (spline has %d points)" % (idx, n)}))
        else:
            cs = _spl_cs(PARAMS.get("coordinate_space") or "world")
            csname = "local" if cs == unreal.SplineCoordinateSpace.LOCAL else "world"
            prior = _spl_capture_point(comp, idx, cs)
            loc = PARAMS.get("location"); typ = PARAMS.get("type")
            arr = PARAMS.get("arrive_tangent"); lev = PARAMS.get("leave_tangent")
            applied = {}
            with unreal.ScopedEditorTransaction("MCP set_spline_point"):
                comp.modify()
                if loc is not None:
                    comp.set_location_at_spline_point(idx, _spl_vec(loc), cs, False)
                    applied["location"] = [float(loc[0]), float(loc[1]), float(loc[2])]
                if typ is not None:
                    comp.set_spline_point_type(idx, _spl_type_from_name(typ), False)
                    applied["type"] = str(typ).upper()
                if arr is not None and lev is not None:
                    comp.set_tangents_at_spline_point(idx, _spl_vec(arr), _spl_vec(lev), cs, False)
                    applied["tangents"] = {"arrive": [float(arr[0]), float(arr[1]), float(arr[2])],
                                           "leave": [float(lev[0]), float(lev[1]), float(lev[2])]}
                comp.update_spline()
            _ledger().append({"op": "set_spline_point", "actor_name": actor.get_name(),
                "component_name": comp.get_name(), "coordinate_space": csname,
                "index": idx, "prior": prior})
            print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
                "component": comp.get_name(), "coordinate_space": csname, "index": idx,
                "applied": applied, "prior": prior, "now": _spl_capture_point(comp, idx, cs),
                "undo_op": "set_spline_point", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_spline_point(ctx, actor_name: str, index: int, component_name: str = None,
                         location: list = None, arrive_tangent: list = None,
                         leave_tangent: list = None, type: str = None,
                         coordinate_space: str = "world") -> str:
        """Edit ONE point of a USplineComponent in place (leaves the other points untouched).

        actor_name:       actor label or unique name that owns the spline.
        index:            0-based point index (must be within the current point count).
        component_name:   name of the SplineComponent (default: the actor's first SplineComponent).
        location:         optional [x, y, z] to move the point to.
        arrive_tangent /  optional; supply BOTH to set custom tangents (forces the point's type to
        leave_tangent:    CURVE_CUSTOM_TANGENT).
        type:             optional point type LINEAR|CURVE|CURVE_CLAMPED|CURVE_CUSTOM_TANGENT|CONSTANT.
        coordinate_space: 'world' (default) or 'local' for the location + tangents.

        Ledgered write (op 'set_spline_point'): captures the PRIOR single point (location, tangents,
        type) so `undo` restores exactly that point."""
        params = {"actor_name": actor_name, "index": index, "component_name": component_name,
                  "location": location, "arrive_tangent": arrive_tangent,
                  "leave_tangent": leave_tangent, "type": type, "coordinate_space": coordinate_space}
        try:
            return json.dumps(_exec(_SET_POINT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_spline — READ: points, tangents, types, length, sampled xforms  #
    # ------------------------------------------------------------------ #
    _GET_SPLINE_BODY = _SPL_PREFIX + r'''
actor = _spl_resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _spl_find(actor, PARAMS.get("component_name"))
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no SplineComponent named %r on actor (splines: %s)" % (PARAMS.get("component_name"), _spl_list_names(actor))}))
    else:
        cs = _spl_cs(PARAMS.get("coordinate_space") or "world")
        csname = "local" if cs == unreal.SplineCoordinateSpace.LOCAL else "world"
        n = comp.get_number_of_spline_points()
        pts = [dict(_spl_capture_point(comp, i, cs)) for i in range(n)]
        length = comp.get_spline_length()
        nsamp = int(PARAMS.get("samples") or 0)
        samples = []
        if nsamp > 0 and length > 0:
            for s in range(nsamp):
                d = (length * s) / float(nsamp - 1) if nsamp > 1 else 0.0
                t = comp.get_transform_at_distance_along_spline(d, cs, False)
                loc = t.translation; rot = t.rotation.rotator()
                samples.append({"distance": round(d, 2),
                    "location": [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)],
                    "rotation": [round(rot.pitch, 2), round(rot.yaw, 2), round(rot.roll, 2)]})
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
            "component": comp.get_name(), "coordinate_space": csname,
            "num_points": n, "num_segments": comp.get_number_of_spline_segments(),
            "spline_length": round(length, 3), "closed_loop": comp.is_closed_loop(),
            "points": pts, "sampled_transforms": samples,
            "all_spline_components": _spl_list_names(actor)}))
'''

    @mcp.tool()
    def get_spline(ctx, actor_name: str, component_name: str = None,
                   coordinate_space: str = "world", samples: int = 0) -> str:
        """READ a USplineComponent: point count/segments, per-point location+tangents+type, spline
        length, closed-loop flag, and optional evenly-sampled transforms along the curve.

        actor_name:       actor label or unique name that owns the spline.
        component_name:   name of the SplineComponent (default: the actor's first SplineComponent).
        coordinate_space: 'world' (default) or 'local' for reported locations/tangents/samples.
        samples:          if > 0, return that many transforms evenly spaced by distance along the
                          spline (endpoints inclusive).

        Read-only (not ledgered)."""
        params = {"actor_name": actor_name, "component_name": component_name,
                  "coordinate_space": coordinate_space, "samples": samples}
        try:
            return json.dumps(_exec(_GET_SPLINE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_spline — READ: structural health                           #
    # ------------------------------------------------------------------ #
    _VALIDATE_SPLINE_BODY = _SPL_PREFIX + r'''
actor = _spl_resolve_actor(PARAMS["actor_name"])
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % PARAMS["actor_name"]}))
else:
    comp = _spl_find(actor, PARAMS.get("component_name"))
    if comp is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no SplineComponent named %r on actor (splines: %s)" % (PARAMS.get("component_name"), _spl_list_names(actor))}))
    else:
        cs = unreal.SplineCoordinateSpace.WORLD
        n = comp.get_number_of_spline_points()
        length = comp.get_spline_length()
        issues = []
        warnings = []
        if n < 2:
            issues.append("spline has fewer than 2 points (%d) -- no drawable curve" % n)
        if length <= 0.0 and n >= 2:
            issues.append("spline length is zero despite %d points (all-coincident?)" % n)
        # zero-length segments (coincident consecutive points)
        zero_segs = []
        prev = None
        for i in range(n):
            loc = comp.get_location_at_spline_point(i, cs)
            if prev is not None:
                dx = loc.x - prev[0]; dy = loc.y - prev[1]; dz = loc.z - prev[2]
                if (dx*dx + dy*dy + dz*dz) < 1e-6:
                    zero_segs.append(i)
            prev = [loc.x, loc.y, loc.z]
        if zero_segs:
            warnings.append("coincident points at indices %s (zero-length segments)" % zero_segs)
        # non-finite tangent check
        bad_tan = []
        for i in range(n):
            at = comp.get_arrive_tangent_at_spline_point(i, cs)
            for v in (at.x, at.y, at.z):
                if v != v or v in (float("inf"), float("-inf")):
                    bad_tan.append(i); break
        if bad_tan:
            issues.append("non-finite arrive-tangent at indices %s" % bad_tan)
        type_hist = {}
        for i in range(n):
            tn = _spl_type_name(comp.get_spline_point_type(i))
            type_hist[tn] = type_hist.get(tn, 0) + 1
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": actor.get_name(),
            "component": comp.get_name(), "num_points": n,
            "spline_length": round(length, 3), "closed_loop": comp.is_closed_loop(),
            "point_type_histogram": type_hist,
            "valid": (len(issues) == 0), "issues": issues, "warnings": warnings}))
'''

    @mcp.tool()
    def validate_spline(ctx, actor_name: str, component_name: str = None) -> str:
        """READ / health-check a USplineComponent: flags <2 points, zero total length, coincident
        (zero-length) segments, and non-finite tangents; reports a point-type histogram.

        actor_name:     actor label or unique name that owns the spline.
        component_name: name of the SplineComponent (default: the actor's first SplineComponent).

        Read-only (not ledgered). 'valid' is False when any hard issue is present."""
        params = {"actor_name": actor_name, "component_name": component_name}
        try:
            return json.dumps(_exec(_VALIDATE_SPLINE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
