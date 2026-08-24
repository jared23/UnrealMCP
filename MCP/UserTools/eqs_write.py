"""UserTools :: Environment Query System / EQS (WRITE)  (spec: docs/spec/eqs.md)

Clean-room WRITE companion to eqs.py (the READ module). Adds the MISSING spec authoring/validate/run
features. Conventions (base64 PARAMS, Output-Log auto-capture, per-session _ledger, ScopedEditorTransaction,
the generic "create_asset" undo op) are copied verbatim from editor_level.py (the gold standard).

LIVE PROBE FINDINGS (UE 5.8.1, TestMCPSetup — what IS / ISN'T Python-reachable for EQS AUTHORING):
  * create_env_query IS reachable. UEnvQuery assets create non-interactively via
    AssetTools.create_asset(name, path, unreal.EnvQuery, None) — the sole EnvQueryFactory is
    AUTO-SELECTED (it is NOT exported to Python: /Script/AIGraph.EnvQueryFactory and unreal.EnvQueryFactory
    both resolve to None) and it has NO configure dialog, so NO modal pops. Verified: creates a valid EMPTY
    query (option_count 0), saves, deletes cleanly. Reuses the "create_asset" undo op (no new op needed).
  * The EQS runtime STRUCTURE is authored through PROTECTED C++ UPROPERTYs with NO C++ write handler:
      - UEnvQuery.Options (TArray<UEnvQueryOption*>)   -> protected: cannot be READ or SET
      - UEnvQueryOption.Generator, UEnvQueryOption.Tests -> protected: cannot be READ or SET
      - every generator/test CONFIG field (TestPurpose, ScoringEquation/Factor, FilterType, ItemType,
        radii/rings, Center context, ...) -> protected: cannot be READ or SET
    This was confirmed against BOTH stock unreal.set_editor_property AND unreal.MCPReflectionLibrary
    .set_editor_property (identical "... is protected and cannot be set" refusal), and node instances
    reachable by find_object(query,'<Class>_<N>') refuse every config prop read/set.
    *** UPDATE 2026-08-16 — C++ #15 EQS-WRITE HANDLERS ARE NOW LIVE. *** MCPReflectionLibrary now exposes
    the symmetric WRITER for the protected structure (inverse of get_env_query_config_json), so
    options/tests/node-properties ARE now authorable from Python via these handlers (verified live):
      - add_env_query_option(query, generator_class_path)        -> {option_index, ...}
      - remove_env_query_option(query, option_index)
      - add_env_query_test(query, option_index, test_class_path) -> {test_index, ...}
      - remove_env_query_test(query, option_index, test_index)
      - set_env_query_node_property(query, node_locator, prop_name, value_json) -> {set, prev}
    value_json is ARRAY-FORM (the "Windows fix"): a scalar wraps as "[123.0]"/"[true]"/"[\"EnumName\"]",
    a struct/config value as an ExportText string inside an array e.g. "[\"(DefaultValue=77.0)\"]". The
    handler unwraps the single-element array and returns the prior value bare in "prev" (re-wrap in an
    array for the inverse). Locator grammar: "option:<i>", "option:<i>/generator", "option:<i>/test:<j>".
    Each handler is hasattr-guarded: on an un-rebuilt DLL the tool returns a clear "handler unavailable".
  * run_env_query IS NOW LIVE (C++ #25 RunEnvQueryJson) but PIE-GATED. unreal.EnvQueryManager processes a
    query only in a RUNNING (PIE/game) world; the editor (non-PIE) world does not tick EQS. So the tool
    resolves the PIE game world (UnrealEditorSubsystem.get_game_world under is_in_play_in_editor), resolves a
    querier actor there, and calls RunEnvQueryJson -> UEnvQueryManager::RunInstantQuery (synchronous). Outside
    PIE it returns a clear "not in PIE" error (never faked). The coordinator verifies it by running PIE.

Implemented (working):
  - create_env_query   (WRITE; ledgered op "create_asset"; creates an EMPTY query)
  - validate_env_query (READ; integrity pass/warn/fail over the C++ config reader)
  - add_eqs_option     (WRITE; C++ AddEnvQueryOption; NEW op "eqs_add_option"; inverse remove_env_query_option)
  - add_eqs_test       (WRITE; C++ AddEnvQueryTest; NEW op "eqs_add_test"; inverse remove_env_query_test)
  - set_eqs_node_property (WRITE; C++ SetEnvQueryNodeProperty; NEW op "eqs_set_node_property"; inverse re-set prev)
  - remove_eqs_option  (WRITE; C++ RemoveEnvQueryOption; NEW op "eqs_readd_option"; BEST-EFFORT replay inverse)
  - remove_eqs_test    (WRITE; C++ RemoveEnvQueryTest; NEW op "eqs_readd_test"; BEST-EFFORT replay inverse)
  - run_env_query      (READ/RUNTIME; C++ RunEnvQueryJson; PIE-gated; NO ledger; scored result items)

NEW undo ops (schemas reported to .mcp_coord/coordinator-inbox/cpp1516_wire_undo_ops.md for folding into
editor_level.undo): eqs_add_option, eqs_add_test, eqs_set_node_property, eqs_readd_option, eqs_readd_test.
The add/set ops have clean faithful inverses; the two remove ops capture the full option/test config via
get_env_query_config_json BEFORE removal and REPLAY it (add + set node props) on undo -- BEST-EFFORT
(re-appended at the array end, not the original slot; config replayed per-prop best-effort). create_env_query
still REUSES the existing generic "create_asset" op.
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

    # Shared Unreal-side helpers (per-session ledger copied verbatim from editor_level._COERCE_HELPERS).
    # No triple-single-quote / no backslash inside.
    _EQS_W_HELPERS = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _refl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _load_eq(path):
    if not path:
        return None, "no env_query path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    if not isinstance(obj, unreal.EnvQuery):
        return None, "asset is not an EnvQuery (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _norm_class(spec):
    # bare short name (EnvQueryGenerator_Donut) -> /Script/AIModule.<name>; a path is passed through.
    if not spec:
        return spec
    s = str(spec)
    if "/" in s or "." in s:
        return s
    return "/Script/AIModule." + s
def _to_array_json(v):
    # ARRAY-FORM value_json (the Windows fix): wrap one already-typed value in a single-element JSON array.
    return json.dumps([v])
def _coerce_val(s):
    # a tool-supplied value string -> a typed JSON value where possible (777.0 / true / [1,2]), else raw string
    # (enum name / ExportText struct "(...)" / object path). None stays None.
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        return s
def _pkg(path):
    seg = path.split("/")[-1]
    if "." in seg:
        return path.rsplit(".", 1)[0]
    return path
def _save(path):
    _try(lambda: unreal.EditorAssetLibrary.save_asset(_pkg(path), only_if_is_dirty=False))
def _read_config(obj):
    m = _refl()
    fn = getattr(m, "get_env_query_config_json", None) if m is not None else None
    if fn is None:
        return None
    return _try(lambda: json.loads(fn(obj)), None)
def _resolve_option_index(cfg, option):
    opts = (cfg or {}).get("options") or []
    if option is None:
        return 0 if opts else None
    try:
        return int(option)
    except Exception:
        pass
    for o in opts:
        if str(o.get("name")) == str(option):
            return o.get("index")
    return None
def _find_option(cfg, opt_idx):
    for o in ((cfg or {}).get("options") or []):
        if o.get("index") == opt_idx:
            return o
    return None
def _resolve_test_index(cfg, opt_idx, test):
    o = _find_option(cfg, opt_idx)
    tests = (o or {}).get("tests") or []
    if test is None:
        return 0 if tests else None
    try:
        return int(test)
    except Exception:
        pass
    want = str(test).split(".")[-1].split(":")[-1]
    for j, t in enumerate(tests):
        if str(t.get("class")) == want:
            return j
    return None
def _apply_props(obj, locator, props):
    # props: dict prop_name -> already-typed value. Best-effort per-prop; returns a result list.
    m = _refl()
    setter = getattr(m, "set_env_query_node_property", None) if m is not None else None
    results = []
    if setter is None or not props:
        return results
    for pn in props:
        vj = _to_array_json(props[pn])
        r = _try(lambda pn=pn, vj=vj: json.loads(setter(obj, locator, pn, vj)), {"error": "call failed"})
        ok = bool(isinstance(r, dict) and r.get("set"))
        results.append({"prop": pn, "ok": ok,
                        "error": (r.get("error") if (isinstance(r, dict) and not ok) else None)})
    return results
'''

    # ------------------------------------------------------------------ #
    # create_env_query — non-interactive EMPTY EnvQuery asset creation     #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _EQS_W_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("path") or "/Game/MCP_Scratch").rstrip("/")
generator_seed = PARAMS.get("generator")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # Auto-select the EnvQueryFactory (factory=None): it is not Python-exported but IS registered and
    # has no configure dialog -> non-interactive, no modal. Do NOT wrap in a ScopedEditorTransaction
    # (would trap the new asset in the undo buffer and block a later delete); the create_asset op
    # inverse (close editors + delete_asset [+ rmdir]) reverts it.
    eq = _try(lambda: at.create_asset(name, package_path, unreal.EnvQuery, None))
    if eq is None or not isinstance(eq, unreal.EnvQuery):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(eq).__name__, asset_path)}))
    else:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if aes is not None:
            try: aes.close_all_editors_for_asset(eq)
            except Exception: pass
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)  # persist + reliable undo-delete
        except Exception: pass
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        res = {"status": "success", "name": eq.get_name(), "asset_path": asset_path,
               "object_path": eq.get_path_name(), "class": eq.get_class().get_name(),
               "option_count": 0, "created_dir": created_dir, "ledger_depth": len(_ledger())}
        if generator_seed:
            res["generator_seed_ignored"] = (
                "The query was created EMPTY. Seeding a first option/generator (%s) is NOT possible "
                "from Python in this build: UEnvQuery.Options and UEnvQueryOption.Generator are protected "
                "C++ UPROPERTYs with no write path (see add_eqs_option). Add the generator in the EQS "
                "editor, or via a future C++ EQS-write handler." % generator_seed)
        res["note"] = ("Created a valid but EMPTY EnvQuery (no options). Option/test/node-property "
                       "authoring is editor-only in this build (protected properties). Inspect with "
                       "eqs.get_env_query_info / get_env_query_config; check with validate_env_query.")
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def create_env_query(ctx, name: str, path: str = "/Game/MCP_Scratch",
                         generator: str = None) -> str:
        """Create a new EnvQuery (EQS) asset non-interactively (NO modal / factory dialog). Ledgered write.

        name:      asset name for the new EnvQuery (e.g. 'EQ_FindCover').
        path:      content directory (default '/Game/MCP_Scratch'); must be under a valid mounted root.
                   Intermediate folders are created as needed.
        generator: optional first-generator class name. ACCEPTED for spec parity but NOT honored in this
                   build -- the query is always created EMPTY (see note below); pass it only to have the
                   response echo why seeding is unavailable.

        Uses AssetTools.create_asset(name, path, unreal.EnvQuery, None), which auto-selects the (non-
        Python-exported, dialog-free) EnvQueryFactory -> no modal. The query is created EMPTY: adding
        options/generators/tests is editor-only in this build because UEnvQuery.Options and
        UEnvQueryOption.Generator/Tests are PROTECTED C++ UPROPERTYs with no Python/C++ write path
        (verified live). Inspect with eqs.get_env_query_info; check with validate_env_query.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close editors
        + delete asset [+ rmdir if we created the dir]."""
        params = {"name": name, "path": path, "generator": generator}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_env_query — integrity pass/warn/fail (read-only)            #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _EQS_W_HELPERS + r'''
path = PARAMS.get("env_query")
obj, err = _load_eq(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    m = _refl()
    fn = getattr(m, "get_env_query_config_json", None) if m is not None else None
    if fn is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.get_env_query_config_json unavailable -- reload the MCP server after the C++ config-reader build. validate_env_query needs it to read the protected option/generator/test structure."}))
    else:
        cfg = _try(lambda: json.loads(fn(obj)), {})
        issues = []  # each: {level: fail|warn, where, message}
        opts = cfg.get("options") or []
        opt_count = cfg.get("option_count")
        if opt_count is None:
            opt_count = len(opts)
        if opt_count == 0:
            issues.append({"level": "fail", "where": "query",
                           "message": "query has NO options; it can never generate or return items"})
        for o in opts:
            oi = o.get("index")
            onm = o.get("name") or ("option[%s]" % oi)
            gen = o.get("generator") or {}
            gcls = gen.get("class")
            if not gcls:
                issues.append({"level": "fail", "where": onm,
                               "message": "option has no generator (it produces no items)"})
            else:
                if not gen.get("item_type"):
                    issues.append({"level": "warn", "where": onm,
                                   "message": "generator %s has no resolved item_type" % gcls})
            tests = o.get("tests") or []
            if not tests:
                issues.append({"level": "warn", "where": onm,
                               "message": "option has no tests; items are returned unscored/unfiltered"})
        fails = [i for i in issues if i["level"] == "fail"]
        warns = [i for i in issues if i["level"] == "warn"]
        result_kind = "fail" if fails else ("warn" if warns else "pass")
        out = {"status": "success", "env_query": obj.get_path_name(),
               "query_name": cfg.get("query_name"),
               "result": result_kind, "option_count": opt_count,
               "fail_count": len(fails), "warn_count": len(warns), "issues": issues,
               "note": ("Read-only integrity pass over the C++ config reader (get_env_query_config_json, "
                        "which reads the protected Options/Generator/Tests). 'fail' = the query is "
                        "structurally non-functional (no options / an option with no generator); 'warn' = "
                        "functional but suspect (option with no tests -> unscored; unresolved item_type). "
                        "This does not execute the query (see run_env_query).")}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def validate_env_query(ctx, env_query: str) -> str:
        """Integrity-check an EnvQuery asset: pass / warn / fail. Read-only.

        env_query: EnvQuery asset path, e.g.
                   '/Game/Variant_Combat/Blueprints/AI/EnvQuery_Evade.EnvQuery_Evade'.

        Reads the query's real structure via the C++ config reader (unlocks the protected
        Options/Generator/Tests) and reports issues:
          - FAIL: query has no options; or an option has no generator (non-functional).
          - WARN: an option has no tests (items returned unscored); a generator has no resolved item type.
        Returns {result, option_count, fail_count, warn_count, issues:[{level, where, message}]}.
        Does NOT execute the query (that is run_env_query, deferred in this build)."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"env_query": env_query}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # EQS authoring WRITES — wired to the C++ #15 MCPReflectionLibrary     #
    # handlers (live). Each is hasattr-guarded: an un-rebuilt DLL returns  #
    # a clear "handler unavailable" error instead of faking a mutation.    #
    # ------------------------------------------------------------------ #

    _ADD_OPTION_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; generator = PARAMS.get("generator"); properties = PARAMS.get("properties")
