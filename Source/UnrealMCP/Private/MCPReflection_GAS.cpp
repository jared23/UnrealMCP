// UnrealMCP reflection — GAS RUNTIME reader (C++ #22 2026-08-19).
//
// get_ability_system_info reads a LIVE UAbilitySystemComponent (attributes, owned tags, granted abilities,
// active-effect count) on a given actor. AbilitySystemBlueprintLibrary / AbilitySystemGlobals are NOT Python-
// exposed and the ASC exposes only get_all_attributes to Python, so this must be C++. Also a small test helper
// (AddTestAbilitySystemComponent) so a live ASC can be created + verified over the bridge (the project's actors
// have none). Build.cs += "GameplayAbilities" (plugin already enabled in the .uproject).
//
// Member DEFINITIONS for UMCPReflectionLibrary; UFUNCTION decls live in MCPReflectionLibrary.h. Anon-namespace
// helpers are prefixed MCPGas_ to stay unique in the unity build.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "GameFramework/Actor.h"
#include "AbilitySystemComponent.h"      // UAbilitySystemComponent (attributes/tags/abilities/effects/InitAbilityActorInfo)
#include "AbilitySystemGlobals.h"        // UAbilitySystemGlobals::GetAbilitySystemComponentFromActor
#include "AttributeSet.h"                // FGameplayAttribute
#include "GameplayEffect.h"              // FGameplayEffectQuery
#include "GameplayEffectTypes.h"         // FActiveGameplayEffectHandle
#include "GameplayTagContainer.h"        // FGameplayTag / FGameplayTagContainer
#include "Abilities/GameplayAbility.h"   // UGameplayAbility (spec.Ability->GetClass())
#include "GameplayAbilitySpec.h"         // FGameplayAbilitySpec

namespace
{
    FString MCPGas_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }
    FString MCPGas_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPGas_SerializeJson(Root);
    }
    TSharedRef<FJsonObject> MCPGas_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }
}

// ---- get_ability_system_info: read a live ASC on an actor ----------------------------------------
FString UMCPReflectionLibrary::GetAbilitySystemInfoJson(AActor* Actor)
{
    if (!Actor) { return MCPGas_Error(TEXT("null actor")); }
    UAbilitySystemComponent* ASC = UAbilitySystemGlobals::GetAbilitySystemComponentFromActor(Actor);

    TSharedRef<FJsonObject> Root = MCPGas_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetStringField(TEXT("actor_class"), Actor->GetClass()->GetName());
    if (!ASC)
    {
        Root->SetBoolField(TEXT("has_asc"), false);
        Root->SetStringField(TEXT("note"), TEXT("actor has no AbilitySystemComponent (no IAbilitySystemInterface / component)"));
        return MCPGas_SerializeJson(Root);
    }
    Root->SetBoolField(TEXT("has_asc"), true);

    // Attributes (name / base / current)
    TArray<TSharedPtr<FJsonValue>> AttrArr;
    TArray<FGameplayAttribute> Attrs;
    ASC->GetAllAttributes(Attrs);
    for (const FGameplayAttribute& A : Attrs)
    {
        if (!A.IsValid()) { continue; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), A.GetName());
        J->SetNumberField(TEXT("base"), ASC->GetNumericAttributeBase(A));
        J->SetNumberField(TEXT("current"), ASC->GetNumericAttribute(A));
        AttrArr.Add(MakeShared<FJsonValueObject>(J));
    }

    // Owned gameplay tags
    TArray<TSharedPtr<FJsonValue>> TagArr;
    FGameplayTagContainer OwnedTags;
    ASC->GetOwnedGameplayTags(OwnedTags);
    for (const FGameplayTag& T : OwnedTags)
    {
        TagArr.Add(MakeShared<FJsonValueString>(T.ToString()));
    }

    // Granted (activatable) abilities
    TArray<TSharedPtr<FJsonValue>> AbilArr;
    for (const FGameplayAbilitySpec& Spec : ASC->GetActivatableAbilities())
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        const UGameplayAbility* Ab = Spec.Ability;
        J->SetStringField(TEXT("ability"), Ab ? Ab->GetClass()->GetName() : FString());
        J->SetNumberField(TEXT("level"), Spec.Level);
        J->SetBoolField(TEXT("active"), Spec.IsActive());
        AbilArr.Add(MakeShared<FJsonValueObject>(J));
    }

    const int32 ActiveEffects = ASC->GetActiveEffects(FGameplayEffectQuery()).Num();

    Root->SetArrayField(TEXT("attributes"), AttrArr);
    Root->SetArrayField(TEXT("owned_tags"), TagArr);
    Root->SetArrayField(TEXT("abilities"), AbilArr);
    Root->SetNumberField(TEXT("attribute_count"), AttrArr.Num());
    Root->SetNumberField(TEXT("owned_tag_count"), TagArr.Num());
    Root->SetNumberField(TEXT("ability_count"), AbilArr.Num());
    Root->SetNumberField(TEXT("active_effect_count"), ActiveEffects);
    return MCPGas_SerializeJson(Root);
}

// ---- test helper: give an actor a minimal live ASC so get_ability_system_info can be verified ----
// Adds + registers + InitAbilityActorInfo's a UAbilitySystemComponent (if the actor has none), and optionally
// adds a loose gameplay tag (if the tag is registered). Test scaffolding for the PIE verification path.
FString UMCPReflectionLibrary::AddTestAbilitySystemComponent(AActor* Actor, const FString& LooseTag)
{
    if (!Actor) { return MCPGas_Error(TEXT("null actor")); }
    UAbilitySystemComponent* ASC = UAbilitySystemGlobals::GetAbilitySystemComponentFromActor(Actor);
    bool bCreated = false;
    if (!ASC)
    {
        ASC = NewObject<UAbilitySystemComponent>(Actor);
        if (!ASC) { return MCPGas_Error(TEXT("failed to create AbilitySystemComponent")); }
        ASC->RegisterComponent();
        bCreated = true;
    }
    ASC->InitAbilityActorInfo(Actor, Actor);

    bool bTagAdded = false;
    if (!LooseTag.IsEmpty())
    {
        const FGameplayTag T = FGameplayTag::RequestGameplayTag(FName(*LooseTag), /*ErrorIfNotFound*/ false);
        if (T.IsValid())
        {
            ASC->AddLooseGameplayTag(T);
            bTagAdded = true;
        }
    }

    TSharedRef<FJsonObject> Root = MCPGas_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetBoolField(TEXT("created"), bCreated);
    Root->SetBoolField(TEXT("tag_added"), bTagAdded);
    Root->SetBoolField(TEXT("has_asc"), true);
    return MCPGas_SerializeJson(Root);
}
