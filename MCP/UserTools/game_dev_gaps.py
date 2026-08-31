"""UserTools :: game-dev tooling gaps (MCP_TOOLING_GAPS.md batch 1).

Tools observed as missing during real UE 5.8 game dev. Each C++-backed tool delegates to a
C++ #53 handler (unreal.MCPReflectionLibrary.<name>) and is hasattr-guarded so the module still
loads on an older plugin DLL. game_screenshot is pure Python (no rebuild needed).

  set_game_mode_override  (#7)  -> WorldSettings.DefaultGameMode override (C++ #53)
  set_pie_settings        (#2)  -> mutate ULevelEditorPlaySettings, no restart (C++ #53)
  list_pie_worlds         (#10) -> enumerate server + client PIE worlds (C++ #53)
  set_material_usage      (#5)  -> set bUsedWith* flags + persist via SavePackage (C++ #53)
  get_reflected_property  (#9)  -> read ANY reflected UPROPERTY incl. protected (C++ #53)
  call_function           (#9)  -> ProcessEvent a UFUNCTION by name (C++ #53)
  pie_input               (#3)  -> inject GAMEPLAY input into PIE (PlayerController -> Enhanced Input) (C++ #53)
  create_child_anim_blueprint_override (#1) -> child AnimBP that remaps anims (C++ #53)
  game_screenshot         (#4)  -> capture the GAME viewport during PIE (AutomationLibrary; pure Python)

  # SLATE-LEVEL PIE input (C++ #54) — drives the UI/UMG layer (menus, hover, click, text, gamepad nav),
  # which pie_input (gameplay-only) cannot reach:
  pie_ui_key       -> keyboard nav into UMG (Enter/Escape/arrows/Tab) (C++ #54)
  pie_ui_text      -> type into the focused editable text box (room codes) (C++ #54)
  pie_ui_mouse_move-> hover hit-testing at a viewport point (C++ #54)
  pie_ui_click     -> click UMG/HUD hitboxes at a viewport point (C++ #54)
  pie_ui_gamepad   -> controller button/analog menu nav (C++ #54)
"""
import json
import base64
import os
import time