obj, err = _load_eq(env_query)
m = _refl()
adder = getattr(m, "add_env_query_option", None) if m is not None else None
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif adder is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "add_eqs_option",
        "message": "C++ handler add_env_query_option unavailable (plugin DLL predates C++ #15; recompile needed)"}))
elif not generator:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "generator class is required"}))
else:
    gen_path = _norm_class(generator)
    res = _try(lambda: json.loads(adder(obj, gen_path)), {"error": "call failed"})
    if not isinstance(res, dict) or res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "generator": gen_path,
            "message": (res.get("error") if isinstance(res, dict) else "add_env_query_option failed")}))
    else:
        oi = int(res.get("option_index"))
        asset_path = obj.get_path_name()
        _ledger().append({"op": "eqs_add_option", "asset_path": asset_path, "option_index": oi})
        prop_results = []
        if properties:
            pd = _try(lambda: json.loads(properties) if isinstance(properties, str) else properties, None)
            if isinstance(pd, dict):
                prop_results = _apply_props(obj, "option:%d/generator" % oi, pd)
        _save(asset_path)
        print("@@UMCP@@" + json.dumps({"status": "success", "env_query": asset_path,
            "option_index": oi, "generator_class": res.get("generator_class"),
            "option_name": res.get("option_name"), "option_count": res.get("option_count"),
            "properties_applied": prop_results, "ledger_depth": len(_ledger()),
            "note": ("Option+generator added via C++ AddEnvQueryOption. Ledger op eqs_add_option -> inverse "
                     "remove_env_query_option(option_index). Generator properties set here are reverted by that "
                     "same option removal (no separate ledger entries needed).")}))
