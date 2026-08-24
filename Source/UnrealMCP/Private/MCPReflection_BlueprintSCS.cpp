// ============================================================================
// MCPReflection_BlueprintSCS.cpp  —  BLUEPRINT SCS (Simple Construction Script)
//   COMPONENT AUTHORING C++ round. Six handlers that reach the SCS component
//   tree the stock Python API + list_blueprint_components CANNOT edit (the
//   attach hierarchy of a Blueprint's default components lives in
//   UBlueprint::SimpleConstructionScript -> USCS_Node tree, editor-only data):
//     1. GetBlueprintSCSJson            (READ  — walk SCS->GetAllNodes())
//     2. AddComponentToBlueprintJson    (WRITE — CreateNode + AddNode/AddChildNode)
//     3. SetBlueprintComponentPropertyJson (WRITE — FProperty on ComponentTemplate)
//     4. DeleteBlueprintComponentJson   (WRITE — RemoveNodeAndPromoteChildren)
//     5. ReparentBlueprintComponentJson (WRITE — RemoveNode + AddChildNode/AddNode)
//     6. SetBlueprintRootComponentJson  (WRITE — promote a scene node to scene root)
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** on purpose:
// implements DEFERRED UMCPReflectionLibrary methods that blueprint_components_cpp.py
// hasattr-guards on. When these link, the Python tools auto-enable. The matching
// UFUNCTION declarations are added to MCPReflectionLibrary.h by the COORDINATOR
// (do NOT edit the .h here — the exact decl lines are in the FINAL REPORT).
//
// CONVENTION: path-string handlers (like MCPReflection_Materials / Niagara2-5 /
// ControlRig) — each takes the Blueprint asset PATH as an FString and LoadObject()s
// it in C++. Returns an FString JSON payload; error JSON carries an "error" field
// (Python callers branch on res.get("error")). Anon-namespace helpers are prefixed
// MCPBpScs_ so they stay unique in the module's unity build.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Every symbol here is ENGINE_API (SimpleConstructionScript / SCS_Node are Engine
//   module — already linked) or UNREALED_API (FBlueprintEditorUtils::
//   MarkBlueprintAsStructurallyModified) / FKismetEditorUtilities::CompileBlueprint
//   (both UnrealEd — already a Build.cs dep). => NO Build.cs change, NO engine
//   export patch. If a symbol turns out NOT exported, REPORT the export patch.
//
//   Confirmed UE 5.8 signatures (verified against the source engine at
//   C:/Users/Joel/Documents/UnrealEngine-release):
//     USimpleConstructionScript::CreateNode(UClass*, FName=NAME_None) ENGINE_API
//       -> USCS_Node*   (SimpleConstructionScript.h:170 / .cpp:1465). NOTE the hard
//       check(NewComponentClass->IsChildOf(UActorComponent::StaticClass())) inside —
//       we MUST validate the class is an ActorComponent BEFORE calling (else editor down).
//     USimpleConstructionScript::AddNode(USCS_Node*) ENGINE_API  (.cpp:901) — adds to
//       RootNodes + AllNodes + ValidateSceneRootNodes().
//     USimpleConstructionScript::RemoveNode(USCS_Node*, bool bValidateSceneRootNodes=true)
//       ENGINE_API (.cpp:914) — root branch clears the node's parent refs; non-root
//       branch does FindParentNode()->RemoveChildNode() (which also drops it from AllNodes).
//     USimpleConstructionScript::RemoveNodeAndPromoteChildren(USCS_Node*) ENGINE_API (.cpp:974)
//       — root branch promotes first promotable child to root (inherits the removed node's
//       parent refs); non-root branch moves children onto the parent. LOSSY for undo (children
//       do not un-promote) — documented on handler #4.
//     USimpleConstructionScript::FindSCSNode(FName) ENGINE_API (.cpp:1050) — matches variable
//       name OR ComponentTemplate FName.
//     USimpleConstructionScript::FindParentNode(USCS_Node*) ENGINE_API (.cpp:1038).
//     USimpleConstructionScript::GetSceneRootComponentTemplate(bool=false, USCS_Node**=null) const
//       ENGINE_API #if WITH_EDITOR (.cpp:1084) — the current scene-root template + its SCS node
//       (null OutSCSNode => native/inherited root).
//     USimpleConstructionScript::GetAllNodes() const ENGINE_API #if WITH_EDITOR (.h:78).
//     USimpleConstructionScript::GetRootNodes()/GetDefaultSceneRootNode() (inline).
//     USCS_Node::AddChildNode(USCS_Node*, bool bAddToAllNodes=true) ENGINE_API (.cpp:247) —
//       ChildNodes.Add + AllNodes add. Same-SCS tree parenting needs ONLY this (the ChildNodes
//       tree IS the parent linkage at construction; ParentComponentOrVariableName is for
//       inherited/native parents only — see SubobjectDataSubsystem::AttachSubobject .cpp:2208).
//     USCS_Node::GetChildNodes() const (inline) / IsRootNode() ENGINE_API (.cpp:371) /
//       GetVariableName() (inline) / ComponentTemplate / ComponentClass / AttachToName (public).
//   The reparent / make-root sequences mirror USubobjectDataSubsystem::MakeNewSceneRoot
//   (.cpp:1792) + Attach/DetachSubobject (.cpp:2115/2158) at the raw-SCS level.
//
// All handlers: null-guarded, WITH_EDITOR-guarded, {"error":...} on any miss, never crash.
// #1 is non-mutating. #2-#6 end with FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified
// + FKismetEditorUtilities::CompileBlueprint (per the task) and mark the package dirty.
// SCS editing is VERSION-SENSITIVE — every such call is "VERIFY vs engine source"-tagged.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

