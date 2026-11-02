#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""windows.py — 把 trace 时间轴切成口径窗口，并排除预热帧（PLAN R2、R3，M1.3）

以参考帧序列（frame_probe.pick_reference，R4 顺序）的有限帧为界，W 为预热帧数（默认 60）：
  startup   [0, 首帧开始]                Insights 的时间轴从 0 开始
  frames    [首帧开始, 末帧结束]          成帧段
  warmup    [首帧开始, 第 W+1 帧开始]     预热帧，不进入统计（R3）
  steady    [第 W+1 帧开始, 末帧结束]     稳态窗口，默认统计窗口（R2、R3）
  shutdown  [末帧结束, trace 结束]        trace 结束取 insights.log 报告的会话时长
边界只看参考序列。其他帧序列在边界外最多多出不到 1 帧：coexist_D 实测最后一个渲染帧比最后一个游戏帧
晚 1.4ms 结束，对秒级窗口的统计影响可以忽略。
会话时长有舍入误差（见 insights_log.py）。被强制结束的 trace 没有退出段，末帧结束时间可能略晚于
报告的时长：落在误差范围内时以末帧结束时间为 trace 结束；超出误差范围才写 warning。
参考序列的非有限帧与重叠帧只在 window_basis 里计数，对应的 warning 由 frame_semantics 按侧写出，避免重复。
"""
from pipeline.caliber.frame_probe import OVERLAP_EPS, count_overlaps

DEFAULT_WARMUP_FRAMES = 60
WINDOW_NAMES = ("startup", "frames", "warmup", "steady", "shutdown")


def resolve_trace_end(duration, last_end, basis: dict, warnings: list):
    if duration is None:
        warnings.append("trace end unknown: no session duration in insights.log; shutdown window not computed")
        return None
    end, clamped = duration["seconds"], False
    if last_end is not None and last_end > end:
        if last_end > duration["bounds"][1] + OVERLAP_EPS:
            warnings.append("last frame ends at %.7fs, after the session duration %s reported by Insights"
                            % (last_end, duration["text"]))
        end, clamped = last_end, True
    basis["trace_end"] = {"seconds": end, "source": "insights_log", "log_text": duration["text"],
                          "log_bounds": duration["bounds"], "clamped_to_last_frame": clamped}
    return end


def split_windows(reference, duration, warmup_frames: int = DEFAULT_WARMUP_FRAMES) -> dict:
    """reference：frame_probe 的参考序列，可以是 None；duration：insights_log 的解析结果，可以是 None。
    返回 {"windows": {...}, "basis": {...}, "warnings": [...]}。"""
    windows = {name: None for name in WINDOW_NAMES}
    warnings = []
    frames = reference["frames"] if reference else []
    basis = {
        "reference": None if reference is None else {
            "side": reference["side"], "source": reference["source"],
            "threads": list(reference["threads"]), "timer": reference["timer"]},
        "frames_total": len(frames),
        "warmup_frames_requested": warmup_frames,
        "frames_warmup": 0,
        "frames_steady": 0,
        "nonfinite_dropped": len(reference["nonfinite"]) if reference else 0,
        "overlapping_frames": count_overlaps(frames),
        "stats_window": None,
        "trace_end": None,
    }
    if not frames:
        warnings.append("no frame series in the trace: frames / warmup / steady windows not computed")
        end = resolve_trace_end(duration, None, basis, warnings)
        if end is not None:
            windows["startup"] = [0.0, end]
        return {"windows": windows, "basis": basis, "warnings": warnings}

    n = len(frames)
    w = min(warmup_frames, n)
    first_start = frames[0][0]
    last_end = max(f[1] for f in frames)
    windows["startup"] = [0.0, first_start]
    windows["frames"] = [first_start, last_end]
    if w > 0:
        windows["warmup"] = [first_start, frames[w][0] if w < n else last_end]
    if w < n:
        windows["steady"] = [frames[w][0], last_end]
        basis["stats_window"] = "steady"
    else:
        warnings.append("only %d frame(s), all inside the %d-frame warmup: no steady window" % (n, warmup_frames))
    basis["frames_warmup"], basis["frames_steady"] = w, n - w

    end = resolve_trace_end(duration, last_end, basis, warnings)
    if end is not None:
        windows["shutdown"] = [last_end, end]
    return {"windows": windows, "basis": basis, "warnings": warnings}
