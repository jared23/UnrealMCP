// Clean-room reflection helpers (SEPARATE TU) for the UnrealMCP plugin. See MCPReflectionLibrary.h.
//
// C++ #47 (2026-08-19) — AUDIO C++-ONLY handlers the pure-Python audio tools could not reach:
//   MetaSound DISCOVERY (#1-4) + a SoundSubmix parent WRITER (#38).
//
//   1) SearchMetaSoundNodesJson    (READ)  ISearchEngine::FindAllClasses -> name/category-filtered node catalog
//   2) DescribeMetaSoundNodeJson   (READ)  ISearchEngine::FindClassesWithName -> inputs/outputs/metadata of one node class
//   3) ListMetaSoundDataTypesJson  (READ)  IDataTypeRegistry::GetRegisteredDataTypeNames -> filtered data-type names
//   4) ListMetaSoundInterfacesJson (READ)  ISearchEngine::FindAllInterfaceVersions (+ IInterfaceRegistry) -> interfaces w/ member counts
//   5) SetSubmixParentJson         (WRITE) USoundSubmixWithParentBase::SetParentSubmix -> reparent/detach a submix
//
// WHY C++: MetaSound node classes are NOT UClasses (they are frontend registry entries) so Python
// dir(unreal)/issubclass cannot enumerate them, and the search/registry singletons are C++-only
// (NOT BlueprintCallable). The submix ParentSubmix / ChildSubmixes UPROPERTYs are EditConst (managed by
// the submix graph editor) so pure-Python set_editor_property REFUSES them (verified live, see
// audio_reparent.py); C++ can call the engine's own public SetParentSubmix() which does the whole job.
//
// EXPORT-PATCH VERDICT: NONE NEEDED. Every engine symbol used here is *_API-exported:
//   MetaSound discovery — all METASOUNDFRONTEND_API (the `UE_API` macro in each header expands to it):
//     * Metasound::Frontend::ISearchEngine::Get()                              METASOUNDFRONTEND_API (MetasoundFrontendSearchEngine.h:37)
//     * ISearchEngine::FindAllClasses(EResultVersion, bool)                    pure-virtual (WITH_EDITORONLY_DATA) (MetasoundFrontendSearchEngine.h:67)
//     * ISearchEngine::FindClassesWithName(ClassName, ESortByVersion)          pure-virtual (WITH_EDITORONLY_DATA) (MetasoundFrontendSearchEngine.h:75)
//     * ISearchEngine::FindAllInterfaceVersions(bool)                          pure-virtual (WITH_EDITORONLY_DATA) (MetasoundFrontendSearchEngine.h:90)
//     * Metasound::Frontend::INodeClassRegistry::GetFrontendClassFromRegistered(Key, OutClass)  static UE_API (MetasoundFrontendNodeClassRegistry.h:257)
//     * Metasound::Frontend::IDataTypeRegistry::Get() + GetRegisteredDataTypeNames(TArray<FName>&) UE_API/pure-virtual (MetasoundFrontendDataTypeRegistry.h:274,292)
//     * Metasound::Frontend::IInterfaceRegistry::Get() + FindInterface(Version, OutIface)         UE_API/pure-virtual (Interfaces/MetasoundFrontendInterfaceRegistry.h:91,111)
//     * FMetasoundFrontendClass::GetDefaultInterface() + FMetasoundFrontendClassMetadata::Get*()  UE_API/inline (MetasoundFrontendDocument.h:1806,1648-1689)
//   Submix writer — all ENGINE_API / public members (Engine module already linked):
//     * USoundSubmixWithParentBase::SetParentSubmix(USoundSubmixBase*, bool)   ENGINE_API (SoundSubmix.h:303; impl SoundSubmix.cpp:819 detaches from old parent's
//                                                                              ChildSubmixes + AddUnique to new parent's + Modify())
//     * USoundSubmixWithParentBase::ParentSubmix / USoundSubmixBase::ChildSubmixes  public UPROPERTYs (SoundSubmix.h:285,193)
//   Build.cs += "MetasoundFrontend" (Runtime module of the Metasound plugin — EnabledByDefault=true, loads
//   without a .uproject/.uplugin edit, same as the already-used Niagara/StateTree engine plugins). SoundSubmix
//   is in the Engine module (already a dep). MetasoundEngine is NOT required (no UMetaSoundSource/editor types used).
//
// EDITOR-ONLY: the discovery calls (FindAllClasses / FindClassesWithName-by-ClassInfo / FindAllInterfaceVersions)
// and the class metadata accessors are WITH_EDITORONLY_DATA / WITH_EDITOR. The whole file is #if WITH_EDITOR
// (this is an Editor-type module where WITH_EDITORONLY_DATA == 1). Non-editor build -> {"error":"editor-only"}.
//
// Conventions mirrored from sibling TUs (MCPReflection_Landscape.cpp / MCPReflection_WorldExt.cpp): file-local
// anon-namespace helpers prefixed MCPAud_ (internal linkage -> no ODR clash); JSON returns {"error": "..."} on
// any miss; every pointer null-guarded; never crash.
//
// UNDO MODEL (submix reparent): the write captures the FAITHFUL prior parent path and returns it; the inverse
// re-runs the SAME C++ setter with that prior path (empty -> detach to root). See audio_cpp.py + the
// editor_level.undo fold branch "audio_set_submix_parent". Reads take NO ledger.

