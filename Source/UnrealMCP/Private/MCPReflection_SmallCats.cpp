// Clean-room reflection helpers (SEPARATE TU) for the UnrealMCP plugin. See MCPReflectionLibrary.h.
//
// "Batch B" small-category sweep — console + projectsettings + dataassets:
//   1) GetConsoleObjectInfoJson   (READ)  console object (cvar OR exec command) info via IConsoleManager
//   2) SetDeveloperSettingJson    (WRITE) set + persist one UDeveloperSettings config property to Default*.ini
//   3) SearchProjectSettingsJson  (READ)  enumerate UDeveloperSettings classes (container/category/section)
//   4) GetPropertyValidTypesJson  (READ)  allowed/concrete classes assignable to an object/class property
//
// Every handler is a member-function definition of UMCPReflectionLibrary (declared in
// MCPReflectionLibrary.h); defining them here in their own TU compiles fine. Build.cs already links
// Core/CoreUObject/Engine/UnrealEd/DeveloperSettings/Settings/Projects -> NO Build.cs change, NO export patch.
//
// Conventions mirrored from the sibling TUs (MCPReflection_Input.cpp / MCPReflection_BlueprintSCS.cpp):
//   * File-local anon-namespace helpers prefixed MCPSc_ (the anon helpers in MCPReflectionLibrary.cpp are
//     internal-linkage per-TU, so this .cpp defines its own — no ODR clash).
//   * JSON returns {"error": "..."} on any miss; every pointer is null/bounds-guarded; never crash.
//   * #if WITH_EDITOR only where a call is genuinely editor-only (metadata reads, PreEditChange/
//     PostEditChangeProperty). The console + registry reads are runtime-safe.

#include "MCPReflectionLibrary.h"

#include "HAL/IConsoleManager.h"          // IConsoleManager / IConsoleObject / IConsoleVariable / IConsoleCommand (Core)
#include "Engine/DeveloperSettings.h"     // UDeveloperSettings (DeveloperSettings module)
#include "Misc/ConfigCacheIni.h"          // GConfig (Core)
#include "Misc/FileHelper.h"              // FFileHelper::LoadFileToString (Core) — had_prior on-disk fallback

#include "UObject/UnrealType.h"           // FProperty / FObjectPropertyBase / FSoftObjectProperty / FindFProperty (CoreUObject)
#include "UObject/Class.h"                // UClass / UClass::TryFindTypeSlowSafe (CoreUObject)
#include "UObject/UObjectIterator.h"      // TObjectIterator<UClass> (CoreUObject)
#include "UObject/EnumProperty.h"         // FEnumProperty (CoreUObject)

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

