"""UserTools :: PCG compute-source / custom-HLSL / subgraph-override authoring  (spec: docs/spec/pcg.md — Wave 3)

Pure-Python, asset-only authoring on the reflected PCG surface (UE 5.8, PCG plugin loaded). NO C++ /
NO engine build. Every write mutates an ASSET (a UPCGComputeSource asset, or a PCGGraph asset — never
a level) and records ONE inverse op on the per-session agent ledger (builtins._UMCP_LEDGERS[session]);
the coordinator folds each new pcg_* op into editor_level.undo. A non-validating save runs after each
edit (EditorLoadingAndSavingUtils.save_packages([asset.get_outermost()], False)).

Reflection facts probed live (2026-08-19, TestMCPSetup):
  - UPCGComputeSource is NOT bound on unreal.* by name; it is reachable ONLY via
    unreal.PCGComputeSourceFactory().supported_class (class name 'PCGComputeSource'). Its base
    UComputeSource IS bound as unreal.ComputeSource.
  - PCGComputeSource has an FString `source` (HLSL body; get/set via get_editor_property) and an
    `additional_sources` array which is a TArray<UComputeSource*> OBJECT-REF array (append/assign a
    compute-source asset; NOT free-form named strings). Both are reflected + writable.
  - AssetRegistry classifies these under TopLevelAssetPath('/Script/PCG','PCGComputeSource').
  - UPCGCustomHLSLSettings is bound; its `kernel_type` is a PCGKernelType enum (values:
    POINT_PROCESSOR[default], POINT_GENERATOR, ATTRIBUTE_SET_GENERATOR/PROCESSOR,
    TEXTURE_GENERATOR/PROCESSOR, TEXTURE_ARRAY_GENERATOR/PROCESSOR, CUSTOM) reachable via
    get/set_editor_property('kernel_type') on a placed node's settings (even though it is not a
    Python getset descriptor).
  - Subgraph override: PCGGraph::UpdatePropertyOverride / ResetPropertyToDefault (PCGGraph.h:88/91)
    are UE_API-not-UFUNCTION and are NOT bound (graph exposes only is_editor_property_overridden /
    reset_editor_property / override_color / override_title). The reachable per-node override surface
    is the subgraph node settings' `subgraph_instance` (a UPCGGraphInstance) on which
    is_editor_property_overridden(prop) reads state, reset_editor_property(prop) reverts, and
    PCGGraphParametersHelpers.set_<type>_parameter overrides an exposed subgraph user-parameter.
    NOTE: no PCGGraph in this project exposes user_parameters, and defining them from Python is not
    reachable (Wave-5 C++), so the named-parameter override leg is implemented + reachable but only
    structurally validated — hence set/reset are reported PARTIAL.

REUSE: the _wrap/_query/_exec scaffold, _PCG_HELPERS reflection block (_ledger / _pcg_load_graph /
_pcg_resolve_node / _pcg_save / _pcg_settable / _pcg_coerce / _pcg_resolve_settings_class) are copied
verbatim from pcg_write.py so the editor_level.undo folds (which use the identical helpers) round-trip.
No ''' and no backslashes in any snippet body; all data crosses as base64 PARAMS; reserved locals
untouched. Scratch-only mutations (asset MUST be under /Game/MCP_Scratch).

NEW pcg_* ledger ops handed to the coordinator (op -> inverse):
  - pcg_set_compute_source{asset_path,prior_source,had_prior} -> set_editor_property('source',prior) [LOSSLESS]
  - pcg_add_compute_source_additional{asset_path,ref_path,index} -> remove the appended ref [LOSSLESS]
  - pcg_remove_compute_source_additional{asset_path,ref_path,index} -> re-insert the removed ref [LOSSLESS]
  - pcg_set_hlsl_kernel_type{graph_path,node_name,prior,had_prior} -> set_editor_property('kernel_type',prior) [LOSSLESS]
  - pcg_set_subgraph_override{graph_path,node_name,property,param_type,via,prior_value,prior_overridden,had_prior}
        -> restore prior override value / reset to default on subgraph_instance [LOSSY best-effort]
  - pcg_reset_subgraph_override{graph_path,node_name,property,param_type,via,prior_value,prior_overridden,had_prior}
        -> re-apply the captured prior override value on subgraph_instance [LOSSY best-effort]
  Creates reuse the EXISTING editor_level.undo 'create_asset' fold (no new fold).
"""
import json
import base64
import textwrap
import os
import time

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


