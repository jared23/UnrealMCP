// ============================================================================
// MCPReflection_WidgetAnim.cpp  —  WIDGET ANIMATIONS "W-C" batch: create/list/
//   remove UWidgetAnimation assets on a UWidgetBlueprint, bind widgets to a
//   MovieScene possessable, add property tracks (opacity/color/transform/
//   visibility), and key their channels. Eight DEFERRED MCPReflectionLibrary
//   handlers that widget_anim_cpp.py hasattr-guards on; when these link the
//   Python tools auto-enable.
// ----------------------------------------------------------------------------
// DRAFTED on Windows 2026-08-19. **ISOLATED translation unit** on purpose
// (mirrors MCPReflection_Widgets.cpp / MCPReflection_Niagara2.cpp). Reuses the
// MCPWid_-style WidgetBlueprint/JSON access from MCPReflection_Widgets.cpp
// (C++ #37), re-prefixed MCPWAn_ for unity-build uniqueness.
//
// >>> BUILD STORY <<<  This batch DOES need TWO new Build.cs modules:
//     MovieScene       (runtime) — UMovieScene, FMovieScenePossessable,
//                                   FMovieSceneBinding, FMovieSceneFloatChannel,
//                                   FMovieSceneBoolChannel, FMovieSceneChannelProxy.
//     MovieSceneTracks (runtime) — UMovieScenePropertyTrack + the concrete
//                                   UMovieSceneFloatTrack / UMovieSceneColorTrack /
//                                   UMovieSceneVisibilityTrack (+ their sections).
//   Both are RUNTIME modules (MOVIESCENE_API / MOVIESCENETRACKS_API export macros)
//   -> LOW link risk. UMG (UMovieScene2DTransformTrack + UWidgetAnimation) and
//   UMGEditor (UWidgetBlueprint) are ALREADY PublicDependencyModuleNames
//   (added for C++ #8, UnrealMCP.Build.cs:42). UnrealEd already a dep
//   (FBlueprintEditorUtils). EXACT edit reported in the final report; add to
//   PublicDependencyModuleNames:  "MovieScene", "MovieSceneTracks".
//
// >>> THE POSSESSABLE-GUID <-> FWidgetAnimationBinding WIRING (the risk) <<<
//   A UMG animation binds a widget in TWO coupled places that MUST share one GUID:
//     1. UMovieScene::AddPossessable(Name, Class) -> returns FGuid G. Internally
//        (MovieScene.cpp:390) it InsertSorted's an FMovieScenePossessable AND an
//        FMovieSceneBinding keyed on G, so tracks can later attach to G.
//     2. UWidgetAnimation::AnimationBindings gets an FWidgetAnimationBinding whose
//        .AnimationGuid = G, .WidgetName = Widget->GetFName(), .bIsRootWidget =
//        (tree root?). This EXACTLY mirrors UWidgetAnimation::BindPossessableObject
//        (WidgetAnimation.cpp:233-278, the non-slot branches) — the engine's own
//        wiring, minus the live-Sequencer GetHandleToObject() path we cannot use
//        headless (FWidgetBlueprintEditor::AddObjectToAnimation, WidgetBlueprintEditor.cpp:2146).
//   Tracks attach with UMovieScene::AddTrack(TrackClass, G) (MovieScene.cpp:1173)
//   which FindBinding(G)'s the binding created in step 1 — returns NULL if G has no
//   binding (guarded). Removing a binding uses UMovieScene::RemovePossessable(G)
//   (MovieScene.cpp:450) which cascades RemoveBinding(G) -> drops the object binding
//   AND its tracks; we then drop the FWidgetAnimationBinding entries for G.
//   We possess the WIDGET itself (never a UPanelSlot), so SlotWidgetName stays
//   NAME_None — matching BindPossessableObject's `PossessedSlot == nullptr` branch.
//
// >>> CREATE recipe (handler #1) <<<  Verbatim from the engine's "+Animation"
//   button (AnimationTabSummoner.cpp:626-655):
//     NewObject<UWidgetAnimation>(WBP, FName(*AnimName), RF_Transactional)
//     Anim->SetDisplayLabel(AnimName)
//     Anim->MovieScene = NewObject<UMovieScene>(Anim, FName(*AnimName), RF_Transactional)  // MovieScene is a PUBLIC member; there is NO setter, only GetMovieScene()
//     MovieScene->SetDisplayRate(FFrameRate(20,1))   // engine reads UMGEditorProjectSettings->DefaultWidgetAnimationFrameRate; 20 is the GetNullAnimation() fallback (WidgetAnimation.cpp:99)
//     WBP->Animations.Add(Anim)                       // public TArray<TObjectPtr<UWidgetAnimation>> under WITH_EDITORONLY_DATA (WidgetBlueprint.h:229)
//     WBP->OnVariableAdded(Anim->GetFName())          // keep WidgetVariableNameToGuidMap in sync (same discipline as C++ #8)
//
// CRASH-SAFETY: every load / FindWidget / GUID lookup / GetMovieScene / section /
// channel is null-guarded; handlers return {"error":...} on any miss (never crash).
// Reads are non-mutating. Writes end with MarkBlueprintAsStructurallyModified. The
// Python side finalizes each write with a compile + save and records the inverse on
// the per-session ledger for editor_level.undo to fold. Anon-namespace helpers are
// prefixed MCPWAn_. VERIFY-tagged calls are version-sensitive; confirm on the live
// build.
//
//   Confirmed 5.8 signatures (header:line verified against the source engine):
//     UWidgetBlueprint::Animations  TArray<TObjectPtr<UWidgetAnimation>> (WITH_EDITORONLY_DATA, public) WidgetBlueprint.h:229
//     UWidgetBlueprint::OnVariableAdded/Removed(FName)  UMGEDITOR_API              WidgetBlueprint.h:257-259
//     UWidgetAnimation::MovieScene (public TObjectPtr<UMovieScene>)                WidgetAnimation.h:143
//     UWidgetAnimation::AnimationBindings (public TArray<FWidgetAnimationBinding>) WidgetAnimation.h:147
//     UWidgetAnimation::GetMovieScene()/SetDisplayLabel/GetDisplayLabel  UMG_API   WidgetAnimation.h:46,99,141
//     UWidgetAnimation::GetStartTime/GetEndTime()  UMG_API                         WidgetAnimation.h:59,68
//     UWidgetAnimation::RemoveBinding(const FWidgetAnimationBinding&)  UMG_API      WidgetAnimation.h:128
//     FWidgetAnimationBinding { FName WidgetName; FName SlotWidgetName; FGuid AnimationGuid; bool bIsRootWidget; FMovieSceneDynamicBinding DynamicBinding; } WidgetAnimationBinding.h:26-44
//     UMovieScene::AddPossessable(const FString&, UClass*) -> FGuid  MOVIESCENE_API MovieScene.h:452
//     UMovieScene::RemovePossessable(const FGuid&) -> bool  MOVIESCENE_API          MovieScene.h:467
//     UMovieScene::AddTrack(TSubclassOf<UMovieSceneTrack>, const FGuid&)  MOVIESCENE_API MovieScene.h:517
//     UMovieScene::FindTrack(TSubclassOf<UMovieSceneTrack>, const FGuid&, FName) const MOVIESCENE_API MovieScene.h:554
//     UMovieScene::RemoveTrack(UMovieSceneTrack&) -> bool  MOVIESCENE_API           MovieScene.h:589
//     UMovieScene::SetPlaybackRange / GetPlaybackRange / GetTickResolution / SetDisplayRate / SetObjectDisplayName  MOVIESCENE_API MovieScene.h:1008,804,811,832,933
//     UMovieScene::GetBindings() const / GetTracks() const  (const refs)           MovieScene.h:777,706
//     FMovieSceneBinding::GetObjectGuid()/GetTracks()                              MovieSceneBinding.h:84,118
//     UMovieScenePropertyTrack::SetPropertyNameAndPath(FName,FString)  MOVIESCENETRACKS_API  MovieScenePropertyTrack.h:80
//     UMovieScenePropertyTrack::FindOrAddSection(FFrameNumber, bool&)  MOVIESCENETRACKS_API  MovieScenePropertyTrack.h:129
//     UMovieSceneSection::GetChannelProxy() const / SetRange / GetRange  MOVIESCENE_API MovieSceneSection.h:674,327,273
//     FMovieSceneChannelProxy::GetChannels<T>() / GetAllEntries()                  MovieSceneChannelProxy.h:259,232
//     FMovieSceneFloatChannel::AddCubicKey/AddLinearKey/AddConstantKey  MOVIESCENE_API MovieSceneFloatChannel.h:277-281
//     TMovieSceneChannelData<T>::FindKey/RemoveKey/AddKey/GetTimes                 MovieSceneChannelData.h:150,506,325,113
//     FMovieSceneBoolChannel::GetData() -> TMovieSceneChannelData<bool>            MovieSceneBoolChannel.h:51
//     FFrameRate::AsFrameNumber(double)                                           FrameRate.h:89
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
#include "UObject/UObjectGlobals.h"       // NewObject / StaticFindObject
#include "Misc/PackageName.h"
#include "Misc/FrameRate.h"
#include "Misc/FrameNumber.h"

