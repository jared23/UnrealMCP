// ============================================================================
// MCPReflection_Niagara5.cpp  —  Niagara LIVE-RUNTIME particle stats (READ-ONLY).
//
//   get_niagara_particle_stats — read the LIVE per-emitter particle counts +
//   execution state of a spawned Niagara effect during PIE. Operates on a LIVE
//   UNiagaraComponent found on an actor in the running (PIE) world. Python cannot
//   reach FNiagaraSystemInstance / FNiagaraEmitterInstance (they are not
//   UObject-reflected), so this is C++. Pure READ — NO ledger, no mutation.
//
// ISOLATED translation unit (mirrors MCPReflection_Niagara4.cpp). Anon-namespace
// helpers are prefixed MCPNia5_ (unique across the unity build). Implements a
// DEFERRED MCPReflectionLibrary method that niagara_runtime_cpp.py hasattr-guards
// on; when it links, the Python tool auto-enables.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Every referenced symbol is EITHER (a) NIAGARA_API-exported in stock 5.8, OR
//   (b) a header-only inline / template. Confirmed 5.8 signatures (header:line):
//     UNiagaraComponent::GetSystemInstanceController() const  inline            NiagaraComponent.h:409
//     UNiagaraComponent::GetExecutionState() const            NIAGARA_API       NiagaraComponent.h:232
//     UNiagaraComponent::IsComplete() const                   NIAGARA_API       NiagaraComponent.h:234
//     UNiagaraComponent::IsActive() const  (UActorComponent)  ENGINE_API        ActorComponent.h
//     FNiagaraSystemInstanceController::GetSystemInstance_Unsafe() const inline  NiagaraSystemInstanceController.h:83
//     FNiagaraSystemInstanceController::WaitForConcurrentTickAndFinalize() SHIM  NiagaraSystemInstanceController.h:153
//        (header-only template -> FNiagaraSystemInstance::WaitForConcurrentTickAndFinalize NIAGARA_API NiagaraSystemInstance.h:243)
//     FNiagaraSystemInstance::GetEmitters() const             inline            NiagaraSystemInstance.h:278
//     FNiagaraSystemInstance::GetActualExecutionState()       inline            NiagaraSystemInstance.h:256
//     FNiagaraSystemInstance::GetSystem() const               inline            NiagaraSystemInstance.h:273
//     FNiagaraSystemInstance::GetAge() const                  inline            NiagaraSystemInstance.h:377
//     FNiagaraSystemInstance::GetTickCount() const            inline            NiagaraSystemInstance.h:378
//     FNiagaraEmitterInstance::GetNumParticles() const        NIAGARA_API virt  NiagaraEmitterInstance.h:72  <-- KEY accessor
//     FNiagaraEmitterInstance::GetTotalSpawnedParticles()     inline            NiagaraEmitterInstance.h:73
//     FNiagaraEmitterInstance::GetExecutionState() const      inline            NiagaraEmitterInstance.h:65
//     FNiagaraEmitterInstance::GetSimTarget() const           inline            NiagaraEmitterInstance.h:54
//     FNiagaraEmitterInstance::GetEmitterHandle() const       NIAGARA_API       NiagaraEmitterInstance.h:94
//     FNiagaraEmitterHandle::GetName() const                  NIAGARA_API       NiagaraEmitterHandle.h:56
//   => NO NiagaraEditor symbol, NO export patch, NO Build.cs change (Niagara is
//      already a dep). This is a pure runtime-Niagara reader.
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...}/{"status"}
// on any miss (never crash). Reads are done on the game thread AFTER
// WaitForConcurrentTickAndFinalize so the particle buffers are finalized.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Core / actor ---
#include "GameFramework/Actor.h"

