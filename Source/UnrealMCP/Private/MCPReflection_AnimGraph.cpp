// ============================================================================
// MCPReflection_AnimGraph.cpp  —  C++ #17 (HIGH-RISK: AnimBP AnimGraph authoring)
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18, NOT YET COMPILED. **ISOLATED translation unit**
// on purpose: these are the AnimGraph state-machine + nodes/layers authoring
// handlers (COMPLETION_anim_statetree.md, Group C — 13 features). They author
// editor-only EdGraph objects (UAnimGraphNode_*/UAnimStateNode/UAnimStateTransitionNode)
// that live in the **AnimGraph** editor module (+ AnimGraphRuntime for the runtime
// FAnimNode_* structs, + BlueprintGraph for UK2Node/AnimationGraphSchema:UEdGraphSchema_K2).
//
// >>> TOP LINK RISK (Niagara/BehaviorTree export-patch precedent) <<<
//   The AnimGraph editor node classes are declared UCLASS(MinimalAPI). MinimalAPI
//   exports StaticClass() (so NewObject<T>() / FGraphNodeCreator<T> link) but NOT
//   the C++ ctor/most methods. This file is written to depend ONLY on:
//     (a) StaticClass() of the MinimalAPI classes (exported by MinimalAPI),
//     (b) methods explicitly marked ANIMGRAPH_API (verified in the 5.8 headers),
//     (c) virtual dispatch through UEdGraphNode/UEdGraphSchema base pointers,
//     (d) CoreUObject FProperty reflection + public UPROPERTY member access.
//   The specific ANIMGRAPH_API symbols we call are listed in the RISK ASSESSMENT
//   in the coordinator report. If AnimGraph.dll does not export any one of them
//   for a plugin, THIS FILE FAILS TO LINK — drop just this .cpp and the other C++
//   groups still land. Every such call is tagged "VERIFY export" below.
//
// Build.cs (coordinator adds): + "AnimGraph", "AnimGraphRuntime", "BlueprintGraph".
//   ("AnimationBlueprintEditor" is NOT needed by this file — anim layers are made
//   via FBlueprintEditorUtils::AddDomainSpecificGraph, which is UnrealEd, already a dep.)
//
// All handlers: null-guarded, return {"status":...}+payload JSON (never crash),
// mark the BP structurally modified, and capture prior state for reversible ops.
// Anon-namespace helpers are prefixed MCPAnimGraph_ (unique across the unity build).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

// --- Reflection / core ---
#include "UObject/UnrealType.h"
#include "UObject/Package.h"
#include "Misc/Guid.h"

// --- Blueprint / graph (Engine + UnrealEd + BlueprintGraph) ---
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/MemberReference.h"                 // FMemberReference (Engine)
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"                        // UEdGraphSchema_K2::GN_AnimGraph (BlueprintGraph)
#include "Kismet2/BlueprintEditorUtils.h"           // FBlueprintEditorUtils (UnrealEd)
#include "Kismet2/KismetEditorUtilities.h"          // FKismetEditorUtilities::CreateBlueprint (UnrealEd)

// --- Anim (Engine runtime) ---
#include "Animation/AnimBlueprint.h"                // UAnimBlueprint (Engine)
#include "Animation/AnimLayerInterface.h"           // UAnimLayerInterface (Engine)

// --- AnimGraph editor module (THE risky includes — VERIFY exports) ---
#include "AnimationGraph.h"                          // UAnimationGraph
#include "AnimationGraphSchema.h"                    // UAnimationGraphSchema
#include "AnimGraphNode_Base.h"                      // UAnimGraphNode_Base (SetPinVisibility, ShowPinForProperties, *Function refs)
#include "AnimGraphNode_StateMachine.h"             // UAnimGraphNode_StateMachine (concrete)
#include "AnimGraphNode_StateMachineBase.h"         // UAnimGraphNode_StateMachineBase (EditorStateMachineGraph)
#include "AnimationStateMachineGraph.h"             // UAnimationStateMachineGraph (EntryNode)
#include "AnimationStateMachineSchema.h"            // UAnimationStateMachineSchema
#include "AnimStateNodeBase.h"                       // UAnimStateNodeBase (GetStateName/GetInput/OutputPin/GetTransitionList)
#include "AnimStateNode.h"                           // UAnimStateNode (BoundGraph)
#include "AnimStateTransitionNode.h"                 // UAnimStateTransitionNode (CreateConnections/GetPrevious/GetNextState)
#include "AnimStateEntryNode.h"                      // UAnimStateEntryNode (GetOutputPin/GetOutputNode)

// --- C++ #59 (locomotion authoring): node types + K2 nodes for state anim / rules / speed drive ---
#include "AnimGraphNode_SequencePlayer.h"           // UAnimGraphNode_SequencePlayer (fill a state)
#include "AnimGraphNode_BlendSpacePlayer.h"         // UAnimGraphNode_BlendSpacePlayer (#60: blend space in a state)
#include "AnimGraphNode_Slot.h"                      // UAnimGraphNode_Slot (#60: montage slot in the AnimGraph)
#include "Animation/BlendSpace.h"                    // UBlendSpace (#60)
#include "AnimGraphNode_RotationOffsetBlendSpace.h" // #61: Aim Offset Player
#include "AnimGraphNode_LayeredBoneBlend.h"          // #61: Layered Blend Per Bone (AddPinToBlendByFilter)
#include "AnimGraphNode_AssetPlayerBase.h"           // #61: SetAnimationAsset (virtual)
#include "Animation/AnimationAsset.h"                // #61: UAnimationAsset
#include "Animation/AnimData/BoneMaskFilter.h"       // #61: FInputBlendPose / FBranchFilter
#include "AnimGraphNode_StateResult.h"              // UAnimGraphNode_StateResult (state's output pose)
#include "AnimGraphNode_Root.h"                     // UAnimGraphNode_Root (AnimGraph Output Pose)
#include "AnimGraphNode_TransitionResult.h"         // UAnimGraphNode_TransitionResult (rule bCanEnterTransition)
#include "Animation/AnimSequenceBase.h"             // UAnimSequenceBase
#include "Animation/AnimInstance.h"                 // UAnimInstance (GetOwningActor / BlueprintUpdateAnimation)
#include "GameFramework/Actor.h"                    // AActor::GetVelocity
#include "Kismet/KismetMathLibrary.h"               // compare funcs + VSize
#include "K2Node_VariableGet.h"                     // rule: variable getter
#include "K2Node_VariableSet.h"                     // speed drive: variable setter
#include "K2Node_CallFunction.h"                    // compare / GetOwningActor / GetVelocity / VSize
#include "K2Node_Event.h"                           // BlueprintUpdateAnimation event
#include "EdGraphSchema_K2.h"                        // PN_Self / PN_Then / PN_Execute (already via GN_AnimGraph use)

namespace
{
    // ---- JSON helpers ------------------------------------------------------
    FString MCPAnimGraph_SerializeObject(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString MCPAnimGraph_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("error"));
        Obj->SetStringField(TEXT("error"), Message);
        return MCPAnimGraph_SerializeObject(Obj);
    }

    TSharedRef<FJsonObject> MCPAnimGraph_Ok()
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("ok"));
        return Obj;
    }

    // ---- Graph discovery ---------------------------------------------------

    // The AnimBlueprint's top-level AnimGraph is a UAnimationGraph in FunctionGraphs,
    // named "AnimGraph" (UEdGraphSchema_K2::GN_AnimGraph). Fall back to the first
    // graph using UAnimationGraphSchema.
    UEdGraph* MCPAnimGraph_FindAnimGraph(UAnimBlueprint* AnimBP)
    {
        if (!AnimBP) { return nullptr; }
        UEdGraph* FirstSchemaMatch = nullptr;
        for (UEdGraph* Graph : AnimBP->FunctionGraphs)
        {
            if (!Graph) { continue; }
            if (Graph->GetFName() == UEdGraphSchema_K2::GN_AnimGraph)
            {
                return Graph;
            }
            if (!FirstSchemaMatch && Graph->Schema &&
                Graph->Schema->IsChildOf(UAnimationGraphSchema::StaticClass()))
            {
                FirstSchemaMatch = Graph;
            }
        }
        return FirstSchemaMatch;
    }

    // Collect the AnimGraph + every layer/function graph + all nested children.
    void MCPAnimGraph_CollectAllGraphs(UAnimBlueprint* AnimBP, TArray<UEdGraph*>& Out)
    {
        if (!AnimBP) { return; }
        TArray<UEdGraph*> Roots;
        Roots.Append(AnimBP->FunctionGraphs);
        Roots.Append(AnimBP->UbergraphPages);
        for (UEdGraph* Root : Roots)
        {
            if (!Root) { continue; }
            Out.AddUnique(Root);
            Root->GetAllChildrenGraphs(Out);
        }
    }

    // Find a state-machine node by its EditorStateMachineGraph name (case-insensitive).
    UAnimGraphNode_StateMachineBase* MCPAnimGraph_FindStateMachine(UAnimBlueprint* AnimBP, const FString& MachineName)
    {
        TArray<UEdGraph*> Graphs;
        MCPAnimGraph_CollectAllGraphs(AnimBP, Graphs);
        for (UEdGraph* Graph : Graphs)
        {
            for (UEdGraphNode* Node : Graph->Nodes)
            {
                UAnimGraphNode_StateMachineBase* SM = Cast<UAnimGraphNode_StateMachineBase>(Node);
                if (SM && SM->EditorStateMachineGraph &&
                    SM->EditorStateMachineGraph->GetName().Equals(MachineName, ESearchCase::IgnoreCase))
                {
                    return SM;
                }
            }
        }
        return nullptr;
    }

    // Find a state node (UAnimStateNode) inside a state-machine graph by name.
    UAnimStateNode* MCPAnimGraph_FindState(UAnimationStateMachineGraph* SMGraph, const FString& StateName)
    {
        if (!SMGraph) { return nullptr; }
        for (UEdGraphNode* Node : SMGraph->Nodes)
        {
            if (UAnimStateNode* State = Cast<UAnimStateNode>(Node))
            {
                // GetStateName() is ANIMGRAPH_API (VERIFY export).
                if (State->GetStateName().Equals(StateName, ESearchCase::IgnoreCase))
                {
                    return State;
                }
            }
        }
        return nullptr;
    }

    // Find a transition node linking FromState -> ToState.
    UAnimStateTransitionNode* MCPAnimGraph_FindTransition(UAnimationStateMachineGraph* SMGraph,
                                                          const FString& FromState, const FString& ToState)
    {
        if (!SMGraph) { return nullptr; }
        for (UEdGraphNode* Node : SMGraph->Nodes)
        {
            if (UAnimStateTransitionNode* Trans = Cast<UAnimStateTransitionNode>(Node))
            {
                // GetPreviousState()/GetNextState() are ANIMGRAPH_API (VERIFY export).
                UAnimStateNodeBase* Prev = Trans->GetPreviousState();
                UAnimStateNodeBase* Next = Trans->GetNextState();
                if (Prev && Next &&
                    Prev->GetStateName().Equals(FromState, ESearchCase::IgnoreCase) &&
                    Next->GetStateName().Equals(ToState, ESearchCase::IgnoreCase))
                {
                    return Trans;
                }
            }
        }
        return nullptr;
    }

    // Find any AnimGraphNode_Base by NodeGuid across all of the AnimBP's graphs.
    UAnimGraphNode_Base* MCPAnimGraph_FindNodeByGuid(UAnimBlueprint* AnimBP, const FGuid& Guid)
    {
        TArray<UEdGraph*> Graphs;
        MCPAnimGraph_CollectAllGraphs(AnimBP, Graphs);
        for (UEdGraph* Graph : Graphs)
        {
            for (UEdGraphNode* Node : Graph->Nodes)
            {
                if (UAnimGraphNode_Base* AnimNode = Cast<UAnimGraphNode_Base>(Node))
                {
                    if (AnimNode->NodeGuid == Guid)
                    {
                        return AnimNode;
                    }
                }
            }
        }
        return nullptr;
    }

    // Strip a single layer of JSON quoting so a bare scalar or a JSON string both work.
    FString MCPAnimGraph_UnwrapValue(const FString& In)
    {
        FString Trimmed = In;
        Trimmed.TrimStartAndEndInline();
        if (Trimmed.Len() >= 2 && Trimmed.StartsWith(TEXT("\"")) && Trimmed.EndsWith(TEXT("\"")))
        {
            // Cheap unquote for a JSON string scalar (avoids a full JSON parse).
            return Trimmed.Mid(1, Trimmed.Len() - 2).ReplaceEscapedCharWithChar();
        }
        return Trimmed;
    }
}

