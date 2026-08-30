// ============================================================================
// MCPReflection_GameDevGaps.cpp — C++ #53. First batch of the MCP_TOOLING_GAPS.md
// backlog (observed during Time-Flies game dev). SELF-CONTAINED translation unit:
// each method is a DEFERRED UMCPReflectionLibrary static that the matching Python
// UserTools hasattr-guard on; when this links, those tools auto-enable.
//
//   #7  SetGameModeOverride(class_path)          -> WorldSettings.DefaultGameMode (TSubclassOf coercion)
//   #2  SetPieSettings(clients, net_mode, one_proc, viewport) -> mutate ULevelEditorPlaySettings in-memory
//   #10 ListPieWorldsJson()                       -> enumerate GEngine world contexts (server + N clients)
//   #5  SetMaterialUsage(mat, flags, save)        -> set bUsedWith* usage bits + C++ SavePackage (persists)
//   #9  GetReflectedPropertyJson(obj, name)       -> read ANY reflected UPROPERTY incl. protected (raw FProperty)
//   #9  CallFunctionJson(obj, fn, args_json)      -> ProcessEvent a UFUNCTION by name with JSON scalar args
//
// LINK-RISK DISCIPLINE: every symbol here is Engine/UnrealEd/CoreUObject — modules
// already linked. NO Build.cs change, NO engine export patch. Editor-only work is
// under WITH_EDITOR. Uncertain 5.8 signatures are tagged "VERIFY".
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/UObjectIterator.h"

#include "Editor.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "EngineUtils.h"                 // TActorIterator
#include "GameFramework/WorldSettings.h"
#include "GameFramework/GameModeBase.h"
#include "Materials/Material.h"
#include "Materials/MaterialInterface.h"
#include "UObject/SavePackage.h"
#include "Misc/PackageName.h"
#include "GameFramework/PlayerController.h"   // #3 PIE input: APlayerController::InputKey
#include "GameFramework/PlayerInput.h"        // #3 FInputKeyParams
#include "Kismet/GameplayStatics.h"           // #3 GetPlayerController
#include "InputCoreTypes.h"                   // #3 FKey / EKeys
#include "Animation/AnimBlueprint.h"          // #1 UAnimBlueprint / FAnimParentNodeAssetOverride / ParentAssetOverrides
#include "Animation/AnimBlueprintGeneratedClass.h"
#include "Animation/AnimationAsset.h"
#include "Kismet2/KismetEditorUtilities.h"    // #1 FKismetEditorUtilities::CreateBlueprint
#include "EdGraph/EdGraph.h"                  // #1 walk parent anim-graph nodes
#include "EdGraph/EdGraphNode.h"
#include "UObject/Package.h"                  // #1 CreatePackage

#if WITH_EDITOR
#include "Settings/LevelEditorPlaySettings.h"   // ULevelEditorPlaySettings (UnrealEd)
#endif

namespace
{
    FString GDG_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString GDG_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return GDG_Serialize(O);
    }

    // Editor world (level being edited).
    UWorld* GDG_EditorWorld()
    {
#if WITH_EDITOR
        if (GEditor)
        {
            return GEditor->GetEditorWorldContext().World();
        }
#endif
        return nullptr;
    }

    const TCHAR* GDG_WorldTypeStr(EWorldType::Type T)
    {
        switch (T)
        {
        case EWorldType::Editor:        return TEXT("Editor");
        case EWorldType::PIE:           return TEXT("PIE");
        case EWorldType::Game:          return TEXT("Game");
        case EWorldType::EditorPreview: return TEXT("EditorPreview");
        case EWorldType::GamePreview:   return TEXT("GamePreview");
        case EWorldType::Inactive:      return TEXT("Inactive");
        default:                        return TEXT("None");
        }
    }

    const TCHAR* GDG_NetModeStr(ENetMode M)
    {
        switch (M)
        {
        case NM_Standalone:      return TEXT("standalone");
        case NM_DedicatedServer: return TEXT("dedicated_server");
        case NM_ListenServer:    return TEXT("listen_server");
        case NM_Client:          return TEXT("client");
        default:                 return TEXT("unknown");
        }
    }
}

