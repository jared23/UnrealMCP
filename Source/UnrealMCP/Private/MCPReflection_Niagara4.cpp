// ============================================================================
// MCPReflection_Niagara4.cpp  —  Niagara GRAPH / SCRIPT AUTHORING writers.
//   Six "graph/script authoring" spec features that stock Python cannot reach:
//     1) create_niagara_scratch_pad_module  (WRITE) — add a scratch-pad module /
//        dynamic-input script to a UNiagaraSystem (data-level: factory-init +
//        System.ScratchPadScripts.Add). NO system view model required.
//     2) create_niagara_module_asset        (WRITE) — create a standalone
//        UNiagaraScript module/dynamic-input/function ASSET via the engine's own
//        Niagara script factory (resolved by /Script path — no factory-class link).
//     3) build_niagara_graph                (WRITE) — batch create nodes + links in
//        a UNiagaraScript's editor graph.
//     4) add_niagara_graph_node             (WRITE) — add one node to a script graph.
//     5) delete_niagara_graph_node          (WRITE) — remove a node from a graph.
//     6) layout_niagara_graph               (WRITE) — self-contained columnar
//        auto-layout (no engine RelayoutGraph dependency — see NOTE below).
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18. **ISOLATED translation unit** (mirrors
// MCPReflection_Niagara2.cpp). Anon-namespace helpers are prefixed MCPNia4_
// (unique across the unity build). Implements DEFERRED MCPReflectionLibrary
// methods that niagara_graph_cpp.py hasattr-guards on; when these link, the
// Python tools auto-enable.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Every symbol referenced here is EITHER (a) already exported in the stock 5.8
//   headers (NIAGARA_API / NIAGARAEDITOR_API / ENGINE_API / UNREALED_API), OR
//   (b) a header-only inline / template / virtual dispatch. In particular:
//     * NODE CREATION uses FGraphNodeCreator<T> (EdGraph.h template) + virtual
//       AllocateDefaultPins() dispatch — the SAME proven pattern the shipping
//       BehaviorTree editor-graph handler already compiles/links in this plugin.
//       The only NiagaraEditor symbol it needs is each node's StaticClass(),
//       which UCLASS(MinimalAPI) DOES export. No non-exported NiagaraEditor
//       function is called.
//     * The Niagara SCRIPT FACTORY is resolved by /Script path string + invoked
//       through the UFactory::FactoryCreateNew VIRTUAL — so the (non-exported)
//       UNiagaraModuleScriptFactory class is never linked, only dispatched.
//     * LINKS use UEdGraphSchema::TryCreateConnection (ENGINE_API virtual, Niagara
//       schema dispatch) so incompatible pins are REJECTED, not silently wired.
//     * LAYOUT is fully self-contained (assigns NodePosX/Y) — the faithful engine
//       helper FNiagaraStackGraphUtilities::RelayoutGraph is NOT NIAGARAEDITOR_API
//       (NiagaraStackGraphUtilities.h:45); rather than patch the engine, this file
//       does its own columnar layering. (Optional export patch reported to coord.)
//
//   Confirmed 5.8 signatures (header:line verified against the source engine):
//     UNiagaraScript::GetLatestSource() -> UNiagaraScriptSourceBase* NIAGARA_API    NiagaraScript.h:1066
//     UNiagaraScript::GetUsage() const inline                                       NiagaraScript.h:990
//     UNiagaraScriptSource::NodeGraph (public UPROPERTY)                            NiagaraScriptSource.h:25
//     UNiagaraSystem::ScratchPadScripts (public editor-only UPROPERTY TArray)       NiagaraSystem.h:518
//     UNiagaraNodeFunctionCall::FunctionScript (public UPROPERTY)                   NiagaraNodeFunctionCall.h:62
//     UNiagaraNodeInput::Input / Usage (public UPROPERTY)                           NiagaraNodeInput.h:53/56
//     UNiagaraNodeOutput::GetUsage() inline                                         NiagaraNodeOutput.h:57
//     FNiagaraTypeDefinition::Get*Def() inline static                              NiagaraTypes.h:1027-1034
//     UFactory::FactoryCreateNew(UClass*,UObject*,FName,EObjectFlags,UObject*,FFeedbackContext*) UNREALED_API  Factory.h:133
//     UEdGraph::RemoveNode(UEdGraphNode*, bool) ENGINE_API                          EdGraph.h
//     UEdGraphNode::BreakAllNodeLinks() ENGINE_API                                  EdGraphNode.h
//     UEdGraphSchema::TryCreateConnection(UEdGraphPin*,UEdGraphPin*) const ENGINE_API EdGraphSchema.h
//     FGraphNodeCreator<T> (template)                                              EdGraph/EdGraph.h
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any
// miss (never crash). Writes MarkPackageDirty + NotifyGraphChanged; the caller
// (Python) drives compile/save. Reversible ops surface everything the inverse
// needs (created node guids / created asset path / prior node positions).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

// --- Reflection / core ---
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/Package.h"
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"
#include "Misc/Guid.h"
#include "Misc/FeedbackContext.h"            // GWarn

// --- Base EdGraph (Engine module — always linkable) ---
#include "EdGraph/EdGraph.h"                 // UEdGraph / FGraphNodeCreator<T>
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraph/EdGraphSchema.h"           // UEdGraphSchema::TryCreateConnection

// --- Asset factory (UnrealEd — already a dep) ---
#include "Factories/Factory.h"               // UFactory (invoked via virtual only)

// --- Niagara runtime (NIAGARA_API) ---
#include "NiagaraSystem.h"                   // UNiagaraSystem (ScratchPadScripts)
#include "NiagaraScript.h"                   // UNiagaraScript / ENiagaraScriptUsage
#include "NiagaraTypes.h"                    // FNiagaraVariable / FNiagaraTypeDefinition

// --- NiagaraEditor (NIAGARAEDITOR_API + MinimalAPI node StaticClass) ---
#include "NiagaraScriptSource.h"             // UNiagaraScriptSource (->NodeGraph)
#include "NiagaraGraph.h"                    // UNiagaraGraph
#include "NiagaraNode.h"                     // UNiagaraNode
#include "NiagaraNodeOutput.h"               // UNiagaraNodeOutput
#include "NiagaraNodeInput.h"                // UNiagaraNodeInput (public Input/Usage)
#include "NiagaraNodeFunctionCall.h"         // UNiagaraNodeFunctionCall (public FunctionScript)

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPNia4_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPNia4_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPNia4_Serialize(Obj);
    }

