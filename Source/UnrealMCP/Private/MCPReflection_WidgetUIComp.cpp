// ============================================================================
// MCPReflection_WidgetUIComp.cpp  —  WIDGET UI-COMPONENTS "W-E" batch (UE 5.8
//   BETA feature). Three DEFERRED MCPReflectionLibrary handlers that
//   widget_uicomp_cpp.py hasattr-guards on. When these link, the Python tools
//   auto-enable.
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** on purpose
// (mirrors MCPReflection_Widgets.cpp — the W-B batch). Authors UUIComponents
// (UE 5.8's per-widget "UI Component" extension: a UUIComponent subclass
// attached to a UMG widget, e.g. UNavigationUIComponent) onto a WidgetBlueprint
// at the ASSET LEVEL, so the attachment persists through a headless compile with
// NO live FWidgetBlueprintEditor tab.
//
// >>> BUILD STORY <<<  NO Build.cs change, NO new module. Everything referenced
// lives in UMG (runtime, UMG_API) or UMGEditor (editor, UMGEDITOR_API), both of
// which are already PublicDependencyModuleNames (added for C++ #8,
// UnrealMCP.Build.cs:42). Confirmed:
//   * UUIComponent / UUIComponentContainer / FUIComponentTarget  -> UMG runtime  (Extensions/UIComponent.h, Extensions/UIComponentContainer.h)
//   * FUIComponentUtils / UUIComponentWidgetBlueprintExtension   -> UMGEditor     (UIComponentUtils.h, UIComponentWidgetBlueprintExtension.h)
//   * UWidgetBlueprintExtension::GetExtension<>                   -> UMGEditor     (WidgetBlueprintExtension.h)
//   * FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified  -> UnrealEd      (Kismet2/BlueprintEditorUtils.h, already used by C++ #8/#37)
//
// >>> THE AUTHORING FLOW (get-or-create, cited from engine source) <<<
// The source-of-truth for authored UI components is the editor-only extension
// UUIComponentWidgetBlueprintExtension hung off the UWidgetBlueprint. It owns a
// default-subobject UUIComponentContainer (an RF_ArchetypeObject). At compile
// time UUIComponentWidgetBlueprintExtension::HandleFinishCompilingClass
// (UIComponentWidgetBlueprintExtension.cpp:292) duplicates that container into a
// UUIComponentWidgetBlueprintGeneratedClassExtension on the generated class, so
// the attachment ships at runtime — i.e. the persist-through-compile path is
// fully reachable headless.
//
// Rather than hand-roll get-or-create + NewObject<UUIComponent>(...) + container
// mutation (fragile against beta churn), these handlers call the ONE public
// UMGEDITOR_API entry point the UMG Designer itself uses:
//   FUIComponentUtils::AddComponent(FWidgetBlueprintEditor*, UWidgetBlueprint*, const UClass*, FName, FText&)   (UIComponentUtils.cpp:64)
//   FUIComponentUtils::RemoveComponent(FWidgetBlueprintEditor*, UWidgetBlueprint*, const UClass*, FName, FText&)(UIComponentUtils.cpp:99)
// The KEY realization: the FWidgetBlueprintEditor* first arg is NULL-GUARDED
// inside AddComponent (UIComponentUtils.cpp:85 — `PreviewWidget = BlueprintEditor
// ? BlueprintEditor->GetPreview() : nullptr`) and RemoveComponent (line 126). So
// passing nullptr does the FULL persistent authoring work (RequestExtension ->
// Extension->AddComponent -> ComponentContainer->AddComponent -> MarkStructurally
// Modified, wrapped in an FScopedTransaction) and merely SKIPS the transient
// live-design-preview mirror (UUIComponentUserWidgetExtension), which is not
// persisted anyway. AddComponent internally does
//   NewObject<UUIComponent>(ComponentContainer, ComponentClass, NAME_None, RF_ArchetypeObject)
//   ComponentContainer->AddComponent(OwnerName, NewComponent)   (UIComponentWidgetBlueprintExtension.cpp:230-233)
// which is exactly the "Container->AddComponent(WidgetName, NewObject<UUIComponent>(...))"
// the spec calls for — reused instead of duplicated.
//
// >>> BETA-AUTHORING UNCERTAINTY (honest flags for the coordinator) <<<
//   [FLAG-1] Headless FScopedTransaction: FUIComponentUtils::Add/RemoveComponent
//     each open an FScopedTransaction. With a live interactive editor (the
//     coordinator's standing setup) this records a proper undo transaction. In a
//     pure commandlet with no GEditor the transaction is a null-safe no-op — the
//     mutation still lands, it just is not on the editor undo stack (we rely on
//     the Python ledger inverse for undo regardless).
//   [FLAG-2] RequestExtension timing: UWidgetBlueprintExtension::RequestExtension
//     is documented "illegal once compilation has commenced". These handlers run
//     OUTSIDE compilation (the Python compiles AFTER the C++ returns), so this is
//     satisfied. If ever called mid-compile it would assert — not a path we hit.
//   [FLAG-3] LIST orphan caveat: ListUIComponentsJson enumerates via the public
//     Extension->GetComponentsFor(Widget) per tree widget (see below), so a
//     component whose target widget was DELETED (an orphan the container still
//     holds until the next compile's CleanupUIComponents) will NOT be listed.
//     Acceptable: orphans are transient and self-heal on compile.
//   [FLAG-4] Exact-class removal: UUIComponentContainer::RemoveAllComponentsOfType
//     matches GetClass()==ComponentClass EXACTLY (UIComponentContainer.cpp:94),
//     whereas GetComponent matches IsChildOf. To guarantee removal (and a faithful
//     re-add) we capture the CONCRETE class off the resolved component and pass
//     THAT to RemoveComponent — never the (possibly base) requested class.
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any
// miss (never crash). ListUIComponentsJson is non-mutating (no ledger). Each
// WRITE ends with FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP)
// (idempotent — the FUIComponentUtils path already marks it; we re-mark for
// uniformity with the rest of the plugin). The Python side finalizes each write
// with a compile + save (widget_uicomp_cpp.py::_compile_save). Anon-namespace
// helpers are prefixed MCPWuc_ (unique across the unity build). VERIFY-tagged
// calls are version-sensitive; confirm on the live 5.8 build.
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Reflection / core ---
#include "UObject/Class.h"
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"

