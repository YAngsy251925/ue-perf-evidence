#include "CoreMinimal.h"
#include "TraceDigestCommandlet.h"
#include "Modules/ModuleManager.h"
#include "HAL/IConsoleManager.h"
#if WITH_EDITOR
#include "TraceServices/ITraceServicesModule.h"
#include "TraceServices/AnalysisService.h"
#include "TraceServices/Model/AnalysisSession.h"
#include "TraceServices/Model/TimingProfiler.h"
#endif

IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, TraceDigestProbe, "TraceDigestProbe");

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
#endif
