// UnrealMCP reflection helpers — CONTROL RIG PHYSICS / VALIDATION spec features.
//
// WHAT / WHY C++: these 6 features analyze the SOLVED geometry (and, where the rig has physics, the SIMULATED
// motion) of a compiled Control Rig. Stock Python can read a rig's AUTHORING hierarchy but cannot instantiate
// the compiled rig, run its Forwards Solve / physics step, and inspect the resulting per-bone matrices. That
// needs the ControlRig RUNTIME class UControlRig — C++ only. The harness here is copied from
// MCPReflection_ControlRigRuntime.cpp (transient UControlRig from <UBlueprint>->GeneratedClass; Initialize(true)
// + RequestInit() + Execute(PrepareForExecution) + Execute(BeginExecution); read Rig->GetHierarchy()). Prefixed
// `MCPCRPh_` so the anon-namespace helpers stay unique in the unity build. The SOURCE ASSET IS NEVER TOUCHED —
// every write (a control offset in the probe) happens on the transient instance, so there is nothing to undo and
// no ledger op is emitted (all reads / transient-only writes).
//
// ---------------------------------------------------------------------------------------------------------
// THE PHYSICS ARCHITECTURE FINDING (UE 5.8) — why we DON'T need to link the ControlRigPhysics plugin.
// In 5.8 the ControlRig *core* module no longer runs simulations (Rigs/RigPhysics.h: FRigPhysicsSimulationBase
// is `Deprecated=5.8` — "Control Rig core no longer supports simulations. Control Rig Physics (for example) now
// stores it in the solver component"). Physics moved to the EXPERIMENTAL plugin
//   Engine/Plugins/Experimental/ControlRigPhysics  (module ControlRigPhysics, EnabledByDefault=true, IsBeta).
// It steps a Chaos ImmediatePhysics::FSimulation (headless — same engine used by the RigidBody anim node in
// Persona preview, no UWorld/PIE required for the sim itself). CRUCIALLY, the step is driven from INSIDE the
// rig's own VM graph by the node "Step Physics Solver" (FRigUnit_StepPhysicsSolver1, RigPhysicsSolverExecution.cpp)
// which runs during the Forwards Solve event and reads DeltaTime from the ExecuteContext. So the EXISTING harness
// — repeated Rig->Execute(BeginExecution) after Rig->SetDeltaTime(dt) — ALREADY steps the physics. We drive it
// through UControlRig and read the solved hierarchy; we never touch a ControlRigPhysics type, so BUILD.CS NEEDS
// NO CHANGE (ControlRig is already a dependency; ControlRigPhysics / Chaos / PhysicsCore are NOT linked).
//
// Verified null-safety for a TRANSIENT rig (no owning component / no world):
//   * FRigUnit_StepPhysicsSolver1::Execute calls PhysicsSolver->StepSimulation(ControlRig->GetWorld(),
//     OwningActor, ...). For our transient rig GetWorld()==nullptr (ControlRig.cpp:334 falls to
//     Super::GetWorld() over the transient package -> null) and GetOwningSceneComponent()==nullptr
//     (ControlRig.cpp:3937, null-guarded). StepSimulation tolerates both.
//   * World-object collision (RigPhysicsSolver_WorldObjects.cpp:208) is `if (WeakWorld.Pin())` — a null world
//     simply skips the overlap, so even a solver authored with WorldCollisionType!=None can't crash us.
//   * The step is gated by CVar `ControlRig.Physics.EnableStepSolver` (RigPhysicsSolverExecution.cpp:28) which
//     DEFAULTS TO 1. If a user set it to 0 the sim silently won't advance (we'd report contains_simulation=false).
//
// HONEST FEASIBILITY (per feature):
//   * validate_rig_deformation  — FEASIBLE headless. Pure geometry on the solved hierarchy: rebuild each
//     element's concatenated GLOBAL matrix from local FMatrices (FTransform cannot represent the shear that
//     non-uniform parent scale injects, so we multiply matrices ourselves) and decompose axis lengths (scale)
//     + inter-axis angles (shear).
//   * measure_mesh_penetration  — FEASIBLE headless, HEURISTIC. Models each bone as a sphere at its solved
//     position with radius = 0.5 * nearest-hierarchy-neighbour distance, then flags non-adjacent bone pairs
//     whose centre distance is closer than (r_i + r_j) by more than margin. This is a SKELETON-geometry proxy,
//     not a skin-accurate or physics-asset-body test (the signature carries no mesh/PhysicsAsset param). The
//     radius heuristic is reported per bone so the caller can judge it. An accurate variant (skin-weight bounds
//     a la get_skeletal_bone_bounds, or reading FRigPhysicsCollision bodies) is DEFERRED pending an extra param
//     + (for bodies) a ControlRigPhysics link.
//   * fit_rig_chain_collision   — FEASIBLE headless for the COMPUTE-AND-RETURN scope drafted here (fit a sphere
//     or capsule to the solved bone-chain positions + margin). WRITING the primitive back into a PhysicsAsset or
//     as a RigPhysicsBodyComponent is a heavier write path (needs ControlRigPhysics + undo) and is DEFERRED.
//   * validate_rig_physics / start_rig_physics_probe / get_rig_physics_probe_report — FEASIBLE headless *when
//     the rig contains physics nodes* (e.g. CR_Mannequin_Procedural). They run the same Execute() loop; the
//     physics rides along via the Step node. If the rig has no physics the handlers still run and report
//     contains_simulation=false + a census of any physics setup, so they degrade gracefully rather than lie.
//     CRASH-RISK NOTE: a genuinely corrupt physics graph could fault inside Execute() (no try/catch in UE) — the
//     one live-verify risk point. Verify get_rig_pose on the same rig first; then validate_rig_physics.
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
#include "Math/Matrix.h"                     // FMatrix (deformation decomposition)
#include "HAL/PlatformTime.h"                // FPlatformTime (probe timestamp)

