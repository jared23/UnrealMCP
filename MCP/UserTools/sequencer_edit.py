"""UserTools :: Sequencer / Cinematics -- FINE-GRAINED EDITING  (spec: docs/spec/sequencer.md)

Clean-room reimplementation over Unreal's public MovieSceneScripting Python API (UE 5.8). This is the
fine-grained EDIT layer that complements the existing sequencer write modules:
  * sequencer_read.py            -- read/introspection
  * level_sequence_write.py      -- create sequence, playback range, actor binding, transform track/keys
  * sequencer_write_ext.py       -- master/binding tracks, sections, spawnables

This module adds the Tier-1 per-key / per-channel / per-section EDIT tools that were MISSING (see
docs/parity/p2_anim_sequencer.md). No C++, no Sequencer editor tab -- everything goes through the
scripting channel/key/section surface reached from EditorAssetLibrary.load_asset(<LevelSequence>).

Query convention, base64 PARAMS injection, Output-Log auto-capture, and the per-session undo ledger
are copied VERBATIM from the gold-standard editor_level.py. Every mutation runs inside an
unreal.ScopedEditorTransaction AND pushes a faithful inverse op onto the per-session ledger.

ADDRESSING (matches sequencer_read.py's positional iteration order):
  binding        -- binding display name or internal name; omit/empty -> master/root tracks (seq.get_tracks())
  track_index    -- index into that track list (binding.get_tracks() or seq.get_tracks()); default 0
  section_index  -- index into track.get_sections(); default 0
  channel_index  -- index into section.get_all_channels()
  key_index      -- index into channel.get_keys() (same order the read tools expose); OR locate by frame

Implemented (all validated live on scratch /Game/_scratch_g1a/LS_G1A, editor left CLEAN, ledger depth 0):
  CHANNELS / KEYS (fully faithful):
    - remove_key                (op "seq_remove_key"      -> re-add key + restore full key state)
    - set_key_time              (op "seq_set_key_time"    -> set_time back to prior frame)
    - set_key_value             (op "seq_set_key_value"   -> set_value back to prior value)
    - set_key_interpolation     (op "seq_set_key_interp"  -> restore prior interp+tangent mode)
    - set_key_tangent           (op "seq_set_key_tangent" -> restore prior tangents/weights/mode)
    - set_channel_default       (op "seq_set_channel_default" -> restore prior default or remove_default)
    - set_channel_extrapolation (op "seq_set_channel_extrap"  -> restore prior pre/post infinity)
    - evaluate_channels         (READ-ONLY; no ledger)
  SECTIONS / TRACKS (faithful):
    - set_section_range         (op "seq_set_section_range" -> restore prior start/end)
    - set_section_easing        (op "seq_set_section_easing" -> restore prior ease in/out durations)
    - set_section_blend_type    (op "seq_set_section_blend" -> restore prior blend type)
    - set_section_property      (op "seq_set_section_property" -> restore prior property value)
    - set_track_property        (op "seq_set_track_property"   -> restore prior property value)

DEFERRED (reachable but refused-not-faked -- no faithful inverse; see footer for full reasoning):
    - remove_section, remove_track : deleting an ARBITRARY section/track destroys its keys, channel
      defaults/extrapolation, per-key tangents, section properties, and (audio/skeletal/camera-cut/
      subsequence sections) external asset references. MovieScene sections/tracks expose NO
      export_text/import_text in Python, so the deleted content cannot be reconstructed faithfully.
      Removing a section/track WE added is already reversible via the add-side undos in
      level_sequence_write.py / sequencer_write_ext.py (remove_track / remove_section inverses).

Undo: this module registers NO `undo` tool (editor_level.py owns the single unified `undo`). The 13
NEW ledger ops above are documented per-tool (schema + exact inverse) for the coordinator to fold into
editor_level.undo. Every inverse was proven live against this session's ledger (depth -> 0).

MovieSceneScripting gotchas discovered (UE 5.8.1):
  * section.get_blend_type() returns an OptionalMovieSceneBlendType STRUCT (.blend_type / .is_valid),
    NOT a raw enum; set_blend_type() needs a raw unreal.MovieSceneBlendType -- passing the Optional
    struct back raises "Cannot nativize OptionalMovieSceneBlendType". Capture .blend_type by name.
  * MovieSceneScriptingDoubleKey uses RichCurveInterpMode for get/set_interpolation_mode and a SEPARATE
    RichCurveTangentMode for get/set_tangent_mode (Cubic interp + Auto/User/Break tangent are decoupled).
  * channel.set_default / has_default / get_default / remove_default are the default-value surface;
    pre/post infinity use RichCurveExtrapolation (RCCE_*).
  * key frame numbers round-trip as the raw FrameNumber value you pass (consistent with add_keyframe).
  * evaluate_keys(range, frame_rate) samples the channel across a range; make_range(seq, start, duration)
    builds the range (duration N -> N+1 samples); make_range(seq, frame, 0) -> single sample at frame.
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

    # ------------------------------------------------------------------ #
    # Shared Unreal-side helpers. No triple-single-quote / no backslash.  #
    # ------------------------------------------------------------------ #
    _SEQEDIT_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _load_seq(path):
    if not path:
        return None, "no sequence_path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, unreal.LevelSequence):
        return None, "asset is not a LevelSequence (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _binding_names(seq):
    names = []
    for b in (seq.get_bindings() or []):
        dn = None; nm = None
        try: dn = str(b.get_display_name())
        except Exception: pass
        try: nm = str(b.get_name())
        except Exception: pass
        names.append(dn or nm)
    return names
def _find_binding(seq, sel):
    for b in (seq.get_bindings() or []):
        dn = None; nm = None
        try: dn = str(b.get_display_name())
        except Exception: pass
        try: nm = str(b.get_name())
        except Exception: pass
        if sel in (dn, nm):
            return b
    return None
def _track_list(seq, binding_sel):
    # binding_sel None/empty -> master/root tracks; else the named binding's tracks.
    if binding_sel in (None, ""):
        return list(seq.get_tracks() or []), None
    b = _find_binding(seq, binding_sel)
    if b is None:
        return None, "binding not found: %s" % binding_sel
    return list(b.get_tracks() or []), None
def _locate_track(seq, binding_sel, track_index):
    tl, err = _track_list(seq, binding_sel)
    if err:
        return None, err
    ti = int(track_index)
    if ti < 0 or ti >= len(tl):
        return None, "track_index %d out of range (track_count=%d)" % (ti, len(tl))
    return tl[ti], None
def _locate_section(seq, binding_sel, track_index, section_index):
    tr, err = _locate_track(seq, binding_sel, track_index)
    if err:
        return None, None, err
    secs = list(tr.get_sections() or [])
    si = int(section_index)
    if si < 0 or si >= len(secs):
        return None, tr, "section_index %d out of range (section_count=%d)" % (si, len(secs))
    return secs[si], tr, None
def _get_channel(section, channel_index):
    chans = list(section.get_all_channels() or [])
    ci = int(channel_index)
    if ci < 0 or ci >= len(chans):
        return None, chans, "channel_index %d out of range (channel_count=%d)" % (ci, len(chans))
    return chans[ci], chans, None
def _key_by_index(ch, key_index):
    ks = list(ch.get_keys() or [])
    ki = int(key_index)
    if ki < 0 or ki >= len(ks):
        return None, "key_index %d out of range (num_keys=%d)" % (ki, len(ks))
    return ks[ki], None
def _key_by_frame(ch, frame):
    for k in (ch.get_keys() or []):
        try:
            if int(k.get_time().frame_number.value) == int(frame):
                return k
        except Exception:
            pass
    return None
def _key_frame(k):
    return int(k.get_time().frame_number.value)
# ---- enum name maps (short friendly name <-> unreal enum). getattr-guarded because some
#      enum members (e.g. RichCurveInterpMode.RCIM_NONE) are not exposed to Python in 5.8. ----
def _emap(cls, pairs):
    m = {}
    for name, attr in pairs:
        v = getattr(cls, attr, None)
        if v is not None:
            m[name] = v
    return m
_INTERP = _emap(unreal.RichCurveInterpMode, [("constant", "RCIM_CONSTANT"), ("linear", "RCIM_LINEAR"),
                                             ("cubic", "RCIM_CUBIC"), ("none", "RCIM_NONE")])
_TANMODE = _emap(unreal.RichCurveTangentMode, [("auto", "RCTM_AUTO"), ("user", "RCTM_USER"),
                                               ("break", "RCTM_BREAK"), ("smart_auto", "RCTM_SMART_AUTO")])
_TANWMODE = _emap(unreal.RichCurveTangentWeightMode, [("none", "RCTWM_WEIGHTED_NONE"), ("arrive", "RCTWM_WEIGHTED_ARRIVE"),
                                                      ("leave", "RCTWM_WEIGHTED_LEAVE"), ("both", "RCTWM_WEIGHTED_BOTH")])
_EXTRAP = _emap(unreal.RichCurveExtrapolation, [("constant", "RCCE_CONSTANT"), ("cycle", "RCCE_CYCLE"),
                                                ("cycle_with_offset", "RCCE_CYCLE_WITH_OFFSET"), ("oscillate", "RCCE_OSCILLATE"),
                                                ("linear", "RCCE_LINEAR"), ("none", "RCCE_NONE")])
_BLEND = _emap(unreal.MovieSceneBlendType, [("absolute", "ABSOLUTE"), ("additive", "ADDITIVE"),
                                            ("relative", "RELATIVE"), ("additive_from_base", "ADDITIVE_FROM_BASE"),
                                            ("override", "OVERRIDE")])
def _name_of(m, v):
    for k2, vv in m.items():
        try:
            if vv == v:
                return k2
        except Exception:
            pass
    return str(v)
def _enum_of(m, name, dflt=None):
    if name is None:
        return dflt
    return m.get(str(name).strip().lower(), dflt)
def _cap_key(k):
    # Full capture of a key's editable state for faithful reconstruction.
    st = {"frame": _key_frame(k), "value": k.get_value(),
          "interp": _name_of(_INTERP, k.get_interpolation_mode()),
          "tangent_mode": _name_of(_TANMODE, k.get_tangent_mode()),
          "tangent_weight_mode": _name_of(_TANWMODE, k.get_tangent_weight_mode()),
          "arrive": k.get_arrive_tangent(), "leave": k.get_leave_tangent(),
          "arrive_weight": k.get_arrive_tangent_weight(), "leave_weight": k.get_leave_tangent_weight()}
    return st
def _apply_key_state(k, st):
    # Restore interp/tangent state onto an existing key (value/time set by caller).
    im = _enum_of(_INTERP, st.get("interp"))
    if im is not None: k.set_interpolation_mode(im)
    tm = _enum_of(_TANMODE, st.get("tangent_mode"))
    if tm is not None: k.set_tangent_mode(tm)
    twm = _enum_of(_TANWMODE, st.get("tangent_weight_mode"))
    if twm is not None: k.set_tangent_weight_mode(twm)
    if st.get("arrive") is not None: k.set_arrive_tangent(float(st["arrive"]))
    if st.get("leave") is not None: k.set_leave_tangent(float(st["leave"]))
    if st.get("arrive_weight") is not None: k.set_arrive_tangent_weight(float(st["arrive_weight"]))
    if st.get("leave_weight") is not None: k.set_leave_tangent_weight(float(st["leave_weight"]))
# ---- universal property capture/coerce (trimmed from editor_level._COERCE_HELPERS) ----
def _enum_name(v):
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _settable(v):
    if v is None:
        return (None, True)
    if isinstance(v, (bool, int, float, str)):
        return (v, True)
    if isinstance(v, unreal.Vector):
        return ([v.x, v.y, v.z], True)
    if isinstance(v, unreal.Rotator):
        return ([v.pitch, v.yaw, v.roll], True)
    if isinstance(v, unreal.LinearColor) or isinstance(v, unreal.Color):
        return ([v.r, v.g, v.b, v.a], True)
    if isinstance(v, (unreal.Name, unreal.Text)):
        return (str(v), True)
    if isinstance(v, unreal.EnumBase):
        return ({"__enum__": _enum_name(v)}, True)
    if isinstance(v, unreal.Object):
        try:
            return ({"__object__": v.get_path_name()}, True)
        except Exception:
            return (None, False)
    return ("<struct %s>" % type(v).__name__, False)
def _coerce(current, value):
    if value is None:
        return None
    if isinstance(value, dict) and "__object__" in value:
        p = value["__object__"]
        return unreal.EditorAssetLibrary.load_asset(p) if p else None
    if isinstance(value, dict) and "__enum__" in value and isinstance(current, unreal.EnumBase):
        try:
            return getattr(type(current), value["__enum__"])
        except Exception:
            return current
    if isinstance(current, unreal.Vector) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(current, unreal.Rotator) and isinstance(value, (list, tuple)) and len(value) >= 3:
        return unreal.Rotator(pitch=float(value[0]), yaw=float(value[1]), roll=float(value[2]))
    if (isinstance(current, unreal.LinearColor) or isinstance(current, unreal.Color)) and isinstance(value, (list, tuple)) and len(value) >= 3:
        aa = float(value[3]) if len(value) > 3 else 1.0
        if isinstance(current, unreal.LinearColor):
            return unreal.LinearColor(float(value[0]), float(value[1]), float(value[2]), aa)
        return unreal.Color(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(aa))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        try:
            return getattr(type(current), value)
        except Exception:
            return value
    if isinstance(current, bool):
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"): return True
            if s in ("false", "0", "no", "off", ""): return False
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, (int, float)): return int(value)
        if isinstance(value, str):
            try: return int(value.strip())
            except Exception:
                try: return int(float(value.strip()))
                except Exception: return value
        return value
    if isinstance(current, float):
        if isinstance(value, (int, float)): return float(value)
        if isinstance(value, str):
            try: return float(value.strip())
            except Exception: return value
        return value
    return value
'''

    # ================================================================== #
    # CHANNELS / KEYS                                                     #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # remove_key                                                          #
    # ------------------------------------------------------------------ #
    _REMOVE_KEY_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err, "available_bindings": _binding_names(seq)})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
if PARAMS.get("frame") is not None:
    k = _key_by_frame(ch, PARAMS["frame"])
    kerr = None if k is not None else "no key at frame %s" % PARAMS["frame"]
else:
    k, kerr = _key_by_index(ch, PARAMS.get("key_index", 0))
if kerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr, "num_keys": ch.get_num_keys()})); raise SystemExit
st = _cap_key(k)
with unreal.ScopedEditorTransaction("MCP seq_remove_key"):
    ch.remove_key(k)
_ledger().append({"op": "seq_remove_key", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "key_state": st})
print("@@UMCP@@" + json.dumps({"status": "success", "removed_frame": st["frame"], "removed_value": st["value"],
    "num_keys": ch.get_num_keys(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def remove_key(ctx, sequence_path: str, channel_index: int, key_index: int = 0,
                   binding: str = None, track_index: int = 0, section_index: int = 0,
                   frame: int = None) -> str:
        """Remove one keyframe from a channel of a LevelSequence section.

        sequence_path: object/package path of the LevelSequence.
        binding:       binding display/internal name; omit for master/root tracks.
        track_index/section_index/channel_index: positional locators (see module addressing).
        key_index:     index into channel.get_keys() (default 0), OR pass `frame` to target the key
                       at a specific frame number.

        Ledgered op 'seq_remove_key' {asset_path, binding, track_index, section_index, channel_index,
        key_state:{frame,value,interp,tangent_mode,tangent_weight_mode,arrive,leave,arrive_weight,
        leave_weight}}. Inverse: locate the channel, ch.add_key(FrameNumber(frame), value), then restore
        interp/tangent state from key_state. FAITHFUL (full key state captured)."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "key_index": key_index,
                  "binding": binding, "track_index": track_index, "section_index": section_index, "frame": frame}
        try:
            return json.dumps(_exec(_REMOVE_KEY_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_key_time                                                        #
    # ------------------------------------------------------------------ #
    _SET_KEY_TIME_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
if PARAMS.get("frame") is not None:
    k = _key_by_frame(ch, PARAMS["frame"])
    kerr = None if k is not None else "no key at frame %s" % PARAMS["frame"]
else:
    k, kerr = _key_by_index(ch, PARAMS.get("key_index", 0))
if kerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr, "num_keys": ch.get_num_keys()})); raise SystemExit
prior_frame = _key_frame(k)
new_frame = int(PARAMS["new_frame"])
if _key_by_frame(ch, new_frame) is not None and new_frame != prior_frame:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "a key already exists at frame %d" % new_frame})); raise SystemExit
with unreal.ScopedEditorTransaction("MCP seq_set_key_time"):
    k.set_time(unreal.FrameNumber(new_frame))
