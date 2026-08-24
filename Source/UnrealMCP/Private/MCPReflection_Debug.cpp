// UnrealMCP — BLUEPRINT DEBUGGER integration (C++ DRAFT 2026-08-19, block "C++ #44").
//
// Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator (do NOT edit the .h here). ELEVEN handlers exposing the
// editor's Blueprint debugger (breakpoints / pin-watches / debug-object / runtime state) that the stock
// Python API cannot reach — every FKismetDebugUtilities method is UNREALED_API and UnrealEd is already
// linked, so NO Build.cs change and NO engine export patch is required.
//
//   WAVE 1 (editor-only; safe with or without PIE):
//     1) SetBlueprintBreakpointJson    — create/enable a breakpoint on a node (by NodeGuid). Dup-safe.
//     2) RemoveBlueprintBreakpointJson — remove one node's breakpoint, or ALL (bAll). Captures prior.
//     3) ListBlueprintBreakpointsJson  — enumerate a BP's breakpoints.
//     4) SetBlueprintPinWatchJson      — add/remove a pin watch (CanWatchPin-guarded).
//     5) ListBlueprintPinWatchesJson   — enumerate watched pins (+values when a debug object is set).
//     6) SetBlueprintDebugObjectJson   — set/clear the object being debugged. Captures prior.
//     7) ListBlueprintDebugObjectsJson — enumerate live instances of the BP's generated class.
//
//   WAVE 2 (PIE readers; degrade cleanly to {"debugging":false}/nulls outside PIE — NEVER error):
//     8) GetBlueprintDebugStateJson       — current instruction / most-recent breakpoint / stepping / world.
//     9) GetBlueprintExecutionTraceJson   — the FKismetTraceSample ring buffer -> source nodes (most usable).
//    10) GetBlueprintCallStackJson        — FFrame::GetScriptCallstack, split into frames.
//    11) InspectBlueprintDebugValueJson   — property tree of the set debug object (GetDebugInfoInternal).
//
// node_id (BP debugger side) == the node's NodeGuid string — the SAME identity the graph tools emit.
//
// CRASH-SAFETY: every load/resolve/pointer is guarded (LoadObject + null-check, guarded FGuid::Parse +
// linear scan, FindPin + null-check — never *Checked on user input). CreateBreakpoint contains a
// checkSlow that asserts the node has no existing breakpoint, so handler 1 ONLY calls CreateBreakpoint
// when FindBreakpointForNode returns null (otherwise it just re-enables). All handlers are #if WITH_EDITOR
// with a non-editor stub returning {"error":"editor-only"}. Any miss returns {"error":...}; nothing crashes.
//
// Anonymous-namespace helpers are prefixed `MCPDbg_` so they stay unique in the module's unity build
// (own prefixed copies of the BP resolvers to avoid ODR/link coupling with the sibling .cpp).
// Every engine-API touch point is tagged "VERIFY vs engine source" with the header:line it was checked at.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"
#include "UObject/UnrealType.h"        // FProperty / TFieldIterator / ContainerPtrToValuePtr
#include "UObject/UObjectGlobals.h"    // LoadObject / FindObject
#include "UObject/UObjectHash.h"       // GetObjectsOfClass
#include "UObject/Stack.h"             // FFrame::GetScriptCallstack (COREUOBJECT_API, Stack.h:270)
#include "Misc/PackageName.h"          // FPackageName::GetShortName (bare-path load fallback)

#include "Engine/Blueprint.h"                 // UBlueprint (Set/GetObjectBeingDebugged — ENGINE_API, Blueprint.h:852/883)
#include "Engine/BlueprintGeneratedClass.h"   // UBlueprintGeneratedClass + TSimpleRingBuffer (BlueprintGeneratedClass.h:124)
#include "Engine/World.h"                     // UWorld::WorldType / EWorldType
#include "EdGraph/EdGraph.h"                  // UEdGraph::Nodes
#include "EdGraph/EdGraphNode.h"              // UEdGraphNode (NodeGuid / GetNodeTitle / GetGraph)
#include "EdGraph/EdGraphPin.h"               // UEdGraphPin (PinName / GetOwningNode)

#if WITH_EDITOR
#include "Kismet2/KismetDebugUtilities.h"     // FKismetDebugUtilities + FKismetTraceSample + FPropertyInstanceInfo
#include "Kismet2/Breakpoint.h"               // FBlueprintBreakpoint (GetLocation / IsEnabled / GetLocationDescription)
#include "Kismet2/WatchedPin.h"               // FBlueprintWatchedPin (ctor(const UEdGraphPin*) / Get / GetPathToProperty)
#include "Kismet2/BlueprintEditorUtils.h"     // FBlueprintEditorUtils::FindBlueprintForNode (UNREALED_API, BlueprintEditorUtils.h:241)
#endif // WITH_EDITOR

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build; usable from the non-editor stubs) ----
    FString MCPDbg_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error").
    FString MCPDbg_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPDbg_Serialize(Root);
    }

