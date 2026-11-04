// Copyright (c) 2026 ue-perf-evidence. Implementation of the PerfEvidence toolset (M3.1).

#include "PerfEvidenceToolset.h"
#include "Misc/FileHelper.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GenericPlatform/GenericPlatformProcess.h"
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

FString UPerfEvidenceToolset::PerfEvidenceLines(const FString& RunDir, const FString& Ref,
	int32 Page, int32 Limit)
{
	const FString Repo = TEXT("D:/dsh-output/ue-perf-evidence");
	const FString Script = Repo / TEXT("perfctl.py");
	const FString OutDir = RunDir / TEXT("evidence_out");
	const int32 UsePage = Page > 0 ? Page : 1;
	const int32 UseLimit = Limit > 0 ? Limit : 200;
	// perfctl.py evidence <run_dir> --ref <ref> --out <dir> --page N --limit N
	const FString Args = FString::Printf(TEXT("-X utf8 \"%s\" evidence \"%s\" --ref \"%s\" --out \"%s\" --page %d --limit %d"),
		*Script, *RunDir, *Ref, *OutDir, UsePage, UseLimit);
	int32 ReturnCode = 1;
	FString StdOut, StdErr;
	if (!FPlatformProcess::ExecProcess(TEXT("python"), *Args, &ReturnCode, &StdOut, &StdErr))
	{
		return TEXT("err: python launch failed (is python on PATH?)");
	}
	const FString JsonPath = OutDir / TEXT("evidence.json");
	if (!FPaths::FileExists(JsonPath))
	{
		return FString::Printf(TEXT("err: perf evidence failed (rc=%d) %s"), ReturnCode, *StdErr.Left(300));
	}
	FString Payload;
	if (!FFileHelper::LoadFileToString(Payload, *JsonPath))
	{
		return TEXT("err: cannot read evidence.json");
	}
	// 给 agent 的东西要小：一页默认 200 行已经够，这里再兜一道 16KB 上限
	const int32 MaxChars = 16000;
	if (Payload.Len() > MaxChars)
	{
		Payload = Payload.Left(MaxChars) +
			FString::Printf(TEXT("\n…（截断：完整内容见 %s；用 page 参数翻页）"), *JsonPath);
	}
	return Payload;
}

class FPerfEvidenceToolsetModule : public IModuleInterface
{
	void StartupModule()
	{
		UToolsetRegistry::RegisterToolsetClass(UPerfEvidenceToolset::StaticClass());
	}

	void ShutdownModule()
	{
		UToolsetRegistry::UnregisterToolsetClass(UPerfEvidenceToolset::StaticClass());
	}
};

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FPerfEvidenceToolsetModule, PerfEvidenceToolset)
