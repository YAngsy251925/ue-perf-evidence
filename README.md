# ue-perf-evidence

安装版 UE 5.8 上、面向 AI agent 闭环的性能证据管线。完整方案见 [docs/PLAN.md](docs/PLAN.md)。

当前进度：M1.1（仓库与迁入）已完成；下一步 M1.2（manifest v0）。

## 目录

| 路径 | 内容 |
|---|---|
| `perfctl.py` | 命令行入口 |
| `pipeline/extract/insights_export.py` | A 路：UnrealInsights 无头导出（base / window / probe） |
| `pipeline/digest/views/cpu.py` | CPU 自耗时视图（digest v0） |
| `host/TraceDigestProbe/` | B2：TraceServices commandlet 的宿主工程 |
| `harness/coexist_run.sh` | 采集进程纪律：后台启动、轮询日志标记、只结束自己启动的编辑器 |
| `harness/experiments/` | 2026-11-01 的四组实验（A2 / B2 / C / D），唯一参数是 run 目录 |
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

# digest（CPU 自耗时视图）
python perfctl.py digest cpu <stats.csv> --out <dir>

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

## 编码规范（强制）

规则见 [docs/PLAN.md §7.4](docs/PLAN.md)，要点：

- 所有文本文件用 UTF-8、无 BOM；.bat 和路径只用 ASCII
- 写完文件后运行 `python -X utf8 tools/check_text.py`；`--selftest` 可以验证检查工具本身
- pre-commit 钩子会拒绝违规内容。新 clone 后执行一次 `git config core.hooksPath .githooks`

## 回归基线（M1.1，2026-11-01）

- 与实验目录 `D:\dsh-output\ue5-perf-lab` 的原产物逐字节比对：17 对全部一致（基础导出 6、窗口导出 1、帧探测 6、digest 4）
- B2 commandlet 在新位置输出 `DIGEST_OK duration=15.481s timers=10462`，无断言
- A2 / B2 实验经 harness 跑通；B2 的 trace 写入 run 目录，导出后得到 9,203 个 Game Frame
