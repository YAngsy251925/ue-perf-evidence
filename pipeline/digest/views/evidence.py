#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence.py — `perf_evidence`：把结论背后的**原始证据行**取回来（PLAN §3 的第四个工具）

其他视图只给结论/摘要；这个工具负责"你说的那条结论，原始行在哪"。三种 ref：

  timer:<Name>@<窗口>   某个 timer 在窗口内的逐次调用（start / dur / depth）——来自 B2 `mode=calls`
  frame:<N>             第 N 帧的 scope 树（含自耗时）——复用 `b2/b2_frame_<N>_<thread>.json`
  hitch:<K>            第 K 次 hitch 的调用树 + 快照文件 —— 读 `hitches_index.json`

窗口写法：`stats` / `coverage` / `all`（取 manifest 里的窗口名），或显式 `12.5-45.0`（秒，trace 时间）。

分页契约（PLAN §3 要求"分页，截断时附提示"）：
  rows / total / page / page_size / next_page / truncated_hint
同 ref 的两次调用顺序必须稳定（按 start 排序，start 相同按收集序号），否则翻页会漏行或重复。

用法：
  perfctl evidence <run_dir> --ref "timer:PerfEvidenceRegress@stats" --out DIR [--page 1] [--limit 200]
  perfctl evidence <run_dir> --ref "frame:700" --out DIR
  perfctl evidence <run_dir> --ref "hitch:1" --out DIR [--b2]
