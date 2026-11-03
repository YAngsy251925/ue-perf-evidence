// PerfTraceTool — 依赖刻意收窄：只要 TraceServices 分析链（Core/CoreUObject/TraceAnalysis），
// 不引引擎 UI / RHI / 编辑器模块。TraceServices 的完整依赖（Cbor/SymsLib/Asio/OpenSSL/Sockets/TraceLog）
// 由 UBT 传递解析，全部随安装版引擎带源码（2026-11-04 逐一核实过存在）。
using UnrealBuildTool;

public class PerfTraceTool : ModuleRules
{
	public PerfTraceTool(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.NoPCHs;
		PrivateDependencyModuleNames.AddRange(
			new string[] {
				"Core",
				"CoreUObject",
				"TraceAnalysis",
				"TraceServices",
			}
		);
	}
}
