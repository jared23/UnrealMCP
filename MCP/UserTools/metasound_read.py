"""UserTools :: MetaSound authoring (READ)  (spec: docs/spec/audio.md, plan: audio_plan.md #6,#7,#22)

Read-only MetaSound introspection over Unreal's public Python API (UE 5.8). Companion to
metasound_write.py. PURE READ: no ledger, no factories, no persistence. Conventions (@@UMCP@@ payload,
base64 PARAMS, Output-Log delta capture) copied verbatim from editor_level.py / audio_read.py.

Implemented:
  * get_metasound_info (#6, PARTIAL): reflected UPROPERTYs of a MetaSoundSource (output_format,
    quality_setting, block/sample-rate overrides, is_preset, sound_class/submix, effect chain, duration,
    max_distance, priority, virtualization, registry version) + graph input/output names via the builder
    getters. Node/edge COUNTS require the C++ document reader (Wave 4) and are reported as null with a note.
  * get_metasound_graph (#7, PARTIAL): graph input names (+ data types + defaults), graph output names
    (+ data types), and declared-interface checks against the known interface set, via
    find_or_begin_building builder getters. Walking the FULL node/edge topology of a foreign asset is C++
    (FMetaSoundFrontendDocumentBuilder, non-BlueprintCallable) -> Wave 4; documented honestly.
  * validate_metasound (#22, PARTIAL): confirms the asset opens a builder (registers with the frontend),
    reports graph input/output counts, and surfaces any Warning/Error log lines captured during the read.
    A full compile/error-count validation is C++ (Wave 4).
  * list_metasound_node_catalog: a seed catalog of standard node class names confirmed live via
    add_node_by_class_name (namespace 'UE'). Full node discovery is C++ (ISearchEngine, Wave 4 #1).

NO ''' and NO backslashes in any snippet body; all data via base64 PARAMS.
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
    "            if _ww: print('@@UMCP_LOG@@'+_jj.dumps(_ww[-50:]))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _wrap(code):
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

    _MS = r'''
import unreal, json
EAL = unreal.EditorAssetLibrary
SUCC = unreal.MetaSoundBuilderResult.SUCCEEDED
KNOWN_IFACES = ["UE.Source.OneShot", "UE.Attenuation", "UE.Spatialization", "UE.Source.Mono",
    "UE.Source.Stereo", "UE.Reverb.Send", "UE.SourceEffect", "UE.Send", "UE.Receive"]
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _enum_short(v):
    if v is None: return None
    s = str(v)
    if "." in s and ":" in s: return s.split(".")[-1].split(":")[0].strip()
    return s
def _objref(o):
    if o is None: return None
    return _try(lambda: {"__object__": o.get_path_name(), "class": o.get_class().get_name()}, str(o))
def _resok(r): return r == SUCC
def _sv(v):
    # JSON-safe coercion: primitives pass through; enums -> short name; structs/objects -> str.
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, unreal.EnumBase):
        return _enum_short(v)
    return _try(lambda: str(v), None)
def _load_ms(path):
    if not path: return None, "no asset path given"
    obj = _try(lambda: EAL.load_asset(path))
    if obj is None: return None, "asset not found: %s" % path
    if not isinstance(obj, (unreal.MetaSoundSource, unreal.MetaSoundPatch)):
        return None, "asset is not a MetaSound (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _fobb(ms):
    es = unreal.get_editor_subsystem(unreal.MetaSoundEditorSubsystem)
    b, r = es.find_or_begin_building(ms)
    if not _resok(r) or b is None: return None
    return b
'''

    # ================================================================ #
    # #6 get_metasound_info                                            #
    # ================================================================ #
    _INFO_BODY = _MS + r'''
apath = PARAMS["asset_path"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    is_source = isinstance(ms, unreal.MetaSoundSource)
    info = {"status": "success", "asset_path": ms.get_path_name(), "name": ms.get_name(),
            "class": ms.get_class().get_name(), "kind": ("source" if is_source else "patch")}
    if is_source:
        info["source"] = {
            "output_format": _enum_short(_try(lambda: ms.get_editor_property("output_format"))),
            "quality_setting": _sv(_try(lambda: ms.get_editor_property("quality_setting"))),
            "block_rate_override": _sv(_try(lambda: ms.get_editor_property("block_rate_override"))),
            "sample_rate_override": _sv(_try(lambda: ms.get_editor_property("sample_rate_override"))),
            "is_preset": _sv(_try(lambda: ms.get_editor_property("is_preset"))),
            "duration": _sv(_try(lambda: ms.get_editor_property("duration"))),
            "max_distance": _sv(_try(lambda: ms.get_editor_property("max_distance"))),
            "priority": _sv(_try(lambda: ms.get_editor_property("priority"))),
            "virtualization_mode": _enum_short(_try(lambda: ms.get_editor_property("virtualization_mode"))),
            "registry_version_major": _sv(_try(lambda: ms.get_editor_property("registry_version_major"))),
            "registry_version_minor": _sv(_try(lambda: ms.get_editor_property("registry_version_minor"))),
            "sound_class_object": _objref(_try(lambda: ms.get_editor_property("sound_class_object"))),
            "sound_submix_object": _objref(_try(lambda: ms.get_editor_property("sound_submix_object"))),
            "source_effect_chain": _objref(_try(lambda: ms.get_editor_property("source_effect_chain"))),
        }
    else:
        info["patch"] = {"is_preset": _try(lambda: ms.get_editor_property("is_preset"))}
    # graph input/output names via builder
    b = _fobb(ms)
    if b is not None:
        gin, ri = b.get_graph_input_names(); gon, ro = b.get_graph_output_names()
        info["graph_inputs"] = [str(x) for x in (gin or [])] if _resok(ri) else None
        info["graph_outputs"] = [str(x) for x in (gon or [])] if _resok(ro) else None
        info["declared_interfaces"] = [i for i in KNOWN_IFACES if _try(lambda i=i: bool(b.interface_is_declared(i)), False)]
    info["node_edge_counts"] = None
    info["counts_note"] = "node/edge COUNTS require the C++ document reader (FMetaSoundFrontendDocumentBuilder, non-BlueprintCallable) -> Wave 4. graph input/output names + declared interfaces ARE available (above)."
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_metasound_info(ctx, asset_path: str) -> str:
        """Read a MetaSound asset's reflected properties + graph input/output names (PARTIAL).

        asset_path: the MetaSoundSource/Patch object path.

        Returns the reflected UPROPERTYs (for a source: output_format, quality_setting, block/sample-rate
        overrides, is_preset, duration, max_distance, priority, virtualization_mode, registry version,
        sound_class/submix/effect-chain refs) plus graph_inputs / graph_outputs names and the declared
        interface set (checked via the builder). node/edge COUNTS require the C++ document reader (Wave 4)
        and are reported null with a note. Errors cleanly if the asset is missing or not a MetaSound."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #7 get_metasound_graph  (PARTIAL)                                #
    # ================================================================ #
    _GRAPH_BODY = _MS + r'''
apath = PARAMS["asset_path"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b = _fobb(ms)
    if b is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not open a builder for the asset"}))
    else:
        gin, ri = b.get_graph_input_names(); gon, ro = b.get_graph_output_names()
        inputs = []
        for nm in (gin or []):
            nmS = str(nm)
            nh, dt, oh, rf = b.find_graph_input_node(nmS)
            dl, rd = b.get_graph_input_default(nmS)
            inputs.append({"name": nmS, "data_type": (str(dt) if _resok(rf) else None),
                           "default": (dl.export_text() if _resok(rd) else None)})
        outputs = []
        for nm in (gon or []):
            nmS = str(nm)
            nh, dt, ih, rf = b.find_graph_output_node(nmS)
            outputs.append({"name": nmS, "data_type": (str(dt) if _resok(rf) else None)})
        ifaces = [i for i in KNOWN_IFACES if _try(lambda i=i: bool(b.interface_is_declared(i)), False)]
        print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ms.get_path_name(),
            "graph_inputs": inputs, "graph_outputs": outputs, "declared_interfaces": ifaces,
            "partial": True,
            "note": "PARTIAL readback: graph inputs (name/type/default) + outputs (name/type) + declared interfaces via builder getters. The full internal node/edge topology of a foreign asset needs the C++ document walker (Wave 4). Graph VARIABLES cannot be enumerated (no get_graph_variable_names in the BP API); query a known variable default via the builder if needed."}))
'''

    @mcp.tool()
    def get_metasound_graph(ctx, asset_path: str) -> str:
        """Read a MetaSound's graph interface (PARTIAL): inputs, outputs, declared interfaces.

        asset_path: the MetaSoundSource/Patch object path.

        Returns graph_inputs [{name, data_type, default}], graph_outputs [{name, data_type}], and
        declared_interfaces, all via find_or_begin_building builder getters. The full internal node/edge
        topology of a foreign asset requires the C++ document walker (non-BlueprintCallable) -> Wave 4.
        Graph variables cannot be enumerated (no get_graph_variable_names in the BP API). Documented
        honestly. Errors cleanly if the asset is missing or not a MetaSound."""
        try:
            return json.dumps(_exec(_GRAPH_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # #22 validate_metasound  (PARTIAL)                                #
    # ================================================================ #
    _VALIDATE_BODY = _MS + r'''
apath = PARAMS["asset_path"]
ms, err = _load_ms(apath)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    b = _fobb(ms)
    registered = b is not None
    ic = None; oc = None
    if registered:
        gin, ri = b.get_graph_input_names(); gon, ro = b.get_graph_output_names()
        ic = len(list(gin or [])) if _resok(ri) else None
        oc = len(list(gon or [])) if _resok(ro) else None
    print("@@UMCP@@" + json.dumps({"status": "success", "asset_path": ms.get_path_name(),
        "registered_with_frontend": registered, "graph_input_count": ic, "graph_output_count": oc,
        "valid": bool(registered),
        "partial": True,
        "note": "PARTIAL validation: confirms the asset opens a builder (registers with the frontend) + reports interface member counts; any Warning/Error lines emitted during the read appear in _log_warnings. A full compile + per-node error report is C++ (Wave 4)."}))
'''

    @mcp.tool()
    def validate_metasound(ctx, asset_path: str) -> str:
        """Validate a MetaSound asset (PARTIAL): frontend registration + interface counts + log capture.

        asset_path: the MetaSoundSource/Patch object path.

        Confirms the asset opens a builder (i.e. registers with the MetaSound frontend), reports graph
        input/output counts, and surfaces any Warning/Error log lines captured during the check
        (result._log_warnings). A full compile/error-count validation is C++ (Wave 4). Errors cleanly if
        the asset is missing or not a MetaSound."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================ #
    # list_metasound_node_catalog  (seed; full discovery is C++)       #
    # ================================================================ #
    @mcp.tool()
    def list_metasound_node_catalog(ctx) -> str:
        """List a seed catalog of standard MetaSound node class names (namespace 'UE') confirmed live.

        Returns {namespace, name, variant} triples usable directly with add_metasound_node /
        build_metasound_graph. This is a SEED list captured by probing add_node_by_class_name -- full node
        discovery (every registered class + its pins) requires the C++ ISearchEngine handler (Wave 4 #1-4).
        A class name that FAILS add_metasound_node is simply not in this engine's registry under that
        namespace/name/variant; adjust the variant (often the data type, e.g. 'Audio' vs 'Float')."""
        catalog = [
            {"namespace": "UE", "name": "Sine", "variant": "Audio", "desc": "sine oscillator (audio-rate)"},
            {"namespace": "UE", "name": "Saw", "variant": "Audio", "desc": "saw oscillator (audio-rate)"},
            {"namespace": "UE", "name": "Square", "variant": "Audio", "desc": "square oscillator (audio-rate)"},
            {"namespace": "UE", "name": "Triangle", "variant": "Audio", "desc": "triangle oscillator (audio-rate)"},
            {"namespace": "UE", "name": "LFO", "variant": "Audio", "desc": "low-frequency oscillator"},
            {"namespace": "UE", "name": "Noise", "variant": "Audio", "desc": "noise generator"},
            {"namespace": "UE", "name": "Add", "variant": "Audio", "desc": "add (audio)"},
            {"namespace": "UE", "name": "Add", "variant": "Float", "desc": "add (float)"},
            {"namespace": "UE", "name": "Multiply", "variant": "Audio", "desc": "multiply (audio)"},
            {"namespace": "UE", "name": "Multiply", "variant": "Float", "desc": "multiply (float)"},
            {"namespace": "UE", "name": "Subtract", "variant": "Float", "desc": "subtract (float)"},
            {"namespace": "UE", "name": "Divide", "variant": "Float", "desc": "divide (float)"},
            {"namespace": "UE", "name": "Ladder Filter", "variant": "Audio", "desc": "ladder low-pass filter"},
            {"namespace": "UE", "name": "Delay", "variant": "Audio", "desc": "audio delay line"},
            {"namespace": "UE", "name": "Wave Player", "variant": "Mono", "desc": "mono wave player"},
            {"namespace": "UE", "name": "Wave Player", "variant": "Stereo", "desc": "stereo wave player"},
        ]
        return json.dumps({"status": "success", "count": len(catalog), "catalog": catalog,
            "note": "SEED catalog confirmed live via add_node_by_class_name; full registry discovery is C++ (Wave 4)."}, indent=2)
