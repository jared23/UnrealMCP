// ============================================================================
// MCPReflection_ProjectAimonGaps2.cpp — C++ #58. Second Project_Aimon gap batch.
// SELF-CONTAINED translation unit. Three unrelated gaps grouped for one build:
//
//   #58 SpawnActorInPie(class, xyz, rot, label)          -> spawn into the RUNNING PIE world (MED gap)
//   #58 FixupRedirectorReferencers(pathOrDir)            -> resave referencers + delete redirectors (MED gap)
//   #58 SetInheritedComponentProperty(bp, comp, prop, v) -> set a default on an INHERITED native component (MED gap)
//
// LINK: adds "AssetTools" + "AssetRegistry" to Build.cs (FixupReferencers + redirector scan). Everything
// else is Engine/UnrealEd/CoreUObject (already linked). All condensed-JSON (single line) so the bridge's
// marker extraction parses it. Editor-only.
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GameFramework/Actor.h"
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/ObjectRedirector.h"

#if WITH_EDITOR
#include "Engine/Blueprint.h"
#include "Components/ActorComponent.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "AssetToolsModule.h"
#include "IAssetTools.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/ARFilter.h"
#include "Modules/ModuleManager.h"
#endif

namespace
{
    // Condensed (single-line) JSON — the bridge extracts the JSON from ONE line after the @@UMCP@@ marker,
    // so pretty/multi-line output would be truncated to just "{".
    FString PA2_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
            TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString PA2_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return PA2_Serialize(O);
    }

#if WITH_EDITOR
    UWorld* PA2_PieWorld()
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
#endif
}

// ---- #58  spawn_actor_in_pie ----------------------------------------------------
FString UMCPReflectionLibrary::SpawnActorInPie(const FString& ClassPath, float X, float Y, float Z,
                                               float Pitch, float Yaw, float Roll, const FString& Label)
{
#if WITH_EDITOR
    UWorld* Pie = PA2_PieWorld();
    if (!Pie) return PA2_Error(TEXT("no PIE world — start Play-In-Editor first (this spawns into the RUNNING play world, not the editor world)"));

    UClass* Cls = LoadClass<AActor>(nullptr, *ClassPath);
    if (!Cls) Cls = LoadObject<UClass>(nullptr, *ClassPath);
    if (!Cls) return PA2_Error(FString::Printf(TEXT("could not load class '%s' (native /Script/Mod.AActorSub or BP /Game/BP.BP_C)"), *ClassPath));
    if (!Cls->IsChildOf(AActor::StaticClass())) return PA2_Error(FString::Printf(TEXT("class %s is not an AActor subclass"), *Cls->GetName()));

    FActorSpawnParameters Params;
    Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
    const FTransform Xform(FRotator(Pitch, Yaw, Roll), FVector(X, Y, Z));
    AActor* Actor = Pie->SpawnActor(Cls, &Xform, Params);
    if (!Actor) return PA2_Error(TEXT("SpawnActor returned null (class may be abstract, or collision rejected the spawn)"));
    if (!Label.IsEmpty()) Actor->SetActorLabel(Label);

    const FVector L = Actor->GetActorLocation();
    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("actor"), Actor->GetName());
    O->SetStringField(TEXT("class"), Cls->GetPathName());
    O->SetStringField(TEXT("world"), Pie->GetName());
    O->SetNumberField(TEXT("x"), L.X); O->SetNumberField(TEXT("y"), L.Y); O->SetNumberField(TEXT("z"), L.Z);
    return PA2_Serialize(O);
#else
    return PA2_Error(TEXT("editor-only"));
#endif
}

// ---- #58  fixup_redirector_referencers ------------------------------------------
FString UMCPReflectionLibrary::FixupRedirectorReferencers(const FString& TargetPath)
{
#if WITH_EDITOR
    const FString Path = TargetPath.TrimStartAndEnd();
    if (Path.IsEmpty()) return PA2_Error(TEXT("empty path (pass a redirector object path or a /Game directory to scan)"));

    TArray<UObjectRedirector*> Redirs;
    // A specific redirector object path first; else treat as a package directory and scan the registry.
    if (UObjectRedirector* Direct = Cast<UObjectRedirector>(LoadObject<UObjectRedirector>(nullptr, *Path)))
    {
        Redirs.Add(Direct);
    }
    else
    {
        FAssetRegistryModule& ARM = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry"));
        FARFilter Filter;
        Filter.bRecursivePaths = true;
        Filter.PackagePaths.Add(FName(*Path));
        Filter.ClassPaths.Add(UObjectRedirector::StaticClass()->GetClassPathName());
        TArray<FAssetData> Assets;
        ARM.Get().GetAssets(Filter, Assets);
        for (const FAssetData& AD : Assets)
        {
            if (UObjectRedirector* R = Cast<UObjectRedirector>(AD.GetAsset()))
            {
                Redirs.Add(R);
            }
        }
    }

    if (Redirs.Num() == 0)
    {
        return PA2_Error(FString::Printf(TEXT("no ObjectRedirectors found at '%s' (already fixed up, or not a redirector/directory)"), *Path));
    }

    TArray<TSharedPtr<FJsonValue>> Names;
    for (UObjectRedirector* R : Redirs) Names.Add(MakeShared<FJsonValueString>(R->GetPathName()));

    IAssetTools& AssetTools = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools")).Get();
    // Resaves every referencer to point at each redirector's target, then deletes the fixed-up redirectors.
    AssetTools.FixupReferencers(Redirs, /*bCheckoutDialogPrompt*/false, ERedirectFixupMode::DeleteFixedUpRedirectors);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetNumberField(TEXT("fixed_count"), Names.Num());
    O->SetArrayField(TEXT("redirectors"), Names);
    O->SetStringField(TEXT("note"), TEXT("referencers resaved against the target; fixed-up redirectors deleted"));
    return PA2_Serialize(O);
#else
    return PA2_Error(TEXT("editor-only"));
#endif
}

