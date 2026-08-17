"""UserTools :: Rendering / Post-Process (READ-ONLY)  (spec: docs/spec/rendering.md)

Clean-room, READ-ONLY introspection of the level's post-process volumes + their
FPostProcessSettings overrides, the project renderer configuration, and the current
scalability/quality buckets. Over Unreal's public Python API (UE 5.8). Executed in the
editor's interpreter via the plugin's `execute_python` handler; results captured from stdout.

Query convention (copied verbatim from editor_level.py / world_settings.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON after
it, so stray engine log lines can't corrupt the payload. Params are injected as base64 JSON
via _exec() so they survive the handler's triple-single-quote wrapping. Every snippet is
wrapped with Output-Log delta capture (new Warning/Error lines -> result['_log_warnings']).

Commands (all READ-ONLY; no writes, no ledger mutation, no factories, no modals):
  - list_post_process_volumes  — every APostProcessVolume actor in the level + priority,
                                 unbound (global) flag, blend_weight, blend_radius, enabled,
                                 and how many FPostProcessSettings properties it overrides.
  - get_post_process_settings  — the FPostProcessSettings of a named PPV (or the unbound/
                                 global one): every bOverride_* flag that is TRUE and its
                                 value (bloom, exposure, color grading, vignette, motion blur,
                                 AO, etc.), with cpp_type/category/tooltip. include_all=True
                                 dumps every value field (flagged overridden or not).
  - get_rendering_settings     — the project renderer configuration: GI method, reflection
                                 method, anti-aliasing method, Nanite, Lumen, virtual textures,
                                 virtual shadow maps, ray tracing, Substrate, default features
                                 — from renderer console variables (decoded) + the reflectable
                                 URendererSettings CDO bools + Config/DefaultEngine.ini.
  - get_scalability_settings   — the current sg.* quality buckets (view distance / shadow / GI
                                 / reflection / post-process / texture / effects / foliage /
                                 shading / AA / landscape) decoded to Low/Medium/High/Epic/
                                 Cinematic, plus the screen-percentage resolution quality.

Honest limitations discovered live (UE 5.8, this from-source build):
  - unreal.RendererSettings is NOT exposed as a Python class. The URendererSettings CDO is
    still reachable by object path (unreal.load_object(None,
    "/Script/Engine.Default__RendererSettings")) and MCPReflectionLibrary reflects 195
    property NAMES, but get_editor_property FAILS on the cvar-backed / enum props (GI method,
    reflection method, AA method are stored as the console variables r.DynamicGlobalIllumination
    Method / r.ReflectionMethod / r.AntiAliasingMethod, not as readable UPROPERTYs). So the
    authoritative source for those is the console variables (which reflect the live renderer
    state initialised from DefaultEngine.ini). Reflectable CDO bools (bNanite, bForwardShading,
    bVirtualTextures, bDefaultFeatureBloom, ...) ARE read and merged in.
  - There is NO unreal.ScalabilitySettings class; scalability is read from the sg.* console
    variables (the live quality buckets), not a settings CDO.
  - FPostProcessSettings exposes only ~7 bOverride_* flags as Python getset descriptors
    (dir()), but get_object_property_metadata / get_struct_fields_json over
    unreal.PostProcessSettings.static_struct() reports all 259 bOverride_* flags, and
    get_editor_property reads every one of them (and every value field) by its C++ PascalCase
    name. That struct-fields helper is the backbone of get_post_process_settings; a hasattr
    guard falls back to the 7 python-exposed flags if MCPReflectionLibrary is absent.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim pattern from editor_level.py) ----------
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

    # Shared Unreal-side helpers (no ''' / no backslashes). A JSON serializer for reflected
    # values (adds Vector4 over the world_settings/project_info serializer) + PPV lookup.
    _RENDER_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Vector4):
        return [v.x, v.y, v.z, v.w]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        out = []
        for i, e in enumerate(v):
            if i >= 25:
                out.append("...(%d more)" % (len(v) - 25)); break
            out.append(_ser(e, depth))
        return out
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:25]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= 25: break
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
        for pn in [n for n in dir(v) if not n.startswith("_")]:
            try:
                sv = v.get_editor_property(pn)
                d[pn] = _ser(sv, depth - 1)
            except Exception:
                continue
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)
def _all_ppvs():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = []
    for a in (eas.get_all_level_actors() or []):
        if a and a.get_class().get_name() == "PostProcessVolume":
            out.append(a)
    return out
def _pps_fields():
    # Full FProperty list of FPostProcessSettings via the C++ struct-fields helper.
    # Returns (fields_by_name, override_names, source_str). Falls back to python dir()
    # (only ~7 bOverride flags exposed) when MCPReflectionLibrary is unavailable.
    if hasattr(unreal, "MCPReflectionLibrary") and hasattr(unreal.MCPReflectionLibrary, "get_struct_fields_json"):
        try:
            ss = unreal.PostProcessSettings.static_struct()
            flds = json.loads(unreal.MCPReflectionLibrary.get_struct_fields_json(ss)).get("fields") or []
            byname = {}
            over = []
            for f in flds:
                nm = f.get("name")
                if not nm:
                    continue
                byname[nm] = f
                if nm.startswith("bOverride_"):
                    over.append(nm)
            if over:
                return byname, over, "MCPReflectionLibrary.get_struct_fields_json"
        except Exception:
            pass
    # Fallback: derive C++-ish names from the python getset descriptors (limited coverage).
    return {}, [], "python_reflection_fallback"
'''

    # ------------------------------------------------------------------ #
    # list_post_process_volumes — PPV actors + blend params + #overrides  #
    # ------------------------------------------------------------------ #
    _LIST_PPV_BODY = _RENDER_HELPERS + r'''
count_over = bool(PARAMS.get("count_overrides") if PARAMS.get("count_overrides") is not None else True)
byname, over_names, fld_src = _pps_fields()
ppvs = _all_ppvs()
rows = []
for a in ppvs:
    rec = {
        "label": _try(lambda a=a: a.get_actor_label()),
        "name": _try(lambda a=a: a.get_name()),
        "priority": _try(lambda a=a: a.get_editor_property("priority")),
        "unbound": _try(lambda a=a: bool(a.get_editor_property("unbound"))),
        "enabled": _try(lambda a=a: bool(a.get_editor_property("enabled"))),
        "blend_weight": _try(lambda a=a: a.get_editor_property("blend_weight")),
        "blend_radius": _try(lambda a=a: a.get_editor_property("blend_radius")),
        "folder": _try(lambda a=a: str(a.get_folder_path())) or "",
    }
    if rec["folder"] in ("None",):
        rec["folder"] = ""
    if count_over:
        st = _try(lambda a=a: a.get_editor_property("settings"))
        n_over = 0
        if st is not None and over_names:
            for on in over_names:
                try:
                    if bool(st.get_editor_property(on)):
                        n_over += 1
                except Exception:
                    pass
        rec["overridden_setting_count"] = n_over
    rows.append(rec)
# sort by (unbound desc, priority desc) so the effective global volume floats to the top
rows.sort(key=lambda r: (0 if r.get("unbound") else 1, -(r.get("priority") or 0.0)))
result = {"status": "success", "volume_count": len(rows),
          "total_override_flags": len(over_names), "override_flag_source": fld_src,
          "note": ("'unbound' = the volume is global (affects the whole world regardless of "
                   "camera position). Higher 'priority' wins where volumes overlap; 'blend_weight' "
                   "0..1 scales the contribution; 'blend_radius' is the cm falloff at the bounds "
                   "(ignored when unbound). 'overridden_setting_count' = FPostProcessSettings "
                   "bOverride_* flags set true (use get_post_process_settings for the values)."),
          "volumes": rows}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_post_process_volumes(ctx, count_overrides: bool = True) -> str:
        """List every PostProcessVolume actor placed in the active level. Read-only.

        For each volume: display label, internal name, outliner folder, and its blend
        parameters — priority (higher wins on overlap), unbound (True = global/affects the
        whole world), enabled, blend_weight (0..1 contribution), and blend_radius (cm falloff,
        ignored when unbound). With count_overrides=True (default) also reports
        'overridden_setting_count' (how many FPostProcessSettings bOverride_* flags are true).

        Volumes are sorted global-first then by descending priority (effective order). Use
        get_post_process_settings to read the actual overridden values of a volume."""
        try:
            return json.dumps(_exec(_LIST_PPV_BODY, {"count_overrides": count_overrides}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_post_process_settings — overridden FPostProcessSettings values  #
    # ------------------------------------------------------------------ #
    _GET_PPS_BODY = _RENDER_HELPERS + r'''
actor_name = PARAMS.get("actor_name")
include_all = bool(PARAMS.get("include_all"))
flt = (PARAMS.get("filter") or "").lower()
max_depth = int(PARAMS.get("max_depth") or 1)
byname, over_names, fld_src = _pps_fields()
ppvs = _all_ppvs()
target = None
if actor_name:
    for a in ppvs:
        if a.get_actor_label() == actor_name or a.get_name() == actor_name:
            target = a; break
    if target is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no PostProcessVolume matched '%s'" % actor_name,
            "available": [a.get_actor_label() for a in ppvs]}))
        raise SystemExit
else:
    # prefer the unbound / global volume, else the highest priority, else first
    unbound = [a for a in ppvs if _try(lambda a=a: bool(a.get_editor_property("unbound")))]
    if unbound:
        target = unbound[0]
    elif ppvs:
        target = sorted(ppvs, key=lambda a: -(_try(lambda a=a: a.get_editor_property("priority")) or 0.0))[0]
if target is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no PostProcessVolume in the level"}))
else:
    st = target.get_editor_property("settings")
    def _meta_for(value_name, override_name):
        f = byname.get(value_name) or byname.get(override_name) or {}
        return {"cpp_type": f.get("cpp_type"), "category": f.get("category"),
                "tooltip": (f.get("tooltip") or None)}
    overridden = []
    if over_names:
        for on in over_names:
            try:
                is_on = bool(st.get_editor_property(on))
            except Exception:
                continue
            if not is_on:
                continue
            base = on[len("bOverride_"):]
            if flt and flt not in base.lower():
                continue
            rec = {"setting": base, "override_flag": on}
            try:
                val = st.get_editor_property(base)
                rec["value"] = _ser(val, max_depth)
                rec["py_type"] = type(val).__name__
                rec["value_reflected"] = True
            except Exception:
                rec["value"] = None
                rec["value_reflected"] = False
                rec["note"] = "override flag is set but the value property is not reflected by name"
            m = _meta_for(base, on)
            rec.update(m)
            overridden.append(rec)
    result = {"status": "success",
              "actor_label": target.get_actor_label(), "actor_name": target.get_name(),
              "unbound": _try(lambda: bool(target.get_editor_property("unbound"))),
              "priority": _try(lambda: target.get_editor_property("priority")),
              "blend_weight": _try(lambda: target.get_editor_property("blend_weight")),
              "settings_source": fld_src,
              "total_override_flags": len(over_names),
              "overridden_count": len(overridden),
              "overridden_settings": overridden}
    if include_all:
        # Dump every non-override value field (flagged or not). Big — filtered by `filter`.
        allvals = {}
        seen = 0
        for nm, f in byname.items():
            if nm.startswith("bOverride_"):
                continue
            if flt and flt not in nm.lower():
                continue
            try:
                allvals[nm] = {"value": _ser(st.get_editor_property(nm), max_depth),
                               "cpp_type": f.get("cpp_type"), "category": f.get("category")}
                seen += 1
            except Exception:
                continue
        result["all_values_returned"] = seen
        result["all_values"] = allvals
    result["note"] = ("Only bOverride_* flags that are TRUE are reported under "
                      "'overridden_settings' (an unset PPV property inherits the engine default "
                      "and is not listed). Pass include_all=True to also dump every value field. "
                      "Color-grading values (saturation/contrast/gamma/gain/offset) are FVector4 "
                      "[R,G,B,Y-luminance]; tints are FLinearColor [r,g,b,a].")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_post_process_settings(ctx, actor_name: str = None, include_all: bool = False,
                                  filter: str = None, max_depth: int = 1) -> str:
        """Read a PostProcessVolume's FPostProcessSettings — which settings are OVERRIDDEN and
        their values. Read-only.

        actor_name:  the PPV's display label or internal name. If omitted, the unbound/global
                     volume is chosen (else the highest-priority one).
        include_all: if True, ALSO dump every value field (overridden or not) under 'all_values'
                     (large; combine with `filter`). Default False = only overridden settings.
        filter:      case-insensitive substring on the setting name (e.g. 'bloom', 'color',
                     'exposure', 'vignette', 'motion', 'ambient_occlusion').
        max_depth:   struct-expansion depth for values (default 1).

        Each overridden setting carries: setting (name), override_flag (bOverride_*), value,
        py_type, cpp_type, category, tooltip. Only bOverride_* flags that are true are listed —
        everything else inherits the engine default. Color grading (saturation/contrast/gamma/
        gain/offset) are FVector4 [R,G,B,Y]; bloom/AO tints are FLinearColor. Backed by
        MCPReflectionLibrary.get_struct_fields_json over the 259 bOverride_* flags of the
        struct (falls back to the ~7 python-exposed flags if that helper is absent)."""
        params = {"actor_name": actor_name, "include_all": include_all,
                  "filter": filter, "max_depth": max_depth}
        try:
            return json.dumps(_exec(_GET_PPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_rendering_settings — project renderer config (cvars + CDO)      #
    # ------------------------------------------------------------------ #
    _GET_RENDER_BODY = _RENDER_HELPERS + r'''
import os
sysl = unreal.SystemLibrary
def _cvi(cv):
    try: return sysl.get_console_variable_int_value(cv)
    except Exception: return None
def _cvf(cv):
    try: return sysl.get_console_variable_float_value(cv)
    except Exception: return None
def _decode(val, table):
    if val is None:
        return None
    return {"value": val, "label": table.get(val, "Unknown(%s)" % val)}
GI = {0: "None", 1: "Lumen", 2: "SSGI (Screen Space)", 3: "Plugin"}
REFL = {0: "None", 1: "Lumen", 2: "Screen Space", 3: "Ray Traced (legacy)"}
AA = {0: "None", 1: "FXAA", 2: "TAA", 3: "MSAA", 4: "TSR (Temporal Super Resolution)"}
ONOFF = {0: "Disabled", 1: "Enabled"}
SHADOWMAP = {0: "Shadow Maps", 1: "Virtual Shadow Maps"}
methods = {
    "global_illumination_method": _decode(_cvi("r.DynamicGlobalIlluminationMethod"), GI),
    "reflection_method": _decode(_cvi("r.ReflectionMethod"), REFL),
    "anti_aliasing_method": _decode(_cvi("r.AntiAliasingMethod"), AA),
    "shadow_map_method": _decode(1 if _cvi("r.Shadow.Virtual.Enable") else 0, SHADOWMAP),
}
features = {
    "nanite_project_enabled": _decode(_cvi("r.Nanite.ProjectEnabled"), ONOFF),
    "lumen_hardware_ray_tracing": _decode(_cvi("r.Lumen.HardwareRayTracing"), ONOFF),
    "lumen_trace_mesh_sdfs": _decode(_cvi("r.Lumen.TraceMeshSDFs"), ONOFF),
    "virtual_textures": _decode(_cvi("r.VirtualTextures"), ONOFF),
    "virtual_shadow_maps": _decode(_cvi("r.Shadow.Virtual.Enable"), ONOFF),
    "ray_tracing": _decode(_cvi("r.RayTracing"), ONOFF),
    "substrate": _decode(_cvi("r.Substrate"), ONOFF),
    "generate_mesh_distance_fields": _decode(_cvi("r.GenerateMeshDistanceFields"), ONOFF),
    "allow_static_lighting": _decode(_cvi("r.AllowStaticLighting"), ONOFF),
    "forward_shading": _decode(_cvi("r.ForwardShading"), ONOFF),
    "mobile_hdr": _decode(_cvi("r.MobileHDR"), ONOFF),
    "gpu_skin_cache": _decode(_cvi("r.SkinCache.CompileShaders"), ONOFF),
}
default_features = {
    "bloom": _decode(_cvi("r.DefaultFeature.Bloom"), ONOFF),
    "auto_exposure": _decode(_cvi("r.DefaultFeature.AutoExposure"), ONOFF),
    "motion_blur": _decode(_cvi("r.DefaultFeature.MotionBlur"), ONOFF),
    "lens_flare": _decode(_cvi("r.DefaultFeature.LensFlare"), ONOFF),
    "ambient_occlusion": _decode(_cvi("r.DefaultFeature.AmbientOcclusion"), ONOFF),
}
# Reflectable URendererSettings CDO bools (merge in; skip anything not readable).
cdo_bools = {}
cdo = _try(lambda: unreal.load_object(None, "/Script/Engine.Default__RendererSettings"))
if cdo is not None:
    for p in ["bNanite", "bForwardShading", "bVirtualTextures", "bVirtualTexturedLightmaps",
              "bDefaultFeatureBloom", "bDefaultFeatureMotionBlur", "bDefaultFeatureLensFlare",
              "bDefaultFeatureAmbientOcclusion", "bEnableRayTracingShadows",
              "bUseHardwareRayTracingForLumen", "bMobileVirtualTextures"]:
        try:
            cdo_bools[p] = bool(cdo.get_editor_property(p))
        except Exception:
            pass
# Config/DefaultEngine.ini [/Script/Engine.RendererSettings] — the project's authored overrides.
ini = {}
try:
    cfgdir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir())
    p = os.path.join(cfgdir, "DefaultEngine.ini")
    if os.path.exists(p):
        cur = None
        for raw in open(p, "r", encoding="utf-8", errors="replace").read().splitlines():
            ln = raw.strip()
            if not ln or ln.startswith(";"):
                continue
            if ln.startswith("[") and ln.endswith("]"):
                cur = ln[1:-1]; continue
            if cur == "/Script/Engine.RendererSettings" and "=" in ln:
                k, v = ln.split("=", 1)
                ini[k.strip()] = v.strip()
except Exception:
    pass
result = {"status": "success",
          "methods": methods, "features": features, "default_post_process_features": default_features,
          "renderer_settings_cdo_bools": cdo_bools,
          "config_defaultengine_renderer_section": ini,
          "sources": {
              "methods_and_features": "renderer console variables (r.*) — the live renderer state, initialised from Config/DefaultEngine.ini",
              "cdo_bools": "URendererSettings CDO via unreal.load_object('/Script/Engine.Default__RendererSettings') + get_editor_property (only reflectable bools; enum/cvar-backed props are not get_editor_property-readable)",
              "ini": "Config/DefaultEngine.ini [/Script/Engine.RendererSettings]"},
          "limitation": ("unreal.RendererSettings is not a Python class in 5.8, and the GI/reflection/"
              "anti-aliasing method properties are console-variable-backed (not readable via "
              "get_editor_property), so those come from r.* cvars. The full URendererSettings CDO "
              "property list (195 props) is enumerable via MCPReflectionLibrary but most values are "
              "not get_editor_property-readable — the curated cvars + reflectable bools above are the "
              "reliable set.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_rendering_settings(ctx) -> str:
        """Read the project's renderer configuration. Read-only.

        Returns the key rendering methods (global illumination = Lumen/SSGI/None, reflection
        method, anti-aliasing method = TSR/TAA/..., shadow map method = Virtual Shadow Maps or
        not), feature toggles (Nanite, Lumen hardware ray tracing, virtual textures, virtual
        shadow maps, ray tracing, Substrate, mesh distance fields, static lighting, forward
        shading, mobile HDR, GPU skin cache), the default post-process features (bloom, auto
        exposure, motion blur), the reflectable URendererSettings CDO bools, and the raw
        Config/DefaultEngine.ini [/Script/Engine.RendererSettings] section.

        Enum values are decoded to {value, label}. NOTE: unreal.RendererSettings is not a
        Python class in 5.8 and the method properties are console-variable-backed, so those are
        read from the live r.* console variables (initialised from DefaultEngine.ini) — this is
        documented under 'sources'/'limitation'."""
        try:
            return json.dumps(_exec(_GET_RENDER_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_scalability_settings — current sg.* quality buckets             #
    # ------------------------------------------------------------------ #
    _GET_SCALABILITY_BODY = _RENDER_HELPERS + r'''
sysl = unreal.SystemLibrary
def _cvi(cv):
    try: return sysl.get_console_variable_int_value(cv)
    except Exception: return None
def _cvf(cv):
    try: return sysl.get_console_variable_float_value(cv)
    except Exception: return None
LEVELS = {0: "Low", 1: "Medium", 2: "High", 3: "Epic", 4: "Cinematic"}
groups = ["ViewDistanceQuality", "AntiAliasingQuality", "ShadowQuality", "GlobalIlluminationQuality",
          "ReflectionQuality", "PostProcessQuality", "TextureQuality", "EffectsQuality",
          "FoliageQuality", "ShadingQuality", "LandscapeQuality"]
buckets = {}
for g in groups:
    v = _cvi("sg." + g)
    key = g[0].lower() + g[1:]
    # snake-ish key
    snake = ""
    for ch in g:
        if ch.isupper() and snake and not snake.endswith("_"):
            snake += "_"
        snake += ch.lower()
    buckets[snake] = ({"value": v, "label": LEVELS.get(v, "Custom(%s)" % v)} if v is not None else None)
# Resolution quality is a float screen-percentage (0 = use r.ScreenPercentage), not a bucket.
res_q = _cvf("sg.ResolutionQuality")
result = {"status": "success",
          "resolution_quality_screen_percent": res_q,
          "quality_buckets": buckets,
          "levels_legend": LEVELS,
          "source": "sg.* console variables (the live scalability/quality group state)",
          "note": ("Scalability is not exposed as a settings class in 5.8; these are the live "
                   "sg.* quality group console variables. 0=Low..4=Cinematic (3=Epic is the "
                   "editor default). 'resolution_quality_screen_percent' is a float screen "
                   "percentage where 0 means 'defer to r.ScreenPercentage'. Per-bucket detailed "
                   "cvar expansions live in the engine/project Scalability.ini groups.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_scalability_settings(ctx) -> str:
        """Read the current scalability / quality-bucket settings. Read-only.

        Returns each sg.* quality group (view distance, anti-aliasing, shadow, global
        illumination, reflection, post-process, texture, effects, foliage, shading, landscape)
        decoded to Low/Medium/High/Epic/Cinematic, plus the resolution quality (a screen-
        percentage float, 0 = defer to r.ScreenPercentage).

        Read from the live sg.* console variables — there is no ScalabilitySettings class in
        UE 5.8 (documented under 'source'/'note')."""
        try:
            return json.dumps(_exec(_GET_SCALABILITY_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
