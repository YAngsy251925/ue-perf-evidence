#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m3_4_demo.py — M3.4 闭环演示：改 → 测 → 对比 → 撤销 → 复测（一条命令复现）

做三组真实的 measure run（真实 RHI、PLAN §7.2 固定条件、每组 n=5，按 A/B/C 交错减少漂移）：
  A：基线            profile=measure
  B：注入已知回归     profile=measure_regress（同一套条件 + PerfEvidence.BusyCount 10 / BusyMs 2）
  C：撤销后复测       profile=measure（与 A 相同）

然后按 R9 判定两次：
  A vs B —— 期望 B 更慢，且 Top 自耗时表里新增 PerfEvidenceRegress
  B vs C —— 期望 C 更快（恢复），PerfEvidenceRegress 消失

用法（仓库根目录）：
  python -X utf8 harness/m3_4_demo.py [--n 5] [--runs-dir runs/m3_4] [--skip-capture]
"""
import argparse
import io
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pipeline.capture import capture                # noqa: E402
from pipeline.caliber import manifest as M         # noqa: E402
from pipeline.digest import attribution as AT      # noqa: E402
from pipeline.digest import diff as D              # noqa: E402

PROFILE_A = os.path.join(REPO, "harness", "profiles", "measure.json")
PROFILE_B = os.path.join(REPO, "harness", "profiles", "measure_regress.json")
INJECTED = "PerfEvidenceRegress"


def run_one(profile, run_dir, uproject=None):
    # 可重入：没有 manifest 的 run 目录（上次被打断）先清空，否则 profile.prepare 会按 P14 拒绝
    if os.path.isdir(run_dir) and os.listdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    args = [profile, run_dir] + (["--uproject", uproject] if uproject else [])
    rc = capture.main(args)
    if rc != 0:
        return False
    return M.main([run_dir]) == 0


def collect_group(runs_dir, names, n):
    """把组内可用 run 的 side 结构收集起来（build_group_diff 需要 run_dir/manifest/trace）。"""
    sides = []
    for i in range(1, n + 1):
        run_dir = os.path.join(REPO, runs_dir, "%s%d" % (names, i))
        manifest_path = os.path.join(run_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            print("[demo] 缺 %s，跳过" % manifest_path)
            continue
        with io.open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        sides.append({"run_dir": run_dir, "manifest": manifest,
                      "trace": os.path.join(run_dir, "trace.utrace")})
    return sides


def do_diff(sides_a, sides_b, out_dir, label):
    d, md = D.build_group_diff(sides_a, sides_b, timer_diff=True)
    os.makedirs(out_dir, exist_ok=True)
    with io.open(os.path.join(out_dir, "perf_diff.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(out_dir, "perf_diff.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    sig = d.get("significance") or {}
    injected = [r for r in (d.get("top_delta") or []) if INJECTED in (r.get("name") or "")]
    print("[demo] %s: verdict=%s Δmean=%s 阈值=%s（%s）"
          % (label, sig.get("verdict"), sig.get("delta_mean"), sig.get("threshold"),
             sig.get("status")))
    for row in injected[:2]:
        print("[demo]   %s: A=%.3fs B=%.3fs Δ=%+.3fs" % (row["name"], row["a_s"], row["b_s"], row["delta_s"]))
    return d, md, sig, injected


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="M3.4 闭环演示（A/B/C 三组 measure run + R9 判定）")
    ap.add_argument("--n", type=int, default=5, help="每组 run 数（R9 要求 ≥5）")
    ap.add_argument("--runs-dir", default=os.path.join("runs", "m3_4"))
    ap.add_argument("--profile-a", default=PROFILE_A, help="A/C 组 profile（基线）")
    ap.add_argument("--profile-b", default=PROFILE_B, help="B 组 profile（注入）")
    ap.add_argument("--uproject", default=None, help="采集用的 uproject（默认宿主工程）")
    ap.add_argument("--skip-capture", action="store_true", help="跳过采集，只用已有 run 重新出报告")
    a = ap.parse_args(argv)

    os.makedirs(os.path.join(REPO, a.runs_dir), exist_ok=True)
    if not a.skip_capture:
        for i in range(1, a.n + 1):
            for group, profile in (("A", a.profile_a), ("B", a.profile_b), ("C", a.profile_a)):
                run_dir = os.path.join(REPO, a.runs_dir, "%s%d" % (group, i))
                if os.path.isfile(os.path.join(run_dir, "manifest.json")):
                    print("[demo] %s%d 已存在，跳过采集" % (group, i))
                    continue
                print("[demo] === %s%d (%s) ===" % (group, i, os.path.basename(profile)), flush=True)
                if not run_one(profile, run_dir, a.uproject):
                    print("[demo] %s%d 失败，终止采集" % (group, i))
                    return 1

    groups = {g: collect_group(a.runs_dir, g, a.n) for g in ("A", "B", "C")}
    counts = {g: len(v) for g, v in groups.items()}
    print("[demo] 收集到 %s" % counts)
    if min(counts.values()) < 2:
        print("[demo] 组内 run 太少，无法对比")
        return 1

    ab_dir = os.path.join(REPO, a.runs_dir, "diff_A_vs_B")
    bc_dir = os.path.join(REPO, a.runs_dir, "diff_B_vs_C")
    d_ab, md_ab, sig_ab, inj_ab = do_diff(groups["A"], groups["B"], ab_dir, "A vs B（注入）")
    d_ab_reps = {"a": D.pick_representative(groups["A"]), "b": D.pick_representative(groups["B"])}
    d_bc, md_bc, sig_bc, inj_bc = do_diff(groups["B"], groups["C"], bc_dir, "B vs C（撤销）")
    d_bc_reps = {"a": D.pick_representative(groups["B"]), "b": D.pick_representative(groups["C"])}

    ok_slow = sig_ab.get("verdict") == "b_slower"
    ok_recover = sig_bc.get("verdict") == "b_faster"
    # 归因不看总秒数（窗口帧数会把总时间缩放），看每帧成本差是否等于帧时间差
    def frames_of(side):
        return ((side["manifest"].get("frame_semantics") or {}).get("game") or {}).get("window_frames")

    att_ab = AT.span_attribution(d_ab, INJECTED, frames_of(d_ab_reps["a"]), frames_of(d_ab_reps["b"]))
    att_bc = AT.span_attribution(d_bc, INJECTED, frames_of(d_bc_reps["a"]), frames_of(d_bc_reps["b"]))
    attributed = bool(att_ab.get("ok")) and bool(att_bc.get("ok"))
    summary = {
        "schema": "perf-demo/0",
        "n": counts,
        "injection": {"cvar": "PerfEvidence.BusyCount=10 / BusyMs=2", "span": INJECTED},
        "a_vs_b": {"verdict": sig_ab.get("verdict"), "status": sig_ab.get("status"),
                   "delta_mean_ms": sig_ab.get("delta_mean"), "threshold_ms": sig_ab.get("threshold"),
                   "expected": "b_slower", "pass": ok_slow,
                   "attribution": att_ab,
                   "injected_rows": inj_ab[:3]},
        "b_vs_c": {"verdict": sig_bc.get("verdict"), "status": sig_bc.get("status"),
                   "delta_mean_ms": sig_bc.get("delta_mean"), "threshold_ms": sig_bc.get("threshold"),
                   "expected": "b_faster", "pass": ok_recover,
                   "attribution": att_bc,
                   "injected_rows": inj_bc[:3]},
        "attribution_ok": attributed,
        "pass": ok_slow and ok_recover and attributed,
    }
    out = os.path.join(REPO, a.runs_dir, "demo_summary.json")
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("[demo] 归因：注入 span 在 A vs B 出现=%s，在 B vs C 消失=%s"
          % (bool(inj_ab), not inj_bc))
    print("[demo] 判定：B 更慢=%s，撤销后恢复=%s，归因正确=%s → %s"
          % (ok_slow, ok_recover, attributed, "PASS" if summary["pass"] else "FAIL"))
    print("[ok] %s" % out)
    return 0 if summary["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
