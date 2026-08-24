// UnrealMCP — BLUEPRINT graph-BUILDERS + typed-asset CREATORS + TYPE-REGISTRY + variable-FLAG setter
// (C++ DRAFT 2026-08-19). Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION
// declarations are added to MCPReflectionLibrary.h by the coordinator (do NOT edit the .h here).
//
// These COMPOSE the shipped K2 node primitives from MCPReflection_BlueprintGraph.cpp (AddBlueprintNodeJson /
// ConnectBlueprintNodesJson / SetBlueprintPinDefaultJson / GetBlueprintGraphJson + the anon MCPBpG_* helpers)
// into one-transaction higher-level tools. Anon-namespace helpers here are prefixed `MCPBpM_` so they stay
// unique in the module's unity build (the MCPBpG_* helpers live in another TU's anonymous namespace and are
// NOT visible here — the node-construction / graph-resolve / pin-lookup logic is RE-IMPLEMENTED verbatim
// under the MCPBpM_ prefix). Six handlers:
//
//   1) BuildBlueprintGraphJson(BlueprintPath, GraphName, SpecJson, Mode) — batch build a graph from a spec
//      {nodes:[{id,kind,...,x,y}], connections:[{from_id,from_pin,to_id,to_pin}], pin_defaults:[{node_id,pin,value}]}.
//      Mode build|merge|sync. Returns created ids + rejected connections + a BEFORE-snapshot build-spec for undo.
//   2) ArrangeBlueprintGraphJson(BlueprintPath, GraphName, OptionsJson) — columnar auto-layout by depth-from-
//      source (mirrors LayoutNiagaraGraph). OptionsJson.restore_positions doubles as the undo path.
//   3) CreateBlueprintInterfaceJson(Name, Path) — BPTYPE_Interface UBlueprint (UInterface parent). Inverse: delete.
//   4) CreateTypedBlueprintJson(Name, Path, ParentClass, BlueprintType) — general typed-BP creator. Inverse: delete.
//   5) GetTypeRegistryJson(Kind, Query, IncludeEngine, Max) — TObjectRange walk of UClass/UScriptStruct/UEnum. READ-ONLY.
//   6) SetBlueprintVariableFlagsJson(BlueprintPath, VariableName, FlagsJson) — toggle FBPVariableDescription
//      PropertyFlags bits + private/expose_on_spawn metadata. Captures prior flags. Inverse: restore prior.
//
// COMPILE STRATEGY (matches the node primitives): writes end with MarkBlueprintAsStructurallyModified ONLY, no
// per-op FKismetEditorUtilities::CompileBlueprint. The Python caller batches then calls compile_blueprint_graph
// (or CompileBlueprintByPath) exactly once. Asset CREATORS (3,4) do NOT compile/save — the Python caller saves.
//
// CRASH-SAFETY: BuildBlueprintGraphJson is the risky one (many node ops). Every node lookup is a guarded
// FGuid::Parse + linear scan; every pin lookup is FindPin + null-check (NEVER FindPinChecked on spec input);
// class/function resolution is validated before NewObject; TryCreateConnection is gated on two non-null pins;
// per-node failures are COLLECTED (node_errors) and the build continues (partial build + the before-snapshot
// undo restores). GetTypeRegistryJson caps at Max (default 500). All handlers are #if WITH_EDITOR gated and
// return {"error":...} (never crash) on any miss.
//
// Module deps: BlueprintGraph (UK2Node_* / EdGraphSchema_K2), UnrealEd (FBlueprintEditorUtils /
// FKismetEditorUtilities), Engine (UBlueprint / FBPVariableDescription), AssetRegistry (AssetCreated),
// CoreUObject (TObjectRange / UEnum / UScriptStruct). ALL already present in UnrealMCP.Build.cs -> NO Build.cs
// change, NO export patch. Every engine-API touch point is tagged "VERIFY vs engine source".

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"              // UClass / UScriptStruct / UStruct / UEnum
#include "UObject/UnrealType.h"         // FProperty / EPropertyFlags (CPF_*)
#include "UObject/ObjectMacros.h"       // CPF_* flag constants
#include "UObject/UObjectGlobals.h"     // LoadObject / FindObject / NewObject / CreatePackage
#include "UObject/UObjectIterator.h"    // TObjectRange<T>
#include "UObject/Object.h"
#include "UObject/Interface.h"          // UInterface (BPTYPE_Interface parent)
#include "UObject/Package.h"            // UPackage
#include "Misc/PackageName.h"           // FPackageName::GetShortName
#include "Misc/App.h"                   // FApp::GetProjectName (engine/non-engine type split)

#include "Engine/Blueprint.h"                 // UBlueprint / FBPVariableDescription / EBlueprintType / BPTYPE_*
#include "Engine/BlueprintGeneratedClass.h"   // UBlueprintGeneratedClass
#include "GameFramework/Actor.h"              // AActor (default typed-BP parent + default event owner)
#include "EdGraph/EdGraph.h"                  // UEdGraph::Nodes / AddNode
#include "EdGraph/EdGraphNode.h"              // UEdGraphNode (NodeGuid / NodePosX / CanUserDeleteNode / ReconstructNode)
#include "EdGraph/EdGraphPin.h"               // UEdGraphPin / FEdGraphPinType

#if WITH_EDITOR
#include "EdGraphSchema_K2.h"                 // UEdGraphSchema_K2 / FBlueprintMetadata (MD_Private / MD_ExposeOnSpawn)
#include "K2Node.h"                           // UK2Node
#include "K2Node_CallFunction.h"             // UK2Node_CallFunction
#include "K2Node_VariableGet.h"              // UK2Node_VariableGet
#include "K2Node_VariableSet.h"              // UK2Node_VariableSet
#include "K2Node_Variable.h"                 // UK2Node_Variable (VariableReference base)
#include "K2Node_IfThenElse.h"              // UK2Node_IfThenElse (branch)
#include "K2Node_DynamicCast.h"             // UK2Node_DynamicCast (TargetType)
#include "K2Node_Knot.h"                    // UK2Node_Knot (reroute)
#include "K2Node_Event.h"                   // UK2Node_Event
#include "K2Node_CustomEvent.h"             // UK2Node_CustomEvent
#include "Engine/MemberReference.h"          // FMemberReference::SetExternalMember / SetSelfMember
#include "Kismet2/BlueprintEditorUtils.h"    // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified / RemoveNode
#include "Kismet2/KismetEditorUtilities.h"   // FKismetEditorUtilities::CreateBlueprint
#include "AssetRegistry/AssetRegistryModule.h" // FAssetRegistryModule::AssetCreated
#endif // WITH_EDITOR

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPBpM_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error").
    FString MCPBpM_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPBpM_Serialize(Root);
    }

