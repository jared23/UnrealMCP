// UnrealMCP reflection helpers — StateTree editor WRITERS + params/bindings/compile.
//
// This file finishes the "statetree" tool category as GUARDED C++ reflection. It exists BECAUSE the
// Python path for these mutations HARD-CRASHED the shared interpreter: export_text/import_text of a
// nested FInstancedStruct (a StateTree node's Node/Instance) raised EXCEPTION_ACCESS_VIOLATION inside
// python311.dll — uncatchable from Python. Doing the same edits here via FProperty reflection on the
// editor-data object graph avoids the wrapper-marshalling that faulted, and every nested struct /
// instanced-struct / object pointer is null/validity-checked before it is touched.
//
// Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator. Anonymous-namespace helpers are prefixed `MCPStateTree_`
// so they stay unique across the unity build (sibling .cpp files share the concatenated TU).

#include "MCPReflectionLibrary.h"

#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/UObjectHash.h"   // GetObjectsWithOuter
#include "Misc/Guid.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// StructUtils (CoreUObject) — property bag + instanced struct memory access.
#include "StructUtils/PropertyBag.h"          // FInstancedPropertyBag / EPropertyBagPropertyType / EPropertyBag*Result
#include "StructUtils/InstancedStruct.h"      // FInstancedStruct::GetScriptStruct/GetMutableMemory

// StateTree runtime (StateTreeModule — already a Build.cs dep).
#include "StateTree.h"                         // UStateTree
#include "StateTreeReference.h"                // FStateTreeReference (StateTreeComponent.StateTreeRef target) — VERIFY path

// StateTree editor data graph (StateTreeEditorModule — already a Build.cs dep from C++ #14).
#include "StateTreeEditorData.h"               // UStateTreeEditorData (Evaluators/GlobalTasks/EditorBindings/Colors/SubTrees + RootParameterPropertyBag)
#include "StateTreeState.h"                    // UStateTreeState + FStateTreeTransition
#include "StateTreeEditorNode.h"               // FStateTreeEditorNode (Node/Instance/InstanceObject/ID + ReallocInstanceData)
#include "StateTreeNodeBase.h"                 // FStateTreeNodeBase::GetInstanceDataType() (C++ #20 node repair)
#include "StateTreeEditorTypes.h"              // FStateTreeEditorColor / FStateTreeEditorColorRef
#include "StateTreeEditorPropertyBindings.h"   // FStateTreeEditorPropertyBindings (EditorBindings) — AddTaskCompletionBinding, GetNumBindings
#include "PropertyBindingPath.h"               // FPropertyBindingPath::SetStructID/FromString (PropertyBindingUtils — already a dep)
#include "PropertyBindingBindableStructDescriptor.h" // FPropertyBindingBindableStructDescriptor (Name/ID/Struct) for GetBindableStructs (C++ #21)
#include "StateTreeDelegate.h"                 // UE::StateTree::ETaskCompletionCondition (Succeeds/Fails/Completes) (C++ #21)

// StateTree compile (StateTreeEditorModule). EditorSubsystem is a PUBLIC dep of StateTreeEditorModule,
// so StateTreeEditingSubsystem.h (which includes EditorSubsystem.h / StateTreeViewModel.h) resolves.
#include "StateTreeEditingSubsystem.h"         // UStateTreeEditingSubsystem::CompileStateTree (static) — VERIFY path
#include "StateTreeCompilerLog.h"              // FStateTreeCompilerLog::ToTokenizedMessages()
#include "Logging/TokenizedMessage.h"          // FTokenizedMessage::ToText/GetSeverity

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPStateTree_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPStateTree_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPStateTree_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPStateTree_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

