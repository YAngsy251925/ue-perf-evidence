using UnrealBuildTool;
public class TraceDigestProbeEditorTarget : TargetRules
{
	public TraceDigestProbeEditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("TraceDigestProbe");
	}
}
