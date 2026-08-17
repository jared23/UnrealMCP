"""UserTools :: Debug Visualization  (debug-draw overlays)

Clean-room reimplementation over Unreal's public Python API (UE 5.8), driving
`unreal.SystemLibrary.draw_debug_*` against the EDITOR world obtained from
`unreal.UnrealEditorSubsystem.get_editor_world()`. These are transient viewport
OVERLAYS: they do not spawn actors, modify, or save the level.

Reversibility model (IMPORTANT):
  - Debug draws are EPHEMERAL. A positive `duration` self-expires after N seconds,
    so no undo is needed and NOTHING is written to the agent ledger for these.
  - A non-positive `duration` (<= 0) makes a PERSISTENT draw that stays until flushed.
    Use `clear_debug_draws()` to flush ALL persistent debug lines + strings in the
    editor world. Because a single flush clears EVERY persistent draw (the engine has
    no per-draw handle), we deliberately do NOT ledger persistent draws either — there
    is no faithful per-op inverse. The documented model is: ephemeral draws (default)
    un-ledgered + auto-expiring, and one non-selective `clear_debug_draws` for the
    persistent case. Nothing here participates in editor_level.undo.

Query convention: each snippet prints  @@UMCP@@<json>  on one line; _query() parses
after that marker. Params are injected as base64 JSON via _exec (survives the handler's
triple-single-quote wrapping). Output-Log warnings/errors are attached as _log_warnings.

Color convention: r,g,b,a are 0-255 integer channels (a defaults to 255). They are
normalized to an FLinearColor internally.

Implemented tools (all overlay-only, level untouched):
  - debug_draw_line, debug_draw_point, debug_draw_sphere, debug_draw_box,
    debug_draw_capsule, debug_draw_arrow, debug_draw_string,
    debug_draw_coordinate_system, debug_draw_circle, debug_draw_cone
  - clear_debug_draws  (flush ALL persistent debug lines + strings)
"""
import json
import base64
import textwrap

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
# No ''' and no stray backslashes in this code (the handler wraps code in '''...''').
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


# Shared draw body. Switches on PARAMS["shape"]; builds an FLinearColor from 0-255
# channels; targets the editor world; records actor count before/after so the caller
# can confirm no actor was spawned. A positive duration self-expires (ephemeral);
# duration <= 0 is persistent (cleared by clear_debug_draws). No ledger, no transaction:
# debug draws are overlays and do not modify or dirty the level.
_DRAW_BODY = r'''
import unreal, json
def _lc(rgba):
    return unreal.LinearColor(float(rgba[0]) / 255.0, float(rgba[1]) / 255.0, float(rgba[2]) / 255.0, float(rgba[3]) / 255.0)
def _v(t):
    return unreal.Vector(float(t[0]), float(t[1]), float(t[2]))
def _rot(t):
    return unreal.Rotator(pitch=float(t[0]), yaw=float(t[1]), roll=float(t[2]))
P = PARAMS
shape = P["shape"]
color = _lc(P.get("color") or [255, 255, 255, 255])
dur = float(P.get("duration", 5.0))
thick = float(P.get("thickness", 1.0))
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
n0 = len(eas.get_all_level_actors() or [])
SL = unreal.SystemLibrary
err = None
if world is None:
    err = "no editor world available"
else:
    try:
        if shape == "line":
            SL.draw_debug_line(world, _v(P["start"]), _v(P["end"]), color, dur, thick)
        elif shape == "point":
            SL.draw_debug_point(world, _v(P["location"]), float(P.get("size", 20.0)), color, dur)
        elif shape == "sphere":
            SL.draw_debug_sphere(world, _v(P["center"]), float(P.get("radius", 100.0)), int(P.get("segments", 12)), color, dur, thick)
        elif shape == "box":
            SL.draw_debug_box(world, _v(P["center"]), _v(P["extent"]), color, _rot(P.get("rotation") or [0, 0, 0]), dur, thick)
        elif shape == "capsule":
            SL.draw_debug_capsule(world, _v(P["center"]), float(P["half_height"]), float(P["radius"]), _rot(P.get("rotation") or [0, 0, 0]), color, dur, thick)
        elif shape == "arrow":
            SL.draw_debug_arrow(world, _v(P["start"]), _v(P["end"]), float(P.get("arrow_size", 50.0)), color, dur, thick)
        elif shape == "string":
            SL.draw_debug_string(world, _v(P["location"]), str(P["text"]), None, color, dur)
        elif shape == "coordinate_system":
            SL.draw_debug_coordinate_system(world, _v(P["location"]), _rot(P.get("rotation") or [0, 0, 0]), float(P.get("scale", 100.0)), dur, thick)
        elif shape == "circle":
            SL.draw_debug_circle(world, _v(P["center"]), float(P.get("radius", 100.0)), int(P.get("segments", 24)), color, dur, thick, unreal.Vector(0.0, 1.0, 0.0), unreal.Vector(0.0, 0.0, 1.0), bool(P.get("draw_axis", False)))
        elif shape == "cone":
            SL.draw_debug_cone_in_degrees(world, _v(P["origin"]), _v(P["direction"]), float(P.get("length", 100.0)), float(P.get("angle_width", 45.0)), float(P.get("angle_height", 45.0)), int(P.get("num_sides", 12)), color, dur, thick)
        else:
            err = "unknown shape: " + str(shape)
    except Exception as e:
        err = str(e)
n1 = len(eas.get_all_level_actors() or [])
result = {"status": "error" if err else "success", "shape": shape, "persistent": (dur <= 0.0),
          "duration": dur, "actor_count": n1, "actor_count_unchanged": (n0 == n1)}
if err:
    result["message"] = err
elif dur <= 0.0:
    result["note"] = "persistent overlay (duration<=0); call clear_debug_draws to remove. Level not modified/saved."
else:
    result["note"] = "ephemeral overlay; self-expires. Level not modified/saved."
print("@@UMCP@@" + json.dumps(result))
'''

