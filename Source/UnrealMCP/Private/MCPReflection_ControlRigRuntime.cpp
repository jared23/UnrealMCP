// UnrealMCP reflection helpers — CONTROL RIG RUNTIME (eval-based) spec features.
//
// WHAT / WHY C++: stock Python can author + read a Control Rig blueprint's authoring hierarchy and VM
// graph, but it CANNOT instantiate the compiled rig, run its Forwards Solve, and read the SOLVED pose /
// downstream impact / timing. That requires the ControlRig RUNTIME class UControlRig (Initialize / Execute /
// GetHierarchy) which is only reachable in C++. These handlers instantiate a TRANSIENT UControlRig from the
// blueprint's generated class, run the solve on that throwaway instance, and read the resulting hierarchy.
// The SOURCE ASSET IS NEVER TOUCHED — every write (the control offset in analyze_rig_control_impact,
// the bone drive in simulate_rig) happens on the transient instance only, so there is NOTHING to undo and
// NO ledger op is emitted (reads + transient-only writes).
//
// ---------------------------------------------------------------------------------------------------------
// THE INSTANTIATE + INIT + EXECUTE SEQUENCE (the riskiest part — cite the engine basis).
// Copied from the engine's OWN standalone-instance path FAnimNode_ControlRig::UpdateControlRig /
// SetControlRig (AnimNode_ControlRig.cpp:363-474) and UControlRigComponent (ControlRigComponent.cpp:141,
// 406, 1351). The exact steps:
//   1. UClass* GenClass = <blueprint>->GeneratedClass  (a UControlRigBlueprintGeneratedClass, subclass of
//      UControlRig). The engine's FControlRigAssetStrongReference::CreateInstance does exactly
//      NewObject<UControlRig>(Outer, BlueprintRigClass, ...) (ControlRigAssetReference.cpp:366-371).
//   2. UControlRig* Rig = NewObject<UControlRig>(GetTransientPackage(), GenClass, NAME_None, RF_Transient)
//      — under an FGCScopeGuard, matching the engine ("make sure GC isn't running when we create a Rig").
//   3. Rig->Initialize(true)  — InitializeFromCDO + InstantiateVMFromCDO + RequestConstruction
//      (ControlRig.cpp:353 / RigVMHost.cpp:302).
//   4. Rig->RequestInit()     — sets bRequiresInitExecution so the first Execute runs InitializeVM.
//   5. Rig->Execute(FRigUnit_PrepareForExecution::EventName)  — "Construction" (builds controls/nulls; the
//      AnimNode calls this explicitly, AnimNode_ControlRig.cpp:382).
//   6. Rig->Execute(FRigUnit_BeginExecution::EventName)       — "Forwards Solve" (the actual solve;
//      event name confirmed at RigUnit_BeginExecution.h:29 = FLazyName("Forwards Solve")).
//   Execute() itself runs InitializeVM on the first call when bRequiresInitExecution is set
//   (ControlRig.cpp:967-975) and returns false (never crashes) if the rig failed to compile
//   (InitializeVM bails on an external-variable-count mismatch, RigVMHost.cpp:334-337).
//
// CRASH-SAFETY: every handler null-guards (a) the loaded asset, (b) the resolved UClass and that it
// IsChildOf(UControlRig), (c) the NewObject instance, (d) GetHierarchy(). Before any Execute we gate on
// CanExecute() (cheap, always-safe — RigVMHost.cpp:297) and SupportsEvent() (VM entry present after
// Initialize instantiates the VM from the CDO). The transient rig is rooted with a TStrongObjectPtr for the
// handler's lifetime so GC cannot collect it mid-solve. A rig that refuses to init returns a clean
// {"status":"error"} JSON. NOTE FOR THE COORDINATOR: a genuinely CORRUPT compiled VM could still fault
// inside Execute() (no try/catch in UE) — this is the one live-verify risk point; verify get_rig_pose first
// on a known-good rig before trusting the others.
//
// Member DEFINITIONS for UMCPReflectionLibrary; matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator (see FINAL REPORT). Anon-namespace helpers are prefixed
// `MCPCRRt_` so they stay unique across the unity build.
//
// BUILD.CS: this is the FIRST use of the ControlRig RUNTIME module in the plugin. `ControlRig` must be added
// to PublicDependencyModuleNames (RigVM is already present; ControlRig is NOT). NO ControlRigDeveloper /
// ControlRigEditor needed — we resolve the generated class through the generic UBlueprint::GeneratedClass,
// never through UControlRigBlueprint. See FINAL REPORT.
// ---------------------------------------------------------------------------------------------------------

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "UObject/SoftObjectPath.h"
#include "UObject/UObjectGlobals.h"          // GetTransientPackage / NewObject
#include "UObject/Package.h"
#include "UObject/GarbageCollection.h"       // FGCScopeGuard
#include "UObject/StrongObjectPtr.h"         // TStrongObjectPtr (root the transient rig)
#include "Engine/Blueprint.h"                // UBlueprint::GeneratedClass
#include "HAL/PlatformTime.h"                // FPlatformTime (profile)

