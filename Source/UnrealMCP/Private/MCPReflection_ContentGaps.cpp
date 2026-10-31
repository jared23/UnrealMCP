// ============================================================================
// MCPReflection_ContentGaps.cpp  —  C++ #61 (Companion-Game content gaps)
// ----------------------------------------------------------------------------
// Three self-contained handlers for gaps logged in Project_Aimon's
// MCP_TOOLING_GAPS.md (2026-10):
//   * ResampleBlendSpace          — a tool-built BlendSpace never gets runtime
//                                    BlendSpaceData (segments/triangles) until the
//                                    Persona editor's ResampleData() runs; call it directly.
//   * RegeneratePhysicsAsset /
//     CreatePhysicsBody           — write physics-asset bodies (no UE-Python path;
//                                    SkeletalBodySetups is protected). Mirrors the
//                                    project's own UAimonHurtboxAuthoring helper.
//   * CopyAssetsWithDependencies  — dialog-free dependency-aware copy, re-pointed +
//                                    renamed + Niagara recompiled. Mirrors
//                                    UAimonAssetPromotion::PromoteAssets.
//
// Build.cs adds "PhysicsUtilities" (FPhysicsAssetUtils). AssetTools/AssetRegistry
// (#58), Niagara (#5), UnrealEd (FileHelpers) are already deps. All ENGINE/editor
// symbols here are *_API-exported -> NO engine export patch. Every handler is
// null-guarded and returns {"status":ok|error,...} condensed JSON (never crashes).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

// --- core ---
#include "UObject/Package.h"
#include "Misc/PackageName.h"

// --- #2 blend space ---
#include "Animation/BlendSpace.h"
#include "Animation/BlendSpace1D.h"

// --- #3 physics ---
#include "Engine/SkeletalMesh.h"
#include "PhysicsEngine/PhysicsAsset.h"
#include "PhysicsEngine/SkeletalBodySetup.h"
#include "PhysicsEngine/BodySetup.h"
#include "PhysicsEngine/AggregateGeom.h"
#include "PhysicsAssetUtils.h"

// --- #4 copy-with-dependencies ---
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "AssetToolsModule.h"
#include "IAssetTools.h"
#include "FileHelpers.h"                 // UEditorLoadingAndSavingUtils
#include "NiagaraSystem.h"

namespace
{
    FString MCPContent_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
            TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString MCPContent_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("error"));
        Obj->SetStringField(TEXT("error"), Message);
        return MCPContent_Serialize(Obj);
    }

    TSharedRef<FJsonObject> MCPContent_Ok()
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("ok"));
        return Obj;
    }

    TSharedPtr<FJsonObject> MCPContent_ParseObject(const FString& Json)
    {
        TSharedPtr<FJsonObject> Parsed;
        if (Json.IsEmpty()) { return nullptr; }
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Json);
        FJsonSerializer::Deserialize(Reader, Parsed);
        return Parsed;
    }
}

// ===========================================================================
//  #2  ResampleBlendSpace — run the Persona editor's ValidateSampleData() +
//      ResampleData() so a script-built blend space gets its runtime
//      BlendSpaceData (1D Segments / 2D Triangles). Without this a tool-added
//      sample leaves the Blend Space Player outputting the bind pose.
// ===========================================================================
FString UMCPReflectionLibrary::ResampleBlendSpace(const FString& BlendSpacePath)
{
    if (BlendSpacePath.IsEmpty()) { return MCPContent_Error(TEXT("BlendSpacePath is empty")); }
    UBlendSpace* BS = LoadObject<UBlendSpace>(nullptr, *BlendSpacePath);
    if (!BS) { return MCPContent_Error(FString::Printf(TEXT("Could not load blend space '%s'"), *BlendSpacePath)); }

    const int32 SampleCount = BS->GetNumberOfBlendSamples();   // ENGINE_API

    BS->Modify();
    BS->ValidateSampleData();   // ENGINE_API — drop invalid samples, recompute ranges
    BS->ResampleData();         // ENGINE_API — rebuild Segments (1D) / Triangles (2D); what SBlendSpaceEditor runs
    BS->MarkPackageDirty();

    TSharedRef<FJsonObject> Obj = MCPContent_Ok();
    Obj->SetStringField(TEXT("blend_space"), BS->GetPathName());
    Obj->SetNumberField(TEXT("sample_count"), SampleCount);
    Obj->SetBoolField(TEXT("is_1d"), BS->IsA<UBlendSpace1D>());
    Obj->SetStringField(TEXT("note"), TEXT("Resampled; save the asset to persist the BlendSpaceData."));
    return MCPContent_Serialize(Obj);
}

