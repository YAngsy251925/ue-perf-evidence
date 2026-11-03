# provider 方法 × UE 5.8 声明（M4.1）

由 `python -X utf8 tools/api_signatures.py --mcp <ue-trace-mcp>` 生成。
只看 ue-trace-mcp 真正调到的方法；`两次以上` 的按调用次数排序。

| provider.method | 次数 | 5.8 声明（括号内为参数） | 弃用 |
|---|---|---|---|
| `ITimingProfilerProvider::ReadTimers` | 6 | virtual void ReadTimers(TFunctionRef<void(const ITimingProfilerTimerReader&)> Callback) const {} `Model/TimingProfiler.h:289` | ⚠️ Use GetTimerReader() instead |
| `ITimingProfilerProvider::EnumerateTimelines` | 3 | virtual void EnumerateTimelines(TFunctionRef<void(const Timeline&)> Callback) const = 0; `Model/TimingProfiler.h:283` |  |
| `ITimingProfilerProvider::ReadTimeline` | 3 | virtual bool ReadTimeline(uint32 Index, TFunctionRef<void(const Timeline&)> Callback) const = 0; `Model/TimingProfiler.h:281` |  |
| `ITimingProfilerProvider::GetCpuThreadTimelineIndex` | 2 | virtual bool GetCpuThreadTimelineIndex(uint32 ThreadId, uint32& OutTimelineIndex) const = 0; `Model/TimingProfiler.h:362` |  |
| `ITimingProfilerProvider::GetTimelineCount` | 2 | virtual uint32 GetTimelineCount() const = 0; `Model/TimingProfiler.h:282` |  |
| `ITimingProfilerProvider::CreateButterfly` | 1 | virtual ITimingProfilerButterfly* CreateButterfly(const FCreateButterflyParams& Params) const { return nullptr; } `Model/TimingProfiler.h:379` |  |
| `ITimingProfilerProvider::EnumerateGpuQueues` | 1 | virtual void EnumerateGpuQueues(TFunctionRef<void(const FGpuQueueInfo&)> Callback) const {} `Model/TimingProfiler.h:342` |  |
| `ITimingProfilerProvider::GetGpuTimelineIndex` | 1 | virtual bool GetGpuTimelineIndex(uint32& OutTimelineIndex) const { return false; } `Model/TimingProfiler.h:338` |  |
| `ITimingProfilerProvider::GetGpu2TimelineIndex` | 1 | virtual bool GetGpu2TimelineIndex(uint32& OutTimelineIndex) const { return false; } `Model/TimingProfiler.h:340` |  |
| `IAllocationsProvider::EnumerateTimeline` | 7 | EnumerateTimeline(ETimelineU64::MinTotalAllocatedMemory, StartIndex, EndIndex, Callback); `Model/AllocationsProvider.h:206` | ⚠️ Use EnumerateTimeline(ETimelineUInt64, ...) instead |
| `IAllocationsProvider::GetTimelineNumPoints` | 2 | virtual int32 GetTimelineNumPoints() const = 0; `Model/AllocationsProvider.h:196` |  |
| `IAllocationsProvider::GetTimelineIndexRange` | 2 | virtual void GetTimelineIndexRange(double StartTime, double EndTime, int32& StartIndex, int32& EndIndex) const = 0; `Model/AllocationsProvider.h:200` |  |
| `IMemoryProvider::EnumerateTags` | 2 | virtual void EnumerateTags(TFunctionRef<void(const FMemoryTagInfo&)> Callback) const = 0; `Model/Memory.h:106` |  |
| `IMemoryProvider::EnumerateTagSamples` | 1 | virtual void EnumerateTagSamples(FMemoryTrackerId Tracker, FMemoryTagId TagId, double StartTime, double EndTime, bool bIncludeRangeNeighbors, TFunctionRef<void( `Model/Memory.h:161` |  |
| `IMemoryProvider::GetTrackerCount` | 1 | virtual uint32 GetTrackerCount() const = 0; `Model/Memory.h:133` |  |
| `IMemoryProvider::GetTagSetCount` | 1 | virtual uint32 GetTagSetCount() const = 0; `Model/Memory.h:145` |  |
| `IModuleProvider::GetNumModules` | 1 | virtual uint32 GetNumModules() const = 0; `Model/Modules.h:195` |  |
| `IModuleProvider::HasFinishedResolving` | 1 | virtual bool HasFinishedResolving() const = 0; `Model/Modules.h:224` |  |
| `IModuleProvider::GetStats` | 1 | virtual void GetStats(FStats* OutStats) const = 0; `Model/Modules.h:221` |  |
| `IModuleProvider::EnumerateModules` | 1 | virtual void EnumerateModules(uint32 Start, TFunctionRef<void(const FModule& Module)> Callback) const = 0; `Model/Modules.h:203` |  |
| `ITasksProvider::GetNumTasks` | 1 | virtual int64 GetNumTasks() const = 0; `Model/TasksProfiler.h:128` |  |
| `ITasksProvider::EnumerateTasks` | 1 | virtual void EnumerateTasks(double StartTime, double EndTime, ETaskEnumerationOption EnumerationOption, TaskCallback Callback) const = 0; `Model/TasksProfiler.h:131` |  |
| `ITasksProvider::TryGetTask` | 1 | virtual const FTaskInfo* TryGetTask(uint32 ThreadId, double Timestamp) const = 0; `Model/TasksProfiler.h:117` |  |
| `IChannelProvider::GetChannels` | 2 | virtual const TArray<FChannelEntry>& GetChannels() const = 0; `Model/Channel.h:28` |  |
| `INetProfilerProvider::GetNetTraceVersion` | 2 | virtual uint32 GetNetTraceVersion() const = 0; `Model/NetProfiler.h:255` |  |
| `ICallstacksProvider::GetCallstack` | 1 | virtual const FCallstack* GetCallstack(uint32 CallstackId) const = 0; `Model/Callstack.h:83` |  |