// ===========================================================================
//  1) AddAnimStateMachine — place a UAnimGraphNode_StateMachine in the AnimGraph.
// ===========================================================================
FString UMCPReflectionLibrary::AddAnimStateMachine(UAnimBlueprint* AnimBlueprint, const FString& MachineName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UEdGraph* AnimGraph = MCPAnimGraph_FindAnimGraph(AnimBlueprint);
    if (!AnimGraph) { return MCPAnimGraph_Error(TEXT("Could not locate the AnimGraph on this Animation Blueprint")); }
    if (MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName))
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("A state machine named '%s' already exists"), *MachineName));
    }

    AnimGraph->Modify();

    // FGraphNodeCreator::Finalize() calls PostPlacedNewNode() which builds the
    // EditorStateMachineGraph + entry node (verified in AnimGraphNode_StateMachineBase.cpp).
    UAnimGraphNode_StateMachine* SMNode = nullptr;
    {
        FGraphNodeCreator<UAnimGraphNode_StateMachine> Creator(*AnimGraph);
        SMNode = Creator.CreateNode(false);   // StaticClass() exported via MinimalAPI
        Creator.Finalize();
    }
    if (!SMNode || !SMNode->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(TEXT("Failed to create the state machine node/graph"));
    }

    if (!MachineName.IsEmpty())
    {
        FBlueprintEditorUtils::RenameGraph(SMNode->EditorStateMachineGraph, MachineName); // UnrealEd
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMNode->EditorStateMachineGraph->GetName());
    Obj->SetStringField(TEXT("node_guid"), SMNode->NodeGuid.ToString());
    Obj->SetStringField(TEXT("graph_name"), AnimGraph->GetName());
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  2) AddAnimState — add a UAnimStateNode into a state machine's graph.
// ===========================================================================
FString UMCPReflectionLibrary::AddAnimState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;
    if (MCPAnimGraph_FindState(SMGraph, StateName))
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' already exists in '%s'"), *StateName, *MachineName));
    }

    SMGraph->Modify();

    // Finalize() -> PostPlacedNewNode() creates the state's inner BoundGraph
    // (UAnimationStateGraph) with its result node (verified in AnimStateNode.cpp).
    UAnimStateNode* StateNode = nullptr;
    {
        FGraphNodeCreator<UAnimStateNode> Creator(*SMGraph);
        StateNode = Creator.CreateNode(false);
        Creator.Finalize();
    }
    if (!StateNode || !StateNode->BoundGraph)
    {
        return MCPAnimGraph_Error(TEXT("Failed to create the state node/bound graph"));
    }

    // The state name IS the bound graph's name (UAnimStateNode::GetStateName()).
    if (!StateName.IsEmpty())
    {
        FBlueprintEditorUtils::RenameGraph(StateNode->BoundGraph, StateName);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("state_name"), StateNode->GetStateName());
    Obj->SetStringField(TEXT("node_guid"), StateNode->NodeGuid.ToString());
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  3) AddAnimTransition — link FromState -> ToState with a transition node.
// ===========================================================================
FString UMCPReflectionLibrary::AddAnimTransition(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                 const FString& FromState, const FString& ToState)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;
    UAnimStateNode* FromNode = MCPAnimGraph_FindState(SMGraph, FromState);
    UAnimStateNode* ToNode   = MCPAnimGraph_FindState(SMGraph, ToState);
    if (!FromNode) { return MCPAnimGraph_Error(FString::Printf(TEXT("Source state '%s' not found"), *FromState)); }
    if (!ToNode)   { return MCPAnimGraph_Error(FString::Printf(TEXT("Target state '%s' not found"), *ToState)); }
    if (MCPAnimGraph_FindTransition(SMGraph, FromState, ToState))
    {
        return MCPAnimGraph_Error(TEXT("A transition between those states already exists"));
    }

    SMGraph->Modify();

    // Finalize() -> PostPlacedNewNode() creates the transition rule BoundGraph.
    UAnimStateTransitionNode* Trans = nullptr;
    {
        FGraphNodeCreator<UAnimStateTransitionNode> Creator(*SMGraph);
        Trans = Creator.CreateNode(false);
        Creator.Finalize();
    }
    if (!Trans) { return MCPAnimGraph_Error(TEXT("Failed to create the transition node")); }

    // Wire the exec pins. CreateConnections is ANIMGRAPH_API (VERIFY export).
    Trans->CreateConnections(FromNode, ToNode);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("from_state"), FromState);
    Obj->SetStringField(TEXT("to_state"), ToState);
    Obj->SetStringField(TEXT("transition_guid"), Trans->NodeGuid.ToString());
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  4) SetAnimEntryState — point the state-machine entry at StateName.
//     Reversible: returns prior_entry_state.
// ===========================================================================
FString UMCPReflectionLibrary::SetAnimEntryState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;
    UAnimStateEntryNode* Entry = SMGraph->EntryNode;   // public UPROPERTY
    if (!Entry) { return MCPAnimGraph_Error(TEXT("State machine has no entry node")); }
    UAnimStateNode* Target = MCPAnimGraph_FindState(SMGraph, StateName);
    if (!Target) { return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' not found"), *StateName)); }

    UEdGraphPin* EntryPin = Entry->GetOutputPin();     // ANIMGRAPH_API (VERIFY export)
    if (!EntryPin) { return MCPAnimGraph_Error(TEXT("Entry node has no output pin")); }

    // Capture prior entry state (if any) for undo.
    FString PriorEntry;
    if (UEdGraphNode* PriorOut = Entry->GetOutputNode())  // ANIMGRAPH_API (VERIFY export)
    {
        if (UAnimStateNodeBase* PriorState = Cast<UAnimStateNodeBase>(PriorOut))
        {
            PriorEntry = PriorState->GetStateName();
        }
    }

    SMGraph->Modify();
    EntryPin->Modify();
    EntryPin->BreakAllPinLinks();                        // UEdGraphPin (Engine)
    if (UEdGraphPin* TargetIn = Target->GetInputPin())   // virtual, via vtable
    {
        TargetIn->Modify();
        EntryPin->MakeLinkTo(TargetIn);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("entry_state"), StateName);
    if (PriorEntry.IsEmpty()) { Obj->SetField(TEXT("prior_entry_state"), MakeShared<FJsonValueNull>()); }
    else { Obj->SetStringField(TEXT("prior_entry_state"), PriorEntry); }
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  5) GetAnimStateMachineJson — read states / transitions / entry (read-only).
// ===========================================================================
FString UMCPReflectionLibrary::GetAnimStateMachineJson(UAnimBlueprint* AnimBlueprint, const FString& MachineName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;

    TArray<TSharedPtr<FJsonValue>> States;
    TArray<TSharedPtr<FJsonValue>> Transitions;
    FString EntryState;

    for (UEdGraphNode* Node : SMGraph->Nodes)
    {
        if (UAnimStateNode* State = Cast<UAnimStateNode>(Node))
        {
            TSharedRef<FJsonObject> S = MakeShared<FJsonObject>();
            S->SetStringField(TEXT("name"), State->GetStateName());
            S->SetStringField(TEXT("node_guid"), State->NodeGuid.ToString());
            S->SetBoolField(TEXT("always_reset_on_entry"), State->bAlwaysResetOnEntry);
            States.Add(MakeShared<FJsonValueObject>(S));
        }
        else if (UAnimStateTransitionNode* Trans = Cast<UAnimStateTransitionNode>(Node))
        {
            UAnimStateNodeBase* Prev = Trans->GetPreviousState();
            UAnimStateNodeBase* Next = Trans->GetNextState();
            TSharedRef<FJsonObject> T = MakeShared<FJsonObject>();
            T->SetStringField(TEXT("from_state"), Prev ? Prev->GetStateName() : FString());
            T->SetStringField(TEXT("to_state"),   Next ? Next->GetStateName() : FString());
            T->SetStringField(TEXT("node_guid"), Trans->NodeGuid.ToString());
            T->SetNumberField(TEXT("priority_order"), Trans->PriorityOrder);
            T->SetNumberField(TEXT("crossfade_duration"), Trans->CrossfadeDuration);
            T->SetBoolField(TEXT("disabled"), Trans->bDisabled);
            Transitions.Add(MakeShared<FJsonValueObject>(T));
        }
        else if (UAnimStateEntryNode* Entry = Cast<UAnimStateEntryNode>(Node))
        {
            if (UEdGraphNode* Out = Entry->GetOutputNode())
            {
                if (UAnimStateNodeBase* EntryTarget = Cast<UAnimStateNodeBase>(Out))
                {
                    EntryState = EntryTarget->GetStateName();
                }
            }
        }
    }

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("entry_state"), EntryState);
    Obj->SetNumberField(TEXT("state_count"), States.Num());
    Obj->SetNumberField(TEXT("transition_count"), Transitions.Num());
    Obj->SetArrayField(TEXT("states"), States);
    Obj->SetArrayField(TEXT("transitions"), Transitions);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  6) RemoveAnimState — delete a state node (and its bound graph).