// ---- #58  set_inherited_component_property ---------------------------------------
FString UMCPReflectionLibrary::SetInheritedComponentProperty(UBlueprint* Blueprint, const FString& ComponentName,
                                                             const FString& PropertyName, const FString& ValueText)
{
#if WITH_EDITOR
    if (!Blueprint) return PA2_Error(TEXT("null Blueprint"));
    UClass* GenClass = Blueprint->GeneratedClass;
    if (!GenClass) return PA2_Error(TEXT("Blueprint has no GeneratedClass — compile it first"));
    UObject* CDO = GenClass->GetDefaultObject();
    if (!CDO) return PA2_Error(TEXT("no generated-class CDO"));

    // Resolve the inherited native component on the CDO. Users pass the UPROPERTY name (Mesh, CapsuleComponent,
    // CharacterMovement) but the actual DEFAULT SUBOBJECT names differ (ACharacter's are CharacterMesh0 /
    // CollisionCylinder / CharMoveComp), so GetDefaultSubobjectByName(propName) misses. Resolve by property first
    // (how get_editor_property('mesh') works), then by subobject name, then by scanning the CDO's components.
    UActorComponent* Comp = nullptr;
    if (FObjectProperty* OP = FindFProperty<FObjectProperty>(GenClass, *ComponentName))
    {
        Comp = Cast<UActorComponent>(OP->GetObjectPropertyValue_InContainer(CDO));
    }
    if (!Comp)
    {
        Comp = Cast<UActorComponent>(CDO->GetDefaultSubobjectByName(FName(*ComponentName)));
    }
    if (!Comp)
    {
        if (AActor* CDOActor = Cast<AActor>(CDO))
        {
            TInlineComponentArray<UActorComponent*> Comps;
            CDOActor->GetComponents(Comps);
            for (UActorComponent* C : Comps)
            {
                if (C && (C->GetName() == ComponentName || C->GetFName() == FName(*ComponentName)))
                {
                    Comp = C; break;
                }
            }
        }
    }
    if (!Comp)
    {
        return PA2_Error(FString::Printf(TEXT("no component '%s' on %s's CDO (try the UPROPERTY name: Mesh, CapsuleComponent, CharacterMovement, ...)"),
            *ComponentName, *GenClass->GetName()));
    }

    FProperty* Prop = FindFProperty<FProperty>(Comp->GetClass(), *PropertyName);
    if (!Prop)
    {
        return PA2_Error(FString::Printf(TEXT("no property '%s' on component %s (%s)"), *PropertyName, *ComponentName, *Comp->GetClass()->GetName()));
    }

    Comp->Modify();
    void* Dest = Prop->ContainerPtrToValuePtr<void>(Comp);
    // ImportText parses ValueText for ANY type: float "90.0", vector "(X=0,Y=0,Z=-90)", enum "AnimationSingleNode",
    // object ref "/Game/.../SKM.SKM", struct literal "(...)". Returns nullptr on a parse failure.
    const TCHAR* Result = Prop->ImportText_Direct(*ValueText, Dest, Comp, PPF_None);
    if (Result == nullptr)
    {
        return PA2_Error(FString::Printf(TEXT("could not parse '%s' into %s.%s (%s)"), *ValueText, *ComponentName, *PropertyName, *Prop->GetClass()->GetName()));
    }

    FPropertyChangedEvent Evt(Prop);
    Comp->PostEditChangeProperty(Evt);
    CDO->MarkPackageDirty();
    FBlueprintEditorUtils::MarkBlueprintAsModified(Blueprint);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("blueprint"), Blueprint->GetPathName());
    O->SetStringField(TEXT("component"), ComponentName);
    O->SetStringField(TEXT("property"), PropertyName);
    O->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());
    O->SetStringField(TEXT("value"), ValueText);
    O->SetStringField(TEXT("note"), TEXT("written to the generated-class CDO subobject (survives recompile); save the Blueprint to persist"));
    return PA2_Serialize(O);
#else
    return PA2_Error(TEXT("editor-only"));
#endif
}
