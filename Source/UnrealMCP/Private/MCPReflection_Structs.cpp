// UnrealMCP — UserDefinedStruct member AUTHORING handlers (C++ #17 2026-08-17).
//
// Companion translation unit to MCPReflectionLibrary.cpp. Defines five additional
// UMCPReflectionLibrary member functions declared in MCPReflectionLibrary.h that let the
// plugin FULLY edit UserDefinedStruct members (rename / retype / default / tooltip) and add
// COMPLEX-typed members (object/class/soft/struct/enum + array|set|map containers). This drives
// the `structs` command category to 100%.
//
// Reuses the SAME machinery AddStructField/RemoveStructField already use:
//   FStructureEditorUtils (UnrealEd module) + FEdGraphPinType (BlueprintGraph module).
// Both modules are ALREADY Build.cs deps (AddStructField needed them) -> NO Build.cs change.
//
// The scalar-only BuildPinType() in MCPReflectionLibrary.cpp lives in that file's anonymous
// namespace (TU-local, not visible here), so this file carries its OWN static pin-type builder
// that is a strict SUPERSET: same scalar coverage + object/class/soft/struct/enum + containers.
// Everything version-sensitive verified against UE 5.8 source (StructureEditorUtils.h,
// UserDefinedStructEditorData.h, EdGraphPin.h, EdGraphNode.h, EdGraphSchema_K2.h).

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "UObject/Class.h"                 // UScriptStruct / UEnum / TBaseStructure / UClass::TryFindTypeSlow
#include "UObject/UObjectGlobals.h"        // LoadObject / FindObject

#include "StructUtils/UserDefinedStruct.h" // UUserDefinedStruct (CoreUObject StructUtils in this build)
#include "Kismet2/StructureEditorUtils.h"  // FStructureEditorUtils (UnrealEd)
#include "UserDefinedStructure/UserDefinedStructEditorData.h" // FStructVariableDescription full def
#include "EdGraph/EdGraphPin.h"            // FEdGraphPinType / EPinContainerType
#include "EdGraph/EdGraphNode.h"           // FEdGraphTerminalType
#include "EdGraphSchema_K2.h"              // UEdGraphSchema_K2::PC_* (BlueprintGraph)

namespace
{
    // ---- small JSON helpers (TU-local; mirror MCPReflectionLibrary.cpp's) ------------------
    FString MCPStructs_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    // {"status":"error","message":...}
    FString ErrStatus(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("message"), Message);
        return MCPStructs_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> OkStatus()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

