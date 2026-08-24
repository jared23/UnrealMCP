// ============================================================================
// MCPReflection_NiagaraExt.cpp  —  Niagara renderer-property / renderer-binding /
//   emitter-handle duplicate + reorder authoring handlers.
// ----------------------------------------------------------------------------
// AUTHORED on Windows 2026-08-18. **ISOLATED translation unit** on purpose:
// implements the four DEFERRED MCPReflectionLibrary methods that niagara_write2.py
// hasattr-guards on (set_niagara_renderer_property / set_niagara_renderer_binding /
// duplicate_niagara_emitter_handle / reorder_niagara_emitter_handle). When these
// four link, the Python tools auto-enable.
//
// >>> LINK RISK — DELIBERATELY LOW <<<
//   Every engine symbol used here is **runtime-Niagara (NIAGARA_API)**, already
//   linked by C++ #5/#10. NO NiagaraEditor-internal symbol is referenced, so this
//   file does NOT depend on the NiagaraEditor export patch. Verified against 5.8
//   source (paths in the VERIFY tags). If any single call ever fails to link, drop
//   just this .cpp; the rest of the plugin still builds.
//
//   Confirmed 5.8 signatures (all NIAGARA_API, runtime module "Niagara"):
//     UNiagaraSystem::GetEmitterHandles()            -> TArray<FNiagaraEmitterHandle>&   (NON-const; mutable — enables reorder)   NiagaraSystem.h:313
//     UNiagaraSystem::DuplicateEmitterHandle(const FNiagaraEmitterHandle&, FName) -> FNiagaraEmitterHandle  (appends + returns)   NiagaraSystem.h:339
//     FNiagaraEmitterHandle::GetName()/GetId()/GetInstance()                                                                       NiagaraEmitterHandle.h:56/49/82
//     FVersionedNiagaraEmitter::GetEmitterData() -> FVersionedNiagaraEmitterData*   ; ::ToBase() -> FVersionedNiagaraEmitterBase   NiagaraTypes.h:1807/1809
//     FVersionedNiagaraEmitterData::GetRenderers() -> const TArray<UNiagaraRendererProperties*>&                                   NiagaraEmitter.h
//     FNiagaraVariableAttributeBinding::SetValue(const FName&, const FVersionedNiagaraEmitterBase&, ENiagaraRendererSourceDataMode) NiagaraCommon.h:1350
//     FNiagaraVariableAttributeBinding::GetParamMapBindableVariable() -> const FNiagaraVariableBase&                                NiagaraCommon.h:1379
//     UNiagaraRendererProperties::GetCurrentSourceMode() -> ENiagaraRendererSourceDataMode  (virtual)                              NiagaraRendererProperties.h:429
//     UNiagaraSystem::RequestCompile(bool)  (used by C++ #5 already)                                                                NiagaraSystem.h
//
// All handlers: null-guarded, WITH_EDITOR-guarded, return {"error":...} on any
// miss (never crash), and provide the exact success fields each Python caller reads
// ({prev} / {added_handle_name,added_handle_id} / {prev_index}).
// Anon-namespace helpers are prefixed MCPNiaRP_ (unique across the unity build).
// ============================================================================

#include "MCPReflectionLibrary.h"

// --- JSON ---
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

// --- Reflection / core ---
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/Package.h"
#include "UObject/SoftObjectPath.h"
#include "Misc/PackageName.h"

// --- Niagara (runtime module only) ---
#include "NiagaraSystem.h"                  // UNiagaraSystem
#include "NiagaraEmitter.h"                 // UNiagaraEmitter / FVersionedNiagaraEmitterData
#include "NiagaraEmitterBase.h"             // FVersionedNiagaraEmitterBase (SetValue arg)
#include "NiagaraEmitterHandle.h"           // FNiagaraEmitterHandle
#include "NiagaraCommon.h"                  // FNiagaraVariableAttributeBinding
#include "NiagaraTypes.h"                   // FVersionedNiagaraEmitter / FNiagaraVariableBase
#include "NiagaraRendererProperties.h"      // UNiagaraRendererProperties / ENiagaraRendererSourceDataMode

