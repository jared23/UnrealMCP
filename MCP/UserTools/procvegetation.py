"""UserTools :: Procedural Vegetation  (spec: docs/spec/pcg.md — procedural-vegetation family)

Clean-room tools for the EXPERIMENTAL `ProceduralVegetationEditor` engine plugin (UE 5.8,
plugin enabled + modules loaded on this project). Procedural Vegetation is built ON TOP of PCG:

  - `UProceduralVegetation` is the ASSET (a graph wrapper; class path
    `/Script/ProceduralVegetation.ProceduralVegetation`, NOT exposed as `unreal.ProceduralVegetation`).
    Its inner PCG graph lives in a PROTECTED `Graph` UPROPERTY that Python cannot read directly;
    the graph subobject is reachable via `unreal.load_object(pv_asset, "ProceduralVegetationGraph")`
    (verified live across all 4 shipped samples — resolves the inner UProceduralVegetationGraph).
  - `UProceduralVegetationGraph : public UPCGGraph` — a PV graph IS a PCG graph, so pcg.py's graph /
    node reflection reads it directly (nodes, edges, settings, pins, positions).
  - `UPVBaseSettings : public UPCGSettings` — the base for the PV node types (57 reflectable
    PVBaseSettings subclasses on this build; each is a UPCGSettings so pcg.py's settings reflection
    reads its CDO). PV node CATEGORY is NOT reflectable (GetCategoryOverride is WITH_EDITOR / not a
    UPROPERTY, and the settings CDO exposes no pin accessors) -> we group by NAME family and, for
    describe, degrade pins with a note (pins are computed per graph-node instance, not on the CDO).

REUSE (this module is a near-verbatim clone of MCP/UserTools/pcg.py):
  - scaffolding _wrap / _query / _exec / _LOG_* and the `_HELPERS` block (_prop_names,
    _base_prop_names, _ser, _node_position, _settings_of, _key_settings, _pin_labels) are copied
    from pcg.py verbatim.
  - search_pv_nodes          <- clone of pcg.list_pcg_settings_classes (base PCGSettings->PVBaseSettings)
  - list_pv_node_categories  <- name-family grouping over that same class list
  - describe_pv_nodes        <- clone of pcg.get_pcg_node_info, reflecting a class CDO (not a node)
  - list_pv_samples          <- clone of pcg.list_pcg_graphs (class ProceduralVegetation, plugin root)
  - export_pv_schema         <- describe output serialized to Saved/MCP_Exports/<name>.json
  - export_pcg_graph_config  <- clone of pcg.get_pcg_graph_info (full depth) -> Saved/MCP_Exports file
  - preview_pv_node          <- clone of pcg.get_pcg_node_info (node inspection; NOT graph execution)
  - create_procedural_vegetation <- the ONE write; foliage_write.py create_asset ledger pattern.

Read-only tools record NO ledger (verbatim pcg.py). Only create_procedural_vegetation writes, using
the EXISTING editor_level.undo `create_asset` branch (delete the created asset) — no new undo fold.

Snippet rules honored (PROTOCOL #1): no ''' and no backslashes inside any snippet body; all data
crosses as base64 PARAMS; no reserved local names (sys/unreal/traceback/output_file/... are avoided).
"""
import json
import base64
import textwrap
import os
import time

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from pcg.py / editor_level.py) ----------
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


# Shared Unreal-side reflection helpers — copied VERBATIM from pcg.py's _HELPERS.
# (no ''' and no backslashes — the handler wraps code in '''...''').
_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")

def _prop_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return sorted(names)

def _base_prop_names(base_cls):
    out = set()
    for klass in base_cls.__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__"):
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                out.add(name)
    return out

def _ser(v, depth):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [v.x, v.y, v.z]
    if isinstance(v, unreal.Rotator):
        return [v.pitch, v.yaw, v.roll]
    if isinstance(v, unreal.Vector2D):
        return [v.x, v.y]
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.Guid):
        return str(v)
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Array):
        items = []
        for i, e in enumerate(v):
            if i >= _ARRAY_LIMIT:
                items.append("...(%d more)" % (len(v) - _ARRAY_LIMIT)); break
            items.append(_ser(e, depth))
        return items
    if isinstance(v, unreal.Set):
        return [_ser(e, depth) for e in list(v)[:_ARRAY_LIMIT]]
    if isinstance(v, unreal.Map):
        d = {}
        for i, k in enumerate(v.keys()):
            if i >= _ARRAY_LIMIT:
                break
            try: d[str(k)] = _ser(v[k], depth)
            except Exception: d[str(k)] = "<err>"
        return d
    if isinstance(v, unreal.Object):
        try: return {"__object__": v.get_path_name(), "class": v.get_class().get_name()}
        except Exception: return str(v)
    if isinstance(v, unreal.StructBase):
        if depth <= 0:
            return "<struct %s>" % type(v).__name__
        d = {}
        for pn in _prop_names(v):
            try: d[pn] = _ser(v.get_editor_property(pn), depth - 1)
            except Exception: d[pn] = "<unreadable>"
        return d if d else ("<struct %s>" % type(v).__name__)
    return str(v)