#include "MCPReflectionLibrary.h"

#if WITH_EDITOR
#include "Sound/SoundSubmix.h"                          // USoundSubmix / USoundSubmixBase / USoundSubmixWithParentBase
#include "UObject/UObjectGlobals.h"                     // StaticLoadObject

// MetaSound frontend (Runtime plugin module MetasoundFrontend)
#include "MetasoundFrontendSearchEngine.h"              // Metasound::Frontend::ISearchEngine
#include "MetasoundFrontendDataTypeRegistry.h"          // Metasound::Frontend::IDataTypeRegistry
#include "MetasoundFrontendNodeClassRegistry.h"         // Metasound::Frontend::INodeClassRegistry (full-class lookup)
#include "MetasoundFrontendQuery.h"                      // Metasound::Frontend::FMetaSoundClassInfo
#include "MetasoundFrontendDocument.h"                   // FMetasoundFrontendClass / *Metadata / *ClassInterface / *ClassInput/Output / *ClassName
#include "Interfaces/MetasoundFrontendInterfaceRegistry.h" // Metasound::Frontend::IInterfaceRegistry
#endif // WITH_EDITOR

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

namespace
{
    // ---- file-local JSON boilerplate (internal linkage; per-TU) -------------------------------
    FString MCPAud_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPAud_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPAud_SerializeJson(Root);
    }

#if WITH_EDITOR
    // Case-insensitive substring test (empty Needle -> always matches).
    bool MCPAud_Contains(const FString& Haystack, const FString& Needle)
    {
        return Needle.IsEmpty() || Haystack.Contains(Needle, ESearchCase::IgnoreCase);
    }

    // Join a category hierarchy (TArray<FText>) into "A|B|C".
    FString MCPAud_JoinCategory(const TArray<FText>& Hierarchy)
    {
        TArray<FString> Parts;
        Parts.Reserve(Hierarchy.Num());
        for (const FText& T : Hierarchy)
        {
            Parts.Add(T.ToString());
        }
        return FString::Join(Parts, TEXT("|"));
    }

    // Resolve the full FMetasoundFrontendClass for a class-info via the node class registry.
    bool MCPAud_ResolveFullClass(const Metasound::Frontend::FMetaSoundClassInfo& Info, FMetasoundFrontendClass& OutClass)
    {
        return Metasound::Frontend::INodeClassRegistry::GetFrontendClassFromRegistered(Info.ToRegistryKey(), OutClass);
    }

    // Serialize one class vertex (input/output) -> {name, data_type[, default]}.
    TSharedRef<FJsonObject> MCPAud_VertexJson(const FMetasoundFrontendClassVertex& Vertex, const FString* OptDefault)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("name"), Vertex.Name.ToString());
        J->SetStringField(TEXT("data_type"), Vertex.TypeName.ToString());
        if (OptDefault)
        {
            J->SetStringField(TEXT("default"), *OptDefault);
        }
        return J;
    }

    // Add the class-name fields (full + split namespace/name/variant) to an object.
    void MCPAud_AddClassName(const TSharedRef<FJsonObject>& Obj, const FMetasoundFrontendClassName& Name)
    {
        Obj->SetStringField(TEXT("class_name"), Name.ToString());
        Obj->SetStringField(TEXT("namespace"), Name.Namespace.ToString());
        Obj->SetStringField(TEXT("name"), Name.Name.ToString());
        Obj->SetStringField(TEXT("variant"), Name.Variant.ToString());
    }
