#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_digest.py — run 级 digest v1（PLAN §5.3，M1.5）

输入：run 目录（含 manifest.json）。对 cpu 视图：
  1. manifest.measurable = false（如 nullrhi）→ 不做任何导出，性能数字一律不出，
     capability 记 not_measurable（Q3 后半的 digest 层，R11）
  2. manifest.window_basis.stats_window（默认 steady）为 null → 不出数字，写明原因
  3. 否则按窗口边界对该线程做一次窗口导出（A 路，约 2.2 秒），再出统计

统计规则（R5、R8）：
  - 导出按 timer ID 聚合，同名不同 ID 的行先按名字合并（如 D3D12 里两行 "Frame"），digest 以名字为主键
  - 等待类（wait / stall / idle 关键词）单列，不进入瓶颈候选；等待是症状，不是原因
  - Top-N、inclusive 榜、前缀聚合都基于排除等待类后的行
  - 省略说明给出"其余 N 个 timer 合计 x%"
  - 结论规则（确定性）：等待类合计 >=50% → clear（GT 在空等，瓶颈不在 GT 自身）；>=30% → moderate；
    否则看排除等待后的 Top1 自耗时占比：>=40% → clear，>=20% → moderate，其余 → marginal
同一输入逐字节同一输出；md 给对话用，json 给程序用，两者内容相同。
"""
import argparse
import json
import os
import re
import sys

from pipeline.digest.views import cpu
from pipeline.extract import insights_export

SCHEMA = "perf-digest/1"
TOP_N = 15
WAIT_RE = re.compile(r"wait|stall|idle", re.IGNORECASE)
WAIT_LABEL = "wait"
CONCLUSION_WAIT_CLEAR = 50.0
CONCLUSION_WAIT_MODERATE = 30.0
CONCLUSION_TOP_CLEAR = 40.0
CONCLUSION_TOP_MODERATE = 20.0


def merge_by_name(rows):
    """同名不同 timer ID 的行合并：self/incl/count 求和，emax 取最大，eavg 重算（= self/calls）。"""
    merged = {}
    for r in rows:
        m = merged.get(r["name"])
        if m is None:
            merged[r["name"]] = dict(r)
            continue
        m["excl"] += r["excl"]
        m["incl"] += r["incl"]
        m["count"] += r["count"]
        m["emax"] = max(m["emax"], r["emax"])
    for m in merged.values():
        m["eavg"] = m["excl"] / m["count"] if m["count"] else 0.0
    return list(merged.values())


def split_wait(rows):
    """R5：等待类（wait / stall / idle）单列。自耗时为 0 的等待行（如 D3D12 的 "Game thread tick wait
    time"）不进展示列表，也不计等待类的 calls。返回 (非等待行, 等待行按 self 降序, 等待合计 dict)。"""
    wait = sorted((r for r in rows if WAIT_RE.search(r["name"]) and r["excl"] > 0), key=lambda r: -r["excl"])
    others = [r for r in rows if not WAIT_RE.search(r["name"])]
    wait_self = sum(r["excl"] for r in wait)
    total = sum(r["excl"] for r in rows) or 1e-9
    return others, wait, {
        "self_s": round(wait_self, 3),
        "self_pct": round(100.0 * wait_self / total, 2),
        "count": sum(r["count"] for r in wait),
    }


def conclude(wait_pct, top_name, top_pct, top_calls):
    """结论规则（确定性，阈值见模块 docstring）。返回 (文本, 置信度)。"""
    if wait_pct >= CONCLUSION_WAIT_CLEAR:
        return ("GT 大部分时间在等待或空转（等待类合计 %.2f%%）；瓶颈不在 GT 自身，"
                "下一步检查被等待的任务与同步对象，以及 RT / GPU 侧" % wait_pct), "clear"
    if wait_pct >= CONCLUSION_WAIT_MODERATE:
        return ("GT 有 %.2f%% 的时间在等待或空转；瓶颈可能不在 GT 自身，"
                "结合等待对象与 RT / GPU 侧进一步定位" % wait_pct), "moderate"
    if top_pct >= CONCLUSION_TOP_CLEAR:
        return ("GT 自耗时集中在 %s（%.2f%%，%d 次调用），从这里入手" % (top_name, top_pct, top_calls)), "clear"
    if top_pct >= CONCLUSION_TOP_MODERATE:
        return ("GT 自耗时最高的是 %s（%.2f%%，%d 次调用），但占比不占绝对主导，结合调用方与下钻证据确认"
                % (top_name, top_pct, top_calls)), "moderate"
    return ("GT 自耗时分散，无单一主导项（Top1 %s 仅 %.2f%%）；先看前缀聚合与调用关系" % (top_name, top_pct)), "marginal"


