// Clean-room reflection helpers (SEPARATE TU) for the UnrealMCP plugin. See MCPReflectionLibrary.h.
//
// C++ #42 (2026-08-19) — WORLD "C++-A infra ext" batch. Reaches editor/world objects that have NO
// BlueprintCallable / reflected Python surface in UE 5.8:
//   1) ListEditorModesJson              (READ)   enumerate registered editor modes (id/name/active/visible)
//   2) SetEditorModeJson                (WRITE)  activate an editor mode (captures prior for undo)
//   3) GetWorldPartitionSettingsJson    (READ)   live UWorldPartition settings + spatial-hash editing grids
//   4) SetWorldPartitionSettingsJson    (WRITE)  set editable UWorldPartition props (+ SetEnableStreaming)
//   5) SetRuntimeGridJson               (WRITE)  edit a UWorldPartitionRuntimeSpatialHash editing grid
//   6) BuildWorldPartitionJson          (ACTION) HONEST build report — real build is commandlet-only
//   7) NavigationBuildStatusJson        (READ)   UNavigationSystemV1 build-progress (non-UFUNCTION funcs)
//
// WHY C++ (Python cannot reach these in 5.8):
//   * The live UWorldPartition object is not exposed to editor Python — the C++ path
//     GEditor->GetEditorWorldContext().World()->GetWorldPartition() is the only reachable handle.
//   * Editor modes go through the non-UFUNCTION global FEditorModeTools (GLevelEditorModeTools()).
//   * UNavigationSystemV1::IsNavigationBuildInProgress()/GetNumRemainingBuildTasks() are not UFUNCTIONs.
//
// MODULES: everything used here links transitively through modules the plugin ALREADY depends on:
//   * GLevelEditorModeTools() / FEditorModeTools / UAssetEditorSubsystem      -> UnrealEd (UNREALED_API), a dep.
//   * FEditorModeInfo::IsVisible() / FBuiltinEditorModes::EM_Default          -> EditorFramework (EDITORFRAMEWORK_API),
//                                                                                a PUBLIC dep of UnrealEd -> transitively linked.
//   * UWorldPartition / UWorldPartitionRuntimeSpatialHash / FSpatialHashRuntimeGrid -> Engine (ENGINE_API), a dep.
//   * UNavigationSystemV1                                                     -> NavigationSystem (NAVIGATIONSYSTEM_API),
//                                                                                a PUBLIC dep of UnrealEd -> transitively linked.
//   => NO Build.cs change is strictly required, NO engine export patch. (See the FINAL REPORT for the optional
//      explicit-dep recommendation.)
//
// Conventions mirrored from the sibling TUs (MCPReflection_SmallCats.cpp / MCPReflection_Structs.cpp):
//   * File-local anon-namespace helpers prefixed MCPWx_ (internal linkage per-TU -> no ODR clash).
//   * JSON returns {"error": "..."} on any miss; every pointer is null/bounds-guarded; never crash.
//   * The whole batch is editor-only -> handler bodies are #if WITH_EDITOR, returning an editor-only error otherwise.

#include "MCPReflectionLibrary.h"

#if WITH_EDITOR
#include "Editor.h"                                   // GEditor, GLevelEditorModeTools() (UnrealEd)
#include "Editor/EditorEngine.h"                      // UEditorEngine::GetEditorWorldContext (UnrealEd)
#include "EditorModeManager.h"                        // FEditorModeTools (UnrealEd)
#include "EditorModes.h"                              // FBuiltinEditorModes::EM_Default (EditorFramework)
#include "Tools/Modes.h"                              // FEditorModeInfo / FEditorModeID (EditorFramework)
#include "Subsystems/AssetEditorSubsystem.h"          // UAssetEditorSubsystem (UnrealEd)

#include "Engine/World.h"                             // UWorld::GetWorldPartition / IsPartitionedWorld (Engine)
#include "WorldPartition/WorldPartition.h"            // UWorldPartition (Engine)
#include "WorldPartition/WorldPartitionRuntimeHash.h" // UWorldPartitionRuntimeHash (Engine)
#include "WorldPartition/WorldPartitionRuntimeSpatialHash.h" // UWorldPartitionRuntimeSpatialHash / FSpatialHashRuntimeGrid (Engine)

#include "NavigationSystem.h"                         // UNavigationSystemV1 (NavigationSystem)

#include "Misc/Paths.h"                               // FPaths::GetProjectFilePath (Core)
#endif // WITH_EDITOR

#include "UObject/UnrealType.h"                       // FProperty / FArrayProperty / FScriptArrayHelper / FPropertyChangedEvent (CoreUObject)
#include "UObject/EnumProperty.h"                     // FEnumProperty (CoreUObject)
#include "UObject/Class.h"                            // UClass (CoreUObject)

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

namespace
{
    // ---- file-local JSON boilerplate (internal linkage; per-TU) -------------------------------
    FString MCPWx_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPWx_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPWx_SerializeJson(Root);
    }

