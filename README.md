# ue-perf-evidence

UE 5.8 上、面向 AI agent 闭环的性能证据管线。完整方案见 [docs/PLAN.md](docs/PLAN.md)。

当前进度：M1 已推送；**M2.1–M2.4 已完成**（采集 profile、条件对账、perf_capture、CSV 干净退出、hitch 通道与报告），待提交（M2 全部完成后一次性提交）。下一步 M2.5（Lyra hitch golden）与 M2.6（Lyra 码率实测）。

## 目录

| 路径 | 内容 |
|---|---|
| `perfctl.py` | 命令行入口 |
| `pipeline/extract/insights_export.py` | A 路：UnrealInsights 无头导出（base / window / probe） |
| `pipeline/digest/views/cpu.py` | digest v0：单 CSV 数字核心（v1 复用其读取与分桶） |
| `pipeline/digest/run_digest.py` | digest v1（PLAN §5.3）：manifest 闸门 → 稳态窗口导出（带缓存）→ 等待类单列 → 结论先行 |
| `pipeline/digest/diff.py` | perf_diff v0：两个 run 的 cpu 对比（|Δ| Top 差值、等待类差值、新增 / 消失）；任一侧不可测量即 not_comparable |
| `pipeline/capture/hitch_evidence.py` | hitch 证据收集：解析 dumphitches 日志块、发现并拷贝快照、probe 导出、写 hitches_index.json |
| `pipeline/digest/views/hitch.py` | hitch 视图（M2.3/M2.4）：hitches_index.json → 每次 hitch 的帧号 / 耗时 / 树骨架 / 快照 Top-N，根因下钻 |
| `host/TraceDigestProbe/` | B2：TraceServices commandlet 的宿主工程 |
| `harness/coexist_run.sh` | 采集进程纪律：后台启动、轮询日志标记（或时长模式：从“引擎就绪”起计时）、只结束自己启动的编辑器 |
| `harness/profiles/` | 采集 profile（M2.1）：smoke / measure；由 `perfctl profile prepare` 生成 launch.bat |
| `harness/experiments/` | 2026-11-01 的四组实验（A2 / B2 / C / D），唯一参数是 run 目录；历史实验带了 `file` 通道（P13），新采集用 profile |
| `pipeline/capture/` | 采集层：profile 校验与启动命令生成（profile.py）、perf_capture 一条命令（capture.py：prepare → 采集 → TraceDigestPerf 书签 → coverage.json） |
| `pipeline/caliber/` | 口径层：引擎日志与 Insights 日志解析、RHI 与 measurable 判定、桥接判定、帧序列加载、时间窗切分、GT / RT 帧口径与精确分位数、采集条件对账（conditions.py）、manifest |
| `pipeline/extract/uecsv.py` | UE 导出 CSV 的统一读取纪律（P10：field_size_limit、尾部行、精确元数据标记） |
| `golden/` | 口径回归基线（golden.json + 说明）；重放见 tests/test_golden.py，生成见 tests/make_golden.py |
| `tests/` | 单元测试与验收测试（验收数据不在本机时自动跳过）；121 个 |
| `tools/check_text.py` | 编码检查（PLAN §7.4）；`--selftest` 自检，`--staged` 供 pre-commit 使用 |
| `.githooks/pre-commit` | 提交前检查暂存内容的编码 |
| `runs/` | 采集与分析输出（不进 git） |

## 用法

所有命令都必须指定输出目录（PLAN P7），不会写当前目录。

