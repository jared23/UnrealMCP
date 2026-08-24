"""UserTools :: Control Rig editor PREVIEW-animation play/stop (C++ #49).

play_rig_preview_animation / stop_rig_preview_animation / get_rig_preview_state.

The CR editor's preview mesh is a UDebugSkelMeshComponent in an EditorPreview world; its UAnimPreviewInstance
drives single-node playback. The C++ handlers (MCPReflectionLibrary.*_rig_preview_*_json) find that component by
TObjectIterator and drive playback -- no private FControlRigEditor toolkit. The CR editor opens headless in this
build. Each tool is resolve-guarded (INERT until the DLL lands, then AUTO-ENABLES).

play <-> stop is a natural NON-LEDGERED runtime inverse pair (like PCG generate/cleanup) -- no editor_level.undo
fold. To make the preview exist, the rig's asset editor must be OPEN; play auto-opens it (a separate call first, so
the editor gets a tick to build its preview scene before play looks for the component).
"""
import json
import base64
import textwrap
import os

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
    "            if _ww: print('@@UMCP_LOG@@'+_jj.dumps(_ww[-40:]))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _wrap(code):
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER


# Resolve the reflected snake_case handler robustly (the "PCG"/acronym snake-casing is exact here, but keep it safe).
_HELP = r'''
import unreal, json
def _mrl(cpp_name):
    m = getattr(unreal, "MCPReflectionLibrary", None)
    if m is None:
        return None, None
    import re
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', cpp_name).lower()
    for cand in (snake, cpp_name):
        if hasattr(m, cand):
            return m, cand
    return m, None
def _norm(p):
    return p.split(".")[0] if p else p
def _open_rig(rig_path):
    rig = unreal.load_asset(rig_path)
    if rig is None:
        return None, None, False, "could not load Control Rig: %s" % rig_path
    L = getattr(unreal, "ControlRigBlueprintLibrary", None)
    openp = []
    if L is not None:
        try:
            openp = [_norm(b.get_path_name()) for b in (L.get_currently_open_rig_blueprints() or [])]
        except Exception:
            openp = []
    was_open = _norm(rig_path) in openp
    pm = ""
    if L is not None:
        try:
            m = L.get_preview_mesh(rig)
            pm = m.get_path_name() if m is not None else ""
        except Exception:
            pm = ""
    return rig, pm, was_open, None
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

    # Ensure the CR editor is open (so its preview scene exists). Returns the preview mesh path.
    _OPEN_BODY = _HELP + r'''
rig, pm, was_open, err = _open_rig(PARAMS["rig_path"])
if err is not None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    opened = False
    if not was_open:
        try:
            unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).open_editor_for_assets([rig])
            opened = True
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "open_editor failed: %s" % _e}))
            rig = None
    if rig is not None:
        print("@@UMCP@@" + json.dumps({"status": "success", "was_open": was_open, "opened": opened,
            "preview_mesh": pm, "rig": PARAMS["rig_path"]}))
'''

    _PLAY_BODY = _HELP + r'''
rl, fn = _mrl("PlayRigPreviewAnimationJson")
pm = PARAMS.get("preview_mesh") or ""
if rl is None or fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True,
        "message": "PlayRigPreviewAnimationJson not built yet (rebuild the plugin)"}))
else:
    raw = getattr(rl, fn)(pm, PARAMS["anim_path"], float(PARAMS.get("play_rate", 1.0)), bool(PARAMS.get("looping", True)))
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _STOP_BODY = _HELP + r'''
rl, fn = _mrl("StopRigPreviewAnimationJson")
pm = PARAMS.get("preview_mesh") or ""
if rl is None or fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True,
        "message": "StopRigPreviewAnimationJson not built yet (rebuild the plugin)"}))
else:
    raw = getattr(rl, fn)(pm)
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    _STATE_BODY = _HELP + r'''
rl, fn = _mrl("GetRigPreviewStateJson")
pm = PARAMS.get("preview_mesh") or ""
if rl is None or fn is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "deferred": True,
        "message": "GetRigPreviewStateJson not built yet (rebuild the plugin)"}))
else:
    raw = getattr(rl, fn)(pm)
    try:
        res = json.loads(raw)
    except Exception:
        res = {"raw": raw}
    if isinstance(res, dict) and res.get("error"):
        res = {"status": "error", "message": res.get("error")}
    print("@@UMCP@@" + json.dumps(res))
'''

    def _resolve_mesh(rig_path):
        """Open the rig editor if needed (separate call -> editor gets a tick), return preview mesh path."""
        o = _exec(_OPEN_BODY, {"rig_path": rig_path})
        if o.get("status") != "success":
            return None, o
        return (o.get("preview_mesh") or ""), o

    @mcp.tool()
    def play_rig_preview_animation(ctx, rig_path: str, anim_path: str, play_rate: float = 1.0,
                                   looping: bool = True) -> str:
        """Play a preview animation on a Control Rig's editor preview mesh.

        Opens the Control Rig asset editor if it isn't already open (its preview scene is where the
        preview mesh lives), then plays `anim_path` on that preview mesh via its UAnimPreviewInstance.
        Runtime playback (NOT an asset mutation) -> NOT ledgered; the inverse is stop_rig_preview_animation.

        rig_path:   the Control Rig blueprint (e.g. /Game/.../CR_Foo).
        anim_path:  a UAnimSequence/animation asset compatible with the rig's preview-mesh skeleton.
        play_rate:  playback speed (default 1.0).
        looping:    loop the animation (default True)."""
        try:
            pm, o = _resolve_mesh(rig_path)
            if pm is None:
                return json.dumps(o, indent=2)
            res = _exec(_PLAY_BODY, {"preview_mesh": pm, "anim_path": anim_path,
                                     "play_rate": play_rate, "looping": looping})
            if isinstance(res, dict):
                res["rig"] = rig_path
                res["editor_was_open"] = o.get("was_open")
            return json.dumps(res, indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def stop_rig_preview_animation(ctx, rig_path: str) -> str:
        """Stop (pause) the Control Rig editor preview playback. Inverse of play_rig_preview_animation.
        Requires the rig's editor to be open (no-op error if no preview component is found).

        rig_path: the Control Rig blueprint whose preview to stop."""
        try:
            pm, o = _resolve_mesh(rig_path)
            if pm is None:
                return json.dumps(o, indent=2)
            res = _exec(_STOP_BODY, {"preview_mesh": pm})
            if isinstance(res, dict):
                res["rig"] = rig_path
            return json.dumps(res, indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def get_rig_preview_state(ctx, rig_path: str) -> str:
        """Read the Control Rig editor preview playback state (is_playing, current_anim, position,
        play_rate, looping). Read-only. Requires the rig's editor to be open.

        rig_path: the Control Rig blueprint to inspect."""
        try:
            pm, o = _resolve_mesh(rig_path)
            if pm is None:
                return json.dumps(o, indent=2)
            res = _exec(_STATE_BODY, {"preview_mesh": pm})
            if isinstance(res, dict):
                res["rig"] = rig_path
            return json.dumps(res, indent=2)
        except Exception as e:
            return f"Error: {e}"
