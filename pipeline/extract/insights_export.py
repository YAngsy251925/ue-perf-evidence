#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""insights_export.py — A 路：UnrealInsights 无头导出

合并原 utrace_extract.py / window_export.py / probe_export.py（实验目录 ue5-perf-lab）。
落实 PLAN v1.1：
  P5  rsp 形式、绝对路径、文件名消毒、产物存在校验（缺产物退出码 1）
  P7  --out 必填，不写当前目录
UE 根目录：--ue-root > 环境变量 UE_ROOT > 默认 D:\\Epic\\Games\\UE_5.8

子命令：
  base     threads / timers / 全量与分线程统计
  window   某时间窗的分线程统计（-startTime/-endTime 单位是秒，trace 时间轴）
  probe    线程表 + timer 表 + 帧轨道 + Frame scope + GT 统计（R4 帧轨道探测的数据源）

导出入参语义（实测）：
  -threads=  逗号分隔 + * 通配（GameThread / RenderThread 0 / GPU1）
  -timers=   逗号分隔 + * 通配；ExportTimerCallees 需要能解析到 timer 名，写 * 会得到空表
  -sortBy=   仅支持 TotalInclusiveTime（自耗时排序在下游做）
  -ExecOnAnalysisCompleteCmd 必须用 @=rsp；内联写法在 5.8 静默不执行
