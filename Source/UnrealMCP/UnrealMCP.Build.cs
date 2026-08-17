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
				// C++ #14 (StateTree editor property-BINDINGS reader): UStateTreeEditorData / UStateTreeState /
				// FStateTreeEditorNode live in the EDITOR module StateTreeEditorModule. TOP RISK: if those
				// classes aren't STATETREEEDITORMODULE_API-exported the bindings reader FAILS TO LINK -> a
				// source-engine export patch may be needed (same as the NIAGARAEDITOR_API round). Isolated here
				// so it can't affect the other rounds.
				"StateTreeEditorModule",
				// C++ #14 fix: UE 5.8 genericized StateTree bindings onto PropertyBindingUtils —
				// FPropertyBindingPath::ToString() is PROPERTYBINDINGUTILS_API, so link it directly.
				"PropertyBindingUtils"
			}
		);
			
		
		PrivateDependencyModuleNames.AddRange(
			new string[] { 
				"Json", "JsonUtilities", "Settings", "InputCore", "PythonScriptPlugin",
				"Kismet", "KismetWidgets"
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
