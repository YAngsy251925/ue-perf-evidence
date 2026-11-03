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
#include "TraceServices/Model/Counters.h"
#include "TraceServices/Model/Regions.h"
#include "TraceServices/Model/Log.h"
#include "TraceServices/Model/Channel.h"
#include "TraceServices/Model/Memory.h"
#include "TraceServices/Model/TasksProfiler.h"
#include "TraceServices/Model/AllocationsProvider.h"
#include "TraceServices/Model/NetProfiler.h"
#include "TraceServices/Model/LoadTimeProfiler.h"
#include "TraceServices/Containers/Tables.h"
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
			// R13：+inf 之类的非有限值必须丢弃，否则 JSON 非法
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

// ---------------------------------------------------------------- M4.2 其余 provider（v0 摘要）
// 目标不是把 ue-trace-mcp 的每个视图都复刻到位，而是让 B2 在 5.8 上把每一类通道都读出来、
// 与 A 路 / 日志侧能对账，给 M4.3 的 digest 接入提供数据面。每个 mode 都输出"计数 + Top-N 行"。

static bool WriteJson(FOutputDevice& Ar, const FString& OutPath, const FString& Json)
{
	if (OutPath.IsEmpty())
	{
		return true;
	}
	if (FFileHelper::SaveStringToFile(Json, *OutPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		Ar.Logf(TEXT("PERF_OUT %s"), *OutPath);
		return true;
	}
	Ar.Logf(TEXT("PERF_ERR write failed: %s"), *OutPath);
	return false;
}

// 在一组 timeline 上聚合每个 timer 的 self/inclusive（供 timerstats / gpu 共用）
struct FTimerAccum
{
	int64 Calls = 0;
	double Inclusive = 0.0;
	double Self = 0.0;
};

static void AggregateTimelineEvents(const TraceServices::ITimingProfilerProvider::Timeline& Timeline,
	double Start, double End, TMap<uint32, FTimerAccum>& Acc)
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
		FTimerAccum& Slot = Acc.FindOrAdd(Top.TimerIndex);
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
			while (Open.Num() > (int32)Depth)
			{
				CloseTop();
			}
			// R13：非有限时长不入栈（否则 self 会变成 inf）
			if (!FMath::IsFinite(EventStart) || !FMath::IsFinite(EventEnd))
			{
				return TraceServices::EEventEnumerate::Continue;
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
}

// 找到某个线程的 CPU timeline；找到返回 true
static bool ResolveThreadTimeline(const TraceServices::IAnalysisSession& Session,
	const TraceServices::ITimingProfilerProvider& Timing, const FString& ThreadName,
	uint32& OutThreadId, uint32& OutTimelineIndex)
{
	OutThreadId = 0;
	OutTimelineIndex = 0;
	const TraceServices::IThreadProvider& Threads = TraceServices::ReadThreadProvider(Session);
	Threads.EnumerateThreads([&](const TraceServices::FThreadInfo& Info)
	{
		if (ThreadName.Equals(FString(Info.Name), ESearchCase::IgnoreCase))
		{
			OutThreadId = Info.Id;
		}
	});
	return OutThreadId != 0 && Timing.GetCpuThreadTimelineIndex(OutThreadId, OutTimelineIndex);
}

// callers / callees：对指定 timer 出调用方与被调用方（按次数排序）
static void EmitCallers(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd,
	const FString& ThreadName, const FString& TimerName, int32 Limit)
{
	const TraceServices::ITimingProfilerProvider* Timing = TraceServices::ReadTimingProfilerProvider(Session);
	if (!Timing) { Ar.Logf(TEXT("PERF_ERR no timing profiler provider")); return; }
	const TraceServices::ITimingProfilerTimerReader& Reader = Timing->GetTimerReader();
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	uint32 ThreadId = 0, TimelineIndex = 0;
	if (!ResolveThreadTimeline(Session, *Timing, ThreadName, ThreadId, TimelineIndex))
	{
		Ar.Logf(TEXT("PERF_ERR no cpu timeline for thread %s"), *ThreadName);
		return;
	}

	// 目标 timer：按名字找（同名多 ID 时全收）
	TSet<uint32> Targets;
	for (uint32 I = 0; I < Reader.GetTimerCount(); ++I)
	{
		const TraceServices::FTimingProfilerTimer* T = Reader.GetTimer(I);
		if (T && T->Name && TimerName.Equals(FString(T->Name)) &&
			T->Type == TraceServices::ETimingProfilerTimerType::CpuScope)
		{
			Targets.Add(I);
		}
	}
	if (Targets.Num() == 0)
	{
		Ar.Logf(TEXT("PERF_ERR timer not found: %s"), *TimerName);
		return;
	}

	struct FEdge { int64 Calls = 0; double ChildInclusive = 0.0; };
	TMap<uint64, FEdge> Edges;   // key = (Parent << 32) | Child
	Edges.Reserve(16384);

	Timing->ReadTimeline(TimelineIndex,
		[&Edges, WindowStart, WindowEnd](const TraceServices::ITimingProfilerProvider::Timeline& TL)
		{
			struct FOpen { uint32 TimerIndex; double Begin; double End; double ChildSum; };
			TArray<FOpen> Open;
			Open.Reserve(64);
			auto CloseTop = [&Edges, &Open]()
			{
				if (Open.Num() == 0) { return; }
				const FOpen Top = Open.Pop();
				if (Open.Num() > 0)
				{
					const uint32 Parent = Open.Last().TimerIndex;
					FEdge& E = Edges.FindOrAdd((uint64(Parent) << 32) | uint64(Top.TimerIndex));
					E.Calls += 1;
					E.ChildInclusive += FMath::Max(0.0, Top.End - Top.Begin);
				}
			};
			TL.EnumerateEvents(WindowStart, WindowEnd,
				[&Open, &CloseTop](double S, double E, uint32 Depth,
					const TraceServices::FTimingProfilerEvent& Ev) -> TraceServices::EEventEnumerate
				{
					while (Open.Num() > (int32)Depth) { CloseTop(); }
					FOpen New; New.TimerIndex = Ev.TimerIndex; New.Begin = S; New.End = E;
					Open.Push(New);
					return TraceServices::EEventEnumerate::Continue;
				});
			while (Open.Num() > 0) { CloseTop(); }
		});

	auto NameOf = [&Reader](uint32 Index) -> FString
	{
		const TraceServices::FTimingProfilerTimer* T = Reader.GetTimer(Index);
		return (T && T->Name) ? FString(T->Name) : FString(TEXT("<unknown>"));
	};

	struct FRow { FString Name; int64 Calls; double Inclusive; };
	TArray<FRow> Callers, Callees;
	for (const TPair<uint64, FEdge>& Pair : Edges)
	{
		const uint32 Parent = (uint32)(Pair.Key >> 32);
		const uint32 Child = (uint32)(Pair.Key & 0xffffffff);
		if (Targets.Contains(Child))
		{
			Callers.Add({ NameOf(Parent), Pair.Value.Calls, Pair.Value.ChildInclusive });
		}
		if (Targets.Contains(Parent))
		{
			Callees.Add({ NameOf(Child), Pair.Value.Calls, Pair.Value.ChildInclusive });
		}
	}
	auto ByCalls = [](const FRow& A, const FRow& B) { return A.Calls > B.Calls; };
	Callers.Sort(ByCalls);
	Callees.Sort(ByCalls);

	auto EmitRows = [&](const TCHAR* Key, const TArray<FRow>& Rows) -> FString
	{
		FString Out = FString::Printf(TEXT("\"%s\":["), Key);
		int32 Emitted = 0;
		for (const FRow& R : Rows)
		{
			if (Limit > 0 && Emitted >= Limit) { break; }
			if (Emitted > 0) { Out += TEXT(","); }
			Out += FString::Printf(TEXT("{\"name\":\"%s\",\"calls\":%lld,\"inclusive_s\":%.6f}"),
				*JsonEscape(*R.Name), R.Calls, R.Inclusive);
			++Emitted;
		}
		Out += TEXT("]");
		return Out;
	};

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-callers/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"thread\":\"%s\",\"timer\":\"%s\",\"caller_count\":%d,\"callee_count\":%d,"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, *JsonEscape(*ThreadName),
		*JsonEscape(*TimerName), Callers.Num(), Callees.Num());
	Out += EmitRows(TEXT("callers"), Callers);
	Out += TEXT(",");
	Out += EmitRows(TEXT("callees"), Callees);
	Out += TEXT("}");
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_CALLERS timer=%s callers=%d callees=%d"), *TimerName, Callers.Num(), Callees.Num());
}

