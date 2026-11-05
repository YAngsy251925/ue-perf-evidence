// PerfEvidenceToolset - AI-callable performance evidence tools (ue-perf-evidence M3.1).

using UnrealBuildTool;

public class PerfEvidenceToolset : ModuleRules
{
	public PerfEvidenceToolset(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PrivateDependencyModuleNames.AddRange(
			new string[]
			{
				"Core",
				"CoreUObject",
				"Engine",
				"ToolsetRegistry",
				// UMG/Blueprint 结构导出（PerfEvidenceUILibrary）需要这些编辑器模块
				"UMG",
				"UMGEditor",
				"UnrealEd",
				"AssetRegistry",
			}
			);
	}
}
