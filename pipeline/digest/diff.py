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

from pipeline.digest import run_digest, significance

SCHEMA = "perf-diff/0"
KIND = "cpu-diff"
DIFF_TOP = 15
DIFF_UNIVERSE = 200
NEW_GONE_CAP = 15
NOTE_V0 = ("每组 n=1，未做重复测量；本报告只给数值，不下“变快 / 变慢”结论"
           "（R9 显著性判定在 M3.3 接入）")
NOTE_R9 = ("按 R9 判定：每组 n ≥ 5、组内 CV < 3% 噪声门、Welch 阈值（97.5% 分位）；"
           "每组只有一个 run 或噪声门不过时只报数值")
METRIC = "frame_interval_p50_ms"


def run_p50(manifest, side="game"):
    """从 manifest 的 frame_semantics 取该 run 的帧间隔 p50（R15：interval，不是 duration）。"""
    fs = (manifest.get("frame_semantics") or {}).get(side) or {}
    stats = fs.get("stats") or {}
    interval = stats.get("interval_ms") or {}
    p50 = interval.get("p50")
    return float(p50) if isinstance(p50, (int, float)) else None


def pick_representative(sides, metric_side="game"):
    """组内挑一个代表 run 做 timer 表对比：p50 最接近组中位数的那个（缺失时取第一个）。"""
    pairs = [(run_p50(s["manifest"], metric_side), i) for i, s in enumerate(sides)]
    usable = [(v, i) for v, i in pairs if v is not None]
    if not usable:
        return sides[0]
    values = sorted(v for v, _ in usable)
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else 0.5 * (values[mid - 1] + values[mid])
    return sides[min(usable, key=lambda p: (abs(p[0] - median), p[1]))[1]]


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
    """兼容 v0 调用：每组一个 run。"""
    return build_group_diff([side_a], [side_b], thread, ue_root)


def build_group_diff(sides_a, sides_b, thread="GameThread", ue_root=None, metric_side="game",
                     timer_diff=True):
    """每组若干 run（M3.3）。

    - 显著性（R9）用每组的 p50 帧间隔做 Welch 判定；
    - timer 表对比只跑组内的“代表 run”（p50 最接近组中位数），报告口径里写明；
    - timer_diff=False 时只做 R9 判定（只读 manifest，不导出 trace，CI 用它做门槛）；
    - 任一侧不可测量即整体 not_comparable，不做任何导出（R11）。
    """
    warnings = []
    prepared = {}
    for key, sides in (("a", sides_a), ("b", sides_b)):
        bad = [s for s in sides if not s["manifest"].get("measurable", False)]
        if bad:
            prepared[key] = {
                "status": "not_measurable",
                "reason": bad[0]["manifest"].get("measurable_reason", "unknown reason"),
                "manifest": bad[0]["manifest"],
                "subset": len(bad),
                "group_n": len(sides),
            }
        else:
            prepared[key] = {"status": "pending", "manifest": sides[0]["manifest"]}

    if any(p["status"] != "pending" for p in prepared.values()):
        single = len(sides_a) <= 1 and len(sides_b) <= 1
        diff = {"schema": SCHEMA, "kind": KIND, "capability": "not_comparable",
                "note": NOTE_V0 if single else NOTE_R9,
                "metric": METRIC,
                "sides": {k: {"run_id": p["manifest"]["run_id"], "status": p["status"],
                              "reason": p.get("reason"),
                              "group_n": p.get("group_n", 1),
                              "not_measurable_runs": p.get("subset", 0)}
                          for k, p in prepared.items()},
                "significance": None,
                "warnings": warnings}
        return diff, render_md(diff)

    # R9：每组的 p50 序列（缺 frame_semantics 的 run 计为缺失，不进统计但记警告）
    p50s = {}
    for key, sides in (("a", sides_a), ("b", sides_b)):
        values = []
        for s in sides:
            v = run_p50(s["manifest"], metric_side)
            if v is None:
                warnings.append("[%s] %s: frame_semantics.%s.stats.interval_ms.p50 缺失，跳过"
                                % (key, s["manifest"].get("run_id"), metric_side))
            else:
                values.append(v)
        p50s[key] = values
    sig = significance.judge(p50s["a"], p50s["b"], unit="ms", label=METRIC)
    sig["metric"] = METRIC
    sig["metric_side"] = metric_side

    reps = {"a": pick_representative(sides_a, metric_side),
            "b": pick_representative(sides_b, metric_side)}

    if not timer_diff:
        single = len(sides_a) <= 1 and len(sides_b) <= 1
        diff = {
            "schema": SCHEMA, "kind": KIND, "capability": "comparable",
            "timer_diff": False,
            "note": NOTE_V0 if single else NOTE_R9,
            "metric": METRIC, "significance": sig,
            "sides": {k: {"run_id": reps[k]["manifest"].get("run_id"),
                          "group_n": len(sides_a if k == "a" else sides_b),
                          "representative": reps[k]["manifest"].get("run_id"),
                          "p50s_ms": sig["groups"][k]["values"]} for k in ("a", "b")},
            "top_delta": None, "wait": None, "new_in_b": None, "only_in_a": None,
            "warnings": warnings,
        }
        return diff, render_md(diff)

    for key in ("a", "b"):
        p = prepared[key]
        status, rows, caliber, w = run_digest.prepare_rows(
            reps[key]["run_dir"], p["manifest"], reps[key]["trace"], thread, ue_root,
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
        "schema": SCHEMA, "kind": KIND, "capability": "comparable",
        "timer_diff": True,
        "note": NOTE_R9 if (sig["groups"]["a"]["n"] >= significance.MIN_N
                            or sig["groups"]["b"]["n"] >= significance.MIN_N) else NOTE_V0,
        "metric": METRIC,
        "significance": sig,
        "sides": {"a": side_summary(rows_a, cal_a), "b": side_summary(rows_b, cal_b)},
        "top_delta": top_delta,
        "wait": {"a": wt_a, "b": wt_b, "delta_s": round(wt_b["self_s"] - wt_a["self_s"], 3) + 0.0,
                 "rows": wait_delta},
        "new_in_b": {"rows": new_rows, "total": new_total},
        "only_in_a": {"rows": gone_rows, "total": gone_total},
        "warnings": warnings,
    }
    diff["sides"]["a"]["group_n"] = len(sides_a)
    diff["sides"]["b"]["group_n"] = len(sides_b)
    diff["sides"]["a"]["representative"] = reps["a"]["manifest"].get("run_id")
    diff["sides"]["b"]["representative"] = reps["b"]["manifest"].get("run_id")
    return diff, render_md(diff)


