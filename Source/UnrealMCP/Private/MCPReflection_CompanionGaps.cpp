// ============================================================================
// MCPReflection_CompanionGaps.cpp — C++ #62. Companion-Game MCP_TOOLING_GAPS
// batch logged 2026-11-11 .. 2026-11-17. SELF-CONTAINED translation unit
// (anon-namespace helpers prefixed MCPCG_ for unity-build uniqueness).
//
//   GetNiagaraModuleInputValues(system, emitter, usage, module, filter)
//       -> the VALUE behind every input of one emitter module: local override-pin literal, linked
//          parameter, dynamic input (+ its own inputs, recursively), data interface (+ its props),
//          or the rapid-iteration constant (where unset stack values actually live). Plus static
//          switches. get_niagara_module_inputs only gives names/types.
//   GetNiagaraParticlePositions(actor, component, emitter, max, attributes_json)
//       -> live CPU-sim particle positions (+ optional float attributes) read from the emitter's
//          particle data buffer, with local->world transform, bounds and max radius from the
//          component. UE 5.8 has no GetNiagaraParticlePositions_DebugOnly any more.
//   RefreshMontageSegments(montage)
//       -> re-fit each slot segment to its sequence's CURRENT play length (after a reimport made
//          the clip longer/shorter), re-chain segment start positions, recompute the montage length.
//
// LINK-RISK DISCIPLINE: only NIAGARA_API / NIAGARAEDITOR_API-exported symbols, inline/template
// header code, the pre-existing FindOutputNode export patch, and reflection (UNiagaraNodeInput's
// private DataInterface UPROPERTY is read by FProperty, not by the non-exported GetDataInterface;
// the ParameterMapSet/Get override nodes live in PRIVATE NiagaraEditor headers, so they are
// recognised by class name instead of Cast). Engine-module montage API only.
// All handlers return CONDENSED JSON with "status" ("ok"/"error") + "error" on failure.
// ============================================================================
#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"
#include "Policies/CondensedJsonPrintPolicy.h"

// --- Core / reflection ---
#include "UObject/UnrealType.h"
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"
#include "GameFramework/Actor.h"

// --- Base EdGraph ---
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"

// --- Niagara runtime ---
#include "NiagaraSystem.h"
#include "NiagaraEmitter.h"
#include "NiagaraEmitterHandle.h"
#include "NiagaraTypes.h"
#include "NiagaraScript.h"
#include "NiagaraParameterStore.h"
#include "NiagaraComponent.h"
#include "NiagaraSystemInstanceController.h"
#include "NiagaraSystemInstance.h"
#include "NiagaraEmitterInstance.h"
#include "NiagaraDataSet.h"
#include "NiagaraDataSetAccessor.h"
#include "NiagaraDataInterface.h"

// --- NiagaraEditor (exported symbols + the FindOutputNode export patch) ---
#include "NiagaraScriptSource.h"
#include "NiagaraGraph.h"
#include "NiagaraNode.h"
#include "NiagaraNodeOutput.h"
#include "NiagaraNodeFunctionCall.h"
#include "NiagaraNodeInput.h"
#include "NiagaraParameterMapHistory.h"
#include "ViewModels/Stack/NiagaraParameterHandle.h"
#include "ViewModels/Stack/NiagaraStackGraphUtilities.h"

// --- Animation (Engine) ---
#include "Animation/AnimMontage.h"
#include "Animation/AnimCompositeBase.h"
#include "Animation/AnimSequenceBase.h"

namespace
{
    FString MCPCG_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> W =
            TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    }

    FString MCPCG_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return MCPCG_Serialize(O);
    }

    TSharedRef<FJsonObject> MCPCG_Ok()
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("ok"));
        return O;
    }

    TArray<TSharedPtr<FJsonValue>> MCPCG_Vec(const FVector& V, double Round = 0.0)
    {
        // Divide by the inverse (10 for 0.1) so the result is the nearest double to N/10 (no 111.60000000000001).
        auto R = [Round](double X) { if (Round <= 0.0) { return X; } const double Inv = FMath::RoundToDouble(1.0 / Round); return FMath::RoundToDouble(X * Inv) / Inv; };
        TArray<TSharedPtr<FJsonValue>> A;
        A.Add(MakeShared<FJsonValueNumber>(R(V.X)));
        A.Add(MakeShared<FJsonValueNumber>(R(V.Y)));
        A.Add(MakeShared<FJsonValueNumber>(R(V.Z)));
        return A;
    }

