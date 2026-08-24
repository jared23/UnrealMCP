// UnrealMCP — BLUEPRINT FUNCTION + EVENT-GRAPH authoring subsystem (C++ DRAFT 2026-08-19).
//
// Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator (do NOT edit the .h here). Composes on the same idioms as
// MCPReflection_BlueprintGraph.cpp (JSON-string returns, {"error":...} on any miss, deferred compile).
//
// PRIMARY WRITES (each folds an inverse into the Python per-session undo ledger):
//   1) CreateBlueprintFunctionGraphJson — new user function graph (fuller signature vs the empty
//      add_blueprint_function): AddFunctionGraph<UClass>(bp, graph, true, nullptr) + optional return pin.
//      Inverse: DeleteBlueprintFunctionJson (RemoveGraph).
//   2) AddFunctionInputJson  — CreateUserDefinedPin on the FunctionEntry (EGPD_Output = a function INPUT).
//      Inverse: RemoveFunctionPinJson(is_output=false).
//   3) AddFunctionOutputJson — find/create the FunctionResult; CreateUserDefinedPin (EGPD_Input = an OUTPUT).
//      Inverse: RemoveFunctionPinJson(is_output=true).
//   4) SetFunctionPropertiesJson — FunctionEntry ExtraFlags (FUNC_BlueprintPure / FUNC_Const / access
//      specifier) + category + tooltip/keywords + arbitrary MetaData. Captures prior. Inverse: restore.
//   5) CreateLocalVariableJson — FBlueprintEditorUtils::AddLocalVariable. Inverse: RemoveLocalVariableJson.
//   6) DeleteBlueprintFunctionJson — FBlueprintEditorUtils::RemoveGraph a function graph. Captures a LOSSY
//      re-add spec (name + signature pins). Inverse (best-effort): CreateBlueprintFunctionGraphJson + pins.
//   7) OverrideBlueprintFunctionJson — override a parent UFUNCTION as a function graph:
//      AddFunctionGraph<UClass>(bp, graph, false, OverrideFuncClass) (mirrors
//      UBlueprintEditorLibrary::AddFunctionOverride). Inverse: DeleteBlueprintFunctionJson.
//   8) CreateEventGraphJson — CreateNewGraph + bp->UbergraphPages.Add (mirrors AddEventNode's graph
//      creation). Inverse: DeleteEventGraphJson (RemoveGraph the ubergraph page).
//   9) RenameEventGraphJson — FBlueprintEditorUtils::RenameGraph. Inverse: rename back.
//  10) DeleteEventGraphJson — RemoveGraph an ubergraph page. LOSSY re-add (see LOSSINESS). Inverse
//      (best-effort): CreateEventGraphJson.
//  11) AddEventDispatcherInputJson — add a param pin to a multicast-delegate dispatcher's SIGNATURE graph
//      entry (extends the shipped no-arg add_event_dispatcher). Inverse: RemoveEventDispatcherInputJson.
//
// COMPANION INVERSE-SUPPORT WRITES (small removers so the ledger inverses of #2/#3/#5/#11 actually run —
// they ALSO need header decls; flagged in the coordinator report):
//  12) RemoveFunctionPinJson          — RemoveUserDefinedPinByName on entry (input) or result (output).
//  13) RemoveLocalVariableJson        — remove a local var from the FunctionEntry LocalVariables array.
//  14) RemoveEventDispatcherInputJson — RemoveUserDefinedPinByName on the delegate signature entry.
//
// COMPILE STRATEGY (matches MCPReflection_BlueprintGraph.cpp): every write ends with
// FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(bp) ONLY — never a per-call compile. Nodes
// whose PIN SHAPE changed (entry/result after a CreateUserDefinedPin) get a guarded ReconstructNode() +
// UEdGraphSchema_K2::HandleParameterDefaultValueChanged (the engine's own OnParamsChanged flow). The Python
// caller batches edits then calls compile_blueprint_by_path / unreal.BlueprintEditorLibrary.compile_blueprint
// exactly once. Rationale: compiling a half-built signature is wasteful and the single most crash-prone op.
//
// LOSSINESS: deleting a function/event graph captures only {name, signature pins} — the graph's BODY (its
// nodes/wiring) is NOT captured, so a delete-undo re-creates an EMPTY graph of the same name/signature, not
// a byte-exact restoration. SetFunctionProperties metadata-restore only re-sets prior values for the keys it
// touched (a key that did not exist before is set to "" by the inverse, not truly unset). Both documented in
// the Python module + this header.
//
// CRASH-SAFETY: every blueprint/graph/node/pin lookup is guarded + null-checked (NEVER *Checked on user
// input); every engine touch point is wrapped where it can re-enter engine code; every path returns
// {"error":...} on a miss. All handlers are #if WITH_EDITOR guarded.
//
// PinType building REUSES the shipped BuildComplexPinType semantics from MCPReflection_Structs.cpp:214.
// That helper lives in ITS file's anonymous namespace (TU-local, not linkable here), so this TU carries an
// IDENTICAL prefixed copy (MCPBpFn_BuildPinType) — same scalar+object/class/soft/struct/enum+container
// coverage. Keep the two in sync if either changes.
//
// Module deps: BlueprintGraph (UK2Node_FunctionEntry/Result / EdGraphSchema_K2), UnrealEd
// (FBlueprintEditorUtils), Engine (UBlueprint / UEdGraph / UEdGraphPin). ALL already present in
// UnrealMCP.Build.cs -> NO Build.cs change, NO export patch. Anonymous-namespace helpers are prefixed
// `MCPBpFn_` so they stay unique in the module's unity build. Every engine-API touch point is tagged
// "VERIFY vs engine source" for the coordinator's live pass.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"                 // UScriptStruct / UEnum / TBaseStructure / UClass::TryFindTypeSlow
#include "UObject/UObjectGlobals.h"        // LoadObject / FindObject
#include "UObject/Script.h"                // EFunctionFlags (FUNC_BlueprintPure / FUNC_Const / FUNC_AccessSpecifiers)
#include "Misc/PackageName.h"              // FPackageName::GetShortName (bare-path load fallback)

#include "Engine/Blueprint.h"                 // UBlueprint / FunctionGraphs / UbergraphPages / DelegateSignatureGraphs
#include "Engine/BlueprintGeneratedClass.h"   // UBlueprintGeneratedClass (BP-asset -> generated class resolve)
#include "EdGraph/EdGraph.h"                  // UEdGraph::Nodes
#include "EdGraph/EdGraphNode.h"              // UEdGraphNode / FEdGraphTerminalType
#include "EdGraph/EdGraphPin.h"               // UEdGraphPin / FEdGraphPinType / EPinContainerType

