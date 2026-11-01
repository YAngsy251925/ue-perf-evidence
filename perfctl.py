#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perfctl — ue-perf-evidence 命令行入口（CI 与 agent 共用）

  python perfctl.py export base   <trace> --out DIR [--threads GameThread,RenderThread 0] [--top 50]
  python perfctl.py export window <trace> --out DIR --start S --end E [--threads GameThread]
  python perfctl.py export probe  <trace> --out DIR [--gpu]
  python perfctl.py digest cpu    <stats.csv> --out DIR

所有命令都必须用 --out 指定输出目录（PLAN P7）。UE 根目录：export --ue-root 或环境变量 UE_ROOT。
"""
import sys

from pipeline.digest.views import cpu
from pipeline.extract import insights_export


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
        if rest[:1] != ["cpu"]:
            print("usage: perfctl digest cpu <stats.csv> --out DIR")
            return 2
        return cpu.main(rest[1:])
    print(f"unknown command: {group}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