// --- UMG runtime (UMG_API — already a dep) ---
#include "Blueprint/WidgetTree.h"                 // UWidgetTree (RootWidget, FindWidget)
#include "Components/Widget.h"                     // UWidget
#include "Animation/WidgetAnimation.h"             // UWidgetAnimation (MovieScene, AnimationBindings)
#include "Animation/WidgetAnimationBinding.h"      // FWidgetAnimationBinding
#include "Animation/MovieScene2DTransformTrack.h"  // UMovieScene2DTransformTrack (transform track lives in UMG, not MovieSceneTracks)

// --- UMGEditor (UMGEDITOR_API — already a dep) ---
#include "WidgetBlueprint.h"                        // UWidgetBlueprint (Animations, OnVariableAdded/Removed)

// --- MovieScene (NEW Build.cs dep) ---
#include "MovieScene.h"                            // UMovieScene
#include "MovieSceneSection.h"                     // UMovieSceneSection
#include "MovieSceneBinding.h"                     // FMovieSceneBinding
#include "Channels/MovieSceneChannelProxy.h"       // FMovieSceneChannelProxy, FMovieSceneChannelEntry
#include "Channels/MovieSceneChannelData.h"        // TMovieSceneChannelData (FindKey/RemoveKey/AddKey/GetTimes)
#include "Channels/MovieSceneFloatChannel.h"       // FMovieSceneFloatChannel, FMovieSceneFloatValue
#include "Channels/MovieSceneBoolChannel.h"        // FMovieSceneBoolChannel

// --- MovieSceneTracks (NEW Build.cs dep) ---
#include "Tracks/MovieScenePropertyTrack.h"        // UMovieScenePropertyTrack (SetPropertyNameAndPath, FindOrAddSection)
#include "Tracks/MovieSceneFloatTrack.h"           // UMovieSceneFloatTrack   (opacity)
#include "Tracks/MovieSceneColorTrack.h"           // UMovieSceneColorTrack   (color)
#include "Tracks/MovieSceneVisibilityTrack.h"      // UMovieSceneVisibilityTrack (visibility)

// --- Blueprint editor utils (UnrealEd — already a dep) ---
#include "Kismet2/BlueprintEditorUtils.h"          // FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPWAn_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPWAn_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPWAn_Serialize(Obj);
    }

