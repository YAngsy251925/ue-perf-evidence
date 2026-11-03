// Copyright (c) 2026 ue-perf-evidence. AI-callable performance tools (M3.1).

#pragma once

#include "CoreMinimal.h"
#include "ToolsetRegistry/ToolsetDefinition.h"
#include "PerfEvidenceToolset.generated.h"

/**
 * Performance evidence tools backed by the ue-perf-evidence pipeline.
 * Capture tools drive the engine's built-in trace profiler directly; report tools
 * run the headless perfctl pipeline (clone of ue-perf-evidence required on this machine).
 */
UCLASS(BlueprintType, MinimalAPI)
class UPerfEvidenceToolset : public UToolsetDefinition
{
	GENERATED_BODY()
public:
	/**
	 * Starts a performance trace capture to the given .utrace path with the default
	 * channel set (cpu,frame,gpu,bookmark). Call PerfStopCapture to finish and
	 * finalize the file. Traces only what the tail buffer holds at stop time.
	 * @param TracePath Absolute .utrace output path (forward slashes recommended).
	 * @return "started" on success, or an error description.
	 */
	UFUNCTION(meta = (AICallable), Category = "PerfEvidence")
	static FString PerfStartCapture(const FString& TracePath);

	/**
	 * Stops the active performance trace capture and finalizes the .utrace file.
	 * @return "stopped" on success, "no active capture" if none was running.
	 */
	UFUNCTION(meta = (AICallable), Category = "PerfEvidence")
	static FString PerfStopCapture();

	/**
	 * Runs the headless ue-perf-evidence pipeline over a run directory and writes
	 * manifest + digest outputs. Requires Python 3 and the ue-perf-evidence repo.
	 * @param RunDir Directory holding engine.log / trace.utrace (the capture run).
	 * @param QuestionType One of: cpu, hitch.
	 * @return Path of the generated perf_digest.md, or an error description.
	 */
	UFUNCTION(meta = (AICallable), Category = "PerfEvidence")
	static FString PerfRunReport(const FString& RunDir, const FString& QuestionType);
};
