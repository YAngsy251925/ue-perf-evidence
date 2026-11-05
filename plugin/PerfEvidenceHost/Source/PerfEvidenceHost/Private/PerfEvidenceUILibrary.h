// Copyright (c) 2026 ue-perf-evidence. UMG/Blueprint structure dump (M10.2).
//
// 为什么需要 C++：UWidgetBlueprint::WidgetTree / Bindings、UBlueprint::UbergraphPages 在
// Python 里是 **protected**，get_editor_property 直接被拒（实测：'Property ... is protected and
// cannot be read'）。所以由 C++ 暴露一个 BlueprintCallable，Python 拿 JSON 再落盘。

#pragma once

#if WITH_EDITOR   // 编辑器专用：UMG/Blueprint 结构导出

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "PerfEvidenceUILibrary.generated.h"

/**
 * Dumps the structure of a Widget Blueprint as JSON: widget tree (name/class/visibility/volatile),
 * property bindings (target property + function), and ubergraph node histogram + event names.
 */
UCLASS()
class UPerfEvidenceUILibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()
public:
	/**
	 * @param AssetPath e.g. "/Game/UI/Hud/W_Crosshair"
	 * @return JSON string; on failure {"error":"..."}
	 */
	UFUNCTION(BlueprintCallable, Category = "PerfEvidence")
	static FString DumpWidgetBlueprint(const FString& AssetPath);

	/** Lists Widget Blueprint asset paths under a package path (recursive). */
	UFUNCTION(BlueprintCallable, Category = "PerfEvidence")
	static TArray<FString> ListWidgetBlueprints(const FString& PackagePath, int32 MaxCount);
};
#endif // WITH_EDITOR