#if WITH_EDITOR
    // Resolve a UWidgetBlueprint from a package/asset path (copied from MCPReflection_Widgets.cpp's MCPWid_LoadWBP).
    UWidgetBlueprint* MCPWAn_LoadWBP(const FString& Path)
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

    // Find an animation on a widget blueprint by object name OR display label (case-sensitive).
    UWidgetAnimation* MCPWAn_FindAnim(UWidgetBlueprint* WBP, const FString& AnimName)
    {
        if (!WBP || AnimName.IsEmpty())
        {
            return nullptr;
        }
#if WITH_EDITORONLY_DATA
        for (UWidgetAnimation* Anim : WBP->Animations)
        {
            if (Anim && Anim->GetName() == AnimName)
            {
                return Anim;
            }
        }
        for (UWidgetAnimation* Anim : WBP->Animations)
        {
            if (Anim && Anim->GetDisplayLabel() == AnimName)
            {
                return Anim;
            }
        }
#endif
        return nullptr;
    }

    // Map a track-type keyword -> the concrete UMovieScenePropertyTrack subclass.
    // opacity -> UMovieSceneFloatTrack ; color -> UMovieSceneColorTrack ;
    // transform -> UMovieScene2DTransformTrack ; visibility -> UMovieSceneVisibilityTrack.
    UClass* MCPWAn_TrackClassFor(const FString& TrackType)
    {
        const FString T = TrackType.ToLower();
        if (T == TEXT("opacity") || T == TEXT("renderopacity") || T == TEXT("float"))
        {
            return UMovieSceneFloatTrack::StaticClass();
        }
        if (T == TEXT("color") || T == TEXT("colorandopacity"))
        {
            return UMovieSceneColorTrack::StaticClass();
        }
        if (T == TEXT("transform") || T == TEXT("rendertransform") || T == TEXT("2dtransform"))
        {
            return UMovieScene2DTransformTrack::StaticClass();
        }
        if (T == TEXT("visibility") || T == TEXT("visible"))
        {
            return UMovieSceneVisibilityTrack::StaticClass();
        }
        return nullptr;
    }

    // The UMG property (name, path) a given track type animates. These are the standard
    // UWidget property names (RenderOpacity float, ColorAndOpacity FLinearColor/FSlateColor,
    // RenderTransform FWidgetTransform, Visibility ESlateVisibility). The track compiles
    // regardless; the string resolves at runtime on widgets that expose the property.
    void MCPWAn_PropertyFor(const FString& TrackType, FName& OutName, FString& OutPath)
    {
        const FString T = TrackType.ToLower();
        if (T == TEXT("color") || T == TEXT("colorandopacity"))
        {
            OutName = FName(TEXT("ColorAndOpacity")); OutPath = TEXT("ColorAndOpacity");
        }
        else if (T == TEXT("transform") || T == TEXT("rendertransform") || T == TEXT("2dtransform"))
        {
            OutName = FName(TEXT("RenderTransform")); OutPath = TEXT("RenderTransform");
        }
        else if (T == TEXT("visibility") || T == TEXT("visible"))
        {
            OutName = FName(TEXT("Visibility")); OutPath = TEXT("Visibility");
        }
        else // opacity / default
        {
            OutName = FName(TEXT("RenderOpacity")); OutPath = TEXT("RenderOpacity");
        }
    }

    // Locate the possessable GUID that an FWidgetAnimationBinding uses for a widget name.
    // Returns an invalid FGuid if the widget is not bound to the animation.
    FGuid MCPWAn_BindingGuidFor(const UWidgetAnimation* Anim, const FName WidgetName)
    {
        if (Anim)
        {
            for (const FWidgetAnimationBinding& B : Anim->AnimationBindings)
            {
                // We bind the widget itself (SlotWidgetName == NAME_None), so match on WidgetName.
                if (B.WidgetName == WidgetName && B.SlotWidgetName.IsNone())
                {
                    return B.AnimationGuid;
                }
            }
        }
        return FGuid();
    }

    // Count all tracks reachable in a MovieScene (root/master tracks + per-binding tracks).
    int32 MCPWAn_TrackCount(const UMovieScene* MS)
    {
        if (!MS)
        {
            return 0;
        }
        int32 Count = MS->GetTracks().Num();
        for (const FMovieSceneBinding& B : MS->GetBindings())
        {
            Count += B.GetTracks().Num();
        }
        return Count;
    }
#endif // WITH_EDITOR
}