#if WITH_EDITOR
    // ---- Editor-data resolution (mirrors GetStateTreeBindingsJson, C++ #14) -------------------------
    UStateTreeEditorData* MCPStateTree_ResolveEditorData(UStateTree* StateTree)
    {
        if (!StateTree) { return nullptr; }
        UStateTreeEditorData* ED = Cast<UStateTreeEditorData>(StateTree->EditorData); // VERIFY vs engine source (UStateTree::EditorData)
        if (ED) { return ED; }
        TArray<UObject*> Inners;
        GetObjectsWithOuter(StateTree, Inners, /*bIncludeNestedObjects*/ false);
        for (UObject* O : Inners)
        {
            if (UStateTreeEditorData* Found = Cast<UStateTreeEditorData>(O))
            {
                // MCP self-heal: create_statetree news the editor data as a NAMED subobject but never assigns the
                // real UStateTree::EditorData pointer, leaving the ed unreferenced -> a CollectGarbage between two
                // bridge calls purges it and the NEXT ed-touching handler AVs. Linking the pointer here (on the
                // first handler call, while the ed is still alive) roots the ed so it survives GC + save/reload.
                StateTree->EditorData = Found; // VERIFY vs engine source (UStateTree::EditorData is TObjectPtr<UObject>)
                return Found;
            }
        }
        return nullptr;
    }

    // Depth-first search for a state by (case-insensitive) Name across all subtrees.
    UStateTreeState* MCPStateTree_FindState(UStateTreeEditorData* ED, const FString& StateName)
    {
        if (!ED) { return nullptr; }
        TArray<UStateTreeState*> Stack;
        for (UStateTreeState* Root : ED->SubTrees) { if (Root) { Stack.Add(Root); } }
        while (Stack.Num() > 0)
        {
            UStateTreeState* St = Stack.Pop();
            if (!St) { continue; }
            if (St->Name.ToString().Equals(StateName, ESearchCase::IgnoreCase)) { return St; } // VERIFY vs engine source (UStateTreeState::Name)
            for (UStateTreeState* Child : St->Children) { if (Child) { Stack.Add(Child); } }    // VERIFY vs engine source (UStateTreeState::Children)
        }
        return nullptr;
    }

    // Resolve the FStateTreeEditorNode addressed by (kind,index). Evaluators/GlobalTasks live on the
    // editor data; task/enter_condition/consideration/single_task live on a state.
    FStateTreeEditorNode* MCPStateTree_ResolveNode(UStateTreeEditorData* ED, UStateTreeState* State,
                                                   const FString& Kind, int32 Index, FString& OutErr)
    {
        const FString K = Kind.ToLower();
        TArray<FStateTreeEditorNode>* Arr = nullptr;
        if (K == TEXT("evaluator"))                                            { Arr = ED ? &ED->Evaluators : nullptr; }
        else if (K == TEXT("global_task") || K == TEXT("globaltask"))          { Arr = ED ? &ED->GlobalTasks : nullptr; }
        else if (K == TEXT("task"))                                            { Arr = State ? &State->Tasks : nullptr; }
        else if (K == TEXT("condition") || K == TEXT("enter_condition"))       { Arr = State ? &State->EnterConditions : nullptr; }
        else if (K == TEXT("consideration"))                                   { Arr = State ? &State->Considerations : nullptr; }
        else if (K == TEXT("single_task") || K == TEXT("singletask"))
        {
            if (!State) { OutErr = TEXT("single_task requires a state"); return nullptr; }
            return &State->SingleTask; // index ignored
        }
        else { OutErr = FString::Printf(TEXT("unknown node kind '%s' (task|condition|consideration|evaluator|global_task|single_task)"), *Kind); return nullptr; }

        if (!Arr) { OutErr = FString::Printf(TEXT("node kind '%s' has no container (missing state/editor-data)"), *Kind); return nullptr; }
        if (Index < 0 || Index >= Arr->Num())
        {
            OutErr = FString::Printf(TEXT("node index %d out of range (0..%d) for kind '%s'"), Index, Arr->Num() - 1, *Kind);
            return nullptr;
        }
        return &(*Arr)[Index];
    }

    // Set property `PropName` on the reflected struct/object at (Container, ContainerPtr): capture the
    // prior value as ExportText into OutPrev, then ImportText NewText. All pointers guarded. Returns
    // false with OutErr set on any miss (property-not-found is a distinct, recoverable miss for `auto`).
    bool MCPStateTree_ApplyProp(const UStruct* Container, void* ContainerPtr, const FString& PropName,
                                const FString& NewText, UObject* Owner, FString& OutPrev, FString& OutErr)
    {
        if (!Container || !ContainerPtr) { OutErr = TEXT("null container"); return false; }
        FProperty* Prop = FindFProperty<FProperty>(Container, *PropName);
        if (!Prop) { OutErr = FString::Printf(TEXT("property '%s' not found on %s"), *PropName, *Container->GetName()); return false; }
        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(ContainerPtr);
        if (!ValuePtr) { OutErr = TEXT("null value ptr"); return false; }

        OutPrev.Reset();
        // Capture prior (bounded — the value is a concrete property, no unbounded recursion).
        Prop->ExportTextItem_Direct(OutPrev, ValuePtr, /*Default*/ nullptr, /*Parent*/ Owner, PPF_None); // VERIFY vs engine source: ExportTextItem_Direct(FString&, const void*, const void*, UObject*, int32)
        const TCHAR* Res = Prop->ImportText_Direct(*NewText, ValuePtr, Owner, PPF_None, nullptr);          // VERIFY vs engine source: ImportText_Direct(const TCHAR*, void*, UObject*, int32, FOutputDevice*)
        if (Res == nullptr) { OutErr = FString::Printf(TEXT("ImportText failed for value '%s' on property '%s'"), *NewText, *PropName); return false; }
        return true;
    }

    // Map a friendly type name to a property-bag scalar/struct type. Returns false for unknown types.
    bool MCPStateTree_ParseBagType(const FString& In, EPropertyBagPropertyType& OutType, const UObject*& OutObj)
    {
        OutObj = nullptr;
        const FString T = In.ToLower();
        if (T == TEXT("bool"))                                   { OutType = EPropertyBagPropertyType::Bool;   return true; }
        if (T == TEXT("byte"))                                   { OutType = EPropertyBagPropertyType::Byte;   return true; }
        if (T == TEXT("int") || T == TEXT("int32"))              { OutType = EPropertyBagPropertyType::Int32;  return true; }
        if (T == TEXT("int64"))                                  { OutType = EPropertyBagPropertyType::Int64;  return true; }
        if (T == TEXT("float"))                                  { OutType = EPropertyBagPropertyType::Float;  return true; }
        if (T == TEXT("double"))                                 { OutType = EPropertyBagPropertyType::Double; return true; }
        if (T == TEXT("name"))                                   { OutType = EPropertyBagPropertyType::Name;   return true; }
        if (T == TEXT("string"))                                 { OutType = EPropertyBagPropertyType::String; return true; }
        if (T == TEXT("text"))                                   { OutType = EPropertyBagPropertyType::Text;   return true; }
        if (T == TEXT("vector") || T == TEXT("vector3"))         { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FVector>::Get();       return true; }
        if (T == TEXT("vector2") || T == TEXT("vector2d"))       { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FVector2D>::Get();     return true; }
        if (T == TEXT("vector4"))                                { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FVector4>::Get();       return true; }
        if (T == TEXT("rotator"))                                { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FRotator>::Get();       return true; }
        if (T == TEXT("quat"))                                   { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FQuat>::Get();          return true; }
        if (T == TEXT("transform"))                              { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FTransform>::Get();     return true; }
        if (T == TEXT("linearcolor") || T == TEXT("color"))      { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FLinearColor>::Get();   return true; }
        if (T == TEXT("guid"))                                   { OutType = EPropertyBagPropertyType::Struct; OutObj = TBaseStructure<FGuid>::Get();          return true; }
        return false;
    }

    const TCHAR* MCPStateTree_BagTypeName(EPropertyBagPropertyType T)
    {
        switch (T)
        {
            case EPropertyBagPropertyType::Bool:   return TEXT("bool");
            case EPropertyBagPropertyType::Byte:   return TEXT("byte");
            case EPropertyBagPropertyType::Int32:  return TEXT("int32");
            case EPropertyBagPropertyType::Int64:  return TEXT("int64");
            case EPropertyBagPropertyType::Float:  return TEXT("float");
            case EPropertyBagPropertyType::Double: return TEXT("double");
            case EPropertyBagPropertyType::Name:   return TEXT("name");
            case EPropertyBagPropertyType::String: return TEXT("string");
            case EPropertyBagPropertyType::Text:   return TEXT("text");
            case EPropertyBagPropertyType::Enum:   return TEXT("enum");
            case EPropertyBagPropertyType::Struct: return TEXT("struct");
            case EPropertyBagPropertyType::Object: return TEXT("object");
            case EPropertyBagPropertyType::Class:  return TEXT("class");
            default:                               return TEXT("other");
        }
    }

    // Reach the (private) RootParameterPropertyBag UPROPERTY by reflection — reflection ignores C++
    // access, so no export symbol or const_cast is referenced.
    FInstancedPropertyBag* MCPStateTree_MutableRootBag(UStateTreeEditorData* ED)
    {
        if (!ED) { return nullptr; }
        FStructProperty* BagProp = FindFProperty<FStructProperty>(UStateTreeEditorData::StaticClass(), TEXT("RootParameterPropertyBag")); // VERIFY vs engine source (member name)
        if (!BagProp) { return nullptr; }
        return BagProp->ContainerPtrToValuePtr<FInstancedPropertyBag>(ED);
    }

    // THE FIX (C++ #20 2026-08-18): after ANY editor-data mutation, refresh the editor property bindings the
    // way the StateTree editor does after every edit (see UStateTreeEditorData::PostLoad, which calls exactly
    // these two). Root cause of the delayed hard-crashes: our raw handler mutations left EditorBindings stale
    // AND left newly-generated structs (notably the RootParameterPropertyBag's dynamic UPropertyBag
    // UScriptStruct + changed node instance structs) OUT of the binding-dependency cache that UpdateBindings
    // builds via EditorBindings.CacheDependency(). GC then freed those structs and a later binding-resolve
    // (validation-on-save = fault #1, or compile / parameter read = fault #2, reading the "full" string as a
    // pointer) dereferenced freed memory -> EXCEPTION_ACCESS_VIOLATION. Calling these keeps the cache + bindings
    // consistent so nothing dangles. Both are UE_API (exported) on UStateTreeEditorData.
    void MCPStateTree_RefreshBindings(UStateTreeEditorData* ED)
    {
        if (!ED) { return; }
        ED->RemoveInvalidBindings(); // drop bindings whose target/source struct no longer exists (VERIFY vs engine source)
        ED->UpdateBindings();        // re-resolve binding paths + re-cache dependent structs (roots them vs GC) (VERIFY vs engine source)
    }

    const TCHAR* MCPStateTree_SeverityName(int32 Sev)
    {
        switch (Sev)
        {
            case (int32)EMessageSeverity::Error:             return TEXT("Error");
            case (int32)EMessageSeverity::PerformanceWarning:return TEXT("PerformanceWarning");
            case (int32)EMessageSeverity::Warning:           return TEXT("Warning");
            case (int32)EMessageSeverity::Info:              return TEXT("Info");
            default:                                         return TEXT("Message");
        }
    }