// ---- #7  set_game_mode_override -------------------------------------------------
FString UMCPReflectionLibrary::SetGameModeOverride(const FString& GameModeClassPath)
{
#if WITH_EDITOR
    UWorld* World = GDG_EditorWorld();
    if (!World) return GDG_Error(TEXT("no editor world"));
    AWorldSettings* WS = World->GetWorldSettings();
    if (!WS) return GDG_Error(TEXT("no world settings actor"));

    UClass* NewClass = nullptr;
    if (!GameModeClassPath.IsEmpty())
    {
        // Accept a native class path (/Script/Module.MyGM) or a BP generated-class path
        // (/Game/BP_MyGM.BP_MyGM_C). LoadClass handles the _C form; fall back to LoadObject<UClass>.
        NewClass = LoadClass<AGameModeBase>(nullptr, *GameModeClassPath);
        if (!NewClass)
        {
            NewClass = LoadObject<UClass>(nullptr, *GameModeClassPath);
        }
        if (!NewClass)
        {
            return GDG_Error(FString::Printf(TEXT("could not load game mode class: %s"), *GameModeClassPath));
        }
        if (!NewClass->IsChildOf(AGameModeBase::StaticClass()))
        {
            return GDG_Error(FString::Printf(TEXT("class %s is not a AGameModeBase subclass"), *NewClass->GetName()));
        }
    }

    UClass* Prior = WS->DefaultGameMode;
    WS->Modify();
    WS->DefaultGameMode = NewClass; // TSubclassOf<AGameModeBase> = UClass* (the coercion the write path lacked)
    WS->MarkPackageDirty();
    WS->PostEditChange();

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("world"), World->GetName());
    O->SetStringField(TEXT("new_game_mode"), NewClass ? NewClass->GetPathName() : TEXT(""));
    O->SetStringField(TEXT("prior_game_mode"), Prior ? Prior->GetPathName() : TEXT(""));
    O->SetStringField(TEXT("note"), TEXT("set on the level's WorldSettings; save the level to persist"));
    return GDG_Serialize(O);
#else
    return GDG_Error(TEXT("editor-only"));
#endif
}

// ---- #2  set_pie_settings -------------------------------------------------------
FString UMCPReflectionLibrary::SetPieSettings(int32 NumClients, const FString& NetMode,
                                              bool bRunUnderOneProcess, bool bUseSplitscreen)
{
#if WITH_EDITOR
    ULevelEditorPlaySettings* PS = GetMutableDefault<ULevelEditorPlaySettings>();
    if (!PS) return GDG_Error(TEXT("no ULevelEditorPlaySettings CDO"));

    UClass* Cls = PS->GetClass();
    TSharedRef<FJsonObject> Applied = MakeShared<FJsonObject>();

    // Set by reflection (these members are private with accessors in some 5.x versions; reflection is
    // version-robust). Property names verified against LevelEditorPlaySettings.h.
    if (NumClients > 0)
    {
        if (FIntProperty* P = FindFProperty<FIntProperty>(Cls, TEXT("PlayNumberOfClients")))
        {
            P->SetPropertyValue_InContainer(PS, NumClients);
            Applied->SetNumberField(TEXT("PlayNumberOfClients"), NumClients);
        }
    }
    if (FBoolProperty* P = FindFProperty<FBoolProperty>(Cls, TEXT("RunUnderOneProcess")))
    {
        P->SetPropertyValue_InContainer(PS, bRunUnderOneProcess);
        Applied->SetBoolField(TEXT("RunUnderOneProcess"), bRunUnderOneProcess);
    }
    if (FBoolProperty* P = FindFProperty<FBoolProperty>(Cls, TEXT("bLaunchSeparateServer")))
    {
        // leave as-is unless one-process listen-server; documented via Applied for transparency
        Applied->SetBoolField(TEXT("bLaunchSeparateServer"), P->GetPropertyValue_InContainer(PS));
    }
    // Split-screen: gameplay setting on the play settings (may be named UseSplitscreen / bUseSplitscreen).
    for (const TCHAR* SplitName : { TEXT("UseSplitscreen"), TEXT("bUseSplitscreen") })
    {
        if (FBoolProperty* P = FindFProperty<FBoolProperty>(Cls, SplitName))
        {
            P->SetPropertyValue_InContainer(PS, bUseSplitscreen);
            Applied->SetBoolField(SplitName, bUseSplitscreen);
            break;
        }
    }

    // Net mode is an enum; set by importing the enum value string. Map friendly names.
    if (!NetMode.IsEmpty())
    {
        FString NM = NetMode.ToLower();
        FString EnumVal;
        if (NM == TEXT("standalone") || NM == TEXT("offline"))         EnumVal = TEXT("PIE_Standalone");
        else if (NM == TEXT("listen") || NM == TEXT("listen_server"))  EnumVal = TEXT("PIE_ListenServer");
        else if (NM == TEXT("client"))                                 EnumVal = TEXT("PIE_Client");
        else                                                           EnumVal = NetMode; // pass-through raw enum name

        if (FProperty* P = FindFProperty<FProperty>(Cls, TEXT("PlayNetMode")))
        {
            void* ValuePtr = P->ContainerPtrToValuePtr<void>(PS);
            const TCHAR* Imported = P->ImportText_Direct(*EnumVal, ValuePtr, PS, PPF_None);
            if (Imported)
            {
                Applied->SetStringField(TEXT("PlayNetMode"), EnumVal);
            }
            else
            {
                Applied->SetStringField(TEXT("PlayNetMode_error"), FString::Printf(TEXT("could not set net mode '%s'"), *EnumVal));
            }
        }
    }

    PS->PostEditChange();

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetObjectField(TEXT("applied"), Applied);
    O->SetStringField(TEXT("note"), TEXT("mutates the in-memory LevelEditorPlaySettings; the next play_in_editor uses them (no restart)"));
    return GDG_Serialize(O);
#else
    return GDG_Error(TEXT("editor-only"));
#endif
}

