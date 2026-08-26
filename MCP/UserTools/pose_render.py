"""Pose render tools (SPEC 01) — render_pose_multiview + compare_pose.

Render an authored AnimSequence pose from N named cameras into ONE composited PNG (per-tile
label + a tile->camera manifest), and compare the current pose to a reference (another pose,
or an external image). Composited entirely in-editor via SceneCapture2D -> render targets ->
Canvas draw -> export_render_target (no PIL / external deps).

HOW THE POSE ACTUALLY DISPLAYS (the load-bearing mechanic — see BUG_01):
  An animated SkeletalMeshComponent does NOT evaluate an AnimSequence in the editor: whatever
  frame you set_position() to, it renders the mesh REFERENCE pose (it only advances during PIE
  or an on-screen anim-editor preview). So we do NOT drive a SkeletalMeshComponent. Instead we
  build a PoseableMeshComponent (added via SubobjectDataSubsystem) and BAKE the target frame
  onto it directly: for every bone, set_bone_transform_by_name(component-space) from
  seq.get_anim_pose_at_frame(frame). Correct at ANY frame — no anim evaluation needed.

  THE FLUSH (why this used to fail): setting the bones updates the component's transforms but a
  backgrounded, MCP-driven editor never ticks/redraws, so the GPU skin proxy stays at the
  reference pose and the SceneCapture renders ref pose regardless. After baking we call the
  plugin's C++ #52 `MCPReflectionLibrary.force_skin_flush(pmc)` (RefreshBoneTransforms ->
  RecreateRenderState_Concurrent -> FlushRenderingCommands) to synchronously push the baked pose
  to the GPU before capture. Requires the plugin built with C++ #52 (RenderCore dep).

CLEAN, LIT, FRAMED CAPTURE (the rest of BUG_01):
  - Isolation: the SceneCapture renders ONLY the subject (PRM_USE_SHOW_ONLY_LIST +
    show_only_actor_components) so no level geometry can occlude it and the background is matte
    regardless of the open level (world origin in a game level is unlit/occluded dead-space —
    we never depend on where we spawn). Atmosphere/Fog/Cloud show-flags off + a pinned manual
    exposure keep the tile a clean black.
  - Lighting: our own key + fill DirectionalLights (directional -> light the subject wherever
    it is spawned).
  - Framing: orthographic + a two-pass silhouette auto-frame. A wide measurement pass reads the
    render target (bulk read_render_target) to find the subject's actual on-screen bbox, then a
    tight pass centers + fills it. Pose-agnostic: a crouched/fallen pose frames as well as a
    standing one, and bounds/socket queries (which return stale ref-pose data for a PMC) are
    never trusted.
"""
import json
import base64
import os

MARKER = "@@UMCP@@"

# name -> {dir: unit direction from the subject toward the camera, proj kept for signature compat}.
# SK_Mannequin faces +Y (front), up +Z; sides are along +/-X. 3/4 = between +Y and -X.
# All views render orthographic (clean, consistent scale for overlay/IoU + exact auto-framing).
DEFAULT_CAMERAS = {
    "three_quarter_front": {"dir": [-0.9, 1.0, 0.35], "proj": "ortho"},
    "side_ortho": {"dir": [-1.0, 0.0, 0.05], "proj": "ortho"},
}

KEY_BONES = ["head", "spine_03", "upperarm_l", "upperarm_r", "lowerarm_l", "lowerarm_r",
             "hand_l", "hand_r", "thigh_l", "thigh_r", "calf_l", "calf_r", "foot_l", "foot_r", "pelvis"]

