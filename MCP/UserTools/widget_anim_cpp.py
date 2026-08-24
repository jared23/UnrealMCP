"""UserTools :: UMG WIDGET ANIMATIONS authoring (spec: widgets "W-C" batch)

DRAFT wiring for the C++ #38 widget-animation round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_WidgetAnim.cpp. Every tool here is
hasattr-guarded on a future unreal.MCPReflectionLibrary method, so this module is INERT until the plugin
DLL is rebuilt with those handlers -- at which point each tool AUTO-ENABLES. Scaffolding (query
convention, base64 PARAMS injection, Output-Log auto-capture, per-session undo ledger) is copied
VERBATIM from the gold-standard niagara_runtime_cpp.py / widget_edit_cpp.py.

Fills the spec features that stock Python cannot reach: UWidgetAnimation lives under
UWidgetBlueprint::Animations (a protected-in-spirit editor-only TArray) and its MovieScene possessable /
FWidgetAnimationBinding / MovieScene tracks + channels are not python-reachable -- they live in
MovieScene / MovieSceneTracks / UMG C++.

  READS (no ledger -- non-mutating):
    * list_widget_animations  -- MCPReflectionLibrary.list_widget_animations_json(asset_path): every
                                 animation {name, display_label, duration, binding_count, track_count}.
    * list_animation_tracks   -- list_animation_tracks_json(asset_path, anim_name): bindings -> tracks ->
                                 sections -> channels (type + num_keys).

  WRITES (ledger -- reversible; inverse folds into editor_level.undo):
    * create_widget_animation         -- new UWidgetAnimation + its UMovieScene. Inverse: remove it.
    * remove_widget_animation         -- drop from Animations. Inverse: best-effort re-create (LOSSY:
                                         tracks/bindings/keys are NOT restored).
    * add_animation_widget_binding    -- possess a widget: AddPossessable -> FGuid, FWidgetAnimationBinding
                                         with AnimationGuid == that GUID. Inverse: remove the binding.
    * remove_animation_widget_binding -- RemovePossessable (cascades tracks) + drop the binding. Inverse:
                                         re-add the binding.
    * add_animation_track             -- a property track (opacity|color|transform|visibility) bound to the
                                         widget's possessable GUID. Inverse: remove the track.
    * add_animation_key               -- find/add a section + key one channel at TimeSeconds. Inverse:
                                         remove the key at `frame` (or restore prev_value if had_key).

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). Each write's
inverse is appended to the shared per-session ledger (builtins._UMCP_LEDGERS[session]) with ledger key
`asset_path`, for editor_level.undo to fold. The `op` names are wanim_* so the coordinator wires the
matching inverse cases into editor_level.undo.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a
snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/
user_code/code_obj (the C++ wrapper's own names).
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
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
    return {"status": "error", "error": (fn + " requires the C++ widget-animation handler "
            "(deferred to a batched C++ round). Rebuild the UnrealMCP plugin DLL with "
            "MCPReflection_WidgetAnim.cpp (adds MovieScene + MovieSceneTracks Build.cs deps) to enable it.")}
'''

    # ================================================================== #
    # READS (no ledger). Each is hasattr-guarded -> inert until the DLL lands.
    # ================================================================== #

    _LIST_ANIMS_BODY = _HELP + r'''
rl = _mrl("list_widget_animations_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_widget_animations")))
else:
    res = _decode(rl.list_widget_animations_json(PARAMS["asset_path"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_widget_animations(ctx, asset_path: str) -> str:
        """List every UWidgetAnimation on a Widget Blueprint.

        asset_path: Widget Blueprint asset path (e.g. '/Game/UI/WBP_Menu.WBP_Menu').

        Returns {blueprint, animation_count, animations:[{name, display_label, duration, binding_count,
        track_count, possessable_count}]}. UMG/MovieScene-only; needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path}
        try:
            return json.dumps(_exec(_LIST_ANIMS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _LIST_TRACKS_BODY = _HELP + r'''
rl = _mrl("list_animation_tracks_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("list_animation_tracks")))
else:
    res = _decode(rl.list_animation_tracks_json(PARAMS["asset_path"], PARAMS["anim_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
gc.collect()
'''

    @mcp.tool()
    def list_animation_tracks(ctx, asset_path: str, anim_name: str) -> str:
        """List an animation's bindings -> tracks -> sections -> channels.

        asset_path: Widget Blueprint asset path.
        anim_name:  animation object name or display label.

        Returns {blueprint, anim_name, display_label, binding_count, possessable_count, bindings:[{
        animation_guid, widget, display_name, track_count, tracks:[{track_class, property_name,
        section_count, sections:[{start_frame, end_frame, channel_count, channels:[{channel_type, index,
        num_keys}]}]}]}]}. UMG/MovieScene-only; needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name}
        try:
            return json.dumps(_exec(_LIST_TRACKS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # WRITES (ledger -- reversible). Inverse folds into editor_level.undo.
    # ================================================================== #

    _CREATE_BODY = _HELP + r'''
rl = _mrl("create_widget_animation_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("create_widget_animation")))
else:
    res = _decode(rl.create_widget_animation_json(PARAMS["asset_path"], PARAMS["anim_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "wanim_create_animation", "asset_path": PARAMS["asset_path"],
            "anim_name": res.get("anim_name", PARAMS["anim_name"])})
        out = {"status": "success", "anim_name": res.get("anim_name"),
               "display_label": res.get("display_label"), "created": bool(res.get("created")),
               "animation_count": res.get("animation_count"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def create_widget_animation(ctx, asset_path: str, anim_name: str) -> str:
        """Create a new UWidgetAnimation (with its UMovieScene) on a Widget Blueprint.

        asset_path: Widget Blueprint asset path.
        anim_name:  name for the new animation (must be unique on the blueprint).

        Creates NewObject<UWidgetAnimation> + NewObject<UMovieScene>, sets a 20fps display rate and a
        1-second default playback range, appends to WidgetBlueprint->Animations, and registers the variable
        GUID. Ledgered inverse = remove the animation. Returns {anim_name, display_label, created,
        animation_count, ledger_depth}. UMG/MovieScene-only; needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_ANIM_BODY = _HELP + r'''
rl = _mrl("remove_widget_animation_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_widget_animation")))
else:
    res = _decode(rl.remove_widget_animation_json(PARAMS["asset_path"], PARAMS["anim_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "wanim_remove_animation", "asset_path": PARAMS["asset_path"],
            "anim_name": res.get("anim_name", PARAMS["anim_name"]),
            "display_label": res.get("display_label"),
            "prev_binding_count": res.get("prev_binding_count"),
            "prev_track_count": res.get("prev_track_count")})
        out = {"status": "success", "anim_name": res.get("anim_name"), "removed": bool(res.get("removed")),
               "display_label": res.get("display_label"), "animation_count": res.get("animation_count"),
               "note": res.get("note"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_widget_animation(ctx, asset_path: str, anim_name: str) -> str:
        """Remove a UWidgetAnimation from a Widget Blueprint.

        asset_path: Widget Blueprint asset path.
        anim_name:  animation object name or display label.

        Ledgered inverse = best-effort re-create (LOSSY: tracks/bindings/keys are NOT restored). Returns
        {anim_name, removed, display_label, animation_count, note, ledger_depth}. UMG/MovieScene-only;
        needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name}
        try:
            return json.dumps(_exec(_REMOVE_ANIM_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_BINDING_BODY = _HELP + r'''
rl = _mrl("add_animation_widget_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_animation_widget_binding")))
else:
    res = _decode(rl.add_animation_widget_binding_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("added"):
            _ledger().append({"op": "wanim_add_binding", "asset_path": PARAMS["asset_path"],
                "anim_name": res.get("anim_name", PARAMS["anim_name"]),
                "widget_name": res.get("widget", PARAMS["widget_name"]),
                "animation_guid": res.get("animation_guid")})
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "added": bool(res.get("added")), "already_bound": bool(res.get("already_bound")),
               "animation_guid": res.get("animation_guid"), "is_root_widget": res.get("is_root_widget"),
               "binding_count": res.get("binding_count"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_animation_widget_binding(ctx, asset_path: str, anim_name: str, widget_name: str) -> str:
        """Bind a widget to an animation (create the MovieScene possessable + FWidgetAnimationBinding).

        asset_path:  Widget Blueprint asset path.
        anim_name:   animation object name or display label.
        widget_name: name of the widget in the tree to possess.

        Calls UMovieScene::AddPossessable (returns the FGuid that keys the object binding) and adds an
        FWidgetAnimationBinding whose AnimationGuid == that GUID -- the exact wiring the engine's
        BindPossessableObject uses. Idempotent (returns already_bound + the GUID if present). Ledgered
        inverse = remove the binding. Returns {anim_name, widget, added, already_bound, animation_guid,
        is_root_widget, binding_count, ledger_depth}. UMG/MovieScene-only; needs the C++ handler."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_ADD_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_BINDING_BODY = _HELP + r'''
rl = _mrl("remove_animation_widget_binding_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_animation_widget_binding")))
else:
    res = _decode(rl.remove_animation_widget_binding_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("removed"):
            _ledger().append({"op": "wanim_remove_binding", "asset_path": PARAMS["asset_path"],
                "anim_name": res.get("anim_name", PARAMS["anim_name"]),
                "widget_name": res.get("widget", PARAMS["widget_name"]),
                "was_root_widget": bool(res.get("was_root_widget"))})
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "removed": bool(res.get("removed")), "animation_guid": res.get("animation_guid"),
               "removed_possessable": bool(res.get("removed_possessable")),
               "binding_count": res.get("binding_count"), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_animation_widget_binding(ctx, asset_path: str, anim_name: str, widget_name: str) -> str:
        """Remove a widget's binding from an animation (drops the possessable + its tracks + the binding).

        asset_path:  Widget Blueprint asset path.
        anim_name:   animation object name or display label.
        widget_name: name of the bound widget.

        UMovieScene::RemovePossessable cascades to the object binding AND its tracks, then the
        FWidgetAnimationBinding entries are dropped. Ledgered inverse = re-add the binding (a fresh GUID;
        any tracks/keys are NOT restored). Returns {anim_name, widget, removed, animation_guid,
        removed_possessable, binding_count, ledger_depth}. UMG/MovieScene-only; needs the C++ handler."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name}
        try:
            return json.dumps(_exec(_REMOVE_BINDING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_TRACK_BODY = _HELP + r'''
rl = _mrl("add_animation_track_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_animation_track")))
else:
    res = _decode(rl.add_animation_track_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"], PARAMS["track_type"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if res.get("added"):
            _ledger().append({"op": "wanim_add_track", "asset_path": PARAMS["asset_path"],
                "anim_name": res.get("anim_name", PARAMS["anim_name"]),
                "widget_name": res.get("widget", PARAMS["widget_name"]),
                "track_type": res.get("track_type", PARAMS["track_type"]),
                "track_class": res.get("track_class"), "animation_guid": res.get("animation_guid")})
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "track_type": res.get("track_type"), "track_class": res.get("track_class"),
               "property_name": res.get("property_name"), "added": bool(res.get("added")),
               "already_exists": bool(res.get("already_exists")), "track_count": res.get("track_count"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_animation_track(ctx, asset_path: str, anim_name: str, widget_name: str,
                            track_type: str) -> str:
        """Add a property track for a bound widget.

        asset_path:  Widget Blueprint asset path.
        anim_name:   animation object name or display label.
        widget_name: name of the bound widget (call add_animation_widget_binding first).
        track_type:  'opacity' (UMovieSceneFloatTrack -> RenderOpacity) | 'color' (UMovieSceneColorTrack ->
                     ColorAndOpacity) | 'transform' (UMovieScene2DTransformTrack -> RenderTransform) |
                     'visibility' (UMovieSceneVisibilityTrack -> Visibility).

        Adds the track via UMovieScene::AddTrack(TrackClass, possessable_guid) and sets its property
        name/path. Idempotent (one track per class per binding). Ledgered inverse = remove the track.
        Returns {anim_name, widget, track_type, track_class, property_name, added, already_exists,
        track_count, ledger_depth}. UMG/MovieScene-only; needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name,
                  "track_type": track_type}
        try:
            return json.dumps(_exec(_ADD_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _ADD_KEY_BODY = _HELP + r'''
rl = _mrl("add_animation_key_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("add_animation_key")))
else:
    res = _decode(rl.add_animation_key_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"], PARAMS["track_type"], float(PARAMS["time_seconds"]),
        float(PARAMS["value"]), int(PARAMS.get("channel_index", 0)), PARAMS.get("interp", "cubic")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "wanim_add_key", "asset_path": PARAMS["asset_path"],
            "anim_name": res.get("anim_name", PARAMS["anim_name"]),
            "widget_name": res.get("widget", PARAMS["widget_name"]),
            "track_type": res.get("track_type", PARAMS["track_type"]),
            "channel_index": res.get("channel_index"), "frame": res.get("frame"),
            "tick_resolution_num": res.get("tick_resolution_num"),
            "tick_resolution_den": res.get("tick_resolution_den"),
            "had_key": bool(res.get("had_key")), "prev_value": res.get("prev_value")})
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "track_type": res.get("track_type"), "channel_type": res.get("channel_type"),
               "channel_index": res.get("channel_index"), "frame": res.get("frame"),
               "time_seconds": res.get("time_seconds"), "value": res.get("value"),
               "interp": res.get("interp"), "section_added": bool(res.get("section_added")),
               "had_key": bool(res.get("had_key")), "prev_value": res.get("prev_value"),
               "keyed": bool(res.get("keyed")), "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def add_animation_key(ctx, asset_path: str, anim_name: str, widget_name: str, track_type: str,
                          time_seconds: float, value: float, channel_index: int = 0,
                          interp: str = "cubic") -> str:
        """Key a value on an animation track's channel at a time.

        asset_path:   Widget Blueprint asset path.
        anim_name:    animation object name or display label.
        widget_name:  name of the bound widget.
        track_type:   opacity | color | transform | visibility (a track of this type must already exist).
        time_seconds: key time in seconds (converted to a frame via the MovieScene tick resolution).
        value:        the value to key (float channels use it directly; visibility keys value != 0 as bool).
        channel_index: which channel to key (default 0). Opacity has 1 float channel; color/transform have
                      several (R,G,B,A / translation,rotation,scale,shear) -- index selects one.
        interp:       'cubic' (default) | 'linear' | 'constant' (float channels only).

        Finds/adds a section on the track, dedups any existing key at that frame (captures prev_value), and
        adds the key. Expands the section + playback range to include the frame. Ledgered inverse = remove
        the key at `frame` (or restore prev_value if had_key). Returns {anim_name, widget, track_type,
        channel_type, channel_index, frame, time_seconds, value, interp, section_added, had_key, prev_value,
        keyed, ledger_depth}. UMG/MovieScene-only; needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name,
                  "track_type": track_type, "time_seconds": time_seconds, "value": value,
                  "channel_index": channel_index, "interp": interp}
        try:
            return json.dumps(_exec(_ADD_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # UNDO-INVERSES (NON-ledgered). These reverse add_animation_track /
    # add_animation_key; editor_level.undo's wanim_add_track / wanim_add_key
    # branches call remove_animation_track_json / remove_animation_key_json.
    # Exposed as normal tools too. They append NOTHING to the ledger.
    # ================================================================== #

    _REMOVE_TRACK_BODY = _HELP + r'''
rl = _mrl("remove_animation_track_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_animation_track")))
else:
    res = _decode(rl.remove_animation_track_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"], PARAMS["track_type"]))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "track_type": res.get("track_type"), "track_class": res.get("track_class"),
               "removed": bool(res.get("removed")), "found": bool(res.get("found")),
               "track_count": res.get("track_count")}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_animation_track(ctx, asset_path: str, anim_name: str, widget_name: str,
                               track_type: str) -> str:
        """Remove a property track from a bound widget (inverse of add_animation_track).

        asset_path:  Widget Blueprint asset path.
        anim_name:   animation object name or display label.
        widget_name: name of the bound widget.
        track_type:  opacity | color | transform | visibility.

        Resolves the binding's possessable GUID + the track of this type and calls
        UMovieScene::RemoveTrack (which also drops the track's sections). NON-ledgered (this IS the
        undo-inverse of add_animation_track). Idempotent (removed=false, found=false if absent). Returns
        {anim_name, widget, track_type, track_class, removed, found, track_count}. UMG/MovieScene-only;
        needs the C++ handler (inert until built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name,
                  "track_type": track_type}
        try:
            return json.dumps(_exec(_REMOVE_TRACK_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    _REMOVE_KEY_BODY = _HELP + r'''
rl = _mrl("remove_animation_key_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("remove_animation_key")))
else:
    res = _decode(rl.remove_animation_key_json(PARAMS["asset_path"], PARAMS["anim_name"],
        PARAMS["widget_name"], PARAMS["track_type"], int(PARAMS.get("channel_index", 0)),
        float(PARAMS["time_seconds"])))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        out = {"status": "success", "anim_name": res.get("anim_name"), "widget": res.get("widget"),
               "track_type": res.get("track_type"), "channel_type": res.get("channel_type"),
               "channel_index": res.get("channel_index"), "frame": res.get("frame"),
               "time_seconds": res.get("time_seconds"), "removed": bool(res.get("removed")),
               "removed_count": res.get("removed_count")}
        print("@@UMCP@@" + json.dumps(out))
gc.collect()
'''

    @mcp.tool()
    def remove_animation_key(ctx, asset_path: str, anim_name: str, widget_name: str, track_type: str,
                             channel_index: int, time_seconds: float) -> str:
        """Remove a key from an animation track's channel at a time (inverse of add_animation_key).

        asset_path:    Widget Blueprint asset path.
        anim_name:     animation object name or display label.
        widget_name:   name of the bound widget.
        track_type:    opacity | color | transform | visibility.
        channel_index: which channel the key is on (0-based; must match the add).
        time_seconds:  key time in seconds (converted to a frame via the MovieScene tick resolution; use
                       frame * tick_resolution_den / tick_resolution_num when reversing from the ledger).

        Finds the key at that frame on the channel and removes it. NON-ledgered (this IS the undo-inverse of
        add_animation_key). Returns {anim_name, widget, track_type, channel_type, channel_index, frame,
        time_seconds, removed, removed_count}. UMG/MovieScene-only; needs the C++ handler (inert until
        built)."""
        params = {"asset_path": asset_path, "anim_name": anim_name, "widget_name": widget_name,
                  "track_type": track_type, "channel_index": channel_index, "time_seconds": time_seconds}
        try:
            return json.dumps(_exec(_REMOVE_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
