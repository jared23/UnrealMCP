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
