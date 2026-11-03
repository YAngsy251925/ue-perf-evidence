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

// ---------------------------------------------------------------- CSV 事件对

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