//     Captures the state's outgoing transitions for a best-effort re-add.
// ===========================================================================
FString UMCPReflectionLibrary::RemoveAnimState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;
    UAnimStateNode* State = MCPAnimGraph_FindState(SMGraph, StateName);
    if (!State) { return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' not found"), *StateName)); }

    // Capture connected transitions for undo (best effort — full graph state not restored).
    TArray<TSharedPtr<FJsonValue>> PriorTransitions;
    {
        TArray<UAnimStateTransitionNode*> TransList;
        State->GetTransitionList(TransList, /*bWantSortedList=*/false);   // ANIMGRAPH_API (VERIFY export)
        for (UAnimStateTransitionNode* T : TransList)
        {
            if (!T) { continue; }
            UAnimStateNodeBase* Prev = T->GetPreviousState();
            UAnimStateNodeBase* Next = T->GetNextState();
            TSharedRef<FJsonObject> TO = MakeShared<FJsonObject>();
            TO->SetStringField(TEXT("from_state"), Prev ? Prev->GetStateName() : FString());
            TO->SetStringField(TEXT("to_state"),   Next ? Next->GetStateName() : FString());
            PriorTransitions.Add(MakeShared<FJsonValueObject>(TO));
        }
    }

    SMGraph->Modify();
    // DestroyNode() removes the node's BoundGraph via FBlueprintEditorUtils::RemoveGraph
    // and breaks its links (verified in AnimStateNode.cpp).
    State->DestroyNode();   // virtual (UEdGraphNode), dispatched via vtable

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("removed_state"), StateName);
    Obj->SetArrayField(TEXT("prior_transitions"), PriorTransitions);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  7) RemoveAnimTransition — delete the transition linking two states.
//     Captures prior crossfade/priority for a faithful re-add.
// ===========================================================================
FString UMCPReflectionLibrary::RemoveAnimTransition(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                    const FString& FromState, const FString& ToState)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimationStateMachineGraph* SMGraph = SM->EditorStateMachineGraph;
    UAnimStateTransitionNode* Trans = MCPAnimGraph_FindTransition(SMGraph, FromState, ToState);
    if (!Trans) { return MCPAnimGraph_Error(TEXT("Transition not found")); }

    const int32 PriorPriority = Trans->PriorityOrder;
    const float PriorCrossfade = Trans->CrossfadeDuration;
    const bool  PriorDisabled = Trans->bDisabled;

    SMGraph->Modify();
    Trans->DestroyNode();   // virtual

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), SMGraph->GetName());
    Obj->SetStringField(TEXT("from_state"), FromState);
    Obj->SetStringField(TEXT("to_state"), ToState);
    Obj->SetNumberField(TEXT("prior_priority_order"), PriorPriority);
    Obj->SetNumberField(TEXT("prior_crossfade_duration"), PriorCrossfade);
    Obj->SetBoolField(TEXT("prior_disabled"), PriorDisabled);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  8) SetAnimTransitionProperty — set a UPROPERTY on the transition node via
//     reflection (CrossfadeDuration/PriorityOrder/bDisabled/LogicType/BlendMode/…).
//     Reversible: returns prior_value.
// ===========================================================================
FString UMCPReflectionLibrary::SetAnimTransitionProperty(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                         const FString& FromState, const FString& ToState,
                                                         const FString& PropName, const FString& ValueJson)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName));
    }
    UAnimStateTransitionNode* Trans = MCPAnimGraph_FindTransition(SM->EditorStateMachineGraph, FromState, ToState);
    if (!Trans) { return MCPAnimGraph_Error(TEXT("Transition not found")); }

    // CoreUObject reflection — no editor-module export dependency.
    FProperty* Prop = FindFProperty<FProperty>(UAnimStateTransitionNode::StaticClass(), FName(*PropName));
    if (!Prop) { return MCPAnimGraph_Error(FString::Printf(TEXT("Property '%s' not found on UAnimStateTransitionNode"), *PropName)); }

    void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Trans);

    FString PriorValue;
    Prop->ExportTextItem_Direct(PriorValue, ValuePtr, /*Default*/nullptr, Trans, PPF_None);

    const FString ImportStr = MCPAnimGraph_UnwrapValue(ValueJson);
    Trans->Modify();
    const TCHAR* Result = Prop->ImportText_Direct(*ImportStr, ValuePtr, Trans, PPF_None);
    if (Result == nullptr)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("Failed to import value '%s' into property '%s'"), *ImportStr, *PropName));
    }
    Trans->PostEditChange();

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("from_state"), FromState);
    Obj->SetStringField(TEXT("to_state"), ToState);
    Obj->SetStringField(TEXT("property"), PropName);
    Obj->SetStringField(TEXT("value"), ImportStr);
    Obj->SetStringField(TEXT("prior_value"), PriorValue);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  9) BuildAnimStateMachine — composite: create machine + states + transitions +
//     entry from one JSON spec:
//     { "states": ["Idle","Run"], "entry": "Idle",
//       "transitions": [ {"from":"Idle","to":"Run"}, ... ] }
//     Idempotent-ish: creates the machine if absent, reuses if present.
// ===========================================================================
FString UMCPReflectionLibrary::BuildAnimStateMachine(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& SpecJson)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }

    TSharedPtr<FJsonObject> Spec;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SpecJson);
    if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
    {
        return MCPAnimGraph_Error(TEXT("SpecJson is not a valid JSON object"));
    }

    // Ensure the machine exists.
    if (!MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName))
    {
        const FString CreateResult = UMCPReflectionLibrary::AddAnimStateMachine(AnimBlueprint, MachineName);
        // If creation errored, propagate.
        if (CreateResult.Contains(TEXT("\"status\":\"error\"")))
        {
            return CreateResult;
        }
    }

    int32 StatesAdded = 0, TransitionsAdded = 0;
    TArray<FString> Errors;

    const TArray<TSharedPtr<FJsonValue>>* StatesArr = nullptr;
    if (Spec->TryGetArrayField(TEXT("states"), StatesArr) && StatesArr)
    {
        for (const TSharedPtr<FJsonValue>& V : *StatesArr)
        {
            FString StateName;
            if (V.IsValid() && V->TryGetString(StateName) && !StateName.IsEmpty())
            {
                const FString R = UMCPReflectionLibrary::AddAnimState(AnimBlueprint, MachineName, StateName);
                if (R.Contains(TEXT("\"status\":\"error\""))) { Errors.Add(FString::Printf(TEXT("state '%s': %s"), *StateName, *R)); }
                else { ++StatesAdded; }
            }
        }
    }

    const TArray<TSharedPtr<FJsonValue>>* TransArr = nullptr;
    if (Spec->TryGetArrayField(TEXT("transitions"), TransArr) && TransArr)
    {
        for (const TSharedPtr<FJsonValue>& V : *TransArr)
        {
            const TSharedPtr<FJsonObject>* TObj = nullptr;
            if (V.IsValid() && V->TryGetObject(TObj) && TObj && TObj->IsValid())
            {
                FString From, To;
                (*TObj)->TryGetStringField(TEXT("from"), From);
                (*TObj)->TryGetStringField(TEXT("to"), To);
                if (!From.IsEmpty() && !To.IsEmpty())
                {
                    const FString R = UMCPReflectionLibrary::AddAnimTransition(AnimBlueprint, MachineName, From, To);
                    if (R.Contains(TEXT("\"status\":\"error\""))) { Errors.Add(FString::Printf(TEXT("transition %s->%s: %s"), *From, *To, *R)); }
                    else { ++TransitionsAdded; }
                }
            }
        }
    }

    FString Entry;
    if (Spec->TryGetStringField(TEXT("entry"), Entry) && !Entry.IsEmpty())
    {
        const FString R = UMCPReflectionLibrary::SetAnimEntryState(AnimBlueprint, MachineName, Entry);
        if (R.Contains(TEXT("\"status\":\"error\""))) { Errors.Add(FString::Printf(TEXT("entry '%s': %s"), *Entry, *R)); }
    }

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), MachineName);
    Obj->SetNumberField(TEXT("states_added"), StatesAdded);
    Obj->SetNumberField(TEXT("transitions_added"), TransitionsAdded);
    if (Errors.Num() > 0)
    {
        TArray<TSharedPtr<FJsonValue>> ErrArr;
        for (const FString& E : Errors) { ErrArr.Add(MakeShared<FJsonValueString>(E)); }
        Obj->SetArrayField(TEXT("partial_errors"), ErrArr);
    }
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
// 10) SetAnimNodePinExposure — toggle a property's pin on an AnimGraph node.
//     Reversible: returns prior_exposed.
// ===========================================================================
FString UMCPReflectionLibrary::SetAnimNodePinExposure(UAnimBlueprint* AnimBlueprint, const FString& NodeGuid,
                                                      const FString& PropertyName, bool bExpose)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    FGuid Guid;
    if (!FGuid::Parse(NodeGuid, Guid)) { return MCPAnimGraph_Error(TEXT("NodeGuid is not a valid GUID")); }
    UAnimGraphNode_Base* Node = MCPAnimGraph_FindNodeByGuid(AnimBlueprint, Guid);
    if (!Node) { return MCPAnimGraph_Error(TEXT("Anim graph node with that GUID not found")); }

    // ShowPinForProperties is a public UPROPERTY TArray<FOptionalPinFromProperty>.
    int32 Index = INDEX_NONE;
    bool bPrior = false;
    for (int32 i = 0; i < Node->ShowPinForProperties.Num(); ++i)
    {
        // FOptionalPinFromProperty::PropertyName / bShowPin (Engine). VERIFY field names.
        if (Node->ShowPinForProperties[i].PropertyName == FName(*PropertyName))
        {
            Index = i;
            bPrior = Node->ShowPinForProperties[i].bShowPin != 0;
            break;
        }
    }
    if (Index == INDEX_NONE)
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("Property '%s' is not an optional (exposable) pin on this node"), *PropertyName));
    }

    Node->Modify();
    // SetPinVisibility is ANIMGRAPH_API (VERIFY export) — updates the array + reconstructs.
    Node->SetPinVisibility(bExpose, Index);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("node_guid"), NodeGuid);
    Obj->SetStringField(TEXT("property"), PropertyName);
    Obj->SetBoolField(TEXT("exposed"), bExpose);
    Obj->SetBoolField(TEXT("prior_exposed"), bPrior);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