namespace
{
    // ---- JSON helpers (prefixed for unity-build uniqueness) ----------------
    FString MCPNiaRP_Serialize(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, Writer);
        return Out;
    }

    // Error JSON MUST carry an "error" field: the Python callers branch on res.get("error").
    FString MCPNiaRP_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetStringField(TEXT("error"), Message);
        return MCPNiaRP_Serialize(Obj);
    }

    // Resolve a UNiagaraSystem from a package/asset path string (renderer_property/binding
    // Python callers pass system_path as a STRING, not a loaded object).
    UNiagaraSystem* MCPNiaRP_LoadSystem(const FString& Path)
    {
        if (Path.IsEmpty())
        {
            return nullptr;
        }
        // FSoftObjectPath resolves both "/Game/FX/S" and "/Game/FX/S.S" forms.
        if (UNiagaraSystem* Sys = Cast<UNiagaraSystem>(FSoftObjectPath(Path).TryLoad()))
        {
            return Sys;
        }
        // Package-only path: retry with an explicit "<pkg>.<shortname>" object path.
        if (!Path.Contains(TEXT(".")))
        {
            const FString ObjPath = Path + TEXT(".") + FPackageName::GetShortName(Path);
            return Cast<UNiagaraSystem>(FSoftObjectPath(ObjPath).TryLoad());
        }
        return nullptr;
    }

    // Local emitter-handle finder (MCP_FindEmitterHandleByName lives in another TU's
    // anon namespace; re-implemented here to keep this file self-contained).
    FNiagaraEmitterHandle* MCPNiaRP_FindHandle(UNiagaraSystem* System, const FString& Name, int32& OutIndex)
    {
        OutIndex = INDEX_NONE;
        if (!System)
        {
            return nullptr;
        }
        const TArray<FNiagaraEmitterHandle>& Handles = System->GetEmitterHandles();   // VERIFY vs engine source: NiagaraSystem.h:314
        for (int32 i = 0; i < Handles.Num(); ++i)
        {
            if (Handles[i].GetName().ToString() == Name)                              // VERIFY vs engine source: NiagaraEmitterHandle.h:56 — GetName() -> FName
            {
                OutIndex = i;
                return &System->GetEmitterHandle(i);                                  // VERIFY vs engine source: NiagaraSystem.h:350 — non-const GetEmitterHandle(int)
            }
        }
        return nullptr;
    }

    // value_json arrives as a small JSON scalar/string (e.g. "5", "Additive", "\"Additive\"",
    // "true"). ImportText wants the bare token, so strip one layer of surrounding double quotes.
    FString MCPNiaRP_Unquote(const FString& In)
    {
        FString S = In;
        S.TrimStartAndEndInline();
        if (S.Len() >= 2 && S.StartsWith(TEXT("\"")) && S.EndsWith(TEXT("\"")))
        {
            S = S.Mid(1, S.Len() - 2).ReplaceEscapedCharWithChar();
        }
        return S;
    }
}

// ---------------------------------------------------------------------------
// 1) SetNiagaraRendererProperty(SystemPath, EmitterName, RendererIndex, PropertyName, ValueJson)
//    Sets a UPROPERTY by name on emitter[EmitterName].GetRenderers()[RendererIndex].
//    Returns {"prev": <old ExportText value>, ...} for undo capture (or {"error"}).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraRendererProperty(const FString& SystemPath, const FString& EmitterName,
    int32 RendererIndex, const FString& PropertyName, const FString& ValueJson)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNiaRP_LoadSystem(SystemPath);
    if (!System)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath));
    }

    int32 HandleIndex = INDEX_NONE;
    FNiagaraEmitterHandle* Handle = MCPNiaRP_FindHandle(System, EmitterName, HandleIndex);
    if (!Handle)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName));
    }

    FVersionedNiagaraEmitter Instance = Handle->GetInstance();                         // VERIFY vs engine source: NiagaraEmitterHandle.h:82
    FVersionedNiagaraEmitterData* Data = Instance.GetEmitterData();                    // VERIFY vs engine source: NiagaraTypes.h:1807
    if (!Data)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("emitter '%s' has no versioned data"), *EmitterName));
    }

    const TArray<UNiagaraRendererProperties*>& Renderers = Data->GetRenderers();       // VERIFY vs engine source: NiagaraEmitter.h — GetRenderers()
    if (RendererIndex < 0 || RendererIndex >= Renderers.Num())
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("renderer index %d out of range (count %d)"), RendererIndex, Renderers.Num()));
    }
    UNiagaraRendererProperties* Renderer = Renderers[RendererIndex];
    if (!Renderer)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("renderer %d is null"), RendererIndex));
    }

    FProperty* Prop = FindFProperty<FProperty>(Renderer->GetClass(), *PropertyName);
    if (!Prop)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("property '%s' not found on %s"), *PropertyName, *Renderer->GetClass()->GetName()));
    }
    void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Renderer);
    if (!ValuePtr)
    {
        return MCPNiaRP_Error(TEXT("null value ptr"));
    }

    // Capture prior value (bounded — concrete property, no unbounded recursion).
    FString Prev;
    Prop->ExportTextItem_Direct(Prev, ValuePtr, /*Default*/ nullptr, /*Parent*/ Renderer, PPF_None); // VERIFY vs engine source: FProperty::ExportTextItem_Direct

    Renderer->Modify();
    const FString NewText = MCPNiaRP_Unquote(ValueJson);
    const TCHAR* Res = Prop->ImportText_Direct(*NewText, ValuePtr, Renderer, PPF_None, nullptr);      // VERIFY vs engine source: FProperty::ImportText_Direct
    if (Res == nullptr)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("ImportText failed for value '%s' on property '%s'"), *NewText, *PropertyName));
    }

    Renderer->PostEditChange();
    System->RequestCompile(false);                                                     // VERIFY vs engine source: NiagaraSystem.h — RequestCompile(bool)
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetNumberField(TEXT("renderer_index"), RendererIndex);
    Root->SetStringField(TEXT("property"), PropertyName);
    Root->SetStringField(TEXT("prev"), Prev);   // Python: json.dumps(res.get("prev")) -> prev_value_json for inverse re-set
    return MCPNiaRP_Serialize(Root);
