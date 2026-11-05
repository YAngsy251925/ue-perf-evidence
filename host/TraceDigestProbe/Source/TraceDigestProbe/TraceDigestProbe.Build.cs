using UnrealBuildTool;
public class TraceDigestProbe : ModuleRules
{
	public TraceDigestProbe(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "RenderCore", "RHI", "Sockets", "Networking" });
		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.AddRange(new string[] { "TraceAnalysis", "TraceServices", "Projects",
				"SlateInsights", "SlateCore" });
			// SlateInsights 的 FSlateProvider 头在插件 Private 目录里，显式加 include path
			PrivateIncludePaths.Add(System.IO.Path.Combine(EngineDirectory,
				"Plugins/Slate/SlateInsights/Source/SlateInsights/Private"));
		}
	}
}
