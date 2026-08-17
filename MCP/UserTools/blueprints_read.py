"""UserTools :: Blueprints (READ)  (spec: docs/spec/blueprints.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO blueprint/asset editors are ever opened (no modals) — everything here is AssetRegistry
tag reads plus reflection over the loaded Blueprint object, its generated class, and the
generated class's CDO. Query convention + base64 PARAMS + Output-Log capture are copied
verbatim from editor_level.py (the gold standard).

What IS reachable from stock Python in this build (verified live):
  * AssetRegistry enumerates `Blueprint` assets (recursive_classes catches WidgetBlueprint,
    AnimBlueprint, etc.). Its tags give NativeParentClass, ParentClass, GeneratedClass and
    BlueprintType with NO asset load -> fast listing/filtering.
  * `unreal.BlueprintEditorLibrary` is richly extended here: generated_class(bp),
    get_blueprint_parent_class(bp), list_member_variable_names(bp), list_functions(bp),
    list_events(bp), list_graph_names(bp), list_event_dispatchers(bp). list_functions/
    list_events return `BlueprintFunctionInfo` structs carrying {name, description,
    is_implemented} — is_implemented=True marks functions/events actually implemented in THIS
    blueprint (vs merely inherited/overridable).
  * `unreal.MCPReflectionLibrary.get_object_property_metadata_json(<generated CDO>)` returns
    real per-property metadata: {name, cpp_type, owner_class, category, tooltip, flags[]}.
    Blueprint-added variables surface here as UPROPERTYs on the generated class; owner_class ==
    the generated class name distinguishes BP-added from inherited. THIS is the good use the
    spec pointed at, and it works (unlike for UStructs, where it cannot nativize a struct type).

Known limits (reported honestly in payloads, NOT hidden):
  * SCS-added components are NOT instantiated on the CDO (the SimpleConstructionScript only runs
    at spawn), so get_components_by_class(CDO) returns only the inherited/native default
    subobjects (with a real attach hierarchy). BP-added components still surface as component-
    typed UPROPERTYs via the CDO metadata (owner_class == generated class), so we list them from
    there and merge in the live attach parent where a CDO instance exists. The SCS node graph /
    attach hierarchy for BP-added components is not exposed to Python (would need a C++ handler
    over USimpleConstructionScript / USCS_Node).
  * BP function-GRAPH internals (nodes, wiring, local variables, per-function inputs/outputs)
    are NOT reachable from Python here (that is editor-only Kismet API). We surface graph names
    and which functions/events are implemented, not their node graphs.

Implemented (all read-only):
  - find_blueprints           (AssetRegistry Blueprint assets; rich filters; tag-derived parents)
  - list_blueprints           (lightweight path-scoped listing; same source)
  - get_blueprint_info        (generated class, parent, kind, variable/function/component counts)
  - list_blueprint_components (component-typed UPROPERTYs + live CDO attach hierarchy)
  - list_blueprint_variables  (MCPReflectionLibrary CDO metadata: cpp_type/category/flags/owner)
  - list_blueprint_functions  (graphs + implemented functions/events; overridable when asked)

Deferred (writes; NOT implemented this batch): create_blueprint, compile_blueprint,
  reparent_blueprint, create_blueprint_variable / set_blueprint_variable_properties /
  delete_blueprint_variable, create_blueprint_function / add_function_input/output /
  delete/rename/override function, add/delete/reparent component, build/apply/arrange graph,
  add/connect/delete nodes, set pin defaults, interfaces & dispatchers. These require the
  editor-only Kismet / BlueprintEditor / K2 graph API (FBlueprintEditorUtils,
  FKismetEditorUtilities, USimpleConstructionScript editing) -> a C++ handler, plus
  ScopedEditorTransaction + per-session ledger inverse + recompile/auto-save. No ledger / no
  `undo` tool here (this batch performs no mutations).
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

    # Shared Unreal-side helpers for blueprints. No triple-single-quote / no backslash here.
    #   _match(s,pat)        -> substring/glob (case-insensitive) name match.
    #   _clsname(tag)        -> (short, full): parse a NativeParentClass-style tag value.
    #   _load_bp(path)       -> (bp, err): load + verify it is a Blueprint.
    #   _gen(bp)             -> generated UClass (or None).
    #   _cdo(gcls)           -> generated class default object (or None).
    #   _meta(cdo)           -> parsed CDO property metadata via MCPReflectionLibrary (or None).
    #   _fninfo(struct)      -> {name, is_implemented, description} from a BlueprintFunctionInfo.
    _BP_HELPERS = r'''
import unreal, json, warnings, fnmatch
warnings.simplefilter("ignore")
_BEL = unreal.BlueprintEditorLibrary
_HAS_MRL = hasattr(unreal, "MCPReflectionLibrary")
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _clsname(tag):
    if not tag:
        return (None, None)
    s = str(tag)
    if "'" in s:
        parts = s.split("'")
        if len(parts) >= 2 and parts[1]:
            s = parts[1]
    short = s.split(".")[-1].split(":")[-1]
    return (short, s)
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
    if cdo is None or not _HAS_MRL:
        return None
    try:
        js = _MRL.get_object_property_metadata_json(cdo)
        d = json.loads(js) if isinstance(js, str) else js
        if isinstance(d, dict):
            return d
    except Exception:
        return None
    return None
def _fninfo(s):
    r = {}
    try:
        r["name"] = str(s.get_editor_property("name"))
    except Exception:
        r["name"] = None
    try:
        r["is_implemented"] = bool(s.get_editor_property("is_implemented"))
    except Exception:
        r["is_implemented"] = None
    try:
        d = str(s.get_editor_property("description")) if s.get_editor_property("description") else ""
        r["description"] = d
    except Exception:
        r["description"] = ""
    return r
def _parent_short_full(bp):
    try:
        pc = _BEL.get_blueprint_parent_class(bp)
        if pc is not None:
            return (pc.get_name(), pc.get_path_name())
    except Exception:
        pass
    return (None, None)
'''

    # ------------------------------------------------------------------ #
    # find_blueprints — AssetRegistry Blueprint assets (tag-derived meta)  #
    # ------------------------------------------------------------------ #
    _FIND_BODY = _BP_HELPERS + r'''
filt = PARAMS.get("filter")
path_filter = PARAMS.get("path_filter")
parent_filter = PARAMS.get("parent_class")
bp_type_filter = PARAMS.get("blueprint_type")
max_results = PARAMS.get("max_results")
cursor = int(PARAMS.get("cursor") or 0)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
try:
    assets = ar.get_assets(unreal.ARFilter(class_names=["Blueprint"], recursive_classes=True))
except Exception:
    assets = []
rows = []
seen = set()
for a in assets:
    nm = str(a.asset_name); pkg = str(a.package_name)
    full = pkg + "." + nm
    if full in seen:
        continue
    if filt and not _match(nm, filt):
        continue
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    npc_short, npc_full = _clsname(a.get_tag_value("NativeParentClass"))
    pc_short, pc_full = _clsname(a.get_tag_value("ParentClass"))
    gc_short, gc_full = _clsname(a.get_tag_value("GeneratedClass"))
    bptype = a.get_tag_value("BlueprintType")
    bptype = str(bptype) if bptype else None
    asset_cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
    if parent_filter:
        pf = parent_filter.lower()
        hay = " ".join([x for x in [pc_short, pc_full, npc_short, npc_full] if x]).lower()
        if pf not in hay:
            continue
    if bp_type_filter and (not bptype or bp_type_filter.lower() not in bptype.lower()):
        continue
    seen.add(full)
    rank = 3
    if filt:
        fl = filt.lower(); nl = nm.lower()
        if nl == fl: rank = 0
        elif nl.startswith(fl): rank = 1
        else: rank = 2
    rows.append({"name": nm, "path": full, "package": pkg,
                 "asset_class": asset_cls,
                 "blueprint_type": bptype,
                 "parent_class": pc_short, "parent_class_path": pc_full,
                 "native_parent_class": npc_short,
                 "generated_class_path": gc_full, "_rank": rank})
rows.sort(key=lambda r: (r["_rank"], r["name"].lower()))
for r in rows:
    r.pop("_rank", None)
total = len(rows)
window = rows[cursor:cursor + int(max_results)] if max_results else rows[cursor:]
nxt = cursor + len(window)
print("@@UMCP@@" + json.dumps({"status": "success", "class": "Blueprint",
    "total": total, "returned": len(window),
    "next_cursor": (nxt if nxt < total else None), "blueprints": window,
    "note": "Parent info is read from AssetRegistry tags (no asset load). 'parent_class' is the "
            "immediate parent (may itself be a Blueprint class), 'native_parent_class' is the "
            "nearest C++ ancestor. recursive_classes=True also lists WidgetBlueprint/AnimBlueprint/"
            "etc. (see asset_class)."}))
'''

    @mcp.tool()
    def find_blueprints(ctx, filter: str = None, path_filter: str = None,
                        parent_class: str = None, blueprint_type: str = None,
                        max_results: int = None, cursor: int = 0) -> str:
        """Find Blueprint assets via the AssetRegistry (fast; no asset load). Read-only.

        filter:         case-insensitive substring/glob on the Blueprint name (also ranks results).
        path_filter:    case-insensitive substring on the package path (e.g. '/Game/ThirdPerson').
        parent_class:   keep only Blueprints whose immediate or native parent class name/path
                        contains this (e.g. 'Character', 'Actor', 'UserWidget').
        blueprint_type: filter on the BlueprintType tag (e.g. 'Normal', 'Interface', 'Const',
                        'MacroLibrary', 'FunctionLibrary').
        max_results/cursor: paginate; response returns 'next_cursor' (pass back as cursor) or null.

        Each row carries name, path, asset_class (Blueprint/WidgetBlueprint/...), blueprint_type,
        immediate parent_class (+path), native_parent_class, and generated_class_path — all from
        AssetRegistry tags."""
        params = {"filter": filter, "path_filter": path_filter, "parent_class": parent_class,
                  "blueprint_type": blueprint_type, "max_results": max_results, "cursor": cursor}
        try:
            return json.dumps(_exec(_FIND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_blueprints — lightweight path-scoped listing (same source)     #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _BP_HELPERS + r'''
package_path = PARAMS.get("package_path") or "/Game"
recursive = PARAMS.get("recursive")
recursive = True if recursive is None else bool(recursive)
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
try:
    flt = unreal.ARFilter(class_names=["Blueprint"], recursive_classes=True,
                          package_paths=[package_path], recursive_paths=recursive)
    assets = ar.get_assets(flt)
except Exception as e:
    assets = []
rows = []
seen = set()
for a in assets:
    nm = str(a.asset_name); pkg = str(a.package_name)
    full = pkg + "." + nm
    if full in seen:
        continue
    seen.add(full)
    pc_short, _pc_full = _clsname(a.get_tag_value("ParentClass"))
    bptype = a.get_tag_value("BlueprintType")
    rows.append({"name": nm, "path": full, "parent_class": pc_short,
                 "blueprint_type": (str(bptype) if bptype else None)})
rows.sort(key=lambda r: r["name"].lower())
total = len(rows)
if max_results:
    rows = rows[:int(max_results)]
print("@@UMCP@@" + json.dumps({"status": "success", "package_path": package_path,
    "recursive": recursive, "total": total, "returned": len(rows), "blueprints": rows}))
'''

    @mcp.tool()
    def list_blueprints(ctx, package_path: str = "/Game", recursive: bool = True,
                        max_results: int = None) -> str:
        """List Blueprint assets under a content path (fast; no asset load). Read-only.

        package_path: content root to scan (default '/Game'); e.g. '/Game/ThirdPerson/Blueprints'.
        recursive:    recurse into sub-paths (default True).
        max_results:  cap the number returned.

        A lightweight companion to find_blueprints: each row is name, path, parent_class,
        blueprint_type. Use find_blueprints for name/parent/type filtering and pagination."""
        params = {"package_path": package_path, "recursive": recursive, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_blueprint_info — generated class, parent, kind, counts          #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _BP_HELPERS + r'''
path = PARAMS.get("blueprint_path")
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    gpath = gcls.get_path_name() if gcls else None
    p_short, p_full = _parent_short_full(bp)
    cdo = _cdo(gcls)
    meta = _meta(cdo)
    # variable counts from CDO metadata (owner_class distinguishes BP-added from inherited)
    total_vars = None; own_vars = None
    if meta is not None:
        props = meta.get("properties", []) or []
        total_vars = len(props)
        own_vars = len([p for p in props
                        if p.get("owner_class") == gname and p.get("name") != "UberGraphFrame"])
    # function/event counts (is_implemented == this BP)
    impl_fn = None; impl_ev = None; graph_names = None; dispatchers = None
    try:
        fns = _BEL.list_functions(bp)
        impl_fn = len([f for f in fns if bool(f.get_editor_property("is_implemented"))])
    except Exception:
        impl_fn = None
    try:
        evs = _BEL.list_events(bp)
        impl_ev = len([e for e in evs if bool(e.get_editor_property("is_implemented"))])
    except Exception:
        impl_ev = None
    try:
        graph_names = [str(x) for x in _BEL.list_graph_names(bp)]
    except Exception:
        graph_names = None
    try:
        dispatchers = [str(x) for x in _BEL.list_event_dispatchers(bp)]
    except Exception:
        dispatchers = None
    # components: merge component-typed UPROPERTYs with live CDO default-subobjects, exactly
    # like list_blueprint_components (so the count here matches that listing).
    comp_total = None; comp_own = None
    comp_prop_names = set()
    if meta is not None:
        cprops = [p for p in (meta.get("properties", []) or [])
                  if p.get("cpp_type", "").endswith("Component*")]
        comp_prop_names = set(p.get("name") for p in cprops)
        comp_own = len([p for p in cprops if p.get("owner_class") == gname])
        comp_total = len(cprops)
    live_names = set()
    if cdo is not None:
        try:
            for c in (cdo.get_components_by_class(unreal.ActorComponent) or []):
                live_names.add(c.get_name())
        except Exception:
            live_names = set()
    if comp_total is not None:
        comp_total = len(comp_prop_names | live_names)
    elif live_names:
        comp_total = len(live_names)
    result = {"status": "success",
              "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
              "asset_class": bp.get_class().get_name(),
              "generated_class": gname, "generated_class_path": gpath,
              "parent_class": p_short, "parent_class_path": p_full,
              "graphs": graph_names, "event_dispatchers": dispatchers,
              "counts": {
                  "variables_total_uproperties": total_vars,
                  "variables_blueprint_added": own_vars,
                  "functions_implemented": impl_fn,
                  "events_implemented": impl_ev,
                  "component_properties_total": comp_total,
                  "component_properties_blueprint_added": comp_own},
              "reflection": {"mcp_reflection_library": _HAS_MRL},
              "note": "Counts come from reflection: variables via MCPReflectionLibrary CDO metadata "
                      "(owner_class == generated class => blueprint-added, else inherited); "
                      "functions/events via BlueprintEditorLibrary is_implemented (this BP). "
                      "BP function-graph internals (nodes/wiring/locals) are editor-only and not "
                      "reachable from Python."}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_blueprint_info(ctx, blueprint_path: str) -> str:
        """Summarize a Blueprint asset (loads it; no editor opened). Read-only.

        blueprint_path: object path, e.g.
                        '/Game/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter'.

        Returns generated class (name + path), parent class (name + path), asset class
        (Blueprint/WidgetBlueprint/AnimBlueprint/...), the list of graph names and event
        dispatchers, and counts: total reflected UPROPERTYs, blueprint-added variables,
        implemented functions, implemented events, and component-typed properties (total +
        blueprint-added). See list_blueprint_* for the details behind each count."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_blueprint_components — component UPROPERTYs + live CDO hierarchy #
    # ------------------------------------------------------------------ #
    _COMPONENTS_BODY = _BP_HELPERS + r'''
path = PARAMS.get("blueprint_path")
blueprint_added_only = bool(PARAMS.get("blueprint_added_only"))
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    meta = _meta(cdo)
    # 1) live component instances on the CDO (inherited/native default subobjects) -> attach map
    live = {}
    cdo_component_error = None
    if cdo is not None:
        try:
            for c in (cdo.get_components_by_class(unreal.ActorComponent) or []):
                pa = None
                try:
                    p = c.get_attach_parent()
                    pa = p.get_name() if p else None
                except Exception:
                    pa = None
                live[c.get_name()] = {"class": c.get_class().get_name(), "attach_parent": pa}
        except Exception as e:
            cdo_component_error = str(e)
    # 2) component-typed UPROPERTYs from CDO metadata (covers BP-added SCS components too)
    comps = []
    prop_names = set()
    if meta is not None:
        for p in (meta.get("properties", []) or []):
            cpp = p.get("cpp_type", "")
            if not cpp.endswith("Component*"):
                continue
            nm = p.get("name")
            prop_names.add(nm)
            is_own = (p.get("owner_class") == gname)
            if blueprint_added_only and not is_own:
                continue
            cls_from_cpp = cpp[:-1]
            if cls_from_cpp.startswith("U"):
                cls_from_cpp = cls_from_cpp[1:]
            liveinfo = live.get(nm)
            comps.append({
                "name": nm,
                "class": (liveinfo["class"] if liveinfo else cls_from_cpp),
                "attach_parent": (liveinfo["attach_parent"] if liveinfo else None),
                "owner_class": p.get("owner_class"),
                "blueprint_added": is_own,
                "category": p.get("category"),
                "instanced_on_cdo": liveinfo is not None,
                "flags": p.get("flags")})
    # any live components that had no matching UPROPERTY (defensive)
    for nm, info in live.items():
        if nm in prop_names:
            continue
        if blueprint_added_only:
            continue
        comps.append({"name": nm, "class": info["class"], "attach_parent": info["attach_parent"],
                      "owner_class": None, "blueprint_added": False, "category": None,
                      "instanced_on_cdo": True, "flags": None})
    comps.sort(key=lambda c: (0 if c.get("attach_parent") is None else 1, c["name"].lower()))
    result = {"status": "success",
              "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
              "generated_class": gname,
              "component_count": len(comps),
              "components": comps,
              "note": "Components are listed from the generated class's component-typed UPROPERTYs "
                      "(MCPReflectionLibrary CDO metadata). 'instanced_on_cdo' components are the "
                      "inherited/native default subobjects and carry a real attach_parent. "
                      "'blueprint_added' components come from the SimpleConstructionScript, which "
                      "does NOT run on the CDO, so their attach_parent/SCS hierarchy is not "
                      "reachable from Python (would need a C++ USimpleConstructionScript handler)."}
    if cdo_component_error:
        result["cdo_component_error"] = cdo_component_error
    if meta is None:
        result["warning"] = ("No CDO metadata: MCPReflectionLibrary unavailable or CDO not "
                             "resolvable. Only inherited live components (if any) are shown.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_blueprint_components(ctx, blueprint_path: str,
                                  blueprint_added_only: bool = False) -> str:
        """List a Blueprint's components (name, class, attach parent, owner). Read-only.

        blueprint_path:       object path of the Blueprint asset.
        blueprint_added_only: only components declared in this Blueprint (its SCS), excluding
                              inherited/native components.

        Components are enumerated from the generated class's component-typed UPROPERTYs. Each entry
        reports blueprint_added (owner_class == this generated class) and instanced_on_cdo.
        IMPORTANT: inherited/native default subobjects are instantiated on the CDO and carry a real
        attach_parent; blueprint-added (SimpleConstructionScript) components are NOT instantiated on
        the CDO, so their attach hierarchy is not reachable from Python (reported honestly)."""
        params = {"blueprint_path": blueprint_path, "blueprint_added_only": blueprint_added_only}
        try:
            return json.dumps(_exec(_COMPONENTS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_blueprint_variables — MCPReflectionLibrary CDO metadata         #
    # ------------------------------------------------------------------ #
    _VARIABLES_BODY = _BP_HELPERS + r'''
