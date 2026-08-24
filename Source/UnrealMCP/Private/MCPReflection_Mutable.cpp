// UnrealMCP — MUTABLE (CustomizableObject) GRAPH-AUTHORING subsystem (C++ DRAFT 2026-08-19, mutable Wave 3).
//
// The graph reader + node primitives for a CustomizableObject's editor Source graph — the C++-only piece the
// Python create/compile/instance/parameter tools cannot reach. Member DEFINITIONS for UMCPReflectionLibrary;
// the matching UFUNCTION declarations (block #46) already live in MCPReflectionLibrary.h. Seven handlers:
//
//   READERS (no mutation, no ledger):
//     1) GetMutableGraphJson       — full walk of the CO Source UEdGraph: per node {class, class_path, title,
//                                     node_guid, x, y, pins:[{name,direction,pin_type,default_value,
//                                     linked_to:[...]}]}. THE READ that proves Source is reachable.
//     2) GetMutableNodeJson        — one node's pins (+links) + a compact set of reflected FProperties.
//
//   NODE PRIMITIVES (writes; the Python caller folds each inverse into the shared per-session undo ledger and
//   persists with a NON-VALIDATING save — the graph edit itself does NOT compile the CO):
//     3) AddMutableNodeJson        — resolve node class by NAME -> NewObject -> base UEdGraphNode lifecycle
//                                     (AddNode / CreateNewGuid / PostPlacedNewNode / AllocateDefaultPins /
//                                     ReconstructNode).
//     4) ConnectMutableNodesJson   — Schema->TryCreateConnection (reports CanCreateConnection's reason).
//     5) DisconnectMutablePinJson  — break one specific link (other_guid+other_pin) or ALL links on a pin.
//     6) DeleteMutableNodeJson     — remove a node by GUID; captures a LOSSY re-add spec (class+pos+links).
//     7) SetMutableNodePropertyJson— set an FProperty on the node (ExportText prior / ImportText new / reconstruct).
//
// ZERO MUTABLE HEADERS — WHY / HOW (v2 rewrite after the Internal-header build failure):
//   UCustomizableObjectPrivate lives in the CustomizableObject module's Internal/ folder, which is NOT on the
//   include path of an EXTERNAL plugin (only Public headers are), and MuCOE/Nodes/CustomizableObjectNode.h
//   transitively pulls that Internal header too. So this .cpp includes NO Mutable header. Every operation is a
//   virtual on the ENGINE_API base UEdGraphNode / UEdGraphSchema, a plain UEdGraph method, or reflection:
//     * Reach the Source graph by ENUMERATING the CO's inner objects (GetObjectsWithOuter, recursive) and
//       picking the editor UEdGraph whose class name contains "CustomizableObject" (else the first UEdGraph).
//       This replaces the non-includable GetPrivate()->GetSource().
//     * Resolve node classes by NAME (UClass::TryFindTypeSlow / FindObject on /Script paths), validate against
//       a name-resolved UEdGraphNode base + (best-effort) the name-resolved CustomizableObjectNode base.
//     * Create nodes via NewObject<UEdGraphNode>(Graph, ResolvedClass) + base virtuals only (PostPlacedNewNode
//       / AllocateDefaultPins / ReconstructNode — the CO overrides run via vtable). BeginConstruct /
//       PostBackwardsCompatibleFixup are SKIPPED (they need the concrete type / an included header); the
//       coordinator live-tests whether a bare node gets valid pins — an accepted limitation for `add`.
//   The CustomizableObject + CustomizableObjectEditor modules remain Build.cs PrivateDependencyModuleNames so
//   the classes are REGISTERED at runtime (name resolution + vtables work); we simply do not #include them.
//
// GATING RESULT: NO ENGINE EXPORT PATCH. The reach is a base-virtual/reflection walk, not an exported-symbol call.
//
// CRASH-SAFETY: every load/guid/pin lookup is guarded (LoadObject + null-check; FGuid::Parse; FindPin +
// null-check, NEVER FindPinChecked); class resolution is validated (concrete, IsChildOf UEdGraphNode, not
// abstract) before NewObject; TryCreateConnection is gated on two non-null pins; ImportText failures are
// reported, not asserted. Re-entrant engine calls are wrapped in try/catch. Any miss returns {"error":...}.
// All handlers are #if WITH_EDITOR guarded (cooked build returns {"error":"editor-only"}).
//
// PERSISTENCE: these handlers MUTATE the loaded CO's in-memory Source graph and mark the package dirty +
// NotifyGraphChanged. The Python wiring (mutable_graph_cpp.py) performs the NON-VALIDATING save after each
// write, mirroring mutable_write.py's _save_nonvalidating. Compiling the CO is a SEPARATE step and is not
// triggered here.
//
// Anonymous-namespace helpers are prefixed `MCPMut_` so they stay unique in the module's unity build.
// Every engine-API touch point is tagged "VERIFY vs engine source" for the coordinator's live pass.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"
#include "UObject/UnrealType.h"        // FProperty::ImportText_InContainer / ExportText_InContainer / TFieldIterator
#include "UObject/UObjectGlobals.h"    // LoadObject / FindObject / NewObject
#include "UObject/UObjectHash.h"       // GetObjectsWithOuter
#include "UObject/Object.h"
#include "Misc/PackageName.h"          // FPackageName::GetShortName (bare-path load fallback)