// ---- #10  list_pie_worlds -------------------------------------------------------
FString UMCPReflectionLibrary::ListPieWorldsJson()
{
    TArray<TSharedPtr<FJsonValue>> Worlds;
    if (GEngine)
    {
        for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
        {
            UWorld* W = Ctx.World();
            if (!W) continue;

            int32 ActorCount = 0;
            for (TActorIterator<AActor> It(W); It; ++It) { ++ActorCount; }

            TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
            O->SetStringField(TEXT("world"), W->GetName());
            O->SetStringField(TEXT("world_type"), GDG_WorldTypeStr(Ctx.WorldType));
            O->SetStringField(TEXT("net_role"), GDG_NetModeStr(W->GetNetMode()));
            O->SetNumberField(TEXT("pie_instance"), Ctx.PIEInstance);
            O->SetNumberField(TEXT("actor_count"), ActorCount);
            O->SetBoolField(TEXT("is_pie"), Ctx.WorldType == EWorldType::PIE);
            Worlds.Add(MakeShared<FJsonValueObject>(O));
        }
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("ok"));
    Root->SetNumberField(TEXT("count"), Worlds.Num());
    Root->SetArrayField(TEXT("worlds"), Worlds);
    return GDG_Serialize(Root);
}

// ---- #5  set_material_usage -----------------------------------------------------
FString UMCPReflectionLibrary::SetMaterialUsage(UMaterialInterface* MaterialIface, const FString& UsageFlagsCsv, bool bSave)
{
#if WITH_EDITOR
    UMaterial* Mat = MaterialIface ? MaterialIface->GetMaterial() : nullptr;
    if (!Mat) return GDG_Error(TEXT("null material (pass a base UMaterial or an instance whose base resolves)"));

    TArray<FString> Names;
    UsageFlagsCsv.ParseIntoArray(Names, TEXT(","), true);
    TArray<TSharedPtr<FJsonValue>> AppliedArr;
    bool bAnyNeedsRecompile = false;

    for (FString N : Names)
    {
        N = N.TrimStartAndEnd().ToLower().Replace(TEXT(" "), TEXT("")).Replace(TEXT("_"), TEXT(""));
        EMaterialUsage Usage;
        if      (N == TEXT("skeletalmesh"))            Usage = MATUSAGE_SkeletalMesh;
        else if (N == TEXT("morphtargets"))            Usage = MATUSAGE_MorphTargets;
        else if (N == TEXT("particlesprites"))         Usage = MATUSAGE_ParticleSprites;
        else if (N == TEXT("beamtrails"))              Usage = MATUSAGE_BeamTrails;
        else if (N == TEXT("meshparticles"))           Usage = MATUSAGE_MeshParticles;
        else if (N == TEXT("staticlighting"))          Usage = MATUSAGE_StaticLighting;
        else if (N == TEXT("splinemesh"))              Usage = MATUSAGE_SplineMesh;
        else if (N == TEXT("instancedstaticmeshes"))   Usage = MATUSAGE_InstancedStaticMeshes;
        else if (N == TEXT("geometrycollections"))     Usage = MATUSAGE_GeometryCollections;
        else if (N == TEXT("clothing"))                Usage = MATUSAGE_Clothing;
        else if (N == TEXT("niagarasprites"))          Usage = MATUSAGE_NiagaraSprites;
        else if (N == TEXT("niagararibbons"))          Usage = MATUSAGE_NiagaraRibbons;
        else if (N == TEXT("niagarameshparticles"))    Usage = MATUSAGE_NiagaraMeshParticles;
        else if (N == TEXT("geometrycache"))           Usage = MATUSAGE_GeometryCache;
        else if (N == TEXT("watersurface"))            Usage = MATUSAGE_Water;
        else if (N == TEXT("virtualheightfieldmesh"))  Usage = MATUSAGE_VirtualHeightfieldMesh;
        else if (N == TEXT("nanite"))                  Usage = MATUSAGE_Nanite;
        else                                           continue; // unknown flag -> skip

        bool bNeedsRecompile = false;
        Mat->SetMaterialUsage(bNeedsRecompile, Usage); // ENGINE_API #if WITH_EDITOR: sets bUsedWith* + flags recompile
        bAnyNeedsRecompile |= bNeedsRecompile;
        AppliedArr.Add(MakeShared<FJsonValueString>(N));
    }

    Mat->MarkPackageDirty();
    Mat->PostEditChange();

    bool bSaved = false;
    FString SaveError;
    if (bSave)
    {
        UPackage* Pkg = Mat->GetPackage();
        if (Pkg)
        {
            const FString FileName = FPackageName::LongPackageNameToFilename(Pkg->GetName(), FPackageName::GetAssetPackageExtension());
            FSavePackageArgs Args;
            Args.TopLevelFlags = RF_Public | RF_Standalone;
            Args.SaveFlags = SAVE_NoError;
            Args.Error = GError;
            // Surface the REAL save result (Python save_loaded_asset returned a bare False — MCP_TOOLING_GAPS #5).
            FSavePackageResultStruct R = UPackage::Save(Pkg, Mat, *FileName, Args);
            bSaved = (R.Result == ESavePackageResult::Success);
            if (!bSaved) SaveError = FString::Printf(TEXT("SavePackage result code %d"), (int32)R.Result);
        }
        else
        {
            SaveError = TEXT("material has no package");
        }
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("material"), Mat->GetPathName());
    O->SetArrayField(TEXT("applied_flags"), AppliedArr);
    O->SetBoolField(TEXT("recompile_triggered"), bAnyNeedsRecompile);
    O->SetBoolField(TEXT("saved"), bSaved);
    if (!SaveError.IsEmpty()) O->SetStringField(TEXT("save_error"), SaveError);
    return GDG_Serialize(O);
#else
    return GDG_Error(TEXT("editor-only"));
#endif
}

