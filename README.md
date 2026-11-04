# ue-perf-evidence

UE 5.8 上、面向 AI agent 的**性能证据管线**：从一次采集（trace + 日志 + CSV + PresentMon + MemReport + 采样器）
推出一句可复核的结论，并能把结论逐行下钻回原始证据。

> 规格唯一权威：[docs/PLAN.md](docs/PLAN.md)（含 R1–R15 口径规则、P1–P21 协议、M0.5–M6 里程碑）
> 交接：[docs/HANDOFF.md](docs/HANDOFF.md) ｜ 变更：[CHANGELOG.md](CHANGELOG.md)

**状态**：M0.5–M6 全部完成（2026-11-05，`v0.7.0`），304 单测 + 口径 golden 回归全绿。

## 它回答什么问题

| 问题 | 命令 | 结论形态 |
|---|---|---|
| 谁烧 CPU、谁调的 | `perfctl digest cpu <run> --out DIR` | 自耗时 Top-N（等待单列）+ 确定性结论 |
| 帧时间可信吗、谁在卡 | `perfctl digest bound <run> --out DIR [--b2] [--sampler]` | 四线 busy/occupancy + 置信度 + 预算分支 |
| 卡的那一帧怎么了 | `perfctl digest hitch <run> --out DIR` | 每次 hitch 的树骨架 + 快照 |
| 哪个 pass 贵、是否 GPU-bound | `perfctl digest gpu <run> --out DIR` | pass 级计时 + 队列 / fence |
| 某帧的层级、某 scope 单次超长 | `perfctl digest single_frame <run> --frame N --out DIR` | 整帧 scope 树 + 自耗时 |
| 内存谁占 | `perfctl digest memory <run> --out DIR`、`digest allocs` | MemReport 结构化 + 分配 Tag/Class/Package Top-N |
| 加载慢在哪个包 | `perfctl digest load <run> --out DIR --b2` | 包级 Top-N（count/total/max/avg） |
| 是不是配置问题 | `perfctl digest config <run> --out DIR` | cvar（双证据源）+ scalability + 声明对账 |
| **在哪儿优化、预期多少** | `perfctl advise <run> --out DIR` | 证据驱动的假设：份额/饱和/fence + 本机引擎 cvar（含 file:line）+ 预期收益 + 证伪条件 |
| 变快了还是变慢了 | `perfctl diff cpu -a … -b … --out DIR [--fail-on regression]` | R9：n / CV / 阈值 / MDE / 退出码 |
| 那条结论的原始行在哪 | `perfctl evidence <run> --ref "timer:Name@stats｜frame:N｜hitch:K"` | 分页证据行（带翻页提示） |
| 引擎自报 vs 我们推导 | `perfctl digest bound <run> --sampler` | 采样器校准段（GT/RT/GPU 偏差） |

## 零摩擦：分析"最新那条 utrace"（推荐日常用法）

录完 trace 什么都不用打开，直接：

```bash
python perfctl.py auto --project C:/UEProject/LyraStarterGame      # 找最新 .utrace → ingest → cpu+bound → 摘要
python perfctl.py auto --project … --views cpu,bound,gpu,config --b2   # 多跑几个视图（--b2 才有 GPU 线）
```

它自动在 `<Project>/Saved/Profiling/**` 里挑 mtime 最新、且已写完（默认 ≥10 秒前）的 `.utrace`，
顺手匹配时间最接近的 `Saved/Logs/*.log`，然后：ingest → manifest → 跑视图 → 写
`runs/auto-<trace 时间戳>/auto/summary.md`。`--json` 会多打一行 `PERF_AUTO_JSON {...}` 给 agent 解析。

在编辑器里（AI Assistant / MCP）等价工具是 **`PerfAnalyzeLatest(ProjectPath, Views, bWithB2)`**，
它直接把摘要文本返回给 agent —— 用户只要说"帮我分析最新的 utrace"。

## 快速开始

采集有三条通路，任选一条（详见 [docs/PLAN.md](docs/PLAN.md) §3）：

```bash
# ① 编辑器里 PIE / Insights 已经录好了 → 只要一份 .utrace（+日志）就能进管线
python perfctl.py ingest --trace "<Project>/Saved/Profiling/xxx.utrace"        --log "<Project>/Saved/Logs/<Project>.log" --run runs/pie1 --mode pie

# ② 打包后的包（同一套 profile 驱动 Game.exe；不传 uproject、不加 -game）
python perfctl.py capture harness/profiles/measure.json runs/pkg1        --exe "<Pkg>/Windows/<Game>.exe"

# ③ 编辑器二进制 -game（最可复现的基线，自带 CSV/采样器全集；会弹 960×540 窗口）
python perfctl.py capture harness/profiles/measure.json runs/r1 --uproject <path>.uproject

# 1) 口径与能力（必须先跑，后面的视图都读它）
python perfctl.py manifest runs/r1

# 2) 结论
python perfctl.py digest cpu   runs/r1 --out runs/r1/cpu
python perfctl.py digest bound runs/r1 --out runs/r1/bound --b2 --sampler

# 3) 下钻到行
python perfctl.py evidence runs/r1 --ref "timer:PerfEvidenceRegress@stats" --out runs/r1/ev --b2
```

