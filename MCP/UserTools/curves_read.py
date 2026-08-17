"""UserTools :: Curves (READ)  (spec: docs/spec/curves.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset editors are ever opened (no modals); nothing is mutated; no ledger. This covers
curve-ASSET introspection: CurveFloat, CurveVector, CurveLinearColor. Query convention +
base64 PARAMS + Output-Log auto-capture are copied verbatim from editor_level.py (gold standard).

What IS reachable from stock Python in this build (verified live vs UE 5.8.1):
  * AssetRegistry enumerates CurveFloat / CurveVector / CurveLinearColor via
    class_paths=[TopLevelAssetPath("/Script/Engine", <Kind>)] (5.8 removed ARFilter.class_names;
    asset path = package_name + "." + asset_name).
  * Every curve asset exposes get_time_range() -> (min_time, max_time) and get_value_range() ->
    (min_value, max_value) tuples, and a native EVALUATOR:
       CurveFloat.get_float_value(time) -> float
       CurveVector.get_vector_value(time) -> Vector (x,y,z)
       CurveLinearColor.get_linear_color_value(time) -> LinearColor (r,g,b,a)
         (also get_clamped_linear_color_value / get_unadjusted_linear_color_value)
    So the curve can be SAMPLED at any time, and its overall time/value extents are known.

Known limit (reported honestly in payloads, NOT hidden):
  * Individual KEY data (per-key time / value / interp_mode / tangent_mode / arrive_tangent /
    leave_tangent) is NOT reachable from stock Python in 5.8. The underlying FRichCurve lives in a
    PROTECTED UPROPERTY on the asset (UCurveFloat.FloatCurve, UCurveVector.FloatCurves,
    UCurveLinearColor.FloatCurves): get_editor_property("FloatCurve"/"FloatCurves") is refused
    ("... is protected and cannot be read"), exactly like Skeleton.Sockets / GroupActor membership
    handled elsewhere. unreal.RichCurve/RichCurveKey types EXIST but there is no route to the
    asset's live FRichCurve instance, and MCPReflectionLibrary exposes no curve helper. Precise key
    extraction would need a C++ handler (e.g. MCPReflectionLibrary.get_curve_keys_json over the
    FRichCurve) -- same C++-round-trip pattern that unblocked struct fields / mesh sockets.
    Until then, curve SHAPE is surfaced by uniform sampling of the native evaluator (key_count=null
    with a note), which is exact for interpolation but does not recover authored key/tangent data.

Implemented (all read-only):
  - list_curve_assets  (AssetRegistry enumerate the 3 curve kinds; counts + paths, filterable)
  - get_curve_info     (type, channels, time/value ranges, uniform-sampled per-channel shape)
  - evaluate_curve     (sample a curve at a given time via the native evaluator)
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
# so snippet bodies must contain NO triple-single-quote and NO stray backslashes. Data is base64.

# The 3 curve-asset kinds (all live in /Script/Engine in UE 5.8).
_CURVE_KINDS = ["CurveFloat", "CurveVector", "CurveLinearColor"]


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
    _CURVE_HELPERS = r'''
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
def _curve_kind(obj):
    if isinstance(obj, unreal.CurveFloat):
        return "CurveFloat"
    if isinstance(obj, unreal.CurveVector):
        return "CurveVector"
    if isinstance(obj, unreal.CurveLinearColor):
        return "CurveLinearColor"
    return None
def _channel_names(kind):
    if kind == "CurveFloat":
        return ["float_curve"]
    if kind == "CurveVector":
        return ["X", "Y", "Z"]
    if kind == "CurveLinearColor":
        return ["R", "G", "B", "A"]
    return []
def _eval_channels(obj, kind, t):
    # returns a list of per-channel float values at time t, aligned with _channel_names(kind)
    if kind == "CurveFloat":
        return [float(obj.get_float_value(t))]
    if kind == "CurveVector":
        v = obj.get_vector_value(t)
        return [float(v.x), float(v.y), float(v.z)]
    if kind == "CurveLinearColor":
        c = obj.get_linear_color_value(t)
        return [float(c.r), float(c.g), float(c.b), float(c.a)]
    return []
'''

    # ------------------------------------------------------------------ #
    # list_curve_assets — enumerate the 3 curve-asset kinds               #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _CURVE_HELPERS + r'''
kinds_all = ["CurveFloat", "CurveVector", "CurveLinearColor"]
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
    result = {"status": "success", "path": package_path,
              "kind": (kinds[0] if req_kind else None), "by_kind": {}, "total": 0}
    grand = 0
    for k in kinds:
        try:
            flt = unreal.ARFilter(
                class_paths=[unreal.TopLevelAssetPath("/Script/Engine", k)],
                recursive_paths=True, package_paths=[package_path])
            assets = ar.get_assets(flt) or []
        except Exception as e:
            result["by_kind"][k] = {"count": 0, "error": str(e), "assets": []}
            continue
        rows = []
        for a in assets:
            pkg = str(a.get_editor_property("package_name"))
            nm = str(a.get_editor_property("asset_name"))
            rows.append({"name": nm, "path": pkg + "." + nm})
        rows.sort(key=lambda r: r["path"].lower())
        grand += len(rows)
        result["by_kind"][k] = {"count": len(rows),
                                "returned": min(len(rows), max_results),
                                "assets": rows[:max_results]}
    result["total"] = grand
    result["note"] = ("Enumerated via AssetRegistry class_paths=TopLevelAssetPath('/Script/Engine', "
                      "<Kind>) (UE5.8 removed ARFilter.class_names). Each kind lists up to max_results "
                      "assets (name + object path); 'count' is the true total under 'path'. The project "
                      "may have no curve assets under /Game; pass path='/Engine' to find engine curves.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_curve_assets(ctx, path: str = None, kind: str = None,
                          max_results: int = 200) -> str:
        """Enumerate curve assets via the AssetRegistry (fast; no asset load). Read-only.

        path:        content root to scan (default '/Game'); e.g. '/Engine/EngineSky'.
        kind:        restrict to ONE of: CurveFloat, CurveVector, CurveLinearColor
                     (case-insensitive). Omit to enumerate all three.
        max_results: cap the assets listed PER KIND (default 200); each kind's true 'count'
                     is always reported even if the list is capped.

        Returns per-kind {count, returned, assets:[{name, path}]} plus a grand 'total'.
        Note: this project has no curve assets under /Game -- scan '/Engine' to find engine
        curves (e.g. /Engine/EngineSky/*_Color CurveLinearColor, /Engine/Tutorial/*Curve)."""
        params = {"path": path, "kind": kind, "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_curve_info — type, channels, ranges, uniform-sampled shape      #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _CURVE_HELPERS + r'''
path = PARAMS.get("path")
samples = PARAMS.get("samples")
samples = 11 if samples is None else int(samples)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    kind = _curve_kind(obj)
    if kind is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a curve (got %s): %s. Supported: CurveFloat, CurveVector, CurveLinearColor." % (obj.get_class().get_name(), path)}))
    else:
        tr = _try(lambda: obj.get_time_range())
        vr = _try(lambda: obj.get_value_range())
        chan_names = _channel_names(kind)
        info = {"status": "success", "path": obj.get_path_name(),
                "class": obj.get_class().get_name(), "kind": kind,
                "num_channels": len(chan_names), "channel_names": chan_names}
        info["time_range"] = [round(tr[0], 6), round(tr[1], 6)] if tr is not None else None
        info["value_range"] = [round(vr[0], 6), round(vr[1], 6)] if vr is not None else None
        info["value_range_note"] = ("value_range is the asset's overall min/max value (across ALL "
                                     "channels for multi-channel curves); per-channel min/max are in "
                                     "each channel's sampled_min/sampled_max below when sampling is on.")
        # Default (fallback) key state -- overridden below if the native C++ helper is present.
        info["key_count"] = None
        info["keys_source"] = "sampling_only"
        info["keys_note"] = ("Per-key data (time/value/interp_mode/tangent_mode/arrive_tangent/"
                             "leave_tangent) is NOT reachable from stock Python in UE5.8: the FRichCurve "
                             "lives in a PROTECTED UPROPERTY (UCurveFloat.FloatCurve / UCurveVector."
                             "FloatCurves / UCurveLinearColor.FloatCurves) -- get_editor_property is "
                             "refused. Curve SHAPE is surfaced by uniform evaluator sampling (exact "
                             "interpolation) instead. When the native MCPReflectionLibrary.get_curve_keys"
                             "_json C++ handler is present, REAL authored keys are populated per channel "
                             "and keys_source becomes 'MCPReflectionLibrary'.")
        # uniform-sampled per-channel shape via the native evaluator
        channels = []
        for i, cn in enumerate(chan_names):
            channels.append({"name": cn, "index": i, "samples": [],
                             "sampled_min": None, "sampled_max": None,
                             "key_count": None, "keys": []})
        if samples > 0 and tr is not None:
            t0, t1 = float(tr[0]), float(tr[1])
            if samples == 1 or t1 <= t0:
                times = [t0]
            else:
                step = (t1 - t0) / (samples - 1)
                times = [t0 + step * n for n in range(samples)]
            for t in times:
                vals = _try(lambda: _eval_channels(obj, kind, t))
                if vals is None:
                    continue
                for i, cn in enumerate(chan_names):
                    v = float(vals[i])
                    channels[i]["samples"].append({"time": round(t, 6), "value": round(v, 6)})
                    cmn = channels[i]["sampled_min"]; cmx = channels[i]["sampled_max"]
                    channels[i]["sampled_min"] = v if cmn is None else min(cmn, v)
                    channels[i]["sampled_max"] = v if cmx is None else max(cmx, v)
            info["sample_count"] = samples
            info["samples_source"] = "uniform sampling of native evaluator across time_range"
        else:
            info["sample_count"] = 0
            info["samples_source"] = "sampling disabled (samples=0) or no time_range"
        # ---- REAL authored keys via native C++ handler (if compiled in) --------
        # MCPReflectionLibrary.get_curve_keys_json reaches the PROTECTED FRichCurve(s) that stock
        # Python reflection cannot. Shape: {"curve","class","channels":[{"name","key_count",
        # "keys":[{"time","value","interp_mode","tangent_mode","arrive_tangent","leave_tangent"}]}]}.
        # Aligned into the existing channels list BY INDEX (CurveFloat->1, Vector->3, LinearColor->4),
        # so C++ channel names (which may be GUID-suffixed) do not need to match ours.
        if hasattr(unreal.MCPReflectionLibrary, "get_curve_keys_json"):
            kjs = _try(lambda: unreal.MCPReflectionLibrary.get_curve_keys_json(obj))
            kd = _try(lambda: json.loads(kjs)) if kjs else None
            if isinstance(kd, dict) and isinstance(kd.get("channels"), list):
                cpp_channels = kd["channels"]
                total_keys = 0
                for i, ch in enumerate(channels):
                    if i < len(cpp_channels):
                        cc = cpp_channels[i] or {}
                        keys = cc.get("keys") or []
                        kc = cc.get("key_count")
                        kc = len(keys) if kc is None else int(kc)
                        ch["keys"] = keys
                        ch["key_count"] = kc
                        ch["cpp_channel_name"] = cc.get("name")
                        total_keys += kc
                info["key_count"] = total_keys
                info["keys_source"] = "MCPReflectionLibrary"
                info["keys_note"] = ("REAL authored keys per channel via native MCPReflectionLibrary."
                                     "get_curve_keys_json (reaches the protected FRichCurve). Each key: "
                                     "time, value, interp_mode (Linear/Constant/Cubic/None), tangent_mode "
                                     "(Auto/User/Break/None), arrive_tangent, leave_tangent. key_count is "
                                     "the sum across channels. Uniform 'samples' are kept for shape too.")
        info["channels"] = channels
        print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_curve_info(ctx, path: str, samples: int = 11) -> str:
        """Introspect a curve asset (CurveFloat / CurveVector / CurveLinearColor). Loads it;
        no editor opened. Read-only.

        path:    curve asset path, e.g. '/Engine/EngineSky/C_Sky_Zenith_Color.C_Sky_Zenith_Color'
                 or '/Engine/Tutorial/ContentIntroCurve.ContentIntroCurve'.
        samples: number of uniform time samples to evaluate per channel across the time range
                 (default 11; set 0 for metadata only). This surfaces the curve SHAPE.

        Returns: kind, num_channels + channel_names (CurveFloat->['float_curve']; CurveVector->
        ['X','Y','Z']; CurveLinearColor->['R','G','B','A']); time_range [min,max]; value_range
        [min,max] (overall, across all channels); and per-channel 'channels' each with a sampled
        [{time,value}] list plus sampled_min/sampled_max, and (when the native
        MCPReflectionLibrary.get_curve_keys_json C++ handler is present) REAL authored 'keys'
        [{time,value,interp_mode,tangent_mode,arrive_tangent,leave_tangent}] + per-channel key_count,
        with keys_source='MCPReflectionLibrary'. If that helper is absent, key_count is null and
        keys_source='sampling_only' (the FRichCurve is a protected UPROPERTY unreachable from stock
        Python -- shape then comes from evaluator sampling only).
        Errors if the asset is missing or not one of the three curve types."""
        params = {"path": path, "samples": samples}
        try:
            return json.dumps(_exec(_INFO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # evaluate_curve — sample a curve at a given time (native evaluator)  #
    # ------------------------------------------------------------------ #
    _EVAL_BODY = _CURVE_HELPERS + r'''
path = PARAMS.get("path")
time = PARAMS.get("time")
time = 0.0 if time is None else float(time)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    kind = _curve_kind(obj)
    if kind is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset is not a curve (got %s): %s. Supported: CurveFloat, CurveVector, CurveLinearColor." % (obj.get_class().get_name(), path)}))
    else:
        chan_names = _channel_names(kind)
        vals = _try(lambda: _eval_channels(obj, kind, time))
        result = {"status": "success", "path": obj.get_path_name(), "kind": kind,
                  "time": round(time, 6), "channel_names": chan_names}
        if vals is None:
            result["status"] = "error"
            result["message"] = "evaluator failed at time %s" % time
        else:
            result["values"] = [round(float(v), 6) for v in vals]
            result["value"] = (result["values"][0] if kind == "CurveFloat" else None)
            per = {}
            for i, cn in enumerate(chan_names):
                per[cn] = round(float(vals[i]), 6)
            result["by_channel"] = per
            result["evaluator"] = {"CurveFloat": "get_float_value",
                                   "CurveVector": "get_vector_value",
                                   "CurveLinearColor": "get_linear_color_value"}[kind]
        print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def evaluate_curve(ctx, path: str, time: float = 0.0) -> str:
        """Sample a curve asset at a given time using its native evaluator. Read-only.

        path: curve asset path (CurveFloat / CurveVector / CurveLinearColor).
        time: the time to evaluate at (default 0.0). Times outside the curve's range clamp
              to the endpoint values (standard Unreal evaluator behavior).

        Returns 'values' (a list: 1 float for CurveFloat, [x,y,z] for CurveVector, [r,g,b,a]
        for CurveLinearColor), 'by_channel' (named), 'value' (the scalar for CurveFloat, else
        null), and which native 'evaluator' method was used. Errors if the asset is missing or
        not a curve. This is the one part of curve data that IS fully reachable from Python."""
        params = {"path": path, "time": time}
        try:
            return json.dumps(_exec(_EVAL_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
