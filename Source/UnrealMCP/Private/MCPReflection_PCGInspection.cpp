// UnrealMCP — PCG GRAPH-EXECUTION INSPECTION reads (C++ #51, 2026-08-20). The ~13-feature blocked group.
//
// The old note ("needs a PCG_PROFILING_ENABLED profiling engine build") was WRONG: PCGCommon.h #defines
// PCG_PROFILING_ENABLED to 1 whenever WITH_EDITOR (|| !UE_BUILD_SHIPPING) -> already on in our editor build.
// So FPCGGraphExecutionInspection is a real, PCG_API-exported class and every method below is reachable.
//
// ACCESS ROUTE (verified vs engine source, UE 5.8):
//   UPCGComponent : public UActorComponent, public IPCGGraphExecutionSource   (PCGComponent.h:112)
//   UPCGComponent::GetExecutionState() -> IPCGGraphExecutionState& (public, PCGComponent.h:128)
//   IPCGGraphExecutionState::GetInspection() -> FPCGGraphExecutionInspection&  (PCGGraphExecutionStateInterface.h:189)
//   FPCGGraphExecutionInspection : all methods PCG_API (PCGGraphExecutionInspection.h). The editor reads
//   (WasNodeExecuted / HasNodeProducedData / GetNodeInactivePinMask / DidNodeTrigger{GPUToCPU,CPUToGPU} /
//   NodeAppliedDataOverrides / InspectData) live under #if WITH_EDITOR inside the (PCG_PROFILING_ENABLED) class.
//
// GetExecutedNodeStacks() HANDS us TMap<TObjectKey<const UPCGNode>, TSet<FNodeExecutedNotificationData>> after a
// generation -> iterate to get every (UPCGNode*, FPCGStack) pair. We NEVER construct an FPCGStack (the key insight:
// the map is the source of truth for the stacks to query). Node addressing key = UPCGNode::GetName() (matches the
// pcg_write/pcg_schema resolve convention); node_type = GetSettings()->GetClass()->GetName() for readability.
//
// FOUR handlers (FString/bool-only across the .h boundary — block #51 in MCPReflectionLibrary.h):
//   1) SetPCGInspectionEnabledJson(ActorPath, bEnable) -> Insp.EnableInspection()/DisableInspection(); read back
//      IsInspecting(). NOTE: EnableInspection/DisableInspection are a REFERENCE-COUNTED pair (InspectionCounter);
//      one enable needs one disable. The Python wrapper treats enable/disable as the natural inverse pair.
//   2) GetPCGInspectionJson(ActorPath, NodeName_opt) -> enumerate GetExecutedNodeStacks(); per (node[, name-filter],
//      stack): {node_name, node_type, stack, executed, produced_data, inactive_pin_mask, gpu_to_cpu_readback,
//      cpu_to_gpu_upload, data_overrides_applied}. Covers ~7 of the tool verbs in one call.
//   3) InspectPCGNodeOutputJson(ActorPath, NodeName) -> find the node's stack, InspectData(stack, lambda) ->
//      serialize the FPCGDataCollection (tagged_data_count + per entry {data_class, pin, tags, num_points}).
//   4) ClearPCGInspectionJson(ActorPath) -> Insp.ClearInspectionData(true).
//
// REVERSIBILITY: ALL transient runtime reads / enable-disable-clear -> NON-LEDGERED. NO editor_level.undo folds
// (this round adds ZERO undo risk — unlike the PCG schema waves). LINKAGE: Build.cs already has "PCG" (Wave-5),
// UnrealEd (public dep) for GEditor/GetActorLabel -> NO Build.cs change, NO engine export patch.
//
// CRASH-SAFETY: every actor/component/inspection touch is null-guarded; the map iteration resolves TObjectKeys and
// skips nulls; all bodies are #if WITH_EDITOR (which guarantees PCG_PROFILING_ENABLED==1 -> the class is defined).
// Anonymous-namespace helpers prefixed MCPPcgI_ to stay unique in the module unity build.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "CoreMinimal.h"
#include "UObject/ObjectKey.h"          // TObjectKey<>::ResolveObjectPtr
#include "GameFramework/Actor.h"
#include "Engine/World.h"
#include "EngineUtils.h"                // TActorIterator
#include "Editor.h"                     // GEditor

