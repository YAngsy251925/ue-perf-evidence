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
			}
			);
	}
}
