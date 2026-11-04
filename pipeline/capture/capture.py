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
import shutil
import subprocess
import sys
import time

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


def project_csv_dir(uproject):
    """工程的 CSV Profiler 输出目录（引擎固定写在 <Project>/Saved/Profiling/CSV）。"""
    return os.path.join(os.path.dirname(os.path.abspath(uproject)), "Saved", "Profiling", "CSV")


def archive_csv(run_dir, uproject, since_epoch, until_epoch=None):
    """把本次 run 写出的 CSV Profiler 文件拷进 <run_dir>/csv/ 并写 index.json（§5.1 的 csv/）。

    time 窗口按文件 mtime 判：CSV 在 -ExitAfterCsvProfiling 时落盘，mtime 落在 run 期间。
    返回拷贝的文件名列表；没有就返回空（不报错——smoke run 可能不开 CSV）。
    """
    src = project_csv_dir(uproject)
    if not os.path.isdir(src):
        return []
    dest = os.path.join(run_dir, "csv")
    copied = []
    for name in sorted(os.listdir(src)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(src, name)
        mtime = os.path.getmtime(path)
        if mtime < since_epoch:
            continue
        if until_epoch is not None and mtime > until_epoch:
            continue
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(path, os.path.join(dest, name))
        copied.append(name)
    if copied:
        with open(os.path.join(dest, "index.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": "perf-csv-index/0", "files": copied,
                       "source": src.replace("\\", "/")}, f, ensure_ascii=False, indent=1)
    return copied


def project_memreport_dir(uproject):
    """工程的 MemReport 输出目录（引擎写在 <Project>/Saved/Profiling/MemReports/<Level>-<Platform>-<date>/）。"""
    return os.path.join(os.path.dirname(os.path.abspath(uproject)), "Saved", "Profiling", "MemReports")


def archive_memreports(run_dir, uproject, since_epoch, until_epoch=None):
    """把本次 run 期间写出的 .memreport 拷进 <run_dir>/memreport/（M6.1，P6 的落地检查）。

    引擎把 memreport 写在多层子目录里；这里**摊平**成 memreport/<文件名字>，并记 index.json。
    """
    src = project_memreport_dir(uproject)
    if not os.path.isdir(src):
        return []
    dest = os.path.join(run_dir, "memreport")
    copied = []
    for dirpath, _dirnames, filenames in os.walk(src):
        for name in filenames:
            if not name.lower().endswith(".memreport"):
                continue
            path = os.path.join(dirpath, name)
            mtime = os.path.getmtime(path)
            if mtime < since_epoch:
                continue
            if until_epoch is not None and mtime > until_epoch:
                continue
            os.makedirs(dest, exist_ok=True)
            target = os.path.join(dest, name)
            shutil.copy2(path, target)
            copied.append({"name": name, "size": os.path.getsize(target),
                           "mtime_epoch": round(mtime, 3),
                           "source": dirpath.replace(os.sep, "/")})
    if copied:
        index_path = os.path.join(dest, "index.json")
        with open(index_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": "perf-memreport-index/0", "files": copied}, f,
                      ensure_ascii=False, indent=1)
    return copied


def start_presentmon(run_dir, seconds):
    """在 harness 之前起 PresentMon（第三方帧时间仲裁者）；工具缺失或起不来只警告。

    带 --process_name 需要提权（实测直接被拒），所以全量采集、解析侧按 Application 匹配。
    """
    from pipeline.capture import presentmon
    if not presentmon.available():
        return None, None
    csv_path = os.path.join(run_dir, "presentmon", "presentmon.csv")
    try:
        proc, _cmd = presentmon.start(csv_path, process_name=None, seconds=int(seconds) + 60)
    except OSError as exc:
        print("[capture] WARNING: PresentMon failed to start (%s)" % exc)
        return None, None
    print("[capture] presentmon started (pid=%d) -> %s" % (proc.pid, csv_path.replace("\\", "/")))
    return proc, csv_path


def finish_presentmon(run_dir, proc, csv_path):
    """收尾：等 PresentMon 退出、解析、写 presentmon/index.json；失败只警告。"""
    if proc is None or csv_path is None:
        return
    from pipeline.capture import presentmon
    rc, err = presentmon.stop(proc, timeout=30)
    if not os.path.isfile(csv_path):
        print("[capture] WARNING: PresentMon wrote no CSV (exit=%s%s)"
              % (rc, (", " + err.strip().splitlines()[0]) if err.strip() else ""))
        return
    summary = presentmon.summarize(csv_path, "UnrealEditor-Cmd.exe")
    summary["exit_code"] = rc
    index_path = os.path.join(run_dir, "presentmon", "index.json")
    with open(index_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    bp = ((summary.get("apps") or {}).get(summary.get("picked")) or {}).get("ms_between_presents") or {}
    print("[capture] presentmon: picked=%s frames=%s p50=%s" %
          (summary.get("picked"), bp.get("n"), bp.get("p50")))


def capture(name, run_dir, uproject=None, ue_root=None, map_override=None):
    snapshot = P.prepare(name, run_dir, uproject, ue_root, map_override)
    budget, fallback_s = snapshot["harness"]["startup_budget_s"], snapshot["harness"]["run_after_ready_s"]
    bat = os.path.join(run_dir, "launch.bat").replace("\\", "/")
    cmd = ["bash", HARNESS, bat, run_dir.replace("\\", "/"), str(budget), "-", "-", str(fallback_s)]
    print("[capture] %s (%s class=%s)" % (run_dir, snapshot["profile_name"], snapshot["profile_class"]))
    print("[capture] harness: %s" % " ".join(cmd[1:]))
    launch_epoch = time.time()
    pm_proc, pm_csv = start_presentmon(run_dir, budget + fallback_s)
    rc, output = run(cmd, budget + fallback_s + 180)
    finish_presentmon(run_dir, pm_proc, pm_csv)
    sys.stdout.write(output)
    if rc != 0:
        print("[capture] FAILED: harness reported failure (assert / early exit / startup timeout)")
        return 2
    memreports = archive_memreports(run_dir, snapshot["uproject"], launch_epoch - 5, time.time() + 5)
    if memreports:
        print("[capture] memreport archived: %s" % ", ".join(m["name"] for m in memreports))
    elif (snapshot.get("exec_at") or {}).get("command", "").startswith("memreport"):
        print("[capture] WARNING: memreport 命令已调度但没有找到 .memreport 文件（看 %s）"
              % project_memreport_dir(snapshot["uproject"]))
    copied = archive_csv(run_dir, snapshot["uproject"], launch_epoch - 5, time.time() + 5)
    if copied:
        print("[capture] csv profiler archived: %s" % ", ".join(copied))
    elif snapshot.get("csv"):
        print("[capture] WARNING: csv profiler enabled but no CSV file found in %s"
              % project_csv_dir(snapshot["uproject"]))
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
