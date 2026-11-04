// PerfEvidenceHost — perf_capture 的进程内配合端，做成插件以便装进任意工程（M3.4）。
//
// 提供两件事：
//   1) P15 的 CSV 事件对：PerfEvidence.CoverageStart <秒> / PerfEvidence.CaptureStop <秒> 到点
//      发 CSV_EVENT_GLOBAL（同一个事件也是 TRACE_BOOKMARK，trace 里能读到）。配合
//      -csvStartOnEvent / -csvStopOnEvent / -ExitAfterCsvProfiling，引擎自己写完 CSV 干净退出。
//   2) 可逆的回归注入：PerfEvidence.BusyCount 个忙等 tickable，每个每帧空转 BusyMs 毫秒，
//      span 名 PerfEvidenceRegress —— A/B/C 三组只差这两个 cvar，撤销 = 改回 0。
//
// 纪律（PLAN P17）：静态初始化阶段只做纯内存的事（注册 cvar、挂 ticker），
// 不建线程、不碰 socket / 后建的子系统。

#include "Modules/ModuleManager.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformTime.h"
#include "Containers/Ticker.h"
#include "ProfilingDebugging/CpuProfilerTrace.h"
#include "Tickable.h"
#include "Engine/Engine.h"

IMPLEMENT_MODULE(FDefaultModuleImpl, PerfEvidenceHost);

#if !UE_BUILD_SHIPPING
// ---------------------------------------------------------------- 注入

static TAutoConsoleVariable<int32> CVarPerfBusyCount(
	TEXT("PerfEvidence.BusyCount"), 0,
	TEXT("M3.4 回归注入：每帧注入 N 个忙等 tickable（0 = 关闭）。配合 PerfEvidence.BusyMs。"),
	ECVF_Cheat);
static TAutoConsoleVariable<float> CVarPerfBusyMs(
	TEXT("PerfEvidence.BusyMs"), 2.0f,
	TEXT("M3.4 回归注入：每个忙等 tickable 每帧空转的毫秒数。"),
	ECVF_Cheat);

class FPerfEvidenceBusyTicker : public FTickableGameObject
{
public:
	virtual void Tick(float) override
	{
		TRACE_CPUPROFILER_EVENT_SCOPE_STR("PerfEvidenceRegress");
		const double EndSeconds = FPlatformTime::Seconds()
			+ CVarPerfBusyMs.GetValueOnGameThread() / 1000.0;
		double Sink = 0.0;
		while (FPlatformTime::Seconds() < EndSeconds)
		{
			Sink += FMath::Sqrt(Sink + 1.0); // 真烧 CPU，避免被优化成空循环
		}
		LastSink = Sink;
	}
	virtual TStatId GetStatId() const override
	{
		RETURN_QUICK_DECLARE_CYCLE_STAT(FPerfEvidenceBusyTicker, STATGROUP_Tickables);
	}
	double LastSink = 0.0;
};

class FPerfEvidenceBusyManager : public FTickableGameObject
{
public:
	virtual void Tick(float) override
	{
		const int32 Want = FMath::Clamp(CVarPerfBusyCount.GetValueOnGameThread(), 0, 256);
		if (Want == Burners.Num())
		{
			return;
		}
		Burners.Reset();
		for (int32 i = 0; i < Want; ++i)
		{
			Burners.Add(MakeUnique<FPerfEvidenceBusyTicker>());
		}
		UE_LOGF(LogTemp, Display, "PerfEvidence: regression burners = %d x %.2f ms",
			Want, CVarPerfBusyMs.GetValueOnGameThread());
	}
	virtual TStatId GetStatId() const override
	{
		RETURN_QUICK_DECLARE_CYCLE_STAT(FPerfEvidenceBusyManager, STATGROUP_Tickables);
	}
private:
	TArray<TUniquePtr<FPerfEvidenceBusyTicker>> Burners;
};

// ---------------------------------------------------------------- M5.1b 每帧采样器
// 每帧读引擎自己的计数（RenderTimer.h 的 uint32 cycle 计数器 + RHIGetGPUFrameCycles），
// 落成 CSV 供 digest bound 与 trace 推导的 busy/wait 对照。cvar PerfEvidence.Sampler=1 打开。
// 只在有真实 RHI 的视口渲染时这些计数才会更新（nullrhi 不算），所以采样行数≈帧数。
#if !UE_BUILD_SHIPPING
#include "RenderTimer.h"
#include "DynamicRHI.h"
#include "HAL/FileManager.h"
#include "Misc/App.h"
#include "Misc/Paths.h"

