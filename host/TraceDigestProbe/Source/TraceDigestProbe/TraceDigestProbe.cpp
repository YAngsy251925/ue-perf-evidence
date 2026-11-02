#include "CoreMinimal.h"
#include "TraceDigestCommandlet.h"
#include "Modules/ModuleManager.h"
#include "HAL/IConsoleManager.h"
#include "Containers/Ticker.h"
#include "Engine/Engine.h"
#if WITH_EDITOR
#include "TraceServices/ITraceServicesModule.h"
#include "TraceServices/AnalysisService.h"
#include "TraceServices/Model/AnalysisSession.h"
#include "TraceServices/Model/TimingProfiler.h"
#include "TraceServices/Model/Bookmarks.h"
#endif
#if CSV_PROFILER
#include "ProfilingDebugging/CsvProfiler.h"
#endif

IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, TraceDigestProbe, "TraceDigestProbe");

// PerfEvidence：perf_capture 的进程内配合端（M2.2）。
// coverage 起点：PerfEvidence.CoverageStart <秒> 在运行该秒数后发 CSV_EVENT_GLOBAL
//   "PerfEvidence CoverageStart"（同时是 TRACE_BOOKMARK，trace 里有同名书签）；
// 停止：PerfEvidence.CaptureStop <秒> 在运行该秒数后发 "PerfEvidence CaptureStop"，
//   命令行 -csvStopOnEvent="PerfEvidence CaptureStop" 看到它就 EndCapture，
//   随后 -ExitAfterCsvProfiling 让引擎写完 CSV 干净退出。
// 两个事件都走 FTSTicker（game thread 端），从第一个 world tick 开始计时。
#if CSV_PROFILER
static void PerfEvidenceScheduleEvents()
{
	const double CoverageStartDelay = [&]
	{
		double V = 0.0;
		FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.CoverageStart="), V);
		return V;
	}();
	const double CaptureStopDelay = [&]
	{
		double V = 0.0;
		FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.CaptureStop="), V);
		return V;
	}();

	// PerfEvidence.ExecAt=<秒>:<命令> — 在运行该秒数后把命令排进延迟命令队列
	//（与 -ExecCmds 同一执行路径；hitch-hunt 用它注入 memreport 制造确定性 hitch，M2.3）。
	// 命令里不能有逗号（命令行按逗号分割 token），多个 ExecAt 用分号不安全，目前只支持一个。
	FString ExecAt;
	if (FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.ExecAt="), ExecAt) && ExecAt.Contains(TEXT(":")))
	{
		int32 Sep = INDEX_NONE;
		ExecAt.FindChar(TEXT(':'), Sep);
		const double DelaySeconds = FCString::Atod(*ExecAt.Left(Sep));
		const FString Command = ExecAt.Mid(Sep + 1);
		if (DelaySeconds > 0.0 && !Command.IsEmpty())
		{
			FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceExec"), float(DelaySeconds),
				[Command](float)
				{
					UE_LOGF(LogTemp, Display, "PerfEvidence: queueing exec [%ls]", *Command);
					if (GEngine)
					{
						GEngine->DeferredCommands.Add(Command);
					}
					return false; // one-shot
				});
		}
	}

	auto MakeTicker = [](double DelaySeconds, const TCHAR* EventText)
	{
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceEvent"), float(DelaySeconds),
			[EventText](float)
			{
				UE_LOGF(LogTemp, Display, "PerfEvidence: firing %ls", EventText);
				CSV_EVENT_GLOBAL(TEXT("%ls"), EventText);
				return false; // one-shot
			});
	};

	if (CoverageStartDelay > 0.0)
	{
		MakeTicker(CoverageStartDelay, TEXT("PerfEvidence CoverageStart"));
	}
	if (CaptureStopDelay > 0.0)
	{
		MakeTicker(CaptureStopDelay, TEXT("PerfEvidence CaptureStop"));
	}
}
static struct FPerfEvidenceInit
{
	FPerfEvidenceInit() { PerfEvidenceScheduleEvents(); }
} GPerfEvidenceInit;
#endif

// UTraceDigestPerfCommandlet 的实现体在文件尾（WITH_EDITOR 块里，紧挨 RunTraceDigestPerf）。