// --- ControlRig RUNTIME (needs `ControlRig` in Build.cs — already present) --------------------------------
#include "ControlRig.h"                                   // UControlRig: Initialize/RequestInit/Execute/GetHierarchy/CanExecute/SupportsEvent/ContainsSimulation
#include "Rigs/RigHierarchy.h"                            // URigHierarchy: GetAllKeys/GetGlobalTransform/GetLocalTransform/GetFirstParent/Find/GetComponentKeys/FindComponent
#include "Rigs/RigHierarchyElements.h"                    // FRigControlElement
#include "Rigs/RigHierarchyComponents.h"                  // FRigBaseComponent (generic physics-component census, no ControlRigPhysics link)
#include "Rigs/RigHierarchyDefines.h"                     // ERigElementType / FRigElementKey / FRigComponentKey / ERigControlType
#include "Units/Execution/RigUnit_PrepareForExecution.h"  // "Construction" event name
#include "Units/Execution/RigUnit_BeginExecution.h"       // "Forwards Solve" event name

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------------
    FString MCPCRPh_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPCRPh_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPCRPh_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPCRPh_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

    double MCPCRPh_R3(double V) { return FMath::RoundToDouble(V * 1000.0) / 1000.0; }

    TSharedPtr<FJsonValue> MCPCRPh_Vec3(const FVector& V)
    {
        TArray<TSharedPtr<FJsonValue>> A;
        A.Add(MakeShared<FJsonValueNumber>(MCPCRPh_R3(V.X)));
        A.Add(MakeShared<FJsonValueNumber>(MCPCRPh_R3(V.Y)));
        A.Add(MakeShared<FJsonValueNumber>(MCPCRPh_R3(V.Z)));
        return MakeShared<FJsonValueArray>(A);
    }

    // Case-insensitive comma-separated substring filter. Empty list => match all.
    TArray<FString> MCPCRPh_ParseTokens(const FString& Csv)
    {
        TArray<FString> Out, Raw;
        Csv.ToLower().ParseIntoArray(Raw, TEXT(","), true);
        for (FString& T : Raw) { const FString Trimmed = T.TrimStartAndEnd(); if (!Trimmed.IsEmpty()) { Out.Add(Trimmed); } }
        return Out;
    }
    bool MCPCRPh_Matches(const FString& NameLower, const TArray<FString>& Tokens)
    {
        if (Tokens.Num() == 0) { return true; }
        for (const FString& Tok : Tokens) { if (NameLower.Contains(Tok)) { return true; } }
        return false;
    }

    bool MCPCRPh_HasTransform(ERigElementType Type)
    {
        return Type == ERigElementType::Bone || Type == ERigElementType::Null ||
               Type == ERigElementType::Control || Type == ERigElementType::Reference ||
               Type == ERigElementType::Socket;
    }

    // Resolve a control-rig blueprint path to its generated UControlRig subclass (blueprint path, generated
    // class path, or a UClass directly). Copied from the runtime harness.
    UClass* MCPCRPh_ResolveRigClass(const FString& Path, FString& OutError)
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
        if (GenClass == UControlRig::StaticClass() || GenClass->HasAnyClassFlags(CLASS_Abstract))
        {
            OutError = TEXT("resolved class is abstract / not a concrete rig");
            return nullptr;
        }
        return GenClass;
    }

    // Instantiate + Initialize a fresh transient rig. Caller keeps it rooted (TStrongObjectPtr) while using it.
    UControlRig* MCPCRPh_NewRig(UClass* GenClass, FString& OutError)
    {
        if (!GenClass) { OutError = TEXT("null generated class"); return nullptr; }
        UControlRig* Rig = nullptr;
        {
            FGCScopeGuard GCGuard; // match the engine: never create the rig while GC is running
            Rig = NewObject<UControlRig>(GetTransientPackage(), GenClass, NAME_None, RF_Transient);
        }
        if (!Rig) { OutError = TEXT("NewObject<UControlRig> returned null"); return nullptr; }
        Rig->Initialize(true);
        Rig->RequestInit();
        if (!Rig->GetHierarchy()) { OutError = TEXT("rig has no hierarchy after Initialize"); return nullptr; }
        return Rig;
    }

    // Run Construction (if present) then Forwards Solve. Returns false (never crashes) if the rig can't execute.
    bool MCPCRPh_ForwardSolve(UControlRig* Rig, FString& OutError)
    {
        if (!Rig || !Rig->CanExecute()) { OutError = TEXT("rig cannot execute (CanExecute()==false)"); return false; }
        if (!Rig->SupportsEvent(FRigUnit_BeginExecution::EventName))
        {
            OutError = TEXT("rig has no 'Forwards Solve' event");
            return false;
        }
        if (Rig->SupportsEvent(FRigUnit_PrepareForExecution::EventName))
        {
            Rig->Execute(FRigUnit_PrepareForExecution::EventName); // Construction (builds controls + physics components)
        }
        const bool bOk = Rig->Execute(FRigUnit_BeginExecution::EventName);
        if (!bOk) { OutError = TEXT("Forwards Solve Execute() returned false (rig likely failed to compile)"); }
        return bOk;
    }

    // Advance time + run one forward solve (this is what steps any embedded physics Step node).
    bool MCPCRPh_SolveFrame(UControlRig* Rig, float DeltaTime, float AbsoluteTime)
    {
        if (!Rig || !Rig->CanExecute()) { return false; }
        Rig->SetDeltaTime(DeltaTime);
        Rig->SetAbsoluteTime(AbsoluteTime);
        return Rig->Execute(FRigUnit_BeginExecution::EventName);
    }

    // Is this control type spatial (a cm translation offset is meaningful)?
    bool MCPCRPh_IsSpatialControl(const URigHierarchy* H, const FRigElementKey& Key, FString* OutTypeName = nullptr)
    {
        if (!H) { return false; }
        if (const FRigBaseElement* Elem = H->Find(Key))
        {
            if (const FRigControlElement* Ctrl = Cast<FRigControlElement>(Elem))
            {
                const ERigControlType T = Ctrl->Settings.ControlType;
                if (OutTypeName)
                {
                    if (const UEnum* E = StaticEnum<ERigControlType>()) { *OutTypeName = E->GetNameStringByValue((int64)T); }
                }
                return (T == ERigControlType::Position || T == ERigControlType::Transform ||
                        T == ERigControlType::EulerTransform || T == ERigControlType::TransformNoScale);
            }
        }
        return false;
    }

    // Rebuild an element's concatenated GLOBAL matrix (scale + any shear from non-uniform parent scale) by
    // multiplying local FMatrices up the single-parent chain. Memoized. Depth-guarded against cycles.
    FMatrix MCPCRPh_GlobalMatrix(URigHierarchy* H, const FRigElementKey& Key, TMap<FRigElementKey, FMatrix>& Cache)
    {
        if (const FMatrix* Found = Cache.Find(Key)) { return *Found; }
        FMatrix Local = H->GetLocalTransform(Key, false).ToMatrixWithScale();
        const FRigElementKey Parent = H->GetFirstParent(Key);
        FMatrix Global = Local;
        if (Parent.IsValid() && Parent != Key && Cache.Num() < 100000)
        {
            Global = Local * MCPCRPh_GlobalMatrix(H, Parent, Cache);
        }
        Cache.Add(Key, Global);
        return Global;
    }

    // Angle (degrees) between two vectors; returns 90 for a degenerate (zero-length) input so it never flags.
    double MCPCRPh_AngleDeg(const FVector& A, const FVector& B)
    {
        const double LA = A.Size(), LB = B.Size();
        if (LA <= SMALL_NUMBER || LB <= SMALL_NUMBER) { return 90.0; }
        const double C = FMath::Clamp(FVector::DotProduct(A, B) / (LA * LB), -1.0, 1.0);
        return FMath::RadiansToDegrees(FMath::Acos(C));
    }

    // Census the physics COMPONENTS on the hierarchy by concrete struct name — no ControlRigPhysics link needed
    // (FRigBaseComponent::GetScriptStruct() resolves the real type). Returns e.g. {RigPhysicsSolverComponent:1,
    // RigPhysicsBodyComponent:20, ...} plus the count of ERigElementType::Physics elements.
    void MCPCRPh_PhysicsCensus(URigHierarchy* H, TSharedRef<FJsonObject>& Out)
    {
        TMap<FString, int32> ByType;
        int32 TotalComponents = 0;
        for (const FRigElementKey& K : H->GetAllKeys(false, ERigElementType::All))
        {
            for (const FRigComponentKey& CK : H->GetComponentKeys(K))
            {
                if (const FRigBaseComponent* Comp = H->FindComponent(CK))
                {
                    ++TotalComponents;
                    FString TypeName = TEXT("FRigBaseComponent");
                    if (const UScriptStruct* SS = Comp->GetScriptStruct()) { TypeName = SS->GetName(); }
                    ByType.FindOrAdd(TypeName)++;
                }
            }
        }
        const int32 PhysicsElemCount = H->GetAllKeys(false, ERigElementType::Physics).Num();

        TSharedRef<FJsonObject> Census = MakeShared<FJsonObject>();
        Census->SetNumberField(TEXT("physics_element_count"), PhysicsElemCount);
        Census->SetNumberField(TEXT("component_count"), TotalComponents);
        TSharedRef<FJsonObject> Types = MakeShared<FJsonObject>();
        int32 PhysicsComponentCount = 0;
        for (const TPair<FString, int32>& P : ByType)
        {
            Types->SetNumberField(P.Key, P.Value);
            if (P.Key.Contains(TEXT("Physics"))) { PhysicsComponentCount += P.Value; }
        }
        Census->SetObjectField(TEXT("component_types"), Types);
        Census->SetNumberField(TEXT("physics_component_count"), PhysicsComponentCount);
        Out->SetObjectField(TEXT("physics_setup"), Census);
    }

    // ---- start_rig_physics_probe / get_rig_physics_probe_report stash ------------------------------------
    // Data-only (no UObject refs) so it is GC-safe to keep in a static map across MCP calls.
    struct FMCPCRPh_ProbeBuffer
    {
        FString  PerturbedControl;
        float    ShakeCm = 0.f;
        float    DeltaTime = 0.f;
        int32    SettleFrames = 0;
        bool     bContainsSimulation = false;
        TArray<FName> BoneNames;
        TArray<TArray<FVector>> PerBonePos; // [boneIndex][settleFrame] global location
        double   CapturedAtSeconds = 0.0;
    };
    static TMap<FString, FMCPCRPh_ProbeBuffer> GMCPCRPh_ProbeBuffers; // keyed by control_rig_path
}

