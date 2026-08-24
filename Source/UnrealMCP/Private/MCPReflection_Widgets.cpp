// ============================================================================
// MCPReflection_Widgets.cpp  —  WIDGETS (UMG) "W-B" batch: widget-tree ops +
//   named slots + property bindings. Twelve DEFERRED MCPReflectionLibrary
//   handlers that widget_edit_cpp.py hasattr-guards on. When these link, the
//   Python tools auto-enable.
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** on purpose
// (mirrors MCPReflection_Niagara2.cpp). Extends the shipping C++ #8 widget-tree
// handlers (AddWidgetToBlueprint / RemoveWidgetFromBlueprint in
// MCPReflectionLibrary.cpp ~L1147-1284) with the ops that stock Python cannot
// reach:
//   * UWidgetTree::RootWidget mutation (public TObjectPtr in 5.8, but
//     set_editor_property refuses it -> only C++ can point the tree root).
//   * UWidget::bIsVariable (a plain UPROPERTY() uint8:1 bitfield, no Edit flags
//     -> set_editor_property refused).
//   * UWidgetBlueprint::Bindings (TArray<FDelegateEditorBinding>, editor-only
//     data, not a python-reachable struct array).
//   * INamedSlotInterface (UINTERFACE meta=CannotImplementInterfaceInBlueprint,
//     a pure C++ interface -> not python-reachable).
//   * FWidgetBlueprintEditorUtils::ReplaceWidgets (asset-level overload; the
//     one that does NOT need a live FWidgetBlueprintEditor tab).
//
// >>> BUILD STORY <<<  NO Build.cs change, NO new module. UMG + UMGEditor are
// already PublicDependencyModuleNames (added for C++ #8, UnrealMCP.Build.cs:42);
// Kismet/KismetWidgets are private deps (:74). Everything referenced here lives
// in UMG (runtime) or UMGEditor (editor) or UnrealEd (FBlueprintEditorUtils).
//
// >>> LINK-RISK DISCIPLINE <<<  Every UMG/UMGEditor symbol used here is *_API in
// the stock 5.8 headers (verified header:line below). The ONE link watch-item is
// the two UMGEDITOR_API statics on the plain (non-UCLASS) class
// FWidgetBlueprintEditorUtils — they are `static UE_API` where
// `#define UE_API UMGEDITOR_API`, so they ARE exported per-function; if the DLL
// fails to resolve them at link, fall back to the manual splice already used by
// WrapWidgetJson (ReplaceWidgets is only used by ReplaceWidgetJson).
//
//   Confirmed 5.8 signatures (header:line verified against the source engine):
//     UBaseWidgetBlueprint::WidgetTree (public UPROPERTY)                       BaseWidgetBlueprint.h (via C++ #8)
//     UWidgetBlueprint::Bindings  TArray<FDelegateEditorBinding>  (WITH_EDITORONLY_DATA, public) WidgetBlueprint.h:226
//     UWidgetBlueprint::OnVariableAdded/Renamed/Removed(FName...)  UMGEDITOR_API WidgetBlueprint.h:257-259
//     FDelegateEditorBinding { FString ObjectName; FName PropertyName/FunctionName/SourceProperty; FEditorPropertyPath SourcePath; FGuid MemberGuid; EBindingKind Kind; } WidgetBlueprint.h:129-174
//     EBindingKind { Function, Property }                                       WidgetBlueprintGeneratedClass.h:22
//     UWidgetTree::RootWidget (public TObjectPtr<UWidget>)                      WidgetTree.h:150
//     UWidgetTree::FindWidget(const FName&) const -> UWidget*     UMG_API        WidgetTree.h:34
//     UWidgetTree::RemoveWidget(UWidget*) -> bool                UMG_API         WidgetTree.h:47
//     UWidgetTree::FindWidgetParent(UWidget*,int32&) [static]    UMG_API         WidgetTree.h:50
//     UWidgetTree::GetChildWidgets(UWidget*,TArray<UWidget*>&) [static] UMG_API  WidgetTree.h:68
//     UWidgetTree::ConstructWidget<T>(TSubclassOf<T>,FName) [template]           WidgetTree.h:105
//     UWidgetTree::GetSlotNames/GetContentForSlot/SetContentForSlot [INamedSlot] UMG_API WidgetTree.h:128-134
//     INamedSlotInterface::GetSlotNames/GetContentForSlot/SetContentForSlot      NamedSlotInterface.h:29-35
//     UWidget::bIsVariable (UPROPERTY() uint8:1, public)                         Widget.h:318
//     UWidget::GetParent() const -> UPanelWidget*                UMG_API         Widget.h:769
//     UWidget::RemoveFromParent()                               UMG_API         Widget.h:776
//     UWidget::SetDisplayLabel(const FString&)                  UMG_API         Widget.h:981
//     UPanelWidget::AddChild/InsertChildAt/ReplaceChildAt/RemoveChild/GetChildIndex/GetChildAt/GetChildrenCount UMG_API PanelWidget.h:28-125
//     FWidgetBlueprintEditorUtils::ReplaceWidgets(UWidgetBlueprint*,TSet<UWidget*>,UClass*,EReplaceWidgetNamingMethod) UMGEDITOR_API WidgetBlueprintEditorUtils.h:73
//     FWidgetBlueprintEditorUtils::EReplaceWidgetNamingMethod { MaintainNameAndReferences, ... } WidgetBlueprintEditorUtils.h:61
//     FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(UBlueprint*)    Kismet2/BlueprintEditorUtils.h
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any
// miss (never crash). Reads are non-mutating. Writes end with
// MarkBlueprintAsStructurallyModified + register/renumber variable GUIDs the same
// way C++ #8 does (OnVariableAdded/Renamed/Removed keep the compiler's
// WidgetVariableNameToGuidMap in sync -> no "Widget was added but did not get a
// GUID" ensure on compile). The Python side finalizes each write with a compile
// + save. Anon-namespace helpers are prefixed MCPWid_ (unique across the unity
// build). VERIFY-tagged calls are version-sensitive; confirm on the live build.
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
#include "UObject/ScriptInterface.h"
#include "Misc/PackageName.h"

