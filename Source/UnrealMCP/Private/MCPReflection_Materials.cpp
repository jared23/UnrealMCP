// ============================================================================
// MCPReflection_Materials.cpp  —  MATERIALS "M5" C++ round. The three material
//   features that have NO Python-reflected surface, so they need C++:
//     1. set_material_layers  — set the Material Attribute Layers stack on a
//        MaterialInstanceConstant (UMaterialInstance::SetMaterialLayers is
//        ENGINE_API but NOT a UFUNCTION -> unreachable from Python).
//     2. get_material_layers  — read that stack back.
//     3. add_material_comment — add a UMaterialExpressionComment to a base
//        Material's graph (it lives in the material's expression collection
//        EditorComments, NOT the expressions list, and
//        MaterialEditingLibrary.create_material_expression refuses it).
//   + 4. remove_material_comment — the FAITHFUL INVERSE of #3 (removing a
//        comment is likewise not Python-reachable: EditorComments is protected
//        editor-only data). Added so add_material_comment is reversible; the
//        3 features asked for are still #1-#3. See FINAL REPORT.
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** on purpose:
// implements the DEFERRED MCPReflectionLibrary methods that materials_write3.py /
// materials_layers.py hasattr-guard on. When these link, the Python tools auto-enable.
//
// >>> LINK-RISK DISCIPLINE <<<
//   Every symbol referenced here is `ENGINE_API` in the stock UE 5.8 headers (or a
//   plain public member / inherited UObject method), so this file links against the
//   already-linked Engine module with NO Build.cs change and NO engine export patch.
//
//   Confirmed 5.8 signatures (header:line verified against the source engine):
//     UMaterialInstance::SetMaterialLayers(const FMaterialLayersFunctions&) ENGINE_API #if WITH_EDITOR
//         Public/Materials/MaterialInstance.h:952  -> bool (true if the stack actually changed)
//     UMaterialInstance::GetMaterialLayers(FMaterialLayersFunctions&, TMicRecursionGuard=default) const
//         ENGINE_API virtual                          Public/Materials/MaterialInstance.h:930 -> bool
//     UMaterialInstance::UpdateStaticPermutation(FMaterialUpdateContext*=nullptr) ENGINE_API #if WITH_EDITOR
//         Public/Materials/MaterialInstance.h:1068  (forces a full static-permutation recompile)
//     UMaterialInstance::UpdateParameterNames() ENGINE_API #if WITH_EDITOR  MaterialInstance.h:1249
//     FMaterialLayersFunctions::AddDefaultBackgroundLayer() ENGINE_API      MaterialLayersFunctions.h:251
//     FMaterialLayersFunctions::AppendBlendedLayer() ENGINE_API -> int32    MaterialLayersFunctions.h:253
//       (both keep the runtime Layers/Blends arrays AND the seven EditorOnly arrays in lock-step —
//        MaterialExpressions.cpp:15227/15241 — so we never hand-sync the editor-only arrays, which
//        is the fragile part; we just overwrite the Layers[i]/Blends[i-1] object slots afterward.)
//       Invariant produced: Layers.Num()==N, Blends.Num()==N-1, EditorOnly.LayerStates/LayerNames/
//       LayerGuids/LayerLinkStates.Num()==N, EditorOnly.RestrictToBlendRelatives.Num()==N-1.
//     FMaterialExpressionCollection::AddComment(UMaterialExpressionComment*) ENGINE_API    MaterialExpression.h:131
//     FMaterialExpressionCollection::RemoveComment(UMaterialExpressionComment*) ENGINE_API MaterialExpression.h:132
//     FMaterialExpressionCollection::EditorComments (public UPROPERTY TArray<TObjectPtr<...>>) MaterialExpression.h:141
//     UMaterial::GetExpressionCollection() ENGINE_API -> FMaterialExpressionCollection&    Material.h:1485
//     UMaterialExpressionComment: Text / CommentColor / SizeX / SizeY (public UPROPERTY)   MaterialExpressionComment.h
//     UMaterialExpression::MaterialExpressionEditorX / ...EditorY (public UPROPERTY)        MaterialExpression.h:159/162
//   NewObject<UMaterialExpressionComment>(Material, NAME_None, RF_Transactional) mirrors the editor's
//   own FMaterialEditor::CreateNewMaterialExpressionComment (MaterialEditor.cpp:6074) minus the live
//   graph-editor node creation (headless: the MaterialGraph is rebuilt from EditorComments on next open —
//   MaterialGraph.cpp:66/259 iterates Material->GetEditorComments()).
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any miss (never crash).
// GetMaterialLayers is non-mutating. The three writes mark the package dirty + recompile/PostEditChange.
// Anon-namespace helpers are prefixed MCPMat_ (unique across the unity build).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"

