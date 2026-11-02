#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hitch_log.py — 解析引擎日志里的 stat dumphitches 块（PLAN §9-M2.3）

块结构（coexist_C 实测，标记为源码原文，附录 A.2）：
  ------------------Thread Hitch <K>, Frame <N>   <ms>ms ---------------   块头（stats 帧号）
  ------------------ Game Thread <ms>ms                                     GT 汇总
  <缩进><ms>ms (<calls>)  -  <名字> [- <STAT_名> - <组> - <类>]              GT 调用树（缩进=2 空格/层）
  ------------------ Render Thread (<路径串>) <ms>ms                        RT 汇总
  <缩进><ms>ms ...                                                          RT 调用树
  ----------------------------------------                                  块结束
其后可跟 stat dumpevents 的 "Events history" 段（Trigger 行等），不属于 hitch 块。

树行名字段三种形态：`名字 - STAT_x - 组 - 类`（有 STAT 名）、`名字 - 组 - 类`（无 STAT 名，
如 HandleMemReportDeferredCommand）、`名字`（Self / OtherChildren）。缩进按 2 空格一层。
时序（R10）：检测滞后约 6 帧（帧 3 的 hitch 在日志帧 9 才打出来），写入 warnings 由调用方携带；
dumphitches 冷却 7 帧，snapshothitches 冷却 10 帧，连续卡顿只报第一次。
"""
import re

RE_HITCH_HEAD = re.compile(r"-+Thread Hitch (\d+), Frame (\d+)\s+([\d.]+)ms -+")
RE_THREAD_SUMMARY = re.compile(r"-+ (Game Thread|Render Thread)(?: \((.*)\))? ([\d.]+)ms")
RE_TREE_LINE = re.compile(r"^\s{2,}([\d.]+)ms \(\s*(\d+)\)\s+-\s+(.*)$")
RE_BLOCK_END = re.compile(r"^-{20,}$")
TREE_INDENT_STEP = 2  # 树行每层 2 空格缩进


def _split_tree_tail(tail: str) -> dict:
    """`名字 [- STAT_x] [- 组] [- 类]` → (名字, stat_name, group)。名字本身可以含 " - "
    （实测 "CPU Stall - Wait For Event"），所以从右往左认字段：STATCAT_* / STATGROUP_* / STAT_*，
    剩下的全是名字。Self / OtherChildren 只有名字。"""
    parts = [p.strip() for p in tail.split(" - ")]
    category = stat_name = group = None
    if parts and parts[-1].startswith("STATCAT_"):
        category = parts.pop()
    if parts and parts[-1].startswith("STATGROUP_"):
        group = parts.pop()
    if parts and parts[-1].startswith("STAT_"):
        stat_name = parts.pop()
    return {"name": " - ".join(parts), "stat_name": stat_name, "group": group}


def parse_hitches(log_text: str) -> list:
    """返回按块头顺序排列的 hitch 列表。每块：
    {index, stats_frame, ms, game_thread_ms, render_thread_ms, render_thread_path,
     game_tree: [...], render_tree: [...]}，树节点 {depth, ms, calls, name, stat_name, group}。"""
    hitches = []
    current = None
    current_tree = None
    for raw in log_text.splitlines():
        # 去掉日志前缀 [时间][帧号]Category: （dumphitches 全部走 LogStats）
        line = re.sub(r"^\[[^\]]*\]\[\s*\d+\](?:LogStats: )?", "", raw.rstrip("\r\n"))
        m = RE_HITCH_HEAD.search(line)
        if m:
            current = {
                "index": int(m.group(1)),
                "stats_frame": int(m.group(2)),
                "ms": float(m.group(3)),
                "game_thread_ms": None,
                "render_thread_ms": None,
                "render_thread_path": None,
                "game_tree": [],
                "render_tree": [],
                "triggers": [],
            }
            hitches.append(current)
            current_tree = None
            continue
        if current is None:
            continue
        if RE_BLOCK_END.fullmatch(line.strip()):
            current = None
            current_tree = None
            continue
        m = RE_THREAD_SUMMARY.search(line)
        if m:
            side = "game" if m.group(1) == "Game Thread" else "render"
            current["%s_thread_ms" % side] = float(m.group(3))
            if side == "render":
                current["render_thread_path"] = m.group(2)
            current_tree = current["%s_tree" % side]
            continue
        m = RE_TREE_LINE.match(line)
        if m and current_tree is not None:
            depth = (len(line) - len(line.lstrip(" ")) - 2) // TREE_INDENT_STEP
            node = {"depth": depth, "ms": float(m.group(1)), "calls": int(m.group(2)),
                    **_split_tree_tail(m.group(3))}
            # 树行按缩进深度原样保留；错误的缩进会在 digest 的展示端被"按出现顺序"兜底
            current_tree.append(node)
            continue
        trig = re.search(r"Trigger:\s+(.*)$", line)
        if trig:
            current["triggers"].append([s.strip() for s in trig.group(1).split("<-")])
    return hitches


def parse_engine_log(path: str) -> list:
    with open(path, encoding="utf-8-sig", errors="replace") as f:  # P9
        return parse_hitches(f.read())
