// Clean-room reflection helpers (SEPARATE TU) for the UnrealMCP plugin. See MCPReflectionLibrary.h.
//
// C++ #43 (2026-08-19) — LANDSCAPE / terrain edit-data BRIDGE. Landscape authoring has NO reflected
// (BlueprintCallable) Python surface in UE 5.8: creating a landscape (ALandscapeProxy::Import) and the
// heightmap/weightmap edit-data path (FLandscapeEditDataInterface + FHeightmapAccessor / TAlphamapAccessor)
// are C++-only. This TU is the sanctioned C++ bridge the Python core tools (landscape_write.py) compose,
// doing procedural math HOST-SIDE (like scatter_foliage_in_box). Six handlers:
//   1) CreateLandscapeJson            (WRITE)  spawn ALandscape + ALandscapeProxy::Import(flat|heightfield)
//   2) LandscapeGetInfoJson           (READ)   resolution / section config / scale / height-range / layers
//   3) LandscapeGetHeightRegionJson   (READ)   read a height region -> base64 uint16 (row-major W*H)
//   4) LandscapeSetHeightRegionJson   (WRITE)  write a height region (optionally returns the prior region)
//   5) LandscapePaintWeightRegionJson (WRITE)  paint a weight region for a target layer (self-captures prior)
//   6) ValidateLandscapeJson          (READ)   health check
//
// EXPORT-PATCH VERDICT: NONE NEEDED. Every engine symbol used here is LANDSCAPE_API-exported, or is a
// header-only template that only calls exported methods:
//   * ALandscapeProxy::Import                       LANDSCAPE_API (LandscapeProxy.h:1418)
//   * ALandscapeProxy::GetLandscapeInfo             LANDSCAPE_API (LandscapeProxy.h:1243)
//   * ALandscapeProxy::AddTargetLayer               LANDSCAPE_API (LandscapeProxy.h:1609)
//   * ULandscapeInfo::GetLandscapeExtent/GetLayerInfoByName/GetLandscapeProxy/UpdateLayerInfoMap  LANDSCAPE_API
//   * ULandscapeInfo public members ComponentSizeQuads/SubsectionSizeQuads/ComponentNumSubsections/DrawScale/Layers
//   * FLandscapeEditDataInterface ctor + GetHeightData/SetHeightData/GetWeightData/SetAlphaData/
//     GetComponentsInRegion/Flush                   ALL LANDSCAPE_API (LandscapeEdit.h:128,145,165,187,197,140,98)
//   * FHeightmapAccessor<bUseInterp> / TAlphamapAccessor<bUseInterp>  header-only templates (LandscapeEdit.h),
//     call only the LANDSCAPE_API FLandscapeEditDataInterface methods above -> compile in this TU, link clean.
//   * ULandscapeLayerInfoObject::SetLayerName/GetLayerName   LANDSCAPE_API (LandscapeLayerInfoObject.h:143,135)
//   * FLandscapeImportLayerInfo(FName)              inline WITH_EDITOR ctor (LandscapeProxy.h:212)
//   The editor-only import helper FLandscapeImportHelper::ChooseBestComponentSizeForImport lives in the
//   LandscapeEditor EDITOR module — we AVOID it by doing the section math host-side (Python), so this TU
//   needs ONLY the runtime "Landscape" module (LANDSCAPE_API). Build.cs += "Landscape".
//
// Section math (mirrors FLandscapeImportHelper / New-Landscape tool, done in Python):
//   SubsectionSizeQuads (a.k.a. QuadsPerSection) in {7,15,31,63,127,255}; ComponentNumSubsections
//   (SectionsPerComponent) in {1,2}; QuadsPerComponent = Sections*QuadsPerSection;
//   SizeX = ComponentCountX*QuadsPerComponent + 1 (verts); NumVerts = SizeX*SizeY.
// Height encoding (LandscapeDataAccess.h): uint16 H; LocalZ=(H-32768)/128; WorldZ=LocalZ*ActorScaleZ. Flat=32768.
//
// WIRE FORMAT: height arrays cross as base64 of raw LITTLE-ENDIAN uint16 (row-major W*H); weights as base64
//   uint8 (0..255). Bytes are (de)composed explicitly so it is endian-independent of the host.
//
// Conventions mirrored from sibling TUs (MCPReflection_WorldExt.cpp): file-local anon-namespace helpers
// prefixed MCPLs_ (internal linkage -> no ODR clash); JSON returns {"error": "..."} on any miss; every
// pointer null/bounds-guarded; never crash. Whole batch is editor-only -> bodies #if WITH_EDITOR.
//
// UNDO MODEL: landscape edit-data writes are NOT reliably UE-transaction-undoable (they mutate textures
// directly), so reversibility rides the plugin's capture-prior LEDGER, not the transaction buffer:
//   * create_landscape          -> ledger op "spawn_actor" {actor_name} (REUSES editor_level.undo's existing
//                                  spawn_actor branch -> destroy_actor; NO new undo branch needed).
//   * landscape_set_height_region  -> ledger captures the prior region (base64); inverse re-writes it (NEW branch).
//   * landscape_paint_weight_region-> handler RETURNS prior weights (base64); inverse re-paints them (NEW branch).

