#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""frame_semantics.py — 帧口径：GT / RT 分列与统计窗口内的精确分位数（PLAN R4、R8、R11、R13、R15，M1.4）

每一侧（game / render）按 R4 的顺序选数据源：帧轨道，其次是该侧线程上的 Frame scope。
B2 的 IFrameProvider 作为第三级在 M4.2 接入；现在两级都空就记 source = none 并写 warning，
nullrhi run 的 render 侧本来就没有帧，不写。

统计只在 window_basis.stats_window（默认 steady）里做；一帧属于窗口的条件是开始时间落在 [起点, 终点)。
时间点（start）的单位是秒，统计量的单位是毫秒，保留 4 位小数（导出精度 1e-7 秒）：
  duration_ms  帧事件自身的时长（导出的 Duration 列，与结束减开始一致）
  interval_ms  相邻两帧开始时间之差，即帧节奏
  gap_ms_p50   上一帧结束到下一帧开始的间隔的中位数；contiguous = gap_ms_p50 不超过 interval p50 的 5%
帧首尾相接时 duration 与 interval 一致；不相接时 duration 只是该线程每帧的忙碌跨度，帧时间要看
interval（R15。coexist_D 实测：渲染帧 duration p50 2.99ms，interval p50 16.67ms）。
分位数用最近秩法：排序后取第 ceil(p*n/100) 个值，结果总是某一帧的真实值，可以回溯到具体帧。
秩用整数运算，避免 0.9*100 = 90.00000000000001 这类浮点误差把 p90 推到下一名。
run 不可测量（measurable = false，如 nullrhi）时只给来源与帧数，统计记 null 并写明原因（R11）。
consistency 对应 §10.1 的 Q3：两侧都有帧时，行数差与统计窗口内的帧数差都不超过 2。
"""
import math

from pipeline.caliber.frame_probe import OVERLAP_EPS, count_overlaps

SIDES = ("game", "render")
SOURCE_ORDER = ("frame_track", "frame_scope")
PERCENTILES = (50, 90, 95, 99)
PERCENTILE_METHOD = "nearest_rank"
MS_DIGITS = 4
CONTIGUOUS_GAP_RATIO = 0.05
Q3_MAX_DELTA = 2


def round_ms(value: float) -> float:
    return round(value, MS_DIGITS) + 0.0  # + 0.0 把 -0.0 变成 0.0


def nearest_rank(sorted_values, p: int):
    """最近秩分位数：第 ceil(p*n/100) 个值（从 1 起算）。p 是整数百分位。"""
    rank = max(1, -(-p * len(sorted_values) // 100))
    return sorted_values[rank - 1]


def summarize(values_ms):
    if not values_ms:
        return None
    v = sorted(values_ms)
    out = {"n": len(v)}
    for p in PERCENTILES:
        out["p%d" % p] = round_ms(nearest_rank(v, p))
    out["max"] = round_ms(v[-1])
    out["mean"] = round_ms(sum(v) / len(v))
    return out


def in_window(frames, window):
    lo, hi = window
    return [f for f in frames if lo <= f[0] < hi]


def choose_series(series: dict, side: str):
    """R4：先找有有限帧的来源；都没有时退回第一个有行的来源（全是非有限值），再没有就是 None。"""
    candidates = [series[(side, source)] for source in SOURCE_ORDER]
    for s in candidates:
        if s["frames"]:
            return s
    for s in candidates:
        if s["rows"]:
            return s
    return None


def track_index_summary(s: dict):
    if s["source"] != "frame_track":
        return None
    idx = [f[3] for f in s["frames"] if f[3] is not None] + [i for _, i in s["nonfinite"] if i is not None]
    if not idx:
        return None
    unique = len(set(idx))
    return {"first": min(idx), "last": max(idx),
            "gaps": max(idx) - min(idx) + 1 - unique, "duplicates": len(idx) - unique}


def frame_stats(inside, window_name: str) -> dict:
    durations = [f[2] * 1000.0 for f in inside]
    intervals = [(inside[i][0] - inside[i - 1][0]) * 1000.0 for i in range(1, len(inside))]
    gaps = sorted((inside[i][0] - inside[i - 1][1]) * 1000.0 for i in range(1, len(inside)))
    interval = summarize(intervals)
    gap_p50 = round_ms(nearest_rank(gaps, 50)) if gaps else None
    contiguous = None if interval is None else gap_p50 <= CONTIGUOUS_GAP_RATIO * interval["p50"]
    longest = max(inside, key=lambda f: (f[2], -f[0]))  # 最长的一帧；一样长时取最早的
    return {"window": window_name, "duration_ms": summarize(durations), "interval_ms": interval,
            "gap_ms_p50": gap_p50, "contiguous": contiguous,
            "max_frame": {"start": longest[0], "track_index": longest[3]}}


def side_semantics(series, side, windows, basis, measurable, measurable_reason, rhi_state):
    warnings = []
    s = choose_series(series, side)
    if s is None:
        out = {"source": "none", "threads": [], "timer": None, "rows": 0, "frames": 0, "nonfinite_dropped": 0,
               "overlapping_frames": 0, "track_index": None, "window_frames": 0, "stats": None,
               "stats_withheld": "no %s frames" % side}
        if side == "render" and rhi_state == "null":
            out["stats_withheld"] = "no render frames (expected for a nullrhi run)"
        else:
            warnings.append("no %s frames in the trace: frame track and Frame scopes are both empty "
                            "(R4; the frame_provider fallback arrives in M4.2)" % side)
        return out, warnings

    frames = s["frames"]
    window_name = basis.get("stats_window")
    window = windows.get(window_name) if window_name else None
    inside = in_window(frames, window) if window else []
    out = {"source": s["source"], "threads": list(s["threads"]), "timer": s["timer"],
           "rows": s["rows"], "frames": len(frames), "nonfinite_dropped": len(s["nonfinite"]),
           "overlapping_frames": count_overlaps(frames), "track_index": track_index_summary(s),
           "window_frames": len(inside), "stats": None, "stats_withheld": None}
    if not measurable:
        out["stats_withheld"] = "not measurable: %s" % measurable_reason
    elif window is None:
        out["stats_withheld"] = "no stats window"
    elif not inside:
        out["stats_withheld"] = "no %s frames inside the %s window" % (side, window_name)
    else:
        out["stats"] = frame_stats(inside, window_name)

    if s["nonfinite"]:
        last_end = max(f[1] for f in frames) if frames else None
        at_end = last_end is not None and all(
            math.isfinite(start) and start >= last_end - OVERLAP_EPS for start, _ in s["nonfinite"])
        warnings.append("dropped %d non-finite frame(s) from the %s %s (%s; R13)" % (
            len(s["nonfinite"]), side, s["source"], "unfinished at trace end" if at_end else "not all at trace end"))
    if out["overlapping_frames"]:
        warnings.append("%d overlapping frame(s) in the %s %s (%s); frame counts may include duplicates" % (
            out["overlapping_frames"], side, s["source"], ",".join(s["threads"])))
    ti = out["track_index"]
    if ti and (ti["gaps"] or ti["duplicates"]):
        warnings.append("%s frame track numbering has %d gap(s) and %d duplicate(s)" % (side, ti["gaps"], ti["duplicates"]))
    return out, warnings


def consistency(game: dict, render: dict) -> dict:
    out = {"game_rows": game["rows"], "render_rows": render["rows"], "rows_delta": None,
           "window_frames_delta": None, "max_delta": Q3_MAX_DELTA, "ok": None, "note": None}
    missing = [side for side, d in (("game", game), ("render", render)) if not d["frames"]]
    if missing:
        out["note"] = "not evaluated: no %s frames" % " / ".join(missing)
        return out
    out["rows_delta"] = abs(game["rows"] - render["rows"])
    out["window_frames_delta"] = abs(game["window_frames"] - render["window_frames"])
    out["ok"] = out["rows_delta"] <= Q3_MAX_DELTA and out["window_frames_delta"] <= Q3_MAX_DELTA
    return out


def build_frame_semantics(series, windows, basis, measurable, measurable_reason, rhi_state):
    """返回 (frame_semantics, warnings)。"""
    out, warnings = {}, []
    for side in SIDES:
        out[side], side_warnings = side_semantics(series, side, windows, basis, measurable, measurable_reason, rhi_state)
        warnings += side_warnings
    out["consistency"] = c = consistency(out["game"], out["render"])
    if c["ok"] is False:
        warnings.append("game and render frame counts differ by %d row(s) and %d frame(s) in the stats window "
                        "(more than %d, Q3)" % (c["rows_delta"], c["window_frames_delta"], Q3_MAX_DELTA))
    out["percentile_method"] = PERCENTILE_METHOD
    return out, warnings
