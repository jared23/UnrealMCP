// UnrealMCP — Blueprint INTERFACE authoring + variable-FLAG reader handlers (C++ #19 2026-08-18).
//
// Member DEFINITIONS for UMCPReflectionLibrary; the matching UFUNCTION declarations are added to
// MCPReflectionLibrary.h by the coordinator (do NOT edit the .h here). Four handlers:
//   1) ImplementBlueprintInterface  — add an interface to a Blueprint (native /Script or BP /Game).
//   2) RemoveBlueprintInterface     — remove an implemented interface.
//   3) ListBlueprintInterfaces      — read Blueprint->ImplementedInterfaces (path + graph names).
//   4) GetBlueprintVariableFlagsJson— read the FBPVariableDescription flags BlueprintEditorLibrary has
//                                     SETTERS for but NO getters (instance_editable / blueprint_read_only /
//                                     expose_on_spawn / private / expose_to_cinematics / config / category /
//                                     replication). Unblocks reversible flag setters.
//
// Everything version-sensitive is checked against UE 5.8 source and tagged "VERIFY vs engine source".
// Anonymous-namespace helpers are prefixed `MCPBpIf_` so they stay unique in the module's unity build.
//
// Module deps: UnrealEd (FBlueprintEditorUtils), BlueprintGraph (FBlueprintMetadata / EdGraphSchema_K2),
// UnrealEd again (FKismetEditorUtilities), Engine (UBlueprint / FBPVariableDescription). ALL already
// present in UnrealMCP.Build.cs -> NO Build.cs change.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"            // UClass / CLASS_Interface
#include "UObject/Interface.h"        // UInterface (interface-ness validation)
#include "UObject/UObjectGlobals.h"   // LoadObject / FindObject
#include "UObject/UnrealType.h"       // property-flag constants (CPF_*)
#include "UObject/ObjectMacros.h"     // EPropertyFlags (CPF_Net / CPF_Interp / CPF_Config / ...)

#include "Engine/Blueprint.h"                 // UBlueprint / FBPVariableDescription / FBPInterfaceDescription
#include "Engine/BlueprintGeneratedClass.h"   // UBlueprintGeneratedClass (BP-interface generated class)
#include "EdGraph/EdGraph.h"                  // UEdGraph (interface graph names)

#if WITH_EDITOR
#include "Kismet2/BlueprintEditorUtils.h"     // FBlueprintEditorUtils::ImplementNewInterface / RemoveInterface
#include "Kismet2/KismetEditorUtilities.h"    // FKismetEditorUtilities::CompileBlueprint
#include "EdGraphSchema_K2.h"                 // FBlueprintMetadata::MD_ExposeOnSpawn / MD_Private (BlueprintGraph)
#endif

namespace
{
    // ---- JSON plumbing (prefixed to stay unique in the unity build) --------------------------------
    FString MCPBpIf_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"status":"error","error":...}
    FString MCPBpIf_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPBpIf_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPBpIf_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

