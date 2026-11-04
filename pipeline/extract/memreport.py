#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memreport.py — MemReport 解析（M6.1）

引擎的 `memreport` 命令写一个纯文本报告（默认 <Project>/Saved/Profiling/MemReports/<Level>-<Platform>-<date>/Pid*_*.memreport）。
本模块只做"结构化"这一步：头部元信息、平台内存总量、`Memory Stats:` 段里的逐条 stat（MB + 名字 + STATGROUP/STATCAT），
以及各段落的 `Showing N of M matched resources` 计数。

不做的事：不去解析 `Objects:` / `Levels:` 这类极长的对象清单——那些用 B2 的 memory/allocations 通道更合适（M6.2）。
"""
import io
import re
import os

RE_KV = re.compile(r"^([A-Za-z][A-Za-z0-9 /_()-]{2,40}):\s*(.+)$")
RE_STAT = re.compile(r"^\s+([0-9]+(?:\.[0-9]+)?)\s*MB\s+-\s+(.+?)\s+-\s+(\S+)\s+-\s+(\S+)\s+-\s+(\S+)")
RE_SHOWING = re.compile(r"Showing (\d+) of (\d+) matched resources")
RE_PROC_PHYS = re.compile(r"Process Physical Memory:\s*([0-9.]+)\s*MB used,\s*([0-9.]+)\s*MB peak")
RE_PROC_VIRT = re.compile(r"Process Virtual Memory:\s*([0-9.]+)\s*MB used,\s*([0-9.]+)\s*MB peak")
RE_SYS_PHYS = re.compile(r"^Physical Memory:\s*([0-9.]+)\s*MB used,\s*([0-9.]+)\s*MB free,\s*([0-9.]+)\s*MB total", re.M)
RE_SYS_VIRT = re.compile(r"^Virtual Memory:\s*([0-9.]+)\s*MB used,\s*([0-9.]+)\s*MB free,\s*([0-9.]+)\s*MB total", re.M)


def find_files(run_dir):
    """run 目录下所有 .memreport（capture 会把它们拷进 <run_dir>/memreport/）。"""
    out = []
    base = os.path.join(run_dir, "memreport")
    if not os.path.isdir(base):
        return out
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.lower().endswith(".memreport"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def parse_text(text):
    """解析一份 memreport 文本，返回结构化的 dict。"""
    header = {}
    stats = []
    showing = []
    sections = []
    pending_section = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        m = RE_SHOWING.search(line)
        if m:
            showing.append({"section": pending_section, "shown": int(m.group(1)),
                            "total": int(m.group(2))})
            continue
        m = RE_STAT.match(line)
        if m:
            mb = _num(m.group(1))
            stats.append({"mb": mb, "label": m.group(2).strip(), "stat": m.group(3),
                          "group": m.group(4), "category": m.group(5),
                          "section": pending_section})
            continue
        if not raw.startswith((" ", "\t")):
            stripped = line.strip()
            if stripped.endswith(":") and len(stripped) > 1:
                pending_section = stripped[:-1]
                sections.append(pending_section)
                continue
            m = RE_KV.match(stripped)
            if m and m.group(1) not in ("Physical Memory", "Virtual Memory"):
                header.setdefault(m.group(1).strip(), m.group(2).strip())

    sys_phys = RE_SYS_PHYS.search(text)
    sys_virt = RE_SYS_VIRT.search(text)
    proc_phys = RE_PROC_PHYS.search(text)
    proc_virt = RE_PROC_VIRT.search(text)

    by_group = {}
    for item in stats:
        if item["mb"] is None:
            continue
        by_group[item["group"]] = round(by_group.get(item["group"], 0.0) + item["mb"], 3)

    top = sorted((s for s in stats if s["mb"] is not None and s["mb"] > 0),
                 key=lambda s: -s["mb"])[:20]

    return {
        "schema": "perf-memreport/0",
        "header": header,
        "platform": {
            "process_physical_mb": None if not proc_phys else {"used": _num(proc_phys.group(1)),
                                                              "peak": _num(proc_phys.group(2))},
            "process_virtual_mb": None if not proc_virt else {"used": _num(proc_virt.group(1)),
                                                             "peak": _num(proc_virt.group(2))},
            "system_physical_mb": None if not sys_phys else {"used": _num(sys_phys.group(1)),
                                                            "free": _num(sys_phys.group(2)),
                                                            "total": _num(sys_phys.group(3))},
            "system_virtual_mb": None if not sys_virt else {"used": _num(sys_virt.group(1)),
                                                           "free": _num(sys_virt.group(2)),
                                                           "total": _num(sys_virt.group(3))},
        },
        "stat_count": len(stats),
        "stats_top": top,
        "group_totals_mb": dict(sorted(by_group.items(), key=lambda kv: -kv[1])[:15]),
        "sections": sections,
        "showing": showing,
    }


def parse(path):
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        result = parse_text(f.read())
    result["file"] = os.path.basename(path)
    result["size_bytes"] = os.path.getsize(path)
    return result
