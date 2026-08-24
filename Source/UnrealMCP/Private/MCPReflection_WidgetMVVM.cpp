// ============================================================================
// MCPReflection_WidgetMVVM.cpp  —  WIDGETS "W-D" batch: UMG Model-View-ViewModel
//   authoring on a UWidgetBlueprint. Add/remove/rename/list viewmodels; add/set/
//   remove/list MVVM bindings; list conversion functions; set viewmodel settings;
//   mark a Blueprint variable FieldNotify. Eleven DEFERRED MCPReflectionLibrary
//   handlers that widget_mvvm_cpp.py hasattr-guards on; when these link the Python
//   tools auto-enable.
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** (mirrors
// MCPReflection_WidgetAnim.cpp / MCPReflection_Niagara2.cpp). Anon-namespace
// helpers prefixed MCPMvvm_ for unity-build uniqueness.
//
// >>> THE BIG FEASIBILITY FINDING <<<  Unlike the Niagara/BehaviorTree/StateTree
//   editor batches, MVVM ships a FULLY-EXPORTED public editor subsystem,
//   UMVVMEditorSubsystem (ModelViewViewModelEditor, MODELVIEWVIEWMODELEDITOR_API),
//   which is the EXACT API the MVVM editor UI drives. Every mutator we need is
//   either a BlueprintCallable UFUNCTION or a UE_API (=MODELVIEWVIEWMODELEDITOR_API)
//   method -> all exported -> **NO source-engine export patch is required** for
//   this batch (the top risk in the brief does NOT materialise). Routing through
//   the subsystem gives us the engine's own FScopedTransaction + OnBindingPre/
//   PostEditChange + MarkBlueprintAsModified semantics for free.
//
// >>> BUILD STORY <<<  This batch needs the beta ModelViewViewModel plugin ENABLED
//   and THREE of its modules linked (the brief named two; the editor subsystem is a
//   third that is genuinely required):
//     ModelViewViewModel          (Runtime)      — EMVVMBindingMode / EMVVMExecutionMode
//                                                   / UE::MVVM::FMVVMConstFieldVariant.
//     ModelViewViewModelBlueprint (UncookedOnly) — UMVVMBlueprintView,
//                                                   FMVVMBlueprintViewModelContext,
//                                                   FMVVMBlueprintViewBinding,
//                                                   FMVVMBlueprintPropertyPath,
//                                                   FMVVMBlueprintFunctionReference,
//                                                   UMVVMWidgetBlueprintExtension_View.
//     ModelViewViewModelEditor    (Editor)       — UMVVMEditorSubsystem (+ its
//                                                   Set*ForBinding / GetConversionFunctions),
//                                                   UE::MVVM::FConversionFunctionValue.
//   UMG + UMGEditor (UWidget / UWidgetTree / UWidgetBlueprint / UWidgetBlueprintExtension)
//   and UnrealEd + BlueprintGraph (FBlueprintEditorUtils / FBlueprintMetadata) are
//   ALREADY PublicDependencyModuleNames -> no change for those. set_variable_field_notify
//   is pure Kismet (needs NO MVVM module at all).
//
// >>> CONFIRMED 5.8 signatures (header:line verified vs the source engine) <<<
//   UMVVMEditorSubsystem (Editor/Public/MVVMEditorSubsystem.h):
//     RequestView/GetView(UWidgetBlueprint*) -> UMVVMBlueprintView*                    :41,44
//     AddViewModel(UWidgetBlueprint*, const UClass*) -> FGuid                          :47  (validates NotifyFieldValueChanged; returns invalid FGuid if bad)
//     RemoveViewModel(UWidgetBlueprint*, FName)                                        :53
//     RenameViewModel(UWidgetBlueprint*, FName, FName, FText&) -> bool                 :59
//     ReparentViewModel(UWidgetBlueprint*, FName, const UClass*, FText&) -> bool       :62
//     AddBinding(UWidgetBlueprint*) -> FMVVMBlueprintViewBinding&                      :65  (returns a live ref INTO the view's Bindings array)
//     RemoveBinding(UWidgetBlueprint*, const FMVVMBlueprintViewBinding&)               :68  (matches by POINTER -> pass the array element ref)
//     SetSourcePathForBinding / SetDestinationPathForBinding(.., bool)                 :91,92
//     SetBindingTypeForBinding / OverrideExecutionModeForBinding / ResetExecutionModeForBinding :95,93,94
//     SetEnabledForBinding / SetCompileForBinding                                      :96,97
//     SetSourceToDestinationConversionFunction(.., FMVVMBlueprintFunctionReference)    :87
//     GetConversionFunctions(WBP, const FProperty*, const FProperty*) -> TArray<FConversionFunctionValue> :141 (null,null = all)
//   UMVVMBlueprintView (Blueprint/Public/MVVMBlueprintView.h):
//     GetViewModels() -> TArrayView<const FMVVMBlueprintViewModelContext>              :105
//     FindViewModel(FGuid) [mutable] / FindViewModel(FName) [const]                    :95,97
//     GetBinding(FGuid) [mutable] / GetBindings() / GetNumBindings()                   :127,130,120
//   FMVVMBlueprintViewModelContext (Blueprint/Public/MVVMBlueprintViewModelContext.h): all UPROPERTYs public;
//     GetViewModelId/Name/Class(), ctor(const UClass*,FName) generates the FGuid       :54,59,66,52
//   FMVVMBlueprintPropertyPath (Blueprint/Public/MVVMPropertyPath.h): inline public
//     SetViewModelId(FGuid) / SetWidgetName(FName) / SetSelfContext() / AppendPropertyPath(const UBlueprint*, FMVVMConstFieldVariant) / GetFieldNames(const UClass*)  :238,250,257,191,158
//     -> AppendPropertyPath emplaces the MODELVIEWVIEWMODELBLUEPRINT_API FMVVMBlueprintFieldPath(const UBlueprint*, FMVVMConstFieldVariant) ctor.
//   FMVVMBlueprintViewBinding (Blueprint/Public/MVVMBlueprintViewBinding.h): SourcePath/DestinationPath/
//     BindingType/bOverrideExecutionMode/OverrideExecutionMode/bEnabled/bCompile/BindingId all public :97-127
//   FieldNotify (engine): FBlueprintEditorUtils::SetBlueprintVariableMetaData / RemoveBlueprintVariableMetaData /
//     RemoveFieldNotifyFromAllMetadata + FBlueprintMetadata::MD_FieldNotify (mirrors Kismet/FieldNotifyToggle.cpp:299-305)
//
// CRASH-SAFETY: every load / subsystem / view / lookup is null-guarded; handlers
// return {"error":...} on any miss (never crash). Reads are non-mutating. The
// subsystem mutators already MarkBlueprintAsModified; direct-member edits end with
// MarkBlueprintAsStructurallyModified. The Python side finalizes each write with a
// compile + save and records the inverse on the per-session ledger for
// editor_level.undo to fold. VERIFY-tagged calls are version-sensitive.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Reflection / core ---
#include "UObject/Class.h"
#include "UObject/UnrealType.h"            // FProperty / FindFProperty
#include "UObject/Field.h"
#include "UObject/SoftObjectPath.h"
#include "UObject/UObjectGlobals.h"
#include "Misc/PackageName.h"

#if WITH_EDITOR
// --- UMG runtime (UMG_API — already a dep) ---
#include "Blueprint/WidgetTree.h"          // UWidgetTree::FindWidget
#include "Components/Widget.h"             // UWidget

// --- UMGEditor (UMGEDITOR_API — already a dep) ---
#include "WidgetBlueprint.h"               // UWidgetBlueprint

// --- Blueprint editor utils / metadata (UnrealEd + BlueprintGraph — already deps) ---
#include "Kismet2/BlueprintEditorUtils.h"  // FBlueprintEditorUtils
#include "EdGraphSchema_K2.h"              // FBlueprintMetadata::MD_FieldNotify

// --- Editor (GEditor / GetEditorSubsystem) ---
#include "Editor.h"

// --- MVVM: ModelViewViewModel (Runtime) ---
#include "Types/MVVMBindingMode.h"         // EMVVMBindingMode
#include "Types/MVVMExecutionMode.h"       // EMVVMExecutionMode
#include "Types/MVVMFieldVariant.h"        // UE::MVVM::FMVVMConstFieldVariant

// --- MVVM: ModelViewViewModelBlueprint (UncookedOnly) ---
#include "MVVMWidgetBlueprintExtension_View.h" // UMVVMWidgetBlueprintExtension_View
#include "MVVMBlueprintView.h"                 // UMVVMBlueprintView + FMVVMBlueprintViewModelContext
#include "MVVMBlueprintViewBinding.h"          // FMVVMBlueprintViewBinding
#include "MVVMPropertyPath.h"                  // FMVVMBlueprintPropertyPath
#include "MVVMBlueprintFunctionReference.h"    // FMVVMBlueprintFunctionReference

// --- MVVM: ModelViewViewModelEditor (Editor) ---
#include "MVVMEditorSubsystem.h"               // UMVVMEditorSubsystem
#include "Types/MVVMConversionFunctionValue.h" // UE::MVVM::FConversionFunctionValue
#endif // WITH_EDITOR

namespace
{
    // ---- JSON helpers (always available; the #else branches use them) ---------
    FString MCPMvvm_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPMvvm_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPMvvm_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UWidgetBlueprint from a package/asset path (copied from MCPReflection_WidgetAnim.cpp).
    UWidgetBlueprint* MCPMvvm_LoadWBP(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(FSoftObjectPath(Path).TryLoad()))
        {
            return WBP;
        }
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UWidgetBlueprint>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Resolve any UBlueprint (for set_variable_field_notify — works on non-widget BPs too).
    UBlueprint* MCPMvvm_LoadBP(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UBlueprint* BP = Cast<UBlueprint>(FSoftObjectPath(Path).TryLoad()))
        {
            return BP;
        }
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UBlueprint>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Resolve a UClass from a path. Accepts a native class path ("/Script/Module.Class"),
    // a generated-class path (".../BP_Foo.BP_Foo_C") or a plain Blueprint asset path
    // (".../BP_Foo.BP_Foo" or ".../BP_Foo" -> loads the BP and returns its GeneratedClass).
    UClass* MCPMvvm_ResolveClass(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        // Native or explicit generated class path.
        if (UClass* Direct = FSoftClassPath(Path).TryLoadClass<UObject>())
        {
            return Direct;
        }
        // Blueprint asset path -> GeneratedClass.
        if (UBlueprint* BP = MCPMvvm_LoadBP(Path))
        {
            return BP->GeneratedClass;
        }
        // Bare short name of a native class.
        if (UClass* Found = UClass::TryFindTypeSlow<UClass>(Path))
        {
            return Found;
        }
        return nullptr;
    }

    UMVVMEditorSubsystem* MCPMvvm_Subsystem()
    {
        return GEditor ? GEditor->GetEditorSubsystem<UMVVMEditorSubsystem>() : nullptr;
    }

    template<typename TEnum>
    FString MCPMvvm_EnumToString(TEnum Value)
    {
        if (const UEnum* E = StaticEnum<TEnum>())
        {
            return E->GetNameStringByValue((int64)Value);
        }
        return FString::Printf(TEXT("%d"), (int32)Value);
    }

    // Case-insensitive parse of an enum name (accepts the bare enumerator, e.g. "TwoWay").
    template<typename TEnum>
    bool MCPMvvm_EnumFromString(const FString& Str, TEnum& OutValue)
    {
        const UEnum* E = StaticEnum<TEnum>();
        if (!E || Str.IsEmpty())
        {
            return false;
        }
        for (int32 i = 0; i < E->NumEnums(); ++i)
        {
            const FString EntryName = E->GetNameStringByIndex(i);
            if (EntryName.EndsWith(TEXT("_MAX")))
            {
                continue; // skip the auto-generated sentinel when present
            }
            if (EntryName.Equals(Str, ESearchCase::IgnoreCase))
            {
                OutValue = (TEnum)E->GetValueByIndex(i);
                return true;
            }
        }
        // Also accept a raw integer.
        if (Str.IsNumeric())
        {
            OutValue = (TEnum)FCString::Atoi(*Str);
            return true;
        }
        return false;
    }

    // Resolve one field name on a UStruct owner -> a const field variant (property first, then function).
    UE::MVVM::FMVVMConstFieldVariant MCPMvvm_ResolveField(UStruct* Owner, const FString& FieldName)
    {
        if (!Owner || FieldName.IsEmpty())
        {
            return UE::MVVM::FMVVMConstFieldVariant();
        }
        if (const FProperty* Prop = FindFProperty<FProperty>(Owner, *FieldName))
        {
            return UE::MVVM::FMVVMConstFieldVariant(Prop);
        }
        if (UClass* AsClass = Cast<UClass>(Owner))
        {
            if (const UFunction* Func = AsClass->FindFunctionByName(*FieldName))
            {
                return UE::MVVM::FMVVMConstFieldVariant(Func);
            }
        }
        return UE::MVVM::FMVVMConstFieldVariant();
    }

    // Advance the owning UStruct across a resolved field for a dotted sub-path
    // (object property -> its class ; struct property -> its script struct). Returns
    // nullptr if the field cannot host a sub-property.
    UStruct* MCPMvvm_AdvanceOwner(const UE::MVVM::FMVVMConstFieldVariant& Field)
    {
        if (Field.IsProperty())
        {
            const FProperty* P = Field.GetProperty();
            if (const FObjectPropertyBase* ObjProp = CastField<FObjectPropertyBase>(P))
            {
                return ObjProp->PropertyClass;
            }
            if (const FStructProperty* StructProp = CastField<FStructProperty>(P))
            {
                return StructProp->Struct;
            }
        }
        else if (Field.IsFunction())
        {
            // Returning the function's return-property owner is out of MVP scope.
            return nullptr;
        }
        return nullptr;
    }

    // Build a property path by walking a (possibly dotted) field path against a starting owner.
    // Returns false + OutErr on the first segment that cannot be resolved.
    bool MCPMvvm_AppendDottedPath(FMVVMBlueprintPropertyPath& Path, const UBlueprint* Context,
                                  UStruct* StartOwner, const FString& DottedPath, FString& OutErr)
    {
        if (DottedPath.IsEmpty())
        {
            OutErr = TEXT("empty property path");
            return false;
        }
        TArray<FString> Segments;
        DottedPath.ParseIntoArray(Segments, TEXT("."), true);
        UStruct* Owner = StartOwner;
        for (int32 i = 0; i < Segments.Num(); ++i)
        {
            if (!Owner)
            {
                OutErr = FString::Printf(TEXT("cannot resolve segment '%s' (no owner struct for sub-path)"), *Segments[i]);
                return false;
            }
            UE::MVVM::FMVVMConstFieldVariant Field = MCPMvvm_ResolveField(Owner, Segments[i]);
            if (Field.IsEmpty())
            {
                OutErr = FString::Printf(TEXT("field '%s' not found on '%s'"), *Segments[i], *Owner->GetName());
                return false;
            }
            Path.AppendPropertyPath(Context, Field);
            if (i + 1 < Segments.Num())
            {
                Owner = MCPMvvm_AdvanceOwner(Field);
            }
        }
        return true;
    }

    void MCPMvvm_FillViewModelJson(const FMVVMBlueprintViewModelContext& Ctx, const TSharedRef<FJsonObject>& Obj)
    {
        Obj->SetStringField(TEXT("name"), Ctx.GetViewModelName().ToString());
        Obj->SetStringField(TEXT("guid"), Ctx.GetViewModelId().ToString(EGuidFormats::DigitsWithHyphens));
        if (const UClass* VMClass = Ctx.GetViewModelClass())
        {
            Obj->SetStringField(TEXT("class_path"), VMClass->GetPathName());
            Obj->SetStringField(TEXT("class_name"), VMClass->GetName());
        }
        else
        {
            Obj->SetField(TEXT("class_path"), MakeShared<FJsonValueNull>());
        }
        Obj->SetStringField(TEXT("creation_type"), MCPMvvm_EnumToString(Ctx.CreationType));
        Obj->SetStringField(TEXT("global_identifier"), Ctx.GlobalViewModelIdentifier.ToString());
        Obj->SetStringField(TEXT("property_path"), Ctx.ViewModelPropertyPath);
        Obj->SetBoolField(TEXT("is_instanced"), Ctx.InstancedViewModel != nullptr);
        Obj->SetBoolField(TEXT("has_resolver"), Ctx.Resolver != nullptr);
        Obj->SetBoolField(TEXT("expose_instance_in_editor"), Ctx.bExposeInstanceInEditor);
        Obj->SetBoolField(TEXT("create_setter"), Ctx.bCreateSetterFunction);
        Obj->SetBoolField(TEXT("create_getter"), Ctx.bCreateGetterFunction);
        Obj->SetBoolField(TEXT("optional"), Ctx.bOptional);
        Obj->SetBoolField(TEXT("can_rename"), Ctx.CanRename());
        Obj->SetBoolField(TEXT("can_remove"), Ctx.bCanRemove);
    }

    // Serialize a property path endpoint (source or destination) into a JSON object.
    void MCPMvvm_FillPathJson(const FMVVMBlueprintPropertyPath& PPath, const UClass* SelfContext,
                              const TSharedRef<FJsonObject>& Obj)
    {
        FGuid VmId = PPath.GetViewModelId();
        FName WidgetName = PPath.GetWidgetName();
        if (VmId.IsValid())
        {
            Obj->SetStringField(TEXT("kind"), TEXT("viewmodel"));
            Obj->SetStringField(TEXT("viewmodel_id"), VmId.ToString(EGuidFormats::DigitsWithHyphens));
        }
        else if (!WidgetName.IsNone())
        {
            Obj->SetStringField(TEXT("kind"), TEXT("widget"));
            Obj->SetStringField(TEXT("widget"), WidgetName.ToString());
        }
        else
        {
            Obj->SetStringField(TEXT("kind"), PPath.HasPaths() ? TEXT("self") : TEXT("none"));
        }
        TArray<FName> FieldNames = PPath.GetFieldNames(SelfContext);
        FString Joined;
        for (int32 i = 0; i < FieldNames.Num(); ++i)
        {
            Joined += (i ? TEXT(".") : TEXT("")) + FieldNames[i].ToString();
        }
        Obj->SetStringField(TEXT("path"), Joined);
    }

    void MCPMvvm_FillBindingJson(const FMVVMBlueprintViewBinding& B, const UClass* SelfContext,
                                 const TSharedRef<FJsonObject>& Obj)
    {
        Obj->SetStringField(TEXT("binding_id"), B.BindingId.ToString(EGuidFormats::DigitsWithHyphens));
        Obj->SetStringField(TEXT("binding_mode"), MCPMvvm_EnumToString(B.BindingType));
        Obj->SetBoolField(TEXT("enabled"), B.bEnabled);
        Obj->SetBoolField(TEXT("compile"), B.bCompile);
        Obj->SetBoolField(TEXT("override_execution_mode"), B.bOverrideExecutionMode);
        if (B.bOverrideExecutionMode)
        {
            Obj->SetStringField(TEXT("execution_mode"), MCPMvvm_EnumToString(B.OverrideExecutionMode));
        }
        Obj->SetBoolField(TEXT("has_source_to_dest_conversion"), B.Conversion.SourceToDestinationConversion != nullptr);
        Obj->SetBoolField(TEXT("has_dest_to_source_conversion"), B.Conversion.DestinationToSourceConversion != nullptr);

        TSharedRef<FJsonObject> Src = MakeShared<FJsonObject>();
        MCPMvvm_FillPathJson(B.SourcePath, SelfContext, Src);
        Obj->SetObjectField(TEXT("source"), Src);

        TSharedRef<FJsonObject> Dst = MakeShared<FJsonObject>();
        MCPMvvm_FillPathJson(B.DestinationPath, SelfContext, Dst);
        Obj->SetObjectField(TEXT("destination"), Dst);
    }
#endif // WITH_EDITOR
} // namespace


// ============================================================================
// 1) GetMvvmViewmodelsJson  (read) -> get_mvvm_viewmodels_json
// ============================================================================
FString UMCPReflectionLibrary::GetMvvmViewmodelsJson(const FString& WidgetBlueprintPath)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable (is the ModelViewViewModel plugin enabled?)"));
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());

    UMVVMBlueprintView* View = Sub->GetView(WBP);
    TArray<TSharedPtr<FJsonValue>> Arr;
    if (View)
    {
        for (const FMVVMBlueprintViewModelContext& Ctx : View->GetViewModels())
        {
            TSharedRef<FJsonObject> Vm = MakeShared<FJsonObject>();
            MCPMvvm_FillViewModelJson(Ctx, Vm);
            Arr.Add(MakeShared<FJsonValueObject>(Vm));
        }
    }
    Root->SetBoolField(TEXT("has_view"), View != nullptr);
    Root->SetNumberField(TEXT("viewmodel_count"), Arr.Num());
    Root->SetArrayField(TEXT("viewmodels"), Arr);
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 2) AddMvvmViewmodelJson  (write) -> add_mvvm_viewmodel_json
// ============================================================================
FString UMCPReflectionLibrary::AddMvvmViewmodelJson(const FString& WidgetBlueprintPath,
                                                    const FString& ViewModelClassPath,
                                                    const FString& DesiredName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    UClass* VMClass = MCPMvvm_ResolveClass(ViewModelClassPath);
    if (!VMClass)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("viewmodel class not found: %s"), *ViewModelClassPath));
    }
    // Ensure a view exists (AddViewModel uses GetView, not RequestView).
    Sub->RequestView(WBP);

    const FGuid NewId = Sub->AddViewModel(WBP, VMClass);
    if (!NewId.IsValid())
    {
        return MCPMvvm_Error(FString::Printf(
            TEXT("AddViewModel rejected class '%s' (it must implement NotifyFieldValueChanged, e.g. derive from UMVVMViewModelBase)"),
            *VMClass->GetName()));
    }

    UMVVMBlueprintView* View = Sub->GetView(WBP);
    const FMVVMBlueprintViewModelContext* Ctx = View ? View->FindViewModel(NewId) : nullptr;
    FString FinalName = Ctx ? Ctx->GetViewModelName().ToString() : FString();
    FString RenameNote;

    if (!DesiredName.IsEmpty() && FinalName != DesiredName)
    {
        FText OutError;
        if (Sub->RenameViewModel(WBP, FName(*FinalName), FName(*DesiredName), OutError))
        {
            FinalName = DesiredName;
        }
        else
        {
            RenameNote = FString::Printf(TEXT("kept auto name '%s' (rename to '%s' failed: %s)"),
                *FinalName, *DesiredName, *OutError.ToString());
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("guid"), NewId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("name"), FinalName);
    Root->SetStringField(TEXT("class_path"), VMClass->GetPathName());
    if (!RenameNote.IsEmpty())
    {
        Root->SetStringField(TEXT("note"), RenameNote);
    }
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 3) RemoveMvvmViewmodelJson  (write) -> remove_mvvm_viewmodel_json
// ============================================================================
FString UMCPReflectionLibrary::RemoveMvvmViewmodelJson(const FString& WidgetBlueprintPath,
                                                       const FString& ViewModelName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    UMVVMBlueprintView* View = Sub->GetView(WBP);
    if (!View)
    {
        return MCPMvvm_Error(TEXT("no MVVM view on this widget blueprint"));
    }
    const FMVVMBlueprintViewModelContext* Ctx = View->FindViewModel(FName(*ViewModelName));
    if (!Ctx)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("viewmodel '%s' not found"), *ViewModelName));
    }
    if (!Ctx->bCanRemove)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("viewmodel '%s' is not removable (bCanRemove=false)"), *ViewModelName));
    }
    // Capture for the (lossy) inverse.
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetStringField(TEXT("name"), ViewModelName);
    Root->SetStringField(TEXT("guid"), Ctx->GetViewModelId().ToString(EGuidFormats::DigitsWithHyphens));
    if (const UClass* VMClass = Ctx->GetViewModelClass())
    {
        Root->SetStringField(TEXT("class_path"), VMClass->GetPathName());
    }
    Root->SetStringField(TEXT("creation_type"), MCPMvvm_EnumToString(Ctx->CreationType));
    Root->SetStringField(TEXT("global_identifier"), Ctx->GlobalViewModelIdentifier.ToString());
    Root->SetStringField(TEXT("property_path"), Ctx->ViewModelPropertyPath);

    Sub->RemoveViewModel(WBP, FName(*ViewModelName));

    UMVVMBlueprintView* ViewAfter = Sub->GetView(WBP);
    const bool bStillPresent = ViewAfter && ViewAfter->FindViewModel(FName(*ViewModelName)) != nullptr;
    Root->SetBoolField(TEXT("removed"), !bStillPresent);
    Root->SetNumberField(TEXT("viewmodel_count"), ViewAfter ? ViewAfter->GetViewModels().Num() : 0);
    Root->SetStringField(TEXT("note"),
        TEXT("inverse re-adds a fresh viewmodel (NEW guid); bindings that referenced the old guid are not restored"));
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 4) RenameMvvmViewmodelJson  (write) -> rename_mvvm_viewmodel_json
// ============================================================================
FString UMCPReflectionLibrary::RenameMvvmViewmodelJson(const FString& WidgetBlueprintPath,
                                                       const FString& OldName, const FString& NewName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    FText OutError;
    const bool bRenamed = Sub->RenameViewModel(WBP, FName(*OldName), FName(*NewName), OutError);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetStringField(TEXT("old_name"), OldName);
    Root->SetStringField(TEXT("new_name"), NewName);
    Root->SetBoolField(TEXT("renamed"), bRenamed);
    if (!bRenamed)
    {
        Root->SetStringField(TEXT("error"),
            OutError.IsEmpty() ? TEXT("rename failed (name invalid, or viewmodel missing / not renamable)")
                               : OutError.ToString());
    }
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 5) SetMvvmViewmodelSettingsJson  (write) -> set_mvvm_viewmodel_settings_json
//    SettingsJson keys (all optional): creation_type (enum name), global_identifier,
//    property_path, create_setter, create_getter, optional, expose_instance,
//    class_path (reparent to a new viewmodel class).
// ============================================================================
FString UMCPReflectionLibrary::SetMvvmViewmodelSettingsJson(const FString& WidgetBlueprintPath,
                                                            const FString& ViewModelName,
                                                            const FString& SettingsJson)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    UMVVMBlueprintView* View = Sub->GetView(WBP);
    if (!View)
    {
        return MCPMvvm_Error(TEXT("no MVVM view on this widget blueprint"));
    }
    const FMVVMBlueprintViewModelContext* ConstCtx = View->FindViewModel(FName(*ViewModelName));
    if (!ConstCtx)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("viewmodel '%s' not found"), *ViewModelName));
    }
    // Mutable handle via the non-const FGuid overload.
    FMVVMBlueprintViewModelContext* Ctx = View->FindViewModel(ConstCtx->GetViewModelId());
    if (!Ctx)
    {
        return MCPMvvm_Error(TEXT("failed to resolve a mutable viewmodel context"));
    }

    TSharedPtr<FJsonObject> Settings;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SettingsJson);
        if (!FJsonSerializer::Deserialize(Reader, Settings) || !Settings.IsValid())
        {
            return MCPMvvm_Error(TEXT("settings_json is not a valid JSON object"));
        }
    }

    TSharedRef<FJsonObject> Prev = MakeShared<FJsonObject>();
    TSharedRef<FJsonObject> New = MakeShared<FJsonObject>();
    bool bAnyChange = false;

    // creation_type
    FString CtStr;
    if (Settings->TryGetStringField(TEXT("creation_type"), CtStr))
    {
        EMVVMBlueprintViewModelContextCreationType NewCt;
        if (!MCPMvvm_EnumFromString(CtStr, NewCt))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("unknown creation_type '%s'"), *CtStr));
        }
        Prev->SetStringField(TEXT("creation_type"), MCPMvvm_EnumToString(Ctx->CreationType));
        Ctx->CreationType = NewCt;
        New->SetStringField(TEXT("creation_type"), MCPMvvm_EnumToString(NewCt));
        bAnyChange = true;
    }
    // global_identifier
    FString GidStr;
    if (Settings->TryGetStringField(TEXT("global_identifier"), GidStr))
    {
        Prev->SetStringField(TEXT("global_identifier"), Ctx->GlobalViewModelIdentifier.ToString());
        Ctx->GlobalViewModelIdentifier = FName(*GidStr);
        New->SetStringField(TEXT("global_identifier"), GidStr);
        bAnyChange = true;
    }
    // property_path
    FString PpStr;
    if (Settings->TryGetStringField(TEXT("property_path"), PpStr))
    {
        Prev->SetStringField(TEXT("property_path"), Ctx->ViewModelPropertyPath);
        Ctx->ViewModelPropertyPath = PpStr;
        New->SetStringField(TEXT("property_path"), PpStr);
        bAnyChange = true;
    }
    // bools
    bool bTmp = false;
    if (Settings->TryGetBoolField(TEXT("create_setter"), bTmp))
    {
        Prev->SetBoolField(TEXT("create_setter"), Ctx->bCreateSetterFunction);
        Ctx->bCreateSetterFunction = bTmp; New->SetBoolField(TEXT("create_setter"), bTmp); bAnyChange = true;
    }
    if (Settings->TryGetBoolField(TEXT("create_getter"), bTmp))
    {
        Prev->SetBoolField(TEXT("create_getter"), Ctx->bCreateGetterFunction);
        Ctx->bCreateGetterFunction = bTmp; New->SetBoolField(TEXT("create_getter"), bTmp); bAnyChange = true;
    }
    if (Settings->TryGetBoolField(TEXT("optional"), bTmp))
    {
        Prev->SetBoolField(TEXT("optional"), Ctx->bOptional);
        Ctx->bOptional = bTmp; New->SetBoolField(TEXT("optional"), bTmp); bAnyChange = true;
    }
    if (Settings->TryGetBoolField(TEXT("expose_instance"), bTmp))
    {
        Prev->SetBoolField(TEXT("expose_instance"), Ctx->bExposeInstanceInEditor);
        Ctx->bExposeInstanceInEditor = bTmp; New->SetBoolField(TEXT("expose_instance"), bTmp); bAnyChange = true;
    }
    // class_path -> reparent (uses the subsystem so allowed-creation-type is re-validated).
    FString ClassPath;
    if (Settings->TryGetStringField(TEXT("class_path"), ClassPath))
    {
        UClass* NewClass = MCPMvvm_ResolveClass(ClassPath);
        if (!NewClass)
        {
            return MCPMvvm_Error(FString::Printf(TEXT("reparent class not found: %s"), *ClassPath));
        }
        if (const UClass* OldClass = Ctx->GetViewModelClass())
        {
            Prev->SetStringField(TEXT("class_path"), OldClass->GetPathName());
        }
        FText OutError;
        const bool bReparented = Sub->ReparentViewModel(WBP, FName(*ViewModelName), NewClass, OutError);
        if (!bReparented)
        {
            return MCPMvvm_Error(FString::Printf(TEXT("reparent to '%s' failed: %s"),
                *NewClass->GetName(), *OutError.ToString()));
        }
        New->SetStringField(TEXT("class_path"), NewClass->GetPathName());
        bAnyChange = true;
    }

    if (bAnyChange)
    {
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetStringField(TEXT("name"), ViewModelName);
    Root->SetBoolField(TEXT("set"), bAnyChange);
    Root->SetObjectField(TEXT("prev"), Prev);
    Root->SetObjectField(TEXT("new"), New);
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 6) GetMvvmBindingsJson  (read) -> get_mvvm_bindings_json
// ============================================================================
FString UMCPReflectionLibrary::GetMvvmBindingsJson(const FString& WidgetBlueprintPath)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    const UClass* SelfContext = WBP->SkeletonGeneratedClass ? WBP->SkeletonGeneratedClass.Get()
                                                            : WBP->GeneratedClass.Get();
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());

    UMVVMBlueprintView* View = Sub->GetView(WBP);
    TArray<TSharedPtr<FJsonValue>> Arr;
    if (View)
    {
        for (const FMVVMBlueprintViewBinding& B : View->GetBindings())
        {
            TSharedRef<FJsonObject> Bo = MakeShared<FJsonObject>();
            MCPMvvm_FillBindingJson(B, SelfContext, Bo);
            Arr.Add(MakeShared<FJsonValueObject>(Bo));
        }
    }
    Root->SetBoolField(TEXT("has_view"), View != nullptr);
    Root->SetNumberField(TEXT("binding_count"), Arr.Num());
    Root->SetArrayField(TEXT("bindings"), Arr);
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 7) AddMvvmBindingJson  (write) -> add_mvvm_binding_json
//    SpecJson: {"source":{"viewmodel":"<name>"|"viewmodel_id":"<guid>","path":"Prop.Sub"},
//               "destination":{"widget":"<name>"|"self":true,"path":"Prop.Sub"},
//               "mode":"OneWayToDestination"}
// ============================================================================
FString UMCPReflectionLibrary::AddMvvmBindingJson(const FString& WidgetBlueprintPath, const FString& SpecJson)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    TSharedPtr<FJsonObject> Spec;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SpecJson);
        if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
        {
            return MCPMvvm_Error(TEXT("spec_json is not a valid JSON object"));
        }
    }
    const TSharedPtr<FJsonObject>* SourceObj = nullptr;
    const TSharedPtr<FJsonObject>* DestObj = nullptr;
    if (!Spec->TryGetObjectField(TEXT("source"), SourceObj) || !Spec->TryGetObjectField(TEXT("destination"), DestObj))
    {
        return MCPMvvm_Error(TEXT("spec_json requires 'source' and 'destination' objects"));
    }

    // Ensure a view exists so we can resolve the source viewmodel context.
    UMVVMBlueprintView* View = Sub->RequestView(WBP);
    if (!View)
    {
        return MCPMvvm_Error(TEXT("failed to create/find the MVVM view"));
    }

    // ---- Resolve the SOURCE (a viewmodel property) BEFORE mutating anything. ----
    FString VmName, VmIdStr, SrcPathStr;
    (*SourceObj)->TryGetStringField(TEXT("viewmodel"), VmName);
    (*SourceObj)->TryGetStringField(TEXT("viewmodel_id"), VmIdStr);
    if (!(*SourceObj)->TryGetStringField(TEXT("path"), SrcPathStr) || SrcPathStr.IsEmpty())
    {
        return MCPMvvm_Error(TEXT("source.path is required"));
    }
    const FMVVMBlueprintViewModelContext* VmCtx = nullptr;
    if (!VmIdStr.IsEmpty())
    {
        FGuid Gid;
        if (FGuid::Parse(VmIdStr, Gid))
        {
            VmCtx = View->FindViewModel(Gid);
        }
    }
    if (!VmCtx && !VmName.IsEmpty())
    {
        VmCtx = View->FindViewModel(FName(*VmName));
    }
    if (!VmCtx)
    {
        return MCPMvvm_Error(TEXT("source viewmodel not found (give source.viewmodel name or source.viewmodel_id)"));
    }
    UClass* VmClass = VmCtx->GetViewModelClass();
    if (!VmClass)
    {
        return MCPMvvm_Error(TEXT("source viewmodel has no class"));
    }

    FMVVMBlueprintPropertyPath SourcePath;
    SourcePath.SetViewModelId(VmCtx->GetViewModelId());
    {
        FString Err;
        if (!MCPMvvm_AppendDottedPath(SourcePath, WBP, VmClass, SrcPathStr, Err))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("source path error: %s"), *Err));
        }
    }

    // ---- Resolve the DESTINATION (a widget property, or the userwidget itself). ----
    FString WidgetName, DstPathStr;
    bool bSelf = false;
    (*DestObj)->TryGetBoolField(TEXT("self"), bSelf);
    (*DestObj)->TryGetStringField(TEXT("widget"), WidgetName);
    if (!(*DestObj)->TryGetStringField(TEXT("path"), DstPathStr) || DstPathStr.IsEmpty())
    {
        return MCPMvvm_Error(TEXT("destination.path is required"));
    }
    FMVVMBlueprintPropertyPath DestPath;
    UStruct* DestOwner = nullptr;
    if (bSelf || WidgetName.IsEmpty())
    {
        DestPath.SetSelfContext();
        DestOwner = WBP->SkeletonGeneratedClass ? WBP->SkeletonGeneratedClass.Get() : WBP->GeneratedClass.Get();
    }
    else
    {
        UWidget* Widget = WBP->WidgetTree ? WBP->WidgetTree->FindWidget(FName(*WidgetName)) : nullptr;
        if (!Widget)
        {
            return MCPMvvm_Error(FString::Printf(TEXT("destination widget '%s' not found in the tree"), *WidgetName));
        }
        DestPath.SetWidgetName(FName(*WidgetName));
        DestOwner = Widget->GetClass();
    }
    {
        FString Err;
        if (!MCPMvvm_AppendDottedPath(DestPath, WBP, DestOwner, DstPathStr, Err))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("destination path error: %s"), *Err));
        }
    }

    // ---- Binding mode. ----
    EMVVMBindingMode Mode = EMVVMBindingMode::OneWayToDestination;
    FString ModeStr;
    if (Spec->TryGetStringField(TEXT("mode"), ModeStr) && !ModeStr.IsEmpty())
    {
        if (!MCPMvvm_EnumFromString(ModeStr, Mode))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("unknown binding mode '%s'"), *ModeStr));
        }
    }

    // ---- Everything resolved: create + wire the binding via the blessed subsystem. ----
    FMVVMBlueprintViewBinding& NewBinding = Sub->AddBinding(WBP);
    Sub->SetSourcePathForBinding(WBP, NewBinding, SourcePath);
    Sub->SetDestinationPathForBinding(WBP, NewBinding, DestPath, /*bAllowEventConversion*/ false);
    Sub->SetBindingTypeForBinding(WBP, NewBinding, Mode);

    const FGuid BindingId = NewBinding.BindingId;

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("binding_id"), BindingId.ToString(EGuidFormats::DigitsWithHyphens));
    Root->SetStringField(TEXT("mode"), MCPMvvm_EnumToString(Mode));
    Root->SetStringField(TEXT("source_viewmodel"), VmCtx->GetViewModelName().ToString());
    Root->SetStringField(TEXT("source_path"), SrcPathStr);
    Root->SetStringField(TEXT("destination_widget"), (bSelf || WidgetName.IsEmpty()) ? TEXT("(self)") : WidgetName);
    Root->SetStringField(TEXT("destination_path"), DstPathStr);
    Root->SetNumberField(TEXT("binding_count"), View->GetNumBindings());
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 8) SetMvvmBindingJson  (write) -> set_mvvm_binding_json
//    SpecJson (all optional): mode (enum), execution_mode (enum) | reset_execution_mode(bool),
//    enabled(bool), compile(bool), conversion_function ("/Script/...:Func" path, best-effort).
// ============================================================================
FString UMCPReflectionLibrary::SetMvvmBindingJson(const FString& WidgetBlueprintPath,
                                                  const FString& BindingId, const FString& SpecJson)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    UMVVMBlueprintView* View = Sub->GetView(WBP);
    if (!View)
    {
        return MCPMvvm_Error(TEXT("no MVVM view on this widget blueprint"));
    }
    FGuid Gid;
    if (!FGuid::Parse(BindingId, Gid))
    {
        return MCPMvvm_Error(FString::Printf(TEXT("binding_id is not a valid GUID: %s"), *BindingId));
    }
    FMVVMBlueprintViewBinding* B = View->GetBinding(Gid);
    if (!B)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("binding not found: %s"), *BindingId));
    }
    TSharedPtr<FJsonObject> Spec;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(SpecJson);
        if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
        {
            return MCPMvvm_Error(TEXT("spec_json is not a valid JSON object"));
        }
    }

    TSharedRef<FJsonObject> Prev = MakeShared<FJsonObject>();
    TSharedRef<FJsonObject> New = MakeShared<FJsonObject>();
    bool bAnyChange = false;

    FString ModeStr;
    if (Spec->TryGetStringField(TEXT("mode"), ModeStr))
    {
        EMVVMBindingMode Mode;
        if (!MCPMvvm_EnumFromString(ModeStr, Mode))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("unknown binding mode '%s'"), *ModeStr));
        }
        Prev->SetStringField(TEXT("mode"), MCPMvvm_EnumToString(B->BindingType));
        Sub->SetBindingTypeForBinding(WBP, *B, Mode);
        New->SetStringField(TEXT("mode"), MCPMvvm_EnumToString(Mode));
        bAnyChange = true;
    }

    bool bResetExec = false;
    FString ExecStr;
    if (Spec->TryGetBoolField(TEXT("reset_execution_mode"), bResetExec) && bResetExec)
    {
        Prev->SetBoolField(TEXT("override_execution_mode"), B->bOverrideExecutionMode);
        if (B->bOverrideExecutionMode)
        {
            Prev->SetStringField(TEXT("execution_mode"), MCPMvvm_EnumToString(B->OverrideExecutionMode));
        }
        Sub->ResetExecutionModeForBinding(WBP, *B);
        New->SetBoolField(TEXT("override_execution_mode"), false);
        bAnyChange = true;
    }
    else if (Spec->TryGetStringField(TEXT("execution_mode"), ExecStr))
    {
        EMVVMExecutionMode Exec;
        if (!MCPMvvm_EnumFromString(ExecStr, Exec))
        {
            return MCPMvvm_Error(FString::Printf(TEXT("unknown execution_mode '%s'"), *ExecStr));
        }
        Prev->SetBoolField(TEXT("override_execution_mode"), B->bOverrideExecutionMode);
        Prev->SetStringField(TEXT("execution_mode"),
            B->bOverrideExecutionMode ? MCPMvvm_EnumToString(B->OverrideExecutionMode) : FString());
        Sub->OverrideExecutionModeForBinding(WBP, *B, Exec);
        New->SetStringField(TEXT("execution_mode"), MCPMvvm_EnumToString(Exec));
        bAnyChange = true;
    }

    bool bEnabled = false;
    if (Spec->TryGetBoolField(TEXT("enabled"), bEnabled))
    {
        Prev->SetBoolField(TEXT("enabled"), B->bEnabled);
        Sub->SetEnabledForBinding(WBP, *B, bEnabled);
        New->SetBoolField(TEXT("enabled"), bEnabled);
        bAnyChange = true;
    }
    bool bCompile = false;
    if (Spec->TryGetBoolField(TEXT("compile"), bCompile))
    {
        Prev->SetBoolField(TEXT("compile"), B->bCompile);
        Sub->SetCompileForBinding(WBP, *B, bCompile);
        New->SetBoolField(TEXT("compile"), bCompile);
        bAnyChange = true;
    }

    // Conversion function (best-effort; simple UFunction source->destination only).
    FString ConvPath;
    FString ConvNote;
    if (Spec->TryGetStringField(TEXT("conversion_function"), ConvPath) && !ConvPath.IsEmpty())
    {
        // Accept "Package.Class:Function" (the form GetMvvmConversionFunctionsJson emits) or a bare
        // object path. Prefer resolving the owning class then FindFunctionByName (robust vs ':' parsing).
        UFunction* ConvFunc = nullptr;
        FString LeftClass, RightFunc;
        if (ConvPath.Split(TEXT(":"), &LeftClass, &RightFunc))
        {
            if (UClass* OwnerClass = MCPMvvm_ResolveClass(LeftClass))
            {
                ConvFunc = OwnerClass->FindFunctionByName(FName(*RightFunc));
            }
        }
        if (!ConvFunc)
        {
            ConvFunc = FindObject<UFunction>(nullptr, *ConvPath);
        }
        if (ConvFunc)
        {
            Sub->SetSourceToDestinationConversionFunction(WBP, *B,
                FMVVMBlueprintFunctionReference(WBP, ConvFunc));
            New->SetStringField(TEXT("conversion_function"), ConvFunc->GetPathName());
            bAnyChange = true;
            ConvNote = TEXT("conversion set best-effort (source->destination, simple UFunction); it clears source_path and generates a wrapper graph");
        }
        else
        {
            ConvNote = FString::Printf(TEXT("conversion_function '%s' not found (pass a full 'Package.Class:Function' path); skipped"), *ConvPath);
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetStringField(TEXT("binding_id"), BindingId);
    Root->SetBoolField(TEXT("set"), bAnyChange);
    Root->SetObjectField(TEXT("prev"), Prev);
    Root->SetObjectField(TEXT("new"), New);
    if (!ConvNote.IsEmpty())
    {
        Root->SetStringField(TEXT("conversion_note"), ConvNote);
    }
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 9) RemoveMvvmBindingJson  (write) -> remove_mvvm_binding_json
// ============================================================================
FString UMCPReflectionLibrary::RemoveMvvmBindingJson(const FString& WidgetBlueprintPath, const FString& BindingId)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    UMVVMBlueprintView* View = Sub->GetView(WBP);
    if (!View)
    {
        return MCPMvvm_Error(TEXT("no MVVM view on this widget blueprint"));
    }
    FGuid Gid;
    if (!FGuid::Parse(BindingId, Gid))
    {
        return MCPMvvm_Error(FString::Printf(TEXT("binding_id is not a valid GUID: %s"), *BindingId));
    }
    FMVVMBlueprintViewBinding* B = View->GetBinding(Gid);
    if (!B)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("binding not found: %s"), *BindingId));
    }
    // Capture a descriptor for the (lossy) inverse re-create.
    const UClass* SelfContext = WBP->SkeletonGeneratedClass ? WBP->SkeletonGeneratedClass.Get()
                                                            : WBP->GeneratedClass.Get();
    TSharedRef<FJsonObject> Descriptor = MakeShared<FJsonObject>();
    MCPMvvm_FillBindingJson(*B, SelfContext, Descriptor);

    Sub->RemoveBinding(WBP, *B);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetStringField(TEXT("binding_id"), BindingId);
    Root->SetBoolField(TEXT("removed"), View->GetBinding(Gid) == nullptr);
    Root->SetNumberField(TEXT("binding_count"), View->GetNumBindings());
    Root->SetObjectField(TEXT("descriptor"), Descriptor);
    Root->SetStringField(TEXT("note"),
        TEXT("inverse re-creates a similar binding (NEW guid) from the descriptor; conversion functions are not restored"));
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 10) GetMvvmConversionFunctionsJson  (read) -> get_mvvm_conversion_functions_json
// ============================================================================
FString UMCPReflectionLibrary::GetMvvmConversionFunctionsJson(const FString& WidgetBlueprintPath,
                                                              const FString& NameFilter, int32 MaxResults)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPMvvm_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UMVVMEditorSubsystem* Sub = MCPMvvm_Subsystem();
    if (!Sub)
    {
        return MCPMvvm_Error(TEXT("MVVMEditorSubsystem unavailable"));
    }
    if (MaxResults <= 0)
    {
        MaxResults = 200;
    }
    // null,null => all conversion functions the project allows (filtered by developer settings).
    TArray<UE::MVVM::FConversionFunctionValue> Funcs = Sub->GetConversionFunctions(WBP, nullptr, nullptr);

    TArray<TSharedPtr<FJsonValue>> Arr;
    int32 Matched = 0;
    for (const UE::MVVM::FConversionFunctionValue& F : Funcs)
    {
        if (!F.IsValid())
        {
            continue;
        }
        const FString Name = F.GetName();
        if (!NameFilter.IsEmpty() && !Name.Contains(NameFilter, ESearchCase::IgnoreCase))
        {
            continue;
        }
        ++Matched;
        if (Arr.Num() >= MaxResults)
        {
            continue; // keep counting Matched, but stop emitting.
        }
        TSharedRef<FJsonObject> Fo = MakeShared<FJsonObject>();
        Fo->SetStringField(TEXT("name"), Name);
        Fo->SetStringField(TEXT("display_name"), F.GetDisplayName().ToString());
        Fo->SetStringField(TEXT("category"), F.GetCategory().ToString());
        Fo->SetBoolField(TEXT("is_function"), F.IsFunction());
        Fo->SetBoolField(TEXT("is_node"), F.IsNode());
        if (F.IsFunction() && F.GetFunction())
        {
            Fo->SetStringField(TEXT("function_path"), F.GetFunction()->GetPathName());
        }
        else if (F.IsNode() && F.GetNode().Get())
        {
            Fo->SetStringField(TEXT("node_class"), F.GetNode()->GetPathName());
        }
        Arr.Add(MakeShared<FJsonValueObject>(Fo));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetPathName());
    Root->SetNumberField(TEXT("total_available"), Funcs.Num());
    Root->SetNumberField(TEXT("matched"), Matched);
    Root->SetNumberField(TEXT("returned"), Arr.Num());
    Root->SetStringField(TEXT("name_filter"), NameFilter);
    Root->SetArrayField(TEXT("functions"), Arr);
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// 11) SetVariableFieldNotifyJson  (write) -> set_variable_field_notify_json
//     Pure Kismet — works on ANY UBlueprint (viewmodel BP, widget BP, actor BP).
//     Mirrors Kismet/FieldNotifyToggle.cpp:299-305.
// ============================================================================
FString UMCPReflectionLibrary::SetVariableFieldNotifyJson(const FString& BlueprintPath,
                                                          const FString& VariableName, bool bEnable)
{
#if WITH_EDITOR
    UBlueprint* BP = MCPMvvm_LoadBP(BlueprintPath);
    if (!BP)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("blueprint not found: %s"), *BlueprintPath));
    }
    const FName VarName(*VariableName);
    const int32 VarIndex = FBlueprintEditorUtils::FindNewVariableIndex(BP, VarName);
    if (VarIndex == INDEX_NONE)
    {
        return MCPMvvm_Error(FString::Printf(TEXT("member variable '%s' not found on '%s'"),
            *VariableName, *BP->GetName()));
    }
    const bool bPrev = BP->NewVariables[VarIndex].HasMetaData(FBlueprintMetadata::MD_FieldNotify);

    if (bEnable)
    {
        FBlueprintEditorUtils::SetBlueprintVariableMetaData(BP, VarName, nullptr,
            FBlueprintMetadata::MD_FieldNotify, FString());
    }
    else
    {
        FBlueprintEditorUtils::RemoveFieldNotifyFromAllMetadata(BP, VarName);
        FBlueprintEditorUtils::RemoveBlueprintVariableMetaData(BP, VarName, nullptr,
            FBlueprintMetadata::MD_FieldNotify);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), BP->GetPathName());
    Root->SetStringField(TEXT("variable"), VariableName);
    Root->SetBoolField(TEXT("enabled"), bEnable);
    Root->SetBoolField(TEXT("prev_enabled"), bPrev);
    Root->SetBoolField(TEXT("changed"), bPrev != bEnable);
    return MCPMvvm_Serialize(Root);
#else
    return MCPMvvm_Error(TEXT("editor-only"));
#endif
}