#include "MCPReflectionLibrary.h"

#if WITH_EDITOR
#include "Editor.h"                                   // GEditor
#include "Editor/EditorEngine.h"                      // UEditorEngine::GetEditorWorldContext
#include "EngineUtils.h"                              // TActorIterator
#include "Engine/World.h"                             // UWorld

#include "Landscape.h"                                // ALandscape, FLandscapeLayer
#include "LandscapeProxy.h"                           // ALandscapeProxy::Import/AddTargetLayer, FLandscapeImportLayerInfo,
                                                      //   ELandscapeImportAlphamapType, FLandscapeTargetLayerSettings,
                                                      //   ELandscapeLayerPaintingRestriction
#include "LandscapeInfo.h"                            // ULandscapeInfo, FLandscapeInfoLayerSettings
#include "LandscapeEdit.h"                            // FLandscapeEditDataInterface, FHeightmapAccessor, TAlphamapAccessor
#include "LandscapeLayerInfoObject.h"                 // ULandscapeLayerInfoObject
#include "LandscapeDataAccess.h"                      // LANDSCAPE_ZSCALE, LandscapeDataAccess::MidValue

#include "Misc/Base64.h"                              // FBase64
#include "UObject/UObjectGlobals.h"                   // StaticLoadObject
#endif // WITH_EDITOR

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

namespace
{
    // ---- file-local JSON boilerplate (internal linkage; per-TU) -------------------------------
    FString MCPLs_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPLs_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPLs_SerializeJson(Root);
    }