// --- Core / reflection ---
#include "UObject/SoftObjectPath.h"
#include "UObject/Package.h"
#include "UObject/UnrealType.h"       // FProperty / typed FProperty casts
#include "UObject/UObjectGlobals.h"   // LoadObject / FindObject / StaticLoadObject
#include "Misc/PackageName.h"

// --- Engine (already linked; ENGINE_API) ---
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SCS_Node.h"
#include "Components/ActorComponent.h"
#include "Components/SceneComponent.h"

#if WITH_EDITOR
#include "Kismet2/BlueprintEditorUtils.h"   // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified (UNREALED_API)
#include "Kismet2/KismetEditorUtilities.h"  // FKismetEditorUtilities::CompileBlueprint
#endif

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPBpScs_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPBpScs_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPBpScs_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UBlueprint asset from a package/asset path (Python callers pass paths as STRINGS).
    UBlueprint* MCPBpScs_LoadBlueprint(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UObject* Obj = FSoftObjectPath(Path).TryLoad())
        {
            return Cast<UBlueprint>(Obj);
        }
        // Tolerate a package-only path ("/Game/Foo") by appending ".Foo".
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UBlueprint>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Resolve a UActorComponent subclass from: a full class path ("/Script/Engine.StaticMeshComponent"),
    // a BP-component asset/generated-class path ("/Game/.../BPC_Foo.BPC_Foo_C" or the UBlueprint asset),
    // or a bare short name ("StaticMeshComponent"). Returns nullptr if not found OR not an ActorComponent.
    // VERIFY vs engine source: UClass::IsChildOf(UActorComponent::StaticClass()) — the CreateNode() check().
    UClass* MCPBpScs_ResolveComponentClass(const FString& ClassPath)
    {
        if (ClassPath.IsEmpty())
        {
            return nullptr;
        }

        UClass* C = LoadObject<UClass>(nullptr, *ClassPath);
        if (!C) { C = FindObject<UClass>(nullptr, *ClassPath); }
        if (!C) { C = UClass::TryFindTypeSlow<UClass>(ClassPath); }
        if (!C)
        {
            // Bare engine class name convenience: "StaticMeshComponent" -> "/Script/Engine.StaticMeshComponent".
            if (!ClassPath.Contains(TEXT(".")) && !ClassPath.Contains(TEXT("/")))
            {
                C = LoadObject<UClass>(nullptr, *(FString(TEXT("/Script/Engine.")) + ClassPath));
            }
        }
        if (!C)
        {
            // A UBlueprint asset (a BP component class); use its generated class.
            if (UObject* Obj = LoadObject<UObject>(nullptr, *ClassPath))
            {
                if (UBlueprint* BP = Cast<UBlueprint>(Obj))
                {
                    C = BP->GeneratedClass;
                }
            }
        }

        if (C && C->IsChildOf(UActorComponent::StaticClass()) && !C->HasAnyClassFlags(CLASS_Abstract))
        {
            return C;
        }
        return nullptr;
    }

    // Return the effective parent NAME of a node: the same-SCS tree parent (FindParentNode) if any,
    // else the inherited/native parent recorded in ParentComponentOrVariableName, else empty.
    FString MCPBpScs_NodeParentName(USimpleConstructionScript* SCS, USCS_Node* Node)
    {
        if (!SCS || !Node) { return FString(); }
        if (USCS_Node* Parent = SCS->FindParentNode(Node))       // VERIFY vs engine source: FindParentNode walks GetAllNodes()->ChildNodes
        {
            return Parent->GetVariableName().ToString();
        }
        if (Node->ParentComponentOrVariableName != NAME_None)    // inherited (parent-BP) or native parent
        {
            return Node->ParentComponentOrVariableName.ToString();
        }
        return FString();
    }

    // Child variable names of a node (direct children only).
    TArray<TSharedPtr<FJsonValue>> MCPBpScs_ChildNames(USCS_Node* Node)
    {
        TArray<TSharedPtr<FJsonValue>> Out;
        if (!Node) { return Out; }
        for (USCS_Node* Child : Node->GetChildNodes())           // VERIFY vs engine source: USCS_Node::GetChildNodes()
        {
            if (Child)
            {
                Out.Add(MakeShared<FJsonValueString>(Child->GetVariableName().ToString()));
            }
        }
        return Out;
    }

    // Crash-safe template summary: NO ExportText of arbitrary props (that is the reader-library's
    // heavy/fragile path). Only typed-safe reads: class, scene-ness, and for scene comps the relative
    // transform + attach socket + mobility. This is what the attach-hierarchy consumer actually needs.
    void MCPBpScs_TemplateSummary(USCS_Node* Node, const TSharedRef<FJsonObject>& Out)
    {
        UActorComponent* Template = Node ? Node->ComponentTemplate : nullptr;   // VERIFY vs engine source: USCS_Node::ComponentTemplate
        if (!Template)
        {
            Out->SetBoolField(TEXT("has_template"), false);
            return;
        }
        Out->SetBoolField(TEXT("has_template"), true);
        Out->SetStringField(TEXT("template_name"), Template->GetName());

        USceneComponent* Scene = Cast<USceneComponent>(Template);
        Out->SetBoolField(TEXT("is_scene_component"), Scene != nullptr);
        if (Scene)
        {
            const FVector Loc = Scene->GetRelativeLocation();
            const FRotator Rot = Scene->GetRelativeRotation();
            const FVector Scale = Scene->GetRelativeScale3D();
            TArray<TSharedPtr<FJsonValue>> L, R, S;
            L.Add(MakeShared<FJsonValueNumber>(Loc.X)); L.Add(MakeShared<FJsonValueNumber>(Loc.Y)); L.Add(MakeShared<FJsonValueNumber>(Loc.Z));
            R.Add(MakeShared<FJsonValueNumber>(Rot.Pitch)); R.Add(MakeShared<FJsonValueNumber>(Rot.Yaw)); R.Add(MakeShared<FJsonValueNumber>(Rot.Roll));
            S.Add(MakeShared<FJsonValueNumber>(Scale.X)); S.Add(MakeShared<FJsonValueNumber>(Scale.Y)); S.Add(MakeShared<FJsonValueNumber>(Scale.Z));
            Out->SetArrayField(TEXT("relative_location"), L);
            Out->SetArrayField(TEXT("relative_rotation"), R);
            Out->SetArrayField(TEXT("relative_scale"), S);
            const TCHAR* Mob = TEXT("Static");
            switch (Scene->Mobility.GetValue())   // EComponentMobility::Type — template-free to avoid StaticEnum lookup
            {
                case EComponentMobility::Stationary: Mob = TEXT("Stationary"); break;
                case EComponentMobility::Movable:    Mob = TEXT("Movable");    break;
                default:                             Mob = TEXT("Static");     break;
            }
            Out->SetStringField(TEXT("mobility"), Mob);
        }
        if (Node->AttachToName != NAME_None)                     // socket/bone this node attaches to on its parent
        {
            Out->SetStringField(TEXT("attach_socket"), Node->AttachToName.ToString());
        }
    }

    // Apply one bare JSON value to one FProperty at ValuePtr — typed fast-paths (mirror of the
    // reader in reverse) + ImportText_Direct universal fallback. Compact clone of
    // MCPReflectionLibrary.cpp::EqsApplyJsonToProperty (that one is static in a different TU).
    bool MCPBpScs_ApplyJsonToProperty(FProperty* Prop, void* ValuePtr, UObject* Owner,
                                      const TSharedPtr<FJsonValue>& V, FString& OutErr)
    {
        if (!Prop || !ValuePtr || !V.IsValid()) { OutErr = TEXT("null prop/value"); return false; }

        if (FBoolProperty* BoolP = CastField<FBoolProperty>(Prop))
        {
            BoolP->SetPropertyValue(ValuePtr, V->AsBool());
            return true;
        }
        if (FEnumProperty* EnumP = CastField<FEnumProperty>(Prop))
        {
            FNumericProperty* U = EnumP->GetUnderlyingProperty();
            UEnum* Enum = EnumP->GetEnum();
            int64 Val = 0;
            if (V->Type == EJson::String && Enum)
            {
                Val = Enum->GetValueByNameString(V->AsString());
                if (Val == INDEX_NONE) { OutErr = FString::Printf(TEXT("bad enum name '%s'"), *V->AsString()); return false; }
            }
            else { Val = (int64)V->AsNumber(); }
            if (U) { U->SetIntPropertyValue(ValuePtr, Val); return true; }
            OutErr = TEXT("enum has no underlying"); return false;
        }
        if (FByteProperty* ByteP = CastField<FByteProperty>(Prop))   // incl. TEnumAsByte
        {
            if (V->Type == EJson::String && ByteP->Enum)
            {
                const int64 EV = ByteP->Enum->GetValueByNameString(V->AsString());
                if (EV == INDEX_NONE) { OutErr = FString::Printf(TEXT("bad enum name '%s'"), *V->AsString()); return false; }
                ByteP->SetPropertyValue(ValuePtr, (uint8)EV);
                return true;
            }
            ByteP->SetPropertyValue(ValuePtr, (uint8)V->AsNumber());
            return true;
        }
        if (FNumericProperty* NumP = CastField<FNumericProperty>(Prop))
        {
            if (NumP->IsFloatingPoint()) { NumP->SetFloatingPointPropertyValue(ValuePtr, V->AsNumber()); }
            else { NumP->SetIntPropertyValue(ValuePtr, (int64)V->AsNumber()); }
            return true;
        }
        if (FStrProperty* StrP = CastField<FStrProperty>(Prop))
        {
            StrP->SetPropertyValue(ValuePtr, V->AsString());
            return true;
        }
        if (FNameProperty* NameP = CastField<FNameProperty>(Prop))
        {
            NameP->SetPropertyValue(ValuePtr, FName(*V->AsString()));
            return true;
        }
        if (FTextProperty* TextP = CastField<FTextProperty>(Prop))
        {
            TextP->SetPropertyValue(ValuePtr, FText::FromString(V->AsString()));
            return true;
        }
        if (FObjectPropertyBase* ObjP = CastField<FObjectPropertyBase>(Prop))
        {
            const FString PathStr = V->AsString();
            UObject* Obj = (PathStr.IsEmpty() || PathStr == TEXT("None"))
                ? nullptr
                : StaticLoadObject(ObjP->PropertyClass, nullptr, *PathStr);
            ObjP->SetObjectPropertyValue(ValuePtr, Obj);
            return true;
        }

        // Struct/array/map/etc.: universal text import (caller passes a UE ExportText string as a JSON string).
        FString Text;
        if (V->Type == EJson::String) { Text = V->AsString(); }
        else if (V->Type == EJson::Boolean) { Text = V->AsBool() ? TEXT("true") : TEXT("false"); }
        else if (V->Type == EJson::Number)
        {
            const double D = V->AsNumber();
            Text = (D == FMath::TruncToDouble(D)) ? FString::Printf(TEXT("%lld"), (int64)D) : FString::SanitizeFloat(D);
        }
        else { OutErr = TEXT("unsupported JSON value for struct/array prop (pass a UE ExportText string)"); return false; }

        const TCHAR* Result = Prop->ImportText_Direct(*Text, ValuePtr, Owner, PPF_None, nullptr); // VERIFY vs engine source: FProperty::ImportText_Direct (UE5.1+)
        if (Result == nullptr) { OutErr = FString::Printf(TEXT("ImportText failed for '%s'"), *Text); return false; }
        return true;
    }

    // Bounded, crash-conservative NON-DEFAULT template-property snapshot for the DELETE inverse.
    // Only top-level SIMPLE-typed props (bool/numeric/enum/byte/str/name/text/object-ref/struct) whose
    // value differs from the component-class CDO are exported (single-prop ExportTextItem_Direct — the
    // same call SetEnvQueryNodeProperty/the reader use for one property). Arrays/maps/sets/delegates are
    // INTENTIONALLY skipped (their deep export is the fragile path). Capped at 64 entries. LOSSY —
    // documented on handler #4. Returns a {propName: exportedString} object.
    TSharedRef<FJsonObject> MCPBpScs_SnapshotTemplateProps(UActorComponent* Template)
    {
        TSharedRef<FJsonObject> Snap = MakeShared<FJsonObject>();
        if (!Template) { return Snap; }
        UClass* Cls = Template->GetClass();
        UObject* CDO = Cls ? Cls->GetDefaultObject() : nullptr;  // VERIFY vs engine source: UClass::GetDefaultObject()
        int32 Count = 0;
        for (TFieldIterator<FProperty> It(Cls); It && Count < 64; ++It)
        {
            FProperty* Prop = *It;
            if (!Prop) { continue; }
            // Skip anything we won't faithfully re-import, transient/deprecated, and editor-only bookkeeping.
            if (Prop->HasAnyPropertyFlags(CPF_Transient | CPF_Deprecated | CPF_EditorOnly)) { continue; }
            const bool bSimple =
                Prop->IsA<FBoolProperty>() || Prop->IsA<FNumericProperty>() || Prop->IsA<FEnumProperty>() ||
                Prop->IsA<FByteProperty>() || Prop->IsA<FStrProperty>() || Prop->IsA<FNameProperty>() ||
                Prop->IsA<FTextProperty>() || Prop->IsA<FObjectPropertyBase>() || Prop->IsA<FStructProperty>();
            if (!bSimple) { continue; }

            const void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Template);
            const void* DefPtr = CDO ? Prop->ContainerPtrToValuePtr<void>(CDO) : nullptr;
            if (DefPtr && Prop->Identical(ValuePtr, DefPtr, PPF_None)) { continue; } // VERIFY vs engine source: FProperty::Identical (non-default only)

            FString Exported;
            // Default=nullptr => full-value export, so the string fully specifies the value regardless of the
            // fresh template's starting state on re-add (avoids partial-struct deltas).
            Prop->ExportTextItem_Direct(Exported, ValuePtr, /*Default*/ nullptr, Template, PPF_None); // VERIFY vs engine source: ExportTextItem_Direct signature (UE5.1+)
            if (!Exported.IsEmpty())
            {
                Snap->SetStringField(Prop->GetName(), Exported);
                ++Count;
            }
        }
        return Snap;
    }

    // Common post-write finalize: mark structurally modified + compile (per the task) + dirty package.
    void MCPBpScs_Finalize(UBlueprint* Blueprint)
    {
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint); // VERIFY vs engine source: UNREALED_API
        FKismetEditorUtilities::CompileBlueprint(Blueprint);                    // VERIFY vs engine source
        if (UPackage* Pkg = Blueprint->GetOutermost())
        {
            Pkg->MarkPackageDirty();
        }
    }