# ---- shared in-editor helpers (base64 PARAMS; NO ''' and NO literal backslashes in this body) ----
_HELPERS = (
    "import unreal, json, os, math\n"
    "RL = unreal.RenderingLibrary; ML = unreal.MathLibrary\n"
    "W = unreal.EditorLevelLibrary.get_editor_world()\n"
    "EAS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
    "SDS = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)\n"
    "CS = unreal.BoneSpaces.COMPONENT_SPACE\n"
    "WS = unreal.AnimPoseSpaces.WORLD; LS = unreal.AnimPoseSpaces.LOCAL\n"
    "OPTS = unreal.AnimPoseEvaluationOptions()\n"
    "BASE_Z = 10000.0; DIST = 900.0\n"
    "def cleanup():\n"
    "    for a in list(EAS.get_all_level_actors()):\n"
    "        try:\n"
    "            if a.get_actor_label().startswith('MCP_MV_'): EAS.destroy_actor(a)\n"
    "        except Exception: pass\n"
    "def load_mesh(seq):\n"
    "    m = unreal.load_asset(PARAMS['mesh_path']) if PARAMS.get('mesh_path') else None\n"
    "    if m is None:\n"
    "        try: m = seq.get_skeleton().get_preview_mesh()\n"
    "        except Exception: m = None\n"
    "    return m\n"
    "def add_pmc(actor):\n"
    "    handles = SDS.k2_gather_subobject_data_for_instance(actor)\n"
    "    p = unreal.AddNewSubobjectParams(); p.set_editor_property('parent_handle', handles[0]); p.set_editor_property('new_class', unreal.PoseableMeshComponent)\n"
    "    SDS.add_new_subobject(p)\n"
    "    return actor.get_component_by_class(unreal.PoseableMeshComponent)\n"
    "def spawn_subject(label, x, mesh):\n"
    "    a = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.Actor, unreal.Vector(x,0,BASE_Z), unreal.Rotator(0,0,0)); a.set_actor_label(label)\n"
    "    pmc = add_pmc(a); pmc.set_skinned_asset_and_update(mesh)\n"
    "    return a, pmc\n"
    "def bake(pmc, seq, frame):\n"
    "    pmc.set_editor_property('visibility_based_anim_tick_option', unreal.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)\n"
    "    pose = seq.get_anim_pose_at_frame(int(frame), OPTS)\n"
    "    names = [str(b) for b in pose.get_bone_names()]\n"
    "    for b in names:\n"
    "        try: pmc.set_bone_transform_by_name(b, pose.get_bone_pose(b, WS), CS)\n"
    "        except Exception: pass\n"
    # C++ #52 force_skin_flush (BUG_01): a backgrounded editor never ticks/redraws, so the GPU skin proxy
    # would otherwise stay at the reference pose. Synchronously flush the baked pose to the GPU
    # (RefreshBoneTransforms -> RecreateRenderState_Concurrent -> FlushRenderingCommands) so the next capture sees it.
    "    unreal.MCPReflectionLibrary.force_skin_flush(pmc)\n"
    "    return pose\n"
    "def spawn_lights():\n"
    "    k = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0,0,BASE_Z+300), unreal.Rotator(-35,-90,0)); k.set_actor_label('MCP_MV_Key'); k.light_component.set_intensity(7.0)\n"
    "    f = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0,0,BASE_Z+300), unreal.Rotator(-15,-160,0)); f.set_actor_label('MCP_MV_Fill'); f.light_component.set_intensity(3.0)\n"
    "def mk_capture():\n"
    "    sc = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SceneCapture2D, unreal.Vector(0,0,0), unreal.Rotator(0,0,0)); sc.set_actor_label('MCP_MV_Cap')\n"
    "    cap = sc.capture_component2d\n"
    "    cap.set_editor_property('capture_source', unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)\n"
    "    cap.set_editor_property('projection_type', unreal.CameraProjectionMode.ORTHOGRAPHIC)\n"
    "    cap.set_editor_property('primitive_render_mode', unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST)\n"
    "    def sf(n,e):\n"
    "        s = unreal.EngineShowFlagsSetting(); s.set_editor_property('show_flag_name', n); s.set_editor_property('enabled', e); return s\n"
    "    cap.set_editor_property('show_flag_settings', [sf('Atmosphere',False), sf('Fog',False), sf('VolumetricFog',False), sf('Cloud',False), sf('EyeAdaptation',False)])\n"
    "    pp = unreal.PostProcessSettings(); pp.set_editor_property('override_auto_exposure_bias', True); pp.set_editor_property('auto_exposure_bias', 11.0); cap.set_editor_property('post_process_settings', pp)\n"
    "    return sc, cap\n"
    "def show_only(cap, actor):\n"
    "    cap.clear_show_only_components(); cap.show_only_actor_components(actor)\n"
    "def shoot(sc, cap, d, aim, ow, px, py):\n"
    "    loc = unreal.Vector(aim.x+d[0]*DIST, aim.y+d[1]*DIST, aim.z+d[2]*DIST); rot = ML.find_look_at_rotation(loc, aim)\n"
    "    cap.set_editor_property('ortho_width', ow); sc.set_actor_location_and_rotation(loc, rot, False, False)\n"
    "    rt = RL.create_render_target2d(W, px, py, unreal.TextureRenderTargetFormat.RTF_RGBA8); cap.set_editor_property('texture_target', rt); cap.capture_scene()\n"
    "    return rt, rot\n"
    "def mask_bbox(rt, px, py, thr):\n"
    "    arr = RL.read_render_target(W, rt)\n"
    "    u0=1.0; v0=1.0; u1=0.0; v1=0.0; hit=0\n"
    "    for y in range(py):\n"
    "        row = y*px\n"
    "        for x in range(px):\n"
    "            c = arr[row+x]\n"
    "            if (c.r+c.g+c.b) > thr:\n"
    "                hit += 1; uu=(x+0.5)/px; vv=(y+0.5)/py\n"
    "                if uu<u0: u0=uu\n"
    "                if uu>u1: u1=uu\n"
    "                if vv<v0: v0=vv\n"
    "                if vv>v1: v1=vv\n"
    "    return u0, v0, u1, v1, hit\n"
    "def norm(d):\n"
    "    l = math.sqrt(d[0]*d[0]+d[1]*d[1]+d[2]*d[2]) or 1.0\n"
    "    return [d[0]/l, d[1]/l, d[2]/l]\n"
    "def measure(sc, cap, d, seed, ow1, out_w, out_h):\n"
    "    AR = float(out_h)/float(out_w)\n"
    "    rt, rot = shoot(sc, cap, d, seed, ow1, 160, 180)\n"
    "    u0,v0,u1,v1,hit = mask_bbox(rt, 160, 180, 60)\n"
    "    return (u0,v0,u1,v1,hit,rot,AR)\n"
    "def frame_from_bbox(seed, rot, AR, ow1, bb, fill):\n"
    "    u0,v0,u1,v1 = bb\n"
    "    cu=(u0+u1)/2.0; cv=(v0+v1)/2.0; rgt=rot.get_right_vector(); up=rot.get_up_vector()\n"
    "    du=(cu-0.5)*ow1; dv=(cv-0.5)*ow1*AR\n"
    "    aim = unreal.Vector(seed.x+rgt.x*du-up.x*dv, seed.y+rgt.y*du-up.y*dv, seed.z+rgt.z*du-up.z*dv)\n"
    "    sil_w=(u1-u0)*ow1; sil_h=(v1-v0)*ow1*AR\n"
    "    ow = max(sil_w, sil_h/AR)/fill\n"
    "    return aim, max(ow, 40.0)\n"
    "def project(P, aim, rot, ow, tw, th):\n"
    "    AR=float(th)/float(tw); rgt=rot.get_right_vector(); up=rot.get_up_vector()\n"
    "    rel = unreal.Vector(P.x-aim.x, P.y-aim.y, P.z-aim.z)\n"
    "    r = rel.x*rgt.x+rel.y*rgt.y+rel.z*rgt.z; u = rel.x*up.x+rel.y*up.y+rel.z*up.z\n"
    "    nx = r/(ow*0.5); ny = u/(ow*AR*0.5)\n"
    "    return ((nx+1.0)*0.5*tw, (1.0-ny)*0.5*th)\n"
    "def outpath(default_name):\n"
    "    outdir = PARAMS.get('out_dir') or unreal.Paths.convert_relative_path_to_full(os.path.join(unreal.Paths.project_saved_dir(),'Screenshots','MCP'))\n"
    "    fname = PARAMS.get('out_name') or default_name\n"
    "    if not fname.lower().endswith('.png'): fname = fname + '.png'\n"
    "    return outdir, fname\n"
    "FONT = unreal.load_object(None, '/Engine/EngineFonts/Roboto.Roboto')\n"
)

