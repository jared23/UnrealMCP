"""UserTools :: Audio / general asset ops  (spec: docs/spec/audio.md, group G)

Wave 1 of the AUDIO buildout (UE 5.8, Windows-native). PURE PYTHON over the reflected
`unreal.*` API -- NO C++, NO build. General audio-asset operations that sit alongside the
SoundCue-graph tools (soundcue_graph.py) and the create_* tools (sound_write.py).

Conventions copied VERBATIM from editor_level.py / sound_write.py: snippets print @@UMCP@@<json>
on ONE line; params are base64-injected via _exec; _query auto-captures new Output-Log
Warning/Error lines into result["_log_warnings"]; the per-session undo ledger lives in
builtins._UMCP_LEDGERS keyed by PARAMS["_session"]. Snippet bodies contain NO triple-single-quotes
and NO backslashes, and never clobber the wrapper's reserved locals.

Tools (G group):
  READS (no ledger):
   * export_audio           -- reflection-walk an audio asset -> write JSON to
                               <project>/Saved/MCP_Exports/<name>.json (a file, not an asset).
   * list_audio_asset_types -- authorable/known audio asset classes (issubclass of SoundBase /
                               SoundClass / SoundMix / SoundSubmixBase / SoundAttenuation /
                               SoundConcurrency) + which are creatable via create_audio_asset.
   * get_audio_asset_info   -- generic dispatcher: detects the asset class and returns generic
                               reflected props plus kind-specific highlights (cue graph, submix
                               parent/children/effects, sound-class parent/children, concurrency,
                               attenuation, wave stats).
  WRITE (ledgered, reuses create_asset):
   * create_audio_asset     -- generic create over a class->factory map (SoundCue / SoundClass /
                               SoundMix / SoundAttenuation / SoundSubmix / SoundConcurrency); each
                               factory is non-modal (bounded-probed live before shipping).

Reversibility: create_audio_asset reuses the coordinator's already-folded generic 'create_asset'
inverse {asset_path, package_path, created_dir} (close editors + delete asset [+ rmdir]); NO new
op. The reads write no assets (export_audio writes a JSON FILE under Saved/, reversible by deleting
the file; no ledger, matching the plan).

Modal-safety: all six factories were bounded-probed once (create -> close editors -> GC -> delete ->
verify gone) with no modal and no leftovers -- the four Sound* factories in sound_write.py plus
SoundSubmixFactory (-> SoundSubmix) and SoundConcurrencyFactory (-> SoundConcurrency) here.
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

# NOTE: plugin wraps incoming code in triple-SINGLE-quotes -> NO ''' and NO backslashes in bodies.


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
    _HELPERS = r'''
import unreal, json, builtins, warnings
warnings.simplefilter("ignore")
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
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
def _ser_val(v, depth=0):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, unreal.EnumBase):
        return _enum_short(v)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Object):
        return _objref(v)
    if isinstance(v, unreal.Array):
        return [_ser_val(e, depth+1) for e in list(v)[:24]]
    if isinstance(v, unreal.StructBase) and depth < 3:
        d = {}
        for pn in _getset_names(v):
            try:
                d[pn] = _ser_val(v.get_editor_property(pn), depth+1)
            except Exception:
                pass
        return d if d else str(v)
    try:
        return str(v)
    except Exception:
        return None
def _dump_props(obj, skip=None):
    skip = skip or set()
    props = {}
    for pn in _getset_names(obj):
        if pn in skip:
            continue
        try:
            props[pn] = _ser_val(obj.get_editor_property(pn))
        except Exception:
            pass
    return props
FACTORY_MAP = {
    "SoundCue": ("SoundCue", "SoundCueFactoryNew"),
    "SoundClass": ("SoundClass", "SoundClassFactory"),
    "SoundMix": ("SoundMix", "SoundMixFactory"),
    "SoundAttenuation": ("SoundAttenuation", "SoundAttenuationFactory"),
    "SoundSubmix": ("SoundSubmix", "SoundSubmixFactory"),
    "SoundConcurrency": ("SoundConcurrency", "SoundConcurrencyFactory"),
}
KIND_ALIAS = {
    "cue": "SoundCue", "soundcue": "SoundCue",
    "class": "SoundClass", "soundclass": "SoundClass",
    "mix": "SoundMix", "soundmix": "SoundMix",
    "attenuation": "SoundAttenuation", "soundattenuation": "SoundAttenuation", "atten": "SoundAttenuation",
    "submix": "SoundSubmix", "soundsubmix": "SoundSubmix",
    "concurrency": "SoundConcurrency", "soundconcurrency": "SoundConcurrency", "conc": "SoundConcurrency",
}
'''

    # ------------------------------------------------------------------ #
    # list_audio_asset_types                                              #
    # ------------------------------------------------------------------ #
    _TYPES_BODY = _HELPERS + r'''
name_filter = (PARAMS.get("name_filter") or "").lower()
bases = ["SoundBase", "SoundClass", "SoundMix", "SoundSubmixBase", "SoundAttenuation", "SoundConcurrency"]
by_base = {}
for bn in bases:
    base = getattr(unreal, bn, None)
    subs = []
    if base is not None:
        for nm in dir(unreal):
            o = getattr(unreal, nm, None)
            try:
                if isinstance(o, type) and issubclass(o, base):
                    subs.append(nm)
            except Exception:
                pass
    if name_filter:
        subs = [s for s in subs if name_filter in s.lower()]
    by_base[bn] = sorted(subs)
creatable = {}
for k, (clsname, facname) in FACTORY_MAP.items():
    creatable[k] = {"class": clsname, "factory": facname,
                    "class_available": getattr(unreal, clsname, None) is not None,
                    "factory_available": getattr(unreal, facname, None) is not None}
print("@@UMCP@@" + json.dumps({"status": "success",
    "subclasses_by_base": by_base,
    "creatable_via_create_audio_asset": creatable,
    "note": "subclasses_by_base = every reflectable subclass of each audio base class. creatable_* = the asset types create_audio_asset can make (each factory is non-modal). SoundWave/MetaSoundSource are imported/built, not factory-created here."}))
'''

    @mcp.tool()
    def list_audio_asset_types(ctx, name_filter: str = None) -> str:
        """List audio asset classes: every reflectable subclass of the audio base classes
        (SoundBase, SoundClass, SoundMix, SoundSubmixBase, SoundAttenuation, SoundConcurrency) plus
        the subset that create_audio_asset can instantiate. Read-only reflection (no ledger).

        name_filter: case-insensitive substring on the class name.

        Returns 'subclasses_by_base' (e.g. SoundBase -> SoundCue/SoundWave/MetaSoundSource/...) and
        'creatable_via_create_audio_asset' (SoundCue/SoundClass/SoundMix/SoundAttenuation/
        SoundSubmix/SoundConcurrency, each with its factory + availability flags)."""
        try:
            return json.dumps(_exec(_TYPES_BODY, {"name_filter": name_filter}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_audio_asset_info — generic dispatcher                           #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _HELPERS + r'''
path = PARAMS["asset_path"]
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
else:
    cn = t.get_class().get_name()
    info = {"status": "success", "path": t.get_path_name(), "name": t.get_name(), "class": cn}
    highlights = {}
    if isinstance(t, unreal.SoundCue):
        info["kind"] = "SoundCue"
        root = t.get_editor_property("first_node")
        seen = set()
        cnt = {"n": 0}
        def walk(n, d):
            if n is None or d > 40:
                return
            nm = n.get_name()
            if nm in seen:
                return
            seen.add(nm); cnt["n"] += 1
            for c in (n.get_editor_property("child_nodes") or []):
                walk(c, d+1)
        walk(root, 0)
        highlights = {"first_node_class": (root.get_class().get_name() if root else None),
                      "node_count": cnt["n"],
                      "volume_multiplier": t.get_editor_property("volume_multiplier"),
                      "pitch_multiplier": t.get_editor_property("pitch_multiplier"),
                      "sound_class": _objref(t.get_editor_property("sound_class_object")),
                      "attenuation_settings": _objref(t.get_editor_property("attenuation_settings"))}
        highlights["hint"] = "use get_sound_cue_graph for the full node tree"
    elif isinstance(t, unreal.SoundClass):
        info["kind"] = "SoundClass"
        parent = None
        for key in ("parent_class", "parent_class_object", "parent"):
            try:
                pv = t.get_editor_property(key)
            except Exception:
                pv = None
            if pv is not None:
                parent = pv; break
        kids = None
        try:
            kids = [_objref(c) for c in (t.get_editor_property("child_classes") or [])]
        except Exception:
            kids = None
        props = None
        try:
            props = _ser_val(t.get_editor_property("properties"))
        except Exception:
            props = None
        highlights = {"parent_class": _objref(parent), "child_classes": kids, "properties": props}
    elif isinstance(t, unreal.SoundSubmixBase):
        info["kind"] = "SoundSubmix"
        parent = None
        try:
            parent = t.get_editor_property("parent_submix")
        except Exception:
            parent = None
        kids = None
        try:
            kids = [_objref(c) for c in (t.get_editor_property("child_submixes") or [])]
        except Exception:
            kids = None
        chain = None
        try:
            chain = [_objref(e) for e in (t.get_editor_property("submix_effect_chain") or [])]
        except Exception:
            chain = None
        highlights = {"parent_submix": _objref(parent), "child_submixes": kids, "submix_effect_chain": chain}
    elif isinstance(t, unreal.SoundConcurrency):
        info["kind"] = "SoundConcurrency"
        try:
            highlights = {"concurrency": _ser_val(t.get_editor_property("concurrency"))}
        except Exception:
            highlights = {}
    elif isinstance(t, unreal.SoundAttenuation):
        info["kind"] = "SoundAttenuation"
        try:
            att = t.get_editor_property("attenuation")
            highlights = {"attenuate": att.get_editor_property("attenuate"),
                          "attenuation_shape": _enum_short(att.get_editor_property("attenuation_shape")),
                          "falloff_distance": att.get_editor_property("falloff_distance"),
                          "spatialize": att.get_editor_property("spatialize")}
        except Exception:
            highlights = {}
        highlights["hint"] = "use get_attenuation_info for the full FSoundAttenuationSettings struct"
    elif isinstance(t, unreal.SoundMix):
        info["kind"] = "SoundMix"
        highlights = {"hint": "SoundClass mix modifiers are in the 'sound_class_effects' property (see props)"}
    elif isinstance(t, unreal.SoundWave):
        info["kind"] = "SoundWave"
        highlights = {"duration_seconds": t.get_editor_property("duration"),
                      "num_channels": t.get_editor_property("num_channels"),
                      "sample_rate": t.get_editor_property("sample_rate"),
                      "hint": "use get_sound_wave_info for the full readout"}
    elif isinstance(t, unreal.SoundBase):
        info["kind"] = cn
        highlights = {"note": "generic SoundBase readout; MetaSoundSource full graph = audio Wave 2"}
    else:
        info["kind"] = cn
        highlights = {"note": "not a recognized audio kind; generic property dump only"}
    info["highlights"] = highlights
    info["properties"] = _dump_props(t)
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_audio_asset_info(ctx, asset_path: str) -> str:
        """Generic read-only dispatcher over any audio asset. Detects the class and returns
        kind-specific highlights plus a generic reflected property dump. No ledger.

        asset_path: any audio asset path (SoundCue, SoundClass, SoundMix, SoundSubmix,
                    SoundConcurrency, SoundAttenuation, SoundWave, or other SoundBase).

        'kind' names the detected type; 'highlights' carries the salient fields per kind (cue:
        first_node/node_count/mults/class/attenuation; sound class: parent + child_classes; submix:
        parent_submix + child_submixes + submix_effect_chain; concurrency: the concurrency struct;
        attenuation: shape/falloff/spatialize; wave: duration/channels/sample_rate). 'properties' is
        the full getset dump. For the deepest reads use the dedicated tools noted in each
        highlights.hint (get_sound_cue_graph / get_sound_wave_info / get_attenuation_info). Errors
        cleanly if the asset is missing."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"asset_path": asset_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # export_audio — reflection walk -> JSON file under Saved/MCP_Exports #
    # ------------------------------------------------------------------ #
    _EXPORT_BODY = _HELPERS + r'''
import os as _os
path = PARAMS["asset_path"]
out_name = PARAMS.get("out_name")
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
else:
    doc = {"asset_path": t.get_path_name(), "name": t.get_name(),
           "class": t.get_class().get_name(),
           "exported_by": "UnrealMCP.export_audio", "engine": "UE5.8",
           "properties": _dump_props(t)}
    if isinstance(t, unreal.SoundCue):
        root = t.get_editor_property("first_node")
        def gnode(n, d):
            if n is None or d > 30:
                return None
            node = {"name": n.get_name(), "class": n.get_class().get_name(), "props": {}}
            for pn in _getset_names(n):
                if pn in ("child_nodes", "graph_node", "sound_cue"):
                    continue
                try:
                    node["props"][pn] = _ser_val(n.get_editor_property(pn))
                except Exception:
                    pass
            kids = []
            for c in (n.get_editor_property("child_nodes") or []):
                cw = gnode(c, d+1) if c is not None else None
                kids.append(cw)
            if kids:
                node["children"] = kids
            return node
        doc["graph"] = gnode(root, 0)
    base_dir = _uu = None
    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
    export_dir = _os.path.join(saved, "MCP_Exports")
    try:
        _os.makedirs(export_dir, exist_ok=True)
    except Exception:
        pass
    fname = (out_name or t.get_name())
    if not fname.lower().endswith(".json"):
        fname = fname + ".json"
    fpath = _os.path.join(export_dir, fname)
    text = json.dumps(doc, indent=2)
    ok = False
    err = None
    try:
        fh = open(fpath, "w", encoding="utf-8")
        fh.write(text); fh.close(); ok = True
    except Exception as e:
        err = str(e)[:160]
    size = None
    if ok:
        try:
            size = _os.path.getsize(fpath)
        except Exception:
            pass
    print("@@UMCP@@" + json.dumps({"status": ("success" if ok else "error"),
        "asset": t.get_name(), "class": t.get_class().get_name(),
        "file": fpath, "bytes": size, "property_count": len(doc.get("properties") or {}),
        "has_graph": ("graph" in doc), "error": err,
        "note": "wrote a JSON reflection export to <project>/Saved/MCP_Exports/. This is a FILE (not a content asset) -> no undo ledger; delete the file to reverse."}))
'''

    @mcp.tool()
    def export_audio(ctx, asset_path: str, out_name: str = None) -> str:
        """Export an audio asset's reflected data to a JSON file under
        <project>/Saved/MCP_Exports/. Read-only w.r.t. content (writes a file, not an asset; no
        ledger).

        asset_path: any audio asset path (SoundCue/SoundWave/SoundClass/SoundMix/SoundSubmix/
                    SoundConcurrency/SoundAttenuation/...).
        out_name:   output filename (default: the asset name; '.json' appended if missing).

        Walks the asset's getset properties (and, for a SoundCue, the full first_node -> child_nodes
        tree) and writes an indented JSON document. Returns the absolute file path, byte size,
        property count, and whether a graph was included. Reverse by deleting the file."""
        try:
            return json.dumps(_exec(_EXPORT_BODY, {"asset_path": asset_path, "out_name": out_name}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_audio_asset — generic create over class->factory map         #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
asset_type = PARAMS["asset_type"]
canon = KIND_ALIAS.get(str(asset_type).lower(), asset_type)
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
if canon not in FACTORY_MAP:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "unknown asset_type: %s" % asset_type,
        "valid_types": sorted(FACTORY_MAP.keys())}))
