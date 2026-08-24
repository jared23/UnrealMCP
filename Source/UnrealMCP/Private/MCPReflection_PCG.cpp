// UnrealMCP — PCG GRAPH-PARAMETER SCHEMA + DYNAMIC-INPUT-PIN authoring (C++ DRAFT 2026-08-19, PCG Wave 5).
//
// The FINAL PCG slice: the two things Python CANNOT reach on a UPCGGraph asset.
//   (A) user-parameter SCHEMA authoring. UPCGGraph.UserParameters is a reflected FInstancedPropertyBag, but
//       unreal.InstancedPropertyBag exposes only generic struct methods (import_text/export_text/to_dict) —
//       NO add/remove/rename-PROPERTY surface. Schema mutation is C++ only. Wave 3's PCGGraphParametersHelpers
//       already covers typed VALUE get/set on the same bag, so this file does SCHEMA only (never duplicates values).
//   (B) dynamic-input-pin editing. UPCGSettingsWithDynamicInputs::OnUserAdd/RemoveDynamicInputPin are
//       WITH_EDITOR PCG_API methods with NO reflected (BlueprintCallable) surface and no Python binding.
//
// Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations (block #48) live in
// MCPReflectionLibrary.h. Seven handlers (all FString-only across the .h boundary — no PropertyBag/PCG type ever
// crosses into the header):
//
//   SCHEMA READERS (no mutation, no ledger):
//     1) ListPCGGraphParametersJson  — enumerate the graph's user_parameters bag descs {name,type,value_type_object,
//                                       container,id}. Python CANNOT enumerate the bag, so this is C++.
//     2) GetPCGGraphParameterJson    — one desc + its best-effort serialized default value.
//
//   SCHEMA WRITERS (ledger; the Python caller folds each inverse into editor_level.undo, then non-validating-saves):
//     3) AddPCGGraphParameterJson    — UPCGGraph::AddUserParameters({FPropertyBagPropertyDesc(name,type,obj)}).
//     4) RemovePCGGraphParameterJson — UPCGGraph::UpdateUserParametersStruct(Bag -> Bag.RemovePropertyByName(name)).
//     5) RenamePCGGraphParameterJson — UPCGGraphInterface::RenameUserParameter(old,new) (WITH_EDITOR).
//
//   DYNAMIC-INPUT-PIN WRITERS (ledger; WITH_EDITOR on the node's settings):
//     6) AddPCGDynamicInputPinJson    — Settings->OnUserAddDynamicInputPin() (adds one default source pin).
//     7) RemovePCGDynamicInputPinJson — Settings->OnUserRemoveDynamicInputPin(Node, AbsPinIndex), guarded by
//                                       CanUserRemoveDynamicInputPin FIRST (the engine method has check()s that
//                                       would CRASH the editor on a bad index — this guard is load-bearing).
//
// LINKAGE (all external-plugin-linkable — see the coordinator report):
//   * UPCGGraph::AddUserParameters / UpdateUserParametersStruct : PCG_API (PCGGraph.cpp:2810 / :2670), NOT
//     WITH_EDITOR-gated. Neither calls Modify() itself -> we Graph->Modify() + MarkPackageDirty() around them.
//   * UPCGGraphInterface::RenameUserParameter : PCG_API, WITH_EDITOR (PCGGraph.cpp:259) — calls Modify() itself.
//   * UPCGGraph::GetUserParametersStruct : public inline override returning &UserParameters (reads).
//   * FInstancedPropertyBag::{FindPropertyDescByName,GetPropertyBagStruct,RemovePropertyByName,GetValueSerializedString,
//     SanitizePropertyName} + UPropertyBag::GetPropertyDescs (inline) : COREUOBJECT_API (StructUtils/PropertyBag.h,
//     which lives IN CoreUObject in UE 5.8 -> already a public dep, NO extra module).
//   * UPCGSettingsWithDynamicInputs::{OnUserAddDynamicInputPin,OnUserRemoveDynamicInputPin,CanUserRemoveDynamicInputPin,
//     GetStaticInputPinNum} : PCG_API, WITH_EDITOR (GetDynamicInputPinNum is inline). Class is MinimalAPI but its
//     StaticClass() is exported (Cast<> works) and each method carries UE_API=PCG_API.
//   * UPCGNode::{GetSettings,GetName} + UPCGGraph::{GetNodes,FindNodeByTitleName} : PCG_API.
//   Build.cs += "PCG" (the RUNTIME module — all of UPCGGraph/UPCGNode/UPCGSettingsWithDynamicInputs live here,
//   PCG_API-exported) -> NO engine export patch. PCG's PUBLIC deps (ComputeFramework/Foliage/Landscape/Geometry*)
//   bring every transitive include of PCGGraph.h; the one editor include (EdGraphNode_Comment.h) resolves via the
//   plugin's existing UnrealEd dep.
//
// CRASH-SAFETY: every load/desc/node/settings lookup is null-guarded; RemovePCGDynamicInputPinJson gates on
// CanUserRemoveDynamicInputPin BEFORE the check()-bearing OnUserRemoveDynamicInputPin; re-entrant engine calls are
// wrapped in try/catch. Any miss returns {"error":...}. All handlers are #if WITH_EDITOR guarded (cooked build
// returns {"error":"editor-only"}).
//
// PERSISTENCE: handlers mutate the loaded graph's in-memory bag / node pins and mark the package dirty. The Python
// wiring (pcg_schema.py) performs the save after each write and folds the inverse; no PCG generation is triggered.
//
// Anonymous-namespace helpers are prefixed `MCPPcg_` so they stay unique in the module's unity build. Every
// engine-API touch point is tagged "VERIFY vs engine source" for the coordinator's live pass.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "CoreMinimal.h"
#include "UObject/Class.h"             // TBaseStructure / UScriptStruct / UObject::StaticClass
#include "UObject/UObjectGlobals.h"    // LoadObject
#include "Misc/PackageName.h"          // FPackageName::GetShortName (bare-path load fallback)