def render_md(diff):
    a, b = diff["sides"]["a"], diff["sides"]["b"]

    def side_label(side, suffix):
        n = side.get("group_n") or 1
        if n <= 1:
            return side["run_id"]
        return "%s（%d runs，代表 %s）" % (suffix, n, side.get("representative") or side["run_id"])

    lines = ["# Perf diff — %s — %s vs %s" % (diff["kind"], side_label(a, "A"), side_label(b, "B")), "",
             "## 说明", diff["note"], ""]
    if diff["capability"] != "comparable":
        lines += ["## 两侧状态", ""]
        for key in ("a", "b"):
            s = diff["sides"][key]
            lines.append("- %s: %s（%s）" % (key, s["run_id"], s.get("reason") or s["status"]))
        lines += ["", "## 警告"]
        lines += ["- %s" % w for w in diff["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"

    if not diff.get("timer_diff", True):
        # 只做 R9：不读 trace，只给组统计与判定（CI 门槛用）
        lines += ["## 口径", "| | A | B |", "|---|---|---|",
                  "| run_id（代表） | %s | %s |" % (a.get("representative"), b.get("representative")),
                  "| 组内 run 数 | %s | %s |" % (a.get("group_n"), b.get("group_n")),
                  "| 本报告范围 | 只做 R9 显著性判定（不导出 trace，无 timer 差值表） | |", ""]
        if diff.get("significance"):
            lines += render_significance(diff["significance"])
        lines += ["## 警告"]
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
              "| 组内 run 数 / 代表 run | %s / %s | %s / %s |" % (a.get("group_n"), a.get("representative"),
                                                              b.get("group_n"), b.get("representative")),
              ""]
    sig = diff.get("significance")
    if sig:
        lines += render_significance(sig)
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


def render_significance(sig):
    """R9 段落：组统计表 + 阈值 + 结论。"""
    ga, gb = sig["groups"]["a"], sig["groups"]["b"]
    lines = ["## 显著性（R9：每组 n ≥ 5、组内 CV < 3%、Welch 阈值）", "",
             "指标：%s（%s 侧，R15 用 interval 不用 duration）" % (sig.get("metric"), sig.get("metric_side")),
             "", "| 组 | n | 各 run p50 (ms) | median | mean | std | CV% |", "|---|---|---|---|---|---|---|"]
    for name, g in (("A", ga), ("B", gb)):
        lines.append("| %s | %d | %s | %s | %s | %s | %s |" % (
            name, g["n"], ", ".join("%.3f" % v for v in g["values"]) or "-",
            "-" if g["median"] is None else "%.3f" % g["median"],
            "-" if g["mean"] is None else "%.3f" % g["mean"],
            "-" if g["std"] is None else "%.3f" % g["std"],
            "-" if g["cv_pct"] is None else "%.2f%%" % g["cv_pct"]))
    lines.append("")
    if sig.get("delta_mean") is not None:
        w = sig["welch"]
        lines += [
            "Δmean = %+.3f ms，Δmedian = %+.3f ms" % (sig["delta_mean"], sig["delta_median"]),
            "阈值 = max(0.5 ms, t × SE) = **%.3f ms**（SE=%.3f，Welch df=%s，t97.5=%s）"
            % (sig["threshold"], w["se"], w["df"], w["t_975"]),
            "MDE（80%% 检出率）= %.3f ms —— 比这更小的真实差异测不出来" % sig["mde_80"],
            "",
            "**结论：%s**" % significance.verdict_short(sig), ""]
    for note in sig.get("notes") or []:
        lines.append("- %s" % note)
    lines.append("")
    return lines


def fmt_range(window):
    return "none" if window is None else "[%.4f, %.4f]" % (window[0], window[1])


def fmt_bridge(effective):
    return {True: "生效", False: "未生效", None: "未知"}.get(effective, str(effective))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="perf_diff：两组 run 的 cpu 对比（PLAN §3、R9）")
    ap.add_argument("runs", nargs="*", help="兼容写法：<run_a> <run_b>（每组一个 run）")
    ap.add_argument("-a", "--group-a", nargs="+", default=None,
                    help="A 组 run 目录（可多个；与 -b 一起用时取代位置参数）")
    ap.add_argument("-b", "--group-b", nargs="+", default=None,
                    help="B 组 run 目录（R9 判定要求每组 ≥ 5 个）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--thread", default="GameThread", help="统计哪个线程（默认 GameThread）")
    ap.add_argument("--metric-side", default="game", choices=("game", "render"),
                    help="R9 用哪一侧的帧间隔 p50（默认 game）")
    ap.add_argument("--significance-only", action="store_true",
                    help="只做 R9 判定：只读 manifest 的 p50，不导出 trace（CI 门槛）")
    ap.add_argument("--trace-a", default=None, help="A 侧 trace（默认 manifest.trace.file，再退 <run_a>/trace.utrace）")
    ap.add_argument("--trace-b", default=None, help="B 侧 trace（同上）")
    ap.add_argument("--manifest-a", default=None, help="A 侧 manifest（默认 <run_a>/manifest.json）")
    ap.add_argument("--manifest-b", default=None, help="B 侧 manifest（默认 <run_b>/manifest.json）")
    ap.add_argument("--ue-root", default=None, help="窗口导出使用的 UE 根目录")
    ap.add_argument("--fail-on", default="none", choices=("none", "regression", "significant"),
                    help="CI 闸门：none 只报告 / regression 变慢才拦 / significant 变慢或变快都拦")
    ap.add_argument("--max-delta-ms", type=float, default=None,
                    help="预算闸门：Δmean 超过这个毫秒数就拦（与显著性无关）")
    ap.add_argument("--max-delta-pct", type=float, default=None,
                    help="预算闸门：Δmean 相对 A 均值超过这个百分比就拦")
    ap.add_argument("--strict", action="store_true",
                    help="数据不足（n 不够 / 噪声门不合格）时也算失败（退出码 2）")
    ap.add_argument("--json-line", action="store_true",
                    help="额外打一行 JSON 摘要（给 CI 记录 / PR 评论用）")
    args = ap.parse_args(argv)

    if args.group_a and args.group_b:
        group_a, group_b = list(args.group_a), list(args.group_b)
    elif args.group_a or args.group_b:
        print("[error] -a 与 -b 必须成对给出")
        return 2
    elif len(args.runs) == 2:
        group_a, group_b = [args.runs[0]], [args.runs[1]]
    else:
        print("[error] 用法：perfctl diff cpu <run_a> <run_b> --out DIR")
        print("      或：perfctl diff cpu -a R1 R2 R3 R4 R5 -b R6 R7 R8 R9 R10 --out DIR")
        return 2

    def load(run_dirs, key, trace_arg, manifest_arg):
        sides = []
        require_trace = not args.significance_only
        for run_dir in run_dirs:
            run_dir = os.path.abspath(run_dir)
            # 允许直接把 manifest.json 路径当 run 传（CI 的 manifests-only 包解压后就是这种形态）
            if manifest_arg is None and run_dir.lower().endswith("manifest.json"):
                manifest_path = run_dir
                run_dir = os.path.dirname(run_dir)
            else:
                manifest_path = manifest_arg or os.path.join(run_dir, "manifest.json")
            if not os.path.isfile(manifest_path):
                print("[error] %s: manifest not found: %s（先运行 perfctl manifest <run_dir>）"
                      % (key, manifest_path))
                return None
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
            trace = run_digest.resolve(run_dir, trace_arg or manifest.get("trace", {}).get("file"), "trace.utrace")
            if require_trace and manifest.get("measurable", False) and not os.path.isfile(trace):
                print("[error] %s: trace not found: %s（只做 R9 可加 --significance-only）" % (key, trace))
                return None
            sides.append({"run_dir": run_dir, "manifest": manifest, "trace": trace})
        return sides

    sides_a = load(group_a, "a", args.trace_a, args.manifest_a)
    sides_b = load(group_b, "b", args.trace_b, args.manifest_b) if sides_a else None
    if not sides_a or not sides_b:
        return 1

    diff, md = build_group_diff(sides_a, sides_b, args.thread, args.ue_root, args.metric_side,
                                timer_diff=not args.significance_only)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "perf_diff.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(diff, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "perf_diff.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print("[ok] %s" % os.path.join(os.path.abspath(args.out), "perf_diff.md"))
    print("  %s vs %s: %s" % (diff["sides"]["a"]["run_id"], diff["sides"]["b"]["run_id"], diff["capability"]))
    sig = diff.get("significance")
    if sig:
        ga, gb = sig["groups"]["a"], sig["groups"]["b"]
        print("  R9: n=%d/%d, CV=%s/%s, Δmean=%s, 阈值=%s, MDE=%s"
              % (ga["n"], gb["n"],
                 "-" if ga["cv_pct"] is None else "%.2f%%" % ga["cv_pct"],
                 "-" if gb["cv_pct"] is None else "%.2f%%" % gb["cv_pct"],
                 sig.get("delta_mean"), sig.get("threshold"), sig.get("mde_80")))
        print("  %s" % significance.verdict_text(sig))
    for w in diff["warnings"]:
        print("  warning: %s" % w)

    gate = significance.gate(sig, fail_on=args.fail_on, max_delta_ms=args.max_delta_ms,
                             max_delta_pct=args.max_delta_pct, strict=args.strict) if sig else {
        "exit_code": significance.EXIT_USAGE if args.strict else 0,
        "decision": "insufficient_data",
        "reasons": ["没有 significance 段（两侧缺 p50）"]}
    diff["gate"] = gate
    with open(os.path.join(args.out, "perf_diff.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(diff, f, ensure_ascii=False, indent=1)

    for reason in gate["reasons"]:
        print("  gate: %s" % reason)
    print("  gate 结论：%s（exit=%d）" % (gate["decision"], gate["exit_code"]))

    if args.json_line:
        ga = (sig or {}).get("groups", {}).get("a", {})
        gb = (sig or {}).get("groups", {}).get("b", {})
        line = {
            "decision": gate["decision"], "exit": gate["exit_code"],
            "verdict": (sig or {}).get("verdict"), "status": (sig or {}).get("status"),
            "delta_mean_ms": (sig or {}).get("delta_mean"),
            "threshold_ms": (sig or {}).get("threshold"), "mde80_ms": (sig or {}).get("mde_80"),
            "n_a": ga.get("n"), "n_b": gb.get("n"),
            "cv_a_pct": ga.get("cv_pct"), "cv_b_pct": gb.get("cv_pct"),
            "max_delta_ms": args.max_delta_ms, "max_delta_pct": args.max_delta_pct,
            "fail_on": args.fail_on,
        }
        print("PERF_GATE_JSON " + json.dumps(line, ensure_ascii=False, sort_keys=True))
    return gate["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
