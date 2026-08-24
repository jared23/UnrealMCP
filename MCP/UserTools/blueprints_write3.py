"""UserTools :: Blueprints (Batch A: export/reflection + var-flags + type search)  (spec: docs/spec/blueprints.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). "Blueprints Batch A" —
pure Python (NO C++/build), auto-discovered on the next bridge reload. Six READ tools + two WRITE
tools (one reversible flag setter, one asset creator).

Query convention (@@UMCP@@ marker), base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger at builtins._UMCP_LEDGERS[session] are copied VERBATIM from the gold-standard
editor_level.py (via blueprints_write2.py). Session id arrives in PARAMS["_session"].

Tools (6 READ, 2 WRITE):
  - export_blueprint          (READ) compose the SHIPPED blueprint readers into ONE sectioned doc:
                               info + class_defaults + variables + functions + components + interfaces.
                               Returns {sections:{...}, inheritance:[...]}. Replicates the logic of
                               get_blueprint_info / get_blueprint_class_defaults / list_blueprint_variables
                               / list_blueprint_functions / list_blueprint_components /
                               MCPReflectionLibrary.list_blueprint_interfaces. No ledger.
  - export_asset              (READ) generic reflection dump of an asset (EditorAssetLibrary.load_asset):
                               class metadata + per-property metadata (MCPReflectionLibrary.
                               get_object_property_metadata_json) + current values + AssetRegistry tags.
  - export_object            (READ) same generic dump for ANY UObject resolved by object path
                               (unreal.load_object). No ledger.
  - export_actor             (READ) same generic dump for a level actor resolved by label/name
                               (EditorActorSubsystem). No ledger.
  - set_blueprint_variable_flags (WRITE; op set_bp_var_flags) toggle a member variable's editing flags.
                               Captures PRIOR via the SHIPPED C++ getter
                               MCPReflectionLibrary.get_blueprint_variable_flags_json, applies via
                               BlueprintEditorLibrary.set_blueprint_variable_{instance_editable,
                               expose_on_spawn,expose_to_cinematics}. ONE compile after. FAITHFUL inverse.
  - search_types             (READ) AssetRegistry query for Blueprint-generated classes /
                               UserDefinedStruct / UserDefinedEnum by name (kind filters). No ledger.
  - search_parent_classes    (READ) walk a resolved class's ancestry (via get_class_metadata_json +
                               name resolution) + list known Blueprint children via AssetRegistry. No ledger.
  - create_blueprint_function_library (WRITE; op create_asset) create a BlueprintFunctionLibrary-typed
                               Blueprint via BlueprintEditorLibrary.create_blueprint_asset_with_parent
                               (parent BlueprintFunctionLibrary); VERIFIES BlueprintType == BPTYPE_FunctionLibrary.

API notes learned live (2026-08-19):
  * MCPReflectionLibrary.get_blueprint_variable_flags_json(bp, var) returns
    {instance_editable, blueprint_read_only, expose_on_spawn, private, expose_to_cinematics,
    config_variable, category, replication} -- ALL six flag bits are READABLE.
  * BlueprintEditorLibrary has SETTERS for only THREE of them: set_blueprint_variable_instance_editable /
    _expose_on_spawn / _expose_to_cinematics (variable_name arg is an unreal.Name). private /
    blueprint_read_only / config have NO Python setter in this build -> reported as unsupported
    (a later C++ setter over FBPVariableDescription.PropertyFlags will upgrade them). So only the three
    settable flags are applied + ledgered; the rest are captured/echoed but never mutated.
  * unreal.Class instances do NOT expose get_super_class() here, so the ancestry walk uses
    MCPReflectionLibrary.get_class_metadata_json(cls) -> parent_class NAME, resolved back to a Class via
    getattr(unreal, name).static_class() (native) or an AssetRegistry Blueprint lookup for Foo_C names.
  * blueprint_type is NOT reachable via get_editor_property('blueprint_type') (property not script-exposed);
    read it from the AssetRegistry 'BlueprintType' tag (EditorAssetLibrary.find_asset_data(path)).
  * create_blueprint_asset_with_parent(path, unreal.BlueprintFunctionLibrary) DOES land
    BPTYPE_FunctionLibrary (verified live) -- no C++ typed-BP creator needed for function libraries.
  * NATIVE C++ class enumeration is Python-limited: search_types(kind='class') lists Blueprint-generated
    classes only (a later C++ GetTypeRegistryJson will add native UClass/UScriptStruct/UEnum enumeration).

This module registers NO `undo` tool (editor_level.py owns the single unified undo). The one reversible
write's ledger op + intended inverse is documented in its docstring and the build report for the
coordinator to fold into editor_level.undo.
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
    #   _meta(obj)           -> parsed object property metadata via MCPReflectionLibrary (or None).
    #   _class_meta(cls)     -> parsed class metadata via MCPReflectionLibrary (or None).
    #   _enum_member(v)      -> clean enum member NAME from an EnumBase value.
    #   _settable(v)         -> (json_value, restorable): serialize an Unreal value to JSON.
    #   _resolve_class(name) -> a Class INSTANCE for a native name / a Foo_C generated-class name (or None).
    #   _inherit_chain(cls)  -> ordered ancestry list [{name, path, is_native}], walking via class metadata.
    #   _asset_tag(path,tag) -> AssetRegistry tag value (string) via find_asset_data (or None).
    _BPA = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
_BEL = unreal.BlueprintEditorLibrary
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
_EAL = unreal.EditorAssetLibrary
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
        obj = _EAL.load_asset(path)
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
def _meta(obj, include_inherited=True):
    if obj is None or _MRL is None:
        return None
    try:
        js = _MRL.get_object_property_metadata_json(obj, include_inherited)
        d = json.loads(js) if isinstance(js, str) else js
        return d if isinstance(d, dict) else None
    except Exception:
        return None
def _class_meta(cls):
    if cls is None or _MRL is None:
        return None
    try:
        js = _MRL.get_class_metadata_json(cls)
        d = json.loads(js) if isinstance(js, str) else js
        return d if isinstance(d, dict) else None
    except Exception:
        return None
def _enum_member(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s.strip("<>").strip()
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
    if isinstance(v, (unreal.Array, list, tuple)):
        try:
            return ("<array len %d>" % len(v), False)
        except Exception:
            return ("<array>", False)
    return ("<%s>" % type(v).__name__, False)
def _resolve_class(name):
    if not name:
        return None
    t = getattr(unreal, name, None)
    if t is not None and hasattr(t, "static_class"):
        try:
            return t.static_class()
        except Exception:
            pass
    if str(name).endswith("_C"):
        base = str(name)[:-2]
        try:
            ar = unreal.AssetRegistryHelpers.get_asset_registry()
            for a in (ar.get_assets(unreal.ARFilter(class_names=["Blueprint"], recursive_classes=True)) or []):
                if str(a.asset_name) == base:
                    bp = _EAL.load_asset(str(a.package_name) + "." + base)
                    if bp is not None:
                        return _gen(bp)
        except Exception:
            return None
    return None
def _inherit_chain(start_cls, limit=32):
    chain = []; seen = set(); cur = start_cls
    while cur is not None and len(chain) < limit:
        try:
            nm = cur.get_name()
        except Exception:
            nm = None
        if not nm or nm in seen:
            break
        seen.add(nm)
        try:
            pth = cur.get_path_name()
        except Exception:
            pth = None
        m = _class_meta(cur) or {}
        chain.append({"name": nm, "path": pth, "is_native": (not nm.endswith("_C"))})
        pname = m.get("parent_class")
        if not pname:
            break
        nxt = _resolve_class(pname)
        if nxt is None:
            chain.append({"name": pname, "path": None, "is_native": (not str(pname).endswith("_C")),
                          "resolved": False})
            break
        cur = nxt
    return chain
def _asset_tag(path, tag):
    try:
        ad = _EAL.find_asset_data(path)
        if ad is None:
            return None
        v = ad.get_tag_value(tag)
        return str(v) if v is not None else None
    except Exception:
        return None
def _dump_object(obj, include_values=True, include_inherited=True, max_props=500):
    cls = obj.get_class()
    cname = cls.get_name()
    meta = _meta(obj, include_inherited)
    props = (meta.get("properties", []) if isinstance(meta, dict) else []) or []
    total = len(props)
    rows = []
    for p in props[:max_props]:
        nm = p.get("name")
        row = {"name": nm, "cpp_type": p.get("cpp_type"), "category": p.get("category"),
               "owner_class": p.get("owner_class"), "flags": p.get("flags"),
               "tooltip": p.get("tooltip")}
        if include_values and nm:
            try:
                val, restorable = _settable(obj.get_editor_property(nm))
            except Exception:
                val, restorable = "<unreadable>", False
            row["value"] = val; row["value_restorable"] = restorable
        rows.append(row)
    return {"class": cname, "class_path": cls.get_path_name(),
            "class_metadata": _class_meta(cls),
            "property_count": total, "returned": len(rows),
            "truncated": (total > len(rows)), "properties": rows}
'''

    # ================================================================== #
    # export_blueprint  (READ)                                           #
    # ================================================================== #
    _EXPORT_BP_BODY = _BPA + r'''
path = PARAMS["blueprint_path"]
sections = PARAMS.get("sections")
ALL = ["info", "class_defaults", "variables", "functions", "components", "interfaces"]
if not sections:
    want = list(ALL)
else:
    want = [str(s).strip().lower() for s in sections if str(s).strip().lower() in ALL]
    if not want:
        want = list(ALL)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    cdo_meta = _meta(cdo) if cdo is not None else None
    out_sections = {}
    # ---- info ----
    if "info" in want:
        p_short = None; p_full = None
        try:
            pc = _BEL.get_blueprint_parent_class(bp)
            if pc is not None:
                p_short = pc.get_name(); p_full = pc.get_path_name()
        except Exception:
            pass
        graphs = None; dispatchers = None
        try:
            graphs = [str(x) for x in (_BEL.list_graph_names(bp) or [])]
        except Exception:
            graphs = None
        try:
            dispatchers = [str(x) for x in (_BEL.list_event_dispatchers(bp) or [])]
        except Exception:
            dispatchers = None
        total_vars = None; own_vars = None
        if isinstance(cdo_meta, dict):
            pr = cdo_meta.get("properties", []) or []
            total_vars = len(pr)
            own_vars = len([p for p in pr if p.get("owner_class") == gname and p.get("name") != "UberGraphFrame"])
        out_sections["info"] = {"asset_class": bp.get_class().get_name(),
            "generated_class": gname, "generated_class_path": (gcls.get_path_name() if gcls else None),
            "parent_class": p_short, "parent_class_path": p_full,
            "graphs": graphs, "event_dispatchers": dispatchers,
            "variables_total_uproperties": total_vars, "variables_blueprint_added": own_vars,
            "blueprint_type": _asset_tag(path, "BlueprintType")}
    # ---- class_defaults (blueprint-added, with values) ----
    if "class_defaults" in want:
        if cdo is None or not isinstance(cdo_meta, dict):
            out_sections["class_defaults"] = {"error": "CDO metadata unavailable"}
        else:
            props = {}
            for p in (cdo_meta.get("properties", []) or []):
                nm = p.get("name")
                if nm == "UberGraphFrame":
                    continue
                if p.get("owner_class") != gname:
                    continue
                try:
                    val, restorable = _settable(cdo.get_editor_property(nm))
                except Exception:
                    val, restorable = "<unreadable>", False
                props[nm] = {"value": val, "cpp_type": p.get("cpp_type"),
                             "category": p.get("category"), "restorable": restorable}
            out_sections["class_defaults"] = {"count": len(props), "properties": props}
    # ---- variables (blueprint-added, non-component) ----
    if "variables" in want:
        if not isinstance(cdo_meta, dict):
            out_sections["variables"] = {"error": "CDO metadata unavailable"}
        else:
            rows = []
            for p in (cdo_meta.get("properties", []) or []):
                nm = p.get("name")
                if nm == "UberGraphFrame":
                    continue
                cpp = p.get("cpp_type", "")
                if cpp.endswith("Component*"):
                    continue
                if p.get("owner_class") != gname:
                    continue
                rows.append({"name": nm, "cpp_type": cpp, "category": p.get("category"),
                             "flags": p.get("flags"), "tooltip": p.get("tooltip")})
            rows.sort(key=lambda r: (r.get("name") or "").lower())
            out_sections["variables"] = {"count": len(rows), "variables": rows}
    # ---- functions / events / graphs ----
    if "functions" in want:
        def _collect(structs):
            acc = []
            for s in (structs or []):
                try:
                    nm = str(s.get_editor_property("name"))
                except Exception:
                    nm = None
                if nm is None:
                    continue
                try:
                    impl = bool(s.get_editor_property("is_implemented"))
                except Exception:
                    impl = None
                if not impl:
                    continue
                acc.append({"name": nm, "is_implemented": impl})
            acc.sort(key=lambda r: (r.get("name") or "").lower())
            return acc
        fns = None; evs = None
        try:
            fns = _collect(_BEL.list_functions(bp))
        except Exception:
            fns = None
        try:
            evs = _collect(_BEL.list_events(bp))
        except Exception:
            evs = None
        gnames = None
        try:
            gnames = [str(x) for x in (_BEL.list_graph_names(bp) or [])]
        except Exception:
            gnames = None
        out_sections["functions"] = {"graphs": gnames, "functions": fns, "events": evs}
    # ---- components (component-typed UPROPERTYs) ----
    if "components" in want:
        if not isinstance(cdo_meta, dict):
            out_sections["components"] = {"error": "CDO metadata unavailable"}
        else:
            live = {}
            if cdo is not None:
                try:
                    for c in (cdo.get_components_by_class(unreal.ActorComponent) or []):
                        pa = None
                        try:
                            p = c.get_attach_parent(); pa = p.get_name() if p else None
                        except Exception:
                            pa = None
                        live[c.get_name()] = {"class": c.get_class().get_name(), "attach_parent": pa}
                except Exception:
                    live = {}
            comps = []
            for p in (cdo_meta.get("properties", []) or []):
                cpp = p.get("cpp_type", "")
                if not cpp.endswith("Component*"):
                    continue
                nm = p.get("name")
                li = live.get(nm)
                cls_from_cpp = cpp[:-1]
                if cls_from_cpp.startswith("U"):
                    cls_from_cpp = cls_from_cpp[1:]
                comps.append({"name": nm, "class": (li["class"] if li else cls_from_cpp),
                              "attach_parent": (li["attach_parent"] if li else None),
                              "owner_class": p.get("owner_class"),
                              "blueprint_added": (p.get("owner_class") == gname),
                              "instanced_on_cdo": li is not None})
            comps.sort(key=lambda c: (c.get("name") or "").lower())
            out_sections["components"] = {"count": len(comps), "components": comps}
    # ---- interfaces (C++ reader) ----
    if "interfaces" in want:
        ifaces = None; ierr = None
        if _MRL is not None and hasattr(_MRL, "list_blueprint_interfaces"):
            try:
                ij = _MRL.list_blueprint_interfaces(bp)
                ifaces = json.loads(ij) if isinstance(ij, str) else ij
            except Exception as e:
                ierr = str(e)[:160]
        else:
            ierr = "MCPReflectionLibrary.list_blueprint_interfaces unavailable"
        out_sections["interfaces"] = ifaces if ifaces is not None else {"error": ierr}
    # ---- inheritance ----
    inheritance = _inherit_chain(gcls) if gcls is not None else []
    print("@@UMCP@@" + json.dumps({"status": "success",
        "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
        "requested_sections": want, "sections": out_sections, "inheritance": inheritance,
        "note": "Composes the shipped blueprint readers into one document. Sections mirror "
                "get_blueprint_info/class_defaults/list_blueprint_variables/functions/components + "
                "MCPReflectionLibrary.list_blueprint_interfaces. 'inheritance' walks the generated "
                "class ancestry via class metadata (native ancestors resolve fully; a Blueprint parent "
                "resolves through the AssetRegistry). Read-only."}))
'''

    @mcp.tool()
    def export_blueprint(ctx, blueprint_path: str, sections=None) -> str:
        """Export a Blueprint as ONE sectioned reflection document (composes the shipped readers). Read-only.

        blueprint_path: object/package path of the Blueprint asset.
        sections:       optional list choosing which sections to include; any of
                        info | class_defaults | variables | functions | components | interfaces
                        (default: all). Unknown names are ignored; empty selection falls back to all.

        Returns {sections:{...}, inheritance:[...]}. Each section replicates a shipped reader:
        'info' = get_blueprint_info summary; 'class_defaults' = blueprint-added CDO defaults with values;
        'variables' = list_blueprint_variables (blueprint-added, non-component); 'functions' =
        list_blueprint_functions (implemented functions/events + graph names); 'components' =
        list_blueprint_components (component-typed UPROPERTYs); 'interfaces' =
        MCPReflectionLibrary.list_blueprint_interfaces. 'inheritance' is the generated class ancestry
        (name/path/is_native), walked via class metadata. No ledger (read-only)."""
        params = {"blueprint_path": blueprint_path, "sections": sections}
        try:
            return json.dumps(_exec(_EXPORT_BP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # export_asset  (READ)                                               #
    # ================================================================== #
    _EXPORT_ASSET_BODY = _BPA + r'''
path = PARAMS["asset_path"]
include_values = PARAMS.get("include_values")
include_values = True if include_values is None else bool(include_values)
include_inherited = PARAMS.get("include_inherited")
include_inherited = True if include_inherited is None else bool(include_inherited)
max_props = int(PARAMS.get("max_props") or 500)
if not path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no asset_path given"}))
elif not _EAL.does_asset_exist(path):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset not found: %s" % path}))
else:
    obj = None
    try:
        obj = _EAL.load_asset(path)
    except Exception as e:
        obj = None
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "load failed: %s" % path}))
    else:
        dump = _dump_object(obj, include_values, include_inherited, max_props)
        tags = {}
        for t in ["BlueprintType", "ParentClass", "NativeParentClass", "GeneratedClass"]:
            v = _asset_tag(path, t)
            if v is not None:
                tags[t] = v
        print("@@UMCP@@" + json.dumps({"status": "success",
            "asset": {"name": obj.get_name(), "path": obj.get_path_name()},
            "asset_registry_tags": tags, "dump": dump,
            "note": "Generic reflection dump via MCPReflectionLibrary.get_object_property_metadata_json "
                    "on the loaded asset object; values serialized (vectors->[x,y,z], object refs->"
                    "{__object__:path}, enums->{__enum__:MEMBER}); non-trivial structs/arrays echoed as a "
                    "type tag. include_inherited widens to base-class properties. Read-only."}))
'''

    @mcp.tool()
    def export_asset(ctx, asset_path: str, include_values: bool = True,
                     include_inherited: bool = True, max_props: int = 500) -> str:
        """Generic reflection dump of an asset (loads it; no editor opened). Read-only.

        asset_path:        object/package path of the asset (EditorAssetLibrary.load_asset).
        include_values:    also read each property's current value (default True).
        include_inherited: include base-class properties (default True).
        max_props:         cap the number of properties dumped (default 500).

        Returns the asset's class metadata, per-property reflection metadata (name/cpp_type/category/
        flags/tooltip) via unreal.MCPReflectionLibrary.get_object_property_metadata_json, current values
        (serialized), and key AssetRegistry tags. No ledger (read-only)."""
        params = {"asset_path": asset_path, "include_values": include_values,
                  "include_inherited": include_inherited, "max_props": max_props}
        try:
            return json.dumps(_exec(_EXPORT_ASSET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # export_object  (READ)                                              #
    # ================================================================== #
    _EXPORT_OBJECT_BODY = _BPA + r'''
path = PARAMS["object_path"]
include_values = PARAMS.get("include_values")
include_values = True if include_values is None else bool(include_values)
include_inherited = PARAMS.get("include_inherited")
include_inherited = True if include_inherited is None else bool(include_inherited)
max_props = int(PARAMS.get("max_props") or 500)
obj = None; how = None
if not path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no object_path given"}))
else:
    try:
        obj = unreal.load_object(None, path); how = "load_object"
    except Exception:
        obj = None
    if obj is None:
        try:
            obj = _EAL.load_asset(path); how = "load_asset"
        except Exception:
            obj = None
    if obj is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not resolve object: %s (tried load_object + load_asset)" % path}))
    else:
        dump = _dump_object(obj, include_values, include_inherited, max_props)
        print("@@UMCP@@" + json.dumps({"status": "success",
            "object": {"name": obj.get_name(), "path": obj.get_path_name()}, "resolved_via": how,
            "dump": dump,
            "note": "Generic reflection dump of any UObject resolved by path (unreal.load_object, then "
                    "EditorAssetLibrary.load_asset fallback). Read-only."}))
'''

    @mcp.tool()
    def export_object(ctx, object_path: str, include_values: bool = True,
                      include_inherited: bool = True, max_props: int = 500) -> str:
        """Generic reflection dump of ANY UObject resolved by object path. Read-only.

        object_path:       full object path (e.g. '/Game/Foo.Foo' or a subobject path). Resolved via
                           unreal.load_object, falling back to EditorAssetLibrary.load_asset.
        include_values:    also read each property's current value (default True).
        include_inherited: include base-class properties (default True).
        max_props:         cap the number of properties dumped (default 500).

        Returns class metadata + per-property reflection metadata + serialized values via
        unreal.MCPReflectionLibrary.get_object_property_metadata_json. No ledger (read-only)."""
        params = {"object_path": object_path, "include_values": include_values,
                  "include_inherited": include_inherited, "max_props": max_props}
        try:
            return json.dumps(_exec(_EXPORT_OBJECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # export_actor  (READ)                                               #
    # ================================================================== #
    _EXPORT_ACTOR_BODY = _BPA + r'''
name = PARAMS["actor_name"]
include_values = PARAMS.get("include_values")
include_values = True if include_values is None else bool(include_values)
include_inherited = PARAMS.get("include_inherited")
include_inherited = True if include_inherited is None else bool(include_inherited)
max_props = int(PARAMS.get("max_props") or 500)
actor = None; candidates = []
try:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = eas.get_all_level_actors() if eas else []
except Exception:
    all_actors = []
for a in (all_actors or []):
    try:
        lbl = a.get_actor_label()
    except Exception:
        lbl = None
    try:
        onm = a.get_name()
    except Exception:
        onm = None
    if lbl == name or onm == name:
        actor = a; break
    if name and ((lbl and name.lower() in lbl.lower()) or (onm and name.lower() in onm.lower())):
        candidates.append(lbl or onm)
if actor is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "no actor with label/name '%s' in the current level" % name,
        "near_matches": candidates[:20], "level_actor_count": len(all_actors or [])}))
else:
    dump = _dump_object(actor, include_values, include_inherited, max_props)
    tinfo = None
    try:
        t = actor.get_actor_transform()
        loc = t.translation; rot = t.rotation.rotator(); sca = t.scale3d
        tinfo = {"location": [loc.x, loc.y, loc.z], "rotation": [rot.pitch, rot.yaw, rot.roll],
                 "scale": [sca.x, sca.y, sca.z]}
    except Exception:
        tinfo = None
    print("@@UMCP@@" + json.dumps({"status": "success",
        "actor": {"label": actor.get_actor_label(), "name": actor.get_name(),
                  "path": actor.get_path_name()},
        "transform": tinfo, "dump": dump,
        "note": "Generic reflection dump of a level actor resolved by label/name via "
                "EditorActorSubsystem. Read-only."}))
'''

    @mcp.tool()
    def export_actor(ctx, actor_name: str, include_values: bool = True,
                     include_inherited: bool = True, max_props: int = 500) -> str:
        """Generic reflection dump of a level actor (resolved by label or object name). Read-only.

        actor_name:        the actor's editor label or object name (exact match preferred; near
                           substring matches are reported to help disambiguate).
        include_values:    also read each property's current value (default True).
        include_inherited: include base-class properties (default True).
        max_props:         cap the number of properties dumped (default 500).

        Returns class metadata + per-property reflection metadata + serialized values (via
        unreal.MCPReflectionLibrary.get_object_property_metadata_json) plus the actor transform.
        No ledger (read-only)."""
        params = {"actor_name": actor_name, "include_values": include_values,
                  "include_inherited": include_inherited, "max_props": max_props}
        try:
            return json.dumps(_exec(_EXPORT_ACTOR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_blueprint_variable_flags  (WRITE; op set_bp_var_flags)         #
    # ================================================================== #
    _SET_VAR_FLAGS_BODY = _BPA + r'''
path = PARAMS["blueprint_path"]; var_name = PARAMS["variable_name"]
flags = PARAMS.get("flags") or {}
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
GKEY = {"instance_editable": "instance_editable", "expose_on_spawn": "expose_on_spawn",
        "expose_to_cinematics": "expose_to_cinematics", "private": "private",
        "blueprint_read_only": "blueprint_read_only", "config": "config_variable"}
SETTABLE = ("instance_editable", "expose_on_spawn", "expose_to_cinematics")
bp, err = _load_bp(path)
fail = err
members = []
if not fail:
    try:
        members = [str(v) for v in (_BEL.list_member_variable_names(bp) or [])]
    except Exception:
        members = []
    if var_name not in members:
        fail = "no member variable named '%s' on this blueprint (only blueprint-added vars are settable)" % var_name
    elif not isinstance(flags, dict) or not flags:
        fail = "no flags given: pass a dict of any of instance_editable/expose_on_spawn/expose_to_cinematics/private/blueprint_read_only/config"
if fail:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": fail,
        "available_member_variables": members}))
else:
    prior_all = {}
    if _MRL is not None and hasattr(_MRL, "get_blueprint_variable_flags_json"):
        try:
            fj = _MRL.get_blueprint_variable_flags_json(bp, var_name)
            prior_all = json.loads(fj) if isinstance(fj, str) else (fj or {})
        except Exception:
            prior_all = {}
    applied = {}; unsupported = {}; prior_ledger = {}
    with unreal.ScopedEditorTransaction("MCP set_blueprint_variable_flags"):
        for fk, want in flags.items():
            fkl = str(fk).strip().lower()
            if fkl not in GKEY:
                unsupported[fk] = "unknown flag"
                continue
            gk = GKEY[fkl]
            prior_val = bool(prior_all.get(gk)) if isinstance(prior_all, dict) and gk in prior_all else None
            if fkl not in SETTABLE:
                unsupported[fkl] = "no BlueprintEditorLibrary setter in this build (needs a C++ setter over FBPVariableDescription PropertyFlags)"
                continue
            want_b = bool(want)
            try:
                if fkl == "instance_editable":
                    _BEL.set_blueprint_variable_instance_editable(bp, unreal.Name(var_name), want_b)
                elif fkl == "expose_on_spawn":
                    _BEL.set_blueprint_variable_expose_on_spawn(bp, unreal.Name(var_name), want_b)
                elif fkl == "expose_to_cinematics":
                    _BEL.set_blueprint_variable_expose_to_cinematics(bp, unreal.Name(var_name), want_b)
                applied[fkl] = {"before": prior_val, "requested": want_b}
                prior_ledger[fkl] = prior_val
            except Exception as e:
                unsupported[fkl] = "setter failed: %s" % str(e)[:120]
    compiled = None
    if do_compile and applied:
        try:
            compiled = bool(_BEL.compile_blueprint(bp))
        except Exception as e:
            compiled = "error: %s" % e
    try:
        _EAL.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
    after_all = {}
    if _MRL is not None and hasattr(_MRL, "get_blueprint_variable_flags_json"):
        try:
            fj2 = _MRL.get_blueprint_variable_flags_json(bp, var_name)
            after_all = json.loads(fj2) if isinstance(fj2, str) else (fj2 or {})
        except Exception:
            after_all = {}
    depth = len(_ledger())
    if applied:
        _ledger().append({"op": "set_bp_var_flags", "blueprint_path": path,
            "variable_name": var_name, "prior": prior_ledger})
        depth = len(_ledger())
    print("@@UMCP@@" + json.dumps({"status": "success",
        "blueprint": bp.get_name(), "variable": var_name,
        "applied": applied, "unsupported": unsupported,
        "flags_before": {k: prior_all.get(k) for k in prior_all} if isinstance(prior_all, dict) else prior_all,
        "flags_after": {k: after_all.get(k) for k in after_all} if isinstance(after_all, dict) else after_all,
        "compiled": compiled, "ledger_depth": depth,
        "note": "Only instance_editable/expose_on_spawn/expose_to_cinematics have BlueprintEditorLibrary "
                "setters in this build; private/blueprint_read_only/config are read-only here (reported "
                "under 'unsupported'; a later C++ setter will enable them). Prior values captured via "
                "MCPReflectionLibrary.get_blueprint_variable_flags_json -> faithful inverse."}))
'''

    @mcp.tool()
    def set_blueprint_variable_flags(ctx, blueprint_path: str, variable_name: str,
                                     flags: dict = None, compile: bool = True) -> str:
        """Toggle a Blueprint member variable's editing flags (ledgered, reversible write).

        blueprint_path: object/package path of the Blueprint asset.
        variable_name:  the member variable (must be blueprint-added, i.e. in list_member_variable_names).
        flags:          dict of any of instance_editable / expose_on_spawn / expose_to_cinematics /
                        private / blueprint_read_only / config -> bool. In THIS build only the first
                        three have a BlueprintEditorLibrary setter and are applied; private /
                        blueprint_read_only / config are captured + echoed but reported as 'unsupported'
                        (a later C++ setter over FBPVariableDescription PropertyFlags will enable them).
        compile:        recompile once after applying (default True).

        Prior flag values are captured via the SHIPPED C++ getter
        MCPReflectionLibrary.get_blueprint_variable_flags_json, then applied via
        BlueprintEditorLibrary.set_blueprint_variable_{instance_editable,expose_on_spawn,
        expose_to_cinematics} inside a ScopedEditorTransaction, then ONE compile + save.

        Ledgered write op 'set_bp_var_flags' {blueprint_path, variable_name, prior:{flag:bool}} where
        prior holds ONLY the settable flags that were changed. Inverse (FAITHFUL; in editor_level.undo):
          bp = EditorAssetLibrary.load_asset(blueprint_path); BEL = unreal.BlueprintEditorLibrary
          with unreal.ScopedEditorTransaction('MCP undo set_bp_var_flags'):
            if 'instance_editable' in prior:     BEL.set_blueprint_variable_instance_editable(bp, unreal.Name(variable_name), bool(prior['instance_editable']))
            if 'expose_on_spawn' in prior:       BEL.set_blueprint_variable_expose_on_spawn(bp, unreal.Name(variable_name), bool(prior['expose_on_spawn']))
            if 'expose_to_cinematics' in prior:  BEL.set_blueprint_variable_expose_to_cinematics(bp, unreal.Name(variable_name), bool(prior['expose_to_cinematics']))
          BEL.compile_blueprint(bp); EditorAssetLibrary.save_asset(blueprint_path, only_if_is_dirty=False)"""
        params = {"blueprint_path": blueprint_path, "variable_name": variable_name,
                  "flags": flags or {}, "compile": compile}
        try:
            return json.dumps(_exec(_SET_VAR_FLAGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # search_types  (READ)                                               #
    # ================================================================== #
    _SEARCH_TYPES_BODY = _BPA + r'''
query = (PARAMS.get("query") or "").strip()
kind = (PARAMS.get("kind") or "any").strip().lower()
include_engine = bool(PARAMS.get("include_engine"))
max_results = PARAMS.get("max_results")
cursor = int(PARAMS.get("cursor") or 0)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
def _keep_path(pkg):
    if include_engine:
        return True
    return str(pkg).startswith("/Game")
def _match(nm):
    if not query:
        return True
    return query.lower() in (nm or "").lower()
rows = []
KINDS = {"class": ["Blueprint"], "struct": ["UserDefinedStruct"], "enum": ["UserDefinedEnum"]}
which = []
if kind in ("class", "struct", "enum"):
    which = [kind]
else:
    which = ["class", "struct", "enum"]
for kd in which:
    cnames = KINDS[kd]
    try:
        assets = ar.get_assets(unreal.ARFilter(class_names=cnames, recursive_classes=True)) or []
    except Exception:
        assets = []
    for a in assets:
        nm = str(a.asset_name); pkg = str(a.package_name)
        if not _keep_path(pkg):
            continue
        if not _match(nm):
            continue
        entry = {"kind": kd, "name": nm, "path": pkg + "." + nm, "package": pkg}
        if kd == "class":
            gc = a.get_tag_value("GeneratedClass")
            pc = a.get_tag_value("ParentClass")
            entry["generated_class_path"] = str(gc) if gc else None
            entry["parent_class"] = (str(pc).split(".")[-1].split(":")[-1].strip("'") if pc else None)
            bt = a.get_tag_value("BlueprintType")
            entry["blueprint_type"] = str(bt) if bt else None
        rows.append(entry)
def _rank(r):
    nm = (r.get("name") or "").lower(); q = query.lower()
    if not q:
        return (3, nm)
    if nm == q: return (0, nm)
    if nm.startswith(q): return (1, nm)
    return (2, nm)
rows.sort(key=_rank)
total = len(rows)
window = rows[cursor:cursor + int(max_results)] if max_results else rows[cursor:]
nxt = cursor + len(window)
print("@@UMCP@@" + json.dumps({"status": "success", "query": query, "kind": kind,
    "include_engine": include_engine, "total_matched": total, "returned": len(window),
    "next_cursor": (nxt if nxt < total else None), "types": window,
    "note": "AssetRegistry search over Blueprint-generated classes (kind=class), UserDefinedStruct "
            "(kind=struct) and UserDefinedEnum (kind=enum). NATIVE C++ classes/structs/enums are NOT "
            "enumerable from stock Python and are omitted here -- a later C++ GetTypeRegistryJson handler "
            "will add native-type enumeration. include_engine=False (default) restricts to /Game."}))
'''

    @mcp.tool()
    def search_types(ctx, query: str = "", kind: str = "any", include_engine: bool = False,
                     max_results: int = None, cursor: int = 0) -> str:
        """Search asset-backed types by name via the AssetRegistry. Read-only.

        query:          case-insensitive substring on the type name (empty lists all; also ranks results).
        kind:           class | struct | enum | any (default any). 'class' = Blueprint-generated classes,
                        'struct' = UserDefinedStruct, 'enum' = UserDefinedEnum.
        include_engine: include /Engine, /Script and plugin content (default False: only /Game).
        max_results/cursor: paginate; response returns 'next_cursor' (pass back as cursor) or null.

        NOTE: native C++ class/struct/enum enumeration is Python-limited and NOT covered here -- this
        searches asset-backed types only (Blueprint classes + UserDefinedStruct/Enum). A later C++
        GetTypeRegistryJson will upgrade this to include native types. No ledger (read-only)."""
        params = {"query": query, "kind": kind, "include_engine": include_engine,
                  "max_results": max_results, "cursor": cursor}
        try:
            return json.dumps(_exec(_SEARCH_TYPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # search_parent_classes  (READ)                                     #
    # ================================================================== #
    _SEARCH_PARENTS_BODY = _BPA + r'''
class_name = (PARAMS.get("class_name") or "").strip()
if not class_name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no class_name given"}))
else:
    # resolve to a Class instance: native name, Foo_C generated name, or a bare Blueprint asset name.
    resolved = _resolve_class(class_name)
    resolved_via = "native/getattr-or-generated" if resolved is not None else None
    if resolved is None:
        # try as a bare blueprint asset name -> its generated class
        try:
            ar = unreal.AssetRegistryHelpers.get_asset_registry()
            for a in (ar.get_assets(unreal.ARFilter(class_names=["Blueprint"], recursive_classes=True)) or []):
                if str(a.asset_name) == class_name:
                    bp = _EAL.load_asset(str(a.package_name) + "." + class_name)
                    if bp is not None:
                        resolved = _gen(bp); resolved_via = "blueprint-asset-name"
                        break
        except Exception:
            resolved = None
    if resolved is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not resolve class '%s' (tried native name, Foo_C generated name, and "
                       "Blueprint asset name)" % class_name}))
    else:
        chain = _inherit_chain(resolved)
        ancestors = chain[1:] if len(chain) > 1 else []
        rname = chain[0]["name"] if chain else resolved.get_name()
        # blueprint children via AssetRegistry: BPs whose ParentClass/NativeParentClass name == rname
        # (compare against both the resolved class name and, for a generated class, its Foo (no _C) base)
        targets = set([rname])
        if rname.endswith("_C"):
            targets.add(rname[:-2])
        children = []
        try:
            ar = unreal.AssetRegistryHelpers.get_asset_registry()
            for a in (ar.get_assets(unreal.ARFilter(class_names=["Blueprint"], recursive_classes=True)) or []):
                def _short(tag):
                    if not tag:
                        return None
                    s = str(tag)
                    if "'" in s:
                        parts = s.split("'")
                        if len(parts) >= 2 and parts[1]:
                            s = parts[1]
                    return s.split(".")[-1].split(":")[-1].strip("'").rstrip("_C") if s else None
                pc = a.get_tag_value("ParentClass")
                npc = a.get_tag_value("NativeParentClass")
                pcs = None; npcs = None
                if pc:
                    s = str(pc)
                    if "'" in s:
                        s = s.split("'")[1] if len(s.split("'")) >= 2 else s
                    pcs = s.split(".")[-1].split(":")[-1].strip("'")
                if npc:
                    s = str(npc)
                    if "'" in s:
                        s = s.split("'")[1] if len(s.split("'")) >= 2 else s
                    npcs = s.split(".")[-1].split(":")[-1].strip("'")
                hit = None
                for t in targets:
                    if pcs == t or (pcs and pcs.rstrip("_C") == t):
                        hit = "immediate"; break
                    if npcs == t:
                        hit = "native"; break
                if hit:
                    children.append({"name": str(a.asset_name),
                                     "path": str(a.package_name) + "." + str(a.asset_name),
                                     "relation": ("direct-child" if hit == "immediate" else "descends-from-native")})
        except Exception:
            children = []
        children.sort(key=lambda c: (c.get("name") or "").lower())
        print("@@UMCP@@" + json.dumps({"status": "success", "class_name": class_name,
            "resolved": {"name": rname, "path": (chain[0].get("path") if chain else None),
                         "is_native": (chain[0].get("is_native") if chain else None),
                         "resolved_via": resolved_via},
            "ancestors": ancestors, "ancestor_count": len(ancestors),
            "blueprint_children": children, "blueprint_child_count": len(children),
            "note": "Ancestry walks the resolved class upward via class metadata (native ancestors "
                    "resolve fully; a Blueprint parent resolves through the AssetRegistry). "
                    "'blueprint_children' are Blueprint assets whose ParentClass (direct) or "
                    "NativeParentClass matches -- Blueprint children only (native subclasses are not "
                    "AssetRegistry-enumerable). Read-only."}))
'''

    @mcp.tool()
    def search_parent_classes(ctx, class_name: str) -> str:
        """Walk a class's ancestry and list its known Blueprint children. Read-only.

        class_name: a native class name (e.g. 'Actor', 'Character'), a generated-class name
                    (e.g. 'BP_Foo_C'), or a Blueprint asset name (e.g. 'BP_Foo').

        Returns the resolved class, its ordered 'ancestors' (each name/path/is_native, walked via class
        metadata), and 'blueprint_children' (Blueprint assets whose immediate ParentClass or
        NativeParentClass matches, via the AssetRegistry). NOTE: unreal.Class has no get_super_class() in
        this build, so the walk uses MCPReflectionLibrary.get_class_metadata_json + name resolution;
        native subclasses are not AssetRegistry-enumerable so only Blueprint children are listed.
        No ledger (read-only)."""
        params = {"class_name": class_name}
        try:
            return json.dumps(_exec(_SEARCH_PARENTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_blueprint_function_library  (WRITE; op create_asset)       #
    # ================================================================== #
    _CREATE_FUNCLIB_BODY = _BPA + r'''
name = (PARAMS.get("name") or "").strip()
pkg_path = (PARAMS.get("path") or "").strip().rstrip("/")
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no name given"}))
elif not pkg_path or not pkg_path.startswith("/"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "path must be a valid content dir starting with a mounted root (e.g. /Game/...)"}))
else:
    asset_path = pkg_path + "/" + name
    if _EAL.does_asset_exist(asset_path):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "an asset already exists at %s" % asset_path}))
    else:
        dir_existed = False
        try:
            dir_existed = _EAL.does_directory_exist(pkg_path)
        except Exception:
            dir_existed = True
        bp = None; cerr = None
        try:
            bp = _BEL.create_blueprint_asset_with_parent(asset_path, unreal.BlueprintFunctionLibrary)
        except Exception as e:
            cerr = str(e)[:200]
        if bp is None:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "create_blueprint_asset_with_parent returned None (%s)" % (cerr or "no exception")}))
        else:
            compiled = None
            if do_compile:
                try:
                    compiled = bool(_BEL.compile_blueprint(bp))
                except Exception as e:
                    compiled = "error: %s" % e
            try:
                _EAL.save_asset(asset_path, only_if_is_dirty=False)
            except Exception:
                pass
            bt = _asset_tag(asset_path, "BlueprintType")
            is_funclib = (str(bt) == "BPTYPE_FunctionLibrary")
            _ledger().append({"op": "create_asset", "asset_path": asset_path,
                "package_path": pkg_path, "created_dir": (pkg_path if not dir_existed else None)})
            result = {"status": "success", "asset_path": asset_path,
                "blueprint_type": bt, "is_function_library": is_funclib,
                "compiled": compiled, "ledger_depth": len(_ledger())}
            if not is_funclib:
                result["warning"] = ("asset created but BlueprintType is %s, NOT BPTYPE_FunctionLibrary -- "
                    "a C++ typed-Blueprint creator (setting BlueprintType directly) is needed to guarantee "
                    "the function-library type; the coordinator can add it. The asset is still created and "
                    "ledgered (create_asset) so it can be undone." % bt)
            result["note"] = ("Created via BlueprintEditorLibrary.create_blueprint_asset_with_parent with parent "
                "unreal.BlueprintFunctionLibrary; type verified via the AssetRegistry BlueprintType tag. "
                "Ledgered op 'create_asset' {asset_path, package_path, created_dir}; inverse deletes the asset "
                "(reuses editor_level's generic create_asset inverse).")
            print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def create_blueprint_function_library(ctx, name: str, path: str = "/Game/MCP_Scratch",
                                          compile: bool = True) -> str:
        """Create a BlueprintFunctionLibrary-typed Blueprint asset (ledgered write).

        name:    asset name (no path, no extension).
        path:    content directory to create it under (default '/Game/MCP_Scratch'); must start with a
                 mounted root.
        compile: compile the new blueprint after creation (default True).

        Creates via BlueprintEditorLibrary.create_blueprint_asset_with_parent with parent
        unreal.BlueprintFunctionLibrary, then VERIFIES the resulting BlueprintType == BPTYPE_FunctionLibrary
        (read from the AssetRegistry 'BlueprintType' tag). If it lands as a different type, the response
        carries an honest 'warning' that a C++ typed-BP creator is needed -- the asset is still created and
        ledgered. (Verified live: create_blueprint_asset_with_parent DOES land BPTYPE_FunctionLibrary.)

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}; the inverse (already in
        editor_level.undo -- the shipped generic create_asset inverse) deletes the created asset (and the
        created_dir if it was empty)."""
        params = {"name": name, "path": path, "compile": compile}
        try:
            return json.dumps(_exec(_CREATE_FUNCLIB_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
