// Clean-room reflection helpers for the UnrealMCP plugin. See MCPReflectionLibrary.h.

#include "MCPReflectionLibrary.h"

#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshSocket.h"

// ---- STAGED C++ #3 includes (all Engine-module public headers) ----
#include "Curves/CurveBase.h"
#include "Curves/CurveFloat.h"
#include "Curves/CurveVector.h"
#include "Curves/CurveLinearColor.h"
#include "Curves/RichCurve.h"
#include "Engine/Texture2D.h"
#include "Engine/TextureDefines.h"
#include "PixelFormat.h"
#include "PhysicsEngine/PhysicsAsset.h"
#include "PhysicsEngine/BodySetup.h"
#include "PhysicsEngine/SkeletalBodySetup.h"
#include "PhysicsEngine/AggregateGeom.h"

// ---- STAGED C++ #4 includes (Wave-3 batch 1: data-asset AUTHORING) ----
// curves: RichCurve.h already included above. structs/enums: UnrealEd (already a dep).
#include "Serialization/JsonReader.h"
#include "StructUtils/UserDefinedStruct.h"  // Windows fix: moved from Engine/ to CoreUObject StructUtils/ in this engine build
#include "Engine/UserDefinedEnum.h"
#include "Kismet2/StructureEditorUtils.h"   // FStructureEditorUtils (UnrealEd module)
#include "UserDefinedStructure/UserDefinedStructEditorData.h"  // Windows fix: full def of FStructVariableDescription (VarName/VarGuid/FriendlyName)
#include "Kismet2/EnumEditorUtils.h"        // FEnumEditorUtils (UnrealEd module)
#include "EdGraph/EdGraphPin.h"             // FEdGraphPinType
#include "EdGraphSchema_K2.h"               // UEdGraphSchema_K2::PC_* (BlueprintGraph module)

// ---- STAGED C++ #5 includes (Niagara authoring; REQUIRES Build.cs += "Niagara","NiagaraEditor") ----
#include "NiagaraSystem.h"                  // UNiagaraSystem (Niagara module)
#include "NiagaraEmitter.h"                 // UNiagaraEmitter
#include "NiagaraEmitterHandle.h"           // FNiagaraEmitterHandle
#include "NiagaraEditorUtilities.h"         // FNiagaraEditorUtilities (NiagaraEditor module) — VERIFY path

// ---- STAGED C++ #6 includes (gameplay-tag authoring; REQUIRES Build.cs += "GameplayTags","GameplayTagsEditor") ----
#include "GameplayTagsManager.h"            // UGameplayTagsManager / FGameplayTagNode (GameplayTags)
#include "GameplayTagsEditorModule.h"       // IGameplayTagsEditorModule (GameplayTagsEditor) — VERIFY path

// ---- STAGED C++ #7 includes (Kismet BP variable/node helper; NO Build.cs change) ----
#include "Engine/Blueprint.h"               // UBlueprint (Engine)
#include "Kismet2/BlueprintEditorUtils.h"   // FBlueprintEditorUtils (UnrealEd)
#include "K2Node_Event.h"                    // UK2Node_Event (BlueprintGraph)

// ---- STAGED C++ #8 includes (widget-tree authoring; REQUIRES Build.cs += "UMG","UMGEditor") ----
#include "WidgetBlueprint.h"                 // UWidgetBlueprint (UMGEditor)
#include "Blueprint/WidgetTree.h"            // UWidgetTree (UMG)
#include "Components/Widget.h"               // UWidget (UMG)
#include "Components/PanelWidget.h"          // UPanelWidget (UMG)

// ---- STAGED C++ #9 includes (BP event-node reader + guid-remove; NO Build.cs change) ----
#include "EdGraph/EdGraph.h"                 // UEdGraph (for GetGraph()->GetName())

// ---- C++ #10 includes (Deeper Niagara) ----
// Windows fix: no separate NiagaraTypeDefinition.h in this build — FNiagaraTypeDefinition /
// FNiagaraVariable all live in NiagaraTypes.h (below).
#include "NiagaraTypes.h"                              // FNiagaraTypeDefinition / FNiagaraVariable / FNiagaraBool / FNiagaraInt32 (Niagara/Public)
#include "NiagaraParameterStore.h"                    // FNiagaraParameterStore — VERIFY path
#include "NiagaraUserRedirectionParameterStore.h"     // FNiagaraUserRedirectionParameterStore — VERIFY path (may be folded into NiagaraParameterStore.h in some builds)
#include "NiagaraRendererProperties.h"            // UNiagaraRendererProperties (base) — VERIFY path
#include "NiagaraSpriteRendererProperties.h"      // UNiagaraSpriteRendererProperties — VERIFY path
#include "NiagaraMeshRendererProperties.h"        // UNiagaraMeshRendererProperties — VERIFY path
#include "NiagaraRibbonRendererProperties.h"      // UNiagaraRibbonRendererProperties — VERIFY path
#include "NiagaraLightRendererProperties.h"       // UNiagaraLightRendererProperties — VERIFY path
#include "NiagaraScript.h"                            // UNiagaraScript, ENiagaraScriptUsage — VERIFY path (Niagara/Public)
#include "NiagaraScriptSource.h"                      // UNiagaraScriptSource (has ->NodeGraph) — NiagaraEditor/Public
#include "NiagaraGraph.h"                             // UNiagaraGraph::FindOutputNode / GetNodesOfClass — NiagaraEditor/Public
#include "NiagaraNodeOutput.h"                        // UNiagaraNodeOutput — NiagaraEditor/Public
#include "NiagaraNodeFunctionCall.h"                  // UNiagaraNodeFunctionCall — NiagaraEditor/Public
#include "ViewModels/Stack/NiagaraStackGraphUtilities.h" // FNiagaraStackGraphUtilities — NiagaraEditor/Public/ViewModels/Stack
#include "AssetRegistry/AssetData.h"                  // FAssetData (AssetRegistry — already transitively available)
#include "UObject/SavePackage.h"   // FSavePackageArgs / FSavePackageResultStruct (matches Materials/Blueprints handlers)
#include "UObject/Package.h"       // UPackage (GetOutermost/GetName) — usually via CoreMinimal, included explicitly
#include "Misc/PackageName.h"      // FPackageName::LongPackageNameToFilename / GetAssetPackageExtension

// ---- C++ #11 includes (BehaviorTree editor-graph; REQUIRES Build.cs += "AIModule","AIGraph","BehaviorTreeEditor") ----
#include "BehaviorTree/BehaviorTree.h"                 // UBehaviorTree (RootNode; BTGraph is WITH_EDITORONLY_DATA UEdGraph*) — VERIFY member access
#include "BehaviorTree/BTCompositeNode.h"              // UBTCompositeNode / FBTCompositeChild (Children, Services) — VERIFY struct members
#include "BehaviorTree/BTTaskNode.h"                   // UBTTaskNode
#include "BehaviorTree/BTDecorator.h"                  // UBTDecorator
#include "BehaviorTree/BTService.h"                    // UBTService
#include "BehaviorTreeGraph.h"                         // UBehaviorTreeGraph::OnCreated/UpdateAsset/CreateBTFromGraph — VERIFY path + BEHAVIORTREEEDITOR_API
#include "BehaviorTreeGraphNode.h"                     // UBehaviorTreeGraphNode::GetInputPin/GetOutputPin — VERIFY export
#include "BehaviorTreeGraphNode_Root.h"                // UBehaviorTreeGraphNode_Root — VERIFY
#include "BehaviorTreeGraphNode_Composite.h"           // UBehaviorTreeGraphNode_Composite — VERIFY
#include "BehaviorTreeGraphNode_Task.h"                // UBehaviorTreeGraphNode_Task — VERIFY
#include "BehaviorTreeGraphNode_Decorator.h"           // UBehaviorTreeGraphNode_Decorator — VERIFY
#include "BehaviorTreeGraphNode_Service.h"             // UBehaviorTreeGraphNode_Service — VERIFY
#include "EdGraphSchema_BehaviorTree.h"                // UEdGraphSchema_BehaviorTree — VERIFY path + export
#include "AIGraphTypes.h"                              // FGraphNodeClassData — VERIFY path (AIGraph/Classes) + AIGRAPH_API

// ---- C++ #12 includes (deferred-reflection READER batch: EQS config + StateTree registry + CR VM/pins) ----
// EQS (no Build.cs change — AIModule already linked):
#include "EnvironmentQuery/EnvQuery.h"                 // UEnvQuery (AIModule) — VERIFY path (EnvironmentQuery/EnvQuery.h)
// StateTree native-node registry (REQUIRES Build.cs += "StateTreeModule" — runtime; RIGVM-like export, low risk):
#include "StateTree.h"                                 // UStateTree (StateTreeModule/Public) — VERIFY path
#include "StateTreeTaskBase.h"                         // FStateTreeTaskBase — VERIFY path
#include "StateTreeEvaluatorBase.h"                    // FStateTreeEvaluatorBase — VERIFY path
#include "StateTreeConditionBase.h"                    // FStateTreeConditionBase — VERIFY path
#include "StateTreeConsiderationBase.h"                // FStateTreeConsiderationBase — VERIFY path (probe-confirmed present in this 5.8 build)
#include "UObject/UObjectIterator.h"                   // TObjectIterator<UScriptStruct> (CoreUObject)
// ControlRig compiled-VM + pin schema (REQUIRES Build.cs += "RigVM" — runtime, RIGVM_API, low risk):
#include "RigVMHost.h"                                 // URigVMHost::GetVM/GetExternalVariables (RigVM) — lives in RigVM/Public/ root, NOT RigVMCore/
#include "RigVMCore/RigVM.h"                           // URigVM::GetByteCode/GetStatistics/GetWorkMemory (RigVM) — VERIFY
#include "RigVMCore/RigVMByteCode.h"                   // FRigVMByteCode / ERigVMOpCode (RigVM) — VERIFY
#include "RigVMCore/RigVMStatistics.h"                 // FRigVMStatistics (StaticStruct) (RigVM) — VERIFY (may be folded into RigVM.h)
#include "RigVMCore/RigVMExternalVariable.h"           // FRigVMExternalVariable (RigVM) — VERIFY
#include "RigVMCore/RigVMMemoryStorage.h"              // URigVMMemoryStorage (RigVM) — VERIFY (only for memory_stats; drop if unresolved)
#include "UObject/StructOnScope.h"                     // FStructOnScope (CoreUObject) — for the pin-schema default export
#include "Engine/Blueprint.h"                          // UBlueprint (Engine) — already included above; harmless re-include

// ---- C++ #13 includes (backlog WRITERS: AnimMontage sections + USkeleton sockets/virtual-bones) ----
// Both areas are Engine-module (already linked) -> NO Build.cs change. Reflection APIs (FArrayProperty/
// FScriptArrayHelper/FindFProperty) already available via UObject/UnrealType.h at the top of this file.
#include "Animation/AnimMontage.h"                     // UAnimMontage / FCompositeSection / FAnimLinkableElement (Engine) — VERIFY path
#include "Animation/Skeleton.h"                        // USkeleton / FVirtualBone / GetReferenceSkeleton (Engine) — VERIFY path
#include "Engine/SkeletalMeshSocket.h"                 // USkeletalMeshSocket (Engine) — SocketName/BoneName/Relative* — VERIFY path

// ---- C++ #14 includes (StateTree editor property-BINDINGS reader; REQUIRES Build.cs += "StateTreeEditorModule") ----
// TOP RISK: UStateTreeEditorData may need STATETREEEDITORMODULE_API export (like the Niagara round). StateTree.h
// is already included from C++ #12. FInstancedStruct helpers already available via StructUtils headers.
#include "StateTreeEditorData.h"                       // UStateTreeEditorData (StateTreeEditorModule) — VERIFY path + STATETREEEDITORMODULE_API export
#include "StateTreeState.h"                            // UStateTreeState + FStateTreeEditorNode (StateTreeEditorModule) — VERIFY path (FStateTreeEditorNode may be in StateTreeEditorNode.h)
#include "StateTreePropertyBindings.h"                 // (StateTreeModule) — runtime binding types
#include "StateTreeEditorPropertyBindings.h"           // UE 5.8: FStateTreeEditorPropertyBindings : FPropertyBindingBindingCollection (StateTreeEditorModule)
#include "PropertyBindingBinding.h"                    // UE 5.8: FPropertyBindingBinding::GetSourcePath/GetTargetPath (PropertyBindingUtils)
#include "PropertyBindingPath.h"                       // UE 5.8: FPropertyBindingPath::GetStructID/ToString (PropertyBindingUtils)
#include "StructUtils/InstancedStruct.h"               // FInstancedStruct::GetScriptStruct (CoreUObject StructUtils)

namespace
{
    FString SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    TSharedRef<FJsonObject> ErrorObj(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return Root;
    }

    TArray<TSharedPtr<FJsonValue>> Vec3(double X, double Y, double Z)
    {
        TArray<TSharedPtr<FJsonValue>> A;
        A.Add(MakeShared<FJsonValueNumber>(X));
        A.Add(MakeShared<FJsonValueNumber>(Y));
        A.Add(MakeShared<FJsonValueNumber>(Z));
        return A;
    }

    // ---- STAGED C++ #3 helpers ----
    const TCHAR* InterpModeStr(ERichCurveInterpMode M)
    {
        switch (M)
        {
            case RCIM_Linear:   return TEXT("Linear");
            case RCIM_Constant: return TEXT("Constant");
            case RCIM_Cubic:    return TEXT("Cubic");
            default:            return TEXT("None");
        }
    }

    const TCHAR* TangentModeStr(ERichCurveTangentMode M)
    {
        switch (M)
        {
            case RCTM_Auto:  return TEXT("Auto");
            case RCTM_User:  return TEXT("User");
            case RCTM_Break: return TEXT("Break");
            default:         return TEXT("None");
        }
    }

    void AddCurveChannel(TArray<TSharedPtr<FJsonValue>>& Channels, const FString& Name, const FRichCurve& Curve)
    {
        TSharedRef<FJsonObject> Ch = MakeShared<FJsonObject>();
        Ch->SetStringField(TEXT("name"), Name);
        TArray<TSharedPtr<FJsonValue>> Keys;
        for (const FRichCurveKey& K : Curve.Keys)
        {
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetNumberField(TEXT("time"), K.Time);
            J->SetNumberField(TEXT("value"), K.Value);
            J->SetStringField(TEXT("interp_mode"), InterpModeStr(K.InterpMode));
            J->SetStringField(TEXT("tangent_mode"), TangentModeStr(K.TangentMode));
            J->SetNumberField(TEXT("arrive_tangent"), K.ArriveTangent);
            J->SetNumberField(TEXT("leave_tangent"), K.LeaveTangent);
            Keys.Add(MakeShared<FJsonValueObject>(J));
        }
        Ch->SetNumberField(TEXT("key_count"), Keys.Num());
        Ch->SetArrayField(TEXT("keys"), Keys);
        Channels.Add(MakeShared<FJsonValueObject>(Ch));
    }

    const TCHAR* PixelFormatStr(EPixelFormat F)
    {
        switch (F)
        {
            case PF_A32B32G32R32F: return TEXT("PF_A32B32G32R32F");
            case PF_B8G8R8A8:      return TEXT("PF_B8G8R8A8");
            case PF_G8:            return TEXT("PF_G8");
            case PF_G16:           return TEXT("PF_G16");
            case PF_DXT1:          return TEXT("PF_DXT1");
            case PF_DXT3:          return TEXT("PF_DXT3");
            case PF_DXT5:          return TEXT("PF_DXT5");
            case PF_BC4:           return TEXT("PF_BC4");
            case PF_BC5:           return TEXT("PF_BC5");
            case PF_BC6H:          return TEXT("PF_BC6H");
            case PF_BC7:           return TEXT("PF_BC7");
            case PF_FloatRGB:      return TEXT("PF_FloatRGB");
            case PF_FloatRGBA:     return TEXT("PF_FloatRGBA");
            case PF_R8G8B8A8:      return TEXT("PF_R8G8B8A8");
            case PF_A8R8G8B8:      return TEXT("PF_A8R8G8B8");
            case PF_R8G8:          return TEXT("PF_R8G8");
            case PF_A16B16G16R16:  return TEXT("PF_A16B16G16R16");
            case PF_G16R16:        return TEXT("PF_G16R16");
            case PF_R16F:          return TEXT("PF_R16F");
            case PF_R32_FLOAT:     return TEXT("PF_R32_FLOAT");
            default:               return TEXT("PF_Other");
        }
    }

#if WITH_EDITORONLY_DATA
    const TCHAR* SourceFormatStr(ETextureSourceFormat F)
    {
        switch (F)
        {
            case TSF_G8:      return TEXT("TSF_G8");
            case TSF_BGRA8:   return TEXT("TSF_BGRA8");
            case TSF_BGRE8:   return TEXT("TSF_BGRE8");
            case TSF_RGBA16:  return TEXT("TSF_RGBA16");
            case TSF_RGBA16F: return TEXT("TSF_RGBA16F");
            case TSF_G16:     return TEXT("TSF_G16");
            case TSF_R16F:    return TEXT("TSF_R16F");
            default:          return TEXT("TSF_Other");
        }
    }
#endif

    // ---- STAGED C++ #4 helpers (Wave-3 batch 1: data-asset authoring) ----
    ERichCurveInterpMode ParseInterp(const FString& S)
    {
        if (S.Equals(TEXT("Linear"), ESearchCase::IgnoreCase))   return RCIM_Linear;
        if (S.Equals(TEXT("Constant"), ESearchCase::IgnoreCase)) return RCIM_Constant;
        return RCIM_Cubic; // default / "Cubic"
    }

    ERichCurveTangentMode ParseTangent(const FString& S)
    {
        if (S.Equals(TEXT("User"), ESearchCase::IgnoreCase))  return RCTM_User;
        if (S.Equals(TEXT("Break"), ESearchCase::IgnoreCase)) return RCTM_Break;
        return RCTM_Auto; // default / "Auto"
    }

    // Address of the FRichCurve for a given channel index (Float=1, Vector=3, LinearColor=4).
    FRichCurve* GetChannelPtr(UCurveBase* Curve, int32 Index)
    {
        if (UCurveFloat* CF = Cast<UCurveFloat>(Curve))
        {
            return (Index == 0) ? &CF->FloatCurve : nullptr;
        }
        if (UCurveVector* CV = Cast<UCurveVector>(Curve))
        {
            return (Index >= 0 && Index < 3) ? &CV->FloatCurves[Index] : nullptr;
        }
        if (UCurveLinearColor* CC = Cast<UCurveLinearColor>(Curve))
        {
            return (Index >= 0 && Index < 4) ? &CC->FloatCurves[Index] : nullptr;
        }
        return nullptr;
    }

    // Map a friendly type string -> FEdGraphPinType for UserDefinedStruct fields.
    bool BuildPinType(const FString& TypeName, FEdGraphPinType& Out)
    {
        const FString L = TypeName.ToLower();
        Out = FEdGraphPinType();
        Out.ContainerType = EPinContainerType::None;
        if (L == TEXT("bool") || L == TEXT("boolean")) { Out.PinCategory = UEdGraphSchema_K2::PC_Boolean; return true; }
        if (L == TEXT("byte"))                          { Out.PinCategory = UEdGraphSchema_K2::PC_Byte; return true; }
        if (L == TEXT("int") || L == TEXT("int32") || L == TEXT("integer")) { Out.PinCategory = UEdGraphSchema_K2::PC_Int; return true; }
        if (L == TEXT("int64"))                         { Out.PinCategory = UEdGraphSchema_K2::PC_Int64; return true; }
        if (L == TEXT("float") || L == TEXT("double") || L == TEXT("real"))
        {
            Out.PinCategory = UEdGraphSchema_K2::PC_Real;
            Out.PinSubCategory = UEdGraphSchema_K2::PC_Double;
            return true;
        }
        if (L == TEXT("name"))   { Out.PinCategory = UEdGraphSchema_K2::PC_Name; return true; }
        if (L == TEXT("string")) { Out.PinCategory = UEdGraphSchema_K2::PC_String; return true; }
        if (L == TEXT("text"))   { Out.PinCategory = UEdGraphSchema_K2::PC_Text; return true; }
        auto AsStruct = [&Out](UScriptStruct* SS) -> bool
        {
            Out.PinCategory = UEdGraphSchema_K2::PC_Struct;
            Out.PinSubCategoryObject = SS;
            return SS != nullptr;
        };
        if (L == TEXT("vector"))       { return AsStruct(TBaseStructure<FVector>::Get()); }
        if (L == TEXT("vector2d"))     { return AsStruct(TBaseStructure<FVector2D>::Get()); }
        if (L == TEXT("rotator"))      { return AsStruct(TBaseStructure<FRotator>::Get()); }
        if (L == TEXT("transform"))    { return AsStruct(TBaseStructure<FTransform>::Get()); }
        if (L == TEXT("linearcolor") || L == TEXT("color")) { return AsStruct(TBaseStructure<FLinearColor>::Get()); }
        return false;
    }
}

FString UMCPReflectionLibrary::GetObjectPropertyMetadataJson(UObject* Object, bool bIncludeInherited)
{
    if (!Object)
    {
        return SerializeJson(ErrorObj(TEXT("null object")));
    }

    UClass* Cls = Object->GetClass();
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("class"), Cls->GetName());

    TArray<TSharedPtr<FJsonValue>> Props;
    const EFieldIteratorFlags::SuperClassFlags SuperFlag =
        bIncludeInherited ? EFieldIteratorFlags::IncludeSuper : EFieldIteratorFlags::ExcludeSuper;

    for (TFieldIterator<FProperty> It(Cls, SuperFlag); It; ++It)
    {
        FProperty* Prop = *It;
        if (!Prop)
        {
            continue;
        }

        TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
        P->SetStringField(TEXT("name"), Prop->GetName());
        P->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());

        UStruct* Owner = Prop->GetOwnerStruct();
        P->SetStringField(TEXT("owner_class"), Owner ? Owner->GetName() : TEXT(""));

#if WITH_EDITOR
        const FString Category = Prop->GetMetaData(TEXT("Category"));
        if (!Category.IsEmpty())
        {
            P->SetStringField(TEXT("category"), Category);
        }
        if (Prop->HasMetaData(TEXT("ClampMin")))
        {
            P->SetStringField(TEXT("clamp_min"), Prop->GetMetaData(TEXT("ClampMin")));
        }
        if (Prop->HasMetaData(TEXT("ClampMax")))
        {
            P->SetStringField(TEXT("clamp_max"), Prop->GetMetaData(TEXT("ClampMax")));
        }
        if (Prop->HasMetaData(TEXT("UIMin")))
        {
            P->SetStringField(TEXT("ui_min"), Prop->GetMetaData(TEXT("UIMin")));
        }
        if (Prop->HasMetaData(TEXT("UIMax")))
        {
            P->SetStringField(TEXT("ui_max"), Prop->GetMetaData(TEXT("UIMax")));
        }
        const FString Tip = Prop->GetMetaData(TEXT("ToolTip"));
        if (!Tip.IsEmpty())
        {
            P->SetStringField(TEXT("tooltip"), Tip);
        }
#endif // WITH_EDITOR

        TArray<TSharedPtr<FJsonValue>> Flags;
        auto AddFlag = [&Prop, &Flags](const TCHAR* Name, EPropertyFlags Flag)
        {
            if (Prop->HasAnyPropertyFlags(Flag))
            {
                Flags.Add(MakeShared<FJsonValueString>(Name));
            }
        };
        AddFlag(TEXT("Edit"), CPF_Edit);
        AddFlag(TEXT("EditConst"), CPF_EditConst);
        AddFlag(TEXT("BlueprintVisible"), CPF_BlueprintVisible);
        AddFlag(TEXT("BlueprintReadOnly"), CPF_BlueprintReadOnly);
        AddFlag(TEXT("Transient"), CPF_Transient);
        AddFlag(TEXT("Config"), CPF_Config);
        AddFlag(TEXT("InstancedReference"), CPF_InstancedReference);
        P->SetArrayField(TEXT("flags"), Flags);

        Props.Add(MakeShared<FJsonValueObject>(P));
    }

    Root->SetArrayField(TEXT("properties"), Props);
    return SerializeJson(Root);
}