static TAutoConsoleVariable<int32> CVarPerfSampler(
	TEXT("PerfEvidence.Sampler"), 0,
	TEXT("M5.1b 每帧采样：写 Saved/Profiling/Sampler/perf_sampler_<pid>.csv（0=关闭）"),
	ECVF_Cheat);

class FPerfEvidenceSampler : public FTickableGameObject
{
public:
	FPerfEvidenceSampler()
	{
		const FString Dir = FPaths::ProjectSavedDir() / TEXT("Profiling/Sampler");
		IFileManager::Get().MakeDirectory(*Dir, true);
		Path = Dir / FString::Printf(TEXT("perf_sampler_%d.csv"), FPlatformProcess::GetCurrentProcessId());
		// 先写表头，后续追加
		FFileHelper::SaveStringToFile(TEXT("frame,game_ms,render_ms,game_wait_ms,render_wait_ms,gpu_ms\n"), *Path);
	}
	~FPerfEvidenceSampler()
	{
		if (!Buffered.IsEmpty()) { Flush(); }
	}
	virtual void Tick(float) override
	{
		if (CVarPerfSampler.GetValueOnGameThread() == 0) { return; }
		const uint32 Game = GGameThreadTime;
		const uint32 Render = GRenderThreadTime;
		const uint32 GameWait = GGameThreadWaitTime;
		const uint32 RenderWait = GRenderThreadWaitTime;
		const uint32 Gpu = RHIGetGPUFrameCycles(0);
		// 计数每帧在 FViewport::Draw 里更新；没渲染的帧会保持旧值，用"和上一帧完全相同"来跳过
		if (Game == LastGame && Render == LastRender && Gpu == LastGpu) { return; }
		LastGame = Game; LastRender = Render; LastGpu = Gpu;
		Buffered += FString::Printf(TEXT("%d,%.3f,%.3f,%.3f,%.3f,%.3f\n"),
			Frames,
			FPlatformTime::ToMilliseconds(Game), FPlatformTime::ToMilliseconds(Render),
			FPlatformTime::ToMilliseconds(GameWait), FPlatformTime::ToMilliseconds(RenderWait),
			FPlatformTime::ToMilliseconds(Gpu));
		++Frames;
		if (Frames % 600 == 0) { Flush(); }
	}
	virtual TStatId GetStatId() const override
	{
		RETURN_QUICK_DECLARE_CYCLE_STAT(FPerfEvidenceSampler, STATGROUP_Tickables);
	}
	void Flush()
	{
		FFileHelper::SaveStringToFile(Buffered, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM,
			&IFileManager::Get(), FILEWRITE_Append);
		Buffered.Reset();
	}
	FString Path;
	FString Buffered;
	uint32 LastGame = 0, LastRender = 0, LastGpu = 0;
	int32 Frames = 0;
};

static struct FPerfEvidenceSamplerInit
{
	FPerfEvidenceSamplerInit()
	{
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceSamplerBoot"), 0.0f, [](float)
		{
			new FPerfEvidenceSampler();
			UE_LOGF(LogTemp, Display, "PerfEvidence: sampler up (cvar PerfEvidence.Sampler)");
			return false;
		});
	}
} GPerfEvidenceSamplerInit;
#endif // !UE_BUILD_SHIPPING

// ---------------------------------------------------------------- CSV 事件对