    // ---- type resolution -------------------------------------------------------------------
    UClass* ResolveClass(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UClass* C = LoadObject<UClass>(nullptr, *Path))
        {
            return C;
        }
        if (UClass* C = FindObject<UClass>(nullptr, *Path))
        {
            return C;
        }
        return UClass::TryFindTypeSlow<UClass>(Path); // bare short name fallback
    }

    UScriptStruct* ResolveStruct(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UScriptStruct* S = LoadObject<UScriptStruct>(nullptr, *Path))
        {
            return S;
        }
        if (UScriptStruct* S = FindObject<UScriptStruct>(nullptr, *Path))
        {
            return S;
        }
        return UClass::TryFindTypeSlow<UScriptStruct>(Path);
    }

    UEnum* ResolveEnum(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UEnum* E = LoadObject<UEnum>(nullptr, *Path))
        {
            return E;
        }
        if (UEnum* E = FindObject<UEnum>(nullptr, *Path))
        {
            return E;
        }
        return UClass::TryFindTypeSlow<UEnum>(Path);
    }

    // Resolve a single (category, type_path) spec into the primary triple of an FEdGraphPinType
    // (or the terminal triple of a map value). Covers the SAME scalars as the scalar-only
    // BuildPinType plus object/class/softobject/softclass/interface/struct/enum. Returns false
    // (with OutErr) on an unresolved path or an unknown category.
    bool ApplyTypeSpec(const FString& CategoryRaw, const FString& TypePath,
                       FName& OutCat, FName& OutSub, UObject*& OutSubObj, FString& OutErr)
    {
        const FString L = CategoryRaw.ToLower();
        OutCat = NAME_None;
        OutSub = NAME_None;
        OutSubObj = nullptr;

        // --- scalars (superset of the existing scalar BuildPinType) ---
        if (L == TEXT("bool") || L == TEXT("boolean")) { OutCat = UEdGraphSchema_K2::PC_Boolean; return true; }
        if (L == TEXT("byte"))                          { OutCat = UEdGraphSchema_K2::PC_Byte; return true; }
        if (L == TEXT("int") || L == TEXT("int32") || L == TEXT("integer")) { OutCat = UEdGraphSchema_K2::PC_Int; return true; }
        if (L == TEXT("int64"))                         { OutCat = UEdGraphSchema_K2::PC_Int64; return true; }
        if (L == TEXT("float") || L == TEXT("double") || L == TEXT("real"))
        {
            OutCat = UEdGraphSchema_K2::PC_Real;
            OutSub = UEdGraphSchema_K2::PC_Double;
            return true;
        }
        if (L == TEXT("name"))   { OutCat = UEdGraphSchema_K2::PC_Name; return true; }
        if (L == TEXT("string")) { OutCat = UEdGraphSchema_K2::PC_String; return true; }
        if (L == TEXT("text"))   { OutCat = UEdGraphSchema_K2::PC_Text; return true; }

        // --- convenience common structs (name-only, no type_path required) ---
        auto SetStruct = [&](UScriptStruct* SS) -> bool
        {
            if (!SS)
            {
                OutErr = TEXT("could not resolve struct type");
                return false;
            }
            OutCat = UEdGraphSchema_K2::PC_Struct;
            OutSubObj = SS;
            return true;
        };
        if (L == TEXT("vector") || L == TEXT("vector3")) { return SetStruct(TBaseStructure<FVector>::Get()); }
        if (L == TEXT("vector2d") || L == TEXT("vector2")) { return SetStruct(TBaseStructure<FVector2D>::Get()); }
        if (L == TEXT("rotator"))    { return SetStruct(TBaseStructure<FRotator>::Get()); }
        if (L == TEXT("transform"))  { return SetStruct(TBaseStructure<FTransform>::Get()); }
        if (L == TEXT("quat"))       { return SetStruct(TBaseStructure<FQuat>::Get()); }
        if (L == TEXT("linearcolor") || L == TEXT("color")) { return SetStruct(TBaseStructure<FLinearColor>::Get()); }

        // --- complex, type_path-driven ---
        if (L == TEXT("struct"))
        {
            UScriptStruct* SS = ResolveStruct(TypePath);
            if (!SS)
            {
                OutErr = FString::Printf(TEXT("could not resolve struct '%s'"), *TypePath);
                return false;
            }
            return SetStruct(SS);
        }
        if (L == TEXT("enum"))
        {
            UEnum* En = ResolveEnum(TypePath);
            if (!En)
            {
                OutErr = FString::Printf(TEXT("could not resolve enum '%s'"), *TypePath);
                return false;
            }
            // Byte-backed enum member (the K2 schema maps PC_Enum -> PC_Byte for member vars).
            OutCat = UEdGraphSchema_K2::PC_Byte;
            OutSubObj = En;
            return true;
        }
        if (L == TEXT("object") || L == TEXT("class") || L == TEXT("softobject") ||
            L == TEXT("softclass") || L == TEXT("interface"))
        {
            UClass* C = TypePath.IsEmpty() ? UObject::StaticClass() : ResolveClass(TypePath);
            if (!C)
            {
                OutErr = FString::Printf(TEXT("could not resolve class '%s'"), *TypePath);
                return false;
            }
            if (L == TEXT("object"))          { OutCat = UEdGraphSchema_K2::PC_Object; }
            else if (L == TEXT("class"))      { OutCat = UEdGraphSchema_K2::PC_Class; }
            else if (L == TEXT("softobject")) { OutCat = UEdGraphSchema_K2::PC_SoftObject; }
            else if (L == TEXT("softclass"))  { OutCat = UEdGraphSchema_K2::PC_SoftClass; }
            else                              { OutCat = UEdGraphSchema_K2::PC_Interface; }
            OutSubObj = C;
            return true;
        }

        OutErr = FString::Printf(TEXT("unsupported type category '%s'"), *CategoryRaw);
        return false;
    }

    // Build a full FEdGraphPinType from TypeJson. TypeJson is either:
    //   * a bare scalar token (e.g. "int", "vector"), OR
    //   * a JSON object: { "category": "...", "type_path"?: "...", "container"?: none|array|set|map,
    //                      "value"?: { "category": "...", "type_path"?: "..." } }   (value = map value type)
    // Also accepts aliases: "type" for "category", "path" for "type_path", and boolean
    // is_array/is_set/is_map instead of "container".
    bool BuildComplexPinType(const FString& TypeJson, FEdGraphPinType& Out, FString& OutErr)
    {
        Out = FEdGraphPinType();
        Out.ContainerType = EPinContainerType::None;

        FString Category, TypePath, Container;
        TSharedPtr<FJsonObject> ValueObj;

        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(TypeJson);
        if (FJsonSerializer::Deserialize(Reader, Obj) && Obj.IsValid())
        {
            Obj->TryGetStringField(TEXT("category"), Category);
            if (Category.IsEmpty()) { Obj->TryGetStringField(TEXT("type"), Category); }
            Obj->TryGetStringField(TEXT("type_path"), TypePath);
            if (TypePath.IsEmpty()) { Obj->TryGetStringField(TEXT("path"), TypePath); }
            Obj->TryGetStringField(TEXT("container"), Container);

            const TSharedPtr<FJsonObject>* VObjPtr = nullptr;
            if (Obj->TryGetObjectField(TEXT("value"), VObjPtr) && VObjPtr)
            {
                ValueObj = *VObjPtr;
            }

            if (Container.IsEmpty())
            {
                bool b = false;
                if (Obj->TryGetBoolField(TEXT("is_map"), b) && b)       { Container = TEXT("map"); }
                else if (Obj->TryGetBoolField(TEXT("is_set"), b) && b)  { Container = TEXT("set"); }
                else if (Obj->TryGetBoolField(TEXT("is_array"), b) && b) { Container = TEXT("array"); }
            }
        }
        else
        {
            // Not a JSON object — treat the raw text as a scalar/complex category token.
            Category = TypeJson.TrimStartAndEnd().TrimQuotes();
        }

        if (Category.IsEmpty())
        {
            OutErr = TEXT("type spec missing 'category'");
            return false;
        }

        FName Cat, Sub;
        UObject* SubObj = nullptr;
        if (!ApplyTypeSpec(Category, TypePath, Cat, Sub, SubObj, OutErr))
        {
            return false;
        }
        Out.PinCategory = Cat;
        Out.PinSubCategory = Sub;
        Out.PinSubCategoryObject = SubObj;

        const FString CL = Container.ToLower();
        if (CL == TEXT("array")) { Out.ContainerType = EPinContainerType::Array; }
        else if (CL == TEXT("set")) { Out.ContainerType = EPinContainerType::Set; }
        else if (CL == TEXT("map"))
        {
            Out.ContainerType = EPinContainerType::Map;
            FString VCat, VPath;
            if (ValueObj.IsValid())
            {
                ValueObj->TryGetStringField(TEXT("category"), VCat);
                if (VCat.IsEmpty()) { ValueObj->TryGetStringField(TEXT("type"), VCat); }
                ValueObj->TryGetStringField(TEXT("type_path"), VPath);
                if (VPath.IsEmpty()) { ValueObj->TryGetStringField(TEXT("path"), VPath); }
            }
            if (VCat.IsEmpty())
            {
                OutErr = TEXT("map type requires a 'value' type spec");
                return false;
            }
            FName VC, VS;
            UObject* VSObj = nullptr;
            if (!ApplyTypeSpec(VCat, VPath, VC, VS, VSObj, OutErr))
            {
                return false;
            }
            Out.PinValueType.TerminalCategory = VC;
            Out.PinValueType.TerminalSubCategory = VS;
            Out.PinValueType.TerminalSubCategoryObject = VSObj;
        }
        else if (!CL.IsEmpty() && CL != TEXT("none"))
        {
            OutErr = FString::Printf(TEXT("unsupported container '%s'"), *Container);
            return false;
        }
        return true;
    }

