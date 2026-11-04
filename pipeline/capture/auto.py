#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto.py — "分析最新的 utrace"（M8.2）

目标：开发时仍然照常录 trace（编辑器里按 Insights 的录制按钮，或打包的包自己写），
然后**不用打开 .utrace**，只要对 agent 说一句"分析最新的那条"，让它跑这一条命令：

  perfctl auto [--project <uproject 或工程目录>] [--dir <额外搜索目录>] [--views cpu,bound]
               [--min-age 10] [--run-root runs] [--b2] [--json]

它做四件事：
  1. 在 `<Project>/Saved/Profiling/**`（以及 --dir 给的目录）里找**最新的 .utrace**；
     --min-age 秒内的文件视为"可能还在录"，跳过（默认 10 秒）；
  2. 把它 ingest 进 `runs/auto-<时间戳>/`（复用 M8.1 的 ingest：建 manifest，标 capture_mode）；
  3. 跑指定的视图（默认 cpu + bound；--b2 才连 GPU 线）；
  4. 打一份**合并摘要**（结论行 + 各视图的关键几行），并写出 `<run>/auto/summary.md`。

对 agent 友好：`--json` 输出一行结构化结果（run 目录、trace、各视图结论、summary 路径）。
"""
import argparse
import io
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_VIEWS = ("cpu", "bound")


def unreal_trace_root():
    """UnrealTrace 的公共根（store、settings、server 日志都在这里）。

    Windows: %LOCALAPPDATA%/UnrealEngine/Common/UnrealTrace
    其它平台按 UE 的约定退化到 ~/UnrealTrace（Gauntlet 源码里同样是这两套）。
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, "UnrealEngine", "Common", "UnrealTrace")
    return os.path.join(os.path.expanduser("~"), "UnrealTrace")


def store_dir_from_settings(root=None):
    """从 trace server 的 Settings.ini 读它实际用的 store 目录（**权威来源**）。

    服务端日志里能看到它就是从这里读的：
        Reading settings from '<root>/Settings.ini'
         - Directory: '<root>/Store/001'
    文件不存在（默认情况）就返回 None。
    """
    root = root or unreal_trace_root()
    path = os.path.join(root, "Settings.ini")
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.strip().lower() in ("directory", "storedirectory", "store"):
            value = value.strip().strip('"')
            if value:
                return value if os.path.isabs(value) else os.path.join(root, value)
    return None


def store_dir_from_server_log(root=None):
    """从最新的 Server_*.log 读服务端**实际**使用的目录（比 Settings.ini 更实时）。

    日志行：` - Directory: 'C:/Users/.../UnrealTrace/Store/001'`
    """
    root = root or unreal_trace_root()
    if not os.path.isdir(root):
        return None
    logs = [os.path.join(root, n) for n in os.listdir(root)
            if n.lower().startswith("server_") and n.lower().endswith(".log")]
    if not logs:
        return None
    logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    pattern = re.compile(r"Directory:\s*'([^']+)'")
    for path in logs[:5]:
        try:
            with io.open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        found = pattern.findall(text)
        if found:
            return found[-1]
    return None


def trace_store_dirs():
    """store 搜索目录，按可信度排序：

    1. Settings.ini 里配的 Directory（权威配置）；
    2. 最新 Server_*.log 里打印的实际目录（最实时）；
    3. `<root>/Store` 及其每个 `<编号>` 子目录（默认布局，逐层枚举）；
    4. 环境变量 UE_TRACE_STORE（给非常规部署/CI 用）。
    """
    root = unreal_trace_root()
    dirs = []
    for candidate in (store_dir_from_settings(root), store_dir_from_server_log(root)):
        if candidate and os.path.isdir(candidate):
            dirs.append(candidate)
    default_store = os.path.join(root, "Store")
    if os.path.isdir(default_store):
        dirs.append(default_store)
        for name in sorted(os.listdir(default_store)):
            path = os.path.join(default_store, name)
            if os.path.isdir(path):
                dirs.append(path)
    if os.environ.get("UE_TRACE_STORE"):
        extra = os.environ["UE_TRACE_STORE"]
        if os.path.isdir(extra):
            dirs.append(extra)
    # 去重保序
    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def candidate_dirs(project=None, extra=None, include_store=True):
    """搜索目录：<Project>/Saved/Profiling + UnrealTrace store（按配置解析）+ --dir。"""
    dirs = []
    if project:
        project = os.path.abspath(project)
        saved = os.path.join(project, "Saved") if os.path.isdir(os.path.join(project, "Saved"))             else os.path.join(os.path.dirname(project), "Saved")
        dirs.append(os.path.join(saved, "Profiling"))
    if include_store:
        dirs += trace_store_dirs()
    for d in extra or []:
        dirs.append(os.path.abspath(d))
    return [d for d in dirs if os.path.isdir(d)]