# Shared authoring helpers (prepended to every write body). No ''' / no backslashes.
# Copied verbatim from pcg_write.py plus a generic-asset loader + compute-source resolver so the
# editor_level.undo folds (which use the identical PCGGraph helpers) round-trip.
_PCG_HELPERS = r'''
import unreal, json, builtins
warnings = None
try:
    import warnings as _wmod
    _wmod.simplefilter("ignore")
except Exception:
    pass

def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]

def _norm_path(p):
    return p.split(".")[0] if p else p

def _pcg_load_graph(p):
    a = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if a is None:
        return None, "asset not found: %s" % p
    if not isinstance(a, unreal.PCGGraph):
        cn = "?"
        try:
            cn = a.get_class().get_name()
        except Exception:
            pass
        return None, "asset is not a PCGGraph (class=%s): %s" % (cn, p)
    return a, None

def _cs_class():
    # UPCGComputeSource is reachable only via the factory's supported_class.
    fac = getattr(unreal, "PCGComputeSourceFactory", None)
    if fac is None:
        return None
    try:
        return fac().supported_class
    except Exception:
        return None

def _is_compute_source(a):
    if a is None:
        return False
    base = getattr(unreal, "ComputeSource", None)
    if base is not None:
        try:
            if isinstance(a, base):
                return True
        except Exception:
            pass
    try:
        return "ComputeSource" in a.get_class().get_name()
    except Exception:
        return False

def _pcg_load_compute_source(p):
    a = unreal.EditorAssetLibrary.load_asset(p) if p else None
    if a is None:
        return None, "asset not found: %s" % p
    if not _is_compute_source(a):
        cn = "?"
        try:
            cn = a.get_class().get_name()
        except Exception:
            pass
        return None, "asset is not a PCGComputeSource (class=%s): %s" % (cn, p)
    return a, None

_INPUT_TOKENS = ("input", "__input__", "in", "inputnode")
_OUTPUT_TOKENS = ("output", "__output__", "out", "outputnode")

def _pcg_resolve_node(g, node_id):
    if node_id is None:
        return None
    key = str(node_id)
    kl = key.lower()
    if kl in _INPUT_TOKENS:
        return g.get_input_node()
    if kl in _OUTPUT_TOKENS:
        return g.get_output_node()
    for n in list(g.get_editor_property("nodes") or []):
        try:
            if n.get_name() == key:
                return n
        except Exception:
            pass
    for getter in (g.get_input_node, g.get_output_node):
        try:
            nn = getter()
            if nn is not None and nn.get_name() == key:
                return nn
        except Exception:
            pass
    return None

def _pcg_save(obj):
    try:
        return bool(unreal.EditorLoadingAndSavingUtils.save_packages([obj.get_outermost()], False))
    except Exception:
        try:
            return bool(unreal.EditorAssetLibrary.save_asset(obj.get_path_name().split(".")[0], only_if_is_dirty=False))
        except Exception:
            return False

def _pcg_enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s

def _pcg_settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Vector2D):
        return ([v.x, v.y], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _pcg_enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)

def _pcg_coerce(current, value):
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
    if isinstance(current, unreal.Vector2D) and isinstance(value, (list, tuple)) and len(value) >= 2:
        return unreal.Vector2D(float(value[0]), float(value[1]))
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
    if isinstance(current, unreal.EnumBase) and isinstance(value, dict) and "__enum__" in value:
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
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
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except Exception:
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except Exception:
            return current
    return value

# Typed graph-parameter helper map (PCGGraphParametersHelpers) for subgraph-instance overrides.
_PARAM_TYPEMAP = {
    "float": ("get_float_parameter", "set_float_parameter", float),
    "double": ("get_double_parameter", "set_double_parameter", float),
    "int": ("get_int32_parameter", "set_int32_parameter", int),
    "int32": ("get_int32_parameter", "set_int32_parameter", int),
    "int64": ("get_int64_parameter", "set_int64_parameter", int),
    "bool": ("get_bool_parameter", "set_bool_parameter", lambda v: str(v).strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)),
    "string": ("get_string_parameter", "set_string_parameter", str),
    "name": ("get_name_parameter", "set_name_parameter", str),
}

def _subgraph_instance(g, node_id):
    # resolve the subgraph node's UPCGGraphInstance (the per-node override surface).
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        return None, None, "node not found: %s" % node_id
    s = node.get_settings()
    if s is None or not hasattr(s, "set_subgraph"):
        cn = (s.get_class().get_name() if s else None)
        return node, None, "node is not a subgraph node (settings has no set_subgraph): %s" % cn
    inst = None
    try:
        inst = s.get_editor_property("subgraph_instance")
    except Exception:
        inst = None
    if inst is None or not isinstance(inst, unreal.Object):
        return node, None, "subgraph node has no resolvable subgraph_instance (assign a subgraph first)"
    return node, inst, None
'''


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

    def _scratch_guard(asset_path, label="asset"):
        if not asset_path:
            return "%s path is required" % label
        if not _norm(asset_path).startswith("/Game/MCP_Scratch"):
            return "%s must be under /Game/MCP_Scratch (scratch-only); got %s" % (label, asset_path)
        return None

    def _norm(p):
        return p.split(".")[0] if p else p

    def _norm_target(asset_path, name, package_path, default_prefix):
        if asset_path:
            ap = _norm(asset_path)
            parts = ap.split("/")
            pkg = "/".join(parts[:-1]); nm = parts[-1]
        else:
            pkg = package_path or "/Game/MCP_Scratch"; nm = name
        if not nm:
            nm = default_prefix + str(int(time.time()))[-6:]
        if not nm.startswith("MCP_"):
            nm = "MCP_PCG_" + nm
        return pkg, nm

    # ------------------------------------------------------------------ #
    # 1. create_pcg_compute_source — PCGComputeSourceFactory              #
    # ------------------------------------------------------------------ #
    _CREATE_CS_BODY = _PCG_HELPERS + r'''
name = PARAMS.get("name")
pkg = PARAMS.get("package_path") or "/Game/MCP_Scratch"
initial = PARAMS.get("source")
full = pkg + "/" + name
created_dir = None
cls = _cs_class()
if cls is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGComputeSourceFactory not present (reflected-gated)"}))
elif unreal.EditorAssetLibrary.does_asset_exist(full):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "asset already exists: %s" % full}))
else:
    if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
        unreal.EditorAssetLibrary.make_directory(pkg)
        created_dir = pkg
    at = unreal.AssetToolsHelpers.get_asset_tools()
    fac = getattr(unreal, "PCGComputeSourceFactory")()
    asset = at.create_asset(name, pkg, cls, fac)
    if asset is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % full}))
    else:
        src_set = None
        if initial:
            try:
                asset.set_editor_property("source", str(initial)); src_set = True
            except Exception as _e:
                src_set = "err:" + str(_e)[:80]
        unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
        _ledger().append({"op": "create_asset", "asset_path": full,
                          "package_path": pkg, "created_dir": created_dir})
        _rb = None
        try:
            _rb = str(asset.get_editor_property("source"))[:60]
        except Exception:
            pass
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": full,
            "class": asset.get_class().get_name(), "package_path": pkg, "created_dir": created_dir,
            "source_set": src_set, "source_preview": _rb,
            "undo_op": "create_asset", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_pcg_compute_source(ctx, name: str = None, source: str = None,
                                  asset_path: str = None,
                                  package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a UPCGComputeSource asset (via PCGComputeSourceFactory) — a reusable HLSL source
        object referenced by PCG compute/Custom-HLSL nodes. Scratch-only. Ledgered write (op
        'create_asset' — the EXISTING editor_level.undo branch; `undo` deletes the asset). Saved on
        create so the undo-delete is reliable.

        name:         asset name; auto-prefixed 'MCP_PCG_' for ownership. Auto-generated if omitted.
        source:       optional initial HLSL body to write into the 'source' FString.
        asset_path:   optional full '/Game/MCP_Scratch/<Name>' (overrides name/package_path).
        package_path: content folder; MUST be under '/Game/MCP_Scratch'."""
        try:
            pkg, nm = _norm_target(asset_path, name, package_path, "MCP_PCG_CS_")
            if not pkg.startswith("/Game/MCP_Scratch"):
                return json.dumps({"status": "error",
                    "message": "package must be under /Game/MCP_Scratch (scratch-only); got %s" % pkg}, indent=2)
            return json.dumps(_exec(_CREATE_CS_BODY,
                {"name": nm, "package_path": pkg, "source": source}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. list_pcg_compute_sources — AssetRegistry                         #
    # ------------------------------------------------------------------ #
    _LIST_CS_BODY = r'''
