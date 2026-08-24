"""UserTools :: Materials (WRITE, wave 2)  (spec: docs/spec/materials.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). This module fills the
genuinely-MISSING Tier-1 material-authoring gaps from the p3 parity audit that material_write.py /
material_graph_write.py / materials_assign.py do NOT already cover. Everything here is ASSET-based
(operates on a Material or MaterialInstanceConstant asset), NON-modal, and cleanly REVERSIBLE.

Query convention (@@UMCP@@ marker), base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger at builtins._UMCP_LEDGERS[session] are copied VERBATIM from the gold-standard
editor_level.py (via material_write.py / statetree_write.py). Session id arrives in PARAMS["_session"].

Tools (2 READ, 4 WRITE):
  - get_material_instance_parameters   (READ) list every scalar/vector/texture/static-switch param of
                                        a MaterialInstanceConstant with current value, parent default,
                                        and whether it is OVERRIDDEN on the instance. No spec reader
                                        existed (parity: get_material_instance_parameters MISSING).
  - get_material_render_properties     (READ) read a UMaterial's extended render properties
                                        (material_domain / opacity_mask_clip_value / translucency /
                                        refraction / etc.) that get_material_info does not surface.
  - set_material_instance_parent       (WRITE; op mi_set_parent) reassign a MIC's parent material,
                                        capturing the prior parent for a faithful inverse.
  - clear_material_instance_parameter  (WRITE; op mi_clear_param) drop a scalar/vector/texture/
                                        static-switch OVERRIDE so the param falls back to the parent
                                        default. Only mutates (and ledgers) when actually overridden.
  - set_material_instance_static_switch (WRITE; op mi_set_static_switch) set a static-switch parameter
                                        on a MIC (triggers static-permutation recompile).
  - set_material_render_property       (WRITE; op set_material_property) set a whitelisted UMaterial
                                        render property (material_domain, opacity_mask_clip_value,
                                        translucency_lighting_mode, refraction_method, two_sided,
                                        blend_mode, shading_model, ...) via reflection + recompile.

All parameter ops use association GLOBAL_PARAMETER (layer/blend associations are out of scope).
Per-param revert is FAITHFUL (never clear_all_material_instance_parameters): the exact prior
value / override-state is captured and restored. This module registers NO `undo` tool
(editor_level.py owns the single unified undo); each write's ledger op + intended inverse is
documented in its docstring and the build report for the coordinator to fold into editor_level.undo.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # ---- Unreal-side shared helpers (prepended to every body; no triple-quote / no backslash) ----
    #   _ledger()                 -> per-session undo stack (verbatim from editor_level.py).
    #   _load_typed(path, ty)     -> (obj, err) load an asset and (optionally) type-check its class.
    #   _close_editors(obj)       -> silently close any open asset editor for obj.
    #   _GLOBAL                   -> MaterialParameterAssociation.GLOBAL_PARAMETER
    #   MAT_PROPS                 -> whitelist {name: ("bool"|"int"|"float"|"enum", enum_class|None)}
    #   _enum_member(v)           -> clean enum member NAME string (e.g. MD_UI) from an enum value.
    #   _read_val/_read_default   -> kind-appropriate MIC param readers (current / parent default).
    _MW2 = r'''
import unreal, json, builtins
_MEL = unreal.MaterialEditingLibrary
_GLOBAL = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_typed(path, tyname):
    if not path:
        return None, "no asset path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if tyname is not None and obj.get_class().get_name() != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, obj.get_class().get_name(), path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _enum_member(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s.strip("<>").strip()
# whitelist of directly-reflected UMaterial render properties (name -> (kind, enum-class-name))
MAT_PROPS = {
    "two_sided": ("bool", None),
    "wireframe": ("bool", None),
    "dithered_lod_transition": ("bool", None),
    "allow_negative_emissive_color": ("bool", None),
    "use_material_attributes": ("bool", None),
    "cast_ray_traced_shadows": ("bool", None),
    "enable_responsive_aa": ("bool", None),
    "is_thin_surface": ("bool", None),
    "opacity_mask_clip_value": ("float", None),
    "num_customized_u_vs": ("int", None),
    "blend_mode": ("enum", "BlendMode"),
    "shading_model": ("enum", "MaterialShadingModel"),
    "material_domain": ("enum", "MaterialDomain"),
    "translucency_lighting_mode": ("enum", "TranslucencyLightingMode"),
    "refraction_method": ("enum", "RefractionMode"),
}
def _resolve_enum(enum_cls_name, token):
    cls = getattr(unreal, enum_cls_name, None)
    if cls is None:
        return None, "unknown enum class %s" % enum_cls_name
    t = str(token).strip()
    if hasattr(cls, t):
        return getattr(cls, t), None
    # accept a member typed without its conventional prefix (UI -> MD_UI, OPAQUE -> BLEND_OPAQUE)
    for pre in ("MD_", "BLEND_", "MSM_", "TLM_", "RM_"):
        if hasattr(cls, pre + t.upper()):
            return getattr(cls, pre + t.upper()), None
    return None, "no member '%s' on %s" % (token, enum_cls_name)
def _read_mic(mic, kind, nm):
    if kind == "scalar":
        return _MEL.get_material_instance_scalar_parameter_value(mic, nm, _GLOBAL)
    if kind == "vector":
        c = _MEL.get_material_instance_vector_parameter_value(mic, nm, _GLOBAL)
        return [c.r, c.g, c.b, c.a] if c is not None else None
    if kind == "texture":
        t = _MEL.get_material_instance_texture_parameter_value(mic, nm, _GLOBAL)
        return (t.get_path_name() if t else None)
    if kind == "static_switch":
        return bool(_MEL.get_material_instance_static_switch_parameter_value(mic, nm, _GLOBAL))
    return None
def _default_from(base, kind, nm):
    if base is None:
        return None
    try:
        if kind == "scalar":
            return _MEL.get_material_default_scalar_parameter_value(base, nm)
        if kind == "vector":
            c = _MEL.get_material_default_vector_parameter_value(base, nm)
            return [c.r, c.g, c.b, c.a] if c is not None else None
        if kind == "texture":
            t = _MEL.get_material_default_texture_parameter_value(base, nm)
            return (t.get_path_name() if t else None)
        if kind == "static_switch":
            return bool(_MEL.get_material_default_static_switch_parameter_value(base, nm))
    except Exception:
        return None
    return None
def _apply_mic(mic, kind, nm, value):
    if kind == "scalar":
        return _MEL.set_material_instance_scalar_parameter_value(mic, nm, float(value), _GLOBAL)
    if kind == "vector":
        lc = unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        return _MEL.set_material_instance_vector_parameter_value(mic, nm, lc, _GLOBAL)
    if kind == "texture":
        tex = unreal.EditorAssetLibrary.load_asset(value) if value else None
        return _MEL.set_material_instance_texture_parameter_value(mic, nm, tex, _GLOBAL)
    if kind == "static_switch":
        return _MEL.set_material_instance_static_switch_parameter_value(mic, nm, bool(value), _GLOBAL)
    return None
def _param_names(mic, kind):
    if kind == "scalar":
        return [str(x) for x in _MEL.get_scalar_parameter_names(mic)]
    if kind == "vector":
        return [str(x) for x in _MEL.get_vector_parameter_names(mic)]
    if kind == "texture":
        return [str(x) for x in _MEL.get_texture_parameter_names(mic)]
    if kind == "static_switch":
        return [str(x) for x in _MEL.get_static_switch_parameter_names(mic)]
    return []
'''

    # ================================================================== #
    # get_material_instance_parameters  (READ)                           #
    # ================================================================== #
    _GET_MI_PARAMS_BODY = _MW2 + r'''
mic, err = _load_typed(PARAMS["mic_path"], "MaterialInstanceConstant")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    base = mic.get_editor_property("parent")
    result = {"status": "success", "material_instance": mic.get_name(),
              "object_path": mic.get_path_name(),
              "parent": (base.get_path_name() if base else None), "parameters": {}}
    overridden_total = 0
    for kind in ("scalar", "vector", "texture", "static_switch"):
        rows = []
        for nm in _param_names(mic, kind):
            ov = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
            if ov:
                overridden_total += 1
            rows.append({"name": nm, "value": _read_mic(mic, kind, nm),
                         "parent_default": _default_from(base, kind, nm), "overridden": ov})
        result["parameters"][kind] = rows
    result["overridden_count"] = overridden_total
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_material_instance_parameters(ctx, mic_path: str) -> str:
        """List every parameter of a MaterialInstanceConstant with its live value, parent default, and
        override state. Read-only.

        mic_path: object/package path of the MaterialInstanceConstant asset.

        Returns parameters grouped by kind (scalar / vector / texture / static_switch); each entry has
        {name, value, parent_default, overridden}. 'overridden' is True when the instance carries its
        own value (vs inheriting the parent default). Companion reader for the set_material_instance_*
        / clear_material_instance_parameter writers. No spec reader existed before this."""
        try:
            return json.dumps(_exec(_GET_MI_PARAMS_BODY, {"mic_path": mic_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_material_render_properties  (READ)                            #
    # ================================================================== #
    _GET_MAT_PROPS_BODY = _MW2 + r'''
import warnings
warnings.simplefilter("ignore")
mat, err = _load_typed(PARAMS["material_path"], "Material")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    props = {}
    for nm, spec in MAT_PROPS.items():
        kind = spec[0]
        try:
            v = mat.get_editor_property(nm)
        except Exception as e:
            props[nm] = {"error": str(e)[:100]}
            continue
        if kind == "enum":
            props[nm] = {"value": _enum_member(v), "kind": "enum", "enum_class": spec[1]}
        elif kind == "bool":
            props[nm] = {"value": bool(v), "kind": "bool"}
        elif kind == "int":
            props[nm] = {"value": int(v), "kind": "int"}
        else:
            props[nm] = {"value": float(v), "kind": "float"}
    print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
        "object_path": mat.get_path_name(), "properties": props}))
'''

    @mcp.tool()
    def get_material_render_properties(ctx, material_path: str) -> str:
        """Read a base Material's extended render properties. Read-only.

        material_path: object/package path of a Material asset.

        Returns the whitelisted render properties that get_material_info does NOT surface:
        two_sided, wireframe, dithered_lod_transition, allow_negative_emissive_color,
        use_material_attributes, cast_ray_traced_shadows, enable_responsive_aa, is_thin_surface,
        opacity_mask_clip_value, num_customized_u_vs, blend_mode, shading_model, material_domain,
        translucency_lighting_mode, refraction_method. Each entry reports {value, kind, enum_class?}.
        Companion reader for set_material_render_property."""
        try:
            return json.dumps(_exec(_GET_MAT_PROPS_BODY, {"material_path": material_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_material_instance_parent  (WRITE; op mi_set_parent)           #
    # ================================================================== #
    _SET_PARENT_BODY = _MW2 + r'''
mic, err = _load_typed(PARAMS["mic_path"], "MaterialInstanceConstant")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    parent, perr = _load_typed(PARAMS["parent_material_path"], None)
    if perr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent: %s" % perr}))
    elif not isinstance(parent, unreal.MaterialInterface):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "parent is not a MaterialInterface (got %s)" % parent.get_class().get_name()}))
    else:
        prior = mic.get_editor_property("parent")
        prior_path = prior.get_path_name() if prior else None
        new_path = parent.get_path_name()
        if prior_path == new_path:
            print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
                "no_change": True, "parent": new_path, "note": "parent already set to this material",
                "ledger_depth": len(_ledger())}))
        else:
            with unreal.ScopedEditorTransaction("MCP set_material_instance_parent"):
                _MEL.set_material_instance_parent(mic, parent)
                _MEL.update_material_instance(mic)
            after = mic.get_editor_property("parent")
            after_path = after.get_path_name() if after else None
            try: unreal.EditorAssetLibrary.save_asset(PARAMS["mic_path"], only_if_is_dirty=False)
            except Exception: pass
            _ledger().append({"op": "mi_set_parent", "asset_path": PARAMS["mic_path"],
                "prior_parent_path": prior_path})
            print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
                "before_parent": prior_path, "after_parent": after_path,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_material_instance_parent(ctx, mic_path: str, parent_material_path: str) -> str:
        """Reassign the parent material of a MaterialInstanceConstant (ledgered write).

        mic_path:             object/package path of the MaterialInstanceConstant.
        parent_material_path: asset path of the new parent (any MaterialInterface: a Material or
                              another MaterialInstance).

        Uses MaterialEditingLibrary.set_material_instance_parent inside a ScopedEditorTransaction,
        then update_material_instance + save. A no-op (not ledgered) if the parent is unchanged.

        Ledgered write op 'mi_set_parent' {asset_path, prior_parent_path}. Inverse (FAITHFUL):
          mic = EditorAssetLibrary.load_asset(asset_path)
          par = EditorAssetLibrary.load_asset(prior_parent_path) if prior_parent_path else None
          with unreal.ScopedEditorTransaction('MCP undo mi_set_parent'):
            unreal.MaterialEditingLibrary.set_material_instance_parent(mic, par)
            unreal.MaterialEditingLibrary.update_material_instance(mic)
          EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)"""
        params = {"mic_path": mic_path, "parent_material_path": parent_material_path}
        try:
            return json.dumps(_exec(_SET_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # clear_material_instance_parameter  (WRITE; op mi_clear_param)     #
    # ================================================================== #
    _CLEAR_PARAM_BODY = _MW2 + r'''
kind = PARAMS["param_kind"]; nm = PARAMS["parameter_name"]
mic, err = _load_typed(PARAMS["mic_path"], "MaterialInstanceConstant")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif kind not in ("scalar", "vector", "texture", "static_switch"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "bad param_kind '%s' (scalar|vector|texture|static_switch)" % kind}))
else:
    valid = _param_names(mic, kind)
    if nm not in valid:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s parameter named '%s' on this instance" % (kind, nm),
            "available": valid}))
    else:
        was_overridden = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
        prior = _read_mic(mic, kind, nm)
        if not was_overridden:
            print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
                "param_kind": kind, "parameter_name": nm, "no_change": True,
                "note": "parameter was not overridden; nothing to clear",
                "ledger_depth": len(_ledger())}))
        else:
            with unreal.ScopedEditorTransaction("MCP clear_material_instance_parameter"):
                _MEL.set_material_instance_parameter_override(mic, nm, False, _GLOBAL)
                _MEL.update_material_instance(mic)
            after = _read_mic(mic, kind, nm)
            now_ov = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
            try: unreal.EditorAssetLibrary.save_asset(PARAMS["mic_path"], only_if_is_dirty=False)
            except Exception: pass
            _ledger().append({"op": "mi_clear_param", "asset_path": PARAMS["mic_path"],
                "param_kind": kind, "parameter_name": nm, "prior_value": prior})
            print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
                "param_kind": kind, "parameter_name": nm, "cleared": True,
                "before": prior, "after_default": after, "now_overridden": now_ov,
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def clear_material_instance_parameter(ctx, mic_path: str, param_kind: str,
                                          parameter_name: str) -> str:
        """Clear (reset) one overridden parameter on a MaterialInstanceConstant so it falls back to the
        parent default (ledgered write).

        mic_path:       object/package path of the MaterialInstanceConstant.
        param_kind:     scalar | vector | texture | static_switch.
        parameter_name: the parameter to un-override.

        Uses MaterialEditingLibrary.set_material_instance_parameter_override(mic, name, False, GLOBAL)
        inside a ScopedEditorTransaction (NOT clear_all -- only this one param), then
        update_material_instance + save. If the parameter is not currently overridden this is a
        reported no-op and is NOT ledgered.

        Ledgered write op 'mi_clear_param' {asset_path, param_kind, parameter_name, prior_value}
        (only pushed when an override was actually removed). Inverse (FAITHFUL -- re-apply the value):
          mic = EditorAssetLibrary.load_asset(asset_path); G = GLOBAL_PARAMETER; MEL = MaterialEditingLibrary
          with unreal.ScopedEditorTransaction('MCP undo mi_clear_param'):
            scalar        : MEL.set_material_instance_scalar_parameter_value(mic, name, prior_value, G)
            vector        : MEL.set_material_instance_vector_parameter_value(mic, name, unreal.LinearColor(*prior_value), G)
            texture       : MEL.set_material_instance_texture_parameter_value(mic, name, EditorAssetLibrary.load_asset(prior_value) if prior_value else None, G)
            static_switch : MEL.set_material_instance_static_switch_parameter_value(mic, name, bool(prior_value), G)
            MEL.update_material_instance(mic)
          EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)"""
        params = {"mic_path": mic_path, "param_kind": param_kind, "parameter_name": parameter_name}
        try:
            return json.dumps(_exec(_CLEAR_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_material_instance_static_switch  (WRITE; op mi_set_static_switch)
    # ================================================================== #
    _SET_SWITCH_BODY = _MW2 + r'''
nm = PARAMS["parameter_name"]; value = bool(PARAMS["value"])
mic, err = _load_typed(PARAMS["mic_path"], "MaterialInstanceConstant")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    valid = _param_names(mic, "static_switch")
    if nm not in valid:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no static-switch parameter named '%s' on this instance" % nm,
            "available_static_switch_params": valid}))
    else:
        was_overridden = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
        prior = bool(_MEL.get_material_instance_static_switch_parameter_value(mic, nm, _GLOBAL))
        with unreal.ScopedEditorTransaction("MCP set_material_instance_static_switch"):
            _MEL.set_material_instance_static_switch_parameter_value(mic, nm, value, _GLOBAL)
            _MEL.update_material_instance(mic)
        after = bool(_MEL.get_material_instance_static_switch_parameter_value(mic, nm, _GLOBAL))
        now_ov = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
        try: unreal.EditorAssetLibrary.save_asset(PARAMS["mic_path"], only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "mi_set_static_switch", "asset_path": PARAMS["mic_path"],
            "parameter_name": nm, "prior_value": prior, "was_overridden": was_overridden})
        print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
            "parameter_name": nm, "requested": value, "after": after, "set_ok": (after == value),
            "was_overridden": was_overridden, "now_overridden": now_ov,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_material_instance_static_switch(ctx, mic_path: str, parameter_name: str,
                                            value: bool) -> str:
        """Set a STATIC-SWITCH parameter override on a MaterialInstanceConstant (ledgered write).

        mic_path:       object/package path of the MaterialInstanceConstant.
        parameter_name: name of a static-switch parameter exposed by the parent material (see
                        get_material_instance_parameters -> static_switch).
        value:          bool to set.

        Uses MaterialEditingLibrary.set_material_instance_static_switch_parameter_value (association
        GLOBAL_PARAMETER) inside a ScopedEditorTransaction, then update_material_instance (this
        recompiles the static permutation) + save. set_ok reflects the read-back matching the request.

        Ledgered write op 'mi_set_static_switch' {asset_path, parameter_name, prior_value, was_overridden}.
        Inverse (FAITHFUL): if was_overridden -> restore prior_value with the same setter; else ->
        set_material_instance_parameter_override(mic, name, False, GLOBAL) so the switch returns to the
        parent default:
          mic = EditorAssetLibrary.load_asset(asset_path); G = GLOBAL_PARAMETER; MEL = MaterialEditingLibrary
          with unreal.ScopedEditorTransaction('MCP undo mi_set_static_switch'):
            if was_overridden: MEL.set_material_instance_static_switch_parameter_value(mic, name, bool(prior_value), G)
            else:              MEL.set_material_instance_parameter_override(mic, name, False, G)
            MEL.update_material_instance(mic)
          EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)"""
        params = {"mic_path": mic_path, "parameter_name": parameter_name, "value": bool(value)}
        try:
            return json.dumps(_exec(_SET_SWITCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_material_render_property  (WRITE; op set_material_property)   #
    # ================================================================== #
    _SET_MAT_PROP_BODY = _MW2 + r'''
import warnings
warnings.simplefilter("ignore")
nm = PARAMS["property_name"]; value = PARAMS["value"]
mat, err = _load_typed(PARAMS["material_path"], "Material")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif nm not in MAT_PROPS:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "property '%s' not settable here (allowed: %s)" % (nm, ", ".join(sorted(MAT_PROPS)))}))
else:
    kind, enum_cls = MAT_PROPS[nm]
    prior_raw = mat.get_editor_property(nm)
    newv = None; verr = None
    if kind == "enum":
        newv, verr = _resolve_enum(enum_cls, value)
        prior_store = _enum_member(prior_raw)
    elif kind == "bool":
        newv = bool(value) if not isinstance(value, str) else value.strip().lower() in ("true", "1", "yes", "on")
        prior_store = bool(prior_raw)
    elif kind == "int":
        newv = int(value); prior_store = int(prior_raw)
    else:
        newv = float(value); prior_store = float(prior_raw)
    if verr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": verr}))
    else:
        with unreal.ScopedEditorTransaction("MCP set_material_render_property"):
            mat.set_editor_property(nm, newv)
        try: _MEL.recompile_material(mat)
        except Exception: pass
        try: unreal.EditorAssetLibrary.save_asset(PARAMS["material_path"], only_if_is_dirty=False)
        except Exception: pass
        after_raw = mat.get_editor_property(nm)
        after_store = _enum_member(after_raw) if kind == "enum" else (bool(after_raw) if kind == "bool" else (int(after_raw) if kind == "int" else float(after_raw)))
        _ledger().append({"op": "set_material_property", "asset_path": PARAMS["material_path"],
            "property": nm, "kind": kind, "enum_class": enum_cls, "prior": prior_store})
        print("@@UMCP@@" + json.dumps({"status": "success", "material": mat.get_name(),
            "property": nm, "kind": kind, "before": prior_store, "after": after_store,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_material_render_property(ctx, material_path: str, property_name: str, value) -> str:
        """Set one whitelisted render property on a base Material (ledgered write).

        material_path: object/package path of a Material asset.
        property_name: one of two_sided | wireframe | dithered_lod_transition |
                       allow_negative_emissive_color | use_material_attributes | cast_ray_traced_shadows |
                       enable_responsive_aa | is_thin_surface | opacity_mask_clip_value |
                       num_customized_u_vs | blend_mode | shading_model | material_domain |
                       translucency_lighting_mode | refraction_method.
        value:         bool ('true'/'false' or bool) / number, or -- for enum properties -- the member
                       name (e.g. material_domain='MD_UI' or 'UI'; blend_mode='BLEND_TRANSLUCENT' or
                       'TRANSLUCENT'; shading_model='MSM_UNLIT'; refraction_method='RM_INDEX_OF_REFRACTION').

        Sets via reflection inside a ScopedEditorTransaction, then MaterialEditingLibrary.recompile_material
        + save. Extends modify_material (which covers shading_model/blend_mode/two_sided/base_color/
        metallic/roughness) with the domain/opacity-mask/translucency/refraction/etc. properties it does
        not expose. (shading_model/blend_mode/two_sided overlap modify_material -- either path works.)

        Ledgered write op 'set_material_property' {asset_path, property, kind, enum_class, prior}.
        Inverse (FAITHFUL): re-apply prior (enum -> getattr(getattr(unreal, enum_class), prior)):
          mat = EditorAssetLibrary.load_asset(asset_path)
          if kind == 'enum': pv = getattr(getattr(unreal, enum_class), prior)
          else:              pv = prior
          with unreal.ScopedEditorTransaction('MCP undo set_material_property'):
            mat.set_editor_property(property, pv)
          unreal.MaterialEditingLibrary.recompile_material(mat)
          EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)"""
        params = {"material_path": material_path, "property_name": property_name, "value": value}
        try:
            return json.dumps(_exec(_SET_MAT_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