#endif // WITH_EDITOR
}

// ============================================================================================
// C++ #18 2026-08-18 — StateTree editor WRITERS (crash-fix) + params/bindings/compile.
// REPLACES the Python statetree_write2 writers that AV'd on nested-instanced-struct import/export.
// NO Build.cs change: StateTreeModule / StateTreeEditorModule / PropertyBindingUtils are already deps.
// ============================================================================================

// ---- (0) Ensure the editor data is linked via the REAL UStateTree::EditorData pointer -----------
// GATING FIX (C++ #19 2026-08-18): create_statetree news the ed as a NAMED subobject but never assigns
// StateTree->EditorData, leaving it unreferenced -> a GC between two bridge calls purges it and the next
// ed-touching handler AVs (proven: set_statetree_color undo crashed the editor). create_statetree calls
// this at the END of creation (ed still alive, pre-GC) to root the ed permanently. Idempotent + safe to
// call on any tree (resolves/creates + links). MCPStateTree_ResolveEditorData now also self-heals the link.
FString UMCPReflectionLibrary::EnsureStateTreeEditorData(UStateTree* StateTree)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    const bool bWasLinked = (StateTree->EditorData != nullptr);
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree); // self-heals the pointer if found via fallback
    bool bCreated = false;
    if (!ED)
    {
        // No editor data exists yet — create one and link it. VERIFY vs engine source (UStateTreeEditorData outered to UStateTree).
        ED = NewObject<UStateTreeEditorData>(StateTree, FName(TEXT("StateTreeEditorData")), RF_Transactional);
        StateTree->EditorData = ED;
        bCreated = true;
    }
    if (StateTree->EditorData)
    {
        StateTree->EditorData->SetFlags(RF_Transactional);
        StateTree->MarkPackageDirty();
    }
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetBoolField(TEXT("was_linked"), bWasLinked);
    Root->SetBoolField(TEXT("linked"), StateTree->EditorData != nullptr);
    Root->SetBoolField(TEXT("created"), bCreated);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (1) Node property edit — the crash-fix ------------------------------------------------------
