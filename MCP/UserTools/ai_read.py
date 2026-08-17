"""UserTools :: AI Assets (READ)  (spec: docs/spec/ai.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset editors are ever opened (no modals); nothing is mutated; no ledger. This introspects
AI navigation/behavior assets: BehaviorTree, BlackboardData, EnvQuery, and AIController Blueprints.
Query convention + base64 PARAMS + Output-Log auto-capture are copied verbatim from editor_level.py
(the gold standard), matching the animation_read.py AssetRegistry-enumeration style.

What IS reachable from stock Python in this build (verified live vs TestMCPSetup, UE 5.8.1):
  * AssetRegistry enumerates BehaviorTree / BlackboardData / EnvQuery via
    class_paths=[TopLevelAssetPath("/Script/AIModule", <Kind>)] (these classes live in
    /Script/AIModule, NOT /Script/Engine). AIController subclasses are Blueprint assets, found by
    scanning Blueprint assets whose ParentClass/NativeParentClass tag contains "AIController".
    (5.8 removed ARFilter.class_names; asset path = package_name + "." + asset_name.)
  * BlackboardData (no getset descriptors, but readable via get_editor_property):
    get_editor_property("keys") -> Array[BlackboardEntry]; each entry exposes entry_name (Name),
    key_type (a UBlackboardKeyType subobject whose CLASS NAME gives the type, e.g.
    BlackboardKeyType_Bool/Int/Float/Vector/Object/Enum/Name/String/Class/Rotator),
    entry_category, entry_description. get_editor_property("parent") -> parent BlackboardData or None.
  * BehaviorTree (getset descriptors blackboard_asset, root_node, root_decorators): the FULL node
    graph is reachable via reflection (unlike Blueprint event graphs). root_node is a BTCompositeNode
    with children (Array[BTCompositeChild]), services (Array[BTService]) and node_name. Each
    BTCompositeChild has child_composite (recurse), child_task (leaf BTTaskNode), and decorators
    (Array[BTDecorator]). Node class + display node_name are read per node.

Known limits (reported honestly in payloads, NOT hidden):
  * This project (Third Person template + variants) has ZERO BehaviorTree and ZERO BlackboardData
    assets anywhere (/Game, /Engine, plugins all scanned -> 0). get_blackboard_info and
    get_behavior_tree_info are therefore EXERCISED only via their enumerator + not-found/wrong-type
    error paths; their extraction bodies are code-complete against the reflected API (probed live on
    the class CDOs + struct types) but NOT positively demoed on a real asset (same honest posture
    sequencer_read used before a LevelSequence existed). No asset was created (asset creation risks a
    factory modal against the shared editor).
  * EnvQuery assets exist (3 in this project) and enumerate fine, but the EnvQuery graph
    (generator/tests/options) exposes NO getset descriptors and no reflected properties in 5.8 Python
    -> only listed (path/name), not deep-introspected. A dedicated EnvQuery graph reader would need a
    C++ MCPReflectionLibrary handler; not attempted here.
  * BlackboardKeyType subclasses (Bool/Int/...) are NOT present in the unreal.* Python namespace
    (only the base BlackboardKeyType), so a key's type is derived from key_type.get_class().get_name()
    with the "BlackboardKeyType_" prefix stripped.

Implemented (all read-only):
  - list_ai_assets          (AssetRegistry enumerate BehaviorTree/BlackboardData/EnvQuery + AIController BPs)
  - get_blackboard_info     (BlackboardData: keys name+type, parent) [code-complete; 0 assets to demo]
  - get_behavior_tree_info  (BehaviorTree: blackboard, root, recursive node tree) [code-complete; 0 assets to demo]
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

# Kinds this module enumerates. BehaviorTree/BlackboardData/EnvQuery are in /Script/AIModule;
# AIController subclasses are Blueprint assets (kind handled specially by tag scan).
_AI_KINDS = ["BehaviorTree", "BlackboardData", "EnvQuery", "AIController"]


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
    _AI_HELPERS = r'''
import unreal, json, warnings
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
'''

    # ------------------------------------------------------------------ #
    # list_ai_assets — enumerate AI asset kinds via AssetRegistry         #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _AI_HELPERS + r'''
kinds_all = ["BehaviorTree", "BlackboardData", "EnvQuery", "AIController"]
# BehaviorTree/BlackboardData/EnvQuery are in /Script/AIModule; AIController = Blueprint scan.
module_of = {"BehaviorTree": "/Script/AIModule", "BlackboardData": "/Script/AIModule",
             "EnvQuery": "/Script/AIModule"}
req_kind = PARAMS.get("kind")
package_path = PARAMS.get("path") or "/Game"
max_results = PARAMS.get("max_results")
max_results = int(max_results) if max_results else 200
kinds = kinds_all
if req_kind:
    match = None
    for k in kinds_all:
        if k.lower() == str(req_kind).lower():
            match = k; break
    if match is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown kind '%s'; valid: %s" % (req_kind, ", ".join(kinds_all))}))
        kinds = None
    else:
        kinds = [match]
if kinds is not None:
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    result = {"status": "success", "path": package_path, "kind": (kinds[0] if req_kind else None),
              "by_kind": {}, "total": 0}
    grand = 0
    for k in kinds:
        rows = []
        err = None
        if k == "AIController":
            # Blueprint assets whose parent (native or BP) class name mentions AIController.
            try:
                flt = unreal.ARFilter(
                    class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
                    recursive_paths=True, package_paths=[package_path])
                for a in (ar.get_assets(flt) or []):
                    parent = a.get_tag_value("ParentClass") or a.get_tag_value("NativeParentClass")
                    if parent and "AIController" in str(parent):
                        pkg = str(a.get_editor_property("package_name"))
                        nm = str(a.get_editor_property("asset_name"))
                        rows.append({"name": nm, "path": pkg + "." + nm, "parent_class": str(parent)})
            except Exception as e:
                err = str(e)
        else:
            try:
                flt = unreal.ARFilter(
                    class_paths=[unreal.TopLevelAssetPath(module_of[k], k)],
                    recursive_paths=True, package_paths=[package_path])
                for a in (ar.get_assets(flt) or []):
                    pkg = str(a.get_editor_property("package_name"))
                    nm = str(a.get_editor_property("asset_name"))
                    rows.append({"name": nm, "path": pkg + "." + nm})
            except Exception as e:
                err = str(e)
        if err is not None:
            result["by_kind"][k] = {"count": 0, "error": err, "assets": []}
            continue
        rows.sort(key=lambda r: r["path"].lower())
        grand += len(rows)
        result["by_kind"][k] = {"count": len(rows),
                                "returned": min(len(rows), max_results),
                                "assets": rows[:max_results]}
    result["total"] = grand
    result["note"] = ("BehaviorTree/BlackboardData/EnvQuery enumerated via AssetRegistry "
                      "class_paths=TopLevelAssetPath('/Script/AIModule', <Kind>) (these are AIModule "
                      "classes, NOT /Script/Engine). AIController = Blueprint assets whose "
                      "Parent/NativeParentClass tag mentions AIController (native C++ AIController "
                      "classes without a Blueprint are not asset-enumerable). 'count' is the true "
                      "total per kind under 'path'; list capped at max_results.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_ai_assets(ctx, path: str = None, kind: str = None,
                       max_results: int = 200) -> str:
        """Enumerate AI assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Game/Variant_Combat'.
        kind:        restrict to ONE of: BehaviorTree, BlackboardData, EnvQuery, AIController
                     (case-insensitive). Omit to enumerate all four.
        max_results: cap the assets listed PER KIND (default 200); each kind's true 'count'
                     is always reported even if the list is capped.

        Returns per-kind {count, returned, assets:[{name, path[, parent_class]}]} plus a grand
        'total'. BehaviorTree/BlackboardData/EnvQuery are AIModule classes (/Script/AIModule);
        AIController is resolved as Blueprint assets whose parent class mentions AIController (native
        AIController C++ classes without a Blueprint are not asset-enumerable)."""
        params = {"path": path, "kind": kind, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_blackboard_info — keys (name + type) + parent blackboard         #
    # ------------------------------------------------------------------ #
    _BB_BODY = _AI_HELPERS + r'''
def _key_type_name(kt):
    # Derive the key type (Bool/Int/Float/Vector/Object/Enum/Name/String/Class/Rotator/...) from the
    # UBlackboardKeyType subobject's class name (subclasses are not in the unreal.* Python namespace).
    if kt is None:
        return None, None
    cls = _try(lambda: kt.get_class().get_name())
    short = cls
    if cls and cls.startswith("BlackboardKeyType_"):
        short = cls[len("BlackboardKeyType_"):]
    return short, cls
path = PARAMS.get("path")
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.BlackboardData):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a BlackboardData (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name()}
    parent = _try(lambda: obj.get_editor_property("parent"))
    info["parent_blackboard"] = _try(lambda: parent.get_path_name()) if parent else None
    keys = _try(lambda: obj.get_editor_property("keys"), []) or []
    rows = []
    for e in keys:
        short, cls = _key_type_name(_try(lambda: e.get_editor_property("key_type")))
        rec = {"name": str(_try(lambda: e.get_editor_property("entry_name"))),
               "type": short, "key_type_class": cls}
        cat = _try(lambda: e.get_editor_property("entry_category"))
        if cat is not None and str(cat) != "":
            rec["category"] = str(cat)
        desc = _try(lambda: e.get_editor_property("entry_description"))
        if desc is not None and str(desc) != "":
            rec["description"] = str(desc)
        rows.append(rec)
    info["key_count"] = len(rows)
    info["keys"] = rows
    info["note"] = ("Keys read from BlackboardData.get_editor_property('keys') (Array[BlackboardEntry]); "
                    "each key's type derives from key_type.get_class().get_name() minus the "
                    "'BlackboardKeyType_' prefix (Bool/Int/Float/Vector/Object/Enum/Name/String/Class/"
                    "Rotator). Inherited parent keys are on the parent_blackboard (follow that path to "
                    "list them).")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_blackboard_info(ctx, path: str) -> str:
        """Keys + parent for a BlackboardData asset (loads it; no editor opened). Read-only.

        path: BlackboardData asset path, e.g. '/Game/AI/BB_Enemy.BB_Enemy'.

        Returns: parent_blackboard (path or null), key_count, and keys: a list of
        {name, type, key_type_class[, category, description]} where 'type' is the blackboard key
        type (Bool/Int/Float/Vector/Object/Enum/Name/String/Class/Rotator, derived from the key's
        UBlackboardKeyType subobject class). Inherited keys live on the parent_blackboard (follow that
        path). Errors if the asset is missing or not a BlackboardData.

        NOTE: this project contains ZERO BlackboardData assets, so this command's extraction is
        code-complete (verified against the reflected BlackboardData/BlackboardEntry API) but has not
        been positively demoed on a real asset here; only its not-found/wrong-type paths were live."""
        try:
            return json.dumps(_exec(_BB_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_behavior_tree_info — blackboard, root, recursive node tree       #
    # ------------------------------------------------------------------ #
    _BT_BODY = _AI_HELPERS + r'''
path = PARAMS.get("path")
max_depth = PARAMS.get("max_depth")
max_depth = int(max_depth) if max_depth else 25
def _aux(n):
    return {"class": _try(lambda: n.get_class().get_name()),
            "name": str(_try(lambda: n.get_editor_property("node_name")))}
def _task(t):
    return {"kind": "Task", "class": _try(lambda: t.get_class().get_name()),
            "name": str(_try(lambda: t.get_editor_property("node_name")))}
def _composite(c, depth):
    node = {"kind": "Composite", "class": _try(lambda: c.get_class().get_name()),
            "name": str(_try(lambda: c.get_editor_property("node_name")))}
    services = _try(lambda: c.get_editor_property("services"), []) or []
    node["services"] = [_aux(s) for s in services]
    kids = []
    if depth > 0:
        children = _try(lambda: c.get_editor_property("children"), []) or []
        for ch in children:
            decs = _try(lambda: ch.get_editor_property("decorators"), []) or []
            entry = {"decorators": [_aux(d) for d in decs]}
            comp = _try(lambda: ch.get_editor_property("child_composite"))
            tsk = _try(lambda: ch.get_editor_property("child_task"))
            if comp is not None:
                entry["child"] = _composite(comp, depth - 1)
            elif tsk is not None:
                entry["child"] = _task(tsk)
            else:
                entry["child"] = None
            kids.append(entry)
    else:
        node["children_truncated"] = True
    node["children"] = kids
    return node
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.BehaviorTree):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a BehaviorTree (got %s): %s" % (obj.get_class().get_name(), path)}))
else:
    info = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name()}
    bb = _try(lambda: obj.get_editor_property("blackboard_asset"))
    info["blackboard_asset"] = _try(lambda: bb.get_path_name()) if bb else None
    root_decs = _try(lambda: obj.get_editor_property("root_decorators"), []) or []
    info["root_decorators"] = [_aux(d) for d in root_decs]
    root = _try(lambda: obj.get_editor_property("root_node"))
    if root is None:
        info["root_node"] = None
        info["tree_note"] = "BehaviorTree has no root_node set (empty tree)."
    else:
        info["root_node"] = _composite(root, max_depth)
    info["note"] = ("Node graph read via reflection: BehaviorTree.root_node (BTCompositeNode) -> "
                    "children (Array[BTCompositeChild] each with child_composite | child_task + "
                    "decorators) + services; node class = get_class().get_name(), label = node_name. "
                    "Node kinds: Composite (BTComposite_Selector/Sequence/SimpleParallel), Task "
                    "(BTTask_*), Decorator (BTDecorator_*), Service (BTService_*). Per-node UPROPERTY "
                    "config (e.g. a task's blackboard key, a decorator's condition) is not expanded "
                    "here; use editor_level.get_object_property / objects.describe_object on the node "
                    "path if needed.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_behavior_tree_info(ctx, path: str, max_depth: int = 25) -> str:
        """Blackboard, root, and full node tree for a BehaviorTree (loads it; no editor). Read-only.

        path:      BehaviorTree asset path, e.g. '/Game/AI/BT_Enemy.BT_Enemy'.
        max_depth: max composite-node recursion depth (default 25; guards against huge trees).

        Returns: blackboard_asset (path or null), root_decorators, and root_node -- a recursive
        node tree. Each node has {kind, class, name}; Composite nodes also have services[] and
        children[] where each child is {decorators:[...], child:<node|null>} (child is a nested
        Composite or a leaf Task). Node 'kind' is Composite / Task / Decorator / Service and 'class'
        is the concrete engine class (e.g. BTComposite_Selector, BTTask_MoveTo, BTDecorator_Blackboard).
        Errors if the asset is missing or not a BehaviorTree.

        Unlike Blueprint event graphs, the BT node graph IS reachable via reflection. NOTE: this
        project contains ZERO BehaviorTree assets, so the extraction is code-complete (verified against
        the reflected BehaviorTree/BTCompositeNode/BTCompositeChild API) but not positively demoed on a
        real asset here; only its not-found/wrong-type paths were live-validated. Per-node UPROPERTY
        config is not expanded (use get_object_property on a node path)."""
        params = {"path": path, "max_depth": max_depth}
        try:
            return json.dumps(_exec(_BT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
