#include "TraceDigestCommandlet.h"
#if WITH_EDITOR
#include "TraceServices/ITraceServicesModule.h"
#include "TraceServices/AnalysisService.h"
#include "TraceServices/Model/AnalysisSession.h"
#include "TraceServices/Model/TimingProfiler.h"
#endif

UTraceDigestCommandlet::UTraceDigestCommandlet()
{
	IsClient = false;
	IsServer = false;
	IsEditor = true;
	LogToConsole = true;
}

int32 UTraceDigestCommandlet::Main(const FString& Params)
{
#if WITH_EDITOR
	FString TracePath;
	FParse::Value(*Params, TEXT("trace="), TracePath);
	UE_LOG(LogTemp, Display, TEXT("DIGEST_START trace=%s"), *TracePath);
	if (TracePath.IsEmpty()) { return 1; }

	ITraceServicesModule& Module = FModuleManager::LoadModuleChecked<ITraceServicesModule>(TEXT("TraceServices"));
	TSharedPtr<TraceServices::IAnalysisService> Service = Module.GetAnalysisService();
	if (!Service.IsValid()) { Service = Module.CreateAnalysisService(); }
	if (!Service.IsValid()) { UE_LOG(LogTemp, Error, TEXT("DIGEST_ERR no service")); return 2; }

	TSharedPtr<const TraceServices::IAnalysisSession> Session = Service->Analyze(*TracePath);
	if (!Session.IsValid()) { UE_LOG(LogTemp, Error, TEXT("DIGEST_ERR analyze")); return 3; }
	Session->Wait();

	int32 TimerCount = 0;
	double DurationSeconds = 0.0;
	{
		TraceServices::FAnalysisSessionReadScope ReadScope(*Session);
		const TraceServices::ITimingProfilerProvider* Timers = TraceServices::ReadTimingProfilerProvider(*Session);
		if (Timers)
		{
			Timers->ReadTimers([&TimerCount](const TraceServices::ITimingProfilerTimerReader& Reader)
			{
				TimerCount = (int32)Reader.GetTimerCount();
			});
		}
		DurationSeconds = Session->GetDurationSeconds();
	}
	UE_LOG(LogTemp, Display, TEXT("DIGEST_OK duration=%.3fs timers=%d"), DurationSeconds, TimerCount);
	return 0;
#else
	return 1;
#endif
}
