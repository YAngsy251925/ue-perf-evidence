#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""attribution.py — 把 diff 的帧时间差与某个 timer 的每帧成本对上（M3.4 的"归因"判据）

思路：diff 的 Top 表给的是**窗口内总自耗时**，而 M3.4 的注入是"每帧多做 k 次忙等、每次 m 毫秒"。
总耗时会被窗口内帧数缩放（B 组帧少、每帧贵），直接看总秒数会误判，所以换算成：

    calls_per_frame = 调用次数 / 窗口帧数
    per_call_ms     = 总自耗时(ms) / 调用次数
    expected_delta  = (calls_per_frame_b - calls_per_frame_a) × 两侧每次成本均值

再与实测的 p50 帧间隔差（R9 的 delta_mean）对比：差距在 max(1ms, 15%) 内即认为"归因正确"。
"""
import math


def _find_row(diff, span):
    rows = diff.get("top_delta") or []
    for row in rows:
        if row.get("name") == span:
            return row
    for row in rows:
        if span in (row.get("name") or ""):
            return row
    return None


def _window_frames(manifest):
    return ((manifest.get("frame_semantics") or {}).get("game") or {}).get("window_frames")


def span_attribution(diff, span, frames_a, frames_b, tolerance_ms=1.0, tolerance_pct=15.0):
    """返回归因块；数据不足时 ok=False 并给原因。"""
    row = _find_row(diff, span)
    sig = diff.get("significance") or {}
    observed = sig.get("delta_mean")
    if row is None:
        return {"ok": False, "reason": "diff 里没有 span %r（Top 表未覆盖）" % span}
    if not frames_a or not frames_b or observed is None:
        return {"ok": False, "reason": "缺窗口帧数或显著性结果"}
    calls_a = row.get("calls_a") or 0
    calls_b = row.get("calls_b") or 0
    if calls_a <= 0 or calls_b <= 0:
        return {"ok": False, "reason": "span %r 在一侧没有调用（%d / %d）" % (span, calls_a, calls_b)}

    per_frame_a = calls_a / float(frames_a)
    per_frame_b = calls_b / float(frames_b)
    per_call_a_ms = float(row["a_s"]) * 1000.0 / calls_a
    per_call_b_ms = float(row["b_s"]) * 1000.0 / calls_b
    per_call_ms = 0.5 * (per_call_a_ms + per_call_b_ms)
    expected = (per_frame_b - per_frame_a) * per_call_ms
    tolerance = max(tolerance_ms, abs(observed) * tolerance_pct / 100.0)
    ok = math.isfinite(expected) and abs(expected - observed) <= tolerance
    return {
        "ok": ok,
        "span": span,
        "frames_a": frames_a, "frames_b": frames_b,
        "calls_a": calls_a, "calls_b": calls_b,
        "calls_per_frame_a": round(per_frame_a, 3), "calls_per_frame_b": round(per_frame_b, 3),
        "per_call_ms_a": round(per_call_a_ms, 3), "per_call_ms_b": round(per_call_b_ms, 3),
        "expected_delta_ms": round(expected, 3), "observed_delta_ms": round(observed, 3),
        "tolerance_ms": round(tolerance, 3),
        "error_pct": round(100.0 * (expected - observed) / observed, 2) if observed else None,
    }