"""
import argparse
import io
import json
import os
import re
import sys

REPEAT_REFS = (
    ("timer", r"^timer:(?P<name>[^@]+)@(?P<window>.+)$"),
    ("frame", r"^frame:(?P<index>\d+)$"),
    ("hitch", r"^hitch:(?P<index>\d+)$"),
)
WIN_RE = re.compile(r"^(?P<a>-?\d+(?:\.\d+)?)\s*-\s*(?P<b>-?\d+(?:\.\d+)?)$")


def parse_ref(ref: str):
    """把 ref 解析成 {kind, ...}；非法时抛 ValueError（附合法形态）。"""
    text = (ref or "").strip()
    for kind, pattern in REPEAT_REFS:
        m = re.match(pattern, text)
        if not m:
            continue
        if kind == "timer":
            return {"kind": kind, "name": m.group("name").strip(), "window": m.group("window").strip()}
        return {"kind": kind, "index": int(m.group("index"))}
    raise ValueError("非法 ref %r；合法形态：timer:Name@stats | frame:700 | hitch:1" % ref)


def resolve_window(run_dir, spec, manifest=None):
    """把窗口名/显式区间解析成 (start, end)。"""
    spec = (spec or "stats").strip()
    m = WIN_RE.match(spec)
    if m:
        return float(m.group("a")), float(m.group("b"))
    if manifest is None:
        path = os.path.join(run_dir, "manifest.json")
        manifest = {}
        if os.path.isfile(path):
            with io.open(path, encoding="utf-8") as f:
                manifest = json.load(f)
    windows = manifest.get("windows") or {}
    key = {"stats": None, "coverage": "coverage_probe", "all": None}.get(spec, spec)
    if spec == "all":
        return 0.0, float(manifest.get("duration_s") or 0.0) or 1e9
    if key is None:
        key = (manifest.get("window_basis") or {}).get("stats_window") or "stats"
    win = windows.get(key)
    if not win:
        raise ValueError("manifest 里没有窗口 %r（可用：%s）" % (spec, ", ".join(sorted(windows)) or "无"))
    return float(win[0]), float(win[1])


def paginate(rows, page, limit):
    """稳定的分页切片 + 截断提示。page 从 1 开始。"""
    total = len(rows)
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 200))
    start = (page - 1) * limit
    chunk = rows[start:start + limit]
    next_page = page + 1 if start + limit < total else None
    hint = None
    if next_page:
        hint = ("本次返回第 %d-%d 行（共 %d 行）；继续取请用 --page %d --limit %d"
                % (start + 1, start + len(chunk), total, next_page, limit))
    return {"rows": chunk, "total": total, "page": page, "page_size": limit,
            "next_page": next_page, "truncated_hint": hint}


def _load_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 三种 ref


def timer_rows(run_dir, ref, limit_per_call=200000, use_b2=False, thread="GameThread"):
    start, end = resolve_window(run_dir, ref["window"])
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ref["name"])
    cache = os.path.join(run_dir, "b2", "b2_calls_%s_%.3f-%.3f.json" % (safe, start, end))
    if not os.path.isfile(cache):
        if not use_b2:
            raise FileNotFoundError("没有缓存 %s（加 --b2 现场跑 perfctl b2 calls）"
                                    % os.path.relpath(cache, run_dir).replace(os.sep, "/"))
        from pipeline.extract import b2
        ok, _text = b2.run_mode("calls", os.path.join(run_dir, "trace.utrace"), cache,
                                limit=limit_per_call, start=start, end=end,
                                thread=thread, timer=ref["name"])
        if not ok:
            raise RuntimeError("b2 calls 失败（见 b2/b2_calls*.log）")
    payload = _load_json(cache)
    rows = []
    base = float(payload.get("window", [0.0, 0.0])[0] or 0.0)
    for i, c in enumerate(payload.get("calls") or []):
        rows.append({"seq": i, "start_s": c["start_s"], "start_rel_ms": round((c["start_s"] - base) * 1000.0, 4),
                     "dur_ms": c["dur_ms"], "depth": c["depth"]})
    context = {"ref": "timer:%s" % ref["name"], "thread": payload.get("thread"),
               "window": payload.get("window"), "events_in_window": payload.get("events_in_window"),
               "matched": payload.get("matched"), "collected": payload.get("collected"),
               "sum_ms": payload.get("sum_ms"), "max_ms": payload.get("max_ms"),
               "b2_truncated": bool(payload.get("truncated"))}
    bad = "命中 %s 次但只收集了 %s 条（B2 的 -limit 上限）" % (payload.get("matched"), payload.get("collected")) \
        if payload.get("truncated") else None
    return rows, context, bad


def frame_rows(run_dir, ref, thread="GameThread", use_b2=False):
    index = ref["index"]
    cache = os.path.join(run_dir, "b2", "b2_frame_%d_%s.json" % (index, thread))
    if not os.path.isfile(cache):
        if not use_b2:
            raise FileNotFoundError("没有缓存 b2/b2_frame_%d_%s.json（加 --b2）" % (index, thread))
        from pipeline.extract import b2
        ok, _text = b2.run_mode("frame", os.path.join(run_dir, "trace.utrace"), cache,
                                limit=0, thread=thread, frame=index)
        if not ok:
            raise RuntimeError("b2 frame 失败（见 b2/b2_frame*.log）")
    payload = _load_json(cache)
    scopes = payload.get("scopes") or []
    rows = []
    for c in scopes:
        rows.append({"name": c.get("name"), "depth": c.get("depth"),
                     "begin_ms": c.get("begin_ms"), "dur_ms": c.get("duration_ms")})
    context = {"ref": "frame:%d" % index, "thread": thread, "frame": payload.get("frame"),
               "scope_count": payload.get("scope_count"), "stat_named_pct": payload.get("stat_named_pct")}
    bad = None
    if payload.get("scope_count") and len(scopes) < payload["scope_count"]:
        bad = "B2 只导出了前 %d 个 scope（共 %s），加 --b2 重跑并确认 limit" % (len(scopes), payload["scope_count"])
    return rows, context, bad


def hitch_rows(run_dir, ref):
    path = os.path.join(run_dir, "hitches_index.json")
    if not os.path.isfile(path):
        raise FileNotFoundError("没有 hitches_index.json（这个 run 不是 hitch-hunt profile）")
    data = _load_json(path)
    hitches = data.get("hitches") or []
    target = next((h for h in hitches if int(h.get("index", -1)) == ref["index"]), None)
    if target is None:
        raise ValueError("没有第 %d 次 hitch（本 run 有 %d 次）" % (ref["index"], len(hitches)))

    rows = []

    def walk(node, depth, side):
        if not isinstance(node, dict):
            return
        rows.append({"side": side, "depth": depth, "name": node.get("name"),
                     "dur_ms": node.get("ms"), "calls": node.get("calls")})
        for child in node.get("children") or []:
            walk(child, depth + 1, side)

    for side in ("game_tree", "render_tree"):
        tree = target.get(side) or []
        if isinstance(tree, dict):
            tree = [tree]
        for root in tree:
            walk(root, 0, side)
    snaps = [s for s in (data.get("snapshots") or []) if s.get("stats_frame") == target.get("stats_frame")]
    context = {"ref": "hitch:%d" % ref["index"], "stats_frame": target.get("stats_frame"),
               "ms": target.get("ms"), "game_thread_ms": target.get("game_thread_ms"),
               "render_thread_ms": target.get("render_thread_ms"),
               "triggers": target.get("triggers"), "snapshots": snaps}
    return rows, context, None


# ---------------------------------------------------------------- 渲染


def render_md(result):
    ctx = result.get("context") or {}
    lines = ["# 证据行 — %s" % result.get("ref"), "",
             "| 项 | 值 |", "|---|---|"]
    for key in ("thread", "window", "matched", "collected", "sum_ms", "max_ms",
                "frame", "scope_count", "stats_frame", "ms", "game_thread_ms", "render_thread_ms"):
        if ctx.get(key) is not None:
            lines.append("| %s | %s |" % (key, ctx[key]))
    lines += ["| 分页 | 第 %s 页，%d-%d / 共 %d 行 |"
              % (result["page"], (result["page"] - 1) * result["page_size"] + (1 if result["rows"] else 0),
                 (result["page"] - 1) * result["page_size"] + len(result["rows"]), result["total"]),
              ""]
    rows = result["rows"]
    if result.get("ref", "").startswith("timer:"):
        lines += ["| # | start(s) | dur(ms) | depth |", "|---|---|---|---|"]
        for r in rows:
            lines.append("| %s | %.6f | %.4f | %s |" % (r["seq"], r["start_s"], r["dur_ms"], r["depth"]))
    elif result.get("ref", "").startswith("frame:"):
        lines += ["| scope | depth | begin(ms) | dur(ms) |", "|---|---|---|---|"]
        for r in rows:
            lines.append("| %s | %s | %.4f | %.4f |" % (str(r["name"])[:60], r["depth"], r["begin_ms"], r["dur_ms"]))
    else:
        lines += ["| side | depth | name | dur(ms) | calls |", "|---|---|---|---|---|"]
        for r in rows:
            lines.append("| %s | %s | %s | %s | %s |"
                         % (r["side"], r["depth"], str(r["name"])[:60], r["dur_ms"], r["calls"]))
    lines.append("")
    if result.get("truncated_hint"):
        lines += ["> %s" % result["truncated_hint"], ""]
    if result.get("warnings"):
        lines += ["## 警告"] + ["- %s" % w for w in result["warnings"]] + [""]
    snaps = ctx.get("snapshots") or []
    if snaps:
        lines += ["## 快照", ""]
        for s in snaps:
            lines.append("- `%s`%s" % (s.get("snapshot"), "（截图 `%s`）" % s["screenshot"] if s.get("screenshot") else ""))
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="原始证据行（perf_evidence）")
    ap.add_argument("run_dir")
    ap.add_argument("--ref", required=True,
                    help="timer:Name@stats | timer:Name@12.5-45.0 | frame:700 | hitch:1")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--thread", default="GameThread")
    ap.add_argument("--b2", action="store_true", help="缓存缺失时现场跑 b2")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    try:
        ref = parse_ref(a.ref)
    except ValueError as exc:
        print("[error] %s" % exc)
        return 1

    warnings = []
    try:
        if ref["kind"] == "timer":
            rows, context, bad = timer_rows(run_dir, ref, use_b2=a.b2, thread=a.thread)
        elif ref["kind"] == "frame":
            rows, context, bad = frame_rows(run_dir, ref, thread=a.thread, use_b2=a.b2)
        else:
            rows, context, bad = hitch_rows(run_dir, ref)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print("[error] %s" % exc)
        return 1
    if bad:
        warnings.append(bad)

    page = paginate(rows, a.page, a.limit)
    result = {"schema": "perf-evidence/0", "ref": a.ref, "total": page["total"],
              "page": page["page"], "page_size": page["page_size"], "next_page": page["next_page"],
              "truncated_hint": page["truncated_hint"], "rows": page["rows"],
              "context": context, "warnings": warnings,
              "run_dir": run_dir.replace(os.sep, "/")}

    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "evidence.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "evidence.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")

    print("[evidence] %s → 第 %d 页，%d 行 / 共 %d 行%s"
          % (a.ref, result["page"], len(result["rows"]), result["total"],
             ("；下一页 --page %d" % result["next_page"]) if result["next_page"] else ""))
    for w in warnings:
        print("[evidence] WARNING: %s" % w)
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "evidence.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