// --- Core / reflection ---
#include "UObject/SoftObjectPath.h"
#include "UObject/Package.h"
#include "Misc/PackageName.h"
#include "Math/Color.h"

// --- Materials (Engine module — always linkable; ENGINE_API) ---
#include "Materials/MaterialInstance.h"          // UMaterialInstance::Set/GetMaterialLayers
#include "Materials/MaterialInstanceConstant.h"  // UMaterialInstanceConstant
#include "Materials/MaterialLayersFunctions.h"   // FMaterialLayersFunctions
#include "Materials/MaterialFunctionInterface.h" // UMaterialFunctionInterface (Layers/Blends element type)
#include "Materials/Material.h"                   // UMaterial::GetExpressionCollection
#include "Materials/MaterialExpression.h"         // FMaterialExpressionCollection
#include "Materials/MaterialExpressionComment.h"  // UMaterialExpressionComment

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPMat_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPMat_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPMat_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UObject asset from a package/asset path (Python callers pass paths as STRINGS).
    UObject* MCPMat_LoadObject(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        if (UObject* Obj = FSoftObjectPath(Path).TryLoad())
        {
            return Obj;
        }
        // Tolerate a package-only path ("/Game/Foo") by appending ".Foo".
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return FSoftObjectPath(ObjPath).TryLoad();
        }
        return nullptr;
    }

    UMaterialInstance* MCPMat_LoadInstance(const FString& Path)
    {
        return Cast<UMaterialInstance>(MCPMat_LoadObject(Path));
    }

    UMaterial* MCPMat_LoadMaterial(const FString& Path)
    {
        return Cast<UMaterial>(MCPMat_LoadObject(Path));
    }

    // Load a layer/blend function; an EMPTY path is a VALID empty slot (nullptr), not an error.
    UMaterialFunctionInterface* MCPMat_LoadLayerFunc(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        return Cast<UMaterialFunctionInterface>(MCPMat_LoadObject(Path));
    }

    // Parse "R,G,B[,A]" (floats) into a linear color. Empty/unparseable -> false (caller keeps default).
    bool MCPMat_ParseColor(const FString& In, FLinearColor& Out)
    {
        if (In.IsEmpty())
        {
            return false;
        }
        TArray<FString> Parts;
        In.ParseIntoArray(Parts, TEXT(","), true);
        if (Parts.Num() < 3)
        {
            return false;
        }
        Out.R = FCString::Atof(*Parts[0].TrimStartAndEnd());
        Out.G = FCString::Atof(*Parts[1].TrimStartAndEnd());
        Out.B = FCString::Atof(*Parts[2].TrimStartAndEnd());
        Out.A = (Parts.Num() >= 4) ? FCString::Atof(*Parts[3].TrimStartAndEnd()) : 1.0f;
        return true;
    }

    // Marshal a resolved FMaterialLayersFunctions out to JSON (used by the reader + the setter's
    // prior-state capture). Layers/Blends -> object path strings ("" for an empty slot). Also emits
    // the editor-only layer_names + layer_states so a reader can present the stack meaningfully.
    void MCPMat_LayersToJsonObj(const FMaterialLayersFunctions& L, const TSharedRef<FJsonObject>& Obj)
    {
        TArray<TSharedPtr<FJsonValue>> LayerArr, BlendArr, NameArr, StateArr;
        for (const TObjectPtr<UMaterialFunctionInterface>& F : L.Layers)
        {
            UMaterialFunctionInterface* Fp = F;
            LayerArr.Add(MakeShared<FJsonValueString>(Fp ? Fp->GetPathName() : FString()));
        }
        for (const TObjectPtr<UMaterialFunctionInterface>& F : L.Blends)
        {
            UMaterialFunctionInterface* Fp = F;
            BlendArr.Add(MakeShared<FJsonValueString>(Fp ? Fp->GetPathName() : FString()));
        }
        for (const FText& N : L.EditorOnly.LayerNames)
        {
            NameArr.Add(MakeShared<FJsonValueString>(N.ToString()));
        }
        for (bool S : L.EditorOnly.LayerStates)
        {
            StateArr.Add(MakeShared<FJsonValueBoolean>(S));
        }
        Obj->SetArrayField(TEXT("layers"), LayerArr);
        Obj->SetArrayField(TEXT("blends"), BlendArr);
        Obj->SetArrayField(TEXT("layer_names"), NameArr);
        Obj->SetArrayField(TEXT("layer_states"), StateArr);
        Obj->SetNumberField(TEXT("layer_count"), L.Layers.Num());
        Obj->SetNumberField(TEXT("blend_count"), L.Blends.Num());
    }

    // Read an array-of-strings JSON field into an FString array (tolerant: absent -> empty).
    void MCPMat_ReadStringArray(const TSharedPtr<FJsonObject>& Root, const TCHAR* Field, TArray<FString>& Out)
    {
        Out.Reset();
        const TArray<TSharedPtr<FJsonValue>>* Arr = nullptr;
        if (Root.IsValid() && Root->TryGetArrayField(Field, Arr) && Arr)
        {
            for (const TSharedPtr<FJsonValue>& V : *Arr)
            {
                FString S;
                if (V.IsValid() && V->TryGetString(S))
                {
                    Out.Add(S);
                }
                else
                {
                    Out.Add(FString()); // preserve positional index -> empty slot
                }
            }
        }
    }

    // Replicate UMaterialEditingLibrary::UpdateMaterialInstance WITHOUT the MaterialEditor module dep:
    // dirty + PreEditChange/PostEditChange + force static-permutation recompile + refresh parameter names.
    void MCPMat_UpdateInstance(UMaterialInstance* Instance)
    {
        if (!Instance)
        {
            return;
        }
        Instance->MarkPackageDirty();
        Instance->PreEditChange(nullptr);
        Instance->PostEditChange();
        Instance->UpdateStaticPermutation();
        // (UpdateParameterNames() is protected in 5.8 -> omitted; PostEditChange + UpdateStaticPermutation
        //  already drive the recompile. Parameter-name cache refreshes on next access.)
    }
