#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perfctl — ue-perf-evidence 命令行入口（CI 与 agent 共用）

  python perfctl.py export base   <trace> --out DIR [--threads GameThread,RenderThread 0] [--top 50]
  python perfctl.py export window <trace> --out DIR --start S --end E [--threads GameThread]
  python perfctl.py export probe  <trace> --out DIR [--gpu]
  python perfctl.py digest cpu    <run_dir> --out DIR [--thread GameThread] [--b2]  # run 级 digest v1（§5.3；--b2=M4.3 下钻）
  python perfctl.py digest cpu    <stats.csv> --out DIR                          # v0：单 CSV 数字核心
  python perfctl.py digest gpu    <run_dir> --out DIR [--b2]                     # GPU 视图 v0（M4.0/M4.3）
  python perfctl.py digest bound  <run_dir> --out DIR [--b2] [--target-fps N]    # bound 判定（M5.1）
  python perfctl.py digest crosscheck <run_dir> --out DIR [--b2] [--window coverage|stats]  # CSV/trace/B2 三路校验（M5.2）
  python perfctl.py digest hitch  <run_dir> --out DIR                            # hitch 视图（M2.3/M2.4）
  python perfctl.py diff cpu      <run_a> <run_b> --out DIR [--trace-a F] [--trace-b F]  # v0（M1.6）
  python perfctl.py manifest      <run_dir> [--log FILE] [--extract DIR] [--trace FILE] [--out FILE]
  python perfctl.py profile list | show <name> | prepare <name> <run_dir> [--map M]       # 采集 profile（M2.1）
  python perfctl.py capture <profile> <run_dir> [--map M]                                  # 一条命令采集（M2.2）
  python perfctl.py session-drift [--segments 6] [--duration 20]                            # M3.0 常驻会话实测
  python perfctl.py runs          [--root DIR] [--filter S] [--limit N] [--json]            # M3.2 run 列表（perf_runs）
  python perfctl.py b2 frames|timers <trace> --out DIR [--limit N]                          # M4.2 B2 深度读取

export / digest 必须用 --out 指定输出目录（PLAN P7）；manifest 写在 run 目录里（或 --out）。
UE 根目录：--ue-root 或环境变量 UE_ROOT。
"""
import os
import sys

from pipeline.capture import capture, profile, session
from pipeline.caliber import manifest
from pipeline.digest import diff, run_digest
from pipeline.digest.views import bound, cpu, crosscheck, gpu, hitch
from pipeline.digest import runs
from pipeline.extract import b2, insights_export


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    group, rest = argv[0], argv[1:]
    if group == "export":
        return insights_export.main(rest)
    if group == "digest":
        if rest[:1] == ["hitch"]:
            return hitch.main(rest[1:])
        if rest[:1] == ["gpu"]:
            return gpu.main(rest[1:])
        if rest[:1] == ["bound"]:
            return bound.main(rest[1:])
        if rest[:1] == ["crosscheck"]:
            return crosscheck.main(rest[1:])
        if rest[:1] != ["cpu"]:
            print("usage: perfctl digest cpu <run_dir|stats.csv> --out DIR\n"
                  "       perfctl digest gpu <run_dir> --out DIR [--b2]\n"
                  "       perfctl digest bound <run_dir> --out DIR [--b2] [--target-fps N]\n"
                  "       perfctl digest crosscheck <run_dir> --out DIR [--b2]")
            return 2
        rest = rest[1:]
        # 第一个非选项参数是输入：目录 → run 级 v1；CSV 文件 → v0 数字核心
        target = next((a for a in rest if not a.startswith("-") and not a.startswith("--")), None)
        if target is None:
            print("usage: perfctl digest cpu <run_dir|stats.csv> --out DIR")
            return 2
        if os.path.isdir(target):
            return run_digest.main(rest)
        return cpu.main(rest)
    if group == "diff":
        if rest[:1] != ["cpu"]:
            print("usage: perfctl diff cpu <run_a> <run_b> --out DIR")
            return 2
        rest = rest[1:]
        pos = [a for a in rest if not a.startswith("-")]
        if len(pos) < 2:
            print("usage: perfctl diff cpu <run_a> <run_b> --out DIR")
            return 2
        if not all(os.path.isdir(p) for p in pos[:2]):
            print("usage: perfctl diff cpu <run_a> <run_b> --out DIR（两个参数都必须是 run 目录）")
            return 2
        return diff.main(rest)
    if group == "manifest":
        return manifest.main(rest)
    if group == "profile":
        return profile.main(rest)
    if group == "capture":
        if len([a for a in rest if not a.startswith("-")]) < 2:
            print("usage: perfctl capture <profile> <run_dir> [--map M]")
            return 2
        return capture.main(rest)
    if group == "session-drift":
        return session.main(rest)
    if group == "runs":
        return runs.main(rest)
    if group == "b2":
        return b2.main(rest)
    print(f"unknown command: {group}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