// --- ControlRig RUNTIME (needs `ControlRig` in Build.cs) --------------------------------------------------
#include "ControlRig.h"                                   // UControlRig: Initialize/RequestInit/Execute/GetHierarchy/CanExecute/SupportsEvent
#include "Rigs/RigHierarchy.h"                            // URigHierarchy: GetAllKeys/GetGlobalTransform/GetLocalTransform/Find
#include "Rigs/RigHierarchyElements.h"                    // FRigControlElement (control type)
#include "Rigs/RigHierarchyDefines.h"                     // ERigElementType / FRigElementKey / ERigControlType
#include "Units/Execution/RigUnit_PrepareForExecution.h"  // "Construction" event name
#include "Units/Execution/RigUnit_BeginExecution.h"       // "Forwards Solve" event name
#include "Units/Execution/RigUnit_InverseExecution.h"     // "Backwards Solve" event name (simulate_rig)
#include "RigVMCore/RigVMExternalVariable.h"              // FRigVMExternalVariable (rig input variables)

// --- Anim sampling (simulate_rig) — Engine module, already a dependency ----------------------------------
#if WITH_EDITOR
#include "Animation/AnimSequence.h"
#include "Animation/AnimData/IAnimationDataModel.h"       // EvaluateBoneTrackTransform / GetBoneTrackNames (editor-only)
#endif

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------------
    FString MCPCRRt_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPCRRt_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPCRRt_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPCRRt_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

    double MCPCRRt_R3(double V) { return FMath::RoundToDouble(V * 1000.0) / 1000.0; }

    TSharedPtr<FJsonValue> MCPCRRt_Vec3(const FVector& V)
    {
        TArray<TSharedPtr<FJsonValue>> A;
        A.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(V.X)));
        A.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(V.Y)));
        A.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(V.Z)));
        return MakeShared<FJsonValueArray>(A);
    }

    // {loc:[x,y,z], rot_quat:[x,y,z,w], rot_euler:[roll,pitch,yaw], scale:[x,y,z]}
    TSharedRef<FJsonObject> MCPCRRt_XformJson(const FTransform& T)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetField(TEXT("loc"), MCPCRRt_Vec3(T.GetLocation()));
        const FQuat Q = T.GetRotation();
        TArray<TSharedPtr<FJsonValue>> Qa;
        Qa.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(Q.X)));
        Qa.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(Q.Y)));
        Qa.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(Q.Z)));
        Qa.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(Q.W)));
        J->SetField(TEXT("rot_quat"), MakeShared<FJsonValueArray>(Qa));
        const FRotator R = Q.Rotator();
        TArray<TSharedPtr<FJsonValue>> Ea;
        Ea.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(R.Roll)));
        Ea.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(R.Pitch)));
        Ea.Add(MakeShared<FJsonValueNumber>(MCPCRRt_R3(R.Yaw)));
        J->SetField(TEXT("rot_euler"), MakeShared<FJsonValueArray>(Ea));
        J->SetField(TEXT("scale"), MCPCRRt_Vec3(T.GetScale3D()));
        return J;
    }

    // Clean lowercase name for an element type.
    FString MCPCRRt_ElemTypeName(ERigElementType Type)
    {
        switch (Type)
        {
        case ERigElementType::Bone:      return TEXT("bone");
        case ERigElementType::Null:      return TEXT("null");
        case ERigElementType::Control:   return TEXT("control");
        case ERigElementType::Curve:     return TEXT("curve");
        case ERigElementType::Physics:   return TEXT("physics");
        case ERigElementType::Reference: return TEXT("reference");
        case ERigElementType::Connector: return TEXT("connector");
        case ERigElementType::Socket:    return TEXT("socket");
        default:                         return TEXT("unknown");
        }
    }

    bool MCPCRRt_HasTransform(ERigElementType Type)
    {
        return Type == ERigElementType::Bone || Type == ERigElementType::Null ||
               Type == ERigElementType::Control || Type == ERigElementType::Reference ||
               Type == ERigElementType::Socket;
    }

    // Case-insensitive comma-separated substring filter. Empty tokens => match all.
    TArray<FString> MCPCRRt_ParseTokens(const FString& Csv)
    {
        TArray<FString> Out, Raw;
        Csv.ToLower().ParseIntoArray(Raw, TEXT(","), true);
        for (FString& T : Raw) { Out.Add(T.TrimStartAndEnd()); }
        return Out;
    }
    bool MCPCRRt_Matches(const FString& NameLower, const TArray<FString>& Tokens)
    {
        if (Tokens.Num() == 0) { return true; }
        for (const FString& Tok : Tokens) { if (!Tok.IsEmpty() && NameLower.Contains(Tok)) { return true; } }
        return false;
    }

    // Resolve a control-rig blueprint path to its generated UControlRig subclass. Accepts a blueprint asset
    // path, a generated-class path (…_C), or a UClass directly. Never touches ControlRigDeveloper types.
    UClass* MCPCRRt_ResolveRigClass(const FString& Path, FString& OutError)
    {
        if (Path.IsEmpty()) { OutError = TEXT("empty control_rig_path"); return nullptr; }
        UObject* Asset = FSoftObjectPath(Path).TryLoad();
        if (!Asset) { OutError = FString::Printf(TEXT("could not load asset '%s'"), *Path); return nullptr; }

        UClass* GenClass = nullptr;
        if (UClass* AsClass = Cast<UClass>(Asset))
        {
            GenClass = AsClass;
        }
        else if (UBlueprint* BP = Cast<UBlueprint>(Asset))
        {
            GenClass = BP->GeneratedClass;
            if (!GenClass)
            {
                OutError = FString::Printf(TEXT("blueprint '%s' has no GeneratedClass (needs a compile/save)"), *Path);
                return nullptr;
            }
        }
        else
        {
            OutError = FString::Printf(TEXT("asset '%s' is a %s, not a control-rig blueprint or class"),
                *Path, *Asset->GetClass()->GetName());
            return nullptr;
        }

        if (!GenClass->IsChildOf(UControlRig::StaticClass()))
        {
            OutError = FString::Printf(TEXT("class '%s' is not a UControlRig subclass"), *GenClass->GetName());
            return nullptr;
        }
        // Reject the abstract base itself.
        if (GenClass == UControlRig::StaticClass() || GenClass->HasAnyClassFlags(CLASS_Abstract))
        {
            OutError = TEXT("resolved class is abstract / not a concrete rig");
            return nullptr;
        }
        return GenClass;
    }

    // Instantiate + Initialize a fresh transient rig from a generated class (steps 2-4 above). Caller must
    // keep the returned rig rooted (TStrongObjectPtr) while using it. Returns nullptr + OutError on failure.
    UControlRig* MCPCRRt_NewRig(UClass* GenClass, FString& OutError)
    {
        if (!GenClass) { OutError = TEXT("null generated class"); return nullptr; }
        UControlRig* Rig = nullptr;
        {
            FGCScopeGuard GCGuard; // match the engine: never create the rig while GC is running
            Rig = NewObject<UControlRig>(GetTransientPackage(), GenClass, NAME_None, RF_Transient);
        }
        if (!Rig) { OutError = TEXT("NewObject<UControlRig> returned null"); return nullptr; }
        Rig->Initialize(true);   // InitializeFromCDO + InstantiateVMFromCDO + RequestConstruction
        Rig->RequestInit();      // first Execute will run InitializeVM
        if (!Rig->GetHierarchy())
        {
            OutError = TEXT("rig has no hierarchy after Initialize");
            return nullptr;
        }
        return Rig;
    }

    // Run Construction (if present) then Forwards Solve on an initialized rig. Returns false (never crashes)
    // if the rig cannot execute or has no forward-solve event.
    bool MCPCRRt_ForwardSolve(UControlRig* Rig, FString& OutError)
    {
        if (!Rig || !Rig->CanExecute()) { OutError = TEXT("rig cannot execute (CanExecute()==false)"); return false; }
        if (!Rig->SupportsEvent(FRigUnit_BeginExecution::EventName))
        {
            OutError = TEXT("rig has no 'Forwards Solve' event");
            return false;
        }
        if (Rig->SupportsEvent(FRigUnit_PrepareForExecution::EventName))
        {
            Rig->Execute(FRigUnit_PrepareForExecution::EventName); // Construction (builds controls)
        }
        const bool bOk = Rig->Execute(FRigUnit_BeginExecution::EventName);
        if (!bOk) { OutError = TEXT("Forwards Solve Execute() returned false (rig likely failed to compile)"); }
        return bOk;
    }

    // Advance time + run a single forward solve (used by profile / motion / simulate loops).
    bool MCPCRRt_SolveFrame(UControlRig* Rig, float DeltaTime, float AbsoluteTime)
    {
        if (!Rig || !Rig->CanExecute()) { return false; }
        Rig->SetDeltaTime(DeltaTime);
        Rig->SetAbsoluteTime(AbsoluteTime);
        return Rig->Execute(FRigUnit_BeginExecution::EventName);
    }

    // ---- start_rig_motion_capture / get_rig_motion_report stash -----------------------------------------
    // Holds ONLY recorded data (no UObject refs) so it is GC-safe to keep in a static map across MCP calls.
    struct FMCPCRRt_MotionBuffer
    {
        TArray<FName> BoneNames;
        TArray<TArray<FVector>> PerBonePos; // [boneIndex][frame] global location
        int32 FrameCount = 0;
        float DeltaTime = 0.f;
        double CapturedAtSeconds = 0.0;
    };
    static TMap<FString, FMCPCRRt_MotionBuffer> GMCPCRRt_MotionBuffers; // keyed by control_rig_path
}

