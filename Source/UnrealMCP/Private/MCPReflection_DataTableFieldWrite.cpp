// ============================================================================
// MCPReflection_DataTableFieldWrite.cpp — C++ #56. MCP_TOOLING_GAPS (Project_Aimon
// G-02): set a single field on a DataTable ROW by name — primarily an FText column
// the JSON importer can't reach. SELF-CONTAINED translation unit.
//
// WHY: DataTableFunctionLibrary.fill_data_table_from_json_string uses "Name" as the
// ROW-ID key, which collides with a row struct's own `FText Name` column export name,
// so that column imports empty (and FText generally isn't set from a plain string via
// the sequence-nativizing set_object_property path). These set the field directly on
// the row's struct memory by reflection.
//
//   #56 SetDataTableRowText(table, row, field, text)          -> FText field via FText::FromString
//   #56 SetDataTableRowFieldFromText(table, row, field, text) -> ANY field via FProperty::ImportText_Direct
//
// LINK-RISK: DataTable lives in the Engine module (already a dep) -> NO Build.cs change.
// Marks the package dirty + HandleDataTableChanged (rebuilds row caches). Editor-safe.
// ============================================================================
#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/UnrealType.h"
#include "UObject/Class.h"               // TFieldIterator
#include "UObject/TextProperty.h"        // FTextProperty
#include "Engine/DataTable.h"

namespace
{
    FString DTW_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    FString DTW_Error(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("status"), TEXT("error"));
        O->SetStringField(TEXT("error"), Msg);
        return DTW_Serialize(O);
    }

    // Resolve the row struct field by C++ name OR authored/display name (case-insensitive).
    FProperty* DTW_FindField(const UScriptStruct* RowStruct, const FString& FieldName)
    {
        if (!RowStruct) return nullptr;
        for (TFieldIterator<FProperty> It(RowStruct); It; ++It)
        {
            FProperty* P = *It;
            if (P->GetName().Equals(FieldName, ESearchCase::IgnoreCase)
                || P->GetAuthoredName().Equals(FieldName, ESearchCase::IgnoreCase))
            {
                return P;
            }
        }
        return nullptr;
    }

    // Common front-half: validate table, locate the row + field. Returns the row memory (or null + fills Err).
    uint8* DTW_Resolve(UDataTable* Table, const FString& RowName, const FString& FieldName,
                       FProperty*& OutProp, FString& OutErr)
    {
        OutProp = nullptr;
        if (!Table) { OutErr = TEXT("null DataTable"); return nullptr; }
        const UScriptStruct* RowStruct = Table->GetRowStruct();
        if (!RowStruct) { OutErr = TEXT("DataTable has no RowStruct"); return nullptr; }
        uint8* RowData = Table->FindRowUnchecked(FName(*RowName));
        if (!RowData)
        {
            OutErr = FString::Printf(TEXT("no row '%s' in %s"), *RowName, *Table->GetName());
            return nullptr;
        }
        OutProp = DTW_FindField(RowStruct, FieldName);
        if (!OutProp)
        {
            OutErr = FString::Printf(TEXT("no field '%s' on row struct %s"), *FieldName, *RowStruct->GetName());
            return nullptr;
        }
        return RowData;
    }

    void DTW_Persist(UDataTable* Table, const FName RowName)
    {
        Table->Modify();
        Table->HandleDataTableChanged(RowName);   // rebuilds any dependent caches
        Table->MarkPackageDirty();
    }
}

// ---- #56  set_data_table_row_text (FText field via FText::FromString) -----------
FString UMCPReflectionLibrary::SetDataTableRowText(UDataTable* Table, const FString& RowName,
                                                   const FString& FieldName, const FString& Text)
{
    FProperty* Prop = nullptr; FString Err;
    uint8* RowData = DTW_Resolve(Table, RowName, FieldName, Prop, Err);
    if (!RowData) return DTW_Error(Err);

    FTextProperty* TP = CastField<FTextProperty>(Prop);
    if (!TP)
    {
        return DTW_Error(FString::Printf(TEXT("field '%s' is not an FText (it is %s) — use set_data_table_row_field for other types"),
            *FieldName, *Prop->GetClass()->GetName()));
    }

    FText* Dest = TP->ContainerPtrToValuePtr<FText>(RowData);
    *Dest = FText::FromString(Text);
    DTW_Persist(Table, FName(*RowName));

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("table"), Table->GetPathName());
    O->SetStringField(TEXT("row"), RowName);
    O->SetStringField(TEXT("field"), FieldName);
    O->SetStringField(TEXT("text"), Text);
    return DTW_Serialize(O);
}

// ---- #56  set_data_table_row_field (ANY field via ImportText_Direct) ------------
FString UMCPReflectionLibrary::SetDataTableRowFieldFromText(UDataTable* Table, const FString& RowName,
                                                            const FString& FieldName, const FString& ValueText)
{
    FProperty* Prop = nullptr; FString Err;
    uint8* RowData = DTW_Resolve(Table, RowName, FieldName, Prop, Err);
    if (!RowData) return DTW_Error(Err);

    void* Dest = Prop->ContainerPtrToValuePtr<void>(RowData);
    // ImportText_Direct parses ValueText into the field (e.g. "5.0" for a float, "NSLOCTEXT(...)" or a
    // quoted string for FText, "(X=1,Y=2)" for a struct). Returns nullptr on a parse failure.
    const TCHAR* Result = Prop->ImportText_Direct(*ValueText, Dest, Table, PPF_None);
    if (Result == nullptr)
    {
        return DTW_Error(FString::Printf(TEXT("could not parse '%s' into field '%s' (%s)"),
            *ValueText, *FieldName, *Prop->GetClass()->GetName()));
    }
    DTW_Persist(Table, FName(*RowName));

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("status"), TEXT("ok"));
    O->SetStringField(TEXT("table"), Table->GetPathName());
    O->SetStringField(TEXT("row"), RowName);
    O->SetStringField(TEXT("field"), FieldName);
    O->SetStringField(TEXT("cpp_type"), Prop->GetCPPType());
    O->SetStringField(TEXT("value"), ValueText);
    return DTW_Serialize(O);
}