// =========================================================================================================
// 1) validate_rig_physics — instantiate + census the physics setup, then run the solve for a fixed observation
//    window (physics rides along via the rig's Step node) and report solver stability: NaN/explosion blow-ups,
//    per-bone drift beyond deviation_threshold_cm, and residual end-of-window velocity. contains_simulation
//    tells the caller whether physics actually stepped (false => the rig has no physics; the check is trivially
//    stable and only the census is meaningful). Signature carries no frame count, so an internal window is used.
// =========================================================================================================
FString UMCPReflectionLibrary::ValidateRigPhysicsJson(const FString& ControlRigPath, float DeviationThresholdCm)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRPh_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRPh_Error(Err); }

    UControlRig* Rig = MCPCRPh_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRPh_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRPh_ForwardSolve(Rig, Err)) { return MCPCRPh_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRPh_Error(TEXT("null hierarchy after solve")); }

    const float Threshold = (DeviationThresholdCm > 0.f) ? DeviationThresholdCm : 1.0f;
    const int32 ObserveFrames = 90;           // internal observation window (~1.5s @ 60fps)
    const float Dt = 1.f / 60.f;              // physics prefers a small fixed step

    const TArray<FRigElementKey> BoneKeys = H->GetAllKeys(false, ERigElementType::Bone);

    // Baseline (frame-0 solved) globals + per-bone tracking.
    TMap<FRigElementKey, FVector> BaseLoc, PrevLoc;
    for (const FRigElementKey& K : BoneKeys)
    {
        const FVector P = H->GetGlobalTransform(K, false).GetLocation();
        BaseLoc.Add(K, P);
        PrevLoc.Add(K, P);
    }

    TMap<FRigElementKey, double> MaxDrift, LastStep;
    for (const FRigElementKey& K : BoneKeys) { MaxDrift.Add(K, 0.0); LastStep.Add(K, 0.0); }

    bool bNaN = false, bExplosion = false;
    int32 FramesRun = 0;
    for (int32 f = 1; f <= ObserveFrames; ++f)
    {
        if (!MCPCRPh_SolveFrame(Rig, Dt, Dt * f)) { break; }
        ++FramesRun;
        for (const FRigElementKey& K : BoneKeys)
        {
            const FVector P = H->GetGlobalTransform(K, false).GetLocation();
            if (P.ContainsNaN()) { bNaN = true; }
            if (P.SizeSquared() > (1.0e7 * 1.0e7)) { bExplosion = true; } // > 100km from origin = blown up
            const double Drift = FVector::Dist(P, BaseLoc[K]);
            double& MD = MaxDrift.FindChecked(K); if (Drift > MD) { MD = Drift; }
            LastStep.FindChecked(K) = FVector::Dist(P, PrevLoc[K]); // per-frame residual velocity (cm/frame)
            PrevLoc.FindChecked(K) = P;
        }
    }

    const bool bContainsSim = Rig->ContainsSimulation();

    // Collect bones that drifted past threshold, or are still moving fast at the end of the window.
    TArray<TSharedPtr<FJsonValue>> Deviations;
    double GlobalMaxDrift = 0.0, GlobalMaxResidual = 0.0;
    for (const FRigElementKey& K : BoneKeys)
    {
        const double Drift = MaxDrift[K];
        const double Residual = LastStep[K];
        GlobalMaxDrift = FMath::Max(GlobalMaxDrift, Drift);
        GlobalMaxResidual = FMath::Max(GlobalMaxResidual, Residual);
        const bool bFlag = (Drift > (double)Threshold) || (Residual > (double)Threshold);
        if (!bFlag) { continue; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("bone"), K.Name.ToString());
        J->SetNumberField(TEXT("max_drift_cm"), MCPCRPh_R3(Drift));
        J->SetNumberField(TEXT("residual_cm_per_frame"), MCPCRPh_R3(Residual));
        Deviations.Add(MakeShared<FJsonValueObject>(J));
    }
    Deviations.Sort([](const TSharedPtr<FJsonValue>& A, const TSharedPtr<FJsonValue>& B)
    {
        return A->AsObject()->GetNumberField(TEXT("max_drift_cm")) > B->AsObject()->GetNumberField(TEXT("max_drift_cm"));
    });

    const bool bStable = !bNaN && !bExplosion;

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetBoolField(TEXT("contains_simulation"), bContainsSim);
    Root->SetNumberField(TEXT("deviation_threshold_cm"), Threshold);
    Root->SetNumberField(TEXT("observe_frames"), ObserveFrames);
    Root->SetNumberField(TEXT("frames_run"), FramesRun);
    Root->SetNumberField(TEXT("delta_time"), MCPCRPh_R3(Dt));
    Root->SetBoolField(TEXT("stable"), bStable);
    Root->SetBoolField(TEXT("has_nan"), bNaN);
    Root->SetBoolField(TEXT("exploded"), bExplosion);
    Root->SetNumberField(TEXT("max_drift_cm"), MCPCRPh_R3(GlobalMaxDrift));
    Root->SetNumberField(TEXT("max_residual_cm_per_frame"), MCPCRPh_R3(GlobalMaxResidual));
    Root->SetNumberField(TEXT("bone_count"), BoneKeys.Num());
    Root->SetNumberField(TEXT("deviating_bone_count"), Deviations.Num());
    Root->SetArrayField(TEXT("deviations"), Deviations);
    MCPCRPh_PhysicsCensus(H, Root);
    if (!bContainsSim)
    {
        Root->SetStringField(TEXT("note"),
            TEXT("contains_simulation=false: no Step Physics Solver ran (rig has no physics, or "
                 "ControlRig.Physics.EnableStepSolver=0). Drift/residual reflect the deterministic forward solve, "
                 "not a simulation; only physics_setup is meaningful for a non-physics rig."));
    }
    return MCPCRPh_SerializeJson(Root);