#if WITH_EDITOR
    // Resolve the editor world (the map open in the level editor). Null if no editor world (e.g. cooked).
    UWorld* MCPWx_EditorWorld()
    {
        if (!GEditor)
        {
            return nullptr;
        }
        return GEditor->GetEditorWorldContext().World();
    }

    // Resolve the editor world's live UWorldPartition. OutErr is a clean message on miss.
    UWorldPartition* MCPWx_ResolveWorldPartition(FString& OutErr)
    {
        UWorld* World = MCPWx_EditorWorld();
        if (!World)
        {
            OutErr = TEXT("no editor world (open a map in the level editor first)");
            return nullptr;
        }
        UWorldPartition* WP = World->GetWorldPartition();
        if (!WP)
        {
            OutErr = TEXT("world is not partitioned (this map has no UWorldPartition; use a World Partition map)");
            return nullptr;
        }
        return WP;
    }

    // Apply one bare JSON value to one FProperty at ValuePtr — typed fast-paths + ImportText_Direct universal
    // fallback. Compact clone of MCPReflection_SmallCats.cpp::MCPSc_ApplyJsonToProperty (a static in another TU).
    // Struct/array/map/etc. expect a UE ExportText string passed as a JSON string (e.g. "(X=1.0,Y=2.0)").
    bool MCPWx_ApplyJsonToProperty(FProperty* Prop, void* ValuePtr, UObject* Owner,
                                   const TSharedPtr<FJsonValue>& V, FString& OutErr)
    {
        if (!Prop || !ValuePtr || !V.IsValid())
        {
            OutErr = TEXT("null prop/value");
            return false;
        }
        if (FBoolProperty* BoolP = CastField<FBoolProperty>(Prop))
        {
            BoolP->SetPropertyValue(ValuePtr, V->AsBool());
            return true;
        }
        if (FEnumProperty* EnumP = CastField<FEnumProperty>(Prop))
        {
            FNumericProperty* U = EnumP->GetUnderlyingProperty();
            UEnum* Enum = EnumP->GetEnum();
            int64 Val = 0;
            if (V->Type == EJson::String && Enum)
            {
                Val = Enum->GetValueByNameString(V->AsString());
                if (Val == INDEX_NONE) { OutErr = FString::Printf(TEXT("bad enum name '%s'"), *V->AsString()); return false; }
            }
            else { Val = (int64)V->AsNumber(); }
            if (U) { U->SetIntPropertyValue(ValuePtr, Val); return true; }
            OutErr = TEXT("enum has no underlying");
            return false;
        }
        if (FByteProperty* ByteP = CastField<FByteProperty>(Prop))   // incl. TEnumAsByte
        {
            if (V->Type == EJson::String && ByteP->Enum)
            {
                const int64 EV = ByteP->Enum->GetValueByNameString(V->AsString());
                if (EV == INDEX_NONE) { OutErr = FString::Printf(TEXT("bad enum name '%s'"), *V->AsString()); return false; }
                ByteP->SetPropertyValue(ValuePtr, (uint8)EV);
                return true;
            }
            ByteP->SetPropertyValue(ValuePtr, (uint8)V->AsNumber());
            return true;
        }
        if (FNumericProperty* NumP = CastField<FNumericProperty>(Prop))
        {
            if (NumP->IsFloatingPoint()) { NumP->SetFloatingPointPropertyValue(ValuePtr, V->AsNumber()); }
            else { NumP->SetIntPropertyValue(ValuePtr, (int64)V->AsNumber()); }
            return true;
        }
        if (FStrProperty* StrP = CastField<FStrProperty>(Prop))
        {
            StrP->SetPropertyValue(ValuePtr, V->AsString());
            return true;
        }
        if (FNameProperty* NameP = CastField<FNameProperty>(Prop))
        {
            NameP->SetPropertyValue(ValuePtr, FName(*V->AsString()));
            return true;
        }
        if (FTextProperty* TextP = CastField<FTextProperty>(Prop))
        {
            TextP->SetPropertyValue(ValuePtr, FText::FromString(V->AsString()));
            return true;
        }
        if (FObjectPropertyBase* ObjP = CastField<FObjectPropertyBase>(Prop))
        {
            const FString PathStr = V->AsString();
            UObject* Obj = (PathStr.IsEmpty() || PathStr == TEXT("None"))
                ? nullptr
                : StaticLoadObject(ObjP->PropertyClass, nullptr, *PathStr);
            ObjP->SetObjectPropertyValue(ValuePtr, Obj);
            return true;
        }

        // Struct/array/map/etc.: universal text import (caller passes a UE ExportText string as a JSON string).
        FString Text;
        if (V->Type == EJson::String) { Text = V->AsString(); }
        else if (V->Type == EJson::Boolean) { Text = V->AsBool() ? TEXT("true") : TEXT("false"); }
        else if (V->Type == EJson::Number)
        {
            const double D = V->AsNumber();
            Text = (D == FMath::TruncToDouble(D)) ? FString::Printf(TEXT("%lld"), (int64)D) : FString::SanitizeFloat(D);
        }
        else { OutErr = TEXT("unsupported JSON value for struct/array prop (pass a UE ExportText string)"); return false; }

        const TCHAR* Result = Prop->ImportText_Direct(*Text, ValuePtr, Owner, PPF_None, nullptr);
        if (Result == nullptr) { OutErr = FString::Printf(TEXT("ImportText failed for '%s'"), *Text); return false; }
        return true;
    }

    // Single-property ExportText (the prior-value capture used across the WRITE handlers).
    FString MCPWx_ExportProp(FProperty* Prop, const void* ValuePtr, UObject* Owner)
    {
        FString Out;
        if (Prop && ValuePtr)
        {
            Prop->ExportTextItem_Direct(Out, ValuePtr, /*Default*/ nullptr, /*Parent*/ Owner, PPF_None);
        }
        return Out;
    }

    // Parse a JSON document string into a root object. Returns false (with OutErr) on a non-object / bad JSON.
    bool MCPWx_ParseObject(const FString& Json, TSharedPtr<FJsonObject>& Out, FString& OutErr)
    {
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(Json);
        if (!FJsonSerializer::Deserialize(R, Out) || !Out.IsValid())
        {
            OutErr = TEXT("invalid JSON object");
            return false;
        }
        return true;
    }