// --- UMG runtime (UMG_API — already a dep) ---
#include "Blueprint/WidgetTree.h"                 // UWidgetTree (RootWidget, FindWidget, ConstructWidget, INamedSlot)
#include "Components/Widget.h"                     // UWidget (bIsVariable, GetParent, RemoveFromParent, SetDisplayLabel)
#include "Components/PanelWidget.h"               // UPanelWidget (AddChild / ReplaceChildAt / GetChildIndex ...)
#include "Components/NamedSlotInterface.h"         // INamedSlotInterface

// --- UMGEditor (UMGEDITOR_API — already a dep) ---
#include "WidgetBlueprint.h"                       // UWidgetBlueprint (WidgetTree, Bindings, OnVariable*), FDelegateEditorBinding
#include "Blueprint/WidgetBlueprintGeneratedClass.h" // EBindingKind
#include "WidgetBlueprintEditorUtils.h"            // FWidgetBlueprintEditorUtils::ReplaceWidgets / EReplaceWidgetNamingMethod

// --- Blueprint editor utils (UnrealEd — already a dep) ---
#include "Kismet2/BlueprintEditorUtils.h"          // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPWid_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPWid_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPWid_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UWidgetBlueprint from a package/asset path (Python callers pass the path as a STRING,
    // mirroring niagara_runtime_cpp.py — the C++ side owns the load so the Python is uniform).
    UWidgetBlueprint* MCPWid_LoadWBP(const FString& Path)
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

    // Load a UWidget subclass from a class path ("/Script/UMG.CanvasPanel", "/Game/UI/WBP_Card.WBP_Card_C").
    UClass* MCPWid_LoadWidgetClass(const FString& ClassPath)
    {
        if (ClassPath.IsEmpty())
        {
            return nullptr;
        }
        UClass* Cls = LoadClass<UWidget>(nullptr, *ClassPath);
        if (!Cls)
        {
            // Second chance: a plain object path to a UClass.
            Cls = Cast<UClass>(FSoftObjectPath(ClassPath).TryLoad());
        }
        return (Cls && Cls->IsChildOf(UWidget::StaticClass())) ? Cls : nullptr;
    }

    // A widget's stable path-name within a tree, for capture. Empty for null.
    FString MCPWid_NameOf(const UWidget* W)
    {
        return W ? W->GetName() : FString();
    }
#endif // WITH_EDITOR
}

// ============================================================================
// TREE OPS (5)
// ============================================================================