#if WITH_EDITOR
    // Resolve the editor world (the map open in the level editor). Null if no editor world (e.g. cooked).
    UWorld* MCPLs_EditorWorld()
    {
        if (!GEditor)
        {
            return nullptr;
        }
        return GEditor->GetEditorWorldContext().World();
    }

    // Find an ALandscape in the editor world by actor label OR unique object name. Null + OutErr on miss.
    ALandscape* MCPLs_FindLandscape(UWorld* World, const FString& Name, FString& OutErr)
    {
        if (!World)
        {
            OutErr = TEXT("no editor world (open a map in the level editor first)");
            return nullptr;
        }
        ALandscape* ByName = nullptr;
        for (TActorIterator<ALandscape> It(World); It; ++It)
        {
            ALandscape* L = *It;
            if (!L)
            {
                continue;
            }
            if (L->GetActorLabel() == Name)
            {
                return L;
            }
            if (L->GetName() == Name)
            {
                ByName = L;
            }
        }
        if (ByName)
        {
            return ByName;
        }
        OutErr = FString::Printf(TEXT("no ALandscape named '%s' in the editor world"), *Name);
        return nullptr;
    }

    // Parse a JSON [x,y,z] array field into an FVector (component-wise; missing -> Default).
    FVector MCPLs_ParseVecField(const TSharedPtr<FJsonObject>& Obj, const TCHAR* Field, const FVector& Default)
    {
        FVector V = Default;
        const TArray<TSharedPtr<FJsonValue>>* Arr = nullptr;
        if (Obj.IsValid() && Obj->TryGetArrayField(Field, Arr) && Arr && Arr->Num() >= 3)
        {
            V.X = (*Arr)[0]->AsNumber();
            V.Y = (*Arr)[1]->AsNumber();
            V.Z = (*Arr)[2]->AsNumber();
        }
        return V;
    }

    // base64(raw LE uint16) -> TArray<uint16>. Returns false if the source is not valid base64 or odd length.
    bool MCPLs_DecodeU16(const FString& B64, TArray<uint16>& Out, FString& OutErr)
    {
        TArray<uint8> Bytes;
        if (!FBase64::Decode(B64, Bytes))
        {
            OutErr = TEXT("height data is not valid base64");
            return false;
        }
        if ((Bytes.Num() % 2) != 0)
        {
            OutErr = TEXT("height data byte-length is odd (expected uint16 pairs)");
            return false;
        }
        Out.Reset(Bytes.Num() / 2);
        for (int32 i = 0; i + 1 < Bytes.Num(); i += 2)
        {
            const uint16 Lo = Bytes[i];
            const uint16 Hi = Bytes[i + 1];
            Out.Add(static_cast<uint16>(Lo | (Hi << 8)));
        }
        return true;
    }

    // TArray<uint16> -> base64(raw LE uint16).
    FString MCPLs_EncodeU16(const TArray<uint16>& In)
    {
        TArray<uint8> Bytes;
        Bytes.Reserve(In.Num() * 2);
        for (uint16 H : In)
        {
            Bytes.Add(static_cast<uint8>(H & 0xFF));
            Bytes.Add(static_cast<uint8>((H >> 8) & 0xFF));
        }
        return FBase64::Encode(Bytes);
    }

    // base64(raw uint8) -> TArray<uint8>.
    bool MCPLs_DecodeU8(const FString& B64, TArray<uint8>& Out, FString& OutErr)
    {
        if (!FBase64::Decode(B64, Out))
        {
            OutErr = TEXT("weight data is not valid base64");
            return false;
        }
        return true;
    }

    // TArray<uint8> -> base64.
    FString MCPLs_EncodeU8(const TArray<uint8>& In)
    {
        return FBase64::Encode(In);
    }

    // Clamp a requested region [X..X+W-1]x[Y..Y+H-1] to a landscape's valid extent. Returns false (with OutErr)
    // if the clamped region is empty. On success OutX1..OutY2 are inside [MinX..MaxX]x[MinY..MaxY].
    bool MCPLs_ClampRegion(ULandscapeInfo* Info, int32 X, int32 Y, int32 W, int32 H,
                           int32& OutX1, int32& OutY1, int32& OutX2, int32& OutY2, FString& OutErr)
    {
        if (W <= 0 || H <= 0)
        {
            OutErr = TEXT("region width/height must be positive");
            return false;
        }
        int32 MinX = 0, MinY = 0, MaxX = 0, MaxY = 0;
        if (!Info || !Info->GetLandscapeExtent(MinX, MinY, MaxX, MaxY))
        {
            OutErr = TEXT("could not resolve landscape extent (no components?)");
            return false;
        }
        OutX1 = FMath::Clamp(X, MinX, MaxX);
        OutY1 = FMath::Clamp(Y, MinY, MaxY);
        OutX2 = FMath::Clamp(X + W - 1, MinX, MaxX);
        OutY2 = FMath::Clamp(Y + H - 1, MinY, MaxY);
        if (OutX2 < OutX1 || OutY2 < OutY1)
        {
            OutErr = FString::Printf(TEXT("region is entirely outside the landscape extent [%d,%d]-[%d,%d]"), MinX, MinY, MaxX, MaxY);
            return false;
        }
        return true;
    }
#endif // WITH_EDITOR
} // namespace