'''

    @mcp.tool()
    def add_eqs_option(ctx, env_query: str, generator: str, properties: str = None) -> str:
        """Add a generator option to an EnvQuery (C++ #15 writer). Ledgered write.

        env_query:  EnvQuery asset path.
        generator:  generator class -- bare name ('EnvQueryGenerator_ActorsOfClass') or full
                    '/Script/AIModule.<Class>' path (bare names are normalized to /Script/AIModule.).
        properties: optional JSON object {PropName: value, ...} applied to the NEW generator node after
                    creation (values follow set_eqs_node_property's forms: scalars/enums/ExportText structs).

        Calls MCPReflectionLibrary.add_env_query_option, then Python saves the asset. Ledgered NEW op
        'eqs_add_option' {asset_path, option_index}; inverse = remove_env_query_option(option_index)."""
        params = {"env_query": env_query, "generator": generator, "properties": properties}
        try:
            return json.dumps(_exec(_ADD_OPTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_TEST_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; test_class = PARAMS.get("test_class"); option = PARAMS.get("option")
purpose = PARAMS.get("purpose"); filter_type = PARAMS.get("filter_type")
scoring_equation = PARAMS.get("scoring_equation"); scoring_factor = PARAMS.get("scoring_factor")
properties = PARAMS.get("properties")
obj, err = _load_eq(env_query)
m = _refl()
adder = getattr(m, "add_env_query_test", None) if m is not None else None
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif adder is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "add_eqs_test",
        "message": "C++ handler add_env_query_test unavailable (plugin DLL predates C++ #15; recompile needed)"}))
elif not test_class:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "test_class is required"}))
else:
    cfg = _read_config(obj)
    opt_idx = _resolve_option_index(cfg, option)
    if opt_idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve option '%s' (query has no options?)" % option}))
    else:
        oi = int(opt_idx); test_path = _norm_class(test_class)
        res = _try(lambda: json.loads(adder(obj, oi, test_path)), {"error": "call failed"})
        if not isinstance(res, dict) or res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "test_class": test_path, "option_index": oi,
                "message": (res.get("error") if isinstance(res, dict) else "add_env_query_test failed")}))
        else:
            ti = int(res.get("test_index"))
            asset_path = obj.get_path_name()
            _ledger().append({"op": "eqs_add_test", "asset_path": asset_path, "option_index": oi, "test_index": ti})
            props = {}
            if purpose: props["TestPurpose"] = purpose
            if filter_type: props["FilterType"] = filter_type
            if scoring_equation: props["ScoringEquation"] = scoring_equation
            if scoring_factor is not None:
                props["ScoringFactor"] = "(DefaultValue=%f)" % float(scoring_factor)
            if properties:
                pd = _try(lambda: json.loads(properties) if isinstance(properties, str) else properties, None)
                if isinstance(pd, dict):
                    props.update(pd)
            prop_results = _apply_props(obj, "option:%d/test:%d" % (oi, ti), props)
            _save(asset_path)
            print("@@UMCP@@" + json.dumps({"status": "success", "env_query": asset_path,
                "option_index": oi, "test_index": ti, "test_class": res.get("test_class"),
                "test_count": res.get("test_count"), "properties_applied": prop_results,
                "ledger_depth": len(_ledger()),
                "note": ("Test added via C++ AddEnvQueryTest. Ledger op eqs_add_test -> inverse "
                         "remove_env_query_test(option_index, test_index). Friendly params "
                         "(purpose/filter_type/scoring_equation/scoring_factor) + properties are applied via "
                         "set_env_query_node_property on the new test node and reverted by the test removal.")}))
'''

    @mcp.tool()
    def add_eqs_test(ctx, env_query: str, test_class: str, option: str = None,
                    purpose: str = None, filter_type: str = None, scoring_equation: str = None,
                    scoring_factor: str = None, index: int = None, properties: str = None) -> str:
        """Add a scoring/filter test to an option (C++ #15 writer). Ledgered write.

        env_query:  EnvQuery asset path.
        test_class: test class -- bare name ('EnvQueryTest_Distance') or full '/Script/AIModule.<Class>'.
        option:     option index (int) or option name; defaults to 0 (the first option).
        purpose:    optional TestPurpose enum ('Filter' | 'Score' | 'FilterAndScore').
        filter_type/scoring_equation: optional enum values set on the test node.
        scoring_factor: optional float -> ScoringFactor (DefaultValue=...).
        properties: optional JSON object of additional {PropName: value} for the test node.

        Calls MCPReflectionLibrary.add_env_query_test, applies any config, then Python saves. Ledgered NEW
        op 'eqs_add_test' {asset_path, option_index, test_index}; inverse = remove_env_query_test."""
        params = {"env_query": env_query, "test_class": test_class, "option": option,
                  "purpose": purpose, "filter_type": filter_type, "scoring_equation": scoring_equation,
                  "scoring_factor": scoring_factor, "properties": properties}
        try:
            return json.dumps(_exec(_ADD_TEST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SET_PROP_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; option = PARAMS.get("option"); test = PARAMS.get("test")
prop = PARAMS.get("property"); value = PARAMS.get("value"); properties = PARAMS.get("properties")
obj, err = _load_eq(env_query)
m = _refl()
setter = getattr(m, "set_env_query_node_property", None) if m is not None else None
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif setter is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "set_eqs_node_property",
        "message": "C++ handler set_env_query_node_property unavailable (plugin DLL predates C++ #15; recompile needed)"}))