// gpu：GPU 队列清单 + GPU1 timeline 的每 timer 聚合
static void EmitGpu(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd, int32 Limit)
{
	const TraceServices::ITimingProfilerProvider* Timing = TraceServices::ReadTimingProfilerProvider(Session);
	if (!Timing) { Ar.Logf(TEXT("PERF_ERR no timing profiler provider")); return; }
	const TraceServices::ITimingProfilerTimerReader& Reader = Timing->GetTimerReader();
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	FString Queues = TEXT("[");
	int32 QueueCount = 0;
	Timing->EnumerateGpuQueues([&](const TraceServices::FGpuQueueInfo& Q)
	{
		if (QueueCount > 0) { Queues += TEXT(","); }
		Queues += FString::Printf(
			TEXT("{\"id\":%u,\"gpu\":%u,\"index\":%u,\"type\":%u,\"name\":\"%s\",\"timeline\":%u}"),
			Q.Id, (uint32)Q.GPU, (uint32)Q.Index, (uint32)Q.Type, *JsonEscape(Q.Name), Q.TimelineIndex);
		++QueueCount;
	});
	Queues += TEXT("]");

	TMap<uint32, FTimerAccum> Acc;
	uint32 GpuTimeline = 0;
	uint32 Gpu2Timeline = 0;
	const bool bHasGpu = Timing->GetGpuTimelineIndex(GpuTimeline);
	const bool bHasGpu2 = Timing->GetGpu2TimelineIndex(Gpu2Timeline);
	// 注意：GetGpuTimelineIndex 在 5.8 实测返回 0（那是 CPU timeline），不能直接用；
	// FGpuQueueInfo::TimelineIndex 才是 GPU 队列的时间线（本 trace 是 5/7/9）。
	int32 QueuesAggregated = 0;
	FString QueueBusy = TEXT("[");
	bool bFirstQueue = true;
	double GraphicsBusy = 0.0;
	double SumAllQueues = 0.0;
	Timing->EnumerateGpuQueues([&](const TraceServices::FGpuQueueInfo& Q)
	{
		if (Q.TimelineIndex == ~0u) { return; }
		TMap<uint32, FTimerAccum> QueueAcc;
		Timing->ReadTimeline(Q.TimelineIndex, [&QueueAcc, WindowStart, WindowEnd](
			const TraceServices::ITimingProfilerProvider::Timeline& TL)
		{
			AggregateTimelineEvents(TL, WindowStart, WindowEnd, QueueAcc);
		});
		double QueueSelf = 0.0;
		for (const TPair<uint32, FTimerAccum>& Pair : QueueAcc) { QueueSelf += Pair.Value.Self; }
		SumAllQueues += QueueSelf;
		if (bFirstQueue)
		{
			// 第一条 GPU 队列（本 trace 是 Graphics）作为 timer 表的来源
			GraphicsBusy = QueueSelf;
			for (const TPair<uint32, FTimerAccum>& Pair : QueueAcc) { Acc.Add(Pair.Key, Pair.Value); }
			bFirstQueue = false;
		}
		if (QueuesAggregated > 0) { QueueBusy += TEXT(","); }
		QueueBusy += FString::Printf(TEXT("{\"name\":\"%s\",\"timeline\":%u,\"busy_s\":%.6f}"),
			*JsonEscape(Q.Name), Q.TimelineIndex, QueueSelf);
		++QueuesAggregated;
	});
	QueueBusy += TEXT("]");

	double SumSelf = 0.0;
	for (const TPair<uint32, FTimerAccum>& Pair : Acc) { SumSelf += Pair.Value.Self; }
	TArray<TPair<uint32, FTimerAccum>> Rows;
	for (const TPair<uint32, FTimerAccum>& Pair : Acc) { Rows.Add(Pair); }
	Rows.Sort([](const TPair<uint32, FTimerAccum>& A, const TPair<uint32, FTimerAccum>& B)
	{
		return A.Value.Self > B.Value.Self;
	});

	FString Timers = TEXT("[");
	int32 Emitted = 0;
	for (const TPair<uint32, FTimerAccum>& Pair : Rows)
	{
		if (Limit > 0 && Emitted >= Limit) { break; }
		const TraceServices::FTimingProfilerTimer* T = Reader.GetTimer(Pair.Key);
		if (Emitted > 0) { Timers += TEXT(","); }
		Timers += FString::Printf(TEXT("{\"name\":\"%s\",\"calls\":%lld,\"self_s\":%.6f,\"inclusive_s\":%.6f}"),
			*(JsonEscape((T && T->Name) ? T->Name : TEXT("<unknown>"))), Pair.Value.Calls,
			Pair.Value.Self, Pair.Value.Inclusive);
		++Emitted;
	}
	Timers += TEXT("]");

	// fences：跨队列 resolved fence（signal 在 A 队列 / wait 在 B 队列，同 Value 配对），
	// stall = wait.Timestamp - signal.Timestamp，是「CPU 等 GPU」的直接证据。
	// 注意引擎实现（TimingProfiler.cpp EnumerateResolvedGpuFences）对每个 QueueId 会从
	// signal 侧与 wait 侧各枚举一遍同一对，因此只保留 SignalQueueId == 当前队列 的那份，避免重复计数。
	struct FFencePair
	{
		uint32 SignalQueue = 0;
		uint32 WaitQueue = 0;
		int64 Count = 0;
		double SumStall = 0.0;
		double MaxStall = 0.0;
	};
	struct FFenceSample
	{
		double Stall = 0.0;
		double SignalTime = 0.0;
		double WaitTime = 0.0;
		uint64 Value = 0;
		uint32 SignalQueue = 0;
		uint32 WaitQueue = 0;
	};
	const int32 MaxWorst = (Limit > 0) ? FMath::Min(Limit, 64) : 16;
	TMap<uint64, FFencePair> FencePairs;
	TArray<FFenceSample> FenceWorst;
	int64 FenceTotal = 0;
	Timing->EnumerateGpuQueues([&](const TraceServices::FGpuQueueInfo& Q)
	{
		Timing->EnumerateResolvedGpuFences(Q.Id, WindowStart, WindowEnd,
			[&](uint32 SignalQueueId, const TraceServices::FGpuSignalFence& Signal,
				uint32 WaitQueueId, const TraceServices::FGpuWaitFence& Wait) -> TraceServices::EEnumerateResult
			{
				if (SignalQueueId != Q.Id) { return TraceServices::EEnumerateResult::Continue; }
				const double Stall = FMath::Max(0.0, Wait.Timestamp - Signal.Timestamp);
				++FenceTotal;
				FFencePair& P = FencePairs.FindOrAdd((static_cast<uint64>(SignalQueueId) << 32) | WaitQueueId);
				P.SignalQueue = SignalQueueId;
				P.WaitQueue = WaitQueueId;
				++P.Count;
				P.SumStall += Stall;
				P.MaxStall = FMath::Max(P.MaxStall, Stall);
				if (FenceWorst.Num() >= MaxWorst)
				{
					// 有界收集：比当前保留集合的最小值还小就丢弃（MaxWorst ≤ 64，线性扫描可忽略）
					int32 MinIdx = 0;
					for (int32 i = 1; i < FenceWorst.Num(); ++i)
					{
						if (FenceWorst[i].Stall < FenceWorst[MinIdx].Stall) { MinIdx = i; }
					}
					if (Stall <= FenceWorst[MinIdx].Stall)
					{
						return TraceServices::EEnumerateResult::Continue;
					}
					FenceWorst.RemoveAt(MinIdx, 1, EAllowShrinking::No);
				}
				FFenceSample S;
				S.Stall = Stall; S.SignalTime = Signal.Timestamp; S.WaitTime = Wait.Timestamp;
				S.Value = Wait.Value; S.SignalQueue = SignalQueueId; S.WaitQueue = WaitQueueId;
				FenceWorst.Add(S);
				return TraceServices::EEnumerateResult::Continue;
			});
	});
	FenceWorst.Sort([](const FFenceSample& A, const FFenceSample& B) { return A.Stall > B.Stall; });

	TArray<FFencePair> FenceRows;
	for (const TPair<uint64, FFencePair>& Pair : FencePairs) { FenceRows.Add(Pair.Value); }
	FenceRows.Sort([](const FFencePair& A, const FFencePair& B) { return A.SumStall > B.SumStall; });
	FString FencePairsJson = TEXT("[");
	for (int32 i = 0; i < FenceRows.Num(); ++i)
	{
		if (i > 0) { FencePairsJson += TEXT(","); }
		const FFencePair& P = FenceRows[i];
		FencePairsJson += FString::Printf(
			TEXT("{\"signal_queue\":%u,\"wait_queue\":%u,\"count\":%lld,\"sum_stall_s\":%.6f,\"max_stall_ms\":%.3f}"),
			P.SignalQueue, P.WaitQueue, P.Count, P.SumStall, P.MaxStall * 1000.0);
	}
	FencePairsJson += TEXT("]");

	FString FenceWorstJson = TEXT("[");
	for (int32 i = 0; i < FenceWorst.Num(); ++i)
	{
		if (i > 0) { FenceWorstJson += TEXT(","); }
		const FFenceSample& S = FenceWorst[i];
		FenceWorstJson += FString::Printf(
			TEXT("{\"stall_ms\":%.3f,\"signal_s\":%.6f,\"wait_s\":%.6f,\"signal_queue\":%u,\"wait_queue\":%u,\"value\":%llu}"),
			S.Stall * 1000.0, S.SignalTime, S.WaitTime, S.SignalQueue, S.WaitQueue, S.Value);
	}
	FenceWorstJson += TEXT("]");

	const double Span = FMath::Max(0.0, WindowEnd - WindowStart);
	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-gpu/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],\"span_s\":%.6f,")
		TEXT("\"gpu_timeline\":{\"available\":%s,\"index\":%u,\"gpu2_available\":%s,\"gpu2_index\":%u},")
		TEXT("\"gpu_busy_s\":%.6f,\"gpu_busy_pct_window\":%.3f,\"sum_all_queues_s\":%.6f,")
		TEXT("\"queue_count\":%d,\"queue_busy\":%s,\"queues\":%s,")
		TEXT("\"timer_count\":%d,\"timers\":%s")
		TEXT(",\"fences\":{\"events_in_window\":%lld,\"pair_count\":%d,\"pairs\":%s,\"worst\":%s}}"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, Span,
		bHasGpu ? TEXT("true") : TEXT("false"), GpuTimeline,
		bHasGpu2 ? TEXT("true") : TEXT("false"), Gpu2Timeline,
		GraphicsBusy, Span > 0.0 ? 100.0 * GraphicsBusy / Span : 0.0, SumAllQueues,
		QueueCount, *QueueBusy, *Queues,
		Acc.Num(), *Timers,
		FenceTotal, FenceRows.Num(), *FencePairsJson, *FenceWorstJson);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_GPU queues=%d aggregated=%d gpu_timeline=%d busy=%.3fs timers=%d fences=%lld"),
		QueueCount, QueuesAggregated, bHasGpu ? (int32)GpuTimeline : -1, SumSelf, Acc.Num(), FenceTotal);
}

