// PerfEvidenceHost - in-process side of perf_capture, portable across projects (M3.4).

using UnrealBuildTool;

public class PerfEvidenceHost : ModuleRules
{
	public PerfEvidenceHost(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(
			new string[]
			{
				"Core",
				"CoreUObject",
				"Engine",
				// 每帧采样器要读 RenderTimer.h 的 cycle 计数器
				"RenderCore",
				"RHI",
			}
			);

		if (Target.bBuildEditor)
		{
			// PerfEvidenceUILibrary（UMG/Blueprint 结构导出）只在编辑器构建里编：
			// WidgetBlueprint.h 来自 UMGEditor，所以这些依赖必须加上，否则 UBT 找不到头文件。
			PrivateDependencyModuleNames.AddRange(new string[]
			{
				"UMG", "UMGEditor", "UnrealEd", "AssetRegistry",
			});
		}
	}
}