#if WITH_EDITOR
    // Load a UBlueprint from an asset path ("/Game/Path/BP_Foo.BP_Foo" or "/Game/Path/BP_Foo").
    UBlueprint* MCPDbg_LoadBlueprint(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UBlueprint* BP = LoadObject<UBlueprint>(nullptr, *Path);
        if (!BP)
        {
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            BP = LoadObject<UBlueprint>(nullptr, *Full);
        }
        return BP;
    }

    // Resolve the target UEdGraph. GraphName ""/"EventGraph" -> the ubergraph (event graph); otherwise a
    // named function graph (then a named ubergraph page as a fallback). Mirrors MCPBpG_ResolveGraph.
    UEdGraph* MCPDbg_ResolveGraph(UBlueprint* BP, const FString& GraphName, FString& OutErr)
    {
        if (!BP)
        {
            OutErr = TEXT("null blueprint");
            return nullptr;
        }
        const bool bWantUber = GraphName.IsEmpty() || GraphName.Equals(TEXT("EventGraph"), ESearchCase::IgnoreCase);
        if (bWantUber)
        {
            for (UEdGraph* G : BP->UbergraphPages)                          // VERIFY vs engine source (UBlueprint::UbergraphPages)
            {
                if (!G) { continue; }
                if (GraphName.IsEmpty() || G->GetName().Equals(GraphName, ESearchCase::IgnoreCase))
                {
                    return G;
                }
            }
            if (BP->UbergraphPages.Num() > 0 && BP->UbergraphPages[0])
            {
                return BP->UbergraphPages[0];
            }
            OutErr = TEXT("blueprint has no ubergraph (event graph)");
            return nullptr;
        }
        for (UEdGraph* G : BP->FunctionGraphs)                              // VERIFY vs engine source (UBlueprint::FunctionGraphs)
        {
            if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase))
            {
                return G;
            }
        }
        for (UEdGraph* G : BP->UbergraphPages)
        {
            if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase))
            {
                return G;
            }
        }
        OutErr = FString::Printf(TEXT("no graph named '%s' in blueprint '%s'"), *GraphName, *BP->GetName());
        return nullptr;
    }

    // Find a node by GUID within one graph (guarded FGuid::Parse; never crashes on bad input).
    UEdGraphNode* MCPDbg_FindNode(UEdGraph* Graph, const FString& GuidStr, FString& OutErr)
    {
        if (!Graph)
        {
            OutErr = TEXT("null graph");
            return nullptr;
        }
        FGuid Target;
        if (!FGuid::Parse(GuidStr, Target))
        {
            OutErr = FString::Printf(TEXT("invalid node guid '%s'"), *GuidStr);
            return nullptr;
        }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (N && N->NodeGuid == Target)                                 // VERIFY vs engine source (UEdGraphNode::NodeGuid)
            {
                return N;
            }
        }
        OutErr = FString::Printf(TEXT("no node with guid '%s' in graph '%s'"), *GuidStr, *Graph->GetName());
        return nullptr;
    }

    // Find a pin by name (exact FName first, case-insensitive fallback). NEVER FindPinChecked.
    UEdGraphPin* MCPDbg_FindPin(UEdGraphNode* Node, const FString& PinName)
    {
        if (!Node)
        {
            return nullptr;
        }
        if (UEdGraphPin* P = Node->FindPin(FName(*PinName)))               // VERIFY vs engine source (UEdGraphNode::FindPin(FName))
        {
            return P;
        }
        for (UEdGraphPin* Pin : Node->Pins)
        {
            if (Pin && Pin->PinName.ToString().Equals(PinName, ESearchCase::IgnoreCase))
            {
                return Pin;
            }
        }
        return nullptr;
    }

    // A compact node identity {node_guid, node_title, graph, blueprint} — resilient to nulls.
    TSharedRef<FJsonObject> MCPDbg_NodeBrief(UEdGraphNode* Node)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Node)
        {
            return J;
        }
        J->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
        // ListView title is compact + single-line.
        J->SetStringField(TEXT("node_title"), Node->GetNodeTitle(ENodeTitleType::ListView).ToString());  // VERIFY vs engine source (UEdGraphNode::GetNodeTitle)
        if (UEdGraph* G = Node->GetGraph())                                // VERIFY vs engine source (UEdGraphNode::GetGraph)
        {
            J->SetStringField(TEXT("graph"), G->GetName());
        }
        if (UBlueprint* OwnerBP = FBlueprintEditorUtils::FindBlueprintForNode(Node))  // VERIFY vs engine source (BlueprintEditorUtils.h:241)
        {
            J->SetStringField(TEXT("blueprint"), OwnerBP->GetName());
        }
        return J;
    }

    // A short human string for the world an object lives in (for the debug-object list).
    const TCHAR* MCPDbg_WorldTypeStr(UWorld* W)
    {
        if (!W) { return TEXT("none"); }
        switch (W->WorldType)                                              // VERIFY vs engine source (UWorld::WorldType / EWorldType)
        {
            case EWorldType::Editor:        return TEXT("editor");
            case EWorldType::PIE:           return TEXT("pie");
            case EWorldType::Game:          return TEXT("game");
            case EWorldType::EditorPreview: return TEXT("editor_preview");
            case EWorldType::GamePreview:   return TEXT("game_preview");
            case EWorldType::Inactive:      return TEXT("inactive");
            default:                        return TEXT("other");
        }
    }
#endif // WITH_EDITOR
} // namespace