else:
    cfg = _read_config(obj)
    opt_idx = _resolve_option_index(cfg, option)
    if opt_idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve option '%s'" % option}))
    else:
        oi = int(opt_idx); locator = None
        if test is not None:
            ti = _resolve_test_index(cfg, oi, test)
            if ti is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve test '%s' on option %d" % (test, oi)}))
            else:
                locator = "option:%d/test:%d" % (oi, int(ti))
        else:
            locator = "option:%d/generator" % oi
        if locator is not None:
            to_set = {}
            if properties:
                pd = _try(lambda: json.loads(properties) if isinstance(properties, str) else properties, None)
                if isinstance(pd, dict):
                    to_set.update(pd)
            if prop is not None:
                to_set[prop] = _coerce_val(value)
            if not to_set:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "provide property+value or a properties object"}))
            else:
                asset_path = obj.get_path_name(); applied = []
                for pn in to_set:
                    vj = _to_array_json(to_set[pn])
                    r = _try(lambda pn=pn, vj=vj: json.loads(setter(obj, locator, pn, vj)), {"error": "call failed"})
                    if isinstance(r, dict) and r.get("set"):
                        _ledger().append({"op": "eqs_set_node_property", "asset_path": asset_path,
                            "node_locator": locator, "prop_name": pn, "prev": r.get("prev")})
                        applied.append({"prop": pn, "ok": True, "prev": r.get("prev")})
                    else:
                        applied.append({"prop": pn, "ok": False,
                            "error": (r.get("error") if isinstance(r, dict) else "set failed")})
                _save(asset_path)
                print("@@UMCP@@" + json.dumps({"status": "success", "env_query": asset_path,
                    "node": locator, "applied": applied, "ledger_depth": len(_ledger()),
                    "note": ("Set via C++ SetEnvQueryNodeProperty (ARRAY-FORM value_json). Each successful set "
                             "pushes ledger op eqs_set_node_property {node_locator, prop_name, prev}; inverse "
                             "re-sets prev (re-wrapped array-form). Struct/array values must be UE ExportText "
                             "strings, e.g. value='(DefaultValue=77.0)'.")}))
