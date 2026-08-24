// ============================================================================
// MCPReflection_Niagara3.cpp  —  Niagara module-stack WRITERS round #2:
//   reorder a module, set a module input to a DYNAMIC INPUT, set a module
//   input's CURVE keys, and a general stack-value setter (local / linked value
//   on a module input pin). Closes the last NiagaraEditor-C++ authoring spec
//   features that stock Python cannot reach (the emitter script graph + module
//   stack + per-input override pins live in NiagaraEditor C++, not Python).
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18. **ISOLATED translation unit** on purpose:
// implements DEFERRED MCPReflectionLibrary methods that niagara_runtime_cpp.py
// hasattr-guards on. When these link, the Python tools auto-enable.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Anon-namespace helpers are prefixed MCPNia3_ (DIFFERENT from Niagara2.cpp's
//   MCPNia2_ to avoid unity-build ODR collisions). The ordered module-stack walk
//   is re-implemented here on the EXPORTED UNiagaraNode::IsParameterMapPin (same
//   as Niagara2.cpp), so it does NOT need the non-exported GetOrderedModuleNodes.
//
//   NiagaraEditor symbols referenced, with export status verified against the
//   5.8 source engine (header:line):
//     EXPORTED (already NIAGARAEDITOR_API in stock 5.8):
//       FNiagaraStackGraphUtilities::GetStackFunctionInputs(..,FCompileConstantResolver,..)  NiagaraStackGraphUtilities.h:134
//       FNiagaraStackGraphUtilities::GetOrCreateStackFunctionInputOverridePin(..)            NiagaraStackGraphUtilities.h:216
//       FNiagaraStackGraphUtilities::SetLinkedParameterValueForFunctionInput(..)             NiagaraStackGraphUtilities.h:229
//       FNiagaraStackGraphUtilities::SetDataInterfaceValueForFunctionInput(..)               NiagaraStackGraphUtilities.h:231
//       FNiagaraStackGraphUtilities::SetDynamicInputForFunctionInput(..)                     NiagaraStackGraphUtilities.h:235
//       FNiagaraParameterHandle(FName) / CreateAliasedModuleParameterHandle / GetName        NiagaraParameterHandle.h:16/24/41
//       UNiagaraGraph::FindOutputNode(ENiagaraScriptUsage,FGuid)  [pre-existing export-patch] NiagaraGraph.h:270
//       UNiagaraNode::IsParameterMapPin(const UEdGraphPin*) const                            NiagaraNode.h:147
//       UNiagaraNodeFunctionCall::GetFunctionName()/HasValidScriptAndGraph()/FunctionScript  NiagaraNodeFunctionCall.h
//       UNiagaraDataInterfaceCurve::Curve (public UPROPERTY) / UpdateTimeRanges (NIAGARA_API) NiagaraDataInterfaceCurve.h:21/40
//       FNiagaraEmitterHandle::GetId() (NIAGARA_API)                                         NiagaraEmitterHandle.h:49
//     REQUIRES AN ENGINE EXPORT PATCH (reported to coordinator; used ONLY by ReorderNiagaraModule):
//       FNiagaraStackGraphUtilities::MoveModule(..)                                          NiagaraStackGraphUtilities.h:324
//
//   Deliberately NOT referenced (would add link deps on non-exported symbols):
//     RemoveNodesForStackFunctionInputOverridePin (NiagaraStackGraphUtilities.h:222) and
//     UNiagaraNodeInput::GetDataInterface (NiagaraNodeInput.h:113). Consequently the input
//     setters here only operate on inputs that have NO existing LINKED override (fresh
//     inputs, or inputs whose current value is a plain pin default). An input that already
//     carries a linked value / dynamic input / data interface returns an honest guard error
//     rather than crashing the checkf(OverridePin.LinkedTo.Num()==0) inside the engine helpers.
//     (Exporting those two would let the setters overwrite/in-place-edit existing overrides.)
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":..} on any miss
// (never crash). Writes capture prev tokens (prev_index / had_override / prev_value)
// for the per-session undo ledger, recompile via RequestCompile(false) + MarkPackageDirty.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

// --- Reflection / core ---
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"
#include "Misc/Guid.h"

// --- Base EdGraph (Engine module — always linkable) ---
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"

// --- Curves (Engine module) ---
#include "Curves/RichCurve.h"

// --- Niagara runtime (NIAGARA_API) ---
#include "NiagaraSystem.h"                  // UNiagaraSystem
#include "NiagaraEmitter.h"                 // UNiagaraEmitter / FVersionedNiagaraEmitterData
#include "NiagaraEmitterHandle.h"           // FNiagaraEmitterHandle::GetId
#include "NiagaraTypes.h"                   // FNiagaraVariable / FNiagaraTypeDefinition / FVersionedNiagaraEmitter
#include "NiagaraScript.h"                  // UNiagaraScript / ENiagaraScriptUsage
#include "NiagaraDataInterfaceCurve.h"      // UNiagaraDataInterfaceCurve (float curve DI)