// =====================================================================================================
// WAVE 1 · 1 — SetBlueprintBreakpointJson. Dup-safe: CreateBreakpoint has a checkSlow that asserts the
// node has no breakpoint yet (KismetDebugUtilities.cpp:1180), so we CreateBreakpoint only when
// FindBreakpointForNode is null; otherwise we just SetBreakpointEnabled. Captures prior enabled state.
// Ledger op 'set_breakpoint' (inverse: prior_exists ? restore prior_enabled : remove).
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintBreakpointJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, bool bEnabled)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }
    FString Err;
    UEdGraph* Graph = MCPDbg_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPDbg_Err(Err); }
    UEdGraphNode* Node = MCPDbg_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPDbg_Err(Err); }

    // Capture prior BEFORE mutating (pointer may be invalidated by CreateBreakpoint's Emplace).
    bool bPriorExists = false, bPriorEnabled = false;
    if (FBlueprintBreakpoint* Existing = FKismetDebugUtilities::FindBreakpointForNode(Node, BP))  // VERIFY vs engine source (:332)
    {
        bPriorExists = true;
        bPriorEnabled = Existing->IsEnabled();                             // VERIFY vs engine source (Breakpoint.h:49)
    }

    if (!bPriorExists)
    {
        FKismetDebugUtilities::CreateBreakpoint(BP, Node, bEnabled);       // VERIFY vs engine source (:285) — dup-guarded above
    }
    else
    {
        FKismetDebugUtilities::SetBreakpointEnabled(Node, BP, bEnabled);   // VERIFY vs engine source (:261 node overload)
    }

    TSharedRef<FJsonObject> Root = MCPDbg_NodeBrief(Node);
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetBoolField(TEXT("enabled"), bEnabled);
    Root->SetBoolField(TEXT("prior_exists"), bPriorExists);
    Root->SetBoolField(TEXT("prior_enabled"), bPriorEnabled);
    Root->SetBoolField(TEXT("created"), !bPriorExists);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 2 — RemoveBlueprintBreakpointJson. bAll -> ClearBreakpoints(BP); else RemoveBreakpointFromNode.
