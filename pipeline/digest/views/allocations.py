#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""allocations.py — M6.2：内存分配视图（A 路 MemoryInsights.ExportAllocs）

先实测再决定：A 路的 `MemoryInsights.ExportAllocs` 在带 `memory` 通道的 trace 上**可用**，
一次导出就是一张 20 列的分配表（Size / Tag / Asset / ClassName / Package / AllocThread / …），
足够回答"内存被谁占"，所以 M6 不走 B2 的内存分配解析（B2 的 `memory`/`allocations` 留作交叉验证）。

用法：
  perfctl digest allocs <run_dir> --out DIR [--rule aAf] [--time-a S --time-b S] [--max N] [--top N]
  已有 CSV 时：perfctl digest allocs <csv 路径> --out DIR …
"""
import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RULES = ("aAf", "afA", "Aaf", "aAfB", "AaBf", "AaB", "AfB", "AafB", "aABf", "AaBCf", "AaBfC", "aABfC", "AaBCfD")


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def read_allocs(csv_path):
    """读 ExportAllocs 的 CSV。返回 (rows, columns)。Size 单位字节，全 0/NA 的列照常保留。"""
    import csv as _csv
    with io.open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        reader = _csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = []
        for row in reader:
            size = _num(row.get("Size"))
            rows.append({"size": size, "tag": (row.get("Tag") or "").strip(),
                         "asset": (row.get("Asset") or "").strip(),
                         "class": (row.get("ClassName") or "").strip(),
                         "package": (row.get("Package") or "").strip(),
                         "thread": (row.get("AllocThread") or "").strip(),
                         "duration": _num(row.get("Duration")),
                         "start": _num(row.get("StartTime"))})
    return rows, columns


def summarize(rows, top=20):
    """总量 + 三个维度的 Top-N（Tag / ClassName / Package）+ 单条 Top。"""
    sized = [r for r in rows if r["size"] and r["size"] > 0]
    total = sum(r["size"] for r in sized)

    def group(key):
        acc = {}
        for r in sized:
            name = r[key] or "<none>"
            acc[name] = acc.get(name, 0) + r["size"]
        ranked = sorted(acc.items(), key=lambda kv: -kv[1])[:top]
        return [{"name": k, "bytes": int(v), "mb": round(v / 1048576.0, 3),
                 "pct": round(100.0 * v / total, 2) if total else None} for k, v in ranked]

    singles = sorted(sized, key=lambda r: -r["size"])[:top]
    return {
        "rows": len(rows), "sized_rows": len(sized),
        "total_bytes": int(total), "total_mb": round(total / 1048576.0, 3),
        "by_tag": group("tag"), "by_class": group("class"), "by_package": group("package"),
        "top_single": [{"mb": round(r["size"] / 1048576.0, 3), "tag": r["tag"],
                        "class": r["class"], "package": r["package"], "thread": r["thread"]}
                       for r in singles],
    }


def render_md(result):
    lines = ["# 内存分配视图 — %s" % result.get("source"), ""]
    if not result.get("summary") or not result["summary"]["sized_rows"]:
        lines += ["没有可用的分配数据。常见原因：trace 没带 `memory` 通道（capture profile 里加 `memory`），"
                  "或该 run 的时间窗内没有分配事件。", ""]
        for w in result.get("warnings") or []:
            lines.append("- %s" % w)
        return "\n".join(lines)

    s = result["summary"]
    lines += ["| 项 | 值 |", "|---|---|",
              "| 规则 | `%s` |" % result.get("rule"),
              "| 行数 / 有大小的行 | %s / %s |" % (s["rows"], s["sized_rows"]),
              "| 合计 | %.1f MB |" % s["total_mb"], ""]
    for key, title in (("by_tag", "按 Tag"), ("by_class", "按 Class"), ("by_package", "按 Package")):
        rows = s.get(key) or []
        if not rows:
            continue
        lines += ["## %s（Top %d）" % (title, len(rows)), "", "| 名称 | MB | 占比 |", "|---|---|---|"]
        for r in rows:
            lines.append("| %s | %.2f | %s |" % (r["name"][:60], r["mb"],
                                                 "-" if r["pct"] is None else "%.1f%%" % r["pct"]))
        lines.append("")
    singles = s.get("top_single") or []
    if singles:
        lines += ["## 单条最大分配", "", "| MB | Tag | Class | Package | 线程 |", "|---|---|---|---|---|"]
        for r in singles[:10]:
            lines.append("| %.2f | %s | %s | %s | %s |"
                         % (r["mb"], r["tag"][:20], r["class"][:24], r["package"][:32], r["thread"]))
        lines.append("")
    if result.get("warnings"):
        lines += ["## 警告"] + ["- %s" % w for w in result["warnings"]] + [""]
    return "\n".join(lines)


def build_from_csv(csv_path, rule, top=20):
    rows, columns = read_allocs(csv_path)
    summary = summarize(rows, top)
    warnings = []
    if summary["rows"] == 0:
        warnings.append("CSV 只有表头：trace 里没有分配事件（多半是没开 memory 通道）")
    return {"schema": "perf-allocs/0", "source": os.path.basename(csv_path),
            "columns": columns, "rule": rule, "summary": summary, "warnings": warnings}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="内存分配视图（M6.2）")
    ap.add_argument("target", help="run 目录或已有的 allocs CSV")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--rule", default="aAf", choices=RULES)
    ap.add_argument("--time-a", type=float, default=None)
    ap.add_argument("--time-b", type=float, default=None)
    ap.add_argument("--max", type=int, default=1000, help="导出行数上限（ExportAllocs 的 MaxResults）")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--ue-root", default=None)
    a = ap.parse_args(argv)

    warnings = []
    if os.path.isdir(os.path.abspath(a.target)):
        run_dir = os.path.abspath(a.target)
        csv_path = os.path.join(run_dir, "allocs", "allocs_%s.csv" % a.rule)
        if not os.path.isfile(csv_path):
            manifest = os.path.join(run_dir, "manifest.json")
            window = None
            if os.path.isfile(manifest):
                with io.open(manifest, encoding="utf-8") as f:
                    m = json.load(f)
                basis = (m.get("window_basis") or {}).get("stats_window") or "stats"
                window = (m.get("windows") or {}).get(basis)
            from pipeline.extract import insights_export as IE
            trace = os.path.join(run_dir, "trace.utrace")
            if not os.path.isfile(trace):
                print("[error] trace not found: %s" % trace)
                return 1
            ta = a.time_a if a.time_a is not None else (window[0] if window else None)
            tb = a.time_b if a.time_b is not None else (window[1] if window else None)
            print("[allocs] 导出中（rule=%s, window=%s..%s）..." % (a.rule, ta, tb))
            out, log = IE.export_allocs(trace, os.path.join(run_dir, "allocs"), a.rule, ta, tb,
                                        a.max, ue_root=a.ue_root)
            if not out:
                tail = "\n".join(log.strip().splitlines()[-6:])
                print("[error] ExportAllocs failed:\n%s" % tail)
                return 1
            csv_path = out
        result = build_from_csv(csv_path, a.rule, a.top)
    else:
        result = build_from_csv(os.path.abspath(a.target), a.rule, a.top)
    result["warnings"] += warnings

    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "allocs.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "allocs.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")
    s = result["summary"]
    tags = ", ".join("%s %.0fMB" % (r["name"], r["mb"]) for r in (s.get("by_tag") or [])[:3])
    print("[allocs] rows=%s sized=%s total=%.1fMB top_tags=%s"
          % (s["rows"], s["sized_rows"], s["total_mb"], tags))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "allocs.md"))
    return 0 if s["sized_rows"] else 3


if __name__ == "__main__":
    sys.exit(main())
