# M4.1 — ue-trace-mcp × UE 5.8 TraceServices API 差异清单

> 2026-11-03 · 对照源：ue-trace-mcp `bf73c60`（6129 行 C++，8 个 provider、48 个调用点）
> vs UE 5.8 安装版 `Engine/Source/Developer/TraceServices/Public`（50 个头文件）。
> 生成工具：`tools/api_diff.py`（名字级）、`tools/api_signatures.py`（方法级，按类体匹配）、
> `tools/scan_deprecated.py`（5.8 的 `UE_DEPRECATED` 清单）。

## 一句话结论

**架构没变，改的是名字与弃用标记**：8 个 provider、26 个被调方法在 5.8 里全部存在，没有需要换 provider 的地方；真正的移植成本集中在 **3 处弃用**（编译器会报 C4996 或直接报错）+ 2 处结构体字段。PLAN 4.2 的移植顺序可以照原计划走。

## 三类差异

### A. 弃用且必须改（否则 C4996；我们的宿主工程已实测踩到）

| # | ue-trace-mcp 用法 | 位置 | 5.8 现状 | 迁移动作 |
|---|---|---|---|---|
| A1 | `ITimingProfilerProvider::ReadTimers(Callback)` | 10 处（`TimerLookup.h:7`，以及 Digest / Compare / Frame / Gpu / Overview / Query / Threads 各 1–2 处） | **deprecated（5.8）**："Use GetTimerReader() instead"（`Model/TimingProfiler.h:288-289`） | 改成 `GetTimerReader()` 取 `ITimingProfilerTimerReader`，用 `GetTimerCount()`（同文件 204 行）与 `GetTimer(uint32 TimerId) -> const FTimingProfilerTimer*`（212 行）读名字与分类。**我们自己的 `TraceDigestProbe.cpp` 已经因此报 C4996**，可直接拿它的改法当样板 |
| A2 | `IAllocationsProvider` 的 `EnumerateMaxTotalAllocatedMemoryTimeline` / `EnumerateMinTotalAllocatedMemoryTimeline` / `EnumerateMaxLiveAllocationsTimeline` / `EnumerateMinLiveAllocationsTimeline` / `EnumerateAllocEventsTimeline` / `EnumerateFreeEventsTimeline` / `EnumeratePageInEventsTimeline` / `EnumeratePageOutEventsTimeline` / `EnumerateSwapFreeEventsTimeline` / `EnumerateMaxTotalSwapMemoryTimeline` … | 未直接调用（ue-trace-mcp 已经用新形态 `EnumerateTimeline(ETimelineU64/32, …)`，`Modes/Allocations.cpp:64-115`） | 十个枚举器全部 deprecated（`Model/AllocationsProvider.h:203-275`），官方建议 `EnumerateTimeline(ETimelineUInt64/UInt32, …)` | **无需改**。但要保留这条记录：5.8 的非弃用入口是 `EnumerateTimeline(ETimelineU64/ETimelineU32, StartIndex, EndIndex, Callback)`（同文件 283/286 行） |
| A3 | `FTimeRegion::Text` / `FTimeRegion::Category` | 未直接使用（ue-trace-mcp 走 `Timer->Name`） | deprecated（5.7）："Use instead Timer->Name" / "Timer->Category->Name"（`Model/Regions.h:41,44`） | **无需改**。M4.2 移植 region 视图时不要退回旧字段 |

### B. 名字在、签名一致（可直接编）

方法级对照 26 个方法，24 个与 5.8 声明逐字一致（`docs/UE_TRACE_MCP_SIGNATURES.md` 全表）。抽样核对：