#if WITH_EDITOR
    // Locate a member's FGuid by friendly name OR internal VarName (matches AddStructField's idiom).
    bool FindFieldGuid(UUserDefinedStruct* Struct, const FString& FieldName, FGuid& OutGuid)
    {
        for (const FStructVariableDescription& D : FStructureEditorUtils::GetVarDesc(Struct))
        {
            if (D.FriendlyName == FieldName || D.VarName == FieldName)
            {
                OutGuid = D.VarGuid;
                return true;
            }
        }
        return false;
    }

    // Serialize one member's TYPE as a TypeJson-shaped object (for undo capture / reporting).
    TSharedRef<FJsonObject> DescribeVarType(const FStructVariableDescription& D)
    {
        TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
        J->SetStringField(TEXT("category"), D.Category.ToString());
        if (!D.SubCategory.IsNone())
        {
            J->SetStringField(TEXT("sub_category"), D.SubCategory.ToString());
        }
        if (!D.SubCategoryObject.IsNull())
        {
            J->SetStringField(TEXT("type_path"), D.SubCategoryObject.ToString());
        }
        const TCHAR* Cont =
            D.ContainerType == EPinContainerType::Array ? TEXT("array") :
            D.ContainerType == EPinContainerType::Set   ? TEXT("set")   :
            D.ContainerType == EPinContainerType::Map   ? TEXT("map")   : TEXT("none");
        J->SetStringField(TEXT("container"), Cont);
        if (D.ContainerType == EPinContainerType::Map)
        {
            TSharedRef<FJsonObject> V = MakeShared<FJsonObject>();
            V->SetStringField(TEXT("category"), D.PinValueType.TerminalCategory.ToString());
            if (UObject* O = D.PinValueType.TerminalSubCategoryObject.Get())
            {
                V->SetStringField(TEXT("type_path"), O->GetPathName());
            }
            J->SetObjectField(TEXT("value"), V);
        }
        return J;
    }

    // Append the struct's current member list (friendly name, internal name, guid, category, container).
    void AppendFieldList(UUserDefinedStruct* Struct, const TSharedRef<FJsonObject>& Root)
    {
        TArray<TSharedPtr<FJsonValue>> Fields;
        for (const FStructVariableDescription& D : FStructureEditorUtils::GetVarDesc(Struct))
        {
            TSharedRef<FJsonObject> F = MakeShared<FJsonObject>();
            F->SetStringField(TEXT("name"), D.FriendlyName);
            F->SetStringField(TEXT("var_name"), D.VarName.ToString());
            F->SetStringField(TEXT("guid"), D.VarGuid.ToString());
            F->SetStringField(TEXT("category"), D.Category.ToString());
            const TCHAR* Cont =
                D.ContainerType == EPinContainerType::Array ? TEXT("array") :
                D.ContainerType == EPinContainerType::Set   ? TEXT("set")   :
                D.ContainerType == EPinContainerType::Map   ? TEXT("map")   : TEXT("none");
            F->SetStringField(TEXT("container"), Cont);
            Fields.Add(MakeShared<FJsonValueObject>(F));
        }
        Root->SetArrayField(TEXT("fields"), Fields);
        Root->SetNumberField(TEXT("field_count"), Fields.Num());
    }