FString UMCPReflectionLibrary::SetStateTreeNodePropertyJson(UStateTree* StateTree, const FString& StateName,
    const FString& NodeKind, int32 NodeIndex, const FString& PropertyName, const FString& NewValueText, const FString& Container)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    if (PropertyName.IsEmpty()) { return MCPStateTree_Error(TEXT("property name is required")); }

    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    const FString K = NodeKind.ToLower();
    const bool bNeedsState = !(K == TEXT("evaluator") || K == TEXT("global_task") || K == TEXT("globaltask"));
    UStateTreeState* State = nullptr;
    if (bNeedsState)
    {
        State = MCPStateTree_FindState(ED, StateName);
        if (!State) { return MCPStateTree_Error(FString::Printf(TEXT("state '%s' not found"), *StateName)); }
    }

    FString ResolveErr;
    FStateTreeEditorNode* Node = MCPStateTree_ResolveNode(ED, State, NodeKind, NodeIndex, ResolveErr);
    if (!Node) { return MCPStateTree_Error(ResolveErr); }

    // Candidate containers, honoring an explicit Container or trying node -> instance -> instance_object for "auto".
    const FString Which = Container.IsEmpty() ? TEXT("auto") : Container.ToLower();
    struct FCand { const TCHAR* Label; const UStruct* Struct; void* Ptr; UObject* Owner; };
    TArray<FCand> Cands;
    auto AddNode = [&]() { if (Node->Node.GetScriptStruct() && Node->Node.GetMutableMemory()) { Cands.Add({ TEXT("node"), Node->Node.GetScriptStruct(), Node->Node.GetMutableMemory(), ED }); } };
    auto AddInst = [&]() { if (Node->Instance.GetScriptStruct() && Node->Instance.GetMutableMemory()) { Cands.Add({ TEXT("instance"), Node->Instance.GetScriptStruct(), Node->Instance.GetMutableMemory(), ED }); } };
    auto AddObj  = [&]() { if (Node->InstanceObject) { Cands.Add({ TEXT("instance_object"), Node->InstanceObject->GetClass(), Node->InstanceObject, Node->InstanceObject }); } };

    if (Which == TEXT("node"))                  { AddNode(); }
    else if (Which == TEXT("instance"))         { AddInst(); }
    else if (Which == TEXT("instance_object"))  { AddObj(); }
    else if (Which == TEXT("auto"))             { AddNode(); AddInst(); AddObj(); }
    else { return MCPStateTree_Error(FString::Printf(TEXT("unknown container '%s' (auto|node|instance|instance_object)"), *Container)); }

    if (Cands.Num() == 0) { return MCPStateTree_Error(TEXT("node has no readable container (empty Node/Instance/InstanceObject)")); }

    FString Prev, ApplyErr, LastErr;
    for (const FCand& C : Cands)
    {
        FString E;
        if (MCPStateTree_ApplyProp(C.Struct, C.Ptr, PropertyName, NewValueText, C.Owner, Prev, E))
        {
            ED->Modify();
            MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
            StateTree->MarkPackageDirty();
            TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
            Root->SetStringField(TEXT("state"), bNeedsState ? StateName : FString(TEXT("<global>")));
            Root->SetStringField(TEXT("kind"), NodeKind);
            Root->SetNumberField(TEXT("index"), NodeIndex);
            Root->SetStringField(TEXT("property"), PropertyName);
            Root->SetStringField(TEXT("container"), C.Label);
            Root->SetStringField(TEXT("prev"), Prev);
            Root->SetStringField(TEXT("value"), NewValueText);
            return MCPStateTree_SerializeJson(Root);
        }
        LastErr = E;
    }
    return MCPStateTree_Error(FString::Printf(TEXT("could not set '%s' on node (container=%s): %s"), *PropertyName, *Which, *LastErr));
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (2) Transition property edit ----------------------------------------------------------------
FString UMCPReflectionLibrary::SetStateTreeTransitionPropertyJson(UStateTree* StateTree, const FString& StateName,
    int32 TransitionIndex, const FString& PropertyName, const FString& NewValueText)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    if (PropertyName.IsEmpty()) { return MCPStateTree_Error(TEXT("property name is required")); }

    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    UStateTreeState* State = MCPStateTree_FindState(ED, StateName);
    if (!State) { return MCPStateTree_Error(FString::Printf(TEXT("state '%s' not found"), *StateName)); }

    if (TransitionIndex < 0 || TransitionIndex >= State->Transitions.Num())
    {
        return MCPStateTree_Error(FString::Printf(TEXT("transition index %d out of range (0..%d)"), TransitionIndex, State->Transitions.Num() - 1));
    }
    FStateTreeTransition& Transition = State->Transitions[TransitionIndex];

    FString Prev, Err;
    if (!MCPStateTree_ApplyProp(FStateTreeTransition::StaticStruct(), &Transition, PropertyName, NewValueText, ED, Prev, Err))
    {
        return MCPStateTree_Error(Err);
    }

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("state"), StateName);
    Root->SetNumberField(TEXT("index"), TransitionIndex);
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("prev"), Prev);
    Root->SetStringField(TEXT("value"), NewValueText);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (3) Component StateTree reference edit -------------------------------------------------------