def _node_position(n):
    try:
        p = n.get_node_position()
    except Exception:
        return None
    if p is None:
        return None
    if hasattr(p, "x") and hasattr(p, "y"):
        return [p.x, p.y]
    try:
        return [p[0], p[1]]
    except Exception:
        return None

def _settings_of(node):
    try:
        return node.get_settings()
    except Exception:
        return None

def _key_settings(settings, limit):
    if settings is None:
        return None, None
    base = getattr(unreal, "PCGSettings", None)
    base_names = _base_prop_names(base) if base is not None else set()
    allnames = _prop_names(settings)
    specific = [n for n in allnames if n not in base_names]
    for common in ("seed", "use_seed", "enabled"):
        if common in allnames and common not in specific:
            specific.append(common)
    specific = sorted(set(specific))[:int(limit)]
    out = {}
    for n in specific:
        try:
            out[n] = _ser(settings.get_editor_property(n), 1)
        except Exception:
            out[n] = "<unreadable>"
    return out, settings.get_class().get_name()

def _pin_labels(node, prop):
    try:
        pins = list(node.get_editor_property(prop) or [])
    except Exception:
        return []
    out = []
    for p in pins:
        lbl = None
        try:
            lbl = str(p.get_editor_property("properties").get_editor_property("label"))
        except Exception:
            lbl = None
        rec = {"label": lbl}
        try:
            rec["connected"] = bool(p.is_connected())
        except Exception:
            pass
        out.append(rec)
    return out
'''

# PV-graph resolver (prepended to graph-consuming bodies). A ProceduralVegetation asset's inner PCG
# graph is a subobject reachable by name (its `Graph` UPROPERTY is protected/unreadable). No ''' / no
# backslashes.
_PV_RESOLVE = r'''
def _pv_resolve_graph(p):
    a = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if a is None:
        return None, None, "asset not found: %s" % p
    if isinstance(a, unreal.PCGGraph):
        return a, a, None
    inner = None
    try:
        inner = unreal.load_object(a, "ProceduralVegetationGraph")
    except Exception:
        inner = None
    if inner is not None and isinstance(inner, unreal.PCGGraph):
        return a, inner, None
    cn = None
    try:
        cn = a.get_class().get_name()
    except Exception:
        cn = "?"
    return a, None, "asset is not a PCGGraph and exposes no ProceduralVegetationGraph subobject (class=%s): %s" % (cn, p)
'''

# Session-aware ledger helper (verbatim from foliage_write.py / editor_level.py) — write body only.
_COERCE_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
'''

# ------------------------------------------------------------------------- #
# Host-side PV node-name -> family categorizer (name-family grouping is the  #
# ship: PV category metadata is not reflectable). Ordered; first substring   #
# match wins; anything unmatched -> "Other".                                 #
# ------------------------------------------------------------------------- #
_PV_FAMILIES = [
    ("Grower", ["Grower"]),
    ("MeshBuilder", ["MeshBuilder", "Mesher"]),
    ("GrowthData", ["GrowthData"]),
    ("Graft", ["Graft"]),
    ("Foliage", ["Foliage"]),
    ("Distribution", ["Distribution", "Distributor"]),
    ("TrunkTexture", ["TrunkTexture"]),
    ("Texture", ["Texture"]),
    ("Extract", ["Extract"]),
    ("MockTree", ["MockTree"]),
    ("Import", ["Import"]),
    ("PlantProfile", ["PlantProfile"]),
    ("Preset", ["Preset"]),
    ("Loader", ["Loader"]),
    ("Simplify", ["Simpl"]),
    ("Export", ["Export"]),
    ("Output", ["Output"]),
    ("Seed", ["Seed"]),
    ("Branch", ["Branch"]),
    ("Recompute", ["Recompute"]),
    ("Scale", ["Scale"]),
    ("Gravity", ["Gravity"]),
    ("Slope", ["Slope"]),
    ("Carve", ["Carve"]),
    ("Bone", ["Bone"]),
    ("ObjectInteraction", ["ObjectInteraction", "Object"]),
    ("ManualEdit", ["ManualEdit", "Manual"]),
]


def _pv_family(cls_name):
    for cat, kws in _PV_FAMILIES:
        for kw in kws:
            if kw in cls_name:
                return cat
    return "Other"


def _safe_export_name(name):
    """Reduce an export name to a safe single-file basename (no dirs / traversal)."""
    base = os.path.basename(str(name or "pv_export")).strip()
    keep = "".join(c for c in base if c.isalnum() or c in ("_", "-", ".", " "))
    keep = keep.strip().rstrip(".") or "pv_export"
    if keep.lower().endswith(".json"):
        keep = keep[:-5]
    return keep


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER payload."""
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
        """Inject PARAMS (base64 JSON) + _session, run the body in Unreal, return MARKER payload.
        _session is harmless for read-only bodies (they ignore it) and drives the ledger for the
        one write tool."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Snippet: enumerate reflectable PVBaseSettings subclasses (the authorable PV node types).
    _PV_CLASSES_BODY = r'''
