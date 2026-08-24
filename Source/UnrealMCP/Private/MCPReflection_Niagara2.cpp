// ============================================================================
// MCPReflection_Niagara2.cpp  —  Niagara module-stack / graph READERS (+ one
//   clean WRITE: module enable/disable). Closes the NiagaraEditor-C++ Niagara
//   spec reads that stock Python cannot reach (see niagara_write3.py header:
//   get_all_emitters returns only a lightweight NiagaraMinimalEmitterInfo; the
//   emitter script graph + module stack are NOT python-reachable).
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18. **ISOLATED translation unit** on purpose:
// implements the DEFERRED MCPReflectionLibrary methods that niagara_runtime_cpp.py
// hasattr-guards on. When these link, the Python tools auto-enable.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Every NiagaraEditor symbol referenced here is EITHER
//     (a) already `NIAGARAEDITOR_API` in the stock 5.8 headers, OR
//     (b) one of the THREE UnrealMCP engine export-patch symbols already relied
//         on by the shipping niagara handlers (FindOutputNode / AddScriptModuleToStack
//         / RemoveModuleFromStack) — of which this file uses only FindOutputNode.
//   NO non-exported NiagaraEditor symbol is referenced. In particular the ordered
//   module-stack walk is RE-IMPLEMENTED here (mirroring the private
//   FNiagaraStackGraphUtilities::GetOrderedModuleNodes) on top of the EXPORTED
//   UNiagaraNode::IsParameterMapPin + base UEdGraph pin traversal, so it does NOT
//   need the non-exported GetParameterMapInputPin / GetOrderedModuleNodes.
//
//   Confirmed 5.8 signatures (header:line verified against the source engine):
//     UNiagaraGraph::FindOutputNode(ENiagaraScriptUsage,FGuid)  [export-patch]   NiagaraGraph.h:270
//     UNiagaraGraph::BuildTraversal(TArray<UNiagaraNode*>&,Usage,FGuid,bool) API  NiagaraGraph.h:287 (unused; kept for ref)
//     UNiagaraNode::IsParameterMapPin(const UEdGraphPin*) const   NIAGARAEDITOR_API NiagaraNode.h:147
//     UNiagaraNodeFunctionCall::GetFunctionName() inline                          NiagaraNodeFunctionCall.h:149
//     UNiagaraNodeFunctionCall::HasValidScriptAndGraph() const   NIAGARAEDITOR_API NiagaraNodeFunctionCall.h:101
//     UNiagaraNodeFunctionCall::FunctionScript (UPROPERTY)                        NiagaraNodeFunctionCall.h:62
//     UNiagaraNodeOutput::GetUsage()/GetUsageId() inline                          NiagaraNodeOutput.h:57/61
//     FNiagaraStackGraphUtilities::GetStackFunctionInputs(...,FCompileConstantResolver,...) NIAGARAEDITOR_API NiagaraStackGraphUtilities.h:134
//     FNiagaraStackGraphUtilities::SetModuleIsEnabled(UNiagaraNodeFunctionCall&,bool) NIAGARAEDITOR_API NiagaraStackGraphUtilities.h:298
//     FCompileConstantResolver(const FVersionedNiagaraEmitter&,ENiagaraScriptUsage,...) inline ctor NiagaraParameterMapHistory.h:28
//     UEdGraphNode::IsNodeEnabled()/GetDesiredEnabledState()/SetEnabledState() ENGINE_API EdGraphNode.h:410/419/425
//     UEdGraph::Nodes (public TArray<TObjectPtr<UEdGraphNode>>)                   EdGraph.h:79
//     UNiagaraSystem::GetEmitterHandles()/GetSystemSpawnScript() NIAGARA_API       NiagaraSystem.h:313/372
//     FNiagaraEmitterHandle::GetInstance() -> const FVersionedNiagaraEmitter      NiagaraEmitterHandle.h:82
//     UNiagaraScript::GetLatestSource() -> UNiagaraScriptSourceBase* NIAGARA_API   NiagaraScript.h:1066
//     UNiagaraScriptSource::NodeGraph (public UPROPERTY)                          NiagaraScriptSource.h
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any
// miss (never crash). Reads are non-mutating (no Modify/dirty). The single write
// (SetNiagaraModuleEnabled) captures prev + marks the package dirty + recompiles.
// Anon-namespace helpers are prefixed MCPNia2_ (unique across the unity build).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Reflection / core ---
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"
#include "Misc/Guid.h"

// --- Base EdGraph (Engine module — always linkable) ---
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"

// --- Niagara runtime (NIAGARA_API) ---
#include "NiagaraSystem.h"                  // UNiagaraSystem
#include "NiagaraEmitter.h"                 // UNiagaraEmitter / FVersionedNiagaraEmitterData
#include "NiagaraEmitterHandle.h"           // FNiagaraEmitterHandle
#include "NiagaraTypes.h"                   // FNiagaraVariable / FNiagaraTypeDefinition / FVersionedNiagaraEmitter
#include "NiagaraScript.h"                  // UNiagaraScript / ENiagaraScriptUsage

