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
};
