#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capture.py — perf_capture 冷启动模式（PLAN §9-M2.2）：一条命令完成 prepare → 采集 → 对账。

用法：python perfctl.py capture <profile> <run_dir> [--map M] [--uproject F] [--ue-root DIR]

流程：
  1. profile.prepare        写 launch.bat 与 capture_profile.json（声明快照）
  2. harness/coexist_run.sh 启动编辑器（时长模式：等 "Engine is initialized" 再计时）
     - clean_exit profile：引擎在 stop_at_s 后自己写完 CSV 退出（P15），超预算才强杀兜底
  3. TraceDigestPerf        经宿主 commandlet 读 trace 书签，写 <run_dir>/coverage.json
     （PerfEvidence CoverageStart / CaptureStop 的 trace 侧证据）
  4. manifest               带 capture_profile.json 与 coverage.json 的 run 会得到
     profile / conditions / windows.coverage_probe / 干净统计窗口
"""
import json
import os
import re
import subprocess
import sys

from pipeline.capture import profile as P

REPO = P.REPO
HARNESS = os.path.join(REPO, "harness", "coexist_run.sh")
CMDLET_BAT = os.path.join(REPO, "host", "TraceDigestProbe", "run_cmdlet.bat")
COVERAGE_FILE = "coverage.json"
COVERAGE_START_MARK = "PerfEvidence CoverageStart"
COVERAGE_STOP_MARK = "PerfEvidence CaptureStop"


def run(cmd, timeout):
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, shell=(os.name == "nt"))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def collect_coverage(run_dir, ue_root, timeout):
    """经宿主 commandlet 读 trace 书签，取 coverage 两个事件的 trace 时间，写 coverage.json。
    返回 (coverage dict 或 None, 输出文本)。"""
    trace = os.path.join(run_dir, "trace.utrace")
    if not os.path.isfile(trace):
        return None, "no trace in run dir"
    log = os.path.join(run_dir, "perf_cmdlet.log")
    args = [CMDLET_BAT, "TraceDigestPerf", trace, log,
            "prefix=%s" % P.COVERAGE_EVENT_START.split()[0]]  # 前缀 = "PerfEvidence"
    args = [a if os.sep in a else a for a in args]
    rc, output = run(["cmd", "/c", " ".join('"%s"' % a if " " in a else a for a in args)], timeout)
    marks = []
    for m in re.finditer(r"PERF_BOOKMARK ([0-9.]+) (.*)", output or ""):
        marks.append((float(m.group(1)), m.group(2).strip()))
    # 输出也可能只进了 ABSLOG
    if not marks and os.path.isfile(log):
        with open(log, encoding="utf-8-sig", errors="replace") as f:
            for m in re.finditer(r"PERF_BOOKMARK ([0-9.]+) (.*)", f.read()):
                marks.append((float(m.group(1)), m.group(2).strip()))
    start = next((t for t, text in marks if text == COVERAGE_START_MARK), None)
    stop = next((t for t, text in marks if text == COVERAGE_STOP_MARK), None)
    coverage = None
    if start is not None and stop is not None and stop > start:
        coverage = {"start": start, "end": stop, "source": "trace_bookmarks",
                    "event_start": COVERAGE_START_MARK, "event_stop": COVERAGE_STOP_MARK}
    result = {"coverage": coverage, "returncode": rc,
              "commandlet_log": os.path.relpath(log, run_dir).replace("\\", "/")}
    with open(os.path.join(run_dir, COVERAGE_FILE), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result, None


def capture(name, run_dir, uproject=None, ue_root=None, map_override=None):
    snapshot = P.prepare(name, run_dir, uproject, ue_root, map_override)
    budget, fallback_s = snapshot["harness"]["startup_budget_s"], snapshot["harness"]["run_after_ready_s"]
    bat = os.path.join(run_dir, "launch.bat").replace("\\", "/")
    cmd = ["bash", HARNESS, bat, run_dir.replace("\\", "/"), str(budget), "-", "-", str(fallback_s)]
    print("[capture] %s (%s class=%s)" % (run_dir, snapshot["profile_name"], snapshot["profile_class"]))
    print("[capture] harness: %s" % " ".join(cmd[1:]))
    rc, output = run(cmd, budget + fallback_s + 180)
    sys.stdout.write(output)
    if rc != 0:
        print("[capture] FAILED: harness reported failure (assert / early exit / startup timeout)")
        return 2
    if snapshot.get("csv"):
        print("[capture] collecting coverage bookmarks via TraceDigestPerf ...")
        result, err = collect_coverage(run_dir, ue_root, 900)
        if err or result is None:
            print("[capture] coverage collection failed: %s" % (err or "unknown"))
        else:
            cov = result["coverage"]
            if cov is None:
                print("[capture] WARNING: coverage events not found in the trace "
                      "(see %s; the run keeps its evidence otherwise)" % result["commandlet_log"])
            else:
                print("[capture] coverage_probe = [%.4f, %.4f] (trace time)" % (cov["start"], cov["end"]))
    if snapshot.get("exec_at") or snapshot.get("profile_class") == "measure":
        print("[capture] collecting hitch evidence (dumphitches log blocks + snapshots) ...")
        from pipeline.capture import hitch_evidence
        host_saved = os.path.join(os.path.dirname(snapshot["uproject"]), "Saved")
        index = hitch_evidence.collect(run_dir, os.path.join(run_dir, "engine.log"),
                                       host_saved=host_saved, ue_root=ue_root)
        print("[capture] hitches=%d snapshots=%d" % (len(index["hitches"]), len(index["snapshots"])))
    print("[capture] next: python perfctl.py manifest %s" % run_dir.replace("\\", "/"))
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    import argparse
    ap = argparse.ArgumentParser(description="perf_capture 冷启动模式（PLAN §9-M2.2）")
    ap.add_argument("profile", help="profile 名或 json 路径")
    ap.add_argument("run_dir", help="新的 run 目录")
    ap.add_argument("--map", default=None, help="覆盖 profile 的地图")
    ap.add_argument("--uproject", default=None, help="宿主工程（默认 host/TraceDigestProbe）")
    ap.add_argument("--ue-root", default=None, help="UE 根目录（默认读 UE_ROOT）")
    a = ap.parse_args(argv)
    try:
        return capture(a.profile, a.run_dir, a.uproject, a.ue_root, a.map)
    except ValueError as e:
        print("[error] %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
