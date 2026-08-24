// Clean-room reflection helpers for the UnrealMCP plugin.
//
// Exposes FProperty / UClass metadata that the stock Python API does NOT surface
// (cpp_type, Category, Clamp/UI ranges, edit flags, a class's abstract flag). Being
// BlueprintCallable statics, Unreal auto-generates Python bindings on compile, so the
// Python UserTools can call e.g.:
//     import unreal
//     j = unreal.MCPReflectionLibrary.get_object_property_metadata_json(obj)
//     meta = json.loads(j)
// No TCP-protocol changes are needed — this rides the existing execute_python channel.
#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "MCPReflectionLibrary.generated.h"

UCLASS()
class UNREALMCP_API UMCPReflectionLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:
    /**
     * Return, as a JSON string, per-property reflection metadata for an object's class:
     * for each UPROPERTY -> { name, cpp_type, category, clamp_min, clamp_max, ui_min,
     * ui_max, tooltip, flags[] }. `bIncludeInherited` walks base classes too.
     * Shape: {"class": "...", "properties": [ {...}, ... ]} (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetObjectPropertyMetadataJson(UObject* Object, bool bIncludeInherited = true);

    /**
     * Return, as a JSON string, class-level metadata for a UClass:
     * { name, path, parent_class, is_abstract, is_deprecated, is_blueprintable }.
     * (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetClassMetadataJson(UClass* Class);

    // ---- STAGED 2026-08-14: authored on the Mac, NOT YET COMPILED on Windows. -------------
    // Include both in the next plugin recompile; then wire structs.py / datatables.py (field
    // schema) and get_mesh_info (sockets) to call them (they currently infer/omit those).

    /**
     * Field schema for a UStruct — pass a loaded UserDefinedStruct asset (it is a UScriptStruct):
     * per member { name, cpp_type, category?, tooltip?, flags[] }.
     * Shape {"struct": "...", "path": "...", "fields": [ {...}, ... ]} (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStructFieldsJson(UScriptStruct* Struct);

    /**
     * Sockets on a StaticMesh: per socket { name, tag, location[x,y,z], rotation[pitch,yaw,roll],
     * scale[x,y,z] }. Shape {"mesh": "...", "sockets": [ {...}, ... ]} (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStaticMeshSocketsJson(UStaticMesh* Mesh);

    // ---- STAGED C++ #3 2026-08-14: authored on the Mac, NOT YET COMPILED on Windows. --------
    // Unlocks protected UPROPERTYs that stock Python reflection cannot read. After compile,
    // wire (hasattr-guarded) into curves_read / texture_read / physics_read. All three use only
    // public Engine headers (curves, textures, physics are all in the Engine module the plugin
    // already depends on) — no Build.cs dependency change expected.

    /**
     * Authored keyframes of a curve asset (UCurveFloat / UCurveVector / UCurveLinearColor).
     * Per channel { name, keys: [ { time, value, interp_mode, tangent_mode, arrive_tangent,
     * leave_tangent } ] }. Shape {"curve": "...", "class": "...", "channels": [ {...} ]}.
     * (Stock Python cannot read the protected FRichCurve members.)
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetCurveKeysJson(UCurveBase* Curve);

    /**
     * Texture2D detail the stock Python API cannot reach: built + imported + (editor-only) source
     * dimensions, pixel_format, num_mips, source_format. Shape {"texture": "...", "width":..,
     * "height":.., "num_mips":.., "pixel_format":"..", ...} (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetTextureInfoJson(UTexture2D* Texture);

    /**
     * Physics bodies of a UPhysicsAsset (protected SkeletalBodySetups). Per body { bone, sphere/
     * box/capsule/convex counts + basic primitive dims }. Shape {"physics_asset": "...",
     * "bodies": [ {...} ]} (or {"error": "..."}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetPhysicsBodiesJson(UPhysicsAsset* PhysicsAsset);

    // ---- C++ #4 2026-08-15 (Wave-3 batch 1: DATA-ASSET AUTHORING) — COMPILED on Windows + WIRED +
    // VERIFIED LIVE (needed 2 include-path fixes, see .cpp). WRITE counterparts that mutate protected / editor-only data the
    // stock Python API cannot reach. All use modules the plugin ALREADY links (Engine for curves;
    // UnrealEd + BlueprintGraph for FStructureEditorUtils / FEnumEditorUtils / FEdGraphPinType) ->
    // NO Build.cs dependency change expected. After compile, wire (hasattr-guarded) into
    // curves_write (keys) and structs_write (fields / enum entries). Reversible ops:
    //   set_curve_keys  (inverse: set_curve_keys with prior keys captured via GetCurveKeysJson)
    //   add_struct_field (inverse: RemoveStructField by name) / add_enum_entry (inverse: RemoveEnumEntry by index)

    /**
     * Overwrite the authored keys of a curve asset (UCurveFloat / UCurveVector / UCurveLinearColor)
     * from JSON: {"channels":[ {"index":0,"keys":[ {"time","value","interp_mode"?,"tangent_mode"?,
     * "arrive_tangent"?,"leave_tangent"?}, ... ]}, ... ]}. Only listed channels are rewritten (each
     * is fully reset then rebuilt from its keys). interp_mode = Linear|Constant|Cubic;
     * tangent_mode = Auto|User|Break. Marks the package dirty. Returns {"curve","channels_written",
     * "keys_written"} (or {"error"}). Inverse-friendly: capture prior state via GetCurveKeysJson first.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetCurveKeysJson(UCurveBase* Curve, const FString& KeysJson);

    /**
     * Add a member field to a UserDefinedStruct. TypeName (case-insensitive) one of:
     * bool | byte | int | int64 | float | name | string | text | vector | rotator | transform |
     * linearcolor | vector2d. The new field is renamed to FieldName. Marks the package dirty.
     * Returns {"struct","added_field","type","guid","field_count"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddStructField(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeName);

    /**
     * Remove a member field (matched by friendly name or internal VarName) from a UserDefinedStruct.
     * Returns {"struct","removed_field","removed","field_count"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveStructField(UUserDefinedStruct* Struct, const FString& FieldName);

    /**
     * Add an enumerator (with DisplayName) to a UserDefinedEnum. Returns {"enum","added_entry",
     * "index","entry_count"} (or {"error"}). entry_count excludes the hidden _MAX sentinel.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddEnumEntry(UUserDefinedEnum* Enum, const FString& DisplayName);

    /**
     * Remove the enumerator at Index from a UserDefinedEnum (0-based, excluding the hidden _MAX).
     * Returns {"enum","removed_index","entry_count"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveEnumEntry(UUserDefinedEnum* Enum, int32 Index);

    // ---- C++ #5 2026-08-15 (Wave-3 batch 2: NIAGARA AUTHORING) — COMPILED + WIRED + VERIFIED LIVE
    // (1 signature fix: AddEmitterToSystem needs the source's version GUID, see .cpp). Adds emitter
    // authoring to a NiagaraSystem asset. **REQUIRED a
    // Build.cs change** (already made in the repo): + "Niagara", "NiagaraEditor" module deps. The
    // Niagara emitter-handle API is VERSION-SENSITIVE (versioned FNiagaraEmitter data in 5.8) — the
    // Windows build should VERIFY the calls marked "VERIFY vs engine source" in the .cpp against
    // NiagaraSystem.h / NiagaraEmitterHandle.h / NiagaraEditorUtilities.h and adjust if the signatures
    // differ in this engine build (expect a possible iteration). After compile+verify, wire into
    // niagara_write (add_emitter / remove_emitter) with ledger op add_emitter (inverse: remove by id).

    /**
     * Add a COPY of SourceEmitter to System as a new emitter handle (HandleName optional label).
     * Returns {system, added_handle_name, added_handle_id, emitter_count} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddEmitterToSystem(UNiagaraSystem* System, UNiagaraEmitter* SourceEmitter, const FString& HandleName);

    /**
     * Remove the emitter handle whose name OR id == HandleNameOrId from System.
     * Returns {system, removed, emitter_count} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveEmitterFromSystem(UNiagaraSystem* System, const FString& HandleNameOrId);

    // ---- C++ #6 2026-08-15 (gameplay-tag authoring) — COMPILED CLEAN (no edits) + WIRED + VERIFIED
    // (on-disk DefaultGameplayTags.ini add/remove confirmed).
    // Probe confirmed no Python path (editing GameplayTagsSettings.GameplayTagList in-memory does NOT
    // register with the live manager + no config-persist exposed). **REQUIRES Build.cs += "GameplayTags",
    // "GameplayTagsEditor"** (already in the repo). Uses the editor module's INI path, which both writes
    // DefaultGameplayTags.ini AND refreshes the manager. Calls tagged "VERIFY vs engine source" in the
    // .cpp are version-sensitive. After compile, wire into a gameplay_tags_write module.

    /**
     * Author a native gameplay tag (writes DefaultGameplayTags.ini + refreshes the manager) via
     * IGameplayTagsEditorModule::AddNewGameplayTagToINI. Returns {tag, added, comment} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddGameplayTag(const FString& TagName, const FString& Comment);

    /**
     * Remove a gameplay tag from the INI via IGameplayTagsEditorModule::DeleteTagFromINI (resolved by
     * UGameplayTagsManager::FindTagNode). Returns {tag, removed} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveGameplayTag(const FString& TagName);

    // ---- C++ #7 2026-08-15 (Kismet BP variable/node helper) — COMPILED CLEAN (no edits) + WIRED +
    // VERIFIED LIVE. Unblocks the deferred blueprints_write `add_blueprint_variable` / `add_event_override`
    // (the Python ADDs work but had no faithful REMOVE). Uses `FBlueprintEditorUtils` (module UnrealEd,
    // already a dep) + `UK2Node_Event` (BlueprintGraph, already a dep) → **NO Build.cs change.** Reuses
    // the C++ #4 `BuildPinType` helper so the variable ADD is clean too. VERIFY-tagged calls in the .cpp
    // are version-sensitive. After compile, wire add/remove_blueprint_variable + add_event_override.

    /**
     * Add a member variable of TypeName (bool/byte/int/int64/float/name/string/text/vector/vector2d/
     * rotator/transform/linearcolor) to a Blueprint. Returns {blueprint, added, variable, type}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddBlueprintVariable(UBlueprint* Blueprint, const FString& VarName, const FString& TypeName);

    /** Remove a member variable (by name) from a Blueprint. Returns {blueprint, found, removed, variable}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveBlueprintVariable(UBlueprint* Blueprint, const FString& VarName);

    /**
     * Remove an event node (matched by event name) from a Blueprint's ubergraph — the inverse of
     * BlueprintEditorLibrary.add_event_override. Returns {blueprint, found, removed, event}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveEventNode(UBlueprint* Blueprint, const FString& EventName);

    // ---- C++ #8 2026-08-15 (widget-tree authoring) — COMPILED + FIXED (GUID-map sync) + WIRED +
    // coordinator-VERIFIED (built RootCanvas>[Title,Box>Inner], removed leaf + panel-w-child, undo, 0 log warnings).
    // Probe confirmed: constructing widgets + panel.add_child ARE Python-reachable, but the WidgetTree's
    // RootWidget is a PROTECTED member (get/set_editor_property fail) — so an empty WidgetBlueprint's tree
    // can't be started from Python. These handlers do the whole thing in C++ (which can touch RootWidget).
    // **REQUIRES Build.cs += "UMG", "UMGEditor"** (already in the repo). VERIFY-tagged calls in the .cpp.

    /**
     * Add a widget of WidgetClassPath (e.g. "/Script/UMG.CanvasPanel", "/Script/UMG.TextBlock") to a
     * WidgetBlueprint. If ParentName is empty and the tree has no root, the new widget becomes the ROOT;
     * otherwise it is added as a child of the named panel (or the root panel if ParentName empty).
     * Returns {blueprint, added, name, is_root, parent} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddWidgetToBlueprint(UWidgetBlueprint* WidgetBlueprint, const FString& WidgetClassPath,
                                        const FString& NewName, const FString& ParentName);

    /**
     * Remove a widget (by name) from a WidgetBlueprint's tree (clears the root if it is the root).
     * Returns {blueprint, found, removed, name} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveWidgetFromBlueprint(UWidgetBlueprint* WidgetBlueprint, const FString& Name);

    // ---- C++ #9 2026-08-15 (BP event-node reader + guid-remove) — COMPILED CLEAN (no edits) + WIRED +
    // VERIFIED (0 log warnings). Unblocks a FAITHFUL blueprints_write.add_event_override: the reader
    // lets the Python side diff a Blueprint's event nodes before/after the add to find EXACTLY the node
    // added; RemoveEventNodeByGuid is its precise inverse. Uses FBlueprintEditorUtils (UnrealEd) +
    // UK2Node_Event (BlueprintGraph), both already deps → **NO Build.cs change.** VERIFY-tagged in .cpp.

    /**
     * List a Blueprint's event nodes: per node { event_name, is_custom, graph, node_guid }.
     * Shape {"blueprint":"...","event_count":N,"events":[{...}]}. Read-only.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintEventNodesJson(UBlueprint* Blueprint);

    /**
     * Remove the event node whose NodeGuid == NodeGuidStr from a Blueprint (precise inverse of an
     * add_event_override). Returns {blueprint, found, removed, node_guid} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveEventNodeByGuid(UBlueprint* Blueprint, const FString& NodeGuidStr);

    // ---- C++ #10 (Niagara user params) 2026-08-15 (Wave-3 batch 3: NIAGARA USER-PARAMETER AUTHORING) —
    // AUTHORED on Mac, NOT YET COMPILED. Adds/sets/removes the exposed `User.*` parameters a designer
    // sets on a NiagaraSystem (the FNiagaraUserRedirectionParameterStore from GetExposedParameters()).
    // Uses ONLY the Niagara/NiagaraEditor modules already linked for C++ #5 -> **NO Build.cs change.**
    // The parameter-store API is version-sensitive (single-precision FVector3f value types; redirection
    // store handles the "User." namespace) — Windows should VERIFY every call tagged "VERIFY vs engine
    // source" in the .cpp against NiagaraSystem.h / NiagaraParameterStore.h /
    // NiagaraUserRedirectionParameterStore.h / NiagaraTypeDefinition.h and adjust if signatures differ.
    // After compile+verify, wire (hasattr-guarded) into niagara_write:
    //   add_niagara_user_parameter    (ledger op add_user_param;    inverse: remove_niagara_user_parameter)
    //   set_niagara_user_parameter_value (ledger op set_user_param; inverse: set prior value — handler returns "prev")
    //   remove_niagara_user_parameter (ledger op remove_user_param; inverse: re-add w/ captured type+value — handler returns "type","value")

    /**
     * Add a `User.<ParamName>` parameter to a NiagaraSystem's exposed parameter store (zero-initialized).
     * TypeName (case-insensitive) one of: bool | int | float | vector2 (vec2) | vector (vec3) |
     * vector4 (vec4) | linearcolor (color) | quat. The `User.` prefix is added automatically if absent.
     * Marks the package dirty. Returns {"system","added","param","type"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddNiagaraUserParameter(UNiagaraSystem* System, const FString& ParamName, const FString& TypeName);

    /**
     * Set the default value of an existing `User.<ParamName>` on a NiagaraSystem. ValueJson is a small
     * JSON scalar/array interpreted by the parameter's stored type: bool -> true/false; int/float ->
     * number; vector2/vector/vector4/quat/linearcolor -> array (e.g. [1,0,0], [1,0,0,1]). Captures and
     * returns the PRIOR value (for undo). Marks the package dirty.
     * Returns {"system","param","set","prev"} (or {"error"}). `prev` is null if the param had no readable value.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraUserParameterValue(UNiagaraSystem* System, const FString& ParamName, const FString& ValueJson);

    /**
     * Remove `User.<ParamName>` from a NiagaraSystem's exposed parameter store. Captures and returns the
     * removed parameter's type + current value BEFORE removal (so the inverse can re-add it). Marks the
     * package dirty. Returns {"system","param","removed","type","value"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveNiagaraUserParameter(UNiagaraSystem* System, const FString& ParamName);

    // ---- C++ #10 (Niagara emitter rename + renderers) ----
    // 2026-08-15. Extends C++ #5 Niagara authoring: rename an emitter HANDLE, and add/remove
    // RENDERERS on a named emitter. Renderers live on the emitter's VERSIONED data
    // (FVersionedNiagaraEmitterData) reached through the emitter handle's FVersionedNiagaraEmitter
    // instance — the same versioned-emitter model C++ #5 already deals with. **NO Build.cs change**
    // (Niagara + NiagaraEditor already linked). All version-sensitive calls tagged "VERIFY vs engine
    // source" in the .cpp — the versioned-emitter renderer API changed across 5.x, so expect a
    // possible signature iteration on the Windows build. After compile+verify, wire (hasattr-guarded)
    // into niagara_write:
    //   rename_niagara_emitter  (inverse: rename back new_name -> old_name)
    //   add_niagara_renderer     (inverse: remove_niagara_renderer at the index it landed = count-1)
    //   remove_niagara_renderer  (inverse: re-add that renderer TYPE — best-effort, property loss OK)

    /**
     * Rename the emitter HANDLE named OldName on System to NewName (display/handle name only — this is
     * the label C++ #5's AddEmitterToSystem inherits from the source emitter). Returns
     * {"system","renamed","old_name","new_name"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameNiagaraEmitterHandle(UNiagaraSystem* System, const FString& OldName, const FString& NewName);

    /**
     * Add a renderer to the emitter named EmitterName on System. RendererType (case-insensitive):
     * sprite | mesh | ribbon | light -> UNiagaraSpriteRendererProperties / UNiagaraMeshRendererProperties /
     * UNiagaraRibbonRendererProperties / UNiagaraLightRendererProperties. Marks the package dirty.
     * Returns {"system","emitter","added_renderer","renderer_class","renderer_count"} (or {"error"}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddNiagaraRenderer(UNiagaraSystem* System, const FString& EmitterName, const FString& RendererType);

    /**
     * Remove the renderer at RendererIndex (0-based) on the emitter named EmitterName. Marks the
     * package dirty. Returns {"system","emitter","removed","removed_renderer_class","renderer_count"}
     * (or {"error"}). removed_renderer_class is captured for a best-effort re-add on undo.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveNiagaraRenderer(UNiagaraSystem* System, const FString& EmitterName, int32 RendererIndex);

    // ---- C++ #10 (Niagara modules) — MODULE authoring on an emitter's script stack. -----------------
    // Linkability CONFIRMED: NiagaraGraph.h / NiagaraNodeOutput.h / NiagaraNodeFunctionCall.h /
    // NiagaraScriptSource.h / ViewModels/Stack/NiagaraStackGraphUtilities.h all live in NiagaraEditor/Public
    // (probe 2026-08-15) → includable+linkable from this plugin; NO Build.cs change beyond C++ #5's
    // "Niagara","NiagaraEditor". Every FNiagaraStackGraphUtilities / emitter-data call is VERSION-SENSITIVE
    // → VERIFY-tagged in the .cpp against engine source on the Windows build. AddModule/RemoveModule are
    // FEASIBLE; SetModuleInput is PARTIAL (scalar-only, pin-default best-effort — faithful setter is
    // view-model-only and was NOT attempted). ScriptUsage (case-insensitive): particle_spawn |
    // particle_update | emitter_spawn | emitter_update.

    /**
     * Add a module (UNiagaraScript asset at ModuleScriptPath, e.g. "/Niagara/.../MyModule") to the named
     * emitter's ScriptUsage stack, inserted before the usage's output node. Returns
     * {"system","emitter","usage","added_module","node_guid"} (or {"error"}). Requires a recompile+save to
     * persist (done here). Inverse: RemoveNiagaraModuleFromStack by the returned node_guid.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddNiagaraModuleToStack(UNiagaraSystem* System, const FString& EmitterName,
                                           const FString& ScriptUsage, const FString& ModuleScriptPath);

    /**
     * PARTIAL / FRAGILE. Best-effort set of a SCALAR local value input (float/int/bool) on a placed module,
     * by writing the matching input pin's typed default via the Niagara schema. Does NOT handle
     * override-map / dynamic / data-interface / vector-struct inputs (those need the editor view-model).
     * ValueJson is a bare JSON scalar (e.g. "1.5", "3", "true"). Returns
     * {"system","emitter","module","input","set","prior_value"} (or {"error"}). Capture prior_value for undo.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraModuleInput(UNiagaraSystem* System, const FString& EmitterName,
                                         const FString& ScriptUsage, const FString& ModuleName,
                                         const FString& InputName, const FString& ValueJson);

    /**
     * Remove the module node whose NodeGuid == NodeGuidStr from the named emitter's ScriptUsage stack
     * (re-links the parameter map around it via FNiagaraStackGraphUtilities). Returns
     * {"system","emitter","removed","node_guid"} (or {"error"}). Recompiles+saves.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveNiagaraModuleFromStack(UNiagaraSystem* System, const FString& EmitterName,
                                                const FString& ScriptUsage, const FString& NodeGuidStr);

    // ---- C++ #10 2026-08-15 (Niagara compile+save fix) — AUTHORED, NOT YET COMPILED. -----------------
    // Fixes: a mutated NiagaraSystem fails to save from Python (save_asset/save_packages return False)
    // with LogSavePackage "Unexpected custom version FortniteMain ... export tagging and final
    // serialization paths differ". Cause: the C++ mutation leaves the compiled script/DDC data (and its
    // custom version) realized only lazily at serialize time; the current async RequestCompile(false)
    // has not finished when Python saves. Fix = SYNCHRONOUS compile + PostEditChange BEFORE saving.
    // Uses only Niagara (already a dep) + UnrealEd's UPackage::SavePackage (already a dep) -> NO Build.cs
    // change. All version-sensitive calls tagged "VERIFY vs engine source" in the .cpp
    // (NiagaraSystem.h). Wire into niagara_write add/remove_emitter, replacing EAL.save_asset.

    /**
     * Compile a NiagaraSystem, optionally blocking until compilation is fully complete (synchronous).
     * Realizes all compiled script/DDC data (and its custom versions) so a subsequent package save does
     * not hit the "custom version used after summary serialized" linker error. Does NOT save.
     * Returns {"system","waited","compiled"} (compiled = no outstanding requests remain) or {"error"}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CompileNiagaraSystem(UNiagaraSystem* System, bool bWaitForCompletion = true);

    /**
     * Make a mutated NiagaraSystem PERSIST: synchronous compile + PostEditChange + save the package IN
     * C++ (the Python save path fails on a freshly-mutated Niagara asset). Returns
     * {"system","compiled","saved","package"} (or {"error"}). `saved` is the real UPackage::SavePackage
     * result (Python's save_asset only returns an opaque bool). Call this instead of EAL.save_asset after
     * an add/remove_emitter mutation.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SaveNiagaraSystem(UNiagaraSystem* System);

    // ---- C++ #11 2026-08-16 (BehaviorTree editor-graph round-trip) — AUTHORED, NOT YET COMPILED. ------
    // Makes an MCP-authored BehaviorTree EDITOR-ROUND-TRIPPABLE. bt_write.py builds the RUNTIME node tree
    // (RootNode + FBTCompositeChild children + services + child-slot decorators) and it persists + reads
    // back — but the editor graph (UBehaviorTreeGraph, WITH_EDITORONLY_DATA) is never built, so opening the
    // BT in the Behavior Tree editor shows an EMPTY graph and editing there regenerates RootNode from that
    // empty graph, WIPING the runtime nodes. This handler reconstructs UBehaviorTreeGraph FROM the built
    // RootNode (create graph → matching UBehaviorTreeGraphNode_Root/_Composite/_Task + _Decorator/_Service
    // subnodes whose NodeInstance points at the EXISTING runtime UBTNodes → wire exec pins → UpdateAsset()).
    // **REQUIRES Build.cs += "AIModule", "AIGraph", "BehaviorTreeEditor"** (first use of all three).
    // TOP RISK (like the Niagara editor-statics): if UBehaviorTreeGraph / UBehaviorTreeGraphNode_* /
    // UEdGraphSchema_BehaviorTree are NOT declared with BEHAVIORTREEEDITOR_API (and AIGraph bases with
    // AIGRAPH_API), the plugin FAILS TO LINK → a source-engine export patch is needed (the same fix used
    // for NIAGARAEDITOR_API). Every version-sensitive call is tagged "VERIFY vs engine source" in the .cpp.

    /**
     * Reconstruct the BT EDITOR graph (UBehaviorTreeGraph) from a BehaviorTree's already-built runtime
     * RootNode, making the asset editor-round-trippable. Idempotent (rebuilds if a graph exists); no-op-safe
     * on a BT with no RootNode. Marks the package dirty (caller saves). Returns {"behavior_tree",
     * "nodes_created","decorators","services","graph_present"} (or {"error"}). Editor-only.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SyncBehaviorTreeEditorGraph(UBehaviorTree* BehaviorTree);

    // ==== C++ #12 2026-08-16 (deferred-reflection READER batch) — AUTHORED on Mac, NOT YET COMPILED. ====
    // Three read-only reflection readers that unlock protected/native data the Python read modules
    // (eqs.py / statetree.py / controlrig.py) could NOT reach. All are FProperty/registry reflection —
    // low export-macro risk. Build.cs += "StateTreeModule","RigVM" (both runtime). The StateTree editor
    // property-BINDINGS resolver is intentionally NOT in this round (it needs StateTreeEditorModule with
    // real *_API export risk) — deferred to an isolated follow-up so a link failure can't block these four.

    /**
     * Fully-resolved EQS (EnvQuery) config INCLUDING the protected node config VALUES stock Python cannot
     * read: UEnvQuery.Options (protected), each UEnvQueryOption.Generator + Tests, and every generator/test
     * node's UPROPERTY value (declared + inherited), plus the per-option->node grouping. Read via generic
     * FProperty walk (offset-based; ignores C++ protected + Python edit-protection). Shape {"query",
     * "query_name","option_count","options":[{name,index,generator:{class,path,item_type,config},
     * tests:[{class,path,config}]}]} (or {"error"}). Read-only. AIModule already linked -> no Build.cs change.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetEnvQueryConfigJson(UEnvQuery* Query);

    /**
     * Enumerate the REGISTERED native StateTree node types — UScriptStructs deriving from the base node
     * structs (FStateTreeTaskBase/EvaluatorBase/ConditionBase/ConsiderationBase), which are FInstancedStruct
     * types NOT enumerable via Python UClass reflection. Category (case-insensitive): "all" | "tasks" |
     * "evaluators" | "conditions" | "considerations". Walks all UScriptStructs (spans StateTree/
     * GameplayStateTree/project modules) selecting IsChildOf a base. Per entry {struct_path,cpp_name,
     * display_name,module,is_abstract}. Shape {tasks[],evaluators[],conditions[],considerations[],counts}
     * (or {"error"}). Read-only. Build.cs += "StateTreeModule".
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStateTreeNodeRegistryJson(const FString& Category);

    /**
     * Summarize a Control Rig's COMPILED RigVM (static; no ticking/posed eval) via the generated-class CDO
     * (URigVMHost::GetVM()). Pass the Control Rig BLUEPRINT (a UBlueprint; ControlRigBlueprint IS-A UBlueprint).
     * Shape {blueprint,path,vm_present,instruction_count,opcode_histogram{op:count},statistics{...reflected
     * FRigVMStatistics...},memory_stats{...},external_variables[{name,type,is_array}]} (or {"error"}). The
     * bytecode instruction/opcode data is NOT reachable from Python (this is the value-add). Read-only.
     * Build.cs += "RigVM". (node_summary intentionally omitted — get_control_rig_vm_graph already supplies it.)
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetControlRigVMJson(UBlueprint* CRBlueprint);

    /**
     * Per-struct PIN SCHEMA for one RigVM node struct (e.g. "RigUnit_SetTransform",
     * "RigVMFunction_MathQuaternionSlerp"): resolve the UScriptStruct by bare name or full path, iterate
     * UPROPERTYs, derive each pin's direction from RigVM Input/Output/Visible metadata, export each default
     * from a default struct instance. Shape {struct,path,property_count,pin_count,pins:[{name,cpp_type,
     * direction,is_pin,is_array,default_value,category?,tooltip?}]} (or {"error"}). CoreUObject reflection
     * only -> NO new module. Read-only.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetRigVMStructPinsJson(const FString& StructName);

    // ==== C++ #13 2026-08-16 (backlog WRITERS: AnimMontage sections + USkeleton sockets/virtual-bones) ====
    // AUTHORED on Mac, NOT YET COMPILED. Both areas are Engine-module-only (already linked) and reach their
    // protected TArrays (UAnimMontage::CompositeSections / USkeleton::Sockets) via FArrayProperty +
    // FScriptArrayHelper reflection (the C++ #12 EqsReflectObjectArray idiom — ignores C++ protected, references
    // NO editor-module export symbol) -> NO Build.cs change, NO export-macro risk. Virtual bones use the public
    // Engine methods USkeleton::AddVirtualBone/RemoveVirtualBones. All reversible: each remove/set returns the
    // captured prior state so the Python inverse re-calls the paired handler. (StateTree editor property-BINDINGS
    // reader is intentionally NOT here — it needs StateTreeEditorModule w/ real *_API export risk; deferred to an
    // isolated C++ #14.) Every version-sensitive member/name is "VERIFY vs engine source"-tagged in the .cpp.

    /** Append a composite section (name @ start-time seconds) to an AnimMontage via CompositeSections reflection;
     *  re-sorts by time; marks dirty + PostEditChange. Refuses a duplicate name. Returns {"montage","section_name",
     *  "start_time","section_count"} (or {"error"}). */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddMontageSection(UAnimMontage* Montage, const FString& SectionName, float StartTime);

    /** Remove a composite section by name; captures + RETURNS prior_start_time + next_section_name for undo.
     *  Returns {"montage","removed","section_name","prior_start_time","next_section_name","section_count"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveMontageSection(UAnimMontage* Montage, const FString& SectionName);

    /** Move a composite section to a new start time; captures + RETURNS prior_start_time; re-sorts. Returns
     *  {"montage","section_name","prior_start_time","new_start_time","section_count"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMontageSectionTime(UAnimMontage* Montage, const FString& SectionName, float NewStartTime);

    /** Set a composite section's NextSectionName link; captures + RETURNS prior link. Returns {"montage",
     *  "section_name","prior_next_section","new_next_section"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMontageSectionNextSection(UAnimMontage* Montage, const FString& SectionName, const FString& NextSectionName);

    /** Add a USkeletalMeshSocket to a USkeleton (outered to it) named SocketName on BoneName with the given
     *  relative transform (loc/rot/scale as individual floats; defaults identity). Appends to the protected
     *  USkeleton::Sockets via reflection; refuses duplicate name / unknown bone. Returns {"skeleton","socket_name",
     *  "bone","socket_count"}. NOTE: a skeleton socket is SHARED across every mesh on this skeleton. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddSkeletonSocket(USkeleton* Skeleton, const FString& SocketName, const FString& BoneName,
                                     float LocX = 0.f, float LocY = 0.f, float LocZ = 0.f,
                                     float Pitch = 0.f, float Yaw = 0.f, float Roll = 0.f,
                                     float ScaleX = 1.f, float ScaleY = 1.f, float ScaleZ = 1.f);

    /** Remove a skeleton socket by name; captures + RETURNS bone + relative transform for a faithful re-add.
     *  Returns {"skeleton","socket_name","removed","bone","location","rotation","scale","socket_count"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveSkeletonSocket(USkeleton* Skeleton, const FString& SocketName);

    /** Add a virtual bone (Source->Target) via USkeleton::AddVirtualBone; RETURNS the engine-assigned name.
     *  Returns {"skeleton","source","target","virtual_bone_name","virtual_bone_count"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddVirtualBone(USkeleton* Skeleton, const FString& SourceBone, const FString& TargetBone);

    /** Remove a virtual bone by name via USkeleton::RemoveVirtualBones; captures + RETURNS prior source/target.
     *  Returns {"skeleton","virtual_bone_name","removed","source","target","virtual_bone_count"}. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveVirtualBone(USkeleton* Skeleton, const FString& VirtualBoneName);

    // ==== C++ #14 2026-08-16 (StateTree editor property-BINDINGS reader) — AUTHORED on Mac, NOT YET
    // COMPILED. The last deferred-C++ backlog item. Reaches UStateTreeEditorData::EditorBindings
    // (FStateTreeEditorPropertyBindings — a protected/non-BP member Python cannot read; probe-confirmed
    // refused) and resolves each binding's FGuid StructID to a readable node/state label. **REQUIRES
    // Build.cs += "StateTreeEditorModule"** (UStateTreeEditorData/UStateTreeState/FStateTreeEditorNode).
    // TOP RISK (isolated on purpose): if UStateTreeEditorData / the bindings accessors are not
    // STATETREEEDITORMODULE_API-exported for a plugin, this FAILS TO LINK — the same NIAGARAEDITOR_API /
    // BEHAVIORTREEEDITOR_API pattern → a source-engine export patch may be needed. Degrades gracefully to
    // raw GUIDs if the label-map member names mismatch. Every version-sensitive call is "VERIFY vs engine
    // source"-tagged in the .cpp.

    /**
     * Serialize a StateTree's editor property bindings (UStateTreeEditorData::EditorBindings). Per binding
     * {source_struct, source_property, target_struct, target_property, source_struct_id, target_struct_id}
     * — the *_struct labels resolve the FGuid StructID to the owning node/state via an ID->name map built by
     * walking the editor data; *_struct_id keeps the raw GUID as fallback. Shape {"state_tree","editor_data",
     * "binding_count","bindings":[...]} (or {"error"}). Read-only, editor-only.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStateTreeBindingsJson(UStateTree* StateTree);

    // ---- C++ #16 2026-08-16 (gameplay-tag rename + source authoring) — same module/include pattern as
    // C++ #6 (IGameplayTagsEditorModule + UGameplayTagsManager; GameplayTags + GameplayTagsEditor already
    // Build.cs deps -> NO Build.cs change, NO new includes). Unblocks the DEFERRED-BLOCKED
    // gas_write.rename_gameplay_tag / add_gameplay_tag_source refusals. VERIFY-tagged calls in the .cpp
    // are version-sensitive. After compile, replace the two _blocked() refusals in gas_write.py.

    /**
     * Rename a gameplay tag, creating an INI redirector so existing references keep resolving, via
     * IGameplayTagsEditorModule::RenameTagInINI. Returns {old_tag, new_tag, renamed, registered} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameGameplayTag(const FString& OldTag, const FString& NewTag);

    /**
     * Register a new gameplay-tag *.ini source with the live manager via
     * IGameplayTagsEditorModule::AddNewGameplayTagSource. Returns {source, added, registered} (or {error}).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddGameplayTagSource(const FString& SourceName);

    // ==== C++ #15 2026-08-16 (EQS authoring WRITER — inverse of GetEnvQueryConfigJson) ====
    // AUTHORED on Mac, NOT YET COMPILED. Writes the PROTECTED EQS structure
    // (UEnvQuery::Options / UEnvQueryOption::Generator+Tests / node config FProperties) via the SAME
    // FProperty-reflection idiom as the reader + the C++ #13 socket writer (FindFProperty / FArrayProperty
    // + FScriptArrayHelper / FObjectPropertyBase). FULLY reflective: the concrete UClass of each element
    // comes from the property's PropertyClass, so NO UEnvQueryOption/Generator/Test include and NO Build.cs
    // change (AIModule + EnvironmentQuery/EnvQuery.h already linked) -> NO export-macro risk. Handlers return
    // FString JSON (class convention); the logical return (option_index/test_index/removed/set) is a field.
    // Defensive: null/bounds checks, {"error"} on failure, never crash. Reversible via the returned prior state.

    /**
     * Create a UEnvQueryOption (outer=Query), instantiate the generator UEnvQueryGenerator subclass named by
     * GeneratorClassPath (e.g. "/Script/AIModule.EnvQueryGenerator_ActorsOfClass"), set Option->Generator, and
     * append the option to the protected UEnvQuery::Options array. Returns {"query","option_index",
     * "option_name","generator_class","option_count"} or {"error"}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString AddEnvQueryOption(UEnvQuery* Query, const FString& GeneratorClassPath);

    /**
     * Remove the option at OptionIndex (and its owned Generator/Tests) from UEnvQuery::Options.
     * Returns {"query","removed":true,"option_count"} or {"error"} (bad index / no options).
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString RemoveEnvQueryOption(UEnvQuery* Query, int32 OptionIndex);

    /**
     * Instantiate a UEnvQueryTest subclass named by TestClassPath and append it to the option's protected
     * UEnvQueryOption::Tests array. Returns {"query","option_index","test_index","test_class","test_count"} or {"error"}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString AddEnvQueryTest(UEnvQuery* Query, int32 OptionIndex, const FString& TestClassPath);

    /**
     * Remove the test at TestIndex from the option at OptionIndex.
     * Returns {"query","option_index","removed":true,"test_count"} or {"error"}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString RemoveEnvQueryTest(UEnvQuery* Query, int32 OptionIndex, int32 TestIndex);

    /**
     * Set a config FProperty (by name) on a generator/test/option node. NodeLocator grammar (case-insensitive):
     *   "option:<i>"              -> the UEnvQueryOption itself
     *   "option:<i>/generator"    -> that option's Generator node
     *   "option:<i>/test:<j>"     -> that option's j-th Test node
     * ValueJson is a BARE JSON value (scalar/array/object) coerced in reverse of the reader (typed setters +
     * ImportText_Direct fallback). Struct/array props must be passed as a UE ExportText string in JSON form,
     * e.g. valueJson = "\"(X=1.0,Y=2.0,Z=3.0)\"". Returns {"query","node","prop","set":true,"prev"} or {"error"}.
     */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString SetEnvQueryNodeProperty(UEnvQuery* Query, const FString& NodeLocator, const FString& PropName, const FString& ValueJson);

    // ==== C++ #17 2026-08-18 (input: full EKeys registry enumerator; defined in MCPReflection_Input.cpp) ====
    /** Enumerate the FULL EKeys registry (InputCore) via EKeys::GetAllKeys. Per key {name, display_name,
     *  is_gamepad_key, is_mouse_button, is_modifier_key, is_touch, is_axis_1d/2d/3d, is_analog, is_digital}.
     *  Shape {"status":"success","count":N,"keys":[{...}]}. Read-only. Backs list_input_keys full-registry mode. */
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetAllInputKeysJson();

    // ==== C++ #17 2026-08-18 (UserDefinedStruct member AUTHORING; defined in MCPReflection_Structs.cpp) ====
    // Full member edit (rename/retype/default/tooltip) + complex-typed add via FStructureEditorUtils +
    // FEdGraphPinType (both already Build.cs deps -> NO Build.cs change). Each returns {"status", ...}.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameStructField(UUserDefinedStruct* Struct, const FString& FieldName, const FString& NewName);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ChangeStructFieldType(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeJson);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStructFieldDefault(UUserDefinedStruct* Struct, const FString& FieldName, const FString& DefaultValue);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStructFieldTooltip(UUserDefinedStruct* Struct, const FString& FieldName, const FString& Tooltip);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddStructFieldEx(UUserDefinedStruct* Struct, const FString& FieldName, const FString& TypeJson);

    // ==== C++ #18 2026-08-18 StateTree editor WRITERS + params/bindings/compile (MCPReflection_StateTree.cpp) ====
    // C++ #19: GATING ed-link fix — roots UStateTreeEditorData via the real UStateTree::EditorData pointer so it
    // survives GC (create_statetree calls this at end of creation). Idempotent. Returns {was_linked,linked,created}.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString EnsureStateTreeEditorData(UStateTree* StateTree);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStateTreeNodePropertyJson(UStateTree* StateTree, const FString& StateName, const FString& NodeKind, int32 NodeIndex, const FString& PropertyName, const FString& NewValueText, const FString& Container);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStateTreeTransitionPropertyJson(UStateTree* StateTree, const FString& StateName, int32 TransitionIndex, const FString& PropertyName, const FString& NewValueText);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStateTreeComponentTreeJson(UObject* Component, const FString& PropertyName, UStateTree* NewStateTree, const FString& NewRefText);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStateTreeColorJson(UStateTree* StateTree, const FString& StateName, const FString& ColorName, const FString& ColorGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStateTreeParametersJson(UStateTree* StateTree);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddStateTreeParameter(UStateTree* StateTree, const FString& ParamName, const FString& TypeName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetStateTreeParameter(UStateTree* StateTree, const FString& ParamName, const FString& ValueText);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveStateTreeParameter(UStateTree* StateTree, const FString& ParamName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddStateTreeBinding(UStateTree* StateTree, const FString& SourceStructId, const FString& SourcePath, const FString& TargetStructId, const FString& TargetPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveStateTreeBinding(UStateTree* StateTree, const FString& TargetStructId, const FString& TargetPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CompileStateTree(UStateTree* StateTree);
    // C++ #20 (2026-08-18): repair malformed editor nodes (empty Instance struct + zero ID) that Python
    // import_text authoring leaves behind — THE real cause of the StateTree editor crash. Reallocs each
    // node's Instance to match Node->GetInstanceDataType() (as the editor's ConditionalUpdateNodeInstanceData
    // does) + assigns fresh GUIDs to unset node/state/transition IDs. Idempotent. Call before any save/compile.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RepairStateTreeNodes(UStateTree* StateTree);
    // C++ #21 (2026-08-19): the last 3 StateTree spec features. Binding SOURCES reader (GetBindableStructs);
    // task-completion binding (EditorBindings.AddTaskCompletionBinding); delegate dispatcher->listener binding
    // (a property binding via AddPropertyBinding). Take these to statetree 100%.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetStateTreeBindingSourcesJson(UStateTree* StateTree, const FString& TargetStructId);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddStateTreeTaskCompletionBinding(UStateTree* StateTree, const FString& SourceTaskId, const FString& TargetStructId, const FString& TargetPath, const FString& Condition);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BindStateTreeDelegate(UStateTree* StateTree, const FString& DispatcherStructId, const FString& DispatcherPath, const FString& ListenerStructId, const FString& ListenerPath);

    // C++ #22 (2026-08-19): gas RUNTIME reader — get_ability_system_info reads a live UAbilitySystemComponent
    // (attributes/tags/abilities/active-effects). AddTestAbilitySystemComponent is test scaffolding so a live
    // ASC can be created + verified over the bridge (project actors have none). See MCPReflection_GAS.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetAbilitySystemInfoJson(AActor* Actor);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddTestAbilitySystemComponent(AActor* Actor, const FString& LooseTag);

    // C++ #23 (2026-08-19): BehaviorTree RUNTIME reader/control on a live UBehaviorTreeComponent in PIE.
    // SpawnBehaviorTreeTestActor is the test fixture (spawn an AIController + RunBehaviorTree). See MCPReflection_BTRuntime.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBehaviorTreeRuntimeJson(AActor* Actor, bool bIncludeBlackboard);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ControlBehaviorTreeRuntimeJson(AActor* Actor, const FString& Action);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBehaviorTreeDynamicSubtreeJson(AActor* Actor, const FString& InjectTag, UBehaviorTree* Subtree);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SpawnBehaviorTreeTestActor(AActor* WorldContextActor, UBehaviorTree* BT);

    // C++ #24 (2026-08-18): niagara graph/stack READERS (emitter/system graph modules, module inputs, node/pin
    // detail, BFS wire trace, structural stack validation) + one reversible write (SetNiagaraModuleEnabled).
    // All via the exported NiagaraEditor symbols — NO Build.cs change. See MCPReflection_Niagara2.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraModules(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, bool bIncludeInputs);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraModuleInputs(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, const FString& InputFilter);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraGraphNodes(const FString& SystemPath, const FString& EmitterName, const FString& Verbosity, const FString& TypeFilter);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraNodeInfo(const FString& SystemPath, const FString& EmitterName, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString TraceNiagaraConnection(const FString& SystemPath, const FString& EmitterName, const FString& NodeGuid, const FString& PinName, const FString& Direction, int32 MaxDepth);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ValidateNiagaraStack(const FString& SystemPath, const FString& EmitterName, const FString& MinSeverity);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraModuleEnabled(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, bool bEnabled);

    // C++ #25 (2026-08-18): control-rig per-bone WEIGHTED bounds from a skeletal mesh's render data (to size/
    // place rig controls & pole vectors). Engine-only, NO Build.cs change. See MCPReflection_ControlRig.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetSkeletalBoneBoundsJson(USkeletalMesh* Mesh, const FString& BoneFilter, const FString& WeightMode, int32 LODIndex, float MinWeight);

    // C++ #26 (2026-08-18): niagara module-stack WRITERS #2 — reorder + input setters (dynamic input,
    // stack local/linked value, float-curve keys). See MCPReflection_Niagara3.cpp. Reorder needs the
    // MoveModule NIAGARAEDITOR_API export patch; the setters use only already-exported symbols.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ReorderNiagaraModule(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, int32 NewIndex);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraDynamicInput(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& DynamicInputScriptPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraStackValue(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& Mode, const FString& ValueOrParameter);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraCurve(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, const FString& InputName, const FString& KeysJson);
    // Faithful inverse for the fresh-input setters: remove an input's override pin (dynamic input / linked / local / curve).
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ClearNiagaraInputOverride(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, const FString& InputName);

    // C++ #29 (2026-08-18): get_niagara_particle_stats (live PIE UNiagaraComponent particle counts) + the
    // CORRECTED reorder (ReorderNiagaraModuleV2 — safe index mapping, replaces the crashing ReorderNiagaraModule).
    // See MCPReflection_Niagara5.cpp. No Build.cs / engine patch (reuses the MoveModule export patch).
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraParticleStatsJson(AActor* Actor, const FString& ComponentName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ReorderNiagaraModuleV2(const FString& SystemPath, const FString& EmitterName, const FString& ScriptUsage, const FString& ModuleName, int32 NewIndex);

    // C++ #27 (2026-08-18): niagara GRAPH/SCRIPT authoring — scratch-pad + standalone module asset + graph node
    // add/build/delete/layout on a standalone UNiagaraScript. See MCPReflection_Niagara4.cpp. No Build.cs / engine patch.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateNiagaraScratchPadModule(UNiagaraSystem* System, const FString& ScriptName, const FString& ScriptType);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateNiagaraModuleAsset(const FString& PackagePath, const FString& AssetName, const FString& ScriptType);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddNiagaraGraphNode(const FString& ScriptPath, const FString& NodeSpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BuildNiagaraGraph(const FString& ScriptPath, const FString& SpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteNiagaraGraphNode(const FString& ScriptPath, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString LayoutNiagaraGraph(const FString& ScriptPath, const FString& OptionsJson);

    // C++ #28 (2026-08-18): Control Rig RUNTIME (eval-based) — instantiate a transient UControlRig, run its
    // Forwards Solve, read the solved hierarchy. Reads + transient-only writes (no undo). Needs ControlRig in
    // Build.cs. See MCPReflection_ControlRigRuntime.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetRigPoseJson(const FString& ControlRigPath, const FString& BonesCsv, float ThresholdCm);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString AnalyzeRigIoJson(const FString& ControlRigPath, int32 Frames, float DeltaTime);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString AnalyzeRigControlImpactJson(const FString& ControlRigPath, const FString& ControlsCsv, float OffsetCm);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ProfileRigJson(const FString& ControlRigPath, int32 Frames, float DeltaTime, int32 TopNodes);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString SimulateRigJson(const FString& ControlRigPath, const FString& AnimSequencePath, int32 Frames, float DeltaTime);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString StartRigMotionCaptureJson(const FString& ControlRigPath, int32 Frames, float DeltaTime);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetRigMotionReportJson(const FString& ControlRigPath);

    // C++ #30 (2026-08-18): Control Rig PHYSICS/validation — transient-rig solve + geometry analysis; physics
    // stepping rides the rig's own "Step Physics Solver" VM node (UE5.8: CR core no longer sims directly), so
    // NO ControlRigPhysics/Chaos link + NO Build.cs change. See MCPReflection_ControlRigPhysics.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ValidateRigPhysicsJson(const FString& ControlRigPath, float DeviationThresholdCm);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ValidateRigDeformationJson(const FString& ControlRigPath, float ScaleTolerance, float ShearToleranceDeg);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString StartRigPhysicsProbeJson(const FString& ControlRigPath, const FString& Control, float ShakeCm, int32 SettleFrames);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetRigPhysicsProbeReportJson(const FString& ControlRigPath, float ResidualThresholdCm, int32 MaxBones);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString MeasureMeshPenetrationJson(const FString& ControlRigPath, const FString& ChainFilter, const FString& BodyFilter, float MarginCm);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString FitRigChainCollisionJson(const FString& ControlRigPath, const FString& ModuleName, float MarginCm, const FString& Shape);

    // C++ #31 (2026-08-19, materials M5): Material Attribute Layers stack (set/get) on a MaterialInstance +
    // base-Material graph comment add/remove. All ENGINE_API / public-member Engine calls -> NO Build.cs
    // change, NO engine export patch. See MCPReflection_Materials.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMaterialLayersJson(const FString& InstancePath, const FString& LayersJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString GetMaterialLayersJson(const FString& InstancePath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddMaterialCommentJson(const FString& MaterialPath, const FString& Text, int32 X, int32 Y, int32 SizeX, int32 SizeY, const FString& Color);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveMaterialCommentJson(const FString& MaterialPath, const FString& CommentName);

    // C++ #32 (2026-08-19, blueprints B): SCS (Simple Construction Script) COMPONENT authoring on a Blueprint
    // asset. Engine/UnrealEd only -> NO Build.cs change, NO export patch. See MCPReflection_BlueprintSCS.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintSCSJson(const FString& BlueprintPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddComponentToBlueprintJson(const FString& BlueprintPath, const FString& ComponentClass, const FString& ComponentName, const FString& ParentComponentName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintComponentPropertyJson(const FString& BlueprintPath, const FString& ComponentName, const FString& PropertyName, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteBlueprintComponentJson(const FString& BlueprintPath, const FString& ComponentName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ReparentBlueprintComponentJson(const FString& BlueprintPath, const FString& ComponentName, const FString& NewParentName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintRootComponentJson(const FString& BlueprintPath, const FString& ComponentName);

    // C++ #33 (2026-08-19, blueprints C-core): K2 graph reader + node primitives (add/connect/set-pin/break/
    // delete/set-prop) + compile. BlueprintGraph/UnrealEd only -> NO Build.cs change. See MCPReflection_BlueprintGraph.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString SearchBlueprintNodesJson(const FString& BlueprintPath, const FString& GraphName, bool bWithPins);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString DescribeBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeSpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ConnectBlueprintNodesJson(const FString& BlueprintPath, const FString& GraphName, const FString& FromGuid, const FString& FromPin, const FString& ToGuid, const FString& ToPin);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintPinDefaultJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, const FString& PinName, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BreakBlueprintNodeLinkJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, const FString& PinName, const FString& OtherGuid, const FString& OtherPin);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteBlueprintNodeJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintNodePropertyJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, const FString& PropertyName, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CompileBlueprintByPath(const FString& BlueprintPath);

    // C++ #34 (2026-08-19, blueprints D): Blueprint FUNCTION + EVENT-GRAPH authoring. BlueprintGraph/UnrealEd
    // only -> NO Build.cs change, NO export patch. See MCPReflection_BlueprintFunc.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateBlueprintFunctionGraphJson(const FString& BlueprintPath, const FString& FunctionName, const FString& ReturnTypeJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddFunctionInputJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, const FString& TypeJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddFunctionOutputJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, const FString& TypeJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetFunctionPropertiesJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PropsJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateLocalVariableJson(const FString& BlueprintPath, const FString& FunctionName, const FString& VarName, const FString& TypeJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteBlueprintFunctionJson(const FString& BlueprintPath, const FString& FunctionName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString OverrideBlueprintFunctionJson(const FString& BlueprintPath, const FString& FunctionName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateEventGraphJson(const FString& BlueprintPath, const FString& GraphName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameEventGraphJson(const FString& BlueprintPath, const FString& OldName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteEventGraphJson(const FString& BlueprintPath, const FString& GraphName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddEventDispatcherInputJson(const FString& BlueprintPath, const FString& DispatcherName, const FString& PinName, const FString& TypeJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveFunctionPinJson(const FString& BlueprintPath, const FString& FunctionName, const FString& PinName, bool bIsOutput);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveLocalVariableJson(const FString& BlueprintPath, const FString& FunctionName, const FString& VarName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveEventDispatcherInputJson(const FString& BlueprintPath, const FString& DispatcherName, const FString& PinName);

    // C++ #35 (2026-08-19, blueprints C): graph-builders + typed-asset creators + type-registry + var-flag setter.
    // BlueprintGraph/UnrealEd/Engine/AssetRegistry (all deps) -> NO Build.cs change. See MCPReflection_BlueprintMisc.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BuildBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName, const FString& SpecJson, const FString& Mode);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ArrangeBlueprintGraphJson(const FString& BlueprintPath, const FString& GraphName, const FString& OptionsJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateBlueprintInterfaceJson(const FString& Name, const FString& Path);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateTypedBlueprintJson(const FString& Name, const FString& Path, const FString& ParentClass, const FString& BlueprintType);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetTypeRegistryJson(const FString& Kind, const FString& Query, bool bIncludeEngine, int32 Max);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintVariableFlagsJson(const FString& BlueprintPath, const FString& VariableName, const FString& FlagsJson);

    // C++ #36 (2026-08-19): EQS RUNTIME executor — run a UEnvQuery in a live PIE world, return scored items.
    // PIE-gated (editor world does not tick EQS). AIModule dep -> NO Build.cs change. See MCPReflection_EQS.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString RunEnvQueryJson(UEnvQuery* Query, AActor* Querier, const FString& RunMode, int32 MaxItems);

    // C++ #37 (2026-08-19, widgets W-B): UMG widget-tree ops + named slots + property bindings. Extends C++ #8
    // (RootWidget/bIsVariable/Bindings/INamedSlotInterface/ReplaceWidgets -- all python-refused). UMG+UMGEditor
    // already deps -> NO Build.cs change. See MCPReflection_Widgets.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameWidgetJson(const FString& WidgetBlueprintPath, const FString& OldName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetRootWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetWidgetIsVariableJson(const FString& WidgetBlueprintPath, const FString& WidgetName, bool bIsVariable);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ReplaceWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& NewClass);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString WrapWidgetJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PanelClass);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListNamedSlotsJson(const FString& WidgetBlueprintPath, const FString& WidgetName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNamedSlotContentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& SlotName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNamedSlotContentJson(const FString& WidgetBlueprintPath, const FString& HostWidgetName, const FString& SlotName, const FString& ContentWidgetName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ClearNamedSlotJson(const FString& WidgetBlueprintPath, const FString& HostWidgetName, const FString& SlotName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddPropertyBindingJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PropertyName, const FString& FunctionName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemovePropertyBindingJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& PropertyName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListPropertyBindingsJson(const FString& WidgetBlueprintPath);

    // C++ #38 (2026-08-19, widget ANIMATIONS W-C): UWidgetAnimation CRUD + MovieScene possessable bindings /
    // property tracks / channel keys. Adds MovieScene + MovieSceneTracks Build.cs deps. See MCPReflection_WidgetAnim.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateWidgetAnimationJson(const FString& WidgetBlueprintPath, const FString& AnimName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListWidgetAnimationsJson(const FString& WidgetBlueprintPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveWidgetAnimationJson(const FString& WidgetBlueprintPath, const FString& AnimName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimationWidgetBindingJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimationWidgetBindingJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimationTrackJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimationKeyJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType, float TimeSeconds, float Value, int32 ChannelIndex, const FString& Interp);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListAnimationTracksJson(const FString& WidgetBlueprintPath, const FString& AnimName);
    // Reversal handlers for the add_track/add_key undo (MCPReflection_WidgetAnim.cpp).
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimationTrackJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimationKeyJson(const FString& WidgetBlueprintPath, const FString& AnimName, const FString& WidgetName, const FString& TrackType, int32 ChannelIndex, float TimeSeconds);

    // C++ #39 (2026-08-19, widget UI-COMPONENTS W-E, UE5.8 beta): attach/detach/list a UUIComponent on a WBP widget
    // via FUIComponentUtils (UMGEditor). NO Build.cs change. See MCPReflection_WidgetUIComp.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddUIComponentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& ComponentClass);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveUIComponentJson(const FString& WidgetBlueprintPath, const FString& WidgetName, const FString& ComponentClass);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListUIComponentsJson(const FString& WidgetBlueprintPath);

    // C++ #40 (2026-08-19, widgets W-D): UMG Model-View-ViewModel authoring via the exported UMVVMEditorSubsystem.
    // Requires the beta ModelViewViewModel plugin ENABLED + Build.cs += ModelViewViewModel/Blueprint/Editor.
    // NO engine export patch. See MCPReflection_WidgetMVVM.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetMvvmViewmodelsJson(const FString& WidgetBlueprintPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddMvvmViewmodelJson(const FString& WidgetBlueprintPath, const FString& ViewModelClassPath, const FString& DesiredName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveMvvmViewmodelJson(const FString& WidgetBlueprintPath, const FString& ViewModelName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameMvvmViewmodelJson(const FString& WidgetBlueprintPath, const FString& OldName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMvvmViewmodelSettingsJson(const FString& WidgetBlueprintPath, const FString& ViewModelName, const FString& SettingsJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetMvvmBindingsJson(const FString& WidgetBlueprintPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddMvvmBindingJson(const FString& WidgetBlueprintPath, const FString& SpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMvvmBindingJson(const FString& WidgetBlueprintPath, const FString& BindingId, const FString& SpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveMvvmBindingJson(const FString& WidgetBlueprintPath, const FString& BindingId);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetMvvmConversionFunctionsJson(const FString& WidgetBlueprintPath, const FString& NameFilter, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetVariableFieldNotifyJson(const FString& BlueprintPath, const FString& VariableName, bool bEnable);

    // C++ #41 (2026-08-19, small-cats sweep): console-object info + project-settings set/persist + search +
    // data-asset property valid-types. All Core/CoreUObject/DeveloperSettings (already deps) -> NO Build.cs change.
    // See MCPReflection_SmallCats.cpp.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetConsoleObjectInfoJson(const FString& Name);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetDeveloperSettingJson(const FString& SettingsClassName, const FString& PropertyPath, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString SearchProjectSettingsJson(const FString& Filter, const FString& Container);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetPropertyValidTypesJson(const FString& ClassName, const FString& PropertyPath, const FString& Filter, bool bIncludeAbstract);

    // ==== C++ #18 2026-08-18 anim skeleton-SLOT registry + editor outliner FOLDERS (MCPReflection_AnimSkelEditor.cpp) ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetSkeletonSlotsJson(USkeleton* Skeleton);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddSkeletonSlot(USkeleton* Skeleton, const FString& SlotName, const FString& GroupName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveSkeletonSlot(USkeleton* Skeleton, const FString& SlotName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenameSkeletonSlot(USkeleton* Skeleton, const FString& OldName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddSkeletonSlotGroup(USkeleton* Skeleton, const FString& GroupName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveSkeletonSlotGroup(USkeleton* Skeleton, const FString& GroupName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateOutlinerFolder(const FString& FolderPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteOutlinerFolder(const FString& FolderPath);

    // ==== C++ #18 2026-08-18 AnimGraph state-machine + nodes/layers (MCPReflection_AnimGraph.cpp) — ISOLATED; needs Build.cs AnimGraph/AnimGraphRuntime ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimStateMachine(UAnimBlueprint* AnimBlueprint, const FString& MachineName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimTransition(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& FromState, const FString& ToState);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetAnimEntryState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetAnimStateMachineJson(UAnimBlueprint* AnimBlueprint, const FString& MachineName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimState(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& StateName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimTransition(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& FromState, const FString& ToState);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetAnimTransitionProperty(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& FromState, const FString& ToState, const FString& PropName, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BuildAnimStateMachine(UAnimBlueprint* AnimBlueprint, const FString& MachineName, const FString& SpecJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetAnimNodePinExposure(UAnimBlueprint* AnimBlueprint, const FString& NodeGuid, const FString& PropertyName, bool bExpose);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BindAnimNodeFunction(UAnimBlueprint* AnimBlueprint, const FString& NodeGuid, const FString& BindingSlot, const FString& FunctionName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddAnimLayer(UAnimBlueprint* AnimBlueprint, const FString& LayerName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateAnimLayerInterface(const FString& PackagePath, const FString& AssetName);

    // ==== C++ #19 2026-08-18 batch — Blueprint interfaces + var-flags (MCPReflection_BlueprintExt.cpp),
    //      AnimGraph removers (MCPReflection_AnimRemove.cpp), Niagara renderer/emitter (MCPReflection_NiagaraExt.cpp).
    //      NO Build.cs change (all modules already deps). EnsureStateTreeEditorData decl is up in the StateTree block. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ImplementBlueprintInterface(UBlueprint* Blueprint, const FString& InterfacePath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveBlueprintInterface(UBlueprint* Blueprint, const FString& InterfacePath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListBlueprintInterfaces(UBlueprint* Blueprint);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintVariableFlagsJson(UBlueprint* Blueprint, const FString& VarName);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimStateMachineNode(UAnimBlueprint* AnimBP, const FString& MachineName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveAnimLayerNode(UAnimBlueprint* AnimBP, const FString& LayerName);

    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraRendererProperty(const FString& SystemPath, const FString& EmitterName, int32 RendererIndex, const FString& PropertyName, const FString& ValueJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetNiagaraRendererBinding(const FString& SystemPath, const FString& EmitterName, int32 RendererIndex, const FString& BindingName, const FString& SourceValue);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DuplicateNiagaraEmitterHandle(UNiagaraSystem* System, const FString& EmitterName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ReorderNiagaraEmitterHandle(UNiagaraSystem* System, const FString& EmitterName, int32 NewIndex);

    // ==== C++ #42 2026-08-19 (WORLD "C++-A infra ext"): editor modes + world-partition settings/grid/build +
    //      navigation build status (MCPReflection_WorldExt.cpp). All reach objects with NO BlueprintCallable /
    //      reflected Python surface in 5.8: the live UWorldPartition is unreachable from editor Python (C++ path
    //      GEditor->GetEditorWorldContext().World()->GetWorldPartition()); editor modes go through the non-UFUNCTION
    //      global FEditorModeTools (GLevelEditorModeTools()); nav build-status funcs are not UFUNCTIONs. Modules:
    //      UnrealEd (GLevelEditorModeTools + UAssetEditorSubsystem) + EditorFramework (FEditorModeInfo/EM_Default) +
    //      Engine (UWorldPartition) + NavigationSystem — EditorFramework & NavigationSystem are PUBLIC deps of
    //      UnrealEd so they link transitively -> NO Build.cs change strictly required, NO engine export patch.
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListEditorModesJson();
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetEditorModeJson(const FString& ModeId);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetWorldPartitionSettingsJson();
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetWorldPartitionSettingsJson(const FString& SettingsJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetRuntimeGridJson(const FString& GridJson);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString BuildWorldPartitionJson(const FString& Builder);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString NavigationBuildStatusJson();

    // ==== C++ #43 2026-08-19 LANDSCAPE / terrain edit-data BRIDGE (MCPReflection_Landscape.cpp) ====
    // Landscape has NO reflected Python authoring surface in UE 5.8 — ALandscapeProxy::Import and the
    // FLandscapeEditDataInterface / FHeightmapAccessor / TAlphamapAccessor edit-data path are C++-only.
    // ALL required symbols are LANDSCAPE_API-exported (or header-only templates) -> NO engine export patch;
    // Build.cs += "Landscape" (runtime module) ONLY. Height/weight arrays cross the boundary as base64 of
    // raw little-endian uint16 (heights, row-major W*H) / uint8 (weights). All handlers are #if WITH_EDITOR,
    // null/bounds-guarded, JSON returns. Section math is done HOST-SIDE in Python (landscape_write.py).
    //   Height encoding: uint16 H; LocalZ = (H-32768)/128; WorldZ = LocalZ*ActorScaleZ. Flat = 32768.
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString CreateLandscapeJson(const FString& Name, const FString& TransformJson, const FString& ConfigJson, const FString& HeightDataB64);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString LandscapeGetInfoJson(const FString& ActorName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString LandscapeGetHeightRegionJson(const FString& ActorName, int32 X, int32 Y, int32 W, int32 H);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString LandscapeSetHeightRegionJson(const FString& ActorName, int32 X, int32 Y, int32 W, int32 H, const FString& HeightDataB64, bool bReturnPrior);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString LandscapePaintWeightRegionJson(const FString& ActorName, const FString& LayerName, int32 X, int32 Y, int32 W, int32 H, const FString& WeightDataB64, const FString& LayerInfoAssetPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ValidateLandscapeJson(const FString& ActorName);

    // ==== C++ #44 2026-08-19 (debug category, Wave 1+2): Blueprint DEBUGGER — breakpoints / pin-watches /
    // debug-object / runtime state / execution-trace / call-stack / live-value inspection (MCPReflection_Debug.cpp).
    // All via UNREALED_API FKismetDebugUtilities + ENGINE_API UBlueprint debug accessors -> NO Build.cs change,
    // NO engine export patch. node_id == the node's NodeGuid string. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintBreakpointJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, bool bEnabled);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveBlueprintBreakpointJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, bool bAll);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListBlueprintBreakpointsJson(const FString& BlueprintPath, const FString& Filter, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintPinWatchJson(const FString& BlueprintPath, const FString& GraphName, const FString& NodeGuid, const FString& PinName, bool bRemove);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListBlueprintPinWatchesJson(const FString& BlueprintPath, bool bValues, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBlueprintDebugObjectJson(const FString& BlueprintPath, const FString& Instance, bool bClear);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListBlueprintDebugObjectsJson(const FString& BlueprintPath, const FString& Filter, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintDebugStateJson(const FString& Detail);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintExecutionTraceJson(const FString& BlueprintPath, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetBlueprintCallStackJson(int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString InspectBlueprintDebugValueJson(const FString& BlueprintPath, const FString& Path, const FString& Filter, int32 Depth, int32 MaxResults);

    // ==== C++ #45 2026-08-19 (debug category, Wave 4): BehaviorTree BREAKPOINTS on the editor graph
    // (MCPReflection_BTDebug.cpp). State = the PUBLIC TRANSIENT bitfields bHasBreakpoint/bIsBreakpointEnabled
    // on UBehaviorTreeGraphNode (NOT UPROPERTY, NOT saved -> list is empty after editor restart by design).
    // NO Build.cs change (AIModule/AIGraph/BehaviorTreeEditor already deps, proven by C++ #11); NO export patch.
    // node_id == runtime NodeName (matches get_behavior_tree_info). ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetBTBreakpointJson(const FString& BehaviorTreePath, const FString& NodeId, bool bEnabled);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemoveBTBreakpointJson(const FString& BehaviorTreePath, const FString& NodeId, bool bAll);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListBTBreakpointsJson(const FString& BehaviorTreePath, int32 MaxResults);

    // ==== C++ #46 2026-08-19 (mutable Wave 3): CustomizableObject SOURCE-GRAPH reader + node primitives
    // (MCPReflection_Mutable.cpp). Source reached via GetPrivate()->GetSource() (both CUSTOMIZABLEOBJECT_API,
    // public) -> NO engine export patch. Node/schema classes are MinimalAPI but their methods are UE_API
    // (CUSTOMIZABLEOBJECTEDITOR_API): AllocateDefaultPins/ReconstructNode/DestroyNode + TryCreateConnection/
    // Break*. Build.cs += CustomizableObject (Runtime) + CustomizableObjectEditor (UncookedOnly). Graph edits
    // do NOT compile the CO -> call compile_customizable_object separately. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetMutableGraphJson(const FString& CoPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetMutableNodeJson(const FString& CoPath, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddMutableNodeJson(const FString& CoPath, const FString& NodeClass, float X, float Y);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ConnectMutableNodesJson(const FString& CoPath, const FString& FromGuid, const FString& FromPin, const FString& ToGuid, const FString& ToPin);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DisconnectMutablePinJson(const FString& CoPath, const FString& NodeGuid, const FString& PinName, const FString& OtherGuid, const FString& OtherPin);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString DeleteMutableNodeJson(const FString& CoPath, const FString& NodeGuid);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetMutableNodePropertyJson(const FString& CoPath, const FString& NodeGuid, const FString& PropertyName, const FString& ValueJson);

    // ==== C++ #47 2026-08-19 (AUDIO C++-only: MetaSound DISCOVERY #1-4 + SoundSubmix parent WRITER #38;
    // MCPReflection_AudioCpp.cpp). MetaSound node classes are frontend-registry entries (NOT UClasses) so Python
    // can't enumerate them; the search/registry singletons are C++-only. Submix parent_submix/child_submixes are
    // EditConst (Python-refused) -> C++ calls the public ENGINE_API SetParentSubmix. Build.cs += "MetasoundFrontend"
    // (all symbols METASOUNDFRONTEND_API -> NO export patch). Discovery = reads; set_submix_parent = ledgered write. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString SearchMetaSoundNodesJson(const FString& Filter, const FString& Category, int32 MaxResults);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString DescribeMetaSoundNodeJson(const FString& Namespace, const FString& Name, const FString& Variant);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListMetaSoundDataTypesJson(const FString& Filter);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListMetaSoundInterfacesJson(const FString& Filter);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetSubmixParentJson(const FString& SubmixPath, const FString& ParentPath);

    // ==== C++ #48 2026-08-19 (PCG Wave 5, FINAL PCG slice): graph-parameter SCHEMA authoring +
    // dynamic-input-pin editing (MCPReflection_PCG.cpp). UPCGGraph.UserParameters is a reflected
    // FInstancedPropertyBag but unreal.InstancedPropertyBag exposes NO add/remove/rename-property surface;
    // UPCGSettingsWithDynamicInputs::OnUserAdd/RemoveDynamicInputPin are WITH_EDITOR PCG_API with no reflected
    // surface -> both are C++-only. SCHEMA via UPCGGraph::AddUserParameters / UpdateUserParametersStruct(Bag ->
    // RemovePropertyByName) / RenameUserParameter (all PCG_API); enum via GetUserParametersStruct()->
    // GetPropertyBagStruct()->GetPropertyDescs(). Dynamic pins via OnUserAdd/RemoveDynamicInputPin GUARDED by
    // CanUserRemoveDynamicInputPin (the remove has check() asserts). Build.cs += "PCG" (RUNTIME module; all
    // symbols PCG_API) -> NO engine export patch. PropertyBag types are in CoreUObject (already a dep). Schema
    // does NOT duplicate Wave-3 PCGGraphParametersHelpers (which does typed VALUE get/set on the same bag). ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString ListPCGGraphParametersJson(const FString& GraphPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetPCGGraphParameterJson(const FString& GraphPath, const FString& Name);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddPCGGraphParameterJson(const FString& GraphPath, const FString& Name, const FString& Type);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemovePCGGraphParameterJson(const FString& GraphPath, const FString& Name);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RenamePCGGraphParameterJson(const FString& GraphPath, const FString& OldName, const FString& NewName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString AddPCGDynamicInputPinJson(const FString& GraphPath, const FString& NodeName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString RemovePCGDynamicInputPinJson(const FString& GraphPath, const FString& NodeName, int32 PinIndex);

    // ==== C++ #49 2026-08-19 (control rig, LAST CR pair): Control Rig editor PREVIEW-animation play/stop
    // (MCPReflection_ControlRigPreview.cpp). The CR editor preview mesh is a UDebugSkelMeshComponent (UnrealEd,
    // public) in an EditorPreview world whose UAnimPreviewInstance (AnimGraph, public) drives single-node
    // playback -- found via TObjectIterator (NO private FControlRigEditor toolkit headers). CR editor opens
    // headless in this build. play<->stop = natural non-ledgered inverse pair. UnrealEd(public)+AnimGraph(#18
    // private) already deps -> NO Build.cs change, NO export patch. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString PlayRigPreviewAnimationJson(const FString& PreviewMeshPath, const FString& AnimPath, float PlayRate, bool bLooping);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString StopRigPreviewAnimationJson(const FString& PreviewMeshPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetRigPreviewStateJson(const FString& PreviewMeshPath);

    // ==== C++ #50 2026-08-19 (niagara, LAST niagara verb): stack-issue list + auto-fix
    // (MCPReflection_NiagaraStack.cpp). Niagara stack issues are FStackIssue on UNiagaraStackEntry entries
    // of the editor's UNiagaraStackViewModel(s); each fix is an FStackIssueFixDelegate (simple delegate).
    // Reached via TObjectIterator once the caller opens the system editor (asset editors open headless).
    // Niagara+NiagaraEditor already deps (#5). fix = repair op -> NON-LEDGERED. ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetNiagaraStackIssuesJson(const FString& SystemPath);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString FixNiagaraStackIssueJson(const FString& SystemPath, const FString& IssueIdentifier, int32 FixIndex, bool bFixAll);

    // ==== C++ #51 2026-08-20 (pcg, EXECUTION INSPECTION): the ~13-feature blocked group, now feasible.
    // (MCPReflection_PCGInspection.cpp). Not blocked: PCG_PROFILING_ENABLED == 1 whenever WITH_EDITOR
    // (PCGCommon.h), so FPCGGraphExecutionInspection exists; every method on it is PCG_API. Reached from a
    // UPCGComponent via Comp->GetExecutionState().GetInspection() (IPCGGraphExecutionState, PCG_API). Handlers
    // take an ACTOR path (resolve actor in the editor world -> FindComponentByClass<UPCGComponent>, Wave-4 style).
    // GetExecutedNodeStacks() hands us every (UPCGNode*, FPCGStack) pair after a generation (never construct a
    // stack). All are transient runtime reads / enable-disable-clear -> NON-LEDGERED, NO editor_level.undo folds
    // (enable<->disable natural pair; clear idempotent) -> ZERO undo risk. Build.cs already has "PCG" (Wave-5). ====
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString SetPCGInspectionEnabledJson(const FString& ActorPath, bool bEnable);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString GetPCGInspectionJson(const FString& ActorPath, const FString& NodeName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Reflection")
    static FString InspectPCGNodeOutputJson(const FString& ActorPath, const FString& NodeName);
    UFUNCTION(BlueprintCallable, Category = "MCP|Authoring")
    static FString ClearPCGInspectionJson(const FString& ActorPath);
};
