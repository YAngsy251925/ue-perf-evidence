#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_deprecated.py — 列出 UE 5.8 TraceServices 里带 UE_DEPRECATED 的成员与替代建议。

M4.1 的补充证据：ue-trace-mcp 按名字都还能找到符号，但 5.8 有一批成员被标记弃用
（编译会出 C4996，有的直接编译失败）。这个脚本把"弃用 + 官方建议替代"抽成清单。
"""
import io
import os
import re
import sys

DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"
BASE = os.path.join("Engine", "Source", "Developer", "TraceServices", "Public", "TraceServices")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ue_root = (argv[0] if argv else None) or os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT
    base = os.path.join(ue_root, BASE)
    if not os.path.isdir(base):
        print("[error] 找不到 %s" % base)
        return 2
    rows = []
    for dirpath, _, filenames in os.walk(base):
        for name in filenames:
            if not name.endswith(".h"):
                continue
            path = os.path.join(dirpath, name)
            lines = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
            for i, line in enumerate(lines):
                if "UE_DEPRECATED" not in line:
                    continue
                m = re.search(r'UE_DEPRECATED\([^,]+,\s*"([^"]*)"', line)
                msg = m.group(1) if m else ""
                member = ""
                for j in range(i + 1, min(i + 9, len(lines))):
                    mm = re.search(r"\b([A-Z][A-Za-z0-9_]+)\s*\(", lines[j])
                    if mm:
                        member = mm.group(1)
                        break
                if member:
                    rows.append((member, os.path.relpath(path, base).replace(os.sep, "/"),
                                 i + 1, msg))
    seen = set()
    print("%-30s %-44s %-6s %s" % ("member", "header", "line", "替代建议 / 说明"))
    for member, rel, line, msg in sorted(rows):
        key = (member, msg)
        if key in seen:
            continue
        seen.add(key)
        print("%-30s %-44s L%-5d %s" % (member, rel, line, msg[:80]))
    print("\n共 %d 条（去重后）" % len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
