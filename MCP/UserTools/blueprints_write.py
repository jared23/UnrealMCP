"""UserTools :: Blueprints (WRITE)  (spec: docs/spec/blueprints.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). WRITE batch, the
mutating counterpart to blueprints_read.py. Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py.

What this build exposes (verified live) — unreal.BlueprintEditorLibrary is richly extended:
  * create_blueprint_asset_with_parent(asset_path, parent_class) -> Blueprint
      Creates a Blueprint asset NON-INTERACTIVELY with the parent class fixed up front, so NO
      modal / parent-picker ever appears (strictly safer than AssetTools.create_asset(...,
      BlueprintFactory) which the protocol warns can pop a modal). We use this.
  * add_function_graph(bp, name) / remove_function_graph(bp, name)  -- a FAITHFUL inverse pair.
  * reparent_blueprint(bp, new_parent_class)                        -- reversible (reparent back).
  * compile_blueprint(bp) -> bool                                   -- benign, deterministic.
  * add_member_variable(bp, name, EdGraphPinType)                   -- add works, but see below.

GOTCHA discovered live (drove the whole cleanup design): create_blueprint_asset_with_parent
auto-OPENS the Blueprint Editor for the new asset on the (Windows) editor. An asset that is open
in an editor CANNOT be deleted -- EditorAssetLibrary.delete_asset returns False (even after
save + collect_garbage). The fix is to call
    unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).close_all_editors_for_asset(obj)
BEFORE delete_asset. create_blueprint closes the editor it opens; the create_blueprint undo
inverse closes editors then deletes. (No modal is ever triggered -- close/delete are silent.)

Implemented (all validated live, session=agentA, editor left CLEAN):
  - create_blueprint      (WRITE; ledgered op "create_blueprint"; inverse = close editors + delete asset [+ rmdir if we made it])
  - add_blueprint_function(WRITE; ledgered op "add_bp_function"; inverse = remove_function_graph)
  - reparent_blueprint    (WRITE; ledgered op "reparent_blueprint"; inverse = reparent to prior parent)
  - add_event_dispatcher  (WRITE; ledgered op "add_event_dispatcher"; inverse = remove_event_dispatcher)
  - compile_blueprint     (benign WRITE; NOT ledgered -- compilation is a deterministic recompute
                           of the existing graph state, nothing to revert; like save_level)

DEFERRED (no faithful, ledger-serializable inverse in this build's Python surface -- refused per
protocol rather than shipping an unrevertable mutation):
  - add_blueprint_variable / remove_blueprint_variable: add_member_variable IS achievable (2026-08-15
      re-probe: even though EdGraphPinType's PinCategory is a PROTECTED struct member that Python
      cannot read/write or construct from scratch, you CAN obtain a valid EdGraphPinType via
      get_member_variable_type(bp, <inherited_var>) and reuse/clone it -- add_member_variable with a
      cloned inherited-bool pin type returns True and the var appears in list_member_variable_names).
      BUT there is still NO faithful targeted REMOVE: BlueprintEditorLibrary has no
      remove_member_variable; the Blueprint's 'new_variables' UPROPERTY is not Python-accessible; the
      only removal is remove_unused_variables(bp), which is DEFINITIVELY non-targeted -- live proof:
      added 2 unused vars, remove_unused_variables returned count=2 and stripped BOTH (it would also
      strip any pre-existing unused user vars). Not gate-able to a single var from Python. So add
      cannot be faithfully undone -> DEFERRED. Faithful fix = a tiny C++ handler over
      FBlueprintEditorUtils::RemoveMemberVariable (proposed MCPReflectionLibrary.RemoveMemberVariable,
      mirroring the shipped AddStructField/RemoveStructField pattern); proposed ledger op then
      add_blueprint_variable {bp_path, var_name} -> inverse = targeted remove.
  - remove_blueprint_function: remove_function_graph works, but its inverse (re-add) yields an
      EMPTY graph -- a populated function's nodes/wiring are lost -> not faithful. Refused.
  - add_blueprint_event (override): add_event_override(bp, event_name, IntPoint) works and is
      idempotent (re-adding returns the SAME existing K2Node unchanged), BUT it is NOT faithfully
      reversible (2026-08-15 re-probe): the created K2Node_Event is inserted INTO the Blueprint's
      SHARED "EventGraph" (list_graph_names is unchanged before/after: ['UserConstructionScript',
      'EventGraph'] -- no new graph is created). BlueprintEditorLibrary has no node-level removal;
      the only graph-level removal, remove_graph(bp, EventGraph), would delete the ENTIRE EventGraph
      (all the user's event logic), so it is catastrophically non-faithful. (Note: remove_graph IS a
      faithful inverse only for a whole graph we added as a unit -- e.g. add_function_graph, already
      covered by remove_function_graph.) No faithful ledger-serializable inverse -> DEFERRED.
      Faithful fix = a C++ handler that removes a single K2Node from a graph
      (FBlueprintEditorUtils::RemoveNode) keyed by the returned node id.

Undo: this module does NOT register its own `undo` tool (the gold-standard editor_level.py owns the
single unified `undo`). It introduces 4 NEW ledger ops -- create_blueprint / add_bp_function /
reparent_blueprint (already folded) plus add_event_dispatcher (NEW, this extension) -- schemas in
each tool's docstring and in the build report; the coordinator folds matching inverse branches into
editor_level.undo. add_event_dispatcher's inverse:
    bp = load(bp_path); BlueprintEditorLibrary.remove_event_dispatcher(bp, Name(name)); compile
Reversibility was proven live by executing that exact inverse against the agentA ledger (depth 0).
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()             -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_bp(path)        -> (blueprint, err) load + type-check a Blueprint asset.
    #   _resolve_class(spec)  -> (UClass, err) from an engine class name, a /Script class path,
    #                            or a Blueprint asset path (uses its generated class).
    #   _close_editors(obj)   -> silently close any open asset editor for obj (so it can be deleted
    #                            and so we never leave a Blueprint Editor tab open on the shared editor).
    _BPW_HELPERS = r'''
import unreal, json, builtins
_BEL = unreal.BlueprintEditorLibrary
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
def _resolve_class(spec):
    if spec is None:
        return None, "no class given"
    # 1) short engine class name exposed on the unreal module (Actor, Pawn, Character, ...).
    #    getattr yields the Python-wrapped TYPE; return its UClass instance (static_class) so the
    #    result carries get_name/get_path_name AND is accepted by the BEL create/reparent calls.
    c = getattr(unreal, spec, None)
    if isinstance(c, type) and issubclass(c, unreal.Object):
        try:
            uc = c.static_class()
            if isinstance(uc, unreal.Class):
                return uc, None
        except Exception:
            pass
    # 2) a class object path like /Script/Engine.Actor -> load_object returns the UClass
    if "/" in spec or "." in spec:
        try:
            o = unreal.load_object(None, spec)
            if isinstance(o, unreal.Class):
                return o, None
        except Exception:
            pass
        # 3) a Blueprint asset path -> use its generated class as the parent
        try:
            a = unreal.EditorAssetLibrary.load_asset(spec)
            if isinstance(a, unreal.Blueprint):
                g = _BEL.generated_class(a)
                if g is not None:
                    return g, None
            if isinstance(a, unreal.Class):
                return a, None
        except Exception:
            pass
    return None, "could not resolve class: %s" % spec
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
'''

    # ------------------------------------------------------------------ #
    # create_blueprint — non-interactive Blueprint asset creation         #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _BPW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
parent_spec = PARAMS.get("parent_class") or "Actor"
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
EAL = unreal.EditorAssetLibrary
asset_path = package_path + "/" + name
pcls, err = _resolve_class(parent_spec)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    bp = None
    with unreal.ScopedEditorTransaction("MCP create_blueprint"):
        bp = _BEL.create_blueprint_asset_with_parent(asset_path, pcls)
    if bp is None:
        # roll back a directory we may have implicitly created
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_blueprint_asset_with_parent returned None for %s" % asset_path}))
    else:
        compiled = None
        if do_compile:
            try: compiled = bool(_BEL.compile_blueprint(bp))
            except Exception as e: compiled = "error: %s" % e
        # do NOT leave a Blueprint Editor tab open on the shared editor
        _close_editors(bp)
        gcls = _BEL.generated_class(bp)
        _ledger().append({"op": "create_blueprint", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": bp.get_name(),
            "asset_path": asset_path, "object_path": bp.get_path_name(),
            "parent_class": pcls.get_name(),
            "generated_class": gcls.get_name() if gcls else None,
            "compiled": compiled, "created_dir": created_dir,
            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_blueprint(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                         parent_class: str = "Actor", compile: bool = True) -> str:
        """Create a new Blueprint asset non-interactively (NO modal / parent-picker).

        name:         asset name for the new Blueprint (e.g. 'BP_MyActor').
        package_path: content directory to create it under (default '/Game/MCP_Scratch');
                      must be under a valid mounted root ('/Game', '/Engine', a plugin root).
                      Intermediate folders are created as needed.
        parent_class: parent class -- an engine class name ('Actor', 'Pawn', 'Character',
                      'ActorComponent'), a /Script class path, or a Blueprint asset path (its
                      generated class is used). Default 'Actor'.
        compile:      compile the fresh Blueprint after creation (default True).

        Uses BlueprintEditorLibrary.create_blueprint_asset_with_parent, which fixes the parent up
        front so no dialog is ever shown. Closes the Blueprint Editor it opens so no tab lingers.

        Ledgered write op 'create_blueprint' {asset_path, package_path, created_dir}. Inverse:
        close editors for the asset, delete it, and remove package_path if we created it and it is
        now empty. The asset is created UNSAVED (in-memory); undo removes it cleanly."""
        params = {"name": name, "package_path": package_path,
                  "parent_class": parent_class, "compile": compile}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_blueprint_function — add an (empty) function graph              #
    # ------------------------------------------------------------------ #
    _ADD_FUNC_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]
func_name = PARAMS["func_name"]
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    existing = [str(x) for x in _BEL.list_graph_names(bp)]
    if func_name in existing:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "a graph named '%s' already exists on this blueprint" % func_name,
            "graphs": existing}))
    else:
        graph = None
        with unreal.ScopedEditorTransaction("MCP add_blueprint_function"):
            graph = _BEL.add_function_graph(bp, func_name)
        compiled = None
        if do_compile:
            try: compiled = bool(_BEL.compile_blueprint(bp))
            except Exception as e: compiled = "error: %s" % e
        after = [str(x) for x in _BEL.list_graph_names(bp)]
        added = func_name in after
        if added:
            _ledger().append({"op": "add_bp_function", "bp_path": bp.get_path_name(),
                              "func_name": func_name})
        print("@@UMCP@@" + json.dumps({"status": "success" if added else "error",
            "blueprint": bp.get_name(), "func_name": func_name, "added": added,
            "graphs": after, "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_blueprint_function(ctx, blueprint_path: str, func_name: str,
                               compile: bool = True) -> str:
        """Add a new (empty) function graph to a Blueprint.

        blueprint_path: object/package path of the Blueprint asset.
        func_name:      name for the new function (must not collide with an existing graph).
        compile:        recompile after adding (default True).

        Uses BlueprintEditorLibrary.add_function_graph. Ledgered write op 'add_bp_function'
        {bp_path, func_name}. Inverse: BlueprintEditorLibrary.remove_function_graph(bp, func_name)
        + recompile -- a FAITHFUL inverse because the added graph is empty. (Removing a function
        that already has a body is NOT reversible and is a separate, deferred command.)"""
        params = {"blueprint_path": blueprint_path, "func_name": func_name, "compile": compile}
        try:
            return json.dumps(_exec(_ADD_FUNC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # reparent_blueprint — change a Blueprint's parent class              #
    # ------------------------------------------------------------------ #
    _REPARENT_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]
new_spec = PARAMS["new_parent_class"]
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ncls, cerr = _resolve_class(new_spec)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        prior = _BEL.get_blueprint_parent_class(bp)
        prior_name = prior.get_name() if prior else None
        prior_path = prior.get_path_name() if prior else None
        if prior_path == ncls.get_path_name():
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "blueprint is already parented to %s" % ncls.get_name()}))
        else:
            with unreal.ScopedEditorTransaction("MCP reparent_blueprint"):
                _BEL.reparent_blueprint(bp, ncls)
            compiled = None
            if do_compile:
                try: compiled = bool(_BEL.compile_blueprint(bp))
                except Exception as e: compiled = "error: %s" % e
            now = _BEL.get_blueprint_parent_class(bp)
            now_name = now.get_name() if now else None
            _ledger().append({"op": "reparent_blueprint", "bp_path": bp.get_path_name(),
                              "prior_parent_path": prior_path, "new_parent_path": ncls.get_path_name()})
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(),
                "prior_parent": prior_name, "new_parent": now_name,
                "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def reparent_blueprint(ctx, blueprint_path: str, new_parent_class: str,
                           compile: bool = True) -> str:
        """Reparent a Blueprint to a new parent class.

        blueprint_path:   object/package path of the Blueprint asset.
        new_parent_class: the new parent -- engine class name, /Script class path, or a Blueprint
                          asset path (its generated class is used).
        compile:          recompile after reparenting (default True).

        Uses BlueprintEditorLibrary.reparent_blueprint. Ledgered write op 'reparent_blueprint'
        {bp_path, prior_parent_path, new_parent_path}. Inverse: reparent back to prior_parent_path
        + recompile -- FAITHFUL (both directions of reparent are supported and verified)."""
        params = {"blueprint_path": blueprint_path, "new_parent_class": new_parent_class,
                  "compile": compile}
        try:
            return json.dumps(_exec(_REPARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_event_dispatcher — add a multicast-delegate event dispatcher     #
    #   CLEAN faithful pair: add_event_dispatcher <-> remove_event_dispatcher
    # ------------------------------------------------------------------ #
    _ADD_DISPATCHER_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]
name = PARAMS["dispatcher_name"]
do_compile = PARAMS.get("compile")
do_compile = True if do_compile is None else bool(do_compile)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    def _dl():
        try:
            return [str(x) for x in _BEL.list_event_dispatchers(bp)]
        except Exception:
            return []
    before = _dl()
    if name in before:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "an event dispatcher named '%s' already exists on this blueprint" % name,
            "dispatchers": before}))
    else:
        added = False
        with unreal.ScopedEditorTransaction("MCP add_event_dispatcher"):
            added = bool(_BEL.add_event_dispatcher(bp, unreal.Name(name)))
        compiled = None
        if do_compile:
            try: compiled = bool(_BEL.compile_blueprint(bp))
            except Exception as e: compiled = "error: %s" % e
        after = _dl()
        present = name in after
        if added and present:
            _ledger().append({"op": "add_event_dispatcher", "bp_path": bp.get_path_name(),
                              "name": name})
        print("@@UMCP@@" + json.dumps({"status": "success" if (added and present) else "error",
            "blueprint": bp.get_name(), "dispatcher_name": name, "added": added and present,
            "dispatchers": after, "compiled": compiled, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_event_dispatcher(ctx, blueprint_path: str, dispatcher_name: str,
                             compile: bool = True) -> str:
        """Add an event dispatcher (multicast delegate) to a Blueprint.

        blueprint_path: object/package path of the Blueprint asset.
        dispatcher_name: name for the new dispatcher (must not collide with an existing one).
        compile:        recompile after adding (default True).

        Uses BlueprintEditorLibrary.add_event_dispatcher (returns False if the name is already
        in use). Ledgered write op 'add_event_dispatcher' {bp_path, name}. Inverse:
        BlueprintEditorLibrary.remove_event_dispatcher(bp, name) + recompile -- a FAITHFUL,
        CLEAN pair verified live (add then remove returns the dispatcher list to its prior state;
        add refuses duplicates and remove refuses non-existent names, so the prior state of a
        freshly-added dispatcher is always 'absent' -> removal is exact)."""
        params = {"blueprint_path": blueprint_path, "dispatcher_name": dispatcher_name,
                  "compile": compile}
        try:
            return json.dumps(_exec(_ADD_DISPATCHER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_blueprint_variable / remove_blueprint_variable (via C++ #7)      #
    # ------------------------------------------------------------------ #
    # ENABLED 2026-08-15 by C++ #7 (MCPReflectionLibrary.AddBlueprintVariable / RemoveBlueprintVariable).
    # The ADD via BlueprintEditorLibrary needed a hard-to-build pin type; the C++ handler uses BuildPinType.
    # Faithful pair: variables are listable (list_member_variable_names) so undo is exact.
    _ADD_VAR_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]; var_name = PARAMS["var_name"]; var_type = PARAMS["var_type"]
compile_after = PARAMS.get("compile", True)
mrl = getattr(unreal, "MCPReflectionLibrary", None)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif mrl is None or not hasattr(mrl, "add_blueprint_variable"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_blueprint_variable unavailable (plugin DLL predates C++ #7; recompile needed)"}))
else:
    existing = [str(v) for v in (_BEL.list_member_variable_names(bp) or [])]
    if var_name in existing:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "variable already exists: %s" % var_name}))
    else:
        res = json.loads(mrl.add_blueprint_variable(bp, var_name, var_type))
        if isinstance(res, dict) and res.get("error"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
        elif not res.get("added"):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "AddMemberVariable returned false for '%s'" % var_name}))
        else:
            if compile_after:
                try: _BEL.compile_blueprint(bp)
                except Exception: pass
            _ledger().append({"op": "add_blueprint_variable", "bp_path": path, "var_name": var_name})
            after = [str(v) for v in (_BEL.list_member_variable_names(bp) or [])]
            print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(),
                "added_variable": var_name, "type": var_type, "variable_count": len(after),
                "present": var_name in after, "ledger_depth": len(_ledger())}))
'''

    _REMOVE_VAR_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]; var_name = PARAMS["var_name"]; compile_after = PARAMS.get("compile", True)
# The variable's type must be caller-provided for undo: BlueprintEditorLibrary.get_member_variable_type
# returns an EdGraphPinType struct that is OPAQUE to Python (pin_category is not get_editor_property-
# readable), so it cannot be auto-captured. If var_type is "" the removal still works but undo cannot
# re-add. (A tiny future C++ GetBlueprintVariableTypeName would enable auto-capture.)
var_type = PARAMS.get("var_type") or None
mrl = getattr(unreal, "MCPReflectionLibrary", None)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif mrl is None or not hasattr(mrl, "remove_blueprint_variable"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_blueprint_variable unavailable (plugin DLL predates C++ #7; recompile needed)"}))
else:
    res = json.loads(mrl.remove_blueprint_variable(bp, var_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("found"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "variable not found: %s" % var_name}))
    else:
        if compile_after:
            try: _BEL.compile_blueprint(bp)
            except Exception: pass
        _ledger().append({"op": "remove_blueprint_variable", "bp_path": path,
                          "var_name": var_name, "var_type": var_type})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(),
            "removed_variable": var_name, "captured_type": var_type, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_blueprint_variable(ctx, blueprint_path: str, var_name: str, var_type: str,
                               compile: bool = True) -> str:
        """Add a member variable to a Blueprint. REQUIRES the C++ #7 handler
        (unreal.MCPReflectionLibrary.add_blueprint_variable); returns a clear error on an older DLL.

        blueprint_path: object/package path of the Blueprint asset.
        var_name:       name for the new variable (refused if it already exists).
        var_type:       one of bool | byte | int | int64 | float | name | string | text | vector |
                        vector2d | rotator | transform | linearcolor (case-insensitive).
        compile:        recompile after (default True). Verify with list_blueprint_variables.

        Ledgered write op 'add_blueprint_variable' {bp_path, var_name}. Inverse (in editor_level.undo):
        remove_blueprint_variable(var_name) -> FAITHFUL (removes exactly the variable we added)."""
        params = {"blueprint_path": blueprint_path, "var_name": var_name, "var_type": var_type,
                  "compile": compile}
        try:
            return json.dumps(_exec(_ADD_VAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_blueprint_variable(ctx, blueprint_path: str, var_name: str, var_type: str = "",
                                  compile: bool = True) -> str:
        """Remove a member variable (by name) from a Blueprint. REQUIRES the C++ #7 handler.

        blueprint_path: object/package path of the Blueprint asset.
        var_name:       name of the variable to remove.
        var_type:       OPTIONAL -- pass the variable's type (bool/int/float/vector/... same vocabulary
                        as add_blueprint_variable) to make this UNDOABLE. It cannot be auto-detected: the
                        engine returns the variable's type as an EdGraphPinType struct that is opaque to
                        Python. If omitted, the removal still works but undo cannot re-add the variable.
        compile:        recompile after (default True).

        Ledgered write op 'remove_blueprint_variable' {bp_path, var_name, var_type}. Inverse (in
        editor_level.undo): add_blueprint_variable(var_name, var_type) if var_type was provided --
        near-faithful (same name + type; a custom default value / category is not restored); otherwise
        undo reports it cannot restore. NOTE: removing a variable still referenced by graph nodes may
        leave dangling refs."""
        params = {"blueprint_path": blueprint_path, "var_name": var_name, "var_type": var_type,
                  "compile": compile}
        try:
            return json.dumps(_exec(_REMOVE_VAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_event_override — add an event override node (faithful via C++ #9)#
    # ------------------------------------------------------------------ #
    # ENABLED 2026-08-15 by C++ #9 (get_blueprint_event_nodes_json + remove_event_node_by_guid). The add
    # itself is BlueprintEditorLibrary.add_event_override (Python); faithfulness comes from diffing the
    # event-node GUIDs before/after: a fresh Actor BP already ships default ReceiveBeginPlay/Tick/Overlap
    # nodes and add_event_override REUSES an existing node (adds nothing) -> the diff yields no new guid ->
    # we don't ledger. Only a genuinely-new node is ledgered, and its exact guid drives a faithful undo.
    _ADD_EVENT_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]; event_name = PARAMS["event_name"]; compile_after = PARAMS.get("compile", True)
mrl = getattr(unreal, "MCPReflectionLibrary", None)
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif mrl is None or not hasattr(mrl, "get_blueprint_event_nodes_json") or not hasattr(mrl, "remove_event_node_by_guid"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handlers get_blueprint_event_nodes_json/remove_event_node_by_guid unavailable (plugin DLL predates C++ #9; recompile needed)"}))
else:
    before = set()
    try:
        _d = json.loads(mrl.get_blueprint_event_nodes_json(bp))
        before = set(e.get("node_guid") for e in (_d.get("events") or []))
    except Exception:
        before = set()
    add_err = None
    try:
        _BEL.add_event_override(bp, unreal.Name(event_name), unreal.IntPoint(0, 0))
    except Exception as e:
        add_err = str(e)
    if compile_after:
        try: _BEL.compile_blueprint(bp)
        except Exception: pass
    after = set()
    try:
        _d2 = json.loads(mrl.get_blueprint_event_nodes_json(bp))
        after = set(e.get("node_guid") for e in (_d2.get("events") or []))
    except Exception:
        after = set()
    new_guids = [g for g in after if g not in before]
    if add_err and not new_guids:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_event_override failed: %s" % add_err}))
    elif not new_guids:
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(), "event": event_name,
            "added": False, "note": "event override already present (reused an existing node); nothing added -> nothing to undo",
            "event_count": len(after), "ledger_depth": len(_ledger())}))
    else:
        try: unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        except Exception: pass
        ng = new_guids[0]
        _ledger().append({"op": "add_event_override", "bp_path": path, "node_guid": ng})
        print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(), "event": event_name,
            "added": True, "node_guid": ng, "event_count": len(after), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_event_override(ctx, blueprint_path: str, event_name: str, compile: bool = True) -> str:
        """Add an event override node to a Blueprint's event graph. REQUIRES the C++ #9 handlers.

        blueprint_path: object/package path of the Blueprint asset.
        event_name:     the event to override, by its function name -- e.g. 'ReceiveBeginPlay',
                        'ReceiveTick', 'ReceiveEndPlay', 'ReceiveActorBeginOverlap'.
        compile:        recompile after (default True).

        Uses BlueprintEditorLibrary.add_event_override, then diffs the Blueprint's event-node GUIDs
        (via get_blueprint_event_nodes_json) before/after to find EXACTLY the node added. IMPORTANT: a
        fresh Actor BP already ships default (disabled) BeginPlay/Tick/Overlap nodes, and add_event_override
        REUSES an existing node rather than adding a duplicate -- so overriding one of those returns
        added=false (nothing was added, nothing to undo). Only a genuinely-new node is ledgered.

        Ledgered write op 'add_event_override' {bp_path, node_guid} (only when a new node was added).
        Inverse (in editor_level.undo): remove_event_node_by_guid(node_guid) -> FAITHFUL (removes exactly
        the node we added, by guid)."""
        params = {"blueprint_path": blueprint_path, "event_name": event_name, "compile": compile}
        try:
            return json.dumps(_exec(_ADD_EVENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # compile_blueprint — benign recompile (NOT ledgered)                 #
    # ------------------------------------------------------------------ #
    _COMPILE_BODY = _BPW_HELPERS + r'''
path = PARAMS["blueprint_path"]
bp, err = _load_bp(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ok = None
    try:
        ok = bool(_BEL.compile_blueprint(bp))
    except Exception as e:
        ok = "error: %s" % e
    print("@@UMCP@@" + json.dumps({"status": "success", "blueprint": bp.get_name(),
        "path": bp.get_path_name(), "compiled_ok": ok,
        "note": "Benign recompute of the existing graph state; not ledgered (nothing to revert)."}))
'''

    @mcp.tool()
    def compile_blueprint(ctx, blueprint_path: str) -> str:
        """Compile a Blueprint. Benign write -- NOT ledgered (compilation is a deterministic
        recompute of the current graph state; there is nothing to undo).

        blueprint_path: object/package path of the Blueprint asset.
        Returns compiled_ok (True if compilation succeeded with no errors). Any compile
        Warnings/Errors surface via _log_warnings."""
        try:
            return json.dumps(_exec(_COMPILE_BODY, {"blueprint_path": blueprint_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (no faithful, ledger-serializable inverse in this build):
    #   add_blueprint_variable / remove_blueprint_variable
    #       add_member_variable IS achievable (clone a pin type from
    #       get_member_variable_type(bp,<inherited_var>); EdGraphPinType's
    #       PinCategory is a protected struct member, not constructible from
    #       scratch in Python) -- BUT there is NO faithful targeted removal:
    #       no remove_member_variable; 'new_variables' UPROPERTY not
    #       Python-accessible; remove_unused_variables is NON-targeted (live
    #       proof: 2 unused vars added -> returned count=2, stripped both).
    #       -> add is not faithfully undoable. Needs C++ RemoveMemberVariable.
    #   remove_blueprint_function
    #       remove_function_graph works, but re-adding yields an EMPTY graph
    #       (a populated function's nodes are lost) -> not faithful.
    #   add_blueprint_event (override)
    #       add_event_override(bp,event,IntPoint) works + is idempotent, but the
    #       created K2Node_Event lands in the SHARED EventGraph (list_graph_names
    #       unchanged); remove_graph would delete the WHOLE EventGraph, and there
    #       is no node-level removal -> no faithful inverse. Needs C++ node removal.
    #   These would need a C++ handler over FBlueprintEditorUtils /
    #   FKismetEditorUtilities (RemoveMemberVariable / RemoveNode).
    # SHIPPED in this extension (faithful, CLEAN pair): add_event_dispatcher
    #   <-> remove_event_dispatcher (both bool; add refuses dup, remove refuses
    #   missing) -- new ledger op 'add_event_dispatcher' {bp_path, name}.
    # This module registers NO `undo` tool; editor_level.py owns the unified
    # `undo`. NEW ledger op schemas + inverse logic are documented in each
    # tool's docstring and in the build report for the coordinator to fold in.
    # ------------------------------------------------------------------ #
