"""UserTools :: Enhanced Input (WRITE)  (spec: docs/spec/input.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE-wave WRITE batch,
the mutating counterpart to input_read.py. Query convention, base64 PARAMS injection, Output-Log
auto-capture, and the per-session undo ledger are copied VERBATIM from the gold-standard
editor_level.py (mirrors ai_write.py's create-wave structure: create_asset ledger op, save-on-create,
close-editors, NO ScopedEditorTransaction around create_asset).

What this build exposes (all NON-MODAL, verified live vs TestMCPSetup, UE 5.8.1):
  * InputAction / InputMappingContext are created with a NULL factory:
      unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, package_path, <Class>, None)
    KEY FINDING (live-probed): neither unreal.InputActionFactory nor unreal.InputMappingContextFactory
    is exposed to Python (hasattr == False; not loadable via load_class/load_object under
    /Script/EnhancedInput or /Script/EnhancedInputEditor). The UEnhancedInput*Factory classes are
    editor-module C++ factories not exported to the Python API. BUT AssetTools.create_asset accepts
    factory=None and creates a default-constructed asset of the class directly (no ConfigureProperties
    dialog exists for either class anyway) -> no modal EVER pops. Returned objects are proper
    InputAction / InputMappingContext instances.
  * InputAction.value_type (EInputActionValueType BOOLEAN/AXIS1D/AXIS2D/AXIS3D) is set AFTER create via
    reflection: ia.set_editor_property("value_type", unreal.InputActionValueType.<MEMBER>).
  * A key mapping is added to an IMC with the native UFUNCTION imc.map_key(action, key), where `key` is
    an FKey built as: k = unreal.Key(); k.set_editor_property("key_name", unreal.Name("<KeyName>")).
    (unreal.Key() takes NO constructor args; the FKey resolves its details lazily from key_name.)
    map_key APPENDS to default_key_mappings.mappings (the real 5.8 mapping array; see input_read.py);
    the inverse imc.unmap_key(action, key) removes it. Round-trip proven live (count 0->1->0).

Implemented (all validated live, session=agentB, editor left CLEAN, ledger depth 0):
  - create_input_action          (WRITE; ledgered op "create_asset"; optional value_type set in-create)
  - create_input_mapping_context (WRITE; ledgered op "create_asset")
  - add_mapping_to_context       (WRITE; ledgered NEW op "add_input_mapping"; inverse = unmap_key + save)

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the single unified `undo`).
The two create_* commands reuse the coordinator's already-folded generic "create_asset" inverse (close
editors + delete_asset [+ rmdir if we made the dir]). add_mapping_to_context introduces ONE NEW ledger op
"add_input_mapping" whose inverse (imc.unmap_key(action, key) + save) is documented in its docstring and the
build report for the coordinator to fold into editor_level.undo. Reversibility was proven live.
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash in this block.
    #   _ledger()                  -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_typed(path, tyname)  -> (obj, err) load an asset and type-check its class name.
    #   _close_editors(obj)        -> silently close any open asset editor for obj.
    #   _VT_MAP / _resolve_value_type(spec) -> friendly value-type name -> InputActionValueType member.
    #   _make_key(name)            -> build an FKey from a key name (unreal.Key() + set key_name).
    _IW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_typed(path, tyname):
    if not path:
        return None, "no asset path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    cn = obj.get_class().get_name()
    if cn != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, cn, path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
_VT_MAP = {"digital": "BOOLEAN", "bool": "BOOLEAN", "boolean": "BOOLEAN",
           "axis1d": "AXIS1D", "1d": "AXIS1D", "analog": "AXIS1D", "float": "AXIS1D",
           "axis2d": "AXIS2D", "2d": "AXIS2D", "vector2d": "AXIS2D",
           "axis3d": "AXIS3D", "3d": "AXIS3D", "vector": "AXIS3D"}
def _resolve_value_type(spec):
    # returns (enum_member, short_name, err); (None, None, None) means "leave factory default".
    if spec is None:
        return None, None, None
    short = _VT_MAP.get(str(spec).strip().lower())
    if short is None:
        return None, None, ("unknown value_type '%s'; valid: Digital/Boolean, Axis1D, Axis2D, Axis3D "
                            "(aliases: 1d/analog/float, 2d/vector2d, 3d/vector)" % spec)
    try:
        return getattr(unreal.InputActionValueType, short), short, None
    except Exception as e:
        return None, None, "could not resolve InputActionValueType.%s: %s" % (short, e)
def _make_key(key_name):
    k = unreal.Key()
    k.set_editor_property("key_name", unreal.Name(str(key_name)))
    return k
def _imc_mapping_count(imc):
    dkm = imc.get_editor_property("default_key_mappings")
    arr = dkm.get_editor_property("mappings") if dkm is not None else None
    return len(arr or []), arr
'''

    # ------------------------------------------------------------------ #
    # create_input_action — non-interactive InputAction asset creation     #
    # ------------------------------------------------------------------ #
    _CREATE_IA_BODY = _IW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
vt_spec = PARAMS.get("value_type")
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
asset_path = package_path + "/" + name
vt_member, vt_short, vt_err = _resolve_value_type(vt_spec)
if vt_err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": vt_err}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    # NO ScopedEditorTransaction around create_asset (it would trap the new asset in the undo buffer
    # and block a later delete). Factory=None -> default-constructed InputAction, no modal.
    ia = at.create_asset(name, package_path, unreal.InputAction, None)
    if ia is None or not isinstance(ia, unreal.InputAction):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(ia).__name__, asset_path)}))
    else:
        if vt_member is not None:
            ia.set_editor_property("value_type", vt_member)
        _close_editors(ia)
        # Save on create: persists it AND makes the create_asset undo-delete reliable.
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        eff_vt = str(ia.get_editor_property("value_type"))
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": ia.get_name(),
            "asset_path": asset_path, "object_path": ia.get_path_name(),
            "class": ia.get_class().get_name(),
            "value_type": eff_vt, "value_type_requested": vt_short,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_input_action(ctx, name: str, package_path: str = "/Game/MCP_Scratch",
                            value_type: str = "Digital") -> str:
        """Create a new Enhanced Input InputAction asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new InputAction (e.g. 'IA_Jump').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid mounted
                      root ('/Game', a plugin root). Intermediate folders are created as needed.
        value_type:   the action's value type — Digital/Boolean (default), Axis1D, Axis2D, or Axis3D
                      (aliases: 1d/analog/float -> Axis1D, 2d/vector2d -> Axis2D, 3d/vector -> Axis3D).
                      Set via reflection (value_type = EInputActionValueType member) after creation.

        Uses AssetTools.create_asset(name, path, unreal.InputAction, None). NOTE: the UInputActionFactory
        C++ class is NOT exposed to Python in 5.8, but create_asset accepts a null factory and
        default-constructs the asset (no ConfigureProperties dialog exists anyway) -> no modal. Inspect
        with input_read.describe_input_action.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already folded
        into editor_level.undo): close editors + delete asset [+ rmdir if we created the dir]. The
        value_type set is reverted for free by deleting the asset. The asset is SAVED on create."""
        params = {"name": name, "package_path": package_path, "value_type": value_type}
        try:
            return json.dumps(_exec(_CREATE_IA_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_input_mapping_context — non-interactive IMC asset creation    #
    # ------------------------------------------------------------------ #
    _CREATE_IMC_BODY = _IW_HELPERS + r'''
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
    # NO ScopedEditorTransaction around create_asset (see create_input_action). Factory=None -> no modal.
    imc = at.create_asset(name, package_path, unreal.InputMappingContext, None)
    if imc is None or not isinstance(imc, unreal.InputMappingContext):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(imc).__name__, asset_path)}))
    else:
        _close_editors(imc)
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)  # persist + reliable undo-delete
        except Exception: pass
        cnt, _arr = _imc_mapping_count(imc)
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": imc.get_name(),
            "asset_path": asset_path, "object_path": imc.get_path_name(),
            "class": imc.get_class().get_name(),
            "mapping_count": cnt,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_input_mapping_context(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new Enhanced Input InputMappingContext asset non-interactively (NO modal / dialog).

        name:         asset name for the new InputMappingContext (e.g. 'IMC_Default').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid mounted
                      root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(name, path, unreal.InputMappingContext, None). The
        UInputMappingContextFactory C++ class is NOT exposed to Python in 5.8, but create_asset with a
        null factory default-constructs the asset (no dialog) -> no modal. The IMC is created EMPTY (0 key
        mappings) -- add mappings with add_mapping_to_context. Inspect with
        input_read.describe_input_mapping_context.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse (already folded
        into editor_level.undo): close editors + delete asset [+ rmdir if we created the dir]. SAVED on
        create."""
        params = {"name": name, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_IMC_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_mapping_to_context — add one key->action mapping to an IMC        #
    # ------------------------------------------------------------------ #
    _ADD_MAPPING_BODY = _IW_HELPERS + r'''
imc_path = PARAMS["imc_path"]
ia_path = PARAMS["input_action_path"]
key_name = PARAMS["key"]
imc, err = _load_typed(imc_path, "InputMappingContext")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    ia, ierr = _load_typed(ia_path, "InputAction")
    if ierr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": ierr}))
    elif not key_name or not str(key_name).strip():
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "no key given"}))
    else:
        key = _make_key(key_name)
        # Refuse if an identical (action, key) mapping already exists -> keeps the unmap_key inverse
        # FAITHFUL (prior matching count is 0, so unmap restores exactly to prior).
        before_cnt, before_arr = _imc_mapping_count(imc)
        dup = False
        for m in (before_arr or []):
            mk = None; ma = None
            try: mk = str(m.get_editor_property("key").get_editor_property("key_name"))
            except Exception: mk = None
            try:
                _a = m.get_editor_property("action")
                ma = _a.get_path_name() if _a is not None else None
            except Exception: ma = None
            if mk == str(key_name) and ma == ia.get_path_name():
                dup = True; break
        if dup:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "a mapping for action '%s' + key '%s' already exists (refusing duplicate; "
                           "keeps undo faithful)" % (ia.get_name(), key_name),
                "mapping_count": before_cnt}))
        else:
            with unreal.ScopedEditorTransaction("MCP add_mapping_to_context"):
                imc.map_key(ia, key)
            try: unreal.EditorAssetLibrary.save_asset(imc_path, only_if_is_dirty=False)
            except Exception: pass
            after_cnt, after_arr = _imc_mapping_count(imc)
            added = after_cnt == before_cnt + 1
            new_row = None
            if after_arr:
                m = after_arr[after_cnt - 1]
                mk = m.get_editor_property("key")
                ma = m.get_editor_property("action")
                new_row = {"key": (str(mk.get_editor_property("key_name")) if mk else None),
                           "action": (ma.get_name() if ma else None),
                           "action_path": (ma.get_path_name() if ma else None)}
            if added:
                _ledger().append({"op": "add_input_mapping", "asset_path": imc_path,
                                  "object_path": imc.get_path_name(), "action_path": ia.get_path_name(),
                                  "key_name": str(key_name), "prior_mapping_count": before_cnt})
            print("@@UMCP@@" + json.dumps({"status": "success" if added else "error",
                "imc": imc.get_name(), "added": added,
                "mapping": new_row, "prior_mapping_count": before_cnt,
                "mapping_count": after_cnt, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_mapping_to_context(ctx, imc_path: str, input_action_path: str, key: str) -> str:
        """Add one key -> InputAction mapping to an existing InputMappingContext.

        imc_path:          object/package path of the InputMappingContext asset.
        input_action_path: object/package path of the InputAction to bind.
        key:               the FKey name to bind (e.g. 'SpaceBar', 'W', 'Gamepad_FaceButton_Bottom',
                           'LeftMouseButton'). Built as unreal.Key() with key_name set; the FKey
                           resolves its details lazily from the name.

        Appends to the IMC's default_key_mappings.mappings (the real 5.8 mapping array) via the native
        UFUNCTION imc.map_key(action, key), then saves the asset. REFUSES a duplicate (same action + key
        already mapped) so the undo stays exact. Verify with input_read.describe_input_mapping_context.

        Ledgered NEW write op 'add_input_mapping' {asset_path, object_path, action_path, key_name,
        prior_mapping_count}. Inverse (for the coordinator to fold into editor_level.undo):
          imc = load_asset(asset_path); ia = load_asset(action_path)
          key = unreal.Key(); key.set_editor_property('key_name', unreal.Name(key_name))
          with unreal.ScopedEditorTransaction('MCP undo add_input_mapping'): imc.unmap_key(ia, key)
          unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)
        FAITHFUL: the command refuses to add a duplicate, so before the add there were 0 mappings
        matching (action, key); unmap_key removes exactly that pair, restoring the prior state."""
        params = {"imc_path": imc_path, "input_action_path": input_action_path, "key": key}
        try:
            return json.dumps(_exec(_ADD_MAPPING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # G2-B EXTENSION — trigger/modifier + mapping-mutation + discovery.    #
    # Shared Unreal-side helpers for the new tools (serialize/coerce so    #
    # ledger entries stay JSON-plain; trigger/modifier UObjects rebuilt    #
    # from {class_path, props} — export_text refs sub-objects by path and  #
    # is GC-fragile, so we snapshot to a self-contained dict instead).     #
    # ================================================================== #
    _IW_HELPERS2 = _IW_HELPERS + r'''
import inspect, warnings as _wn
_wn.simplefilter("ignore")
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
def _snake(pn):
    s = ""
    for ch in (pn or ""):
        if ch.isupper() and s and not s.endswith("_"):
            s += "_"
        s += ch.lower()
    return s
def _enum_token(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    if "." in s:
        return s.split(".")[-1].strip()
    return s
def _get_prop(o, pn):
    for cand in (pn, _snake(pn)):
        try:
            return o.get_editor_property(cand), True
        except Exception:
            continue
    return None, False
def _ser_val(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, unreal.EnumBase):
        return {"__enum__": type(v).__name__, "token": _enum_token(v)}
    if isinstance(v, (unreal.Name, unreal.Text)):
        return {"__name__": str(v)}
    if isinstance(v, unreal.Vector):
        return {"__vec__": [v.x, v.y, v.z]}
    if isinstance(v, unreal.Vector2D):
        return {"__vec2__": [v.x, v.y]}
    if isinstance(v, unreal.Object):
        return {"__object__": v.get_path_name()}
    return {"__skip__": type(v).__name__}
def _coerce_val(v):
    if isinstance(v, dict):
        if "__enum__" in v:
            et = getattr(unreal, v.get("__enum__"), None)
            return getattr(et, v.get("token"), None) if et else None
        if "__name__" in v:
            return unreal.Name(v.get("__name__"))
        if "__vec__" in v:
            return unreal.Vector(*v.get("__vec__"))
        if "__vec2__" in v:
            return unreal.Vector2D(*v.get("__vec2__"))
        if "__object__" in v:
            return unreal.EditorAssetLibrary.load_asset(v.get("__object__"))
        if "__skip__" in v:
            return None
    return v
def _prop_names(o):
    if _MRL is None:
        return []
    try:
        return [p.get("name") for p in json.loads(_MRL.get_object_property_metadata_json(o)).get("properties", [])]
    except Exception:
        return []
def _set_prop(o, pn, value):
    cur, ok = _get_prop(o, pn)
    tv = value
    if ok and isinstance(cur, unreal.EnumBase) and isinstance(value, str):
        et = type(cur); tok = value.strip().upper().replace(" ", "_")
        tv = getattr(et, tok, None)
        if tv is None:
            return False, "bad enum token '%s' for %s" % (value, et.__name__)
    elif isinstance(value, dict):
        tv = _coerce_val(value)
    last = None
    for cand in (pn, _snake(pn)):
        try:
            o.set_editor_property(cand, tv); return True, None
        except Exception as e:
            last = e; continue
    return False, "could not set %s: %s" % (pn, last)
def _snap_tm(o):
    d = {}
    for pn in _prop_names(o):
        val, ok = _get_prop(o, pn)
        if ok:
            d[pn] = _ser_val(val)
    return {"class_path": o.get_class().get_path_name(), "props": d}
def _rebuild_tm(spec, outer):
    lc = unreal.load_class(None, spec.get("class_path"))
    if lc is None:
        return None
    inst = unreal.new_object(lc, outer)
    for pn, sv in (spec.get("props") or {}).items():
        if isinstance(sv, dict) and "__skip__" in sv:
            continue
        cv = _coerce_val(sv)
        for cand in (pn, _snake(pn)):
            try:
                inst.set_editor_property(cand, cv); break
            except Exception:
                continue
    return inst
def _resolve_tm_class(type_name, base, prefix):
    if not type_name:
        return None, "no type given"
    nm = str(type_name).strip()
    cands = [nm]
    if not nm.startswith(prefix):
        cands.append(prefix + nm)
    for c in cands:
        cls = getattr(unreal, c, None)
        if cls is not None:
            try:
                if inspect.isclass(cls) and issubclass(cls, base) and cls is not base:
                    return cls, None
            except Exception:
                pass
    return None, "unknown type '%s' (want a %s subclass; e.g. %sHold)" % (type_name, base.__name__, prefix)
def _tm_arr(o, field):
    return list(o.get_editor_property(field) or [])
def _set_tm_arr(o, field, arr):
    o.set_editor_property(field, arr)
def _get_dkm(imc):
    dkm = imc.get_editor_property("default_key_mappings")
    return dkm, list(dkm.get_editor_property("mappings") or [])
def _set_dkm(imc, dkm, arr):
    dkm.set_editor_property("mappings", arr)
    imc.set_editor_property("default_key_mappings", dkm)
def _map_key_name(m):
    k = m.get_editor_property("key")
    return str(k.get_editor_property("key_name")) if k is not None else None
def _map_action_path(m):
    a = m.get_editor_property("action")
    return a.get_path_name() if a is not None else None
def _snap_mapping(m):
    return {"action_path": _map_action_path(m), "key_name": _map_key_name(m),
            "triggers": [_snap_tm(t) for t in (m.get_editor_property("triggers") or []) if t is not None],
            "modifiers": [_snap_tm(t) for t in (m.get_editor_property("modifiers") or []) if t is not None]}
def _rebuild_mapping(spec, imc):
    m = unreal.EnhancedActionKeyMapping()
    ap = spec.get("action_path")
    if ap:
        act = unreal.EditorAssetLibrary.load_asset(ap)
        if act is not None:
            m.set_editor_property("action", act)
    kn = spec.get("key_name")
    if kn:
        kk = unreal.Key(); kk.set_editor_property("key_name", unreal.Name(kn))
        m.set_editor_property("key", kk)
    trs = [x for x in (_rebuild_tm(s, imc) for s in (spec.get("triggers") or [])) if x is not None]
    mds = [x for x in (_rebuild_tm(s, imc) for s in (spec.get("modifiers") or [])) if x is not None]
    m.set_editor_property("triggers", trs)
    m.set_editor_property("modifiers", mds)
    return m
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
'''

    def _norm_props(p):
        """Accept a dict or JSON string of trigger/modifier config properties."""
        if p is None:
            return {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                return {}
        return p if isinstance(p, dict) else {}

    # ------------------------------------------------------------------ #
    # set_input_action_properties                                          #
    # ------------------------------------------------------------------ #
    _SET_IA_PROPS_BODY = _IW_HELPERS2 + r'''
ia_path = PARAMS["asset_path"]
props = PARAMS.get("properties") or {}
ia, err = _load_typed(ia_path, "InputAction")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not props:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no properties given"}))
else:
    prior = {}; results = {}
    with unreal.ScopedEditorTransaction("MCP set_input_action_properties"):
        for pn, val in props.items():
            target = pn; is_vt = str(pn).strip().lower() in ("value_type", "valuetype")
            if is_vt:
                member, short, verr = _resolve_value_type(val)
                if verr:
                    results[pn] = {"ok": False, "error": verr}; continue
                target = "value_type"
                cur, okc = _get_prop(ia, target); pser = _ser_val(cur) if okc else None
                try:
                    ia.set_editor_property("value_type", member); okset = True; serr = None
                except Exception as e:
                    okset = False; serr = str(e)
            else:
                cur, okc = _get_prop(ia, target); pser = _ser_val(cur) if okc else None
                okset, serr = _set_prop(ia, target, val)
            if okset:
                prior[target] = pser
                nv, _ok = _get_prop(ia, target)
                results[pn] = {"ok": True, "value": _ser_val(nv)}
            else:
                results[pn] = {"ok": False, "error": serr}
    _save(ia_path)
    if prior:
        _ledger().append({"op": "set_input_action_properties", "asset_path": ia_path, "prior": prior})
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ia_path,
        "results": results, "prior": prior, "changed": list(prior.keys()),
        "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_input_action_properties(ctx, asset_path: str, properties: dict = None) -> str:
        """Set editable properties on an existing InputAction asset (reflection-driven, reversible).

        asset_path: the InputAction asset path.
        properties: a dict of property -> value. Recognized: value_type (Digital/Boolean, Axis1D,
                    Axis2D, Axis3D or aliases 1d/analog/float, 2d/vector2d, 3d/vector), consume_input
                    (bool), trigger_when_paused (bool), reserve_all_mappings (bool),
                    accumulation_behavior (enum token e.g. 'CUMULATIVE' / 'TAKE_HIGHEST_ABSOLUTE_VALUE'),
                    action_description (text). Enum props accept a case-insensitive token string. Any other
                    reflected FProperty name is accepted too (best-effort). Each result reports ok/error.

        Ledgered op 'set_input_action_properties' {asset_path, prior:{name: serialized_prior}}; inverse
        restores each prior value. Verify with input_read.describe_input_action."""
        params = {"asset_path": asset_path, "properties": _norm_props(properties)}
        try:
            return json.dumps(_exec(_SET_IA_PROPS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add / remove InputAction-level trigger or modifier (shared bodies)   #
    # ------------------------------------------------------------------ #
    _ADD_IA_TM_BODY = _IW_HELPERS2 + r'''
ia_path = PARAMS["asset_path"]; field = PARAMS["field"]; type_name = PARAMS["type_name"]
props = PARAMS.get("properties") or {}
base = unreal.InputTrigger if field == "triggers" else unreal.InputModifier
prefix = "InputTrigger" if field == "triggers" else "InputModifier"
ia, err = _load_typed(ia_path, "InputAction")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    cls, cerr = _resolve_tm_class(type_name, base, prefix)
    if cerr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
    else:
        arr = _tm_arr(ia, field); idx = len(arr); applied = {}
        with unreal.ScopedEditorTransaction("MCP add_input_action_" + field):
            inst = unreal.new_object(cls, ia)
            for pn, val in props.items():
                ok, serr = _set_prop(inst, pn, val); applied[pn] = {"ok": ok, "error": serr}
            arr.append(inst); _set_tm_arr(ia, field, arr)
        _save(ia_path)
        _ledger().append({"op": "add_input_action_tm", "asset_path": ia_path, "field": field, "index": idx})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ia_path, "field": field,
            "index": idx, "class": cls.get_default_object().get_class().get_name(),
            "applied_properties": applied, "count": len(_tm_arr(ia, field)),
            "ledger_depth": len(_ledger())}))
'''

    _REMOVE_IA_TM_BODY = _IW_HELPERS2 + r'''
ia_path = PARAMS["asset_path"]; field = PARAMS["field"]; index = PARAMS["index"]
ia, err = _load_typed(ia_path, "InputAction")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    arr = _tm_arr(ia, field)
    if index is None or index < 0 or index >= len(arr):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "index %s out of range (have %d %s)" % (index, len(arr), field)}))
    else:
        snap = _snap_tm(arr[index]); removed_cls = arr[index].get_class().get_name()
        with unreal.ScopedEditorTransaction("MCP remove_input_action_" + field):
            del arr[index]; _set_tm_arr(ia, field, arr)
        _save(ia_path)
        _ledger().append({"op": "remove_input_action_tm", "asset_path": ia_path, "field": field,
            "index": index, "item": snap})
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ia_path, "field": field,
            "removed_index": index, "removed_class": removed_cls,
            "count": len(_tm_arr(ia, field)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_input_action_trigger(ctx, asset_path: str, trigger_type: str, properties: dict = None) -> str:
        """Append a Trigger to an InputAction's asset-level triggers array (reversible).

        trigger_type: an InputTrigger subclass name — 'InputTriggerHold' or short 'Hold' (also Pressed,
                      Released, Down, Tap, HoldAndRelease, Pulse, Combo, ChordAction, ChordBlocker,
                      RepeatedTap). List via list_trigger_types.
        properties:   optional config dict, e.g. {'HoldTimeThreshold': 0.75, 'ActuationThreshold': 0.6}.

        Ledgered op 'add_input_action_tm' {asset_path, field:'triggers', index}; inverse deletes the
        trigger at that index (it was appended at the end)."""
        params = {"asset_path": asset_path, "field": "triggers", "type_name": trigger_type,
                  "properties": _norm_props(properties)}
        try:
            return json.dumps(_exec(_ADD_IA_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_input_action_modifier(ctx, asset_path: str, modifier_type: str, properties: dict = None) -> str:
        """Append a Modifier to an InputAction's asset-level modifiers array (reversible).

        modifier_type: an InputModifier subclass name — 'InputModifierDeadZone' or short 'DeadZone' (also
                       Negate, Scalar, SwizzleAxis, Smooth, ScaleByDeltaTime, FOVScaling,
                       ResponseCurveExponential, ToWorldSpace). List via list_modifier_types.
        properties:    optional config dict, e.g. {'LowerThreshold': 0.25, 'Type': 'Radial'} or
                       {'Scalar': [1,0,0]} — enum props accept a token string.

        Ledgered op 'add_input_action_tm' {asset_path, field:'modifiers', index}; inverse deletes it."""
        params = {"asset_path": asset_path, "field": "modifiers", "type_name": modifier_type,
                  "properties": _norm_props(properties)}
        try:
            return json.dumps(_exec(_ADD_IA_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_input_action_trigger(ctx, asset_path: str, index: int) -> str:
        """Remove the Trigger at `index` from an InputAction's asset-level triggers array (reversible).

        Ledgered op 'remove_input_action_tm' {asset_path, field:'triggers', index, item:{class_path,props}};
        inverse rebuilds the trigger from the captured class+properties and re-inserts it at index."""
        params = {"asset_path": asset_path, "field": "triggers", "index": index}
        try:
            return json.dumps(_exec(_REMOVE_IA_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_input_action_modifier(ctx, asset_path: str, index: int) -> str:
        """Remove the Modifier at `index` from an InputAction's asset-level modifiers array (reversible).

        Ledgered op 'remove_input_action_tm' {asset_path, field:'modifiers', index, item:{class_path,props}};
        inverse rebuilds the modifier and re-inserts it at index."""
        params = {"asset_path": asset_path, "field": "modifiers", "index": index}
        try:
            return json.dumps(_exec(_REMOVE_IA_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_key_mapping — drop a mapping from an IMC (by index or match)  #
    # ------------------------------------------------------------------ #
    _REMOVE_KEY_MAPPING_BODY = _IW_HELPERS2 + r'''
imc_path = PARAMS["context_path"]; index = PARAMS.get("index")
action_path = PARAMS.get("action_path"); key = PARAMS.get("key")
imc, err = _load_typed(imc_path, "InputMappingContext")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    dkm, arr = _get_dkm(imc)
    if index is None:
        if not action_path or not key:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "give either index, or both action_path and key"}))
            arr = None
        else:
            found = [i for i, m in enumerate(arr)
                     if _map_action_path(m) == action_path and _map_key_name(m) == str(key)]
            if not found:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "no mapping matches action '%s' + key '%s'" % (action_path, key)}))
                arr = None
            elif len(found) > 1:
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "ambiguous: %d mappings match; pass an explicit index" % len(found),
                    "matching_indices": found}))
                arr = None
            else:
                index = found[0]
    if arr is not None:
        if index < 0 or index >= len(arr):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "index %s out of range (have %d mappings)" % (index, len(arr))}))
        else:
            snap = _snap_mapping(arr[index])
            with unreal.ScopedEditorTransaction("MCP remove_key_mapping"):
                del arr[index]; _set_dkm(imc, dkm, arr)
            _save(imc_path)
            _ledger().append({"op": "imc_remove_mapping", "asset_path": imc_path,
                "index": index, "mapping": snap})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": imc_path,
                "removed_index": index, "removed": snap,
                "mapping_count": len(_get_dkm(imc)[1]), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_key_mapping(ctx, context_path: str, index: int = None,
                           action_path: str = None, key: str = None) -> str:
        """Remove one key->action mapping from an InputMappingContext (reversible).

        context_path: the InputMappingContext asset path.
        index:        the mapping index to remove (from describe_input_mapping_context). OR
        action_path + key: identify the mapping by its bound action and key (refuses if ambiguous —
                      pass an explicit index instead).

        Ledgered op 'imc_remove_mapping' {asset_path, index, mapping:{action_path,key_name,triggers,
        modifiers}}; inverse rebuilds the full mapping (incl. per-mapping triggers/modifiers) and
        re-inserts it at its original index."""
        params = {"context_path": context_path, "index": index,
                  "action_path": action_path, "key": key}
        try:
            return json.dumps(_exec(_REMOVE_KEY_MAPPING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_key_mapping — change an existing mapping's key and/or action     #
    # ------------------------------------------------------------------ #
    _SET_KEY_MAPPING_BODY = _IW_HELPERS2 + r'''
imc_path = PARAMS["context_path"]; index = PARAMS["index"]
key = PARAMS.get("key"); action_path = PARAMS.get("action_path")
imc, err = _load_typed(imc_path, "InputMappingContext")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif key is None and action_path is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "nothing to set: give key and/or action_path"}))
else:
    dkm, arr = _get_dkm(imc)
    if index is None or index < 0 or index >= len(arr):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "index %s out of range (have %d mappings)" % (index, len(arr))}))
    else:
        new_action = None
        if action_path is not None:
            new_action, aerr = _load_typed(action_path, "InputAction")
            if aerr:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": aerr}))
                arr = None
        if arr is not None:
            prior = _snap_mapping(arr[index]); m = arr[index]
            with unreal.ScopedEditorTransaction("MCP set_key_mapping"):
                if new_action is not None:
                    m.set_editor_property("action", new_action)
                if key is not None:
                    kk = unreal.Key(); kk.set_editor_property("key_name", unreal.Name(str(key)))
                    m.set_editor_property("key", kk)
                arr[index] = m; _set_dkm(imc, dkm, arr)
            _save(imc_path)
            _ledger().append({"op": "imc_replace_mapping", "asset_path": imc_path,
                "index": index, "prior": prior})
            cur = _get_dkm(imc)[1][index]
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": imc_path, "index": index,
                "prior": {"key": prior["key_name"], "action": prior["action_path"]},
                "now": {"key": _map_key_name(cur), "action": _map_action_path(cur)},
                "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_key_mapping(ctx, context_path: str, index: int, key: str = None,
                        action_path: str = None) -> str:
        """Change the key and/or bound action of an existing IMC mapping in place (reversible).

        context_path: the InputMappingContext asset path.
        index:        the mapping index to edit.
        key:          new FKey name to bind (e.g. 'Q'), or omit to keep the current key.
        action_path:  new InputAction to bind, or omit to keep the current action.

        Per-mapping triggers/modifiers on that entry are preserved. Ledgered op 'imc_replace_mapping'
        {asset_path, index, prior:{...full mapping snapshot...}}; inverse restores the prior mapping
        exactly (key, action, and its triggers/modifiers)."""
        params = {"context_path": context_path, "index": index, "key": key, "action_path": action_path}
        try:
            return json.dumps(_exec(_SET_KEY_MAPPING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add / remove per-MAPPING trigger or modifier (shared bodies)        #
    # ------------------------------------------------------------------ #
    _ADD_MAP_TM_BODY = _IW_HELPERS2 + r'''
imc_path = PARAMS["context_path"]; mi = PARAMS["mapping_index"]; field = PARAMS["field"]
type_name = PARAMS["type_name"]; props = PARAMS.get("properties") or {}
base = unreal.InputTrigger if field == "triggers" else unreal.InputModifier
prefix = "InputTrigger" if field == "triggers" else "InputModifier"
imc, err = _load_typed(imc_path, "InputMappingContext")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    dkm, arr = _get_dkm(imc)
    if mi is None or mi < 0 or mi >= len(arr):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "mapping_index %s out of range (have %d mappings)" % (mi, len(arr))}))
    else:
        cls, cerr = _resolve_tm_class(type_name, base, prefix)
        if cerr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": cerr}))
        else:
            prior = _snap_mapping(arr[mi]); m = arr[mi]; sub = list(m.get_editor_property(field) or [])
            sidx = len(sub); applied = {}
            with unreal.ScopedEditorTransaction("MCP add_mapping_" + field):
                inst = unreal.new_object(cls, imc)
                for pn, val in props.items():
                    ok, serr = _set_prop(inst, pn, val); applied[pn] = {"ok": ok, "error": serr}
                sub.append(inst); m.set_editor_property(field, sub); arr[mi] = m; _set_dkm(imc, dkm, arr)
            _save(imc_path)
            _ledger().append({"op": "imc_replace_mapping", "asset_path": imc_path, "index": mi, "prior": prior})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": imc_path, "mapping_index": mi,
                "field": field, "added_index": sidx, "class": cls.get_default_object().get_class().get_name(),
                "applied_properties": applied, "count": len(_get_dkm(imc)[1][mi].get_editor_property(field)),
                "ledger_depth": len(_ledger())}))