#endif // WITH_EDITOR
}

// ============================================================================================
// 1) Rename a member.
// ============================================================================================
FString UMCPReflectionLibrary::RenameStructField(UUserDefinedStruct* Struct, const FString& FieldName, const FString& NewName)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return ErrStatus(TEXT("null struct"));
    }
    if (NewName.IsEmpty())
    {
        return ErrStatus(TEXT("empty new name"));
    }
    FGuid Guid;
    if (!FindFieldGuid(Struct, FieldName, Guid))
    {
        return ErrStatus(FString::Printf(TEXT("no field named '%s'"), *FieldName));
    }

    const FString OldFriendly = FStructureEditorUtils::GetVariableFriendlyName(Struct, Guid);
    const bool bOk = FStructureEditorUtils::RenameVariable(Struct, Guid, NewName);
    if (!bOk)
    {
        return ErrStatus(FString::Printf(TEXT("RenameVariable failed (name '%s' may be duplicate/invalid)"), *NewName));
    }
    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = OkStatus();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("guid"), Guid.ToString());
    Root->SetStringField(TEXT("old_name"), OldFriendly);
    Root->SetStringField(TEXT("new_name"), NewName);
    Root->SetBoolField(TEXT("renamed"), true);
    AppendFieldList(Struct, Root);
    return MCPStructs_SerializeJson(Root);