#if WITH_EDITOR
static void RunTraceDigest(const TArray<FString>& Args, UWorld*, FOutputDevice& Ar)
{
	FString TracePath;
	for (const FString& A : Args)
	{
		if (A.StartsWith(TEXT("trace="))) { TracePath = A.RightChop(6); }
	}
	if (TracePath.IsEmpty()) { Ar.Logf(TEXT("DIGEST_ERR missing trace=<file>")); return; }

	ITraceServicesModule& Module = FModuleManager::LoadModuleChecked<ITraceServicesModule>(TEXT("TraceServices"));
	TSharedPtr<TraceServices::IAnalysisService> Service = Module.GetAnalysisService();
	if (!Service.IsValid()) { Service = Module.CreateAnalysisService(); }
	if (!Service.IsValid()) { Ar.Logf(TEXT("DIGEST_ERR no analysis service")); return; }

	TSharedPtr<const TraceServices::IAnalysisSession> Session = Service->Analyze(*TracePath);
	if (!Session.IsValid()) { Ar.Logf(TEXT("DIGEST_ERR analyze failed")); return; }
	Session->Wait();

	int32 TimerCount = 0;
	double DurationSeconds = 0.0;
	for (int32 Attempt = 0; Attempt < 3 && TimerCount == 0; ++Attempt)
	{
		Session->Wait();
		{
			TraceServices::FAnalysisSessionReadScope ReadScope(*Session);
			const TraceServices::ITimingProfilerProvider* Timers =
				TraceServices::ReadTimingProfilerProvider(*Session);
			if (Timers)
			{
				Timers->ReadTimers([&TimerCount](const TraceServices::ITimingProfilerTimerReader& Reader)
				{
					TimerCount = (int32)Reader.GetTimerCount();
				});
			}
			DurationSeconds = Session->GetDurationSeconds();
		}
	}
	Ar.Logf(TEXT("DIGEST_OK duration=%.3fs timers=%d trace=%s"),
		DurationSeconds, TimerCount, *TracePath);
}

static FAutoConsoleCommandWithWorldArgsAndOutputDevice GTraceDigestCmd(
	TEXT("TraceDigest"),
	TEXT("TraceDigest trace=<path.utrace>  -- offline analysis via TraceServices (headless probe)"),
	FConsoleCommandWithWorldArgsAndOutputDeviceDelegate::CreateStatic(&RunTraceDigest));

static FString ParseUnquoted(const FString& Params, const TCHAR* Key)
{
	// FParse::Value 要求匹配键前面是空白；bat/引擎转发会把 token 重排成 "prefix=X"（引号在键前），
	// 所以先剥掉全部引号再解析。这里的值（路径、事件文本）本身不含引号。
	FString Clean = Params.Replace(TEXT("\""), TEXT(""));
	FString Value;
	FParse::Value(*Clean, Key, Value);
	return Value;
}

// TraceDigestPerf 的实现（-run=TraceDigestPerf 经 UTraceDigestPerfCommandlet 进来）。
// 输出 trace 里以 prefix 开头的书签：PerfEvidence CoverageStart / CaptureStop 的 trace 侧证据
//（M2.2 的 coverage 边界）。
void RunTraceDigestPerf(const FString& Params, FOutputDevice& Ar)
{
	FString TracePath = ParseUnquoted(Params, TEXT("trace="));
	FString Prefix = ParseUnquoted(Params, TEXT("prefix="));
	if (TracePath.IsEmpty()) { Ar.Logf(TEXT("PERF_ERR missing trace=<file>")); return; }

	ITraceServicesModule& Module = FModuleManager::LoadModuleChecked<ITraceServicesModule>(TEXT("TraceServices"));
	TSharedPtr<TraceServices::IAnalysisService> Service = Module.GetAnalysisService();
	if (!Service.IsValid()) { Service = Module.CreateAnalysisService(); }
	if (!Service.IsValid()) { Ar.Logf(TEXT("PERF_ERR no analysis service")); return; }

	TSharedPtr<const TraceServices::IAnalysisSession> Session = Service->Analyze(*TracePath);
	if (!Session.IsValid()) { Ar.Logf(TEXT("PERF_ERR analyze failed")); return; }
	Session->Wait();
	TraceServices::FAnalysisSessionReadScope ReadScope(*Session);
	const TraceServices::IBookmarkProvider& Bookmarks = TraceServices::ReadBookmarkProvider(*Session);
	uint64 Count = Bookmarks.GetBookmarkCount();
	Ar.Logf(TEXT("PERF_BOOKMARKS count=%llu"), (unsigned long long)Count);
	Bookmarks.EnumerateBookmarks(0.0, Session->GetDurationSeconds(), [&](const TraceServices::FBookmark& B)
	{
		if (Prefix.IsEmpty() || FString(B.Text).StartsWith(Prefix))
		{
			Ar.Logf(TEXT("PERF_BOOKMARK %.6f %ls"), B.Time, B.Text);
		}
	});
	Ar.Logf(TEXT("PERF_DONE"));
}

UTraceDigestPerfCommandlet::UTraceDigestPerfCommandlet()
{
	IsClient = false;
	IsServer = false;
	IsEditor = true;
	LogToConsole = true;
}

int32 UTraceDigestPerfCommandlet::Main(const FString& Params)
{
	RunTraceDigestPerf(Params, *GLog);
	return 0;
}
#endif // WITH_EDITOR