FString UMCPReflectionLibrary::GetClassMetadataJson(UClass* Class)
{
    if (!Class)
    {
        return SerializeJson(ErrorObj(TEXT("null class")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("name"), Class->GetName());
    Root->SetStringField(TEXT("path"), Class->GetPathName());

    UClass* Super = Class->GetSuperClass();
    Root->SetStringField(TEXT("parent_class"), Super ? Super->GetName() : TEXT(""));

    Root->SetBoolField(TEXT("is_abstract"), Class->HasAnyClassFlags(CLASS_Abstract));
    Root->SetBoolField(TEXT("is_deprecated"), Class->HasAnyClassFlags(CLASS_Deprecated));

    bool bBlueprintable = false;
#if WITH_EDITOR
    bBlueprintable = Class->GetBoolMetaDataHierarchical(TEXT("IsBlueprintBase"))
        || Class->HasMetaData(TEXT("BlueprintType"));
#endif
    Root->SetBoolField(TEXT("is_blueprintable"), bBlueprintable);

    return SerializeJson(Root);
}

// ---- STAGED 2026-08-14: authored on the Mac, NOT YET COMPILED on Windows. ----------------

FString UMCPReflectionLibrary::GetStructFieldsJson(UScriptStruct* Struct)
{
    if (!Struct)
    {
        return SerializeJson(ErrorObj(TEXT("null struct")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("path"), Struct->GetPathName());

    TArray<TSharedPtr<FJsonValue>> Fields;
    for (TFieldIterator<FProperty> It(Struct); It; ++It)
    {
        FProperty* Prop = *It;
        if (!Prop)
        {
            continue;
        }
        TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
        P->SetStringField(TEXT("name"), Prop->GetName());
        P->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());
#if WITH_EDITOR
        const FString Category = Prop->GetMetaData(TEXT("Category"));
        if (!Category.IsEmpty())
        {
            P->SetStringField(TEXT("category"), Category);
        }
        const FString Tip = Prop->GetMetaData(TEXT("ToolTip"));
        if (!Tip.IsEmpty())
        {
            P->SetStringField(TEXT("tooltip"), Tip);
        }
#endif
        TArray<TSharedPtr<FJsonValue>> Flags;
        auto AddFlag = [&Prop, &Flags](const TCHAR* Name, EPropertyFlags Flag)
        {
            if (Prop->HasAnyPropertyFlags(Flag))
            {
                Flags.Add(MakeShared<FJsonValueString>(Name));
            }
        };
        AddFlag(TEXT("Edit"), CPF_Edit);
        AddFlag(TEXT("BlueprintVisible"), CPF_BlueprintVisible);
        AddFlag(TEXT("BlueprintReadOnly"), CPF_BlueprintReadOnly);
        P->SetArrayField(TEXT("flags"), Flags);

        Fields.Add(MakeShared<FJsonValueObject>(P));
    }
    Root->SetArrayField(TEXT("fields"), Fields);
    return SerializeJson(Root);
}

FString UMCPReflectionLibrary::GetStaticMeshSocketsJson(UStaticMesh* Mesh)
{
    if (!Mesh)
    {
        return SerializeJson(ErrorObj(TEXT("null mesh")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("mesh"), Mesh->GetPathName());

    TArray<TSharedPtr<FJsonValue>> Out;
    // NOTE for the recompile: `Mesh->Sockets` is the socket array (TArray<UStaticMeshSocket*>).
    // If it isn't accessible in this engine build, use the accessor `Mesh->GetSockets()` instead.
    for (UStaticMeshSocket* S : Mesh->Sockets)
    {
        if (!S)
        {
            continue;
        }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), S->SocketName.ToString());
        J->SetStringField(TEXT("tag"), S->Tag);
        J->SetArrayField(TEXT("location"), Vec3(S->RelativeLocation.X, S->RelativeLocation.Y, S->RelativeLocation.Z));
        J->SetArrayField(TEXT("rotation"), Vec3(S->RelativeRotation.Pitch, S->RelativeRotation.Yaw, S->RelativeRotation.Roll));
        J->SetArrayField(TEXT("scale"), Vec3(S->RelativeScale.X, S->RelativeScale.Y, S->RelativeScale.Z));
        Out.Add(MakeShared<FJsonValueObject>(J));
    }
    Root->SetArrayField(TEXT("sockets"), Out);
    return SerializeJson(Root);
}

// ---- STAGED C++ #3 2026-08-14: authored on the Mac, NOT YET COMPILED on Windows. -------------

FString UMCPReflectionLibrary::GetCurveKeysJson(UCurveBase* Curve)
{
    if (!Curve)
    {
        return SerializeJson(ErrorObj(TEXT("null curve")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("curve"), Curve->GetName());
    Root->SetStringField(TEXT("class"), Curve->GetClass()->GetName());

    TArray<TSharedPtr<FJsonValue>> Channels;
    if (UCurveFloat* CF = Cast<UCurveFloat>(Curve))
    {
        AddCurveChannel(Channels, TEXT("Value"), CF->FloatCurve);
    }
    else if (UCurveVector* CV = Cast<UCurveVector>(Curve))
    {
        static const TCHAR* Names[3] = { TEXT("X"), TEXT("Y"), TEXT("Z") };
        for (int32 i = 0; i < 3; ++i)
        {
            AddCurveChannel(Channels, Names[i], CV->FloatCurves[i]);
        }
    }
    else if (UCurveLinearColor* CC = Cast<UCurveLinearColor>(Curve))
    {
        static const TCHAR* Names[4] = { TEXT("R"), TEXT("G"), TEXT("B"), TEXT("A") };
        for (int32 i = 0; i < 4; ++i)
        {
            AddCurveChannel(Channels, Names[i], CC->FloatCurves[i]);
        }
    }
    else
    {
        Root->SetStringField(TEXT("note"), TEXT("unsupported curve subclass; only Float/Vector/LinearColor expose keys"));
    }

    Root->SetArrayField(TEXT("channels"), Channels);
    return SerializeJson(Root);
}

FString UMCPReflectionLibrary::GetTextureInfoJson(UTexture2D* Texture)
{
    if (!Texture)
    {
        return SerializeJson(ErrorObj(TEXT("null texture")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("texture"), Texture->GetPathName());
    Root->SetNumberField(TEXT("width"), (double)Texture->GetSizeX());
    Root->SetNumberField(TEXT("height"), (double)Texture->GetSizeY());
    Root->SetNumberField(TEXT("num_mips"), Texture->GetNumMips());

    const EPixelFormat PF = Texture->GetPixelFormat();
    Root->SetStringField(TEXT("pixel_format"), PixelFormatStr(PF));
    Root->SetNumberField(TEXT("pixel_format_value"), (int32)PF);

    const FIntPoint Imported = Texture->GetImportedSize();
    Root->SetNumberField(TEXT("imported_width"), Imported.X);
    Root->SetNumberField(TEXT("imported_height"), Imported.Y);

#if WITH_EDITORONLY_DATA
    Root->SetBoolField(TEXT("has_source"), Texture->Source.IsValid());
    if (Texture->Source.IsValid())
    {
        Root->SetNumberField(TEXT("source_width"), (double)Texture->Source.GetSizeX());
        Root->SetNumberField(TEXT("source_height"), (double)Texture->Source.GetSizeY());
        Root->SetNumberField(TEXT("source_num_mips"), Texture->Source.GetNumMips());
        Root->SetStringField(TEXT("source_format"), SourceFormatStr(Texture->Source.GetFormat()));
    }
#endif

    return SerializeJson(Root);
}

FString UMCPReflectionLibrary::GetPhysicsBodiesJson(UPhysicsAsset* PhysicsAsset)
{
    if (!PhysicsAsset)
    {
        return SerializeJson(ErrorObj(TEXT("null physics asset")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("physics_asset"), PhysicsAsset->GetPathName());

    TArray<TSharedPtr<FJsonValue>> Bodies;
    for (const TObjectPtr<USkeletalBodySetup>& BSPtr : PhysicsAsset->SkeletalBodySetups)
    {
        USkeletalBodySetup* BS = BSPtr.Get();
        if (!BS)
        {
            continue;
        }

        TSharedRef<FJsonObject> B = MakeShared<FJsonObject>();
        B->SetStringField(TEXT("bone"), BS->BoneName.ToString());

        const FKAggregateGeom& Geom = BS->AggGeom;
        B->SetNumberField(TEXT("sphere_count"), Geom.SphereElems.Num());
        B->SetNumberField(TEXT("box_count"), Geom.BoxElems.Num());
        B->SetNumberField(TEXT("capsule_count"), Geom.SphylElems.Num());
        B->SetNumberField(TEXT("convex_count"), Geom.ConvexElems.Num());
        B->SetNumberField(TEXT("tapered_capsule_count"), Geom.TaperedCapsuleElems.Num());

        TArray<TSharedPtr<FJsonValue>> Prims;
        for (const FKSphereElem& S : Geom.SphereElems)
        {
            TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
            P->SetStringField(TEXT("type"), TEXT("sphere"));
            P->SetArrayField(TEXT("center"), Vec3(S.Center.X, S.Center.Y, S.Center.Z));
            P->SetNumberField(TEXT("radius"), S.Radius);
            Prims.Add(MakeShared<FJsonValueObject>(P));
        }
        for (const FKBoxElem& Bx : Geom.BoxElems)
        {
            TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
            P->SetStringField(TEXT("type"), TEXT("box"));
            P->SetArrayField(TEXT("center"), Vec3(Bx.Center.X, Bx.Center.Y, Bx.Center.Z));
            P->SetArrayField(TEXT("extent"), Vec3(Bx.X, Bx.Y, Bx.Z));
            Prims.Add(MakeShared<FJsonValueObject>(P));
        }
        for (const FKSphylElem& C : Geom.SphylElems)
        {
            TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
            P->SetStringField(TEXT("type"), TEXT("capsule"));
            P->SetArrayField(TEXT("center"), Vec3(C.Center.X, C.Center.Y, C.Center.Z));
            P->SetNumberField(TEXT("radius"), C.Radius);
            P->SetNumberField(TEXT("length"), C.Length);
            Prims.Add(MakeShared<FJsonValueObject>(P));
        }
        for (const FKConvexElem& Cv : Geom.ConvexElems)
        {
            TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
            P->SetStringField(TEXT("type"), TEXT("convex"));
            P->SetNumberField(TEXT("vertex_count"), Cv.VertexData.Num());
            Prims.Add(MakeShared<FJsonValueObject>(P));
        }
        B->SetArrayField(TEXT("primitives"), Prims);

        Bodies.Add(MakeShared<FJsonValueObject>(B));
    }

    Root->SetNumberField(TEXT("body_count"), Bodies.Num());
    Root->SetArrayField(TEXT("bodies"), Bodies);
    return SerializeJson(Root);
}

// ---- C++ #4 2026-08-15 (Wave-3 batch 1: DATA-ASSET AUTHORING) — COMPILED + WIRED + VERIFIED LIVE. --

FString UMCPReflectionLibrary::SetCurveKeysJson(UCurveBase* Curve, const FString& KeysJson)
{
    if (!Curve)
    {
        return SerializeJson(ErrorObj(TEXT("null curve")));
    }

    TSharedPtr<FJsonObject> In;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(KeysJson);
    if (!FJsonSerializer::Deserialize(Reader, In) || !In.IsValid())
    {
        return SerializeJson(ErrorObj(TEXT("invalid JSON")));
    }

    const TArray<TSharedPtr<FJsonValue>>* Channels = nullptr;
    if (!In->TryGetArrayField(TEXT("channels"), Channels))
    {
        return SerializeJson(ErrorObj(TEXT("missing 'channels' array")));
    }

    int32 ChannelsWritten = 0;
    int32 KeysWritten = 0;
    for (const TSharedPtr<FJsonValue>& CVal : *Channels)
    {
        const TSharedPtr<FJsonObject> CObj = CVal->AsObject();
        if (!CObj.IsValid())
        {
            continue;
        }
        const int32 Index = (int32)CObj->GetNumberField(TEXT("index"));
        FRichCurve* RC = GetChannelPtr(Curve, Index);
        if (!RC)
        {
            continue;
        }
        RC->Reset(); // fully clear this channel, then rebuild from the provided keys

        const TArray<TSharedPtr<FJsonValue>>* Keys = nullptr;
        if (CObj->TryGetArrayField(TEXT("keys"), Keys))
        {
            for (const TSharedPtr<FJsonValue>& KVal : *Keys)
            {
                const TSharedPtr<FJsonObject> K = KVal->AsObject();
                if (!K.IsValid())
                {
                    continue;
                }
                const float Time = (float)K->GetNumberField(TEXT("time"));
                const float Value = (float)K->GetNumberField(TEXT("value"));
                const FKeyHandle H = RC->AddKey(Time, Value);

                FString ModeStr;
                if (K->TryGetStringField(TEXT("interp_mode"), ModeStr))
                {
                    RC->SetKeyInterpMode(H, ParseInterp(ModeStr));
                }
                if (K->TryGetStringField(TEXT("tangent_mode"), ModeStr))
                {
                    RC->SetKeyTangentMode(H, ParseTangent(ModeStr));
                }
                double Tan = 0.0;
                FRichCurveKey& RK = RC->GetKey(H);
                if (K->TryGetNumberField(TEXT("arrive_tangent"), Tan))
                {
                    RK.ArriveTangent = (float)Tan;
                }
                if (K->TryGetNumberField(TEXT("leave_tangent"), Tan))
                {
                    RK.LeaveTangent = (float)Tan;
                }
                ++KeysWritten;
            }
        }
        ++ChannelsWritten;
    }

    Curve->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("curve"), Curve->GetName());
    Root->SetNumberField(TEXT("channels_written"), ChannelsWritten);
    Root->SetNumberField(TEXT("keys_written"), KeysWritten);
    return SerializeJson(Root);
}

FString UMCPReflectionLibrary::AddStructField(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeName)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return SerializeJson(ErrorObj(TEXT("null struct")));
    }
    FEdGraphPinType PinType;
    if (!BuildPinType(TypeName, PinType))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("unsupported type '%s'"), *TypeName)));
    }

    const int32 Before = FStructureEditorUtils::GetVarDesc(Struct).Num();
    if (!FStructureEditorUtils::AddVariable(Struct, PinType))
    {
        return SerializeJson(ErrorObj(TEXT("AddVariable failed")));
    }

    // The newly-added variable is the last descriptor; rename it to the requested friendly name.
    FString GuidStr;
    TArray<FStructVariableDescription>& Desc = FStructureEditorUtils::GetVarDesc(Struct);
    if (Desc.Num() > Before)
    {
        const FGuid NewGuid = Desc.Last().VarGuid;
        if (!FieldName.IsEmpty())
        {
            FStructureEditorUtils::RenameVariable(Struct, NewGuid, FieldName);
        }
        GuidStr = NewGuid.ToString();
    }

    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("added_field"), FieldName);
    Root->SetStringField(TEXT("type"), TypeName);
    Root->SetStringField(TEXT("guid"), GuidStr);
    Root->SetNumberField(TEXT("field_count"), FStructureEditorUtils::GetVarDesc(Struct).Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveStructField(UUserDefinedStruct* Struct, const FString& FieldName)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return SerializeJson(ErrorObj(TEXT("null struct")));
    }
    TArray<FStructVariableDescription>& Desc = FStructureEditorUtils::GetVarDesc(Struct);
    FGuid Target;
    bool bFound = false;
    for (const FStructVariableDescription& D : Desc)
    {
        if (D.FriendlyName == FieldName || D.VarName == FieldName)
        {
            Target = D.VarGuid;
            bFound = true;
            break;
        }
    }
    if (!bFound)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("field '%s' not found"), *FieldName)));
    }

    const bool bOk = FStructureEditorUtils::RemoveVariable(Struct, Target);
    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("removed_field"), FieldName);
    Root->SetBoolField(TEXT("removed"), bOk);
    Root->SetNumberField(TEXT("field_count"), FStructureEditorUtils::GetVarDesc(Struct).Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddEnumEntry(UUserDefinedEnum* Enum, const FString& DisplayName)
{
#if WITH_EDITOR
    if (!Enum)
    {
        return SerializeJson(ErrorObj(TEXT("null enum")));
    }
    FEnumEditorUtils::AddNewEnumeratorForUserDefinedEnum(Enum);

    // A UserDefinedEnum keeps a hidden trailing _MAX sentinel; real entries = NumEnums()-1.
    const int32 RealCount = Enum->NumEnums() > 0 ? Enum->NumEnums() - 1 : 0;
    const int32 NewIndex = RealCount - 1;
    if (!DisplayName.IsEmpty() && NewIndex >= 0)
    {
        FEnumEditorUtils::SetEnumeratorDisplayName(Enum, NewIndex, FText::FromString(DisplayName));
    }

    Enum->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("enum"), Enum->GetName());
    Root->SetStringField(TEXT("added_entry"), DisplayName);
    Root->SetNumberField(TEXT("index"), NewIndex);
    Root->SetNumberField(TEXT("entry_count"), RealCount);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEnumEntry(UUserDefinedEnum* Enum, int32 Index)
{
#if WITH_EDITOR
    if (!Enum)
    {
        return SerializeJson(ErrorObj(TEXT("null enum")));
    }
    const int32 RealBefore = Enum->NumEnums() > 0 ? Enum->NumEnums() - 1 : 0;
    if (Index < 0 || Index >= RealBefore)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("index %d out of range (0..%d)"), Index, RealBefore - 1)));
    }
    FEnumEditorUtils::RemoveEnumeratorFromUserDefinedEnum(Enum, Index);
    Enum->MarkPackageDirty();

    const int32 RealAfter = Enum->NumEnums() > 0 ? Enum->NumEnums() - 1 : 0;
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("enum"), Enum->GetName());
    Root->SetNumberField(TEXT("removed_index"), Index);
    Root->SetNumberField(TEXT("entry_count"), RealAfter);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #5 2026-08-15 (Wave-3 batch 2: NIAGARA AUTHORING) — COMPILED + WIRED + VERIFIED LIVE. -----
// The Niagara emitter-handle API is version-sensitive. Each line tagged "VERIFY" is one to confirm
// against engine source on the Windows build (NiagaraSystem.h / NiagaraEmitterHandle.h /
// NiagaraEditorUtilities.h) and adjust if the signature differs in this UE build.