#endif // WITH_EDITOR
}

// ================================================================================================
// 1) READ: enumerate the registered editor modes. The registry lives behind the non-UFUNCTION
//    UAssetEditorSubsystem::GetEditorModeInfoOrderedByPriority() + the global FEditorModeTools
//    (GLevelEditorModeTools()); neither is Python-reachable. Works on ANY level.
// ================================================================================================
FString UMCPReflectionLibrary::ListEditorModesJson()
{
#if WITH_EDITOR
    if (!GEditor)
    {
        return MCPWx_Error(TEXT("no GEditor (editor not running)"));
    }
    UAssetEditorSubsystem* AES = GEditor->GetEditorSubsystem<UAssetEditorSubsystem>();
    if (!AES)
    {
        return MCPWx_Error(TEXT("no UAssetEditorSubsystem"));
    }

    FEditorModeTools& Tools = GLevelEditorModeTools();

    TArray<FEditorModeInfo> Infos = AES->GetEditorModeInfoOrderedByPriority();

    TArray<TSharedPtr<FJsonValue>> ModesJson;
    TArray<TSharedPtr<FJsonValue>> ActiveIds;
    for (const FEditorModeInfo& Info : Infos)
    {
        const bool bActive = Tools.IsModeActive(Info.ID);
        if (bActive)
        {
            ActiveIds.Add(MakeShared<FJsonValueString>(Info.ID.ToString()));
        }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("id"), Info.ID.ToString());
        J->SetStringField(TEXT("name"), Info.Name.ToString());
        J->SetBoolField(TEXT("is_active"), bActive);
        J->SetBoolField(TEXT("is_visible"), Info.IsVisible());
        J->SetBoolField(TEXT("is_default_mode"), Tools.IsDefaultMode(Info.ID));
        J->SetNumberField(TEXT("priority"), Info.PriorityOrder);
        ModesJson.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetNumberField(TEXT("mode_count"), ModesJson.Num());
    Root->SetArrayField(TEXT("modes"), ModesJson);
    Root->SetArrayField(TEXT("active_mode_ids"), ActiveIds);
    Root->SetBoolField(TEXT("is_default_mode_active"), Tools.IsDefaultModeActive());
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 2) WRITE: activate an editor mode via GLevelEditorModeTools().ActivateMode (which itself deactivates
//    incompatible / other-visible modes). Captures the prior primary (non-default) active mode + the full
//    prior active set for a faithful undo. An empty / "default" / "EM_Default" / "none" ModeId restores the
//    default mode set (DeactivateAllModes -> ActivateMode(EM_Default)).
// ================================================================================================
FString UMCPReflectionLibrary::SetEditorModeJson(const FString& ModeId)
{
#if WITH_EDITOR
    if (!GEditor)
    {
        return MCPWx_Error(TEXT("no GEditor (editor not running)"));
    }
    UAssetEditorSubsystem* AES = GEditor->GetEditorSubsystem<UAssetEditorSubsystem>();
    if (!AES)
    {
        return MCPWx_Error(TEXT("no UAssetEditorSubsystem"));
    }

    FEditorModeTools& Tools = GLevelEditorModeTools();

    // ---- capture prior state (for the undo) ----------------------------------------------------
    TArray<TSharedPtr<FJsonValue>> PrevActive;
    FString PrevPrimary;   // first active mode that is NOT a default mode (the "tool mode" the user was in)
    {
        TArray<FEditorModeInfo> Infos = AES->GetEditorModeInfoOrderedByPriority();
        for (const FEditorModeInfo& Info : Infos)
        {
            if (Tools.IsModeActive(Info.ID))
            {
                PrevActive.Add(MakeShared<FJsonValueString>(Info.ID.ToString()));
                if (PrevPrimary.IsEmpty() && !Tools.IsDefaultMode(Info.ID))
                {
                    PrevPrimary = Info.ID.ToString();
                }
            }
        }
    }

    FString Trimmed = ModeId;
    Trimmed.TrimStartAndEndInline();
    const bool bRestoreDefault = Trimmed.IsEmpty()
        || Trimmed.Equals(TEXT("default"), ESearchCase::IgnoreCase)
        || Trimmed.Equals(TEXT("none"), ESearchCase::IgnoreCase)
        || Trimmed.Equals(TEXT("EM_Default"), ESearchCase::IgnoreCase);

    FString ResolvedTarget;
    if (bRestoreDefault)
    {
        // Restore the default mode set: drop every active mode, then re-activate the defaults immediately
        // (ActivateMode(EM_Default) special-cases to activate all registered default modes).
        Tools.DeactivateAllModes();
        Tools.ActivateMode(FBuiltinEditorModes::EM_Default);
        ResolvedTarget = TEXT("EM_Default");
    }
    else
    {
        // Validate the mode is registered before activating (ActivateMode logs+no-ops on an unknown id).
        FEditorModeInfo Found;
        if (!AES->FindEditorModeInfo(FEditorModeID(*Trimmed), Found))
        {
            return MCPWx_Error(FString::Printf(
                TEXT("unknown editor mode '%s' (call list_editor_modes for valid ids)"), *Trimmed));
        }
        Tools.ActivateMode(FEditorModeID(*Trimmed));
        ResolvedTarget = Trimmed;
    }

    const bool bIsActiveNow = bRestoreDefault
        ? Tools.IsDefaultModeActive()
        : Tools.IsModeActive(FEditorModeID(*ResolvedTarget));

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("mode"), ResolvedTarget);
    Root->SetStringField(TEXT("prev_mode"), PrevPrimary);        // "" == was in the default mode only
    Root->SetArrayField(TEXT("prev_modes"), PrevActive);         // full prior active set (diagnostic)
    Root->SetBoolField(TEXT("restored_default"), bRestoreDefault);
    Root->SetBoolField(TEXT("is_active"), bIsActiveNow);
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 3) READ: the live UWorldPartition settings. Resolves via the C++-only editor-world path Python lacks
//    (GEditor->GetEditorWorldContext().World()->GetWorldPartition()). Reports the streaming settings + the
//    runtime hash + (spatial hash) the EDITING grids array.
// ================================================================================================
FString UMCPReflectionLibrary::GetWorldPartitionSettingsJson()
{
#if WITH_EDITOR
    FString Err;
    UWorldPartition* WP = MCPWx_ResolveWorldPartition(Err);
    if (!WP)
    {
        return MCPWx_Error(Err);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("world_partition_path"), WP->GetPathName());

    // Method-based state (public ENGINE_API getters — not reflected UPROPERTYs).
    Root->SetBoolField(TEXT("b_enable_streaming"), WP->bEnableStreaming);
    Root->SetBoolField(TEXT("supports_streaming"), WP->SupportsStreaming());
    Root->SetBoolField(TEXT("is_streaming_enabled"), WP->IsStreamingEnabled());
    Root->SetBoolField(TEXT("is_streaming_enabled_in_editor"), WP->IsStreamingEnabledInEditor());
    Root->SetBoolField(TEXT("is_initialized"), WP->IsInitialized());
    Root->SetBoolField(TEXT("is_main_world_partition"), WP->IsMainWorldPartition());
    Root->SetStringField(TEXT("editor_name"), WP->GetWorldPartitionEditorName().ToString());

    // Reflected enum/settings props (readable names via ExportText).
    auto ExportNamed = [&](const TCHAR* PropName, const TCHAR* JsonKey)
    {
        if (FProperty* P = FindFProperty<FProperty>(WP->GetClass(), PropName))
        {
            const void* VP = P->ContainerPtrToValuePtr<void>(WP);
            Root->SetStringField(JsonKey, MCPWx_ExportProp(P, VP, WP));
        }
    };
    ExportNamed(TEXT("ServerStreamingMode"), TEXT("server_streaming_mode"));
    ExportNamed(TEXT("ServerStreamingOutMode"), TEXT("server_streaming_out_mode"));
    ExportNamed(TEXT("DataLayersLogicOperator"), TEXT("data_layers_logic_operator"));

    Root->SetBoolField(TEXT("can_generate_streaming"), WP->CanGenerateStreaming());

    // Runtime hash + (spatial hash) editing grids.
    UWorldPartitionRuntimeHash* Hash = WP->RuntimeHash;
    if (Hash)
    {
        Root->SetStringField(TEXT("runtime_hash_class"), Hash->GetClass()->GetName());
        Root->SetStringField(TEXT("runtime_hash_class_path"), Hash->GetClass()->GetPathName());

        if (UWorldPartitionRuntimeSpatialHash* SpatialHash = Cast<UWorldPartitionRuntimeSpatialHash>(Hash))
        {
            Root->SetBoolField(TEXT("is_spatial_hash"), true);
            Root->SetNumberField(TEXT("num_generated_streaming_grids"), (double)SpatialHash->GetNumGrids());

            // Reflect the private WITH_EDITORONLY_DATA "Grids" array (offset-based; ignores C++ private).
            TArray<TSharedPtr<FJsonValue>> GridsJson;
            if (FArrayProperty* GridsProp = FindFProperty<FArrayProperty>(SpatialHash->GetClass(), TEXT("Grids")))
            {
                FScriptArrayHelper Helper(GridsProp, GridsProp->ContainerPtrToValuePtr<void>(SpatialHash));
                FStructProperty* ElemStruct = CastField<FStructProperty>(GridsProp->Inner);
                UScriptStruct* GridStruct = ElemStruct ? ElemStruct->Struct : nullptr;
                if (GridStruct)
                {
                    FProperty* PName  = FindFProperty<FProperty>(GridStruct, TEXT("GridName"));
                    FProperty* PCell  = FindFProperty<FProperty>(GridStruct, TEXT("CellSize"));
                    FProperty* PRange = FindFProperty<FProperty>(GridStruct, TEXT("LoadingRange"));
                    FProperty* PPrio  = FindFProperty<FProperty>(GridStruct, TEXT("Priority"));
                    FProperty* PBlock = FindFProperty<FProperty>(GridStruct, TEXT("bBlockOnSlowStreaming"));
                    for (int32 i = 0; i < Helper.Num(); ++i)
                    {
                        const void* Elem = Helper.GetRawPtr(i);
                        TSharedRef<FJsonObject> G = MakeShared<FJsonObject>();
                        if (FNameProperty* NP = CastField<FNameProperty>(PName))
                        {
                            G->SetStringField(TEXT("grid_name"), NP->GetPropertyValue_InContainer(Elem).ToString());
                        }
                        if (FIntProperty* IP = CastField<FIntProperty>(PCell))
                        {
                            G->SetNumberField(TEXT("cell_size"), (double)IP->GetPropertyValue_InContainer(Elem));
                        }
                        if (FFloatProperty* FP = CastField<FFloatProperty>(PRange))
                        {
                            G->SetNumberField(TEXT("loading_range"), (double)FP->GetPropertyValue_InContainer(Elem));
                        }
                        if (FIntProperty* IP = CastField<FIntProperty>(PPrio))
                        {
                            G->SetNumberField(TEXT("priority"), (double)IP->GetPropertyValue_InContainer(Elem));
                        }
                        if (FBoolProperty* BP = CastField<FBoolProperty>(PBlock))
                        {
                            G->SetBoolField(TEXT("block_on_slow_streaming"), BP->GetPropertyValue_InContainer(Elem));
                        }
                        GridsJson.Add(MakeShared<FJsonValueObject>(G));
                    }
                }
            }
            Root->SetNumberField(TEXT("editing_grid_count"), GridsJson.Num());
            Root->SetArrayField(TEXT("editing_grids"), GridsJson);
        }
        else
        {
            Root->SetBoolField(TEXT("is_spatial_hash"), false);
            Root->SetStringField(TEXT("grids_note"),
                TEXT("runtime hash is not a UWorldPartitionRuntimeSpatialHash; editing-grids reflection is spatial-hash only"));
        }
    }

    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 4) WRITE: set editable UWorldPartition properties. SettingsJson is a flat JSON object {prop: value}.
