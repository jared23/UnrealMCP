"""UserTools :: Blueprints (WRITE, wave 2)  (spec: docs/spec/blueprints.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). WRITE wave 2 — fills the
genuinely-MISSING Tier-1 Blueprint-authoring gaps from the p4 parity audit that blueprints_write.py
does NOT already cover. Everything here is ASSET-based (operates on a Blueprint asset under
/Game/MCP_Scratch or elsewhere), NON-modal, and cleanly REVERSIBLE.

Query convention (@@UMCP@@ marker), base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger at builtins._UMCP_LEDGERS[session] are copied VERBATIM from the gold-standard
editor_level.py (via blueprints_write.py / materials_write2.py). Session id arrives in
PARAMS["_session"].

Tools (2 READ, 3 WRITE):
  - get_blueprint_variable_details    (READ) per-variable detail for ONE member variable: real
                                       cpp_type / category / tooltip / FProperty flags (via
                                       MCPReflectionLibrary CDO metadata) + replication mode + the
                                       variable's current DEFAULT VALUE (read off the generated-class
                                       CDO). Parity had list_blueprint_variables (bulk) but NO
                                       per-variable getter (parity: PARTIAL).
  - get_blueprint_class_defaults      (READ) read named default properties off the Blueprint's
                                       generated-class CDO (blueprint-added by default; include_inherited
                                       to widen), paginated + filterable. Parity: MISSING.
  - set_blueprint_variable_properties (WRITE; op set_bp_var_props) set a member variable's CATEGORY
                                       and/or REPLICATION mode. Both have working getters so the prior
                                       state is captured for a FAITHFUL inverse. Parity: MISSING.
  - set_blueprint_class_defaults      (WRITE; op set_bp_class_default) set ONE property's default value
                                       on the generated-class CDO (captures prior; ONE compile so it
                                       persists). Parity: MISSING.
  - rename_blueprint_function         (WRITE; op rename_bp_function) rename a user function graph; the
                                       inverse renames it back. Parity: MISSING.

This module registers NO `undo` tool (editor_level.py owns the single unified undo); each write's
ledger op + intended inverse is documented in its docstring and in the build report for the
coordinator to fold into editor_level.undo.

DEFERRED (no faithful stock-Python path; reported honestly rather than shipping something unrevertable):
  * implement_blueprint_interface / remove_blueprint_interface / list_blueprint_interfaces —
    BlueprintEditorLibrary exposes NO interface API in this build (dir() has zero interface/implement
    methods); the Blueprint's ImplementedInterfaces UPROPERTY is not Python-accessible
    ("Failed to find property 'implemented_interfaces' on 'Blueprint'"); and
    MCPReflectionLibrary.get_class_metadata_json returns only {name, path, parent_class, is_abstract,
    is_deprecated, is_blueprintable} — no interface list. So neither the WRITE nor even the READ is
    reachable. Needs a C++ handler over FBlueprintEditorUtils::ImplementNewInterface /
    RemoveInterface + a UClass interface reader (mirroring the shipped AddBlueprintVariable pattern).
  * set_blueprint_variable_properties for the BOOLEAN flags (instance_editable / expose_on_spawn /
    expose_to_cinematics): BEL has SETTERS for these but NO getters, and the FProperty-flag metadata
    does not reflect ExposeOnSpawn or editability toggles (verified live: setting expose_on_spawn=True
    left the flags list unchanged at ["Edit","BlueprintVisible"]). Their prior state is therefore not
    capturable from Python -> not faithfully reversible -> omitted. Needs a C++ getter over the
    FBPVariableDescription PropertyFlags / metadata map.
  * Graph-node authoring (add_blueprint_node / connect_blueprint_nodes / set_pin_default / etc.) —
    Tier-2, editor-only K2 graph API, out of scope here.

API notes learned live (2026-08-17):
  * unreal.AssetEditorSubsystem is exposed as the singleton INSTANCE here, not a class —
    unreal.get_editor_subsystem(unreal.AssetEditorSubsystem) RAISES ("must be a Class"). None of these
    tools open an asset editor, so no close is needed.
  * BlueprintEditorLibrary.rename_graph(graph, new_name) takes an EdGraph (get it via find_graph),
    NOT (bp, old, new).
  * A CDO default value set via cdo.set_editor_property(...) persists after compile_blueprint(bp)
    (verified: 0.0 -> 7.5 -> restore 0.0 across compiles).
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
    #   _ledger()            -> per-session undo stack (verbatim from editor_level.py).
    #   _load_bp(path)       -> (bp, err) load + verify a Blueprint asset.
    #   _gen(bp)/_cdo(gcls)  -> generated UClass / its default object.
    #   _meta(cdo)           -> parsed CDO property metadata via MCPReflectionLibrary (or None).
    #   _repl_member(v)      -> clean enum member NAME from a BlueprintVariableReplication value.
    #   _repl_from(token)    -> (enum value, err) resolve NONE|REPLICATED|REP_NOTIFY (case-insensitive).
    #   _settable(v)         -> (json_value, restorable): serialize an Unreal value to re-appliable JSON.
    #   _coerce(current, v)  -> build an Unreal value from JSON using current's type as a hint.
    _BPW2 = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
_BEL = unreal.BlueprintEditorLibrary
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_bp(path):
    if not path:
        return None, "no blueprint_path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.Blueprint):
        return None, "asset is not a Blueprint (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _gen(bp):
    try:
        return _BEL.generated_class(bp)
    except Exception:
        return None
def _cdo(gcls):
    if gcls is None:
        return None
    try:
        return unreal.get_default_object(gcls)
    except Exception:
        try:
            return gcls.get_default_object()
        except Exception:
            return None
def _meta(cdo):
    if cdo is None or _MRL is None:
        return None
    try:
        js = _MRL.get_object_property_metadata_json(cdo)
        d = json.loads(js) if isinstance(js, str) else js
        return d if isinstance(d, dict) else None
    except Exception:
        return None
def _enum_member(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s.strip("<>").strip()
def _repl_from(token):
    cls = getattr(unreal, "BlueprintVariableReplication", None)
    if cls is None:
        return None, "BlueprintVariableReplication enum unavailable"
    t = str(token).strip().upper()
    if hasattr(cls, t):
        return getattr(cls, t), None
    return None, "no replication member '%s' (expected NONE|REPLICATED|REP_NOTIFY)" % token
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.Vector2D):
        return ([v.x, v.y], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_member(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if isinstance(current, unreal.Vector2D) and isinstance(value, (list, tuple)) and len(value) >= 2:
        return unreal.Vector2D(float(value[0]), float(value[1]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return value
    if isinstance(current, float):
        try:
            return float(value)
        except Exception:
            return value
    return value
'''

    # ================================================================== #
    # get_blueprint_variable_details  (READ)                             #
    # ================================================================== #
    _GET_VAR_DETAILS_BODY = _BPW2 + r'''
path = PARAMS["blueprint_path"]; var_name = PARAMS["variable_name"]
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    meta = _meta(cdo)
    member_names = []
    try:
        member_names = [str(v) for v in (_BEL.list_member_variable_names(bp) or [])]
    except Exception:
        member_names = []
    entry = None
    if meta is not None:
        for p in (meta.get("properties", []) or []):
            if p.get("name") == var_name:
                entry = p; break
    if entry is None and var_name not in member_names:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no variable named '%s' on this blueprint" % var_name,
            "available_member_variables": member_names}))
    else:
        flags = (entry.get("flags") if entry else []) or []
        category = None
        try:
            category = str(_BEL.get_blueprint_variable_category(bp, unreal.Name(var_name)))
        except Exception:
            category = (entry.get("category") if entry else None)
        replication = None
        try:
            replication = _enum_member(_BEL.get_blueprint_variable_replication(bp, unreal.Name(var_name)))
        except Exception:
            replication = None
        default_value = None; default_restorable = None
        if cdo is not None:
            try:
                default_value, default_restorable = _settable(cdo.get_editor_property(var_name))
            except Exception:
                default_value, default_restorable = None, False
        owner = entry.get("owner_class") if entry else None
        result = {"status": "success",
                  "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
                  "generated_class": gname,
                  "variable": {
                      "name": var_name,
                      "cpp_type": (entry.get("cpp_type") if entry else None),
                      "category": category,
                      "tooltip": (entry.get("tooltip") if entry else None),
                      "owner_class": owner,
                      "blueprint_added": (owner == gname) if owner else (var_name in member_names),
                      "replication": replication,
                      "flags": flags,
                      "instance_editable": ("Edit" in flags),
                      "blueprint_visible": ("BlueprintVisible" in flags),
                      "blueprint_readonly": ("BlueprintReadOnly" in flags),
                      "default_value": default_value,
                      "default_value_restorable": default_restorable},
                  "note": "cpp_type/category/tooltip/flags come from MCPReflectionLibrary CDO metadata "
                          "(real FProperty data); replication via BlueprintEditorLibrary; default_value "
                          "is read off the generated-class CDO. instance_editable/blueprint_visible/"
                          "blueprint_readonly are derived from FProperty flags. ExposeOnSpawn is NOT "
                          "surfaced by Python reflection here."}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_blueprint_variable_details(ctx, blueprint_path: str, variable_name: str) -> str:
        """Get full detail for ONE Blueprint member variable. Read-only.

        blueprint_path: object/package path of the Blueprint asset.
        variable_name:  the member variable's name.

        Returns {name, cpp_type, category, tooltip, owner_class, blueprint_added, replication, flags,
        instance_editable, blueprint_visible, blueprint_readonly, default_value}. Types/flags are REAL
        FProperty metadata (via MCPReflectionLibrary CDO metadata); replication via
        BlueprintEditorLibrary; default_value is read off the generated-class CDO. Companion per-variable
        getter for list_blueprint_variables (which only lists in bulk) and the setters below."""
        params = {"blueprint_path": blueprint_path, "variable_name": variable_name}
        try:
            return json.dumps(_exec(_GET_VAR_DETAILS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_blueprint_class_defaults  (READ)                              #
    # ================================================================== #
    _GET_CLASS_DEFAULTS_BODY = _BPW2 + r'''
path = PARAMS["blueprint_path"]
include_inherited = bool(PARAMS.get("include_inherited"))
filt = PARAMS.get("filter")
max_entries = PARAMS.get("max_entries")
cursor = int(PARAMS.get("cursor") or 0)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    meta = _meta(cdo)
    if cdo is None or meta is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "CDO metadata unavailable (MCPReflectionLibrary missing or CDO unresolvable)",
            "blueprint": bp.get_name()}))
    else:
        rows = []
        for p in (meta.get("properties", []) or []):
            nm = p.get("name")
            if nm == "UberGraphFrame":
                continue
            owner = p.get("owner_class")
            is_own = (owner == gname)
            if not include_inherited and not is_own:
                continue
            if filt and filt.lower() not in (nm or "").lower():
                continue
            rows.append(p)
        rows.sort(key=lambda r: (0 if r.get("owner_class") == gname else 1, (r.get("name") or "").lower()))
        total = len(rows)
        window = rows[cursor:cursor + int(max_entries)] if max_entries else rows[cursor:]
        props = {}
        for p in window:
            nm = p.get("name")
            try:
                val, restorable = _settable(cdo.get_editor_property(nm))
            except Exception:
                val, restorable = "<unreadable>", False
            props[nm] = {"value": val, "cpp_type": p.get("cpp_type"),
                         "category": p.get("category"), "owner_class": p.get("owner_class"),
                         "blueprint_added": (p.get("owner_class") == gname),
                         "restorable": restorable}
        nxt = cursor + len(window)
        print("@@UMCP@@" + json.dumps({"status": "success",
            "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
            "generated_class": gname, "include_inherited": include_inherited,
            "total_matched": total, "returned": len(props),
            "next_cursor": (nxt if nxt < total else None), "properties": props,
            "note": "Default values read off the generated-class CDO (the class archetype). By default "
                    "only blueprint-added properties are shown; include_inherited=True adds inherited "
                    "ones. 'restorable' marks values set_blueprint_class_defaults can round-trip."}))
'''

    @mcp.tool()
    def get_blueprint_class_defaults(ctx, blueprint_path: str, filter: str = None,
                                     include_inherited: bool = False, max_entries: int = 60,
                                     cursor: int = 0) -> str:
        """Read default property values from a Blueprint's generated-class CDO. Read-only.

        blueprint_path:    object/package path of the Blueprint asset.
        filter:            case-insensitive substring; only property names containing it.
        include_inherited: also include properties inherited from parent classes (default False:
                           only blueprint-added properties).
        max_entries/cursor: paginate; response returns 'next_cursor' (pass back as cursor) or null.

        Each entry reports {value, cpp_type, category, owner_class, blueprint_added, restorable}.
        Values are JSON-serialized (vectors->[x,y,z], object refs->{__object__: path}, enums->
        {__enum__: MEMBER}). Companion reader for set_blueprint_class_defaults."""
        params = {"blueprint_path": blueprint_path, "filter": filter,
                  "include_inherited": include_inherited, "max_entries": max_entries, "cursor": cursor}
        try:
            return json.dumps(_exec(_GET_CLASS_DEFAULTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_blueprint_variable_properties  (WRITE; op set_bp_var_props)   #
    # ================================================================== #
    _SET_VAR_PROPS_BODY = _BPW2 + r'''
path = PARAMS["blueprint_path"]; var_name = PARAMS["variable_name"]
new_category = PARAMS.get("category")
new_replication = PARAMS.get("replication")
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
bp, err = _load_bp(path)
fail = err
members = []
repl_val = None
if not fail:
    try:
        members = [str(v) for v in (_BEL.list_member_variable_names(bp) or [])]
    except Exception:
        members = []
    if var_name not in members:
        fail = ("no member variable named '%s' on this blueprint (only blueprint-added vars are settable)" % var_name)
    elif new_category is None and new_replication is None:
        fail = "nothing to set: provide category and/or replication"
    elif new_replication is not None:
        repl_val, repl_err = _repl_from(new_replication)
        if repl_err:
            fail = repl_err
if fail:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": fail,
        "available_member_variables": members}))
else:
    if True:
        # capture prior
        prior_category = None
        try:
            prior_category = str(_BEL.get_blueprint_variable_category(bp, unreal.Name(var_name)))
        except Exception:
            prior_category = None
        prior_replication = None
        try:
            prior_replication = _enum_member(_BEL.get_blueprint_variable_replication(bp, unreal.Name(var_name)))
        except Exception:
            prior_replication = None
        changed = {}
        with unreal.ScopedEditorTransaction("MCP set_blueprint_variable_properties"):
            if new_category is not None:
                _BEL.set_blueprint_variable_category(bp, unreal.Name(var_name), unreal.Text(str(new_category)))
                changed["category"] = prior_category
            if new_replication is not None:
                _BEL.set_blueprint_variable_replication(bp, unreal.Name(var_name), repl_val)
                changed["replication"] = prior_replication
        compiled = None
        if do_compile:
            try: compiled = bool(_BEL.compile_blueprint(bp))
            except Exception as e2: compiled = "error: %s" % e2
        try: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        except Exception: pass
        now_category = None; now_replication = None
        try: now_category = str(_BEL.get_blueprint_variable_category(bp, unreal.Name(var_name)))
        except Exception: pass
        try: now_replication = _enum_member(_BEL.get_blueprint_variable_replication(bp, unreal.Name(var_name)))
        except Exception: pass
        _ledger().append({"op": "set_bp_var_props", "bp_path": path, "var_name": var_name,
            "prior_category": (changed.get("category") if "category" in changed else None),
            "prior_replication": (changed.get("replication") if "replication" in changed else None),
            "changed_category": ("category" in changed),
            "changed_replication": ("replication" in changed)})
        print("@@UMCP@@" + json.dumps({"status": "success",
            "blueprint": bp.get_name(), "variable": var_name,
            "category": {"before": (changed.get("category") if "category" in changed else None),
                         "after": now_category, "changed": ("category" in changed)},
            "replication": {"before": (changed.get("replication") if "replication" in changed else None),
                            "after": now_replication, "changed": ("replication" in changed)},
            "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_blueprint_variable_properties(ctx, blueprint_path: str, variable_name: str,
                                          category: str = None, replication: str = None,
                                          compile: bool = True) -> str:
        """Set a Blueprint member variable's CATEGORY and/or REPLICATION mode (ledgered write).

        blueprint_path: object/package path of the Blueprint asset.
        variable_name:  the member variable (must be blueprint-added, i.e. in list_member_variable_names).
        category:       new category string (optional). Pass to change; omit to leave unchanged.
        replication:    new replication mode (optional): NONE | REPLICATED | REP_NOTIFY (case-insensitive).
        compile:        recompile after (default True).

        Uses BlueprintEditorLibrary.set_blueprint_variable_category / _replication inside a
        ScopedEditorTransaction, then compile + save. Prior category & replication are captured via the
        matching getters, so the inverse is FAITHFUL. (The boolean flags instance_editable /
        expose_on_spawn / expose_to_cinematics are NOT included: BEL has no getter for them and their
        prior state is not readable from Python, so they cannot be faithfully reverted — deferred.)

        Ledgered write op 'set_bp_var_props' {bp_path, var_name, prior_category, prior_replication,
        changed_category, changed_replication}. Inverse (in editor_level.undo):
          bp = EditorAssetLibrary.load_asset(bp_path); BEL = unreal.BlueprintEditorLibrary
          with unreal.ScopedEditorTransaction('MCP undo set_bp_var_props'):
            if changed_category:    BEL.set_blueprint_variable_category(bp, unreal.Name(var_name), unreal.Text(prior_category))
            if changed_replication: BEL.set_blueprint_variable_replication(bp, unreal.Name(var_name), getattr(unreal.BlueprintVariableReplication, prior_replication))
          BEL.compile_blueprint(bp); EditorAssetLibrary.save_asset(bp_path, only_if_is_dirty=False)"""
        params = {"blueprint_path": blueprint_path, "variable_name": variable_name,
                  "category": category, "replication": replication, "compile": compile}
        try:
            return json.dumps(_exec(_SET_VAR_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_blueprint_class_defaults  (WRITE; op set_bp_class_default)    #
    # ================================================================== #
    _SET_CLASS_DEFAULT_BODY = _BPW2 + r'''
path = PARAMS["blueprint_path"]; prop = PARAMS["property_name"]; new_value = PARAMS.get("value")
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif "." in str(prop):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "dotted/struct sub-paths are not supported (they would not persist); pass a top-level property name"}))
else:
    gcls = _gen(bp)
    cdo = _cdo(gcls)
    if cdo is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "generated-class CDO unresolvable"}))
    else:
        try:
            prior_raw = cdo.get_editor_property(prop); have = True
        except Exception as e:
            have = False
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "no such property on CDO: %s" % prop}))
        if have:
            prior_json, restorable = _settable(prior_raw)
            newv = _coerce(prior_raw, new_value)
            set_err = None
            with unreal.ScopedEditorTransaction("MCP set_blueprint_class_defaults"):
                try:
                    cdo.set_editor_property(prop, newv)
                except Exception as e:
                    set_err = str(e)
            if set_err:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "set_editor_property failed: %s" % set_err[:200], "property": prop}))
            else:
                compiled = None
                if do_compile:
                    try: compiled = bool(_BEL.compile_blueprint(bp))
                    except Exception as e: compiled = "error: %s" % e
                try: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
                except Exception: pass
                cdo2 = _cdo(_gen(bp))
                after_json, _u = _settable(cdo2.get_editor_property(prop)) if cdo2 is not None else (None, False)
                _ledger().append({"op": "set_bp_class_default", "bp_path": path,
                    "property": prop, "prior": prior_json, "restorable": restorable})
                print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(),
                    "property": prop, "before": prior_json, "after": after_json,
                    "restorable": restorable, "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_blueprint_class_defaults(ctx, blueprint_path: str, property_name: str,
                                     value=None, compile: bool = True) -> str:
        """Set ONE default property value on a Blueprint's generated-class CDO (ledgered write).

        blueprint_path: object/package path of the Blueprint asset.
        property_name:  a TOP-LEVEL property name on the class default object (a blueprint variable's
                        name, or an inherited settable property). Dotted/struct sub-paths are refused
                        (they would not persist).
        value:          the new default value. Coerced from the current value's type: vectors/rotators/
                        colors accept [..] lists; object/asset props accept an asset path string; enums
                        accept the member name; bools/numbers/strings pass through.
        compile:        recompile after (default True) so the change persists on the class archetype.

        Sets via cdo.set_editor_property inside a ScopedEditorTransaction, then ONE compile + save.
        (ONE compile per call by design — never batch compiles, they wedge the game thread.)

        Ledgered write op 'set_bp_class_default' {bp_path, property, prior, restorable}. Inverse
        (FAITHFUL when restorable; in editor_level.undo):
          bp = EditorAssetLibrary.load_asset(bp_path); BEL = unreal.BlueprintEditorLibrary
          cdo = unreal.get_default_object(BEL.generated_class(bp))
          with unreal.ScopedEditorTransaction('MCP undo set_bp_class_default'):
            cdo.set_editor_property(property, _coerce(cdo.get_editor_property(property), prior))
          BEL.compile_blueprint(bp); EditorAssetLibrary.save_asset(bp_path, only_if_is_dirty=False)
        (_coerce is the shared helper already in the undo scaffold.)"""
        params = {"blueprint_path": blueprint_path, "property_name": property_name,
                  "value": value, "compile": compile}
        try:
            return json.dumps(_exec(_SET_CLASS_DEFAULT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # rename_blueprint_function  (WRITE; op rename_bp_function)         #
    # ================================================================== #
    _RENAME_FUNC_BODY = _BPW2 + r'''
path = PARAMS["blueprint_path"]; old_name = PARAMS["old_name"]; new_name = PARAMS["new_name"]
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
_RESERVED = ("EventGraph", "UserConstructionScript")
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    graphs = []
    try:
        graphs = [str(x) for x in (_BEL.list_graph_names(bp) or [])]
    except Exception:
        graphs = []
    if old_name in _RESERVED:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "refusing to rename reserved graph '%s' (use a dedicated event-graph tool)" % old_name}))
    elif old_name not in graphs:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "no function graph named '%s'" % old_name, "graphs": graphs}))
    elif new_name in graphs:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "a graph named '%s' already exists" % new_name, "graphs": graphs}))
    elif not new_name or new_name in _RESERVED:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "invalid new_name '%s'" % new_name}))
    else:
        g = None
        try:
            g = _BEL.find_graph(bp, old_name)
        except Exception:
            g = None
        if g is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "could not resolve graph object for '%s'" % old_name}))
        else:
            with unreal.ScopedEditorTransaction("MCP rename_blueprint_function"):
                _BEL.rename_graph(g, new_name)
            compiled = None
            if do_compile:
                try: compiled = bool(_BEL.compile_blueprint(bp))
                except Exception as e: compiled = "error: %s" % e
            try: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
            except Exception: pass
            after = [str(x) for x in (_BEL.list_graph_names(bp) or [])]
            ok = (new_name in after) and (old_name not in after)
            if ok:
                _ledger().append({"op": "rename_bp_function", "bp_path": path,
                    "old_name": old_name, "new_name": new_name})
            print("@@UMCP@@" + json.dumps({"status": "success" if ok else "error",
                "blueprint": bp.get_name(), "old_name": old_name, "new_name": new_name,
                "renamed": ok, "graphs": after, "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def rename_blueprint_function(ctx, blueprint_path: str, old_name: str, new_name: str,
                                  compile: bool = True) -> str:
        """Rename a user function graph on a Blueprint (ledgered write).

        blueprint_path: object/package path of the Blueprint asset.
        old_name:       current name of the function graph (refused for reserved graphs EventGraph /
                        UserConstructionScript, and for a name not present).
        new_name:       new name (refused if it collides with an existing graph).
        compile:        recompile after (default True).

        Resolves the graph via BlueprintEditorLibrary.find_graph, then rename_graph inside a
        ScopedEditorTransaction, then compile + save.

        Ledgered write op 'rename_bp_function' {bp_path, old_name, new_name}. Inverse (FAITHFUL —
        rename back; in editor_level.undo):
          bp = EditorAssetLibrary.load_asset(bp_path); BEL = unreal.BlueprintEditorLibrary
          g = BEL.find_graph(bp, new_name)
          with unreal.ScopedEditorTransaction('MCP undo rename_bp_function'):
            BEL.rename_graph(g, old_name)
          BEL.compile_blueprint(bp); EditorAssetLibrary.save_asset(bp_path, only_if_is_dirty=False)"""
        params = {"blueprint_path": blueprint_path, "old_name": old_name, "new_name": new_name,
                  "compile": compile}
        try:
            return json.dumps(_exec(_RENAME_FUNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