```bash
# A 路导出
python perfctl.py export base   <trace.utrace> --out <dir> [--threads "GameThread,RenderThread 0"] [--top 50]
python perfctl.py export window <trace.utrace> --out <dir> --start 9.75 --end 12.52 [--threads GameThread]
python perfctl.py export probe  <trace.utrace> --out <dir> [--gpu]

# digest（CPU 视图）
#   run 目录 → digest v1：读 manifest，measurable=false（如 nullrhi）直接返回 not_measurable；
#   否则按稳态窗口做一次窗口导出（约 2.2s，写 <run_dir>/digest_export/），等待类单列，结论先行
python perfctl.py digest cpu <run_dir> --out <dir> [--thread GameThread] [--trace FILE]
#   单个统计 CSV → digest v0 数字核心（无口径、无闸门）
python perfctl.py digest cpu <stats.csv> --out <dir>
python perfctl.py digest hitch <run_dir> --out <dir>   # hitch 视图（需先有 hitches_index.json）

# manifest v0（PLAN §5.2）：默认读 <run_dir>/engine.log、<run_dir>/extract，写 <run_dir>/manifest.json；
# extract 缺失但有 <run_dir>/trace.utrace 时会自动先跑 probe 导出。
# 时间窗：startup / frames / warmup / steady / shutdown，steady 是默认统计窗口（成帧段去掉前 60 帧）
python perfctl.py manifest <run_dir> [--log FILE] [--extract DIR] [--trace FILE] [--out FILE] [--warmup-frames 60]
# run 目录里有 capture_profile.json 时，manifest 会把 profile 的每条声明和日志 / trace 证据对账（conditions 块）

# 采集 profile（M2.1，harness/profiles/）：smoke（nullrhi，只验证链路）/ measure（真实 RHI + §7.2 固定条件）
python perfctl.py profile list
python perfctl.py profile show measure
python perfctl.py profile prepare measure runs/<run_id>      # 写 launch.bat + capture_profile.json，并打印下一步命令
# 或一条命令（M2.2）：measure 用 CSV 事件对开停，引擎在 stop_at_s 后写完 CSV 自己退出（P15）；会弹出 960x540 窗口约 60 秒
python perfctl.py capture measure runs/<run_id>
python perfctl.py manifest runs/<run_id>

# diff（cpu 对比）：每组一个 run 时只报数值；每组 ≥5 个 run 时按 R9 判定（噪声门 CV<3%、Welch 阈值、MDE）
python perfctl.py diff cpu <run_a> <run_b> --out <dir> [--trace-a FILE] [--trace-b FILE]
python perfctl.py diff cpu -a R1 R2 R3 R4 R5 -b S1 S2 S3 S4 S5 --out <dir>    # 组形态（M3.3）
python perfctl.py diff cpu -a ... -b ... --out <dir> --significance-only       # 只读 manifest 的 p50，CI 门槛

# 内存（M6）：MemReport 视图与分配导出
python perfctl.py capture harness/profiles/memory.json runs/<id>     # memreport 落在 probe 段，p99 不受影响
python perfctl.py digest memory runs/<id> --out runs/<id>/mem
python perfctl.py digest allocs runs/<id> --out runs/<id>/allocs --rule aAf --max 20000   # 需要 trace 带 memory 通道

# 配置快照（M5.5）：cvar（ExecCmds + LogConfig 两个源）与 scalability 档位，对账 profile 声明
python perfctl.py digest config runs/<id> --out runs/<id>/config

# 单帧 scope 树（M5.4）：B2 读第 N 帧的整棵树 + 自耗时 + 名字覆盖率
python perfctl.py digest single_frame runs/<id> --frame 700 --out runs/<id>/frame700 --b2

# PresentMon（M5.3）：capture 会自动起停并写 <run_dir>/presentmon/；也可单独跑
python -m pipeline.capture.presentmon runs/x/pm.csv --seconds 60
python -m pipeline.capture.presentmon --parse runs/x/pm.csv --process UnrealEditor-Cmd.exe

# 三路交叉校验（M5.2）：CSV Profiler ↔ trace 帧间隔 ↔ B2 GPU
# capture 会把 CSV Profiler 产物归档到 <run_dir>/csv/；历史 run 用 tools/backfill_csv.py 回填
python perfctl.py digest crosscheck runs/<id> --out runs/<id>/crosscheck --b2

# PerfreportTool（引擎自带，需 DOTNET_ROOT 指向引擎自带的 .NET 10）
# 注意 reportType 只有 all/default/memory/condensed/Default60fps/Default30fps/AI5/Memory
DOTNET_ROOT=D:/Epic/Games/UE_5.8/Engine/Binaries/ThirdParty/DotNet/10.0/win-x64   D:/Epic/Games/UE_5.8/Engine/Binaries/DotNET/CsvTools/PerfreportTool.exe   -csv <run_dir>/csv/<Profile>.csv -o <out_dir> -reportType Default60fps

# B2 深度读取（M4.2）：frames / timers / timerstats / callers / gpu / counters / regions /
# logs / channels / tasks / memory / allocations / net / load
# 传参用空格形态（P19），--out 给绝对路径
python perfctl.py b2 timerstats <trace.utrace> --out runs/dir --start 22.6 --end 54.9 --thread GameThread --limit 40
python perfctl.py b2 callers    <trace.utrace> --out runs/dir --start 22.6 --end 54.9 --thread GameThread --timer Frame
python perfctl.py b2 gpu        <trace.utrace> --out runs/dir --start 22.6 --end 54.9 --limit 10
python perfctl.py b2 regions    <trace.utrace> --out runs/dir --start 22.6 --end 54.9 --limit 15

# run 列表（M3.2，对应 §3 的 perf_runs）：先看有哪些 run、哪些可测量、稳态多长
python perfctl.py runs [--filter m3] [--json]

# MCP 通路（M3.2）：编辑器里先 exec "ModelContextProtocol.StartServer 18900"，再从外部客户端调工具
# 注意 call_tool 的 tool_name 只认裸名（PerfStartCapture），不是 describe_toolset 里的三段名
python -X utf8 tools/mcp_probe.py --url http://127.0.0.1:18900/mcp --call call_tool \
  '{"toolset_name":"PerfEvidenceToolset.PerfEvidenceToolset","tool_name":"PerfStartCapture","arguments":{"TracePath":"D:/x.utrace"}}'

# M3.4 闭环演示（A/B/C 各 5 次真实 RHI；约 45 分钟，连续弹窗）
# 注入端在 plugin/PerfEvidenceHost（装进目标工程即可用），演示驱动在 harness/m3_4_demo.py
python -X utf8 harness/m3_4_demo.py --n 5 --runs-dir runs/m3_4L2   --profile-a harness/profiles/lyra_measure_work.json   --profile-b harness/profiles/lyra_measure_regress.json   --uproject C:/UEProject/LyraStarterGame/LyraStarterGame.uproject
# 已有数据只想重出报告：加 --skip-capture

# 测试
python -X utf8 -m unittest discover -s tests -t .

# B2 commandlet（先编译一次 host 工程）
host/TraceDigestProbe/build_probe.bat
host/TraceDigestProbe/run_cmdlet.bat <trace.utrace> <log_file>

# 采集实验（Git Bash；D 会在桌面弹出 960x540 的游戏窗口）
bash harness/coexist_run.sh harness/experiments/A2_smoke_stats.bat runs/<run_id> 240
bash harness/coexist_run.sh harness/experiments/C_smoke_hitch.bat runs/<run_id> 240 "Thread Hitch" "Hitch detected on frame"
bash harness/coexist_run.sh harness/experiments/D_realrhi_csv.bat runs/<run_id> 300 "Capture Ended" "Capture Ended" 6
```