#if WITH_EDITOR
    // Resolve an interface UClass from a path that may be:
    //   * a native interface class path   "/Script/Module.MyInterface"
    //   * a BP-interface generated class  "/Game/Path/BPI_Foo.BPI_Foo_C"
    //   * a BP-interface asset path       "/Game/Path/BPI_Foo.BPI_Foo"  (a UBlueprint -> use GeneratedClass)
    //   * a bare short name               "MyInterface"
    // Returns the interface UClass (CLASS_Interface) or nullptr; loads the package so the subsequent
    // engine call's internal FindObject<UClass> cannot fail its check(). // VERIFY vs engine source
    // (FBlueprintEditorUtils::ImplementNewInterface uses FindObject<UClass> + check(InterfaceClass)).
    UClass* MCPBpIf_ResolveInterfaceClass(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }

        // Direct class load / find.
        UClass* C = LoadObject<UClass>(nullptr, *Path);
        if (!C) { C = FindObject<UClass>(nullptr, *Path); }
        if (!C) { C = UClass::TryFindTypeSlow<UClass>(Path); }

        // Not a class directly — maybe it is a UBlueprint asset (BP interface); use its generated class.
        if (!C)
        {
            if (UObject* Obj = LoadObject<UObject>(nullptr, *Path))
            {
                if (UBlueprint* BP = Cast<UBlueprint>(Obj))
                {
                    C = BP->GeneratedClass; // VERIFY vs engine source (UBlueprint::GeneratedClass)
                }
            }
        }

        // Validate interface-ness. Both native interface UClasses and BP-interface generated classes carry
        // CLASS_Interface; IsChildOf(UInterface) is a belt-and-suspenders fallback. // VERIFY vs engine
        // source (EClassFlags::CLASS_Interface / UInterface::StaticClass).
        if (C && (C->HasAnyClassFlags(CLASS_Interface) || C->IsChildOf(UInterface::StaticClass())) && C != UInterface::StaticClass())
        {
            return C;
        }
        return nullptr;
    }

    // Append the current implemented-interface list to Root (path + graph names) and set implemented_count.
    void MCPBpIf_AppendInterfaceList(UBlueprint* Blueprint, const TSharedRef<FJsonObject>& Root)
    {
        TArray<TSharedPtr<FJsonValue>> Ifaces;
        for (const FBPInterfaceDescription& Desc : Blueprint->ImplementedInterfaces) // VERIFY vs engine source (UBlueprint::ImplementedInterfaces)
        {
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            const UClass* IfaceClass = Desc.Interface; // VERIFY vs engine source (FBPInterfaceDescription::Interface : TSubclassOf<UInterface>)
            if (IfaceClass)
            {
                J->SetStringField(TEXT("path"), IfaceClass->GetClassPathName().ToString());
                J->SetStringField(TEXT("name"), IfaceClass->GetName());
            }
            else
            {
                J->SetStringField(TEXT("path"), TEXT(""));
                J->SetStringField(TEXT("name"), TEXT("<null>"));
            }
            TArray<TSharedPtr<FJsonValue>> Graphs;
            for (const UEdGraph* G : Desc.Graphs) // VERIFY vs engine source (FBPInterfaceDescription::Graphs : TArray<TObjectPtr<UEdGraph>>)
            {
                if (G)
                {
                    Graphs.Add(MakeShared<FJsonValueString>(G->GetName()));
                }
            }
            J->SetArrayField(TEXT("graphs"), Graphs);
            Ifaces.Add(MakeShared<FJsonValueObject>(J));
        }
        Root->SetArrayField(TEXT("interfaces"), Ifaces);
        Root->SetNumberField(TEXT("implemented_count"), Blueprint->ImplementedInterfaces.Num());
    }
#endif // WITH_EDITOR
}

// ============================================================================================
// 1) Implement (add) an interface on a Blueprint.
// ============================================================================================
FString UMCPReflectionLibrary::ImplementBlueprintInterface(UBlueprint* Blueprint, const FString& InterfacePath)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return MCPBpIf_Error(TEXT("null blueprint"));
    }

    UClass* IfaceClass = MCPBpIf_ResolveInterfaceClass(InterfacePath);
    if (!IfaceClass)
    {
        return MCPBpIf_Error(FString::Printf(TEXT("could not resolve interface class '%s' (not found or not an interface)"), *InterfacePath));
    }

    // HARD GUARD: only a real interface class (CLASS_Interface) is safe. Adding a non-interface then compiling
    // asserts inside FKismetCompilerContext::AddInterfacesFromBlueprint (CLASS_Interface check) and takes the
    // whole editor down. Reject up-front. VERIFY vs engine source (UClass class flags).
    if (!IfaceClass->HasAnyClassFlags(CLASS_Interface))
    {
        return MCPBpIf_Error(FString::Printf(TEXT("'%s' is not an interface class (CLASS_Interface not set) — refusing (implementing it would crash the compiler)"), *IfaceClass->GetPathName()));
    }

    // Use the class's canonical path (matches what RemoveInterface compares against later).
    const FTopLevelAssetPath ActualPath = IfaceClass->GetClassPathName(); // VERIFY vs engine source (UClass::GetClassPathName)

    // Reject a duplicate up-front (the engine call would just ShowNotification + return false).
    for (const FBPInterfaceDescription& Desc : Blueprint->ImplementedInterfaces)
    {
        if (Desc.Interface == IfaceClass)
        {
            return MCPBpIf_Error(FString::Printf(TEXT("interface '%s' already implemented"), *ActualPath.ToString()));
        }
    }

    // VERIFY vs engine source (FBlueprintEditorUtils::ImplementNewInterface(UBlueprint*, FTopLevelAssetPath))
    const bool bImplemented = FBlueprintEditorUtils::ImplementNewInterface(Blueprint, ActualPath);
    if (!bImplemented)
    {
        return MCPBpIf_Error(FString::Printf(TEXT("ImplementNewInterface failed for '%s' (function/graph name conflict or non-anim function on non-anim BP)"), *ActualPath.ToString()));
    }

    // ImplementNewInterface marks the BP structurally modified; a full compile realizes the class change.
    FKismetEditorUtilities::CompileBlueprint(Blueprint); // VERIFY vs engine source (FKismetEditorUtilities::CompileBlueprint)

    TSharedRef<FJsonObject> Root = MCPBpIf_Ok();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetStringField(TEXT("interface"), ActualPath.ToString());
    Root->SetBoolField(TEXT("implemented"), true);
    MCPBpIf_AppendInterfaceList(Blueprint, Root);
    return MCPBpIf_SerializeJson(Root);