#endif // WITH_EDITOR
} // namespace

// ================================================================================================
// 1) SearchMetaSoundNodesJson — ISearchEngine::FindAllClasses -> filter by name substring / category -> catalog.
//    Filter:     case-insensitive substring matched against the class full-name (namespace.name.variant).
//    Category:   case-insensitive substring matched against the resolved category hierarchy ("A|B|C").
//    MaxResults: cap on returned entries (<= 0 -> uncapped). Each entry {class_name, namespace, name, variant,
//                display_name, category, description, class_type, version}.
// ================================================================================================
FString UMCPReflectionLibrary::SearchMetaSoundNodesJson(const FString& Filter, const FString& Category, int32 MaxResults)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    using namespace Metasound::Frontend;
    ISearchEngine& SE = ISearchEngine::Get();

    // "Highest" = only the highest major version of each class (the sane default for a browse catalog).
    const TArray<FMetaSoundClassInfo> Classes = SE.FindAllClasses(ISearchEngine::EResultVersion::Highest, /*bIncludeUnloadedAssets=*/false);

    TArray<TSharedPtr<FJsonValue>> Results;
    int32 Scanned = 0;
    for (const FMetaSoundClassInfo& Info : Classes)
    {
        ++Scanned;
        const FString FullName = Info.ClassName.ToString();
        // Cheap name gate first: skip resolution entirely for non-matching names when a Filter is given.
        if (!Filter.IsEmpty() && !MCPAud_Contains(FullName, Filter))
        {
            continue;
        }

        // Resolve the full class for display/description/category (works for native + asset classes).
        FString DisplayName = Info.ClassName.Name.ToString();
        FString Description;
        FString CategoryStr;
        FMetasoundFrontendClass FullClass;
        if (MCPAud_ResolveFullClass(Info, FullClass))
        {
            const FMetasoundFrontendClassMetadata& Meta = FullClass.Metadata;
            const FString D = Meta.GetDisplayName().ToString();
            if (!D.IsEmpty())
            {
                DisplayName = D;
            }
            Description = Meta.GetDescription().ToString();
            CategoryStr = MCPAud_JoinCategory(Meta.GetCategoryHierarchy());
        }

        // Category gate on the resolved hierarchy.
        if (!Category.IsEmpty() && !MCPAud_Contains(CategoryStr, Category))
        {
            continue;
        }

        TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
        MCPAud_AddClassName(Entry, Info.ClassName);
        Entry->SetStringField(TEXT("display_name"), DisplayName);
        Entry->SetStringField(TEXT("category"), CategoryStr);
        Entry->SetStringField(TEXT("description"), Description);
        Entry->SetNumberField(TEXT("class_type"), static_cast<int32>(Info.ClassType));
        Entry->SetStringField(TEXT("version"), Info.Version.ToString());
        Results.Add(MakeShared<FJsonValueObject>(Entry));

        if (MaxResults > 0 && Results.Num() >= MaxResults)
        {
            break;
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetNumberField(TEXT("total_classes"), Classes.Num());
    Root->SetNumberField(TEXT("scanned"), Scanned);
    Root->SetNumberField(TEXT("count"), Results.Num());
    Root->SetArrayField(TEXT("nodes"), Results);
    return MCPAud_SerializeJson(Root);
#else
    return MCPAud_Error(TEXT("MetaSound discovery requires WITH_EDITORONLY_DATA"));
#endif
#else
    return MCPAud_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 2) DescribeMetaSoundNodeJson — one node class by {Namespace, Name, Variant}: metadata + inputs/outputs.
//    Resolves the FULL FMetasoundFrontendClass objects directly via the bool overload of FindClassesWithName
//    (the FMetaSoundClassInfo-returning overloads do not resolve in this TU), then picks the highest version.
//    Inputs carry their default literal (page-default[0]) as a string.
// ================================================================================================
FString UMCPReflectionLibrary::DescribeMetaSoundNodeJson(const FString& Namespace, const FString& Name, const FString& Variant)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    using namespace Metasound::Frontend;
    if (Name.IsEmpty())
    {
        return MCPAud_Error(TEXT("Name is required (Namespace/Variant optional)"));
    }

    // Implemented via the WORKING FindAllClasses path (FindClassesWithName C2665's in this plugin TU): enumerate
    // all node classes (highest version of each) and match by {Namespace(optional), Name, Variant(optional)}.
    const FName WantNs(*Namespace), WantName(*Name), WantVar(*Variant);
    ISearchEngine& SE = ISearchEngine::Get();
    const TArray<FMetaSoundClassInfo> Classes = SE.FindAllClasses(ISearchEngine::EResultVersion::Highest, /*bIncludeUnloadedAssets=*/false);

    const FMetaSoundClassInfo* Found = nullptr;
    int32 MatchCount = 0;
    for (const FMetaSoundClassInfo& Info : Classes)
    {
        if (Info.ClassName.Name != WantName) { continue; }
        if (!Namespace.IsEmpty() && Info.ClassName.Namespace != WantNs) { continue; }
        if (!Variant.IsEmpty() && Info.ClassName.Variant != WantVar) { continue; }
        ++MatchCount;
        if (!Found) { Found = &Info; }
    }
    if (!Found)
    {
        return MCPAud_Error(FString::Printf(TEXT("no registered MetaSound node class matches namespace='%s' name='%s' variant='%s'"),
            *Namespace, *Name, *Variant));
    }

    FMetasoundFrontendClass FullClass;
    if (!MCPAud_ResolveFullClass(*Found, FullClass))
    {
        return MCPAud_Error(FString::Printf(TEXT("class '%s' matched but is not in the node class registry (unloaded asset?)"), *Found->ClassName.ToString()));
    }

    const FMetasoundFrontendClassMetadata& Meta = FullClass.Metadata;
    const FMetasoundFrontendClassInterface& Iface = FullClass.GetDefaultInterface();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    MCPAud_AddClassName(Root, Meta.GetClassName());
    Root->SetStringField(TEXT("display_name"), Meta.GetDisplayName().ToString());
    Root->SetStringField(TEXT("description"), Meta.GetDescription().ToString());
    Root->SetStringField(TEXT("category"), MCPAud_JoinCategory(Meta.GetCategoryHierarchy()));
    Root->SetStringField(TEXT("author"), Meta.GetAuthor());
    Root->SetNumberField(TEXT("class_type"), static_cast<int32>(Meta.GetType()));
    Root->SetStringField(TEXT("version"), Meta.GetVersion().ToString());
    Root->SetNumberField(TEXT("match_count"), MatchCount);

    // Keywords.
    {
        TArray<TSharedPtr<FJsonValue>> KwArr;
        for (const FText& Kw : Meta.GetKeywords())
        {
            KwArr.Add(MakeShared<FJsonValueString>(Kw.ToString()));
        }
        Root->SetArrayField(TEXT("keywords"), KwArr);
    }

    // Inputs (with default literal).
    {
        TArray<TSharedPtr<FJsonValue>> InArr;
        for (const FMetasoundFrontendClassInput& In : Iface.Inputs)
        {
            FString DefaultStr;
            const TArray<FMetasoundFrontendClassInputDefault>& Defaults = In.GetDefaults();
            if (Defaults.Num() > 0)
            {
                DefaultStr = Defaults[0].Literal.ToString();
            }
            InArr.Add(MakeShared<FJsonValueObject>(MCPAud_VertexJson(In, &DefaultStr)));
        }
        Root->SetArrayField(TEXT("inputs"), InArr);
        Root->SetNumberField(TEXT("input_count"), InArr.Num());
    }

    // Outputs.
    {
        TArray<TSharedPtr<FJsonValue>> OutArr;
        for (const FMetasoundFrontendClassOutput& Out : Iface.Outputs)
        {
            OutArr.Add(MakeShared<FJsonValueObject>(MCPAud_VertexJson(Out, nullptr)));
        }
        Root->SetArrayField(TEXT("outputs"), OutArr);
        Root->SetNumberField(TEXT("output_count"), OutArr.Num());
    }

    return MCPAud_SerializeJson(Root);
#else
    return MCPAud_Error(TEXT("MetaSound discovery requires WITH_EDITORONLY_DATA"));
#endif
#else
    return MCPAud_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 3) ListMetaSoundDataTypesJson — IDataTypeRegistry::GetRegisteredDataTypeNames -> [name] (Filter substring).