FString UMCPReflectionLibrary::AddEmitterToSystem(UNiagaraSystem* System, UNiagaraEmitter* SourceEmitter, const FString& HandleName)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }
    if (!SourceEmitter)
    {
        return SerializeJson(ErrorObj(TEXT("null source emitter")));
    }

    const int32 Before = System->GetEmitterHandles().Num();   // VERIFY: GetEmitterHandles() -> const TArray<FNiagaraEmitterHandle>&

    // Windows fix (C++ #5): this engine's signature is
    //   const FGuid AddEmitterToSystem(UNiagaraSystem&, UNiagaraEmitter&, FGuid EmitterVersion, bool bCreateCopy=true)
    // — it requires the source emitter's exposed version GUID (versioned FNiagaraEmitter data), which the
    // original 3-arg call omitted. GetExposedVersion() returns FNiagaraAssetVersion (has .VersionGuid).
    const FGuid SourceVersion = SourceEmitter->GetExposedVersion().VersionGuid;
    FNiagaraEditorUtilities::AddEmitterToSystem(*System, *SourceEmitter, SourceVersion, /*bCreateCopy=*/true);

    const TArray<FNiagaraEmitterHandle>& Handles = System->GetEmitterHandles();
    const int32 After = Handles.Num();

    FString AddedName, AddedId;
    if (After > Before && After > 0)
    {
        const FNiagaraEmitterHandle& H = Handles[After - 1];   // newly-added handle is last
        AddedName = H.GetName().ToString();                    // VERIFY: FNiagaraEmitterHandle::GetName()
        AddedId = H.GetId().ToString();                        // VERIFY: FNiagaraEmitterHandle::GetId() -> FGuid
    }

    System->RequestCompile(false);   // VERIFY: RequestCompile(bool) — make the added emitter usable
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("added_handle_name"), AddedName);
    Root->SetStringField(TEXT("added_handle_id"), AddedId);
    Root->SetNumberField(TEXT("emitter_count"), After);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEmitterFromSystem(UNiagaraSystem* System, const FString& HandleNameOrId)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }

    // Find the handle by name or id first, then remove by id (avoids mutating while iterating).
    FGuid TargetId;
    bool bFound = false;
    for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
    {
        if (H.GetName().ToString() == HandleNameOrId || H.GetId().ToString() == HandleNameOrId)
        {
            TargetId = H.GetId();
            bFound = true;
            break;
        }
    }

    if (bFound)
    {
        // VERIFY vs engine source: UNiagaraSystem::RemoveEmitterHandlesById(const TSet<FGuid>&).
        // Alternative: RemoveEmitterHandle(const FNiagaraEmitterHandle&).
        TSet<FGuid> ToRemove;
        ToRemove.Add(TargetId);
        System->RemoveEmitterHandlesById(ToRemove);
        System->RequestCompile(false);
        System->MarkPackageDirty();
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetBoolField(TEXT("removed"), bFound);
    Root->SetNumberField(TEXT("emitter_count"), System->GetEmitterHandles().Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #6 2026-08-15 (gameplay-tag authoring) — COMPILED CLEAN + WIRED + VERIFIED LIVE. ---------
// Uses the GameplayTagsEditor INI path (writes DefaultGameplayTags.ini + refreshes the manager).
// Calls tagged VERIFY are version-sensitive — confirm against GameplayTagsEditorModule.h on the build.

FString UMCPReflectionLibrary::AddGameplayTag(const FString& TagName, const FString& Comment)
{
#if WITH_EDITOR
    if (TagName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty tag name")));
    }
    // VERIFY vs engine source: IGameplayTagsEditorModule::Get() (static accessor) +
    //   AddNewGameplayTagToINI(const FString& NewTag, const FString& Comment, const FName& TagSourceName, ...).
    const bool bAdded = IGameplayTagsEditorModule::Get().AddNewGameplayTagToINI(TagName, Comment, NAME_None);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("tag"), TagName);
    Root->SetStringField(TEXT("comment"), Comment);
    Root->SetBoolField(TEXT("added"), bAdded);
    // Confirm it is now known to the manager.
    const bool bRegistered = UGameplayTagsManager::Get().FindTagNode(FName(*TagName)).IsValid();
    Root->SetBoolField(TEXT("registered"), bRegistered);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveGameplayTag(const FString& TagName)
{
#if WITH_EDITOR
    if (TagName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty tag name")));
    }
    UGameplayTagsManager& Mgr = UGameplayTagsManager::Get();
    // VERIFY: UGameplayTagsManager::FindTagNode(const FName&) -> TSharedPtr<FGameplayTagNode>.
    TSharedPtr<FGameplayTagNode> Node = Mgr.FindTagNode(FName(*TagName));
    bool bRemoved = false;
    if (Node.IsValid())
    {
        // VERIFY: IGameplayTagsEditorModule::DeleteTagFromINI(TSharedPtr<FGameplayTagNode>).
        bRemoved = IGameplayTagsEditorModule::Get().DeleteTagFromINI(Node);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("tag"), TagName);
    Root->SetBoolField(TEXT("found"), Node.IsValid());
    Root->SetBoolField(TEXT("removed"), bRemoved);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #7 2026-08-15 (Kismet BP variable/node helper) — COMPILED CLEAN + WIRED + VERIFIED LIVE. --
// FBlueprintEditorUtils lives in the UnrealEd module (already a dep); UK2Node_Event in BlueprintGraph
// (already a dep) — NO Build.cs change. Calls tagged VERIFY are version-sensitive.

FString UMCPReflectionLibrary::AddBlueprintVariable(UBlueprint* Blueprint, const FString& VarName, const FString& TypeName)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }
    FEdGraphPinType PinType;
    if (!BuildPinType(TypeName, PinType))   // reuses the C++ #4 helper
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("unsupported type '%s'"), *TypeName)));
    }
    // VERIFY: FBlueprintEditorUtils::AddMemberVariable(UBlueprint*, const FName&, const FEdGraphPinType&, const FString& DefaultValue="")
    const bool bAdded = FBlueprintEditorUtils::AddMemberVariable(Blueprint, FName(*VarName), PinType);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetBoolField(TEXT("added"), bAdded);
    Root->SetStringField(TEXT("variable"), VarName);
    Root->SetStringField(TEXT("type"), TypeName);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveBlueprintVariable(UBlueprint* Blueprint, const FString& VarName)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }
    const FName VN(*VarName);
    // VERIFY: FBlueprintEditorUtils::FindNewVariableIndex(UBlueprint*, const FName&) -> int32 (INDEX_NONE if absent)
    const int32 Idx = FBlueprintEditorUtils::FindNewVariableIndex(Blueprint, VN);
    bool bRemoved = false;
    if (Idx != INDEX_NONE)
    {
        // VERIFY: FBlueprintEditorUtils::RemoveMemberVariable(UBlueprint*, const FName&)
        FBlueprintEditorUtils::RemoveMemberVariable(Blueprint, VN);
        bRemoved = true;
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetBoolField(TEXT("found"), Idx != INDEX_NONE);
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetStringField(TEXT("variable"), VarName);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEventNode(UBlueprint* Blueprint, const FString& EventName)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }
    TArray<UK2Node_Event*> EventNodes;
    // VERIFY: FBlueprintEditorUtils::GetAllNodesOfClass<T>(const UBlueprint*, TArray<T*>&) template helper
    FBlueprintEditorUtils::GetAllNodesOfClass<UK2Node_Event>(Blueprint, EventNodes);

    const FName EN(*EventName);
    UK2Node_Event* Found = nullptr;
    for (UK2Node_Event* N : EventNodes)
    {
        if (!N)
        {
            continue;
        }
        // VERIFY: UK2Node_Event::EventReference (FMemberReference).GetMemberName() + CustomFunctionName (FName)
        if (N->EventReference.GetMemberName() == EN || N->CustomFunctionName == EN)
        {
            Found = N;
            break;
        }
    }

    bool bRemoved = false;
    if (Found)
    {
        // VERIFY: FBlueprintEditorUtils::RemoveNode(UBlueprint*, UEdGraphNode*, bool bDontRecompile=false)
        FBlueprintEditorUtils::RemoveNode(Blueprint, Found, false);
        bRemoved = true;
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetBoolField(TEXT("found"), Found != nullptr);
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetStringField(TEXT("event"), EventName);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #8 2026-08-15 (widget-tree authoring) — COMPILED + FIXED (GUID-map sync) + WIRED + VERIFIED. -
// Root widget is a protected member of UWidgetTree (Python can't set it) -> done in C++. Calls tagged
// VERIFY are version-sensitive — confirm against WidgetTree.h / WidgetBlueprint.h on the build.

FString UMCPReflectionLibrary::AddWidgetToBlueprint(UWidgetBlueprint* WidgetBlueprint, const FString& WidgetClassPath,
                                                    const FString& NewName, const FString& ParentName)
{
#if WITH_EDITOR
    if (!WidgetBlueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null widget blueprint")));
    }
    UWidgetTree* Tree = WidgetBlueprint->WidgetTree;   // VERIFY: UWidgetBlueprint::WidgetTree
    if (!Tree)
    {
        return SerializeJson(ErrorObj(TEXT("blueprint has no WidgetTree")));
    }
    UClass* WClass = LoadClass<UWidget>(nullptr, *WidgetClassPath);
    if (!WClass)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("could not load widget class '%s'"), *WidgetClassPath)));
    }
    // VERIFY: UWidgetTree::ConstructWidget<T>(TSubclassOf<UWidget>, FName) — registers the widget in the tree.
    UWidget* NewW = Tree->ConstructWidget<UWidget>(WClass, FName(*NewName));
    if (!NewW)
    {
        return SerializeJson(ErrorObj(TEXT("ConstructWidget returned null")));
    }

    bool bIsRoot = false;
    FString ParentUsed;
    if (ParentName.IsEmpty() && Tree->RootWidget == nullptr)   // VERIFY: UWidgetTree::RootWidget (protected member, C++-accessible)
    {
        Tree->RootWidget = NewW;
        bIsRoot = true;
    }
    else
    {
        UPanelWidget* Parent = nullptr;
        if (!ParentName.IsEmpty())
        {
            Parent = Cast<UPanelWidget>(Tree->FindWidget(FName(*ParentName)));   // VERIFY: UWidgetTree::FindWidget(FName)
        }
        else
        {
            Parent = Cast<UPanelWidget>(Tree->RootWidget);
        }
        if (!Parent)
        {
            return SerializeJson(ErrorObj(FString::Printf(TEXT("no valid parent panel (ParentName='%s')"), *ParentName)));
        }
        Parent->AddChild(NewW);   // VERIFY: UPanelWidget::AddChild(UWidget*)
        ParentUsed = Parent->GetName();
    }

    // Windows fix (C++ #8): register the widget's variable GUID so the UMG compiler's
    // WidgetVariableNameToGuidMap stays in sync with the tree. Without this,
    // WidgetBlueprintCompiler.cpp ensures "Widget [x] was added but did not get a GUID" on compile.
    WidgetBlueprint->OnVariableAdded(NewW->GetFName());

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WidgetBlueprint);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WidgetBlueprint->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("name"), NewW->GetName());
    Root->SetBoolField(TEXT("is_root"), bIsRoot);
    Root->SetStringField(TEXT("parent"), ParentUsed);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveWidgetFromBlueprint(UWidgetBlueprint* WidgetBlueprint, const FString& Name)
{
#if WITH_EDITOR
    if (!WidgetBlueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null widget blueprint")));
    }
    UWidgetTree* Tree = WidgetBlueprint->WidgetTree;
    if (!Tree)
    {
        return SerializeJson(ErrorObj(TEXT("blueprint has no WidgetTree")));
    }
    UWidget* W = Tree->FindWidget(FName(*Name));
    bool bRemoved = false;
    if (W)
    {
        const FName WName = W->GetFName();   // capture before removal for GUID-map cleanup

        // Collect descendants BEFORE detaching so we can clean them up too.
        TArray<UWidget*> ChildWidgets;
        UWidgetTree::GetChildWidgets(W, ChildWidgets);

        if (Tree->RootWidget == W)
        {
            Tree->RootWidget = nullptr;
            bRemoved = true;
        }
        else
        {
            // VERIFY: UWidgetTree::RemoveWidget(UWidget*) -> bool (detaches from parent slot)
            bRemoved = Tree->RemoveWidget(W);
        }

        // Windows fix (C++ #8): RemoveWidget only detaches from the parent slot — the widget UObject
        // stays OUTERED to the WidgetTree, so the UMG compiler's ForEachSourceWidget still sees it and
        // ensures on WidgetVariableNameToGuidMap. Mirror the engine's own delete path
        // (WidgetBlueprintEditorUtils::DeleteWidgets): rename the removed widget + its children into the
        // transient package so they are no longer source widgets, then drop their variable GUID entries.
        W->Rename(nullptr, GetTransientPackage());
        WidgetBlueprint->OnVariableRemoved(WName);
        for (UWidget* Child : ChildWidgets)
        {
            if (!Child)
            {
                continue;
            }
            const FName ChildName = Child->GetFName();
            Child->Rename(nullptr, GetTransientPackage());
            WidgetBlueprint->OnVariableRemoved(ChildName);
        }

        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WidgetBlueprint);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WidgetBlueprint->GetName());
    Root->SetBoolField(TEXT("found"), W != nullptr);
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetStringField(TEXT("name"), Name);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #9 2026-08-15 (BP event-node reader + guid-remove) — COMPILED CLEAN + WIRED + VERIFIED. ---

FString UMCPReflectionLibrary::GetBlueprintEventNodesJson(UBlueprint* Blueprint)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }
    TArray<UK2Node_Event*> EventNodes;
    FBlueprintEditorUtils::GetAllNodesOfClass<UK2Node_Event>(Blueprint, EventNodes);

    TArray<TSharedPtr<FJsonValue>> Events;
    for (UK2Node_Event* N : EventNodes)
    {
        if (!N)
        {
            continue;
        }
        TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
        // VERIFY: UK2Node_Event::bOverrideFunction (override event like ReceiveBeginPlay) vs a custom event.
        const bool bCustom = !N->bOverrideFunction;
        const FName EvName = N->bOverrideFunction ? N->EventReference.GetMemberName() : N->CustomFunctionName;
        E->SetStringField(TEXT("event_name"), EvName.ToString());
        E->SetBoolField(TEXT("is_custom"), bCustom);
        const UEdGraph* G = N->GetGraph();   // VERIFY: UEdGraphNode::GetGraph()
        E->SetStringField(TEXT("graph"), G ? G->GetName() : TEXT(""));
        E->SetStringField(TEXT("node_guid"), N->NodeGuid.ToString());   // VERIFY: UEdGraphNode::NodeGuid (FGuid)
        Events.Add(MakeShared<FJsonValueObject>(E));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetNumberField(TEXT("event_count"), Events.Num());
    Root->SetArrayField(TEXT("events"), Events);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEventNodeByGuid(UBlueprint* Blueprint, const FString& NodeGuidStr)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }
    FGuid Target;
    if (!FGuid::Parse(NodeGuidStr, Target))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("invalid guid '%s'"), *NodeGuidStr)));
    }

    TArray<UK2Node_Event*> EventNodes;
    FBlueprintEditorUtils::GetAllNodesOfClass<UK2Node_Event>(Blueprint, EventNodes);

    UK2Node_Event* Found = nullptr;
    for (UK2Node_Event* N : EventNodes)
    {
        if (N && N->NodeGuid == Target)
        {
            Found = N;
            break;
        }
    }

    bool bRemoved = false;
    if (Found)
    {
        FBlueprintEditorUtils::RemoveNode(Blueprint, Found, false);
        bRemoved = true;
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetBoolField(TEXT("found"), Found != nullptr);
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetStringField(TEXT("node_guid"), NodeGuidStr);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ================= C++ #10 (Deeper Niagara) — AUTHORED on Mac, NOT YET COMPILED on Windows. =================

namespace
{
    // ---- C++ #10 helpers (Niagara user parameters) ----

    // Map a friendly type string -> FNiagaraTypeDefinition. Niagara value types are SINGLE-PRECISION
    // (FVector2f/FVector3f/FVector4f/FQuat4f) — not the double FVector family. VERIFY the Get*Def()
    // accessor names against NiagaraTypeDefinition.h.
    bool NiagaraTypeFromName(const FString& In, FNiagaraTypeDefinition& Out)
    {
        const FString L = In.ToLower();
        if (L == TEXT("bool") || L == TEXT("boolean"))              { Out = FNiagaraTypeDefinition::GetBoolDef();  return true; } // VERIFY vs engine source: NiagaraTypeDefinition.h GetBoolDef()
        if (L == TEXT("int")  || L == TEXT("int32") || L == TEXT("integer")) { Out = FNiagaraTypeDefinition::GetIntDef(); return true; } // VERIFY: GetIntDef()
        if (L == TEXT("float") || L == TEXT("double") || L == TEXT("real"))  { Out = FNiagaraTypeDefinition::GetFloatDef(); return true; } // VERIFY: GetFloatDef() (Niagara float is 32-bit)
        if (L == TEXT("vector2") || L == TEXT("vec2") || L == TEXT("vector2d")) { Out = FNiagaraTypeDefinition::GetVec2Def(); return true; } // VERIFY: GetVec2Def() -> FVector2f
        if (L == TEXT("vector")  || L == TEXT("vec3") || L == TEXT("vector3"))  { Out = FNiagaraTypeDefinition::GetVec3Def(); return true; } // VERIFY: GetVec3Def() -> FVector3f
        if (L == TEXT("vector4") || L == TEXT("vec4"))              { Out = FNiagaraTypeDefinition::GetVec4Def();  return true; } // VERIFY: GetVec4Def() -> FVector4f
        if (L == TEXT("linearcolor") || L == TEXT("color"))         { Out = FNiagaraTypeDefinition::GetColorDef(); return true; } // VERIFY: GetColorDef() -> FLinearColor
        if (L == TEXT("quat") || L == TEXT("quaternion"))           { Out = FNiagaraTypeDefinition::GetQuatDef();  return true; } // VERIFY: GetQuatDef() -> FQuat4f
        return false;
    }

    // Friendly string for a NiagaraTypeDefinition (for JSON echo).
    FString NiagaraTypeToName(const FNiagaraTypeDefinition& T)
    {
        if (T == FNiagaraTypeDefinition::GetBoolDef())  return TEXT("bool");
        if (T == FNiagaraTypeDefinition::GetIntDef())   return TEXT("int");
        if (T == FNiagaraTypeDefinition::GetFloatDef()) return TEXT("float");
        if (T == FNiagaraTypeDefinition::GetVec2Def())  return TEXT("vector2");
        if (T == FNiagaraTypeDefinition::GetVec3Def())  return TEXT("vector");
        if (T == FNiagaraTypeDefinition::GetVec4Def())  return TEXT("vector4");
        if (T == FNiagaraTypeDefinition::GetColorDef()) return TEXT("linearcolor");
        if (T == FNiagaraTypeDefinition::GetQuatDef())  return TEXT("quat");
        return T.GetName(); // VERIFY: FNiagaraTypeDefinition::GetName() -> FString
    }

    // Prepend the conventional "User." namespace if the caller did not include it.
    FName MakeUserParamName(const FString& ParamName)
    {
        FString N = ParamName;
        if (!N.StartsWith(TEXT("User."), ESearchCase::IgnoreCase))
        {
            N = TEXT("User.") + N;
        }
        return FName(*N);
    }

    // Read N floats from a JSON scalar/array into Out. Accepts an array [a,b,..] (preferred) or, for N==1,
    // a bare number. Returns false if there are not enough components.
    bool JsonToFloats(const TSharedPtr<FJsonValue>& V, int32 N, float* Out)
    {
        if (!V.IsValid()) return false;
        const TArray<TSharedPtr<FJsonValue>>* Arr = nullptr;
        if (V->TryGetArray(Arr))
        {
            if (!Arr || Arr->Num() < N) return false;
            for (int32 i = 0; i < N; ++i) { Out[i] = (float)(*Arr)[i]->AsNumber(); }
            return true;
        }
        if (N == 1) { Out[0] = (float)V->AsNumber(); return true; }
        return false;
    }

    // Interpret ValueJson per the stored type and write it into the redirection store. Var carries the
    // FULL "User."-prefixed name and the correct type. Returns false + Err on a shape mismatch.
    bool ApplyJsonValueToStore(FNiagaraUserRedirectionParameterStore& Store, const FNiagaraVariable& Var,
                               const TSharedPtr<FJsonValue>& V, FString& Err)
    {
        const FNiagaraTypeDefinition& T = Var.GetType();
        float f[4] = {0,0,0,0};

        if (T == FNiagaraTypeDefinition::GetFloatDef())
        {
            Store.SetParameterValue((float)V->AsNumber(), Var, /*bAdd=*/false); return true; // VERIFY vs engine source: FNiagaraParameterStore::SetParameterValue<T>(const T&, const FNiagaraVariable&, bool)
        }
        if (T == FNiagaraTypeDefinition::GetIntDef())
        {
            FNiagaraInt32 I; I.Value = (int32)V->AsNumber();                                   // VERIFY: FNiagaraInt32 { int32 Value; } in NiagaraTypes.h
            Store.SetParameterValue(I, Var, false); return true;
        }
        if (T == FNiagaraTypeDefinition::GetBoolDef())
        {
            const bool b = (V->Type == EJson::Boolean) ? V->AsBool() : (V->AsNumber() != 0.0);
            FNiagaraBool B; B.SetValue(b);                                                     // VERIFY: FNiagaraBool::SetValue(bool) in NiagaraTypes.h
            Store.SetParameterValue(B, Var, false); return true;
        }
        if (T == FNiagaraTypeDefinition::GetVec2Def())
        {
            if (!JsonToFloats(V, 2, f)) { Err = TEXT("vector2 needs a 2-element array"); return false; }
            Store.SetParameterValue(FVector2f(f[0], f[1]), Var, false); return true;           // VERIFY: GetVec2Def() underlying is FVector2f
        }
        if (T == FNiagaraTypeDefinition::GetVec3Def())
        {
            if (!JsonToFloats(V, 3, f)) { Err = TEXT("vector needs a 3-element array"); return false; }
            Store.SetParameterValue(FVector3f(f[0], f[1], f[2]), Var, false); return true;     // VERIFY: GetVec3Def() underlying is FVector3f
        }
        if (T == FNiagaraTypeDefinition::GetVec4Def())
        {
            if (!JsonToFloats(V, 4, f)) { Err = TEXT("vector4 needs a 4-element array"); return false; }
            Store.SetParameterValue(FVector4f(f[0], f[1], f[2], f[3]), Var, false); return true; // VERIFY: GetVec4Def() underlying is FVector4f
        }
        if (T == FNiagaraTypeDefinition::GetColorDef())
        {
            if (!JsonToFloats(V, 4, f)) { Err = TEXT("linearcolor needs a 4-element array [r,g,b,a]"); return false; }
            Store.SetParameterValue(FLinearColor(f[0], f[1], f[2], f[3]), Var, false); return true;
        }
        if (T == FNiagaraTypeDefinition::GetQuatDef())
        {
            if (!JsonToFloats(V, 4, f)) { Err = TEXT("quat needs a 4-element array [x,y,z,w]"); return false; }
            Store.SetParameterValue(FQuat4f(f[0], f[1], f[2], f[3]), Var, false); return true; // VERIFY: GetQuatDef() underlying is FQuat4f
        }
        Err = TEXT("unsupported user-parameter type for set");
        return false;
    }

    // Read the value at a known store offset back to a JSON value (for prev/removed capture).
    TSharedPtr<FJsonValue> StoreValueToJson(const FNiagaraParameterStore& Store, const FNiagaraTypeDefinition& T, int32 Offset)
    {
        const uint8* D = Store.GetParameterData(Offset); // VERIFY vs engine source: FNiagaraParameterStore::GetParameterData(int32) const -> const uint8*
        if (!D || Offset == INDEX_NONE) { return MakeShared<FJsonValueNull>(); }

        auto Floats = [](const uint8* Src, int32 N) -> TSharedPtr<FJsonValue>
        {
            TArray<TSharedPtr<FJsonValue>> A;
            const float* P = reinterpret_cast<const float*>(Src);
            for (int32 i = 0; i < N; ++i) { A.Add(MakeShared<FJsonValueNumber>(P[i])); }
            return MakeShared<FJsonValueArray>(A);
        };

        if (T == FNiagaraTypeDefinition::GetFloatDef()) { float v; FMemory::Memcpy(&v, D, sizeof(float)); return MakeShared<FJsonValueNumber>(v); }
        if (T == FNiagaraTypeDefinition::GetIntDef())   { int32 v; FMemory::Memcpy(&v, D, sizeof(int32)); return MakeShared<FJsonValueNumber>((double)v); }
        if (T == FNiagaraTypeDefinition::GetBoolDef())  { FNiagaraBool b; FMemory::Memcpy(&b, D, sizeof(FNiagaraBool)); return MakeShared<FJsonValueBoolean>(b.GetValue()); } // VERIFY: FNiagaraBool::GetValue()
        if (T == FNiagaraTypeDefinition::GetVec2Def())  { return Floats(D, 2); }
        if (T == FNiagaraTypeDefinition::GetVec3Def())  { return Floats(D, 3); }
        if (T == FNiagaraTypeDefinition::GetVec4Def())  { return Floats(D, 4); }
        if (T == FNiagaraTypeDefinition::GetColorDef()) { return Floats(D, 4); }
        if (T == FNiagaraTypeDefinition::GetQuatDef())  { return Floats(D, 4); }
        return MakeShared<FJsonValueNull>();
    }

    // Locate a stored user variable by full (prefixed) name. Returns true + fills type/offset.
    bool FindUserVar(const FNiagaraUserRedirectionParameterStore& Store, const FName& FullName,
                     FNiagaraTypeDefinition& OutType, int32& OutOffset)
    {
        // ReadParameterVariables() -> const array of FNiagaraVariableWithOffset (name+type+offset).
        for (const FNiagaraVariableWithOffset& VO : Store.ReadParameterVariables()) // VERIFY vs engine source: FNiagaraParameterStore::ReadParameterVariables() -> TConstArrayView/TArray<FNiagaraVariableWithOffset>
        {
            if (VO.GetName() == FullName) // VERIFY: FNiagaraVariableWithOffset::GetName() (from FNiagaraVariableBase) + ::Offset member
            {
                OutType = VO.GetType();   // VERIFY: FNiagaraVariableWithOffset::GetType()
                OutOffset = VO.Offset;
                return true;
            }
        }
        return false;
    }

    // Shared local: find a mutable emitter handle by name. Returns nullptr if not found.
    // (GetEmitterHandle(int32) is the NON-const accessor; GetEmitterHandles() is the const array used
    //  by C++ #5 to enumerate. We enumerate to find the index, then take the mutable handle.)
    FNiagaraEmitterHandle* MCP_FindEmitterHandleByName(UNiagaraSystem* System, const FString& Name)
    {
#if WITH_EDITOR
        if (!System)
        {
            return nullptr;
        }
        const TArray<FNiagaraEmitterHandle>& Handles = System->GetEmitterHandles(); // VERIFY vs engine source: NiagaraSystem.h — GetEmitterHandles() -> const TArray<FNiagaraEmitterHandle>&
        for (int32 i = 0; i < Handles.Num(); ++i)
        {
            if (Handles[i].GetName().ToString() == Name)                            // VERIFY vs engine source: NiagaraEmitterHandle.h — FNiagaraEmitterHandle::GetName() -> FName
            {
                return &System->GetEmitterHandle(i);                               // VERIFY vs engine source: NiagaraSystem.h — GetEmitterHandle(int32) -> FNiagaraEmitterHandle& (non-const)
            }
        }
#endif
        return nullptr;
    }

    // ---- C++ #10 helpers ----
    // Map the case-insensitive ScriptUsage string to the engine enum for the four EMITTER-level stacks.
    // VERIFY vs engine source: ENiagaraScriptUsage enumerators (NiagaraCommon.h / NiagaraScript.h).
    bool ParseScriptUsage(const FString& In, ENiagaraScriptUsage& Out)
    {
        const FString U = In.ToLower();
        if (U == TEXT("particle_spawn"))  { Out = ENiagaraScriptUsage::ParticleSpawnScript;  return true; }
        if (U == TEXT("particle_update")) { Out = ENiagaraScriptUsage::ParticleUpdateScript; return true; }
        if (U == TEXT("emitter_spawn"))   { Out = ENiagaraScriptUsage::EmitterSpawnScript;   return true; }
        if (U == TEXT("emitter_update"))  { Out = ENiagaraScriptUsage::EmitterUpdateScript;  return true; }
        return false;
    }

    // Resolve the emitter's SHARED graph (all four emitter scripts share one UNiagaraScriptSource->NodeGraph)
    // from a UNiagaraSystem + emitter handle name. Returns nullptr + fills OutErr on failure.
    UNiagaraGraph* ResolveEmitterGraph(UNiagaraSystem* System, const FString& EmitterName, FString& OutErr)
    {
        for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles()) // VERIFY: GetEmitterHandles()
        {
            if (H.GetName().ToString() != EmitterName) continue;           // VERIFY: FNiagaraEmitterHandle::GetName()

            // VERIFY: FNiagaraEmitterHandle::GetInstance() -> FVersionedNiagaraEmitter (5.8 versioned wrapper).
            FVersionedNiagaraEmitter VEmitter = H.GetInstance();
            // VERIFY: FVersionedNiagaraEmitter::GetEmitterData() -> FVersionedNiagaraEmitterData*.
            FVersionedNiagaraEmitterData* Data = VEmitter.GetEmitterData();
            if (!Data) { OutErr = TEXT("emitter has no versioned data"); return nullptr; }

            // VERIFY: FVersionedNiagaraEmitterData::SpawnScriptProps.Script (FNiagaraEmitterScriptProperties::Script).
            // Any emitter script works — they share one graph — SpawnScript is always present.
            UNiagaraScript* AnyScript = Data->SpawnScriptProps.Script;
            if (!AnyScript) { OutErr = TEXT("emitter spawn script missing"); return nullptr; }

            // VERIFY: UNiagaraScript::GetLatestSource() -> UNiagaraScriptSourceBase* ; concrete type is
            // UNiagaraScriptSource (NiagaraEditor). Older builds used GetSource(); confirm which exists.
            UNiagaraScriptSource* Source = Cast<UNiagaraScriptSource>(AnyScript->GetLatestSource());
            if (!Source) { OutErr = TEXT("script source is not a graph-backed UNiagaraScriptSource"); return nullptr; }

            UNiagaraGraph* Graph = Source->NodeGraph;   // VERIFY: UNiagaraScriptSource::NodeGraph (UPROPERTY, public)
            if (!Graph) { OutErr = TEXT("script source has no NodeGraph"); return nullptr; }
            return Graph;
        }
        OutErr = FString::Printf(TEXT("no emitter handle named '%s'"), *EmitterName);
        return nullptr;
    }

    // ---- STAGED C++ #10 helper: synchronous Niagara compile ----
    // Returns true if, after the (optional) wait, no compilation requests remain outstanding.
    bool CompileNiagaraSystemImpl(UNiagaraSystem* System, bool bWait)
    {
    #if WITH_EDITOR
        if (!System)
        {
            return false;
        }

        // Propagate the structural mutation first; on a NiagaraSystem this also kicks a recompile,
        // which the WaitForCompilationComplete below will then block on.
        System->PostEditChange();                                       // VERIFY vs engine source: NiagaraSystem.h (UObject::PostEditChange override)

        // Kick a compile of any invalidated scripts. RequestCompile is async on its own.
        System->RequestCompile(/*bForce=*/false);                       // VERIFY vs engine source: NiagaraSystem.h  RequestCompile(bool, FNiagaraSystemUpdateContext* = nullptr)  (some 5.x builds return bool)

        if (bWait)
        {
            // Blocking, synchronous wait — this is the call that realizes ALL compiled/DDC data (and its
            // custom versions) BEFORE any save can begin, which is the actual fix for the linker error.
            // Args: (bIncludingGPUShaders=false, bShowProgress=false). GPU shaders are not needed for the
            // package save and waiting on them can add significant latency (see RISK NOTES).
            System->WaitForCompilationComplete(/*bIncludingGPUShaders=*/false, /*bShowProgress=*/false); // VERIFY vs engine source: NiagaraSystem.h  WaitForCompilationComplete(bool,bool)
        }

        // Report whether the system is settled. HasOutstandingCompilationRequests() is the non-mutating
        // status check; PollForCompilationComplete() also flushes and is an alternative.
        const bool bCompiled = !System->HasOutstandingCompilationRequests(/*bIncludingGPUShaders=*/false); // VERIFY vs engine source: NiagaraSystem.h  HasOutstandingCompilationRequests(bool) const
        return bCompiled;
    #else
        return false;
    #endif
    }
}