#include "PCGComponent.h"               // UPCGComponent (GetExecutionState)
#include "PCGGraphExecutionStateInterface.h" // IPCGGraphExecutionState::GetInspection
#include "PCGGraphExecutionInspection.h"     // FPCGGraphExecutionInspection (PCG_PROFILING_ENABLED)
#include "PCGNode.h"                    // UPCGNode (GetName / GetSettings / GetOutputPins)
#include "PCGPin.h"                     // UPCGPin (Properties.Label)
#include "PCGSettings.h"                // UPCGSettings
#include "PCGData.h"                    // FPCGDataCollection / FPCGTaggedData
#include "Data/PCGBasePointData.h"      // UPCGBasePointData::GetNumPoints
#include "Graph/PCGStackContext.h"      // FPCGStack::CreateStackFramePath

namespace
{
    FString MCPPcgI_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths key off res.get("error").
    FString MCPPcgI_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPPcgI_Serialize(Root);
    }

#if WITH_EDITOR
    // Resolve an actor in the editor world by path/name/label, then its UPCGComponent (Wave-4 addressing).
    UPCGComponent* MCPPcgI_ResolveComp(const FString& ActorPath, FString& OutErr, AActor** OutActor)
    {
        if (OutActor) { *OutActor = nullptr; }
        UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
        if (!World)
        {
            OutErr = TEXT("no editor world");
            return nullptr;
        }
        if (ActorPath.IsEmpty())
        {
            OutErr = TEXT("actor path is empty");
            return nullptr;
        }
        AActor* Found = nullptr;
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            AActor* A = *It;
            if (!A || !IsValid(A))
            {
                continue;
            }
            if (A->GetPathName() == ActorPath || A->GetName() == ActorPath || A->GetActorLabel() == ActorPath)
            {
                Found = A;
                break;
            }
        }
        if (!Found)
        {
            OutErr = FString::Printf(TEXT("actor not found in editor world: %s"), *ActorPath);
            return nullptr;
        }
        if (OutActor) { *OutActor = Found; }
        UPCGComponent* Comp = Found->FindComponentByClass<UPCGComponent>();
        if (!Comp)
        {
            OutErr = FString::Printf(TEXT("no UPCGComponent on actor: %s"), *ActorPath);
            return nullptr;
        }
        return Comp;
    }

    FString MCPPcgI_NodeType(const UPCGNode* Node)
    {
        if (!Node)
        {
            return TEXT("");
        }
        if (const UPCGSettings* S = Node->GetSettings())
        {
            if (S->GetClass())
            {
                return S->GetClass()->GetName();
            }
        }
        return TEXT("");
    }

    // Fill the per-(node,stack) inspection record. Insp is non-const (the query methods are const-correct anyway).
    void MCPPcgI_FillNodeStack(const TSharedRef<FJsonObject>& E, FPCGGraphExecutionInspection& Insp,
        const UPCGNode* Node, const FPCGStack& Stack)
    {
        E->SetStringField(TEXT("node_name"), Node->GetName());
        E->SetStringField(TEXT("node_type"), MCPPcgI_NodeType(Node));
        FString StackStr;
        Stack.CreateStackFramePath(StackStr, Node);
        E->SetStringField(TEXT("stack"), StackStr);
        E->SetBoolField(TEXT("executed"), Insp.WasNodeExecuted(Node, Stack));
        E->SetBoolField(TEXT("produced_data"), Insp.HasNodeProducedData(Node, Stack));
        E->SetNumberField(TEXT("inactive_pin_mask"), static_cast<double>(Insp.GetNodeInactivePinMask(Node, Stack)));
        E->SetBoolField(TEXT("gpu_to_cpu_readback"), Insp.DidNodeTriggerGPUToCPUReadback(Node, Stack));
        E->SetBoolField(TEXT("cpu_to_gpu_upload"), Insp.DidNodeTriggerCPUToGPUUpload(Node, Stack));
        E->SetBoolField(TEXT("data_overrides_applied"), Insp.NodeAppliedDataOverrides(Node, Stack));
    }
#endif // WITH_EDITOR
}