def build_from_stats(rows, caliber, window_name, warnings):
    """纯函数：窗口统计行 → digest 核心。caliber 是口径行 dict，warnings 为调用方累积的警告。"""
    rows = merge_by_name(rows)
    total = sum(r["excl"] for r in rows) or 1e-9
    others, wait, wait_total = split_wait(rows)
    by_self = sorted(others, key=lambda r: -r["excl"])
    by_incl = sorted(others, key=lambda r: -r["incl"])
    top = by_self[:TOP_N]
    top_self = sum(r["excl"] for r in top)
    omitted_pct = 100.0 * (1.0 - (top_self + wait_total["self_s"]) / total)

    def row_view(r):
        return {"name": r["name"], "count": r["count"], "self_s": round(r["excl"], 3),
                "self_pct": round(100.0 * r["excl"] / total, 2), "incl_s": round(r["incl"], 3),
                "avg_ms": round(1000 * r["eavg"], 3), "max_ms": round(1000 * r["emax"], 3),
                "spike_ratio": round(r["emax"] / r["eavg"], 1) if r["eavg"] > 0 else None}

    top_name = top[0]["name"] if top else None
    top_pct = round(100.0 * top[0]["excl"] / total, 2) if top else 0.0
    text, confidence = conclude(wait_total["self_pct"], top_name, top_pct, top[0]["count"] if top else 0)

    refs = ["timer:%s@%s" % (r["name"], window_name) for r in top[:5]]
    refs += ["timer:%s@%s" % (r["name"], window_name) for r in wait[:3] if r["excl"] > 0]
    omitted_n = len(rows) - len(top) - len(wait)
    digest = {
        "schema": SCHEMA,
        "kind": "cpu",
        "capability": "measurable",
        "conclusion": {"text": text, "confidence": confidence},
        "caliber": caliber,
        "stats": {
            "window": window_name,
            "source_csv": caliber.get("source_csv"),
            "timers": len(rows),
            "total_self_s": round(total, 3),
            "top_by_self_time": [row_view(r) for r in top],
            "wait_timers": [row_view(r) for r in wait],
            "wait_total": wait_total,
            "top_by_inclusive": [{"name": r["name"], "incl_s": round(r["incl"], 3),
                                  "incl_pct": round(100.0 * r["incl"] / total, 2)} for r in by_incl[:TOP_N]],
            "by_prefix": cpu_prefix(others, total),
            "omitted": {"timers": max(omitted_n, 0), "self_pct": round(omitted_pct, 2)},
        },
        "refs": refs,
        "warnings": warnings,
    }
    return digest, render_md(digest)


def cpu_prefix(rows, total):
    agg = {}
    for r in rows:
        head = cpu.bucket(r["name"])
        a = agg.setdefault(head, {"excl": 0.0, "count": 0, "n": 0})
        a["excl"] += r["excl"]
        a["count"] += r["count"]
        a["n"] += 1
    out = [{"prefix": k, "self_s": round(v["excl"], 3), "self_pct": round(100.0 * v["excl"] / total, 2),
            "timers": v["n"], "calls": v["count"]} for k, v in agg.items()]
    return sorted(out, key=lambda d: -d["self_s"])[:12]


def caliber_from_manifest(manifest, source_csv):
    eng, tr = manifest["engine"], manifest["trace"]
    fs = manifest["frame_semantics"]
    win_name = manifest["window_basis"]["stats_window"]
    ref = manifest["window_basis"]["reference"]
    return {
        "run_id": manifest["run_id"],
        "profile": manifest.get("profile") or "unknown (M2.1)",
        "rhi": eng.get("rhi"), "rhi_state": eng.get("rhi_state"), "rhi_source": eng.get("rhi_source"),
        "build_config": eng.get("build_config"),
        "stats_window": win_name,
        "window": manifest["windows"].get(win_name) if win_name else None,
        "frames_window": manifest["windows"].get("frames"),
        "reference": ref,
        "bridge_effective": tr.get("statnamedevents_effective"),
        "bridge_requested": tr.get("statnamedevents_requested"),
        "thread_frames": {side: fs[side]["window_frames"] for side in ("game", "render")},
        "source_csv": source_csv,
    }


def not_measurable_digest(manifest):
    reason = manifest.get("measurable_reason", "unknown reason")
    digest = {
        "schema": SCHEMA, "kind": "cpu", "capability": "not_measurable",
        "conclusion": {"text": "capability: not_measurable —— %s；不输出任何性能结论" % reason, "confidence": "none"},
        "caliber": caliber_from_manifest(manifest, None),
        "stats": None, "refs": [], "warnings": list(manifest.get("warnings", [])),
    }
    return digest, render_md(digest)