// counters：计数器清单 + 窗口内采样点数与最后值
static void EmitCounters(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd, int32 Limit)
{
	const TraceServices::ICounterProvider& Counters = TraceServices::ReadCounterProvider(Session);
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	FString Rows = TEXT("[");
	uint64 Count = Counters.GetCounterCount();
	int32 Emitted = 0;
	Counters.EnumerateCounters([&](uint32 CounterId, const TraceServices::ICounter& Counter)
	{
		if (Limit > 0 && Emitted >= Limit) { return; }
		int64 Samples = 0;
		int64 LastValue = 0;
		Counter.EnumerateValues(WindowStart, WindowEnd, false,
			[&Samples, &LastValue](double, int64 Value)
			{
				if (Samples < 1000000) { ++Samples; LastValue = Value; }
			});
		if (Emitted > 0) { Rows += TEXT(","); }
		Rows += FString::Printf(
			TEXT("{\"id\":%u,\"name\":\"%s\",\"group\":\"%s\",\"samples\":%lld,\"last_value\":%lld}"),
			CounterId, *JsonEscape(Counter.GetName()), *JsonEscape(Counter.GetGroup()),
			Samples, LastValue);
		++Emitted;
	});
	Rows += TEXT("]");

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-counters/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"counter_count\":%llu,\"emitted\":%d,\"counters\":%s}"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, (unsigned long long)Count, Emitted, *Rows);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_COUNTERS count=%llu emitted=%d"), (unsigned long long)Count, Emitted);
}