#else
    return MCPCRPh_Error(TEXT("editor-only"));
#endif
}

// =========================================================================================================
// 2) validate_rig_deformation — instantiate + forward-solve, then for every transform-bearing element rebuild
//    the concatenated GLOBAL matrix from local FMatrices (FTransform can't carry shear) and decompose:
//      * non-uniform SCALE: ratio of longest to shortest basis-axis length > (1 + scale_tolerance).
//      * SHEAR: any inter-axis angle deviates from 90 deg by > shear_tolerance_deg.
//      * FLIPPED: negative matrix determinant (mirrored basis).
//    Pure geometry; no physics. Fully feasible headless.
// =========================================================================================================
FString UMCPReflectionLibrary::ValidateRigDeformationJson(const FString& ControlRigPath, float ScaleTolerance, float ShearToleranceDeg)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRPh_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRPh_Error(Err); }

    UControlRig* Rig = MCPCRPh_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRPh_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRPh_ForwardSolve(Rig, Err)) { return MCPCRPh_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRPh_Error(TEXT("null hierarchy after solve")); }

    const double ScaleTol = (ScaleTolerance > 0.f) ? (double)ScaleTolerance : 0.05;   // 5% default
    const double ShearTol = (ShearToleranceDeg > 0.f) ? (double)ShearToleranceDeg : 1.0; // 1 deg default

    TMap<FRigElementKey, FMatrix> Cache;
    TArray<TSharedPtr<FJsonValue>> Flagged;
    int32 Examined = 0, NonUniform = 0, Sheared = 0, Flippd = 0;

    for (const FRigElementKey& K : H->GetAllKeys(false, ERigElementType::All))
    {
        if (!MCPCRPh_HasTransform(K.Type)) { continue; }
        ++Examined;

        const FMatrix M = MCPCRPh_GlobalMatrix(H, K, Cache);
        const FVector AX = M.GetScaledAxis(EAxis::X);
        const FVector AY = M.GetScaledAxis(EAxis::Y);
        const FVector AZ = M.GetScaledAxis(EAxis::Z);
        const double LX = AX.Size(), LY = AY.Size(), LZ = AZ.Size();

        const double MaxL = FMath::Max3(LX, LY, LZ);
        const double MinL = FMath::Min3(LX, LY, LZ);
        const double ScaleRatio = (MinL > SMALL_NUMBER) ? (MaxL / MinL) : 0.0;

        const double AngXY = MCPCRPh_AngleDeg(AX, AY);
        const double AngYZ = MCPCRPh_AngleDeg(AY, AZ);
        const double AngXZ = MCPCRPh_AngleDeg(AX, AZ);
        const double MaxShear = FMath::Max3(FMath::Abs(90.0 - AngXY), FMath::Abs(90.0 - AngYZ), FMath::Abs(90.0 - AngXZ));

        const double Det = M.Determinant();

        const bool bNonUniform = (ScaleRatio > (1.0 + ScaleTol));
        const bool bShear = (MaxShear > ShearTol);
        const bool bFlipped = (Det < 0.0);

        if (bNonUniform) { ++NonUniform; }
        if (bShear) { ++Sheared; }
        if (bFlipped) { ++Flippd; }
        if (!bNonUniform && !bShear && !bFlipped) { continue; }

        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), K.Name.ToString());
        J->SetStringField(TEXT("type"),
            K.Type == ERigElementType::Bone ? TEXT("bone") :
            K.Type == ERigElementType::Control ? TEXT("control") :
            K.Type == ERigElementType::Null ? TEXT("null") :
            K.Type == ERigElementType::Socket ? TEXT("socket") : TEXT("other"));
        J->SetField(TEXT("axis_lengths"), MCPCRPh_Vec3(FVector(LX, LY, LZ)));
        J->SetNumberField(TEXT("scale_ratio"), MCPCRPh_R3(ScaleRatio));
        J->SetNumberField(TEXT("max_shear_deg"), MCPCRPh_R3(MaxShear));
        J->SetNumberField(TEXT("determinant"), MCPCRPh_R3(Det));
        J->SetBoolField(TEXT("non_uniform_scale"), bNonUniform);
        J->SetBoolField(TEXT("shear"), bShear);
        J->SetBoolField(TEXT("flipped"), bFlipped);
        Flagged.Add(MakeShared<FJsonValueObject>(J));
    }
    Flagged.Sort([](const TSharedPtr<FJsonValue>& A, const TSharedPtr<FJsonValue>& B)
    {
        return A->AsObject()->GetNumberField(TEXT("max_shear_deg")) > B->AsObject()->GetNumberField(TEXT("max_shear_deg"));
    });

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("scale_tolerance"), ScaleTol);
    Root->SetNumberField(TEXT("shear_tolerance_deg"), ShearTol);
    Root->SetNumberField(TEXT("examined_element_count"), Examined);
    Root->SetNumberField(TEXT("non_uniform_scale_count"), NonUniform);
    Root->SetNumberField(TEXT("shear_count"), Sheared);
    Root->SetNumberField(TEXT("flipped_count"), Flippd);
    Root->SetNumberField(TEXT("flagged_count"), Flagged.Num());
    Root->SetBoolField(TEXT("clean"), Flagged.Num() == 0);
    Root->SetArrayField(TEXT("flagged"), Flagged);
    Root->SetStringField(TEXT("method"),
        TEXT("concatenated global FMatrix per element (local matrices multiplied up the GetFirstParent chain); "
             "scale = basis-axis length ratio, shear = inter-axis angle vs 90deg, flipped = negative determinant"));
    return MCPCRPh_SerializeJson(Root);