// =========================================================================================================
// 1) get_rig_pose — instantiate, forward-solve, return each element's global + local transform.
//    threshold_cm: flags elements whose solved global location moved > threshold from the ref (initial) pose.
//    bones_csv: optional case-insensitive substring filter over element names (empty => all transform elems).
// =========================================================================================================
FString UMCPReflectionLibrary::GetRigPoseJson(const FString& ControlRigPath, const FString& BonesCsv, float ThresholdCm)
{
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    UControlRig* Rig = MCPCRRt_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig); // root for the handler's lifetime

    if (!MCPCRRt_ForwardSolve(Rig, Err)) { return MCPCRRt_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRRt_Error(TEXT("null hierarchy after solve")); }

    const TArray<FString> Tokens = MCPCRRt_ParseTokens(BonesCsv);
    const TArray<FRigElementKey> Keys = H->GetAllKeys(false, ERigElementType::All);

    TArray<TSharedPtr<FJsonValue>> Elems;
    int32 MovedCount = 0;
    for (const FRigElementKey& Key : Keys)
    {
        if (!MCPCRRt_HasTransform(Key.Type)) { continue; }
        const FString Name = Key.Name.ToString();
        if (!MCPCRRt_Matches(Name.ToLower(), Tokens)) { continue; }

        const FTransform GCur = H->GetGlobalTransform(Key, false);
        const FTransform LCur = H->GetLocalTransform(Key, false);
        const FTransform GInit = H->GetGlobalTransform(Key, true);
        const double DeltaCm = FVector::Dist(GCur.GetLocation(), GInit.GetLocation());
        const bool bMoved = DeltaCm > (double)ThresholdCm;
        if (bMoved) { ++MovedCount; }

        TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
        E->SetStringField(TEXT("name"), Name);
        E->SetStringField(TEXT("type"), MCPCRRt_ElemTypeName(Key.Type));
        E->SetObjectField(TEXT("global"), MCPCRRt_XformJson(GCur));
        E->SetObjectField(TEXT("local"), MCPCRRt_XformJson(LCur));
        E->SetNumberField(TEXT("delta_cm"), MCPCRRt_R3(DeltaCm));
        E->SetBoolField(TEXT("moved"), bMoved);
        Elems.Add(MakeShared<FJsonValueObject>(E));
    }

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("threshold_cm"), ThresholdCm);
    Root->SetNumberField(TEXT("element_count"), Elems.Num());
    Root->SetNumberField(TEXT("moved_count"), MovedCount);
    Root->SetArrayField(TEXT("elements"), Elems);
    return MCPCRRt_SerializeJson(Root);
}