// ================================================================================================
// 1) CreateLandscapeJson — spawn an ALandscape and Import flat|heightfield data.
//    TransformJson: {"location":[x,y,z],"rotation":[pitch,yaw,roll],"scale":[x,y,z]}  (any field optional).
//    ConfigJson:    {"quads_per_section":63,"sections_per_component":1,"component_count_x":1,"component_count_y":1}
//    HeightDataB64: base64 of SizeX*SizeY LE uint16 (row-major); EMPTY -> flat (all 32768).
//    Reversible: delete the returned actor (Python ledgers op "spawn_actor").
// ================================================================================================
FString UMCPReflectionLibrary::CreateLandscapeJson(const FString& Name, const FString& TransformJson, const FString& ConfigJson, const FString& HeightDataB64)
{
#if WITH_EDITOR
    UWorld* World = MCPLs_EditorWorld();
    if (!World)
    {
        return MCPLs_Error(TEXT("no editor world (open a map in the level editor first)"));
    }

    // --- parse + validate the section config -------------------------------------------------
    TSharedPtr<FJsonObject> Cfg;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ConfigJson);
        if (!FJsonSerializer::Deserialize(Reader, Cfg) || !Cfg.IsValid())
        {
            return MCPLs_Error(TEXT("ConfigJson is not a valid JSON object"));
        }
    }
    double DQuads = 0.0, DSections = 0.0, DCcx = 0.0, DCcy = 0.0;
    Cfg->TryGetNumberField(TEXT("quads_per_section"), DQuads);
    Cfg->TryGetNumberField(TEXT("sections_per_component"), DSections);
    Cfg->TryGetNumberField(TEXT("component_count_x"), DCcx);
    Cfg->TryGetNumberField(TEXT("component_count_y"), DCcy);
    const int32 QuadsPerSection      = static_cast<int32>(DQuads);
    const int32 SectionsPerComponent = static_cast<int32>(DSections);
    const int32 ComponentCountX      = static_cast<int32>(DCcx);
    const int32 ComponentCountY      = static_cast<int32>(DCcy);

    const bool bValidQuads = (QuadsPerSection == 7 || QuadsPerSection == 15 || QuadsPerSection == 31 ||
                              QuadsPerSection == 63 || QuadsPerSection == 127 || QuadsPerSection == 255);
    if (!bValidQuads)
    {
        return MCPLs_Error(TEXT("quads_per_section must be one of 7,15,31,63,127,255"));
    }
    if (SectionsPerComponent != 1 && SectionsPerComponent != 2)
    {
        return MCPLs_Error(TEXT("sections_per_component must be 1 or 2"));
    }
    if (ComponentCountX < 1 || ComponentCountX > 32 || ComponentCountY < 1 || ComponentCountY > 32)
    {
        return MCPLs_Error(TEXT("component_count_x/y must be in 1..32"));
    }

    const int32 QuadsPerComponent = SectionsPerComponent * QuadsPerSection;
    const int32 SizeX = ComponentCountX * QuadsPerComponent + 1;
    const int32 SizeY = ComponentCountY * QuadsPerComponent + 1;
    const int32 NumVerts = SizeX * SizeY;

    // --- height data (flat or supplied) ------------------------------------------------------
    TArray<uint16> Heights;
    bool bFlat = HeightDataB64.IsEmpty();
    if (bFlat)
    {
        Heights.Init(static_cast<uint16>(LandscapeDataAccess::MidValue), NumVerts);
    }
    else
    {
        FString DecErr;
        if (!MCPLs_DecodeU16(HeightDataB64, Heights, DecErr))
        {
            return MCPLs_Error(DecErr);
        }
        if (Heights.Num() != NumVerts)
        {
            return MCPLs_Error(FString::Printf(TEXT("height data has %d samples but the config needs SizeX*SizeY=%d (%dx%d)"), Heights.Num(), NumVerts, SizeX, SizeY));
        }
    }

    // --- transform ---------------------------------------------------------------------------
    TSharedPtr<FJsonObject> Xform;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(TransformJson);
        FJsonSerializer::Deserialize(Reader, Xform); // optional; missing fields default below
    }
    const FVector Location = MCPLs_ParseVecField(Xform, TEXT("location"), FVector::ZeroVector);
    const FVector RotV     = MCPLs_ParseVecField(Xform, TEXT("rotation"), FVector::ZeroVector); // [pitch,yaw,roll]
    const FVector Scale    = MCPLs_ParseVecField(Xform, TEXT("scale"), FVector(100.0, 100.0, 100.0));
    const FRotator Rotation(RotV.X, RotV.Y, RotV.Z);

    // --- spawn + import ----------------------------------------------------------------------
    FActorSpawnParameters SpawnParams;
    SpawnParams.ObjectFlags |= RF_Transactional;
    ALandscape* Landscape = World->SpawnActor<ALandscape>(Location, Rotation, SpawnParams);
    if (!Landscape)
    {
        return MCPLs_Error(TEXT("SpawnActor<ALandscape> returned null"));
    }
    // Scale MUST be set before Import (Import reads GetRootComponent()->GetRelativeScale3D() for normals).
    Landscape->SetActorRelativeScale3D(Scale);

    const FGuid LandscapeGuid = FGuid::NewGuid();
    TMap<FGuid, TArray<uint16>> HeightDataPerLayers;
    HeightDataPerLayers.Add(FGuid(), Heights);              // final/base layer keyed by the zero GUID
    TMap<FGuid, TArray<FLandscapeImportLayerInfo>> MaterialLayerDataPerLayers;
    MaterialLayerDataPerLayers.Add(FGuid(), TArray<FLandscapeImportLayerInfo>()); // no paint layers at create

    // Empty edit-layer view (matches the engine New-Landscape tool call).
    Landscape->Import(LandscapeGuid, 0, 0, SizeX - 1, SizeY - 1, SectionsPerComponent, QuadsPerSection,
                      HeightDataPerLayers, TEXT(""), MaterialLayerDataPerLayers,
                      ELandscapeImportAlphamapType::Additive, TArrayView<const FLandscapeLayer>());

    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (Info)
    {
        Info->UpdateLayerInfoMap(Landscape);
    }
    Landscape->SetActorLabel(Name); // best-effort display label (uniqueness not enforced)

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());   // unique object name (undo/find key)
    Root->SetStringField(TEXT("actor_label"), Landscape->GetActorLabel());
    Root->SetStringField(TEXT("landscape_guid"), LandscapeGuid.ToString());
    Root->SetNumberField(TEXT("size_x"), SizeX);
    Root->SetNumberField(TEXT("size_y"), SizeY);
    Root->SetNumberField(TEXT("num_verts"), NumVerts);
    Root->SetNumberField(TEXT("quads_per_section"), QuadsPerSection);
    Root->SetNumberField(TEXT("sections_per_component"), SectionsPerComponent);
    Root->SetNumberField(TEXT("component_count_x"), ComponentCountX);
    Root->SetNumberField(TEXT("component_count_y"), ComponentCountY);
    Root->SetBoolField(TEXT("flat"), bFlat);
    Root->SetBoolField(TEXT("landscape_info_valid"), Info != nullptr);
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 2) LandscapeGetInfoJson — resolution, section/component config, scale, height range, layer names.
// ================================================================================================
FString UMCPReflectionLibrary::LandscapeGetInfoJson(const FString& ActorName)
{
#if WITH_EDITOR
    FString Err;
    ALandscape* Landscape = MCPLs_FindLandscape(MCPLs_EditorWorld(), ActorName, Err);
    if (!Landscape)
    {
        return MCPLs_Error(Err);
    }
    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (!Info)
    {
        return MCPLs_Error(TEXT("landscape has no ULandscapeInfo (not registered?)"));
    }

    int32 MinX = 0, MinY = 0, MaxX = 0, MaxY = 0;
    const bool bHasExtent = Info->GetLandscapeExtent(MinX, MinY, MaxX, MaxY);
    const int32 SizeX = bHasExtent ? (MaxX - MinX + 1) : 0;
    const int32 SizeY = bHasExtent ? (MaxY - MinY + 1) : 0;

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());
    Root->SetStringField(TEXT("actor_label"), Landscape->GetActorLabel());
    Root->SetBoolField(TEXT("has_extent"), bHasExtent);
    Root->SetNumberField(TEXT("min_x"), MinX);
    Root->SetNumberField(TEXT("min_y"), MinY);
    Root->SetNumberField(TEXT("max_x"), MaxX);
    Root->SetNumberField(TEXT("max_y"), MaxY);
    Root->SetNumberField(TEXT("size_x"), SizeX);
    Root->SetNumberField(TEXT("size_y"), SizeY);
    Root->SetNumberField(TEXT("component_size_quads"), Info->ComponentSizeQuads);
    Root->SetNumberField(TEXT("subsection_size_quads"), Info->SubsectionSizeQuads);
    Root->SetNumberField(TEXT("component_num_subsections"), Info->ComponentNumSubsections);

    const FVector DrawScale = Info->DrawScale;
    TArray<TSharedPtr<FJsonValue>> ScaleArr;
    ScaleArr.Add(MakeShared<FJsonValueNumber>(DrawScale.X));
    ScaleArr.Add(MakeShared<FJsonValueNumber>(DrawScale.Y));
    ScaleArr.Add(MakeShared<FJsonValueNumber>(DrawScale.Z));
    Root->SetArrayField(TEXT("draw_scale"), ScaleArr);

    // Height range over the whole landscape (reads the full region once; skipped if huge to stay cheap).
    if (bHasExtent && SizeX > 0 && SizeY > 0 && (static_cast<int64>(SizeX) * SizeY) <= (4097 * 4097))
    {
        TArray<uint16> Data;
        Data.SetNumZeroed(SizeX * SizeY);
        int32 gx1 = MinX, gy1 = MinY, gx2 = MaxX, gy2 = MaxY;
        {
            FHeightmapAccessor<false> Acc(Info);
            Acc.GetData(gx1, gy1, gx2, gy2, Data.GetData());
        }
        uint16 HMin = 65535, HMax = 0;
        for (uint16 H : Data)
        {
            HMin = FMath::Min(HMin, H);
            HMax = FMath::Max(HMax, H);
        }
        Root->SetNumberField(TEXT("height_min_u16"), HMin);
        Root->SetNumberField(TEXT("height_max_u16"), HMax);
        // Local Z (unscaled) range: (H-32768)/128.
        Root->SetNumberField(TEXT("height_min_local_z"), (static_cast<float>(HMin) - LandscapeDataAccess::MidValue) * LANDSCAPE_ZSCALE);
        Root->SetNumberField(TEXT("height_max_local_z"), (static_cast<float>(HMax) - LandscapeDataAccess::MidValue) * LANDSCAPE_ZSCALE);
    }

    TArray<TSharedPtr<FJsonValue>> LayersArr;
    for (const FLandscapeInfoLayerSettings& L : Info->Layers)
    {
        TSharedRef<FJsonObject> LJ = MakeShared<FJsonObject>();
        LJ->SetStringField(TEXT("name"), L.GetLayerName().ToString());
        LJ->SetBoolField(TEXT("has_layer_info"), L.LayerInfoObj != nullptr);
        if (L.LayerInfoObj)
        {
            LJ->SetStringField(TEXT("layer_info_path"), L.LayerInfoObj->GetPathName());
        }
        LayersArr.Add(MakeShared<FJsonValueObject>(LJ));
    }
    Root->SetArrayField(TEXT("layers"), LayersArr);
    Root->SetNumberField(TEXT("layer_count"), LayersArr.Num());
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 3) LandscapeGetHeightRegionJson — read a height region -> base64 uint16 (row-major, clamped W*H).
// ================================================================================================
FString UMCPReflectionLibrary::LandscapeGetHeightRegionJson(const FString& ActorName, int32 X, int32 Y, int32 W, int32 H)
{
#if WITH_EDITOR
    FString Err;
    ALandscape* Landscape = MCPLs_FindLandscape(MCPLs_EditorWorld(), ActorName, Err);
    if (!Landscape)
    {
        return MCPLs_Error(Err);
    }
    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (!Info)
    {
        return MCPLs_Error(TEXT("landscape has no ULandscapeInfo"));
    }

    int32 X1, Y1, X2, Y2;
    if (!MCPLs_ClampRegion(Info, X, Y, W, H, X1, Y1, X2, Y2, Err))
    {
        return MCPLs_Error(Err);
    }
    const int32 RW = X2 - X1 + 1;
    const int32 RH = Y2 - Y1 + 1;

    TArray<uint16> Data;
    Data.SetNumZeroed(RW * RH);
    int32 gx1 = X1, gy1 = Y1, gx2 = X2, gy2 = Y2;
    {
        FHeightmapAccessor<false> Acc(Info);
        Acc.GetData(gx1, gy1, gx2, gy2, Data.GetData()); // Stride 0 => stride = RW; origin (X1,Y1)
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());
    Root->SetNumberField(TEXT("x"), X1);
    Root->SetNumberField(TEXT("y"), Y1);
    Root->SetNumberField(TEXT("w"), RW);
    Root->SetNumberField(TEXT("h"), RH);
    Root->SetNumberField(TEXT("count"), Data.Num());
    Root->SetStringField(TEXT("heights_b64"), MCPLs_EncodeU16(Data));
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 4) LandscapeSetHeightRegionJson — write a height region (base64 uint16, row-major W*H). If bReturnPrior,
//    the response includes "prev_b64" (the exact region overwritten) for a self-contained ledger inverse.
// ================================================================================================
FString UMCPReflectionLibrary::LandscapeSetHeightRegionJson(const FString& ActorName, int32 X, int32 Y, int32 W, int32 H, const FString& HeightDataB64, bool bReturnPrior)
{
#if WITH_EDITOR
    FString Err;
    ALandscape* Landscape = MCPLs_FindLandscape(MCPLs_EditorWorld(), ActorName, Err);
    if (!Landscape)
    {
        return MCPLs_Error(Err);
    }
    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (!Info)
    {
        return MCPLs_Error(TEXT("landscape has no ULandscapeInfo"));
    }

    int32 X1, Y1, X2, Y2;
    if (!MCPLs_ClampRegion(Info, X, Y, W, H, X1, Y1, X2, Y2, Err))
    {
        return MCPLs_Error(Err);
    }
    const int32 RW = X2 - X1 + 1;
    const int32 RH = Y2 - Y1 + 1;
    const int32 RCount = RW * RH;

    TArray<uint16> NewData;
    if (!MCPLs_DecodeU16(HeightDataB64, NewData, Err))
    {
        return MCPLs_Error(Err);
    }
    // The caller sizes the array to the ORIGINAL W*H. If the region was clamped, reject rather than mis-stride.
    if (NewData.Num() != (W * H))
    {
        return MCPLs_Error(FString::Printf(TEXT("height data has %d samples but W*H=%d"), NewData.Num(), W * H));
    }
    if (RW != W || RH != H)
    {
        return MCPLs_Error(FString::Printf(TEXT("region was clamped to %dx%d (from %dx%d); re-issue with an in-bounds region so the array stride matches"), RW, RH, W, H));
    }

    // Optionally capture the prior region first (same accessor semantics as GetHeightRegion).
    FString PrevB64;
    if (bReturnPrior)
    {
        TArray<uint16> Prev;
        Prev.SetNumZeroed(RCount);
        int32 px1 = X1, py1 = Y1, px2 = X2, py2 = Y2;
        {
            FHeightmapAccessor<false> Acc(Info);
            Acc.GetData(px1, py1, px2, py2, Prev.GetData());
        }
        PrevB64 = MCPLs_EncodeU16(Prev);
    }

    {
        FHeightmapAccessor<false> Acc(Info);
        Acc.SetData(X1, Y1, X2, Y2, NewData.GetData()); // dtor Flush() commits + releases texture locks
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());
    Root->SetNumberField(TEXT("x"), X1);
    Root->SetNumberField(TEXT("y"), Y1);
    Root->SetNumberField(TEXT("w"), RW);
    Root->SetNumberField(TEXT("h"), RH);
    Root->SetNumberField(TEXT("count"), RCount);
    Root->SetBoolField(TEXT("written"), true);
    if (bReturnPrior)
    {
        Root->SetStringField(TEXT("prev_b64"), PrevB64);
    }
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 5) LandscapePaintWeightRegionJson — paint a target-layer weight region (base64 uint8, row-major W*H).
//    Self-captures the prior weights and returns them ("prev_b64") for a self-contained ledger inverse.
//    Layer resolution: by LayerName via ULandscapeInfo::GetLayerInfoByName; if absent AND LayerInfoAssetPath
//    is supplied, that ULandscapeLayerInfoObject asset (created host-side by the Python tool, factory None)
//    is loaded, named, and registered as a target layer via ALandscapeProxy::AddTargetLayer. If the layer is
//    absent and no asset path is given -> error (create the layer info asset first).
// ================================================================================================
FString UMCPReflectionLibrary::LandscapePaintWeightRegionJson(const FString& ActorName, const FString& LayerName, int32 X, int32 Y, int32 W, int32 H, const FString& WeightDataB64, const FString& LayerInfoAssetPath)
{
#if WITH_EDITOR
    FString Err;
    ALandscape* Landscape = MCPLs_FindLandscape(MCPLs_EditorWorld(), ActorName, Err);
    if (!Landscape)
    {
        return MCPLs_Error(Err);
    }
    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (!Info)
    {
        return MCPLs_Error(TEXT("landscape has no ULandscapeInfo"));
    }
    if (LayerName.IsEmpty())
    {
        return MCPLs_Error(TEXT("LayerName is required"));
    }
    const FName LayerFName(*LayerName);

    // Resolve or register the target layer info.
    ULandscapeLayerInfoObject* LayerInfo = Info->GetLayerInfoByName(LayerFName);
    bool bRegisteredNow = false;
    if (!LayerInfo)
    {
        if (LayerInfoAssetPath.IsEmpty())
        {
            return MCPLs_Error(FString::Printf(TEXT("layer '%s' is not a target layer on this landscape; pass LayerInfoAssetPath (create a ULandscapeLayerInfoObject asset first) to register it"), *LayerName));
        }
        UObject* Loaded = StaticLoadObject(ULandscapeLayerInfoObject::StaticClass(), nullptr, *LayerInfoAssetPath);
        LayerInfo = Cast<ULandscapeLayerInfoObject>(Loaded);
        if (!LayerInfo)
        {
            return MCPLs_Error(FString::Printf(TEXT("could not load ULandscapeLayerInfoObject at '%s'"), *LayerInfoAssetPath));
        }
        LayerInfo->SetLayerName(LayerFName, /*bInModify=*/true);
        // Register as a target layer so IsLayerAllowed passes and a weightmap channel can be allocated.
        Landscape->AddTargetLayer(LayerFName, FLandscapeTargetLayerSettings(LayerInfo));
        Info->UpdateLayerInfoMap(Landscape);
        bRegisteredNow = true;
    }

    int32 X1, Y1, X2, Y2;
    if (!MCPLs_ClampRegion(Info, X, Y, W, H, X1, Y1, X2, Y2, Err))
    {
        return MCPLs_Error(Err);
    }
    const int32 RW = X2 - X1 + 1;
    const int32 RH = Y2 - Y1 + 1;
    const int32 RCount = RW * RH;

    TArray<uint8> NewData;
    if (!MCPLs_DecodeU8(WeightDataB64, NewData, Err))
    {
        return MCPLs_Error(Err);
    }
    if (NewData.Num() != (W * H))
    {
        return MCPLs_Error(FString::Printf(TEXT("weight data has %d samples but W*H=%d"), NewData.Num(), W * H));
    }
    if (RW != W || RH != H)
    {
        return MCPLs_Error(FString::Printf(TEXT("region was clamped to %dx%d (from %dx%d); re-issue with an in-bounds region so the array stride matches"), RW, RH, W, H));
    }

    // Capture prior weights, then write. TAlphamapAccessor<false> (no interpolation) mirrors the paint tool.
    TArray<uint8> Prev;
    Prev.SetNumZeroed(RCount);
    {
        TAlphamapAccessor<false> Acc(Info, LayerInfo);
        int32 px1 = X1, py1 = Y1, px2 = X2, py2 = Y2;
        Acc.GetData(px1, py1, px2, py2, Prev.GetData());
    }
    {
        TAlphamapAccessor<false> Acc(Info, LayerInfo);
        Acc.SetData(X1, Y1, X2, Y2, NewData.GetData(), ELandscapeLayerPaintingRestriction::None);
        // dtor Flush() commits + releases texture locks
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());
    Root->SetStringField(TEXT("layer_name"), LayerName);
    Root->SetStringField(TEXT("layer_info_path"), LayerInfo->GetPathName());
    Root->SetBoolField(TEXT("layer_registered_now"), bRegisteredNow);
    Root->SetNumberField(TEXT("x"), X1);
    Root->SetNumberField(TEXT("y"), Y1);
    Root->SetNumberField(TEXT("w"), RW);
    Root->SetNumberField(TEXT("h"), RH);
    Root->SetNumberField(TEXT("count"), RCount);
    Root->SetBoolField(TEXT("written"), true);
    Root->SetStringField(TEXT("prev_b64"), MCPLs_EncodeU8(Prev)); // inverse: re-paint these weights on this region
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}