import unreal, json
path_filter = PARAMS.get("path_filter")
name_filter = PARAMS.get("name_filter")
max_results = PARAMS.get("max_results")
ar = unreal.AssetRegistryHelpers.get_asset_registry()
out = []
err = None
try:
    tlap = unreal.TopLevelAssetPath("/Script/PCG", "PCGComputeSource")
    assets = ar.get_assets_by_class(tlap, search_sub_classes=True) or []
except Exception as e:
    assets = []; err = str(e)
for a in assets:
    name = str(a.asset_name)
    pkg = str(a.package_name)
    if path_filter and path_filter.lower() not in pkg.lower():
        continue
    if name_filter and name_filter.lower() not in name.lower():
        continue
    out.append({"name": name, "path": pkg, "class": str(a.asset_class_path.asset_name)})
out.sort(key=lambda g: g["path"])
total = len(out)
if max_results:
    out = out[:int(max_results)]
res = {"status": "success", "total": total, "returned": len(out), "compute_sources": out}
if err:
    res["registry_error"] = err
print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def list_pcg_compute_sources(ctx, path_filter: str = None, name_filter: str = None,
                                 max_results: int = None) -> str:
        """List UPCGComputeSource assets in the project via the Asset Registry. Read-only, no ledger.

        path_filter: case-insensitive substring on the package path.
        name_filter: case-insensitive substring on the asset name.
        max_results: cap returned (after filtering); 'total' reports the full count.

        Each entry: {name, path, class}. Includes the engine's /PCG/ComputeSources library."""
        try:
            return json.dumps(_exec(_LIST_CS_BODY,
                {"path_filter": path_filter, "name_filter": name_filter,
                 "max_results": max_results}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. get_pcg_compute_source — read source + additional sources        #
    # ------------------------------------------------------------------ #
    _GET_CS_BODY = _PCG_HELPERS + r'''
ap = PARAMS.get("asset_path")
a, err = _pcg_load_compute_source(ap)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    src = ""
    try:
        src = str(a.get_editor_property("source"))
    except Exception as _e:
        src = None
    add = []
    try:
        for e in list(a.get_editor_property("additional_sources") or []):
            try:
                if isinstance(e, unreal.Object) and e is not None:
                    add.append({"path": _norm_path(e.get_path_name()), "class": e.get_class().get_name()})
                else:
                    add.append({"value": str(e)[:80]})
            except Exception:
                add.append({"value": "<unreadable>"})
    except Exception as _e:
        add = None
    _srclen = len(src) if isinstance(src, str) else None
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap,
        "class": a.get_class().get_name(),
        "source": src, "source_length": _srclen,
        "additional_sources": add, "additional_source_count": (len(add) if add is not None else None)},
        default=str))
'''

    @mcp.tool()
    def get_pcg_compute_source(ctx, asset_path: str) -> str:
        """Read a UPCGComputeSource asset: its 'source' HLSL body and its 'additional_sources'
        (object references to other compute-source assets). Read-only, no ledger. Works on any asset
        (engine library included) — not restricted to scratch.

        asset_path: content path to the UPCGComputeSource asset."""
        try:
            return json.dumps(_exec(_GET_CS_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. set_pcg_compute_source — set the HLSL body (LEDGERED)             #
    # ------------------------------------------------------------------ #
    _SET_CS_BODY = _PCG_HELPERS + r'''
ap = PARAMS.get("asset_path")
value = PARAMS.get("source")
a, err = _pcg_load_compute_source(ap)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    had_prior = True; prior = None
    try:
        prior = str(a.get_editor_property("source"))
    except Exception:
        had_prior = False
    try:
        a.set_editor_property("source", "" if value is None else str(value))
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "set source failed: %s" % _e}))
        a = None
    if a is not None:
        rb = None
        try:
            rb = str(a.get_editor_property("source"))
        except Exception:
            pass
        _ledger().append({"op": "pcg_set_compute_source", "asset_path": _norm_path(ap),
                          "prior_source": prior, "had_prior": had_prior})
        _pcg_save(a)
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap,
            "prior_length": (len(prior) if isinstance(prior, str) else None),
            "new_length": (len(rb) if isinstance(rb, str) else None),
            "new_preview": (rb[:80] if isinstance(rb, str) else None), "had_prior": had_prior,
            "undo_op": "pcg_set_compute_source", "ledger_depth": len(_ledger())}, default=str))
'''

    @mcp.tool()
    def set_pcg_compute_source(ctx, asset_path: str, source: str) -> str:
        """Set the 'source' HLSL body (FString) of a UPCGComputeSource asset. Captures the prior
        source. Ledgered write (op 'pcg_set_compute_source' -> `undo` restores the prior string,
        LOSSLESS). Scratch-only.

        asset_path: content path to the UPCGComputeSource (MUST be under /Game/MCP_Scratch).
        source:     the new HLSL body text."""
        try:
            gerr = _scratch_guard(asset_path, "compute source")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_CS_BODY,
                {"asset_path": asset_path, "source": source}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5. add_pcg_compute_source_additional — append a UComputeSource ref  #
    # ------------------------------------------------------------------ #
    _ADD_ADDL_BODY = _PCG_HELPERS + r'''
ap = PARAMS.get("asset_path")
ref = PARAMS.get("source_ref")
a, err = _pcg_load_compute_source(ap)
rref = _norm_path(ref)
robj = unreal.EditorAssetLibrary.load_asset(rref) if rref else None
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif robj is None or not _is_compute_source(robj):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "source_ref is not a UComputeSource/PCGComputeSource asset: %s" % ref}))
else:
    try:
        arr = list(a.get_editor_property("additional_sources") or [])
    except Exception as _e:
        arr = []
    before = len(arr)
    # avoid duplicate append of the same ref
    exists = False
    for e in arr:
        try:
            if isinstance(e, unreal.Object) and _norm_path(e.get_path_name()) == rref:
                exists = True; break
        except Exception:
            pass
    if exists:
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap, "source_ref": rref,
            "added": False, "note": "ref already present (not ledgered)",
            "additional_source_count": before}))
    else:
        arr.append(robj)
        try:
            a.set_editor_property("additional_sources", arr)
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "append failed: %s" % _e}))
            a = None
        if a is not None:
            after = len(list(a.get_editor_property("additional_sources") or []))
            _ledger().append({"op": "pcg_add_compute_source_additional", "asset_path": _norm_path(ap),
                              "ref_path": rref, "index": before})
            _pcg_save(a)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap, "source_ref": rref,
                "added": True, "index": before, "count_before": before, "count_after": after,
                "undo_op": "pcg_add_compute_source_additional", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_pcg_compute_source_additional(ctx, asset_path: str, source_ref: str) -> str:
        """Append an additional source reference to a UPCGComputeSource's 'additional_sources' array.
        NOTE (probed live): this array is a TArray<UComputeSource*> of OBJECT REFERENCES — not
        free-form named strings — so `source_ref` is the content path of another compute-source asset
        to reference. Captures the appended ref + index. Ledgered write (op
        'pcg_add_compute_source_additional' -> `undo` removes the appended ref, LOSSLESS). Scratch-only
        target.

        asset_path: the UPCGComputeSource to modify (MUST be under /Game/MCP_Scratch).
        source_ref: content path to another UComputeSource/PCGComputeSource asset to add (may live
                    anywhere, e.g. the engine /PCG/ComputeSources library)."""
        try:
            gerr = _scratch_guard(asset_path, "compute source")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not source_ref:
                return json.dumps({"status": "error", "message": "source_ref is required"}, indent=2)
            return json.dumps(_exec(_ADD_ADDL_BODY,
                {"asset_path": asset_path, "source_ref": source_ref}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 6. remove_pcg_compute_source_additional — remove a ref              #
    # ------------------------------------------------------------------ #
    _REMOVE_ADDL_BODY = _PCG_HELPERS + r'''
ap = PARAMS.get("asset_path")
ref = PARAMS.get("source_ref")
a, err = _pcg_load_compute_source(ap)
rref = _norm_path(ref)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    try:
        arr = list(a.get_editor_property("additional_sources") or [])
    except Exception:
        arr = []
    before = len(arr)
    kept = []; removed_index = None
    for i, e in enumerate(arr):
        match = False
        try:
            if isinstance(e, unreal.Object) and e is not None and _norm_path(e.get_path_name()) == rref:
                match = True
        except Exception:
            match = False
        if match and removed_index is None:
            removed_index = i
        else:
            kept.append(e)
    if removed_index is None:
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap, "source_ref": rref,
            "removed": False, "note": "ref not present (not ledgered)",
            "additional_source_count": before}))
    else:
        try:
            a.set_editor_property("additional_sources", kept)
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "remove failed: %s" % _e}))
            a = None
        if a is not None:
            after = len(list(a.get_editor_property("additional_sources") or []))
            _ledger().append({"op": "pcg_remove_compute_source_additional", "asset_path": _norm_path(ap),
                              "ref_path": rref, "index": removed_index})
            _pcg_save(a)
            print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ap, "source_ref": rref,
                "removed": True, "index": removed_index, "count_before": before, "count_after": after,
                "undo_op": "pcg_remove_compute_source_additional", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_pcg_compute_source_additional(ctx, asset_path: str, source_ref: str) -> str:
        """Remove an additional source reference (by content path) from a UPCGComputeSource's
        'additional_sources' object-ref array. Captures the removed ref + its index. Ledgered write
        (op 'pcg_remove_compute_source_additional' -> `undo` re-inserts the ref, LOSSLESS). Scratch-only
        target.

        asset_path: the UPCGComputeSource to modify (MUST be under /Game/MCP_Scratch).
        source_ref: content path of the referenced compute-source asset to remove."""
        try:
            gerr = _scratch_guard(asset_path, "compute source")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            if not source_ref:
                return json.dumps({"status": "error", "message": "source_ref is required"}, indent=2)
            return json.dumps(_exec(_REMOVE_ADDL_BODY,
                {"asset_path": asset_path, "source_ref": source_ref}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 7. set_pcg_custom_hlsl_kernel_type — on a placed CustomHLSL node     #
    # ------------------------------------------------------------------ #
    _SET_KERNEL_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
kt = PARAMS.get("kernel_type")
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node = _pcg_resolve_node(g, node_id)
    if node is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "node not found: %s" % node_id}))
    else:
        s = node.get_settings()
        cur = None; had_prior = True
        if s is None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "node has no settings object"}))
        else:
            try:
                cur = s.get_editor_property("kernel_type")
            except Exception as _e:
                had_prior = False
                print("@@UMCP@@" + json.dumps({"status": "error",
                    "message": "node settings has no 'kernel_type' (not a Custom-HLSL node): %s" % (s.get_class().get_name())}))
                s = None
            if s is not None:
                prior_settable, _ok = _pcg_settable(cur)
                # enum options for the response
                opts = []
                try:
                    et = type(cur)
                    for k in dir(et):
                        if k.startswith("__"):
                            continue
                        try:
                            v = getattr(et, k)
                            if isinstance(v, et):
                                opts.append(k)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    newv = _pcg_coerce(cur, kt)
                    s.set_editor_property("kernel_type", newv)
                except Exception as _e:
                    print("@@UMCP@@" + json.dumps({"status": "error",
                        "message": "set kernel_type failed for value '%s': %s" % (kt, _e),
                        "valid_values": sorted(set(opts))}))
                    s = None
                if s is not None:
                    readback, _ok2 = _pcg_settable(s.get_editor_property("kernel_type"))
                    _ledger().append({"op": "pcg_set_hlsl_kernel_type", "graph_path": gp,
                                      "node_name": node.get_name(), "prior": prior_settable,
                                      "had_prior": had_prior})
                    _pcg_save(g)
                    print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                        "node_name": node.get_name(), "prior": prior_settable, "new_value": readback,
                        "valid_values": sorted(set(opts)), "had_prior": had_prior,
                        "undo_op": "pcg_set_hlsl_kernel_type", "ledger_depth": len(_ledger())}, default=str))
