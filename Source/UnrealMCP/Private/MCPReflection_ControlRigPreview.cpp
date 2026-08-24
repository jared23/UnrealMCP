// UnrealMCP — CONTROL RIG editor PREVIEW-ANIMATION play/stop (C++ #49, 2026-08-19).
//
// The last-blocked Control Rig pair: play_rig_preview_animation / stop_rig_preview_animation. The old
// note said this needed the PRIVATE FControlRigEditor toolkit (GetPersonaToolkit()->GetPreviewScene()->
// GetPreviewMeshComponent()). It does NOT: the CR editor's preview mesh is a UDebugSkelMeshComponent
// (UnrealEd, UNREALED_API) living in an EWorldType::EditorPreview world, and its UAnimPreviewInstance
// (AnimGraph) drives single-node playback. Both are PUBLIC. We find the component by TObjectIterator
// (no private editor headers) and drive its preview instance. The CR editor opens headless in this
// build (open_editor_for_assets returns true; get_currently_open_rig_blueprints tracks it), so the
// preview component exists once the caller has opened the rig editor.
//
// Handlers (FString/float/bool-only across the .h boundary — block #49 in MCPReflectionLibrary.h):
//   PlayRigPreviewAnimationJson(PreviewMeshPath, AnimPath, PlayRate, bLooping) -- EnablePreview(true,Anim)
//       on the matching preview component, then PreviewInstance->SetPlaying(true)/SetPlayRate/SetLooping.
//   StopRigPreviewAnimationJson(PreviewMeshPath) -- PreviewInstance->SetPlaying(false) (inverse of play).
//   GetRigPreviewStateJson(PreviewMeshPath) -- read is_playing / current anim / position (verify + read).
//
// Matching: iterate UDebugSkelMeshComponent whose World is EditorPreview; if PreviewMeshPath is given,
// require GetSkeletalMeshAsset() == that mesh; prefer the UControlRigSkeletalMeshComponent subclass
// (found by reflected class name -- NO ControlRigEditor dep). Empty PreviewMeshPath -> first CR preview.
//
// Reversibility: play <-> stop is a natural NON-LEDGERED runtime inverse pair (like PCG generate/cleanup);
// no editor_level.undo fold needed. LINKAGE: UnrealEd (public dep) + AnimGraph (private dep, #18) -> NO
// Build.cs change, NO engine export patch. All #if WITH_EDITOR; null-guarded; skeleton-compat checked
// before EnablePreview (an incompatible anim would otherwise assert).

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "CoreMinimal.h"
#include "UObject/UObjectIterator.h"                 // TObjectIterator
#include "UObject/UObjectGlobals.h"                  // LoadObject
#include "Engine/World.h"                            // EWorldType
#include "Engine/SkeletalMesh.h"                     // USkeletalMesh
#include "Animation/AnimationAsset.h"                // UAnimationAsset
#include "Animation/Skeleton.h"                      // USkeleton
#include "Animation/DebugSkelMeshComponent.h"        // UDebugSkelMeshComponent (UnrealEd)
#include "AnimPreviewInstance.h"                     // UAnimPreviewInstance (AnimGraph)

// Windows <winbase.h> #defines GetCurrentTime -> GetTickCount, which would rewrite our
// UAnimSingleNodeInstance::GetCurrentTime() call. Drop it for this TU.
#ifdef GetCurrentTime
#undef GetCurrentTime
#endif

namespace
{
    FString MCPCRP_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPCRP_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPCRP_Serialize(Root);
    }

#if WITH_EDITOR
    // Find the CR editor preview mesh component. MeshPath empty -> any EditorPreview CR component.
    UDebugSkelMeshComponent* MCPCRP_FindPreviewComp(const FString& MeshPath, FString& OutErr)
    {
        USkeletalMesh* WantMesh = MeshPath.IsEmpty() ? nullptr : LoadObject<USkeletalMesh>(nullptr, *MeshPath);
        if (!MeshPath.IsEmpty() && !WantMesh)
        {
            OutErr = FString::Printf(TEXT("could not load preview mesh '%s'"), *MeshPath);
            return nullptr;
        }
        UClass* CRClass = UClass::TryFindTypeSlow<UClass>(TEXT("ControlRigSkeletalMeshComponent"));

        UDebugSkelMeshComponent* AnyMatch = nullptr;
        UDebugSkelMeshComponent* CRMatch = nullptr;
        int32 PreviewCompCount = 0;
        for (TObjectIterator<UDebugSkelMeshComponent> It; It; ++It)
        {
            UDebugSkelMeshComponent* C = *It;
            if (!C || C->IsTemplate() || !IsValid(C))
            {
                continue;
            }
            UWorld* W = C->GetWorld();
            if (!W || W->WorldType != EWorldType::EditorPreview)
            {
                continue;
            }
            ++PreviewCompCount;
            if (WantMesh && C->GetSkeletalMeshAsset() != WantMesh)
            {
                continue;
            }
            if (!AnyMatch)
            {
                AnyMatch = C;
            }
            if (CRClass && C->IsA(CRClass) && !CRMatch)
            {
                CRMatch = C;
            }
        }
        UDebugSkelMeshComponent* Chosen = CRMatch ? CRMatch : AnyMatch;
        if (!Chosen)
        {
            OutErr = FString::Printf(TEXT("no matching preview component (EditorPreview DebugSkelMeshComponents seen: %d). "
                "Open the Control Rig editor first (open_editor_for_assets), and check the preview mesh path."), PreviewCompCount);
        }
        return Chosen;
    }

    void MCPCRP_FillState(const TSharedRef<FJsonObject>& Root, UDebugSkelMeshComponent* Comp)
    {
        Root->SetStringField(TEXT("component"), Comp->GetName());
        Root->SetStringField(TEXT("component_class"), Comp->GetClass()->GetName());
        if (USkeletalMesh* M = Comp->GetSkeletalMeshAsset())
        {
            Root->SetStringField(TEXT("preview_mesh"), M->GetPathName());
        }
        UAnimPreviewInstance* PrevInst = Comp->PreviewInstance;
        Root->SetBoolField(TEXT("has_preview_instance"), PrevInst != nullptr);
        if (PrevInst)
        {
            Root->SetBoolField(TEXT("is_playing"), PrevInst->IsPlaying());
            Root->SetNumberField(TEXT("position"), PrevInst->GetCurrentTime());
            Root->SetNumberField(TEXT("play_rate"), PrevInst->GetPlayRate());
            Root->SetBoolField(TEXT("looping"), PrevInst->IsLooping());
            if (UAnimationAsset* A = PrevInst->GetAnimationAsset())
            {
                Root->SetStringField(TEXT("current_anim"), A->GetPathName());
            }
        }
    }