'''

    @mcp.tool()
    def set_eqs_node_property(ctx, env_query: str, option: str = None, test: str = None,
                             property: str = None, value: str = None, properties: str = None,
                             index: int = None) -> str:
        """Modify a generator/test config property on an EQS node (C++ #15 writer). Ledgered write.

        env_query: EnvQuery asset path.
        option:    option index (int) or name; defaults to 0.
        test:      test index (int) or class name -> targets 'option:<i>/test:<j>'. Omit to target the
                   option's generator ('option:<i>/generator', where most config lives, e.g. InnerRadius).
        property/value: a single property name and its value. value is coerced: '777.0'/'true' become typed
                   scalars; an enum name ('Linear') or a UE ExportText struct ('(DefaultValue=77.0)') or an
                   object path ('/Script/AIModule.EnvQueryContext_Querier') is sent as-is. Internally each
                   value is wrapped ARRAY-FORM as the C++ handler requires.
        properties: OR a JSON object {PropName: value, ...} to set several at once.

        Calls MCPReflectionLibrary.set_env_query_node_property per property and Python saves. Each success
        pushes NEW op 'eqs_set_node_property' {asset_path, node_locator, prop_name, prev}; inverse re-sets
        the captured prev value (array-form)."""
        params = {"env_query": env_query, "option": option, "test": test,
                  "property": property, "value": value, "properties": properties}
        try:
            return json.dumps(_exec(_SET_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_OPTION_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; option = PARAMS.get("option")
obj, err = _load_eq(env_query)
m = _refl()
remover = getattr(m, "remove_env_query_option", None) if m is not None else None
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif remover is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "remove_eqs_option",
        "message": "C++ handler remove_env_query_option unavailable (plugin DLL predates C++ #15; recompile needed)"}))
else:
    cfg = _read_config(obj)
    opt_idx = _resolve_option_index(cfg, option)
    if opt_idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve option '%s'" % option}))
    else:
        oi = int(opt_idx)
        cap = _find_option(cfg, oi) or {}
        gen = cap.get("generator") or {}
        gen_class = gen.get("class"); gen_config = gen.get("config") or {}
        tests_cap = []
        for t in (cap.get("tests") or []):
            tests_cap.append({"test_class": t.get("class"), "config": t.get("config") or {}})
        res = _try(lambda: json.loads(remover(obj, oi)), {"error": "call failed"})
        if not isinstance(res, dict) or res.get("error") or not res.get("removed"):
            print("@@UMCP@@" + json.dumps({"status": "error", "option_index": oi,
                "message": (res.get("error") if isinstance(res, dict) else "remove_env_query_option failed")}))
        else:
            asset_path = obj.get_path_name()
            _ledger().append({"op": "eqs_readd_option", "asset_path": asset_path,
                "generator_class": gen_class, "generator_config": gen_config, "tests": tests_cap})
            _save(asset_path)
            print("@@UMCP@@" + json.dumps({"status": "success", "env_query": asset_path,
                "removed_option_index": oi, "option_count": res.get("option_count"),
                "captured_generator": gen_class, "captured_test_count": len(tests_cap),
                "ledger_depth": len(_ledger()),
                "note": ("Option removed via C++ RemoveEnvQueryOption. Ledger op eqs_readd_option captured the "
                         "generator class + config + all tests BEFORE removal for a BEST-EFFORT replay inverse "
                         "(re-appends the option at the array END, replays config per-prop best-effort -- NOT a "
                         "positionally-faithful restore). See cpp1516_wire_undo_ops.md.")}))
'''

    @mcp.tool()
    def remove_eqs_option(ctx, env_query: str, option: str) -> str:
        """Delete an option (and its tests) from an EnvQuery (C++ #15 writer). Ledgered write (best-effort undo).

        env_query: EnvQuery asset path.
        option:    option index (int) or option name to remove.

        Captures the option's full config (generator class + node props + every test class + config) via the
        C++ reader, then calls MCPReflectionLibrary.remove_env_query_option and Python saves. Ledgered NEW op
        'eqs_readd_option' -- the inverse REPLAYS the captured config (add option + set props + add tests),
        which is BEST-EFFORT: the option is re-appended at the array end (not its original slot) and node
        props are replayed per-prop best-effort. Fully faithful positional restore is impossible with the
        append-only C++ API."""
        params = {"env_query": env_query, "option": option}
        try:
            return json.dumps(_exec(_REMOVE_OPTION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_TEST_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; test = PARAMS.get("test"); option = PARAMS.get("option")
obj, err = _load_eq(env_query)
m = _refl()
remover = getattr(m, "remove_env_query_test", None) if m is not None else None
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif remover is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "remove_eqs_test",
        "message": "C++ handler remove_env_query_test unavailable (plugin DLL predates C++ #15; recompile needed)"}))
