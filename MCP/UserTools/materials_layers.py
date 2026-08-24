"""UserTools :: Materials -- Material Attribute Layers stack (set/get)  (spec: docs/spec/materials.md)

DRAFT wiring for the materials-M5 C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_Materials.cpp. Both tools here are
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT (honest
fallback) until the plugin DLL is rebuilt with those handlers -- at which point each tool
AUTO-ENABLES. Scaffolding (query convention, base64 PARAMS injection, Output-Log auto-capture,
per-session undo ledger) is copied VERBATIM from the gold-standard niagara_runtime_cpp.py.

Fills the spec features that stock Python cannot reach: UMaterialInstance::SetMaterialLayers /
GetMaterialLayers are ENGINE_API but NOT UFUNCTIONs, so the Material Attribute Layers stack on a
MaterialInstanceConstant is unreachable from runtime Python (only the C++ handler can touch it).

  READ (no ledger -- non-mutating):
    * get_material_layers -- MCPReflectionLibrary.get_material_layers_json(instance_path): the resolved
      layer stack (layers[] + blends[] as function object paths, plus editor-only layer_names /
      layer_states). Returns has_layers=false (not an error) when the instance/its parents set no stack.

  WRITE (ledger -- reversible):
    * set_material_layers -- MCPReflectionLibrary.set_material_layers_json(instance_path, layers_json):
      set the whole stack from {layers:[func paths], blends:[func paths]} (layers[0] = background, no
      blend; blends[i-1] blends layers[i]; "" = an empty slot; layers=[] CLEARS the override). The C++
      handler recompiles the instance (UpdateStaticPermutation). Captures the PRIOR stack (returned as
      res["prev"]) into the ledger; inverse re-sets it.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The
write's inverse is appended to the shared per-session ledger for editor_level.undo to fold:
  op 'set_material_layers' {asset_path, object_path, prev:{has_layers, layers[], blends[], ...}}
  inverse: set_material_layers(asset_path, prev.layers, prev.blends) if prev.has_layers else
           set_material_layers(asset_path, [], [])  (clear).

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

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py) ----
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
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ material handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_Materials.cpp to enable it.")}
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
'''

    # ------------------------------------------------------------------ #
    # set_material_layers                                                 #
    # ------------------------------------------------------------------ #
    _SET_LAYERS_BODY = _HELP + r'''
rl = _mrl("set_material_layers_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_material_layers")))
else:
    layers_json = json.dumps({"layers": PARAMS.get("layers") or [], "blends": PARAMS.get("blends") or []})
    res = _decode(rl.set_material_layers_json(PARAMS["instance_path"], layers_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _save(PARAMS["instance_path"])
        _ledger().append({"op": "set_material_layers", "asset_path": PARAMS["instance_path"],
            "object_path": res.get("instance_path"), "prev": res.get("prev")})
        out = {"status": "success", "instance": res.get("instance"),
               "layer_count": res.get("layer_count"), "blend_count": res.get("blend_count"),
               "changed": res.get("changed"), "prev": res.get("prev"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def set_material_layers(ctx, instance_path: str, layers=None, blends=None) -> str:
        """Set the Material Attribute Layers stack on a MaterialInstanceConstant. Ledgered, reversible write.

        instance_path: object/package path of a MaterialInstanceConstant.
        layers:        list of MaterialFunction(Interface) object paths, one per layer. layers[0] is the
                       BACKGROUND layer (no blend). An empty string "" is a valid empty slot. An empty/None
                       list CLEARS the layer override.
        blends:        list of blend-function object paths; blends[i-1] blends layers[i] onto the stack
                       (so len(blends) is normally len(layers)-1). "" / missing = an empty blend slot.

        UMaterialInstance::SetMaterialLayers is ENGINE_API but not a UFUNCTION, so this calls the C++
        handler MCPReflectionLibrary.set_material_layers_json. The C++ side builds a well-formed stack via
        the engine's AddDefaultBackgroundLayer/AppendBlendedLayer (keeping all editor-only arrays in
        lock-step), then recompiles the instance (UpdateStaticPermutation) and marks it dirty. Saved after.

        NOTE (live-verify): SetMaterialLayers only STORES an override when the new stack differs from what
        the instance would inherit from its parent -- so the parent material should use the Material
        Attribute Layers system (a Material Attribute Layers node/parameter) for a set to be meaningful.

        Ledgered op 'set_material_layers' {asset_path, object_path, prev:{has_layers, layers[], blends[],
        ...}}. Inverse (FAITHFUL): re-set prev.layers/prev.blends (or set [] to clear if prev.has_layers is
        false). hasattr-guarded (inert until the plugin DLL is rebuilt)."""
        params = {"instance_path": instance_path,
                  "layers": list(layers) if layers else [],
                  "blends": list(blends) if blends else []}
        try:
            return json.dumps(_exec(_SET_LAYERS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_material_layers                                                 #
    # ------------------------------------------------------------------ #
    _GET_LAYERS_BODY = _HELP + r'''
rl = _mrl("get_material_layers_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_material_layers")))
else:
    res = _decode(rl.get_material_layers_json(PARAMS["instance_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res.setdefault("status", "success")
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_material_layers(ctx, instance_path: str) -> str:
        """Read the Material Attribute Layers stack of a MaterialInstance. Read-only (no ledger).

        instance_path: object/package path of a MaterialInstance(Constant).

        Calls MCPReflectionLibrary.get_material_layers_json (UMaterialInstance::GetMaterialLayers is
        ENGINE_API but not a UFUNCTION). Returns {has_layers, layers[], blends[], layer_names[],
        layer_states[], layer_count, blend_count}. has_layers is false (NOT an error) when neither the
        instance nor its parents define a layer stack. hasattr-guarded (inert until the plugin DLL is
        rebuilt)."""
        params = {"instance_path": instance_path}
        try:
            return json.dumps(_exec(_GET_LAYERS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