'''

    @mcp.tool()
    def set_pcg_custom_hlsl_kernel_type(ctx, graph_path: str, node_name: str, kernel_type: str) -> str:
        """Set the `kernel_type` (PCGKernelType enum) on a placed Custom-HLSL node inside a PCGGraph
        (set_editor_property on node.get_settings()). Captures the prior enum. Ledgered write (op
        'pcg_set_hlsl_kernel_type' -> `undo` restores the prior enum, LOSSLESS). Scratch-only graph.

        graph_path:  content path to the host PCGGraph (MUST be under /Game/MCP_Scratch).
        node_name:   a Custom-HLSL node's addressing id (add one first with
                     add_pcg_node graph_path 'PCGCustomHLSLSettings').
        kernel_type: one of POINT_PROCESSOR, POINT_GENERATOR, ATTRIBUTE_SET_GENERATOR,
                     ATTRIBUTE_SET_PROCESSOR, TEXTURE_GENERATOR, TEXTURE_PROCESSOR,
                     TEXTURE_ARRAY_GENERATOR, TEXTURE_ARRAY_PROCESSOR, CUSTOM (passed by name).
                     The response also returns the live 'valid_values' list."""
        try:
            gerr = _scratch_guard(graph_path, "graph")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_KERNEL_BODY,
                {"graph_path": graph_path, "node_name": node_name, "kernel_type": kernel_type}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. get_pcg_subgraph_override — read override state (READ, no ledger) #
    # ------------------------------------------------------------------ #
    _GET_OVERRIDE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
prop = PARAMS.get("property")
param_type = (PARAMS.get("param_type") or "").lower()
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node, inst, ierr = _subgraph_instance(g, node_id)
    if inst is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": ierr}))
    else:
        res = {"status": "success", "graph_path": gp, "node_name": node.get_name(),
               "subgraph_instance_class": inst.get_class().get_name()}
        # the assigned subgraph
        try:
            _sg = inst.get_editor_property("graph")
            res["subgraph"] = _norm_path(_sg.get_path_name()) if _sg else None
        except Exception:
            res["subgraph"] = None
        if prop:
            state = None
            try:
                state = str(inst.is_editor_property_overridden(prop))
            except Exception as _e:
                state = "unqueryable: %s" % str(_e)[:80]
            res["property"] = prop
            res["override_state"] = state
            # best-effort typed value read (subgraph user-parameter)
            if param_type in _PARAM_TYPEMAP:
                getn, setn, conv = _PARAM_TYPEMAP[param_type]
                H = getattr(unreal, "PCGGraphParametersHelpers", None)
                getter = getattr(H, getn, None) if H is not None else None
                if getter is not None:
                    try:
                        res["param_value"] = getter(inst, prop)
                    except Exception as _e:
                        res["param_value"] = "err: %s" % str(_e)[:80]
                res["param_type"] = param_type
            # best-effort plain reflected value
            try:
                res["reflected_value"], _ok = _pcg_settable(inst.get_editor_property(prop))
            except Exception:
                res["reflected_value"] = None
        else:
            res["note"] = "pass a 'property' to read its override state; PCGGraph::UpdatePropertyOverride is UE_API-not-UFUNCTION (unreachable), state is read from the node's subgraph_instance."
        print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def get_pcg_subgraph_override(ctx, graph_path: str, node_name: str,
                                  property: str = None, param_type: str = None) -> str:
        """Read a PCG subgraph node's property-override state, best-effort via reflection. Read-only,
        no ledger.

        The canonical PCGGraph::UpdatePropertyOverride API (PCGGraph.h) is UE_API-not-UFUNCTION and is
        NOT reachable from Python; this reads the per-node override surface instead — the subgraph
        node's `subgraph_instance` (a UPCGGraphInstance) — via is_editor_property_overridden and (for
        an exposed subgraph user-parameter) PCGGraphParametersHelpers typed getters.

        graph_path: content path to the host PCGGraph.
        node_name:  a subgraph node's addressing id (must already have a subgraph assigned).
        property:   the instance/parameter name to inspect (omit for a summary).
        param_type: optional float/double/int/int64/bool/string/name to also read the typed
                    parameter value."""
        try:
            return json.dumps(_exec(_GET_OVERRIDE_BODY,
                {"graph_path": graph_path, "node_name": node_name,
                 "property": property, "param_type": param_type}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. set_pcg_subgraph_override — set an override (LEDGERED, PARTIAL)   #
    # ------------------------------------------------------------------ #
    _SET_OVERRIDE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
prop = PARAMS.get("property")
value = PARAMS.get("value")
param_type = (PARAMS.get("param_type") or "").lower()
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node, inst, ierr = _subgraph_instance(g, node_id)
    if inst is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": ierr}))
    elif not prop:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "property is required"}))
    else:
        # capture prior override state + prior value
        prior_overridden = None
        try:
            prior_overridden = str(inst.is_editor_property_overridden(prop))
        except Exception:
            prior_overridden = None
        via = None; prior_value = None; had_prior = False; applied = None; readback = None
        H = getattr(unreal, "PCGGraphParametersHelpers", None)
        if param_type in _PARAM_TYPEMAP:
            # subgraph user-parameter override leg (the true per-node override)
            getn, setn, conv = _PARAM_TYPEMAP[param_type]
            getter = getattr(H, getn, None) if H is not None else None
            setter = getattr(H, setn, None) if H is not None else None
            if setter is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGGraphParametersHelpers.%s absent" % setn}))
            else:
                via = "param:" + param_type
                if getter is not None:
                    try:
                        prior_value = getter(inst, prop); had_prior = True
                    except Exception:
                        had_prior = False
                try:
                    setter(inst, prop, conv(value)); applied = True
                except Exception as _e:
                    applied = "err: " + str(_e)[:100]
                if getter is not None:
                    try:
                        readback = getter(inst, prop)
                    except Exception:
                        pass
        else:
            # plain reflected-property override leg
            via = "prop"
            try:
                cur = inst.get_editor_property(prop)
                prior_value, _ok = _pcg_settable(cur); had_prior = True
            except Exception:
                cur = None; had_prior = False
            try:
                inst.set_editor_property(prop, _pcg_coerce(cur, value)); applied = True
            except Exception as _e:
                applied = "err: " + str(_e)[:100]
            try:
                readback, _ok2 = _pcg_settable(inst.get_editor_property(prop))
            except Exception:
                pass
        if applied is True:
            new_overridden = None
            try:
                new_overridden = str(inst.is_editor_property_overridden(prop))
            except Exception:
                pass
            _ledger().append({"op": "pcg_set_subgraph_override", "graph_path": gp,
                              "node_name": node.get_name(), "property": prop,
                              "param_type": param_type, "via": via,
                              "prior_value": prior_value, "prior_overridden": prior_overridden,
                              "had_prior": had_prior})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "node_name": node.get_name(), "property": prop, "via": via,
                "prior_value": prior_value, "prior_overridden": prior_overridden,
                "new_value": readback, "new_overridden": new_overridden, "had_prior": had_prior,
                "partial_note": "canonical PCGGraph::UpdatePropertyOverride is UE_API-not-UFUNCTION (unreachable); applied via the node subgraph_instance.",
                "undo_op": "pcg_set_subgraph_override", "ledger_depth": len(_ledger())}, default=str))
        else:
            print("@@UMCP@@" + json.dumps({"status": "error", "graph_path": gp,
                "node_name": node.get_name(), "property": prop, "via": via,
                "message": "override apply failed", "detail": applied}, default=str))
'''

    @mcp.tool()
    def set_pcg_subgraph_override(ctx, graph_path: str, node_name: str, property: str, value,
                                 param_type: str = None) -> str:
        """Set an override value on a PCG subgraph node, best-effort. Ledgered write (op
        'pcg_set_subgraph_override' -> `undo` restores the prior override value, best-effort LOSSY).
        Scratch-only graph. PARTIAL — see note.

        PARTIAL: the canonical PCGGraph::UpdatePropertyOverride API (PCGGraph.h) is UE_API-not-UFUNCTION
        and is NOT reachable from Python. This applies the override to the subgraph node's
        `subgraph_instance` (a UPCGGraphInstance): with `param_type` it sets an exposed subgraph
        user-parameter via PCGGraphParametersHelpers (the true per-node parameter override); without
        it, it set_editor_property's a reflected instance property (e.g. an override_* / metadata
        field). The named-parameter leg could only be validated structurally because no PCGGraph in
        this project exposes user_parameters and defining them from Python is not reachable (Wave-5 C++).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        node_name:  a subgraph node with an assigned subgraph.
        property:   the parameter/instance property name.
        value:      the new value.
        param_type: float/double/int/int64/bool/string/name to use the typed subgraph-parameter path;
                    omit to set a reflected instance property directly."""
        try:
            if param_type and str(param_type).strip().lower() in ("float", "double", "int", "int32", "int64", "bool", "string", "name"):
                return json.dumps({"status": "error", "blocked": True,
                    "message": "param-leg (named subgraph user-parameter) override is DISABLED: applying it via "
                    "PCGGraphParametersHelpers on a subgraph instance destabilizes the editor -- it plants a deferred "
                    "python311 stack-overflow that crashes on the NEXT operation (reproduced 2026-08-19). Omit "
                    "param_type to override a reflected instance property (the prop-leg, verified safe)."}, indent=2)
            gerr = _scratch_guard(graph_path, "graph")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_SET_OVERRIDE_BODY,
                {"graph_path": graph_path, "node_name": node_name, "property": property,
                 "value": value, "param_type": param_type}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 10. reset_pcg_subgraph_override — reset to default (LEDGERED, PARTIAL)#
    # ------------------------------------------------------------------ #
    _RESET_OVERRIDE_BODY = _PCG_HELPERS + r'''
gp = PARAMS.get("graph_path")
node_id = PARAMS.get("node_name")
prop = PARAMS.get("property")
param_type = (PARAMS.get("param_type") or "").lower()
g, err = _pcg_load_graph(gp)
if g is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    node, inst, ierr = _subgraph_instance(g, node_id)
    if inst is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": ierr}))
    elif not prop:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "property is required"}))
    else:
        prior_overridden = None
        try:
            prior_overridden = str(inst.is_editor_property_overridden(prop))
        except Exception:
            prior_overridden = None
        # capture prior value for a best-effort re-apply on undo
        prior_value = None; had_prior = False; via = "prop"
        H = getattr(unreal, "PCGGraphParametersHelpers", None)
        if param_type in _PARAM_TYPEMAP:
            via = "param:" + param_type
            getn = _PARAM_TYPEMAP[param_type][0]
            getter = getattr(H, getn, None) if H is not None else None
            if getter is not None:
                try:
                    prior_value = getter(inst, prop); had_prior = True
                except Exception:
                    had_prior = False
        else:
            try:
                prior_value, _ok = _pcg_settable(inst.get_editor_property(prop)); had_prior = True
            except Exception:
                had_prior = False
        # reset via the reachable generic reflection primitive
        ok = None
        try:
            inst.reset_editor_property(prop); ok = True
        except Exception as _e:
            ok = "err: " + str(_e)[:100]
        if ok is True:
            new_overridden = None
            try:
                new_overridden = str(inst.is_editor_property_overridden(prop))
            except Exception:
                pass
            _ledger().append({"op": "pcg_reset_subgraph_override", "graph_path": gp,
                              "node_name": node.get_name(), "property": prop,
                              "param_type": param_type, "via": via,
                              "prior_value": prior_value, "prior_overridden": prior_overridden,
                              "had_prior": had_prior})
            _pcg_save(g)
            print("@@UMCP@@" + json.dumps({"status": "success", "graph_path": gp,
                "node_name": node.get_name(), "property": prop, "via": via,
                "prior_value": prior_value, "prior_overridden": prior_overridden,
                "new_overridden": new_overridden, "had_prior": had_prior,
                "partial_note": "reset applied via subgraph_instance.reset_editor_property (reachable); canonical PCGGraph::ResetPropertyToDefault is UE_API-not-UFUNCTION.",
                "undo_op": "pcg_reset_subgraph_override", "ledger_depth": len(_ledger())}, default=str))
        else:
            print("@@UMCP@@" + json.dumps({"status": "error", "graph_path": gp,
                "node_name": node.get_name(), "property": prop,
                "message": "reset_editor_property failed", "detail": ok}, default=str))
'''

    @mcp.tool()
    def reset_pcg_subgraph_override(ctx, graph_path: str, node_name: str, property: str,
                                    param_type: str = None) -> str:
        """Reset a PCG subgraph node's property override back to its subgraph default. Ledgered write
        (op 'pcg_reset_subgraph_override' -> `undo` re-applies the captured prior override value,
        best-effort LOSSY). Scratch-only graph. PARTIAL — see note.

        PARTIAL: the canonical PCGGraph::ResetPropertyToDefault API is UE_API-not-UFUNCTION and is NOT
        reachable from Python. This resets the subgraph node's `subgraph_instance` (UPCGGraphInstance)
        via the reachable generic reflection primitive reset_editor_property(property). The prior value
        is captured (typed via PCGGraphParametersHelpers when `param_type` is given) so undo can
        re-apply it, but re-application is best-effort (LOSSY).

        graph_path: host PCGGraph (MUST be under /Game/MCP_Scratch).
        node_name:  a subgraph node with an assigned subgraph.
        property:   the parameter/instance property name to reset.
        param_type: float/double/int/int64/bool/string/name — captures the typed prior value for undo."""
        try:
            if param_type and str(param_type).strip().lower() in ("float", "double", "int", "int32", "int64", "bool", "string", "name"):
                return json.dumps({"status": "error", "blocked": True,
                    "message": "param-leg (named subgraph user-parameter) reset is DISABLED: it destabilizes the "
                    "editor (deferred python311 crash on the next operation, reproduced 2026-08-19). Omit param_type "
                    "to reset a reflected instance property (the prop-leg, verified safe)."}, indent=2)
            gerr = _scratch_guard(graph_path, "graph")
            if gerr:
                return json.dumps({"status": "error", "message": gerr}, indent=2)
            return json.dumps(_exec(_RESET_OVERRIDE_BODY,
                {"graph_path": graph_path, "node_name": node_name, "property": property,
                 "param_type": param_type}), indent=2)
        except Exception as e:
            return f"Error: {e}"