// (1) Rename a widget in the tree + carry the BP variable with it. Mirrors the core of
//     FWidgetBlueprintOperationUtils::RenameWidget (WidgetBlueprintOperationUtils.cpp:608) minus the
//     live-editor preview path: Modify -> OnVariableRenamed -> SetDisplayLabel + Rename -> fix Bindings +
//     desired-focus + animation bindings -> MarkStructurallyModified.
FString UMCPReflectionLibrary::RenameWidgetJson(const FString& WidgetBlueprintPath, const FString& OldName, const FString& NewName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    if (NewName.IsEmpty())
    {
        return MCPWid_Error(TEXT("new name is empty"));
    }
    UWidget* W = Tree->FindWidget(FName(*OldName));
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *OldName));
    }
    const FName NewFName(*NewName);
    // Name-collision guard (a cheap stand-in for the engine's FKismetNameValidator).
    if (NewFName != W->GetFName() && Tree->FindWidget(NewFName) != nullptr)
    {
        return MCPWid_Error(FString::Printf(TEXT("a widget named '%s' already exists"), *NewName));
    }

    const FName OldFName = W->GetFName();
    const FString OldNameStr = OldFName.ToString();
    const FString NewNameStr = NewFName.ToString();

    WBP->Modify();
    W->Modify();

    // Keep the compiler's WidgetVariableNameToGuidMap in sync (same discipline as C++ #8's OnVariableAdded).
    WBP->OnVariableRenamed(OldFName, NewFName);   // VERIFY: UWidgetBlueprint::OnVariableRenamed(FName,FName)

    // Rename the template UObject inside the tree. (Outer stays the WidgetTree.)
    W->SetDisplayLabel(NewNameStr);                // VERIFY: UWidget::SetDisplayLabel
    W->Rename(*NewNameStr, W->GetOuter());         // VERIFY: UObject::Rename(NewName, NewOuter)

    // Fix up the desired-focus name if it pointed at the old name (asset-level overload).
    FWidgetBlueprintEditorUtils::ReplaceDesiredFocus(WBP, OldFName, NewFName);  // VERIFY: asset-level overload

#if WITH_EDITORONLY_DATA
    // Fix up any property bindings that referenced the old widget name.
    for (FDelegateEditorBinding& Binding : WBP->Bindings)
    {
        if (Binding.ObjectName == OldNameStr)
        {
            Binding.ObjectName = NewNameStr;
        }
    }
#endif

    // NOTE (documented limitation): UK2Node_Variable getter pins in the event graph that reference the old
    // widget name are NOT orphan-patched here (the engine's editor path does that via the open preview). A
    // subsequent compile will surface any stale getter. For the coordinator's scratch WBPs (no graph refs)
    // this is a no-op. Rename-back is symmetric regardless.

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("renamed"), true);
    Root->SetStringField(TEXT("old_name"), OldNameStr);
    Root->SetStringField(TEXT("new_name"), NewNameStr);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (2) Point the tree's RootWidget at a named widget already in the tree. RootWidget is python-refused.
FString UMCPReflectionLibrary::SetRootWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }

    const UWidget* PrevRoot = Tree->RootWidget;                 // VERIFY: UWidgetTree::RootWidget (direct C++ access)
    const FString PrevRootName = MCPWid_NameOf(PrevRoot);
    const bool bHadPrevRoot = (PrevRoot != nullptr);

    WBP->Modify();
    Tree->Modify();
    Tree->RootWidget = W;

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("set_root"), true);
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetStringField(TEXT("prev_root"), PrevRootName);
    Root->SetBoolField(TEXT("had_prev_root"), bHadPrevRoot);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (3) Set a widget's bIsVariable flag (a UPROPERTY() bitfield with no Edit flags -> python-refused).