// =========================================================================================================
// 2) analyze_rig_io — enumerate INPUTS (controls + public input variables) and OUTPUTS (bones the forward
//    solve writes), by diffing bone globals before/after solve. frames/delta_time: run the solve up to
//    `frames` times advancing time so a time-dependent rig's outputs are captured (max delta per bone).
// =========================================================================================================
FString UMCPReflectionLibrary::AnalyzeRigIoJson(const FString& ControlRigPath, int32 Frames, float DeltaTime)
{
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    UControlRig* Rig = MCPCRRt_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRRt_Error(TEXT("null hierarchy")); }

    // Snapshot bone initial (== current pre-solve) global locations.
    const TArray<FRigElementKey> BoneKeys = H->GetAllKeys(false, ERigElementType::Bone);
    TMap<FRigElementKey, FVector> PreLoc;
    for (const FRigElementKey& K : BoneKeys) { PreLoc.Add(K, H->GetGlobalTransform(K, true).GetLocation()); }

    // Build the input list BEFORE solving (construction may add controls; do it after first construction).
    if (!MCPCRRt_ForwardSolve(Rig, Err)) { return MCPCRRt_Error(Err); }

    // Extra timed frames (>=1 total already run above).
    const int32 NumFrames = FMath::Clamp(Frames, 1, 2000);
    const float Dt = (DeltaTime <= 0.f) ? (1.f / 30.f) : DeltaTime;
    TMap<FRigElementKey, double> MaxDelta;
    for (const FRigElementKey& K : BoneKeys)
    {
        MaxDelta.Add(K, FVector::Dist(H->GetGlobalTransform(K, false).GetLocation(), PreLoc[K]));
    }
    for (int32 f = 1; f < NumFrames; ++f)
    {
        if (!MCPCRRt_SolveFrame(Rig, Dt, Dt * f)) { break; }
        for (const FRigElementKey& K : BoneKeys)
        {
            const double D = FVector::Dist(H->GetGlobalTransform(K, false).GetLocation(), PreLoc[K]);
            double& M = MaxDelta.FindChecked(K);
            if (D > M) { M = D; }
        }
    }

    // INPUTS: controls.
    TArray<TSharedPtr<FJsonValue>> Controls;
    for (const FRigElementKey& K : H->GetAllKeys(false, ERigElementType::Control))
    {
        TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
        C->SetStringField(TEXT("name"), K.Name.ToString());
        FString CtrlType = TEXT("unknown");
        if (FRigBaseElement* Elem = H->Find(K))
        {
            if (FRigControlElement* Ctrl = Cast<FRigControlElement>(Elem))
            {
                if (const UEnum* E = StaticEnum<ERigControlType>())
                {
                    CtrlType = E->GetNameStringByValue((int64)Ctrl->Settings.ControlType);
                }
            }
        }
        C->SetStringField(TEXT("control_type"), CtrlType);
        Controls.Add(MakeShared<FJsonValueObject>(C));
    }

    // INPUTS: public input variables (the rig's exposed variables).
    TArray<TSharedPtr<FJsonValue>> Vars;
    for (const FRigVMExternalVariable& V : Rig->GetPublicVariables())
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), V.GetName().ToString());
        J->SetStringField(TEXT("cpp_type"), V.GetBaseCPPType().ToString());
        J->SetBoolField(TEXT("is_array"), V.IsArray());
        J->SetBoolField(TEXT("is_read_only"), V.IsReadOnly());
        Vars.Add(MakeShared<FJsonValueObject>(J));
    }

    // OUTPUTS: bones the solve moved.
    TArray<TSharedPtr<FJsonValue>> Outputs;
    for (const FRigElementKey& K : BoneKeys)
    {
        const double D = MaxDelta[K];
        if (D <= 1.0e-3) { continue; } // unmoved => not driven by the solve
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), K.Name.ToString());
        J->SetNumberField(TEXT("max_delta_cm"), MCPCRRt_R3(D));
        Outputs.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("frames"), NumFrames);
    Root->SetNumberField(TEXT("bone_count"), BoneKeys.Num());
    TSharedRef<FJsonObject> Inputs = MakeShared<FJsonObject>();
    Inputs->SetNumberField(TEXT("control_count"), Controls.Num());
    Inputs->SetArrayField(TEXT("controls"), Controls);
    Inputs->SetNumberField(TEXT("variable_count"), Vars.Num());
    Inputs->SetArrayField(TEXT("variables"), Vars);
    Root->SetObjectField(TEXT("inputs"), Inputs);
    Root->SetNumberField(TEXT("output_bone_count"), Outputs.Num());
    Root->SetArrayField(TEXT("output_bones"), Outputs);
    return MCPCRRt_SerializeJson(Root);
}