#if WITH_EDITOR
    // ------------------------------------------------------------------ Niagara helpers
    UNiagaraSystem* MCPCG_LoadSystem(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        if (UNiagaraSystem* S = Cast<UNiagaraSystem>(FSoftObjectPath(Path).TryLoad())) { return S; }
        if (!Path.Contains(TEXT(".")))
        {
            return Cast<UNiagaraSystem>(FSoftObjectPath(Path + TEXT(".") + FPackageName::GetShortName(Path)).TryLoad());
        }
        return nullptr;
    }

    bool MCPCG_ParseUsage(const FString& In, ENiagaraScriptUsage& Out)
    {
        const FString U = In.ToLower();
        if (U == TEXT("particle_spawn"))  { Out = ENiagaraScriptUsage::ParticleSpawnScript;  return true; }
        if (U == TEXT("particle_update")) { Out = ENiagaraScriptUsage::ParticleUpdateScript; return true; }
        if (U == TEXT("emitter_spawn"))   { Out = ENiagaraScriptUsage::EmitterSpawnScript;   return true; }
        if (U == TEXT("emitter_update"))  { Out = ENiagaraScriptUsage::EmitterUpdateScript;  return true; }
        return false;
    }

    const TCHAR* MCPCG_UsageToString(ENiagaraScriptUsage U)
    {
        switch (U)
        {
            case ENiagaraScriptUsage::ParticleSpawnScript:  return TEXT("particle_spawn");
            case ENiagaraScriptUsage::ParticleUpdateScript: return TEXT("particle_update");
            case ENiagaraScriptUsage::EmitterSpawnScript:   return TEXT("emitter_spawn");
            case ENiagaraScriptUsage::EmitterUpdateScript:  return TEXT("emitter_update");
            default:                                        return TEXT("other");
        }
    }

    UNiagaraScript* MCPCG_ScriptForUsage(FVersionedNiagaraEmitterData* Data, ENiagaraScriptUsage U)
    {
        if (!Data) { return nullptr; }
        switch (U)
        {
            case ENiagaraScriptUsage::EmitterSpawnScript:   return Data->EmitterSpawnScriptProps.Script;
            case ENiagaraScriptUsage::EmitterUpdateScript:  return Data->EmitterUpdateScriptProps.Script;
            case ENiagaraScriptUsage::ParticleSpawnScript:  return Data->SpawnScriptProps.Script;
            case ENiagaraScriptUsage::ParticleUpdateScript: return Data->UpdateScriptProps.Script;
            default:                                        return nullptr;
        }
    }

    UEdGraphPin* MCPCG_ParamMapInputPin(UNiagaraNode* Node)
    {
        if (!Node) { return nullptr; }
        for (UEdGraphPin* P : Node->Pins)
        {
            if (P && P->Direction == EGPD_Input && Node->IsParameterMapPin(P)) { return P; }
        }
        return nullptr;
    }

    void MCPCG_OrderedModules(UNiagaraNodeOutput* OutputNode, TArray<UNiagaraNodeFunctionCall*>& Out)
    {
        Out.Reset();
        UNiagaraNode* Prev = OutputNode;
        int32 Guard = 0;
        while (Prev && Guard++ < 4096)
        {
            UEdGraphPin* In = MCPCG_ParamMapInputPin(Prev);
            if (In && In->LinkedTo.Num() == 1 && In->LinkedTo[0])
            {
                UNiagaraNode* Cur = Cast<UNiagaraNode>(In->LinkedTo[0]->GetOwningNode());
                if (UNiagaraNodeFunctionCall* M = Cast<UNiagaraNodeFunctionCall>(Cur)) { Out.Insert(M, 0); }
                Prev = Cur;
            }
            else { Prev = nullptr; }
        }
    }

    // The ParameterMapSet "override node" that feeds a function call's parameter-map input (the engine's
    // GetStackFunctionOverrideNode, re-implemented; the class is in a PRIVATE header -> match by name).
    UEdGraphNode* MCPCG_OverrideNode(UNiagaraNodeFunctionCall* Fn)
    {
        UEdGraphPin* In = MCPCG_ParamMapInputPin(Fn);
        if (!In || In->LinkedTo.Num() != 1 || !In->LinkedTo[0]) { return nullptr; }
        UEdGraphNode* N = In->LinkedTo[0]->GetOwningNode();
        return (N && N->GetClass()->GetName().Contains(TEXT("ParameterMapSet"))) ? N : nullptr;
    }

    // Read-only twin of GetStackFunctionInputOverridePin (that one is not exported, and the exported
    // GetOrCreate... would CREATE a pin — a read must not mutate the graph).
    UEdGraphPin* MCPCG_FindOverridePin(UNiagaraNodeFunctionCall* Fn, const FName AliasedName)
    {
        UEdGraphNode* Ov = MCPCG_OverrideNode(Fn);
        if (!Ov) { return nullptr; }
        for (UEdGraphPin* P : Ov->Pins)
        {
            if (P && P->Direction == EGPD_Input && P->PinName == AliasedName) { return P; }
        }
        return nullptr;
    }

    // Format one parameter-store entry by type.
    TSharedPtr<FJsonValue> MCPCG_StoreValue(const FNiagaraParameterStore& Store, const FNiagaraVariableWithOffset& VO)
    {
        const uint8* D = Store.GetParameterData(VO.Offset);
        if (!D) { return MakeShared<FJsonValueNull>(); }
        const FNiagaraTypeDefinition& T = VO.GetType();
        auto Floats = [D](int32 N) -> TSharedPtr<FJsonValue>
        {
            TArray<TSharedPtr<FJsonValue>> A;
            const float* P = reinterpret_cast<const float*>(D);
            for (int32 i = 0; i < N; ++i) { A.Add(MakeShared<FJsonValueNumber>(P[i])); }
            return MakeShared<FJsonValueArray>(A);
        };
        if (T == FNiagaraTypeDefinition::GetFloatDef()) { float v; FMemory::Memcpy(&v, D, 4); return MakeShared<FJsonValueNumber>(v); }
        if (T == FNiagaraTypeDefinition::GetIntDef())   { int32 v; FMemory::Memcpy(&v, D, 4); return MakeShared<FJsonValueNumber>((double)v); }
        if (T == FNiagaraTypeDefinition::GetBoolDef())  { int32 v; FMemory::Memcpy(&v, D, 4); return MakeShared<FJsonValueBoolean>(v != 0); }
        if (T == FNiagaraTypeDefinition::GetVec2Def())  { return Floats(2); }
        if (T == FNiagaraTypeDefinition::GetVec3Def() || T == FNiagaraTypeDefinition::GetPositionDef()) { return Floats(3); }
        if (T == FNiagaraTypeDefinition::GetVec4Def() || T == FNiagaraTypeDefinition::GetColorDef() || T == FNiagaraTypeDefinition::GetQuatDef()) { return Floats(4); }
        if (T.IsEnum() || T.GetSize() == 4)
        {
            int32 v; FMemory::Memcpy(&v, D, 4);
            if (T.IsEnum() && T.GetEnum())
            {
                return MakeShared<FJsonValueString>(T.GetEnum()->GetNameStringByValue(v));
            }
            return MakeShared<FJsonValueNumber>((double)v);
        }
        // Unknown struct: a hex dump keeps it inspectable.
        return MakeShared<FJsonValueString>(FString::FromHexBlob(D, FMath::Min(T.GetSize(), 64)));
    }

    // Find a rapid-iteration constant whose name ends with ".<Aliased>" (the engine names them
    // "Constants.<UniqueEmitterName>.<Aliased>" — matching the suffix avoids the non-exported
    // CreateRapidIterationParameter / ConvertVariableToRapidIterationConstantName).
    bool MCPCG_FindRapidIteration(UNiagaraScript* Script, const FString& Aliased, TSharedPtr<FJsonValue>& OutVal, FString& OutName)
    {
        if (!Script) { return false; }
        const FString Suffix = TEXT(".") + Aliased;
        const FNiagaraParameterStore& RI = Script->RapidIterationParameters;
        for (const FNiagaraVariableWithOffset& VO : RI.ReadParameterVariables())
        {
            const FString N = VO.GetName().ToString();
            if (N.EndsWith(Suffix, ESearchCase::IgnoreCase))
            {
                OutVal = MCPCG_StoreValue(RI, VO);
                OutName = N;
                return true;
            }
        }
        return false;
    }

    // Export the editable UPROPERTYs of a data interface (curve keys, mesh refs, ...) as text.
    TSharedRef<FJsonObject> MCPCG_ObjectProps(UObject* Obj, int32 MaxLen = 2000)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        if (!Obj) { return O; }
        for (TFieldIterator<FProperty> It(Obj->GetClass()); It; ++It)
        {
            if (!It->HasAnyPropertyFlags(CPF_Edit)) { continue; }
            FString V;
            It->ExportTextItem_InContainer(V, Obj, nullptr, Obj, PPF_None);
            if (V.Len() > MaxLen) { V = V.Left(MaxLen) + TEXT("...(truncated)"); }
            O->SetStringField(It->GetName(), V);
        }
        return O;
    }

    // Describe the value of ONE function input (module or dynamic input), recursing into dynamic inputs.
    TSharedRef<FJsonObject> MCPCG_DescribeInput(UNiagaraNodeFunctionCall* Fn, const FNiagaraVariable& InputVar,
        bool bHidden, UNiagaraScript* Script, const FVersionedNiagaraEmitter& Instance, ENiagaraScriptUsage Usage, int32 Depth);

    TArray<TSharedPtr<FJsonValue>> MCPCG_DescribeFunctionInputs(UNiagaraNodeFunctionCall* Fn, UNiagaraScript* Script,
        const FVersionedNiagaraEmitter& Instance, ENiagaraScriptUsage Usage, const FString& Filter, int32 Depth)
    {
        TArray<TSharedPtr<FJsonValue>> Arr;
        if (!Fn || !Fn->HasValidScriptAndGraph() || Depth > 6) { return Arr; }
        FCompileConstantResolver Resolver(Instance, Usage);
        TArray<FNiagaraVariable> Inputs;
        TSet<FNiagaraVariable> Hidden;
        FNiagaraStackGraphUtilities::GetStackFunctionInputs(*Fn, Inputs, Hidden, Resolver,
            FNiagaraStackGraphUtilities::ENiagaraGetStackFunctionInputPinsOptions::ModuleInputsOnly, /*bIgnoreDisabled*/ false);
        const FString F = Filter.ToLower();
        for (const FNiagaraVariable& V : Inputs)
        {
            if (!F.IsEmpty() && !V.GetName().ToString().ToLower().Contains(F)) { continue; }
            Arr.Add(MakeShared<FJsonValueObject>(MCPCG_DescribeInput(Fn, V, Hidden.Contains(V), Script, Instance, Usage, Depth)));
        }
        return Arr;
    }

    TSharedRef<FJsonObject> MCPCG_DescribeInput(UNiagaraNodeFunctionCall* Fn, const FNiagaraVariable& InputVar,
        bool bHidden, UNiagaraScript* Script, const FVersionedNiagaraEmitter& Instance, ENiagaraScriptUsage Usage, int32 Depth)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        const FString Full = InputVar.GetName().ToString();           // "Module.Lifetime"
        FString Short = Full; int32 Dot;
        if (Full.FindLastChar(TEXT('.'), Dot)) { Short = Full.RightChop(Dot + 1); }
        const FNiagaraParameterHandle Aliased = FNiagaraParameterHandle::CreateAliasedModuleParameterHandle(
            FNiagaraParameterHandle(InputVar.GetName()), Fn);
        const FName AliasedName = Aliased.GetParameterHandleString(); // "InitializeParticle.Lifetime"

        O->SetStringField(TEXT("name"), Short);
        O->SetStringField(TEXT("input"), Full);
        O->SetStringField(TEXT("aliased"), AliasedName.ToString());
        O->SetStringField(TEXT("type"), InputVar.GetType().GetName());
        if (bHidden) { O->SetBoolField(TEXT("hidden"), true); }

        UEdGraphPin* Ov = MCPCG_FindOverridePin(Fn, AliasedName);
        if (Ov && Ov->LinkedTo.Num() > 0 && Ov->LinkedTo[0])
        {
            UEdGraphPin* Src = Ov->LinkedTo[0];
            UEdGraphNode* SrcNode = Src->GetOwningNode();
            const FString Cls = SrcNode ? SrcNode->GetClass()->GetName() : FString();
            if (UNiagaraNodeFunctionCall* Dyn = Cast<UNiagaraNodeFunctionCall>(SrcNode))
            {
                O->SetStringField(TEXT("mode"), TEXT("dynamic_input"));
                O->SetStringField(TEXT("dynamic_input"), Dyn->GetFunctionName());
                O->SetStringField(TEXT("dynamic_input_script"), Dyn->FunctionScript ? Dyn->FunctionScript->GetPathName() : FString());
                O->SetArrayField(TEXT("inputs"), MCPCG_DescribeFunctionInputs(Dyn, Script, Instance, Usage, FString(), Depth + 1));
            }
            else if (UNiagaraNodeInput* InNode = Cast<UNiagaraNodeInput>(SrcNode))
            {
                // Data interface / object input. DataInterface is a PRIVATE UPROPERTY -> reflection.
                UObject* DI = nullptr;
                if (FObjectPropertyBase* P = FindFProperty<FObjectPropertyBase>(UNiagaraNodeInput::StaticClass(), TEXT("DataInterface")))
                {
                    DI = P->GetObjectPropertyValue_InContainer(InNode);
                }
                UObject* ObjAsset = nullptr;
                if (FObjectPropertyBase* P = FindFProperty<FObjectPropertyBase>(UNiagaraNodeInput::StaticClass(), TEXT("ObjectAsset")))
                {
                    ObjAsset = P->GetObjectPropertyValue_InContainer(InNode);
                }
                if (DI)
                {
                    O->SetStringField(TEXT("mode"), TEXT("data_interface"));
                    O->SetStringField(TEXT("data_interface_class"), DI->GetClass()->GetName());
                    O->SetObjectField(TEXT("data_interface"), MCPCG_ObjectProps(DI));
                }
                else if (ObjAsset)
                {
                    O->SetStringField(TEXT("mode"), TEXT("object"));
                    O->SetStringField(TEXT("object"), ObjAsset->GetPathName());
                }
                else
                {
                    O->SetStringField(TEXT("mode"), TEXT("input_node"));
                    O->SetStringField(TEXT("parameter"), InNode->Input.GetName().ToString());
                }
            }
            else if (Cls.Contains(TEXT("ParameterMapGet")))
            {
                O->SetStringField(TEXT("mode"), TEXT("linked"));
                O->SetStringField(TEXT("linked"), Src->PinName.ToString());   // e.g. "User.FlightTime"
            }
            else
            {
                O->SetStringField(TEXT("mode"), TEXT("linked_node"));
                O->SetStringField(TEXT("node_class"), Cls);
                O->SetStringField(TEXT("pin"), Src->PinName.ToString());
            }
            return O;
        }
        if (Ov && !Ov->DefaultValue.IsEmpty())
        {
            O->SetStringField(TEXT("mode"), TEXT("local"));
            O->SetStringField(TEXT("value"), Ov->DefaultValue);
            return O;
        }
        // No override: unset stack values live as rapid-iteration constants on the compiled script.
        TSharedPtr<FJsonValue> RIVal; FString RIName;
        if (MCPCG_FindRapidIteration(Script, AliasedName.ToString(), RIVal, RIName))
        {
            O->SetStringField(TEXT("mode"), TEXT("rapid_iteration"));
            O->SetField(TEXT("value"), RIVal);
            O->SetStringField(TEXT("rapid_iteration_name"), RIName);
            return O;
        }
        O->SetStringField(TEXT("mode"), TEXT("module_default"));
        O->SetStringField(TEXT("note"), TEXT("no override and no rapid-iteration constant: the module script's own default applies (compile the system to materialise RI constants)"));
        return O;
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// GetNiagaraModuleInputValues
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraModuleInputValues(const FString& SystemPath, const FString& EmitterName,
    const FString& ScriptUsage, const FString& ModuleName, const FString& InputFilter)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPCG_LoadSystem(SystemPath);
    if (!System) { return MCPCG_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }
    if (EmitterName.IsEmpty() || ModuleName.IsEmpty()) { return MCPCG_Error(TEXT("emitter and module are required")); }

    FVersionedNiagaraEmitter Instance;
    bool bFound = false;
    for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
    {
        if (H.GetName().ToString() == EmitterName) { Instance = H.GetInstance(); bFound = true; break; }
    }
    if (!bFound) { return MCPCG_Error(FString::Printf(TEXT("no emitter handle named '%s'"), *EmitterName)); }
    FVersionedNiagaraEmitterData* Data = Instance.GetEmitterData();
    if (!Data || !Data->SpawnScriptProps.Script) { return MCPCG_Error(TEXT("emitter has no versioned data / scripts")); }
    UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(Data->SpawnScriptProps.Script->GetLatestSource());
    UNiagaraGraph* Graph = Source ? Source->NodeGraph : nullptr;
    if (!Graph) { return MCPCG_Error(TEXT("emitter has no script graph")); }

    TArray<ENiagaraScriptUsage> Usages;
    if (ScriptUsage.IsEmpty() || ScriptUsage.ToLower() == TEXT("all"))
    {
        Usages = { ENiagaraScriptUsage::EmitterSpawnScript, ENiagaraScriptUsage::EmitterUpdateScript,
                   ENiagaraScriptUsage::ParticleSpawnScript, ENiagaraScriptUsage::ParticleUpdateScript };
    }
    else
    {
        ENiagaraScriptUsage U;
        if (!MCPCG_ParseUsage(ScriptUsage, U)) { return MCPCG_Error(TEXT("bad usage (particle_spawn|particle_update|emitter_spawn|emitter_update|all)")); }
        Usages.Add(U);
    }

    UNiagaraNodeFunctionCall* Module = nullptr;
    ENiagaraScriptUsage FoundUsage = ENiagaraScriptUsage::ParticleSpawnScript;
    int32 FoundIndex = INDEX_NONE;
    for (ENiagaraScriptUsage U : Usages)
    {
        UNiagaraNodeOutput* Out = Graph->FindOutputNode(U);
        if (!Out) { continue; }
        TArray<UNiagaraNodeFunctionCall*> Mods;
        MCPCG_OrderedModules(Out, Mods);
        for (int32 i = 0; i < Mods.Num(); ++i)
        {
            UNiagaraNodeFunctionCall* M = Mods[i];
            if (M && (M->GetFunctionName().Equals(ModuleName, ESearchCase::IgnoreCase) ||
                      (M->FunctionScript && M->FunctionScript->GetName().Equals(ModuleName, ESearchCase::IgnoreCase))))
            {
                Module = M; FoundUsage = U; FoundIndex = i; break;
            }
        }
        if (Module) { break; }
    }
    if (!Module) { return MCPCG_Error(FString::Printf(TEXT("no module named '%s' in emitter '%s'"), *ModuleName, *EmitterName)); }

    UNiagaraScript* Script = MCPCG_ScriptForUsage(Data, FoundUsage);
    TArray<TSharedPtr<FJsonValue>> Inputs = MCPCG_DescribeFunctionInputs(Module, Script, Instance, FoundUsage, InputFilter, 0);

    // Static switches live as pins on the function-call node itself.
    TArray<TSharedPtr<FJsonValue>> Switches;
    {
        FCompileConstantResolver Resolver(Instance, FoundUsage);
        TArray<UEdGraphPin*> SwitchPins;
        TSet<UEdGraphPin*> HiddenSwitch;
        FNiagaraStackGraphUtilities::GetStackFunctionStaticSwitchPins(*Module, SwitchPins, HiddenSwitch, Resolver);
        for (UEdGraphPin* P : SwitchPins)
        {
            if (!P) { continue; }
            TSharedRef<FJsonObject> S = MakeShared<FJsonObject>();
            S->SetStringField(TEXT("name"), P->PinName.ToString());
            S->SetStringField(TEXT("value"), P->DefaultValue);
            if (HiddenSwitch.Contains(P)) { S->SetBoolField(TEXT("hidden"), true); }
            Switches.Add(MakeShared<FJsonValueObject>(S));
        }
    }

    TSharedRef<FJsonObject> Root = MCPCG_Ok();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("module"), Module->GetFunctionName());
    Root->SetStringField(TEXT("usage"), MCPCG_UsageToString(FoundUsage));
    Root->SetNumberField(TEXT("module_index"), FoundIndex);
    Root->SetBoolField(TEXT("enabled"), Module->IsNodeEnabled());
    Root->SetNumberField(TEXT("input_count"), Inputs.Num());
    Root->SetArrayField(TEXT("inputs"), Inputs);
    if (Switches.Num() > 0) { Root->SetArrayField(TEXT("static_switches"), Switches); }
    return MCPCG_Serialize(Root);