// --- UMG runtime (UMG_API — already a dep) ---
#include "Blueprint/WidgetTree.h"                          // UWidgetTree (FindWidget, ForEachWidget)
#include "Components/Widget.h"                              // UWidget
#include "Extensions/UIComponent.h"                         // UUIComponent (base, Abstract)
#include "Extensions/UIComponentContainer.h"               // UUIComponentContainer::GetPropertyNameForComponent (static)

// --- UMGEditor (UMGEDITOR_API — already a dep) ---
#include "WidgetBlueprint.h"                                // UWidgetBlueprint (WidgetTree)
#include "WidgetBlueprintExtension.h"                       // UWidgetBlueprintExtension::GetExtension<>
#include "UIComponentWidgetBlueprintExtension.h"            // UUIComponentWidgetBlueprintExtension (GetComponent / GetComponentsFor)
#include "UIComponentUtils.h"                               // FUIComponentUtils::AddComponent / RemoveComponent

// --- Blueprint editor utils (UnrealEd — already a dep) ---
#include "Kismet2/BlueprintEditorUtils.h"                  // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPWuc_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPWuc_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPWuc_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UWidgetBlueprint from a package/asset path (Python callers pass the path as a STRING,
    // mirroring widget_edit_cpp.py — the C++ side owns the load so the Python is uniform).
    UWidgetBlueprint* MCPWuc_LoadWBP(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(FSoftObjectPath(Path).TryLoad()))
        {
            return WBP;
        }
        // Tolerate a bare package path ("/Game/UI/WBP_Foo") -> append the object name.
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UWidgetBlueprint>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Load a UUIComponent subclass from a class path ("/Script/UMG.NavigationUIComponent",
    // "/Game/UI/BP_MyComp.BP_MyComp_C"). Returns nullptr unless the loaded UClass is a UUIComponent child.
    UClass* MCPWuc_LoadComponentClass(const FString& ClassPath)
    {
        if (ClassPath.IsEmpty())
        {
            return nullptr;
        }
        UClass* Cls = LoadClass<UUIComponent>(nullptr, *ClassPath);
        if (!Cls)
        {
            // Second chance: a plain object path to a UClass.
            Cls = Cast<UClass>(FSoftObjectPath(ClassPath).TryLoad());
        }
        return (Cls && Cls->IsChildOf(UUIComponent::StaticClass())) ? Cls : nullptr;
    }

    // Get the (already-requested) UI-component authoring extension for a WBP, or nullptr if the blueprint has
    // never had a component added (the extension is created lazily by FUIComponentUtils::AddComponent).
    UUIComponentWidgetBlueprintExtension* MCPWuc_GetExtension(const UWidgetBlueprint* WBP)
    {
        return UWidgetBlueprintExtension::GetExtension<UUIComponentWidgetBlueprintExtension>(WBP);
    }
#endif // WITH_EDITOR
}

