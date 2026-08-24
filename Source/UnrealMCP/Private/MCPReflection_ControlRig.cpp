// UnrealMCP reflection helper — SkeletalMesh PER-BONE weighted bounds (Control Rig spec: get_skeletal_bone_bounds).
//
// WHY C++: there is NO Python API for per-bone weighted geometry. The stock Python surface only exposes the
// whole-mesh bounds (SkeletalMesh.get_bounds / GetImportedBounds). Per-bone bounds require iterating the
// skeletal-mesh RENDER geometry (position vertex buffer + skin-weight vertex buffer) and resolving each
// vertex influence's SECTION-LOCAL bone index through the section BoneMap to the RefSkeleton — none of which
// is reachable from Python. This handler does exactly that, entirely with the Engine module (already a
// PublicDependency), so it needs NO Build.cs change and carries NO export-macro / link risk.
//
// EDITOR-SAFETY (CPU access): the whole handler is #if WITH_EDITOR. FSkeletalMeshLODRenderData::
// ShouldForceKeepCPUResources() returns true unconditionally in the editor (the r.FreeSkeletalMeshBuffers
// branch is compiled out under WITH_EDITOR — verified in SkeletalMeshLODRenderData.cpp:781-791), so the LOD
// position + skin-weight buffers retain CPU-readable data in-editor. We still defensively validate the CPU
// data pointers before touching them and never dereference an out-of-range index — on any miss we return a
// clean {"status":"error"} JSON, never crash.
//
// Member DEFINITION for UMCPReflectionLibrary; the matching UFUNCTION declaration is added to
// MCPReflectionLibrary.h by the coordinator. Anon-namespace helpers are prefixed `MCPControlRig_` so they
// stay unique across the unity build (sibling .cpp files share the concatenated TU).
//
// ---------------------------------------------------------------------------------------------------------
// NOTE FOR THE COORDINATOR — the OTHER two Control-Rig spec features in this batch need NO C++ (verified
// against engine headers), so they are intentionally NOT drafted here:
//
//  * SUBGRAPH SNAPSHOT for faithful undo of collapse/expand/promote (spec feature 2):
//      URigVMController::ExportNodesToText(TArray<FName>, bIncludeExteriorLinks=true) and
//      ImportNodesFromText(FString, bSetupUndoRedo, bPrintPythonCommands) are BOTH
//      UFUNCTION(BlueprintCallable) (RigVMController.h:638-655) — already Python-reachable. A FAITHFUL,
//      LOSSLESS inverse is therefore a pure-Python recipe (capture text before the forward op; on undo remove
//      the produced node(s) then re-import the captured text). See controlrig_cpp.py for the wiring.
//
//  * MODULAR RIG writes add/connect/auto_connect/set_config/bind/mirror (spec feature 3):
//      UModularRigController (ModularRigController.h) exposes AddModule(name,class,parent,undo) [deprecated
//      but UFUNCTION], ConnectConnectorToElement, AutoConnectModules, SetConfigValueInModule [deprecated
//      FString overload, UFUNCTION], BindModuleVariable, MirrorModule, DeleteModule — all
//      UFUNCTION(BlueprintCallable). The controller is reachable via
//      UControlRigBlueprint::GetModularRigController() (already used from Python) and FRigElementKey /
//      FRigVMMirrorSettings are BlueprintType (Python-constructible). So these are Python-native.
//      CAVEAT: the PREFERRED AddModuleFromAssetReference (ModularRigController.h:33), the newer
//      SetConfigValueInModule(FControlRigOverrideValue) (:66) and GetPossibleBindings (:74) are NOT
//      UFUNCTIONs — a C++ wrapper could expose those modern APIs, but that adds ControlRig +
//      ControlRigDeveloper LINK deps (first use in this plugin => real export/link risk) and the
//      BlueprintCallable equivalents already cover the feature. See controlrig_cpp.py.
// ---------------------------------------------------------------------------------------------------------

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Engine/SkeletalMesh.h"                     // USkeletalMesh::GetResourceForRendering / GetRefSkeleton / GetImportedBounds
#include "ReferenceSkeleton.h"                       // FReferenceSkeleton (GetNum/GetBoneName/GetParentIndex/GetRefBonePose)
#include "Rendering/SkeletalMeshRenderData.h"        // FSkeletalMeshRenderData::LODRenderData
#include "Rendering/SkeletalMeshLODRenderData.h"     // FSkeletalMeshLODRenderData (RenderSections / StaticVertexBuffers / GetSkinWeightVertexBuffer)
#include "Rendering/SkinWeightVertexBuffer.h"        // FSkinWeightVertexBuffer (GetVertexInfluenceOffsetCount/GetBoneIndex/GetBoneWeight)
#include "Rendering/PositionVertexBuffer.h"          // FPositionVertexBuffer::VertexPosition/GetNumVertices (used by value)
#include "GPUSkinPublicDefs.h"                       // MAX_TOTAL_INFLUENCES (defensive influence cap)

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPControlRig_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPControlRig_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPControlRig_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPControlRig_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