def render_md(digest):
    c = digest["caliber"]
    lines = ["# Perf digest — %s — %s" % (digest["kind"], c["run_id"]), ""]
    lines += ["## 结论", digest["conclusion"]["text"] + "（置信度: %s）" % digest["conclusion"]["confidence"], ""]
    lines += ["## 口径",
              "- run_id: %s" % c["run_id"],
              "- profile: %s" % c["profile"],
              "- rhi: %s（state=%s, source=%s）" % (c["rhi"], c["rhi_state"], c["rhi_source"]),
              "- build_config: %s" % c["build_config"],
              "- 窗口: %s %s（成帧 %s）" % (c["stats_window"], fmt_range(c["window"]), fmt_range(c["frames_window"])),
              "- 帧轨道参考: %s" % (fmt_reference(c["reference"])),
              "- 帧数(窗口内): game=%s render=%s" % (c["thread_frames"]["game"], c["thread_frames"]["render"]),
              "- 桥接: %s（requested=%s）" % (fmt_bridge(c["bridge_effective"]), c["bridge_requested"]),
              ""]
    st = digest["stats"]
    if st is None:
        lines += ["## 警告"]
        lines += ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"
    lines += ["## Top self-time（瓶颈候选，等待类已单列）",
              "| # | timer | self_s | self% | calls | avg_ms | max_ms | spike |",
              "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(digest["stats"]["top_by_self_time"], 1):
        lines.append("| %d | %s | %s | %s%% | %s | %s | %s | %s |" % (
            i, r["name"], r["self_s"], r["self_pct"], r["count"], r["avg_ms"], r["max_ms"], r["spike_ratio"]))
    lines += ["", "## 等待类（R5：症状，不进瓶颈候选）",
              "wait_total: self_s=%s, self_pct=%s%%, calls=%s" % (
                  st["wait_total"]["self_s"], st["wait_total"]["self_pct"], st["wait_total"]["count"]),
              ""]
    if st["wait_timers"]:
        lines += ["| timer | self_s | self% | calls |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s%% | %s |" % (r["name"], r["self_s"], r["self_pct"], r["count"])
                  for r in st["wait_timers"]]
        lines.append("")
    lines += ["## 按命名前缀聚合（不含等待类）", "| prefix | self_s | self% | timers | calls |", "|---|---|---|---|---|"]
    lines += ["| %s | %s | %s%% | %s | %s |" % (r["prefix"], r["self_s"], r["self_pct"], r["timers"], r["calls"])
              for r in st["by_prefix"]]
    lines += ["", "## 省略", "其余 %s 个 timer 合计 %s%%。" % (st["omitted"]["timers"], st["omitted"]["self_pct"]), ""]
    if digest["refs"]:
        lines += ["## 下钻"] + ["- %s" % r for r in digest["refs"]] + [""]
    lines += ["## 警告"]
    lines += ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
    return "\n".join(lines) + "\n"


def fmt_range(window):
    return "none" if window is None else "[%.4f, %.4f]" % (window[0], window[1])


def fmt_reference(ref):
    if ref is None:
        return "none"
    return "%s %s %s" % (ref["side"], ref["source"], ",".join(ref["threads"]))


def fmt_bridge(effective):
    return {True: "生效", False: "未生效", None: "未知"}.get(effective, str(effective))


def resolve(run_dir, value, default_name):
    if value:
        p = value if os.path.isabs(value) else os.path.join(run_dir, value)
        return p
    return os.path.join(run_dir, default_name)


def prepare_rows(run_dir, manifest, trace, thread="GameThread", ue_root=None, export_top=50, outdir_suffix=""):
    """窗口导出（带缓存）+ 读取 + 按名合并 + 口径行。measurable=false 由调用方先处理
    （not_measurable_digest）。返回 ("ok", 合并后的行, caliber, warnings) 或
    ("blocked", 原因, caliber, warnings)；blocked 只发生在没有统计窗口时，此时不做任何导出。"""
    win_name = manifest["window_basis"].get("stats_window")
    window = manifest["windows"].get(win_name) if win_name else None
    warnings = list(manifest.get("warnings", []))
    caliber = caliber_from_manifest(manifest, None)
    if not window:
        if not win_name:
            warnings.append("no stats window (window_basis.stats_window is null)")
        else:
            warnings.append("stats window %s is null in the manifest" % win_name)
        return "blocked", "no stats window", caliber, warnings

    # 窗口导出写在 run 目录自己的 digest_export/ 下：manifest.inputs.extract 可能指向只读的实验档案。
    # 导出结果按 (trace, 窗口, top, 线程) 缓存：export.rsp 与输入 stamp 都匹配时直接复用 CSV，
    # 让 digest / diff 的重复调用与测试套件不必反复拉起 UnrealInsights（它的启动偶发很慢）。
    outdir = os.path.join(run_dir, "digest_export", "window_%s%s" % (win_name, outdir_suffix))
    cmds, _ = insights_export.window_commands(outdir, [thread], export_top, repr(window[0]), repr(window[1]))
    source_csv = os.path.join(outdir, "stats_%s_window.csv" % insights_export.safe_name(thread))
    rsp_path = os.path.join(outdir, "export.rsp")
    stamp_path = os.path.join(outdir, "export_inputs.json")
    stamp = json.dumps({"trace": os.path.abspath(trace), "thread": thread, "top": export_top,
                        "start": repr(window[0]), "end": repr(window[1])}, sort_keys=True)
    if os.path.isfile(source_csv) and os.path.isfile(rsp_path) and os.path.isfile(stamp_path):
        with open(rsp_path, encoding="utf-8") as f:
            rsp_ok = f.read() == "\n".join(cmds) + "\n"
        with open(stamp_path, encoding="utf-8") as f:
            stamp_ok = f.read() == stamp
        if rsp_ok and stamp_ok:
            print("[cache] reusing %s" % source_csv)
            caliber = caliber_from_manifest(manifest, os.path.basename(source_csv))
            return "ok", merge_by_name(cpu.load_rows(source_csv)), caliber, warnings

    argv = (["--ue-root", ue_root] if ue_root else []) + [
        "window", trace, "--out", outdir,
        "--start", repr(window[0]), "--end", repr(window[1]), "--threads", thread]
    if export_top != 50:
        argv += ["--top", str(export_top)]
    if insights_export.main(argv) != 0:
        sys.exit("[error] windowed export failed (see insights log under %s)" % outdir)
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(stamp)
    caliber = caliber_from_manifest(manifest, os.path.basename(source_csv))
    return "ok", merge_by_name(cpu.load_rows(source_csv)), caliber, warnings


def build_run_digest(run_dir, manifest, trace, thread="GameThread", ue_root=None):
    """run 级入口。measurable=false 时不导出；否则按稳态窗口做一次窗口导出。"""
    if not manifest.get("measurable", False):
        return not_measurable_digest(manifest)
    win_name = manifest["window_basis"].get("stats_window")
    status, rows, caliber, warnings = prepare_rows(run_dir, manifest, trace, thread, ue_root)
    if status == "blocked":
        digest = {"schema": SCHEMA, "kind": "cpu", "capability": "measurable",
                  "conclusion": {"text": "没有可用的统计窗口，无法给出性能结论", "confidence": "none"},
                  "caliber": caliber, "stats": None, "refs": [], "warnings": warnings}
        return digest, render_md(digest)
    return build_from_stats(rows, caliber, win_name, warnings)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="run 级 CPU digest v1（PLAN §5.3）")
    ap.add_argument("run_dir", help="run 目录（含 manifest.json；extract 或 trace 缺失时报错）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--thread", default="GameThread", help="统计哪个线程（默认 GameThread）")
    ap.add_argument("--trace", default=None, help="trace 文件（默认 manifest.trace.file，再退 <run_dir>/trace.utrace）")
    ap.add_argument("--manifest", default=None, help="manifest 路径（默认 <run_dir>/manifest.json）")
    ap.add_argument("--ue-root", default=None, help="窗口导出使用的 UE 根目录")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    manifest_path = a.manifest or os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print("[error] manifest not found: %s（先运行 perfctl manifest <run_dir>）" % manifest_path)
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    trace = a.trace or resolve(run_dir, manifest.get("trace", {}).get("file"), "trace.utrace")
    if not manifest.get("measurable", False):
        trace = None  # 不可测量的 run 不需要 trace
    elif not os.path.isfile(trace):
        print("[error] trace not found: %s" % trace)
        return 1

    digest, md = build_run_digest(run_dir, manifest, trace, a.thread, a.ue_root)
    os.makedirs(a.out, exist_ok=True)
    json_path = os.path.join(a.out, "perf_digest.json")
    md_path = os.path.join(a.out, "perf_digest.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=1)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print("[ok] %s" % md_path)
    print("  conclusion: %s（%s）" % (digest["conclusion"]["confidence"], digest["capability"]))
    print("  %s" % digest["conclusion"]["text"])
    for w in digest["warnings"]:
        print("  warning: %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