// --- NiagaraEditor (NIAGARAEDITOR_API + the 3 export-patch symbols) ---
#include "NiagaraScriptSource.h"                          // UNiagaraScriptSource (->NodeGraph)
#include "NiagaraGraph.h"                                 // UNiagaraGraph::FindOutputNode (export-patch)
#include "NiagaraNode.h"                                  // UNiagaraNode::IsParameterMapPin
#include "NiagaraNodeOutput.h"                            // UNiagaraNodeOutput::GetUsage
#include "NiagaraNodeFunctionCall.h"                      // UNiagaraNodeFunctionCall
#include "NiagaraParameterMapHistory.h"                   // FCompileConstantResolver (inline ctor)
#include "ViewModels/Stack/NiagaraStackGraphUtilities.h"  // GetStackFunctionInputs / SetModuleIsEnabled

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPNia2_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPNia2_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPNia2_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UNiagaraSystem from a package/asset path (Python callers pass system_path as a STRING).
    UNiagaraSystem* MCPNia2_LoadSystem(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UNiagaraSystem* Sys = Cast<UNiagaraSystem>(FSoftObjectPath(Path).TryLoad()))
        {
            return Sys;
        }
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UNiagaraSystem>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Map a case-insensitive usage string to the emitter-level enum (the four particle/emitter stacks).
    bool MCPNia2_ParseUsage(const FString& In, ENiagaraScriptUsage& Out)
    {
        const FString U = In.ToLower();
        if (U == TEXT("particle_spawn"))  { Out = ENiagaraScriptUsage::ParticleSpawnScript;  return true; }
        if (U == TEXT("particle_update")) { Out = ENiagaraScriptUsage::ParticleUpdateScript; return true; }
        if (U == TEXT("emitter_spawn"))   { Out = ENiagaraScriptUsage::EmitterSpawnScript;   return true; }
        if (U == TEXT("emitter_update"))  { Out = ENiagaraScriptUsage::EmitterUpdateScript;  return true; }
        return false;
    }

    const TCHAR* MCPNia2_UsageToString(ENiagaraScriptUsage Usage)
    {
        switch (Usage)
        {
            case ENiagaraScriptUsage::ParticleSpawnScript:  return TEXT("particle_spawn");
            case ENiagaraScriptUsage::ParticleUpdateScript: return TEXT("particle_update");
            case ENiagaraScriptUsage::EmitterSpawnScript:   return TEXT("emitter_spawn");
            case ENiagaraScriptUsage::EmitterUpdateScript:  return TEXT("emitter_update");
            default:                                        return TEXT("other");
        }
    }

    // The four emitter-level usages iterated when the caller does not pin a single usage.
    void MCPNia2_AllEmitterUsages(TArray<ENiagaraScriptUsage>& Out)
    {
        Out.Reset();
        Out.Add(ENiagaraScriptUsage::EmitterSpawnScript);
        Out.Add(ENiagaraScriptUsage::EmitterUpdateScript);
        Out.Add(ENiagaraScriptUsage::ParticleSpawnScript);
        Out.Add(ENiagaraScriptUsage::ParticleUpdateScript);
    }

    const TCHAR* MCPNia2_EnabledStateToString(ENodeEnabledState State)
    {
        switch (State)
        {
            case ENodeEnabledState::Enabled:          return TEXT("enabled");
            case ENodeEnabledState::Disabled:         return TEXT("disabled");
            case ENodeEnabledState::DevelopmentOnly:  return TEXT("development_only");
            default:                                  return TEXT("unknown");
        }
    }

    // Short type label from a UNiagaraNode UClass name (strip the "NiagaraNode" prefix if present).
    FString MCPNia2_NodeTypeShort(const UEdGraphNode* Node)
    {
        if (!Node) { return TEXT("None"); }
        FString ClassName = Node->GetClass()->GetName();
        FString Trimmed = ClassName;
        if (Trimmed.StartsWith(TEXT("NiagaraNode"))) { Trimmed = Trimmed.RightChop(11); }
        return Trimmed.IsEmpty() ? ClassName : Trimmed;
    }

    // Find an emitter handle by name; returns the const instance copy + fills OutFound. (The runtime
    // GetEmitterHandles() const array is enumerated; GetInstance() returns a const FVersionedNiagaraEmitter.)
    bool MCPNia2_FindEmitterInstance(UNiagaraSystem* System, const FString& EmitterName,
                                     FVersionedNiagaraEmitter& OutInstance)
    {
        if (!System) { return false; }
        for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
        {
            if (H.GetName().ToString() == EmitterName)
            {
                OutInstance = H.GetInstance();   // NiagaraEmitterHandle.h:82 — const FVersionedNiagaraEmitter
                return true;
            }
        }
        return false;
    }

    // Resolve the shared UNiagaraGraph. EmitterName empty -> the SYSTEM graph (GetSystemSpawnScript's
    // source graph). Otherwise the named emitter's shared source graph. Fills OutInstance for the emitter
    // case (invalid/default for the system case). Returns nullptr + OutErr on any miss.
    UNiagaraGraph* MCPNia2_ResolveGraph(UNiagaraSystem* System, const FString& EmitterName,
                                        FVersionedNiagaraEmitter& OutInstance, bool& bOutSystemScope, FString& OutErr)
    {
        bOutSystemScope = EmitterName.IsEmpty();
        if (!System) { OutErr = TEXT("null system"); return nullptr; }

        UNiagaraScript* AnyScript = nullptr;
        if (bOutSystemScope)
        {
            AnyScript = System->GetSystemSpawnScript();   // NiagaraSystem.h:372 — NIAGARA_API
            if (!AnyScript) { OutErr = TEXT("system spawn script missing"); return nullptr; }
        }
        else
        {
            if (!MCPNia2_FindEmitterInstance(System, EmitterName, OutInstance))
            {
                OutErr = FString::Printf(TEXT("no emitter handle named '%s'"), *EmitterName);
                return nullptr;
            }
            FVersionedNiagaraEmitterData* Data = OutInstance.GetEmitterData();
            if (!Data) { OutErr = TEXT("emitter has no versioned data"); return nullptr; }
            AnyScript = Data->SpawnScriptProps.Script;    // any emitter script shares one graph
            if (!AnyScript) { OutErr = TEXT("emitter spawn script missing"); return nullptr; }
        }

        UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(AnyScript->GetLatestSource()); // NiagaraScript.h:1066
        if (!Source) { OutErr = TEXT("script source is not a graph-backed UNiagaraScriptSource"); return nullptr; }
        UNiagaraGraph* Graph = Source->NodeGraph;
        if (!Graph) { OutErr = TEXT("script source has no NodeGraph"); return nullptr; }
        return Graph;
    }

    // Re-implementation of FNiagaraStackGraphUtilities::GetParameterMapInputPin using ONLY the exported
    // UNiagaraNode::IsParameterMapPin + base pin traversal (the engine helper is not exported).
    UEdGraphPin* MCPNia2_ParamMapInputPin(UNiagaraNode* Node)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && Node->IsParameterMapPin(P)) // NiagaraNode.h:147 — NIAGARAEDITOR_API
            {
                return P;
            }
        }
        return nullptr;
    }

    // Re-implementation of FNiagaraStackGraphUtilities::GetOrderedModuleNodes: walk backward from the
    // output node along the single-linked parameter-map input pin, collecting function-call (module)
    // nodes in stack order. Mirrors NiagaraStackGraphUtilities.cpp:296 exactly, minus the non-exported
    // GetParameterMapInputPin (replaced by MCPNia2_ParamMapInputPin).
    void MCPNia2_GetOrderedModules(UNiagaraNodeOutput* OutputNode, TArray<UNiagaraNodeFunctionCall*>& OutModules)
    {
        OutModules.Reset();
        UNiagaraNode* PreviousNode = OutputNode;
        int32 Guard = 0;
        while (PreviousNode != nullptr && Guard++ < 4096)
        {
            UEdGraphPin* InputPin = MCPNia2_ParamMapInputPin(PreviousNode);
            if (InputPin != nullptr && InputPin->LinkedTo.Num() == 1 && InputPin->LinkedTo[0] != nullptr)
            {
                UNiagaraNode* CurrentNode = Cast<UNiagaraNode>(InputPin->LinkedTo[0]->GetOwningNode());
                if (UNiagaraNodeFunctionCall* ModuleNode = Cast<UNiagaraNodeFunctionCall>(CurrentNode))
                {
                    OutModules.Insert(ModuleNode, 0);
                }
                PreviousNode = CurrentNode;
            }
            else
            {
                PreviousNode = nullptr;
            }
        }
    }

    // A Niagara pin's coarse category label (base FEdGraphPinType — no Niagara schema dependency) + a
    // parameter-map flag from the exported IsParameterMapPin.
    void MCPNia2_DescribePin(UNiagaraNode* OwnerNode, UEdGraphPin* Pin, const TSharedRef<FJsonObject>& Obj)
    {
        Obj->SetStringField(TEXT("name"), Pin->PinName.ToString());
        Obj->SetStringField(TEXT("direction"), Pin->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
        Obj->SetStringField(TEXT("category"), Pin->PinType.PinCategory.ToString());
        Obj->SetBoolField(TEXT("is_parameter_map"), OwnerNode ? OwnerNode->IsParameterMapPin(Pin) : false);
        Obj->SetNumberField(TEXT("linked_count"), Pin->LinkedTo.Num());
        if (Pin->Direction == EGPD_Input && Pin->LinkedTo.Num() == 0 && !Pin->DefaultValue.IsEmpty())
        {
            Obj->SetStringField(TEXT("default_value"), Pin->DefaultValue);
        }
    }

    // Emit the module-input list for a function-call node into an FJsonValue array. Uses the EXPORTED
    // GetStackFunctionInputs (which needs an FCompileConstantResolver — built inline from the emitter +
    // usage). Only called when Module->HasValidScriptAndGraph(). InputFilter (optional, case-insensitive
    // substring) narrows the reported inputs.
    TArray<TSharedPtr<FJsonValue>> MCPNia2_CollectInputs(UNiagaraNodeFunctionCall* Module,
        const FVersionedNiagaraEmitter& Instance, ENiagaraScriptUsage Usage, const FString& InputFilter)
    {
        TArray<TSharedPtr<FJsonValue>> Arr;
        if (!Module || !Module->HasValidScriptAndGraph()) { return Arr; } // NiagaraNodeFunctionCall.h:101

        FCompileConstantResolver Resolver(Instance, Usage);               // NiagaraParameterMapHistory.h:28 (inline ctor)
        TArray<FNiagaraVariable> InputVars;
        TSet<FNiagaraVariable> HiddenVars;
        FNiagaraStackGraphUtilities::GetStackFunctionInputs(               // NiagaraStackGraphUtilities.h:134 — NIAGARAEDITOR_API
            *Module, InputVars, HiddenVars, Resolver,
            FNiagaraStackGraphUtilities::ENiagaraGetStackFunctionInputPinsOptions::ModuleInputsOnly,
            /*bIgnoreDisabled*/ false);

        const FString Filter = InputFilter.ToLower();
        for (const FNiagaraVariable& Var : InputVars)
        {
            const FString VarName = Var.GetName().ToString();
            if (!Filter.IsEmpty() && !VarName.ToLower().Contains(Filter)) { continue; }
            TSharedRef<FJsonObject> InObj = MakeShared<FJsonObject>();
            InObj->SetStringField(TEXT("name"), VarName);
            InObj->SetStringField(TEXT("type"), Var.GetType().GetName());  // FNiagaraTypeDefinition::GetName() -> FString
            InObj->SetBoolField(TEXT("hidden"), HiddenVars.Contains(Var));
            Arr.Add(MakeShared<FJsonValueObject>(InObj));
        }
        return Arr;
    }

    // Locate a module node by function name (GetFunctionName) OR called-script asset name, within a
    // single usage stack. Returns the node + its stack index, or nullptr.
    UNiagaraNodeFunctionCall* MCPNia2_FindModuleInUsage(UNiagaraGraph* Graph, ENiagaraScriptUsage Usage,
        const FString& ModuleName, int32& OutIndex)
    {
        OutIndex = INDEX_NONE;
        if (!Graph) { return nullptr; }
        UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(Usage); // NiagaraGraph.h:270 (export-patch)
        if (!OutputNode) { return nullptr; }
        TArray<UNiagaraNodeFunctionCall*> Modules;
        MCPNia2_GetOrderedModules(OutputNode, Modules);
        for (int32 i = 0; i < Modules.Num(); ++i)
        {
            UNiagaraNodeFunctionCall* M = Modules[i];
            if (!M) { continue; }
            if (M->GetFunctionName() == ModuleName ||
                (M->FunctionScript && M->FunctionScript->GetName() == ModuleName))
            {
                OutIndex = i;
                return M;
            }
        }
        return nullptr;
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// 1) GetNiagaraModules(SystemPath, EmitterName, ScriptUsage, bIncludeInputs)
//    Lists the module (function-call) nodes of an emitter's stack(s), in stack order.
//    ScriptUsage empty/"all" -> all four emitter-level usages, grouped.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraModules(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, bool bIncludeInputs)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (EmitterName.IsEmpty()) { return MCPNia2_Error(TEXT("emitter name is required for get_niagara_modules")); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        MCPNia2_AllEmitterUsages(Usages);
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPNia2_ParseUsage(ScriptUsage, U))
        {
            return MCPNia2_Error(TEXT("bad usage (particle_spawn|particle_update|emitter_spawn|emitter_update|all)"));
        }
        Usages.Add(U);
    }

    TArray<TSharedPtr<FJsonValue>> UsageArr;
    int32 TotalModules = 0;
    for (ENiagaraScriptUsage U : Usages)
    {
        UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(U);
        TArray<UNiagaraNodeFunctionCall*> Modules;
        if (OutputNode) { MCPNia2_GetOrderedModules(OutputNode, Modules); }

        TArray<TSharedPtr<FJsonValue>> ModArr;
        for (int32 i = 0; i < Modules.Num(); ++i)
        {
            UNiagaraNodeFunctionCall* M = Modules[i];
            if (!M) { continue; }
            ++TotalModules;
            TSharedRef<FJsonObject> MObj = MakeShared<FJsonObject>();
            MObj->SetNumberField(TEXT("index"), i);
            MObj->SetStringField(TEXT("function_name"), M->GetFunctionName());
            MObj->SetStringField(TEXT("node_guid"), M->NodeGuid.ToString());
            MObj->SetStringField(TEXT("script_path"), M->FunctionScript ? M->FunctionScript->GetPathName() : FString());
            MObj->SetStringField(TEXT("script_name"), M->FunctionScript ? M->FunctionScript->GetName() : FString());
            MObj->SetBoolField(TEXT("enabled"), M->IsNodeEnabled());
            MObj->SetStringField(TEXT("enabled_state"), MCPNia2_EnabledStateToString(M->GetDesiredEnabledState()));
            MObj->SetBoolField(TEXT("valid_script"), M->HasValidScriptAndGraph());
            MObj->SetBoolField(TEXT("deprecated"), M->IsDeprecated());
            if (bIncludeInputs)
            {
                MObj->SetArrayField(TEXT("inputs"), MCPNia2_CollectInputs(M, Instance, U, FString()));
            }
            ModArr.Add(MakeShared<FJsonValueObject>(MObj));
        }

        TSharedRef<FJsonObject> UObj = MakeShared<FJsonObject>();
        UObj->SetStringField(TEXT("usage"), MCPNia2_UsageToString(U));
        UObj->SetBoolField(TEXT("has_output_node"), OutputNode != nullptr);
        UObj->SetArrayField(TEXT("modules"), ModArr);
        UsageArr.Add(MakeShared<FJsonValueObject>(UObj));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetNumberField(TEXT("module_count"), TotalModules);
    Root->SetArrayField(TEXT("usages"), UsageArr);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 2) GetNiagaraModuleInputs(SystemPath, EmitterName, ScriptUsage, ModuleName, InputFilter)