// 11) BindAnimNodeFunction — bind a thread-safe function to an anim node slot
//     (BindingSlot: initial_update | become_relevant | update). Empty FunctionName
//     clears the binding. Reversible: returns prior_function.
// ===========================================================================
FString UMCPReflectionLibrary::BindAnimNodeFunction(UAnimBlueprint* AnimBlueprint, const FString& NodeGuid,
                                                    const FString& BindingSlot, const FString& FunctionName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    FGuid Guid;
    if (!FGuid::Parse(NodeGuid, Guid)) { return MCPAnimGraph_Error(TEXT("NodeGuid is not a valid GUID")); }
    UAnimGraphNode_Base* Node = MCPAnimGraph_FindNodeByGuid(AnimBlueprint, Guid);
    if (!Node) { return MCPAnimGraph_Error(TEXT("Anim graph node with that GUID not found")); }

    FMemberReference* Ref = nullptr;
    const FString Slot = BindingSlot.ToLower();
    if      (Slot == TEXT("initial_update"))  { Ref = &Node->InitialUpdateFunction; }
    else if (Slot == TEXT("become_relevant")) { Ref = &Node->BecomeRelevantFunction; }
    else if (Slot == TEXT("update"))          { Ref = &Node->UpdateFunction; }
    else { return MCPAnimGraph_Error(TEXT("BindingSlot must be one of: initial_update | become_relevant | update")); }

    const FString PriorFunc = Ref->GetMemberName().ToString();   // FMemberReference (Engine), inline

    Node->Modify();
    if (FunctionName.IsEmpty())
    {
        *Ref = FMemberReference();   // clear
    }
    else
    {
        // Bind to a member function on this anim blueprint's generated class.
        // SetSelfMember is ENGINE_API. VERIFY: for function-library targets a
        // SetExternalMember path would be needed; self-member covers the common case.
        Ref->SetSelfMember(FName(*FunctionName));
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("node_guid"), NodeGuid);
    Obj->SetStringField(TEXT("binding_slot"), Slot);
    Obj->SetStringField(TEXT("function"), FunctionName);
    if (PriorFunc.IsEmpty() || PriorFunc == TEXT("None")) { Obj->SetField(TEXT("prior_function"), MakeShared<FJsonValueNull>()); }
    else { Obj->SetStringField(TEXT("prior_function"), PriorFunc); }
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
// 12) AddAnimLayer — add a new animation layer graph to the AnimBlueprint.
//     Reversible: FBlueprintEditorUtils::RemoveGraph by the returned layer name.
// ===========================================================================
FString UMCPReflectionLibrary::AddAnimLayer(UAnimBlueprint* AnimBlueprint, const FString& LayerName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    if (LayerName.IsEmpty()) { return MCPAnimGraph_Error(TEXT("LayerName is empty")); }

    // Duplicate-name guard.
    for (UEdGraph* G : AnimBlueprint->FunctionGraphs)
    {
        if (G && G->GetName().Equals(LayerName, ESearchCase::IgnoreCase))
        {
            return MCPAnimGraph_Error(FString::Printf(TEXT("A graph named '%s' already exists"), *LayerName));
        }
    }

    AnimBlueprint->Modify();

    // Mirrors FBlueprintEditor::NewDocument_OnClicked(CGT_NewAnimationLayer).
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
        AnimBlueprint, FName(*LayerName),
        UAnimationGraph::StaticClass(),        // MinimalAPI StaticClass (VERIFY export)
        UAnimationGraphSchema::StaticClass());  // MinimalAPI StaticClass (VERIFY export)
    if (!NewGraph) { return MCPAnimGraph_Error(TEXT("CreateNewGraph failed")); }

    FBlueprintEditorUtils::AddDomainSpecificGraph(AnimBlueprint, NewGraph);  // UnrealEd

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("layer_name"), NewGraph->GetName());
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
// 13) CreateAnimLayerInterface — create a new Anim Layer Interface asset
//     (a BPTYPE_Interface UAnimBlueprint parented to UAnimLayerInterface).
//     NOT saved here — coordinator/Python saves the returned package.
// ===========================================================================
FString UMCPReflectionLibrary::CreateAnimLayerInterface(const FString& PackagePath, const FString& AssetName)
{
    if (PackagePath.IsEmpty() || AssetName.IsEmpty())
    {
        return MCPAnimGraph_Error(TEXT("PackagePath and AssetName are required"));
    }

    FString FullPackageName = PackagePath;
    if (!FullPackageName.EndsWith(TEXT("/"))) { FullPackageName += TEXT("/"); }
    FullPackageName += AssetName;

    if (FindPackage(nullptr, *FullPackageName) || FindObject<UObject>(nullptr, *(FullPackageName + TEXT(".") + AssetName)))
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("An asset already exists at '%s'"), *FullPackageName));
    }

    UPackage* Package = CreatePackage(*FullPackageName);
    if (!Package) { return MCPAnimGraph_Error(TEXT("CreatePackage failed")); }
    Package->FullyLoad();

    // Mirrors UAnimBlueprintFactory's BPTYPE_Interface path (verified in AnimBlueprintFactory.cpp):
    //   ClassToUse = UAnimLayerInterface::StaticClass(); GeneratedClass = UBlueprintGeneratedClass.
    UObject* Created = FKismetEditorUtilities::CreateBlueprint(
        UAnimLayerInterface::StaticClass(),
        Package,
        FName(*AssetName),
        BPTYPE_Interface,
        UAnimBlueprint::StaticClass(),
        UBlueprintGeneratedClass::StaticClass(),
        FName(TEXT("MCPCreateAnimLayerInterface")));

    UAnimBlueprint* NewBP = Cast<UAnimBlueprint>(Created);
    if (!NewBP) { return MCPAnimGraph_Error(TEXT("CreateBlueprint did not return a UAnimBlueprint")); }

    NewBP->MarkPackageDirty();

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_layer_interface"), NewBP->GetName());
    Obj->SetStringField(TEXT("package"), FullPackageName);
    Obj->SetStringField(TEXT("path"), NewBP->GetPathName());
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  C++ #59 — LOCOMOTION AUTHORING (fill states, wire poses, transition rules, speed drive)
// ===========================================================================
namespace
{
    // The single pose pin of a given direction on an AnimGraph node.
    UEdGraphPin* MCPAnimGraph_FindPosePin(UEdGraphNode* Node, EEdGraphPinDirection Dir)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* Pin : Node->Pins)
        {
            if (Pin && Pin->Direction == Dir && UAnimationGraphSchema::IsPosePin(Pin->PinType))
            {
                return Pin;
            }
        }
        return nullptr;
    }

    // First node of type T in a graph.
    template <typename T>
    T* MCPAnimGraph_FirstNodeOfType(UEdGraph* Graph)
    {
        if (!Graph) { return nullptr; }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (T* Typed = Cast<T>(N)) { return Typed; }
        }
        return nullptr;
    }

    // Connect two pins via the graph schema, falling back to a direct link.
    bool MCPAnimGraph_Connect(UEdGraph* Graph, UEdGraphPin* From, UEdGraphPin* To)
    {
        if (!From || !To) { return false; }
        const UEdGraphSchema* Schema = Graph ? Graph->GetSchema() : nullptr;
        if (Schema && Schema->TryCreateConnection(From, To)) { return true; }
        From->MakeLinkTo(To);
        return From->LinkedTo.Contains(To);
    }

    // Map a comparison operator to a KismetMathLibrary double-compare function name.
    FName MCPAnimGraph_CompareFunc(const FString& Op)
    {
        const FString O = Op.TrimStartAndEnd();
        if (O == TEXT(">"))  { return FName(TEXT("Greater_DoubleDouble")); }
        if (O == TEXT(">=")) { return FName(TEXT("GreaterEqual_DoubleDouble")); }
        if (O == TEXT("<"))  { return FName(TEXT("Less_DoubleDouble")); }
        if (O == TEXT("<=")) { return FName(TEXT("LessEqual_DoubleDouble")); }
        if (O == TEXT("==")) { return FName(TEXT("EqualEqual_DoubleDouble")); }
        if (O == TEXT("!=")) { return FName(TEXT("NotEqual_DoubleDouble")); }
        return NAME_None;
    }
}

// 14) SetStateAnim — fill a state's bound graph with a looping Sequence Player wired to the state Result.
FString UMCPReflectionLibrary::SetStateAnim(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                            const FString& StateName, const FString& AnimAssetPath,
                                            bool bLoop, float PlayRate)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName)); }
    UAnimStateNode* State = MCPAnimGraph_FindState(SM->EditorStateMachineGraph, StateName);
    if (!State || !State->BoundGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' not found"), *StateName)); }
    UEdGraph* BoundGraph = State->BoundGraph;

    UAnimSequenceBase* Seq = LoadObject<UAnimSequenceBase>(nullptr, *AnimAssetPath);
    if (!Seq) { return MCPAnimGraph_Error(FString::Printf(TEXT("Could not load anim sequence '%s'"), *AnimAssetPath)); }

    UAnimGraphNode_StateResult* ResultNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_StateResult>(BoundGraph);
    if (!ResultNode) { return MCPAnimGraph_Error(TEXT("State bound graph has no result node")); }
    UEdGraphPin* ResultIn = MCPAnimGraph_FindPosePin(ResultNode, EGPD_Input);
    if (!ResultIn) { return MCPAnimGraph_Error(TEXT("State result node has no input pose pin")); }

    BoundGraph->Modify();

    // Reuse an existing sequence player if one is already in this state (idempotent re-run).
    UAnimGraphNode_SequencePlayer* SeqNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_SequencePlayer>(BoundGraph);
    if (SeqNode)
    {
        SeqNode->Modify();
        SeqNode->Node.SetSequence(Seq);            // Sequence/bLoopAnimation/PlayRate are protected — use setters
        SeqNode->Node.SetLoopAnimation(bLoop);
        SeqNode->Node.SetPlayRate(PlayRate);
        SeqNode->ReconstructNode();
    }
    else
    {
        FGraphNodeCreator<UAnimGraphNode_SequencePlayer> Creator(*BoundGraph);
        SeqNode = Creator.CreateNode(false);
        SeqNode->Node.SetSequence(Seq);
        SeqNode->Node.SetLoopAnimation(bLoop);
        SeqNode->Node.SetPlayRate(PlayRate);
        Creator.Finalize();
        SeqNode->NodePosX = ResultNode->NodePosX - 320;
        SeqNode->NodePosY = ResultNode->NodePosY;
    }

    UEdGraphPin* SeqOut = MCPAnimGraph_FindPosePin(SeqNode, EGPD_Output);
    if (!SeqOut) { return MCPAnimGraph_Error(TEXT("Sequence player has no output pose pin")); }

    ResultIn->BreakAllPinLinks();
    const bool bConnected = MCPAnimGraph_Connect(BoundGraph, SeqOut, ResultIn);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state"), StateName);
    Obj->SetStringField(TEXT("sequence"), Seq->GetPathName());
    Obj->SetStringField(TEXT("sequence_player_guid"), SeqNode->NodeGuid.ToString());
    Obj->SetBoolField(TEXT("loop"), bLoop);
    Obj->SetNumberField(TEXT("play_rate"), PlayRate);
    Obj->SetBoolField(TEXT("connected_to_result"), bConnected);
    return MCPAnimGraph_SerializeObject(Obj);
}