// ============================================================================
// (1) CREATE — new UWidgetAnimation + its UMovieScene, append to WBP->Animations.
//     Inverse: RemoveWidgetAnimationJson(AnimName).
// ============================================================================
FString UMCPReflectionLibrary::CreateWidgetAnimationJson(const FString& WidgetBlueprintPath, const FString& AnimName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    if (AnimName.IsEmpty())
    {
        return MCPWAn_Error(TEXT("anim_name is empty"));
    }
    if (MCPWAn_FindAnim(WBP, AnimName) != nullptr)
    {
        return MCPWAn_Error(FString::Printf(TEXT("an animation named '%s' already exists"), *AnimName));
    }
    // Object-name collision guard: NewObject with an explicit name that already exists under
    // this outer would rename/assert; refuse cleanly instead.
    if (StaticFindObject(UObject::StaticClass(), WBP, *AnimName) != nullptr)
    {
        return MCPWAn_Error(FString::Printf(TEXT("a subobject named '%s' already exists on the blueprint"), *AnimName));
    }

    WBP->Modify();

    UWidgetAnimation* Anim = NewObject<UWidgetAnimation>(WBP, FName(*AnimName), RF_Transactional);
    if (!Anim)
    {
        return MCPWAn_Error(TEXT("NewObject<UWidgetAnimation> returned null"));
    }
    Anim->SetDisplayLabel(AnimName);

    // MovieScene is a PUBLIC member (no setter exists) — assign directly, exactly like the engine.
    UMovieScene* MS = NewObject<UMovieScene>(Anim, FName(*AnimName), RF_Transactional);
    if (!MS)
    {
        return MCPWAn_Error(TEXT("NewObject<UMovieScene> returned null"));
    }
    Anim->MovieScene = MS;
    MS->SetDisplayRate(FFrameRate(20, 1));

    // Give the fresh animation a 1-second default playback range so it is non-degenerate.
    // (Keys added later expand this in AddAnimationKeyJson.)
    const FFrameRate Tick = MS->GetTickResolution();
    MS->SetPlaybackRange(TRange<FFrameNumber>(FFrameNumber(0), Tick.AsFrameNumber(1.0)));

    WBP->Animations.Add(Anim);
    WBP->OnVariableAdded(Anim->GetFName());   // keep WidgetVariableNameToGuidMap in sync

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("created"), true);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("display_label"), Anim->GetDisplayLabel());
    Root->SetNumberField(TEXT("animation_count"), WBP->Animations.Num());
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (2) LIST animations. READ.
// ============================================================================
FString UMCPReflectionLibrary::ListWidgetAnimationsJson(const FString& WidgetBlueprintPath)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (UWidgetAnimation* Anim : WBP->Animations)
    {
        if (!Anim)
        {
            continue;
        }
        TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
        E->SetStringField(TEXT("name"), Anim->GetName());
        E->SetStringField(TEXT("display_label"), Anim->GetDisplayLabel());
        E->SetNumberField(TEXT("binding_count"), Anim->AnimationBindings.Num());
        const UMovieScene* MS = Anim->GetMovieScene();
        if (MS)
        {
            // GetEndTime/GetStartTime deref MovieScene internally; guarded by MS != null here.
            E->SetNumberField(TEXT("duration"), Anim->GetEndTime() - Anim->GetStartTime());
            E->SetNumberField(TEXT("track_count"), MCPWAn_TrackCount(MS));
            E->SetNumberField(TEXT("possessable_count"), MS->GetPossessableCount());
        }
        else
        {
            E->SetNumberField(TEXT("duration"), 0.0);
            E->SetNumberField(TEXT("track_count"), 0);
            E->SetBoolField(TEXT("movie_scene_missing"), true);
        }
        Arr.Add(MakeShared<FJsonValueObject>(E));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetNumberField(TEXT("animation_count"), Arr.Num());
    Root->SetArrayField(TEXT("animations"), Arr);
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (3) REMOVE an animation. Captures the name/label for a best-effort re-create
//     inverse (LOSSY: tracks/bindings/keys are not restored by re-create).
// ============================================================================
FString UMCPReflectionLibrary::RemoveWidgetAnimationJson(const FString& WidgetBlueprintPath, const FString& AnimName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }

    const FString ObjName = Anim->GetName();
    const FString DisplayLabel = Anim->GetDisplayLabel();
    const int32 BindingCount = Anim->AnimationBindings.Num();
    const UMovieScene* MS = Anim->GetMovieScene();
    const int32 TrackCount = MCPWAn_TrackCount(MS);
    const FName AnimFName = Anim->GetFName();

    WBP->Modify();
    const int32 Removed = WBP->Animations.Remove(Anim);
    WBP->OnVariableRemoved(AnimFName);   // keep WidgetVariableNameToGuidMap in sync

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), Removed > 0);
    Root->SetStringField(TEXT("anim_name"), ObjName);
    Root->SetStringField(TEXT("display_label"), DisplayLabel);
    Root->SetNumberField(TEXT("prev_binding_count"), BindingCount);
    Root->SetNumberField(TEXT("prev_track_count"), TrackCount);
    Root->SetNumberField(TEXT("animation_count"), WBP->Animations.Num());
    Root->SetStringField(TEXT("note"), TEXT("re-create inverse is LOSSY: tracks/bindings/keys are not restored"));
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (4) ADD WIDGET BINDING — possess a widget: AddPossessable -> FGuid, then an
//     FWidgetAnimationBinding.AnimationGuid == that GUID. THE core wiring.
//     Inverse: RemoveAnimationWidgetBindingJson(AnimName, WidgetName).
// ============================================================================
FString UMCPReflectionLibrary::AddAnimationWidgetBindingJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    UWidgetTree* Tree = WBP->WidgetTree;
    if (!Tree)
    {
        return MCPWAn_Error(TEXT("blueprint has no WidgetTree"));
    }
    UWidget* W = Tree->FindWidget(FName(*WidgetName));
    if (!W)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget not found: %s"), *WidgetName));
    }

    // Idempotency guard: if the widget is already bound, return its GUID rather than double-possess.
    const FGuid Existing = MCPWAn_BindingGuidFor(Anim, W->GetFName());
    if (Existing.IsValid())
    {
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("blueprint"), WBP->GetName());
        R->SetBoolField(TEXT("added"), false);
        R->SetBoolField(TEXT("already_bound"), true);
        R->SetStringField(TEXT("anim_name"), Anim->GetName());
        R->SetStringField(TEXT("widget"), W->GetName());
        R->SetStringField(TEXT("animation_guid"), Existing.ToString());
        return MCPWAn_Serialize(R);
    }

    Anim->Modify();
    MS->Modify();

    // 1) MovieScene possessable + object binding (keyed on the returned GUID).
    const FGuid NewGuid = MS->AddPossessable(W->GetName(), W->GetClass());
    if (!NewGuid.IsValid())
    {
        return MCPWAn_Error(TEXT("AddPossessable returned an invalid GUID"));
    }
    MS->SetObjectDisplayName(NewGuid, FText::FromString(W->GetName()));

    // 2) FWidgetAnimationBinding with the SAME GUID (mirrors BindPossessableObject non-slot branch).
    const bool bIsRoot = (Tree->RootWidget == W);
    FWidgetAnimationBinding NewBinding;
    NewBinding.AnimationGuid = NewGuid;
    NewBinding.WidgetName    = W->GetFName();
    NewBinding.bIsRootWidget = bIsRoot;
    // SlotWidgetName intentionally left NAME_None (we possess the widget, not its slot).
    Anim->AnimationBindings.Add(NewBinding);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), W->GetName());
    Root->SetStringField(TEXT("animation_guid"), NewGuid.ToString());
    Root->SetBoolField(TEXT("is_root_widget"), bIsRoot);
    Root->SetNumberField(TEXT("binding_count"), Anim->AnimationBindings.Num());
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (5) REMOVE WIDGET BINDING — inverse of (4). RemovePossessable cascades the
//     object binding + its tracks; then drop the FWidgetAnimationBinding(s).
//     Captures GUID + is_root for a re-add inverse.
// ============================================================================
FString UMCPReflectionLibrary::RemoveAnimationWidgetBindingJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    const FName WidgetFName(*WidgetName);
    const FGuid Guid = MCPWAn_BindingGuidFor(Anim, WidgetFName);
    if (!Guid.IsValid())
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget '%s' is not bound to animation '%s'"), *WidgetName, *AnimName));
    }

    // Capture for the re-add inverse.
    bool bIsRoot = false;
    for (const FWidgetAnimationBinding& B : Anim->AnimationBindings)
    {
        if (B.AnimationGuid == Guid)
        {
            bIsRoot = B.bIsRootWidget;
            break;
        }
    }

    Anim->Modify();
    MS->Modify();

    // Cascades: removes possessable + object binding + that binding's tracks (MovieScene.cpp:473).
    const bool bRemovedPossessable = MS->RemovePossessable(Guid);

    // Drop the FWidgetAnimationBinding entries carrying this GUID.
    const int32 RemovedBindings = Anim->AnimationBindings.RemoveAll([&](const FWidgetAnimationBinding& B)
    {
        return B.AnimationGuid == Guid;
    });

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), RemovedBindings > 0);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("animation_guid"), Guid.ToString());
    Root->SetBoolField(TEXT("was_root_widget"), bIsRoot);
    Root->SetBoolField(TEXT("removed_possessable"), bRemovedPossessable);
    Root->SetNumberField(TEXT("binding_count"), Anim->AnimationBindings.Num());
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (6) ADD TRACK — a property track bound to the widget's possessable GUID.
//     Inverse: RemoveTrack (folded via editor_level.undo).
// ============================================================================
FString UMCPReflectionLibrary::AddAnimationTrackJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    UClass* TrackCls = MCPWAn_TrackClassFor(TrackType);
    if (!TrackCls)
    {
        return MCPWAn_Error(FString::Printf(TEXT("unknown track_type '%s' (want opacity|color|transform|visibility)"), *TrackType));
    }
    const FGuid Guid = MCPWAn_BindingGuidFor(Anim, FName(*WidgetName));
    if (!Guid.IsValid())
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget '%s' is not bound; call AddAnimationWidgetBinding first"), *WidgetName));
    }

    // Idempotency: one track of a given class per binding.
    if (MS->FindTrack(TrackCls, Guid) != nullptr)
    {
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("blueprint"), WBP->GetName());
        R->SetBoolField(TEXT("added"), false);
        R->SetBoolField(TEXT("already_exists"), true);
        R->SetStringField(TEXT("anim_name"), Anim->GetName());
        R->SetStringField(TEXT("widget"), WidgetName);
        R->SetStringField(TEXT("track_type"), TrackType);
        R->SetStringField(TEXT("track_class"), TrackCls->GetName());
        R->SetStringField(TEXT("animation_guid"), Guid.ToString());
        return MCPWAn_Serialize(R);
    }

    Anim->Modify();
    MS->Modify();

    UMovieSceneTrack* Track = MS->AddTrack(TrackCls, Guid);
    if (!Track)
    {
        // AddTrack returns null if the GUID has no binding (shouldn't happen post-guard) or the
        // track class is disallowed on this MovieScene.
        return MCPWAn_Error(FString::Printf(TEXT("AddTrack(%s) returned null"), *TrackCls->GetName()));
    }
    Track->Modify();

    // Set the animated property name/path (all four types are UMovieScenePropertyTrack subclasses).
    FName PropName; FString PropPath;
    MCPWAn_PropertyFor(TrackType, PropName, PropPath);
    if (UMovieScenePropertyTrack* PropTrack = Cast<UMovieScenePropertyTrack>(Track))
    {
        PropTrack->SetPropertyNameAndPath(PropName, PropPath);
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("added"), true);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("track_type"), TrackType.ToLower());
    Root->SetStringField(TEXT("track_class"), TrackCls->GetName());
    Root->SetStringField(TEXT("property_name"), PropName.ToString());
    Root->SetStringField(TEXT("animation_guid"), Guid.ToString());
    Root->SetNumberField(TEXT("track_count"), MCPWAn_TrackCount(MS));
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (7) ADD KEY — find/add a section on the track, key one channel at TimeSeconds.
//     Keys the float channel at ChannelIndex (opacity=1 channel; color/transform
//     have several; index selects) or a bool channel (visibility). Dedups any
//     existing key at the same frame (captures prev value for the inverse).
//     Inverse: remove the key at `frame` on `channel_index` (folded via
//     editor_level.undo); if had_key, restore prev_value instead.
// ============================================================================
FString UMCPReflectionLibrary::AddAnimationKeyJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType, float TimeSeconds, float Value, int32 ChannelIndex, const FString& Interp)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    UClass* TrackCls = MCPWAn_TrackClassFor(TrackType);
    if (!TrackCls)
    {
        return MCPWAn_Error(FString::Printf(TEXT("unknown track_type '%s'"), *TrackType));
    }
    const FGuid Guid = MCPWAn_BindingGuidFor(Anim, FName(*WidgetName));
    if (!Guid.IsValid())
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget '%s' is not bound"), *WidgetName));
    }
    UMovieSceneTrack* Track = MS->FindTrack(TrackCls, Guid);
    if (!Track)
    {
        return MCPWAn_Error(FString::Printf(TEXT("no %s track on widget '%s'; call AddAnimationTrack first"), *TrackType, *WidgetName));
    }
    UMovieScenePropertyTrack* PropTrack = Cast<UMovieScenePropertyTrack>(Track);
    if (!PropTrack)
    {
        return MCPWAn_Error(TEXT("track is not a property track"));
    }

    const FFrameRate Tick = MS->GetTickResolution();
    const FFrameNumber Frame = Tick.AsFrameNumber((double)TimeSeconds);

    MS->Modify();
    Track->Modify();

    bool bSectionAdded = false;
    UMovieSceneSection* Section = PropTrack->FindOrAddSection(Frame, bSectionAdded);
    if (!Section)
    {
        return MCPWAn_Error(TEXT("FindOrAddSection returned null"));
    }
    Section->Modify();

    // Ensure the section spans [0, Frame] so the key evaluates (a freshly added section may be point-sized).
    {
        const FFrameNumber Zero(0);
        const FFrameNumber Hi = (Frame.Value >= 0) ? Frame : Zero;
        const TRange<FFrameNumber> Desired = TRange<FFrameNumber>::Inclusive(FMath::Min(Zero, Frame), Hi);
        Section->SetRange(TRange<FFrameNumber>::Hull(Section->GetRange(), Desired));
    }

    FMovieSceneChannelProxy& Proxy = Section->GetChannelProxy();
    FString ChannelType;
    bool bHadKey = false;
    double PrevValue = 0.0;
    int32 UsedChannelIndex = ChannelIndex;

    TArrayView<FMovieSceneFloatChannel*> FloatChs = Proxy.GetChannels<FMovieSceneFloatChannel>();
    if (FloatChs.Num() > 0)
    {
        UsedChannelIndex = FMath::Clamp(ChannelIndex, 0, FloatChs.Num() - 1);
        FMovieSceneFloatChannel* Ch = FloatChs[UsedChannelIndex];
        if (!Ch)
        {
            return MCPWAn_Error(TEXT("null float channel"));
        }
        // Dedup: capture + remove any existing key at Frame so we replace rather than duplicate.
        TMovieSceneChannelData<FMovieSceneFloatValue> Data = Ch->GetData();
        const int32 ExistingIdx = Data.FindKey(Frame);
        if (ExistingIdx != INDEX_NONE)
        {
            bHadKey = true;
            TArrayView<const FMovieSceneFloatValue> Vals = Ch->GetValues();
            if (Vals.IsValidIndex(ExistingIdx))
            {
                PrevValue = (double)Vals[ExistingIdx].Value;
            }
            Data.RemoveKey(ExistingIdx);
        }
        const FString I = Interp.ToLower();
        if (I == TEXT("linear"))
        {
            Ch->AddLinearKey(Frame, Value);
        }
        else if (I == TEXT("constant") || I == TEXT("step"))
        {
            Ch->AddConstantKey(Frame, Value);
        }
        else
        {
            Ch->AddCubicKey(Frame, Value);
        }
        ChannelType = TEXT("float");
    }
    else
    {
        TArrayView<FMovieSceneBoolChannel*> BoolChs = Proxy.GetChannels<FMovieSceneBoolChannel>();
        if (BoolChs.Num() == 0)
        {
            return MCPWAn_Error(TEXT("section has no keyable float or bool channel"));
        }
        UsedChannelIndex = FMath::Clamp(ChannelIndex, 0, BoolChs.Num() - 1);
        FMovieSceneBoolChannel* Ch = BoolChs[UsedChannelIndex];
        if (!Ch)
        {
            return MCPWAn_Error(TEXT("null bool channel"));
        }
        TMovieSceneChannelData<bool> Data = Ch->GetData();
        const int32 ExistingIdx = Data.FindKey(Frame);
        if (ExistingIdx != INDEX_NONE)
        {
            bHadKey = true;
            TArrayView<const bool> Vals = Ch->GetValues();
            if (Vals.IsValidIndex(ExistingIdx))
            {
                PrevValue = Vals[ExistingIdx] ? 1.0 : 0.0;
            }
            Data.RemoveKey(ExistingIdx);
        }
        Data.AddKey(Frame, Value != 0.0f);
        ChannelType = TEXT("bool");
    }

    // Expand the MovieScene playback range to include the keyed frame.
    {
        const TRange<FFrameNumber> PB = MS->GetPlaybackRange();
        if (!PB.Contains(Frame))
        {
            MS->SetPlaybackRange(TRange<FFrameNumber>::Hull(PB, TRange<FFrameNumber>::Inclusive(Frame, Frame)));
        }
    }

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("keyed"), true);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("track_type"), TrackType.ToLower());
    Root->SetStringField(TEXT("channel_type"), ChannelType);
    Root->SetNumberField(TEXT("channel_index"), UsedChannelIndex);
    Root->SetNumberField(TEXT("time_seconds"), TimeSeconds);
    Root->SetNumberField(TEXT("frame"), Frame.Value);
    Root->SetNumberField(TEXT("tick_resolution_num"), Tick.Numerator);
    Root->SetNumberField(TEXT("tick_resolution_den"), Tick.Denominator);
    Root->SetNumberField(TEXT("value"), Value);
    Root->SetStringField(TEXT("interp"), Interp.ToLower());
    Root->SetBoolField(TEXT("section_added"), bSectionAdded);
    Root->SetBoolField(TEXT("had_key"), bHadKey);
    Root->SetNumberField(TEXT("prev_value"), PrevValue);
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (8) LIST TRACKS — bindings + tracks + sections + channels. READ.
// ============================================================================
FString UMCPReflectionLibrary::ListAnimationTracksJson(const FString& WidgetBlueprintPath, const FString& AnimName)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }

    // Map each possessable GUID -> the widget name from the FWidgetAnimationBinding table.
    TMap<FGuid, FString> GuidToWidget;
    for (const FWidgetAnimationBinding& B : Anim->AnimationBindings)
    {
        GuidToWidget.Add(B.AnimationGuid, B.WidgetName.ToString());
    }

    TArray<TSharedPtr<FJsonValue>> Bindings;
    for (const FMovieSceneBinding& Binding : MS->GetBindings())
    {
        TSharedRef<FJsonObject> BObj = MakeShared<FJsonObject>();
        const FGuid BGuid = Binding.GetObjectGuid();
        BObj->SetStringField(TEXT("animation_guid"), BGuid.ToString());
        if (const FString* WName = GuidToWidget.Find(BGuid))
        {
            BObj->SetStringField(TEXT("widget"), *WName);
        }
        BObj->SetStringField(TEXT("display_name"), MS->GetObjectDisplayName(BGuid).ToString());

        TArray<TSharedPtr<FJsonValue>> Tracks;
        for (UMovieSceneTrack* Track : Binding.GetTracks())
        {
            if (!Track)
            {
                continue;
            }
            TSharedRef<FJsonObject> TObj = MakeShared<FJsonObject>();
            TObj->SetStringField(TEXT("track_class"), Track->GetClass()->GetName());
            if (const UMovieScenePropertyTrack* PT = Cast<UMovieScenePropertyTrack>(Track))
            {
                TObj->SetStringField(TEXT("property_name"), PT->GetPropertyName().ToString());
            }

            TArray<TSharedPtr<FJsonValue>> Sections;
            for (const UMovieSceneSection* Section : Track->GetAllSections())
            {
                if (!Section)
                {
                    continue;
                }
                TSharedRef<FJsonObject> SObj = MakeShared<FJsonObject>();
                const TRange<FFrameNumber> Range = Section->GetRange();
                SObj->SetBoolField(TEXT("range_has_start"), Range.GetLowerBound().IsClosed());
                SObj->SetBoolField(TEXT("range_has_end"), Range.GetUpperBound().IsClosed());
                if (Range.GetLowerBound().IsClosed())
                {
                    SObj->SetNumberField(TEXT("start_frame"), Range.GetLowerBoundValue().Value);
                }
                if (Range.GetUpperBound().IsClosed())
                {
                    SObj->SetNumberField(TEXT("end_frame"), Range.GetUpperBoundValue().Value);
                }

                TArray<TSharedPtr<FJsonValue>> Channels;
                FMovieSceneChannelProxy& Proxy = const_cast<UMovieSceneSection*>(Section)->GetChannelProxy();
                for (const FMovieSceneChannelEntry& Entry : Proxy.GetAllEntries())
                {
                    const FName TypeName = Entry.GetChannelTypeName();
                    TArrayView<FMovieSceneChannel* const> Chs = Entry.GetChannels();
                    for (int32 Ci = 0; Ci < Chs.Num(); ++Ci)
                    {
                        TSharedRef<FJsonObject> CObj = MakeShared<FJsonObject>();
                        CObj->SetStringField(TEXT("channel_type"), TypeName.ToString());
                        CObj->SetNumberField(TEXT("index"), Ci);
                        CObj->SetNumberField(TEXT("num_keys"), Chs[Ci] ? Chs[Ci]->GetNumKeys() : 0);
                        Channels.Add(MakeShared<FJsonValueObject>(CObj));
                    }
                }
                SObj->SetNumberField(TEXT("channel_count"), Channels.Num());
                SObj->SetArrayField(TEXT("channels"), Channels);
                Sections.Add(MakeShared<FJsonValueObject>(SObj));
            }
            TObj->SetNumberField(TEXT("section_count"), Sections.Num());
            TObj->SetArrayField(TEXT("sections"), Sections);
            Tracks.Add(MakeShared<FJsonValueObject>(TObj));
        }
        BObj->SetNumberField(TEXT("track_count"), Tracks.Num());
        BObj->SetArrayField(TEXT("tracks"), Tracks);
        Bindings.Add(MakeShared<FJsonValueObject>(BObj));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("display_label"), Anim->GetDisplayLabel());
    Root->SetNumberField(TEXT("binding_count"), Bindings.Num());
    Root->SetNumberField(TEXT("possessable_count"), MS->GetPossessableCount());
    Root->SetArrayField(TEXT("bindings"), Bindings);
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (9) REMOVE TRACK — inverse of AddAnimationTrackJson. Resolve the binding GUID +
//     the track of TrackType on it, then UMovieScene::RemoveTrack (MovieScene.cpp:1221,
//     which also drops the track's sections). Non-ledgered: this IS an undo-inverse
//     invoked by editor_level.undo (also exposed as a normal tool).
// ============================================================================
FString UMCPReflectionLibrary::RemoveAnimationTrackJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    UClass* TrackCls = MCPWAn_TrackClassFor(TrackType);
    if (!TrackCls)
    {
        return MCPWAn_Error(FString::Printf(TEXT("unknown track_type '%s' (want opacity|color|transform|visibility)"), *TrackType));
    }
    const FGuid Guid = MCPWAn_BindingGuidFor(Anim, FName(*WidgetName));
    if (!Guid.IsValid())
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget '%s' is not bound to animation '%s'"), *WidgetName, *AnimName));
    }
    UMovieSceneTrack* Track = MS->FindTrack(TrackCls, Guid);
    if (!Track)
    {
        // Nothing to remove — report cleanly (idempotent inverse).
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetStringField(TEXT("blueprint"), WBP->GetName());
        R->SetBoolField(TEXT("removed"), false);
        R->SetBoolField(TEXT("found"), false);
        R->SetStringField(TEXT("anim_name"), Anim->GetName());
        R->SetStringField(TEXT("widget"), WidgetName);
        R->SetStringField(TEXT("track_type"), TrackType.ToLower());
        R->SetStringField(TEXT("animation_guid"), Guid.ToString());
        return MCPWAn_Serialize(R);
    }

    Anim->Modify();
    MS->Modify();

    const bool bRemoved = MS->RemoveTrack(*Track);

    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), bRemoved);
    Root->SetBoolField(TEXT("found"), true);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("track_type"), TrackType.ToLower());
    Root->SetStringField(TEXT("track_class"), TrackCls->GetName());
    Root->SetStringField(TEXT("animation_guid"), Guid.ToString());
    Root->SetNumberField(TEXT("track_count"), MCPWAn_TrackCount(MS));
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}