#else
    return MCPBpIf_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 2) Remove an implemented interface from a Blueprint.
// ============================================================================================
FString UMCPReflectionLibrary::RemoveBlueprintInterface(UBlueprint* Blueprint, const FString& InterfacePath)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return MCPBpIf_Error(TEXT("null blueprint"));
    }

    UClass* IfaceClass = MCPBpIf_ResolveInterfaceClass(InterfacePath);

    // Locate the implemented entry. Prefer resolved-class match; fall back to a path-string compare so a
    // stale/unloadable interface can still be removed by the path the user passed.
    FTopLevelAssetPath ActualPath;
    bool bFound = false;
    for (const FBPInterfaceDescription& Desc : Blueprint->ImplementedInterfaces)
    {
        if (!Desc.Interface) { continue; }
        const FTopLevelAssetPath DescPath = Desc.Interface->GetClassPathName();
        if ((IfaceClass && Desc.Interface == IfaceClass) ||
            DescPath.ToString() == InterfacePath ||
            Desc.Interface->GetName() == InterfacePath)
        {
            ActualPath = DescPath;
            bFound = true;
            break;
        }
    }

    if (!bFound)
    {
        return MCPBpIf_Error(FString::Printf(TEXT("interface '%s' is not implemented on this blueprint"), *InterfacePath));
    }

    // VERIFY vs engine source (FBlueprintEditorUtils::RemoveInterface(UBlueprint*, FTopLevelAssetPath, bool bPreserveFunctions=false))
    // — returns void; it Modify()s the BP + removes graphs/events. bPreserveFunctions=false (drop the graphs).
    FBlueprintEditorUtils::RemoveInterface(Blueprint, ActualPath, /*bPreserveFunctions=*/false);

    FKismetEditorUtilities::CompileBlueprint(Blueprint); // VERIFY vs engine source (FKismetEditorUtilities::CompileBlueprint)

    TSharedRef<FJsonObject> Root = MCPBpIf_Ok();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetStringField(TEXT("interface"), ActualPath.ToString());
    Root->SetBoolField(TEXT("removed"), true);
    MCPBpIf_AppendInterfaceList(Blueprint, Root);
    return MCPBpIf_SerializeJson(Root);
#else
    return MCPBpIf_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 3) List a Blueprint's implemented interfaces (read-only).
// ============================================================================================
FString UMCPReflectionLibrary::ListBlueprintInterfaces(UBlueprint* Blueprint)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return MCPBpIf_Error(TEXT("null blueprint"));
    }

    TSharedRef<FJsonObject> Root = MCPBpIf_Ok();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    MCPBpIf_AppendInterfaceList(Blueprint, Root);
    // Mirror the documented shape: also expose "count" alongside implemented_count.
    Root->SetNumberField(TEXT("count"), Blueprint->ImplementedInterfaces.Num());
    return MCPBpIf_SerializeJson(Root);
#else
    return MCPBpIf_Error(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 4) Read a Blueprint member variable's boolean flags (the setters with no getters).