else:
    cfg = _read_config(obj)
    opt_idx = _resolve_option_index(cfg, option)
    if opt_idx is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve option '%s'" % option}))
    else:
        oi = int(opt_idx)
        ti = _resolve_test_index(cfg, oi, test)
        if ti is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not resolve test '%s' on option %d" % (test, oi)}))
        else:
            tj = int(ti)
            o = _find_option(cfg, oi) or {}
            tcap = ((o.get("tests") or [])[tj]) if tj < len(o.get("tests") or []) else {}
            test_class = tcap.get("class"); test_config = tcap.get("config") or {}
            res = _try(lambda: json.loads(remover(obj, oi, tj)), {"error": "call failed"})
            if not isinstance(res, dict) or res.get("error") or not res.get("removed"):
                print("@@UMCP@@" + json.dumps({"status": "error", "option_index": oi, "test_index": tj,
                    "message": (res.get("error") if isinstance(res, dict) else "remove_env_query_test failed")}))
            else:
                asset_path = obj.get_path_name()
                _ledger().append({"op": "eqs_readd_test", "asset_path": asset_path, "option_index": oi,
                    "test_class": test_class, "config": test_config})
                _save(asset_path)
                print("@@UMCP@@" + json.dumps({"status": "success", "env_query": asset_path,
                    "option_index": oi, "removed_test_index": tj, "test_count": res.get("test_count"),
                    "captured_test_class": test_class, "ledger_depth": len(_ledger()),
                    "note": ("Test removed via C++ RemoveEnvQueryTest. Ledger op eqs_readd_test captured the "
                             "test class + config BEFORE removal for a BEST-EFFORT replay inverse (re-appends "
                             "the test at the option's tests END, replays config per-prop best-effort).")}))