#else
    return MCPNiaRP_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 2) SetNiagaraRendererBinding(SystemPath, EmitterName, RendererIndex, BindingName, SourceValue)
//    Sets an FNiagaraVariableAttributeBinding named BindingName on the renderer to bind
//    attribute SourceValue. Returns {"prev": <old bound var name>, ...} (or {"error"}).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::SetNiagaraRendererBinding(const FString& SystemPath, const FString& EmitterName,
    int32 RendererIndex, const FString& BindingName, const FString& SourceValue)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNiaRP_LoadSystem(SystemPath);
    if (!System)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath));
    }

    int32 HandleIndex = INDEX_NONE;
    FNiagaraEmitterHandle* Handle = MCPNiaRP_FindHandle(System, EmitterName, HandleIndex);
    if (!Handle)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName));
    }

    FVersionedNiagaraEmitter Instance = Handle->GetInstance();
    FVersionedNiagaraEmitterData* Data = Instance.GetEmitterData();
    if (!Data)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("emitter '%s' has no versioned data"), *EmitterName));
    }

    const TArray<UNiagaraRendererProperties*>& Renderers = Data->GetRenderers();
    if (RendererIndex < 0 || RendererIndex >= Renderers.Num())
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("renderer index %d out of range (count %d)"), RendererIndex, Renderers.Num()));
    }
    UNiagaraRendererProperties* Renderer = Renderers[RendererIndex];
    if (!Renderer)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("renderer %d is null"), RendererIndex));
    }

    // Locate the named struct property and confirm it is an attribute binding.
    FStructProperty* StructProp = FindFProperty<FStructProperty>(Renderer->GetClass(), *BindingName);
    if (!StructProp || !StructProp->Struct ||
        StructProp->Struct->GetFName() != TEXT("NiagaraVariableAttributeBinding"))
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("binding '%s' not found (or not an FNiagaraVariableAttributeBinding) on %s"),
            *BindingName, *Renderer->GetClass()->GetName()));
    }
    FNiagaraVariableAttributeBinding* Binding =
        StructProp->ContainerPtrToValuePtr<FNiagaraVariableAttributeBinding>(Renderer);
    if (!Binding)
    {
        return MCPNiaRP_Error(TEXT("null binding ptr"));
    }

    // Best-effort prev: the currently-bound parameter-map variable name (for inverse re-bind).
    const FString Prev = Binding->GetParamMapBindableVariable().GetName().ToString();  // VERIFY vs engine source: NiagaraCommon.h:1379 / NiagaraTypes.h:1328 (GetName()->const FName&)

    Renderer->Modify();
    // Non-deprecated overload takes FVersionedNiagaraEmitterBase (5.7+); ToBase() supplies it.
    Binding->SetValue(FName(*SourceValue), Instance.ToBase(), Renderer->GetCurrentSourceMode()); // VERIFY vs engine source: NiagaraCommon.h:1350 + NiagaraTypes.h:1809 + NiagaraRendererProperties.h:429

    Renderer->PostEditChange();
    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetNumberField(TEXT("renderer_index"), RendererIndex);
    Root->SetStringField(TEXT("binding"), BindingName);
    Root->SetStringField(TEXT("prev"), Prev);   // Python: stored as prev_source; inverse restores it
    return MCPNiaRP_Serialize(Root);
#else
    return MCPNiaRP_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 3) DuplicateNiagaraEmitterHandle(System, EmitterName, NewName)