#endif // WITH_EDITOR
}

// ---------------------------------------------------------------------------
// set_material_layers : set the Material Attribute Layers stack on an instance.
//   LayersJson = {"layers":[func path or "", ...], "blends":[func path or "", ...]}.
//   layers[0] is the background layer (no blend). blends[i-1] blends layers[i].
//   An EMPTY layers array CLEARS the override. Captures the PRIOR stack into "prev"
//   (single atomic read) so the Python ledger's inverse can re-set it.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetMaterialLayersJson(const FString& InstancePath, const FString& LayersJson)
{
#if WITH_EDITOR
    UMaterialInstance* Instance = MCPMat_LoadInstance(InstancePath);
    if (!Instance)
    {
        return MCPMat_Error(FString::Printf(TEXT("could not load MaterialInstance '%s'"), *InstancePath));
    }

    TSharedPtr<FJsonObject> Spec;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(LayersJson);
    if (!FJsonSerializer::Deserialize(Reader, Spec) || !Spec.IsValid())
    {
        return MCPMat_Error(TEXT("layers_json is not a valid JSON object (expected {\"layers\":[...],\"blends\":[...]})"));
    }

    TArray<FString> LayerPaths, BlendPaths;
    MCPMat_ReadStringArray(Spec, TEXT("layers"), LayerPaths);
    MCPMat_ReadStringArray(Spec, TEXT("blends"), BlendPaths);

    // Capture the PRIOR stack for the reversible ledger op (before we mutate anything).
    TSharedRef<FJsonObject> PrevObj = MakeShared<FJsonObject>();
    {
        FMaterialLayersFunctions Prev;
        const bool bHadPrev = Instance->GetMaterialLayers(Prev);
        PrevObj->SetBoolField(TEXT("has_layers"), bHadPrev);
        if (bHadPrev)
        {
            MCPMat_LayersToJsonObj(Prev, PrevObj);
        }
    }

    // Build a well-formed stack via the engine helpers so the seven editor-only arrays stay in
    // lock-step; then overwrite the object slots. N==0 => empty stack (clears the override).
    FMaterialLayersFunctions NewLayers;
    const int32 N = LayerPaths.Num();
    if (N > 0)
    {
        NewLayers.AddDefaultBackgroundLayer();               // Layer[0] (+ editor-only[0])
        NewLayers.Layers[0] = MCPMat_LoadLayerFunc(LayerPaths[0]);
        for (int32 i = 1; i < N; ++i)
        {
            NewLayers.AppendBlendedLayer();                  // Layer[i] + Blend[i-1] (+ editor-only)
            NewLayers.Layers[i] = MCPMat_LoadLayerFunc(LayerPaths[i]);
            const int32 BlendIdx = i - 1;
            if (BlendPaths.IsValidIndex(BlendIdx))
            {
                NewLayers.Blends[BlendIdx] = MCPMat_LoadLayerFunc(BlendPaths[BlendIdx]);
            }
        }
    }

    const bool bChanged = Instance->SetMaterialLayers(NewLayers); // MaterialInstance.h:952 (WITH_EDITOR)
    MCPMat_UpdateInstance(Instance);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("instance"), Instance->GetName());
    Root->SetStringField(TEXT("instance_path"), Instance->GetPathName());
    Root->SetNumberField(TEXT("layer_count"), NewLayers.Layers.Num());
    Root->SetNumberField(TEXT("blend_count"), NewLayers.Blends.Num());
    Root->SetBoolField(TEXT("changed"), bChanged);
    Root->SetObjectField(TEXT("prev"), PrevObj); // Python ledger stores this; inverse re-sets it.
    return MCPMat_Serialize(Root);
