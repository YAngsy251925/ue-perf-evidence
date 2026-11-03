#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_golden.py — （重新）生成 golden/golden.json（PLAN §10.2，M1.7）

用法（仓库根目录）：python -X utf8 -m tests.make_golden

需要本机有验收数据（tests/acceptance_data.ACCEPT 指向的目录与 trace）；生成是确定性重算
（窗口导出走 prepare_rows 缓存）。golden.json 进 git，是口径回归防线——重新生成前先看
tests/test_golden 失败的具体字段，确认是预期变化（引擎升级 / 导出器变化 / 口径规则修改）再覆盖。
"""
import json
import os
import sys

from tests import acceptance_data

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    if not acceptance_data.have_acceptance_data():
        print("[error] acceptance data missing (see tests/acceptance_data.py)")
        return 1
    golden = acceptance_data.collect_golden()
    out = os.path.join(REPO, "golden", "golden.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(golden, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("[ok] %s (runs=%d, digests=%d, diffs=%d)" % (
        out, len(golden["runs"]), len(golden["digests"]), len(golden["diffs"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