def _all_traces(dirs):
    out = []
    for root in dirs:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith(".utrace"):
                    out.append(os.path.join(dirpath, name))
    return out


def _pick_stable(paths, min_age_s, now, check_stable, settle_s):
    ranked = []
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime < min_age_s:
            continue
        ranked.append((path, mtime))
    ranked.sort(key=lambda kv: -kv[1])
    for path, mtime in ranked:
        if not check_stable:
            return path, mtime
        try:
            size_before = os.path.getsize(path)
            time.sleep(settle_s)
            if os.path.getsize(path) == size_before:
                return path, mtime
        except OSError:
            continue
    return (None, None)


def find_latest(dirs, min_age_s=10.0, now=None, check_stable=True, settle_s=0.4):
    """返回 (path, mtime) 或 (None, None)。

    两个"还在录"的判据：
      - mtime 距现在短于 min_age_s（默认 10s）；
      - check_stable：隔 settle_s 再 stat 一次，大小变了就跳过（store 里的活动文件持续增长）。
    最新那条还在增长时，退到"次新且稳定"的候选。
    """
    now = time.time() if now is None else now
    traces = _all_traces(dirs)
    if not traces:
        return (None, None)
    path, mtime = _pick_stable(traces, min_age_s, now, check_stable, settle_s)
    if path is None and check_stable:
        # 全都不稳定（例如刚录完、server 还在写尾巴）：放宽"稳定性"再来一次
        path, mtime = _pick_stable(traces, min_age_s, now, False, settle_s)
    return (path, mtime)


def guess_project(project):
    """--project 可以是 .uproject 文件、工程目录，或 Saved 目录。返回工程目录（用于打时间戳/日志）。"""
    if not project:
        return None
    p = os.path.abspath(project)
    # 后缀判断优先于"文件是否存在"：agent 传进来的路径可能还没落在本机（或只是约定写法）
    if p.lower().endswith(".uproject"):
        return os.path.dirname(p)
    if os.path.basename(p) == "Saved":
        return os.path.dirname(p)
    return p


