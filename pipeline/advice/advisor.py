#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""advisor.py — 从证据生成**可证伪**的优化假设（不是背诵套路）

设计原则（对应 docs/ADVISOR.md）：
  1. **证据锚定**：每条假设必须引用具体数字（哪个 pass 占多少、哪条线饱和、fence 等多久），
     没数字的建议一律不输出——这样它就不会退化成"试试关掉 Lumen"。
  2. **杠杆来自本机引擎**：可调开关不写死在代码里，而是从 `catalog/cvars.json`
     （`tools/scan_cvars.py` 扫引擎源码生成）查——版本升级后重新扫一次，清单自然更新。
  3. **可证伪 + 可复验**：每条假设带"预期收益区间"和"验证命令"（我们自己的 R9 diff + CI 闸门），
     跑完把实测写进 `advice/ledger.json`。预测与实测差太远的规则会被降权（见 docs/ADVISOR.md §4）。
  4. **主动发现优先于清单**：检测器只看数据形态（份额、饱和、离散度、fence 耦合），
     不预设"这个引擎有哪些毛病"——新现象自然会产生新假设。

用法：
  perfctl advise <run_dir> --out DIR [--catalog catalog/cvars.json] [--budget-ms 16.67]
"""
import argparse
import io
import json
import os
import sys

# 检测器阈值（可调，写在一处便于回归）
SHARE_MIN = 0.03          # 单个效果占 GPU 工作 ≥3% 才值得提
GT_TOP_SHARE = 0.15       # GT 单个 timer ≥15% 视为热点
GT_UI_SHARE = 0.30        # GT 里 UI 相关合计 ≥30% 视为 UI 主导
FENCE_RATIO = 0.30        # fence 等待 ≥30% 帧间隔
SATURATED = 0.95          # busy ≥95% 帧间隔

UI_PATTERNS = ("slate", "winpumpmessages", "ui ", "umg", "widget")
# 效果名 → 目录里的前缀（用于查杠杆）
EFFECT_PREFIX = {
    "lumen": ("r.Lumen", "r.DynamicGlobalIllumination"),
    "temporalsuperresolution": ("r.TSR", "r.AntiAliasing", "r.ScreenPercentage"),
    "bloom": ("r.Bloom", "r.BloomQuality"),
    "nanite": ("r.Nanite",),
    "shadow": ("r.Shadow", "r.VSM"),
    "postprocess": ("r.PostProcessing", "r.Tonemap", "r.ColorGrading"),
    "translucen": ("r.Translucency",),
    "volumetric": ("r.VolumetricFog",),
}
QUALITY_HINTS = ("quality", "allow", "screenpercentage", "radius", "downsample", "steps",
                 "scale", "resolution", "asynccompute")


def load_catalog(path=None):
    if not path:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "catalog", "cvars.json")
    if not os.path.isfile(path):
        return {"cvars": [], "missing": True, "path": path}
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["missing"] = False
    data["path"] = path
    return data


def levers_for(catalog, prefixes, limit=4):
    """在目录里找与该子系统相关、且像"质量/开关"的 cvar（带 file:line 供核对）。"""
    rows = [c for c in catalog.get("cvars") or []
            if any(c["name"].startswith(p) for p in prefixes)]
    ranked = sorted(rows, key=lambda c: (
        0 if any(h in c["name"].lower() for h in QUALITY_HINTS) else 1,
        len(c["name"]), c["name"]))
    return [{"name": c["name"], "default": c["default"], "help": c.get("help"),
             "where": "%s:%s" % (os.path.basename(c["file"]), c["line"])}
            for c in ranked[:limit]]


def _pct(v):
    return None if v is None else round(100.0 * v, 1)


# ---------------------------------------------------------------- 检测器


def detect_gpu_effects(gpu, catalog, graphics_busy_ms):
    """GPU 侧：按效果份额找可砍的量。份额用 GPU timer 合计做分母（见 docs 里的口径说明）。"""
    stats = (gpu or {}).get("stats") or {}
    rows = stats.get("top_by_self_time") or []
    total = stats.get("total_self_s") or 0.0
    if not rows or not total:
        return []
    out = []
    for row in rows:
        name = str(row.get("name") or "")
        share = (row.get("self_pct") or 0.0) / 100.0
        if share < SHARE_MIN:
            continue
        key = next((k for k in EFFECT_PREFIX if k in name.lower()), None)
        if not key:
            continue
        ms_raw = (row.get("self_s") or 0.0) * 1000.0 / max(1, stats.get("frame_count") or 1)
        # 折算到真实 GPU 预算：份额 × 队列 busy（原始 timer 会因并行/父级聚合超过 busy）
        ms_effective = share * (graphics_busy_ms or 0.0)
        out.append({
            "id": "gpu_effect_share:%s" % name.split("(")[0].strip().lower().replace(" ", "_"),
            "kind": "reduce_effect_cost",
            "subject": name.split("(")[0].strip(),
            "evidence": {"share_of_gpu_work_pct": _pct(share),
                         "ms_effective": round(ms_effective, 2),
                         "graphics_busy_ms": graphics_busy_ms,
                         "source": "gpu.perf_digest.json top_by_self_time"},
            "expected_saving_ms": [round(ms_effective * 0.3, 2), round(ms_effective * 0.6, 2)],
            "levers": levers_for(catalog, EFFECT_PREFIX[key]),
            "verification": ("用同一 profile 采 A（现状）/ B（只改上面一个 cvar），每组 5 次："
                             "perfctl diff cpu -a A1..A5 -b B1..B5 --significance-only "
                             "--fail-on regression --json-line"),
            "invalidation": "若 R9 判定 within_noise（Δ 在噪声内），该假设证伪；"
                            "不要因为「看起来应该有效」就写进结论",
        })
    return out


def detect_cpu_hotspots(cpu, interval_p50_ms, catalog):
    """CPU 侧：单一热点 vs 分散，以及 UI 主导。两者给出的建议完全不同。"""
    stats = (cpu or {}).get("stats") or {}
    rows = stats.get("top_by_self_time") or []
    if not rows:
        return []
    total = sum(r.get("self_s") or 0.0 for r in rows) or 1.0
    out = []
    top = rows[0]
    top_share = (top.get("self_s") or 0.0) / total
    ui_share = sum((r.get("self_s") or 0.0) for r in rows
                   if any(p in str(r.get("name") or "").lower() for p in UI_PATTERNS)) / total

    if ui_share >= GT_UI_SHARE:
        out.append({
            "id": "cpu_ui_dominant",
            "kind": "reduce_ui_cost",
            "subject": "Slate / UMG 重绘",
            "evidence": {"ui_share_pct": _pct(ui_share), "top1": top.get("name"),
                         "top1_pct": _pct(top_share), "source": "cpu.perf_digest.json"},
            "expected_saving_ms": [round(0.1 * interval_p50_ms, 2), round(0.3 * interval_p50_ms, 2)],
            "levers": levers_for(catalog, ("Slate.", "UMG.", "r.UMG"), limit=4),
            "verification": "先查「是否在编辑器里 PIE」（editor Slate 不该算进游戏帧）；"
                            "再用 perfctl evidence --ref \"frame:N\" 看 UI 是否每帧都重绘",
            "invalidation": "若把 UI 关掉（空白关卡）后帧时间无变化 → UI 不是成本，假设证伪",
        })
    if top_share >= GT_TOP_SHARE:
        out.append({
            "id": "cpu_single_hotspot",
            "kind": "investigate_hotspot",
            "subject": str(top.get("name")),
            "evidence": {"top1_pct": _pct(top_share), "self_s": top.get("self_s")},
            "expected_saving_ms": [round(0.2 * interval_p50_ms * top_share, 2),
                                   round(0.5 * interval_p50_ms * top_share, 2)],
            "levers": [],
            "verification": 'perfctl b2 callers <trace> --out DIR --thread GameThread --timer "<顶部 timer>"',
            "invalidation": "若 callers 显示它的时间都在等锁/等任务（overlap），则不是热点，改看等待目标",
        })
    if top_share < GT_TOP_SHARE:
        out.append({
            "id": "cpu_dispersion",
            "kind": "investigate_structure",
            "subject": "无单一热点（自耗时分散）",
            "evidence": {"top1_pct": _pct(top_share), "names": [r.get("name") for r in rows[:5]]},
            "expected_saving_ms": None,
            "levers": [],
            "verification": "按前缀聚合（digest 的 prefix 表）+ evidence 逐次分布，"
                            "判断是「广度」还是「抖动」",
            "invalidation": "若加总后发现某前缀占 >40%，说明只是被拆成了多个名字，回到单热点路径",
        })
    return out


def detect_bound(bound, interval_p50_ms, catalog):
    stats = (bound or {}).get("stats") or {}
    lines = [l for l in (stats.get("lines") or []) if l.get("available")]
    fence = stats.get("fence") or {}
    out = []
    saturated = [l for l in lines if (l.get("occupancy") or 0.0) >= SATURATED]
    if len(saturated) >= 2:
        out.append({
            "id": "bound_saturated_multi",
            "kind": "use_self_ranking",
            "subject": "多条线饱和，busy 无分辨力",
            "evidence": {"lines": [{"line": l["line"], "busy_ms": l["busy_per_frame_ms"],
                                    "self_ms": l.get("self_per_frame_ms")} for l in saturated]},
            "expected_saving_ms": None,
            "levers": [],
            "verification": "看 self/帧 的排名（digest 已按自耗时打破并列），再用 evidence 逐次确认",
            "invalidation": "若换到非饱和的场景（例如降低负载/关掉后台 Compute）排名变化，说明是测量假象",
        })
    stall = fence.get("stall_per_frame_ms")
    if stall and interval_p50_ms and stall / interval_p50_ms >= FENCE_RATIO:
        out.append({
            "id": "fence_cross_queue",
            "kind": "reduce_queue_dependency",
            "subject": "队列间 fence 等待",
            "evidence": {"stall_per_frame_ms": stall,
                         "ratio_pct": _pct(stall / interval_p50_ms),
                         "worst": (fence.get("worst") or [{}])[:2]},
            "expected_saving_ms": [round(stall * 0.2, 2), round(stall * 0.5, 2)],
            "levers": levers_for(catalog, ("r.RDG", "r.AsyncCompute", "r.Lumen.AsyncCompute",
                                           "r.Nanite.AsyncCompute"), limit=4),
            "verification": "perfctl b2 gpu <trace> --out DIR 看 queue 间 signal/wait 配对；"
                            "关掉异步（r.RDG.AsyncCompute=0 等）做 A/B",
            "invalidation": "若关掉异步后帧时间不变或更慢 → 该 fence 不是瓶颈，假设证伪",
        })
    return out


def _find_digest(run_dir, kind, extra_roots=()):
    """定位某类 digest：run 目录内（auto/digest_<kind>/<kind>）或兄弟目录都试一遍。"""
    names = ["%s/perf_digest.json" % kind, "digest_%s/perf_digest.json" % kind,
             "auto/%s/perf_digest.json" % kind]
    roots = [run_dir] + list(extra_roots) + [os.path.dirname(run_dir)]
    for root in roots:
        for name in names:
            path = os.path.join(root, name)
            if os.path.isfile(path):
                return path
    return None


def detect_memory(memory, catalog, process_physical_mb=None):
    """内存侧：某个 STATGROUP 吃掉大部分物理内存 / 峰值远高于当值 / 清单被截断。"""
    if not memory or not (memory.get("group_totals_mb") or memory.get("stats_top")):
        return []
    out = []
    groups = memory.get("group_totals_mb") or {}
    total = sum(groups.values()) or 0.0
    top_name, top_mb = next(iter(groups.items()), (None, 0.0)) if groups else (None, 0.0)
    share = (top_mb / total) if total else 0.0
    if top_name and share >= 0.35:
        out.append({
            "id": "memory_group_dominant:%s" % top_name.lower(),
            "kind": "investigate_memory_group",
            "subject": "%s 占内存 %.0f%%" % (top_name, 100 * share),
            "evidence": {"group": top_name, "group_mb": round(top_mb, 1),
                         "share_of_groups_pct": round(100 * share, 1),
                         "process_physical_mb": process_physical_mb,
                         "source": "memory.json group_totals_mb"},
            "expected_saving_ms": None,
            "levers": levers_for(catalog, ("r.Streaming", "r.Mip", "r.TextureStreaming",
                                           "gc.", "s.LevelStreaming"), limit=4),
            "verification": "改一个上面的 cvar 后采 A/B，比较 memory.json 的 process_physical_mb "
                            "与帧时间（内存下降但帧时间不变 → 只是省内存，不是省帧）",
            "invalidation": "若该 group 的大小由内容本身决定（贴图/网格资产），调 cvar 不会降，假设证伪",
        })
    phys = (memory.get("platform") or {}).get("process_physical_mb") or {}
    used, peak = phys.get("used"), phys.get("peak")
    if used and peak and peak > used * 1.5:
        out.append({
            "id": "memory_peak_gap",
            "kind": "investigate_transient_peak",
            "subject": "进程物理内存峰值远高于当值（%.0f vs %.0f MB）" % (peak, used),
            "evidence": {"used_mb": used, "peak_mb": peak,
                         "ratio": round(peak / used, 2), "source": "memory.json platform"},
            "expected_saving_ms": None,
            "levers": levers_for(catalog, ("gc.", "r.Streaming", "s.AsyncLoadingThreadEnabled"), limit=3),
            "verification": "在 hitch 视图对齐峰值发生的时间点；若峰值与 hitch 同期，是加载/PGC 抖动",
            "invalidation": "若峰值发生在启动/切换关卡（一次性），它不影响稳态帧，假设证伪",
        })
    return out


def detect_load(load, catalog):
    """加载侧：单个加载项吃掉大部分加载时间 / 缺通道导致无法归因。"""
    if not load:
        return []
    out = []
    pkgs = load.get("packages") or []
    if pkgs:
        total = sum(p.get("total_ms") or 0.0 for p in pkgs) or 0.0
        top = pkgs[0]
        share = (top.get("total_ms") or 0.0) / total if total else 0.0
        if share >= 0.20:
            out.append({
                "id": "load_top_entry:%s" % str(top.get("name"))[:24].lower().replace(" ", "_"),
                "kind": "investigate_load_entry",
                "subject": "加载 Top1：%s" % str(top.get("name"))[:48],
                "evidence": {"total_ms": top.get("total_ms"), "share_pct": round(100 * share, 1),
                             "count": top.get("count"), "max_ms": top.get("max_ms"),
                             "requests": load.get("request_rows"),
                             "source": "load.json packages"},
                "expected_saving_ms": None,
                "levers": levers_for(catalog, ("s.AsyncLoadingThreadEnabled", "s.MinBulkDataSizeForAsyncLoading",
                                               "s.PriorityAsyncLoadingExtraTime", "r.Streaming"), limit=4),
                "verification": "perfctl digest load <run> --out DIR --b2 复看该条目；"
                                "调异步加载参数后 A/B 比较加载总时长（不是帧时间）",
                "invalidation": "若该条目是一次性 LevelStreaming（只在切关卡时发生），对稳态帧无影响，假设证伪",
            })
    if load.get("request_rows") == 0:
        out.append({
            "id": "load_missing_channel",
            "kind": "data_gap",
            "subject": "trace 里没有 LoadTime 事件",
            "evidence": {"request_rows": 0, "file_activity_rows": load.get("file_activity_rows")},
            "expected_saving_ms": None,
            "levers": [],
            "verification": "确认 profile 的 trace.channels 含 loadtime（我们已自动启用分析模块）",
            "invalidation": "若 trace 确实没采该通道，这不是优化点而是采集缺口",
        })
    return out


def analyze(run_dir, catalog, budget_ms=None, digest_roots=(), ledger_data=None):
    """读 run 目录（或给定目录）里的 digest，产出假设列表 + 口径说明。"""
    def load(kind):
        path = _find_digest(run_dir, kind, digest_roots)
        if not path:
            return None
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)

    cpu, bound, gpu = load("cpu"), load("bound"), load("gpu")
    memory, loadview = load("memory"), load("load")
    manifest_path = os.path.join(run_dir, "manifest.json")
    manifest = {}
    if os.path.isfile(manifest_path):
        with io.open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

    fs = ((manifest.get("frame_semantics") or {}).get("game") or {})
    interval_p50 = ((fs.get("stats") or {}).get("interval_ms") or {}).get("p50")
    frames = fs.get("window_frames") or 0
    graphics_busy = None
    for line in (((bound or {}).get("stats") or {}).get("lines") or []):
        if line.get("line") == "GPU" and line.get("available"):
            graphics_busy = line.get("busy_per_frame_ms")

    hyp = []
    hyp += detect_bound(bound, interval_p50, catalog)
    hyp += detect_gpu_effects(gpu, catalog, graphics_busy)
    hyp += detect_cpu_hotspots(cpu, interval_p50 or 0.0, catalog)
    hyp += detect_memory(memory, catalog, ((memory or {}).get("platform") or {}).get(
        "process_physical_mb", {}).get("used"))
    hyp += detect_load(loadview, catalog)

    if ledger_data:
        from pipeline.advice import ledger as ledger_mod
        hyp = ledger_mod.apply_calibration(hyp, ledger_data)

    return {
        "schema": "perf-advice/0",
        "run_dir": run_dir.replace(os.sep, "/"),
        "context": {"interval_p50_ms": interval_p50, "window_frames": frames,
                    "gpu_graphics_busy_ms": graphics_busy,
                    "budget_ms": budget_ms,
                    "catalog": catalog.get("path"), "catalog_cvars": len(catalog.get("cvars") or []),
                    "catalog_missing": catalog.get("missing", False)},
        "hypotheses": hyp,
    }


def render_md(result):
    ctx = result["context"]
    lines = ["# 优化假设（证据驱动）", "",
             "| 项 | 值 |", "|---|---|",
             "| run | `%s` |" % result["run_dir"],
             "| 帧间隔 p50 | %s ms |" % ctx.get("interval_p50_ms"),
             "| GPU Graphics busy | %s ms/帧 |" % ctx.get("gpu_graphics_busy_ms"),
             "| 杠杆目录 | %s（%s 个 cvar） |"
             % (ctx.get("catalog"), ctx.get("catalog_cvars")),
             ""]
    if ctx.get("catalog_missing"):
        lines += ["> 杠杆目录缺失：运行 `python tools/scan_cvars.py --out catalog/cvars.json` "
                  "（从本机引擎源码扫出当前版本的开关）", ""]
    if not result["hypotheses"]:
        lines += ["没有触发任何检测器（可能窗口太短/数据不足）。", ""]
    for i, h in enumerate(result["hypotheses"], 1):
        lines += ["## %d. %s" % (i, h["subject"]), "",
                  "`%s` · %s" % (h["id"], h["kind"]), ""]
        ev = h.get("evidence") or {}
        lines.append("**证据**：" + "；".join("%s = %s" % (k, v) for k, v in ev.items() if k != "source"))
        saving = h.get("expected_saving_ms")
        lines.append("")
        lines.append("**预期收益**：%s" % ("%.2f – %.2f ms/帧" % tuple(saving) if saving else "无法先验估计（属调查型）"))
        if h.get("levers"):
            lines += ["", "**可调开关（来自本机引擎源码）**：", "",
                      "| cvar | 默认 | 说明 | 位置 |", "|---|---|---|---|"]
            for lv in h["levers"]:
                lines.append("| `%s` | %s | %s | `%s` |"
                             % (lv["name"], lv["default"], (lv.get("help") or "")[:48], lv["where"]))
        lines += ["", "**如何验证**：%s" % h["verification"],
                  "", "**证伪条件**：%s" % h["invalidation"], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    from pipeline.advice import ledger as ledger_mod
    if argv[:1] == ["record"]:
        # perfctl advise record --hypothesis ID --diff DIR [--predicted lo,hi] [--ledger PATH]
        ap2 = argparse.ArgumentParser(description="把一次 A/B 验证写进建议账本")
        ap2.add_argument("--hypothesis", required=True)
        ap2.add_argument("--diff", required=True, help="perfctl diff 的输出目录（含 perf_diff.json）")
        ap2.add_argument("--predicted", default=None, help="预测区间 lo,hi（ms/帧）；不给则从该 run 的 advice.json 找")
        ap2.add_argument("--ledger", default="advice/ledger.json")
        ap2.add_argument("--engine-version", default=None)
        ap2.add_argument("--scene", default=None)
        a2 = ap2.parse_args(argv[1:])
        predicted = None
        if a2.predicted:
            lo, _, hi = a2.predicted.partition(",")
            predicted = [float(lo), float(hi or lo)]
        elif a2.diff:
            for name in ("advice.json",):
                path = os.path.join(a2.diff, name)
                if os.path.isfile(path):
                    with io.open(path, encoding="utf-8") as f:
                        for h in (json.load(f).get("hypotheses") or []):
                            if h.get("id") == a2.hypothesis:
                                predicted = h.get("expected_saving_ms")
                                break
        entry = ledger_mod.record_from_diff(a2.ledger, a2.hypothesis, a2.diff, predicted,
                                            a2.engine_version, a2.scene)
        print("[ledger] %s 实测 Δ=%s ms，判定 %s（共 %d 条记录）"
              % (a2.hypothesis, entry["measured_ms"], entry["verdict"],
                 len(ledger_mod.load(a2.ledger)["entries"])))
        cal = ledger_mod.calibration(ledger_mod.load(a2.ledger), a2.hypothesis)
        print("  校准：样本 %d，实测中位 %s ms，预测中点 %s ms，因子 ×%s%s"
              % (cal["samples"], cal["measured_median_ms"], cal["predicted_mid_ms"], cal["factor"],
                 "，已降级" if cal["retired"] else ""))
        return 0
    if argv[:1] == ["ledger"]:
        ap3 = argparse.ArgumentParser(description="查看建议账本")
        ap3.add_argument("--ledger", default="advice/ledger.json")
        ap3.add_argument("--out", default=None, help="同时写一份 md")
        a3 = ap3.parse_args(argv[1:])
        data = ledger_mod.load(a3.ledger)
        md = ledger_mod.render_md(data)
        print(md)
        if a3.out:
            os.makedirs(a3.out, exist_ok=True)
            with io.open(os.path.join(a3.out, "ledger.md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(md + "\n")
            print("[ok] %s" % os.path.join(os.path.abspath(a3.out), "ledger.md"))
        return 0

    ap = argparse.ArgumentParser(description="从 digest 证据生成可证伪的优化假设")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--catalog", default=None, help="杠杆目录（tools/scan_cvars.py 产出）")
    ap.add_argument("--budget-ms", type=float, default=None, help="目标帧预算（例如 1000/60）")
    ap.add_argument("--digest-dir", action="append", default=[],
                    help="额外搜索 digest 的目录（可重复；默认既看 run 目录也看其父目录）")
    ap.add_argument("--ledger", default=None, help="建议账本（给出后会用历史校准预期收益）")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    if not os.path.isdir(run_dir):
        print("[error] run dir not found: %s" % run_dir)
        return 1
    catalog = load_catalog(a.catalog)
    ledger_data = None
    if a.ledger:
        from pipeline.advice import ledger as ledger_mod
        ledger_data = ledger_mod.load(a.ledger)
    result = analyze(run_dir, catalog, a.budget_ms, tuple(a.digest_dir), ledger_data)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "advice.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    md = render_md(result)
    with io.open(os.path.join(a.out, "advice.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(md + "\n")

    print("[advise] %d 条假设（杠杆目录 %s 个 cvar%s）"
          % (len(result["hypotheses"]), result["context"]["catalog_cvars"],
             "，缺失" if result["context"]["catalog_missing"] else ""))
    for h in result["hypotheses"]:
        saving = h.get("expected_saving_ms")
        print("  %-34s %s" % (h["subject"][:34],
                              ("预计省 %.2f–%.2f ms/帧" % tuple(saving)) if saving else "调查型"))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "advice.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