// ---- C++ #10 2026-08-15 (Niagara USER-PARAMETER authoring) — AUTHORED, NOT COMPILED. ---------------
// The exposed-parameter store API is version-sensitive; every "VERIFY" line is one to confirm against
// NiagaraSystem.h / NiagaraParameterStore.h / NiagaraUserRedirectionParameterStore.h on the Windows build.

FString UMCPReflectionLibrary::AddNiagaraUserParameter(UNiagaraSystem* System, const FString& ParamName, const FString& TypeName)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }
    if (ParamName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty param name")));
    }
    FNiagaraTypeDefinition Type;
    if (!NiagaraTypeFromName(TypeName, Type))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("unsupported type '%s'"), *TypeName)));
    }

    FNiagaraUserRedirectionParameterStore& Store = System->GetExposedParameters(); // VERIFY vs engine source: UNiagaraSystem::GetExposedParameters() -> FNiagaraUserRedirectionParameterStore&
    const FName FullName = MakeUserParamName(ParamName);
    const FNiagaraVariable Var(Type, FullName);

    // Refuse a duplicate (AddParameter would no-op but we want a clear signal).
    FNiagaraTypeDefinition ExistingType; int32 ExistingOffset = INDEX_NONE;
    if (FindUserVar(Store, FullName, ExistingType, ExistingOffset))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("user parameter '%s' already exists"), *FullName.ToString())));
    }

    // The redirection store's AddParameter registers the User.<->redirect mapping and zero-initializes.
    const bool bAdded = Store.AddParameter(Var, /*bInitInterfaces=*/true, /*bTriggerAsIfNew=*/true, /*OutOffset=*/nullptr); // VERIFY vs engine source: FNiagaraUserRedirectionParameterStore::AddParameter(const FNiagaraVariable&, bool, bool, int32*)
    if (!bAdded)
    {
        return SerializeJson(ErrorObj(TEXT("AddParameter returned false")));
    }
    // Windows fix: RecreateRedirections() is not exported (NIAGARA_API-less → link error). It's a full-table
    // rebuild that's redundant here — AddParameter() already registers this param's redirect (store .cpp:104),
    // and the system rebuilds all redirects on load / PostEditChange (NiagaraSystem.cpp:1517).
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("param"), FullName.ToString());
    Root->SetStringField(TEXT("type"), NiagaraTypeToName(Type));
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::SetNiagaraUserParameterValue(UNiagaraSystem* System, const FString& ParamName, const FString& ValueJson)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }

    FNiagaraUserRedirectionParameterStore& Store = System->GetExposedParameters();
    const FName FullName = MakeUserParamName(ParamName);

    FNiagaraTypeDefinition Type; int32 Offset = INDEX_NONE;
    if (!FindUserVar(Store, FullName, Type, Offset))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("no user parameter '%s'"), *FullName.ToString())));
    }

    // Parse ValueJson as a bare JSON value (scalar or array), not an object.
    TSharedPtr<FJsonValue> Val;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ValueJson);
    if (!FJsonSerializer::Deserialize(Reader, Val) || !Val.IsValid()) // VERIFY: FJsonSerializer::Deserialize(Reader, TSharedPtr<FJsonValue>&) overload
    {
        return SerializeJson(ErrorObj(TEXT("invalid value JSON")));
    }

    // Capture the PRIOR value (for undo) BEFORE overwriting.
    const TSharedPtr<FJsonValue> Prev = StoreValueToJson(Store, Type, Offset);

    const FNiagaraVariable Var(Type, FullName);
    FString ApplyErr;
    if (!ApplyJsonValueToStore(Store, Var, Val, ApplyErr))
    {
        return SerializeJson(ErrorObj(ApplyErr.IsEmpty() ? TEXT("failed to set value") : ApplyErr));
    }
    Store.OnParameterChange(); // VERIFY vs engine source: FNiagaraParameterStore::OnParameterChange()/MarkParameterDirty — notify layout; drop this line if the accessor differs
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("param"), FullName.ToString());
    Root->SetBoolField(TEXT("set"), true);
    Root->SetField(TEXT("prev"), Prev.IsValid() ? Prev : MakeShared<FJsonValueNull>());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveNiagaraUserParameter(UNiagaraSystem* System, const FString& ParamName)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }

    FNiagaraUserRedirectionParameterStore& Store = System->GetExposedParameters();
    const FName FullName = MakeUserParamName(ParamName);

    FNiagaraTypeDefinition Type; int32 Offset = INDEX_NONE;
    const bool bFound = FindUserVar(Store, FullName, Type, Offset);

    FString TypeStr;
    TSharedPtr<FJsonValue> ValJson = MakeShared<FJsonValueNull>();
    if (bFound)
    {
        // Capture type + current value BEFORE removal (for the re-add inverse).
        TypeStr = NiagaraTypeToName(Type);
        ValJson = StoreValueToJson(Store, Type, Offset);

        const FNiagaraVariable Var(Type, FullName);
        Store.RemoveParameter(Var);      // VERIFY vs engine source: FNiagaraUserRedirectionParameterStore::RemoveParameter(const FNiagaraVariableBase&)
        // Windows fix: RecreateRedirections() not exported (link error); redundant — RemoveParameter() already
        // drops this param's redirect entry (store .cpp:129).
        System->MarkPackageDirty();
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("param"), FullName.ToString());
    Root->SetBoolField(TEXT("removed"), bFound);
    if (bFound)
    {
        Root->SetStringField(TEXT("type"), TypeStr);
        Root->SetField(TEXT("value"), ValJson);
    }
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #10 2026-08-15 (Niagara emitter-handle rename + renderer authoring). ------------------
// Renderers live on the emitter's VERSIONED data. Path: emitter handle -> GetInstance()
// (FVersionedNiagaraEmitter{ Emitter*, Version }) -> GetEmitterData() (FVersionedNiagaraEmitterData*).
// Renderers are added/removed on that data with the version GUID. Every VERIFY line is version-
// sensitive (the versioned-emitter renderer API changed across 5.x) — confirm on the Windows build.