#endif // WITH_EDITOR
}

// ============================================================================================
// 1) READ: serialize the SCS node tree.
// ============================================================================================
FString UMCPReflectionLibrary::GetBlueprintSCSJson(const FString& BlueprintPath)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;  // VERIFY vs engine source: UBlueprint::SimpleConstructionScript
    if (!SCS)
    {
        return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript (not an Actor-based Blueprint?)"), *Blueprint->GetName()));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetStringField(TEXT("path"), Blueprint->GetPathName());
    Root->SetStringField(TEXT("parent_class"), Blueprint->ParentClass ? Blueprint->ParentClass->GetPathName() : TEXT("None"));

    USCS_Node* DefaultRoot = SCS->GetDefaultSceneRootNode();               // VERIFY vs engine source: GetDefaultSceneRootNode()
    USCS_Node* SceneRootNode = nullptr;
    SCS->GetSceneRootComponentTemplate(false, &SceneRootNode);            // VERIFY vs engine source: GetSceneRootComponentTemplate(bShouldUseDefaultRoot, OutSCSNode)

    TArray<TSharedPtr<FJsonValue>> Nodes;
    for (USCS_Node* Node : SCS->GetAllNodes())                            // VERIFY vs engine source: GetAllNodes() (WITH_EDITOR)
    {
        if (!Node) { continue; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), Node->GetVariableName().ToString());
        J->SetStringField(TEXT("component_class"), Node->ComponentClass ? Node->ComponentClass->GetPathName() : TEXT("None")); // VERIFY vs engine source: USCS_Node::ComponentClass
        J->SetStringField(TEXT("parent_name"), MCPBpScs_NodeParentName(SCS, Node));
        J->SetBoolField(TEXT("is_root"), Node->IsRootNode());             // VERIFY vs engine source: USCS_Node::IsRootNode()
        J->SetBoolField(TEXT("is_default_scene_root"), Node == DefaultRoot);
        J->SetBoolField(TEXT("is_scene_root"), Node == SceneRootNode);
        J->SetBoolField(TEXT("parent_is_native"), Node->bIsParentComponentNative); // VERIFY vs engine source: USCS_Node::bIsParentComponentNative
        J->SetArrayField(TEXT("child_names"), MCPBpScs_ChildNames(Node));
        MCPBpScs_TemplateSummary(Node, J);
        Nodes.Add(MakeShared<FJsonValueObject>(J));
    }
    Root->SetArrayField(TEXT("nodes"), Nodes);
    Root->SetNumberField(TEXT("node_count"), Nodes.Num());
    Root->SetStringField(TEXT("scene_root"), SceneRootNode ? SceneRootNode->GetVariableName().ToString() : FString());
    return MCPBpScs_Serialize(Root);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 2) WRITE: add a component node (root, or child of ParentComponentName). Inverse: delete by name.
