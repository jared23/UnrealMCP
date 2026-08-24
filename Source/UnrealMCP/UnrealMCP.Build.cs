// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;

public class UnrealMCP : ModuleRules
{
	public UnrealMCP(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;
		
		PublicIncludePaths.AddRange(
			new string[] {
				// ... add public include paths required here ...
			}
			);
				
		
		PrivateIncludePaths.AddRange(
			new string[] {
				// ... add other private include paths required here ...
			}
			);
			
		
		PublicDependencyModuleNames.AddRange(
			new string[] {
				"Core", "CoreUObject", "Engine", "UnrealEd",
				"Networking", "Sockets", "Slate", "SlateCore", "EditorStyle",
				"DeveloperSettings", "Projects", "ToolMenus",
				"BlueprintGraph", "GraphEditor", "KismetCompiler",
				// C++ #5 (Niagara authoring): UNiagaraSystem/UNiagaraEmitter (Niagara runtime) +
				// FNiagaraEditorUtilities (NiagaraEditor). First Build.cs dep change in the plugin.
				"Niagara", "NiagaraEditor",
				// C++ #6 (gameplay-tag authoring): UGameplayTagsManager (GameplayTags) +
				// IGameplayTagsEditorModule::AddNewGameplayTagToINI/DeleteTagFromINI (GameplayTagsEditor).
				"GameplayTags", "GameplayTagsEditor",
				// C++ #22 (gas RUNTIME reader): UAbilitySystemComponent / FGameplayAttribute / FGameplayAbilitySpec
				// (GameplayAbilities plugin, already enabled in the .uproject) for get_ability_system_info on a live ASC.
				"GameplayAbilities",
				// C++ #8 (widget-tree authoring): UWidgetTree/UWidget/UPanelWidget (UMG runtime) +
				// UWidgetBlueprint (UMGEditor). Root widget is a protected member -> needs C++.
				"UMG", "UMGEditor",
				// C++ #11 (BehaviorTree editor-graph): AIModule (runtime BT types) + AIGraph
				// (UAIGraph/UAIGraphNode/FGraphNodeClassData/AddSubNode) + BehaviorTreeEditor
				// (UBehaviorTreeGraph/UBehaviorTreeGraphNode_*/UEdGraphSchema_BehaviorTree). TOP RISK:
				// these editor classes may not be *_API-exported -> possible source-engine export patch.
				"AIModule", "AIGraph", "BehaviorTreeEditor",
				// C++ #12 (deferred-reflection READER batch): StateTreeModule (runtime — base node
				// structs FStateTreeTaskBase/EvaluatorBase/ConditionBase/ConsiderationBase for the native
				// node-type registry) + RigVM (runtime — URigVMHost/URigVM/FRigVMByteCode/FRigVMStatistics/
				// FRigVMExternalVariable/URigVMMemoryStorage for the compiled-VM reader). Both RUNTIME modules
				// (RIGVM_API-style export) -> LOW export-macro risk. The StateTree editor property-BINDINGS
				// resolver is deferred (it needs StateTreeEditorModule with real *_API export risk).
				"StateTreeModule", "RigVM",
				// C++ #28 (Control Rig RUNTIME eval): the ControlRig runtime module (UControlRig/URigHierarchy —
				// CONTROLRIG_API) for MCPReflection_ControlRigRuntime.cpp. Runtime module, low export risk.
				"ControlRig",
				// C++ #14 (StateTree editor property-BINDINGS reader): UStateTreeEditorData / UStateTreeState /
				// FStateTreeEditorNode live in the EDITOR module StateTreeEditorModule. TOP RISK: if those
				// classes aren't STATETREEEDITORMODULE_API-exported the bindings reader FAILS TO LINK -> a
				// source-engine export patch may be needed (same as the NIAGARAEDITOR_API round). Isolated here
				// so it can't affect the other rounds.
				"StateTreeEditorModule",
				// C++ #14 fix: UE 5.8 genericized StateTree bindings onto PropertyBindingUtils —
				// FPropertyBindingPath::ToString() is PROPERTYBINDINGUTILS_API, so link it directly.
				"PropertyBindingUtils",
				// C++ #38 (widget ANIMATIONS, W-C): UMovieScene/possessables/bindings/channels (MovieScene) +
				// property tracks + sections (MovieSceneTracks). Both RUNTIME modules -> LOW link risk.
				"MovieScene", "MovieSceneTracks",
				// C++ #39 (widget MVVM, W-D): the beta ModelViewViewModel plugin (enabled in the .uproject).
				// Runtime enums/FMVVMConstFieldVariant (ModelViewViewModel) + UMVVMBlueprintView/contexts/bindings
				// (ModelViewViewModelBlueprint, UncookedOnly) + UMVVMEditorSubsystem (ModelViewViewModelEditor).
				"ModelViewViewModel", "ModelViewViewModelBlueprint", "ModelViewViewModelEditor",
				// C++ #42 (world infra): editor modes (EditorFramework -- FEditorModeInfo/EM_Default) + nav build
				// status (NavigationSystem). Both are transitive UnrealEd deps; listed explicitly for link robustness.
				"NavigationSystem", "EditorFramework",
				// C++ #43 (LANDSCAPE / terrain edit-data bridge): ALandscapeProxy::Import + FLandscapeEditDataInterface /
				// FHeightmapAccessor / TAlphamapAccessor + ULandscapeInfo/ULandscapeLayerInfoObject. ALL symbols are
				// LANDSCAPE_API-exported (or header-only templates calling exported methods) -> NO engine export patch.
				// The RUNTIME "Landscape" module ONLY -- the section math is done host-side in Python, so the editor
				// LandscapeEditor module (FLandscapeImportHelper) is NOT needed. See MCPReflection_Landscape.cpp.
				// Foliage: LandscapeEdit.h transitively #includes InstancedFoliageActor.h -> need Foliage's include path.
				"Landscape", "Foliage"
			}
		);
			
		
		PrivateDependencyModuleNames.AddRange(
			new string[] {
				"Json", "JsonUtilities", "Settings", "InputCore", "PythonScriptPlugin",
				"Kismet", "KismetWidgets",
				// C++ #18 (AnimGraph authoring, ISOLATED MCPReflection_AnimGraph.cpp): UAnimGraphNode_*/
				// UAnimStateNode/UAnimStateTransitionNode/UAnimationStateMachineGraph (AnimGraph editor module)
				// + embedded FAnimNode_* (AnimGraphRuntime). TOP LINK RISK: if the AnimGraph editor symbols
				// (esp. UAnimStateTransitionNode::CreateConnections / UAnimGraphNode_Base::SetPinVisibility)
				// aren't ANIMGRAPH_API-exported for a plugin -> FAILS TO LINK; drop that .cpp if so.
				"AnimGraph", "AnimGraphRuntime",
				// C++ #47 (AUDIO discovery, MCPReflection_AudioCpp.cpp): MetaSound frontend registries
				// (Metasound::Frontend::ISearchEngine / IDataTypeRegistry / INodeClassRegistry /
				// IInterfaceRegistry + FMetasoundFrontendClass/Metadata/ClassInterface). ALL symbols are
				// METASOUNDFRONTEND_API-exported (the header `UE_API` macro) -> NO engine export patch.
				// MetasoundFrontend is the RUNTIME module of the Metasound plugin (EnabledByDefault=true, loads
				// without a .uproject/.uplugin edit, same as the already-used Niagara/StateTree plugin modules).
				// MetasoundEngine is NOT needed (no UMetaSoundSource/editor types used). SoundSubmix (#38) is in
				// the Engine module (already a dep) -> no dep needed for the submix writer.
				"MetasoundFrontend",
				// C++ #46 (mutable Wave 3 graph authoring): UCustomizableObject::GetPrivate()->GetSource()
				// (CustomizableObject, Runtime) + UCustomizableObjectNode / UEdGraphSchema_CustomizableObject
				// (CustomizableObjectEditor, UncookedOnly). Both method-level *_API-exported despite MinimalAPI
				// -> NO engine export patch. MutableTools NOT needed (only the UEdGraph node/schema layer). The
				// .h signatures use only FString/float so CO types never cross the module's public interface.
				"CustomizableObject", "CustomizableObjectEditor",
				// C++ #48 (PCG Wave 5 — graph-parameter SCHEMA + dynamic-input-pin authoring,
				// MCPReflection_PCG.cpp): UPCGGraph::AddUserParameters / UpdateUserParametersStruct /
				// RenameUserParameter + UPCGNode::GetSettings + UPCGSettingsWithDynamicInputs::OnUserAdd/
				// RemoveDynamicInputPin ALL live in the RUNTIME "PCG" module and are PCG_API-exported (the
				// class UCLASS(MinimalAPI) macros still export StaticClass + the UE_API=PCG_API methods) ->
				// NO engine export patch. The FInstancedPropertyBag / FPropertyBagPropertyDesc /
				// EPropertyBagPropertyType schema types are in CoreUObject in UE 5.8
				// (Engine/Source/Runtime/CoreUObject/Public/StructUtils/PropertyBag.h, UE_API=COREUOBJECT_API)
				// -> already a public dep, no StructUtils module needed. PCG's PUBLIC deps (ComputeFramework,
				// Foliage, Landscape, Geometry*) bring every transitive include of PCGGraph.h; its one editor
				// include (EdGraphNode_Comment.h) resolves via the plugin's existing UnrealEd dep.
				"PCG"
			}
		);
		
		
		DynamicallyLoadedModuleNames.AddRange(
			new string[]
			{
				// ... add any modules that your module loads dynamically here ...
			}
			);
	}
}
