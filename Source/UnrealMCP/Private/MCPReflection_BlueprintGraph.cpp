// UnrealMCP — BLUEPRINT K2 GRAPH-CORE node subsystem (C++ DRAFT 2026-08-19).
//
// The reader + node primitives that every higher-level blueprint tool composes on. Member
// DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator (do NOT edit the .h here). Ten handlers:
//
//   READERS (no mutation, no ledger):
//     1) GetBlueprintGraphJson     — full walk of one UEdGraph: per node {class,title,node_guid,x,y,
//                                     pins:[{name,direction,pin_type,default_value,linked_to:[...]}]}.
//     2) SearchBlueprintNodesJson  — compact live-node enumeration (bWithPins adds pin detail).
//     3) DescribeBlueprintNodeJson — one node's full pins/props by GUID.
//
//   NODE PRIMITIVES (writes; each folds its inverse into the Python per-session undo ledger):
//     4) AddBlueprintNodeJson      — spec-driven K2 node create (call_function / variable_get /
//                                     variable_set / branch / cast / knot / event / custom_event).
//     5) ConnectBlueprintNodesJson — Schema->TryCreateConnection; returns the bool + a clear reason.
//     6) SetBlueprintPinDefaultJson— Schema->TrySetDefaultValue / TrySetDefaultObject; captures prior.
//     7) BreakBlueprintNodeLinkJson— break one specific link or all; captures broken links for reconnect.
//     8) DeleteBlueprintNodeJson   — generalized RemoveEventNodeByGuid: remove ANY node by GUID; captures
//                                     a LOSSY re-add spec for the inverse.
//     9) SetBlueprintNodePropertyJson — set an FProperty on the UK2Node; captures prior; reconstructs.
//
//   COMPILE (finalize):
//    10) CompileBlueprintByPath    — FKismetEditorUtilities::CompileBlueprint + status echo. Writes do NOT
//                                     compile per-node (see COMPILE STRATEGY below); Python calls this once.
//
// COMPILE STRATEGY (chosen: MARK-MODIFIED per write, DEFERRED compile):
//   Every write ends with FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(bp) ONLY — never a
//   per-node FKismetEditorUtilities::CompileBlueprint. Rationale: (a) compiling a half-built graph (a cast
//   with no target wired, a branch with no condition, an unconnected exec) is wasteful and emits spurious
//   errors; (b) compile is the single most crash-prone op in this batch. The caller batches its edits then
//   calls CompileBlueprintByPath (or the stock unreal.BlueprintEditorLibrary.compile_blueprint) exactly
//   once to finalize. Nodes whose PIN SHAPE depends on a reference (variable_get/set, cast) DO get a
//   node->ReconstructNode() so their pins are correct without a full compile.
//
// CRASH-SAFETY (the editor hard-crashes on bad pin ops): every pin lookup is FindPin + null-check (NEVER
// FindPinChecked on user input); every node lookup is a guarded FGuid::Parse + linear scan; class/function
// resolution is validated before NewObject; TryCreateConnection is gated on two non-null pins; ImportText
// failures are reported, not asserted. Any miss returns {"error":...}. Reads and writes are #if WITH_EDITOR
// guarded (editor-only handlers return {"error":"editor-only"} in a cooked build).
//
// Module deps: BlueprintGraph (UK2Node_* / EdGraphSchema_K2), UnrealEd (FBlueprintEditorUtils /
// FKismetEditorUtilities), Engine (UBlueprint / UEdGraph / UEdGraphPin). ALL already present in
// UnrealMCP.Build.cs -> NO Build.cs change, NO export patch.
//
// Anonymous-namespace helpers are prefixed `MCPBpG_` so they stay unique in the module's unity build.
// Every engine-API touch point is tagged "VERIFY vs engine source" for the coordinator's live pass.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"
#include "UObject/UnrealType.h"        // FProperty::ImportText_InContainer / ExportText_InContainer
#include "UObject/UObjectGlobals.h"    // LoadObject / FindObject / NewObject
#include "UObject/Object.h"
#include "Misc/PackageName.h"          // FPackageName::GetShortName (bare-path load fallback)

#include "Engine/Blueprint.h"                 // UBlueprint / UbergraphPages / FunctionGraphs / Status / EBlueprintStatus
#include "Engine/BlueprintGeneratedClass.h"   // UBlueprintGeneratedClass (BP-asset -> generated class resolve)
#include "GameFramework/Actor.h"              // AActor (default event owner)
#include "EdGraph/EdGraph.h"                  // UEdGraph::Nodes / AddNode
#include "EdGraph/EdGraphNode.h"              // UEdGraphNode (NodeGuid / NodePosX / Pins / GetNodeTitle / ReconstructNode / CreateNewGuid)
#include "EdGraph/EdGraphPin.h"               // UEdGraphPin / FEdGraphPinType / EEdGraphPinDirection

