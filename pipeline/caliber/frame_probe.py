#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""frame_probe.py — 帧序列加载与参考序列选择（PLAN R4、R13）

数据源：probe 导出的 frame_tracks.csv（Game Frames / Rendering Frames 两条帧轨道）与
frame_scopes.csv（-timers=Frame，所有线程上名为 Frame 的 scope）。
M1.3 用参考序列切窗口；M1.4（frame_semantics.py）在同一份数据上做 GT / RT 分列与精确分位数。

每一帧记作 (start, end, duration, track_index)，时间单位是秒。track_index 是帧轨道事件名
"Game Frame N" / "Rendering Frame N" 里的 N（实测从 0 起连续编号）；Frame scope 没有编号，记 None。

规则：
  - Frame scope 只认 GameThread 与 RenderThread N。真实 RHI 下 RHIThread、worker 线程上也有大量
    名为 Frame 的 scope，它们不是帧标记（R4；coexist_D 的 RHIThread 上有 25,781 个）
  - 渲染线程重启后编号会变（RenderThread 0 / 1 / 2），同一侧的 Frame scope 按时间合并；
    合并后若有重叠，frame_semantics 会写 warning
  - 时长先按 float 解析再判断是否有限；非有限值（导出器把没结束的帧写成 "+inf"）不进入帧序列，
    只记录 (start, track_index)，供调用方写 warnings（R13）
  - 参考序列按 R4 的顺序选第一个非空的：游戏帧轨道、渲染帧轨道、GameThread scope、RenderThread scope
"""
import csv
import io
import json
import math
import os
import re

from pipeline.extract import uecsv

SERIES_ORDER = (("game", "frame_track"), ("render", "frame_track"), ("game", "frame_scope"), ("render", "frame_scope"))
TRACK_SIDES = {"Game Frames": "game", "Rendering Frames": "render"}
TRACK_TIMERS = {"game": "Game Frame N", "render": "Rendering Frame N"}
RE_RENDER_THREAD = re.compile(r"^RenderThread \d+$")
RE_TRACK_INDEX = re.compile(r"(\d+)$")
OVERLAP_EPS = 1e-6  # 导出精度是 1e-7 秒，留一个数量级的余量


def to_float(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return math.nan


def empty_series(side: str, source: str) -> dict:
    return {"side": side, "source": source,
            "timer": TRACK_TIMERS[side] if source == "frame_track" else "Frame",
            "threads": [], "rows": 0, "frames": [], "nonfinite": []}


def track_index(row: dict, source: str):
    if source != "frame_track":
        return None
    m = RE_TRACK_INDEX.search(row["TimerName"])
    return int(m.group(1)) if m else None


def add_row(series: dict, row: dict):
    series["rows"] += 1
    if row["ThreadName"] not in series["threads"]:
        series["threads"].append(row["ThreadName"])
    start, end, duration = to_float(row["StartTime"]), to_float(row["EndTime"]), to_float(row["Duration"])
    index = track_index(row, series["source"])
    if math.isfinite(start) and math.isfinite(end) and math.isfinite(duration):
        series["frames"].append((start, end, duration, index))
    else:
        series["nonfinite"].append((start, index))


def load_frame_series(extract: str) -> dict:
    """返回 {(side, source): series}；series 的 frames 按开始时间排序。"""
    series = {key: empty_series(*key) for key in SERIES_ORDER}
    for row in uecsv.read_dicts(os.path.join(extract, "frame_tracks.csv")):
        side = TRACK_SIDES.get(row["ThreadName"])
        if side:
            add_row(series[(side, "frame_track")], row)
    for row in uecsv.read_dicts(os.path.join(extract, "frame_scopes.csv")):
        if row["TimerName"] != "Frame":
            continue
        if row["ThreadName"] == "GameThread":
            add_row(series[("game", "frame_scope")], row)
        elif RE_RENDER_THREAD.match(row["ThreadName"]):
            add_row(series[("render", "frame_scope")], row)
    for s in series.values():
        s["frames"].sort(key=lambda f: (f[0], f[1]))  # Frame scope 的 track_index 是 None，不能参与比较
        s["threads"].sort()
    return series


def frames_seen(series: dict) -> dict:
    """各来源的原始行数（含非有限值行），是 rhi_detect 的帧证据（M1.2 的口径不变）。"""
    return {
        "game_frame_track": series[("game", "frame_track")]["rows"],
        "rendering_frame_track": series[("render", "frame_track")]["rows"],
        "gt_frame_scopes": series[("game", "frame_scope")]["rows"],
        "rt_frame_scopes": series[("render", "frame_scope")]["rows"],
    }


B2_SOURCE = "b2_frame_provider"


def empty_series_map():
    """与 load_frame_series 同结构的空表（B2 兜底时用同一个形状）。"""
    return {key: empty_series(key[0], key[1]) for key in SERIES_ORDER}


def b2_series(trace_path, out_dir, ue_root=None, limit=0):
    """R4 第三级：A 路没有帧轨道时，用 B2（TraceServices 的 IFrameProvider）读帧。

    返回 (series dict, warnings)。B2 只给 Game / Rendering 两条帧轨道，没有 Frame scope，
    所以 frame_scope 两项留空；帧元组仍按 (start, end, duration, track_index)，单位秒。
    """
    from pipeline.extract import b2

    series = empty_series_map()
    warnings = []
    out_path = os.path.join(out_dir, "b2_frames.json")
    ok, text = b2.run_mode("frames", trace_path, out_path, limit=limit, ue_root=ue_root)
    if not ok:
        warnings.append("B2 帧兜底失败（%s）" % os.path.join(out_dir, "b2_frames.log"))
        return series, warnings
    with io.open(out_path, encoding="utf-8") as f:
        payload = json.load(f)

    for side in ("game", "render"):
        block = payload.get(side) or {}
        target = series[(side, "frame_track")]
        target["source"] = B2_SOURCE
        target["timer"] = "Game Frame N" if side == "game" else "Rendering Frame N"
        target["rows"] = int(block.get("count") or 0)
        frames = []
        for item in block.get("frames") or []:
            start = item.get("start_ms")
            end = item.get("end_ms")
            if start is None or end is None:
                continue
            if not (math.isfinite(start) and math.isfinite(end)):
                target["nonfinite"].append((start, item.get("idx")))
                continue
            frames.append((start / 1000.0, end / 1000.0, (end - start) / 1000.0, item.get("idx")))
        target["frames"] = frames
        if frames:
            target["threads"] = ["IFrameProvider"]
    warnings.append("A 路没有帧轨道，参考序列改用 B2 IFrameProvider（R4 第 3 级）")
    return series, warnings


def pick_reference(series: dict):
    for key in SERIES_ORDER:
        if series[key]["frames"]:
            return series[key]
    return None


def count_overlaps(frames) -> int:
    """按开始时间排序后，开始时间早于上一帧结束时间的帧数。正常的帧序列不重叠，应为 0。"""
    return sum(1 for i in range(1, len(frames)) if frames[i][0] < frames[i - 1][1] - OVERLAP_EPS)
