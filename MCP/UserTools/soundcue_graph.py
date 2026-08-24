"""UserTools :: Sound / SoundCue graph authoring + reads  (spec: docs/spec/audio.md, group F)

Wave 1 of the AUDIO buildout (UE 5.8, Windows-native). PURE PYTHON over the reflected
`unreal.*` API -- NO C++, NO build. This module authors and inspects the classic SoundCue
runtime node graph (first_node -> child_nodes tree of USoundNode subobjects), extending the
create_* / add_wave_player_to_cue tools already in sound_write.py.

Conventions are copied VERBATIM from the gold-standard editor_level.py / sound_write.py:
snippets print @@UMCP@@<json> on ONE line; params are injected as base64 JSON via _exec;
_query wraps every snippet with Output-Log auto-capture (new Warning/Error lines surface as
result["_log_warnings"]); the per-session undo ledger lives in builtins._UMCP_LEDGERS keyed by
PARAMS["_session"]. Snippet bodies contain NO triple-single-quotes and NO backslashes, and never
clobber the C++ wrapper's reserved locals (sys/unreal/traceback/output_file/error_file/
original_stdout/original_stderr/success/user_code/code_obj).

Tools (F group):
  READS (no ledger):
   * list_sound_cue_node_types  -- reflectable unreal.SoundNode subclasses (PCG-style dir+issubclass).
   * get_sound_cue_graph        -- first_node -> child_nodes tree + a flat node table (name/class/children).
   * get_sound_cue_node         -- one node's reflected props (getset + native FProperty names).
   * validate_sound_cue         -- walk the tree; flag missing root, empty child slots, wave-less players.
  WRITES (ledgered):
   * add_sound_cue_node         -- new_object(<SoundNode cls>, cue); optional root / parent-slot placement.
   * connect_sound_cue_nodes    -- wire child into parent.child_nodes[index] (append or replace).
   * remove_sound_cue_node      -- detach a node from first_node and every parent slot.
   * set_sound_cue_node_properties -- set scalar/enum/object props on a node, with faithful prior capture.
   * build_sound_cue_graph      -- whole-tree build in ONE call (create cue + nodes + connections + root).

Node identity: a SoundCue node is a UObject subobject of the cue package. Its stable identifier is
its subobject NAME (e.g. "SoundNodeWavePlayer_0"); tools accept either the bare name or the full
object path "<cue>.<cue>:<Name>". Resolution is unreal.load_object(None, "<cuepath>:<name>") with a
first_node-tree walk fallback. Disconnected ("floating") nodes DO persist in the saved package
(verified live), so an add -> connect flow across calls works.

Serialization limits honestly noted:
  * USoundCue::AllNodes and the editor UEdGraph (visual layout/wiring metadata) are editor-only and
    NOT reflected to Python; there is no "enumerate every node" API, so get_sound_cue_graph reports
    the reachable runtime tree (the semantically meaningful graph), and validate reasons over it.
  * USoundNode::GetMaxChildNodes()/GetMinChildNodes() are NOT Python-reflected, so child-slot COUNT
    limits are not enforced here (the engine validates on cue compile); we set the child_nodes array
    as given.

Reversibility: this module registers NO `undo` tool (editor_level.py owns the unified `undo`).
  * add_sound_cue_node / connect / remove / set-props record NEW per-op ledger entries whose exact
    op-name + fields + inverse are documented on each tool's docstring (and in the build report) for
    the coordinator to fold into editor_level.undo. Each write RETURNS its recorded ledger_entry so
    the inverse can be verified without guessing.
  * build_sound_cue_graph CREATES the cue and reuses the generic already-folded "create_asset"
    inverse {asset_path, package_path, created_dir}; NO new op.

Scratch discipline: author test cues under /Game/MCP_Scratch with an MCP_ prefix; soft-delete by
rename to /Game/_MCP_Trash, never delete_asset (the coordinator's create_asset undo handles deletes).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers. No ''' / no backslashes in this block.  #
    # ------------------------------------------------------------------ #
    _HELPERS = r'''
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
def _load_cue(path):
    if not path:
        return None, "no cue path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.SoundCue):
        return None, "asset is not a SoundCue (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _nname(n):
    try:
        return n.get_name()
    except Exception:
        return None
def _children(n):
    try:
        return list(n.get_editor_property("child_nodes") or [])
    except Exception:
        return []
def _resolve_node(cue, ident):
    if not ident or cue is None:
        return None
    cands = []
    if ":" in str(ident):
        cands.append(str(ident))
    else:
        cands.append(cue.get_path_name() + ":" + str(ident))
    for pth in cands:
        try:
            o = unreal.load_object(None, pth)
            if isinstance(o, unreal.SoundNode):
                return o
        except Exception:
            pass
    target = {"n": None}
    seen = set()
    def w(n):
        if n is None or target["n"] is not None:
            return
        nm = _nname(n)
        if nm in seen:
            return
        seen.add(nm)
        if nm == str(ident):
            target["n"] = n
            return
        for c in _children(n):
            w(c)
    w(cue.get_editor_property("first_node"))
    return target["n"]
def _write_children(node, seq):
    arr = unreal.Array(unreal.SoundNode)
    for x in seq:
        try:
            arr.append(x)
        except Exception:
            pass
    node.set_editor_property("child_nodes", arr)
def _child_names(node):
    out = []
    for c in _children(node):
        out.append(_nname(c) if c is not None else None)
    return out
def _enum_short(v):
    if v is None:
        return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _objref(o):
    if o is None:
        return None
    try:
        return {"__object__": o.get_path_name(), "class": o.get_class().get_name()}
    except Exception:
        return str(o)
def _getset_names(obj):
    names, seen = [], set()
    for klass in type(obj).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
            seen.add(name)
    return names
def _ser_val(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, unreal.EnumBase):
        return _enum_short(v)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Object):
        return _objref(v)
    if isinstance(v, unreal.Array):
        return [_ser_val(e) for e in list(v)[:16]]
    try:
        return str(v)
    except Exception:
        return None
_NODE_SKIP = set(["child_nodes", "graph_node", "sound_cue"])
_HAS_META = hasattr(unreal.MCPReflectionLibrary, "get_object_property_metadata_json") if hasattr(unreal, "MCPReflectionLibrary") else False
def _snake(raw):
    out = ""
    for i, cch in enumerate(raw):
        if cch.isupper() and i > 0 and not raw[i-1].isupper():
            out += "_"
        out += cch.lower()
    return out
def _node_props(n):
    props = {}
    for pn in _getset_names(n):
        if pn in _NODE_SKIP:
            continue
        try:
            props[pn] = _ser_val(n.get_editor_property(pn))
        except Exception:
            pass
    if _HAS_META:
        try:
            md = unreal.MCPReflectionLibrary.get_object_property_metadata_json(n)
            doc = json.loads(md) if md else None
        except Exception:
            doc = None
        if isinstance(doc, dict):
            for pd in (doc.get("properties") or []):
                raw = pd.get("name")
                if not raw:
                    continue
                cands = [_snake(raw)]
                if len(raw) > 1 and raw[0] == "b" and raw[1].isupper():
                    cands.append(_snake(raw[1:]))
                for cand in cands:
                    if cand in props or cand in _NODE_SKIP:
                        continue
                    try:
                        val = n.get_editor_property(cand)
                    except Exception:
                        val = None
                    if val is not None:
                        props[cand] = _ser_val(val)
                        break
    return props
def _capture_prop(node, key):
    try:
        v = node.get_editor_property(key)
    except Exception as e:
        return {"kind": "error", "error": str(e)[:100]}
    if v is None:
        return {"kind": "none"}
    if isinstance(v, bool):
        return {"kind": "scalar", "value": v}
    if isinstance(v, (int, float, str)):
        return {"kind": "scalar", "value": v}
    if isinstance(v, unreal.EnumBase):
        return {"kind": "enum", "enum_type": type(v).__name__, "member": _enum_short(v)}
    if isinstance(v, unreal.Object):
        return {"kind": "object", "path": v.get_path_name()}
    if isinstance(v, unreal.Name):
        return {"kind": "name", "value": str(v)}
    if isinstance(v, unreal.Text):
        return {"kind": "text", "value": str(v)}
    return {"kind": "unsupported", "type": type(v).__name__}
def _restore_prop(node, key, cap):
    k = cap.get("kind")
    if k == "none":
        node.set_editor_property(key, None)
    elif k == "scalar":
        node.set_editor_property(key, cap.get("value"))
    elif k == "enum":
        node.set_editor_property(key, getattr(getattr(unreal, cap.get("enum_type")), cap.get("member")))
    elif k == "object":
        p = cap.get("path")
        node.set_editor_property(key, unreal.EditorAssetLibrary.load_asset(p) if p else None)
    elif k in ("name", "text"):
        node.set_editor_property(key, cap.get("value"))
def _set_prop(node, key, val):
    cur = None
    try:
        cur = node.get_editor_property(key)
    except Exception:
        cur = None
    tv = val
    if isinstance(cur, unreal.EnumBase) and isinstance(val, str):
        tv = getattr(getattr(unreal, type(cur).__name__), val)
    elif isinstance(cur, unreal.Object) and isinstance(val, str):
        tv = unreal.EditorAssetLibrary.load_asset(val) if val else None
    elif cur is None and isinstance(val, str) and val.startswith("/") and "." in val and unreal.EditorAssetLibrary.does_asset_exist(val):
        tv = unreal.EditorAssetLibrary.load_asset(val)
    node.set_editor_property(key, tv)
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        return True
    except Exception:
        return False
def _walk(root, max_nodes):
    counter = {"n": 0, "truncated": False}
    def go(n, depth):
        if n is None or depth > 24:
            return None
        if counter["n"] >= max_nodes:
            counter["truncated"] = True
            return None
        counter["n"] += 1
        node = {"name": _nname(n), "class": n.get_class().get_name(), "props": _node_props(n)}
        kids = _children(n)
        children = []
        for c in kids:
            if c is None:
                children.append({"name": None, "class": None, "empty_slot": True})
                continue
            cw = go(c, depth + 1)
            if cw is not None:
                children.append(cw)
        if children:
            node["children"] = children
        return node
    tree = go(root, 0)
    return tree, counter["n"], counter["truncated"]
'''

    # ================================================================== #
    # READS                                                              #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # list_sound_cue_node_types                                           #
    # ------------------------------------------------------------------ #
    _NODE_TYPES_BODY = r'''
import unreal, json
name_filter = PARAMS.get("name_filter")
base = getattr(unreal, "SoundNode", None)
out = []
if base is not None:
    for nm in dir(unreal):
        obj = getattr(unreal, nm, None)
        try:
            if isinstance(obj, type) and issubclass(obj, base):
                out.append(nm)
        except Exception:
            pass
if name_filter:
    f = name_filter.lower()
    out = [n for n in out if f in n.lower()]
out = sorted(out)
print("@@UMCP@@" + json.dumps({"status": "success", "base_class": "SoundNode",
    "total": len(out), "node_types": out,
    "note": "These USoundNode subclasses are constructible into a SoundCue via add_sound_cue_node / build_sound_cue_graph (unreal.new_object(<cls>, cue)). SoundNodeWavePlayer plays a SoundWave; SoundNodeMixer/Random/Concatenator/DistanceCrossFade branch to multiple children; Modulator/Attenuation/Looping/Delay wrap one child."}))
'''

    @mcp.tool()
    def list_sound_cue_node_types(ctx, name_filter: str = None) -> str:
        """List the reflectable unreal.SoundNode subclasses on this build (the SoundCue node
        palette). Read-only reflection (no ledger).

        name_filter: case-insensitive substring on the class name (e.g. 'Wave', 'Mixer').

        Returns the class names (e.g. 'SoundNodeWavePlayer', 'SoundNodeMixer', 'SoundNodeRandom',
        'SoundNodeModulator', 'SoundNodeAttenuation', ...) that add_sound_cue_node /
        build_sound_cue_graph can instantiate. This is the audio analogue of
        list_pcg_node_classes."""
        try:
            return json.dumps(_exec(_NODE_TYPES_BODY, {"name_filter": name_filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_sound_cue_graph                                                 #
    # ------------------------------------------------------------------ #
    _GET_GRAPH_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
max_nodes = int(PARAMS.get("max_nodes") or 300)
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    root = cue.get_editor_property("first_node")
    tree, n_count, truncated = _walk(root, max_nodes)
    flat = []
    def flatten(node):
        if not node or node.get("empty_slot"):
            return
        kids = node.get("children") or []
        flat.append({"name": node.get("name"), "class": node.get("class"),
                     "children": [(k.get("name") if not k.get("empty_slot") else None) for k in kids]})
        for k in kids:
            flatten(k)
    flatten(tree)
    out = {"status": "success", "cue": cue.get_name(), "cue_path": cue.get_path_name(),
           "first_node": (_nname(root) if root else None),
           "root_class": (root.get_class().get_name() if root else None),
           "node_count": n_count, "truncated": truncated,
           "nodes": flat, "graph": tree,
           "note": "Reachable runtime USoundNode tree from first_node (the semantic graph). Use a node 'name' as the identifier for get_sound_cue_node / connect_sound_cue_nodes / remove_sound_cue_node / set_sound_cue_node_properties. Editor UEdGraph visual layout is editor-only and not exposed."}
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def get_sound_cue_graph(ctx, cue_path: str, max_nodes: int = 300) -> str:
        """Read a SoundCue's runtime node graph: the first_node -> child_nodes tree plus a flat
        node table. Read-only (no ledger).

        cue_path:  the SoundCue asset path (e.g. '/Game/MCP_Scratch/MCP_SC.MCP_SC').
        max_nodes: cap on nodes walked (default 300; 'truncated' flags if hit).

        Returns first_node (root node name + class), a nested 'graph' (each node: name, class,
        reflected props, children) and a flat 'nodes' list [{name, class, children:[names]}]. The
        node 'name' (e.g. 'SoundNodeWavePlayer_0') is the identifier accepted by the other graph
        tools. Complements get_sound_cue_info (which is cue-parameter-centric) with an
        authoring-shaped view. Empty child slots surface as null. Errors cleanly if the asset is
        missing or is not a SoundCue."""
        try:
            return json.dumps(_exec(_GET_GRAPH_BODY, {"cue_path": cue_path, "max_nodes": max_nodes}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_sound_cue_node                                                  #
    # ------------------------------------------------------------------ #
    _GET_NODE_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
ident = PARAMS["node"]
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _resolve_node(cue, ident)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "node not found in cue: %s" % ident,
            "hint": "pass a subobject name from get_sound_cue_graph (e.g. SoundNodeWavePlayer_0) or a full object path"}))
    else:
        out = {"status": "success", "cue_path": cue.get_path_name(),
               "name": _nname(node), "node_path": node.get_path_name(),
               "class": node.get_class().get_name(),
               "child_names": _child_names(node),
               "props": _node_props(node)}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def get_sound_cue_node(ctx, cue_path: str, node: str) -> str:
        """Read one SoundCue node's reflected properties. Read-only (no ledger).

        cue_path: the SoundCue asset path.
        node:     the node's subobject name (from get_sound_cue_graph, e.g.
                  'SoundNodeWavePlayer_0') or its full object path '<cue>.<cue>:<Name>'.

        Returns the node class, its child node names, and a 'props' map (getset properties plus
        native FProperty names surfaced via reflection metadata, e.g. a WavePlayer's
        sound_wave_asset_ptr, a Modulator's volume/pitch min/max). Errors cleanly if the cue or
        node is missing."""
        try:
            return json.dumps(_exec(_GET_NODE_BODY, {"cue_path": cue_path, "node": node}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # validate_sound_cue                                                  #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    issues = []
    root = cue.get_editor_property("first_node")
    if root is None:
        issues.append({"severity": "error", "code": "no_root",
                       "message": "cue has no first_node (empty graph); it will produce no sound"})
    seen = set()
    stats = {"nodes": 0, "wave_players": 0, "empty_slots": 0, "waveless_players": 0}
    def visit(n, depth):
        if n is None or depth > 40:
            return
        nm = _nname(n)
        if nm in seen:
            return
        seen.add(nm)
        stats["nodes"] += 1
        cn = n.get_class().get_name()
        if cn in ("SoundNodeWavePlayer", "SoundNodeWaveParam", "SoundNodeDialoguePlayer"):
            stats["wave_players"] += 1
            wave = None
            for key in ("sound_wave_asset_ptr", "sound_wave", "dialogue_wave_parameter"):
                try:
                    wv = n.get_editor_property(key)
                except Exception:
                    wv = None
                if wv is not None:
                    wave = wv
                    break
            if wave is None:
                stats["waveless_players"] += 1
                issues.append({"severity": "warning", "code": "waveless_player",
                               "node": nm, "class": cn,
                               "message": "%s '%s' has no sound wave assigned -> silent" % (cn, nm)})
        kids = _children(n)
        for i, c in enumerate(kids):
            if c is None:
                stats["empty_slots"] += 1
                issues.append({"severity": "warning", "code": "empty_child_slot",
                               "node": nm, "class": cn, "slot": i,
                               "message": "%s '%s' child slot %d is empty (dangling input)" % (cn, nm, i)})
            else:
                visit(c, depth + 1)
    visit(root, 0)
    ok = not any(x.get("severity") == "error" for x in issues)
    print("@@UMCP@@" + json.dumps({"status": "success", "cue_path": cue.get_path_name(),
        "valid": ok, "issue_count": len(issues), "issues": issues, "stats": stats,
        "note": "Validation reasons over the reachable first_node tree. USoundNode min/max child-count limits are not Python-reflected, so slot-count violations are not checked here (the engine validates on cue compile)."}))
'''

    @mcp.tool()
    def validate_sound_cue(ctx, cue_path: str) -> str:
        """Validate a SoundCue's runtime node graph. Read-only (no ledger).

        cue_path: the SoundCue asset path.

        Walks the reachable first_node tree and reports issues: 'no_root' (error -- no first_node),
        'waveless_player' (warning -- a WavePlayer/WaveParam/DialoguePlayer with no wave -> silent),
        and 'empty_child_slot' (warning -- a null entry in a node's child_nodes / dangling input).
        Returns valid (True if no error-severity issues), an issues list, and stats
        (nodes/wave_players/empty_slots/waveless_players). Min/max child-count limits are not
        Python-reflected so are not enforced (documented). Errors cleanly if the asset is missing or
        is not a SoundCue."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"cue_path": cue_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledgered)                                                   #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # add_sound_cue_node                                                  #
    # ------------------------------------------------------------------ #
    _ADD_NODE_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
node_class = PARAMS["node_class"]
is_root = bool(PARAMS.get("is_root"))
parent_ident = PARAMS.get("parent_node")
child_index = PARAMS.get("child_index")
props = PARAMS.get("properties") or {}
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    base = getattr(unreal, "SoundNode", None)
    cls = getattr(unreal, node_class, None)
    if cls is None or base is None or not (isinstance(cls, type) and issubclass(cls, base)):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "node_class is not a reflectable unreal.SoundNode subclass: %s" % node_class,
            "hint": "call list_sound_cue_node_types"}))
    elif is_root and parent_ident:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "specify at most one placement: is_root OR parent_node (not both)"}))
    else:
        parent = None
        if parent_ident:
            parent = _resolve_node(cue, parent_ident)
            if parent is None:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "parent_node not found in cue: %s" % parent_ident}))
                parent = "MISS"
        if parent == "MISS":
            pass
        else:
            with unreal.ScopedEditorTransaction("MCP add_sound_cue_node"):
                node = unreal.new_object(cls, cue)
                applied = {}
                for k, v in props.items():
                    try:
                        _set_prop(node, k, v); applied[k] = True
                    except Exception as e:
                        applied[k] = "ERR:" + str(e)[:60]
                entry = {"op": "sound_cue_add_node", "asset_path": cue.get_path_name(),
                         "node_name": _nname(node)}
                placement = "floating"
                if is_root:
                    placement = "root"
                    prior_root = cue.get_editor_property("first_node")
                    entry["placement"] = "root"
                    entry["prior_first_node_name"] = (_nname(prior_root) if prior_root else None)
                    cue.set_editor_property("first_node", node)
                elif parent is not None:
                    placement = "parent"
                    entry["placement"] = "parent"
                    entry["parent_name"] = _nname(parent)
                    entry["prior_children_names"] = _child_names(parent)
                    kids = _children(parent)
                    if child_index is None or int(child_index) >= len(kids):
                        kids.append(node)
                    else:
                        ci = int(child_index)
                        if ci < 0:
                            ci = 0
                        kids[ci] = node
                    _write_children(parent, kids)
                else:
                    entry["placement"] = "floating"
            _save(cue.get_path_name())
            _ledger().append(entry)
            print("@@UMCP@@" + json.dumps({"status": "success", "cue_path": cue.get_path_name(),
                "node_name": entry["node_name"], "node_path": node.get_path_name(),
                "class": node.get_class().get_name(), "placement": placement,
                "properties_applied": applied,
                "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_sound_cue_node(ctx, cue_path: str, node_class: str, is_root: bool = False,
                           parent_node: str = None, child_index: int = None,
                           properties: dict = None) -> str:
        """Add a USoundNode to a SoundCue's runtime graph (generalizes add_wave_player_to_cue to
        any SoundNode class). Ledgered write.

        cue_path:    the SoundCue asset path (use create_sound_cue / create_audio_asset first).
        node_class:  a SoundNode subclass name from list_sound_cue_node_types (e.g.
                     'SoundNodeWavePlayer', 'SoundNodeMixer', 'SoundNodeModulator').
        is_root:     if True, set the new node as the cue's first_node (graph root). Mutually
                     exclusive with parent_node.
        parent_node: if given, wire the new node into this existing node's child_nodes (identify by
                     name or full path).
        child_index: with parent_node, the slot to occupy (default: append). An index within the
                     current array replaces that slot; index >= len appends.
        properties:  optional {prop: value} applied to the new node (e.g.
                     {'sound_wave_asset_ptr': '/Engine/.../Wave.Wave', 'looping': True}); object
                     props accept an asset path string, enum props a member name.

        The node is created via unreal.new_object(getattr(unreal, node_class), cue) with the cue as
        outer, so it persists in the saved package whether or not it is wired (verified live). If
        neither is_root nor parent_node is given the node is created 'floating' (connect it later
        with connect_sound_cue_nodes).

        NEW ledger op 'sound_cue_add_node'. Fields: {asset_path, node_name, placement
        ('root'|'parent'|'floating'), [prior_first_node_name] if root, [parent_name,
        prior_children_names] if parent}. Inverse (for the coordinator to fold into
        editor_level.undo): reload the cue; if placement=='root' set first_node back to
        resolve(prior_first_node_name) or None; if placement=='parent' set the parent's child_nodes
        back to [resolve(n) for n in prior_children_names]; save. The added node lingers as an
        orphaned subobject (cosmetic; not in the graph -- same posture as add_wave_player_to_cue)."""
        params = {"cue_path": cue_path, "node_class": node_class, "is_root": is_root,
                  "parent_node": parent_node, "child_index": child_index,
                  "properties": properties or {}}
        try:
            return json.dumps(_exec(_ADD_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # connect_sound_cue_nodes                                             #
    # ------------------------------------------------------------------ #
    _CONNECT_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
parent_ident = PARAMS["parent_node"]
child_ident = PARAMS["child_node"]
child_index = PARAMS.get("child_index")
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    parent = _resolve_node(cue, parent_ident)
    child = _resolve_node(cue, child_ident)
    if parent is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent_node not found: %s" % parent_ident}))
    elif child is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "child_node not found: %s" % child_ident}))
    elif parent.get_path_name() == child.get_path_name():
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot connect a node to itself"}))
    else:
        prior = _child_names(parent)
        with unreal.ScopedEditorTransaction("MCP connect_sound_cue_nodes"):
            kids = _children(parent)
            if child_index is None or int(child_index) >= len(kids):
                kids.append(child)
                slot = len(kids) - 1
            else:
                slot = int(child_index)
                if slot < 0:
                    slot = 0
                kids[slot] = child
            _write_children(parent, kids)
        _save(cue.get_path_name())
        entry = {"op": "sound_cue_connect", "asset_path": cue.get_path_name(),
                 "parent_name": _nname(parent), "prior_children_names": prior}
        _ledger().append(entry)
        print("@@UMCP@@" + json.dumps({"status": "success", "cue_path": cue.get_path_name(),
            "parent": _nname(parent), "child": _nname(child), "slot": slot,
            "children_after": _child_names(parent),
            "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_sound_cue_nodes(ctx, cue_path: str, parent_node: str, child_node: str,
                                child_index: int = None) -> str:
        """Wire one SoundCue node into another node's child_nodes (an input slot). Ledgered write.

        cue_path:    the SoundCue asset path.
        parent_node: the node receiving the input (name or full path).
        child_node:  the node feeding into the parent (name or full path).
        child_index: the parent slot to set (default: append). Index within the current array
                     replaces that slot; index >= len appends.

        Sets parent.child_nodes[index] = child (audio signal flows child -> parent -> ... ->
        first_node). Both nodes must already exist in the cue (add them with add_sound_cue_node or
        build the whole graph with build_sound_cue_graph). USoundNode max-child limits are not
        Python-reflected, so this does not cap slot count (the engine validates on compile).

        NEW ledger op 'sound_cue_connect'. Fields: {asset_path, parent_name,
        prior_children_names}. Inverse (coordinator fold): reload the cue, resolve the parent by
        name, set its child_nodes back to [resolve(n) for n in prior_children_names], save. The
        full prior array is captured so the restore is exact."""
        params = {"cue_path": cue_path, "parent_node": parent_node,
                  "child_node": child_node, "child_index": child_index}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_sound_cue_node                                               #
    # ------------------------------------------------------------------ #
    _REMOVE_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
ident = PARAMS["node"]
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _resolve_node(cue, ident)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found in cue: %s" % ident}))
    else:
        npath = node.get_path_name()
        # find all reachable nodes + which parents reference the target
        allnodes = {}
        seen = set()
        def collect(n, depth):
            if n is None or depth > 40:
                return
            nm = _nname(n)
            if nm in seen:
                return
            seen.add(nm)
            allnodes[nm] = n
            for c in _children(n):
                collect(c, depth + 1)
        root = cue.get_editor_property("first_node")
        collect(root, 0)
        was_root = (root is not None and root.get_path_name() == npath)
        parents = []
        for pnm, pn in allnodes.items():
            if any((c is not None and c.get_path_name() == npath) for c in _children(pn)):
                parents.append({"parent_name": pnm, "prior_children_names": _child_names(pn)})
        with unreal.ScopedEditorTransaction("MCP remove_sound_cue_node"):
            if was_root:
                cue.set_editor_property("first_node", None)
            for prec in parents:
                pn = allnodes.get(prec["parent_name"])
                if pn is None:
                    continue
                kept = [c for c in _children(pn) if not (c is not None and c.get_path_name() == npath)]
                _write_children(pn, kept)
        _save(cue.get_path_name())
        entry = {"op": "sound_cue_remove_node", "asset_path": cue.get_path_name(),
                 "node_name": _nname(node), "was_root": was_root, "parents": parents}
        _ledger().append(entry)
        print("@@UMCP@@" + json.dumps({"status": "success", "cue_path": cue.get_path_name(),
            "removed": _nname(node), "was_root": was_root,
            "detached_from_parents": [p["parent_name"] for p in parents],
            "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_sound_cue_node(ctx, cue_path: str, node: str) -> str:
        """Detach a node from a SoundCue's graph -- clears it from first_node (if it is the root)
        and removes it from every parent's child_nodes. Ledgered write.

        cue_path: the SoundCue asset path.
        node:     the node's subobject name (from get_sound_cue_graph) or full path.

        The node object itself is NOT destroyed (it lingers as an orphaned package subobject); it is
        only unwired, which is what removes it from the runtime graph. Its own children are left
        attached to it (they leave the reachable graph with it).

        NEW ledger op 'sound_cue_remove_node'. Fields: {asset_path, node_name, was_root,
        parents:[{parent_name, prior_children_names}]}. Inverse (coordinator fold): reload the cue;
        if was_root set first_node back to resolve(node_name); for each parent record set its
        child_nodes back to [resolve(n) for n in prior_children_names]; save. Because the node
        subobject persists, re-resolving it by name restores the exact prior wiring."""
        params = {"cue_path": cue_path, "node": node}
        try:
            return json.dumps(_exec(_REMOVE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_sound_cue_node_properties                                       #
    # ------------------------------------------------------------------ #
    _SET_PROPS_BODY = _HELPERS + r'''
path = PARAMS["cue_path"]
ident = PARAMS["node"]
props = PARAMS.get("properties") or {}
cue, err = _load_cue(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not props:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no properties given"}))
else:
    node = _resolve_node(cue, ident)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found in cue: %s" % ident}))
    else:
        prior = {}
        refused = {}
        for k in props.keys():
            cap = _capture_prop(node, k)
            if cap.get("kind") in ("error", "unsupported"):
                refused[k] = cap
            else:
                prior[k] = cap
        if refused:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing to set property/properties whose prior value cannot be captured faithfully (undo would be lossy): %s" % list(refused.keys()),
                "refused": refused,
                "hint": "this tool supports scalar (bool/int/float/str), enum (member name), and object-ref (asset path) properties"}))
        else:
            applied = {}
            with unreal.ScopedEditorTransaction("MCP set_sound_cue_node_properties"):
                for k, v in props.items():
                    try:
                        _set_prop(node, k, v); applied[k] = _ser_val(node.get_editor_property(k))
                    except Exception as e:
                        applied[k] = "ERR:" + str(e)[:80]
            _save(cue.get_path_name())
            entry = {"op": "sound_cue_set_node_props", "asset_path": cue.get_path_name(),
                     "node_name": _nname(node), "prior": prior}
            _ledger().append(entry)
            print("@@UMCP@@" + json.dumps({"status": "success", "cue_path": cue.get_path_name(),
                "node": _nname(node), "applied": applied, "prior": prior,
                "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_sound_cue_node_properties(ctx, cue_path: str, node: str, properties: dict) -> str:
        """Set reflected properties on a SoundCue node, with faithful prior capture. Ledgered write.

        cue_path:   the SoundCue asset path.
        node:       the node's subobject name (from get_sound_cue_graph) or full path.
        properties: {prop: value} to set. Supported value kinds: scalar (bool/int/float/str), enum
                    (pass the member name string, e.g. 'BINK_AUDIO'), and object reference (pass an
                    asset path string, e.g. a SoundWave path for a WavePlayer's
                    'sound_wave_asset_ptr'). Object/enum coercion follows the node's current
                    property type.

        A property whose prior value cannot be captured faithfully (an unsupported struct/array
        type, or an unreadable prop) is REFUSED for the whole call so undo stays exact -- nothing is
        set. Use get_sound_cue_node to see a node's props.

        NEW ledger op 'sound_cue_set_node_props'. Fields: {asset_path, node_name, prior:{key:cap}}
        where cap is one of {kind:none} | {kind:scalar,value} | {kind:enum,enum_type,member} |
        {kind:object,path} | {kind:name/text,value}. Inverse (coordinator fold): reload the cue,
        resolve the node by name, and for each key restore per cap: none->None; scalar->value;
        enum->getattr(getattr(unreal,enum_type),member); object->load_asset(path) or None;
        name/text->value; save."""
        params = {"cue_path": cue_path, "node": node, "properties": properties or {}}
        try:
            return json.dumps(_exec(_SET_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # build_sound_cue_graph — whole-tree build in one call                #
    # ------------------------------------------------------------------ #
    _BUILD_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
nodes_spec = PARAMS.get("nodes") or []
conns_spec = PARAMS.get("connections") or []
root_id = PARAMS.get("root")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if not nodes_spec:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no nodes given"}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (build_sound_cue_graph creates a NEW cue; pick a fresh name or use add/connect tools)" % asset_path}))
else:
    base = getattr(unreal, "SoundNode", None)
    bad = [n.get("class") for n in nodes_spec if not (isinstance(getattr(unreal, n.get("class", ""), None), type) and issubclass(getattr(unreal, n.get("class", ""), None), base))]
    if bad:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "unknown SoundNode class(es): %s" % bad, "hint": "call list_sound_cue_node_types"}))
    else:
        created_dir = not EAL.does_directory_exist(package_path)
        cue = at.create_asset(name, package_path, unreal.SoundCue, unreal.SoundCueFactoryNew())
        if cue is None or not isinstance(cue, unreal.SoundCue):
            if created_dir and EAL.does_directory_exist(package_path):
                try: EAL.delete_directory(package_path)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset failed for %s" % asset_path}))
        else:
            id_to_node = {}
            id_to_name = {}
            node_report = []
            with unreal.ScopedEditorTransaction("MCP build_sound_cue_graph"):
                for spec in nodes_spec:
                    nid = spec.get("id")
                    cls = getattr(unreal, spec.get("class"))
                    n = unreal.new_object(cls, cue)
                    for k, v in (spec.get("properties") or {}).items():
                        try: _set_prop(n, k, v)
                        except Exception: pass
                    id_to_node[nid] = n
                    id_to_name[nid] = _nname(n)
                    node_report.append({"id": nid, "name": _nname(n), "class": n.get_class().get_name()})
                # group connections by parent, honoring explicit indices then append-order
                by_parent = {}
                for c in conns_spec:
                    by_parent.setdefault(c.get("parent"), []).append(c)
                conn_report = []
                for pid, clist in by_parent.items():
                    pnode = id_to_node.get(pid)
                    if pnode is None:
                        conn_report.append({"parent": pid, "error": "unknown parent id"})
                        continue
                    indexed = [c for c in clist if c.get("index") is not None]
                    appended = [c for c in clist if c.get("index") is None]
                    size = 0
                    for c in indexed:
                        size = max(size, int(c.get("index")) + 1)
                    size += len(appended)
                    slots = [None] * size
                    used = set()
                    for c in indexed:
                        ci = int(c.get("index"))
                        slots[ci] = id_to_node.get(c.get("child")); used.add(ci)
                    ai = 0
                    for c in appended:
                        while ai in used:
                            ai += 1
                        slots[ai] = id_to_node.get(c.get("child")); used.add(ai); ai += 1
                    _write_children(pnode, slots)
                    conn_report.append({"parent": pid, "children": [(_nname(s) if s else None) for s in slots]})
                if root_id is not None and root_id in id_to_node:
                    cue.set_editor_property("first_node", id_to_node[root_id])
            _close = None
            _save(asset_path)
            entry = {"op": "create_asset", "asset_path": asset_path,
                     "package_path": package_path, "created_dir": created_dir}
            _ledger().append(entry)
            root_node = cue.get_editor_property("first_node")
            print("@@UMCP@@" + json.dumps({"status": "success", "cue": cue.get_name(),
                "asset_path": asset_path, "object_path": cue.get_path_name(),
                "first_node": (_nname(root_node) if root_node else None),
                "nodes": node_report, "connections": conn_report,
                "id_to_name": id_to_name, "created_dir": created_dir,
                "ledger_entry": entry, "ledger_depth": len(_ledger()),
                "undo_note": "reuses the generic create_asset inverse (delete the whole cue)"}))
'''

    @mcp.tool()
    def build_sound_cue_graph(ctx, name: str, nodes: list, connections: list = None,
                              root: str = None, package_path: str = "/Game/MCP_Scratch") -> str:
        """Build an entire SoundCue node graph -- create the cue, all nodes, and all wiring -- in a
        single call. Ledgered write (reuses the create_asset inverse).

        name:         asset name for the NEW SoundCue (must not already exist).
        package_path: content directory (default '/Game/MCP_Scratch').
        nodes:        list of node specs [{'id': <your id>, 'class': '<SoundNode subclass>',
                      'properties': {optional prop: value}}]. 'id' is your own handle used to
                      reference the node in connections/root (it maps to the real subobject name in
                      the response's id_to_name).
        connections:  list of [{'parent': <id>, 'child': <id>, 'index': <optional slot>}]. Children
                      with an explicit index take that slot; the rest fill remaining slots in order.
        root:         the id of the node to set as the cue's first_node (graph root).

        Everything is built while the cue is live, then saved, so all reachable nodes persist.
        Property/enum/object coercion matches add_sound_cue_node.

        Reuses the already-folded generic ledger op 'create_asset' {asset_path, package_path,
        created_dir} -- NO new op. Inverse: close editors + delete the whole cue [+ rmdir if this
        call created the directory]."""
        params = {"name": name, "package_path": package_path, "nodes": nodes or [],
                  "connections": connections or [], "root": root}
        try:
            return json.dumps(_exec(_BUILD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NO `undo` tool here (editor_level.py owns the unified undo). NEW folds to add to
    # editor_level.undo (specified on each tool docstring + the build report):
    #   sound_cue_add_node, sound_cue_connect, sound_cue_remove_node, sound_cue_set_node_props.
    # build_sound_cue_graph reuses the existing create_asset fold.
    # ------------------------------------------------------------------ #
