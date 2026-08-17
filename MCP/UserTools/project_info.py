"""UserTools :: Project / Engine Info  (spec: docs/spec/project.md)

Clean-room, READ-ONLY introspection of the project + engine + plugins + input +
project settings, over Unreal's public Python API (UE 5.8). Executed in the editor's
interpreter via the plugin's `execute_python` handler; results captured from stdout.

Query convention (copied verbatim from editor_level.py / editor_query.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it, so stray engine log lines can't corrupt the payload. Every snippet is wrapped
with Output-Log delta capture, surfacing new Warning/Error lines as result['_log_warnings'].

Commands (all READ-ONLY; no writes, no ledger mutation, no modals):
  - get_project_info      — project name/id/dirs/.uproject + engine version + is-installed
  - get_engine_info       — engine version/branch/dirs + build config + reachable flags
  - list_plugins          — ENABLED plugins via PluginBlueprintLibrary (+ version/desc/dir)
  - list_input_mappings   — Enhanced Input assets (IA/IMC) via AssetRegistry + legacy fallback
  - get_project_settings  — reflect a settings CDO's Config properties (MCPReflectionLibrary)

Honest limitations discovered live (UE 5.8, this from-source build):
  - There is NO `unreal.GeneralProjectSettings` class exposed to Python, so the project's
    display name / company / description / version are read from Config/DefaultGame.ini
    (section [/Script/EngineSettings.GeneralProjectSettings]) as a FALLBACK. The engine
    MODULE name is unreal.SystemLibrary.get_game_name().
  - `unreal.Paths` has no is_installed_build / engine_installed. is-installed is derived
    from the presence of <Engine>/Build/InstalledBuild.txt (source builds lack it).
  - PluginBlueprintLibrary.get_enabled_plugin_names() lists ENABLED plugins only; disabled-
    but-available plugins are not enumerable from Python (stated in the payload).
  - Settings CDOs expose NO Python getset descriptors, so property NAMES come from
    unreal.MCPReflectionLibrary.get_object_property_metadata_json(cdo); values via
    get_editor_property. Opaque structs (e.g. FSoftObjectPath) serialize to a repr string.
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
    # Session id identifies THIS reader process (kept for parity with the other modules;
    # this module is pure-read and never touches any ledger). One value per OS process.
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
        the body in Unreal, and return its MARKER payload."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (no ''' / no backslashes). _full() makes a relative
    # engine path absolute; _ini_section() parses one [section] out of a config file.
    _INFO_HELPERS = r'''
import unreal, os, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _full(p):
    try: return unreal.Paths.convert_relative_path_to_full(p) if p else p
    except Exception: return p
def _read_ini_section(path, section):
    out = {}
    try:
        if not os.path.exists(path):
            return out
        cur = None
        for raw in open(path, "r", encoding="utf-8", errors="replace").read().splitlines():
            ln = raw.strip()
            if not ln or ln.startswith(";"):
                continue
            if ln.startswith("[") and ln.endswith("]"):
                cur = ln[1:-1]
                continue
            if cur == section and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out
'''

    # ------------------------------------------------------------------ #
    # get_project_info — project identity, dirs, engine version           #
    # ------------------------------------------------------------------ #
    _PROJECT_INFO_BODY = _INFO_HELPERS + r'''
info = {"status": "success"}
info["module_name"] = _try(lambda: unreal.SystemLibrary.get_game_name())
info["project_file"] = _full(_try(lambda: unreal.Paths.get_project_file_path()))
info["project_file_is_set"] = _try(lambda: bool(unreal.Paths.is_project_file_path_set()))
info["directories"] = {
    "project_dir":   _full(_try(lambda: unreal.Paths.project_dir())),
    "content_dir":   _full(_try(lambda: unreal.Paths.project_content_dir())),
    "config_dir":    _full(_try(lambda: unreal.Paths.project_config_dir())),
    "saved_dir":     _full(_try(lambda: unreal.Paths.project_saved_dir())),
    "log_dir":       _full(_try(lambda: unreal.Paths.project_log_dir())),
    "intermediate_dir": _full(_try(lambda: unreal.Paths.project_intermediate_dir())),
    "plugins_dir":   _full(_try(lambda: unreal.Paths.project_plugins_dir())),
}
info["engine_version"] = _try(lambda: unreal.SystemLibrary.get_engine_version())
info["engine_dir"] = _full(_try(lambda: unreal.Paths.engine_dir()))
info["engine_root_dir"] = _full(_try(lambda: unreal.Paths.root_dir()))
info["build_configuration"] = _try(lambda: unreal.SystemLibrary.get_build_configuration())
info["build_version"] = _try(lambda: unreal.SystemLibrary.get_build_version())
# is-installed (engine): no Python API -> derive from the InstalledBuild.txt marker.
_eng = info["engine_dir"] or ""
_marker = os.path.join(_eng, "Build", "InstalledBuild.txt")
info["is_installed_build"] = _try(lambda: os.path.exists(_marker), None)
info["is_installed_build_source"] = "presence of <Engine>/Build/InstalledBuild.txt (no Python API for this)"
# Configured project identity (display name/company/etc.) -> DefaultGame.ini fallback,
# because unreal.GeneralProjectSettings is NOT exposed to the Python API in 5.8.
_cfg = info["directories"]["config_dir"] or ""
_gp = _read_ini_section(os.path.join(_cfg, "DefaultGame.ini"), "/Script/EngineSettings.GeneralProjectSettings")
info["configured_project"] = {
    "project_name":  _gp.get("ProjectName"),
    "project_id":    _gp.get("ProjectID"),
    "company_name":  _gp.get("CompanyName"),
    "description":   _gp.get("Description"),
    "project_version": _gp.get("ProjectVersion"),
    "homepage":      _gp.get("Homepage"),
}
info["configured_project_source"] = ("Config/DefaultGame.ini [/Script/EngineSettings.GeneralProjectSettings] "
    "(unreal.GeneralProjectSettings is not exposed to Python in 5.8; keys absent from the ini are null)")
info["note"] = ("module_name is the engine/game module (unreal.SystemLibrary.get_game_name()); "
    "the human-facing name is configured_project.project_name from DefaultGame.ini.")
print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_project_info(ctx) -> str:
        """Project identity + on-disk layout + engine version. Read-only.

        Returns the engine module name (get_game_name), the absolute .uproject path,
        the absolute project directories (project/content/config/saved/log/intermediate/
        plugins), the engine version string, engine + root dirs, build configuration and
        version, and whether this is an Installed (binary) engine build.

        The human-facing project name/company/description/version come from
        Config/DefaultGame.ini ([/Script/EngineSettings.GeneralProjectSettings]) because
        unreal.GeneralProjectSettings is not exposed to the Python API in UE 5.8 — the
        payload documents this under 'configured_project_source'. Keys not present in the
        ini are returned as null."""
        try:
            return json.dumps(_query(_PROJECT_INFO_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_engine_info — engine version/branch/dirs + reachable flags      #
    # ------------------------------------------------------------------ #
    _ENGINE_INFO_BODY = _INFO_HELPERS + r'''
info = {"status": "success"}
ver = _try(lambda: unreal.SystemLibrary.get_engine_version()) or ""
info["engine_version"] = ver
# Parse "5.8.1-0+UE5" -> {major,minor,patch,changelist,branch}
parsed = {"major": None, "minor": None, "patch": None, "changelist": None, "branch": None}
try:
    core = ver
    if "+" in core:
        core, parsed["branch"] = core.split("+", 1)
    if "-" in core:
        core, parsed["changelist"] = core.split("-", 1)
    bits = core.split(".")
    if len(bits) > 0 and bits[0].isdigit(): parsed["major"] = int(bits[0])
    if len(bits) > 1 and bits[1].isdigit(): parsed["minor"] = int(bits[1])
    if len(bits) > 2 and bits[2].isdigit(): parsed["patch"] = int(bits[2])
except Exception:
    pass
info["version_parts"] = parsed
info["build_configuration"] = _try(lambda: unreal.SystemLibrary.get_build_configuration())
info["build_version"] = _try(lambda: unreal.SystemLibrary.get_build_version())
info["directories"] = {
    "engine_dir":         _full(_try(lambda: unreal.Paths.engine_dir())),
    "engine_content_dir": _full(_try(lambda: unreal.Paths.engine_content_dir())),
    "engine_root_dir":    _full(_try(lambda: unreal.Paths.root_dir())),
    "launch_dir":         _full(_try(lambda: unreal.Paths.launch_dir())),
}
_eng = info["directories"]["engine_dir"] or ""
_marker = os.path.join(_eng, "Build", "InstalledBuild.txt")
info["is_installed_build"] = _try(lambda: os.path.exists(_marker), None)
# Reachable "feature" facts (what Python CAN tell us about this editor/runtime).
flags = {}
flags["with_editor"] = _try(lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem) is not None, None)
ues = _try(lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem))
flags["is_play_in_editor"] = _try(lambda: bool(ues.get_editor_world().is_play_in_editor()) if ues else None)
flags["python_plugin_enabled"] = _try(lambda: "PythonScriptPlugin" in list(unreal.PluginBlueprintLibrary.get_enabled_plugin_names()), None)
flags["enabled_plugin_count"] = _try(lambda: len(list(unreal.PluginBlueprintLibrary.get_enabled_plugin_names())), None)
info["reachable_flags"] = flags
info["limitations"] = ("Compile-time engine feature macros (WITH_EDITOR, WITH_EDITORONLY_DATA, "
    "UE_BUILD_SHIPPING, target platform set, etc.) are NOT exposed to the Python API in 5.8; "
    "only the runtime facts under 'reachable_flags' and the version/build strings are obtainable.")
print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_engine_info(ctx) -> str:
        """Engine version, branch, directories, and reachable runtime flags. Read-only.

        Returns the raw engine version string plus a parsed breakdown
        (major/minor/patch/changelist/branch), the build configuration and version, the
        engine/content/root/launch directories, whether this is an Installed build, and a
        'reachable_flags' block (with_editor, PIE state, python-plugin enabled, enabled
        plugin count). Compile-time feature macros are not exposed to Python — this is
        stated under 'limitations'."""
        try:
            return json.dumps(_query(_ENGINE_INFO_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_plugins — ENABLED plugins via PluginBlueprintLibrary            #
    # ------------------------------------------------------------------ #
    _LIST_PLUGINS_BODY = _INFO_HELPERS + r'''
name_filter = (PARAMS.get("name_filter") or "").lower()
details = bool(PARAMS.get("details"))
scope = (PARAMS.get("scope") or "all").lower()   # all | engine | project
page = max(1, int(PARAMS.get("page") or 1))
page_size = max(1, int(PARAMS.get("page_size") or 50))
pbl = unreal.PluginBlueprintLibrary
names = _try(lambda: list(pbl.get_enabled_plugin_names()), []) or []
_engine_full = _full(unreal.Paths.engine_dir()) or ""
def _classify(base):
    b = (base or "")
    bf = _full(b) or b
    return "engine" if _engine_full and bf.startswith(_engine_full) else "project"
rows = []
for nm in names:
    base = _try(lambda nm=nm: pbl.get_plugin_base_dir(nm))
    loc = _classify(base)
    if scope in ("engine", "project") and loc != scope:
        continue
    if name_filter and name_filter not in nm.lower():
        continue
    rec = {"name": nm, "location": loc}
    if details:
        rec["version_name"] = _try(lambda nm=nm: pbl.get_plugin_version_name(nm))
        rec["version"] = _try(lambda nm=nm: pbl.get_plugin_version(nm))
        rec["description"] = _try(lambda nm=nm: pbl.get_plugin_description(nm))
        rec["descriptor_file"] = _full(_try(lambda nm=nm: pbl.get_plugin_descriptor_file_path(nm)))
        rec["base_dir"] = _full(base)
        rec["content_mounted"] = _try(lambda nm=nm: bool(pbl.is_plugin_mounted(nm)))
    rows.append(rec)
rows.sort(key=lambda r: r["name"].lower())
total = len(rows)
start = (page - 1) * page_size
window = rows[start:start + page_size]
nxt = page + 1 if start + page_size < total else None
by_loc = {}
for r in rows:
    by_loc[r["location"]] = by_loc.get(r["location"], 0) + 1
result = {"status": "success",
          "api": "unreal.PluginBlueprintLibrary.get_enabled_plugin_names()",
          "scope": scope, "name_filter": PARAMS.get("name_filter"),
          "total_enabled_matching": total, "by_location": by_loc,
          "page": page, "page_size": page_size, "returned": len(window), "next_page": nxt,
          "plugins": window,
          "limitation": ("Lists ENABLED plugins only. The Python API exposes no enumeration of "
              "disabled-but-available plugins, so those are not included. 'location' engine vs "
              "project is derived from each plugin's base dir relative to the engine dir.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_plugins(ctx, name_filter: str = None, details: bool = False,
                     scope: str = "all", page: int = 1, page_size: int = 50) -> str:
        """List ENABLED plugins in this project/engine. Read-only.

        Uses unreal.PluginBlueprintLibrary.get_enabled_plugin_names(). Each entry has the
        plugin name and 'location' (engine vs project, derived from its base dir). With
        details=True, also returns version_name, numeric version, description, absolute
        descriptor (.uplugin) path, base dir, and whether its content is mounted.

        name_filter: case-insensitive substring on the plugin name.
        scope:       'all' (default), 'engine', or 'project'.
        page/page_size: paginate the (name-sorted) list; response returns 'next_page' or null.

        Limitation: only ENABLED plugins are enumerable from Python — disabled-but-available
        plugins are not (stated in the payload)."""
        params = {"name_filter": name_filter, "details": details, "scope": scope,
                  "page": page, "page_size": page_size}
        try:
            return json.dumps(_exec(_LIST_PLUGINS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_input_mappings — Enhanced Input assets + legacy fallback        #
    # ------------------------------------------------------------------ #
    _LIST_INPUT_BODY = _INFO_HELPERS + r'''
search_path = PARAMS.get("search_path") or "/Game"
include_paths = bool(PARAMS.get("include_paths") if PARAMS.get("include_paths") is not None else True)
max_paths = int(PARAMS.get("max_paths") or 200)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
def _enum(class_name):
    try:
        f = unreal.ARFilter(class_names=[class_name], recursive_paths=True,
                            package_paths=[search_path])
        assets = ar.get_assets(f) or []
        return [str(a.package_name) for a in assets]
    except Exception as e:
        return []
ia = _enum("InputAction")
imc = _enum("InputMappingContext")
# Enhanced Input plugin enabled?
ei_enabled = _try(lambda: "EnhancedInput" in list(unreal.PluginBlueprintLibrary.get_enabled_plugin_names()), None)
# Legacy input (project-wide action/axis mappings on UInputSettings CDO)
legacy = {"action_mapping_count": None, "axis_mapping_count": None,
          "action_mappings": [], "axis_mappings": []}
try:
    ins = unreal.get_default_object(unreal.InputSettings)
    ams = ins.get_editor_property("action_mappings") or []
    axs = ins.get_editor_property("axis_mappings") or []
    legacy["action_mapping_count"] = len(ams)
    legacy["axis_mapping_count"] = len(axs)
    for m in list(ams)[:max_paths]:
        legacy["action_mappings"].append({
            "name": _try(lambda m=m: str(m.get_editor_property("action_name"))),
            "key":  _try(lambda m=m: str(m.get_editor_property("key").get_editor_property("key_name")))})
    for m in list(axs)[:max_paths]:
        legacy["axis_mappings"].append({
            "name":  _try(lambda m=m: str(m.get_editor_property("axis_name"))),
            "key":   _try(lambda m=m: str(m.get_editor_property("key").get_editor_property("key_name"))),
            "scale": _try(lambda m=m: m.get_editor_property("scale"))})
except Exception:
    pass
enhanced_present = (len(ia) + len(imc)) > 0
legacy_present = bool((legacy["action_mapping_count"] or 0) or (legacy["axis_mapping_count"] or 0))
if enhanced_present and not legacy_present:
    system = "enhanced_input"
elif legacy_present and not enhanced_present:
    system = "legacy_input"
elif enhanced_present and legacy_present:
    system = "both"
else:
    system = "none_found"
result = {"status": "success",
          "input_system_in_use": system,
          "enhanced_input_plugin_enabled": ei_enabled,
          "search_path": search_path,
          "enhanced_input": {
              "input_action_count": len(ia),
              "input_mapping_context_count": len(imc),
              "input_actions": (sorted(ia)[:max_paths] if include_paths else "omitted (include_paths=False)"),
              "input_mapping_contexts": (sorted(imc)[:max_paths] if include_paths else "omitted (include_paths=False)")},
          "legacy_input": legacy,
          "note": ("Enhanced Input assets are enumerated from the AssetRegistry (InputAction / "
              "InputMappingContext) under search_path. Legacy action/axis mappings are read from "
              "the UInputSettings CDO (project-wide Config/DefaultInput.ini). 'input_system_in_use' "
              "reports which system actually has content.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_input_mappings(ctx, search_path: str = "/Game", include_paths: bool = True,
                            max_paths: int = 200) -> str:
        """List the project's input mappings, detecting Enhanced vs legacy input. Read-only.

        Enhanced Input: enumerates InputAction (IA_*) and InputMappingContext (IMC_*) assets
        via the AssetRegistry under search_path, with counts and asset paths.
        Legacy Input: reads project-wide action/axis mappings from the UInputSettings CDO
        (Config/DefaultInput.ini), with each mapping's name and bound key.

        'input_system_in_use' is one of enhanced_input / legacy_input / both / none_found,
        based on which system actually has content.

        search_path:   AssetRegistry root to scan for IA/IMC assets (default '/Game').
        include_paths: include the full asset path lists (default True).
        max_paths:     cap the number of paths/mappings returned per list."""
        params = {"search_path": search_path, "include_paths": include_paths, "max_paths": max_paths}
        try:
            return json.dumps(_exec(_LIST_INPUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_project_settings — reflect a settings CDO (config properties)    #
    # ------------------------------------------------------------------ #
    # Friendly aliases -> the unreal.* class name whose CDO holds those config values.
    _SETTINGS_ALIASES = {
        "maps": "GameMapsSettings", "gamemaps": "GameMapsSettings",
        "input": "InputSettings", "physics": "PhysicsSettings",
        "engine": "Engine", "navigation": "NavigationSystemV1",
        "animation": "AnimationSettings", "audio": "AudioSettings",
        "rendering": "RendererSettings", "renderer": "RendererSettings",
        "collision": "CollisionProfile", "worldsettings": "WorldSettings",
        "gameplaytags": "GameplayTagsSettings", "assetmanager": "AssetManagerSettings",
        "cine": "CineCameraSettings",
    }
    _KNOWN_SECTIONS = sorted(set(_SETTINGS_ALIASES.values()))

    _PROJECT_SETTINGS_BODY = _INFO_HELPERS + r'''
ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 20)
config_only = bool(PARAMS.get("config_only") if PARAMS.get("config_only") is not None else True)
flt = (PARAMS.get("filter") or "").lower()
section = PARAMS.get("section")
known = PARAMS.get("known_sections") or []
def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        out = []
        for i, e in enumerate(v):
            if i >= ARRAY_LIMIT:
                out.append("...(%d more)" % (len(v) - ARRAY_LIMIT)); break
            out.append(_ser(e, depth))
        return out
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= ARRAY_LIMIT: break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    # Opaque struct (e.g. FSoftObjectPath / FSoftClassPath): no Python getset descriptors.
    return {"__struct__": type(v).__name__, "repr": str(v)}

if not section:
    print("@@UMCP@@" + json.dumps({"status": "success", "mode": "catalog",
        "note": ("No section given. Pass section=<name> (a friendly alias or an unreal.* "
            "settings class name) to reflect that settings object's config properties. "
            "Property NAMES come from unreal.MCPReflectionLibrary; values via get_editor_property."),
        "resolvable_sections": known,
        "aliases": PARAMS.get("aliases") or {}}))
else:
    # Resolve to a class: alias -> class, else the literal name.
    aliases = PARAMS.get("aliases") or {}
    clsname = aliases.get(section.lower(), section)
    cls = getattr(unreal, clsname, None)
    if cls is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no settings class 'unreal.%s' (resolved from section '%s')" % (clsname, section),
            "resolvable_sections": known}))
    else:
        cdo = _try(lambda: unreal.get_default_object(cls))
        if cdo is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "could not get default object (CDO) for unreal.%s" % clsname}))
        else:
            # Property metadata (names/cpp_type/category/flags/tooltip) via MCPReflectionLibrary,
            # since settings CDOs expose NO Python getset descriptors.
            meta = []
            try:
                mj = unreal.MCPReflectionLibrary.get_object_property_metadata_json(cdo)
                meta = json.loads(mj).get("properties", []) if mj else []
            except Exception:
                meta = []
            props = {}
            total = 0
            for pm in meta:
                pname = pm.get("name")
                flags = pm.get("flags") or []
                if config_only and "Config" not in flags:
                    continue
                if flt and flt not in (pname or "").lower():
                    continue
                total += 1
                # get_editor_property wants the snake_case python name; MCPReflectionLibrary
                # reports the C++ PascalCase name. Try the reported name, then a snake guess.
                val = "<unreadable>"
                snake = ""
                for ch in (pname or ""):
                    if ch.isupper() and snake and not snake.endswith("_"):
                        snake += "_"
                    snake += ch.lower()
                for cand in (pname, snake):
                    try:
                        val = _ser(cdo.get_editor_property(cand), 1); break
                    except Exception:
                        continue
                props[pname] = {"cpp_type": pm.get("cpp_type"), "category": pm.get("category"),
                                "flags": flags, "value": val}
            result = {"status": "success", "mode": "section",
                      "section": section, "resolved_class": clsname,
                      "cdo": str(cdo.get_path_name()),
                      "config_only": config_only, "returned": len(props),
                      "properties": props,
                      "note": ("Property names/cpp_type/category/flags come from "
                          "unreal.MCPReflectionLibrary.get_object_property_metadata_json(cdo); values "
                          "from get_editor_property. Opaque structs (e.g. FSoftObjectPath) serialize as "
                          "{__struct__, repr}. config_only=True limits to properties flagged 'Config'.")}
            if not meta:
                result["reflection_note"] = ("MCPReflectionLibrary returned no property metadata for this "
                    "class; nothing reflectable was found.")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_project_settings(ctx, section: str = None, config_only: bool = True,
                             filter: str = None, array_element_limit: int = 20) -> str:
        """Read a project/engine settings object's config properties by reflection. Read-only.

        Call with no section to get a catalog of resolvable section names. Call with a
        section (a friendly alias like 'maps', 'physics', 'input', 'rendering', or a raw
        unreal.* settings class name like 'GameMapsSettings') to reflect that settings CDO.

        For each property it returns cpp_type, category, UPROPERTY flags, and the current
        value (read via get_editor_property). Property NAMES are obtained from
        unreal.MCPReflectionLibrary (settings CDOs expose no Python getset descriptors).

        config_only:  if True (default), only properties flagged 'Config' (the ones that
                      persist to Default*.ini) are returned; False returns all reflected props.
        filter:       case-insensitive substring on property name.
        array_element_limit: cap elements shown per array/set/map.

        Opaque structs (e.g. FSoftObjectPath) serialize as {__struct__, repr}. Note: the
        classic GeneralProjectSettings is NOT a Python class in 5.8 — use get_project_info
        for the project name/company/description (read from DefaultGame.ini)."""
        params = {"section": section, "config_only": config_only, "filter": filter,
                  "array_element_limit": array_element_limit,
                  "aliases": _SETTINGS_ALIASES, "known_sections": _KNOWN_SECTIONS}
        try:
            return json.dumps(_exec(_PROJECT_SETTINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
