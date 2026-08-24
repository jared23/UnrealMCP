"""UserTools :: MetaSound authoring (WRITE)  (spec: docs/spec/audio.md, plan: audio_plan.md Group C + #5,#20,#21)

Pure-Python MetaSound graph authoring over Unreal's public Python API (UE 5.8, MetaSound plugin
EnabledByDefault -> no build, no enable). This is audio Wave 2. Wave 1 SoundCue tools live in
soundcue_graph.py / audio_asset_ops.py and are NOT duplicated here -- MetaSound is a separate system.

The clean-room conventions (base64 PARAMS injection, @@UMCP@@ one-line payload, Output-Log delta
capture, per-session undo ledger) are copied verbatim from the gold-standard editor_level.py and mirror
sound_write.py. Every tool performs ONE operation per call.

RESOLVED PROBES (all confirmed live vs TestMCPSetup, UE 5.8.1, headless):
  * Casing: unreal.MetaSoundSource / MetaSoundPatch / MetaSoundBuilderSubsystem (get_engine_subsystem) /
    MetaSoundEditorSubsystem (get_editor_subsystem) / MetaSoundBuilderResult{SUCCEEDED=0,FAILED=1} use
    CAPITAL-S; the Frontend structs MetasoundFrontendClassName{namespace,name,variant} and
    MetasoundFrontendLiteral use LOWERCASE-s. (UE genuinely mixes the two.)
  * create_source_builder(name, output_format=MetaSoundOutputAudioFormat.MONO, is_one_shot=True) returns a
    5-tuple (builder, on_play_output_handle, on_finished_input_handle, audio_out_input_handles[], result).
    create_patch_builder(name) returns (builder, result).
  * MetaSoundBuilderNodeInput/OutputHandle and MetaSoundNodeHandle are OPAQUE structs (no reflected GUID
    field), BUT handle.export_text() yields "(NodeID=<32-hex>)" and a fresh handle.import_text(that) on a
    NEW builder (even after reload) resolves the SAME node -> node identity is a persistent GUID string.
    This is how per-node incremental editing works across separate MCP calls.
  * Persistence: editor_subsystem.find_or_begin_building(ms) returns a builder editing the asset IN PLACE;
    EditorAssetLibrary.save_asset(path) alone commits the mutations (verified: a fobb-added graph input +
    a fobb-added node both survive a reload). NO register_graph_with_frontend / build_and_overwrite needed.
  * build_to_asset(builder, author, asset_name, package_path, template_sound_wave=None) -> (doc, result);
    creates a real MetaSoundSource under package_path. No init_node_locations required (locations are
    optional cosmetic editor metadata set via editor_subsystem.set_node_location(builder, handle, vec2d)).
  * add_node_by_class_name(MetasoundFrontendClassName, major_version=1) -> (handle, result). Confirmed
    standard class names (namespace "UE"): Sine/Saw/Square/Triangle (variant "Audio"), Add/Multiply
    (variant "Audio" or "Float"), Subtract/Divide (variant "Float"), LFO/Noise/Ladder Filter/Delay
    (variant "Audio"), Wave Player (variant "Mono"/"Stereo"). A short starter catalog is exposed via
    metasound_read.list_metasound_node_catalog; full node discovery is C++ (Wave 4, ISearchEngine).

REVERSIBILITY (see the module-bottom ledger doc + the build report for the exact op schemas the coordinator
folds into editor_level.undo):
  * create_metasound + build_metasound_graph(new asset) REUSE the existing generic create_asset inverse
    (editor_level.py:2236). NO new fold.
  * Incremental builder edits ledger ONE op each with prior capture; the inverse is the opposite builder
    call replayed on a fresh find_or_begin_building builder. NEW ops:
      metasound_add_node{asset_path,node_id}                 -> reconstruct handle, remove_node
      metasound_connect{asset_path,to_node_id,to_input_name} -> reconstruct+find input, disconnect_node_input
      metasound_disconnect{asset_path,from_node_id,from_output_name,to_node_id,to_input_name} -> connect_nodes
      metasound_set_node_input_default{asset_path,node_id,input_name,had_prior,prior_literal} -> restore/remove
      metasound_add_graph_input{asset_path,input_name}       -> remove_graph_input
      metasound_add_graph_output{asset_path,output_name}     -> remove_graph_output
      metasound_add_variable{asset_path,variable_name}       -> remove_graph_variable
      metasound_set_graph_input_default{asset_path,input_name,had_prior,prior_literal} -> restore/reset
      metasound_remove_graph_member{asset_path,kind,name,data_type,default_literal} -> re-add (PARTIAL: any
          downstream connections are NOT restored -- documented, honest)
      metasound_set_interface{asset_path,interface_name,added} -> remove_interface if added else add_interface
  * remove_metasound_node is NOT faithfully reversible in pure Python (a node's registered class cannot be
    read back from its handle in the BP API, so it cannot be re-created) -> it does NOT push a ledger entry
    and says so in its result. Honest coarse/no-undo per the plan's escape hatch.
  * layout_metasound_graph sets cosmetic node positions; there is no reflected get-node-location, so prior
    positions cannot be captured -> NO ledger, documented as non-reversible cosmetic metadata.

SCRATCH-ONLY: MetaSound assets are created under /Game/MCP_Scratch with the MCP_MS_ prefix. No level
mutation (asset-only). Soft-delete is a rename to /Game/_MCP_Trash; this module never delete_assets.
NO ''' and NO backslashes in any snippet body; all data goes through base64 PARAMS. Reserved wrapper
locals (sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj) are never assigned.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

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

    # ---------------------------------------------------------------- #
    # Shared Unreal-side helpers. NO ''' / NO backslashes below.        #
    # ---------------------------------------------------------------- #
    _MS = r'''
import unreal, json, builtins
EAL = unreal.EditorAssetLibrary
SUCC = unreal.MetaSoundBuilderResult.SUCCEEDED
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _bss():
    return unreal.get_engine_subsystem(unreal.MetaSoundBuilderSubsystem)
def _es():
    return unreal.get_editor_subsystem(unreal.MetaSoundEditorSubsystem)
def _resok(r):
    return r == SUCC
def _resstr(r):
    return str(r).split(".")[-1].split(":")[0].strip()
def _close(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _load_ms(path):
    if not path:
        return None, "no asset path given"
    try:
        obj = EAL.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, (unreal.MetaSoundSource, unreal.MetaSoundPatch)):
        return None, "asset is not a MetaSound (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _fobb(ms):
    b, r = _es().find_or_begin_building(ms)
    if not _resok(r) or b is None:
        return None, "find_or_begin_building failed: %s" % _resstr(r)
    return b, None
def _save(path):
    try:
        EAL.save_asset(path, only_if_is_dirty=False)
        return True
    except Exception:
        return False
def _guid_of(handle):
    try:
        t = handle.export_text()
    except Exception:
        return None
    if "NodeID=" not in t:
        return None
    return t.split("NodeID=", 1)[1].split(")", 1)[0].strip()
def _node_from_guid(guid):
    h = unreal.MetaSoundNodeHandle()
    h.import_text("(NodeID=" + str(guid) + ")")
    return h
def _mk_classname(ns, nm, va):
    c = unreal.MetasoundFrontendClassName()
    c.set_editor_property("namespace", ns or "")
    c.set_editor_property("name", nm or "")
    c.set_editor_property("variant", va or "")
    return c
def _mk_literal(type_str, value):
    # returns (literal_or_None, err). Scalar/array creators return (literal, data_type); object returns literal.
    bss = _bss()
    t = (type_str or "").lower()
    try:
        if t in ("float", "time", "audio_float"):
            return bss.create_float_meta_sound_literal(float(value))[0], None
        if t in ("int32", "int", "integer"):
            return bss.create_int_meta_sound_literal(int(value))[0], None
        if t in ("bool", "boolean", "trigger"):
            return bss.create_bool_meta_sound_literal(bool(value))[0], None
        if t in ("string",):
            return bss.create_string_meta_sound_literal(str(value))[0], None
        if t in ("floatarray", "float:array"):
            return bss.create_float_array_meta_sound_literal([float(x) for x in (value or [])])[0], None
        if t in ("intarray", "int32:array"):
            return bss.create_int_array_meta_sound_literal([int(x) for x in (value or [])])[0], None
        if t in ("boolarray",):
            return bss.create_bool_array_meta_sound_literal([bool(x) for x in (value or [])])[0], None
        if t in ("stringarray",):
            return bss.create_string_array_meta_sound_literal([str(x) for x in (value or [])])[0], None
        if t in ("object",):
            obj = EAL.load_asset(value) if value else None
            return bss.create_object_meta_sound_literal(obj), None
    except Exception as e:
        return None, "literal build failed for type %s: %s" % (type_str, str(e)[:100])
    return None, "unsupported literal type: %s" % type_str
def _default_literal_for_type(data_type):
    # a benign default literal for a graph input/output of the given data type; empty literal for
    # types with no scalar creator (Audio/Trigger/etc.)
    lit, err = _mk_literal(data_type, 0)
    if lit is not None:
        return lit
    return unreal.MetasoundFrontendLiteral()
def _restore_literal(text):
    lit = unreal.MetasoundFrontendLiteral()
    lit.import_text(text)
    return lit
'''

    # ================================================================ #
    # #5 create_metasound                                              #
    # ================================================================ #
    _CREATE_BODY = _MS + r'''
name = PARAMS["name"]
pkg = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
kind = (PARAMS.get("source_type") or "source").lower()
fmt = (PARAMS.get("output_format") or "mono").lower()
one_shot = bool(PARAMS.get("is_one_shot", True))
apath = pkg + "/" + name + "." + name
if EAL.does_asset_exist(apath):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % apath}))
else:
    fmt_map = {"mono": unreal.MetaSoundOutputAudioFormat.MONO, "stereo": unreal.MetaSoundOutputAudioFormat.STEREO,
               "quad": unreal.MetaSoundOutputAudioFormat.QUAD, "5.1": unreal.MetaSoundOutputAudioFormat.FIVE_DOT_ONE,
               "7.1": unreal.MetaSoundOutputAudioFormat.SEVEN_DOT_ONE}
    bss = _bss(); es = _es()
    created_dir = not EAL.does_directory_exist(pkg)
    if kind == "patch":
        tup = bss.create_patch_builder(name + "_Builder")
        builder = tup[0]; mkres = tup[-1]
    else:
        of = fmt_map.get(fmt, unreal.MetaSoundOutputAudioFormat.MONO)
        tup = bss.create_source_builder(name + "_Builder", of, one_shot)
        builder = tup[0]; mkres = tup[-1]
    if builder is None or not _resok(mkres):
        if created_dir and EAL.does_directory_exist(pkg):
            try: EAL.delete_directory(pkg)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "builder create failed: %s" % _resstr(mkres)}))
    else:
        doc, br = es.build_to_asset(builder, PARAMS.get("author") or "MCP", name, pkg)
        ok = _resok(br) and EAL.does_asset_exist(apath)
        obj = EAL.load_asset(apath) if ok else None
        if not ok or obj is None:
            if created_dir and EAL.does_directory_exist(pkg):
                try: EAL.delete_directory(pkg)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "build_to_asset failed: %s" % _resstr(br)}))
        else:
            _close(obj)
            _save(apath)
            _ledger().append({"op": "create_asset", "asset_path": apath, "package_path": pkg, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(), "asset_path": apath,
                "object_path": obj.get_path_name(), "class": obj.get_class().get_name(),
                "kind": kind, "output_format": (fmt if kind == "source" else None), "is_one_shot": (one_shot if kind == "source" else None),
                "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_metasound(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                         source_type: str = "source", output_format: str = "mono",
                         is_one_shot: bool = True, author: str = "MCP") -> str:
        """Create a new MetaSound asset (MetaSoundSource or MetaSoundPatch) non-interactively.

        name:          asset name (e.g. 'MCP_MS_Beep'). Scratch convention: prefix MCP_MS_.
        package_path:  content dir (default '/Game/MCP_Scratch'); intermediate folders created as needed.
        source_type:   'source' (a playable MetaSoundSource, default) or 'patch' (a reusable MetaSoundPatch).
        output_format: for a source: 'mono' (default), 'stereo', 'quad', '5.1', '7.1'. Ignored for a patch.
        is_one_shot:   for a source: whether it declares the UE.Source.OneShot interface (default True).
        author:        author string stamped into the document metadata (default 'MCP').

        Uses MetaSoundBuilderSubsystem.create_source_builder/create_patch_builder ->
        MetaSoundEditorSubsystem.build_to_asset(builder, author, name, package_path). The new source ships
        with the standard interfaces (OnPlay input, OnFinished + OutputFormat audio outputs). Add nodes with
        add_metasound_node, wire with connect_metasound_nodes, or build a whole graph in one call with
        build_metasound_graph. Inspect with metasound_read.get_metasound_info.

        Ledgers the generic 'create_asset' op {asset_path, package_path, created_dir} (already folded into
        editor_level.undo; inverse = close editors + delete asset [+ rmdir if we created the dir])."""
        params = {"name": name, "package_path": package_path, "source_type": source_type,
                  "output_format": output_format, "is_one_shot": is_one_shot, "author": author}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #8 add_metasound_node                                            #
    # ================================================================ #
    _ADD_NODE_BODY = _MS + r'''
apath = PARAMS["asset_path"]
ns = PARAMS.get("namespace") or "UE"
nm = PARAMS["name"]
va = PARAMS.get("variant") or ""
mv = int(PARAMS.get("major_version") or 1)
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        h, r = b.add_node_by_class_name(_mk_classname(ns, nm, va), mv)
        if not _resok(r):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "add_node_by_class_name FAILED for %s.%s.%s (v%d) -- unknown/invalid class name" % (ns, nm, va, mv),
                "hint": "see metasound_read.list_metasound_node_catalog for confirmed class names"}))
        else:
            guid = _guid_of(h)
            _save(apath)
            _ledger().append({"op": "metasound_add_node", "asset_path": apath, "node_id": guid})
            ins, ri = b.find_node_inputs(h); outs, ro = b.find_node_outputs(h)
            def _vd(hh, getter):
                nm2, dt2, rr = getter(hh); return {"name": str(nm2), "type": str(dt2)}
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "node_id": guid,
                "class": {"namespace": ns, "name": nm, "variant": va, "major_version": mv},
                "inputs": [_vd(x, b.get_node_input_data) for x in list(ins)],
                "outputs": [_vd(x, b.get_node_output_data) for x in list(outs)],
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_metasound_node(ctx, asset_path: str, name: str, namespace: str = "UE",
                           variant: str = "", major_version: int = 1) -> str:
        """Add a standard/external node to a MetaSound graph by its registered class name.

        asset_path: the MetaSoundSource/Patch object path (e.g. '/Game/MCP_Scratch/MCP_MS_Beep.MCP_MS_Beep').
        name:       the node class Name (e.g. 'Sine', 'Add', 'Multiply', 'Wave Player', 'Ladder Filter').
        namespace:  the class Namespace (default 'UE' for engine standard nodes).
        variant:    the class Variant (e.g. 'Audio' for the audio Sine oscillator, 'Float' for a float Add,
                    'Mono'/'Stereo' for a Wave Player). Empty for variant-less classes.
        major_version: class major version (default 1).

        Edits the asset in place via MetaSoundEditorSubsystem.find_or_begin_building ->
        add_node_by_class_name, then saves. Returns node_id -- the persistent NodeID GUID (usable in
        connect/disconnect/set-default/layout across later calls) -- plus the node's input/output pin
        names and types. FAILS cleanly for an unknown class name (see list_metasound_node_catalog).

        Ledgers NEW op 'metasound_add_node' {asset_path, node_id}. Inverse: find_or_begin_building ->
        reconstruct the handle from node_id (import_text) -> remove_node -> save."""
        params = {"asset_path": asset_path, "name": name, "namespace": namespace,
                  "variant": variant, "major_version": major_version}
        try:
            return json.dumps(_exec(_ADD_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #9 remove_metasound_node  (NO faithful undo -- documented)       #
    # ================================================================ #
    _REMOVE_NODE_BODY = _MS + r'''
apath = PARAMS["asset_path"]
guid = PARAMS["node_id"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        try:
            h = _node_from_guid(guid)
        except Exception as e:
            h = None
        if h is None or not b.contains_node(h):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "node_id not found in graph: %s" % guid}))
        else:
            r = b.remove_node(h, True)
            _save(apath)
            print("@@UMCP@@" + json.dumps({"status": ("success" if _resok(r) else "error"),
                "asset_path": apath, "node_id": guid, "result": _resstr(r),
                "undo": "NOT reversible -- a removed node's registered class cannot be read back from the BP API to re-create it; this op is intentionally NOT ledgered (honest coarse/no-undo)."}))
'''

    @mcp.tool()
    def remove_metasound_node(ctx, asset_path: str, node_id: str) -> str:
        """Remove a node from a MetaSound graph by its node_id GUID.

        asset_path: the MetaSound asset object path.
        node_id:    the node's persistent NodeID GUID (from add_metasound_node / build_metasound_graph).

        Edits in place via find_or_begin_building -> remove_node(handle, remove_unused_dependencies=True),
        then saves. NOT UNDOABLE: a node's registered class name cannot be recovered from its handle in the
        BlueprintCallable API, so a removed node cannot be faithfully re-created. This op is intentionally
        NOT ledgered and says so in its result -- use with care on scratch assets only."""
        try:
            return json.dumps(_exec(_REMOVE_NODE_BODY, {"asset_path": asset_path, "node_id": node_id}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #10 connect_metasound_nodes                                      #
    # ================================================================ #
    _CONNECT_BODY = _MS + r'''
apath = PARAMS["asset_path"]
fg = PARAMS["from_node_id"]; fo = PARAMS["from_output"]
tg = PARAMS["to_node_id"]; ti = PARAMS["to_input"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        try:
            fh = _node_from_guid(fg); th = _node_from_guid(tg)
        except Exception as e:
            fh = None; th = None
        if fh is None or not b.contains_node(fh):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "from_node_id not found: %s" % fg}))
        elif th is None or not b.contains_node(th):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "to_node_id not found: %s" % tg}))
        else:
            oh, ro = b.find_node_output_by_name(fh, fo)
            ih, ri = b.find_node_input_by_name(th, ti)
            if not _resok(ro):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "output pin not found: %s on %s" % (fo, fg)}))
            elif not _resok(ri):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "input pin not found: %s on %s" % (ti, tg)}))
            elif b.node_input_is_connected(ih):
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "target input %s on %s is already connected; disconnect it first (this reversible op only wires an UNCONNECTED input so its inverse stays faithful)" % (ti, tg)}))
            else:
                r = b.connect_nodes(oh, ih)
                if not _resok(r):
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "connect_nodes FAILED: %s (type mismatch?)" % _resstr(r)}))
                else:
                    _save(apath)
                    _ledger().append({"op": "metasound_connect", "asset_path": apath,
                        "to_node_id": tg, "to_input_name": ti})
                    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath,
                        "from": {"node_id": fg, "output": fo}, "to": {"node_id": tg, "input": ti},
                        "connected": bool(b.nodes_are_connected(oh, ih)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def connect_metasound_nodes(ctx, asset_path: str, from_node_id: str, from_output: str,
                                to_node_id: str, to_input: str) -> str:
        """Wire a node output pin to another node's input pin in a MetaSound graph.

        asset_path:   the MetaSound asset object path.
        from_node_id: source node's NodeID GUID.
        from_output:  the source node's output pin name (e.g. 'Audio' on a Sine node).
        to_node_id:   destination node's NodeID GUID.
        to_input:     the destination node's input pin name (e.g. 'In' on a mixer).

        Edits in place: find_or_begin_building -> find_node_output_by_name + find_node_input_by_name ->
        connect_nodes, then saves. REFUSES a target input that is already connected, so the inverse stays
        faithful (mirrors add_wave_player_to_cue's refuse-if-nonempty trick). FAILS cleanly on a missing pin
        or a type mismatch.

        Ledgers NEW op 'metasound_connect' {asset_path, to_node_id, to_input_name}. Inverse:
        find_or_begin_building -> reconstruct to-node handle -> find_node_input_by_name ->
        disconnect_node_input -> save (restores the input to unconnected, its exact prior state)."""
        params = {"asset_path": asset_path, "from_node_id": from_node_id, "from_output": from_output,
                  "to_node_id": to_node_id, "to_input": to_input}
        try:
            return json.dumps(_exec(_CONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #11 disconnect_metasound_nodes                                   #
    # ================================================================ #
    _DISCONNECT_BODY = _MS + r'''
apath = PARAMS["asset_path"]
fg = PARAMS["from_node_id"]; fo = PARAMS["from_output"]
tg = PARAMS["to_node_id"]; ti = PARAMS["to_input"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        try:
            fh = _node_from_guid(fg); th = _node_from_guid(tg)
        except Exception as e:
            fh = None; th = None
        if fh is None or not b.contains_node(fh) or th is None or not b.contains_node(th):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "from/to node_id not found"}))
        else:
            oh, ro = b.find_node_output_by_name(fh, fo)
            ih, ri = b.find_node_input_by_name(th, ti)
            if not (_resok(ro) and _resok(ri)):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "output/input pin not found"}))
            elif not b.nodes_are_connected(oh, ih):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "those pins are not connected"}))
            else:
                r = b.disconnect_nodes(oh, ih)
                _save(apath)
                if _resok(r):
                    _ledger().append({"op": "metasound_disconnect", "asset_path": apath,
                        "from_node_id": fg, "from_output_name": fo, "to_node_id": tg, "to_input_name": ti})
                print("@@UMCP@@" + json.dumps({"status": ("success" if _resok(r) else "error"),
                    "asset_path": apath, "result": _resstr(r),
                    "from": {"node_id": fg, "output": fo}, "to": {"node_id": tg, "input": ti},
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def disconnect_metasound_nodes(ctx, asset_path: str, from_node_id: str, from_output: str,
                                   to_node_id: str, to_input: str) -> str:
        """Break the wire from a node output pin to another node's input pin.

        asset_path/from_node_id/from_output/to_node_id/to_input: identify the exact edge to remove (both
        endpoints, so the inverse can reconnect precisely).

        Edits in place: find_or_begin_building -> disconnect_nodes(output_handle, input_handle), then saves.
        FAILS cleanly if the pins are not actually connected.

        Ledgers NEW op 'metasound_disconnect' {asset_path, from_node_id, from_output_name, to_node_id,
        to_input_name}. Inverse: find_or_begin_building -> reconstruct both handles -> find output/input by
        name -> connect_nodes -> save (faithful because both endpoints are captured)."""
        params = {"asset_path": asset_path, "from_node_id": from_node_id, "from_output": from_output,
                  "to_node_id": to_node_id, "to_input": to_input}
        try:
            return json.dumps(_exec(_DISCONNECT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #12 set_metasound_node_input_default                             #
    # ================================================================ #
    _SET_NODE_DEF_BODY = _MS + r'''
apath = PARAMS["asset_path"]
guid = PARAMS["node_id"]; iname = PARAMS["input_name"]
vtype = PARAMS["value_type"]; value = PARAMS.get("value")
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        try:
            h = _node_from_guid(guid)
        except Exception:
            h = None
        if h is None or not b.contains_node(h):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "node_id not found: %s" % guid}))
        else:
            ih, ri = b.find_node_input_by_name(h, iname)
            if not _resok(ri):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "input pin not found: %s" % iname}))
            else:
                # capture prior override (FAILED result -> no explicit prior default was set)
                plit, pr = b.get_node_input_default(ih)
                had_prior = _resok(pr)
                prior_text = plit.export_text() if had_prior else None
                lit, lerr = _mk_literal(vtype, value)
                if lit is None:
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
                else:
                    r = b.set_node_input_default(ih, lit)
                    if not _resok(r):
                        print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_node_input_default FAILED: %s" % _resstr(r)}))
                    else:
                        _save(apath)
                        _ledger().append({"op": "metasound_set_node_input_default", "asset_path": apath,
                            "node_id": guid, "input_name": iname, "had_prior": had_prior, "prior_literal": prior_text})
                        aft, ra = b.get_node_input_default(ih)
                        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath,
                            "node_id": guid, "input_name": iname, "value_type": vtype, "value": value,
                            "had_prior": had_prior, "new_literal": (aft.export_text() if _resok(ra) else None),
                            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_metasound_node_input_default(ctx, asset_path: str, node_id: str, input_name: str,
                                         value_type: str, value=None) -> str:
        """Set the literal default value on a node's (unconnected) input pin.

        asset_path: the MetaSound asset object path.
        node_id:    the node's NodeID GUID.
        input_name: the input pin name (e.g. 'Frequency' on a Sine node).
        value_type: one of 'Float','Int32','Bool','String','Object','FloatArray','IntArray','BoolArray',
                    'StringArray' (Object takes an asset path as value).
        value:      the literal value (number/bool/str/list, or an asset path for Object).

        Edits in place: find_or_begin_building -> find_node_input_by_name ->
        create_*_meta_sound_literal -> set_node_input_default, then saves.

        Ledgers NEW op 'metasound_set_node_input_default' {asset_path, node_id, input_name, had_prior,
        prior_literal}. prior_literal is captured with get_node_input_default().export_text() BEFORE the set
        (had_prior=False when the pin had no explicit override). Inverse: if had_prior, restore the prior
        literal via import_text + set_node_input_default; else remove_node_input_default -> save."""
        params = {"asset_path": asset_path, "node_id": node_id, "input_name": input_name,
                  "value_type": value_type, "value": value}
        try:
            return json.dumps(_exec(_SET_NODE_DEF_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #13 add_metasound_graph_input                                    #
    # ================================================================ #
    _ADD_GIN_BODY = _MS + r'''
apath = PARAMS["asset_path"]
iname = PARAMS["input_name"]; dtype = PARAMS["data_type"]
is_ctor = bool(PARAMS.get("is_constructor"))
has_def = "default_value" in PARAMS and PARAMS.get("default_value") is not None
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        existing, re0 = b.get_graph_input_names()
        if iname in [str(x) for x in existing]:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "graph input already exists: %s" % iname}))
        else:
            if has_def:
                lit, lerr = _mk_literal(dtype, PARAMS.get("default_value"))
                if lit is None:
                    lit = _default_literal_for_type(dtype)
            else:
                lit = _default_literal_for_type(dtype)
            oh, r = b.add_graph_input_node(iname, dtype, lit, is_ctor)
            if not _resok(r):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_graph_input_node FAILED: %s (bad data_type '%s'?)" % (_resstr(r), dtype)}))
            else:
                _save(apath)
                _ledger().append({"op": "metasound_add_graph_input", "asset_path": apath, "input_name": iname})
                names, rn = b.get_graph_input_names()
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "input_name": iname,
                    "data_type": dtype, "is_constructor": is_ctor, "graph_inputs": [str(x) for x in names],
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_metasound_graph_input(ctx, asset_path: str, input_name: str, data_type: str,
                                  default_value=None, is_constructor: bool = False) -> str:
        """Add a graph-level input (a public parameter) to a MetaSound.

        asset_path:  the MetaSound asset object path.
        input_name:  the new input's name (e.g. 'Gain').
        data_type:   the MetaSound data type Name (e.g. 'Float','Int32','Bool','String','Trigger','Audio').
        default_value: optional literal default (matched to data_type; ignored for types without a scalar
                     literal such as Audio/Trigger).
        is_constructor: whether it is a constructor input (default False).

        Edits in place: find_or_begin_building -> add_graph_input_node(name, data_type, default_literal,
        is_constructor), then saves. FAILS cleanly for a duplicate name or an invalid data_type.

        Ledgers NEW op 'metasound_add_graph_input' {asset_path, input_name}. Inverse:
        find_or_begin_building -> remove_graph_input(input_name) -> save."""
        params = {"asset_path": asset_path, "input_name": input_name, "data_type": data_type,
                  "default_value": default_value, "is_constructor": is_constructor}
        try:
            return json.dumps(_exec(_ADD_GIN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #14 add_metasound_graph_output                                   #
    # ================================================================ #
    _ADD_GOUT_BODY = _MS + r'''
apath = PARAMS["asset_path"]
oname = PARAMS["output_name"]; dtype = PARAMS["data_type"]
is_ctor = bool(PARAMS.get("is_constructor"))
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        existing, re0 = b.get_graph_output_names()
        if oname in [str(x) for x in existing]:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "graph output already exists: %s" % oname}))
        else:
            lit = _default_literal_for_type(dtype)
            ih, r = b.add_graph_output_node(oname, dtype, lit, is_ctor)
            if not _resok(r):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_graph_output_node FAILED: %s (bad data_type '%s'?)" % (_resstr(r), dtype)}))
            else:
                _save(apath)
                _ledger().append({"op": "metasound_add_graph_output", "asset_path": apath, "output_name": oname})
                names, rn = b.get_graph_output_names()
                print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "output_name": oname,
                    "data_type": dtype, "is_constructor": is_ctor, "graph_outputs": [str(x) for x in names],
                    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_metasound_graph_output(ctx, asset_path: str, output_name: str, data_type: str,
                                   is_constructor: bool = False) -> str:
        """Add a graph-level output to a MetaSound.

        asset_path:  the MetaSound asset object path.
        output_name: the new output's name.
        data_type:   the MetaSound data type Name (e.g. 'Audio','Float','Trigger').
        is_constructor: whether it is a constructor output (default False).

        Edits in place: find_or_begin_building -> add_graph_output_node, then saves. FAILS cleanly for a
        duplicate name or an invalid data_type.

        Ledgers NEW op 'metasound_add_graph_output' {asset_path, output_name}. Inverse:
        find_or_begin_building -> remove_graph_output(output_name) -> save."""
        params = {"asset_path": asset_path, "output_name": output_name, "data_type": data_type,
                  "is_constructor": is_constructor}
        try:
            return json.dumps(_exec(_ADD_GOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #15 remove_metasound_graph_member                               #
    # ================================================================ #
    _REMOVE_MEMBER_BODY = _MS + r'''
apath = PARAMS["asset_path"]
kind = (PARAMS.get("kind") or "input").lower()
mname = PARAMS["member_name"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        data_type = None; default_text = None; r = None
        if kind == "input":
            nh, dt, oh2, rf = b.find_graph_input_node(mname)
            if _resok(rf):
                data_type = str(dt)
                dl, rd = b.get_graph_input_default(mname)
                if _resok(rd):
                    default_text = dl.export_text()
            r = b.remove_graph_input(mname)
        elif kind == "output":
            nh, dt, ih2, rf = b.find_graph_output_node(mname)
            if _resok(rf):
                data_type = str(dt)
            r = b.remove_graph_output(mname)
        elif kind == "variable":
            dl, rd = b.get_graph_variable_default(mname)
            if _resok(rd):
                default_text = dl.export_text()
            r = b.remove_graph_variable(mname)
        else:
            r = None
        if r is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "kind must be input/output/variable, got %s" % kind}))
        elif not _resok(r):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "remove %s '%s' FAILED: %s (not found?)" % (kind, mname, _resstr(r))}))
        else:
            _save(apath)
            _ledger().append({"op": "metasound_remove_graph_member", "asset_path": apath, "kind": kind,
                "name": mname, "data_type": data_type, "default_literal": default_text})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "kind": kind,
                "name": mname, "captured_data_type": data_type,
                "undo_note": "inverse re-adds the member with its captured data_type + default; downstream CONNECTIONS are NOT restored (partial). variable data_type is not reflected -> variable re-add may be skipped.",
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_metasound_graph_member(ctx, asset_path: str, member_name: str, kind: str = "input") -> str:
        """Remove a graph-level member (input, output, or variable) from a MetaSound.

        asset_path:  the MetaSound asset object path.
        member_name: the member's name.
        kind:        'input' (default), 'output', or 'variable'.

        Edits in place: find_or_begin_building -> remove_graph_input/remove_graph_output/
        remove_graph_variable, then saves. Captures the member's data_type (inputs/outputs) and default
        literal before removal.

        Ledgers NEW op 'metasound_remove_graph_member' {asset_path, kind, name, data_type, default_literal}.
        Inverse: re-add the member with the captured data_type + default. PARTIAL: any wires that were
        connected to the member are NOT restored, and a variable's data_type is not reflected (so a variable
        re-add may be skipped) -- documented honestly rather than faked."""
        params = {"asset_path": asset_path, "member_name": member_name, "kind": kind}
        try:
            return json.dumps(_exec(_REMOVE_MEMBER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #16 set_metasound_graph_input_default                           #
    # ================================================================ #
    _SET_GIN_DEF_BODY = _MS + r'''
apath = PARAMS["asset_path"]
iname = PARAMS["input_name"]; vtype = PARAMS["value_type"]; value = PARAMS.get("value")
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        existing, re0 = b.get_graph_input_names()
        if iname not in [str(x) for x in existing]:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "graph input not found: %s" % iname}))
        else:
            plit, pr = b.get_graph_input_default(iname)
            had_prior = _resok(pr)
            prior_text = plit.export_text() if had_prior else None
            lit, lerr = _mk_literal(vtype, value)
            if lit is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": lerr}))
            else:
                r = b.set_graph_input_default(iname, lit)
                if not _resok(r):
                    print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_graph_input_default FAILED: %s" % _resstr(r)}))
                else:
                    _save(apath)
                    _ledger().append({"op": "metasound_set_graph_input_default", "asset_path": apath,
                        "input_name": iname, "had_prior": had_prior, "prior_literal": prior_text})
                    aft, ra = b.get_graph_input_default(iname)
                    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "input_name": iname,
                        "value_type": vtype, "value": value, "had_prior": had_prior,
                        "new_literal": (aft.export_text() if _resok(ra) else None), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_metasound_graph_input_default(ctx, asset_path: str, input_name: str,
                                          value_type: str, value=None) -> str:
        """Set the default value of an existing graph-level input.

        asset_path: the MetaSound asset object path.
        input_name: the graph input's name.
        value_type: 'Float','Int32','Bool','String','Object', or an array variant.
        value:      the literal value.

        Edits in place: find_or_begin_building -> set_graph_input_default(name, literal), then saves.

        Ledgers NEW op 'metasound_set_graph_input_default' {asset_path, input_name, had_prior,
        prior_literal} (prior captured via get_graph_input_default().export_text()). Inverse: if had_prior,
        restore the prior literal via import_text + set_graph_input_default; else reset_graph_input_defaults
        -> save."""
        params = {"asset_path": asset_path, "input_name": input_name, "value_type": value_type, "value": value}
        try:
            return json.dumps(_exec(_SET_GIN_DEF_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #17 add_metasound_variable                                       #
    # ================================================================ #
    _ADD_VAR_BODY = _MS + r'''
apath = PARAMS["asset_path"]
vname = PARAMS["variable_name"]; dtype = PARAMS["data_type"]
has_def = PARAMS.get("default_value") is not None
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        if has_def:
            lit, lerr = _mk_literal(dtype, PARAMS.get("default_value"))
            if lit is None:
                lit = _default_literal_for_type(dtype)
        else:
            lit = _default_literal_for_type(dtype)
        r = b.add_graph_variable(vname, dtype, lit)
        if not _resok(r):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add_graph_variable FAILED: %s (dup name or bad data_type '%s'?)" % (_resstr(r), dtype)}))
        else:
            _save(apath)
            _ledger().append({"op": "metasound_add_variable", "asset_path": apath, "variable_name": vname})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "variable_name": vname,
                "data_type": dtype, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_metasound_variable(ctx, asset_path: str, variable_name: str, data_type: str,
                               default_value=None) -> str:
        """Add a graph variable to a MetaSound (a stateful value shared across the graph).

        asset_path:    the MetaSound asset object path.
        variable_name: the variable's name.
        data_type:     the MetaSound data type Name (e.g. 'Float','Int32','Bool').
        default_value: optional literal default (matched to data_type).

        Edits in place: find_or_begin_building -> add_graph_variable(name, data_type, default_literal), then
        saves. Create get/set/get-delayed accessor nodes for it with add_metasound_variable_node.

        Ledgers NEW op 'metasound_add_variable' {asset_path, variable_name}. Inverse:
        find_or_begin_building -> remove_graph_variable(variable_name) -> save."""
        params = {"asset_path": asset_path, "variable_name": variable_name, "data_type": data_type,
                  "default_value": default_value}
        try:
            return json.dumps(_exec(_ADD_VAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #18 add_metasound_variable_node                                  #
    # ================================================================ #
    _ADD_VAR_NODE_BODY = _MS + r'''
apath = PARAMS["asset_path"]
vname = PARAMS["variable_name"]; acc = (PARAMS.get("accessor") or "get").lower()
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        if acc == "set":
            h, r = b.add_graph_variable_set_node(vname)
        elif acc in ("get_delayed", "getdelayed", "delayed"):
            h, r = b.add_graph_variable_get_delayed_node(vname)
        else:
            h, r = b.add_graph_variable_get_node(vname)
        if not _resok(r):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "add variable %s node FAILED: %s (variable '%s' exists?)" % (acc, _resstr(r), vname)}))
        else:
            guid = _guid_of(h)
            _save(apath)
            _ledger().append({"op": "metasound_add_node", "asset_path": apath, "node_id": guid})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "variable_name": vname,
                "accessor": acc, "node_id": guid, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_metasound_variable_node(ctx, asset_path: str, variable_name: str, accessor: str = "get") -> str:
        """Add a variable accessor node (get / set / get-delayed) for an existing graph variable.

        asset_path:    the MetaSound asset object path.
        variable_name: the graph variable's name (create it first with add_metasound_variable).
        accessor:      'get' (default), 'set', or 'get_delayed'.

        Edits in place: find_or_begin_building -> add_graph_variable_get_node / set_node /
        get_delayed_node, then saves. Returns the node's NodeID GUID (wire it like any other node).

        Ledgers the SAME 'metasound_add_node' op {asset_path, node_id} as add_metasound_node (a variable
        node is a normal node -> inverse = reconstruct handle + remove_node)."""
        params = {"asset_path": asset_path, "variable_name": variable_name, "accessor": accessor}
        try:
            return json.dumps(_exec(_ADD_VAR_NODE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #19 set_metasound_interface  (add / remove)                     #
    # ================================================================ #
    _SET_IFACE_BODY = _MS + r'''
apath = PARAMS["asset_path"]
iface = PARAMS["interface_name"]; action = (PARAMS.get("action") or "add").lower()
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        try:
            was_present = bool(b.interface_is_declared(iface))
        except Exception:
            was_present = None
        if action == "remove":
            r = b.remove_interface(iface)
        else:
            r = b.add_interface(iface)
        if not _resok(r):
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "%s_interface FAILED: %s (interface '%s' registered?)" % (action, _resstr(r), iface)}))
        else:
            _save(apath)
            try:
                now_present = bool(b.interface_is_declared(iface))
            except Exception:
                now_present = None
            changed = (was_present is None) or (now_present != was_present)
            if changed:
                _ledger().append({"op": "metasound_set_interface", "asset_path": apath,
                    "interface_name": iface, "added": (action == "add")})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "interface_name": iface,
                "action": action, "was_present": was_present, "now_present": now_present,
                "ledgered": changed, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_metasound_interface(ctx, asset_path: str, interface_name: str, action: str = "add") -> str:
        """Declare or remove a MetaSound interface on the graph.

        asset_path:     the MetaSound asset object path.
        interface_name: the interface name (e.g. 'UE.Attenuation', 'UE.Source.OneShot', 'UE.Spatialization').
        action:         'add' (default) declares it; 'remove' undeclares it.

        Edits in place: find_or_begin_building -> add_interface / remove_interface, then saves. FAILS cleanly
        for an unregistered interface name. Only ledgers if the declared-set actually changed.

        Ledgers NEW op 'metasound_set_interface' {asset_path, interface_name, added}. Inverse: if added,
        remove_interface; else add_interface -> save."""
        params = {"asset_path": asset_path, "interface_name": interface_name, "action": action}
        try:
            return json.dumps(_exec(_SET_IFACE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #21 layout_metasound_graph  (cosmetic, NO ledger)               #
    # ================================================================ #
    _LAYOUT_BODY = _MS + r'''
apath = PARAMS["asset_path"]
positions = PARAMS.get("positions") or []
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b, berr = _fobb(ms)
    if berr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": berr}))
    else:
        es = _es(); done = []; fail = []
        for p in positions:
            gid = p.get("node_id"); x = float(p.get("x", 0.0)); y = float(p.get("y", 0.0))
            try:
                h = _node_from_guid(gid)
                if not b.contains_node(h):
                    fail.append({"node_id": gid, "err": "not found"}); continue
                r = es.set_node_location(b, h, unreal.Vector2D(x, y))
                (done if _resok(r) else fail).append({"node_id": gid, "x": x, "y": y})
            except Exception as e:
                fail.append({"node_id": gid, "err": str(e)[:80]})
        _save(apath)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "positioned": done, "failed": fail,
            "undo": "NOT ledgered -- node position is cosmetic editor metadata with no reflected getter to capture prior positions (honest non-reversible)."}))
'''

    @mcp.tool()
    def layout_metasound_graph(ctx, asset_path: str, positions: list = None) -> str:
        """Set cosmetic editor positions for MetaSound graph nodes.

        asset_path: the MetaSound asset object path.
        positions:  list of {node_id, x, y} placements (node_id = a node's NodeID GUID).

        Edits in place: find_or_begin_building -> MetaSoundEditorSubsystem.set_node_location(builder, handle,
        Vector2D(x, y)) per node, then saves. Positions are cosmetic editor-graph metadata.

        NOT UNDOABLE: there is no reflected get-node-location, so prior positions cannot be captured. This op
        is intentionally NOT ledgered and says so -- honest non-reversible cosmetic metadata."""
        params = {"asset_path": asset_path, "positions": positions or []}
        try:
            return json.dumps(_exec(_LAYOUT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #20 build_metasound_graph  (whole-graph, one call -> new asset) #
    # ================================================================ #
    _BUILD_GRAPH_BODY = _MS + r'''
name = PARAMS["name"]
pkg = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
kind = (PARAMS.get("source_type") or "source").lower()
fmt = (PARAMS.get("output_format") or "mono").lower()
one_shot = bool(PARAMS.get("is_one_shot", True))
spec = PARAMS.get("graph") or {}
apath = pkg + "/" + name + "." + name
if EAL.does_asset_exist(apath):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s (build_metasound_graph only creates NEW assets)" % apath}))
else:
    fmt_map = {"mono": unreal.MetaSoundOutputAudioFormat.MONO, "stereo": unreal.MetaSoundOutputAudioFormat.STEREO,
               "quad": unreal.MetaSoundOutputAudioFormat.QUAD, "5.1": unreal.MetaSoundOutputAudioFormat.FIVE_DOT_ONE,
               "7.1": unreal.MetaSoundOutputAudioFormat.SEVEN_DOT_ONE}
    bss = _bss(); es = _es()
    created_dir = not EAL.does_directory_exist(pkg)
    if kind == "patch":
        tup = bss.create_patch_builder(name + "_Builder"); builder = tup[0]; mkres = tup[-1]
    else:
        tup = bss.create_source_builder(name + "_Builder", fmt_map.get(fmt, unreal.MetaSoundOutputAudioFormat.MONO), one_shot)
        builder = tup[0]; mkres = tup[-1]
    steps = {"nodes": [], "graph_inputs": [], "graph_outputs": [], "variables": [], "interfaces": [], "connections": [], "errors": []}
    idmap = {}
    if builder is None or not _resok(mkres):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "builder create failed: %s" % _resstr(mkres)}))
    else:
        # interfaces
        for iface in (spec.get("interfaces") or []):
            r = builder.add_interface(iface)
            steps["interfaces"].append({"name": iface, "ok": _resok(r)})
        # graph inputs
        for gi in (spec.get("graph_inputs") or []):
            nm = gi.get("name"); dt = gi.get("data_type")
            if gi.get("default") is not None:
                lit, le = _mk_literal(dt, gi.get("default"))
                if lit is None: lit = _default_literal_for_type(dt)
            else:
                lit = _default_literal_for_type(dt)
            oh, r = builder.add_graph_input_node(nm, dt, lit, bool(gi.get("is_constructor")))
            steps["graph_inputs"].append({"name": nm, "ok": _resok(r)})
            if not _resok(r): steps["errors"].append("graph_input %s failed" % nm)
        # graph outputs
        for go in (spec.get("graph_outputs") or []):
            nm = go.get("name"); dt = go.get("data_type")
            lit = _default_literal_for_type(dt)
            ih, r = builder.add_graph_output_node(nm, dt, lit, bool(go.get("is_constructor")))
            steps["graph_outputs"].append({"name": nm, "ok": _resok(r)})
            if not _resok(r): steps["errors"].append("graph_output %s failed" % nm)
        # variables
        for v in (spec.get("variables") or []):
            nm = v.get("name"); dt = v.get("data_type")
            if v.get("default") is not None:
                lit, le = _mk_literal(dt, v.get("default"))
                if lit is None: lit = _default_literal_for_type(dt)
            else:
                lit = _default_literal_for_type(dt)
            r = builder.add_graph_variable(nm, dt, lit)
            steps["variables"].append({"name": nm, "ok": _resok(r)})
            if not _resok(r): steps["errors"].append("variable %s failed" % nm)
        # nodes (keep user-id -> handle map)
        handles = {}
        for nd in (spec.get("nodes") or []):
            uid = str(nd.get("id"))
            cn = _mk_classname(nd.get("namespace") or "UE", nd.get("name"), nd.get("variant") or "")
            h, r = builder.add_node_by_class_name(cn, int(nd.get("major_version") or 1))
            if _resok(r):
                handles[uid] = h; g = _guid_of(h); idmap[uid] = g
                steps["nodes"].append({"id": uid, "ok": True, "node_id": g})
                # per-node input defaults
                for iname, spec2 in (nd.get("defaults") or {}).items():
                    ih, ri = builder.find_node_input_by_name(h, iname)
                    if _resok(ri):
                        lit, le = _mk_literal(spec2.get("type"), spec2.get("value"))
                        if lit is not None:
                            builder.set_node_input_default(ih, lit)
                # optional location
                if nd.get("x") is not None and nd.get("y") is not None:
                    try: es.set_node_location(builder, h, unreal.Vector2D(float(nd.get("x")), float(nd.get("y"))))
                    except Exception: pass
            else:
                steps["nodes"].append({"id": uid, "ok": False}); steps["errors"].append("node %s (%s) failed" % (uid, nd.get("name")))
        # connections
        for c in (spec.get("connections") or []):
            ok = False; detail = ""
            try:
                if c.get("graph_input") is not None and c.get("to_node") is not None:
                    th = handles.get(str(c.get("to_node")))
                    ih, ri = builder.find_node_input_by_name(th, c.get("to_input"))
                    if _resok(ri):
                        r = builder.connect_node_input_to_graph_input(c.get("graph_input"), ih); ok = _resok(r)
                elif c.get("graph_output") is not None and c.get("from_node") is not None:
                    fh = handles.get(str(c.get("from_node")))
                    oh, ro = builder.find_node_output_by_name(fh, c.get("from_output"))
                    if _resok(ro):
                        r = builder.connect_node_output_to_graph_output(c.get("graph_output"), oh); ok = _resok(r)
                else:
                    fh = handles.get(str(c.get("from_node"))); th = handles.get(str(c.get("to_node")))
                    oh, ro = builder.find_node_output_by_name(fh, c.get("from_output"))
                    ih, ri = builder.find_node_input_by_name(th, c.get("to_input"))
                    if _resok(ro) and _resok(ri):
                        r = builder.connect_nodes(oh, ih); ok = _resok(r)
            except Exception as e:
                detail = str(e)[:80]
            steps["connections"].append({"conn": c, "ok": ok, "detail": detail})
            if not ok: steps["errors"].append("connection failed: %s %s" % (json.dumps(c), detail))
        # persist to a NEW asset
        doc, br = es.build_to_asset(builder, PARAMS.get("author") or "MCP", name, pkg)
        ok = _resok(br) and EAL.does_asset_exist(apath)
        obj = EAL.load_asset(apath) if ok else None
        if not ok or obj is None:
            if created_dir and EAL.does_directory_exist(pkg):
                try: EAL.delete_directory(pkg)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "build_to_asset failed: %s" % _resstr(br), "steps": steps}))
        else:
            _close(obj); _save(apath)
            _ledger().append({"op": "create_asset", "asset_path": apath, "package_path": pkg, "created_dir": created_dir})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": apath, "class": obj.get_class().get_name(),
                "kind": kind, "id_to_node_id": idmap, "summary": {k: len(v) for k, v in steps.items() if isinstance(v, list)},
                "errors": steps["errors"], "steps": steps, "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def build_metasound_graph(ctx, name: str, graph: dict, package_path: str = "/Game/MCP_Scratch",
                              source_type: str = "source", output_format: str = "mono",
                              is_one_shot: bool = True, author: str = "MCP") -> str:
        """Build a whole MetaSound graph in one call and persist it to a NEW asset.

        name/package_path/source_type/output_format/is_one_shot/author: as create_metasound.
        graph: a dict describing the graph:
          {
            "interfaces":    ["UE.Attenuation", ...],                       (optional)
            "graph_inputs":  [{"name","data_type","default"?,"is_constructor"?}, ...],
            "graph_outputs": [{"name","data_type","is_constructor"?}, ...],
            "variables":     [{"name","data_type","default"?}, ...],
            "nodes":         [{"id","name","namespace"?,"variant"?,"major_version"?,
                               "defaults"?:{"<input>":{"type","value"}}, "x"?,"y"?}, ...],
            "connections":   [ {"from_node":"id","from_output":"Audio","to_node":"id","to_input":"In"}
                               | {"graph_input":"Gain","to_node":"id","to_input":"In"}
                               | {"from_node":"id","from_output":"Audio","graph_output":"UE.OutputFormat.Mono.Audio:0"} ]
          }
        node "id" is a caller-chosen key; the response 'id_to_node_id' maps each to its persistent NodeID
        GUID. Builds everything on one transient builder (create_source_builder/create_patch_builder ->
        add nodes/inputs/outputs/vars -> defaults -> connections -> optional set_node_location) then
        build_to_asset. Per-step ok/errors are reported; a failed step does not abort the build.

        Ledgers the generic 'create_asset' op (reused; inverse = delete the asset) -- the whole graph is
        undone by deleting the new asset. Only creates NEW assets (refuses an existing path)."""
        params = {"name": name, "graph": graph, "package_path": package_path, "source_type": source_type,
                  "output_format": output_format, "is_one_shot": is_one_shot, "author": author}
        try:
            return json.dumps(_exec(_BUILD_GRAPH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---------------------------------------------------------------- #
    # This module registers NO `undo` tool (editor_level.py owns the unified `undo`). create_metasound and
    # build_metasound_graph reuse the folded 'create_asset' inverse. The incremental ops push NEW ledger
    # entries whose inverses the coordinator folds into editor_level.undo (schemas in the module docstring +
    # the build report). remove_metasound_node and layout_metasound_graph are honestly NON-ledgered.
    # ---------------------------------------------------------------- #
