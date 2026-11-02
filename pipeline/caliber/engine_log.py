#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine_log.py — 从 UE 引擎日志里抽取 manifest 需要的事实（M1.2）

只匹配源码或实测核实过的 ASCII 标记（PLAN P9、附录 A.2）：
  LogInit: Command Line: / Engine Version: / Build: / Build Configuration: / Platform=
  LogRHI: Using <Default|Forced|Preferred> RHI: <name>     WindowsDynamicRHI.cpp:1333 / 1344 / 1349
  LogRHI: Using Highest Feature Level of <rhi>: <level>    实测 coexist_D
  InternalLoadLibrary: 'NullDrv'                           nullrhi 实测（B2 smoke）
日志开头的 LogInit 行没有时间戳前缀，之后的行才有 [时间][帧号] 前缀，两种都要能匹配。
"""
import re

RE_FIRST_TIMESTAMP = re.compile(r"^\[(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}:\d{3})\]")
RE_INIT = {
    "command_line": re.compile(r"LogInit: Command Line: ?(.*)$"),
    "engine_version": re.compile(r"LogInit: Engine Version: (\S+)"),
    "build": re.compile(r"LogInit: Build: (\S+)"),
    "build_config": re.compile(r"LogInit: Build Configuration: (\S+)"),
    "platform": re.compile(r"LogInit: Platform=(\S+)"),
}
RE_RHI = re.compile(r"LogRHI: Using (Default|Forced|Preferred) RHI: (\S+)")
RE_FEATURE_LEVEL = re.compile(r"LogRHI: Using Highest Feature Level of \S+: (\S+)")
NULLDRV_MARK = "InternalLoadLibrary: 'NullDrv'"


def parse_engine_log_text(text: str) -> dict:
    facts = {key: None for key in RE_INIT}
    facts.update({"rhi_selection": None, "rhi_name": None, "feature_level": None,
                  "nulldrv_loaded": False, "first_timestamp_utc": None})
    for line in text.splitlines():
        if facts["first_timestamp_utc"] is None:
            m = RE_FIRST_TIMESTAMP.match(line)
            if m:
                facts["first_timestamp_utc"] = m.group(1)
        for key, rx in RE_INIT.items():
            if facts[key] is None:
                m = rx.search(line)
                if m:
                    facts[key] = m.group(1).strip()
        if facts["rhi_name"] is None:
            m = RE_RHI.search(line)
            if m:
                facts["rhi_selection"], facts["rhi_name"] = m.group(1), m.group(2)
        if facts["feature_level"] is None:
            m = RE_FEATURE_LEVEL.search(line)
            if m:
                facts["feature_level"] = m.group(1)
        if not facts["nulldrv_loaded"] and NULLDRV_MARK in line:
            facts["nulldrv_loaded"] = True
    return facts


def parse_engine_log(path: str) -> dict:
    # P9：UE 日志是带 BOM 的 UTF-8
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse_engine_log_text(f.read())