FString UMCPReflectionLibrary::SetWidgetIsVariableJson(const FString& WidgetBlueprintPath, const FString& WidgetName, bool bIsVariable)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }

    const bool bPrev = (W->bIsVariable != 0);                    // VERIFY: UWidget::bIsVariable (public uint8:1)

    WBP->Modify();
    W->Modify();
    W->bIsVariable = bIsVariable ? 1 : 0;

    // Keep the variable-GUID map in sync: turning it into a variable ADDS a variable entry; turning it off
    // REMOVES it. (Mirror C++ #8's OnVariableAdded discipline — otherwise the compiler ensures on the map.)
    if (bIsVariable && !bPrev)
    {
        WBP->OnVariableAdded(W->GetFName());                    // VERIFY: UWidgetBlueprint::OnVariableAdded
    }
    else if (!bIsVariable && bPrev)
    {
        WBP->OnVariableRemoved(W->GetFName());                  // VERIFY: UWidgetBlueprint::OnVariableRemoved
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetBoolField(TEXT("is_variable"), bIsVariable);
    Root->SetBoolField(TEXT("prev_is_variable"), bPrev);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (4) Replace a widget with a new class, preserving name + references + compatible props. Uses the
//     ASSET-LEVEL FWidgetBlueprintEditorUtils::ReplaceWidgets (no live editor tab required).
FString UMCPReflectionLibrary::ReplaceWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& NewClass)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    UClass* NewCls = MCPWid_LoadWidgetClass(NewClass);
    if (!NewCls)
    {
        return MCPWid_Error(FString::Printf(TEXT("could not load a UWidget subclass '%s'"), *NewClass));
    }
    if (NewCls->HasAnyClassFlags(CLASS_Abstract))
    {
        return MCPWid_Error(FString::Printf(TEXT("class '%s' is abstract"), *NewClass));
    }

    UClass* OldCls = W->GetClass();
    const FString OldClassPath = OldCls->GetPathName();

    // CAVEAT (documented): ReplaceWidgets pops a confirmation MODAL (ShouldContinueReplaceOperation) IF the
    // widget is referenced as a variable in the event graph. On a scratch WBP with no graph refs there is no
    // modal. The coordinator should verify against a graph-unreferenced widget (per the "check for a modal
    // before assuming a hang" memory).
    TSet<UWidget*> ToReplace;
    ToReplace.Add(W);

    WBP->Modify();
    // MaintainNameAndReferences: keep the same name (so inverse can re-target it) and repoint graph refs.
    FWidgetBlueprintEditorUtils::ReplaceWidgets(WBP, ToReplace, NewCls,
        FWidgetBlueprintEditorUtils::EReplaceWidgetNamingMethod::MaintainNameAndReferences);  // VERIFY: UMGEDITOR_API asset-level overload

    // Re-find by name (ReplaceWidgets constructs a fresh widget object under the same name).
    UWidget* NewW = Tree->FindWidget(FName(*WidgetName));
    const FString NewName = NewW ? NewW->GetName() : WidgetName;

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("replaced"), NewW != nullptr);
    Root->SetStringField(TEXT("widget"), NewName);
    Root->SetStringField(TEXT("old_class"), OldClassPath);
    Root->SetStringField(TEXT("new_class"), NewCls->GetPathName());
    Root->SetStringField(TEXT("note"), TEXT("only class-compatible properties transfer; non-transferable props are lost (lossy inverse)"));
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (5) Wrap a widget: construct a panel, splice it into the target's parent slot (or make it the new root),
//     then reparent the target under the panel. Inverse (documented for the fold): remove the panel and
//     reparent the child back.
FString UMCPReflectionLibrary::WrapWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PanelClass)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* Target = Tree->FindWidget(FName(*WidgetName));
    if (!Target)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    UClass* PanelCls = MCPWid_LoadWidgetClass(PanelClass);
    if (!PanelCls || !PanelCls->IsChildOf(UPanelWidget::StaticClass()))
    {
        return MCPWid_Error(FString::Printf(TEXT("'%s' is not a UPanelWidget subclass"), *PanelClass));
    }
    if (PanelCls->HasAnyClassFlags(CLASS_Abstract))
    {
        return MCPWid_Error(FString::Printf(TEXT("panel class '%s' is abstract"), *PanelClass));
    }

    UPanelWidget* OldParent = Target->GetParent();               // VERIFY: UWidget::GetParent
    const bool bWasRoot = (Tree->RootWidget == Target);
    const int32 ChildIndex = OldParent ? OldParent->GetChildIndex(Target) : INDEX_NONE;

    // A wrap of a widget that is neither the root nor slotted in a panel (e.g. it sits in a named slot) is
    // refused here — named-slot hosting is handled by the named-slot ops, not by panel splicing.
    if (!bWasRoot && !OldParent)
    {
        return MCPWid_Error(TEXT("widget is neither the root nor a child of a panel (cannot wrap in place)"));
    }

    WBP->Modify();
    Tree->Modify();

    UPanelWidget* NewPanel = Tree->ConstructWidget<UPanelWidget>(PanelCls);  // VERIFY: template ConstructWidget
    if (!NewPanel)
    {
        return MCPWid_Error(TEXT("ConstructWidget<UPanelWidget> returned null"));
    }

    if (bWasRoot)
    {
        // Panel becomes the root; the old root becomes the panel's only child.
        Tree->RootWidget = NewPanel;
        NewPanel->AddChild(Target);                              // VERIFY: UPanelWidget::AddChild
    }
    else
    {
        // Splice: put the panel where the target was, then reparent the target under it.
        OldParent->Modify();
        // ReplaceChildAt swaps the target's slot for the panel (detaches target's slot). Then AddChild
        // re-parents the target under the panel.
        const bool bReplaced = OldParent->ReplaceChildAt(ChildIndex, NewPanel);  // VERIFY: UPanelWidget::ReplaceChildAt
        if (!bReplaced)
        {
            // Fallback: remove + insert at the captured index.
            OldParent->RemoveChild(Target);
            OldParent->InsertChildAt(ChildIndex == INDEX_NONE ? OldParent->GetChildrenCount() : ChildIndex, NewPanel);
        }
        NewPanel->AddChild(Target);
    }

    // Register the new panel's variable GUID (same discipline as C++ #8's AddWidgetToBlueprint).
    WBP->OnVariableAdded(NewPanel->GetFName());                  // VERIFY: UWidgetBlueprint::OnVariableAdded

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("wrapped"), true);
    Root->SetStringField(TEXT("target"), Target->GetName());
    Root->SetStringField(TEXT("panel_name"), NewPanel->GetName());
    Root->SetStringField(TEXT("panel_class"), PanelCls->GetPathName());
    Root->SetBoolField(TEXT("was_root"), bWasRoot);
    Root->SetStringField(TEXT("parent"), MCPWid_NameOf(OldParent));
    Root->SetNumberField(TEXT("child_index"), ChildIndex);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// NAMED SLOTS (4) — INamedSlotInterface