"""
import argparse
import os
import re
import subprocess
import sys

DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"
EVENT_COLUMNS = "-columns=ThreadName,TimerName,StartTime,EndTime,Duration,Depth"


def insights_exe(ue_root=None) -> str:
    root = ue_root or os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT
    exe = os.path.join(root, "Engine", "Binaries", "Win64", "UnrealInsights.exe")
    if not os.path.isfile(exe):
        sys.exit(f"[error] UnrealInsights.exe not found: {exe} (use --ue-root or UE_ROOT)")
    return exe


def run_insights(trace: str, outdir: str, commands, ue_root=None, timeout=1800) -> str:
    trace = os.path.abspath(trace)
    outdir = os.path.abspath(outdir)
    if not os.path.isfile(trace):
        sys.exit(f"[error] trace not found: {trace}")
    os.makedirs(outdir, exist_ok=True)
    rsp = os.path.join(outdir, "export.rsp")
    with open(rsp, "w", encoding="utf-8") as f:
        f.write("\n".join(commands) + "\n")
    # 注意 -ExecOnAnalysisCompleteCmd=@=path 这一写法：等号后是 @= + rsp 路径
    log_path = os.path.join(outdir, "insights.log")
    if os.path.isfile(log_path):
        os.remove(log_path)  # 清掉旧日志，避免 Insights 启动时把它轮转成 insights-backup-*.log 堆积
    args = [
        f'"{insights_exe(ue_root)}"',
        f'-OpenTraceFile="{trace}"',
        "-AutoQuit",
        "-NoUI",
        f"-ExecOnAnalysisCompleteCmd=@={rsp}",
        f'-ABSLOG="{log_path}"',
        "-log",
    ]
    subprocess.run(" ".join(args), timeout=timeout)
    return os.path.join(outdir, "insights.log")


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def out_path(outdir: str, name: str) -> str:
    return os.path.join(os.path.abspath(outdir), name).replace("\\", "/")


def base_commands(outdir: str, threads, top: int):
    names = ["threads.csv", "timers.csv", "all_stats.csv"]
    cmds = [
        f"TimingInsights.ExportThreads {out_path(outdir, 'threads.csv')}",
        f"TimingInsights.ExportTimers {out_path(outdir, 'timers.csv')}",
        # 全量统计，按 inclusive 降序取 top 200（自耗时排序下游做）
        f"TimingInsights.ExportTimerStatistics {out_path(outdir, 'all_stats.csv')} -columns=* "
        f"-sortBy=TotalInclusiveTime -sortOrder=Descending -maxTimerCount=200",
    ]
    for t in threads:
        name = f"stats_{safe_name(t)}.csv"
        names.append(name)
        cmds.append(
            f"TimingInsights.ExportTimerStatistics {out_path(outdir, name)} -threads=\"{t}\" "
            f"-columns=* -sortBy=TotalInclusiveTime -maxTimerCount={top}"
        )
    return cmds, names


def window_commands(outdir: str, threads, top: int, start: str, end: str):
    names, cmds = [], []
    for t in threads:
        name = f"stats_{safe_name(t)}_window.csv"
        names.append(name)
        cmds.append(
            f'TimingInsights.ExportTimerStatistics {out_path(outdir, name)} -threads="{t}" -columns=* '
            f"-sortBy=TotalInclusiveTime -maxTimerCount={top} -startTime={start} -endTime={end}"
        )
    return cmds, names


def probe_commands(outdir: str, gpu: bool, top: int):
    names = ["threads.csv", "timers.csv", "frame_tracks.csv", "frame_scopes.csv", "gt_stats.csv"]
    cmds = [
        f"TimingInsights.ExportThreads {out_path(outdir, 'threads.csv')}",
        f"TimingInsights.ExportTimers {out_path(outdir, 'timers.csv')}",
        f'TimingInsights.ExportTimingEvents {out_path(outdir, "frame_tracks.csv")} {EVENT_COLUMNS} '
        f'-threads="Game Frames,Rendering Frames"',
        # Frame scope 必须下游按线程过滤：真实 RHI 下 RHIThread 上也有大量 "Frame"（R4）
        f'TimingInsights.ExportTimingEvents {out_path(outdir, "frame_scopes.csv")} {EVENT_COLUMNS} -timers="Frame"',
        f'TimingInsights.ExportTimerStatistics {out_path(outdir, "gt_stats.csv")} -threads="GameThread" -columns=* '
        f"-sortBy=TotalInclusiveTime -maxTimerCount=60",
    ]
    if gpu:
        names.append("gpu1_stats.csv")
        cmds.append(
            f'TimingInsights.ExportTimerStatistics {out_path(outdir, "gpu1_stats.csv")} -threads="GPU1" '
            f"-columns=* -sortBy=TotalInclusiveTime -maxTimerCount={top}"
        )
    return cmds, names


def report(outdir: str, names) -> int:
    """P5：逐个校验产物；返回缺失数。"""
    missing = 0
    for n in names:
        p = os.path.join(outdir, n)
        if not os.path.isfile(p):
            missing += 1
            print(f"  [MISSING] {n}")
            continue
        with open(p, encoding="utf-8-sig", errors="ignore") as f:
            rows = sum(1 for _ in f) - 1
        print(f"  {n:<24} {os.path.getsize(p) / 1024:8.1f} KiB  {rows:>7} rows")
    return missing


def split_list(value: str):
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser(ap=None):
    ap = ap or argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ue-root", default=None, help="UE 根目录（默认读 UE_ROOT，再退回 D:\\Epic\\Games\\UE_5.8）")
    sub = ap.add_subparsers(dest="kind", required=True)

    p = sub.add_parser("base", help="threads / timers / 全量与分线程统计")
    p.add_argument("trace")
    p.add_argument("--out", required=True, help="输出目录（必填）")
    p.add_argument("--threads", default="GameThread,RenderThread 0")
    p.add_argument("--top", type=int, default=50)

    p = sub.add_parser("window", help="时间窗分线程统计")
    p.add_argument("trace")
    p.add_argument("--out", required=True, help="输出目录（必填）")
    p.add_argument("--start", required=True, help="窗口起点，秒")
    p.add_argument("--end", required=True, help="窗口终点，秒")
    p.add_argument("--threads", default="GameThread")
    p.add_argument("--top", type=int, default=50)

    p = sub.add_parser("probe", help="帧轨道探测导出（R4）")
    p.add_argument("trace")
    p.add_argument("--out", required=True, help="输出目录（必填）")
    p.add_argument("--gpu", action="store_true", help="同时导出 GPU1 统计")
    p.add_argument("--top", type=int, default=15, help="GPU1 统计条数")
    return ap


def run(a) -> int:
    if a.kind == "base":
        cmds, names = base_commands(a.out, split_list(a.threads), a.top)
    elif a.kind == "window":
        if float(a.end) <= float(a.start):
            sys.exit("[error] --end must be greater than --start")
        cmds, names = window_commands(a.out, split_list(a.threads), a.top, a.start, a.end)
    else:
        cmds, names = probe_commands(a.out, a.gpu, a.top)
    log = run_insights(a.trace, a.out, cmds, ue_root=a.ue_root)
    print(f"[insights] {log}")
    missing = report(os.path.abspath(a.out), names)
    if missing:
        print(f"[error] {missing} expected file(s) missing")
        return 1
    print(f"[ok] {len(names)} file(s) in {os.path.abspath(a.out)}")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