#endif // WITH_EDITOR
}

// =====================================================================================================
// PlayRigPreviewAnimationJson — set the CR editor preview to play AnimPath.
// =====================================================================================================
FString UMCPReflectionLibrary::PlayRigPreviewAnimationJson(const FString& PreviewMeshPath, const FString& AnimPath,
    float PlayRate, bool bLooping)
{
#if WITH_EDITOR
    FString Err;
    UDebugSkelMeshComponent* Comp = MCPCRP_FindPreviewComp(PreviewMeshPath, Err);
    if (!Comp)
    {
        return MCPCRP_Err(Err);
    }
    UAnimationAsset* Anim = AnimPath.IsEmpty() ? nullptr : LoadObject<UAnimationAsset>(nullptr, *AnimPath);
    if (!Anim)
    {
        return MCPCRP_Err(FString::Printf(TEXT("could not load animation asset '%s'"), *AnimPath));
    }
    // Skeleton compatibility: an incompatible anim would assert inside EnablePreview.
    USkeletalMesh* Mesh = Comp->GetSkeletalMeshAsset();
    USkeleton* MeshSkel = Mesh ? Mesh->GetSkeleton() : nullptr;
    USkeleton* AnimSkel = Anim->GetSkeleton();
    if (MeshSkel && AnimSkel && MeshSkel != AnimSkel && !AnimSkel->IsCompatibleForEditor(MeshSkel))
    {
        return MCPCRP_Err(FString::Printf(TEXT("animation skeleton '%s' is not compatible with the preview mesh skeleton '%s'"),
            *AnimSkel->GetName(), *MeshSkel->GetName()));
    }

    try
    {
        Comp->EnablePreview(true, Anim);
        UAnimPreviewInstance* PrevInst = Comp->PreviewInstance;
        if (PrevInst)
        {
            if (PrevInst->GetAnimationAsset() != Anim)
            {
                PrevInst->SetAnimationAsset(Anim, bLooping, PlayRate > 0.f ? PlayRate : 1.f);
            }
            PrevInst->SetLooping(bLooping);
            PrevInst->SetPlayRate(PlayRate > 0.f ? PlayRate : 1.f);
            PrevInst->SetPlaying(true);
        }
    }
    catch (...)
    {
        return MCPCRP_Err(TEXT("exception during EnablePreview/play"));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("requested_anim"), AnimPath);
    MCPCRP_FillState(Root, Comp);
    return MCPCRP_Serialize(Root);
#else
    return MCPCRP_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// StopRigPreviewAnimationJson — pause the CR editor preview playback (inverse of play).
// =====================================================================================================
FString UMCPReflectionLibrary::StopRigPreviewAnimationJson(const FString& PreviewMeshPath)
{
#if WITH_EDITOR
    FString Err;
    UDebugSkelMeshComponent* Comp = MCPCRP_FindPreviewComp(PreviewMeshPath, Err);
    if (!Comp)
    {
        return MCPCRP_Err(Err);
    }
    try
    {
        if (UAnimPreviewInstance* PrevInst = Comp->PreviewInstance)
        {
            PrevInst->SetPlaying(false);
        }
    }
    catch (...)
    {
        return MCPCRP_Err(TEXT("exception during stop"));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetBoolField(TEXT("stopped"), true);
    MCPCRP_FillState(Root, Comp);
    return MCPCRP_Serialize(Root);
#else
    return MCPCRP_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// GetRigPreviewStateJson — read the preview playback state (read-only).
// =====================================================================================================
FString UMCPReflectionLibrary::GetRigPreviewStateJson(const FString& PreviewMeshPath)
{
#if WITH_EDITOR
    FString Err;
    UDebugSkelMeshComponent* Comp = MCPCRP_FindPreviewComp(PreviewMeshPath, Err);
    if (!Comp)
    {
        return MCPCRP_Err(Err);
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    MCPCRP_FillState(Root, Comp);
    return MCPCRP_Serialize(Root);
#else
    return MCPCRP_Err(TEXT("editor-only"));
#endif
}
