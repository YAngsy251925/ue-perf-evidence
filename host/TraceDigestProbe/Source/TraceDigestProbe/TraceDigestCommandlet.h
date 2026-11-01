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
