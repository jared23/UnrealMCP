"""UserTools :: StateTree (READ)  (spec: docs/spec/statetree.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8.1). READ-ONLY batch.
NO asset editors are ever opened (no modals); nothing is mutated; no ledger. This introspects
UE5 StateTree assets (hierarchical state + utility AI): their schema/context, the authorable state
hierarchy, per-state tasks / enter-conditions / considerations / transitions, and the StateTree
schema + node CLASSES that are authorable in the project. The query convention, base64 PARAMS, and
Output-Log auto-capture are copied verbatim from editor_level.py (the gold standard), matching
eqs.py / ai_read.py's AssetRegistry-enumeration style.

PROBE FINDINGS (verified live vs TestMCPSetup, UE 5.8.1 -- what IS / ISN'T reflectable):
  * 2 StateTree assets exist: /Game/Variant_Combat/Blueprints/AI/ST_CombatEnemy (26 states) and
    /Game/Variant_SideScrolling/Blueprints/AI/ST_SideScrollingNPC (7 states). AssetRegistry
    enumerates them via class_paths=[TopLevelAssetPath("/Script/StateTreeModule", "StateTree")].
    (StateTree is a /Script/StateTreeModule class, parent UDataAsset.)
  * The COMPILED/baked runtime data on UStateTree (States, Transitions, Nodes, Frames, Schema,
    EditorData, Parameters, ...) is stored in PROTECTED / non-BlueprintVisible C++ UPROPERTYs.
    Stock Python get_editor_property REFUSES all of them ("... is protected and cannot be read");
    unreal.MCPReflectionLibrary.get_editor_property enforces the SAME protection. Their METADATA
    (property names / cpp_type) IS readable via get_object_property_metadata_json.
  * UNLIKE EQS, the AUTHORABLE graph IS fully value-readable, reached through the editor data:
      - The UStateTreeEditorData subobject is reached by name via
        unreal.find_object(state_tree, "StateTreeEditorData_<N>") (auto-named, index 0 for these).
      - EditorData.get_editor_property("schema") -> the UStateTreeSchema subobject (VALUE-readable);
        its class name (e.g. StateTreeAIComponentSchema) and schema props (AIControllerClass, and
        Context props like ContextActorClass) read back real.
      - EditorData.get_editor_property("sub_trees") -> TArray<UStateTreeState> (the ROOT states).
        UStateTreeState is a UObject and is FULLY value-readable: Name, Type
        (State/Group/Linked/LinkedAsset/Subtree), SelectionBehavior, Weight, Children (recurse),
        Tasks, EnterConditions, Considerations, Transitions, LinkedSubtree/LinkedAsset.
      - EditorData.get_editor_property("evaluators" / "global_tasks") -> TArray of editor nodes.
      - Each Task/Condition/Evaluator/Consideration is an FStateTreeEditorNode struct. Its inner
        node is an FInstancedStruct: node.export_text() yields the concrete node TYPE PATH plus all
        config VALUES, e.g. "/Script/StateTreeModule.StateTreeMoveToTask(...)". Blueprint-based nodes
        instead carry an instance_object UObject (class name = the BP node type). So node TYPES and
        their configured VALUES ARE readable here (parsed from export_text) -- NOT faked.
      - A transition's target is an FStateTreeStateLink: link.export_text() gives
        (Name="<target>",ID=..,LinkType=Succeeded|Failed|NextState|NextSelectableState|GotoState,..).
  * KNOWN LIMITS (reported in payloads, never hidden):
      - The native engine StateTree node types (StateTreeMoveToTask, StateTreeRunEnvQueryTask, ...)
        are C++ FInstancedStruct types (F...Base structs), NOT UClasses, so they are NOT enumerable
        via UClass reflection. list_state_tree_node_classes enumerates the Blueprint node BASE
        classes + their unreal.*-namespace subclasses + project Blueprint subclasses (the authorable
        BP node set); the native struct node types are surfaced only when actually USED in an asset
        (via get_state_tree_states / get_state_tree_state, parsed from export_text). An exhaustive
        native-node-type listing would need a C++ struct-registry handler (DEFERRED-C++).
      - The COMPILED UStateTree.States/Nodes/Transitions arrays (protected) are not read; this module
        reads the EDITOR (authorable) graph, which is the meaningful/source-of-truth structure.
      - Full FGuid state IDs and binding batches are present but not deeply resolved (property
        bindings live in FStateTreeEditorPropertyBindings; a dedicated binding reader is DEFERRED).

Implemented (all read-only, no ledger):
  - list_state_trees             (AssetRegistry enumerate StateTree assets)
  - get_state_tree_info          (schema/context class + root/total state counts + evaluators/global tasks)
  - get_state_tree_states        (recursive authorable state hierarchy: names/types/tasks/transitions)
  - get_state_tree_state         (one state: full tasks/enter-conditions/considerations/transitions w/ config)
  - list_state_tree_schemas      (authorable UStateTreeSchema classes: namespace + project Blueprint)
  - list_state_tree_node_classes (authorable StateTree Task/Evaluator/Condition/Consideration BP node classes)
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
# (This module deliberately parses export_text with pure str ops -- no regex -- to avoid backslashes.)

# Blueprint node BASE classes (the authorable UClass roots; native node types are FInstancedStruct
# structs, not UClasses -- see module docstring). These ARE exported into the unreal.* namespace.
_NODE_BASES = {
    "task": "StateTreeTaskBlueprintBase",
    "evaluator": "StateTreeEvaluatorBlueprintBase",
    "condition": "StateTreeConditionBlueprintBase",
    "consideration": "StateTreeConsiderationBlueprintBase",
}


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
    _ST_HELPERS = r'''
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
def _enum_name(v):
    # unreal enum -> short name ("STATE" from StateTreeStateType.STATE), robust fallback parse.
    if v is None:
        return None
    n = getattr(v, "name", None)
    if isinstance(n, str) and n:
        return n
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".", 1)[1].split(":", 1)[0].strip()
    return s
def _is_obj(v):
    return v is not None and not isinstance(v, str)
def _find_editor_data(st):
    # UStateTreeEditorData is a named subobject outered to the StateTree; probe the auto-name range.
    for i in range(0, 12):
        o = _try(lambda i=i: unreal.find_object(st, "StateTreeEditorData_%d" % i))
        if _is_obj(o):
            return o
    return None
def _node_desc(en, include_config=True, cfg_limit=400):
    # en is an FStateTreeEditorNode struct. Resolve node type + (optional) configured values.
    rec = {"type": None, "source": "empty"}
    io = _try(lambda: en.get_editor_property("instance_object"))
    if _is_obj(io):
        rec["type"] = io.get_class().get_name()
        rec["source"] = "blueprint"
        return rec
    inner = _try(lambda: en.get_editor_property("node"))
    if _is_obj(inner):
        txt = _try(lambda: inner.export_text(), "") or ""
        head = txt.split("(", 1)[0].strip()
        if head.startswith("/Script/"):
            rec["type"] = head.split(".")[-1]
            rec["type_path"] = head
            rec["source"] = "native"
            if include_config and len(txt) > len(head):
                rec["config"] = txt[:cfg_limit]
        elif txt.strip():
            rec["source"] = "native"
            if include_config:
                rec["config"] = txt[:cfg_limit]
    return rec
def _link_desc(link):
    d = {"link_type": _enum_name(_try(lambda: link.get_editor_property("link_type"))), "name": None}
    txt = _try(lambda: link.export_text(), "") or ""
    key = 'Name="'
    i = txt.find(key)
    if i >= 0:
        j = txt.find('"', i + len(key))
        if j > i:
            d["name"] = txt[i + len(key):j]
    return d
def _trans_desc(tr, include_config=True, cfg_limit=300, max_conds=16):
    d = {"trigger": _enum_name(_try(lambda: tr.get_editor_property("trigger"))),
         "priority": _enum_name(_try(lambda: tr.get_editor_property("priority")))}
    link = _try(lambda: tr.get_editor_property("state"))
    if _is_obj(link):
        d["target"] = _link_desc(link)
    conds = _try(lambda: tr.get_editor_property("conditions"))
    if _is_obj(conds) and len(conds) > 0:
        d["conditions"] = [_node_desc(c, include_config, cfg_limit) for c in list(conds)[:max_conds]]
    return d
def _task_types(st):
    tks = _try(lambda: st.get_editor_property("tasks"))
    if not _is_obj(tks):
        return []
    return [(_node_desc(t, include_config=False).get("type") or "?") for t in tks]
'''

    # ------------------------------------------------------------------ #
    # list_state_trees — AssetRegistry enumerate StateTree assets          #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _ST_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 200
ar = unreal.AssetRegistryHelpers.get_asset_registry()
rows = []
err = None
try:
    flt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/StateTreeModule", "StateTree")],
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
result["note"] = ("StateTree is a /Script/StateTreeModule class (parent UDataAsset); enumerated via "
                  "AssetRegistry TopLevelAssetPath('/Script/StateTreeModule','StateTree'). Use "
                  "get_state_tree_info / get_state_tree_states on a path for its schema + state graph.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_state_trees(ctx, path: str = None, max_results: int = 200) -> str:
        """Enumerate StateTree assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Variant_Combat'.
        max_results: cap the assets listed (default 200; true 'count' is always reported).

        Returns {count, returned, assets:[{name, path}]}. StateTree is a /Script/StateTreeModule class.
        Follow up with get_state_tree_info(asset_path) for schema/context + counts, or
        get_state_tree_states(asset_path) for the full authorable state hierarchy."""
        params = {"path": path, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_state_tree_info — schema/context + counts                        #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _ST_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a StateTree (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name()}
    ed = _find_editor_data(obj)
    if ed is None:
        info["editor_data"] = None
        info["note"] = ("Could not reach the UStateTreeEditorData subobject by name; the authorable "
                        "graph is exposed only through it. The compiled UStateTree state arrays are "
                        "protected C++ UPROPERTYs (metadata-only), so no state structure is available.")
    else:
        info["editor_data_class"] = ed.get_class().get_name()
        # Schema (value-readable subobject)
        sch = _try(lambda: ed.get_editor_property("schema"))
        if _is_obj(sch):
            srec = {"class": sch.get_class().get_name(), "path": sch.get_path_name()}
            # Common schema context props (present depending on schema class); read if available.
            for pn in ("ai_controller_class", "context_actor_class", "actor_class"):
                v = _try(lambda pn=pn: sch.get_editor_property(pn))
                if _is_obj(v):
                    srec[pn] = v.get_name()
            info["schema"] = srec
        else:
            info["schema"] = None
        # Root subtrees + total state count
        subs = _try(lambda: ed.get_editor_property("sub_trees"))
        roots = []
        total = [0]
        def _count(s):
            total[0] += 1
            for c in (_try(lambda: s.get_editor_property("children")) or []):
                _count(c)
        if _is_obj(subs):
            for s in subs:
                roots.append(str(s.get_editor_property("name")))
                _count(s)
        info["root_states"] = roots
        info["root_state_count"] = len(roots)
        info["total_state_count"] = total[0]
        # Tree-global evaluators + global tasks (editor-data level)
        for kind, prop in (("evaluators", "evaluators"), ("global_tasks", "global_tasks")):
            arr = _try(lambda prop=prop: ed.get_editor_property(prop))
            if _is_obj(arr):
                info[kind] = [(_node_desc(x, include_config=False).get("type") or "?") for x in arr]
                info[kind + "_count"] = len(arr)
            else:
                info[kind] = []
                info[kind + "_count"] = 0
    info["reflection_note"] = (
        "Schema and the authorable state graph are read from the UStateTreeEditorData subobject "
        "(reached via find_object). The COMPILED UStateTree.States/Nodes/Transitions arrays are "
        "protected C++ UPROPERTYs (metadata-only, not value-read here). Node TYPES/VALUES come from "
        "each FStateTreeEditorNode's FInstancedStruct export_text (real, not faked).")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_state_tree_info(ctx, asset_path: str) -> str:
        """Overview of one StateTree asset: schema/context + state/evaluator/global-task counts. Read-only.

        asset_path: StateTree asset path, e.g.
                    '/Game/Variant_Combat/Blueprints/AI/ST_CombatEnemy.ST_CombatEnemy'.

        Returns: editor_data_class; schema {class, ai_controller_class/context_actor_class if set};
        root_states[] + root_state_count + total_state_count; evaluators[] and global_tasks[] (node
        type names) with counts.

        NOTE (reported in 'reflection_note'): schema + graph are read from the UStateTreeEditorData
        subobject (the authorable source). The compiled UStateTree arrays are protected C++ UPROPERTYs
        and are not value-read. Use get_state_tree_states for the full hierarchy."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_state_tree_states — recursive authorable state hierarchy         #
    # ------------------------------------------------------------------ #
    _STATES_BODY = _ST_HELPERS + r'''
path = PARAMS.get("asset_path")
max_depth = int(PARAMS.get("max_depth") or 32)
max_states = int(PARAMS.get("max_states") or 2000)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a StateTree (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    ed = _find_editor_data(obj)
    if ed is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not reach UStateTreeEditorData for %s (authorable graph unreachable)" % path}))
    else:
        counter = [0]
        truncated = [False]
        def _state(s, depth):
            if counter[0] >= max_states:
                truncated[0] = True
                return None
            counter[0] += 1
            rec = {"name": str(s.get_editor_property("name")),
                   "type": _enum_name(_try(lambda: s.get_editor_property("type"))),
                   "selection_behavior": _enum_name(_try(lambda: s.get_editor_property("selection_behavior")))}
            w = _try(lambda: s.get_editor_property("weight"))
            if w is not None:
                rec["weight"] = w
            tt = _task_types(s)
            if tt:
                rec["tasks"] = tt
            ec = _try(lambda: s.get_editor_property("enter_conditions"))
            if _is_obj(ec) and len(ec) > 0:
                rec["enter_condition_count"] = len(ec)
            trs = _try(lambda: s.get_editor_property("transitions"))
            if _is_obj(trs) and len(trs) > 0:
                rec["transitions"] = [{"trigger": _enum_name(_try(lambda t=t: t.get_editor_property("trigger"))),
                                       "target": _link_desc(t.get_editor_property("state"))
                                                 if _is_obj(_try(lambda t=t: t.get_editor_property("state"))) else None}
                                      for t in trs]
            # Linked subtree / linked asset (for Linked / LinkedAsset state types)
            la = _try(lambda: s.get_editor_property("linked_asset"))
            if _is_obj(la):
                rec["linked_asset"] = la.get_path_name()
            kids = _try(lambda: s.get_editor_property("children")) or []
            if depth < max_depth and len(kids) > 0:
                ch = []
                for c in kids:
                    cr = _state(c, depth + 1)
                    if cr is not None:
                        ch.append(cr)
                if ch:
                    rec["children"] = ch
            elif len(kids) > 0:
                rec["children_omitted"] = len(kids)
            return rec
        subs = _try(lambda: ed.get_editor_property("sub_trees")) or []
        roots = []
        for s in subs:
            r = _state(s, 0)
            if r is not None:
                roots.append(r)
        result = {"status": "success", "path": obj.get_path_name(),
                  "state_count": counter[0], "truncated": truncated[0], "states": roots}
        result["note"] = (
            "Authorable state hierarchy from UStateTreeEditorData.sub_trees (UStateTreeState UObjects, "
            "value-readable). Per state: name, type (State/Group/Linked/LinkedAsset/Subtree), "
            "selection_behavior, weight, task node type names, transition summaries (trigger + target "
            "state link). Use get_state_tree_state(asset_path, state_name) for full per-node config "
            "(tasks/enter-conditions/considerations/transitions with values).")
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_state_tree_states(ctx, asset_path: str, max_depth: int = 32,
                              max_states: int = 2000) -> str:
        """Full authorable state hierarchy of a StateTree (recursive). Read-only.

        asset_path: StateTree asset path (e.g. '.../ST_CombatEnemy.ST_CombatEnemy').
        max_depth:  cap recursion depth (default 32).
        max_states: cap total states emitted (default 2000; 'truncated' flags if hit).

        Returns nested states[]; each: {name, type, selection_behavior, weight, tasks:[node type names],
        enter_condition_count, transitions:[{trigger, target:{link_type, name}}], linked_asset?,
        children:[...]}. This reads UStateTreeEditorData.sub_trees (the authorable graph); node types
        are real (parsed from each node's FInstancedStruct export_text). For a single state's full
        per-node config values, use get_state_tree_state."""
        params = {"asset_path": asset_path, "max_depth": max_depth, "max_states": max_states}
        try:
            return json.dumps(_exec(_STATES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_state_tree_state — one state, full per-node detail               #
    # ------------------------------------------------------------------ #
    _STATE_BODY = _ST_HELPERS + r'''
