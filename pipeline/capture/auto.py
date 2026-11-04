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


def candidate_dirs(project=None, extra=None):
    """搜索目录：<Project>/Saved/Profiling、其子目录（MemReports 那套结构不用），外加 --dir。"""
    dirs = []
    if project:
        project = os.path.abspath(project)
        saved = os.path.join(project, "Saved") if os.path.isdir(os.path.join(project, "Saved")) \
            else os.path.join(os.path.dirname(project), "Saved")
        dirs.append(os.path.join(saved, "Profiling"))
    for d in extra or []:
        dirs.append(os.path.abspath(d))
    return [d for d in dirs if os.path.isdir(d)]


def find_latest(dirs, min_age_s=10.0, now=None):
    """返回 (path, mtime) 或 (None, None)。按 mtime 倒序，跳过"刚写过、可能还在录"的文件。"""
    now = time.time() if now is None else now
    best = (None, None)
    for root in dirs:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.lower().endswith(".utrace"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime < min_age_s:
                    continue          # 可能还在写
                if best[1] is None or mtime > best[1]:
                    best = (path, mtime)
    return best


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
    a = ap.parse_args(argv)

    views = [v.strip() for v in a.views.split(",") if v.strip()]
    valid = {"cpu", "bound", "gpu", "config", "hitch", "memory", "allocs", "load", "sampler"}
    bad = [v for v in views if v not in valid]
    if bad:
        print("[error] 未知视图：%s（可用：%s）" % (", ".join(bad), ", ".join(sorted(valid))))
        return 2

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