// 15) ConnectAnimStateMachineToOutput — wire the state machine's pose output to the AnimGraph Output Pose.
FString UMCPReflectionLibrary::ConnectAnimStateMachineToOutput(UAnimBlueprint* AnimBlueprint, const FString& MachineName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM) { return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName)); }
    UEdGraph* HostGraph = SM->GetGraph();
    if (!HostGraph) { return MCPAnimGraph_Error(TEXT("State machine node has no host graph")); }
    UAnimGraphNode_Root* Root = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_Root>(HostGraph);
    if (!Root) { return MCPAnimGraph_Error(TEXT("AnimGraph has no Output Pose (root) node")); }

    UEdGraphPin* SMOut = MCPAnimGraph_FindPosePin(SM, EGPD_Output);
    UEdGraphPin* RootIn = MCPAnimGraph_FindPosePin(Root, EGPD_Input);
    if (!SMOut)  { return MCPAnimGraph_Error(TEXT("State machine node has no output pose pin")); }
    if (!RootIn) { return MCPAnimGraph_Error(TEXT("Output Pose node has no input pose pin")); }

    HostGraph->Modify();
    RootIn->BreakAllPinLinks();
    const bool bConnected = MCPAnimGraph_Connect(HostGraph, SMOut, RootIn);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state_machine_name"), MachineName);
    Obj->SetBoolField(TEXT("connected_to_output"), bConnected);
    return MCPAnimGraph_SerializeObject(Obj);
}

// 16) SetAnimTransitionRule — author a "<variable> <op> <value>" bool rule on a transition.
FString UMCPReflectionLibrary::SetAnimTransitionRule(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                     const FString& FromState, const FString& ToState,
                                                     const FString& VariableName, const FString& Op, const FString& ValueStr)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName)); }
    UAnimStateTransitionNode* Trans = MCPAnimGraph_FindTransition(SM->EditorStateMachineGraph, FromState, ToState);
    if (!Trans || !Trans->BoundGraph) { return MCPAnimGraph_Error(TEXT("Transition (or its rule graph) not found")); }
    UEdGraph* RuleGraph = Trans->BoundGraph;

    const FName FuncName = MCPAnimGraph_CompareFunc(Op);
    if (FuncName == NAME_None) { return MCPAnimGraph_Error(FString::Printf(TEXT("Unsupported op '%s' (use > >= < <= == !=)"), *Op)); }

    UAnimGraphNode_TransitionResult* ResultNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_TransitionResult>(RuleGraph);
    if (!ResultNode) { return MCPAnimGraph_Error(TEXT("Transition rule graph has no result node")); }
    UEdGraphPin* CanEnter = ResultNode->FindPin(TEXT("bCanEnterTransition"), EGPD_Input);
    if (!CanEnter) { return MCPAnimGraph_Error(TEXT("Result node has no bCanEnterTransition pin")); }

    RuleGraph->Modify();
    // Clean slate: remove every node except the result node so a re-run replaces the rule.
    TArray<UEdGraphNode*> ToRemove;
    for (UEdGraphNode* N : RuleGraph->Nodes) { if (N != ResultNode) { ToRemove.Add(N); } }
    for (UEdGraphNode* N : ToRemove) { FBlueprintEditorUtils::RemoveNode(AnimBlueprint, N, /*bDontRecompile*/true); }

    // Variable getter.
    UK2Node_VariableGet* VarGet = nullptr;
    {
        FGraphNodeCreator<UK2Node_VariableGet> C(*RuleGraph);
        VarGet = C.CreateNode(false);
        VarGet->VariableReference.SetSelfMember(FName(*VariableName));
        C.Finalize();
    }
    UEdGraphPin* VarOut = VarGet->GetValuePin();
    if (!VarOut) { return MCPAnimGraph_Error(FString::Printf(TEXT("Variable '%s' getter has no value pin (does the variable exist?)"), *VariableName)); }

    // Comparison call (KismetMathLibrary double compare).
    UK2Node_CallFunction* Cmp = nullptr;
    {
        FGraphNodeCreator<UK2Node_CallFunction> C(*RuleGraph);
        Cmp = C.CreateNode(false);
        Cmp->FunctionReference.SetExternalMember(FuncName, UKismetMathLibrary::StaticClass());
        C.Finalize();
    }
    UEdGraphPin* PinA = Cmp->FindPin(TEXT("A"), EGPD_Input);
    UEdGraphPin* PinB = Cmp->FindPin(TEXT("B"), EGPD_Input);
    UEdGraphPin* PinRet = Cmp->GetReturnValuePin();
    if (!PinA || !PinB || !PinRet) { return MCPAnimGraph_Error(TEXT("Compare node pins not found")); }
    PinB->DefaultValue = ValueStr;

    VarGet->NodePosX = ResultNode->NodePosX - 640; VarGet->NodePosY = ResultNode->NodePosY;
    Cmp->NodePosX = ResultNode->NodePosX - 320;    Cmp->NodePosY = ResultNode->NodePosY;

    const bool bC1 = MCPAnimGraph_Connect(RuleGraph, VarOut, PinA);
    const bool bC2 = MCPAnimGraph_Connect(RuleGraph, PinRet, CanEnter);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("from_state"), FromState);
    Obj->SetStringField(TEXT("to_state"), ToState);
    Obj->SetStringField(TEXT("rule"), FString::Printf(TEXT("%s %s %s"), *VariableName, *Op, *ValueStr));
    Obj->SetStringField(TEXT("compare_func"), FuncName.ToString());
    Obj->SetBoolField(TEXT("wired"), bC1 && bC2);
    return MCPAnimGraph_SerializeObject(Obj);
}

// 17) SetupAnimSpeedFromVelocity — author BlueprintUpdateAnimation: Set <var> = VSize(GetOwningActor.GetVelocity()).
FString UMCPReflectionLibrary::SetupAnimSpeedFromVelocity(UAnimBlueprint* AnimBlueprint, const FString& VariableName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    if (AnimBlueprint->UbergraphPages.Num() == 0 || !AnimBlueprint->UbergraphPages[0])
    {
        return MCPAnimGraph_Error(TEXT("AnimBlueprint has no event graph (ubergraph)"));
    }
    UEdGraph* Uber = AnimBlueprint->UbergraphPages[0];
    Uber->Modify();

    // Find or create the BlueprintUpdateAnimation event.
    UK2Node_Event* Evt = nullptr;
    for (UEdGraphNode* N : Uber->Nodes)
    {
        if (UK2Node_Event* E = Cast<UK2Node_Event>(N))
        {
            if (E->EventReference.GetMemberName() == FName(TEXT("BlueprintUpdateAnimation"))) { Evt = E; break; }
        }
    }
    if (!Evt)
    {
        FGraphNodeCreator<UK2Node_Event> C(*Uber);
        Evt = C.CreateNode(false);
        Evt->EventReference.SetExternalMember(FName(TEXT("BlueprintUpdateAnimation")), UAnimInstance::StaticClass());
        Evt->bOverrideFunction = true;
        C.Finalize();
        Evt->NodePosX = 0; Evt->NodePosY = 0;
    }
    UEdGraphPin* EvtThen = Evt->FindPin(UEdGraphSchema_K2::PN_Then, EGPD_Output);
    if (!EvtThen) { return MCPAnimGraph_Error(TEXT("Update event has no exec (then) pin")); }

    // GetOwningActor (self-context, pure).
    UK2Node_CallFunction* GetActor = nullptr;
    {
        FGraphNodeCreator<UK2Node_CallFunction> C(*Uber);
        GetActor = C.CreateNode(false);
        GetActor->FunctionReference.SetSelfMember(FName(TEXT("GetOwningActor")));
        C.Finalize();
        GetActor->NodePosX = 300; GetActor->NodePosY = 200;
    }
    UEdGraphPin* ActorRet = GetActor->GetReturnValuePin();

    // GetVelocity on the actor (member call, pure) — target via the 'self' pin.
    UK2Node_CallFunction* GetVel = nullptr;
    {
        FGraphNodeCreator<UK2Node_CallFunction> C(*Uber);
        GetVel = C.CreateNode(false);
        GetVel->FunctionReference.SetExternalMember(FName(TEXT("GetVelocity")), AActor::StaticClass());
        C.Finalize();
        GetVel->NodePosX = 560; GetVel->NodePosY = 200;
    }
    UEdGraphPin* VelSelf = GetVel->FindPin(UEdGraphSchema_K2::PN_Self, EGPD_Input);
    UEdGraphPin* VelRet = GetVel->GetReturnValuePin();

    // VSize (static, pure).
    UK2Node_CallFunction* VSizeNode = nullptr;
    {
        FGraphNodeCreator<UK2Node_CallFunction> C(*Uber);
        VSizeNode = C.CreateNode(false);
        VSizeNode->FunctionReference.SetExternalMember(FName(TEXT("VSize")), UKismetMathLibrary::StaticClass());
        C.Finalize();
        VSizeNode->NodePosX = 820; VSizeNode->NodePosY = 200;
    }
    UEdGraphPin* VSizeA = VSizeNode->FindPin(TEXT("A"), EGPD_Input);
    UEdGraphPin* VSizeRet = VSizeNode->GetReturnValuePin();

    // Set <VariableName>.
    UK2Node_VariableSet* SetVar = nullptr;
    {
        FGraphNodeCreator<UK2Node_VariableSet> C(*Uber);
        SetVar = C.CreateNode(false);
        SetVar->VariableReference.SetSelfMember(FName(*VariableName));
        C.Finalize();
        SetVar->NodePosX = 1080; SetVar->NodePosY = 0;
    }
    UEdGraphPin* SetExecIn = SetVar->FindPin(UEdGraphSchema_K2::PN_Execute, EGPD_Input);
    UEdGraphPin* SetValIn = SetVar->FindPin(FName(*VariableName), EGPD_Input);

    if (!ActorRet || !VelSelf || !VelRet || !VSizeA || !VSizeRet || !SetExecIn || !SetValIn)
    {
        return MCPAnimGraph_Error(TEXT("Could not resolve one or more pins for the speed-update graph (variable may not exist yet)"));
    }

    // Wire: exec Event.then -> Set.exec ; data GetActor -> GetVelocity.self -> VSize.A -> Set.<var>.
    const bool bE = MCPAnimGraph_Connect(Uber, EvtThen, SetExecIn);
    const bool bD1 = MCPAnimGraph_Connect(Uber, ActorRet, VelSelf);
    const bool bD2 = MCPAnimGraph_Connect(Uber, VelRet, VSizeA);
    const bool bD3 = MCPAnimGraph_Connect(Uber, VSizeRet, SetValIn);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("variable"), VariableName);
    Obj->SetBoolField(TEXT("exec_wired"), bE);
    Obj->SetBoolField(TEXT("data_wired"), bD1 && bD2 && bD3);
    return MCPAnimGraph_SerializeObject(Obj);
}

