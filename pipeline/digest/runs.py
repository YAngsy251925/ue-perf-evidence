#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runs.py — `perfctl runs`：列出 runs/ 下的 run 与一行摘要（PLAN §3 的 perf_runs）。

给 CI 和 agent 用：先看有哪些 run、哪些可测量、稳态窗口多长，再决定 diff 拿哪两个。
只读 manifest.json，不碰 trace（秒级返回）。缺 manifest 的目录也会列出来并标注。
"""
import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ROOT = os.path.join(REPO, "runs")


def _load_manifest(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _frame_count(frames_seen):
    """manifest 的 frames_seen 可能是整数，也可能是按轨道分的字典（§5.2）。

    字典时优先取 Game 帧轨道，其次取最大的数值轨道——只为列表给一个量级参考。
    """
    if isinstance(frames_seen, int):
        return frames_seen
    if isinstance(frames_seen, dict) and frames_seen:
        if isinstance(frames_seen.get("game_frame_track"), int):
            return frames_seen["game_frame_track"]
        numeric = [v for v in frames_seen.values() if isinstance(v, int)]
        if numeric:
            return max(numeric)
    return None


def summarize(run_dir):
    """从一个 run 目录读出摘要；manifest 缺失/损坏时只保留目录名与原因。"""
    name = os.path.basename(run_dir.rstrip("\\/"))
    info = {"run_id": name, "dir": run_dir, "manifest": False, "measurable": None,
            "reason": "", "rhi": "", "steady_s": None, "frames": None, "trace_mb": None}
    manifest_path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        info["reason"] = "no manifest.json"
        return info
    m = _load_manifest(manifest_path)
    if m is None:
        info["reason"] = "manifest.json unreadable"
        return info

    info["manifest"] = True
    info["run_id"] = m.get("run_id") or name
    info["measurable"] = m.get("measurable")
    info["reason"] = m.get("measurable_reason") or ""
    info["rhi"] = ((m.get("engine") or {}).get("rhi") or "")
    steady = (m.get("windows") or {}).get("steady")
    if steady and len(steady) == 2:
        info["steady_s"] = round(float(steady[1]) - float(steady[0]), 1)
    info["frames"] = _frame_count(m.get("frames_seen"))
    trace = m.get("trace") or {}
    trace_file = trace.get("file")
    if trace_file:
        trace_path = os.path.join(run_dir, trace_file)
        if os.path.isfile(trace_path):
            info["trace_mb"] = round(os.path.getsize(trace_path) / 1e6, 1)
    return info


def collect(root, name_filter=None):
    if not os.path.isdir(root):
        return []
    rows = []
    for entry in sorted(os.listdir(root)):
        run_dir = os.path.join(root, entry)
        if not os.path.isdir(run_dir):
            continue
        if name_filter and name_filter.lower() not in entry.lower():
            continue
        rows.append(summarize(run_dir))
    return rows


def format_row(r):
    measurable = "-" if r["measurable"] is None else ("yes" if r["measurable"] else "NO")
    steady = "-" if r["steady_s"] is None else "%.1fs" % r["steady_s"]
    frames = "-" if r["frames"] is None else str(r["frames"])
    size = "-" if r["trace_mb"] is None else "%.0fMB" % r["trace_mb"]
    reason = r["reason"] if not r["measurable"] else ""
    line = "%-22s %-5s %-7s %7s %8s %7s  %s" % (
        r["run_id"], measurable, r["rhi"] or "-", steady, frames, size, reason)
    return line.rstrip()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="列出 run 与一行摘要（perf_runs）")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="runs 根目录（默认仓库 runs/）")
    ap.add_argument("--filter", default=None, help="只列 run_id 含此子串的 run")
    ap.add_argument("--limit", type=int, default=0, help="最多列几个（0 = 全部）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 agent / CI 解析）")
    a = ap.parse_args(argv)

    rows = collect(a.root, a.filter)
    if a.limit > 0:
        rows = rows[:a.limit]
    if a.json:
        print(json.dumps({"schema": "perf-runs/0", "root": a.root, "runs": rows},
                         ensure_ascii=False, indent=1))
        return 0 if rows else 1
    if not rows:
        print("[runs] %s 下没有 run 目录" % a.root)
        return 1
    print("%-22s %-5s %-7s %7s %8s %7s  %s" % ("run_id", "meas", "rhi", "steady", "frames", "trace", "note"))
    for r in rows:
        print(format_row(r))
    print("[runs] %d run(s) under %s" % (len(rows), a.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