namespace
{
    // ---- file-local JSON boilerplate (internal linkage; per-TU) -------------------------------
    FString MCPSc_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPSc_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPSc_SerializeJson(Root);
    }

    // Resolve a UClass by bare short name ("RendererSettings") OR full path ("/Script/Engine.RendererSettings").
    UClass* MCPSc_FindClass(const FString& Name)
    {
        if (Name.IsEmpty())
        {
            return nullptr;
        }
        // TryFindTypeSlowSafe handles both short-name and path-name and never asserts/logs on miss.
        if (UClass* C = UClass::TryFindTypeSlowSafe<UClass>(Name))
        {
            return C;
        }
        // Best-effort load for an unloaded module's class specified by full object path.
        if (Name.Contains(TEXT(".")) || Name.Contains(TEXT("/")))
        {
            if (UClass* C = LoadObject<UClass>(nullptr, *Name))
            {
                return C;
            }
        }
        return nullptr;
    }

    // Walk a dotted PropertyPath ("Foo" or "Struct.Member") from OwnerStruct. If Container is non-null the
    // matching value-ptr at the leaf is written to OutPtr (for a read/write of the actual value); pass
    // Container=nullptr to resolve the leaf FProperty by TYPE only (OutPtr stays null). Descends FStructProperty.
    bool MCPSc_ResolvePropertyPath(UStruct* OwnerStruct, void* Container, const FString& Path,
                                   FProperty*& OutProp, void*& OutPtr, FString& OutErr)
    {
        OutProp = nullptr;
        OutPtr = nullptr;
        if (!OwnerStruct)
        {
            OutErr = TEXT("null owner struct");
            return false;
        }
        TArray<FString> Segs;
        Path.ParseIntoArray(Segs, TEXT("."), /*bCullEmpty=*/true);
        if (Segs.Num() == 0)
        {
            OutErr = TEXT("empty property path");
            return false;
        }
        UStruct* CurStruct = OwnerStruct;
        void* CurPtr = Container;
        for (int32 i = 0; i < Segs.Num(); ++i)
        {
            FProperty* P = FindFProperty<FProperty>(CurStruct, *Segs[i]);
            if (!P)
            {
                OutErr = FString::Printf(TEXT("no property '%s' on '%s'"), *Segs[i], *CurStruct->GetName());
                return false;
            }
            if (i == Segs.Num() - 1)
            {
                OutProp = P;
                OutPtr = CurPtr ? P->ContainerPtrToValuePtr<void>(CurPtr) : nullptr;
                return true;
            }
            FStructProperty* SP = CastField<FStructProperty>(P);
            if (!SP || !SP->Struct)
            {
                OutErr = FString::Printf(TEXT("'%s' is not a struct; cannot descend into '%s'"), *Segs[i], *Path);
                return false;
            }
            if (CurPtr)
            {
                CurPtr = P->ContainerPtrToValuePtr<void>(CurPtr);
            }
            CurStruct = SP->Struct;
        }
        OutErr = TEXT("unreachable");
        return false;
    }

    // Apply one bare JSON value to one FProperty at ValuePtr — typed fast-paths + ImportText_Direct universal
    // fallback. Compact clone of MCPReflection_BlueprintSCS.cpp::MCPBpScs_ApplyJsonToProperty (that one is a
    // static in a different TU). Struct/array/map/etc. expect a UE ExportText string passed as a JSON string.
    bool MCPSc_ApplyJsonToProperty(FProperty* Prop, void* ValuePtr, UObject* Owner,
                                   const TSharedPtr<FJsonValue>& V, FString& OutErr)
    {
        if (!Prop || !ValuePtr || !V.IsValid())
        {
            OutErr = TEXT("null prop/value");
            return false;
        }
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
            OutErr = TEXT("enum has no underlying");
            return false;
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

    // Parse a ValueJson document into a single bare JSON value. UE's reader rejects a bare scalar at the
    // document root, so callers pass an array-wrapped value ([1.5] / [true] / ["EnumName"] / ["(X=1,Y=2)"]);
    // a single-element array is unwrapped. Objects/multi-element arrays pass through unchanged.
    bool MCPSc_ParseValueJson(const FString& ValueJson, TSharedPtr<FJsonValue>& Out, FString& OutErr)
    {
        TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(ValueJson);
        if (!FJsonSerializer::Deserialize(R, Out) || !Out.IsValid())
        {
            OutErr = TEXT("invalid value JSON (pass an array-wrapped value, e.g. [1.5] or [\"(X=1,Y=2,Z=3)\"])");
            return false;
        }
        if (Out->Type == EJson::Array)
        {
            const TArray<TSharedPtr<FJsonValue>>& Arr = Out->AsArray();
            if (Arr.Num() == 1 && Arr[0].IsValid()) { Out = Arr[0]; }
        }
        return true;
    }

    // Notable console-object flag bits -> label (a curated subset; not exhaustive).
    void MCPSc_AddCVarFlags(const TSharedRef<FJsonObject>& Obj, EConsoleVariableFlags Flags)
    {
        TArray<TSharedPtr<FJsonValue>> A;
        auto Add = [&A, Flags](const TCHAR* Name, EConsoleVariableFlags Bit)
        {
            if (((uint32)Flags & (uint32)Bit) != 0)
            {
                A.Add(MakeShared<FJsonValueString>(Name));
            }
        };
        Add(TEXT("ReadOnly"), ECVF_ReadOnly);
        Add(TEXT("Cheat"), ECVF_Cheat);
        Add(TEXT("Unregistered"), ECVF_Unregistered);
        Add(TEXT("Scalability"), ECVF_Scalability);
        Add(TEXT("ScalabilityGroup"), ECVF_ScalabilityGroup);
        Add(TEXT("RenderThreadSafe"), ECVF_RenderThreadSafe);
        Add(TEXT("Preview"), ECVF_Preview);
        Obj->SetArrayField(TEXT("flags"), A);
    }

    // Count class properties flagged CPF_Config (the ones that persist to Default*.ini), incl. inherited.
    int32 MCPSc_CountConfigProps(UClass* Cls)
    {
        int32 N = 0;
        for (TFieldIterator<FProperty> It(Cls, EFieldIteratorFlags::IncludeSuper); It; ++It)
        {
            if (It->HasAnyPropertyFlags(CPF_Config))
            {
                ++N;
            }
        }
        return N;
    }
}

