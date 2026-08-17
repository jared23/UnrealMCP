"""UserTools :: Niagara / VFX (READ)  (spec: docs/spec/niagara.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO Niagara/asset editors are ever opened (no modals); nothing is mutated; no ledger.
Query convention + base64 PARAMS + Output-Log auto-capture are copied verbatim from
editor_level.py (the gold standard). This is the Niagara counterpart to animation_read /
audio_read: enumerate assets via AssetRegistry, then introspect what stock Python reflection
(plus the Niagara scripting library) actually exposes, and report the rest honestly.

What IS reachable from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * AssetRegistry enumerates NiagaraSystem / NiagaraEmitter via
    class_paths=[TopLevelAssetPath("/Script/Niagara", <Kind>)] -- these classes live in
    /Script/Niagara (NOT /Script/Engine). Path = package_name + "." + asset_name.
  * NiagaraSystem EMITTERS + USER PARAMETERS come from the Niagara SCRIPTING library, not
    reflection: unreal.NiagaraFunctionLibrary.get_all_emitters(system) -> Array of
    NiagaraMinimalEmitterInfo {emitter_name, is_enabled, is_lightweight,
    used_renderer_materials[], used_renderer_meshes[]}; get_all_user_parameters(system) ->
    Array of NiagaraUserParameterInfo {parameter_name, parameter_type (ScriptStruct/Class/Enum)}.
    (The underlying EmitterHandles / ExposedParameters UPROPERTYs are NOT python-readable --
    get_editor_property refuses them -- so these two library calls are the only route.)
  * NiagaraSystem SETTINGS via get_editor_property: warmup_time / warmup_tick_count /
    warmup_tick_delta, fixed_bounds (FBox), effect_type, plus determinism / random_seed /
    fixed_tick_delta / fixed_tick_delta_time / bake_out_rapid_iteration where present.

Known limits (reported honestly in payloads, NOT hidden):
  * Standalone NiagaraEmitter ASSET introspection is almost entirely EDITOR-ONLY here: in UE5
    every meaningful emitter setting (SimTarget CPU/GPU, bLocalSpace, bDeterminism, RandomSeed,
    RendererProperties, spawn/update script props, simulation stages) moved to VERSIONED data
    (FVersionedNiagaraEmitterData) and the top-level UNiagaraEmitter aliases are DEPRECATED ->
    get_editor_property refuses them, and no version-data accessor is exposed to Python. Only
    asset-metadata props (is_inheritable, template_asset_description) are reflectively readable.
    Rich PER-EMITTER info (name / enabled / renderer materials + meshes) is instead available
    for emitters AS THEY APPEAR INSIDE A SYSTEM via get_niagara_system_info (get_all_emitters).
  * The Niagara stack/graph (modules, node wiring, per-emitter script bodies) is editor-only and
    not reachable from Python; there is a Niagara SCRIPTING API (NiagaraFunctionLibrary /
    NiagaraClipboardEditorScriptingUtilities) but it does not expose the emitter graph read-side.

Implemented (all read-only):
  - list_niagara_assets      (AssetRegistry enumerate NiagaraSystem / NiagaraEmitter)
  - get_niagara_system_info  (emitters + user parameters + warmup/bounds/determinism settings)
  - get_niagara_emitter_info (reflectively-reachable emitter asset props + honest limits)
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.
# ALSO (learned the hard way): never name a snippet variable `sys` (or unreal/traceback/
# output_file/error_file/original_stdout/original_stderr/success/user_code/code_obj) -- the C++
# wrapper execs our code in the SAME namespace and reuses those names; clobbering `sys` breaks
# the wrapper's stdout restore and leaks the capture file handle, wedging output for all agents.

# Both kinds live in /Script/Niagara (NOT /Script/Engine).
_NIAGARA_KINDS = ["NiagaraSystem", "NiagaraEmitter"]


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
    _NIAG_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load(path):
    if not path:
        return None, "no asset path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    return obj, None
def _obj_path(o):
    return _try(lambda: o.get_path_name()) if o is not None else None
def _ser(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Array):
        return [_ser(x) for x in v]
    if isinstance(v, unreal.Object):
        return _obj_path(v)
    if isinstance(v, unreal.Vector):
        return [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
    if isinstance(v, unreal.Box):
        mn = _try(lambda: v.get_editor_property("min")); mx = _try(lambda: v.get_editor_property("max"))
        return {"min": _ser(mn), "max": _ser(mx), "is_valid": bool(_try(lambda: v.get_editor_property("is_valid"), False))}
    return str(v)[:200]
'''

    # ------------------------------------------------------------------ #
    # list_niagara_assets — enumerate NiagaraSystem / NiagaraEmitter      #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _NIAG_HELPERS + r'''
kinds_all = ["NiagaraSystem", "NiagaraEmitter"]
req_kind = PARAMS.get("kind")
package_path = PARAMS.get("path") or "/Game"
name_filter = (PARAMS.get("name_filter") or "").lower()
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 200
kinds = kinds_all
if req_kind:
    match = None
    for k in kinds_all:
        if k.lower() == str(req_kind).lower():
            match = k; break
    if match is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown kind '%s'; valid: %s" % (req_kind, ", ".join(kinds_all))}))
        kinds = None
    else:
        kinds = [match]
if kinds is not None:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    result = {"status": "success", "path": package_path,
              "kind": (kinds[0] if req_kind else None), "by_kind": {}, "total": 0}
    grand = 0
    for k in kinds:
        try:
            flt = unreal.ARFilter(
                class_paths=[unreal.TopLevelAssetPath("/Script/Niagara", k)],
                recursive_paths=True, package_paths=[package_path])
            assets = ar.get_assets(flt) or []
        except Exception as e:
            result["by_kind"][k] = {"count": 0, "error": str(e), "assets": []}
            continue
        rows = []
        for a in assets:
            pkg = str(a.get_editor_property("package_name"))
            nm = str(a.get_editor_property("asset_name"))
            if name_filter and name_filter not in nm.lower():
                continue
            rows.append({"name": nm, "path": pkg + "." + nm})
        rows.sort(key=lambda r: r["path"].lower())
        grand += len(rows)
        result["by_kind"][k] = {"count": len(rows),
                                "returned": min(len(rows), max_results),
                                "assets": rows[:max_results]}
    result["total"] = grand
    result["note"] = ("Enumerated via AssetRegistry class_paths=TopLevelAssetPath('/Script/Niagara', "
                      "<Kind>). NiagaraSystem/NiagaraEmitter are in /Script/Niagara (NOT /Script/Engine). "
                      "In UE5 emitters are usually OWNED by a system (not standalone assets), so a Third "
                      "Person template typically has NiagaraSystems under /Game but 0 standalone "
                      "NiagaraEmitter assets there; standalone emitters live in plugins (e.g. /Niagara, "
                      "/HairStrands). Pass path='/Niagara' or path=None-equivalent roots to see those.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_niagara_assets(ctx, path: str = None, kind: str = None,
                            name_filter: str = None, max_results: int = 200) -> str:
        """Enumerate Niagara assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Niagara', '/Game/VFX'.
                     NiagaraSystems usually live under /Game; standalone NiagaraEmitter assets
                     usually live in plugins (/Niagara, /HairStrands), so pass those to see them.
        kind:        restrict to 'NiagaraSystem' or 'NiagaraEmitter' (case-insensitive). Omit
                     for both.
        name_filter: case-insensitive substring on the asset name.
        max_results: cap the assets listed PER KIND (default 200); each kind's true 'count' is
                     always reported even if the list is capped.

        Returns per-kind {count, returned, assets:[{name, path}]} plus a grand 'total'.
        NiagaraSystem and NiagaraEmitter are in /Script/Niagara (not /Script/Engine)."""
        params = {"path": path, "kind": kind, "name_filter": name_filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_niagara_system_info — emitters + user params + settings          #
    # ------------------------------------------------------------------ #
    _SYS_BODY = _NIAG_HELPERS + r'''
path = PARAMS.get("path")
m, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(m, unreal.NiagaraSystem):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a NiagaraSystem (got %s): %s" % (m.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": m.get_path_name(), "class": m.get_class().get_name()}
    nfl = getattr(unreal, "NiagaraFunctionLibrary", None)
    # --- emitters (via Niagara scripting library; the EmitterHandles UPROPERTY is not readable) ---
    emitters = []
    emitters_source = None
    if nfl is not None and hasattr(nfl, "get_all_emitters"):
        try:
            for e in (nfl.get_all_emitters(m) or []):
                emitters.append({
                    "name": str(_try(lambda: e.get_editor_property("emitter_name"))),
                    "enabled": bool(_try(lambda: e.get_editor_property("is_enabled"), False)),
                    "lightweight": bool(_try(lambda: e.get_editor_property("is_lightweight"), False)),
                    "renderer_materials": _ser(_try(lambda: e.get_editor_property("used_renderer_materials"), [])) or [],
                    "renderer_meshes": _ser(_try(lambda: e.get_editor_property("used_renderer_meshes"), [])) or [],
                })
            emitters_source = "NiagaraFunctionLibrary.get_all_emitters"
        except Exception as ex:
            info["emitters_error"] = str(ex)[:160]
    info["emitter_count"] = len(emitters)
    info["emitters"] = emitters
    info["emitters_source"] = emitters_source
    # --- exposed user parameters (User.* namespace a designer sets) ---
    uparams = []
    uparams_source = None
    if nfl is not None and hasattr(nfl, "get_all_user_parameters"):
        try:
            for p in (nfl.get_all_user_parameters(m) or []):
                pt = _try(lambda: p.get_editor_property("parameter_type"))
                uparams.append({
                    "name": str(_try(lambda: p.get_editor_property("parameter_name"))),
                    "type": (_try(lambda: pt.get_name()) if pt is not None else None),
                    "type_path": (_try(lambda: pt.get_path_name()) if pt is not None else None),
                })
            uparams_source = "NiagaraFunctionLibrary.get_all_user_parameters"
        except Exception as ex:
            info["user_parameters_error"] = str(ex)[:160]
    info["user_parameter_count"] = len(uparams)
    info["user_parameters"] = uparams
    info["user_parameters_source"] = uparams_source
    info["user_parameters_note"] = ("Exposed user parameters are the User.* namespace values a "
                                    "designer sets on an instance; read via the Niagara scripting "
                                    "library (the ExposedParameters store UPROPERTY is not "
                                    "python-readable).")
    # --- reachable system settings (per-prop best-effort; deprecated/protected ones skipped) ---
    settings = {}
    for prop in ["warmup_time", "warmup_tick_count", "warmup_tick_delta", "fixed_bounds",
                 "effect_type", "determinism", "random_seed", "fixed_tick_delta",
                 "fixed_tick_delta_time", "bake_out_rapid_iteration", "use_initial_streaming_bounds",
                 "initial_streaming_bounds"]:
        try:
            settings[prop] = _ser(m.get_editor_property(prop))
        except Exception:
            pass
    info["settings"] = settings
    info["settings_note"] = ("Only python-exposed UPROPERTYs are shown; bFixedBounds and many "
                             "internal flags are protected/editor-only. fixed_bounds is an FBox "
                             "{min,max,is_valid}.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_niagara_system_info(ctx, path: str) -> str:
        """Introspect a NiagaraSystem asset (loads it; no Niagara editor opened). Read-only.

        path: NiagaraSystem asset path, e.g.
              '/Game/LevelPrototyping/Interactable/JumpPad/Assets/NS_JumpPad.NS_JumpPad'.

        Returns:
          - emitters: per-emitter {name, enabled, lightweight, renderer_materials[],
            renderer_meshes[]} via unreal.NiagaraFunctionLibrary.get_all_emitters (the
            EmitterHandles UPROPERTY itself is not python-readable).
          - user_parameters: the exposed User.* parameters a designer sets, each {name, type,
            type_path} via get_all_user_parameters.
          - settings: reachable warmup_* / fixed_bounds (FBox) / effect_type / determinism /
            random_seed / fixed_tick_delta(_time) where python-exposed.
        Errors if the asset is missing or is not a NiagaraSystem."""
        try:
            return json.dumps(_exec(_SYS_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_niagara_emitter_info — standalone emitter asset (honest limits)  #
    # ------------------------------------------------------------------ #
    _EMITTER_BODY = _NIAG_HELPERS + r'''
path = PARAMS.get("path")
em, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(em, unreal.NiagaraEmitter):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a NiagaraEmitter (got %s): %s" % (em.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": em.get_path_name(), "class": em.get_class().get_name()}
    # Reflectively-reachable asset-metadata props (best-effort; most emitter settings are deprecated).
    reachable = {}
    for prop in ["is_inheritable", "template_asset_description", "template_specification",
                 "library_visibility", "asset_tags"]:
        try:
            reachable[prop] = _ser(em.get_editor_property(prop))
        except Exception:
            pass
    info["reachable_properties"] = reachable
    # Probe (and honestly report) the settings a caller expects but that are editor-only here.
    protected = {}
    for prop in ["sim_target", "local_space", "determinism", "random_seed", "renderer_properties",
                 "spawn_script_props", "update_script_props", "simulation_stages",
                 "interpolated_spawning", "allocation_mode", "pre_allocation_count"]:
        try:
            em.get_editor_property(prop)
            protected[prop] = "readable"
        except Exception as ex:
            msg = str(ex)
            protected[prop] = ("deprecated (moved to versioned emitter data)"
                               if "deprecated" in msg.lower() else "not exposed")
    info["editor_only_settings"] = protected
    info["limits_note"] = ("In UE5 every meaningful NiagaraEmitter setting (SimTarget CPU/GPU, "
                           "bLocalSpace, bDeterminism, RandomSeed, RendererProperties, spawn/update "
                           "script props, simulation stages) lives on VERSIONED data "
                           "(FVersionedNiagaraEmitterData); the top-level UNiagaraEmitter aliases are "
                           "DEPRECATED and get_editor_property refuses them, and no version-data "
                           "accessor is exposed to Python -> these are editor-only. For rich per-emitter "
                           "info (name/enabled/renderer materials+meshes) inspect the emitter AS IT "
                           "APPEARS INSIDE A SYSTEM via get_niagara_system_info (get_all_emitters).")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_niagara_emitter_info(ctx, path: str) -> str:
        """Introspect a standalone NiagaraEmitter asset (loads it; no editor opened). Read-only.

        path: NiagaraEmitter asset path. NOTE most emitters are owned by a system, not standalone;
              standalone emitter assets typically live in plugins, e.g.
              '/Niagara/VectorFields/VectorFieldParticleEmitter.VectorFieldParticleEmitter'.

        Returns the reflectively-reachable asset-metadata props (is_inheritable,
        template_asset_description, ...) and an honest 'editor_only_settings' map showing which
        expected settings (sim_target, local_space, determinism, renderer_properties, ...) are
        DEPRECATED/versioned and therefore not reachable from Python in this build. For rich
        per-emitter data, use get_niagara_system_info on a system that contains the emitter
        (its get_all_emitters gives name/enabled/renderer materials+meshes). Errors if the asset is
        missing or is not a NiagaraEmitter."""
        try:
            return json.dumps(_exec(_EMITTER_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
