#pragma once
#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "TraceDigestCommandlet.generated.h"

UCLASS()
class UTraceDigestCommandlet : public UCommandlet
{
	GENERATED_BODY()
public:
	UTraceDigestCommandlet();
	virtual int32 Main(const FString& Params) override;
};

// -run=TraceDigestPerf 的入口：把参数转给 RunTraceDigestPerf（TraceDigestProbe.cpp，普通函数）。
UCLASS()
class UTraceDigestPerfCommandlet : public UCommandlet
{
	GENERATED_BODY()
public:
	UTraceDigestPerfCommandlet();
	virtual int32 Main(const FString& Params) override;
};