// ============================================================================

namespace
{
#if WITH_EDITOR
    // Resolve a widget in a tree and its INamedSlotInterface (host). Returns nullptr host if the widget does
    // not implement the interface.
    UWidget* MCPWid_FindHost(UWidgetTree* Tree, const FString& WidgetName, INamedSlotInterface*& OutHost)
    {
        OutHost = nullptr;
        if (!Tree)
        {
            return nullptr;
        }
        UWidget* W = Tree->FindWidget(FName(*WidgetName));
        if (W)
        {
            OutHost = Cast<INamedSlotInterface>(W);   // VERIFY: native-interface Cast on a UWidget*
        }
        return W;
    }
#endif
}

// (6) List a widget's named slots. READ.
FString UMCPReflectionLibrary::ListNamedSlotsJson(const FString& WidgetBlueprintPath, const FString& WidgetName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    INamedSlotInterface* Host = nullptr;
    UWidget* W = MCPWid_FindHost(WBP->WidgetTree, WidgetName, Host);
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    if (!Host)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget '%s' (%s) does not implement INamedSlotInterface"),
            *WidgetName, *W->GetClass()->GetName()));
    }

    TArray<FName> SlotNames;
    Host->GetSlotNames(SlotNames);                              // VERIFY: INamedSlotInterface::GetSlotNames

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FName& S : SlotNames)
    {
        Arr.Add(MakeShared<FJsonValueString>(S.ToString()));
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetStringField(TEXT("widget_class"), W->GetClass()->GetName());
    Root->SetNumberField(TEXT("slot_count"), SlotNames.Num());
    Root->SetArrayField(TEXT("slot_names"), Arr);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (7) Get the content widget in a named slot. READ.
FString UMCPReflectionLibrary::GetNamedSlotContentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& SlotName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    INamedSlotInterface* Host = nullptr;
    UWidget* W = MCPWid_FindHost(WBP->WidgetTree, WidgetName, Host);
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    if (!Host)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget '%s' does not implement INamedSlotInterface"), *WidgetName));
    }

    UWidget* Content = Host->GetContentForSlot(FName(*SlotName));  // VERIFY: INamedSlotInterface::GetContentForSlot

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetStringField(TEXT("slot"), SlotName);
    Root->SetBoolField(TEXT("has_content"), Content != nullptr);
    if (Content)
    {
        Root->SetStringField(TEXT("content"), Content->GetName());
        Root->SetStringField(TEXT("content_class"), Content->GetClass()->GetName());
    }
    else
    {
        Root->SetField(TEXT("content"), MakeShared<FJsonValueNull>());
    }
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (8) Put an existing tree widget into a named slot of a host. Captures prior content for the inverse.
FString UMCPReflectionLibrary::SetNamedSlotContentJson(const FString& WidgetBlueprintPath, const FString& HostWidgetName, const FString& SlotName, const FString& ContentWidgetName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    INamedSlotInterface* Host = nullptr;
    UWidget* HostW = MCPWid_FindHost(Tree, HostWidgetName, Host);
    if (!HostW)
    {
        return MCPWid_Error(FString::Printf(TEXT("host widget not found: %s"), *HostWidgetName));
    }
    if (!Host)
    {
        return MCPWid_Error(FString::Printf(TEXT("host widget '%s' does not implement INamedSlotInterface"), *HostWidgetName));
    }
    // Validate the slot exists.
    {
        TArray<FName> SlotNames;
        Host->GetSlotNames(SlotNames);
        if (!SlotNames.Contains(FName(*SlotName)))
        {
            return MCPWid_Error(FString::Printf(TEXT("host '%s' has no named slot '%s'"), *HostWidgetName, *SlotName));
        }
    }
    UWidget* Content = Tree->FindWidget(FName(*ContentWidgetName));
    if (!Content)
    {
        return MCPWid_Error(FString::Printf(TEXT("content widget not found: %s"), *ContentWidgetName));
    }

    UWidget* PrevContent = Host->GetContentForSlot(FName(*SlotName));
    const FString PrevContentName = MCPWid_NameOf(PrevContent);
    const bool bHadPrev = (PrevContent != nullptr);

    WBP->Modify();
    HostW->Modify();
    Content->Modify();

    // Detach the content from any current parent (panel slot or another named slot) before slotting it.
    if (UPanelWidget* CurParent = Content->GetParent())
    {
        CurParent->Modify();
        CurParent->RemoveChild(Content);                       // VERIFY: UPanelWidget::RemoveChild
    }
    else
    {
        // Might be sitting in another named slot; RemoveFromParent handles both cleanly.
        Content->RemoveFromParent();                           // VERIFY: UWidget::RemoveFromParent
    }

    Host->SetContentForSlot(FName(*SlotName), Content);        // VERIFY: INamedSlotInterface::SetContentForSlot

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("host"), HostW->GetName());
    Root->SetStringField(TEXT("slot"), SlotName);
    Root->SetStringField(TEXT("content"), Content->GetName());
    Root->SetStringField(TEXT("prev_content"), PrevContentName);
    Root->SetBoolField(TEXT("had_prev_content"), bHadPrev);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (9) Clear a named slot (set content to null). Captures prior content for the inverse.