// ================================================================================================
// 1) READ: console object info (cvar OR exec command) via IConsoleManager::FindConsoleObject.
//    Fixes the cvar-only get_console_command_info + console_command_exists (existence now covers
//    registered exec commands too — FindConsoleObject != null). Note: FSelfRegisteringExec/stat/show
//    style commands are NOT IConsoleObjects and remain undiscoverable (documented, not faked).
// ================================================================================================
FString UMCPReflectionLibrary::GetConsoleObjectInfoJson(const FString& Name)
{
    if (Name.IsEmpty())
    {
        return MCPSc_Error(TEXT("empty console object name"));
    }

    IConsoleManager& Mgr = IConsoleManager::Get();
    IConsoleObject* Obj = Mgr.FindConsoleObject(*Name, /*bTrackFrequentCalls=*/false);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("name"), Name);

    if (!Obj)
    {
        Root->SetBoolField(TEXT("found"), false);
        Root->SetBoolField(TEXT("is_variable"), false);
        Root->SetBoolField(TEXT("is_command"), false);
        Root->SetStringField(TEXT("note"),
            TEXT("no registered IConsoleObject with this exact name. It may be misspelled, or a "
                 "FSelfRegisteringExec / stat / show style exec command (those are not console objects "
                 "and cannot be existence-checked without running them)."));
        return MCPSc_SerializeJson(Root);
    }

    Root->SetBoolField(TEXT("found"), true);

    IConsoleVariable* Var = Obj->AsVariable();
    IConsoleCommand* Cmd = Obj->AsCommand();
    const bool bIsVar = (Var != nullptr);
    const bool bIsCmd = (Cmd != nullptr);
    Root->SetBoolField(TEXT("is_variable"), bIsVar);
    Root->SetBoolField(TEXT("is_command"), bIsCmd);

    const EConsoleVariableFlags Flags = Obj->GetFlags();
    MCPSc_AddCVarFlags(Root, Flags);
    Root->SetStringField(TEXT("help"), Obj->GetHelp());
    Root->SetBoolField(TEXT("is_enabled"), Obj->IsEnabled());

    if (bIsVar)
    {
        Root->SetStringField(TEXT("current_value"), Var->GetString());
        Root->SetBoolField(TEXT("is_bool"), Var->IsVariableBool());
        Root->SetBoolField(TEXT("is_int"), Var->IsVariableInt());
        Root->SetBoolField(TEXT("is_float"), Var->IsVariableFloat());
        Root->SetBoolField(TEXT("is_string"), Var->IsVariableString());
        const TCHAR* SetBy = GetConsoleVariableSetByName((EConsoleVariableFlags)((uint32)Flags & ECVF_SetByMask));
        Root->SetStringField(TEXT("set_by"), SetBy ? SetBy : TEXT(""));
    }

    return MCPSc_SerializeJson(Root);
}

