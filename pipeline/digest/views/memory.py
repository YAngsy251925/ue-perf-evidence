#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory.py — M6.1：内存视图（MemReport 的结构化报告）

回答：这次 run 占了多少内存、被谁占（按 STATGROUP 合计 + 单条 Top）、对象清单有多大（Showing N of M）、
以及 memreport 这次重操作有没有污染统计窗口（R7：它必须在 coverage_probe 段里）。

用法：
  perfctl digest memory <run_dir> --out DIR [--top 20]
"""
import argparse
import io
import json
import os
import sys

from pipeline.extract import memreport as M


def build(run_dir, top=20):
    files = M.find_files(run_dir)
    result = {"schema": "perf-memory/0",
              "run_dir": os.path.abspath(run_dir).replace(os.sep, "/"),
              "files": [], "platform": None, "stat_count": None,
              "group_totals_mb": {}, "stats_top": [], "sections": [], "showing": [],
              "windows": None, "at_s": None, "inside_probe": None,
              "warnings": []}
    if not files:
        result["warnings"].append("没有 memreport/*.memreport（profile 未挂 ExecAt memreport，或采集时没落盘）")
    else:
        parsed = M.parse(files[0])
        result["files"] = [os.path.basename(f) for f in files]
        result["platform"] = parsed["platform"]
        result["stat_count"] = parsed["stat_count"]
        result["group_totals_mb"] = parsed["group_totals_mb"]
        result["stats_top"] = parsed["stats_top"][:top]
        result["sections"] = parsed["sections"]
        result["showing"] = parsed["showing"]

    manifest_path = os.path.join(run_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with io.open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        block = manifest.get("memreport") or {}
        result["at_s"] = block.get("at_s")
        result["inside_probe"] = block.get("inside_coverage_probe")
        windows = manifest.get("windows") or {}
        result["windows"] = {k: windows.get(k) for k in ("stats", "coverage_probe") if windows.get(k)}
        result["p99_ms"] = ((manifest.get("frame_semantics") or {}).get("game") or {}).get(
            "stats", {}).get("interval_ms", {}).get("p99")
        # R7 的直接证据：全 trace p99 vs 统计窗口 p99。两者之差就是被窗口排除掉的重操作。
        try:
            from pipeline.caliber import frame_probe
            series = frame_probe.load_frame_series(os.path.join(run_dir, "extract"))
            ref = frame_probe.pick_reference(series)
            if ref:
                frames = ref["frames"]
                all_int = [(frames[i][0] - frames[i - 1][0]) * 1000.0 for i in range(1, len(frames))]
                stats_win = (manifest.get("windows") or {}).get(
                    manifest.get("window_basis", {}).get("stats_window") or "stats")
                win_int = []
                if stats_win:
                    for i in range(1, len(frames)):
                        t = frames[i][0]
                        if stats_win[0] <= t <= stats_win[1]:
                            win_int.append((t - frames[i - 1][0]) * 1000.0)
                def p99(vals):
                    vals = sorted(v for v in vals if v > 0)
                    if not vals:
                        return None
                    idx = min(len(vals) - 1, max(0, int(round(0.99 * (len(vals) - 1)))))
                    return round(vals[idx], 3)
                result["p99_all_ms"] = p99(all_int)
                result["p99_stats_ms"] = p99(win_int) if win_int else result["p99_ms"]
        except OSError:
            pass
        if result["inside_probe"] is None:
            result["warnings"].append("manifest 里没有 memreport 块：这个 run 没挂内存 profile")
    else:
        result["warnings"].append("没有 manifest.json：无法核对重操作是否落在探针段")
    return result


def render_md(r):
    lines = ["# 内存视图 — %s" % r["run_dir"], ""]
    plat = r.get("platform") or {}
    if plat:
        pp = plat.get("process_physical_mb") or {}
        pv = plat.get("process_virtual_mb") or {}
        sp = plat.get("system_physical_mb") or {}
        lines += ["## 平台内存", "", "| 项 | 值 |", "|---|---|",
                  "| Process Physical | %s MB used / %s MB peak |" % (pp.get("used"), pp.get("peak")),
                  "| Process Virtual | %s MB used / %s MB peak |" % (pv.get("used"), pv.get("peak")),
                  "| System Physical | %s / %s MB（free %s） |" % (sp.get("used"), sp.get("total"), sp.get("free")),
                  "| memreport 触发 | %s s（in_probe=%s，落在探针段=%s） |"
                  % (r.get("at_s"), r.get("inside_probe"), r.get("inside_probe")),
                  "| 统计窗口 p99 | %s ms |" % r.get("p99_ms"),
                  "| 全 trace p99（含探针段） | %s ms |" % r.get("p99_all_ms"),
                  "| R7 证据 | 全 trace p99 %s ms vs 统计窗口 p99 %s ms —— 差值来自探针段的重操作被排除 |"
                  % (r.get("p99_all_ms"), r.get("p99_stats_ms")),
                  ""]
    groups = r.get("group_totals_mb") or {}
    if groups:
        lines += ["## 按 STATGROUP 合计（Top 15）", "", "| group | MB |", "|---|---|"]
        for name, mb in list(groups.items())[:15]:
            lines.append("| %s | %.1f |" % (name, mb))
        lines.append("")
    top = r.get("stats_top") or []
    if top:
        lines += ["## 单条 Top %d" % len(top), "", "| 标签 | MB | STAT | group |", "|---|---|---|---|"]
        for item in top:
            lines.append("| %s | %.3f | %s | %s |"
                         % (item["label"][:48], item["mb"] or 0.0, item["stat"], item["group"]))
        lines.append("")
    if r.get("showing"):
        lines += ["## 清单规模", "", "| 段 | 展示 | 总数 |", "|---|---|---|"]
        for s in r["showing"]:
            lines.append("| %s | %s | %s |" % (s.get("section"), s.get("shown"), s.get("total")))
        lines.append("")
    if r.get("warnings"):
        lines += ["## 警告"] + ["- %s" % w for w in r["warnings"]] + [""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="内存视图（M6.1，MemReport）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    if not os.path.isdir(run_dir):
        print("[error] run dir not found: %s" % run_dir)
        return 1
    result = build(run_dir, a.top)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "memory.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "memory.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")

    pp = ((result.get("platform") or {}).get("process_physical_mb") or {})
    groups = list((result.get("group_totals_mb") or {}).items())[:3]
    print("[memory] files=%d process_physical=%sMB stats=%s top_groups=%s"
          % (len(result["files"]), pp.get("used"), result.get("stat_count"),
             ", ".join("%s %.0fMB" % (k, v) for k, v in groups)))
    if result.get("inside_probe") is False:
        print("[memory] WARNING: memreport 不在探针段，统计窗口可能被它污染（R7）")
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "memory.md"))
    return 0 if result["files"] else 3


if __name__ == "__main__":
    sys.exit(main())
