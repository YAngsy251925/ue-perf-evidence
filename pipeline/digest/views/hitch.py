#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hitch.py — hitch 视图（digest v0，PLAN §9-M2.4）

输入：run 目录（hitches_index.json + hitches/ 下的快照导出）。manifest 只用来写口径行。
固定顺序（§5.3）：结论 → 口径 → 每个 hitch 的块（帧号/时长/GT/RT/树 Top/快照 Top-N）→
冷却与滞后说明（R10）→ 警告。
快照导出里的统计是快照自身的（含 hitch 帧前后各几帧）；Top-N 直接用 GameThread 线程统计
排除等待类后的自耗时前 5—— hitch 帧的瓶颈通常就在树顶，等待类在这里也是症状（R5/R15 不展开）。
"""
import argparse
import json
import os
import sys

SCHEMA = "perf-hitch-digest/0"
TREE_TOP = 8
SNAPSHOT_TOP = 5
WAIT_RE = None  # 延迟导入 cpu 视图的等待规则，保持单一来源


def load_index(run_dir):
    path = os.path.join(run_dir, "hitches_index.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tree_top(tree, n=TREE_TOP):
    """调用树展示：深度最小的 n 个节点（树的骨架），跳过线程根与 OtherChildren/Self 的纯噪音层。"""
    out = []
    for node in tree:
        if node["name"] in ("OtherChildren", "Self"):
            continue
        out.append(node)
        if len(out) >= n:
            break
    return out


def root_cause_node(tree):
    """根因：从最重的深度-0 节点出发，沿"最重的具名子节点"下钻，
    直到没有占父节点 50% 以上的具名子节点（跳过 Self / OtherChildren 噪音层）。
    实测（coexist_C）：Thread → FrameTime → Deferred Tick Time → TickDeferredCommands
    → HandleMemReportDeferredCommand（269.019ms，正是 memreport 的 GC）。"""
    named = [n for n in tree if n["name"] not in ("Self", "OtherChildren")]
    if not named:
        return None
    node = max(named, key=lambda n: n["ms"])
    while True:
        children = [n for n in tree
                    if n["name"] not in ("Self", "OtherChildren")
                    and n["depth"] == node["depth"] + 1
                    and n["ms"] >= 0.5 * node["ms"]]
        if not children:
            return node
        node = max(children, key=lambda n: n["ms"])


def snapshot_top_timers(export_dir, n=SNAPSHOT_TOP):
    """快照导出的 GameThread 统计 Top-N（自耗时，等待类单列计数）。"""
    from pipeline.digest.views import cpu
    from pipeline.extract import uecsv
    src = os.path.join(export_dir, "stats_GameThread.csv")
    if not os.path.isfile(src):
        return None
    rows = cpu.load_rows(src)
    total = sum(r["excl"] for r in rows) or 1e-9
    from pipeline.digest import run_digest
    others, wait, wait_total = run_digest.split_wait(cpu.load_rows(src)) if rows else ([], [], None)
    top = sorted(others, key=lambda r: -r["excl"])[:n]
    return {"total_self_s": round(total, 3),
            "top": [{"name": r["name"], "self_s": round(r["excl"], 3),
                     "self_pct": round(100.0 * r["excl"] / total, 2), "calls": r["count"]} for r in top],
            "wait_total": wait_total}


def build(run_dir, manifest=None):
    index = load_index(run_dir)
    caliber = None
    measurable = None
    if manifest is not None:
        eng = manifest.get("engine") or {}
        caliber = {
            "run_id": manifest.get("run_id"),
            "rhi": eng.get("rhi"), "rhi_state": eng.get("rhi_state"),
            "stats_window": (manifest.get("window_basis") or {}).get("stats_window"),
            "steady": (manifest.get("windows") or {}).get("steady"),
        }
        measurable = manifest.get("measurable")
    if index is None:
        digest = {"schema": SCHEMA, "kind": "hitch", "capability": "no_evidence",
                  "conclusion": {"text": "本 run 没有 hitches_index.json（不是 hitch-hunt run，或收集步骤"
                                         "没有执行）；先运行 perfctl capture，再运行 perfctl hitches <run_dir>",
                                 "confidence": "none"},
                  "caliber": caliber, "hitches": [], "snapshots": [], "warnings": []}
        return digest, render_md(digest)

    hitches = index.get("hitches") or []
    snapshots = index.get("snapshots") or []
    warnings = []
    if not hitches:
        text = ("整个运行期没有检出 hitch（阈值内为阴性）。dumphitches 已武装且全程逐帧检查；"
                "若预期有 hitch，检查注入方式与 t.HitchFrameTimeThreshold。")
    elif len(hitches) == 1:
        h = hitches[0]
        snap = next((s for s in snapshots if s["stats_frame"] == h["stats_frame"] and s["matched_dumphitches_block"]),
                    None)
        node = root_cause_node(h.get("game_tree") or [])
        text = ("检出 1 次 hitch：stats 帧 %d，%0.1fms（GT %0.1fms / RT %0.1fms）。根因链最重的是 %s"
                "（%0.1fms）。快照见 hitches/Frame-%d.utrace。"
                % (h["stats_frame"], h["ms"], h["game_thread_ms"] or 0, h["render_thread_ms"] or 0,
                   node["name"] if node else "未知", node["ms"] if node else 0, h["stats_frame"]))
    else:
        by_ms = sorted(hitches, key=lambda h: -h["ms"])[:3]
        text = ("检出 %d 次 hitch（冷却 7 帧内只报首次）。最重的 %d 次：%s。"
                % (len(hitches), len(by_ms),
                   "；".join("stats 帧 %d / %0.1fms" % (h["stats_frame"], h["ms"]) for h in by_ms)))
    confidence = "clear" if hitches else "none"

    entries = []
    for h in hitches:
        snap = next((s for s in snapshots if s["stats_frame"] == h["stats_frame"]), None)
        export_dir = os.path.join(run_dir, snap["export_dir"]) if snap else None
        entries.append({
            "index": h["index"], "stats_frame": h["stats_frame"], "ms": h["ms"],
            "game_thread_ms": h["game_thread_ms"], "render_thread_ms": h["render_thread_ms"],
            "tree_top": tree_top(h.get("game_tree") or []),
            "render_tree_top": tree_top(h.get("render_tree") or [])[:3],
            "triggers": h.get("triggers") or [],
            "snapshot": snap.get("snapshot") if snap else None,
            "screenshot": snap.get("screenshot") if snap else None,
            "snapshot_top": snapshot_top_timers(export_dir) if export_dir else None,
            "matched_dumphitches_block": snap.get("matched_dumphitches_block") if snap else False,
        })
    digest = {
        "schema": SCHEMA, "kind": "hitch",
        "capability": "measurable" if measurable is not False else "not_measurable",
        "conclusion": {"text": text, "confidence": confidence},
        "caliber": caliber,
        "hitches": entries,
        "notes": {
            "cooldown_frames": {"dumphitches": 7, "snapshothitches": 10},
            "detection_lag_frames": 6,
            "source": "stat dumphitches（日志树）+ snapshothitches（Frame-N.utrace 快照）；"
                      "两者同帧触发，冷却不同（R10）",
        },
        "warnings": list(manifest.get("warnings", [])) if manifest else [],
    }
    return digest, render_md(digest)


def render_md(digest):
    c = digest.get("caliber") or {}
    lines = ["# Perf digest — %s — %s" % (digest["kind"], c.get("run_id", "?")), ""]
    lines += ["## 结论", digest["conclusion"]["text"] + "（置信度: %s）" % digest["conclusion"]["confidence"], ""]
    if not digest["hitches"] and digest["capability"] == "no_evidence":
        lines += ["## 警告"] + ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
        return "\n".join(lines) + "\n"
    lines += ["## 口径",
              "- run_id: %s" % c.get("run_id"),
              "- rhi: %s（state=%s）" % (c.get("rhi"), c.get("rhi_state")),
              "- 稳态窗口: %s" % c.get("steady"), ""]
    for h in digest["hitches"]:
        lines += ["## Hitch %d — stats 帧 %d — %.1fms" % (h["index"], h["stats_frame"], h["ms"]),
                  "- Game Thread: %.2fms；Render Thread: %.2fms" % (h["game_thread_ms"] or 0, h["render_thread_ms"] or 0)]
        if h["snapshot"]:
            lines.append("- 快照: %s%s" % (h["snapshot"], "（截图 %s）" % h["screenshot"] if h["screenshot"] else ""))
        lines += ["", "### Game Thread 树（骨架 Top %d）" % len(h["tree_top"]),
                  "| depth | ms | calls | 名字 | STAT |", "|---|---|---|---|---|"]
        lines += ["| %d | %s | %d | %s | %s |" % (n["depth"], n["ms"], n["calls"], n["name"],
                                                  n["stat_name"] or "") for n in h["tree_top"]]
        if h["snapshot_top"]:
            st = h["snapshot_top"]
            lines += ["", "### 快照 GameThread 自耗时 Top %d（全部 %.3fs，等待类 %s%%）"
                      % (len(st["top"]), st["total_self_s"],
                         st["wait_total"]["self_pct"] if st["wait_total"] else "?"),
                      "| timer | self_s | self% | calls |", "|---|---|---|---|"]
            lines += ["| %s | %s | %s%% | %s |" % (r["name"], r["self_s"], r["self_pct"], r["calls"])
                      for r in st["top"]]
        lines.append("")
    lines += ["## 时序说明（R10）", digest["notes"]["source"],
              "dumphitches 冷却 %d 帧、snapshothitches 冷却 %d 帧、检测滞后约 %d 帧。"
              % (digest["notes"]["cooldown_frames"]["dumphitches"],
                 digest["notes"]["cooldown_frames"]["snapshothitches"],
                 digest["notes"]["detection_lag_frames"]), ""]
    lines += ["## 警告"] + ["- %s" % w for w in digest["warnings"]] or ["- （无）"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="hitch 视图（PLAN §9-M2.4）")
    ap.add_argument("run_dir", help="run 目录（hitches_index.json 已在）")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--manifest", default=None, help="manifest 路径（默认 <run_dir>/manifest.json，可选）")
    a = ap.parse_args(argv)
    run_dir = os.path.abspath(a.run_dir)
    manifest = None
    manifest_path = a.manifest or os.path.join(run_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    digest, md = build(run_dir, manifest)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "perf_digest.json"), "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "perf_digest.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "perf_digest.md"))
    print("  %s" % digest["conclusion"]["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
