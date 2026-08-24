// ============================================================================
// MCPReflection_AnimRemove.cpp — AnimGraph state-machine + layer REMOVERS
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18, NOT YET COMPILED. Companion / isolated
// translation unit to MCPReflection_AnimGraph.cpp. Supplies the two FAITHFUL
// undo inverses that anim_statemachine_write.py flagged DEFERRED:
//
//   anim_add_state_machine  (AddAnimStateMachine)  -> RemoveAnimStateMachineNode
//   anim_add_layer          (AddAnimLayer)         -> RemoveAnimLayerNode
//
// Both ADDs live in MCPReflection_AnimGraph.cpp:
//   * AddAnimStateMachine places a UAnimGraphNode_StateMachine in the AnimGraph
//     and renames its EditorStateMachineGraph to MachineName.
//   * AddAnimLayer creates a UAnimationGraph via FBlueprintEditorUtils::CreateNewGraph
//     + AddDomainSpecificGraph, which appends it to AnimBlueprint->FunctionGraphs
//     (VERIFIED vs engine source: AddDomainSpecificGraph does `FunctionGraphs.Add(Graph)`).
//     >>> So an "anim layer" here is a GRAPH in FunctionGraphs — NOT a
//         UAnimGraphNode_LinkedAnimLayer node. The task asked me to VERIFY the node
//         class; the verification result is: the thing add_anim_layer creates is a
//         UAnimationGraph (domain-specific function graph), so the faithful inverse
//         is FBlueprintEditorUtils::RemoveGraph on that named graph. The
//         UAnimGraphNode_LinkedAnimLayer class is a DIFFERENT concept (a node that
//         *calls* a layer) and is intentionally NOT used here.
//
// LINK RISK (same profile as MCPReflection_AnimGraph.cpp):
//   * UAnimGraphNode_StateMachineBase::StaticClass()  — exported by MinimalAPI.
//   * UAnimGraphNode_StateMachineBase::DestroyNode()  — ANIMGRAPH_API AND virtual;
//     called through a UEdGraphNode* base pointer so it dispatches via vtable (no
//     direct import symbol needed). VERIFIED vs engine source: its body nulls
//     EditorStateMachineGraph then FBlueprintEditorUtils::RemoveGraph(...,Recompile),
//     i.e. it removes the sub-graph AND recompiles — the exact inverse of the add.
//   * FBlueprintEditorUtils::RemoveGraph / MarkBlueprintAsStructurallyModified —
//     UNREALED_API (UnrealEd, already a dep).
//   * UEdGraphSchema_K2::GN_AnimGraph — BlueprintGraph (already a dep).
//
// Build.cs: NO additions. Confirmed present in UnrealMCP.Build.cs:
//   Engine, UnrealEd, BlueprintGraph (PublicDependencyModuleNames) and
//   AnimGraph, AnimGraphRuntime (PrivateDependencyModuleNames).
//
// Anon-namespace helpers are prefixed MCPAnimRm_ (unique across the unity build).
// JSON shape: {"status":"success","removed":"<name>",...} / {"status":"error","error":"..."}.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Anim / Blueprint / graph ---
#include "Animation/AnimBlueprint.h"                // UAnimBlueprint (Engine)

#if WITH_EDITOR
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraphSchema_K2.h"                        // UEdGraphSchema_K2::GN_AnimGraph (BlueprintGraph)
#include "Kismet2/BlueprintEditorUtils.h"           // FBlueprintEditorUtils (UnrealEd)
#include "AnimGraphNode_StateMachineBase.h"         // UAnimGraphNode_StateMachineBase (AnimGraph editor)
#include "AnimationStateMachineGraph.h"             // UAnimationStateMachineGraph (EditorStateMachineGraph type)
#endif

namespace
{
    FString MCPAnimRm_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // {"status":"error","error":<message>} — matches the sibling MCPReflection_AnimGraph
    // error field name and the Python _is_err() check (status=="error" OR bool(error)).
    FString MCPAnimRm_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("error"));
        Obj->SetStringField(TEXT("error"), Message);
        return MCPAnimRm_Serialize(Obj);
    }

    TSharedRef<FJsonObject> MCPAnimRm_Ok()
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("success"));
        return Obj;
    }

#if WITH_EDITOR
    // Every graph reachable from the AnimBlueprint (top-level AnimGraph + layer/function
    // graphs + ubergraphs + all nested children). Mirrors MCPAnimGraph_CollectAllGraphs.
    void MCPAnimRm_CollectAllGraphs(UAnimBlueprint* AnimBP, TArray<UEdGraph*>& Out)
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
#endif // WITH_EDITOR
}

