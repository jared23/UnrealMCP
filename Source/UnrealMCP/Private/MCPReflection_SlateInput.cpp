// ============================================================================
// MCPReflection_SlateInput.cpp — C++ #54. MCP_TOOLING_GAPS batch 3: SLATE-LEVEL
// synthetic input during PIE (the "Synthetic input injection during PIE (HIGH)"
// gap). SELF-CONTAINED translation unit.
//
// WHY THIS EXISTS (vs C++ #53 InjectPieKey / pie_input):
//   InjectPieKey feeds APlayerController::InputKey -> the player's UPlayerInput,
//   which drives GAMEPLAY input (legacy + Enhanced Input IA_* actions -> pawn
//   movement, fire, etc.). It does NOT touch the Slate/UMG layer, so it cannot
//   exercise MENU navigation, button hover/click hit-testing, editable-text entry
//   (room codes), or gamepad menu nav — all of which flow through FSlateApplication,
//   NOT the PlayerController. This file injects at the Slate layer, the same path a
//   real keypress/mouse-move/gamepad button takes, so UMG widgets actually respond.
//
//   #3S InjectSlateKey(key, down|up, mods, user)          -> ProcessKeyDownEvent / ProcessKeyUpEvent
//   #3S InjectSlateChar(text, user)                        -> ProcessKeyCharEvent per char (focused text box)
//   #3S InjectSlateMouseMove(x, y, normalized, user)       -> SetCursorPos + ProcessMouseMoveEvent (hover)
//   #3S InjectSlateMouseButton(button, event, x, y, norm)  -> ProcessMouseButtonDown/Up (click hitboxes)
//   #3S InjectSlateGamepad(key, pressed|released|analog,v) -> OnControllerButtonPressed/Released/OnControllerAnalog
//
// LINK-RISK DISCIPLINE: every symbol is Slate / SlateCore / InputCore /
// ApplicationCore (all already linked; ApplicationCore is transitive via Slate) +
// Engine/UnrealEd -> NO Build.cs change, NO engine export patch. All calls were
// signature-verified against the 5.8 engine source before authoring; the few
// version-sensitive spots are tagged "VERIFY". Editor-only (PIE testing).
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Engine/Engine.h"
#include "Engine/World.h"
#include "Engine/GameViewportClient.h"

#include "InputCoreTypes.h"                                   // FKey / EKeys / FGamepadKeyNames::Type

#if WITH_EDITOR
#include "Framework/Application/SlateApplication.h"           // FSlateApplication
#include "Input/Events.h"                                     // FKeyEvent / FCharacterEvent / FPointerEvent
#include "Layout/Geometry.h"                                  // FGeometry (viewport rect -> screen mapping)
#include "Widgets/SViewport.h"                                // SViewport (game viewport widget)
#include "Widgets/SWindow.h"                                  // SWindow::GetNativeWindow
#include "GenericPlatform/GenericApplication.h"               // FModifierKeysState
#include "GenericPlatform/GenericWindow.h"                    // FGenericWindow
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h" // IPlatformInputDeviceMapper (gamepad user/device)
#endif

namespace
{
    FString SI_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        // Condensed (single-line): the pie_ui_* tools print this raw via _cpp_delegate, and the bridge reads
        // JSON from ONE line after the marker — pretty/multi-line would be truncated to "{".
        FString Out;
        TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
            TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString SI_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return SI_Serialize(O);
    }

