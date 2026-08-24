"""UserTools :: AUDIO C++-backed -- MetaSound DISCOVERY (#1-4) + SoundSubmix parent WRITER (#38)

Wires the AUDIO C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_AudioCpp.cpp. These are the audio features the
pure-Python audio tools (metasound_read/write.py, sound_write.py, audio_reparent.py) could NOT reach:

  * MetaSound nodes are NOT UClasses -> dir(unreal)/issubclass cannot enumerate them, and the frontend
    search/registry singletons (ISearchEngine / IDataTypeRegistry / IInterfaceRegistry) are C++-only
    (not BlueprintCallable). Hence the four DISCOVERY reads live in C++.
  * USoundSubmix.parent_submix and SoundSubmixBase.child_submixes are EditConst (managed by the submix
    graph editor) -> pure-Python set_editor_property REFUSES them (verified live -- see audio_reparent.py,
    which returns "unsupported"). The C++ handler calls the engine's own public SetParentSubmix().

  READS (no ledger -- non-mutating):
    * search_metasound_nodes     -- ISearchEngine::FindAllClasses -> name/category-filtered node catalog.
    * describe_metasound_node    -- one node class's metadata + inputs/outputs (+ input default literals).
    * list_metasound_datatypes   -- IDataTypeRegistry::GetRegisteredDataTypeNames -> filtered names.
    * list_metasound_interfaces  -- FindAllInterfaceVersions (+ IInterfaceRegistry) -> interfaces + member counts.

  WRITE (ledger -- reversible; inverse folds into editor_level.undo):
    * set_submix_parent          -- reparent a USoundSubmix under another (or detach to root) via the C++
                                    direct-write handler. SUPERSEDES the pure-Python audio_reparent.py stub
                                    (which returns "unsupported"). Captures prior_parent_path. Ledger op
                                    "audio_set_submix_parent".

SUPERSEDES: audio_reparent.py already defines a `set_submix_parent` tool that returns status "unsupported"
(the EditConst wall). This module's `set_submix_parent` is the real, C++-backed replacement. The coordinator
should remove/disable the audio_reparent.py stub so the two do not collide (same tool name). The OTHER two
audio_reparent.py tools (set_sound_class_parent / set_audio_effect_chain) are pure-Python and stay.

Each tool hasattr-guards its future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with MCPReflection_AudioCpp.cpp -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied VERBATIM from
the gold-standard world_ext_cpp.py / niagara_runtime_cpp.py.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). set_submix_parent
appends ONE ledger op whose inverse the coordinator folds into editor_level.undo:
    audio_set_submix_parent  {submix_path, prior_parent_path}
        -> set_submix_parent_json(submix_path, prior_parent_path)   (empty prior -> detach to root)

The MetaSound MRL method names are tried in a few UHT snake-case spellings (meta_sound / metasound) so the
tools bind whichever the plugin exports.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a snippet
local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from world_ext_cpp.py / niagara_runtime_cpp.py) ---
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    # _mfind resolves an MRL method by trying candidate snake spellings (UHT acronym-casing may render
    # "MetaSound" as meta_sound OR metasound); returns the bound method or None.
    _HELP = r'''
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
def _rl():
    return getattr(unreal, "MCPReflectionLibrary", None)
def _mfind(names):
    rl = _rl()
    if rl is None:
        return None
    for n in names:
        if hasattr(rl, n):
            return getattr(rl, n)
    return None
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _save(path):
    if not path:
        return False
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        return True
    except Exception:
        return False
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ handler in MCPReflection_AudioCpp.cpp "
            "(rebuild the UnrealMCP plugin DLL to enable it).")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _SEARCH_BODY = _HELP + r'''
fn = _mfind(["search_meta_sound_nodes_json", "search_metasound_nodes_json"])
if fn is None:
    print("@@UMCP@@" + json.dumps(_defer("search_metasound_nodes")))
else:
    res = _decode(fn(PARAMS.get("filter", ""), PARAMS.get("category", ""), int(PARAMS.get("max_results", 0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def search_metasound_nodes(ctx, filter: str = "", category: str = "", max_results: int = 100) -> str:
        """Search the MetaSound node-class catalog (registered frontend classes). Read-only.

        filter:      case-insensitive substring matched against the class full-name (namespace.name.variant).
        category:    case-insensitive substring matched against the class category hierarchy ('A|B|C').
        max_results: cap on returned entries (<= 0 -> uncapped; default 100).

        MetaSound node classes are frontend registry entries, NOT UClasses, so stock Python cannot enumerate
        them -- this reads Metasound::Frontend::ISearchEngine::FindAllClasses(Highest) in C++. Use the returned
        'class_name' (or namespace/name/variant) with describe_metasound_node or with the MetaSound builder's
        add_node_by_class_name.

        Returns {total_classes, scanned, count, nodes:[{class_name, namespace, name, variant, display_name,
        category, description, class_type, version}]}. Needs the C++ handler (inert until the plugin DLL is
        rebuilt with MCPReflection_AudioCpp.cpp)."""
        params = {"filter": filter or "", "category": category or "", "max_results": int(max_results or 0)}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _DESCRIBE_BODY = _HELP + r'''
fn = _mfind(["describe_meta_sound_node_json", "describe_metasound_node_json"])
if fn is None:
    print("@@UMCP@@" + json.dumps(_defer("describe_metasound_node")))
else:
    res = _decode(fn(PARAMS.get("namespace", ""), PARAMS.get("name", ""), PARAMS.get("variant", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def describe_metasound_node(ctx, name: str, namespace: str = "", variant: str = "") -> str:
        """Describe one MetaSound node class -- its inputs / outputs / metadata. Read-only.

        name:      the node class name (required), e.g. 'Sine', 'Add', 'InterpTo'.
        namespace: the class namespace (e.g. 'UE'); optional but disambiguates.
        variant:   the class variant (types the node operates on); optional.

        Resolves the highest-version class via ISearchEngine::FindClassesWithName, then the full
        FMetasoundFrontendClass via the node class registry (C++-only). Input entries carry their default
        literal (as a string).

        Returns {class_name, namespace, name, variant, display_name, description, category, author, class_type,
        version, keywords:[...], inputs:[{name, data_type, default}], input_count, outputs:[{name, data_type}],
        output_count}. Needs the C++ handler (inert until built)."""
        params = {"name": name or "", "namespace": namespace or "", "variant": variant or ""}
        try:
            return json.dumps(_exec(_DESCRIBE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _DATATYPES_BODY = _HELP + r'''
fn = _mfind(["list_meta_sound_data_types_json", "list_metasound_data_types_json", "list_meta_sound_datatypes_json"])
if fn is None:
    print("@@UMCP@@" + json.dumps(_defer("list_metasound_datatypes")))
else:
    res = _decode(fn(PARAMS.get("filter", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_metasound_datatypes(ctx, filter: str = "") -> str:
        """List the registered MetaSound data-type names (Float, Int32, Audio, Trigger, ...). Read-only.

        filter: case-insensitive substring on the data-type name (empty -> all).

        Reads Metasound::Frontend::IDataTypeRegistry::GetRegisteredDataTypeNames in C++ (not Python-reachable).
        Use a returned name as the data_type when adding a graph input/output or a variable.

        Returns {total, count, data_types:[name, ...]} (sorted). Needs the C++ handler (inert until built)."""
        params = {"filter": filter or ""}
        try:
            return json.dumps(_exec(_DATATYPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _INTERFACES_BODY = _HELP + r'''
fn = _mfind(["list_meta_sound_interfaces_json", "list_metasound_interfaces_json"])
if fn is None:
    print("@@UMCP@@" + json.dumps(_defer("list_metasound_interfaces")))
else:
    res = _decode(fn(PARAMS.get("filter", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_metasound_interfaces(ctx, filter: str = "") -> str:
        """List the registered MetaSound interfaces + their member counts. Read-only.

        filter: case-insensitive substring on the interface name (empty -> all).

        Reads ISearchEngine::FindAllInterfaceVersions and resolves each via IInterfaceRegistry for exact-version
        member counts (C++-only). Use a returned interface name with the MetaSound builder's
        add_interface/remove_interface.

        Returns {total, count, interfaces:[{name, version, version_major, version_minor, input_count,
        output_count, environment_count, resolved}]}. Needs the C++ handler (inert until built)."""
        params = {"filter": filter or ""}
        try:
            return json.dumps(_exec(_INTERFACES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITE (ledger -- reversible). Inverse folds into editor_level.undo.
    # SUPERSEDES the audio_reparent.py "unsupported" set_submix_parent stub.
    # ================================================================== #

    _SUBMIX_PARENT_BODY = _HELP + r'''
fn = _mfind(["set_submix_parent_json"])
if fn is None:
    print("@@UMCP@@" + json.dumps(_defer("set_submix_parent")))
else:
    submix_path = PARAMS.get("submix_path", "")
    parent_path = PARAMS.get("parent_path") or ""
    res = _decode(fn(submix_path, parent_path))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        # Persist every touched package (submix + old/new parents) the C++ marked dirty.
        saved = []
        for p in (res.get("touched_paths") or []):
            if _save(p):
                saved.append(p)
        entry = {"op": "audio_set_submix_parent",
                 "submix_path": res.get("submix_path", submix_path),
                 "prior_parent_path": res.get("prior_parent_path", "")}
        _ledger().append(entry)
        out = {"status": "success",
               "submix": res.get("submix"),
               "submix_path": res.get("submix_path"),
               "prior_parent_path": res.get("prior_parent_path"),
               "new_parent_path": res.get("new_parent_path"),
               "readback_parent_path": res.get("readback_parent_path"),
               "detached": bool(res.get("detached")),
               "saved_paths": saved,
               "ledger_entry": entry, "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def set_submix_parent(ctx, submix_path: str, parent_path: str = None) -> str:
        """Reparent a USoundSubmix under another submix, or detach it to root. REVERSIBLE ledgered write.

        submix_path: object/package path of the SoundSubmix to move (must support a parent -- a
                     USoundSubmixWithParentBase; endpoint submixes have no parent).
        parent_path: object/package path of the new parent SoundSubmix, or None/empty to detach to root.

        C++-BACKED (supersedes the pure-Python audio_reparent.py stub, which returns 'unsupported' because
        parent_submix/child_submixes are EditConst). Calls the engine's public
        USoundSubmixWithParentBase::SetParentSubmix, which detaches the submix from its old parent's
        child_submixes and adds it to the new parent's child_submixes. Refuses a self-parent or a cycle (walks
        the new parent's parent chain). Captures prior_parent_path, saves every touched package, and appends the
        inverse to the per-session ledger for editor_level.undo.

        Returns {submix, submix_path, prior_parent_path, new_parent_path, readback_parent_path, detached,
        saved_paths, ledger_depth}.

        Ledgered op 'audio_set_submix_parent' {submix_path, prior_parent_path}; inverse:
        set_submix_parent(submix_path, prior_parent_path) (empty prior -> detach to root). Needs the C++ handler
        (inert until the plugin DLL is rebuilt with MCPReflection_AudioCpp.cpp)."""
        params = {"submix_path": submix_path, "parent_path": parent_path}
        try:
            return json.dumps(_exec(_SUBMIX_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # NO `undo` tool here; editor_level.py owns the unified `undo`. Ledger op + inverse:
    #   audio_set_submix_parent  {submix_path, prior_parent_path}
    #       -> set_submix_parent_json(submix_path, prior_parent_path)  (empty prior -> detach to root)