FString UMCPReflectionLibrary::ClearNamedSlotJson(const FString& WidgetBlueprintPath, const FString& HostWidgetName, const FString& SlotName)
{
#if WITH_EDITOR
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    INamedSlotInterface* Host = nullptr;
    UWidget* HostW = MCPWid_FindHost(WBP->WidgetTree, HostWidgetName, Host);
    if (!HostW)
    {
        return MCPWid_Error(FString::Printf(TEXT("host widget not found: %s"), *HostWidgetName));
    }
    if (!Host)
    {
        return MCPWid_Error(FString::Printf(TEXT("host widget '%s' does not implement INamedSlotInterface"), *HostWidgetName));
    }

    UWidget* PrevContent = Host->GetContentForSlot(FName(*SlotName));
    const FString PrevContentName = MCPWid_NameOf(PrevContent);
    const bool bHadPrev = (PrevContent != nullptr);

    WBP->Modify();
    HostW->Modify();
    if (PrevContent)
    {
        PrevContent->Modify();
    }
    Host->SetContentForSlot(FName(*SlotName), nullptr);        // VERIFY: SetContentForSlot(name, nullptr)

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("host"), HostW->GetName());
    Root->SetStringField(TEXT("slot"), SlotName);
    Root->SetBoolField(TEXT("cleared"), true);
    Root->SetStringField(TEXT("prev_content"), PrevContentName);
    Root->SetBoolField(TEXT("had_prev_content"), bHadPrev);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// PROPERTY BINDINGS (3) — WidgetBlueprint->Bindings (TArray<FDelegateEditorBinding>)
// ============================================================================