#if WITH_EDITOR
    TSharedPtr<FJsonObject> MCPBpM_ParseObject(const FString& JsonText)
    {
        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
        if (FJsonSerializer::Deserialize(Reader, Obj) && Obj.IsValid())
        {
            return Obj;
        }
        return nullptr;
    }

    // Load a UBlueprint from an asset path ("/Game/Path/BP_Foo.BP_Foo" or "/Game/Path/BP_Foo").
    UBlueprint* MCPBpM_LoadBlueprint(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        UBlueprint* BP = LoadObject<UBlueprint>(nullptr, *Path);
        if (!BP)
        {
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            BP = LoadObject<UBlueprint>(nullptr, *Full);
        }
        return BP;
    }

    // Resolve a UClass from a native path / short name / BP asset path (-> GeneratedClass). Mirrors MCPBpG_ResolveClass.
    UClass* MCPBpM_ResolveClass(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        UClass* C = LoadObject<UClass>(nullptr, *Path);
        if (!C) { C = FindObject<UClass>(nullptr, *Path); }
        if (!C) { C = UClass::TryFindTypeSlow<UClass>(Path); }                  // VERIFY vs engine source
        if (!C)
        {
            const FString EnginePath = FString::Printf(TEXT("/Script/Engine.%s"), *Path);
            C = LoadObject<UClass>(nullptr, *EnginePath);
        }
        if (!C)
        {
            if (UObject* Obj = LoadObject<UObject>(nullptr, *Path))
            {
                if (UBlueprint* BP = Cast<UBlueprint>(Obj)) { C = BP->GeneratedClass; }
            }
        }
        return C;
    }

    // Resolve the target UEdGraph. GraphName ""/"EventGraph" -> the ubergraph; else a named function/uber page.
    UEdGraph* MCPBpM_ResolveGraph(UBlueprint* BP, const FString& GraphName, FString& OutErr)
    {
        if (!BP) { OutErr = TEXT("null blueprint"); return nullptr; }
        const bool bWantUber = GraphName.IsEmpty() || GraphName.Equals(TEXT("EventGraph"), ESearchCase::IgnoreCase);
        if (bWantUber)
        {
            for (UEdGraph* G : BP->UbergraphPages)                              // VERIFY vs engine source
            {
                if (!G) { continue; }
                if (GraphName.IsEmpty() || G->GetName().Equals(GraphName, ESearchCase::IgnoreCase)) { return G; }
            }
            if (BP->UbergraphPages.Num() > 0 && BP->UbergraphPages[0]) { return BP->UbergraphPages[0]; }
            OutErr = TEXT("blueprint has no ubergraph (event graph)");
            return nullptr;
        }
        for (UEdGraph* G : BP->FunctionGraphs)                                  // VERIFY vs engine source
        {
            if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase)) { return G; }
        }
        for (UEdGraph* G : BP->UbergraphPages)
        {
            if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase)) { return G; }
        }
        OutErr = FString::Printf(TEXT("no graph named '%s' in blueprint '%s'"), *GraphName, *BP->GetName());
        return nullptr;
    }

    // Find a node by GUID within one graph (guarded FGuid::Parse; never crashes on bad input).
    UEdGraphNode* MCPBpM_FindNodeByGuid(UEdGraph* Graph, const FString& GuidStr)
    {
        if (!Graph) { return nullptr; }
        FGuid Target;
        if (!FGuid::Parse(GuidStr, Target)) { return nullptr; }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (N && N->NodeGuid == Target) { return N; }                       // VERIFY vs engine source
        }
        return nullptr;
    }

    // Find a pin by name (exact FName first, case-insensitive fallback). NEVER FindPinChecked.
    UEdGraphPin* MCPBpM_FindPin(UEdGraphNode* Node, const FString& PinName)
    {
        if (!Node) { return nullptr; }
        if (UEdGraphPin* P = Node->FindPin(FName(*PinName))) { return P; }      // VERIFY vs engine source
        for (UEdGraphPin* Pin : Node->Pins)
        {
            if (Pin && Pin->PinName.ToString().Equals(PinName, ESearchCase::IgnoreCase)) { return Pin; }
        }
        return nullptr;
    }

    const UEdGraphSchema_K2* MCPBpM_K2Schema() { return GetDefault<UEdGraphSchema_K2>(); }

    // Construct one K2 node from a spec object (kind + identifying fields). RE-IMPLEMENTS MCPBpG_ConstructNode.
    // On success returns the not-yet-added node with PINS NOT YET allocated (caller allocates/positions/adds).
    UK2Node* MCPBpM_ConstructNode(UEdGraph* Graph, UBlueprint* BP, const TSharedPtr<FJsonObject>& Spec,
                                  bool& bOutPinsDependOnRef, FString& OutErr)
    {
        bOutPinsDependOnRef = false;

        FString Kind;
        if (!Spec->TryGetStringField(TEXT("kind"), Kind)) { Spec->TryGetStringField(TEXT("type"), Kind); }
        Kind = Kind.ToLower();
        if (Kind.IsEmpty()) { OutErr = TEXT("node spec missing 'kind'"); return nullptr; }

        if (Kind == TEXT("call_function") || Kind == TEXT("callfunction") || Kind == TEXT("function"))
        {
            FString ClassPath, FuncName;
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            Spec->TryGetStringField(TEXT("function"), FuncName);
            if (FuncName.IsEmpty()) { OutErr = TEXT("call_function spec requires 'function'"); return nullptr; }
            UClass* OwnerClass = MCPBpM_ResolveClass(ClassPath);
            if (!OwnerClass) { OutErr = FString::Printf(TEXT("call_function: could not resolve class '%s'"), *ClassPath); return nullptr; }
            if (!OwnerClass->FindFunctionByName(FName(*FuncName)))              // VERIFY vs engine source
            {
                OutErr = FString::Printf(TEXT("call_function: class '%s' has no function '%s'"), *OwnerClass->GetName(), *FuncName);
                return nullptr;
            }
            UK2Node_CallFunction* Node = NewObject<UK2Node_CallFunction>(Graph);
            Node->FunctionReference.SetExternalMember(FName(*FuncName), OwnerClass);  // VERIFY vs engine source
            return Node;
        }
        if (Kind == TEXT("variable_get") || Kind == TEXT("variableget") || Kind == TEXT("get"))
        {
            FString VarName;
            Spec->TryGetStringField(TEXT("var_name"), VarName);
            if (VarName.IsEmpty()) { Spec->TryGetStringField(TEXT("variable"), VarName); }
            if (VarName.IsEmpty()) { OutErr = TEXT("variable_get spec requires 'var_name'"); return nullptr; }
            UK2Node_VariableGet* Node = NewObject<UK2Node_VariableGet>(Graph);
            Node->VariableReference.SetSelfMember(FName(*VarName));             // VERIFY vs engine source
            bOutPinsDependOnRef = true;
            return Node;
        }
        if (Kind == TEXT("variable_set") || Kind == TEXT("variableset") || Kind == TEXT("set"))
        {
            FString VarName;
            Spec->TryGetStringField(TEXT("var_name"), VarName);
            if (VarName.IsEmpty()) { Spec->TryGetStringField(TEXT("variable"), VarName); }
            if (VarName.IsEmpty()) { OutErr = TEXT("variable_set spec requires 'var_name'"); return nullptr; }
            UK2Node_VariableSet* Node = NewObject<UK2Node_VariableSet>(Graph);
            Node->VariableReference.SetSelfMember(FName(*VarName));
            bOutPinsDependOnRef = true;
            return Node;
        }
        if (Kind == TEXT("branch") || Kind == TEXT("if") || Kind == TEXT("ifthenelse"))
        {
            return NewObject<UK2Node_IfThenElse>(Graph);
        }
        if (Kind == TEXT("cast") || Kind == TEXT("dynamic_cast") || Kind == TEXT("dynamiccast"))
        {
            FString ClassPath;
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            if (ClassPath.IsEmpty()) { Spec->TryGetStringField(TEXT("target_class"), ClassPath); }
            UClass* TargetClass = MCPBpM_ResolveClass(ClassPath);
            if (!TargetClass) { OutErr = FString::Printf(TEXT("cast: could not resolve target class '%s'"), *ClassPath); return nullptr; }
            UK2Node_DynamicCast* Node = NewObject<UK2Node_DynamicCast>(Graph);
            Node->TargetType = TargetClass;                                    // VERIFY vs engine source — MUST precede AllocateDefaultPins
            bOutPinsDependOnRef = true;
            return Node;
        }
        if (Kind == TEXT("knot") || Kind == TEXT("reroute"))
        {
            return NewObject<UK2Node_Knot>(Graph);
        }
        if (Kind == TEXT("event"))
        {
            FString EventName, ClassPath;
            Spec->TryGetStringField(TEXT("event_name"), EventName);
            if (EventName.IsEmpty()) { Spec->TryGetStringField(TEXT("function"), EventName); }
            if (EventName.IsEmpty()) { OutErr = TEXT("event spec requires 'event_name' (e.g. 'ReceiveBeginPlay')"); return nullptr; }
            Spec->TryGetStringField(TEXT("class"), ClassPath);
            UClass* OwnerClass = ClassPath.IsEmpty()
                ? (BP && BP->ParentClass ? (UClass*)BP->ParentClass : AActor::StaticClass())
                : MCPBpM_ResolveClass(ClassPath);
            if (!OwnerClass) { OutErr = FString::Printf(TEXT("event: could not resolve owner class '%s'"), *ClassPath); return nullptr; }
            UK2Node_Event* Node = NewObject<UK2Node_Event>(Graph);
            Node->EventReference.SetExternalMember(FName(*EventName), OwnerClass);  // VERIFY vs engine source
            Node->bOverrideFunction = true;
            return Node;
        }
        if (Kind == TEXT("custom_event") || Kind == TEXT("customevent"))
        {
            FString EventName;
            Spec->TryGetStringField(TEXT("event_name"), EventName);
            if (EventName.IsEmpty()) { Spec->TryGetStringField(TEXT("name"), EventName); }
            if (EventName.IsEmpty()) { OutErr = TEXT("custom_event spec requires 'event_name'"); return nullptr; }
            UK2Node_CustomEvent* Node = NewObject<UK2Node_CustomEvent>(Graph);
            Node->CustomFunctionName = FName(*EventName);                      // VERIFY vs engine source
            return Node;
        }
        if (Kind == TEXT("unsupported"))
        {
            OutErr = TEXT("node kind 'unsupported' cannot be reconstructed (lossy snapshot node)");
            return nullptr;
        }

        OutErr = FString::Printf(TEXT("unknown node kind '%s' (expected call_function|variable_get|variable_set|"
            "branch|cast|knot|event|custom_event)"), *Kind);
        return nullptr;
    }

    // Instantiate + add ONE spec node to the graph. Returns the added node (with a fresh guid) or null (OutErr set).
    UK2Node* MCPBpM_InstantiateNode(UEdGraph* Graph, UBlueprint* BP, const TSharedPtr<FJsonObject>& Spec, FString& OutErr)
    {
        bool bPinsDependOnRef = false;
        UK2Node* NewNode = nullptr;
        try { NewNode = MCPBpM_ConstructNode(Graph, BP, Spec, bPinsDependOnRef, OutErr); }
        catch (...) { OutErr = TEXT("exception during node construction"); return nullptr; }
        if (!NewNode) { return nullptr; }

        try { NewNode->AllocateDefaultPins(); }                               // VERIFY vs engine source
        catch (...) { OutErr = TEXT("exception during AllocateDefaultPins"); return nullptr; }

        double PosX = 0.0, PosY = 0.0;
        Spec->TryGetNumberField(TEXT("x"), PosX);
        Spec->TryGetNumberField(TEXT("y"), PosY);
        NewNode->NodePosX = (int32)PosX;
        NewNode->NodePosY = (int32)PosY;

        Graph->AddNode(NewNode, /*bUserAction*/false, /*bSelectNewNode*/false);  // VERIFY vs engine source
        NewNode->CreateNewGuid();                                             // fresh persistent guid AFTER add

        if (bPinsDependOnRef)
        {
            try { NewNode->ReconstructNode(); } catch (...) { /* keep node; pins stay as allocated */ }
        }
        return NewNode;
    }

    // Reverse-map a live node's concrete class to a build-spec node object {id, kind, ...fields, x, y}. Mirrors the
    // DeleteBlueprintNodeJson capture. Nodes that do not reverse-map get kind "unsupported" (skipped on rebuild).
    TSharedRef<FJsonObject> MCPBpM_NodeToSpec(UEdGraphNode* Node)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("id"), Node->NodeGuid.ToString());
        J->SetNumberField(TEXT("x"), Node->NodePosX);
        J->SetNumberField(TEXT("y"), Node->NodePosY);

        if (UK2Node_CallFunction* CF = Cast<UK2Node_CallFunction>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("call_function"));
            J->SetStringField(TEXT("function"), CF->FunctionReference.GetMemberName().ToString());
            if (UClass* MC = CF->FunctionReference.GetMemberParentClass(CF->GetBlueprintClassFromNode()))  // VERIFY vs engine source
            {
                J->SetStringField(TEXT("class"), MC->GetPathName());
            }
        }
        else if (UK2Node_VariableSet* VS = Cast<UK2Node_VariableSet>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("variable_set"));
            J->SetStringField(TEXT("var_name"), VS->VariableReference.GetMemberName().ToString());
        }
        else if (UK2Node_VariableGet* VG = Cast<UK2Node_VariableGet>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("variable_get"));
            J->SetStringField(TEXT("var_name"), VG->VariableReference.GetMemberName().ToString());
        }
        else if (UK2Node_DynamicCast* DC = Cast<UK2Node_DynamicCast>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("cast"));
            J->SetStringField(TEXT("class"), DC->TargetType ? DC->TargetType->GetPathName() : FString());
        }
        else if (Cast<UK2Node_IfThenElse>(Node)) { J->SetStringField(TEXT("kind"), TEXT("branch")); }
        else if (Cast<UK2Node_Knot>(Node))       { J->SetStringField(TEXT("kind"), TEXT("knot")); }
        else if (UK2Node_CustomEvent* CE = Cast<UK2Node_CustomEvent>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("custom_event"));
            J->SetStringField(TEXT("event_name"), CE->CustomFunctionName.ToString());
        }
        else if (UK2Node_Event* EV = Cast<UK2Node_Event>(Node))
        {
            J->SetStringField(TEXT("kind"), TEXT("event"));
            J->SetStringField(TEXT("event_name"), EV->EventReference.GetMemberName().ToString());
        }
        else { J->SetStringField(TEXT("kind"), TEXT("unsupported")); }
        return J;
    }

    // Serialize the CURRENT graph as a build-spec {nodes, connections, pin_defaults} that BuildBlueprintGraphJson
    // (mode "build") can re-import — the document-pattern undo snapshot. Only USER-DELETABLE nodes are captured as
    // spec nodes; non-deletable scaffolding (function entry/result/tunnel) stays in the graph and is referenced by
    // its guid in connections. Reconstruction is LOSSY: "unsupported" node kinds are skipped and links to the kept
    // scaffolding round-trip only when build-mode leaves that scaffolding in place.
    TSharedRef<FJsonObject> MCPBpM_GraphToSpec(UEdGraph* Graph)
    {
        TSharedRef<FJsonObject> Spec = MakeShared<FJsonObject>();

        TArray<TSharedPtr<FJsonValue>> Nodes;
        TSet<FGuid> CapturedGuids;   // nodes emitted as spec nodes
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (!N || !N->CanUserDeleteNode()) { continue; }                  // VERIFY vs engine source
            CapturedGuids.Add(N->NodeGuid);
            Nodes.Add(MakeShared<FJsonValueObject>(MCPBpM_NodeToSpec(N)));
        }
        Spec->SetArrayField(TEXT("nodes"), Nodes);

        // Connections: emit once per link from the OUTPUT side. Reference endpoints by their (stable) node guid so
        // build-mode's spec-id->new-guid map (which keys on the same guid string) reconnects them; links whose OTHER
        // end is a kept-scaffolding node also survive because the connection resolver falls back to a direct guid
        // lookup in the graph.
        TArray<TSharedPtr<FJsonValue>> Connections;
        TArray<TSharedPtr<FJsonValue>> PinDefaults;
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (!N) { continue; }
            const bool bCaptured = CapturedGuids.Contains(N->NodeGuid);
            for (UEdGraphPin* P : N->Pins)
            {
                if (!P) { continue; }
                if (P->Direction == EGPD_Output)
                {
                    for (UEdGraphPin* L : P->LinkedTo)
                    {
                        if (!L || !L->GetOwningNodeUnchecked()) { continue; }
                        UEdGraphNode* Other = L->GetOwningNode();
                        // Only emit if at least one endpoint is a captured (rebuildable) node — a link between two
                        // kept-scaffolding nodes is intrinsic to the graph and needs no restore.
                        if (!bCaptured && !CapturedGuids.Contains(Other->NodeGuid)) { continue; }
                        TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
                        C->SetStringField(TEXT("from_id"), N->NodeGuid.ToString());
                        C->SetStringField(TEXT("from_pin"), P->PinName.ToString());
                        C->SetStringField(TEXT("to_id"), Other->NodeGuid.ToString());
                        C->SetStringField(TEXT("to_pin"), L->PinName.ToString());
                        Connections.Add(MakeShared<FJsonValueObject>(C));
                    }
                }
                else if (bCaptured && P->Direction == EGPD_Input && P->LinkedTo.Num() == 0)
                {
                    // Capture literal input defaults on captured nodes only (linked inputs need no default).
                    const bool bHasDefault = !P->DefaultValue.IsEmpty() || P->DefaultObject != nullptr;
                    if (bHasDefault)
                    {
                        TSharedRef<FJsonObject> D = MakeShared<FJsonObject>();
                        D->SetStringField(TEXT("node_id"), N->NodeGuid.ToString());
                        D->SetStringField(TEXT("pin"), P->PinName.ToString());
                        D->SetStringField(TEXT("value"), P->DefaultObject ? P->DefaultObject->GetPathName() : P->DefaultValue);
                        PinDefaults.Add(MakeShared<FJsonValueObject>(D));
                    }
                }
            }
        }
        Spec->SetArrayField(TEXT("connections"), Connections);
        Spec->SetArrayField(TEXT("pin_defaults"), PinDefaults);
        return Spec;
    }

    // Apply a pin default (value or object) using the K2 schema, deciding by pin category. Best-effort; guarded.
    void MCPBpM_ApplyPinDefault(const UEdGraphSchema_K2* Schema, UEdGraphPin* Pin, const FString& Value)
    {
        if (!Schema || !Pin) { return; }
        const FName Cat = Pin->PinType.PinCategory;
        const bool bObjectPin =
            Cat == UEdGraphSchema_K2::PC_Object || Cat == UEdGraphSchema_K2::PC_Class ||
            Cat == UEdGraphSchema_K2::PC_Interface || Cat == UEdGraphSchema_K2::PC_SoftObject ||
            Cat == UEdGraphSchema_K2::PC_SoftClass;
        try
        {
            if (bObjectPin && !Value.IsEmpty())
            {
                UObject* NewObj = MCPBpM_ResolveClass(Value);                 // class pins: resolve a UClass
                if (!NewObj) { NewObj = LoadObject<UObject>(nullptr, *Value); } // object pins: resolve an asset
                Schema->TrySetDefaultObject(*Pin, NewObj);                    // VERIFY vs engine source (void)
            }
            else
            {
                Schema->TrySetDefaultValue(*Pin, Value);                      // VERIFY vs engine source (void)
            }
        }
        catch (...) { /* leave the pin unchanged on any coercion failure */ }
    }

    // Map a BlueprintType token -> EBlueprintType. Returns false on unknown.
    bool MCPBpM_ParseBlueprintType(const FString& In, EBlueprintType& Out)
    {
        const FString T = In.ToLower();
        if (T.IsEmpty() || T == TEXT("normal"))                              { Out = BPTYPE_Normal;          return true; }
        if (T == TEXT("const"))                                             { Out = BPTYPE_Const;           return true; }
        if (T == TEXT("function_library") || T == TEXT("functionlibrary"))   { Out = BPTYPE_FunctionLibrary; return true; }
        if (T == TEXT("macro_library") || T == TEXT("macrolibrary"))         { Out = BPTYPE_MacroLibrary;    return true; }
        if (T == TEXT("interface"))                                         { Out = BPTYPE_Interface;       return true; }
        return false;
    }

    // Read an OPTIONAL bool field; returns true if present (and writes Out).
    bool MCPBpM_TryGetBool(const TSharedPtr<FJsonObject>& Obj, const TCHAR* Key, bool& Out)
    {
        return Obj.IsValid() && Obj->TryGetBoolField(Key, Out);
    }

    // The "engine type" test for GetTypeRegistryJson: a native (/Script/) type whose module is NOT the game
    // project module. Asset types (/Game/, content plugins) and the project's own C++ types are NOT engine types.
    bool MCPBpM_IsEngineType(const UObject* Obj)
    {
        if (!Obj) { return false; }
        const FString Pkg = Obj->GetOutermost() ? Obj->GetOutermost()->GetName() : FString();
        if (!Pkg.StartsWith(TEXT("/Script/"))) { return false; }             // asset-defined -> not "engine"
        const FString Module = Pkg.RightChop(8);                             // strip "/Script/"
        const FString Project = FApp::GetProjectName();
        return !(!Project.IsEmpty() && Module.Equals(Project, ESearchCase::IgnoreCase));
    }