// ================================================================================================
FString UMCPReflectionLibrary::ListMetaSoundDataTypesJson(const FString& Filter)
{
#if WITH_EDITOR
    using namespace Metasound::Frontend;
    IDataTypeRegistry& Reg = IDataTypeRegistry::Get();

    TArray<FName> Names;
    Reg.GetRegisteredDataTypeNames(Names);
    Names.Sort([](const FName& A, const FName& B) { return A.LexicalLess(B); });

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FName& N : Names)
    {
        const FString S = N.ToString();
        if (!MCPAud_Contains(S, Filter))
        {
            continue;
        }
        Arr.Add(MakeShared<FJsonValueString>(S));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetNumberField(TEXT("total"), Names.Num());
    Root->SetNumberField(TEXT("count"), Arr.Num());
    Root->SetArrayField(TEXT("data_types"), Arr);
    return MCPAud_SerializeJson(Root);
#else
    return MCPAud_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 4) ListMetaSoundInterfacesJson — ISearchEngine::FindAllInterfaceVersions (+ IInterfaceRegistry::FindInterface
//    for member counts) -> [{name, version, input_count, output_count, environment_count}] (Filter on name).
// ================================================================================================
FString UMCPReflectionLibrary::ListMetaSoundInterfacesJson(const FString& Filter)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    using namespace Metasound::Frontend;
    ISearchEngine& SE = ISearchEngine::Get();
    IInterfaceRegistry& IReg = IInterfaceRegistry::Get();

    const TArray<FMetasoundFrontendVersion> Versions = SE.FindAllInterfaceVersions(/*bInIncludeAllVersions=*/true);

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FMetasoundFrontendVersion& Ver : Versions)
    {
        const FString NameStr = Ver.Name.ToString();
        if (!MCPAud_Contains(NameStr, Filter))
        {
            continue;
        }

        TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
        Entry->SetStringField(TEXT("name"), NameStr);
        Entry->SetStringField(TEXT("version"), Ver.Number.ToString());
        Entry->SetNumberField(TEXT("version_major"), Ver.Number.Major);
        Entry->SetNumberField(TEXT("version_minor"), Ver.Number.Minor);

        // Resolve exact-version member counts (best-effort; interface may not resolve for a browse-only entry).
        FMetasoundFrontendInterface Iface;
        if (IReg.FindInterface(Ver, Iface))
        {
            Entry->SetNumberField(TEXT("input_count"), Iface.Inputs.Num());
            Entry->SetNumberField(TEXT("output_count"), Iface.Outputs.Num());
            Entry->SetNumberField(TEXT("environment_count"), Iface.Environment.Num());
            Entry->SetBoolField(TEXT("resolved"), true);
        }
        else
        {
            Entry->SetBoolField(TEXT("resolved"), false);
        }
        Arr.Add(MakeShared<FJsonValueObject>(Entry));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetNumberField(TEXT("total"), Versions.Num());
    Root->SetNumberField(TEXT("count"), Arr.Num());
    Root->SetArrayField(TEXT("interfaces"), Arr);
    return MCPAud_SerializeJson(Root);
#else
    return MCPAud_Error(TEXT("MetaSound discovery requires WITH_EDITORONLY_DATA"));
#endif
#else
    return MCPAud_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 5) SetSubmixParentJson — reparent a USoundSubmix under ParentPath (empty -> detach to root).
