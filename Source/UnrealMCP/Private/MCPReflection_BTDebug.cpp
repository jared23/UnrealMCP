// UnrealMCP — BEHAVIOR-TREE BREAKPOINT subsystem (debug category, Wave 4; C++ DRAFT 2026-08-19).
//
// Three editor-graph handlers that place / clear / list BehaviorTree breakpoints. Member DEFINITIONS
// for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to MCPReflectionLibrary.h by
// the coordinator (do NOT edit the .h here). Handlers:
//
//   1) SetBTBreakpointJson    — resolve NodeId -> UBehaviorTreeGraphNode on the asset's editor BTGraph,
//                                set the transient debugger bitfields bHasBreakpoint=1 / bIsBreakpointEnabled.
//                                Only nodes where CanPlaceBreakpoints() is true (Task/Composite) qualify.
//                                Captures prior {present, enabled} for the reversible ledger.
//   2) RemoveBTBreakpointJson  — clear bHasBreakpoint on one node (by NodeId) OR on every graph node (bAll).
//                                Captures prior state of every cleared node for the inverse.
//   3) ListBTBreakpointsJson    — walk BTGraph->Nodes emitting those with bHasBreakpoint:
//                                {node_id, node_guid, node_title, node_class, enabled}.
//
// TRANSIENT-STATE CONTRACT (important): BT breakpoints are stored in PUBLIC, NON-UPROPERTY, transient
// uint32:1 bitfields on UBehaviorTreeGraphNode (bHasBreakpoint / bIsBreakpointEnabled, engine header lines
// 107/110). They are NEVER serialized with the asset — so a ListBTBreakpointsJson right after an editor
// restart is EMPTY BY DESIGN, and these writes do NOT dirty the package (there is nothing on disk to save).
// "Undo" of a breakpoint is cosmetic; the ledger is recorded only for cross-tool consistency.
//
// NODE IDENTITY (node_id) — matched to get_behavior_tree_info (MCP/UserTools/ai_read.py):
//   That reader walks the RUNTIME node tree and emits per-node {class, name}, where `name` =
//   n.get_editor_property("node_name") (ai_read.py:319/322/325) — i.e. the runtime UBTNode's NodeName
//   FString. It emits NO graph-node GUID and NO runtime ExecutionIndex. So the PRIMARY node_id here is the
//   NodeName of the graph node's NodeInstance (the SAME runtime UBTNode object, wired by C++ #11's
//   SyncBehaviorTreeEditorGraph), read via pure reflection (FindPropertyByName "NodeName") for an EXACT
//   string match to the reader. As a stable fallback (NodeName may be empty/duplicated) the resolver ALSO
//   accepts the UEdGraphNode::NodeGuid string, and ListBTBreakpointsJson emits BOTH node_id (NodeName) and
//   node_guid so a list->remove round-trip is unambiguous.
//
// LINK-SAFETY (no export patch): UBehaviorTreeGraphNode is UCLASS(MinimalAPI). This file touches ONLY
//   StaticClass()+Cast (exported for MinimalAPI), the public transient bitfields (direct member writes — no
//   symbol), the public UPROPERTY members NodeInstance / NodeGuid, and CanPlaceBreakpoints() which is an
//   INLINE VIRTUAL (resolved through the object's vtable — no external method symbol). NO non-exported
//   *_API method is called. C++ #11 (SyncBehaviorTreeEditorGraph in MCPReflectionLibrary.cpp) already
//   Casts UBehaviorTreeGraph / constructs UBehaviorTreeGraphNode_* in THIS plugin and builds, proving these
//   editor classes link here -> NO Build.cs change (AIModule/AIGraph/BehaviorTreeEditor already at
//   UnrealMCP.Build.cs:47), NO engine export patch.
//
// Anonymous-namespace helpers are prefixed `MCPBtDbg_` so they stay unique in the module's unity build.
// All handlers are #if WITH_EDITOR guarded (breakpoints are editor-graph state) with a cooked-build stub.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"
#include "UObject/UnrealType.h"        // FStrProperty::GetPropertyValue_InContainer
#include "UObject/UObjectGlobals.h"    // LoadObject
#include "Misc/PackageName.h"          // FPackageName::GetShortName (bare-path load fallback)

#include "BehaviorTree/BehaviorTree.h" // UBehaviorTree (BTGraph is WITH_EDITORONLY_DATA UEdGraph*)