// Captures prior enabled (FindBreakpointForNode) before removing so the inverse can re-create faithfully.
// Ledger op 'remove_breakpoint' (inverse: re-create at node_guid with prior_enabled).
// =====================================================================================================
FString UMCPReflectionLibrary::RemoveBlueprintBreakpointJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, bool bAll)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());

    if (bAll)
    {
        // Count first (so we can report), then clear.
        int32 Count = 0;
        FKismetDebugUtilities::ForeachBreakpoint(BP, [&Count](FBlueprintBreakpoint&) { ++Count; });  // VERIFY vs engine source (:292)
        FKismetDebugUtilities::ClearBreakpoints(BP);                       // VERIFY vs engine source (:335)
        Root->SetBoolField(TEXT("cleared_all"), true);
        Root->SetNumberField(TEXT("removed_count"), Count);
        Root->SetBoolField(TEXT("removed"), Count > 0);
        return MCPDbg_Serialize(Root);
    }

    FString Err;
    UEdGraph* Graph = MCPDbg_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPDbg_Err(Err); }
    UEdGraphNode* Node = MCPDbg_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPDbg_Err(Err); }

    // ENGINE-QUIRK-SAFE single-node removal. FKismetDebugUtilities::RemoveBreakpointFromNode is UNRELIABLE:
    // RemoveBreakpointsByPredicate (KismetDebugUtilities.cpp:1205-1220) FIRST nulls the matching breakpoint's
    // location (SetBreakpointLocation(.., nullptr)) and THEN RemoveAllSwap re-evaluates the SAME predicate
    // (GetLocation() == OwnerNode) — which now sees nullptr and no longer matches, leaving a DANGLING
    // null-location breakpoint. ClearBreakpoints, by contrast, empties cleanly. So we rebuild: capture every
    // OTHER breakpoint (by resolved node + enabled) — dropping any pre-existing dangling nulls — clear all,
    // then re-create the keepers. Uses only the proven-clean ClearBreakpoints + CreateBreakpoint.
    bool bPriorExists = false, bPriorEnabled = false;
    struct FMCPDbgKeep { UEdGraphNode* KNode; bool bKEnabled; };
    TArray<FMCPDbgKeep> Keepers;
    FKismetDebugUtilities::ForeachBreakpoint(BP, [&](FBlueprintBreakpoint& Bp)     // VERIFY vs engine source (:292)
    {
        UEdGraphNode* Loc = Bp.GetLocation();                                      // VERIFY vs engine source (Breakpoint.h:43)
        if (Loc == Node)
        {
            bPriorExists = true;
            bPriorEnabled = Bp.IsEnabled();                                        // VERIFY vs engine source (Breakpoint.h:49)
        }
        else if (Loc != nullptr)                                                   // keep valid others; drop dangling nulls
        {
            Keepers.Add({ Loc, Bp.IsEnabled() });
        }
    });
    FKismetDebugUtilities::ClearBreakpoints(BP);                                    // VERIFY vs engine source (:335) — clean empty
    for (const FMCPDbgKeep& K : Keepers)
    {
        if (K.KNode) { FKismetDebugUtilities::CreateBreakpoint(BP, K.KNode, K.bKEnabled); }  // VERIFY vs engine source (:285)
    }

    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetBoolField(TEXT("cleared_all"), false);
    Root->SetBoolField(TEXT("prior_exists"), bPriorExists);
    Root->SetBoolField(TEXT("prior_enabled"), bPriorEnabled);
    Root->SetBoolField(TEXT("removed"), bPriorExists);
    Root->SetNumberField(TEXT("removed_count"), bPriorExists ? 1 : 0);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 3 — ListBlueprintBreakpointsJson. ForeachBreakpoint -> per node {node_guid, node_title,
// enabled, location_description}. Filter is a case-insensitive substring over node_title.
// =====================================================================================================
FString UMCPReflectionLibrary::ListBlueprintBreakpointsJson(const FString& BlueprintPath, const FString& Filter, int32 MaxResults)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    const bool bHasFilter = !Filter.IsEmpty();
    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;

    TArray<TSharedPtr<FJsonValue>> Items;
    int32 Total = 0;
    FKismetDebugUtilities::ForeachBreakpoint(BP, [&](FBlueprintBreakpoint& Bp)   // VERIFY vs engine source (:292)
    {
        ++Total;
        if (Items.Num() >= Cap) { return; }
        UEdGraphNode* Node = Bp.GetLocation();                            // VERIFY vs engine source (Breakpoint.h:43)
        const FString Title = Node ? Node->GetNodeTitle(ENodeTitleType::ListView).ToString() : FString();
        if (bHasFilter && !Title.Contains(Filter, ESearchCase::IgnoreCase)) { return; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("node_guid"), Node ? Node->NodeGuid.ToString() : FString());
        J->SetStringField(TEXT("node_title"), Title);
        J->SetBoolField(TEXT("enabled"), Bp.IsEnabled());                 // VERIFY vs engine source (Breakpoint.h:49)
        J->SetStringField(TEXT("location_description"), Bp.GetLocationDescription().ToString());  // VERIFY vs engine source (Breakpoint.h:66)
        if (Node)
        {
            if (UEdGraph* G = Node->GetGraph()) { J->SetStringField(TEXT("graph"), G->GetName()); }
        }
        Items.Add(MakeShared<FJsonValueObject>(J));
    });

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetNumberField(TEXT("breakpoint_count"), Total);
    Root->SetNumberField(TEXT("returned"), Items.Num());
    Root->SetArrayField(TEXT("breakpoints"), Items);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 4 — SetBlueprintPinWatchJson. bRemove -> RemovePinWatch; else CanWatchPin guard + AddPinWatch.
// Captures prior watched state (IsPinBeingWatched). Ledger op 'set_pin_watch'
// (inverse: re-issue with bRemove flipped).
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintPinWatchJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, const FString& PinName, bool bRemove)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }
    FString Err;
    UEdGraph* Graph = MCPDbg_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPDbg_Err(Err); }
    UEdGraphNode* Node = MCPDbg_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPDbg_Err(Err); }
    UEdGraphPin* Pin = MCPDbg_FindPin(Node, PinName);
    if (!Pin) { return MCPDbg_Err(FString::Printf(TEXT("node has no pin '%s'"), *PinName)); }

    const bool bPriorWatched = FKismetDebugUtilities::IsPinBeingWatched(BP, Pin);  // VERIFY vs engine source (:355)

    bool bNowWatched = bPriorWatched;
    bool bDidRemove = false;
    if (bRemove)
    {
        bDidRemove = FKismetDebugUtilities::RemovePinWatch(BP, Pin);      // VERIFY vs engine source (:378) — returns bool
        bNowWatched = false;
    }
    else
    {
        if (!FKismetDebugUtilities::CanWatchPin(BP, Pin))                 // VERIFY vs engine source (:347)
        {
            return MCPDbg_Err(FString::Printf(TEXT("pin '%s' cannot be watched"), *Pin->PinName.ToString()));
        }
        if (!bPriorWatched)
        {
            FKismetDebugUtilities::AddPinWatch(BP, FBlueprintWatchedPin(Pin));  // VERIFY vs engine source (:385, ctor WatchedPin.h:27)
        }
        bNowWatched = true;
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("pin_name"), Pin->PinName.ToString());
    Root->SetBoolField(TEXT("removed"), bRemove);
    Root->SetBoolField(TEXT("did_remove"), bDidRemove);
    Root->SetBoolField(TEXT("prior_watched"), bPriorWatched);
    Root->SetBoolField(TEXT("watched"), bNowWatched);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 5 — ListBlueprintPinWatchesJson. ForeachPinPropertyWatch -> {node_guid, pin_name, path}.
