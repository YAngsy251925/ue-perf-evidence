#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hitch_evidence.py — hitch 证据收集与索引（PLAN §9-M2.3）

run 结束后：
  1. 解析 engine.log 的 dumphitches 块（hitch_log.parse_engine_log）
  2. 在宿主工程的 Saved/Profiling/Hitches/<InstanceId>/ 下发现本 run 的快照
     （Frame-N.utrace + Frame-N.png；snapshothitches 写的，N 是 stats 帧号，与 dumphitches 同源）
  3. 拷贝进 <run_dir>/hitches/（证据进 run 目录，引擎目录按时间截断后证据不丢）
  4. 每份快照跑一次 A 路 probe 导出（快照是完整的 trace，含 hitch 帧前后各几帧）
  5. 写 <run_dir>/hitches_index.json：hitch 列表 + 快照路径 + 快照导出目录
"""
import json
import os
import re
import shutil
import sys

from pipeline.extract import hitch_log
from pipeline.extract import insights_export

RE_SNAPSHOT = re.compile(r"^Frame-(\d+)\.utrace$")
SNAPSHOT_MARK = "Hitch detected on frame"
RE_SNAPSHOT_LINE = re.compile(r"snapshot saved in '(.*/Hitches/[^']+)/Frame-(\d+)\.utrace'")


def discover_snapshot_dirs(host_saved, run_started_after):
    """宿主 Saved/Profiling/Hitches/ 下 mtime 晚于 run 开始的实例目录（日志提示缺失时的回退）。"""
    root = os.path.join(host_saved, "Profiling", "Hitches")
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.getmtime(d) >= run_started_after:
            out.append(d)
    return sorted(out)


def snapshot_log_hint(log_path):
    """从引擎日志的 snapshothitches 行拿快照目录（比按 mtime 猜更准）。"""
    if not os.path.isfile(log_path):
        return []
    dirs = set()
    with open(log_path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if SNAPSHOT_MARK in line:
                m = RE_SNAPSHOT_LINE.search(line)
                if m:
                    dirs.add(m.group(1).replace("/", "\\"))
    return sorted(dirs)


def export_snapshot(trace, outdir, ue_root=None):
    """快照是完整 trace：跑一次 probe 导出（带 gpu，看 hitch 时刻的 GPU pass）。"""
    argv = (["--ue-root", ue_root] if ue_root else []) + ["probe", trace, "--out", outdir, "--gpu"]
    return insights_export.main(argv)


def collect(run_dir, log_path, host_saved=None, ue_root=None, snapshot_dirs=None, run_started_after=None,
            only_frames=None, max_export=4):
    """返回 hitches_index dict；无 hitch 时 hitches 为空列表（同样是有效结果：阴性测试）。
    快照目录优先取日志提示；回退扫描只在本 run 开始之后（run_started_after 给出时），
    绝不收历史实验的旧快照。
    only_frames：只收集这些 stats 帧的快照（Lyra 一场能产出几十份快照、总计十几 GB，
    全拷全导不现实——调用方按 hitch 时长 / memreport 归属挑选）。
    max_export：最多导出多少份快照（拷贝不受限）；导出按帧号升序。"""
    hitches = hitch_log.parse_engine_log(log_path) if os.path.isfile(log_path) else []
    if snapshot_dirs is None:
        hinted = [d for d in snapshot_log_hint(log_path) if os.path.isdir(d)]
        if hinted:
            snapshot_dirs = hinted
        elif host_saved and run_started_after is not None:
            snapshot_dirs = discover_snapshot_dirs(host_saved, run_started_after)
        else:
            snapshot_dirs = []
    hdir = os.path.join(run_dir, "hitches")
    os.makedirs(hdir, exist_ok=True)
    entries = []
    exported = 0
    for d in snapshot_dirs:
        for name in sorted(os.listdir(d)):
            m = RE_SNAPSHOT.match(name)
            if not m:
                continue
            stats_frame = int(m.group(1))
            if only_frames is not None and stats_frame not in only_frames:
                continue
            src = os.path.join(d, name)
            dst = os.path.join(hdir, name)
            if not os.path.isfile(dst):
                shutil.copy2(src, dst)
            outdir = os.path.join(hdir, "Frame-%d_export" % stats_frame)
            export_rc = 1
            if exported < max_export:
                export_rc = export_snapshot(dst, outdir, ue_root)
                exported += 1
            png = os.path.join(d, "Frame-%d.png" % stats_frame)
            if os.path.isfile(png):
                shutil.copy2(png, os.path.join(hdir, "Frame-%d.png" % stats_frame))
            hitch = next((h for h in hitches if h["stats_frame"] == stats_frame), None)
            entries.append({
                "stats_frame": stats_frame,
                "snapshot": "hitches/" + name,
                "screenshot": ("hitches/Frame-%d.png" % stats_frame) if os.path.isfile(png) else None,
                "export_dir": os.path.relpath(outdir, run_dir).replace("\\", "/"),
                "export_ok": export_rc == 0,
                "hitch_ms": hitch["ms"] if hitch else None,
                "matched_dumphitches_block": hitch is not None,
            })
    index = {
        "schema": "perf-hitches/0",
        "hitches": hitches,
        "snapshots": entries,
        "source_dirs": [d.replace("\\", "/") for d in snapshot_dirs],
    }
    with open(os.path.join(run_dir, "hitches_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return index


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    import argparse
    ap = argparse.ArgumentParser(description="收集 hitch 证据（PLAN §9-M2.3）")
    ap.add_argument("run_dir", help="run 目录（engine.log 已在）")
    ap.add_argument("--host-saved", default=None, help="宿主工程 Saved 目录（回退扫描用）")
    ap.add_argument("--ue-root", default=None, help="UE 根目录（快照导出用）")
    a = ap.parse_args(argv)
    run_dir = os.path.abspath(a.run_dir)
    log = os.path.join(run_dir, "engine.log")
    index = collect(run_dir, log, a.host_saved, a.ue_root)
    print("[ok] hitches=%d snapshots=%d" % (len(index["hitches"]), len(index["snapshots"])))
    for e in index["snapshots"]:
        print("  frame %d: %s (export_ok=%s)" % (e["stats_frame"], e["snapshot"], e["export_ok"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