// Component is the UStateTreeComponent (actor component or BP SCS template). We locate its
// FStateTreeReference-typed struct property by type (or by PropertyName), capture the prior target
// asset path for undo, then repoint it (SetStateTree syncs the parameter bag) or import a whole ref.
FString UMCPReflectionLibrary::SetStateTreeComponentTreeJson(UObject* Component, const FString& PropertyName,
    UStateTree* NewStateTree, const FString& NewRefText)
{
#if WITH_EDITOR
    if (!Component) { return MCPStateTree_Error(TEXT("null component")); }

    UClass* Cls = Component->GetClass();
    FStructProperty* RefProp = nullptr;
    if (!PropertyName.IsEmpty())
    {
        RefProp = FindFProperty<FStructProperty>(Cls, *PropertyName);
        if (!RefProp) { return MCPStateTree_Error(FString::Printf(TEXT("struct property '%s' not found on %s"), *PropertyName, *Cls->GetName())); }
        if (RefProp->Struct != FStateTreeReference::StaticStruct())
        {
            return MCPStateTree_Error(FString::Printf(TEXT("property '%s' is not an FStateTreeReference"), *PropertyName));
        }
    }
    else
    {
        for (TFieldIterator<FStructProperty> It(Cls); It; ++It)
        {
            if (It->Struct == FStateTreeReference::StaticStruct()) { RefProp = *It; break; }
        }
        if (!RefProp) { return MCPStateTree_Error(TEXT("no FStateTreeReference property found on component (pass PropertyName)")); }
    }

    FStateTreeReference* Ref = RefProp->ContainerPtrToValuePtr<FStateTreeReference>(Component);
    if (!Ref) { return MCPStateTree_Error(TEXT("null state-tree-reference value ptr")); }

    // Capture prior target asset path for a reversible undo.
    const UStateTree* PriorTree = Ref->GetStateTree(); // VERIFY vs engine source (FStateTreeReference::GetStateTree)
    const FString PrevPath = PriorTree ? PriorTree->GetPathName() : FString(TEXT("None"));

    Component->Modify();
    if (!NewRefText.IsEmpty())
    {
        // Inverse / exact path: import the whole FStateTreeReference struct text.
        const TCHAR* Res = RefProp->ImportText_Direct(*NewRefText, Ref, Component, PPF_None, nullptr);
        if (Res == nullptr) { return MCPStateTree_Error(FString::Printf(TEXT("ImportText failed for state-tree-ref '%s'"), *NewRefText)); }
    }
    else
    {
        Ref->SetStateTree(NewStateTree); // syncs the parameter bag to the new asset; NewStateTree may be null to clear
    }
    Component->MarkPackageDirty();

    const UStateTree* NowTree = Ref->GetStateTree();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("component"), Component->GetPathName());
    Root->SetStringField(TEXT("property"), RefProp->GetName());
    Root->SetStringField(TEXT("prev_state_tree"), PrevPath);
    Root->SetStringField(TEXT("state_tree"), NowTree ? NowTree->GetPathName() : FString(TEXT("None")));
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (4) State color edit ------------------------------------------------------------------------
// Forward: set a state's ColorRef by palette DisplayName (looked up in EditorData->Colors).
// Inverse: pass ColorGuid = the returned `prev` (raw ColorRef GUID). `prev` is always returned.
FString UMCPReflectionLibrary::SetStateTreeColorJson(UStateTree* StateTree, const FString& StateName,
    const FString& ColorName, const FString& ColorGuid)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }

    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    UStateTreeState* State = MCPStateTree_FindState(ED, StateName);
    if (!State) { return MCPStateTree_Error(FString::Printf(TEXT("state '%s' not found"), *StateName)); }

    const FString PrevGuid = State->ColorRef.ID.ToString(EGuidFormats::DigitsWithHyphens); // VERIFY vs engine source (UStateTreeState::ColorRef.ID)

    FGuid NewId;
    FString ResolvedName;
    if (!ColorName.IsEmpty())
    {
        bool bFound = false;
        for (const FStateTreeEditorColor& Color : ED->Colors) // TSet<FStateTreeEditorColor>
        {
            if (Color.DisplayName.Equals(ColorName, ESearchCase::IgnoreCase))
            {
                NewId = Color.ColorRef.ID; // VERIFY vs engine source (FStateTreeEditorColor::ColorRef.ID)
                ResolvedName = Color.DisplayName;
                bFound = true;
                break;
            }
        }
        if (!bFound) { return MCPStateTree_Error(FString::Printf(TEXT("palette color '%s' not found in editor-data Colors"), *ColorName)); }
    }
    else if (!ColorGuid.IsEmpty())
    {
        if (!FGuid::Parse(ColorGuid, NewId)) { return MCPStateTree_Error(FString::Printf(TEXT("could not parse color guid '%s'"), *ColorGuid)); }
    }
    else
    {
        return MCPStateTree_Error(TEXT("provide either ColorName (palette display name) or ColorGuid"));
    }

    State->Modify();
    State->ColorRef.ID = NewId;
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("state"), StateName);
    Root->SetStringField(TEXT("color_name"), ResolvedName);
    Root->SetStringField(TEXT("color_guid"), NewId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("prev"), PrevGuid);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (5) RootParameters — read ------------------------------------------------------------------
