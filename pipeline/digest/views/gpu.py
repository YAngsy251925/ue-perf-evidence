#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gpu.py — GPU 视图 v0（M4.0 / M4.3）

回答 PLAN §5.2 的两个问题：哪个 pass 贵（A 路 GPU1 pass 级 Top-N）；是否 GPU-bound
（B2 队列 busy + resolved fence → GPU 慢 / CPU 等 GPU / GPU 非瓶颈 / 难分）。

数据源：
  A 路：UnrealInsights 窗口导出的 GPU1 TimerStatistics CSV（与 cpu 视图同列，单位秒）
  B2：  b2 gpu 的 JSON（queue_busy + fences；fence 聚合是本轮给宿主 EmitGpu 补的）
  帧基准：manifest.frame_semantics.game 的 window_frames 与 interval p50

判定规则（确定性；R14：不假定 GPU 帧与游戏帧一一对应）：
  fill(队列) = busy_s / 窗口帧数 / 游戏帧间隔 p50
  GT 忙碌比   = GameThread 非等待类自耗时合计 / 窗口时长（A 路 Top-400 截断，是下界）
  max_fill >= 0.8 且 GT < 0.95   → GPU 慢（fill 最大的队列），clear
  max_fill >= 0.8 且 GT >= 0.95  → GT 与 GPU 都接近满载，难分，marginal（bound 判定 M5.1 再分）
  max_fill <= 0.5 且 fence 显著  → CPU 等 GPU（GPU 队列有空转但跨队列等待大），moderate
  max_fill <= 0.5 且 fence 不显著 → GPU 非瓶颈（瓶颈看 CPU 视图），clear
  其余                           → 难分，marginal
  fence 显著 = 每帧跨队列 fence 等待 >= 帧间隔的 25%
B2 数据不可用时只列 pass 级 Top-N 与帧映射，结论置信度 none。

