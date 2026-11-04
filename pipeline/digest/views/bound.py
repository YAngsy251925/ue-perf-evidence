#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bound.py — bound 判定（M5.1，借鉴 BoundHound 的方法，自写实现；BoundHound 为 MIT，未引用其代码）

回答一个问题：这一帧 24.6ms 花在哪条线上了？GT / RT / RHI / GPU 四条线各算每帧忙碌时间，
取最大者为瓶颈候选；相差 10% 以内标「难分」；给了目标帧率预算再按预算判定（超预算 = 真瓶颈；
帧率被钉在预算上 = 节流不是慢；都远低于预算 = 瓶颈不在这四条线）。

口径（PLAN 5.1）：CPU 线忙碌 = 帧时长 − 该线程的等待类 scope（wait/stall/idle，R5）；
  wait_per_frame = 该线程窗口导出里等待类自耗时合计 / 窗口帧数；busy = interval_p50 − wait_per_frame。
  同时报 self_per_frame =（非等待自耗时合计）/ 帧数 作佐证：busy 与 self 的差就是 scope 之间的空隙。
GPU 线没有等待类概念：busy = 最忙 GPU 队列的 busy/帧（B2 gpu JSON，M4.0 的 queue_fills）；
  没有 B2 数据时该线记 unavailable（A 路 GPU1 统计是两队列聚合，会超 100%，不能当单线 busy）。
帧时间按 R15 取 interval p50；RT/RHI 帧与游戏帧不对齐，统一用每帧平均归一。