'''

    @mcp.tool()
    def remove_eqs_test(ctx, env_query: str, test: str, option: str = None) -> str:
        """Remove a test from an option (C++ #15 writer). Ledgered write (best-effort undo).

        env_query: EnvQuery asset path.
        test:      test index (int) or test class name to remove.
        option:    option index (int) or name; defaults to 0.

        Captures the test's class + config via the C++ reader, then calls
        MCPReflectionLibrary.remove_env_query_test and Python saves. Ledgered NEW op 'eqs_readd_test' -- the
        inverse REPLAYS the captured test (add test + set props), BEST-EFFORT: re-appended at the option's
        tests end (not its original slot); config replayed per-prop best-effort."""
        params = {"env_query": env_query, "test": test, "option": option}
        try:
            return json.dumps(_exec(_REMOVE_TEST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # run_env_query — PIE-gated runtime execution (C++ #25 RunEnvQueryJson) #
    # Needs a LIVE (PIE/game) world; the editor world does not tick EQS.   #
    # Read-only (no ledger). hasattr-guarded on an un-rebuilt DLL.         #
    # ------------------------------------------------------------------ #
    _RUN_BODY = _EQS_W_HELPERS + r'''
env_query = PARAMS["env_query"]; querier_name = PARAMS.get("querier"); run_mode = PARAMS.get("run_mode") or "all"
max_results = PARAMS.get("max_results")
try:
    max_items = int(max_results) if max_results is not None else 0
