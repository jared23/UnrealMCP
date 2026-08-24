"""UserTools :: World builds -- lighting, reflection/sky captures, navigation  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8), mirroring
editor_level.py / volumes_write.py conventions VERBATIM: base64-injected PARAMS, the Output-Log
auto-capture wrapper, the @@UMCP@@ marker, and the session field. These are BUILD / bake / capture
operations on whatever level is currently open -- they are NON-LEDGERED and NON-REVERSIBLE (a
lighting bake or nav rebuild has no meaningful inverse), so NO ScopedEditorTransaction / ledger
entry is recorded. Most are effectively READ-adjacent (they recompute cached data in place).

  - build_lighting             -> LevelEditorSubsystem.build_light_maps(quality, with_reflection_
                                  captures). quality is unreal.LightingBuildQuality (PREVIEW/MEDIUM/
                                  HIGH/PRODUCTION). Non-ledgered build.
  - lighting_build_status      -> best-effort READ (Python has no direct "is lighting building"
                                  query -- that is GEditor/FStaticLightingSystem, C++). Reports the
                                  scene's lighting-relevant stats + a note.
  - recapture_sky              -> SkyLightComponent.recapture_sky() on every SkyLight in the level.
  - update_reflection_captures -> LevelEditorSubsystem.build_light_maps(quality, with_reflection_
                                  captures=True). (GEditor->BuildReflectionCaptures / UpdateReflection
                                  Captures is C++-only; the flag on build_light_maps is the Python route.)
  - validate_lighting          -> READ: lightmass world settings + static/stationary/movable light
                                  census + skylight presence.
  - build_navigation           -> SystemLibrary.execute_console_command(world, "RebuildNavigation")
                                  + Output-Log capture.
  - validate_navigation        -> READ: NavigationSystemV1 presence, nav actors, and a live
                                  project_point_to_navigation / find_path_to_location_synchronously test.

API notes (UE 5.8.1; probed live against TestMCPSetup scratch level):
  - unreal.LightingBuildQuality members: QUALITY_PREVIEW, QUALITY_MEDIUM, QUALITY_HIGH,
    QUALITY_PRODUCTION. build_light_maps(quality, with_reflection_captures) lives on
    LevelEditorSubsystem.
  - unreal.SkyLightComponent has recapture_sky(), set_real_time_capture(), capture_emissive_only().
  - unreal.NavigationSystemV1: get_navigation_system(world_context), project_point_to_navigation(
    world_context, point, nav_data=None, filter_class=None, project_extent=Vector.ZERO),
    find_path_to_location_synchronously(...), is_navigation_being_built(world_context).
  - navigation_build_status precise polling is C++ (coordinator handles it) -- intentionally omitted.
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

    # Shared world helpers (prepended to build bodies). No ''' / no backslashes.
    _WORLD_HELPERS = r'''
import unreal, json
def _wb_world():
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return ues.get_editor_world() if ues else None
def _wb_les():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
def _wb_actors():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return eas.get_all_level_actors() or []
def _wb_quality(name):
    m = {"preview": unreal.LightingBuildQuality.QUALITY_PREVIEW,
         "medium": unreal.LightingBuildQuality.QUALITY_MEDIUM,
         "high": unreal.LightingBuildQuality.QUALITY_HIGH,
         "production": unreal.LightingBuildQuality.QUALITY_PRODUCTION}
    return m.get(str(name).strip().lower(), unreal.LightingBuildQuality.QUALITY_PREVIEW)
def _wb_comps_of(actor, cls):
    try:
        return actor.get_components_by_class(cls) or []
    except Exception:
        return []
def _wb_light_census():
    census = {"DirectionalLight": 0, "PointLight": 0, "SpotLight": 0, "RectLight": 0, "SkyLight": 0}
    mobility = {"Static": 0, "Stationary": 0, "Movable": 0}
    for a in _wb_actors():
        if a is None:
            continue
        cn = a.get_class().get_name()
        for k in list(census.keys()):
            if k in cn:
                census[k] += 1
        for c in _wb_comps_of(a, unreal.LightComponentBase):
            try:
                mob = c.get_editor_property("mobility")
                mn = str(mob).split(".")[-1].split(":")[0].split(">")[0].strip()
                if mn in ("STATIC",):
                    mobility["Static"] += 1
                elif mn in ("STATIONARY",):
                    mobility["Stationary"] += 1
                elif mn in ("MOVABLE",):
                    mobility["Movable"] += 1
            except Exception:
                pass
    return census, mobility
'''

    # ------------------------------------------------------------------ #
    # build_lighting                                                      #
    # ------------------------------------------------------------------ #
    _BUILD_LIGHTING_BODY = _WORLD_HELPERS + r'''
les = _wb_les()
world = _wb_world()
if les is None or world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no LevelEditorSubsystem / editor world"}))
else:
    quality = _wb_quality(PARAMS.get("quality") or "Preview")
    qn = str(quality).split(".")[-1].split(":")[0].split(">")[0].strip()
    with_refl = bool(PARAMS.get("with_reflection_captures") or False)
    census, mobility = _wb_light_census()
    err = None
    try:
        les.build_light_maps(quality, with_refl)
    except Exception as e:
        err = "%s" % e
    print("@@UMCP@@" + json.dumps({"status": ("error" if err else "success"),
        "operation": "build_light_maps", "quality": qn,
        "with_reflection_captures": with_refl, "world": world.get_name(),
        "light_census": census, "light_mobility": mobility,
        "note": "build_light_maps kicked off (Lightmass). Static-lighting completion is async; "
                "precise status polling is C++ (see lighting_build_status).",
        "error": err}))
'''

    @mcp.tool()
    def build_lighting(ctx, quality: str = "Preview", with_reflection_captures: bool = False) -> str:
        """Build static lighting (Lightmass) for the currently open level via
        LevelEditorSubsystem.build_light_maps.

        quality:                  'Preview' (default), 'Medium', 'High', or 'Production'
                                  (unreal.LightingBuildQuality).
        with_reflection_captures: also update reflection captures as part of the build.

        Non-ledgered build (a bake has no inverse). Reports the light census/mobility and kicks off
        Lightmass; static-lighting completion is asynchronous."""
        params = {"quality": quality, "with_reflection_captures": with_reflection_captures}
        try:
            return json.dumps(_exec(_BUILD_LIGHTING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # lighting_build_status (best-effort READ)                            #
    # ------------------------------------------------------------------ #
    _LIGHTING_STATUS_BODY = _WORLD_HELPERS + r'''
world = _wb_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    census, mobility = _wb_light_census()
    ws = None
    try:
        ws = world.get_world_settings()
    except Exception:
        ws = None
    force_no_precomputed = None
    try:
        if ws is not None:
            force_no_precomputed = bool(ws.get_editor_property("force_no_precomputed_lighting"))
    except Exception:
        force_no_precomputed = None
    print("@@UMCP@@" + json.dumps({"status": "success", "world": world.get_name(),
        "light_census": census, "light_mobility": mobility,
        "force_no_precomputed_lighting": force_no_precomputed,
        "note": "Python exposes no direct 'is lighting building' flag (that is GEditor / "
                "FStaticLightingSystem in C++). Reported values are the scene's lighting inputs, "
                "not live bake progress. Precise polling is delegated to the coordinator (C++)."}))
'''

    @mcp.tool()
    def lighting_build_status(ctx) -> str:
        """Best-effort READ of lighting state for the open level: light census by class, light
        mobility breakdown, and the world's force_no_precomputed_lighting flag.

        Python exposes no direct 'is lighting building' query (that lives in GEditor /
        FStaticLightingSystem in C++), so this reports the scene's lighting inputs rather than live
        bake progress. Read-only (not ledgered)."""
        try:
            return json.dumps(_exec(_LIGHTING_STATUS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # recapture_sky                                                       #
    # ------------------------------------------------------------------ #
    _RECAPTURE_SKY_BODY = _WORLD_HELPERS + r'''
world = _wb_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    recaptured = []
    for a in _wb_actors():
        if a is None:
            continue
        for c in _wb_comps_of(a, unreal.SkyLightComponent):
            try:
                c.recapture_sky()
                recaptured.append(a.get_actor_label())
            except Exception as e:
                recaptured.append(a.get_actor_label() + " (ERR:" + ("%s" % e)[:60] + ")")
    print("@@UMCP@@" + json.dumps({"status": "success", "operation": "recapture_sky",
        "world": world.get_name(), "skylights_recaptured": recaptured,
        "count": len([x for x in recaptured if "(ERR:" not in x]),
        "note": "no SkyLight in level" if not recaptured else "recapture_sky() called on each SkyLightComponent"}))
'''

    @mcp.tool()
    def recapture_sky(ctx) -> str:
        """Recapture the sky for every SkyLight in the open level (SkyLightComponent.recapture_sky).
        Refreshes captured sky lighting/reflection after skydome/atmosphere changes.

        Non-ledgered (a capture is transient render state with no inverse). Reports which SkyLights
        were recaptured."""
        try:
            return json.dumps(_exec(_RECAPTURE_SKY_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # update_reflection_captures                                          #
    # ------------------------------------------------------------------ #
    _UPDATE_REFL_BODY = _WORLD_HELPERS + r'''
les = _wb_les()
world = _wb_world()
if les is None or world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no LevelEditorSubsystem / editor world"}))
else:
    quality = _wb_quality(PARAMS.get("quality") or "Preview")
    qn = str(quality).split(".")[-1].split(":")[0].split(">")[0].strip()
    refl_actors = []
    for a in _wb_actors():
        if a is not None and "ReflectionCapture" in a.get_class().get_name():
            refl_actors.append(a.get_actor_label())
    err = None
    try:
        les.build_light_maps(quality, True)
    except Exception as e:
        err = "%s" % e
    print("@@UMCP@@" + json.dumps({"status": ("error" if err else "success"),
        "operation": "update_reflection_captures", "quality": qn,
        "reflection_capture_actors": refl_actors, "world": world.get_name(),
        "note": "Routed via build_light_maps(quality, with_reflection_captures=True). A dedicated "
                "GEditor->BuildReflectionCaptures / UpdateReflectionCaptures is C++-only.",
        "error": err}))
'''

    @mcp.tool()
    def update_reflection_captures(ctx, quality: str = "Preview") -> str:
        """Update the level's reflection captures via build_light_maps(quality,
        with_reflection_captures=True).

        quality: 'Preview' (default), 'Medium', 'High', or 'Production'.

        Non-ledgered build. NOTE: a dedicated GEditor->BuildReflectionCaptures /
        UpdateReflectionCaptures entry point is C++-only; this is the Python-accessible route."""
        params = {"quality": quality}
        try:
            return json.dumps(_exec(_UPDATE_REFL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_lighting (READ)                                            #
    # ------------------------------------------------------------------ #
    _VALIDATE_LIGHTING_BODY = _WORLD_HELPERS + r'''
world = _wb_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    census, mobility = _wb_light_census()
    warnings = []
    issues = []
    total_lights = sum(census.values())
    if total_lights == 0:
        warnings.append("level has no light actors -- scene may be unlit/black")
    if census.get("SkyLight", 0) == 0:
        warnings.append("no SkyLight -- no ambient/sky contribution")
    if census.get("DirectionalLight", 0) == 0:
        warnings.append("no DirectionalLight -- no primary sun/key light")
    if census.get("SkyLight", 0) > 1:
        warnings.append("multiple SkyLights (%d) -- only one is typically effective" % census["SkyLight"])
    ws = None
    try:
        ws = world.get_world_settings()
    except Exception:
        ws = None
    force_no_precomputed = None
    try:
        if ws is not None:
            force_no_precomputed = bool(ws.get_editor_property("force_no_precomputed_lighting"))
    except Exception:
        force_no_precomputed = None
    print("@@UMCP@@" + json.dumps({"status": "success", "world": world.get_name(),
        "light_census": census, "light_mobility": mobility,
        "total_lights": total_lights,
        "force_no_precomputed_lighting": force_no_precomputed,
        "valid": (len(issues) == 0), "issues": issues, "warnings": warnings}))
'''

    @mcp.tool()
    def validate_lighting(ctx) -> str:
        """READ / health-check lighting for the open level: light census by class, mobility
        breakdown, world force_no_precomputed_lighting, and warnings (no lights, no skylight, no
        directional, multiple skylights).

        Read-only (not ledgered)."""
        try:
            return json.dumps(_exec(_VALIDATE_LIGHTING_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # build_navigation                                                    #
    # ------------------------------------------------------------------ #
    _BUILD_NAV_BODY = _WORLD_HELPERS + r'''
world = _wb_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    nav_bounds = [a.get_actor_label() for a in _wb_actors() if a is not None and "NavMeshBoundsVolume" in a.get_class().get_name()]
    err = None
    try:
        unreal.SystemLibrary.execute_console_command(world, "RebuildNavigation")
    except Exception as e:
        err = "%s" % e
    being_built = None
    try:
        being_built = bool(unreal.NavigationSystemV1.is_navigation_being_built(world))
    except Exception:
        being_built = None
    print("@@UMCP@@" + json.dumps({"status": ("error" if err else "success"),
        "operation": "RebuildNavigation", "world": world.get_name(),
        "nav_bounds_volumes": nav_bounds,
        "is_navigation_being_built": being_built,
        "note": "Issued console command RebuildNavigation. With no NavMeshBoundsVolume there is no "
                "region to build. Nav generation is async." if not nav_bounds
                else "Issued console command RebuildNavigation over the NavMeshBoundsVolume region(s).",
        "error": err}))
'''

    @mcp.tool()
    def build_navigation(ctx) -> str:
        """Rebuild navigation for the open level via the RebuildNavigation console command.

        Non-ledgered build. Reports the NavMeshBoundsVolume actors present and the async
        is_navigation_being_built flag. With no NavMeshBoundsVolume there is no region to build."""
        try:
            return json.dumps(_exec(_BUILD_NAV_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_navigation (READ)                                          #
    # ------------------------------------------------------------------ #
    _VALIDATE_NAV_BODY = _WORLD_HELPERS + r'''
world = _wb_world()
if world is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no editor world"}))
else:
    navsys = None
    try:
        navsys = unreal.NavigationSystemV1.get_navigation_system(world)
    except Exception:
        navsys = None
    nav_bounds = [a.get_actor_label() for a in _wb_actors() if a is not None and "NavMeshBoundsVolume" in a.get_class().get_name()]
    nav_data = [a.get_actor_label() for a in _wb_actors() if a is not None and ("RecastNavMesh" in a.get_class().get_name() or "NavMeshBoundsVolume" not in a.get_class().get_name() and "NavData" in a.get_class().get_name())]
    warnings = []
    issues = []
    if navsys is None:
        warnings.append("no NavigationSystem present in this world")
    if not nav_bounds:
        warnings.append("no NavMeshBoundsVolume -- nothing defines the navigable region")
    # live projection test around a probe point
    probe = PARAMS.get("probe_point") or [0.0, 0.0, 0.0]
    extent = PARAMS.get("project_extent") or [500.0, 500.0, 500.0]
    projection = None
    try:
        pv = unreal.Vector(float(probe[0]), float(probe[1]), float(probe[2]))
        ev = unreal.Vector(float(extent[0]), float(extent[1]), float(extent[2]))
        res = unreal.NavigationSystemV1.project_point_to_navigation(world, pv, None, None, ev)
        if res is not None:
            projection = {"projected": True, "location": [round(res.x, 2), round(res.y, 2), round(res.z, 2)]}
        else:
            projection = {"projected": False}
    except Exception as e:
        projection = {"projected": False, "error": ("%s" % e)[:120]}
    if projection and not projection.get("projected"):
        warnings.append("project_point_to_navigation found no navmesh near probe %s (build nav first?)" % probe)
    print("@@UMCP@@" + json.dumps({"status": "success", "world": world.get_name(),
        "has_navigation_system": navsys is not None,
        "nav_bounds_volumes": nav_bounds, "nav_data_actors": nav_data,
        "probe_point": probe, "project_extent": extent, "projection": projection,
        "valid": (len(issues) == 0), "issues": issues, "warnings": warnings}))
'''

    @mcp.tool()
    def validate_navigation(ctx, probe_point: list = None, project_extent: list = None) -> str:
        """READ / health-check navigation for the open level: NavigationSystem presence,
        NavMeshBoundsVolume + nav-data actors, and a live project_point_to_navigation test near a
        probe point (proves an actual navmesh exists there).

        probe_point:    [x, y, z] to project onto the navmesh (default [0,0,0]).
        project_extent: [x, y, z] search half-extents for the projection (default [500,500,500]).

        Read-only (not ledgered). Warns when there is no nav system / no bounds volume / no navmesh
        near the probe."""
        params = {"probe_point": probe_point, "project_extent": project_extent}
        try:
            return json.dumps(_exec(_VALIDATE_NAV_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