#include "EdGraph/EdGraph.h"           // UEdGraph::Nodes / AddNode / RemoveNode / GetSchema / NotifyGraphChanged
#include "EdGraph/EdGraphNode.h"       // UEdGraphNode (NodeGuid / NodePosX / Pins / GetNodeTitle / CreateNewGuid / virtuals)
#include "EdGraph/EdGraphPin.h"        // UEdGraphPin / FEdGraphPinType / EEdGraphPinDirection
#include "EdGraph/EdGraphSchema.h"     // UEdGraphSchema (TryCreateConnection / CanCreateConnection / Break* virtuals)

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPMut_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error"). Success objects never
    // carry an "error" key.
    FString MCPMut_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPMut_Serialize(Root);
    }

#if WITH_EDITOR
    // Load the CustomizableObject as a bare UObject (we never reference the UCustomizableObject type).
    // Accepts "/Game/Path/CO_Foo.CO_Foo" or "/Game/Path/CO_Foo".
    UObject* MCPMut_LoadCO(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UObject* CO = LoadObject<UObject>(nullptr, *Path);
        if (!CO)
        {
            // Tolerate a bare package path lacking the .Object suffix.
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            CO = LoadObject<UObject>(nullptr, *Full);
        }
        return CO;
    }

    // Reach the CO's editor Source graph WITHOUT the private accessor: enumerate the CO's inner objects
    // (recursive — the Source graph is a subobject of the CO's private object) and pick the editor UEdGraph
    // whose class name contains "CustomizableObject"; fall back to the first UEdGraph found.
    UEdGraph* MCPMut_ResolveSource(UObject* CO, FString& OutErr)
    {
        if (!CO)
        {
            OutErr = TEXT("null customizable object");
            return nullptr;
        }
        TArray<UObject*> Inners;
        GetObjectsWithOuter(CO, Inners, /*bIncludeNestedObjects*/true);     // VERIFY vs engine source (UObjectHash)
        UEdGraph* Best = nullptr;
        for (UObject* O : Inners)
        {
            if (UEdGraph* G = Cast<UEdGraph>(O))
            {
                if (G->GetClass()->GetName().Contains(TEXT("CustomizableObject")))
                {
                    return G;                                              // the UCustomizableObjectGraph
                }
                if (!Best)
                {
                    Best = G;
                }
            }
        }
        if (!Best)
        {
            OutErr = TEXT("customizable object has no Source graph (unauthored, or a cooked/non-editor object)");
        }
        return Best;
    }

    // Find a node by GUID within one graph (guarded FGuid::Parse; never crashes on bad input).
    UEdGraphNode* MCPMut_FindNode(UEdGraph* Graph, const FString& GuidStr, FString& OutErr)
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
            if (N && N->NodeGuid == Target)                                // VERIFY vs engine source (UEdGraphNode::NodeGuid)
            {
                return N;
            }
        }
        OutErr = FString::Printf(TEXT("no node with guid '%s' in CO source graph"), *GuidStr);
        return nullptr;
    }

    // Find a pin by name (exact FName first, case-insensitive fallback). NEVER FindPinChecked.
    UEdGraphPin* MCPMut_FindPin(UEdGraphNode* Node, const FString& PinName)
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

    // Resolve a UEdGraphNode subclass from a NAME with no Mutable header: short name -> full /Script path ->
    // /Script/CustomizableObjectEditor.<Name>. Caller validates concrete + IsChildOf UEdGraphNode + (best-
    // effort) the name-resolved CustomizableObjectNode base.
    UClass* MCPMut_ResolveNodeClass(const FString& Name)
    {
        if (Name.IsEmpty())
        {
            return nullptr;
        }
        UClass* Cls = UClass::TryFindTypeSlow<UClass>(Name);               // VERIFY vs engine source (short-name UClass lookup)
        if (!Cls) { Cls = FindObject<UClass>(nullptr, *Name); }            // full /Script path
        if (!Cls) { Cls = LoadObject<UClass>(nullptr, *Name); }
        if (!Cls)
        {
            const FString EditorPath = FString::Printf(TEXT("/Script/CustomizableObjectEditor.%s"), *Name);
            Cls = FindObject<UClass>(nullptr, *EditorPath);
            if (!Cls) { Cls = LoadObject<UClass>(nullptr, *EditorPath); }
        }
        return Cls;
    }

    // FEdGraphPinType -> JSON. Mutable pins are plain UEdGraphPins whose PinCategory is one of the schema's
    // PC_* FNames ("Object","Mesh","Material","Color","Float",...). The generic serializer captures it.
    TSharedRef<FJsonObject> MCPMut_SerializePinType(const FEdGraphPinType& T)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("category"), T.PinCategory.ToString());
        if (!T.PinSubCategory.IsNone())
        {
            J->SetStringField(TEXT("sub_category"), T.PinSubCategory.ToString());
        }
        if (T.PinSubCategoryObject.IsValid())                             // TWeakObjectPtr — .IsValid()/.Get() are safe
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
        return J;
    }

    // One pin -> JSON. bWithLinks appends linked_to:[{node_guid, pin_name}].
    TSharedRef<FJsonObject> MCPMut_SerializePin(UEdGraphPin* Pin, bool bWithLinks)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Pin)
        {
            return J;
        }
        J->SetStringField(TEXT("name"), Pin->PinName.ToString());
        J->SetStringField(TEXT("direction"), Pin->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
        J->SetObjectField(TEXT("pin_type"), MCPMut_SerializePinType(Pin->PinType));
        J->SetStringField(TEXT("default_value"), Pin->DefaultValue);
        if (Pin->DefaultObject)
        {
            J->SetStringField(TEXT("default_object"), Pin->DefaultObject->GetPathName());
        }
        J->SetStringField(TEXT("pin_id"), Pin->PinId.ToString());
        J->SetBoolField(TEXT("is_orphaned"), Pin->bOrphanedPin);          // VERIFY vs engine source (orphan pins on type change)
        if (bWithLinks)
        {
            TArray<TSharedPtr<FJsonValue>> Links;
            for (UEdGraphPin* L : Pin->LinkedTo)
            {
                if (!L || !L->GetOwningNodeUnchecked())                   // GetOwningNodeUnchecked guards a stale/None owner
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

    // One node -> JSON: {class, class_path, title, node_guid, x, y[, pins]}.
    TSharedRef<FJsonObject> MCPMut_SerializeNode(UEdGraphNode* Node, bool bWithPins, bool bWithLinks)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Node)
        {
            return J;
        }
        J->SetStringField(TEXT("class"), Node->GetClass()->GetName());
        J->SetStringField(TEXT("class_path"), Node->GetClass()->GetPathName());
        FString Title;
        // GetNodeTitle is a base virtual that dispatches to the CO override; guard it (some nodes read members).
        try
        {
            Title = Node->GetNodeTitle(ENodeTitleType::ListView).ToString();  // VERIFY vs engine source (UEdGraphNode::GetNodeTitle)
        }
        catch (...)
        {
            Title = Node->GetClass()->GetName();
        }
        J->SetStringField(TEXT("title"), Title);
        J->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
        J->SetNumberField(TEXT("x"), Node->NodePosX);
        J->SetNumberField(TEXT("y"), Node->NodePosY);
        if (bWithPins)
        {
            TArray<TSharedPtr<FJsonValue>> Pins;
            for (UEdGraphPin* P : Node->Pins)
            {
                if (!P) { continue; }
                Pins.Add(MakeShared<FJsonValueObject>(MCPMut_SerializePin(P, bWithLinks)));
            }
            J->SetArrayField(TEXT("pins"), Pins);
        }
        return J;
    }

    // A compact set of the node's reflected FProperties as {name: exported-text}. Skips base bookkeeping +
    // the internal pin-data map + very large exports. Best-effort — never throws.
    TSharedRef<FJsonObject> MCPMut_SerializeNodeProperties(UEdGraphNode* Node)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        if (!Node)
        {
            return J;
        }
        for (TFieldIterator<FProperty> It(Node->GetClass()); It; ++It)     // VERIFY vs engine source (TFieldIterator<FProperty>)
        {
            FProperty* Prop = *It;
            if (!Prop) { continue; }
            const FString PName = Prop->GetName();
            if (PName == TEXT("Pins") || PName == TEXT("PinsDataId") || PName == TEXT("PinsData_DEPRECATED") ||
                PName == TEXT("NodeGuid") || PName == TEXT("NodePosX") || PName == TEXT("NodePosY") ||
                PName == TEXT("NodeWidth") || PName == TEXT("NodeHeight") || PName == TEXT("NodeComment"))
            {
                continue;
            }
            FString Text;
            Prop->ExportText_InContainer(0, Text, Node, /*Delta*/nullptr, /*Parent*/Node, PPF_None);  // VERIFY vs engine source (void)
            if (Text.Len() > 2048)
            {
                Text = Text.Left(2048) + TEXT("...<truncated>");
            }
            J->SetStringField(PName, Text);
        }
        return J;
    }

    // Mark the CO package dirty + refresh the graph UI. The actual persist is the Python NON-VALIDATING save.
    void MCPMut_MarkChanged(UObject* CO, UEdGraph* Graph)
    {
        if (Graph)
        {
            Graph->NotifyGraphChanged();                                  // VERIFY vs engine source (UEdGraph::NotifyGraphChanged)
        }
        if (CO)
        {
            CO->MarkPackageDirty();                                       // VERIFY vs engine source (UObject::MarkPackageDirty)
        }
    }