// ================================================================================================
// 6) ValidateLandscapeJson — READ health check: info valid, extent, section config sanity, layer sanity.
// ================================================================================================
FString UMCPReflectionLibrary::ValidateLandscapeJson(const FString& ActorName)
{
#if WITH_EDITOR
    FString Err;
    ALandscape* Landscape = MCPLs_FindLandscape(MCPLs_EditorWorld(), ActorName, Err);
    if (!Landscape)
    {
        return MCPLs_Error(Err);
    }

    TArray<TSharedPtr<FJsonValue>> Issues;
    TArray<TSharedPtr<FJsonValue>> Warnings;

    ULandscapeInfo* Info = Landscape->GetLandscapeInfo();
    if (!Info)
    {
        Issues.Add(MakeShared<FJsonValueString>(TEXT("landscape has no ULandscapeInfo (not registered)")));
    }

    int32 MinX = 0, MinY = 0, MaxX = 0, MaxY = 0;
    bool bHasExtent = false;
    if (Info)
    {
        bHasExtent = Info->GetLandscapeExtent(MinX, MinY, MaxX, MaxY);
        if (!bHasExtent)
        {
            Issues.Add(MakeShared<FJsonValueString>(TEXT("landscape has no valid extent (no components)")));
        }
        if (Info->ComponentNumSubsections != 1 && Info->ComponentNumSubsections != 2)
        {
            Warnings.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("unusual component_num_subsections=%d"), Info->ComponentNumSubsections)));
        }
        const int32 ss = Info->SubsectionSizeQuads;
        const bool bValidSS = (ss == 7 || ss == 15 || ss == 31 || ss == 63 || ss == 127 || ss == 255);
        if (!bValidSS)
        {
            Warnings.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("unusual subsection_size_quads=%d"), ss)));
        }
        int32 LayersWithoutInfo = 0;
        for (const FLandscapeInfoLayerSettings& L : Info->Layers)
        {
            if (L.LayerInfoObj == nullptr)
            {
                ++LayersWithoutInfo;
            }
        }
        if (LayersWithoutInfo > 0)
        {
            Warnings.Add(MakeShared<FJsonValueString>(FString::Printf(TEXT("%d target layer(s) have no LayerInfo asset (cannot be painted)"), LayersWithoutInfo)));
        }
    }
    if (!Landscape->GetLandscapeGuid().IsValid())
    {
        Warnings.Add(MakeShared<FJsonValueString>(TEXT("landscape GUID is invalid")));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("actor_name"), Landscape->GetName());
    Root->SetStringField(TEXT("actor_label"), Landscape->GetActorLabel());
    Root->SetBoolField(TEXT("landscape_info_valid"), Info != nullptr);
    Root->SetBoolField(TEXT("has_extent"), bHasExtent);
    if (bHasExtent)
    {
        Root->SetNumberField(TEXT("size_x"), MaxX - MinX + 1);
        Root->SetNumberField(TEXT("size_y"), MaxY - MinY + 1);
    }
    Root->SetBoolField(TEXT("valid"), Issues.Num() == 0);
    Root->SetArrayField(TEXT("issues"), Issues);
    Root->SetArrayField(TEXT("warnings"), Warnings);
    return MCPLs_SerializeJson(Root);
#else
    return MCPLs_Error(TEXT("editor-only"));
#endif
}