#else
    return MCPCG_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// GetNiagaraParticlePositions
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetNiagaraParticlePositions(AActor* Actor, const FString& ComponentName,
    const FString& EmitterName, int32 MaxParticles, const FString& AttributesJson)
{
#if WITH_EDITOR
    if (!Actor) { return MCPCG_Error(TEXT("null actor")); }
    TArray<UNiagaraComponent*> Comps;
    Actor->GetComponents<UNiagaraComponent>(Comps);
    if (Comps.Num() == 0) { return MCPCG_Error(FString::Printf(TEXT("actor '%s' has no NiagaraComponent"), *Actor->GetName())); }
    const int32 Cap = MaxParticles > 0 ? MaxParticles : 2000;

    TArray<FString> Attrs;
    if (!AttributesJson.IsEmpty())
    {
        TArray<TSharedPtr<FJsonValue>> A;
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(AttributesJson);
        if (FJsonSerializer::Deserialize(R, A)) { for (const TSharedPtr<FJsonValue>& V : A) { FString S; if (V.IsValid() && V->TryGetString(S) && !S.IsEmpty()) { Attrs.Add(S); } } }
    }

    TArray<TSharedPtr<FJsonValue>> CompsJson;
    for (UNiagaraComponent* Comp : Comps)
    {
        if (!Comp) { continue; }
        if (!ComponentName.IsEmpty() && !Comp->GetName().Contains(ComponentName, ESearchCase::IgnoreCase)) { continue; }
        TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
        C->SetStringField(TEXT("component"), Comp->GetName());
        if (UNiagaraSystem* Sys = Comp->GetAsset()) { C->SetStringField(TEXT("system"), Sys->GetPathName()); }
        const FTransform CompXf = Comp->GetComponentTransform();
        const FVector Origin = CompXf.GetLocation();
        C->SetArrayField(TEXT("component_location"), MCPCG_Vec(Origin, 0.1));

        if (FNiagaraSystemInstanceControllerPtr MutCtl = Comp->GetSystemInstanceController())
        {
            MutCtl->WaitForConcurrentTickAndFinalize();
        }
        FNiagaraSystemInstanceControllerConstPtr Ctl = Comp->GetSystemInstanceController();
        FNiagaraSystemInstance* Inst = (Ctl.IsValid() && Ctl->IsValid()) ? Ctl->GetSystemInstance_Unsafe() : nullptr;
        if (!Inst)
        {
            C->SetStringField(TEXT("note"), TEXT("no live system instance (inactive / culled / not ticking — in the editor world the viewport must be realtime, or use PIE)"));
            CompsJson.Add(MakeShared<FJsonValueObject>(C));
            continue;
        }

        TArray<TSharedPtr<FJsonValue>> EmittersJson;
        for (const FNiagaraEmitterInstanceRef& ERef : Inst->GetEmitters())
        {
            FNiagaraEmitterInstance& E = ERef.Get();
            const FString EName = E.GetEmitterHandle().GetName().ToString();
            if (!EmitterName.IsEmpty() && !EName.Equals(EmitterName, ESearchCase::IgnoreCase)) { continue; }
            TSharedRef<FJsonObject> EO = MakeShared<FJsonObject>();
            EO->SetStringField(TEXT("emitter"), EName);
            EO->SetBoolField(TEXT("local_space"), E.IsLocalSpace());
            EO->SetNumberField(TEXT("particle_count"), E.GetNumParticles());

            if (E.GetSimTarget() != ENiagaraSimTarget::CPUSim)
            {
                EO->SetStringField(TEXT("note"), TEXT("GPU emitter: particle data lives on the GPU and can't be read back here (switch the emitter to CPU sim to measure it)"));
                EmittersJson.Add(MakeShared<FJsonValueObject>(EO));
                continue;
            }
            FNiagaraDataSet& DS = E.GetParticleData();
            FNiagaraDataBuffer* Buf = DS.GetCurrentData();
            const int32 Num = Buf ? (int32)Buf->GetNumInstances() : 0;
            FNiagaraDataSetReaderFloat<FNiagaraPosition> PosR = FNiagaraDataSetAccessor<FNiagaraPosition>(DS, FName(TEXT("Position"))).GetReader(DS);
            if (!PosR.IsValid())
            {
                EO->SetStringField(TEXT("note"), TEXT("emitter has no Position attribute"));
                EmittersJson.Add(MakeShared<FJsonValueObject>(EO));
                continue;
            }
            TArray<FNiagaraDataSetReaderFloat<float>> AttrReaders;
            TArray<FString> AttrNames;
            for (const FString& AN : Attrs)
            {
                FNiagaraDataSetReaderFloat<float> R = FNiagaraDataSetAccessor<float>(DS, FName(*AN)).GetReader(DS);
                if (R.IsValid()) { AttrReaders.Add(R); AttrNames.Add(AN); }
            }

            FBox Bounds(ForceInit);
            double MaxR = 0.0, MaxRXY = 0.0, SumR = 0.0;
            TArray<TSharedPtr<FJsonValue>> Pts;
            for (int32 i = 0; i < Num; ++i)
            {
                const FVector3f P3 = PosR.GetSafe(i, FNiagaraPosition(FVector3f::ZeroVector));
                FVector W(P3.X, P3.Y, P3.Z);
                if (E.IsLocalSpace()) { W = CompXf.TransformPosition(W); }
                Bounds += W;
                const FVector D = W - Origin;
                const double R = D.Size(), RXY = FVector2D(D.X, D.Y).Size();
                MaxR = FMath::Max(MaxR, R); MaxRXY = FMath::Max(MaxRXY, RXY); SumR += RXY;
                if (i < Cap)
                {
                    TArray<TSharedPtr<FJsonValue>> Row = MCPCG_Vec(W, 0.1);
                    for (const FNiagaraDataSetReaderFloat<float>& AR : AttrReaders) { Row.Add(MakeShared<FJsonValueNumber>(AR.GetSafe(i, 0.f))); }
                    Pts.Add(MakeShared<FJsonValueArray>(Row));
                }
            }
            EO->SetNumberField(TEXT("read_count"), Num);
            EO->SetBoolField(TEXT("truncated"), Num > Cap);
            TArray<TSharedPtr<FJsonValue>> Cols;
            Cols.Add(MakeShared<FJsonValueString>(TEXT("x")));
            Cols.Add(MakeShared<FJsonValueString>(TEXT("y")));
            Cols.Add(MakeShared<FJsonValueString>(TEXT("z")));
            for (const FString& AN : AttrNames) { Cols.Add(MakeShared<FJsonValueString>(AN)); }
            EO->SetArrayField(TEXT("columns"), Cols);
            EO->SetArrayField(TEXT("positions_world"), Pts);
            if (Num > 0)
            {
                EO->SetArrayField(TEXT("bounds_min"), MCPCG_Vec(Bounds.Min, 0.1));
                EO->SetArrayField(TEXT("bounds_max"), MCPCG_Vec(Bounds.Max, 0.1));
                EO->SetNumberField(TEXT("max_radius_from_component"), MaxR);
                EO->SetNumberField(TEXT("max_radius_xy_from_component"), MaxRXY);
                EO->SetNumberField(TEXT("mean_radius_xy_from_component"), SumR / Num);
            }
            if (AttrNames.Num() < Attrs.Num()) { EO->SetStringField(TEXT("attribute_note"), TEXT("some requested attributes are not float attributes of this emitter and were skipped")); }
            EmittersJson.Add(MakeShared<FJsonValueObject>(EO));
        }
        C->SetArrayField(TEXT("emitters"), EmittersJson);
        CompsJson.Add(MakeShared<FJsonValueObject>(C));
    }
    if (CompsJson.Num() == 0) { return MCPCG_Error(FString::Printf(TEXT("no NiagaraComponent on '%s' matched '%s'"), *Actor->GetName(), *ComponentName)); }

    TSharedRef<FJsonObject> Root = MCPCG_Ok();
    Root->SetStringField(TEXT("actor"), Actor->GetName());
    Root->SetStringField(TEXT("world"), Actor->GetWorld() ? Actor->GetWorld()->GetName() : FString());
    Root->SetArrayField(TEXT("components"), CompsJson);
    return MCPCG_Serialize(Root);
#else
    return MCPCG_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// RefreshMontageSegments
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::RefreshMontageSegments(UAnimMontage* Montage)
{
#if WITH_EDITOR
    if (!Montage) { return MCPCG_Error(TEXT("null montage")); }
    const float PrevLength = Montage->GetPlayLength();
    Montage->Modify();

    TArray<TSharedPtr<FJsonValue>> SegsJson;
    int32 Changed = 0;
    for (int32 s = 0; s < Montage->SlotAnimTracks.Num(); ++s)
    {
        FSlotAnimationTrack& Slot = Montage->SlotAnimTracks[s];
        TArray<FAnimSegment>& Segs = Slot.AnimTrack.AnimSegments;
        float Cursor = Segs.Num() > 0 ? Segs[0].StartPos : 0.f;
        for (int32 i = 0; i < Segs.Num(); ++i)
        {
            FAnimSegment& Seg = Segs[i];
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("slot"), Slot.SlotName.ToString());
            J->SetNumberField(TEXT("index"), i);
            const UAnimSequenceBase* Ref = Seg.GetAnimReference();
            J->SetStringField(TEXT("anim"), Ref ? Ref->GetPathName() : FString());
            J->SetNumberField(TEXT("prev_anim_start"), Seg.AnimStartTime);
            J->SetNumberField(TEXT("prev_anim_end"), Seg.AnimEndTime);
            J->SetNumberField(TEXT("prev_start_pos"), Seg.StartPos);
            if (Ref)
            {
                const float SeqLen = Ref->GetPlayLength();
                if (!FMath::IsNearlyEqual(Seg.AnimEndTime, SeqLen, 1e-4f) || Seg.AnimStartTime > SeqLen)
                {
                    if (Seg.AnimStartTime > SeqLen) { Seg.AnimStartTime = 0.f; }
                    Seg.AnimEndTime = SeqLen;
                    ++Changed;
                }
                J->SetNumberField(TEXT("sequence_length"), SeqLen);
            }
            if (i > 0 && !FMath::IsNearlyEqual(Seg.StartPos, Cursor, 1e-4f)) { Seg.StartPos = Cursor; ++Changed; }
            Cursor = Seg.StartPos + Seg.GetLength();
            J->SetNumberField(TEXT("anim_start"), Seg.AnimStartTime);
            J->SetNumberField(TEXT("anim_end"), Seg.AnimEndTime);
            J->SetNumberField(TEXT("start_pos"), Seg.StartPos);
            J->SetNumberField(TEXT("segment_length"), Seg.GetLength());
            SegsJson.Add(MakeShared<FJsonValueObject>(J));
        }
    }

    // Same steps the montage editor runs on its next tick: recompute + store the composite length, re-link
    // sections/notifies to the new timing, and refresh caches.
    const float NewLength = Montage->CalculateSequenceLength();
    Montage->SetCompositeLength(NewLength);
    Montage->UpdateLinkableElements();
    Montage->PostEditChange();
    Montage->MarkPackageDirty();

    // Sections past the new end would be unreachable — report them (not moved: section placement is a design choice).
    TArray<TSharedPtr<FJsonValue>> Sections;
    for (int32 i = 0; i < Montage->CompositeSections.Num(); ++i)
    {
        const FCompositeSection& Sec = Montage->CompositeSections[i];
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), Sec.SectionName.ToString());
        J->SetNumberField(TEXT("time"), Sec.GetTime());
        if (Sec.GetTime() > NewLength + 1e-4f) { J->SetBoolField(TEXT("past_end"), true); }
        Sections.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPCG_Ok();
    Root->SetStringField(TEXT("montage"), Montage->GetPathName());
    Root->SetNumberField(TEXT("prev_length"), PrevLength);
    Root->SetNumberField(TEXT("new_length"), Montage->GetPlayLength());
    Root->SetNumberField(TEXT("changed"), Changed);
    Root->SetArrayField(TEXT("segments"), SegsJson);
    Root->SetArrayField(TEXT("sections"), Sections);
    return MCPCG_Serialize(Root);
#else
    return MCPCG_Error(TEXT("editor-only"));
#endif
}