// --- NiagaraEditor (NIAGARAEDITOR_API + the export-patch symbols) ---
#include "NiagaraScriptSource.h"                          // UNiagaraScriptSource (->NodeGraph)
#include "NiagaraGraph.h"                                 // UNiagaraGraph::FindOutputNode (export-patch)
#include "NiagaraNode.h"                                  // UNiagaraNode::IsParameterMapPin
#include "NiagaraNodeOutput.h"                            // UNiagaraNodeOutput::GetUsage/GetUsageId
#include "NiagaraNodeFunctionCall.h"                      // UNiagaraNodeFunctionCall
#include "NiagaraParameterMapHistory.h"                   // FCompileConstantResolver (inline ctor)
#include "ViewModels/Stack/NiagaraParameterHandle.h"      // FNiagaraParameterHandle
#include "ViewModels/Stack/NiagaraStackGraphUtilities.h"  // stack graph utilities

namespace
{
    // ---- JSON helpers (MCPNia3_ prefixed for unity-build uniqueness) -------
    FString MCPNia3_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPNia3_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPNia3_Serialize(Obj);
    }

#if WITH_EDITOR
    UNiagaraSystem* MCPNia3_LoadSystem(const FString& Path)
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

    bool MCPNia3_ParseUsage(const FString& In, ENiagaraScriptUsage& Out)
    {
        const FString U = In.ToLower();
        if (U == TEXT("particle_spawn"))  { Out = ENiagaraScriptUsage::ParticleSpawnScript;  return true; }
        if (U == TEXT("particle_update")) { Out = ENiagaraScriptUsage::ParticleUpdateScript; return true; }
        if (U == TEXT("emitter_spawn"))   { Out = ENiagaraScriptUsage::EmitterSpawnScript;   return true; }
        if (U == TEXT("emitter_update"))  { Out = ENiagaraScriptUsage::EmitterUpdateScript;  return true; }
        return false;
    }

    const TCHAR* MCPNia3_UsageToString(ENiagaraScriptUsage Usage)
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

    void MCPNia3_AllEmitterUsages(TArray<ENiagaraScriptUsage>& Out)
    {
        Out.Reset();
        Out.Add(ENiagaraScriptUsage::EmitterSpawnScript);
        Out.Add(ENiagaraScriptUsage::EmitterUpdateScript);
        Out.Add(ENiagaraScriptUsage::ParticleSpawnScript);
        Out.Add(ENiagaraScriptUsage::ParticleUpdateScript);
    }

    // The emitter's per-usage UNiagaraScript (matches FNiagaraEditorUtilities::GetScriptFromSystem for
    // the four emitter-level usages — the object MoveModule internally resolves as its TargetScript).
    UNiagaraScript* MCPNia3_ScriptForUsage(FVersionedNiagaraEmitterData* Data, ENiagaraScriptUsage Usage)
    {
        if (!Data) { return nullptr; }
        switch (Usage)
        {
            case ENiagaraScriptUsage::EmitterSpawnScript:   return Data->EmitterSpawnScriptProps.Script;
            case ENiagaraScriptUsage::EmitterUpdateScript:  return Data->EmitterUpdateScriptProps.Script;
            case ENiagaraScriptUsage::ParticleSpawnScript:  return Data->SpawnScriptProps.Script;
            case ENiagaraScriptUsage::ParticleUpdateScript: return Data->UpdateScriptProps.Script;
            default:                                        return nullptr;
        }
    }

    // Find an emitter handle by name; fills the const instance copy + the handle id. (Reorder needs the id.)
    bool MCPNia3_FindEmitterHandle(UNiagaraSystem* System, const FString& EmitterName,
                                   FVersionedNiagaraEmitter& OutInstance, FGuid& OutHandleId)
    {
        if (!System) { return false; }
        for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
        {
            if (H.GetName().ToString() == EmitterName)
            {
                OutInstance = H.GetInstance();   // NiagaraEmitterHandle.h — const FVersionedNiagaraEmitter
                OutHandleId = H.GetId();          // NiagaraEmitterHandle.h:49 — NIAGARA_API
                return true;
            }
        }
        return false;
    }

    // Resolve the named emitter's shared source graph (+ instance + handle id). nullptr + OutErr on miss.
    UNiagaraGraph* MCPNia3_ResolveEmitterGraph(UNiagaraSystem* System, const FString& EmitterName,
        FVersionedNiagaraEmitter& OutInstance, FGuid& OutHandleId, FString& OutErr)
    {
        if (!System) { OutErr = TEXT("null system"); return nullptr; }
        if (EmitterName.IsEmpty()) { OutErr = TEXT("emitter name is required"); return nullptr; }
        if (!MCPNia3_FindEmitterHandle(System, EmitterName, OutInstance, OutHandleId))
        {
            OutErr = FString::Printf(TEXT("no emitter handle named '%s'"), *EmitterName);
            return nullptr;
        }
        FVersionedNiagaraEmitterData* Data = OutInstance.GetEmitterData();
        if (!Data) { OutErr = TEXT("emitter has no versioned data"); return nullptr; }
        UNiagaraScript* AnyScript = Data->SpawnScriptProps.Script;  // any emitter script shares one graph
        if (!AnyScript) { OutErr = TEXT("emitter spawn script missing"); return nullptr; }
        UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(AnyScript->GetLatestSource());
        if (!Source) { OutErr = TEXT("script source is not a graph-backed UNiagaraScriptSource"); return nullptr; }
        UNiagaraGraph* Graph = Source->NodeGraph;
        if (!Graph) { OutErr = TEXT("script source has no NodeGraph"); return nullptr; }
        return Graph;
    }

    // Re-implementation of GetParameterMapInputPin via the exported IsParameterMapPin (engine helper not exported).
    UEdGraphPin* MCPNia3_ParamMapInputPin(UNiagaraNode* Node)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && Node->IsParameterMapPin(P))
            {
                return P;
            }
        }
        return nullptr;
    }

    // Re-implementation of GetOrderedModuleNodes: walk backward from the output node along the single-linked
    // parameter-map input pin, collecting function-call (module) nodes in stack order.
    void MCPNia3_GetOrderedModules(UNiagaraNodeOutput* OutputNode, TArray<UNiagaraNodeFunctionCall*>& OutModules)
    {
        OutModules.Reset();
        UNiagaraNode* PreviousNode = OutputNode;
        int32 Guard = 0;
        while (PreviousNode != nullptr && Guard++ < 4096)
        {
            UEdGraphPin* InputPin = MCPNia3_ParamMapInputPin(PreviousNode);
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

    // Locate a module node by function name (or called-script asset name) in one usage stack. Also returns the
    // owning output node (needed for MoveModule's usage id).
    UNiagaraNodeFunctionCall* MCPNia3_FindModuleInUsage(UNiagaraGraph* Graph, ENiagaraScriptUsage Usage,
        const FString& ModuleName, int32& OutIndex, UNiagaraNodeOutput*& OutOutputNode)
    {
        OutIndex = INDEX_NONE;
        OutOutputNode = nullptr;
        if (!Graph) { return nullptr; }
        UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(Usage); // NiagaraGraph.h:270 (pre-existing export-patch)
        if (!OutputNode) { return nullptr; }
        OutOutputNode = OutputNode;
        TArray<UNiagaraNodeFunctionCall*> Modules;
        MCPNia3_GetOrderedModules(OutputNode, Modules);
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

    // Find a module INPUT variable by name (exact "Module.Foo" or short "Foo", case-insensitive) via the
    // exported GetStackFunctionInputs. Returns the FNiagaraVariable (name is the module-namespaced "Module.Foo").
    bool MCPNia3_FindInput(UNiagaraNodeFunctionCall* Module, const FVersionedNiagaraEmitter& Instance,
        ENiagaraScriptUsage Usage, const FString& InputName, FNiagaraVariable& OutVar)
    {
        if (!Module || !Module->HasValidScriptAndGraph()) { return false; }
        FCompileConstantResolver Resolver(Instance, Usage);
        TArray<FNiagaraVariable> InputVars;
        TSet<FNiagaraVariable> HiddenVars;
        FNiagaraStackGraphUtilities::GetStackFunctionInputs(
            *Module, InputVars, HiddenVars, Resolver,
            FNiagaraStackGraphUtilities::ENiagaraGetStackFunctionInputPinsOptions::ModuleInputsOnly,
            /*bIgnoreDisabled*/ false);

        for (const FNiagaraVariable& V : InputVars)
        {
            const FString Full = V.GetName().ToString();     // e.g. "Module.SpawnRate"
            FString Short = Full;
            int32 DotIdx = INDEX_NONE;
            if (Full.FindLastChar(TEXT('.'), DotIdx)) { Short = Full.RightChop(DotIdx + 1); }
            if (Full == InputName || Short == InputName ||
                Full.Equals(InputName, ESearchCase::IgnoreCase) || Short.Equals(InputName, ESearchCase::IgnoreCase))
            {
                OutVar = V;
                return true;
            }
        }
        return false;
    }

    // Build the aliased module input handle for an input variable on a module node.
    FNiagaraParameterHandle MCPNia3_AliasedInputHandle(const FNiagaraVariable& InputVar, UNiagaraNodeFunctionCall* Module)
    {
        FNiagaraParameterHandle InputHandle(InputVar.GetName());   // "Module.Foo" -> namespace "Module", name "Foo"
        return FNiagaraParameterHandle::CreateAliasedModuleParameterHandle(InputHandle, Module);
    }

    ERichCurveInterpMode MCPNia3_ParseInterp(const FString& In)
    {
        const FString S = In.ToLower();
        if (S == TEXT("constant")) { return RCIM_Constant; }
        if (S == TEXT("linear"))   { return RCIM_Linear; }
        return RCIM_Cubic; // default (auto tangents applied after)
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// 1) ReorderNiagaraModule(SystemPath, EmitterName, ScriptUsage, ModuleName, NewIndex)  [WRITE]
//    Move a module to NewIndex within its emitter stack via the engine's own reorder
//    (FNiagaraStackGraphUtilities::MoveModule — the same call the editor's drag-drop uses).
//    ScriptUsage empty/"all" -> search every usage for the module. Returns {prev_index,new_index}.
//    NOTE: MoveModule needs a NIAGARAEDITOR_API export patch (reported to the coordinator).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::ReorderNiagaraModule(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, int32 NewIndex)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia3_LoadSystem(SystemPath);
    if (!System) { return MCPNia3_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia3_Error(TEXT("module name is required")); }
    if (NewIndex < 0) { return MCPNia3_Error(TEXT("new_index must be >= 0")); }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia3_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia3_Error(Err); }
    FVersionedNiagaraEmitterData* Data = Instance.GetEmitterData();
    if (!Data) { return MCPNia3_Error(TEXT("emitter has no versioned data")); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        MCPNia3_AllEmitterUsages(Usages);
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPNia3_ParseUsage(ScriptUsage, U))
        {
            return MCPNia3_Error(TEXT("bad usage (particle_spawn|particle_update|emitter_spawn|emitter_update|all)"));
        }
        Usages.Add(U);
    }

    UNiagaraNodeFunctionCall* Module = nullptr;
    UNiagaraNodeOutput* OutputNode = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    int32 PrevIndex = INDEX_NONE;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE;
        UNiagaraNodeOutput* Out = nullptr;
        if (UNiagaraNodeFunctionCall* M = MCPNia3_FindModuleInUsage(Graph, U, ModuleName, Idx, Out))
        {
            Module = M; FoundUsage = U; PrevIndex = Idx; OutputNode = Out; break;
        }
    }
    if (!Module || !OutputNode)
    {
        return MCPNia3_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName));
    }

    // Count modules in the found usage to clamp/validate the requested index.
    TArray<UNiagaraNodeFunctionCall*> ModsInUsage;
    MCPNia3_GetOrderedModules(OutputNode, ModsInUsage);
    const int32 ModuleCount = ModsInUsage.Num();
    if (NewIndex >= ModuleCount) { NewIndex = ModuleCount - 1; }
    if (NewIndex < 0) { NewIndex = 0; }

    if (NewIndex == PrevIndex)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("system"), System->GetName());
        Root->SetStringField(TEXT("emitter"), EmitterName);
        Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
        Root->SetStringField(TEXT("module"), Module->GetFunctionName());
        Root->SetNumberField(TEXT("prev_index"), PrevIndex);
        Root->SetNumberField(TEXT("new_index"), NewIndex);
        Root->SetBoolField(TEXT("moved"), false);
        Root->SetStringField(TEXT("note"), TEXT("already at the requested index (no-op)"));
        return MCPNia3_Serialize(Root);
    }

    UNiagaraScript* SourceScript = MCPNia3_ScriptForUsage(Data, FoundUsage);
    if (!SourceScript) { return MCPNia3_Error(TEXT("could not resolve the emitter script for the module's usage")); }

    UNiagaraNodeFunctionCall* OutMoved = nullptr;
    FNiagaraStackGraphUtilities::MoveModule(
        *SourceScript, *Module, *System, HandleId, FoundUsage, OutputNode->GetUsageId(),
        /*TargetModuleIndex*/ NewIndex, /*bForceCopy*/ false, OutMoved); // NiagaraStackGraphUtilities.h:324 (export-patch)

    System->RequestCompile(false);
    System->MarkPackageDirty();

    // Recompute the module's landed index for truthful reporting/verification.
    int32 LandedIndex = NewIndex;
    {
        UNiagaraNodeFunctionCall* MovedRef = OutMoved ? OutMoved : Module;
        if (UNiagaraNodeOutput* PostOut = Graph->FindOutputNode(FoundUsage))
        {
            TArray<UNiagaraNodeFunctionCall*> PostMods;
            MCPNia3_GetOrderedModules(PostOut, PostMods);
            const int32 FoundAt = PostMods.IndexOfByKey(MovedRef);
            if (FoundAt != INDEX_NONE) { LandedIndex = FoundAt; }
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetNumberField(TEXT("prev_index"), PrevIndex);   // Python: inverse reorder targets this
    Root->SetNumberField(TEXT("new_index"), LandedIndex);
    Root->SetNumberField(TEXT("module_count"), ModuleCount);
    Root->SetBoolField(TEXT("moved"), true);
    return MCPNia3_Serialize(Root);
#else
    return MCPNia3_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 2) SetNiagaraDynamicInput(SystemPath, EmitterName, ScriptUsage, ModuleName, InputName, DynamicInputScriptPath) [WRITE]
//    Set a module input to a DYNAMIC INPUT (a function-call feeding the input pin) via the exported
//    GetOrCreateStackFunctionInputOverridePin + SetDynamicInputForFunctionInput. Only for inputs with NO
//    existing linked override (see LINK-RISK DISCIPLINE). Returns {had_override, dynamic_input_node_guid}.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraDynamicInput(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& DynamicInputScriptPath)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia3_LoadSystem(SystemPath);
    if (!System) { return MCPNia3_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia3_Error(TEXT("module name is required")); }
    if (InputName.IsEmpty())  { return MCPNia3_Error(TEXT("input name is required")); }
    if (DynamicInputScriptPath.IsEmpty()) { return MCPNia3_Error(TEXT("dynamic_input_script_path is required")); }

    UNiagaraScript* DynamicInput = Cast<UNiagaraScript>(FSoftObjectPath(DynamicInputScriptPath).TryLoad());
    if (!DynamicInput) { return MCPNia3_Error(FString::Printf(TEXT("could not load dynamic-input UNiagaraScript '%s'"), *DynamicInputScriptPath)); }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia3_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia3_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all")) { MCPNia3_AllEmitterUsages(Usages); }
    else { ENiagaraScriptUsage U; if (!MCPNia3_ParseUsage(ScriptUsage, U)) { return MCPNia3_Error(TEXT("bad usage")); } Usages.Add(U); }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE; UNiagaraNodeOutput* Out = nullptr;
        if (UNiagaraNodeFunctionCall* M = MCPNia3_FindModuleInUsage(Graph, U, ModuleName, Idx, Out)) { Module = M; FoundUsage = U; break; }
    }
    if (!Module) { return MCPNia3_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    FNiagaraVariable InputVar;
    if (!MCPNia3_FindInput(Module, Instance, FoundUsage, InputName, InputVar))
    {
        return MCPNia3_Error(FString::Printf(TEXT("module '%s' has no input '%s'"), *ModuleName, *InputName));
    }

    FNiagaraParameterHandle AliasedHandle = MCPNia3_AliasedInputHandle(InputVar, Module);
    UEdGraphPin& OverridePin = FNiagaraStackGraphUtilities::GetOrCreateStackFunctionInputOverridePin(
        *Module, AliasedHandle, InputVar.GetType(), FGuid(), FGuid());

    const bool bPrevLinked = OverridePin.LinkedTo.Num() > 0;
    const bool bHadOverride = bPrevLinked || !OverridePin.DefaultValue.IsEmpty();
    if (bPrevLinked)
    {
        return MCPNia3_Error(FString::Printf(TEXT("input '%s' already has a linked value/dynamic input; clearing an "
            "existing linked override is not reachable headless (would require exporting "
            "RemoveNodesForStackFunctionInputOverridePin) — reset the input in-editor first"), *InputName));
    }

    UNiagaraNodeFunctionCall* OutDynamicInput = nullptr;
    FNiagaraStackGraphUtilities::SetDynamicInputForFunctionInput(OverridePin, DynamicInput, OutDynamicInput); // NiagaraStackGraphUtilities.h:235

    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("input"), InputVar.GetName().ToString());
    Root->SetStringField(TEXT("dynamic_input"), DynamicInput->GetName());
    Root->SetStringField(TEXT("dynamic_input_node_guid"), OutDynamicInput ? OutDynamicInput->NodeGuid.ToString() : FString());
    Root->SetBoolField(TEXT("had_override"), bHadOverride);   // Python: inverse-hint
    Root->SetBoolField(TEXT("prev_linked"), bPrevLinked);
    Root->SetBoolField(TEXT("set"), true);
    return MCPNia3_Serialize(Root);