#if WITH_EDITOR
#include "EdGraph/EdGraph.h"           // UEdGraph::Nodes
#include "EdGraph/EdGraphNode.h"       // UEdGraphNode::NodeGuid
#include "BehaviorTreeGraph.h"         // UBehaviorTreeGraph (Cast target for BTGraph)
#include "BehaviorTreeGraphNode.h"     // UBehaviorTreeGraphNode (bHasBreakpoint/bIsBreakpointEnabled/CanPlaceBreakpoints/NodeInstance)
#endif // WITH_EDITOR

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPBtDbg_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python wiring keys off res.get("error"). Success objects never carry "error".
    FString MCPBtDbg_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPBtDbg_Serialize(Root);
    }

#if WITH_EDITOR
    // Load a UBehaviorTree from an asset path ("/Game/AI/BT_Foo.BT_Foo" or bare "/Game/AI/BT_Foo").
    UBehaviorTree* MCPBtDbg_LoadBT(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UBehaviorTree* BT = LoadObject<UBehaviorTree>(nullptr, *Path);
        if (!BT)
        {
            // Tolerate a bare package path lacking the .Object suffix.
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            BT = LoadObject<UBehaviorTree>(nullptr, *Full);
        }
        return BT;
    }

    // Resolve the editor graph (UBehaviorTreeGraph) on the asset. Returns nullptr if the graph was never
    // built (an MCP-authored BT that hasn't had sync_bt_editor_graph run yet).
    UBehaviorTreeGraph* MCPBtDbg_GetGraph(UBehaviorTree* BT)
    {
        if (!BT || !BT->BTGraph)  // VERIFY vs engine source: UBehaviorTree::BTGraph (WITH_EDITORONLY_DATA UEdGraph*)
        {
            return nullptr;
        }
        return Cast<UBehaviorTreeGraph>(BT->BTGraph);
    }

    // The runtime NodeName of a graph node's NodeInstance, read via pure reflection so the string matches
    // get_behavior_tree_info's `name` EXACTLY (which uses get_editor_property("node_name")). May be empty.
    FString MCPBtDbg_NodeName(UBehaviorTreeGraphNode* GNode)
    {
        if (!GNode || !GNode->NodeInstance)  // NodeInstance: public UPROPERTY on UAIGraphNode
        {
            return FString();
        }
        UObject* Inst = GNode->NodeInstance;
        if (FProperty* Prop = Inst->GetClass()->FindPropertyByName(TEXT("NodeName")))
        {
            if (FStrProperty* Str = CastField<FStrProperty>(Prop))
            {
                return Str->GetPropertyValue_InContainer(Inst);
            }
        }
        return FString();
    }

    // A human title for list output — the concrete runtime class of the node (matches the reader's `class`,
    // e.g. "BTComposite_Selector" / "BTTask_MoveTo"). Falls back to the graph-node class if NodeInstance null.
    FString MCPBtDbg_NodeClass(UBehaviorTreeGraphNode* GNode)
    {
        if (GNode && GNode->NodeInstance)
        {
            return GNode->NodeInstance->GetClass()->GetName();
        }
        return GNode ? GNode->GetClass()->GetName() : FString();
    }

    // Resolve a NodeId to a breakpoint-capable graph node. Matches, in order: (1) NodeInstance NodeName
    // (== get_behavior_tree_info `name`); (2) UEdGraphNode::NodeGuid string (stable fallback / list output).
    // Only nodes where CanPlaceBreakpoints() is true (Task/Composite) are considered.
    UBehaviorTreeGraphNode* MCPBtDbg_ResolveNode(UBehaviorTreeGraph* Graph, const FString& NodeId)
    {
        if (!Graph || NodeId.IsEmpty())
        {
            return nullptr;
        }
        // Pass 1: exact NodeName match.
        for (UEdGraphNode* N : Graph->Nodes)
        {
            UBehaviorTreeGraphNode* G = Cast<UBehaviorTreeGraphNode>(N);
            if (!G || !G->CanPlaceBreakpoints())  // VERIFY vs engine source: inline virtual (returns true for Task/Composite)
            {
                continue;
            }
            if (MCPBtDbg_NodeName(G).Equals(NodeId, ESearchCase::CaseSensitive))
            {
                return G;
            }
        }
        // Pass 2: NodeGuid string fallback (tolerant of default + digits format).
        for (UEdGraphNode* N : Graph->Nodes)
        {
            UBehaviorTreeGraphNode* G = Cast<UBehaviorTreeGraphNode>(N);
            if (!G || !G->CanPlaceBreakpoints())
            {
                continue;
            }
            if (N->NodeGuid.ToString() == NodeId ||
                N->NodeGuid.ToString(EGuidFormats::Digits) == NodeId)
            {
                return G;
            }
        }
        return nullptr;
    }

    // Populate {node_id, node_guid, node_title, node_class} identity fields onto a JSON object.
    void MCPBtDbg_WriteIdentity(const TSharedRef<FJsonObject>& Obj, UBehaviorTreeGraphNode* G)
    {
        const FString Name = MCPBtDbg_NodeName(G);
        const FString Cls = MCPBtDbg_NodeClass(G);
        Obj->SetStringField(TEXT("node_id"), Name);                       // matches get_behavior_tree_info `name`
        Obj->SetStringField(TEXT("node_guid"), G->NodeGuid.ToString());   // stable disambiguator
        Obj->SetStringField(TEXT("node_title"), Cls);
        Obj->SetStringField(TEXT("node_class"), Cls);
    }