// regions：按 region timer 名字聚合次数与总时长
static void EmitRegions(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd, int32 Limit)
{
	const TraceServices::IRegionProvider& Regions = TraceServices::ReadRegionProvider(Session);
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	struct FStat { int64 Count = 0; double Total = 0.0; double Max = 0.0; };
	TMap<FString, FStat> Stats;
	uint64 RegionCount = 0;
	uint64 NonFinite = 0;
	Regions.BeginRead();   // 区域 provider 有自己的读锁：只包在 FAnalysisSessionReadScope 里不够
	Regions.GetDefaultTimeline().EnumerateRegions(WindowStart, WindowEnd,
		[&Stats, &RegionCount, &NonFinite](const TraceServices::FTimeRegion& R)
		{
			++RegionCount;
			// R13：未闭合的 region 会被导出成 +inf，丢弃并计数
			if (!FMath::IsFinite(R.BeginTime) || !FMath::IsFinite(R.EndTime))
			{
				++NonFinite;
				return true;
			}
			const TCHAR* Name = (R.Timer && R.Timer->Name) ? R.Timer->Name : TEXT("<unknown>");
			const double Duration = FMath::Max(0.0, R.EndTime - R.BeginTime);
			FStat& S = Stats.FindOrAdd(FString(Name));
			S.Count += 1;
			S.Total += Duration;
			S.Max = FMath::Max(S.Max, Duration);
			return true;
		});
	Regions.EndRead();

	TArray<TPair<FString, FStat>> Rows;
	for (const TPair<FString, FStat>& Pair : Stats) { Rows.Add(Pair); }
	Rows.Sort([](const TPair<FString, FStat>& A, const TPair<FString, FStat>& B)
	{
		return A.Value.Total > B.Value.Total;
	});

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-regions/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"region_count\":%llu,\"non_finite\":%llu,\"timer_count\":%d,\"regions\":["),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, (unsigned long long)RegionCount,
		(unsigned long long)NonFinite, Rows.Num());
	int32 Emitted = 0;
	for (const TPair<FString, FStat>& Pair : Rows)
	{
		if (Limit > 0 && Emitted >= Limit) { break; }
		if (Emitted > 0) { Out += TEXT(","); }
		Out += FString::Printf(TEXT("{\"name\":\"%s\",\"count\":%lld,\"total_s\":%.6f,\"max_s\":%.6f}"),
			*JsonEscape(*Pair.Key), Pair.Value.Count, Pair.Value.Total, Pair.Value.Max);
		++Emitted;
	}
	Out += TEXT("]}");
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_REGIONS regions=%llu non_finite=%llu timers=%d"),
		(unsigned long long)RegionCount, (unsigned long long)NonFinite, Rows.Num());
}

