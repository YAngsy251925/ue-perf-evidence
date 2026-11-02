#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff.py — perf_diff v0：两个 run 的 cpu 对比（PLAN §3、R9，M1.6）

用法：perfctl diff cpu <run_a> <run_b> --out DIR [--thread GameThread] [--trace-a F] [--trace-b F]

v0 范围（R9 的显著性判定在 M3.3 接入）：
  - 每组一个 run（n=1），报告固定带说明：未做重复测量，只给数值，不下"变快 / 变慢"结论
  - 对比内容：Top 自耗时差值（A/B 各自 Top15 的并集里挑 |Δ| 最大的 15 行）、等待类差值、
    B 新增 / A 独有的 timer
  - 每侧用 top-200 的窗口导出（digest v1 用 top-50），所以两侧总数与等待类占比和 digest 不相等，
    报告口径行里写明来源文件
  - 任一侧 not_measurable（nullrhi 等）或没有统计窗口 → capability: not_comparable，不出任何数字
  - 两侧都按名合并后再比；导出按 timer ID 聚合，同名行先合并（run_digest.merge_by_name）
同一输入逐字节同一输出；md 给对话用，json 给程序用，两者内容相同。
"""
import argparse
import json
import os
import sys

from pipeline.digest import run_digest

SCHEMA = "perf-diff/0"
KIND = "cpu-diff"
DIFF_TOP = 15
DIFF_UNIVERSE = 200
NEW_GONE_CAP = 15
NOTE_V0 = ("每组 n=1，未做重复测量；本报告只给数值，不下“变快 / 变慢”结论"
           "（R9 显著性判定在 M3.3 接入）")


def delta_rows(rows_a, rows_b, total_a, total_b, cap=DIFF_TOP):
    """Top 差值：两侧 Top-N（非等待类）名字并集，按 |Δself| 降序、名字升序截断。时间单位秒。"""
    map_a, map_b = {r["name"]: r for r in rows_a}, {r["name"]: r for r in rows_b}
    top_a = sorted(rows_a, key=lambda r: -r["excl"])[:cap]
    top_b = sorted(rows_b, key=lambda r: -r["excl"])[:cap]
    union = {r["name"] for r in top_a} | {r["name"] for r in top_b}
    items = []
    for n in union:
        a_s = map_a[n]["excl"] if n in map_a else 0.0
        b_s = map_b[n]["excl"] if n in map_b else 0.0
        a_c = map_a[n]["count"] if n in map_a else 0
        b_c = map_b[n]["count"] if n in map_b else 0
        items.append({
            "name": n,
            "a_s": round(a_s, 3), "b_s": round(b_s, 3), "delta_s": round(b_s - a_s, 3) + 0.0,  # +0.0 归一 -0.0
            "a_pct": round(100.0 * a_s / total_a, 2), "b_pct": round(100.0 * b_s / total_b, 2),
            "calls_a": a_c, "calls_b": b_c,
        })
    items.sort(key=lambda d: (-abs(d["delta_s"]), d["name"]))
    return items[:cap]


def side_only(rows_own, rows_other, total_own, cap=NEW_GONE_CAP):
    """只在单侧有自耗时的 timer（另一侧没有或为 0），按 self 降序截断。返回 (行视图, 总数)。"""
    map_other = {r["name"]: r for r in rows_other}
    hits = [r for r in rows_own if r["excl"] > 0 and map_other.get(r["name"], {"excl": 0.0})["excl"] == 0.0]
    hits.sort(key=lambda r: -r["excl"])
    view = [{"name": r["name"], "self_s": round(r["excl"], 3),
             "self_pct": round(100.0 * r["excl"] / total_own, 2), "calls": r["count"]} for r in hits[:cap]]
    return view, len(hits)


def side_summary(rows, caliber):
    _, wait, wait_total = run_digest.split_wait(rows)
    others = sorted((r for r in rows if not run_digest.WAIT_RE.search(r["name"])), key=lambda r: -r["excl"])
    return {
        "run_id": caliber["run_id"],
        "rhi": caliber["rhi"], "rhi_state": caliber["rhi_state"], "rhi_source": caliber["rhi_source"],
        "stats_window": caliber["stats_window"], "window": caliber["window"],
        "bridge_effective": caliber["bridge_effective"],
        "thread_frames": caliber["thread_frames"],
        "source_csv": caliber["source_csv"],
        "timers": len(rows),
        "total_self_s": round(sum(r["excl"] for r in rows), 3),
        "wait_total": wait_total,
        "top1": {"name": others[0]["name"], "self_s": round(others[0]["excl"], 3)} if others else None,
    }


def build_diff(side_a, side_b, thread="GameThread", ue_root=None):
    """side = {"run_dir", "manifest", "trace"}。返回 (diff dict, md)。
    先分类两侧：任一侧不可测量即整体 not_comparable，不给可测的那一侧做任何导出。"""
    warnings = []
    prepared = {}
    for key, side in (("a", side_a), ("b", side_b)):
        manifest = side["manifest"]
        if not manifest.get("measurable", False):
            prepared[key] = {"status": "not_measurable", "reason": manifest.get("measurable_reason", "unknown reason"),
                             "manifest": manifest}
        else:
            prepared[key] = {"status": "pending", "manifest": manifest}

    if any(p["status"] != "pending" for p in prepared.values()):
        diff = {"schema": SCHEMA, "kind": KIND, "capability": "not_comparable", "note": NOTE_V0,
                "sides": {k: {"run_id": p["manifest"]["run_id"], "status": p["status"],
                              "reason": p.get("reason")} for k, p in prepared.items()},
                "warnings": warnings}
        return diff, render_md(diff)

    for key in ("a", "b"):
        p = prepared[key]
        status, rows, caliber, w = run_digest.prepare_rows(
            side_a["run_dir"] if key == "a" else side_b["run_dir"],
            p["manifest"], side_a["trace"] if key == "a" else side_b["trace"], thread, ue_root,
            export_top=DIFF_UNIVERSE, outdir_suffix=("_top%d" % DIFF_UNIVERSE))
        p.update({"status": status, "rows": rows, "caliber": caliber})
        warnings += ["[%s] %s" % (key, x) for x in w]

    rows_a, cal_a = prepared["a"]["rows"], prepared["a"]["caliber"]
    rows_b, cal_b = prepared["b"]["rows"], prepared["b"]["caliber"]
    total_a = sum(r["excl"] for r in rows_a)
    total_b = sum(r["excl"] for r in rows_b)
    others_a, wait_a, wt_a = run_digest.split_wait(rows_a)
    others_b, wait_b, wt_b = run_digest.split_wait(rows_b)

    top_delta = delta_rows(others_a, others_b, total_a, total_b)
    wait_delta = delta_rows(wait_a, wait_b, total_a, total_b, cap=DIFF_TOP)
    # 新增 / 消失只看非等待类：等待 timer 的此消彼长已经在 wait.rows 里体现（R5）
    new_rows, new_total = side_only(others_b, others_a, total_b)
    gone_rows, gone_total = side_only(others_a, others_b, total_a)

    diff = {
        "schema": SCHEMA, "kind": KIND, "capability": "comparable", "note": NOTE_V0,
        "sides": {"a": side_summary(rows_a, cal_a), "b": side_summary(rows_b, cal_b)},
        "top_delta": top_delta,
        "wait": {"a": wt_a, "b": wt_b, "delta_s": round(wt_b["self_s"] - wt_a["self_s"], 3) + 0.0,
                 "rows": wait_delta},
        "new_in_b": {"rows": new_rows, "total": new_total},
        "only_in_a": {"rows": gone_rows, "total": gone_total},
        "warnings": warnings,
    }
    return diff, render_md(diff)


def render_md(diff):
    a, b = diff["sides"]["a"], diff["sides"]["b"]
    lines = ["# Perf diff — %s — %s vs %s" % (diff["kind"], a["run_id"], b["run_id"]), "",
             "## 说明", diff["note"], ""]
    if diff["capability"] != "comparable":
        lines += ["## 两侧状态", ""]
        for key in ("a", "b"):
            s = diff["sides"][key]
            lines.append("- %s: %s（%s）" % (key, s["run_id"], s.get("reason") or s["status"]))
        lines += ["", "## 警告"]
        lines += ["- %s" % w for w in diff["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"

    lines += ["## 口径",
              "| | A | B |", "|---|---|---|",
              "| run_id | %s | %s |" % (a["run_id"], b["run_id"]),
              "| rhi | %s（%s） | %s（%s） |" % (a["rhi"], a["rhi_state"], b["rhi"], b["rhi_state"]),
              "| 窗口 %s | %s | %s |" % (a["stats_window"], fmt_range(a["window"]), fmt_range(b["window"])),
              "| 桥接 | %s | %s |" % (fmt_bridge(a["bridge_effective"]), fmt_bridge(b["bridge_effective"])),
              "| 帧数(窗口内) game/render | %s/%s | %s/%s |" % (a["thread_frames"]["game"], a["thread_frames"]["render"],
                                                               b["thread_frames"]["game"], b["thread_frames"]["render"]),
              "| timers(按名合并, top-%d 导出) | %s | %s |" % (DIFF_UNIVERSE, a["timers"], b["timers"]),
              "| total_self_s | %s | %s |" % (a["total_self_s"], b["total_self_s"]),
              "| 等待类合计 | %ss (%s%%) | %ss (%s%%) |" % (a["wait_total"]["self_s"], a["wait_total"]["self_pct"],
                                                            b["wait_total"]["self_s"], b["wait_total"]["self_pct"]),
              ""]
    lines += ["## Top 自耗时差值（A / B 各自 Top%d 的并集，|Δ| 降序）" % DIFF_TOP,
              "| timer | A self_s | B self_s | Δself_s | A% | B% | calls A→B |", "|---|---|---|---|---|---|---|"]
    lines += ["| %s | %s | %s | %+s | %s | %s | %s→%s |" % (
        r["name"], r["a_s"], r["b_s"], r["delta_s"], r["a_pct"], r["b_pct"], r["calls_a"], r["calls_b"])
        for r in diff["top_delta"]]
    lines += ["", "## 等待类差值（R5：症状，不进瓶颈候选）",
              "wait_total: %ss (%s%%) → %ss (%s%%)，Δ=%+ss" % (
                  a["wait_total"]["self_s"], a["wait_total"]["self_pct"],
                  b["wait_total"]["self_s"], b["wait_total"]["self_pct"], diff["wait"]["delta_s"]), ""]
    if diff["wait"]["rows"]:
        lines += ["| timer | A self_s | B self_s | Δself_s | calls A→B |", "|---|---|---|---|---|"]
        lines += ["| %s | %s | %s | %+s | %s→%s |" % (
            r["name"], r["a_s"], r["b_s"], r["delta_s"], r["calls_a"], r["calls_b"])
            for r in diff["wait"]["rows"]]
        lines.append("")
    for key, title in (("new_in_b", "B 新增（A 中没有或为 0）"), ("only_in_a", "A 独有（B 中没有或为 0）")):
        block = diff[key]
        lines += ["## %s（共 %d 个）" % (title, block["total"]), ""]
        if block["rows"]:
            lines += ["| timer | self_s | self% | calls |", "|---|---|---|---|"]
            lines += ["| %s | %s | %s%% | %s |" % (r["name"], r["self_s"], r["self_pct"], r["calls"])
                      for r in block["rows"]]
            lines.append("")
    lines += ["## 警告"]
    lines += ["- %s" % w for w in diff["warnings"]] or ["- （无）"]
    return "\n".join(lines) + "\n"


def fmt_range(window):
    return "none" if window is None else "[%.4f, %.4f]" % (window[0], window[1])


def fmt_bridge(effective):
    return {True: "生效", False: "未生效", None: "未知"}.get(effective, str(effective))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="perf_diff v0：两个 run 的 cpu 对比（PLAN §3）")
    ap.add_argument("run_a", help="A 侧 run 目录（含 manifest.json）")
    ap.add_argument("run_b", help="B 侧 run 目录（含 manifest.json）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--thread", default="GameThread", help="统计哪个线程（默认 GameThread）")
    ap.add_argument("--trace-a", default=None, help="A 侧 trace（默认 manifest.trace.file，再退 <run_a>/trace.utrace）")
    ap.add_argument("--trace-b", default=None, help="B 侧 trace（同上）")
    ap.add_argument("--manifest-a", default=None, help="A 侧 manifest（默认 <run_a>/manifest.json）")
    ap.add_argument("--manifest-b", default=None, help="B 侧 manifest（默认 <run_b>/manifest.json）")
    ap.add_argument("--ue-root", default=None, help="窗口导出使用的 UE 根目录")
    args = ap.parse_args(argv)

    sides = {}
    for key, run_dir, trace_arg, manifest_arg in (
            ("a", args.run_a, args.trace_a, args.manifest_a),
            ("b", args.run_b, args.trace_b, args.manifest_b)):
        run_dir = os.path.abspath(run_dir)
        manifest_path = manifest_arg or os.path.join(run_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            print("[error] %s: manifest not found: %s（先运行 perfctl manifest <run_dir>）" % (key, manifest_path))
            return 1
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        trace = run_digest.resolve(run_dir, trace_arg or manifest.get("trace", {}).get("file"), "trace.utrace")
        if manifest.get("measurable", False) and not os.path.isfile(trace):
            print("[error] %s: trace not found: %s" % (key, trace))
            return 1
        sides[key] = {"run_dir": run_dir, "manifest": manifest, "trace": trace}

    diff, md = build_diff(sides["a"], sides["b"], args.thread, args.ue_root)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "perf_diff.json"), "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "perf_diff.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print("[ok] %s" % os.path.join(os.path.abspath(args.out), "perf_diff.md"))
    print("  %s vs %s: %s" % (diff["sides"]["a"]["run_id"], diff["sides"]["b"]["run_id"], diff["capability"]))
    for w in diff["warnings"]:
        print("  warning: %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
