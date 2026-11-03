#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b2_drilldown.py — B2 下钻证据接进 run digest（M4.3）

cpu digest 的下钻增强（--b2 时启用，纯增量：不加时输出与既有逐字节一致）：
  callers   Top1 自耗时 timer 的调用方（B2 深度栈，与 A 路 inclusive 互为印证）
  regions   窗口内 region 聚合（IRegionProvider，按 region timer 分组）
  counters  窗口内有采样的计数器 Top-N

B2 commandlet 冷启动约 60 秒，结果一律缓存在 <run_dir>/b2/，stamp = trace + 窗口 + limit；
重复 digest / 测试不重跑。某个 mode 失败只记 warning，不拖垮整个 digest。
输出里只放数据与来源文件名，不放缓存命中与否——同一输入逐字节同一输出。
"""
import io
import json
import os

from pipeline.extract import b2
from pipeline.extract.insights_export import safe_name

SCHEMA = "perf-b2-drilldown/1"
REGIONS_TOP = 20
COUNTERS_TOP = 10
CALLERS_TOP = 10


def _stamp(run_dir, name, payload):
    d = os.path.join(run_dir, "b2")
    return os.path.join(d, "%s_inputs.json" % name), json.dumps(payload, sort_keys=True)


def _read_stamp(path):
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _load_cached(run_dir, name, stamp_payload):
    path = os.path.join(run_dir, "b2", "%s.json" % name)
    stamp_path, stamp = _stamp(run_dir, name, stamp_payload)
    if os.path.isfile(path) and _read_stamp(stamp_path) == stamp:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _run_and_cache(run_dir, name, stamp_payload, run_fn, warnings):
    """真跑一次 B2 mode 并写缓存。返回 JSON dict 或 None（失败只记 warning）。"""
    d = os.path.join(run_dir, "b2")
    os.makedirs(d, exist_ok=True)
    out_path = os.path.join(d, "%s.json" % name)
    ok, _text = run_fn(out_path)
    if not ok:
        warnings.append("b2 %s 运行失败（见 %s.log）" % (name, os.path.splitext(out_path)[0]))
        return None
    stamp_path, stamp = _stamp(run_dir, name, stamp_payload)
    with io.open(stamp_path, "w", encoding="utf-8") as f:
        f.write(stamp)
    with io.open(out_path, encoding="utf-8") as f:
        return json.load(f)


def _get_mode(run_dir, name, stamp_payload, want_run, run_fn, warnings):
    """缓存优先；want_run=False 且缓存未命中 → None（不报错，写进 files 的 missing 语义）。"""
    cached = _load_cached(run_dir, name, stamp_payload)
    if cached is not None:
        return cached
    if not want_run:
        return None
    return _run_and_cache(run_dir, name, stamp_payload, run_fn, warnings)


def build_drilldown(run_dir, manifest, trace, top_timer, ue_root=None, want_run=True,
                    callers_top=CALLERS_TOP, regions_top=REGIONS_TOP, counters_top=COUNTERS_TOP):
    """返回 digest 的 "b2_drilldown" 字段。top_timer 为 cpu digest 的 Top1 自耗时 timer 名。"""
    warnings = []
    win_name = manifest["window_basis"].get("stats_window")
    window = manifest["windows"].get(win_name) if win_name else None
    dd = {"schema": SCHEMA, "top_timer": top_timer, "window": window,
          "callers": None, "regions": None, "counters": None, "files": {}}
    if not window:
        warnings.append("no stats window: b2 drilldown 跳过")
        dd["warnings"] = warnings
        return dd
    start, end = window
    trace = os.path.abspath(trace)

    callers_key = "b2_callers_%s" % safe_name(top_timer)[:60]
    callers_stamp = {"trace": trace, "mode": "callers", "start": repr(start), "end": repr(end),
                     "thread": "GameThread", "timer": top_timer, "limit": callers_top}
    callers = _get_mode(
        run_dir, callers_key, callers_stamp, want_run, warnings=warnings,
        run_fn=lambda out: b2.run_mode("callers", trace, out, callers_top, ue_root,
                                       start=start, end=end, thread="GameThread", timer=top_timer))
    if callers is not None:
        dd["callers"] = {"timer": top_timer, "thread": "GameThread",
                         "caller_count": callers.get("caller_count"),
                         "top": (callers.get("callers") or [])[:callers_top]}
        dd["files"]["callers"] = "b2/%s.json" % callers_key

    regions_stamp = {"trace": trace, "mode": "regions", "start": repr(start), "end": repr(end),
                     "limit": regions_top}
    regions = _get_mode(
        run_dir, "b2_regions", regions_stamp, want_run, warnings=warnings,
        run_fn=lambda out: b2.run_mode("regions", trace, out, regions_top, ue_root,
                                       start=start, end=end))
    if regions is not None:
        rows = sorted(regions.get("regions") or [], key=lambda r: (-float(r.get("total_s") or 0.0), r.get("name") or ""))
        dd["regions"] = {"region_count": regions.get("region_count"),
                         "top": rows[:regions_top]}
        dd["files"]["regions"] = "b2/b2_regions.json"

    counters_stamp = {"trace": trace, "mode": "counters", "start": repr(start), "end": repr(end),
                      "limit": 0}
    counters = _get_mode(
        run_dir, "b2_counters", counters_stamp, want_run, warnings=warnings,
        run_fn=lambda out: b2.run_mode("counters", trace, out, 0, ue_root,
                                       start=start, end=end))
    if counters is not None:
        rows = [c for c in (counters.get("counters") or []) if (c.get("samples") or 0) > 0]
        rows.sort(key=lambda c: (-(c.get("samples") or 0), c.get("name") or ""))
        dd["counters"] = {"counter_count": counters.get("counter_count"),
                          "sampled_count": len(rows),
                          "top": rows[:counters_top]}
        dd["files"]["counters"] = "b2/b2_counters.json"

    dd["warnings"] = warnings
    return dd


def render_md(dd):
    """drilldown 的 MD 段（接在 digest md 的省略段之后）。"""
    lines = ["## B2 下钻（M4.3：callers / regions / counters，缓存在 run 目录 b2/）", ""]
    if dd.get("callers"):
        c = dd["callers"]
        lines.append("Top1 timer「%s」的调用方（%s 个，GameThread）：" % (c.get("timer"), c.get("caller_count")))
        lines += ["| caller | calls | inclusive_s |", "|---|---|---|"]
        lines += ["| %s | %s | %s |" % (r.get("name"), r.get("calls"), r.get("inclusive_s"))
                  for r in c.get("top") or []]
        lines.append("")
    if dd.get("regions"):
        r = dd["regions"]
        lines.append("窗口内 region Top（%s 条 / 共 %s）：" % (len(r.get("top") or []), r.get("region_count")))
        lines += ["| region | count | total_s | max_s |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s |" % (x.get("name"), x.get("count"), x.get("total_s"), x.get("max_s"))
                  for x in r.get("top") or []]
        lines.append("")
    if dd.get("counters"):
        c = dd["counters"]
        lines.append("窗口内有采样的计数器（%s / 共 %s，按采样数）：" % (c.get("sampled_count"), c.get("counter_count")))
        lines += ["| counter | group | samples | last_value |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s |" % (x.get("name"), x.get("group"), x.get("samples"), x.get("last_value"))
                  for x in c.get("top") or []]
        lines.append("")
    if dd.get("files"):
        lines.append("来源：")
        lines += ["- %s" % v for v in sorted(dd["files"].values())]
        lines.append("")
    for w in dd.get("warnings") or []:
        lines.append("- warning: %s" % w)
    if not dd.get("callers") and not dd.get("regions") and not dd.get("counters") and not dd.get("warnings"):
        lines.append("（无数据）")
        lines.append("")
    return "\n".join(lines)
