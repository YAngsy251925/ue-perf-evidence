// PerfTraceToolMain.cpp — M4.4 PoC 主程序
//
// 回答 M4.4 的「若能」分支：B2 的 TraceServices 读取代码能不能不经 UnrealEditor-Cmd
// 编辑器 boot 直接跑。Hive 线索核实（2026-11-04）：UnrealInsights.exe 只能跑内建
// TimingInsights.Export* 控制台命令（其 ProofScripts 与我们 A 路同一形态），自定义分析
// 代码在他们那边也是走 UnrealEditor-Cmd commandlet —— 所以「经 UnrealInsights.exe」
// 此路不通；本 PoC 验证替代路线：Program 目标直链 TraceServices，期望 boot+分析 秒级。
//
// 用法：PerfTraceTool.exe trace=<abs .utrace> out=<abs .json> [limit=N]
// 输出 JSON 与 B2 `mode frames` 同形（perf-b2-frames/0），便于将来 Python 侧无缝切换。

#include "RequiredProgramMainCPPInclude.h"
#include "HAL/PlatformTime.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Modules/ModuleManager.h"
#include "TraceServices/ITraceServicesModule.h"
#include "TraceServices/AnalysisService.h"
#include "TraceServices/Model/AnalysisSession.h"
#include "TraceServices/Model/Frames.h"

IMPLEMENT_APPLICATION(PerfTraceTool, "PerfTraceTool");

// 参数取值：既认 key=value，也认 key value（与 B2 宿主同规则，P19 的两种形态都接受）。
static FString ParseArg(const FString& Params, const TCHAR* Key)
{
	const FString Clean = Params.Replace(TEXT("\""), TEXT(""));
	FString Value;
	if (FParse::Value(*Clean, *(FString(Key) + TEXT("=")), Value))
	{
		return Value;
	}
	TArray<FString> Tokens;
	Clean.ParseIntoArrayWS(Tokens);
	for (int32 i = 0; i + 1 < Tokens.Num(); ++i)
	{
		if (Tokens[i].Equals(Key, ESearchCase::IgnoreCase))
		{
			return Tokens[i + 1];
		}
	}
	return FString();
}

static FString JsonEscape(const TCHAR* Text)
{
	if (!Text) { return FString(); }
	FString Out(Text);
	Out.ReplaceInline(TEXT("\\"), TEXT("\\\\"));
	Out.ReplaceInline(TEXT("\""), TEXT("\\\""));
	Out.ReplaceInline(TEXT("\n"), TEXT("\\n"));
	Out.ReplaceInline(TEXT("\r"), TEXT("\\r"));
	Out.ReplaceInline(TEXT("\t"), TEXT("\\t"));
	return Out;
}

// 与 B2 EmitFrames 逐字段同形（perf-b2-frames/0）：R13 丢非有限帧，limit 截断但 count 是全量。
static void EmitFrames(const TraceServices::IAnalysisSession& Session, const FString& TracePath,
	const FString& OutPath, int32 Limit)
{
	const TraceServices::IFrameProvider& Frames = TraceServices::ReadFrameProvider(Session);
	const uint64 GameCount = Frames.GetFrameCount(TraceFrameType_Game);
	const uint64 RenderCount = Frames.GetFrameCount(TraceFrameType_Rendering);
	const double EndSec = Session.GetDurationSeconds();

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-frames/0\",\"trace\":\"%s\",\"duration_ms\":%.4f,"),
		*JsonEscape(*TracePath), EndSec * 1000.0);

	auto EmitType = [&](const TCHAR* Key, ETraceFrameType Type, uint64 Count)
	{
		Out += FString::Printf(TEXT("\"%s\":{\"count\":%llu,\"frames\":["), Key, (unsigned long long)Count);
		uint64 Emitted = 0;
		Frames.EnumerateFrames(Type, 0ull, Count, [&](const TraceServices::FFrame& Frame)
		{
			if (Limit > 0 && Emitted >= (uint64)Limit) { return; }
			if (!FMath::IsFinite(Frame.StartTime) || !FMath::IsFinite(Frame.EndTime)) { return; }
			if (Emitted > 0) { Out += TEXT(","); }
			Out += FString::Printf(TEXT("{\"idx\":%llu,\"start_ms\":%.4f,\"end_ms\":%.4f,\"duration_ms\":%.4f}"),
				(unsigned long long)Frame.Index, Frame.StartTime * 1000.0, Frame.EndTime * 1000.0,
				(Frame.EndTime - Frame.StartTime) * 1000.0);
			++Emitted;
		});
		Out += FString::Printf(TEXT("],\"emitted\":%llu}"), (unsigned long long)Emitted);
	};

	EmitType(TEXT("game"), TraceFrameType_Game, GameCount);
	Out += TEXT(",");
	EmitType(TEXT("render"), TraceFrameType_Rendering, RenderCount);
	Out += TEXT("}");

	if (!FFileHelper::SaveStringToFile(Out, *OutPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		UE_LOG(LogTemp, Error, TEXT("PERF_ERR write failed: %s"), *OutPath);
		return;
	}
	UE_LOG(LogTemp, Display, TEXT("PERF_OUT %s"), *OutPath);
	UE_LOG(LogTemp, Display, TEXT("PERF_FRAMES game=%llu render=%llu duration_ms=%.4f"),
		(unsigned long long)GameCount, (unsigned long long)RenderCount, EndSec * 1000.0);
}