'''

    _REMOVE_MAP_TM_BODY = _IW_HELPERS2 + r'''
imc_path = PARAMS["context_path"]; mi = PARAMS["mapping_index"]; field = PARAMS["field"]
si = PARAMS["sub_index"]
imc, err = _load_typed(imc_path, "InputMappingContext")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    dkm, arr = _get_dkm(imc)
    if mi is None or mi < 0 or mi >= len(arr):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "mapping_index %s out of range (have %d mappings)" % (mi, len(arr))}))
    else:
        m = arr[mi]; sub = list(m.get_editor_property(field) or [])
        if si is None or si < 0 or si >= len(sub):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "%s index %s out of range (mapping has %d)" % (field, si, len(sub))}))
        else:
            prior = _snap_mapping(arr[mi]); removed_cls = sub[si].get_class().get_name()
            with unreal.ScopedEditorTransaction("MCP remove_mapping_" + field):
                del sub[si]; m.set_editor_property(field, sub); arr[mi] = m; _set_dkm(imc, dkm, arr)
            _save(imc_path)
            _ledger().append({"op": "imc_replace_mapping", "asset_path": imc_path, "index": mi, "prior": prior})
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": imc_path, "mapping_index": mi,
                "field": field, "removed_index": si, "removed_class": removed_cls,
                "count": len(_get_dkm(imc)[1][mi].get_editor_property(field)), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_mapping_trigger(ctx, context_path: str, mapping_index: int, trigger_type: str,
                            properties: dict = None) -> str:
        """Add a per-mapping Trigger to one IMC mapping entry (reversible).

        These layer on top of the InputAction's asset-level triggers, for THIS key binding only.
        trigger_type: an InputTrigger subclass ('Hold', 'Pressed', ...); see list_trigger_types.
        properties:   optional config dict (e.g. {'HoldTimeThreshold': 0.5}).

        Ledgered op 'imc_replace_mapping' {asset_path, index=mapping_index, prior:{full snapshot}};
        inverse restores the mapping to its prior state (removing this trigger)."""
        params = {"context_path": context_path, "mapping_index": mapping_index, "field": "triggers",
                  "type_name": trigger_type, "properties": _norm_props(properties)}
        try:
            return json.dumps(_exec(_ADD_MAP_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def add_mapping_modifier(ctx, context_path: str, mapping_index: int, modifier_type: str,
                             properties: dict = None) -> str:
        """Add a per-mapping Modifier to one IMC mapping entry (reversible).

        These layer on top of the InputAction's asset-level modifiers, for THIS key binding only.
        modifier_type: an InputModifier subclass ('Negate', 'DeadZone', 'SwizzleAxis', ...); see
                       list_modifier_types. properties: optional config dict.

        Ledgered op 'imc_replace_mapping' {asset_path, index=mapping_index, prior:{full snapshot}};
        inverse restores the mapping to its prior state (removing this modifier)."""
        params = {"context_path": context_path, "mapping_index": mapping_index, "field": "modifiers",
                  "type_name": modifier_type, "properties": _norm_props(properties)}
        try:
            return json.dumps(_exec(_ADD_MAP_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_mapping_trigger(ctx, context_path: str, mapping_index: int, trigger_index: int) -> str:
        """Remove a per-mapping Trigger (at trigger_index) from one IMC mapping entry (reversible).

        Ledgered op 'imc_replace_mapping' {asset_path, index=mapping_index, prior:{full snapshot}};
        inverse restores the mapping (re-adding the removed trigger with its config)."""
        params = {"context_path": context_path, "mapping_index": mapping_index, "field": "triggers",
                  "sub_index": trigger_index}
        try:
            return json.dumps(_exec(_REMOVE_MAP_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_mapping_modifier(ctx, context_path: str, mapping_index: int, modifier_index: int) -> str:
        """Remove a per-mapping Modifier (at modifier_index) from one IMC mapping entry (reversible).

        Ledgered op 'imc_replace_mapping' {asset_path, index=mapping_index, prior:{full snapshot}};
        inverse restores the mapping (re-adding the removed modifier with its config)."""
        params = {"context_path": context_path, "mapping_index": mapping_index, "field": "modifiers",
                  "sub_index": modifier_index}
        try:
            return json.dumps(_exec(_REMOVE_MAP_TM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # Discovery reads (no ledger): list_trigger_types / list_modifier_types#
    # ------------------------------------------------------------------ #
    _LIST_TM_TYPES_BODY = _IW_HELPERS2 + r'''
field = PARAMS["field"]; flt = (PARAMS.get("filter") or "").lower()
base = unreal.InputTrigger if field == "triggers" else unreal.InputModifier
rows = []
for nm in dir(unreal):
    obj = getattr(unreal, nm, None)
    try:
        if not (inspect.isclass(obj) and issubclass(obj, base) and obj is not base):
            continue
    except Exception:
        continue
    if flt and flt not in nm.lower():
        continue
    cp = None; pnames = []
    try:
        cdo = obj.get_default_object(); cp = cdo.get_class().get_path_name(); pnames = _prop_names(cdo)
    except Exception:
        pass
    rows.append({"name": nm, "short": (nm[len("InputTrigger"):] if field == "triggers" and nm.startswith("InputTrigger") else (nm[len("InputModifier"):] if nm.startswith("InputModifier") else nm)),
        "class_path": cp, "config_properties": pnames})
rows.sort(key=lambda r: r["name"])
print("@@UMCP@@" + json.dumps({"status": "success", "kind": field, "count": len(rows),
    "types": rows, "note": ("Native %s subclasses exposed to Python (UE 5.8). 'config_properties' are "
        "the reflected FProperty names settable via add_*_%s properties." % (base.__name__, field[:-1]))}))
'''

    @mcp.tool()
    def list_trigger_types(ctx, filter: str = None) -> str:
        """List available Enhanced Input Trigger types (InputTrigger subclasses). Read-only.

        Returns each trigger class name (+ short name, class path, and its reflected config-property
        names, e.g. InputTriggerHold -> HoldTimeThreshold/ActuationThreshold). Use these with
        add_input_action_trigger / add_mapping_trigger. filter: case-insensitive substring on the name."""
        params = {"field": "triggers", "filter": filter}
        try:
            return json.dumps(_exec(_LIST_TM_TYPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def list_modifier_types(ctx, filter: str = None) -> str:
        """List available Enhanced Input Modifier types (InputModifier subclasses). Read-only.

        Returns each modifier class name (+ short name, class path, and reflected config-property names,
        e.g. InputModifierDeadZone -> LowerThreshold/UpperThreshold/Type). Use these with
        add_input_action_modifier / add_mapping_modifier. filter: case-insensitive substring on name."""
        params = {"field": "modifiers", "filter": filter}
        try:
            return json.dumps(_exec(_LIST_TM_TYPES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_input_keys — curated FKey catalog (UE 5.8 binds no EKeys enum)  #
    # ------------------------------------------------------------------ #
    _LIST_KEYS_BODY = _IW_HELPERS2 + r'''
flt = (PARAMS.get("filter") or "").lower(); cat = (PARAMS.get("category") or "").strip().lower()
max_results = int(PARAMS.get("max_results") or 300)
CATALOG = {
  "Keyboard": ["A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z",
    "Zero","One","Two","Three","Four","Five","Six","Seven","Eight","Nine",
    "NumPadZero","NumPadOne","NumPadTwo","NumPadThree","NumPadFour","NumPadFive","NumPadSix","NumPadSeven","NumPadEight","NumPadNine",
    "F1","F2","F3","F4","F5","F6","F7","F8","F9","F10","F11","F12",
    "SpaceBar","Enter","Escape","Tab","BackSpace","Delete","Insert","Home","End","PageUp","PageDown",
    "Up","Down","Left","Right","LeftShift","RightShift","LeftControl","RightControl","LeftAlt","RightAlt","LeftCommand","RightCommand",
    "CapsLock","Add","Subtract","Multiply","Divide","Decimal","Equals","Comma","Period","Slash","SemiColon","Apostrophe",
    "LeftBracket","RightBracket","Backslash","Hyphen","Tilde"],
  "Mouse": ["LeftMouseButton","RightMouseButton","MiddleMouseButton","ThumbMouseButton","ThumbMouseButton2",
    "MouseScrollUp","MouseScrollDown","MouseWheelAxis","MouseX","MouseY","Mouse2D"],
  "Gamepad": ["Gamepad_FaceButton_Bottom","Gamepad_FaceButton_Right","Gamepad_FaceButton_Left","Gamepad_FaceButton_Top",
    "Gamepad_DPad_Up","Gamepad_DPad_Down","Gamepad_DPad_Left","Gamepad_DPad_Right",
    "Gamepad_LeftShoulder","Gamepad_RightShoulder","Gamepad_LeftTrigger","Gamepad_RightTrigger",
    "Gamepad_LeftTriggerAxis","Gamepad_RightTriggerAxis","Gamepad_LeftThumbstick","Gamepad_RightThumbstick",
    "Gamepad_Special_Left","Gamepad_Special_Right",
    "Gamepad_LeftX","Gamepad_LeftY","Gamepad_RightX","Gamepad_RightY","Gamepad_Left2D","Gamepad_Right2D"],
  "Touch": ["Touch1","Touch2","Touch3","Touch4","Touch5","Touch6","Touch7","Touch8","Touch9","Touch10"],
  "Motion": ["Tilt","RotationRate","Gravity","Acceleration"],
}
rows = []
for c, keys in CATALOG.items():
    if cat and cat != c.lower():
        continue
    for kn in keys:
        if flt and flt not in kn.lower():
            continue
        rows.append({"key": kn, "category": c})
total = len(rows); window = rows[:max_results]
print("@@UMCP@@" + json.dumps({"status": "success", "total_matching": total, "returned": len(window),
    "truncated": total > len(window), "categories": sorted(CATALOG.keys()), "keys": window,
    "note": ("Curated canonical FKey catalog. UE 5.8 does NOT bind EKeys::GetAllKeys to Python (no "
        "InputKeyManager/EKeys/InputCoreTypes on unreal.*), so this is a maintained standard-key list, "
        "not a live registry dump; project-registered custom keys are not enumerated. Any name here is a "
        "valid FKey for add_mapping_to_context / set_key_mapping.")}))
'''

    @mcp.tool()
    def list_input_keys(ctx, filter: str = None, category: str = None, max_results: int = 300) -> str:
        """List common input keys (FKey names) usable in mappings. Read-only.

        Returns canonical FKey names grouped by category (Keyboard, Mouse, Gamepad, Touch, Motion) for
        use with add_mapping_to_context / set_key_mapping. filter: case-insensitive substring on the key
        name. category: restrict to one category. NOTE: this is a curated standard-key catalog — UE 5.8
        binds no EKeys::GetAllKeys to Python, so custom project-registered keys are not enumerated."""
        params = {"filter": filter, "category": category, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_KEYS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`.
    # Undo ops introduced by this module (report to coordinator for folding into editor_level.undo):
    #   create_asset (create_input_action / create_input_mapping_context) — already folded (generic).
    #   add_input_mapping (add_mapping_to_context) — already folded.
    #   set_input_action_properties {asset_path, prior:{name: ser}}       — NEW (G2-B).
    #   add_input_action_tm {asset_path, field, index}                    — NEW (G2-B).
    #   remove_input_action_tm {asset_path, field, index, item:{cp,props}}— NEW (G2-B).
    #   imc_remove_mapping {asset_path, index, mapping:{...}}             — NEW (G2-B).
    #   imc_replace_mapping {asset_path, index, prior:{...}}              — NEW (G2-B).
    # Exact inverse code is in .mcp_coord/coordinator-inbox/g2b_input_undo_ops.md.
    # ------------------------------------------------------------------ #
