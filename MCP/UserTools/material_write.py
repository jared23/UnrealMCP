"""UserTools :: Material Instances (WRITE / CREATE)  (spec: docs/spec/editor.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE/CREATE batch for
MaterialInstanceConstant assets, the mutating counterpart to the material readers
(get_material_info / get_material_slots). Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py (via materials_assign.py / ai_write.py).

What this build exposes (all NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * MaterialInstanceConstant created non-interactively via
      unreal.AssetToolsHelpers.get_asset_tools().create_asset(
          name, package_path, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    MaterialInstanceConstantFactoryNew has NO ConfigureProperties dialog and the scripting
    create_asset path never calls one -> no modal EVER pops (bounded-probe confirmed live). The
    factory does NOT expose `initial_parent` to Python, so the parent is set AFTER creation via
    unreal.MaterialEditingLibrary.set_material_instance_parent(mic, parent). The asset is SAVED on
    creation (EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)) so it persists and the
    create_asset undo-delete is reliable.
  * Scalar / vector / texture parameter overrides via
      unreal.MaterialEditingLibrary.set_material_instance_{scalar,vector,texture}_parameter_value(
          mic, name, value, association=MaterialParameterAssociation.GLOBAL_PARAMETER)
    Readback via the matching get_material_instance_*_parameter_value getters.

PER-PARAMETER REVERT (the crux — proven live): MaterialEditingLibrary exposes
  is_material_instance_parameter_overridden(mic, name, assoc) -> bool   (state BEFORE our write)
  set_material_instance_parameter_override(mic, name, False, assoc)     (clean per-param CLEAR)
so the inverse is FAITHFUL and per-parameter (clear_all_material_instance_parameters is NOT used --
it would nuke unrelated overrides). Two cases, captured in the ledger as was_overridden:
  - was already overridden -> restore the prior value with the same setter.
  - was NOT overridden      -> set_material_instance_parameter_override(..., False) drops the
                               override so the param falls back to the parent's inherited default
                               (verified: value returns to the parent default AND overridden->False).

Implemented (all validated live, session=agentB, editor left CLEAN, ledger depth 0):
  - create_material_instance      (WRITE; ledgered op "create_asset"; inverse = close editors + delete asset [+ rmdir])
  - set_material_instance_scalar   (WRITE; ledgered op "set_material_instance_param" kind=scalar)
  - set_material_instance_vector   (WRITE; ledgered op "set_material_instance_param" kind=vector)
  - set_material_instance_texture  (WRITE; ledgered op "set_material_instance_param" kind=texture)

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified
`undo`). Creation reuses the coordinator's generic "create_asset" inverse. The three parameter
setters introduce ONE new ledger op, "set_material_instance_param", whose inverse is documented in
each tool docstring + the build report for the coordinator to fold into editor_level.undo.
Reversibility was proven live by executing those exact inverses against the agentB ledger (depth->0).
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
# snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never
# assign a snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/
# original_stderr/success/user_code/code_obj (they are the C++ wrapper's own locals).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                    -> per-session undo stack (verbatim from editor_level.py).
    #   _load_typed(path, tyname)    -> (obj, err) load an asset and type-check its class name.
    #   _close_editors(obj)          -> silently close any open asset editor for obj.
    #   _GLOBAL                      -> MaterialParameterAssociation.GLOBAL_PARAMETER
    #   _read_param(mic, kind, nm)   -> current (effective) value, JSON-friendly.
    #   _param_names(mic, kind)      -> valid parameter names of that kind on the material.
    _MW_HELPERS = r'''
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
    cn = obj.get_class().get_name()
    if cn != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, cn, path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
def _param_names(mic, kind):
    if kind == "scalar":
        return [str(x) for x in _MEL.get_scalar_parameter_names(mic)]
    if kind == "vector":
        return [str(x) for x in _MEL.get_vector_parameter_names(mic)]
    if kind == "texture":
        return [str(x) for x in _MEL.get_texture_parameter_names(mic)]
    return []
def _read_param(mic, kind, nm):
    if kind == "scalar":
        return _MEL.get_material_instance_scalar_parameter_value(mic, nm, _GLOBAL)
    if kind == "vector":
        c = _MEL.get_material_instance_vector_parameter_value(mic, nm, _GLOBAL)
        return [c.r, c.g, c.b, c.a] if c is not None else None
    if kind == "texture":
        t = _MEL.get_material_instance_texture_parameter_value(mic, nm, _GLOBAL)
        return (t.get_path_name() if t else None)
    return None
'''

    # ------------------------------------------------------------------ #
    # create_material_instance — non-interactive MIC asset creation        #
    # ------------------------------------------------------------------ #
    _CREATE_MIC_BODY = _MW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
parent_path = PARAMS["parent_material_path"]
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
parent, perr = _load_typed(parent_path, None) if False else (EAL.load_asset(parent_path), None)
if parent is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "parent material not found: %s" % parent_path}))
elif not isinstance(parent, unreal.MaterialInterface):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "parent is not a MaterialInterface (got %s): %s" % (parent.get_class().get_name(), parent_path)}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # Do NOT wrap create_asset in a ScopedEditorTransaction (per PROTOCOL #5b) -- it would trap the
    # new asset in the undo buffer and block a later delete. Creation is ledgered via create_asset.
    mic = at.create_asset(name, package_path, unreal.MaterialInstanceConstant,
                          unreal.MaterialInstanceConstantFactoryNew())
    if mic is None or not isinstance(mic, unreal.MaterialInstanceConstant):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(mic).__name__, asset_path)}))
    else:
        # Factory has no initial_parent in Python -> set the parent after creation.
        _MEL.set_material_instance_parent(mic, parent)
        _MEL.update_material_instance(mic)
        _close_editors(mic)
        # Save the new asset (persist + reliable create_asset undo-delete).
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        par = mic.get_editor_property("parent")
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": mic.get_name(),
            "asset_path": asset_path, "object_path": mic.get_path_name(),
            "class": mic.get_class().get_name(),
            "parent": (par.get_path_name() if par else None),
            "scalar_params": _param_names(mic, "scalar"),
            "vector_params": _param_names(mic, "vector"),
            "texture_params": _param_names(mic, "texture"),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_material_instance(ctx, name: str, parent_material_path: str,
                                 package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new MaterialInstanceConstant asset non-interactively (NO modal / factory dialog).

        name:                 asset name for the new material instance (e.g. 'MI_Rock').
        parent_material_path: asset path of the parent Material or MaterialInstance the new instance
                              derives from (any MaterialInterface, e.g.
                              '/Engine/BasicShapes/BasicShapeMaterial').
        package_path:         content directory to create it under (default '/Game/MCP_Scratch');
                              must be under a valid mounted root ('/Game', '/Engine', a plugin root).
                              Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.MaterialInstanceConstant,
        unreal.MaterialInstanceConstantFactoryNew()) then MaterialEditingLibrary.
        set_material_instance_parent(mic, parent) -- the factory has no ConfigureProperties dialog
        and exposes no initial_parent in Python, so the parent is linked post-create. The asset is
        saved on creation. Returns the parent link and the parent's available scalar/vector/texture
        parameter names (set overrides with set_material_instance_{scalar,vector,texture}).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close any
        editors for the asset, delete it, and remove package_path if we created it and it is now
        empty (the same generic inverse used by create_blueprint / create_blackboard)."""
        params = {"name": name, "parent_material_path": parent_material_path,
                  "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_MIC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # Shared parameter-set body (kind = scalar | vector | texture)         #
    # ------------------------------------------------------------------ #
    # ONE snippet drives all three setters; the caller passes param_kind + a pre-normalized value.
    _SET_PARAM_BODY = _MW_HELPERS + r'''
mic_path = PARAMS["mic_path"]
kind = PARAMS["param_kind"]
nm = PARAMS["parameter_name"]
mic, err = _load_typed(mic_path, "MaterialInstanceConstant")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    valid = _param_names(mic, kind)
    if nm not in valid:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no %s parameter named '%s' on this instance's material" % (kind, nm),
            "available_%s_params" % kind: valid}))
    else:
        # texture setter needs the texture asset loaded + type-checked first.
        tex = None
        terr = None
        if kind == "texture":
            tex, terr = _load_typed(PARAMS.get("texture_path"), "Texture2D")
        if terr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "texture_path invalid: %s" % terr}))
        else:
            was_overridden = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
            prior = _read_param(mic, kind, nm)
            with unreal.ScopedEditorTransaction("MCP set_material_instance_param"):
                if kind == "scalar":
                    ok = _MEL.set_material_instance_scalar_parameter_value(mic, nm, float(PARAMS["value"]), _GLOBAL)
                elif kind == "vector":
                    v = PARAMS["value"]
                    lc = unreal.LinearColor(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
                    ok = _MEL.set_material_instance_vector_parameter_value(mic, nm, lc, _GLOBAL)
                else:
                    ok = _MEL.set_material_instance_texture_parameter_value(mic, nm, tex, _GLOBAL)
                _MEL.update_material_instance(mic)
            after = _read_param(mic, kind, nm)
            now_ov = bool(_MEL.is_material_instance_parameter_overridden(mic, nm, _GLOBAL))
            try: unreal.EditorAssetLibrary.save_asset(mic_path, only_if_is_dirty=False)
            except Exception: pass
            _ledger().append({"op": "set_material_instance_param", "asset_path": mic_path,
                "object_path": mic.get_path_name(), "param_kind": kind, "parameter_name": nm,
                "prior_value": prior, "was_overridden": was_overridden})
            print("@@UMCP@@" + json.dumps({"status": "success", "material_instance": mic.get_name(),
                "param_kind": kind, "parameter_name": nm, "set_ok": bool(ok),
                "was_overridden": was_overridden, "before": prior, "after": after,
                "now_overridden": now_ov, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_material_instance_scalar(ctx, mic_path: str, parameter_name: str, value: float) -> str:
        """Override a SCALAR (float) parameter on a MaterialInstanceConstant.

        mic_path:       object/package path of the MaterialInstanceConstant asset.
        parameter_name: name of a scalar parameter exposed by the instance's parent material
                        (see create_material_instance -> scalar_params, or the material readers).
        value:          float value to set.

        Uses MaterialEditingLibrary.set_material_instance_scalar_parameter_value (association
        GLOBAL_PARAMETER), inside a ScopedEditorTransaction, then update_material_instance + save.
        Verify via get_material_instance_scalar_parameter_value.

        Ledgered write op 'set_material_instance_param' {asset_path, object_path, param_kind:'scalar',
        parameter_name, prior_value, was_overridden}. Inverse (FAITHFUL, per-parameter): if
        was_overridden -> restore prior_value with the same setter; else ->
        set_material_instance_parameter_override(mic, name, False, GLOBAL) so the param drops its
        override and returns to the parent's inherited default (never clear_all -- that is too broad)."""
        params = {"mic_path": mic_path, "param_kind": "scalar",
                  "parameter_name": parameter_name, "value": value}
        try:
            return json.dumps(_exec(_SET_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_material_instance_vector(ctx, mic_path: str, parameter_name: str, rgba: list) -> str:
        """Override a VECTOR/COLOR parameter on a MaterialInstanceConstant.

        mic_path:       object/package path of the MaterialInstanceConstant asset.
        parameter_name: name of a vector parameter exposed by the instance's parent material.
        rgba:           [r, g, b] or [r, g, b, a] LinearColor components (0..1 typically; alpha
                        defaults to 1.0 if only 3 given). LinearColor positional order IS r,g,b,a.

        Uses MaterialEditingLibrary.set_material_instance_vector_parameter_value (association
        GLOBAL_PARAMETER), inside a ScopedEditorTransaction, then update_material_instance + save.
        Verify via get_material_instance_vector_parameter_value.

        Ledgered write op 'set_material_instance_param' {param_kind:'vector', prior_value:[r,g,b,a],
        was_overridden}. Inverse (FAITHFUL, per-parameter): if was_overridden -> restore prior_value
        (LinearColor(*prior_value)); else -> set_material_instance_parameter_override(..., False)."""
        v = list(rgba or [])
        if len(v) == 3:
            v = v + [1.0]
        if len(v) != 4:
            return json.dumps({"status": "error",
                "message": "rgba must have 3 (rgb, a=1) or 4 (rgba) components, got %d" % len(v)}, indent=2)
        params = {"mic_path": mic_path, "param_kind": "vector",
                  "parameter_name": parameter_name, "value": v}
        try:
            return json.dumps(_exec(_SET_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_material_instance_texture(ctx, mic_path: str, parameter_name: str,
                                      texture_path: str) -> str:
        """Override a TEXTURE parameter on a MaterialInstanceConstant.

        mic_path:       object/package path of the MaterialInstanceConstant asset.
        parameter_name: name of a texture parameter exposed by the instance's parent material.
        texture_path:   asset path of a Texture2D to assign.

        Uses MaterialEditingLibrary.set_material_instance_texture_parameter_value (association
        GLOBAL_PARAMETER), inside a ScopedEditorTransaction, then update_material_instance + save.
        The texture asset is loaded + type-checked (Texture2D) before the write. Verify via
        get_material_instance_texture_parameter_value.

        Ledgered write op 'set_material_instance_param' {param_kind:'texture', prior_value:<path|null>,
        was_overridden}. Inverse (FAITHFUL, per-parameter): if was_overridden -> restore prior_value
        (load that texture, or None); else -> set_material_instance_parameter_override(..., False) so
        the param returns to the parent's inherited default texture."""
        params = {"mic_path": mic_path, "param_kind": "texture",
                  "parameter_name": parameter_name, "texture_path": texture_path}
        try:
            return json.dumps(_exec(_SET_PARAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NEW ledger op for editor_level.undo to learn (report to coordinator):
    #   {"op":"set_material_instance_param","asset_path":<mic path>,"object_path":<full>,
    #    "param_kind":"scalar"|"vector"|"texture","parameter_name":<name>,
    #    "prior_value":<float | [r,g,b,a] | texture_path|null>,"was_overridden":<bool>}
    #   INVERSE (LIFO):
    #     mic = EditorAssetLibrary.load_asset(asset_path)
    #     G = unreal.MaterialParameterAssociation.GLOBAL_PARAMETER
    #     MEL = unreal.MaterialEditingLibrary
    #     with unreal.ScopedEditorTransaction("MCP undo set_material_instance_param"):
    #       if was_overridden:
    #         scalar : MEL.set_material_instance_scalar_parameter_value(mic, name, prior_value, G)
    #         vector : MEL.set_material_instance_vector_parameter_value(mic, name, unreal.LinearColor(*prior_value), G)
    #         texture: MEL.set_material_instance_texture_parameter_value(mic, name, EAL.load_asset(prior_value) if prior_value else None, G)
    #       else:
    #         MEL.set_material_instance_parameter_override(mic, name, False, G)   # per-param CLEAR
    #       MEL.update_material_instance(mic)
    #     EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
    #     mic = None   # release ref so a later create_asset delete can succeed
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # ------------------------------------------------------------------ #
