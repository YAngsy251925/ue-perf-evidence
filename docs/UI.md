# UI 成本的逐控件分析（M10.2）

两层证据，缺一不可：

| 层 | 数据 | 回答什么 | 命令 |
|---|---|---|---|
| **运行时间**（trace） | 引擎原生 `SlateTrace` 通道 → `WidgetUpdated`（每次更新耗时）+ `WidgetInfo`（WidgetId → Path） | 哪个控件贵、为什么重绘 | `perfctl b2 widgets <trace> --out DIR --start S --end S` |
| **资产结构**（编辑器） | `UWidgetBlueprint` 的控件树 / 属性绑定 / Ubergraph 节点 | 布局怎么搭的、逻辑在哪、绑定函数是谁 | 在编辑器里跑 `tools/dump_umg.py` |

## 1. 采集：必须带 `slate` 通道

```bash
perfctl capture harness/profiles/slate_probe.json runs/ui1   # trace.channels 里含 slate
```

`slate` = 引擎的 `SlateChannel`（`Source/Runtime/SlateCore/Private/Trace/SlateTrace.cpp`），
它写入的事件是：`ApplicationTickAndDrawWidgets / AddWidget / WidgetInfo / RemoveWidget /
WidgetUpdated / WidgetInvalidated / RootInvalidated`。

**不带这个通道，trace 里只有 `Slate::Prepass` 这种 pass 级计时，看不到控件。**
我们早先用 `measure` profile 采的 trace 就是这种情况（这也是为什么当时只能说"UI 占 47%"）。

## 2. 读逐控件耗时

```bash
perfctl b2 widgets runs/ui1/trace.utrace --out runs/ui1/w --start 0 --end 60 --limit 20
```

输出（实测，宿主工程 60 秒窗口）：

```
[b2] widgets: 更新事件 44645，控件 4 个（Slate 通道）
   GameEngine.cpp(674)                                   688.053 ms   1.15%  updates=11162
   GameEngine.cpp(674)/SWindow.cpp(659)/SWindow.cpp(543)  199.266 ms   0.33%  updates=11161
   .../SGameLayerManager.cpp(114)/...                     144.084 ms   0.24%  updates=11161
```

`share_pct` 是该控件在这段时间里的占用比例；`updates` 是更新次数；`max_ms` 是单次最坏值
（在 JSON 里）。路径来自 `FReflectionMetaData::GetWidgetPath`：纯 C++ 控件显示为构造位置，
**UMG 控件会带 blueprint 路径**，从而能和 `dump_umg.py` 的资产/控件名对齐。

## 3. 读资产结构（编辑器里）

```bash
# 命令行（headless，同样跑编辑器 Python）
set UMG_PREFIX=/Game/UI
set UMG_LIMIT=25
set UMG_OUT=D:/out/umg.json
UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<repo>/tools/dump_umg.py" \
    -unattended -nosplash -nullrhi
```

每个控件的产物：`depth / name / class / visibility`；每个资产还有
`bindings[]`（object / property / function）与 `graph{}`（节点类名直方图 + 事件入口）。

**为什么由 C++ 导出而不是纯 Python**：`UWidgetBlueprint::WidgetTree`、`Bindings`、
`UBlueprint::UbergraphPages` 在 Python 里是 protected（实测报
`Property 'WidgetTree' ... is protected and cannot be read`），所以由
`UPerfEvidenceUILibrary`（PerfEvidenceHost 插件的 `#if WITH_EDITOR` 部分）暴露
`DumpWidgetBlueprint(path)` / `ListWidgetBlueprints(prefix, max)`，Python 只做遍历与落盘。

## 4. 两边怎么对齐

- 控件：trace 的 `path` 里含 `WidgetBlueprintGeneratedClass` 名（如 `W_Crosshair_C`）与控件名 →
  与 dump 的 `assets[].path` / `widgets[].name` 对齐；
- 函数：`WidgetInvalidated.ScriptTrace`（失效时的脚本栈）与 dump 的 `bindings[].function` /
  `graph.events` 对齐 —— 这是"每帧重绘是谁触发的"的直接证据。

## 5. 实测发现（Lyra，2026-11-05）

- `/Game/UI` 下 25 个 WidgetBlueprint：控件树能完整导出（最大 25 个控件，如 `W_SafeZoneEditor`）；
- **`bindings` 全为 0**：Lyra 的 UI 不走"属性绑定"，而是走事件 / 视图模型。这意味着
  "每帧重绘"的嫌疑要落到 **Tick / 事件驱动 / 动画** 上，而不是绑定函数 —— 这条结论本身就很有价值
  （很多团队的 UI 性能优化清单第一条就是"删属性绑定"，在 Lyra 上不适用）。
- 节点数分布：`W_ActionTouchButton` 92 个节点、`W_Credits` 15 个 —— 结构复杂度可量化。

## 6. 已知边界

- `WidgetInvalidated` 的 `reason` 是位标志（`EInvalidateWidgetReason`），我们目前只统计分布，
  还没翻译成"文本变了/布局变了/子级变了"的语义；
- `dump_umg.py` 只读结构，不读 K2 节点的**连线与常量**（要做需要更深的 BlueprintEditorLibrary 调用）；
- 逐控件耗时是**Slate 更新**的耗时，不含 RHI 绘制本身（后者看 `digest gpu` 的 SlateUI pass）。