#else
    return ErrStatus(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 2) Change a member's TYPE (scalar / complex / container) from TypeJson.
// ============================================================================================
FString UMCPReflectionLibrary::ChangeStructFieldType(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeJson)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return ErrStatus(TEXT("null struct"));
    }
    FGuid Guid;
    if (!FindFieldGuid(Struct, FieldName, Guid))
    {
        return ErrStatus(FString::Printf(TEXT("no field named '%s'"), *FieldName));
    }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!BuildComplexPinType(TypeJson, PinType, BuildErr))
    {
        return ErrStatus(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }

    // Pre-validate for a clean message (ChangeVariableType would otherwise just return false).
    FString ValidateMsg;
    if (!FStructureEditorUtils::CanHaveAMemberVariableOfType(Struct, PinType, &ValidateMsg))
    {
        return ErrStatus(FString::Printf(TEXT("type not allowed: %s"), *ValidateMsg));
    }

    // Capture prior type (for undo) BEFORE the change.
    TSharedRef<FJsonObject> PrevType = MakeShared<FJsonObject>();
    if (const FStructVariableDescription* D = FStructureEditorUtils::GetVarDescByGuid(Struct, Guid))
    {
        PrevType = DescribeVarType(*D);
    }

    const bool bOk = FStructureEditorUtils::ChangeVariableType(Struct, Guid, PinType);
    if (!bOk)
    {
        return ErrStatus(TEXT("ChangeVariableType failed"));
    }
    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = OkStatus();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("field"), FieldName);
    Root->SetStringField(TEXT("guid"), Guid.ToString());
    Root->SetBoolField(TEXT("type_changed"), true);
    Root->SetObjectField(TEXT("prev_type"), PrevType);
    if (const FStructVariableDescription* D = FStructureEditorUtils::GetVarDescByGuid(Struct, Guid))
    {
        Root->SetObjectField(TEXT("new_type"), DescribeVarType(*D));
    }
    AppendFieldList(Struct, Root);
    return MCPStructs_SerializeJson(Root);
#else
    return ErrStatus(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 3) Set a member's DEFAULT value.
// ============================================================================================
FString UMCPReflectionLibrary::SetStructFieldDefault(UUserDefinedStruct* Struct, const FString& FieldName, const FString& DefaultValue)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return ErrStatus(TEXT("null struct"));
    }
    FGuid Guid;
    if (!FindFieldGuid(Struct, FieldName, Guid))
    {
        return ErrStatus(FString::Printf(TEXT("no field named '%s'"), *FieldName));
    }

    // Capture prior default (for undo).
    FString PrevDefault;
    if (const FStructVariableDescription* D = FStructureEditorUtils::GetVarDescByGuid(Struct, Guid))
    {
        PrevDefault = D->DefaultValue;
    }

    const bool bOk = FStructureEditorUtils::ChangeVariableDefaultValue(Struct, Guid, DefaultValue);
    if (!bOk)
    {
        return ErrStatus(FString::Printf(TEXT("ChangeVariableDefaultValue failed (value '%s' invalid for this type)"), *DefaultValue));
    }
    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = OkStatus();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("field"), FieldName);
    Root->SetStringField(TEXT("guid"), Guid.ToString());
    Root->SetStringField(TEXT("new_default"), DefaultValue);
    Root->SetStringField(TEXT("prev_default"), PrevDefault);
    Root->SetBoolField(TEXT("default_set"), true);
    return MCPStructs_SerializeJson(Root);