// ---- #9  get_reflected_property (read protected/any reflected UPROPERTY) ---------
FString UMCPReflectionLibrary::GetReflectedPropertyJson(UObject* Target, const FString& PropertyName)
{
    if (!Target) return GDG_Error(TEXT("null target object"));
    FProperty* Prop = FindFProperty<FProperty>(Target->GetClass(), *PropertyName);
    if (!Prop)
    {
        return GDG_Error(FString::Printf(TEXT("no reflected property '%s' on %s (must be a UPROPERTY; truly-private C++ members are out of scope)"),
                                         *PropertyName, *Target->GetClass()->GetName()));
    }
    const void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Target);
    FString ValueStr;
    Prop->ExportTextItem_Direct(ValueStr, ValuePtr, nullptr, Target, PPF_None); // raw FProperty read: ignores Python edit-protection

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("object"), Target->GetPathName());
    O->SetStringField(TEXT("property"), PropertyName);
    O->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());
    O->SetStringField(TEXT("value"), ValueStr);
    return GDG_Serialize(O);
}

// ---- #9  call_function (ProcessEvent a UFUNCTION by name with JSON scalar args) --
FString UMCPReflectionLibrary::CallFunctionJson(UObject* Target, const FString& FunctionName, const FString& ArgsJson)
{
    if (!Target) return GDG_Error(TEXT("null target object"));
    UFunction* Fn = Target->FindFunction(*FunctionName);
    if (!Fn)
    {
        return GDG_Error(FString::Printf(TEXT("no UFUNCTION '%s' on %s (only reflected UFUNCTIONs are callable; plain C++ methods are out of scope)"),
                                         *FunctionName, *Target->GetClass()->GetName()));
    }

    // Parse args object (name -> JSON scalar). Empty/invalid => no args.
    TSharedPtr<FJsonObject> Args;
    if (!ArgsJson.IsEmpty())
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ArgsJson);
        FJsonSerializer::Deserialize(Reader, Args); // Args may stay null if not an object
    }

    // Allocate + initialize the params block.
    uint8* ParamBuffer = (uint8*)FMemory_Alloca(FMath::Max<int32>(Fn->ParmsSize, 1));
    FMemory::Memzero(ParamBuffer, Fn->ParmsSize);
    for (TFieldIterator<FProperty> It(Fn); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        It->InitializeValue_InContainer(ParamBuffer);
    }

    // Fill input params from JSON (scalars via ImportText).
    TArray<FString> Unset;
    for (TFieldIterator<FProperty> It(Fn); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        FProperty* P = *It;
        if (P->HasAnyPropertyFlags(CPF_ReturnParm)) continue;
        if (P->HasAnyPropertyFlags(CPF_OutParm) && !P->HasAnyPropertyFlags(CPF_ReferenceParm)) continue; // pure out
        const FString PName = P->GetName();
        if (Args.IsValid() && Args->HasField(PName))
        {
            FString AsStr;
            TSharedPtr<FJsonValue> V = Args->TryGetField(PName);
            if (V.IsValid() && V->Type == EJson::String) AsStr = V->AsString();
            else if (V.IsValid() && V->Type == EJson::Boolean) AsStr = V->AsBool() ? TEXT("true") : TEXT("false");
            else if (V.IsValid() && (V->Type == EJson::Number)) AsStr = FString::SanitizeFloat(V->AsNumber());
            else if (V.IsValid()) AsStr = V->AsString();
            void* Dest = P->ContainerPtrToValuePtr<void>(ParamBuffer);
            P->ImportText_Direct(*AsStr, Dest, Target, PPF_None);
        }
        else if (!P->HasAnyPropertyFlags(CPF_OutParm))
        {
            Unset.Add(PName);
        }
    }

    // Invoke.
    Target->ProcessEvent(Fn, ParamBuffer);

    // Collect return + out params.
    TSharedRef<FJsonObject> Outs = MakeShared<FJsonObject>();
    for (TFieldIterator<FProperty> It(Fn); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        FProperty* P = *It;
        if (P->HasAnyPropertyFlags(CPF_ReturnParm) || P->HasAnyPropertyFlags(CPF_OutParm))
        {
            const void* Src = P->ContainerPtrToValuePtr<void>(ParamBuffer);
            FString ValStr;
            P->ExportTextItem_Direct(ValStr, Src, nullptr, Target, PPF_None);
            Outs->SetStringField(P->HasAnyPropertyFlags(CPF_ReturnParm) ? TEXT("return") : P->GetName(), ValStr);
        }
    }

    // Destroy the params block.
    for (TFieldIterator<FProperty> It(Fn); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        It->DestroyValue_InContainer(ParamBuffer);
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("object"), Target->GetPathName());
    O->SetStringField(TEXT("function"), FunctionName);
    O->SetObjectField(TEXT("outputs"), Outs);
    if (Unset.Num() > 0)
    {
        TArray<TSharedPtr<FJsonValue>> UnsetArr;
        for (const FString& U : Unset) UnsetArr.Add(MakeShared<FJsonValueString>(U));
        O->SetArrayField(TEXT("unset_params_defaulted"), UnsetArr);
    }
    return GDG_Serialize(O);
}