#else
    return MCPNia3_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 3) SetNiagaraStackValue(SystemPath, EmitterName, ScriptUsage, ModuleName, InputName, Mode, ValueOrParameter) [WRITE]
//    General stack-value setter. Mode "linked": bind the input to a parameter (e.g. "Particles.Velocity",
//    "User.MyFloat", "Engine.DeltaTime") via the exported SetLinkedParameterValueForFunctionInput. Mode
//    "local": set the input's override-pin DEFAULT VALUE string (best-effort local literal). Only for inputs
//    with NO existing linked override. Returns {mode, prev_value, had_override} for the ledger.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraStackValue(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& Mode,
    const FString& ValueOrParameter)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia3_LoadSystem(SystemPath);
    if (!System) { return MCPNia3_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia3_Error(TEXT("module name is required")); }
    if (InputName.IsEmpty())  { return MCPNia3_Error(TEXT("input name is required")); }

    const FString ModeL = Mode.ToLower();
    const bool bLinked = (ModeL == TEXT("linked") || ModeL == TEXT("link") || ModeL == TEXT("parameter"));
    const bool bLocal  = (ModeL == TEXT("local") || ModeL == TEXT("value") || ModeL.IsEmpty());
    if (!bLinked && !bLocal) { return MCPNia3_Error(TEXT("mode must be 'local' or 'linked'")); }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia3_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia3_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all")) { MCPNia3_AllEmitterUsages(Usages); }
    else { ENiagaraScriptUsage U; if (!MCPNia3_ParseUsage(ScriptUsage, U)) { return MCPNia3_Error(TEXT("bad usage")); } Usages.Add(U); }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE; UNiagaraNodeOutput* Out = nullptr;
        if (UNiagaraNodeFunctionCall* M = MCPNia3_FindModuleInUsage(Graph, U, ModuleName, Idx, Out)) { Module = M; FoundUsage = U; break; }
    }
    if (!Module) { return MCPNia3_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    FNiagaraVariable InputVar;
    if (!MCPNia3_FindInput(Module, Instance, FoundUsage, InputName, InputVar))
    {
        return MCPNia3_Error(FString::Printf(TEXT("module '%s' has no input '%s'"), *ModuleName, *InputName));
    }

    FNiagaraParameterHandle AliasedHandle = MCPNia3_AliasedInputHandle(InputVar, Module);
    UEdGraphPin& OverridePin = FNiagaraStackGraphUtilities::GetOrCreateStackFunctionInputOverridePin(
        *Module, AliasedHandle, InputVar.GetType(), FGuid(), FGuid());

    const bool bPrevLinked = OverridePin.LinkedTo.Num() > 0;
    const FString PrevDefault = OverridePin.DefaultValue;
    const bool bHadOverride = bPrevLinked || !PrevDefault.IsEmpty();

    if (bLinked)
    {
        if (ValueOrParameter.IsEmpty()) { return MCPNia3_Error(TEXT("linked mode requires value_or_parameter (a parameter name like 'Particles.Velocity')")); }
        if (bPrevLinked)
        {
            return MCPNia3_Error(FString::Printf(TEXT("input '%s' already has a linked value/dynamic input; clearing an "
                "existing linked override is not reachable headless — reset the input in-editor first"), *InputName));
        }
        FNiagaraVariableBase LinkedParam(InputVar.GetType(), FName(*ValueOrParameter));
        TSet<FNiagaraVariableBase> KnownParameters; // empty: no static/loose-type substitution attempted
        FNiagaraStackGraphUtilities::SetLinkedParameterValueForFunctionInput(OverridePin, LinkedParam, KnownParameters); // NiagaraStackGraphUtilities.h:229
    }
    else // local
    {
        if (bPrevLinked)
        {
            return MCPNia3_Error(FString::Printf(TEXT("input '%s' currently has a linked value/dynamic input; overwriting "
                "it with a local value is not reachable headless — reset the input in-editor first"), *InputName));
        }
        // Set the override pin's default string directly. The caller supplies a Niagara pin-default literal
        // (e.g. "1.5" for float, "(X=1.000000,Y=0.000000,Z=0.000000)" for vector). Best-effort: no type coercion.
        OverridePin.Modify();
        OverridePin.DefaultValue = ValueOrParameter;
        if (UNiagaraNode* OwningNiagaraNode = Cast<UNiagaraNode>(OverridePin.GetOwningNode()))
        {
            OwningNiagaraNode->MarkNodeRequiresSynchronization(TEXT("MCP SetNiagaraStackValue local"), true);
        }
    }

    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("input"), InputVar.GetName().ToString());
    Root->SetStringField(TEXT("mode"), bLinked ? TEXT("linked") : TEXT("local"));
    Root->SetStringField(TEXT("value"), ValueOrParameter);
    Root->SetBoolField(TEXT("had_override"), bHadOverride);
    Root->SetBoolField(TEXT("prev_linked"), bPrevLinked);
    Root->SetStringField(TEXT("prev_value"), PrevDefault);   // Python: inverse re-set (local) uses this
    Root->SetBoolField(TEXT("set"), true);
    return MCPNia3_Serialize(Root);
