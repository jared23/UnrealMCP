"""UserTools :: Niagara / VFX discovery + runtime reads (READ2)  (spec: docs/spec/niagara.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). READ-ONLY. No Niagara/asset
editors are opened; nothing is mutated; no ledger. This is the DISCOVERY + RUNTIME-inspection wave,
the counterpart to niagara_read.py (which owns list_niagara_assets / get_niagara_system_info /
get_niagara_emitter_info). Query convention, base64 PARAMS, and Output-Log auto-capture are copied
verbatim from niagara_read.py / editor_level.py.

What IS reachable from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * list_niagara_asset_types    -- reflect the Niagara UClass tree (hasattr on unreal.<Class>) + count
                                   live instances per kind via AssetRegistry.
  * list_niagara_modules        -- AssetRegistry enumerates UNiagaraScript assets; the 'Usage' UPROPERTY
                                   is PROTECTED (get_editor_property refuses it) but the AssetRegistry
                                   TAG 'Usage' is readable via asset_data.get_tag_value('Usage') -- so
                                   Module scripts are filtered by tag WITHOUT loading each asset.
  * list_niagara_emitter_templates -- standalone UNiagaraEmitter assets ARE the template/parent emitters
                                   (in UE5 emitters owned by a system are not standalone assets), so this
                                   enumerates NiagaraEmitter assets across roots.
  * list_niagara_data_interfaces -- walk the unreal namespace for UNiagaraDataInterface subclasses
                                   (30 concrete DIs reachable via reflection here).
  * get_niagara_actors          -- EditorActorSubsystem.get_all_level_actors filtered to ANiagaraActor +
                                   any actor carrying a UNiagaraComponent (NOT find_all_object_of_class).
  * get_niagara_renderer_info   -- per-emitter renderer SUMMARY (materials + meshes + inferred renderer
                                   kinds) via NiagaraFunctionLibrary.get_all_emitters. Upgrades the prior
                                   PARTIAL from a flat system dump to a per-renderer structure.

Honest limits (reported in payloads, never hidden):
  * list_niagara_parameter_types -- FNiagaraTypeDefinition is a struct with NO Python enumeration of the
    registered type registry, so we enumerate the common Niagara parameter struct/class types via
    reflection (NiagaraPosition/NiagaraID/Vector/LinearColor/Quat/...) and SAY SO.
  * get_niagara_renderer_info -- the renderer UNiagaraRendererProperties objects live in versioned
    emitter data (FVersionedNiagaraEmitterData) and are NOT reachable from Python, so per-renderer
    PROPERTY values (sort mode, alignment, facing, sub-uv, ...) are editor-only; only the renderer's
    consumed materials/meshes + an inferred kind are surfaced.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never
assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _HELP = r'''
import unreal, json, warnings, gc
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _ser(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Array):
        return [_ser(x) for x in v]
    if isinstance(v, unreal.Object):
        return _try(lambda: v.get_path_name())
    if isinstance(v, unreal.Vector):
        return [round(v.x, 3), round(v.y, 3), round(v.z, 3)]
    return str(v)[:200]
def _ar():
    return unreal.AssetRegistryHelpers.get_asset_registry()
def _assets(kind, root):
    flt = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Niagara", kind)],
                          recursive_paths=True, package_paths=[root])
    return _ar().get_assets(flt) or []
'''

    # ------------------------------------------------------------------ #
    # list_niagara_asset_types                                            #
    # ------------------------------------------------------------------ #
    _ASSET_TYPES_BODY = _HELP + r'''
root = PARAMS.get("path") or "/Game"
# (python_class_name, asset_registry_class_name, module_path)
kinds = [
    ("NiagaraSystem", "NiagaraSystem", "/Script/Niagara"),
    ("NiagaraEmitter", "NiagaraEmitter", "/Script/Niagara"),
    ("NiagaraScript", "NiagaraScript", "/Script/Niagara"),
    ("NiagaraParameterCollection", "NiagaraParameterCollection", "/Script/Niagara"),
    ("NiagaraParameterCollectionInstance", "NiagaraParameterCollectionInstance", "/Script/Niagara"),
    ("NiagaraEffectType", "NiagaraEffectType", "/Script/Niagara"),
    ("NiagaraSimCache", "NiagaraSimCache", "/Script/Niagara"),
    ("NiagaraValidationRuleSet", "NiagaraValidationRuleSet", "/Script/Niagara"),
]
rows = []
for pyname, arname, mod in kinds:
    present = hasattr(unreal, pyname)
    count = None
    if present:
        try:
            count = len(_assets(arname, root))
        except Exception:
            count = None
    rows.append({"type": pyname, "python_class_available": bool(present),
                 "asset_class_path": mod + "." + arname, "instances_under_path": count})
print("@@UMCP@@" + json.dumps({"status": "success", "path": root, "count": len(rows),
    "asset_types": rows,
    "note": "python_class_available = unreal.<Class> exists via reflection; instances_under_path = "
            "AssetRegistry count under 'path' (None if not counted). Niagara classes live in "
            "/Script/Niagara."}))
gc.collect()
'''

    @mcp.tool()
    def list_niagara_asset_types(ctx, path: str = "/Game") -> str:
        """Enumerate the Niagara asset UClasses available in this build + a live instance count per kind.

        path: content root to count instances under (default '/Game'); e.g. '/Niagara', '/Engine'.

        Returns per-type {type, python_class_available, asset_class_path, instances_under_path}. Read-only
        (reflection + AssetRegistry; no asset load)."""
        try:
            return json.dumps(_exec(_ASSET_TYPES_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_niagara_modules — UNiagaraScript with Usage tag == Module      #
    # ------------------------------------------------------------------ #
    _MODULES_BODY = _HELP + r'''
root = PARAMS.get("path") or "/Niagara"
name_filter = (PARAMS.get("name_filter") or "").lower()
max_results = int(PARAMS.get("max_results") or 200)
usage_want = (PARAMS.get("usage") or "Module")
scripts = _assets("NiagaraScript", root)
rows = []
usages_seen = {}
for a in scripts:
    tag = None
    try:
        tv = a.get_tag_value("Usage")
        tag = str(tv) if tv is not None else None
    except Exception:
        tag = None
    if tag:
        usages_seen[tag] = usages_seen.get(tag, 0) + 1
    if usage_want and (tag or "").lower() != usage_want.lower():
        continue
    nm = str(a.get_editor_property("asset_name"))
    if name_filter and name_filter not in nm.lower():
        continue
    pkg = str(a.get_editor_property("package_name"))
    rows.append({"name": nm, "path": pkg + "." + nm, "usage": tag})
rows.sort(key=lambda r: r["path"].lower())
print("@@UMCP@@" + json.dumps({"status": "success", "path": root, "usage_filter": usage_want,
    "total_scripts_scanned": len(scripts), "match_count": len(rows),
    "returned": min(len(rows), max_results), "modules": rows[:max_results],
    "usage_tag_histogram": usages_seen,
    "note": "UNiagaraScript.Usage is a PROTECTED UPROPERTY (get_editor_property refuses it); classified "
            "via the AssetRegistry 'Usage' TAG (no per-asset load). Pass usage='' to list ALL scripts, "
            "or usage='DynamicInput'/'Function'/etc. to filter differently."}))
gc.collect()
'''

    @mcp.tool()
    def list_niagara_modules(ctx, path: str = "/Niagara", name_filter: str = None,
                             usage: str = "Module", max_results: int = 200) -> str:
        """Enumerate Niagara Module scripts (UNiagaraScript assets with Usage == Module) via AssetRegistry.

        path:        content root to scan (default '/Niagara'; try '/Game' or '/Engine' too).
        name_filter: case-insensitive substring on the script name.
        usage:       AssetRegistry 'Usage' tag to filter on (default 'Module'); pass '' for ALL scripts,
                     or e.g. 'DynamicInput' / 'Function'. A usage_tag_histogram of what was seen is returned.
        max_results: cap the list (default 200); true match_count is always reported.

        Read-only; classifies by the AssetRegistry 'Usage' tag (the Usage UPROPERTY is protected)."""
        params = {"path": path, "name_filter": name_filter, "usage": usage, "max_results": max_results}
        try:
            return json.dumps(_exec(_MODULES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_niagara_emitter_templates — standalone UNiagaraEmitter assets  #
    # ------------------------------------------------------------------ #
    _TEMPLATES_BODY = _HELP + r'''
root = PARAMS.get("path") or "/Niagara"
name_filter = (PARAMS.get("name_filter") or "").lower()
max_results = int(PARAMS.get("max_results") or 200)
ems = _assets("NiagaraEmitter", root)
rows = []
for a in ems:
    nm = str(a.get_editor_property("asset_name"))
    if name_filter and name_filter not in nm.lower():
        continue
    pkg = str(a.get_editor_property("package_name"))
    row = {"name": nm, "path": pkg + "." + nm}
    for tagname in ("TemplateSpecification", "Template", "Category"):
        try:
            tv = a.get_tag_value(tagname)
            if tv is not None:
                row[tagname] = str(tv)
        except Exception:
            pass
    rows.append(row)
rows.sort(key=lambda r: r["path"].lower())
print("@@UMCP@@" + json.dumps({"status": "success", "path": root, "count": len(rows),
    "returned": min(len(rows), max_results), "emitter_templates": rows[:max_results],
    "note": "In UE5, emitters owned by a NiagaraSystem are NOT standalone assets; standalone "
            "UNiagaraEmitter assets ARE the template/parent emitters, and live mostly in plugins "
            "(/Niagara). Pass path='/Game' to find project-local emitter assets."}))
gc.collect()
'''

    @mcp.tool()
    def list_niagara_emitter_templates(ctx, path: str = "/Niagara", name_filter: str = None,
                                       max_results: int = 200) -> str:
        """Enumerate standalone NiagaraEmitter (template/parent) assets via AssetRegistry. Read-only.

        path:        content root to scan (default '/Niagara'; standalone emitters live mostly in plugins).
        name_filter: case-insensitive substring on the emitter name.
        max_results: cap the list (default 200); true count is always reported.

        Standalone UNiagaraEmitter assets are the templates/parents a system copies emitters from (a
        system's owned emitters are not standalone assets)."""
        params = {"path": path, "name_filter": name_filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_TEMPLATES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_niagara_data_interfaces — reflect UNiagaraDataInterface tree    #
    # ------------------------------------------------------------------ #
    _DI_BODY = _HELP + r'''
name_filter = (PARAMS.get("name_filter") or "").lower()
base = getattr(unreal, "NiagaraDataInterface", None)
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unreal.NiagaraDataInterface not available"}))
else:
    subs = []
    for nm in dir(unreal):
        if not nm.startswith("NiagaraDataInterface"):
            continue
        o = getattr(unreal, nm, None)
        try:
            if isinstance(o, type) and issubclass(o, base) and o is not base:
                if name_filter and name_filter not in nm.lower():
                    continue
                subs.append(nm)
        except Exception:
            pass
    subs.sort()
    print("@@UMCP@@" + json.dumps({"status": "success", "count": len(subs),
        "data_interfaces": subs,
        "note": "Concrete UNiagaraDataInterface subclasses reachable via python reflection (walk of the "
                "unreal namespace). Abstract/native-only DIs not wrapped for Python are not listed."}))
gc.collect()
'''

    @mcp.tool()
    def list_niagara_data_interfaces(ctx, name_filter: str = None) -> str:
        """Enumerate UNiagaraDataInterface subclasses reachable via Python reflection. Read-only.

        name_filter: case-insensitive substring on the class name (e.g. 'grid', 'array', 'mesh').

        Walks the unreal namespace for concrete NiagaraDataInterface* classes. Native-only DIs that
        are not Python-wrapped are not listed (noted in the payload)."""
        try:
            return json.dumps(_exec(_DI_BODY, {"name_filter": name_filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_niagara_parameter_types — common Niagara type defs (reflection) #
    # ------------------------------------------------------------------ #
    _PARAM_TYPES_BODY = _HELP + r'''
# FNiagaraTypeDefinition exposes no registry enumeration to Python -> enumerate the common Niagara
# parameter struct/class types via reflection and say so.
candidates = [
    ("float", "float", "core"), ("int32", "int", "core"), ("bool", "bool", "core"),
    ("Vector2D", "Vector2D", "core"), ("Vector", "Vector", "core"), ("Vector4", "Vector4", "core"),
    ("LinearColor", "LinearColor", "core"), ("Quat", "Quat", "core"), ("Matrix", "Matrix", "core"),
    ("NiagaraPosition", "NiagaraPosition", "niagara"), ("NiagaraID", "NiagaraID", "niagara"),
    ("NiagaraBool", "NiagaraBool", "niagara"), ("NiagaraFloat", "NiagaraFloat", "niagara"),
    ("NiagaraInt32", "NiagaraInt32", "niagara"), ("NiagaraNumeric", "NiagaraNumeric", "niagara"),
    ("NiagaraSpawnInfo", "NiagaraSpawnInfo", "niagara"),
]
rows = []
for label, pyname, group in candidates:
    rows.append({"type": label, "python_type_available": bool(hasattr(unreal, pyname)), "group": group})
td_available = hasattr(unreal, "NiagaraTypeDefinition")
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(rows), "parameter_types": rows,
    "niagara_type_definition_available": bool(td_available),
    "note": "FNiagaraTypeDefinition is a struct with NO Python accessor to the engine's registered "
            "type registry, so the full runtime type list is NOT enumerable from Python. These are the "
            "common Niagara parameter struct/class types probed via reflection (python_type_available = "
            "unreal.<Type> exists). The niagara_write user-parameter setter accepts: bool/int/float/"
            "vector2/vector/vector4/linearcolor/quat."}))
gc.collect()
'''

    @mcp.tool()
    def list_niagara_parameter_types(ctx) -> str:
        """Enumerate common Niagara parameter type definitions via reflection. Read-only.

        FNiagaraTypeDefinition exposes no Python route to the engine's registered type registry, so
        this returns the common Niagara parameter struct/class types (float/int/bool/vector*/linearcolor/
        quat/NiagaraPosition/NiagaraID/...) with a python_type_available flag, and SAYS SO in the payload."""
        try:
            return json.dumps(_exec(_PARAM_TYPES_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_niagara_actors — placed Niagara actors/components in the level   #
    # ------------------------------------------------------------------ #
    _ACTORS_BODY = _HELP + r'''
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
rows = []
for a in actors:
    ncomps = []
    try:
        ncomps = list(a.get_components_by_class(unreal.NiagaraComponent) or [])
    except Exception:
        ncomps = []
    is_nia_actor = isinstance(a, unreal.NiagaraActor)
    if not is_nia_actor and not ncomps:
        continue
    comp_rows = []
    for c in ncomps:
        asset = _try(lambda: c.get_asset())
        comp_rows.append({"component": c.get_name(),
                          "system": (_try(lambda: asset.get_path_name()) if asset else None),
                          "is_active": bool(_try(lambda: c.is_active(), False))})
    loc = _try(lambda: a.get_actor_location())
    rows.append({"label": _try(lambda: a.get_actor_label()), "name": a.get_name(),
                 "class": a.get_class().get_name(), "is_niagara_actor": bool(is_nia_actor),
                 "location": _ser(loc), "niagara_components": comp_rows})
rows.sort(key=lambda r: str(r.get("label")).lower())
print("@@UMCP@@" + json.dumps({"status": "success", "count": len(rows), "niagara_actors": rows,
    "note": "Level actors that are ANiagaraActor OR carry a UNiagaraComponent. Enumerated via "
            "EditorActorSubsystem.get_all_level_actors (NOT find_all_object_of_class)."}))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_actors(ctx) -> str:
        """List placed Niagara actors/components in the CURRENT level. Read-only.

        Returns each actor that is an ANiagaraActor or carries a UNiagaraComponent, with {label, name,
        class, is_niagara_actor, location, niagara_components:[{component, system, is_active}]}. Uses
        EditorActorSubsystem.get_all_level_actors (never find_all_object_of_class)."""
        try:
            return json.dumps(_exec(_ACTORS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_niagara_renderer_info — per-emitter renderer summary             #
    # ------------------------------------------------------------------ #
    _RENDERER_BODY = _HELP + r'''
path = PARAMS.get("system_path") or PARAMS.get("path")
want_emitter = PARAMS.get("emitter_name")
m = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
if m is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif not isinstance(m, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a NiagaraSystem (got %s): %s" % (m.get_class().get_name(), path)}))
else:
    nfl = unreal.NiagaraFunctionLibrary
    ems = _try(lambda: nfl.get_all_emitters(m), []) or []
    out_ems = []
    for e in ems:
        en = str(_try(lambda: e.get_editor_property("emitter_name")))
        if want_emitter and en != want_emitter:
            continue
        mats = _ser(_try(lambda: e.get_editor_property("used_renderer_materials"), [])) or []
        meshes = _ser(_try(lambda: e.get_editor_property("used_renderer_meshes"), [])) or []
        renderers = []
        # infer renderer kinds from what the emitter consumes (honest: object-level props are editor-only)
        if meshes:
            renderers.append({"inferred_kind": "MeshRenderer", "meshes": meshes,
                              "materials": mats if not meshes else mats})
        if mats and not meshes:
            renderers.append({"inferred_kind": "SpriteOrRibbonRenderer", "materials": mats})
        out_ems.append({"emitter": en,
                        "enabled": bool(_try(lambda: e.get_editor_property("is_enabled"), False)),
                        "renderer_materials": mats, "renderer_meshes": meshes,
                        "inferred_renderers": renderers})
    print("@@UMCP@@" + json.dumps({"status": "success", "system": m.get_path_name(),
        "emitter_filter": want_emitter, "emitter_count": len(out_ems), "emitters": out_ems,
        "note": "Renderer materials + meshes come from NiagaraFunctionLibrary.get_all_emitters. The "
                "renderer UNiagaraRendererProperties objects live in versioned emitter data and are NOT "
                "python-reachable, so per-renderer PROPERTY values (sort mode / alignment / facing / "
                "sub-uv) are editor-only; 'inferred_renderers' is derived from consumed materials/meshes. "
                "Full renderer-property introspection needs a C++ NiagaraEditor handler."}))
gc.collect()
'''

    @mcp.tool()
    def get_niagara_renderer_info(ctx, system_path: str, emitter_name: str = None) -> str:
        """Per-emitter renderer summary for a NiagaraSystem (loads it; no editor opened). Read-only.

        system_path:  NiagaraSystem asset path.
        emitter_name: optional -- restrict to one emitter handle name.

        Returns per emitter {emitter, enabled, renderer_materials[], renderer_meshes[],
        inferred_renderers[]}. Renderer materials/meshes are exact (from get_all_emitters); the renderer
        UObjects (and thus sort_mode / alignment / facing / sub-uv) live in versioned emitter data and are
        editor-only -- that limit is stated in the payload, and full renderer-property reads need C++."""
        params = {"system_path": system_path, "emitter_name": emitter_name}
        try:
            return json.dumps(_exec(_RENDERER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
