// ============================================================================
// MCPReflection_GameplayTagWrite.cpp — C++ #55. MCP_TOOLING_GAPS (Project_Aimon
// G-01): set an FGameplayTag / FGameplayTagContainer UPROPERTY on any object from
// string tag name(s). SELF-CONTAINED translation unit.
//
// WHY: in the 5.8 Python API there is NO way to build a REGISTERED gameplay tag
// from a string — GameplayTagLibrary.request_gameplay_tag isn't exposed, the
// FGameplayTag ctor/tag_name are rejected/read-only, and set_object_property
// nativizes a struct from a Python *sequence* (not ExportText) so a tag-container
// literal fails. So tag-typed fields (species DefaultTypes, a creature's
// StatsComponent.Types, a move's MoveTag) can't be authored from Python. These
// two handlers build the tags in C++ via FGameplayTag::RequestGameplayTag and
// assign the property by reflection — generic (any UObject), not Aimon-specific.
//
//   #55 SetGameplayTagProperty(obj, prop, tagName)        -> a single FGameplayTag field
//   #55 SetGameplayTagContainerProperty(obj, prop, json)  -> an FGameplayTagContainer field (JSON array or CSV)
//
// LINK-RISK: GameplayTags module is already a Build.cs dep -> NO Build.cs change.
// Reports which names RESOLVED vs were MISSING (unregistered) so a typo/undefined
// tag is visible instead of silently dropped. Marks the package dirty + PostEditChange.
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"
#include "UObject/UnrealType.h"
#include "UObject/Class.h"

#include "GameplayTagContainer.h"   // FGameplayTag / FGameplayTagContainer / RequestGameplayTag

namespace
{
    FString GTW_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString GTW_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return GTW_Serialize(O);
    }

    // Parse a tag-name list from either a JSON array (["A","B"]) or a CSV/space list ("A, B").
    void GTW_ParseNames(const FString& In, TArray<FString>& Out)
    {
        const FString Trimmed = In.TrimStartAndEnd();
        if (Trimmed.StartsWith(TEXT("[")))
        {
            TArray<TSharedPtr<FJsonValue>> Arr;
            TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(Trimmed);
            if (FJsonSerializer::Deserialize(R, Arr))
            {
                for (const TSharedPtr<FJsonValue>& V : Arr)
                {
                    FString S;
                    if (V.IsValid() && V->TryGetString(S))
                    {
                        S = S.TrimStartAndEnd();
                        if (!S.IsEmpty()) Out.Add(S);
                    }
                }
                return;
            }
        }
        // CSV fallback (also handles a bare single name).
        TArray<FString> Parts;
        Trimmed.ParseIntoArray(Parts, TEXT(","), true);
        for (FString S : Parts)
        {
            S = S.TrimStartAndEnd();
            if (!S.IsEmpty()) Out.Add(S);
        }
    }

    // Find a struct UPROPERTY (by name, inherited included) and confirm its struct type.
    FStructProperty* GTW_FindStructProp(UObject* Target, const FString& PropertyName, UScriptStruct* Expected, FString& OutErr)
    {
        if (!Target) { OutErr = TEXT("null target object"); return nullptr; }
        FProperty* Prop = FindFProperty<FProperty>(Target->GetClass(), *PropertyName);
        if (!Prop)
        {
            OutErr = FString::Printf(TEXT("no property '%s' on %s"), *PropertyName, *Target->GetClass()->GetName());
            return nullptr;
        }
        FStructProperty* SP = CastField<FStructProperty>(Prop);
        if (!SP || SP->Struct != Expected)
        {
            OutErr = FString::Printf(TEXT("property '%s' is not a %s (it is %s)"),
                *PropertyName, *Expected->GetName(),
                SP && SP->Struct ? *SP->Struct->GetName() : *Prop->GetClass()->GetName());
            return nullptr;
        }
        return SP;
    }

    void GTW_Persist(UObject* Target, FProperty* Prop)
    {
        Target->Modify();
#if WITH_EDITOR
        FPropertyChangedEvent Evt(Prop);
        Target->PostEditChangeProperty(Evt);
#endif
        Target->MarkPackageDirty();
    }
}

// ---- #55  set_gameplay_tag_property (single FGameplayTag field) ------------------
FString UMCPReflectionLibrary::SetGameplayTagProperty(UObject* Target, const FString& PropertyName, const FString& TagName)
{
    FString Err;
    FStructProperty* SP = GTW_FindStructProp(Target, PropertyName, FGameplayTag::StaticStruct(), Err);
    if (!SP) return GTW_Error(Err);

    const FString Clean = TagName.TrimStartAndEnd();
    const FGameplayTag Tag = Clean.IsEmpty()
        ? FGameplayTag()   // empty string clears the tag
        : FGameplayTag::RequestGameplayTag(FName(*Clean), /*ErrorIfNotFound*/false);
    if (!Clean.IsEmpty() && !Tag.IsValid())
    {
        return GTW_Error(FString::Printf(TEXT("gameplay tag '%s' is not registered (define it in the project's tag list first)"), *Clean));
    }

    FGameplayTag* Dest = SP->ContainerPtrToValuePtr<FGameplayTag>(Target);
    *Dest = Tag;
    GTW_Persist(Target, SP);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("object"), Target->GetPathName());
    O->SetStringField(TEXT("property"), PropertyName);
    O->SetStringField(TEXT("tag"), Tag.IsValid() ? Tag.ToString() : TEXT(""));
    return GTW_Serialize(O);
}

// ---- #55  set_gameplay_tag_container_property (FGameplayTagContainer field) ------
FString UMCPReflectionLibrary::SetGameplayTagContainerProperty(UObject* Target, const FString& PropertyName, const FString& TagsJson)
{
    FString Err;
    FStructProperty* SP = GTW_FindStructProp(Target, PropertyName, FGameplayTagContainer::StaticStruct(), Err);
    if (!SP) return GTW_Error(Err);

    TArray<FString> Names;
    GTW_ParseNames(TagsJson, Names);

    FGameplayTagContainer NewContainer;
    TArray<TSharedPtr<FJsonValue>> Applied, Missing;
    for (const FString& N : Names)
    {
        const FGameplayTag Tag = FGameplayTag::RequestGameplayTag(FName(*N), /*ErrorIfNotFound*/false);
        if (Tag.IsValid())
        {
            NewContainer.AddTag(Tag);
            Applied.Add(MakeShared<FJsonValueString>(Tag.ToString()));
        }
        else
        {
            Missing.Add(MakeShared<FJsonValueString>(N));
        }
    }

    FGameplayTagContainer* Dest = SP->ContainerPtrToValuePtr<FGameplayTagContainer>(Target);
    *Dest = NewContainer;   // full replace (empty list clears the container)
    GTW_Persist(Target, SP);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), Missing.Num() > 0 ? TEXT("partial") : TEXT("ok"));
    O->SetStringField(TEXT("object"), Target->GetPathName());
    O->SetStringField(TEXT("property"), PropertyName);
    O->SetArrayField(TEXT("applied"), Applied);
    O->SetArrayField(TEXT("missing"), Missing);
    O->SetNumberField(TEXT("count"), Applied.Num());
    return GTW_Serialize(O);
}