_CLEAR_BODY = r'''
import unreal, json
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = ues.get_editor_world()
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
n0 = len(eas.get_all_level_actors() or [])
err = None
if world is None:
    err = "no editor world available"
else:
    try:
        unreal.SystemLibrary.flush_persistent_debug_lines(world)
        unreal.SystemLibrary.flush_debug_strings(world)
    except Exception as e:
        err = str(e)
n1 = len(eas.get_all_level_actors() or [])
result = {"status": "error" if err else "success",
          "cleared": "all persistent debug lines and strings in the editor world",
          "actor_count": n1, "actor_count_unchanged": (n0 == n1),
          "note": "Non-selective flush of ALL persistent debug draws. Ephemeral (timed) draws expire on their own. Level not modified/saved."}
if err:
    result["message"] = err
print("@@UMCP@@" + json.dumps(result))
'''


def register_tools(mcp, utils):
    send_command = utils["send_command"]

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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run
        the body in Unreal, and return its MARKER payload."""
        params = dict(params or {})
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    def _draw(params):
        try:
            return json.dumps(_exec(_DRAW_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # debug_draw_line                                                     #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_line(ctx, start: list, end: list,
                        r: int = 255, g: int = 0, b: int = 0, a: int = 255,
                        duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a debug line overlay in the editor viewport (transient; no actor spawned,
        level not modified/saved).

        start/end:  [x,y,z] world-space endpoints.
        r,g,b,a:    color channels 0-255 (a defaults to opaque).
        duration:   seconds before it auto-expires. Positive = ephemeral (no undo needed).
                    Use <= 0 for a PERSISTENT line (remove later via clear_debug_draws).
        thickness:  line thickness in pixels (0 = thin hairline)."""
        return _draw({"shape": "line", "start": start, "end": end,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_point                                                   #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_point(ctx, location: list, size: float = 20.0,
                         r: int = 255, g: int = 255, b: int = 0, a: int = 255,
                         duration: float = 5.0) -> str:
        """Draw a debug point (square sprite) overlay at a world location (transient;
        no actor spawned, level not modified/saved).

        location: [x,y,z] world position.
        size:     point size in pixels.
        r,g,b,a:  color channels 0-255.
        duration: seconds before auto-expiry; <= 0 = persistent (clear_debug_draws to remove)."""
        return _draw({"shape": "point", "location": location, "size": size,
                      "color": [r, g, b, a], "duration": duration})

    # ------------------------------------------------------------------ #
    # debug_draw_sphere                                                   #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_sphere(ctx, center: list, radius: float = 100.0, segments: int = 12,
                          r: int = 0, g: int = 255, b: int = 0, a: int = 255,
                          duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a wireframe debug sphere overlay (transient; no actor spawned, level untouched).

        center:   [x,y,z] world position.
        radius:   sphere radius (uu).
        segments: wireframe segment count (higher = smoother).
        r,g,b,a:  color channels 0-255.
        duration: seconds before auto-expiry; <= 0 = persistent.
        thickness: line thickness in pixels."""
        return _draw({"shape": "sphere", "center": center, "radius": radius, "segments": segments,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_box                                                     #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_box(ctx, center: list, extent: list, rotation: list = None,
                       r: int = 0, g: int = 128, b: int = 255, a: int = 255,
                       duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a wireframe debug box overlay (transient; no actor spawned, level untouched).

        center:   [x,y,z] world position of the box center.
        extent:   [x,y,z] half-extents (half-size along each axis).
        rotation: [pitch,yaw,roll] degrees (default none).
        r,g,b,a:  color channels 0-255.
        duration: seconds before auto-expiry; <= 0 = persistent.
        thickness: line thickness in pixels."""
        return _draw({"shape": "box", "center": center, "extent": extent, "rotation": rotation,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_capsule                                                 #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_capsule(ctx, center: list, half_height: float, radius: float,
                           rotation: list = None,
                           r: int = 255, g: int = 128, b: int = 0, a: int = 255,
                           duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a wireframe debug capsule overlay (transient; no actor spawned, level untouched).

        center:      [x,y,z] world position of the capsule center.
        half_height: half of the capsule's total height (uu).
        radius:      capsule radius (uu).
        rotation:    [pitch,yaw,roll] degrees (default none = upright).
        r,g,b,a:     color channels 0-255.
        duration:    seconds before auto-expiry; <= 0 = persistent.
        thickness:   line thickness in pixels."""
        return _draw({"shape": "capsule", "center": center, "half_height": half_height,
                      "radius": radius, "rotation": rotation,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_arrow                                                   #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_arrow(ctx, start: list, end: list, arrow_size: float = 50.0,
                         r: int = 255, g: int = 0, b: int = 255, a: int = 255,
                         duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a debug arrow overlay from start to end with an arrowhead at end (transient;
        no actor spawned, level untouched).

        start/end:  [x,y,z] world-space tail and head positions.
        arrow_size: size of the arrowhead (uu).
        r,g,b,a:    color channels 0-255.
        duration:   seconds before auto-expiry; <= 0 = persistent.
        thickness:  line thickness in pixels."""
        return _draw({"shape": "arrow", "start": start, "end": end, "arrow_size": arrow_size,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_string                                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_string(ctx, location: list, text: str,
                          r: int = 255, g: int = 255, b: int = 255, a: int = 255,
                          duration: float = 5.0) -> str:
        """Draw a floating debug text string in the world at a location (transient overlay;
        no actor spawned, level untouched).

        location: [x,y,z] world position where the text is anchored.
        text:     the string to display.
        r,g,b,a:  text color channels 0-255.
        duration: seconds before auto-expiry; <= 0 = persistent (remove via clear_debug_draws,
                  which also flushes debug strings). Note: debug strings have no thickness."""
        return _draw({"shape": "string", "location": location, "text": text,
                      "color": [r, g, b, a], "duration": duration})

    # ------------------------------------------------------------------ #
    # debug_draw_coordinate_system                                       #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_coordinate_system(ctx, location: list, rotation: list = None,
                                     scale: float = 100.0,
                                     duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a debug XYZ coordinate-system gizmo (red=X, green=Y, blue=Z axes) at a
        transform (transient overlay; no actor spawned, level untouched). Axis colors are
        fixed by the engine.

        location: [x,y,z] world position of the origin.
        rotation: [pitch,yaw,roll] degrees orienting the axes (default none).
        scale:    axis length (uu).
        duration: seconds before auto-expiry; <= 0 = persistent.
        thickness: line thickness in pixels."""
        return _draw({"shape": "coordinate_system", "location": location, "rotation": rotation,
                      "scale": scale, "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_circle                                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_circle(ctx, center: list, radius: float = 100.0, segments: int = 24,
                          draw_axis: bool = False,
                          r: int = 255, g: int = 255, b: int = 255, a: int = 255,
                          duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a wireframe debug circle overlay in the world XY plane (transient; no actor
        spawned, level untouched).

        center:    [x,y,z] world position of the circle center.
        radius:    circle radius (uu).
        segments:  segment count (higher = smoother).
        draw_axis: also draw the circle's local axes.
        r,g,b,a:   color channels 0-255.
        duration:  seconds before auto-expiry; <= 0 = persistent.
        thickness: line thickness in pixels."""
        return _draw({"shape": "circle", "center": center, "radius": radius, "segments": segments,
                      "draw_axis": draw_axis, "color": [r, g, b, a],
                      "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # debug_draw_cone                                                    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def debug_draw_cone(ctx, origin: list, direction: list, length: float = 100.0,
                        angle_width: float = 45.0, angle_height: float = 45.0, num_sides: int = 12,
                        r: int = 255, g: int = 255, b: int = 0, a: int = 255,
                        duration: float = 5.0, thickness: float = 1.0) -> str:
        """Draw a wireframe debug cone overlay (apex at origin, opening along direction),
        angles in DEGREES (transient; no actor spawned, level untouched).

        origin:       [x,y,z] apex position.
        direction:    [x,y,z] direction vector the cone opens toward.
        length:       cone length (uu).
        angle_width:  half-angle width in degrees.
        angle_height: half-angle height in degrees.
        num_sides:    wireframe side count.
        r,g,b,a:      color channels 0-255.
        duration:     seconds before auto-expiry; <= 0 = persistent.
        thickness:    line thickness in pixels."""
        return _draw({"shape": "cone", "origin": origin, "direction": direction, "length": length,
                      "angle_width": angle_width, "angle_height": angle_height, "num_sides": num_sides,
                      "color": [r, g, b, a], "duration": duration, "thickness": thickness})

    # ------------------------------------------------------------------ #
    # clear_debug_draws                                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def clear_debug_draws(ctx) -> str:
        """Flush ALL persistent debug draws (lines/shapes drawn with duration <= 0) and all
        debug strings from the editor world. Non-selective: there is no per-draw handle, so
        this clears every persistent overlay at once. Ephemeral (timed) draws expire on their
        own and are unaffected. Does not spawn/destroy actors or modify/save the level."""
        try:
            return json.dumps(_exec(_CLEAR_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"