#endif // WITH_EDITOR
}

// =====================================================================================================
// 1) SetBTBreakpointJson — place (or re-enable/disable) a breakpoint on one BT node.
// =====================================================================================================
FString UMCPReflectionLibrary::SetBTBreakpointJson(const FString& BehaviorTreePath, const FString& NodeId, bool bEnabled)
{
#if WITH_EDITOR
    UBehaviorTree* BT = MCPBtDbg_LoadBT(BehaviorTreePath);
    if (!BT)
    {
        return MCPBtDbg_Err(FString::Printf(TEXT("could not load behavior tree '%s'"), *BehaviorTreePath));
    }
    UBehaviorTreeGraph* Graph = MCPBtDbg_GetGraph(BT);
    if (!Graph)
    {
        return MCPBtDbg_Err(TEXT("behavior tree has no editor graph (run sync_bt_editor_graph first)"));
    }
    UBehaviorTreeGraphNode* G = MCPBtDbg_ResolveNode(Graph, NodeId);
    if (!G)
    {
        return MCPBtDbg_Err(FString::Printf(
            TEXT("no breakpoint-capable node (Task/Composite) matches node_id '%s'"), *NodeId));
    }

    // Capture prior transient state for the reversible ledger.
    const bool bPriorPresent = (G->bHasBreakpoint != 0);
    const bool bPriorEnabled = (G->bIsBreakpointEnabled != 0);

    // Set the transient debugger bitfields directly (NOT UPROPERTY, NOT saved).
    G->bHasBreakpoint = 1;
    G->bIsBreakpointEnabled = bEnabled ? 1 : 0;

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("behavior_tree"), BT->GetName());
    Root->SetStringField(TEXT("behavior_tree_path"), BT->GetPathName());
    MCPBtDbg_WriteIdentity(Root, G);
    Root->SetBoolField(TEXT("prior_present"), bPriorPresent);
    Root->SetBoolField(TEXT("prior_enabled"), bPriorEnabled);
    Root->SetBoolField(TEXT("now_present"), true);
    Root->SetBoolField(TEXT("now_enabled"), bEnabled);
    Root->SetBoolField(TEXT("set"), true);
    Root->SetBoolField(TEXT("transient"), true);
    return MCPBtDbg_Serialize(Root);
#else
    return MCPBtDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 2) RemoveBTBreakpointJson — clear one node's breakpoint (by NodeId) or every node's (bAll).