import unreal, json
base = getattr(unreal, "PVBaseSettings", None)
out = []
if base is not None:
    for nm in dir(unreal):
        obj = getattr(unreal, nm, None)
        try:
            if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
                out.append(nm)
        except Exception:
            pass
out = sorted(out)
print("@@UMCP@@" + json.dumps({"status": "success", "base_class": "PVBaseSettings",
    "has_base": base is not None, "total": len(out), "classes": out}))
'''

    def _pv_class_list():
        """Fetch the full PVBaseSettings subclass list from the editor (raises on plugin-off)."""
        res = _exec(_PV_CLASSES_BODY, {})
        if not res.get("has_base"):
            raise RuntimeError("unreal.PVBaseSettings not found — ProceduralVegetationEditor plugin "
                               "is not enabled/loaded")
        return res.get("classes") or []

    # ------------------------------------------------------------------ #
    # 1. search_pv_nodes                                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def search_pv_nodes(ctx, filter: str = None, filters: list = None,
                        category: str = None, max_results: int = 200) -> str:
        """Search the authorable Procedural-Vegetation node types (the reflectable UPVBaseSettings
        subclasses — clone of pcg.list_pcg_settings_classes with base PVBaseSettings). Read-only.

        filter:      case-insensitive name substring (e.g. 'Grower', 'Distribution').
        filters:     list of substrings; a class matches if ANY of them is present (OR).
        category:    restrict to a name-family category (see list_pv_node_categories, e.g.
                     'Grower', 'MeshBuilder', 'Distribution'). Case-insensitive.
        max_results: cap returned entries; 'total_matched' reports the full count.

        Each entry: {class, category}. Categories are name-family derived (PV category metadata is
        not reflectable on this build)."""
        try:
            names = _pv_class_list()
            subs = []
            if filter:
                subs.append(str(filter))
            if filters:
                subs.extend([str(x) for x in filters])
            if subs:
                low = [s.lower() for s in subs]
                names = [n for n in names if any(s in n.lower() for s in low)]
            if category:
                cl = str(category).lower()
                names = [n for n in names if _pv_family(n).lower() == cl]
            total = len(names)
            capped = names[:int(max_results)] if max_results else names
            nodes = [{"class": n, "category": _pv_family(n)} for n in capped]
            return json.dumps({"status": "success", "base_class": "PVBaseSettings",
                               "total_matched": total, "returned": len(nodes),
                               "category": category, "nodes": nodes}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. list_pv_node_categories                                          #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def list_pv_node_categories(ctx, filter: str = None) -> str:
        """Group the Procedural-Vegetation node types into name-family CATEGORIES (Grower,
        MeshBuilder, Distribution, Graft, Foliage, TrunkTexture, Texture, Extract, Import, Simplify,
        Export, Output, Seed, ...). Read-only.

        filter: case-insensitive substring to narrow to matching category NAMES.

        NOTE: PV node category metadata is NOT reflectable (GetCategoryOverride is WITH_EDITOR /
        non-UPROPERTY), so this is a NAME-family grouping over the 57 PVBaseSettings subclasses, not
        the editor's own palette categories. Each category: {count, classes:[...]}."""
        try:
            names = _pv_class_list()
            cats = {}
            for n in names:
                fam = _pv_family(n)
                rec = cats.setdefault(fam, {"count": 0, "classes": []})
                rec["count"] += 1
                rec["classes"].append(n)
            if filter:
                f = str(filter).lower()
                cats = {k: v for k, v in cats.items() if f in k.lower()}
            ordered = {k: cats[k] for k in sorted(cats.keys())}
            return json.dumps({"status": "success", "base_class": "PVBaseSettings",
                               "total_classes": sum(v["count"] for v in cats.values()),
                               "category_count": len(ordered),
                               "grouping": "name-family (PV category metadata not reflectable)",
                               "categories": ordered}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. describe_pv_nodes  (+ reused by 4. export_pv_schema)             #
    # ------------------------------------------------------------------ #
    # Reflect each PV settings CLASS via its CDO (get_pcg_node_info-style, but on a class default
    # object, not a placed node). Pins are NOT reflectable off a settings CDO -> degraded with a note.
    _DESCRIBE_BODY = _HELPERS + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 50)
names = PARAMS.get("class_names")
include_pins = bool(PARAMS.get("include_pins", True))
include_properties = bool(PARAMS.get("include_properties", True))
depth = int(PARAMS.get("max_depth") or 1)
max_props = int(PARAMS.get("max_properties") or 200)
out_file = PARAMS.get("out_file")
base = getattr(unreal, "PVBaseSettings", None)
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "PVBaseSettings not found (ProceduralVegetationEditor plugin not loaded)"}))
else:
    if not names:
        names = []
        for _nm in dir(unreal):
            _o = getattr(unreal, _nm, None)
            try:
                if isinstance(_o, type) and issubclass(_o, base) and _o is not base:
                    names.append(_nm)
            except Exception:
                pass
        names = sorted(names)
    pcgbase = getattr(unreal, "PCGSettings", None)
    base_names = _base_prop_names(pcgbase) if pcgbase is not None else set()
    results = []
    missing = []
    for cn in names:
        cls = getattr(unreal, cn, None)
        ok = False
        try:
            ok = isinstance(cls, type) and issubclass(cls, base) and cls is not base
        except Exception:
            ok = False
        if not ok:
            missing.append(cn); continue
        try:
            cdo = unreal.get_default_object(cls)
        except Exception:
            missing.append(cn); continue
        rec = {"class": cn}
        try:
            rec["settings_class"] = cdo.get_class().get_name()
        except Exception:
            rec["settings_class"] = cn
        allp = _prop_names(cdo)
        specific = [p for p in allp if p not in base_names]
        rec["property_count"] = len(allp)
        rec["node_specific_property_count"] = len(specific)
        rec["node_specific_properties"] = specific
        if include_properties:
            props = {}
            shown = allp[:max_props]
            for pn in shown:
                try:
                    props[pn] = _ser(cdo.get_editor_property(pn), depth)
                except Exception:
                    props[pn] = "<unreadable>"
            rec["properties"] = props
            rec["properties_truncated"] = len(allp) > max_props
        if include_pins:
            rec["pins"] = None
            rec["pins_note"] = "pins are not reflectable from a settings CDO; they are computed per placed graph-node. Inspect a node in a graph via preview_pv_node or pcg.get_pcg_node_info."
        results.append(rec)
    res = {"status": "success", "base_class": "PVBaseSettings",
           "requested": len(names), "described": len(results), "nodes": results}
    if missing:
        res["not_found"] = missing
    if out_file:
        import os as _os2
        _sd = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
        _od = _os2.path.join(_sd, "MCP_Exports")
        _os2.makedirs(_od, exist_ok=True)
        _fp = _os2.path.join(_od, str(out_file) + ".json")
        res["file"] = _fp
        res["written"] = True
        _fh2 = open(_fp, "w", encoding="utf-8")
        try:
            _fh2.write(json.dumps(res, indent=2, default=str))
        finally:
            _fh2.close()
    print("@@UMCP@@" + json.dumps(res, default=str))
'''

    def _resolve_describe_names(node, nodes, category):
        """Host-side selection of the class list for describe/export (node/nodes/category)."""
        if nodes:
            return [str(x) for x in nodes]
        if node:
            return [str(node)]
        if category:
            alln = _pv_class_list()
            cl = str(category).lower()
            return [n for n in alln if _pv_family(n).lower() == cl]
        return None  # None -> snippet enumerates ALL PVBaseSettings subclasses

    @mcp.tool()
    def describe_pv_nodes(ctx, node: str = None, nodes: list = None, include_pins: bool = True,
                          include_properties: bool = True, max_depth: int = 1,
                          max_properties: int = 200, category: str = None) -> str:
        """Describe Procedural-Vegetation node types by reflecting each settings class's CDO — its
        reflected UPROPERTYs (values), split into node-specific vs inherited-from-PCGSettings.
        Read-only (clone of pcg.get_pcg_node_info applied to a class default object).

        node:     one class name (e.g. 'PVGrowerSettings').
        nodes:    list of class names.
        category: describe every class in a name-family category (see list_pv_node_categories).
        (With none of node/nodes/category set, ALL 57 PV node types are described.)
        include_pins:       best-effort pins. Pins are NOT reflectable from a settings CDO, so this
                            returns pins=null plus a note (inspect a placed node via preview_pv_node).
        include_properties: include the reflected property values.
        max_depth:          struct-expansion depth for property values (default 1).
        max_properties:     cap properties serialized per class.

        Per node: {class, settings_class, property_count, node_specific_properties, properties,
        pins (null + note)}."""
        try:
            names = _resolve_describe_names(node, nodes, category)
            params = {"class_names": names, "include_pins": include_pins,
                      "include_properties": include_properties, "max_depth": max_depth,
                      "max_properties": max_properties}
            return json.dumps(_exec(_DESCRIBE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. export_pv_schema                                                 #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def export_pv_schema(ctx, nodes: list = None, name: str = "pv_schema",
                         max_depth: int = 1) -> str:
        """Reflect Procedural-Vegetation node types (like describe_pv_nodes) and WRITE the schema to
        a JSON file under <project>/Saved/MCP_Exports/<name>.json. Read-only w.r.t. asset/level state
        (writes only a host Saved file; no ledger).

        nodes:     list of class names to include; omit to export ALL PV node types.
        name:      output file base name (sanitized; '.json' appended). Default 'pv_schema'.
        max_depth: struct-expansion depth for property values.

        Returns the written file path and the node count. Parse the file for the full schema."""
        try:
            safe = _safe_export_name(name)
            params = {"class_names": ([str(x) for x in nodes] if nodes else None),
                      "include_pins": True, "include_properties": True,
                      "max_depth": max_depth, "max_properties": 500, "out_file": safe}
            res = _exec(_DESCRIBE_BODY, params)
            slim = {"status": res.get("status"), "file": res.get("file"),
                    "written": res.get("written"), "node_count": res.get("described"),
                    "requested": res.get("requested")}
            if res.get("not_found"):
                slim["not_found"] = res.get("not_found")
            if "_log_warnings" in res:
                slim["_log_warnings"] = res["_log_warnings"]
            return json.dumps(slim, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 6. list_pv_samples                                                  #
    # ------------------------------------------------------------------ #
    _LIST_SAMPLES_BODY = r'''
import unreal, json
path_filter = PARAMS.get("path_filter")
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
samples = []
err = None
try:
    tlap = unreal.TopLevelAssetPath("/Script/ProceduralVegetation", "ProceduralVegetation")
    assets = ar.get_assets_by_class(tlap, search_sub_classes=True) or []
except Exception as e:
    assets = []
    err = str(e)
for a in assets:
    name = str(a.asset_name)
    pkg = str(a.package_name)
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    if name_filter and name_filter.lower() not in name.lower():
        continue
    samples.append({"name": name, "path": pkg,
                    "class": str(a.asset_class_path.asset_name)})
samples.sort(key=lambda g: g["path"])
total = len(samples)
if max_results:
    samples = samples[:int(max_results)]
res = {"status": "success", "asset_class": "ProceduralVegetation",
       "total": total, "returned": len(samples), "samples": samples}
if err:
    res["registry_error"] = err
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_pv_samples(ctx, filter: str = None, max_results: int = 100,
                        path_filter: str = "/ProceduralVegetationEditor") -> str:
        """List UProceduralVegetation assets via the Asset Registry (clone of pcg.list_pcg_graphs
        with class ProceduralVegetation). Read-only.

        filter:      case-insensitive name substring.
        path_filter: package-path substring; defaults to '/ProceduralVegetationEditor' (the shipped
                     sample library — the 4 PVE_Sample_* assets). Pass '' or '/Game' to widen to
                     project/user-created PV assets.
        max_results: cap returned; 'total' reports the full count.

        Each entry: {name, path (package path), class}."""
        params = {"path_filter": path_filter, "name_filter": filter, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_SAMPLES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. export_pcg_graph_config                                          #
    # ------------------------------------------------------------------ #
    # Clone of pcg.get_pcg_graph_info widened to full depth, with the PV asset -> inner graph
    # resolver, serialized to Saved/MCP_Exports/<name>.json. Works on ANY PCGGraph path (and any
    # ProceduralVegetation asset path via its inner ProceduralVegetationGraph subobject).
    _GRAPH_CONFIG_BODY = _HELPERS + _PV_RESOLVE + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 25)
asset_path = PARAMS.get("graph_path")
max_nodes = PARAMS.get("max_nodes")
key_limit = int(PARAMS.get("key_settings_limit") or 24)
include_edges = bool(PARAMS.get("include_edges", True))
include_settings = bool(PARAMS.get("include_settings", True))
depth = int(PARAMS.get("max_depth") or 3)
out_file = PARAMS.get("out_file")

if not asset_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "graph_path is required"}))
else:
    wrapper, g, gerr = _pv_resolve_graph(asset_path)
    if g is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": gerr}))
    else:
        nodes = list(g.get_editor_property("nodes") or [])
        node_recs = []
        shown = nodes if not max_nodes else nodes[:int(max_nodes)]
        for n in shown:
            rec = {"name": None, "settings_class": None}
            try:
                rec["name"] = n.get_name()
            except Exception:
                pass
            try:
                ot = str(n.get_editor_property("node_title"))
                rec["override_title"] = None if ot in ("None", "") else ot
            except Exception:
                pass
            rec["position"] = _node_position(n)
            s = _settings_of(n)
            if s is not None:
                rec["settings_class"] = s.get_class().get_name()
                if include_settings:
                    ks, _sc = _key_settings(s, key_limit)
                    rec["key_settings"] = ks
                    full_props = {}
                    for pn in _prop_names(s):
                        try:
                            full_props[pn] = _ser(s.get_editor_property(pn), depth)
                        except Exception:
                            full_props[pn] = "<unreadable>"
                    rec["settings"] = full_props
            try:
                rec["input_pins"] = _pin_labels(n, "input_pins")
                rec["output_pins"] = _pin_labels(n, "output_pins")
            except Exception:
                pass
            node_recs.append(rec)

        def _nname(x):
            try:
                return x.get_name()
            except Exception:
                return None
        inn = None; onn = None
        try:
            inn = _nname(g.get_input_node())
        except Exception:
            pass
        try:
            onn = _nname(g.get_output_node())
        except Exception:
            pass

        edges = []
        edge_total = 0
        if include_edges:
            try:
                alledges = list(g.get_all_edges() or [])
                edge_total = len(alledges)
                for e in alledges[: _ARRAY_LIMIT * 8]:
                    try:
                        edges.append({
                            "from_node": _nname(e.get_input_node()),
                            "from_pin": str(e.get_input_pin_label()),
                            "to_node": _nname(e.get_output_node()),
                            "to_pin": str(e.get_output_pin_label()),
                        })
                    except Exception:
                        pass
            except Exception:
                edge_total = None

        params_out = None
        try:
            up = g.get_editor_property("user_parameters")
            if up is not None and hasattr(up, "to_dict"):
                params_out = up.to_dict()
        except Exception:
            params_out = None

        wrap_cls = None
        try:
            wrap_cls = wrapper.get_class().get_name()
        except Exception:
            pass
        res = {"status": "success", "graph_path": asset_path,
               "source_asset_class": wrap_cls,
               "graph_name": g.get_name(),
               "graph_path_name": g.get_path_name(),
               "is_procedural_vegetation": (wrap_cls == "ProceduralVegetation"),
               "node_count": len(nodes), "nodes_returned": len(node_recs),
               "input_node": inn, "output_node": onn,
               "edge_count": edge_total, "edges": edges,
               "graph_parameters": params_out,
               "nodes": node_recs}
        if out_file:
            import os as _os2
            _sd = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
            _od = _os2.path.join(_sd, "MCP_Exports")
            _os2.makedirs(_od, exist_ok=True)
            _fp = _os2.path.join(_od, str(out_file) + ".json")
            res["file"] = _fp
            res["written"] = True
            _fh2 = open(_fp, "w", encoding="utf-8")
            try:
                _fh2.write(json.dumps(res, indent=2, default=str))
            finally:
                _fh2.close()
        print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def export_pcg_graph_config(ctx, graph_path: str, name: str = "pcg_graph",
                                max_depth: int = 3, max_nodes: int = None) -> str:
        """Serialize a PCG graph's full configuration (nodes, settings at full depth, pins, edges,
        input/output nodes, graph parameters) to <project>/Saved/MCP_Exports/<name>.json. Read-only;
        no ledger (writes only a host Saved file). Clone of pcg.get_pcg_graph_info widened to full depth.

        graph_path: content path to a PCGGraph asset, OR a ProceduralVegetation asset path (its inner
                    ProceduralVegetationGraph subobject is resolved automatically).
        name:       output file base name (sanitized; '.json' appended). Default 'pcg_graph'.
        max_depth:  struct-expansion depth for each node's settings (default 3 — full config).
        max_nodes:  cap nodes serialized (node_count reports the full total).

        Returns the written file path and the node count. Parse the file for the full config."""
        try:
            safe = _safe_export_name(name)
            params = {"graph_path": graph_path, "name_unused": None, "out_file": safe,
                      "max_depth": max_depth, "max_nodes": max_nodes}
            res = _exec(_GRAPH_CONFIG_BODY, params)
            if res.get("status") != "success":
                return json.dumps(res, indent=2)
            slim = {"status": "success", "graph_path": graph_path,
                    "source_asset_class": res.get("source_asset_class"),
                    "is_procedural_vegetation": res.get("is_procedural_vegetation"),
                    "graph_name": res.get("graph_name"),
                    "node_count": res.get("node_count"),
                    "nodes_returned": res.get("nodes_returned"),
                    "edge_count": res.get("edge_count"),
                    "file": res.get("file"), "written": res.get("written")}
            if "_log_warnings" in res:
                slim["_log_warnings"] = res["_log_warnings"]
            return json.dumps(slim, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. preview_pv_node  (ships as node INSPECTION, not graph execution) #
    # ------------------------------------------------------------------ #
    # Resolve a graph (PCGGraph or a PV asset's inner graph), find a node by name or positional index
    # in nodes[], and return its settings/pins/position (pcg.get_pcg_node_info lookup). Does NOT
    # execute/generate the graph (that is editor-only/heavy and would mutate).
    _PREVIEW_BODY = _HELPERS + _PV_RESOLVE + r'''
_ARRAY_LIMIT = int(PARAMS.get("array_element_limit") or 50)
asset_path = PARAMS.get("graph_path")
node_name = PARAMS.get("node_name")
node_index = PARAMS.get("node_index")
depth = int(PARAMS.get("max_depth") or 2)

if not asset_path:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "graph_path is required"}))
else:
    wrapper, g, gerr = _pv_resolve_graph(asset_path)
    if g is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": gerr}))
    else:
        nodes = list(g.get_editor_property("nodes") or [])
        target = None
        if node_name:
            for n in nodes:
                try:
                    if n.get_name() == node_name:
                        target = n; break
                except Exception:
                    pass
        elif node_index is not None:
            idx = int(node_index)
            if 0 <= idx < len(nodes):
                target = nodes[idx]
        else:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "supply node_name or node_index"}))
            target = "__ERR__"
        if target is None:
            avail = []
            for i, n in enumerate(nodes[:80]):
                try:
                    avail.append({"index": i, "name": n.get_name()})
                except Exception:
                    pass
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "node not found (name=%s index=%s)" % (str(node_name), str(node_index)),
                "node_count": len(nodes), "available_nodes": avail}))
        elif target != "__ERR__":
            s = _settings_of(target)
            settings = {}
            settings_class = None
            if s is not None:
                settings_class = s.get_class().get_name()
                for pn in _prop_names(s):
                    try:
                        settings[pn] = _ser(s.get_editor_property(pn), depth)
                    except Exception:
                        settings[pn] = "<unreadable>"

            def _pins_full(prop):
                out = []
                try:
                    pins = list(target.get_editor_property(prop) or [])
                except Exception:
                    return out
                for p in pins:
                    rec = {}
                    try:
                        rec["label"] = str(p.get_editor_property("properties").get_editor_property("label"))
                    except Exception:
                        rec["label"] = None
                    try:
                        rec["connected"] = bool(p.is_connected())
                    except Exception:
                        pass
                    try:
                        rec["tooltip"] = str(p.get_tooltip())
                    except Exception:
                        pass
                    conns = []
                    try:
                        for e in list(p.get_editor_property("edges") or []):
                            try:
                                conns.append({
                                    "from_node": e.get_input_node().get_name() if e.get_input_node() else None,
                                    "from_pin": str(e.get_input_pin_label()),
                                    "to_node": e.get_output_node().get_name() if e.get_output_node() else None,
                                    "to_pin": str(e.get_output_pin_label()),
                                })
                            except Exception:
                                pass
                    except Exception:
                        pass
                    rec["edges"] = conns
                    out.append(rec)
                return out

            wrap_cls = None
            try:
                wrap_cls = wrapper.get_class().get_name()
            except Exception:
                pass
            res = {"status": "success", "graph_path": asset_path,
                   "source_asset_class": wrap_cls,
                   "graph_name": g.get_name(),
                   "preview_mode": "inspection (settings/pins/position; graph NOT executed)",
                   "node_name": target.get_name(),
                   "node_class": target.get_class().get_name(),
                   "settings_class": settings_class,
                   "position": _node_position(target),
                   "input_pins": _pins_full("input_pins"),
                   "output_pins": _pins_full("output_pins"),
                   "settings": settings}
            print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def preview_pv_node(ctx, graph_path: str, node_name: str = None,
                        node_index: int = None, max_depth: int = 2) -> str:
        """Preview a node inside a Procedural-Vegetation / PCG graph. SHIPS AS INSPECTION: resolves
        the node and returns its settings class, all reflected settings values, pins (with per-pin
        edge connections), and canvas position. It does NOT execute/generate the graph (PCG
        generation is editor-only/heavy and would mutate) — 'preview' here means static inspection.
        Read-only. Clone of pcg.get_pcg_node_info with the PV-asset inner-graph resolver.

        graph_path: a PCGGraph asset path, OR a ProceduralVegetation asset path (inner graph resolved).
        node_name:  node's internal name (e.g. 'ProceduralVegetationGrower_0'); on miss returns
                    'available_nodes'.
        node_index: OR select by positional index into the graph's nodes[] (0-based).
        max_depth:  struct-expansion depth for settings values (default 2)."""
        try:
            params = {"graph_path": graph_path, "node_name": node_name,
                      "node_index": node_index, "max_depth": max_depth}
            return json.dumps(_exec(_PREVIEW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5. create_procedural_vegetation  (THE ONLY WRITE)                   #
    # ------------------------------------------------------------------ #
    # Create a UProceduralVegetation asset. Empty: AssetTools.create_asset(name, pkg, <PVClass>,
    # ProceduralVegetationFactory()). From a sample: EditorAssetLibrary.duplicate_asset(sample, dest).
    # Save on create (reliable undo-delete). Ledger op 'create_asset' -> REUSES the EXISTING
    # editor_level.undo branch (deletes the asset; gc.collect() safety lives there). No new fold.
    _CREATE_BODY = _COERCE_HELPERS + r'''
name = PARAMS.get("name")
sample = PARAMS.get("sample")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
full = pkg + "/" + name
created_dir = None
if unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    asset = None
    err = None
    mode = None
    if sample:
        mode = "duplicate"
        src = unreal.load_asset(sample)
        if src is None:
            err = "sample not found: %s" % sample
        elif src.get_class().get_name() != "ProceduralVegetation":
            err = "sample is not a ProceduralVegetation asset (class=%s): %s" % (src.get_class().get_name(), sample)
        else:
            asset = unreal.EditorAssetLibrary.duplicate_asset(sample, full)
            if asset is None:
                err = "duplicate_asset returned None (%s -> %s)" % (sample, full)
    else:
        mode = "create"
        pvcls = unreal.load_object(None, "/Script/ProceduralVegetation.ProceduralVegetation")
        if pvcls is None:
            err = "could not resolve ProceduralVegetation UClass"
        else:
            at = unreal.AssetToolsHelpers.get_asset_tools()
            fac = unreal.ProceduralVegetationFactory()
            asset = at.create_asset(name, pkg, pvcls, fac)
            if asset is None:
                err = "create_asset returned None for %s" % full
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err, "mode": mode}))
    else:
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        _cls = asset.get_class().get_name()
        _exists = unreal.EditorAssetLibrary.does_asset_exist(full)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": _cls, "mode": mode, "from_sample": sample,
            "package_path": pkg, "created_dir": created_dir, "exists": _exists,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_procedural_vegetation(ctx, asset_path: str = None, sample: str = None,
                                     name: str = None,
                                     package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a UProceduralVegetation asset (the ONLY write in this module). Scratch-only.

        sample:       optional ProceduralVegetation asset path to DUPLICATE (e.g. a PVE_Sample_*
                      from list_pv_samples). If omitted, creates a fresh empty PV asset via
                      AssetTools.create_asset + ProceduralVegetationFactory (verified modal-free).
        asset_path:   optional full target path '/Game/MCP_Scratch/<Name>'. Otherwise built from
                      package_path + name.
        name:         asset name; auto-prefixed 'MCP_A_' for ownership. If unspecified, a unique
                      'MCP_A_PV_<ts>' name is generated.
        package_path: content folder; MUST be under '/Game/MCP_Scratch' (scratch discipline).

        Ledgered write (op 'create_asset', the EXISTING editor_level.undo branch): `undo` deletes the
        created asset (gc.collect() safety handled by that branch). Saved on create so the undo-delete
        is reliable. Creates an ASSET only — never mutates a level."""
        try:
            if asset_path:
                ap = asset_path
                last = ap.split("/")[-1]
                if "." in last:
                    ap = ap.split(".")[0]
                parts = ap.split("/")
                pkg = "/".join(parts[:-1])
                nm = parts[-1]
            else:
                pkg = package_path or "/Game/MCP_Scratch"
                nm = name
            if not nm:
                nm = "MCP_A_PV_" + str(int(time.time()))[-6:]
            if not nm.startswith("MCP_"):
                nm = "MCP_A_" + nm
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg},
                    indent=2)
            params = {"name": nm, "sample": sample, "package_path": pkg}
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
