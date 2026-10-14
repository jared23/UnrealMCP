"""Semantic pose authoring (SPEC 01) — set_pose / get_pose.

Callers speak SEMANTIC MOTION (flexion, abduction, ...), never raw local-axis degrees.
The bridge (this module) owns the axis mapping (calibration file) and the joint limits
(limits file): it resolves each (bone, motion) -> signed local axis, enforces limits, and
composes a per-bone delta quaternion. The editor only APPLIES: posed_local = ref_local *
delta, authored via the AnimDataController recipe. A raw-axis sign error is unrepresentable.

Rig data is resolved by ROLE via the project CLAUDE.md '## Rig data' block (or explicit
*_path args) — never a guessed path. Missing calibration is a hard refusal, never a guess.

AnimDataController gotchas honored (UE 5.8, verified on this rig):
  - controller = anim.get_editor_property("controller")  (get_controller() does not exist)
  - set_number_of_frames(unreal.FrameNumber(N))          (FrameNumber struct, not int)
  - set_bone_track_keys needs unreal.Array(Vector/Quat)  (plain lists silently no-op)
  - key count = num_frames + 1; keys are LOCAL, seeded from the reference pose (never identity)
  - verify by get_anim_pose_at_frame -> get_bone_pose (NOT get_positional_keys, empty pre-bake)
  - set_update_animation_in_editor(True) so a live preview refreshes
"""
import json
import base64
import os
import re
import math

MARKER = "@@UMCP@@"

MOTIONS = (
    "flexion", "extension", "abduction", "adduction",
    "internal_rotation", "external_rotation",
    "lateral_bend_l", "lateral_bend_r", "axial_rotation_l", "axial_rotation_r",
    "dorsiflexion", "plantarflexion", "inversion", "eversion",
    "elevation", "depression", "protraction", "retraction",
    "pronation", "supination", "radial_deviation", "ulnar_deviation",
)


# ---------- bridge-side helpers ----------
def _axis_vec(signed):
    """'+Y' / '-x' / 'Y' -> (unit-vector tuple, signed_scalar 1/-1)."""
    s = str(signed).strip()
    sign = 1.0
    if s and s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    s = s.upper()
    base = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}.get(s)
    if base is None:
        raise ValueError("axis must be one of +X -X +Y -Y +Z -Z, got %r" % signed)
    return base, sign