// ============================================================================================
FString UMCPReflectionLibrary::AddComponentToBlueprintJson(const FString& BlueprintPath, const FString& ComponentClass,
                                                           const FString& ComponentName, const FString& ParentComponentName)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
    if (!SCS) { return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript"), *Blueprint->GetName())); }

    // HARD GUARD: CreateNode() has check(NewComponentClass->IsChildOf(UActorComponent::StaticClass())).
    UClass* CompClass = MCPBpScs_ResolveComponentClass(ComponentClass);
    if (!CompClass)
    {
        return MCPBpScs_Error(FString::Printf(TEXT("could not resolve component class '%s' (not found, abstract, or not a UActorComponent)"), *ComponentClass));
    }

    // A BPGC must exist for CreateNode()/SetParent() (ensure() inside CreateNode). Compiled asset => present.
    if (!Cast<UBlueprintGeneratedClass>(Blueprint->GeneratedClass))
    {
        return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no BlueprintGeneratedClass (compile it first)"), *Blueprint->GetName()));
    }

    // Resolve the requested parent (if any) BEFORE creating anything.
    USCS_Node* ParentNode = nullptr;
    const bool bWantParent = !ParentComponentName.IsEmpty();
    if (bWantParent)
    {
        ParentNode = SCS->FindSCSNode(FName(*ParentComponentName));       // VERIFY vs engine source: FindSCSNode(FName)
        if (!ParentNode)
        {
            return MCPBpScs_Error(FString::Printf(TEXT("parent component '%s' not found in SCS"), *ParentComponentName));
        }
        // A non-scene ActorComponent cannot have scene children attached under it.
        if (ParentNode->ComponentTemplate && !ParentNode->ComponentTemplate->IsA<USceneComponent>()
            && CompClass->IsChildOf(USceneComponent::StaticClass()))
        {
            return MCPBpScs_Error(FString::Printf(TEXT("parent '%s' is not a SceneComponent; cannot attach a scene component under it"), *ParentComponentName));
        }
    }

    const FName DesiredName = ComponentName.IsEmpty() ? NAME_None : FName(*ComponentName);
    USCS_Node* NewNode = SCS->CreateNode(CompClass, DesiredName);         // VERIFY vs engine source: CreateNode(UClass*, FName) — name may be auto-suffixed
    if (!NewNode)
    {
        return MCPBpScs_Error(TEXT("CreateNode returned null"));
    }

    if (ParentNode)
    {
        // Same-SCS tree parenting: AddChildNode ONLY (the ChildNodes tree IS the parent linkage; see
        // SubobjectDataSubsystem::AttachSubobject same-SCS branch). Do NOT SetParent (that is for
        // inherited/native parents only).
        ParentNode->AddChildNode(NewNode);                               // VERIFY vs engine source: USCS_Node::AddChildNode (adds to ChildNodes + AllNodes)
    }
    else
    {
        SCS->AddNode(NewNode);                                           // VERIFY vs engine source: AddNode (root set + AllNodes + ValidateSceneRootNodes)
    }

    const FString CreatedName = NewNode->GetVariableName().ToString();
    MCPBpScs_Finalize(Blueprint);

    TSharedRef<FJsonObject> Out = MakeShared<FJsonObject>();
    Out->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Out->SetStringField(TEXT("component"), CreatedName);
    Out->SetStringField(TEXT("component_class"), CompClass->GetPathName());
    Out->SetStringField(TEXT("parent"), ParentNode ? ParentNode->GetVariableName().ToString() : FString());
    Out->SetBoolField(TEXT("is_root"), NewNode->IsRootNode());
    Out->SetBoolField(TEXT("added"), true);
    return MCPBpScs_Serialize(Out);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 3) WRITE: set an FProperty on a component's ComponentTemplate archetype. Captures prior (ExportText).
