"""UserTools :: Gameplay Ability System (GAS) — WRITE  (spec: docs/spec/gas.md)

Clean-room WRITE companion to gas.py (the READ module). Adds the MISSING spec authoring/validate
features that ARE Python-reachable in UE 5.8.1. Conventions (base64 PARAMS, Output-Log auto-capture,
per-session _ledger, the generic "create_blueprint"/"create_asset" undo op) are copied verbatim from
editor_level.py (the gold standard) and blueprints_write.py.

LIVE PROBE FINDINGS (UE 5.8.1, TestMCPSetup — what IS / ISN'T Python-reachable for GAS AUTHORING):
  * GAS assets are authored as BLUEPRINT SUBCLASSES. UGameplayEffect, UGameplayAbility, UAttributeSet,
    UGameplayCueNotify_Static and UGameplayCueNotify_Actor are all is_blueprintable=True. A GAS asset is
    created non-interactively via BlueprintEditorLibrary.create_blueprint_asset_with_parent(path, parent)
    (the proven no-modal path; strictly safer than AssetTools.create_asset(..,BlueprintFactory) which can
    pop a parent-picker). Reuses the ledger op "create_blueprint" (inverse already in editor_level.undo).
  * CDO editing is the authoring surface. The GAS config properties (DurationPolicy, Modifiers, GEComponents,
    InstancingPolicy, GameplayCueTag, ...) are EditDefaultsOnly: set_editor_property on a transient INSTANCE
    is REFUSED ("cannot be edited on instances"), but succeeds on the generated-class CDO
    (unreal.get_default_object(BEL.generated_class(bp))). Edits persist after compile_blueprint + save_asset
    (verified by reload). So every create_* tool edits the fresh CDO before compiling.
  * MODIFIERS are authorable. FGameplayModifierInfo / FGameplayEffectModifierMagnitude reject BOTH kwargs
    ctors and set_editor_property (EditDefaultsOnly), but their import_text()/export_text() round-trip
    faithfully (verified: attribute FieldPath, ModifierOp, ScalableFloat value all persist across reload).
    So set_gameplay_effect_modifier builds each modifier via import_text and assigns the whole array to the
    CDO. A real attribute BINDS when given the full FieldPath (ClassPath:AttrName).
  * GEComponents are authorable but SPARSE. Only 3 UGameplayEffectComponent subclasses are exposed to Python
    (GameplayEffectUIData, GameplayEffectUIData_TextOnly, ImmunityGameplayEffectComponent) — the tag/
    requirement GEComponents (Target/AssetTags*, ...) are NOT bound. Components are inline subobjects created
    via new_object(cls, outer=cdo) and appended to the ge_components array; they round-trip via export_text.
  * GameplayCueNotify tag binding works: the CDO exposes gameplay_cue_tag (FGameplayTag), set via a tag
    resolved from the live manager (GameplayTagLibrary.request_gameplay_tag) or import_text.

  * DEFERRED-BLOCKED (verified NOT reachable — refused, not faked):
      - add_attribute: a gameplay attribute is an FGameplayAttributeData UPROPERTY that GAS registers only
        via C++ accessor macros. The C++ add_blueprint_variable handler REFUSES the type ("unsupported type
        'FGameplayAttributeData'"), and a Blueprint-added struct var is not a functional gameplay attribute
        anyway (engine limitation). No Python/C++ write path -> BLOCKED. (create_attribute_set still makes a
        valid EMPTY UAttributeSet subclass.)
      *** UPDATE 2026-08-16 — rename_gameplay_tag / add_gameplay_tag_source are NO LONGER BLOCKED. *** The
      C++ #16 handlers are live: MCPReflectionLibrary.rename_gameplay_tag (RenameTagInINI, writes an INI
      redirector so existing references keep resolving) and .add_gameplay_tag_source (AddNewGameplayTagSource
      + live manager rescan). Both wired below and verified live. Only add_attribute remains BLOCKED:

Implemented (WORKING, all reversible):
  - create_gameplay_effect     (WRITE; op "create_blueprint"; optional duration_policy on the CDO)
  - create_gameplay_ability    (WRITE; op "create_blueprint"; optional instancing/net policies on the CDO)
  - create_gameplay_cue_notify (WRITE; op "create_blueprint"; static|actor; optional cue tag bound on the CDO)
  - create_attribute_set       (WRITE; op "create_blueprint"; EMPTY UAttributeSet subclass — see add_attribute)
  - set_gameplay_effect_modifier   (WRITE; NEW op "gas_set_modifiers"; import_text modifier build)
  - add_gameplay_effect_component  (WRITE; NEW op "gas_set_ge_components")
  - remove_gameplay_effect_component (WRITE; NEW op "gas_set_ge_components")
Read-side validators / listers:
  - list_gameplay_cue_notifies (READ)
  - validate_attribute_set     (READ)
  - validate_gameplay_effect   (READ)
  - validate_gameplay_cues     (READ; notifies vs their bound tags)
GAS gameplay-tag authoring (WIRED to C++ #16, verified live):
  - rename_gameplay_tag    (WRITE; C++ RenameGameplayTag; NEW op "rename_gameplay_tag"; self-inverse)
  - add_gameplay_tag_source (WRITE; C++ AddGameplayTagSource; UNLEDGERED -- no engine "remove source" API)
Honest refusals (no mutation; explain the concrete blocker):
  - add_attribute (BLOCKED)

NEW undo ops (schemas reported to .mcp_coord/coordinator-inbox/g4_gas_undo_ops.md for folding into
editor_level.undo): gas_set_modifiers, gas_set_ge_components. Both snapshot the PRIOR array as a list of
export_text strings and rebuild it on undo (import_text for modifier structs; new_object+import_text for
GEComponent subobjects), then compile_blueprint + save_asset. create_* reuse the existing create_blueprint op.
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
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64;
# any double-quote needed inside an Unreal import_text string is built from chr(34) (var Q).


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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside; Q=chr(34).
    _GASW_HELPERS = r'''
import unreal, json, builtins, gc, inspect, warnings, fnmatch
warnings.simplefilter("ignore")
Q = chr(34)
EAL = unreal.EditorAssetLibrary
BEL = unreal.BlueprintEditorLibrary
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
def _match(s, pat):
    if not pat:
        return True
    s = (s or "").lower(); p = pat.lower()
    return fnmatch.fnmatch(s, p) or (p in s)
def _aes():
    return unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
def _close(obj):
    a = _aes()
    if a is not None and obj is not None:
        try: a.close_all_editors_for_asset(obj)
        except Exception: pass
def _compile_save(bp, ap):
    ok = _try(lambda: bool(BEL.compile_blueprint(bp)))
    _close(bp)
    _try(lambda: EAL.save_asset(ap, only_if_is_dirty=False))
    return ok
def _resolve_cls(spec):
    # engine class name, /Script path, or Blueprint asset path -> UClass
    if not spec:
        return None
    c = getattr(unreal, spec, None)
    if inspect.isclass(c):
        return c
    obj = _try(lambda: EAL.load_asset(spec))
    if obj is not None:
        if isinstance(obj, unreal.Blueprint):
            return _try(lambda: BEL.generated_class(obj))
        if isinstance(obj, unreal.Class):
            return obj
    return _try(lambda: unreal.load_class(None, spec))
def _load_bp(path, base):
    # load a GAS Blueprint asset and return (bp, gcls, cdo, err). base = expected native base name.
    if not path:
        return None, None, None, "no asset path given"
    obj = _try(lambda: EAL.load_asset(path))
    if obj is None:
        return None, None, None, "asset not found or failed to load: %s" % path
    if not isinstance(obj, unreal.Blueprint):
        return None, None, None, "asset is not a Blueprint (got %s): %s" % (obj.get_class().get_name(), path)
    gcls = _try(lambda: BEL.generated_class(obj))
    if gcls is None:
        return None, None, None, "blueprint has no generated class: %s" % path
    cdo = _try(lambda: unreal.get_default_object(gcls))
    if cdo is None:
        return None, None, None, "could not resolve CDO for %s" % path
    # A Blueprint generated class is a unreal.Class INSTANCE (not a Python type), so issubclass fails;
    # validate via the CDO which IS a real instance of the generated class.
    basec = getattr(unreal, base, None)
    if basec is not None and not isinstance(cdo, basec):
        return None, None, None, "blueprint %s is not a %s subclass" % (path, base)
    return obj, gcls, cdo, None
def _create_bp(name, package_path, parent_cls):
    # non-interactive Blueprint create; returns (bp, asset_path, created_dir, err). Ledger op create_blueprint.
    package_path = (package_path or "/Game/MCP_Scratch").rstrip("/")
    ap = package_path + "/" + name
    if EAL.does_asset_exist(ap):
        return None, ap, False, "asset already exists: %s (refusing to overwrite)" % ap
    created_dir = not EAL.does_directory_exist(package_path)
    bp = None
    with unreal.ScopedEditorTransaction("MCP gas_write create"):
        bp = _try(lambda: BEL.create_blueprint_asset_with_parent(ap, parent_cls))
    if bp is None:
        if created_dir and EAL.does_directory_exist(package_path):
            _try(lambda: EAL.delete_directory(package_path))
        return None, ap, created_dir, "create_blueprint_asset_with_parent returned None for %s" % ap
    return bp, ap, created_dir, None
def _mod_op(spec):
    # friendly modifier-op name -> UE export enum name (for import_text)
    if not spec:
        return "AddBase"
    s = str(spec).strip().lower().replace(" ", "_")
    m = {"add": "AddBase", "additive": "AddBase", "add_base": "AddBase", "addbase": "AddBase",
         "add_final": "AddFinal", "addfinal": "AddFinal",
         "multiply": "MultiplyAdditive", "multiplicative": "MultiplyAdditive",
         "multiply_additive": "MultiplyAdditive", "multiplyadditive": "MultiplyAdditive",
         "multiply_compound": "MultiplyCompound", "multiplycompound": "MultiplyCompound", "compound": "MultiplyCompound",
         "divide": "DivideAdditive", "division": "DivideAdditive", "divide_additive": "DivideAdditive", "divideadditive": "DivideAdditive",
         "override": "Override"}
    return m.get(s, spec)
def _attr_fieldpath(spec):
    # accept 'ClassName:Attr' | 'ClassName.Attr' | '/Script/Mod.Class:Attr' | plain 'Attr'.
    # -> (attribute_name, fieldpath_or_None)
    if not spec:
        return None, None
    s = str(spec)
    if "/Script/" in s and ":" in s:
        return s.split(":")[-1], s
    sep = ":" if ":" in s else ("." if "." in s else None)
    if sep is None:
        return s, None  # name only (no live binding)
    cls_name, attr = s.rsplit(sep, 1)
    c = getattr(unreal, cls_name.split(".")[-1].split(":")[-1], None)
    if inspect.isclass(c):
        cp = _try(lambda: c.static_class().get_path_name()) if hasattr(c, "static_class") else None
        if cp:
            return attr, cp + ":" + attr
    return attr, None
def _mod_import_text(attr_name, fieldpath, op_name, value):
    # build an FGameplayModifierInfo import_text string (ScalableFloat magnitude)
    inner = "AttributeName=" + Q + str(attr_name or "") + Q
    if fieldpath:
        inner += ",Attribute=" + Q + str(fieldpath) + Q
    return ("(Attribute=(" + inner + "),ModifierOp=" + str(op_name) +
            ",ModifierMagnitude=(MagnitudeCalculationType=ScalableFloat,ScalableFloatMagnitude=(Value=%.6f)))" % float(value))
def _snapshot_modifiers(cdo):
    out = []
    for m in (_try(lambda: cdo.get_editor_property("modifiers"), []) or []):
        out.append(_try(lambda m=m: m.export_text()))
    return out
def _snapshot_ge_components(cdo):
    out = []
    for c in (_try(lambda: cdo.get_editor_property("ge_components"), []) or []):
        if c is None:
            continue
        out.append({"class_path": _try(lambda c=c: c.get_class().get_path_name()),
                    "text": _try(lambda c=c: c.export_text())})
    return out
'''

    # ================================================================== #
    # create_gameplay_effect                                             #
    # ================================================================== #
    _CREATE_GE_BODY = _GASW_HELPERS + r'''
name = PARAMS["name"]; package_path = PARAMS.get("path") or "/Game/MCP_Scratch"
dur = PARAMS.get("duration_policy")
bp, ap, created_dir, err = _create_bp(name, package_path, unreal.GameplayEffect)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cdo = unreal.get_default_object(BEL.generated_class(bp))
    dur_set = None
    if dur:
        dm = {"instant": "INSTANT", "infinite": "INFINITE", "has_duration": "HAS_DURATION", "duration": "HAS_DURATION"}
        mem = dm.get(str(dur).strip().lower())
        ev = getattr(unreal.GameplayEffectDurationType, mem, None) if mem else None
        if ev is not None:
            dur_set = _try(lambda: (cdo.set_editor_property("duration_policy", ev), str(dur))[1])
    compiled = _compile_save(bp, ap)
    _ledger().append({"op": "create_blueprint", "asset_path": ap, "package_path": package_path.rstrip("/"), "created_dir": created_dir})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": bp.get_name(), "asset_path": ap,
        "object_path": bp.get_path_name(), "class": "GameplayEffect(Blueprint)",
        "generated_class": _try(lambda: BEL.generated_class(bp).get_name()),
        "duration_policy": str(_try(lambda: cdo.get_editor_property("duration_policy"))),
        "duration_policy_applied": dur_set, "compiled": compiled, "created_dir": created_dir,
        "ledger_depth": len(_ledger()),
        "note": "Empty GameplayEffect Blueprint. Add modifiers with set_gameplay_effect_modifier; GEComponents with add_gameplay_effect_component. Undo (op create_blueprint) deletes the asset."}))
'''

    @mcp.tool()
    def create_gameplay_effect(ctx, name: str, path: str = "/Game/MCP_Scratch",
                               duration_policy: str = None) -> str:
        """Create a new GameplayEffect as a Blueprint subclass (non-interactive, NO modal). Ledgered write.

        name:            asset name (e.g. 'GE_Damage').
        path:            content dir (default '/Game/MCP_Scratch'); must be under a mounted root.
        duration_policy: optional 'instant' | 'has_duration' | 'infinite' — set on the effect CDO.

        Creates a UGameplayEffect-parented Blueprint via BlueprintEditorLibrary.create_blueprint_asset_
        with_parent (no parent-picker modal), edits the generated-class CDO, compiles and saves. Populate
        it with set_gameplay_effect_modifier / add_gameplay_effect_component. Ledgered op 'create_blueprint'
        {asset_path, package_path, created_dir}; inverse = close editors + delete asset [+ rmdir]."""
        params = {"name": name, "path": path, "duration_policy": duration_policy}
        try:
            return json.dumps(_exec(_CREATE_GE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_gameplay_ability                                            #
    # ================================================================== #
    _CREATE_GA_BODY = _GASW_HELPERS + r'''
name = PARAMS["name"]; package_path = PARAMS.get("path") or "/Game/MCP_Scratch"
inst = PARAMS.get("instancing_policy"); netx = PARAMS.get("net_execution_policy")
bp, ap, created_dir, err = _create_bp(name, package_path, unreal.GameplayAbility)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cdo = unreal.get_default_object(BEL.generated_class(bp))
    applied = {}
    if inst:
        im = {"non_instanced": "NON_INSTANCED", "instanced_per_actor": "INSTANCED_PER_ACTOR",
              "instanced_per_execution": "INSTANCED_PER_EXECUTION"}
        mem = im.get(str(inst).strip().lower())
        ev = getattr(unreal.GameplayAbilityInstancingPolicy, mem, None) if mem else None
        if ev is not None:
            applied["instancing_policy"] = _try(lambda: (cdo.set_editor_property("instancing_policy", ev), True)[1])
    if netx:
        nm = {"local_only": "LOCAL_ONLY", "local_predicted": "LOCAL_PREDICTED",
              "server_initiated": "SERVER_INITIATED", "server_only": "SERVER_ONLY"}
        mem = nm.get(str(netx).strip().lower())
        ev = getattr(unreal.GameplayAbilityNetExecutionPolicy, mem, None) if mem else None
        if ev is not None:
            applied["net_execution_policy"] = _try(lambda: (cdo.set_editor_property("net_execution_policy", ev), True)[1])
    compiled = _compile_save(bp, ap)
    _ledger().append({"op": "create_blueprint", "asset_path": ap, "package_path": package_path.rstrip("/"), "created_dir": created_dir})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": bp.get_name(), "asset_path": ap,
        "object_path": bp.get_path_name(), "class": "GameplayAbility(Blueprint)",
        "generated_class": _try(lambda: BEL.generated_class(bp).get_name()),
        "instancing_policy": str(_try(lambda: cdo.get_editor_property("instancing_policy"))),
        "net_execution_policy": str(_try(lambda: cdo.get_editor_property("net_execution_policy"))),
        "policies_applied": applied, "compiled": compiled, "created_dir": created_dir,
        "ledger_depth": len(_ledger()),
        "note": "Empty GameplayAbility Blueprint (ActivateAbility graph is authored in the BP editor). Undo (op create_blueprint) deletes the asset."}))
'''

    @mcp.tool()
    def create_gameplay_ability(ctx, name: str, path: str = "/Game/MCP_Scratch",
                                instancing_policy: str = None,
                                net_execution_policy: str = None) -> str:
        """Create a new GameplayAbility as a Blueprint subclass (non-interactive, NO modal). Ledgered write.

        name:                 asset name (e.g. 'GA_Fireball').
        path:                 content dir (default '/Game/MCP_Scratch').
        instancing_policy:    optional 'non_instanced' | 'instanced_per_actor' | 'instanced_per_execution'.
        net_execution_policy: optional 'local_only' | 'local_predicted' | 'server_initiated' | 'server_only'.

        Creates a UGameplayAbility-parented Blueprint, sets the requested CDO policies, compiles and saves.
        The ability's activation logic is a BP event graph (authored in the Blueprint editor). Ledgered op
        'create_blueprint' {asset_path, package_path, created_dir}; inverse = delete asset [+ rmdir]."""
        params = {"name": name, "path": path, "instancing_policy": instancing_policy,
                  "net_execution_policy": net_execution_policy}
        try:
            return json.dumps(_exec(_CREATE_GA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_gameplay_cue_notify                                         #
    # ================================================================== #
    _CREATE_CUE_BODY = _GASW_HELPERS + r'''
name = PARAMS["name"]; package_path = PARAMS.get("path") or "/Game/MCP_Scratch"
notify_type = (PARAMS.get("notify_type") or "static").strip().lower()
cue_tag = PARAMS.get("cue_tag")
base_name = "GameplayCueNotify_Actor" if notify_type in ("actor", "burst", "looping") else "GameplayCueNotify_Static"
parent = getattr(unreal, base_name, None)
if parent is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "class %s unavailable" % base_name}))
else:
    bp, ap, created_dir, err = _create_bp(name, package_path, parent)
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        cdo = unreal.get_default_object(BEL.generated_class(bp))
        tag_applied = None; tag_warn = None
        if cue_tag:
            t = _try(lambda: unreal.GameplayTagLibrary.request_gameplay_tag(unreal.Name(cue_tag)))
            valid = _try(lambda: unreal.GameplayTagLibrary.is_gameplay_tag_valid(t)) if t is not None else False
            if t is not None and valid:
                tag_applied = _try(lambda: (cdo.set_editor_property("gameplay_cue_tag", t), cue_tag)[1])
            else:
                # fall back to import_text (still requires the tag to be registered to resolve at runtime)
                gt = _try(lambda: cdo.get_editor_property("gameplay_cue_tag"))
                if gt is not None:
                    ok = _try(lambda: gt.import_text("(TagName=" + Q + str(cue_tag) + Q + ")"))
                    if ok:
                        _try(lambda: cdo.set_editor_property("gameplay_cue_tag", gt))
                        tag_applied = cue_tag
                tag_warn = ("cue tag '%s' is not registered with the live GameplayTagsManager; bound by name "
                            "but it will not resolve until the tag exists (add it with add_gameplay_tag)." % cue_tag)
        compiled = _compile_save(bp, ap)
        _ledger().append({"op": "create_blueprint", "asset_path": ap, "package_path": package_path.rstrip("/"), "created_dir": created_dir})
        res = {"status": "success", "name": bp.get_name(), "asset_path": ap, "object_path": bp.get_path_name(),
               "class": base_name + "(Blueprint)", "notify_type": notify_type,
               "generated_class": _try(lambda: BEL.generated_class(bp).get_name()),
               "gameplay_cue_tag": str(_try(lambda: unreal.GameplayTagLibrary.get_tag_name(cdo.get_editor_property("gameplay_cue_tag")))),
               "cue_tag_applied": tag_applied, "compiled": compiled, "created_dir": created_dir,
               "ledger_depth": len(_ledger()),
               "note": "GameplayCueNotify Blueprint. Undo (op create_blueprint) deletes the asset."}
        if tag_warn:
            res["cue_tag_warning"] = tag_warn
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def create_gameplay_cue_notify(ctx, name: str, path: str = "/Game/MCP_Scratch",
                                   notify_type: str = "static", cue_tag: str = None) -> str:
        """Create a GameplayCueNotify Blueprint bound to a GameplayCue tag (non-interactive). Ledgered write.

        name:        asset name (e.g. 'GCN_Fire_Impact').
        path:        content dir (default '/Game/MCP_Scratch').
        notify_type: 'static' (UGameplayCueNotify_Static — burst/one-shot, no spawned actor) or
                     'actor' (UGameplayCueNotify_Actor — spawns an actor, supports looping). Default 'static'.
        cue_tag:     optional GameplayCue.* tag to bind (e.g. 'GameplayCue.Fire.Impact'). Bound on the CDO;
                     the tag should already be registered (add_gameplay_tag) or it is bound by-name with a
                     warning and will not resolve at runtime until it exists.

        Creates the correct GameplayCueNotify parent Blueprint, binds the cue tag, compiles and saves.
        Ledgered op 'create_blueprint' {asset_path, package_path, created_dir}; inverse = delete asset."""
        params = {"name": name, "path": path, "notify_type": notify_type, "cue_tag": cue_tag}
        try:
            return json.dumps(_exec(_CREATE_CUE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # create_attribute_set (EMPTY subclass — add_attribute is BLOCKED)   #
    # ================================================================== #
    _CREATE_AS_BODY = _GASW_HELPERS + r'''
name = PARAMS["name"]; package_path = PARAMS.get("path") or "/Game/MCP_Scratch"
bp, ap, created_dir, err = _create_bp(name, package_path, unreal.AttributeSet)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    compiled = _compile_save(bp, ap)
    _ledger().append({"op": "create_blueprint", "asset_path": ap, "package_path": package_path.rstrip("/"), "created_dir": created_dir})
    print("@@UMCP@@" + json.dumps({"status": "success", "name": bp.get_name(), "asset_path": ap,
        "object_path": bp.get_path_name(), "class": "AttributeSet(Blueprint)",
        "generated_class": _try(lambda: BEL.generated_class(bp).get_name()),
        "compiled": compiled, "created_dir": created_dir, "ledger_depth": len(_ledger()),
        "attribute_count": 0,
        "note": ("Created a valid but EMPTY UAttributeSet subclass. Attributes CANNOT be added from Python "
                 "(add_attribute is BLOCKED): a functional gameplay attribute is an FGameplayAttributeData "
                 "UPROPERTY that GAS registers via C++ accessor macros; the C++ add_blueprint_variable handler "
                 "refuses the FGameplayAttributeData type and a BP-added struct var is not a functional "
                 "attribute. Author attribute sets in C++. Undo (op create_blueprint) deletes the asset.")}))
'''

    @mcp.tool()
    def create_attribute_set(ctx, name: str, path: str = "/Game/MCP_Scratch",
                             attributes: list = None) -> str:
        """Create an AttributeSet as a Blueprint subclass (EMPTY — see note). Ledgered write.

        name:       asset name (e.g. 'BP_CombatAttributes').
        path:       content dir (default '/Game/MCP_Scratch').
        attributes: ACCEPTED for spec parity but NOT honored — attributes cannot be added from Python
                    (see add_attribute; a functional gameplay attribute needs a C++ FGameplayAttributeData
                    UPROPERTY + accessor macros). The set is always created EMPTY.

        Creates a UAttributeSet-parented Blueprint (valid but non-functional without C++ attributes),
        compiles and saves. Ledgered op 'create_blueprint' {asset_path, package_path, created_dir};
        inverse = delete asset [+ rmdir]."""
        params = {"name": name, "path": path}
        try:
            res = _exec(_CREATE_AS_BODY, params)
            if attributes and isinstance(res, dict) and res.get("status") == "success":
                res["attributes_ignored"] = ("add_attribute is BLOCKED in this build; the set was created "
                                             "EMPTY. See the tool note and add_attribute.")
            return json.dumps(res, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_gameplay_effect_modifier — NEW op gas_set_modifiers            #
    # ================================================================== #
    _SET_MOD_BODY = _GASW_HELPERS + r'''
path = PARAMS["effect"]; idx = PARAMS.get("index")
attr = PARAMS.get("attribute"); op = PARAMS.get("modifier_op"); mag = PARAMS.get("magnitude")
bp, gcls, cdo, err = _load_bp(path, "GameplayEffect")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif attr is None or mag is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "attribute and magnitude are required"}))
else:
    prior = _snapshot_modifiers(cdo)
    attr_name, fieldpath = _attr_fieldpath(attr)
    op_name = _mod_op(op)
    txt = _mod_import_text(attr_name, fieldpath, op_name, mag)
    mod = unreal.GameplayModifierInfo()
    ok = _try(lambda: mod.import_text(txt))
    cur = list(_try(lambda: cdo.get_editor_property("modifiers"), []) or [])
    action = None; used_index = None
    if idx is None or int(idx) >= len(cur):
        cur.append(mod); action = "appended"; used_index = len(cur) - 1
    elif int(idx) < 0:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "index must be >= 0"})); cur = None
    else:
        used_index = int(idx); cur[used_index] = mod; action = "replaced"
    if cur is not None:
        with unreal.ScopedEditorTransaction("MCP set_gameplay_effect_modifier"):
            cdo.set_editor_property("modifiers", cur)
        compiled = _compile_save(bp, path)
        _ledger().append({"op": "gas_set_modifiers", "asset_path": path, "prior": prior})
        readback = []
        for m in (_try(lambda: cdo.get_editor_property("modifiers"), []) or []):
            fa = _try(lambda m=m: m.get_editor_property("attribute"))
            readback.append({"attribute": str(_try(lambda fa=fa: fa.get_editor_property("attribute_name"))),
                             "modifier_op": str(_try(lambda m=m: m.get_editor_property("modifier_op")))})
        print("@@UMCP@@" + json.dumps({"status": "success", "effect": path, "action": action,
            "index": used_index, "import_ok": bool(ok), "attribute": attr_name,
            "attribute_bound": fieldpath is not None, "modifier_op": op_name, "magnitude": float(mag),
            "modifier_count": len(readback), "modifiers": readback, "compiled": compiled,
            "ledger_depth": len(_ledger()),
            "note": ("Modifier built via import_text on the effect CDO. attribute_bound=false means only the "
                     "display AttributeName was set (pass 'AttrSetClass:Attr' or a /Script FieldPath to bind "
                     "the real attribute). Undo op gas_set_modifiers restores the prior modifiers array.")}))
'''

    @mcp.tool()
    def set_gameplay_effect_modifier(ctx, effect: str, attribute: str, magnitude: float,
                                     modifier_op: str = "add", index: int = None) -> str:
        """Set/append an attribute modifier on a GameplayEffect (CDO edit). Ledgered write.

        effect:      GameplayEffect Blueprint asset path.
        attribute:   the attribute to modify. For a REAL binding pass 'AttrSetClass:Attr' or
                     'AttrSetClass.Attr' (e.g. 'AbilitySystemTestAttributeSet:Health') or a full
                     '/Script/Module.Class:Attr' FieldPath. A bare name sets display only (unbound).
        magnitude:   the ScalableFloat base value applied by the modifier.
        modifier_op: 'add' (AddBase) | 'add_final' | 'multiply' (MultiplyAdditive) | 'multiply_compound' |
                     'divide' (DivideAdditive) | 'override'. Default 'add'.
        index:       modifier slot to replace; omit (or >= count) to append.

        Builds an FGameplayModifierInfo via import_text (the struct's fields are EditDefaultsOnly and
        reject set_editor_property; import_text is the working path) and assigns the modifiers array to the
        effect CDO, then compiles and saves. Ledgered NEW op 'gas_set_modifiers' {asset_path, prior}
        (prior = export_text of each prior modifier); inverse restores the prior array + recompiles."""
        params = {"effect": effect, "attribute": attribute, "magnitude": magnitude,
                  "modifier_op": modifier_op, "index": index}
        try:
            return json.dumps(_exec(_SET_MOD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_gameplay_effect_component — NEW op gas_set_ge_components        #
    # ================================================================== #
    _ADD_GEC_BODY = _GASW_HELPERS + r'''
path = PARAMS["effect"]; comp_spec = PARAMS["component_class"]
bp, gcls, cdo, err = _load_bp(path, "GameplayEffect")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ccls = _resolve_cls(comp_spec)
    base = getattr(unreal, "GameplayEffectComponent", None)
    if ccls is None or not (inspect.isclass(ccls) and base is not None and issubclass(ccls, base)):
        avail = []
        for n in dir(unreal):
            o = getattr(unreal, n, None)
            if inspect.isclass(o) and base is not None and issubclass(o, base) and o is not base:
                avail.append(n)
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "not a Python-exposed UGameplayEffectComponent subclass: %s" % comp_spec,
            "available_component_classes": sorted(avail)}))
    else:
        cname = _try(lambda: ccls.__name__) or _try(lambda: ccls.get_name()) or str(comp_spec).split(".")[-1].split(":")[-1]
        prior = _snapshot_ge_components(cdo)
        comp = _try(lambda: unreal.new_object(ccls, cdo, unreal.Name("MCP_GEC_" + cname)))
        if comp is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "new_object failed for %s" % comp_spec}))
        else:
            cur = list(_try(lambda: cdo.get_editor_property("ge_components"), []) or [])
            cur.append(comp)
            with unreal.ScopedEditorTransaction("MCP add_gameplay_effect_component"):
                cdo.set_editor_property("ge_components", cur)
            compiled = _compile_save(bp, path)
            _ledger().append({"op": "gas_set_ge_components", "asset_path": path, "prior": prior})
            names = [ _try(lambda c=c: c.get_class().get_name()) for c in (_try(lambda: cdo.get_editor_property("ge_components"), []) or []) ]
            print("@@UMCP@@" + json.dumps({"status": "success", "effect": path, "added": cname,
                "ge_component_count": len(names), "ge_components": names, "compiled": compiled,
                "ledger_depth": len(_ledger()),
                "note": "Inline GEComponent subobject appended to the effect CDO. Undo op gas_set_ge_components restores the prior array."}))
'''

    @mcp.tool()
    def add_gameplay_effect_component(ctx, effect: str, component_class: str) -> str:
        """Add a GameplayEffectComponent to a GameplayEffect (CDO edit). Ledgered write.

        effect:          GameplayEffect Blueprint asset path.
        component_class: a Python-exposed UGameplayEffectComponent subclass. Only three are bound to Python
                         in this build: 'GameplayEffectUIData', 'GameplayEffectUIData_TextOnly',
                         'ImmunityGameplayEffectComponent'. (The tag/requirement GEComponents are NOT exposed;
                         an unavailable class returns the available list.)

        Creates the component as an inline subobject owned by the effect CDO, appends it to ge_components,
        compiles and saves. Ledgered NEW op 'gas_set_ge_components' {asset_path, prior} (prior = class+export
        of each prior component); inverse rebuilds the prior array + recompiles."""
        params = {"effect": effect, "component_class": component_class}
        try:
            return json.dumps(_exec(_ADD_GEC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # remove_gameplay_effect_component — NEW op gas_set_ge_components     #
    # ================================================================== #
    _REMOVE_GEC_BODY = _GASW_HELPERS + r'''
path = PARAMS["effect"]; comp_spec = PARAMS.get("component_class"); idx = PARAMS.get("index")
bp, gcls, cdo, err = _load_bp(path, "GameplayEffect")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    prior = _snapshot_ge_components(cdo)
    cur = list(_try(lambda: cdo.get_editor_property("ge_components"), []) or [])
    target = None
    if idx is not None:
        if 0 <= int(idx) < len(cur):
            target = int(idx)
    elif comp_spec:
        want = str(comp_spec).split(".")[-1].split(":")[-1]
        for i, c in enumerate(cur):
            if c is not None and _try(lambda c=c: c.get_class().get_name()) == want:
                target = i; break
    if target is None:
        names = [ _try(lambda c=c: c.get_class().get_name()) for c in cur ]
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "component not found (by class '%s' / index %s)" % (comp_spec, idx),
            "ge_components": names}))
    else:
        removed = _try(lambda: cur[target].get_class().get_name())
        del cur[target]
        with unreal.ScopedEditorTransaction("MCP remove_gameplay_effect_component"):
            cdo.set_editor_property("ge_components", cur)
        compiled = _compile_save(bp, path)
        _ledger().append({"op": "gas_set_ge_components", "asset_path": path, "prior": prior})
        names = [ _try(lambda c=c: c.get_class().get_name()) for c in (_try(lambda: cdo.get_editor_property("ge_components"), []) or []) ]
        print("@@UMCP@@" + json.dumps({"status": "success", "effect": path, "removed": removed,
            "index": target, "ge_component_count": len(names), "ge_components": names, "compiled": compiled,
            "ledger_depth": len(_ledger()),
            "note": "Undo op gas_set_ge_components restores the prior array (the removed component is rebuilt from its export snapshot)."}))
'''

    @mcp.tool()
    def remove_gameplay_effect_component(ctx, effect: str, component_class: str = None,
                                         index: int = None) -> str:
        """Remove a GameplayEffectComponent from a GameplayEffect (CDO edit). Ledgered write.

        effect:          GameplayEffect Blueprint asset path.
        component_class: remove the first component of this class name (e.g. 'ImmunityGameplayEffectComponent').
        index:           OR remove by position in ge_components. Pass one of component_class / index.

        Removes the matching component from ge_components, compiles and saves. Ledgered NEW op
        'gas_set_ge_components' {asset_path, prior} — the FULL prior array is snapshotted (class + export_text
        of each component) so the inverse rebuilds it exactly (new_object + import_text), recompiling."""
        params = {"effect": effect, "component_class": component_class, "index": index}
        try:
            return json.dumps(_exec(_REMOVE_GEC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_gameplay_cue_notifies (READ)                                  #
    # ================================================================== #
    _LIST_CUES_BODY = _GASW_HELPERS + r'''
package_path = PARAMS.get("package_path") or "/Game"
flt = PARAMS.get("filter")
rows = []
bases = ("GameplayCueNotify_Static", "GameplayCueNotify_Actor")
basecs = [getattr(unreal, b, None) for b in bases]
basecs = [b for b in basecs if b is not None]
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
    if not (inspect.isclass(anc) and any(issubclass(anc, b) for b in basecs)):
        continue
    pkg = str(a.get_editor_property("package_name"))
    ap = pkg + "." + nm
    cue_tag = None
    bp = _try(lambda ap=ap: EAL.load_asset(ap))
    gcls = _try(lambda bp=bp: BEL.generated_class(bp)) if bp else None
    cdo = _try(lambda gcls=gcls: unreal.get_default_object(gcls)) if gcls else None
    if cdo is not None:
        cue_tag = str(_try(lambda: unreal.GameplayTagLibrary.get_tag_name(cdo.get_editor_property("gameplay_cue_tag"))))
    rows.append({"name": nm, "path": ap, "native_parent_class": short,
                 "gameplay_cue_tag": (None if cue_tag in ("None", "", None) else cue_tag)})
rows.sort(key=lambda r: r["name"].lower())
print("@@UMCP@@" + json.dumps({"status": "success", "package_path": package_path,
    "count": len(rows), "cue_notifies": rows,
    "note": "GameplayCueNotify Blueprint assets (Static/Actor parents) under package_path with their bound gameplay_cue_tag (null if unset)."}))
'''

    @mcp.tool()
    def list_gameplay_cue_notifies(ctx, package_path: str = "/Game", filter: str = None) -> str:
        """List GameplayCueNotify Blueprint assets and their bound cue tags. Read-only.

        package_path: content root to scan (default '/Game').
        filter:       case-insensitive substring/glob on the asset name.

        Returns each GameplayCueNotify_Static/_Actor Blueprint with its gameplay_cue_tag (null if unset)."""
        params = {"package_path": package_path, "filter": filter}
        try:
            return json.dumps(_exec(_LIST_CUES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_gameplay_effect (READ)                                    #
    # ================================================================== #
    _VAL_GE_BODY = _GASW_HELPERS + r'''
path = PARAMS["effect"]
bp, gcls, cdo, err = _load_bp(path, "GameplayEffect")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    issues = []
    dur = _try(lambda: cdo.get_editor_property("duration_policy"))
    dur_s = str(dur)
    mods = _try(lambda: cdo.get_editor_property("modifiers"), []) or []
    comps = _try(lambda: cdo.get_editor_property("ge_components"), []) or []
    period = _try(lambda: cdo.get_editor_property("period").get_editor_property("value"))
    if len(mods) == 0 and len(comps) == 0:
        issues.append({"level": "warn", "where": "effect", "message": "effect has no modifiers and no GEComponents (does nothing)"})
    for i, m in enumerate(mods):
        fa = _try(lambda m=m: m.get_editor_property("attribute"))
        an = str(_try(lambda fa=fa: fa.get_editor_property("attribute_name")))
        exp = _try(lambda fa=fa: fa.export_text()) or ""
        if an in ("", "None"):
            issues.append({"level": "fail", "where": "modifier[%d]" % i, "message": "modifier has no attribute set"})
        elif ("Attribute=" + Q) not in exp and "Attribute=/Script" not in exp:
            issues.append({"level": "warn", "where": "modifier[%d]" % i,
                           "message": "modifier attribute '%s' has display name only, no bound FieldPath (won't affect a real attribute)" % an})
    if "INSTANT" in dur_s and period and float(period) > 0:
        issues.append({"level": "warn", "where": "period", "message": "INSTANT effect with a non-zero period (period is ignored for instant effects)"})
    if ("HAS_DURATION" in dur_s):
        dm = _try(lambda: cdo.get_editor_property("duration_magnitude").get_editor_property("magnitude_calculation_type"))
        if dm is None:
            issues.append({"level": "warn", "where": "duration", "message": "HAS_DURATION effect but duration magnitude is unresolved"})
    fails = [i for i in issues if i["level"] == "fail"]; warns = [i for i in issues if i["level"] == "warn"]
    result_kind = "fail" if fails else ("warn" if warns else "pass")
    print("@@UMCP@@" + json.dumps({"status": "success", "effect": path, "result": result_kind,
        "duration_policy": dur_s, "modifier_count": len(mods), "ge_component_count": len(comps),
        "fail_count": len(fails), "warn_count": len(warns), "issues": issues,
        "note": "Static integrity pass over the effect CDO: flags empty effects, modifiers with no attribute / name-only (unbound) attributes, and duration/period mismatches."}))
'''

    @mcp.tool()
    def validate_gameplay_effect(ctx, effect: str) -> str:
        """Integrity-check a GameplayEffect: pass / warn / fail. Read-only.

        effect: GameplayEffect Blueprint asset path.

        Reads the effect CDO and flags: no modifiers and no GEComponents (does nothing); a modifier with no
        attribute (FAIL); a modifier whose attribute is display-name-only with no bound FieldPath (WARN — it
        won't affect a real attribute); INSTANT effect with a non-zero period; unresolved duration magnitude.
        Returns {result, modifier_count, ge_component_count, fail_count, warn_count, issues:[...]}."""
        try:
            return json.dumps(_exec(_VAL_GE_BODY, {"effect": effect}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_attribute_set (READ)                                      #
    # ================================================================== #
    _VAL_AS_BODY = _GASW_HELPERS + r'''
ident = PARAMS["attribute_set"]
cls = None
if "/" in ident:
    obj = _try(lambda: EAL.load_asset(ident))
    if isinstance(obj, unreal.Blueprint):
        cls = _try(lambda: BEL.generated_class(obj))
    elif isinstance(obj, unreal.Class):
        cls = obj
else:
    cls = getattr(unreal, ident, None)
base = getattr(unreal, "AttributeSet", None)
if cls is None or not (inspect.isclass(cls) and base is not None and issubclass(cls, base)):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UAttributeSet subclass: %s" % ident}))
else:
    cdo = _try(lambda: unreal.get_default_object(cls))
    mrl = getattr(unreal, "MCPReflectionLibrary", None)
    props = None
    if mrl is not None and hasattr(mrl, "get_object_property_metadata_json"):
        d = _try(lambda: json.loads(mrl.get_object_property_metadata_json(cdo, include_inherited=True)))
        props = (d or {}).get("properties") if isinstance(d, dict) else None
    attrs = []
    for p in (props or []):
        cpp = p.get("cpp_type"); owner = p.get("owner_class")
        if cpp in ("FGameplayAttributeData", "float", "double") and owner not in ("Object", "AttributeSet"):
            rec = {"name": p.get("name"), "data_type": cpp, "owner_class": owner}
            if cpp == "FGameplayAttributeData":
                v = _try(lambda nm=p.get("name"): cdo.get_editor_property(nm))
                rec["base_value"] = _try(lambda v=v: v.get_editor_property("base_value"))
            else:
                rec["default_value"] = _try(lambda nm=p.get("name"): cdo.get_editor_property(nm))
            attrs.append(rec)
    is_bp = _try(lambda: cls.get_name().endswith("_C"))
    issues = []
    if len(attrs) == 0:
        issues.append({"level": "warn", "where": "set", "message": "attribute set defines no attributes (empty set does nothing)"})
    if is_bp:
        issues.append({"level": "warn", "where": "set",
                       "message": "Blueprint AttributeSet: GAS does not register attributes declared in Blueprint; functional attribute sets must be authored in C++"})
    fails = [i for i in issues if i["level"] == "fail"]; warns = [i for i in issues if i["level"] == "warn"]
    print("@@UMCP@@" + json.dumps({"status": "success", "attribute_set": ident,
        "class_name": _try(lambda: cls.get_name()), "is_blueprint": bool(is_bp),
        "attribute_count": len(attrs), "attributes": attrs,
        "result": ("fail" if fails else ("warn" if warns else "pass")),
        "fail_count": len(fails), "warn_count": len(warns), "issues": issues,
        "reflection_available": props is not None,
        "note": "Reads the set's FGameplayAttributeData/float attributes from the CDO and flags empties + non-functional Blueprint AttributeSets."}))
'''

    @mcp.tool()
    def validate_attribute_set(ctx, attribute_set: str) -> str:
        """Integrity-check an AttributeSet: attributes, replication, warnings. Read-only.

        attribute_set: native class name (e.g. 'AbilitySystemTestAttributeSet') or a /Game Blueprint path.

        Reads the set's attribute UPROPERTYs from the CDO and reports issues: an empty set (WARN); a
        Blueprint AttributeSet (WARN — GAS does not register Blueprint-declared attributes; must be C++).
        Returns {result, attribute_count, attributes, is_blueprint, issues:[...]}."""
        try:
            return json.dumps(_exec(_VAL_AS_BODY, {"attribute_set": attribute_set}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # validate_gameplay_cues (READ)                                      #
    # ================================================================== #
    _VAL_CUES_BODY = _GASW_HELPERS + r'''
package_path = PARAMS.get("package_path") or "/Game"
rows = []; issues = []
bases = [getattr(unreal, b, None) for b in ("GameplayCueNotify_Static", "GameplayCueNotify_Actor")]
bases = [b for b in bases if b is not None]
ar = unreal.AssetRegistryHelpers.get_asset_registry()
far = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
                      recursive_paths=True, package_paths=[package_path])
seen_tags = {}
for a in (_try(lambda: ar.get_assets(far), []) or []):
    nm = str(a.get_editor_property("asset_name"))
    npc = _try(lambda a=a: str(a.get_tag_value("NativeParentClass")))
    short = None
    if npc:
        short = npc.split("'")[1] if "'" in npc else npc
        short = short.rstrip("'").split(".")[-1].split(":")[-1]
    anc = getattr(unreal, short, None) if short else None
    if not (inspect.isclass(anc) and any(issubclass(anc, b) for b in bases)):
        continue
    pkg = str(a.get_editor_property("package_name")); ap = pkg + "." + nm
    bp = _try(lambda ap=ap: EAL.load_asset(ap))
    gcls = _try(lambda bp=bp: BEL.generated_class(bp)) if bp else None
    cdo = _try(lambda gcls=gcls: unreal.get_default_object(gcls)) if gcls else None
    tag = None
    if cdo is not None:
        tag = str(_try(lambda: unreal.GameplayTagLibrary.get_tag_name(cdo.get_editor_property("gameplay_cue_tag"))))
    tag = None if tag in ("None", "", None) else tag
    rows.append({"name": nm, "path": ap, "gameplay_cue_tag": tag})
    if tag is None:
        issues.append({"level": "fail", "where": nm, "message": "cue notify has no GameplayCueTag bound (will never trigger)"})
    else:
        if not tag.startswith("GameplayCue"):
            issues.append({"level": "warn", "where": nm, "message": "cue tag '%s' is not under the GameplayCue.* root (GAS routes cues by that root)" % tag})
        seen_tags.setdefault(tag, []).append(nm)
for tag, notifies in seen_tags.items():
    if len(notifies) > 1:
        issues.append({"level": "warn", "where": tag, "message": "tag bound by multiple notifies: %s" % ", ".join(notifies)})
fails = [i for i in issues if i["level"] == "fail"]; warns = [i for i in issues if i["level"] == "warn"]
print("@@UMCP@@" + json.dumps({"status": "success", "package_path": package_path,
    "cue_notify_count": len(rows), "cue_notifies": rows,
    "result": ("fail" if fails else ("warn" if warns else "pass")),
    "fail_count": len(fails), "warn_count": len(warns), "issues": issues,
    "note": "Checks each GameplayCueNotify has a bound tag under GameplayCue.*, and flags unbound notifies / duplicate tag bindings."}))
'''

    @mcp.tool()
    def validate_gameplay_cues(ctx, package_path: str = "/Game") -> str:
        """Validate GameplayCueNotify assets vs their bound tags: pass / warn / fail. Read-only.

        package_path: content root to scan (default '/Game').

        Scans GameplayCueNotify Blueprints and flags: a notify with no bound GameplayCueTag (FAIL — never
        triggers); a tag not under the GameplayCue.* root (WARN); a tag bound by multiple notifies (WARN).
        Returns {result, cue_notify_count, cue_notifies:[{name,path,gameplay_cue_tag}], issues:[...]}."""
        try:
            return json.dumps(_exec(_VAL_CUES_BODY, {"package_path": package_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # DEFERRED-BLOCKED (honest refusals; NO mutation)                    #
    # ================================================================== #
    def _blocked(feature, message, **fields):
        payload = {"status": "blocked", "feature": feature, "message": message, "mutated": False}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return json.dumps(payload, indent=2)

    @mcp.tool()
    def add_attribute(ctx, attribute_set: str, attribute_name: str,
                      default_value: float = 0.0) -> str:
        """Add an attribute to an AttributeSet. DEFERRED-BLOCKED (no mutation) in this build.

        A functional gameplay attribute is an FGameplayAttributeData UPROPERTY that GAS registers via C++
        accessor macros (GAMEPLAYATTRIBUTE_*). Verified live: the C++ add_blueprint_variable handler refuses
        the FGameplayAttributeData type ('unsupported type'), and a Blueprint-declared struct variable is not
        a functional gameplay attribute regardless. There is no Python/C++ write path, so attributes cannot
        be added from Python -- author the AttributeSet (with its attributes + accessors) in C++.
        create_attribute_set makes a valid EMPTY subclass; this returns a 'blocked' status, no mutation."""
        return _blocked("add_attribute",
                        ("BLOCKED: gameplay attributes require a C++ FGameplayAttributeData UPROPERTY + "
                         "accessor macros. The C++ add_blueprint_variable handler refuses the "
                         "FGameplayAttributeData type (verified live UE 5.8.1) and Blueprint AttributeSets are "
                         "non-functional. Author attribute sets in C++."),
                        attribute_set=attribute_set, attribute_name=attribute_name)

    # ================================================================== #
    # rename_gameplay_tag — WIRED to C++ #16 RenameGameplayTag           #
    # ================================================================== #
    _RENAME_TAG_BODY = _GASW_HELPERS + r'''
old_tag = PARAMS["old_tag"]; new_tag = PARAMS["new_tag"]
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if mrl is None or not hasattr(mrl, "rename_gameplay_tag"):
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "rename_gameplay_tag",
        "message": "C++ handler rename_gameplay_tag unavailable (plugin DLL predates C++ #16; recompile needed)"}))
else:
    res = _try(lambda: json.loads(mrl.rename_gameplay_tag(old_tag, new_tag)), {"error": "call failed"})
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not (isinstance(res, dict) and res.get("renamed")):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "RenameTagInINI returned false for " + Q + str(old_tag) + Q + " (source tag not registered?)",
            "registered": (res.get("registered") if isinstance(res, dict) else None)}))
    else:
        _ledger().append({"op": "rename_gameplay_tag", "old_tag": old_tag, "new_tag": new_tag})
        print("@@UMCP@@" + json.dumps({"status": "success", "old_tag": old_tag, "new_tag": new_tag,
            "renamed": res.get("renamed"), "registered": res.get("registered"), "ledger_depth": len(_ledger()),
            "note": ("Renamed via C++ RenameGameplayTag (writes an INI redirector to DefaultGameplayTags.ini). "
                     "Ledger op rename_gameplay_tag -> self-inverse rename_gameplay_tag(new_tag, old_tag). "
                     "Persists to the project's DefaultGameplayTags.ini.")}))
'''

    @mcp.tool()
    def rename_gameplay_tag(ctx, old_tag: str, new_tag: str) -> str:
        """Rename a gameplay tag, writing an INI redirector so references keep resolving (C++ #16). Ledgered write.

        old_tag: the existing registered tag (e.g. 'Ability.Fire').
        new_tag: the new tag name; an old->new redirector is written to DefaultGameplayTags.ini.

        Calls MCPReflectionLibrary.rename_gameplay_tag; a nonexistent source tag returns a clean error
        (RenameTagInINI false). Verify with gameplay_tags_read.get_gameplay_tag_info. Ledgered NEW op
        'rename_gameplay_tag' {old_tag, new_tag}; inverse (self-inverse) = rename_gameplay_tag(new_tag, old_tag).
        NOTE: this persists to the project's DefaultGameplayTags.ini."""
        params = {"old_tag": old_tag, "new_tag": new_tag}
        try:
            return json.dumps(_exec(_RENAME_TAG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # add_gameplay_tag_source — WIRED to C++ #16 AddGameplayTagSource     #
    # (UNLEDGERED: no engine "remove tag source" API)                    #
    # ================================================================== #
    _ADD_TAG_SOURCE_BODY = _GASW_HELPERS + r'''
source = PARAMS["source_name"]
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if mrl is None or not hasattr(mrl, "add_gameplay_tag_source"):
    print("@@UMCP@@" + json.dumps({"status": "error", "feature": "add_gameplay_tag_source",
        "message": "C++ handler add_gameplay_tag_source unavailable (plugin DLL predates C++ #16; recompile needed)"}))
else:
    res = _try(lambda: json.loads(mrl.add_gameplay_tag_source(source)), {"error": "call failed"})
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not (isinstance(res, dict) and res.get("added")):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "AddNewGameplayTagSource returned false for " + Q + str(source) + Q + " (already exists or invalid?)",
            "registered": (res.get("registered") if isinstance(res, dict) else None)}))
    else:
        print("@@UMCP@@" + json.dumps({"status": "success", "source": source,
            "added": res.get("added"), "registered": res.get("registered"),
            "note": ("Registered via C++ AddGameplayTagSource (+ live manager rescan). UNLEDGERED: the engine "
                     "exposes no 'remove tag source' API, so there is no faithful inverse (adding an empty "
                     "source is near net-zero). Persists to Config/Tags/<source>.")}))
'''

    @mcp.tool()
    def add_gameplay_tag_source(ctx, source_name: str) -> str:
        """Register a new gameplay-tag *.ini source with the live manager (C++ #16). Unledgered write.

        source_name: source file name (e.g. 'MyProjectTags' or 'MyProjectTags.ini'; the engine appends
                     '.ini' if omitted). Config/Tags/<source_name> is created and the manager rescans.

        Calls MCPReflectionLibrary.add_gameplay_tag_source. UNLEDGERED: the engine exposes no 'remove tag
        source' API, so there is no faithful inverse (an empty source is near net-zero). Persists to the
        project's Config/Tags directory."""
        params = {"source_name": source_name}
        try:
            return json.dumps(_exec(_ADD_TAG_SOURCE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