// ============================================================================
// (1) ADD — attach a UUIComponent subclass to a named widget in the WBP.
//     WRITE. Ledger inverse (Python) = RemoveUIComponentJson(same widget, class).
// ============================================================================
FString UMCPReflectionLibrary::AddUIComponentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& ComponentClass)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWuc_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWuc_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWuc_Error(TEXT("blueprint has no WidgetTree"));
    }
    // Validate the target widget EXISTS before authoring — FUIComponentUtils::AddComponent does NOT check this
    // (it would happily create a component whose TargetName resolves to nothing -> an orphan pruned on compile).
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWuc_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    UClass* CompCls = MCPWuc_LoadComponentClass(ComponentClass);
    if (!CompCls)
    {
        return MCPWuc_Error(FString::Printf(TEXT("could not load a UUIComponent subclass '%s'"), *ComponentClass));
    }
    if (CompCls->HasAnyClassFlags(CLASS_Abstract))
    {
        return MCPWuc_Error(FString::Printf(TEXT("component class '%s' is abstract"), *ComponentClass));
    }

    const FName OwnerName = W->GetFName();

    // The one public authoring entry point (nullptr editor => persistent asset-level path, preview mirror
    // skipped). Requests/creates the UUIComponentWidgetBlueprintExtension, NewObject's the UUIComponent
    // archetype into its ComponentContainer, and MarkBlueprintAsStructurallyModified — all under a scoped
    // transaction. Rejects hierarchy conflicts (a base/derived of an already-present component) with OutError set.
    FText OutError;
    FUIComponentUtils::AddComponent(nullptr, WBP, CompCls, OwnerName, OutError);   // VERIFY: UMGEDITOR_API 5-arg overload (UIComponentUtils.cpp:64)

    if (!OutError.IsEmpty())
    {
        return MCPWuc_Error(FString::Printf(TEXT("add UI component failed: %s"), *OutError.ToString()));
    }

    // Confirm the component landed in the persistent extension container (robust success check; also yields the
    // generated component-variable name that the compiler will synthesize: "<ComponentName>_<WidgetName>").
    UUIComponentWidgetBlueprintExtension* Ext = MCPWuc_GetExtension(WBP);
    UUIComponent* Added = Ext ? Ext->GetComponent(CompCls, OwnerName) : nullptr;   // VERIFY: Extension::GetComponent (IsChildOf match)
    if (!Added)
    {
        return MCPWuc_Error(TEXT("add UI component reported no error but the component is not present on the extension container"));
    }
    const FString ComponentVar = UUIComponentContainer::GetPropertyNameForComponent(Added, OwnerName).ToString();

    // Idempotent re-mark for plugin uniformity (FUIComponentUtils::AddComponent already marked it).
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetStringField(TEXT("component_class"), Added->GetClass()->GetPathName());
    Root->SetStringField(TEXT("component_var"), ComponentVar);
    Root->SetStringField(TEXT("note"), TEXT("authored on the editor-only UUIComponentWidgetBlueprintExtension; the Python compile duplicates it into the generated class"));
    return MCPWuc_Serialize(Root);
#else
    return MCPWuc_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (2) REMOVE — detach a UUIComponent (by class) from a named widget.
