# ue-trace-mcp × UE 5.8 TraceServices API 对照（M4.1）

由 `python -X utf8 tools/api_diff.py --mcp <ue-trace-mcp 目录>` 生成；对照源是 UE 5.8 安装版的 `Engine/Source/Developer/TraceServices/Public`（50 个头文件）。

判定口径：**名字级**。✅ = 5.8 头文件里能找到同名声明；❌ = 找不到，M4.2 必须改写；⚠️ 名字在但签名/语义可能变（本脚本不判签名），由 M4.2 编译逐个确认；include 行单独按路径比对。

## TraceServices include

| 符号 | 次数 | 5.8 | 例（ue-trace-mcp） |
|---|---|---|---|
| `TraceServices/Model/AnalysisSession.h` | 26 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Daemon.cpp:13 |
| `TraceServices/Model/TimingProfiler.h` | 10 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/TimerLookup.h:7 |
| `TraceServices/Model/Frames.h` | 10 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:11 |
| `TraceServices/Model/Regions.h` | 5 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:15 |
| `TraceServices/Model/AllocationsProvider.h` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Allocations.cpp:12 |
| `TraceServices/Model/Counters.h` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:12 |
| `TraceServices/Model/Memory.h` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:14 |
| `TraceServices/Model/Modules.h` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Callstack.cpp:9 |
| `TraceServices/Model/Channel.h` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Channels.cpp:8 |
| `TraceServices/Model/Threads.h` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Frame.cpp:10 |
| `TraceServices/Model/Log.h` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Logs.cpp:9 |
| `TraceServices/AnalysisService.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Session.cpp:17 |
| `TraceServices/ITraceServicesModule.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Session.cpp:18 |
| `TraceServices/ModuleService.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Session.cpp:20 |
| `TraceServices/Containers/Tables.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:9 |
| `TraceServices/Model/LoadTimeProfiler.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:12 |
| `TraceServices/Model/Bookmarks.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Bookmarks.cpp:8 |
| `TraceServices/Model/Callstack.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Callstack.cpp:8 |
| `TraceServices/Model/NetProfiler.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:8 |
| `TraceServices/Model/TasksProfiler.h` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Tasks.cpp:10 |

## Provider 类型

| 符号 | 次数 | 5.8 | 例（ue-trace-mcp） |
|---|---|---|---|
| `IAllocationsProvider` | 35 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Allocations.cpp:20 |
| `ITimingProfilerProvider` | 31 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/ProviderReadScope.h:14 |
| `IFrameProvider` | 17 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/ProviderReadScope.h:15 |
| `IMemoryProvider` | 10 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:257 |
| `IRegionProvider` | 6 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:148 |
| `ICounterProvider` | 6 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:327 |
| `INetProfilerProvider` | 6 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:15 |
| `ILoadTimeProfilerProvider` | 5 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:19 |
| `IThreadProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/ProviderReadScope.h:15 |
| `IChannelProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/ProviderReadScope.h:15 |
| `ILogProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Logs.cpp:56 |
| `ITasksProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Tasks.cpp:17 |
| `InProvider` | 2 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/ProviderReadScope.h:22 |
| `IModuleProvider` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Modules.cpp:40 |
| `IBookmarkProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Bookmarks.cpp:23 |
| `ICallstacksProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Callstack.cpp:38 |

## 会话访问函数（Read*Provider）

| 符号 | 次数 | 5.8 | 例（ue-trace-mcp） |
|---|---|---|---|
| `ReadFrameProvider` | 10 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:48 |
| `ReadTimingProfilerProvider` | 10 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Butterfly.cpp:23 |
| `ReadRegionProvider` | 5 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:148 |
| `ReadAllocationsProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Allocations.cpp:258 |
| `ReadMemoryProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:257 |
| `ReadCounterProvider` | 3 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:327 |
| `ReadChannelProvider` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Channels.cpp:24 |
| `ReadThreadProvider` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Frame.cpp:27 |
| `ReadLogProvider` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Logs.cpp:56 |
| `ReadTasksProvider` | 2 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Tasks.cpp:81 |
| `ReadLoadTimeProfilerProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:190 |
| `ReadBookmarkProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Bookmarks.cpp:23 |
| `ReadCallstacksProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Callstack.cpp:38 |
| `ReadModuleProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Modules.cpp:40 |
| `ReadNetProfilerProvider` | 1 | ✅ | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:243 |

## 数据读取函数（Read*）

| 符号 | 次数 | 5.8 | 例（ue-trace-mcp） |
|---|---|---|---|
| `ReadTimers` | 10 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/TimerLookup.h:20 |
| `ReadTimeline` | 6 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:119 |
| `ReadAllTimed` | 3 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Daemon.cpp:80 |
| `ReadAllocationsProvider` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Allocations.cpp:258 |
| `ReadConnections` | 2 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:102 |
| `ReadGameInstances` | 1 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:73 |
| `ReadObjects` | 1 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Net.cpp:208 |

## 高频成员函数（≥3 次出现，用于 M4.2 逐个编译确认）

| 成员 | 次数 | 5.8 | 例 |
|---|---|---|---|
| `GetTotal` | 12 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Digest.cpp:81 |
| `NextRow` | 9 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:73 |
| `EnumerateTimeline` | 7 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:444 |
| `ReadTimers` | 6 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:80 |
| `GetPercentiles` | 4 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Digest.cpp:90 |
| `GetCount` | 4 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Digest.cpp:93 |
| `GetAvg` | 4 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Digest.cpp:95 |
| `GetMax` | 4 | ❌ 不存在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Digest.cpp:99 |
| `Num` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Allocations.cpp:203 |
| `GetRowCount` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:63 |
| `CreateReader` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:69 |
| `IsValid` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:70 |
| `GetCurrentRow` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Asset.cpp:74 |
| `EnumerateTimelines` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:69 |
| `ReadTimeline` | 3 | ⚠️ 名字在 | ue-program/TraceDigest/Source/TraceDigest/Private/Modes/Compare.cpp:119 |
