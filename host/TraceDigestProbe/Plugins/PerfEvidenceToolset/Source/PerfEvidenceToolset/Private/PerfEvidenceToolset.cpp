// Copyright (c) 2026 ue-perf-evidence. Implementation of the PerfEvidence toolset (M3.1).

#include "PerfEvidenceToolset.h"
#include "Containers/Ticker.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GenericPlatform/GenericPlatformProcess.h"
#include "Misc/CoreDelegates.h"
#include "Misc/Paths.h"
#include "ToolsetRegistry/UToolsetRegistry.h"

FString UPerfEvidenceToolset::PerfStartCapture(const FString& TracePath)
{
	const FString Channels = TEXT("cpu,frame,gpu,bookmark");
	FString Cmd = FString::Printf(TEXT("Trace.File %s %s"), *TracePath, *Channels);
	if (!GEngine)
	{
		return TEXT("err: no engine (game only?)");
	}
	const bool bOk = GEngine->Exec(GWorld, *Cmd);
	return bOk ? TEXT("started") : FString::Printf(TEXT("err: Trace.File failed (%s)"), *Cmd);
}

FString UPerfEvidenceToolset::PerfStopCapture()
{
	if (!GEngine)
	{
		return TEXT("err: no engine");
	}
	const bool bOk = GEngine->Exec(GWorld, TEXT("Trace.Stop"));
	return bOk ? TEXT("stopped") : TEXT("err: Trace.Stop failed");
}

FString UPerfEvidenceToolset::PerfRunReport(const FString& RunDir, const FString& QuestionType)
{
	const FString Repo = TEXT("D:/dsh-output/ue-perf-evidence");
	const FString Script = Repo / TEXT("perfctl.py");
	const FString OutDir = RunDir / (TEXT("digest_") + QuestionType);
	const FString Kind = (QuestionType == TEXT("hitch")) ? TEXT("hitch") : TEXT("cpu");
	FString CmdLine = FString::Printf(TEXT("\"%s\" digest %s \"%s\" --out \"%s\""),
		*Script, *Kind, *RunDir, *OutDir);
	int32 ReturnCode = 1;
	FString StdOut, StdErr;
	const bool bOk = FPlatformProcess::ExecProcess(TEXT("python"),
		*FString::Printf(TEXT("-X utf8 %s"), *CmdLine), &ReturnCode, &StdOut, &StdErr);
	if (!bOk)
	{
		return TEXT("err: python launch failed (is python on PATH?)");
	}
	const FString DigestMd = OutDir / TEXT("perf_digest.md");
	return FPaths::FileExists(DigestMd)
		? DigestMd
		: FString::Printf(TEXT("err: pipeline failed (rc=%d) %s"), ReturnCode, *StdErr.Left(200));
}

class FPerfEvidenceToolsetModule : public IModuleInterface
{
public:
	virtual void StartupModule() override
	{
		if (UToolsetRegistry::IsAvailable())
		{
			RegisterToolset();
			return;
		}
		// 注册表背后是编辑器子系统，模块 StartupModule 阶段还不存在
		// （实测：LogToolsetRegistry: Error: AIToolsetRegistrySubsystem unavailable），
		// 所以推到引擎初始化之后再登记。与宿主控制通道同一个教训：
		// DLL 加载 / 模块启动期不要依赖后建的子系统。
		PostEngineInitHandle = FCoreDelegates::OnPostEngineInit.AddRaw(
			this, &FPerfEvidenceToolsetModule::OnPostEngineInit);
	}

	virtual void ShutdownModule() override
	{
		if (PostEngineInitHandle.IsValid())
		{
			FCoreDelegates::OnPostEngineInit.Remove(PostEngineInitHandle);
			PostEngineInitHandle.Reset();
		}
		if (RetryTickerHandle.IsValid())
		{
			FTSTicker::GetCoreTicker().RemoveTicker(RetryTickerHandle);
			RetryTickerHandle.Reset();
		}
		if (bRegistered)
		{
			UToolsetRegistry::UnregisterToolsetClass(UPerfEvidenceToolset::StaticClass());
			bRegistered = false;
		}
	}

private:
	void RegisterToolset()
	{
		if (bRegistered)
		{
			return;
		}
		UToolsetRegistry::RegisterToolsetClass(UPerfEvidenceToolset::StaticClass());
		bRegistered = true;
		UE_LOG(LogTemp, Display,
			TEXT("PerfEvidenceToolset: registered toolset PerfEvidence (3 tools) with the toolset registry"));
	}

	void OnPostEngineInit()
	{
		if (UToolsetRegistry::IsAvailable())
		{
			RegisterToolset();
			return;
		}
		RetrySeconds = 0.0f;
		RetryTickerHandle = FTSTicker::GetCoreTicker().AddTicker(
			TEXT("PerfEvidenceToolsetRetry"), 0.0f,
			[this](float DeltaTime) -> bool
			{
				if (UToolsetRegistry::IsAvailable())
				{
					RegisterToolset();
					return false; // one-shot
				}
				RetrySeconds += DeltaTime;
				if (RetrySeconds > 30.0f)
				{
					UE_LOG(LogTemp, Warning,
						TEXT("PerfEvidenceToolset: toolset registry never became available; tools NOT registered"));
					return false;
				}
				return true;
			});
	}

	FDelegateHandle PostEngineInitHandle;
	FTSTicker::FDelegateHandle RetryTickerHandle;
	bool bRegistered = false;
	float RetrySeconds = 0.0f;
};

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FPerfEvidenceToolsetModule, PerfEvidenceToolset)