#else
    return MCPCRPh_Error(TEXT("editor-only"));
#endif
}

// =========================================================================================================
// 3) start_rig_physics_probe — solve to rest, perturb a spatial control by shake_cm (+X, held), then solve
//    settle_frames advancing time; the physics (if any) rides along via the rig's Step node. Every bone's
//    per-frame global position is stashed (data-only => GC-safe) keyed by control_rig_path for the report.
//    Perturbing-and-holding measures the settle/ring-down toward the new equilibrium (a held offset is the
//    only control-level perturbation available without a ControlRigPhysics impulse API).
// =========================================================================================================
FString UMCPReflectionLibrary::StartRigPhysicsProbeJson(const FString& ControlRigPath, const FString& Control, float ShakeCm, int32 SettleFrames)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRPh_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRPh_Error(Err); }

    UControlRig* Rig = MCPCRPh_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRPh_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRPh_ForwardSolve(Rig, Err)) { return MCPCRPh_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRPh_Error(TEXT("null hierarchy after solve")); }

    const float Shake = (FMath::Abs(ShakeCm) < KINDA_SMALL_NUMBER) ? 10.f : ShakeCm;
    const int32 Frames = FMath::Clamp(SettleFrames, 1, 5000);
    const float Dt = 1.f / 60.f;

    // Resolve the control: exact FName first, else case-insensitive substring over spatial controls.
    const TArray<FRigElementKey> ControlKeys = H->GetAllKeys(false, ERigElementType::Control);
    FRigElementKey Target;
    const FString WantLower = Control.ToLower();
    for (const FRigElementKey& K : ControlKeys) { if (K.Name.ToString().Equals(Control, ESearchCase::IgnoreCase)) { Target = K; break; } }
    if (!Target.IsValid() && !WantLower.IsEmpty())
    {
        for (const FRigElementKey& K : ControlKeys)
        {
            if (K.Name.ToString().ToLower().Contains(WantLower) && MCPCRPh_IsSpatialControl(H, K)) { Target = K; break; }
        }
    }
    if (!Target.IsValid())
    {
        return MCPCRPh_Error(FString::Printf(TEXT("control '%s' not found among %d controls"), *Control, ControlKeys.Num()));
    }
    FString CtrlTypeName;
    if (!MCPCRPh_IsSpatialControl(H, Target, &CtrlTypeName))
    {
        return MCPCRPh_Error(FString::Printf(
            TEXT("control '%s' is %s — not spatial; a cm shake is only meaningful on a Position/Transform control"),
            *Target.Name.ToString(), *CtrlTypeName));
    }

    // Warm the sim to rest at the un-perturbed pose so the baseline is steady (harmless for non-physics rigs).
    for (int32 f = 1; f <= 8; ++f) { if (!MCPCRPh_SolveFrame(Rig, Dt, Dt * f)) { break; } }

    // Perturb: offset the control's local translation by +X shake_cm and HOLD it, then re-solve.
    FTransform L = Rig->GetControlLocalTransform(Target.Name);
    L.AddToTranslation(FVector((double)Shake, 0.0, 0.0));
    Rig->SetControlLocalTransform(Target.Name, L);

    const TArray<FRigElementKey> BoneKeys = H->GetAllKeys(false, ERigElementType::Bone);

    FMCPCRPh_ProbeBuffer Buf;
    Buf.PerturbedControl = Target.Name.ToString();
    Buf.ShakeCm = Shake;
    Buf.DeltaTime = Dt;
    Buf.SettleFrames = Frames;
    Buf.CapturedAtSeconds = FPlatformTime::Seconds();
    Buf.BoneNames.Reserve(BoneKeys.Num());
    Buf.PerBonePos.SetNum(BoneKeys.Num());
    for (int32 b = 0; b < BoneKeys.Num(); ++b) { Buf.BoneNames.Add(BoneKeys[b].Name); Buf.PerBonePos[b].Reserve(Frames); }

    int32 Captured = 0;
    for (int32 f = 0; f < Frames; ++f)
    {
        // AbsoluteTime continues past the warm-up window so the solver sees a monotone clock.
        if (!MCPCRPh_SolveFrame(Rig, Dt, Dt * (float)(9 + f))) { break; }
        for (int32 b = 0; b < BoneKeys.Num(); ++b)
        {
            Buf.PerBonePos[b].Add(H->GetGlobalTransform(BoneKeys[b], false).GetLocation());
        }
        ++Captured;
    }
    Buf.SettleFrames = Captured;
    Buf.bContainsSimulation = Rig->ContainsSimulation();
    GMCPCRPh_ProbeBuffers.Add(ControlRigPath, MoveTemp(Buf));

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetStringField(TEXT("perturbed_control"), Target.Name.ToString());
    Root->SetStringField(TEXT("control_type"), CtrlTypeName);
    Root->SetNumberField(TEXT("shake_cm"), Shake);
    Root->SetNumberField(TEXT("settle_frames_captured"), Captured);
    Root->SetNumberField(TEXT("delta_time"), MCPCRPh_R3(Dt));
    Root->SetNumberField(TEXT("bone_count"), BoneKeys.Num());
    Root->SetBoolField(TEXT("contains_simulation"), Buf.bContainsSimulation);
    Root->SetStringField(TEXT("note"),
        Buf.bContainsSimulation
            ? TEXT("physics stepped during settle; call get_rig_physics_probe_report with the same control_rig_path")
            : TEXT("contains_simulation=false — no physics ran, so the settle is a rigid one-frame jump; "
                   "residuals will be ~0. Call get_rig_physics_probe_report to confirm."));
    return MCPCRPh_SerializeJson(Root);