//     WRITE. Ledger inverse (Python) = AddUIComponentJson(same widget, captured
//     concrete class). LOSSY: any authored property values on the removed
//     archetype are gone; re-add creates a fresh default component.
// ============================================================================
FString UMCPReflectionLibrary::RemoveUIComponentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& ComponentClass)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWuc_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWuc_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UClass* CompCls = MCPWuc_LoadComponentClass(ComponentClass);
    if (!CompCls)
    {
        return MCPWuc_Error(FString::Printf(TEXT("could not load a UUIComponent subclass '%s'"), *ComponentClass));
    }

    UUIComponentWidgetBlueprintExtension* Ext = MCPWuc_GetExtension(WBP);
    if (!Ext)
    {
        return MCPWuc_Error(TEXT("blueprint has no UI-component extension (no components have ever been added)"));
    }

    const FName OwnerName(*WidgetName);
    // Resolve the ACTUAL component (IsChildOf match) so we can (a) confirm it exists and (b) capture the exact
    // concrete class — RemoveAllComponentsOfType matches class EXACTLY, so passing a base class would no-op
    // [FLAG-4]. The concrete path is also what the ledger re-add must use for a faithful inverse.
    UUIComponent* Existing = Ext->GetComponent(CompCls, OwnerName);                // VERIFY: Extension::GetComponent
    if (!Existing)
    {
        return MCPWuc_Error(FString::Printf(TEXT("no UI component matching '%s' on widget '%s'"), *ComponentClass, *WidgetName));
    }
    UClass* ExactCls = Existing->GetClass();
    const FString RemovedClassPath = ExactCls->GetPathName();

    // Detach via the public entry point (nullptr editor => persistent path, preview mirror skipped). Pass the
    // EXACT class so the container's exact-match removal actually fires.
    FText OutError;
    const bool bOk = FUIComponentUtils::RemoveComponent(nullptr, WBP, ExactCls, OwnerName, OutError);   // VERIFY: UMGEDITOR_API 5-arg overload (UIComponentUtils.cpp:99)
    if (!bOk)
    {
        return MCPWuc_Error(FString::Printf(TEXT("remove UI component failed: %s"),
            OutError.IsEmpty() ? TEXT("(no error text)") : *OutError.ToString()));
    }

    // Idempotent re-mark for plugin uniformity (FUIComponentUtils::RemoveComponent already marked it).
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("component_class"), RemovedClassPath);   // exact concrete class -> the ledger re-add uses THIS
    Root->SetStringField(TEXT("note"), TEXT("lossy inverse: authored property values on the removed archetype are not preserved; re-add creates a fresh default component"));
    return MCPWuc_Serialize(Root);
#else
    return MCPWuc_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (3) LIST — enumerate every UI component authored on the WBP. READ (no ledger).
//     Walks the tree and, per widget, reads the public Extension->GetComponentsFor
//     (which iterates the container's ForEachComponentTarget). [FLAG-3] orphans
//     (components whose target widget was deleted) are not listed.
// ============================================================================
FString UMCPReflectionLibrary::ListUIComponentsJson(const FString& WidgetBlueprintPath)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWuc_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWuc_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWuc_Error(TEXT("blueprint has no WidgetTree"));
    }

    UUIComponentWidgetBlueprintExtension* Ext = MCPWuc_GetExtension(WBP);

    TArray<TSharedPtr<FJsonValue>> Arr;
    if (Ext)
    {
        // ForEachWidget covers this tree's own widgets (not foreign UserWidget subtrees) — the same enumeration
        // the engine uses in CleanupUIComponents (UIComponentContainer.cpp:281).
        Tree->ForEachWidget([&Arr, Ext](UWidget* W)      // VERIFY: UWidgetTree::ForEachWidget(TFunctionRef<void(UWidget*)>)
        {
            if (!W)
            {
                return;
            }
            const TArray<UUIComponent*> Comps = Ext->GetComponentsFor(W);   // VERIFY: Extension::GetComponentsFor (public UMGEDITOR_API)
            for (UUIComponent* C : Comps)
            {
                if (!C)
                {
                    continue;
                }
                TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
                E->SetStringField(TEXT("widget_name"), W->GetName());
                E->SetStringField(TEXT("component_class"), C->GetClass()->GetPathName());
                E->SetStringField(TEXT("component_var"), UUIComponentContainer::GetPropertyNameForComponent(C, W->GetFName()).ToString());
                Arr.Add(MakeShared<FJsonValueObject>(E));
            }
        });
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("has_extension"), Ext != nullptr);
    Root->SetNumberField(TEXT("component_count"), Arr.Num());
    Root->SetArrayField(TEXT("components"), Arr);
    Root->SetStringField(TEXT("note"), TEXT("lists components whose target widget still exists in this tree; orphaned components (widget deleted, not yet compiled) are omitted"));
    return MCPWuc_Serialize(Root);
#else
    return MCPWuc_Error(TEXT("editor-only"));
#endif
}