| provider.method | 5.8 声明 | 判断 |
|---|---|---|
| `IFrameProvider`（`ReadFrameProvider`） | `Model/Frames.h` | 一致（我们的 B2 已在用） |
| `ITimingProfilerProvider::ReadTimeline(uint32, TFunctionRef<void(const Timeline&)>)` | `TimingProfiler.h:281` | 一致 |
| `ITimingProfilerProvider::EnumerateTimelines(TFunctionRef<void(const Timeline&)>)` | `TimingProfiler.h:283` | 一致（注意别被 `StackSamples.h:48` 的同名方法误导，类体匹配才对得上） |
| `ITimingProfilerProvider::CreateButterfly(const FCreateButterflyParams&)` | `TimingProfiler.h:379` | 一致；只有**旧签名** `CreateButterfly(const TCHAR*, …)` 被弃用，ue-trace-mcp 已用新结构体（`Modes/Butterfly.cpp:51,67`） |
| `ITimingProfilerProvider::EnumerateGpuQueues / GetGpuTimelineIndex / GetGpu2TimelineIndex` | `TimingProfiler.h:338-342` | 一致（M4.0 的 GPU 视图要用的就是这三个） |
| `IMemoryProvider::EnumerateTags(TFunctionRef<void(const FMemoryTagInfo&)>)` | `Memory.h:106` | 一致（调用点 `Modes/Memory.cpp:53` 的 lambda 参数类型相同） |
| `IMemoryProvider::EnumerateTagSamples(FMemoryTrackerId, FMemoryTagId, double, double, bool, …)` | `Memory.h:161` | 一致 |
| `ITasksProvider::EnumerateTasks(double, double, ETaskEnumerationOption, TaskCallback)` | `TasksProfiler.h:131` | 一致 |
| `IModuleProvider::EnumerateModules / GetStats / HasFinishedResolving / GetNumModules` | `Modules.h:195-224` | 一致 |
| `IBookmarkProvider` + `FBookmark{Time, Text, CallstackId}` | `Bookmarks.h:13-18` | 一致（`FBookmark::Text` 没有被弃用，和 `FTimeRegion::Text` 不是一回事） |

### C. 语义上要注意、但编译不会报的

| # | 事项 | 影响 |
|---|---|---|
| C1 | `IAllocationsProvider` 的时间线枚举按 **index 区间**（`GetTimelineIndexRange(Start,End,OutStart,OutEnd)` 先把时间换成索引） | 移植时必须保留这层换算，否则窗口边界会错位 |
| C2 | `ITimingProfilerProvider::EnumerateTimelines` 在新版里可能返回**多条 timeline**（每 CPU 线程一条），ue-trace-mcp 假设"一条默认 timeline" | 移植后按它自己的注释保留"取第一条 + 记录 TimelineCount"，别硬写死 |
| C3 | `CreateButterfly` 返回的是**已计算好的蝴蝶图**，5.8 版本有 `FCreateButterflyParams` 的字段增删风险 | M4.2 编过一次就固定；golden 里要记录引擎版本 |

## 移植顺序（对齐 PLAN 4.2，按"依赖 + 风险"排）

| 序 | 模块 | 依赖 | 风险 | 备注 |
|---|---|---|---|---|
| 1 | `frames`（`IFrameProvider`） | 无 | 低 | 作为 R4 的第三级帧轨道，同时验证类体匹配结论 |
| 2 | `timers`（`ITimingProfilerProvider` + `GetTimerReader`） | 无 | **中** | 唯一需要改代码的地方（A1）；改完 digest 的 Top-N 才有 5.8 口径 |
| 3 | `callers / callees` | timers | 低 | 建立在 timer ID 之上 |
| 4 | `gpu`（`EnumerateGpuQueues` + timeline index） | timers | 低 | 与 M4.0 的 A 路 GPU1 视图并行，用于"CPU 等 GPU"判定 |
| 5 | `counters / regions / bookmarks / logs / channels` | 无 | 低 | 结构体字段已确认可用（B 表） |
| 6 | `allocations / memory` | 无 | 中 | 索引区间换算（C1）容易错，配 golden 断言 |
| 7 | `tasks / net / load` | 无 | 低 | 调用点少，最后补 |

## 复现

```bash
python -X utf8 tools/api_diff.py        --mcp <ue-trace-mcp 目录>   # 名字级：include / provider / Read* 函数
python -X utf8 tools/api_signatures.py  --mcp <ue-trace-mcp 目录>   # 方法级：按类体匹配 5.8 声明 + 弃用标记
python -X utf8 tools/scan_deprecated.py                              # 5.8 TraceServices 全部 UE_DEPRECATED
```

产物：`docs/UE_TRACE_MCP_SIGNATURES.md`（方法级全表）、`docs/UE_TRACE_MCP_API_DIFF.md`（名字级全表），
本文是结论与动作清单。
