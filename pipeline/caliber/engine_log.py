#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine_log.py — 从 UE 引擎日志里抽取 manifest 需要的事实（M1.2）

只匹配源码或实测核实过的 ASCII 标记（PLAN P9、附录 A.2）：
  LogInit: Command Line: / Engine Version: / Build: / Build Configuration: / Platform=
  LogRHI: Using <Default|Forced|Preferred> RHI: <name>     WindowsDynamicRHI.cpp:1333 / 1344 / 1349
  LogRHI: Using Highest Feature Level of <rhi>: <level>    实测 coexist_D
  InternalLoadLibrary: 'NullDrv'                           nullrhi 实测（B2 smoke）
  <cvar> = "<value>"                                       ExecCmds 设 cvar 后的回显（coexist_D 第 1532 行实测）
  Setting the console variable '<cvar>' with 'SetBy<X>' was ignored as it is lower priority than the
    previous 'SetBy<Y>'. Value remains '<v>'               低优先级设置被忽略（ConsoleManager.cpp:286）
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
# cvar 名必须含点（t.MaxFPS、r.SetRes），避免把普通日志里的 `Foo = "bar"` 误当回显
RE_CVAR_ECHO = re.compile(r'^(?:\[[^\]]*\]\[\s*\d+\])?([A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_.]+) = "(.*)"\s*$')
RE_CVAR_IGNORED = re.compile(
    r"Setting the console variable '([^']+)' with 'SetBy(\w+)' was ignored as it is lower priority than "
    r"the previous 'SetBy(\w+)'\. Value remains '([^']*)'")


def parse_engine_log_text(text: str) -> dict:
    facts = {key: None for key in RE_INIT}
    facts.update({"rhi_selection": None, "rhi_name": None, "feature_level": None,
                  "nulldrv_loaded": False, "first_timestamp_utc": None,
                  "cvar_echoes": {}, "cvar_ignored": [],
                  "_raw_lines": text.splitlines()})
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
        m = RE_CVAR_ECHO.match(line)
        if m:  # 按出现顺序记下每次回显；对账取最后一次（后来的设置会覆盖前面的）
            facts["cvar_echoes"].setdefault(m.group(1).lower(), []).append(m.group(2))
        m = RE_CVAR_IGNORED.search(line)
        if m:
            facts["cvar_ignored"].append({"cvar": m.group(1), "set_by": m.group(2),
                                          "kept_by": m.group(3), "value": m.group(4)})
    return facts


def parse_engine_log(path: str) -> dict:
    # P9：UE 日志是带 BOM 的 UTF-8
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse_engine_log_text(f.read())