//    Duplicates the handle named EmitterName under NewName (auto-generated if empty).
//    Returns {"added_handle_name","added_handle_id", ...} (or {"error"}).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::DuplicateNiagaraEmitterHandle(UNiagaraSystem* System, const FString& EmitterName,
    const FString& NewName)
{
#if WITH_EDITOR
    if (!System)
    {
        return MCPNiaRP_Error(TEXT("null system"));
    }

    int32 HandleIndex = INDEX_NONE;
    FNiagaraEmitterHandle* Found = MCPNiaRP_FindHandle(System, EmitterName, HandleIndex);
    if (!Found)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName));
    }

    // Copy the source handle BEFORE DuplicateEmitterHandle emplaces into EmitterHandles
    // (an Emplace can reallocate the array and dangle a reference into it).
    const FNiagaraEmitterHandle SourceCopy = *Found;

    // Choose a unique target name.
    FString DesiredName = NewName;
    if (DesiredName.IsEmpty())
    {
        DesiredName = EmitterName + TEXT("_Copy");
    }
    {
        auto NameTaken = [System](const FString& N) -> bool
        {
            for (const FNiagaraEmitterHandle& H : System->GetEmitterHandles())
            {
                if (H.GetName().ToString() == N) { return true; }
            }
            return false;
        };
        FString Candidate = DesiredName;
        int32 Suffix = 1;
        while (NameTaken(Candidate))
        {
            Candidate = FString::Printf(TEXT("%s_%d"), *DesiredName, Suffix++);
        }
        DesiredName = Candidate;
    }

    System->Modify();
    // VERIFY vs engine source: NiagaraSystem.h:339 — DuplicateEmitterHandle(const FNiagaraEmitterHandle&, FName)
    // appends the new handle to EmitterHandles and returns it (NiagaraSystem.cpp:3040).
    const FNiagaraEmitterHandle NewHandle = System->DuplicateEmitterHandle(SourceCopy, FName(*DesiredName));

    const FString AddedName = NewHandle.GetName().ToString();
    const FString AddedId   = NewHandle.GetId().ToString();

    System->RequestCompile(false);
    System->MarkPackageDirty();

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("added_handle_name"), AddedName);
    Root->SetStringField(TEXT("added_handle_id"), AddedId);
    Root->SetNumberField(TEXT("emitter_count"), System->GetEmitterHandles().Num());
    return MCPNiaRP_Serialize(Root);
#else
    return MCPNiaRP_Error(TEXT("editor-only"));
#endif
}

// ---------------------------------------------------------------------------
// 4) ReorderNiagaraEmitterHandle(System, EmitterName, NewIndex)
//    Moves the handle named EmitterName to NewIndex in the system's handle array.
//    Returns {"prev_index", ...} (or {"error"}).
// ---------------------------------------------------------------------------
FString UMCPReflectionLibrary::ReorderNiagaraEmitterHandle(UNiagaraSystem* System, const FString& EmitterName,
    int32 NewIndex)
{
#if WITH_EDITOR
    if (!System)
    {
        return MCPNiaRP_Error(TEXT("null system"));
    }

    // Mutable handle array (NiagaraSystem.h:313 — non-const overload returns EmitterHandles&).
    TArray<FNiagaraEmitterHandle>& Handles = System->GetEmitterHandles();
    const int32 Count = Handles.Num();

    int32 PrevIndex = INDEX_NONE;
    for (int32 i = 0; i < Count; ++i)
    {
        if (Handles[i].GetName().ToString() == EmitterName)
        {
            PrevIndex = i;
            break;
        }
    }
    if (PrevIndex == INDEX_NONE)
    {
        return MCPNiaRP_Error(FString::Printf(TEXT("no emitter named '%s'"), *EmitterName));
    }

    // Clamp the destination into [0, Count-1].
    int32 Dest = NewIndex;
    if (Dest < 0) { Dest = 0; }
    if (Dest > Count - 1) { Dest = Count - 1; }

    if (Dest != PrevIndex)
    {
        System->Modify();
        const FNiagaraEmitterHandle Moved = Handles[PrevIndex];             // copy out before removing
        Handles.RemoveAt(PrevIndex, 1, EAllowShrinking::No);                // VERIFY vs engine source: TArray::RemoveAt(int32,int32,EAllowShrinking) (UE5.5+ signature)
        Handles.Insert(Moved, Dest);
        System->RequestCompile(false);   // rebuild EmitterExecutionOrder etc.
        System->MarkPackageDirty();
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetStringField(TEXT("emitter"), EmitterName);
    Root->SetNumberField(TEXT("prev_index"), PrevIndex);
    Root->SetNumberField(TEXT("new_index"), Dest);
    Root->SetNumberField(TEXT("emitter_count"), Count);
    return MCPNiaRP_Serialize(Root);
#else
    return MCPNiaRP_Error(TEXT("editor-only"));
#endif
}