path = PARAMS.get("asset_path")
want_name = (PARAMS.get("state_name") or "").strip().lower()
want_path = PARAMS.get("state_path")
cfg_limit = int(PARAMS.get("config_limit") or 600)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a StateTree (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    ed = _find_editor_data(obj)
    if ed is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not reach UStateTreeEditorData"}))
    else:
        matches = []
        def _walk(s):
            nm = str(s.get_editor_property("name"))
            if want_path is not None:
                if s.get_path_name() == want_path:
                    matches.append(s)
            elif nm.lower() == want_name:
                matches.append(s)
            for c in (_try(lambda: s.get_editor_property("children")) or []):
                _walk(c)
        for s in (_try(lambda: ed.get_editor_property("sub_trees")) or []):
            _walk(s)
        if not matches:
            # List available names to help the caller.
            names = []
            def _names(s):
                names.append(str(s.get_editor_property("name")))
                for c in (_try(lambda: s.get_editor_property("children")) or []):
                    _names(c)
            for s in (_try(lambda: ed.get_editor_property("sub_trees")) or []):
                _names(s)
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "no state matched name=%r path=%r" % (PARAMS.get("state_name"), want_path),
                "available_states": names}))
        else:
            s = matches[0]
            info = {"status": "success", "path": s.get_path_name(),
                    "name": str(s.get_editor_property("name")),
                    "type": _enum_name(_try(lambda: s.get_editor_property("type"))),
                    "selection_behavior": _enum_name(_try(lambda: s.get_editor_property("selection_behavior")))}
            if len(matches) > 1:
                info["ambiguous_note"] = "%d states share this name; returning the first (use state_path to disambiguate)." % len(matches)
            w = _try(lambda: s.get_editor_property("weight"))
            if w is not None:
                info["weight"] = w
            desc = _try(lambda: s.get_editor_property("description"))
            if desc:
                info["description"] = str(desc)
            en_arr = _try(lambda: s.get_editor_property("enter_conditions"))
            info["enter_conditions"] = [_node_desc(x, True, cfg_limit) for x in en_arr] if _is_obj(en_arr) else []
            tk_arr = _try(lambda: s.get_editor_property("tasks"))
            info["tasks"] = [_node_desc(x, True, cfg_limit) for x in tk_arr] if _is_obj(tk_arr) else []
            co_arr = _try(lambda: s.get_editor_property("considerations"))
            info["considerations"] = [_node_desc(x, True, cfg_limit) for x in co_arr] if _is_obj(co_arr) else []
            tr_arr = _try(lambda: s.get_editor_property("transitions"))
            info["transitions"] = [_trans_desc(x, True, cfg_limit) for x in tr_arr] if _is_obj(tr_arr) else []
            la = _try(lambda: s.get_editor_property("linked_asset"))
            if _is_obj(la):
                info["linked_asset"] = la.get_path_name()
            kids = _try(lambda: s.get_editor_property("children")) or []
            info["child_state_names"] = [str(c.get_editor_property("name")) for c in kids]
            info["note"] = (
                "Full per-node detail for one authorable state. Each task/condition/consideration is "
                "{type, source(native|blueprint), type_path?, config?}; config is the node's "
                "FInstancedStruct export_text (concrete configured VALUES, truncated to config_limit). "
                "Transitions include trigger, priority, target state link, and gating conditions.")
            print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_state_tree_state(ctx, asset_path: str, state_name: str = None,
                             state_path: str = None, config_limit: int = 600) -> str:
        """Full detail for ONE StateTree state: tasks/conditions/considerations/transitions. Read-only.

        asset_path:   StateTree asset path.
        state_name:   state Name to find (case-insensitive; first match). If several share a name, use
                      state_path to disambiguate (an error lists available_states when nothing matches).
        state_path:   exact UStateTreeState object path (overrides state_name).
        config_limit: max chars of each node's configured-value export text (default 600).

        Returns the state's enter_conditions[], tasks[], considerations[], transitions[] (each node as
        {type, source, type_path?, config?} where config = concrete configured VALUES from the node's
        FInstancedStruct export_text), plus child_state_names and linked_asset if any. These are REAL
        configured values (not faked)."""
        if not state_name and not state_path:
            return "Error: provide state_name or state_path."
        params = {"asset_path": asset_path, "state_name": state_name,
                  "state_path": state_path, "config_limit": config_limit}
        try:
            return json.dumps(_exec(_STATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_state_tree_schemas — authorable UStateTreeSchema classes        #
    # ------------------------------------------------------------------ #
    _SCHEMAS_BODY = _ST_HELPERS + r'''
package_path = PARAMS.get("path") or "/Game"
include_blueprint = bool(PARAMS.get("include_blueprint", True))
base = getattr(unreal, "StateTreeSchema", None)
classes = []
seen = set()
if base is not None:
    for nm in dir(unreal):
        cand = getattr(unreal, nm, None)
        if inspect.isclass(cand) and issubclass(cand, unreal.Object) and cand is not base and issubclass(cand, base):
            if nm in seen:
                continue
            seen.add(nm)
            cm = _class_meta(cand)
            classes.append({"name": nm, "class_path": "unreal." + nm,
                            "parent_class": cm.get("parent_class"),
                            "is_abstract": cm.get("is_abstract"),
                            "is_blueprintable": cm.get("is_blueprintable")})
classes.sort(key=lambda r: r["name"].lower())
bp_rows = []
if include_blueprint:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        flt = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
            recursive_paths=True, package_paths=[package_path])
        for a in (ar.get_assets(flt) or []):
            parent = str(a.get_tag_value("ParentClass") or a.get_tag_value("NativeParentClass") or "")
            if "StateTreeSchema" in parent:
                pkg = str(a.get_editor_property("package_name"))
                nm = str(a.get_editor_property("asset_name"))
                bp_rows.append({"name": nm, "path": pkg + "." + nm, "parent_class": parent})
    except Exception:
        pass
bp_rows.sort(key=lambda r: r["path"].lower())
result = {"status": "success", "base_class": "StateTreeSchema",
          "class_count": len(classes), "classes": classes,
          "blueprint_class_count": len(bp_rows), "blueprint_classes": bp_rows}
result["note"] = (
    "Authorable StateTree schema classes. 'classes' = unreal.*-namespace UStateTreeSchema subclasses "
    "(e.g. StateTreeComponentSchema, StateTreeAIComponentSchema, BrainComponentStateTreeSchema, "
    "CameraDirectorStateTreeSchema); the schema determines which contexts/nodes a tree may use. "
    "'blueprint_classes' = project Blueprint schema subclasses. A fully exhaustive native subclass "
    "walk would need a C++ GetDerivedClasses handler (not Python-exposed).")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_state_tree_schemas(ctx, path: str = None, include_blueprint: bool = True) -> str:
        """List authorable UStateTreeSchema classes (namespace + project Blueprint). Read-only.

        path:              content root scanned for Blueprint schema subclasses (default '/Game').
        include_blueprint: include project Blueprint StateTreeSchema subclasses (default True).

        Returns 'classes' (unreal.*-namespace schema subclasses, each with parent_class / is_abstract /
        is_blueprintable) and 'blueprint_classes'. The schema fixes which contexts and node types a
        StateTree may use (e.g. StateTreeAIComponentSchema for AI controllers). See 'note' for scope."""
        params = {"path": path, "include_blueprint": include_blueprint}
        try:
            return json.dumps(_exec(_SCHEMAS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_state_tree_node_classes — authorable BP node classes            #
    # ------------------------------------------------------------------ #
    _NODES_BODY = _ST_HELPERS + r'''
kind = PARAMS.get("kind") or "all"
node_bases = PARAMS.get("node_bases") or {}
package_path = PARAMS.get("path") or "/Game"
include_blueprint = bool(PARAMS.get("include_blueprint", True))
kinds = list(node_bases.keys()) if kind == "all" else [kind]
out_groups = {}
ar = None
if include_blueprint:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
for k in kinds:
    base_name = node_bases.get(k)
    if base_name is None:
        out_groups[k] = {"error": "unknown kind"}
        continue
    base = getattr(unreal, base_name, None)
    classes = []
    seen = set()
    if base is not None:
        cm0 = _class_meta(base)
        classes.append({"name": base_name, "class_path": "unreal." + base_name,
                        "is_base": True, "is_abstract": cm0.get("is_abstract"),
                        "is_blueprintable": cm0.get("is_blueprintable")})
        seen.add(base_name)
        for nm in dir(unreal):
            cand = getattr(unreal, nm, None)
            if inspect.isclass(cand) and issubclass(cand, unreal.Object) and cand is not base and issubclass(cand, base):
                if nm in seen:
                    continue
                seen.add(nm)
                cm = _class_meta(cand)
                classes.append({"name": nm, "class_path": "unreal." + nm,
                                "parent_class": cm.get("parent_class"),
                                "is_abstract": cm.get("is_abstract"),
                                "is_blueprintable": cm.get("is_blueprintable")})
    classes.sort(key=lambda r: (not r.get("is_base", False), r["name"].lower()))
    bp_rows = []
    if include_blueprint and ar is not None:
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
    bp_rows.sort(key=lambda r: r["path"].lower())
    out_groups[k] = {"base_class": base_name, "class_count": len(classes), "classes": classes,
                     "blueprint_class_count": len(bp_rows), "blueprint_classes": bp_rows}
result = {"status": "success", "kind": kind, "groups": out_groups}
result["note"] = (
    "Authorable StateTree NODE classes, grouped by kind (task/evaluator/condition/consideration). "
    "'classes' = the Blueprint node BASE class + its unreal.*-namespace subclasses; 'blueprint_classes' "
    "= project Blueprint node subclasses. IMPORTANT: the NATIVE engine node types (e.g. "
    "StateTreeMoveToTask, StateTreeRunEnvQueryTask, StateTreeDelayTask, condition types) are C++ "
    "FInstancedStruct types (F...Base structs), NOT UClasses, so they are NOT enumerable via UClass "
    "reflection and do NOT appear here; they are surfaced only when actually USED in an asset (via "
    "get_state_tree_states / get_state_tree_state, parsed from export_text). An exhaustive native "
    "node-type registry listing would require a C++ handler (DEFERRED-C++).")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_state_tree_node_classes(ctx, kind: str = "all", path: str = None,
                                     include_blueprint: bool = True) -> str:
        """List authorable StateTree node classes (task/evaluator/condition/consideration). Read-only.

        kind:              'task' | 'evaluator' | 'condition' | 'consideration' | 'all' (default 'all').
        path:              content root scanned for Blueprint node subclasses (default '/Game').
        include_blueprint: include project Blueprint node subclasses (default True).

        Returns groups{kind: {base_class, classes:[Blueprint base + namespace subclasses],
        blueprint_classes:[project BP node subclasses]}}.

        IMPORTANT (in 'note', not faked): the NATIVE engine node types (StateTreeMoveToTask,
        StateTreeRunEnvQueryTask, StateTreeDelayTask, ...) are C++ FInstancedStruct structs, NOT
        UClasses, so they are not enumerable via UClass reflection and are NOT listed here; they are
        surfaced when USED in an asset via get_state_tree_states/get_state_tree_state. This tool lists
        the authorable Blueprint node class set."""
        params = {"kind": kind, "node_bases": _NODE_BASES, "path": path,
                  "include_blueprint": include_blueprint}
        try:
            return json.dumps(_exec(_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- list_state_tree_native_nodes (C++ #12: native FInstancedStruct node registry) ----
    _NATIVE_NODES_BODY = _ST_HELPERS + r'''
category = PARAMS.get("category") or "all"
m = _refl()
fn = getattr(m, "get_state_tree_node_registry_json", None) if m is not None else None
if fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary.get_state_tree_node_registry_json unavailable — reload the MCP server after the C++ #12 rebuild. list_state_tree_node_classes still lists the Blueprint node set."}))
else:
    try:
        data = json.loads(fn(category))
        data["status"] = "error" if "error" in data else "success"
    except Exception as _e:
        data = {"status": "error", "message": str(_e)}
    print("@@UMCP@@" + json.dumps(data))
'''

    @mcp.tool()
    def list_state_tree_native_nodes(ctx, category: str = "all") -> str:
        """List NATIVE StateTree node types — C++ FInstancedStruct UScriptStructs (C++ #12 reader).
        Read-only.

        category: 'all' | 'tasks' | 'evaluators' | 'conditions' | 'considerations'.

        Complements list_state_tree_node_classes (which lists the authorable Blueprint node set). This
        lists the native struct node types (StateTreeMoveToTask, StateTreeRunEnvQueryTask, project C++
        nodes, ...) that UClass reflection cannot enumerate — selected by IsChildOf the four base node
        structs, spanning StateTree/GameplayStateTree/project modules. Per entry {struct_path, cpp_name,
        display_name, module}. Requires the C++ #12 plugin build; falls back with a clear message."""
        params = {"category": category}
        try:
            return json.dumps(_exec(_NATIVE_NODES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- get_state_tree_bindings (C++ #14: editor property-bindings resolver) ----
    _BINDINGS_BODY = _ST_HELPERS + r'''
path = PARAMS.get("asset_path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.StateTree):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a StateTree (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    m = _refl()
    fn = getattr(m, "get_state_tree_bindings_json", None) if m is not None else None
    if fn is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.get_state_tree_bindings_json unavailable — reload the MCP server after the C++ #14 rebuild"}))
    else:
        try:
            data = json.loads(fn(obj))
            data["status"] = "error" if "error" in data else "success"
        except Exception as _e:
            data = {"status": "error", "message": str(_e)}
        print("@@UMCP@@" + json.dumps(data))
'''

    @mcp.tool()
    def get_state_tree_bindings(ctx, asset_path: str) -> str:
        """Resolve a StateTree's editor property bindings (source->target), C++ #14 reader. Read-only.

        asset_path: StateTree asset path (e.g. '.../ST_CombatEnemy.ST_CombatEnemy').

        Returns bindings[] each {source_struct, source_property, target_struct, target_property,
        source_struct_id, target_struct_id}. The *_struct labels resolve the FGuid StructID to the owning
        node/state (e.g. "State 'Dead' EnterCond FStateTreeCompareFloatCondition"); a GUID that belongs to
        a global parameter falls back to the raw *_struct_id. Reads UStateTreeEditorData.EditorBindings,
        which stock Python cannot access (protected). Requires the C++ #14 plugin build; falls back with a
        clear message otherwise."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_BINDINGS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