#if WITH_EDITOR
    // A 3-number JSON array [x,y,z] (rounded for compactness).
    TSharedPtr<FJsonValue> MCPControlRig_Vec3(const FVector& V)
    {
        TArray<TSharedPtr<FJsonValue>> Arr;
        Arr.Add(MakeShared<FJsonValueNumber>(FMath::RoundToDouble(V.X * 1000.0) / 1000.0));
        Arr.Add(MakeShared<FJsonValueNumber>(FMath::RoundToDouble(V.Y * 1000.0) / 1000.0));
        Arr.Add(MakeShared<FJsonValueNumber>(FMath::RoundToDouble(V.Z * 1000.0) / 1000.0));
        return MakeShared<FJsonValueArray>(Arr);
    }

    // Emit a component of {min,max,center,extent} for a bounding box into a fresh JSON object.
    TSharedRef<FJsonObject> MCPControlRig_BoxJson(const FBox& Box)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (Box.IsValid)
        {
            const FVector Center = Box.GetCenter();
            const FVector Extent = Box.GetExtent();
            J->SetField(TEXT("min"), MCPControlRig_Vec3(Box.Min));
            J->SetField(TEXT("max"), MCPControlRig_Vec3(Box.Max));
            J->SetField(TEXT("center"), MCPControlRig_Vec3(Center));
            J->SetField(TEXT("extent"), MCPControlRig_Vec3(Extent));
            J->SetNumberField(TEXT("radius"), FMath::RoundToDouble(Extent.Size() * 1000.0) / 1000.0);
        }
        else
        {
            J->SetBoolField(TEXT("empty"), true);
        }
        return J;
    }

    // Case-insensitive filter: empty => match all. Otherwise the bone name must CONTAIN one of the
    // comma-separated tokens (each token trimmed / lower-cased). "root,spine" matches any bone whose
    // name contains "root" or "spine".
    bool MCPControlRig_BoneMatchesFilter(const FString& BoneNameLower, const TArray<FString>& Tokens)
    {
        if (Tokens.Num() == 0) { return true; }
        for (const FString& Tok : Tokens)
        {
            if (!Tok.IsEmpty() && BoneNameLower.Contains(Tok)) { return true; }
        }
        return false;
    }
#endif // WITH_EDITOR
}