// logs：消息总数 + 窗口内按类别计数 + 前 N 条样本
static void EmitLogs(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd, int32 Limit)
{
	const TraceServices::ILogProvider& Logs = TraceServices::ReadLogProvider(Session);
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	TMap<FString, int64> ByCategory;
	uint64 InWindow = 0;
	TArray<FString> Samples;
	Logs.EnumerateMessages(WindowStart, WindowEnd, [&](const TraceServices::FLogMessageInfo& M)
	{
		++InWindow;
		const TCHAR* Cat = (M.Category && M.Category->Name) ? M.Category->Name : TEXT("<none>");
		ByCategory.FindOrAdd(FString(Cat)) += 1;
		if (Limit <= 0 || Samples.Num() < Limit)
		{
			Samples.Add(FString::Printf(TEXT("%.3f|%s|%s"), M.Time, Cat,
				(M.Message ? M.Message : TEXT(""))).Left(240));
		}
	});

	TArray<TPair<FString, int64>> Cats;
	for (const TPair<FString, int64>& Pair : ByCategory) { Cats.Add(Pair); }
	Cats.Sort([](const TPair<FString, int64>& A, const TPair<FString, int64>& B)
	{
		return A.Value > B.Value;
	});

	FString CatJson = TEXT("[");
	for (int32 I = 0; I < Cats.Num() && I < 20; ++I)
	{
		if (I > 0) { CatJson += TEXT(","); }
		CatJson += FString::Printf(TEXT("{\"category\":\"%s\",\"count\":%lld}"),
			*JsonEscape(*Cats[I].Key), Cats[I].Value);
	}
	CatJson += TEXT("]");

	FString SampleJson = TEXT("[");
	for (int32 I = 0; I < Samples.Num(); ++I)
	{
		if (I > 0) { SampleJson += TEXT(","); }
		SampleJson += FString::Printf(TEXT("\"%s\""), *JsonEscape(*Samples[I]));
	}
	SampleJson += TEXT("]");

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-logs/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"total_messages\":%llu,\"window_messages\":%llu,\"categories\":%s,\"samples\":%s}"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd,
		(unsigned long long)Logs.GetMessageCount(), (unsigned long long)InWindow,
		*CatJson, *SampleJson);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_LOGS total=%llu window=%llu categories=%d"),
		(unsigned long long)Logs.GetMessageCount(), (unsigned long long)InWindow, Cats.Num());
}