FString UMCPReflectionLibrary::RenameNiagaraEmitterHandle(UNiagaraSystem* System, const FString& OldName, const FString& NewName)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }
    if (NewName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty new_name")));
    }

    FNiagaraEmitterHandle* Handle = MCP_FindEmitterHandleByName(System, OldName);
    if (!Handle)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("no emitter handle named '%s'"), *OldName)));
    }

    // VERIFY vs engine source: NiagaraEmitterHandle.h — the setter signature CHANGED across versions.
    // 5.x form takes the owning system so it can enforce name-uniqueness:
    //     void FNiagaraEmitterHandle::SetName(FName InName, UNiagaraSystem& InOwnerSystem)
    // Older form was just SetName(FName). If this engine build only has the 1-arg form, drop *System.
    Handle->SetName(FName(*NewName), *System);

    System->RequestCompile(false);   // VERIFY vs engine source: NiagaraSystem.h — RequestCompile(bool). Mirrors C++ #5; keeps the asset consistent/saveable after a handle edit.
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetBoolField(TEXT("renamed"), true);
    Root->SetStringField(TEXT("old_name"), OldName);
    // Read the name back off the handle (the system may have de-duplicated it) so the caller/ledger
    // records the REAL applied name rather than the requested one.
    Root->SetStringField(TEXT("new_name"), Handle->GetName().ToString());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddNiagaraRenderer(UNiagaraSystem* System, const FString& EmitterName, const FString& RendererType)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }

    FNiagaraEmitterHandle* Handle = MCP_FindEmitterHandleByName(System, EmitterName);
    if (!Handle)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName)));
    }

    // Versioned emitter instance + its data (where renderers live).
    FVersionedNiagaraEmitter Instance = Handle->GetInstance();                     // VERIFY vs engine source: NiagaraEmitterHandle.h — GetInstance() -> FVersionedNiagaraEmitter{ TObjectPtr<UNiagaraEmitter> Emitter; FGuid Version; }
    FVersionedNiagaraEmitterData* EmitterData = Instance.GetEmitterData();         // VERIFY vs engine source: NiagaraEmitter.h — FVersionedNiagaraEmitter::GetEmitterData() -> FVersionedNiagaraEmitterData*
    UNiagaraEmitter* EmitterAsset = Instance.Emitter;                              // VERIFY vs engine source: NiagaraEmitter.h — FVersionedNiagaraEmitter::Emitter (outer for the new renderer)
    if (!EmitterData || !EmitterAsset)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("emitter '%s' has no versioned data"), *EmitterName)));
    }

    // Pick the renderer-properties subclass by name (case-insensitive).
    const FString Type = RendererType.ToLower();
    UClass* RendererClass = nullptr;
    if (Type == TEXT("sprite"))       { RendererClass = UNiagaraSpriteRendererProperties::StaticClass(); }
    else if (Type == TEXT("mesh"))    { RendererClass = UNiagaraMeshRendererProperties::StaticClass(); }
    else if (Type == TEXT("ribbon"))  { RendererClass = UNiagaraRibbonRendererProperties::StaticClass(); }
    else if (Type == TEXT("light"))   { RendererClass = UNiagaraLightRendererProperties::StaticClass(); }
    else
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("unknown renderer_type '%s' (want sprite|mesh|ribbon|light)"), *RendererType)));
    }

    // Construct with the emitter asset as outer + a transactional flag so it participates in the asset.
    UNiagaraRendererProperties* Renderer = NewObject<UNiagaraRendererProperties>(
        EmitterAsset, RendererClass, NAME_None, RF_Transactional);
    if (!Renderer)
    {
        return SerializeJson(ErrorObj(TEXT("failed to construct renderer properties")));
    }

    // VERIFY vs engine source: NiagaraEmitter.h — FVersionedNiagaraEmitterData::AddRenderer(
    //     UNiagaraRendererProperties*, FGuid EmitterVersion). Some 5.x builds take only the renderer
    //     (no version arg); if so, drop Instance.Version.
    EmitterAsset->AddRenderer(Renderer, Instance.Version);   // Windows fix: AddRenderer is on UNiagaraEmitter, not FVersionedNiagaraEmitterData

    System->RequestCompile(false);   // VERIFY vs engine source: NiagaraSystem.h — RequestCompile(bool). Rebuild so the renderer is live + keeps the asset saveable (see RISK).
    System->MarkPackageDirty();

    const int32 Count = EmitterData->GetRenderers().Num();                         // VERIFY vs engine source: NiagaraEmitter.h — GetRenderers() -> const TArray<UNiagaraRendererProperties*>&

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetBoolField(TEXT("added_renderer"), true);
    Root->SetStringField(TEXT("renderer_class"), Renderer->GetClass()->GetName());
    Root->SetNumberField(TEXT("renderer_count"), Count);   // index it landed at = renderer_count - 1 (append semantics)
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveNiagaraRenderer(UNiagaraSystem* System, const FString& EmitterName, int32 RendererIndex)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }

    FNiagaraEmitterHandle* Handle = MCP_FindEmitterHandleByName(System, EmitterName);
    if (!Handle)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName)));
    }

    FVersionedNiagaraEmitter Instance = Handle->GetInstance();                     // VERIFY vs engine source: NiagaraEmitterHandle.h — GetInstance()
    FVersionedNiagaraEmitterData* EmitterData = Instance.GetEmitterData();         // VERIFY vs engine source: NiagaraEmitter.h — GetEmitterData()
    if (!EmitterData)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("emitter '%s' has no versioned data"), *EmitterName)));
    }

    const TArray<UNiagaraRendererProperties*>& Renderers = EmitterData->GetRenderers(); // VERIFY vs engine source: NiagaraEmitter.h — GetRenderers()
    if (RendererIndex < 0 || RendererIndex >= Renderers.Num())
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("renderer index %d out of range (count %d)"), RendererIndex, Renderers.Num())));
    }

    UNiagaraRendererProperties* Target = Renderers[RendererIndex];
    const FString RemovedClass = Target ? Target->GetClass()->GetName() : FString(TEXT("None"));

    // VERIFY vs engine source: NiagaraEmitter.h — FVersionedNiagaraEmitterData::RemoveRenderer(
    //     UNiagaraRendererProperties*, FGuid EmitterVersion). As with AddRenderer, some builds omit
    //     the version arg — if so, drop Instance.Version.
    Instance.Emitter->RemoveRenderer(Target, Instance.Version);   // Windows fix: RemoveRenderer is on UNiagaraEmitter, not FVersionedNiagaraEmitterData

    System->RequestCompile(false);   // VERIFY vs engine source: NiagaraSystem.h — RequestCompile(bool).
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("removed_renderer_class"), RemovedClass);  // captured for best-effort undo re-add
    Root->SetNumberField(TEXT("renderer_count"), EmitterData->GetRenderers().Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddNiagaraModuleToStack(UNiagaraSystem* System, const FString& EmitterName,
                                                       const FString& ScriptUsage, const FString& ModuleScriptPath)
{
    // C++ #10 area C ENABLED via engine export patch (NiagaraGraph.h::FindOutputNode +
    // NiagaraStackGraphUtilities.h::AddScriptModuleToStack given NIAGARAEDITOR_API on the Windows source engine).
#if WITH_EDITOR
    if (!System) return SerializeJson(ErrorObj(TEXT("null system")));

    ENiagaraScriptUsage Usage;
    if (!ParseScriptUsage(ScriptUsage, Usage))
        return SerializeJson(ErrorObj(TEXT("bad usage (particle_spawn|particle_update|emitter_spawn|emitter_update)")));

    FString Err;
    UNiagaraGraph* Graph = ResolveEmitterGraph(System, EmitterName, Err);
    if (!Graph) return SerializeJson(ErrorObj(Err));

    // Load the module script asset.
    UNiagaraScript* ModuleScript = LoadObject<UNiagaraScript>(nullptr, *ModuleScriptPath);
    if (!ModuleScript) return SerializeJson(ErrorObj(FString::Printf(TEXT("module script not found: %s"), *ModuleScriptPath)));
    if (ModuleScript->GetUsage() != ENiagaraScriptUsage::Module)
        return SerializeJson(ErrorObj(TEXT("asset is not a Niagara Module script (usage != Module)")));

    UNiagaraNodeOutput* OutputNode = Graph->FindOutputNode(Usage);
    if (!OutputNode) return SerializeJson(ErrorObj(TEXT("no output node for that usage in the emitter graph")));

    UNiagaraNodeFunctionCall* NewNode =
        FNiagaraStackGraphUtilities::AddScriptModuleToStack(FAssetData(ModuleScript), *OutputNode, INDEX_NONE);
    if (!NewNode) return SerializeJson(ErrorObj(TEXT("AddScriptModuleToStack returned null")));

    Graph->NotifyGraphChanged();
    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("usage"), ScriptUsage.ToLower());
    Root->SetStringField(TEXT("added_module"), NewNode->GetFunctionName());
    Root->SetStringField(TEXT("node_guid"), NewNode->NodeGuid.ToString());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveNiagaraModuleFromStack(UNiagaraSystem* System, const FString& EmitterName,
                                                            const FString& ScriptUsage, const FString& NodeGuidStr)
{
    // C++ #10 area C ENABLED via engine export patch (NiagaraStackGraphUtilities.h::RemoveModuleFromStack
    // given NIAGARAEDITOR_API on the Windows source engine).
#if WITH_EDITOR
    if (!System) return SerializeJson(ErrorObj(TEXT("null system")));

    ENiagaraScriptUsage Usage;
    if (!ParseScriptUsage(ScriptUsage, Usage))
        return SerializeJson(ErrorObj(TEXT("bad usage")));

    FGuid Target;
    if (!FGuid::Parse(NodeGuidStr, Target))
        return SerializeJson(ErrorObj(TEXT("node_guid is not a valid GUID")));

    FString Err;
    UNiagaraGraph* Graph = ResolveEmitterGraph(System, EmitterName, Err);
    if (!Graph) return SerializeJson(ErrorObj(Err));

    TArray<UNiagaraNodeFunctionCall*> ModuleNodes;
    Graph->GetNodesOfClass<UNiagaraNodeFunctionCall>(ModuleNodes);

    UNiagaraNodeFunctionCall* Victim = nullptr;
    for (UNiagaraNodeFunctionCall* N : ModuleNodes)
    {
        if (N && N->NodeGuid == Target) { Victim = N; break; }
    }

    // Windows fix (crash): RemoveModuleFromStack HARD-asserts (checkf, NiagaraStackGraphUtilities.cpp:2381
    // "Owning script could not be found") when it can't resolve the owning script — which it can't with an
    // empty emitter handle id. The module lives on an EMITTER, so pass that emitter handle's REAL id.
    // The original /*EmitterHandleId*/FGuid() placeholder crashed the editor.
    FNiagaraEmitterHandle* Handle = MCP_FindEmitterHandleByName(System, EmitterName);
    if (!Handle) return SerializeJson(ErrorObj(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName)));
    const FGuid EmitterHandleId = Handle->GetId();

    bool bRemoved = false;
    if (Victim)
    {
        FNiagaraStackGraphUtilities::RemoveModuleFromStack(*System, EmitterHandleId, *Victim);
        Graph->NotifyGraphChanged();
        System->RequestCompile(false);
        System->MarkPackageDirty();
        bRemoved = true;
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetStringField(TEXT("node_guid"), NodeGuidStr);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// PARTIAL / FRAGILE — read the RISK NOTES before trusting this. Sets ONLY a scalar (float/int/bool) local
// constant input, by writing the module input pin's typed default. Faithful arbitrary-input setting is
// view-model-only (NOT-FEASIBLE-HEADLESS) and is deliberately NOT attempted here.
FString UMCPReflectionLibrary::SetNiagaraModuleInput(UNiagaraSystem* System, const FString& EmitterName,
                                                     const FString& ScriptUsage, const FString& ModuleName,
                                                     const FString& InputName, const FString& ValueJson)
{
#if WITH_EDITOR
    if (!System) return SerializeJson(ErrorObj(TEXT("null system")));

    ENiagaraScriptUsage Usage;
    if (!ParseScriptUsage(ScriptUsage, Usage))
        return SerializeJson(ErrorObj(TEXT("bad usage")));

    FString Err;
    UNiagaraGraph* Graph = ResolveEmitterGraph(System, EmitterName, Err);
    if (!Graph) return SerializeJson(ErrorObj(Err));

    // Locate the module function-call node by name.
    TArray<UNiagaraNodeFunctionCall*> ModuleNodes;
    Graph->GetNodesOfClass<UNiagaraNodeFunctionCall>(ModuleNodes);
    UNiagaraNodeFunctionCall* Module = nullptr;
    for (UNiagaraNodeFunctionCall* N : ModuleNodes)
    {
        // VERIFY: GetFunctionName() (node instance name) — match either that or the script's asset name.
        if (N && (N->GetFunctionName() == ModuleName ||
                  (N->FunctionScript && N->FunctionScript->GetName() == ModuleName))) // VERIFY: UNiagaraNodeFunctionCall::FunctionScript
        { Module = N; break; }
    }
    if (!Module) return SerializeJson(ErrorObj(FString::Printf(TEXT("no module named '%s' in that stack"), *ModuleName)));

    // Find the INPUT pin matching InputName. Niagara input pins carry the parameter name as the pin name.
    // VERIFY: iterating UEdGraphNode::Pins and EGPD_Input direction is valid; the pin PinName for a Niagara
    // module input is the input's parameter name (may be "Module.<InputName>" — try exact then suffix match).
    UEdGraphPin* Target = nullptr;
    for (UEdGraphPin* P : Module->Pins)
    {
        if (!P || P->Direction != EGPD_Input) continue;
        const FString PinName = P->PinName.ToString();
        if (PinName == InputName || PinName.EndsWith(FString(TEXT(".")) + InputName)) { Target = P; break; }
    }
    if (!Target) return SerializeJson(ErrorObj(FString::Printf(TEXT("no scalar input pin '%s' on module"), *InputName)));
    if (Target->LinkedTo.Num() > 0)
        return SerializeJson(ErrorObj(TEXT("input is connected/overridden via the map — needs the editor view-model (NOT supported headless)")));

    const FString PriorValue = Target->DefaultValue;  // capture for undo

    // Best-effort scalar coercion from the JSON scalar. We set the RAW pin DefaultValue string; the Niagara
    // schema expects a specific text form per FNiagaraTypeDefinition. VERIFY vs engine: the Niagara pin default
    // string format (e.g. float "1.500000", int "3", bool "true"/"false"). If wrong, the compile keeps the old
    // value. A more correct route is UEdGraphSchema_Niagara::TrySetDefaultValue / a typed FNiagaraVariable ->
    // FNiagaraEditorUtilities::VariableToString, but that requires the Niagara schema/type plumbing (heavier,
    // and still scalar-limited).
    FString Coerced = ValueJson;
    Coerced.TrimStartAndEndInline();
    Coerced.ReplaceInline(TEXT("\""), TEXT(""));      // tolerate a quoted scalar

    Target->Modify();                                 // VERIFY: UEdGraphPin::Modify()
    Target->DefaultValue = Coerced;
    Graph->NotifyGraphChanged();
    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetStringField(TEXT("module"), ModuleName);
    Root->SetStringField(TEXT("input"), InputName);
    Root->SetBoolField(TEXT("set"), true);
    Root->SetStringField(TEXT("prior_value"), PriorValue);   // for the undo ledger
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- C++ #10 2026-08-15 (Niagara compile+save fix) — AUTHORED, NOT YET COMPILED. -----------------
// Version-sensitive Niagara compile calls are in CompileNiagaraSystemImpl (anonymous namespace).

FString UMCPReflectionLibrary::CompileNiagaraSystem(UNiagaraSystem* System, bool bWaitForCompletion)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }
    if (!System->IsA(UNiagaraSystem::StaticClass()))   // defensive; UFUNCTION already types it
    {
        return SerializeJson(ErrorObj(TEXT("not a NiagaraSystem")));
    }

    const bool bCompiled = CompileNiagaraSystemImpl(System, bWaitForCompletion);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetBoolField(TEXT("waited"), bWaitForCompletion);
    Root->SetBoolField(TEXT("compiled"), bCompiled);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::SaveNiagaraSystem(UNiagaraSystem* System)
{
#if WITH_EDITOR
    if (!System)
    {
        return SerializeJson(ErrorObj(TEXT("null system")));
    }
    if (!System->IsA(UNiagaraSystem::StaticClass()))
    {
        return SerializeJson(ErrorObj(TEXT("not a NiagaraSystem")));
    }

    // 1) Realize all compiled data synchronously (the fix). PostEditChange happens inside the helper.
    const bool bCompiled = CompileNiagaraSystemImpl(System, /*bWait=*/true);
    System->MarkPackageDirty();

    // 2) Save the package in C++ (Python's save path fails on the freshly-mutated asset).
    UPackage* Pkg = System->GetOutermost();                              // VERIFY vs engine source: UObject.h GetOutermost() (== GetPackage() for a top-level asset)
    if (!Pkg)
    {
        return SerializeJson(ErrorObj(TEXT("no package")));
    }

    const FString PackageName = Pkg->GetName();
    const FString FileName = FPackageName::LongPackageNameToFilename(  // VERIFY vs engine source: Misc/PackageName.h
        PackageName, FPackageName::GetAssetPackageExtension());        // VERIFY vs engine source: Misc/PackageName.h  GetAssetPackageExtension() -> ".uasset"

    FSavePackageArgs Args;                                             // VERIFY vs engine source: UObject/SavePackage.h (fields match Materials/Blueprints handlers)
    Args.TopLevelFlags = RF_Public | RF_Standalone;
    Args.SaveFlags = SAVE_NoError;

    // Windows fix: UPackage::SavePackage returns bool in this engine build (not FSavePackageResultStruct).
    const bool bSaved = UPackage::SavePackage(Pkg, System, *FileName, Args);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("package"), PackageName);
    Root->SetBoolField(TEXT("compiled"), bCompiled);
    Root->SetBoolField(TEXT("saved"), bSaved);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ================= C++ #11 (BehaviorTree editor-graph) — AUTHORED on Mac, NOT YET COMPILED on Windows. =================
// Reconstructs UBehaviorTreeGraph from the runtime RootNode so an MCP-authored BT is editor-round-trippable.
// TOP RISK: BehaviorTreeEditor/AIGraph editor symbols may lack BEHAVIORTREEEDITOR_API/AIGRAPH_API export ->
// link failure -> source-engine export patch needed (same as the Niagara NIAGARAEDITOR_API fix). Every
// version-sensitive call is "VERIFY vs engine source"-tagged.
#if WITH_EDITOR
namespace
{
    // Create one graph node of type T in Graph, bind it to RuntimeNode, finalize (allocates pins).
    template <typename T>
    static T* BT_MakeGraphNode(UBehaviorTreeGraph& Graph, UObject* RuntimeNode, int32 PosX, int32 PosY)
    {
        FGraphNodeCreator<T> Creator(Graph);           // VERIFY vs engine source: EdGraph/EdGraph.h (FGraphNodeCreator template)
        T* GNode = Creator.CreateNode(/*bSelectNewNode=*/false);
        GNode->NodeInstance = RuntimeNode;             // VERIFY vs engine source: AIGraph/Classes/AIGraphNode.h (UAIGraphNode::NodeInstance is a public UPROPERTY)
        if (RuntimeNode)
        {
            GNode->ClassData = FGraphNodeClassData(RuntimeNode->GetClass(), FString());  // VERIFY vs engine source: AIGraph/Classes/AIGraphTypes.h (FGraphNodeClassData ctor(UClass*,FString))
        }
        GNode->NodePosX = PosX;
        GNode->NodePosY = PosY;
        Creator.Finalize();                            // runs AllocateDefaultPins() + registers the node
        return GNode;
    }

    // Link parent exec-out -> child exec-in.
    static void BT_LinkExec(UBehaviorTreeGraphNode* Parent, UBehaviorTreeGraphNode* Child)
    {
        if (!Parent || !Child) { return; }
        UEdGraphPin* Out = Parent->GetOutputPin();     // VERIFY vs engine source: BehaviorTreeEditor/Classes/BehaviorTreeGraphNode.h (GetOutputPin())
        UEdGraphPin* In  = Child->GetInputPin();        // VERIFY vs engine source: BehaviorTreeEditor/Classes/BehaviorTreeGraphNode.h (GetInputPin())
        if (Out && In)
        {
            Out->MakeLinkTo(In);                       // VERIFY vs engine source: EdGraph/EdGraphPin.h
        }
    }

    // Recursively build graph nodes for a runtime composite subtree. Returns the graph node for Comp.
    static UBehaviorTreeGraphNode* BT_BuildComposite(UBehaviorTreeGraph& Graph, UBTCompositeNode* Comp,
                                                     int32 Depth, int32& OutNodeCount,
                                                     int32& OutDecoCount, int32& OutSvcCount)
    {
        UBehaviorTreeGraphNode_Composite* GComp =
            BT_MakeGraphNode<UBehaviorTreeGraphNode_Composite>(Graph, Comp, Depth * 300, OutNodeCount * 120);
        ++OutNodeCount;

        // Services on the composite -> subnodes.
        for (UBTService* Svc : Comp->Services)          // VERIFY vs engine source: BehaviorTree/BTCompositeNode.h (UBTCompositeNode::Services)
        {
            if (!Svc) { continue; }
            UBehaviorTreeGraphNode_Service* GSvc =
                BT_MakeGraphNode<UBehaviorTreeGraphNode_Service>(Graph, Svc, 0, 0);
            GComp->AddSubNode(GSvc, &Graph);           // VERIFY vs engine source: AIGraph/Classes/AIGraphNode.h (AddSubNode(UAIGraphNode*, UEdGraph*))
            ++OutSvcCount;
        }

        // Child slots. FBTCompositeChild has ChildComposite / ChildTask / Decorators.
        for (const FBTCompositeChild& Slot : Comp->Children)   // VERIFY vs engine source: BehaviorTree/BTCompositeNode.h (FBTCompositeChild members)
        {
            UBehaviorTreeGraphNode* GChild = nullptr;
            if (Slot.ChildComposite)
            {
                GChild = BT_BuildComposite(Graph, Slot.ChildComposite, Depth + 1,
                                           OutNodeCount, OutDecoCount, OutSvcCount);
            }
            else if (Slot.ChildTask)
            {
                GChild = BT_MakeGraphNode<UBehaviorTreeGraphNode_Task>(Graph, Slot.ChildTask,
                                           (Depth + 1) * 300, OutNodeCount * 120);
                ++OutNodeCount;
            }
            if (!GChild) { continue; }

            // Child-slot decorators -> subnodes on the guarded child node.
            for (UBTDecorator* Dec : Slot.Decorators)
            {
                if (!Dec) { continue; }
                UBehaviorTreeGraphNode_Decorator* GDec =
                    BT_MakeGraphNode<UBehaviorTreeGraphNode_Decorator>(Graph, Dec, 0, 0);
                GChild->AddSubNode(GDec, &Graph);      // VERIFY vs engine source: AIGraph AddSubNode; decorator sub-node placement
                ++OutDecoCount;
            }

            BT_LinkExec(GComp, GChild);
        }
        return GComp;
    }
}
#endif // WITH_EDITOR

FString UMCPReflectionLibrary::SyncBehaviorTreeEditorGraph(UBehaviorTree* BehaviorTree)
{
#if WITH_EDITOR
    if (!BehaviorTree)
    {
        return SerializeJson(ErrorObj(TEXT("null behavior_tree")));
    }

    UBTCompositeNode* Root = BehaviorTree->RootNode;   // VERIFY vs engine source: BehaviorTree/BehaviorTree.h (UBehaviorTree::RootNode public)
    if (!Root)
    {
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("behavior_tree"), BehaviorTree->GetName());
        R->SetNumberField(TEXT("nodes_created"), 0);
        R->SetBoolField(TEXT("graph_present"), BehaviorTree->BTGraph != nullptr);
        return SerializeJson(R);
    }

    // (Re)create the editor graph, mirroring FBehaviorTreeEditor's own init path.
    UBehaviorTreeGraph* Graph = Cast<UBehaviorTreeGraph>(
        FBlueprintEditorUtils::CreateNewGraph(                                   // VERIFY vs engine source: Kismet2/BlueprintEditorUtils.h (CreateNewGraph signature)
            BehaviorTree, TEXT("Behavior Tree"),
            UBehaviorTreeGraph::StaticClass(),                                   // VERIFY vs engine source: BehaviorTreeEditor export (BEHAVIORTREEEDITOR_API) — TOP RISK
            UEdGraphSchema_BehaviorTree::StaticClass()));                        // VERIFY vs engine source: BehaviorTreeEditor export — TOP RISK
    if (!Graph)
    {
        return SerializeJson(ErrorObj(TEXT("failed to create UBehaviorTreeGraph")));
    }
    Graph->bAllowDeletion = false;                                              // VERIFY vs engine source: UEdGraph::bAllowDeletion
    BehaviorTree->BTGraph = Graph;

    UBehaviorTreeGraphNode_Root* GRoot =
        BT_MakeGraphNode<UBehaviorTreeGraphNode_Root>(*Graph, nullptr, 0, 0);   // VERIFY vs engine source: BehaviorTreeGraphNode_Root.h export

    int32 NodeCount = 0, DecoCount = 0, SvcCount = 0;
    UBehaviorTreeGraphNode* GRootComposite =
        BT_BuildComposite(*Graph, Root, 1, NodeCount, DecoCount, SvcCount);
    BT_LinkExec(GRoot, GRootComposite);

    // Regenerate RootNode from the wired graph, REUSING our NodeInstances (Outer is the BT). If this build's
    // CreateBTFromGraph DUPLICATES instead, drop the next two lines (see handoff "Mitigation switch").
    Graph->OnCreated();                                                         // VERIFY vs engine source: UBehaviorTreeGraph::OnCreated()
    Graph->UpdateAsset(/*UpdateFlags=*/0);                                      // VERIFY vs engine source: UBehaviorTreeGraph::UpdateAsset(int32) + reuse semantics — TOP RISK
    Graph->UpdateBlackboardChange();                                            // VERIFY vs engine source: UBehaviorTreeGraph::UpdateBlackboardChange()

    BehaviorTree->MarkPackageDirty();

    TSharedRef<FJsonObject> Root2 = MakeShared<FJsonObject>();
    Root2->SetStringField(TEXT("behavior_tree"), BehaviorTree->GetName());
    Root2->SetNumberField(TEXT("nodes_created"), NodeCount);
    Root2->SetNumberField(TEXT("decorators"), DecoCount);
    Root2->SetNumberField(TEXT("services"), SvcCount);
    Root2->SetBoolField(TEXT("graph_present"), true);
    return SerializeJson(Root2);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ============================================================================================
// ==== C++ #12 2026-08-16 — deferred-reflection READER batch ================================
//   (A) GetEnvQueryConfigJson  — EQS node config VALUES via FProperty walk (no Build.cs change)
//   (B) GetStateTreeNodeRegistryJson — native StateTree node structs (Build.cs += StateTreeModule)
//   (C) GetControlRigVMJson    — compiled RigVM summary via generated-class CDO (Build.cs += RigVM)
//   (D) GetRigVMStructPinsJson — per-struct RigVM pin schema (CoreUObject reflection only)
// The StateTree editor property-BINDINGS resolver is deferred (needs StateTreeEditorModule + likely
// an *_API export patch). Every version-sensitive call below is tagged "VERIFY vs engine source".
// AUTHORED on Mac, NOT YET COMPILED. Clean-room from public-API knowledge.
// ============================================================================================

namespace
{
    // ---- (A) EQS helpers ------------------------------------------------------------------
    // Hardened by C++ #17 (crash mode #3): reading a freshly-authored EQS option/test could
    // hang ~60s then AV. We add a depth cap, a cycle guard (visited struct/UObject addresses),
    // a per-node element cap, and safe object-pointer validation. Output shape is IDENTICAL
    // for valid/shallow/acyclic data; only pathological graphs get a marker instead of a crash.

    static constexpr int32 kEqsMaxDepth    = 8;    // struct/container recursion cap
    static constexpr int32 kEqsMaxElements = 256;  // props/array/set/map elements walked per node

    // Safe-to-deref predicate for a raw UObject* pulled from reflection.
    // VERIFY vs engine source: IsValid(const UObject*) [Object.h ~1886] and UObject::IsUnreachable()
    // [UObjectBaseUtility.h ~263] — both CoreUObject, confirmed present in this 5.8 build.
    bool EqsObjectIsSafe(const UObject* Obj)
    {
        return Obj != nullptr && IsValid(Obj) && !Obj->IsUnreachable();
    }

    // Reflective SCAN (no output): returns true iff the value graph rooted at (Prop,ValuePtr) is
    // finite within our caps, acyclic w.r.t. Visited, and every object pointer is safe to deref.
    // On false, OutReason is one of: "max-depth" | "cycle" | "max-elements" | "unsafe-ref".
    // Walks the same edges ExportTextItem_Direct would, so a "true" result means ExportText is safe.
    bool EqsIsValueSafe(const FProperty* Prop, const void* ValuePtr, int32 Depth,
                        TSet<const void*>& Visited, FString& OutReason)
    {
        if (!Prop || !ValuePtr) { return true; }
        if (Depth > kEqsMaxDepth) { OutReason = TEXT("max-depth"); return false; }

        // Object refs: validate but do NOT descend (matches path-only output form).
        if (const FObjectPropertyBase* ObjP = CastField<FObjectPropertyBase>(Prop))
        {
            UObject* Obj = ObjP->GetObjectPropertyValue(ValuePtr); // VERIFY vs engine source
            if (Obj != nullptr && !EqsObjectIsSafe(Obj)) { OutReason = TEXT("unsafe-ref"); return false; }
            return true;
        }

        // Struct-by-value: cycle-guard the instance address, then recurse its members.
        if (const FStructProperty* StructP = CastField<FStructProperty>(Prop))
        {
            bool bAlready = false;
            Visited.Add(ValuePtr, &bAlready);
            if (bAlready) { OutReason = TEXT("cycle"); return false; }
            if (StructP->Struct) // VERIFY vs engine source: FStructProperty::Struct (TObjectPtr<UScriptStruct>)
            {
                int32 Count = 0;
                for (TFieldIterator<FProperty> It(StructP->Struct); It; ++It)
                {
                    if (++Count > kEqsMaxElements) { OutReason = TEXT("max-elements"); return false; }
                    const void* MemberPtr = It->ContainerPtrToValuePtr<void>(ValuePtr);
                    if (!EqsIsValueSafe(*It, MemberPtr, Depth + 1, Visited, OutReason)) { return false; }
                }
            }
            return true;
        }

        // Dynamic array.
        if (const FArrayProperty* ArrP = CastField<FArrayProperty>(Prop))
        {
            FScriptArrayHelper Helper(ArrP, ValuePtr); // VERIFY: ctor accepts const void* (confirmed 5.8)
            if (Helper.Num() > kEqsMaxElements) { OutReason = TEXT("max-elements"); return false; }
            for (int32 i = 0; i < Helper.Num(); ++i)
            {
                if (!EqsIsValueSafe(ArrP->Inner, Helper.GetRawPtr(i), Depth + 1, Visited, OutReason))
                {
                    return false;
                }
            }
            return true;
        }

        // Set.
        if (const FSetProperty* SetP = CastField<FSetProperty>(Prop))
        {
            FScriptSetHelper Helper(SetP, ValuePtr); // VERIFY: ctor const void* (confirmed 5.8)
            if (Helper.Num() > kEqsMaxElements) { OutReason = TEXT("max-elements"); return false; }
            for (int32 i = 0; i < Helper.GetMaxIndex(); ++i)
            {
                if (!Helper.IsValidIndex(i)) { continue; }
                if (!EqsIsValueSafe(Helper.GetElementProperty(), Helper.GetElementPtr(i),
                                    Depth + 1, Visited, OutReason))
                {
                    return false;
                }
            }
            return true;
        }

        // Map.
        if (const FMapProperty* MapP = CastField<FMapProperty>(Prop))
        {
            FScriptMapHelper Helper(MapP, ValuePtr); // VERIFY: ctor const void* (confirmed 5.8)
            if (Helper.Num() > kEqsMaxElements) { OutReason = TEXT("max-elements"); return false; }
            for (int32 i = 0; i < Helper.GetMaxIndex(); ++i)
            {
                if (!Helper.IsValidIndex(i)) { continue; }
                if (!EqsIsValueSafe(Helper.GetKeyProperty(), Helper.GetKeyPtr(i),
                                    Depth + 1, Visited, OutReason)) { return false; }
                if (!EqsIsValueSafe(Helper.GetValueProperty(), Helper.GetValuePtr(i),
                                    Depth + 1, Visited, OutReason)) { return false; }
            }
            return true;
        }

        // Primitives / name / text / delegates: inherently finite & safe.
        return true;
    }

    // Small helper to build a {"_marker": value} object for a tripped guard.
    TSharedPtr<FJsonValue> EqsMarker(const FString& Reason)
    {
        TSharedRef<FJsonObject> M = MakeShared<FJsonObject>();
        if (Reason == TEXT("cycle"))            { M->SetBoolField(TEXT("_cycle"), true); }
        else if (Reason == TEXT("unsafe-ref"))  { M->SetBoolField(TEXT("_unsafe_ref"), true); }
        else                                     { M->SetStringField(TEXT("_truncated"), Reason); } // max-depth | max-elements
        return MakeShared<FJsonValueObject>(M);
    }

    // One UPROPERTY value -> a JSON value. Order matters: FByteProperty derives from FNumericProperty
    // (enum-aware) so it is tested first; FEnumProperty is standalone.
    // Depth/Visited added by C++ #17 so the struct/array/map fallback is bounded & crash-proof.
    TSharedPtr<FJsonValue> EqsPropertyToJson(FProperty* Prop, const void* ValuePtr,
                                             int32 Depth, TSet<const void*>& Visited)
    {
        if (const FBoolProperty* BoolP = CastField<FBoolProperty>(Prop))
        {
            return MakeShared<FJsonValueBoolean>(BoolP->GetPropertyValue(ValuePtr));
        }
        if (const FEnumProperty* EnumP = CastField<FEnumProperty>(Prop))
        {
            FNumericProperty* Underlying = EnumP->GetUnderlyingProperty();
            const int64 Val = Underlying ? Underlying->GetSignedIntPropertyValue(ValuePtr) : 0;
            UEnum* Enum = EnumP->GetEnum();
            return MakeShared<FJsonValueString>(Enum ? Enum->GetNameStringByValue(Val) : LexToString(Val));
        }
        if (const FByteProperty* ByteP = CastField<FByteProperty>(Prop)) // incl. TEnumAsByte
        {
            const uint8 Val = ByteP->GetPropertyValue(ValuePtr);
            if (ByteP->Enum) // VERIFY vs engine source: FByteProperty::Enum member name
            {
                return MakeShared<FJsonValueString>(ByteP->Enum->GetNameStringByValue((int64)Val));
            }
            return MakeShared<FJsonValueNumber>((double)Val);
        }
        if (const FNumericProperty* NumP = CastField<FNumericProperty>(Prop))
        {
            if (NumP->IsFloatingPoint())
            {
                return MakeShared<FJsonValueNumber>(NumP->GetFloatingPointPropertyValue(ValuePtr));
            }
            return MakeShared<FJsonValueNumber>((double)NumP->GetSignedIntPropertyValue(ValuePtr));
        }
        if (const FStrProperty* StrP = CastField<FStrProperty>(Prop))
        {
            return MakeShared<FJsonValueString>(StrP->GetPropertyValue(ValuePtr));
        }
        if (const FNameProperty* NameP = CastField<FNameProperty>(Prop))
        {
            return MakeShared<FJsonValueString>(NameP->GetPropertyValue(ValuePtr).ToString());
        }
        if (const FTextProperty* TextP = CastField<FTextProperty>(Prop))
        {
            return MakeShared<FJsonValueString>(TextP->GetPropertyValue(ValuePtr).ToString());
        }
        if (const FObjectPropertyBase* ObjP = CastField<FObjectPropertyBase>(Prop)) // object/class/soft refs
        {
            UObject* Obj = ObjP->GetObjectPropertyValue(ValuePtr);
            // C++ #17: never GetPathName() a raw/stale pointer — validate first (the AV source).
            if (Obj != nullptr && !EqsObjectIsSafe(Obj))
            {
                return MakeShared<FJsonValueString>(TEXT("None")); // same shape as the null case
            }
            return MakeShared<FJsonValueString>(Obj ? Obj->GetPathName() : FString(TEXT("None")));
        }
        // Fallback: structs/arrays/sets/maps/delegates -> engine text form, but ONLY after a bounded
        // scan proves the graph is finite & every object pointer is safe. Otherwise emit a marker.
        // This is the hang/crash fix: ExportTextItem_Direct has no bound of ours; the scan gives it one.
        {
            FString Reason;
            if (!EqsIsValueSafe(Prop, ValuePtr, Depth, Visited, Reason))
            {
                return EqsMarker(Reason);
            }
        }
        FString Exported;
        Prop->ExportTextItem_Direct(Exported, ValuePtr, /*Default*/ nullptr, /*Parent*/ nullptr, PPF_None); // VERIFY vs engine source: ExportTextItem_Direct signature (UE5.1+, takes TNotNull<const void*>)
        return MakeShared<FJsonValueString>(Exported);
    }

    void EqsSerializeNodeConfig(UObject* Node, const TSharedRef<FJsonObject>& ConfigObj,
                                int32 Depth, TSet<const void*>& Visited)
    {
        if (!Node) { return; }
        int32 Count = 0;
        for (TFieldIterator<FProperty> It(Node->GetClass()); It; ++It)
        {
            if (++Count > kEqsMaxElements) // C++ #17: bound the property count per node
            {
                ConfigObj->SetField(TEXT("_truncated"), EqsMarker(TEXT("max-elements")));
                break;
            }
            FProperty* Prop = *It;
            const void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Node);
            ConfigObj->SetField(Prop->GetName(), EqsPropertyToJson(Prop, ValuePtr, Depth + 1, Visited));
        }
    }

    UObject* EqsReflectObject(UObject* Owner, const TCHAR* PropName)
    {
        if (!Owner) { return nullptr; }
        FObjectPropertyBase* P = FindFProperty<FObjectPropertyBase>(Owner->GetClass(), PropName);
        if (!P) { return nullptr; }
        UObject* Result = P->GetObjectPropertyValue_InContainer(Owner); // VERIFY vs engine source: FObjectPropertyBase::GetObjectPropertyValue_InContainer
        // C++ #17: hand back only safe-to-deref objects; a stale/garbage node ptr becomes null.
        return EqsObjectIsSafe(Result) ? Result : nullptr;
    }

    void EqsReflectObjectArray(UObject* Owner, const TCHAR* PropName, TArray<UObject*>& Out)
    {
        if (!Owner) { return; }
        FArrayProperty* Arr = FindFProperty<FArrayProperty>(Owner->GetClass(), PropName);
        if (!Arr) { return; }
        FObjectPropertyBase* Inner = CastField<FObjectPropertyBase>(Arr->Inner);
        if (!Inner) { return; }
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Owner));
        const int32 Num = FMath::Min(Helper.Num(), kEqsMaxElements); // C++ #17: cap runaway arrays
        for (int32 i = 0; i < Num; ++i)
        {
            UObject* Elem = Inner->GetObjectPropertyValue(Helper.GetRawPtr(i));
            // Keep positional index meaning: push validated object, or nullptr (reader emits name:"None").
            Out.Add(EqsObjectIsSafe(Elem) ? Elem : nullptr);
        }
    }

    // ---- (B) StateTree registry helper ----------------------------------------------------
    void AddStateTreeNodeRow(TArray<TSharedPtr<FJsonValue>>& Arr, UScriptStruct* S)
    {
        if (!S) { return; }
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("struct_path"), S->GetPathName());
        J->SetStringField(TEXT("cpp_name"), FString(S->GetPrefixCPP()) + S->GetName()); // VERIFY vs engine source (GetPrefixCPP)