// ===========================================================================
//  RemoveAnimStateMachineNode — inverse of AddAnimStateMachine.
//  Finds the UAnimGraphNode_StateMachineBase whose EditorStateMachineGraph is
//  named MachineName (case-insensitive) and DestroyNode()s it. DestroyNode
//  removes the EditorStateMachineGraph sub-graph and recompiles (verified above).
// ===========================================================================
FString UMCPReflectionLibrary::RemoveAnimStateMachineNode(UAnimBlueprint* AnimBP, const FString& MachineName)
{
#if WITH_EDITOR
    if (!AnimBP) { return MCPAnimRm_Error(TEXT("AnimBlueprint is null")); }
    if (MachineName.IsEmpty()) { return MCPAnimRm_Error(TEXT("MachineName is empty")); }

    // Collect ALL matching state-machine nodes so we can report ambiguity.
    TArray<UAnimGraphNode_StateMachineBase*> Matches;
    {
        TArray<UEdGraph*> Graphs;
        MCPAnimRm_CollectAllGraphs(AnimBP, Graphs);
        for (UEdGraph* Graph : Graphs)
        {
            if (!Graph) { continue; }
            for (UEdGraphNode* Node : Graph->Nodes)
            {
                // VERIFY vs engine source (AnimGraphNode_StateMachineBase.h): the state-machine
                // node class is UAnimGraphNode_StateMachineBase (UAnimGraphNode_StateMachine is the
                // concrete subclass AddAnimStateMachine spawns); the name lives on the bound
                // UAnimationStateMachineGraph in the public UPROPERTY EditorStateMachineGraph — NOT
                // a "StateMachineName" field on the node (there is none; the node exposes
                // GetStateMachineName() which returns EditorStateMachineGraph->GetName()).
                if (UAnimGraphNode_StateMachineBase* SM = Cast<UAnimGraphNode_StateMachineBase>(Node))
                {
                    if (SM->EditorStateMachineGraph &&
                        SM->EditorStateMachineGraph->GetName().Equals(MachineName, ESearchCase::IgnoreCase))
                    {
                        Matches.Add(SM);
                    }
                }
            }
        }
    }

    if (Matches.Num() == 0)
    {
        return MCPAnimRm_Error(FString::Printf(TEXT("State machine node named '%s' not found"), *MachineName));
    }

    // Ambiguous names: remove the FIRST match and say so (documented in the task).
    UAnimGraphNode_StateMachineBase* Target = Matches[0];
    const FString RemovedGuid = Target->NodeGuid.ToString();
    const FString ResolvedName = Target->EditorStateMachineGraph
        ? Target->EditorStateMachineGraph->GetName() : MachineName;

    AnimBP->Modify();
    if (UEdGraph* ParentGraph = Target->GetGraph())
    {
        ParentGraph->Modify();
    }

    // DestroyNode() (ANIMGRAPH_API virtual via UEdGraphNode* vtable): nulls
    // EditorStateMachineGraph then FBlueprintEditorUtils::RemoveGraph(...,Recompile).
    Target->DestroyNode();

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBP);

    TSharedRef<FJsonObject> Obj = MCPAnimRm_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBP->GetName());
    Obj->SetStringField(TEXT("removed"), ResolvedName);
    Obj->SetStringField(TEXT("node_guid"), RemovedGuid);
    if (Matches.Num() > 1)
    {
        Obj->SetBoolField(TEXT("ambiguous"), true);
        Obj->SetNumberField(TEXT("match_count"), Matches.Num());
        Obj->SetStringField(TEXT("note"),
            TEXT("Multiple state-machine nodes shared this name; removed the first match."));
    }
    return MCPAnimRm_Serialize(Obj);
#else
    return MCPAnimRm_Error(TEXT("RemoveAnimStateMachineNode is editor-only"));
#endif // WITH_EDITOR
}

// ===========================================================================
//  RemoveAnimLayerNode — inverse of AddAnimLayer.
//  AddAnimLayer appended a UAnimationGraph named LayerName to FunctionGraphs;
//  this removes that named graph via FBlueprintEditorUtils::RemoveGraph(Recompile).
// ===========================================================================
FString UMCPReflectionLibrary::RemoveAnimLayerNode(UAnimBlueprint* AnimBP, const FString& LayerName)
{
#if WITH_EDITOR
    if (!AnimBP) { return MCPAnimRm_Error(TEXT("AnimBlueprint is null")); }
    if (LayerName.IsEmpty()) { return MCPAnimRm_Error(TEXT("LayerName is empty")); }

    // Search FunctionGraphs (where AddDomainSpecificGraph placed the layer — VERIFIED
    // vs engine source). Collect matches to report ambiguity.
    TArray<UEdGraph*> Matches;
    for (UEdGraph* Graph : AnimBP->FunctionGraphs)
    {
        if (!Graph) { continue; }
        if (Graph->GetName().Equals(LayerName, ESearchCase::IgnoreCase))
        {
            Matches.Add(Graph);
        }
    }

    if (Matches.Num() == 0)
    {
        return MCPAnimRm_Error(FString::Printf(TEXT("Anim layer graph named '%s' not found"), *LayerName));
    }

    UEdGraph* Target = Matches[0];

    // Safety: never nuke the AnimBlueprint's primary AnimGraph. AddAnimLayer refuses a
    // layer named "AnimGraph" (duplicate guard), so this only triggers on a bad caller.
    if (Target->GetFName() == UEdGraphSchema_K2::GN_AnimGraph)
    {
        return MCPAnimRm_Error(TEXT("Refusing to remove the primary 'AnimGraph' graph"));
    }

    const FString ResolvedName = Target->GetName();

    AnimBP->Modify();
    Target->Modify();

    // RemoveGraph(...,Recompile): removes the graph from FunctionGraphs and recompiles —
    // the faithful inverse of CreateNewGraph + AddDomainSpecificGraph.
    FBlueprintEditorUtils::RemoveGraph(AnimBP, Target, EGraphRemoveFlags::Recompile);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(AnimBP);

    TSharedRef<FJsonObject> Obj = MCPAnimRm_Ok();
    Obj->SetStringField(TEXT("anim_blueprint"), AnimBP->GetName());
    Obj->SetStringField(TEXT("removed"), ResolvedName);
    if (Matches.Num() > 1)
    {
        Obj->SetBoolField(TEXT("ambiguous"), true);
        Obj->SetNumberField(TEXT("match_count"), Matches.Num());
        Obj->SetStringField(TEXT("note"),
            TEXT("Multiple layer graphs shared this name; removed the first match."));
    }
    return MCPAnimRm_Serialize(Obj);
#else
    return MCPAnimRm_Error(TEXT("RemoveAnimLayerNode is editor-only"));
#endif // WITH_EDITOR
}