// channels：通道清单
static void EmitChannels(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath)
{
	const TraceServices::IChannelProvider* Channels = TraceServices::ReadChannelProvider(Session);
	if (!Channels) { Ar.Logf(TEXT("PERF_ERR no channel provider")); return; }
	const TArray<TraceServices::FChannelEntry>& List = Channels->GetChannels();
	FString Rows = TEXT("[");
	int32 Emitted = 0;
	for (const TraceServices::FChannelEntry& C : List)
	{
		if (Emitted > 0) { Rows += TEXT(","); }
		Rows += FString::Printf(TEXT("{\"id\":%u,\"name\":\"%s\",\"enabled\":%s,\"read_only\":%s}"),
			C.Id, *JsonEscape(*C.Name),
			C.bIsEnabled ? TEXT("true") : TEXT("false"),
			C.bReadOnly ? TEXT("true") : TEXT("false"));
		++Emitted;
	}
	Rows += TEXT("]");
	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-channels/0\",\"trace\":\"%s\",\"channel_count\":%llu,\"channels\":%s}"),
		*JsonEscape(*TracePath), (unsigned long long)Channels->GetChannelCount(), *Rows);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_CHANNELS count=%llu"), (unsigned long long)Channels->GetChannelCount());
}

// tasks：任务数 + 窗口内按 DebugName 聚合
static void EmitTasks(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd, int32 Limit)
{
	const TraceServices::ITasksProvider* Tasks = TraceServices::ReadTasksProvider(Session);
	if (!Tasks) { Ar.Logf(TEXT("PERF_ERR no tasks provider")); return; }
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }

	TMap<FString, int64> ByName;
	uint64 InWindow = 0;
	Tasks->EnumerateTasks(WindowStart, WindowEnd, TraceServices::ETaskEnumerationOption::Launched,
		[&ByName, &InWindow](const TraceServices::FTaskInfo& Info)
			-> TraceServices::ETaskEnumerationResult
		{
			++InWindow;
			const TCHAR* Name = Info.DebugName ? Info.DebugName : TEXT("<unnamed>");
			ByName.FindOrAdd(FString(Name)) += 1;
			return TraceServices::ETaskEnumerationResult::Continue;
		});

	TArray<TPair<FString, int64>> Rows;
	for (const TPair<FString, int64>& Pair : ByName) { Rows.Add(Pair); }
	Rows.Sort([](const TPair<FString, int64>& A, const TPair<FString, int64>& B)
	{
		return A.Value > B.Value;
	});

	FString Json = TEXT("[");
	int32 Emitted = 0;
	for (const TPair<FString, int64>& Pair : Rows)
	{
		if (Limit > 0 && Emitted >= Limit) { break; }
		if (Emitted > 0) { Json += TEXT(","); }
		Json += FString::Printf(TEXT("{\"name\":\"%s\",\"count\":%lld}"),
			*JsonEscape(*Pair.Key), Pair.Value);
		++Emitted;
	}
	Json += TEXT("]");

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-tasks/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"task_count\":%lld,\"window_tasks\":%llu,\"by_name\":%s}"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, Tasks->GetNumTasks(),
		(unsigned long long)InWindow, *Json);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_TASKS total=%lld window=%llu names=%d"),
		Tasks->GetNumTasks(), (unsigned long long)InWindow, Rows.Num());
}