//    "bEnableStreaming"/"enable_streaming" routes through the side-effecting SetEnableStreaming(); every
//    other key is resolved as a UPROPERTY on the WP class and set via reflection + PreEditChange/
//    PostEditChangeProperty. Captures a `prev` object (prop -> prior value) for a faithful undo.
//    Guard: null WP -> clean "world is not partitioned".
// ================================================================================================
FString UMCPReflectionLibrary::SetWorldPartitionSettingsJson(const FString& SettingsJson)
{
#if WITH_EDITOR
    FString Err;
    UWorldPartition* WP = MCPWx_ResolveWorldPartition(Err);
    if (!WP)
    {
        return MCPWx_Error(Err);
    }

    TSharedPtr<FJsonObject> In;
    FString ParseErr;
    if (!MCPWx_ParseObject(SettingsJson, In, ParseErr))
    {
        return MCPWx_Error(FString::Printf(TEXT("%s (pass a JSON object, e.g. {\"bEnableStreaming\":true})"), *ParseErr));
    }

    TSharedRef<FJsonObject> Prev = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> Applied;
    TArray<TSharedPtr<FJsonValue>> Errors;

    WP->Modify();

    for (const TPair<FString, TSharedPtr<FJsonValue>>& KV : In->Values)
    {
        const FString& Key = KV.Key;
        const TSharedPtr<FJsonValue>& Val = KV.Value;

        // Special-case the streaming toggle -> the side-effecting setter (reflection would skip OnEnableStreamingChanged).
        if (Key.Equals(TEXT("bEnableStreaming"), ESearchCase::IgnoreCase)
            || Key.Equals(TEXT("enable_streaming"), ESearchCase::IgnoreCase))
        {
            const bool bPrev = WP->bEnableStreaming;
            Prev->SetBoolField(TEXT("bEnableStreaming"), bPrev);
            WP->SetEnableStreaming(Val.IsValid() ? Val->AsBool() : false);
            Applied.Add(MakeShared<FJsonValueString>(TEXT("bEnableStreaming")));
            continue;
        }

        FProperty* Prop = FindFProperty<FProperty>(WP->GetClass(), *Key);
        if (!Prop)
        {
            Errors.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("no property '%s' on UWorldPartition"), *Key)));
            continue;
        }
        void* VP = Prop->ContainerPtrToValuePtr<void>(WP);
        Prev->SetStringField(Key, MCPWx_ExportProp(Prop, VP, WP)); // prior value (ExportText) for undo

        WP->PreEditChange(Prop);
        FString ApplyErr;
        if (!MCPWx_ApplyJsonToProperty(Prop, VP, WP, Val, ApplyErr))
        {
            Errors.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("set '%s' failed: %s"), *Key, *ApplyErr)));
            continue;
        }
        FPropertyChangedEvent Evt(Prop);
        WP->PostEditChangeProperty(Evt);
        Applied.Add(MakeShared<FJsonValueString>(Key));
    }

    WP->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("world_partition_path"), WP->GetPathName());
    Root->SetObjectField(TEXT("prev"), Prev);          // prop -> prior value; inverse re-calls with this object
    Root->SetArrayField(TEXT("applied"), Applied);
    Root->SetArrayField(TEXT("errors"), Errors);
    Root->SetNumberField(TEXT("applied_count"), Applied.Num());
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 5) WRITE: edit one spatial-hash EDITING grid (UWorldPartitionRuntimeSpatialHash::Grids — a private,
//    WITH_EDITORONLY_DATA TArray<FSpatialHashRuntimeGrid>). GridJson = {grid_name (required), cell_size?,
//    loading_range?, priority?, block_on_slow_streaming?, origin?, debug_color?}. Locates the grid by name,
//    captures the changed fields' prior values (+ the whole-array ExportText), sets the provided fields via
//    reflection, then RuntimeHash->PostEditChangeProperty. Captures the prior grids for a faithful undo.
// ================================================================================================
FString UMCPReflectionLibrary::SetRuntimeGridJson(const FString& GridJson)
{
#if WITH_EDITOR
    FString Err;
    UWorldPartition* WP = MCPWx_ResolveWorldPartition(Err);
    if (!WP)
    {
        return MCPWx_Error(Err);
    }
    UWorldPartitionRuntimeSpatialHash* SpatialHash = Cast<UWorldPartitionRuntimeSpatialHash>(WP->RuntimeHash);
    if (!SpatialHash)
    {
        return MCPWx_Error(FString::Printf(
            TEXT("runtime hash is not a spatial hash (class '%s'); grid editing is spatial-hash only"),
            WP->RuntimeHash ? *WP->RuntimeHash->GetClass()->GetName() : TEXT("null")));
    }

    TSharedPtr<FJsonObject> In;
    FString ParseErr;
    if (!MCPWx_ParseObject(GridJson, In, ParseErr))
    {
        return MCPWx_Error(FString::Printf(TEXT("%s (pass a JSON object with grid_name + fields)"), *ParseErr));
    }
    FString GridName;
    In->TryGetStringField(TEXT("grid_name"), GridName);
    if (GridName.IsEmpty())
    {
        return MCPWx_Error(TEXT("missing 'grid_name' (call get_world_partition_settings for editing_grids)"));
    }

    FArrayProperty* GridsProp = FindFProperty<FArrayProperty>(SpatialHash->GetClass(), TEXT("Grids"));
    FStructProperty* ElemStruct = GridsProp ? CastField<FStructProperty>(GridsProp->Inner) : nullptr;
    UScriptStruct* GridStruct = ElemStruct ? ElemStruct->Struct : nullptr;
    if (!GridsProp || !GridStruct)
    {
        return MCPWx_Error(TEXT("could not reflect UWorldPartitionRuntimeSpatialHash::Grids (engine version mismatch?)"));
    }

    FScriptArrayHelper Helper(GridsProp, GridsProp->ContainerPtrToValuePtr<void>(SpatialHash));
    FNameProperty* PName = CastField<FNameProperty>(FindFProperty<FProperty>(GridStruct, TEXT("GridName")));

    // Locate the target grid by name; collect available names for a helpful miss message.
    int32 FoundIdx = INDEX_NONE;
    TArray<TSharedPtr<FJsonValue>> AvailableNames;
    for (int32 i = 0; i < Helper.Num(); ++i)
    {
        const void* Elem = Helper.GetRawPtr(i);
        const FName ThisName = PName ? PName->GetPropertyValue_InContainer(Elem) : NAME_None;
        AvailableNames.Add(MakeShared<FJsonValueString>(ThisName.ToString()));
        if (ThisName.ToString().Equals(GridName, ESearchCase::IgnoreCase))
        {
            FoundIdx = i;
        }
    }
    if (FoundIdx == INDEX_NONE)
    {
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("error"), FString::Printf(TEXT("no editing grid named '%s'"), *GridName));
        R->SetArrayField(TEXT("available_grids"), AvailableNames);
        return MCPWx_SerializeJson(R);
    }

    // Capture the whole prior array (belt-and-suspenders undo) BEFORE mutating.
    FString PrevGridsText;
    GridsProp->ExportTextItem_Direct(PrevGridsText, GridsProp->ContainerPtrToValuePtr<void>(SpatialHash),
                                     nullptr, SpatialHash, PPF_None);

    void* Elem = Helper.GetRawPtr(FoundIdx);

    // JSON key -> struct member name mapping for the editable fields.
    static const TArray<TPair<FString, FString>> FieldMap = {
        { TEXT("cell_size"),               TEXT("CellSize") },
        { TEXT("loading_range"),           TEXT("LoadingRange") },
        { TEXT("priority"),                TEXT("Priority") },
        { TEXT("block_on_slow_streaming"), TEXT("bBlockOnSlowStreaming") },
        { TEXT("origin"),                  TEXT("Origin") },
        { TEXT("debug_color"),             TEXT("DebugColor") },
        { TEXT("client_only_visible"),     TEXT("bClientOnlyVisible") },
    };

    SpatialHash->Modify();
    SpatialHash->PreEditChange(GridsProp);

    TSharedRef<FJsonObject> PrevFields = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> Changed;
    TArray<TSharedPtr<FJsonValue>> Errors;
    for (const TPair<FString, FString>& F : FieldMap)
    {
        if (!In->HasField(F.Key))
        {
            continue;
        }
        FProperty* MemberProp = FindFProperty<FProperty>(GridStruct, *F.Value);
        if (!MemberProp)
        {
            Errors.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("no grid member '%s'"), *F.Value)));
            continue;
        }
        void* MemberPtr = MemberProp->ContainerPtrToValuePtr<void>(Elem);
        PrevFields->SetStringField(F.Key, MCPWx_ExportProp(MemberProp, MemberPtr, SpatialHash)); // prior value
        FString ApplyErr;
        if (!MCPWx_ApplyJsonToProperty(MemberProp, MemberPtr, SpatialHash, In->TryGetField(F.Key), ApplyErr))
        {
            Errors.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("set '%s' failed: %s"), *F.Key, *ApplyErr)));
            continue;
        }
        Changed.Add(MakeShared<FJsonValueString>(F.Key));
    }

    FPropertyChangedEvent Evt(GridsProp);
    SpatialHash->PostEditChangeProperty(Evt);
    SpatialHash->MarkPackageDirty();
    WP->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("world_partition_path"), WP->GetPathName());
    Root->SetStringField(TEXT("grid_name"), GridName);
    Root->SetArrayField(TEXT("changed"), Changed);
    Root->SetArrayField(TEXT("errors"), Errors);
    Root->SetObjectField(TEXT("prev"), PrevFields);        // per-field prior values; inverse re-calls set_runtime_grid
    Root->SetStringField(TEXT("prev_grids_text"), PrevGridsText); // whole-array ExportText fallback
    Root->SetNumberField(TEXT("grid_count"), Helper.Num());
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 6) ACTION: World Partition build. HONEST report — a real WP build (streaming / HLOD / minimap / navmesh)
//    is performed by the WorldPartitionBuilderCommandlet as a SEPARATE -run= process; there is no safe,
//    persisting in-editor build entry point (UWorldPartition::GenerateStreaming is an in-memory cook/PIE
//    step that neither persists nor is safe to fire under a live editor). This handler therefore does NOT
//    perform (or fake) an in-editor build; it reports readiness + the exact commandlet command line.
//    Guard: null WP -> "world is not partitioned".
// ================================================================================================
FString UMCPReflectionLibrary::BuildWorldPartitionJson(const FString& Builder)
{
#if WITH_EDITOR
    FString Err;
    UWorldPartition* WP = MCPWx_ResolveWorldPartition(Err);
    if (!WP)
    {
        return MCPWx_Error(Err);
    }
    UWorld* World = MCPWx_EditorWorld();

    // Map the friendly builder name to the engine's commandlet -Builder class.
    FString Req = Builder;
    Req.TrimStartAndEndInline();
    FString BuilderClass;
    const FString ReqLC = Req.ToLower();
    if (ReqLC.IsEmpty() || ReqLC == TEXT("streaming") || ReqLC == TEXT("resave"))
    {
        BuilderClass = TEXT("WorldPartitionResaveActorsBuilder");
    }
    else if (ReqLC == TEXT("hlod") || ReqLC == TEXT("hlods"))
    {
        BuilderClass = TEXT("WorldPartitionHLODsBuilder");
    }
    else if (ReqLC == TEXT("minimap"))
    {
        BuilderClass = TEXT("WorldPartitionMiniMapBuilder");
    }
    else if (ReqLC == TEXT("navmesh") || ReqLC == TEXT("navigation"))
    {
        BuilderClass = TEXT("WorldPartitionNavigationDataBuilder");
    }
    else if (ReqLC == TEXT("landscape") || ReqLC == TEXT("landscapespline"))
    {
        BuilderClass = TEXT("WorldPartitionLandscapeSplineMeshesBuilder");
    }
    else
    {
        BuilderClass = Req; // pass through an explicit commandlet-builder class name unchanged
    }

    const FString ProjectFile = FPaths::GetProjectFilePath();
    const FString MapPackage = (World && World->GetOutermost()) ? World->GetOutermost()->GetName() : TEXT("<MapPackage>");
    const FString CommandLine = FString::Printf(
        TEXT("UnrealEditor-Cmd \"%s\" \"%s\" -run=WorldPartitionBuilderCommandlet -Builder=%s -AllowCommandletRendering"),
        *ProjectFile, *MapPackage, *BuilderClass);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("world_partition_path"), WP->GetPathName());
    Root->SetStringField(TEXT("requested_builder"), Req.IsEmpty() ? TEXT("streaming") : Req);
    Root->SetStringField(TEXT("builder_class"), BuilderClass);
    Root->SetBoolField(TEXT("in_editor_build_performed"), false);  // HONEST: we do not run a build in-process
    Root->SetBoolField(TEXT("can_generate_streaming"), WP->CanGenerateStreaming());
    Root->SetBoolField(TEXT("is_streaming_enabled"), WP->IsStreamingEnabled());
    if (UWorldPartitionRuntimeSpatialHash* SH = Cast<UWorldPartitionRuntimeSpatialHash>(WP->RuntimeHash))
    {
        Root->SetNumberField(TEXT("num_generated_streaming_grids"), (double)SH->GetNumGrids());
    }
    Root->SetStringField(TEXT("supported_via"), TEXT("WorldPartitionBuilderCommandlet (separate -run= process)"));
    Root->SetStringField(TEXT("command_line"), CommandLine);
    Root->SetStringField(TEXT("note"),
        TEXT("A World Partition build is not performed in-editor: the only correct + persisting build path is the "
             "WorldPartitionBuilderCommandlet run as a separate process (command_line above). "
             "UWorldPartition::GenerateStreaming exists but is an in-memory cook/PIE step that does not persist and "
             "is unsafe to fire under a live editor, so it is deliberately not called (no fake success)."));
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 7) READ: navigation build status. UNavigationSystemV1::IsNavigationBuildInProgress() /
//    GetNumRemainingBuildTasks() / GetNumRunningBuildTasks() are NOT UFUNCTIONs -> C++-only. Resolves the
//    nav system from the editor world.
// ================================================================================================
FString UMCPReflectionLibrary::NavigationBuildStatusJson()
{
#if WITH_EDITOR
    UWorld* World = MCPWx_EditorWorld();
    if (!World)
    {
        return MCPWx_Error(TEXT("no editor world (open a map in the level editor first)"));
    }

    UNavigationSystemV1* Nav = UNavigationSystemV1::GetNavigationSystem(World);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("world"), World->GetName());
    if (!Nav)
    {
        Root->SetBoolField(TEXT("has_nav_system"), false);
        Root->SetBoolField(TEXT("building"), false);
        Root->SetNumberField(TEXT("remaining_tasks"), 0);
        Root->SetNumberField(TEXT("running_tasks"), 0);
        Root->SetStringField(TEXT("note"),
            TEXT("no navigation system in the editor world (add a NavMeshBoundsVolume / enable navigation to create one)"));
        return MCPWx_SerializeJson(Root);
    }

    Root->SetBoolField(TEXT("has_nav_system"), true);
    Root->SetBoolField(TEXT("building"), Nav->IsNavigationBuildInProgress());
    Root->SetNumberField(TEXT("remaining_tasks"), Nav->GetNumRemainingBuildTasks());
    Root->SetNumberField(TEXT("running_tasks"), Nav->GetNumRunningBuildTasks());
    Root->SetNumberField(TEXT("supported_agents"), Nav->GetSupportedAgents().Num());
    return MCPWx_SerializeJson(Root);
#else
    return MCPWx_Error(TEXT("editor-only"));
#endif
}
