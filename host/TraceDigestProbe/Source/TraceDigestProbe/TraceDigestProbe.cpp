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
#if !UE_BUILD_SHIPPING
#include "SocketSubsystem.h"
#include "Sockets.h"
#include "Common/TcpListener.h"
#include "Common/TcpSocketBuilder.h"
#include "HAL/PlatformTime.h"
#include "ProfilingDebugging/CpuProfilerTrace.h"
#include "Tickable.h"
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

// PerfEvidence 控制通道（M3.0 常驻会话）：本机 TCP，一行一命令，回复后关连接。
//   ping            -> pong
//   exec <命令...>  -> 排进 GEngine->DeferredCommands（与 -ExecCmds 同路径）-> queued
//   quit            -> RequestExit(false) 干净退出 -> quitting
// 启动参数 PerfEvidence.ControlPort=<端口> 时开启；只绑 127.0.0.1。
#if !UE_BUILD_SHIPPING
#include "HAL/PlatformTime.h"
static FSocket* GControlClient = nullptr;
static FTcpListener* GControlListener = nullptr;
static TArray<uint8> GControlRecv;
static TQueue<FSocket*, EQueueMode::Mpsc> GControlPending;
static bool GControlStarted = false;

static void PerfEvidenceHandleLine(const FString& Line, FSocket* Client)
{
	FString Reply;
	FString Trimmed = Line.TrimStartAndEnd();
	if (Trimmed == TEXT("ping"))
	{
		Reply = TEXT("pong");
	}
	else if (Trimmed.StartsWith(TEXT("exec ")))
	{
		FString Cmd = Trimmed.Mid(5).TrimStartAndEnd();
		if (GEngine && !Cmd.IsEmpty())
		{
			GEngine->DeferredCommands.Add(Cmd);
			UE_LOGF(LogTemp, Display, "PerfEvidence: control exec [%ls]", *Cmd);
			Reply = TEXT("queued");
		}
		else
		{
			Reply = TEXT("err no engine");
		}
	}
	else if (Trimmed == TEXT("quit"))
	{
		FPlatformMisc::RequestExit(false, TEXT("PerfEvidence.Control"));
		Reply = TEXT("quitting");
	}
	else
	{
		Reply = TEXT("err unknown");
	}
	FString ReplyLine = Reply + TEXT("\n");
	FTCHARToUTF8 Utf8(*ReplyLine);
	int32 Sent = 0;
	Client->Send(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length(), Sent);
}

static bool PerfEvidenceControlAccepted(FSocket* Client, const FIPv4Endpoint& /*Peer*/)
{
	GControlPending.Enqueue(Client);
	return true; // 已接管该连接的所有权
}

static bool PerfEvidenceControlTick(float)
{
	if (!GControlStarted)
	{
		return true;
	}
	ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
	FSocket* NewClient = nullptr;
	while (GControlPending.Dequeue(NewClient))
	{
		if (GControlClient)
		{
			SS->DestroySocket(GControlClient); // 单客户端：新连接顶掉旧的
		}
		GControlClient = NewClient;
		GControlRecv.Reset();
	}
	if (!GControlClient)
	{
		return true;
	}
	uint32 Pending = 0;
	while (GControlClient && GControlClient->HasPendingData(Pending) && Pending > 0)
	{
		uint8 Buf[512] = {};
		int32 Read = 0;
		if (!GControlClient->Recv(Buf, FMath::Min<uint32>(Pending, sizeof(Buf)), Read) || Read <= 0)
		{
			break;
		}
		GControlRecv.Append(Buf, Read);
	}
	int32 LineEnd = INDEX_NONE;
	while ((LineEnd = GControlRecv.Find((uint8)'\n')) != INDEX_NONE)
	{
		FUTF8ToTCHAR Converter(reinterpret_cast<const ANSICHAR*>(GControlRecv.GetData()), LineEnd);
		FString Line(Converter.Length(), Converter.Get());
		PerfEvidenceHandleLine(Line, GControlClient);
		TArray<uint8> Rest;
		Rest.Append(GControlRecv.GetData() + LineEnd + 1, GControlRecv.Num() - (LineEnd + 1));
		GControlRecv = MoveTemp(Rest);
		// 一连接一命令：回复已发，关闭
		SS->DestroySocket(GControlClient);
		GControlClient = nullptr;
		GControlRecv.Reset();
		break;
	}
	return true;
}

static void PerfEvidenceStartControl()
{
	if (GControlStarted)
	{
		return;
	}
	int32 Port = 0;
	if (!FParse::Value(FCommandLine::Get(), TEXT("PerfEvidence.ControlPort="), Port) || Port <= 0)
	{
		return;
	}
	GControlStarted = true;
	ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
	FIPv4Endpoint Endpoint(FIPv4Address(127, 0, 0, 1), Port);
	GControlListener = new FTcpListener(Endpoint); // 构造即自建线程开始监听
	GControlListener->OnConnectionAccepted().BindStatic(&PerfEvidenceControlAccepted);
	FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceControl"), 0.0f, &PerfEvidenceControlTick);
	UE_LOGF(LogTemp, Display, "PerfEvidence: control listening on 127.0.0.1:%d", Port);
}

// 一次性 boot ticker：监听必须在引擎起来之后才建。
// M3.0 卡死根因：之前在静态初始化（DllMain，持加载器锁）里直接 ISocketSubsystem::Get()
// + new FTcpListener（其内部再起监听线程），两处都要 LoadModule/加载 DLL，
// 与本模块的加载器锁互等 → 编辑器永远停在 "InternalLoadLibrary: TraceDigestProbe" 之后，
// 主线程 WaitReason=UserRequest，日志再无一行（1800s 预算也等不到 Engine is initialized）。
static bool PerfEvidenceControlBootTick(float)
{
	PerfEvidenceStartControl();
	return false; // one-shot
}

static struct FPerfEvidenceControlInit
{
	FPerfEvidenceControlInit()
	{
		// 静态初始化阶段只挂 ticker（纯内存，无模块加载），不碰 socket、不建线程。
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceControlBoot"), 0.0f,
			&PerfEvidenceControlBootTick);
	}
} GPerfEvidenceControlInit;
#endif // !UE_BUILD_SHIPPING

// ---- M3.4 闭环演示的已知回归注入 ----
// 两个 cvar 决定“每帧多花多少 GameThread 时间”：BusyCount 个 tickable，每个空转 BusyMs 毫秒。
// 注入与撤销都只是改 cvar：不改游戏代码、不重启进程，A/B/C 三组的启动参数除这两条外完全一致。
// 忙等 span 的名字是 PerfEvidenceRegress，所以 diff 的 Top 自耗时表能把它直接认出来。
// 纪律（P17）：cvar 注册是纯数据；tickable 在第 0 帧之后由一次性 ticker 建。
#if !UE_BUILD_SHIPPING
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

static struct FPerfEvidenceBusyInit
{
	FPerfEvidenceBusyInit()
	{
		FTSTicker::GetCoreTicker().AddTicker(TEXT("PerfEvidenceBusyBoot"), 0.0f,
			[](float)
			{
				new FPerfEvidenceBusyManager(); // 活到进程结束：引擎要它一直 tick
				UE_LOGF(LogTemp, Display,
					"PerfEvidence: busy manager up (cvar PerfEvidence.BusyCount / BusyMs)");
				return false; // one-shot
			});
	}
} GPerfEvidenceBusyInit;
#endif // !UE_BUILD_SHIPPING

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
