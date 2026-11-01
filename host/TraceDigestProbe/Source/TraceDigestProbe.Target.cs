using UnrealBuildTool;
public class TraceDigestProbeTarget : TargetRules
{
	public TraceDigestProbeTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Game;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("TraceDigestProbe");
	}
}