// 18) SetStateAnimPlayRateFromVariable — drive a state's Sequence Player Play Rate from <variable>/<divisor>
//     (speed-matched locomotion: play_rate = Speed / RefSpeed, so the walk cycle tracks movement speed).
FString UMCPReflectionLibrary::SetStateAnimPlayRateFromVariable(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                               const FString& StateName, const FString& VariableName, float Divisor)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName)); }
    UAnimStateNode* State = MCPAnimGraph_FindState(SM->EditorStateMachineGraph, StateName);
    if (!State || !State->BoundGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' not found"), *StateName)); }
    UEdGraph* BoundGraph = State->BoundGraph;

    UAnimGraphNode_SequencePlayer* SeqNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_SequencePlayer>(BoundGraph);
    if (!SeqNode) { return MCPAnimGraph_Error(TEXT("State has no Sequence Player (call set_state_anim first)")); }

    // Expose the (hidden-by-default) Play Rate pin so it can be driven by a graph link.
    int32 Index = INDEX_NONE;
    for (int32 i = 0; i < SeqNode->ShowPinForProperties.Num(); ++i)
    {
        if (SeqNode->ShowPinForProperties[i].PropertyName == FName(TEXT("PlayRate"))) { Index = i; break; }
    }
    if (Index == INDEX_NONE) { return MCPAnimGraph_Error(TEXT("Sequence Player has no exposable 'PlayRate' pin")); }

    BoundGraph->Modify();
    SeqNode->Modify();
    SeqNode->SetPinVisibility(true, Index);   // ANIMGRAPH_API — shows the pin + reconstructs the node

    UEdGraphPin* RatePin = SeqNode->FindPin(TEXT("PlayRate"), EGPD_Input);
    if (!RatePin) { return MCPAnimGraph_Error(TEXT("Play Rate pin not found after exposing")); }

    // Idempotent: drop any prior driver nodes (variable getter / math call) we may have added.
    TArray<UEdGraphNode*> ToRemove;
    for (UEdGraphNode* N : BoundGraph->Nodes)
    {
        if (N->IsA<UK2Node_VariableGet>() || N->IsA<UK2Node_CallFunction>()) { ToRemove.Add(N); }
    }
    for (UEdGraphNode* N : ToRemove) { FBlueprintEditorUtils::RemoveNode(AnimBlueprint, N, /*bDontRecompile*/true); }
    RatePin->BreakAllPinLinks();

    UK2Node_VariableGet* VarGet = nullptr;
    {
        FGraphNodeCreator<UK2Node_VariableGet> C(*BoundGraph);
        VarGet = C.CreateNode(false);
        VarGet->VariableReference.SetSelfMember(FName(*VariableName));
        C.Finalize();
    }
    UEdGraphPin* VarOut = VarGet->GetValuePin();
    if (!VarOut) { return MCPAnimGraph_Error(FString::Printf(TEXT("Variable '%s' getter has no value pin (does the variable exist?)"), *VariableName)); }
    VarGet->NodePosX = SeqNode->NodePosX - 640; VarGet->NodePosY = SeqNode->NodePosY + 120;

    bool bWired = false;
    const bool bUseDivide = !FMath::IsNearlyEqual(Divisor, 1.0f) && Divisor != 0.0f;
    if (bUseDivide)
    {
        UK2Node_CallFunction* Div = nullptr;
        {
            FGraphNodeCreator<UK2Node_CallFunction> C(*BoundGraph);
            Div = C.CreateNode(false);
            Div->FunctionReference.SetExternalMember(FName(TEXT("Divide_DoubleDouble")), UKismetMathLibrary::StaticClass());
            C.Finalize();
        }
        UEdGraphPin* PinA = Div->FindPin(TEXT("A"), EGPD_Input);
        UEdGraphPin* PinB = Div->FindPin(TEXT("B"), EGPD_Input);
        UEdGraphPin* PinRet = Div->GetReturnValuePin();
        if (!PinA || !PinB || !PinRet) { return MCPAnimGraph_Error(TEXT("Divide node pins not found")); }
        PinB->DefaultValue = FString::SanitizeFloat(Divisor);
        Div->NodePosX = SeqNode->NodePosX - 320; Div->NodePosY = SeqNode->NodePosY + 120;
        const bool bA = MCPAnimGraph_Connect(BoundGraph, VarOut, PinA);
        const bool bR = MCPAnimGraph_Connect(BoundGraph, PinRet, RatePin);
        bWired = bA && bR;
    }
    else
    {
        // Divisor 1.0 -> drive the rate directly from the variable.
        bWired = MCPAnimGraph_Connect(BoundGraph, VarOut, RatePin);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state"), StateName);
    Obj->SetStringField(TEXT("expression"), bUseDivide ? FString::Printf(TEXT("%s / %s"), *VariableName, *FString::SanitizeFloat(Divisor)) : VariableName);
    Obj->SetNumberField(TEXT("divisor"), Divisor);
    Obj->SetBoolField(TEXT("pin_exposed"), true);
    Obj->SetBoolField(TEXT("wired"), bWired);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  C++ #60 — Blend Space Player in a state + montage Slot in the AnimGraph
//  (the last Companion-Game AnimGraph gap: finish ABP_Emberkit's blendspace
//   locomotion + a DefaultSlot so the AM_Emberkit_* montages can play).
// ===========================================================================
namespace
{
    // Ensure an anim-node input pin named PropName exists + is visible, exposing it
    // via the optional-pin array if it is hidden-by-default (e.g. PlayRate). X/Y on a
    // BlendSpacePlayer are PinShownByDefault so FindPin resolves them directly.
    UEdGraphPin* MCPAnimGraph_EnsurePinVisible(UAnimGraphNode_Base* Node, const TCHAR* PropName)
    {
        if (!Node) { return nullptr; }
        if (UEdGraphPin* Existing = Node->FindPin(PropName, EGPD_Input)) { return Existing; }
        int32 Index = INDEX_NONE;
        for (int32 i = 0; i < Node->ShowPinForProperties.Num(); ++i)
        {
            if (Node->ShowPinForProperties[i].PropertyName == FName(PropName)) { Index = i; break; }
        }
        if (Index == INDEX_NONE) { return nullptr; }
        Node->SetPinVisibility(true, Index);   // ANIMGRAPH_API — shows the pin + reconstructs
        return Node->FindPin(PropName, EGPD_Input);
    }

    // Drop a self-member VariableGet and wire its value into Node's input pin PinName
    // (exposing the pin if hidden). Empty VarName -> no-op (returns true: nothing asked).
    // Returns false only if the pin/variable could not be resolved/linked.
    bool MCPAnimGraph_BindVarToPin(UEdGraph* Graph, UAnimGraphNode_Base* Node,
                                   const TCHAR* PinName, const FString& VarName, int32 PosYOffset)
    {
        if (VarName.IsEmpty()) { return true; }
        UEdGraphPin* Pin = MCPAnimGraph_EnsurePinVisible(Node, PinName);
        if (!Pin) { return false; }

        UK2Node_VariableGet* VarGet = nullptr;
        {
            FGraphNodeCreator<UK2Node_VariableGet> C(*Graph);
            VarGet = C.CreateNode(false);
            VarGet->VariableReference.SetSelfMember(FName(*VarName));
            C.Finalize();
        }
        UEdGraphPin* VarOut = VarGet ? VarGet->GetValuePin() : nullptr;
        if (!VarOut) { return false; }
        VarGet->NodePosX = Node->NodePosX - 420;
        VarGet->NodePosY = Node->NodePosY + PosYOffset;

        Pin->BreakAllPinLinks();
        return MCPAnimGraph_Connect(Graph, VarOut, Pin);
    }
}

// 19) SetStateBlendSpace — fill a state's bound graph with a Blend Space Player wired to the state
//     Result, binding its X / Y / Play Rate pins to member variables. The set_state_anim counterpart
//     for UAnimGraphNode_BlendSpacePlayer (set_state_anim only makes a Sequence Player). Idempotent.
FString UMCPReflectionLibrary::SetStateBlendSpace(UAnimBlueprint* AnimBlueprint, const FString& MachineName,
                                                  const FString& StateName, const FString& BlendSpacePath,
                                                  const FString& XVariable, const FString& YVariable,
                                                  const FString& PlayRateVariable)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UAnimGraphNode_StateMachineBase* SM = MCPAnimGraph_FindStateMachine(AnimBlueprint, MachineName);
    if (!SM || !SM->EditorStateMachineGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State machine '%s' not found"), *MachineName)); }
    UAnimStateNode* State = MCPAnimGraph_FindState(SM->EditorStateMachineGraph, StateName);
    if (!State || !State->BoundGraph) { return MCPAnimGraph_Error(FString::Printf(TEXT("State '%s' not found"), *StateName)); }
    UEdGraph* BoundGraph = State->BoundGraph;

    UBlendSpace* BS = LoadObject<UBlendSpace>(nullptr, *BlendSpacePath);
    if (!BS) { return MCPAnimGraph_Error(FString::Printf(TEXT("Could not load blend space '%s'"), *BlendSpacePath)); }

    UAnimGraphNode_StateResult* ResultNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_StateResult>(BoundGraph);
    if (!ResultNode) { return MCPAnimGraph_Error(TEXT("State bound graph has no result node")); }
    UEdGraphPin* ResultIn = MCPAnimGraph_FindPosePin(ResultNode, EGPD_Input);
    if (!ResultIn) { return MCPAnimGraph_Error(TEXT("State result node has no input pose pin")); }

    BoundGraph->Modify();

    // Idempotent rebuild: reuse an existing Blend Space Player; drop any prior Sequence Player
    // (switching a state seq->blendspace) and any prior driver nodes (VariableGet / math calls).
    UAnimGraphNode_BlendSpacePlayer* BSNode = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_BlendSpacePlayer>(BoundGraph);
    {
        TArray<UEdGraphNode*> ToRemove;
        for (UEdGraphNode* N : BoundGraph->Nodes)
        {
            if (N->IsA<UAnimGraphNode_SequencePlayer>() ||
                N->IsA<UK2Node_VariableGet>() || N->IsA<UK2Node_CallFunction>())
            {
                ToRemove.Add(N);
            }
        }
        for (UEdGraphNode* N : ToRemove) { FBlueprintEditorUtils::RemoveNode(AnimBlueprint, N, /*bDontRecompile*/true); }
    }

    if (BSNode)
    {
        BSNode->Modify();
        BSNode->SetAnimationAsset(BS);             // virtual -> Node.SetBlendSpace (vtable dispatch)
        BSNode->CopySettingsFromAnimationAsset(BS); // sync bLoop from the asset
        BSNode->ReconstructNode();
    }
    else
    {
        FGraphNodeCreator<UAnimGraphNode_BlendSpacePlayer> Creator(*BoundGraph);
        BSNode = Creator.CreateNode(false);        // StaticClass() via MinimalAPI
        BSNode->SetAnimationAsset(BS);
        BSNode->CopySettingsFromAnimationAsset(BS);
        Creator.Finalize();
        BSNode->NodePosX = ResultNode->NodePosX - 360;
        BSNode->NodePosY = ResultNode->NodePosY;
    }

    UEdGraphPin* BSOut = MCPAnimGraph_FindPosePin(BSNode, EGPD_Output);
    if (!BSOut) { return MCPAnimGraph_Error(TEXT("Blend Space Player has no output pose pin")); }
    ResultIn->BreakAllPinLinks();
    const bool bConnected = MCPAnimGraph_Connect(BoundGraph, BSOut, ResultIn);

    // Bind the sample-coordinate + play-rate pins to member variables (each optional).
    BSNode->Modify();
    const bool bX    = MCPAnimGraph_BindVarToPin(BoundGraph, BSNode, TEXT("X"),        XVariable,        -40);
    const bool bY    = MCPAnimGraph_BindVarToPin(BoundGraph, BSNode, TEXT("Y"),        YVariable,         60);
    const bool bRate = MCPAnimGraph_BindVarToPin(BoundGraph, BSNode, TEXT("PlayRate"), PlayRateVariable, 160);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("state"), StateName);
    Obj->SetStringField(TEXT("blend_space"), BS->GetPathName());
    Obj->SetStringField(TEXT("blend_space_player_guid"), BSNode->NodeGuid.ToString());
    Obj->SetBoolField(TEXT("connected_to_result"), bConnected);
    if (!XVariable.IsEmpty())        { Obj->SetBoolField(TEXT("x_bound"), bX); }
    if (!YVariable.IsEmpty())        { Obj->SetBoolField(TEXT("y_bound"), bY); }
    if (!PlayRateVariable.IsEmpty()) { Obj->SetBoolField(TEXT("play_rate_bound"), bRate); }
    return MCPAnimGraph_SerializeObject(Obj);
}

// 20) InsertAnimSlot — splice a UAnimGraphNode_Slot (montage slot) between whatever currently feeds
//     the AnimGraph Output Pose and the Output Pose, so slot/montage playback (AM_* montages) layers
//     over the locomotion pose. Idempotent (reuses a slot node of the same SlotName).
FString UMCPReflectionLibrary::InsertAnimSlot(UAnimBlueprint* AnimBlueprint, const FString& SlotName)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UEdGraph* AnimGraph = MCPAnimGraph_FindAnimGraph(AnimBlueprint);
    if (!AnimGraph) { return MCPAnimGraph_Error(TEXT("Could not locate the AnimGraph on this Animation Blueprint")); }
    UAnimGraphNode_Root* Root = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_Root>(AnimGraph);
    if (!Root) { return MCPAnimGraph_Error(TEXT("AnimGraph has no Output Pose (root) node")); }
    UEdGraphPin* RootIn = MCPAnimGraph_FindPosePin(Root, EGPD_Input);
    if (!RootIn) { return MCPAnimGraph_Error(TEXT("Output Pose node has no input pose pin")); }

    const FName WantSlot = SlotName.IsEmpty() ? FName(TEXT("DefaultSlot")) : FName(*SlotName);

    // Whatever currently drives the Output Pose becomes the slot's Source input.
    UEdGraphPin* CurrentSource = (RootIn->LinkedTo.Num() > 0) ? RootIn->LinkedTo[0] : nullptr;

    AnimGraph->Modify();

    // Reuse an existing slot node with the same SlotName (idempotent re-run).
    UAnimGraphNode_Slot* SlotNode = nullptr;
    for (UEdGraphNode* N : AnimGraph->Nodes)
    {
        if (UAnimGraphNode_Slot* S = Cast<UAnimGraphNode_Slot>(N))
        {
            if (S->Node.SlotName == WantSlot) { SlotNode = S; break; }
        }
    }
    const bool bReused = (SlotNode != nullptr);
    if (SlotNode)
    {
        SlotNode->Modify();
    }
    else
    {
        FGraphNodeCreator<UAnimGraphNode_Slot> Creator(*AnimGraph);
        SlotNode = Creator.CreateNode(false);     // StaticClass() via MinimalAPI
        SlotNode->Node.SlotName = WantSlot;        // Source + SlotName are public UPROPERTYs
        Creator.Finalize();
        SlotNode->NodePosX = Root->NodePosX - 300;
        SlotNode->NodePosY = Root->NodePosY;
    }

    UEdGraphPin* SlotIn  = MCPAnimGraph_FindPosePin(SlotNode, EGPD_Input);
    UEdGraphPin* SlotOut = MCPAnimGraph_FindPosePin(SlotNode, EGPD_Output);
    if (!SlotIn || !SlotOut) { return MCPAnimGraph_Error(TEXT("Slot node has no pose pins")); }

    // Splice: [CurrentSource] -> Slot.Source ; Slot.Out -> Output Pose.
    // Skip the source rewire if the slot is already the thing feeding Output (re-run).
    bool bSourceWired = true;
    const bool bAlreadySpliced = (CurrentSource && CurrentSource->GetOwningNode() == SlotNode);
    RootIn->BreakAllPinLinks();
    if (CurrentSource && !bAlreadySpliced)
    {
        SlotIn->BreakAllPinLinks();
        bSourceWired = MCPAnimGraph_Connect(AnimGraph, CurrentSource, SlotIn);
    }
    const bool bOutputWired = MCPAnimGraph_Connect(AnimGraph, SlotOut, RootIn);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("slot_name"), WantSlot.ToString());
    Obj->SetStringField(TEXT("slot_node_guid"), SlotNode->NodeGuid.ToString());
    Obj->SetBoolField(TEXT("reused_existing"), bReused);
    Obj->SetBoolField(TEXT("source_wired"), bSourceWired);
    Obj->SetBoolField(TEXT("output_wired"), bOutputWired);
    Obj->SetBoolField(TEXT("had_prior_source"), CurrentSource != nullptr);
    return MCPAnimGraph_SerializeObject(Obj);
}