// (10) Add (or overwrite) a property binding: bind WidgetName.PropertyName -> the BP function FunctionName.
FString UMCPReflectionLibrary::AddPropertyBindingJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PropertyName, const FString& FunctionName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWid_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }
    if (PropertyName.IsEmpty() || FunctionName.IsEmpty())
    {
        return MCPWid_Error(TEXT("property_name and function_name are required"));
    }

    const FName PropFName(*PropertyName);
    // Bindings are keyed (by operator==) on (ObjectName, PropertyName). Capture any existing one for a
    // faithful inverse, then replace it.
    bool bHadExisting = false;
    FString PrevFunction;
    for (const FDelegateEditorBinding& B : WBP->Bindings)
    {
        if (B.ObjectName == WidgetName && B.PropertyName == PropFName)
        {
            bHadExisting = true;
            PrevFunction = B.FunctionName.ToString();
            break;
        }
    }

    WBP->Modify();

    FDelegateEditorBinding NewBinding;
    NewBinding.ObjectName   = WidgetName;
    NewBinding.PropertyName = PropFName;
    NewBinding.FunctionName = FName(*FunctionName);
    NewBinding.Kind         = EBindingKind::Function;          // property bound to a generated getter function

    // Remove any existing binding for this (object, property) first (operator== matches on those two).
    WBP->Bindings.RemoveAll([&](const FDelegateEditorBinding& B)
    {
        return B.ObjectName == WidgetName && B.PropertyName == PropFName;
    });
    const int32 Index = WBP->Bindings.Add(NewBinding);

    // Keep the perf counter in step with the shipping compiler's expectation.
    WBP->PropertyBindings = WBP->Bindings.Num();

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("function"), FunctionName);
    Root->SetNumberField(TEXT("binding_index"), Index);
    Root->SetBoolField(TEXT("had_existing"), bHadExisting);
    Root->SetStringField(TEXT("prev_function"), PrevFunction);
    Root->SetNumberField(TEXT("binding_count"), WBP->Bindings.Num());
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (11) Remove the property binding for (WidgetName, PropertyName). Captures it for re-add.
FString UMCPReflectionLibrary::RemovePropertyBindingJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PropertyName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    if (PropertyName.IsEmpty())
    {
        return MCPWid_Error(TEXT("property_name is required"));
    }
    const FName PropFName(*PropertyName);

    FString PrevFunction;
    int32 PrevKind = -1;
    bool bFound = false;
    for (const FDelegateEditorBinding& B : WBP->Bindings)
    {
        if (B.ObjectName == WidgetName && B.PropertyName == PropFName)
        {
            bFound = true;
            PrevFunction = B.FunctionName.ToString();
            PrevKind = static_cast<int32>(B.Kind);
            break;
        }
    }

    int32 Removed = 0;
    if (bFound)
    {
        WBP->Modify();
        Removed = WBP->Bindings.RemoveAll([&](const FDelegateEditorBinding& B)
        {
            return B.ObjectName == WidgetName && B.PropertyName == PropFName;
        });
        WBP->PropertyBindings = WBP->Bindings.Num();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), Removed > 0);
    Root->SetBoolField(TEXT("found"), bFound);
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("prev_function"), PrevFunction);
    Root->SetNumberField(TEXT("prev_kind"), PrevKind);
    Root->SetNumberField(TEXT("binding_count"), WBP->Bindings.Num());
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}

// (12) List every property binding on the blueprint. READ.
FString UMCPReflectionLibrary::ListPropertyBindingsJson(const FString& WidgetBlueprintPath)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWid_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWid_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FDelegateEditorBinding& B : WBP->Bindings)
    {
        TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
        E->SetStringField(TEXT("object_name"), B.ObjectName);
        E->SetStringField(TEXT("property_name"), B.PropertyName.ToString());
        E->SetStringField(TEXT("function_name"), B.FunctionName.ToString());
        E->SetStringField(TEXT("source_property"), B.SourceProperty.ToString());
        E->SetStringField(TEXT("kind"), B.Kind == EBindingKind::Function ? TEXT("function") : TEXT("property"));
        E->SetStringField(TEXT("member_guid"), B.MemberGuid.ToString());
        Arr.Add(MakeShared<FJsonValueObject>(E));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetNumberField(TEXT("binding_count"), Arr.Num());
    Root->SetArrayField(TEXT("bindings"), Arr);
    return MCPWid_Serialize(Root);
#else
    return MCPWid_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWid_Error(TEXT("editor-only"));
#endif
}