#else
    return MCPNia3_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 4) SetNiagaraCurve(SystemPath, EmitterName, ScriptUsage, ModuleName, InputName, KeysJson)  [WRITE]
//    Set a module input's CURVE keys. SCOPED to inputs whose type is the float Curve data interface
//    ("Curve for Floats" / UNiagaraDataInterfaceCurve), with NO existing linked override — creates the DI on
//    the input's override pin via the exported SetDataInterfaceValueForFunctionInput, then populates its public
//    FRichCurve. KeysJson is a JSON array: [{"time":0.0,"value":1.0,"interp":"cubic|linear|constant"}, ...].
//    Inputs that already carry a curve DI (in-place edit) or non-float curve inputs return an honest error.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraCurve(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& KeysJson)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia3_LoadSystem(SystemPath);
    if (!System) { return MCPNia3_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia3_Error(TEXT("module name is required")); }
    if (InputName.IsEmpty())  { return MCPNia3_Error(TEXT("input name is required")); }

    // Parse the keys array up front.
    TArray<TSharedPtr<FJsonValue>> KeyVals;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(KeysJson);
        if (!FJsonSerializer::Deserialize(Reader, KeyVals))
        {
            return MCPNia3_Error(TEXT("keys_json must be a JSON array of {time,value[,interp]} objects"));
        }
    }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia3_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia3_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all")) { MCPNia3_AllEmitterUsages(Usages); }
    else { ENiagaraScriptUsage U; if (!MCPNia3_ParseUsage(ScriptUsage, U)) { return MCPNia3_Error(TEXT("bad usage")); } Usages.Add(U); }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE; UNiagaraNodeOutput* Out = nullptr;
        if (UNiagaraNodeFunctionCall* M = MCPNia3_FindModuleInUsage(Graph, U, ModuleName, Idx, Out)) { Module = M; FoundUsage = U; break; }
    }
    if (!Module) { return MCPNia3_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    FNiagaraVariable InputVar;
    if (!MCPNia3_FindInput(Module, Instance, FoundUsage, InputName, InputVar))
    {
        return MCPNia3_Error(FString::Printf(TEXT("module '%s' has no input '%s'"), *ModuleName, *InputName));
    }

    // The input must be a float-curve data-interface input to accept a UNiagaraDataInterfaceCurve.
    UClass* InputClass = InputVar.GetType().GetClass();
    if (!InputClass || !InputClass->IsChildOf(UNiagaraDataInterfaceCurve::StaticClass()))
    {
        return MCPNia3_Error(FString::Printf(TEXT("input '%s' is type '%s', not a float Curve data interface "
            "('Curve for Floats'/UNiagaraDataInterfaceCurve). Setting the curve of a float-from-curve DYNAMIC "
            "INPUT (nested curve) is not reachable headless this round."), *InputName, *InputVar.GetType().GetName()));
    }

    FNiagaraParameterHandle AliasedHandle = MCPNia3_AliasedInputHandle(InputVar, Module);
    UEdGraphPin& OverridePin = FNiagaraStackGraphUtilities::GetOrCreateStackFunctionInputOverridePin(
        *Module, AliasedHandle, InputVar.GetType(), FGuid(), FGuid());

    const bool bPrevLinked = OverridePin.LinkedTo.Num() > 0;
    const bool bHadOverride = bPrevLinked || !OverridePin.DefaultValue.IsEmpty();
    if (bPrevLinked)
    {
        return MCPNia3_Error(FString::Printf(TEXT("input '%s' already has an override value; in-place curve edits are "
            "not reachable headless (would require exporting UNiagaraNodeInput::GetDataInterface) — reset the input "
            "in-editor first, then set the curve fresh"), *InputName));
    }

    UNiagaraDataInterface* OutDI = nullptr;
    FNiagaraStackGraphUtilities::SetDataInterfaceValueForFunctionInput(
        OverridePin, UNiagaraDataInterfaceCurve::StaticClass(),
        AliasedHandle.GetParameterHandleString().ToString(), OutDI); // NiagaraStackGraphUtilities.h:231

    UNiagaraDataInterfaceCurve* CurveDI = Cast<UNiagaraDataInterfaceCurve>(OutDI);
    if (!CurveDI) { return MCPNia3_Error(TEXT("failed to create the float Curve data interface on the input")); }

    CurveDI->Modify();
    CurveDI->Curve.Reset();
    int32 KeysWritten = 0;
    for (const TSharedPtr<FJsonValue>& KV : KeyVals)
    {
        const TSharedPtr<FJsonObject>* KObjPtr = nullptr;
        if (!KV.IsValid() || !KV->TryGetObject(KObjPtr) || !KObjPtr) { continue; }
        const TSharedPtr<FJsonObject>& KObj = *KObjPtr;
        double Time = 0.0, Value = 0.0;
        KObj->TryGetNumberField(TEXT("time"), Time);
        KObj->TryGetNumberField(TEXT("value"), Value);
        FKeyHandle H = CurveDI->Curve.AddKey((float)Time, (float)Value);
        FString InterpStr;
        if (KObj->TryGetStringField(TEXT("interp"), InterpStr))
        {
            CurveDI->Curve.SetKeyInterpMode(H, MCPNia3_ParseInterp(InterpStr));
        }
        else
        {
            CurveDI->Curve.SetKeyInterpMode(H, RCIM_Cubic);
        }
        ++KeysWritten;
    }
    CurveDI->Curve.AutoSetTangents();
    CurveDI->UpdateTimeRanges();          // NiagaraDataInterfaceCurve.h:40 — NIAGARA_API (LUT rebuilds on compile)

    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("input"), InputVar.GetName().ToString());
    Root->SetStringField(TEXT("curve_type"), InputVar.GetType().GetName());
    Root->SetNumberField(TEXT("keys_written"), KeysWritten);
    Root->SetBoolField(TEXT("had_override"), bHadOverride);
    Root->SetBoolField(TEXT("prev_linked"), bPrevLinked);
    Root->SetBoolField(TEXT("set"), true);
    return MCPNia3_Serialize(Root);