#if WITH_EDITOR
    // The active PIE world (first PIE world context). Slate input is global, but we use the
    // PIE world's game viewport widget to map viewport-local coordinates -> absolute screen space.
    UWorld* SI_PieWorld()
    {
        if (GEngine)
        {
            for (const FWorldContext& C : GEngine->GetWorldContexts())
            {
                if (C.WorldType == EWorldType::PIE && C.World())
                {
                    return C.World();
                }
            }
        }
        return nullptr;
    }

    // The SViewport widget the game is rendering into (wherever it lives: the level-editor
    // viewport for play-in-viewport, or a separate PIE window). Its cached geometry gives the
    // absolute on-screen rectangle we map coordinates against.
    TSharedPtr<SViewport> SI_GameViewportWidget()
    {
        UWorld* World = SI_PieWorld();
        UGameViewportClient* GVC = World ? World->GetGameViewport() : nullptr;
        if (!GVC && GEngine)
        {
            GVC = GEngine->GameViewport;
        }
        return GVC ? GVC->GetGameViewportWidget() : nullptr;
    }

    FModifierKeysState SI_Mods(bool bShift, bool bCtrl, bool bAlt, bool bCmd)
    {
        // Set BOTH left+right for each held modifier so IsShiftDown()/IsControlDown()/etc. report true.
        return FModifierKeysState(bShift, bShift, bCtrl, bCtrl, bAlt, bAlt, bCmd, bCmd, /*caps*/false);
    }

    // Resolve a mouse button name -> its FKey. Returns EKeys::Invalid on an unknown name.
    FKey SI_MouseButton(const FString& Name)
    {
        const FString N = Name.ToLower();
        if (N == TEXT("left")   || N == TEXT("leftmousebutton"))   return EKeys::LeftMouseButton;
        if (N == TEXT("right")  || N == TEXT("rightmousebutton"))  return EKeys::RightMouseButton;
        if (N == TEXT("middle") || N == TEXT("middlemousebutton")) return EKeys::MiddleMouseButton;
        if (N == TEXT("thumb1") || N == TEXT("thumbmousebutton"))  return EKeys::ThumbMouseButton;
        if (N == TEXT("thumb2") || N == TEXT("thumbmousebutton2")) return EKeys::ThumbMouseButton2;
        return EKeys::Invalid;
    }
#endif // WITH_EDITOR
}

// ---- #3S  inject_slate_key (keyboard down/up at the Slate layer — drives UMG nav + shortcuts) ----
FString UMCPReflectionLibrary::InjectSlateKey(const FString& KeyName, const FString& EventName,
                                              bool bShift, bool bCtrl, bool bAlt, bool bCmd, int32 UserIndex)
{
#if WITH_EDITOR
    if (!FSlateApplication::IsInitialized()) return SI_Error(TEXT("Slate is not initialized"));

    const FName KeyFName(*KeyName);
    FKey Key(KeyFName);   // two statements: FKey Key(FName(*KeyName)) would be a most-vexing-parse fn decl
    if (!Key.IsValid())
    {
        return SI_Error(FString::Printf(TEXT("invalid key '%s' (use FKey names, e.g. Enter, Escape, Down, Tab, A)"), *KeyName));
    }

    const FString E = EventName.ToLower();
    const bool bUp = (E == TEXT("up") || E == TEXT("released") || E == TEXT("keyup"));

    // FKeyEvent(FKey, FModifierKeysState, UserIndex, bIsRepeat, CharacterCode, KeyCode). We pass 0 char/key
    // codes — Slate routes keyboard NAVIGATION (Enter/arrows/Tab) and accelerators by FKey, not raw scancode.
    FKeyEvent KeyEvent(Key, SI_Mods(bShift, bCtrl, bAlt, bCmd), (uint32)FMath::Max(0, UserIndex),
                       /*bIsRepeat*/false, /*CharacterCode*/0, /*KeyCode*/0);

    FSlateApplication& Slate = FSlateApplication::Get();
    const bool bHandled = bUp ? Slate.ProcessKeyUpEvent(KeyEvent) : Slate.ProcessKeyDownEvent(KeyEvent);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("key"), Key.ToString());
    O->SetStringField(TEXT("event"), bUp ? TEXT("up") : TEXT("down"));
    O->SetBoolField(TEXT("handled"), bHandled);
    return SI_Serialize(O);
#else
    return SI_Error(TEXT("editor-only"));
#endif
}

// ---- #3S  inject_slate_char (type characters into the FOCUSED editable text box) -----------------
FString UMCPReflectionLibrary::InjectSlateChar(const FString& Text, int32 UserIndex)
{
#if WITH_EDITOR
    if (!FSlateApplication::IsInitialized()) return SI_Error(TEXT("Slate is not initialized"));
    if (Text.IsEmpty()) return SI_Error(TEXT("empty text"));

    FSlateApplication& Slate = FSlateApplication::Get();
    const FModifierKeysState Mods = SI_Mods(false, false, false, false);
    int32 Sent = 0;
    // A character event is what actually INSERTS text into a focused SEditableText/SMultiLineEditableText
    // (the room-code box). Focus must already be on the text box — click it first via inject_slate_mouse_button.
    for (const TCHAR Ch : Text)
    {
        FCharacterEvent CharEvent(Ch, Mods, (uint32)FMath::Max(0, UserIndex), /*bIsRepeat*/false);
        Slate.ProcessKeyCharEvent(CharEvent);
        ++Sent;
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("text"), Text);
    O->SetNumberField(TEXT("chars_sent"), Sent);
    O->SetStringField(TEXT("note"), TEXT("inserts into the FOCUSED editable text widget — click the text box first"));
    return SI_Serialize(O);
#else
    return SI_Error(TEXT("editor-only"));
#endif
}

