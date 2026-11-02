#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge_detect.py — 判定 -statnamedevents 桥接是否真的生效（PLAN R1）

以 trace 内容为准，不以命令行为准：桥接生效时，有描述的 stat 以描述名进入 trace。
标记（实测：B2 smoke 与 coexist_D 都有，example.utrace 一个都没有）：
  FrameTime、GameEngine Tick、World Tick Time —— 至少命中 2 个才判为生效，避免项目里恰好有同名 scope。
"""
import re

BRIDGE_MARKERS = ("FrameTime", "GameEngine Tick", "World Tick Time")
MIN_HITS = 2
RE_STATNAMEDEVENTS = re.compile(r"(?:^|\s)-statnamedevents(?:\s|$)", re.IGNORECASE)


def detect_bridge(timer_names, command_line=None) -> dict:
    names = set(timer_names)
    found = [m for m in BRIDGE_MARKERS if m in names]
    requested = None if command_line is None else bool(RE_STATNAMEDEVENTS.search(command_line))
    effective = len(found) >= MIN_HITS
    warnings = []
    if requested and not effective:
        warnings.append("-statnamedevents requested but bridging is not effective in the trace")
    return {"statnamedevents_requested": requested, "statnamedevents_effective": effective,
            "bridge_evidence": found, "warnings": warnings}
