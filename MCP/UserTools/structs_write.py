"""UserTools :: Blueprint Structs & Enums (WRITE)  (spec: docs/spec/structs.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE-wave WRITE batch, the
mutating counterpart to the read-only structs.py. Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py (via the create-wave reference ai_write.py).

What this build exposes (both factories verified NON-MODAL live vs TestMCPSetup, UE 5.8.1):
  * UserDefinedStruct created non-interactively via
      unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, package_path,
                                                              unreal.UserDefinedStruct, unreal.StructureFactory())
    StructureFactory.supported_class == UserDefinedStruct; the scripting create_asset path does NOT
    invoke any ConfigureProperties dialog -> no modal EVER pops (bounded-probed: isolated
    create+save+delete clean, no hang). A fresh UserDefinedStruct ships with ONE default member
    (MemberVar_0_<GUID>, cpp_type bool, flags Edit/BlueprintVisible) -- confirmed by reading it back
    with the native MCPReflectionLibrary.get_struct_fields_json handler (same one structs.py uses).
  * UserDefinedEnum created the same way with unreal.EnumFactory() (supported_class == UserDefinedEnum,
    no dialog -> no modal, bounded-probed clean). is_Enum == True.

Implemented (validated live, session=agentA, editor left CLEAN, ledger depth 0):
  - create_user_defined_struct (WRITE; ledgered op "create_asset"; inverse ALREADY in editor_level.undo)
  - create_user_defined_enum   (WRITE; ledgered op "create_asset"; inverse ALREADY in editor_level.undo)

Both creates push the SHARED generic ledger op
  {op: create_asset, asset_path, package_path, created_dir}
whose inverse (close asset editors + GC + delete_asset [+ rmdir package_path if we created it and it is
now empty], with the follow-up separate-call sweep for settle-timing) is ALREADY folded into
editor_level.undo. So there is NOTHING NEW for the coordinator to fold from this module.

DEFERRED (editor-only in this build's Python surface; refused rather than shipping unrevertable/fake writes):
  - add_struct_field / set_struct_field / remove_struct_field (struct member authoring), AND
  - add_enum_enumerator / set_enum_display_name / remove_enum_enumerator (enum value authoring):
      Probed DEFINITIVELY live. The member/enumerator authoring APIs are FStructureEditorUtils /
      FEnumEditorUtils, which are EDITOR-ONLY C++ and NOT Python-exposed:
        * unreal.StructureEditorUtils / unreal.EnumEditorUtils do NOT exist (hasattr == False).
        * The created UserDefinedStruct object exposes NO field mutator (no add_variable / add_member /
          etc.; dir() yields only generic get/set_editor_property accessors). Its members live in the
          C++-only UUserDefinedStructEditorData (reflected as the opaque 'EditorData' UPROPERTY) which is
          not safely/faithfully writable from Python.
        * The created UserDefinedEnum object exposes NO enumerator mutator and not even num_enums() from
          Python; enumerator display-name/value editing is FEnumEditorUtils' job.
        * MCPReflectionLibrary has get_struct_fields_json (READ) but NO struct-field/enum WRITE handler.
      MISSING PYTHON SURFACE (for a future Wave-3 C++ round-trip, mirroring the curves SetCurveKeysJson
      proposal): a native handler such as
        MCPReflectionLibrary.AddStructVariable(UUserDefinedStruct*, name, pinTypeJson)  (+ Rename/Remove)
        MCPReflectionLibrary.AddEnumerator(UUserDefinedEnum*, displayName)              (+ SetDisplayName/Remove)
      wrapping FStructureEditorUtils::AddVariable / FEnumEditorUtils::AddNewEnumeratorForUserDefinedEnum
      (+ ModifyStructData / BroadcastPreChange/PostChange + MarkPackageDirty). PROPOSED faithful ledger
      ops when that lands: set_struct_fields{asset_path, prior_fields:[...]} and
      set_enum_enumerators{asset_path, prior_enumerators:[...]} (prior read via get_struct_fields_json /
      an enum-read handler before the edit -> reversible). NOT attempted against the shared editor.

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified `undo`).
It only reuses the already-folded generic "create_asset" inverse; it introduces NO new ledger op.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
# bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never assign a
# snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
# success/user_code/code_obj (they are the C++ wrapper's own locals -> clobbering them wedges capture).


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

    # --- Python-side TypeJson builders (host process, NOT snippet bodies) -------- #
    # A field type is COMPLEX (needs the C++ add_struct_field_ex / change_struct_field_type
    # TypeJson path) when it names an object/class/soft/interface/struct/enum category, OR
    # carries a type_path, OR uses an array|set|map container. Bare scalars ("int", "float",
    # "vector", ...) take the scalar fast-path (add_struct_field) or a bare-string retype.
    _COMPLEX_STRUCT_CATS = {"object", "class", "softobject", "softclass", "interface",
                            "struct", "enum"}

    def _struct_type_is_complex(type_name, type_path, container):
        if type_path:
            return True
        if container and str(container).lower() not in ("none", ""):
            return True
        if type_name and str(type_name).lower() in _COMPLEX_STRUCT_CATS:
            return True
        return False

    def _build_struct_type_json(type_name, type_path, container, value_type=None,
                                value_type_path=None):
        """Assemble a TypeJson dict for add_struct_field_ex / change_struct_field_type."""
        tj = {"category": type_name}
        if type_path:
            tj["type_path"] = type_path
        cont = str(container).lower() if container else "none"
        if cont not in ("none", ""):
            tj["container"] = cont
            if cont == "map" and value_type:
                v = {"category": value_type}
                if value_type_path:
                    v["type_path"] = value_type_path
                tj["value"] = v
        return tj

    def _retype_json(type_name, type_path, container, value_type, value_type_path):
        """Retype payload for change_struct_field_type: dict (complex) | bare scalar str | None."""
        if not type_name and not type_path:
            return None
        if type_path or (container and str(container).lower() not in ("none", "")):
            return _build_struct_type_json(type_name, type_path, container, value_type, value_type_path)
        return type_name

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()          -> per-session undo stack (copied verbatim from editor_level.py).
    #   _struct_fields(o)   -> default member schema readback via MCPReflectionLibrary (hasattr-guarded).
    _SW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _struct_fields(o):
    mrl = getattr(unreal, "MCPReflectionLibrary", None)
    if mrl is None or not hasattr(mrl, "get_struct_fields_json"):
        return None
    try:
        js = mrl.get_struct_fields_json(o)
        d = json.loads(js) if isinstance(js, str) else js
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("fields"), list):
        return None
    out = []
    for f in d["fields"]:
        if isinstance(f, dict):
            out.append({"name": f.get("name"), "cpp_type": f.get("cpp_type"), "flags": f.get("flags") or []})
    return out
'''

    # ------------------------------------------------------------------ #
    # create_user_defined_struct — non-interactive UserDefinedStruct       #
    # ------------------------------------------------------------------ #
    _CREATE_STRUCT_BODY = _SW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # Do NOT wrap create_asset in a ScopedEditorTransaction (see ai_write.py/create-wave rules) -- it
    # would trap the new asset in the transaction buffer and block a later delete. Creation is ledgered
    # via our own create_asset op instead; StructureFactory is non-modal (bounded-probed).
    st = at.create_asset(name, package_path, unreal.UserDefinedStruct, unreal.StructureFactory())
    if st is None or not isinstance(st, unreal.UserDefinedStruct):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(st).__name__, asset_path)}))
    else:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if aes:
            try: aes.close_all_editors_for_asset(st)
            except Exception: pass
        # Save on create: persists it AND makes the create_asset undo-delete reliable (an unsaved
        # just-created asset resists delete in the immediately-following call = settle timing).
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        fields = _struct_fields(st)
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": st.get_name(),
            "asset_path": asset_path, "object_path": st.get_path_name(),
            "class": st.get_class().get_name(),
            "default_field_count": (len(fields) if fields is not None else None),
            "default_fields": fields,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_user_defined_struct(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new UserDefinedStruct (Blueprint struct) asset non-interactively (NO modal / dialog).

        name:         asset name for the new struct (e.g. 'S_ItemInfo').
        package_path: content directory to create it under (default '/Game/MCP_Scratch'); must be under
                      a valid mounted root ('/Game', '/Engine', a plugin root). Intermediate folders
                      are created as needed.

        Uses AssetTools.create_asset(..., unreal.UserDefinedStruct, unreal.StructureFactory()). The
        factory has no ConfigureProperties dialog and the scripting path never calls one -> no modal.
        A fresh UserDefinedStruct ships with ONE default member (a bool 'MemberVar_0_<GUID>', reported
        as default_fields via the native MCPReflectionLibrary struct-field reader). Inspect/verify with
        structs.describe_blueprint_struct or structs.list_blueprint_structs.

        NOTE: adding/renaming/removing struct MEMBERS is NOT available in this build (FStructureEditorUtils
        is editor-only C++, not Python-exposed; the created struct object has no field mutator). That is a
        deferred Wave-3 C++ item -- see the module docstring. This command only CREATES the struct shell.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (ALREADY in
        editor_level.undo): close editors + delete the asset [+ rmdir package_path if we created it and
        it is now empty]. Saved to disk on create."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_STRUCT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_user_defined_enum — non-interactive UserDefinedEnum           #
    # ------------------------------------------------------------------ #
    _CREATE_ENUM_BODY = _SW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
if EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    en = at.create_asset(name, package_path, unreal.UserDefinedEnum, unreal.EnumFactory())
    if en is None or not isinstance(en, unreal.UserDefinedEnum):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(en).__name__, asset_path)}))
    else:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if aes:
            try: aes.close_all_editors_for_asset(en)
            except Exception: pass
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": en.get_name(),
            "asset_path": asset_path, "object_path": en.get_path_name(),
            "class": en.get_class().get_name(), "is_enum": isinstance(en, unreal.Enum),
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_user_defined_enum(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new UserDefinedEnum (Blueprint enum) asset non-interactively (NO modal / dialog).

        name:         asset name for the new enum (e.g. 'E_ItemKind').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid mounted
                      root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.UserDefinedEnum, unreal.EnumFactory()) (no dialog ->
        no modal, bounded-probed). The enum is created with the factory's default enumerator(s).

        NOTE: adding/renaming/removing ENUMERATORS (enum values / display names) is NOT available in this
        build (FEnumEditorUtils is editor-only C++, not Python-exposed; the created enum object has no
        enumerator mutator, and even num_enums() is not reachable from Python). That is a deferred Wave-3
        C++ item -- see the module docstring. This command only CREATES the enum shell.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (ALREADY in
        editor_level.undo): close editors + delete the asset [+ rmdir package_path if we created it and
        it is now empty]. Saved to disk on create."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_ENUM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # STRUCT FIELD + ENUM ENUMERATOR authoring — ENABLED 2026-08-15 by C++ #4 handlers on
    # MCPReflectionLibrary (AddStructField/RemoveStructField/AddEnumEntry/RemoveEnumEntry, wrapping
    # FStructureEditorUtils / FEnumEditorUtils). All hasattr-guarded so the module still loads on an
    # older DLL. Cpp_type -> friendly-type reverse map (for remove-field reversibility):
    _CPP2TYPE = ('_CPP2TYPE = {"bool":"bool","uint8":"byte","int32":"int","int64":"int64",'
                 '"float":"float","double":"float","FName":"name","FString":"string","FText":"text",'
                 '"FVector":"vector","FVector2D":"vector2d","FRotator":"rotator","FTransform":"transform",'
                 '"FLinearColor":"linearcolor"}\n')

    _ADD_STRUCT_FIELD_BODY = _SW_HELPERS + _CPP2TYPE + r'''
struct_path = PARAMS["struct_path"]; field_name = PARAMS["field_name"]
type_name = PARAMS.get("type_name"); type_json = PARAMS.get("type_json")
is_complex = bool(PARAMS.get("is_complex"))
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
obj = EAL.load_asset(struct_path)
if obj is None or not isinstance(obj, unreal.UserDefinedStruct):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UserDefinedStruct: %s" % struct_path}))
elif is_complex and (mrl is None or not hasattr(mrl, "add_struct_field_ex")):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_struct_field_ex unavailable (plugin DLL predates the complex-add round; recompile needed)"}))
elif (not is_complex) and (mrl is None or not hasattr(mrl, "add_struct_field")):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_struct_field unavailable (plugin DLL predates C++ #4; recompile needed)"}))
else:
    if is_complex:
        tj = type_json if isinstance(type_json, str) else json.dumps(type_json)
        res = json.loads(mrl.add_struct_field_ex(obj, field_name, tj))
    else:
        res = json.loads(mrl.add_struct_field(obj, field_name, type_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        try: EAL.save_asset(struct_path, only_if_is_dirty=False)
        except Exception: pass
        # Complex-add reuses the SAME ledger op as the scalar add -> inverse is C++ remove_struct_field
        # (by friendly name), already folded into editor_level.undo. No new op for the complex path.
        _ledger().append({"op": "add_struct_field", "asset_path": struct_path, "field_name": field_name})
        print("@@UMCP@@" + json.dumps({"status": "success", "struct": res.get("struct"),
            "added_field": field_name, "type": (type_json if is_complex else type_name),
            "complex": is_complex, "guid": res.get("guid"), "added_type": res.get("added_type"),
            "field_count": res.get("field_count"), "ledger_depth": len(_ledger())}))
'''

    _REMOVE_STRUCT_FIELD_BODY = _SW_HELPERS + _CPP2TYPE + r'''
struct_path = PARAMS["struct_path"]; field_name = PARAMS["field_name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
obj = EAL.load_asset(struct_path)
if obj is None or not isinstance(obj, unreal.UserDefinedStruct):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UserDefinedStruct: %s" % struct_path}))
elif mrl is None or not hasattr(mrl, "remove_struct_field"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_struct_field unavailable (plugin DLL predates C++ #4; recompile needed)"}))
else:
    # Capture the field's type before removal so undo can re-add it (matched by internal-name prefix,
    # since get_struct_fields_json returns the GUID-suffixed VarName not the friendly name).
    field_type = None
    for f in (_struct_fields(obj) or []):
        nm = f.get("name") or ""
        if nm == field_name or nm.startswith(field_name + "_"):
            field_type = _CPP2TYPE.get(f.get("cpp_type")); break
    res = json.loads(mrl.remove_struct_field(obj, field_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        try: EAL.save_asset(struct_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "remove_struct_field", "asset_path": struct_path,
                          "field_name": field_name, "field_type": field_type})
        print("@@UMCP@@" + json.dumps({"status": "success", "struct": res.get("struct"),
            "removed_field": field_name, "captured_type": field_type,
            "field_count": res.get("field_count"), "ledger_depth": len(_ledger())}))
'''

    _ADD_ENUM_ENTRY_BODY = _SW_HELPERS + r'''
enum_path = PARAMS["enum_path"]; display_name = PARAMS["display_name"]
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
obj = EAL.load_asset(enum_path)
if obj is None or not isinstance(obj, unreal.UserDefinedEnum):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UserDefinedEnum: %s" % enum_path}))
elif mrl is None or not hasattr(mrl, "add_enum_entry"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_enum_entry unavailable (plugin DLL predates C++ #4; recompile needed)"}))
else:
    res = json.loads(mrl.add_enum_entry(obj, display_name))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        try: EAL.save_asset(enum_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "add_enum_entry", "asset_path": enum_path, "index": res.get("index")})
        print("@@UMCP@@" + json.dumps({"status": "success", "enum": res.get("enum"),
            "added_entry": display_name, "index": res.get("index"),
            "entry_count": res.get("entry_count"), "ledger_depth": len(_ledger())}))
'''

    _REMOVE_ENUM_ENTRY_BODY = _SW_HELPERS + r'''
enum_path = PARAMS["enum_path"]; index = int(PARAMS["index"])
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
obj = EAL.load_asset(enum_path)
if obj is None or not isinstance(obj, unreal.UserDefinedEnum):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UserDefinedEnum: %s" % enum_path}))
elif mrl is None or not hasattr(mrl, "remove_enum_entry"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_enum_entry unavailable (plugin DLL predates C++ #4; recompile needed)"}))
else:
    # Best-effort capture of the display name at index for undo (re-add appends at END, so order is
    # only preserved when removing the LAST entry -- documented in the tool).
    prior_name = None
    try:
        if hasattr(obj, "get_display_name_text_by_index"):
            prior_name = str(obj.get_display_name_text_by_index(index))
    except Exception:
        prior_name = None
    res = json.loads(mrl.remove_enum_entry(obj, index))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        try: EAL.save_asset(enum_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "remove_enum_entry", "asset_path": enum_path,
                          "index": index, "prior_display_name": prior_name})
        print("@@UMCP@@" + json.dumps({"status": "success", "enum": res.get("enum"),
            "removed_index": index, "captured_name": prior_name,
            "entry_count": res.get("entry_count"), "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # set_blueprint_struct_variable — modify member name/type/default/tooltip
    # via the C++ #N handlers (rename/change_type/set_default/set_tooltip). Each sub-change
    # ledgers its OWN reversible op capturing the prior from the handler's JSON return. Order:
    # type -> default -> tooltip -> rename LAST (rename last keeps the name lookups valid; on
    # LIFO undo the rename inverse runs FIRST, restoring the old name before the other inverses
    # replay against it).
    _SET_STRUCT_VAR_BODY = _SW_HELPERS + r'''
struct_path = PARAMS["struct_path"]; name = PARAMS["name"]
new_name = PARAMS.get("new_name"); type_json = PARAMS.get("type_json")
has_default = bool(PARAMS.get("has_default")); default = PARAMS.get("default")
tooltip = PARAMS.get("tooltip")
EAL = unreal.EditorAssetLibrary
mrl = getattr(unreal, "MCPReflectionLibrary", None)
obj = EAL.load_asset(struct_path)
if obj is None or not isinstance(obj, unreal.UserDefinedStruct):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a UserDefinedStruct: %s" % struct_path}))
elif mrl is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "MCPReflectionLibrary unavailable (plugin DLL predates the struct-edit round; recompile needed)"}))
else:
    changes = []; errors = []
    if type_json is not None:
        if not hasattr(mrl, "change_struct_field_type"):
            errors.append("change_struct_field_type unavailable")
        else:
            tj = type_json if isinstance(type_json, str) else json.dumps(type_json)
            r = json.loads(mrl.change_struct_field_type(obj, name, tj))
            if isinstance(r, dict) and r.get("error"):
                errors.append("type: " + str(r.get("error")))
            else:
                prior = r.get("prev_type")
                _ledger().append({"op": "change_struct_field_type", "asset_path": struct_path,
                                  "name": name, "prior_type_json": prior})
                changes.append({"field": "type", "new_type": r.get("new_type"), "prev_type": prior})
    if has_default:
        if not hasattr(mrl, "set_struct_field_default"):
            errors.append("set_struct_field_default unavailable")
        else:
            r = json.loads(mrl.set_struct_field_default(obj, name, default))
            if isinstance(r, dict) and r.get("error"):
                errors.append("default: " + str(r.get("error")))
            else:
                prior = r.get("prev_default")
                _ledger().append({"op": "set_struct_field_default", "asset_path": struct_path,
                                  "name": name, "prior_default": prior})
                changes.append({"field": "default", "new_default": default, "prev_default": prior})
    if tooltip is not None:
        if not hasattr(mrl, "set_struct_field_tooltip"):
            errors.append("set_struct_field_tooltip unavailable")
        else:
            r = json.loads(mrl.set_struct_field_tooltip(obj, name, tooltip))
            if isinstance(r, dict) and r.get("error"):
                errors.append("tooltip: " + str(r.get("error")))
            else:
                prior = r.get("prev_tooltip")
                _ledger().append({"op": "set_struct_field_tooltip", "asset_path": struct_path,
                                  "name": name, "prior_tooltip": prior})
                changes.append({"field": "tooltip", "new_tooltip": tooltip, "prev_tooltip": prior})
    if new_name is not None:
        if not hasattr(mrl, "rename_struct_field"):
            errors.append("rename_struct_field unavailable")
        else:
            r = json.loads(mrl.rename_struct_field(obj, name, new_name))
            if isinstance(r, dict) and r.get("error"):
                errors.append("rename: " + str(r.get("error")))
            else:
                old = r.get("old_name")
                _ledger().append({"op": "rename_struct_field", "asset_path": struct_path,
                                  "name": new_name, "prior_name": old})
                changes.append({"field": "name", "new_name": new_name, "old_name": old})
    if changes:
        try: EAL.save_asset(struct_path, only_if_is_dirty=False)
        except Exception: pass
    status = "success" if (changes and not errors) else ("partial" if changes else "error")
    print("@@UMCP@@" + json.dumps({"status": status, "struct_path": struct_path, "field": name,
        "changes": changes, "errors": errors, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_struct_field(ctx, struct_path: str, field_name: str, type_name: str,
                         type_path: str = None, container: str = None,
                         value_type: str = None, value_type_path: str = None) -> str:
        """Add a member field to a UserDefinedStruct. REQUIRES the C++ handler
        (unreal.MCPReflectionLibrary.add_struct_field / add_struct_field_ex); clear error on older DLL.

        struct_path: object/package path of the UserDefinedStruct asset.
        field_name:  friendly name for the new field (e.g. 'Health').
        type_name:   SCALAR (case-insensitive): bool | byte | int | int64 | float | name | string | text |
                     vector | vector2d | rotator | transform | linearcolor -> scalar fast-path.
                     COMPLEX category: object | class | softobject | softclass | interface | struct | enum
                     (pair with type_path) -> routes to add_struct_field_ex.
        type_path:   for complex categories, the referenced type, e.g. '/Script/Engine.Actor' (object),
                     '/Game/.../MyStruct.MyStruct' (struct), '/Game/.../E_Kind.E_Kind' (enum).
        container:   none | array | set | map. Any of array/set/map (even over a scalar element) is COMPLEX.
        value_type / value_type_path: element type of a MAP's VALUE (container='map'); key type is type_name.

        Complex types route to the C++ add_struct_field_ex (returns guid + added_type); scalars keep the
        add_struct_field fast-path. Saved after the edit. Verify with structs.describe_blueprint_struct.

        Ledgered write op 'add_struct_field' {asset_path, field_name} — SAME op for scalar AND complex adds.
        Inverse (already in editor_level.undo): remove_struct_field(field_name) -> FAITHFUL (removes exactly
        the field we added, by name). The complex-add path introduces NO new ledger op."""
        complex_ = _struct_type_is_complex(type_name, type_path, container)
        type_json = (_build_struct_type_json(type_name, type_path, container, value_type, value_type_path)
                     if complex_ else None)
        params = {"struct_path": struct_path, "field_name": field_name, "type_name": type_name,
                  "type_json": type_json, "is_complex": complex_}
        try:
            return json.dumps(_exec(_ADD_STRUCT_FIELD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_blueprint_struct_variable(ctx, struct_path: str, name: str, new_name: str = None,
                                      type: str = None, type_path: str = None, default: str = None,
                                      tooltip: str = None, container: str = None,
                                      value_type: str = None, value_type_path: str = None) -> str:
        """Modify an existing UserDefinedStruct member: rename, retype, set default, and/or set tooltip.
        REQUIRES the C++ struct-edit handlers (rename_struct_field / change_struct_field_type /
        set_struct_field_default / set_struct_field_tooltip); each missing handler is reported per-field.

        struct_path: object/package path of the UserDefinedStruct asset.
        name:        CURRENT friendly name of the member to edit.
        new_name:    optional new friendly name (rename is applied LAST so the other edits resolve 'name').
        type/type_path/container/value_type/value_type_path: optional retype. 'type' is a scalar name OR a
                     complex category (object/struct/enum/...); pair complex categories with type_path;
                     container array|set|map wraps it; value_type(_path) is a map VALUE's element type.
        default:     optional new default value (string form, e.g. '5', 'true', '1,2,3').
        tooltip:     optional new tooltip text.

        Each provided edit is applied in the order type -> default -> tooltip -> rename, and EACH ledgers
        its OWN reversible op capturing the prior value returned by the handler. Saved after any change.

        New ledger ops (folded into editor_level.undo):
          - change_struct_field_type {asset_path, name, prior_type_json} -> inverse change_struct_field_type(name, prior_type_json)
          - set_struct_field_default {asset_path, name, prior_default}    -> inverse set_struct_field_default(name, prior_default)
          - set_struct_field_tooltip {asset_path, name, prior_tooltip}    -> inverse set_struct_field_tooltip(name, prior_tooltip)
          - rename_struct_field {asset_path, name:new_name, prior_name}   -> inverse rename_struct_field(new_name, prior_name)"""
        type_json = _retype_json(type, type_path, container, value_type, value_type_path)
        params = {"struct_path": struct_path, "name": name, "new_name": new_name,
                  "type_json": type_json, "has_default": (default is not None), "default": default,
                  "tooltip": tooltip}
        try:
            return json.dumps(_exec(_SET_STRUCT_VAR_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_struct_field(ctx, struct_path: str, field_name: str) -> str:
        """Remove a member field (by friendly name) from a UserDefinedStruct. REQUIRES the C++ #4 handler.

        struct_path: object/package path of the UserDefinedStruct asset.
        field_name:  friendly name of the field to remove.

        Saved after the edit. Ledgered write op 'remove_struct_field' {asset_path, field_name, field_type}
        (field_type captured from the struct's current schema before removal). Inverse (in
        editor_level.undo): add_struct_field(field_name, field_type) -- restores a field of the same
        name + type. NOTE near-faithful: the restored field gets a NEW GUID and loses any custom default
        value; use undo right after the remove for the cleanest result."""
        params = {"struct_path": struct_path, "field_name": field_name}
        try:
            return json.dumps(_exec(_REMOVE_STRUCT_FIELD_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_enum_entry(ctx, enum_path: str, display_name: str) -> str:
        """Add an enumerator (with display name) to a UserDefinedEnum. REQUIRES the C++ #4 handler.

        enum_path:    object/package path of the UserDefinedEnum asset.
        display_name: display name for the new entry (e.g. 'Fire').

        The entry is appended (index = entry_count-1, excluding the hidden _MAX). Saved after the edit.

        Ledgered write op 'add_enum_entry' {asset_path, index}. Inverse (in editor_level.undo):
        remove_enum_entry(index) -> FAITHFUL (removes exactly the entry we appended)."""
        params = {"enum_path": enum_path, "display_name": display_name}
        try:
            return json.dumps(_exec(_ADD_ENUM_ENTRY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_enum_entry(ctx, enum_path: str, index: int) -> str:
        """Remove the enumerator at Index from a UserDefinedEnum. REQUIRES the C++ #4 handler.

        enum_path: object/package path of the UserDefinedEnum asset.
        index:     0-based index of the entry to remove (excludes the hidden trailing _MAX sentinel).

        Saved after the edit. Ledgered write op 'remove_enum_entry' {asset_path, index,
        prior_display_name}. Inverse (in editor_level.undo): add_enum_entry(prior_display_name) --
        best-effort: the re-added entry is APPENDED at the end, so order is preserved only when the
        removed entry was the LAST one. Use undo right after the remove for the cleanest result."""
        params = {"enum_path": enum_path, "index": index}
        try:
            return json.dumps(_exec(_REMOVE_ENUM_ENTRY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_blueprint_struct — create + add members atomically (thin wrapper)
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def create_blueprint_struct(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                                members=None) -> str:
        """Create a UserDefinedStruct and (optionally) add members atomically.

        name:         asset name for the new struct (e.g. 'S_ItemInfo').
        package_path: content directory (default '/Game/MCP_Scratch').
        members:      optional list (or JSON string) of member dicts, each:
                        {"name": <str>, "type": <scalar|complex category>, "type_path": <opt>,
                         "container": <none|array|set|map, opt>, "value_type": <map value, opt>,
                         "value_type_path": <opt>, "default": <opt>}

        Thin wrapper: calls create_user_defined_struct (reuses the folded 'create_asset' undo op), then
        loops add_struct_field (scalar or complex -> add_struct_field_ex; each ledgers 'add_struct_field',
        inverse = C++ remove_struct_field) and, if a member has a 'default', set_blueprint_struct_variable.
        NO new ledger op is introduced here — reuses create_asset + the field ops. NOTE: a fresh
        UserDefinedStruct ships with one default bool member (MemberVar_0_<GUID>); requested members are
        APPENDED after it."""
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except Exception as e:
                return json.dumps({"status": "error", "message": "members not valid JSON: %s" % e}, indent=2)
        try:
            created = json.loads(create_user_defined_struct(ctx, name, package_path))
        except Exception as e:
            return f"Error (create): {e}"
        if not isinstance(created, dict) or created.get("status") != "success":
            return json.dumps({"status": "error", "stage": "create", "detail": created}, indent=2)
        asset_path = created.get("asset_path")
        member_results = []
        for m in (members or []):
            if not isinstance(m, dict) or not m.get("name") or not m.get("type"):
                member_results.append({"member": m, "status": "error",
                                       "message": "each member needs at least 'name' and 'type'"})
                continue
            try:
                ar = json.loads(add_struct_field(ctx, asset_path, m["name"], m["type"],
                                                 type_path=m.get("type_path"), container=m.get("container"),
                                                 value_type=m.get("value_type"),
                                                 value_type_path=m.get("value_type_path")))
            except Exception as e:
                member_results.append({"member": m.get("name"), "status": "error", "message": str(e)})
                continue
            entry = {"member": m.get("name"), "add": ar}
            if m.get("default") is not None:
                try:
                    entry["default"] = json.loads(
                        set_blueprint_struct_variable(ctx, asset_path, m["name"], default=m.get("default")))
                except Exception as e:
                    entry["default_error"] = str(e)
            member_results.append(entry)
        return json.dumps({"status": "success", "asset_path": asset_path, "created": created,
                           "members": member_results}, indent=2)

    # This module registers NO `undo` tool (editor_level.py owns the unified one). The 2 creates reuse the
    # generic "create_asset" inverse; the authoring tools add ops (add_struct_field/remove_struct_field/
    # add_enum_entry/remove_enum_entry) plus the struct-EDIT ops from set_blueprint_struct_variable
    # (change_struct_field_type/set_struct_field_default/set_struct_field_tooltip/rename_struct_field),
    # whose inverses are folded into editor_level.undo. The complex add_struct_field path REUSES the
    # existing add_struct_field op (inverse C++ remove_struct_field) -> NO new op. create_blueprint_struct
    # is a thin wrapper reusing create_asset + the field ops -> NO new op.
