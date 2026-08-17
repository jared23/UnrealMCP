"""UserTools :: Environment Query System / EQS (READ)  (spec: docs/spec/eqs.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). READ-ONLY batch.
NO asset editors are ever opened (no modals); nothing is mutated; no ledger. This introspects
EnvQuery (EQS) assets and the EQS node CLASSES (generators / tests / contexts) that are authorable
in the project. The query convention, base64 PARAMS, and Output-Log auto-capture are copied verbatim
from editor_level.py (the gold standard), matching ai_read.py's AssetRegistry-enumeration style.

PROBE FINDINGS (verified live vs TestMCPSetup, UE 5.8.1 -- what IS / ISN'T reflectable):
  * 3 EnvQuery assets exist (all under /Game/Variant_Combat/Blueprints/AI): EnvQuery_Evade,
    EnvQuery_Fallback, EnvQuery_Flank. AssetRegistry enumerates them via
    class_paths=[TopLevelAssetPath("/Script/AIModule", "EnvQuery")] (EnvQuery is an AIModule class).
  * The runtime query STRUCTURE is stored in PROTECTED / non-BlueprintVisible C++ UPROPERTYs:
      - UEnvQuery.Options (TArray<UEnvQueryOption*>), UEnvQuery.EdGraph, UEnvQuery.QueryName
      - UEnvQueryOption.Generator, UEnvQueryOption.Tests
      - every config field on a generator/test (TestPurpose, ScoringEquation, ScoringFactor,
        FilterType, ItemType, Center-context, ...)
    Stock Python get_editor_property REFUSES all of these ("... is protected and cannot be read", or
    "Failed to find property ..." because a protected member is not exported as a Python attribute).
    unreal.MCPReflectionLibrary.get_editor_property enforces the SAME protection (no value bypass).
    So the concrete configured VALUES of a query's generators/tests/contexts are NOT readable from
    stock Python in this build -- a dedicated C++ value-reading handler would be required. This module
    does NOT fake them.
  * What IS reachable, and how this module uses it:
      - unreal.MCPReflectionLibrary.get_object_property_metadata_json(obj) reads FProperty METADATA
        (property names, cpp_type, category, tooltip, flags) for ANY object incl. protected ones --
        used to surface each node class's AUTHORABLE SCHEMA (what you CAN configure), not its values.
      - unreal.MCPReflectionLibrary.get_class_metadata_json(uclass) gives parent_class / is_abstract /
        is_deprecated / is_blueprintable.
      - unreal.find_object(query, "<ClassName>_<N>") REACHES the runtime option/generator/test
        subobjects by name (they are outered to the EnvQuery and auto-named "<Class>_<index>", e.g.
        EnvQueryOption_0, EnvQueryGenerator_Donut_0, EnvQueryTest_Distance_1). Their real CLASS names
        are then readable via get_class().get_name(). There is NO Python enumeration API
        (get_objects_with_outer / find_objects are absent), so node discovery probes the known engine
        EQS class-name list; project-authored Blueprint EQS nodes are found separately via AssetRegistry.
      - unreal.load_object(None, "/Script/AIModule.<Class>") resolves a native EQS UClass by path even
        when it is NOT exported into the unreal.* Python namespace (only the *_BlueprintBase classes
        and EnvQueryContext_NavigationData ARE in unreal.*; the native generators/tests are not).
  * KNOWN LIMITS (reported in payloads, never hidden):
      - Per-option -> node mapping is NOT recoverable (Options and each Option.Generator/Tests are
        protected; nodes are enumerated at the QUERY level, not grouped under their option).
      - Node discovery via find_object covers the STANDARD engine EQS classes probed by name only;
        a query using a non-listed custom/Blueprint node would have that node missed by the scan.
      - list_env_query_* class enumeration lists the standard engine classes that resolve under
        /Script/AIModule + any unreal.*-exported subclasses + project Blueprint subclasses; a fully
        exhaustive native subclass walk would need a C++ GetDerivedClasses handler (not Python-exposed).

Implemented (all read-only, no ledger):
  - list_env_queries          (AssetRegistry enumerate EnvQuery assets)
  - get_env_query_info         (options count + detected generator/test nodes w/ class + config schema)
  - list_env_query_generators  (authorable EnvQueryGenerator classes: engine + namespace + Blueprint)
  - list_env_query_tests       (authorable EnvQueryTest classes)
  - list_env_query_contexts    (authorable EnvQueryContext classes)
  - get_env_query_node_info    (one node class: metadata + authorable property schema from its CDO)
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

# Standard engine EQS node classes (public UE 5.x AIModule). Native generators/tests are NOT
# exported into the unreal.* Python namespace, so they are resolved by /Script/AIModule path and
# runtime instances are probed by "<Class>_<N>" name. This is the public, documented class set.
_STD_GENERATORS = [
    "EnvQueryGenerator_ActorsOfClass", "EnvQueryGenerator_Composite",
    "EnvQueryGenerator_CurrentLocation", "EnvQueryGenerator_OnCircle",
    "EnvQueryGenerator_PathingGrid", "EnvQueryGenerator_ProjectedPoints",
    "EnvQueryGenerator_SimpleGrid", "EnvQueryGenerator_Donut",
    "EnvQueryGenerator_PerceivedActors", "EnvQueryGenerator_BlueprintBase",
]
_STD_TESTS = [
    "EnvQueryTest_Distance", "EnvQueryTest_Dot", "EnvQueryTest_Trace",
    "EnvQueryTest_Pathfinding", "EnvQueryTest_PathfindingBatch", "EnvQueryTest_Overlap",
    "EnvQueryTest_Project", "EnvQueryTest_GameplayTags", "EnvQueryTest_Random",
    "EnvQueryTest_Volume", "EnvQueryTest_BlueprintBase",
]
_STD_CONTEXTS = [
    "EnvQueryContext_Querier", "EnvQueryContext_Item",
    "EnvQueryContext_BlueprintBase", "EnvQueryContext_NavigationData",
]


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
    _EQS_HELPERS = r'''
import unreal, json, inspect, warnings
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
def _refl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _class_meta(cls):
    m = _refl()
    if m is None or cls is None:
        return {}
    try:
        return json.loads(m.get_class_metadata_json(cls))
    except Exception:
        return {}
def _obj_prop_schema(obj, include_inherited=False, limit=80):
    # Property METADATA (names/types/tooltip/category) -- reachable even for protected members.
    m = _refl()
    if m is None or obj is None:
        return None
    try:
        parsed = json.loads(m.get_object_property_metadata_json(obj, include_inherited=include_inherited))
    except Exception:
        return None
    props = parsed.get("properties", []) if isinstance(parsed, dict) else []
    rows = []
    for p in props[:limit]:
        rec = {"name": p.get("name"), "cpp_type": p.get("cpp_type")}
        if p.get("category"):
            rec["category"] = p.get("category")
        tip = p.get("tooltip")
        if tip:
            rec["tooltip"] = tip[:200]
        rows.append(rec)
    return rows
def _resolve_eqs_class(name):
    # Resolve a native/exported EQS UClass: /Script/AIModule path first, then unreal.* namespace.
    if not name:
        return None, None
    obj = _try(lambda: unreal.load_object(None, "/Script/AIModule." + name))
    if obj is not None:
        return obj, "/Script/AIModule." + name
    py = getattr(unreal, name, None)
    if inspect.isclass(py) and issubclass(py, unreal.Object):
        return py, "unreal." + name
    return None, None
'''

    # ------------------------------------------------------------------ #
    # list_env_queries — AssetRegistry enumerate EnvQuery assets           #
    # ------------------------------------------------------------------ #
    _LIST_QUERIES_BODY = _EQS_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 200
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
err = None
try:
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/AIModule", "EnvQuery")],
        recursive_paths=True, package_paths=[package_path])
    for a in (ar.get_assets(flt) or []):
        pkg = str(a.get_editor_property("package_name"))
        nm = str(a.get_editor_property("asset_name"))
        rows.append({"name": nm, "path": pkg + "." + nm})
except Exception as e:
    err = str(e)
rows.sort(key=lambda r: r["path"].lower())
result = {"status": ("error" if err else "success"), "path": package_path,
          "count": len(rows), "returned": min(len(rows), max_results),
          "assets": rows[:max_results]}
if err:
    result["error"] = err
result["note"] = ("EnvQuery is a /Script/AIModule class (not /Script/Engine); enumerated via "
                  "AssetRegistry TopLevelAssetPath('/Script/AIModule','EnvQuery'). Use "
                  "get_env_query_info on a path for its option/node structure.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_env_queries(ctx, path: str = None, max_results: int = 200) -> str:
        """Enumerate EnvQuery (EQS) assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Variant_Combat'.
        max_results: cap the assets listed (default 200; true 'count' is always reported).

        Returns {count, returned, assets:[{name, path}]}. EnvQuery is an AIModule class. Follow up
        with get_env_query_info(asset_path) to read a query's options and detected generator/test
        nodes."""
        params = {"path": path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_QUERIES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_env_query_info — options + detected generator/test nodes         #
    # ------------------------------------------------------------------ #
    _QUERY_INFO_BODY = _EQS_HELPERS + r'''
path = PARAMS.get("asset_path")
max_options = int(PARAMS.get("max_options") or 32)
scan_depth = int(PARAMS.get("scan_depth") or 8)
include_schema = bool(PARAMS.get("include_schema", True))
std_gen = PARAMS.get("std_generators") or []
std_test = PARAMS.get("std_tests") or []
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.EnvQuery):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an EnvQuery (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name()}
    # Options: reachable by name (EnvQueryOption_<N>), outered to the query. Indices are not
    # guaranteed contiguous/0-based (editor re-numbers on add/remove), so scan a padded range.
    options = []
    for i in range(0, max_options + 12):
        o = _try(lambda: unreal.find_object(obj, "EnvQueryOption_%d" % i))
        if o is not None:
            options.append({"index": i, "name": o.get_name(),
                            "path": o.get_path_name()})
    info["option_count"] = len(options)
    info["options"] = options
    # Detected generator/test node instances: probe the standard engine class names by
    # "<Class>_<N>" against the query outer (no Python object-enumeration API exists).
    schemas = {}
    def _scan(class_names, want_schema_inherited):
        found = []
        for cn in class_names:
            for i in range(0, scan_depth):
                n = _try(lambda: unreal.find_object(obj, "%s_%d" % (cn, i)))
                if n is not None:
                    real = n.get_class().get_name()
                    found.append({"name": n.get_name(), "class": real})
                    if include_schema and real not in schemas:
                        sch = _obj_prop_schema(n, include_inherited=want_schema_inherited, limit=60)
                        if sch is not None:
                            schemas[real] = sch
        found.sort(key=lambda r: r["name"].lower())
        return found
    info["generators"] = _scan(std_gen, False)
    info["tests"] = _scan(std_test, False)
    if include_schema:
        info["node_config_schema"] = schemas
    info["reflection_note"] = (
        "Options enumerated via find_object(query,'EnvQueryOption_N'). Generator/test NODES are the "
        "runtime subobjects reached via find_object(query,'<Class>_<N>') for the standard engine EQS "
        "class set (class names are real, read from get_class().get_name()); a custom/Blueprint node "
        "whose class is not in that set would be missed. Node CONFIG VALUES (TestPurpose, "
        "ScoringEquation/Factor, FilterType, generator ItemType, Center context, ...) and the "
        "per-option->node grouping are stored in PROTECTED C++ UPROPERTYs that stock Python "
        "get_editor_property refuses, so they are NOT read here (not faked); 'node_config_schema' "
        "lists each detected node class's AUTHORABLE properties (names/types/tooltips, via "
        "MCPReflectionLibrary metadata) so you can see WHAT is configurable. Reading concrete values "
        "would need a C++ value-reading reflection handler. QueryName is likewise protected.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_env_query_info(ctx, asset_path: str, max_options: int = 32,
                           include_schema: bool = True) -> str:
        """Structure of one EnvQuery asset: options + detected generator/test nodes. Read-only.

        asset_path:     EnvQuery asset path, e.g.
                        '/Game/Variant_Combat/Blueprints/AI/EnvQuery_Evade.EnvQuery_Evade'.
        max_options:    upper bound on option-index probing (default 32).
        include_schema: include each detected node class's authorable property schema (default True).

        Returns: option_count + options[{index,name,path}]; generators[] and tests[] as
        [{name, class}] (real engine class names, e.g. EnvQueryGenerator_Donut, EnvQueryTest_Distance);
        and node_config_schema {className: [{name, cpp_type[, category, tooltip]}]} describing what
        each node type EXPOSES to author.

        IMPORTANT (reflection limits, reported in 'reflection_note', never faked): the concrete
        CONFIGURED VALUES of generators/tests (purpose, scoring, filter, item type, context) and the
        per-option node grouping live in PROTECTED C++ UPROPERTYs that stock Python cannot read; this
        tool reports the node classes present + their authorable schema, not their set values. Node
        discovery covers the standard engine EQS classes (a non-standard custom node may be missed)."""
        params = {"asset_path": asset_path, "max_options": max_options,
                  "include_schema": include_schema,
                  "std_generators": _STD_GENERATORS, "std_tests": _STD_TESTS}
        try:
            return json.dumps(_exec(_QUERY_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # _CLASS_LIST_BODY — shared enumerator for generators/tests/contexts   #
    # ------------------------------------------------------------------ #
    _CLASS_LIST_BODY = _EQS_HELPERS + r'''
kind = PARAMS.get("kind")                # "generator" | "test" | "context"
base_name = PARAMS.get("base_name")      # "EnvQueryGenerator" | "EnvQueryTest" | "EnvQueryContext"
std_names = PARAMS.get("std_names") or []
include_blueprint = bool(PARAMS.get("include_blueprint", True))
package_path = PARAMS.get("path") or "/Game"
seen = {}
def _add(name, source, path=None):
    if name in seen:
        if source not in seen[name]["sources"]:
            seen[name]["sources"].append(source)
        return
    cls, cpath = _resolve_eqs_class(name)
    rec = {"name": name, "sources": [source]}
    if cls is not None:
        cm = _class_meta(cls)
        rec["class_path"] = cpath
        if cm:
            rec["parent_class"] = cm.get("parent_class")
            rec["is_abstract"] = cm.get("is_abstract")
            rec["is_blueprintable"] = cm.get("is_blueprintable")
            rec["is_deprecated"] = cm.get("is_deprecated")
    elif path:
        rec["class_path"] = path
    seen[name] = rec
# 1) standard engine classes that resolve under /Script/AIModule
for n in std_names:
    cls, cpath = _resolve_eqs_class(n)
    if cls is not None:
        _add(n, "engine")
# 2) unreal.* namespace subclasses of the base (catches *_BlueprintBase + any exported subclass)
base = getattr(unreal, base_name, None)
if base is not None:
    for nm in dir(unreal):
        cand = getattr(unreal, nm, None)
        if inspect.isclass(cand) and issubclass(cand, unreal.Object) and cand is not base and issubclass(cand, base):
            _add(nm, "namespace")
# 3) project Blueprint subclasses via AssetRegistry (parent class mentions the base)
bp_rows = []
if include_blueprint:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        flt = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
            recursive_paths=True, package_paths=[package_path])
        for a in (ar.get_assets(flt) or []):
            parent = str(a.get_tag_value("ParentClass") or a.get_tag_value("NativeParentClass") or "")
            if base_name in parent:
                pkg = str(a.get_editor_property("package_name"))
                nm = str(a.get_editor_property("asset_name"))
                bp_rows.append({"name": nm, "path": pkg + "." + nm, "parent_class": parent})
    except Exception:
        pass
classes = sorted(seen.values(), key=lambda r: r["name"].lower())
bp_rows.sort(key=lambda r: r["path"].lower())
result = {"status": "success", "kind": kind, "base_class": base_name,
          "class_count": len(classes), "classes": classes,
          "blueprint_class_count": len(bp_rows), "blueprint_classes": bp_rows}
result["note"] = (
    "Authorable %s classes. 'classes' = standard engine classes resolvable under /Script/AIModule "
    "(the native EQS classes are NOT exported into the unreal.* Python namespace, so they are resolved "
    "by class path) + any unreal.*-exported subclasses (e.g. %s_BlueprintBase). 'blueprint_classes' = "
    "project Blueprint assets whose parent class is %s. A fully exhaustive native subclass walk would "
    "need a C++ GetDerivedClasses handler (not Python-exposed), so a non-standard native subclass not "
    "in the engine list would be omitted from 'classes'." % (kind, base_name, base_name))
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_env_query_generators(ctx, path: str = None,
                                  include_blueprint: bool = True) -> str:
        """List authorable EnvQueryGenerator classes (engine + namespace + project Blueprint). Read-only.

        path:              content root scanned for Blueprint generator subclasses (default '/Game').
        include_blueprint: include project Blueprint EnvQueryGenerator subclasses (default True).

        Returns 'classes' (standard engine generators resolvable under /Script/AIModule + any
        unreal.*-exported subclasses, each with parent_class / is_abstract / is_blueprintable) and
        'blueprint_classes' (project Blueprint generators). See 'note' for enumeration scope: native
        EQS generators are not in the unreal.* namespace and are resolved by class path."""
        params = {"kind": "generator", "base_name": "EnvQueryGenerator",
                  "std_names": _STD_GENERATORS, "include_blueprint": include_blueprint, "path": path}
        try:
            return json.dumps(_exec(_CLASS_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_env_query_tests(ctx, path: str = None,
                             include_blueprint: bool = True) -> str:
        """List authorable EnvQueryTest classes (engine + namespace + project Blueprint). Read-only.

        path:              content root scanned for Blueprint test subclasses (default '/Game').
        include_blueprint: include project Blueprint EnvQueryTest subclasses (default True).

        Returns 'classes' (standard engine tests resolvable under /Script/AIModule, e.g.
        EnvQueryTest_Distance / _Dot / _Pathfinding / _Trace, + any unreal.*-exported subclasses) and
        'blueprint_classes'. See 'note' for enumeration scope."""
        params = {"kind": "test", "base_name": "EnvQueryTest",
                  "std_names": _STD_TESTS, "include_blueprint": include_blueprint, "path": path}
        try:
            return json.dumps(_exec(_CLASS_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_env_query_contexts(ctx, path: str = None,
                                include_blueprint: bool = True) -> str:
        """List authorable EnvQueryContext classes (engine + namespace + project Blueprint). Read-only.

        path:              content root scanned for Blueprint context subclasses (default '/Game').
        include_blueprint: include project Blueprint EnvQueryContext subclasses (default True).

        Returns 'classes' (standard engine contexts resolvable under /Script/AIModule, e.g.
        EnvQueryContext_Querier / _Item, plus namespace-exported EnvQueryContext_NavigationData /
        _BlueprintBase) and 'blueprint_classes' (project Blueprint contexts -- the usual way games add
        custom query points). See 'note' for enumeration scope."""
        params = {"kind": "context", "base_name": "EnvQueryContext",
                  "std_names": _STD_CONTEXTS, "include_blueprint": include_blueprint, "path": path}
        try:
            return json.dumps(_exec(_CLASS_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_env_query_node_info — one node class: metadata + authorable props #
    # ------------------------------------------------------------------ #
    _NODE_INFO_BODY = _EQS_HELPERS + r'''
node_class = PARAMS.get("node_class")
include_inherited = bool(PARAMS.get("include_inherited", True))
cls, cpath = _resolve_eqs_class(node_class)
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": ("could not resolve EQS node class '%s' (tried /Script/AIModule.%s and unreal.%s). "
                    "Use a class name from list_env_query_generators/tests/contexts, or a Blueprint "
                    "node's generated class path." % (node_class, node_class, node_class))}))
else:
    cm = _class_meta(cls)
    info = {"status": "success", "node_class": node_class, "class_path": cpath, "class_meta": cm}
    cdo = _try(lambda: unreal.get_default_object(cls))
    own = _obj_prop_schema(cdo, include_inherited=False, limit=120) if cdo is not None else None
    info["configurable_properties_own"] = own
    if include_inherited and cdo is not None:
        info["configurable_properties_all"] = _obj_prop_schema(cdo, include_inherited=True, limit=200)
    info["note"] = (
        "Authorable schema for the node class, read from its CDO via MCPReflectionLibrary property "
        "METADATA (names / cpp_type / category / tooltip). 'configurable_properties_own' are declared "
        "on this class; '..._all' includes inherited config. These are the fields you set when "
        "authoring this generator/test/context. (Default VALUES on the CDO are not dumped -- most EQS "
        "config members are protected C++ UPROPERTYs unreadable by stock Python get_editor_property; "
        "only their schema is reachable.)")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_env_query_node_info(ctx, node_class: str, include_inherited: bool = True) -> str:
        """Class details + authorable property schema for ONE EQS node class. Read-only.

        node_class:       an EnvQueryGenerator/Test/Context class name (e.g. 'EnvQueryGenerator_Donut',
                          'EnvQueryTest_Distance', 'EnvQueryContext_Querier'). Resolved under
                          /Script/AIModule or the unreal.* namespace.
        include_inherited: also list inherited configurable properties (default True).

        Returns class_meta (parent_class / is_abstract / is_blueprintable / is_deprecated) and the
        node's authorable property schema (configurable_properties_own and, optionally, _all) as
        [{name, cpp_type[, category, tooltip]}] -- i.e. WHAT you configure on this node type (e.g. a
        Donut generator's InnerRadius / OuterRadius / NumberOfRings / ArcAngle / Center-context). Use
        list_env_query_generators/tests/contexts to discover class names.

        NOTE: schema (property names/types) is reflectable; concrete default VALUES are not (EQS config
        members are protected C++ UPROPERTYs unreadable by stock Python)."""
        params = {"node_class": node_class, "include_inherited": include_inherited}
        try:
            return json.dumps(_exec(_NODE_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- get_env_query_config (C++ #12: node config VALUES via FProperty reflection) ----
    _QUERY_CONFIG_BODY = _EQS_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.EnvQuery):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an EnvQuery (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    m = _refl()
    fn = getattr(m, "get_env_query_config_json", None) if m is not None else None
    if fn is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.get_env_query_config_json unavailable — reload the MCP server after the C++ #12 rebuild; use get_env_query_info for the schema-only view meanwhile."}))
    else:
        try:
            parsed = json.loads(fn(obj))
        except Exception as _e:
            parsed = {"error": "get_env_query_config_json failed: %s" % _e}
        parsed.setdefault("status", "error" if "error" in parsed else "success")
        parsed["note"] = ("Node CONFIG VALUES + per-option->node grouping via C++ FProperty reflection "
                          "(unlocks the protected UPROPERTYs stock Python get_editor_property refuses). "
                          "Enums are value-name strings, object/class refs are paths, structs are ExportText.")
        print("@@UMCP@@" + json.dumps(parsed))
'''

    @mcp.tool()
    def get_env_query_config(ctx, asset_path: str) -> str:
        """Full EQS config INCLUDING the protected node config VALUES (C++ reflection reader). Read-only.

        asset_path: EnvQuery asset path, e.g.
                    '/Game/Variant_Combat/Blueprints/AI/EnvQuery_Evade.EnvQuery_Evade'.

        Returns option_count + options[{index, name, generator:{class, path, item_type, config{...}},
        tests:[{class, path, config{...}}]}] — the concrete configured VALUES (TestPurpose,
        ScoringEquation, ScoringFactor, FilterType, generator radii/rings/ItemType, Center context, ...)
        and the per-option node grouping that get_env_query_info could NOT read (protected C++
        UPROPERTYs). Requires the C++ #12 plugin build; falls back with a clear message otherwise."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_QUERY_CONFIG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
