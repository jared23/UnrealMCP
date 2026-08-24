"""UserTools :: Audio / reparent + effect-chain (WRITE)  (spec: docs/spec/audio.md, group G #38-40)

Wave 3 of the AUDIO buildout (UE 5.8, Windows-native). PURE PYTHON over the reflected
`unreal.*` API -- NO C++, NO build. The three general audio-asset RELATIONSHIP writers that sit
alongside sound_write.py (create_*), audio_asset_ops.py (create_audio_asset / dispatcher reads)
and audio_read.py:

  * set_submix_parent      -- reparent a USoundSubmix under another (or to root). HONEST PARTIAL:
                              the submix hierarchy props (parent_submix / child_submixes) are
                              EditConst -- managed only by the submix graph editor -- so they are
                              NOT writable via pure-Python reflection (verified live). The tool does
                              all reflection-side work (load / type-check / cycle-guard / prior
                              capture) then reports the EditConst limitation + resolution (a small
                              C++ direct-write handler); it does NOT fabricate a success.
  * set_sound_class_parent -- reparent a USoundClass. SoundClass hierarchy is defined by the PARENT's
                              `child_classes` TArray; the child's `parent_class` pointer auto-syncs
                              (engine PostEditChange -- verified live). We maintain both sides
                              explicitly (remove from old parent, add to new, set parent_class).
                              Cycle-guarded.
  * set_audio_effect_chain -- set the reflected `submix_effect_chain` TArray (of
                              USoundEffectSubmixPreset) on a USoundSubmix. Accepts a JSON list of
                              preset asset paths, or empty/None to clear.

Conventions copied VERBATIM from editor_level.py / sound_write.py: snippets print @@UMCP@@<json>
on ONE line; params are base64-injected via _exec; _query auto-captures new Output-Log
Warning/Error lines into result["_log_warnings"]; the per-session undo ledger lives in
builtins._UMCP_LEDGERS keyed by PARAMS["_session"]. Snippet bodies contain NO triple-single-quotes
and NO backslashes, and never clobber the wrapper's reserved locals.

Reflected property names + writability (confirmed LIVE on TestMCPSetup, UE 5.8.1):
  USoundSubmix : parent_submix  -> EDITCONST (owner SoundSubmixWithParentBase) -- NOT reflection-writable
                 child_submixes -> EDITCONST (owner SoundSubmixBase)           -- NOT reflection-writable
                 submix_effect_chain (Array, Edit/BlueprintReadOnly)           -> reflection-WRITABLE
  USoundClass  : child_classes (Array on the PARENT)  -> reflection-WRITABLE
                 parent_class  (pointer on the CHILD)  -> reflection-WRITABLE + auto-syncs when
                 the parent's child_classes is edited (engine PostEditChange).
  USoundEffectSubmixPreset : concrete subclasses (SubmixEffectReverbPreset, ...) are creatable via
                 AssetTools.create_asset(name, pkg, <PresetSubclass>, None) [factory=None] -- non-modal.

Reversibility / undo (this module registers NO `undo` tool; editor_level.py owns the unified one).
TWO NEW reversible ledger ops (from the two FULL tools), each inverse = re-apply the captured prior:
  * audio_set_sound_class_parent {child_path, prior_parent_path}
       inverse: set_sound_class_parent(child_path, prior_parent_path)  (prior None -> detach to root)
  * audio_set_effect_chain       {submix_path, prior_chain_paths}
       inverse: reload submix, set submix_effect_chain to the prior preset assets, save
Both capture the FAITHFUL prior (prior parent path / prior chain list) before mutating.
  * (audio_set_submix_parent is DEFINED for the day a C++ submix writer lands, but is NOT emitted by
     the pure-Python tool -- the EditConst wall means no mutation, hence no ledger entry, occurs.)

Modal-safety: no factory dialogs are invoked; all writes are property sets on already-loaded assets
+ EditorAssetLibrary.save_asset. Editors are closed before save. Each tool does ONE op per call.
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64. Never
# assign a snippet variable named sys/unreal/traceback/output_file/error_file/original_stdout/
# original_stderr/success/user_code/code_obj (the C++ wrapper's own locals).


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
def _load_inst(path, base):
    if not path:
        return None, "no asset path given"
    try:
        obj = unreal.EditorAssetLibrary.load_asset(path)
    except Exception as e:
        return None, "load failed: %s" % e
    if obj is None:
        return None, "asset not found: %s" % path
    if not isinstance(obj, base):
        return None, "asset is not a %s (got %s): %s" % (base.__name__, obj.get_class().get_name(), path)
    return obj, None
def _pn(o):
    try:
        return o.get_path_name() if o is not None else None
    except Exception:
        return None
def _same(a, b):
    return _pn(a) is not None and _pn(a) == _pn(b)
def _close_editors(obj):
    try:
        aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        if obj is not None:
            aes.close_all_editors_for_asset(obj)
    except Exception:
        pass
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
        return True
    except Exception:
        return False
'''

    # ------------------------------------------------------------------ #
    # set_submix_parent                                                   #
    # ------------------------------------------------------------------ #
    # HONEST-PARTIAL (verified live UE 5.8.1): USoundSubmix.parent_submix AND
    # SoundSubmixBase.child_submixes are BOTH flagged EditConst (owner SoundSubmixWithParentBase /
    # SoundSubmixBase) -- the submix hierarchy is managed exclusively by the submix graph editor,
    # which sets the members directly in C++ and rebuilds. Python reflection REFUSES both
    # (set_editor_property and set_editor_properties -> "read-only and cannot be set"), and there is
    # NO BlueprintCallable submix-parenting function (AudioMixerLibrary's submix functions are
    # RUNTIME effect-chain overrides on a live audio device, not persistent asset edits). So the
    # parent edit is NOT achievable in pure Python; it needs a tiny C++ direct-write handler (assign
    # ParentSubmix + ChildSubmixes, mark packages dirty) like the graph editor does. This tool does
    # all the reflection-side work it can (load, type-check, cycle-guard, prior capture) and then
    # reports the exact EditConst limitation instead of faking a success or leaving partial state.
    _SUBMIX_PARENT_BODY = _HELPERS + r'''
submix_path = PARAMS["submix_path"]
parent_path = PARAMS.get("parent_path")
sm, err = _load_inst(submix_path, unreal.SoundSubmixBase)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    new_parent = None
    bad = False
    if parent_path:
        new_parent, perr = _load_inst(parent_path, unreal.SoundSubmixBase)
        if perr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent_path invalid: %s" % perr}))
            bad = True
    if bad:
        pass
    elif new_parent is not None and _same(new_parent, sm):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "cannot parent a submix to itself: %s" % submix_path}))
    else:
        # cycle guard: walk up from new_parent via parent_submix; refuse if we reach sm
        cycle = False
        seen = set()
        cur = new_parent
        while cur is not None:
            if _same(cur, sm):
                cycle = True; break
            k = _pn(cur)
            if k in seen:
                break
            seen.add(k)
            try:
                cur = cur.get_editor_property("parent_submix")
            except Exception:
                cur = None
        if cycle:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing: parenting %s under %s would create a cycle" % (submix_path, parent_path)}))
        else:
            # capture prior parent (for a would-be inverse) BEFORE attempting any mutation
            old_parent = None
            try:
                old_parent = sm.get_editor_property("parent_submix")
            except Exception:
                old_parent = None
            prior_parent_path = _pn(old_parent)
            # Attempt the reflected write. In UE 5.8 parent_submix/child_submixes are EditConst -> this
            # raises immediately, before any state changes (so there is NO partial mutation to unwind).
            wrote = False
            write_err = None
            try:
                sm.set_editor_property("parent_submix", new_parent)
                wrote = True
            except Exception as e:
                write_err = str(e)
            if wrote:
                # (unreachable on 5.8 stock; kept correct-by-construction if a future engine relaxes
                #  EditConst) -- reconcile child arrays, save, ledger.
                touched = [sm]
                if new_parent is not None:
                    try:
                        nkids = [c for c in (new_parent.get_editor_property("child_submixes") or []) if not _same(c, sm)]
                        nkids.append(sm)
                        new_parent.set_editor_property("child_submixes", nkids)
                        touched.append(new_parent)
                    except Exception:
                        pass
                for o in touched:
                    _close_editors(o); _save(_pn(o))
                entry = {"op": "audio_set_submix_parent", "submix_path": _pn(sm),
                         "prior_parent_path": prior_parent_path}
                _ledger().append(entry)
                print("@@UMCP@@" + json.dumps({"status": "success",
                    "submix": sm.get_name(), "submix_path": _pn(sm),
                    "prior_parent_path": prior_parent_path, "new_parent_path": _pn(new_parent),
                    "readback_parent_submix": _pn(sm.get_editor_property("parent_submix")),
                    "property_used": "parent_submix (+ child_submixes reconcile)",
                    "ledger_entry": entry, "ledger_depth": len(_ledger())}))
            else:
                print("@@UMCP@@" + json.dumps({"status": "unsupported",
                    "submix": sm.get_name(), "submix_path": _pn(sm),
                    "current_parent_path": prior_parent_path,
                    "requested_parent_path": _pn(new_parent),
                    "validated": {"types_ok": True, "cycle_guarded": True, "prior_captured": prior_parent_path},
                    "reason": "USoundSubmix.parent_submix and SoundSubmixBase.child_submixes are EditConst (managed by the submix graph editor); not writable via pure-Python reflection.",
                    "engine_error": (write_err or "")[:200],
                    "resolution": "needs a small C++ direct-write handler (assign ParentSubmix + ChildSubmixes + MarkPackageDirty), as the submix graph schema does. All reflection-side prerequisites (load/type-check/cycle-guard/prior-capture) succeeded.",
                    "property_used": "parent_submix / child_submixes (EditConst)"}))
'''

    # set_submix_parent is SUPERSEDED by the C++-backed tool in audio_cpp.py (C++ #47, which CAN write the
    # EditConst parent_submix/child_submixes). The @mcp.tool() decorator is removed here so this pure-Python
    # EditConst stub does NOT register (it would collide on the same tool name). Body kept as dead code.
    def _superseded_set_submix_parent(ctx, submix_path: str, parent_path: str = None) -> str:
        """Reparent a USoundSubmix under another submix, or detach it to root.

        submix_path: object/package path of the SoundSubmix to move.
        parent_path: object/package path of the new parent SoundSubmix, or None/empty to clear
                     the parent (make it a root submix).

        HONEST PARTIAL (UE 5.8): the submix hierarchy properties `parent_submix` and
        `child_submixes` are flagged EditConst -- managed only by the submix graph editor's C++ --
        so they are NOT writable via pure-Python reflection (verified live: set_editor_property and
        set_editor_properties both refuse; there is no BlueprintCallable submix-parenting function).
        This tool performs every reflection-side step it can (load, type-check, self/cycle guard,
        prior-parent capture) and then returns status 'unsupported' with the exact EditConst reason
        and the resolution (a small C++ direct-write handler), rather than fabricating a success.
        No mutation and no ledger entry occur on the unsupported path.

        Would-be ledger op (once a C++ writer exists) 'audio_set_submix_parent'
        {submix_path, prior_parent_path}; inverse: set_submix_parent(submix_path, prior_parent_path)."""
        params = {"submix_path": submix_path, "parent_path": parent_path}
        try:
            return json.dumps(_exec(_SUBMIX_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_sound_class_parent                                              #
    # ------------------------------------------------------------------ #
    _SC_PARENT_BODY = _HELPERS + r'''
child_path = PARAMS["child_path"]
parent_path = PARAMS.get("parent_path")
child, err = _load_inst(child_path, unreal.SoundClass)
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    new_parent = None
    bad = False
    if parent_path:
        new_parent, perr = _load_inst(parent_path, unreal.SoundClass)
        if perr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "parent_path invalid: %s" % perr}))
            bad = True
    if bad:
        pass
    elif new_parent is not None and _same(new_parent, child):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "cannot parent a sound class to itself: %s" % child_path}))
    else:
        # cycle guard: walk up from new_parent via parent_class; refuse if we reach child
        cycle = False
        seen = set()
        cur = new_parent
        while cur is not None:
            if _same(cur, child):
                cycle = True; break
            k = _pn(cur)
            if k in seen:
                break
            seen.add(k)
            try:
                cur = cur.get_editor_property("parent_class")
            except Exception:
                cur = None
        if cycle:
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "refusing: parenting %s under %s would create a cycle" % (child_path, parent_path)}))
        else:
            # capture prior parent BEFORE mutating (faithful inverse) -- SoundClass parent lives
            # canonically as the child's parent_class pointer (auto-synced from child_classes).
            old_parent = None
            try:
                old_parent = child.get_editor_property("parent_class")
            except Exception:
                old_parent = None
            prior_parent_path = _pn(old_parent)
            touched = [child]
            # 1) remove child from OLD parent's child_classes
            if old_parent is not None and not _same(old_parent, new_parent):
                try:
                    okids = [c for c in (old_parent.get_editor_property("child_classes") or []) if not _same(c, child)]
                    old_parent.set_editor_property("child_classes", okids)
                    touched.append(old_parent)
                except Exception:
                    pass
            # 2) add child to NEW parent's child_classes (dedupe)
            if new_parent is not None:
                try:
                    nkids = [c for c in (new_parent.get_editor_property("child_classes") or []) if not _same(c, child)]
                    nkids.append(child)
                    new_parent.set_editor_property("child_classes", nkids)
                    touched.append(new_parent)
                except Exception:
                    pass
            # 3) set the child's parent_class pointer explicitly (clears to None when detaching)
            try:
                child.set_editor_property("parent_class", new_parent)
            except Exception:
                pass
            for o in touched:
                _close_editors(o)
                _save(_pn(o))
            rb = None
            try:
                rb = _pn(child.get_editor_property("parent_class"))
            except Exception:
                rb = None
            entry = {"op": "audio_set_sound_class_parent", "child_path": _pn(child),
                     "prior_parent_path": prior_parent_path}
            _ledger().append(entry)
            print("@@UMCP@@" + json.dumps({"status": "success",
                "child": child.get_name(), "child_path": _pn(child),
                "prior_parent_path": prior_parent_path,
                "new_parent_path": _pn(new_parent),
                "readback_parent_class": rb,
                "parent_child_classes": ([c.get_name() for c in (new_parent.get_editor_property("child_classes") or [])] if new_parent is not None else None),
                "property_used": "child_classes (parent) + parent_class (child)",
                "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_sound_class_parent(ctx, child_path: str, parent_path: str = None) -> str:
        """Reparent a USoundClass under another sound class, or detach it to root. Ledgered write.

        child_path:  object/package path of the SoundClass to move.
        parent_path: object/package path of the new parent SoundClass, or None/empty to detach
                     (clear the parent).

        SoundClass hierarchy is defined by the PARENT's `child_classes` TArray; the child's
        `parent_class` pointer auto-syncs (engine PostEditChange -- confirmed live). This maintains
        BOTH sides explicitly: removes the child from the old parent's child_classes, adds it to the
        new parent's child_classes, and sets the child's parent_class pointer. Refuses a self-parent
        or a cycle (walks the new parent's parent_class chain).

        Ledgered NEW write op 'audio_set_sound_class_parent' {child_path, prior_parent_path}.
        Inverse: re-run set_sound_class_parent(child_path, prior_parent_path) -- removes from the
        current parent's child_classes and restores the prior parent + save."""
        params = {"child_path": child_path, "parent_path": parent_path}
        try:
            return json.dumps(_exec(_SC_PARENT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # set_audio_effect_chain                                              #
    # ------------------------------------------------------------------ #
    _EFFECT_CHAIN_BODY = _HELPERS + r'''
submix_path = PARAMS["submix_path"]
preset_paths = PARAMS.get("effect_preset_paths")
if preset_paths is None:
    preset_paths = []
if not isinstance(preset_paths, list):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "effect_preset_paths must be a JSON list of preset asset paths (or null/empty to clear)"}))
else:
    sm, err = _load_inst(submix_path, unreal.SoundSubmix)
    if err:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        # resolve + type-check each preset asset
        resolved = []
        bad = None
        for p in preset_paths:
            po, perr = _load_inst(p, unreal.SoundEffectSubmixPreset)
            if perr:
                bad = "effect preset invalid (%s): %s" % (p, perr)
                break
            resolved.append(po)
        if bad is not None:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": bad,
                "hint": "each path must load a USoundEffectSubmixPreset (e.g. SubmixEffectReverbPreset)"}))
        else:
            # capture prior chain (list of preset paths) BEFORE mutating (faithful inverse)
            prior_chain_paths = []
            try:
                for e in (sm.get_editor_property("submix_effect_chain") or []):
                    prior_chain_paths.append(_pn(e))
            except Exception:
                prior_chain_paths = []
            # set the new chain
            new_arr = unreal.Array(unreal.SoundEffectSubmixPreset)
            for po in resolved:
                new_arr.append(po)
            sm.set_editor_property("submix_effect_chain", new_arr)
            _close_editors(sm)
            _save(_pn(sm))
            # read back
            readback = []
            try:
                for e in (sm.get_editor_property("submix_effect_chain") or []):
                    readback.append(_pn(e))
            except Exception:
                readback = None
            entry = {"op": "audio_set_effect_chain", "submix_path": _pn(sm),
                     "prior_chain_paths": prior_chain_paths}
            _ledger().append(entry)
            print("@@UMCP@@" + json.dumps({"status": "success",
                "submix": sm.get_name(), "submix_path": _pn(sm),
                "prior_chain_paths": prior_chain_paths,
                "new_chain_paths": [_pn(po) for po in resolved],
                "readback_chain_paths": readback,
                "chain_length": len(resolved),
                "property_used": "submix_effect_chain",
                "ledger_entry": entry, "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def set_audio_effect_chain(ctx, submix_path: str, effect_preset_paths: str = None) -> str:
        """Set the submix effect chain (USoundSubmix.submix_effect_chain) on a SoundSubmix.
        Ledgered write.

        submix_path:         object/package path of the SoundSubmix.
        effect_preset_paths: a JSON list of USoundEffectSubmixPreset asset paths (e.g.
                             '["/Game/MCP_Scratch/Rev.Rev"]'), or None/empty ('[]') to CLEAR the
                             chain. Each path must load a USoundEffectSubmixPreset (concrete
                             subclasses like SubmixEffectReverbPreset / SubmixEffectFilterPreset;
                             create one non-modally via AssetTools.create_asset(name, pkg,
                             unreal.SubmixEffectReverbPreset, None)).

        Captures the prior chain (list of preset paths) then replaces submix_effect_chain and saves.
        Ledgered NEW write op 'audio_set_effect_chain' {submix_path, prior_chain_paths}. Inverse:
        reload the submix, rebuild submix_effect_chain from the prior preset assets, and save."""
        parsed = effect_preset_paths
        if isinstance(effect_preset_paths, str):
            s = effect_preset_paths.strip()
            if s == "" or s.lower() == "none" or s.lower() == "null":
                parsed = []
            else:
                try:
                    parsed = json.loads(s)
                except Exception:
                    # allow a single bare path or comma-separated list
                    parsed = [x.strip() for x in s.split(",") if x.strip()]
        if parsed is None:
            parsed = []
        if not isinstance(parsed, list):
            parsed = [parsed]
        params = {"submix_path": submix_path, "effect_preset_paths": parsed}
        try:
            return json.dumps(_exec(_EFFECT_CHAIN_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # NO `undo` tool here. editor_level.py owns the unified `undo`. The three NEW ops
    # (audio_set_submix_parent / audio_set_sound_class_parent / audio_set_effect_chain) each carry a
    # faithful prior capture; their inverses are documented above for the coordinator to fold into
    # editor_level.undo. Verified live (set + readback + restore-prior round-trips).
    # ------------------------------------------------------------------ #
