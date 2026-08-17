"""UserTools :: Navigation / Introspection  (spec: docs/spec/editor.md)

Clean-room, READ-ONLY navigation introspection over Unreal's public Python API
(subsystem-based, UE 5.8). Executed in the editor's interpreter via the plugin's
`execute_python` handler; results are captured from stdout.

Query convention (copied verbatim from editor_level.py / level_introspection.py): a
snippet prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the
JSON after it, so stray engine log lines can't corrupt the payload. Every snippet is
wrapped with Output-Log delta capture, surfacing new Warning/Error lines as
result['_log_warnings'].

Commands (all READ-ONLY — this module mutates NOTHING and NEVER rebuilds navigation):

  - list_nav_actors        — the level's navigation actors (NavMeshBoundsVolume,
                             RecastNavMesh nav data, NavModifierVolume, NavLinkProxy),
                             found via EditorActorSubsystem.get_all_level_actors() filtered
                             by class. Per-actor label/class/location/world-bounds.
  - get_navmesh_info       — for the level's RecastNavMesh actor (if present): reachable
                             navmesh settings (agent radius/height/max-slope, tile size,
                             tile pool) via get_editor_property, plus the combined
                             NavMeshBoundsVolume bounds (the navigable region). Honest about
                             what UE 5.8 Python cannot reach (protected cell size/height/step,
                             and 'is built / tile count' which has no Python getter).
  - get_nav_agent_config   — the project's navigation agent config from the settings CDOs
                             (NavigationSystemV1 + RecastNavMesh defaults + WorldSettings):
                             supported agents, agent radius/height/max-slope, and the world's
                             navigation-enabled flag. Defers honestly for values Python can't read.

Honest posture note: the stock Third Person template ships with NO navigation actors and no
built navmesh. list_nav_actors then returns a clean empty set; get_navmesh_info reports the
RecastNavMesh actor as absent (posture ai_read) while still surfacing any NavMeshBoundsVolume
region; get_nav_agent_config still works because it reads the project/settings CDOs, not level
actors. NOTHING here triggers a navigation build/rebuild (that is a mutation/compute).

READ-ONLY: no writes, no ledger, no asset/actor creation, no *CreationParameters, no factories,
no modal-popping APIs, no nav rebuild.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / level_introspection.py) ---
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
        the body in Unreal, and return its MARKER payload. Read-only: _session is carried
        for convention only (no ledger is ever written by this module)."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # The four canonical navigation actor classes we categorize. RecastNavMesh is the
    # generated nav-data actor; the other three are user-placed authoring actors.
    _NAV_CLASSES = ["NavMeshBoundsVolume", "RecastNavMesh", "NavModifierVolume", "NavLinkProxy"]

    # ------------------------------------------------------------------ #
    # list_nav_actors — navigation-related actors in the level (read)     #
    # ------------------------------------------------------------------ #
    _LIST_NAV_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
nav_class_names = PARAMS.get("nav_classes") or []
nav_class_set = set(nav_class_names)
# Resolve each target class to a Python type so subclasses (e.g. a project RecastNavMesh
# child) are also matched via isinstance, not only exact class-name equality.
target_types = []
for cn in nav_class_names:
    t = getattr(unreal, cn, None)
    if t is not None:
        target_types.append((cn, t))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
out = []
by_class = {}
for a in actors:
    if not a:
        continue
    cn = _try(lambda a=a: a.get_class().get_name()) or "?"
    matched = None
    if cn in nav_class_set:
        matched = cn
    else:
        for tname, ttype in target_types:
            if isinstance(a, ttype):
                matched = tname
                break
        # last-resort name heuristic for nav actors of classes not in the canonical list
        if matched is None and (cn.startswith("Nav") or "RecastNav" in cn):
            matched = cn
    if matched is None:
        continue
    loc = _try(lambda a=a: a.get_actor_location())
    b = _try(lambda a=a: a.get_actor_bounds(False))
    bounds = None
    if isinstance(b, tuple) and len(b) == 2:
        origin, extent = b
        bounds = {"origin": [round(origin.x, 2), round(origin.y, 2), round(origin.z, 2)],
                  "extent": [round(extent.x, 2), round(extent.y, 2), round(extent.z, 2)],
                  "min": [round(origin.x - extent.x, 2), round(origin.y - extent.y, 2), round(origin.z - extent.z, 2)],
                  "max": [round(origin.x + extent.x, 2), round(origin.y + extent.y, 2), round(origin.z + extent.z, 2)]}
    rec = {"label": _try(lambda a=a: a.get_actor_label()), "name": _try(lambda a=a: a.get_name()),
           "class": cn, "matched_as": matched,
           "location": ([round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)] if loc else None),
           "bounds": bounds}
    out.append(rec)
    by_class[matched] = by_class.get(matched, 0) + 1
result = {"status": "success", "nav_actor_count": len(out),
          "counts_by_class": by_class, "searched_classes": nav_class_names,
          "actors": out}
if not out:
    result["note"] = ("No navigation actors found in this level. The stock Third Person template "
                      "ships without a NavMeshBoundsVolume / RecastNavMesh / NavModifierVolume / "
                      "NavLinkProxy; a navmesh is only generated after a NavMeshBoundsVolume is "
                      "placed. This is a clean empty result, not an error.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_nav_actors(ctx) -> str:
        """List the navigation-related actors placed in the active level. Read-only.

        Scans EditorActorSubsystem.get_all_level_actors() and keeps actors that are (or
        subclass) one of the canonical navigation classes: NavMeshBoundsVolume (defines the
        region a navmesh is built for), RecastNavMesh (the generated nav-data actor),
        NavModifierVolume (marks areas as a nav area type / non-navigable), and NavLinkProxy
        (off-mesh links, e.g. jump/ladder connections). Each result carries label, internal
        name, class, matched_as (which canonical class it counted as), world location, and
        its world-space bounds (origin/extent/min/max) from get_actor_bounds().

        Returns a clean empty set (with an explanatory note) when the level has no navigation
        actors — the default for the Third Person template — rather than an error."""
        try:
            return json.dumps(_exec(_LIST_NAV_BODY, {"nav_classes": _NAV_CLASSES}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_navmesh_info — RecastNavMesh settings + navigable region (read) #
    # ------------------------------------------------------------------ #
    _NAVMESH_INFO_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _read(obj, prop):
    # Returns (value_or_None, status) where status is 'ok' | 'protected' | 'absent' | 'error'.
    try:
        return obj.get_editor_property(prop), "ok"
    except Exception as e:
        msg = str(e)
        if "protected" in msg:
            return None, "protected"
        if "Failed to find property" in msg:
            return None, "absent"
        return None, "error"
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
# --- the RecastNavMesh nav-data actor (generated when a navmesh is built) ---
recast = None
for a in actors:
    if a and isinstance(a, unreal.RecastNavMesh):
        recast = a
        break
# --- combined NavMeshBoundsVolume region (the navigable area) ---
nbv_mn = [None, None, None]
nbv_mx = [None, None, None]
nbv_count = 0
for a in actors:
    if not (a and isinstance(a, unreal.NavMeshBoundsVolume)):
        continue
    b = _try(lambda a=a: a.get_actor_bounds(False))
    if not (isinstance(b, tuple) and len(b) == 2):
        continue
    origin, extent = b
    o = [origin.x, origin.y, origin.z]; e = [extent.x, extent.y, extent.z]
    amn = [o[i] - e[i] for i in range(3)]; amx = [o[i] + e[i] for i in range(3)]
    for i in range(3):
        nbv_mn[i] = amn[i] if nbv_mn[i] is None else min(nbv_mn[i], amn[i])
        nbv_mx[i] = amx[i] if nbv_mx[i] is None else max(nbv_mx[i], amx[i])
    nbv_count += 1
if nbv_count:
    nbv_region = {"volume_count": nbv_count,
                  "min": [round(v, 2) for v in nbv_mn], "max": [round(v, 2) for v in nbv_mx],
                  "center": [round((nbv_mn[i] + nbv_mx[i]) / 2.0, 2) for i in range(3)],
                  "size": [round(nbv_mx[i] - nbv_mn[i], 2) for i in range(3)]}
else:
    nbv_region = {"volume_count": 0, "note": "no NavMeshBoundsVolume in this level; navigable region undefined"}
result = {"status": "success", "navmesh_bounds_region": nbv_region}
if recast is None:
    result["recast_navmesh"] = {
        "present": False, "posture": "ai_read",
        "note": ("No RecastNavMesh actor exists in this level (no navmesh has been built). "
                 "RecastNavMesh is auto-created when a NavMeshBoundsVolume is placed and "
                 "navigation is built; per-instance navmesh settings are only readable from "
                 "that actor. See get_nav_agent_config for the project/CDO default agent "
                 "settings, which ARE reachable without a built navmesh.")}
else:
    settings = {}
    prop_map = {"agent_radius": "agent_radius", "agent_height": "agent_height",
                "agent_max_slope": "agent_max_slope", "agent_max_step_height": "agent_max_step_height",
                "cell_size": "cell_size", "cell_height": "cell_height",
                "tile_size_uu": "tile_size_uu", "tile_pool_size": "tile_pool_size",
                "max_simplification_error": "max_simplification_error",
                "tile_number_hard_limit": "tile_number_hard_limit"}
    unreadable = {}
    for key, prop in prop_map.items():
        val, st = _read(recast, prop)
        if st == "ok":
            settings[key] = (str(val) if isinstance(val, unreal.EnumBase) else val)
        else:
            unreadable[key] = st
    rg, _st = _read(recast, "runtime_generation")
    if _st == "ok":
        settings["runtime_generation"] = str(rg)
    result["recast_navmesh"] = {
        "present": True, "posture": "ai_read",
        "label": _try(lambda: recast.get_actor_label()),
        "name": _try(lambda: recast.get_name()),
        "class": _try(lambda: recast.get_class().get_name()),
        "settings": settings, "unreadable_settings": unreadable,
        "build_state": {"reachable": False,
            "note": ("Whether the navmesh is built / how many tiles it has is NOT exposed to "
                     "UE 5.8 editor Python (RecastNavMesh exposes only k2_replace_area_in_tile_bounds; "
                     "no get_tile_count / is-built getter). Presence of this actor implies a build "
                     "has occurred at least once.")},
        "notes": ("cell_size / cell_height / agent_max_step_height are C++ 'protected' UPROPERTYs "
                  "and cannot be read via get_editor_property in this build (see unreadable_settings); "
                  "MCPReflectionLibrary exposes their metadata but not their values.")}
print("@@UMCP@@" + json.dumps(result, default=str))
'''

    @mcp.tool()
    def get_navmesh_info(ctx) -> str:
        """Report the active level's RecastNavMesh settings and its navigable region. Read-only.

        If a RecastNavMesh nav-data actor exists (i.e. a navmesh has been built), returns its
        reachable generation settings via get_editor_property: agent radius/height/max-slope,
        tile_size_uu, tile_pool_size, max_simplification_error, tile_number_hard_limit and
        runtime_generation. Values that are C++-protected in this build (cell_size, cell_height,
        agent_max_step_height) are listed under 'unreadable_settings' with the reason. Whether
        the mesh is built / its tile count is NOT reachable from UE 5.8 Python and is reported
        honestly under 'build_state'.

        Always also returns 'navmesh_bounds_region' — the combined AABB of all NavMeshBoundsVolume
        actors (the region a navmesh would cover), or a note if none are present.

        On the Third Person template (no navmesh built) the RecastNavMesh section reports
        present=false with posture 'ai_read' and points at get_nav_agent_config for the project
        default agent settings. This never triggers a navigation build."""
        try:
            return json.dumps(_query(_NAVMESH_INFO_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_nav_agent_config — project nav agent settings from CDOs (read)  #
    # ------------------------------------------------------------------ #
    _NAV_AGENT_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _read(obj, prop):
    try:
        return obj.get_editor_property(prop), "ok"
    except Exception as e:
        msg = str(e)
        if "protected" in msg:
            return None, "protected"
        if "Failed to find property" in msg:
            return None, "absent"
        return None, "error"
result = {"status": "success"}
# --- NavigationSystemV1 settings CDO (project-wide navigation system config) ---
navsys = _try(lambda: unreal.get_default_object(unreal.NavigationSystemV1))
ns = {}
if navsys is not None:
    for key in ["default_agent_name", "auto_create_navigation_data",
                "generate_navigation_only_around_navigation_invokers"]:
        val, st = _read(navsys, key)
        ns[key] = (str(val) if st == "ok" else ("<%s>" % st))
    agents, st = _read(navsys, "supported_agents")
    agent_list = []
    if st == "ok" and agents is not None:
        for ag in agents:
            entry = {}
            for f in ["name", "nav_agent_radius", "nav_agent_height", "agent_radius",
                      "agent_height", "agent_step_height", "nav_walking_search_height_scale"]:
                v, vst = _read(ag, f)
                if vst == "ok":
                    entry[f] = (str(v) if isinstance(v, unreal.EnumBase) else v)
            agent_list.append(entry)
    ns["supported_agents_count"] = len(agent_list)
    ns["supported_agents"] = agent_list
    if not agent_list:
        ns["supported_agents_note"] = ("supported_agents is empty on the settings CDO: the project "
                                       "uses the single implicit default agent whose dimensions come "
                                       "from the RecastNavMesh defaults below.")
result["navigation_system"] = ns
# --- RecastNavMesh default agent dimensions (the effective single-agent config) ---
recast_cdo = _try(lambda: unreal.get_default_object(unreal.RecastNavMesh))
agent = {}
unreadable = {}
if recast_cdo is not None:
    for key in ["agent_radius", "agent_height", "agent_max_slope", "agent_max_step_height",
                "cell_size", "cell_height", "tile_size_uu", "tile_pool_size"]:
        val, st = _read(recast_cdo, key)
        if st == "ok":
            agent[key] = (str(val) if isinstance(val, unreal.EnumBase) else val)
        else:
            unreadable[key] = st
result["default_agent"] = {"source": "RecastNavMesh class default object (CDO)",
                           "values": agent, "unreadable": unreadable}
if unreadable:
    result["default_agent"]["note"] = ("cell_size / cell_height / agent_max_step_height are C++ "
        "'protected' and cannot be read from Python in this build; the readable agent dimensions "
        "(radius/height/max_slope) reflect the project defaults, e.g. a standard humanoid agent.")
# --- WorldSettings navigation flags (level-scoped) ---
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = _try(lambda: ues.get_editor_world())
ws = _try(lambda: world.get_world_settings()) if world else None
world_nav = {}
if ws is not None:
    en, st = _read(ws, "enable_navigation_system")
    world_nav["enable_navigation_system"] = (en if st == "ok" else "<%s>" % st)
    cfg, st2 = _read(ws, "navigation_system_config")
    if st2 == "ok" and cfg is not None:
        world_nav["navigation_system_config_class"] = _try(lambda: cfg.get_class().get_name())
    else:
        world_nav["navigation_system_config_class"] = None
result["world_settings"] = world_nav
result["limitations"] = [
    "supported_agents on the NavigationSystemV1 CDO is empty here -> the project uses one implicit "
    "default agent; its dimensions are the RecastNavMesh defaults in 'default_agent'.",
    "cell_size / cell_height / agent_max_step_height are C++-protected UPROPERTYs and are not "
    "readable via get_editor_property in UE 5.8 Python (metadata-only via MCPReflectionLibrary).",
    "Per-agent overrides and ini-configured supported agents ([/Script/NavigationSystem...] in "
    "DefaultEngine.ini) beyond what the CDO surfaces are not parsed here."]
print("@@UMCP@@" + json.dumps(result, default=str))
'''

    @mcp.tool()
    def get_nav_agent_config(ctx) -> str:
        """Report the project's navigation agent configuration from the settings CDOs. Read-only.

        Reads three reachable sources without needing a built navmesh:
          - NavigationSystemV1 CDO (unreal.get_default_object): default_agent_name,
            auto_create_navigation_data, generate_navigation_only_around_navigation_invokers,
            and the supported_agents array (each agent's name + radius/height/step when present).
          - RecastNavMesh CDO defaults: the effective single-agent dimensions — agent_radius,
            agent_height, agent_max_slope (plus tile_size_uu / tile_pool_size). cell_size /
            cell_height / agent_max_step_height are C++-protected and reported under 'unreadable'.
          - WorldSettings: enable_navigation_system flag and the navigation_system_config class.

        When supported_agents is empty (the default) the project uses one implicit agent whose
        dimensions are the RecastNavMesh defaults, and that is stated explicitly. Includes an
        honest 'limitations' list. Nothing is mutated and no navigation build is triggered."""
        try:
            return json.dumps(_query(_NAV_AGENT_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"