else:
    class_name, factory_name = FACTORY_MAP[canon]
    acls = getattr(unreal, class_name, None)
    fcls = getattr(unreal, factory_name, None)
    asset_path = package_path + "/" + name
    if acls is None or fcls is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "missing class/factory: %s / %s" % (class_name, factory_name)}))
    elif EAL.does_asset_exist(asset_path):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
    else:
        created_dir = not EAL.does_directory_exist(package_path)
        obj = at.create_asset(name, package_path, acls, fcls())
        if obj is None or not isinstance(obj, acls):
            if created_dir and EAL.does_directory_exist(package_path):
                try: EAL.delete_directory(package_path)
                except Exception: pass
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
        else:
            try:
                aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
                aes.close_all_editors_for_asset(obj)
            except Exception:
                pass
            try:
                EAL.save_asset(asset_path, only_if_is_dirty=False)
            except Exception:
                pass
            entry = {"op": "create_asset", "asset_path": asset_path,
                     "package_path": package_path, "created_dir": created_dir}
            _ledger().append(entry)
            print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
                "asset_path": asset_path, "object_path": obj.get_path_name(),
                "class": obj.get_class().get_name(), "kind": canon,
                "created_dir": created_dir,
                "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def create_audio_asset(ctx, name: str, asset_type: str,
                           package_path: str = "/Game/MCP_Scratch") -> str:
        """Create an audio asset of a chosen type non-interactively (NO modal). Ledgered write
        (reuses the generic create_asset inverse).

        name:         asset name for the new asset.
        asset_type:   one of 'SoundCue', 'SoundClass', 'SoundMix', 'SoundAttenuation',
                      'SoundSubmix', 'SoundConcurrency' (case-insensitive; short aliases like
                      'cue'/'submix'/'concurrency' accepted).
        package_path: content directory (default '/Game/MCP_Scratch'); intermediate folders created
                      as needed.

        Dispatches over a class->factory map (SoundCueFactoryNew / SoundClassFactory /
        SoundMixFactory / SoundAttenuationFactory / SoundSubmixFactory / SoundConcurrencyFactory);
        every factory is non-modal (bounded-probed live: create -> close -> delete -> verify gone).
        For SoundCue graph authoring afterwards use add_sound_cue_node / build_sound_cue_graph; for
        SoundWave use the import tools (waves are imported, not factory-created).

        Reuses the already-folded generic ledger op 'create_asset' {asset_path, package_path,
        created_dir} -- NO new op. Inverse: close editors + delete asset [+ rmdir if this call
        created the directory]."""
        params = {"name": name, "asset_type": asset_type, "package_path": package_path}
        try:
            return json.dumps(_exec(_CREATE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NO `undo` tool here. create_audio_asset reuses the folded generic 'create_asset' inverse;
    # the reads write no ledgered content (export_audio writes a JSON file under Saved/).
    # ------------------------------------------------------------------ #
