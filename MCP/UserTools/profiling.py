"""UserTools :: Profiling / Stats  (READ-ONLY)  (spec: docs/spec/profiling.md)

Clean-room, READ-ONLY profiling + engine-stats reads over Unreal's PUBLIC Python API
(UE 5.8). Reimplemented from scratch; no decompiled/third-party source. Executed in the
editor's interpreter via the plugin's `execute_python` handler; results captured from stdout.

Follows the editor_level.py / rendering_read.py / texture_read.py gold-standard conventions
VERBATIM: snippets print  @@UMCP@@<json>  on one line and _query() parses after that marker;
params are injected as base64 JSON via _exec() (survives the handler's triple-single-quote
wrapping); every snippet is wrapped with Output-Log delta capture (new Warning/Error lines ->
result["_log_warnings"]). PURE READ: no ledger, no writes, no factories, no modals.

------------------------------------------------------------------------------------------
PROBE FINDINGS — what profiling/stats data IS / ISN'T Python-reachable in this UE 5.8.1 build
------------------------------------------------------------------------------------------
REACHABLE via direct public API:
  - Frame timing: unreal.SystemLibrary.get_frame_count() (cumulative rendered frames) +
    get_platform_time_seconds() (monotonic wall clock). Sampling the DELTA of both across two
    separate execute_python calls (the editor ticks freely between calls) yields a REAL average
    FPS + average frame time over the window. This is the backbone of the live-timing tools.
    Verified live: 15 frames / 0.196 s -> 76.7 avg FPS, 13.0 ms/frame (idle editor).
  - Scene composition: level actors / primitive components / static-mesh triangle totals
    (UStaticMesh.get_num_triangles(lod), get_num_nanite_triangles) / light counts by type — all
    via EditorActorSubsystem + reflection. (CPU-side scene counts, NOT per-frame GPU draw stats.)
  - Live UObject counts + UObject memory by class: the `obj list class=<X>` console command
    (unreal.SystemLibrary.execute_console_command) writes a parseable summary to the Output Log
    ("N Objects (Total: X.XXXM / Max: ...M / Res: ...M ...)"). Read back via the log delta. This
    is STAT-COMMAND-BACKED (not a direct API), counts ALL live UObjects of the class/subclasses
    (not only placed actors).
  - On-disk asset footprint: AssetRegistry (get_assets) + resolving package_name to the
    Content/.uasset file and os.path.getsize — cheap (no asset loading), REAL disk bytes.

NOT Python-reachable in this build (documented as limitations; NOT faked):
  - Platform/process RAM (physical/virtual used/available): no psutil in the embedded
    interpreter, no memory getter on SystemLibrary, and `memreport` does not log a compact
    summary to the main log. get_memory_stats is therefore SCOPED to UObject memory (from
    `obj list`), and platform RAM is reported as null with a note. (See get_memory_stats.)
  - Per-thread times (game / render / GPU thread ms) and GPU per-frame draw calls / triangles
    rendered: these live in engine render-thread stat counters (GGameThreadTime, RHI draw-call
    counters) exposed only through the on-screen `stat unit` / `stat scenerendering` / `stat rhi`
    HUD overlays — NOT readable from Python without toggling a viewport overlay (which we refuse
    to leave on screen). Reported as null with a note; the frame-time AVERAGE (from the live pair)
    is the reachable substitute for total frame ms.

NO overlap with existing modules: get_texture_stats (texture_read.py), get_rendering_settings /
get_scalability_settings (rendering_read.py), and count_actors_by_class are complemented, not
duplicated — this module adds engine timing, live UObject counts/memory, scene triangle/light
composition, and on-disk asset footprint.

Implemented (all READ-ONLY):
  - get_engine_perf          — pollable one-shot: current frame/time counters + avg FPS/frame-ms
                               measured against the previous get_engine_perf call (auto-primes).
  - performance_live_start   — begin a named frame-timing window (baseline stashed on builtins).
  - performance_live_stop    — end the window: avg FPS + avg frame time over the elapsed interval.
  - get_object_counts        — live UObject counts + UObject memory by class (obj list backed).
  - get_scene_render_stats   — scene composition: actors, primitive components, triangles, lights.
  - list_largest_assets      — largest assets under a path by on-disk .uasset/.umap byte size.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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
# before exec, so snippet bodies must contain NO ''' and NO stray backslashes. All data is
# passed as base64 JSON via _exec so it survives that wrapping.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    # Session id identifies THIS reader process (kept for parity + to namespace the live-timing
    # baselines on builtins so concurrent agents never read each other's window). Pure-read module:
    # it never touches any undo ledger.
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
        """Inject PARAMS (as base64 JSON, to survive the handler's ''' wrapping), run the body
        in Unreal, and return its MARKER payload."""
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to bodies). No ''' / no backslashes.
    #  - _try: swallow-and-default.
    #  - _now: current frame_count + platform wall-clock seconds + editor game time.
    #  - _run_console_readlog: exec a console command and return the appended Output-Log lines
    #    (timestamp-prefix stripped) — the mechanism behind the stat-command-backed reads.
    #  - _objlist_summary: parse the "N Objects (Total: X.XXXM / Max: ...M / Res: ...M ...)" line.
    _PROF_HELPERS = r'''
import unreal, json, os
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _editor_world():
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return _try(lambda: ues.get_editor_world())
def _now():
    w = _editor_world()
    return {
        "frame_count": _try(lambda: int(unreal.SystemLibrary.get_frame_count())),
        "platform_time": _try(lambda: float(unreal.SystemLibrary.get_platform_time_seconds())),
        "game_time": _try(lambda: float(unreal.SystemLibrary.get_game_time_in_seconds(w))) if w else None,
    }
def _logfile():
    d = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())
    for f in os.listdir(d):
        if f.endswith(".log") and "-backup-" not in f:
            return os.path.join(d, f)
    return None
def _strip_ts(line):
    # engine log lines are "[2026.08.16-05.38.18:727][ 78]<payload>"; drop to the last ']'.
    i = line.rfind("]")
    return line[i+1:] if i >= 0 else line
def _run_console_readlog(cmd):
    lf = _logfile()
    w = _editor_world()
    unreal.log_flush()
    s0 = os.path.getsize(lf) if lf else 0
    unreal.SystemLibrary.execute_console_command(w, cmd)
    unreal.log_flush()
    s1 = os.path.getsize(lf) if lf else 0
    if lf and s1 > s0:
        fh = open(lf, "rb"); fh.seek(s0); dd = fh.read().decode("utf-8", "replace"); fh.close()
        return [_strip_ts(l) for l in dd.splitlines()]
    return []
def _f(tok):
    try: return float(tok)
    except Exception: return None
def _objlist_summary(lines):
    # Find the "N Objects (Total: X.XXXM / Max: Y.YYYM / Res: Z.ZZZM | ResDedSys: ... )" line.
    for l in lines:
        s = l.strip()
        if " Objects (Total:" in s:
            left, right = s.split(" Objects (Total:", 1)
            count = None
            try: count = int(left.split()[-1])
            except Exception: count = None
            total_mb = _f(right.split("/")[0].replace("M", "").strip())
            max_mb = None; res_mb = None
            if "Max:" in right:
                max_mb = _f(right.split("Max:", 1)[1].split("/")[0].replace("M", "").strip())
            if "Res:" in right:
                res_mb = _f(right.split("Res:", 1)[1].split("|")[0].replace("M", "").strip())
            return {"count": count, "total_mb": total_mb, "max_mb": max_mb, "resident_mb": res_mb,
                    "summary_line": s}
    return None
'''

    # ================================================================== #
    # get_engine_perf — pollable one-shot frame-timing + counters         #
    # ================================================================== #
    _ENGINE_PERF_BODY = _PROF_HELPERS + r'''
import builtins
key = "engine_perf_last:" + str(PARAMS.get("_session", "default"))
root = getattr(builtins, "_UMCP_PROF", None)
if root is None:
    root = {}; builtins._UMCP_PROF = root
cur = _now()
prev = root.get(key)
root[key] = cur
result = {"status": "success",
          "frame_count": cur["frame_count"],
          "platform_time_seconds": (round(cur["platform_time"], 3) if cur["platform_time"] is not None else None),
          "game_time_seconds": (round(cur["game_time"], 3) if cur["game_time"] is not None else None),
          "engine_version": _try(lambda: unreal.SystemLibrary.get_engine_version())}
window = None
if (prev and cur["frame_count"] is not None and prev.get("frame_count") is not None
        and cur["platform_time"] is not None and prev.get("platform_time") is not None):
    dfc = cur["frame_count"] - prev["frame_count"]
    dpt = cur["platform_time"] - prev["platform_time"]
    if dfc > 0 and dpt > 0:
        window = {"delta_frames": dfc, "delta_seconds": round(dpt, 4),
                  "avg_fps": round(dfc / dpt, 2),
                  "avg_frame_time_ms": round(1000.0 * dpt / dfc, 3),
                  "note": "measured between this call and the PREVIOUS get_engine_perf call (same session)"}
if window:
    result["measured"] = window
else:
    result["measured"] = None
    result["measure_note"] = ("no usable prior sample this session yet (auto-primed now) OR the "
        "editor did not advance a frame between calls; call get_engine_perf again after a moment, "
        "or use performance_live_start/performance_live_stop for an explicit window.")
result["thread_times_note"] = ("per-thread times (game/render/GPU thread ms) and GPU draw-call / "
    "rendered-triangle counters are NOT Python-reachable in this build (engine render-thread stats "
    "exposed only via the on-screen 'stat unit'/'stat scenerendering'/'stat rhi' overlays, which we "
    "refuse to leave enabled). avg_frame_time_ms above is the reachable total-frame-time substitute.")
result["method"] = ("avg FPS/frame-time = delta of SystemLibrary.get_frame_count() over delta of "
    "get_platform_time_seconds() (wall clock) across two calls; the editor ticks freely between "
    "calls so this is a real average over the inter-call interval.")
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_engine_perf(ctx) -> str:
        """Read live engine performance counters. Read-only.

        Returns the current cumulative frame_count, platform (wall-clock) time, editor game time,
        and engine version. If you have called get_engine_perf before in this session, it also
        returns 'measured' = the average FPS and average frame time (ms) between the previous call
        and this one (the editor ticks freely between calls, so it is a real average over that
        interval). The FIRST call auto-primes and returns measured=null — call again after a moment
        to get a reading, or use performance_live_start / performance_live_stop for an explicit,
        controlled window.

        LIMITATION (honest, not faked): per-thread times (game/render/GPU thread ms) and GPU
        draw-call / rendered-triangle counters are NOT reachable from Python in this build — those
        live in engine render-thread stat counters exposed only via the on-screen 'stat' HUD
        overlays. avg_frame_time_ms is the reachable total-frame-time substitute."""
        try:
            return json.dumps(_exec(_ENGINE_PERF_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # performance_live_start — begin a named frame-timing window          #
    # ================================================================== #
    _LIVE_START_BODY = _PROF_HELPERS + r'''
import builtins
label = str(PARAMS.get("label") or "default")
key = "live:" + str(PARAMS.get("_session", "default")) + ":" + label
root = getattr(builtins, "_UMCP_PROF", None)
if root is None:
    root = {}; builtins._UMCP_PROF = root
cur = _now()
existing = root.get(key)
root[key] = cur
result = {"status": "success", "label": label,
          "baseline": {"frame_count": cur["frame_count"],
                       "platform_time_seconds": (round(cur["platform_time"], 3) if cur["platform_time"] is not None else None),
                       "game_time_seconds": (round(cur["game_time"], 3) if cur["game_time"] is not None else None)},
          "replaced_running_window": existing is not None,
          "note": ("frame-timing window started. Let the editor run (interact with the viewport / "
                   "leave it a few seconds), then call performance_live_stop with the same label to "
                   "get avg FPS + avg frame time over the elapsed interval. No trace overhead — this "
                   "just samples the frame counter + wall clock.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def performance_live_start(ctx, label: str = "default") -> str:
        """Begin a live frame-timing window (no trace overhead). Read-only.

        Records a baseline sample (frame counter + wall clock) under 'label'. Later call
        performance_live_stop with the SAME label to get the average FPS and average frame time
        over the elapsed interval. Use a distinct label to run several overlapping windows.

        This samples SystemLibrary.get_frame_count() + get_platform_time_seconds() only — it does
        NOT start an Unreal Insights .utrace (that requires trace automation not exposed to Python
        in this build). The baseline is held in-process (per session), so start and stop must be
        issued by the same agent/bridge process."""
        try:
            return json.dumps(_exec(_LIVE_START_BODY, {"label": label}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # performance_live_stop — end window, report avg timing               #
    # ================================================================== #
    _LIVE_STOP_BODY = _PROF_HELPERS + r'''
import builtins
label = str(PARAMS.get("label") or "default")
clear = bool(PARAMS.get("clear") if PARAMS.get("clear") is not None else True)
key = "live:" + str(PARAMS.get("_session", "default")) + ":" + label
root = getattr(builtins, "_UMCP_PROF", None)
if root is None:
    root = {}; builtins._UMCP_PROF = root
base = root.get(key)
if base is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "label": label,
        "message": "no live window named '%s' for this session; call performance_live_start first" % label}))
else:
    cur = _now()
    if clear:
        try: del root[key]
        except Exception: pass
    dfc = None; dpt = None
    if cur["frame_count"] is not None and base.get("frame_count") is not None:
        dfc = cur["frame_count"] - base["frame_count"]
    if cur["platform_time"] is not None and base.get("platform_time") is not None:
        dpt = cur["platform_time"] - base["platform_time"]
    res = {"status": "success", "label": label,
           "elapsed_seconds": (round(dpt, 4) if dpt is not None else None),
           "frames_rendered": dfc,
           "avg_fps": (round(dfc / dpt, 2) if (dfc and dpt and dpt > 0) else None),
           "avg_frame_time_ms": (round(1000.0 * dpt / dfc, 3) if (dfc and dpt and dfc > 0) else None),
           "baseline": base, "window_cleared": clear}
    if not dfc or not dpt or dpt <= 0:
        res["warning"] = ("window too short or the editor did not advance a frame (elapsed<=0 or "
                          "0 frames). Leave a longer gap between start and stop.")
    res["note"] = ("avg over the whole window = frames_rendered / elapsed_seconds (frame counter "
                   "delta over wall-clock delta). This is the mean; min/max/percentile per-frame "
                   "spikes need Unreal Insights tracing, which is not Python-exposed in this build.")
    print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def performance_live_stop(ctx, label: str = "default", clear: bool = True) -> str:
        """End a live frame-timing window and report the average timing over the interval.
        Read-only.

        label: the window label passed to performance_live_start (default 'default').
        clear: remove the window after reading (default True); pass False to keep sampling and
               stop again later (each stop measures from the ORIGINAL start baseline).

        Returns elapsed_seconds, frames_rendered, avg_fps and avg_frame_time_ms over the window.
        These are means over the whole interval — per-frame min/max/percentile spike stats need
        Unreal Insights tracing, which is not exposed to Python here (documented, not faked)."""
        params = {"label": label, "clear": clear}
        try:
            return json.dumps(_exec(_LIVE_STOP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_object_counts — live UObject counts + memory by class           #
    # ================================================================== #
    _OBJ_COUNTS_BODY = _PROF_HELPERS + r'''
classes = PARAMS.get("classes")
if isinstance(classes, str):
    classes = [c.strip() for c in classes.split(",") if c.strip()]
if not classes:
    classes = ["Actor", "StaticMeshActor", "StaticMeshComponent", "SkeletalMeshComponent",
               "InstancedStaticMeshComponent", "PointLightComponent", "SpotLightComponent",
               "DirectionalLightComponent", "MaterialInstanceDynamic", "Texture2D"]
rows = []
grand_count = 0
grand_total_mb = 0.0
for cls in classes:
    # bounded, read-only console query; the summary line carries count + UObject memory.
    lines = _run_console_readlog("obj list class=%s" % cls)
    summ = _objlist_summary(lines)
    if summ is None:
        rows.append({"class": cls, "count": None, "note": "no summary line (unknown class or no output)"})
        continue
    rows.append({"class": cls, "count": summ["count"], "uobject_total_mb": summ["total_mb"],
                 "uobject_max_mb": summ["max_mb"], "uobject_resident_mb": summ["resident_mb"]})
    if summ["count"]:
        grand_count += summ["count"]
    if summ["total_mb"]:
        grand_total_mb += summ["total_mb"]
result = {"status": "success",
          "classes_queried": classes,
          "counts": rows,
          "queried_totals": {"count": grand_count, "uobject_total_mb": round(grand_total_mb, 4),
                             "note": "sum across the queried classes ONLY (classes overlap by "
                                     "inheritance, e.g. StaticMeshActor is also counted under Actor "
                                     "— do not read this as a whole-engine total)."},
          "source": "console command 'obj list class=<X>' -> Output Log summary (stat-command-backed)",
          "note": ("count = ALL live UObjects of that class OR its subclasses (includes CDOs, "
                   "transient/editor-preview objects, not only placed level actors). uobject_*_mb "
                   "are the engine's reported UObject memory columns (NumKB/MaxKB/ResExcKB rolled to "
                   "MB) — this is UObject bookkeeping memory, NOT GPU/texture memory (use "
                   "get_texture_stats for texture GPU memory). A class with very many instances "
                   "writes many lines to the Output Log while listing.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_object_counts(ctx, classes=None) -> str:
        """Count live UObjects (and their UObject memory) by class. Read-only.

        classes: a class name, a comma-separated string, or a list of class names. If omitted, a
                 sensible default set is queried (Actor, StaticMeshActor, StaticMeshComponent,
                 SkeletalMeshComponent, InstancedStaticMeshComponent, the light components,
                 MaterialInstanceDynamic, Texture2D).

        For each class returns count + UObject memory (total/max/resident MB). Backed by the
        engine `obj list class=<X>` console command, whose Output-Log summary is parsed — so the
        count includes that class AND its subclasses and covers ALL live UObjects (CDOs, transient
        and editor-preview objects), not only placed level actors. The memory columns are UObject
        bookkeeping memory, NOT GPU/texture memory (use get_texture_stats for that). Complements
        count_actors_by_class (which counts only placed level actors of one class).

        NOTE: querying a class with a very large instance count writes many lines to the Output Log
        during the listing — keep the class list targeted."""
        try:
            return json.dumps(_exec(_OBJ_COUNTS_BODY, {"classes": classes}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # get_scene_render_stats — scene composition (CPU-side counts)        #
    # ================================================================== #
    _SCENE_STATS_BODY = _PROF_HELPERS + r'''
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors() or []
n_actors = len(actors)
prim = {"static_mesh": 0, "instanced_static_mesh": 0, "skeletal_mesh": 0, "other_primitive": 0}
tris_lod0 = 0
nanite_tris = 0
ism_instances = 0
n_meshes_measured = 0
lights = {"point": 0, "spot": 0, "directional": 0, "rect": 0, "sky": 0}
decals = 0
def _isa(o, tname):
    c = getattr(unreal, tname, None)
    return (c is not None) and isinstance(o, c)
ISM = getattr(unreal, "InstancedStaticMeshComponent", None)
for a in actors:
    if a is None:
        continue
    for c in (a.get_components_by_class(unreal.PrimitiveComponent) or []):
        if _isa(c, "StaticMeshComponent"):
            is_ism = (ISM is not None and isinstance(c, ISM))
            inst = 1
            if is_ism:
                prim["instanced_static_mesh"] += 1
                inst = _try(lambda c=c: int(c.get_instance_count()), 0) or 0
                ism_instances += inst
            else:
                prim["static_mesh"] += 1
            m = _try(lambda c=c: c.get_editor_property("static_mesh"))
            if m is not None:
                n_meshes_measured += 1
                t = _try(lambda m=m: int(m.get_num_triangles(0)), 0) or 0
                nt = _try(lambda m=m: int(m.get_num_nanite_triangles()), 0) or 0
                mult = inst if is_ism else 1
                tris_lod0 += t * mult
                nanite_tris += nt * mult
        elif _isa(c, "SkeletalMeshComponent"):
            prim["skeletal_mesh"] += 1
        elif _isa(c, "DecalComponent"):
            decals += 1
        else:
            prim["other_primitive"] += 1
    for c in (a.get_components_by_class(unreal.LightComponent) or []):
        if _isa(c, "PointLightComponent") and not _isa(c, "SpotLightComponent"):
            lights["point"] += 1
        elif _isa(c, "SpotLightComponent"):
            lights["spot"] += 1
        elif _isa(c, "DirectionalLightComponent"):
            lights["directional"] += 1
        elif _isa(c, "RectLightComponent"):
            lights["rect"] += 1
        else:
            lights["point"] += 0
    if _isa(a, "SkyLight"):
        lights["sky"] += 1
result = {"status": "success",
          "level": _try(lambda: _editor_world().get_name()),
          "actor_count": n_actors,
          "primitive_components": prim,
          "instanced_static_mesh_instances": ism_instances,
          "lights_by_type": lights,
          "light_total": sum(lights.values()),
          "decal_components": decals,
          "geometry": {"static_mesh_components_measured": n_meshes_measured,
                       "triangles_lod0_total": tris_lod0,
                       "nanite_triangles_total": nanite_tris,
                       "note": "sum of UStaticMesh.get_num_triangles(LOD0) (and get_num_nanite_triangles) "
                               "over every static-mesh component, multiplied by instance count for ISM/HISM. "
                               "This is the SCENE's authored triangle budget, not the number of triangles "
                               "the GPU draws in a given frame (that is view/cull/LOD dependent)."},
          "scope_note": ("CPU-side SCENE COMPOSITION from the level's placed actors + components — "
                         "NOT per-frame GPU render stats. Draw calls, rendered-triangle counts, and "
                         "primitives-visible-in-view are render-thread counters exposed only through "
                         "the on-screen 'stat scenerendering'/'stat rhi' overlays and are NOT "
                         "Python-reachable here (documented, not faked). Use get_engine_perf / the "
                         "live-timing pair for frame timing.")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_scene_render_stats(ctx) -> str:
        """Scene composition statistics for the active level. Read-only.

        Returns placed-actor count, primitive-component breakdown (static mesh, instanced static
        mesh, skeletal mesh, other), total instanced-static-mesh instance count, light counts by
        type (point/spot/directional/rect/sky) and total, decal-component count, and the scene's
        triangle budget (sum of static-mesh LOD0 triangles and Nanite triangles across all
        components, multiplied by instance count for ISM/HISM).

        SCOPE (honest): these are CPU-side SCENE COMPOSITION counts derived from the level's actors
        and components — NOT per-frame GPU stats. Draw calls, rendered-triangle counts, and
        primitives-visible-in-view are render-thread counters exposed only via the on-screen
        'stat scenerendering' / 'stat rhi' HUD overlays and are NOT reachable from Python in this
        build. Use get_engine_perf or performance_live_start/stop for frame timing."""
        try:
            return json.dumps(_exec(_SCENE_STATS_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # list_largest_assets — largest assets under a path by disk size      #
    # ================================================================== #
    _LARGEST_BODY = _PROF_HELPERS + r'''
path = PARAMS.get("path") or "/Game"
class_filter = PARAMS.get("class_filter")
top = int(PARAMS.get("top") or 25)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
if class_filter:
    far = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/Engine", class_filter)],
        recursive_paths=True, recursive_classes=True, package_paths=[path])
else:
    far = unreal.ARFilter(recursive_paths=True, package_paths=[path])
assets = ar.get_assets(far) or []
content_dir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
engine_dir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_content_dir())
def _pkg_to_file(pkg):
    roots = None
    if pkg.startswith("/Game/"):
        roots = (content_dir, pkg[len("/Game/"):])
    elif pkg.startswith("/Engine/"):
        roots = (engine_dir, pkg[len("/Engine/"):])
    if roots is None:
        return None
    for ext in (".uasset", ".umap"):
        p = os.path.join(roots[0], roots[1] + ext)
        if os.path.exists(p):
            return p
    return None
seen = set()
rows = []
total_bytes = 0
resolved = 0
unresolved = 0
for d in assets:
    pkg = str(d.get_editor_property("package_name"))
    if pkg in seen:
        continue
    seen.add(pkg)
    fp = _pkg_to_file(pkg)
    if fp is None:
        unresolved += 1
        continue
    try:
        b = os.path.getsize(fp)
    except Exception:
        unresolved += 1
        continue
    resolved += 1
    total_bytes += b
    rows.append({"asset": str(d.get_editor_property("asset_name")),
                 "class": str(d.get_editor_property("asset_class_path").get_editor_property("asset_name")) if _try(lambda d=d: d.get_editor_property("asset_class_path")) else None,
                 "package": pkg,
                 "disk_bytes": b, "disk_mb": round(b / 1048576.0, 4)})
rows.sort(key=lambda r: r["disk_bytes"], reverse=True)
result = {"status": "success", "path": path, "class_filter": class_filter,
          "unique_packages_resolved": resolved, "unresolved": unresolved,
          "total_disk_mb_scanned": round(total_bytes / 1048576.0, 3),
          "top": top, "largest": rows[:top],
          "source": "AssetRegistry (get_assets, no asset loaded) + os.path.getsize on the .uasset/.umap file",
          "note": ("disk_bytes = the on-disk cooked/editor package file size (the asset's DISK "
                   "footprint), read WITHOUT loading the asset — cheap. This is not the same as "
                   "runtime GPU/RAM memory (that needs the asset loaded; see get_texture_stats for "
                   "texture GPU memory). 'unresolved' = packages with no local .uasset/.umap "
                   "(e.g. /Engine transient, or a non-/Game//Engine mount root not mapped here).")}
print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def list_largest_assets(ctx, path: str = "/Game", class_filter: str = None,
                            top: int = 25) -> str:
        """List the largest assets under a content path by on-disk file size. Read-only.

        path:         content path to scan recursively (default '/Game').
        class_filter: optional engine class name to restrict to (e.g. 'StaticMesh', 'Texture2D',
                      'SkeletalMesh'); matches that class and its subclasses.
        top:          how many of the largest to return (default 25).

        Returns the top-N assets by DISK footprint (the .uasset/.umap file byte size), the count
        of packages resolved, and the total disk MB scanned. Cheap: reads the AssetRegistry and
        stats files on disk — no assets are loaded.

        NOTE: disk size is the packaged/editor file footprint, NOT runtime GPU/RAM memory (which
        requires loading the asset; use get_texture_stats for texture GPU memory). Packages under
        mount roots other than /Game and /Engine are reported as 'unresolved'."""
        params = {"path": path, "class_filter": class_filter, "top": top}
        try:
            return json.dumps(_exec(_LARGEST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