// PerfEvidence.ExecAt=<秒>:<命令> —— 到点把命令排进 GEngine->DeferredCommands（与 -ExecCmds 同路径）。
// 宿主模块 TraceDigestProbe 里一直有；插件这边是 M6.3 才发现缺的（Lyra 的 open 命令因此静默没执行）。
// 从命令行取 PerfEvidence.ExecAt 的值。
// 坑：引擎会把带空格的 token 整体重新加引号 —— 命令行里看到的是
//   "PerfEvidence.ExecAt=12:stat none"（引号在键前、闭合引号在整段末尾），
// 所以不能靠 FParse::Value（它只认"键前是空白"），也不能简单剥引号（值会在空格处截断）。
// 这里直接定位键，再按"是否被引号包裹"决定读到哪个字符。
static FString PerfEvidenceReadExecAt(const TCHAR* InKey)
{
	FString OutValue;
	const FString Line = FCommandLine::Get();
	const FString Key = InKey;
	const int32 KeyAt = Line.Find(Key, ESearchCase::IgnoreCase, ESearchDir::FromStart);
	if (KeyAt == INDEX_NONE)
	{
		return FString();
	}
	int32 Start = KeyAt + Key.Len();
	if (Start >= Line.Len())
	{
		return FString();
	}
	if (Line[Start] == TEXT('"'))
	{
		const int32 End = Line.Find(TEXT("\""), ESearchCase::IgnoreCase, ESearchDir::FromStart, Start + 1);
		if (End == INDEX_NONE) { return FString(); }
		return Line.Mid(Start + 1, End - Start - 1);
	}
	if (KeyAt > 0 && Line[KeyAt - 1] == TEXT('"'))
	{
		const int32 End = Line.Find(TEXT("\""), ESearchCase::IgnoreCase, ESearchDir::FromStart, Start);
		if (End == INDEX_NONE)
		{
			return Line.Mid(Start);
		}
		return Line.Mid(Start, End - Start);
	}
	int32 End = Start;
	while (End < Line.Len() && !FChar::IsWhitespace(Line[End])) { ++End; }
	return Line.Mid(Start, End - Start);
}

static void PerfEvidenceScheduleExecAt()
{
	auto ScheduleOne = [](const FString& Spec, const TCHAR* Slot)
	{
		int32 Sep = INDEX_NONE;
		if (Spec.IsEmpty() || !Spec.FindChar(TEXT(':'), Sep))
		{
			return;
		}
		const double DelaySeconds = FCString::Atod(*Spec.Left(Sep));
		const FString Command = Spec.Mid(Sep + 1).TrimStartAndEnd();
		if (DelaySeconds <= 0.0 || Command.IsEmpty())
		{
			return;
		}
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceExecAt"), float(DelaySeconds),
			[Command](float)
			{
				UE_LOGF(LogTemp, Display, "PerfEvidence: queueing exec [%ls]", *Command);
				if (GEngine)
				{
					GEngine->DeferredCommands.Add(Command);
				}
				return false; // one-shot
			});
		UE_LOGF(LogTemp, Display, "PerfEvidence: ExecAt%ls scheduled at %.1fs [%ls]", Slot,
			DelaySeconds, *Command);
	};
	// 两个槽：常规注入用 ExecAt，需要"开图 + 退出"这类成对动作时用 ExecAt2（M6.3 编辑器验证）
	ScheduleOne(PerfEvidenceReadExecAt(TEXT("PerfEvidence.ExecAt=")), TEXT(""));
	ScheduleOne(PerfEvidenceReadExecAt(TEXT("PerfEvidence.ExecAt2=")), TEXT("2"));
}

#if CSV_PROFILER
static void PerfEvidenceMakeEventTicker(double DelaySeconds, const TCHAR* EventText)
{
	FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceEvent"), float(DelaySeconds),
		[EventText](float)
		{
			UE_LOGF(LogTemp, Display, "PerfEvidence: firing %ls", EventText);
			CSV_EVENT_GLOBAL(TEXT("%ls"), EventText);
			return false; // one-shot
		});
}
#endif

static struct FPerfEvidenceHostInit
{
	FPerfEvidenceHostInit()
	{
		// 引擎起来之后再建 tickable：静态初始化阶段只挂一次性 ticker（P17）。
		PerfEvidenceScheduleExecAt();
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceBusyBoot"), 0.0f,
			[](float)
			{
				new FPerfEvidenceBusyManager(); // 活到进程结束
				UE_LOGF(LogTemp, Display,
					"PerfEvidence: busy manager up (cvar PerfEvidence.BusyCount / BusyMs)");
				return false;
			});
#if CSV_PROFILER
		double CoverageStart = 0.0;
		FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.CoverageStart="), CoverageStart);
		double CaptureStop = 0.0;
		FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.CaptureStop="), CaptureStop);
		if (CoverageStart > 0.0)
		{
			PerfEvidenceMakeEventTicker(CoverageStart, TEXT("PerfEvidence CoverageStart"));
		}
		if (CaptureStop > 0.0)
		{
			PerfEvidenceMakeEventTicker(CaptureStop, TEXT("PerfEvidence CaptureStop"));
		}
#endif
	}
} GPerfEvidenceHostInit;
#endif // !UE_BUILD_SHIPPING