// =====================================================================================================
// SetPCGInspectionEnabledJson — enable/disable inspection on the component (must be ON before generate).
// =====================================================================================================
FString UMCPReflectionLibrary::SetPCGInspectionEnabledJson(const FString& ActorPath, bool bEnable)
{
#if WITH_EDITOR
    FString Err;
    AActor* Actor = nullptr;
    UPCGComponent* Comp = MCPPcgI_ResolveComp(ActorPath, Err, &Actor);
    if (!Comp)
    {
        return MCPPcgI_Err(Err);
    }
    FPCGGraphExecutionInspection& Insp = Comp->GetExecutionState().GetInspection();
    if (bEnable)
    {
        Insp.EnableInspection();
    }
    else
    {
        Insp.DisableInspection();
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor"), Actor ? Actor->GetPathName() : ActorPath);
    Root->SetBoolField(TEXT("requested_enable"), bEnable);
    Root->SetBoolField(TEXT("is_inspecting"), Insp.IsInspecting());
    return MCPPcgI_Serialize(Root);
#else
    return MCPPcgI_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// GetPCGInspectionJson — enumerate executed (node, stack) pairs + their per-node inspection flags.
// NodeName empty -> all nodes. Covers was_executed / has_produced_data / inactive_pin_mask / gpu-cpu /
// cpu-gpu / data_overrides_applied / list_executed_nodes in one call.
// =====================================================================================================
FString UMCPReflectionLibrary::GetPCGInspectionJson(const FString& ActorPath, const FString& NodeName)
{
#if WITH_EDITOR
    FString Err;
    AActor* Actor = nullptr;
    UPCGComponent* Comp = MCPPcgI_ResolveComp(ActorPath, Err, &Actor);
    if (!Comp)
    {
        return MCPPcgI_Err(Err);
    }
    FPCGGraphExecutionInspection& Insp = Comp->GetExecutionState().GetInspection();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor"), Actor ? Actor->GetPathName() : ActorPath);
    Root->SetBoolField(TEXT("is_inspecting"), Insp.IsInspecting());
    Root->SetNumberField(TEXT("executed_stacks_generation"), static_cast<double>(Insp.GetExecutedStacksGeneration()));

    TArray<TSharedPtr<FJsonValue>> Nodes;
    int32 DistinctNodes = 0;
    TMap<TObjectKey<const UPCGNode>, TSet<FPCGGraphExecutionInspection::FNodeExecutedNotificationData>> Stacks
        = Insp.GetExecutedNodeStacks();
    for (const TPair<TObjectKey<const UPCGNode>, TSet<FPCGGraphExecutionInspection::FNodeExecutedNotificationData>>& Pair : Stacks)
    {
        const UPCGNode* Node = Pair.Key.ResolveObjectPtr();
        if (!Node)
        {
            continue;
        }
        const FString NName = Node->GetName();
        if (!NodeName.IsEmpty() && NName != NodeName)
        {
            continue;
        }
        ++DistinctNodes;
        for (const FPCGGraphExecutionInspection::FNodeExecutedNotificationData& Notif : Pair.Value)
        {
            TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
            MCPPcgI_FillNodeStack(E, Insp, Node, Notif.Stack);
            Nodes.Add(MakeShared<FJsonValueObject>(E));
        }
    }
    Root->SetArrayField(TEXT("executed_nodes"), Nodes);
    Root->SetNumberField(TEXT("executed_record_count"), Nodes.Num());
    Root->SetNumberField(TEXT("distinct_node_count"), DistinctNodes);
    if (!NodeName.IsEmpty())
    {
        Root->SetStringField(TEXT("node_filter"), NodeName);
    }
    return MCPPcgI_Serialize(Root);
#else
    return MCPPcgI_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// InspectPCGNodeOutputJson — InspectData(stack) for a named node; summarize the FPCGDataCollection.
// =====================================================================================================
FString UMCPReflectionLibrary::InspectPCGNodeOutputJson(const FString& ActorPath, const FString& NodeName)
{
#if WITH_EDITOR
    if (NodeName.IsEmpty())
    {
        return MCPPcgI_Err(TEXT("node_name is required for inspect_node_output"));
    }
    FString Err;
    AActor* Actor = nullptr;
    UPCGComponent* Comp = MCPPcgI_ResolveComp(ActorPath, Err, &Actor);
    if (!Comp)
    {
        return MCPPcgI_Err(Err);
    }
    FPCGGraphExecutionInspection& Insp = Comp->GetExecutionState().GetInspection();

    // Find the node + a stack it executed in.
    const UPCGNode* TargetNode = nullptr;
    FPCGStack TargetStack;
    bool bFound = false;
    TMap<TObjectKey<const UPCGNode>, TSet<FPCGGraphExecutionInspection::FNodeExecutedNotificationData>> Stacks
        = Insp.GetExecutedNodeStacks();
    for (const TPair<TObjectKey<const UPCGNode>, TSet<FPCGGraphExecutionInspection::FNodeExecutedNotificationData>>& Pair : Stacks)
    {
        const UPCGNode* Node = Pair.Key.ResolveObjectPtr();
        if (!Node || Node->GetName() != NodeName)
        {
            continue;
        }
        for (const FPCGGraphExecutionInspection::FNodeExecutedNotificationData& Notif : Pair.Value)
        {
            TargetNode = Node;
            TargetStack = Notif.Stack;
            bFound = true;
            break;
        }
        if (bFound)
        {
            break;
        }
    }
    if (!bFound)
    {
        return MCPPcgI_Err(FString::Printf(TEXT("node '%s' not found among executed nodes (generate with inspection enabled first)"), *NodeName));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor"), Actor ? Actor->GetPathName() : ActorPath);
    Root->SetStringField(TEXT("node_name"), NodeName);
    Root->SetStringField(TEXT("node_type"), MCPPcgI_NodeType(TargetNode));
    FString StackStr;
    TargetStack.CreateStackFramePath(StackStr, TargetNode);
    Root->SetStringField(TEXT("stack"), StackStr);

    // The inspection cache is keyed on (execStack + Node + Pin), one entry PER OUTPUT PIN — NOT the bare
    // execution stack that GetExecutedNodeStacks() hands back (see FPCGGraphExecutionInspection::StoreInspectionData:
    // it pushes InNode then each output pin onto a copy of the exec stack). So reconstruct that key per output pin.
    TArray<TSharedPtr<FJsonValue>> Items;
    int32 Count = 0;
    int32 PinsInspected = 0;
    const TArray<TObjectPtr<UPCGPin>>& OutPins = TargetNode->GetOutputPins();
    for (const UPCGPin* Pin : OutPins)
    {
        if (!Pin)
        {
            continue;
        }
        FPCGStack Key = TargetStack;
        Key.PushFrame(TargetNode);   // push node frame (matches StorePinInspectionData)
        Key.PushFrame(Pin);          // push pin frame  (matches StorePinInspectionDataFromNode)
        const FString PinLabel = Pin->Properties.Label.ToString();
        const bool bPinInspected = Insp.InspectData(Key, [&Items, &Count, &PinLabel](const FPCGDataCollection& DC)
        {
            const TArray<FPCGTaggedData>& TD = DC.GetAllInputs();
            for (const FPCGTaggedData& T : TD)
            {
                ++Count;
                TSharedRef<FJsonObject> Item = MakeShared<FJsonObject>();
                const UPCGData* D = T.Data.Get();
                Item->SetStringField(TEXT("data_class"), (D && D->GetClass()) ? D->GetClass()->GetName() : TEXT("null"));
                Item->SetStringField(TEXT("pin"), PinLabel);
                TArray<TSharedPtr<FJsonValue>> Tags;
                for (const FString& Tag : T.Tags)
                {
                    Tags.Add(MakeShared<FJsonValueString>(Tag));
                }
                Item->SetArrayField(TEXT("tags"), Tags);
                if (const UPCGBasePointData* PD = Cast<UPCGBasePointData>(D))
                {
                    Item->SetNumberField(TEXT("num_points"), PD->GetNumPoints());
                }
                Items.Add(MakeShared<FJsonValueObject>(Item));
            }
        });
        if (bPinInspected)
        {
            ++PinsInspected;
        }
    }

    Root->SetBoolField(TEXT("inspected"), PinsInspected > 0);
    Root->SetNumberField(TEXT("pins_inspected"), PinsInspected);
    Root->SetNumberField(TEXT("output_pin_count"), OutPins.Num());
    Root->SetArrayField(TEXT("tagged_data"), Items);
    Root->SetNumberField(TEXT("tagged_data_count"), Count);
    return MCPPcgI_Serialize(Root);
#else
    return MCPPcgI_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// ClearPCGInspectionJson — clear cached inspection data (+ per-node execution data). Idempotent.
// =====================================================================================================
FString UMCPReflectionLibrary::ClearPCGInspectionJson(const FString& ActorPath)
{
#if WITH_EDITOR
    FString Err;
    AActor* Actor = nullptr;
    UPCGComponent* Comp = MCPPcgI_ResolveComp(ActorPath, Err, &Actor);
    if (!Comp)
    {
        return MCPPcgI_Err(Err);
    }
    FPCGGraphExecutionInspection& Insp = Comp->GetExecutionState().GetInspection();
    Insp.ClearInspectionData(true);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor"), Actor ? Actor->GetPathName() : ActorPath);
    Root->SetBoolField(TEXT("cleared"), true);
    return MCPPcgI_Serialize(Root);
#else
    return MCPPcgI_Err(TEXT("editor-only"));
#endif
}