// =====================================================================================================
FString UMCPReflectionLibrary::RemoveBTBreakpointJson(const FString& BehaviorTreePath, const FString& NodeId, bool bAll)
{
#if WITH_EDITOR
    UBehaviorTree* BT = MCPBtDbg_LoadBT(BehaviorTreePath);
    if (!BT)
    {
        return MCPBtDbg_Err(FString::Printf(TEXT("could not load behavior tree '%s'"), *BehaviorTreePath));
    }
    UBehaviorTreeGraph* Graph = MCPBtDbg_GetGraph(BT);
    if (!Graph)
    {
        return MCPBtDbg_Err(TEXT("behavior tree has no editor graph (run sync_bt_editor_graph first)"));
    }

    TArray<TSharedPtr<FJsonValue>> Cleared;

    if (bAll)
    {
        // Walk every graph node, clearing any that carry a breakpoint (capture prior for the inverse).
        for (UEdGraphNode* N : Graph->Nodes)
        {
            UBehaviorTreeGraphNode* G = Cast<UBehaviorTreeGraphNode>(N);
            if (!G || G->bHasBreakpoint == 0)
            {
                continue;
            }
            TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
            MCPBtDbg_WriteIdentity(Entry, G);
            Entry->SetBoolField(TEXT("prior_enabled"), G->bIsBreakpointEnabled != 0);
            Cleared.Add(MakeShared<FJsonValueObject>(Entry));

            G->bHasBreakpoint = 0;
            G->bIsBreakpointEnabled = 0;
        }
    }
    else
    {
        UBehaviorTreeGraphNode* G = MCPBtDbg_ResolveNode(Graph, NodeId);
        if (!G)
        {
            return MCPBtDbg_Err(FString::Printf(
                TEXT("no breakpoint-capable node (Task/Composite) matches node_id '%s'"), *NodeId));
        }
        const bool bPriorPresent = (G->bHasBreakpoint != 0);
        if (bPriorPresent)
        {
            TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
            MCPBtDbg_WriteIdentity(Entry, G);
            Entry->SetBoolField(TEXT("prior_enabled"), G->bIsBreakpointEnabled != 0);
            Cleared.Add(MakeShared<FJsonValueObject>(Entry));

            G->bHasBreakpoint = 0;
            G->bIsBreakpointEnabled = 0;
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("behavior_tree"), BT->GetName());
    Root->SetStringField(TEXT("behavior_tree_path"), BT->GetPathName());
    Root->SetBoolField(TEXT("all"), bAll);
    Root->SetBoolField(TEXT("removed"), Cleared.Num() > 0);
    Root->SetNumberField(TEXT("cleared_count"), Cleared.Num());
    Root->SetArrayField(TEXT("cleared"), Cleared);
    Root->SetBoolField(TEXT("transient"), true);
    return MCPBtDbg_Serialize(Root);
#else
    return MCPBtDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 3) ListBTBreakpointsJson — enumerate graph nodes that currently carry a breakpoint.
// =====================================================================================================
FString UMCPReflectionLibrary::ListBTBreakpointsJson(const FString& BehaviorTreePath, int32 MaxResults)
{
#if WITH_EDITOR
    UBehaviorTree* BT = MCPBtDbg_LoadBT(BehaviorTreePath);
    if (!BT)
    {
        return MCPBtDbg_Err(FString::Printf(TEXT("could not load behavior tree '%s'"), *BehaviorTreePath));
    }
    UBehaviorTreeGraph* Graph = MCPBtDbg_GetGraph(BT);
    if (!Graph)
    {
        return MCPBtDbg_Err(TEXT("behavior tree has no editor graph (run sync_bt_editor_graph first)"));
    }

    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;
    TArray<TSharedPtr<FJsonValue>> Breakpoints;
    bool bTruncated = false;

    for (UEdGraphNode* N : Graph->Nodes)
    {
        UBehaviorTreeGraphNode* G = Cast<UBehaviorTreeGraphNode>(N);
        if (!G || G->bHasBreakpoint == 0)
        {
            continue;
        }
        if (Breakpoints.Num() >= Cap)
        {
            bTruncated = true;
            break;
        }
        TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
        MCPBtDbg_WriteIdentity(Entry, G);
        Entry->SetBoolField(TEXT("enabled"), G->bIsBreakpointEnabled != 0);
        Breakpoints.Add(MakeShared<FJsonValueObject>(Entry));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("behavior_tree"), BT->GetName());
    Root->SetStringField(TEXT("behavior_tree_path"), BT->GetPathName());
    Root->SetNumberField(TEXT("breakpoint_count"), Breakpoints.Num());
    Root->SetArrayField(TEXT("breakpoints"), Breakpoints);
    Root->SetBoolField(TEXT("truncated"), bTruncated);
    Root->SetBoolField(TEXT("transient"), true);
    Root->SetStringField(TEXT("note"),
        TEXT("BT breakpoints are transient (session-only, not saved with the asset); this list is empty "
             "after an editor restart by design."));
    return MCPBtDbg_Serialize(Root);
#else
    return MCPBtDbg_Err(TEXT("editor-only"));
#endif
}