// --- Niagara runtime (NIAGARA_API — Niagara module already a Build.cs dep) ---
#include "NiagaraComponent.h"                    // UNiagaraComponent
#include "NiagaraSystem.h"                        // UNiagaraSystem (name)
#include "NiagaraSystemInstanceController.h"      // FNiagaraSystemInstanceController (GetSystemInstance_Unsafe / WaitForConcurrentTickAndFinalize shim)
#include "NiagaraSystemInstance.h"                // FNiagaraSystemInstance (GetEmitters / exec state / age)
#include "NiagaraEmitterInstance.h"               // FNiagaraEmitterInstance (GetNumParticles etc.)
#include "NiagaraEmitterHandle.h"                 // FNiagaraEmitterHandle (GetName)
#include "NiagaraTypes.h"                         // ENiagaraExecutionState
#include "NiagaraCommon.h"                        // ENiagaraSimTarget

// ---- Extra deps used ONLY by ReorderNiagaraModuleV2 (feature 2) below --------
// Reflection / path
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"
// Base EdGraph (Engine module)
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
// Niagara runtime (extra)
#include "NiagaraEmitter.h"                       // FVersionedNiagaraEmitter / FVersionedNiagaraEmitterData
#include "NiagaraScript.h"                        // UNiagaraScript / ENiagaraScriptUsage
// NiagaraEditor (NIAGARAEDITOR_API — Niagara/NiagaraEditor already Build.cs deps; MoveModule + FindOutputNode
// already carry the pre-existing UnrealMCP export patch, so NO new engine patch is required for V2)
#include "NiagaraScriptSource.h"                  // UNiagaraScriptSource (->NodeGraph)
#include "NiagaraGraph.h"                         // UNiagaraGraph::FindOutputNode (export-patched)
#include "NiagaraNode.h"                          // UNiagaraNode::IsParameterMapPin
#include "NiagaraNodeOutput.h"                    // UNiagaraNodeOutput::GetUsageId
#include "NiagaraNodeFunctionCall.h"              // UNiagaraNodeFunctionCall
#include "ViewModels/Stack/NiagaraStackGraphUtilities.h" // FNiagaraStackGraphUtilities::MoveModule (export-patched)

namespace
{
    FString MCPNia5_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry both "status" and "error" (Python callers branch on res.get("error")).
    FString MCPNia5_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("status"), TEXT("error"));
        Obj->SetStringField(TEXT("error"), Message);
        return MCPNia5_Serialize(Obj);
    }