// ---- #3S  inject_slate_mouse_move (move the cursor -> hover hit-testing) -------------------------
FString UMCPReflectionLibrary::InjectSlateMouseMove(float X, float Y, bool bNormalized, int32 /*UserIndex*/)
{
#if WITH_EDITOR
    if (!FSlateApplication::IsInitialized()) return SI_Error(TEXT("Slate is not initialized"));
    TSharedPtr<SViewport> VP = SI_GameViewportWidget();
    if (!VP.IsValid()) return SI_Error(TEXT("no game viewport widget — start Play-In-Editor first"));

    const FGeometry& Geo = VP->GetTickSpaceGeometry();
    // Normalized (0..1 across the viewport) is resolution-independent — best for HUD hit-testing; else raw
    // viewport-local pixels. GetAbsolutePositionAtCoordinates maps a normalized point to absolute screen space.
    const FVector2D Screen = bNormalized
        ? FVector2D(Geo.GetAbsolutePositionAtCoordinates(FVector2f(X, Y)))
        : FVector2D(Geo.GetAbsolutePosition()) + FVector2D(X, Y);

    FSlateApplication& Slate = FSlateApplication::Get();
    Slate.SetCursorPos(Screen);   // keep the OS/Slate cursor in sync so subsequent clicks land here
    FPointerEvent MouseEvent(/*PointerIndex*/0, Screen, /*LastScreenPos*/Screen, TSet<FKey>(),
                             EKeys::Invalid, /*WheelDelta*/0.f, SI_Mods(false, false, false, false));
    const bool bHandled = Slate.ProcessMouseMoveEvent(MouseEvent);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetNumberField(TEXT("screen_x"), Screen.X);
    O->SetNumberField(TEXT("screen_y"), Screen.Y);
    O->SetBoolField(TEXT("normalized"), bNormalized);
    O->SetBoolField(TEXT("handled"), bHandled);
    return SI_Serialize(O);
#else
    return SI_Error(TEXT("editor-only"));
#endif
}

