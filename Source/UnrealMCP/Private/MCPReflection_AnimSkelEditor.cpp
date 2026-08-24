// UnrealMCP — anim skeleton-SLOT authoring + editor World-Outliner FOLDER create/delete
// (C++ #18 2026-08-18). FILE-ONLY authoring; NOT yet compiled (coordinator runs the single build).
//
// Companion translation unit to MCPReflectionLibrary.cpp. Defines EIGHT additional
// UMCPReflectionLibrary member functions declared in MCPReflectionLibrary.h:
//
//   (A) USkeleton slot registry (6) — mirrors the shipped C++ #13 socket handlers
//       (AddSkeletonSocket/RemoveSkeletonSocket/AddVirtualBone/RemoveVirtualBone). Unlike the
//       sockets (protected TArray reached by reflection), the SLOT registry is exposed by PUBLIC
//       ENGINE_API methods on USkeleton (RegisterSlotNode/SetSlotGroupName/AddSlotGroupName/
//       RemoveSlotName/RemoveSlotGroup/RenameSlotName/GetSlotGroups/ContainsSlotName/
//       GetSlotGroupName/FindAnimSlotGroup) — so we call them directly. Module: Engine (linked).
//       Each mutator captures PRIOR state so the Python ledger can record a reversible inverse.
//
//   (B) Editor World-Outliner folders (2) — FActorFolders::Get().CreateFolder / DeleteFolder on
//       the editor world (GEditor->GetEditorWorldContext().World()). LevelEditorSubsystem has no
//       create-empty-folder binding; FActorFolders (UnrealEd, linked) does. Create<->Delete are a
//       reversible pair for EMPTY folders (the create tool makes empty folders).
//
// Build.cs: Engine (USkeleton + FAnimSlotGroup) and UnrealEd (FActorFolders + Editor.h/GEditor)
// are BOTH already deps -> NO Build.cs change, NO new module, NO export-macro risk.
//
// Anon-namespace helpers are PREFIXED "MCPAnimSkel_" so they are unique across the unity build
// (the main .cpp / MCPReflection_Structs.cpp carry their own TU-local copies). Every
// version-sensitive call is verified against UE 5.8 source (Animation/Skeleton.h + .cpp,
// Editor/UnrealEd/Public/EditorActorFolders.h, Runtime/Engine/Public/Folder.h).

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Animation/Skeleton.h"        // USkeleton + FAnimSlotGroup (Engine)

#if WITH_EDITOR
#include "Editor.h"                     // GEditor (UnrealEd)
#include "Engine/World.h"               // UWorld
#include "Engine/Level.h"               // ULevel (UWorld::GetCurrentLevel)
#include "Folder.h"                      // FFolder (Engine)
#include "EditorActorFolders.h"          // FActorFolders (UnrealEd)
#endif

namespace
{
    // ---- small JSON helpers (TU-local; prefixed to stay unique in the unity build) ------------
    FString MCPAnimSkel_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"status":"error","message":...}
    FString MCPAnimSkel_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("message"), Message);
        return MCPAnimSkel_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPAnimSkel_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

#if WITH_EDITOR
    // Total registered slots across all groups (the runtime SlotToGroupNameMap is private, so sum
    // the serialized FAnimSlotGroup::SlotNames instead — matches map cardinality post-BuildSlotToGroupMap).
    int32 MCPAnimSkel_TotalSlotCount(USkeleton* Skeleton)
    {
        int32 Count = 0;
        for (const FAnimSlotGroup& G : Skeleton->GetSlotGroups())
        {
            Count += G.SlotNames.Num();
        }
        return Count;
    }
#endif
}

// ============================================================================================
// ==== (A) USkeleton slot registry (6) =======================================================
// ============================================================================================