// ===========================================================================
//  #3a RegeneratePhysicsAsset — auto-generate per-bone bodies on a skeletal
//      mesh's physics asset (FPhysicsAssetUtils::CreateFromSkeletalMesh),
//      optionally keeping only a bone whitelist. Mirrors UAimonHurtboxAuthoring.
//      ParamsJson (all optional): {geom_type:capsule|sphere|box, min_bone_size,
//        body_for_all:bool, create_constraints:bool, disable_collision_by_default:bool,
//        auto_orient:bool}. KeepBonesJson: JSON array of bone names (empty=keep all).
// ===========================================================================
FString UMCPReflectionLibrary::RegeneratePhysicsAsset(const FString& SkeletalMeshPath,
                                                      const FString& ParamsJson, const FString& KeepBonesJson)
{
#if WITH_EDITOR
    if (SkeletalMeshPath.IsEmpty()) { return MCPContent_Error(TEXT("SkeletalMeshPath is empty")); }
    USkeletalMesh* Mesh = LoadObject<USkeletalMesh>(nullptr, *SkeletalMeshPath);
    if (!Mesh) { return MCPContent_Error(FString::Printf(TEXT("Could not load skeletal mesh '%s'"), *SkeletalMeshPath)); }
    UPhysicsAsset* PA = Mesh->GetPhysicsAsset();
    if (!PA) { return MCPContent_Error(TEXT("Skeletal mesh has no physics asset assigned (assign one first)")); }

    // Bone whitelist (empty -> keep every generated body).
    TSet<FName> KeepBones;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(KeepBonesJson);
        TArray<TSharedPtr<FJsonValue>> Arr;
        if (!KeepBonesJson.IsEmpty() && FJsonSerializer::Deserialize(Reader, Arr))
        {
            for (const TSharedPtr<FJsonValue>& V : Arr)
            {
                FString B;
                if (V.IsValid() && V->TryGetString(B) && !B.IsEmpty()) { KeepBones.Add(FName(*B)); }
            }
        }
    }

    FPhysAssetCreateParams Params;
    Params.GeomType = EFG_Sphyl;
    Params.VertWeight = EVW_DominantWeight;
    Params.MinBoneSize = 0.f;
    Params.MinWeldSize = 0.f;
    Params.bBodyForAll = true;
    Params.bWalkPastSmall = false;
    Params.bAutoOrientToBone = true;
    Params.bCreateConstraints = true;
    Params.bDisableCollisionsByDefault = true;
    Params.AngularConstraintMode = ACM_Limited;

    if (TSharedPtr<FJsonObject> P = MCPContent_ParseObject(ParamsJson))
    {
        FString G;
        if (P->TryGetStringField(TEXT("geom_type"), G))
        {
            const FString GL = G.ToLower();
            if (GL == TEXT("sphere")) { Params.GeomType = EFG_Sphere; }
            else if (GL == TEXT("box")) { Params.GeomType = EFG_Box; }
            else { Params.GeomType = EFG_Sphyl; }   // capsule / sphyl default
        }
        double D; bool B;
        if (P->TryGetNumberField(TEXT("min_bone_size"), D)) { Params.MinBoneSize = (float)D; }
        if (P->TryGetBoolField(TEXT("body_for_all"), B)) { Params.bBodyForAll = B; }
        if (P->TryGetBoolField(TEXT("create_constraints"), B)) { Params.bCreateConstraints = B; }
        if (P->TryGetBoolField(TEXT("disable_collision_by_default"), B)) { Params.bDisableCollisionsByDefault = B; }
        if (P->TryGetBoolField(TEXT("auto_orient"), B)) { Params.bAutoOrientToBone = B; }
    }

    PA->Modify();
    // Start clean (DestroyBody also removes that body's constraints).
    for (int32 i = PA->SkeletalBodySetups.Num() - 1; i >= 0; --i)
    {
        FPhysicsAssetUtils::DestroyBody(PA, i);
    }

    FText ErrorText;
    if (!FPhysicsAssetUtils::CreateFromSkeletalMesh(PA, Mesh, Params, ErrorText, /*bSetToMesh*/ false, /*bShowProgress*/ false))
    {
        return MCPContent_Error(FString::Printf(TEXT("generation failed: %s"), *ErrorText.ToString()));
    }

    TArray<FString> Removed;
    if (KeepBones.Num() > 0)
    {
        for (int32 i = PA->SkeletalBodySetups.Num() - 1; i >= 0; --i)
        {
            const USkeletalBodySetup* Setup = PA->SkeletalBodySetups[i];
            if (Setup && !KeepBones.Contains(Setup->BoneName))
            {
                Removed.Add(Setup->BoneName.ToString());
                FPhysicsAssetUtils::DestroyBody(PA, i);
            }
        }
    }

    PA->UpdateBodySetupIndexMap();
    PA->UpdateBoundsBodiesArray();
    PA->RefreshPhysicsAssetChange();
    PA->MarkPackageDirty();

    TArray<TSharedPtr<FJsonValue>> Bodies;
    for (const USkeletalBodySetup* Setup : PA->SkeletalBodySetups)
    {
        if (!Setup) { continue; }
        TSharedRef<FJsonObject> B = MakeShared<FJsonObject>();
        B->SetStringField(TEXT("bone"), Setup->BoneName.ToString());
        if (Setup->AggGeom.SphylElems.Num() > 0)
        {
            const FKSphylElem& E = Setup->AggGeom.SphylElems[0];
            B->SetStringField(TEXT("shape"), FString::Printf(TEXT("sphyl r%.1f l%.1f"), E.Radius, E.Length));
        }
        else if (Setup->AggGeom.SphereElems.Num() > 0) { B->SetStringField(TEXT("shape"), FString::Printf(TEXT("sphere r%.1f"), Setup->AggGeom.SphereElems[0].Radius)); }
        else if (Setup->AggGeom.BoxElems.Num() > 0) { B->SetStringField(TEXT("shape"), TEXT("box")); }
        else { B->SetStringField(TEXT("shape"), TEXT("none")); }
        Bodies.Add(MakeShared<FJsonValueObject>(B));
    }

    TSharedRef<FJsonObject> Obj = MCPContent_Ok();
    Obj->SetStringField(TEXT("physics_asset"), PA->GetPathName());
    Obj->SetNumberField(TEXT("body_count"), PA->SkeletalBodySetups.Num());
    Obj->SetNumberField(TEXT("constraint_count"), PA->ConstraintSetup.Num());
    Obj->SetArrayField(TEXT("bodies"), Bodies);
    if (Removed.Num() > 0)
    {
        TArray<TSharedPtr<FJsonValue>> R;
        for (const FString& S : Removed) { R.Add(MakeShared<FJsonValueString>(S)); }
        Obj->SetArrayField(TEXT("removed_bodies"), R);
    }
    Obj->SetStringField(TEXT("note"), TEXT("Save the physics asset to persist."));
    return MCPContent_Serialize(Obj);
