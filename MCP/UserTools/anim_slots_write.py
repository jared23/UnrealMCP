"""UserTools :: Animation skeleton-SLOT authoring (WRITE)  (spec: docs/spec/animation.md)

Clean-room Python wiring for the C++ #18 USkeleton slot-registry handlers on
unreal.MCPReflectionLibrary (GROUP 2). The slot registry (RegisterSlotNode / SetSlotGroupName /
AddSlotGroupName / RemoveSlotName / RemoveSlotGroup / RenameSlotName) is reached only from C++, so
each tool here resolves the USkeleton via unreal.load_asset and calls the hasattr-guarded reflection
handler; the handler captures PRIOR state and returns it as JSON so we can push a faithful reversible
inverse onto the per-session undo ledger.

Query convention, base64 PARAMS injection, Output-Log auto-capture, and the per-session ledger are
copied VERBATIM from the gold-standard editor_level.py / editor_levels.py / anim_write.py.

Implemented:
  - get_anim_slots          (READ; get_skeleton_slots_json)
  - add_anim_slot           (WRITE; op "add_anim_slot"; captures existed + prior_group)
  - remove_anim_slot        (WRITE; op "remove_anim_slot"; captures prior_group)
  - rename_anim_slot        (WRITE; op "rename_anim_slot")
  - add_anim_slot_group     (WRITE; op "add_anim_slot_group"; captures added flag)
  - remove_anim_slot_group  (WRITE; op "remove_anim_slot_group"; captures prior_slots[])

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). The op
inverses below are reported to the coordinator to fold into editor_level.undo:
  add_anim_slot          -> if existed: add_skeleton_slot(slot, prior_group) (restore group);
                            else: remove_skeleton_slot(slot).
  remove_anim_slot       -> add_skeleton_slot(slot, prior_group).
  rename_anim_slot       -> rename_skeleton_slot(new_name, old_name).
  add_anim_slot_group    -> if added: remove_skeleton_slot_group(group); else no-op.
  remove_anim_slot_group -> add_skeleton_slot_group(group) + add_skeleton_slot(s, group) for each prior slot.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet
bodies here contain NO triple-single-quote and NO stray backslashes; all data crosses as base64.
Never assign a snippet local named sys/unreal/traceback/output_file/error_file/original_stdout/
original_stderr/success/user_code/code_obj (the wrapper's own names).
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


# Shared Unreal-side helpers (prepended to bodies). No triple-single-quote / no backslash inside.
_SLOTS_HELPERS = r'''
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
def _load_skel(path):
    if not path:
        return None, "no skeleton path given"
    obj = None
    try:
        obj = unreal.load_asset(path)
    except Exception as e:
        return None, "failed to load: %s (%s)" % (path, str(e)[:120])
    if obj is None:
        return None, "skeleton not found: %s" % path
    if not isinstance(obj, unreal.Skeleton):
        return None, "asset is not a Skeleton (got %s): %s" % (obj.get_class().get_name(), path)
    return obj, None
def _save(path):
    try:
        unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)
    except Exception:
        pass
def _reflib(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
'''


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
    # get_anim_slots — READ the skeleton slot registry                   #
    # ------------------------------------------------------------------ #
    _GET_SLOTS_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("get_skeleton_slots_json")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.get_skeleton_slots_json unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        res = json.loads(rl.get_skeleton_slots_json(skel))
        res["skeleton_path"] = PARAMS["skeleton_path"]
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def get_anim_slots(ctx, skeleton_path: str) -> str:
        """Read a USkeleton's anim montage-slot registry (C++ #18 handler get_skeleton_slots_json).
        READ-only — not ledgered.

        skeleton_path: object path of a Skeleton asset, e.g.
                       '/Game/Characters/Mannequins/Meshes/SK_Mannequin.SK_Mannequin'.

        Returns the groups (each with group_name + slots[] + slot_count) plus group_count / slot_count."""
        try:
            return json.dumps(_exec(_GET_SLOTS_BODY, {"skeleton_path": skeleton_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_anim_slot — register a slot into a group (creates group)       #
    # ------------------------------------------------------------------ #
    _ADD_SLOT_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("add_skeleton_slot")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.add_skeleton_slot unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        with unreal.ScopedEditorTransaction("MCP add_anim_slot"):
            res = json.loads(rl.add_skeleton_slot(skel, PARAMS["slot_name"], PARAMS.get("group_name") or ""))
        if res.get("status") != "success":
            print("@@UMCP@@" + json.dumps(res))
        else:
            _save(PARAMS["skeleton_path"])
            _ledger().append({"op": "add_anim_slot", "skeleton_path": PARAMS["skeleton_path"],
                              "slot_name": PARAMS["slot_name"], "existed": bool(res.get("existed")),
                              "prior_group": res.get("prior_group")})
            res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def add_anim_slot(ctx, skeleton_path: str, slot_name: str, group_name: str = "") -> str:
        """Register a montage slot on a Skeleton, placing it in group_name (C++ #18 add_skeleton_slot).
        An empty group_name uses the engine DefaultGroup. If the slot already exists it is moved to the
        new group (its prior group is captured for undo). Ledgered, reversible.

        skeleton_path: object path of a Skeleton asset.
        slot_name:     the slot to register (e.g. 'MCP_TestSlot').
        group_name:    slot group to place it under; '' => DefaultGroup. Group is created if missing.

        Ledgered op 'add_anim_slot' {skeleton_path, slot_name, existed, prior_group}; inverse (folded
        into editor_level.undo): if existed -> add_skeleton_slot(slot, prior_group) to restore the
        original group; else remove_skeleton_slot(slot)."""
        params = {"skeleton_path": skeleton_path, "slot_name": slot_name, "group_name": group_name}
        try:
            return json.dumps(_exec(_ADD_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_anim_slot — unregister a slot (captures prior group)        #
    # ------------------------------------------------------------------ #
    _REMOVE_SLOT_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("remove_skeleton_slot")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.remove_skeleton_slot unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        with unreal.ScopedEditorTransaction("MCP remove_anim_slot"):
            res = json.loads(rl.remove_skeleton_slot(skel, PARAMS["slot_name"]))
        if res.get("status") != "success":
            print("@@UMCP@@" + json.dumps(res))
        else:
            _save(PARAMS["skeleton_path"])
            _ledger().append({"op": "remove_anim_slot", "skeleton_path": PARAMS["skeleton_path"],
                              "slot_name": PARAMS["slot_name"], "prior_group": res.get("prior_group")})
            res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def remove_anim_slot(ctx, skeleton_path: str, slot_name: str) -> str:
        """Unregister a montage slot from a Skeleton (C++ #18 remove_skeleton_slot). The slot's prior
        group is captured so undo re-adds it into the same group. Ledgered, reversible.

        skeleton_path: object path of a Skeleton asset.
        slot_name:     the slot to remove (refused if not present).

        Ledgered op 'remove_anim_slot' {skeleton_path, slot_name, prior_group}; inverse (folded into
        editor_level.undo): add_skeleton_slot(slot, prior_group)."""
        params = {"skeleton_path": skeleton_path, "slot_name": slot_name}
        try:
            return json.dumps(_exec(_REMOVE_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # rename_anim_slot — rename a slot (group preserved)                 #
    # ------------------------------------------------------------------ #
    _RENAME_SLOT_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("rename_skeleton_slot")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.rename_skeleton_slot unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        with unreal.ScopedEditorTransaction("MCP rename_anim_slot"):
            res = json.loads(rl.rename_skeleton_slot(skel, PARAMS["old_name"], PARAMS["new_name"]))
        if res.get("status") != "success":
            print("@@UMCP@@" + json.dumps(res))
        else:
            _save(PARAMS["skeleton_path"])
            _ledger().append({"op": "rename_anim_slot", "skeleton_path": PARAMS["skeleton_path"],
                              "old_name": PARAMS["old_name"], "new_name": PARAMS["new_name"]})
            res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def rename_anim_slot(ctx, skeleton_path: str, old_name: str, new_name: str) -> str:
        """Rename a montage slot on a Skeleton (C++ #18 rename_skeleton_slot); its group is preserved.
        Refused if old_name is absent or new_name already exists. Ledgered, reversible.

        skeleton_path: object path of a Skeleton asset.
        old_name:      existing slot name.
        new_name:      new slot name.

        Ledgered op 'rename_anim_slot' {skeleton_path, old_name, new_name}; inverse (folded into
        editor_level.undo): rename_skeleton_slot(new_name, old_name)."""
        params = {"skeleton_path": skeleton_path, "old_name": old_name, "new_name": new_name}
        try:
            return json.dumps(_exec(_RENAME_SLOT_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # add_anim_slot_group — create a slot group                          #
    # ------------------------------------------------------------------ #
    _ADD_GROUP_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("add_skeleton_slot_group")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.add_skeleton_slot_group unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        with unreal.ScopedEditorTransaction("MCP add_anim_slot_group"):
            res = json.loads(rl.add_skeleton_slot_group(skel, PARAMS["group_name"]))
        if res.get("status") != "success":
            print("@@UMCP@@" + json.dumps(res))
        else:
            _save(PARAMS["skeleton_path"])
            _ledger().append({"op": "add_anim_slot_group", "skeleton_path": PARAMS["skeleton_path"],
                              "group_name": PARAMS["group_name"], "added": bool(res.get("added"))})
            res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def add_anim_slot_group(ctx, skeleton_path: str, group_name: str) -> str:
        """Create a montage slot GROUP on a Skeleton (C++ #18 add_skeleton_slot_group). If the group
        already exists this is a no-op (added=false) and undo does nothing. Ledgered, reversible.

        skeleton_path: object path of a Skeleton asset.
        group_name:    the slot group to create (e.g. 'MCP_TestGroup').

        Ledgered op 'add_anim_slot_group' {skeleton_path, group_name, added}; inverse (folded into
        editor_level.undo): if added -> remove_skeleton_slot_group(group); else no-op."""
        params = {"skeleton_path": skeleton_path, "group_name": group_name}
        try:
            return json.dumps(_exec(_ADD_GROUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # remove_anim_slot_group — delete a group (captures prior slots)     #
    # ------------------------------------------------------------------ #
    _REMOVE_GROUP_BODY = _SLOTS_HELPERS + r'''
skel, err = _load_skel(PARAMS["skeleton_path"])
if err:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
else:
    rl = _reflib("remove_skeleton_slot_group")
    if rl is None:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "MCPReflectionLibrary.remove_skeleton_slot_group unavailable — reload the MCP server after the C++ #18 rebuild"}))
    else:
        with unreal.ScopedEditorTransaction("MCP remove_anim_slot_group"):
            res = json.loads(rl.remove_skeleton_slot_group(skel, PARAMS["group_name"]))
        if res.get("status") != "success":
            print("@@UMCP@@" + json.dumps(res))
        else:
            _save(PARAMS["skeleton_path"])
            _ledger().append({"op": "remove_anim_slot_group", "skeleton_path": PARAMS["skeleton_path"],
                              "group_name": PARAMS["group_name"], "prior_slots": res.get("prior_slots") or []})
            res["ledger_depth"] = len(_ledger())
            print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def remove_anim_slot_group(ctx, skeleton_path: str, group_name: str) -> str:
        """Delete a montage slot GROUP from a Skeleton (C++ #18 remove_skeleton_slot_group). The group's
        member slots are captured so undo re-creates the group and re-adds each slot. Refused if the
        group is absent. Ledgered, reversible.

        skeleton_path: object path of a Skeleton asset.
        group_name:    the slot group to remove.

        Ledgered op 'remove_anim_slot_group' {skeleton_path, group_name, prior_slots[]}; inverse (folded
        into editor_level.undo): add_skeleton_slot_group(group) then add_skeleton_slot(s, group) for each
        prior slot."""
        params = {"skeleton_path": skeleton_path, "group_name": group_name}
        try:
            return json.dumps(_exec(_REMOVE_GROUP_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
