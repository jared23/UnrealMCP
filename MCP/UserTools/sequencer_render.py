"""UserTools :: Sequencer RENDER (render_sequence / render_status) — the last sequencer features.

Uses the LEGACY, Python-scriptable render path (unreal.SequencerTools.render_movie /
is_rendering_movie / cancel_movie_render + unreal.AutomatedLevelSequenceCapture) — the modern
MovieRenderPipeline plugin is NOT enabled in this project. render_status is a clean query that works
anywhere; render_sequence SETS UP + submits an AutomatedLevelSequenceCapture. NOTE: actual frame output
needs a GPU/RHI — a headless `-nullrhi` editor cannot produce frames (the tool submits the job either way).

Scaffolding mirrors the sequencer modules: base64 PARAMS, @@UMCP@@ marker, Output-Log capture.
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


def register_tools(mcp, utils):
    send_command = utils["send_command"]

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
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    _STATUS_BODY = r'''
import unreal, json
st = unreal.SequencerTools
print("@@UMCP@@" + json.dumps({"status": "success",
    "is_rendering": bool(st.is_rendering_movie()),
    "note": "render_status via unreal.SequencerTools.is_rendering_movie()"}))
'''

    _RENDER_BODY = r'''
import unreal, json
seq_path = PARAMS["sequence_path"]
out_dir = PARAMS.get("output_directory") or unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir() + "MovieRenders/")
fmt = (PARAMS.get("image_format") or "jpg").lower()
res_x = int(PARAMS.get("resolution_x") or 1280); res_y = int(PARAMS.get("resolution_y") or 720)
out_name = PARAMS.get("output_name") or "{sequence}.{frame}"

seq = unreal.load_asset(seq_path)
if seq is None or not isinstance(seq, unreal.LevelSequence):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a LevelSequence: %s" % seq_path}))
elif unreal.SequencerTools.is_rendering_movie():
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "a movie render is already in progress (see render_status / cancel_movie_render)"}))
else:
    cap = unreal.AutomatedLevelSequenceCapture()
    cap.level_sequence_asset = unreal.SoftObjectPath(seq.get_path_name())
    # output settings
    s = cap.get_editor_property("settings")
    s.set_editor_property("output_directory", unreal.DirectoryPath(out_dir))
    s.set_editor_property("output_format", out_name)
    s.set_editor_property("overwrite_existing", True)
    try:
        s.set_editor_property("resolution", unreal.CaptureResolution(res_x, res_y))
    except Exception:
        pass
    # image-sequence protocol (format-specific classes; getattr so a missing name never raises)
    _protoname = {"png": "ImageSequenceProtocol_PNG", "jpg": "ImageSequenceProtocol_JPG",
                  "jpeg": "ImageSequenceProtocol_JPG", "bmp": "ImageSequenceProtocol_BMP"}.get(fmt)
    proto = getattr(unreal, _protoname, None) if _protoname else None
    try:
        if proto is not None:
            cap.set_image_capture_protocol_type(proto)
    except Exception:
        pass
    submitted = False; err = None
    try:
        cb = unreal.OnRenderMovieStopped()
        unreal.SequencerTools.render_movie(cap, cb)
        submitted = True
    except Exception as e:
        err = str(e)[:200]
    print("@@UMCP@@" + json.dumps({"status": "success" if submitted else "error",
        "sequence": seq.get_name(), "output_directory": out_dir, "image_format": fmt,
        "resolution": [res_x, res_y], "submitted": submitted,
        "is_rendering": bool(unreal.SequencerTools.is_rendering_movie()),
        "error": err,
        "note": "legacy SequencerTools.render_movie submitted; actual frame OUTPUT requires a GPU/RHI (a headless -nullrhi editor renders no frames). Poll render_status; cancel via cancel_movie_render."}))
'''

    @mcp.tool()
    def render_status(ctx) -> str:
        """Is a Sequencer movie render currently in progress? (unreal.SequencerTools.is_rendering_movie).
        Read-only. Returns {is_rendering}."""
        try:
            return json.dumps(_exec(_STATUS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def render_sequence(ctx, sequence_path: str, output_directory: str = "", image_format: str = "jpg",
                        resolution_x: int = 1280, resolution_y: int = 720, output_name: str = "") -> str:
        """Render a LevelSequence to an image sequence via the legacy AutomatedLevelSequenceCapture +
        SequencerTools.render_movie. Submits the render job.

        sequence_path: a /Game LevelSequence asset. output_directory: absolute dir (default Saved/MovieRenders).
        image_format: png|jpg|bmp. resolution_x/y: default 1280x720. output_name: file pattern (default
        '{sequence}.{frame}').
        NOTE: actual frames need a GPU — a headless -nullrhi editor submits the job but renders nothing. Poll
        render_status to watch progress."""
        try:
            return json.dumps(_exec(_RENDER_BODY, {"sequence_path": sequence_path,
                "output_directory": output_directory, "image_format": image_format,
                "resolution_x": resolution_x, "resolution_y": resolution_y, "output_name": output_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"