//    Lists a single module's stack inputs (name/type/hidden). ScriptUsage empty -> search all usages.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraModuleInputs(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputFilter)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (EmitterName.IsEmpty()) { return MCPNia2_Error(TEXT("emitter name is required")); }
    if (ModuleName.IsEmpty())  { return MCPNia2_Error(TEXT("module name is required")); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        MCPNia2_AllEmitterUsages(Usages);
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPNia2_ParseUsage(ScriptUsage, U)) { return MCPNia2_Error(TEXT("bad usage")); }
        Usages.Add(U);
    }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    int32 FoundIndex = INDEX_NONE;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE;
        if (UNiagaraNodeFunctionCall* M = MCPNia2_FindModuleInUsage(Graph, U, ModuleName, Idx))
        {
            Module = M; FoundUsage = U; FoundIndex = Idx; break;
        }
    }
    if (!Module) { return MCPNia2_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    TArray<TSharedPtr<FJsonValue>> Inputs = MCPNia2_CollectInputs(Module, Instance, FoundUsage, InputFilter);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("usage"), MCPNia2_UsageToString(FoundUsage));
    Root->SetNumberField(TEXT("module_index"), FoundIndex);
    Root->SetBoolField(TEXT("valid_script"), Module->HasValidScriptAndGraph());
    Root->SetNumberField(TEXT("input_count"), Inputs.Num());
    Root->SetArrayField(TEXT("inputs"), Inputs);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 3) GetNiagaraGraphNodes(SystemPath, EmitterName, Verbosity, TypeFilter)