#endif // WITH_EDITOR
} // namespace

// =====================================================================================================
// 1) BuildBlueprintGraphJson — batch build a graph from a spec. Modes build|merge|sync.
//    Undo: returns "before_snapshot" (a build-spec of the PRIOR graph) for the Python restore op.
// =====================================================================================================
FString UMCPReflectionLibrary::BuildBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& SpecJson, const FString& Mode)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpM_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPBpM_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }
    FString Err;
    UEdGraph* Graph = MCPBpM_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpM_Err(Err); }

    TSharedPtr<FJsonObject> Spec = MCPBpM_ParseObject(SpecJson);
    if (!Spec.IsValid()) { return MCPBpM_Err(TEXT("spec_json is not a JSON object")); }

    FString ModeL = Mode.ToLower();
    if (ModeL.IsEmpty()) { ModeL = TEXT("build"); }
    if (ModeL != TEXT("build") && ModeL != TEXT("merge") && ModeL != TEXT("sync"))
    {
        return MCPBpM_Err(FString::Printf(TEXT("unknown mode '%s' (expected build|merge|sync)"), *Mode));
    }

    // ---- Undo snapshot: capture the PRIOR graph as a build-spec BEFORE any mutation. ----
    TSharedRef<FJsonObject> BeforeSnapshot = MCPBpM_GraphToSpec(Graph);

    const UEdGraphSchema_K2* Schema = MCPBpM_K2Schema();
    if (!Schema) { return MCPBpM_Err(TEXT("K2 schema unavailable")); }

    // ---- Gather spec node ids (for sync mode's keep-set). ----
    const TArray<TSharedPtr<FJsonValue>>* SpecNodes = nullptr;
    Spec->TryGetArrayField(TEXT("nodes"), SpecNodes);
    TSet<FString> SpecIdSet;
    if (SpecNodes)
    {
        for (const TSharedPtr<FJsonValue>& V : *SpecNodes)
        {
            const TSharedPtr<FJsonObject>* NObj = nullptr;
            if (V.IsValid() && V->TryGetObject(NObj) && NObj)
            {
                FString Id;
                if ((*NObj)->TryGetStringField(TEXT("id"), Id)) { SpecIdSet.Add(Id); }
            }
        }
    }

    // ---- Mode-specific pre-clear. "build" wipes every user-deletable node; "sync" removes user-deletable nodes
    //      whose guid is NOT named by a spec id; "merge" removes nothing. ----
    int32 RemovedCount = 0;
    if (ModeL == TEXT("build") || ModeL == TEXT("sync"))
    {
        TArray<UEdGraphNode*> ToRemove;
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (!N || !N->CanUserDeleteNode()) { continue; }
            if (ModeL == TEXT("sync") && SpecIdSet.Contains(N->NodeGuid.ToString())) { continue; }  // keep matched
            ToRemove.Add(N);
        }
        for (UEdGraphNode* N : ToRemove)
        {
            try { FBlueprintEditorUtils::RemoveNode(BP, N, /*bDontRecompile*/true); ++RemovedCount; }  // VERIFY vs engine source
            catch (...) { /* skip this node; continue */ }
        }
    }

    // ---- Node creation: map spec id -> resulting NodeGuid string. In sync mode a spec id that matches a KEPT
    //      node reuses it (position update only); otherwise a fresh node is instantiated. ----
    TMap<FString, FString> IdToGuid;   // spec id -> node guid string
    TArray<TSharedPtr<FJsonValue>> Created;
    TArray<TSharedPtr<FJsonValue>> NodeErrors;
    if (SpecNodes)
    {
        for (const TSharedPtr<FJsonValue>& V : *SpecNodes)
        {
            const TSharedPtr<FJsonObject>* NObjPtr = nullptr;
            if (!V.IsValid() || !V->TryGetObject(NObjPtr) || !NObjPtr) { continue; }
            TSharedPtr<FJsonObject> NObj = *NObjPtr;

            FString Id;
            NObj->TryGetStringField(TEXT("id"), Id);

            // sync reuse: spec id equals a still-present node guid.
            if (ModeL == TEXT("sync") && !Id.IsEmpty())
            {
                if (UEdGraphNode* Existing = MCPBpM_FindNodeByGuid(Graph, Id))
                {
                    double PosX = 0.0, PosY = 0.0;
                    if (NObj->TryGetNumberField(TEXT("x"), PosX)) { Existing->NodePosX = (int32)PosX; }
                    if (NObj->TryGetNumberField(TEXT("y"), PosY)) { Existing->NodePosY = (int32)PosY; }
                    IdToGuid.Add(Id, Existing->NodeGuid.ToString());
                    continue;
                }
            }

            FString NErr;
            UK2Node* NewNode = MCPBpM_InstantiateNode(Graph, BP, NObj, NErr);
            if (!NewNode)
            {
                TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
                E->SetStringField(TEXT("id"), Id);
                E->SetStringField(TEXT("error"), NErr);
                NodeErrors.Add(MakeShared<FJsonValueObject>(E));
                continue;
            }
            const FString NewGuid = NewNode->NodeGuid.ToString();
            if (!Id.IsEmpty()) { IdToGuid.Add(Id, NewGuid); }

            TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
            C->SetStringField(TEXT("id"), Id);
            C->SetStringField(TEXT("node_guid"), NewGuid);
            C->SetStringField(TEXT("class"), NewNode->GetClass()->GetName());
            Created.Add(MakeShared<FJsonValueObject>(C));
        }
    }

    // Resolve a spec endpoint id -> a live node: prefer the id->guid map (fresh/reused nodes), then fall back to a
    // direct guid lookup in the graph (so links to kept scaffolding nodes resolve during a restore).
    auto ResolveEndpoint = [&](const FString& EndId) -> UEdGraphNode*
    {
        if (const FString* MappedGuid = IdToGuid.Find(EndId))
        {
            if (UEdGraphNode* N = MCPBpM_FindNodeByGuid(Graph, *MappedGuid)) { return N; }
        }
        return MCPBpM_FindNodeByGuid(Graph, EndId);
    };

    // ---- Connections. TryCreateConnection SILENTLY rejects incompatible pins; collect the rejects w/ reasons. ----
    const TArray<TSharedPtr<FJsonValue>>* SpecConns = nullptr;
    Spec->TryGetArrayField(TEXT("connections"), SpecConns);
    int32 ConnectedCount = 0;
    TArray<TSharedPtr<FJsonValue>> Rejected;
    if (SpecConns)
    {
        for (const TSharedPtr<FJsonValue>& V : *SpecConns)
        {
            const TSharedPtr<FJsonObject>* CObjPtr = nullptr;
            if (!V.IsValid() || !V->TryGetObject(CObjPtr) || !CObjPtr) { continue; }
            TSharedPtr<FJsonObject> CObj = *CObjPtr;

            FString FromId, FromPin, ToId, ToPin;
            CObj->TryGetStringField(TEXT("from_id"), FromId);
            CObj->TryGetStringField(TEXT("from_pin"), FromPin);
            CObj->TryGetStringField(TEXT("to_id"), ToId);
            CObj->TryGetStringField(TEXT("to_pin"), ToPin);

            auto RejectWith = [&](const FString& Reason)
            {
                TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
                R->SetStringField(TEXT("from_id"), FromId);
                R->SetStringField(TEXT("from_pin"), FromPin);
                R->SetStringField(TEXT("to_id"), ToId);
                R->SetStringField(TEXT("to_pin"), ToPin);
                R->SetStringField(TEXT("reason"), Reason);
                Rejected.Add(MakeShared<FJsonValueObject>(R));
            };

            UEdGraphNode* FromNode = ResolveEndpoint(FromId);
            UEdGraphNode* ToNode = ResolveEndpoint(ToId);
            if (!FromNode) { RejectWith(FString::Printf(TEXT("unresolved from_id '%s'"), *FromId)); continue; }
            if (!ToNode)   { RejectWith(FString::Printf(TEXT("unresolved to_id '%s'"), *ToId)); continue; }
            UEdGraphPin* A = MCPBpM_FindPin(FromNode, FromPin);
            UEdGraphPin* B = MCPBpM_FindPin(ToNode, ToPin);
            if (!A) { RejectWith(FString::Printf(TEXT("from-node has no pin '%s'"), *FromPin)); continue; }
            if (!B) { RejectWith(FString::Printf(TEXT("to-node has no pin '%s'"), *ToPin)); continue; }

            const FPinConnectionResponse Resp = Schema->CanCreateConnection(A, B);  // VERIFY vs engine source
            bool bOk = false;
            try { bOk = Schema->TryCreateConnection(A, B); }                   // VERIFY vs engine source (gated on 2 non-null pins)
            catch (...) { bOk = false; }
            if (bOk) { ++ConnectedCount; }
            else
            {
                FString Reason = Resp.Message.ToString();
                if (Reason.IsEmpty()) { Reason = TEXT("incompatible pins/directions"); }
                RejectWith(Reason);
            }
        }
    }

    // ---- Pin defaults. ----
    const TArray<TSharedPtr<FJsonValue>>* SpecDefaults = nullptr;
    Spec->TryGetArrayField(TEXT("pin_defaults"), SpecDefaults);
    int32 DefaultsCount = 0;
    TArray<TSharedPtr<FJsonValue>> DefaultErrors;
    if (SpecDefaults)
    {
        for (const TSharedPtr<FJsonValue>& V : *SpecDefaults)
        {
            const TSharedPtr<FJsonObject>* DObjPtr = nullptr;
            if (!V.IsValid() || !V->TryGetObject(DObjPtr) || !DObjPtr) { continue; }
            TSharedPtr<FJsonObject> DObj = *DObjPtr;

            FString NodeId, PinName, Value;
            DObj->TryGetStringField(TEXT("node_id"), NodeId);
            DObj->TryGetStringField(TEXT("pin"), PinName);
            DObj->TryGetStringField(TEXT("value"), Value);

            UEdGraphNode* Node = ResolveEndpoint(NodeId);
            UEdGraphPin* Pin = Node ? MCPBpM_FindPin(Node, PinName) : nullptr;
            if (!Pin)
            {
                TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
                E->SetStringField(TEXT("node_id"), NodeId);
                E->SetStringField(TEXT("pin"), PinName);
                E->SetStringField(TEXT("error"), Node ? TEXT("no such pin") : TEXT("unresolved node_id"));
                DefaultErrors.Add(MakeShared<FJsonValueObject>(E));
                continue;
            }
            MCPBpM_ApplyPinDefault(Schema, Pin, Value);
            ++DefaultsCount;
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);            // VERIFY vs engine source — NO per-op compile

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("blueprint_path"), BP->GetPathName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("mode"), ModeL);
    Root->SetNumberField(TEXT("removed_count"), RemovedCount);
    Root->SetNumberField(TEXT("created_count"), Created.Num());
    Root->SetNumberField(TEXT("connected_count"), ConnectedCount);
    Root->SetNumberField(TEXT("pin_defaults_set"), DefaultsCount);
    Root->SetArrayField(TEXT("created"), Created);
    Root->SetArrayField(TEXT("node_errors"), NodeErrors);
    Root->SetArrayField(TEXT("rejected_connections"), Rejected);
    Root->SetArrayField(TEXT("pin_default_errors"), DefaultErrors);
    // The full document snapshot of the PRIOR graph -> the Python side ledgers it as restore_blueprint_graph.
    Root->SetObjectField(TEXT("before_snapshot"), BeforeSnapshot);
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 2) ArrangeBlueprintGraphJson — columnar auto-layout (depth-from-source over exec+data links). Mirrors
//    LayoutNiagaraGraph: OptionsJson may carry {column_width, row_height, restore_positions:{guid:[x,y]}}.
//    restore_positions overrides the computed layout — the SAME handler serves the undo path.
// =====================================================================================================
FString UMCPReflectionLibrary::ArrangeBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName,
    const FString& OptionsJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpM_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPBpM_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }
    FString Err;
    UEdGraph* Graph = MCPBpM_ResolveGraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpM_Err(Err); }

    int32 ColW = 320, RowH = 180;
    TSharedPtr<FJsonObject> Opts = OptionsJson.IsEmpty() ? nullptr : MCPBpM_ParseObject(OptionsJson);
    const TSharedPtr<FJsonObject>* RestorePtr = nullptr;
    if (Opts.IsValid())
    {
        double D = 0.0;
        if (Opts->TryGetNumberField(TEXT("column_width"), D) && D > 0) { ColW = (int32)D; }
        if (Opts->TryGetNumberField(TEXT("row_height"), D) && D > 0)   { RowH = (int32)D; }
        Opts->TryGetObjectField(TEXT("restore_positions"), RestorePtr);
    }

    // Capture prior positions (as a {guid:[x,y]} object) for the undo -> feeds back as restore_positions.
    TSharedRef<FJsonObject> PriorPositions = MakeShared<FJsonObject>();
    for (UEdGraphNode* N : Graph->Nodes)
    {
        if (!N) { continue; }
        TArray<TSharedPtr<FJsonValue>> XY;
        XY.Add(MakeShared<FJsonValueNumber>(N->NodePosX));
        XY.Add(MakeShared<FJsonValueNumber>(N->NodePosY));
        PriorPositions->SetArrayField(N->NodeGuid.ToString(), XY);
    }

    int32 MovedCount = 0;

    if (RestorePtr && (*RestorePtr).IsValid())
    {
        // ---- Undo path: set each node's position from the supplied {guid:[x,y]} map. ----
        for (const TPair<FString, TSharedPtr<FJsonValue>>& Pair : (*RestorePtr)->Values)
        {
            UEdGraphNode* N = MCPBpM_FindNodeByGuid(Graph, Pair.Key);
            if (!N || !Pair.Value.IsValid()) { continue; }
            const TArray<TSharedPtr<FJsonValue>>* Arr = nullptr;
            if (!Pair.Value->TryGetArray(Arr) || !Arr || Arr->Num() < 2) { continue; }
            N->Modify();
            N->NodePosX = (int32)(*Arr)[0]->AsNumber();
            N->NodePosY = (int32)(*Arr)[1]->AsNumber();
            ++MovedCount;
        }
    }
    else
    {
        // ---- Layout: column = longest input-chain depth from a source (memoized, cycle-guarded). Sources sit
        //      left (col 0), downstream nodes step right; rows stack within a column in node order. ----
        TArray<UEdGraphNode*> Nodes;
        for (UEdGraphNode* N : Graph->Nodes) { if (N) { Nodes.Add(N); } }

        TMap<UEdGraphNode*, int32> ColMemo;
        TSet<UEdGraphNode*> InProgress;
        TFunction<int32(UEdGraphNode*)> ColOf = [&](UEdGraphNode* Node) -> int32
        {
            if (!Node) { return 0; }
            if (const int32* Found = ColMemo.Find(Node)) { return *Found; }
            if (InProgress.Contains(Node)) { return 0; }                      // cycle guard
            InProgress.Add(Node);
            int32 Best = 0;
            for (UEdGraphPin* P : Node->Pins)
            {
                if (!P || P->Direction != EGPD_Input) { continue; }          // walk PREDECESSORS via input pins
                for (UEdGraphPin* Linked : P->LinkedTo)
                {
                    if (!Linked || !Linked->GetOwningNodeUnchecked()) { continue; }
                    Best = FMath::Max(Best, 1 + ColOf(Linked->GetOwningNode()));
                }
            }
            InProgress.Remove(Node);
            ColMemo.Add(Node, Best);
            return Best;
        };
        for (UEdGraphNode* N : Nodes) { ColOf(N); }

        TMap<int32, int32> RowCursor;   // column -> next row index
        for (UEdGraphNode* N : Nodes)
        {
            const int32 Col = ColMemo.FindRef(N);
            int32& Row = RowCursor.FindOrAdd(Col);
            N->Modify();
            N->NodePosX = Col * ColW;
            N->NodePosY = Row * RowH;
            ++Row;
            ++MovedCount;
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetNumberField(TEXT("node_count"), Graph->Nodes.Num());
    Root->SetNumberField(TEXT("moved_count"), MovedCount);
    Root->SetNumberField(TEXT("column_width"), ColW);
    Root->SetNumberField(TEXT("row_height"), RowH);
    Root->SetBoolField(TEXT("restored"), RestorePtr && (*RestorePtr).IsValid());
    Root->SetObjectField(TEXT("prior_positions"), PriorPositions);            // {guid:[x,y]} -> ledger as restore_positions
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 3) CreateBlueprintInterfaceJson — a BPTYPE_Interface UBlueprint (UInterface parent). NOT saved here
//    (Python saves). Inverse: delete the asset (generic create_asset ledger op).
// =====================================================================================================
FString UMCPReflectionLibrary::CreateBlueprintInterfaceJson(const FString& Name, const FString& Path)
{
#if WITH_EDITOR
    if (Name.IsEmpty() || Path.IsEmpty()) { return MCPBpM_Err(TEXT("Name and Path are required")); }

    FString FullPackageName = Path;
    if (!FullPackageName.EndsWith(TEXT("/"))) { FullPackageName += TEXT("/"); }
    FullPackageName += Name;

    if (FindPackage(nullptr, *FullPackageName) || FindObject<UObject>(nullptr, *(FullPackageName + TEXT(".") + Name)))
    {
        return MCPBpM_Err(FString::Printf(TEXT("an asset already exists at '%s'"), *FullPackageName));
    }

    UPackage* Package = CreatePackage(*FullPackageName);
    if (!Package) { return MCPBpM_Err(TEXT("CreatePackage failed")); }
    Package->FullyLoad();

    // BPTYPE_Interface cannot be created by Python's create-with-parent path — this uses the 6-arg CreateBlueprint.
    UObject* Created = nullptr;
    try
    {
        Created = FKismetEditorUtilities::CreateBlueprint(                    // VERIFY vs engine source (6-arg overload)
            UInterface::StaticClass(),
            Package,
            FName(*Name),
            BPTYPE_Interface,
            UBlueprint::StaticClass(),
            UBlueprintGeneratedClass::StaticClass(),
            FName(TEXT("MCPCreateBlueprintInterface")));
    }
    catch (...) { return MCPBpM_Err(TEXT("exception during CreateBlueprint")); }

    UBlueprint* NewBP = Cast<UBlueprint>(Created);
    if (!NewBP) { return MCPBpM_Err(TEXT("CreateBlueprint did not return a UBlueprint")); }

    FAssetRegistryModule::AssetCreated(NewBP);                                // VERIFY vs engine source
    NewBP->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), NewBP->GetName());
    Root->SetStringField(TEXT("package"), FullPackageName);
    Root->SetStringField(TEXT("asset_path"), NewBP->GetPathName());
    Root->SetStringField(TEXT("blueprint_type"), TEXT("interface"));
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 4) CreateTypedBlueprintJson — general typed-BP creator (normal / function_library / macro_library / const /
//    interface). Backs the Python function-library fallback. NOT saved here. Inverse: delete the asset.
// =====================================================================================================
FString UMCPReflectionLibrary::CreateTypedBlueprintJson(const FString& Name, const FString& Path,
    const FString& ParentClass, const FString& BlueprintType)
{
#if WITH_EDITOR
    if (Name.IsEmpty() || Path.IsEmpty()) { return MCPBpM_Err(TEXT("Name and Path are required")); }

    EBlueprintType BPType;
    if (!MCPBpM_ParseBlueprintType(BlueprintType, BPType))
    {
        return MCPBpM_Err(FString::Printf(TEXT("unknown blueprint_type '%s' (expected normal|function_library|"
            "macro_library|const|interface)"), *BlueprintType));
    }

    // Resolve the parent class. Explicit ParentClass wins; else default by type.
    UClass* Parent = MCPBpM_ResolveClass(ParentClass);
    if (!Parent)
    {
        if (BPType == BPTYPE_Interface)
        {
            Parent = UInterface::StaticClass();
        }
        else if (BPType == BPTYPE_FunctionLibrary)
        {
            Parent = MCPBpM_ResolveClass(TEXT("/Script/Engine.BlueprintFunctionLibrary"));
            if (!Parent) { Parent = UObject::StaticClass(); }
        }
        else if (BPType == BPTYPE_MacroLibrary || BPType == BPTYPE_Const)
        {
            Parent = UObject::StaticClass();
        }
        else
        {
            Parent = AActor::StaticClass();
        }
    }
    if (!Parent) { return MCPBpM_Err(FString::Printf(TEXT("could not resolve parent class '%s'"), *ParentClass)); }

    FString FullPackageName = Path;
    if (!FullPackageName.EndsWith(TEXT("/"))) { FullPackageName += TEXT("/"); }
    FullPackageName += Name;

    if (FindPackage(nullptr, *FullPackageName) || FindObject<UObject>(nullptr, *(FullPackageName + TEXT(".") + Name)))
    {
        return MCPBpM_Err(FString::Printf(TEXT("an asset already exists at '%s'"), *FullPackageName));
    }

    UPackage* Package = CreatePackage(*FullPackageName);
    if (!Package) { return MCPBpM_Err(TEXT("CreatePackage failed")); }
    Package->FullyLoad();

    UObject* Created = nullptr;
    try
    {
        Created = FKismetEditorUtilities::CreateBlueprint(                    // VERIFY vs engine source (6-arg overload)
            Parent,
            Package,
            FName(*Name),
            BPType,
            UBlueprint::StaticClass(),
            UBlueprintGeneratedClass::StaticClass(),
            FName(TEXT("MCPCreateTypedBlueprint")));
    }
    catch (...) { return MCPBpM_Err(TEXT("exception during CreateBlueprint")); }

    UBlueprint* NewBP = Cast<UBlueprint>(Created);
    if (!NewBP) { return MCPBpM_Err(TEXT("CreateBlueprint did not return a UBlueprint")); }

    FAssetRegistryModule::AssetCreated(NewBP);
    NewBP->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), NewBP->GetName());
    Root->SetStringField(TEXT("package"), FullPackageName);
    Root->SetStringField(TEXT("asset_path"), NewBP->GetPathName());
    Root->SetStringField(TEXT("parent_class"), Parent->GetPathName());
    Root->SetStringField(TEXT("blueprint_type"), BlueprintType.IsEmpty() ? TEXT("normal") : BlueprintType.ToLower());
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 5) GetTypeRegistryJson — TObjectRange walk of UClass/UScriptStruct/UEnum with name/engine filters + a Max
//    cap. READ-ONLY (no ledger). Full-fidelity native-type coverage Python's AssetRegistry search cannot reach.
// =====================================================================================================
FString UMCPReflectionLibrary::GetTypeRegistryJson(const FString& Kind, const FString& Query, bool bIncludeEngine, int32 Max)
{
#if WITH_EDITOR
    FString K = Kind.ToLower();
    if (K.IsEmpty()) { K = TEXT("class"); }
    if (K != TEXT("class") && K != TEXT("struct") && K != TEXT("enum"))
    {
        return MCPBpM_Err(FString::Printf(TEXT("unknown kind '%s' (expected class|struct|enum)"), *Kind));
    }
    const int32 Cap = (Max <= 0) ? 500 : Max;
    const FString Q = Query.ToLower();

    TArray<TSharedPtr<FJsonValue>> Types;
    int32 TotalMatched = 0;

    auto NameMatches = [&Q](const FString& Nm) -> bool
    {
        return Q.IsEmpty() || Nm.ToLower().Contains(Q);
    };

    if (K == TEXT("class"))
    {
        for (UClass* C : TObjectRange<UClass>())
        {
            if (!C) { continue; }
            const FString Nm = C->GetName();
            if (!NameMatches(Nm)) { continue; }
            if (!bIncludeEngine && MCPBpM_IsEngineType(C)) { continue; }
            ++TotalMatched;
            if (Types.Num() >= Cap) { continue; }
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("name"), Nm);
            J->SetStringField(TEXT("path"), C->GetClassPathName().ToString());  // VERIFY vs engine source
            if (UClass* Super = C->GetSuperClass()) { J->SetStringField(TEXT("parent"), Super->GetName()); }
            J->SetBoolField(TEXT("is_abstract"), C->HasAnyClassFlags(CLASS_Abstract));
            J->SetBoolField(TEXT("is_native"), C->IsNative());
            J->SetBoolField(TEXT("is_interface"), C->HasAnyClassFlags(CLASS_Interface));
            if (C->GetOutermost()) { J->SetStringField(TEXT("package"), C->GetOutermost()->GetName()); }
            Types.Add(MakeShared<FJsonValueObject>(J));
        }
    }
    else if (K == TEXT("struct"))
    {
        for (UScriptStruct* S : TObjectRange<UScriptStruct>())
        {
            if (!S) { continue; }
            const FString Nm = S->GetName();
            if (!NameMatches(Nm)) { continue; }
            if (!bIncludeEngine && MCPBpM_IsEngineType(S)) { continue; }
            ++TotalMatched;
            if (Types.Num() >= Cap) { continue; }
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("name"), Nm);
            J->SetStringField(TEXT("path"), S->GetPathName());
            J->SetBoolField(TEXT("is_native"), S->IsNative());
            if (UStruct* Super = S->GetSuperStruct()) { J->SetStringField(TEXT("parent"), Super->GetName()); }
            if (S->GetOutermost()) { J->SetStringField(TEXT("package"), S->GetOutermost()->GetName()); }
            Types.Add(MakeShared<FJsonValueObject>(J));
        }
    }
    else // enum
    {
        for (UEnum* E : TObjectRange<UEnum>())
        {
            if (!E) { continue; }
            const FString Nm = E->GetName();
            if (!NameMatches(Nm)) { continue; }
            if (!bIncludeEngine && MCPBpM_IsEngineType(E)) { continue; }
            ++TotalMatched;
            if (Types.Num() >= Cap) { continue; }
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("name"), Nm);
            J->SetStringField(TEXT("path"), E->GetPathName());
            J->SetBoolField(TEXT("is_native"), E->IsNative());
            J->SetNumberField(TEXT("num_values"), E->NumEnums());             // VERIFY vs engine source (includes _MAX sentinel)
            if (E->GetOutermost()) { J->SetStringField(TEXT("package"), E->GetOutermost()->GetName()); }
            Types.Add(MakeShared<FJsonValueObject>(J));
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("kind"), K);
    Root->SetStringField(TEXT("query"), Query);
    Root->SetBoolField(TEXT("include_engine"), bIncludeEngine);
    Root->SetNumberField(TEXT("max"), Cap);
    Root->SetNumberField(TEXT("total_matched"), TotalMatched);
    Root->SetNumberField(TEXT("count"), Types.Num());
    Root->SetBoolField(TEXT("truncated"), TotalMatched > Types.Num());
    Root->SetArrayField(TEXT("types"), Types);
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 6) SetBlueprintVariableFlagsJson — set ALL variable flags (incl. private / blueprint_read_only / config
//    which BlueprintEditorLibrary has no setter for) by toggling FBPVariableDescription::PropertyFlags bits +
//    private/expose_on_spawn METADATA. Captures prior flags. Companion to GetBlueprintVariableFlagsJson.
//    FlagsJson keys are ALL OPTIONAL (only provided keys are applied):
//      instance_editable, blueprint_read_only, config, expose_on_spawn, private, expose_to_cinematics (bool)
//    Inverse: re-call this handler with the returned prior_flags.
// =====================================================================================================
FString UMCPReflectionLibrary::SetBlueprintVariableFlagsJson(const FString& BlueprintPath, const FString& VariableName,
    const FString& FlagsJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpM_LoadBlueprint(BlueprintPath);
    if (!BP) { return MCPBpM_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    TSharedPtr<FJsonObject> Flags = MCPBpM_ParseObject(FlagsJson);
    if (!Flags.IsValid()) { return MCPBpM_Err(TEXT("flags_json is not a JSON object")); }

    const FName VarFName(*VariableName);
    FBPVariableDescription* Var = nullptr;
    for (FBPVariableDescription& D : BP->NewVariables)                        // VERIFY vs engine source (NON-const walk)
    {
        if (D.VarName == VarFName) { Var = &D; break; }
    }
    if (!Var) { return MCPBpM_Err(FString::Printf(TEXT("no variable named '%s' on blueprint '%s'"), *VariableName, *BP->GetName())); }

    // ---- Read the current logical flags (as the getter does) for the prior snapshot + post-read. ----
    auto ReadFlags = [&](const FBPVariableDescription* V) -> TSharedRef<FJsonObject>
    {
        const uint64 F = V->PropertyFlags;                                   // VERIFY vs engine source
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("instance_editable"), (F & CPF_DisableEditOnInstance) == 0);
        O->SetBoolField(TEXT("blueprint_read_only"), (F & CPF_BlueprintReadOnly) != 0);
        O->SetBoolField(TEXT("config"), (F & CPF_Config) != 0);
        O->SetBoolField(TEXT("expose_to_cinematics"), (F & CPF_Interp) != 0);
        bool bXOS = false;
        if (V->HasMetaData(FBlueprintMetadata::MD_ExposeOnSpawn)) { bXOS = V->GetMetaData(FBlueprintMetadata::MD_ExposeOnSpawn).ToBool(); }
        O->SetBoolField(TEXT("expose_on_spawn"), bXOS);
        bool bPriv = (F & CPF_NativeAccessSpecifierPrivate) != 0;
        if (!bPriv && V->HasMetaData(FBlueprintMetadata::MD_Private)) { bPriv = V->GetMetaData(FBlueprintMetadata::MD_Private).ToBool(); }
        O->SetBoolField(TEXT("private"), bPriv);
        return O;
    };

    TSharedRef<FJsonObject> PriorFlags = ReadFlags(Var);

    BP->Modify();

    // Helper: set/clear a CPF bit on PropertyFlags.
    auto SetBit = [&](uint64 Bit, bool bOn)
    {
        if (bOn) { Var->PropertyFlags |= Bit; } else { Var->PropertyFlags &= ~Bit; }
    };

    bool bVal = false;
    TArray<FString> Applied;

    if (MCPBpM_TryGetBool(Flags, TEXT("instance_editable"), bVal))
    {
        // instance_editable is the INVERSE of CPF_DisableEditOnInstance. Editable vars also need CPF_Edit set.
        SetBit(CPF_DisableEditOnInstance, !bVal);
        if (bVal) { Var->PropertyFlags |= CPF_Edit; }                        // VERIFY vs engine source (BP vars are CPF_Edit|CPF_BlueprintVisible)
        Applied.Add(TEXT("instance_editable"));
    }
    if (MCPBpM_TryGetBool(Flags, TEXT("blueprint_read_only"), bVal))
    {
        SetBit(CPF_BlueprintReadOnly, bVal);
        Applied.Add(TEXT("blueprint_read_only"));
    }
    if (MCPBpM_TryGetBool(Flags, TEXT("config"), bVal))
    {
        SetBit(CPF_Config, bVal);
        Applied.Add(TEXT("config"));
    }
    if (MCPBpM_TryGetBool(Flags, TEXT("expose_to_cinematics"), bVal))
    {
        SetBit(CPF_Interp, bVal);
        if (bVal) { Var->PropertyFlags |= CPF_Edit; }
        Applied.Add(TEXT("expose_to_cinematics"));
    }
    if (MCPBpM_TryGetBool(Flags, TEXT("expose_on_spawn"), bVal))
    {
        // The editor stores this as BOTH the CPF_ExposeOnSpawn flag AND MD_ExposeOnSpawn metadata; the getter reads
        // the metadata, so set both to keep them consistent.
        SetBit(CPF_ExposeOnSpawn, bVal);
        if (bVal) { Var->SetMetaData(FBlueprintMetadata::MD_ExposeOnSpawn, TEXT("true")); }
        else if (Var->HasMetaData(FBlueprintMetadata::MD_ExposeOnSpawn)) { Var->RemoveMetaData(FBlueprintMetadata::MD_ExposeOnSpawn); }
        Applied.Add(TEXT("expose_on_spawn"));
    }
    if (MCPBpM_TryGetBool(Flags, TEXT("private"), bVal))
    {
        // BP-authored privacy lives in MD_Private metadata (the getter reads it); do NOT touch the native
        // CPF_NativeAccessSpecifierPrivate bit (that is for C++ properties).
        if (bVal) { Var->SetMetaData(FBlueprintMetadata::MD_Private, TEXT("true")); }
        else if (Var->HasMetaData(FBlueprintMetadata::MD_Private)) { Var->RemoveMetaData(FBlueprintMetadata::MD_Private); }
        Applied.Add(TEXT("private"));
    }

    if (Applied.Num() == 0)
    {
        return MCPBpM_Err(TEXT("flags_json set no recognized keys (instance_editable|blueprint_read_only|config|"
            "expose_on_spawn|private|expose_to_cinematics)"));
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);           // VERIFY vs engine source

    TSharedRef<FJsonObject> NewFlags = ReadFlags(Var);

    TArray<TSharedPtr<FJsonValue>> AppliedArr;
    for (const FString& A : Applied) { AppliedArr.Add(MakeShared<FJsonValueString>(A)); }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("var"), VariableName);
    Root->SetArrayField(TEXT("applied"), AppliedArr);
    Root->SetObjectField(TEXT("prior_flags"), PriorFlags);
    Root->SetObjectField(TEXT("new_flags"), NewFlags);
    return MCPBpM_Serialize(Root);
#else
    return MCPBpM_Err(TEXT("editor-only"));
#endif
}
