// PerfTraceTool — M4.4 PoC（已定性，2026-11-04）：本目标在安装版引擎上**编不出来**，保留作证据。
// UBT 两道墙（源码级核实）：1) TargetType.Program 默认 BuildEnvironment.Unique，
// Unique+IsEngineInstalled() 直接 RulesError（RulesAssembly.cs:710）；改 Shared 后仍被
// "Program targets are not currently supported from this engine distribution" 拒绝
// （UEBuildTarget.cs:1403，InstalledPlatformInfo 把 Program 降级为 Game，无开关）。
// 结论见 PLAN M4.4：安装版跑自定义分析代码只有 commandlet 一条路；秒级靠「B2 常驻」而非换二进制。
using UnrealBuildTool;

public class PerfTraceToolTarget : TargetRules
{
	public PerfTraceToolTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Program;
		LinkType = TargetLinkType.Monolithic;
		// 安装版引擎只允许 Shared（UBT 规则：Unique + IsEngineInstalled() 直接 RulesError）。
		// 代价是所有依赖模块要从安装版自带源码现编一遍（一次性）。
		BuildEnvironment = TargetBuildEnvironment.Shared;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("PerfTraceTool");
	}
}