// =========================================================================================================
// 3) analyze_rig_control_impact — for each named control (or ALL controls if controls_csv empty): offset the
//    control's LOCAL translation by offset_cm along +X, re-solve on a FRESH instance, and report which bones
//    moved and by how much (downstream impact map). Fresh instance per control => zero cross-contamination.
//    TRANSIENT-ONLY: the source asset is never modified, so no undo is needed.
// =========================================================================================================
FString UMCPReflectionLibrary::AnalyzeRigControlImpactJson(const FString& ControlRigPath, const FString& ControlsCsv, float OffsetCm)
{
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    const float Offset = (FMath::Abs(OffsetCm) < KINDA_SMALL_NUMBER) ? 10.f : OffsetCm;

    // Base instance: solve once to get the baseline bone globals + the control set.
    UControlRig* Base = MCPCRRt_NewRig(GenClass, Err);
    if (!Base) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> BaseGuard(Base);
    if (!MCPCRRt_ForwardSolve(Base, Err)) { return MCPCRRt_Error(Err); }
    URigHierarchy* BH = Base->GetHierarchy();
    if (!BH) { return MCPCRRt_Error(TEXT("null hierarchy")); }

    const TArray<FRigElementKey> BoneKeys = BH->GetAllKeys(false, ERigElementType::Bone);
    TMap<FRigElementKey, FVector> BaseLoc;
    for (const FRigElementKey& K : BoneKeys) { BaseLoc.Add(K, BH->GetGlobalTransform(K, false).GetLocation()); }

    // Which controls to probe.
    const TArray<FString> Wanted = MCPCRRt_ParseTokens(ControlsCsv);
    TArray<FRigElementKey> ControlKeys;
    for (const FRigElementKey& K : BH->GetAllKeys(false, ERigElementType::Control))
    {
        if (MCPCRRt_Matches(K.Name.ToString().ToLower(), Wanted)) { ControlKeys.Add(K); }
    }

    TArray<TSharedPtr<FJsonValue>> Results;
    for (const FRigElementKey& CtrlKey : ControlKeys)
    {
        FString FErr;
        UControlRig* Rig = MCPCRRt_NewRig(GenClass, FErr);
        if (!Rig) { continue; }
        TStrongObjectPtr<UControlRig> Guard(Rig);
        if (!MCPCRRt_ForwardSolve(Rig, FErr)) { continue; }
        URigHierarchy* H = Rig->GetHierarchy();
        if (!H) { continue; }

        // Only spatial controls carry a meaningful cm offset.
        bool bSpatial = false;
        if (FRigBaseElement* Elem = H->Find(CtrlKey))
        {
            if (FRigControlElement* Ctrl = Cast<FRigControlElement>(Elem))
            {
                const ERigControlType T = Ctrl->Settings.ControlType;
                bSpatial = (T == ERigControlType::Position || T == ERigControlType::Transform ||
                            T == ERigControlType::EulerTransform || T == ERigControlType::TransformNoScale);
            }
        }

        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("control"), CtrlKey.Name.ToString());
        if (!bSpatial)
        {
            R->SetBoolField(TEXT("spatial"), false);
            R->SetStringField(TEXT("note"), TEXT("non-spatial control; cm offset not applicable — skipped"));
            Results.Add(MakeShared<FJsonValueObject>(R));
            continue;
        }

        // Offset the control's local translation by +X offset_cm and re-solve (transient instance only).
        FTransform L = Rig->GetControlLocalTransform(CtrlKey.Name);
        L.AddToTranslation(FVector((double)Offset, 0.0, 0.0));
        Rig->SetControlLocalTransform(CtrlKey.Name, L);
        Rig->Execute(FRigUnit_BeginExecution::EventName); // re-solve with the offset control

        TArray<TSharedPtr<FJsonValue>> Affected;
        double MaxD = 0.0, SumD = 0.0;
        for (const FRigElementKey& BK : BoneKeys)
        {
            const double D = FVector::Dist(H->GetGlobalTransform(BK, false).GetLocation(), BaseLoc[BK]);
            if (D <= 1.0e-3) { continue; }
            MaxD = FMath::Max(MaxD, D);
            SumD += D;
            TSharedRef<FJsonObject> B = MakeShared<FJsonObject>();
            B->SetStringField(TEXT("bone"), BK.Name.ToString());
            B->SetNumberField(TEXT("delta_cm"), MCPCRRt_R3(D));
            Affected.Add(MakeShared<FJsonValueObject>(B));
        }
        // Sort affected bones by displacement, descending.
        Affected.Sort([](const TSharedPtr<FJsonValue>& A, const TSharedPtr<FJsonValue>& B)
        {
            return A->AsObject()->GetNumberField(TEXT("delta_cm")) > B->AsObject()->GetNumberField(TEXT("delta_cm"));
        });

        R->SetBoolField(TEXT("spatial"), true);
        R->SetNumberField(TEXT("offset_cm"), Offset);
        R->SetNumberField(TEXT("affected_bone_count"), Affected.Num());
        R->SetNumberField(TEXT("max_delta_cm"), MCPCRRt_R3(MaxD));
        R->SetNumberField(TEXT("total_delta_cm"), MCPCRRt_R3(SumD));
        R->SetArrayField(TEXT("affected_bones"), Affected);
        Results.Add(MakeShared<FJsonValueObject>(R));
    }

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("offset_cm"), Offset);
    Root->SetNumberField(TEXT("control_count"), Results.Num());
    Root->SetArrayField(TEXT("controls"), Results);
    return MCPCRRt_SerializeJson(Root);
}

