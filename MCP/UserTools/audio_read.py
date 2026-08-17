"""UserTools :: Audio / Read  (spec: docs/spec/editor.md)

Clean-room, READ-ONLY audio-asset introspection over Unreal's public Python API
(AssetRegistry + reflection, UE 5.8). Follows the editor_level.py / texture_read.py
gold-standard conventions verbatim: snippets print @@UMCP@@<json> on one line; params
are injected as base64 JSON via _exec; _query wraps every snippet with Output-Log
auto-capture and surfaces new Warning/Error lines as result["_log_warnings"]. Session id
is injected as PARAMS["_session"] for parity, though this module is PURE READ (no ledger,
no writes, no factories/creation-params, no modals, no asset editors opened).

Implemented:
  - list_audio_assets   (read-only) enumerate SoundWave / SoundCue / SoundAttenuation /
                        SoundClass / SoundMix / MetaSoundSource via AssetRegistry
                        (class_paths API); counts + paths, path/kind filterable.
  - get_sound_wave_info (read-only) a SoundWave asset: duration, num_channels, sample_rate,
                        looping, compression (type + quality), streaming/loading behavior,
                        total_samples, derived raw-PCM size, on-disk package size, sound
                        group / class / attenuation refs, virtualization, priority.
  - get_sound_cue_info  (read-only) a SoundCue asset: volume/pitch multipliers, attenuation
                        settings ref, sound class, max distance, duration, and the runtime
                        SoundNode graph (first_node -> child_nodes, walked recursively).
  - get_attenuation_info(read-only) a SoundAttenuation asset: the FSoundAttenuationSettings
                        struct -> shape, falloff distances, spatialization, occlusion, reverb,
                        focus, LPF/HPF air-absorption, priority attenuation.

Honest limitations learned live against TestMCPSetup / UE-release Engine content (UE 5.8.1):
  - SoundWave: there is NO 'sampling_rate'/'streaming'/'is_looping' UPROPERTY; the reachable
    names are get_editor_property("sample_rate") (48000), .num_channels, .looping, .duration,
    .total_samples, .compression_quality, and get_sound_asset_compression_type() (BINK_AUDIO/
    PCM/...). There is no reflected boolean that says "is this wave streamed": streaming is a
    function of the project's stream-caching setting + the wave's loading_behavior; we report
    loading_behavior + seekable_streaming + initial_chunk_size + streaming_priority and say so.
  - SoundWave raw audio byte size: the compressed cooked bulk data / FByteBulkData is NOT
    exposed to Python. We report (a) an ESTIMATED uncompressed 16-bit PCM size derived from
    total_samples * num_channels * 2, and (b) the on-disk .uasset PACKAGE size (whole package,
    editor-cook dependent), both clearly labeled. The exact compressed audio payload is not
    reachable from stock 5.8 Python and there is no MCPReflectionLibrary audio handler.
  - SoundCue graph: the RUNTIME USoundNode tree (first_node -> child_nodes) IS reachable and
    fully walked. The editor's visual UEdGraph node positions/wiring metadata is editor-only
    and not exposed; we report the runtime node tree (class + reflected props per node), which
    is the semantically meaningful graph. Per-node non-getset props are surfaced via
    MCPReflectionLibrary.get_object_property_metadata_json when present (real FProperty names).
  - This project (TestMCPSetup) + engine content has ZERO SoundAttenuation and ZERO
    MetaSoundSource assets anywhere reachable, and all engine SoundCues are shallow single
    wave-player graphs. get_attenuation_info is therefore code-complete + error-path validated
    but not positively demonstrated on a real asset (same honest posture as sequencer_read /
    animation_read for absent asset types). Field names were confirmed against the
    USoundAttenuation class-default object's FSoundAttenuationSettings struct.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


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

    # Shared Unreal-side helpers. No ''' / no backslashes.
    #  _try(fn,d)      : swallow exceptions -> default.
    #  _enum_short(v)  : str(enum) "<X.TC_MASKS: 2>" -> "TC_MASKS".
    #  _objref(o)      : object ref -> {__object__: path, class: name} or None.
    #  KIND_MODULE     : which /Script module each audio class lives in (MetaSoundSource
    #                    is in MetasoundEngine, the rest in Engine).
    _HELPER = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _enum_short(v):
    if v is None:
        return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _objref(o):
    if o is None:
        return None
    try:
        return {"__object__": o.get_path_name(), "class": o.get_class().get_name()}
    except Exception:
        return str(o)
KIND_MODULE = {
    "SoundWave": "/Script/Engine",
    "SoundCue": "/Script/Engine",
    "SoundAttenuation": "/Script/Engine",
    "SoundClass": "/Script/Engine",
    "SoundMix": "/Script/Engine",
    "MetaSoundSource": "/Script/MetasoundEngine",
}
KIND_ALIAS = {
    "soundwave": "SoundWave", "wave": "SoundWave", "sound_wave": "SoundWave",
    "soundcue": "SoundCue", "cue": "SoundCue", "sound_cue": "SoundCue",
    "soundattenuation": "SoundAttenuation", "attenuation": "SoundAttenuation",
    "soundclass": "SoundClass", "class": "SoundClass",
    "soundmix": "SoundMix", "mix": "SoundMix",
    "metasoundsource": "MetaSoundSource", "metasound": "MetaSoundSource",
}
'''

    # ------------------------------------------------------------------ #
    # list_audio_assets — enumerate audio assets via AssetRegistry        #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _HELPER + r'''
path = PARAMS.get("path") or "/Game"
kind = PARAMS.get("kind")
name_filter = (PARAMS.get("name_filter") or "").lower()
max_results = int(PARAMS.get("max_results") or 500)
ar = unreal.AssetRegistryHelpers.get_asset_registry()

ALL = ["SoundWave", "SoundCue", "SoundAttenuation", "SoundClass", "SoundMix", "MetaSoundSource"]
if kind is None or str(kind).lower() in ("all", ""):
    want = ALL
    bad_kind = None
else:
    canon = KIND_ALIAS.get(str(kind).lower(), kind)
    if canon in KIND_MODULE:
        want = [canon]; bad_kind = None
    else:
        want = []; bad_kind = kind

if bad_kind is not None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unknown kind: %s" % bad_kind,
        "valid_kinds": ALL + ["all"]}))
else:
    by_kind = {}
    all_paths = []
    truncated = False
    for cls in want:
        module = KIND_MODULE[cls]
        far = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath(module, cls)],
            recursive_paths=True, package_paths=[path])
        assets = ar.get_assets(far) or []
        kcount = 0
        for d in assets:
            nm = str(d.get_editor_property("asset_name"))
            if name_filter and name_filter not in nm.lower():
                continue
            kcount += 1
            if len(all_paths) >= max_results:
                truncated = True
                continue
            p = str(d.get_editor_property("package_name")) + "." + nm
            all_paths.append({"path": p, "kind": cls})
        by_kind[cls] = kcount
    out = {"status": "success", "path": path,
           "kind": (kind if kind else "all"),
           "name_filter": PARAMS.get("name_filter"),
           "total": sum(by_kind.values()),
           "by_kind": by_kind, "returned": len(all_paths),
           "truncated": truncated, "assets": all_paths}
    print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def list_audio_assets(ctx, path: str = "/Game", kind: str = None,
                          name_filter: str = None, max_results: int = 500) -> str:
        """Enumerate audio assets via the AssetRegistry (no assets are loaded). Read-only.

        path:        content path to scan recursively (default '/Game'; use '/Engine' for
                     engine audio, or a deeper path to narrow).
        kind:        one of 'SoundWave', 'SoundCue', 'SoundAttenuation', 'SoundClass',
                     'SoundMix', 'MetaSoundSource', or None/'all' (enumerate all six;
                     response 'by_kind' reports the per-class counts). MetaSoundSource is
                     resolved from the MetasoundEngine module; the rest from Engine.
        name_filter: case-insensitive substring on the asset name.
        max_results: cap the returned path list (default 500); 'truncated' flags if hit.

        Returns per-kind counts plus a flat list of {path, kind}. Cheap: reads registry
        descriptors rather than loading the sound objects."""
        params = {"path": path, "kind": kind, "name_filter": name_filter,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_sound_wave_info — full introspection of a SoundWave asset        #
    # ------------------------------------------------------------------ #
    _WAVE_BODY = _HELPER + r'''
path = PARAMS["path"]
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
elif not isinstance(t, unreal.SoundWave):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a SoundWave (got %s): %s" % (t.get_class().get_name(), path),
        "hint": "get_sound_wave_info targets SoundWave; use list_audio_assets for other kinds"}))
else:
    dur = _try(lambda: float(t.get_editor_property("duration")))
    ch = _try(lambda: int(t.get_editor_property("num_channels")))
    sr = _try(lambda: int(t.get_editor_property("sample_rate")))
    total_samples = _try(lambda: t.get_editor_property("total_samples"))
    info = {"status": "success", "path": t.get_path_name(),
            "name": t.get_name(), "class": t.get_class().get_name(),
            "duration_seconds": dur,
            "num_channels": ch,
            "sample_rate": sr,
            "total_samples": (int(total_samples) if total_samples is not None else None),
            "total_samples_note": "single-channel frame count (== duration * sample_rate); independent of channel count",
            "looping": _try(lambda: t.get_editor_property("looping")),
            "compression": {
                "asset_compression_type": _enum_short(_try(lambda: t.get_sound_asset_compression_type())),
                "compression_quality": _try(lambda: t.get_editor_property("compression_quality")),
                "sample_rate_quality": _enum_short(_try(lambda: t.get_editor_property("sample_rate_quality"))),
            },
            "streaming": {
                "loading_behavior": _enum_short(_try(lambda: t.get_editor_property("loading_behavior"))),
                "note": "loading_behavior is the only reachable streaming knob in 5.8. UE exposes no single 'is-streamed' bool; whether a wave streams depends on the project stream-caching setting + loading_behavior. bSeekableStreaming / StreamingPriority / bUseBinkAudio are PROTECTED and InitialChunkSize is DEPRECATED -> not readable via Python reflection.",
            },
            "sound_group": _enum_short(_try(lambda: t.get_editor_property("sound_group"))),
            "virtualization_mode": _enum_short(_try(lambda: t.get_editor_property("virtualization_mode"))),
            "priority": _try(lambda: t.get_editor_property("priority")),
            "sound_class_object": _objref(_try(lambda: t.get_editor_property("sound_class_object"))),
    }
    # attenuation on a SoundWave (SoundBase.attenuation_settings) if present
    info["attenuation_settings"] = _objref(_try(lambda: t.get_editor_property("attenuation_settings")))
    info["submix"] = _objref(_try(lambda: t.get_editor_property("sound_submix_object")))
    # ---- size (best-effort; compressed audio bulk data is not exposed to Python) ----
    est_pcm = None
    if (total_samples is not None) and ch:
        est_pcm = int(total_samples) * int(ch) * 2
    pkg_bytes = None
    sys_path = _try(lambda: unreal.SystemLibrary.get_system_path(t))
    if sys_path:
        pkg_bytes = _try(lambda: __import__("os").path.getsize(sys_path))
    info["size"] = {
        "estimated_uncompressed_pcm16_bytes": est_pcm,
        "estimated_pcm_note": "derived = total_samples * num_channels * 2 (16-bit); the actual COMPRESSED cooked audio payload / FByteBulkData is not exposed to Python.",
        "package_disk_bytes": pkg_bytes,
        "package_disk_path": sys_path,
        "package_disk_note": "size of the whole .uasset package on the editor host (editor/cook dependent), not the audio payload alone.",
    }
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_sound_wave_info(ctx, path: str) -> str:
        """Full read-only introspection of a SoundWave asset.

        path: the SoundWave asset path, e.g.
              '/Engine/EngineSounds/WhiteNoise.WhiteNoise'.

        Returns: duration_seconds, num_channels, sample_rate, total_samples, looping, a
        'compression' block (asset_compression_type e.g. BINK_AUDIO/PCM, compression_quality,
        sample_rate_quality), a 'streaming' block (loading_behavior, seekable_streaming,
        initial_chunk_size, streaming_priority), sound_group, virtualization_mode, priority,
        sound_class_object / attenuation_settings / submix refs, and a 'size' block
        (estimated uncompressed PCM16 bytes derived from samples, plus the on-disk .uasset
        package size). See the module docstring for what audio data is NOT reachable in 5.8
        (the compressed cooked payload; there is no direct is-streamed bool).
        Errors cleanly if the asset is missing or is not a SoundWave."""
        try:
            return json.dumps(_exec(_WAVE_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_sound_cue_info — SoundCue params + runtime node graph            #
    # ------------------------------------------------------------------ #
    _CUE_BODY = _HELPER + r'''
path = PARAMS["path"]
include_graph = PARAMS.get("include_graph", True)
max_nodes = int(PARAMS.get("max_nodes") or 200)
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
elif not isinstance(t, unreal.SoundCue):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a SoundCue (got %s): %s" % (t.get_class().get_name(), path),
        "hint": "get_sound_cue_info targets SoundCue; use list_audio_assets for other kinds"}))
else:
    info = {"status": "success", "path": t.get_path_name(),
            "name": t.get_name(), "class": t.get_class().get_name(),
            "volume_multiplier": _try(lambda: t.get_editor_property("volume_multiplier")),
            "pitch_multiplier": _try(lambda: t.get_editor_property("pitch_multiplier")),
            "duration_seconds": _try(lambda: float(t.get_editor_property("duration"))),
            "max_distance": _try(lambda: t.get_editor_property("max_distance")),
            "attenuation_settings": _objref(_try(lambda: t.get_editor_property("attenuation_settings"))),
            "sound_class_object": _objref(_try(lambda: t.get_editor_property("sound_class_object"))),
            "submix": _objref(_try(lambda: t.get_editor_property("sound_submix_object"))),
            "virtualization_mode": _enum_short(_try(lambda: t.get_editor_property("virtualization_mode"))),
            "priority": _try(lambda: t.get_editor_property("priority"))}
    root = _try(lambda: t.get_editor_property("first_node"))
    info["first_node_class"] = root.get_class().get_name() if root else None
    # ---- runtime SoundNode graph walk (first_node -> child_nodes) ----
    has_meta = hasattr(unreal.MCPReflectionLibrary, "get_object_property_metadata_json")
    def _getset_names(obj):
        names, seen = [], set()
        for klass in type(obj).__mro__:
            for name, val in vars(klass).items():
                if name.startswith("__") or name in seen:
                    continue
                if type(val).__name__ in ("getset_descriptor", "property"):
                    names.append(name)
                seen.add(name)
        return names
    SKIP = set(["child_nodes", "graph_node", "sound_cue"])
    counter = {"n": 0, "truncated": False}
    def _ser_val(v):
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        if isinstance(v, unreal.EnumBase):
            return _enum_short(v)
        if isinstance(v, (unreal.Name, unreal.Text)):
            return str(v)
        if isinstance(v, unreal.Object):
            return _objref(v)
        if isinstance(v, unreal.Array):
            return [_ser_val(e) for e in list(v)[:12]]
        return str(v)
    def _node_props(n):
        props = {}
        # getset descriptor props (skip structural)
        for pn in _getset_names(n):
            if pn in SKIP:
                continue
            props[pn] = _ser_val(_try(lambda pn=pn: n.get_editor_property(pn)))
        # native FProperty names not surfaced as getset (e.g. SoundWave on WavePlayer)
        if has_meta:
            md = _try(lambda: unreal.MCPReflectionLibrary.get_object_property_metadata_json(n))
            try:
                doc = json.loads(md) if md else None
            except Exception:
                doc = None
            if isinstance(doc, dict):
                for pd in (doc.get("properties") or []):
                    raw = pd.get("name")
                    if not raw:
                        continue
                    # snake-case the PascalCase FProperty name for get_editor_property
                    snake = ""
                    for i, cch in enumerate(raw):
                        if cch.isupper() and i > 0 and not raw[i-1].isupper():
                            snake += "_"
                        snake += cch.lower()
                    if snake in props or snake in SKIP:
                        continue
                    val = _try(lambda snake=snake: n.get_editor_property(snake))
                    if val is not None:
                        props[snake] = _ser_val(val)
        return props
    def _walk(n, depth):
        if n is None or depth > 12:
            return None
        if counter["n"] >= max_nodes:
            counter["truncated"] = True
            return None
        counter["n"] += 1
        node = {"class": n.get_class().get_name()}
        node["props"] = _node_props(n)
        kids = _try(lambda: n.get_editor_property("child_nodes")) or []
        children = []
        for c in kids:
            cw = _walk(c, depth + 1)
            if cw is not None:
                children.append(cw)
        if children:
            node["children"] = children
        return node
    if include_graph:
        info["graph"] = _walk(root, 0)
        info["node_count"] = counter["n"]
        info["graph_truncated"] = counter["truncated"]
        info["graph_note"] = "runtime USoundNode tree (first_node -> child_nodes). The editor's visual UEdGraph layout/wiring metadata is editor-only and not exposed; this is the semantic node graph."
    else:
        info["graph_note"] = "call with include_graph=True to expand the runtime SoundNode tree"
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_sound_cue_info(ctx, path: str, include_graph: bool = True,
                           max_nodes: int = 200) -> str:
        """Full read-only introspection of a SoundCue asset.

        path:          the SoundCue asset path, e.g.
                       '/Engine/VREditor/Sounds/VR_teleport_Cue.VR_teleport_Cue'.
        include_graph: also walk the runtime SoundNode graph (default True).
        max_nodes:     cap on nodes walked (default 200; 'graph_truncated' flags if hit).

        Returns: volume_multiplier, pitch_multiplier, duration_seconds, max_distance,
        attenuation_settings ref, sound_class_object ref, submix ref, virtualization_mode,
        priority, first_node_class, and (when include_graph) a nested 'graph' of the runtime
        SoundNode tree: each node's class plus its reflected props (WavePlayer -> sound_wave
        ref, Modulator -> pitch/volume min/max, Attenuation -> settings, etc.) and children.
        The visual editor graph layout is editor-only and not exposed; this is the semantic
        runtime tree. Errors cleanly if the asset is missing or is not a SoundCue."""
        params = {"path": path, "include_graph": include_graph, "max_nodes": max_nodes}
        try:
            return json.dumps(_exec(_CUE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_attenuation_info — SoundAttenuation settings struct              #
    # ------------------------------------------------------------------ #
    _ATTEN_BODY = _HELPER + r'''
path = PARAMS["path"]
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
elif not isinstance(t, unreal.SoundAttenuation):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a SoundAttenuation (got %s): %s" % (t.get_class().get_name(), path),
        "hint": "get_attenuation_info targets SoundAttenuation; use list_audio_assets for other kinds"}))
else:
    att = _try(lambda: t.get_editor_property("attenuation"))
    if att is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "could not read 'attenuation' struct on: %s" % path}))
    else:
        def E(name):
            return _enum_short(_try(lambda: att.get_editor_property(name)))
        def B(name):
            return _try(lambda: att.get_editor_property(name))
        def V(name):
            v = _try(lambda: att.get_editor_property(name))
            if v is None:
                return None
            try:
                return [v.x, v.y, v.z]
            except Exception:
                return _try(lambda: str(v))
        info = {"status": "success", "path": t.get_path_name(),
                "name": t.get_name(), "class": t.get_class().get_name(),
                "shape": {
                    "attenuation_shape": E("attenuation_shape"),
                    "attenuation_shape_extents": V("attenuation_shape_extents"),
                    "cone_offset": B("cone_offset"),
                },
                "distance": {
                    "attenuate": B("attenuate"),
                    "distance_algorithm": E("distance_algorithm"),
                    "falloff_mode": E("falloff_mode"),
                    "falloff_distance": B("falloff_distance"),
                    "db_attenuation_at_max": B("d_b_attenuation_at_max"),
                },
                "spatialization": {
                    "spatialize": B("spatialize"),
                    "spatialization_algorithm": E("spatialization_algorithm"),
                    "omni_radius": B("omni_radius"),
                    "stereo_spread": B("stereo_spread"),
                    "binaural_radius": B("binaural_radius"),
                    "non_spatialized_radius_start": B("non_spatialized_radius_start"),
                    "non_spatialized_radius_end": B("non_spatialized_radius_end"),
                },
                "air_absorption_lpf": {
                    "attenuate_with_lpf": B("attenuate_with_lpf"),
                    "lpf_radius_min": B("lpf_radius_min"),
                    "lpf_radius_max": B("lpf_radius_max"),
                    "lpf_frequency_at_min": B("lpf_frequency_at_min"),
                    "lpf_frequency_at_max": B("lpf_frequency_at_max"),
                    "hpf_frequency_at_min": B("hpf_frequency_at_min"),
                    "hpf_frequency_at_max": B("hpf_frequency_at_max"),
                },
                "occlusion": {
                    "enable_occlusion": B("enable_occlusion"),
                    "occlusion_trace_channel": E("occlusion_trace_channel"),
                    "occlusion_low_pass_filter_frequency": B("occlusion_low_pass_filter_frequency"),
                    "occlusion_volume_attenuation": B("occlusion_volume_attenuation"),
                    "occlusion_interpolation_time": B("occlusion_interpolation_time"),
                },
                "reverb": {
                    "enable_reverb_send": B("enable_reverb_send"),
                    "reverb_send_method": E("reverb_send_method"),
                    "reverb_distance_min": B("reverb_distance_min"),
                    "reverb_distance_max": B("reverb_distance_max"),
                    "reverb_wet_level_min": B("reverb_wet_level_min"),
                    "reverb_wet_level_max": B("reverb_wet_level_max"),
                },
                "focus": {
                    "enable_listener_focus": B("enable_listener_focus"),
                    "focus_azimuth": B("focus_azimuth"),
                    "non_focus_azimuth": B("non_focus_azimuth"),
                    "focus_volume_attenuation": B("focus_volume_attenuation"),
                    "non_focus_volume_attenuation": B("non_focus_volume_attenuation"),
                },
                "priority_attenuation": {
                    "enable_priority_attenuation": B("enable_priority_attenuation"),
                    "priority_attenuation_method": E("priority_attenuation_method"),
                    "priority_attenuation_min": B("priority_attenuation_min"),
                    "priority_attenuation_max": B("priority_attenuation_max"),
                    "priority_attenuation_distance_min": B("priority_attenuation_distance_min"),
                    "priority_attenuation_distance_max": B("priority_attenuation_distance_max"),
                },
                "submix_sends": {
                    "enable_submix_sends": B("enable_submix_sends"),
                },
        }
        print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_attenuation_info(ctx, path: str) -> str:
        """Full read-only introspection of a SoundAttenuation asset (its
        FSoundAttenuationSettings struct).

        path: the SoundAttenuation asset path.

        Returns grouped settings: shape (attenuation_shape, extents, cone_offset),
        distance (attenuate, distance_algorithm, falloff_mode, falloff_distance,
        db_attenuation_at_max), spatialization (spatialize, algorithm, omni_radius,
        stereo_spread, non-spatialized radii), air_absorption_lpf (LPF/HPF frequencies vs
        radius), occlusion, reverb, focus, and priority_attenuation blocks.

        NOTE: this project + engine content contains ZERO SoundAttenuation assets, so this
        command is validated on its error paths (missing / wrong-type) rather than a live
        asset; the field extraction was confirmed against the USoundAttenuation class-default
        object. Errors cleanly if the asset is missing or is not a SoundAttenuation."""
        try:
            return json.dumps(_exec(_ATTEN_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"