#else
    return ErrStatus(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 4) Set a member's TOOLTIP.
// ============================================================================================
FString UMCPReflectionLibrary::SetStructFieldTooltip(UUserDefinedStruct* Struct, const FString& FieldName, const FString& Tooltip)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return ErrStatus(TEXT("null struct"));
    }
    FGuid Guid;
    if (!FindFieldGuid(Struct, FieldName, Guid))
    {
        return ErrStatus(FString::Printf(TEXT("no field named '%s'"), *FieldName));
    }

    // Capture prior tooltip (for undo).
    const FString PrevTooltip = FStructureEditorUtils::GetVariableTooltip(Struct, Guid);

    const bool bOk = FStructureEditorUtils::ChangeVariableTooltip(Struct, Guid, Tooltip);
    if (!bOk)
    {
        return ErrStatus(TEXT("ChangeVariableTooltip failed"));
    }
    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = OkStatus();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("field"), FieldName);
    Root->SetStringField(TEXT("guid"), Guid.ToString());
    Root->SetStringField(TEXT("new_tooltip"), Tooltip);
    Root->SetStringField(TEXT("prev_tooltip"), PrevTooltip);
    Root->SetBoolField(TEXT("tooltip_set"), true);
    return MCPStructs_SerializeJson(Root);
#else
    return ErrStatus(TEXT("editor-only"));
#endif
}

// ============================================================================================
// 5) Add a member with a COMPLEX type (supersedes the scalar-only AddStructField).
// ============================================================================================
FString UMCPReflectionLibrary::AddStructFieldEx(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeJson)
{
#if WITH_EDITOR
    if (!Struct)
    {
        return ErrStatus(TEXT("null struct"));
    }

    FEdGraphPinType PinType;
    FString BuildErr;
    if (!BuildComplexPinType(TypeJson, PinType, BuildErr))
    {
        return ErrStatus(FString::Printf(TEXT("bad type spec: %s"), *BuildErr));
    }

    FString ValidateMsg;
    if (!FStructureEditorUtils::CanHaveAMemberVariableOfType(Struct, PinType, &ValidateMsg))
    {
        return ErrStatus(FString::Printf(TEXT("type not allowed: %s"), *ValidateMsg));
    }

    const int32 Before = FStructureEditorUtils::GetVarDesc(Struct).Num();
    if (!FStructureEditorUtils::AddVariable(Struct, PinType))
    {
        return ErrStatus(TEXT("AddVariable failed"));
    }

    // The newly-added variable is the last descriptor; rename it to the requested friendly name.
    FString GuidStr;
    FGuid NewGuid;
    TArray<FStructVariableDescription>& Desc = FStructureEditorUtils::GetVarDesc(Struct);
    if (Desc.Num() > Before)
    {
        NewGuid = Desc.Last().VarGuid;
        if (!FieldName.IsEmpty())
        {
            FStructureEditorUtils::RenameVariable(Struct, NewGuid, FieldName);
        }
        GuidStr = NewGuid.ToString();
    }

    Struct->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = OkStatus();
    Root->SetStringField(TEXT("struct"), Struct->GetName());
    Root->SetStringField(TEXT("added_field"), FieldName);
    Root->SetStringField(TEXT("guid"), GuidStr);
    if (NewGuid.IsValid())
    {
        if (const FStructVariableDescription* D = FStructureEditorUtils::GetVarDescByGuid(Struct, NewGuid))
        {
            Root->SetObjectField(TEXT("added_type"), DescribeVarType(*D));
        }
    }
    AppendFieldList(Struct, Root);
    return MCPStructs_SerializeJson(Root);
#else
    return ErrStatus(TEXT("editor-only"));
#endif
}
