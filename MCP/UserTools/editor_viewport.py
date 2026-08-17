"""UserTools :: Editor / Viewport  (spec: docs/spec/editor.md — "Viewport / camera / screenshots")

Clean-room reimplementation over Unreal's public Python API (subsystem-based, UE 5.8).
Commands run in the editor's interpreter via the plugin's `execute_python` handler, which
returns captured stdout under result["output"] (CRLF).

Query convention: a snippet prints  @@UMCP@@<json>  on one line; _query() finds that
marker and parses the JSON after it, so stray engine log lines can't corrupt it.

Camera API: uses the UE 5.8 subsystem calls
  unreal.UnrealEditorSubsystem.get_level_viewport_camera_info()  -> (Vector, Rotator)
  unreal.UnrealEditorSubsystem.set_level_viewport_camera_info(location, rotation)
The deprecated EditorLevelLibrary.* viewport-camera calls are avoided; snippets run with
warnings.simplefilter("ignore") so any residual deprecation noise stays off stdout.

Write policy — NO LEDGER: the viewport camera is EPHEMERAL editor state (a view, not level
content). set_viewport_camera / focus_viewport therefore do NOT push undo-ledger entries and
this module defines no `undo` tool. take_screenshot is a pure capture: it briefly spawns an
off-screen SceneCapture2D at the viewport-camera pose, exports one frame to PNG, and destroys
the capture actor in the same call, so the level is left exactly as found.

Screenshot mechanism: AutomationLibrary.take_high_res_screenshot / the `HighResShot` console
command both target the *game* viewport, which does not exist in a plain editor session (no
PIE) and silently never render — validated: nothing was ever written. Instead we capture
off-screen via a transient SceneCaptureComponent2D -> TextureRenderTarget2D and
RenderingLibrary.export_render_target(), which renders and writes the PNG SYNCHRONOUSLY (no
dependence on the interactive viewport redrawing). The returned path is on the Windows box
(the Mac can't read it); returning the PATH is the deliverable. FUTURE ENHANCEMENT: stream the
PNG bytes back base64-encoded so the caller can view the image directly.

Implemented:
  - get_viewport_camera  (read-only)
  - set_viewport_camera  (write; ephemeral, NOT ledgered; partial update)
  - focus_viewport       (write; ephemeral, NOT ledgered; frame an actor)
  - take_screenshot      (capture editor viewport -> PNG on the Windows box)
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied from editor_level.py) --------------------
# Wraps every snippet so it records the editor .log size before running and, in a
# finally, flushes + reads the appended bytes, surfacing new Warning/Error lines as
# @@UMCP_LOG@@ (attached to the result as "_log_warnings").
# NB: no ''' and no stray backslashes in this code (the handler wraps code in '''...''').
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes ('''...''')
# before exec, so any ''' or backslash in the code corrupts it. Pass all parameters as base64
# (its alphabet has no quote/backslash/triple-quote). Snippet bodies also avoid ''' / backslashes.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS process. Nothing here is ledgered, but we keep the same
    # session resolution + _exec injection as the other modules for consistency.
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        """Run a snippet in Unreal (with Output-Log auto-capture) and parse its MARKER
        payload. Any new Warning/Error log lines are attached as result['_log_warnings']."""
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run the
        body in Unreal, and return its MARKER payload. Adds _session for consistency."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helper (prepended to bodies that resolve actors). Copied verbatim
    # from editor_level.py's _COERCE_HELPERS _resolve_actor: label first, then unique name.
    # No ''' / no backslashes.
    _RESOLVE_HELPER = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _resolve_actor(ident):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = eas.get_all_level_actors() or []
    for a in actors:
        if a and a.get_actor_label() == ident:
            return a
    for a in actors:
        if a and a.get_name() == ident:
            return a
    return None
'''

    # ------------------------------------------------------------------ #
    # get_viewport_camera — read the editor perspective camera pose       #
    # ------------------------------------------------------------------ #
    _GET_CAM_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
loc, rot = ues.get_level_viewport_camera_info()
print("@@UMCP@@" + json.dumps({"status": "success",
    "location": [loc.x, loc.y, loc.z],
    "rotation": [rot.pitch, rot.yaw, rot.roll]}))
'''

    @mcp.tool()
    def get_viewport_camera(ctx) -> str:
        """Get the active editor perspective viewport camera pose. Read-only.

        Returns location [x, y, z] (world units) and rotation [pitch, yaw, roll] (degrees)
        of the level-editor camera (unreal.UnrealEditorSubsystem.get_level_viewport_camera_info)."""
        try:
            return json.dumps(_query(_GET_CAM_BODY), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_viewport_camera — move the editor camera (ephemeral, NOT ledgered) #
    # ------------------------------------------------------------------ #
    _SET_CAM_BODY = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
loc = PARAMS.get("location")
rot = PARAMS.get("rotation")
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
cur_loc, cur_rot = ues.get_level_viewport_camera_info()
before = {"location": [cur_loc.x, cur_loc.y, cur_loc.z],
          "rotation": [cur_rot.pitch, cur_rot.yaw, cur_rot.roll]}
new_loc = cur_loc
new_rot = cur_rot
if loc is not None:
    new_loc = unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2]))
if rot is not None:
    new_rot = unreal.Rotator(pitch=float(rot[0]), yaw=float(rot[1]), roll=float(rot[2]))
ues.set_level_viewport_camera_info(new_loc, new_rot)
a_loc, a_rot = ues.get_level_viewport_camera_info()
print("@@UMCP@@" + json.dumps({"status": "success", "ledgered": False,
    "before": before,
    "after": {"location": [a_loc.x, a_loc.y, a_loc.z],
              "rotation": [a_rot.pitch, a_rot.yaw, a_rot.roll]}}))
'''

    @mcp.tool()
    def set_viewport_camera(ctx, location: list = None, rotation: list = None) -> str:
        """Move the editor perspective viewport camera. Partial update — pass either or both.

        location: [x, y, z] world position (kept as-is if omitted).
        rotation: [pitch, yaw, roll] in degrees (kept as-is if omitted).

        The viewport camera is EPHEMERAL editor state (a view, not level content), so this
        write is NOT recorded on the undo ledger and there is no `undo` for it. Returns the
        camera pose 'before' and 'after' so callers can restore it themselves if desired."""
        params = {"location": location, "rotation": rotation}
        try:
            return json.dumps(_exec(_SET_CAM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # focus_viewport — frame an actor in the viewport (ephemeral)          #
    # ------------------------------------------------------------------ #
    _FOCUS_BODY = _RESOLVE_HELPER + r'''
_DIRS = {
    "iso": (-1.0, -1.0, 1.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "front": (-1.0, 0.0, 0.35),
    "back": (1.0, 0.0, 0.35),
    "left": (0.0, -1.0, 0.35),
    "right": (0.0, 1.0, 0.35),
}
ident = PARAMS.get("actor")
direction = (PARAMS.get("direction") or "iso").lower()
req_distance = PARAMS.get("distance")
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
cur_loc, cur_rot = ues.get_level_viewport_camera_info()
before = {"location": [cur_loc.x, cur_loc.y, cur_loc.z],
          "rotation": [cur_rot.pitch, cur_rot.yaw, cur_rot.roll]}
if not ident:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor is required"}))
else:
    target = _resolve_actor(ident)
    if target is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % ident}))
    else:
        origin = target.get_actor_location()
        radius = 100.0
        try:
            o, ext = target.get_actor_bounds(False)
            origin = o
            radius = max(ext.x, ext.y, ext.z)
        except Exception:
            pass
        if radius <= 1.0:
            radius = 100.0
        distance = float(req_distance) if req_distance is not None else radius * 3.0
        d = _DIRS.get(direction, _DIRS["iso"])
        dv = unreal.Vector(d[0], d[1], d[2]).normal()
        cam = unreal.Vector(origin.x + dv.x * distance,
                            origin.y + dv.y * distance,
                            origin.z + dv.z * distance)
        look = unreal.MathLibrary.find_look_at_rotation(cam, origin)
        ues.set_level_viewport_camera_info(cam, look)
        a_loc, a_rot = ues.get_level_viewport_camera_info()
        print("@@UMCP@@" + json.dumps({"status": "success", "ledgered": False,
            "actor": target.get_actor_label(), "name": target.get_name(),
            "direction": direction,
            "focus_origin": [origin.x, origin.y, origin.z],
            "bounds_radius": radius, "distance": distance,
            "before": before,
            "after": {"location": [a_loc.x, a_loc.y, a_loc.z],
                      "rotation": [a_rot.pitch, a_rot.yaw, a_rot.roll]}}))
'''

    @mcp.tool()
    def focus_viewport(ctx, actor: str = None, direction: str = "iso",
                       distance: float = None) -> str:
        """Frame an actor in the editor viewport by moving the camera to look at it.

        actor:     actor display label (preferred) or unique internal name (required).
        direction: which side to view from — one of iso (default), top, bottom, front,
                   back, left, right.
        distance:  camera distance back from the actor (world units). Default is 3x the
                   actor's bounds radius, so the actor comfortably fills the frame.

        Computes the actor's world-space bounds, positions the camera `distance` back along
        the chosen direction, and aims it at the actor's center. EPHEMERAL: not ledgered;
        'before'/'after' poses are returned so the caller can restore the prior view."""
        params = {"actor": actor, "direction": direction, "distance": distance}
        try:
            return json.dumps(_exec(_FOCUS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # take_screenshot — off-screen SceneCapture2D -> PNG on Windows box    #
    # ------------------------------------------------------------------ #
    _SHOT_BODY = r'''
import unreal, json, os, time, warnings
warnings.simplefilter("ignore")
BSLASH = chr(92)
def _slash(p):
    return p.replace(BSLASH, "/")
res = PARAMS.get("resolution")
if isinstance(res, (list, tuple)) and len(res) >= 2:
    width, height = int(res[0]), int(res[1])
elif isinstance(res, (int, float)):
    width = int(res); height = int(res)
else:
    width, height = 1280, 720
fov = float(PARAMS.get("fov") or 90.0)
saved = _slash(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())).rstrip("/")
file_path = PARAMS.get("file_path")
if file_path:
    file_path = _slash(file_path)
    outdir = os.path.dirname(file_path) or (saved + "/Screenshots/MCP")
    fname = os.path.basename(file_path)
else:
    outdir = saved + "/Screenshots/MCP"
    fname = "mcp_screenshot_" + str(int(time.time())) + ".png"
if not fname.lower().endswith(".png"):
    fname = fname + ".png"
try:
    os.makedirs(outdir, exist_ok=True)
except Exception:
    pass
full = outdir + "/" + fname
try:
    if os.path.exists(full):
        os.remove(full)
except Exception:
    pass
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = ues.get_editor_world()
loc, rot = ues.get_level_viewport_camera_info()
err = None
cap = None
rt = None
try:
    rt = unreal.RenderingLibrary.create_render_target2d(world, width, height, unreal.TextureRenderTargetFormat.RTF_RGBA8)
    cap = eas.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
    comp = cap.get_editor_property("capture_component2d")
    comp.set_editor_property("texture_target", rt)
    comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    comp.set_editor_property("fov_angle", fov)
    comp.set_editor_property("capture_every_frame", False)
    comp.set_editor_property("capture_on_movement", False)
    comp.capture_scene()
    unreal.RenderingLibrary.export_render_target(world, rt, outdir, fname)
except Exception as e:
    err = str(e)
try:
    if cap is not None:
        eas.destroy_actor(cap)
except Exception:
    pass
exists = os.path.exists(full)
size = os.path.getsize(full) if exists else 0
result = {"status": ("success" if exists and not err else "error"),
    "file_path": full, "resolution": [width, height], "fov": fov,
    "exists": exists, "size_bytes": size,
    "method": "SceneCapture2D off-screen (synchronous export_render_target)",
    "note": "Path is on the Windows editor host; the Mac cannot read it. base64 image return is a future enhancement."}
if err:
    result["error"] = err
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def take_screenshot(ctx, file_path: str = None, resolution: list = None) -> str:
        """Capture the editor viewport to a PNG on the Windows editor host.

        file_path:  absolute output path on the Windows box (default:
                    <Project>/Saved/Screenshots/MCP/mcp_screenshot_<epoch>.png). A '.png'
                    extension is enforced.
        resolution: [width, height] in pixels (default [1280, 720]); a single int is treated
                    as a square.

        Renders off-screen via a transient SceneCapture2D placed at the current viewport-camera
        pose and exports one frame synchronously (the plain editor has no game viewport, so
        HighResShot/take_high_res_screenshot never render). The capture actor is destroyed in
        the same call, so the level is left unchanged (no ledger entry).

        Returns the absolute file path written and whether the file now exists on disk. NOTE:
        the Mac cannot read Windows files, so the PATH is the deliverable; streaming the image
        back as base64 is a future enhancement."""
        params = {"file_path": file_path, "resolution": resolution}
        try:
            return json.dumps(_exec(_SHOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
