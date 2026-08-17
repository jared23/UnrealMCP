"""UserTools :: Gameplay Ability System (GAS) — READ-ONLY  (spec: docs/spec/gas.md)

Clean-room, READ-ONLY introspection of the Gameplay Ability System over Unreal's public
Python API (UE 5.8). Reimplemented from the public reflection surface — never from decompiled
or third-party GAS source. Executed in the editor's interpreter via the plugin's
`execute_python` handler; results captured from stdout.

Query convention (copied verbatim from editor_level.py / gameplay_tags_read.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON after it.
Every snippet is wrapped with Output-Log delta capture, surfacing new Warning/Error lines as
result['_log_warnings']. Params are passed as base64 JSON.

Design (works with ZERO project GAS assets — probed live 2026-08-15): the "Gameplay Abilities"
plugin ships C++ classes (UGameplayAbility / UGameplayEffect / UAttributeSet subclasses) that are
introspectable via CLASS reflection even when /Game contains no GAS Blueprint assets yet. So every
tool works off BOTH project Blueprint assets (AssetRegistry, by native parent class) AND native
class reflection (dir(unreal) issubclass scan), and returns honest-empty when a source is empty.
CDO fields are read via unreal.MCPReflectionLibrary (hasattr-guarded) + get_editor_property.

Commands (all READ-ONLY; no writes, no ledger, no factories, no modals):
  - list_gameplay_abilities  — GameplayAbility Blueprint assets (AssetRegistry) + native
                               UGameplayAbility subclasses (class reflection).
  - list_gameplay_effects    — GameplayEffect Blueprint assets + native UGameplayEffect subclasses.
  - list_attribute_sets      — UAttributeSet subclasses (native + BP); per set, its attribute
                               UPROPERTYs (FGameplayAttributeData + numeric) with defaults.
  - get_gameplay_ability_info — a GameplayAbility CDO: instancing/net/security policy, ability/
                               cancel/block/activation tags, cost+cooldown GE classes, triggers.
  - get_gameplay_effect_info — a GameplayEffect CDO: duration policy, modifiers (attribute+op+
                               magnitude type), GEComponents, stacking, inherited tag containers.
  - get_attribute_set_info   — one AttributeSet: attributes + defaults + class metadata.

Honest limitations discovered LIVE (UE 5.8, from-source build, project TestMCPSetup):
  - The project currently has ZERO GAS Blueprint assets (37 BPs in /Game, none GAS-parented), so
    Blueprint listings are honestly empty; validation ran against engine class CDOs.
  - Struct sub-values are read by KNOWN FProperty names (attribute/modifier_op/modifier_magnitude,
    combined_tags/gameplay_tags, trigger_tag/trigger_source, etc.). Deprecated CDO properties
    (e.g. GameplayEffect.ChanceToApplyToTarget, GameplayAbility.CostGameplayEffect pointer) are NOT
    read — the modern replacements (*_class, GEComponents) are used instead.
  - Deep GEComponent internals and FScalableFloat curve tables beyond scalar 'value' are not fully
    expanded — reported as class name / scalar; deeper expansion is DEFERRED (needs per-struct code).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / gameplay_tags_read.py) ---
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
    # Session id identifies THIS reader process (parity with other modules; pure-read,
    # never touches any ledger). One value per OS process.
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

    # Shared Unreal-side helpers (no ''' / no backslashes). Reflection over the public GAS API.
    #   _enum_name(v)        -> short enum member name from an unreal.EnumBase str.
    #   _mrl_props(obj)      -> [{name, cpp_type, owner_class}] via MCPReflectionLibrary (or None).
    #   _class_meta(cls)     -> {parent_class, is_abstract, is_blueprintable} via MRL (or None).
    #   _tag_names(cont)     -> [dotted tag names] from an FGameplayTagContainer.
    #   _inherited_tags(cdo,p) -> combined tag names from an FInheritedTagContainer property.
    #   _ser_attr(fa)        -> attribute name string from an FGameplayAttribute struct.
    #   _ser_class_ref(v)    -> class name from a TSubclassOf / UClass value (or None).
    #   _native_subclasses(b)-> [names] of unreal.* classes that subclass base b (excludes b).
    #   _resolve_cdo(ident)  -> dict{cdo, kind, name, path, class_name, err} for a name or /Game path.
    _GAS_HELPERS = r'''
import unreal, json, warnings, inspect, fnmatch
warnings.simplefilter("ignore")
_HAS_MRL = hasattr(unreal, "MCPReflectionLibrary")
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
_GTL = unreal.GameplayTagLibrary
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _match(s, pat):
    if not pat: return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _enum_name(v):
    if v is None: return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip().rstrip(">")
    return s
def _mrl_props(obj):
    if obj is None or not _HAS_MRL: return None
    try:
        js = _MRL.get_object_property_metadata_json(obj, include_inherited=True)
        d = json.loads(js) if isinstance(js, str) else js
        if isinstance(d, dict):
            return d.get("properties", []) or []
    except Exception:
        return None
    return None
def _class_meta(cls):
    if cls is None or not _HAS_MRL: return None
    try:
        cm = json.loads(_MRL.get_class_metadata_json(cls))
        return {"parent_class": cm.get("parent_class"),
                "is_abstract": cm.get("is_abstract"),
                "is_blueprintable": cm.get("is_blueprintable"),
                "is_deprecated": cm.get("is_deprecated")}
    except Exception:
        return None
def _tag_names(cont):
    if cont is None: return []
    tags = _try(lambda: cont.get_editor_property("gameplay_tags"), []) or []
    out = []
    for t in tags:
        nm = _try(lambda t=t: str(_GTL.get_tag_name(t)))
        if nm and nm not in ("", "None"):
            out.append(nm)
    return out
def _inherited_tags(cdo, prop):
    inh = _try(lambda: cdo.get_editor_property(prop))
    if inh is None: return []
    comb = _try(lambda: inh.get_editor_property("combined_tags"))
    return _tag_names(comb)
def _ser_attr(fa):
    if fa is None: return None
    nm = _try(lambda: str(fa.get_editor_property("attribute_name")))
    return nm if nm not in (None, "", "None") else None
def _ser_class_ref(v):
    if v is None: return None
    nm = _try(lambda: v.get_name())
    if nm: return str(nm)
    s = str(v)
    return None if s in ("None", "") else s
def _native_subclasses(base_name):
    base = getattr(unreal, base_name, None)
    if not (inspect.isclass(base) and issubclass(base, unreal.Object)):
        return None
    out = []
    for n in dir(unreal):
        o = getattr(unreal, n, None)
        if inspect.isclass(o) and issubclass(o, unreal.Object) and issubclass(o, base) and o is not base:
            out.append(n)
    return sorted(out)
def _resolve_cdo(ident):
    res = {"cdo": None, "cls": None, "kind": None, "name": ident, "path": None,
           "class_name": None, "err": None}
    if not ident:
        res["err"] = "identifier required (a native class name or a /Game asset path)"
        return res
    if "/" not in ident:
        c = getattr(unreal, ident, None)
        if inspect.isclass(c) and issubclass(c, unreal.Object):
            res["cls"] = c
            res["cdo"] = _try(lambda: unreal.get_default_object(c))
            res["kind"] = "native_class"
            res["class_name"] = ident
            res["path"] = _try(lambda: c.static_class().get_path_name())
            return res
        res["err"] = "no native unreal class named '%s' (and not a /Game path)" % ident
        return res
    asset = _try(lambda: unreal.EditorAssetLibrary.load_asset(ident))
    if asset is None:
        asset = _try(lambda: unreal.load_object(None, ident))
    if asset is None:
        res["err"] = "asset/object not found: %s" % ident
        return res
    if isinstance(asset, unreal.Blueprint):
        gcls = _try(lambda: unreal.BlueprintEditorLibrary.generated_class(asset))
        if gcls is None:
            res["err"] = "blueprint has no generated class: %s" % ident
            return res
        res["cls"] = gcls
        res["cdo"] = _try(lambda: unreal.get_default_object(gcls))
        res["kind"] = "blueprint"
        res["name"] = _try(lambda: asset.get_name(), ident)
        res["path"] = _try(lambda: asset.get_path_name(), ident)
        res["class_name"] = _try(lambda: gcls.get_name())
        return res
    if isinstance(asset, unreal.Class):
        res["cls"] = asset
        res["cdo"] = _try(lambda: unreal.get_default_object(asset))
        res["kind"] = "class"
        res["path"] = ident
        res["class_name"] = _try(lambda: asset.get_name())
        return res
    # an object/CDO instance
    res["cdo"] = asset
    res["cls"] = _try(lambda: asset.get_class())
    res["kind"] = "object"
    res["path"] = ident
    res["class_name"] = _try(lambda: asset.get_class().get_name())
    return res
'''

    # ------------------------------------------------------------------ #
    # shared: enumerate BP assets + native subclasses of a GAS base class #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _GAS_HELPERS + r'''
base_name = PARAMS.get("base")
package_path = PARAMS.get("package_path") or "/Game"
flt = PARAMS.get("filter")
max_results = PARAMS.get("max_results")
include_native = PARAMS.get("include_native")
include_native = True if include_native is None else bool(include_native)
base = getattr(unreal, base_name, None)
base_ok = bool(inspect.isclass(base) and issubclass(base, unreal.Object))

# --- Blueprint assets under package_path whose native ancestor subclasses `base` ---
bp_rows = []
if base_ok:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    far = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
                          recursive_paths=True, package_paths=[package_path])
    assets = _try(lambda: ar.get_assets(far), []) or []
    for a in assets:
        nm = str(a.get_editor_property("asset_name"))
        if flt and not _match(nm, flt):
            continue
        npc = _try(lambda a=a: str(a.get_tag_value("NativeParentClass")))
        short = None
        if npc:
            short = npc.split("'")[1] if "'" in npc else npc
            short = short.rstrip("'").split(".")[-1].split(":")[-1]
        anc = getattr(unreal, short, None) if short else None
        if not (inspect.isclass(anc) and issubclass(anc, unreal.Object) and issubclass(anc, base)):
            continue
        pkg = str(a.get_editor_property("package_name"))
        pc = _try(lambda a=a: str(a.get_tag_value("ParentClass")))
        pc_short = None
        if pc:
            pc_short = pc.split("'")[1] if "'" in pc else pc
            pc_short = pc_short.rstrip("'").split(".")[-1].split(":")[-1]
        bp_rows.append({"name": nm, "path": pkg + "." + nm, "package": pkg,
                        "parent_class": pc_short, "native_parent_class": short,
                        "generated_class_path": pkg + "." + nm + "_C"})
    bp_rows.sort(key=lambda r: r["name"].lower())

# --- native C++ subclasses of `base` (introspectable even with zero project assets) ---
native_rows = []
if include_native and base_ok:
    for n in (_native_subclasses(base_name) or []):
        if flt and not _match(n, flt):
            continue
        c = getattr(unreal, n, None)
        cm = _class_meta(c)
        native_rows.append({"name": n,
                            "path": _try(lambda c=c: c.static_class().get_path_name()),
                            "parent_class": (cm or {}).get("parent_class"),
                            "is_abstract": (cm or {}).get("is_abstract"),
                            "is_blueprintable": (cm or {}).get("is_blueprintable")})

bp_total = len(bp_rows)
nat_total = len(native_rows)
if max_results:
    try:
        m = int(max_results)
        bp_rows = bp_rows[:m]; native_rows = native_rows[:m]
    except Exception:
        pass
result = {"status": "success",
          "base_class": base_name,
          "base_class_resolved": base_ok,
          "package_path": package_path,
          "blueprint_asset_count": bp_total,
          "blueprint_assets": bp_rows,
          "native_subclass_count": nat_total,
          "native_subclasses": native_rows,
          "note": ("'blueprint_assets' are %s Blueprint assets under %s whose native ancestor "
              "subclasses U%s (AssetRegistry NativeParentClass tag; no asset load). "
              "'native_subclasses' are engine/plugin C++ classes exposed on the unreal module "
              "that subclass it (introspectable even when the project has zero GAS Blueprints). "
              "A base_class_resolved=false means the GAS plugin class is not loaded." )
              % (base_name.replace("Gameplay",""), package_path, base_name)}
if not base_ok:
    result["error_hint"] = ("unreal.%s is not available — the Gameplay Abilities plugin may be "
                            "disabled or not yet loaded." % base_name)
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_gameplay_abilities(ctx, package_path: str = "/Game", filter: str = None,
                                include_native: bool = True, max_results: int = None) -> str:
        """List Gameplay Abilities: /Game Blueprint assets + native UGameplayAbility subclasses. Read-only.

        Enumerates both (a) GameplayAbility Blueprint assets under package_path via the AssetRegistry
        (matched by their native parent class resolving to a UGameplayAbility subclass; no asset load),
        and (b) native C++ UGameplayAbility subclasses via class reflection — so it returns real data
        even when the project has ZERO GAS Blueprint assets yet.

        package_path:   content root to scan for Blueprints (default '/Game').
        filter:         case-insensitive substring/glob on the ability name.
        include_native: also list engine/plugin C++ UGameplayAbility subclasses (default True).
        max_results:    cap each list. Pass a name/path from here to get_gameplay_ability_info."""
        params = {"base": "GameplayAbility", "package_path": package_path, "filter": filter,
                  "include_native": include_native, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_gameplay_effects(ctx, package_path: str = "/Game", filter: str = None,
                              include_native: bool = True, max_results: int = None) -> str:
        """List Gameplay Effects: /Game Blueprint assets + native UGameplayEffect subclasses. Read-only.

        Enumerates both (a) GameplayEffect Blueprint assets under package_path via the AssetRegistry
        (matched by native parent class resolving to a UGameplayEffect subclass; no asset load), and
        (b) native C++ UGameplayEffect subclasses via class reflection — real data even with zero
        project GAS assets.

        package_path:   content root to scan (default '/Game').
        filter:         case-insensitive substring/glob on the effect name.
        include_native: also list engine/plugin C++ UGameplayEffect subclasses (default True).
        max_results:    cap each list. Pass a name/path from here to get_gameplay_effect_info."""
        params = {"base": "GameplayEffect", "package_path": package_path, "filter": filter,
                  "include_native": include_native, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_attribute_sets — UAttributeSet subclasses + their attributes   #
    # ------------------------------------------------------------------ #
    _ATTR_HELPERS = _GAS_HELPERS + r'''
_ATTR_CPP = ("FGameplayAttributeData", "float", "double")
def _attr_props(cdo, props):
    # From MRL CDO metadata, keep numeric/attribute UPROPERTYs and read their defaults.
    # FGameplayAttributeData -> {base_value, current_value}; float/double -> the scalar default.
    out = []
    for p in (props or []):
        cpp = p.get("cpp_type"); nm = p.get("name")
        if cpp not in _ATTR_CPP:
            continue
        owner = p.get("owner_class")
        if owner == "Object":
            continue
        rec = {"name": nm, "data_type": cpp, "owner_class": owner}
        if cpp == "FGameplayAttributeData":
            v = _try(lambda nm=nm: cdo.get_editor_property(nm))
            rec["base_value"] = _try(lambda v=v: v.get_editor_property("base_value"))
            rec["current_value"] = _try(lambda v=v: v.get_editor_property("current_value"))
        else:
            rec["default_value"] = _try(lambda nm=nm: cdo.get_editor_property(nm))
        out.append(rec)
    return out
'''

    _LIST_SETS_BODY = _ATTR_HELPERS + r'''
package_path = PARAMS.get("package_path") or "/Game"
flt = PARAMS.get("filter")
include_attrs = PARAMS.get("include_attributes")
include_attrs = True if include_attrs is None else bool(include_attrs)
base = getattr(unreal, "AttributeSet", None)
base_ok = bool(inspect.isclass(base) and issubclass(base, unreal.Object))
sets = []

# native C++ UAttributeSet subclasses
if base_ok:
    for n in (_native_subclasses("AttributeSet") or []):
        if flt and not _match(n, flt):
            continue
        c = getattr(unreal, n, None)
        cm = _class_meta(c) or {}
        rec = {"name": n, "kind": "native",
               "path": _try(lambda c=c: c.static_class().get_path_name()),
               "parent_class": cm.get("parent_class"),
               "is_abstract": cm.get("is_abstract")}
        if include_attrs:
            cdo = _try(lambda c=c: unreal.get_default_object(c))
            props = _mrl_props(cdo)
            attrs = _attr_props(cdo, props) if props is not None else None
            rec["attribute_count"] = (len(attrs) if attrs is not None else None)
            rec["attributes"] = attrs
        sets.append(rec)

# Blueprint UAttributeSet subclasses under package_path
if base_ok:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    far = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
                          recursive_paths=True, package_paths=[package_path])
    for a in (_try(lambda: ar.get_assets(far), []) or []):
        nm = str(a.get_editor_property("asset_name"))
        if flt and not _match(nm, flt):
            continue
        npc = _try(lambda a=a: str(a.get_tag_value("NativeParentClass")))
        short = None
        if npc:
            short = npc.split("'")[1] if "'" in npc else npc
            short = short.rstrip("'").split(".")[-1].split(":")[-1]
        anc = getattr(unreal, short, None) if short else None
        if not (inspect.isclass(anc) and issubclass(anc, unreal.Object) and issubclass(anc, base)):
            continue
        pkg = str(a.get_editor_property("package_name"))
        rec = {"name": nm, "kind": "blueprint", "path": pkg + "." + nm,
               "native_parent_class": short}
        if include_attrs:
            bp = _try(lambda a=a: unreal.EditorAssetLibrary.load_asset(pkg + "." + nm))
            gcls = _try(lambda bp=bp: unreal.BlueprintEditorLibrary.generated_class(bp)) if bp else None
            cdo = _try(lambda gcls=gcls: unreal.get_default_object(gcls)) if gcls else None
            props = _mrl_props(cdo)
            attrs = _attr_props(cdo, props) if props is not None else None
            rec["attribute_count"] = (len(attrs) if attrs is not None else None)
            rec["attributes"] = attrs
        sets.append(rec)

sets.sort(key=lambda r: r["name"].lower())
result = {"status": "success",
          "base_class_resolved": base_ok,
          "package_path": package_path,
          "attribute_set_count": len(sets),
          "attribute_sets": sets,
          "reflection": {"mcp_reflection_library": _HAS_MRL},
          "note": ("Enumerates UAttributeSet subclasses (native C++ via class reflection + "
              "Blueprint assets under %s via AssetRegistry). Each set's 'attributes' are its "
              "numeric UPROPERTYs read from the CDO via MCPReflectionLibrary: FGameplayAttributeData "
              "attributes carry base_value/current_value; plain float/double attributes carry "
              "default_value. UObject-owned props are excluded. Use get_attribute_set_info for one "
              "set in detail." % package_path)}
if not base_ok:
    result["error_hint"] = "unreal.AttributeSet unavailable — Gameplay Abilities plugin not loaded."
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_attribute_sets(ctx, package_path: str = "/Game", filter: str = None,
                            include_attributes: bool = True) -> str:
        """List AttributeSets (native + Blueprint) and the attributes each defines. Read-only.

        Enumerates UAttributeSet subclasses two ways: native C++ sets via class reflection, and
        Blueprint sets under package_path via the AssetRegistry. For each set, reflects its attribute
        UPROPERTYs from the CDO — FGameplayAttributeData attributes (base_value/current_value) and
        plain float/double attributes (default_value).

        package_path:       content root to scan for Blueprint sets (default '/Game').
        filter:             case-insensitive substring/glob on the set name.
        include_attributes: read each set's attributes+defaults (default True; False = names only)."""
        params = {"package_path": package_path, "filter": filter,
                  "include_attributes": include_attributes}
        try:
            return json.dumps(_exec(_LIST_SETS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _GET_SET_BODY = _ATTR_HELPERS + r'''
ident = PARAMS.get("attribute_set")
r = _resolve_cdo(ident)
if r["err"]:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": r["err"]}))
else:
    cdo = r["cdo"]; cls = r["cls"]
    is_attrset = bool(inspect.isclass(cls) and issubclass(cls, unreal.AttributeSet)) if cls else None
    props = _mrl_props(cdo)
    attrs = _attr_props(cdo, props) if props is not None else None
    cm = _class_meta(cls)
    result = {"status": "success",
              "attribute_set": {"name": r["name"], "path": r["path"],
                                "class_name": r["class_name"], "resolved_kind": r["kind"]},
              "is_attribute_set_subclass": is_attrset,
              "class_metadata": cm,
              "attribute_count": (len(attrs) if attrs is not None else None),
              "attributes": attrs,
              "reflection": {"mcp_reflection_library": _HAS_MRL},
              "note": ("Attributes are the CDO's numeric UPROPERTYs via MCPReflectionLibrary: "
                  "FGameplayAttributeData -> base_value/current_value; float/double -> default_value. "
                  "If reflection was unavailable, 'attributes' is null.")}
    if not is_attrset:
        result["warning"] = "resolved class is not a UAttributeSet subclass; attributes may be empty/irrelevant."
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_attribute_set_info(ctx, attribute_set: str) -> str:
        """Inspect one AttributeSet: its attributes, defaults, and class metadata. Read-only.

        attribute_set: a native class name (e.g. 'AbilitySystemTestAttributeSet') OR a /Game
                       Blueprint asset path (e.g. '/Game/GAS/BP_MyAttributes').

        Returns the resolved class, whether it is a UAttributeSet subclass, class metadata
        (parent/abstract/blueprintable), and every attribute UPROPERTY with its default:
        FGameplayAttributeData attributes report base_value/current_value; float/double report
        default_value."""
        try:
            return json.dumps(_exec(_GET_SET_BODY, {"attribute_set": attribute_set}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_gameplay_ability_info — CDO policies + tags + cost/cooldown      #
    # ------------------------------------------------------------------ #
    _GET_ABILITY_BODY = _GAS_HELPERS + r'''
ident = PARAMS.get("ability")
r = _resolve_cdo(ident)
if r["err"]:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": r["err"]}))
else:
    cdo = r["cdo"]; cls = r["cls"]
    is_ability = bool(inspect.isclass(cls) and issubclass(cls, unreal.GameplayAbility)) if cls else None
    if cdo is None or not is_ability:
        msg = "resolved object is not a UGameplayAbility (class=%s)" % r["class_name"]
        print("@@UMCP@@" + json.dumps({"status": "error", "message": msg,
              "resolved": {"name": r["name"], "path": r["path"], "class_name": r["class_name"]}}))
    else:
        def _tagprop(p):
            return _tag_names(_try(lambda: cdo.get_editor_property(p)))
        triggers = []
        for t in (_try(lambda: cdo.get_editor_property("ability_triggers"), []) or []):
            triggers.append({
                "trigger_tag": _try(lambda t=t: str(_GTL.get_tag_name(t.get_editor_property("trigger_tag")))),
                "trigger_source": _enum_name(_try(lambda t=t: t.get_editor_property("trigger_source")))})
        result = {"status": "success",
            "ability": {"name": r["name"], "path": r["path"],
                        "class_name": r["class_name"], "resolved_kind": r["kind"]},
            "class_metadata": _class_meta(cls),
            "policies": {
                "instancing_policy": _enum_name(_try(lambda: cdo.get_editor_property("instancing_policy"))),
                "net_execution_policy": _enum_name(_try(lambda: cdo.get_editor_property("net_execution_policy"))),
                "net_security_policy": _enum_name(_try(lambda: cdo.get_editor_property("net_security_policy"))),
                "replication_policy": _enum_name(_try(lambda: cdo.get_editor_property("replication_policy"))),
                "replicate_input_directly": _try(lambda: bool(cdo.get_editor_property("replicate_input_directly"))),
                "server_respects_remote_cancellation": _try(lambda: bool(cdo.get_editor_property("server_respects_remote_ability_cancellation"))),
                "retrigger_instanced_ability": _try(lambda: bool(cdo.get_editor_property("retrigger_instanced_ability")))},
            "tags": {
                "ability_tags": _tagprop("ability_tags"),
                "cancel_abilities_with_tag": _tagprop("cancel_abilities_with_tag"),
                "block_abilities_with_tag": _tagprop("block_abilities_with_tag"),
                "activation_owned_tags": _tagprop("activation_owned_tags"),
                "activation_required_tags": _tagprop("activation_required_tags"),
                "activation_blocked_tags": _tagprop("activation_blocked_tags"),
                "source_required_tags": _tagprop("source_required_tags"),
                "source_blocked_tags": _tagprop("source_blocked_tags"),
                "target_required_tags": _tagprop("target_required_tags"),
                "target_blocked_tags": _tagprop("target_blocked_tags")},
            "cost_gameplay_effect_class": _ser_class_ref(_try(lambda: cdo.get_editor_property("cost_gameplay_effect_class"))),
            "cooldown_gameplay_effect_class": _ser_class_ref(_try(lambda: cdo.get_editor_property("cooldown_gameplay_effect_class"))),
            "ability_triggers": triggers,
            "reflection": {"mcp_reflection_library": _HAS_MRL},
            "note": ("CDO reflection via get_editor_property. Tags are the dotted names inside each "
                "FGameplayTagContainer. Cost/cooldown are the modern TSubclassOf<UGameplayEffect> "
                "properties (the deprecated inline-GE pointers are NOT read). Empty lists mean the "
                "CDO leaves them unset (common on engine base classes).")}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_gameplay_ability_info(ctx, ability: str) -> str:
        """Inspect a GameplayAbility CDO: policies, tags, cost/cooldown GE, triggers. Read-only.

        ability: a native class name (e.g. 'GameplayAbility_CharacterJump') OR a /Game Blueprint
                 asset path (e.g. '/Game/GAS/GA_Fireball').

        Returns instancing/net-execution/net-security/replication policies; the ability's tag
        containers (ability/cancel/block/activation/source/target tags); the cost and cooldown
        GameplayEffect classes; and ability triggers (trigger tag + source). Errors cleanly if the
        target is not a UGameplayAbility."""
        try:
            return json.dumps(_exec(_GET_ABILITY_BODY, {"ability": ability}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_gameplay_effect_info — duration, modifiers, GEComponents, tags   #
    # ------------------------------------------------------------------ #
    _GET_EFFECT_BODY = _GAS_HELPERS + r'''
ident = PARAMS.get("effect")
r = _resolve_cdo(ident)
if r["err"]:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": r["err"]}))
else:
    cdo = r["cdo"]; cls = r["cls"]
    is_effect = bool(inspect.isclass(cls) and issubclass(cls, unreal.GameplayEffect)) if cls else None
    if cdo is None or not is_effect:
        msg = "resolved object is not a UGameplayEffect (class=%s)" % r["class_name"]
        print("@@UMCP@@" + json.dumps({"status": "error", "message": msg,
              "resolved": {"name": r["name"], "path": r["path"], "class_name": r["class_name"]}}))
    else:
        modifiers = []
        for m in (_try(lambda: cdo.get_editor_property("modifiers"), []) or []):
            mag = _try(lambda m=m: m.get_editor_property("modifier_magnitude"))
            modifiers.append({
                "attribute": _ser_attr(_try(lambda m=m: m.get_editor_property("attribute"))),
                "modifier_op": _enum_name(_try(lambda m=m: m.get_editor_property("modifier_op"))),
                "magnitude_calculation_type": _enum_name(_try(lambda mag=mag: mag.get_editor_property("magnitude_calculation_type")))})
        # GEComponents (modern UE5.3+ tag/requirement plumbing lives here)
        components = []
        for c in (_try(lambda: cdo.get_editor_property("ge_components"), []) or []):
            components.append({"class_name": _try(lambda c=c: c.get_class().get_name()),
                               "path": _try(lambda c=c: c.get_class().get_path_name())})
        period_val = _try(lambda: cdo.get_editor_property("period").get_editor_property("value"))
        result = {"status": "success",
            "effect": {"name": r["name"], "path": r["path"],
                       "class_name": r["class_name"], "resolved_kind": r["kind"]},
            "class_metadata": _class_meta(cls),
            "duration_policy": _enum_name(_try(lambda: cdo.get_editor_property("duration_policy"))),
            "duration_magnitude_calculation_type": _enum_name(_try(lambda: cdo.get_editor_property("duration_magnitude").get_editor_property("magnitude_calculation_type"))),
            "period_value": period_val,
            "modifier_count": len(modifiers),
            "modifiers": modifiers,
            "ge_component_count": len(components),
            "ge_components": components,
            "stacking": {
                "stacking_type": _enum_name(_try(lambda: cdo.get_editor_property("stacking_type"))),
                "stack_limit_count": _try(lambda: int(cdo.get_editor_property("stack_limit_count"))),
                "stack_duration_refresh_policy": _enum_name(_try(lambda: cdo.get_editor_property("stack_duration_refresh_policy"))),
                "stack_period_reset_policy": _enum_name(_try(lambda: cdo.get_editor_property("stack_period_reset_policy"))),
                "stack_expiration_policy": _enum_name(_try(lambda: cdo.get_editor_property("stack_expiration_policy")))},
            "tags": {
                "asset_tags": _inherited_tags(cdo, "inheritable_gameplay_effect_tags"),
                "granted_owned_tags": _inherited_tags(cdo, "inheritable_owned_tags_container"),
                "blocked_ability_tags": _inherited_tags(cdo, "inheritable_blocked_ability_tags_container"),
                "ongoing_tag_requirements": _tag_names(_try(lambda: cdo.get_editor_property("ongoing_tag_requirements").get_editor_property("require_tags"))),
                "application_tag_requirements": _tag_names(_try(lambda: cdo.get_editor_property("application_tag_requirements").get_editor_property("require_tags")))},
            "reflection": {"mcp_reflection_library": _HAS_MRL},
            "note": ("CDO reflection via get_editor_property. Modifiers report attribute + op + "
                "magnitude calculation type. In UE5.3+ much tag/requirement plumbing moved into "
                "GEComponents (listed by class). Inherited tag containers report their combined "
                "tags. Deep GEComponent internals and FScalableFloat curves are not expanded "
                "(DEFERRED). Empty values are genuine CDO defaults (common on the engine base).")}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_gameplay_effect_info(ctx, effect: str) -> str:
        """Inspect a GameplayEffect CDO: duration, modifiers, GEComponents, stacking, tags. Read-only.

        effect: a native class name (e.g. 'GameplayEffect') OR a /Game Blueprint asset path
                (e.g. '/Game/GAS/GE_Damage').

        Returns the duration policy and magnitude type; period; each modifier (attribute + operation
        + magnitude calculation type); the GameplayEffectComponents (by class — modern UE5.3+ tag/
        requirement plumbing); stacking configuration; and inherited tag containers (asset tags,
        granted/owned tags, blocked-ability tags) plus ongoing/application tag requirements. Errors
        cleanly if the target is not a UGameplayEffect."""
        try:
            return json.dumps(_exec(_GET_EFFECT_BODY, {"effect": effect}), indent=2)
        except Exception as e:
            return f"Error: {e}"
