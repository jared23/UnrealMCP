"""UserTools :: Sound / Audio (WRITE)  (spec: docs/spec/audio.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). CREATE wave for audio
assets -- the mutating counterpart to audio_read.py. Query convention, base64 PARAMS injection,
Output-Log auto-capture, and the per-session undo ledger are copied VERBATIM from the
gold-standard editor_level.py (and mirror ai_write.py, the reference create-wave module).

What this build exposes (all NON-MODAL, bounded-probed live vs TestMCPSetup, UE 5.8.1):
  * SoundCue         via unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, path,
                       unreal.SoundCue, unreal.SoundCueFactoryNew())
  * SoundClass       via unreal.SoundClassFactory()
  * SoundMix         via unreal.SoundMixFactory()
  * SoundAttenuation via unreal.SoundAttenuationFactory()
    Each factory's supported_class matches its asset type and NONE pops a ConfigureProperties
    dialog on the scripting create_asset path -> no modal EVER (proven by a create+delete probe
    per factory: created_type == expected, saved, deleted, dir removed, exists_after == False).
  * add_wave_player_to_cue: attach a USoundNodeWavePlayer (playing a given SoundWave) as the
    cue's first_node. SoundCue.construct_sound_node is NOT Python-exposed in 5.8, but the node
    is constructible via unreal.new_object(unreal.SoundNodeWavePlayer, <cue>); its wave is set
    via the reflected SoundWaveAssetPtr (get/set_editor_property('sound_wave_asset_ptr', wave))
    and the cue's runtime graph root via SoundCue.first_node (a settable getset property).
    Readback via audio_read.get_sound_cue_info shows first_node -> SoundNodeWavePlayer -> sound_wave.

Reversibility / undo: this module does NOT register its own `undo` tool (editor_level.py owns the
single unified `undo`; a duplicate name collides at bridge registration).
  - The four create_* commands reuse the coordinator's generic "create_asset" inverse
    {asset_path, package_path, created_dir} (close editors + delete asset [+ rmdir if we made the
    dir]) -- already folded into editor_level.undo; NO new op needed.
  - add_wave_player_to_cue introduces ONE new op "add_wave_player_to_cue" {asset_path,
    object_path}. Inverse = reload the cue and clear its first_node (set to None) + save. To keep
    that inverse FAITHFUL, the command REFUSES a cue that already has a first_node (prior state must
    be empty -> restoring to empty is exact). Inverse logic is documented in the tool docstring +
    the build report for the coordinator to fold into editor_level.undo.

MODAL-SAFETY: every factory was bounded-probed exactly once (create then immediately close-editors
+ GC + delete + rmdir) before shipping; all four returned cleanly with no modal and no leftovers.
Each tool performs ONE operation per call, staying well under the execute_python socket window.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
# bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never assign a
# snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/
# success/user_code/code_obj (they are the C++ wrapper's own locals -> clobbering them wedges capture).


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

    # Shared Unreal-side helpers. No ''' / no backslashes in this block.
    #   _ledger()               -> per-session undo stack (copied verbatim from editor_level.py).
    #   _load_typed(path, ty)   -> (obj, err) load an asset and type-check its class name.
    #   _close_editors(obj)     -> silently close any open asset editor for obj.
    _SW_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_typed(path, tyname):
    if not path:
        return None, "no asset path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    cn = obj.get_class().get_name()
    if cn != tyname:
        return None, "asset is not a %s (got %s): %s" % (tyname, cn, path)
    return obj, None
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
        return True
    except Exception:
        return False
'''

    # ------------------------------------------------------------------ #
    # Generic create-sound body: one implementation, parameterized by     #
    # asset class + factory name (all four sound factories are non-modal). #
    # ------------------------------------------------------------------ #
    _CREATE_SOUND_BODY = _SW_HELPERS + r'''
name = PARAMS["name"]
package_path = (PARAMS.get("package_path") or "/Game/MCP_Scratch").rstrip("/")
class_name = PARAMS["asset_class"]
factory_name = PARAMS["factory"]
kind_label = PARAMS.get("kind_label") or class_name
EAL = unreal.EditorAssetLibrary
at = unreal.AssetToolsHelpers.get_asset_tools()
acls = getattr(unreal, class_name, None)
fcls = getattr(unreal, factory_name, None)
asset_path = package_path + "/" + name
if acls is None or fcls is None:
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "missing class/factory on unreal module: %s / %s" % (class_name, factory_name)}))
elif EAL.does_asset_exist(asset_path):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset already exists: %s (refusing to overwrite)" % asset_path}))
else:
    created_dir = not EAL.does_directory_exist(package_path)
    obj = None
    # NOTE: asset creation must NOT be wrapped in a ScopedEditorTransaction -- that puts the new
    # asset into the editor's transaction buffer, which then references it and blocks a later delete
    # (ForceDeleteObject -> "package potentially corrupt"). Creation is ledgered via our own
    # create_asset op instead; the AssetTools call is registry-level, not a transactable edit.
    obj = at.create_asset(name, package_path, acls, fcls())
    if obj is None or not isinstance(obj, acls):
        if created_dir and EAL.does_directory_exist(package_path):
            try: EAL.delete_directory(package_path)
            except Exception: pass
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "create_asset returned %s for %s" % (type(obj).__name__, asset_path)}))
    else:
        _close_editors(obj)
        # Save on create: persists the asset AND makes the create_asset undo-delete reliable
        # (an unsaved just-created asset resists delete in the immediately-following call).
        try: EAL.save_asset(asset_path, only_if_is_dirty=False)
        except Exception: pass
        _ledger().append({"op": "create_asset", "asset_path": asset_path,
                          "package_path": package_path, "created_dir": created_dir})
        print("@@UMCP@@" + json.dumps({"status": "success", "name": obj.get_name(),
            "asset_path": asset_path, "object_path": obj.get_path_name(),
            "class": obj.get_class().get_name(), "kind": kind_label,
            "created_dir": created_dir, "ledger_depth": len(_ledger())}))
'''

    def _do_create(name, package_path, asset_class, factory, kind_label):
        params = {"name": name, "package_path": package_path,
                  "asset_class": asset_class, "factory": factory, "kind_label": kind_label}
        try:
            return json.dumps(_exec(_CREATE_SOUND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # create_sound_cue                                                    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def create_sound_cue(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new SoundCue asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new SoundCue (e.g. 'SC_Footstep').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid
                      mounted root ('/Game', '/Engine', a plugin root). Intermediate folders
                      are created as needed.

        Uses AssetTools.create_asset(..., unreal.SoundCue, unreal.SoundCueFactoryNew()) -- the
        factory has no ConfigureProperties dialog and the scripting path never invokes one -> no
        modal (bounded-probed live: create+delete round-trip clean). The cue is created EMPTY
        (first_node is None / no runtime SoundNode graph). Add a wave player with
        add_wave_player_to_cue; inspect with audio_read.get_sound_cue_info.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close any
        editors for the asset, delete it, and remove package_path if we created it and it is now
        empty (the same generic inverse used by create_blueprint / create_blackboard)."""
        return _do_create(name, package_path, "SoundCue", "SoundCueFactoryNew", "SoundCue")

    # ------------------------------------------------------------------ #
    # create_sound_class                                                  #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def create_sound_class(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new SoundClass asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new SoundClass (e.g. 'SClass_SFX').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid
                      mounted root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.SoundClass, unreal.SoundClassFactory()) (no dialog
        -> no modal; bounded-probed live). A fresh SoundClass ships with default mixer properties
        (volume/pitch 1.0, no parent/children). Inspect its reflected properties with
        editor_level.get_object_property / audio_read tooling.

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close
        editors + delete asset [+ rmdir if we created the dir]."""
        return _do_create(name, package_path, "SoundClass", "SoundClassFactory", "SoundClass")

    # ------------------------------------------------------------------ #
    # create_sound_mix                                                    #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def create_sound_mix(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new SoundMix (SoundMix modifier) asset non-interactively (NO modal).

        name:         asset name for the new SoundMix (e.g. 'SMix_Underwater').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid
                      mounted root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.SoundMix, unreal.SoundMixFactory()) (no dialog ->
        no modal; bounded-probed live). Created empty (no sound-class adjustments / EQ set).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close
        editors + delete asset [+ rmdir if we created the dir]."""
        return _do_create(name, package_path, "SoundMix", "SoundMixFactory", "SoundMix")

    # ------------------------------------------------------------------ #
    # create_sound_attenuation                                            #
    # ------------------------------------------------------------------ #
    @mcp.tool()
    def create_sound_attenuation(ctx, name: str, package_path: str = "/Game/MCP_Scratch") -> str:
        """Create a new SoundAttenuation asset non-interactively (NO modal / factory dialog).

        name:         asset name for the new SoundAttenuation (e.g. 'Att_3D_Default').
        package_path: content directory (default '/Game/MCP_Scratch'); must be under a valid
                      mounted root. Intermediate folders are created as needed.

        Uses AssetTools.create_asset(..., unreal.SoundAttenuation, unreal.SoundAttenuationFactory())
        (no dialog -> no modal; bounded-probed live). Ships with the default FSoundAttenuationSettings
        struct (attenuate=True, Linear falloff, spatialize=True). Inspect with
        audio_read.get_attenuation_info (which this build's create finally lets you demo on a real
        asset, since the project had zero SoundAttenuation assets before).

        Ledgered write op 'create_asset' {asset_path, package_path, created_dir}. Inverse: close
        editors + delete asset [+ rmdir if we created the dir]."""
        return _do_create(name, package_path, "SoundAttenuation", "SoundAttenuationFactory", "SoundAttenuation")

    # ------------------------------------------------------------------ #
    # add_wave_player_to_cue — attach a WavePlayer node as the cue root   #
    # ------------------------------------------------------------------ #
    _ADD_WAVE_PLAYER_BODY = _SW_HELPERS + r'''
cue_path = PARAMS["cue_path"]
wave_path = PARAMS["sound_wave_path"]
looping = bool(PARAMS.get("looping"))
cue, err = _load_typed(cue_path, "SoundCue")
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    wave, werr = _load_typed(wave_path, "SoundWave")
    if werr:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "sound_wave_path invalid: %s" % werr}))
    elif cue.get_editor_property("first_node") is not None:
        # Refuse a non-empty cue so the inverse (clear first_node -> None) stays FAITHFUL.
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "cue already has a first_node (%s); this reversible op only attaches to an EMPTY cue" % cue.get_editor_property("first_node").get_class().get_name(),
            "hint": "create a fresh cue with create_sound_cue, or clear its graph in the editor first"}))
    else:
        # SoundCue.construct_sound_node is not Python-exposed in 5.8; construct the node directly.
        # The node is owned by the cue package (outer=cue) so it persists with the asset.
        with unreal.ScopedEditorTransaction("MCP add_wave_player_to_cue"):
            node = unreal.new_object(unreal.SoundNodeWavePlayer, cue)
            node.set_editor_property("sound_wave_asset_ptr", wave)
            try: node.set_editor_property("looping", looping)
            except Exception: pass
            cue.set_editor_property("first_node", node)
        try: unreal.EditorAssetLibrary.save_asset(cue_path, only_if_is_dirty=False)
        except Exception: pass
        fn = cue.get_editor_property("first_node")
        fn_class = fn.get_class().get_name() if fn else None
        set_wave = None
        if fn is not None:
            sw = fn.get_editor_property("sound_wave_asset_ptr")
            set_wave = sw.get_path_name() if sw else None
        ok = fn is not None and set_wave == wave.get_path_name()
        if ok:
            _ledger().append({"op": "add_wave_player_to_cue", "asset_path": cue_path,
                              "object_path": cue.get_path_name()})
        print("@@UMCP@@" + json.dumps({"status": "success" if ok else "error",
            "cue": cue.get_name(), "cue_path": cue.get_path_name(),
            "first_node_class": fn_class, "sound_wave": set_wave, "looping": looping,
            "attached": ok, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_wave_player_to_cue(ctx, cue_path: str, sound_wave_path: str,
                               looping: bool = False) -> str:
        """Attach a USoundNodeWavePlayer (playing sound_wave_path) as a SoundCue's first_node.

        cue_path:        object/package path of an EMPTY SoundCue (first_node must be None). Use
                         create_sound_cue to make one. A cue that already has a graph is REFUSED,
                         so the inverse stays faithful (see below).
        sound_wave_path: object/package path of a SoundWave to play, e.g.
                         '/Engine/VREditor/Sounds/UI/Object_PickUp.Object_PickUp'.
        looping:         set the wave player's Looping flag (default False).

        SoundCue.construct_sound_node is not Python-exposed in 5.8; the node is built via
        unreal.new_object(unreal.SoundNodeWavePlayer, <cue>), its wave set through the reflected
        SoundWaveAssetPtr, and wired as the cue root via SoundCue.first_node. Verify with
        audio_read.get_sound_cue_info (graph -> first_node SoundNodeWavePlayer -> sound_wave).

        Ledgered NEW write op 'add_wave_player_to_cue' {asset_path, object_path}. Inverse: reload
        the cue, set first_node = None (clear the runtime graph root) and save. This is FAITHFUL
        because the command only runs on a cue whose prior first_node was None -> restoring to None
        exactly reproduces the prior state. (The orphaned wave-player subobject is GC'd; nothing
        else in the cue is touched.)"""
        params = {"cue_path": cue_path, "sound_wave_path": sound_wave_path, "looping": looping}
        try:
            return json.dumps(_exec(_ADD_WAVE_PLAYER_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # This module registers NO `undo` tool; editor_level.py owns the unified `undo`. The four
    # create_* commands reuse the generic folded "create_asset" inverse. add_wave_player_to_cue adds
    # ONE new op 'add_wave_player_to_cue' {asset_path, object_path} whose inverse (reload cue ->
    # set first_node=None -> save) is documented above for the coordinator to fold into
    # editor_level.undo. Reversibility was proven live (attach -> readback -> clear -> empty).
    # ------------------------------------------------------------------ #
