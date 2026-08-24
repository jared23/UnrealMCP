// UnrealMCP reflection — Behavior-Tree RUNTIME reader/control (C++ #23 2026-08-19).
//
// get_bt_runtime / control_bt_runtime / set_bt_dynamic_subtree operate on a LIVE UBehaviorTreeComponent on a
// possessed AIController in PIE. Python cannot spawn into the PIE world (no World.spawn_actor exposed) nor read a
// BTC's active node / blackboard, so this is C++. SpawnBehaviorTreeTestActor is the test fixture (spawn an
// AIController into the game world + RunBehaviorTree). AIModule + GameplayTags are already Build.cs deps.
//
// Member DEFINITIONS for UMCPReflectionLibrary; UFUNCTION decls in MCPReflectionLibrary.h. Anon-namespace helpers
// prefixed MCPBT_ to stay unique in the unity build.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Engine/World.h"
#include "GameFramework/Actor.h"
#include "GameFramework/Pawn.h"
#include "AIController.h"
#include "BehaviorTree/BehaviorTree.h"
#include "BehaviorTree/BehaviorTreeComponent.h"
#include "BehaviorTree/BlackboardComponent.h"
#include "BehaviorTree/BTNode.h"
#include "GameplayTagContainer.h"

namespace
{
    FString MCPBT_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }
    FString MCPBT_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPBT_SerializeJson(Root);
    }
    TSharedRef<FJsonObject> MCPBT_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }
    // Resolve the AIController + its UBehaviorTreeComponent from an actor (the actor may BE an AIController,
    // or a pawn whose controller is one).
    UBehaviorTreeComponent* MCPBT_GetBTC(AActor* Actor, AAIController*& OutAI)
    {
        OutAI = nullptr;
        if (!Actor) { return nullptr; }
        AAIController* AI = Cast<AAIController>(Actor);
        if (!AI)
        {
            if (APawn* P = Cast<APawn>(Actor)) { AI = Cast<AAIController>(P->GetController()); }
        }
        OutAI = AI;
        if (!AI) { return nullptr; }
        return Cast<UBehaviorTreeComponent>(AI->GetBrainComponent());
    }
}

// ---- get_bt_runtime: inspect a running BehaviorTreeComponent ------------------------------------
FString UMCPReflectionLibrary::GetBehaviorTreeRuntimeJson(AActor* Actor, bool bIncludeBlackboard)
{
#if WITH_EDITOR
    if (!Actor) { return MCPBT_Error(TEXT("null actor")); }
    AAIController* AI = nullptr;
    UBehaviorTreeComponent* BTC = MCPBT_GetBTC(Actor, AI);

    TSharedRef<FJsonObject> Root = MCPBT_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetStringField(TEXT("controller"), AI ? AI->GetName() : FString());
    if (!AI)
    {
        Root->SetBoolField(TEXT("has_bt_component"), false);
        Root->SetStringField(TEXT("note"), TEXT("actor is not an AIController and has no AIController possessing it"));
        return MCPBT_SerializeJson(Root);
    }
    if (!BTC)
    {
        Root->SetBoolField(TEXT("has_bt_component"), false);
        Root->SetStringField(TEXT("note"), TEXT("AIController has no UBehaviorTreeComponent (no BT running)"));
        return MCPBT_SerializeJson(Root);
    }

    Root->SetBoolField(TEXT("has_bt_component"), true);
    Root->SetBoolField(TEXT("is_running"), BTC->IsRunning());
    const UBTNode* Active = BTC->GetActiveNode();
    Root->SetStringField(TEXT("active_node"), Active ? Active->GetNodeName() : FString());
    Root->SetStringField(TEXT("active_tasks"), BTC->DescribeActiveTasks());
    Root->SetStringField(TEXT("active_trees"), BTC->DescribeActiveTrees());

    if (bIncludeBlackboard)
    {
        if (UBlackboardComponent* BB = AI->GetBlackboardComponent())
        {
            Root->SetNumberField(TEXT("blackboard_key_count"), BB->GetNumKeys());
            Root->SetStringField(TEXT("blackboard"), BB->GetDebugInfoString(EBlackboardDescription::KeyWithValue));
        }
        else
        {
            Root->SetStringField(TEXT("blackboard"), TEXT("(no blackboard component)"));
        }
    }
    return MCPBT_SerializeJson(Root);
#else
    return MCPBT_Error(TEXT("editor-only"));
#endif
}