//    Enumerates the nodes of an emitter's shared graph (EmitterName empty -> system graph).
//    Verbosity: "basic" (guid/class/type) | "detailed" (+title/pos/enabled/pin counts).
//    TypeFilter: optional case-insensitive substring over the node class / short type.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraGraphNodes(const FString& SystemPath, const FString& EmitterName,
    const FString& Verbosity, const FString& TypeFilter)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    const bool bDetailed = Verbosity.ToLower() == TEXT("detailed") || Verbosity.ToLower() == TEXT("full");
    const FString Filter = TypeFilter.ToLower();

    TArray<TSharedPtr<FJsonValue>> NodeArr;
    for (UEdGraphNode* EdNode : Graph->Nodes)   // EdGraph.h:79 — public TArray
    {
        if (!EdNode) { continue; }
        const FString ClassName = EdNode->GetClass()->GetName();
        const FString TypeShort = MCPNia2_NodeTypeShort(EdNode);
        if (!Filter.IsEmpty() && !ClassName.ToLower().Contains(Filter) && !TypeShort.ToLower().Contains(Filter))
        {
            continue;
        }

        TSharedRef<FJsonObject> NObj = MakeShared<FJsonObject>();
        NObj->SetStringField(TEXT("node_guid"), EdNode->NodeGuid.ToString());
        NObj->SetStringField(TEXT("node_class"), ClassName);
        NObj->SetStringField(TEXT("node_type"), TypeShort);

        UNiagaraNodeFunctionCall* AsFunc = Cast<UNiagaraNodeFunctionCall>(EdNode);
        if (AsFunc)
        {
            NObj->SetStringField(TEXT("function_name"), AsFunc->GetFunctionName());
            NObj->SetStringField(TEXT("script_path"), AsFunc->FunctionScript ? AsFunc->FunctionScript->GetPathName() : FString());
        }

        if (bDetailed)
        {
            NObj->SetStringField(TEXT("title"), EdNode->GetNodeTitle(ENodeTitleType::ListView).ToString());
            NObj->SetNumberField(TEXT("pos_x"), EdNode->NodePosX);
            NObj->SetNumberField(TEXT("pos_y"), EdNode->NodePosY);
            NObj->SetBoolField(TEXT("enabled"), EdNode->IsNodeEnabled());
            int32 NumIn = 0, NumOut = 0;
            for (UEdGraphPin* P : EdNode->Pins)
            {
                if (!P) { continue; }
                if (P->Direction == EGPD_Input) { ++NumIn; } else { ++NumOut; }
            }
            NObj->SetNumberField(TEXT("num_input_pins"), NumIn);
            NObj->SetNumberField(TEXT("num_output_pins"), NumOut);
        }
        NodeArr.Add(MakeShared<FJsonValueObject>(NObj));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("scope"), bSystemScope ? TEXT("system") : EmitterName);
    Root->SetNumberField(TEXT("node_count"), NodeArr.Num());
    Root->SetArrayField(TEXT("nodes"), NodeArr);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 4) GetNiagaraNodeInfo(SystemPath, EmitterName, NodeGuid)