置信度：clear = 最大线领先次大 >10% 且四线数据齐全；moderate = 缺 RHI/GPU 线或领先 10–20%；
marginal = 难分组或更多线缺失；none = 没有 interval 或一线都没有。R11：not_measurable 的 run
不输出任何数字。与 fence 的关系随结论一并给出（验收：与 B2 GPU fence 的结论不矛盾）。
"""
import argparse
import json
import os
import sys

from pipeline.digest import run_digest
from pipeline.digest.views import gpu as gpu_view

SCHEMA = "perf-digest/1"
KIND = "bound"
TOP_N = 8

TIE_BAND = 0.10           # 相差 10% 以内标「难分」（PLAN 5.1）
CLEAR_GAP = 0.20          # 领先 20% 以上才算 clear（10–20% moderate）
CAPPED_RATIO = 0.95       # interval ≥ 预算的 95% → 帧被节流
UNDER_BUDGET_RATIO = 0.80 # 都低于预算 80% 且 interval 也低于 → 瓶颈不在这四线

CPU_LINES = (
    ("GT", "GameThread"),
    ("RT", "RenderThread 0"),
    ("RHI", "RHIThread"),
)


def thread_line(label, rows, window_frames, interval_mean_ms):
    """单条 CPU 线的 busy 口径（PLAN 5.1）。rows 已按名合并（prepare_rows 产物）。

    busy = 帧间隔均值 − 等待类/帧。分母必须用**均值**帧间隔（span/帧数）：wait/self 都是
    按窗口帧数均摊的均值量，与 p50 相除在有 hitch 的 run 上会自相矛盾（B1 实测：GT self
    43.3ms > busy 37.8ms，均值 48.1 vs p50 39.3）。节流/预算判定才用 p50（R15 节拍）。
    """
    if not rows or not window_frames or not interval_mean_ms:
        return {"line": label, "available": False}
    _others, _wait, wait_total = run_digest.split_wait(rows)
    wait_s = wait_total["self_s"]
    self_s = sum(r["excl"] for r in rows) - wait_s
    wait_pf = 1000.0 * wait_s / window_frames
    busy_pf = max(interval_mean_ms - wait_pf, 0.0)
    self_pf = 1000.0 * self_s / window_frames
    return {"line": label, "available": True,
            "wait_s": round(wait_s, 3),
            "wait_per_frame_ms": round(wait_pf, 3),
            "busy_per_frame_ms": round(busy_pf, 3),
            "occupancy": round(busy_pf / interval_mean_ms, 3),
            "self_per_frame_ms": round(self_pf, 3),
            "gap_note": ("busy 与 self 的差是 scope 之间的空隙（%.1f ms/帧）" % (busy_pf - self_pf))
                        if busy_pf - self_pf > 0.05 else None}


def gpu_line(b2_gpu, window_frames, interval_mean_ms):
    """GPU 线：**Graphics 队列**（present 路径）的 busy/帧。

    不用 max-queue：实测（2026-11-04，A1/B1）Compute 队列在两组 trace 里都整窗饱和
    （32.1s busy / 32.2s span ≈ 99.9%）——持续后台队列的 busy/帧 ≈ interval 本身
    （B1 上 122% 是事件跨窗口边界的膨胀），会把任何 run 都误判成 GPU bound。
    饱和的非 Graphics 队列降级为上下文注记（并行关键路径的线索，配合 fence 看）。
    """
    if b2_gpu is None or not window_frames or not interval_mean_ms:
        return {"line": "GPU", "available": False,
                "note": None if b2_gpu is None else "B2 gpu JSON 没有队列数据"}
    fills = [f for f in gpu_view.queue_fills(b2_gpu, window_frames, interval_mean_ms)
             if f["fill"] is not None]
    if not fills:
        return {"line": "GPU", "available": False, "note": "所有队列 busy 为 0"}
    graphics = next((f for f in fills if (f["name"] or "").lower() == "graphics"), None)
    if graphics is None:
        graphics = next((f for f in fills if (f["name"] or "").lower() != "copy" and f["fill"] > 0),
                        fills[0])
    saturated = [f["name"] for f in fills
                 if f is not graphics and f["fill"] is not None and f["fill"] >= 0.95]
    note = None
    if saturated:
        note = "%s 队列整窗饱和（持续后台型，不作为 GPU bound 信号；配合 fence 看并行关键路径）" % "、".join(saturated)
    return {"line": "GPU", "available": True,
            "queue": graphics["name"], "timeline": graphics["timeline"],
            "busy_per_frame_ms": graphics["busy_per_frame_ms"],
            "occupancy": graphics["fill"],
            "queues": fills, "note": note}


def verdict(lines, interval_p50_ms, budget_ms=None, fence=None):
    """确定性判定。lines 含 available 的四线（GT/RT/RHI/GPU）。返回 (text, confidence, kind, detail)。"""
    avail = [l for l in lines if l.get("available")]
    if not avail or not interval_p50_ms:
        return "数据不足：没有 interval 或任何一条线的统计，无法判定", "none", "insufficient_data", {}
    avail = sorted(avail, key=lambda l: -l["busy_per_frame_ms"])
    top = avail[0]
    group = [l for l in avail if top["busy_per_frame_ms"] - l["busy_per_frame_ms"]
             <= TIE_BAND * top["busy_per_frame_ms"]]
    gap = (top["busy_per_frame_ms"] - avail[1]["busy_per_frame_ms"]) / top["busy_per_frame_ms"] if len(avail) > 1 else 1.0
    missing = [l["line"] for l in lines if not l.get("available")]
    fence_txt = ""
    if fence and fence.get("available") and fence.get("stall_per_frame_ms") is not None:
        if any(l["line"] == "GPU" for l in group):
            fence_note = "，与「GPU 在关键路径」一致"
        else:
            fence_note = "，GPU 队列内部有跨队列等待（并行关键路径在跑）"
        fence_txt = ("；fence 每帧等待 %.1f ms（占帧间隔 %.0f%%）%s" %
                     (fence["stall_per_frame_ms"], 100.0 * fence["stall_ratio"], fence_note))
    detail = {"top": top["line"], "group": [l["line"] for l in group],
              "gap_to_second": round(gap, 3), "missing": missing}

    if budget_ms:
        if interval_p50_ms >= CAPPED_RATIO * budget_ms and top["busy_per_frame_ms"] <= budget_ms:
            text = ("帧时间 %.1f ms 被目标帧率预算 %.1f ms 钉住（节流，不是慢）：最忙的 %s 每帧忙 %.1f ms，"
                    "在预算内%s" % (interval_p50_ms, budget_ms, top["line"], top["busy_per_frame_ms"], fence_txt))
            conf = "marginal" if len(group) > 1 else "moderate"
            return text, conf, "frame_rate_capped", detail
        if top["busy_per_frame_ms"] > budget_ms:
            who = "、".join("%s(%.1fms)" % (l["line"], l["busy_per_frame_ms"]) for l in group)
            text = ("超预算：预算 %.1f ms，%s 每帧忙碌超过预算，瓶颈在 %s%s"
                    % (budget_ms, who, top["line"], fence_txt))
            conf = "marginal" if len(group) > 1 else "clear"
            return text, conf, "over_budget", detail
        if interval_p50_ms < budget_ms and top["busy_per_frame_ms"] < UNDER_BUDGET_RATIO * budget_ms:
            text = ("四条线都远低于预算（最忙 %s %.1f ms < 预算 80%%）且帧时间 %.1f ms < 预算："
                    "瓶颈不在这四条线上（看 hitch 视图与 dispatch/调度侧）%s"
                    % (top["line"], top["busy_per_frame_ms"], interval_p50_ms, fence_txt))
            return text, "moderate", "under_budget", detail
    if len(group) > 1:
        names = "、".join("%s(%.1fms)" % (l["line"], l["busy_per_frame_ms"]) for l in group)
        text = ("难分：%s 每帧忙碌相差在 10%% 以内（帧间隔 %.1f ms），需要 M5.1 的每帧证据或 "
                "5.1b 采样器校准进一步拆分%s" % (names, interval_p50_ms, fence_txt))
        return text, "marginal", "tie", detail
    missing_txt = "；缺失线：%s" % "、".join(missing) if missing else ""
    text = ("瓶颈在 %s：每帧忙 %.1f ms（占帧间隔 %.0f%%），领先次名 %.0f%%%s%s"
            % (top["line"], top["busy_per_frame_ms"], 100.0 * top["occupancy"], 100.0 * gap,
               missing_txt, fence_txt))
    conf = "moderate" if (missing or gap < CLEAR_GAP) else "clear"
    return text, conf, "single", detail


def build_from_data(lines, interval_p50_ms, caliber, window_name, warnings,
                    budget_ms=None, fence=None, frame_mapping=None):
    """纯函数：四线 busy 行 → bound digest。"""
    text, conf, kind, detail = verdict(lines, interval_p50_ms, budget_ms, fence)
    rows = [dict(l) for l in lines]
    for r in rows:
        r.setdefault("available", False)
    refs = []
    for l in lines:
        if l.get("available"):
            refs.append("bound:%s busy=%.1fms/frame" % (l["line"], l["busy_per_frame_ms"]))
    if fence and fence.get("available") and fence.get("worst"):
        worst = fence["worst"][0]
        refs.append("gpu_fence_stall:%.3fs(%.1fms) q%d->q%d" % (
            worst.get("wait_s", 0.0), worst.get("stall_ms", 0.0),
            worst.get("signal_queue", 0), worst.get("wait_queue", 0)))
    digest = {
        "schema": SCHEMA, "kind": KIND, "capability": "measurable",
        "conclusion": {"text": text, "confidence": conf, "verdict": kind},
        "caliber": caliber,
        "stats": {
            "window": window_name,
            "interval_p50_ms": None if interval_p50_ms is None else round(interval_p50_ms, 4),
            "budget_ms": None if budget_ms is None else round(budget_ms, 3),
            "interval_note": "busy/occupancy 用均值帧间隔（span/帧数）；预算与节流判定用 p50（R15）",
            "lines": rows,
            "verdict_detail": detail,
            "fence": fence,
            "frame_mapping": frame_mapping,
        },
        "refs": refs,
        "warnings": warnings,
    }
    return digest, render_md(digest)


def render_md(digest):
    c = digest["caliber"]
    st = digest["stats"]
    lines = ["# Perf digest — %s — %s" % (digest["kind"], c["run_id"]), ""]
    lines += ["## 结论", digest["conclusion"]["text"] + "（置信度: %s）" % digest["conclusion"]["confidence"], ""]
    lines += ["## 口径",
              "- run_id: %s" % c["run_id"],
              "- profile: %s" % c["profile"],
              "- rhi: %s（state=%s, source=%s）" % (c["rhi"], c["rhi_state"], c["rhi_source"]),
              "- build_config: %s" % c["build_config"],
              "- 窗口: %s %s（成帧 %s）" % (c["stats_window"], run_digest.fmt_range(c["window"]),
                                           run_digest.fmt_range(c["frames_window"])),
              "- 口径: busy = 帧间隔均值 − 等待类 scope/帧（PLAN 5.1）；预算/节流判定用 p50（R15 节拍）",
              "- 帧间隔 p50: %s" % (("（无统计）" if st is None else
                                     "%s ms%s" % (st["interval_p50_ms"],
                                                  "" if not st.get("budget_ms") else "，预算 %s ms" % st["budget_ms"]))),
              ""]
    if st is None:
        lines += ["## 警告"]
        lines += ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"
    lines += ["## 四线忙碌（每帧平均，busy = interval − wait）",
              "| 线 | busy/帧 ms | 占帧间隔 | wait/帧 ms | self/帧 ms | 备注 |",
              "|---|---|---|---|---|---|"]
    for l in st["lines"]:
        if l.get("available"):
            if l.get("note"):
                note = l["note"]
            elif l.get("queue"):
                note = "队列 %s" % l["queue"]
            else:
                note = l.get("gap_note") or ""
            lines.append("| %s | %s | %s%% | %s | %s | %s |" % (
                l["line"], l["busy_per_frame_ms"], round(100.0 * l["occupancy"], 1),
                l.get("wait_per_frame_ms", "—"), l.get("self_per_frame_ms", "—"), note))
        else:
            lines.append("| %s | （缺失） | | | | %s |" % (
                l["line"], l.get("note") or "无该线程统计"))
    lines.append("")
    fence = st.get("fence")
    if fence and fence.get("available"):
        lines += ["fence（M4.0）：窗口内 %s 事件 / %s 对，每帧等待 %s ms" % (
            fence.get("events_in_window"), fence.get("pair_count"), fence.get("stall_per_frame_ms"))]
        for p in fence.get("pairs") or []:
            lines.append("- q%s → q%s：%s 次，合计 %.3f s，最大 %.1f ms" % (
                p.get("signal_queue"), p.get("wait_queue"), p.get("count"),
                p.get("sum_stall_s", 0.0), p.get("max_stall_ms", 0.0)))
        lines.append("")
    fm = st.get("frame_mapping")
    if fm:
        lines += ["R14 帧映射：GPU1 Frame 计时器 %s 次 / 窗口游戏帧 %s = %s（%s）" % (
            fm.get("frame_timer_calls"), fm.get("game_window_frames"), fm.get("ratio"), fm.get("note")), ""]
    if digest["refs"]:
        lines += ["## 下钻"] + ["- %s" % r for r in digest["refs"]] + [""]
    lines += ["## 警告"]
    lines += ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
    return "\n".join(lines) + "\n"


def not_measurable_digest(manifest):
    reason = manifest.get("measurable_reason", "unknown reason")
    digest = {
        "schema": SCHEMA, "kind": KIND, "capability": "not_measurable",
        "conclusion": {"text": "capability: not_measurable —— %s；不输出任何性能结论" % reason,
                       "confidence": "none", "verdict": "not_measurable"},
        "caliber": run_digest.caliber_from_manifest(manifest, None),
        "stats": None, "refs": [], "warnings": list(manifest.get("warnings", [])),
    }
    return digest, render_md(digest)


def build_run_digest(run_dir, manifest, trace, ue_root=None, with_b2=False, target_fps=None):
    """run 级 bound digest 入口（M5.1）。measurable=false 不导出；预算可选（--target-fps）。"""
    if not manifest.get("measurable", False):
        return not_measurable_digest(manifest)
    win_name = manifest["window_basis"].get("stats_window")
    caliber = run_digest.caliber_from_manifest(manifest, None)
    warnings = list(manifest.get("warnings", []))
    window = manifest["windows"].get(win_name) if win_name else None
    if not window:
        warnings.append("no stats window: bound 判定需要统计窗口")
        digest = {"schema": SCHEMA, "kind": KIND, "capability": "measurable",
                  "conclusion": {"text": "没有可用的统计窗口，无法判定 bound", "confidence": "none",
                                 "verdict": "no_window"},
                  "caliber": caliber, "stats": None, "refs": [], "warnings": warnings}
        return digest, render_md(digest)

    fs_game = (manifest.get("frame_semantics") or {}).get("game") or {}
    window_frames = fs_game.get("window_frames")
    interval_p50 = ((fs_game.get("stats") or {}).get("interval_ms") or {}).get("p50")
    # busy 口径用均值帧间隔（wait/self 都是按帧数均摊的均值量）；p50 留给预算/节流判定（R15）
    interval_mean = ((fs_game.get("stats") or {}).get("interval_ms") or {}).get("mean")
    if interval_mean is None and window and window_frames:
        interval_mean = 1000.0 * (window[1] - window[0]) / window_frames

    lines = []
    for label, thread in CPU_LINES:
        status, rows, _caliber, warnings = run_digest.prepare_rows(
            run_dir, manifest, trace, thread, ue_root, 200, "_bound_%s" % label)
        if status != "ok":
            lines.append({"line": label, "available": False, "note": "无 %s 统计窗口导出" % thread})
            continue
        lines.append(thread_line(label, rows, window_frames, interval_mean))

    b2_gpu = None
    fence = None
    if interval_mean is not None and window_frames:
        b2_gpu, extra = gpu_view.load_b2_gpu(run_dir, manifest, trace, ue_root, want_run=with_b2)
        warnings += extra
        if b2_gpu is not None:
            lines.append(gpu_line(b2_gpu, window_frames, interval_mean))
            fence = gpu_view.fence_summary(b2_gpu, window_frames, interval_mean)
    if not any(l["line"] == "GPU" for l in lines):
        lines.append({"line": "GPU", "available": False,
                      "note": "无 B2 gpu 数据（用 --b2 生成，缓存后不重跑）"})

    budget_ms = 1000.0 / target_fps if target_fps else None
    return build_from_data(lines, interval_p50, caliber, win_name, warnings,
                           budget_ms=budget_ms, fence=fence, frame_mapping=None)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="run 级 bound 判定（M5.1，PLAN 5.1 口径）")
    ap.add_argument("run_dir", help="run 目录（含 manifest.json）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--trace", default=None, help="trace 文件（默认 manifest.trace.file）")
    ap.add_argument("--manifest", default=None, help="manifest 路径（默认 <run_dir>/manifest.json）")
    ap.add_argument("--ue-root", default=None)
    ap.add_argument("--b2", action="store_true", help="GPU 线需要 B2 gpu JSON；缓存未命中时真跑一次（约 60 秒）")
    ap.add_argument("--target-fps", type=float, default=None, help="目标帧率预算（可选；不给则只做相对排序）")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    manifest_path = a.manifest or os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print("[error] manifest not found: %s（先运行 perfctl manifest <run_dir>）" % manifest_path)
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    trace = a.trace or run_digest.resolve(run_dir, manifest.get("trace", {}).get("file"), "trace.utrace")
    if not manifest.get("measurable", False):
        trace = None
    elif not os.path.isfile(trace):
        print("[error] trace not found: %s" % trace)
        return 1

    digest, md = build_run_digest(run_dir, manifest, trace, a.ue_root,
                                  with_b2=a.b2, target_fps=a.target_fps)
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