#else
    return MCPNia3_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 5) ClearNiagaraInputOverride(SystemPath, EmitterName, ScriptUsage, ModuleName, InputName)  [WRITE]
//    Remove a module input's OVERRIDE (dynamic input / linked value / local default / curve DI) on the input's
//    override pin, reverting the input to its module default. This is the FAITHFUL INVERSE for the fresh-input
//    set_niagara_dynamic_input / set_niagara_stack_value / set_niagara_curve writes. Uses the exported
//    RemoveNodesForStackFunctionInputOverridePin. Returns {removed}.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::ClearNiagaraInputOverride(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputName)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia3_LoadSystem(SystemPath);
    if (!System) { return MCPNia3_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia3_Error(TEXT("module name is required")); }
    if (InputName.IsEmpty())  { return MCPNia3_Error(TEXT("input name is required")); }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia3_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia3_Error(Err); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all")) { MCPNia3_AllEmitterUsages(Usages); }
    else { ENiagaraScriptUsage U; if (!MCPNia3_ParseUsage(ScriptUsage, U)) { return MCPNia3_Error(TEXT("bad usage")); } Usages.Add(U); }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    for (ENiagaraScriptUsage U : Usages)
    {
        int32 Idx = INDEX_NONE; UNiagaraNodeOutput* Out = nullptr;
        if (UNiagaraNodeFunctionCall* M = MCPNia3_FindModuleInUsage(Graph, U, ModuleName, Idx, Out)) { Module = M; FoundUsage = U; break; }
    }
    if (!Module) { return MCPNia3_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName)); }

    FNiagaraVariable InputVar;
    if (!MCPNia3_FindInput(Module, Instance, FoundUsage, InputName, InputVar))
    {
        return MCPNia3_Error(FString::Printf(TEXT("module '%s' has no input '%s'"), *ModuleName, *InputName));
    }

    FNiagaraParameterHandle AliasedHandle = MCPNia3_AliasedInputHandle(InputVar, Module);
    UEdGraphPin& OverridePin = FNiagaraStackGraphUtilities::GetOrCreateStackFunctionInputOverridePin(
        *Module, AliasedHandle, InputVar.GetType(), FGuid(), FGuid());

    FNiagaraStackGraphUtilities::RemoveNodesForStackFunctionInputOverridePin(OverridePin); // NiagaraStackGraphUtilities.h:222 (export-patch)

    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), MCPNia3_UsageToString(FoundUsage));
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("input"), InputVar.GetName().ToString());
    Root->SetBoolField(TEXT("removed"), true);
    return MCPNia3_Serialize(Root);
#else
    return MCPNia3_Error(TEXT("editor-only"));
#endif
}