所有命令都必须带 `--out`（PLAN P7），不会写当前目录；`runs/` 不进 git（trace 太大）。

## CI 闸门（R9 落成退出码）

```bash
# 只读 manifest 的 p50，秒级、无需 GPU；瘦包 10 个 run 约 90KB
python tools/pack_manifests.py --out bundle/ --root runs --runs A1 A2 A3 A4 A5 B1 B2 B3 B4 B5
python perfctl.py diff cpu -a bundle/A1 … -b bundle/B1 … --significance-only \
        --fail-on regression --json-line --out artifacts/perf-gate
```

退出码：`0` 通过 / `3` 变慢或超预算 / `4` 显著改善（`--fail-on significant`）/ `2` 数据不足（`--strict`）。
workflow 模板在 [docs/ci/perf-gate.yml](docs/ci/perf-gate.yml)（**本仓库 token 无 workflow scope**，用时复制到 `.github/workflows/`）。

## 口径要点（完整见 PLAN §9）

- 只有 measure run 能下性能结论；`nullrhi` / smoke 只验链路（R1、R2）。
- 显著性用 R9：每组 n≥5、组内 CV<3% 噪声门、阈值 `max(0.5ms, t·√(sa²/na+sb²/nb))`，附 MDE80；
  不合格只报数值，不下"变快/变慢"结论。
- busy = 帧间隔均值 − **停顿**（`stall`）；"等并行任务"（`overlap`）不扣但单独报，另有旧口径对照列
  —— 这是 M5.1b 采样器校准出来的（GT +23.4%→+11.2%，RT +41.6%→+10.8%）。
- 多条线都饱和时 busy 无分辨力，改按**自耗时**排序（`saturated_tie`）。
- 重操作（MemReport / CSV 探针）必须落在 `coverage_probe` 段，统计窗口的 p99 才不受影响（R7/P15）。

## 目录

| 路径 | 内容 |
|---|---|
| `perfctl.py` | 唯一 CLI 入口（capture / manifest / profile / digest / diff / runs / b2 / export / evidence / session-drift） |
| `pipeline/capture/` | profile 校验与启动命令、一条命令采集、PresentMon、MemReport / 采样器归档 |
| `pipeline/extract/` | A 路 Insights 无头导出、B2 CLI 封装、memreport 解析、UE CSV 读取纪律 |
| `pipeline/caliber/` | 引擎 / Insights 日志解析、RHI 与 measurable 判定、帧序列、窗口切分、条件对账、manifest |
| `pipeline/digest/views/` | cpu / hitch / gpu / bound / crosscheck / single_frame / config / memory / allocs / load / sampler / evidence |
| `host/TraceDigestProbe/` | B2：TraceServices commandlet 宿主工程（16 个 mode） |
| `plugin/PerfEvidenceHost/` | 游戏内插件：CSV 事件、burner、ExecAt / ExecAt2、LoadTime 探针、每帧采样器 |
| `plugin/PerfEvidenceToolset/` | MCP / AI Assistant 工具集（capture / report / evidence） |
| `harness/profiles/` | 采集 profile（smoke / measure / hitch-hunt / memory / measure_sampler / lyra_*） |
| `golden/` | 口径回归基线（重放 `tests/test_golden.py`，生成 `tests/make_golden.py`） |
| `tests/` | 304 条单测与验收测试（验收数据不在本机时自动跳过） |
| `tools/` | `check_text.py`（编码检查）、`pack_manifests.py`（CI 瘦包）、`scan_cvars.py`（扫本机引擎生成杠杆目录）、`session_cost.py`（token 成本）、`mcp_probe.py`、`backfill_csv.py` |
| `pipeline/advice/` | 建议层：证据 → 可调开关 → 预期收益 → 证伪条件（见 [docs/ADVISOR.md](docs/ADVISOR.md)） |

## 已知限制

- 采集要 UE + 真实 RHI：CI 里只能做"拿已有 run 做判定闸门"，不能在托管 runner 上跑引擎。
- PresentMon 与 trace 是**生命周期级**对齐（不逐帧）；采样器按"结尾对齐取窗口帧数"（±1 帧）。
- Lyra 的 Shooter / GameFeature 地图在 `-game` 下会断言崩溃（`OtherPin`），实验用非 GameFeature 地图。
- `mode=tasks` 需要 trace 带 `task` 通道；`mode=memory` / `allocations` 需要 `memory` 通道。
- Shipping 包里 MemReport、snapshothitches 不可用，manifest 会标 `memreport: false`。