// ================================================================================================
// 2) WRITE + PERSIST: set one UDeveloperSettings config property and persist it to its Default*.ini.
//    Resolves the CDO by class name, PreEditChange -> typed-setter/ImportText_Direct -> PostEditChange
//    -> TryUpdateDefaultConfigFile. Captures prev (ExportText) + had_prior (whether the ini key pre-existed).
// ================================================================================================
FString UMCPReflectionLibrary::SetDeveloperSettingJson(const FString& SettingsClassName,
                                                       const FString& PropertyPath, const FString& ValueJson)
{
#if WITH_EDITOR
    UClass* Cls = MCPSc_FindClass(SettingsClassName);
    if (!Cls)
    {
        return MCPSc_Error(FString::Printf(TEXT("could not resolve class '%s'"), *SettingsClassName));
    }
    if (!Cls->IsChildOf(UDeveloperSettings::StaticClass()))
    {
        return MCPSc_Error(FString::Printf(TEXT("class '%s' does not derive UDeveloperSettings"), *Cls->GetName()));
    }
    if (Cls->HasAnyClassFlags(CLASS_Abstract))
    {
        return MCPSc_Error(FString::Printf(TEXT("class '%s' is abstract"), *Cls->GetName()));
    }

    UDeveloperSettings* Settings = Cast<UDeveloperSettings>(Cls->GetDefaultObject(/*bCreateIfNeeded=*/true));
    if (!Settings)
    {
        return MCPSc_Error(FString::Printf(TEXT("could not get the CDO for '%s'"), *Cls->GetName()));
    }

    FProperty* Prop = nullptr;
    void* ValuePtr = nullptr;
    FString ResolveErr;
    if (!MCPSc_ResolvePropertyPath(Cls, Settings, PropertyPath, Prop, ValuePtr, ResolveErr) || !Prop || !ValuePtr)
    {
        return MCPSc_Error(ResolveErr.IsEmpty() ? TEXT("could not resolve property path") : ResolveErr);
    }

    TSharedPtr<FJsonValue> V;
    FString ParseErr;
    if (!MCPSc_ParseValueJson(ValueJson, V, ParseErr))
    {
        return MCPSc_Error(ParseErr);
    }

    // Capture PRIOR value via single-property ExportText (for the undo).
    FString PrevExported;
    Prop->ExportTextItem_Direct(PrevExported, ValuePtr, /*Default*/ nullptr, /*Parent*/ Settings, PPF_None); // VERIFY vs engine source: ExportTextItem_Direct

    // Best-effort: did the ini key already exist BEFORE this write? (drives the undo: restore-prev if it did,
    // clear-the-key if it did not.) The config section for a (non-perObject) settings class is
    // GetClass()->GetPathName(); the class may override it. The key is the leaf property's name.
    FString Section = Cls->GetPathName();
    Settings->OverrideConfigSection(Section);          // VERIFY vs engine source: UObject::OverrideConfigSection
    const FString ConfigFile = Settings->GetDefaultConfigFilename(); // VERIFY vs engine source: UObject::GetDefaultConfigFilename
    const FString KeyName = Prop->GetName();
    bool bHadPrior = false;
    if (GConfig)
    {
        FString ExistingVal;
        bHadPrior = GConfig->GetString(*Section, *KeyName, ExistingVal, ConfigFile);
    }
    if (!bHadPrior)
    {
        // GConfig is keyed by the merged/platform config, so a Default*.ini path may not resolve to a loaded
        // branch. Fall back to scanning the on-disk Default*.ini for the "[Section]" + "Key="/"+Key="/"Key[" line.
        FString FileText;
        if (FFileHelper::LoadFileToString(FileText, *ConfigFile))
        {
            TArray<FString> Lines;
            FileText.ParseIntoArrayLines(Lines, /*bCullEmpty=*/false);
            bool bInSection = false;
            const FString SectionHeader = FString::Printf(TEXT("[%s]"), *Section);
            for (FString Ln : Lines)
            {
                Ln.TrimStartAndEndInline();
                if (Ln.StartsWith(TEXT("[")) && Ln.EndsWith(TEXT("]")))
                {
                    bInSection = Ln.Equals(SectionHeader, ESearchCase::IgnoreCase);
                    continue;
                }
                if (bInSection && !Ln.IsEmpty() && !Ln.StartsWith(TEXT(";")))
                {
                    if (Ln.StartsWith(KeyName + TEXT("=")) || Ln.StartsWith(TEXT("+") + KeyName + TEXT("=")) ||
                        Ln.StartsWith(TEXT("-") + KeyName + TEXT("=")) || Ln.StartsWith(TEXT("!") + KeyName + TEXT("=")) ||
                        Ln.StartsWith(KeyName + TEXT("[")))
                    {
                        bHadPrior = true;
                        break;
                    }
                }
            }
        }
    }

    Settings->Modify();
    Settings->PreEditChange(Prop);
    FString ApplyErr;
    if (!MCPSc_ApplyJsonToProperty(Prop, ValuePtr, Settings, V, ApplyErr))
    {
        return MCPSc_Error(ApplyErr.IsEmpty() ? TEXT("failed to set property") : ApplyErr);
    }
    {
        FPropertyChangedEvent Evt(Prop);
        Settings->PostEditChangeProperty(Evt);
    }

    const bool bPersisted = Settings->TryUpdateDefaultConfigFile(); // VERIFY vs engine source: writes the real Default*.ini

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("settings_class"), Cls->GetName());
    Root->SetStringField(TEXT("settings_class_path"), Cls->GetPathName());
    Root->SetStringField(TEXT("property"), PropertyPath);
    Root->SetStringField(TEXT("prev"), PrevExported);   // ExportText string; inverse re-applies via ValueJson=[prev]
    Root->SetBoolField(TEXT("had_prior"), bHadPrior);
    Root->SetBoolField(TEXT("persisted"), bPersisted);
    Root->SetStringField(TEXT("config_file"), ConfigFile);
    Root->SetStringField(TEXT("config_section"), Section);
    return MCPSc_SerializeJson(Root);
