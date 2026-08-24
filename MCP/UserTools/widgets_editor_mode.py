"""UserTools :: Widget editor mode (set_widget_editor_mode) — the last widget spec feature.

Opens a Widget Blueprint's asset editor and requests a mode. NOTE: the Designer/Graph mode is a
UMG-editor TOOLKIT TAB, not a persistent/scriptable asset state — UE 5.8 exposes no reflected
`SetCurrentMode`, so the tab-switch itself only happens interactively. This tool does the reachable
part (open the editor for the asset) and reports the requested mode; on a headless editor there is no
UI so it is a documented no-op-with-note. Read-only (no ledger): switching a UI tab mutates nothing on
the asset. Scaffolding mirrors the sibling widget modules (base64 PARAMS, @@UMCP@@ marker).
"""
import json
import base64

MARKER = "@@UMCP@@"


def register_tools(mcp, utils):
    send_command = utils["send_command"]

    def _exec(body, params):
        b64 = base64.b64encode(json.dumps(dict(params or {})).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        resp = send_command("execute_python", {"code": header + body})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    _BODY = r'''
import unreal, json
path = PARAMS["widget_blueprint_path"]
mode = (PARAMS.get("mode") or "designer").lower()
wb = unreal.load_asset(path)
if wb is None or not isinstance(wb, unreal.WidgetBlueprint):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "not a WidgetBlueprint: %s" % path}))
else:
    aes = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    opened = False
    try:
        aes.open_editor_for_assets([wb]); opened = True
    except Exception as e:
        pass
    is_headless = False
    try:
        is_headless = not unreal.SystemLibrary.get_supported_fully_featured_editor()
    except Exception:
        pass
    print("@@UMCP@@" + json.dumps({"status": "success", "widget_blueprint": wb.get_name(),
        "requested_mode": mode, "editor_opened": opened,
        "note": "opened the WidgetBlueprint editor; the Designer/Graph/Preview mode is an interactive toolkit tab with no reflected SetCurrentMode in UE 5.8, so the tab-switch itself only applies in an interactive editor (no-op headless). The asset is not mutated."}))
'''

    @mcp.tool()
    def set_widget_editor_mode(ctx, widget_blueprint_path: str, mode: str = "designer") -> str:
        """Open a Widget Blueprint's editor and request a mode (designer|graph|preview).

        widget_blueprint_path: a /Game WidgetBlueprint asset. mode: designer (default) | graph | preview.
        Opens the asset editor. NOTE: the mode is an interactive UMG-editor tab (no reflected
        SetCurrentMode in 5.8), so the tab-switch applies only in an interactive editor — this is a
        documented no-op on a headless editor. Read-only (mutates nothing; no undo)."""
        try:
            return json.dumps(_exec(_BODY, {"widget_blueprint_path": widget_blueprint_path, "mode": mode}), indent=2)
        except Exception as e:
            return f"Error: {e}"