#if WITH_EDITOR
#include "EdGraphSchema_K2.h"                 // UEdGraphSchema_K2::PC_* / FBlueprintMetadata / HandleParameterDefaultValueChanged
#include "K2Node_EditablePinBase.h"           // UK2Node_EditablePinBase::CreateUserDefinedPin / RemoveUserDefinedPinByName / FKismetUserDeclaredFunctionMetadata
#include "K2Node_FunctionEntry.h"             // UK2Node_FunctionEntry (ExtraFlags / MetaData / LocalVariables)
#include "K2Node_FunctionResult.h"            // UK2Node_FunctionResult (GetAllResultNodes)
#include "Kismet2/BlueprintEditorUtils.h"     // FBlueprintEditorUtils (AddFunctionGraph / RemoveGraph / CreateNewGraph / RenameGraph / AddLocalVariable / ...)
#endif // WITH_EDITOR

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPBpFn_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"error": msg} — the Python read/write paths both key off res.get("error"). Success objects never
    // carry an "error" key.
    FString MCPBpFn_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPBpFn_Serialize(Root);
    }

#if WITH_EDITOR
    // ---- blueprint / class / graph resolution (mirror MCPReflection_BlueprintGraph.cpp) -------------
    UBlueprint* MCPBpFn_LoadBlueprint(const FString& Path)
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

    UClass* MCPBpFn_ResolveClass(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        UClass* C = LoadObject<UClass>(nullptr, *Path);
        if (!C) { C = FindObject<UClass>(nullptr, *Path); }
        if (!C) { C = UClass::TryFindTypeSlow<UClass>(Path); }                 // VERIFY vs engine source
        if (!C)
        {
            const FString EnginePath = FString::Printf(TEXT("/Script/Engine.%s"), *Path);
            C = LoadObject<UClass>(nullptr, *EnginePath);
        }
        if (!C)
        {
            if (UObject* Obj = LoadObject<UObject>(nullptr, *Path))
            {
                if (UBlueprint* BP = Cast<UBlueprint>(Obj))
                {
                    C = BP->GeneratedClass;                                    // VERIFY vs engine source (UBlueprint::GeneratedClass)
                }
            }
        }
        return C;
    }

    UScriptStruct* MCPBpFn_ResolveStruct(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        if (UScriptStruct* S = LoadObject<UScriptStruct>(nullptr, *Path)) { return S; }
        if (UScriptStruct* S = FindObject<UScriptStruct>(nullptr, *Path)) { return S; }
        return UClass::TryFindTypeSlow<UScriptStruct>(Path);
    }

    UEnum* MCPBpFn_ResolveEnum(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        if (UEnum* E = LoadObject<UEnum>(nullptr, *Path)) { return E; }
        if (UEnum* E = FindObject<UEnum>(nullptr, *Path)) { return E; }
        return UClass::TryFindTypeSlow<UEnum>(Path);
    }

    // Resolve a single (category, type_path) spec into the primary triple of an FEdGraphPinType. IDENTICAL
    // in semantics to MCPReflection_Structs.cpp's ApplyTypeSpec (kept a prefixed copy — that one is TU-local).
    bool MCPBpFn_ApplyTypeSpec(const FString& CategoryRaw, const FString& TypePath,
                               FName& OutCat, FName& OutSub, UObject*& OutSubObj, FString& OutErr)
    {
        const FString L = CategoryRaw.ToLower();
        OutCat = NAME_None;
        OutSub = NAME_None;
        OutSubObj = nullptr;

        if (L == TEXT("bool") || L == TEXT("boolean")) { OutCat = UEdGraphSchema_K2::PC_Boolean; return true; }
        if (L == TEXT("byte"))                          { OutCat = UEdGraphSchema_K2::PC_Byte; return true; }
        if (L == TEXT("int") || L == TEXT("int32") || L == TEXT("integer")) { OutCat = UEdGraphSchema_K2::PC_Int; return true; }
        if (L == TEXT("int64"))                         { OutCat = UEdGraphSchema_K2::PC_Int64; return true; }
        if (L == TEXT("float") || L == TEXT("double") || L == TEXT("real"))
        {
            OutCat = UEdGraphSchema_K2::PC_Real;
            OutSub = UEdGraphSchema_K2::PC_Double;
            return true;
        }
        if (L == TEXT("name"))   { OutCat = UEdGraphSchema_K2::PC_Name; return true; }
        if (L == TEXT("string")) { OutCat = UEdGraphSchema_K2::PC_String; return true; }
        if (L == TEXT("text"))   { OutCat = UEdGraphSchema_K2::PC_Text; return true; }

        auto SetStruct = [&](UScriptStruct* SS) -> bool
        {
            if (!SS) { OutErr = TEXT("could not resolve struct type"); return false; }
            OutCat = UEdGraphSchema_K2::PC_Struct;
            OutSubObj = SS;
            return true;
        };
        if (L == TEXT("vector") || L == TEXT("vector3")) { return SetStruct(TBaseStructure<FVector>::Get()); }
        if (L == TEXT("vector2d") || L == TEXT("vector2")) { return SetStruct(TBaseStructure<FVector2D>::Get()); }
        if (L == TEXT("rotator"))    { return SetStruct(TBaseStructure<FRotator>::Get()); }
        if (L == TEXT("transform"))  { return SetStruct(TBaseStructure<FTransform>::Get()); }
        if (L == TEXT("quat"))       { return SetStruct(TBaseStructure<FQuat>::Get()); }
        if (L == TEXT("linearcolor") || L == TEXT("color")) { return SetStruct(TBaseStructure<FLinearColor>::Get()); }

        if (L == TEXT("struct"))
        {
            UScriptStruct* SS = MCPBpFn_ResolveStruct(TypePath);
            if (!SS) { OutErr = FString::Printf(TEXT("could not resolve struct '%s'"), *TypePath); return false; }
            return SetStruct(SS);
        }
        if (L == TEXT("enum"))
        {
            UEnum* En = MCPBpFn_ResolveEnum(TypePath);
            if (!En) { OutErr = FString::Printf(TEXT("could not resolve enum '%s'"), *TypePath); return false; }
            OutCat = UEdGraphSchema_K2::PC_Byte;       // K2 schema maps a byte-backed enum member onto PC_Byte
            OutSubObj = En;
            return true;
        }
        if (L == TEXT("object") || L == TEXT("class") || L == TEXT("softobject") ||
            L == TEXT("softclass") || L == TEXT("interface"))
        {
            UClass* C = TypePath.IsEmpty() ? UObject::StaticClass() : MCPBpFn_ResolveClass(TypePath);
            if (!C) { OutErr = FString::Printf(TEXT("could not resolve class '%s'"), *TypePath); return false; }
            if (L == TEXT("object"))          { OutCat = UEdGraphSchema_K2::PC_Object; }
            else if (L == TEXT("class"))      { OutCat = UEdGraphSchema_K2::PC_Class; }
            else if (L == TEXT("softobject")) { OutCat = UEdGraphSchema_K2::PC_SoftObject; }
            else if (L == TEXT("softclass"))  { OutCat = UEdGraphSchema_K2::PC_SoftClass; }
            else                              { OutCat = UEdGraphSchema_K2::PC_Interface; }
            OutSubObj = C;
            return true;
        }

        OutErr = FString::Printf(TEXT("unsupported type category '%s'"), *CategoryRaw);
        return false;
    }

    // Build a full FEdGraphPinType from TypeJson (bare scalar token OR {category, type_path?, container?,
    // value?}; aliases type/path and is_array/is_set/is_map). IDENTICAL to Structs.cpp BuildComplexPinType.
    bool MCPBpFn_BuildPinType(const FString& TypeJson, FEdGraphPinType& Out, FString& OutErr)
    {
        Out = FEdGraphPinType();
        Out.ContainerType = EPinContainerType::None;

        FString Category, TypePath, Container;
        TSharedPtr<FJsonObject> ValueObj;

        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(TypeJson);
        if (FJsonSerializer::Deserialize(Reader, Obj) && Obj.IsValid())
        {
            Obj->TryGetStringField(TEXT("category"), Category);
            if (Category.IsEmpty()) { Obj->TryGetStringField(TEXT("type"), Category); }
            Obj->TryGetStringField(TEXT("type_path"), TypePath);
            if (TypePath.IsEmpty()) { Obj->TryGetStringField(TEXT("path"), TypePath); }
            Obj->TryGetStringField(TEXT("container"), Container);

            const TSharedPtr<FJsonObject>* VObjPtr = nullptr;
            if (Obj->TryGetObjectField(TEXT("value"), VObjPtr) && VObjPtr)
            {
                ValueObj = *VObjPtr;
            }

            if (Container.IsEmpty())
            {
                bool b = false;
                if (Obj->TryGetBoolField(TEXT("is_map"), b) && b)        { Container = TEXT("map"); }
                else if (Obj->TryGetBoolField(TEXT("is_set"), b) && b)   { Container = TEXT("set"); }
                else if (Obj->TryGetBoolField(TEXT("is_array"), b) && b) { Container = TEXT("array"); }
            }
        }
        else
        {
            Category = TypeJson.TrimStartAndEnd().TrimQuotes();
        }

        if (Category.IsEmpty())
        {
            OutErr = TEXT("type spec missing 'category'");
            return false;
        }

        FName Cat, Sub;
        UObject* SubObj = nullptr;
        if (!MCPBpFn_ApplyTypeSpec(Category, TypePath, Cat, Sub, SubObj, OutErr))
        {
            return false;
        }
        Out.PinCategory = Cat;
        Out.PinSubCategory = Sub;
        Out.PinSubCategoryObject = SubObj;

        const FString CL = Container.ToLower();
        if (CL == TEXT("array")) { Out.ContainerType = EPinContainerType::Array; }
        else if (CL == TEXT("set")) { Out.ContainerType = EPinContainerType::Set; }
        else if (CL == TEXT("map"))
        {
            Out.ContainerType = EPinContainerType::Map;
            FString VCat, VPath;
            if (ValueObj.IsValid())
            {
                ValueObj->TryGetStringField(TEXT("category"), VCat);
                if (VCat.IsEmpty()) { ValueObj->TryGetStringField(TEXT("type"), VCat); }
                ValueObj->TryGetStringField(TEXT("type_path"), VPath);
                if (VPath.IsEmpty()) { ValueObj->TryGetStringField(TEXT("path"), VPath); }
            }
            if (VCat.IsEmpty())
            {
                OutErr = TEXT("map type requires a 'value' type spec");
                return false;
            }
            FName VC, VS;
            UObject* VSObj = nullptr;
            if (!MCPBpFn_ApplyTypeSpec(VCat, VPath, VC, VS, VSObj, OutErr))
            {
                return false;
            }
            Out.PinValueType.TerminalCategory = VC;
            Out.PinValueType.TerminalSubCategory = VS;
            Out.PinValueType.TerminalSubCategoryObject = VSObj;
        }
        else if (!CL.IsEmpty() && CL != TEXT("none"))
        {
            OutErr = FString::Printf(TEXT("unsupported container '%s'"), *Container);
            return false;
        }
        return true;
    }

    // FEdGraphPinType -> JSON (compact; for reporting the created pin's type).
    TSharedRef<FJsonObject> MCPBpFn_SerializePinType(const FEdGraphPinType& T)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("category"), T.PinCategory.ToString());
        if (!T.PinSubCategory.IsNone())
        {
            J->SetStringField(TEXT("sub_category"), T.PinSubCategory.ToString());
        }
        if (T.PinSubCategoryObject.IsValid() && T.PinSubCategoryObject.Get())
        {
            J->SetStringField(TEXT("sub_category_object"), T.PinSubCategoryObject.Get()->GetPathName());
        }
        const TCHAR* Cont =
            T.ContainerType == EPinContainerType::Array ? TEXT("array") :
            T.ContainerType == EPinContainerType::Set   ? TEXT("set")   :
            T.ContainerType == EPinContainerType::Map   ? TEXT("map")   : TEXT("none");
        J->SetStringField(TEXT("container"), Cont);
        return J;
    }

    TSharedPtr<FJsonObject> MCPBpFn_ParseObject(const FString& JsonText)
    {
        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
        if (FJsonSerializer::Deserialize(Reader, Obj) && Obj.IsValid())
        {
            return Obj;
        }
        return nullptr;
    }

    // Find a FUNCTION graph by name (function graphs only; NOT ubergraph pages).
    UEdGraph* MCPBpFn_FindFunctionGraph(UBlueprint* BP, const FString& FunctionName, FString& OutErr)
    {
        if (!BP) { OutErr = TEXT("null blueprint"); return nullptr; }
        for (UEdGraph* G : BP->FunctionGraphs)                                 // VERIFY vs engine source (UBlueprint::FunctionGraphs)
        {
            if (G && G->GetName().Equals(FunctionName, ESearchCase::IgnoreCase))
            {
                return G;
            }
        }
        OutErr = FString::Printf(TEXT("no function graph named '%s' in blueprint '%s'"), *FunctionName, *BP->GetName());
        return nullptr;
    }

    // Find an UBERGRAPH PAGE (event graph) by name.
    UEdGraph* MCPBpFn_FindUbergraph(UBlueprint* BP, const FString& GraphName, FString& OutErr)
    {
        if (!BP) { OutErr = TEXT("null blueprint"); return nullptr; }
        for (UEdGraph* G : BP->UbergraphPages)                                 // VERIFY vs engine source (UBlueprint::UbergraphPages)
        {
            if (G && G->GetName().Equals(GraphName, ESearchCase::IgnoreCase))
            {
                return G;
            }
        }
        OutErr = FString::Printf(TEXT("no event graph (ubergraph page) named '%s' in blueprint '%s'"), *GraphName, *BP->GetName());
        return nullptr;
    }

    // Locate the (single) UK2Node_FunctionEntry of a function / delegate-signature graph.
    UK2Node_FunctionEntry* MCPBpFn_FindEntry(UEdGraph* Graph)
    {
        if (!Graph) { return nullptr; }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (UK2Node_FunctionEntry* E = Cast<UK2Node_FunctionEntry>(N))     // VERIFY vs engine source
            {
                return E;
            }
        }
        return nullptr;
    }

    // True if any graph (function OR ubergraph OR delegate-signature) already carries this name — used to
    // reject a colliding create up-front (the engine would rename/uniquify silently).
    bool MCPBpFn_GraphNameInUse(UBlueprint* BP, const FString& Name)
    {
        auto Scan = [&Name](const TArray<UEdGraph*>& Arr) -> bool
        {
            for (UEdGraph* G : Arr)
            {
                if (G && G->GetName().Equals(Name, ESearchCase::IgnoreCase)) { return true; }
            }
            return false;
        };
        return Scan(BP->FunctionGraphs) || Scan(BP->UbergraphPages) ||
               Scan(BP->DelegateSignatureGraphs) || Scan(BP->MacroGraphs);
    }

    // Map an access-specifier string to its EFunctionFlags bit (default: keep None -> Public at compile).
    uint32 MCPBpFn_AccessFlag(const FString& Access, bool& bOut)
    {
        const FString A = Access.ToLower();
        bOut = true;
        if (A == TEXT("public"))    { return FUNC_Public; }
        if (A == TEXT("protected")) { return FUNC_Protected; }
        if (A == TEXT("private"))   { return FUNC_Private; }
        bOut = false;
        return 0;
    }

    FString MCPBpFn_AccessString(int32 Flags)
    {
        if (Flags & FUNC_Private)   { return TEXT("private"); }
        if (Flags & FUNC_Protected) { return TEXT("protected"); }
        return TEXT("public"); // FUNC_Public or unspecified
    }

    // Add a user-defined pin to entry (INPUT, EGPD_Output) or every result node (OUTPUT, EGPD_Input),
    // then run the engine's post-change flow (ReconstructNode + HandleParameterDefaultValueChanged).
    // Returns the created pin (from the primary node) or nullptr, with OutErr set.
    UEdGraphPin* MCPBpFn_AddPinAndReconstruct(UK2Node_EditablePinBase* PrimaryNode,
                                              const TArray<UK2Node_EditablePinBase*>& AllNodes,
                                              const FName PinName, const FEdGraphPinType& PinType,
                                              EEdGraphPinDirection Direction, FString& OutErr)
    {
        UEdGraphPin* Created = nullptr;
        const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
        for (UK2Node_EditablePinBase* Node : AllNodes)
        {
            if (!Node) { continue; }
            Node->Modify();
            UEdGraphPin* NewPin = nullptr;
            try
            {
                // bUseUniqueName=false: the caller already vetted the name; keep it stable across nodes.
                NewPin = Node->CreateUserDefinedPin(PinName, PinType, Direction, /*bUseUniqueName*/false);  // VERIFY vs engine source
            }
            catch (...)
            {
                OutErr = TEXT("exception during CreateUserDefinedPin");
                return nullptr;
            }
            if (!NewPin)
            {
                OutErr = FString::Printf(TEXT("CreateUserDefinedPin returned null for '%s' (duplicate name or disallowed type)"), *PinName.ToString());
                return nullptr;
            }
            if (Node == PrimaryNode) { Created = NewPin; }

            // Engine OnParamsChanged flow: reconstruct with orphan-pin saving disabled, then notify schema.
            try
            {
                const bool bPrev = Node->bDisableOrphanPinSaving;
                Node->bDisableOrphanPinSaving = true;
                Node->ReconstructNode();                                        // VERIFY vs engine source
                Node->bDisableOrphanPinSaving = bPrev;
                if (Schema) { Schema->HandleParameterDefaultValueChanged(Node); } // VERIFY vs engine source (UK2Node* overload)
            }
            catch (...) { /* pin exists; reconstruct best-effort */ }
        }
        return Created;
    }