def find_log(project_dir, trace_mtime, window_s=3600):
    """找与 trace 时间最接近的引擎日志（Saved/Logs/*.log）。找不到就 None（manifest 会降级但能跑）。"""
    if not project_dir:
        return None
    logs = os.path.join(project_dir, "Saved", "Logs")
    if not os.path.isdir(logs):
        return None
    best = (None, None)
    for name in os.listdir(logs):
        if not name.lower().endswith(".log"):
            continue
        path = os.path.join(logs, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        gap = abs(mtime - trace_mtime)
        if gap > window_s:
            continue
        if best[1] is None or gap < best[1]:
            best = (path, gap)
    return best[0]


def _view_main(name):
    """注意：run 级 CPU digest 在 pipeline.digest.run_digest（v1），不是 views.cpu（那是 v0 单 CSV）。"""
    from pipeline.digest import run_digest
    from pipeline.digest.views import (allocations, bound, config, gpu, hitch, load, memory, sampler)
    return {"cpu": run_digest, "bound": bound, "gpu": gpu, "config": config, "hitch": hitch,
            "memory": memory, "allocs": allocations, "load": load, "sampler": sampler}[name]


def run_views(run_dir, views, out_root, b2=False, ue_root=None, target_fps=None):
    """逐个视图跑一遍，返回 {view: (rc, md_path, 前几行摘要)}。"""
    results = {}
    for view in views:
        out = os.path.join(out_root, view)
        args = [run_dir, "--out", out]
        if view in ("bound", "gpu", "load") and b2:
            args.append("--b2")
        if view == "bound" and target_fps:
            args += ["--target-fps", str(target_fps)]
        if ue_root and view in ("gpu", "bound", "load", "allocs"):
            args += ["--ue-root", ue_root]
        try:
            rc = _view_main(view).main(args)
        except SystemExit as exc:          # 视图里 sys.exit 的情形
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:           # 单个视图炸了不要影响其它视图
            rc, _ = 1, exc
        md = os.path.join(out, "perf_digest.md")
        if not os.path.isfile(md):
            md = os.path.join(out, "%s.md" % view)
        digest_json = os.path.join(out, "perf_digest.json")
        conclusion = None
        if os.path.isfile(digest_json):
            try:
                with io.open(digest_json, encoding="utf-8") as f:
                    payload = json.load(f)
                conclusion = (payload.get("conclusion") or {}).get("text")
            except (OSError, ValueError):
                conclusion = None
        results[view] = {"rc": rc, "md": md if os.path.isfile(md) else None,
                         "conclusion": conclusion}
    return results


def trace_duration_s(trace, out_dir):
    """用 B2 的 frames 摘要拿 trace 时长（没有帧也能拿到 duration_ms）。失败返回 None。"""
    from pipeline.extract import b2
    cache = os.path.join(out_dir, "b2_frames.json")
    ok, _text = b2.run_mode("frames", trace, cache, limit=1)
    if not ok or not os.path.isfile(cache):
        return None
    try:
        with io.open(cache, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    ms = payload.get("duration_ms")
    return (ms / 1000.0) if ms else None


def no_frame_fallback(run_dir, out_root, ue_root=None,
                      threads=("GameThread", "RenderThread 0")):
    """没有帧数据时的回退：整段 CPU 聚合（b2 timerstats）。

    什么时候会发生：录的是**没有 PIE / 没有游戏的编辑器会话** —— Frame 通道开着但 0 个帧事件。
    这时帧级口径（digest cpu / bound）无从谈起，但"这段时间 CPU 花在哪"仍然可答。

    注意：B2 的 window 必须落在 trace 内部，越过末尾会让未闭合 scope 变成 inf（R13）；
    所以这里先取 duration，再留 5 秒余量。
    """
    from pipeline.extract import b2
    trace = os.path.join(run_dir, "trace.utrace")
    if not os.path.isfile(trace):
        return None
    duration = trace_duration_s(trace, out_root)
    if not duration:
        return None
    end = max(1.0, duration - 5.0)
    result = {"duration_s": round(duration, 3), "window": [0.0, round(end, 3)], "threads": {}}
    for thread in threads:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", thread)
        cache = os.path.join(out_root, "b2_timerstats_%s.json" % safe)
        ok, _text = b2.run_mode("timerstats", trace, cache, limit=25, start=0.0, end=end, thread=thread)
        if not ok or not os.path.isfile(cache):
            continue
        try:
            with io.open(cache, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        rows = [{"name": t.get("name"), "calls": t.get("calls"), "self_s": t.get("self_s"),
                 "pct": t.get("self_pct_window")} for t in (payload.get("timers") or [])[:12]]
        total = ((payload.get("totals") or {}).get("sum_self_s")) or 0.0
        result["threads"][thread] = {"sum_self_s": round(total, 3),
                                     "busy_pct": round(100.0 * total / end, 2) if end else None,
                                     "rows": rows}
    return result


def render_no_frame(section):
    lines = ["## 没有帧数据（多半是还没 PIE）", "",
             "trace 时长 %.1f s，`Frame`/`Cpu` 通道开着但**没有任何帧事件** —— "
             "帧级口径（cpu / bound）无从谈起。下面按整段 CPU 聚合，回答「这段时间 CPU 花在哪」。" %
             section["duration_s"], ""]
    for thread, info in section["threads"].items():
        lines += ["### %s" % thread, "",
                  "| timer | calls | self (s) | 占窗口 |", "|---|---|---|---|"]
        for row in info["rows"]:
            lines.append("| %s | %s | %.3f | %.1f%% |"
                         % (str(row["name"])[:52], row["calls"], row["self_s"] or 0.0, row["pct"] or 0.0))
        lines += ["", "该线程 self 合计 %.1f s（占窗口 %.1f%%）。" % (info["sum_self_s"], info["busy_pct"] or 0.0), ""]
    lines += ["> 想要帧级结论：**先开 PIE 再录**（Insights 录的是编辑器进程，PIE 的帧才会进 Frame 轨道）。", ""]
    return "\n".join(lines)


def _has_stats_window(run_dir):
    path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(path):
        return True          # 没有 manifest：交给视图自己报错，不在这里猜
    try:
        with io.open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return True
    return bool((manifest.get("window_basis") or {}).get("stats_window"))


def build_summary(run_dir, trace, log, views_result, capture_note=None):
    lines = ["# 自动分析：最新 utrace", "",
             "| 项 | 值 |", "|---|---|",
             "| trace | `%s` |" % trace,
             "| run 目录 | `%s` |" % run_dir,
             "| 日志 | %s |" % ("`%s`" % log if log else "（未找到，部分口径降级）"),
             "| 采集方式 | %s |" % (capture_note or "外部采集（PIE / Insights / 打包包），条件未由本工具强制"),
             ""]
    for view, info in views_result.items():
        lines.append("## %s" % view)
        lines.append("")
        if info.get("conclusion"):
            lines += ["> %s" % info["conclusion"], ""]
        elif info["rc"] != 0:
            lines += ["> 该视图失败（rc=%s），见 %s" % (info["rc"], info.get("md") or "日志"), ""]
        if info.get("md"):
            lines.append("[完整报告](%s)" % info["md"].replace(os.sep, "/"))
            lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="分析最新的 utrace（自动发现 → ingest → 视图 → 摘要）")
    ap.add_argument("--project", default=None, help=".uproject / 工程目录 / Saved 目录（找 Saved/Profiling 与 Saved/Logs）")
    ap.add_argument("--dir", action="append", default=[], help="额外搜索目录（可重复）")
    ap.add_argument("--trace", default=None, help="直接指定 trace（跳过自动发现）")
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS),
                    help="要跑的视图，逗号分隔（cpu,bound,gpu,config,hitch,memory,allocs,load,sampler）")
    ap.add_argument("--min-age", type=float, default=10.0, help="mtime 小于该秒数的 trace 视为还在写，跳过")
    ap.add_argument("--run-root", default=os.path.join(REPO, "runs"), help="run 目录放哪")
    ap.add_argument("--b2", action="store_true", help="bound/gpu/load 连 B2（慢，但 GPU 线才有数）")
    ap.add_argument("--target-fps", type=float, default=None)
    ap.add_argument("--ue-root", default=None)
    ap.add_argument("--json", action="store_true", help="输出一行结构化结果（给 agent）")
    ap.add_argument("--explain", action="store_true",
                    help="打印搜索来源（UnrealTrace 根、Settings.ini、server 日志、实际选中的文件）")
    a = ap.parse_args(argv)

    views = [v.strip() for v in a.views.split(",") if v.strip()]
    valid = {"cpu", "bound", "gpu", "config", "hitch", "memory", "allocs", "load", "sampler"}
    bad = [v for v in views if v not in valid]
    if bad:
        print("[error] 未知视图：%s（可用：%s）" % (", ".join(bad), ", ".join(sorted(valid))))
        return 2

    if a.explain:
        root = unreal_trace_root()
        print("[explain] UnrealTrace 根      : %s" % root)
        print("[explain] Settings.ini 目录   : %s" % (store_dir_from_settings(root) or "（未配置，用默认）"))
        print("[explain] server 日志目录     : %s" % (store_dir_from_server_log(root) or "（无日志）"))
        for d in candidate_dirs(a.project, a.dir):
            print("[explain] 搜索：%s" % d)

    project_dir = guess_project(a.project)
    if a.trace:
        trace, trace_mtime = os.path.abspath(a.trace), os.path.getmtime(a.trace)
        if not os.path.isfile(trace):
            print("[error] trace not found: %s" % trace)
            return 1
    else:
        dirs = candidate_dirs(a.project, a.dir)
        if not dirs:
            print("[error] 找不到搜索目录：给 --project <.uproject|工程目录> 或 --dir <目录>")
            return 2
        trace, trace_mtime = find_latest(dirs, a.min_age)
        if not trace:
            print("[error] 在 %s 里没找到 %s 秒前写过的 .utrace（刚录完的等一会儿再试，或用 --min-age 0）"
                  % ("、".join(dirs), int(a.min_age)))
            return 1

    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(trace_mtime))
    run_dir = os.path.join(os.path.abspath(a.run_root), "auto-%s" % stamp)
    log = find_log(project_dir, trace_mtime)
    print("[auto] trace   = %s" % trace)
    print("[auto] project = %s" % (project_dir or "（未给）"))
    print("[auto] log     = %s" % (log or "（未找到）"))
    print("[auto] run     = %s" % run_dir)

    from pipeline.capture import ingest
    ingest.ingest(run_dir, trace, log, project_dir, capture_mode="external",
                  note="perfctl auto：自动接住最新 utrace")
    from pipeline.caliber import manifest as manifest_mod
    rc = manifest_mod.main([run_dir])
    if rc not in (0, None):
        print("[auto] manifest 失败（rc=%s），后续视图可能降级" % rc)

    out_root = os.path.join(run_dir, "auto")
    os.makedirs(out_root, exist_ok=True)
    views_result = run_views(run_dir, views, out_root, a.b2, a.ue_root, a.target_fps)
    summary = build_summary(run_dir, trace, log, views_result)
    # 没有统计窗口（典型原因：录的是没 PIE 的编辑器会话 —— Frame 通道开着但 0 个帧事件）
    # → 走整段 CPU 回退，至少回答"这段时间 CPU 花在哪"
    if not _has_stats_window(run_dir):
        fallback = no_frame_fallback(run_dir, out_root, a.ue_root)
        if fallback:
            summary += "\n" + render_no_frame(fallback)
            print("[auto] 没有帧数据 → 已按整段 CPU 聚合（trace 时长 %.1fs）" % fallback["duration_s"])
        else:
            print("[auto] 没有帧数据，且整段 CPU 聚合也失败（见 auto/b2_timerstats*.log）")
    summary_path = os.path.join(out_root, "summary.md")
    with io.open(summary_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(summary + "\n")

    print("")
    for view, info in views_result.items():
        state = "ok" if info["rc"] == 0 else "rc=%s" % info["rc"]
        print("  %-8s %-8s %s" % (view, state, (info.get("conclusion") or "")[:110]))
    print("\n[ok] %s" % summary_path)

    if a.json:
        print("PERF_AUTO_JSON " + json.dumps({
            "run_dir": run_dir.replace(os.sep, "/"),
            "trace": trace.replace(os.sep, "/"),
            "log": (log or "").replace(os.sep, "/") or None,
            "summary": summary_path.replace(os.sep, "/"),
            "views": {k: {"rc": v["rc"], "conclusion": v.get("conclusion"), "md": v.get("md")}
                      for k, v in views_result.items()},
        }, ensure_ascii=False, sort_keys=True))
    return 0 if all(v["rc"] == 0 for v in views_result.values()) else 3


if __name__ == "__main__":
    sys.exit(main())
