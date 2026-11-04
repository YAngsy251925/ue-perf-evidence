#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crosscheck.py — M5.2：CSV Profiler / trace / B2 三路帧时间与 GPU 时间的交叉校验

用法：
  perfctl digest crosscheck <run_dir> --out DIR [--b2] [--window coverage|stats]

比什么：
  1. **CSV Profiler**（`<run_dir>/csv/*.csv`，只在 coverage_probe 段采）：`FrameTime` 与 `GPUTime`（毫秒）的
     n / mean / p50 / max。注意 CSV 只覆盖探针段，所以对照的 trace 也只看探针窗口。
  2. **trace**：同一窗口内 Game 帧轨道的 interval（R15），给 n / mean / p50。
  3. **B2 GPU**（`<run_dir>/b2/b2_gpu.json`，M4.2 的 `gpu` mode 缓存）：Graphics 队列 busy，换算成每帧毫秒。

判定（三路一致性）：
  - 帧时间：|CSV FrameTime mean − trace interval mean| / trace mean
  - GPU：|CSV GPUTime mean − B2 Graphics busy/帧| / B2 每帧毫秒
  ≤10% 记 agree，10–25% 记 warn，>25% 记 disagree。任一来源缺失就只报有数据的部分，不猜。
"""
import argparse
import csv
import io
import json
import math
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AGREE_PCT = 10.0
WARN_PCT = 25.0


def _finite(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _stats(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0, "mean": None, "p50": None, "max": None}
    ordered = sorted(clean)
    mid = len(ordered) // 2
    p50 = ordered[mid] if len(ordered) % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])
    return {"n": len(clean), "mean": round(sum(clean) / len(clean), 4),
            "p50": round(p50, 4), "max": round(ordered[-1], 4)}


def newest_csv(run_dir):
    csv_dir = os.path.join(run_dir, "csv")
    if not os.path.isdir(csv_dir):
        return None
    names = [n for n in sorted(os.listdir(csv_dir)) if n.lower().endswith(".csv")]
    if not names:
        return None
    return os.path.join(csv_dir, names[-1])


def read_csv_profiler(path):
    """读 CSV Profiler：第一行是列名（EVENTS, ...），之后每行一帧。

    返回 {"frame_time": [...], "gpu_time": [...], "rows": N, "columns": M}。
    FrameTime / GPUTime 缺列时给空列表（不同图表配置列会变）。
    """
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {"frame_time": [], "gpu_time": [], "rows": 0, "columns": 0}
        idx = {}
        for i, name in enumerate(header):
            key = (name or "").strip()
            if key in ("FrameTime", "GPUTime"):
                idx[key] = i
        frame_time, gpu_time, rows = [], [], 0
        for row in reader:
            if not row or len(row) < 2:
                continue
            rows += 1
            for key, target in (("FrameTime", frame_time), ("GPUTime", gpu_time)):
                i = idx.get(key)
                if i is not None and i < len(row):
                    v = _finite(row[i])
                    if v is not None:
                        target.append(v)
    return {"frame_time": frame_time, "gpu_time": gpu_time,
            "rows": rows, "columns": len(header)}


def load_window(run_dir, kind="coverage"):
    """窗口来源：coverage.json 的探针段（默认），或 manifest 的 stats 窗口。"""
    if kind == "coverage":
        path = os.path.join(run_dir, "coverage.json")
        if os.path.isfile(path):
            with io.open(path, encoding="utf-8") as f:
                cov = (json.load(f) or {}).get("coverage") or {}
            if cov.get("start") is not None and cov.get("end") is not None:
                return [float(cov["start"]), float(cov["end"])], "coverage_probe"
    path = os.path.join(run_dir, "manifest.json")
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        windows = manifest.get("windows") or {}
        win = windows.get("stats") or windows.get("steady")
        if win:
            return [float(win[0]), float(win[1])], "manifest." + ("stats" if windows.get("stats") else "steady")
    return None, None


def trace_frame_intervals(run_dir, window=None):
    """窗口内的 game 帧 interval（毫秒）；直接用 extract/frame_tracks.csv。"""
    from pipeline.caliber import frame_probe
    extract = os.path.join(run_dir, "extract")
    if not os.path.isdir(extract):
        return []
    try:
        series = frame_probe.load_frame_series(extract)
    except OSError:
        return []   # extract 不完整（缺 frame_scopes.csv 等）：只报"trace 侧缺数据"，不炸
    ref = frame_probe.pick_reference(series)
    if not ref:
        return []
    frames = ref["frames"]
    intervals = []
    for i in range(1, len(frames)):
        start, prev_end = frames[i][0], None
        prev = frames[i - 1][0]
        if window and (start < window[0] or start > window[1]):
            continue
        delta = (start - prev) * 1000.0
        if delta > 0 and math.isfinite(delta):
            intervals.append(delta)
    return intervals


def load_b2_gpu(run_dir):
    path = os.path.join(run_dir, "b2", "b2_gpu.json")
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def verdict(diff_pct):
    if diff_pct is None:
        return "unknown"
    if abs(diff_pct) <= AGREE_PCT:
        return "agree"
    if abs(diff_pct) <= WARN_PCT:
        return "warn"
    return "disagree"


def build(run_dir, window_kind="coverage"):
    window, window_basis = load_window(run_dir, window_kind)
    csv_path = newest_csv(run_dir)
    csv_block = read_csv_profiler(csv_path) if csv_path else None

    result = {
        "schema": "perf-crosscheck/0",
        "run_dir": os.path.abspath(run_dir).replace("\\", "/"),
        "window": window, "window_basis": window_basis,
        "csv": None, "trace": None, "b2_gpu": None, "compare": {},
        "warnings": [],
    }
    if csv_path:
        result["csv"] = {"file": os.path.basename(csv_path), "rows": csv_block["rows"],
                         "columns": csv_block["columns"],
                         "frame_time_ms": _stats(csv_block["frame_time"]),
                         "gpu_time_ms": _stats(csv_block["gpu_time"])}
    else:
        result["warnings"].append("run 目录里没有 csv/*.csv（capture 未归档或该 run 没开 CSV Profiler）")

    if window:
        intervals = trace_frame_intervals(run_dir, window)
        result["trace"] = {"frame_interval_ms": _stats(intervals)}
    else:
        result["warnings"].append("没有 coverage/manifest 窗口，trace 侧无法对齐")

    gpu = load_b2_gpu(run_dir)
    if gpu:
        intervals_n = (result["trace"] or {}).get("frame_interval_ms", {}).get("n")
        frames = (intervals_n + 1) if intervals_n else None
        busy_s = gpu.get("gpu_busy_s")
        per_frame = None
        if frames and busy_s is not None:
            per_frame = 1000.0 * float(busy_s) / frames
        result["b2_gpu"] = {"file": "b2/b2_gpu.json", "graphics_busy_s": busy_s,
                            "window_s": gpu.get("span_s"), "window": gpu.get("window"),
                            "frames": frames,
                            "gpu_ms_per_frame": None if per_frame is None else round(per_frame, 4)}
    else:
        result["warnings"].append("没有 b2/b2_gpu.json（先跑 perfctl b2 gpu 或 digest gpu --b2）")

    csv_ft = (result["csv"] or {}).get("frame_time_ms", {}).get("mean")
    tr_ft = (result["trace"] or {}).get("frame_interval_ms", {}).get("mean")
    csv_gpu = (result["csv"] or {}).get("gpu_time_ms", {}).get("mean")
    b2_gpu = (result["b2_gpu"] or {}).get("gpu_ms_per_frame")

    def compare(csv_v, trace_v, unit):
        if csv_v is None or trace_v is None or not trace_v:
            return {"csv": csv_v, "trace": trace_v, "diff_pct": None, "verdict": "unknown"}
        diff = 100.0 * (float(csv_v) - float(trace_v)) / float(trace_v)
        return {"csv": round(float(csv_v), 4), "trace": round(float(trace_v), 4),
                "diff_pct": round(diff, 3), "verdict": verdict(diff), "unit": unit}

    # PresentMon（M5.3）：生命周期级对比（整条 trace 的帧间隔 p50 vs PresentMon 的 p50）
    pm_path = os.path.join(run_dir, "presentmon", "presentmon.csv")
    result["presentmon"] = None
    if os.path.isfile(pm_path):
        from pipeline.capture import presentmon
        summary = presentmon.summarize(pm_path, "UnrealEditor-Cmd.exe")
        all_intervals = trace_frame_intervals(run_dir, None)
        result["presentmon"] = {
            "file": "presentmon/presentmon.csv",
            "app": summary.get("picked"),
            "columns": summary.get("columns"),
            "apps": {k: {"rows": v["rows"],
                         "ms_between_presents": v["ms_between_presents"]}
                     for k, v in (summary.get("apps") or {}).items()},
            "compare": presentmon.compare(summary, _stats(all_intervals)["p50"], len(all_intervals)),
        }
    else:
        result["warnings"].append("没有 presentmon/presentmon.csv（capture 未启用或工具缺失）")

    result["compare"]["frame_time"] = compare(csv_ft, tr_ft, "ms")
    result["compare"]["gpu_time"] = compare(csv_gpu, b2_gpu, "ms")
    return result


def render_md(r):
    lines = ["# 三路交叉校验 — %s" % r["run_dir"], ""]
    if r["window"]:
        lines += ["窗口：`%s` = [%.4f, %.4f]（%.2fs）" % (r["window_basis"], r["window"][0],
                                                          r["window"][1],
                                                          r["window"][1] - r["window"][0]), ""]
    lines += ["## 三路数据", "",
              "| 来源 | 帧时间 mean / p50 / max (ms) | GPU mean / p50 (ms) | 备注 |",
              "|---|---|---|---|"]
    csv_blk = r.get("csv") or {}
    tr_blk = (r.get("trace") or {}).get("frame_interval_ms") or {}
    b2_blk = r.get("b2_gpu") or {}

    def tri(block):
        if not block or not block.get("n"):
            return "-"
        return "%.2f / %.2f / %.2f" % (block["mean"], block["p50"], block["max"])

    def pair(block):
        if not block or not block.get("n"):
            return "-"
        return "%.2f / %.2f" % (block["mean"], block["p50"])

    lines.append("| CSV Profiler | %s | %s | %s（%d 行 × %d 列） |"
                 % (tri(csv_blk.get("frame_time_ms")), pair(csv_blk.get("gpu_time_ms")),
                    csv_blk.get("file", "-"), csv_blk.get("rows", 0), csv_blk.get("columns", 0)))
    lines.append("| trace（Game 帧轨道） | %s | - | interval（R15） |" % tri(tr_blk))
    lines.append("| B2 GPU（Graphics） | - | %s | busy %.2fs / 窗口 %.2fs |"
                 % ("%.2f" % b2_blk["gpu_ms_per_frame"] if b2_blk.get("gpu_ms_per_frame") else "-",
                    b2_blk.get("graphics_busy_s") or 0.0, b2_blk.get("window_s") or 0.0))
    lines.append("")
    lines += ["## 一致性", "",
              "口径提醒：**帧时间**两边是同一件事（CSV 的 FrameTime 与 trace 的帧间隔），应当逐位接近；"
              "**GPU** 两边不是同一件事——CSV `GPUTime` 是 RHI 报的每帧 GPU 时间，B2 是 Graphics 队列"
              "占用（busy/帧数）。异步 Compute 常驻时后者会明显偏大，偏差只作信息量，不作判决。", "",
              "| 指标 | CSV | trace/B2 | 偏差 | 判定 |", "|---|---|---|---|---|"]
    for key, label in (("frame_time", "帧时间"), ("gpu_time", "GPU 时间")):
        c = r["compare"].get(key) or {}
        lines.append("| %s | %s | %s | %s | **%s** |"
                     % (label,
                        "-" if c.get("csv") is None else "%.3f ms" % c["csv"],
                        "-" if c.get("trace") is None else "%.3f ms" % c["trace"],
                        "-" if c.get("diff_pct") is None else "%+.2f%%" % c["diff_pct"],
                        c.get("verdict", "unknown")))
    lines.append("")
    pm = r.get("presentmon") or {}
    cmp = pm.get("compare") or {}
    lines += ["## PresentMon（第三方仲裁，生命周期级）", ""]
    if pm:
        lines.append("- 应用：`%s`；PresentMon 帧数 %s；CSV 列数 %s"
                     % (pm.get("app"), cmp.get("presentmon_frames"), pm.get("columns")))
        lines.append("- p50：PresentMon %s ms vs trace 整条 %s ms → **%s**（%s）"
                     % (cmp.get("presentmon_p50"), cmp.get("trace_p50"), cmp.get("verdict"),
                        "-" if cmp.get("diff_pct") is None else "%+.2f%%" % cmp["diff_pct"]))
        lines.append("- 口径：PresentMon 覆盖整个进程生命周期（含启动/退出段），不做逐帧对齐")
    else:
        lines.append("- 没有 PresentMon 数据")
    lines.append("")
    if r["warnings"]:
        lines += ["## 警告"] + ["- %s" % w for w in r["warnings"]] + [""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="CSV / trace / B2 三路交叉校验（M5.2）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--window", default="coverage", choices=("coverage", "stats"),
                    help="对齐窗口：coverage（CSV 实际采集段，默认）或 stats（统计窗口）")
    ap.add_argument("--b2", action="store_true",
                    help="没有 b2/b2_gpu.json 时先跑一次 perfctl b2 gpu（冷启动约 17s）")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    if not os.path.isdir(run_dir):
        print("[error] run dir not found: %s" % run_dir)
        return 1
    if a.b2 and not os.path.isfile(os.path.join(run_dir, "b2", "b2_gpu.json")):
        trace = os.path.join(run_dir, "trace.utrace")
        if os.path.isfile(trace):
            from pipeline.extract import b2
            print("[crosscheck] b2 gpu 未缓存，先跑一次 ...")
            win, _basis = load_window(run_dir, a.window)
            ok, _ = b2.run_mode("gpu", trace, os.path.join(run_dir, "b2", "b2_gpu.json"),
                                limit=0, start=win[0] if win else None,
                                end=win[1] if win else None)
            if not ok:
                print("[crosscheck] b2 gpu 失败，GPU 那一路会缺失")

    result = build(run_dir, a.window)
    md = render_md(result)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "crosscheck.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "crosscheck.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(md + "\n")
    ft = result["compare"]["frame_time"]
    gpu = result["compare"]["gpu_time"]
    print("[crosscheck] 帧时间 CSV=%s trace=%s 偏差=%s 判定=%s"
          % (ft.get("csv"), ft.get("trace"),
             "-" if ft.get("diff_pct") is None else "%+.2f%%" % ft["diff_pct"], ft.get("verdict")))
    print("[crosscheck] GPU    CSV=%s B2=%s 偏差=%s 判定=%s"
          % (gpu.get("csv"), gpu.get("trace"),
             "-" if gpu.get("diff_pct") is None else "%+.2f%%" % gpu["diff_pct"], gpu.get("verdict")))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "crosscheck.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