#if WITH_EDITOR
        const FString Disp = S->GetMetaData(TEXT("DisplayName")); // VERIFY vs engine source (struct DisplayName meta)
        J->SetStringField(TEXT("display_name"), Disp.IsEmpty() ? S->GetName() : Disp);
#else
        J->SetStringField(TEXT("display_name"), S->GetName());
#endif
        FString Module;
        {
            const FString Pkg = S->GetOutermost() ? S->GetOutermost()->GetName() : FString();
            FString L, R;
            if (Pkg.Split(TEXT("/Script/"), &L, &R)) { Module = R; } else { Module = Pkg; }
        }
        J->SetStringField(TEXT("module"), Module);
#if WITH_EDITOR
        J->SetBoolField(TEXT("is_abstract"), S->GetBoolMetaDataHierarchical(TEXT("Abstract"))); // VERIFY vs engine source
#endif
        Arr.Add(MakeShared<FJsonValueObject>(J));
    }
}

// ---- (A) EQS node config VALUE reader --------------------------------------------------------
FString UMCPReflectionLibrary::GetEnvQueryConfigJson(UEnvQuery* Query)
{
    if (!Query)
    {
        return SerializeJson(ErrorObj(TEXT("null query")));
    }

    // C++ #17: single visited-set threaded through the whole walk — guards BOTH node-level UObject
    // cycles (Option/Generator/Test revisited) AND struct-address cycles inside config values.
    TSet<const void*> Visited;

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());

    FString QueryName = Query->GetName();
    if (FNameProperty* QN = FindFProperty<FNameProperty>(UEnvQuery::StaticClass(), TEXT("QueryName"))) // VERIFY vs engine source: UEnvQuery::QueryName
    {
        const FName NameVal = QN->GetPropertyValue_InContainer(Query);
        if (!NameVal.IsNone())
        {
            QueryName = NameVal.ToString();
        }
    }
    Root->SetStringField(TEXT("query_name"), QueryName);

    TArray<UObject*> Options;
    EqsReflectObjectArray(Query, TEXT("Options"), Options); // VERIFY vs engine source: UEnvQuery::Options property name

    TArray<TSharedPtr<FJsonValue>> OptionArr;
    int32 Index = 0;
    for (UObject* OptionObj : Options)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("index"), Index++);
        if (!OptionObj)
        {
            O->SetStringField(TEXT("name"), TEXT("None"));
            OptionArr.Add(MakeShared<FJsonValueObject>(O));
            continue;
        }
        // C++ #17: node-level cycle guard.
        {
            bool bAlready = false;
            Visited.Add(OptionObj, &bAlready);
            if (bAlready)
            {
                O->SetStringField(TEXT("name"), OptionObj->GetName());
                O->SetBoolField(TEXT("_cycle"), true);
                OptionArr.Add(MakeShared<FJsonValueObject>(O));
                continue;
            }
        }
        O->SetStringField(TEXT("name"), OptionObj->GetName());

        if (UObject* Gen = EqsReflectObject(OptionObj, TEXT("Generator"))) // VERIFY vs engine source: UEnvQueryOption::Generator
        {
            TSharedRef<FJsonObject> G = MakeShared<FJsonObject>();
            G->SetStringField(TEXT("class"), Gen->GetClass()->GetName());
            G->SetStringField(TEXT("path"), Gen->GetPathName());
            if (UObject* IT = EqsReflectObject(Gen, TEXT("ItemType"))) // VERIFY vs engine source: UEnvQueryGenerator::ItemType
            {
                G->SetStringField(TEXT("item_type"), IT->GetPathName());
            }
            TSharedRef<FJsonObject> GenConfig = MakeShared<FJsonObject>();  // not 'GConfig' — that shadows UE's global config ptr (C4459 = error)
            bool bGenSeen = false;
            Visited.Add(Gen, &bGenSeen);
            if (bGenSeen) { GenConfig->SetBoolField(TEXT("_cycle"), true); }
            else          { EqsSerializeNodeConfig(Gen, GenConfig, /*Depth*/ 0, Visited); }
            G->SetObjectField(TEXT("config"), GenConfig);
            O->SetObjectField(TEXT("generator"), G);
        }

        TArray<UObject*> Tests;
        EqsReflectObjectArray(OptionObj, TEXT("Tests"), Tests); // VERIFY vs engine source: UEnvQueryOption::Tests
        TArray<TSharedPtr<FJsonValue>> TestArr;
        for (UObject* TestObj : Tests)
        {
            TSharedRef<FJsonObject> T = MakeShared<FJsonObject>();
            if (!TestObj)
            {
                T->SetStringField(TEXT("class"), TEXT("None"));
                TestArr.Add(MakeShared<FJsonValueObject>(T));
                continue;
            }
            T->SetStringField(TEXT("class"), TestObj->GetClass()->GetName());
            T->SetStringField(TEXT("path"), TestObj->GetPathName());
            TSharedRef<FJsonObject> TConfig = MakeShared<FJsonObject>();
            bool bTestSeen = false;
            Visited.Add(TestObj, &bTestSeen);
            if (bTestSeen) { TConfig->SetBoolField(TEXT("_cycle"), true); }
            else           { EqsSerializeNodeConfig(TestObj, TConfig, /*Depth*/ 0, Visited); }
            T->SetObjectField(TEXT("config"), TConfig);
            TestArr.Add(MakeShared<FJsonValueObject>(T));
        }
        O->SetArrayField(TEXT("tests"), TestArr);

        OptionArr.Add(MakeShared<FJsonValueObject>(O));
    }

    Root->SetNumberField(TEXT("option_count"), OptionArr.Num());
    Root->SetArrayField(TEXT("options"), OptionArr);
    return SerializeJson(Root);
}

// ---- (B) StateTree native node-type registry ------------------------------------------------
FString UMCPReflectionLibrary::GetStateTreeNodeRegistryJson(const FString& Category)
{
    UScriptStruct* TaskBase     = FStateTreeTaskBase::StaticStruct();          // VERIFY vs engine source
    UScriptStruct* EvalBase     = FStateTreeEvaluatorBase::StaticStruct();     // VERIFY vs engine source
    UScriptStruct* CondBase     = FStateTreeConditionBase::StaticStruct();     // VERIFY vs engine source
    UScriptStruct* ConsiderBase = FStateTreeConsiderationBase::StaticStruct(); // VERIFY vs engine source (probe-confirmed present)

    const FString Cat = Category.IsEmpty() ? TEXT("all") : Category.ToLower();
    const bool bAll   = (Cat == TEXT("all"));
    const bool bTasks = bAll || Cat == TEXT("tasks")          || Cat == TEXT("task");
    const bool bEval  = bAll || Cat == TEXT("evaluators")     || Cat == TEXT("evaluator");
    const bool bCond  = bAll || Cat == TEXT("conditions")     || Cat == TEXT("condition");
    const bool bCons  = bAll || Cat == TEXT("considerations") || Cat == TEXT("consideration");

    TArray<TSharedPtr<FJsonValue>> Tasks, Evals, Conds, Cons;

    for (TObjectIterator<UScriptStruct> It; It; ++It)
    {
        UScriptStruct* S = *It;
        if (!S) { continue; }
        if (bTasks && TaskBase     && S != TaskBase     && S->IsChildOf(TaskBase))     { AddStateTreeNodeRow(Tasks, S); continue; }
        if (bEval  && EvalBase     && S != EvalBase     && S->IsChildOf(EvalBase))     { AddStateTreeNodeRow(Evals, S); continue; }
        if (bCond  && CondBase     && S != CondBase     && S->IsChildOf(CondBase))     { AddStateTreeNodeRow(Conds, S); continue; }
        if (bCons  && ConsiderBase && S != ConsiderBase && S->IsChildOf(ConsiderBase)) { AddStateTreeNodeRow(Cons,  S); continue; }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("category"), Cat);
    Root->SetArrayField(TEXT("tasks"), Tasks);
    Root->SetArrayField(TEXT("evaluators"), Evals);
    Root->SetArrayField(TEXT("conditions"), Conds);
    Root->SetArrayField(TEXT("considerations"), Cons);
    Root->SetNumberField(TEXT("task_count"), Tasks.Num());
    Root->SetNumberField(TEXT("evaluator_count"), Evals.Num());
    Root->SetNumberField(TEXT("condition_count"), Conds.Num());
    Root->SetNumberField(TEXT("consideration_count"), Cons.Num());
    Root->SetStringField(TEXT("note"),
        TEXT("Native StateTree node types are UScriptStructs (FInstancedStruct), selected by IsChildOf the "
             "four base node structs; spans StateTreeModule / GameplayStateTreeModule / project modules."));
    return SerializeJson(Root);
}

// ---- (C) ControlRig compiled-VM summary (UBlueprint* fallback form; no editor Developer modules) -------
FString UMCPReflectionLibrary::GetControlRigVMJson(UBlueprint* CRBlueprint)
{
    if (!CRBlueprint)
    {
        return SerializeJson(ErrorObj(TEXT("null blueprint")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), CRBlueprint->GetName());
    Root->SetStringField(TEXT("path"), CRBlueprint->GetPathName());

    // Reach the compiled VM through the generated-class CDO. UBlueprint::GeneratedClass is public (Engine);
    // a Control Rig's CDO is a URigVMHost (RigVM). Same path Python uses: bp.generated_class() ->
    // get_default_object() -> get_vm().
    UClass* GenClass = CRBlueprint->GeneratedClass;
    URigVMHost* Host = GenClass ? Cast<URigVMHost>(GenClass->GetDefaultObject()) : nullptr; // VERIFY URigVMHost base (module RigVM)
    URigVM* VM = Host ? Host->GetVM() : nullptr;                                             // VERIFY URigVMHost::GetVM()

    Root->SetBoolField(TEXT("vm_present"), VM != nullptr);

    if (VM)
    {
        const FRigVMByteCode& ByteCode = VM->GetByteCode();           // VERIFY URigVM::GetByteCode() -> const FRigVMByteCode&
        const int32 NumInstr = ByteCode.GetNumInstructions();        // VERIFY FRigVMByteCode::GetNumInstructions()
        Root->SetNumberField(TEXT("instruction_count"), NumInstr);

        TMap<FString, int32> OpHist;
        for (int32 i = 0; i < NumInstr; ++i)
        {
            const ERigVMOpCode Op = ByteCode.GetOpCodeAt(i);         // VERIFY FRigVMByteCode::GetOpCodeAt(int32)
            FString OpName = TEXT("Unknown");
            if (const UEnum* OpEnum = StaticEnum<ERigVMOpCode>())    // VERIFY ERigVMOpCode is a UENUM
            {
                OpName = OpEnum->GetNameStringByValue((int64)Op);
            }
            OpHist.FindOrAdd(OpName)++;
        }
        TSharedRef<FJsonObject> HistObj = MakeShared<FJsonObject>();
        for (const TPair<FString, int32>& Pair : OpHist)
        {
            HistObj->SetNumberField(Pair.Key, Pair.Value);
        }
        Root->SetObjectField(TEXT("opcode_histogram"), HistObj);

        FRigVMStatistics Stats = VM->GetStatistics();                // VERIFY URigVM::GetStatistics() -> FRigVMStatistics (by value)
        TSharedRef<FJsonObject> StatObj = MakeShared<FJsonObject>();
        if (UScriptStruct* StatStruct = FRigVMStatistics::StaticStruct()) // VERIFY FRigVMStatistics is a USTRUCT
        {
            for (TFieldIterator<FProperty> It(StatStruct); It; ++It)
            {
                if (FNumericProperty* Num = CastField<FNumericProperty>(*It))
                {
                    const void* ValPtr = Num->ContainerPtrToValuePtr<void>(&Stats);
                    if (Num->IsInteger())
                    {
                        StatObj->SetNumberField(Num->GetName(), (double)Num->GetSignedIntPropertyValue(ValPtr));
                    }
                    else
                    {
                        StatObj->SetNumberField(Num->GetName(), Num->GetFloatingPointPropertyValue(ValPtr));
                    }
                }
                else if (FBoolProperty* Bp = CastField<FBoolProperty>(*It))
                {
                    StatObj->SetBoolField(Bp->GetName(), Bp->GetPropertyValue_InContainer(&Stats));
                }
            }
        }
        Root->SetObjectField(TEXT("statistics"), StatObj);

        // memory_stats intentionally omitted on this engine build: UE 5.8 URigVM::GetWorkMemory() now
        // requires an FRigVMExtendedExecuteContext& and both Get*Memory() return FRigVMMemoryStorageStruct*
        // (not URigVMMemoryStorage* with ::Num()). Per the handoff's sanctioned fallback, drop the block —
        // FRigVMStatistics above already carries the byte/memory counts.
    }
    else
    {
        Root->SetStringField(TEXT("note"),
            TEXT("compiled VM not reachable from the generated-class CDO (not a Control Rig blueprint, needs a "
                 "recompile, or URigVMHost::GetVM() differs). Static compiled summary only — no posed/evaluated state."));
    }

    // External variables (host-level; safe whether or not the VM resolved).
    TArray<TSharedPtr<FJsonValue>> Externals;
    if (Host)
    {
        const TArray<FRigVMExternalVariable> Vars = Host->GetExternalVariables(); // VERIFY URigVMHost::GetExternalVariables()
        for (const FRigVMExternalVariable& V : Vars)
        {
            TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
            // UE 5.8: FRigVMExternalVariableDef members are protected + there is no TypeName; use the
            // public getters. GetBaseCPPType() is the base CPP type FName (e.g. "float"/"FVector").
            E->SetStringField(TEXT("name"), V.GetName().ToString());          // FRigVMExternalVariableDef::GetName()
            E->SetStringField(TEXT("type"), V.GetBaseCPPType().ToString());   // FRigVMExternalVariableDef::GetBaseCPPType()
            E->SetBoolField(TEXT("is_array"), V.IsArray());                   // FRigVMExternalVariableDef::IsArray()
            Externals.Add(MakeShared<FJsonValueObject>(E));
        }
    }
    Root->SetArrayField(TEXT("external_variables"), Externals);
    // NOTE: node_summary intentionally omitted (would need ControlRigDeveloper/RigVMDeveloper editor modules);
    // the existing Python controlrig.get_control_rig_vm_graph already returns per-node names + script_struct.
    return SerializeJson(Root);
}

// ---- (D) RigVM per-struct pin schema (CoreUObject reflection only) --------------------------
FString UMCPReflectionLibrary::GetRigVMStructPinsJson(const FString& StructName)
{
    if (StructName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty struct name")));
    }

    UScriptStruct* Struct = nullptr;
    if (StructName.Contains(TEXT(".")) || StructName.StartsWith(TEXT("/")))
    {
        Struct = FindObject<UScriptStruct>(nullptr, *StructName); // full path form
    }
    if (!Struct)
    {
        Struct = FindFirstObject<UScriptStruct>(*StructName, EFindFirstObjectOptions::None); // VERIFY EFindFirstObjectOptions::None
    }
    if (!Struct)
    {
        return SerializeJson(ErrorObj(FString::Printf(
            TEXT("UScriptStruct '%s' not found (is the ControlRig/RigVM plugin enabled?)"), *StructName)));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("path"), Struct->GetPathName());

    FStructOnScope DefaultScope(Struct);
    const uint8* DefaultData = DefaultScope.GetStructMemory();

    int32 PinCount = 0;
    TArray<TSharedPtr<FJsonValue>> Pins;
    for (TFieldIterator<FProperty> It(Struct); It; ++It)
    {
        FProperty* Prop = *It;
        if (!Prop) { continue; }

        FString Direction = TEXT("hidden");
#if WITH_EDITOR
        const bool bIn  = Prop->HasMetaData(TEXT("Input"));   // VERIFY RigVM metadata key "Input"
        const bool bOut = Prop->HasMetaData(TEXT("Output"));  // VERIFY RigVM metadata key "Output"
        const bool bVis = Prop->HasMetaData(TEXT("Visible")); // VERIFY RigVM metadata key "Visible"
        if (bIn && bOut)      { Direction = TEXT("io"); }
        else if (bIn)         { Direction = TEXT("input"); }
        else if (bOut)        { Direction = TEXT("output"); }
        else if (bVis)        { Direction = TEXT("visible"); }
#else
        if (Prop->HasAnyPropertyFlags(CPF_BlueprintReadOnly)) { Direction = TEXT("output"); }
        else if (Prop->HasAnyPropertyFlags(CPF_Edit))         { Direction = TEXT("input"); }
#endif
        const bool bIsPin = (Direction != TEXT("hidden"));
        if (bIsPin) { ++PinCount; }

        TSharedRef<FJsonObject> P = MakeShared<FJsonObject>();
        P->SetStringField(TEXT("name"), Prop->GetName());
        P->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());
        P->SetStringField(TEXT("direction"), Direction);
        P->SetBoolField(TEXT("is_pin"), bIsPin);
        P->SetBoolField(TEXT("is_array"), Prop->IsA<FArrayProperty>());

        FString DefaultValue;
        if (DefaultData)
        {
            const void* ValPtr = Prop->ContainerPtrToValuePtr<void>(DefaultData);
            Prop->ExportTextItem_Direct(DefaultValue, ValPtr, nullptr, nullptr, PPF_None); // VERIFY ExportTextItem_Direct signature
        }
        P->SetStringField(TEXT("default_value"), DefaultValue);

#if WITH_EDITOR
        const FString PinCategory = Prop->GetMetaData(TEXT("Category"));
        if (!PinCategory.IsEmpty()) { P->SetStringField(TEXT("category"), PinCategory); }
        const FString Tip = Prop->GetMetaData(TEXT("ToolTip"));
        if (!Tip.IsEmpty()) { P->SetStringField(TEXT("tooltip"), Tip); }
#endif
        Pins.Add(MakeShared<FJsonValueObject>(P));
    }

    Root->SetNumberField(TEXT("property_count"), Pins.Num());
    Root->SetNumberField(TEXT("pin_count"), PinCount);
    Root->SetArrayField(TEXT("pins"), Pins);
    return SerializeJson(Root);
}

// ============================================================================================
// ==== C++ #13 2026-08-16 — backlog WRITERS (AnimMontage sections + USkeleton authoring) =====
// Both areas reach protected Engine TArrays via FArrayProperty + FScriptArrayHelper reflection
// (the C++ #12 EqsReflectObjectArray idiom — no editor-module export symbol referenced). Engine
// module only -> NO Build.cs change. Every version-sensitive member is "VERIFY vs engine source".
// AUTHORED on Mac, NOT YET COMPILED.
// ============================================================================================

// ---- (A) AnimMontage composite-section reflection helpers ----------------------------------
namespace
{
    bool BindMontageSections(UAnimMontage* Montage, FArrayProperty*& OutArrayProp, UScriptStruct*& OutElemStruct)
    {
        if (!Montage) { return false; }
        FArrayProperty* Arr = FindFProperty<FArrayProperty>(UAnimMontage::StaticClass(), TEXT("CompositeSections")); // VERIFY vs engine source (probe-confirmed protected)
        if (!Arr) { return false; }
        FStructProperty* InnerStruct = CastField<FStructProperty>(Arr->Inner);
        if (!InnerStruct || !InnerStruct->Struct) { return false; }
        OutArrayProp = Arr;
        OutElemStruct = InnerStruct->Struct; // FCompositeSection
        return true;
    }

    struct FMontageSectionFields
    {
        FNameProperty* SectionName = nullptr; // FName SectionName
        FNameProperty* NextSection = nullptr; // FName NextSectionName
        FFloatProperty* LinkValue  = nullptr; // float LinkValue (FAnimLinkableElement)
        FProperty* LinkMethod      = nullptr; // TEnumAsByte<EAnimLinkMethod::Type> LinkMethod
        bool Valid() const { return SectionName && LinkValue; }
    };

    FMontageSectionFields ResolveSectionFields(UScriptStruct* ElemStruct)
    {
        FMontageSectionFields F;
        if (!ElemStruct) { return F; }
        F.SectionName = CastField<FNameProperty>(ElemStruct->FindPropertyByName(TEXT("SectionName")));      // VERIFY vs engine source
        F.NextSection = CastField<FNameProperty>(ElemStruct->FindPropertyByName(TEXT("NextSectionName")));  // VERIFY vs engine source
        F.LinkValue   = CastField<FFloatProperty>(ElemStruct->FindPropertyByName(TEXT("LinkValue")));       // VERIFY vs engine source (float not double in 5.8)
        F.LinkMethod  = ElemStruct->FindPropertyByName(TEXT("LinkMethod"));                                 // VERIFY vs engine source (TEnumAsByte -> FByteProperty)
        return F;
    }