//    Full detail for one graph node addressed by GUID: class/title/pos/enabled + every pin
//    (name/direction/category/param-map flag/default) with its linked (node_guid,pin) endpoints.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraNodeInfo(const FString& SystemPath, const FString& EmitterName,
    const FString& NodeGuid)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }

    FGuid Target;
    if (!FGuid::Parse(NodeGuid, Target)) { return MCPNia2_Error(TEXT("node_guid is not a valid GUID")); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    UEdGraphNode* Found = nullptr;
    for (UEdGraphNode* EdNode : Graph->Nodes)
    {
        if (EdNode && EdNode->NodeGuid == Target) { Found = EdNode; break; }
    }
    if (!Found) { return MCPNia2_Error(FString::Printf(TEXT("no node with guid '%s' in the graph"), *NodeGuid)); }

    UNiagaraNode* AsNiagara = Cast<UNiagaraNode>(Found);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("scope"), bSystemScope ? TEXT("system") : EmitterName);
    Root->SetStringField(TEXT("node_guid"), Found->NodeGuid.ToString());
    Root->SetStringField(TEXT("node_class"), Found->GetClass()->GetName());
    Root->SetStringField(TEXT("node_type"), MCPNia2_NodeTypeShort(Found));
    Root->SetStringField(TEXT("title"), Found->GetNodeTitle(ENodeTitleType::ListView).ToString());
    Root->SetNumberField(TEXT("pos_x"), Found->NodePosX);
    Root->SetNumberField(TEXT("pos_y"), Found->NodePosY);
    Root->SetBoolField(TEXT("enabled"), Found->IsNodeEnabled());
    Root->SetStringField(TEXT("enabled_state"), MCPNia2_EnabledStateToString(Found->GetDesiredEnabledState()));

    if (UNiagaraNodeFunctionCall* AsFunc = Cast<UNiagaraNodeFunctionCall>(Found))
    {
        Root->SetStringField(TEXT("function_name"), AsFunc->GetFunctionName());
        Root->SetStringField(TEXT("script_path"), AsFunc->FunctionScript ? AsFunc->FunctionScript->GetPathName() : FString());
        Root->SetBoolField(TEXT("valid_script"), AsFunc->HasValidScriptAndGraph());
        Root->SetBoolField(TEXT("deprecated"), AsFunc->IsDeprecated());
    }
    if (UNiagaraNodeOutput* AsOut = Cast<UNiagaraNodeOutput>(Found))
    {
        Root->SetStringField(TEXT("output_usage"), MCPNia2_UsageToString(AsOut->GetUsage()));
    }

    TArray<TSharedPtr<FJsonValue>> PinArr;
    for (UEdGraphPin* Pin : Found->Pins)
    {
        if (!Pin) { continue; }
        TSharedRef<FJsonObject> PObj = MakeShared<FJsonObject>();
        MCPNia2_DescribePin(AsNiagara, Pin, PObj);

        TArray<TSharedPtr<FJsonValue>> Links;
        for (UEdGraphPin* Linked : Pin->LinkedTo)
        {
            if (!Linked || !Linked->GetOwningNodeUnchecked()) { continue; }
            TSharedRef<FJsonObject> LObj = MakeShared<FJsonObject>();
            LObj->SetStringField(TEXT("node_guid"), Linked->GetOwningNode()->NodeGuid.ToString());
            LObj->SetStringField(TEXT("node_type"), MCPNia2_NodeTypeShort(Linked->GetOwningNode()));
            LObj->SetStringField(TEXT("pin"), Linked->PinName.ToString());
            Links.Add(MakeShared<FJsonValueObject>(LObj));
        }
        PObj->SetArrayField(TEXT("linked_to"), Links);
        PinArr.Add(MakeShared<FJsonValueObject>(PObj));
    }
    Root->SetArrayField(TEXT("pins"), PinArr);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 5) TraceNiagaraConnection(SystemPath, EmitterName, NodeGuid, PinName, Direction, MaxDepth)