// =========================================================================================================
// 4) profile_rig — execute the forward solve `frames` times, time it with FPlatformTime, report total/avg
//    ms + est fps. top_nodes: per-node timing is NOT exposed via a public UControlRig/RigVM API from a
//    plugin, so it is omitted with a note (best-effort not reachable).
// =========================================================================================================
FString UMCPReflectionLibrary::ProfileRigJson(const FString& ControlRigPath, int32 Frames, float DeltaTime, int32 TopNodes)
{
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    UControlRig* Rig = MCPCRRt_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);

    // Warm up (construction + first solve, incl. VM init) so init cost is excluded from the timing.
    if (!MCPCRRt_ForwardSolve(Rig, Err)) { return MCPCRRt_Error(Err); }

    const int32 NumFrames = FMath::Clamp(Frames, 1, 100000);
    const float Dt = (DeltaTime <= 0.f) ? (1.f / 30.f) : DeltaTime;

    const double Start = FPlatformTime::Seconds();
    int32 Ran = 0;
    for (int32 f = 0; f < NumFrames; ++f)
    {
        if (!MCPCRRt_SolveFrame(Rig, Dt, Dt * (f + 1))) { break; }
        ++Ran;
    }
    const double Elapsed = FPlatformTime::Seconds() - Start;

    const double TotalMs = Elapsed * 1000.0;
    const double AvgMs = (Ran > 0) ? (TotalMs / Ran) : 0.0;

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("frames_requested"), NumFrames);
    Root->SetNumberField(TEXT("frames_run"), Ran);
    Root->SetNumberField(TEXT("total_ms"), MCPCRRt_R3(TotalMs));
    Root->SetNumberField(TEXT("avg_ms_per_frame"), MCPCRRt_R3(AvgMs));
    Root->SetNumberField(TEXT("est_fps"), AvgMs > 0.0 ? MCPCRRt_R3(1000.0 / AvgMs) : 0.0);
    if (TopNodes > 0)
    {
        Root->SetStringField(TEXT("top_nodes_note"),
            TEXT("per-node timing is not exposed via a public UControlRig/RigVM API to a plugin; omitted"));
    }
    return MCPCRRt_SerializeJson(Root);
}