// ============================================================================================
FString UMCPReflectionLibrary::SetBlueprintComponentPropertyJson(const FString& BlueprintPath, const FString& ComponentName,
                                                                 const FString& PropertyName, const FString& ValueJson)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
    if (!SCS) { return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript"), *Blueprint->GetName())); }

    USCS_Node* Node = SCS->FindSCSNode(FName(*ComponentName));
    if (!Node) { return MCPBpScs_Error(FString::Printf(TEXT("component '%s' not found in SCS"), *ComponentName)); }

    UActorComponent* Template = Node->ComponentTemplate;
    if (!Template) { return MCPBpScs_Error(FString::Printf(TEXT("component '%s' has no ComponentTemplate"), *ComponentName)); }

    FProperty* Prop = FindFProperty<FProperty>(Template->GetClass(), *PropertyName); // VERIFY vs engine source: FindFProperty
    if (!Prop)
    {
        return MCPBpScs_Error(FString::Printf(TEXT("component '%s' (%s) has no property '%s'"), *ComponentName, *Template->GetClass()->GetName(), *PropertyName));
    }

    // Parse ValueJson as a JSON value; UE's reader rejects a BARE scalar/string at document root, so callers
    // pass an array-wrapped value ([1.5] / [true] / ["EnumName"] / ["(X=1,Y=2,Z=3)"]) — unwrap a single element.
    TSharedPtr<FJsonValue> V;
    TSharedRef<TJsonReader<>> JReader = TJsonReaderFactory<>::Create(ValueJson);
    if (!FJsonSerializer::Deserialize(JReader, V) || !V.IsValid())
    {
        return MCPBpScs_Error(TEXT("invalid value JSON (pass an array-wrapped value, e.g. [1.5] or [\"(X=1,Y=2,Z=3)\"])"));
    }
    if (V->Type == EJson::Array)
    {
        const TArray<TSharedPtr<FJsonValue>>& Arr = V->AsArray();
        if (Arr.Num() == 1 && Arr[0].IsValid()) { V = Arr[0]; }
    }

    // Capture PRIOR value via single-property ExportText (for undo).
    void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Template);
    FString PrevExported;
    Prop->ExportTextItem_Direct(PrevExported, ValuePtr, /*Default*/ nullptr, /*Parent*/ Template, PPF_None); // VERIFY vs engine source: ExportTextItem_Direct

    Template->Modify();
    Template->PreEditChange(Prop);
    FString ApplyErr;
    if (!MCPBpScs_ApplyJsonToProperty(Prop, ValuePtr, Template, V, ApplyErr))
    {
        return MCPBpScs_Error(ApplyErr.IsEmpty() ? TEXT("failed to set property") : ApplyErr);
    }
    {
        FPropertyChangedEvent Evt(Prop);
        Template->PostEditChangeProperty(Evt);
    }
    MCPBpScs_Finalize(Blueprint);

    TSharedRef<FJsonObject> Out = MakeShared<FJsonObject>();
    Out->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Out->SetStringField(TEXT("component"), ComponentName);
    Out->SetStringField(TEXT("property"), PropertyName);
    Out->SetBoolField(TEXT("set"), true);
    Out->SetStringField(TEXT("prev"), PrevExported);   // ExportText string; inverse re-applies via ValueJson=[prev]
    return MCPBpScs_Serialize(Out);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 4) WRITE: delete a component (RemoveNodeAndPromoteChildren). Captures class+parent+prop snapshot.