// If bValues, also GetWatchText against the set debug object; EWTR_NoDebugObject/etc. outside PIE emits
// value:null + a note (NEVER an error).
// =====================================================================================================
FString UMCPReflectionLibrary::ListBlueprintPinWatchesJson(const FString& BlueprintPath, bool bValues, int32 MaxResults)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    UObject* Active = bValues ? BP->GetObjectBeingDebugged() : nullptr;   // VERIFY vs engine source (Blueprint.h:883)
    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;

    TArray<TSharedPtr<FJsonValue>> Items;
    int32 Total = 0;
    FKismetDebugUtilities::ForeachPinPropertyWatch(BP, [&](FBlueprintWatchedPin& Wp)  // VERIFY vs engine source (:405)
    {
        ++Total;
        if (Items.Num() >= Cap) { return; }
        UEdGraphPin* Pin = Wp.Get();                                      // VERIFY vs engine source (WatchedPin.h:31)
        if (!Pin) { return; }
        UEdGraphNode* Owner = Pin->GetOwningNodeUnchecked();
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("node_guid"), Owner ? Owner->NodeGuid.ToString() : FString());
        J->SetStringField(TEXT("pin_name"), Pin->PinName.ToString());
        // Optional nested-property path (segments are authored property names).
        const TArray<FName>& Path = Wp.GetPathToProperty();               // VERIFY vs engine source (WatchedPin.h:34)
        if (Path.Num() > 0)
        {
            TArray<TSharedPtr<FJsonValue>> PathArr;
            for (const FName& Seg : Path) { PathArr.Add(MakeShared<FJsonValueString>(Seg.ToString())); }
            J->SetArrayField(TEXT("path"), PathArr);
        }
        if (bValues)
        {
            FString WatchText;
            const FKismetDebugUtilities::EWatchTextResult R =
                FKismetDebugUtilities::GetWatchText(WatchText, BP, Active, Pin);  // VERIFY vs engine source (:444)
            if (R == FKismetDebugUtilities::EWTR_Valid)
            {
                J->SetStringField(TEXT("value"), WatchText);
            }
            else
            {
                J->SetField(TEXT("value"), MakeShared<FJsonValueNull>());
                const TCHAR* Note =
                    (R == FKismetDebugUtilities::EWTR_NoDebugObject) ? TEXT("no debug object (set one; needs a live PIE instance)") :
                    (R == FKismetDebugUtilities::EWTR_NotInScope)    ? TEXT("local not on the current stack") :
                    (R == FKismetDebugUtilities::EWTR_NoProperty)    ? TEXT("no property associated with this pin") :
                                                                       TEXT("value unavailable");
                J->SetStringField(TEXT("value_note"), Note);
            }
        }
        Items.Add(MakeShared<FJsonValueObject>(J));
    });

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetBoolField(TEXT("has_debug_object"), Active != nullptr);
    if (Active) { Root->SetStringField(TEXT("debug_object"), Active->GetPathName()); }
    Root->SetNumberField(TEXT("watch_count"), Total);
    Root->SetNumberField(TEXT("returned"), Items.Num());
    Root->SetArrayField(TEXT("watches"), Items);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 6 — SetBlueprintDebugObjectJson. bClear -> SetObjectBeingDebugged(nullptr). Else resolve
// Instance by object path (FindObject) or by name/path among live instances of the generated class.
// Captures prior debug-object path. Ledger op 'set_debug_object' (inverse: restore prior / clear).
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintDebugObjectJson(const FString& BlueprintPath, const FString& Instance, bool bClear)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    UObject* Prior = BP->GetObjectBeingDebugged();                        // VERIFY vs engine source (Blueprint.h:883)
    const FString PriorPath = Prior ? Prior->GetPathName() : FString();

    UObject* NewObj = nullptr;
    if (!bClear && !Instance.IsEmpty())
    {
        NewObj = FindObject<UObject>(nullptr, *Instance);                 // full path (runtime instances aren't LoadObject-able)
        if (!NewObj)
        {
            if (UClass* GenClass = BP->GeneratedClass)                    // VERIFY vs engine source (UBlueprint::GeneratedClass)
            {
                TArray<UObject*> Objects;
                GetObjectsOfClass(GenClass, Objects, /*bIncludeDerived*/true, RF_ClassDefaultObject);  // VERIFY vs engine source (UObjectHash.h:228)
                for (UObject* O : Objects)
                {
                    if (O && (O->GetName() == Instance || O->GetPathName() == Instance))
                    {
                        NewObj = O;
                        break;
                    }
                }
            }
        }
        if (!NewObj)
        {
            return MCPDbg_Err(FString::Printf(TEXT("could not resolve debug instance '%s'"), *Instance));
        }
    }

    BP->SetObjectBeingDebugged(NewObj);                                   // VERIFY vs engine source (Blueprint.h:852, ENGINE_API)

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetBoolField(TEXT("cleared"), bClear || NewObj == nullptr);
    if (PriorPath.IsEmpty()) { Root->SetField(TEXT("prior_instance_path"), MakeShared<FJsonValueNull>()); }
    else                     { Root->SetStringField(TEXT("prior_instance_path"), PriorPath); }
    if (NewObj) { Root->SetStringField(TEXT("instance_path"), NewObj->GetPathName()); }
    else        { Root->SetField(TEXT("instance_path"), MakeShared<FJsonValueNull>()); }
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 1 · 7 — ListBlueprintDebugObjectsJson. Enumerate live instances of BP->GeneratedClass via
// GetObjectsOfClass (CDO excluded). Outside PIE the only instances are editor-world placed actors; when
// none exist the CDO is emitted (flagged is_cdo). Marks the current debug object (is_current).
// =====================================================================================================
FString UMCPReflectionLibrary::ListBlueprintDebugObjectsJson(const FString& BlueprintPath, const FString& Filter, int32 MaxResults)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }
    UClass* GenClass = BP->GeneratedClass;
    if (!GenClass) { return MCPDbg_Err(TEXT("blueprint has no generated class (not compiled?)")); }

    UObject* Current = BP->GetObjectBeingDebugged();
    UWorld* DbgWorld = FKismetDebugUtilities::GetCurrentDebuggingWorld(); // VERIFY vs engine source (:213) — non-null only in PIE/SIE
    const bool bHasFilter = !Filter.IsEmpty();
    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;

    TArray<UObject*> Objects;
    GetObjectsOfClass(GenClass, Objects, /*bIncludeDerived*/true, RF_ClassDefaultObject);  // VERIFY vs engine source (UObjectHash.h:228)

    TArray<TSharedPtr<FJsonValue>> Items;
    int32 Total = 0;
    for (UObject* O : Objects)
    {
        if (!O) { continue; }
        const FString Name = O->GetName();
        const FString PathName = O->GetPathName();
        if (bHasFilter && !Name.Contains(Filter, ESearchCase::IgnoreCase) && !PathName.Contains(Filter, ESearchCase::IgnoreCase))
        {
            continue;
        }
        ++Total;
        if (Items.Num() >= Cap) { continue; }
        UWorld* W = O->GetWorld();
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("path"), PathName);
        J->SetStringField(TEXT("name"), Name);
        J->SetStringField(TEXT("world"), W ? W->GetName() : FString());
        J->SetStringField(TEXT("world_type"), MCPDbg_WorldTypeStr(W));
        J->SetBoolField(TEXT("in_debugging_world"), DbgWorld != nullptr && W == DbgWorld);
        J->SetBoolField(TEXT("is_current"), O == Current);
        Items.Add(MakeShared<FJsonValueObject>(J));
    }

    // Fall back to the CDO if there are no live instances (so the caller always sees the class).
    bool bCdoFallback = false;
    if (Items.Num() == 0 && Total == 0)
    {
        if (UObject* CDO = GenClass->GetDefaultObject())
        {
            bCdoFallback = true;
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("path"), CDO->GetPathName());
            J->SetStringField(TEXT("name"), CDO->GetName());
            J->SetStringField(TEXT("world"), FString());
            J->SetStringField(TEXT("world_type"), TEXT("none"));
            J->SetBoolField(TEXT("is_cdo"), true);
            J->SetBoolField(TEXT("is_current"), CDO == Current);
            Items.Add(MakeShared<FJsonValueObject>(J));
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("generated_class"), GenClass->GetPathName());
    Root->SetBoolField(TEXT("pie_active"), DbgWorld != nullptr);
    Root->SetBoolField(TEXT("cdo_fallback"), bCdoFallback);
    if (Current) { Root->SetStringField(TEXT("current_debug_object"), Current->GetPathName()); }
    Root->SetNumberField(TEXT("instance_count"), Total);
    Root->SetNumberField(TEXT("returned"), Items.Num());
    Root->SetArrayField(TEXT("objects"), Items);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 2 · 8 — GetBlueprintDebugStateJson. Current instruction / most-recent breakpoint / stepping /
