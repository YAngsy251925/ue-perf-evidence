// Copyright (c) 2026 ue-perf-evidence. UMG/Blueprint structure dump (M10.2).

#include "PerfEvidenceUILibrary.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "Blueprint/WidgetTree.h"
#include "Components/PanelWidget.h"
#include "Components/Widget.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "Engine/Blueprint.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Misc/Paths.h"
#include "WidgetBlueprint.h"

namespace
{
	FString Esc(const FString& In)
	{
		FString Out;
		Out.Reserve(In.Len() + 8);
		for (const TCHAR Ch : In)
		{
			switch (Ch)
			{
			case TEXT('"'):  Out += TEXT("\\\""); break;
			case TEXT('\\'): Out += TEXT("\\\\"); break;
			case TEXT('\n'): Out += TEXT("\\n"); break;
			case TEXT('\r'): Out += TEXT("\\r"); break;
			case TEXT('\t'): Out += TEXT("\\t"); break;
			default:         Out.AppendChar(Ch); break;
			}
		}
		return Out;
	}

	void WalkWidget(UWidget* Widget, int32 Depth, TArray<FString>& Out, int32& Budget)
	{
		if (!Widget || Budget <= 0)
		{
			return;
		}
		--Budget;
		const FString VisibilityName = StaticEnum<ESlateVisibility>()->GetNameStringByValue(
			(int64)Widget->GetVisibility());
		Out.Add(FString::Printf(
			TEXT("{\"depth\":%d,\"name\":\"%s\",\"class\":\"%s\",\"visibility\":\"%s\"}"),
			Depth, *Esc(Widget->GetName()), *Esc(Widget->GetClass()->GetName()),
			*Esc(VisibilityName)));

		if (const UPanelWidget* Panel = Cast<UPanelWidget>(Widget))
		{
			const int32 Count = Panel->GetChildrenCount();
			for (int32 i = 0; i < Count && Budget > 0; ++i)
			{
				WalkWidget(Panel->GetChildAt(i), Depth + 1, Out, Budget);
			}
		}
	}
}

TArray<FString> UPerfEvidenceUILibrary::ListWidgetBlueprints(const FString& PackagePath, int32 MaxCount)
{
	TArray<FString> Out;
	FAssetRegistryModule& RegistryModule = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
	IAssetRegistry& Registry = RegistryModule.Get();
	FARFilter Filter;
	Filter.ClassPaths.Add(UWidgetBlueprint::StaticClass()->GetClassPathName());
	Filter.PackagePaths.Add(FName(*PackagePath));
	Filter.bRecursivePaths = true;
	TArray<FAssetData> Assets;
	Registry.GetAssets(Filter, Assets);
	const int32 Limit = MaxCount > 0 ? FMath::Min(MaxCount, Assets.Num()) : Assets.Num();
	for (int32 i = 0; i < Limit; ++i)
	{
		Out.Add(Assets[i].GetObjectPathString());
	}
	return Out;
}

FString UPerfEvidenceUILibrary::DumpWidgetBlueprint(const FString& AssetPath)
{
	UWidgetBlueprint* BP = LoadObject<UWidgetBlueprint>(nullptr, *AssetPath);
	if (!BP)
	{
		return FString::Printf(TEXT("{\"error\":\"cannot load %s\"}"), *Esc(AssetPath));
	}

	// 控件树
	TArray<FString> Widgets;
	int32 Budget = 500;
	if (UWidgetTree* Tree = BP->WidgetTree)
	{
		WalkWidget(Tree->RootWidget, 0, Widgets, Budget);
	}

	// 属性绑定
	TArray<FString> Bindings;
	for (const FDelegateEditorBinding& Binding : BP->Bindings)
	{
		Bindings.Add(FString::Printf(
			TEXT("{\"object\":\"%s\",\"property\":\"%s\",\"function\":\"%s\"}"),
			*Esc(Binding.ObjectName), *Esc(Binding.PropertyName.ToString()),
			*Esc(Binding.FunctionName.ToString())));
	}

	// Ubergraph 节点直方图 + 事件入口
	TMap<FString, int32> Hist;
	TArray<FString> Events;
	int32 NodeCount = 0;
	TSet<FString> SeenEvents;
	for (UEdGraph* Graph : BP->UbergraphPages)
	{
		if (!Graph) { continue; }
		for (UEdGraphNode* Node : Graph->Nodes)
		{
			if (!Node) { continue; }
			++NodeCount;
			const FString ClassName = Node->GetClass()->GetName();
			Hist.FindOrAdd(ClassName) += 1;
			if (ClassName.Contains(TEXT("Event")) || ClassName.Contains(TEXT("FunctionEntry")))
			{
				const FString NodeName = Node->GetNodeTitle(ENodeTitleType::ListView).ToString();
				if (!SeenEvents.Contains(NodeName))
				{
					SeenEvents.Add(NodeName);
					Events.Add(NodeName);
				}
			}
		}
	}
	TArray<TPair<FString, int32>> HistRows;
	for (const TPair<FString, int32>& Pair : Hist) { HistRows.Add(Pair); }
	HistRows.Sort([](const TPair<FString, int32>& A, const TPair<FString, int32>& B)
	{
		return A.Value > B.Value;
	});

	FString Out = FString::Printf(
		TEXT("{\"path\":\"%s\",\"parent_class\":\"%s\",\"widget_count\":%d,\"widgets\":["),
		*Esc(AssetPath),
		*Esc(BP->ParentClass ? BP->ParentClass->GetName() : TEXT("")), Widgets.Num());
	Out += FString::Join(Widgets, TEXT(","));
	Out += TEXT("],\"bindings\":[");
	Out += FString::Join(Bindings, TEXT(","));
	Out += FString::Printf(TEXT("],\"graph\":{\"node_count\":%d,\"by_class\":{"), NodeCount);
	int32 Idx = 0;
	for (int32 i = 0; i < HistRows.Num() && i < 20; ++i)
	{
		Out += FString::Printf(TEXT("%s\"%s\":%d"), Idx++ > 0 ? TEXT(",") : TEXT(""),
			*Esc(HistRows[i].Key), HistRows[i].Value);
	}
	Out += TEXT("},\"events\":[");
	TArray<FString> QuotedEvents;
	for (int32 i = 0; i < Events.Num() && i < 40; ++i)
	{
		QuotedEvents.Add(FString::Printf(TEXT("\"%s\""), *Esc(Events[i])));
	}
	Out += FString::Join(QuotedEvents, TEXT(","));
	Out += TEXT("]}}");
	return Out;
}