#else
    return MCPCRPh_Error(TEXT("editor-only"));
#endif
}

// =========================================================================================================
// 4) get_rig_physics_probe_report — read the stashed probe and report residual motion per bone: end-of-window
//    per-frame velocity (residual_cm_per_frame), total settle path length, net displacement, and peak. Bones
//    whose residual exceeds residual_threshold_cm are "not settled" (still ringing => underdamped/unstable).
//    Returns the top max_bones by residual.
// =========================================================================================================
FString UMCPReflectionLibrary::GetRigPhysicsProbeReportJson(const FString& ControlRigPath, float ResidualThresholdCm, int32 MaxBones)
{
    const FMCPCRPh_ProbeBuffer* Buf = GMCPCRPh_ProbeBuffers.Find(ControlRigPath);
    if (!Buf)
    {
        return MCPCRPh_Error(FString::Printf(
            TEXT("no physics probe stashed for '%s' — call start_rig_physics_probe first"), *ControlRigPath));
    }
    if (Buf->SettleFrames <= 0) { return MCPCRPh_Error(TEXT("stashed probe is empty")); }

    const double Threshold = (ResidualThresholdCm > 0.f) ? (double)ResidualThresholdCm : 0.1;
    const int32 Limit = (MaxBones > 0) ? MaxBones : 40;

    struct FRow { FString Bone; double Residual, Path, NetDisp, Peak; };
    TArray<FRow> Rows;
    int32 Unsettled = 0;
    double GlobalMaxResidual = 0.0;

    for (int32 b = 0; b < Buf->BoneNames.Num(); ++b)
    {
        const TArray<FVector>& Pos = Buf->PerBonePos[b];
        if (Pos.Num() == 0) { continue; }
        const FVector P0 = Pos[0];
        const FVector PN = Pos.Last();
        double Path = 0.0, Peak = 0.0;
        for (int32 k = 0; k < Pos.Num(); ++k)
        {
            Peak = FMath::Max(Peak, (double)FVector::Dist(Pos[k], P0));
            if (k > 0) { Path += FVector::Dist(Pos[k], Pos[k - 1]); }
        }
        const double Residual = (Pos.Num() >= 2) ? (double)FVector::Dist(PN, Pos[Pos.Num() - 2]) : 0.0;
        const double NetDisp = (double)FVector::Dist(PN, P0);
        GlobalMaxResidual = FMath::Max(GlobalMaxResidual, Residual);
        if (Residual > Threshold) { ++Unsettled; }
        Rows.Add({ Buf->BoneNames[b].ToString(), Residual, Path, NetDisp, Peak });
    }

    Rows.Sort([](const FRow& A, const FRow& B) { return A.Residual > B.Residual; });

    TArray<TSharedPtr<FJsonValue>> Bones;
    for (int32 i = 0; i < Rows.Num() && i < Limit; ++i)
    {
        const FRow& R = Rows[i];
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("bone"), R.Bone);
        J->SetNumberField(TEXT("residual_cm_per_frame"), MCPCRPh_R3(R.Residual));
        J->SetNumberField(TEXT("settle_path_cm"), MCPCRPh_R3(R.Path));
        J->SetNumberField(TEXT("net_disp_cm"), MCPCRPh_R3(R.NetDisp));
        J->SetNumberField(TEXT("peak_disp_cm"), MCPCRPh_R3(R.Peak));
        J->SetBoolField(TEXT("settled"), R.Residual <= Threshold);
        Bones.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetStringField(TEXT("perturbed_control"), Buf->PerturbedControl);
    Root->SetNumberField(TEXT("shake_cm"), Buf->ShakeCm);
    Root->SetNumberField(TEXT("settle_frames"), Buf->SettleFrames);
    Root->SetNumberField(TEXT("delta_time"), MCPCRPh_R3(Buf->DeltaTime));
    Root->SetBoolField(TEXT("contains_simulation"), Buf->bContainsSimulation);
    Root->SetNumberField(TEXT("residual_threshold_cm"), Threshold);
    Root->SetNumberField(TEXT("bone_count"), Buf->BoneNames.Num());
    Root->SetNumberField(TEXT("unsettled_bone_count"), Unsettled);
    Root->SetNumberField(TEXT("max_residual_cm_per_frame"), MCPCRPh_R3(GlobalMaxResidual));
    Root->SetNumberField(TEXT("reported_bone_count"), Bones.Num());
    Root->SetArrayField(TEXT("bones"), Bones);
    if (!Buf->bContainsSimulation)
    {
        Root->SetStringField(TEXT("note"),
            TEXT("contains_simulation=false — residuals reflect a rigid (non-physics) response and are expected "
                 "to be ~0. A meaningful settle report needs a rig with a Step Physics Solver node."));
    }
    return MCPCRPh_SerializeJson(Root);
}