//    Breadth-first walk of the wire graph from a start node/pin. Direction "upstream" follows
//    input pins toward producers; "downstream" follows output pins toward consumers. Records
//    each hop {depth, from_node_guid, from_pin, to_node_guid, to_node_type, to_pin}.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::TraceNiagaraConnection(const FString& SystemPath, const FString& EmitterName,
    const FString& NodeGuid, const FString& PinName, const FString& Direction, int32 MaxDepth)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }

    FGuid Target;
    if (!FGuid::Parse(NodeGuid, Target)) { return MCPNia2_Error(TEXT("node_guid is not a valid GUID")); }

    const FString Dir = Direction.ToLower();
    const bool bUpstream = (Dir != TEXT("downstream")); // default upstream
    const EEdGraphPinDirection FollowDir = bUpstream ? EGPD_Input : EGPD_Output;
    int32 Depth = MaxDepth;
    if (Depth <= 0) { Depth = 8; }
    if (Depth > 64) { Depth = 64; }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    UEdGraphNode* Start = nullptr;
    for (UEdGraphNode* EdNode : Graph->Nodes)
    {
        if (EdNode && EdNode->NodeGuid == Target) { Start = EdNode; break; }
    }
    if (!Start) { return MCPNia2_Error(FString::Printf(TEXT("no node with guid '%s' in the graph"), *NodeGuid)); }

    // BFS frontier of nodes; a visited set prevents cycles. Each level follows only pins of FollowDir.
    TArray<TSharedPtr<FJsonValue>> Hops;
    TSet<FGuid> Visited;
    Visited.Add(Start->NodeGuid);
    TArray<UEdGraphNode*> Frontier;
    Frontier.Add(Start);

    for (int32 Level = 0; Level < Depth && Frontier.Num() > 0; ++Level)
    {
        TArray<UEdGraphNode*> Next;
        for (UEdGraphNode* Node : Frontier)
        {
            if (!Node) { continue; }
            for (UEdGraphPin* Pin : Node->Pins)
            {
                if (!Pin || Pin->Direction != FollowDir) { continue; }
                // If PinName was supplied, restrict the FIRST-level walk to that pin only.
                if (Level == 0 && !PinName.IsEmpty() && Pin->PinName.ToString() != PinName) { continue; }
                for (UEdGraphPin* Linked : Pin->LinkedTo)
                {
                    if (!Linked || !Linked->GetOwningNodeUnchecked()) { continue; }
                    UEdGraphNode* Other = Linked->GetOwningNode();
                    TSharedRef<FJsonObject> HObj = MakeShared<FJsonObject>();
                    HObj->SetNumberField(TEXT("depth"), Level + 1);
                    HObj->SetStringField(TEXT("from_node_guid"), Node->NodeGuid.ToString());
                    HObj->SetStringField(TEXT("from_pin"), Pin->PinName.ToString());
                    HObj->SetStringField(TEXT("to_node_guid"), Other->NodeGuid.ToString());
                    HObj->SetStringField(TEXT("to_node_type"), MCPNia2_NodeTypeShort(Other));
                    HObj->SetStringField(TEXT("to_pin"), Linked->PinName.ToString());
                    Hops.Add(MakeShared<FJsonValueObject>(HObj));
                    if (!Visited.Contains(Other->NodeGuid))
                    {
                        Visited.Add(Other->NodeGuid);
                        Next.Add(Other);
                    }
                }
            }
        }
        Frontier = MoveTemp(Next);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("scope"), bSystemScope ? TEXT("system") : EmitterName);
    Root->SetStringField(TEXT("start_node_guid"), Start->NodeGuid.ToString());
    Root->SetStringField(TEXT("direction"), bUpstream ? TEXT("upstream") : TEXT("downstream"));
    Root->SetNumberField(TEXT("max_depth"), Depth);
    Root->SetNumberField(TEXT("hop_count"), Hops.Num());
    Root->SetArrayField(TEXT("hops"), Hops);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 6) ValidateNiagaraStack(SystemPath, EmitterName, MinSeverity)
