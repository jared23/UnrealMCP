"""UserTools :: Gameplay Ability System (GAS) — READ-ONLY, part 2  (spec: docs/spec/gas.md)

Clean-room, READ-ONLY introspection of the Gameplay Ability System over Unreal's public
Python API (UE 5.8). Completes the GAS surface with four standalone read tools whose data
was previously only reachable folded inside broader inspect tools (gas.py) or not at all.

Query convention (copied verbatim from editor_level.py / gas.py / gameplay_tags_read.py): a
snippet prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it. Every snippet is wrapped with Output-Log delta capture, surfacing new Warning/Error
lines as result['_log_warnings']. Params are passed as base64 JSON.

Commands (all READ-ONLY; no writes, no ledger, no factories, no modals, no asset mutation):
  - validate_gameplay_tags        — validate a list of gameplay tag strings against the LIVE
                                    registry (GameplayTag.import_text consults it); per tag returns
                                    registered?/resolved. Empty list validates the AUTHORED set.
  - search_gas                    — ranked keyword search across GAS assets: GameplayAbility /
                                    GameplayEffect / AttributeSet / GameplayCueNotify (native
                                    subclasses + /Game Blueprint assets via AssetRegistry) plus
                                    authored gameplay tags. Exact > prefix > contains scoring.
  - list_attributes               — standalone: the FGameplayAttributes an AttributeSet class
                                    defines (numeric UPROPERTYs off the CDO + their defaults).
                                    Previously only inside get_attribute_set_info.
  - list_gameplay_effect_components — standalone: the UGameplayEffectComponent entries on a
                                    GameplayEffect CDO (class + index). Previously only inside
                                    get_gameplay_effect_info.

Design notes (parity with gas.py, probed live 2026-08-17):
  - Works with ZERO project GAS assets: native C++ subclasses are introspectable via class
    reflection, and per-CDO fields via unreal.MCPReflectionLibrary (hasattr-guarded) +
    get_editor_property. Blueprint listings are honestly empty when /Game has no GAS assets.
  - The FULL registered gameplay-tag tree (native/plugin tags) is NOT Python-enumerable
    (unreal.GameplayTagsManager is not exposed), but ANY single tag name can be validated
    against the LIVE registry via GameplayTag.import_text — which is exactly what
    validate_gameplay_tags does.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / gas.py) ---
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
# Double-quote characters needed at runtime (GameplayTag.import_text) are built with chr(34)
# so the SOURCE contains no backslash-escaped quotes.


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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers (no ''' / no backslashes).               #
    #   Reflection over the public GAS API + tag validation. Mirrors the  #
    #   helpers in gas.py and gameplay_tags_read.py (kept self-contained).#
    # ------------------------------------------------------------------ #
    _GAS2_HELPERS = r'''
import unreal, json, warnings, inspect, fnmatch
warnings.simplefilter("ignore")
_Q = chr(34)
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
    res["cdo"] = asset
    res["cls"] = _try(lambda: asset.get_class())
    res["kind"] = "object"
    res["path"] = ident
    res["class_name"] = _try(lambda: asset.get_class().get_name())
    return res
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
def _valid_tag(name):
    # Validate a single tag name against the LIVE registry. import_text consults it; an
    # unregistered name resolves to the empty tag (name None, valid False).
    if not name:
        return (False, None)
    t = unreal.GameplayTag()
    _try(lambda: t.import_text("(TagName=" + _Q + str(name) + _Q + ")"), None)
    if not unreal.GameplayTagLibrary.is_gameplay_tag_valid(t):
        return (False, None)
    rn = str(unreal.GameplayTagLibrary.get_tag_name(t))
    if rn in ("", "None"):
        return (False, None)
    return (True, rn)
def _settings_cdo():
    cls = _try(lambda: unreal.load_object(None, "/Script/GameplayTags.GameplayTagsSettings"))
    if cls is None:
        return None
    return _try(lambda: unreal.get_default_object(cls))
def _rows_tags(rows):
    out = []
    for r in (rows or []):
        tg = _try(lambda r=r: str(r.get_editor_property("tag")))
        if tg and tg not in ("", "None"):
            out.append(tg)
    return out
def _authored_tag_names():
    cdo = _settings_cdo()
    acc = []
    seen = set()
    def _add(names):
        for nm in names:
            if nm not in seen:
                seen.add(nm); acc.append(nm)
    if cdo is not None:
        _add(_rows_tags(_try(lambda: cdo.get_editor_property("GameplayTagList"), []) or []))
        _add(_rows_tags(_try(lambda: cdo.get_editor_property("RestrictedTagList"), []) or []))
        for dt in (_try(lambda: cdo.get_editor_property("GameplayTagTableList"), []) or []):
            asset = dt
            path = _try(lambda dt=dt: str(dt))
            try:
                if not isinstance(asset, unreal.Object):
                    asset = unreal.EditorAssetLibrary.load_asset(path)
            except Exception:
                asset = None
            if asset is None:
                continue
            names = _try(lambda asset=asset: unreal.DataTableFunctionLibrary.get_data_table_row_names(asset), []) or []
            _add([str(rn) for rn in names])
    return acc
'''

    # ------------------------------------------------------------------ #
    # validate_gameplay_tags — live-registry validity for a list of names #
    # ------------------------------------------------------------------ #
    _VALIDATE_TAGS_BODY = _GAS2_HELPERS + r'''
tags = PARAMS.get("tags") or []
tags = [str(t) for t in tags]
source = "provided"
if not tags:
    tags = _authored_tag_names()
    source = "authored"
seen = set()
results = []
valid_count = 0
for nm in tags:
    reg, rn = _valid_tag(nm)
    if reg:
        valid_count += 1
    dup = nm in seen
    seen.add(nm)
    results.append({"name": nm, "registered": reg, "resolved": rn, "duplicate": dup})
result = {"status": "success",
          "input_source": source,
          "tag_count": len(results),
          "valid_count": valid_count,
          "invalid_count": len(results) - valid_count,
          "results": results,
          "note": ("Each tag is validated against the LIVE gameplay-tag registry via "
              "GameplayTag.import_text (registered=false means the name resolves to the empty tag "
              "= not in the live registry). Native/plugin tags DO validate here even though the "
              "full tree is not enumerable. With no 'tags' argument, the project's AUTHORED tag set "
              "(GameplayTagsSettings CDO) is validated instead. 'duplicate' flags a repeated input "
              "name.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def validate_gameplay_tags(ctx, tags: list = None) -> str:
        """Validate gameplay tag strings against the LIVE tag registry. Read-only.

        tags: a list of candidate gameplay tag names (e.g. ['Input.Move', 'Ability.Fireball']).
              Each is checked against the live registry via GameplayTag.import_text — 'registered'
              is True only if the name resolves to a real tag. Native/plugin tags validate here
              even though the full tag tree is not Python-enumerable. Pass an empty/None list to
              validate the project's AUTHORED tag set instead.

        Returns per-tag {name, registered, resolved, duplicate} plus valid/invalid counts. No
        writes, no registry mutation."""
        params = {"tags": tags}
        try:
            return json.dumps(_exec(_VALIDATE_TAGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # search_gas — ranked keyword search across GAS assets + tags         #
    # ------------------------------------------------------------------ #
    _SEARCH_BODY = _GAS2_HELPERS + r'''
query = (PARAMS.get("query") or "").strip()
queries = PARAMS.get("queries") or ([query] if query else [])
kind = (PARAMS.get("kind") or "").strip().lower()
package_path = PARAMS.get("package_path") or "/Game"
max_results = int(PARAMS.get("max_results") or 40)
include_native = PARAMS.get("include_native")
include_native = True if include_native is None else bool(include_native)
include_tags = PARAMS.get("include_tags")
include_tags = True if include_tags is None else bool(include_tags)

# category -> the native GAS base class name(s) that define it
CATS = {"ability": ["GameplayAbility"],
        "effect": ["GameplayEffect"],
        "attribute_set": ["AttributeSet"],
        "cue": ["GameplayCueNotify_Static", "GameplayCueNotify_Actor"]}
want = [kind] if kind in CATS else list(CATS.keys())

ql = [q.lower() for q in queries if q]
def _score(name):
    nl = (name or "").lower()
    if not ql:
        return 1
    best = 0
    for q in ql:
        if nl == q:
            best = max(best, 100)
        elif nl.startswith(q):
            best = max(best, 60)
        elif q in nl:
            best = max(best, 30)
    return best

rows = []
native_scanned = 0
bp_scanned = 0
tag_scanned = 0

# resolve the base classes we want, keyed by category
bases = {}
for cat in want:
    for bn in CATS[cat]:
        b = getattr(unreal, bn, None)
        if inspect.isclass(b) and issubclass(b, unreal.Object):
            bases.setdefault(cat, []).append((bn, b))

# --- native C++ subclasses (introspectable even with zero project assets) ---
if include_native:
    for cat, blist in bases.items():
        names = set()
        for bn, b in blist:
            for n in (_native_subclasses(bn) or []):
                names.add(n)
        for n in sorted(names):
            native_scanned += 1
            sc = _score(n)
            if sc <= 0:
                continue
            c = getattr(unreal, n, None)
            rows.append({"name": n, "category": cat, "kind": "native_class",
                         "path": _try(lambda c=c: c.static_class().get_path_name()),
                         "is_blueprint": False, "score": sc})

# --- Blueprint assets under package_path whose native ancestor subclasses a wanted base ---
ar = unreal.AssetRegistryHelpers.get_asset_registry()
far = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
                      recursive_paths=True, package_paths=[package_path])
assets = _try(lambda: ar.get_assets(far), []) or []
for a in assets:
    nm = str(a.get_editor_property("asset_name"))
    npc = _try(lambda a=a: str(a.get_tag_value("NativeParentClass")))
    short = None
    if npc:
        short = npc.split("'")[1] if "'" in npc else npc
        short = short.rstrip("'").split(".")[-1].split(":")[-1]
    anc = getattr(unreal, short, None) if short else None
    if not (inspect.isclass(anc) and issubclass(anc, unreal.Object)):
        continue
    hit_cat = None
    for cat, blist in bases.items():
        for bn, b in blist:
            if issubclass(anc, b):
                hit_cat = cat; break
        if hit_cat:
            break
    if hit_cat is None:
        continue
    bp_scanned += 1
    sc = _score(nm)
    if sc <= 0:
        continue
    pkg = str(a.get_editor_property("package_name"))
    rows.append({"name": nm, "category": hit_cat, "kind": "blueprint",
                 "path": pkg + "." + nm, "native_parent_class": short,
                 "is_blueprint": True, "score": sc})

# --- authored gameplay tags (only when no category filter or kind=='tag') ---
if include_tags and (kind in ("", "tag")):
    for tn in _authored_tag_names():
        tag_scanned += 1
        sc = _score(tn)
        if sc <= 0:
            continue
        reg, rn = _valid_tag(tn)
        rows.append({"name": tn, "category": "tag", "kind": "gameplay_tag",
                     "registered": reg, "is_blueprint": False, "score": sc})

rows.sort(key=lambda r: (-r["score"], len(r["name"]), r["name"].lower()))
truncated = len(rows) > max_results
rows = rows[:max_results]
result = {"status": "success",
          "queries": queries,
          "kind": (kind if kind in CATS or kind == "tag" else "all"),
          "package_path": package_path,
          "count": len(rows),
          "results": rows,
          "truncated": truncated,
          "scanned": {"native": native_scanned, "blueprint": bp_scanned, "tags": tag_scanned},
          "note": ("Ranked keyword search across GAS assets: native C++ subclasses of "
              "UGameplayAbility / UGameplayEffect / UAttributeSet / GameplayCueNotify (class "
              "reflection), /Game Blueprint assets whose NativeParentClass resolves to one of those "
              "(AssetRegistry, no asset load), and the project's AUTHORED gameplay tags. Scoring: "
              "exact=100 > prefix=60 > contains=30; empty query returns everything (score 1). Use "
              "kind=ability|effect|attribute_set|cue|tag to restrict.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def search_gas(ctx, query: str = None, queries: list = None, kind: str = None,
                   package_path: str = "/Game", max_results: int = 40,
                   include_native: bool = True, include_tags: bool = True) -> str:
        """Ranked keyword search across GAS assets and tags. Read-only.

        Searches GameplayAbility / GameplayEffect / AttributeSet / GameplayCueNotify (both native
        C++ subclasses via class reflection and /Game Blueprint assets via the AssetRegistry) plus
        the project's authored gameplay tags — so it returns real data even with zero project GAS
        Blueprint assets.

        query / queries: one or more case-insensitive substrings (exact > prefix > contains
                         ranking; any query matching scores the entry). Empty returns all.
        kind:            restrict to ability | effect | attribute_set | cue | tag (default: all).
        package_path:    content root to scan for Blueprint assets (default '/Game').
        include_native:  also scan engine/plugin C++ GAS subclasses (default True).
        include_tags:    also search authored gameplay tags (default True; ignored under a class kind).
        max_results:     cap the ranked results.

        Returns results[] {name, category, kind, path, is_blueprint, score, ...} sorted by score."""
        params = {"query": query, "queries": queries, "kind": kind, "package_path": package_path,
                  "max_results": max_results, "include_native": include_native,
                  "include_tags": include_tags}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_attributes — standalone attribute list for one AttributeSet    #
    # ------------------------------------------------------------------ #
    _LIST_ATTRS_BODY = _GAS2_HELPERS + r'''
ident = PARAMS.get("attribute_set")
r = _resolve_cdo(ident)
if r["err"]:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": r["err"]}))
else:
    cdo = r["cdo"]; cls = r["cls"]
    is_attrset = bool(inspect.isclass(cls) and issubclass(cls, unreal.AttributeSet)) if cls else None
    props = _mrl_props(cdo)
    attrs = _attr_props(cdo, props) if props is not None else None
    result = {"status": "success",
              "attribute_set": {"name": r["name"], "path": r["path"],
                                "class_name": r["class_name"], "resolved_kind": r["kind"]},
              "is_attribute_set_subclass": is_attrset,
              "attribute_count": (len(attrs) if attrs is not None else None),
              "attributes": attrs,
              "reflection": {"mcp_reflection_library": _HAS_MRL},
              "note": ("The FGameplayAttributes an AttributeSet defines, read from the CDO's numeric "
                  "UPROPERTYs via MCPReflectionLibrary: FGameplayAttributeData -> base_value/"
                  "current_value; plain float/double -> default_value. UObject-owned props are "
                  "excluded. Standalone attribute list (same data get_attribute_set_info folds in). "
                  "If reflection was unavailable, 'attributes' is null.")}
    if is_attrset is False:
        result["warning"] = ("resolved class is not a UAttributeSet subclass; attributes may be "
                             "empty or irrelevant.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_attributes(ctx, attribute_set: str) -> str:
        """List the FGameplayAttributes an AttributeSet class defines. Read-only.

        attribute_set: a native class name (e.g. 'AbilitySystemTestAttributeSet') OR a /Game
                       Blueprint asset path (e.g. '/Game/GAS/BP_MyAttributes').

        Returns each attribute UPROPERTY with its default value: FGameplayAttributeData attributes
        report base_value/current_value; plain float/double attributes report default_value. This
        is the standalone form of the attribute list otherwise folded inside get_attribute_set_info.
        Errors cleanly if the target cannot be resolved; warns if it is not a UAttributeSet."""
        try:
            return json.dumps(_exec(_LIST_ATTRS_BODY, {"attribute_set": attribute_set}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_gameplay_effect_components — standalone GEComponents list       #
    # ------------------------------------------------------------------ #
    _LIST_GEC_BODY = _GAS2_HELPERS + r'''
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
        components = []
        idx = 0
        raw = _try(lambda: cdo.get_editor_property("ge_components"), []) or []
        for c in raw:
            rec = {"index": idx,
                   "class_name": _try(lambda c=c: c.get_class().get_name()),
                   "class_path": _try(lambda c=c: c.get_class().get_path_name())}
            if c is None:
                rec["class_name"] = None
                rec["is_null"] = True
            components.append(rec)
            idx += 1
        result = {"status": "success",
                  "effect": {"name": r["name"], "path": r["path"],
                             "class_name": r["class_name"], "resolved_kind": r["kind"]},
                  "component_count": len(components),
                  "components": components,
                  "reflection": {"mcp_reflection_library": _HAS_MRL},
                  "note": ("The UGameplayEffectComponent entries on the GameplayEffect CDO's "
                      "'ge_components' array (UE5.3+ tag/requirement/cue plumbing lives here), each "
                      "with its index + component class. Standalone form of the component list "
                      "otherwise folded inside get_gameplay_effect_info. Deep per-component internals "
                      "are not expanded here (use the specific inspect tools). An empty array is a "
                      "genuine CDO default (common on the engine base class).")}
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_gameplay_effect_components(ctx, effect: str) -> str:
        """List the UGameplayEffectComponent entries on a GameplayEffect. Read-only.

        effect: a native class name (e.g. 'GameplayEffect') OR a /Game Blueprint asset path
                (e.g. '/Game/GAS/GE_Damage').

        Returns each entry in the CDO's 'ge_components' array with its index and component class
        (path). In UE5.3+ much GameplayEffect tag/requirement/cue plumbing lives in these components.
        This is the standalone form of the component list otherwise folded inside
        get_gameplay_effect_info. Errors cleanly if the target is not a UGameplayEffect."""
        try:
            return json.dumps(_exec(_LIST_GEC_BODY, {"effect": effect}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- C++ #22: RUNTIME ability-system reader (needs a live PIE world) ----
    # Sent LEAN (raw code, no _GAS2_HELPERS / no _wrap footprint) since it calls a C++ reflection handler
    # (avoids the crash-#2 footprint class seen with heavy helper blocks + C++ handlers).
    def _lean_send(code):
        resp = send_command("execute_python", {"code": code})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    _ASC_INFO_LEAN = (
        "import unreal, json\n"
        "ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
        "gw = ues.get_game_world() if les.is_in_play_in_editor() else None\n"
        "if gw is None:\n"
        "    print('@@UMCP@@' + json.dumps({'status': 'error', 'message': 'not in PIE — call play_in_editor, poll get_pie_status until in_pie, then retry'}))\n"
        "else:\n"
        "    _name = PARAMS.get('actor_name') or ''\n"
        "    _t = None\n"
        "    if _name:\n"
        "        for _a in unreal.GameplayStatics.get_all_actors_of_class(gw, unreal.Actor):\n"
        "            try:\n"
        "                if _a.get_name() == _name or (_name.lower() in _a.get_name().lower()) or _a.get_actor_label() == _name:\n"
        "                    _t = _a; break\n"
        "            except Exception:\n"
        "                pass\n"
        "    else:\n"
        "        _pc = unreal.GameplayStatics.get_player_controller(gw, 0)\n"
        "        _t = _pc.get_controlled_pawn() if _pc else None\n"
        "    if _t is None:\n"
        "        print('@@UMCP@@' + json.dumps({'status': 'error', 'message': 'actor not found: %s' % (_name or '(player pawn)')}))\n"
        "    else:\n"
        "        _m = getattr(unreal, 'MCPReflectionLibrary', None)\n"
        "        _fn = getattr(_m, 'get_ability_system_info_json', None) if _m is not None else None\n"
        "        if _fn is None:\n"
        "            print('@@UMCP@@' + json.dumps({'status': 'error', 'message': 'C++ handler get_ability_system_info_json unavailable'}))\n"
        "        else:\n"
        "            print('@@UMCP@@' + json.dumps(json.loads(_fn(_t))))\n")

    @mcp.tool()
    def get_ability_system_info(ctx, actor_name: str = "") -> str:
        """Read a LIVE AbilitySystemComponent on an actor in the running PIE world (C++ #22 handler):
        attributes (name/base/current), owned gameplay tags, granted abilities, active-effect count.

        REQUIRES PIE — call play_in_editor, poll get_pie_status until in_pie is true, THEN call this.
        actor_name: substring / exact-name / label match in the game world (empty = the player-controlled pawn).
        Returns {actor, has_asc, attributes[], owned_tags[], abilities[], active_effect_count, ...}."""
        try:
            b64 = base64.b64encode(json.dumps({"actor_name": actor_name}).encode("utf-8")).decode("ascii")
            header = ('import base64 as _b64, json as _json\n'
                      'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
            return json.dumps(_lean_send(header + _ASC_INFO_LEAN), indent=2)
        except Exception as e:
            return f"Error: {e}"