#else
    return MCPSc_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 3) READ: enumerate the registered UDeveloperSettings classes (Project Settings sections). The
//    container/category/section come from virtuals (GetContainerName/GetCategoryName/GetSectionName)
//    that are NOT UPROPERTYs -> Python-unreachable, hence this C++ reader. Fuzzy-matches Filter.
// ================================================================================================
FString UMCPReflectionLibrary::SearchProjectSettingsJson(const FString& Filter, const FString& Container)
{
    const FString FilterLC = Filter.ToLower();
    const FString ContainerLC = Container.ToLower();

    TArray<TSharedPtr<FJsonValue>> Matches;
    for (TObjectIterator<UClass> It; It; ++It)
    {
        UClass* Cls = *It;
        if (!Cls || !Cls->IsChildOf(UDeveloperSettings::StaticClass()))
        {
            continue;
        }
        if (Cls == UDeveloperSettings::StaticClass() || Cls->HasAnyClassFlags(CLASS_Abstract))
        {
            continue;
        }
        // Skip stale reinstanced/skeleton classes.
        const FString ClsName = Cls->GetName();
        if (ClsName.StartsWith(TEXT("SKEL_")) || ClsName.StartsWith(TEXT("REINST_")) ||
            ClsName.StartsWith(TEXT("TRASHCLASS_")) || ClsName.StartsWith(TEXT("HOTRELOADED_")))
        {
            continue;
        }

        UDeveloperSettings* CDO = Cast<UDeveloperSettings>(Cls->GetDefaultObject(/*bCreateIfNeeded=*/false));
        if (!CDO)
        {
            continue;
        }

        const FString ContainerName = CDO->GetContainerName().ToString();
        const FString CategoryName = CDO->GetCategoryName().ToString();
        const FString SectionName = CDO->GetSectionName().ToString();

        // Optional container filter: match the container ("Project"/"Editor") OR the category ("Game"/...).
        if (!ContainerLC.IsEmpty())
        {
            if (!ContainerName.ToLower().Equals(ContainerLC) && !CategoryName.ToLower().Equals(ContainerLC))
            {
                continue;
            }
        }

        // Fuzzy filter across class / category / section (case-insensitive substring).
        if (!FilterLC.IsEmpty())
        {
            const bool bHit = ClsName.ToLower().Contains(FilterLC)
                || CategoryName.ToLower().Contains(FilterLC)
                || SectionName.ToLower().Contains(FilterLC);
            if (!bHit)
            {
                continue;
            }
        }

        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("class"), ClsName);
        J->SetStringField(TEXT("class_path"), Cls->GetPathName());
        J->SetStringField(TEXT("container"), ContainerName);
        J->SetStringField(TEXT("category"), CategoryName);
        J->SetStringField(TEXT("section"), SectionName);
        J->SetNumberField(TEXT("config_property_count"), MCPSc_CountConfigProps(Cls));
        Matches.Add(MakeShared<FJsonValueObject>(J));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("filter"), Filter);
    Root->SetStringField(TEXT("container"), Container);
    Root->SetNumberField(TEXT("match_count"), Matches.Num());
    Root->SetArrayField(TEXT("settings"), Matches);
    return MCPSc_SerializeJson(Root);
}