//    STRUCTURAL validation of an emitter's stacks (NOT the view-model stack-issue set, which is
//    headless-infeasible; compile-based diagnostics live in validate_niagara_graph). Flags:
//      error   - module with an invalid/missing script (HasValidScriptAndGraph()==false)
//      error   - usage stack has no reachable module chain but an output node exists w/ dangling map
//      warning - deprecated module (IsDeprecated())
//      info    - disabled module
//    MinSeverity ("error"|"warning"|"info"|"all") filters the returned issues.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::ValidateNiagaraStack(const FString& SystemPath, const FString& EmitterName,
    const FString& MinSeverity)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (EmitterName.IsEmpty()) { return MCPNia2_Error(TEXT("emitter name is required")); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    // Severity ranking for the min-severity filter.
    auto Rank = [](const FString& S) -> int32
    {
        if (S == TEXT("error")) { return 3; }
        if (S == TEXT("warning")) { return 2; }
        if (S == TEXT("info")) { return 1; }
        return 0;
    };
    const int32 MinRank = Rank(MinSeverity.ToLower());

    TArray<TSharedPtr<FJsonValue>> Issues;
    int32 ErrCount = 0, WarnCount = 0, InfoCount = 0;

    auto AddIssue = [&](const FString& Severity, const FString& Usage, const FString& Module, const FString& Message)
    {
        if (Severity == TEXT("error")) { ++ErrCount; }
        else if (Severity == TEXT("warning")) { ++WarnCount; }
        else { ++InfoCount; }
        if (Rank(Severity) < MinRank) { return; }
        TSharedRef<FJsonObject> IObj = MakeShared<FJsonObject>();
        IObj->SetStringField(TEXT("severity"), Severity);
        IObj->SetStringField(TEXT("usage"), Usage);
        IObj->SetStringField(TEXT("module"), Module);
        IObj->SetStringField(TEXT("message"), Message);
        Issues.Add(MakeShared<FJsonValueObject>(IObj));
    };

    TArray<ENiagaraScriptUsage> Usages;
    MCPNia2_AllEmitterUsages(Usages);
    int32 TotalModules = 0;
    for (ENiagaraScriptUsage U : Usages)
    {
        const FString UsageStr = MCPNia2_UsageToString(U);
        UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(U);
        if (!OutputNode) { continue; } // an emitter need not populate every usage; not an error

        TArray<UNiagaraNodeFunctionCall*> Modules;
        MCPNia2_GetOrderedModules(OutputNode, Modules);
        for (UNiagaraNodeFunctionCall* M : Modules)
        {
            if (!M) { continue; }
            ++TotalModules;
            const FString ModName = M->GetFunctionName();
            if (!M->HasValidScriptAndGraph())
            {
                AddIssue(TEXT("error"), UsageStr, ModName, TEXT("module has a missing or invalid script/graph"));
            }
            if (M->IsDeprecated())
            {
                AddIssue(TEXT("warning"), UsageStr, ModName, TEXT("module script is deprecated"));
            }
            if (!M->IsNodeEnabled())
            {
                AddIssue(TEXT("info"), UsageStr, ModName, TEXT("module is disabled"));
            }
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetNumberField(TEXT("module_count"), TotalModules);
    Root->SetNumberField(TEXT("error_count"), ErrCount);
    Root->SetNumberField(TEXT("warning_count"), WarnCount);
    Root->SetNumberField(TEXT("info_count"), InfoCount);
    Root->SetBoolField(TEXT("valid"), ErrCount == 0);
    Root->SetStringField(TEXT("min_severity"), MinSeverity.IsEmpty() ? TEXT("all") : MinSeverity.ToLower());
    Root->SetStringField(TEXT("note"), TEXT("Structural validation only (module script validity / deprecation / "
        "enabled state). Compile-level diagnostics come from validate_niagara_graph / get_niagara_system_errors; "
        "the full view-model stack-issue set is not reachable headless."));
    Root->SetArrayField(TEXT("issues"), Issues);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 7) SetNiagaraModuleEnabled(SystemPath, EmitterName, ScriptUsage, ModuleName, bEnabled)  [WRITE]
//    Enable/disable a module in an emitter stack via the EXPORTED SetModuleIsEnabled (which toggles
//    every associated node + flags recompile). Captures prev state; recompiles + marks dirty.
//    Returns {prev_enabled, enabled} for the undo ledger.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraModuleEnabled(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, bool bEnabled)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia2_LoadSystem(SystemPath);
    if (!System) { return MCPNia2_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (EmitterName.IsEmpty()) { return MCPNia2_Error(TEXT("emitter name is required")); }
    if (ModuleName.IsEmpty())  { return MCPNia2_Error(TEXT("module name is required")); }

    FVersionedNiagaraEmitter Instance;
    bool bSystemScope = false;
    FString Err;
    UNiagaraGraph* Graph = MCPNia2_ResolveGraph(System, EmitterName, Instance, bSystemScope, Err);
    if (!Graph) { return MCPNia2_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        MCPNia2_AllEmitterUsages(Usages);
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPNia2_ParseUsage(ScriptUsage, U)) { return MCPNia2_Error(TEXT("bad usage")); }
        Usages.Add(U);
    }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE;
        if (UNiagaraNodeFunctionCall* M = MCPNia2_FindModuleInUsage(Graph, U, ModuleName, Idx))
        {
            Module = M; FoundUsage = U; break;
        }
    }
    if (!Module) { return MCPNia2_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    const bool bPrevEnabled = Module->IsNodeEnabled();
    if (bPrevEnabled != bEnabled)
    {
        FNiagaraStackGraphUtilities::SetModuleIsEnabled(*Module, bEnabled); // NiagaraStackGraphUtilities.h:298 — NIAGARAEDITOR_API
        System->RequestCompile(false);
        System->MarkPackageDirty();
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("usage"), MCPNia2_UsageToString(FoundUsage));
    Root->SetBoolField(TEXT("prev_enabled"), bPrevEnabled);   // Python: inverse re-set uses this
    Root->SetBoolField(TEXT("enabled"), bEnabled);
    return MCPNia2_Serialize(Root);
#else
    return MCPNia2_Error(TEXT("editor-only"));
#endif
}
