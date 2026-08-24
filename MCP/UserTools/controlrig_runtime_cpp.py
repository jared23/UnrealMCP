"""UserTools :: Control Rig RUNTIME (eval-based) READS  (spec: docs/spec/controlrig_runtime.md)

DRAFT wiring for the ControlRig-runtime C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_ControlRigRuntime.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention,
base64 PARAMS injection, Output-Log auto-capture) is copied VERBATIM from niagara_runtime_cpp.py.

Fills the spec features that stock Python cannot reach: Python can author + read a Control Rig blueprint's
authoring hierarchy and VM graph, but it CANNOT instantiate the COMPILED rig, run its Forwards Solve, and
read the SOLVED pose. That needs the ControlRig RUNTIME class UControlRig (C++ only). Each handler
instantiates a TRANSIENT UControlRig from the blueprint's generated class, solves on that throwaway
instance, and reads the result -- the SOURCE ASSET IS NEVER TOUCHED, so these are all READS (no ledger,
no undo), including the two that write to the transient instance (control offset / bone drive).

  READS (no ledger -- the source asset is never mutated):
    * get_rig_pose                -- instantiate + Forwards Solve; per bone/control global+local transform.
                                    threshold_cm flags elements whose solved global moved > threshold from
                                    the ref (initial) pose. bones_csv = optional substring name filter.
    * analyze_rig_io              -- enumerate INPUTS (controls + public input variables) and OUTPUTS (bones
                                    the solve writes, by diffing globals before/after execute over N frames).
    * analyze_rig_control_impact  -- for each control: offset it by offset_cm on a FRESH instance, re-solve,
                                    report which bones moved + by how much (downstream impact map).
    * profile_rig                 -- execute the solve `frames` times, time it (FPlatformTime); total/avg ms
                                    + est fps. (per-node timing not exposed via public API -> omitted w/ note.)
    * simulate_rig                -- drive the rig's bones from an AnimSequence's per-bone pose over `frames`,
                                    solve each frame (Backwards Solve first if supported), sampled outputs.
    * start_rig_motion_capture    -- solve N frames, stash every bone's global-per-frame into a static buffer
                                    keyed by control_rig_path (data-only -> GC-safe).
    * get_rig_motion_report       -- read the stashed capture; per-bone min/max/mean displacement.

Undo: this module registers NO own `undo` tool and appends NOTHING to the shared ledger -- every operation
is a read or a transient-instance write that never persists to the asset.

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

# --- Output Log auto-capture (copied verbatim from niagara_runtime_cpp.py) -----
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
    _HELP = r'''
import unreal, json, builtins, warnings, gc
warnings.simplefilter("ignore")
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ ControlRig-runtime handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_ControlRigRuntime.cpp -- and ControlRig added to Build.cs -- to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _POSE_BODY = _HELP + r'''
rl = _mrl("get_rig_pose_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_rig_pose")))
else:
    res = _decode(rl.get_rig_pose_json(PARAMS["control_rig_path"], PARAMS.get("bones_csv", ""),
        float(PARAMS.get("threshold_cm", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_rig_pose(ctx, control_rig_path: str, bones_csv: str = "", threshold_cm: float = 0.0) -> str:
        """Instantiate a Control Rig, run its Forwards Solve, and read the solved pose.

        control_rig_path: Control Rig blueprint asset path (e.g. '/Game/Chars/Rigs/CR_Manny.CR_Manny').
        bones_csv:        optional comma-separated case-insensitive substring filter over element names
                          (empty -> all transform-bearing elements: bones/controls/nulls/sockets).
        threshold_cm:     flags each element whose solved GLOBAL location moved > threshold_cm from its ref
                          (initial) pose.

        Returns {control_rig, threshold_cm, element_count, moved_count, elements:[{name, type,
        global:{loc,rot_quat,rot_euler,scale}, local:{...}, delta_cm, moved}]}. A TRANSIENT rig is solved;
        the asset is never touched. ControlRig-runtime C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "bones_csv": bones_csv, "threshold_cm": threshold_cm}
        try:
            return json.dumps(_exec(_POSE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _IO_BODY = _HELP + r'''
rl = _mrl("analyze_rig_io_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("analyze_rig_io")))
else:
    res = _decode(rl.analyze_rig_io_json(PARAMS["control_rig_path"], int(PARAMS.get("frames", 1)),
        float(PARAMS.get("delta_time", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def analyze_rig_io(ctx, control_rig_path: str, frames: int = 1, delta_time: float = 0.0) -> str:
        """Enumerate a Control Rig's INPUTS (controls + public variables) and OUTPUTS (bones the solve writes).

        control_rig_path: Control Rig blueprint asset path.
        frames:           number of solve frames to run (1..2000); >1 advances time so a time-dependent rig's
                          outputs are captured (max per-bone delta reported). Default 1.
        delta_time:       seconds per frame (default 0 -> 1/30). Only relevant when frames>1.

        Outputs are found by diffing bone global transforms before/after the solve. Returns {control_rig,
        frames, bone_count, inputs:{control_count, controls:[{name, control_type}], variable_count,
        variables:[{name, cpp_type, is_array, is_read_only}]}, output_bone_count,
        output_bones:[{name, max_delta_cm}]}. ControlRig-runtime C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "frames": frames, "delta_time": delta_time}
        try:
            return json.dumps(_exec(_IO_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _IMPACT_BODY = _HELP + r'''
rl = _mrl("analyze_rig_control_impact_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("analyze_rig_control_impact")))
else:
    res = _decode(rl.analyze_rig_control_impact_json(PARAMS["control_rig_path"],
        PARAMS.get("controls_csv", ""), float(PARAMS.get("offset_cm", 10.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def analyze_rig_control_impact(ctx, control_rig_path: str, controls_csv: str = "",
                                   offset_cm: float = 10.0) -> str:
        """Map each control's downstream bone impact: offset the control, re-solve, report which bones moved.

        control_rig_path: Control Rig blueprint asset path.
        controls_csv:     optional comma-separated case-insensitive substring filter over control names
                          (empty -> every control).
        offset_cm:        cm to offset each spatial control's LOCAL translation (+X) before re-solving
                          (default 10; 0 -> 10). Non-spatial controls (bool/float/etc.) are reported skipped.

        Each control is probed on a FRESH transient instance (zero cross-contamination); the source asset is
        never modified (no undo needed). Returns {control_rig, offset_cm, control_count, controls:[{control,
        spatial, offset_cm, affected_bone_count, max_delta_cm, total_delta_cm,
        affected_bones:[{bone, delta_cm}] (sorted desc)}]}. ControlRig-runtime C++; needs the handler."""
        params = {"control_rig_path": control_rig_path, "controls_csv": controls_csv, "offset_cm": offset_cm}
        try:
            return json.dumps(_exec(_IMPACT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PROFILE_BODY = _HELP + r'''
rl = _mrl("profile_rig_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("profile_rig")))
else:
    res = _decode(rl.profile_rig_json(PARAMS["control_rig_path"], int(PARAMS.get("frames", 100)),
        float(PARAMS.get("delta_time", 0.0)), int(PARAMS.get("top_nodes", 0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def profile_rig(ctx, control_rig_path: str, frames: int = 100, delta_time: float = 0.0,
                    top_nodes: int = 0) -> str:
        """Time a Control Rig's Forwards Solve over N frames (total / avg ms / est fps).

        control_rig_path: Control Rig blueprint asset path.
        frames:           number of solve frames to time (1..100000; default 100). Init/first-solve cost is
                          excluded (warm-up frame is not counted).
        delta_time:       seconds per frame (default 0 -> 1/30).
        top_nodes:        if >0, requests per-node timing -- NOT exposed via public API, so it is omitted with
                          a note rather than fabricated.

        Returns {control_rig, frames_requested, frames_run, total_ms, avg_ms_per_frame, est_fps
        [, top_nodes_note]}. ControlRig-runtime C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "frames": frames, "delta_time": delta_time,
                  "top_nodes": top_nodes}
        try:
            return json.dumps(_exec(_PROFILE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _SIMULATE_BODY = _HELP + r'''
rl = _mrl("simulate_rig_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("simulate_rig")))
else:
    res = _decode(rl.simulate_rig_json(PARAMS["control_rig_path"], PARAMS["anim_sequence_path"],
        int(PARAMS.get("frames", 30)), float(PARAMS.get("delta_time", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def simulate_rig(ctx, control_rig_path: str, anim_sequence_path: str, frames: int = 30,
                     delta_time: float = 0.0) -> str:
        """Drive a Control Rig's bones from an AnimSequence over N frames and sample the solved output.

        control_rig_path:   Control Rig blueprint asset path.
        anim_sequence_path: UAnimSequence asset path whose per-bone LOCAL pose drives the matching rig bones.
        frames:             simulated frames (1..2000; default 30), resampled across the anim's length.
        delta_time:         seconds per frame (default 0 -> 1/30).

        For a rig that supports Backwards Solve, each frame runs Backwards then Forwards (the bake round-trip);
        otherwise Forwards only (bone drive has limited effect on a control-driven rig -- reported via a note).
        Editor-only (anim sampling). Returns {control_rig, anim_sequence, frames, source_frames,
        driven_bone_count, used_backwards_solve[, note], bone_count, bones:[{bone, driven, start, end, mean,
        travel_cm}]}. ControlRig-runtime C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "anim_sequence_path": anim_sequence_path,
                  "frames": frames, "delta_time": delta_time}
        try:
            return json.dumps(_exec(_SIMULATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _MOCAP_START_BODY = _HELP + r'''
rl = _mrl("start_rig_motion_capture_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("start_rig_motion_capture")))
else:
    res = _decode(rl.start_rig_motion_capture_json(PARAMS["control_rig_path"],
        int(PARAMS.get("frames", 60)), float(PARAMS.get("delta_time", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def start_rig_motion_capture(ctx, control_rig_path: str, frames: int = 60,
                                 delta_time: float = 0.0) -> str:
        """Record a Control Rig's per-frame bone motion into a server-side buffer (read via get_rig_motion_report).

        control_rig_path: Control Rig blueprint asset path (also the buffer key).
        frames:           frames to solve + record (1..5000; default 60), advancing time each frame.
        delta_time:       seconds per frame (default 0 -> 1/30).

        Solves the rig `frames` times and stashes every bone's global location per frame in a static
        (GC-safe, data-only) buffer keyed by control_rig_path, overwriting any prior capture for that path.
        Returns {control_rig, frames_captured, bone_count, note}. ControlRig-runtime C++; needs the handler."""
        params = {"control_rig_path": control_rig_path, "frames": frames, "delta_time": delta_time}
        try:
            return json.dumps(_exec(_MOCAP_START_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _MOCAP_REPORT_BODY = _HELP + r'''
rl = _mrl("get_rig_motion_report_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_rig_motion_report")))
else:
    res = _decode(rl.get_rig_motion_report_json(PARAMS["control_rig_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_rig_motion_report(ctx, control_rig_path: str) -> str:
        """Report per-bone min/max/mean displacement from a stashed motion capture.

        control_rig_path: the same path passed to start_rig_motion_capture (the buffer key).

        Displacement = distance of each bone from its frame-0 position. Returns {control_rig, frames,
        bone_count, bones:[{bone, min_disp_cm, max_disp_cm, mean_disp_cm}]}. Errors if no capture is stashed
        for that path. ControlRig-runtime C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path}
        try:
            return json.dumps(_exec(_MOCAP_REPORT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
