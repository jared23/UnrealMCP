"""UserTools :: Enhanced Input (read-only introspection)  (spec: docs/spec/input.md)

Clean-room, READ-ONLY introspection of Enhanced Input assets (UInputAction /
UInputMappingContext) over Unreal's public Python API (UE 5.8). Executed in the
editor's interpreter via the plugin's `execute_python` handler; results captured
from stdout. Complements project_info.list_input_mappings (which only ENUMERATES
IA/IMC paths + counts) by going DEEP on individual assets.

Query convention (copied verbatim from editor_level.py / project_info.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it. Every snippet is wrapped with Output-Log delta capture, surfacing new
Warning/Error lines as result['_log_warnings'].

Commands (all READ-ONLY; no writes, no ledger, no modals, no factories):
  - describe_input_action(path)          — value type, consume/pause flags, asset-level
                                           Triggers + Modifiers (class + reflected props)
  - describe_input_mapping_context(path) — the actual control scheme: every key->action
                                           mapping with per-mapping Triggers/Modifiers
  - list_input_actions(path?)            — IA enumerator w/ per-asset value-type + T/M counts
  - list_input_mapping_contexts(path?)   — IMC enumerator w/ per-asset mapping/action counts

Live-confirmed API (UE 5.8.1, this from-source build):
  - UInputAction props: value_type (InputActionValueType BOOLEAN/AXIS1D/AXIS2D/AXIS3D),
    consume_input (bool), trigger_when_paused (bool), triggers (Array[UInputTrigger]),
    modifiers (Array[UInputModifier]), accumulation_behavior (enum). Asset-level triggers/
    modifiers apply to EVERY mapping of that action.
  - UInputMappingContext: the top-level `mappings` UPROPERTY is EMPTY in 5.8; the real
    control scheme lives under default_key_mappings (a InputMappingContextMappingData
    struct) -> .mappings = Array[FEnhancedActionKeyMapping]. Each mapping has:
      .action (UInputAction), .key (FKey; key_name via get_editor_property),
      .triggers (Array[UInputTrigger]), .modifiers (Array[UInputModifier]).
  - Trigger/Modifier CONFIG properties are NOT Python getset descriptors (dir() misses
    them), but unreal.MCPReflectionLibrary.get_object_property_metadata_json(obj) yields
    their real C++ FProperty names, and get_editor_property reads each value (the C++
    PascalCase name works; snake_case is tried as a fallback). e.g. InputModifierDeadZone
    -> LowerThreshold/UpperThreshold/Type ; InputTriggerPressed -> ActuationThreshold.
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
    # Session id kept for parity with the other modules; this module is pure-read and
    # never touches any ledger. One value per OS process.
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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers (no ''' / no backslashes).               #
    #   _snake(pn): C++ PascalCase -> snake_case guess for get_editor_property.
    #   _ser(v): JSON-serialize an Unreal value (enums->str, vector->list, obj->path,
    #            opaque struct -> {__struct__}).
    #   _value_type_name(vt): friendly bool/Axis1D/Axis2D/Axis3D from InputActionValueType.
    #   _reflect_props(o): trigger/modifier CONFIG props via MCPReflectionLibrary + reads.
    #   _tm_list(arr): serialize an Array[UInputTrigger|UInputModifier] to [{class, properties}].
    # ------------------------------------------------------------------ #
    _INPUT_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _snake(pn):
    s = ""
    for ch in (pn or ""):
        if ch.isupper() and s and not s.endswith("_"):
            s += "_"
        s += ch.lower()
    return s
def _ser(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        return [_ser(e) for e in v]
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        return {"__struct__": type(v).__name__, "repr": str(v)}
    return str(v)
def _enum_token(v):
    # "<InputActionValueType.AXIS2D: 2>" -> "AXIS2D"
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    if "." in s:
        return s.split(".")[-1].strip()
    return s
_VT_FRIENDLY = {"BOOLEAN": "bool", "AXIS1D": "Axis1D", "AXIS2D": "Axis2D", "AXIS3D": "Axis3D"}
def _value_type(vt):
    tok = _enum_token(vt)
    return {"raw": str(vt), "token": tok, "friendly": _VT_FRIENDLY.get(tok, tok)}
def _reflect_props(o):
    # Trigger/Modifier config properties are not Python getset descriptors; names come
    # from MCPReflectionLibrary, values from get_editor_property (PascalCase then snake).
    names = []
    if hasattr(unreal, "MCPReflectionLibrary"):
        try:
            mj = unreal.MCPReflectionLibrary.get_object_property_metadata_json(o)
            names = [p.get("name") for p in json.loads(mj).get("properties", [])] if mj else []
        except Exception:
            names = []
    d = {}
    for pn in names:
        got = False
        for cand in (pn, _snake(pn)):
            try:
                d[pn] = _ser(o.get_editor_property(cand)); got = True; break
            except Exception:
                continue
        if not got:
            d[pn] = "<unreadable>"
    return d
def _tm_list(arr, with_props):
    out = []
    for x in (arr or []):
        if x is None:
            continue
        rec = {"class": x.get_class().get_name()}
        cp = _try(lambda x=x: x.get_class().get_path_name())
        if cp:
            rec["class_path"] = cp
        if with_props:
            rec["properties"] = _reflect_props(x)
        out.append(rec)
    return out
def _imc_mappings(imc):
    # UE 5.8: real mappings live under default_key_mappings.mappings; the top-level
    # `mappings` UPROPERTY is empty. Fall back to top-level for older layouts.
    dkm = _try(lambda: imc.get_editor_property("default_key_mappings"))
    arr = None
    if dkm is not None:
        arr = _try(lambda: dkm.get_editor_property("mappings"))
    top = _try(lambda: imc.get_editor_property("mappings"))
    if (arr is None or (hasattr(arr, "__len__") and len(arr) == 0)) and top is not None and hasattr(top, "__len__") and len(top) > 0:
        return top, "mappings (top-level)"
    return (arr if arr is not None else []), "default_key_mappings.mappings"
'''

    # ------------------------------------------------------------------ #
    # describe_input_action                                                #
    # ------------------------------------------------------------------ #
    _DESCRIBE_IA_BODY = _INPUT_HELPERS + r'''
path = PARAMS.get("path")
with_props = bool(PARAMS.get("include_properties") if PARAMS.get("include_properties") is not None else True)
ia = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
if ia is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif ia.get_class().get_name() != "InputAction":
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "not an InputAction (got %s): %s" % (ia.get_class().get_name(), path)}))
else:
    triggers = _try(lambda: ia.get_editor_property("triggers"), []) or []
    modifiers = _try(lambda: ia.get_editor_property("modifiers"), []) or []
    result = {"status": "success", "path": path, "name": ia.get_name(),
              "class": "InputAction",
              "value_type": _value_type(_try(lambda: ia.get_editor_property("value_type"))),
              "consume_input": _try(lambda: ia.get_editor_property("consume_input")),
              "trigger_when_paused": _try(lambda: ia.get_editor_property("trigger_when_paused")),
              "reserve_all_mappings": _try(lambda: ia.get_editor_property("reserve_all_mappings")),
              "accumulation_behavior": _ser(_try(lambda: ia.get_editor_property("accumulation_behavior"))),
              "action_description": _ser(_try(lambda: ia.get_editor_property("action_description"))),
              "trigger_count": len(triggers), "modifier_count": len(modifiers),
              "triggers": _tm_list(triggers, with_props),
              "modifiers": _tm_list(modifiers, with_props),
              "note": ("Asset-level triggers/modifiers apply to EVERY key mapping of this action "
                  "(per-mapping ones from an InputMappingContext are added on top). Trigger/modifier "
                  "config props come via unreal.MCPReflectionLibrary (not Python getset descriptors).")}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def describe_input_action(ctx, path: str, include_properties: bool = True) -> str:
        """Describe an Enhanced Input InputAction asset in detail. Read-only.

        Returns the action's value type (bool / Axis1D / Axis2D / Axis3D, plus the raw
        enum), the consume_input and trigger_when_paused flags, accumulation behavior, and
        the asset-level Triggers and Modifiers lists. Each trigger/modifier reports its
        class name and (with include_properties=True, default) its reflected config
        properties (e.g. InputTriggerPressed -> ActuationThreshold, InputTriggerHold ->
        HoldTimeThreshold). Asset-level triggers/modifiers apply to every mapping of the
        action; per-mapping ones live on the InputMappingContext.

        path: the InputAction asset path, e.g. '/Game/Input/Actions/IA_Jump'."""
        params = {"path": path, "include_properties": include_properties}
        try:
            return json.dumps(_exec(_DESCRIBE_IA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # describe_input_mapping_context — the actual control scheme          #
    # ------------------------------------------------------------------ #
    _DESCRIBE_IMC_BODY = _INPUT_HELPERS + r'''
path = PARAMS.get("path")
with_props = bool(PARAMS.get("include_properties") if PARAMS.get("include_properties") is not None else True)
imc = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
if imc is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
elif imc.get_class().get_name() != "InputMappingContext":
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "not an InputMappingContext (got %s): %s" % (imc.get_class().get_name(), path)}))
else:
    arr, source = _imc_mappings(imc)
    mappings = []
    by_action = {}
    for m in (arr or []):
        act = _try(lambda m=m: m.get_editor_property("action"))
        key = _try(lambda m=m: m.get_editor_property("key"))
        key_name = None
        if key is not None:
            key_name = _try(lambda key=key: str(key.get_editor_property("key_name")))
        trg = _try(lambda m=m: m.get_editor_property("triggers"), []) or []
        mod = _try(lambda m=m: m.get_editor_property("modifiers"), []) or []
        act_path = _try(lambda act=act: act.get_path_name()) if isinstance(act, unreal.Object) else None
        act_name = _try(lambda act=act: act.get_name()) if isinstance(act, unreal.Object) else None
        act_vt = None
        if isinstance(act, unreal.Object):
            act_vt = _value_type(_try(lambda act=act: act.get_editor_property("value_type")))
        rec = {"key": key_name, "action": act_name, "action_path": act_path,
               "action_value_type": (act_vt.get("friendly") if act_vt else None),
               "trigger_count": len(trg), "modifier_count": len(mod),
               "triggers": _tm_list(trg, with_props), "modifiers": _tm_list(mod, with_props)}
        mappings.append(rec)
        if act_name:
            by_action.setdefault(act_name, []).append(key_name)
    result = {"status": "success", "path": path, "name": imc.get_name(),
              "class": "InputMappingContext",
              "context_description": _ser(_try(lambda: imc.get_editor_property("context_description"))),
              "mapping_source": source, "mapping_count": len(mappings),
              "distinct_actions": len(by_action),
              "keys_by_action": by_action,
              "mappings": mappings,
              "note": ("In UE 5.8 the control scheme lives under default_key_mappings.mappings "
                  "(the top-level `mappings` UPROPERTY is empty); each entry maps a Key to an "
                  "InputAction with optional per-mapping Triggers/Modifiers layered on the "
                  "action's asset-level ones. Trigger/modifier config props come via "
                  "unreal.MCPReflectionLibrary.")}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def describe_input_mapping_context(ctx, path: str, include_properties: bool = True) -> str:
        """Describe an Enhanced Input InputMappingContext — the actual control scheme. Read-only.

        Returns every key -> action mapping: the bound Key (e.g. 'SpaceBar', 'W',
        'Gamepad_Left2D'), the InputAction it maps to (name + path + value type), and any
        per-mapping Triggers and Modifiers (class + reflected config properties, e.g.
        InputModifierNegate -> bX/bY/bZ, InputModifierDeadZone -> LowerThreshold). Also
        returns 'keys_by_action' (a per-action summary of which keys drive it).

        In UE 5.8 the mappings live under default_key_mappings.mappings; the payload reports
        which source was used under 'mapping_source'. Per-mapping triggers/modifiers layer
        on top of the InputAction's own asset-level triggers/modifiers (see
        describe_input_action).

        path: the InputMappingContext asset path, e.g. '/Game/Input/IMC_Default'."""
        params = {"path": path, "include_properties": include_properties}
        try:
            return json.dumps(_exec(_DESCRIBE_IMC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_input_actions — enumerator w/ per-asset value-type + T/M counts #
    # ------------------------------------------------------------------ #
    _LIST_IA_BODY = _INPUT_HELPERS + r'''
search_path = PARAMS.get("search_path") or "/Game"
name_filter = (PARAMS.get("name_filter") or "").lower()
page = max(1, int(PARAMS.get("page") or 1))
page_size = max(1, int(PARAMS.get("page_size") or 100))
ar = unreal.AssetRegistryHelpers.get_asset_registry()
f = unreal.ARFilter(class_names=["InputAction"], recursive_paths=True, package_paths=[search_path])
assets = _try(lambda: ar.get_assets(f), []) or []
rows = []
for a in assets:
    pkg = str(a.package_name)
    nm = str(a.asset_name)
    if name_filter and name_filter not in nm.lower():
        continue
    obj = _try(lambda pkg=pkg: unreal.EditorAssetLibrary.load_asset(pkg))
    rec = {"path": pkg, "name": nm}
    if obj is not None:
        vt = _value_type(_try(lambda obj=obj: obj.get_editor_property("value_type")))
        rec["value_type"] = vt.get("friendly")
        rec["consume_input"] = _try(lambda obj=obj: obj.get_editor_property("consume_input"))
        rec["trigger_when_paused"] = _try(lambda obj=obj: obj.get_editor_property("trigger_when_paused"))
        rec["trigger_count"] = len(_try(lambda obj=obj: obj.get_editor_property("triggers"), []) or [])
        rec["modifier_count"] = len(_try(lambda obj=obj: obj.get_editor_property("modifiers"), []) or [])
    rows.append(rec)
rows.sort(key=lambda r: r["name"].lower())
total = len(rows)
start = (page - 1) * page_size
window = rows[start:start + page_size]
nxt = page + 1 if start + page_size < total else None
print("@@UMCP@@" + json.dumps({"status": "success", "search_path": search_path,
    "total_matching": total, "page": page, "page_size": page_size,
    "returned": len(window), "next_page": nxt, "input_actions": window,
    "note": ("Enumerates InputAction assets via the AssetRegistry and loads each for a "
        "value-type + trigger/modifier-count summary. Use describe_input_action for full "
        "detail. Complements project_info.list_input_mappings (paths/counts only).")}))
'''

    @mcp.tool()
    def list_input_actions(ctx, search_path: str = "/Game", name_filter: str = None,
                           page: int = 1, page_size: int = 100) -> str:
        """List Enhanced Input InputAction assets with a per-asset summary. Read-only.

        Enumerates InputAction (IA_*) assets under search_path via the AssetRegistry and,
        for each, returns its value type (bool/Axis1D/Axis2D/Axis3D), consume_input and
        trigger_when_paused flags, and its asset-level trigger/modifier counts. This adds
        the value-type + count summary that project_info.list_input_mappings (paths/counts
        only) does not provide; use describe_input_action for the full trigger/modifier
        detail.

        search_path: AssetRegistry root to scan (default '/Game').
        name_filter: case-insensitive substring on the asset name.
        page/page_size: paginate the (name-sorted) list; response returns 'next_page' or null."""
        params = {"search_path": search_path, "name_filter": name_filter,
                  "page": page, "page_size": page_size}
        try:
            return json.dumps(_exec(_LIST_IA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_input_mapping_contexts — enumerator w/ mapping/action counts    #
    # ------------------------------------------------------------------ #
    _LIST_IMC_BODY = _INPUT_HELPERS + r'''
search_path = PARAMS.get("search_path") or "/Game"
name_filter = (PARAMS.get("name_filter") or "").lower()
page = max(1, int(PARAMS.get("page") or 1))
page_size = max(1, int(PARAMS.get("page_size") or 100))
ar = unreal.AssetRegistryHelpers.get_asset_registry()
f = unreal.ARFilter(class_names=["InputMappingContext"], recursive_paths=True, package_paths=[search_path])
assets = _try(lambda: ar.get_assets(f), []) or []
rows = []
for a in assets:
    pkg = str(a.package_name)
    nm = str(a.asset_name)
    if name_filter and name_filter not in nm.lower():
        continue
    obj = _try(lambda pkg=pkg: unreal.EditorAssetLibrary.load_asset(pkg))
    rec = {"path": pkg, "name": nm}
    if obj is not None:
        arr, source = _imc_mappings(obj)
        acts = set()
        for m in (arr or []):
            act = _try(lambda m=m: m.get_editor_property("action"))
            if isinstance(act, unreal.Object):
                acts.add(_try(lambda act=act: act.get_name()))
        rec["mapping_count"] = len(arr or [])
        rec["distinct_action_count"] = len(acts)
        rec["mapping_source"] = source
    rows.append(rec)
rows.sort(key=lambda r: r["name"].lower())
total = len(rows)
start = (page - 1) * page_size
window = rows[start:start + page_size]
nxt = page + 1 if start + page_size < total else None
print("@@UMCP@@" + json.dumps({"status": "success", "search_path": search_path,
    "total_matching": total, "page": page, "page_size": page_size,
    "returned": len(window), "next_page": nxt, "input_mapping_contexts": window,
    "note": ("Enumerates InputMappingContext assets via the AssetRegistry and loads each "
        "for a mapping-count + distinct-action-count summary. Use "
        "describe_input_mapping_context for the full key->action control scheme.")}))
'''

    @mcp.tool()
    def list_input_mapping_contexts(ctx, search_path: str = "/Game", name_filter: str = None,
                                    page: int = 1, page_size: int = 100) -> str:
        """List Enhanced Input InputMappingContext assets with a per-asset summary. Read-only.

        Enumerates InputMappingContext (IMC_*) assets under search_path via the
        AssetRegistry and, for each, returns how many key->action mappings it holds and how
        many distinct InputActions it drives. This adds the mapping/action counts that
        project_info.list_input_mappings (paths only) does not; use
        describe_input_mapping_context for the full control scheme.

        search_path: AssetRegistry root to scan (default '/Game').
        name_filter: case-insensitive substring on the asset name.
        page/page_size: paginate the (name-sorted) list; response returns 'next_page' or null."""
        params = {"search_path": search_path, "name_filter": name_filter,
                  "page": page, "page_size": page_size}
        try:
            return json.dumps(_exec(_LIST_IMC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