#else
    return MCPContent_Error(TEXT("RegeneratePhysicsAsset is editor-only"));
#endif
}

// ===========================================================================
//  #3b CreatePhysicsBody — add ONE capsule/sphere/box body for a bone to a
//      physics asset (surgical alternative to the full regenerate). CenterJson
//      is an optional [x,y,z] local offset. Returns the new body index.
// ===========================================================================
FString UMCPReflectionLibrary::CreatePhysicsBody(const FString& PhysicsAssetPath, const FString& BoneName,
                                                 const FString& Shape, float Radius, float Length,
                                                 const FString& CenterJson)
{
#if WITH_EDITOR
    if (PhysicsAssetPath.IsEmpty() || BoneName.IsEmpty()) { return MCPContent_Error(TEXT("PhysicsAssetPath and BoneName are required")); }
    UPhysicsAsset* PA = LoadObject<UPhysicsAsset>(nullptr, *PhysicsAssetPath);
    if (!PA) { return MCPContent_Error(FString::Printf(TEXT("Could not load physics asset '%s'"), *PhysicsAssetPath)); }

    FVector Center = FVector::ZeroVector;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(CenterJson);
        TArray<TSharedPtr<FJsonValue>> Arr;
        if (!CenterJson.IsEmpty() && FJsonSerializer::Deserialize(Reader, Arr) && Arr.Num() == 3)
        {
            Center = FVector(Arr[0]->AsNumber(), Arr[1]->AsNumber(), Arr[2]->AsNumber());
        }
    }

    PA->Modify();
    FPhysAssetCreateParams Params;   // CreateNewBody only needs a few of these; it makes an empty body setup.
    const int32 Index = FPhysicsAssetUtils::CreateNewBody(PA, FName(*BoneName), Params);
    if (!PA->SkeletalBodySetups.IsValidIndex(Index) || !PA->SkeletalBodySetups[Index])
    {
        return MCPContent_Error(TEXT("CreateNewBody failed"));
    }
    USkeletalBodySetup* Setup = PA->SkeletalBodySetups[Index];
    Setup->Modify();
    Setup->BoneName = FName(*BoneName);

    const FString S = Shape.ToLower();
    if (S == TEXT("sphere"))
    {
        FKSphereElem E; E.Center = Center; E.Radius = Radius > 0.f ? Radius : 10.f;
        Setup->AggGeom.SphereElems.Add(E);
    }
    else if (S == TEXT("box"))
    {
        FKBoxElem E; E.Center = Center;
        E.X = Radius > 0.f ? Radius * 2.f : 20.f;
        E.Y = Radius > 0.f ? Radius * 2.f : 20.f;
        E.Z = Length > 0.f ? Length : 20.f;
        Setup->AggGeom.BoxElems.Add(E);
    }
    else // capsule / sphyl (default)
    {
        FKSphylElem E; E.Center = Center; E.Radius = Radius > 0.f ? Radius : 10.f; E.Length = Length > 0.f ? Length : 20.f;
        Setup->AggGeom.SphylElems.Add(E);
    }

    PA->UpdateBodySetupIndexMap();
    PA->UpdateBoundsBodiesArray();
    PA->RefreshPhysicsAssetChange();
    PA->MarkPackageDirty();

    TSharedRef<FJsonObject> Obj = MCPContent_Ok();
    Obj->SetStringField(TEXT("physics_asset"), PA->GetPathName());
    Obj->SetStringField(TEXT("bone"), BoneName);
    Obj->SetStringField(TEXT("shape"), S.IsEmpty() ? TEXT("sphyl") : S);
    Obj->SetNumberField(TEXT("body_index"), Index);
    Obj->SetNumberField(TEXT("body_count"), PA->SkeletalBodySetups.Num());
    Obj->SetStringField(TEXT("note"), TEXT("Save the physics asset to persist."));
    return MCPContent_Serialize(Obj);
