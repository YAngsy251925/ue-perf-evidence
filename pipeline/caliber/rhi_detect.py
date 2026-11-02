#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rhi_detect.py — 判定 run 有没有真实 RHI，据此决定 measurable（PLAN §5.2、R11）

判定顺序（先命中先用）：
  1. 命令行有 -nullrhi                                   → null
  2. 日志有 LogRHI: Using <Default|Forced|Preferred> RHI: X → real，rhi = X
  3. 日志里加载了 NullDrv 模块                           → null
  4. 只有 trace：有渲染帧证据（Rendering Frames 轨道，或 RenderThread 上的 Frame scope）→ real，名称未知
                 只有游戏帧、没有任何渲染帧               → null（实测 nullrhi 的形态）
  5. 以上都没有                                           → undetermined
measurable 只在 rhi_state = real 时为 true。日志 / 命令行的结论与 trace 证据矛盾时写入 warnings。
"""
import re

RE_NULLRHI = re.compile(r"(?:^|\s)-nullrhi(?:\s|$)", re.IGNORECASE)

MEASURABLE_REASON = {
    "real": "real RHI ({rhi})",
    "null": "nullrhi run: smoke only, no performance conclusions (PLAN R11)",
    "undetermined": "cannot prove a real RHI",
}


def frame_evidence(frames):
    """返回 (有渲染帧证据, 有游戏帧证据)。"""
    if not frames:
        return False, False
    rendering = frames.get("rendering_frame_track", 0) > 0 or frames.get("rt_frame_scopes", 0) > 0
    game = frames.get("game_frame_track", 0) > 0 or frames.get("gt_frame_scopes", 0) > 0
    return rendering, game


def frames_text(frames) -> str:
    return "game_track=%d rendering_track=%d gt_scopes=%d rt_scopes=%d" % (
        frames.get("game_frame_track", 0), frames.get("rendering_frame_track", 0),
        frames.get("gt_frame_scopes", 0), frames.get("rt_frame_scopes", 0))


def detect_rhi(log, frames) -> dict:
    log = log or {}
    cmd = log.get("command_line")
    rendering, game = frame_evidence(frames)
    out = {"rhi": None, "rhi_state": "undetermined", "rhi_source": "none", "rhi_evidence": None,
           "feature_level": log.get("feature_level")}
    warnings = []
    if cmd and RE_NULLRHI.search(cmd):
        out.update(rhi_state="null", rhi_source="command_line", rhi_evidence="-nullrhi")
    elif log.get("rhi_name"):
        out.update(rhi=log["rhi_name"], rhi_state="real", rhi_source="log",
                   rhi_evidence="LogRHI: Using %s RHI: %s" % (log["rhi_selection"], log["rhi_name"]))
    elif log.get("nulldrv_loaded"):
        out.update(rhi_state="null", rhi_source="log", rhi_evidence="NullDrv module loaded")
    elif rendering:
        out.update(rhi_state="real", rhi_source="trace", rhi_evidence=frames_text(frames))
        warnings.append("rhi name unknown: no engine log; real RHI inferred from rendering frames in the trace")
    elif game:
        out.update(rhi_state="null", rhi_source="trace", rhi_evidence=frames_text(frames))

    if frames and out["rhi_source"] in ("command_line", "log"):
        if out["rhi_state"] == "real" and game and not rendering:
            warnings.append("engine log reports a real RHI but the trace has game frames and no rendering frames")
        if out["rhi_state"] == "null" and rendering:
            warnings.append("engine log or command line says nullrhi but the trace has rendering frames")
    if out["rhi_state"] == "undetermined":
        warnings.append("rhi undetermined: no engine log, no command line and no frame evidence in the trace")

    out["measurable"] = out["rhi_state"] == "real"
    out["measurable_reason"] = MEASURABLE_REASON[out["rhi_state"]].format(rhi=out["rhi"] or "name unknown")
    out["warnings"] = warnings
    return out