// ================================================================================================
// 4) READ: valid types assignable to an object/class property. Resolves the class + FindFProperty along
//    PropertyPath, derives the base (PropertyClass / MetaClass), reads AllowedClasses/DisallowedClasses/
//    MustImplement metadata, then enumerates concrete UClasses IsChildOf the base (Filter + IncludeAbstract).
// ================================================================================================
FString UMCPReflectionLibrary::GetPropertyValidTypesJson(const FString& ClassName, const FString& PropertyPath,
                                                         const FString& Filter, bool bIncludeAbstract)
{
    UClass* OwnerClass = MCPSc_FindClass(ClassName);
    if (!OwnerClass)
    {
        return MCPSc_Error(FString::Printf(TEXT("could not resolve class '%s'"), *ClassName));
    }

    FProperty* Prop = nullptr;
    void* Unused = nullptr;
    FString ResolveErr;
    if (!MCPSc_ResolvePropertyPath(OwnerClass, /*Container=*/nullptr, PropertyPath, Prop, Unused, ResolveErr) || !Prop)
    {
        return MCPSc_Error(ResolveErr.IsEmpty() ? TEXT("could not resolve property path") : ResolveErr);
    }

    // Derive the base class the property accepts (the "kind" of object/class it references).
    UClass* BaseClass = nullptr;
    bool bIsMetaClass = false;   // true for class/soft-class properties (they store a UClass*, not an instance)
    FString PropKind;
    if (FClassProperty* CP = CastField<FClassProperty>(Prop))
    {
        BaseClass = CP->MetaClass;
        bIsMetaClass = true;
        PropKind = TEXT("class");
    }
    else if (FSoftClassProperty* SCP = CastField<FSoftClassProperty>(Prop))
    {
        BaseClass = SCP->MetaClass;
        bIsMetaClass = true;
        PropKind = TEXT("soft_class");
    }
    else if (FSoftObjectProperty* SOP = CastField<FSoftObjectProperty>(Prop))
    {
        BaseClass = SOP->PropertyClass;
        PropKind = TEXT("soft_object");
    }
    else if (FObjectPropertyBase* OP = CastField<FObjectPropertyBase>(Prop))
    {
        BaseClass = OP->PropertyClass;
        PropKind = TEXT("object");
    }
    else
    {
        return MCPSc_Error(FString::Printf(TEXT("property '%s' is not an object/class reference (cpp_type '%s')"),
            *PropertyPath, *Prop->GetCPPType()));
    }
    if (!BaseClass)
    {
        BaseClass = UObject::StaticClass();
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("class"), OwnerClass->GetName());
    Root->SetStringField(TEXT("property"), PropertyPath);
    Root->SetStringField(TEXT("property_kind"), PropKind);
    Root->SetBoolField(TEXT("is_meta_class"), bIsMetaClass);
    Root->SetStringField(TEXT("base_class"), BaseClass->GetName());
    Root->SetStringField(TEXT("base_class_path"), BaseClass->GetPathName());

    // Metadata-declared restrictions (editor-only meta). Report them; they further narrow the picker in-editor.
    TArray<UClass*> AllowedResolved;
    TArray<UClass*> DisallowedResolved;
    UClass* MustImplement = nullptr;
    TArray<TSharedPtr<FJsonValue>> AllowedJson;
    TArray<TSharedPtr<FJsonValue>> DisallowedJson;
    FString MustImplementName;
#if WITH_EDITOR
    auto ParseClassList = [](const FString& CSV, TArray<UClass*>& OutResolved, TArray<TSharedPtr<FJsonValue>>& OutJson)
    {
        TArray<FString> Names;
        CSV.ParseIntoArray(Names, TEXT(","), true);
        for (FString N : Names)
        {
            N.TrimStartAndEndInline();
            if (N.IsEmpty()) { continue; }
            OutJson.Add(MakeShared<FJsonValueString>(N));
            if (UClass* Rc = MCPSc_FindClass(N)) { OutResolved.Add(Rc); }
        }
    };
    if (Prop->HasMetaData(TEXT("AllowedClasses")))
    {
        ParseClassList(Prop->GetMetaData(TEXT("AllowedClasses")), AllowedResolved, AllowedJson);
    }
    if (Prop->HasMetaData(TEXT("DisallowedClasses")))
    {
        ParseClassList(Prop->GetMetaData(TEXT("DisallowedClasses")), DisallowedResolved, DisallowedJson);
    }
    if (Prop->HasMetaData(TEXT("MustImplement")))
    {
        MustImplementName = Prop->GetMetaData(TEXT("MustImplement"));
        MustImplement = MCPSc_FindClass(MustImplementName);
    }
#endif
    Root->SetArrayField(TEXT("allowed_classes"), AllowedJson);
    Root->SetArrayField(TEXT("disallowed_classes"), DisallowedJson);
    Root->SetStringField(TEXT("must_implement"), MustImplementName);

    // Enumerate concrete UClasses assignable here. Bound the result to keep the payload sane.
    const int32 kMax = 750;
    const FString FilterLC = Filter.ToLower();
    TArray<TSharedPtr<FJsonValue>> Valid;
    bool bTruncated = false;
    int32 TotalMatched = 0;
    for (TObjectIterator<UClass> It; It; ++It)
    {
        UClass* C = *It;
        if (!C) { continue; }
        if (!C->IsChildOf(BaseClass)) { continue; }
        if (!bIncludeAbstract && C->HasAnyClassFlags(CLASS_Abstract)) { continue; }
        if (C->HasAnyClassFlags(CLASS_NewerVersionExists | CLASS_Deprecated)) { continue; }

        const FString CName = C->GetName();
        if (CName.StartsWith(TEXT("SKEL_")) || CName.StartsWith(TEXT("REINST_")) ||
            CName.StartsWith(TEXT("TRASHCLASS_")) || CName.StartsWith(TEXT("HOTRELOADED_")))
        {
            continue;
        }
        // Honor AllowedClasses (if any declared): the class must descend from at least one allowed class.
        if (AllowedResolved.Num() > 0)
        {
            bool bAllowed = false;
            for (UClass* A : AllowedResolved) { if (A && C->IsChildOf(A)) { bAllowed = true; break; } }
            if (!bAllowed) { continue; }
        }
        // Honor DisallowedClasses.
        bool bDisallowed = false;
        for (UClass* D : DisallowedResolved) { if (D && C->IsChildOf(D)) { bDisallowed = true; break; } }
        if (bDisallowed) { continue; }
        // Honor MustImplement (interface).
        if (MustImplement && !C->ImplementsInterface(MustImplement)) { continue; }

        if (!FilterLC.IsEmpty() && !CName.ToLower().Contains(FilterLC)) { continue; }

        ++TotalMatched;
        if (Valid.Num() < kMax)
        {
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetStringField(TEXT("class"), CName);
            J->SetStringField(TEXT("path"), C->GetPathName());
            J->SetBoolField(TEXT("is_abstract"), C->HasAnyClassFlags(CLASS_Abstract));
            Valid.Add(MakeShared<FJsonValueObject>(J));
        }
        else
        {
            bTruncated = true;
        }
    }

    Root->SetNumberField(TEXT("valid_concrete_count"), TotalMatched);
    Root->SetBoolField(TEXT("truncated"), bTruncated);
    Root->SetArrayField(TEXT("valid_concrete_classes"), Valid);
    return MCPSc_SerializeJson(Root);
}