    float ReadElemTime(const FMontageSectionFields& F, const void* ElemPtr)
    {
        return F.LinkValue ? F.LinkValue->GetPropertyValue_InContainer(ElemPtr) : 0.f;
    }
    void WriteElemTime(const FMontageSectionFields& F, void* ElemPtr, float T)
    {
        if (F.LinkValue) { F.LinkValue->SetPropertyValue_InContainer(ElemPtr, T); }
        if (FByteProperty* B = CastField<FByteProperty>(F.LinkMethod))
        {
            B->SetPropertyValue_InContainer(ElemPtr, (uint8)0); // EAnimLinkMethod::Absolute == 0 — VERIFY vs engine source
        }
    }
    FName ReadElemName(FNameProperty* P, const void* ElemPtr)
    {
        return P ? P->GetPropertyValue_InContainer(ElemPtr) : NAME_None;
    }
    void SortSectionsByTime(FScriptArrayHelper& Helper, const FMontageSectionFields& F)
    {
        const int32 N = Helper.Num();
        for (int32 i = 1; i < N; ++i)
        {
            for (int32 j = i; j > 0; --j)
            {
                const float A = ReadElemTime(F, Helper.GetRawPtr(j - 1));
                const float B = ReadElemTime(F, Helper.GetRawPtr(j));
                if (B < A) { Helper.SwapValues(j - 1, j); } // VERIFY vs engine source: FScriptArrayHelper::SwapValues
                else { break; }
            }
        }
    }
    int32 FindSectionIndex(FScriptArrayHelper& Helper, FNameProperty* NameProp, FName Target)
    {
        if (!NameProp) { return INDEX_NONE; }
        for (int32 i = 0; i < Helper.Num(); ++i)
        {
            if (ReadElemName(NameProp, Helper.GetRawPtr(i)) == Target) { return i; }
        }
        return INDEX_NONE;
    }
    void RefreshMontage(UAnimMontage* Montage)
    {
#if WITH_EDITOR
        Montage->PostEditChange(); // VERIFY vs engine source: rebuilds branching-point markers
#endif
        Montage->MarkPackageDirty();
    }

    // ---- (B) USkeleton reflection helpers -----------------------------------------------------
    FArrayProperty* SkelSocketsArrayProp(USkeleton* Skeleton)
    {
        if (!Skeleton) { return nullptr; }
        return FindFProperty<FArrayProperty>(Skeleton->GetClass(), TEXT("Sockets")); // VERIFY vs engine source (protected, probe-confirmed name)
    }
    void SkelReadSockets(USkeleton* Skeleton, TArray<USkeletalMeshSocket*>& Out)
    {
        FArrayProperty* Arr = SkelSocketsArrayProp(Skeleton);
        if (!Arr) { return; }
        FObjectPropertyBase* Inner = CastField<FObjectPropertyBase>(Arr->Inner);
        if (!Inner) { return; }
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Skeleton));
        for (int32 i = 0; i < Helper.Num(); ++i)
        {
            Out.Add(Cast<USkeletalMeshSocket>(Inner->GetObjectPropertyValue(Helper.GetRawPtr(i))));
        }
    }
    bool SkelHasBone(USkeleton* Skeleton, const FName BoneName)
    {
        if (!Skeleton) { return false; }
        return Skeleton->GetReferenceSkeleton().FindBoneIndex(BoneName) != INDEX_NONE; // VERIFY vs engine source
    }
}

// ---- (A) AnimMontage handler impls ----------------------------------------------------------
FString UMCPReflectionLibrary::AddMontageSection(UAnimMontage* Montage, const FString& SectionName, float StartTime)
{
#if WITH_EDITOR
    if (!Montage) { return SerializeJson(ErrorObj(TEXT("null montage"))); }
    if (SectionName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("empty section_name"))); }
    FArrayProperty* Arr = nullptr; UScriptStruct* Elem = nullptr;
    if (!BindMontageSections(Montage, Arr, Elem)) { return SerializeJson(ErrorObj(TEXT("could not bind CompositeSections (reflection)"))); }
    FMontageSectionFields F = ResolveSectionFields(Elem);
    if (!F.Valid()) { return SerializeJson(ErrorObj(TEXT("could not resolve FCompositeSection fields (SectionName/LinkValue)"))); }
    FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Montage));
    const FName NewName(*SectionName);
    if (FindSectionIndex(Helper, F.SectionName, NewName) != INDEX_NONE)
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("section already exists: %s (refusing duplicate)"), *SectionName)));
    }
    const int32 NewIdx = Helper.AddValue();
    void* ElemPtr = Helper.GetRawPtr(NewIdx);
    F.SectionName->SetPropertyValue_InContainer(ElemPtr, NewName);
    WriteElemTime(F, ElemPtr, StartTime);
    SortSectionsByTime(Helper, F);
    RefreshMontage(Montage);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("montage"), Montage->GetName());
    Root->SetStringField(TEXT("section_name"), SectionName);
    Root->SetNumberField(TEXT("start_time"), StartTime);
    Root->SetNumberField(TEXT("section_count"), Helper.Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveMontageSection(UAnimMontage* Montage, const FString& SectionName)
{
#if WITH_EDITOR
    if (!Montage) { return SerializeJson(ErrorObj(TEXT("null montage"))); }
    FArrayProperty* Arr = nullptr; UScriptStruct* Elem = nullptr;
    if (!BindMontageSections(Montage, Arr, Elem)) { return SerializeJson(ErrorObj(TEXT("could not bind CompositeSections (reflection)"))); }
    FMontageSectionFields F = ResolveSectionFields(Elem);
    if (!F.Valid()) { return SerializeJson(ErrorObj(TEXT("could not resolve FCompositeSection fields"))); }
    FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Montage));
    const FName Target(*SectionName);
    const int32 Idx = FindSectionIndex(Helper, F.SectionName, Target);
    if (Idx == INDEX_NONE) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no such section: %s"), *SectionName))); }
    const void* ElemPtr = Helper.GetRawPtr(Idx);
    const float PriorTime = ReadElemTime(F, ElemPtr);
    const FName NextName  = ReadElemName(F.NextSection, ElemPtr);
    Helper.RemoveValues(Idx, 1); // VERIFY vs engine source: FScriptArrayHelper::RemoveValues(int32,int32)
    RefreshMontage(Montage);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("montage"), Montage->GetName());
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("section_name"), SectionName);
    Root->SetNumberField(TEXT("prior_start_time"), PriorTime);
    Root->SetStringField(TEXT("next_section_name"), NextName.ToString());
    Root->SetNumberField(TEXT("section_count"), Helper.Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::SetMontageSectionTime(UAnimMontage* Montage, const FString& SectionName, float NewStartTime)
{
#if WITH_EDITOR
    if (!Montage) { return SerializeJson(ErrorObj(TEXT("null montage"))); }
    FArrayProperty* Arr = nullptr; UScriptStruct* Elem = nullptr;
    if (!BindMontageSections(Montage, Arr, Elem)) { return SerializeJson(ErrorObj(TEXT("could not bind CompositeSections (reflection)"))); }
    FMontageSectionFields F = ResolveSectionFields(Elem);
    if (!F.Valid()) { return SerializeJson(ErrorObj(TEXT("could not resolve FCompositeSection fields"))); }
    FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Montage));
    const int32 Idx = FindSectionIndex(Helper, F.SectionName, FName(*SectionName));
    if (Idx == INDEX_NONE) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no such section: %s"), *SectionName))); }
    void* ElemPtr = Helper.GetRawPtr(Idx);
    const float PriorTime = ReadElemTime(F, ElemPtr);
    WriteElemTime(F, ElemPtr, NewStartTime);
    SortSectionsByTime(Helper, F);
    RefreshMontage(Montage);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("montage"), Montage->GetName());
    Root->SetStringField(TEXT("section_name"), SectionName);
    Root->SetNumberField(TEXT("prior_start_time"), PriorTime);
    Root->SetNumberField(TEXT("new_start_time"), NewStartTime);
    Root->SetNumberField(TEXT("section_count"), Helper.Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::SetMontageSectionNextSection(UAnimMontage* Montage, const FString& SectionName, const FString& NextSectionName)
{
#if WITH_EDITOR
    if (!Montage) { return SerializeJson(ErrorObj(TEXT("null montage"))); }
    FArrayProperty* Arr = nullptr; UScriptStruct* Elem = nullptr;
    if (!BindMontageSections(Montage, Arr, Elem)) { return SerializeJson(ErrorObj(TEXT("could not bind CompositeSections (reflection)"))); }
    FMontageSectionFields F = ResolveSectionFields(Elem);
    if (!F.SectionName || !F.NextSection) { return SerializeJson(ErrorObj(TEXT("could not resolve SectionName/NextSectionName fields"))); }
    FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Montage));
    const int32 Idx = FindSectionIndex(Helper, F.SectionName, FName(*SectionName));
    if (Idx == INDEX_NONE) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no such section: %s"), *SectionName))); }
    void* ElemPtr = Helper.GetRawPtr(Idx);
    const FName PriorNext = ReadElemName(F.NextSection, ElemPtr);
    F.NextSection->SetPropertyValue_InContainer(ElemPtr, FName(*NextSectionName));
    RefreshMontage(Montage);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("montage"), Montage->GetName());
    Root->SetStringField(TEXT("section_name"), SectionName);
    Root->SetStringField(TEXT("prior_next_section"), PriorNext.ToString());
    Root->SetStringField(TEXT("new_next_section"), NextSectionName);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ---- (B) USkeleton handler impls ------------------------------------------------------------
FString UMCPReflectionLibrary::AddSkeletonSocket(USkeleton* Skeleton, const FString& SocketName,
    const FString& BoneName, float LocX, float LocY, float LocZ,
    float Pitch, float Yaw, float Roll, float ScaleX, float ScaleY, float ScaleZ)
{
#if WITH_EDITOR
    if (!Skeleton) { return SerializeJson(ErrorObj(TEXT("null skeleton"))); }
    if (SocketName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("socket_name is required"))); }
    if (BoneName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("bone name is required"))); }
    const FName SocketFName(*SocketName);
    const FName BoneFName(*BoneName);
    if (!SkelHasBone(Skeleton, BoneFName)) { return SerializeJson(ErrorObj(FString::Printf(TEXT("bone '%s' not found on skeleton"), *BoneName))); }
    FArrayProperty* Arr = SkelSocketsArrayProp(Skeleton);
    if (!Arr) { return SerializeJson(ErrorObj(TEXT("could not resolve USkeleton::Sockets array property"))); }
    FObjectPropertyBase* Inner = CastField<FObjectPropertyBase>(Arr->Inner);
    if (!Inner) { return SerializeJson(ErrorObj(TEXT("Sockets inner is not an object property"))); }
    {
        TArray<USkeletalMeshSocket*> Existing;
        SkelReadSockets(Skeleton, Existing);
        for (USkeletalMeshSocket* S : Existing)
        {
            if (S && S->SocketName == SocketFName) // VERIFY vs engine source: USkeletalMeshSocket::SocketName (public)
            {
                return SerializeJson(ErrorObj(FString::Printf(TEXT("a socket named '%s' already exists on this skeleton"), *SocketName)));
            }
        }
    }
    USkeletalMeshSocket* NewSock = NewObject<USkeletalMeshSocket>(Skeleton);
    if (!NewSock) { return SerializeJson(ErrorObj(TEXT("failed to construct USkeletalMeshSocket"))); }
    NewSock->SocketName       = SocketFName;                    // VERIFY vs engine source (public UPROPERTY)
    NewSock->BoneName         = BoneFName;                      // VERIFY vs engine source
    NewSock->RelativeLocation = FVector(LocX, LocY, LocZ);      // VERIFY vs engine source
    NewSock->RelativeRotation = FRotator(Pitch, Yaw, Roll);     // VERIFY vs engine source
    NewSock->RelativeScale    = FVector(ScaleX, ScaleY, ScaleZ);// VERIFY vs engine source (RelativeScale, not RelativeScale3D)
    Skeleton->Modify();
    Skeleton->PreEditChange(Arr);
    {
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Skeleton));
        const int32 NewIndex = Helper.AddValue();
        Inner->SetObjectPropertyValue(Helper.GetRawPtr(NewIndex), NewSock); // VERIFY vs engine source
    }
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();
    TArray<USkeletalMeshSocket*> After; SkelReadSockets(Skeleton, After);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("socket_name"), SocketName);
    Root->SetStringField(TEXT("bone"), BoneName);
    Root->SetNumberField(TEXT("socket_count"), After.Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveSkeletonSocket(USkeleton* Skeleton, const FString& SocketName)
{
#if WITH_EDITOR
    if (!Skeleton) { return SerializeJson(ErrorObj(TEXT("null skeleton"))); }
    if (SocketName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("socket_name is required"))); }
    FArrayProperty* Arr = SkelSocketsArrayProp(Skeleton);
    if (!Arr) { return SerializeJson(ErrorObj(TEXT("could not resolve USkeleton::Sockets array property"))); }
    FObjectPropertyBase* Inner = CastField<FObjectPropertyBase>(Arr->Inner);
    if (!Inner) { return SerializeJson(ErrorObj(TEXT("Sockets inner is not an object property"))); }
    const FName SocketFName(*SocketName);
    int32 FoundIndex = INDEX_NONE; USkeletalMeshSocket* Found = nullptr;
    {
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Skeleton));
        for (int32 i = 0; i < Helper.Num(); ++i)
        {
            USkeletalMeshSocket* S = Cast<USkeletalMeshSocket>(Inner->GetObjectPropertyValue(Helper.GetRawPtr(i)));
            if (S && S->SocketName == SocketFName) { FoundIndex = i; Found = S; break; }
        }
    }
    if (FoundIndex == INDEX_NONE || !Found) { return SerializeJson(ErrorObj(FString::Printf(TEXT("skeleton socket not found: %s"), *SocketName))); }
    const FString Bone = Found->BoneName.ToString();
    const FVector Loc  = Found->RelativeLocation;
    const FRotator Rot = Found->RelativeRotation;
    const FVector Scl  = Found->RelativeScale;
    Skeleton->Modify();
    Skeleton->PreEditChange(Arr);
    {
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Skeleton));
        Helper.RemoveValues(FoundIndex, 1); // VERIFY vs engine source
    }
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();
    TArray<USkeletalMeshSocket*> After; SkelReadSockets(Skeleton, After);
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("socket_name"), SocketName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("bone"), Bone);
    Root->SetArrayField(TEXT("location"), Vec3(Loc.X, Loc.Y, Loc.Z));
    Root->SetArrayField(TEXT("rotation"), Vec3(Rot.Pitch, Rot.Yaw, Rot.Roll));
    Root->SetArrayField(TEXT("scale"), Vec3(Scl.X, Scl.Y, Scl.Z));
    Root->SetNumberField(TEXT("socket_count"), After.Num());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddVirtualBone(USkeleton* Skeleton, const FString& SourceBone, const FString& TargetBone)
{
#if WITH_EDITOR
    if (!Skeleton) { return SerializeJson(ErrorObj(TEXT("null skeleton"))); }
    if (SourceBone.IsEmpty() || TargetBone.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("source and target bone names are required"))); }
    const FName SourceFName(*SourceBone);
    const FName TargetFName(*TargetBone);
    if (!SkelHasBone(Skeleton, SourceFName)) { return SerializeJson(ErrorObj(FString::Printf(TEXT("source bone '%s' not found"), *SourceBone))); }
    if (!SkelHasBone(Skeleton, TargetFName)) { return SerializeJson(ErrorObj(FString::Printf(TEXT("target bone '%s' not found"), *TargetBone))); }
    Skeleton->Modify();
    FName NewVBName = NAME_None;
    const bool bOk = Skeleton->AddNewVirtualBone(SourceFName, TargetFName, NewVBName); // UE 5.8: method is AddNewVirtualBone(FName,FName,FName&)->bool (not AddVirtualBone)
    if (!bOk) { return SerializeJson(ErrorObj(FString::Printf(TEXT("AddVirtualBone(%s -> %s) failed (invalid pair or already exists)"), *SourceBone, *TargetBone))); }
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();
    const int32 VBCount = Skeleton->GetVirtualBones().Num(); // VERIFY vs engine source: const TArray<FVirtualBone>& GetVirtualBones()
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("source"), SourceBone);
    Root->SetStringField(TEXT("target"), TargetBone);
    Root->SetStringField(TEXT("virtual_bone_name"), NewVBName.ToString());
    Root->SetNumberField(TEXT("virtual_bone_count"), VBCount);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveVirtualBone(USkeleton* Skeleton, const FString& VirtualBoneName)
{
#if WITH_EDITOR
    if (!Skeleton) { return SerializeJson(ErrorObj(TEXT("null skeleton"))); }
    if (VirtualBoneName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("virtual_bone_name is required"))); }
    const FName VBFName(*VirtualBoneName);
    FString PriorSource, PriorTarget; bool bFound = false;
    for (const FVirtualBone& VB : Skeleton->GetVirtualBones()) // VERIFY vs engine source: FVirtualBone members
    {
        if (VB.VirtualBoneName == VBFName)
        {
            PriorSource = VB.SourceBoneName.ToString();
            PriorTarget = VB.TargetBoneName.ToString();
            bFound = true; break;
        }
    }
    if (!bFound) { return SerializeJson(ErrorObj(FString::Printf(TEXT("virtual bone not found: %s"), *VirtualBoneName))); }
    Skeleton->Modify();
    Skeleton->RemoveVirtualBones(TArray<FName>{ VBFName }); // VERIFY vs engine source: void RemoveVirtualBones(const TArray<FName>&)
    Skeleton->PostEditChange();
    Skeleton->MarkPackageDirty();
    const int32 VBCount = Skeleton->GetVirtualBones().Num();
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("skeleton"), Skeleton->GetName());
    Root->SetStringField(TEXT("virtual_bone_name"), VirtualBoneName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("source"), PriorSource);
    Root->SetStringField(TEXT("target"), PriorTarget);
    Root->SetNumberField(TEXT("virtual_bone_count"), VBCount);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ============================================================================================
// ==== C++ #14 2026-08-16 — StateTree editor property-BINDINGS reader (last C++ backlog item) =
// Reaches UStateTreeEditorData::EditorBindings (protected/non-BP, probe-confirmed refused from Python)
// + resolves each binding's FGuid StructID to a readable node/state label. REQUIRES Build.cs +=
// "StateTreeEditorModule" — the isolated export-risk piece (STATETREEEDITORMODULE_API). Degrades
// gracefully to raw GUIDs if the label-map member names mismatch. AUTHORED on Mac, NOT YET COMPILED.
// ============================================================================================
FString UMCPReflectionLibrary::GetStateTreeBindingsJson(UStateTree* StateTree)
{
    if (!StateTree)
    {
        return SerializeJson(ErrorObj(TEXT("null state tree")));
    }

#if WITH_EDITORONLY_DATA
    UStateTreeEditorData* EditorData = Cast<UStateTreeEditorData>(StateTree->EditorData); // VERIFY vs engine source (UStateTree::EditorData, editor-only, may be protected)
    if (!EditorData)
    {
        TArray<UObject*> Inners;
        GetObjectsWithOuter(StateTree, Inners, /*bIncludeNestedObjects*/ false);
        for (UObject* O : Inners)
        {
            if (UStateTreeEditorData* ED = Cast<UStateTreeEditorData>(O)) { EditorData = ED; break; }
        }
    }
    if (!EditorData)
    {
        return SerializeJson(ErrorObj(TEXT("could not reach UStateTreeEditorData (authorable bindings unreachable)")));
    }

    // Build a StructID(FGuid) -> readable label map by walking the editor data.
    TMap<FGuid, FString> IdToLabel;
    auto LabelNode = [&IdToLabel](const FStateTreeEditorNode& Node, const FString& Ctx)
    {
        const FGuid Id = Node.ID; // VERIFY vs engine source (FStateTreeEditorNode::ID)
        if (!Id.IsValid()) { return; }
        FString TypeName;
        if (const UScriptStruct* NS = Node.Node.GetScriptStruct()) // VERIFY vs engine source (FInstancedStruct::GetScriptStruct)
        {
            TypeName = FString(NS->GetPrefixCPP()) + NS->GetName();
        }
        else if (Node.InstanceObject) // VERIFY vs engine source (FStateTreeEditorNode::InstanceObject)
        {
            TypeName = Node.InstanceObject->GetClass()->GetName();
        }
        IdToLabel.Add(Id, FString::Printf(TEXT("%s %s"), *Ctx, *TypeName));
    };

    for (const FStateTreeEditorNode& N : EditorData->Evaluators)  { LabelNode(N, TEXT("Evaluator")); }  // VERIFY vs engine source (member name)
    for (const FStateTreeEditorNode& N : EditorData->GlobalTasks) { LabelNode(N, TEXT("GlobalTask")); }  // VERIFY vs engine source (member name)

    TFunction<void(UStateTreeState*)> Visit = [&](UStateTreeState* St)
    {
        if (!St) { return; }
        const FString StateName = St->Name.ToString(); // VERIFY vs engine source (UStateTreeState::Name)
        IdToLabel.Add(St->ID, FString::Printf(TEXT("State '%s'"), *StateName)); // VERIFY vs engine source (UStateTreeState::ID)
        for (const FStateTreeEditorNode& N : St->Tasks)           { LabelNode(N, FString::Printf(TEXT("State '%s' Task"), *StateName)); }      // VERIFY (Tasks)
        for (const FStateTreeEditorNode& N : St->EnterConditions) { LabelNode(N, FString::Printf(TEXT("State '%s' EnterCond"), *StateName)); } // VERIFY (EnterConditions)
        for (const FStateTreeEditorNode& N : St->Considerations)  { LabelNode(N, FString::Printf(TEXT("State '%s' Consider"), *StateName)); }  // VERIFY (Considerations; may be absent in older 5.x)
        for (UStateTreeState* Child : St->Children) { Visit(Child); } // VERIFY vs engine source (UStateTreeState::Children)
    };
    for (UStateTreeState* Root2 : EditorData->SubTrees) { Visit(Root2); } // VERIFY vs engine source (UStateTreeEditorData::SubTrees)

    auto ResolveLabel = [&IdToLabel](const FGuid& Id) -> FString
    {
        if (const FString* Found = IdToLabel.Find(Id)) { return *Found; }
        return Id.IsValid() ? Id.ToString(EGuidFormats::DigitsWithHyphens) : FString(TEXT("<invalid>"));
    };

    const FStateTreeEditorPropertyBindings& Bindings = EditorData->EditorBindings; // public UPROPERTY (StateTreeEditorData.h)

    // UE 5.8: StateTree bindings were genericized onto PropertyBindingUtils. FStateTreeEditorPropertyBindings
    // derives FPropertyBindingBindingCollection, which is enumerated via ForEachBinding (no GetBindings()); each
    // FPropertyBindingBinding exposes GetSourcePath()/GetTargetPath() -> FPropertyBindingPath (GetStructID/ToString).
    // JSON shape is unchanged.
    TArray<TSharedPtr<FJsonValue>> Out;
    Bindings.ForEachBinding([&Out, &ResolveLabel](const FPropertyBindingBinding& B)
    {
        const FPropertyBindingPath& Src = B.GetSourcePath();
        const FPropertyBindingPath& Tgt = B.GetTargetPath();
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("source_struct"),   ResolveLabel(Src.GetStructID()));
        J->SetStringField(TEXT("source_property"), Src.ToString());
        J->SetStringField(TEXT("target_struct"),   ResolveLabel(Tgt.GetStructID()));
        J->SetStringField(TEXT("target_property"), Tgt.ToString());
        J->SetStringField(TEXT("source_struct_id"), Src.GetStructID().ToString(EGuidFormats::DigitsWithHyphens));
        J->SetStringField(TEXT("target_struct_id"), Tgt.GetStructID().ToString(EGuidFormats::DigitsWithHyphens));
        Out.Add(MakeShared<FJsonValueObject>(J));
    });

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("state_tree"), StateTree->GetPathName());
    Root->SetStringField(TEXT("editor_data"), EditorData->GetPathName());
    Root->SetNumberField(TEXT("binding_count"), Out.Num());
    Root->SetArrayField(TEXT("bindings"), Out);
    Root->SetStringField(TEXT("note"),
        TEXT("Bindings from UStateTreeEditorData.EditorBindings (FStateTreeEditorPropertyBindings). *_struct "
             "labels resolve the FGuid StructID to the owning node/state via an ID->name map built by walking "
             "the editor data; *_struct_id keeps the raw GUID as fallback."));
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only (bindings live in WITH_EDITORONLY_DATA)")));
#endif
}