#if WITH_EDITOR
#include "EdGraphSchema_K2.h"                 // UEdGraphSchema_K2 (TryCreateConnection / TrySetDefaultValue / CanCreateConnection / PC_*)
#include "K2Node.h"                           // UK2Node (base)
#include "K2Node_CallFunction.h"             // UK2Node_CallFunction (FunctionReference.SetExternalMember)
#include "K2Node_VariableGet.h"              // UK2Node_VariableGet
#include "K2Node_VariableSet.h"              // UK2Node_VariableSet
#include "K2Node_Variable.h"                 // UK2Node_Variable::VariableReference (base of get/set)
#include "K2Node_IfThenElse.h"              // UK2Node_IfThenElse (branch)
#include "K2Node_DynamicCast.h"             // UK2Node_DynamicCast (TargetType)
#include "K2Node_Knot.h"                    // UK2Node_Knot (reroute)
#include "K2Node_Event.h"                   // UK2Node_Event (EventReference / bOverrideFunction / CustomFunctionName)
#include "K2Node_CustomEvent.h"             // UK2Node_CustomEvent (CustomFunctionName)
#include "Engine/MemberReference.h"          // FMemberReference::SetExternalMember / SetSelfMember
#include "Kismet2/BlueprintEditorUtils.h"    // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified / RemoveNode
#include "Kismet2/KismetEditorUtilities.h"   // FKismetEditorUtilities::CompileBlueprint
#endif // WITH_EDITOR

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPBpG_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error"). Success objects
    // never carry an "error" key.
    FString MCPBpG_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPBpG_Serialize(Root);
    }

