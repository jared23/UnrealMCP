// ============================================================================
// MCPReflection_BlackboardKeyEnum.cpp — C++ #57. MCP_TOOLING_GAPS (Project_Aimon):
// bind a Blackboard Enum key to its UEnum. SELF-CONTAINED translation unit.
//
// WHY: UBlackboardKeyType_Enum::EnumType / EnumName aren't reachable from the 5.8
// Python API (get/set_editor_property('enum_type') -> "Failed to find property";
// the KeyType is an instanced sub-object Python can't navigate), so an Enum-typed
// blackboard key works functionally (byte storage) but shows raw bytes in the editor
// instead of enum names. This sets EnumType on an existing Enum key by reflection.
//
//   #57 SetBlackboardKeyEnum(bb, keyName, enumPathOrName) -> bind the key's UEnum
//
// LINK-RISK: AIModule already a Build.cs dep -> NO Build.cs change. The members are
// public C++ (UBlackboardKeyType_Enum::EnumType/EnumName/bIsEnumNameValid). Marks the
// asset dirty (caller saves).
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "BehaviorTree/BlackboardData.h"
#include "BehaviorTree/Blackboard/BlackboardKeyType_Enum.h"

namespace
{
    FString BBE_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString BBE_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return BBE_Serialize(O);
    }

    // Resolve a UEnum from a full path (/Script/Module.EEnum for native, /Game/.../Enum.Enum for a
    // UserDefinedEnum asset) or a bare type name (EMainMentalState).
    UEnum* BBE_ResolveEnum(const FString& Ref)
    {
        UEnum* Enum = nullptr;
        if (Ref.Contains(TEXT(".")) || Ref.Contains(TEXT("/")))
        {
            Enum = LoadObject<UEnum>(nullptr, *Ref);
        }
        if (!Enum)
        {
            Enum = FindFirstObject<UEnum>(*Ref, EFindFirstObjectOptions::NativeFirst);
        }
        return Enum;
    }
}

// ---- #57  set_blackboard_key_enum -----------------------------------------------
FString UMCPReflectionLibrary::SetBlackboardKeyEnum(UBlackboardData* Blackboard, const FString& KeyName, const FString& EnumRef)
{
    if (!Blackboard) return BBE_Error(TEXT("null BlackboardData"));

    UEnum* Enum = BBE_ResolveEnum(EnumRef);
    if (!Enum)
    {
        return BBE_Error(FString::Printf(TEXT("could not resolve UEnum '%s' (pass a path like /Script/Project_Aimon.EMainMentalState or /Game/.../MyEnum, or a bare UENUM type name)"), *EnumRef));
    }

    // Find the entry among this asset's own keys (not ParentKeys — those belong to a parent asset).
    const FName KeyFName(*KeyName);
    const FBlackboardEntry* FoundEntry = nullptr;
    for (const FBlackboardEntry& E : Blackboard->GetKeys())
    {
        if (E.EntryName == KeyFName) { FoundEntry = &E; break; }
    }
    if (!FoundEntry)
    {
        return BBE_Error(FString::Printf(TEXT("no key '%s' on %s (checked this asset's own keys; parent keys live on the parent BlackboardData)"),
            *KeyName, *Blackboard->GetName()));
    }

    UBlackboardKeyType_Enum* EnumKey = Cast<UBlackboardKeyType_Enum>(FoundEntry->KeyType);
    if (!EnumKey)
    {
        return BBE_Error(FString::Printf(TEXT("key '%s' is not an Enum key (its KeyType is %s)"),
            *KeyName, FoundEntry->KeyType ? *FoundEntry->KeyType->GetClass()->GetName() : TEXT("null")));
    }

    // Bind via EnumType and disable the EnumName C++-override so the drawer/DescribeValue use EnumType
    // (works for both a UserDefinedEnum asset and a native UENUM object).
    EnumKey->Modify();
    EnumKey->EnumType = Enum;
    EnumKey->EnumName = Enum->GetName();      // informational; override is disabled below
    EnumKey->bIsEnumNameValid = false;

    Blackboard->Modify();
#if WITH_EDITOR
    Blackboard->PostEditChange();
#endif
    Blackboard->MarkPackageDirty();

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("blackboard"), Blackboard->GetPathName());
    O->SetStringField(TEXT("key"), KeyName);
    O->SetStringField(TEXT("enum"), Enum->GetPathName());
    O->SetNumberField(TEXT("enum_entries"), Enum->NumEnums() > 0 ? Enum->NumEnums() - 1 : 0);  // minus hidden _MAX
    return BBE_Serialize(O);
}
