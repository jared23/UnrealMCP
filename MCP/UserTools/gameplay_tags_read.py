"""UserTools :: Gameplay Tags (READ-ONLY)  (spec: docs/spec/gameplay_tags.md)

Clean-room, READ-ONLY introspection of the project's GameplayTag registry over
Unreal's public Python API (UE 5.8). Executed in the editor's interpreter via the
plugin's `execute_python` handler; results captured from stdout.

Query convention (copied verbatim from editor_level.py / project_info.py): a snippet
prints  @@UMCP@@<json>  on one line; _query() finds that marker and parses the JSON
after it. Every snippet is wrapped with Output-Log delta capture, surfacing new
Warning/Error lines as result['_log_warnings']. Params are passed as base64 JSON.

Commands (all READ-ONLY; no writes, no ledger, no factories, no modals):
  - list_gameplay_tags       — the AUTHORED gameplay tags (project ini + data-table sources),
                               each validated against the live registry; plus an optional
                               probe_names validator for arbitrary candidate names.
  - get_gameplay_tag_info    — for one tag: registered?, parent (+registered), ancestors,
                               depth, direct children (within the reachable authored set),
                               and leaf status.
  - list_gameplay_tag_sources — the config-defined tag sources (ImportTagsFromConfig,
                               inline GameplayTagList, RestrictedTagList, GameplayTagTableList
                               data-table assets) + the DefaultGameplayTags.ini location.

Honest limitations discovered LIVE (UE 5.8, this from-source build):
  - `unreal.GameplayTagsManager` is NOT exposed to Python, and its class object exposes
    NO callable UFUNCTIONs (RequestAllGameplayTags / RequestGameplayTagChildren /
    RequestGameplayTagParents are C++-only). `unreal.GameplayTagLibrary` has NO
    get_all / request_all method either. THEREFORE the FULL registered tag tree
    (which includes native C++ / plugin tags, e.g. "Niagara") is NOT Python-enumerable.
    We CAN validate any single tag name against the live registry (see below), and we
    CAN enumerate the AUTHORED tags defined by config/data sources — but not native tags.
  - `unreal.GameplayTagsSettings` is NOT a Python-exposed class; its UClass loads via
    unreal.load_object("/Script/GameplayTags.GameplayTagsSettings") and its CDO
    (unreal.get_default_object) holds the authored tag lists.
  - `GameplayTag.TagName` is READ-ONLY; a tag cannot be built by setting the name. A tag is
    built from a name string with `GameplayTag.import_text('(TagName="X.Y")')`, which
    CONSULTS the live registry: unregistered names resolve to the empty tag
    (is_gameplay_tag_valid == False). This is the single-tag validation primitive we use.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / project_info.py) ---
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
# Double-quote characters needed at runtime (for GameplayTag import_text) are built with
# chr(34) so the SOURCE contains no backslash-escaped quotes.


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

    # Shared Unreal-side helpers (no ''' / no backslashes). _valid_tag validates a single
    # tag name against the LIVE registry (import_text consults it); unregistered names
    # resolve to the empty tag. _authored_tags aggregates the config/data-source tags from
    # the GameplayTagsSettings CDO (project's authored tags; NOT native C++/plugin tags).
    _GT_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
_Q = chr(34)
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _settings_cdo():
    cls = _try(lambda: unreal.load_object(None, "/Script/GameplayTags.GameplayTagsSettings"))
    if cls is None:
        return None
    return _try(lambda: unreal.get_default_object(cls))
def _valid_tag(name):
    # Returns (registered_bool, resolved_name_or_None). import_text consults the live
    # registry; an unregistered name resolves to the empty tag (name None, valid False).
    if not name:
        return (False, None)
    t = unreal.GameplayTag()
    ok = _try(lambda: t.import_text("(TagName=" + _Q + name + _Q + ")"), None)
    if not unreal.GameplayTagLibrary.is_gameplay_tag_valid(t):
        return (False, None)
    rn = str(unreal.GameplayTagLibrary.get_tag_name(t))
    if rn in ("", "None"):
        return (False, None)
    return (True, rn)
def _rows_tags(rows):
    out = []
    for r in (rows or []):
        tg = _try(lambda r=r: str(r.get_editor_property("tag")))
        cm = _try(lambda r=r: str(r.get_editor_property("dev_comment")))
        if tg and tg not in ("", "None"):
            out.append({"name": tg, "dev_comment": (cm if cm not in ("", "None") else None)})
    return out
def _authored_tags():
    # Aggregate authored tags + their source label. Returns list of {name, dev_comment, source}.
    cdo = _settings_cdo()
    acc = {}
    def _add(recs, src):
        for rec in recs:
            nm = rec["name"]
            if nm not in acc:
                acc[nm] = {"name": nm, "dev_comment": rec.get("dev_comment"), "source": src}
    if cdo is not None:
        _add(_rows_tags(_try(lambda: cdo.get_editor_property("GameplayTagList"), []) or []),
             "DefaultGameplayTags.ini (GameplayTagList)")
        _add(_rows_tags(_try(lambda: cdo.get_editor_property("RestrictedTagList"), []) or []),
             "RestrictedTagList (restricted config)")
        # GameplayTagTableList: data-table assets used as tag sources.
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
            # For a GameplayTagTableRow-based DataTable the row name IS the tag name (the
            # 'Tag' FName field mirrors the row name). We read the row NAMES because Python
            # has no generic per-row struct-field reader here.
            names = _try(lambda asset=asset: unreal.DataTableFunctionLibrary.get_data_table_row_names(asset), []) or []
            recs = [{"name": str(rn), "dev_comment": None} for rn in names]
            _add(recs, "data table: %s" % path)
    return list(acc.values())
'''

    # ------------------------------------------------------------------ #
    # list_gameplay_tags — authored tags + live-registry validation       #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _GT_HELPERS + r'''
flt = (PARAMS.get("filter") or "").lower()
max_results = PARAMS.get("max_results")
probe_names = PARAMS.get("probe_names") or []
authored = _authored_tags()
# validate each authored tag against the live registry
for a in authored:
    reg, rn = _valid_tag(a["name"])
    a["registered"] = reg
authored.sort(key=lambda a: a["name"].lower())
# filter
sel = [a for a in authored if (not flt or flt in a["name"].lower())]
total = len(sel)
truncated = False
if max_results:
    try:
        m = int(max_results)
        if len(sel) > m:
            sel = sel[:m]; truncated = True
    except Exception:
        pass
# optional: validate caller-supplied candidate names against the live registry.
probe = None
if probe_names:
    probe = []
    for nm in probe_names:
        reg, rn = _valid_tag(str(nm))
        probe.append({"name": str(nm), "registered": reg, "resolved": rn})
result = {"status": "success",
          "authored_tag_count": total,
          "returned": len(sel),
          "truncated": truncated,
          "filter": PARAMS.get("filter"),
          "tags": sel,
          "native_registry_enumerable": False,
          "probe_results": probe,
          "note": ("'tags' are the AUTHORED gameplay tags defined by this project's config/"
              "data sources (GameplayTagsSettings CDO: GameplayTagList + RestrictedTagList + "
              "GameplayTagTableList data tables), each validated against the LIVE registry via "
              "GameplayTag.import_text. The FULL registered tree (native C++ / plugin tags such "
              "as 'Niagara') is NOT Python-enumerable: unreal.GameplayTagsManager is not exposed "
              "and exposes no callable UFUNCTIONs, and unreal.GameplayTagLibrary has no get_all/"
              "request_all. Use probe_names=[...] to validate specific candidate names against "
              "the live registry.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_gameplay_tags(ctx, filter: str = None, max_results: int = None,
                           probe_names: list = None) -> str:
        """List the project's AUTHORED gameplay tags, validated against the live registry. Read-only.

        Returns the tags defined by this project's config/data sources (the GameplayTagsSettings
        CDO: inline GameplayTagList, RestrictedTagList, and GameplayTagTableList data-table
        assets). Each returned tag carries 'registered' (whether it resolves in the LIVE tag
        registry) and its 'source'.

        filter:       case-insensitive substring on the dotted tag name.
        max_results:  cap the number of tags returned ('truncated' reports if capped).
        probe_names:  optional list of candidate tag names to validate against the LIVE registry
                      (returned under 'probe_results' as {name, registered, resolved}). This is
                      the way to check native/plugin tags, which are NOT enumerable.

        IMPORTANT LIMITATION (in the payload as 'note'): the FULL registered tree — native C++
        and plugin tags (e.g. 'Niagara') — is NOT Python-enumerable in UE 5.8 because
        unreal.GameplayTagsManager is not exposed (and exposes no callable UFUNCTIONs) and
        unreal.GameplayTagLibrary has no get_all/request_all. Only AUTHORED tags are enumerable;
        any single name can be validated via probe_names."""
        params = {"filter": filter, "max_results": max_results, "probe_names": probe_names}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_gameplay_tag_info — parent/ancestors/depth/children/leaf        #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _GT_HELPERS + r'''
name = PARAMS.get("tag") or ""
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "tag is required"}))
else:
    registered, resolved = _valid_tag(name)
    segs = [s for s in name.split(".") if s != ""]
    depth = len(segs)
    parent_name = ".".join(segs[:-1]) if depth > 1 else None
    parent_registered = None
    if parent_name is not None:
        parent_registered, _pr = _valid_tag(parent_name)
    # ancestors (each proper prefix), with live-registry validity
    ancestors = []
    for i in range(1, depth):
        anc = ".".join(segs[:i])
        areg, _a = _valid_tag(anc)
        ancestors.append({"name": anc, "registered": areg})
    # direct children — reachable ONLY within the authored set (native children need the
    # manager, which is not exposed). Scan authored tags for direct descendants of `name`.
    authored = _authored_tags()
    prefix = name + "."
    child_set = {}
    for a in authored:
        an = a["name"]
        if an.startswith(prefix):
            rest = an[len(prefix):]
            direct = prefix + rest.split(".")[0]
            if direct not in child_set:
                creg, _c = _valid_tag(direct)
                child_set[direct] = {"name": direct, "registered": creg}
    children = sorted(child_set.values(), key=lambda c: c["name"].lower())
    result = {"status": "success",
              "tag": name,
              "registered": registered,
              "resolved_name": resolved,
              "depth": depth,
              "parent": ({"name": parent_name, "registered": parent_registered}
                         if parent_name is not None else None),
              "ancestors": ancestors,
              "children": children,
              "child_count_reachable": len(children),
              "is_root": depth == 1,
              "is_leaf_within_reachable_scope": (registered and len(children) == 0),
              "note": ("parent/ancestors/depth are derived from the dotted name and each is "
                  "validated against the LIVE registry (GameplayTag.import_text). 'children' are "
                  "direct descendants found in the AUTHORED tag set only; native/plugin children "
                  "are NOT enumerable (unreal.GameplayTagsManager is not exposed and has no "
                  "callable UFUNCTIONs). So 'is_leaf_within_reachable_scope' means no children "
                  "were found among reachable authored tags, not a guarantee of a native leaf.")}
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_gameplay_tag_info(ctx, tag: str) -> str:
        """Inspect one gameplay tag: registration, parent, ancestors, depth, children. Read-only.

        tag: a dotted gameplay tag name (e.g. 'Input.Move' or 'Niagara').

        Returns whether the tag is registered in the LIVE registry, its parent tag (with the
        parent's own registration state), the full ancestor chain (each validated), the depth
        (number of dotted segments), and its direct children.

        LIMITATION (in the payload as 'note'): parent/ancestors/depth are derived from the name
        and validated live, but 'children' are only those found in the project's AUTHORED tag
        set — native/plugin children are NOT enumerable because unreal.GameplayTagsManager is
        not exposed to Python (and exposes no callable UFUNCTIONs). 'is_leaf_within_reachable_scope'
        therefore reflects the reachable authored set, not a guaranteed native leaf."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"tag": tag}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_gameplay_tag_sources — config-defined tag sources              #
    # ------------------------------------------------------------------ #
    _SOURCES_BODY = _GT_HELPERS + r'''
import os
cdo = _settings_cdo()
info = {"status": "success"}
if cdo is None:
    info["settings_reachable"] = False
    info["note_settings"] = "could not load GameplayTagsSettings CDO"
else:
    info["settings_reachable"] = True
    info["import_tags_from_config"] = _try(lambda: bool(cdo.get_editor_property("ImportTagsFromConfig")))
    info["config_file_name"] = _try(lambda: str(cdo.get_editor_property("ConfigFileName")))
    info["new_tag_source"] = _try(lambda: str(cdo.get_editor_property("NewTagSource")))
    gtl = _try(lambda: cdo.get_editor_property("GameplayTagList"), []) or []
    rtl = _try(lambda: cdo.get_editor_property("RestrictedTagList"), []) or []
    info["inline_gameplay_tag_list_count"] = len(gtl)
    info["restricted_tag_list_count"] = len(rtl)
    info["restricted_config_files"] = [str(x) for x in (_try(lambda: cdo.get_editor_property("RestrictedConfigFiles"), []) or [])]
    # GameplayTagTableList: data-table asset sources
    tables = []
    for dt in (_try(lambda: cdo.get_editor_property("GameplayTagTableList"), []) or []):
        tables.append(_try(lambda dt=dt: str(dt)))
    info["gameplay_tag_table_assets"] = tables
    info["gameplay_tag_redirect_count"] = len(_try(lambda: cdo.get_editor_property("GameplayTagRedirects"), []) or [])
# DefaultGameplayTags.ini location + existence (authored tags persist here when config-imported)
cfg = _try(lambda: unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_config_dir())) or ""
ini = os.path.join(cfg, "DefaultGameplayTags.ini") if cfg else None
info["default_gameplay_tags_ini"] = ini
info["default_gameplay_tags_ini_exists"] = _try(lambda: os.path.exists(ini) if ini else False)
info["native_and_plugin_sources_reachable"] = False
info["note"] = ("These are the CONFIG-DEFINED tag sources readable from the GameplayTagsSettings "
    "CDO (project-authored tags): inline GameplayTagList (DefaultGameplayTags.ini), "
    "RestrictedTagList, and GameplayTagTableList data-table assets. The AUTHORITATIVE per-source "
    "list the engine keeps (unreal FGameplayTagSource: native C++ modules and EVERY plugin's "
    "DefaultGameplayTags.ini) is NOT Python-reachable because unreal.GameplayTagsManager is not "
    "exposed and exposes no callable UFUNCTIONs -- that breakdown is DEFERRED (needs C++/native "
    "access). Native tags still resolve in the live registry (validate them via "
    "list_gameplay_tags probe_names).")
print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def list_gameplay_tag_sources(ctx) -> str:
        """List the project's config-defined gameplay-tag sources. Read-only.

        Reads the GameplayTagsSettings CDO and reports: whether tags are imported from config,
        the config file name, the inline GameplayTagList count, the RestrictedTagList count and
        its restricted config files, and the GameplayTagTableList data-table assets used as tag
        sources. Also reports the DefaultGameplayTags.ini path and whether it exists.

        LIMITATION (in the payload as 'note'): the engine's authoritative per-source list
        (unreal FGameplayTagSource — native C++ modules and every plugin's DefaultGameplayTags.ini)
        is NOT Python-reachable because unreal.GameplayTagsManager is not exposed (and exposes no
        callable UFUNCTIONs). That native/plugin source breakdown is DEFERRED (needs native access);
        only the config-defined sources above are returned."""
        try:
            return json.dumps(_exec(_SOURCES_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
