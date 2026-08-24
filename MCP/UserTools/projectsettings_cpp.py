"""UserTools :: Project Settings authoring + discovery (C++-backed)  (spec: docs/spec/project.md)

Wires the "Batch B" small-category C++ round drafted in
Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPReflection_SmallCats.cpp. Two tools that reach what stock
Python cannot:

  READ  (no ledger):
    * search_project_settings -- enumerate the registered UDeveloperSettings classes (every Project/Editor
                                 Settings SECTION), reading the container/category/section VIRTUALS
                                 (GetContainerName/GetCategoryName/GetSectionName) that are NOT UPROPERTYs
                                 and so are Python-unreachable. Backed by search_project_settings_json.

  WRITE (ledger -- reversible; PERSISTS to a real Default*.ini):
    * set_project_settings    -- set ONE config property on a UDeveloperSettings CDO and persist it to its
                                 Default*.ini (TryUpdateDefaultConfigFile). Captures prev (ExportText) +
                                 had_prior (did the ini key pre-exist). Backed by set_developer_setting_json.

The property VALUES for get_project_settings still come from project_info.get_project_settings (reflection +
get_editor_property); THIS module owns the section-discovery virtuals + the persisting setter, both of which
are the C++-only value-add. Each tool hasattr-guards its future handler, so this module is INERT until the
plugin DLL is rebuilt with MCPReflection_SmallCats.cpp -- at which point it AUTO-ENABLES.

Undo: this module registers NO own `undo` tool (editor_level.py owns the ONE unified `undo`). set_project_settings
appends ONE ledger op "set_project_setting" {settings_class, property, prev, had_prior} whose inverse the
coordinator folds into editor_level.undo:
    if had_prior:  re-set the captured prev via set_developer_setting_json(settings_class, property, [prev])
    else:          the key did NOT exist before -> CLEAR it: GConfig/EmptySection or remove the key from the
                   Default*.ini section, then re-persist (TryUpdateDefaultConfigFile) so the ini returns to
                   its pre-write state.

RISK (flagged): set_project_settings WRITES A REAL Config/Default*.ini on disk (TryUpdateDefaultConfigFile).
For any live test use a SESSION/editor-scoped setting (e.g. a UEditorPerProjectUserSettings bool), never a
destructive project-wide setting.

NB: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so snippet bodies
contain NO triple-single-quote and NO stray backslashes; all data crosses as base64. Never assign a snippet
local named sys/unreal/traceback/output_file/error_file/original_stdout/original_stderr/success/user_code/
code_obj (the C++ wrapper's own names).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from console.py / niagara_runtime_cpp.py) -------
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
    _HELP = r'''
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
def _mrl(fn):
    rl = getattr(unreal, "MCPReflectionLibrary", None)
    if rl is None or not hasattr(rl, fn):
        return None
    return rl
def _decode(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": str(raw)[:400]}
def _defer(fn):
    return {"status": "error", "error": (fn + " requires the C++ handler in MCPReflection_SmallCats.cpp "
            "(rebuild the UnrealMCP plugin DLL to enable it).")}
'''

    # ================================================================== #
    # search_project_settings — enumerate UDeveloperSettings sections    #
    # (READ; no ledger)                                                  #
    # ================================================================== #
    _SEARCH_BODY = _HELP + r'''
rl = _mrl("search_project_settings_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("search_project_settings")))
else:
    res = _decode(rl.search_project_settings_json(PARAMS.get("filter", ""), PARAMS.get("container", "")))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        if isinstance(res, dict):
            res["status"] = "success"
        print("@@UMCP@@" + json.dumps(res))
'''

    @mcp.tool()
    def search_project_settings(ctx, filter: str = "", container: str = "") -> str:
        """Enumerate the project's settings SECTIONS (registered UDeveloperSettings classes). Read-only.

        filter:    case-insensitive substring matched against class / category / section (empty = all).
        container: optional -- restrict to a container ('Project' or 'Editor') or a category name
                   ('Game'/'Engine'/'Editor'/...). Empty = no container filter.

        Reads the container/category/section VIRTUALS (GetContainerName/GetCategoryName/GetSectionName) that
        are not UPROPERTYs and so are unreachable from stock Python -- hence the C++ handler. Returns
        {filter, container, match_count, settings:[{class, class_path, container, category, section,
        config_property_count}]}. Pair a returned 'class' with project_info.get_project_settings to read its
        values, or with set_project_settings to change one. Needs the C++ handler (inert until the plugin DLL
        is rebuilt with MCPReflection_SmallCats.cpp)."""
        params = {"filter": filter, "container": container}
        try:
            return json.dumps(_exec(_SEARCH_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ================================================================== #
    # set_project_settings — set + PERSIST one UDeveloperSettings prop    #
    # (WRITE; ledger op "set_project_setting"; inverse folds into undo)   #
    # ================================================================== #
    _SET_BODY = _HELP + r'''
rl = _mrl("set_developer_setting_json")
if rl is None:
    print("@@UMCP@@" + json.dumps(_defer("set_project_settings")))
else:
    settings_class = PARAMS["settings_class"]
    prop = PARAMS["property"]
    value_json = PARAMS["value_json"]
    res = _decode(rl.set_developer_setting_json(settings_class, prop, value_json))
    if isinstance(res, dict) and res.get("error"):
        print("@@UMCP@@" + json.dumps({"status": "error", "message": res.get("error")}))
    else:
        _ledger().append({"op": "set_project_setting",
            "settings_class": res.get("settings_class", settings_class),
            "property": res.get("property", prop),
            "prev": res.get("prev"), "had_prior": bool(res.get("had_prior"))})
        out = {"status": "success",
               "settings_class": res.get("settings_class"),
               "settings_class_path": res.get("settings_class_path"),
               "property": res.get("property"),
               "prev": res.get("prev"), "had_prior": bool(res.get("had_prior")),
               "persisted": bool(res.get("persisted")),
               "config_file": res.get("config_file"),
               "config_section": res.get("config_section"),
               "ledger_depth": len(_ledger())}
        print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def set_project_settings(ctx, settings_class: str, property: str, value) -> str:
        """Set ONE config property on a UDeveloperSettings section and PERSIST it to its Default*.ini.
        REVERSIBLE ledgered write.

        settings_class: a UDeveloperSettings subclass -- bare name ('RendererSettings') or full path
                        ('/Script/Engine.RendererSettings'). Use search_project_settings to discover them.
        property:       config property name, or a dotted path into a struct member ('Foo.Bar').
        value:          the new value. Scalars/bools/strings/enum-names pass straight through; a struct or
                        array value is passed as its UE ExportText STRING (e.g. '(X=1.0,Y=2.0,Z=3.0)').
                        (Sent to C++ array-wrapped as JSON so the reader can unwrap a bare scalar.)

        Persists via UObject::TryUpdateDefaultConfigFile -> writes the class's whole section into the real
        Default*.ini. Captures prev (single-property ExportText) and had_prior (whether the ini key already
        existed) for the undo. Returns {settings_class, property, prev, had_prior, persisted, config_file,
        config_section, ledger_depth}.

        WARNING: this writes a REAL Config/Default*.ini. Prefer a session/editor-scoped setting
        (e.g. UEditorPerProjectUserSettings) over a destructive project-wide setting.

        Ledgered op 'set_project_setting' {settings_class, property, prev, had_prior}. Inverse (coordinator
        folds into editor_level.undo): if had_prior -> re-set prev via set_project_settings(settings_class,
        property, prev); else -> clear the ini key (remove it from the section + re-persist). Needs the C++
        handler (inert until the plugin DLL is rebuilt with MCPReflection_SmallCats.cpp)."""
        # Array-wrap the value so the C++ JSON reader accepts a bare scalar/string at the document root.
        value_json = json.dumps([value])
        params = {"settings_class": settings_class, "property": property, "value_json": value_json}
        try:
            return json.dumps(_exec(_SET_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
    # NO `undo` tool here; editor_level.py owns the unified `undo`. set_project_settings adds ONE ledger op
    # "set_project_setting" {settings_class, property, prev, had_prior}; inverse per the docstring above.