int main(int argc, char* argv[])
{
	const double TStart = FPlatformTime::Seconds();
	FEngineLoop EngineLoop;
	int32 ErrorLevel = EngineLoop.PreInit(argc, argv);
	double TPreInit = FPlatformTime::Seconds();
	{
		if (ErrorLevel == 0)
		{
			EngineLoop.Init();
		}
		double TInit = FPlatformTime::Seconds();

		const FString Cmdline = FCommandLine::Get();
		const FString TracePath = ParseArg(Cmdline, TEXT("trace"));
		const FString OutPath = ParseArg(Cmdline, TEXT("out"));
		int32 Limit = 0;
		{
			const FString LimitText = ParseArg(Cmdline, TEXT("limit"));
			if (!LimitText.IsEmpty()) { Limit = FCString::Atoi(*LimitText); }
		}

		if (TracePath.IsEmpty())
		{
			UE_LOG(LogTemp, Error, TEXT("PERF_ERR missing trace=<file>"));
			ErrorLevel = 1;
		}
		else if (ErrorLevel == 0)
		{
			UE_LOG(LogTemp, Display, TEXT("PERF_ANALYZE begin %s"), *TracePath);
			ITraceServicesModule& Module = FModuleManager::LoadModuleChecked<ITraceServicesModule>(TEXT("TraceServices"));
			TSharedPtr<TraceServices::IAnalysisService> Service = Module.GetAnalysisService();
			if (!Service.IsValid()) { Service = Module.CreateAnalysisService(); }
			if (!Service.IsValid())
			{
				UE_LOG(LogTemp, Error, TEXT("PERF_ERR no analysis service"));
				ErrorLevel = 1;
			}
			else
			{
				TSharedPtr<const TraceServices::IAnalysisSession> Session = Service->Analyze(*TracePath);
				if (!Session.IsValid())
				{
					UE_LOG(LogTemp, Error, TEXT("PERF_ERR analyze failed"));
					ErrorLevel = 1;
				}
				else
				{
					Session->Wait();
					double TAnalyzed = FPlatformTime::Seconds();
					{
						TraceServices::FAnalysisSessionReadScope ReadScope(*Session);
						EmitFrames(*Session, TracePath, OutPath, Limit);
					}
					double TDone = FPlatformTime::Seconds();
					UE_LOG(LogTemp, Display,
						TEXT("PERFTIMING preinit=%.2fs init=%.2fs analyze=%.2fs emit=%.2fs total=%.2fs"),
						TPreInit - TStart, TInit - TPreInit, TAnalyzed - TInit, TDone - TAnalyzed, TDone - TStart);
					fflush(stdout);
				}
			}
		}
	}
	EngineLoop.Exit();
	return ErrorLevel;
}