// ============================================================================
// (10) REMOVE KEY — inverse of AddAnimationKeyJson. Resolve track+section+channel
//     (same float-first-then-bool logic), convert TimeSeconds->frame via the
//     MovieScene tick resolution (the section shares the MovieScene's resolution),
//     and remove the key at that frame on ChannelIndex. Non-ledgered undo-inverse
//     (also exposed as a normal tool). Removes across every section of the track
//     that carries a key at the frame (normally exactly one).
//     NB: the wanim_add_key ledger stores frame + tick_resolution_num/den (not
//     seconds); the undo branch reconstructs seconds = frame * den / num, which
//     AsFrameNumber maps back to the same frame exactly.
// ============================================================================
FString UMCPReflectionLibrary::RemoveAnimationKeyJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType, int32 ChannelIndex, float TimeSeconds)
{
#if WITH_EDITOR
#if WITH_EDITORONLY_DATA
    UWidgetBlueprint* WBP = MCPWAn_LoadWBP(WidgetBlueprintPath);
    if (!WBP)
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget blueprint not found: %s"), *WidgetBlueprintPath));
    }
    UWidgetAnimation* Anim = MCPWAn_FindAnim(WBP, AnimName);
    if (!Anim)
    {
        return MCPWAn_Error(FString::Printf(TEXT("animation not found: %s"), *AnimName));
    }
    UMovieScene* MS = Anim->GetMovieScene();
    if (!MS)
    {
        return MCPWAn_Error(TEXT("animation has no MovieScene"));
    }
    UClass* TrackCls = MCPWAn_TrackClassFor(TrackType);
    if (!TrackCls)
    {
        return MCPWAn_Error(FString::Printf(TEXT("unknown track_type '%s'"), *TrackType));
    }
    const FGuid Guid = MCPWAn_BindingGuidFor(Anim, FName(*WidgetName));
    if (!Guid.IsValid())
    {
        return MCPWAn_Error(FString::Printf(TEXT("widget '%s' is not bound"), *WidgetName));
    }
    UMovieSceneTrack* Track = MS->FindTrack(TrackCls, Guid);
    if (!Track)
    {
        return MCPWAn_Error(FString::Printf(TEXT("no %s track on widget '%s'"), *TrackType, *WidgetName));
    }

    const FFrameRate Tick = MS->GetTickResolution();
    const FFrameNumber Frame = Tick.AsFrameNumber((double)TimeSeconds);

    int32 RemovedCount = 0;
    FString ChannelType;

    for (UMovieSceneSection* Section : Track->GetAllSections())
    {
        if (!Section)
        {
            continue;
        }
        FMovieSceneChannelProxy& Proxy = Section->GetChannelProxy();

        TArrayView<FMovieSceneFloatChannel*> FloatChs = Proxy.GetChannels<FMovieSceneFloatChannel>();
        if (FloatChs.Num() > 0)
        {
            const int32 Idx = FMath::Clamp(ChannelIndex, 0, FloatChs.Num() - 1);
            FMovieSceneFloatChannel* Ch = FloatChs[Idx];
            if (Ch)
            {
                TMovieSceneChannelData<FMovieSceneFloatValue> Data = Ch->GetData();
                const int32 KeyIdx = Data.FindKey(Frame);
                if (KeyIdx != INDEX_NONE)
                {
                    Section->Modify();
                    Data.RemoveKey(KeyIdx);
                    ++RemovedCount;
                    ChannelType = TEXT("float");
                }
            }
        }
        else
        {
            TArrayView<FMovieSceneBoolChannel*> BoolChs = Proxy.GetChannels<FMovieSceneBoolChannel>();
            if (BoolChs.Num() > 0)
            {
                const int32 Idx = FMath::Clamp(ChannelIndex, 0, BoolChs.Num() - 1);
                FMovieSceneBoolChannel* Ch = BoolChs[Idx];
                if (Ch)
                {
                    TMovieSceneChannelData<bool> Data = Ch->GetData();
                    const int32 KeyIdx = Data.FindKey(Frame);
                    if (KeyIdx != INDEX_NONE)
                    {
                        Section->Modify();
                        Data.RemoveKey(KeyIdx);
                        ++RemovedCount;
                        ChannelType = TEXT("bool");
                    }
                }
            }
        }
    }

    if (RemovedCount > 0)
    {
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(WBP);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("blueprint"), WBP->GetName());
    Root->SetBoolField(TEXT("removed"), RemovedCount > 0);
    Root->SetNumberField(TEXT("removed_count"), RemovedCount);
    Root->SetStringField(TEXT("anim_name"), Anim->GetName());
    Root->SetStringField(TEXT("widget"), WidgetName);
    Root->SetStringField(TEXT("track_type"), TrackType.ToLower());
    Root->SetStringField(TEXT("channel_type"), ChannelType);
    Root->SetNumberField(TEXT("channel_index"), ChannelIndex);
    Root->SetNumberField(TEXT("time_seconds"), TimeSeconds);
    Root->SetNumberField(TEXT("frame"), Frame.Value);
    Root->SetNumberField(TEXT("tick_resolution_num"), Tick.Numerator);
    Root->SetNumberField(TEXT("tick_resolution_den"), Tick.Denominator);
    return MCPWAn_Serialize(Root);
#else
    return MCPWAn_Error(TEXT("editor-only (no WITH_EDITORONLY_DATA)"));
#endif
#else
    return MCPWAn_Error(TEXT("editor-only"));
#endif
}