// ---- C++ #16 2026-08-16 (gameplay-tag rename + source authoring) — same GameplayTagsEditor INI path as
// C++ #6. NO new includes / NO Build.cs change (GameplayTags + GameplayTagsEditor already deps).
// Calls tagged VERIFY are version-sensitive — confirm against GameplayTagsEditorModule.h on the build.

FString UMCPReflectionLibrary::RenameGameplayTag(const FString& OldTag, const FString& NewTag)
{
#if WITH_EDITOR
    if (OldTag.IsEmpty() || NewTag.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty tag name")));
    }
    if (OldTag == NewTag)
    {
        return SerializeJson(ErrorObj(TEXT("old and new tag are identical")));
    }
    // Guard: RenameTagInINI internally calls RequestGameplayTag(OldTag), which trips a HANDLED ENSURE
    // (GameplayTagsManager.cpp "Requested Gameplay Tag ... was not found") if the source tag isn't a
    // registered tag. Validate it exists first so bad input returns a clean error instead of an engine ensure.
    if (!UGameplayTagsManager::Get().FindTagNode(FName(*OldTag)).IsValid())
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("source tag '%s' is not a registered gameplay tag"), *OldTag)));
    }
    // VERIFY vs engine source: IGameplayTagsEditorModule::RenameTagInINI(const FString& TagToRename,
    //   const FString& TagToRenameTo) -> bool. Adds a GameplayTag redirector to DefaultGameplayTags.ini so
    //   existing references resolve. (5.8 may take extra optional args or return void — confirm signature.)
    const bool bRenamed = IGameplayTagsEditorModule::Get().RenameTagInINI(OldTag, NewTag);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("old_tag"), OldTag);
    Root->SetStringField(TEXT("new_tag"), NewTag);
    Root->SetBoolField(TEXT("renamed"), bRenamed);
    // Confirm the new tag is now known to the live manager (reuses the C++ #6 FindTagNode path).
    // VERIFY: UGameplayTagsManager::FindTagNode(const FName&) -> TSharedPtr<FGameplayTagNode>.
    const bool bRegistered = UGameplayTagsManager::Get().FindTagNode(FName(*NewTag)).IsValid();
    Root->SetBoolField(TEXT("registered"), bRegistered);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddGameplayTagSource(const FString& SourceName)
{
#if WITH_EDITOR
    if (SourceName.IsEmpty())
    {
        return SerializeJson(ErrorObj(TEXT("empty source name")));
    }
    // VERIFY vs engine source: IGameplayTagsEditorModule::AddNewGameplayTagSource(const FString& NewSourceName,
    //   const FString& RootDirToUse = FString()) -> bool. Registers a new *.ini tag source and rescans the
    //   manager (the engine appends ".ini" if missing). If this overload is absent in 5.8, fall back to
    //   UGameplayTagsManager::Get().FindOrAddTagSource(FName(*SourceName), EGameplayTagSourceType::TagList)
    //   which returns FGameplayTagSource* (non-null == success) — VERIFY that enum + signature too.
    const bool bAdded = IGameplayTagsEditorModule::Get().AddNewGameplayTagSource(SourceName);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("source"), SourceName);
    Root->SetBoolField(TEXT("added"), bAdded);
    // Confirm the source is now registered with the live manager (source names carry the ".ini" suffix).
    // VERIFY: UGameplayTagsManager::FindTagSource(FName) -> const FGameplayTagSource*.
    const FString SourceLookup = SourceName.EndsWith(TEXT(".ini")) ? SourceName : SourceName + TEXT(".ini");
    const bool bRegistered = (UGameplayTagsManager::Get().FindTagSource(FName(*SourceLookup)) != nullptr);
    Root->SetBoolField(TEXT("registered"), bRegistered);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

// ============================================================================================
// ==== C++ #15 2026-08-16 — EQS authoring WRITER (inverse of GetEnvQueryConfigJson) ==========
// Writes UEnvQuery::Options / UEnvQueryOption::Generator+Tests / node config FProperties through
// FProperty reflection (protected-member-blind, export-patch-free). AIModule + EnvQuery.h already linked.
// AUTHORED on Mac, NOT YET COMPILED. Clean-room; version-sensitive calls tagged "VERIFY vs engine source".
// ============================================================================================

namespace
{
    // Resolve a UClass from a path ("/Script/AIModule.EnvQueryGenerator_ActorsOfClass"), a blueprint-gen class
    // path ("/Game/AI/BP_MyGen.BP_MyGen_C"), or a bare class name ("EnvQueryTest_Distance"). Returns null on miss.
    UClass* EqsResolveClass(const FString& InPath)
    {
        if (InPath.IsEmpty()) { return nullptr; }
        // Path form (contains '.' or '/'): the object AT that path IS a UClass.
        if (InPath.Contains(TEXT(".")) || InPath.Contains(TEXT("/")))
        {
            if (UClass* C = LoadObject<UClass>(nullptr, *InPath)) { return C; }          // VERIFY vs engine source: LoadObject<UClass>(path) for /Script/ + /Game/*_C
            if (UClass* C = FindObject<UClass>(nullptr, *InPath)) { return C; }           // already-loaded native fallback
            // Try LoadClass (blueprint asset -> generated class) as a last resort.
            return LoadClass<UObject>(nullptr, *InPath);                                  // VERIFY vs engine source: LoadClass<UObject>(nullptr, path)
        }
        // Bare name: search by short name across loaded classes.
        return UClass::TryFindTypeSlow<UClass>(InPath);                                   // VERIFY vs engine source: UClass::TryFindTypeSlow<UClass>(const FString&) (UE5.1+; replaces FindObject<UClass>(ANY_PACKAGE,...))
    }

    // Resolve a protected TArray<UObject-derived*> property on Owner. Returns the FArrayProperty + object inner.
    FArrayProperty* EqsObjArrayProp(UObject* Owner, const TCHAR* PropName, FObjectPropertyBase*& OutInner)
    {
        OutInner = nullptr;
        if (!Owner) { return nullptr; }
        FArrayProperty* Arr = FindFProperty<FArrayProperty>(Owner->GetClass(), PropName);
        if (!Arr) { return nullptr; }
        OutInner = CastField<FObjectPropertyBase>(Arr->Inner);
        return OutInner ? Arr : nullptr;
    }

    // Fetch the UEnvQueryOption at OptionIndex from UEnvQuery::Options (bounds-checked). Null on OOB.
    UObject* EqsGetOption(UEnvQuery* Query, int32 OptionIndex)
    {
        FObjectPropertyBase* Inner = nullptr;
        FArrayProperty* Arr = EqsObjArrayProp(Query, TEXT("Options"), Inner);             // VERIFY vs engine source: UEnvQuery::Options
        if (!Arr) { return nullptr; }
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Query));
        if (OptionIndex < 0 || OptionIndex >= Helper.Num()) { return nullptr; }
        return Inner->GetObjectPropertyValue(Helper.GetRawPtr(OptionIndex));
    }

    // Apply one bare JSON value to one FProperty at ValuePtr. Typed fast-paths mirror the reader in reverse;
    // ImportText_Direct is the universal fallback. OwnerNode is the object owning ValuePtr (for object import).
    bool EqsApplyJsonToProperty(FProperty* Prop, void* ValuePtr, UObject* OwnerNode,
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
                Val = Enum->GetValueByNameString(V->AsString());                          // VERIFY vs engine source: UEnum::GetValueByNameString (returns INDEX_NONE on miss)
                if (Val == INDEX_NONE) { OutErr = FString::Printf(TEXT("bad enum name '%s'"), *V->AsString()); return false; }
            }
            else { Val = (int64)V->AsNumber(); }
            if (U) { U->SetIntPropertyValue(ValuePtr, Val); return true; }                // VERIFY vs engine source: FNumericProperty::SetIntPropertyValue(void*, int64)
            OutErr = TEXT("enum has no underlying"); return false;
        }
        if (FByteProperty* ByteP = CastField<FByteProperty>(Prop))                        // incl. TEnumAsByte
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
                : StaticLoadObject(ObjP->PropertyClass, nullptr, *PathStr);              // VERIFY vs engine source: FObjectPropertyBase::PropertyClass + StaticLoadObject(UClass*,Outer,path)
            ObjP->SetObjectPropertyValue(ValuePtr, Obj);
            return true;
        }
        // Struct/array/map/etc.: universal text import. Caller passes a UE ExportText string (as a JSON string),
        // e.g. "(X=1.0,Y=2.0,Z=3.0)". Numbers/bools also import fine here as a fallback.
        FString Text;
        if (V->Type == EJson::String) { Text = V->AsString(); }
        else if (V->Type == EJson::Boolean) { Text = V->AsBool() ? TEXT("true") : TEXT("false"); }
        else if (V->Type == EJson::Number)
        {
            const double D = V->AsNumber();
            Text = (D == FMath::TruncToDouble(D)) ? FString::Printf(TEXT("%lld"), (int64)D) : FString::SanitizeFloat(D);
        }
        else { OutErr = TEXT("unsupported JSON value for struct/array prop (pass a UE ExportText string)"); return false; }

        const TCHAR* Result = Prop->ImportText_Direct(*Text, ValuePtr, OwnerNode, PPF_None, nullptr); // VERIFY vs engine source: FProperty::ImportText_Direct(const TCHAR*, void*, UObject*, int32, FOutputDevice*) (UE5.1+)
        if (Result == nullptr) { OutErr = FString::Printf(TEXT("ImportText failed for '%s'"), *Text); return false; }
        return true;
    }
}

FString UMCPReflectionLibrary::AddEnvQueryOption(UEnvQuery* Query, const FString& GeneratorClassPath)
{
#if WITH_EDITOR
    if (!Query) { return SerializeJson(ErrorObj(TEXT("null query"))); }
    if (GeneratorClassPath.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("generator class path is required"))); }

    FObjectPropertyBase* OptInner = nullptr;
    FArrayProperty* OptArr = EqsObjArrayProp(Query, TEXT("Options"), OptInner);           // VERIFY vs engine source: UEnvQuery::Options
    if (!OptArr) { return SerializeJson(ErrorObj(TEXT("could not resolve UEnvQuery::Options array property"))); }

    UClass* OptionClass = OptInner->PropertyClass;                                        // = UEnvQueryOption::StaticClass()  // VERIFY vs engine source
    if (!OptionClass) { return SerializeJson(ErrorObj(TEXT("Options inner has no PropertyClass"))); }

    UClass* GenClass = EqsResolveClass(GeneratorClassPath);
    if (!GenClass) { return SerializeJson(ErrorObj(FString::Printf(TEXT("could not resolve generator class '%s'"), *GeneratorClassPath))); }
    if (GenClass->HasAnyClassFlags(CLASS_Abstract)) { return SerializeJson(ErrorObj(FString::Printf(TEXT("generator class '%s' is abstract"), *GeneratorClassPath))); }

    // Create the option (outer=Query), then discover its Generator property to validate + set.
    UObject* Option = NewObject<UObject>(Query, OptionClass, NAME_None, RF_Transactional); // VERIFY vs engine source: RF_Transactional for undo
    if (!Option) { return SerializeJson(ErrorObj(TEXT("failed to construct UEnvQueryOption"))); }

    FObjectPropertyBase* GenProp = FindFProperty<FObjectPropertyBase>(OptionClass, TEXT("Generator")); // VERIFY vs engine source: UEnvQueryOption::Generator
    if (!GenProp) { return SerializeJson(ErrorObj(TEXT("could not resolve UEnvQueryOption::Generator property"))); }
    if (GenProp->PropertyClass && !GenClass->IsChildOf(GenProp->PropertyClass))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("'%s' is not a %s"), *GeneratorClassPath, *GenProp->PropertyClass->GetName())));
    }

    UObject* Generator = NewObject<UObject>(Option, GenClass, NAME_None, RF_Transactional);
    if (!Generator) { return SerializeJson(ErrorObj(TEXT("failed to construct generator"))); }

    Query->Modify();
    Query->PreEditChange(OptArr);
    GenProp->SetObjectPropertyValue_InContainer(Option, Generator);                       // VERIFY vs engine source: FObjectPropertyBase::SetObjectPropertyValue_InContainer(void*, UObject*)
    int32 NewIndex = INDEX_NONE;
    {
        FScriptArrayHelper Helper(OptArr, OptArr->ContainerPtrToValuePtr<void>(Query));
        NewIndex = Helper.AddValue();
        OptInner->SetObjectPropertyValue(Helper.GetRawPtr(NewIndex), Option);
    }
    Query->PostEditChange();
    Query->MarkPackageDirty();

    FObjectPropertyBase* AfterInner = nullptr;
    FArrayProperty* AfterArr = EqsObjArrayProp(Query, TEXT("Options"), AfterInner);
    const int32 Count = AfterArr ? FScriptArrayHelper(AfterArr, AfterArr->ContainerPtrToValuePtr<void>(Query)).Num() : (NewIndex + 1);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetNumberField(TEXT("option_index"), NewIndex);
    Root->SetStringField(TEXT("option_name"), Option->GetName());
    Root->SetStringField(TEXT("generator_class"), GenClass->GetName());
    Root->SetNumberField(TEXT("option_count"), Count);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEnvQueryOption(UEnvQuery* Query, int32 OptionIndex)
{
#if WITH_EDITOR
    if (!Query) { return SerializeJson(ErrorObj(TEXT("null query"))); }
    FObjectPropertyBase* Inner = nullptr;
    FArrayProperty* Arr = EqsObjArrayProp(Query, TEXT("Options"), Inner);
    if (!Arr) { return SerializeJson(ErrorObj(TEXT("could not resolve UEnvQuery::Options array property"))); }

    Query->Modify();
    Query->PreEditChange(Arr);
    int32 Count = 0;
    {
        FScriptArrayHelper Helper(Arr, Arr->ContainerPtrToValuePtr<void>(Query));
        if (OptionIndex < 0 || OptionIndex >= Helper.Num())
        {
            return SerializeJson(ErrorObj(FString::Printf(TEXT("option index %d out of range [0,%d)"), OptionIndex, Helper.Num())));
        }
        Helper.RemoveValues(OptionIndex, 1);                                             // VERIFY vs engine source: FScriptArrayHelper::RemoveValues(int32,int32)
        Count = Helper.Num();
    }
    Query->PostEditChange();
    Query->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetNumberField(TEXT("option_index"), OptionIndex);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetNumberField(TEXT("option_count"), Count);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::AddEnvQueryTest(UEnvQuery* Query, int32 OptionIndex, const FString& TestClassPath)
{
#if WITH_EDITOR
    if (!Query) { return SerializeJson(ErrorObj(TEXT("null query"))); }
    if (TestClassPath.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("test class path is required"))); }

    UObject* Option = EqsGetOption(Query, OptionIndex);
    if (!Option) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no option at index %d"), OptionIndex))); }

    FObjectPropertyBase* TestInner = nullptr;
    FArrayProperty* TestArr = EqsObjArrayProp(Option, TEXT("Tests"), TestInner);          // VERIFY vs engine source: UEnvQueryOption::Tests
    if (!TestArr) { return SerializeJson(ErrorObj(TEXT("could not resolve UEnvQueryOption::Tests array property"))); }

    UClass* TestClass = EqsResolveClass(TestClassPath);
    if (!TestClass) { return SerializeJson(ErrorObj(FString::Printf(TEXT("could not resolve test class '%s'"), *TestClassPath))); }
    if (TestClass->HasAnyClassFlags(CLASS_Abstract)) { return SerializeJson(ErrorObj(FString::Printf(TEXT("test class '%s' is abstract"), *TestClassPath))); }
    if (TestInner->PropertyClass && !TestClass->IsChildOf(TestInner->PropertyClass))
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("'%s' is not a %s"), *TestClassPath, *TestInner->PropertyClass->GetName())));
    }

    UObject* Test = NewObject<UObject>(Option, TestClass, NAME_None, RF_Transactional);
    if (!Test) { return SerializeJson(ErrorObj(TEXT("failed to construct test"))); }

    Option->Modify();
    Query->Modify();
    Option->PreEditChange(TestArr);
    int32 NewIndex = INDEX_NONE;
    {
        FScriptArrayHelper Helper(TestArr, TestArr->ContainerPtrToValuePtr<void>(Option));
        NewIndex = Helper.AddValue();
        TestInner->SetObjectPropertyValue(Helper.GetRawPtr(NewIndex), Test);
    }
    Option->PostEditChange();
    Query->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetNumberField(TEXT("option_index"), OptionIndex);
    Root->SetNumberField(TEXT("test_index"), NewIndex);
    Root->SetStringField(TEXT("test_class"), TestClass->GetName());
    Root->SetNumberField(TEXT("test_count"), NewIndex + 1);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::RemoveEnvQueryTest(UEnvQuery* Query, int32 OptionIndex, int32 TestIndex)
{
#if WITH_EDITOR
    if (!Query) { return SerializeJson(ErrorObj(TEXT("null query"))); }
    UObject* Option = EqsGetOption(Query, OptionIndex);
    if (!Option) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no option at index %d"), OptionIndex))); }

    FObjectPropertyBase* TestInner = nullptr;
    FArrayProperty* TestArr = EqsObjArrayProp(Option, TEXT("Tests"), TestInner);
    if (!TestArr) { return SerializeJson(ErrorObj(TEXT("could not resolve UEnvQueryOption::Tests array property"))); }

    Option->Modify();
    Query->Modify();
    Option->PreEditChange(TestArr);
    int32 Count = 0;
    {
        FScriptArrayHelper Helper(TestArr, TestArr->ContainerPtrToValuePtr<void>(Option));
        if (TestIndex < 0 || TestIndex >= Helper.Num())
        {
            return SerializeJson(ErrorObj(FString::Printf(TEXT("test index %d out of range [0,%d)"), TestIndex, Helper.Num())));
        }
        Helper.RemoveValues(TestIndex, 1);
        Count = Helper.Num();
    }
    Option->PostEditChange();
    Query->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetNumberField(TEXT("option_index"), OptionIndex);
    Root->SetNumberField(TEXT("test_index"), TestIndex);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetNumberField(TEXT("test_count"), Count);
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}

FString UMCPReflectionLibrary::SetEnvQueryNodeProperty(UEnvQuery* Query, const FString& NodeLocator, const FString& PropName, const FString& ValueJson)
{
#if WITH_EDITOR
    if (!Query) { return SerializeJson(ErrorObj(TEXT("null query"))); }
    if (NodeLocator.IsEmpty() || PropName.IsEmpty()) { return SerializeJson(ErrorObj(TEXT("node locator and prop name are required"))); }

    // --- Parse NodeLocator: "option:<i>[/generator | /test:<j>]" ---------------------------
    TArray<FString> Parts; NodeLocator.ToLower().ParseIntoArray(Parts, TEXT("/"), true);
    if (Parts.Num() < 1 || !Parts[0].StartsWith(TEXT("option:")))
    {
        return SerializeJson(ErrorObj(TEXT("locator must start with 'option:<i>'")));
    }
    int32 OptionIndex = INDEX_NONE;
    LexFromString(OptionIndex, *Parts[0].RightChop(7)); // after "option:"
    UObject* Option = EqsGetOption(Query, OptionIndex);
    if (!Option) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no option at index %d"), OptionIndex))); }

    UObject* Node = nullptr;
    FString NodeDesc;
    if (Parts.Num() == 1)
    {
        Node = Option; NodeDesc = FString::Printf(TEXT("option:%d"), OptionIndex);
    }
    else if (Parts[1] == TEXT("generator"))
    {
        FObjectPropertyBase* GenProp = FindFProperty<FObjectPropertyBase>(Option->GetClass(), TEXT("Generator"));
        Node = GenProp ? GenProp->GetObjectPropertyValue_InContainer(Option) : nullptr;
        NodeDesc = FString::Printf(TEXT("option:%d/generator"), OptionIndex);
    }
    else if (Parts[1].StartsWith(TEXT("test:")))
    {
        int32 TestIndex = INDEX_NONE;
        LexFromString(TestIndex, *Parts[1].RightChop(5)); // after "test:"
        FObjectPropertyBase* TestInner = nullptr;
        FArrayProperty* TestArr = EqsObjArrayProp(Option, TEXT("Tests"), TestInner);
        if (TestArr)
        {
            FScriptArrayHelper Helper(TestArr, TestArr->ContainerPtrToValuePtr<void>(Option));
            if (TestIndex >= 0 && TestIndex < Helper.Num())
            {
                Node = TestInner->GetObjectPropertyValue(Helper.GetRawPtr(TestIndex));
            }
        }
        NodeDesc = FString::Printf(TEXT("option:%d/test:%d"), OptionIndex, TestIndex);
    }
    else
    {
        return SerializeJson(ErrorObj(FString::Printf(TEXT("bad locator segment '%s'"), *Parts[1])));
    }
    if (!Node) { return SerializeJson(ErrorObj(FString::Printf(TEXT("no node at '%s'"), *NodeDesc))); }

    // --- Resolve the config FProperty by name ---------------------------------------------
    FProperty* Prop = FindFProperty<FProperty>(Node->GetClass(), *PropName);
    if (!Prop) { return SerializeJson(ErrorObj(FString::Printf(TEXT("node '%s' (%s) has no property '%s'"), *NodeDesc, *Node->GetClass()->GetName(), *PropName))); }

    // --- Parse ValueJson as a bare JSON value (reuse the SetNiagaraUserParameterValue idiom) ---
    TSharedPtr<FJsonValue> V;
    TSharedRef<TJsonReader<>> JReader = TJsonReaderFactory<>::Create(ValueJson);
    if (!FJsonSerializer::Deserialize(JReader, V) || !V.IsValid())
    {
        return SerializeJson(ErrorObj(TEXT("invalid value JSON")));
    }
    // UE's JSON reader rejects a BARE scalar/string at document root, so callers pass array-wrapped values
    // (the SetNiagaraUserParameterValue idiom): [123.0] for a numeric, [true] for a bool, ["EnumName"] for an
    // enum, ["(DefaultValue=1.0)"] / ["(X=1,Y=2,Z=3)"] for struct props (EQS configs like InnerRadius are
    // FAIDataProvider* structs -> ExportText string). Unwrap a single-element array so the typed fast-paths +
    // the ExportText fallback in EqsApplyJsonToProperty see the scalar/string directly.
    if (V->Type == EJson::Array)
    {
        const TArray<TSharedPtr<FJsonValue>>& VArr = V->AsArray();
        if (VArr.Num() == 1 && VArr[0].IsValid()) { V = VArr[0]; }
    }

    // --- Capture PRIOR value (for undo/inverse) via the reader helper, then apply ----------
    void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Node);
    // C++ #17: EqsPropertyToJson gained Depth/Visited args (bounded/crash-proof struct export); pass a fresh set.
    TSet<const void*> _PrevVisited;
    TSharedPtr<FJsonValue> Prev = EqsPropertyToJson(Prop, ValuePtr, /*Depth*/ 0, _PrevVisited); // reader helper (earlier anon ns, same TU)

    Node->Modify();
    Query->Modify();
    Node->PreEditChange(Prop);
    FString ApplyErr;
    const bool bOk = EqsApplyJsonToProperty(Prop, ValuePtr, Node, V, ApplyErr);
    if (!bOk)
    {
        return SerializeJson(ErrorObj(ApplyErr.IsEmpty() ? TEXT("failed to set property") : ApplyErr));
    }
    {
        FPropertyChangedEvent Evt(Prop);                                                 // VERIFY vs engine source: FPropertyChangedEvent(FProperty*)
        Node->PostEditChangeProperty(Evt);
    }
    Query->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetStringField(TEXT("node"), NodeDesc);
    Root->SetStringField(TEXT("prop"), PropName);
    Root->SetBoolField(TEXT("set"), true);
    Root->SetField(TEXT("prev"), Prev.IsValid() ? Prev : MakeShared<FJsonValueNull>());
    return SerializeJson(Root);
#else
    return SerializeJson(ErrorObj(TEXT("editor-only")));
#endif
}
