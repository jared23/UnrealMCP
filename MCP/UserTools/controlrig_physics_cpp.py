"""UserTools :: Control Rig PHYSICS / VALIDATION  (spec: docs/spec/controlrig.md — Physics/validation row)

DRAFT wiring for the ControlRig PHYSICS/validation C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_ControlRigPhysics.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query convention,
base64 PARAMS injection, Output-Log auto-capture) is copied VERBATIM from controlrig_runtime_cpp.py.

These analyze the SOLVED geometry (and, where the rig has physics, the SIMULATED motion) of a COMPILED
Control Rig -- reachable only via the runtime class UControlRig in C++. Each handler instantiates a TRANSIENT
UControlRig from the blueprint's generated class, solves on that throwaway instance, and reads the result --
the SOURCE ASSET IS NEVER TOUCHED, so these are all READS (no ledger, no undo), including the probe which
writes a control offset to the transient instance only.

Physics note: in UE 5.8 the ControlRig core no longer simulates; physics lives in the experimental
ControlRigPhysics plugin and is STEPPED FROM INSIDE the rig's own VM graph (the "Step Physics Solver" node,
during Forwards Solve). So the same Execute()-loop harness steps the physics for free -- these tools do NOT
link ControlRigPhysics. When a rig has no physics nodes the physics tools still run and report
contains_simulation=false plus a census of any physics setup, so they degrade gracefully.

  READS (no ledger -- the source asset is never mutated):
    * validate_rig_physics           -- census the physics setup + run the solve for an observation window;
                                        report solver stability (NaN/explosion), per-bone drift, residual
                                        velocity, contains_simulation.
    * validate_rig_deformation       -- rebuild each element's concatenated GLOBAL matrix and flag non-uniform
                                        scale / shear / flipped basis (bad deformation).
    * start_rig_physics_probe        -- perturb a spatial control by shake_cm (held), solve settle_frames, stash
                                        every bone's per-frame global position (data-only -> GC-safe).
    * get_rig_physics_probe_report   -- read the stashed probe; per-bone residual motion (still-ringing bones).
    * measure_mesh_penetration       -- skeleton-geometry self-penetration proxy: bone spheres (radius = 0.5 *
                                        nearest-neighbour dist); flag non-adjacent pairs closer than r_a+r_b-margin.
    * fit_rig_chain_collision        -- fit a sphere/capsule to a bone chain's solved positions + margin
                                        (compute-and-return; writing into a PhysicsAsset is deferred).

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

# --- Output Log auto-capture (copied verbatim from controlrig_runtime_cpp.py) --
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
    return {"status": "error", "error": (fn + " requires the C++ ControlRig-physics handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_ControlRigPhysics.cpp to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _VALIDATE_PHYSICS_BODY = _HELP + r'''
rl = _mrl("validate_rig_physics_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("validate_rig_physics")))
else:
    res = _decode(rl.validate_rig_physics_json(PARAMS["control_rig_path"],
        float(PARAMS.get("deviation_threshold_cm", 1.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def validate_rig_physics(ctx, control_rig_path: str, deviation_threshold_cm: float = 1.0) -> str:
        """Check a Control Rig's physics setup + solver stability (instantiate, census, run an observation window).

        control_rig_path:       Control Rig blueprint asset path (e.g. '/Game/.../CR_Mannequin_Procedural').
        deviation_threshold_cm: cm beyond which a bone's drift or end-of-window residual velocity is flagged
                                (default 1.0; 0 -> 1.0).

        Instantiates a TRANSIENT rig, censuses its physics components (solver/body/joint counts by type + physics
        elements), then solves an internal ~90-frame window at 1/60s -- any embedded Step Physics Solver node
        rides along. Reports {control_rig, contains_simulation, deviation_threshold_cm, observe_frames, frames_run,
        stable, has_nan, exploded, max_drift_cm, max_residual_cm_per_frame, bone_count, deviating_bone_count,
        deviations:[{bone, max_drift_cm, residual_cm_per_frame}], physics_setup:{physics_element_count,
        component_count, component_types:{...}, physics_component_count}[, note]}. contains_simulation=false means
        the rig has no physics (or ControlRig.Physics.EnableStepSolver=0) -- only physics_setup is meaningful then.
        The source asset is never touched. ControlRig-physics C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "deviation_threshold_cm": deviation_threshold_cm}
        try:
            return json.dumps(_exec(_VALIDATE_PHYSICS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _VALIDATE_DEFORM_BODY = _HELP + r'''
rl = _mrl("validate_rig_deformation_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("validate_rig_deformation")))
else:
    res = _decode(rl.validate_rig_deformation_json(PARAMS["control_rig_path"],
        float(PARAMS.get("scale_tolerance", 0.05)), float(PARAMS.get("shear_tolerance_deg", 1.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def validate_rig_deformation(ctx, control_rig_path: str, scale_tolerance: float = 0.05,
                                 shear_tolerance_deg: float = 1.0) -> str:
        """Detect non-uniform scale / shear / flipped basis in a Control Rig's SOLVED skeleton (bad deformation).

        control_rig_path:    Control Rig blueprint asset path.
        scale_tolerance:     flags an element whose longest/shortest basis-axis length ratio exceeds
                             (1 + scale_tolerance) (default 0.05 = 5%; 0 -> 0.05).
        shear_tolerance_deg: flags an element whose inter-axis angle deviates from 90 deg by more than this
                             (default 1.0; 0 -> 1.0).

        For each transform-bearing element the concatenated GLOBAL matrix is rebuilt from local FMatrices up the
        parent chain (FTransform cannot represent the shear that non-uniform parent scale injects), then decomposed.
        Returns {control_rig, scale_tolerance, shear_tolerance_deg, examined_element_count, non_uniform_scale_count,
        shear_count, flipped_count, flagged_count, clean, flagged:[{name, type, axis_lengths, scale_ratio,
        max_shear_deg, determinant, non_uniform_scale, shear, flipped}], method}. Pure geometry, no physics.
        ControlRig-physics C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "scale_tolerance": scale_tolerance,
                  "shear_tolerance_deg": shear_tolerance_deg}
        try:
            return json.dumps(_exec(_VALIDATE_DEFORM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PROBE_START_BODY = _HELP + r'''
rl = _mrl("start_rig_physics_probe_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("start_rig_physics_probe")))
else:
    res = _decode(rl.start_rig_physics_probe_json(PARAMS["control_rig_path"], PARAMS.get("control", ""),
        float(PARAMS.get("shake_cm", 10.0)), int(PARAMS.get("settle_frames", 60))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def start_rig_physics_probe(ctx, control_rig_path: str, control: str, shake_cm: float = 10.0,
                                settle_frames: int = 60) -> str:
        """Perturb a spatial control, let physics settle over N frames, and stash the settle trajectory.

        control_rig_path: Control Rig blueprint asset path (also the stash key).
        control:          name of a SPATIAL control (Position/Transform) to shake -- exact name first, else a
                          case-insensitive substring over spatial controls.
        shake_cm:         cm to offset the control's LOCAL translation (+X), HELD for the settle (default 10; 0 -> 10).
        settle_frames:    frames to solve + record after the perturbation (1..5000; default 60), at 1/60s.

        The rig is warmed to rest, the control is offset and held, then solved settle_frames times -- any embedded
        Step Physics Solver rides along, so the bodies ring down toward the new equilibrium. Every bone's per-frame
        global position is stashed (data-only -> GC-safe) keyed by control_rig_path. Returns {control_rig,
        perturbed_control, control_type, shake_cm, settle_frames_captured, delta_time, bone_count,
        contains_simulation, note}. Read residuals via get_rig_physics_probe_report. The source asset is never
        touched. ControlRig-physics C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "control": control, "shake_cm": shake_cm,
                  "settle_frames": settle_frames}
        try:
            return json.dumps(_exec(_PROBE_START_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PROBE_REPORT_BODY = _HELP + r'''
rl = _mrl("get_rig_physics_probe_report_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("get_rig_physics_probe_report")))
else:
    res = _decode(rl.get_rig_physics_probe_report_json(PARAMS["control_rig_path"],
        float(PARAMS.get("residual_threshold_cm", 0.1)), int(PARAMS.get("max_bones", 40))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def get_rig_physics_probe_report(ctx, control_rig_path: str, residual_threshold_cm: float = 0.1,
                                     max_bones: int = 40) -> str:
        """Report residual (still-ringing) motion per bone from a stashed physics probe.

        control_rig_path:      the same path passed to start_rig_physics_probe (the stash key).
        residual_threshold_cm: per-frame end-of-window velocity above which a bone is "not settled" (default 0.1;
                               0 -> 0.1).
        max_bones:             cap on reported bones, sorted by residual desc (default 40; 0 -> 40).

        Residual = distance a bone moved on the FINAL settle frame (still-ringing => underdamped/unstable). Returns
        {control_rig, perturbed_control, shake_cm, settle_frames, delta_time, contains_simulation,
        residual_threshold_cm, bone_count, unsettled_bone_count, max_residual_cm_per_frame, reported_bone_count,
        bones:[{bone, residual_cm_per_frame, settle_path_cm, net_disp_cm, peak_disp_cm, settled}][, note]}. Errors
        if no probe is stashed for that path. ControlRig-physics C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "residual_threshold_cm": residual_threshold_cm,
                  "max_bones": max_bones}
        try:
            return json.dumps(_exec(_PROBE_REPORT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _PENETRATION_BODY = _HELP + r'''
rl = _mrl("measure_mesh_penetration_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("measure_mesh_penetration")))
else:
    res = _decode(rl.measure_mesh_penetration_json(PARAMS["control_rig_path"], PARAMS.get("chain_filter", ""),
        PARAMS.get("body_filter", ""), float(PARAMS.get("margin_cm", 0.0))))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def measure_mesh_penetration(ctx, control_rig_path: str, chain_filter: str = "", body_filter: str = "",
                                 margin_cm: float = 0.0) -> str:
        """Detect skeletal self-penetration (bones overlapping beyond a margin) in the SOLVED pose (geometry proxy).

        control_rig_path: Control Rig blueprint asset path.
        chain_filter:     comma-separated case-insensitive substring filter selecting the PROBE bones (empty -> all).
        body_filter:      comma-separated substring filter selecting the TARGET bones (empty -> all).
        margin_cm:        overlap tolerance; a pair is flagged when (r_a + r_b - distance) > margin_cm (default 0).

        HEURISTIC: each bone is a sphere at its solved position with radius = 0.5 * distance to its nearest
        hierarchy neighbour (parent or closest child, floored 0.5cm); NON-adjacent probe/target pairs closer than
        their combined radii (by more than margin) are flagged. This is a skeleton-geometry proxy -- NOT skin-weight
        or physics-body accurate (those need an extra mesh/PhysicsAsset param, deferred). Per-bone radii are
        reported for transparency. Returns {control_rig, margin_cm, bone_count, probe_bone_count,
        penetration_count, deepest_penetration_cm, reported_count, penetrations:[{bone_a, bone_b, distance_cm,
        radius_a_cm, radius_b_cm, penetration_cm}] (sorted desc, capped 200), method}. ControlRig-physics C++;
        needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "chain_filter": chain_filter,
                  "body_filter": body_filter, "margin_cm": margin_cm}
        try:
            return json.dumps(_exec(_PENETRATION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _FIT_COLLISION_BODY = _HELP + r'''
rl = _mrl("fit_rig_chain_collision_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("fit_rig_chain_collision")))
else:
    res = _decode(rl.fit_rig_chain_collision_json(PARAMS["control_rig_path"], PARAMS.get("module_name", ""),
        float(PARAMS.get("margin_cm", 0.0)), PARAMS.get("shape", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def fit_rig_chain_collision(ctx, control_rig_path: str, module_name: str = "", margin_cm: float = 0.0,
                                shape: str = "") -> str:
        """Fit a collision primitive (sphere/capsule) to a Control Rig bone chain's SOLVED positions.

        control_rig_path: Control Rig blueprint asset path.
        module_name:      comma-separated case-insensitive substring / module-namespace filter selecting the chain
                          bones (empty -> all bones).
        margin_cm:        cm added to the fitted radius (default 0).
        shape:            'sphere' | 'capsule' | '' (auto: capsule if the chain is elongated, else sphere).

        COMPUTE-AND-RETURN only -- writing the primitive into a PhysicsAsset or as a RigPhysicsBodyComponent is a
        heavier write path (needs the ControlRigPhysics link + undo) and is DEFERRED (write_applied=false). Sphere
        fit = bounding sphere about the centroid; capsule fit = axis from the farthest-apart bone pair, radius =
        max perpendicular distance. Returns {control_rig, module_name, margin_cm, chain_bone_count, shape,
        write_applied, scope_note, chain_bones:[...], fitted_shape, sphere:{center, radius_cm} OR capsule:{point_a,
        point_b, center, axis, segment_length_cm, radius_cm}}. Errors if no bone matched module_name.
        ControlRig-physics C++; needs the handler (inert until built)."""
        params = {"control_rig_path": control_rig_path, "module_name": module_name, "margin_cm": margin_cm,
                  "shape": shape}
        try:
            return json.dumps(_exec(_FIT_COLLISION_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