// =========================================================================================================
// 5) simulate_rig — drive the rig's bone hierarchy from an AnimSequence's per-bone LOCAL pose over `frames`,
//    solve each frame, return sampled output bone globals (compact per-bone start/end/mean summary).
//    HONEST CAVEAT: a standard forward-solve rig is CONTROL-driven and overwrites bones, so the bone drive
//    only "sticks" when the rig supports a Backwards Solve (control values are inferred from the driven
//    bones, then the forward solve re-applies them — the sequencer bake round-trip). We run Backwards Solve
//    when SupportsBackwardsSolve(), then Forwards Solve; otherwise Forwards Solve only (documented per-frame).
//    Editor-only (anim DataModel sampling is WITH_EDITOR).
// =========================================================================================================
FString UMCPReflectionLibrary::SimulateRigJson(const FString& ControlRigPath, const FString& AnimSequencePath, int32 Frames, float DeltaTime)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    UObject* AnimObj = FSoftObjectPath(AnimSequencePath).TryLoad();
    UAnimSequence* Anim = Cast<UAnimSequence>(AnimObj);
    if (!Anim) { return MCPCRRt_Error(FString::Printf(TEXT("could not load AnimSequence '%s'"), *AnimSequencePath)); }
    IAnimationDataModel* Model = Anim->GetDataModel();
    if (!Model) { return MCPCRRt_Error(TEXT("AnimSequence has no data model (needs editor build/loaded)")); }

    UControlRig* Rig = MCPCRRt_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRRt_ForwardSolve(Rig, Err)) { return MCPCRRt_Error(Err); }
    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRRt_Error(TEXT("null hierarchy")); }

    const bool bBackwards = Rig->SupportsBackwardsSolve() &&
                            Rig->SupportsEvent(FRigUnit_InverseExecution::EventName);

    // Match anim bone tracks to rig bones (case-sensitive FName match).
    TArray<FName> TrackNames;
    Model->GetBoneTrackNames(TrackNames);
    TSet<FName> TrackSet(TrackNames);
    TArray<FRigElementKey> DrivenBones;
    for (const FRigElementKey& K : H->GetAllKeys(false, ERigElementType::Bone))
    {
        if (TrackSet.Contains(K.Name)) { DrivenBones.Add(K); }
    }

    const int32 NumSrcFrames = FMath::Max(Model->GetNumberOfFrames(), 1);
    const int32 NumFrames = FMath::Clamp(Frames, 1, 2000);
    const float Dt = (DeltaTime <= 0.f) ? (1.f / 30.f) : DeltaTime;

    // Per driven-or-output bone: record start/end/mean global position across the simulated frames.
    const TArray<FRigElementKey> AllBones = H->GetAllKeys(false, ERigElementType::Bone);
    TMap<FRigElementKey, FVector> StartLoc, LastLoc, SumLoc;
    for (const FRigElementKey& K : AllBones) { SumLoc.Add(K, FVector::ZeroVector); }

    for (int32 f = 0; f < NumFrames; ++f)
    {
        // Map sim frame -> source frame (clamp; NumFrames may differ from source length).
        const double Alpha = (NumFrames > 1) ? ((double)f / (double)(NumFrames - 1)) : 0.0;
        const int32 SrcFrameIdx = FMath::Clamp((int32)FMath::RoundToInt(Alpha * (NumSrcFrames - 1)), 0, NumSrcFrames - 1);
        const FFrameTime SrcTime = FFrameTime(FFrameNumber(SrcFrameIdx));

        // Drive rig bones from the anim's LOCAL pose.
        for (const FRigElementKey& K : DrivenBones)
        {
            const FTransform Local = Model->EvaluateBoneTrackTransform(K.Name, SrcTime, EAnimInterpolationType::Linear);
            H->SetLocalTransform(K, Local, false /*bInitial*/, true /*bAffectChildren*/, false, false);
        }

        Rig->SetDeltaTime(Dt);
        Rig->SetAbsoluteTime(Dt * f);
        if (bBackwards) { Rig->Execute(FRigUnit_InverseExecution::EventName); }
        Rig->Execute(FRigUnit_BeginExecution::EventName);

        for (const FRigElementKey& K : AllBones)
        {
            const FVector P = H->GetGlobalTransform(K, false).GetLocation();
            if (f == 0) { StartLoc.Add(K, P); }
            LastLoc.Add(K, P);
            SumLoc[K] += P;
        }
    }

    TArray<TSharedPtr<FJsonValue>> Bones;
    for (const FRigElementKey& K : AllBones)
    {
        const FVector S = StartLoc.FindRef(K);
        const FVector E = LastLoc.FindRef(K);
        const FVector M = SumLoc[K] / (double)NumFrames;
        const double Travel = FVector::Dist(S, E);
        TSharedRef<FJsonObject> B = MakeShared<FJsonObject>();
        B->SetStringField(TEXT("bone"), K.Name.ToString());
        B->SetBoolField(TEXT("driven"), DrivenBones.Contains(K));
        B->SetField(TEXT("start"), MCPCRRt_Vec3(S));
        B->SetField(TEXT("end"), MCPCRRt_Vec3(E));
        B->SetField(TEXT("mean"), MCPCRRt_Vec3(M));
        B->SetNumberField(TEXT("travel_cm"), MCPCRRt_R3(Travel));
        Bones.Add(MakeShared<FJsonValueObject>(B));
    }

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetStringField(TEXT("anim_sequence"), AnimSequencePath);
    Root->SetNumberField(TEXT("frames"), NumFrames);
    Root->SetNumberField(TEXT("source_frames"), NumSrcFrames);
    Root->SetNumberField(TEXT("driven_bone_count"), DrivenBones.Num());
    Root->SetBoolField(TEXT("used_backwards_solve"), bBackwards);
    if (!bBackwards)
    {
        Root->SetStringField(TEXT("note"),
            TEXT("rig has no Backwards Solve; forward solve is control-driven so the bone drive has limited "
                 "effect — a full anim->control bake requires the sequencer subsystem (out of scope)"));
    }
    Root->SetNumberField(TEXT("bone_count"), Bones.Num());
    Root->SetArrayField(TEXT("bones"), Bones);
    return MCPCRRt_SerializeJson(Root);