#endif // WITH_EDITOR
} // namespace

// =====================================================================================================
// 1) CreateBlueprintFunctionGraphJson — new user function graph (+ optional return pin).
//    Inverse: DeleteBlueprintFunctionJson(function_name).
// =====================================================================================================
FString UMCPReflectionLibrary::CreateBlueprintFunctionGraphJson(const FString& BlueprintPath, const FString& FunctionName, const FString& ReturnTypeJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (FunctionName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("function_name is empty"));
    }
    if (MCPBpFn_GraphNameInUse(BP, FunctionName))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("a graph named '%s' already exists on this blueprint"), *FunctionName));
    }

    // Optional return type must parse BEFORE we mutate anything.
    FEdGraphPinType ReturnType;
    const bool bWantReturn = !ReturnTypeJson.IsEmpty() && ReturnTypeJson != TEXT("null");
    if (bWantReturn)
    {
        FString BuildErr;
        if (!MCPBpFn_BuildPinType(ReturnTypeJson, ReturnType, BuildErr))
        {
            return MCPBpFn_Err(FString::Printf(TEXT("bad return type spec: %s"), *BuildErr));
        }
    }

    BP->Modify();
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
        BP, FName(*FunctionName), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());  // VERIFY vs engine source
    if (!NewGraph)
    {
        return MCPBpFn_Err(TEXT("CreateNewGraph returned null"));
    }
    // <UClass> + (UClass*)nullptr signature => a fresh, user-editable function (entry node, no result node).
    try
    {
        FBlueprintEditorUtils::AddFunctionGraph<UClass>(BP, NewGraph, /*bIsUserCreated*/true, (UClass*)nullptr);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during AddFunctionGraph"));
    }

    // Optional: seed a return pin on a (freshly created) FunctionResult node.
    bool bReturnAdded = false;
    FString ReturnPinName;
    if (bWantReturn)
    {
        UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(NewGraph);
        if (Entry)
        {
            UK2Node_FunctionResult* Result = FBlueprintEditorUtils::FindOrCreateFunctionResultNode(Entry);  // VERIFY vs engine source
            if (Result)
            {
                TArray<UK2Node_EditablePinBase*> AllResults;
                for (UK2Node_FunctionResult* R : Result->GetAllResultNodes())   // VERIFY vs engine source
                {
                    if (R) { AllResults.Add(R); }
                }
                if (AllResults.Num() == 0) { AllResults.Add(Result); }
                const FName PinName = Result->CreateUniquePinName(TEXT("ReturnValue"));  // VERIFY vs engine source (UEdGraphNode::CreateUniquePinName)
                FString AddErr;
                UEdGraphPin* NewPin = MCPBpFn_AddPinAndReconstruct(Result, AllResults, PinName, ReturnType, EGPD_Input, AddErr);
                if (NewPin)
                {
                    bReturnAdded = true;
                    ReturnPinName = PinName.ToString();
                }
                // A failed return-pin add is non-fatal: the function graph still exists; report it.
            }
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);            // VERIFY vs engine source — NO per-call compile

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("blueprint_path"), BP->GetPathName());
    Root->SetStringField(TEXT("function"), NewGraph->GetName());
    Root->SetBoolField(TEXT("created"), true);
    Root->SetBoolField(TEXT("has_return"), bReturnAdded);
    if (bReturnAdded)
    {
        Root->SetStringField(TEXT("return_pin"), ReturnPinName);
        Root->SetObjectField(TEXT("return_type"), MCPBpFn_SerializePinType(ReturnType));
    }
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 2) AddFunctionInputJson — CreateUserDefinedPin on the FunctionEntry (EGPD_Output = a function INPUT).
//    Inverse: RemoveFunctionPinJson(function, pin, is_output=false).
// =====================================================================================================
FString UMCPReflectionLibrary::AddFunctionInputJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, const FString& TypeJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (PinName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("pin_name is empty"));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' has no entry node"), *FunctionName));
    }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!MCPBpFn_BuildPinType(TypeJson, PinType, BuildErr))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }

    if (Entry->UserDefinedPinExists(FName(*PinName)))                          // VERIFY vs engine source
    {
        return MCPBpFn_Err(FString::Printf(TEXT("input pin '%s' already exists on function '%s'"), *PinName, *FunctionName));
    }

    Entry->Modify();
    TArray<UK2Node_EditablePinBase*> One; One.Add(Entry);
    // Entry OUTPUT pins are the function's INPUT parameters.
    UEdGraphPin* NewPin = MCPBpFn_AddPinAndReconstruct(Entry, One, FName(*PinName), PinType, EGPD_Output, Err);
    if (!NewPin)
    {
        return MCPBpFn_Err(Err);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetStringField(TEXT("input"), NewPin->PinName.ToString());
    Root->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(PinType));
    Root->SetBoolField(TEXT("added"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 3) AddFunctionOutputJson — find/create the FunctionResult; CreateUserDefinedPin (EGPD_Input = OUTPUT).
//    Inverse: RemoveFunctionPinJson(function, pin, is_output=true).
// =====================================================================================================
FString UMCPReflectionLibrary::AddFunctionOutputJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, const FString& TypeJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (PinName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("pin_name is empty"));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' has no entry node"), *FunctionName));
    }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!MCPBpFn_BuildPinType(TypeJson, PinType, BuildErr))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }
    PinType.bIsReference = false; // output params are never pass-by-ref (mirrors OnAddNewOutputClicked)

    UK2Node_FunctionResult* Result = FBlueprintEditorUtils::FindOrCreateFunctionResultNode(Entry);  // VERIFY vs engine source
    if (!Result)
    {
        return MCPBpFn_Err(TEXT("could not find or create a function result node"));
    }
    if (Result->UserDefinedPinExists(FName(*PinName)))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("output pin '%s' already exists on function '%s'"), *PinName, *FunctionName));
    }

    // A function may legally have multiple result nodes; add the pin to EACH (mirrors GatherAllResultNodes).
    TArray<UK2Node_EditablePinBase*> AllResults;
    for (UK2Node_FunctionResult* R : Result->GetAllResultNodes())             // VERIFY vs engine source
    {
        if (R) { AllResults.Add(R); }
    }
    if (AllResults.Num() == 0) { AllResults.Add(Result); }

    // Result INPUT pins are the function's OUTPUT parameters.
    UEdGraphPin* NewPin = MCPBpFn_AddPinAndReconstruct(Result, AllResults, FName(*PinName), PinType, EGPD_Input, Err);
    if (!NewPin)
    {
        return MCPBpFn_Err(Err);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetStringField(TEXT("output"), NewPin->PinName.ToString());
    Root->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(PinType));
    Root->SetNumberField(TEXT("result_node_count"), AllResults.Num());
    Root->SetBoolField(TEXT("added"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 4) SetFunctionPropertiesJson — FunctionEntry ExtraFlags (pure/const/access) + category + tooltip/keywords
//    + arbitrary MetaData. PropsJson: {pure?:bool, const?:bool, access?:"public|protected|private",
//    category?:str, tooltip?:str, keywords?:str, metadata?:{k:v}}. Captures prior for the restore inverse.
//    Inverse: SetFunctionPropertiesJson(prior props).
// =====================================================================================================
FString UMCPReflectionLibrary::SetFunctionPropertiesJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PropsJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' has no entry node"), *FunctionName));
    }
    TSharedPtr<FJsonObject> Props = MCPBpFn_ParseObject(PropsJson);
    if (!Props.IsValid())
    {
        return MCPBpFn_Err(TEXT("props is not a JSON object"));
    }

    // ---- capture prior state (for the restore inverse) ----------------------------------------------
    const int32 PrevExtraFlags = Entry->GetExtraFlags();                       // VERIFY vs engine source
    const bool bPrevPure  = (PrevExtraFlags & FUNC_BlueprintPure) != 0;
    const bool bPrevConst = (PrevExtraFlags & FUNC_Const) != 0;
    const FString PrevAccess = MCPBpFn_AccessString(PrevExtraFlags & FUNC_AccessSpecifiers);
    const FString PrevCategory = Entry->MetaData.Category.ToString();          // VERIFY vs engine source (FKismetUserDeclaredFunctionMetadata::Category)
    const FString PrevTooltip = Entry->MetaData.ToolTip.ToString();
    const FString PrevKeywords = Entry->MetaData.Keywords.ToString();

    Entry->Modify();
    int32 ExtraFlags = PrevExtraFlags;

    // pure
    TSharedRef<FJsonObject> Applied = MakeShared<FJsonObject>();
    bool bTmp = false;
    if (Props->TryGetBoolField(TEXT("pure"), bTmp))
    {
        if (bTmp) { ExtraFlags |= FUNC_BlueprintPure; } else { ExtraFlags &= ~FUNC_BlueprintPure; }
        Applied->SetBoolField(TEXT("pure"), bTmp);
    }
    // const
    if (Props->TryGetBoolField(TEXT("const"), bTmp))
    {
        if (bTmp) { ExtraFlags |= FUNC_Const; } else { ExtraFlags &= ~FUNC_Const; }
        Applied->SetBoolField(TEXT("const"), bTmp);
    }
    // access specifier
    FString Access;
    if (Props->TryGetStringField(TEXT("access"), Access))
    {
        bool bValid = false;
        const uint32 Flag = MCPBpFn_AccessFlag(Access, bValid);
        if (!bValid)
        {
            return MCPBpFn_Err(FString::Printf(TEXT("bad access '%s' (expected public|protected|private)"), *Access));
        }
        ExtraFlags &= ~FUNC_AccessSpecifiers;
        ExtraFlags |= (int32)Flag;
        Applied->SetStringField(TEXT("access"), Access.ToLower());
    }
    Entry->SetExtraFlags(ExtraFlags);                                          // VERIFY vs engine source (masks off FUNC_Native)

    // category (via the engine helper so it lands on the graph/metadata consistently)
    FString Category;
    if (Props->TryGetStringField(TEXT("category"), Category))
    {
        FBlueprintEditorUtils::SetBlueprintFunctionOrMacroCategory(Graph, FText::FromString(Category), /*bDontRecompile*/true);  // VERIFY vs engine source
        Applied->SetStringField(TEXT("category"), Category);
    }
    // tooltip / keywords -> the entry node's declared-function metadata (persistent source of truth)
    FString Tooltip;
    if (Props->TryGetStringField(TEXT("tooltip"), Tooltip))
    {
        Entry->MetaData.ToolTip = FText::FromString(Tooltip);
        Applied->SetStringField(TEXT("tooltip"), Tooltip);
    }
    FString Keywords;
    if (Props->TryGetStringField(TEXT("keywords"), Keywords))
    {
        Entry->MetaData.Keywords = FText::FromString(Keywords);
        Applied->SetStringField(TEXT("keywords"), Keywords);
    }

    // arbitrary metadata map {k:v} -> FKismetUserDeclaredFunctionMetadata::SetMetaData. Capture prior for each
    // touched key so the inverse can restore (a key absent before is restored to "" — documented lossiness).
    TSharedRef<FJsonObject> PrevMeta = MakeShared<FJsonObject>();
    const TSharedPtr<FJsonObject>* MetaObj = nullptr;
    if (Props->TryGetObjectField(TEXT("metadata"), MetaObj) && MetaObj)
    {
        for (const auto& Pair : (*MetaObj)->Values)
        {
            FString Val;
            if (Pair.Value.IsValid() && Pair.Value->TryGetString(Val))
            {
                const FName Key(*Pair.Key);
                const FString Prior = Entry->MetaData.HasMetaData(Key) ? Entry->MetaData.GetMetaData(Key) : FString();  // VERIFY vs engine source
                PrevMeta->SetStringField(Pair.Key, Prior);
                // MoveTemp -> binds the SetMetaData(FName, FString&&) overload unambiguously (Val is unused after).
                Entry->MetaData.SetMetaData(Key, MoveTemp(Val));               // VERIFY vs engine source (FKismetUserDeclaredFunctionMetadata::SetMetaData)
            }
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetObjectField(TEXT("applied"), Applied);
    // Prior state — the Python side ledgers this as the restore props for the inverse.
    TSharedRef<FJsonObject> Prior = MakeShared<FJsonObject>();
    Prior->SetBoolField(TEXT("pure"), bPrevPure);
    Prior->SetBoolField(TEXT("const"), bPrevConst);
    Prior->SetStringField(TEXT("access"), PrevAccess);
    Prior->SetStringField(TEXT("category"), PrevCategory);
    Prior->SetStringField(TEXT("tooltip"), PrevTooltip);
    Prior->SetStringField(TEXT("keywords"), PrevKeywords);
    Prior->SetObjectField(TEXT("metadata"), PrevMeta);
    Root->SetObjectField(TEXT("prior"), Prior);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 5) CreateLocalVariableJson — FBlueprintEditorUtils::AddLocalVariable (function-scope local).
//    Inverse: RemoveLocalVariableJson(function, var).
// =====================================================================================================
FString UMCPReflectionLibrary::CreateLocalVariableJson(const FString& BlueprintPath, const FString& FunctionName, const FString& VarName, const FString& TypeJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (VarName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("var_name is empty"));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!MCPBpFn_BuildPinType(TypeJson, PinType, BuildErr))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }

    // Reject a duplicate up-front (AddLocalVariable would silently add a second entry otherwise).
    if (UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph))
    {
        for (const FBPVariableDescription& D : Entry->LocalVariables)          // VERIFY vs engine source (UK2Node_FunctionEntry::LocalVariables)
        {
            if (D.VarName == FName(*VarName))
            {
                return MCPBpFn_Err(FString::Printf(TEXT("local variable '%s' already exists in function '%s'"), *VarName, *FunctionName));
            }
        }
    }

    bool bOk = false;
    try
    {
        bOk = FBlueprintEditorUtils::AddLocalVariable(BP, Graph, FName(*VarName), PinType);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during AddLocalVariable"));
    }
    if (!bOk)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("AddLocalVariable failed (graph '%s' is not a function graph?)"), *FunctionName));
    }

    // AddLocalVariable already MarkBlueprintAsStructurallyModified's; call again for consistency (idempotent).
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetStringField(TEXT("local_variable"), VarName);
    Root->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(PinType));
    Root->SetBoolField(TEXT("created"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 6) DeleteBlueprintFunctionJson — RemoveGraph a function graph. Captures a LOSSY re-add spec (name +
//    signature pins only; NOT the body). Inverse (best-effort): CreateBlueprintFunctionGraphJson + pins.
// =====================================================================================================
FString UMCPReflectionLibrary::DeleteBlueprintFunctionJson(const FString& BlueprintPath, const FString& FunctionName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }

    // Capture the signature (entry outputs = inputs; result inputs = outputs) for a best-effort re-add.
    TSharedRef<FJsonObject> Captured = MakeShared<FJsonObject>();
    Captured->SetStringField(TEXT("function"), Graph->GetName());
    TArray<TSharedPtr<FJsonValue>> Inputs, Outputs;
    if (UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph))
    {
        for (const TSharedPtr<FUserPinInfo>& P : Entry->UserDefinedPins)       // VERIFY vs engine source (UK2Node_EditablePinBase::UserDefinedPins)
        {
            if (!P.IsValid()) { continue; }
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("name"), P->PinName.ToString());
            J->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(P->PinType));
            Inputs.Add(MakeShared<FJsonValueObject>(J));
        }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (UK2Node_FunctionResult* R = Cast<UK2Node_FunctionResult>(N))
            {
                for (const TSharedPtr<FUserPinInfo>& P : R->UserDefinedPins)
                {
                    if (!P.IsValid()) { continue; }
                    TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
                    J->SetStringField(TEXT("name"), P->PinName.ToString());
                    J->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(P->PinType));
                    Outputs.Add(MakeShared<FJsonValueObject>(J));
                }
                break; // primary result node's signature is authoritative
            }
        }
    }
    Captured->SetArrayField(TEXT("inputs"), Inputs);
    Captured->SetArrayField(TEXT("outputs"), Outputs);

    const FString RemovedName = Graph->GetName();
    try
    {
        // Default flags (Recompile|MarkTransient); we MarkStructurallyModified below and defer real compile.
        FBlueprintEditorUtils::RemoveGraph(BP, Graph, EGraphRemoveFlags::MarkTransient);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during RemoveGraph"));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), RemovedName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetObjectField(TEXT("captured"), Captured);
    Root->SetBoolField(TEXT("inverse_is_lossy"), true);   // body/wiring NOT captured; re-add is signature-only
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 7) OverrideBlueprintFunctionJson — override a parent UFUNCTION as a function graph
//    (mirrors UBlueprintEditorLibrary::AddFunctionOverride). Inverse: DeleteBlueprintFunctionJson.
// =====================================================================================================
FString UMCPReflectionLibrary::OverrideBlueprintFunctionJson(const FString& BlueprintPath, const FString& FunctionName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (FunctionName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("function_name is empty"));
    }

    FBlueprintEditorUtils::ConformImplementedInterfaces(BP);                   // VERIFY vs engine source

    UFunction* OverrideFunc = nullptr;
    UClass* const OverrideFuncClass = FBlueprintEditorUtils::GetOverrideFunctionClass(BP, FName(*FunctionName), &OverrideFunc);  // VERIFY vs engine source
    if (!OverrideFuncClass || !OverrideFunc)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' is not overridable on this blueprint"), *FunctionName));
    }

    // Already present as a function graph?
    if (UEdGraph* ExistingGraph = FindObject<UEdGraph>(BP, *FunctionName))
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("blueprint"), BP->GetName());
        Root->SetStringField(TEXT("function"), ExistingGraph->GetName());
        Root->SetBoolField(TEXT("created"), false);
        Root->SetBoolField(TEXT("already_present"), true);
        return MCPBpFn_Serialize(Root);
    }
    // Already overridden as an event node? (function-graph and event-node forms are mutually exclusive)
    if (FBlueprintEditorUtils::FindOverrideForFunction(BP, OverrideFuncClass, FName(*FunctionName)))  // VERIFY vs engine source
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' is already overridden as an EVENT node — remove that event first"), *FunctionName));
    }

    BP->Modify();
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(BP, FName(*FunctionName), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
    if (!NewGraph)
    {
        return MCPBpFn_Err(TEXT("CreateNewGraph returned null"));
    }
    try
    {
        // Passing the parent UClass as SignatureFromObject marks this an override (terminators inherit the
        // parent signature; a CallParentFunction node is emitted). bIsUserCreated=false.
        FBlueprintEditorUtils::AddFunctionGraph<UClass>(BP, NewGraph, /*bIsUserCreated*/false, OverrideFuncClass);  // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during AddFunctionGraph (override)"));
    }
    NewGraph->Modify();
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), NewGraph->GetName());
    Root->SetStringField(TEXT("override_source_class"), OverrideFuncClass->GetPathName());
    Root->SetBoolField(TEXT("created"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 8) CreateEventGraphJson — CreateNewGraph + bp->UbergraphPages.Add (mirrors AddEventNode's creation).
//    Inverse: DeleteEventGraphJson.
// =====================================================================================================
FString UMCPReflectionLibrary::CreateEventGraphJson(const FString& BlueprintPath, const FString& GraphName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (GraphName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("graph_name is empty"));
    }
    if (MCPBpFn_GraphNameInUse(BP, GraphName))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("a graph named '%s' already exists on this blueprint"), *GraphName));
    }

    BP->Modify();
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
        BP, FName(*GraphName), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());  // VERIFY vs engine source
    if (!NewGraph)
    {
        return MCPBpFn_Err(TEXT("CreateNewGraph returned null"));
    }
    BP->UbergraphPages.Add(NewGraph);                                         // VERIFY vs engine source (mirror FMCPBlueprintUtils::AddEventNode)
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), NewGraph->GetName());
    Root->SetBoolField(TEXT("created"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 9) RenameEventGraphJson — FBlueprintEditorUtils::RenameGraph an ubergraph page. Inverse: rename back.
// =====================================================================================================
FString UMCPReflectionLibrary::RenameEventGraphJson(const FString& BlueprintPath, const FString& OldName, const FString& NewName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (NewName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("new_name is empty"));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindUbergraph(BP, OldName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    if (MCPBpFn_GraphNameInUse(BP, NewName))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("a graph named '%s' already exists on this blueprint"), *NewName));
    }

    const FString ActualOld = Graph->GetName();
    try
    {
        FBlueprintEditorUtils::RenameGraph(Graph, NewName);                    // VERIFY vs engine source
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during RenameGraph"));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("old_name"), ActualOld);
    Root->SetStringField(TEXT("new_name"), Graph->GetName());
    Root->SetBoolField(TEXT("renamed"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 10) DeleteEventGraphJson — RemoveGraph an ubergraph page. LOSSY re-add (name only). Inverse (best-effort):
//     CreateEventGraphJson.
// =====================================================================================================
FString UMCPReflectionLibrary::DeleteEventGraphJson(const FString& BlueprintPath, const FString& GraphName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindUbergraph(BP, GraphName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }

    const FString RemovedName = Graph->GetName();
    const int32 NodeCount = Graph->Nodes.Num();
    try
    {
        FBlueprintEditorUtils::RemoveGraph(BP, Graph, EGraphRemoveFlags::MarkTransient);  // VERIFY vs engine source (removes from UbergraphPages)
    }
    catch (...)
    {
        return MCPBpFn_Err(TEXT("exception during RemoveGraph"));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("graph"), RemovedName);
    Root->SetNumberField(TEXT("removed_node_count"), NodeCount);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetBoolField(TEXT("inverse_is_lossy"), true);   // an empty ubergraph of the same name is re-created; nodes are NOT restored
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 11) AddEventDispatcherInputJson — add a param pin to a multicast-delegate dispatcher's SIGNATURE graph
//     entry (extends the shipped no-arg add_event_dispatcher). Inverse: RemoveEventDispatcherInputJson.
// =====================================================================================================
FString UMCPReflectionLibrary::AddEventDispatcherInputJson(const FString& BlueprintPath, const FString& DispatcherName, const FString& PinName, const FString& TypeJson)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    if (PinName.IsEmpty())
    {
        return MCPBpFn_Err(TEXT("pin_name is empty"));
    }
    UEdGraph* SigGraph = FBlueprintEditorUtils::GetDelegateSignatureGraphByName(BP, FName(*DispatcherName));  // VERIFY vs engine source
    if (!SigGraph)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("no event dispatcher named '%s' on blueprint '%s'"), *DispatcherName, *BP->GetName()));
    }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(SigGraph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("dispatcher '%s' signature graph has no entry node"), *DispatcherName));
    }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!MCPBpFn_BuildPinType(TypeJson, PinType, BuildErr))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }
    if (Entry->UserDefinedPinExists(FName(*PinName)))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("param '%s' already exists on dispatcher '%s'"), *PinName, *DispatcherName));
    }

    Entry->Modify();
    TArray<UK2Node_EditablePinBase*> One; One.Add(Entry);
    // Dispatcher signature params are entry OUTPUT pins (same as a function input).
    FString Err;
    UEdGraphPin* NewPin = MCPBpFn_AddPinAndReconstruct(Entry, One, FName(*PinName), PinType, EGPD_Output, Err);
    if (!NewPin)
    {
        return MCPBpFn_Err(Err);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("dispatcher"), DispatcherName);
    Root->SetStringField(TEXT("param"), NewPin->PinName.ToString());
    Root->SetObjectField(TEXT("type"), MCPBpFn_SerializePinType(PinType));
    Root->SetBoolField(TEXT("added"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 12) RemoveFunctionPinJson — inverse-support for AddFunctionInput/AddFunctionOutput. Removes a user-defined
//     pin from the entry (bIsOutput=false) or every result node (bIsOutput=true).
// =====================================================================================================
FString UMCPReflectionLibrary::RemoveFunctionPinJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, bool bIsOutput)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' has no entry node"), *FunctionName));
    }

    int32 Removed = 0;
    if (bIsOutput)
    {
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (UK2Node_FunctionResult* R = Cast<UK2Node_FunctionResult>(N))
            {
                if (R->UserDefinedPinExists(FName(*PinName)))
                {
                    R->Modify();
                    R->RemoveUserDefinedPinByName(FName(*PinName));            // VERIFY vs engine source
                    try { R->ReconstructNode(); } catch (...) {}
                    ++Removed;
                }
            }
        }
    }
    else
    {
        if (Entry->UserDefinedPinExists(FName(*PinName)))
        {
            Entry->Modify();
            Entry->RemoveUserDefinedPinByName(FName(*PinName));               // VERIFY vs engine source
            try { Entry->ReconstructNode(); } catch (...) {}
            ++Removed;
        }
    }

    if (Removed == 0)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("no %s pin '%s' on function '%s'"), bIsOutput ? TEXT("output") : TEXT("input"), *PinName, *FunctionName));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetStringField(TEXT("pin"), PinName);
    Root->SetBoolField(TEXT("is_output"), bIsOutput);
    Root->SetNumberField(TEXT("removed_count"), Removed);
    Root->SetBoolField(TEXT("removed"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 13) RemoveLocalVariableJson — inverse-support for CreateLocalVariable. Removes a local var from the
//     FunctionEntry LocalVariables array (mirrors the inner body of FBlueprintEditorUtils::RemoveLocalVariable).
// =====================================================================================================
FString UMCPReflectionLibrary::RemoveLocalVariableJson(const FString& BlueprintPath, const FString& FunctionName, const FString& VarName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    FString Err;
    UEdGraph* Graph = MCPBpFn_FindFunctionGraph(BP, FunctionName, Err);
    if (!Graph) { return MCPBpFn_Err(Err); }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(Graph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("function '%s' has no entry node"), *FunctionName));
    }

    // Prefer the engine helper with a resolved scope (it also cleans up any get/set nodes); fall back to a
    // direct array removal if the skeleton function isn't resolvable yet.
    const UStruct* Scope = nullptr;
    if (BP->SkeletonGeneratedClass)                                           // VERIFY vs engine source (UBlueprint::SkeletonGeneratedClass)
    {
        Scope = BP->SkeletonGeneratedClass->FindFunctionByName(FName(*FunctionName));
    }

    bool bRemoved = false;
    if (Scope)
    {
        const int32 Before = Entry->LocalVariables.Num();
        try
        {
            FBlueprintEditorUtils::RemoveLocalVariable(BP, Scope, FName(*VarName));  // VERIFY vs engine source
        }
        catch (...)
        {
            return MCPBpFn_Err(TEXT("exception during RemoveLocalVariable"));
        }
        bRemoved = Entry->LocalVariables.Num() < Before;
    }
    if (!bRemoved)
    {
        // Direct removal fallback (freshly-added local vars have no referencing nodes to clean up).
        for (int32 i = 0; i < Entry->LocalVariables.Num(); ++i)
        {
            if (Entry->LocalVariables[i].VarName == FName(*VarName))
            {
                Entry->Modify();
                Entry->LocalVariables.RemoveAt(i);
                bRemoved = true;
                break;
            }
        }
    }

    if (!bRemoved)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("no local variable '%s' in function '%s'"), *VarName, *FunctionName));
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("function"), Graph->GetName());
    Root->SetStringField(TEXT("local_variable"), VarName);
    Root->SetBoolField(TEXT("removed"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// 14) RemoveEventDispatcherInputJson — inverse-support for AddEventDispatcherInput. Removes a param pin from
//     the dispatcher's signature graph entry.
// =====================================================================================================
FString UMCPReflectionLibrary::RemoveEventDispatcherInputJson(const FString& BlueprintPath, const FString& DispatcherName, const FString& PinName)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPBpFn_LoadBlueprint(BlueprintPath);
    if (!BP)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath));
    }
    UEdGraph* SigGraph = FBlueprintEditorUtils::GetDelegateSignatureGraphByName(BP, FName(*DispatcherName));  // VERIFY vs engine source
    if (!SigGraph)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("no event dispatcher named '%s' on blueprint '%s'"), *DispatcherName, *BP->GetName()));
    }
    UK2Node_FunctionEntry* Entry = MCPBpFn_FindEntry(SigGraph);
    if (!Entry)
    {
        return MCPBpFn_Err(FString::Printf(TEXT("dispatcher '%s' signature graph has no entry node"), *DispatcherName));
    }
    if (!Entry->UserDefinedPinExists(FName(*PinName)))
    {
        return MCPBpFn_Err(FString::Printf(TEXT("no param '%s' on dispatcher '%s'"), *PinName, *DispatcherName));
    }

    Entry->Modify();
    Entry->RemoveUserDefinedPinByName(FName(*PinName));                        // VERIFY vs engine source
    try { Entry->ReconstructNode(); } catch (...) {}
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetName());
    Root->SetStringField(TEXT("dispatcher"), DispatcherName);
    Root->SetStringField(TEXT("param"), PinName);
    Root->SetBoolField(TEXT("removed"), true);
    return MCPBpFn_Serialize(Root);
#else
    return MCPBpFn_Err(TEXT("editor-only"));
#endif
}