// ============================================================================================
FString UMCPReflectionLibrary::GetBlueprintVariableFlagsJson(UBlueprint* Blueprint, const FString& VarName)
{
#if WITH_EDITOR
    if (!Blueprint)
    {
        return MCPBpIf_Error(TEXT("null blueprint"));
    }

    const FName VarFName(*VarName);
    const FBPVariableDescription* Var = nullptr;
    for (const FBPVariableDescription& D : Blueprint->NewVariables) // VERIFY vs engine source (UBlueprint::NewVariables : TArray<FBPVariableDescription>)
    {
        if (D.VarName == VarFName) // VERIFY vs engine source (FBPVariableDescription::VarName : FName)
        {
            Var = &D;
            break;
        }
    }
    if (!Var)
    {
        return MCPBpIf_Error(FString::Printf(TEXT("no variable named '%s' on blueprint '%s'"), *VarName, *Blueprint->GetName()));
    }

    const uint64 Flags = Var->PropertyFlags; // VERIFY vs engine source (FBPVariableDescription::PropertyFlags : uint64)

    // instance_editable = NOT CPF_DisableEditOnInstance  (SetBlueprintOnlyEditableFlag sets the flag when
    // bBlueprintOnly == !bInstanceEditable). // VERIFY vs engine source (FBlueprintEditorUtils::SetBlueprintOnlyEditableFlag)
    const bool bInstanceEditable = (Flags & CPF_DisableEditOnInstance) == 0;
    // blueprint_read_only = CPF_BlueprintReadOnly. // VERIFY vs engine source (SetBlueprintPropertyReadOnlyFlag)
    const bool bBlueprintReadOnly = (Flags & CPF_BlueprintReadOnly) != 0;
    // expose_to_cinematics = CPF_Interp. // VERIFY vs engine source (FBlueprintEditorUtils::SetInterpFlag)
    const bool bExposeToCinematics = (Flags & CPF_Interp) != 0;
    // config_variable = CPF_Config.
    const bool bConfig = (Flags & CPF_Config) != 0;

    // expose_on_spawn = metadata "ExposeOnSpawn" == "true". // VERIFY vs engine source
    // (SetBlueprintVariableExposeOnSpawn writes FBlueprintMetadata::MD_ExposeOnSpawn="true")
    bool bExposeOnSpawn = false;
    if (Var->HasMetaData(FBlueprintMetadata::MD_ExposeOnSpawn)) // VERIFY vs engine source (FBPVariableDescription::HasMetaData/GetMetaData)
    {
        bExposeOnSpawn = Var->GetMetaData(FBlueprintMetadata::MD_ExposeOnSpawn).ToBool();
    }

    // private = CPF_NativeAccessSpecifierPrivate OR metadata "BlueprintPrivate" (MD_Private) == "true".
    // For BP-authored vars the state lives in metadata; the native flag is included for completeness.
    // VERIFY vs engine source (FBlueprintEditorUtils::IsVariablePrivate uses CPF_NativeAccessSpecifierPrivate || GetBoolMetaData(MD_Private))
    bool bPrivate = (Flags & CPF_NativeAccessSpecifierPrivate) != 0;
    if (!bPrivate && Var->HasMetaData(FBlueprintMetadata::MD_Private))
    {
        bPrivate = Var->GetMetaData(FBlueprintMetadata::MD_Private).ToBool();
    }

    // replication: none | replicated | repnotify. // VERIFY vs engine source (GetBlueprintVariableReplication:
    // !CPF_Net => None; CPF_RepNotify or RepNotifyFunc != None => RepNotify; else Replicated)
    FString Replication = TEXT("none");
    if ((Flags & CPF_Net) != 0)
    {
        const bool bRepNotify = (Flags & CPF_RepNotify) != 0 || Var->RepNotifyFunc != NAME_None; // VERIFY vs engine source (FBPVariableDescription::RepNotifyFunc : FName)
        Replication = bRepNotify ? TEXT("repnotify") : TEXT("replicated");
    }

    TSharedRef<FJsonObject> Root = MCPBpIf_Ok();
    Root->SetStringField(TEXT("blueprint"), Blueprint->GetName());
    Root->SetStringField(TEXT("var"), VarName);
    Root->SetBoolField(TEXT("instance_editable"), bInstanceEditable);
    Root->SetBoolField(TEXT("blueprint_read_only"), bBlueprintReadOnly);
    Root->SetBoolField(TEXT("expose_on_spawn"), bExposeOnSpawn);
    Root->SetBoolField(TEXT("private"), bPrivate);
    Root->SetBoolField(TEXT("expose_to_cinematics"), bExposeToCinematics);
    Root->SetBoolField(TEXT("config_variable"), bConfig);
    Root->SetStringField(TEXT("category"), Var->Category.ToString()); // VERIFY vs engine source (FBPVariableDescription::Category : FText)
    Root->SetStringField(TEXT("replication"), Replication);
    if (Var->RepNotifyFunc != NAME_None)
    {
        Root->SetStringField(TEXT("rep_notify_func"), Var->RepNotifyFunc.ToString());
    }
    return MCPBpIf_SerializeJson(Root);
#else
    return MCPBpIf_Error(TEXT("editor-only"));
#endif
}