FString UMCPReflectionLibrary::GetSkeletonSlotsJson(USkeleton* Skeleton)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    TArray<TSharedPtr<FJsonValue>> Groups;
    for (const FAnimSlotGroup& G : Skeleton->GetSlotGroups()) // VERIFY vs engine source: const TArray<FAnimSlotGroup>& GetSlotGroups() const
    {
        TSharedRef<FJsonObject> GObj = MakeShared<FJsonObject>();
        GObj->SetStringField(TEXT("group_name"), G.GroupName.ToString()); // VERIFY: FAnimSlotGroup::GroupName (public UPROPERTY)
        TArray<TSharedPtr<FJsonValue>> Slots;
        for (const FName& S : G.SlotNames) // VERIFY: FAnimSlotGroup::SlotNames (public UPROPERTY TArray<FName>)
        {
            Slots.Add(MakeShared<FJsonValueString>(S.ToString()));
        }
        GObj->SetArrayField(TEXT("slots"), Slots);
        GObj->SetNumberField(TEXT("slot_count"), G.SlotNames.Num());
        Groups.Add(MakeShared<FJsonValueObject>(GObj));
    }
    Root->SetArrayField(TEXT("groups"), Groups);
    Root->SetNumberField(TEXT("group_count"), Skeleton->GetSlotGroups().Num());
    Root->SetNumberField(TEXT("slot_count"), MCPAnimSkel_TotalSlotCount(Skeleton));
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::AddSkeletonSlot(USkeleton* Skeleton, const FString& SlotName, const FString& GroupName)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    if (SlotName.IsEmpty()) { return MCPAnimSkel_Err(TEXT("slot_name is required")); }
    const FName SlotFName(*SlotName);
    // Empty group -> the engine default group (FAnimSlotGroup::DefaultGroupName).
    const FName GroupFName = GroupName.IsEmpty() ? FAnimSlotGroup::DefaultGroupName : FName(*GroupName); // VERIFY: static ENGINE_API const FName DefaultGroupName

    // Capture prior state for a reversible inverse: if the slot already existed (possibly in a
    // different group), undo restores its prior group; otherwise undo removes the slot.
    const bool bExisted = Skeleton->ContainsSlotName(SlotFName); // VERIFY: bool ContainsSlotName(const FName&) const
    const FName PriorGroup = bExisted ? Skeleton->GetSlotGroupName(SlotFName) : NAME_None; // VERIFY: FName GetSlotGroupName(const FName&) const

    Skeleton->Modify();
    // SetSlotGroupName registers the slot AND creates the group if missing (handles both add + regroup).
    Skeleton->SetSlotGroupName(SlotFName, GroupFName); // VERIFY: void SetSlotGroupName(const FName& InSlotName, const FName& InGroupName)
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("slot_name"), SlotName);
    Root->SetStringField(TEXT("group"), GroupFName.ToString());
    Root->SetBoolField(TEXT("existed"), bExisted);
    Root->SetStringField(TEXT("prior_group"), PriorGroup.ToString());
    Root->SetNumberField(TEXT("slot_count"), MCPAnimSkel_TotalSlotCount(Skeleton));
    Root->SetNumberField(TEXT("group_count"), Skeleton->GetSlotGroups().Num());
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::RemoveSkeletonSlot(USkeleton* Skeleton, const FString& SlotName)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    if (SlotName.IsEmpty()) { return MCPAnimSkel_Err(TEXT("slot_name is required")); }
    const FName SlotFName(*SlotName);
    if (!Skeleton->ContainsSlotName(SlotFName)) { return MCPAnimSkel_Err(FString::Printf(TEXT("slot not found: %s"), *SlotName)); }
    // Capture prior group so undo re-adds the slot into the same group.
    const FName PriorGroup = Skeleton->GetSlotGroupName(SlotFName);

    Skeleton->Modify();
    Skeleton->RemoveSlotName(SlotFName); // VERIFY: void RemoveSlotName(const FName&) — guarded by our ContainsSlotName check above
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("slot_name"), SlotName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("prior_group"), PriorGroup.ToString());
    Root->SetNumberField(TEXT("slot_count"), MCPAnimSkel_TotalSlotCount(Skeleton));
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::RenameSkeletonSlot(USkeleton* Skeleton, const FString& OldName, const FString& NewName)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    if (OldName.IsEmpty() || NewName.IsEmpty()) { return MCPAnimSkel_Err(TEXT("old_name and new_name are required")); }
    const FName OldFName(*OldName);
    const FName NewFName(*NewName);
    // USkeleton::RenameSlotName does check(ContainsSlotName(OldName)) -> guard to avoid an assert-crash.
    if (!Skeleton->ContainsSlotName(OldFName)) { return MCPAnimSkel_Err(FString::Printf(TEXT("slot not found: %s"), *OldName)); }
    if (OldFName == NewFName) { return MCPAnimSkel_Err(TEXT("new_name equals old_name")); }
    if (Skeleton->ContainsSlotName(NewFName)) { return MCPAnimSkel_Err(FString::Printf(TEXT("a slot named '%s' already exists"), *NewName)); }
    const FName Group = Skeleton->GetSlotGroupName(OldFName); // preserved by RenameSlotName; captured for the report

    Skeleton->Modify();
    Skeleton->RenameSlotName(OldFName, NewFName); // VERIFY: void RenameSlotName(const FName& OldName, const FName& NewName)
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("old_name"), OldName);
    Root->SetStringField(TEXT("new_name"), NewName);
    Root->SetStringField(TEXT("group"), Group.ToString());
    Root->SetNumberField(TEXT("slot_count"), MCPAnimSkel_TotalSlotCount(Skeleton));
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::AddSkeletonSlotGroup(USkeleton* Skeleton, const FString& GroupName)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    if (GroupName.IsEmpty()) { return MCPAnimSkel_Err(TEXT("group_name is required")); }
    const FName GroupFName(*GroupName);

    Skeleton->Modify();
    const bool bAdded = Skeleton->AddSlotGroupName(GroupFName); // VERIFY: bool AddSlotGroupName(const FName&) — true if created, false if already exists
    if (bAdded)
    {
        Skeleton->PostEditChange();
        Skeleton->MarkPackageDirty();
    }

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("group"), GroupName);
    Root->SetBoolField(TEXT("added"), bAdded);
    Root->SetNumberField(TEXT("group_count"), Skeleton->GetSlotGroups().Num());
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::RemoveSkeletonSlotGroup(USkeleton* Skeleton, const FString& GroupName)
{
#if WITH_EDITOR
    if (!Skeleton) { return MCPAnimSkel_Err(TEXT("null skeleton")); }
    if (GroupName.IsEmpty()) { return MCPAnimSkel_Err(TEXT("group_name is required")); }
    const FName GroupFName(*GroupName);
    // USkeleton::RemoveSlotGroup dereferences FindAnimSlotGroup() WITHOUT a null check -> guard here.
    const FAnimSlotGroup* Existing = Skeleton->FindAnimSlotGroup(GroupFName); // VERIFY: const FAnimSlotGroup* FindAnimSlotGroup(const FName&) const
    if (!Existing) { return MCPAnimSkel_Err(FString::Printf(TEXT("slot group not found: %s"), *GroupName)); }
    // Capture the group's slot names so undo re-creates the group and its slots.
    TArray<TSharedPtr<FJsonValue>> PriorSlots;
    for (const FName& S : Existing->SlotNames)
    {
        PriorSlots.Add(MakeShared<FJsonValueString>(S.ToString()));
    }

    Skeleton->Modify();
    Skeleton->RemoveSlotGroup(GroupFName); // VERIFY: void RemoveSlotGroup(const FName& InSlotGroupName) — guarded by FindAnimSlotGroup above
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("group"), GroupName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetArrayField(TEXT("prior_slots"), PriorSlots);
    Root->SetNumberField(TEXT("group_count"), Skeleton->GetSlotGroups().Num());
    Root->SetNumberField(TEXT("slot_count"), MCPAnimSkel_TotalSlotCount(Skeleton));
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

// ============================================================================================
// ==== (B) Editor World-Outliner folders (2) =================================================
// ============================================================================================

FString UMCPReflectionLibrary::CreateOutlinerFolder(const FString& FolderPath)
{
#if WITH_EDITOR
    if (!GEditor) { return MCPAnimSkel_Err(TEXT("no GEditor")); }
    if (FolderPath.IsEmpty()) { return MCPAnimSkel_Err(TEXT("folder_path is required")); }
    UWorld* World = GEditor->GetEditorWorldContext().World();
    if (!World) { return MCPAnimSkel_Err(TEXT("no editor world")); }
    // Same root-object idiom the outliner uses (FActorFolderTypedElementInterfaces.cpp): prefer the
    // current level's folder root object, fall back to the world root folder's.
    const FFolder::FRootObject RootObject = FFolder::GetOptionalFolderRootObject(World->GetCurrentLevel())
        .Get(FFolder::GetWorldRootFolder(World).GetRootObject()); // VERIFY: GetOptionalFolderRootObject / GetWorldRootFolder (Folder.h)
    const FFolder Folder(RootObject, FName(*FolderPath)); // VERIFY: FFolder(const FRootObject&, const FName&)

    const bool bAlready = FActorFolders::Get().ContainsFolder(*World, Folder); // VERIFY: bool ContainsFolder(UWorld&, const FFolder&)
    bool bCreated = false;
    if (!bAlready)
    {
        bCreated = FActorFolders::Get().CreateFolder(*World, Folder); // VERIFY: bool CreateFolder(UWorld&, const FFolder&)
    }

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("world"), World->GetName());
    Root->SetStringField(TEXT("folder_path"), FolderPath);
    Root->SetBoolField(TEXT("created"), bCreated);
    Root->SetBoolField(TEXT("existed"), bAlready);
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}