//    LOSSY inverse: re-add re-creates the node + re-applies the prop snapshot, but promoted children
//    do NOT un-promote back under it (RemoveNodeAndPromoteChildren moved them to the parent).
// ============================================================================================
FString UMCPReflectionLibrary::DeleteBlueprintComponentJson(const FString& BlueprintPath, const FString& ComponentName)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
    if (!SCS) { return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript"), *Blueprint->GetName())); }

    USCS_Node* Node = SCS->FindSCSNode(FName(*ComponentName));
    if (!Node) { return MCPBpScs_Error(FString::Printf(TEXT("component '%s' not found in SCS"), *ComponentName)); }

    if (Node == SCS->GetDefaultSceneRootNode())
    {
        return MCPBpScs_Error(TEXT("refusing to delete the DefaultSceneRoot node (the engine manages it)"));
    }

    // Capture inverse state BEFORE removal.
    const FString ClassPath = Node->ComponentClass ? Node->ComponentClass->GetPathName() : FString();
    const FString ParentName = MCPBpScs_NodeParentName(SCS, Node);
    const bool bWasRoot = Node->IsRootNode();
    TArray<TSharedPtr<FJsonValue>> Children = MCPBpScs_ChildNames(Node);
    TSharedRef<FJsonObject> Snapshot = MCPBpScs_SnapshotTemplateProps(Node->ComponentTemplate);

    SCS->RemoveNodeAndPromoteChildren(Node);                             // VERIFY vs engine source: RemoveNodeAndPromoteChildren
    MCPBpScs_Finalize(Blueprint);

    TSharedRef<FJsonObject> Out = MakeShared<FJsonObject>();
    Out->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Out->SetStringField(TEXT("component"), ComponentName);
    Out->SetBoolField(TEXT("removed"), true);
    Out->SetStringField(TEXT("component_class"), ClassPath);             // inverse re-add class
    Out->SetStringField(TEXT("parent"), ParentName);                    // inverse re-add parent
    Out->SetBoolField(TEXT("was_root"), bWasRoot);
    Out->SetArrayField(TEXT("promoted_children"), Children);           // children promoted to parent (not restored by undo)
    Out->SetObjectField(TEXT("prop_snapshot"), Snapshot);             // {name: exportText} of non-default simple props
    return MCPBpScs_Serialize(Out);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 5) WRITE: reparent a component under NewParentName (empty => promote to root). Captures prior parent.