path = PARAMS.get("blueprint_path")
include_inherited = bool(PARAMS.get("include_inherited"))
include_components = bool(PARAMS.get("include_components"))
filt = PARAMS.get("filter")
max_results = PARAMS.get("max_results")
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    gcls = _gen(bp)
    gname = gcls.get_name() if gcls else None
    cdo = _cdo(gcls)
    meta = _meta(cdo)
    if meta is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "CDO property metadata unavailable (MCPReflectionLibrary missing or CDO "
                       "unresolvable); cannot enumerate variables with types.",
            "blueprint": bp.get_name(),
            "mcp_reflection_library": _HAS_MRL}))
    else:
        props = meta.get("properties", []) or []
        rows = []
        for p in props:
            nm = p.get("name")
            if nm == "UberGraphFrame":
                continue
            cpp = p.get("cpp_type", "")
            is_component = cpp.endswith("Component*")
            if is_component and not include_components:
                continue
            is_own = (p.get("owner_class") == gname)
            if not include_inherited and not is_own:
                continue
            if filt and not _match(nm, filt):
                continue
            rows.append({"name": nm, "cpp_type": cpp, "category": p.get("category"),
                         "owner_class": p.get("owner_class"), "blueprint_added": is_own,
                         "is_component": is_component, "flags": p.get("flags"),
                         "tooltip": p.get("tooltip")})
        rows.sort(key=lambda r: (0 if r["blueprint_added"] else 1, r["name"].lower()))
        total = len(rows)
        if max_results:
            rows = rows[:int(max_results)]
        print("@@UMCP@@" + json.dumps({"status": "success",
            "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
            "generated_class": gname,
            "include_inherited": include_inherited,
            "returned": len(rows), "total_matched": total, "variables": rows,
            "source": "MCPReflectionLibrary.get_object_property_metadata_json(generated CDO)",
            "note": "Blueprint variables surface as UPROPERTYs on the generated class; cpp_type/"
                    "category/flags/owner_class are REAL (not inferred). blueprint_added == "
                    "owner_class matches this generated class. By default only blueprint-added, "
                    "non-component properties are shown (include_inherited / include_components to "
                    "widen). Per-function LOCAL variables are NOT reachable from Python (editor-only)."}))