//    Uses the engine's own public ENGINE_API USoundSubmixWithParentBase::SetParentSubmix, which detaches
//    the submix from its old parent's ChildSubmixes and AddUnique's it to the new parent's ChildSubmixes
//    (SoundSubmix.cpp:819). Cycle-guarded. Captures + returns prior_parent_path for the ledger inverse, and
//    touched_paths (submix + old/new parents) for the Python side to save. Direct C++ write of the EditConst
//    ParentSubmix/ChildSubmixes UPROPERTYs the pure-Python tool could not touch.
// ================================================================================================
FString UMCPReflectionLibrary::SetSubmixParentJson(const FString& SubmixPath, const FString& ParentPath)
{
#if WITH_EDITOR
    if (SubmixPath.IsEmpty())
    {
        return MCPAud_Error(TEXT("SubmixPath is required"));
    }

    UObject* SubmixObj = StaticLoadObject(USoundSubmixBase::StaticClass(), nullptr, *SubmixPath);
    USoundSubmixWithParentBase* Submix = Cast<USoundSubmixWithParentBase>(SubmixObj);
    if (!Submix)
    {
        if (SubmixObj)
        {
            return MCPAud_Error(FString::Printf(TEXT("submix '%s' (%s) does not support a parent (not a USoundSubmixWithParentBase; e.g. an endpoint submix)"), *SubmixPath, *SubmixObj->GetClass()->GetName()));
        }
        return MCPAud_Error(FString::Printf(TEXT("could not load a SoundSubmix at '%s'"), *SubmixPath));
    }

    // Resolve the new parent (empty ParentPath -> detach to root).
    USoundSubmixBase* NewParent = nullptr;
    if (!ParentPath.IsEmpty())
    {
        UObject* ParentObj = StaticLoadObject(USoundSubmixBase::StaticClass(), nullptr, *ParentPath);
        NewParent = Cast<USoundSubmixBase>(ParentObj);
        if (!NewParent)
        {
            return MCPAud_Error(FString::Printf(TEXT("could not load a SoundSubmix parent at '%s'"), *ParentPath));
        }
        if (NewParent == Submix)
        {
            return MCPAud_Error(FString::Printf(TEXT("cannot parent a submix to itself: '%s'"), *SubmixPath));
        }
        // Cycle guard: walk up the new parent's ParentSubmix chain; refuse if it reaches Submix.
        TSet<const USoundSubmixBase*> Seen;
        const USoundSubmixBase* Cur = NewParent;
        while (Cur)
        {
            if (Cur == Submix)
            {
                return MCPAud_Error(FString::Printf(TEXT("refusing: parenting '%s' under '%s' would create a cycle"), *SubmixPath, *ParentPath));
            }
            if (Seen.Contains(Cur))
            {
                break; // pre-existing cycle upstream; stop walking
            }
            Seen.Add(Cur);
            if (const USoundSubmixWithParentBase* CurWP = Cast<USoundSubmixWithParentBase>(Cur))
            {
                Cur = CurWP->ParentSubmix;
            }
            else
            {
                Cur = nullptr;
            }
        }
    }

    // Capture prior parent (faithful inverse) BEFORE mutating.
    USoundSubmixBase* OldParent = Submix->ParentSubmix;
    const FString PriorParentPath = OldParent ? OldParent->GetPathName() : FString();

    // Engine setter: detaches from OldParent->ChildSubmixes + AddUnique to NewParent->ChildSubmixes + Modify().
    Submix->SetParentSubmix(NewParent, /*bModifyAssets=*/true);

    // Ensure every touched package persists (SetParentSubmix Modify()s the submix + old parent, but only
    // AddUnique's onto the new parent without Modify() — mark all three dirty explicitly).
    Submix->MarkPackageDirty();
    if (OldParent)
    {
        OldParent->MarkPackageDirty();
    }
    if (NewParent)
    {
        NewParent->Modify();
        NewParent->MarkPackageDirty();
    }

    // Collect distinct touched object paths for the Python side to save.
    TArray<TSharedPtr<FJsonValue>> Touched;
    TSet<FString> TouchedSet;
    auto AddTouched = [&Touched, &TouchedSet](UObject* Obj)
    {
        if (Obj)
        {
            const FString P = Obj->GetPathName();
            if (!TouchedSet.Contains(P))
            {
                TouchedSet.Add(P);
                Touched.Add(MakeShared<FJsonValueString>(P));
            }
        }
    };
    AddTouched(Submix);
    AddTouched(OldParent);
    AddTouched(NewParent);

    const USoundSubmixBase* ReadbackParent = Submix->ParentSubmix;

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("submix"), Submix->GetName());
    Root->SetStringField(TEXT("submix_path"), Submix->GetPathName());
    Root->SetStringField(TEXT("prior_parent_path"), PriorParentPath);
    Root->SetStringField(TEXT("new_parent_path"), NewParent ? NewParent->GetPathName() : FString());
    Root->SetStringField(TEXT("readback_parent_path"), ReadbackParent ? ReadbackParent->GetPathName() : FString());
    Root->SetBoolField(TEXT("detached"), NewParent == nullptr);
    Root->SetArrayField(TEXT("touched_paths"), Touched);
    return MCPAud_SerializeJson(Root);
#else
    return MCPAud_Error(TEXT("editor-only"));
#endif
}