// ============================================================================================
FString UMCPReflectionLibrary::ReparentBlueprintComponentJson(const FString& BlueprintPath, const FString& ComponentName,
                                                              const FString& NewParentName)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
    if (!SCS) { return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript"), *Blueprint->GetName())); }

    USCS_Node* Node = SCS->FindSCSNode(FName(*ComponentName));
    if (!Node) { return MCPBpScs_Error(FString::Printf(TEXT("component '%s' not found in SCS"), *ComponentName)); }
    if (Node == SCS->GetDefaultSceneRootNode())
    {
        return MCPBpScs_Error(TEXT("refusing to reparent the DefaultSceneRoot node"));
    }

    // Capture prior parent (empty => was a root).
    const FString PriorParent = MCPBpScs_NodeParentName(SCS, Node);

    USCS_Node* NewParent = nullptr;
    const bool bToRoot = NewParentName.IsEmpty();
    if (!bToRoot)
    {
        NewParent = SCS->FindSCSNode(FName(*NewParentName));
        if (!NewParent) { return MCPBpScs_Error(FString::Printf(TEXT("new parent '%s' not found in SCS"), *NewParentName)); }
        if (NewParent == Node) { return MCPBpScs_Error(TEXT("cannot parent a component to itself")); }
        if (NewParent->IsChildOf(Node))                                 // VERIFY vs engine source: USCS_Node::IsChildOf — refuse a cycle
        {
            return MCPBpScs_Error(FString::Printf(TEXT("'%s' is a descendant of '%s'; reparenting would create a cycle"), *NewParentName, *ComponentName));
        }
        // Only a scene component can attach under another scene component; a scene node needs a scene parent.
        if (Node->ComponentTemplate && Node->ComponentTemplate->IsA<USceneComponent>()
            && NewParent->ComponentTemplate && !NewParent->ComponentTemplate->IsA<USceneComponent>())
        {
            return MCPBpScs_Error(FString::Printf(TEXT("new parent '%s' is not a SceneComponent"), *NewParentName));
        }
    }

    const bool bAlreadyThere = bToRoot ? Node->IsRootNode() : (MCPBpScs_NodeParentName(SCS, Node) == NewParentName);
    if (bAlreadyThere)
    {
        // Already parented there (or already a root) — no-op success.
        TSharedRef<FJsonObject> NoOp = MakeShared<FJsonObject>();
        NoOp->SetStringField(TEXT("blueprint"), Blueprint->GetName());
        NoOp->SetStringField(TEXT("component"), ComponentName);
        NoOp->SetStringField(TEXT("new_parent"), NewParentName);
        NoOp->SetStringField(TEXT("prior_parent"), PriorParent);
        NoOp->SetBoolField(TEXT("reparented"), false);
        return MCPBpScs_Serialize(NoOp);
    }

    // Detach from current location (root branch clears parent refs; child branch drops from parent's
    // ChildNodes + AllNodes). Then re-attach. Mirrors SubobjectDataSubsystem Detach->Attach.
    SCS->RemoveNode(Node, /*bValidateSceneRootNodes=*/false);           // VERIFY vs engine source: RemoveNode(node, false)
    if (bToRoot)
    {
        SCS->AddNode(Node);                                             // VERIFY vs engine source: AddNode (promote to root)
    }
    else
    {
        NewParent->AddChildNode(Node);                                  // VERIFY vs engine source: AddChildNode (same-SCS tree parent)
        // Same-SCS child: parent linkage is the tree; keep the inherited/native parent refs clear.
        Node->Modify();
        Node->bIsParentComponentNative = false;
        Node->ParentComponentOrVariableName = NAME_None;
        Node->ParentComponentOwnerClassName = NAME_None;
    }
    MCPBpScs_Finalize(Blueprint);

    TSharedRef<FJsonObject> Out = MakeShared<FJsonObject>();
    Out->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Out->SetStringField(TEXT("component"), ComponentName);
    Out->SetStringField(TEXT("new_parent"), NewParentName);
    Out->SetStringField(TEXT("prior_parent"), PriorParent);
    Out->SetBoolField(TEXT("reparented"), true);
    return MCPBpScs_Serialize(Out);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 6) WRITE: promote a scene component to the scene root (old root becomes its child). Captures prior root.