except Exception:
    max_items = 0
ues = _try(lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem))
les = _try(lambda: unreal.get_editor_subsystem(unreal.LevelEditorSubsystem))
gw = ues.get_game_world() if (ues is not None and les is not None and les.is_in_play_in_editor()) else None
obj, err = _load_eq(env_query)
m = _refl()
runner = getattr(m, "run_env_query_json", None) if m is not None else None
if gw is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "run_env_query", "executed": False,
        "message": "not in PIE -- EQS is only processed by a running world. play_in_editor, poll get_pie_status until in_pie, then retry."}))
elif err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif runner is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "run_env_query", "executed": False,
        "message": "C++ handler run_env_query_json unavailable (plugin DLL predates C++ #25; recompile needed)"}))
else:
    acts = _try(lambda: list(unreal.GameplayStatics.get_all_actors_of_class(gw, unreal.Actor)), []) or []
    querier = None
    if querier_name:
        for a in acts:
            try:
                nm = a.get_name(); lbl = _try(lambda a=a: a.get_actor_label(), "")
                if nm == querier_name or (querier_name.lower() in nm.lower()) or lbl == querier_name:
                    querier = a; break
            except Exception:
                pass
    if querier is None and not querier_name:
        querier = _try(lambda: unreal.GameplayStatics.get_player_pawn(gw, 0), None)
        if querier is None:
            for a in acts:
                if isinstance(a, unreal.Pawn):
                    querier = a; break
        if querier is None and acts:
            querier = acts[0]
    if querier is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "feature": "run_env_query", "executed": False,
            "message": "could not resolve a querier actor (querier=%s) in the PIE world" % (querier_name or "(auto)")}))
    else:
        res = _try(lambda: json.loads(runner(obj, querier, run_mode, max_items)), {"error": "call failed"})
        if not isinstance(res, dict) or res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "feature": "run_env_query", "executed": False,
                "querier": _try(lambda: querier.get_name(), None),
                "message": (res.get("error") if isinstance(res, dict) else "run_env_query_json failed")}))
        else:
            res["executed"] = True
            res["note"] = ("Ran via C++ RunEnvQueryJson (UEnvQueryManager.RunInstantQuery) in the live PIE "
                           "world. items are scored best-first (score normalized 0..1); each item carries a "
                           "location and, for actor queries, an actor + actor_path. Read-only (no ledger).")
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def run_env_query(ctx, env_query: str, querier: str = None, run_mode: str = None,
                     max_results: int = None, params: str = None) -> str:
        """Execute an EnvQuery in the LIVE PIE world and return scored results (C++ #25 handler).

        REQUIRES PIE -- EQS is only processed by a running world. play_in_editor, poll get_pie_status until
        in_pie, then call this (the editor / non-PIE world does not tick queries).

        env_query:   EnvQuery asset path.
        querier:     querier actor name / label / substring in the PIE world (the query OWNER; contexts like
                     EnvQueryContext_Querier resolve against it). Empty = the player pawn, else the first
                     Pawn, else the first actor.
        run_mode:    'single' | 'randombest5' | 'randombest25' | 'all' (default 'all' = every scored item).
        max_results: cap on returned items (the full item_count is always reported); None/0 = all.
        params:      accepted for spec parity; named EQS query params are not wired in this build (ignored).

        Calls MCPReflectionLibrary.run_env_query_json(query, querier, run_mode, max_items) via RunInstantQuery.
        Read-only: NO mutation, NO ledger. Returns {result_status, success, finished, option_index, item_type,
        item_count, returned_count, items:[{index, score, location, actor?, actor_path?}]}."""
        exec_params = {"env_query": env_query, "querier": querier, "run_mode": run_mode,
                       "max_results": max_results}
        try:
            return json.dumps(_exec(_RUN_BODY, exec_params), indent=2)
        except Exception as e:
            return f"Error: {e}"