FString UMCPReflectionLibrary::DeleteOutlinerFolder(const FString& FolderPath)
{
#if WITH_EDITOR
    if (!GEditor) { return MCPAnimSkel_Err(TEXT("no GEditor")); }
    if (FolderPath.IsEmpty()) { return MCPAnimSkel_Err(TEXT("folder_path is required")); }
    UWorld* World = GEditor->GetEditorWorldContext().World();
    if (!World) { return MCPAnimSkel_Err(TEXT("no editor world")); }
    const FFolder::FRootObject RootObject = FFolder::GetOptionalFolderRootObject(World->GetCurrentLevel())
        .Get(FFolder::GetWorldRootFolder(World).GetRootObject());
    const FFolder Folder(RootObject, FName(*FolderPath));

    if (!FActorFolders::Get().ContainsFolder(*World, Folder))
    {
        return MCPAnimSkel_Err(FString::Printf(TEXT("folder not found: %s"), *FolderPath));
    }
    FActorFolders::Get().DeleteFolder(*World, Folder); // VERIFY: void DeleteFolder(UWorld&, const FFolder&)

    TSharedRef<FJsonObject> Root = MCPAnimSkel_Ok();
    Root->SetStringField(TEXT("world"), World->GetName());
    Root->SetStringField(TEXT("folder_path"), FolderPath);
    Root->SetBoolField(TEXT("deleted"), true);
    return MCPAnimSkel_SerializeJson(Root);
#else
    return MCPAnimSkel_Err(TEXT("editor-only"));
#endif
}
