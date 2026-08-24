"""UserTools :: Data Assets (WRITE + class discovery)  (spec: docs/spec/dataassets.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). The mutating +
class-discovery counterpart to the READ-ONLY dataassets.py. Query convention, base64 PARAMS
injection, Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py / the shipped write module datatable_write.py.

Implemented:
  - create_data_asset        (WRITE; ledgered op "create_asset" -> reuses editor_level's shipped
                              soft-delete inverse; NO new fold). Non-modal factory-preset create.
  - list_data_asset_classes  (READ; enumerate UDataAsset subclasses via reflection; no ledger)

create_data_asset mechanism (verified live vs TestMCPSetup, UE 5.8.1):
  UDataAsset (and UPrimaryDataAsset) are ABSTRACT, so a concrete non-abstract UDataAsset subclass
  must be chosen. Creation is the SAME non-modal "preset-factory" trick create_data_table uses:
    fac = unreal.DataAssetFactory(); fac.set_editor_property("data_asset_class", <ConcreteClass>)
    unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, package_path, <ConcreteClass>, fac)
  Because the DataAssetClass editor-prop is PRE-SET, plain create_asset never calls the factory's
  ConfigureProperties -> NO class-picker modal pops (proven: PrimaryAssetLabel created clean, saved,
  soft-deleted by rename, editor never wedged). The asset is saved on creation (persists it + makes
  the undo-delete reliable). initial_properties are applied (best-effort, coerced) BEFORE the final
  save so they are baked into the created asset -- the single "create_asset" ledger op's inverse
  (delete the whole asset) reverses them too, so NO separate property op / undo fold is needed.

list_data_asset_classes mechanism:
  Walks unreal.* for UDataAsset subclasses (the same class-walk the shipped list_widget_types uses),
  reading is_abstract/parent_class from the native unreal.MCPReflectionLibrary.get_class_metadata_json
  reflection helper. This enumerates the Python-reflected (native/C++) UDataAsset hierarchy; it does
  NOT enumerate Blueprint-generated UDataAsset classes (they are not bound on unreal.*) -- reported
  honestly. A Blueprint DataAsset class can still be passed to create_data_asset by its class path.

Undo: this module registers NO `undo` tool (editor_level.py owns the unified `undo`). create_data_asset
reuses the ALREADY-FOLDED generic "create_asset" op {asset_path, package_path, created_dir}; there is
NOTHING new for the coordinator to fold.

HARD CONSTRAINTS honored: snippet bodies contain NO triple-single-quotes and NO stray backslashes;
all params travel as base64 JSON via _exec; no reserved local names are assigned.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


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

    # Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash.
    #   _ledger()        -> per-session undo stack (verbatim from editor_level.py).
    #   _coerce/_enum_name -> value coercion for initial_properties (verbatim from editor_level.py).
    #   _resolve_class(spec) -> resolve a class spec (unreal.<Name> OR a /Script or Blueprint class path).
    #   _class_meta(cls) -> native reflection metadata {is_abstract, parent_class, ...} (guarded).
    _DA_HELPERS = r'''
import unreal, json, builtins
_MRL = getattr(unreal, "MCPReflectionLibrary", None)
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if (current is None or isinstance(current, unreal.Object)) and isinstance(value, str):
        obj = None
        try:
            obj = unreal.EditorAssetLibrary.load_asset(value)
        except Exception:
            obj = None
        if obj is not None:
            return obj
        if isinstance(current, unreal.Object):
            return None
        return value
    if isinstance(current, bool):
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off", ""):
                return False
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, str):
            try:
                return int(value.strip())
            except Exception:
                try:
                    return int(float(value.strip()))
                except Exception:
                    return value
        if isinstance(value, (int, float)):
            return int(value)
        return value
    if isinstance(current, float):
        if isinstance(value, str):
            try:
                return float(value.strip())
            except Exception:
                return value
        if isinstance(value, (int, float)):
            return float(value)
        return value
    return value
def _resolve_class(spec):
    if not spec:
        return None
    spec = str(spec)
    # Bare name (no path separators) -> try the unreal.* namespace FIRST (fast + no log noise);
    # a path-like spec (/Script/... or a Blueprint class path) -> load_class FIRST.
    path_like = ("/" in spec) or ("." in spec)
    if not path_like:
        o = getattr(unreal, spec, None)
        if isinstance(o, type):
            try:
                return o.static_class()
            except Exception:
                return None
    c = None
    try:
        c = unreal.load_class(None, spec)
    except Exception:
        c = None
    if isinstance(c, unreal.Class):
        return c
    o = getattr(unreal, spec, None)
    if isinstance(o, type):
        try:
            return o.static_class()
        except Exception:
            return None
    return None
def _class_meta(cls):
    if _MRL is not None and hasattr(_MRL, "get_class_metadata_json"):
        try:
            return json.loads(_MRL.get_class_metadata_json(cls))
        except Exception:
            return {}
    return {}
'''

    # ------------------------------------------------------------------ #
    # create_data_asset — new UDataAsset via preset factory (non-modal)   #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _DA_HELPERS + r'''
name = str(PARAMS.get("name") or "").strip()
package_path = str(PARAMS.get("path") or "/Game/MCP_Scratch").rstrip("/")
class_spec = PARAMS.get("data_asset_class")
initial = PARAMS.get("initial_properties") or {}
if not name:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "name is required"}))
elif not class_spec:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "data_asset_class is required (a UDataAsset subclass name like 'PrimaryAssetLabel' or a class path like /Script/Engine.PrimaryAssetLabel)"}))
else:
    cls = _resolve_class(class_spec)
    if cls is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not resolve data_asset_class '%s' (tried unreal.<name>.static_class() and load_class)" % class_spec}))
    else:
        # verify it is a concrete (non-abstract) UDataAsset subclass
        cdo = None
        try:
            cdo = unreal.get_default_object(cls)
        except Exception:
            cdo = None
        is_da = isinstance(cdo, unreal.DataAsset) if cdo is not None else False
        meta = _class_meta(cls)
        is_abstract = bool(meta.get("is_abstract"))
        if not is_da:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "class '%s' is not a UDataAsset subclass" % cls.get_name(),
                "class_path": cls.get_path_name()}))
        elif is_abstract:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "class '%s' is ABSTRACT and cannot be instantiated (UDataAsset and UPrimaryDataAsset themselves are abstract); choose a concrete subclass (see list_data_asset_classes)" % cls.get_name(),
                "class_path": cls.get_path_name()}))
        else:
            full = package_path + "/" + name
            if unreal.EditorAssetLibrary.does_asset_exist(full):
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
            else:
                dir_existed = unreal.EditorAssetLibrary.does_directory_exist(package_path)
                fac = unreal.DataAssetFactory()
                fac_ok = True
                try:
                    fac.set_editor_property("data_asset_class", cls)
                except Exception as e:
                    fac_ok = False
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "could not preset DataAssetFactory.data_asset_class: %s" % str(e)[:160]}))
                if fac_ok:
                    tools = unreal.AssetToolsHelpers.get_asset_tools()
                    da = tools.create_asset(name, package_path, cls, fac)
                    if da is None:
                        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None"}))
                    else:
                        # apply initial_properties (best-effort, coerced) BEFORE save so they bake into
                        # the created asset; the single create_asset undo (delete) reverses them too.
                        applied = []; skipped = []
                        for k in list(initial.keys()):
                            try:
                                cur = da.get_editor_property(k)
                            except Exception as e:
                                skipped.append({"name": k, "reason": "no such editor property (%s)" % str(e)[:80]})
                                continue
                            try:
                                da.set_editor_property(k, _coerce(cur, initial[k]))
                                applied.append(k)
                            except Exception as e:
                                skipped.append({"name": k, "reason": str(e)[:100]})
                        try:
                            unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
                            saved = True
                        except Exception:
                            saved = False
                        created_dir = None if dir_existed else package_path
                        _ledger().append({"op": "create_asset", "asset_path": da.get_path_name(),
                            "package_path": package_path, "created_dir": created_dir})
                        print("@@UMCP@@" + json.dumps({"status": "success",
                            "asset_path": da.get_path_name(), "name": name,
                            "class": da.get_class().get_name(),
                            "is_primary_data_asset": bool(isinstance(da, unreal.PrimaryDataAsset)),
                            "saved": saved,
                            "initial_properties_applied": applied,
                            "initial_properties_skipped": skipped,
                            "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_data_asset(ctx, name: str, path: str, data_asset_class: str,
                          initial_properties: dict = None) -> str:
        """Create a new UDataAsset asset of a chosen concrete class (ledgered write; NON-MODAL).

        name:              asset name for the new data asset.
        path:              destination content path (e.g. /Game/Data, /Game/MCP_Scratch). Must be a
                           mounted root (/Game/, /Engine/, plugin roots) -- never /Temp/.
        data_asset_class:  the concrete UDataAsset subclass to instantiate -- a class NAME exposed on
                           unreal.* (e.g. 'PrimaryAssetLabel', 'PlayerMappableInputConfig') OR a class
                           path ('/Script/Engine.PrimaryAssetLabel', or a Blueprint class path like
                           '/Game/Data/BP_MyDataAsset.BP_MyDataAsset_C'). UDataAsset and UPrimaryDataAsset
                           themselves are ABSTRACT and are refused -- pick a concrete subclass
                           (use list_data_asset_classes to browse them).
        initial_properties: optional {property_name: value} applied via set_editor_property (best-effort,
                           type-coerced). Property names are snake_case editor names; unknown or
                           unsettable ones are reported under initial_properties_skipped (not fatal).

        Uses the same non-modal trick as create_data_table: a DataAssetFactory with its DataAssetClass
        PRE-SET, then plain create_asset (which does NOT call the factory's ConfigureProperties, so NO
        class-picker modal pops) + save. Refuses if an asset already exists at the target path.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir} -- REUSES the shipped
        generic create-asset inverse (the unified editor_level.undo deletes the created asset, and the
        scratch dir if this call created it). NO new undo fold is required."""
        params = {"name": name, "path": path, "data_asset_class": data_asset_class,
                  "initial_properties": initial_properties or {}}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # list_data_asset_classes — enumerate UDataAsset subclasses (read)    #
    # ------------------------------------------------------------------ #
    _LIST_CLASSES_BODY = _DA_HELPERS + r'''
flt = (PARAMS.get("filter") or "").lower()
include_abstract = bool(PARAMS.get("include_abstract"))
base = unreal.DataAsset
rows = []
seen_paths = set()
for nm in dir(unreal):
    o = getattr(unreal, nm, None)
    try:
        ok = isinstance(o, type) and issubclass(o, base)
    except Exception:
        ok = False
    if not ok:
        continue
    try:
        cls = o.static_class()
        cpath = cls.get_path_name()
    except Exception:
        cls = None; cpath = None
    if cpath is None or cpath in seen_paths:
        continue
    seen_paths.add(cpath)
    meta = _class_meta(cls) if cls is not None else {}
    is_abstract = meta.get("is_abstract")
    if is_abstract is None:
        is_abstract = False
    if is_abstract and not include_abstract:
        continue
    try:
        is_primary = bool(issubclass(o, unreal.PrimaryDataAsset))
    except Exception:
        is_primary = False
    row = {"name": nm, "class_path": cpath, "is_abstract": bool(is_abstract),
           "is_primary_data_asset": is_primary, "parent_class": meta.get("parent_class")}
    if flt and (flt not in nm.lower() and flt not in cpath.lower()):
        continue
    rows.append(row)
rows.sort(key=lambda r: r["name"])
concrete = [r for r in rows if not r["is_abstract"]]
print("@@UMCP@@" + json.dumps({"status": "success",
    "count": len(rows), "concrete_count": len(concrete),
    "filter": PARAMS.get("filter") or None, "include_abstract": include_abstract,
    "source": "reflection walk of UDataAsset subclasses exposed on unreal.* + MCPReflectionLibrary.get_class_metadata_json",
    "note": ("Enumerates the Python-reflected (native/C++) UDataAsset class hierarchy. Blueprint-"
             "generated UDataAsset classes are NOT bound on unreal.* so are not listed here; a "
             "Blueprint DataAsset class can still be passed to create_data_asset by its class path "
             "(e.g. /Game/Data/BP_MyDA.BP_MyDA_C). UDataAsset and UPrimaryDataAsset are abstract and "
             "only shown when include_abstract=true."),
    "classes": rows}))
'''

    @mcp.tool()
    def list_data_asset_classes(ctx, filter: str = "", include_abstract: bool = False) -> str:
        """List the UDataAsset subclasses available to create (read-only).

        filter:           case-insensitive substring matched against the class name and class path
                          (e.g. 'Input', 'Niagara', 'Primary'). Empty = all.
        include_abstract: include abstract classes (UDataAsset / UPrimaryDataAsset and any abstract
                          subclasses) in the list (default False -- only instantiable classes).

        Walks the UDataAsset hierarchy exposed on unreal.* (the same reflection the list_widget_types
        tool uses) and reads is_abstract / parent_class from the native MCPReflectionLibrary. Each row:
        {name, class_path, is_abstract, is_primary_data_asset, parent_class}. The class_path (or the
        name) feeds create_data_asset's data_asset_class.

        LIMITATION (honest, not faked): this lists Python-reflected NATIVE classes only. Blueprint-
        generated UDataAsset classes are not bound on unreal.* and are not enumerated here -- but one
        can still be created via create_data_asset by passing its Blueprint class path."""
        params = {"filter": filter, "include_abstract": include_abstract}
        try:
            return json.dumps(_exec(_LIST_CLASSES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
