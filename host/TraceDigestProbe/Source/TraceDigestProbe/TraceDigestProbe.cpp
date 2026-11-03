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
#include "TraceServices/Model/Frames.h"
#include "TraceServices/Model/Threads.h"
#include "TraceServices/Containers/Timelines.h"
#include "Misc/FileHelper.h"
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
				// 5.8：ReadTimers 弃用 → GetTimerReader()
				TimerCount = (int32)Timers->GetTimerReader().GetTimerCount();
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

// 参数取值：既认 key=value，也认 key value。
// 实测：命令行经引擎转发后 "mode=frames" 会变成 "mode frames"（等号被吃掉），
// 所以只按 FParse::Value(Key+"=") 解析会静默拿到空串 —— 这里两种形态都接受。
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

static FString ParseUnquoted(const FString& Params, const TCHAR* Key)
{
	// FParse::Value 要求匹配键前面是空白；bat/引擎转发会把 token 重排成 "prefix=X"（引号在键前），
	// 所以先剥掉全部引号再解析。这里的值（路径、事件文本）本身不含引号。
	FString Clean = Params.Replace(TEXT("\""), TEXT(""));
	FString Value;
	FParse::Value(*Clean, Key, Value);
	return Value;
}

// ---------------------------------------------------------------- M4.2 深度读取
// 移植自 ue-trace-mcp（Unlicense，bf73c60）的 frames / timers 两个 mode，改成 5.8 API：
// timers 走 GetTimerReader()（ReadTimers 在 5.8 已弃用，见 docs/M4_1_API_DIFF.md A1）。
// 输出是手写 JSON（不引 Json 模块，保持依赖最少），写 -out=<file>；不写就打到日志。

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