// ---- #3S  inject_slate_mouse_button (down/up/click at a viewport point — click hitboxes) ---------
FString UMCPReflectionLibrary::InjectSlateMouseButton(const FString& ButtonName, const FString& EventName,
                                                      float X, float Y, bool bNormalized)
{
#if WITH_EDITOR
    if (!FSlateApplication::IsInitialized()) return SI_Error(TEXT("Slate is not initialized"));
    TSharedPtr<SViewport> VP = SI_GameViewportWidget();
    if (!VP.IsValid()) return SI_Error(TEXT("no game viewport widget — start Play-In-Editor first"));

    const FKey Button = SI_MouseButton(ButtonName);
    if (!Button.IsValid()) return SI_Error(FString::Printf(TEXT("invalid mouse button '%s' (left|right|middle|thumb1|thumb2)"), *ButtonName));

    const FGeometry& Geo = VP->GetTickSpaceGeometry();
    const FVector2D Screen = bNormalized
        ? FVector2D(Geo.GetAbsolutePositionAtCoordinates(FVector2f(X, Y)))
        : FVector2D(Geo.GetAbsolutePosition()) + FVector2D(X, Y);

    FSlateApplication& Slate = FSlateApplication::Get();
    // ProcessMouseButtonDownEvent takes the platform window under the cursor so Slate can route hit-testing.
    TSharedPtr<SWindow> Win = Slate.FindWidgetWindow(VP.ToSharedRef());
    TSharedPtr<FGenericWindow> NativeWin = Win.IsValid() ? Win->GetNativeWindow() : nullptr;

    const FString E = EventName.ToLower();
    const bool bDoDown = (E != TEXT("up"));                                  // down|click|doubleclick all press
    const bool bDoUp   = (E == TEXT("up") || E == TEXT("click") || E == TEXT("doubleclick") || E.IsEmpty());
    const bool bDblClick = (E == TEXT("doubleclick"));

    // Keep the cursor at the target first so hover/hit-test state matches before the press.
    Slate.SetCursorPos(Screen);
    {
        FPointerEvent MoveEvent(0, Screen, Screen, TSet<FKey>(), EKeys::Invalid, 0.f, SI_Mods(false,false,false,false));
        Slate.ProcessMouseMoveEvent(MoveEvent);
    }

    TSet<FKey> Pressed;  Pressed.Add(Button);
    bool bDownHandled = false, bUpHandled = false;
    if (bDoDown)
    {
        FPointerEvent DownEvent(0, Screen, Screen, Pressed, Button, 0.f, SI_Mods(false,false,false,false));
        bDownHandled = bDblClick ? Slate.ProcessMouseButtonDoubleClickEvent(NativeWin, DownEvent)
                                 : Slate.ProcessMouseButtonDownEvent(NativeWin, DownEvent);
    }
    if (bDoUp)
    {
        // On release the button is no longer in the pressed set.
        FPointerEvent UpEvent(0, Screen, Screen, TSet<FKey>(), Button, 0.f, SI_Mods(false,false,false,false));
        bUpHandled = Slate.ProcessMouseButtonUpEvent(UpEvent);
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("button"), Button.ToString());
    O->SetStringField(TEXT("event"), E.IsEmpty() ? TEXT("click") : E);
    O->SetNumberField(TEXT("screen_x"), Screen.X);
    O->SetNumberField(TEXT("screen_y"), Screen.Y);
    O->SetBoolField(TEXT("down_handled"), bDownHandled);
    O->SetBoolField(TEXT("up_handled"), bUpHandled);
    O->SetBoolField(TEXT("had_window"), NativeWin.IsValid());
    return SI_Serialize(O);
#else
    return SI_Error(TEXT("editor-only"));
#endif
}

// ---- #3S  inject_slate_gamepad (controller button / analog for menu nav) ------------------------
FString UMCPReflectionLibrary::InjectSlateGamepad(const FString& KeyName, const FString& EventName,
                                                  float AnalogValue, int32 /*ControllerId*/)
{
#if WITH_EDITOR
    if (!FSlateApplication::IsInitialized()) return SI_Error(TEXT("Slate is not initialized"));
    if (KeyName.IsEmpty()) return SI_Error(TEXT("empty gamepad key name (e.g. Gamepad_FaceButton_Bottom, Gamepad_DPad_Up, Gamepad_LeftX)"));

    // FGamepadKeyNames::Type is an FName; the controller key routes to Slate's navigation config
    // (UI actions Accept/Back/Up/Down are bound to these gamepad keys).
    const FGamepadKeyNames::Type GKey(*KeyName);

    IPlatformInputDeviceMapper& Mapper = IPlatformInputDeviceMapper::Get();
    const FPlatformUserId UserId = Mapper.GetPrimaryPlatformUser();
    const FInputDeviceId DeviceId = Mapper.GetPrimaryInputDeviceForUser(UserId);

    FSlateApplication& Slate = FSlateApplication::Get();
    const FString E = EventName.ToLower();
    bool bHandled = false;
    FString Kind;
    if (E == TEXT("analog"))
    {
        bHandled = Slate.OnControllerAnalog(GKey, UserId, DeviceId, AnalogValue);
        Kind = TEXT("analog");
    }
    else if (E == TEXT("released") || E == TEXT("up"))
    {
        bHandled = Slate.OnControllerButtonReleased(GKey, UserId, DeviceId, /*IsRepeat*/false);
        Kind = TEXT("released");
    }
    else
    {
        bHandled = Slate.OnControllerButtonPressed(GKey, UserId, DeviceId, /*IsRepeat*/false);
        Kind = TEXT("pressed");
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("key"), KeyName);
    O->SetStringField(TEXT("event"), Kind);
    if (E == TEXT("analog")) O->SetNumberField(TEXT("value"), AnalogValue);
    O->SetBoolField(TEXT("handled"), bHandled);
    return SI_Serialize(O);
#else
    return SI_Error(TEXT("editor-only"));
#endif
}