// ============================================================================================
// get_skeletal_bone_bounds — per-bone weighted bounds from a SkeletalMesh.
// Engine-module-only; NO Build.cs change. Read-only (no ledger / no undo op).
// ============================================================================================
//
// WeightMode (case-insensitive):
//   "dominant" (default) : each vertex contributes ONLY to the single bone carrying its highest influence
//                          weight (tight, non-overlapping per-bone boxes — best for gizmo fitting).
//   "any"                : each vertex contributes to EVERY bone whose influence weight >= MinWeight
//                          (looser, overlapping boxes — best for "which bones touch this region").
// MinWeight: normalized 0..1 threshold (default 0). In "any" mode it culls weak influences; in "dominant"
//            mode it is a floor on the winning weight (a vertex whose max weight < MinWeight is skipped).
// LODIndex : which LOD's render geometry to sample (default 0 = highest detail; clamped to valid range).
// BoneFilter: optional comma-separated case-insensitive substring tokens (see MCPControlRig_BoneMatchesFilter).
//
// Output (per emitted bone): { name, index, parent, parent_index, vertex_count, influence_count,
//   bounds_component:{min,max,center,extent,radius}, bounds_local:{...}, ref_location:[x,y,z] }.
//   bounds_component = box in mesh/component (ref-pose) space; bounds_local = same points expressed in the
//   bone's own ref-pose space (via the bone's inverse component transform) — the space a control gizmo uses.
FString UMCPReflectionLibrary::GetSkeletalBoneBoundsJson(USkeletalMesh* Mesh, const FString& BoneFilter,
    const FString& WeightMode, int32 LODIndex, float MinWeight)
{
#if WITH_EDITOR
    if (!Mesh) { return MCPControlRig_Error(TEXT("null skeletal mesh")); }

    FSkeletalMeshRenderData* RenderData = Mesh->GetResourceForRendering(); // VERIFY vs engine source (USkeletalMesh::GetResourceForRendering)
    if (!RenderData || RenderData->LODRenderData.Num() == 0)
    {
        return MCPControlRig_Error(TEXT("skeletal mesh has no render data (not built / not loaded)"));
    }

    const int32 NumLODs = RenderData->LODRenderData.Num();
    const int32 UseLOD = FMath::Clamp(LODIndex, 0, NumLODs - 1);
    FSkeletalMeshLODRenderData& LODData = RenderData->LODRenderData[UseLOD];

    // Position buffer + skin-weight buffer (with CPU-data validity guards; editor keeps CPU data).
    const FPositionVertexBuffer& PosBuf = LODData.StaticVertexBuffers.PositionVertexBuffer; // VERIFY vs engine source (member path)
    const FSkinWeightVertexBuffer* SkinBuf = LODData.GetSkinWeightVertexBuffer();            // returns default-override buffer if present
    if (!SkinBuf) { return MCPControlRig_Error(TEXT("LOD has no skin-weight vertex buffer")); }
    if (PosBuf.GetNumVertices() == 0) { return MCPControlRig_Error(TEXT("LOD position buffer is empty (no CPU vertex data)")); }
    if (const FSkinWeightDataVertexBuffer* DataVB = SkinBuf->GetDataVertexBuffer())
    {
        if (DataVB->GetWeightData() == nullptr) // CPU copy discarded (would be a cooked/streamed-out mesh)
        {
            return MCPControlRig_Error(TEXT("skin-weight buffer has no CPU-readable data for this LOD"));
        }
    }

    const FReferenceSkeleton& RefSkel = Mesh->GetRefSkeleton(); // VERIFY vs engine source (USkeletalMesh::GetRefSkeleton const)
    const int32 NumBones = RefSkel.GetNum();
    if (NumBones == 0) { return MCPControlRig_Error(TEXT("skeletal mesh reference skeleton is empty")); }

    // Component-space ref-pose transforms (bone-local FinalRefBonePose accumulated through parents; the
    // ref skeleton is ordered parent-before-child so a single forward pass is correct).
    const TArray<FTransform>& LocalPose = RefSkel.GetRefBonePose(); // FinalRefBonePose (bone-local)
    TArray<FTransform> CompSpace;
    CompSpace.SetNum(NumBones);
    for (int32 i = 0; i < NumBones; ++i)
    {
        const FTransform Local = LocalPose.IsValidIndex(i) ? LocalPose[i] : FTransform::Identity;
        const int32 ParentIdx = RefSkel.GetParentIndex(i);
        CompSpace[i] = (ParentIdx == INDEX_NONE || !CompSpace.IsValidIndex(ParentIdx))
            ? Local
            : (Local * CompSpace[ParentIdx]); // child_component = child_local * parent_component (UE convention)
    }

    // Parse mode + filter + threshold.
    const FString Mode = WeightMode.IsEmpty() ? FString(TEXT("dominant")) : WeightMode.ToLower();
    const bool bDominant = !(Mode == TEXT("any") || Mode == TEXT("all"));
    const uint16 RawThreshold = (uint16)FMath::Clamp<int32>(FMath::RoundToInt(FMath::Clamp(MinWeight, 0.f, 1.f) * 65535.f), 0, 65535);

    TArray<FString> FilterTokens;
    {
        TArray<FString> Raw;
        BoneFilter.ToLower().ParseIntoArray(Raw, TEXT(","), /*bCullEmpty*/ true);
        for (FString& T : Raw) { FilterTokens.Add(T.TrimStartAndEnd()); }
    }
    const bool bFilterExplicit = FilterTokens.Num() > 0;

    // Per-bone accumulators.
    TArray<FBox> ComponentBoxes;   ComponentBoxes.Init(FBox(ForceInit), NumBones);
    TArray<FBox> LocalBoxes;       LocalBoxes.Init(FBox(ForceInit), NumBones);
    TArray<int32> VertexCounts;    VertexCounts.Init(0, NumBones);
    TArray<int32> InfluenceCounts; InfluenceCounts.Init(0, NumBones);

    auto AccumulateBone = [&](int32 RealBone, const FVector& CompPos)
    {
        if (!ComponentBoxes.IsValidIndex(RealBone)) { return; }
        ComponentBoxes[RealBone] += CompPos;
        LocalBoxes[RealBone] += CompSpace[RealBone].InverseTransformPosition(CompPos); // VERIFY vs engine source (FTransform::InverseTransformPosition)
        VertexCounts[RealBone] += 1;
    };

    // Walk every section; each vertex spans [BaseVertexIndex, BaseVertexIndex+NumVertices) in the LOD-global
    // position/skin buffers. Skin-weight bone indices are SECTION-LOCAL — resolved via Section.BoneMap.
    for (const FSkelMeshRenderSection& Section : LODData.RenderSections)
    {
        const TArray<FBoneIndexType>& BoneMap = Section.BoneMap;
        const uint32 SectionBase = Section.BaseVertexIndex;
        const uint32 SectionNum = Section.NumVertices;

        for (uint32 Local = 0; Local < SectionNum; ++Local)
        {
            const uint32 V = SectionBase + Local;
            if (V >= PosBuf.GetNumVertices()) { break; }

            uint32 WeightOffset = 0, InfluenceCount = 0;
            SkinBuf->GetVertexInfluenceOffsetCount(V, WeightOffset, InfluenceCount); // VERIFY vs engine source
            if (InfluenceCount == 0) { continue; }
            InfluenceCount = FMath::Min<uint32>(InfluenceCount, (uint32)MAX_TOTAL_INFLUENCES);

            const FVector3f P3 = PosBuf.VertexPosition(V);
            const FVector CompPos((double)P3.X, (double)P3.Y, (double)P3.Z);

            if (bDominant)
            {
                uint16 BestWeight = 0; int32 BestLocalBone = INDEX_NONE;
                for (uint32 Inf = 0; Inf < InfluenceCount; ++Inf)
                {
                    const uint16 W = SkinBuf->GetBoneWeight(V, Inf); // VERIFY vs engine source
                    if (W > BestWeight) { BestWeight = W; BestLocalBone = (int32)SkinBuf->GetBoneIndex(V, Inf); }
                }
                if (BestLocalBone != INDEX_NONE && BestWeight >= RawThreshold && BoneMap.IsValidIndex(BestLocalBone))
                {
                    const int32 RealBone = (int32)BoneMap[BestLocalBone];
                    AccumulateBone(RealBone, CompPos);
                    if (ComponentBoxes.IsValidIndex(RealBone)) { InfluenceCounts[RealBone] += 1; }
                }
            }
            else // "any"
            {
                for (uint32 Inf = 0; Inf < InfluenceCount; ++Inf)
                {
                    const uint16 W = SkinBuf->GetBoneWeight(V, Inf);
                    if (W < RawThreshold || W == 0) { continue; }
                    const int32 LocalBone = (int32)SkinBuf->GetBoneIndex(V, Inf);
                    if (!BoneMap.IsValidIndex(LocalBone)) { continue; }
                    const int32 RealBone = (int32)BoneMap[LocalBone];
                    AccumulateBone(RealBone, CompPos);
                    if (ComponentBoxes.IsValidIndex(RealBone)) { InfluenceCounts[RealBone] += 1; }
                }
            }
        }
    }

    // Emit. With an explicit filter we also emit MATCHED bones that had zero contributing vertices (so the
    // caller sees the bone exists but is unskinned in this LOD); with no filter we only emit skinned bones.
    TArray<TSharedPtr<FJsonValue>> BonesJson;
    int32 SkinnedBoneCount = 0;
    for (int32 b = 0; b < NumBones; ++b)
    {
        const FString BoneName = RefSkel.GetBoneName(b).ToString();
        const bool bMatches = MCPControlRig_BoneMatchesFilter(BoneName.ToLower(), FilterTokens);
        if (!bMatches) { continue; }
        const bool bHasVerts = VertexCounts[b] > 0;
        if (bHasVerts) { ++SkinnedBoneCount; }
        if (!bHasVerts && !bFilterExplicit) { continue; }

        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), BoneName);
        J->SetNumberField(TEXT("index"), b);
        const int32 ParentIdx = RefSkel.GetParentIndex(b);
        J->SetNumberField(TEXT("parent_index"), ParentIdx);
        J->SetStringField(TEXT("parent"), RefSkel.IsValidIndex(ParentIdx) ? RefSkel.GetBoneName(ParentIdx).ToString() : FString(TEXT("None")));
        J->SetNumberField(TEXT("vertex_count"), VertexCounts[b]);
        J->SetNumberField(TEXT("influence_count"), InfluenceCounts[b]);
        J->SetField(TEXT("ref_location"), MCPControlRig_Vec3(CompSpace[b].GetLocation()));
        J->SetObjectField(TEXT("bounds_component"), MCPControlRig_BoxJson(ComponentBoxes[b]));
        J->SetObjectField(TEXT("bounds_local"), MCPControlRig_BoxJson(LocalBoxes[b]));
        BonesJson.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPControlRig_Ok();
    Root->SetStringField(TEXT("mesh"), Mesh->GetPathName());
    Root->SetNumberField(TEXT("lod"), UseLOD);
    Root->SetNumberField(TEXT("lod_count"), NumLODs);
    Root->SetNumberField(TEXT("bone_count"), NumBones);
    Root->SetNumberField(TEXT("section_count"), LODData.RenderSections.Num());
    Root->SetNumberField(TEXT("lod_vertex_count"), (double)PosBuf.GetNumVertices());
    Root->SetStringField(TEXT("weight_mode"), bDominant ? TEXT("dominant") : TEXT("any"));
    Root->SetNumberField(TEXT("min_weight"), MinWeight);
    Root->SetNumberField(TEXT("skinned_bone_count"), SkinnedBoneCount);
    Root->SetNumberField(TEXT("emitted_bone_count"), BonesJson.Num());
    // Whole-mesh bounds for context (the only thing Python could already read).
    {
        const FBoxSphereBounds MeshBounds = Mesh->GetImportedBounds(); // VERIFY vs engine source (USkeletalMesh::GetImportedBounds)
        TSharedRef<FJsonObject> MB = MakeShared<FJsonObject>();
        MB->SetField(TEXT("origin"), MCPControlRig_Vec3(MeshBounds.Origin));
        MB->SetField(TEXT("box_extent"), MCPControlRig_Vec3(MeshBounds.BoxExtent));
        MB->SetNumberField(TEXT("sphere_radius"), FMath::RoundToDouble(MeshBounds.SphereRadius * 1000.0) / 1000.0);
        Root->SetObjectField(TEXT("mesh_bounds"), MB);
    }
    Root->SetArrayField(TEXT("bones"), BonesJson);
    return MCPControlRig_SerializeJson(Root);
#else
    return MCPControlRig_Error(TEXT("editor-only"));
#endif
}
