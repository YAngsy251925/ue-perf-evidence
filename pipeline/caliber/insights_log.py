#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""insights_log.py — 从 UnrealInsights 的日志里读出 trace 的会话时长（M1.3）

来源（源码核实）：
  InsightsManager.cpp:388  "Analysis has completed in %ls (%.1fX speed; session duration: %ls)."
  InsightsManager.cpp:375  "The session duration was updated (%ls) after the analysis has been completed."
两处时长都由 FormatTimeAuto(Duration, 2) 格式化（TimeUtils.cpp:45），精度随量级变化：
  [1ms, 1s)   "523.45 ms"  两位小数，四舍五入
  [1s, 1m)    "15.48s"     两位小数，四舍五入，去掉尾零；不小于 59.95s 时写作 "1m"
  [1m, 1h)    "1m 15.5s"   秒的部分保留一位小数
  [1h, 1d)    "2h 5m"      分钟向下取整，没有秒
  [1d, ...)   "1d 2h"      小时向下取整
所以除了数值，还要给出真实时长所在的区间 bounds。
"""
import re

RE_COMPLETED = re.compile(r"Analysis has completed in .+? \([\d.]+X speed; session duration: (.+?)\)\.")
RE_UPDATED = re.compile(r"The session duration was updated \((.+?)\) after the analysis has been completed\.")
# 单位按长度优先："ms" 必须排在 "m"、"s" 前面。微秒的 mu 用转义写（U+00B5 或 U+03BC）
RE_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|[\u00b5\u03bc]s|ns|ps|d|h|m|s)")
MICRO_SIGNS = "\u00b5\u03bc"
UNIT_SECONDS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12}


def parse_duration_text(text):
    """把 FormatTimeAuto 的输出换算成秒。返回 {"text", "seconds", "bounds": [lo, hi]}；
    无法解析（"NaN"、"~0"、无穷大等）时返回 None。"""
    text = text.strip()
    if text == "0":
        return {"text": text, "seconds": 0.0, "bounds": [0.0, 0.0]}
    tokens = RE_TOKEN.findall(text)
    if not tokens or RE_TOKEN.sub("", text).strip():
        return None
    units = ["us" if unit[0] in MICRO_SIGNS else unit for _, unit in tokens]
    value = sum(float(number) * UNIT_SECONDS[unit] for (number, _), unit in zip(tokens, units))
    if "d" in units:            # "Xd" / "Xd Yh"：小时向下取整
        lo, hi = value, value + UNIT_SECONDS["h"]
    elif "h" in units:          # "Xh" / "Xh Ym"：分钟向下取整
        lo, hi = value, value + UNIT_SECONDS["m"]
    elif units == ["m"]:        # 只有秒级分支会写出单独的 "1m"：59.95s <= d < 60s
        lo, hi = value - 0.05, value
    elif "m" in units:          # "Xm Y.Ys"：秒保留一位小数
        lo, hi = value - 0.05, value + 0.05
    else:                       # "X.XXs" / "X.XX ms" / "X.XX us" 两位小数；ns、ps 是整数
        unit = UNIT_SECONDS[units[-1]]
        half = 0.5 * unit if units[-1] in ("ns", "ps") else 0.005 * unit
        lo, hi = value - half, value + half
    return {"text": text, "seconds": round(value, 9), "bounds": [round(lo, 9), round(hi, 9)]}


def session_duration_from_text(log_text):
    """取日志里最后一次报告的会话时长（分析完成后若再更新，以更新值为准）。"""
    found = None
    for line in log_text.splitlines():
        m = RE_COMPLETED.search(line) or RE_UPDATED.search(line)
        if m:
            found = m.group(1)
    return None if found is None else parse_duration_text(found)


def read_session_duration(path):
    # Insights 日志与引擎日志一样是带 BOM 的 UTF-8（P9）
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return session_duration_from_text(f.read())
