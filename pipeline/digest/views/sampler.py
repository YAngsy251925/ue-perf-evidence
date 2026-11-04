#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sampler.py — M5.1b：进程内每帧采样（校准 bound 判定的等待 scope 清单）

为什么需要它：M5.1 的 busy 是**从 trace 推导**的（帧间隔 − 等待类 scope 的 self 之和）。推导依赖
"等待类清单完整"；漏一项就会把等待算成忙碌，把"在等 GPU/在等锁"误判成"CPU 很忙"。

引擎自己有一组权威计数（`RenderTimer.h` 的 uint32 cycle 计数器，每帧在 FViewport::Draw 里更新）：

  GGameThreadTime / GRenderThreadTime        该线程本帧用了多少 cycle（**不含空闲**）
  GGameThreadWaitTime / GRenderThreadWaitTime 该线程本帧等了多久
  RHIGetGPUFrameCycles()                      GPU 本帧 cycle

采样器把这些每帧落成 CSV，`perfctl digest bound --sampler` 把"trace 推导的 busy/wait"与
"引擎自报的 busy/wait"并排放，差值就是清单可能漏掉的部分。

**PIE 与 standalone 必须分开校准**（PLAN §5.1b）：PIE 下多一层编辑器 tick，计数含编辑器开销。
本模块只做读取与对照，采集由插件写 <Project>/Saved/Profiling/Sampler/*.csv（capture 负责收进 run 目录）。

用法：
  perfctl digest bound <run_dir> --out DIR [--b2] [--sampler]
  python -m pipeline.digest.views.sampler <run_dir> --out DIR   # 单看采样器
"""
import argparse
import io
import json
import os
import sys

COLUMNS = ("frame", "game_ms", "render_ms", "game_wait_ms", "render_wait_ms", "gpu_ms")


def find_files(run_dir):
    out = []
    base = os.path.join(run_dir, "sampler")
    if not os.path.isdir(base):
        return out
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.lower().endswith(".csv"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def read_csv(path):
    """读采样 CSV → (rows, columns)。列名容忍顺序变化，按名字取值。"""
    import csv as _csv
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = _csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = []
        for raw in reader:
            row = {key: _num(raw.get(key)) for key in COLUMNS if key in raw}
            if row.get("frame") is None:
                continue
            rows.append(row)
    return rows, columns


def summarize(rows):
    """逐帧采样的统计：均值/p50/max，以及"busy = 本帧时间 − 等待"的推导值。"""
    def col(name):
        return [r[name] for r in rows if r.get(name) is not None]

    def stats(values):
        if not values:
            return {"n": 0, "mean": None, "p50": None, "max": None}
        ordered = sorted(values)
        n = len(ordered)
        return {"n": n,
                "mean": round(sum(ordered) / n, 4),
                "p50": round(ordered[min(n - 1, int(round(0.5 * (n - 1))))], 4),
                "max": round(ordered[-1], 4)}

    game, render, gpu = col("game_ms"), col("render_ms"), col("gpu_ms")
    gwait, rwait = col("game_wait_ms"), col("render_wait_ms")
    # 引擎的 GGameThreadTime 注释是"excluding idle time"（RenderTimer.h），所以它本身就是 busy；
    # 不再减 wait（那样会重复扣除）。wait 单独报，用于和 trace 的等待类自耗时对照。
    busy_game = list(game)
    return {
        "rows": len(rows),
        "game_thread_ms": stats(game),
        "render_thread_ms": stats(render),
        "gpu_ms": stats(gpu),
        "game_wait_ms": stats(gwait),
        "render_wait_ms": stats(rwait),
        "game_busy_derived_ms": stats(busy_game),
    }


def compare(sampler_summary, bound_lines):
    """把采样均值与 bound 视图推导的 busy/wait 并排。

    bound_lines: [{"line": "GameThread", "busy_per_frame_ms":…, "wait_per_frame_ms":…}, …]
    返回 {"lines": [{line, trace_busy_ms, sampler_busy_ms, diff_ms, diff_pct, verdict}]}
    """
    # bound 视图的线标签是 GT / RT / RHI / GPU（不是线程全名），这里做一次归一化
    alias = {"gt": "gt", "gamethread": "gt", "rt": "rt", "renderthread": "rt",
             "rhi": "rhi", "rithread": "rhi", "gpu": "gpu"}
    by_name = {}
    for line in (bound_lines or []):
        key = alias.get(str(line.get("line") or "").strip().lower())
        if key:
            by_name[key] = line
    out = []
    pairs = (("GT", "game_thread_ms", "game_busy_derived_ms"),
             ("RT", "render_thread_ms", "render_busy_derived_ms"),
             ("GPU", "gpu_ms", "gpu_busy_derived_ms"))
    for label, own_key, busy_key in pairs:
        trace_line = by_name.get(label.lower())
        if not trace_line or trace_line.get("busy_per_frame_ms") is None:
            continue
        sampler_busy = (sampler_summary.get(busy_key) or sampler_summary.get(own_key) or {}).get("mean")
        if sampler_busy is None:
            continue
        trace_busy = float(trace_line["busy_per_frame_ms"])
        diff = sampler_busy - trace_busy
        pct = (100.0 * diff / trace_busy) if trace_busy else None
        if pct is None:
            verdict = "unknown"
        elif abs(pct) <= 5.0:
            verdict = "agree"
        elif abs(pct) <= 15.0:
            verdict = "warn"
        else:
            verdict = "disagree"
        out.append({"line": label, "trace_busy_ms": round(trace_busy, 3),
                    "sampler_busy_ms": round(sampler_busy, 3),
                    "diff_ms": round(diff, 3),
                    "diff_pct": None if pct is None else round(pct, 2),
                    "verdict": verdict,
                    "meaning": ("差值明显为正 → trace 推导的 busy 偏小，等待清单可能把该算忙碌的 scope 归成了等待"
                                if diff < 0 else
                                "差值明显为正 → trace 推导的 busy 偏大，等待清单可能漏了若干等待 scope")})
    return {"lines": out}


def slice_to_window(rows, window_frames):
    """把采样行切到与 trace 统计窗口相当的区间。

    采样器的 `frame` 是它自己的计数（从第一次 Tick 起），trace 的帧号是 Game Frames 轨道索引，
    两者不是同一序列。唯一稳的共同点是"都结束在 run 结束"，所以按**结尾对齐**取最后 window_frames 行。
    这个近似会带来 ±1 帧的边界误差，对均值级对照够用；需要逐帧对齐请用 digest crosscheck 那套。
    """
    if not window_frames or window_frames <= 0 or len(rows) <= window_frames:
        return rows, "whole_run"
    return rows[-int(window_frames):], "tail_aligned"


def load(run_dir, window_frames=None):
    """读 run 目录里的采样 CSV（多份就取行数最多的那份）。没有则 None。"""
    files = find_files(run_dir)
    if not files:
        return None
    best = None
    for path in files:
        rows, columns = read_csv(path)
        if best is None or len(rows) > best["rows"]:
            best = {"file": os.path.basename(path), "columns": columns, "rows": len(rows),
                    "rows_raw": rows}
    sliced, basis = slice_to_window(best["rows_raw"], window_frames)
    best["rows_used"] = len(sliced)
    best["alignment"] = basis
    best["summary"] = summarize(sliced)
    return best


def render_md(result, thread_note=True):
    lines = ["# 每帧采样器（M5.1b）", ""]
    if not result:
        lines += ["没有 sampler/*.csv（profile 没开 `PerfEvidence.Sampler 1`，或采集时没落盘）。", ""]
        return "\n".join(lines)
    s = result["summary"]
    lines += ["| 项 | 均值 ms | p50 ms | 最大 ms | n |", "|---|---|---|---|---|"]
    for key, label in (("game_thread_ms", "GameThread 本帧用时"),
                       ("game_wait_ms", "GameThread 等待"),
                       ("render_thread_ms", "RenderThread 本帧用时"),
                       ("render_wait_ms", "RenderThread 等待"),
                       ("gpu_ms", "GPU 帧用时")):
        st = s.get(key) or {}
        if st.get("n"):
            lines.append("| %s | %.3f | %.3f | %.3f | %s |"
                         % (label, st["mean"], st["p50"], st["max"], st["n"]))
    lines.append("")
    lines += ["| 对齐 | %s（用 %s / %s 行） |" % (result.get("alignment"),
                                                  result.get("rows_used"), result.get("rows")), ""]
    if thread_note:
        lines += ["> 口径：`GGameThreadTime` / `GRenderThreadTime` / `RHIGetGPUFrameCycles()` 是引擎每帧更新的"
                  " cycle 计数器（`RenderTimer.h`），**不含空闲**；PIE 与 standalone 含的开销不同，必须分开校准。", ""]
    return "\n".join(lines)


def render_calibration(section):
    """bound 视图末尾的校准段：trace 推导 vs 引擎自报。"""
    lines = ["", "## 采样器校准（M5.1b）", ""]
    if not section or not section.get("available"):
        lines += ["没有 `sampler/*.csv`：profile 里加 `exec_cmds \"PerfEvidence.Sampler 1\"` 再采一次。", ""]
        return "\n".join(lines)
    s = section.get("summary") or {}
    rows = (section.get("compare") or {}).get("lines") or []
    if not rows:
        lines += ["采样文件 %s（%s 帧），但没有可对照的 bound 线。" % (section.get("file"), section.get("rows")), ""]
        return "\n".join(lines)
    lines += ["| 线 | trace 推导 busy/帧 | 采样器 busy/帧 | 差 | 判定 |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %.3f ms | %.3f ms | %+.3f ms（%s） | **%s** |"
                     % (r["line"], r["trace_busy_ms"], r["sampler_busy_ms"], r["diff_ms"],
                        "-" if r["diff_pct"] is None else "%+.1f%%" % r["diff_pct"], r["verdict"]))
    lines.append("")
    if any(r["verdict"] == "disagree" for r in rows):
        lines += ["> 偏差超 15%：说明等待 scope 清单可能不完整（trace 推导的 busy 偏大），"
                  "按 PLAN §12 该更新 `WAIT_SCOPES` 并跑 golden 回归。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="每帧采样器视图（M5.1b）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    result = load(run_dir)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "sampler.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")
    if result:
        with io.open(os.path.join(a.out, "sampler.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": "perf-sampler/0", "file": result["file"],
                       "rows": result["rows"], "summary": result["summary"]},
                      f, ensure_ascii=False, indent=1)
        s = result["summary"]["game_thread_ms"]
        print("[sampler] %s：%d 帧，GameThread 均值 %s ms，GPU 均值 %s ms"
              % (result["file"], result["rows"], s.get("mean"),
                 (result["summary"]["gpu_ms"] or {}).get("mean")))
    else:
        print("[sampler] 没有采样数据（profile 里加 exec_cmds \"PerfEvidence.Sampler 1\"）")
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "sampler.md"))
    return 0 if result else 3


if __name__ == "__main__":
    sys.exit(main())