// =========================================================================================================
// 5) measure_mesh_penetration — instantiate + forward-solve, then treat each bone as a sphere at its solved
//    position with radius = 0.5 * distance to its nearest hierarchy neighbour (parent or closest child), and
//    flag NON-ADJACENT bone pairs whose centre distance is closer than (r_i + r_j) by more than margin_cm.
//    chain_filter selects the probe set, body_filter the target set (empty => all). SKELETON-geometry proxy —
//    NOT skin-accurate or physics-body-accurate (see header). Per-bone radii are reported for transparency.
// =========================================================================================================
FString UMCPReflectionLibrary::MeasureMeshPenetrationJson(const FString& ControlRigPath, const FString& ChainFilter, const FString& BodyFilter, float MarginCm)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRPh_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRPh_Error(Err); }

    UControlRig* Rig = MCPCRPh_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRPh_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRPh_ForwardSolve(Rig, Err)) { return MCPCRPh_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRPh_Error(TEXT("null hierarchy after solve")); }

    const double Margin = (double)MarginCm; // may be 0 (touching) or negative (require overlap slack)
    const TArray<FRigElementKey> BoneKeys = H->GetAllKeys(false, ERigElementType::Bone);
    const int32 N = BoneKeys.Num();
    if (N == 0) { return MCPCRPh_Error(TEXT("rig has no bones")); }

    // Positions + single-parent map + children lists.
    TArray<FVector> Pos; Pos.SetNum(N);
    TMap<FRigElementKey, int32> IndexOf;
    for (int32 i = 0; i < N; ++i) { Pos[i] = H->GetGlobalTransform(BoneKeys[i], false).GetLocation(); IndexOf.Add(BoneKeys[i], i); }

    TArray<int32> ParentIdx; ParentIdx.Init(INDEX_NONE, N);
    TArray<TArray<int32>> Children; Children.SetNum(N);
    for (int32 i = 0; i < N; ++i)
    {
        const FRigElementKey P = H->GetFirstParent(BoneKeys[i]);
        if (P.IsValid()) { if (const int32* Pi = IndexOf.Find(P)) { ParentIdx[i] = *Pi; Children[*Pi].Add(i); } }
    }

    // Radius = 0.5 * nearest-neighbour (parent or closest child) distance, floored to 0.5cm.
    TArray<double> Radius; Radius.SetNum(N);
    for (int32 i = 0; i < N; ++i)
    {
        double Nearest = TNumericLimits<double>::Max();
        if (ParentIdx[i] != INDEX_NONE) { Nearest = FMath::Min(Nearest, (double)FVector::Dist(Pos[i], Pos[ParentIdx[i]])); }
        for (int32 c : Children[i]) { Nearest = FMath::Min(Nearest, (double)FVector::Dist(Pos[i], Pos[c])); }
        Radius[i] = (Nearest == TNumericLimits<double>::Max()) ? 0.5 : FMath::Max(0.5, 0.5 * Nearest);
    }

    const TArray<FString> ChainTok = MCPCRPh_ParseTokens(ChainFilter);
    const TArray<FString> BodyTok = MCPCRPh_ParseTokens(BodyFilter);

    auto IsAdjacent = [&](int32 a, int32 b) -> bool
    { return ParentIdx[a] == b || ParentIdx[b] == a; };

    TArray<TSharedPtr<FJsonValue>> Hits;
    int32 ProbeCount = 0;
    double DeepestPen = 0.0;
    for (int32 i = 0; i < N; ++i)
    {
        if (!MCPCRPh_Matches(BoneKeys[i].Name.ToString().ToLower(), ChainTok)) { continue; }
        ++ProbeCount;
        for (int32 j = 0; j < N; ++j)
        {
            if (i == j) { continue; }
            if (!MCPCRPh_Matches(BoneKeys[j].Name.ToString().ToLower(), BodyTok)) { continue; }
            if (IsAdjacent(i, j)) { continue; }
            if (i > j && MCPCRPh_Matches(BoneKeys[i].Name.ToString().ToLower(), BodyTok)
                      && MCPCRPh_Matches(BoneKeys[j].Name.ToString().ToLower(), ChainTok))
            {
                continue; // avoid double-reporting a symmetric pair when both sets overlap
            }
            const double Dist = (double)FVector::Dist(Pos[i], Pos[j]);
            const double Penetration = (Radius[i] + Radius[j]) - Dist;
            if (Penetration > Margin)
            {
                DeepestPen = FMath::Max(DeepestPen, Penetration);
                TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
                J->SetStringField(TEXT("bone_a"), BoneKeys[i].Name.ToString());
                J->SetStringField(TEXT("bone_b"), BoneKeys[j].Name.ToString());
                J->SetNumberField(TEXT("distance_cm"), MCPCRPh_R3(Dist));
                J->SetNumberField(TEXT("radius_a_cm"), MCPCRPh_R3(Radius[i]));
                J->SetNumberField(TEXT("radius_b_cm"), MCPCRPh_R3(Radius[j]));
                J->SetNumberField(TEXT("penetration_cm"), MCPCRPh_R3(Penetration));
                Hits.Add(MakeShared<FJsonValueObject>(J));
            }
        }
    }
    Hits.Sort([](const TSharedPtr<FJsonValue>& A, const TSharedPtr<FJsonValue>& B)
    {
        return A->AsObject()->GetNumberField(TEXT("penetration_cm")) > B->AsObject()->GetNumberField(TEXT("penetration_cm"));
    });
    // Cap the payload.
    const int32 MaxHits = 200;
    const int32 TotalHits = Hits.Num();
    if (Hits.Num() > MaxHits) { Hits.SetNum(MaxHits); }

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetNumberField(TEXT("margin_cm"), Margin);
    Root->SetNumberField(TEXT("bone_count"), N);
    Root->SetNumberField(TEXT("probe_bone_count"), ProbeCount);
    Root->SetNumberField(TEXT("penetration_count"), TotalHits);
    Root->SetNumberField(TEXT("deepest_penetration_cm"), MCPCRPh_R3(DeepestPen));
    Root->SetNumberField(TEXT("reported_count"), Hits.Num());
    Root->SetArrayField(TEXT("penetrations"), Hits);
    Root->SetStringField(TEXT("method"),
        TEXT("SKELETON-geometry proxy: bone as sphere, radius = 0.5*nearest-hierarchy-neighbour distance "
             "(floored 0.5cm); non-adjacent pairs with (r_a+r_b-dist) > margin flagged. NOT skin-weight or "
             "physics-asset-body accurate — those need an extra mesh/PhysicsAsset param (deferred)."));
    return MCPCRPh_SerializeJson(Root);
#else
    return MCPCRPh_Error(TEXT("editor-only"));
#endif
}

