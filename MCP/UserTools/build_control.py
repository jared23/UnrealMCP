"""UserTools :: build / editor-restart orchestration (MCP_TOOLING_GAPS #8).

These run IN THE BRIDGE PROCESS, not the editor — a tool executing inside the editor cannot rebuild or
restart that same editor. They read the engine + project paths from the live editor (before quitting),
quit it, optionally run UBT, relaunch the editor, and poll the MCP port until it reconnects. This
encodes the close -> Build.bat -> relaunch -> poll loop done by hand every C++ change.

CAVEATS:
- Registers only in a FRESH MCP session (the bridge loads UserTools at startup).
- build_project blocks for the whole build (~1-2 min); a very slow build could exceed the MCP client's
  tool timeout even though the build itself succeeds — check the editor came back with health_check.
- The editor must be responsive when called (its paths are read first).
"""
import json
import os
import socket
import subprocess
import time

MARKER = "@@UMCP@@"
DEFAULT_PORT = 13377


def _port_open(host="127.0.0.1", port=DEFAULT_PORT, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def register_tools(mcp, utils):
    send_command = utils["send_command"]

    def _ctx():
        code = ("import unreal, json\n"
                "print('@@UMCP@@' + json.dumps({"
                "'engine_dir': unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir()),"
                "'uproject': unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path()),"
                "'project': unreal.Paths.get_project_file_path().replace(chr(92),'/').split('/')[-1].rsplit('.',1)[0]}))\n")
        resp = send_command("execute_python", {"code": code})
        out = resp.get("result", {}).get("output", "") if isinstance(resp, dict) else ""
        for line in out.splitlines():
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError("could not read editor context from: " + str(out))

    def _quit_editor():
        try:
            send_command("execute_python", {"code":
                "import unreal\n"
                "unreal.SystemLibrary.execute_console_command(unreal.EditorLevelLibrary.get_editor_world(), 'QUIT_EDITOR')\n"})
        except Exception:
            pass  # the editor quitting legitimately drops the socket

    def _wait_port(want_open, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _port_open() == want_open:
                return True
            time.sleep(1.0)
        return _port_open() == want_open

    def _run_build(engine_dir, target, uproject):
        bat = os.path.join(engine_dir, "Build", "BatchFiles", "Build.bat")
        if not os.path.exists(bat):
            return False, "Build.bat not found at %s" % bat
        # .bat must run through cmd on Windows.
        cmd = ["cmd", "/c", bat, target, "Win64", "Development", "-project=" + uproject, "-waitmutex"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except Exception as e:
            return False, "build launch failed: %s" % e
        stdout = p.stdout or ""
        ok = "Result: Succeeded" in stdout
        tail = "\n".join(stdout.splitlines()[-15:])
        return ok, tail

    def _launch_editor(engine_dir, uproject):
        exe = os.path.join(engine_dir, "Binaries", "Win64", "UnrealEditor.exe")
        if not os.path.exists(exe):
            return False, "UnrealEditor.exe not found at %s" % exe
        try:
            subprocess.Popen([exe, uproject], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, exe
        except Exception as e:
            return False, "editor launch failed: %s" % e

    def _cycle(rebuild, target):
        try:
            c = _ctx()
        except Exception as e:
            return {"status": "error", "message": "editor not reachable to read context: %s" % e}
        tgt = target or (c["project"] + "Editor")
        _quit_editor()
        if not _wait_port(False, 60):
            return {"status": "error", "message": "editor did not close within 60s"}
        build_ok, build_tail = (True, "skipped")
        if rebuild:
            build_ok, build_tail = _run_build(c["engine_dir"], tgt, c["uproject"])
        launched, launch_info = _launch_editor(c["engine_dir"], c["uproject"])
        reconnected = _wait_port(True, 180) if launched else False
        status = "ok" if (build_ok and launched and reconnected) else ("build_failed" if not build_ok else "partial")
        return {"status": status, "target": tgt, "rebuilt": bool(rebuild), "build_ok": build_ok,
                "launched": launched, "reconnected": reconnected, "build_tail": build_tail,
                "launch_info": launch_info if not launched else None}

    @mcp.tool()
    def build_project(ctx, target: str = None) -> str:
        """Rebuild the project's editor target: closes the editor, runs UBT (Build.bat <target> Win64
        Development), relaunches the editor, and waits for the MCP port to reconnect. Runs in the bridge
        process. target defaults to <ProjectName>Editor (read from the live editor). ~1-2 min; if the tool
        call times out client-side, confirm with health_check — the build likely still finished. Returns
        {build_ok, reconnected, build_tail}."""
        try:
            return json.dumps(_cycle(True, target), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def restart_editor(ctx, rebuild: bool = False, target: str = None) -> str:
        """Restart the editor (quit -> optionally rebuild -> relaunch -> reconnect). rebuild=False just
        cycles the editor (~30s, e.g. to clear a wedged state); rebuild=True runs UBT first (like
        build_project). Runs in the bridge process. Returns {rebuilt, build_ok, reconnected}."""
        try:
            return json.dumps(_cycle(rebuild, target), indent=2)
        except Exception as e:
            return f"Error: {e}"