static void EmitFrames(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, int32 Limit)
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

	if (!OutPath.IsEmpty())
	{
		if (FFileHelper::SaveStringToFile(Out, *OutPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
		{
			Ar.Logf(TEXT("PERF_OUT %s"), *OutPath);
		}
		else
		{
			Ar.Logf(TEXT("PERF_ERR write failed: %s"), *OutPath);
		}
	}
	Ar.Logf(TEXT("PERF_FRAMES game=%llu render=%llu duration_ms=%.4f"),
		(unsigned long long)GameCount, (unsigned long long)RenderCount, EndSec * 1000.0);
}

static void EmitTimers(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, int32 Limit)
{
	// 5.8：ReadTimingProfilerProvider 返回指针（ReadFrameProvider 才是引用）；ReadTimers 已弃用，走 GetTimerReader()
	const TraceServices::ITimingProfilerProvider* Timing = TraceServices::ReadTimingProfilerProvider(Session);
	if (!Timing) { Ar.Logf(TEXT("PERF_ERR no timing profiler provider")); return; }
	const TraceServices::ITimingProfilerTimerReader& Reader = Timing->GetTimerReader();
	const uint32 TimerCount = Reader.GetTimerCount();

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-timers/0\",\"trace\":\"%s\",\"timer_count\":%u,\"timers\":["),
		*JsonEscape(*TracePath), TimerCount);
	uint32 Emitted = 0;
	for (uint32 I = 0; I < TimerCount; ++I)
	{
		if (Limit > 0 && Emitted >= (uint32)Limit) { break; }
		const TraceServices::FTimingProfilerTimer* Timer = Reader.GetTimer(I);
		if (!Timer) { continue; }
		if (Emitted > 0) { Out += TEXT(","); }
		Out += FString::Printf(TEXT("{\"id\":%u,\"name\":\"%s\",\"file\":\"%s\",\"line\":%u,\"type\":%u}"),
			Timer->Id, *JsonEscape(Timer->Name), *JsonEscape(Timer->File), Timer->Line,
			(uint32)Timer->Type);
		++Emitted;
	}
	Out += FString::Printf(TEXT("],\"emitted\":%u}"), Emitted);

	if (!OutPath.IsEmpty())
	{
		if (FFileHelper::SaveStringToFile(Out, *OutPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
		{
			Ar.Logf(TEXT("PERF_OUT %s"), *OutPath);
		}
		else
		{
			Ar.Logf(TEXT("PERF_ERR write failed: %s"), *OutPath);
		}
	}
	Ar.Logf(TEXT("PERF_TIMERS count=%u emitted=%u"), TimerCount, Emitted);
}

// timerstats：按时间窗口聚合每个 CPU timer 的 calls / self / inclusive（M4.2 第 2 步）。
// 自耗时用"深度栈"算：同一 timeline 的事件按开始时间前序到达，且每个回调自带 [Begin, End]，
// 所以栈里存住自己的 End；子事件收尾时把整段时长加回父事件的 ChildSum，
// 父事件收尾时 self = 整段 - ChildSum。线程过滤：先在线程表里按名字找 ThreadId，
// 再用 GetCpuThreadTimelineIndex 定位 timeline。
static void EmitTimerStats(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd,
	const FString& ThreadName, int32 Limit)
{
	const TraceServices::ITimingProfilerProvider* Timing = TraceServices::ReadTimingProfilerProvider(Session);
	if (!Timing) { Ar.Logf(TEXT("PERF_ERR no timing profiler provider")); return; }
	const TraceServices::ITimingProfilerTimerReader& Reader = Timing->GetTimerReader();

	if (WindowEnd <= 0.0)
	{
		WindowEnd = Session.GetDurationSeconds();
	}
	const double Span = FMath::Max(0.0, WindowEnd - WindowStart);

	struct FAccum
	{
		int64 Calls = 0;
		double Inclusive = 0.0;
		double Self = 0.0;
	};
	TMap<uint32, FAccum> Acc; // key = TimerIndex（GetTimer(i) 的索引域）
	Acc.Reserve(8192);

	auto AggregateTimeline = [&Acc](const TraceServices::ITimingProfilerProvider::Timeline& Timeline,
		double Start, double End)
	{
		struct FOpen
		{
			uint32 TimerIndex = 0;
			double Begin = 0.0;
			double End = 0.0;
			double ChildSum = 0.0;
		};
		TArray<FOpen> Open;
		Open.Reserve(64);

		auto CloseTop = [&Acc, &Open]()
		{
			if (Open.Num() == 0)
			{
				return;
			}
			const FOpen Top = Open.Pop();
			const double Duration = FMath::Max(0.0, Top.End - Top.Begin);
			FAccum& Slot = Acc.FindOrAdd(Top.TimerIndex);
			Slot.Inclusive += Duration;
			Slot.Self += FMath::Max(0.0, Duration - Top.ChildSum);
			if (Open.Num() > 0)
			{
				Open.Last().ChildSum += Duration;
			}
		};

		Timeline.EnumerateEvents(Start, End,
			[&Acc, &Open, &CloseTop](double EventStart, double EventEnd, uint32 Depth,
				const TraceServices::FTimingProfilerEvent& Event) -> TraceServices::EEventEnumerate
			{
				// 前序到达：深度 >= 当前深度的已开事件都已完整，先收尾
				while (Open.Num() > (int32)Depth)
				{
					CloseTop();
				}
				FOpen New;
				New.TimerIndex = Event.TimerIndex;
				New.Begin = EventStart;
				New.End = EventEnd;
				Open.Push(New);
				Acc.FindOrAdd(Event.TimerIndex).Calls += 1;
				return TraceServices::EEventEnumerate::Continue;
			});

		while (Open.Num() > 0)
		{
			CloseTop();
		}
	};

	// 定位 timeline：给了 -thread 就必须找到；找不到直接报错，不静默降级
	FString UsedThread = ThreadName;
	uint32 UsedThreadId = 0;
	if (!ThreadName.IsEmpty())
	{
		const TraceServices::IThreadProvider& Threads = TraceServices::ReadThreadProvider(Session);
		Threads.EnumerateThreads([&](const TraceServices::FThreadInfo& Info)
		{
			if (ThreadName.Equals(FString(Info.Name), ESearchCase::IgnoreCase))
			{
				UsedThreadId = Info.Id;
			}
		});
		if (UsedThreadId == 0)
		{
			Ar.Logf(TEXT("PERF_ERR thread not found: %s"), *ThreadName);
			return;
		}
		uint32 TimelineIndex = 0;
		if (!Timing->GetCpuThreadTimelineIndex(UsedThreadId, TimelineIndex))
		{
			Ar.Logf(TEXT("PERF_ERR no cpu timeline for thread %s (id=%u)"), *ThreadName, UsedThreadId);
			return;
		}
		Timing->ReadTimeline(TimelineIndex, [&](const TraceServices::ITimingProfilerProvider::Timeline& TL)
		{
			AggregateTimeline(TL, WindowStart, WindowEnd);
		});
	}
	else
	{
		Ar.Logf(TEXT("PERF_ERR -thread=<名字> 必填（不给会把 GPU timeline 混进 CPU 聚合）"));
		return;
	}

	// 汇总
	double SumSelf = 0.0;
	double SumInclusive = 0.0;
	for (const TPair<uint32, FAccum>& Pair : Acc)
	{
		SumSelf += Pair.Value.Self;
		SumInclusive += Pair.Value.Inclusive;
	}

	TArray<TPair<uint32, FAccum>> Rows;
	Rows.Reserve(Acc.Num());
	for (const TPair<uint32, FAccum>& Pair : Acc)
	{
		Rows.Add(Pair);
	}
	Rows.Sort([](const TPair<uint32, FAccum>& A, const TPair<uint32, FAccum>& B)
	{
		return A.Value.Self > B.Value.Self;
	});

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-timerstats/0\",\"trace\":\"%s\",")
		TEXT("\"window\":[%.6f,%.6f],\"span_s\":%.6f,\"thread\":\"%s\",\"thread_id\":%u,")
		TEXT("\"totals\":{\"sum_self_s\":%.6f,\"sum_inclusive_s\":%.6f,\"timer_count\":%d},")
		TEXT("\"timers\":["),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, Span, *JsonEscape(*UsedThread), UsedThreadId,
		SumSelf, SumInclusive, Acc.Num());

	int32 Emitted = 0;
	for (const TPair<uint32, FAccum>& Pair : Rows)
	{
		if (Limit > 0 && Emitted >= Limit)
		{
			break;
		}
		const TraceServices::FTimingProfilerTimer* Timer = Reader.GetTimer(Pair.Key);
		const TCHAR* Name = (Timer && Timer->Name) ? Timer->Name : TEXT("<unknown>");
		const uint32 TimerId = Timer ? Timer->Id : 0u;
		if (Emitted > 0) { Out += TEXT(","); }
		Out += FString::Printf(
			TEXT("{\"index\":%u,\"id\":%u,\"name\":\"%s\",\"calls\":%lld,")
			TEXT("\"self_s\":%.6f,\"inclusive_s\":%.6f,\"self_pct_window\":%.4f,\"self_pct_sum\":%.4f}"),
			Pair.Key, TimerId, *JsonEscape(Name), Pair.Value.Calls,
			Pair.Value.Self, Pair.Value.Inclusive,
			Span > 0.0 ? 100.0 * Pair.Value.Self / Span : 0.0,
			SumSelf > 0.0 ? 100.0 * Pair.Value.Self / SumSelf : 0.0);
		++Emitted;
	}
	Out += FString::Printf(TEXT("],\"emitted\":%d}"), Emitted);

	if (!OutPath.IsEmpty())
	{
		if (FFileHelper::SaveStringToFile(Out, *OutPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
		{
			Ar.Logf(TEXT("PERF_OUT %s"), *OutPath);
		}
		else
		{
			Ar.Logf(TEXT("PERF_ERR write failed: %s"), *OutPath);
		}
	}
	Ar.Logf(TEXT("PERF_TIMERSTATS thread=%s timers=%d emitted=%d sum_self=%.3fs span=%.3fs"),
		*UsedThread, Acc.Num(), Emitted, SumSelf, Span);
}

// TraceDigestPerf 的实现（-run=TraceDigestPerf 经 UTraceDigestPerfCommandlet 进来）。
//   mode 为空（默认）：输出 trace 里以 prefix 开头的书签（M2.2 的 coverage 边界，capture.py 在用）
//   mode=frames      ：帧轨道（Game / Rendering 各一条，index/start/end/duration）
//   mode=timers      ：timer 目录（id/name/file/line/type），5.8 的 GetTimerReader 路径
// -out=<file> 写 JSON；-limit=N 限制每条输出的条目数（0 = 全量）。
void RunTraceDigestPerf(const FString& Params, FOutputDevice& Ar)
{
	FString TracePath = ParseArg(Params, TEXT("trace"));
	FString Prefix = ParseArg(Params, TEXT("prefix"));
	FString Mode = ParseArg(Params, TEXT("mode"));
	FString OutPath = ParseArg(Params, TEXT("out"));
	int32 Limit = 0;
	{
		const FString LimitText = ParseArg(Params, TEXT("limit"));
		if (!LimitText.IsEmpty())
		{
			Limit = FCString::Atoi(*LimitText);
		}
	}
	double WindowStart = 0.0;
	{
		const FString Text = ParseArg(Params, TEXT("start"));
		if (!Text.IsEmpty()) { WindowStart = FCString::Atod(*Text); }
	}
	double WindowEnd = 0.0;
	{
		const FString Text = ParseArg(Params, TEXT("end"));
		if (!Text.IsEmpty()) { WindowEnd = FCString::Atod(*Text); }
	}
	const FString ThreadName = ParseArg(Params, TEXT("thread"));
	if (TracePath.IsEmpty()) { Ar.Logf(TEXT("PERF_ERR missing trace=<file>")); return; }

	ITraceServicesModule& Module = FModuleManager::LoadModuleChecked<ITraceServicesModule>(TEXT("TraceServices"));
	TSharedPtr<TraceServices::IAnalysisService> Service = Module.GetAnalysisService();
	if (!Service.IsValid()) { Service = Module.CreateAnalysisService(); }
	if (!Service.IsValid()) { Ar.Logf(TEXT("PERF_ERR no analysis service")); return; }

	TSharedPtr<const TraceServices::IAnalysisSession> Session = Service->Analyze(*TracePath);
	if (!Session.IsValid()) { Ar.Logf(TEXT("PERF_ERR analyze failed")); return; }
	Session->Wait();
	TraceServices::FAnalysisSessionReadScope ReadScope(*Session);

	if (Mode == TEXT("frames"))
	{
		EmitFrames(*Session, Ar, TracePath, OutPath, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("timers"))
	{
		EmitTimers(*Session, Ar, TracePath, OutPath, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("timerstats"))
	{
		EmitTimerStats(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, ThreadName, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}

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