#if WITH_EDITOR
    FString MCPNia5_ExecStateToString(ENiagaraExecutionState State)
    {
        switch (State)
        {
        case ENiagaraExecutionState::Active:        return TEXT("Active");
        case ENiagaraExecutionState::Inactive:      return TEXT("Inactive");
        case ENiagaraExecutionState::InactiveClear: return TEXT("InactiveClear");
        case ENiagaraExecutionState::Complete:      return TEXT("Complete");
        case ENiagaraExecutionState::Disabled:      return TEXT("Disabled");
        default:                                    return TEXT("Unknown");
        }
    }

    FString MCPNia5_SimTargetToString(ENiagaraSimTarget Target)
    {
        switch (Target)
        {
        case ENiagaraSimTarget::CPUSim:        return TEXT("CPU");
        case ENiagaraSimTarget::GPUComputeSim: return TEXT("GPU");
        default:                               return TEXT("Unknown");
        }
    }

    // Build one component's live-stats object. Never mutates. Safe on any component state
    // (idle / not-yet-initialized -> has_instance:false, particle_count 0).
    TSharedRef<FJsonObject> MCPNia5_ComponentStats(UNiagaraComponent* Comp)
    {
        TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
        C->SetStringField(TEXT("component"), Comp->GetName());
        C->SetStringField(TEXT("component_class"), Comp->GetClass()->GetName());
        if (UNiagaraSystem* Sys = Comp->GetAsset())
        {
            C->SetStringField(TEXT("system_asset"), Sys->GetName());
            C->SetStringField(TEXT("system_path"), Sys->GetPathName());
        }
        C->SetStringField(TEXT("component_exec_state"), MCPNia5_ExecStateToString(Comp->GetExecutionState()));
        C->SetBoolField(TEXT("is_active"), Comp->IsActive());
        C->SetBoolField(TEXT("is_complete"), Comp->IsComplete());
        C->SetBoolField(TEXT("is_paused"), Comp->IsPaused());

        FNiagaraSystemInstanceControllerConstPtr Controller = Comp->GetSystemInstanceController();
        if (!Controller.IsValid() || !Controller->IsValid())
        {
            C->SetBoolField(TEXT("has_instance"), false);
            C->SetNumberField(TEXT("total_particles"), 0);
            C->SetStringField(TEXT("note"), TEXT("component has no live system instance (not activated / already destroyed / scalability-culled)"));
            return C;
        }

        // Finalize any in-flight concurrent tick so the particle buffers we read are current + stable.
        // Non-const shim -> use a mutable controller handle.
        if (FNiagaraSystemInstanceControllerPtr MutController = Comp->GetSystemInstanceController())
        {
            MutController->WaitForConcurrentTickAndFinalize();
        }

        // GetSystemInstance_Unsafe() returns a non-const FNiagaraSystemInstance* even from the const handle.
        FNiagaraSystemInstance* Inst = Controller->GetSystemInstance_Unsafe();
        if (!Inst)
        {
            C->SetBoolField(TEXT("has_instance"), false);
            C->SetNumberField(TEXT("total_particles"), 0);
            C->SetStringField(TEXT("note"), TEXT("controller valid but system instance is null"));
            return C;
        }

        C->SetBoolField(TEXT("has_instance"), true);
        C->SetStringField(TEXT("system_exec_state"),
            MCPNia5_ExecStateToString(Inst->GetActualExecutionState()));
        C->SetNumberField(TEXT("age"), Inst->GetAge());
        C->SetNumberField(TEXT("tick_count"), Inst->GetTickCount());

        int32 TotalParticles = 0;
        TArray<TSharedPtr<FJsonValue>> Emitters;
        TConstArrayView<FNiagaraEmitterInstanceRef> EmitterInsts = Inst->GetEmitters();
        for (int32 i = 0; i < EmitterInsts.Num(); ++i)
        {
            const FNiagaraEmitterInstance& E = EmitterInsts[i].Get();
            const int32 Num = E.GetNumParticles();           // KEY: NIAGARA_API exported accessor
            TotalParticles += Num;

            TSharedRef<FJsonObject> EObj = MakeShared<FJsonObject>();
            EObj->SetNumberField(TEXT("index"), i);
            EObj->SetStringField(TEXT("emitter"), E.GetEmitterHandle().GetName().ToString());
            EObj->SetNumberField(TEXT("particle_count"), Num);
            EObj->SetNumberField(TEXT("total_spawned"), E.GetTotalSpawnedParticles());
            EObj->SetStringField(TEXT("exec_state"), MCPNia5_ExecStateToString(E.GetExecutionState()));
            EObj->SetStringField(TEXT("sim_target"), MCPNia5_SimTargetToString(E.GetSimTarget()));
            EObj->SetBoolField(TEXT("is_active"), E.IsActive());
            EObj->SetBoolField(TEXT("is_complete"), E.IsComplete());
            Emitters.Add(MakeShared<FJsonValueObject>(EObj));
        }

        C->SetNumberField(TEXT("emitter_count"), Emitters.Num());
        C->SetNumberField(TEXT("total_particles"), TotalParticles);
        C->SetArrayField(TEXT("emitters"), Emitters);
        return C;
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// GetNiagaraParticleStatsJson(Actor, ComponentName)  [READ — no ledger]
//   Read live per-emitter particle counts + execution state of every (or one
//   named) UNiagaraComponent on Actor. Intended to run in PIE against a spawned
//   Niagara effect. ComponentName empty -> ALL Niagara components on the actor;
//   otherwise case-insensitive exact/substring match on the component name.
//
//   Returns {status, actor, component_count, components:[{component, system_asset,
//   component_exec_state, is_active, is_complete, is_paused, has_instance,
//   [system_exec_state, age, tick_count, emitter_count, total_particles,
//   emitters:[{index, emitter, particle_count, total_spawned, exec_state,
//   sim_target, is_active, is_complete}]]}], grand_total_particles}.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraParticleStatsJson(AActor* Actor, const FString& ComponentName)
{
#if WITH_EDITOR
    if (!Actor) { return MCPNia5_Error(TEXT("null actor")); }

    TArray<UNiagaraComponent*> Comps;
    Actor->GetComponents<UNiagaraComponent>(Comps);
    if (Comps.Num() == 0)
    {
        return MCPNia5_Error(FString::Printf(
            TEXT("actor '%s' has no UNiagaraComponent (spawn a Niagara effect on it first)"), *Actor->GetName()));
    }

    const FString Filter = ComponentName.TrimStartAndEnd();
    const bool bFilter = !Filter.IsEmpty();

    TArray<TSharedPtr<FJsonValue>> ComponentsJson;
    int32 GrandTotal = 0;
    for (UNiagaraComponent* Comp : Comps)
    {
        if (!Comp) { continue; }
        if (bFilter)
        {
            const FString Name = Comp->GetName();
            if (!(Name == Filter || Name.Contains(Filter, ESearchCase::IgnoreCase))) { continue; }
        }
        TSharedRef<FJsonObject> C = MCPNia5_ComponentStats(Comp);
        int32 Total = 0;
        C->TryGetNumberField(TEXT("total_particles"), Total);
        GrandTotal += Total;
        ComponentsJson.Add(MakeShared<FJsonValueObject>(C));
    }

    if (bFilter && ComponentsJson.Num() == 0)
    {
        return MCPNia5_Error(FString::Printf(
            TEXT("no UNiagaraComponent on actor '%s' matched component name '%s'"), *Actor->GetName(), *Filter));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetNumberField(TEXT("component_count"), ComponentsJson.Num());
    Root->SetArrayField(TEXT("components"), ComponentsJson);
    Root->SetNumberField(TEXT("grand_total_particles"), GrandTotal);
    return MCPNia5_Serialize(Root);
#else
    return MCPNia5_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// FEATURE 2 — ReorderNiagaraModuleV2 (THE SAFE, NON-CRASHING module reorder).
//
//   The prior ReorderNiagaraModule (MCPReflection_Niagara3.cpp) passed the user's
//   new_index straight through as MoveModule's TargetModuleIndex and only guarded
//   new_index == prev_index. That is WRONG in two ways and corrupted the emitter
//   param-map into a self-loop cycle -> NiagaraGraph::BuildTraversalHelper
//   infinite-recursed on the next compile (stack overflow / editor crash):
//
//     (a) MoveModule captures its TargetGroups list BEFORE the source module is
//         disconnected, so TargetModuleIndex is interpreted in PRE-removal indexing.
//         Passing T == prev_index (S) OR T == S+1 reconnects the moved module's
//         param-map OUTPUT pin to its OWN input pin -> a self-referential link (a
//         cycle). Both are also no-op positions. The stock stack UI's own guard
//         (NiagaraStackScriptItemGroup::CanDropEntriesOnTarget, ...cpp:1104-1111)
//         rejects exactly these two "won't actually move" targets. The old code
//         guarded only S, never S+1 -> the crash.
//     (b) For a RIGHTWARD move (desired final index F > S), because the source is
//         removed from a slot to the LEFT of the insertion point, MoveModule lands
//         the module at final position T-1. So to achieve final index F you must
//         pass T = F+1 (not F). The old code passed T = F -> off-by-one AND, when
//         F == S+1, T == S+1 -> the crash case again.
//
//   FIX (this V2): map the user's desired FINAL 0-based module index F to the
//   correct pre-removal TargetModuleIndex, and it is then provably never in the
//   degenerate {S, S+1} set:
//       F == S            -> no-op (already there)
//       F <  S            -> T = F           (T in [0, S-1]; never S or S+1)
//       F >  S            -> T = F + 1       (T in [S+2, N]; never S or S+1)
//   This keeps the param-map ACYCLIC, so the follow-up RequestCompile traverses
//   normally. Uses the SAME already-export-patched MoveModule + FindOutputNode as
//   Niagara3.cpp — NO new engine patch, NO Build.cs change. Helpers re-implemented
//   here (MCPNia5_ prefixed) so this stays an isolated translation unit.
// ============================================================================

namespace
{
#if WITH_EDITOR
    UNiagaraSystem* MCPNia5_LoadSystem(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        if (UNiagaraSystem* Sys = Cast<UNiagaraSystem>(FSoftObjectPath(Path).TryLoad())) { return Sys; }
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UNiagaraSystem>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    bool MCPNia5_ParseUsage(const FString& In, ENiagaraScriptUsage& Out)
    {
        const FString U = In.ToLower();
        if (U == TEXT("particle_spawn"))  { Out = ENiagaraScriptUsage::ParticleSpawnScript;  return true; }
        if (U == TEXT("particle_update")) { Out = ENiagaraScriptUsage::ParticleUpdateScript; return true; }
        if (U == TEXT("emitter_spawn"))   { Out = ENiagaraScriptUsage::EmitterSpawnScript;   return true; }
        if (U == TEXT("emitter_update"))  { Out = ENiagaraScriptUsage::EmitterUpdateScript;  return true; }
        return false;
    }

    const TCHAR* MCPNia5_UsageToString(ENiagaraScriptUsage Usage)
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

    void MCPNia5_AllEmitterUsages(TArray<ENiagaraScriptUsage>& Out)
    {
        Out.Reset();
        Out.Add(ENiagaraScriptUsage::EmitterSpawnScript);
        Out.Add(ENiagaraScriptUsage::EmitterUpdateScript);
        Out.Add(ENiagaraScriptUsage::ParticleSpawnScript);
        Out.Add(ENiagaraScriptUsage::ParticleUpdateScript);
    }

    UNiagaraScript* MCPNia5_ScriptForUsage(FVersionedNiagaraEmitterData* Data, ENiagaraScriptUsage Usage)
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

    bool MCPNia5_FindEmitterHandle(UNiagaraSystem* System, const FString& EmitterName,
                                   FVersionedNiagaraEmitter& OutInstance, FGuid& OutHandleId)
    {
        if (!System) { return false; }
        for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
        {
            if (H.GetName().ToString() == EmitterName)
            {
                OutInstance = H.GetInstance();
                OutHandleId = H.GetId();
                return true;
            }
        }
        return false;
    }

    UNiagaraGraph* MCPNia5_ResolveEmitterGraph(UNiagaraSystem* System, const FString& EmitterName,
        FVersionedNiagaraEmitter& OutInstance, FGuid& OutHandleId, FString& OutErr)
    {
        if (!System) { OutErr = TEXT("null system"); return nullptr; }
        if (EmitterName.IsEmpty()) { OutErr = TEXT("emitter name is required"); return nullptr; }
        if (!MCPNia5_FindEmitterHandle(System, EmitterName, OutInstance, OutHandleId))
        {
            OutErr = FString::Printf(TEXT("no emitter handle named '%s'"), *EmitterName);
            return nullptr;
        }
        FVersionedNiagaraEmitterData* Data = OutInstance.GetEmitterData();
        if (!Data) { OutErr = TEXT("emitter has no versioned data"); return nullptr; }
        UNiagaraScript* AnyScript = Data->SpawnScriptProps.Script;   // all emitter scripts share one graph
        if (!AnyScript) { OutErr = TEXT("emitter spawn script missing"); return nullptr; }
        UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(AnyScript->GetLatestSource());
        if (!Source) { OutErr = TEXT("script source is not a graph-backed UNiagaraScriptSource"); return nullptr; }
        UNiagaraGraph* Graph = Source->NodeGraph;
        if (!Graph) { OutErr = TEXT("script source has no NodeGraph"); return nullptr; }
        return Graph;
    }

    // GetParameterMapInputPin re-implemented on the exported UNiagaraNode::IsParameterMapPin.
    UEdGraphPin* MCPNia5_ParamMapInputPin(UNiagaraNode* Node)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && Node->IsParameterMapPin(P)) { return P; }
        }
        return nullptr;
    }

    // GetOrderedModuleNodes re-implemented: walk backward from the output node along the single-linked
    // parameter-map input pin, collecting function-call (module) nodes in stack order (index 0 = first).
    void MCPNia5_GetOrderedModules(UNiagaraNodeOutput* OutputNode, TArray<UNiagaraNodeFunctionCall*>& OutModules)
    {
        OutModules.Reset();
        UNiagaraNode* PreviousNode = OutputNode;
        int32 Guard = 0;
        while (PreviousNode != nullptr && Guard++ < 4096)
        {
            UEdGraphPin* InputPin = MCPNia5_ParamMapInputPin(PreviousNode);
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

    UNiagaraNodeFunctionCall* MCPNia5_FindModuleInUsage(UNiagaraGraph* Graph, ENiagaraScriptUsage Usage,
        const FString& ModuleName, int32& OutIndex, UNiagaraNodeOutput*& OutOutputNode)
    {
        OutIndex = INDEX_NONE;
        OutOutputNode = nullptr;
        if (!Graph) { return nullptr; }
        UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(Usage);   // export-patched (NiagaraGraph.h:270)
        if (!OutputNode) { return nullptr; }
        OutOutputNode = OutputNode;
        TArray<UNiagaraNodeFunctionCall*> Modules;
        MCPNia5_GetOrderedModules(OutputNode, Modules);
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
// ReorderNiagaraModuleV2(SystemPath, EmitterName, ScriptUsage, ModuleName, NewIndex)  [WRITE]
//   Move a module to the 0-based FINAL index NewIndex within its emitter stack via the engine's
//   own drag-drop reorder (FNiagaraStackGraphUtilities::MoveModule) with the CORRECT index mapping
//   (see the header block above) so the param-map stays acyclic. ScriptUsage empty/"all" -> search
//   every emitter usage for the module. Returns {system,emitter,usage,module,prev_index,new_index,
//   module_count,moved[,note]}. prev_index is what the Python ledger inverse targets.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::ReorderNiagaraModuleV2(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, int32 NewIndex)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNia5_LoadSystem(SystemPath);
    if (!System) { return MCPNia5_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (ModuleName.IsEmpty()) { return MCPNia5_Error(TEXT("module name is required")); }
    if (NewIndex < 0) { return MCPNia5_Error(TEXT("new_index must be >= 0")); }

    FVersionedNiagaraEmitter Instance;
    FGuid HandleId;
    FString Err;
    UNiagaraGraph* Graph = MCPNia5_ResolveEmitterGraph(System, EmitterName, Instance, HandleId, Err);
    if (!Graph) { return MCPNia5_Error(Err); }
    FVersionedNiagaraEmitterData* Data = Instance.GetEmitterData();
    if (!Data) { return MCPNia5_Error(TEXT("emitter has no versioned data")); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        MCPNia5_AllEmitterUsages(Usages);
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPNia5_ParseUsage(ScriptUsage, U))
        {
            return MCPNia5_Error(TEXT("bad usage (particle_spawn|particle_update|emitter_spawn|emitter_update|all)"));
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
        if (UNiagaraNodeFunctionCall* M = MCPNia5_FindModuleInUsage(Graph, U, ModuleName, Idx, Out))
        {
            Module = M; FoundUsage = U; PrevIndex = Idx; OutputNode = Out; break;
        }
    }
    if (!Module || !OutputNode)
    {
        return MCPNia5_Error(FString::Printf(TEXT("no module named '%s' in the emitter's stack(s)"), *ModuleName));
    }

    // Ordered module list of the found usage: gives N (module count) and validates the requested index.
    TArray<UNiagaraNodeFunctionCall*> ModsInUsage;
    MCPNia5_GetOrderedModules(OutputNode, ModsInUsage);
    const int32 ModuleCount = ModsInUsage.Num();
    const int32 S = PrevIndex;                                   // source's current 0-based module index
    int32 F = FMath::Clamp(NewIndex, 0, ModuleCount - 1);        // desired FINAL 0-based module index

    auto MakeResult = [&](int32 LandedIndex, bool bMoved, const TCHAR* Note) -> FString
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        Root->SetStringField(TEXT("system"), System->GetName());
        Root->SetStringField(TEXT("emitter"), EmitterName);
        Root->SetStringField(TEXT("usage"), MCPNia5_UsageToString(FoundUsage));
        Root->SetStringField(TEXT("module"), Module->GetFunctionName());
        Root->SetNumberField(TEXT("prev_index"), S);            // Python: inverse reorder targets this
        Root->SetNumberField(TEXT("new_index"), LandedIndex);
        Root->SetNumberField(TEXT("module_count"), ModuleCount);
        Root->SetBoolField(TEXT("moved"), bMoved);
        if (Note) { Root->SetStringField(TEXT("note"), Note); }
        return MCPNia5_Serialize(Root);
    };

    // No-op: already at the requested final index (this is the S==T degenerate; DO NOT call MoveModule).
    if (F == S)
    {
        return MakeResult(S, false, TEXT("already at the requested index (no-op)"));
    }

    // Map desired FINAL index F -> MoveModule's pre-removal TargetModuleIndex T.
    //   F < S -> T = F     (in [0, S-1])
    //   F > S -> T = F + 1 (in [S+2, N])
    // Both are provably outside the crashing {S, S+1} degenerate set.
    const int32 T = (F < S) ? F : (F + 1);
    // Defensive clamp to the legal insert range [0, N]; never let T become S or S+1.
    const int32 TargetModuleIndex = FMath::Clamp(T, 0, ModuleCount);
    if (TargetModuleIndex == S || TargetModuleIndex == S + 1)
    {
        // Should be unreachable given the mapping above; refuse rather than risk the cycle.
        return MakeResult(S, false, TEXT("refused: computed target index is a no-op/self-loop position"));
    }

    UNiagaraScript* SourceScript = MCPNia5_ScriptForUsage(Data, FoundUsage);
    if (!SourceScript) { return MCPNia5_Error(TEXT("could not resolve the emitter script for the module's usage")); }

    UNiagaraNodeFunctionCall* OutMoved = nullptr;
    FNiagaraStackGraphUtilities::MoveModule(
        *SourceScript, *Module, *System, HandleId, FoundUsage, OutputNode->GetUsageId(),
        TargetModuleIndex, /*bForceCopy*/ false, OutMoved);      // NiagaraStackGraphUtilities.h:324 (export-patched)

    // Graph is acyclic now, so compile traverses normally (no BuildTraversalHelper recursion).
    System->RequestCompile(false);
    System->MarkPackageDirty();

    // Recompute the module's landed index for truthful reporting/verification.
    int32 LandedIndex = F;
    {
        UNiagaraNodeFunctionCall* MovedRef = OutMoved ? OutMoved : Module;
        if (UNiagaraNodeOutput* PostOut = Graph->FindOutputNode(FoundUsage))
        {
            TArray<UNiagaraNodeFunctionCall*> PostMods;
            MCPNia5_GetOrderedModules(PostOut, PostMods);
            const int32 FoundAt = PostMods.IndexOfByKey(MovedRef);
            if (FoundAt != INDEX_NONE) { LandedIndex = FoundAt; }
        }
    }

    return MakeResult(LandedIndex, true, nullptr);
#else
    return MCPNia5_Error(TEXT("editor-only"));
#endif
}
