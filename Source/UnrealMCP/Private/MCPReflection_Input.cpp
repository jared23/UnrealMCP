// Clean-room reflection helper (SEPARATE TU) for the UnrealMCP plugin. See MCPReflectionLibrary.h.
//
// GetAllInputKeysJson(): enumerate the FULL EKeys registry (InputCore) so the Python
// `list_input_keys` tool can return the complete key set instead of a curated subset.
// This is a member-function definition of UMCPReflectionLibrary declared in
// MCPReflectionLibrary.h; defining it in its own translation unit compiles fine.

#include "MCPReflectionLibrary.h"

#include "InputCoreTypes.h"              // EKeys / FKey (InputCore module)
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// File-local JSON serializer. The anonymous-namespace helpers in MCPReflectionLibrary.cpp
// are per-TU (internal linkage), so this separate .cpp defines its own — no ODR clash.
namespace
{
    FString MCPInput_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }
}

FString UMCPReflectionLibrary::GetAllInputKeysJson()
{
    // EKeys::Initialize() is invoked by engine init; in any normal editor/commandlet context
    // the registry is populated by the time Python can call this. Enumerate defensively anyway.
    TArray<FKey> AllKeys;
    EKeys::GetAllKeys(AllKeys);

    TArray<TSharedPtr<FJsonValue>> KeyArray;
    KeyArray.Reserve(AllKeys.Num());

    for (const FKey& Key : AllKeys)
    {
        // Guard against any key whose key-details metadata isn't loaded/registered. Most of the
        // Is*() predicates below funnel through the cached FKeyDetails; an invalid key can return
        // defaults or (for GetDisplayName) touch unloaded data, so skip anything not valid.
        if (!Key.IsValid())
        {
            continue;
        }

        TSharedRef<FJsonObject> KeyObj = MakeShared<FJsonObject>();

        // Names. GetFName() is the stable registry name; GetDisplayName() is the human label.
        KeyObj->SetStringField(TEXT("name"), Key.GetFName().ToString());
        KeyObj->SetStringField(TEXT("display_name"), Key.GetDisplayName().ToString());

        // Category booleans available on FKey in UE 5.8.
        KeyObj->SetBoolField(TEXT("is_gamepad_key"), Key.IsGamepadKey());
        KeyObj->SetBoolField(TEXT("is_mouse_button"), Key.IsMouseButton());
        KeyObj->SetBoolField(TEXT("is_modifier_key"), Key.IsModifierKey());
        KeyObj->SetBoolField(TEXT("is_touch"), Key.IsTouch());
        KeyObj->SetBoolField(TEXT("is_axis_1d"), Key.IsAxis1D());
        KeyObj->SetBoolField(TEXT("is_axis_2d"), Key.IsAxis2D());
        KeyObj->SetBoolField(TEXT("is_axis_3d"), Key.IsAxis3D());
        KeyObj->SetBoolField(TEXT("is_analog"), Key.IsAnalog());
        KeyObj->SetBoolField(TEXT("is_digital"), Key.IsDigital());

        KeyArray.Add(MakeShared<FJsonValueObject>(KeyObj));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetNumberField(TEXT("count"), KeyArray.Num());
    Root->SetArrayField(TEXT("keys"), KeyArray);
    return MCPInput_SerializeJson(Root);
}