MARKER = "@@UMCP@@"


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": code})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        for line in reversed(out.splitlines()):
            if MARKER in line:
                return json.loads(line.split(MARKER, 1)[1])
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    def _cpp_delegate(fn_name, call_expr):
        # A tiny in-editor body: guard the C++ handler, then print its (already-JSON) return verbatim.
        return (
            "import unreal, json\n"
            "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
            "if mrl is None or not hasattr(mrl, '%s'):\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':"
            "'C++ #53 handler %s unavailable — recompile the UnrealMCP plugin (DLL predates C++ #53)'}))\n"
            "else:\n"
            "    print('@@UMCP@@' + %s)\n" % (fn_name, fn_name, call_expr)
        )

    # ---- #7 set_game_mode_override ------------------------------------------------
    @mcp.tool()
    def set_game_mode_override(ctx, class_path: str = "") -> str:
        """Set the current editor level's WorldSettings DefaultGameMode override. class_path is a native
        (/Script/Module.MyGameMode) or Blueprint (/Game/BP_GM.BP_GM_C) class path; pass "" to clear the
        override. Marks the level dirty — call save_level to persist. Requires the C++ #53 handler."""
        body = _cpp_delegate("set_game_mode_override", "mrl.set_game_mode_override(PARAMS['class_path'])")
        try:
            return json.dumps(_exec(body, {"class_path": class_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #2 set_pie_settings ------------------------------------------------------
    @mcp.tool()
    def set_pie_settings(ctx, num_clients: int = 0, net_mode: str = "",
                         run_under_one_process: bool = True, use_splitscreen: bool = False) -> str:
        """Configure Play-In-Editor for the NEXT play_in_editor with no editor restart (mutates the
        in-memory ULevelEditorPlaySettings). num_clients: >0 to set the client count (e.g. 2 for a
        listen-server + 1 client). net_mode: 'standalone' | 'listen' | 'client' (blank = leave).
        run_under_one_process: one process (fast) vs separate processes. use_splitscreen: local
        split-screen for multiple local players. Requires the C++ #53 handler."""
        body = _cpp_delegate(
            "set_pie_settings",
            "mrl.set_pie_settings(int(PARAMS['num_clients']), PARAMS['net_mode'], "
            "bool(PARAMS['run_under_one_process']), bool(PARAMS['use_splitscreen']))")
        try:
            return json.dumps(_exec(body, {"num_clients": num_clients, "net_mode": net_mode,
                                           "run_under_one_process": run_under_one_process,
                                           "use_splitscreen": use_splitscreen}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #10 list_pie_worlds ------------------------------------------------------
    @mcp.tool()
    def list_pie_worlds(ctx) -> str:
        """Enumerate every world context (Editor/PIE/Game). For multi-client PIE this lists the server
        world AND each client world separately: {world, world_type, net_role, pie_instance, actor_count}.
        Read-only. Requires the C++ #53 handler."""
        body = _cpp_delegate("list_pie_worlds_json", "mrl.list_pie_worlds_json()")
        try:
            return json.dumps(_exec(body, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #5 set_material_usage ----------------------------------------------------
    @mcp.tool()
    def set_material_usage(ctx, material_path: str, usage_flags: str, save: bool = True) -> str:
        """Set a material's usage flags (bUsedWith*) and PERSIST them into the .uasset via a C++
        SavePackage (Python save_loaded_asset returned a bare False for this). usage_flags is a
        comma-separated list, e.g. 'skeletal_mesh,morph_targets' (also: particle_sprites, mesh_particles,
        instanced_static_meshes, spline_mesh, nanite, niagara_sprites, ...). material_path is the base
        UMaterial (or an instance whose base resolves). Returns applied flags + the real save result.
        Requires the C++ #53 handler."""
        body = (
            "import unreal, json\n"
            "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
            "mat = unreal.load_asset(PARAMS['material_path'])\n"
            "if mrl is None or not hasattr(mrl, 'set_material_usage'):\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'C++ #53 set_material_usage unavailable — recompile the plugin'}))\n"
            "elif mat is None:\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'could not load material: ' + str(PARAMS['material_path'])}))\n"
            "else:\n"
            "    print('@@UMCP@@' + mrl.set_material_usage(mat, PARAMS['usage_flags'], bool(PARAMS['save'])))\n"
        )
        try:
            return json.dumps(_exec(body, {"material_path": material_path, "usage_flags": usage_flags, "save": save}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #9 get_reflected_property ------------------------------------------------
    @mcp.tool()
    def get_reflected_property(ctx, object_path: str, property_name: str, actor_label: str = "") -> str:
        """Read ANY reflected UPROPERTY (including protected / non-BlueprintReadable / editor-hidden) via
        raw FProperty — bypasses the Python get_editor_property edit-protection. Identify the object by
        object_path (asset or full object path) OR actor_label (a level actor). Returns {cpp_type, value}.
        Truly-private (non-UPROPERTY) C++ members are out of scope. Requires the C++ #53 handler."""
        body = (
            "import unreal, json\n"
            "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
            "obj = None\n"
            "if PARAMS.get('actor_label'):\n"
            "    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "    obj = next((a for a in eas.get_all_level_actors() if a.get_actor_label()==PARAMS['actor_label'] or a.get_name()==PARAMS['actor_label']), None)\n"
            "if obj is None and PARAMS.get('object_path'):\n"
            "    obj = unreal.load_asset(PARAMS['object_path']) or unreal.load_object(None, PARAMS['object_path'])\n"
            "if mrl is None or not hasattr(mrl, 'get_reflected_property_json'):\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'C++ #53 get_reflected_property_json unavailable — recompile the plugin'}))\n"
            "elif obj is None:\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'could not resolve object (object_path/actor_label)'}))\n"
            "else:\n"
            "    print('@@UMCP@@' + mrl.get_reflected_property_json(obj, PARAMS['property_name']))\n"
        )
        try:
            return json.dumps(_exec(body, {"object_path": object_path, "property_name": property_name,
                                           "actor_label": actor_label}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #9 call_function ---------------------------------------------------------
    @mcp.tool()
    def call_function(ctx, function_name: str, args: dict = None, object_path: str = "", actor_label: str = "") -> str:
        """Call a UFUNCTION by name on an object via ProcessEvent. args is a {param_name: scalar} dict
        (bool/number/string, marshalled via ImportText). Identify the target by object_path OR actor_label.
        Returns {outputs:{return?, out_params...}}. Only reflected UFUNCTIONs are callable (plain C++
        methods are out of scope; mark them UFUNCTION for tool visibility). Requires the C++ #53 handler."""
        body = (
            "import unreal, json\n"
            "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
            "obj = None\n"
            "if PARAMS.get('actor_label'):\n"
            "    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "    obj = next((a for a in eas.get_all_level_actors() if a.get_actor_label()==PARAMS['actor_label'] or a.get_name()==PARAMS['actor_label']), None)\n"
            "if obj is None and PARAMS.get('object_path'):\n"
            "    obj = unreal.load_asset(PARAMS['object_path']) or unreal.load_object(None, PARAMS['object_path'])\n"
            "if mrl is None or not hasattr(mrl, 'call_function_json'):\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'C++ #53 call_function_json unavailable — recompile the plugin'}))\n"
            "elif obj is None:\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'could not resolve object (object_path/actor_label)'}))\n"
            "else:\n"
            "    print('@@UMCP@@' + mrl.call_function_json(obj, PARAMS['function_name'], json.dumps(PARAMS.get('args') or {})))\n"
        )
        try:
            return json.dumps(_exec(body, {"function_name": function_name, "args": args or {},
                                           "object_path": object_path, "actor_label": actor_label}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #3 pie_input (inject input into the running PIE session) -----------------
    _PIE_PRESS_BODY = (
        "import unreal, json\n"
        "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
        "if mrl is None or not hasattr(mrl, 'inject_pie_key'):\n"
        "    print('@@UMCP@@' + json.dumps({'status':'error','message':'C++ #53 inject_pie_key unavailable — recompile the plugin'}))\n"
        "else:\n"
        "    r = []\n"
        "    for k in (PARAMS.get('keys') or []):\n"
        "        r.append(json.loads(mrl.inject_pie_key(int(PARAMS['pi']), k, 'pressed', 1.0, False)))\n"
        "    for k, v in (PARAMS.get('axes') or {}).items():\n"
        "        r.append(json.loads(mrl.inject_pie_key(int(PARAMS['pi']), k, 'pressed', float(v), False)))\n"
        "    errs = [x for x in r if x.get('status') != 'ok']\n"
        "    print('@@UMCP@@' + json.dumps({'status': 'error' if errs else 'ok', 'pressed': r}))\n"
    )
    _PIE_RELEASE_BODY = (
        "import unreal, json\n"
        "mrl = unreal.MCPReflectionLibrary\n"
        "r = [json.loads(mrl.inject_pie_key(int(PARAMS['pi']), k, 'released', 0.0, False)) for k in (PARAMS.get('keys') or [])]\n"
        "print('@@UMCP@@' + json.dumps({'status':'ok','released': r}))\n"
    )

    @mcp.tool()
    def pie_input(ctx, keys: list = None, hold_seconds: float = 0.2, player_index: int = 0,
                  release: bool = True, axes: dict = None) -> str:
        """Inject keyboard/gamepad input into the RUNNING PIE session so input-driven features can be
        self-tested (WASD move, jump, fire/ADS, sprint/crouch). keys: FKey names to press together, e.g.
        ['W'], ['SpaceBar'], ['W','LeftShift'] (sprint). They are HELD for hold_seconds (press -> wait ->
        release) so movement/holds register across game ticks, then released (release=False to keep them
        down). player_index selects the PIE player (0 first; 1 the second client in multi-client PIE).
        axes: {fkey: value} for analog, injected once (e.g. {'Gamepad_LeftX': 1.0}). Requires PIE running
        (start play_in_editor first) + the C++ #53 handler. Verify effects with get_pie_status /
        get_reflected_property on the pawn, or game_screenshot (keep the PIE window foreground)."""
        keys = keys or []
        try:
            press = _exec(_PIE_PRESS_BODY, {"keys": keys, "axes": axes or {}, "pi": player_index})
            if press.get("status") != "ok":
                return json.dumps(press, indent=2)
            if hold_seconds and hold_seconds > 0:
                time.sleep(min(float(hold_seconds), 30.0))
            released = None
            if release and keys:
                released = _exec(_PIE_RELEASE_BODY, {"keys": keys, "pi": player_index})
            return json.dumps({"status": "ok", "held_seconds": hold_seconds,
                               "pressed": press.get("pressed"), "released": released.get("released") if released else None}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ---- #1 create_child_anim_blueprint_override ---------------------------------
    @mcp.tool()
    def create_child_anim_blueprint_override(ctx, parent_path: str, package_path: str,
                                             asset_name: str, overrides: dict) -> str:
        """Create a CHILD AnimBlueprint of parent_path that REMAPS animations — the standard "reuse a
        working locomotion/state-machine graph with a different anim set" flow (e.g. reuse ABP_Unarmed's
        graph with Rifle anims). overrides = {oldAnimPathOrName: newAnimPath}, e.g.
        {"MM_Unarmed_Idle": "/Game/Characters/.../MM_Rifle_Idle"}. It matches parent anim-graph nodes that
        play each old anim (by full path, asset name, or path suffix) and adds a ParentAssetOverride to the
        new asset. Sets the override DATA + saves the child; then OPEN + COMPILE the child AnimBP in-editor
        to apply it (the MCP intentionally does not compile AnimBlueprints — that path is crash-prone).
        Requires the C++ #53 handler."""
        body = (
            "import unreal, json\n"
            "mrl = getattr(unreal, 'MCPReflectionLibrary', None)\n"
            "if mrl is None or not hasattr(mrl, 'create_child_anim_blueprint_override'):\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':'C++ #53 create_child_anim_blueprint_override unavailable — recompile the plugin'}))\n"
            "else:\n"
            "    print('@@UMCP@@' + mrl.create_child_anim_blueprint_override(PARAMS['parent'], PARAMS['pkg'], PARAMS['name'], json.dumps(PARAMS.get('overrides') or {})))\n"
        )
        try:
            return json.dumps(_exec(body, {"parent": parent_path, "pkg": package_path,
                                           "name": asset_name, "overrides": overrides}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ==== #3S SLATE-LEVEL PIE input (C++ #54) — drives the UI/UMG layer =====================
    # Unlike pie_input (gameplay: PlayerController->Enhanced Input->pawn), these inject at the
    # FSlateApplication layer so UMG menus, button hover/click hitboxes, editable-text (room codes),
    # and gamepad menu nav actually respond. All require PIE running (start play_in_editor) + C++ #54.

    def _slate_call(fn, call_expr, params):
        return _exec(_cpp_delegate(fn, call_expr), params)

    @mcp.tool()
    def pie_ui_key(ctx, key: str, hold_seconds: float = 0.0, release: bool = True,
                   shift: bool = False, ctrl: bool = False, alt: bool = False, cmd: bool = False,
                   user_index: int = 0) -> str:
        """Inject a KEYBOARD event into the MENU/UMG layer during PIE (main-menu nav, pause menu, list
        selection) — the Slate path a real keypress takes, so UMG navigation actually moves. Unlike pie_input
        (which drives the PAWN via Enhanced Input), this drives WIDGET focus/navigation: Enter=activate,
        Escape=back, Up/Down/Left/Right/Tab=navigate, plus letter/number keys. By default it's a full tap
        (key down then up); set release=False + hold_seconds>0 to hold. Modifier flags (shift/ctrl/alt/cmd)
        apply to the event. key is an FKey name (Enter, Escape, Down, Up, Left, Right, Tab, A, One...).
        Requires PIE running + the C++ #54 handler. Verify with a game_screenshot (keep the PIE window
        foreground) or by reading the widget/HUD state."""
        try:
            down = _slate_call("inject_slate_key", "mrl.inject_slate_key(PARAMS['key'],'down',"
                               "bool(PARAMS['shift']),bool(PARAMS['ctrl']),bool(PARAMS['alt']),bool(PARAMS['cmd']),int(PARAMS['ui']))",
                               {"key": key, "shift": shift, "ctrl": ctrl, "alt": alt, "cmd": cmd, "ui": user_index})
            if down.get("status") != "ok":
                return json.dumps(down, indent=2)
            if hold_seconds and hold_seconds > 0:
                time.sleep(min(float(hold_seconds), 30.0))
            up = None
            if release:
                up = _slate_call("inject_slate_key", "mrl.inject_slate_key(PARAMS['key'],'up',"
                                 "bool(PARAMS['shift']),bool(PARAMS['ctrl']),bool(PARAMS['alt']),bool(PARAMS['cmd']),int(PARAMS['ui']))",
                                 {"key": key, "shift": shift, "ctrl": ctrl, "alt": alt, "cmd": cmd, "ui": user_index})
            return json.dumps({"status": "ok", "down": down, "up": up}, indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pie_ui_text(ctx, text: str, user_index: int = 0) -> str:
        """Type TEXT into the currently-FOCUSED editable text box during PIE (e.g. the room-code entry field) via
        per-character Slate character events. Focus the text box FIRST with pie_ui_click on its location — a
        character event only lands in whichever widget currently has keyboard focus. Returns {chars_sent}.
        Requires PIE running + the C++ #54 handler."""
        try:
            return json.dumps(_slate_call("inject_slate_char", "mrl.inject_slate_char(PARAMS['text'], int(PARAMS['ui']))",
                                          {"text": text, "ui": user_index}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pie_ui_mouse_move(ctx, x: float, y: float, normalized: bool = True, user_index: int = 0) -> str:
        """Move the mouse cursor to a viewport point during PIE and fire a Slate mouse-move so HUD/UMG HOVER
        hit-testing runs (reveal hover states, tooltips, highlight the button under the cursor). normalized=True
        => x,y in 0..1 across the viewport (0.5,0.5 = center; resolution-independent, best for HUD); False => raw
        viewport-local pixels. Returns the absolute {screen_x, screen_y}. Requires PIE running + the C++ #54
        handler. Pair with game_screenshot to see the hover state."""
        try:
            return json.dumps(_slate_call("inject_slate_mouse_move",
                                          "mrl.inject_slate_mouse_move(float(PARAMS['x']), float(PARAMS['y']), bool(PARAMS['n']), int(PARAMS['ui']))",
                                          {"x": x, "y": y, "n": normalized, "ui": user_index}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pie_ui_click(ctx, x: float, y: float, button: str = "left", event: str = "click",
                     normalized: bool = True) -> str:
        """Click a UMG/HUD element at a viewport point during PIE — tests click HITBOXES (menu buttons, the
        host-lobby Start gate, list rows). One call moves the cursor there, then presses+releases. button =
        left|right|middle. event = 'click' (down+up, default) | 'down' | 'up' | 'doubleclick'. normalized=True
        => x,y in 0..1 across the viewport (best for HUD); False => raw viewport pixels. To fill a text box:
        pie_ui_click its location to focus it, then pie_ui_text. Returns {button, screen_x, screen_y,
        down_handled, up_handled}. Requires PIE running + the C++ #54 handler."""
        try:
            return json.dumps(_slate_call("inject_slate_mouse_button",
                                          "mrl.inject_slate_mouse_button(PARAMS['b'], PARAMS['e'], float(PARAMS['x']), float(PARAMS['y']), bool(PARAMS['n']))",
                                          {"b": button, "e": event, "x": x, "y": y, "n": normalized}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def pie_ui_gamepad(ctx, key: str, event: str = "press", value: float = 1.0,
                       hold_seconds: float = 0.0, release: bool = True, controller_id: int = 0) -> str:
        """Inject a GAMEPAD button or analog axis into the MENU/UMG layer during PIE (controller menu nav +
        analog-stick edge-detect) — routed to Slate's UI navigation (Accept/Back/Up/Down), so controller menu
        support can actually be exercised. key is a gamepad FKey name: Gamepad_FaceButton_Bottom (A / Accept),
        Gamepad_FaceButton_Right (B / Back), Gamepad_DPad_Up/Down/Left/Right, Gamepad_LeftX / Gamepad_LeftY
        (analog). event = 'press' (pressed then released — a tap) | 'analog' (set the axis to `value`) | 'pressed'
        | 'released'. For a held button use event='pressed', release=False. For analog menu edge-detect, send
        event='analog' value=1.0 then value=0.0. Routes to the primary controller. Requires PIE running + the
        C++ #54 handler."""
        try:
            e = (event or "press").lower()
            if e == "analog":
                return json.dumps(_slate_call("inject_slate_gamepad",
                                              "mrl.inject_slate_gamepad(PARAMS['k'],'analog',float(PARAMS['v']),int(PARAMS['c']))",
                                              {"k": key, "v": value, "c": controller_id}), indent=2)
            if e in ("pressed", "released"):
                return json.dumps(_slate_call("inject_slate_gamepad",
                                              "mrl.inject_slate_gamepad(PARAMS['k'],PARAMS['e'],1.0,int(PARAMS['c']))",
                                              {"k": key, "e": e, "c": controller_id}), indent=2)
            # 'press' == a tap: pressed, optional hold, released.
            down = _slate_call("inject_slate_gamepad",
                               "mrl.inject_slate_gamepad(PARAMS['k'],'pressed',1.0,int(PARAMS['c']))",
                               {"k": key, "c": controller_id})
            if down.get("status") != "ok":
                return json.dumps(down, indent=2)
            if hold_seconds and hold_seconds > 0:
                time.sleep(min(float(hold_seconds), 30.0))
            up = None
            if release:
                up = _slate_call("inject_slate_gamepad",
                                 "mrl.inject_slate_gamepad(PARAMS['k'],'released',0.0,int(PARAMS['c']))",
                                 {"k": key, "c": controller_id})
            return json.dumps({"status": "ok", "pressed": down, "released": up}, indent=2)
        except Exception as e2:
            return f"Error: {e2}"

    # ---- #4 game_screenshot (pure Python — captures the GAME viewport, works in PIE) ---
    @mcp.tool()
    def game_screenshot(ctx, width: int = 1920, height: int = 1080, wait_seconds: float = 8.0) -> str:
        """Capture the actual GAME viewport during PIE (unlike take_screenshot, which captures the editor
        world and fails in PIE). Requests AutomationLibrary.take_high_res_screenshot, waits for the PNG,
        and returns its path on the Windows host (Read it to view) — one call instead of three.

        CAVEAT: UE renders the game viewport's frame for this shot; if the editor/PIE window is
        BACKGROUNDED or minimized the frame is black (a headless-editor limitation, not a tool bug).
        Keep the PIE window visible/foreground while capturing — which is the normal case when you're
        playtesting. No plugin rebuild needed (pure Python)."""
        name = "mcp_game_%d" % int(time.time() * 1000)
        req_body = (
            "import unreal, json\n"
            "try:\n"
            "    unreal.AutomationLibrary.take_high_res_screenshot(int(PARAMS['w']), int(PARAMS['h']), PARAMS['name'])\n"
            "    saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())\n"
            "    print('@@UMCP@@' + json.dumps({'status':'requested','saved_dir':saved}))\n"
            "except Exception as e:\n"
            "    print('@@UMCP@@' + json.dumps({'status':'error','message':str(e)}))\n"
        )
        try:
            req = _exec(req_body, {"w": width, "h": height, "name": name})
            if req.get("status") != "requested":
                return json.dumps(req, indent=2)
            saved = req["saved_dir"].replace("\\", "/").rstrip("/")
            # High-res screenshots land under Saved/Screenshots/<platform>/; poll a few likely paths.
            candidates = [f"{saved}/Screenshots/WindowsEditor/{name}.png",
                          f"{saved}/Screenshots/Windows/{name}.png",
                          f"{saved}/Screenshots/{name}.png"]
            deadline = time.time() + max(1.0, float(wait_seconds))
            found = None
            while time.time() < deadline and not found:
                for c in candidates:
                    if os.path.exists(c):
                        found = c
                        break
                if not found:
                    time.sleep(0.3)
            if found:
                return json.dumps({"status": "success", "image": found,
                                   "note": "GAME-viewport screenshot; Read the path to view."}, indent=2)
            return json.dumps({"status": "pending",
                               "message": "screenshot requested but PNG not found within %ss; it may be in Saved/Screenshots/<platform>/ under name '%s'." % (wait_seconds, name),
                               "checked": candidates}, indent=2)
        except Exception as e:
            return f"Error: {e}"