# ---------- render_pose_multiview ----------
_MULTIVIEW_BODY = _HELPERS + (
    "cleanup()\n"
    "seq = unreal.load_asset(PARAMS['animation_path'])\n"
    "if seq is None or not isinstance(seq, unreal.AnimSequence):\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'animation_path is not an AnimSequence'})); raise SystemExit\n"
    "mesh = load_mesh(seq)\n"
    "if mesh is None:\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'no mesh: pass mesh_path (skeleton has no preview mesh)'})); raise SystemExit\n"
    "frame = int(PARAMS['frame'])\n"
    "actor, pmc = spawn_subject('MCP_MV_Subject', 0.0, mesh)\n"
    "bake(pmc, seq, frame)\n"
    "spawn_lights()\n"
    "sc, cap = mk_capture(); show_only(cap, actor)\n"
    "aloc = actor.get_actor_location(); seed = unreal.Vector(aloc.x, aloc.y, aloc.z+80.0)\n"
    "cams = PARAMS['cameras']; tw = int(PARAMS['tile_w']); th = int(PARAMS['tile_h']); lab = 34; n = len(cams)\n"
    "tiles = []\n"
    "for name, d, proj in cams:\n"
    "    d = norm(d)\n"
    "    u0,v0,u1,v1,hit,rot,AR = measure(sc, cap, d, seed, 460.0, tw, th)\n"
    "    if hit == 0:\n"
    "        rt, _ = shoot(sc, cap, d, seed, 220.0, tw, th)\n"
    "    else:\n"
    "        aim, ow = frame_from_bbox(seed, rot, AR, 460.0, (u0,v0,u1,v1), 0.85)\n"
    "        rt, _ = shoot(sc, cap, d, aim, ow, tw, th)\n"
    "    tiles.append((name, rt))\n"
    "comp = RL.create_render_target2d(W, tw*n, th+lab, unreal.TextureRenderTargetFormat.RTF_RGBA8)\n"
    "canvas, size, ctx = RL.begin_draw_canvas_to_render_target(W, comp)\n"
    "manifest = {}\n"
    "for i, (name, rt) in enumerate(tiles):\n"
    "    x = i*tw\n"
    "    canvas.draw_texture(rt, unreal.Vector2D(x,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "    canvas.draw_text(FONT, name + '   frame ' + str(frame), unreal.Vector2D(x+10, th+7), unreal.Vector2D(1,1), unreal.LinearColor(1,1,0,1))\n"
    "    manifest[str(i)] = name\n"
    "RL.end_draw_canvas_to_render_target(W, ctx)\n"
    "outdir, fname = outpath('multiview_f' + str(frame) + '.png')\n"
    "RL.export_render_target(W, comp, outdir, fname)\n"
    "cleanup()\n"
    "p = os.path.join(outdir, fname).replace(chr(92), '/')\n"
    "print('@@UMCP@@' + json.dumps({'status':'success','image': p, 'exists': os.path.exists(os.path.join(outdir,fname)), 'manifest': manifest, 'cameras': [c[0] for c in cams], 'frame': frame, 'tile': [tw, th]}))\n"
)