#else
    return MCPMat_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// get_material_layers : read the Material Attribute Layers stack back.
//   Returns has_layers=false (NOT an error) when the instance/its parents have no
//   layer stack; otherwise the resolved layers/blends paths + editor-only names/states.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::GetMaterialLayersJson(const FString& InstancePath)
{
#if WITH_EDITOR
    UMaterialInstance* Instance = MCPMat_LoadInstance(InstancePath);
    if (!Instance)
    {
        return MCPMat_Error(FString::Printf(TEXT("could not load MaterialInstance '%s'"), *InstancePath));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("instance"), Instance->GetName());
    Root->SetStringField(TEXT("instance_path"), Instance->GetPathName());

    FMaterialLayersFunctions Layers;
    const bool bHasLayers = Instance->GetMaterialLayers(Layers); // MaterialInstance.h:930
    Root->SetBoolField(TEXT("has_layers"), bHasLayers);
    if (bHasLayers)
    {
        MCPMat_LayersToJsonObj(Layers, Root);
    }
    else
    {
        // Emit empty arrays so callers can treat the shape uniformly.
        Root->SetArrayField(TEXT("layers"), TArray<TSharedPtr<FJsonValue>>());
        Root->SetArrayField(TEXT("blends"), TArray<TSharedPtr<FJsonValue>>());
        Root->SetNumberField(TEXT("layer_count"), 0);
        Root->SetNumberField(TEXT("blend_count"), 0);
    }
    return MCPMat_Serialize(Root);
#else
    return MCPMat_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// add_material_comment : add a comment box to a BASE Material's graph.
//   Color is "R,G,B[,A]" floats ("" -> keep the expression's default white).
//   Returns the created comment's object name + index in EditorComments (for the
//   ledger's reversible remove).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::AddMaterialCommentJson(const FString& MaterialPath, const FString& Text,
    int32 X, int32 Y, int32 SizeX, int32 SizeY, const FString& Color)
{
#if WITH_EDITOR
    UMaterial* Material = MCPMat_LoadMaterial(MaterialPath);
    if (!Material)
    {
        return MCPMat_Error(FString::Printf(TEXT("could not load base Material '%s' (must be a UMaterial, not an instance/function)"), *MaterialPath));
    }

    UMaterialExpressionComment* Comment = NewObject<UMaterialExpressionComment>(Material, NAME_None, RF_Transactional);
    if (!Comment)
    {
        return MCPMat_Error(TEXT("failed to construct UMaterialExpressionComment"));
    }

    Comment->MaterialExpressionEditorX = X;
    Comment->MaterialExpressionEditorY = Y;
    Comment->SizeX = (SizeX > 0) ? SizeX : 400;
    Comment->SizeY = (SizeY > 0) ? SizeY : 100;
    Comment->Text = Text;
    FLinearColor Col;
    if (MCPMat_ParseColor(Color, Col))
    {
        Comment->CommentColor = Col;
    }

    Material->GetExpressionCollection().AddComment(Comment); // MaterialExpression.h:131
    Material->MarkPackageDirty();
    Material->PostEditChange();

    const int32 Index = Material->GetExpressionCollection().EditorComments.IndexOfByKey(Comment);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("material"), Material->GetName());
    Root->SetStringField(TEXT("material_path"), Material->GetPathName());
    Root->SetStringField(TEXT("comment_name"), Comment->GetName());
    Root->SetNumberField(TEXT("index"), Index);
    Root->SetStringField(TEXT("text"), Comment->Text);
    Root->SetNumberField(TEXT("x"), Comment->MaterialExpressionEditorX);
    Root->SetNumberField(TEXT("y"), Comment->MaterialExpressionEditorY);
    Root->SetNumberField(TEXT("size_x"), Comment->SizeX);
    Root->SetNumberField(TEXT("size_y"), Comment->SizeY);
    Root->SetNumberField(TEXT("comment_count"), Material->GetExpressionCollection().EditorComments.Num());
    return MCPMat_Serialize(Root);