FString UMCPReflectionLibrary::GetStateTreeParametersJson(UStateTree* StateTree)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    const FInstancedPropertyBag& Bag = ED->GetRootParametersPropertyBag(); // public inline getter (const)
    TArray<TSharedPtr<FJsonValue>> Params;
    if (const UPropertyBag* BagStruct = Bag.GetPropertyBagStruct()) // null when the bag is empty
    {
        for (const FPropertyBagPropertyDesc& Desc : BagStruct->GetPropertyDescs())
        {
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("name"), Desc.Name.ToString());
            J->SetStringField(TEXT("type"), MCPStateTree_BagTypeName(Desc.ValueType));
            J->SetStringField(TEXT("id"), Desc.ID.ToString(EGuidFormats::DigitsWithHyphens));
            if (Desc.ValueTypeObject) { J->SetStringField(TEXT("type_object"), Desc.ValueTypeObject->GetPathName()); }
            TValueOrError<FString, EPropertyBagResult> V = Bag.GetValueSerializedString(Desc.Name);
            J->SetStringField(TEXT("value"), V.HasValue() ? V.GetValue() : FString());
            Params.Add(MakeShared<FJsonValueObject>(J));
        }
    }

    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("state_tree"), StateTree->GetPathName());
    Root->SetStringField(TEXT("parameters_guid"), ED->GetRootParametersGuid().ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetNumberField(TEXT("count"), Params.Num());
    Root->SetArrayField(TEXT("parameters"), Params);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (6) RootParameters — add ------------------------------------------------------------------
FString UMCPReflectionLibrary::AddStateTreeParameter(UStateTree* StateTree, const FString& ParamName, const FString& TypeName)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    if (ParamName.IsEmpty()) { return MCPStateTree_Error(TEXT("parameter name is required")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    EPropertyBagPropertyType Type = EPropertyBagPropertyType::None;
    const UObject* TypeObj = nullptr;
    if (!MCPStateTree_ParseBagType(TypeName, Type, TypeObj))
    {
        return MCPStateTree_Error(FString::Printf(TEXT("unknown parameter type '%s'"), *TypeName));
    }

    FInstancedPropertyBag* Bag = MCPStateTree_MutableRootBag(ED);
    if (!Bag) { return MCPStateTree_Error(TEXT("could not reach RootParameterPropertyBag")); }

    const FName PName(*ParamName);
    if (Bag->FindPropertyDescByName(PName) != nullptr)
    {
        return MCPStateTree_Error(FString::Printf(TEXT("parameter '%s' already exists"), *ParamName));
    }

    const EPropertyBagAlterationResult R = Bag->AddProperty(PName, Type, TypeObj);
    if (R != EPropertyBagAlterationResult::Success)
    {
        return MCPStateTree_Error(FString::Printf(TEXT("AddProperty('%s') failed (alteration result %d)"), *ParamName, (int32)R));
    }

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("param"), ParamName);
    Root->SetStringField(TEXT("type"), MCPStateTree_BagTypeName(Type));
    Root->SetNumberField(TEXT("count"), Bag->GetNumPropertiesInBag());
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (7) RootParameters — set value (reversible via returned `prev`) ----------------------------
FString UMCPReflectionLibrary::SetStateTreeParameter(UStateTree* StateTree, const FString& ParamName, const FString& ValueText)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    if (ParamName.IsEmpty()) { return MCPStateTree_Error(TEXT("parameter name is required")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FInstancedPropertyBag* Bag = MCPStateTree_MutableRootBag(ED);
    if (!Bag) { return MCPStateTree_Error(TEXT("could not reach RootParameterPropertyBag")); }

    const FName PName(*ParamName);
    if (Bag->FindPropertyDescByName(PName) == nullptr)
    {
        return MCPStateTree_Error(FString::Printf(TEXT("parameter '%s' not found"), *ParamName));
    }

    TValueOrError<FString, EPropertyBagResult> PrevV = Bag->GetValueSerializedString(PName);
    const FString Prev = PrevV.HasValue() ? PrevV.GetValue() : FString();

    const EPropertyBagResult R = Bag->SetValueSerializedString(PName, ValueText);
    if (R != EPropertyBagResult::Success)
    {
        return MCPStateTree_Error(FString::Printf(TEXT("SetValueSerializedString('%s') failed (result %d)"), *ParamName, (int32)R));
    }

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("param"), ParamName);
    Root->SetStringField(TEXT("value"), ValueText);
    Root->SetStringField(TEXT("prev"), Prev);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (8) RootParameters — remove (captures type+value for a faithful re-add) --------------------
FString UMCPReflectionLibrary::RemoveStateTreeParameter(UStateTree* StateTree, const FString& ParamName)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    if (ParamName.IsEmpty()) { return MCPStateTree_Error(TEXT("parameter name is required")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FInstancedPropertyBag* Bag = MCPStateTree_MutableRootBag(ED);
    if (!Bag) { return MCPStateTree_Error(TEXT("could not reach RootParameterPropertyBag")); }

    const FName PName(*ParamName);
    const FPropertyBagPropertyDesc* Desc = Bag->FindPropertyDescByName(PName);
    if (Desc == nullptr) { return MCPStateTree_Error(FString::Printf(TEXT("parameter '%s' not found"), *ParamName)); }

    const FString CapturedType = MCPStateTree_BagTypeName(Desc->ValueType);
    const FString CapturedTypeObj = Desc->ValueTypeObject ? Desc->ValueTypeObject->GetPathName() : FString();
    TValueOrError<FString, EPropertyBagResult> ValV = Bag->GetValueSerializedString(PName);
    const FString CapturedValue = ValV.HasValue() ? ValV.GetValue() : FString();

    const EPropertyBagAlterationResult R = Bag->RemovePropertyByName(PName);
    if (R != EPropertyBagAlterationResult::Success)
    {
        return MCPStateTree_Error(FString::Printf(TEXT("RemovePropertyByName('%s') failed (alteration result %d)"), *ParamName, (int32)R));
    }

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("param"), ParamName);
    Root->SetStringField(TEXT("type"), CapturedType);
    Root->SetStringField(TEXT("type_object"), CapturedTypeObj);
    Root->SetStringField(TEXT("value"), CapturedValue);
    Root->SetNumberField(TEXT("count"), Bag->GetNumPropertiesInBag());
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (9) EditorBindings — add -------------------------------------------------------------------
FString UMCPReflectionLibrary::AddStateTreeBinding(UStateTree* StateTree, const FString& SourceStructId,
    const FString& SourcePath, const FString& TargetStructId, const FString& TargetPath)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FGuid SrcId, TgtId;
    if (!FGuid::Parse(SourceStructId, SrcId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad source struct id '%s'"), *SourceStructId)); }
    if (!FGuid::Parse(TargetStructId, TgtId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target struct id '%s'"), *TargetStructId)); }

    FPropertyBindingPath Src, Tgt;
    Src.SetStructID(SrcId);
    Tgt.SetStructID(TgtId);
    if (!Src.FromString(SourcePath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad source path '%s'"), *SourcePath)); }
    if (!Tgt.FromString(TargetPath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target path '%s'"), *TargetPath)); }

    const int32 Before = ED->EditorBindings.GetNumBindings();
    ED->AddPropertyBinding(Src, Tgt); // inline helper -> EditorBindings.AddBinding (replaces any exact-target binding)
    const int32 After = ED->EditorBindings.GetNumBindings();

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("source_struct_id"), SrcId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("source_property"), SourcePath);
    Root->SetStringField(TEXT("target_struct_id"), TgtId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("target_property"), TargetPath);
    Root->SetNumberField(TEXT("binding_count"), After);
    Root->SetBoolField(TEXT("replaced_existing"), After == Before);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (10) EditorBindings — remove (by target path) ----------------------------------------------
FString UMCPReflectionLibrary::RemoveStateTreeBinding(UStateTree* StateTree, const FString& TargetStructId, const FString& TargetPath)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FGuid TgtId;
    if (!FGuid::Parse(TargetStructId, TgtId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target struct id '%s'"), *TargetStructId)); }

    FPropertyBindingPath Tgt;
    Tgt.SetStructID(TgtId);
    if (!Tgt.FromString(TargetPath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target path '%s'"), *TargetPath)); }

    const int32 Before = ED->EditorBindings.GetNumBindings();
    ED->RemovePropertyBinding(Tgt); // inline helper -> EditorBindings.RemoveBindings(Tgt, Includes)
    const int32 After = ED->EditorBindings.GetNumBindings();
    const int32 Removed = FMath::Max(0, Before - After);

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("target_struct_id"), TgtId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("target_property"), TargetPath);
    Root->SetNumberField(TEXT("removed"), Removed);
    Root->SetNumberField(TEXT("binding_count"), After);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (11) Compile -------------------------------------------------------------------------------
FString UMCPReflectionLibrary::CompileStateTree(UStateTree* StateTree)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }

    FStateTreeCompilerLog Log;
    const bool bCompiled = UStateTreeEditingSubsystem::CompileStateTree(StateTree, Log); // canonical headless compile (same as StateTreeCompileAllCommandlet)

    TArray<TSharedPtr<FJsonValue>> Messages;
    int32 ErrorCount = 0;
    int32 WarningCount = 0;
    for (const TSharedRef<FTokenizedMessage>& Msg : Log.ToTokenizedMessages())
    {
        const int32 Sev = (int32)Msg->GetSeverity();
        if (Sev == (int32)EMessageSeverity::Error) { ++ErrorCount; }
        else if (Sev == (int32)EMessageSeverity::Warning || Sev == (int32)EMessageSeverity::PerformanceWarning) { ++WarningCount; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("severity"), MCPStateTree_SeverityName(Sev));
        J->SetStringField(TEXT("message"), Msg->ToText().ToString());
        Messages.Add(MakeShared<FJsonValueObject>(J));
    }

    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), bCompiled ? TEXT("success") : TEXT("error"));
    Root->SetStringField(TEXT("state_tree"), StateTree->GetPathName());
    Root->SetBoolField(TEXT("compiled"), bCompiled);
    Root->SetNumberField(TEXT("error_count"), ErrorCount);
    Root->SetNumberField(TEXT("warning_count"), WarningCount);
    Root->SetNumberField(TEXT("message_count"), Messages.Num());
    Root->SetArrayField(TEXT("messages"), Messages);
    if (!bCompiled && ErrorCount == 0)
    {
        Root->SetStringField(TEXT("error"), TEXT("compile failed (see messages)"));
    }
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (12) Repair malformed nodes — THE StateTree crash fix (C++ #20 2026-08-18) -----------------
// Nodes authored via Python `en.import_text("(Node=/Script/...)")` set the Node struct but leave the editor
// node's Instance FInstancedStruct EMPTY and the ID a zero GUID. The StateTree compiler hard-requires
// `check(EditorNode.GetInstance().GetStruct() == Instance.GetScriptStruct())` (StateTreeCompiler.cpp:1907)
// and does GUID-keyed binding lookups, so ANY compile / validate / save that walks such a node AVs
// (a clean assertion in a checked build; EXCEPTION_ACCESS_VIOLATION in Development). This mirrors the editor's
// own FStateTreeEditorNode picker path (StateTreeEditorNodeUtils::ConditionalUpdateNodeInstanceData ->
// ReallocInstanceData): realloc each node's Instance to match Node->GetInstanceDataType(), and stamp a fresh
// FGuid onto any node/state/transition whose ID is unset. Idempotent (well-formed tree => 0 fixes).
FString UMCPReflectionLibrary::RepairStateTreeNodes(UStateTree* StateTree)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    int32 NodesSeen = 0, InstancesFixed = 0, NodeIdsFixed = 0, StateIdsFixed = 0, TransIdsFixed = 0;

    auto RepairNode = [&](FStateTreeEditorNode& EN)
    {
        const FStateTreeNodeBase* NodeBase = EN.GetNode().GetPtr<FStateTreeNodeBase>();
        if (!NodeBase) { return; } // empty line in the editor array (compiler skips these too)
        ++NodesSeen;
        const UStruct* Desired = NodeBase->GetInstanceDataType();
        const UStruct* Current = EN.GetInstance().GetStruct();
        if (Current != Desired)
        {
            EN.ReallocInstanceData(ED); // reallocates Instance (+ exec-runtime) to match the node type; preserves ID
            ++InstancesFixed;
        }
        if (!EN.ID.IsValid())
        {
            EN.ID = FGuid::NewGuid();
            ++NodeIdsFixed;
        }
    };
    auto RepairArray = [&](TArray<FStateTreeEditorNode>& Arr) { for (FStateTreeEditorNode& EN : Arr) { RepairNode(EN); } };

    RepairArray(ED->Evaluators);
    RepairArray(ED->GlobalTasks);

    TArray<UStateTreeState*> Stack;
    for (UStateTreeState* Root : ED->SubTrees) { if (Root) { Stack.Add(Root); } }
    while (Stack.Num() > 0)
    {
        UStateTreeState* St = Stack.Pop();
        if (!St) { continue; }
        if (!St->ID.IsValid()) { St->ID = FGuid::NewGuid(); ++StateIdsFixed; }
        RepairArray(St->Tasks);
        RepairArray(St->EnterConditions);
        RepairArray(St->Considerations);
        RepairNode(St->SingleTask);
        for (FStateTreeTransition& Tr : St->Transitions)
        {
            if (!Tr.ID.IsValid()) { Tr.ID = FGuid::NewGuid(); ++TransIdsFixed; }
            RepairArray(Tr.Conditions);
        }
        for (UStateTreeState* Child : St->Children) { if (Child) { Stack.Add(Child); } }
    }

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); // keep binding cache consistent after the instance reallocs
    ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("state_tree"), StateTree->GetPathName());
    Root->SetNumberField(TEXT("nodes_seen"), NodesSeen);
    Root->SetNumberField(TEXT("instances_fixed"), InstancesFixed);
    Root->SetNumberField(TEXT("node_ids_fixed"), NodeIdsFixed);
    Root->SetNumberField(TEXT("state_ids_fixed"), StateIdsFixed);
    Root->SetNumberField(TEXT("transition_ids_fixed"), TransIdsFixed);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (13) Binding SOURCES for a target (C++ #21 2026-08-19) --------------------------------------
// The valid binding sources for a target struct (root/state parameters, evaluators, global tasks, preceding
// tasks, context data, ...). Mirrors what the editor's binding dropdown offers. Read-only.
FString UMCPReflectionLibrary::GetStateTreeBindingSourcesJson(UStateTree* StateTree, const FString& TargetStructId)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FGuid TgtId;
    if (!FGuid::Parse(TargetStructId, TgtId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target struct id '%s'"), *TargetStructId)); }

    TArray<TInstancedStruct<FPropertyBindingBindableStructDescriptor>> Descs;
    ED->GetBindableStructs(TgtId, Descs);

    TArray<TSharedPtr<FJsonValue>> Sources;
    for (const TInstancedStruct<FPropertyBindingBindableStructDescriptor>& D : Descs)
    {
        const FPropertyBindingBindableStructDescriptor* Desc = D.GetPtr();
        if (!Desc) { continue; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), Desc->Name.ToString());
        J->SetStringField(TEXT("id"), Desc->ID.ToString(EGuidFormats::DigitsWithHyphens));
        J->SetStringField(TEXT("struct"), Desc->Struct ? Desc->Struct->GetPathName() : FString());
        Sources.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("target_struct_id"), TgtId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetNumberField(TEXT("count"), Sources.Num());
    Root->SetArrayField(TEXT("sources"), Sources);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (14) Task-completion binding (C++ #21) -----------------------------------------------------
// Bind a task's completion (a generated delegate) to a target listener path. Condition: succeeds|fails|completes.
FString UMCPReflectionLibrary::AddStateTreeTaskCompletionBinding(UStateTree* StateTree, const FString& SourceTaskId,
    const FString& TargetStructId, const FString& TargetPath, const FString& Condition)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FGuid SrcTask, TgtId;
    if (!FGuid::Parse(SourceTaskId, SrcTask)) { return MCPStateTree_Error(FString::Printf(TEXT("bad source task id '%s'"), *SourceTaskId)); }
    if (!FGuid::Parse(TargetStructId, TgtId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target struct id '%s'"), *TargetStructId)); }

    FPropertyBindingPath Tgt;
    Tgt.SetStructID(TgtId);
    if (!Tgt.FromString(TargetPath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad target path '%s'"), *TargetPath)); }

    const FString C = Condition.ToLower();
    UE::StateTree::ETaskCompletionCondition Cond = UE::StateTree::ETaskCompletionCondition::Completes;
    if (C == TEXT("succeeds") || C == TEXT("success") || C == TEXT("succeeded")) { Cond = UE::StateTree::ETaskCompletionCondition::Succeeds; }
    else if (C == TEXT("fails") || C == TEXT("failed") || C == TEXT("failure")) { Cond = UE::StateTree::ETaskCompletionCondition::Fails; }

    const int32 Before = ED->EditorBindings.GetNumBindings();
    const FStateTreePropertyPathBinding* B = ED->EditorBindings.AddTaskCompletionBinding(SrcTask, Tgt, Cond);
    const int32 After = ED->EditorBindings.GetNumBindings();

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("source_task_id"), SrcTask.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("target_struct_id"), TgtId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("target_property"), TargetPath);
    Root->SetStringField(TEXT("condition"), C);
    Root->SetBoolField(TEXT("added"), B != nullptr);
    Root->SetNumberField(TEXT("binding_count"), After);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}

// ---- (15) Delegate dispatcher->listener binding (C++ #21) ---------------------------------------
// A delegate binding is a property binding from a dispatcher (FStateTreeDelegateDispatcher-typed source) to a
// listener (FStateTreeDelegateListener-typed target). Same mechanism as AddStateTreeBinding, semantically distinct.
FString UMCPReflectionLibrary::BindStateTreeDelegate(UStateTree* StateTree, const FString& DispatcherStructId,
    const FString& DispatcherPath, const FString& ListenerStructId, const FString& ListenerPath)
{
#if WITH_EDITOR
    if (!StateTree) { return MCPStateTree_Error(TEXT("null state tree")); }
    UStateTreeEditorData* ED = MCPStateTree_ResolveEditorData(StateTree);
    if (!ED) { return MCPStateTree_Error(TEXT("could not reach UStateTreeEditorData")); }

    FGuid SrcId, TgtId;
    if (!FGuid::Parse(DispatcherStructId, SrcId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad dispatcher struct id '%s'"), *DispatcherStructId)); }
    if (!FGuid::Parse(ListenerStructId, TgtId)) { return MCPStateTree_Error(FString::Printf(TEXT("bad listener struct id '%s'"), *ListenerStructId)); }

    FPropertyBindingPath Src, Tgt;
    Src.SetStructID(SrcId);
    Tgt.SetStructID(TgtId);
    if (!Src.FromString(DispatcherPath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad dispatcher path '%s'"), *DispatcherPath)); }
    if (!Tgt.FromString(ListenerPath)) { return MCPStateTree_Error(FString::Printf(TEXT("bad listener path '%s'"), *ListenerPath)); }

    const int32 Before = ED->EditorBindings.GetNumBindings();
    ED->AddPropertyBinding(Src, Tgt);
    const int32 After = ED->EditorBindings.GetNumBindings();

    ED->Modify();
    MCPStateTree_RefreshBindings(ED); ED->MarkPackageDirty();
    StateTree->MarkPackageDirty();
    TSharedRef<FJsonObject> Root = MCPStateTree_Ok();
    Root->SetStringField(TEXT("dispatcher_struct_id"), SrcId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("dispatcher_path"), DispatcherPath);
    Root->SetStringField(TEXT("listener_struct_id"), TgtId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("listener_path"), ListenerPath);
    Root->SetNumberField(TEXT("binding_count"), After);
    Root->SetBoolField(TEXT("replaced_existing"), After == Before);
    return MCPStateTree_SerializeJson(Root);
#else
    return MCPStateTree_Error(TEXT("editor-only"));
#endif
}