#if WITH_EDITOR
    // Load a UBlueprint from an asset path ("/Game/Path/BP_Foo.BP_Foo" or "/Game/Path/BP_Foo").
    UBlueprint* MCPBpG_LoadBlueprint(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UBlueprint* BP = LoadObject<UBlueprint>(nullptr, *Path);
        if (!BP)
        {
            // Tolerate a bare package path lacking the .Object suffix.
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            BP = LoadObject<UBlueprint>(nullptr, *Full);
        }
        return BP;
    }

    // Resolve a UClass from a native path ("/Script/Engine.KismetSystemLibrary"), a short name
    // ("KismetSystemLibrary"), or a BP asset path (-> its GeneratedClass). Mirrors the BlueprintExt idiom.
    UClass* MCPBpG_ResolveClass(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UClass* C = LoadObject<UClass>(nullptr, *Path);
        if (!C) { C = FindObject<UClass>(nullptr, *Path); }
        if (!C) { C = UClass::TryFindTypeSlow<UClass>(Path); }              // VERIFY vs engine source (UClass::TryFindTypeSlow)
        if (!C)
        {
            // Common convenience: bare engine class name.
            const FString EnginePath = FString::Printf(TEXT("/Script/Engine.%s"), *Path);
            C = LoadObject<UClass>(nullptr, *EnginePath);
        }
        if (!C)
        {
            if (UObject* Obj = LoadObject<UObject>(nullptr, *Path))
            {
                if (UBlueprint* BP = Cast<UBlueprint>(Obj))
                {
                    C = BP->GeneratedClass;                                  // VERIFY vs engine source (UBlueprint::GeneratedClass)
                }
            }
        }
        return C;
    }

    // Resolve the target UEdGraph. GraphName ""/"EventGraph" -> the ubergraph (event graph); otherwise a
    // named function graph (then a named ubergraph page as a fallback).
    UEdGraph* MCPBpG_ResolveGraph(UBlueprint* BP, const FString& GraphName, FString& OutErr)
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
    UEdGraphNode* MCPBpG_FindNode(UEdGraph* Graph, const FString& GuidStr, FString& OutErr)
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
    UEdGraphPin* MCPBpG_FindPin(UEdGraphNode* Node, const FString& PinName)
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

    const UEdGraphSchema_K2* MCPBpG_K2Schema()
    {
        return GetDefault<UEdGraphSchema_K2>();                             // VERIFY vs engine source (schema CDO is always valid)
    }

    // FEdGraphPinType -> JSON (the "pin-type serializer" everything composes on). Mirrors DescribeVarType
    // in MCPReflection_Structs.cpp but for a live pin's FEdGraphPinType.
    TSharedRef<FJsonObject> MCPBpG_SerializePinType(const FEdGraphPinType& T)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("category"), T.PinCategory.ToString());
        if (!T.PinSubCategory.IsNone())
        {
            J->SetStringField(TEXT("sub_category"), T.PinSubCategory.ToString());
        }
        if (T.PinSubCategoryObject.IsValid())                              // TWeakObjectPtr — .IsValid()/.Get() are safe
        {
            if (UObject* O = T.PinSubCategoryObject.Get())
            {
                J->SetStringField(TEXT("sub_category_object"), O->GetPathName());
                J->SetStringField(TEXT("sub_category_object_name"), O->GetName());
            }
        }
        const TCHAR* Cont =
            T.ContainerType == EPinContainerType::Array ? TEXT("array") :
            T.ContainerType == EPinContainerType::Set   ? TEXT("set")   :
            T.ContainerType == EPinContainerType::Map   ? TEXT("map")   : TEXT("none");
        J->SetStringField(TEXT("container"), Cont);
        J->SetBoolField(TEXT("is_reference"), T.bIsReference);
        J->SetBoolField(TEXT("is_const"), T.bIsConst);
        if (T.ContainerType == EPinContainerType::Map)
        {
            TSharedRef<FJsonObject> V = MakeShared<FJsonObject>();
            V->SetStringField(TEXT("category"), T.PinValueType.TerminalCategory.ToString());  // VERIFY vs engine source (FEdGraphTerminalType)
            if (!T.PinValueType.TerminalSubCategory.IsNone())
            {
                V->SetStringField(TEXT("sub_category"), T.PinValueType.TerminalSubCategory.ToString());
            }
            if (T.PinValueType.TerminalSubCategoryObject.IsValid() && T.PinValueType.TerminalSubCategoryObject.Get())
            {
                V->SetStringField(TEXT("sub_category_object"), T.PinValueType.TerminalSubCategoryObject.Get()->GetPathName());
            }
            J->SetObjectField(TEXT("value_type"), V);
        }
        return J;
    }

    // One pin -> JSON. bWithLinks appends linked_to:[{node_guid, pin_name}].
    TSharedRef<FJsonObject> MCPBpG_SerializePin(UEdGraphPin* Pin, bool bWithLinks)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Pin)
        {
            return J;
        }
        J->SetStringField(TEXT("name"), Pin->PinName.ToString());
        J->SetStringField(TEXT("direction"), Pin->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
        J->SetObjectField(TEXT("pin_type"), MCPBpG_SerializePinType(Pin->PinType));
        J->SetStringField(TEXT("default_value"), Pin->DefaultValue);
        if (Pin->DefaultObject)
        {
            J->SetStringField(TEXT("default_object"), Pin->DefaultObject->GetPathName());
        }
        if (!Pin->DefaultTextValue.IsEmpty())
        {
            J->SetStringField(TEXT("default_text"), Pin->DefaultTextValue.ToString());
        }
        J->SetStringField(TEXT("pin_id"), Pin->PinId.ToString());
        if (bWithLinks)
        {
            TArray<TSharedPtr<FJsonValue>> Links;
            for (UEdGraphPin* L : Pin->LinkedTo)
            {
                if (!L || !L->GetOwningNodeUnchecked())                     // GetOwningNodeUnchecked guards a stale/None owner
                {
                    continue;
                }
                TSharedRef<FJsonObject> LO = MakeShared<FJsonObject>();
                LO->SetStringField(TEXT("node_guid"), L->GetOwningNode()->NodeGuid.ToString());
                LO->SetStringField(TEXT("pin_name"), L->PinName.ToString());
                Links.Add(MakeShared<FJsonValueObject>(LO));
            }
            J->SetArrayField(TEXT("linked_to"), Links);
        }
        return J;
    }

    // One node -> JSON: {class, title, node_guid, x, y[, pins]}.
    TSharedRef<FJsonObject> MCPBpG_SerializeNode(UEdGraphNode* Node, bool bWithPins, bool bWithLinks)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Node)
        {
            return J;
        }
        J->SetStringField(TEXT("class"), Node->GetClass()->GetName());
        // ListView title is compact + single-line (FullTitle can be multi-line / more expensive).
        J->SetStringField(TEXT("title"), Node->GetNodeTitle(ENodeTitleType::ListView).ToString());  // VERIFY vs engine source (UEdGraphNode::GetNodeTitle)
        J->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
        J->SetNumberField(TEXT("x"), Node->NodePosX);
        J->SetNumberField(TEXT("y"), Node->NodePosY);
        if (bWithPins)
        {
            TArray<TSharedPtr<FJsonValue>> Pins;
            for (UEdGraphPin* P : Node->Pins)
            {
                if (!P) { continue; }
                Pins.Add(MakeShared<FJsonValueObject>(MCPBpG_SerializePin(P, bWithLinks)));
            }
            J->SetArrayField(TEXT("pins"), Pins);
        }
        return J;
    }

    TSharedPtr<FJsonObject> MCPBpG_ParseObject(const FString& JsonText)
    {
        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
        if (FJsonSerializer::Deserialize(Reader, Obj) && Obj.IsValid())
        {
            return Obj;
        }
        return nullptr;
    }

    FString MCPBpG_StatusToString(EBlueprintStatus S)
    {
        switch (S)                                                          // VERIFY vs engine source (EBlueprintStatus)
        {
            case BS_UpToDate:             return TEXT("up_to_date");
            case BS_Dirty:                return TEXT("dirty");
            case BS_Error:                return TEXT("error");
            case BS_UpToDateWithWarnings: return TEXT("up_to_date_with_warnings");
            case BS_BeingCreated:         return TEXT("being_created");
            default:                      return TEXT("unknown");
        }
    }

    // Construct one K2 node from a spec object. On success returns the (not-yet-added) node with pins
    // ALLOCATED; the caller positions it, AddNode's it, assigns a fresh guid, and reconstructs if needed.
    // Sets OutErr + returns nullptr on any failure (unknown kind / unresolved class / missing function).
    UK2Node* MCPBpG_ConstructNode(UEdGraph* Graph, UBlueprint* BP, const TSharedPtr<FJsonObject>& Spec,
                                  bool& bOutPinsDependOnRef, FString& OutErr)
    {
        bOutPinsDependOnRef = false;

        FString Kind;
        if (!Spec->TryGetStringField(TEXT("kind"), Kind))
        {
            Spec->TryGetStringField(TEXT("type"), Kind);                    // alias
        }
        Kind = Kind.ToLower();
        if (Kind.IsEmpty())
        {
            OutErr = TEXT("node spec missing 'kind'");
            return nullptr;
        }

        // ---- call_function {class, function} -----------------------------------------------------
        if (Kind == TEXT("call_function") || Kind == TEXT("callfunction") || Kind == TEXT("function"))
        {
            FString ClassPath, FuncName;
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            Spec->TryGetStringField(TEXT("function"), FuncName);
            if (FuncName.IsEmpty())
            {
                OutErr = TEXT("call_function spec requires 'function'");
                return nullptr;
            }
            UClass* OwnerClass = MCPBpG_ResolveClass(ClassPath);
            if (!OwnerClass)
            {
                OutErr = FString::Printf(TEXT("call_function: could not resolve class '%s'"), *ClassPath);
                return nullptr;
            }
            if (!OwnerClass->FindFunctionByName(FName(*FuncName)))          // VERIFY vs engine source (UClass::FindFunctionByName)
            {
                OutErr = FString::Printf(TEXT("call_function: class '%s' has no function '%s'"),
                                         *OwnerClass->GetName(), *FuncName);
                return nullptr;
            }
            UK2Node_CallFunction* Node = NewObject<UK2Node_CallFunction>(Graph);
            Node->FunctionReference.SetExternalMember(FName(*FuncName), OwnerClass);  // VERIFY vs engine source
            return Node;
        }

        // ---- variable_get / variable_set {var_name} ----------------------------------------------
        if (Kind == TEXT("variable_get") || Kind == TEXT("variableget") || Kind == TEXT("get"))
        {
            FString VarName;
            Spec->TryGetStringField(TEXT("var_name"), VarName);
            if (VarName.IsEmpty()) { Spec->TryGetStringField(TEXT("variable"), VarName); }
            if (VarName.IsEmpty())
            {
                OutErr = TEXT("variable_get spec requires 'var_name'");
                return nullptr;
            }
            UK2Node_VariableGet* Node = NewObject<UK2Node_VariableGet>(Graph);
            Node->VariableReference.SetSelfMember(FName(*VarName));          // VERIFY vs engine source (FMemberReference::SetSelfMember)
            bOutPinsDependOnRef = true;
            return Node;
        }
        if (Kind == TEXT("variable_set") || Kind == TEXT("variableset") || Kind == TEXT("set"))
        {
            FString VarName;
            Spec->TryGetStringField(TEXT("var_name"), VarName);
            if (VarName.IsEmpty()) { Spec->TryGetStringField(TEXT("variable"), VarName); }
            if (VarName.IsEmpty())
            {
                OutErr = TEXT("variable_set spec requires 'var_name'");
                return nullptr;
            }
            UK2Node_VariableSet* Node = NewObject<UK2Node_VariableSet>(Graph);
            Node->VariableReference.SetSelfMember(FName(*VarName));
            bOutPinsDependOnRef = true;
            return Node;
        }

        // ---- branch (if/then/else) ---------------------------------------------------------------
        if (Kind == TEXT("branch") || Kind == TEXT("if") || Kind == TEXT("ifthenelse"))
        {
            return NewObject<UK2Node_IfThenElse>(Graph);
        }

        // ---- cast {class} ------------------------------------------------------------------------
        if (Kind == TEXT("cast") || Kind == TEXT("dynamic_cast") || Kind == TEXT("dynamiccast"))
        {
            FString ClassPath;
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            if (ClassPath.IsEmpty()) { Spec->TryGetStringField(TEXT("target_class"), ClassPath); }
            UClass* TargetClass = MCPBpG_ResolveClass(ClassPath);
            if (!TargetClass)
            {
                OutErr = FString::Printf(TEXT("cast: could not resolve target class '%s'"), *ClassPath);
                return nullptr;
            }
            UK2Node_DynamicCast* Node = NewObject<UK2Node_DynamicCast>(Graph);
            Node->TargetType = TargetClass;                                 // VERIFY vs engine source (UK2Node_DynamicCast::TargetType) — MUST precede AllocateDefaultPins
            bOutPinsDependOnRef = true;
            return Node;
        }

        // ---- knot (reroute) ----------------------------------------------------------------------
        if (Kind == TEXT("knot") || Kind == TEXT("reroute"))
        {
            return NewObject<UK2Node_Knot>(Graph);
        }

        // ---- event {event_name, class?} (override, e.g. ReceiveBeginPlay) ------------------------
        if (Kind == TEXT("event"))
        {
            FString EventName, ClassPath;
            Spec->TryGetStringField(TEXT("event_name"), EventName);
            if (EventName.IsEmpty()) { Spec->TryGetStringField(TEXT("function"), EventName); }
            if (EventName.IsEmpty())
            {
                OutErr = TEXT("event spec requires 'event_name' (the OVERRIDE function name, e.g. 'ReceiveBeginPlay')");
                return nullptr;
            }
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            UClass* OwnerClass = ClassPath.IsEmpty()
                ? (BP && BP->ParentClass ? (UClass*)BP->ParentClass : AActor::StaticClass())
                : MCPBpG_ResolveClass(ClassPath);
            if (!OwnerClass)
            {
                OutErr = FString::Printf(TEXT("event: could not resolve owner class '%s'"), *ClassPath);
                return nullptr;
            }
            UK2Node_Event* Node = NewObject<UK2Node_Event>(Graph);
            Node->EventReference.SetExternalMember(FName(*EventName), OwnerClass);  // VERIFY vs engine source (mirrors FMCPBlueprintUtils::AddEventNode)
            Node->bOverrideFunction = true;
            return Node;
        }

        // ---- custom_event {event_name} -----------------------------------------------------------
        if (Kind == TEXT("custom_event") || Kind == TEXT("customevent"))
        {
            FString EventName;
            Spec->TryGetStringField(TEXT("event_name"), EventName);
            if (EventName.IsEmpty()) { Spec->TryGetStringField(TEXT("name"), EventName); }
            if (EventName.IsEmpty())
            {
                OutErr = TEXT("custom_event spec requires 'event_name'");
                return nullptr;
            }
            UK2Node_CustomEvent* Node = NewObject<UK2Node_CustomEvent>(Graph);
            Node->CustomFunctionName = FName(*EventName);                    // VERIFY vs engine source (UK2Node_CustomEvent::CustomFunctionName)
            return Node;
        }

        OutErr = FString::Printf(TEXT("unknown node kind '%s' (expected call_function|variable_get|"
            "variable_set|branch|cast|knot|event|custom_event)"), *Kind);
        return nullptr;
    }