退出码：缺参数为 2；trace 不存在或导出产物缺失为 1。`coexist_run.sh` 拿到两个日志标记时返回 0，否则返回 1。

## 环境

- Windows + Git Bash；Python 3，只用标准库（已在 3.11 上验证）
- UE 5.8 安装版。根目录默认 `D:\Epic\Games\UE_5.8`，可用环境变量 `UE_ROOT` 覆盖；`export` 子命令也接受 `--ue-root`
- A2 / B2 / C 是 nullrhi 实验，只能验证链路，性能数字不可用（PLAN R11）
- Epic PerfreportTool（M5.2）：**不需要系统安装 .NET 10**。调用时设 `DOTNET_ROOT=<UE_ROOT>\Engine\Binaries\ThirdParty\DotNet\10.0\win-x64`，即可使用引擎自带的 10.0.7 运行时
- PresentMon（M5.3）：2.6.0 在 `D:\dsh-output\tools\presentmon\2.6.0\PresentMon-2.6.0-x64.exe`（不进 git；sha256 `b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af`，Intel 签名）。账户需要在 Performance Log Users 组里或以管理员运行，本机账户已在组内

## 编码规范（强制）

规则见 [docs/PLAN.md §7.4](docs/PLAN.md)，要点：

- 所有文本文件用 UTF-8、无 BOM；.bat 和路径只用 ASCII
- 写完文件后运行 `python -X utf8 tools/check_text.py`；`--selftest` 可以验证检查工具本身
- pre-commit 钩子会拒绝违规内容。新 clone 后执行一次 `git config core.hooksPath .githooks`

## 回归基线（M1.1，2026-11-01）

- 与实验目录 `D:\dsh-output\ue5-perf-lab` 的原产物逐字节比对：17 对全部一致（基础导出 6、窗口导出 1、帧探测 6、digest 4）
- B2 commandlet 在新位置输出 `DIGEST_OK duration=15.481s timers=10462`，无断言
- A2 / B2 实验经 harness 跑通；B2 的 trace 写入 run 目录，导出后得到 9,203 个 Game Frame