def _axis_angle_quat(axis, deg):
    a = math.radians(deg) / 2.0
    s = math.sin(a)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(a))


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_claude_rig_data(project_dir):
    """Read <project_dir>/CLAUDE.md, return {role_lower: path} from the '## Rig data' block."""
    cpath = os.path.join(project_dir, "CLAUDE.md")
    if not os.path.exists(cpath):
        return {}
    out = {}
    in_block = False
    with open(cpath, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip().lower().startswith("## rig data"):
                in_block = True
                continue
            if in_block and line.startswith("## "):
                break
            if in_block:
                m = re.match(r"\s*-\s*([A-Za-z ]+?):\s*`([^`]+)`", line)
                if m:
                    out[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def _resolve_project_dir(send_command):
    try:
        resp = send_command("execute_python", {
            "code": "import unreal,json;print('@@UMCP@@'+json.dumps({'d':unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())}))"})
        out = resp.get("result", {}).get("output", "")
        for ln in out.splitlines():
            if MARKER in ln:
                return json.loads(ln.split(MARKER, 1)[1])["d"]
    except Exception:
        pass
    return None


def _resolve_rig_paths(send_command, calibration_path, limits_path, project_dir):
    pdir = project_dir or _resolve_project_dir(send_command)
    decl = _parse_claude_rig_data(pdir) if pdir else {}

    def _abs(p):
        if not p:
            return None
        return p if os.path.isabs(p) else (os.path.join(pdir, p) if pdir else p)

    cal = calibration_path or _abs(decl.get("calibration"))
    lim = limits_path or _abs(decl.get("joint limits"))
    return pdir, cal, lim


# ---------- in-editor apply body (base64 PARAMS; no ''' or backslashes) ----------
_APPLY_BODY = (
    "import unreal, json, math\n"
    "seq = unreal.load_asset(PARAMS['animation_path'])\n"
    "res = {'applied': {}, 'errors': []}\n"
    "if seq is None or not isinstance(seq, unreal.AnimSequence):\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'animation_path is not an AnimSequence: ' + str(PARAMS['animation_path'])})); raise SystemExit\n"
    "skel = seq.get_skeleton()\n"
    "ctrl = seq.get_editor_property('controller')\n"
    "model = seq.get_editor_property('data_model')\n"
    "try:\n"
    "    nframes = model.get_number_of_frames()\n"
    "except Exception:\n"
    "    nframes = 1\n"
    "frame = int(PARAMS['frame'])\n"
    "opts = unreal.AnimPoseEvaluationOptions()\n"
    "refname = 'MCP_RefPose_' + skel.get_name()\n"
    "if unreal.EditorAssetLibrary.does_asset_exist('/Game/_MCP_Trash/' + refname):\n"
    "    refseq = unreal.load_asset('/Game/_MCP_Trash/' + refname)\n"
    "else:\n"
    "    af = unreal.AnimSequenceFactory(); af.set_editor_property('target_skeleton', skel)\n"
    "    refseq = unreal.AssetToolsHelpers.get_asset_tools().create_asset(refname, '/Game/_MCP_Trash', unreal.AnimSequence, af)\n"
    "    rc = refseq.get_editor_property('controller'); rc.open_bracket('ref', False); rc.set_number_of_frames(unreal.FrameNumber(1), False); rc.close_bracket(False)\n"
    "def refpose(b):\n"
    "    p = refseq.get_anim_pose_at_frame(0, opts); return p.get_bone_pose(b, unreal.AnimPoseSpaces.LOCAL)\n"
    "def curlocal(b, fi):\n"
    "    p = seq.get_anim_pose_at_frame(fi, opts); return p.get_bone_pose(b, unreal.AnimPoseSpaces.LOCAL)\n"
    "def qmul(a, bq):\n"
    "    ax,ay,az,aw=a.x,a.y,a.z,a.w; bx,by,bz,bw=bq.x,bq.y,bq.z,bq.w\n"
    "    return unreal.Quat(aw*bx+ax*bw+ay*bz-az*by, aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw, aw*bw-ax*bx-ay*by-az*bz)\n"
    "ctrl.open_bracket('set_pose', False)\n"
    "if frame + 1 > nframes:\n"
    "    ctrl.set_number_of_frames(unreal.FrameNumber(frame + 1), False); nframes = frame + 1\n"
    "for bone in PARAMS['order']:\n"
    "    spec = PARAMS['bones'][bone]\n"
    "    try:\n"
    "        ref = refpose(bone)\n"
    "        dq = spec['delta']\n"
    "        delta = unreal.Quat(dq[0], dq[1], dq[2], dq[3])\n"
    "        posed_rot = qmul(ref.rotation, delta)\n"
    "        base_pos = ref.translation\n"
    "        if spec.get('dloc'):\n"
    "            dl = spec['dloc']; base_pos = unreal.Vector(base_pos.x+dl[0], base_pos.y+dl[1], base_pos.z+dl[2])\n"
    "        va = unreal.Array(unreal.Vector); qa = unreal.Array(unreal.Quat); sa = unreal.Array(unreal.Vector)\n"
    "        for fi in range(nframes + 1):\n"
    "            if fi == frame:\n"
    "                va.append(base_pos); qa.append(posed_rot); sa.append(ref.scale3d)\n"
    "            else:\n"
    "                cl = curlocal(bone, min(fi, nframes)); va.append(cl.translation); qa.append(cl.rotation); sa.append(cl.scale3d)\n"
    "        ctrl.add_bone_track(bone, False)\n"
    "        ctrl.set_bone_track_keys(bone, va, qa, sa, False)\n"
    "        chk = curlocal(bone, frame).rotation.rotator()\n"
    "        res['applied'][bone] = {'local_euler': [round(chk.roll,3), round(chk.pitch,3), round(chk.yaw,3)]}\n"
    "    except Exception as e:\n"
    "        res['errors'].append(bone + ': ' + str(e))\n"
    "ctrl.close_bracket(False)\n"
    "for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors():\n"
    "    try:\n"
    "        for comp in a.get_components_by_class(unreal.SkeletalMeshComponent):\n"
    "            if comp.get_animation_mode() == unreal.AnimationMode.ANIMATION_SINGLE_NODE and comp.animation_data.anim_to_play == seq:\n"
    "                comp.set_update_animation_in_editor(True); comp.set_position(float(frame)/30.0)\n"
    "    except Exception:\n"
    "        pass\n"
    "res['status'] = 'success'; res['num_frames'] = nframes\n"
    "print('@@UMCP@@' + json.dumps(res))\n"
)

_READ_BODY = (
    "import unreal, json\n"
    "seq = unreal.load_asset(PARAMS['animation_path'])\n"
    "if seq is None or not isinstance(seq, unreal.AnimSequence):\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'not an AnimSequence'})); raise SystemExit\n"
    "opts = unreal.AnimPoseEvaluationOptions(); frame = int(PARAMS['frame'])\n"
    "p = seq.get_anim_pose_at_frame(frame, opts)\n"
    "bones = PARAMS.get('bones') or [str(b) for b in p.get_bone_names()]\n"
    "out = {}\n"
    "for b in bones:\n"
    "    try:\n"
    "        L = p.get_bone_pose(b, unreal.AnimPoseSpaces.LOCAL); C = p.get_bone_pose(b, unreal.AnimPoseSpaces.WORLD)\n"
    "        lr = L.rotation.rotator(); cr = C.rotation.rotator()\n"
    "        out[b] = {'local': {'loc':[round(L.translation.x,3),round(L.translation.y,3),round(L.translation.z,3)], 'euler':[round(lr.roll,3),round(lr.pitch,3),round(lr.yaw,3)]},\n"
    "                  'component': {'loc':[round(C.translation.x,3),round(C.translation.y,3),round(C.translation.z,3)], 'euler':[round(cr.roll,3),round(cr.pitch,3),round(cr.yaw,3)]}}\n"
    "    except Exception as e:\n"
    "        out[b] = {'error': str(e)}\n"
    "print('@@UMCP@@' + json.dumps({'status':'success','frame':frame,'bones':out}))\n"
)


def register_tools(mcp, utils):
    send_command = utils["send_command"]

    def _run(body, params):
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = "import base64 as _b, json as _j\nPARAMS=_j.loads(_b.b64decode('%s').decode('utf-8'))\n" % b64
        resp = send_command("execute_python", {"code": header + body})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError("execute_python failed: %s" % resp)
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError("no marker in output:\n" + out)

    @mcp.tool()
    def set_pose(ctx, animation_path: str, frame: int, pose: dict,
                 calibration_path: str = None, limits_path: str = None,
                 project_dir: str = None, allow_over_limit: bool = False) -> str:
        """Author a pose at ONE frame of an AnimSequence using SEMANTIC motions (never raw axes).

        animation_path: AnimSequence object/package path.
        frame:          frame index to key.
        pose:           {bone: {motion: degrees, ...}, ...}. `motion` is a semantic name
                        (flexion, extension, abduction, adduction, internal_rotation,
                        external_rotation, lateral_bend_l/r, axial_rotation_l/r, dorsiflexion,
                        plantarflexion, inversion, eversion, elevation, depression, protraction,
                        retraction, pronation, supination, radial_deviation, ulnar_deviation).
                        A bone may also carry "dloc": [x,y,z] for root/pelvis translation.
        calibration_path / limits_path / project_dir: optional overrides; otherwise resolved by
                        role from the project CLAUDE.md '## Rig data' block.

        Server resolves each (bone, motion) -> signed local axis via the CALIBRATION file,
        enforces the LIMITS file (rejects over-limit with a BLOCKED block, applies nothing),
        composes posed_local = ref_local * delta, and authors via AnimDataController. Missing
        calibration is refused, never guessed. Returns per-bone resulting local euler + the
        semantic values applied."""
        pdir, cal, lim = _resolve_rig_paths(send_command, calibration_path, limits_path, project_dir)
        if not cal or not os.path.exists(cal):
            return json.dumps({"status": "refused", "reason": "missing_calibration",
                               "message": "No calibration file (resolved: %r). Run Phase 0 calibration; "
                                          "never guess an axis." % cal, "project_dir": pdir}, indent=2)
        calib = _load_json(cal)
        cbones = calib.get("bones", calib)
        limits = None
        if lim and os.path.exists(lim):
            lj = _load_json(lim)
            limits = lj.get("bones", lj)

        # validate + compose
        violations = []
        composed = {}
        order_seed = list(pose.keys())
        for bone, motions in pose.items():
            dloc = None
            delta = (0.0, 0.0, 0.0, 1.0)
            for motion, deg in motions.items():
                if motion == "dloc":
                    dloc = list(deg)
                    continue
                if motion not in MOTIONS:
                    return json.dumps({"status": "error", "message": "unknown motion %r on %s; valid: %s"
                                       % (motion, bone, ", ".join(MOTIONS))}, indent=2)
                bmap = cbones.get(bone)
                if not bmap or motion not in bmap:
                    return json.dumps({"status": "refused", "reason": "uncalibrated_motion",
                                       "message": "calibration has no (%s, %s) mapping; never guess an axis."
                                       % (bone, motion), "calibration": cal}, indent=2)
                if limits is not None and bone in limits and motion in limits[bone] and not allow_over_limit:
                    lo, hi = limits[bone][motion]
                    if deg < lo or deg > hi:
                        violations.append((bone, motion, deg, lo, hi))
                axis, sign = _axis_vec(bmap[motion])
                delta = _qmul(delta, _axis_angle_quat(axis, deg * sign))
            composed[bone] = {"delta": list(delta), "dloc": dloc}

        if violations:
            lines = ["BLOCKED — joint limit violation"]
            for b, m, d, lo, hi in violations:
                lines.append("  %-9s %-16s %+g deg   outside [%g, %g]" % (b, m, d, lo, hi))
            lines.append("A violation this large is usually a SIGN ERROR — re-read the calibration for "
                         "that bone before shaving the angle.")
            return json.dumps({"status": "blocked", "violations":
                               [{"bone": b, "motion": m, "value": d, "range": [lo, hi]} for b, m, d, lo, hi in violations],
                               "message": "\n".join(lines)}, indent=2)

        # Application order: each bone writes an independent LOCAL track, so order does not change the
        # result; we keep the caller's order (root-outward is the caller's responsibility per the protocol).
        result = _run(_APPLY_BODY, {"animation_path": animation_path, "frame": int(frame),
                                    "bones": composed, "order": order_seed})
        result["semantic_applied"] = pose
        return json.dumps(result, indent=2)

    @mcp.tool()
    def get_pose(ctx, animation_path: str, frame: int, bones: list = None) -> str:
        """Read per-bone LOCAL and COMPONENT-space transforms (translation + euler) at a frame of
        an AnimSequence. Numeric state before pixels. bones: optional subset (default all)."""
        return json.dumps(_run(_READ_BODY, {"animation_path": animation_path,
                                             "frame": int(frame), "bones": bones}), indent=2)