// world. Degrades to {"debugging":false} outside a live debugging session (NEVER errors). Detail:
// "full" adds the trace-sample count. (Detail otherwise reserved.)
// =====================================================================================================
FString UMCPReflectionLibrary::GetBlueprintDebugStateJson(const FString& Detail)
{
#if WITH_EDITOR
    UEdGraphNode* Cur = FKismetDebugUtilities::GetCurrentInstruction();      // VERIFY vs engine source (:207)
    UEdGraphNode* Brk = FKismetDebugUtilities::GetMostRecentBreakpointHit(); // VERIFY vs engine source (:210)
    const bool bStepping = FKismetDebugUtilities::IsSingleStepping();        // VERIFY vs engine source (:249)
    UWorld* W = FKismetDebugUtilities::GetCurrentDebuggingWorld();           // VERIFY vs engine source (:213)
    const bool bDebugging = (Cur != nullptr) || (Brk != nullptr) || (W != nullptr);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("debugging"), bDebugging);
    Root->SetBoolField(TEXT("single_stepping"), bStepping);
    if (W) { Root->SetStringField(TEXT("world"), W->GetName()); Root->SetStringField(TEXT("world_type"), MCPDbg_WorldTypeStr(W)); }
    else   { Root->SetField(TEXT("world"), MakeShared<FJsonValueNull>()); }

    if (Cur) { Root->SetObjectField(TEXT("current_node"), MCPDbg_NodeBrief(Cur)); }
    else     { Root->SetField(TEXT("current_node"), MakeShared<FJsonValueNull>()); }
    if (Brk) { Root->SetObjectField(TEXT("most_recent_breakpoint"), MCPDbg_NodeBrief(Brk)); }
    else     { Root->SetField(TEXT("most_recent_breakpoint"), MakeShared<FJsonValueNull>()); }

    if (Detail.Equals(TEXT("full"), ESearchCase::IgnoreCase))
    {
        Root->SetNumberField(TEXT("trace_sample_count"), FKismetDebugUtilities::GetTraceStack().Num());  // VERIFY vs engine source (:234)
    }
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 2 · 9 — GetBlueprintExecutionTraceJson. The FKismetTraceSample ring buffer (newest-first) mapped
// via FindSourceNodeForCodeLocation to source nodes. Optional BlueprintPath filters to samples whose
// context is an instance of that BP's generated class. Readable AFTER a PIE resume — the most usable of
// the four runtime readers. Empty (never errors) when nothing has executed.
// =====================================================================================================
FString UMCPReflectionLibrary::GetBlueprintExecutionTraceJson(const FString& BlueprintPath, int32 MaxResults)
{
#if WITH_EDITOR
    // Optional class filter.
    UClass* FilterClass = nullptr;
    if (!BlueprintPath.IsEmpty())
    {
        if (UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath))
        {
            FilterClass = BP->GeneratedClass;
        }
    }

    const TSimpleRingBuffer<FKismetTraceSample>& Stack = FKismetDebugUtilities::GetTraceStack();  // VERIFY vs engine source (:234)
    const int32 N = Stack.Num();
    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;

    TArray<TSharedPtr<FJsonValue>> Items;
    for (int32 i = 0; i < N && Items.Num() < Cap; ++i)
    {
        const FKismetTraceSample& S = Stack(i);                          // VERIFY vs engine source (BlueprintGeneratedClass.h:148, newest-first)
        UObject* Ctx = S.Context.Get();
        UFunction* Fn = S.Function.Get();
        if (FilterClass && (!Ctx || !Ctx->IsA(FilterClass))) { continue; }

        UEdGraphNode* Node = (Ctx && Fn)
            ? FKismetDebugUtilities::FindSourceNodeForCodeLocation(Ctx, Fn, S.Offset, /*bAllowImpreciseHit*/true)  // VERIFY vs engine source (:237)
            : nullptr;

        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (Node)
        {
            J->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
            J->SetStringField(TEXT("node_title"), Node->GetNodeTitle(ENodeTitleType::ListView).ToString());
            if (UEdGraph* G = Node->GetGraph()) { J->SetStringField(TEXT("graph"), G->GetName()); }
        }
        else
        {
            J->SetField(TEXT("node_guid"), MakeShared<FJsonValueNull>());
        }
        J->SetStringField(TEXT("context"), Ctx ? Ctx->GetPathName() : FString());
        J->SetStringField(TEXT("function"), Fn ? Fn->GetName() : FString());
        J->SetNumberField(TEXT("offset"), S.Offset);
        J->SetNumberField(TEXT("observation_time"), S.ObservationTime);
        Items.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("debugging"), N > 0);
    Root->SetNumberField(TEXT("sample_count"), N);
    Root->SetNumberField(TEXT("returned"), Items.Num());
    if (N == 0) { Root->SetStringField(TEXT("note"), TEXT("empty trace — run PIE (the ring fills during execution; readable after a resume)")); }
    Root->SetArrayField(TEXT("samples"), Items);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 2 · 10 — GetBlueprintCallStackJson. FFrame::GetScriptCallstack (bReturnEmpty=true) split into
