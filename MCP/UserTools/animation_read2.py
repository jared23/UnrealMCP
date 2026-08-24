"""UserTools :: Animation / Skeletal (READ, part 2)  (spec: docs/spec/anim.md)

Clean-room reimplementation over Unreal's public Python API (UE 5.8). READ-ONLY batch.
NO asset/anim editors are ever opened (no modals); nothing is mutated; no ledger. This is
the DETAIL companion to animation_read.get_anim_sequence_info (which surfaces notify NAMES
and curve NAMES only). Query convention + base64 PARAMS + Output-Log auto-capture are copied
verbatim from editor_level.py / animation_read.py (the gold standard).

Implemented (both read-only, no ledger, no mutation):
  - get_anim_notifies  — per-notify DETAIL for an AnimSequence / AnimMontage: name, kind
    (notify vs notify_state), class name + path, trigger time, duration (states), track index +
    track name, and best-effort notify-specific properties (export_text of the notify object).
  - get_anim_curves    — per-curve DETAIL: curve name, type (float/vector/transform), and — for
    FLOAT curves — key data (time/value pairs) plus value range. Vector/Transform curves are
    listed by name+type (no per-key Python reader exists for those in this build).

What IS reachable from stock Python here (verified live vs TestMCPSetup, UE 5.8.1):
  * AnimationLibrary.get_animation_notify_track_names(anim) -> ordered track names.
  * AnimationLibrary.get_animation_notify_events_for_track(anim, track) -> [AnimNotifyEvent];
    per event: get_anim_notify_event_trigger_time / get_anim_notify_event_duration, and editor
    properties 'notify_name', 'notify' (point-notify instance) / 'notify_state_class' (state
    instance). The instanced notify object's props are read best-effort via export_text().
  * AnimationLibrary.get_animation_curve_names(anim, RawCurveTrackTypes.RCT_FLOAT/RCT_VECTOR/
    RCT_TRANSFORM) -> curve names; get_float_keys(anim, name) -> (times[], values[]) for float
    curves (the only per-key reader exposed to Python in this build).

Known limits (reported honestly in payloads, NOT hidden):
  * Per-key interpolation / tangent metadata is not exposed by get_float_keys (times+values only).
  * Vector / Transform curves have no Python per-key getter here -> listed by name/type with a note.
  * A notify's per-property detail is surfaced as export_text (T3D-style) of the instanced notify
    object; it is truncated when very long (properties_truncated flag set).
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

    # Shared Unreal-side helpers. No triple-single-quote / no backslash inside.
    _ANIM_HELPERS = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
AL = unreal.AnimationLibrary
def _try(fn, d=None):
    try:
        return fn()
    except Exception:
        return d
def _load(path):
    if not path:
        return None, "no asset path given"
    obj = _try(lambda: unreal.EditorAssetLibrary.load_asset(path))
    if obj is None:
        return None, "asset not found or failed to load: %s" % path
    return obj, None
def _round(x, n=6):
    return round(float(x), n) if isinstance(x, (int, float)) else x
def _pnames(o):
    names = []; seen = set()
    for klass in type(o).__mro__:
        for name, val in vars(klass).items():
            if name.startswith("__") or name in seen:
                continue
            seen.add(name)
            if type(val).__name__ in ("getset_descriptor", "property"):
                names.append(name)
    return names
def _ser1(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return str(v)
    if isinstance(v, unreal.Vector):
        return [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
    if isinstance(v, unreal.Rotator):
        return [round(v.pitch, 4), round(v.yaw, 4), round(v.roll, 4)]
    if isinstance(v, (unreal.LinearColor, unreal.Color)):
        return [v.r, v.g, v.b, v.a]
    if isinstance(v, unreal.EnumBase):
        return str(v)
    if isinstance(v, unreal.Object):
        return {"__object__": _try(lambda: v.get_path_name()), "class": _try(lambda: v.get_class().get_name())}
    if isinstance(v, unreal.Array):
        return ("array[%d]" % len(v)) if len(v) else []
    if isinstance(v, (unreal.Map, unreal.Set)):
        return "%s[%d]" % (type(v).__name__, len(v))
    if isinstance(v, unreal.StructBase):
        return "<struct %s>" % type(v).__name__
    return str(v)[:200]
def _obj_props(inst, cap=60):
    out = {}
    for pn in _pnames(inst):
        if len(out) >= cap:
            out["__truncated__"] = True; break
        try:
            out[pn] = _ser1(inst.get_editor_property(pn))
        except Exception:
            pass
    return out
'''

    # ------------------------------------------------------------------ #
    # get_anim_notifies — per-notify DETAIL (name/class/time/dur/track)   #
    # ------------------------------------------------------------------ #
    _NOTIFIES_BODY = _ANIM_HELPERS + r'''
path = PARAMS.get("path")
track_filter = PARAMS.get("track")
want_props = PARAMS.get("include_properties")
want_props = True if want_props is None else bool(want_props)
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.AnimSequenceBase):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an AnimSequence/AnimMontage (got %s): %s. BlendSpaces have no notifies." % (obj.get_class().get_name(), path)}))
else:
    tracks = [str(t) for t in (_try(lambda: AL.get_animation_notify_track_names(obj), []) or [])]
    notifies = []
    for ti, tname in enumerate(tracks):
        if track_filter and tname != str(track_filter):
            continue
        events = _try(lambda: AL.get_animation_notify_events_for_track(obj, tname), []) or []
        for ev in events:
            rec = {"track_index": ti, "track_name": tname}
            rec["name"] = str(_try(lambda: ev.get_editor_property("notify_name")))
            rec["trigger_time"] = _round(_try(lambda: AL.get_anim_notify_event_trigger_time(ev)))
            dur = _try(lambda: AL.get_anim_notify_event_duration(ev))
            rec["duration"] = _round(dur)
            n = _try(lambda: ev.get_editor_property("notify"))
            nsc = _try(lambda: ev.get_editor_property("notify_state_class"))
            inst = None
            if n is not None:
                rec["kind"] = "notify"
                rec["is_state"] = False
                inst = n
            elif nsc is not None:
                rec["kind"] = "notify_state"
                rec["is_state"] = True
                inst = nsc
            else:
                rec["kind"] = "skeleton_notify"
                rec["is_state"] = False
                inst = None
                rec["note"] = ("no notify/notify_state_class object -- this is a bare skeleton notify "
                               "(name-only trigger with no notify class instance).")
            if inst is not None:
                cls = inst if isinstance(inst, unreal.Class) else _try(lambda: inst.get_class())
                rec["class"] = str(_try(lambda: cls.get_name())) if cls is not None else None
                rec["class_path"] = str(_try(lambda: cls.get_path_name())) if cls is not None else None
                if want_props and not isinstance(inst, unreal.Class):
                    props = _try(lambda: _obj_props(inst))
                    if isinstance(props, dict):
                        rec["properties"] = props
            else:
                rec["class"] = None
                rec["class_path"] = None
            notifies.append(rec)
    notifies.sort(key=lambda r: (r["track_index"], r["trigger_time"] if isinstance(r.get("trigger_time"), (int, float)) else 0.0))
    result = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name(),
              "track_names": tracks, "notify_count": len(notifies), "notifies": notifies}
    if track_filter:
        result["track_filter"] = str(track_filter)
        if str(track_filter) not in tracks:
            result["note"] = "track filter '%s' matched no existing track; existing tracks: %s" % (track_filter, tracks)
    result["fields_note"] = ("Per notify: track_index/track_name, name (event notify_name), kind "
                             "(notify | notify_state | skeleton_notify), trigger_time seconds, duration "
                             "seconds (>0 only for notify-states), class + class_path of the notify object, "
                             "and properties (reflected editor properties of the notify instance, capped "
                             "at 60). Point notifies report duration 0.")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_anim_notifies(ctx, path: str, track: str = None,
                          include_properties: bool = True) -> str:
        """Per-notify DETAIL for an AnimSequence / AnimMontage (loads it; no editor opened). Read-only.

        path:               AnimSequence or AnimMontage object path, e.g.
                            '/Game/Characters/Mannequins/Anims/Unarmed/MM_Idle.MM_Idle'.
        track:              optional notify-track name to restrict to (default: all tracks).
        include_properties: include each notify's reflected instance properties (default True).

        Returns notify_count and a 'notifies' list, one entry per notify/notify-state, sorted by
        (track_index, trigger_time). Each entry: name (the event's notify_name), kind
        ('notify' point event | 'notify_state' with duration | 'skeleton_notify' bare name),
        class + class_path of the notify object, trigger_time (s), duration (s; >0 only for
        notify-states), track_index + track_name, and (when include_properties) a 'properties'
        dict of the notify instance's reflected editor properties (capped at 60). This is the
        per-item detail beyond get_anim_sequence_info's notify_event_names list. Errors if the
        asset is missing or is not an AnimSequence/AnimMontage (BlendSpaces have no notifies)."""
        params = {"path": path, "track": track, "include_properties": include_properties}
        try:
            return json.dumps(_exec(_NOTIFIES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_anim_curves — per-curve DETAIL (name/type/keys/value range)     #
    # ------------------------------------------------------------------ #
    _CURVES_BODY = _ANIM_HELPERS + r'''
path = PARAMS.get("path")
want_keys = PARAMS.get("include_keys")
want_keys = True if want_keys is None else bool(want_keys)
max_keys = PARAMS.get("max_keys")
max_keys = int(max_keys) if max_keys else 1000
obj, err = _load(path)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
elif not isinstance(obj, unreal.AnimSequenceBase):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not an AnimSequence/AnimMontage (got %s): %s. BlendSpaces have no curves." % (obj.get_class().get_name(), path)}))
else:
    rct = getattr(unreal, "RawCurveTrackTypes", None)
    curves = []
    # FLOAT curves -- full per-key detail via get_float_keys
    if rct is not None:
        fnames = _try(lambda: [str(x) for x in (AL.get_animation_curve_names(obj, rct.RCT_FLOAT) or [])], []) or []
        for name in fnames:
            entry = {"name": name, "type": "float"}
            tv = _try(lambda: AL.get_float_keys(obj, name))
            if tv is not None:
                tk, vk = tv
                nkeys = min(len(tk), len(vk))
                entry["key_count"] = nkeys
                if nkeys:
                    vals = [float(vk[i]) for i in range(nkeys)]
                    entry["value_range"] = [_round(min(vals)), _round(max(vals))]
                    entry["time_range"] = [_round(float(tk[0])), _round(float(tk[nkeys - 1]))]
                    if want_keys:
                        keys = [{"time": _round(float(tk[i])), "value": _round(float(vk[i]))}
                                for i in range(min(nkeys, max_keys))]
                        entry["keys"] = keys
                        entry["keys_returned"] = len(keys)
                        entry["keys_capped"] = (nkeys > max_keys)
                else:
                    entry["value_range"] = None
                    entry["time_range"] = None
                    if want_keys:
                        entry["keys"] = []
                        entry["keys_returned"] = 0
                        entry["keys_capped"] = False
            else:
                entry["key_count"] = None
                entry["note"] = "get_float_keys failed for this curve"
            curves.append(entry)
        # VECTOR / TRANSFORM curves -- names/type only (no Python per-key reader here)
        for tname, rc in (("vector", getattr(rct, "RCT_VECTOR", None)), ("transform", getattr(rct, "RCT_TRANSFORM", None))):
            if rc is None:
                continue
            onames = _try(lambda: [str(x) for x in (AL.get_animation_curve_names(obj, rc) or [])], []) or []
            for name in onames:
                curves.append({"name": name, "type": tname, "key_count": None,
                               "note": ("%s-curve per-key data is not exposed to Python in this build "
                                        "(only float curves have get_float_keys); name/type only." % tname)})
    result = {"status": "success", "path": obj.get_path_name(), "class": obj.get_class().get_name(),
              "curve_count": len(curves), "curves": curves}
    result["counts_by_type"] = {}
    for c in curves:
        t = c["type"]
        result["counts_by_type"][t] = result["counts_by_type"].get(t, 0) + 1
    result["fields_note"] = ("Per curve: name, type (float | vector | transform), key_count. Float "
                             "curves additionally carry value_range [min,max], time_range [first,last], "
                             "and (when include_keys) a 'keys' list of {time,value} (capped at max_keys). "
                             "Per-key interpolation/tangent metadata is not exposed by get_float_keys. "
                             "Vector/Transform curves are name/type only (no Python per-key reader).")
    print("@@UMCP@@" + json.dumps(result))
'''

    @mcp.tool()
    def get_anim_curves(ctx, path: str, include_keys: bool = True,
                        max_keys: int = 1000) -> str:
        """Per-curve DETAIL for an AnimSequence / AnimMontage (loads it; no editor opened). Read-only.

        path:         AnimSequence or AnimMontage object path, e.g.
                      '/Game/Characters/Mannequins/Anims/Unarmed/MM_Idle.MM_Idle'.
        include_keys: include the {time,value} key list for each FLOAT curve (default True).
        max_keys:     cap the keys returned PER curve (default 1000); key_count is always the true
                      total and keys_capped flags when the list was capped.

        Returns curve_count, counts_by_type, and a 'curves' list. Each entry: name, type
        ('float' | 'vector' | 'transform'), and key_count. FLOAT curves additionally carry
        value_range [min,max], time_range [first,last], and (when include_keys) a 'keys' list of
        {time,value}. Vector/Transform curves are reported by name/type only (no per-key Python
        reader exists for those in this build). This is the per-item detail beyond
        get_anim_sequence_info's float_curve_names list. Errors if the asset is missing or is not
        an AnimSequence/AnimMontage."""
        params = {"path": path, "include_keys": include_keys, "max_keys": max_keys}
        try:
            return json.dumps(_exec(_CURVES_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