#endif // WITH_EDITOR

} // namespace

// =====================================================================================================
// READER 1 — GetMutableGraphJson (THE READ; proves Source is reachable). Full walk of the CO Source graph.
// =====================================================================================================
FString UMCPReflectionLibrary::GetMutableGraphJson(const FString& CoPath)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph)
    {
        return MCPMut_Err(Err);
    }

    TArray<TSharedPtr<FJsonValue>> Nodes;
    for (UEdGraphNode* N : Graph->Nodes)
    {
        if (!N) { continue; }
        Nodes.Add(MakeShared<FJsonValueObject>(MCPMut_SerializeNode(N, /*bWithPins*/true, /*bWithLinks*/true)));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("object_path"), CO->GetPathName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_class"), Graph->GetClass()->GetName());
    Root->SetNumberField(TEXT("node_count"), Nodes.Num());
    Root->SetArrayField(TEXT("nodes"), Nodes);
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// READER 2 — GetMutableNodeJson (one node: pins + links + reflected properties)
// =====================================================================================================
FString UMCPReflectionLibrary::GetMutableNodeJson(const FString& CoPath, const FString& NodeGuid)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph)
    {
        return MCPMut_Err(Err);
    }
    UEdGraphNode* Node = MCPMut_FindNode(Graph, NodeGuid, Err);
    if (!Node)
    {
        return MCPMut_Err(Err);
    }

    TSharedRef<FJsonObject> Root = MCPMut_SerializeNode(Node, /*bWithPins*/true, /*bWithLinks*/true);
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetObjectField(TEXT("properties"), MCPMut_SerializeNodeProperties(Node));
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 3 — AddMutableNodeJson. Resolve class by NAME -> NewObject -> base UEdGraphNode lifecycle -> add.
// BeginConstruct / PostBackwardsCompatibleFixup are SKIPPED (concrete-type-only); the CO's AllocateDefaultPins
// override (reached via vtable) builds pins. Inverse: DeleteMutableNodeJson(new_guid).
// =====================================================================================================
FString UMCPReflectionLibrary::AddMutableNodeJson(const FString& CoPath, const FString& NodeClass, float X, float Y)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph)
    {
        return MCPMut_Err(Err);
    }

    UClass* ResolvedClass = MCPMut_ResolveNodeClass(NodeClass);
    if (!ResolvedClass)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not resolve node class '%s' (try the full path e.g. "
            "'/Script/CustomizableObjectEditor.CustomizableObjectNodeObjectGroup')"), *NodeClass));
    }
    if (!ResolvedClass->IsChildOf(UEdGraphNode::StaticClass()))
    {
        return MCPMut_Err(FString::Printf(TEXT("class '%s' is not a UEdGraphNode"), *ResolvedClass->GetName()));
    }
    if (ResolvedClass->HasAnyClassFlags(CLASS_Abstract))
    {
        return MCPMut_Err(FString::Printf(TEXT("class '%s' is abstract (cannot be instantiated)"),
                          *ResolvedClass->GetName()));
    }
    // Best-effort: reject non-CustomizableObject nodes when the base class is name-resolvable (module loaded).
    if (UClass* CONodeBase = FindObject<UClass>(nullptr, TEXT("/Script/CustomizableObjectEditor.CustomizableObjectNode")))
    {
        if (!ResolvedClass->IsChildOf(CONodeBase))
        {
            return MCPMut_Err(FString::Printf(TEXT("class '%s' is not a UCustomizableObjectNode"),
                              *ResolvedClass->GetName()));
        }
    }

    UEdGraphNode* Node = nullptr;
    try
    {
        Graph->Modify();
        Node = NewObject<UEdGraphNode>(Graph, ResolvedClass, NAME_None, RF_Transactional);  // VERIFY vs engine source
        if (!Node)
        {
            return MCPMut_Err(TEXT("NewObject returned null for the node class"));
        }
        Graph->AddNode(Node, /*bFromUI*/false, /*bSelectNewNode*/false);   // VERIFY vs engine source (UEdGraph::AddNode)
        Node->CreateNewGuid();
        Node->NodePosX = (int32)X;
        Node->NodePosY = (int32)Y;
        Node->PostPlacedNewNode();                                         // VERIFY vs engine source (UEdGraphNode virtual)
        Node->AllocateDefaultPins();                                       // VERIFY vs engine source (CO override builds pins via vtable)
        Node->ReconstructNode();                                           // VERIFY vs engine source (idempotent pin rebuild)
    }
    catch (...)
    {
        return MCPMut_Err(TEXT("exception during node construction / AllocateDefaultPins"));
    }

    MCPMut_MarkChanged(CO, Graph);

    TSharedRef<FJsonObject> Root = MCPMut_SerializeNode(Node, /*bWithPins*/true, /*bWithLinks*/false);
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("note"), TEXT("BeginConstruct/PostBackwardsCompatibleFixup skipped (header-free); "
        "pins built by the node's AllocateDefaultPins override."));
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 4 — ConnectMutableNodesJson. Schema->TryCreateConnection; report CanCreateConnection's reason.
// A REJECTED wire returns {"error":...} so the Python side never ledgers a phantom inverse.
// Inverse: DisconnectMutablePinJson(from_guid, from_pin, to_guid, to_pin).
// =====================================================================================================
FString UMCPReflectionLibrary::ConnectMutableNodesJson(const FString& CoPath, const FString& FromGuid,
    const FString& FromPin, const FString& ToGuid, const FString& ToPin)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph) { return MCPMut_Err(Err); }

    UEdGraphNode* FromNode = MCPMut_FindNode(Graph, FromGuid, Err);
    if (!FromNode) { return MCPMut_Err(Err); }
    UEdGraphNode* ToNode = MCPMut_FindNode(Graph, ToGuid, Err);
    if (!ToNode) { return MCPMut_Err(Err); }

    UEdGraphPin* A = MCPMut_FindPin(FromNode, FromPin);
    if (!A) { return MCPMut_Err(FString::Printf(TEXT("from-node has no pin '%s'"), *FromPin)); }
    UEdGraphPin* B = MCPMut_FindPin(ToNode, ToPin);
    if (!B) { return MCPMut_Err(FString::Printf(TEXT("to-node has no pin '%s'"), *ToPin)); }

    const UEdGraphSchema* Schema = Graph->GetSchema();                     // base pointer; dispatches to CO schema
    if (!Schema)
    {
        return MCPMut_Err(TEXT("graph schema unavailable"));
    }

    // Ask WHY first (base virtual -> CO override) for a clear message, then perform the connection.
    // TryCreateConnection may RECONSTRUCT a node (invalidating A/B) — endpoints are addressed by guid+name
    // from Python, so the inverse stays robust.
    const FPinConnectionResponse Resp = Schema->CanCreateConnection(A, B);  // VERIFY vs engine source (UEdGraphSchema virtual)
    bool bConnected = false;
    try
    {
        bConnected = Schema->TryCreateConnection(A, B);                     // VERIFY vs engine source (UEdGraphSchema virtual)
    }
    catch (...)
    {
        return MCPMut_Err(TEXT("exception during TryCreateConnection"));
    }

    if (bConnected)
    {
        MCPMut_MarkChanged(CO, Graph);
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("customizable_object"), CO->GetName());
        Root->SetStringField(TEXT("graph"), Graph->GetName());
        Root->SetBoolField(TEXT("connected"), true);
        Root->SetStringField(TEXT("message"), Resp.Message.ToString());
        return MCPMut_Serialize(Root);
    }

    FString Reason = Resp.Message.ToString();
    if (Reason.IsEmpty()) { Reason = TEXT("incompatible pins/directions"); }
    return MCPMut_Err(FString::Printf(TEXT("TryCreateConnection rejected: %s"), *Reason));
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 5 — DisconnectMutablePinJson. Break one specific link (OtherGuid+OtherPin) or ALL links on the pin.
// Captures broken endpoints for the reconnect inverse.
// =====================================================================================================
FString UMCPReflectionLibrary::DisconnectMutablePinJson(const FString& CoPath, const FString& NodeGuid,
    const FString& PinName, const FString& OtherGuid, const FString& OtherPin)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph) { return MCPMut_Err(Err); }
    UEdGraphNode* Node = MCPMut_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPMut_Err(Err); }
    UEdGraphPin* Pin = MCPMut_FindPin(Node, PinName);
    if (!Pin) { return MCPMut_Err(FString::Printf(TEXT("node has no pin '%s'"), *PinName)); }

    const UEdGraphSchema* Schema = Graph->GetSchema();
    if (!Schema)
    {
        return MCPMut_Err(TEXT("graph schema unavailable"));
    }

    // Snapshot endpoints BEFORE mutating (for the reconnect inverse).
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
    try
    {
        if (bTargeted)
        {
            UEdGraphNode* OtherNode = MCPMut_FindNode(Graph, OtherGuid, Err);
            if (!OtherNode) { return MCPMut_Err(Err); }
            UEdGraphPin* Other = MCPMut_FindPin(OtherNode, OtherPin);
            if (!Other) { return MCPMut_Err(FString::Printf(TEXT("other-node has no pin '%s'"), *OtherPin)); }
            if (!Pin->LinkedTo.Contains(Other))
            {
                return MCPMut_Err(TEXT("the two pins are not linked"));
            }
            RecordLink(Other);
            Schema->BreakSinglePinLink(Pin, Other);                        // VERIFY vs engine source (UEdGraphSchema virtual)
            BrokenCount = 1;
        }
        else
        {
            for (UEdGraphPin* L : Pin->LinkedTo)
            {
                RecordLink(L);
            }
            BrokenCount = Pin->LinkedTo.Num();
            Schema->BreakPinLinks(*Pin, /*bSendsNodeNotification*/true);    // VERIFY vs engine source (UEdGraphSchema virtual)
        }
    }
    catch (...)
    {
        return MCPMut_Err(TEXT("exception during break-link"));
    }

    MCPMut_MarkChanged(CO, Graph);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("pin"), Pin->PinName.ToString());
    Root->SetNumberField(TEXT("broken_count"), BrokenCount);
    Root->SetArrayField(TEXT("broken"), Broken);
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 6 — DeleteMutableNodeJson. Remove a node by GUID; capture a LOSSY re-add spec (class+pos+pin links)
// for the inverse. Mutable input values usually come from constant/parameter NODES (not pin literals), so
// the inverse restores class+position+links; per-node internal state does NOT round-trip.
// =====================================================================================================
FString UMCPReflectionLibrary::DeleteMutableNodeJson(const FString& CoPath, const FString& NodeGuid)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph) { return MCPMut_Err(Err); }
    UEdGraphNode* Node = MCPMut_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPMut_Err(Err); }

    // --- capture a best-effort re-add spec BEFORE removal -------------------------------------------------
    TSharedRef<FJsonObject> Captured = MakeShared<FJsonObject>();
    Captured->SetStringField(TEXT("node_class"), Node->GetClass()->GetPathName());
    Captured->SetNumberField(TEXT("x"), Node->NodePosX);
    Captured->SetNumberField(TEXT("y"), Node->NodePosY);

    TArray<TSharedPtr<FJsonValue>> PinCaps;
    for (UEdGraphPin* P : Node->Pins)
    {
        if (!P) { continue; }
        TSharedRef<FJsonObject> PC = MakeShared<FJsonObject>();
        PC->SetStringField(TEXT("name"), P->PinName.ToString());
        PC->SetStringField(TEXT("direction"), P->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
        PC->SetStringField(TEXT("default_value"), P->DefaultValue);
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

    const FString RemovedGuid = Node->NodeGuid.ToString();
    try
    {
        Graph->Modify();
        Node->Modify();
        // UEdGraph::RemoveNode breaks all links + calls the node's DestroyNode override (pin-data cleanup) via vtable.
        Graph->RemoveNode(Node);                                           // VERIFY vs engine source (UEdGraph::RemoveNode)
    }
    catch (...)
    {
        return MCPMut_Err(TEXT("exception during RemoveNode"));
    }

    MCPMut_MarkChanged(CO, Graph);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), RemovedGuid);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetObjectField(TEXT("captured"), Captured);                     // lossy re-add spec for the Python inverse
    Root->SetBoolField(TEXT("inverse_is_lossy"), true);
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 7 — SetMutableNodePropertyJson. Set an FProperty on the node. Capture prior via ExportText; set via
// ImportText; reconstruct (a property may drive pin shape). Inverse: restore prior via the same handler.
// =====================================================================================================
FString UMCPReflectionLibrary::SetMutableNodePropertyJson(const FString& CoPath, const FString& NodeGuid,
    const FString& PropertyName, const FString& ValueJson)
{
#if WITH_EDITOR
    UObject* CO = MCPMut_LoadCO(CoPath);
    if (!CO)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not load customizable object '%s'"), *CoPath));
    }
    FString Err;
    UEdGraph* Graph = MCPMut_ResolveSource(CO, Err);
    if (!Graph) { return MCPMut_Err(Err); }
    UEdGraphNode* Node = MCPMut_FindNode(Graph, NodeGuid, Err);
    if (!Node) { return MCPMut_Err(Err); }

    FProperty* Prop = Node->GetClass()->FindPropertyByName(FName(*PropertyName));  // VERIFY vs engine source
    if (!Prop)
    {
        return MCPMut_Err(FString::Printf(TEXT("node class '%s' has no property '%s'"),
                          *Node->GetClass()->GetName(), *PropertyName));
    }

    // Capture prior value as text.
    FString PrevText;
    Prop->ExportText_InContainer(0, PrevText, Node, /*Delta*/nullptr, /*Parent*/Node, PPF_None);  // VERIFY vs engine source

    // Apply new value; ImportText_InContainer returns nullptr on parse failure (no crash).
    const TCHAR* ImportResult = nullptr;
    try
    {
        Node->Modify();
        ImportResult = Prop->ImportText_InContainer(*ValueJson, Node, /*OwnerObject*/Node, PPF_None);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPMut_Err(TEXT("exception during ImportText"));
    }
    if (ImportResult == nullptr)
    {
        return MCPMut_Err(FString::Printf(TEXT("could not parse value '%s' for property '%s'"),
                          *ValueJson, *PropertyName));
    }

    // A property may drive pin shape; reconstruct so pins stay consistent (guarded).
    try { Node->ReconstructNode(); } catch (...) { /* value is set; pins best-effort */ }

    MCPMut_MarkChanged(CO, Graph);

    // Re-read the actual value post-set.
    FString NewText;
    Prop->ExportText_InContainer(0, NewText, Node, /*Delta*/nullptr, /*Parent*/Node, PPF_None);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("customizable_object"), CO->GetName());
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("prev_value"), PrevText);
    Root->SetStringField(TEXT("new_value"), NewText);
    return MCPMut_Serialize(Root);
#else
    return MCPMut_Err(TEXT("editor-only"));
#endif
}