# ---------- compare_pose: pose-vs-pose NUMERIC (two co-located subjects, common framing) ----------
_COMPARE_POSE_BODY = _HELPERS + (
    "cleanup()\n"
    "cur_seq = unreal.load_asset(PARAMS['animation_path']); ref_seq = unreal.load_asset(PARAMS['ref_animation_path'])\n"
    "if cur_seq is None or ref_seq is None:\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'could not load animation_path / ref_animation_path'})); raise SystemExit\n"
    "mesh = load_mesh(cur_seq)\n"
    "if mesh is None:\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'no mesh: pass mesh_path'})); raise SystemExit\n"
    "cur_frame = int(PARAMS['frame']); ref_frame = int(PARAMS['ref_frame'])\n"
    "name, d, proj = PARAMS['camera']; d = norm(d)\n"
    "tw = int(PARAMS['tile_w']); th = int(PARAMS['tile_h']); lab = 34; keys = PARAMS['keys']\n"
    # co-locate both subjects at x=0 (show_only isolates them so overlap is fine) -> same camera aligns them.
    "refA, refP = spawn_subject('MCP_MV_Ref', 0.0, mesh); ref_pose = bake(refP, ref_seq, ref_frame)\n"
    "curA, curP = spawn_subject('MCP_MV_Cur', 0.0, mesh); cur_pose = bake(curP, cur_seq, cur_frame)\n"
    # align='pelvis' (opt-in): translate each subject so its pelvis lands at a common point, removing the
    # global position/root-motion difference so the compare is POSE-ONLY (limb configuration). Default
    # 'none' keeps root motion (faithful screen positions). Moving the actor makes its render transform
    # stale in a backgrounded editor -> re-flush (RecreateRenderState rebuilds the proxy at the new transform).
    "if PARAMS.get('align') == 'pelvis':\n"
    "    try:\n"
    "        rp = ref_pose.get_bone_pose('pelvis', WS).translation; cp = cur_pose.get_bone_pose('pelvis', WS).translation\n"
    "        refA.set_actor_location(unreal.Vector(-rp.x, -rp.y, BASE_Z-rp.z), False, False); unreal.MCPReflectionLibrary.force_skin_flush(refP)\n"
    "        curA.set_actor_location(unreal.Vector(-cp.x, -cp.y, BASE_Z-cp.z), False, False); unreal.MCPReflectionLibrary.force_skin_flush(curP)\n"
    "    except Exception: pass\n"
    "spawn_lights()\n"
    "sc, cap = mk_capture()\n"
    "raloc = refA.get_actor_location(); caloc = curA.get_actor_location(); seed = unreal.Vector(0.0, 0.0, BASE_Z+80.0)\n"
    # union framing from both silhouettes so ref + cur share one camera (the load-bearing requirement)
    "show_only(cap, refA); ru0,rv0,ru1,rv1,rhit,rot,AR = measure(sc, cap, d, seed, 460.0, tw, th)\n"
    "show_only(cap, curA); cu0,cv0,cu1,cv1,chit,rot,AR = measure(sc, cap, d, seed, 460.0, tw, th)\n"
    "bb = (min(ru0,cu0), min(rv0,cv0), max(ru1,cu1), max(rv1,cv1)) if (rhit and chit) else ((cu0,cv0,cu1,cv1) if chit else (ru0,rv0,ru1,rv1))\n"
    "aim, ow = frame_from_bbox(seed, rot, AR, 460.0, bb, 0.82)\n"
    "show_only(cap, refA); ref_rt, crot = shoot(sc, cap, d, aim, ow, tw, th)\n"
    "show_only(cap, curA); cur_rt, crot = shoot(sc, cap, d, aim, ow, tw, th)\n"
    # numeric: per-joint pixel delta (ortho projection, shared camera), angular error (LOCAL quats), silhouette IoU
    "joints = {}; pxs = []; angs = []\n"
    "for b in keys:\n"
    "    j = {}\n"
    "    try:\n"
    "        Pr = ref_pose.get_bone_pose(b, WS).translation; Pc = cur_pose.get_bone_pose(b, WS).translation\n"
    "        Wr = unreal.Vector(raloc.x+Pr.x, raloc.y+Pr.y, raloc.z+Pr.z); Wc = unreal.Vector(caloc.x+Pc.x, caloc.y+Pc.y, caloc.z+Pc.z)\n"
    "        sr = project(Wr, aim, crot, ow, tw, th); scc = project(Wc, aim, crot, ow, tw, th)\n"
    "        dx = scc[0]-sr[0]; dy = scc[1]-sr[1]; dp = math.hypot(dx, dy)\n"
    "        j['dx'] = round(dx,1); j['dy'] = round(dy,1); j['px'] = round(dp,1); pxs.append(dp)\n"
    "    except Exception: pass\n"
    "    try:\n"
    "        qr = ref_pose.get_bone_pose(b, LS).rotation; qc = cur_pose.get_bone_pose(b, LS).rotation\n"
    "        dq = min(1.0, abs(qr.x*qc.x + qr.y*qc.y + qr.z*qc.z + qr.w*qc.w)); ang = math.degrees(2.0*math.acos(dq))\n"
    "        j['ang'] = round(ang,1); angs.append(ang)\n"
    "    except Exception: pass\n"
    "    if j: joints[b] = j\n"
    "iou = None\n"
    "try:\n"
    "    ra = RL.read_render_target(W, ref_rt); ca = RL.read_render_target(W, cur_rt); inter=0; uni=0\n"
    "    step = max(1, (tw*th)//4000)\n"
    "    for idx in range(0, tw*th, step):\n"
    "        rc = ra[idx]; cc = ca[idx]; rm = (rc.r+rc.g+rc.b) > 30; cm = (cc.r+cc.g+cc.b) > 30\n"
    "        if rm or cm: uni += 1\n"
    "        if rm and cm: inter += 1\n"
    "    iou = round(float(inter)/uni, 3) if uni else 0.0\n"
    "except Exception: iou = None\n"
    "comp = RL.create_render_target2d(W, tw*3, th+lab, unreal.TextureRenderTargetFormat.RTF_RGBA8)\n"
    "canvas, size, ctx = RL.begin_draw_canvas_to_render_target(W, comp)\n"
    "canvas.draw_texture(ref_rt, unreal.Vector2D(0,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "canvas.draw_texture(cur_rt, unreal.Vector2D(tw,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "canvas.draw_texture(ref_rt, unreal.Vector2D(2*tw,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,0,1,1), unreal.BlendMode.BLEND_ADDITIVE, 0.0, unreal.Vector2D(0,0))\n"
    "canvas.draw_texture(cur_rt, unreal.Vector2D(2*tw,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(0,1,0,1), unreal.BlendMode.BLEND_ADDITIVE, 0.0, unreal.Vector2D(0,0))\n"
    "mp = round(sum(pxs)/len(pxs),1) if pxs else None; ma = round(sum(angs)/len(angs),1) if angs else None\n"
    "for i,t in enumerate(['reference  f'+str(ref_frame), 'current  f'+str(cur_frame), 'overlay: ref=magenta cur=green']):\n"
    "    canvas.draw_text(FONT, t, unreal.Vector2D(i*tw+10, th+7), unreal.Vector2D(1,1), unreal.LinearColor(1,1,0,1))\n"
    "canvas.draw_text(FONT, 'mean dpx ' + str(mp) + '   mean dang ' + str(ma) + '   IoU ' + str(iou), unreal.Vector2D(2*tw+10, 8), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1))\n"
    "RL.end_draw_canvas_to_render_target(W, ctx)\n"
    "outdir, fname = outpath('compare_pose_f' + str(cur_frame) + '.png')\n"
    "RL.export_render_target(W, comp, outdir, fname)\n"
    "cleanup()\n"
    "p = os.path.join(outdir, fname).replace(chr(92), '/')\n"
    "print('@@UMCP@@' + json.dumps({'status':'success','image': p, 'exists': os.path.exists(os.path.join(outdir,fname)), 'camera': name, 'align': PARAMS.get('align','none'), 'summary': {'mean_pixel_delta': mp, 'mean_angular_error_deg': ma, 'silhouette_iou': iou}, 'per_joint': joints}))\n"
)

# ---------- compare_pose: image-reference VISUAL mode ----------
_COMPARE_IMG_BODY = _HELPERS + (
    "cleanup()\n"
    "seq = unreal.load_asset(PARAMS['animation_path'])\n"
    "if seq is None or not isinstance(seq, unreal.AnimSequence):\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'animation_path is not an AnimSequence'})); raise SystemExit\n"
    "mesh = load_mesh(seq)\n"
    "if mesh is None:\n"
    "    print('@@UMCP@@' + json.dumps({'status':'error','message':'no mesh: pass mesh_path'})); raise SystemExit\n"
    "frame = int(PARAMS['frame']); name, d, proj = PARAMS['camera']; d = norm(d)\n"
    "tw = int(PARAMS['tile_w']); th = int(PARAMS['tile_h']); lab = 34\n"
    "actor, pmc = spawn_subject('MCP_MV_Subject', 0.0, mesh); bake(pmc, seq, frame); spawn_lights()\n"
    "sc, cap = mk_capture(); show_only(cap, actor)\n"
    "aloc = actor.get_actor_location(); seed = unreal.Vector(aloc.x, aloc.y, aloc.z+80.0)\n"
    "u0,v0,u1,v1,hit,rot,AR = measure(sc, cap, d, seed, 460.0, tw, th)\n"
    "if hit == 0:\n"
    "    cur, crot = shoot(sc, cap, d, seed, 220.0, tw, th)\n"
    "else:\n"
    "    aim, ow = frame_from_bbox(seed, rot, AR, 460.0, (u0,v0,u1,v1), 0.85); cur, crot = shoot(sc, cap, d, aim, ow, tw, th)\n"
    "reftex = None; refpath = PARAMS.get('ref_image')\n"
    "if refpath and os.path.exists(refpath):\n"
    "    task = unreal.AssetImportTask(); task.filename = refpath; task.destination_path = '/Game/_MCP_Trash'\n"
    "    task.destination_name = 'MCP_RefImg'; task.automated = True; task.replace_existing = True; task.save = False\n"
    "    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])\n"
    "    outs = list(task.get_editor_property('imported_object_paths')) if task.get_editor_property('imported_object_paths') else []\n"
    "    if outs: reftex = unreal.load_asset(outs[0])\n"
    "comp = RL.create_render_target2d(W, tw*3, th+lab, unreal.TextureRenderTargetFormat.RTF_RGBA8)\n"
    "canvas, size, ctx = RL.begin_draw_canvas_to_render_target(W, comp)\n"
    "def fit(tex_w, tex_h):\n"
    "    s = min(float(tw)/max(tex_w,1), float(th)/max(tex_h,1)); return (tex_w*s), (tex_h*s)\n"
    "if reftex is not None:\n"
    "    rw, rh = fit(reftex.blueprint_get_size_x(), reftex.blueprint_get_size_y()); ox=(tw-rw)/2.0; oy=(th-rh)/2.0\n"
    "    canvas.draw_texture(reftex, unreal.Vector2D(ox,oy), unreal.Vector2D(rw,rh), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "canvas.draw_texture(cur, unreal.Vector2D(tw,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(1,1,1,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "if reftex is not None:\n"
    "    rw, rh = fit(reftex.blueprint_get_size_x(), reftex.blueprint_get_size_y()); ox=2*tw+(tw-rw)/2.0; oy=(th-rh)/2.0\n"
    "    canvas.draw_texture(reftex, unreal.Vector2D(ox,oy), unreal.Vector2D(rw,rh), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(0.5,0.5,0.5,1), unreal.BlendMode.BLEND_OPAQUE, 0.0, unreal.Vector2D(0,0))\n"
    "canvas.draw_texture(cur, unreal.Vector2D(2*tw,0), unreal.Vector2D(tw,th), unreal.Vector2D(0,0), unreal.Vector2D(1,1), unreal.LinearColor(0,1,0,1), unreal.BlendMode.BLEND_ADDITIVE, 0.0, unreal.Vector2D(0,0))\n"
    "for i,t in enumerate(['reference', 'current  f'+str(frame), 'overlay (ref dim + current green)']):\n"
    "    canvas.draw_text(FONT, t, unreal.Vector2D(i*tw+10, th+7), unreal.Vector2D(1,1), unreal.LinearColor(1,1,0,1))\n"
    "RL.end_draw_canvas_to_render_target(W, ctx)\n"
    "outdir, fname = outpath('compare_f' + str(frame) + '.png')\n"
    "RL.export_render_target(W, comp, outdir, fname)\n"
    "if reftex is not None:\n"
    "    try: unreal.EditorAssetLibrary.delete_asset('/Game/_MCP_Trash/MCP_RefImg')\n"
    "    except Exception: pass\n"
    "cleanup()\n"
    "p = os.path.join(outdir, fname).replace(chr(92), '/')\n"
    "print('@@UMCP@@' + json.dumps({'status':'success','image': p, 'exists': os.path.exists(os.path.join(outdir,fname)), 'reference_loaded': reftex is not None, 'camera': name, 'frame': frame, 'note': 'image-reference mode: side-by-side + current-over-reference overlay (visual). Per-joint pixel delta / IoU need a pose reference (ref_animation_path).'}))\n"
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

    def _cam_list(cams):
        out = []
        for name, spec in cams.items():
            if isinstance(spec, dict):
                out.append([name, list(spec.get("dir", [-0.9, 1.0, 0.35])), spec.get("proj", "ortho")])
            else:  # bare [dx,dy,dz]
                out.append([name, list(spec), "ortho"])
        return out

    @mcp.tool()
    def render_pose_multiview(ctx, animation_path: str, frame: int = 0, cameras: dict = None,
                              mesh_path: str = None, out_dir: str = None, out_name: str = None,
                              tile_w: int = 640, tile_h: int = 720, settle_seconds: float = 0.0) -> str:
        """Render N named cameras of an AnimSequence pose into ONE composited PNG (labels + manifest).

        The pose is BAKED onto a PoseableMeshComponent from seq.get_anim_pose_at_frame(frame) — so
        it is correct at ANY frame (a SkeletalMeshComponent renders only the reference pose in the
        editor). Each tile is orthographic and silhouette-auto-framed (tight + centered, pose-agnostic),
        lit by our own key/fill lights, isolated (show-only) on a clean matte-black background.

        animation_path: AnimSequence to render. frame: frame index. cameras: {name: [dx,dy,dz]} unit
        directions from the subject toward each camera (character faces +Y, up +Z); default =
        three_quarter_front + side_ortho. mesh_path: SkeletalMesh (SK_Mannequin has no preview mesh,
        so pass one, e.g. /Game/Characters/Mannequins/Meshes/SKM_Manny_Simple). tile_w/tile_h: per-tile
        pixels. Returns the composited PNG path (on the Windows editor host), an existence flag, and a
        tile-index -> camera manifest. settle_seconds is accepted for compatibility (unused — the bake
        is synchronous)."""
        cams = _cam_list(cameras or DEFAULT_CAMERAS)
        base = {"animation_path": animation_path, "frame": int(frame), "cameras": cams,
                "mesh_path": mesh_path, "out_dir": out_dir, "out_name": out_name,
                "tile_w": int(tile_w), "tile_h": int(tile_h)}
        return json.dumps(_run(_MULTIVIEW_BODY, base), indent=2)

    @mcp.tool()
    def compare_pose(ctx, animation_path: str, frame: int = 0, ref_image: str = None,
                     ref_animation_path: str = None, ref_frame: int = 0, camera: dict = None,
                     mesh_path: str = None, out_dir: str = None, out_name: str = None,
                     tile_w: int = 640, tile_h: int = 720, align: str = "none",
                     settle_seconds: float = 0.0) -> str:
        """Compare the CURRENT pose against a reference, rendered from the SAME camera (the
        load-bearing requirement). ONE image, three tiles: reference | current | overlay.

        Both poses are BAKED onto PoseableMeshComponents (correct at any frame). Reference is ONE of:
          - ref_animation_path (+ ref_frame): a POSE reference -> NUMERIC mode. The two subjects are
            co-located and share ONE auto-framed orthographic camera (so the overlay aligns). Returns
            per-joint screen-space pixel delta (dx/dy/px), per-limb angular error (deg), and silhouette
            IoU, plus an overlay with reference=MAGENTA / current=GREEN.
          - ref_image: an absolute image path -> VISUAL mode. reference | current | overlay (reference
            dimmed + current in green). Numeric N/A (a photo has no known joints).
        camera: {name, dir:[dx,dy,dz], proj:'ortho'} matching the reference's viewpoint (SK_Mannequin
        faces +Y; sides along X); default three_quarter_front. mesh_path optional (pass SKM_Manny_Simple
        for the mannequin skeleton). align (NUMERIC mode only): 'none' (default) keeps root motion /
        global position — faithful screen-space delta; 'pelvis' co-locates both pelvises so the compare
        is POSE-ONLY (limb configuration), which makes the pixel-delta + IoU + overlay ignore where the
        character is and focus on how it is posed. Angular error is always root-motion-independent.
        settle_seconds accepted for compatibility (unused)."""
        cam = camera or DEFAULT_CAMERAS["three_quarter_front"]
        if isinstance(cam, dict):
            cam_t = [cam.get("name", "camera"), list(cam.get("dir", [-0.9, 1.0, 0.35])), cam.get("proj", "ortho")]
        else:
            cam_t = ["camera", list(cam), "ortho"]

        if ref_animation_path:  # pose-vs-pose NUMERIC mode
            align_mode = align if align in ("none", "pelvis") else "none"
            return json.dumps(_run(_COMPARE_POSE_BODY, {
                "animation_path": animation_path, "frame": int(frame),
                "ref_animation_path": ref_animation_path, "ref_frame": int(ref_frame),
                "camera": cam_t, "keys": KEY_BONES, "mesh_path": mesh_path, "align": align_mode,
                "out_dir": out_dir, "out_name": out_name,
                "tile_w": int(tile_w), "tile_h": int(tile_h)}), indent=2)

        if not ref_image:
            return json.dumps({"status": "error",
                               "message": "pass ref_animation_path (numeric) or ref_image (visual)"}, indent=2)
        return json.dumps(_run(_COMPARE_IMG_BODY, {
            "animation_path": animation_path, "frame": int(frame), "camera": cam_t,
            "ref_image": ref_image, "mesh_path": mesh_path, "out_dir": out_dir, "out_name": out_name,
            "tile_w": int(tile_w), "tile_h": int(tile_h)}), indent=2)