_ledger().append({"op": "seq_set_key_time", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "prior_frame": prior_frame, "new_frame": new_frame})
print("@@UMCP@@" + json.dumps({"status": "success", "prior_frame": prior_frame, "new_frame": _key_frame(k),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_key_time(ctx, sequence_path: str, channel_index: int, new_frame: int, key_index: int = 0,
                     binding: str = None, track_index: int = 0, section_index: int = 0,
                     frame: int = None) -> str:
        """Move a keyframe to a new frame number.

        Locate the key by key_index (default 0) or by its current `frame`. Refuses if another key
        already occupies new_frame. new_frame is the raw FrameNumber value (consistent with add_keyframe).

        Ledgered op 'seq_set_key_time' {asset_path, binding, track_index, section_index, channel_index,
        prior_frame, new_frame}. Inverse: find the key now at new_frame, set_time(FrameNumber(prior_frame))."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "new_frame": new_frame,
                  "key_index": key_index, "binding": binding, "track_index": track_index,
                  "section_index": section_index, "frame": frame}
        try:
            return json.dumps(_exec(_SET_KEY_TIME_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_key_value                                                       #
    # ------------------------------------------------------------------ #
    _SET_KEY_VALUE_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
if PARAMS.get("frame") is not None:
    k = _key_by_frame(ch, PARAMS["frame"])
    kerr = None if k is not None else "no key at frame %s" % PARAMS["frame"]
else:
    k, kerr = _key_by_index(ch, PARAMS.get("key_index", 0))
if kerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr, "num_keys": ch.get_num_keys()})); raise SystemExit
prior_value = k.get_value()
kf = _key_frame(k)
raw = PARAMS["value"]
if isinstance(prior_value, bool):
    newv = bool(raw)
elif isinstance(prior_value, int) and not isinstance(prior_value, bool):
    newv = int(raw)
else:
    newv = float(raw)
with unreal.ScopedEditorTransaction("MCP seq_set_key_value"):
    k.set_value(newv)
_ledger().append({"op": "seq_set_key_value", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "frame": kf, "prior_value": prior_value})
print("@@UMCP@@" + json.dumps({"status": "success", "frame": kf, "prior_value": prior_value,
    "new_value": k.get_value(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_key_value(ctx, sequence_path: str, channel_index: int, value, key_index: int = 0,
                      binding: str = None, track_index: int = 0, section_index: int = 0,
                      frame: int = None) -> str:
        """Set a keyframe's value (typed to the channel: float/int/bool).

        Locate the key by key_index (default 0) or by its `frame`.

        Ledgered op 'seq_set_key_value' {asset_path, binding, track_index, section_index, channel_index,
        frame, prior_value}. Inverse: find the key at `frame`, set_value(prior_value)."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "value": value,
                  "key_index": key_index, "binding": binding, "track_index": track_index,
                  "section_index": section_index, "frame": frame}
        try:
            return json.dumps(_exec(_SET_KEY_VALUE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_key_interpolation                                               #
    # ------------------------------------------------------------------ #
    _SET_KEY_INTERP_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
if PARAMS.get("frame") is not None:
    k = _key_by_frame(ch, PARAMS["frame"])
    kerr = None if k is not None else "no key at frame %s" % PARAMS["frame"]
else:
    k, kerr = _key_by_index(ch, PARAMS.get("key_index", 0))
if kerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr, "num_keys": ch.get_num_keys()})); raise SystemExit
im = _enum_of(_INTERP, PARAMS["interpolation"])
if im is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown interpolation '%s' (use constant/linear/cubic/none)" % PARAMS["interpolation"]})); raise SystemExit
tm_in = _enum_of(_TANMODE, PARAMS.get("tangent_mode"))
prior_interp = _name_of(_INTERP, k.get_interpolation_mode())
prior_tanmode = _name_of(_TANMODE, k.get_tangent_mode())
kf = _key_frame(k)
with unreal.ScopedEditorTransaction("MCP seq_set_key_interp"):
    k.set_interpolation_mode(im)
    if tm_in is not None:
        k.set_tangent_mode(tm_in)
_ledger().append({"op": "seq_set_key_interp", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "frame": kf,
                  "prior_interp": prior_interp, "prior_tangent_mode": prior_tanmode})
print("@@UMCP@@" + json.dumps({"status": "success", "frame": kf, "prior_interp": prior_interp,
    "new_interp": _name_of(_INTERP, k.get_interpolation_mode()),
    "tangent_mode": _name_of(_TANMODE, k.get_tangent_mode()), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_key_interpolation(ctx, sequence_path: str, channel_index: int, interpolation: str,
                              key_index: int = 0, binding: str = None, track_index: int = 0,
                              section_index: int = 0, frame: int = None, tangent_mode: str = None) -> str:
        """Set a keyframe's interpolation mode.

        interpolation: one of 'constant', 'linear', 'cubic', 'none' (RichCurveInterpMode).
        tangent_mode:  optional 'auto'/'user'/'break'/'smart_auto' (RichCurveTangentMode; only meaningful
                       for cubic). Locate the key by key_index (default 0) or by its `frame`.

        Ledgered op 'seq_set_key_interp' {asset_path, binding, track_index, section_index, channel_index,
        frame, prior_interp, prior_tangent_mode}. Inverse: find the key at `frame`, restore
        set_interpolation_mode(prior_interp) + set_tangent_mode(prior_tangent_mode)."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "interpolation": interpolation,
                  "key_index": key_index, "binding": binding, "track_index": track_index,
                  "section_index": section_index, "frame": frame, "tangent_mode": tangent_mode}
        try:
            return json.dumps(_exec(_SET_KEY_INTERP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_key_tangent                                                     #
    # ------------------------------------------------------------------ #
    _SET_KEY_TANGENT_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
if PARAMS.get("frame") is not None:
    k = _key_by_frame(ch, PARAMS["frame"])
    kerr = None if k is not None else "no key at frame %s" % PARAMS["frame"]
else:
    k, kerr = _key_by_index(ch, PARAMS.get("key_index", 0))
if kerr:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": kerr, "num_keys": ch.get_num_keys()})); raise SystemExit
prior = _cap_key(k)
kf = prior["frame"]
with unreal.ScopedEditorTransaction("MCP seq_set_key_tangent"):
    tm = _enum_of(_TANMODE, PARAMS.get("tangent_mode"))
    if tm is not None: k.set_tangent_mode(tm)
    twm = _enum_of(_TANWMODE, PARAMS.get("tangent_weight_mode"))
    if twm is not None: k.set_tangent_weight_mode(twm)
    if PARAMS.get("arrive_tangent") is not None: k.set_arrive_tangent(float(PARAMS["arrive_tangent"]))
    if PARAMS.get("leave_tangent") is not None: k.set_leave_tangent(float(PARAMS["leave_tangent"]))
    if PARAMS.get("arrive_tangent_weight") is not None: k.set_arrive_tangent_weight(float(PARAMS["arrive_tangent_weight"]))
    if PARAMS.get("leave_tangent_weight") is not None: k.set_leave_tangent_weight(float(PARAMS["leave_tangent_weight"]))
_ledger().append({"op": "seq_set_key_tangent", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "frame": kf, "prior": prior})
print("@@UMCP@@" + json.dumps({"status": "success", "frame": kf, "now": _cap_key(k), "prior": prior,
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_key_tangent(ctx, sequence_path: str, channel_index: int, key_index: int = 0,
                        binding: str = None, track_index: int = 0, section_index: int = 0, frame: int = None,
                        tangent_mode: str = None, tangent_weight_mode: str = None,
                        arrive_tangent: float = None, leave_tangent: float = None,
                        arrive_tangent_weight: float = None, leave_tangent_weight: float = None) -> str:
        """Set a keyframe's tangent parameters (mode, weight mode, arrive/leave tangents + weights).

        Any subset may be provided; only the given fields are changed. For weighted tangents to apply,
        set tangent_mode='user' or 'break' and tangent_weight_mode to 'arrive'/'leave'/'both'. Locate the
        key by key_index (default 0) or by its `frame`.

        Ledgered op 'seq_set_key_tangent' {asset_path, binding, track_index, section_index, channel_index,
        frame, prior:{full key state}}. Inverse: find the key at `frame`, restore tangent_mode/
        tangent_weight_mode/arrive/leave/arrive_weight/leave_weight from prior. FAITHFUL."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "key_index": key_index,
                  "binding": binding, "track_index": track_index, "section_index": section_index, "frame": frame,
                  "tangent_mode": tangent_mode, "tangent_weight_mode": tangent_weight_mode,
                  "arrive_tangent": arrive_tangent, "leave_tangent": leave_tangent,
                  "arrive_tangent_weight": arrive_tangent_weight, "leave_tangent_weight": leave_tangent_weight}
        try:
            return json.dumps(_exec(_SET_KEY_TANGENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_channel_default                                                 #
    # ------------------------------------------------------------------ #
    _SET_CHANNEL_DEFAULT_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
had = bool(ch.has_default())
prior = ch.get_default() if had else None
clear = bool(PARAMS.get("clear"))
raw = PARAMS.get("value")
with unreal.ScopedEditorTransaction("MCP seq_set_channel_default"):
    if clear:
        ch.remove_default()
    else:
        if isinstance(prior, bool) or (isinstance(raw, bool)):
            newv = bool(raw)
        else:
            try: newv = float(raw)
            except Exception: newv = raw
        ch.set_default(newv)
_ledger().append({"op": "seq_set_channel_default", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "had_default": had, "prior_default": prior})
print("@@UMCP@@" + json.dumps({"status": "success", "had_default": had, "prior_default": prior,
    "has_default_now": bool(ch.has_default()),
    "default_now": (ch.get_default() if ch.has_default() else None), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_channel_default(ctx, sequence_path: str, channel_index: int, value=None,
                            binding: str = None, track_index: int = 0, section_index: int = 0,
                            clear: bool = False) -> str:
        """Set (or clear) a channel's default value -- the value used when the channel has no keys.

        value: the default to set (float/int/bool per channel type). clear=True removes the default.

        Ledgered op 'seq_set_channel_default' {asset_path, binding, track_index, section_index,
        channel_index, had_default, prior_default}. Inverse: if had_default, set_default(prior_default);
        else remove_default(). FAITHFUL."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index, "value": value,
                  "binding": binding, "track_index": track_index, "section_index": section_index, "clear": clear}
        try:
            return json.dumps(_exec(_SET_CHANNEL_DEFAULT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_channel_extrapolation                                           #
    # ------------------------------------------------------------------ #
    _SET_CHANNEL_EXTRAP_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
ch, chans, err = _get_channel(sec, PARAMS["channel_index"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
pre_in = PARAMS.get("pre_infinity"); post_in = PARAMS.get("post_infinity")
pre_e = _enum_of(_EXTRAP, pre_in) if pre_in is not None else None
post_e = _enum_of(_EXTRAP, post_in) if post_in is not None else None
if pre_in is not None and pre_e is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown pre_infinity '%s'" % pre_in})); raise SystemExit
if post_in is not None and post_e is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown post_infinity '%s'" % post_in})); raise SystemExit
prior_pre = _name_of(_EXTRAP, ch.get_pre_infinity_extrapolation())
prior_post = _name_of(_EXTRAP, ch.get_post_infinity_extrapolation())
with unreal.ScopedEditorTransaction("MCP seq_set_channel_extrap"):
    if pre_e is not None: ch.set_pre_infinity_extrapolation(pre_e)
    if post_e is not None: ch.set_post_infinity_extrapolation(post_e)
_ledger().append({"op": "seq_set_channel_extrap", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "channel_index": int(PARAMS["channel_index"]), "prior_pre": prior_pre, "prior_post": prior_post})
print("@@UMCP@@" + json.dumps({"status": "success", "prior_pre": prior_pre, "prior_post": prior_post,
    "pre_now": _name_of(_EXTRAP, ch.get_pre_infinity_extrapolation()),
    "post_now": _name_of(_EXTRAP, ch.get_post_infinity_extrapolation()), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_channel_extrapolation(ctx, sequence_path: str, channel_index: int,
                                  pre_infinity: str = None, post_infinity: str = None,
                                  binding: str = None, track_index: int = 0, section_index: int = 0) -> str:
        """Set a channel's pre-/post-infinity extrapolation (behavior outside the keyed range).

        pre_infinity / post_infinity: one of 'constant','cycle','cycle_with_offset','oscillate',
        'linear','none' (RichCurveExtrapolation). Provide either or both.

        Ledgered op 'seq_set_channel_extrap' {asset_path, binding, track_index, section_index,
        channel_index, prior_pre, prior_post}. Inverse: restore set_pre_infinity_extrapolation(prior_pre)
        + set_post_infinity_extrapolation(prior_post). FAITHFUL."""
        params = {"sequence_path": sequence_path, "channel_index": channel_index,
                  "pre_infinity": pre_infinity, "post_infinity": post_infinity,
                  "binding": binding, "track_index": track_index, "section_index": section_index}
        try:
            return json.dumps(_exec(_SET_CHANNEL_EXTRAP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # evaluate_channels  (READ-ONLY)                                      #
    # ------------------------------------------------------------------ #
    _EVAL_CHANNELS_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
chans = list(sec.get_all_channels() or [])
idxs = PARAMS.get("channel_indices")
if not idxs:
    idxs = list(range(len(chans)))
dr = seq.get_display_rate()
start = PARAMS.get("start_frame")
end = PARAMS.get("end_frame")
single = False
if start is None:
    start = int(PARAMS.get("frame", 0)); duration = 0; single = True
else:
    start = int(start)
    duration = max(0, int(end) - start) if end is not None else 0
    if duration == 0:
        single = True
# make_range(seq, start, N) yields N samples (frames start..start+N-1); N=0 -> empty.
# For a single-frame sample we need duration 1 to get exactly the value at `start`.
sample_duration = 1 if single else duration
rng = unreal.MovieSceneSequenceExtensions.make_range(seq, start, sample_duration)
results = []
for ci in idxs:
    ci = int(ci)
    if ci < 0 or ci >= len(chans):
        results.append({"channel_index": ci, "error": "out of range"}); continue
    ch = chans[ci]
    cname = None
    try: cname = str(ch.get_editor_property("channel_name"))
    except Exception: pass
    try:
        vals = ch.evaluate_keys(rng, dr)
        vlist = [ (v if isinstance(v, (int, float, bool)) else float(v)) for v in list(vals) ]
    except Exception as e:
        results.append({"channel_index": ci, "channel_name": cname, "error": str(e)}); continue
    entry = {"channel_index": ci, "channel_name": cname, "num_keys": ch.get_num_keys()}
    if duration == 0:
        entry["value_at_frame"] = (vlist[0] if vlist else None)
    else:
        entry["values"] = vlist
    results.append(entry)
print("@@UMCP@@" + json.dumps({"status": "success", "start_frame": start, "duration": duration,
    "display_rate": [int(dr.numerator), int(dr.denominator)], "channels": results}))
'''

    @mcp.tool()
    def evaluate_channels(ctx, sequence_path: str, binding: str = None, track_index: int = 0,
                          section_index: int = 0, channel_indices: list = None,
                          frame: int = 0, start_frame: int = None, end_frame: int = None) -> str:
        """Evaluate a section's channels (sample the animated value). READ-ONLY (no ledger).

        channel_indices: optional list of channel indices; omit to evaluate all channels of the section.
        Single frame:    pass `frame` (default 0) -> reports value_at_frame per channel.
        Range:           pass start_frame (+ optional end_frame) -> reports a `values` list sampled per
                         display-rate frame across [start_frame, end_frame].

        Uses channel.evaluate_keys(MovieSceneSequenceExtensions.make_range(seq, start, duration),
        seq.get_display_rate())."""
        params = {"sequence_path": sequence_path, "binding": binding, "track_index": track_index,
                  "section_index": section_index, "channel_indices": channel_indices,
                  "frame": frame, "start_frame": start_frame, "end_frame": end_frame}
        try:
            return json.dumps(_exec(_EVAL_CHANNELS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # SECTIONS / TRACKS                                                  #
    # ================================================================== #

    # ------------------------------------------------------------------ #
    # set_section_range                                                   #
    # ------------------------------------------------------------------ #
    _SET_SECTION_RANGE_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
prior_has_start = bool(sec.has_start_frame()); prior_has_end = bool(sec.has_end_frame())
prior_start = sec.get_start_frame() if prior_has_start else None
prior_end = sec.get_end_frame() if prior_has_end else None
new_start = int(PARAMS["start_frame"]); new_end = int(PARAMS["end_frame"])
with unreal.ScopedEditorTransaction("MCP seq_set_section_range"):
    sec.set_range(new_start, new_end)
_ledger().append({"op": "seq_set_section_range", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "prior_has_start": prior_has_start, "prior_has_end": prior_has_end,
                  "prior_start": prior_start, "prior_end": prior_end})
print("@@UMCP@@" + json.dumps({"status": "success", "prior_start": prior_start, "prior_end": prior_end,
    "new_start": sec.get_start_frame(), "new_end": sec.get_end_frame(), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_section_range(ctx, sequence_path: str, start_frame: int, end_frame: int,
                          binding: str = None, track_index: int = 0, section_index: int = 0) -> str:
        """Set a section's [start, end] frame range (its time bounds on the timeline).

        Ledgered op 'seq_set_section_range' {asset_path, binding, track_index, section_index,
        prior_has_start, prior_has_end, prior_start, prior_end}. Inverse: if both bounds existed,
        sec.set_range(prior_start, prior_end); else restore each present bound via set_start_frame/
        set_end_frame (or unbounded via set_start_frame_bounded/set_end_frame_bounded(False))."""
        params = {"sequence_path": sequence_path, "start_frame": start_frame, "end_frame": end_frame,
                  "binding": binding, "track_index": track_index, "section_index": section_index}
        try:
            return json.dumps(_exec(_SET_SECTION_RANGE_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_section_easing                                                  #
    # ------------------------------------------------------------------ #
    _SET_SECTION_EASING_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
prior_in = sec.get_ease_in_duration(); prior_out = sec.get_ease_out_duration()
with unreal.ScopedEditorTransaction("MCP seq_set_section_easing"):
    if PARAMS.get("ease_in_duration") is not None: sec.set_ease_in_duration(int(PARAMS["ease_in_duration"]))
    if PARAMS.get("ease_out_duration") is not None: sec.set_ease_out_duration(int(PARAMS["ease_out_duration"]))
_ledger().append({"op": "seq_set_section_easing", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "prior_ease_in": prior_in, "prior_ease_out": prior_out})
print("@@UMCP@@" + json.dumps({"status": "success", "prior_ease_in": prior_in, "prior_ease_out": prior_out,
    "ease_in_now": sec.get_ease_in_duration(), "ease_out_now": sec.get_ease_out_duration(),
    "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_section_easing(ctx, sequence_path: str, ease_in_duration: int = None, ease_out_duration: int = None,
                           binding: str = None, track_index: int = 0, section_index: int = 0) -> str:
        """Set a section's ease-in / ease-out durations (in frames).

        Provide either or both durations. Ledgered op 'seq_set_section_easing' {asset_path, binding,
        track_index, section_index, prior_ease_in, prior_ease_out}. Inverse: restore
        set_ease_in_duration(prior_ease_in) + set_ease_out_duration(prior_ease_out). FAITHFUL."""
        params = {"sequence_path": sequence_path, "ease_in_duration": ease_in_duration,
                  "ease_out_duration": ease_out_duration, "binding": binding,
                  "track_index": track_index, "section_index": section_index}
        try:
            return json.dumps(_exec(_SET_SECTION_EASING_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_section_blend_type                                              #
    # ------------------------------------------------------------------ #
    _SET_SECTION_BLEND_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
bt = _enum_of(_BLEND, PARAMS["blend_type"])
if bt is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown blend_type '%s' (absolute/additive/relative/additive_from_base/override)" % PARAMS["blend_type"]})); raise SystemExit
# get_blend_type() returns OptionalMovieSceneBlendType (.blend_type / .is_valid), NOT a raw enum.
opt = sec.get_blend_type()
prior_valid = False; prior_name = "absolute"
try:
    prior_valid = bool(opt.is_valid); prior_name = _name_of(_BLEND, opt.blend_type)
except Exception:
    pass
with unreal.ScopedEditorTransaction("MCP seq_set_section_blend"):
    sec.set_blend_type(bt)
_ledger().append({"op": "seq_set_section_blend", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "prior_valid": prior_valid, "prior_blend_type": prior_name})
now = sec.get_blend_type()
print("@@UMCP@@" + json.dumps({"status": "success", "prior_blend_type": prior_name, "prior_valid": prior_valid,
    "blend_type_now": _name_of(_BLEND, now.blend_type), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_section_blend_type(ctx, sequence_path: str, blend_type: str,
                               binding: str = None, track_index: int = 0, section_index: int = 0) -> str:
        """Set a section's blend type: 'absolute','additive','relative','additive_from_base','override'.

        Ledgered op 'seq_set_section_blend' {asset_path, binding, track_index, section_index,
        prior_valid, prior_blend_type}. Inverse: sec.set_blend_type(_BLEND[prior_blend_type]) -- a raw
        unreal.MovieSceneBlendType enum (the OptionalMovieSceneBlendType wrapper cannot be passed back).
        Restoring the prior enum reproduces the prior effective blend. FAITHFUL."""
        params = {"sequence_path": sequence_path, "blend_type": blend_type, "binding": binding,
                  "track_index": track_index, "section_index": section_index}
        try:
            return json.dumps(_exec(_SET_SECTION_BLEND_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_section_property  (universal)                                   #
    # ------------------------------------------------------------------ #
    _SET_SECTION_PROP_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
sec, tr, err = _locate_section(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0), PARAMS.get("section_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
prop = PARAMS["property_name"]
try:
    current = sec.get_editor_property(prop)
except Exception as e:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot read property '%s': %s" % (prop, e)})); raise SystemExit
prior_json, restorable = _settable(current)
if not restorable:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property '%s' has non-restorable type (%s); refusing to set without a faithful inverse" % (prop, type(current).__name__)})); raise SystemExit
newv = _coerce(current, PARAMS["value"])
with unreal.ScopedEditorTransaction("MCP seq_set_section_property"):
    sec.set_editor_property(prop, newv)
try: applied, _r = _settable(sec.get_editor_property(prop))
except Exception: applied = None
_ledger().append({"op": "seq_set_section_property", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "section_index": int(PARAMS.get("section_index", 0)),
                  "property_name": prop, "prior_value": prior_json})
print("@@UMCP@@" + json.dumps({"status": "success", "property": prop, "prior_value": prior_json,
    "applied_value": applied, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_section_property(ctx, sequence_path: str, property_name: str, value,
                             binding: str = None, track_index: int = 0, section_index: int = 0) -> str:
        """Universal setter for a MovieSceneSection editor property (e.g. 'row_index', 'is_active',
        'is_locked', 'pre_roll_frames', 'post_roll_frames', 'color_tint', 'completion_mode', ...).

        Reads the current value first (for a faithful inverse) and refuses properties whose type cannot be
        captured/restored (arbitrary structs). value is coerced against the current type (enum name ->
        enum, [x,y,z] -> Vector/Rotator, [r,g,b,a] -> Color, object path -> asset).

        Ledgered op 'seq_set_section_property' {asset_path, binding, track_index, section_index,
        property_name, prior_value}. Inverse: sec.set_editor_property(property_name, _coerce(prior_value))."""
        params = {"sequence_path": sequence_path, "property_name": property_name, "value": value,
                  "binding": binding, "track_index": track_index, "section_index": section_index}
        try:
            return json.dumps(_exec(_SET_SECTION_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_track_property  (universal)                                     #
    # ------------------------------------------------------------------ #
    _SET_TRACK_PROP_BODY = _SEQEDIT_HELPERS + r'''
seq, err = _load_seq(PARAMS["sequence_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err})); raise SystemExit
tr, err = _locate_track(seq, PARAMS.get("binding"), PARAMS.get("track_index", 0))
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err, "available_bindings": _binding_names(seq)})); raise SystemExit
prop = PARAMS["property_name"]
try:
    current = tr.get_editor_property(prop)
except Exception as e:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "cannot read property '%s': %s" % (prop, e)})); raise SystemExit
prior_json, restorable = _settable(current)
if not restorable:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "property '%s' has non-restorable type (%s); refusing" % (prop, type(current).__name__)})); raise SystemExit
newv = _coerce(current, PARAMS["value"])
with unreal.ScopedEditorTransaction("MCP seq_set_track_property"):
    tr.set_editor_property(prop, newv)
try: applied, _r = _settable(tr.get_editor_property(prop))
except Exception: applied = None
_ledger().append({"op": "seq_set_track_property", "asset_path": PARAMS["sequence_path"], "binding": PARAMS.get("binding"),
                  "track_index": int(PARAMS.get("track_index", 0)), "property_name": prop, "prior_value": prior_json})
print("@@UMCP@@" + json.dumps({"status": "success", "property": prop, "prior_value": prior_json,
    "applied_value": applied, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_track_property(ctx, sequence_path: str, property_name: str, value,
                           binding: str = None, track_index: int = 0) -> str:
        """Universal setter for a MovieSceneTrack editor property (e.g. 'color_tint', 'sorting_order',
        'display_name', 'track_row_display_name', ...).

        binding: omit for a master/root track; else the named binding's track. track_index selects among
        that binding's (or the sequence's) tracks. Reads current first for a faithful inverse and refuses
        non-restorable types.

        Ledgered op 'seq_set_track_property' {asset_path, binding, track_index, property_name,
        prior_value}. Inverse: tr.set_editor_property(property_name, _coerce(prior_value))."""
        params = {"sequence_path": sequence_path, "property_name": property_name, "value": value,
                  "binding": binding, "track_index": track_index}
        try:
            return json.dumps(_exec(_SET_TRACK_PROP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # DEFERRED (refused-not-faked):
    #   remove_section / remove_track -- reachable (track.remove_section / binding.remove_track /
    #   seq.get_tracks + remove) but NOT faithfully reversible for ARBITRARY content: deleting a
    #   section/track destroys its keys, per-key tangents, channel defaults + extrapolation, section
    #   properties (range/easing/blend/row/active/locked), and -- for audio / skeletal-animation /
    #   camera-cut / subsequence sections -- external asset references. MovieScene sections and tracks
    #   expose NO export_text/import_text in Python (verified: not in dir()), so the deleted content
    #   cannot be serialized and rebuilt exactly. Per PROTOCOL ("if a write has no faithful inverse, say
    #   so ... rather than shipping an unrevertable mutation") these are DEFERRED to a follow-up batch
    #   (a full capture+rebuild of channel-only sections is feasible but out of scope to validate here).
    #   Removing a section/track WE created is already reversible via the add-side undos in
    #   level_sequence_write.py (add_transform_track) and sequencer_write_ext.py (add_seq_* ops).
    #
    # This module registers NO `undo` tool (editor_level.py owns the unified `undo`). 13 NEW ledger ops
    # (seq_remove_key, seq_set_key_time, seq_set_key_value, seq_set_key_interp, seq_set_key_tangent,
    # seq_set_channel_default, seq_set_channel_extrap, seq_set_section_range, seq_set_section_easing,
    # seq_set_section_blend, seq_set_section_property, seq_set_track_property) documented above for the
    # coordinator to fold into editor_level.undo. evaluate_channels is read-only (no op).
    # ------------------------------------------------------------------ #