'''

    @mcp.tool()
    def list_blueprint_variables(ctx, blueprint_path: str, include_inherited: bool = False,
                                 include_components: bool = False, filter: str = None,
                                 max_results: int = None) -> str:
        """List a Blueprint's member variables with real types. Read-only.

        blueprint_path:     object path of the Blueprint asset.
        include_inherited:  also list variables inherited from parent classes (default False:
                            only variables added in this Blueprint).
        include_components: also include component-typed properties (default False; see
                            list_blueprint_components for those).
        filter:             case-insensitive substring/glob on the variable name.
        max_results:        cap the number returned.

        Each variable carries name, cpp_type, category, owner_class, blueprint_added, flags
        (Edit/BlueprintVisible/...), and tooltip. Types are REAL FProperty metadata via
        unreal.MCPReflectionLibrary.get_object_property_metadata_json(<generated CDO>), not
        inferred. NOTE: per-function local variables are editor-only and not reachable here."""
        params = {"blueprint_path": blueprint_path, "include_inherited": include_inherited,
                  "include_components": include_components, "filter": filter,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_VARIABLES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_blueprint_functions — graphs + implemented functions/events    #
    # ------------------------------------------------------------------ #
    _FUNCTIONS_BODY = _BP_HELPERS + r'''
path = PARAMS.get("blueprint_path")
include_inherited = bool(PARAMS.get("include_inherited"))
include_events = PARAMS.get("include_events")
include_events = True if include_events is None else bool(include_events)
with_descriptions = bool(PARAMS.get("with_descriptions"))
filt = PARAMS.get("filter")
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    try:
        graph_names = [str(x) for x in _BEL.list_graph_names(bp)]
    except Exception as e:
        graph_names = None
    def _collect(structs):
        out = []
        for s in (structs or []):
            info = _fninfo(s)
            if info.get("name") is None:
                continue
            if not include_inherited and not info.get("is_implemented"):
                continue
            if filt and not _match(info.get("name"), filt):
                continue
            rec = {"name": info["name"], "is_implemented": info["is_implemented"]}
            if with_descriptions:
                d = info.get("description") or ""
                rec["description"] = d[:400]
            out.append(rec)
        out.sort(key=lambda r: (0 if r["is_implemented"] else 1, (r["name"] or "").lower()))
        return out
    try:
        functions = _collect(_BEL.list_functions(bp))
    except Exception as e:
        functions = None
        fn_err = str(e)
    events = None
    if include_events:
        try:
            events = _collect(_BEL.list_events(bp))
        except Exception:
            events = None
    dispatchers = None
    try:
        dispatchers = [str(x) for x in _BEL.list_event_dispatchers(bp)]
    except Exception:
        dispatchers = None
    result = {"status": "success",
              "blueprint": {"name": bp.get_name(), "path": bp.get_path_name()},
              "graphs": graph_names,
              "functions": functions,
              "events": events,
              "event_dispatchers": dispatchers,
              "include_inherited": include_inherited,
              "note": "Via BlueprintEditorLibrary.list_functions/list_events. is_implemented=True "
                      "means the function/event has a graph in THIS blueprint; is_implemented=False "
                      "entries are inherited/overridable (shown only with include_inherited=True). "
                      "'graphs' are the top-level graph names. Node-level graph contents "
                      "(nodes/pins/wiring/local vars) are editor-only and not reachable from Python."}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_blueprint_functions(ctx, blueprint_path: str, include_inherited: bool = False,
                                 include_events: bool = True, with_descriptions: bool = False,
                                 filter: str = None) -> str:
        """List a Blueprint's functions/events and graph names. Read-only.

        blueprint_path:    object path of the Blueprint asset.
        include_inherited: also list inherited/overridable (not-implemented) functions and events
                           (default False: only those actually implemented in this Blueprint).
        include_events:    include the events section (default True).
        with_descriptions: include each function/event tooltip (truncated).
        filter:            case-insensitive substring/glob on the function/event name.

        Returns 'graphs' (top-level graph names), 'functions' and 'events' (each name +
        is_implemented), and 'event_dispatchers'. is_implemented=True marks entries with a real
        graph in this Blueprint. NOTE: node-level graph contents (nodes, pins, wiring, per-function
        local variables) are editor-only Kismet data and NOT reachable from Python here."""
        params = {"blueprint_path": blueprint_path, "include_inherited": include_inherited,
                  "include_events": include_events, "with_descriptions": with_descriptions,
                  "filter": filter}
        try:
            return json.dumps(_exec(_FUNCTIONS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (writes; not implemented this batch): create_blueprint,
    #   compile_blueprint, reparent_blueprint, create/set/delete blueprint
    #   variable, create/delete/rename/override blueprint function,
    #   add/delete/reparent component, build/apply/arrange graph, add/connect/
    #   delete nodes, set pin defaults, interfaces & dispatchers.
    # These need the editor-only Kismet / BlueprintEditor / K2 graph API
    # (FBlueprintEditorUtils, FKismetEditorUtilities, USimpleConstructionScript
    # editing) -> a C++ handler, plus ScopedEditorTransaction + per-session ledger
    # inverse + recompile/auto-save. No `undo` tool is defined here (read-only batch).