// memory：内存 tag 清单
static void EmitMemory(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, int32 Limit)
{
	const TraceServices::IMemoryProvider* Memory = TraceServices::ReadMemoryProvider(Session);
	if (!Memory) { Ar.Logf(TEXT("PERF_ERR no memory provider")); return; }

	FString Json = TEXT("[");
	int32 Emitted = 0;
	uint32 TagCount = 0;
	Memory->BeginRead();
	TagCount = Memory->GetTagCount();
	Memory->EnumerateTags([&Json, &Emitted, Limit](const TraceServices::FMemoryTagInfo& Tag)
	{
		if (Limit > 0 && Emitted >= Limit) { return; }
		if (Emitted > 0) { Json += TEXT(","); }
		Json += FString::Printf(TEXT("{\"id\":%u,\"parent\":%u,\"name\":\"%s\"}"),
			(uint32)Tag.Id, (uint32)Tag.ParentId, *JsonEscape(*Tag.Name));
		++Emitted;
	});
	Memory->EndRead();
	Json += TEXT("]");

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-memory/0\",\"trace\":\"%s\",\"tag_count\":%u,\"emitted\":%d,\"tags\":%s}"),
		*JsonEscape(*TracePath), TagCount, Emitted, *Json);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_MEMORY tags=%u emitted=%d"), TagCount, Emitted);
}