R14 帧映射：GPU1 的 "Frame" 计时器次数 / 窗口内游戏帧数。真实 RHI 下 Graphics 与
Compute 队列各记一个 Frame（probe_D 实测 1472 / 737 ≈ 2.0），比值接近 1 说明只有单队列
在记帧、明显大于 2 需要检查重复登记；"Frame" 是结构行，不进 pass Top-N。
"""
import argparse
import json
import os
import sys

from pipeline.digest import run_digest
from pipeline.digest.views import cpu
from pipeline.extract import b2

SCHEMA = "perf-digest/1"
KIND = "gpu"
TOP_N = 15
GPU_THREAD = "GPU1"
GT_EXPORT_TOP = 400          # GT 忙碌比的窗口导出条数（下界）
FRAME_TIMER = "Frame"
PRESENT_TIMER = "EndDrawingViewport"

FILL_CLEAR = 0.80
FILL_IDLE = 0.50
GT_SATURATED = 0.95
FENCE_RATIO_SIGNIFICANT = 0.25


def frame_mapping(rows, game_window_frames):
    """R14：GPU1 的 Frame 计时器 / 窗口内游戏帧数。rows 已按名合并。"""
    frame = next((r for r in rows if r["name"] == FRAME_TIMER), None)
    presents = next((r for r in rows if r["name"] == PRESENT_TIMER), None)
    calls = frame["count"] if frame else 0
    ratio = round(calls / game_window_frames, 3) if (game_window_frames and calls) else None
    if calls == 0:
        note = "GPU1 没有 %s 计时器：trace 可能没带 gpu 通道，或该 RHI 不写 GPU 帧范围" % FRAME_TIMER
    elif ratio is not None and 1.8 <= ratio <= 2.2:
        note = "接近 2，符合预期（Graphics 与 Compute 队列各记一个 Frame）"
    elif ratio is not None and 0.8 <= ratio <= 1.2:
        note = "接近 1：只有单队列在记 Frame"
    else:
        note = "与 1 或 2 都不接近，检查是否有重复登记或帧轨道错位"
    return {"frame_timer_calls": calls,
            "game_window_frames": game_window_frames,
            "ratio": ratio,
            "present_calls": presents["count"] if presents else 0,
            "note": note}


def queue_fills(b2_gpu, game_window_frames, interval_p50_ms):
    """每队列 fill = busy_s / 帧数 / 帧间隔 p50。返回按 fill 降序的行（busy 为 0 的队列也列出）。"""
    span_frames = game_window_frames or 0
    rows = []
    for q in b2_gpu.get("queue_busy") or []:
        busy_s = float(q.get("busy_s") or 0.0)
        per_frame_ms = 1000.0 * busy_s / span_frames if span_frames else None
        fill = (per_frame_ms / interval_p50_ms) if (per_frame_ms is not None and interval_p50_ms) else None
        rows.append({"name": q.get("name"), "timeline": q.get("timeline"), "busy_s": round(busy_s, 3),
                     "busy_per_frame_ms": round(per_frame_ms, 3) if per_frame_ms is not None else None,
                     "fill": round(fill, 3) if fill is not None else None})
    rows.sort(key=lambda r: -(r["fill"] or 0.0))
    return rows


def fence_summary(b2_gpu, game_window_frames, interval_p50_ms):
    """跨队列 fence 聚合 → 每帧等待与显著性。b2 无 fences 字段（旧宿主）时 available=False。"""
    fences = b2_gpu.get("fences")
    if fences is None:
        return {"available": False,
                "note": "b2 gpu JSON 没有 fences 字段（旧版宿主导出），重新跑 perfctl b2 gpu 可得"}
    pairs = fences.get("pairs") or []
    sum_stall_s = sum(float(p.get("sum_stall_s") or 0.0) for p in pairs)
    per_frame_ms = 1000.0 * sum_stall_s / game_window_frames if game_window_frames else None
    ratio = (per_frame_ms / interval_p50_ms) if (per_frame_ms is not None and interval_p50_ms) else None
    return {"available": True,
            "events_in_window": fences.get("events_in_window"),
            "pair_count": fences.get("pair_count"),
            "pairs": pairs,
            "worst": (fences.get("worst") or [])[:5],
            "stall_per_frame_ms": round(per_frame_ms, 3) if per_frame_ms is not None else None,
            "stall_ratio": round(ratio, 3) if ratio is not None else None,
            "significant": bool(ratio is not None and ratio >= FENCE_RATIO_SIGNIFICANT)}


def verdict(fills, fence, gt_busy_ratio, interval_p50_ms):
    """确定性判定（阈值见模块 docstring）。返回 (text, confidence, verdict_kind)。

    M5.1 修正（2026-11-04）：判定信号从 max-fill 改为 **Graphics 队列**（present 路径）的 fill——
    实测 Compute 队列在 A1/B1 两组 trace 都整窗饱和（≈interval 本身），max-fill 会把任何
    run 误判成 GPU 慢；饱和的非 Graphics 队列降级为上下文注记。
    """
    with_fill = [f for f in fills if f["fill"] is not None]
    graphics = next((f for f in with_fill if (f["name"] or "").lower() == "graphics"), None)
    if graphics is None:
        graphics = next((f for f in with_fill if (f["name"] or "").lower() != "copy" and f["fill"] > 0),
                        with_fill[0] if with_fill else None)
    top = graphics
    max_fill = top["fill"] if top else 0.0
    top_name = top["name"] if top else "?"
    saturated = [f["name"] for f in with_fill
                 if top is None or (f is not top and f["fill"] >= 0.95)]
    sat_txt = ("；注意 %s 队列整窗饱和（持续后台型，不是 GPU bound 证据）" % "、".join(saturated)) if saturated else ""
    stall_ms = fence.get("stall_per_frame_ms") if fence.get("available") else None
    stall_txt = ("；跨队列 fence 等待平均 %.1f ms/帧（占帧间隔 %.0f%%）" %
                 (stall_ms, 100.0 * fence["stall_ratio"])) if stall_ms is not None else ""
    if not top or interval_p50_ms is None:
        return "B2 队列数据或帧基准不足，无法判定", "none", "insufficient_data"
    gt_txt = "" if gt_busy_ratio is None else "（GT 忙碌比下界 %.2f）" % gt_busy_ratio
    if max_fill >= FILL_CLEAR:
        if gt_busy_ratio is not None and gt_busy_ratio >= GT_SATURATED:
            return ("%s 队列每帧忙 %.1f ms（fill %.2f），但 GT 也接近满载%s：两边并行饱和，"
                    "瓶颈归属需 bound 判定（M5.1）%s%s" % (top_name, top["busy_per_frame_ms"], max_fill, gt_txt, stall_txt, sat_txt)
                    ), "marginal", "both_saturated"
        return ("GPU 慢：%s 队列每帧忙 %.1f ms，占帧间隔 %.0f%%%s，GPU 侧优先下钻该队列的 pass%s%s"
                % (top_name, top["busy_per_frame_ms"], 100.0 * max_fill, gt_txt, stall_txt, sat_txt)
                ), "clear", "gpu_slow"
    if max_fill <= FILL_IDLE:
        if fence.get("significant"):
            return ("GPU 队列有空转（%s fill %.2f）但跨队列 fence 等待大（平均 %.1f ms/帧）："
                    "CPU 等 GPU 的信号/节流，看 fence worst 的时间点回溯%s%s"
                    % (top_name, max_fill, stall_ms, gt_txt, sat_txt)), "moderate", "cpu_wait_gpu"
        return ("GPU 非瓶颈：%s 队列每帧只忙 %.1f ms（fill %.2f），帧间隔 %.1f ms 大部分不在 GPU%s%s；"
                "瓶颈看 CPU 视图" % (top_name, top["busy_per_frame_ms"], max_fill, interval_p50_ms, gt_txt, sat_txt)
                ), "clear", "gpu_idle"
    return ("难分：%s 队列 fill %.2f 在 [%s, %s] 判定带之间%s%s%s"
            % (top_name, max_fill, FILL_IDLE, FILL_CLEAR, gt_txt, stall_txt, sat_txt)), "marginal", "unclear"


def build_from_rows(rows, caliber, window_name, warnings, game_window_frames, interval_p50_ms,
                    b2_gpu=None, gt_busy_ratio=None):
    """纯函数：GPU1 窗口统计行（已合并）→ gpu digest 核心。b2_gpu 为 B2 的 gpu JSON dict 或 None。"""
    structural = [r for r in rows if r["name"] == FRAME_TIMER]
    others0 = [r for r in rows if r["name"] != FRAME_TIMER]
    total = sum(r["excl"] for r in others0) or 1e-9  # Frame 是结构行，不进 pass 口径
    others, wait, wait_total = run_digest.split_wait(others0)
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

    mapping = frame_mapping(structural or rows, game_window_frames)

    fills, fence, v_text, v_conf, v_kind = None, None, None, "none", "no_b2"
    if b2_gpu is not None:
        fills = queue_fills(b2_gpu, game_window_frames, interval_p50_ms)
        fence = fence_summary(b2_gpu, game_window_frames, interval_p50_ms)
        v_text, v_conf, v_kind = verdict(fills, fence, gt_busy_ratio, interval_p50_ms)
    else:
        v_text = ("B2 gpu 数据不可用（用 --b2 生成）；只列 pass 级 Top-N 与 R14 帧映射。"
                  "Top pass：%s" % (top[0]["name"] if top else "（无数据）"))
    top_name = top[0]["name"] if top else None
    refs = ["timer:%s@%s" % (r["name"], window_name) for r in top[:5]]
    if fence and fence.get("available") and fence.get("worst"):
        worst = fence["worst"][0]
        refs.append("gpu_fence_stall:%.3fs(%.1fms) q%d->q%d" % (
            worst.get("wait_s", 0.0), worst.get("stall_ms", 0.0),
            worst.get("signal_queue", 0), worst.get("wait_queue", 0)))
    digest = {
        "schema": SCHEMA, "kind": KIND, "capability": "measurable",
        "conclusion": {"text": v_text, "confidence": v_conf, "verdict": v_kind},
        "caliber": caliber,
        "stats": {
            "window": window_name,
            "thread": GPU_THREAD,
            "source_csv": caliber.get("source_csv"),
            "timers": len(rows),
            "total_self_s": round(total, 3),
            "top_by_self_time": [row_view(r) for r in top],
            "wait_timers": [row_view(r) for r in wait],
            "wait_total": wait_total,
            "top_by_inclusive": [{"name": r["name"], "incl_s": round(r["incl"], 3),
                                  "incl_pct": round(100.0 * r["incl"] / total, 2)} for r in by_incl[:TOP_N]],
            "omitted": {"timers": max(len(rows) - len(top) - len(wait), 0), "self_pct": round(omitted_pct, 2)},
            "frame_mapping": mapping,
            "b2": None if b2_gpu is None else {
                "queues": fills,
                "gpu_busy_s": b2_gpu.get("gpu_busy_s"),
                "gpu_busy_pct_window": b2_gpu.get("gpu_busy_pct_window"),
                "fences": fence,
                "gt_busy_ratio": None if gt_busy_ratio is None else round(gt_busy_ratio, 4),
                "gt_busy_lower_bound": True,
            },
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
              "- GPU 线程: %s（pass 级统计，含全部 GPU 队列）" % (st["thread"] if st else GPU_THREAD),
              ""]
    if st is None:
        lines += ["## 警告"]
        lines += ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"
    lines += ["## Top self-time（GPU pass 级，%s 结构行已排除，等待类已单列）" % FRAME_TIMER,
              "| # | timer | self_s | self% | calls | avg_ms | max_ms | spike |",
              "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(st["top_by_self_time"], 1):
        lines.append("| %d | %s | %s | %s%% | %s | %s | %s | %s |" % (
            i, r["name"], r["self_s"], r["self_pct"], r["count"], r["avg_ms"], r["max_ms"], r["spike_ratio"]))
    lines += ["", "## R14 帧映射",
              "- GPU1 \"%s\" 计时器 %s 次 / 窗口游戏帧 %s = %s" % (
                  FRAME_TIMER, st["frame_mapping"]["frame_timer_calls"],
                  st["frame_mapping"]["game_window_frames"], st["frame_mapping"]["ratio"]),
              "- %s" % st["frame_mapping"]["note"],
              "- %s（present）调用 %s 次" % (PRESENT_TIMER, st["frame_mapping"]["present_calls"]),
              ""]
    if st["wait_timers"]:
        lines += ["## 等待类（R5：症状，不进瓶颈候选）",
                  "wait_total: self_s=%s, self_pct=%s%%" % (st["wait_total"]["self_s"], st["wait_total"]["self_pct"]),
                  ""]
        lines += ["| timer | self_s | self% | calls |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s%% | %s |" % (r["name"], r["self_s"], r["self_pct"], r["count"])
                  for r in st["wait_timers"]]
        lines.append("")
    b2s = st.get("b2")
    if b2s:
        lines += ["## B2 队列与 fence（R14：fill = busy/帧数/帧间隔 p50）",
                  "| 队列 | busy_s | busy/帧 ms | fill |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s |" % (q["name"], q["busy_s"], q["busy_per_frame_ms"], q["fill"])
                  for q in b2s["queues"]]
        lines.append("")
        fence = b2s["fences"]
        if fence.get("available"):
            lines += ["fence: 窗口内 %s 事件 / %s 对，每帧等待 %s ms（GT 忙碌比下界 %s，Top-%d 截断）" % (
                fence.get("events_in_window"), fence.get("pair_count"),
                fence.get("stall_per_frame_ms"), b2s.get("gt_busy_ratio"), GT_EXPORT_TOP)]
            for p in fence.get("pairs") or []:
                lines.append("- q%s → q%s：%s 次，合计 %.3f s，最大 %.1f ms" % (
                    p.get("signal_queue"), p.get("wait_queue"), p.get("count"),
                    p.get("sum_stall_s", 0.0), p.get("max_stall_ms", 0.0)))
            lines.append("")
        else:
            lines += ["- %s" % fence.get("note", ""), ""]
    lines += ["## 省略", "其余 %s 个 timer 合计 %s%%。" % (st["omitted"]["timers"], st["omitted"]["self_pct"]), ""]
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


def render_blocked(manifest, caliber, warnings, why):
    digest = {
        "schema": SCHEMA, "kind": KIND, "capability": "measurable",
        "conclusion": {"text": "没有可用的统计窗口，无法给出 GPU 结论", "confidence": "none",
                       "verdict": "no_window"},
        "caliber": caliber, "stats": None, "refs": [], "warnings": warnings,
    }
    return digest, render_md(digest)


def load_b2_gpu(run_dir, manifest, trace, ue_root=None, want_run=False, limit=40):
    """取 B2 gpu JSON：优先 run_dir/b2/b2_gpu.json 缓存（窗口一致才用），--b2 时缓存未命中就真跑一次。
    返回 (dict 或 None, warnings 增量)。"""
    warnings = []
    win_name = manifest["window_basis"].get("stats_window")
    window = manifest["windows"].get(win_name) if win_name else None
    if not window:
        return None, warnings
    cache_dir = os.path.join(run_dir, "b2")
    cache = os.path.join(cache_dir, "b2_gpu.json")
    stamp_path = os.path.join(cache_dir, "b2_gpu_inputs.json")
    stamp = json.dumps({"trace": os.path.abspath(trace), "start": repr(window[0]), "end": repr(window[1]),
                        "limit": limit}, sort_keys=True)
    if os.path.isfile(cache) and os.path.isfile(stamp_path):
        with open(stamp_path, encoding="utf-8") as f:
            cached_stamp = f.read()
        if cached_stamp == stamp:  # 缓存命中
            with open(cache, encoding="utf-8") as f:
                return json.load(f), warnings
    if not want_run:
        return None, warnings
    os.makedirs(cache_dir, exist_ok=True)
    ok, text = b2.run_mode("gpu", trace, cache, limit, ue_root, start=window[0], end=window[1])
    if not ok:
        warnings.append("b2 gpu 运行失败（见 %s）" % os.path.splitext(cache)[0] + ".log")
        return None, warnings
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(stamp)
    with open(cache, encoding="utf-8") as f:
        return json.load(f), warnings


def gt_busy_from_export(run_dir, manifest, trace, ue_root=None):
    """GT 忙碌比（下界）：GameThread 窗口导出 Top-N 的非等待自耗时合计 / 窗口时长。"""
    win_name = manifest["window_basis"].get("stats_window")
    window = manifest["windows"].get(win_name) if win_name else None
    if not window:
        return None
    span = window[1] - window[0]
    if span <= 0:
        return None
    status, rows, _caliber, _warnings = run_digest.prepare_rows(
        run_dir, manifest, trace, "GameThread", ue_root, GT_EXPORT_TOP, "_gt%d" % GT_EXPORT_TOP)
    if status != "ok":
        return None
    _others, _wait, wait_total = run_digest.split_wait(rows)
    busy = sum(r["excl"] for r in rows) - wait_total["self_s"]
    return max(busy / span, 0.0)


def build_run_digest(run_dir, manifest, trace, ue_root=None, with_b2=False, b2_limit=40):
    """run 级 GPU digest 入口。measurable=false 时不导出；B2 数据按需取（缓存优先）。"""
    if not manifest.get("measurable", False):
        return not_measurable_digest(manifest)
    win_name = manifest["window_basis"].get("stats_window")
    caliber = run_digest.caliber_from_manifest(manifest, None)
    warnings = list(manifest.get("warnings", []))
    window = manifest["windows"].get(win_name) if win_name else None
    if not window:
        if not win_name:
            warnings.append("no stats window (window_basis.stats_window is null)")
        else:
            warnings.append("stats window %s is null in the manifest" % win_name)
        return render_blocked(manifest, caliber, warnings, "no stats window")

    status, rows, caliber, warnings = run_digest.prepare_rows(
        run_dir, manifest, trace, GPU_THREAD, ue_root, 50, "_%s" % GPU_THREAD)
    if status != "ok":
        return render_blocked(manifest, caliber, warnings, "export failed")

    fs_game = (manifest.get("frame_semantics") or {}).get("game") or {}
    game_window_frames = fs_game.get("window_frames")
    interval_p50 = ((fs_game.get("stats") or {}).get("interval_ms") or {}).get("p50")

    b2_gpu = None
    if with_b2:
        b2_gpu, extra = load_b2_gpu(run_dir, manifest, trace, ue_root, want_run=True, limit=b2_limit)
        warnings += extra
    else:
        b2_gpu, extra = load_b2_gpu(run_dir, manifest, trace, ue_root, want_run=False, limit=b2_limit)
        warnings += extra
    gt_busy = None
    if b2_gpu is not None:
        gt_busy = gt_busy_from_export(run_dir, manifest, trace, ue_root)
    return build_from_rows(rows, caliber, win_name, warnings, game_window_frames, interval_p50,
                           b2_gpu=b2_gpu, gt_busy_ratio=gt_busy)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="run 级 GPU digest v0（M4.0/M4.3）")
    ap.add_argument("run_dir", help="run 目录（含 manifest.json）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--trace", default=None, help="trace 文件（默认 manifest.trace.file，再退 <run_dir>/trace.utrace）")
    ap.add_argument("--manifest", default=None, help="manifest 路径（默认 <run_dir>/manifest.json）")
    ap.add_argument("--ue-root", default=None, help="窗口导出使用的 UE 根目录")
    ap.add_argument("--b2", action="store_true", help="缓存未命中时真跑一次 b2 gpu（约 60 秒）")
    ap.add_argument("--b2-limit", type=int, default=40, help="b2 gpu timers 列表条数")
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

    digest, md = build_run_digest(run_dir, manifest, trace, a.ue_root, with_b2=a.b2, b2_limit=a.b2_limit)
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
