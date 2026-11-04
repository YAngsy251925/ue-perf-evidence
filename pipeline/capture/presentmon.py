#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""presentmon.py — M5.3：PresentMon 采集与解析（引擎之外的第三方帧时间）

定位：给「引擎自己说的帧时间」找一个**独立**参照。A 路（Insights）和 B2（TraceServices）都读同一条 trace，
不是独立证据；PresentMon 从 ETW 的 present 事件看同一进程，才是外部仲裁者。

用法（供 capture 调用，也可单独跑）：
  python -m pipeline.capture.presentmon <out.csv> --process UnrealEditor-Cmd.exe --seconds 120

口径（PLAN §8 / M5.3）：
  - 2.6.0，v2 metrics（28 列）；关键列 `msBetweenPresents` / `msGPUTime` / `msCPUBusy` / `msGPUWait`
  - 不提权时其他账户或极短命进程的 Application 会写成 `<unknown>`，所以默认**先按进程名过滤**，
    拿不到再退回全量采集、在解析侧按 Application 匹配
  - 与 trace 的对比是**生命周期级**（同一次 capture 的 p50 分布），不是逐帧对齐：
    PresentMon 的 TimeInMs 从采集开始算，trace 从首帧算，两者的偏移只能到秒级
"""
import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_EXE = os.environ.get("PRESENTMON_EXE", "tools/presentmon/PresentMon-x64.exe")

V2_FIELDS = ["Application", "ProcessID", "SwapChainAddress", "PresentRuntime", "SyncInterval",
             "PresentFlags", "AllowsTearing", "PresentMode", "TimeInMs", "MsBetweenSimulationStart",
             "MsBetweenPresents", "MsBetweenDisplayChange", "MsInPresentAPI", "MsRenderPresentLatency",
             "MsUntilDisplayed", "CPUStartTimeInMs", "MsBetweenAppStart", "MsCPUBusy", "MsCPUWait",
             "MsGPULatency", "MsGPUTime", "MsGPUBusy", "MsGPUWait", "MsAnimationError",
             "AnimationTime", "MsFlipDelay", "MsAllInputToPhotonLatency", "MsClickToPhotonLatency"]

ALIASES = {
    "between_presents": ("MsBetweenPresents", "FrameTime"),
    "gpu_time": ("MsGPUTime", "GPUTime"),
    "cpu_busy": ("MsCPUBusy", "CPUBusy"),
    "gpu_wait": ("MsGPUWait", "GPUWait"),
}

AGREE_PCT = 15.0     # 生命周期级 p50 一致度：≤15% 认为两路一致
WARN_PCT = 30.0


def exe_path(value=None):
    return value or os.environ.get("PRESENTMON_EXE") or DEFAULT_EXE


def available(value=None):
    return os.path.isfile(exe_path(value))


def build_cmd(csv_path, process_name=None, seconds=120, exe=None):
    """PresentMon 命令行。不传 process_name 就是全量采集（解析侧再按 Application 过滤）。"""
    # 注意：本机 2.6.0 实测 **不吃 --v2_metrics**（传了仍写 v1 的 23 列），所以不传；
    # 解析侧两种列名都认（v1: FrameTime/GPUTime/CPUBusy；v2: MsBetweenPresents/MsGPUTime/MsCPUBusy）。
    cmd = [exe_path(exe), "--output_file", csv_path,
           "--stop_existing_session", "--timed", str(int(seconds)), "--terminate_after_timed",
           "--no_console_stats"]
    if process_name:
        cmd += ["--process_name", process_name]
    return cmd


def start(csv_path, process_name=None, seconds=120, exe=None):
    """启动 PresentMon（后台），返回 (Popen, cmd)。目录会先建好。"""
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    cmd = build_cmd(csv_path, process_name, seconds, exe)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL)
    return proc, cmd


def stop(proc, timeout=20):
    """等它自己退出（--timed --terminate_after_timed），超时就终止。返回 (exit_code, stderr)。"""
    try:
        _out, err = proc.communicate(timeout=timeout)
        return proc.returncode, (err or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = b"", b""
        return None, ((err or b"").decode("utf-8", errors="replace") + " (killed after timeout)")


def _f(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def read_rows(csv_path):
    """读 CSV → (fields, rows[dict])。PresentMon 偶尔写 BOM，用 utf-8-sig。"""
    with io.open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = [r for r in reader if r.get("Application")]
    return fields, rows


def app_of(row):
    return (row.get("Application") or "").strip()


def stats(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(clean)
    n = len(ordered)

    def pct(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return ordered[idx]

    return {"n": n, "mean": round(sum(ordered) / n, 4), "p50": round(pct(0.5), 4),
            "p95": round(pct(0.95), 4), "max": round(ordered[-1], 4)}


def summarize(csv_path, process_name=None):
    """按 Application 聚合。返回 {"fields": [...], "apps": {...}, "picked": name|None}。

    process_name 给了就只认开始匹配的应用（如 UnrealEditor-Cmd.exe）；否则取行数最多的。
    """
    fields, rows = read_rows(csv_path)
    by_app = {}
    for row in rows:
        app = app_of(row)
        entry = by_app.setdefault(app, {"rows": 0, "between_presents": [], "gpu_time": [],
                                        "cpu_busy": [], "gpu_wait": []})
        entry["rows"] += 1
        for key, names in ALIASES.items():
            value = None
            for name in names:
                if name in row:
                    value = _f(row.get(name))
                    break
            entry[key].append(value)

    picked = None
    if process_name:
        stem = process_name.lower()
        if stem.endswith(".exe"):
            stem = stem[:-4]
        for app in by_app:
            if app.lower().split(".exe")[0] == stem:
                picked = app
                break
    if picked is None and by_app:
        picked = max(by_app, key=lambda a: by_app[a]["rows"])

    result = {"schema": "perf-presentmon/0", "csv": os.path.basename(csv_path),
              "fields": fields, "columns": len(fields), "total_rows": len(rows),
              "process_name": process_name, "picked": picked, "apps": {}}
    for app, entry in sorted(by_app.items(), key=lambda kv: -kv[1]["rows"])[:10]:
        result["apps"][app] = {
            "rows": entry["rows"],
            "ms_between_presents": stats(entry["between_presents"]),
            "ms_gpu_time": stats(entry["gpu_time"]),
            "ms_cpu_busy": stats(entry["cpu_busy"]),
            "ms_gpu_wait": stats(entry["gpu_wait"]),
        }
    return result


def compare(summary, trace_p50_ms, trace_n=None):
    """生命周期级对比：PresentMon 的 msBetweenPresents p50 vs trace 帧间隔 p50。"""
    picked = summary.get("picked")
    app = (summary.get("apps") or {}).get(picked) or {}
    pm = (app.get("ms_between_presents") or {})
    pm_p50 = pm.get("p50")
    if pm_p50 is None or trace_p50_ms is None or not trace_p50_ms:
        return {"verdict": "unknown", "presentmon_p50": pm_p50, "trace_p50": trace_p50_ms,
                "presentmon_frames": pm.get("n"), "reason": "一侧没有数据"}
    diff = 100.0 * (pm_p50 - float(trace_p50_ms)) / float(trace_p50_ms)
    if abs(diff) <= AGREE_PCT:
        verdict = "agree"
    elif abs(diff) <= WARN_PCT:
        verdict = "warn"
    else:
        verdict = "disagree"
    return {"verdict": verdict, "presentmon_p50": pm_p50, "trace_p50": round(float(trace_p50_ms), 4),
            "diff_pct": round(diff, 3), "presentmon_frames": pm.get("n"), "trace_frames": trace_n,
            "note": "生命周期级对比（含启动/退出段）；不是逐帧对齐"}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="PresentMon 采集/解析（M5.3）")
    ap.add_argument("csv", help="输出 CSV 路径（采集模式）或已存在的 CSV（--parse）")
    ap.add_argument("--parse", action="store_true", help="只解析已有 CSV，不采集")
    ap.add_argument("--process", default=None, help="进程名（如 UnrealEditor-Cmd.exe）")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--exe", default=None)
    ap.add_argument("--out", default=None, help="解析结果 JSON（默认 <csv>.json）")
    a = ap.parse_args(argv)

    if not a.parse:
        if not available(a.exe):
            print("[error] PresentMon not found: %s（用 PRESENTMON_EXE 覆盖）" % exe_path(a.exe))
            return 2
        proc, cmd = start(a.csv, a.process, a.seconds, a.exe)
        print("[presentmon] started pid=%d: %s" % (proc.pid, " ".join(cmd)))
        rc, err = stop(proc)
        print("[presentmon] exit=%s %s" % (rc, ("stderr: " + err) if err else ""))
        if not os.path.isfile(a.csv):
            print("[error] no CSV written")
            return 1

    summary = summarize(a.csv, a.process)
    out = a.out or (os.path.splitext(a.csv)[0] + ".json")
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    picked = summary.get("picked")
    app = (summary.get("apps") or {}).get(picked) or {}
    bp = app.get("ms_between_presents") or {}
    print("[presentmon] rows=%d columns=%d picked=%s frames=%s p50=%s mean=%s"
          % (summary["total_rows"], summary["columns"], picked, bp.get("n"),
             bp.get("p50"), bp.get("mean")))
    print("[ok] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