// frames. Populated ONLY while script is executing/halted; empty otherwise (degrades, never errors).
// =====================================================================================================
FString UMCPReflectionLibrary::GetBlueprintCallStackJson(int32 MaxResults)
{
#if WITH_EDITOR
    const FString Raw = FFrame::GetScriptCallstack(/*bReturnEmpty*/true, /*bTopOfStackOnly*/false);  // VERIFY vs engine source (Stack.h:270)

    TArray<TSharedPtr<FJsonValue>> Frames;
    const int32 Cap = (MaxResults > 0) ? MaxResults : MAX_int32;
    if (!Raw.IsEmpty())
    {
        TArray<FString> Lines;
        Raw.ParseIntoArrayLines(Lines, /*bCullEmpty*/true);
        for (FString& Line : Lines)
        {
            if (Frames.Num() >= Cap) { break; }
            Line.TrimStartAndEndInline();
            if (Line.IsEmpty()) { continue; }
            Frames.Add(MakeShared<FJsonValueString>(Line));
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("debugging"), Frames.Num() > 0);
    Root->SetNumberField(TEXT("frame_count"), Frames.Num());
    if (Frames.Num() == 0) { Root->SetStringField(TEXT("note"), TEXT("no active script callstack — only populated while a script frame is executing/halted")); }
    Root->SetArrayField(TEXT("frames"), Frames);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WAVE 2 · 11 — InspectBlueprintDebugValueJson. Property tree of the SET debug object. Built pin-free via
// FKismetDebugUtilities::GetDebugInfoInternal (UNREALED_API) per top-level property of the debug object's
// class (the plan's nullptr-WatchPin GetDebugInfo call yields nothing — FindClassPropertyForPin returns
// null for a null pin — so this walks the object directly instead; SEE the report). Path descends via
// FPropertyInstanceInfo::ResolvePathToProperty; Depth bounds GetChildren recursion. Needs a debug object
// (PIE for a live instance); degrades to a note when none is set (NEVER errors).
// =====================================================================================================
FString UMCPReflectionLibrary::InspectBlueprintDebugValueJson(const FString& BlueprintPath, const FString& Path,
    const FString& Filter, int32 Depth, int32 MaxResults)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPDbg_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPDbg_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    UObject* Active = BP->GetObjectBeingDebugged();                       // VERIFY vs engine source (Blueprint.h:883)
    if (!Active)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("blueprint"), BP->GetName());
        Root->SetBoolField(TEXT("debugging"), false);
        Root->SetStringField(TEXT("note"), TEXT("no debug object set — call set_blueprint_debug_object (a live PIE instance) first"));
        Root->SetArrayField(TEXT("values"), TArray<TSharedPtr<FJsonValue>>());
        return MCPDbg_Serialize(Root);
    }

    UClass* Cls = Active->GetClass();
    const int32 ChildCap = (MaxResults > 0) ? MaxResults : 256;
    const int32 DepthClamped = FMath::Clamp(Depth, 0, 8);

    // Recursive serializer for an FPropertyInstanceInfo (public Name/Value/Type FText members; GetChildren
    // is UNREALED_API). Bounded by DepthClamped + ChildCap; guarded against a null info.
    TFunction<TSharedRef<FJsonObject>(const TSharedPtr<FPropertyInstanceInfo>&, int32)> SerInfo;
    SerInfo = [&SerInfo, ChildCap](const TSharedPtr<FPropertyInstanceInfo>& Info, int32 Remaining) -> TSharedRef<FJsonObject>
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Info.IsValid()) { return J; }
        J->SetStringField(TEXT("name"), Info->DisplayName.IsEmpty() ? Info->Name.ToString() : Info->DisplayName.ToString());  // VERIFY vs engine source (KismetDebugUtilities.h:90-92)
        J->SetStringField(TEXT("value"), Info->Value.ToString());
        J->SetStringField(TEXT("type"), Info->Type.ToString());
        if (Remaining > 0)
        {
            const TArray<TSharedPtr<FPropertyInstanceInfo>>& Kids = Info->GetChildren();  // VERIFY vs engine source (:88, UNREALED_API)
            if (Kids.Num() > 0)
            {
                TArray<TSharedPtr<FJsonValue>> ChildArr;
                for (const TSharedPtr<FPropertyInstanceInfo>& Kid : Kids)
                {
                    if (ChildArr.Num() >= ChildCap) { break; }
                    if (Kid.IsValid()) { ChildArr.Add(MakeShared<FJsonValueObject>(SerInfo(Kid, Remaining - 1))); }
                }
                J->SetArrayField(TEXT("children"), ChildArr);
            }
        }
        return J;
    };

    TArray<TSharedPtr<FJsonValue>> Values;

    // Build the FPropertyInstanceInfo for one top-level property of the debug object.
    auto MakeInfoForProperty = [&](FProperty* Prop) -> TSharedPtr<FPropertyInstanceInfo>
    {
        TSharedPtr<FPropertyInstanceInfo> Info;
        if (!Prop) { return Info; }
        const void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Active);   // Prop is a member of Cls (iterated over Cls) -> safe
        if (!ValuePtr) { return Info; }
        FKismetDebugUtilities::GetDebugInfoInternal(Info, Prop, ValuePtr);   // VERIFY vs engine source (:450, UNREALED_API)
        return Info;
    };

    if (!Path.IsEmpty())
    {
        // Split "A.B.C" -> head property A, descend [B,C] via ResolvePathToProperty.
        TArray<FString> Segs;
        Path.ParseIntoArray(Segs, TEXT("."), /*bCullEmpty*/true);
        if (Segs.Num() == 0) { Segs.Add(Path); }

        FProperty* Head = Cls->FindPropertyByName(FName(*Segs[0]));
        if (!Head)
        {
            // authored-name fallback
            for (TFieldIterator<FProperty> It(Cls); It; ++It)
            {
                if (It->GetAuthoredName().Equals(Segs[0], ESearchCase::IgnoreCase) ||
                    It->GetName().Equals(Segs[0], ESearchCase::IgnoreCase)) { Head = *It; break; }
            }
        }
        if (!Head)
        {
            return MCPDbg_Err(FString::Printf(TEXT("debug object class '%s' has no property '%s'"), *Cls->GetName(), *Segs[0]));
        }

        TSharedPtr<FPropertyInstanceInfo> Info = MakeInfoForProperty(Head);
        if (Info.IsValid() && Segs.Num() > 1)
        {
            TArray<FName> Rest;
            for (int32 i = 1; i < Segs.Num(); ++i) { Rest.Add(FName(*Segs[i])); }
            TSharedPtr<FPropertyInstanceInfo> Resolved = Info->ResolvePathToProperty(Rest);  // VERIFY vs engine source (:82, UNREALED_API)
            if (Resolved.IsValid()) { Info = Resolved; }
        }
        if (Info.IsValid()) { Values.Add(MakeShared<FJsonValueObject>(SerInfo(Info, DepthClamped))); }
    }
    else
    {
        // Enumerate top-level properties (Filter = case-insensitive substring on the property name).
        const bool bHasFilter = !Filter.IsEmpty();
        for (TFieldIterator<FProperty> It(Cls); It && Values.Num() < ChildCap; ++It)
        {
            FProperty* Prop = *It;
            if (!Prop) { continue; }
            if (bHasFilter && !Prop->GetName().Contains(Filter, ESearchCase::IgnoreCase) &&
                !Prop->GetAuthoredName().Contains(Filter, ESearchCase::IgnoreCase)) { continue; }
            TSharedPtr<FPropertyInstanceInfo> Info = MakeInfoForProperty(Prop);
            if (Info.IsValid()) { Values.Add(MakeShared<FJsonValueObject>(SerInfo(Info, DepthClamped))); }
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetBoolField(TEXT("debugging"), true);
    Root->SetStringField(TEXT("debug_object"), Active->GetPathName());
    Root->SetStringField(TEXT("debug_class"), Cls->GetPathName());
    Root->SetNumberField(TEXT("returned"), Values.Num());
    Root->SetArrayField(TEXT("values"), Values);
    return MCPDbg_Serialize(Root);
#else
    return MCPDbg_Err(TEXT("editor-only"));
#endif
}