#if WITH_EDITOR
    // ---- Type mapping (mirrors the shipping NiagaraTypeFromName; the Get*Def()s are inline) ----
    bool MCPNia4_TypeFromName(const FString& In, FNiagaraTypeDefinition& Out)
    {
        const FString L = In.ToLower();
        if (L == TEXT("bool") || L == TEXT("boolean"))                      { Out = FNiagaraTypeDefinition::GetBoolDef();  return true; }
        if (L == TEXT("int")  || L == TEXT("int32") || L == TEXT("integer")){ Out = FNiagaraTypeDefinition::GetIntDef();   return true; }
        if (L == TEXT("float") || L == TEXT("double") || L == TEXT("real")) { Out = FNiagaraTypeDefinition::GetFloatDef(); return true; }
        if (L == TEXT("vector2") || L == TEXT("vec2") || L == TEXT("vector2d")) { Out = FNiagaraTypeDefinition::GetVec2Def(); return true; }
        if (L == TEXT("vector")  || L == TEXT("vec3") || L == TEXT("vector3"))  { Out = FNiagaraTypeDefinition::GetVec3Def(); return true; }
        if (L == TEXT("vector4") || L == TEXT("vec4"))                      { Out = FNiagaraTypeDefinition::GetVec4Def();  return true; }
        if (L == TEXT("linearcolor") || L == TEXT("color"))                 { Out = FNiagaraTypeDefinition::GetColorDef(); return true; }
        if (L == TEXT("quat") || L == TEXT("quaternion"))                   { Out = FNiagaraTypeDefinition::GetQuatDef();  return true; }
        if (L == TEXT("position"))                                          { Out = FNiagaraTypeDefinition::GetPositionDef(); return true; }
        return false;
    }

    // Map an input-node usage string to the enum (defaults to Parameter).
    ENiagaraInputNodeUsage MCPNia4_InputUsageFromName(const FString& In)
    {
        const FString L = In.ToLower();
        if (L == TEXT("systemconstant") || L == TEXT("system_constant")) { return ENiagaraInputNodeUsage::SystemConstant; }
        if (L == TEXT("attribute"))                                      { return ENiagaraInputNodeUsage::Attribute; }
        if (L == TEXT("translatorconstant") || L == TEXT("translator_constant")) { return ENiagaraInputNodeUsage::TranslatorConstant; }
        if (L == TEXT("rapiditerationparameter") || L == TEXT("rapid_iteration")) { return ENiagaraInputNodeUsage::RapidIterationParameter; }
        return ENiagaraInputNodeUsage::Parameter;
    }

    // Short type label from a UEdGraphNode UClass name (strip the "NiagaraNode" prefix if present).
    FString MCPNia4_NodeTypeShort(const UEdGraphNode* Node)
    {
        if (!Node) { return TEXT("None"); }
        FString ClassName = Node->GetClass()->GetName();
        FString Trimmed = ClassName;
        if (Trimmed.StartsWith(TEXT("NiagaraNode"))) { Trimmed = Trimmed.RightChop(11); }
        return Trimmed.IsEmpty() ? ClassName : Trimmed;
    }

    // Load a UNiagaraScript from a package/asset path (tolerates a bare package path).
    UNiagaraScript* MCPNia4_LoadScript(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        if (UNiagaraScript* S = Cast<UNiagaraScript>(FSoftObjectPath(Path).TryLoad())) { return S; }
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UNiagaraScript>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Resolve the editor graph of a standalone UNiagaraScript asset (module/dynamic-input/function/scratch).
    // This is the SAFE authoring surface (a self-contained script asset) — NOT an emitter's live compiled
    // stack graph. Returns nullptr + OutErr on any miss.
    UNiagaraGraph* MCPNia4_ResolveScriptGraph(const FString& ScriptPath, UNiagaraScript*& OutScript, FString& OutErr)
    {
        OutScript = MCPNia4_LoadScript(ScriptPath);
        if (!OutScript) { OutErr = FString::Printf(TEXT("could not load UNiagaraScript '%s'"), *ScriptPath); return nullptr; }
        UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(OutScript->GetLatestSource()); // NiagaraScript.h:1066
        if (!Source) { OutErr = TEXT("script source is not a graph-backed UNiagaraScriptSource"); return nullptr; }
        UNiagaraGraph* Graph = Source->NodeGraph;                                                // NiagaraScriptSource.h:25
        if (!Graph) { OutErr = TEXT("script source has no NodeGraph"); return nullptr; }
        return Graph;
    }

    // Resolve the Niagara script factory UClass by /Script path WITHOUT linking the (non-exported)
    // factory class. Returns nullptr if the module/class is not loaded (NiagaraEditor is always loaded
    // in an editor session). ScriptType (case-insensitive): module | dynamic_input | function.
    UClass* MCPNia4_ResolveFactoryClass(const FString& ScriptType, FString& OutErr)
    {
        const FString T = ScriptType.ToLower();
        const TCHAR* Path = nullptr;
        if (T == TEXT("module"))                                        { Path = TEXT("/Script/NiagaraEditor.NiagaraModuleScriptFactory"); }
        else if (T == TEXT("dynamic_input") || T == TEXT("dynamicinput")){ Path = TEXT("/Script/NiagaraEditor.NiagaraDynamicInputScriptFactory"); }
        else if (T == TEXT("function"))                                 { Path = TEXT("/Script/NiagaraEditor.NiagaraFunctionScriptFactory"); }
        else { OutErr = FString::Printf(TEXT("unknown script_type '%s' (want module|dynamic_input|function)"), *ScriptType); return nullptr; }

        UClass* FactoryClass = FindObject<UClass>(nullptr, Path);
        if (!FactoryClass) { FactoryClass = LoadObject<UClass>(nullptr, Path); }
        if (!FactoryClass) { OutErr = FString::Printf(TEXT("factory class not found: %s"), Path); return nullptr; }
        return FactoryClass;
    }

    // Create + init a UNiagaraScript via the engine's own factory (dispatched through UFactory virtual).
    // Outer = InParent (a package for a top-level asset, the system for a scratch pad script). Flags differ:
    // a top-level asset is RF_Public|RF_Standalone|RF_Transactional; a scratch-pad SUB-object drops
    // RF_Standalone (that flag is only for top-level assets). Returns nullptr on miss.
    UNiagaraScript* MCPNia4_FactoryCreateScript(UClass* FactoryClass, UObject* InParent, FName Name,
                                                EObjectFlags Flags, FString& OutErr)
    {
        if (!FactoryClass || !InParent) { OutErr = TEXT("null factory/parent"); return nullptr; }
        UFactory* Factory = NewObject<UFactory>(GetTransientPackage(), FactoryClass);
        if (!Factory) { OutErr = TEXT("failed to construct factory"); return nullptr; }
        // Factory.h:133 — 6-arg virtual. Copies the configured default template script + inits usage/graph.
        UObject* NewObj = Factory->FactoryCreateNew(
            UNiagaraScript::StaticClass(), InParent, Name, Flags, /*Context*/nullptr, GWarn);
        UNiagaraScript* Script = Cast<UNiagaraScript>(NewObj);
        if (!Script) { OutErr = TEXT("factory did not return a UNiagaraScript"); }
        return Script;
    }

    // Find one node in a graph by GUID.
    UEdGraphNode* MCPNia4_FindNodeByGuid(UNiagaraGraph* Graph, const FGuid& Guid)
    {
        if (!Graph) { return nullptr; }
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (N && N->NodeGuid == Guid) { return N; }
        }
        return nullptr;
    }

    // Find a pin on a node by name + direction; if Name is empty, return the FIRST pin of that direction.
    UEdGraphPin* MCPNia4_FindPin(UEdGraphNode* Node, const FString& Name, EEdGraphPinDirection Dir)
    {
        if (!Node) { return nullptr; }
        UEdGraphPin* FirstOfDir = nullptr;
        for (UEdGraphPin* P : Node->Pins)
        {
            if (!P || P->Direction != Dir) { continue; }
            if (!FirstOfDir) { FirstOfDir = P; }
            if (Name.IsEmpty()) { return FirstOfDir; }
            const FString PinName = P->PinName.ToString();
            if (PinName == Name || PinName.EndsWith(FString(TEXT(".")) + Name)) { return P; }
        }
        return Name.IsEmpty() ? FirstOfDir : nullptr;
    }

    // Create ONE node of a supported kind in Graph. Returns the created node (added to the graph) or nullptr.
    // Supported kinds (Public-header classes with a public defining property; safe to construct headless):
    //   "function_call" — requires spec["script_path"] (a UNiagaraScript to call).
    //   "input"         — requires spec["input_name"] + spec["input_type"]; optional spec["input_usage"].
    // Everything else is refused (returns nullptr + OutErr) rather than risk a crash on unvalidated setup.
    UNiagaraNode* MCPNia4_CreateNode(UNiagaraGraph* Graph, const TSharedPtr<FJsonObject>& Spec, FString& OutErr)
    {
        if (!Graph || !Spec.IsValid()) { OutErr = TEXT("null graph/spec"); return nullptr; }
        FString KindRaw;
        Spec->TryGetStringField(TEXT("kind"), KindRaw);
        const FString Kind = KindRaw.ToLower();

        int32 PosX = 0, PosY = 0;
        Spec->TryGetNumberField(TEXT("pos_x"), PosX);
        Spec->TryGetNumberField(TEXT("pos_y"), PosY);

        if (Kind == TEXT("function_call") || Kind == TEXT("function") || Kind == TEXT("module") || Kind == TEXT("dynamic_input"))
        {
            FString CalledPath;
            if (!Spec->TryGetStringField(TEXT("script_path"), CalledPath) || CalledPath.IsEmpty())
            {
                OutErr = TEXT("function_call node requires 'script_path'"); return nullptr;
            }
            UNiagaraScript* Called = MCPNia4_LoadScript(CalledPath);
            if (!Called) { OutErr = FString::Printf(TEXT("called script not found: %s"), *CalledPath); return nullptr; }

            FGraphNodeCreator<UNiagaraNodeFunctionCall> Creator(*Graph);          // EdGraph.h template
            UNiagaraNodeFunctionCall* Node = Creator.CreateNode(/*bSelectNewNode=*/false);
            Node->FunctionScript = Called;                                        // public UPROPERTY (NiagaraNodeFunctionCall.h:62)
            Node->NodePosX = PosX; Node->NodePosY = PosY;
            Creator.Finalize();                                                   // CreateNewGuid + PostPlacedNewNode + AllocateDefaultPins (reads Called's graph)
            return Node;
        }
        if (Kind == TEXT("input"))
        {
            FString InName, InType, InUsage;
            if (!Spec->TryGetStringField(TEXT("input_name"), InName) || InName.IsEmpty())
            {
                OutErr = TEXT("input node requires 'input_name'"); return nullptr;
            }
            Spec->TryGetStringField(TEXT("input_type"), InType);
            FNiagaraTypeDefinition Type;
            if (!MCPNia4_TypeFromName(InType, Type))
            {
                OutErr = FString::Printf(TEXT("input node has unsupported 'input_type' '%s'"), *InType); return nullptr;
            }
            Spec->TryGetStringField(TEXT("input_usage"), InUsage);

            FGraphNodeCreator<UNiagaraNodeInput> Creator(*Graph);
            UNiagaraNodeInput* Node = Creator.CreateNode(/*bSelectNewNode=*/false);
            Node->Input = FNiagaraVariable(Type, FName(*InName));                 // public UPROPERTY (NiagaraNodeInput.h:53)
            Node->Usage = MCPNia4_InputUsageFromName(InUsage);                    // public UPROPERTY (NiagaraNodeInput.h:56)
            Node->NodePosX = PosX; Node->NodePosY = PosY;
            Creator.Finalize();                                                   // AllocateDefaultPins builds the output pin from Input's type
            return Node;
        }

        OutErr = FString::Printf(TEXT("unsupported node kind '%s' (supported: function_call | input)"), *Kind);
        return nullptr;
    }

    // Self-contained columnar layout: column = longest chain to a sink following OUTPUT pins (memoized w/
    // in-progress cycle guard). X = (maxCol - col)*ColW so sinks/output sit right, sources left; Y stacks
    // within a column. Fills OutPrior with each moved node's prior (x,y).
    void MCPNia4_LayoutColumns(UNiagaraGraph* Graph, int32 ColW, int32 RowH,
                               TMap<FGuid, TPair<int32,int32>>& OutPrior)
    {
        TArray<UEdGraphNode*> Nodes;
        for (UEdGraphNode* N : Graph->Nodes) { if (N) { Nodes.Add(N); } }

        TMap<UEdGraphNode*, int32> ColMemo;
        TSet<UEdGraphNode*> InProgress;
        TFunction<int32(UEdGraphNode*)> ColOf = [&](UEdGraphNode* Node) -> int32
        {
            if (!Node) { return 0; }
            if (const int32* Found = ColMemo.Find(Node)) { return *Found; }
            if (InProgress.Contains(Node)) { return 0; }   // cycle guard
            InProgress.Add(Node);
            int32 Best = 0;
            for (UEdGraphPin* P : Node->Pins)
            {
                if (!P || P->Direction != EGPD_Output) { continue; }
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

        int32 MaxCol = 0;
        for (UEdGraphNode* N : Nodes) { MaxCol = FMath::Max(MaxCol, ColOf(N)); }

        TMap<int32, int32> RowCursor;   // column -> next row index
        for (UEdGraphNode* N : Nodes)
        {
            const int32 Col = ColMemo.FindRef(N);
            int32& Row = RowCursor.FindOrAdd(Col);
            OutPrior.Add(N->NodeGuid, TPair<int32,int32>(N->NodePosX, N->NodePosY));
            N->Modify();
            N->NodePosX = (MaxCol - Col) * ColW;
            N->NodePosY = Row * RowH;
            ++Row;
        }
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// 1) CreateNiagaraScratchPadModule(System, ScriptName, ScriptType)  [WRITE]
//    Add a scratch-pad module / dynamic-input script to a UNiagaraSystem, data-level.
//    Uses the engine Niagara script factory to fully initialize the script (usage +
//    default template graph w/ output node), outers it to the System, and appends it
//    to the public editor-only UNiagaraSystem::ScratchPadScripts array. NO system view
//    model required — when the system editor next opens, RefreshScriptViewModels()
//    rebuilds the scratch-pad view models from this array.
//    ScriptType (case-insensitive): module | dynamic_input.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::CreateNiagaraScratchPadModule(UNiagaraSystem* System, const FString& ScriptName,
    const FString& ScriptType)
{
#if WITH_EDITOR
    if (!System) { return MCPNia4_Error(TEXT("null system")); }

    const FString T = ScriptType.IsEmpty() ? TEXT("dynamic_input") : ScriptType.ToLower();
    if (T != TEXT("module") && T != TEXT("dynamic_input") && T != TEXT("dynamicinput"))
    {
        return MCPNia4_Error(FString::Printf(TEXT("unsupported script_type '%s' (want module|dynamic_input)"), *ScriptType));
    }

    FString Err;
    UClass* FactoryClass = MCPNia4_ResolveFactoryClass(T, Err);
    if (!FactoryClass) { return MCPNia4_Error(Err); }

    // Unique name within the system's scratch pad.
    FName BaseName = ScriptName.IsEmpty() ? FName(TEXT("ScratchModule")) : FName(*ScriptName);
    FName UniqueName = MakeUniqueObjectName(System, UNiagaraScript::StaticClass(), BaseName);

    // Scratch-pad script is a SUB-object of the system -> no RF_Standalone.
    UNiagaraScript* Script = MCPNia4_FactoryCreateScript(FactoryClass, System, UniqueName,
        RF_Public | RF_Transactional, Err);
    if (!Script) { return MCPNia4_Error(Err); }

    System->Modify();
    System->ScratchPadScripts.Add(Script);   // NiagaraSystem.h:518 — public editor-only UPROPERTY
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("script_name"), Script->GetName());
    Root->SetStringField(TEXT("script_path"), Script->GetPathName());
    Root->SetStringField(TEXT("script_type"), T);
    Root->SetNumberField(TEXT("scratch_pad_count"), System->ScratchPadScripts.Num());
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 2) CreateNiagaraModuleAsset(PackagePath, AssetName, ScriptType)  [WRITE]
//    Create a standalone UNiagaraScript ASSET (module/dynamic_input/function) via the
//    engine's own Niagara script factory. Does NOT save (mirrors CreateAnimLayerInterface:
//    the Python caller saves the returned package). ScriptType: module|dynamic_input|function.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::CreateNiagaraModuleAsset(const FString& PackagePath, const FString& AssetName,
    const FString& ScriptType)
{
#if WITH_EDITOR
    if (PackagePath.IsEmpty() || AssetName.IsEmpty())
    {
        return MCPNia4_Error(TEXT("PackagePath and AssetName are required"));
    }

    FString FullPackageName = PackagePath;
    if (!FullPackageName.EndsWith(TEXT("/"))) { FullPackageName += TEXT("/"); }
    FullPackageName += AssetName;

    if (FindPackage(nullptr, *FullPackageName) ||
        FindObject<UObject>(nullptr, *(FullPackageName + TEXT(".") + AssetName)))
    {
        return MCPNia4_Error(FString::Printf(TEXT("an asset already exists at '%s'"), *FullPackageName));
    }

    FString Err;
    UClass* FactoryClass = MCPNia4_ResolveFactoryClass(ScriptType.IsEmpty() ? TEXT("module") : ScriptType, Err);
    if (!FactoryClass) { return MCPNia4_Error(Err); }

    UPackage* Package = CreatePackage(*FullPackageName);
    if (!Package) { return MCPNia4_Error(TEXT("CreatePackage failed")); }
    Package->FullyLoad();

    // Top-level asset in its own package -> RF_Standalone.
    UNiagaraScript* Script = MCPNia4_FactoryCreateScript(FactoryClass, Package, FName(*AssetName),
        RF_Public | RF_Standalone | RF_Transactional, Err);
    if (!Script) { return MCPNia4_Error(Err); }

    Script->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("asset"), Script->GetName());
    Root->SetStringField(TEXT("package"), FullPackageName);
    Root->SetStringField(TEXT("asset_path"), Script->GetPathName());
    Root->SetStringField(TEXT("script_type"), (ScriptType.IsEmpty() ? TEXT("module") : ScriptType.ToLower()));
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 4) AddNiagaraGraphNode(ScriptPath, NodeSpecJson)  [WRITE]
//    Add ONE node to a standalone UNiagaraScript's editor graph.
//    NodeSpecJson: {"kind":"function_call","script_path":"/..."[,"pos_x","pos_y"]}
//               or {"kind":"input","input_name":"X","input_type":"float"[,"input_usage","pos_x","pos_y"]}
//    Inverse: DeleteNiagaraGraphNode(ScriptPath, <returned node_guid>).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::AddNiagaraGraphNode(const FString& ScriptPath, const FString& NodeSpecJson)
{
#if WITH_EDITOR
    UNiagaraScript* Script = nullptr;
    FString Err;
    UNiagaraGraph* Graph = MCPNia4_ResolveScriptGraph(ScriptPath, Script, Err);
    if (!Graph) { return MCPNia4_Error(Err); }

    TSharedPtr<FJsonObject> Spec;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(NodeSpecJson);
    if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
    {
        return MCPNia4_Error(TEXT("node_spec_json is not a valid JSON object"));
    }

    Graph->Modify();
    UNiagaraNode* Node = MCPNia4_CreateNode(Graph, Spec, Err);
    if (!Node) { return MCPNia4_Error(Err); }
    Graph->NotifyGraphChanged();
    Script->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("script"), Script->GetName());
    Root->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
    Root->SetStringField(TEXT("node_class"), Node->GetClass()->GetName());
    Root->SetStringField(TEXT("node_type"), MCPNia4_NodeTypeShort(Node));
    Root->SetNumberField(TEXT("node_count"), Graph->Nodes.Num());
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 3) BuildNiagaraGraph(ScriptPath, SpecJson)  [WRITE]
//    Batch create nodes + links in a standalone UNiagaraScript's editor graph.
//    SpecJson: {"nodes":[{"id":"a","kind":"input",...},{"id":"b","kind":"function_call",...}],
//               "links":[{"from_node":"a","from_pin":"","to_node":"b","to_pin":""}],
//               "layout":true }
//    Links use the Niagara schema (TryCreateConnection) so incompatible pins are rejected, not
//    silently wired. Failed nodes/links are reported (never crash). Inverse: delete each
//    returned created node_guid.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::BuildNiagaraGraph(const FString& ScriptPath, const FString& SpecJson)
{
#if WITH_EDITOR
    UNiagaraScript* Script = nullptr;
    FString Err;
    UNiagaraGraph* Graph = MCPNia4_ResolveScriptGraph(ScriptPath, Script, Err);
    if (!Graph) { return MCPNia4_Error(Err); }

    TSharedPtr<FJsonObject> Spec;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SpecJson);
    if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
    {
        return MCPNia4_Error(TEXT("spec_json is not a valid JSON object"));
    }

    const UEdGraphSchema* Schema = Graph->GetSchema();   // Niagara schema (dispatched)
    Graph->Modify();

    TMap<FString, UNiagaraNode*> IdToNode;
    TArray<TSharedPtr<FJsonValue>> CreatedArr;
    TArray<TSharedPtr<FJsonValue>> NodeErrArr;

    const TArray<TSharedPtr<FJsonValue>>* NodesJson = nullptr;
    if (Spec->TryGetArrayField(TEXT("nodes"), NodesJson) && NodesJson)
    {
        for (const TSharedPtr<FJsonValue>& V : *NodesJson)
        {
            const TSharedPtr<FJsonObject>* NodeObjPtr = nullptr;
            if (!V.IsValid() || !V->TryGetObject(NodeObjPtr) || !NodeObjPtr) { continue; }
            const TSharedPtr<FJsonObject> NodeObj = *NodeObjPtr;

            FString LocalId;
            NodeObj->TryGetStringField(TEXT("id"), LocalId);

            FString NodeErr;
            UNiagaraNode* Node = MCPNia4_CreateNode(Graph, NodeObj, NodeErr);
            if (!Node)
            {
                TSharedRef<FJsonObject> EObj = MakeShared<FJsonObject>();
                EObj->SetStringField(TEXT("id"), LocalId);
                EObj->SetStringField(TEXT("error"), NodeErr);
                NodeErrArr.Add(MakeShared<FJsonValueObject>(EObj));
                continue;
            }
            if (!LocalId.IsEmpty()) { IdToNode.Add(LocalId, Node); }

            TSharedRef<FJsonObject> CObj = MakeShared<FJsonObject>();
            CObj->SetStringField(TEXT("id"), LocalId);
            CObj->SetStringField(TEXT("node_guid"), Node->NodeGuid.ToString());
            CObj->SetStringField(TEXT("node_type"), MCPNia4_NodeTypeShort(Node));
            CreatedArr.Add(MakeShared<FJsonValueObject>(CObj));
        }
    }

    // Links: resolve endpoints by local id (or by node_guid if given), then TryCreateConnection.
    int32 LinksMade = 0;
    TArray<TSharedPtr<FJsonValue>> LinkErrArr;
    const TArray<TSharedPtr<FJsonValue>>* LinksJson = nullptr;
    if (Spec->TryGetArrayField(TEXT("links"), LinksJson) && LinksJson)
    {
        for (const TSharedPtr<FJsonValue>& V : *LinksJson)
        {
            const TSharedPtr<FJsonObject>* LinkObjPtr = nullptr;
            if (!V.IsValid() || !V->TryGetObject(LinkObjPtr) || !LinkObjPtr) { continue; }
            const TSharedPtr<FJsonObject> LinkObj = *LinkObjPtr;

            FString FromId, ToId, FromPinName, ToPinName, FromGuidStr, ToGuidStr;
            LinkObj->TryGetStringField(TEXT("from_node"), FromId);
            LinkObj->TryGetStringField(TEXT("to_node"), ToId);
            LinkObj->TryGetStringField(TEXT("from_pin"), FromPinName);
            LinkObj->TryGetStringField(TEXT("to_pin"), ToPinName);
            LinkObj->TryGetStringField(TEXT("from_node_guid"), FromGuidStr);
            LinkObj->TryGetStringField(TEXT("to_node_guid"), ToGuidStr);

            UEdGraphNode* FromNode = IdToNode.FindRef(FromId);
            UEdGraphNode* ToNode = IdToNode.FindRef(ToId);
            FGuid G;
            if (!FromNode && !FromGuidStr.IsEmpty() && FGuid::Parse(FromGuidStr, G)) { FromNode = MCPNia4_FindNodeByGuid(Graph, G); }
            if (!ToNode && !ToGuidStr.IsEmpty() && FGuid::Parse(ToGuidStr, G)) { ToNode = MCPNia4_FindNodeByGuid(Graph, G); }

            auto AddLinkErr = [&](const FString& Why)
            {
                TSharedRef<FJsonObject> EObj = MakeShared<FJsonObject>();
                EObj->SetStringField(TEXT("from_node"), FromId);
                EObj->SetStringField(TEXT("to_node"), ToId);
                EObj->SetStringField(TEXT("error"), Why);
                LinkErrArr.Add(MakeShared<FJsonValueObject>(EObj));
            };

            if (!FromNode || !ToNode) { AddLinkErr(TEXT("could not resolve from_node/to_node")); continue; }
            UEdGraphPin* FromPin = MCPNia4_FindPin(FromNode, FromPinName, EGPD_Output);
            UEdGraphPin* ToPin = MCPNia4_FindPin(ToNode, ToPinName, EGPD_Input);
            if (!FromPin || !ToPin) { AddLinkErr(TEXT("could not resolve from_pin/to_pin")); continue; }

            if (Schema && Schema->TryCreateConnection(FromPin, ToPin)) { ++LinksMade; }
            else { AddLinkErr(TEXT("schema rejected the connection (incompatible pins)")); }
        }
    }

    Graph->NotifyGraphChanged();
    Script->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("script"), Script->GetName());
    Root->SetNumberField(TEXT("nodes_created"), CreatedArr.Num());
    Root->SetArrayField(TEXT("created"), CreatedArr);
    Root->SetArrayField(TEXT("node_errors"), NodeErrArr);
    Root->SetNumberField(TEXT("links_made"), LinksMade);
    Root->SetArrayField(TEXT("link_errors"), LinkErrArr);
    Root->SetNumberField(TEXT("node_count"), Graph->Nodes.Num());
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 5) DeleteNiagaraGraphNode(ScriptPath, NodeGuid)  [WRITE]
//    Remove a node from a standalone UNiagaraScript's editor graph (breaks its links
//    first). REFUSES to delete a UNiagaraNodeOutput (removing it would break the whole
//    script). Modify()-wrapped so the editor's native undo can restore it; returns the
//    removed node's descriptor for the ledger.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::DeleteNiagaraGraphNode(const FString& ScriptPath, const FString& NodeGuid)
{
#if WITH_EDITOR
    UNiagaraScript* Script = nullptr;
    FString Err;
    UNiagaraGraph* Graph = MCPNia4_ResolveScriptGraph(ScriptPath, Script, Err);
    if (!Graph) { return MCPNia4_Error(Err); }

    FGuid Target;
    if (!FGuid::Parse(NodeGuid, Target)) { return MCPNia4_Error(TEXT("node_guid is not a valid GUID")); }

    UEdGraphNode* Node = MCPNia4_FindNodeByGuid(Graph, Target);
    if (!Node) { return MCPNia4_Error(FString::Printf(TEXT("no node with guid '%s' in the graph"), *NodeGuid)); }

    if (Node->IsA(UNiagaraNodeOutput::StaticClass()))
    {
        return MCPNia4_Error(TEXT("refusing to delete the graph's output node (would invalidate the script)"));
    }

    const FString NodeClass = Node->GetClass()->GetName();
    const FString NodeType = MCPNia4_NodeTypeShort(Node);
    const int32 PosX = Node->NodePosX, PosY = Node->NodePosY;

    Node->Modify();
    Graph->Modify();
    Node->BreakAllNodeLinks();                 // ENGINE_API
    Graph->RemoveNode(Node);                    // ENGINE_API (bBreakAllLinks defaults true)
    Graph->NotifyGraphChanged();
    Script->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("script"), Script->GetName());
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("node_guid"), NodeGuid);
    Root->SetStringField(TEXT("node_class"), NodeClass);
    Root->SetStringField(TEXT("node_type"), NodeType);
    Root->SetNumberField(TEXT("pos_x"), PosX);
    Root->SetNumberField(TEXT("pos_y"), PosY);
    Root->SetNumberField(TEXT("node_count"), Graph->Nodes.Num());
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 6) LayoutNiagaraGraph(ScriptPath, OptionsJson)  [WRITE]
//    Auto-layout a standalone UNiagaraScript's editor graph. Self-contained columnar
//    layering (output/sinks right, sources left). OptionsJson (all optional):
//      {"column_width":300,"row_height":140,"restore_positions":{"<guid>":[x,y],...}}
//    If "restore_positions" is given, those exact positions are applied instead (this
//    is the inverse path). Returns the prior positions so the caller can undo.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::LayoutNiagaraGraph(const FString& ScriptPath, const FString& OptionsJson)
{
#if WITH_EDITOR
    UNiagaraScript* Script = nullptr;
    FString Err;
    UNiagaraGraph* Graph = MCPNia4_ResolveScriptGraph(ScriptPath, Script, Err);
    if (!Graph) { return MCPNia4_Error(Err); }

    TSharedPtr<FJsonObject> Opts;
    if (!OptionsJson.IsEmpty())
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(OptionsJson);
        FJsonSerializer::Deserialize(Reader, Opts);   // tolerate absent/invalid -> defaults
    }

    int32 ColW = 300, RowH = 140;
    if (Opts.IsValid())
    {
        Opts->TryGetNumberField(TEXT("column_width"), ColW);
        Opts->TryGetNumberField(TEXT("row_height"), RowH);
    }

    TMap<FGuid, TPair<int32,int32>> Prior;

    // Inverse path: explicit restore_positions.
    const TSharedPtr<FJsonObject>* RestorePtr = nullptr;
    if (Opts.IsValid() && Opts->TryGetObjectField(TEXT("restore_positions"), RestorePtr) && RestorePtr)
    {
        int32 Moved = 0;
        for (const auto& Pair : (*RestorePtr)->Values)
        {
            FGuid G;
            if (!FGuid::Parse(Pair.Key, G)) { continue; }
            UEdGraphNode* Node = MCPNia4_FindNodeByGuid(Graph, G);
            const TArray<TSharedPtr<FJsonValue>>* XY = nullptr;
            if (!Node || !Pair.Value.IsValid() || !Pair.Value->TryGetArray(XY) || !XY || XY->Num() < 2) { continue; }
            Prior.Add(G, TPair<int32,int32>(Node->NodePosX, Node->NodePosY));
            Node->Modify();
            Node->NodePosX = (int32)(*XY)[0]->AsNumber();
            Node->NodePosY = (int32)(*XY)[1]->AsNumber();
            ++Moved;
        }
        Graph->NotifyGraphChanged();
        Script->MarkPackageDirty();

        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("script"), Script->GetName());
        Root->SetStringField(TEXT("mode"), TEXT("restore"));
        Root->SetNumberField(TEXT("nodes_moved"), Moved);
        return MCPNia4_Serialize(Root);
    }

    // Normal path: compute a columnar layout.
    Graph->Modify();
    MCPNia4_LayoutColumns(Graph, ColW, RowH, Prior);
    Graph->NotifyGraphChanged();
    Script->MarkPackageDirty();

    TSharedRef<FJsonObject> PriorObj = MakeShared<FJsonObject>();
    for (const auto& P : Prior)
    {
        TArray<TSharedPtr<FJsonValue>> XY;
        XY.Add(MakeShared<FJsonValueNumber>(P.Value.Key));
        XY.Add(MakeShared<FJsonValueNumber>(P.Value.Value));
        PriorObj->SetArrayField(P.Key.ToString(), XY);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("script"), Script->GetName());
    Root->SetStringField(TEXT("mode"), TEXT("layout"));
    Root->SetNumberField(TEXT("nodes_moved"), Prior.Num());
    Root->SetNumberField(TEXT("column_width"), ColW);
    Root->SetNumberField(TEXT("row_height"), RowH);
    Root->SetObjectField(TEXT("prior_positions"), PriorObj);   // for the undo ledger
    return MCPNia4_Serialize(Root);
#else
    return MCPNia4_Error(TEXT("editor-only"));
#endif
}