#include "StructUtils/PropertyBag.h"   // FInstancedPropertyBag / FPropertyBagPropertyDesc / EPropertyBagPropertyType / UPropertyBag

#include "PCGGraph.h"                  // UPCGGraph / UPCGGraphInterface (AddUserParameters / RenameUserParameter / ...)
#include "PCGNode.h"                   // UPCGNode (GetSettings / GetName)
#include "PCGSettings.h"               // UPCGSettings
#include "PCGSettingsWithDynamicInputs.h" // UPCGSettingsWithDynamicInputs (OnUserAdd/RemoveDynamicInputPin / ...)

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPPcg_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error").
    FString MCPPcg_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPPcg_Serialize(Root);
    }

#if WITH_EDITOR
    // Load a UPCGGraph as a typed object. Accepts "/Game/Path/Foo.Foo" or bare "/Game/Path/Foo".
    UPCGGraph* MCPPcg_LoadGraph(const FString& Path, FString& OutErr)
    {
        if (Path.IsEmpty())
        {
            OutErr = TEXT("graph path is empty");
            return nullptr;
        }
        UPCGGraph* Graph = LoadObject<UPCGGraph>(nullptr, *Path);           // VERIFY vs engine source (LoadObject)
        if (!Graph)
        {
            const FString Short = FPackageName::GetShortName(Path);
            const FString Full = FString::Printf(TEXT("%s.%s"), *Path, *Short);
            Graph = LoadObject<UPCGGraph>(nullptr, *Full);
        }
        if (!Graph)
        {
            OutErr = FString::Printf(TEXT("could not load PCGGraph '%s'"), *Path);
        }
        return Graph;
    }

    // Find a node by its UObject name (matches pcg_write.py._pcg_resolve_node's n.get_name()==key), then by title.
    UPCGNode* MCPPcg_FindNode(UPCGGraph* Graph, const FString& NodeName, FString& OutErr)
    {
        if (!Graph)
        {
            OutErr = TEXT("null graph");
            return nullptr;
        }
        for (UPCGNode* N : Graph->GetNodes())                              // VERIFY vs engine source (UPCGGraph::GetNodes)
        {
            if (N && N->GetName() == NodeName)
            {
                return N;
            }
        }
        if (UPCGNode* ByTitle = Graph->FindNodeByTitleName(FName(*NodeName)))  // VERIFY vs engine source (PCG_API)
        {
            return ByTitle;
        }
        OutErr = FString::Printf(TEXT("no node named '%s' in graph (match is on the UPCGNode object name, "
            "then the node title)"), *NodeName);
        return nullptr;
    }

    // Map a friendly type string -> (EPropertyBagPropertyType, ValueTypeObject). Returns false + OutErr on miss.
    bool MCPPcg_ResolveType(const FString& InType, EPropertyBagPropertyType& OutType,
                            const UObject*& OutTypeObject, FString& OutErr)
    {
        OutTypeObject = nullptr;
        const FString T = InType.ToLower();
        if (T == TEXT("bool") || T == TEXT("boolean"))        { OutType = EPropertyBagPropertyType::Bool;   return true; }
        if (T == TEXT("byte") || T == TEXT("uint8"))          { OutType = EPropertyBagPropertyType::Byte;   return true; }
        if (T == TEXT("int") || T == TEXT("int32") || T == TEXT("integer")) { OutType = EPropertyBagPropertyType::Int32; return true; }
        if (T == TEXT("int64"))                               { OutType = EPropertyBagPropertyType::Int64;  return true; }
        if (T == TEXT("float"))                               { OutType = EPropertyBagPropertyType::Float;  return true; }
        if (T == TEXT("double"))                              { OutType = EPropertyBagPropertyType::Double; return true; }
        if (T == TEXT("name"))                                { OutType = EPropertyBagPropertyType::Name;   return true; }
        if (T == TEXT("string"))                              { OutType = EPropertyBagPropertyType::String; return true; }
        if (T == TEXT("text"))                                { OutType = EPropertyBagPropertyType::Text;   return true; }
        if (T == TEXT("vector") || T == TEXT("vector3") || T == TEXT("fvector"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FVector>::Get();      return true; }
        if (T == TEXT("vector2d") || T == TEXT("vector2"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FVector2D>::Get();    return true; }
        if (T == TEXT("rotator"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FRotator>::Get();     return true; }
        if (T == TEXT("transform"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FTransform>::Get();   return true; }
        if (T == TEXT("quat"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FQuat>::Get();        return true; }
        if (T == TEXT("linearcolor") || T == TEXT("color"))
            { OutType = EPropertyBagPropertyType::Struct; OutTypeObject = TBaseStructure<FLinearColor>::Get(); return true; }
        if (T == TEXT("object"))     { OutType = EPropertyBagPropertyType::Object;     OutTypeObject = UObject::StaticClass(); return true; }
        if (T == TEXT("softobject")) { OutType = EPropertyBagPropertyType::SoftObject; OutTypeObject = UObject::StaticClass(); return true; }
        if (T == TEXT("class"))      { OutType = EPropertyBagPropertyType::Class;      OutTypeObject = UObject::StaticClass(); return true; }
        if (T == TEXT("softclass"))  { OutType = EPropertyBagPropertyType::SoftClass;  OutTypeObject = UObject::StaticClass(); return true; }
        OutErr = FString::Printf(TEXT("unsupported parameter type '%s' (valid: bool, byte, int32, int64, float, "
            "double, name, string, text, vector, vector2d, rotator, transform, quat, linearcolor, object, "
            "softobject, class, softclass)"), *InType);
        return false;
    }

    // EPropertyBagPropertyType -> friendly string (inverse of MCPPcg_ResolveType, for readback).
    FString MCPPcg_TypeName(EPropertyBagPropertyType T)
    {
        switch (T)
        {
        case EPropertyBagPropertyType::Bool:       return TEXT("bool");
        case EPropertyBagPropertyType::Byte:       return TEXT("byte");
        case EPropertyBagPropertyType::Int32:      return TEXT("int32");
        case EPropertyBagPropertyType::Int64:      return TEXT("int64");
        case EPropertyBagPropertyType::Float:      return TEXT("float");
        case EPropertyBagPropertyType::Double:     return TEXT("double");
        case EPropertyBagPropertyType::Name:       return TEXT("name");
        case EPropertyBagPropertyType::String:     return TEXT("string");
        case EPropertyBagPropertyType::Text:       return TEXT("text");
        case EPropertyBagPropertyType::Enum:       return TEXT("enum");
        case EPropertyBagPropertyType::Struct:     return TEXT("struct");
        case EPropertyBagPropertyType::Object:     return TEXT("object");
        case EPropertyBagPropertyType::SoftObject: return TEXT("softobject");
        case EPropertyBagPropertyType::Class:      return TEXT("class");
        case EPropertyBagPropertyType::SoftClass:  return TEXT("softclass");
        case EPropertyBagPropertyType::Int8:       return TEXT("int8");
        case EPropertyBagPropertyType::Int16:      return TEXT("int16");
        case EPropertyBagPropertyType::UInt16:     return TEXT("uint16");
        case EPropertyBagPropertyType::UInt32:     return TEXT("uint32");
        case EPropertyBagPropertyType::UInt64:     return TEXT("uint64");
        default:                                   return TEXT("none");
        }
    }

    // EPropertyBagAlterationResult -> string (for error reporting).
    FString MCPPcg_AlterResultName(EPropertyBagAlterationResult R)
    {
        switch (R)
        {
        case EPropertyBagAlterationResult::Success:                       return TEXT("Success");
        case EPropertyBagAlterationResult::InternalError:                 return TEXT("InternalError");
        case EPropertyBagAlterationResult::PropertyNameEmpty:             return TEXT("PropertyNameEmpty");
        case EPropertyBagAlterationResult::PropertyNameInvalidCharacters: return TEXT("PropertyNameInvalidCharacters");
        case EPropertyBagAlterationResult::SourcePropertyNotFound:        return TEXT("SourcePropertyNotFound");
        case EPropertyBagAlterationResult::TargetPropertyNotFound:        return TEXT("TargetPropertyNotFound");
        case EPropertyBagAlterationResult::TargetPropertyAlreadyExists:   return TEXT("TargetPropertyAlreadyExists");
        case EPropertyBagAlterationResult::PropertyKeyTypeNotHashable:    return TEXT("PropertyKeyTypeNotHashable");
        default:                                                          return TEXT("Unknown");
        }
    }

    // One FPropertyBagPropertyDesc -> JSON {name,type,value_type_object?,value_type_object_name?,container,id}.
    TSharedRef<FJsonObject> MCPPcg_SerializeDesc(const FPropertyBagPropertyDesc& D)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), D.Name.ToString());
        J->SetStringField(TEXT("type"), MCPPcg_TypeName(D.ValueType));     // VERIFY vs engine source (FPropertyBagPropertyDesc::ValueType)
        if (const UObject* VTO = D.ValueTypeObject.Get())                  // TObjectPtr<const UObject> — .Get() is safe
        {
            J->SetStringField(TEXT("value_type_object"), VTO->GetPathName());
            J->SetStringField(TEXT("value_type_object_name"), VTO->GetName());
        }
        const EPropertyBagContainerType C = D.ContainerTypes.GetFirstContainerType();  // VERIFY vs engine source
        const TCHAR* Cont =
            C == EPropertyBagContainerType::Array ? TEXT("array") :
            C == EPropertyBagContainerType::Set   ? TEXT("set")   :
            C == EPropertyBagContainerType::Map   ? TEXT("map")   : TEXT("none");
        J->SetStringField(TEXT("container"), Cont);
        J->SetStringField(TEXT("id"), D.ID.ToString());
        return J;
    }

    // Enumerate all descs on a graph's user_parameters bag (empty array if the bag is unauthored/invalid).
    TArray<TSharedPtr<FJsonValue>> MCPPcg_EnumerateParams(UPCGGraph* Graph)
    {
        TArray<TSharedPtr<FJsonValue>> Out;
        if (!Graph) { return Out; }
        const FInstancedPropertyBag* Bag = Graph->GetUserParametersStruct();  // VERIFY vs engine source (public inline)
        if (!Bag || !Bag->IsValid()) { return Out; }
        if (const UPropertyBag* Struct = Bag->GetPropertyBagStruct())         // VERIFY vs engine source (COREUOBJECT_API)
        {
            for (const FPropertyBagPropertyDesc& D : Struct->GetPropertyDescs())  // VERIFY vs engine source (inline)
            {
                Out.Add(MakeShared<FJsonValueObject>(MCPPcg_SerializeDesc(D)));
            }
        }
        return Out;
    }
#endif // WITH_EDITOR

} // namespace

// =====================================================================================================
// READER 1 — ListPCGGraphParametersJson. Enumerate the graph's user_parameters bag descs (Python can't).
// =====================================================================================================
FString UMCPReflectionLibrary::ListPCGGraphParametersJson(const FString& GraphPath)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }

    TArray<TSharedPtr<FJsonValue>> Params = MCPPcg_EnumerateParams(Graph);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetNumberField(TEXT("parameter_count"), Params.Num());
    Root->SetArrayField(TEXT("parameters"), Params);
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// READER 2 — GetPCGGraphParameterJson. One desc + its best-effort serialized default value.
// =====================================================================================================
FString UMCPReflectionLibrary::GetPCGGraphParameterJson(const FString& GraphPath, const FString& Name)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }

    const FInstancedPropertyBag* Bag = Graph->GetUserParametersStruct();
    const FPropertyBagPropertyDesc* Desc = (Bag && Bag->IsValid()) ? Bag->FindPropertyDescByName(FName(*Name)) : nullptr;
    if (!Desc)
    {
        return MCPPcg_Err(FString::Printf(TEXT("graph has no user parameter named '%s'"), *Name));
    }

    TSharedRef<FJsonObject> Root = MCPPcg_SerializeDesc(*Desc);
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    // Best-effort serialized default value (never asserts; absent if the type has no string form).
    if (Bag)
    {
        TValueOrError<FString, EPropertyBagResult> V = Bag->GetValueSerializedString(FName(*Name));  // VERIFY vs engine source
        if (V.IsValid())
        {
            Root->SetStringField(TEXT("value_serialized"), V.GetValue());
        }
    }
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 3 — AddPCGGraphParameterJson. UPCGGraph::AddUserParameters({desc}). Inverse: RemovePCGGraphParameterJson(name).
// The bag sanitizes the property name; the response reports the ACTUAL (sanitized) name so the ledger targets it.
// =====================================================================================================
FString UMCPReflectionLibrary::AddPCGGraphParameterJson(const FString& GraphPath, const FString& Name,
    const FString& Type)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }

    if (Name.IsEmpty())
    {
        return MCPPcg_Err(TEXT("parameter name is empty"));
    }

    EPropertyBagPropertyType ValueType = EPropertyBagPropertyType::None;
    const UObject* ValueTypeObject = nullptr;
    if (!MCPPcg_ResolveType(Type, ValueType, ValueTypeObject, Err))
    {
        return MCPPcg_Err(Err);
    }

    // The bag would REPLACE an existing property of the same name (AddProperties bOverwrite=true) — refuse that so
    // the add stays a pure inverse of remove and never silently changes a param's type.
    const FName SanitizedName = FInstancedPropertyBag::SanitizePropertyName(Name);  // VERIFY vs engine source (static, COREUOBJECT_API)
    const FInstancedPropertyBag* PreBag = Graph->GetUserParametersStruct();
    if (PreBag && PreBag->IsValid() && PreBag->FindPropertyDescByName(SanitizedName))
    {
        return MCPPcg_Err(FString::Printf(TEXT("graph already has a user parameter named '%s' "
            "(remove it first, or rename)"), *SanitizedName.ToString()));
    }

    EPropertyBagAlterationResult Result = EPropertyBagAlterationResult::InternalError;
    try
    {
        Graph->Modify();
        TArray<FPropertyBagPropertyDesc> Descs;
        Descs.Add(FPropertyBagPropertyDesc(SanitizedName, ValueType, ValueTypeObject));  // VERIFY vs engine source (desc ctor)
        Result = Graph->AddUserParameters(Descs);                          // VERIFY vs engine source (PCG_API)
    }
    catch (...)
    {
        return MCPPcg_Err(TEXT("exception during AddUserParameters"));
    }
    if (Result != EPropertyBagAlterationResult::Success)
    {
        return MCPPcg_Err(FString::Printf(TEXT("AddUserParameters failed: %s"), *MCPPcg_AlterResultName(Result)));
    }
    Graph->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetStringField(TEXT("requested_name"), Name);
    Root->SetStringField(TEXT("name"), SanitizedName.ToString());
    Root->SetStringField(TEXT("type"), MCPPcg_TypeName(ValueType));
    Root->SetBoolField(TEXT("added"), true);
    Root->SetArrayField(TEXT("parameters"), MCPPcg_EnumerateParams(Graph));
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 4 — RemovePCGGraphParameterJson. UpdateUserParametersStruct(Bag -> Bag.RemovePropertyByName(name)).
// Captures {name,type,value_type_object,container,value_serialized} BEFORE removal for the re-add inverse.
// =====================================================================================================
FString UMCPReflectionLibrary::RemovePCGGraphParameterJson(const FString& GraphPath, const FString& Name)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }

    const FInstancedPropertyBag* Bag = Graph->GetUserParametersStruct();
    const FPropertyBagPropertyDesc* Desc = (Bag && Bag->IsValid()) ? Bag->FindPropertyDescByName(FName(*Name)) : nullptr;
    if (!Desc)
    {
        return MCPPcg_Err(FString::Printf(TEXT("graph has no user parameter named '%s'"), *Name));
    }

    // --- capture the re-add spec BEFORE removal (the bag pointer is invalidated by the mutation) ---------
    TSharedRef<FJsonObject> Captured = MCPPcg_SerializeDesc(*Desc);
    {
        TValueOrError<FString, EPropertyBagResult> V = Bag->GetValueSerializedString(FName(*Name));  // VERIFY vs engine source
        if (V.IsValid())
        {
            Captured->SetStringField(TEXT("value_serialized"), V.GetValue());
        }
    }

    EPropertyBagAlterationResult Result = EPropertyBagAlterationResult::InternalError;
    try
    {
        Graph->Modify();
        // UpdateUserParametersStruct mutates the graph's own bag then fires the proper GraphPostLoad refresh.
        Graph->UpdateUserParametersStruct([&Result, &Name](FInstancedPropertyBag& MutBag)  // VERIFY vs engine source (PCG_API)
        {
            Result = MutBag.RemovePropertyByName(FName(*Name));            // VERIFY vs engine source (COREUOBJECT_API)
        });
    }
    catch (...)
    {
        return MCPPcg_Err(TEXT("exception during UpdateUserParametersStruct/RemovePropertyByName"));
    }
    if (Result != EPropertyBagAlterationResult::Success)
    {
        return MCPPcg_Err(FString::Printf(TEXT("RemovePropertyByName failed: %s"), *MCPPcg_AlterResultName(Result)));
    }
    Graph->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetStringField(TEXT("name"), Name);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetObjectField(TEXT("captured"), Captured);                     // re-add spec for the Python inverse
    // Schema round-trips exactly; the stored DEFAULT VALUE is only restorable via the Wave-3 typed setter.
    Root->SetBoolField(TEXT("value_restore_is_lossy"), true);
    Root->SetArrayField(TEXT("parameters"), MCPPcg_EnumerateParams(Graph));
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 5 — RenamePCGGraphParameterJson. UPCGGraphInterface::RenameUserParameter (WITH_EDITOR; calls Modify()).
// Inverse: rename NewName -> OldName. LOSSLESS.
// =====================================================================================================
FString UMCPReflectionLibrary::RenamePCGGraphParameterJson(const FString& GraphPath, const FString& OldName,
    const FString& NewName)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }

    if (OldName.IsEmpty() || NewName.IsEmpty())
    {
        return MCPPcg_Err(TEXT("old_name and new_name are both required"));
    }

    const FName SanitizedNew = FInstancedPropertyBag::SanitizePropertyName(NewName);
    const FInstancedPropertyBag* Bag = Graph->GetUserParametersStruct();
    if (!Bag || !Bag->IsValid() || !Bag->FindPropertyDescByName(FName(*OldName)))
    {
        return MCPPcg_Err(FString::Printf(TEXT("graph has no user parameter named '%s'"), *OldName));
    }
    if (Bag->FindPropertyDescByName(SanitizedNew))
    {
        return MCPPcg_Err(FString::Printf(TEXT("graph already has a user parameter named '%s'"),
                          *SanitizedNew.ToString()));
    }

    EPropertyBagAlterationResult Result = EPropertyBagAlterationResult::InternalError;
    try
    {
        Result = Graph->RenameUserParameter(FName(*OldName), SanitizedNew);  // VERIFY vs engine source (PCG_API, WITH_EDITOR; Modify() internal)
    }
    catch (...)
    {
        return MCPPcg_Err(TEXT("exception during RenameUserParameter"));
    }
    if (Result != EPropertyBagAlterationResult::Success)
    {
        return MCPPcg_Err(FString::Printf(TEXT("RenameUserParameter failed: %s"), *MCPPcg_AlterResultName(Result)));
    }
    Graph->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetStringField(TEXT("old_name"), OldName);
    Root->SetStringField(TEXT("requested_new_name"), NewName);
    Root->SetStringField(TEXT("new_name"), SanitizedNew.ToString());
    Root->SetBoolField(TEXT("renamed"), true);
    Root->SetArrayField(TEXT("parameters"), MCPPcg_EnumerateParams(Graph));
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 6 — AddPCGDynamicInputPinJson. Settings->OnUserAddDynamicInputPin() adds one default source pin (the
// concrete override's AddDefaultDynamicInputPin, reached via vtable) and broadcasts OnSettingsChanged, which
// reconstructs the node's pins. Inverse: RemovePCGDynamicInputPinJson(node, new_pin_index).
// =====================================================================================================
FString UMCPReflectionLibrary::AddPCGDynamicInputPinJson(const FString& GraphPath, const FString& NodeName)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }
    UPCGNode* Node = MCPPcg_FindNode(Graph, NodeName, Err);
    if (!Node) { return MCPPcg_Err(Err); }

    UPCGSettings* Settings = Node->GetSettings();                          // VERIFY vs engine source (PCG_API)
    UPCGSettingsWithDynamicInputs* DynSettings = Cast<UPCGSettingsWithDynamicInputs>(Settings);
    if (!DynSettings)
    {
        return MCPPcg_Err(FString::Printf(TEXT("node '%s' is not a dynamic-input node (settings class '%s' is not "
            "a UPCGSettingsWithDynamicInputs)"), *NodeName, Settings ? *Settings->GetClass()->GetName() : TEXT("<none>")));
    }

    const int32 StaticNum  = DynSettings->GetStaticInputPinNum();          // VERIFY vs engine source (PCG_API)
    const int32 DynBefore  = DynSettings->GetDynamicInputPinNum();         // VERIFY vs engine source (inline)
    try
    {
        Graph->Modify();
        Node->Modify();
        DynSettings->Modify();
        DynSettings->OnUserAddDynamicInputPin();                          // VERIFY vs engine source (PCG_API, WITH_EDITOR)
    }
    catch (...)
    {
        return MCPPcg_Err(TEXT("exception during OnUserAddDynamicInputPin"));
    }
    const int32 DynAfter = DynSettings->GetDynamicInputPinNum();
    Graph->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetStringField(TEXT("node_name"), Node->GetName());
    Root->SetStringField(TEXT("settings_class"), DynSettings->GetClass()->GetName());
    Root->SetNumberField(TEXT("static_input_pins"), StaticNum);
    Root->SetNumberField(TEXT("dynamic_before"), DynBefore);
    Root->SetNumberField(TEXT("dynamic_after"), DynAfter);
    // Absolute input-pin index of the newly added pin (what RemovePCGDynamicInputPinJson expects). -1 if the add
    // was a no-op (e.g. CustomPropertiesAreValid rejected it).
    Root->SetNumberField(TEXT("new_pin_index"), DynAfter > DynBefore ? (StaticNum + DynAfter - 1) : -1);
    Root->SetBoolField(TEXT("added"), DynAfter > DynBefore);
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// WRITE 7 — RemovePCGDynamicInputPinJson. Settings->OnUserRemoveDynamicInputPin(Node, AbsPinIndex).
// GUARDED by CanUserRemoveDynamicInputPin FIRST: OnUserRemoveDynamicInputPin has check() asserts on the index
// AND on GetInputPin(label) that would CRASH the editor otherwise. PinIndex < 0 => the LAST dynamic pin.
// Inverse: AddPCGDynamicInputPinJson(node) (re-adds a DEFAULT pin — LOSSY vs a custom-configured pin).
// =====================================================================================================
FString UMCPReflectionLibrary::RemovePCGDynamicInputPinJson(const FString& GraphPath, const FString& NodeName,
    int32 PinIndex)
{
#if WITH_EDITOR
    FString Err;
    UPCGGraph* Graph = MCPPcg_LoadGraph(GraphPath, Err);
    if (!Graph) { return MCPPcg_Err(Err); }
    UPCGNode* Node = MCPPcg_FindNode(Graph, NodeName, Err);
    if (!Node) { return MCPPcg_Err(Err); }

    UPCGSettings* Settings = Node->GetSettings();
    UPCGSettingsWithDynamicInputs* DynSettings = Cast<UPCGSettingsWithDynamicInputs>(Settings);
    if (!DynSettings)
    {
        return MCPPcg_Err(FString::Printf(TEXT("node '%s' is not a dynamic-input node (settings class '%s' is not "
            "a UPCGSettingsWithDynamicInputs)"), *NodeName, Settings ? *Settings->GetClass()->GetName() : TEXT("<none>")));
    }

    const int32 StaticNum = DynSettings->GetStaticInputPinNum();
    const int32 DynBefore = DynSettings->GetDynamicInputPinNum();
    if (DynBefore <= 0)
    {
        return MCPPcg_Err(FString::Printf(TEXT("node '%s' has no dynamic input pins to remove"), *NodeName));
    }
    // PinIndex is the ABSOLUTE input-pin index (static + dynamic). Negative -> the last dynamic pin.
    int32 AbsIndex = PinIndex < 0 ? (StaticNum + DynBefore - 1) : PinIndex;

    // LOAD-BEARING guard: mirrors the check()s inside OnUserRemoveDynamicInputPin. Never remove without this.
    if (!DynSettings->CanUserRemoveDynamicInputPin(AbsIndex))              // VERIFY vs engine source (PCG_API, WITH_EDITOR)
    {
        return MCPPcg_Err(FString::Printf(TEXT("pin index %d is not a removable dynamic input pin "
            "(static=%d, dynamic=%d; the valid ABSOLUTE range is [%d, %d))"),
            AbsIndex, StaticNum, DynBefore, StaticNum, StaticNum + DynBefore));
    }

    // Best-effort capture of the removed pin's label (for the report; the inverse re-adds a default pin).
    FString RemovedLabel;
    {
        const TArray<FName> Labels = DynSettings->GetNodeDefinedPinLabels();  // VERIFY vs engine source (PCG_API)
        if (Labels.IsValidIndex(AbsIndex))
        {
            RemovedLabel = Labels[AbsIndex].ToString();
        }
    }

    try
    {
        Graph->Modify();
        Node->Modify();
        DynSettings->Modify();
        DynSettings->OnUserRemoveDynamicInputPin(Node, AbsIndex);         // VERIFY vs engine source (PCG_API, WITH_EDITOR)
    }
    catch (...)
    {
        return MCPPcg_Err(TEXT("exception during OnUserRemoveDynamicInputPin"));
    }
    const int32 DynAfter = DynSettings->GetDynamicInputPinNum();
    Graph->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("graph"), Graph->GetName());
    Root->SetStringField(TEXT("graph_path"), Graph->GetPathName());
    Root->SetStringField(TEXT("node_name"), Node->GetName());
    Root->SetStringField(TEXT("settings_class"), DynSettings->GetClass()->GetName());
    Root->SetNumberField(TEXT("removed_pin_index"), AbsIndex);
    if (!RemovedLabel.IsEmpty())
    {
        Root->SetStringField(TEXT("removed_pin_label"), RemovedLabel);
    }
    Root->SetNumberField(TEXT("static_input_pins"), StaticNum);
    Root->SetNumberField(TEXT("dynamic_before"), DynBefore);
    Root->SetNumberField(TEXT("dynamic_after"), DynAfter);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetBoolField(TEXT("inverse_is_lossy"), true);
    return MCPPcg_Serialize(Root);
#else
    return MCPPcg_Err(TEXT("editor-only"));
#endif
}
