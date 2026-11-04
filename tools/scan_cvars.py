#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_cvars.py — 从**本机安装的引擎源码**里扫出可调开关（cvar），生成"杠杆目录"

为什么要扫而不是写死：性能建议最容易过时的部分就是"有哪些开关、默认值多少、哪个版本被删了"。
把清单写成文档，半年后就变成套路；而引擎源码就在本机，一次扫描 = 当前版本的真实 cvar 表。

产出 JSON：name / default / help / file / line / subsystem（从名字前缀推断）。
之后 `perfctl advise` 会拿热区（Lumen/Bloom/TSR/Slate/…) 去目录里查**这个版本真的存在**的开关，
并附上 file:line —— 建议因此可以追溯到具体代码位置，而不是"我记得有个 cvar"。

用法：
  python tools/scan_cvars.py --ue-root D:/Epic/Games/UE_5.8 --out catalog/cvars.json
  python tools/scan_cvars.py --prefix r.Lumen --prefix r.Bloom --limit 50
"""
import argparse
import io
import json
import os
import re
import sys

# TAutoConsoleVariable<T>(TEXT("r.Bloom.xxx"), 默认值, TEXT("帮助"), 标志);
RE_VAR = re.compile(
    r"TAutoConsoleVariable\s*<[^>]+>\s*[A-Za-z_][\w:]*\s*\(\s*TEXT\s*\(\s*\"(?P<name>[^\"]+)\"\s*\)\s*,\s*"
    r"(?P<default>[^,]+),",
    re.S)
RE_REF = re.compile(
    r"FAutoConsoleVariableRef\s+[A-Za-z_][\w:]*\s*\(\s*TEXT\s*\(\s*\"(?P<name>[^\"]+)\"\s*\)\s*,\s*"
    r"(?P<target>[A-Za-z_][\w:.]*)")
RE_HELP = re.compile(r"TEXT\s*\(\s*\"(?P<help>[^\"]{4,200})\"\s*\)")

SUBSYSTEMS = ("r.Lumen", "r.Nanite", "r.Bloom", "r.TSR", "r.AntiAliasing", "r.Shadow", "r.VSM",
              "r.VolumetricFog", "r.Translucency", "r.PostProcessing", "r.ScreenPercentage",
              "r.DynamicGlobalIllumination", "r.Reflection", "r.SSGI", "r.Streaming", "r.Mip",
              "r.RayTracing", "r.Hair", "r.SkinCache", "r.SkyAtmosphere", "r.Cloud",
              "sg.", "t.MaxFPS", "t.RHTSync", "Slate.", "r.RDG", "r.GraphicsAdapter")


def subsystem_of(name):
    for s in SUBSYSTEMS:
        if name.startswith(s):
            return s.rstrip(".")
    return name.split(".")[0]


def scan(root, prefixes=(), max_files=None, limit=0):
    sources = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 只扫源码目录，跳过中间产物与第三方
        low = dirpath.replace("\\", "/").lower()
        if "/intermediate/" in low or "/thirdparty/" in low or "/binaries/" in low:
            continue
        for fn in filenames:
            if fn.endswith((".cpp", ".h")):
                sources.append(os.path.join(dirpath, fn))
        if max_files and len(sources) >= max_files:
            break
    found = {}
    for path in sources:
        try:
            with io.open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "TAutoConsoleVariable" not in text and "FAutoConsoleVariableRef" not in text:
            continue
        for m in RE_VAR.finditer(text):
            name = m.group("name")
            if prefixes and not any(name.startswith(p) for p in prefixes):
                continue
            line = text.count("\n", 0, m.start()) + 1
            help_match = RE_HELP.search(text, m.end(), m.end() + 600)
            found.setdefault(name, {
                "name": name, "default": m.group("default").strip(),
                "help": help_match.group("help") if help_match else None,
                "file": path.replace("\\", "/"), "line": line,
                "subsystem": subsystem_of(name), "kind": "console_variable"})
        for m in RE_REF.finditer(text):
            name = m.group("name")
            if prefixes and not any(name.startswith(p) for p in prefixes):
                continue
            line = text.count("\n", 0, m.start()) + 1
            found.setdefault(name, {
                "name": name, "default": None, "help": None,
                "file": path.replace("\\", "/"), "line": line,
                "subsystem": subsystem_of(name), "kind": "variable_ref",
                "target": m.group("target")})
    rows = sorted(found.values(), key=lambda r: r["name"])
    return rows[:limit] if limit else rows


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="扫描引擎源码里的 cvar，生成杠杆目录")
    ap.add_argument("--ue-root", default=os.environ.get("UE_ROOT") or r"D:/Epic/Games/UE_5.8")
    ap.add_argument("--subdir", default="Engine/Source", help="相对 ue-root 的源码目录")
    ap.add_argument("--prefix", action="append", default=[], help="只看这些前缀（可重复）")
    ap.add_argument("--out", default=None, help="输出 JSON（不给就打印）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-files", type=int, default=None)
    a = ap.parse_args(argv)

    root = os.path.join(a.ue_root, a.subdir)
    if not os.path.isdir(root):
        print("[error] 源码目录不存在：%s（用 --ue-root 指到引擎根）" % root)
        return 1
    rows = scan(root, tuple(a.prefix), a.max_files, a.limit)
    payload = {"schema": "perf-cvar-catalog/0", "engine_root": a.ue_root.replace("\\", "/"),
               "source_root": root.replace("\\", "/"), "count": len(rows),
               "prefixes": list(a.prefix), "cvars": rows}
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("[scan] %d 个 cvar → %s" % (len(rows), os.path.abspath(a.out)))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:4000])
    by_sub = {}
    for r in rows:
        by_sub[r["subsystem"]] = by_sub.get(r["subsystem"], 0) + 1
    top = sorted(by_sub.items(), key=lambda kv: -kv[1])[:10]
    print("[scan] 子系统分布：" + "、".join("%s %d" % (k, v) for k, v in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