namespace
{
    UWorld* GDG_PieWorld()
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
}

// ---- #3  inject_pie_key (feed a key/axis event into a PIE player's input) --------
FString UMCPReflectionLibrary::InjectPieKey(int32 PlayerIndex, const FString& KeyName, const FString& EventName,
                                            float Delta, bool bIsGamepad)
{
#if WITH_EDITOR
    UWorld* GameWorld = GDG_PieWorld();
    if (!GameWorld) return GDG_Error(TEXT("no PIE world — start Play-In-Editor first"));
    APlayerController* PC = UGameplayStatics::GetPlayerController(GameWorld, PlayerIndex);
    if (!PC) return GDG_Error(FString::Printf(TEXT("no player controller at index %d"), PlayerIndex));

    const FName KeyFName(*KeyName);
    FKey Key(KeyFName);   // two statements: FKey Key(FName(*KeyName)) would be a most-vexing-parse fn decl
    if (!Key.IsValid())
    {
        return GDG_Error(FString::Printf(TEXT("invalid key '%s' (use FKey names, e.g. W, SpaceBar, LeftMouseButton, Gamepad_FaceButton_Bottom)"), *KeyName));
    }

    EInputEvent Evt = IE_Pressed;
    const FString E = EventName.ToLower();
    if (E == TEXT("released"))          Evt = IE_Released;
    else if (E == TEXT("repeat"))       Evt = IE_Repeat;
    else if (E == TEXT("doubleclick"))  Evt = IE_DoubleClick;
    // "pressed" (default) otherwise.

    bool bHandled = false;
    PRAGMA_DISABLE_DEPRECATION_WARNINGS
    // FInputKeyParams is deprecated 5.6 (superseded by FInputKeyEventArgs) but present + functional in 5.8;
    // guarded so the deprecation warning doesn't add noise. Feeds the player's UPlayerInput, which both the
    // legacy input stack AND Enhanced Input read from — so this drives IA_* actions without needing the assets.
    FInputKeyParams Params(Key, Evt, (double)Delta, bIsGamepad);
    bHandled = PC->InputKey(Params);
    PRAGMA_ENABLE_DEPRECATION_WARNINGS

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetNumberField(TEXT("player_index"), PlayerIndex);
    O->SetStringField(TEXT("key"), Key.ToString());
    O->SetStringField(TEXT("event"), E.IsEmpty() ? TEXT("pressed") : E);
    O->SetNumberField(TEXT("delta"), Delta);
    O->SetBoolField(TEXT("handled"), bHandled);
    return GDG_Serialize(O);
#else
    return GDG_Error(TEXT("editor-only"));
#endif
}