// ---- control_bt_runtime: start/stop/pause/resume a running tree --------------------------------
FString UMCPReflectionLibrary::ControlBehaviorTreeRuntimeJson(AActor* Actor, const FString& Action)
{
#if WITH_EDITOR
    if (!Actor) { return MCPBT_Error(TEXT("null actor")); }
    AAIController* AI = nullptr;
    UBehaviorTreeComponent* BTC = MCPBT_GetBTC(Actor, AI);
    if (!BTC) { return MCPBT_Error(TEXT("actor has no live UBehaviorTreeComponent (needs a possessing AIController running a BT)")); }

    const FString A = Action.ToLower();
    if (A == TEXT("stop"))            { BTC->StopTree(EBTStopMode::Safe); }
    else if (A == TEXT("start") || A == TEXT("restart")) { BTC->RestartTree(); }
    else if (A == TEXT("pause"))      { BTC->PauseLogic(TEXT("MCP")); }
    else if (A == TEXT("resume"))     { BTC->ResumeLogic(TEXT("MCP")); }
    else { return MCPBT_Error(FString::Printf(TEXT("unknown action '%s' (start|stop|pause|resume)"), *Action)); }

    TSharedRef<FJsonObject> Root = MCPBT_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetStringField(TEXT("action"), A);
    Root->SetBoolField(TEXT("is_running"), BTC->IsRunning());
    return MCPBT_SerializeJson(Root);
#else
    return MCPBT_Error(TEXT("editor-only"));
#endif
}

// ---- set_bt_dynamic_subtree: inject a subtree at a gameplay tag ---------------------------------
FString UMCPReflectionLibrary::SetBehaviorTreeDynamicSubtreeJson(AActor* Actor, const FString& InjectTag, UBehaviorTree* Subtree)
{
#if WITH_EDITOR
    if (!Actor) { return MCPBT_Error(TEXT("null actor")); }
    AAIController* AI = nullptr;
    UBehaviorTreeComponent* BTC = MCPBT_GetBTC(Actor, AI);
    if (!BTC) { return MCPBT_Error(TEXT("actor has no live UBehaviorTreeComponent")); }

    const FGameplayTag Tag = FGameplayTag::RequestGameplayTag(FName(*InjectTag), /*ErrorIfNotFound*/ false);
    if (!Tag.IsValid()) { return MCPBT_Error(FString::Printf(TEXT("inject tag '%s' is not a registered gameplay tag"), *InjectTag)); }
    BTC->SetDynamicSubtree(Tag, Subtree);

    TSharedRef<FJsonObject> Root = MCPBT_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetStringField(TEXT("inject_tag"), Tag.ToString());
    Root->SetStringField(TEXT("subtree"), Subtree ? Subtree->GetPathName() : FString());
    return MCPBT_SerializeJson(Root);
#else
    return MCPBT_Error(TEXT("editor-only"));
#endif
}

// ---- test fixture: spawn an AIController into a world + run a BT --------------------------------
// WorldContextActor is any actor in the target (PIE game) world; the new controller spawns into that world.
FString UMCPReflectionLibrary::SpawnBehaviorTreeTestActor(AActor* WorldContextActor, UBehaviorTree* BT)
{
#if WITH_EDITOR
    if (!WorldContextActor) { return MCPBT_Error(TEXT("null world-context actor")); }
    if (!BT) { return MCPBT_Error(TEXT("null behavior tree")); }
    UWorld* World = WorldContextActor->GetWorld();
    if (!World) { return MCPBT_Error(TEXT("could not resolve world from context actor")); }

    FActorSpawnParameters Params;
    Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
    AAIController* AI = World->SpawnActor<AAIController>(AAIController::StaticClass(), FVector(0.f, 0.f, 300.f), FRotator::ZeroRotator, Params);
    if (!AI) { return MCPBT_Error(TEXT("failed to spawn AIController")); }

    const bool bRan = AI->RunBehaviorTree(BT);

    TSharedRef<FJsonObject> Root = MCPBT_Ok();
    Root->SetStringField(TEXT("controller"), AI->GetName());
    Root->SetStringField(TEXT("behavior_tree"), BT->GetName());
    Root->SetBoolField(TEXT("ran"), bRan);
    return MCPBT_SerializeJson(Root);
#else
    return MCPBT_Error(TEXT("editor-only"));
#endif
}