#endif // WITH_EDITOR
} // namespace

// =====================================================================================================
// READER 1 — GetBlueprintGraphJson
// =====================================================================================================
FString UMCPReflectionLibrary::GetBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph)
    {
        return MCPBpG_Err(Err);
    }

    TArray<TSharedPtr<FJsonValue>> Nodes;
    for (UEdGraphNode* N : Graph->Nodes)
    {
        if (!N) { continue; }
        Nodes.Add(MakeShared<FJsonValueObject>(MCPBpG_SerializeNode(N, /*bWithPins*/true, /*bWithLinks*/true)));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("blueprint_path"), BP->GetPathName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetNumberField(TEXT("node_count"), Nodes.Num());
    Root->SetArrayField(TEXT("nodes"), Nodes);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// READER 2 — SearchBlueprintNodesJson (compact enumeration; bWithPins adds pin detail)
// =====================================================================================================
FString UMCPReflectionLibrary::SearchBlueprintNodesJson(const FString& BlueprintPath, const FString& GraphName, bool bWithPins)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph)
    {
        return MCPBpG_Err(Err);
    }

    TArray<TSharedPtr<FJsonValue>> Nodes;
    for (UEdGraphNode* N : Graph->Nodes)
    {
        if (!N) { continue; }
        // Compact: pins WITHOUT link detail (link walking is the heavy part). bWithPins gates pins entirely.
        Nodes.Add(MakeShared<FJsonValueObject>(MCPBpG_SerializeNode(N, /*bWithPins*/bWithPins, /*bWithLinks*/false)));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetNumberField(TEXT("node_count"), Nodes.Num());
    Root->SetArrayField(TEXT("nodes"), Nodes);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// READER 3 — DescribeBlueprintNodeJson (one node, full pins + links)
// =====================================================================================================
FString UMCPReflectionLibrary::DescribeBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph)
    {
        return MCPBpG_Err(Err);
    }
    UEdGraphNode* Node = MCPBpG_FindNode(Graph, NodeGuid, Err);
    if (!Node)
    {
        return MCPBpG_Err(Err);
    }

    TSharedRef<FJsonObject> Root = MCPBpG_SerializeNode(Node, /*bWithPins*/true, /*bWithLinks*/true);
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetBoolField(TEXT("enabled"), Node->IsNodeEnabled());            // VERIFY vs engine source (UEdGraphNode::IsNodeEnabled)
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 4 — AddBlueprintNodeJson (spec-driven K2 node create). Inverse: DeleteBlueprintNodeJson(guid).
// =====================================================================================================
FString UMCPReflectionLibrary::AddBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeSpecJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph)
    {
        return MCPBpG_Err(Err);
    }
    TSharedPtr<FJsonObject> Spec = MCPBpG_ParseObject(NodeSpecJson);
    if (!Spec.IsValid())
    {
        return MCPBpG_Err(TEXT("node_spec is not a JSON object"));
    }

    // Optional position (defaults 0,0).
    double PosX = 0.0, PosY = 0.0;
    Spec->TryGetNumberField(TEXT("x"), PosX);
    Spec->TryGetNumberField(TEXT("y"), PosY);

    UK2Node* NewNode = nullptr;
    bool bPinsDependOnRef = false;
    // Construction (NewObject + ref set) can be re-entrant into engine code; wrap defensively.
    try
    {
        NewNode = MCPBpG_ConstructNode(Graph, BP, Spec, bPinsDependOnRef, Err);
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during node construction"));
    }
    if (!NewNode)
    {
        return MCPBpG_Err(Err);
    }

    // Allocate pins (for cast, TargetType was set before this; for variable, the self-member is set).
    try
    {
        NewNode->AllocateDefaultPins();                                    // VERIFY vs engine source (UEdGraphNode::AllocateDefaultPins)
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during AllocateDefaultPins"));
    }

    NewNode->NodePosX = (int32)PosX;
    NewNode->NodePosY = (int32)PosY;
    Graph->AddNode(NewNode, /*bUserAction*/false, /*bSelectNewNode*/false);  // VERIFY vs engine source (UEdGraph::AddNode)
    NewNode->CreateNewGuid();                                              // fresh persistent guid AFTER add

    // Reconstruct nodes whose pin shape derives from a reference so pins reflect the resolved var/class.
    if (bPinsDependOnRef)
    {
        try { NewNode->ReconstructNode(); } catch (...) { /* keep the node; pins stay as allocated */ }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);        // VERIFY vs engine source — NO per-node compile

    TSharedRef<FJsonObject> Root = MCPBpG_SerializeNode(NewNode, /*bWithPins*/true, /*bWithLinks*/false);
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetBoolField(TEXT("added"), true);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 5 — ConnectBlueprintNodesJson. TryCreateConnection SILENTLY REJECTS incompatible pins: we report
// the bool + the schema's own reason, and NEVER assume success. Inverse: break that specific link.
// =====================================================================================================
FString UMCPReflectionLibrary::ConnectBlueprintNodesJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& FromGuid, const FString& FromPin, const FString& ToGuid, const FString& ToPin)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpG_Err(Err); }

    UEdGraphNode* FromNode = MCPBpG_FindNode(Graph, FromGuid, Err);
    if (!FromNode) { return MCPBpG_Err(Err); }
    UEdGraphNode* ToNode = MCPBpG_FindNode(Graph, ToGuid, Err);
    if (!ToNode) { return MCPBpG_Err(Err); }

    UEdGraphPin* A = MCPBpG_FindPin(FromNode, FromPin);
    if (!A) { return MCPBpG_Err(FString::Printf(TEXT("from-node has no pin '%s'"), *FromPin)); }
    UEdGraphPin* B = MCPBpG_FindPin(ToNode, ToPin);
    if (!B) { return MCPBpG_Err(FString::Printf(TEXT("to-node has no pin '%s'"), *ToPin)); }

    const UEdGraphSchema_K2* Schema = MCPBpG_K2Schema();
    if (!Schema)
    {
        return MCPBpG_Err(TEXT("K2 schema unavailable"));
    }

    // Ask the schema WHY first (for a clear message), then perform the connection.
    const FPinConnectionResponse Resp = Schema->CanCreateConnection(A, B);  // VERIFY vs engine source (FPinConnectionResponse.Message/.Response)
    bool bConnected = false;
    try
    {
        bConnected = Schema->TryCreateConnection(A, B);                    // VERIFY vs engine source — returns bool; may auto-insert a conversion node
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during TryCreateConnection"));
    }

    if (bConnected)
    {
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("blueprint"), BP->GetName());
        Root->SetStringField(TEXT("graph"), Graph->GetName());
        Root->SetBoolField(TEXT("connected"), true);
        Root->SetStringField(TEXT("message"), Resp.Message.ToString());
        // Flag the lossy case: the schema may have inserted a conversion/promotion node between A and B,
        // which a plain break-link inverse will NOT remove.
        const bool bConverted = (Resp.Response == CONNECT_RESPONSE_MAKE_WITH_CONVERSION_NODE);
        Root->SetBoolField(TEXT("inserted_conversion_node"), bConverted);
        return MCPBpG_Serialize(Root);
    }

    // Rejected: return an error so the Python side does NOT ledger an inverse. Include the reason.
    FString Reason = Resp.Message.ToString();
    if (Reason.IsEmpty()) { Reason = TEXT("incompatible pins/directions"); }
    return MCPBpG_Err(FString::Printf(TEXT("TryCreateConnection rejected: %s"), *Reason));
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 6 — SetBlueprintPinDefaultJson. Captures prior default (value + object + text). Inverse: restore.
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintPinDefaultJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, const FString& PinName, const FString& ValueJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpG_Err(Err); }
    UEdGraphNode* Node = MCPBpG_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPBpG_Err(Err); }
    UEdGraphPin* Pin = MCPBpG_FindPin(Node, PinName);
    if (!Pin) { return MCPBpG_Err(FString::Printf(TEXT("node has no pin '%s'"), *PinName)); }

    const UEdGraphSchema_K2* Schema = MCPBpG_K2Schema();
    if (!Schema) { return MCPBpG_Err(TEXT("K2 schema unavailable")); }

    // Capture prior default for the inverse.
    const FString PrevValue = Pin->DefaultValue;
    const FString PrevObject = Pin->DefaultObject ? Pin->DefaultObject->GetPathName() : FString();
    const FString PrevText = Pin->DefaultTextValue.ToString();

    // Decide value vs object by pin category.
    const FName Cat = Pin->PinType.PinCategory;
    const bool bObjectPin =
        Cat == UEdGraphSchema_K2::PC_Object || Cat == UEdGraphSchema_K2::PC_Class ||
        Cat == UEdGraphSchema_K2::PC_Interface || Cat == UEdGraphSchema_K2::PC_SoftObject ||
        Cat == UEdGraphSchema_K2::PC_SoftClass;

    try
    {
        if (bObjectPin && !ValueJson.IsEmpty())
        {
            UObject* NewObj = MCPBpG_ResolveClass(ValueJson);              // class pins: resolve a UClass
            if (!NewObj)
            {
                NewObj = LoadObject<UObject>(nullptr, *ValueJson);         // object pins: resolve an asset
            }
            Schema->TrySetDefaultObject(*Pin, NewObj);                     // VERIFY vs engine source (void; validates internally)
        }
        else
        {
            Schema->TrySetDefaultValue(*Pin, ValueJson);                   // VERIFY vs engine source (void; validates internally)
        }
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during TrySetDefault*"));
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("pin"), Pin->PinName.ToString());
    Root->SetBoolField(TEXT("is_object_pin"), bObjectPin);
    Root->SetStringField(TEXT("prev_value"), PrevValue);
    Root->SetStringField(TEXT("prev_object"), PrevObject);
    Root->SetStringField(TEXT("prev_text"), PrevText);
    // Re-read the ACTUAL post-set default (the schema may have coerced/ignored it).
    Root->SetStringField(TEXT("new_value"), Pin->DefaultValue);
    Root->SetStringField(TEXT("new_object"), Pin->DefaultObject ? Pin->DefaultObject->GetPathName() : FString());
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 7 — BreakBlueprintNodeLinkJson. Break one specific link (OtherGuid+OtherPin) or ALL links on the
// pin. Captures broken endpoints for the reconnect inverse.
// =====================================================================================================
FString UMCPReflectionLibrary::BreakBlueprintNodeLinkJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, const FString& PinName, const FString& OtherGuid, const FString& OtherPin)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpG_Err(Err); }
    UEdGraphNode* Node = MCPBpG_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPBpG_Err(Err); }
    UEdGraphPin* Pin = MCPBpG_FindPin(Node, PinName);
    if (!Pin) { return MCPBpG_Err(FString::Printf(TEXT("node has no pin '%s'"), *PinName)); }

    // Capture the endpoints we are about to break (for the reconnect inverse). Snapshot BEFORE mutating.
    TArray<TSharedPtr<FJsonValue>> Broken;
    auto RecordLink = [&Broken](UEdGraphPin* Other)
    {
        if (!Other || !Other->GetOwningNodeUnchecked()) { return; }
        TSharedRef<FJsonObject> LO = MakeShared<FJsonObject>();
        LO->SetStringField(TEXT("other_guid"), Other->GetOwningNode()->NodeGuid.ToString());
        LO->SetStringField(TEXT("other_pin"), Other->PinName.ToString());
        Broken.Add(MakeShared<FJsonValueObject>(LO));
    };

    const bool bTargeted = !OtherGuid.IsEmpty();
    int32 BrokenCount = 0;
    if (bTargeted)
    {
        UEdGraphNode* OtherNode = MCPBpG_FindNode(Graph, OtherGuid, Err);
        if (!OtherNode) { return MCPBpG_Err(Err); }
        UEdGraphPin* Other = MCPBpG_FindPin(OtherNode, OtherPin);
        if (!Other) { return MCPBpG_Err(FString::Printf(TEXT("other-node has no pin '%s'"), *OtherPin)); }
        if (!Pin->LinkedTo.Contains(Other))
        {
            return MCPBpG_Err(TEXT("the two pins are not linked"));
        }
        RecordLink(Other);
        Pin->BreakLinkTo(Other);                                          // VERIFY vs engine source (handles both sides)
        BrokenCount = 1;
    }
    else
    {
        // Snapshot all current links, then break them all.
        for (UEdGraphPin* L : Pin->LinkedTo)
        {
            RecordLink(L);
        }
        BrokenCount = Pin->LinkedTo.Num();
        Pin->BreakAllPinLinks();                                          // VERIFY vs engine source
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("pin"), Pin->PinName.ToString());
    Root->SetNumberField(TEXT("broken_count"), BrokenCount);
    Root->SetArrayField(TEXT("broken"), Broken);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 8 — DeleteBlueprintNodeJson (generalized RemoveEventNodeByGuid: remove ANY node by GUID).