#else
    return MCPContent_Error(TEXT("CreatePhysicsBody is editor-only"));
#endif
}

// ===========================================================================
//  #4  CopyAssetsWithDependencies — dialog-free dependency-aware copy of
//      SourcesJson (array of object paths) into FolderMapJson (map of
//      src-package-prefix -> dst-package-prefix), re-pointed at each other,
//      optional "_PT_"-style prefix strip, Niagara copies recompiled.
//      Mirrors UAimonAssetPromotion::PromoteAssets. bDryRun returns the plan.
// ===========================================================================
FString UMCPReflectionLibrary::CopyAssetsWithDependencies(const FString& SourcesJson, const FString& FolderMapJson,
                                                          const FString& SourceRoot, bool bStripTestPrefix, bool bDryRun)
{
#if WITH_EDITOR
    // Parse sources.
    TArray<FString> Sources;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SourcesJson);
        TArray<TSharedPtr<FJsonValue>> Arr;
        if (SourcesJson.IsEmpty() || !FJsonSerializer::Deserialize(Reader, Arr))
        {
            return MCPContent_Error(TEXT("SourcesJson must be a JSON array of object paths"));
        }
        for (const TSharedPtr<FJsonValue>& V : Arr)
        {
            FString S;
            if (V.IsValid() && V->TryGetString(S) && !S.IsEmpty()) { Sources.Add(S); }
        }
    }
    if (Sources.Num() == 0) { return MCPContent_Error(TEXT("SourcesJson is empty")); }

    // Parse folder map.
    TMap<FString, FString> FolderMap;
    if (TSharedPtr<FJsonObject> M = MCPContent_ParseObject(FolderMapJson))
    {
        for (const TPair<FString, TSharedPtr<FJsonValue>>& P : M->Values)
        {
            FString Dst;
            if (P.Value.IsValid() && P.Value->TryGetString(Dst)) { FolderMap.Add(P.Key, Dst); }
        }
    }
    if (FolderMap.Num() == 0) { return MCPContent_Error(TEXT("FolderMapJson must map src-prefix -> dst-prefix")); }

    IAssetRegistry& Registry = FAssetRegistryModule::GetRegistry();
    Registry.WaitForCompletion();

    // 1. Hard-dependency closure within SourceRoot (soft refs reported, not copied).
    TArray<FName> Queue;
    TSet<FName> Closure;
    for (const FString& S : Sources)
    {
        const FName Pkg(*FPackageName::ObjectPathToPackageName(S));
        if (!FPackageName::DoesPackageExist(Pkg.ToString()))
        {
            return MCPContent_Error(FString::Printf(TEXT("source package not found: %s"), *Pkg.ToString()));
        }
        Queue.Add(Pkg);
    }
    TSet<FString> SoftNotes;
    while (Queue.Num())
    {
        const FName Pkg = Queue.Pop(EAllowShrinking::No);
        if (Closure.Contains(Pkg)) { continue; }
        Closure.Add(Pkg);
        TArray<FName> Hard;
        Registry.GetDependencies(Pkg, Hard, UE::AssetRegistry::EDependencyCategory::Package, UE::AssetRegistry::EDependencyQuery::Hard);
        for (const FName& D : Hard)
        {
            if ((SourceRoot.IsEmpty() || D.ToString().StartsWith(SourceRoot)) && !Closure.Contains(D)) { Queue.Add(D); }
        }
        TArray<FName> Soft;
        Registry.GetDependencies(Pkg, Soft, UE::AssetRegistry::EDependencyCategory::Package, UE::AssetRegistry::EDependencyQuery::Soft);
        for (const FName& D : Soft)
        {
            if (!SourceRoot.IsEmpty() && D.ToString().StartsWith(SourceRoot))
            {
                SoftNotes.Add(FString::Printf(TEXT("%s -> %s"), *Pkg.ToString(), *D.ToString()));
            }
        }
    }

    // 2. Destinations: longest matching folder prefix, optional "_PT_" strip.
    TMap<FString, FString> SrcToDst;
    TArray<FString> Unmapped;
    for (const FName& Pkg : Closure)
    {
        const FString Src = Pkg.ToString();
        const FString* BestKey = nullptr;
        for (const TPair<FString, FString>& M : FolderMap)
        {
            if (Src.StartsWith(M.Key) && (!BestKey || M.Key.Len() > BestKey->Len())) { BestKey = &M.Key; }
        }
        if (!BestKey) { Unmapped.Add(Src); continue; }
        const FString Rest = Src.RightChop(BestKey->Len());
        FString Folder, Name;
        if (!Rest.Split(TEXT("/"), &Folder, &Name, ESearchCase::CaseSensitive, ESearchDir::FromEnd)) { Name = Rest; }
        if (bStripTestPrefix)
        {
            int32 Underscore = INDEX_NONE;
            if (Name.FindChar(TEXT('_'), Underscore) && Name.Mid(Underscore, 4) == TEXT("_PT_"))
            {
                Name = Name.Left(Underscore) + Name.Mid(Underscore + 3);
            }
        }
        const FString& DstRoot = FolderMap[*BestKey];
        SrcToDst.Add(Src, DstRoot + (Folder.IsEmpty() ? FString() : Folder + TEXT("/")) + Name);
    }

    if (Unmapped.Num())
    {
        Unmapped.Sort();
        TArray<TSharedPtr<FJsonValue>> U;
        for (const FString& S : Unmapped) { U.Add(MakeShared<FJsonValueString>(S)); }
        TSharedRef<FJsonObject> Err = MakeShared<FJsonObject>();
        Err->SetStringField(TEXT("status"), TEXT("error"));
        Err->SetStringField(TEXT("error"), TEXT("dependencies under the source root that no folder mapping covers (nothing copied)"));
        Err->SetArrayField(TEXT("unmapped"), U);
        return MCPContent_Serialize(Err);
    }

    // Build the plan payload.
    TArray<TSharedPtr<FJsonValue>> PlanArr;
    {
        TArray<FString> Keys; SrcToDst.GetKeys(Keys); Keys.Sort();
        for (const FString& K : Keys)
        {
            TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
            E->SetStringField(TEXT("from"), K);
            E->SetStringField(TEXT("to"), SrcToDst[K]);
            PlanArr.Add(MakeShared<FJsonValueObject>(E));
        }
    }
    TArray<TSharedPtr<FJsonValue>> SoftArr;
    { TArray<FString> N = SoftNotes.Array(); N.Sort(); for (const FString& S : N) { SoftArr.Add(MakeShared<FJsonValueString>(S)); } }

    if (bDryRun)
    {
        TSharedRef<FJsonObject> Obj = MCPContent_Ok();
        Obj->SetBoolField(TEXT("dry_run"), true);
        Obj->SetNumberField(TEXT("package_count"), SrcToDst.Num());
        Obj->SetArrayField(TEXT("plan"), PlanArr);
        Obj->SetArrayField(TEXT("soft_refs_left_in_place"), SoftArr);
        return MCPContent_Serialize(Obj);
    }

    // 3. Copy (Advanced Copy re-points the copies at each other) and save.
    IAssetTools& AssetTools = FModuleManager::LoadModuleChecked<FAssetToolsModule>("AssetTools").Get();
    const bool bOk = AssetTools.AdvancedCopyPackages(SrcToDst, /*bForceAutosave*/ true, /*bCopyOverAllDestinationOverlaps*/ true);

    // 4. Recompile + save copied Niagara systems (they keep stale compiled data otherwise).
    int32 Recompiled = 0;
    TArray<UPackage*> ToSave;
    for (const TPair<FString, FString>& P : SrcToDst)
    {
        const FString ObjectPath = P.Value + TEXT(".") + FPackageName::GetShortName(P.Value);
        if (UNiagaraSystem* System = LoadObject<UNiagaraSystem>(nullptr, *ObjectPath))
        {
            System->RequestCompile(/*bForce*/ true);
            System->WaitForCompilationComplete();
            ToSave.Add(System->GetOutermost());
            ++Recompiled;
        }
    }
    if (ToSave.Num()) { UEditorLoadingAndSavingUtils::SavePackages(ToSave, /*bOnlyDirty*/ false); }

    TSharedRef<FJsonObject> Obj = bOk ? MCPContent_Ok() : MakeShared<FJsonObject>();
    if (!bOk) { Obj->SetStringField(TEXT("status"), TEXT("error")); Obj->SetStringField(TEXT("error"), TEXT("Advanced Copy failed")); }
    Obj->SetNumberField(TEXT("package_count"), SrcToDst.Num());
    Obj->SetNumberField(TEXT("niagara_recompiled"), Recompiled);
    Obj->SetArrayField(TEXT("plan"), PlanArr);
    Obj->SetArrayField(TEXT("soft_refs_left_in_place"), SoftArr);
    return MCPContent_Serialize(Obj);
#else
    return MCPContent_Error(TEXT("CopyAssetsWithDependencies is editor-only"));
#endif
}
