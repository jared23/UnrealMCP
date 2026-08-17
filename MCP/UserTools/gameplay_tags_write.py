"""UserTools :: Gameplay Tags (WRITE)  (spec: docs/spec/gameplay_tags.md)

Clean-room reimplementation over Unreal's public API (UE 5.8). The mutating counterpart to
gameplay_tags_read.py. Query convention, base64 PARAMS injection, Output-Log auto-capture, and the
per-session undo ledger are copied VERBATIM from the gold-standard editor_level.py.

Gameplay-tag authoring is NOT reachable from stock Python (probed live 2026-08-15: editing
GameplayTagsSettings.GameplayTagList only edits in-memory — the tag does NOT register with the live
UGameplayTagsManager and there is no exposed config-persist). It goes through the C++ #6 handlers
`unreal.MCPReflectionLibrary.add_gameplay_tag` / `remove_gameplay_tag`, which use
`IGameplayTagsEditorModule::AddNewGameplayTagToINI` / `DeleteTagFromINI` — writing
DefaultGameplayTags.ini AND refreshing the manager (verified on-disk). All hasattr-guarded so the
module still loads if the plugin DLL predates C++ #6.

Implemented (validated live, ledger depth 0):
  - add_gameplay_tag     (WRITE; ledgered op "add_gameplay_tag"; inverse = remove the tag — FAITHFUL)
  - remove_gameplay_tag  (WRITE; ledgered op "remove_gameplay_tag"; inverse = re-add the tag, best-effort)

Undo: this module does NOT register its own `undo` tool (editor_level.py owns the unified `undo`).
It introduces 2 NEW ledger ops whose inverses are folded into editor_level.undo.
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

    _GT_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
'''

    _ADD_TAG_BODY = _GT_HELPERS + r'''
tag = PARAMS["tag"]; comment = PARAMS.get("comment") or ""
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if mrl is None or not hasattr(mrl, "add_gameplay_tag"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler add_gameplay_tag unavailable (plugin DLL predates C++ #6; recompile needed)"}))
else:
    res = json.loads(mrl.add_gameplay_tag(tag, comment))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("added"):
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "AddNewGameplayTagToINI returned false for '%s' (already exists or invalid?)" % tag,
            "registered": res.get("registered")}))
    else:
        _ledger().append({"op": "add_gameplay_tag", "tag": tag})
        print("@@UMCP@@" + json.dumps({"status": "success", "tag": tag, "comment": comment,
            "added": res.get("added"), "registered": res.get("registered"), "ledger_depth": len(_ledger())}))
'''

    _REMOVE_TAG_BODY = _GT_HELPERS + r'''
tag = PARAMS["tag"]; comment = PARAMS.get("comment") or ""
mrl = getattr(unreal, "MCPReflectionLibrary", None)
if mrl is None or not hasattr(mrl, "remove_gameplay_tag"):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "C++ handler remove_gameplay_tag unavailable (plugin DLL predates C++ #6; recompile needed)"}))
else:
    res = json.loads(mrl.remove_gameplay_tag(tag))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    elif not res.get("found"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "tag not found: %s" % tag}))
    elif not res.get("removed"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "DeleteTagFromINI returned false for '%s'" % tag}))
    else:
        _ledger().append({"op": "remove_gameplay_tag", "tag": tag, "comment": comment})
        print("@@UMCP@@" + json.dumps({"status": "success", "tag": tag, "found": res.get("found"),
            "removed": res.get("removed"), "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def add_gameplay_tag(ctx, tag: str, comment: str = "") -> str:
        """Author a gameplay tag (writes Config/DefaultGameplayTags.ini + registers it with the live
        manager). REQUIRES the C++ #6 handler (unreal.MCPReflectionLibrary.add_gameplay_tag); returns a
        clear error on an older DLL.

        tag:     the tag string, dot-separated (e.g. 'Ability.Fire.Cast'). Parent tags are implied.
        comment: optional DevComment stored alongside the tag in the INI.

        Verify with gameplay_tags_read.get_gameplay_tag_info (registered=true). Ledgered write op
        'add_gameplay_tag' {tag}. Inverse (in editor_level.undo): remove_gameplay_tag(tag) -> FAITHFUL
        (removes exactly the tag we added, INI back to net-zero)."""
        params = {"tag": tag, "comment": comment}
        try:
            return json.dumps(_exec(_ADD_TAG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def remove_gameplay_tag(ctx, tag: str, comment: str = "") -> str:
        """Remove a gameplay tag from Config/DefaultGameplayTags.ini. REQUIRES the C++ #6 handler.

        tag:     the tag string to remove (must exist).
        comment: optional -- pass the tag's DevComment so undo can re-add it faithfully; if omitted, the
                 undo re-adds the tag with no comment (best-effort).

        Ledgered write op 'remove_gameplay_tag' {tag, comment}. Inverse (in editor_level.undo):
        add_gameplay_tag(tag, comment) -- best-effort (re-adds the tag; the DevComment is only restored
        if you passed it). Removing a tag that has registered children may be refused by the engine."""
        params = {"tag": tag, "comment": comment}
        try:
            return json.dumps(_exec(_REMOVE_TAG_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
