using UnrealBuildTool;
public class TraceDigestProbe : ModuleRules
{
	public TraceDigestProbe(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "RenderCore", "RHI", "Sockets", "Networking" });
		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.AddRange(new string[] { "TraceAnalysis", "TraceServices", "Projects" });
		}
	}
}