// ---- #1  create_child_anim_blueprint_override -----------------------------------
// Create a CHILD AnimBlueprint parented to Parent's generated class, remapping animation assets by adding
// FAnimParentNodeAssetOverride entries (the standard "reuse ABP_Unarmed's graph with Rifle anims" flow).
// The overrides are DATA-set + saved; they apply on COMPILE — we do NOT compile here (compiling AnimBPs
// from the MCP is crash-prone), so the child compiles when opened/loaded in-editor.
FString UMCPReflectionLibrary::CreateChildAnimBlueprintOverride(const FString& ParentAnimBpPath,
    const FString& PackagePath, const FString& AssetName, const FString& OverridesJson)
{
#if WITH_EDITOR
    UAnimBlueprint* Parent = LoadObject<UAnimBlueprint>(nullptr, *ParentAnimBpPath);
    if (!Parent) return GDG_Error(FString::Printf(TEXT("could not load parent AnimBlueprint: %s"), *ParentAnimBpPath));
    UClass* ParentClass = Parent->GeneratedClass;
    if (!ParentClass) return GDG_Error(TEXT("parent AnimBlueprint has no GeneratedClass — open+compile it in-editor first"));

    TSharedPtr<FJsonObject> Ov;
    {
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(OverridesJson);
        FJsonSerializer::Deserialize(R, Ov);
    }
    if (!Ov.IsValid() || Ov->Values.Num() == 0)
        return GDG_Error(TEXT("overrides must be a non-empty JSON object {oldAnimPathOrName: newAnimPath}"));

    FString CleanPkg = PackagePath; CleanPkg.RemoveFromEnd(TEXT("/"));
    const FString FullName = CleanPkg + TEXT("/") + AssetName;
    UPackage* Pkg = CreatePackage(*FullName);
    if (!Pkg) return GDG_Error(FString::Printf(TEXT("could not create package %s"), *FullName));

    UBlueprint* NewBP = FKismetEditorUtilities::CreateBlueprint(ParentClass, Pkg, FName(*AssetName), BPTYPE_Normal,
        UAnimBlueprint::StaticClass(), UAnimBlueprintGeneratedClass::StaticClass());
    UAnimBlueprint* Child = Cast<UAnimBlueprint>(NewBP);
    if (!Child) return GDG_Error(TEXT("CreateBlueprint did not return a UAnimBlueprint"));

    // carry the parent's target skeleton (reflection avoids TargetSkeleton deprecation churn).
    if (FObjectProperty* TS = FindFProperty<FObjectProperty>(UAnimBlueprint::StaticClass(), TEXT("TargetSkeleton")))
    {
        UObject* ParentSkel = TS->GetObjectPropertyValue_InContainer(Parent);
        TS->SetObjectPropertyValue_InContainer(Child, ParentSkel);
    }

    // Walk parent anim-graph nodes; for any node whose reflected anim asset matches an override key, add an
    // override remapping it to the new asset. Pure reflection -> no ANIMGRAPH_API symbol references.
    int32 nAdded = 0;
    TArray<TSharedPtr<FJsonValue>> Applied;
    TArray<UEdGraph*> Graphs;
    Parent->GetAllGraphs(Graphs);
    for (UEdGraph* G : Graphs)
    {
        if (!G) continue;
        for (UEdGraphNode* Node : G->Nodes)
        {
            if (!Node) continue;
            UAnimationAsset* FoundAsset = nullptr;
            for (TFieldIterator<FProperty> It(Node->GetClass()); It && !FoundAsset; ++It)
            {
                if (FObjectProperty* OP = CastField<FObjectProperty>(*It))
                {
                    if (UAnimationAsset* AA = Cast<UAnimationAsset>(OP->GetObjectPropertyValue_InContainer(Node)))
                        FoundAsset = AA;
                }
                else if (FStructProperty* SP = CastField<FStructProperty>(*It))
                {
                    const void* StructPtr = SP->ContainerPtrToValuePtr<void>(Node);
                    for (TFieldIterator<FProperty> SIt(SP->Struct); SIt && !FoundAsset; ++SIt)
                    {
                        if (FObjectProperty* SOP = CastField<FObjectProperty>(*SIt))
                        {
                            if (UAnimationAsset* AA = Cast<UAnimationAsset>(SOP->GetObjectPropertyValue_InContainer(StructPtr)))
                                FoundAsset = AA;
                        }
                    }
                }
            }
            if (!FoundAsset) continue;

            const FString OldPath = FoundAsset->GetPathName();
            const FString OldName = FoundAsset->GetName();
            // Match this node's anim by full path OR asset name against the override keys (public API,
            // sidesteps FJsonObject::Values being keyed on a template FStringType, not FString).
            FString NewPath;
            if (Ov->TryGetStringField(OldPath, NewPath) || Ov->TryGetStringField(OldName, NewPath))
            {
                UAnimationAsset* NewAsset = LoadObject<UAnimationAsset>(nullptr, *NewPath);
                if (NewAsset)
                {
                    Child->ParentAssetOverrides.Add(FAnimParentNodeAssetOverride(Node->NodeGuid, NewAsset));
                    TSharedRef<FJsonObject> A = MakeShared<FJsonObject>();
                    A->SetStringField(TEXT("node"), Node->GetName());
                    A->SetStringField(TEXT("old"), OldPath);
                    A->SetStringField(TEXT("new"), NewAsset->GetPathName());
                    Applied.Add(MakeShared<FJsonValueObject>(A));
                    ++nAdded;
                }
            }
        }
    }

    Child->MarkPackageDirty();
    bool bSaved = false;
    {
        UPackage* CP = Child->GetPackage();
        if (CP)
        {
            const FString FileName = FPackageName::LongPackageNameToFilename(CP->GetName(), FPackageName::GetAssetPackageExtension());
            FSavePackageArgs Args; Args.TopLevelFlags = RF_Public | RF_Standalone; Args.SaveFlags = SAVE_NoError; Args.Error = GError;
            bSaved = UPackage::Save(CP, Child, *FileName, Args).Result == ESavePackageResult::Success;
        }
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("child"), Child->GetPathName());
    O->SetStringField(TEXT("parent"), Parent->GetPathName());
    O->SetNumberField(TEXT("overrides_added"), nAdded);
    O->SetArrayField(TEXT("applied"), Applied);
    O->SetBoolField(TEXT("saved"), bSaved);
    O->SetStringField(TEXT("note"), TEXT("overrides are DATA-set; open + compile the child AnimBP in-editor to apply them (MCP does not compile AnimBPs)"));
    return GDG_Serialize(O);
#else
    return GDG_Error(TEXT("editor-only"));
#endif
}