#else
    return MCPMat_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// remove_material_comment : FAITHFUL INVERSE of add_material_comment. Removes the
//   comment whose object name == CommentName from the material's EditorComments.
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::RemoveMaterialCommentJson(const FString& MaterialPath, const FString& CommentName)
{
#if WITH_EDITOR
    UMaterial* Material = MCPMat_LoadMaterial(MaterialPath);
    if (!Material)
    {
        return MCPMat_Error(FString::Printf(TEXT("could not load base Material '%s'"), *MaterialPath));
    }
    if (CommentName.IsEmpty())
    {
        return MCPMat_Error(TEXT("comment_name is required"));
    }

    FMaterialExpressionCollection& Collection = Material->GetExpressionCollection();
    UMaterialExpressionComment* Found = nullptr;
    for (const TObjectPtr<UMaterialExpressionComment>& C : Collection.EditorComments)
    {
        UMaterialExpressionComment* Cp = C;
        if (Cp && Cp->GetName() == CommentName)
        {
            Found = Cp;
            break;
        }
    }
    if (!Found)
    {
        return MCPMat_Error(FString::Printf(TEXT("no comment named '%s' on material"), *CommentName));
    }

    Collection.RemoveComment(Found); // MaterialExpression.h:132
    Material->MarkPackageDirty();
    Material->PostEditChange();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("material"), Material->GetName());
    Root->SetStringField(TEXT("material_path"), Material->GetPathName());
    Root->SetStringField(TEXT("comment_name"), CommentName);
    Root->SetBoolField(TEXT("removed"), true);
    Root->SetNumberField(TEXT("comment_count"), Collection.EditorComments.Num());
    return MCPMat_Serialize(Root);
#else
    return MCPMat_Error(TEXT("editor-only"));
#endif
}