//    Mirrors USubobjectDataSubsystem::MakeNewSceneRoot at the raw-SCS level (keeps the old root reversible
//    rather than deleting a default scene root).
// ============================================================================================
FString UMCPReflectionLibrary::SetBlueprintRootComponentJson(const FString& BlueprintPath, const FString& ComponentName)
{
#if WITH_EDITOR
    UBlueprint* Blueprint = MCPBpScs_LoadBlueprint(BlueprintPath);
    if (!Blueprint) { return MCPBpScs_Error(FString::Printf(TEXT("could not load blueprint '%s'"), *BlueprintPath)); }

    USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
    if (!SCS) { return MCPBpScs_Error(FString::Printf(TEXT("blueprint '%s' has no SimpleConstructionScript"), *Blueprint->GetName())); }

    USCS_Node* Node = SCS->FindSCSNode(FName(*ComponentName));
    if (!Node) { return MCPBpScs_Error(FString::Printf(TEXT("component '%s' not found in SCS"), *ComponentName)); }

    // Only a scene component can be the scene root.
    if (!Node->ComponentTemplate || !Node->ComponentTemplate->IsA<USceneComponent>())
    {
        return MCPBpScs_Error(FString::Printf(TEXT("component '%s' is not a SceneComponent; only scene components can be the root"), *ComponentName));
    }

    // Find the CURRENT scene-root SCS node.
    USCS_Node* OldRootNode = nullptr;
    USceneComponent* SceneRootTemplate = SCS->GetSceneRootComponentTemplate(false, &OldRootNode); // VERIFY vs engine source
    if (!OldRootNode)
    {
        // No non-default SCS scene root — fall back to the DefaultSceneRoot node if it's currently a root.
        USCS_Node* DefRoot = SCS->GetDefaultSceneRootNode();
        if (DefRoot && SCS->GetRootNodes().Contains(DefRoot))
        {
            OldRootNode = DefRoot;
        }
    }
    if (!OldRootNode)
    {
        if (SceneRootTemplate)
        {
            return MCPBpScs_Error(FString::Printf(TEXT("current scene root is a native/inherited component '%s' with no SCS node; parent '%s' under it via reparent instead"),
                *SceneRootTemplate->GetName(), *ComponentName));
        }
        return MCPBpScs_Error(TEXT("no existing SCS scene-root node to replace"));
    }
    if (OldRootNode == Node)
    {
        TSharedRef<FJsonObject> NoOp = MakeShared<FJsonObject>();
        NoOp->SetStringField(TEXT("blueprint"), Blueprint->GetName());
        NoOp->SetStringField(TEXT("component"), ComponentName);
        NoOp->SetStringField(TEXT("prior_root"), OldRootNode->GetVariableName().ToString());
        NoOp->SetBoolField(TEXT("changed"), false);
        return MCPBpScs_Serialize(NoOp);
    }
    if (OldRootNode->IsChildOf(Node))
    {
        // Old root is a descendant of Node — swapping would be ill-defined. Refuse.
        return MCPBpScs_Error(FString::Printf(TEXT("current root '%s' is a descendant of '%s'"), *OldRootNode->GetVariableName().ToString(), *ComponentName));
    }

    const FString PriorRoot = OldRootNode->GetVariableName().ToString();
    const FString NodePriorParent = MCPBpScs_NodeParentName(SCS, Node); // so a faithful inverse can restore Node's original parent

    // Reset the new root's relative transform (the editor does this when making a new scene root).
    if (USceneComponent* NewRootTemplate = Cast<USceneComponent>(Node->ComponentTemplate))
    {
        NewRootTemplate->Modify();
        NewRootTemplate->SetRelativeLocation(FVector::ZeroVector);
        NewRootTemplate->SetRelativeRotation(FRotator::ZeroRotator);
        Node->Modify();
        Node->AttachToName = NAME_None;
    }

    // Sequence (mirrors MakeNewSceneRoot): detach Node; drop old root from the root set; add Node to the
    // root set; re-attach old root as a child of Node. Order keeps ValidateSceneRootNodes (fired inside
    // AddNode) seeing exactly one scene root.
    SCS->RemoveNode(Node, /*bValidateSceneRootNodes=*/false);
    SCS->RemoveNode(OldRootNode, /*bValidateSceneRootNodes=*/false);
    SCS->AddNode(Node);
    Node->AddChildNode(OldRootNode);                                    // VERIFY vs engine source: AddChildNode (old root -> child of new root)
    // Old root is now a same-SCS tree child; keep its parent refs clear.
    OldRootNode->Modify();
    OldRootNode->bIsParentComponentNative = false;
    OldRootNode->ParentComponentOrVariableName = NAME_None;
    OldRootNode->ParentComponentOwnerClassName = NAME_None;

    MCPBpScs_Finalize(Blueprint);

    TSharedRef<FJsonObject> Out = MakeShared<FJsonObject>();
    Out->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Out->SetStringField(TEXT("component"), ComponentName);
    Out->SetStringField(TEXT("prior_root"), PriorRoot);
    Out->SetStringField(TEXT("node_prior_parent"), NodePriorParent);
    Out->SetBoolField(TEXT("changed"), true);
    return MCPBpScs_Serialize(Out);
#else
    return MCPBpScs_Error(TEXT("editor-only"));
#endif
}