#else
    return MCPCRRt_Error(TEXT("editor-only (anim DataModel sampling is WITH_EDITOR)"));
#endif
}

// =========================================================================================================
// 6a) start_rig_motion_capture — solve the rig `frames` times (advancing time), record every bone's global
//     location per frame into a static stash keyed by control_rig_path (data-only => GC-safe). Overwrites
//     any prior capture for the same path.
// =========================================================================================================
FString UMCPReflectionLibrary::StartRigMotionCaptureJson(const FString& ControlRigPath, int32 Frames, float DeltaTime)
{
    FString Err;
    UClass* GenClass = MCPCRRt_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRRt_Error(Err); }

    UControlRig* Rig = MCPCRRt_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRRt_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRRt_ForwardSolve(Rig, Err)) { return MCPCRRt_Error(Err); }
    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRRt_Error(TEXT("null hierarchy")); }

    const int32 NumFrames = FMath::Clamp(Frames, 1, 5000);
    const float Dt = (DeltaTime <= 0.f) ? (1.f / 30.f) : DeltaTime;

    const TArray<FRigElementKey> BoneKeys = H->GetAllKeys(false, ERigElementType::Bone);

    FMCPCRRt_MotionBuffer Buf;
    Buf.FrameCount = NumFrames;
    Buf.DeltaTime = Dt;
    Buf.CapturedAtSeconds = FPlatformTime::Seconds();
    Buf.BoneNames.Reserve(BoneKeys.Num());
    Buf.PerBonePos.SetNum(BoneKeys.Num());
    for (int32 b = 0; b < BoneKeys.Num(); ++b)
    {
        Buf.BoneNames.Add(BoneKeys[b].Name);
        Buf.PerBonePos[b].Reserve(NumFrames);
    }

    int32 Captured = 0;
    for (int32 f = 0; f < NumFrames; ++f)
    {
        if (f > 0 && !MCPCRRt_SolveFrame(Rig, Dt, Dt * f)) { break; } // frame 0 already solved by warm-up
        for (int32 b = 0; b < BoneKeys.Num(); ++b)
        {
            Buf.PerBonePos[b].Add(H->GetGlobalTransform(BoneKeys[b], false).GetLocation());
        }
        ++Captured;
    }
    Buf.FrameCount = Captured;
    GMCPCRRt_MotionBuffers.Add(ControlRigPath, MoveTemp(Buf));

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("frames_captured"), Captured);
    Root->SetNumberField(TEXT("bone_count"), BoneKeys.Num());
    Root->SetStringField(TEXT("note"), TEXT("call get_rig_motion_report with the same control_rig_path"));
    return MCPCRRt_SerializeJson(Root);
}

// =========================================================================================================
// 6b) get_rig_motion_report — read the stashed capture for control_rig_path and report per-bone min/max/mean
//     displacement (distance from each bone's frame-0 position).
// =========================================================================================================
FString UMCPReflectionLibrary::GetRigMotionReportJson(const FString& ControlRigPath)
{
    const FMCPCRRt_MotionBuffer* Buf = GMCPCRRt_MotionBuffers.Find(ControlRigPath);
    if (!Buf)
    {
        return MCPCRRt_Error(FString::Printf(
            TEXT("no motion capture stashed for '%s' — call start_rig_motion_capture first"), *ControlRigPath));
    }
    if (Buf->FrameCount <= 0)
    {
        return MCPCRRt_Error(TEXT("stashed capture is empty"));
    }

    TArray<TSharedPtr<FJsonValue>> Bones;
    for (int32 b = 0; b < Buf->BoneNames.Num(); ++b)
    {
        const TArray<FVector>& Pos = Buf->PerBonePos[b];
        if (Pos.Num() == 0) { continue; }
        const FVector P0 = Pos[0];
        double MinD = 0.0, MaxD = 0.0, SumD = 0.0;
        bool bFirst = true;
        for (const FVector& P : Pos)
        {
            const double D = FVector::Dist(P, P0);
            SumD += D;
            if (bFirst) { MinD = MaxD = D; bFirst = false; }
            else { MinD = FMath::Min(MinD, D); MaxD = FMath::Max(MaxD, D); }
        }
        const double MeanD = SumD / (double)Pos.Num();
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("bone"), Buf->BoneNames[b].ToString());
        J->SetNumberField(TEXT("min_disp_cm"), MCPCRRt_R3(MinD));
        J->SetNumberField(TEXT("max_disp_cm"), MCPCRRt_R3(MaxD));
        J->SetNumberField(TEXT("mean_disp_cm"), MCPCRRt_R3(MeanD));
        Bones.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPCRRt_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("frames"), Buf->FrameCount);
    Root->SetNumberField(TEXT("bone_count"), Bones.Num());
    Root->SetArrayField(TEXT("bones"), Bones);
    return MCPCRRt_SerializeJson(Root);
}