// ===========================================================================
//  C++ #61 — InsertAnimGraphNode: splice a general overlay node (Aim Offset
//  Player, Layered Blend Per Bone, or any UAnimGraphNode_Base) between whatever
//  feeds the AnimGraph Output Pose and the Output Pose. The missing counterpart
//  to insert_anim_slot for non-slot nodes.
// ===========================================================================
namespace
{
    UEdGraphPin* MCPAnimGraph_FindInputPosePinByName(UEdGraphNode* Node, const TCHAR* Name)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && UAnimationGraphSchema::IsPosePin(P->PinType) && P->PinName == FName(Name))
            {
                return P;
            }
        }
        return nullptr;
    }

    void MCPAnimGraph_CollectInputPosePins(UEdGraphNode* Node, TArray<UEdGraphPin*>& Out)
    {
        if (!Node) { return; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && UAnimationGraphSchema::IsPosePin(P->PinType)) { Out.Add(P); }
        }
    }

    // Resolve a friendly name / class path to a UAnimGraphNode_Base subclass.
    UClass* MCPAnimGraph_ResolveNodeClass(const FString& NameOrPath)
    {
        const FString N = NameOrPath.ToLower();
        if (N == TEXT("aimoffset") || N == TEXT("aimoffsetplayer") || N == TEXT("rotationoffsetblendspace"))
        {
            return UAnimGraphNode_RotationOffsetBlendSpace::StaticClass();
        }
        if (N == TEXT("layeredboneblend") || N == TEXT("layeredblendperbone") || N == TEXT("layeredblend"))
        {
            return UAnimGraphNode_LayeredBoneBlend::StaticClass();
        }
        if (N == TEXT("blendspaceplayer") || N == TEXT("blendspace"))
        {
            return UAnimGraphNode_BlendSpacePlayer::StaticClass();
        }
        if (N == TEXT("slot")) { return UAnimGraphNode_Slot::StaticClass(); }
        // Full class path, e.g. "/Script/AnimGraph.AnimGraphNode_ApplyAdditive".
        if (UClass* ByPath = LoadObject<UClass>(nullptr, *NameOrPath)) { return ByPath; }
        // Bare class name.
        if (UClass* ByName = FindFirstObject<UClass>(*NameOrPath, EFindFirstObjectOptions::NativeFirst)) { return ByName; }
        return nullptr;
    }
}