// allocations：时间线点数与窗口索引范围
static void EmitAllocations(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath, double WindowStart, double WindowEnd)
{
	const TraceServices::IAllocationsProvider* Alloc = TraceServices::ReadAllocationsProvider(Session);
	if (!Alloc) { Ar.Logf(TEXT("PERF_ERR no allocations provider")); return; }
	if (WindowEnd <= 0.0) { WindowEnd = Session.GetDurationSeconds(); }
	int32 StartIndex = -1, EndIndex = -1;
	Alloc->BeginRead();
	Alloc->GetTimelineIndexRange(WindowStart, WindowEnd, StartIndex, EndIndex);
	const int32 NumPoints = Alloc->GetTimelineNumPoints();
	Alloc->EndRead();

	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-allocations/0\",\"trace\":\"%s\",\"window\":[%.6f,%.6f],")
		TEXT("\"num_points\":%d,\"index_range\":[%d,%d],\"window_points\":%d}"),
		*JsonEscape(*TracePath), WindowStart, WindowEnd, NumPoints, StartIndex, EndIndex,
		FMath::Max(0, EndIndex - StartIndex));
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_ALLOCATIONS points=%d window_points=%d"), NumPoints,
		FMath::Max(0, EndIndex - StartIndex));
}

// net：网络 trace 的基本计数
static void EmitNet(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath)
{
	const TraceServices::INetProfilerProvider* Net = TraceServices::ReadNetProfilerProvider(Session);
	if (!Net) { Ar.Logf(TEXT("PERF_ERR no net profiler provider")); return; }
	uint32 Connections = 0;
	for (uint32 Gi = 0; Gi < Net->GetGameInstanceCount(); ++Gi)
	{
		Connections += Net->GetConnectionCount(Gi);
	}
	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-net/0\",\"trace\":\"%s\",\"version\":%u,")
		TEXT("\"names\":%u,\"event_types\":%u,\"game_instances\":%u,\"connections\":%u}"),
		*JsonEscape(*TracePath), Net->GetNetTraceVersion(), Net->GetNameCount(),
		Net->GetEventTypesCount(), Net->GetGameInstanceCount(), Connections);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_NET version=%u names=%u event_types=%u game_instances=%u connections=%u"),
		Net->GetNetTraceVersion(), Net->GetNameCount(), Net->GetEventTypesCount(),
		Net->GetGameInstanceCount(), Connections);
}

// load：加载分析的两张表规模
static void EmitLoad(const TraceServices::IAnalysisSession& Session, FOutputDevice& Ar,
	const FString& TracePath, const FString& OutPath)
{
	const TraceServices::ILoadTimeProfilerProvider* Load = TraceServices::ReadLoadTimeProfilerProvider(Session);
	const TraceServices::IFileActivityProvider* FileActivity = TraceServices::ReadFileActivityProvider(Session);
	if (!Load && !FileActivity) { Ar.Logf(TEXT("PERF_ERR no load time / file activity provider")); return; }
	const uint64 FileRows = FileActivity ? FileActivity->GetFileActivityTable().GetRowCount() : 0;
	const uint64 RequestRows = Load ? Load->GetRequestsTable().GetRowCount() : 0;
	FString Out = FString::Printf(
		TEXT("{\"schema\":\"perf-b2-load/0\",\"trace\":\"%s\",\"file_activity_rows\":%llu,\"request_rows\":%llu}"),
		*JsonEscape(*TracePath), (unsigned long long)FileRows, (unsigned long long)RequestRows);
	WriteJson(Ar, OutPath, Out);
	Ar.Logf(TEXT("PERF_LOAD file_activity=%llu requests=%llu"),
		(unsigned long long)FileRows, (unsigned long long)RequestRows);
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
	const FString TimerName = ParseArg(Params, TEXT("timer"));
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
	if (Mode == TEXT("callers"))
	{
		EmitCallers(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, ThreadName, TimerName, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("gpu"))
	{
		EmitGpu(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("counters"))
	{
		EmitCounters(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("regions"))
	{
		EmitRegions(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("logs"))
	{
		EmitLogs(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("channels"))
	{
		EmitChannels(*Session, Ar, TracePath, OutPath);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("tasks"))
	{
		EmitTasks(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("memory"))
	{
		EmitMemory(*Session, Ar, TracePath, OutPath, Limit);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("allocations"))
	{
		EmitAllocations(*Session, Ar, TracePath, OutPath, WindowStart, WindowEnd);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("net"))
	{
		EmitNet(*Session, Ar, TracePath, OutPath);
		Ar.Logf(TEXT("PERF_DONE"));
		return;
	}
	if (Mode == TEXT("load"))
	{
		EmitLoad(*Session, Ar, TracePath, OutPath);
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