// =========================================================================================================
// 6) fit_rig_chain_collision — instantiate + forward-solve, gather the bone-chain selected by module_name
//    (case-insensitive substring / module namespace over bone names; empty => all bones), and fit a collision
//    primitive to the solved positions + margin_cm. shape: "sphere" | "capsule" | "" (auto: capsule if the
//    chain is elongated, else sphere). COMPUTE-AND-RETURN only — writing the primitive into a PhysicsAsset or
//    as a RigPhysicsBodyComponent is a heavier write path (needs ControlRigPhysics + undo) and is DEFERRED.
// =========================================================================================================
FString UMCPReflectionLibrary::FitRigChainCollisionJson(const FString& ControlRigPath, const FString& ModuleName, float MarginCm, const FString& Shape)
{
#if WITH_EDITOR
    FString Err;
    UClass* GenClass = MCPCRPh_ResolveRigClass(ControlRigPath, Err);
    if (!GenClass) { return MCPCRPh_Error(Err); }

    UControlRig* Rig = MCPCRPh_NewRig(GenClass, Err);
    if (!Rig) { return MCPCRPh_Error(Err); }
    TStrongObjectPtr<UControlRig> RigGuard(Rig);
    if (!MCPCRPh_ForwardSolve(Rig, Err)) { return MCPCRPh_Error(Err); }

    URigHierarchy* H = Rig->GetHierarchy();
    if (!H) { return MCPCRPh_Error(TEXT("null hierarchy after solve")); }

    const double Margin = (double)MarginCm;
    const TArray<FString> ModuleTok = MCPCRPh_ParseTokens(ModuleName);

    TArray<FString> ChainNames;
    TArray<FVector> P;
    for (const FRigElementKey& K : H->GetAllKeys(false, ERigElementType::Bone))
    {
        if (!MCPCRPh_Matches(K.Name.ToString().ToLower(), ModuleTok)) { continue; }
        ChainNames.Add(K.Name.ToString());
        P.Add(H->GetGlobalTransform(K, false).GetLocation());
    }
    const int32 N = P.Num();
    if (N == 0) { return MCPCRPh_Error(FString::Printf(TEXT("no bones matched module_name '%s'"), *ModuleName)); }

    // Centroid + AABB extents (for auto shape + sphere fit).
    FVector Centroid = FVector::ZeroVector;
    FVector MinB(TNumericLimits<double>::Max()), MaxB(-TNumericLimits<double>::Max());
    for (const FVector& V : P) { Centroid += V; MinB = MinB.ComponentMin(V); MaxB = MaxB.ComponentMax(V); }
    Centroid /= (double)N;
    const FVector Extent = (MaxB - MinB);
    const double MaxExt = FMath::Max3(Extent.X, Extent.Y, Extent.Z);
    const double MidExt = Extent.X + Extent.Y + Extent.Z - MaxExt - FMath::Min3(Extent.X, Extent.Y, Extent.Z);

    FString ShapeLower = Shape.ToLower().TrimStartAndEnd();
    if (ShapeLower.IsEmpty()) { ShapeLower = (N >= 2 && MaxExt > 2.0 * FMath::Max(MidExt, 1.0)) ? TEXT("capsule") : TEXT("sphere"); }

    TSharedRef<FJsonObject> Root = MCPCRPh_Ok();
    Root->SetStringField(TEXT("control_rig"), ControlRigPath);
    Root->SetStringField(TEXT("module_name"), ModuleName);
    Root->SetNumberField(TEXT("margin_cm"), Margin);
    Root->SetNumberField(TEXT("chain_bone_count"), N);
    Root->SetStringField(TEXT("shape"), ShapeLower);
    Root->SetBoolField(TEXT("write_applied"), false);
    Root->SetStringField(TEXT("scope_note"),
        TEXT("compute-and-return only; writing collision into a PhysicsAsset / RigPhysicsBodyComponent is deferred"));

    TArray<TSharedPtr<FJsonValue>> Names;
    for (const FString& Nm : ChainNames) { Names.Add(MakeShared<FJsonValueString>(Nm)); }
    Root->SetArrayField(TEXT("chain_bones"), Names);

    if (ShapeLower == TEXT("sphere") || N < 2)
    {
        double R = 0.0;
        for (const FVector& V : P) { R = FMath::Max(R, (double)FVector::Dist(V, Centroid)); }
        R += Margin;
        TSharedRef<FJsonObject> S = MakeShared<FJsonObject>();
        S->SetField(TEXT("center"), MCPCRPh_Vec3(Centroid));
        S->SetNumberField(TEXT("radius_cm"), MCPCRPh_R3(FMath::Max(0.0, R)));
        Root->SetStringField(TEXT("fitted_shape"), TEXT("sphere"));
        Root->SetObjectField(TEXT("sphere"), S);
    }
    else
    {
        // Capsule axis from the farthest-apart pair (robust vs endpoint choice): A = farthest from centroid,
        // B = farthest from A. Project all points on the axis; radius = max perpendicular distance + margin.
        int32 ia = 0; double best = -1.0;
        for (int32 i = 0; i < N; ++i) { const double d = FVector::Dist(P[i], Centroid); if (d > best) { best = d; ia = i; } }
        int32 ib = 0; best = -1.0;
        for (int32 i = 0; i < N; ++i) { const double d = FVector::Dist(P[i], P[ia]); if (d > best) { best = d; ib = i; } }
        const FVector A = P[ia];
        FVector Axis = P[ib] - A;
        const double AxisLen = Axis.Size();
        if (AxisLen <= SMALL_NUMBER) { Axis = FVector::ForwardVector; } else { Axis /= AxisLen; }

        double tMin = TNumericLimits<double>::Max(), tMax = -TNumericLimits<double>::Max(), RadMax = 0.0;
        for (const FVector& V : P)
        {
            const double t = FVector::DotProduct(V - A, Axis);
            tMin = FMath::Min(tMin, t); tMax = FMath::Max(tMax, t);
            const FVector Perp = (V - A) - t * Axis;
            RadMax = FMath::Max(RadMax, Perp.Size());
        }
        const FVector EndA = A + tMin * Axis;
        const FVector EndB = A + tMax * Axis;
        const double Rad = RadMax + Margin;

        TSharedRef<FJsonObject> C = MakeShared<FJsonObject>();
        C->SetField(TEXT("point_a"), MCPCRPh_Vec3(EndA));
        C->SetField(TEXT("point_b"), MCPCRPh_Vec3(EndB));
        C->SetField(TEXT("center"), MCPCRPh_Vec3((EndA + EndB) * 0.5));
        C->SetField(TEXT("axis"), MCPCRPh_Vec3(Axis));
        C->SetNumberField(TEXT("segment_length_cm"), MCPCRPh_R3((double)FVector::Dist(EndA, EndB)));
        C->SetNumberField(TEXT("radius_cm"), MCPCRPh_R3(FMath::Max(0.0, Rad)));
        Root->SetStringField(TEXT("fitted_shape"), TEXT("capsule"));
        Root->SetObjectField(TEXT("capsule"), C);
    }
    return MCPCRPh_SerializeJson(Root);
#else
    return MCPCRPh_Error(TEXT("editor-only"));
#endif
}