// Captures a LOSSY re-add spec (class + kind + pos + pin defaults + links) for the inverse. See the
// LOSSINESS note in the header/report: only the fields below round-trip; internal node state does not.
// =====================================================================================================
FString UMCPReflectionLibrary::DeleteBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpG_Err(Err); }
    UEdGraphNode* Node = MCPBpG_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPBpG_Err(Err); }

    // --- capture a best-effort re-add spec BEFORE removal ---------------------------------------------
    TSharedRef<FJsonObject> Captured = MakeShared<FJsonObject>();
    Captured->SetStringField(TEXT("class"), Node->GetClass()->GetName());
    Captured->SetNumberField(TEXT("x"), Node->NodePosX);
    Captured->SetNumberField(TEXT("y"), Node->NodePosY);

    // Reverse-map the concrete class to a re-add "kind" + its identifying fields.
    if (UK2Node_CallFunction* CF = Cast<UK2Node_CallFunction>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("call_function"));
        Captured->SetStringField(TEXT("function"), CF->FunctionReference.GetMemberName().ToString());
        if (UClass* MC = CF->FunctionReference.GetMemberParentClass(CF->GetBlueprintClassFromNode()))  // VERIFY vs engine source
        {
            Captured->SetStringField(TEXT("class"), MC->GetPathName());
        }
    }
    else if (UK2Node_VariableSet* VS = Cast<UK2Node_VariableSet>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("variable_set"));
        Captured->SetStringField(TEXT("var_name"), VS->VariableReference.GetMemberName().ToString());
    }
    else if (UK2Node_VariableGet* VG = Cast<UK2Node_VariableGet>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("variable_get"));
        Captured->SetStringField(TEXT("var_name"), VG->VariableReference.GetMemberName().ToString());
    }
    else if (UK2Node_DynamicCast* DC = Cast<UK2Node_DynamicCast>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("cast"));
        Captured->SetStringField(TEXT("class"), DC->TargetType ? DC->TargetType->GetPathName() : FString());
    }
    else if (Cast<UK2Node_IfThenElse>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("branch"));
    }
    else if (Cast<UK2Node_Knot>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("knot"));
    }
    else if (UK2Node_CustomEvent* CE = Cast<UK2Node_CustomEvent>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("custom_event"));
        Captured->SetStringField(TEXT("event_name"), CE->CustomFunctionName.ToString());
    }
    else if (UK2Node_Event* EV = Cast<UK2Node_Event>(Node))
    {
        Captured->SetStringField(TEXT("kind"), TEXT("event"));
        Captured->SetStringField(TEXT("event_name"), EV->EventReference.GetMemberName().ToString());
    }
    else
    {
        Captured->SetStringField(TEXT("kind"), TEXT("unsupported"));       // re-add inverse cannot reconstruct this class
    }

    // Capture pin defaults + links (for a best-effort default/relink pass in the inverse).
    TArray<TSharedPtr<FJsonValue>> PinCaps;
    for (UEdGraphPin* P : Node->Pins)
    {
        if (!P) { continue; }
        TSharedRef<FJsonObject> PC = MakeShared<FJsonObject>();
        PC->SetStringField(TEXT("name"), P->PinName.ToString());
        PC->SetStringField(TEXT("direction"), P->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
        PC->SetStringField(TEXT("default_value"), P->DefaultValue);
        PC->SetStringField(TEXT("default_object"), P->DefaultObject ? P->DefaultObject->GetPathName() : FString());
        TArray<TSharedPtr<FJsonValue>> Links;
        for (UEdGraphPin* L : P->LinkedTo)
        {
            if (!L || !L->GetOwningNodeUnchecked()) { continue; }
            TSharedRef<FJsonObject> LO = MakeShared<FJsonObject>();
            LO->SetStringField(TEXT("node_guid"), L->GetOwningNode()->NodeGuid.ToString());
            LO->SetStringField(TEXT("pin_name"), L->PinName.ToString());
            Links.Add(MakeShared<FJsonValueObject>(LO));
        }
        PC->SetArrayField(TEXT("linked_to"), Links);
        PinCaps.Add(MakeShared<FJsonValueObject>(PC));
    }
    Captured->SetArrayField(TEXT("pins"), PinCaps);

    // --- remove (RemoveNode breaks its links internally; bDontRecompile=true, we mark-modified below) ---
    const FString RemovedGuid = Node->NodeGuid.ToString();
    try
    {
        FBlueprintEditorUtils::RemoveNode(BP, Node, /*bDontRecompile*/true);  // VERIFY vs engine source (generalizes RemoveEventNodeByGuid)
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during RemoveNode"));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), RemovedGuid);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetObjectField(TEXT("captured"), Captured);                     // lossy re-add spec for the Python inverse
    Root->SetBoolField(TEXT("inverse_is_lossy"), true);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 9 — SetBlueprintNodePropertyJson (set an FProperty on the UK2Node; e.g. a literal node's value).
// Captures prior via ExportText; sets via ImportText; reconstructs (pins may depend on the property).
// Inverse: restore prior via the same handler.
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintNodePropertyJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& NodeGuid, const FString& PropertyName, const FString& ValueJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpG_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpG_Err(Err); }
    UEdGraphNode* Node = MCPBpG_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPBpG_Err(Err); }

    FProperty* Prop = Node->GetClass()->FindPropertyByName(FName(*PropertyName));  // VERIFY vs engine source
    if (!Prop)
    {
        return MCPBpG_Err(FString::Printf(TEXT("node class '%s' has no property '%s'"),
                          *Node->GetClass()->GetName(), *PropertyName));
    }

    // Capture prior value as text.
    FString PrevText;
    Prop->ExportText_InContainer(0, PrevText, Node, /*Delta*/nullptr, /*Parent*/Node, PPF_None);  // VERIFY vs engine source

    // Apply new value; ImportText_InContainer returns nullptr on parse failure (no crash).
    const TCHAR* ImportResult = nullptr;
    try
    {
        ImportResult = Prop->ImportText_InContainer(*ValueJson, Node, /*OwnerObject*/Node, PPF_None);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during ImportText"));
    }
    if (ImportResult == nullptr)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not parse value '%s' for property '%s'"),
                          *ValueJson, *PropertyName));
    }

    // The property may drive pin shape; reconstruct so pins stay consistent (guarded).
    try { Node->ReconstructNode(); } catch (...) { /* value is set; pins best-effort */ }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    // Re-read the actual value post-set.
    FString NewText;
    Prop->ExportText_InContainer(0, NewText, Node, nullptr, Node, PPF_None);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("prev_value"), PrevText);
    Root->SetStringField(TEXT("new_value"), NewText);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// COMPILE 10 — CompileBlueprintByPath (deferred finalize; call ONCE after a batch of writes).
// =====================================================================================================
FString UMCPReflectionLibrary::CompileBlueprintByPath(const FString& BlueprintPath)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpG_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpG_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    try
    {
        FKismetEditorUtilities::CompileBlueprint(BP);                     // VERIFY vs engine source (default flags)
    }
    catch (...)
    {
        return MCPBpG_Err(TEXT("exception during CompileBlueprint"));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("blueprint_path"), BP->GetPathName());
    Root->SetStringField(TEXT("compile_status"), MCPBpG_StatusToString(BP->Status));  // VERIFY vs engine source (UBlueprint::Status)
    Root->SetBoolField(TEXT("up_to_date"), BP->Status == BS_UpToDate || BP->Status == BS_UpToDateWithWarnings);
    Root->SetBoolField(TEXT("has_errors"), BP->Status == BS_Error);
    return MCPBpG_Serialize(Root);
#else
    return MCPBpG_Err(TEXT("editor-only"));
#endif
}