// 21) InsertAnimGraphNode — create a node of NodeClass, set its Node-struct properties (and asset, for
//     asset-player nodes), optionally configure a Layered-Blend branch filter + feed a blend pose from
//     another node, splice it before the Output Pose, and bind input pins to member variables.
FString UMCPReflectionLibrary::InsertAnimGraphNode(UAnimBlueprint* AnimBlueprint, const FString& NodeClass,
                                                   const FString& PropertiesJson, const FString& PinBindingsJson,
                                                   const FString& BranchFilterBone, int32 BranchFilterDepth,
                                                   const FString& BlendSourceNodeGuid)
{
    if (!AnimBlueprint) { return MCPAnimGraph_Error(TEXT("AnimBlueprint is null")); }
    UEdGraph* AnimGraph = MCPAnimGraph_FindAnimGraph(AnimBlueprint);
    if (!AnimGraph) { return MCPAnimGraph_Error(TEXT("Could not locate the AnimGraph on this Animation Blueprint")); }
    UAnimGraphNode_Root* Root = MCPAnimGraph_FirstNodeOfType<UAnimGraphNode_Root>(AnimGraph);
    if (!Root) { return MCPAnimGraph_Error(TEXT("AnimGraph has no Output Pose (root) node")); }
    UEdGraphPin* RootIn = MCPAnimGraph_FindPosePin(Root, EGPD_Input);
    if (!RootIn) { return MCPAnimGraph_Error(TEXT("Output Pose node has no input pose pin")); }

    UClass* Cls = MCPAnimGraph_ResolveNodeClass(NodeClass);
    if (!Cls) { return MCPAnimGraph_Error(FString::Printf(TEXT("Could not resolve node class '%s'"), *NodeClass)); }
    if (!Cls->IsChildOf(UAnimGraphNode_Base::StaticClass()))
    {
        return MCPAnimGraph_Error(FString::Printf(TEXT("'%s' is not a UAnimGraphNode_Base subclass"), *Cls->GetName()));
    }

    // Whatever currently drives the Output Pose becomes the new node's base-pose input.
    UEdGraphPin* CurrentSource = (RootIn->LinkedTo.Num() > 0) ? RootIn->LinkedTo[0] : nullptr;

    AnimGraph->Modify();

    // UEdGraph::CreateNode is protected (FGraphNodeCreator-only friend); replicate it for a runtime UClass
    // via NewObject + the public AddNode, then the same Finalize steps FGraphNodeCreator runs.
    UAnimGraphNode_Base* Node = NewObject<UAnimGraphNode_Base>(AnimGraph, Cls, NAME_None, RF_Transactional);
    if (!Node) { return MCPAnimGraph_Error(TEXT("Node creation failed")); }
    AnimGraph->AddNode(Node, /*bUserAction*/ false, /*bSelectNewNode*/ false);  // ENGINE_API
    Node->CreateNewGuid();
    Node->PostPlacedNewNode();
    if (Node->Pins.Num() == 0) { Node->AllocateDefaultPins(); }
    Node->NodePosX = Root->NodePosX - 300;
    Node->NodePosY = Root->NodePosY;

    // --- Properties (onto the embedded "Node" FStructProperty) + special "asset" key. ---
    TSharedPtr<FJsonObject> Props;
    if (!PropertiesJson.IsEmpty())
    {
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(PropertiesJson);
        FJsonSerializer::Deserialize(R, Props);
    }
    FString AssetApplied;
    if (Props.IsValid())
    {
        FString AssetPath;
        if (Props->TryGetStringField(TEXT("asset"), AssetPath) && !AssetPath.IsEmpty())
        {
            if (UAnimGraphNode_AssetPlayerBase* AP = Cast<UAnimGraphNode_AssetPlayerBase>(Node))
            {
                if (UAnimationAsset* A = LoadObject<UAnimationAsset>(nullptr, *AssetPath))
                {
                    AP->SetAnimationAsset(A);   // virtual (vtable)
                    AssetApplied = A->GetPathName();
                }
            }
        }
        FStructProperty* NodeProp = FindFProperty<FStructProperty>(Node->GetClass(), TEXT("Node"));
        if (NodeProp && NodeProp->Struct)
        {
            void* NodeData = NodeProp->ContainerPtrToValuePtr<void>(Node);
            for (const TPair<FString, TSharedPtr<FJsonValue>>& KV : Props->Values)
            {
                if (KV.Key == TEXT("asset")) { continue; }
                FProperty* Sub = FindFProperty<FProperty>(NodeProp->Struct, FName(*KV.Key));
                if (!Sub) { continue; }
                FString ValStr;
                if (!KV.Value->TryGetString(ValStr))
                {
                    if (KV.Value->Type == EJson::Boolean) { ValStr = KV.Value->AsBool() ? TEXT("true") : TEXT("false"); }
                    else if (KV.Value->Type == EJson::Number) { ValStr = FString::SanitizeFloat(KV.Value->AsNumber()); }
                }
                if (!ValStr.IsEmpty())
                {
                    Sub->ImportText_Direct(*ValStr, Sub->ContainerPtrToValuePtr<void>(NodeData), Node, PPF_None);
                }
            }
        }
    }

    // --- Layered Blend Per Bone: ensure a blend layer + set its branch filter. ---
    bool bBranchFilterSet = false;
    if (UAnimGraphNode_LayeredBoneBlend* LB = Cast<UAnimGraphNode_LayeredBoneBlend>(Node))
    {
        if (!BranchFilterBone.IsEmpty())
        {
            LB->Node.BlendMode = ELayeredBoneBlendMode::BranchFilter;
            if (LB->Node.BlendPoses.Num() == 0)
            {
                LB->AddPinToBlendByFilter();   // ANIMGRAPH_API — grows BlendPoses/LayerSetup/BlendWeights + a pin
            }
            if (LB->Node.LayerSetup.Num() > 0)
            {
                FBranchFilter Filter;
                Filter.BoneName = FName(*BranchFilterBone);
                Filter.BlendDepth = BranchFilterDepth;
                LB->Node.LayerSetup[0].BranchFilters.Reset();
                LB->Node.LayerSetup[0].BranchFilters.Add(Filter);
                bBranchFilterSet = true;
            }
        }
    }

    Node->ReconstructNode();

    // --- Splice into the output chain. ---
    UEdGraphPin* BaseIn = MCPAnimGraph_FindInputPosePinByName(Node, TEXT("BasePose"));
    if (!BaseIn) { BaseIn = MCPAnimGraph_FindInputPosePinByName(Node, TEXT("Source")); }
    TArray<UEdGraphPin*> InputPosePins;
    MCPAnimGraph_CollectInputPosePins(Node, InputPosePins);
    if (!BaseIn && InputPosePins.Num() > 0) { BaseIn = InputPosePins[0]; }
    UEdGraphPin* NodeOut = MCPAnimGraph_FindPosePin(Node, EGPD_Output);
    if (!BaseIn || !NodeOut) { return MCPAnimGraph_Error(TEXT("Node has no usable base-pose input / output pose pin")); }

    RootIn->BreakAllPinLinks();
    bool bBaseWired = true;
    const bool bAlreadyThis = (CurrentSource && CurrentSource->GetOwningNode() == Node);
    if (CurrentSource && !bAlreadyThis)
    {
        BaseIn->BreakAllPinLinks();
        bBaseWired = MCPAnimGraph_Connect(AnimGraph, CurrentSource, BaseIn);
    }
    const bool bOutWired = MCPAnimGraph_Connect(AnimGraph, NodeOut, RootIn);

    // --- Optional: feed a blend pose from another node (e.g. an Aim Offset -> this Layered Blend). ---
    bool bBlendSourceWired = false;
    FString BlendSourceResolved;
    if (!BlendSourceNodeGuid.IsEmpty())
    {
        FGuid Guid;
        if (FGuid::Parse(BlendSourceNodeGuid, Guid))
        {
            if (UAnimGraphNode_Base* SrcNode = MCPAnimGraph_FindNodeByGuid(AnimBlueprint, Guid))
            {
                UEdGraphPin* SrcOut = MCPAnimGraph_FindPosePin(SrcNode, EGPD_Output);
                // The blend pose is an input pose pin that is NOT the base pose pin.
                UEdGraphPin* BlendIn = nullptr;
                for (UEdGraphPin* P : InputPosePins) { if (P != BaseIn) { BlendIn = P; break; } }
                if (SrcOut && BlendIn)
                {
                    BlendIn->BreakAllPinLinks();
                    bBlendSourceWired = MCPAnimGraph_Connect(AnimGraph, SrcOut, BlendIn);
                    BlendSourceResolved = SrcNode->NodeGuid.ToString();
                }
            }
        }
    }

    // --- Bind input pins (X / Y / Alpha / ...) to member variables. ---
    TArray<FString> BoundPins;
    if (!PinBindingsJson.IsEmpty())
    {
        TSharedPtr<FJsonObject> Bind;
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(PinBindingsJson);
        if (FJsonSerializer::Deserialize(R, Bind) && Bind.IsValid())
        {
            Node->Modify();
            int32 Offset = -40;
            for (const TPair<FString, TSharedPtr<FJsonValue>>& KV : Bind->Values)
            {
                FString VarName;
                if (KV.Value->TryGetString(VarName) && !VarName.IsEmpty())
                {
                    if (MCPAnimGraph_BindVarToPin(AnimGraph, Node, *KV.Key, VarName, Offset))
                    {
                        BoundPins.Add(FString::Printf(TEXT("%s=%s"), *KV.Key, *VarName));
                    }
                    Offset += 100;
                }
            }
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBlueprint);

    TSharedRef<FJsonObject> Obj = MCPAnimGraph_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBlueprint->GetName());
    Obj->SetStringField(TEXT("node_class"), Cls->GetName());
    Obj->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Obj->SetBoolField(TEXT("base_wired"), bBaseWired);
    Obj->SetBoolField(TEXT("output_wired"), bOutWired);
    Obj->SetBoolField(TEXT("had_prior_source"), CurrentSource != nullptr);
    if (!AssetApplied.IsEmpty()) { Obj->SetStringField(TEXT("asset"), AssetApplied); }
    if (bBranchFilterSet) { Obj->SetStringField(TEXT("branch_filter_bone"), BranchFilterBone); Obj->SetNumberField(TEXT("branch_filter_depth"), BranchFilterDepth); }
    if (!BlendSourceNodeGuid.IsEmpty()) { Obj->SetBoolField(TEXT("blend_source_wired"), bBlendSourceWired); if (!BlendSourceResolved.IsEmpty()) { Obj->SetStringField(TEXT("blend_source_guid"), BlendSourceResolved); } }
    if (BoundPins.Num() > 0)
    {
        TArray<TSharedPtr<FJsonValue>> B;
        for (const FString& S : BoundPins) { B.Add(MakeShared<FJsonValueString>(S)); }
        Obj->SetArrayField(TEXT("bound_pins"), B);
    }
    return MCPAnimGraph_SerializeObject(Obj);
}
