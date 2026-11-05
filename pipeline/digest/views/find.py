#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""find.py — 按名字在 trace 里找 scope（"这个特效/这个函数花了多少？"）

场景：想知道 Lyra 的传送门特效、死亡消散、某个蓝图函数、某个子系统到底花多少时间。
做法：先列出 trace 里所有 timer 名（B2 `mode=timers`，几万条），按**子串/正则**过滤；
命中项如果有窗口统计（`mode=timerstats`）就带上 self/incl/次数/每帧毫秒，并给出下一步下钻命令。

诚实边界（会写在输出里）：**只有被命名过的 scope 才会出现**。
  - 引擎/插件里写了 `SCOPED_NAMED_EVENT` / `TRACE_CPUPROFILER_EVENT_SCOPE` 的才有名字；
  - Niagara 系统、蓝图函数默认**不一定**有独立 scope（要看 `statnamedevents` 与具体代码）；
  - 资产名（如 `NS_CharacterPortalDissolve`）出现在 timer 表里，多半是**加载期**的统计，不等于运行时每帧开销。

用法：
  perfctl find <run_dir|trace.utrace> --name portal [--regex] [--top 20] [--b2]
  perfctl find runs/x --name "DeathCubes|Portal" --regex
"""
import argparse
import io
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _trace_of(target):
    """run 目录 → <dir>/trace.utrace；.utrace 直接返回。按**后缀**判断而不是 isdir，
    这样对不存在的路径也给出一致的解释（agent 传来的路径可能还没落盘）。"""
    if target.lower().endswith(".utrace"):
        return target
    return os.path.join(target, "trace.utrace")


def _cache_path(target, name):
    base = target if os.path.isdir(target) else os.path.dirname(os.path.abspath(target))
    out = os.path.join(base, "b2")
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, name)


def load_timers(trace, base_for_cache, use_b2=False):
    """返回 [(name, index)]。优先用缓存；没有且 use_b2 才现场跑（大 trace 要几十秒）。"""
    from pipeline.extract import b2, metric_json
    cache = _cache_path(base_for_cache, "b2_timers.json")
    if not os.path.isfile(cache):
        if not use_b2:
            raise FileNotFoundError("没有 b2/b2_timers.json（加 --b2 现场跑一次）")
        ok, _text = b2.run_mode("timers", trace, cache, limit=0)
        if not ok:
            raise RuntimeError("b2 timers 失败（见 b2/b2_timers.log）")
    payload = metric_json.load_path(cache)
    return [(t.get("name"), t.get("index")) for t in (payload.get("timers") or [])]


def load_stats(trace, base_for_cache, thread, start=None, end=None, use_b2=False, top=200,
               full=False):
    """窗口统计（可选）：给出命中项的 self/incl/次数。

    full=True 时导**全部** timer（limit=20000，缓存另存一份）：按名字找效果时，热门前 200 里
    通常没有它（例如 NS_WallPortal 只占 0.09%），必须全量才能给出准确数字。
    """
    from pipeline.extract import b2, metric_json
    safe = thread.replace(" ", "_")
    name = "b2_timerstats_%s%s.json" % (safe, "_full" if full else "")
    cache = _cache_path(base_for_cache, name)
    if not os.path.isfile(cache):
        if not use_b2:
            return {}
        limit = 20000 if full else top
        ok, _text = b2.run_mode("timerstats", trace, cache, limit=limit, start=start, end=end,
                                thread=thread)
        if not ok:
            return {}
    payload = metric_json.load_path(cache)
    rows = payload.get("timers") or []
    out = {}
    for t in rows:
        out[t.get("name")] = t
        # 名字里带参数的 scope（如 TemporalSuperResolution(sg.X=1)）按前缀也能对上
        base = str(t.get("name") or "").split("(")[0]
        out.setdefault(base, t)
    return out


def match(names, pattern, regex=False, limit=200):
    if regex:
        rx = re.compile(pattern, re.IGNORECASE)
        hits = [n for n in names if n and rx.search(n)]
    else:
        needle = pattern.lower()
        hits = [n for n in names if n and needle in n.lower()]
    return sorted(set(hits))[:limit]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="按名字在 trace 里找 scope（特效/函数/子系统）")
    ap.add_argument("target", help="run 目录或 .utrace")
    ap.add_argument("--name", required=True, help="子串（或 --regex 时的正则）")
    ap.add_argument("--regex", action="store_true")
    ap.add_argument("--thread", default="GameThread", help="取窗口统计用哪个线程")
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--b2", action="store_true", help="缓存缺失时现场跑 b2（大 trace 较慢）")
    ap.add_argument("--full", action="store_true",
                    help="导出全部 timer 再匹配（精确，但要多跑一次 B2；适合查低占比特效）")
    ap.add_argument("--out", default=None, help="把结果写成 JSON")
    a = ap.parse_args(argv)

    trace = _trace_of(a.target)
    if not os.path.isfile(trace):
        print("[error] trace not found: %s" % trace)
        return 1
    try:
        timers = load_timers(trace, a.target, a.b2)
    except (FileNotFoundError, RuntimeError) as exc:
        print("[error] %s" % exc)
        return 1

    names = [n for n, _i in timers]
    hits = match(names, a.name, a.regex)
    print("[find] trace 里共 %d 个 timer，'%s' 命中 %d 个" % (len(names), a.name, len(hits)))
    if not hits:
        print("  没命中。可能原因：① 该效果/函数没有命名 scope（见文件头说明）；"
              "② 名字不同（用 --regex 放宽，如 'portal|dissolve'）")
        return 3

    stats = load_stats(trace, a.target, a.thread, a.start, a.end, a.b2, max(200, a.top * 4),
                       full=a.full)
    rows = []
    for name in hits[:a.top]:
        st = stats.get(name) or {}
        rows.append({"name": name, "self_s": st.get("self_s"), "inclusive_s": st.get("inclusive_s"),
                     "calls": st.get("calls"), "self_pct_window": st.get("self_pct_window")})
    rows.sort(key=lambda r: -(r["self_s"] or 0))
    print("")
    print("%-58s %10s %8s %8s" % ("timer", "self(s)", "次数", "占窗口"))
    for r in rows:
        print("%-58s %10s %8s %8s"
              % (r["name"][:58],
                 "%.3f" % r["self_s"] if r["self_s"] is not None else "-",
                 r["calls"] if r["calls"] is not None else "-",
                 "%.2f%%" % r["self_pct_window"] if r["self_pct_window"] is not None else "-"))
    if not stats:
        print("\n（没有窗口统计：加 --b2 或在 run 目录里先跑 perfctl b2 timerstats）")
    print("\n下一步：")
    print("  perfctl evidence %s --ref \"timer:%s@stats\" --out DIR --b2   # 逐次调用分布"
          % (a.target, rows[0]["name"]))
    print("  perfctl b2 callers %s --out DIR --thread %s --timer \"%s\"   # 谁调用了它"
          % (trace, a.thread, rows[0]["name"]))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": "perf-find/0", "pattern": a.name, "regex": a.regex,
                       "timer_count": len(names), "hits": rows}, f, ensure_ascii=False, indent=1)
        print("\n[ok] %s" % os.path.abspath(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
