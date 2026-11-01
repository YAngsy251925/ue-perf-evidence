#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpu.py — CPU 自耗时视图（digest v0，迁自实验目录 ue5-perf-lab 的 digest.py）

输入: UnrealInsights 无头导出的 TimerStatistics CSV
      (Name,Count,C.Avg,Incl,I.Min,I.Max,I.Avg,I.Med,Excl,E.Min,E.Max,E.Avg,E.Med)
输出: <out>/perf_digest.json 与 <out>/perf_digest.md（--out 必填，P7）

同一输入逐字节同一输出；改输出格式前先跑回归对比。
digest v1 的结构（结论先行、等待类单列、省略说明、下钻指针）在 M1.5 实现。
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

OUT_JSON = "perf_digest.json"
OUT_MD = "perf_digest.md"
TOP_N = 15


def load_rows(src: str):
    rows = []
    with open(src, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "name": r["Name"],
                    "count": int(float(r["Count"] or 0)),
                    "incl": float(r["Incl"] or 0),
                    "excl": float(r["Excl"] or 0),
                    "imax": float(r["I.Max"] or 0),
                    "iavg": float(r["I.Avg"] or 0),
                    "imed": float(r["I.Med"] or 0),
                    "emax": float(r["E.Max"] or 0),
                    "eavg": float(r["E.Avg"] or 0),
                })
            except Exception:
                continue
    return rows


# 分类前缀（可按项目命名规范调整）：Scene./RHI/TaskGraph/GC/...
def bucket(name: str) -> str:
    for sep in (".", "::", "_"):
        if sep in name:
            head = name.split(sep)[0]
            if head:
                return head
    return name


def build(src: str) -> dict:
    rows = load_rows(src)
    total_excl = sum(r["excl"] for r in rows) or 1e-9
    by_excl = sorted(rows, key=lambda r: r["excl"], reverse=True)
    by_incl = sorted(rows, key=lambda r: r["incl"], reverse=True)

    agg = defaultdict(lambda: {"excl": 0.0, "incl": 0.0, "count": 0, "n": 0})
    for r in rows:
        b = agg[bucket(r["name"])]
        b["excl"] += r["excl"]; b["incl"] += r["incl"]; b["count"] += r["count"]; b["n"] += 1

    def pct(x):
        return round(100.0 * x / total_excl, 2)

    return {
        "source_csv": os.path.basename(src),
        "timer_count": len(rows),
        "total_self_time_s": round(total_excl, 3),
        "top_by_self_time": [
            {
                "name": r["name"], "count": r["count"],
                "self_s": round(r["excl"], 3), "self_pct": pct(r["excl"]),
                "incl_s": round(r["incl"], 3),
                "avg_ms": round(1000 * r["eavg"], 3), "max_ms": round(1000 * r["emax"], 3),
                "spike_ratio": round(r["emax"] / r["eavg"], 1) if r["eavg"] > 0 else None,
            }
            for r in by_excl[:TOP_N]
        ],
        "top_by_inclusive": [
            {"name": r["name"], "incl_s": round(r["incl"], 3), "incl_pct": pct(r["incl"])}
            for r in by_incl[:TOP_N]
        ],
        "by_prefix": sorted(
            [{"prefix": k, "self_s": round(v["excl"], 3), "self_pct": pct(v["excl"]),
              "timers": v["n"], "calls": v["count"]}
             for k, v in agg.items()],
            key=lambda d: d["self_s"], reverse=True,
        )[:12],
    }


def render_md(digest: dict) -> str:
    lines = [
        f"# Perf digest — {digest['source_csv']}",
        f"timers={digest['timer_count']} total_self={digest['total_self_time_s']}s",
        "",
        "## Top self-time（瓶颈候选，按自耗时排序）",
        "| # | timer | self_s | self% | calls | avg_ms | max_ms | spike |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(digest["top_by_self_time"], 1):
        lines.append(f"| {i} | {r['name']} | {r['self_s']} | {r['self_pct']}% | {r['count']} | {r['avg_ms']} | {r['max_ms']} | {r['spike_ratio']}x |")
    lines += ["", "## 按命名前缀聚合", "| prefix | self_s | self% | timers | calls |", "|---|---|---|---|---|"]
    for r in digest["by_prefix"]:
        lines.append(f"| {r['prefix']} | {r['self_s']} | {r['self_pct']}% | {r['timers']} | {r['calls']} |")
    return "\n".join(lines) + "\n"


def write(digest: dict, md: str, out: str):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, OUT_JSON), "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, OUT_MD), "w", encoding="utf-8") as f:
        f.write(md)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="CPU 自耗时视图（digest v0）")
    ap.add_argument("src", help="TimerStatistics CSV")
    ap.add_argument("--out", required=True, help="输出目录（必填）")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.src):
        sys.exit(f"[error] csv not found: {a.src}")
    digest = build(a.src)
    md = render_md(digest)
    write(digest, md, a.out)

    raw_bytes = os.path.getsize(a.src)
    json_len = len(json.dumps(digest, ensure_ascii=False))
    print(f"raw csv      : {raw_bytes/1024:.1f} KiB  (~{raw_bytes/4:.0f} tok)")
    print(f"digest json  : {json_len/1024:.1f} KiB (~{json_len/4:.0f} tok)")
    print(f"digest md    : {len(md)/1024:.1f} KiB (~{len(md)/4:.0f} tok)")
    print(f"compression  : {raw_bytes/max(len(md),1):.0f}x (md)")
    print(f"[ok] {os.path.join(os.path.abspath(a.out), OUT_MD)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
