#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manifest.py — 生成 run 的 manifest v0（PLAN §5.2，M1.2–M1.4）

输入：run 目录（engine.log、trace.utrace、extract/ 下的 probe 导出及其 insights.log），各项都可以用参数覆盖。
输出：<run_dir>/manifest.json（--out 可改）。同一输入逐字节同一输出：不写生成时间。
v0 覆盖 RHI 与 measurable、桥接状态、构建信息与能力（M1.2），时间窗与预热帧排除（M1.3），
GT / RT 帧口径与稳态窗口精确分位数（M1.4）；§5.2 里还没实现的字段列在 pending。
extract 不完整但有 trace 时，先自动跑一次 probe 导出。

用法（在仓库根目录）：
  python perfctl.py manifest <run_dir> [--log FILE] [--extract DIR] [--trace FILE] [--out FILE] [--warmup-frames N]
"""
import argparse
import json
import os
import re
import sys

from pipeline.caliber import conditions
from pipeline.caliber.bridge_detect import detect_bridge
from pipeline.caliber.engine_log import parse_engine_log
from pipeline.caliber.frame_probe import b2_series, frames_seen, load_frame_series, pick_reference
from pipeline.caliber.frame_semantics import build_frame_semantics
from pipeline.caliber.insights_log import read_session_duration
from pipeline.caliber.rhi_detect import detect_rhi
from pipeline.caliber.windows import DEFAULT_WARMUP_FRAMES, split_windows
from pipeline.extract import insights_export, uecsv

SCHEMA = "perf-manifest/0"
PROBE_FILES = ("timers.csv", "frame_tracks.csv", "frame_scopes.csv")
COVERAGE_FILE = "coverage.json"  # perfctl capture 写的 coverage 证据（trace 书签时间）
# §5.2 里尚未实现的字段及其里程碑；字段缺席不等于“没有数据”
PENDING = [
    "frame_semantics.gpu (M4.0)",
    "hitch (M2.3)",
    "retention (M2.6)",
    "session (M3.0)",
    "presentmon (M5.3)",
    "mapping (M5.4)",
    "excluded (M6.1)",
]
RE_TRACE_CHANNELS = re.compile(r"(?:^|\s)-trace=(\S+)", re.IGNORECASE)


def read_timer_names(extract: str):
    return [r["Name"] for r in uecsv.read_dicts(os.path.join(extract, "timers.csv"))]


def describe_command_line(cmd):
    if not cmd:
        return {"map": None, "mode": None, "trace_channels_requested": None}
    tokens = cmd.split()
    lowered = [t.lower() for t in tokens]
    if any(t.startswith("-run=") for t in lowered):
        mode = "commandlet"
    else:
        mode = "game" if "-game" in lowered else "editor"
    m = RE_TRACE_CHANNELS.search(cmd)
    return {
        "map": tokens[0] if tokens and tokens[0].startswith("/") else None,
        "mode": mode,
        "trace_channels_requested": m.group(1).split(",") if m else [],
    }


def capabilities(build_config):
    if not build_config:
        return {"stats": None, "memreport": None, "snapshothitches": None,
                "source": "unknown: no build configuration in the engine log"}
    shipping = build_config.lower() == "shipping"
    return {"stats": not shipping, "memreport": not shipping, "snapshothitches": not shipping,
            "source": "build_config=%s (ALLOW_DEBUG_FILES, UE_SNAPSHOTHITCHES_ENABLED)" % build_config}


def display_path(path, run_dir):
    if not path:
        return None
    path, run_dir = os.path.abspath(path), os.path.abspath(run_dir)
    try:
        inside = os.path.commonpath([path, run_dir]) == run_dir
    except ValueError:  # 不同盘符（如 run 目录在 C:、实验数据在 D:）时 commonpath 会抛异常
        inside = False
    if inside:
        return os.path.relpath(path, run_dir).replace("\\", "/")
    return path.replace("\\", "/")


def load_capture_snapshot(run_dir):
    """perfctl profile prepare 写在 run 目录里的声明快照；手工 run 没有它。"""
    path = os.path.join(run_dir, conditions.SNAPSHOT_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_coverage(run_dir):
    """perfctl capture 写的 coverage 证据（PerfEvidence 书签的 trace 时间）；没有则 None。"""
    path = os.path.join(run_dir, COVERAGE_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cov = data.get("coverage")
    if not cov or cov.get("start") is None or cov.get("end") is None:
        return None
    return cov


def apply_coverage(windows, basis, coverage, snapshot, warnings):
    """P15 双窗口隔离：coverage_probe = CSV 开启段（含写文件尖峰）；
    stats = 稳态窗口去掉 coverage 后剩下的干净前段。CSV 没盖住稳态起点时 stats 才存在；
    否则降级：stats 保持 steady 并写 warning（_digest 侧按 R11 机制拒绝数字）。
    返回 capabilities 里的 csv_profiler 值。"""
    if coverage is None:
        return None
    cov_start, cov_end = float(coverage["start"]), float(coverage["end"])
    windows["coverage_probe"] = [cov_start, cov_end]
    steady = windows.get("steady")
    if steady and cov_start > steady[0]:
        windows["stats"] = [steady[0], min(steady[1], cov_start)]
        basis["stats_window"] = "stats"
        basis["stats_window_derivation"] = ("steady minus coverage_probe (P15); the write spike lives "
                                            "inside coverage_probe")
        return True
    warnings.append("coverage_probe starts at or before the steady window: no CSV-free stats window "
                    "remains (P15 fallback: treat the run as probe-covered)")
    basis["stats_window_derivation"] = "steady kept (coverage_probe covers it; P15 fallback)"
    return True


def build_manifest(run_dir, log_path, extract, trace_path, warmup_frames=None) -> dict:
    """warmup_frames：显式值 > capture_profile.json 声明的值 > DEFAULT_WARMUP_FRAMES。"""
    warnings = []
    snapshot = load_capture_snapshot(run_dir)
    if warmup_frames is None:
        declared = ((snapshot or {}).get("profile") or {}).get("conditions") or {}
        warmup_frames = declared.get("warmup_frames", DEFAULT_WARMUP_FRAMES)
    log = parse_engine_log(log_path) if log_path and os.path.isfile(log_path) else None
    if log is None:
        warnings.append("no engine log: command line, build configuration and RHI name are unknown")
    names = read_timer_names(extract)
    series = load_frame_series(extract)
    # R4 的第三级：A 路一条帧轨道都没有时，退回 B2 的 IFrameProvider（M4.2）
    if pick_reference(series) is None and trace_path and os.path.isfile(trace_path):
        fallback, b2_warnings = b2_series(trace_path, extract)
        if pick_reference(fallback) is not None:
            series = fallback
            warnings += b2_warnings
    frames = frames_seen(series)
    rhi = detect_rhi(log, frames)
    cmd = log["command_line"] if log else None
    bridge = detect_bridge(names, cmd)
    insights_log = os.path.join(extract, "insights.log")
    has_insights_log = os.path.isfile(insights_log)
    duration = read_session_duration(insights_log) if has_insights_log else None
    win = split_windows(pick_reference(series), duration, warmup_frames)
    warnings += rhi.pop("warnings") + bridge.pop("warnings") + win["warnings"]
    measurable, reason = rhi.pop("measurable"), rhi.pop("measurable_reason")
    coverage = load_coverage(run_dir)
    csv_profiler = apply_coverage(win["windows"], win["basis"], coverage, snapshot, warnings)
    frame_semantics, fs_warnings = build_frame_semantics(
        series, win["windows"], win["basis"], measurable, reason, rhi["rhi_state"])
    warnings += fs_warnings
    desc = describe_command_line(cmd)
    profile_block, conditions_block, cond_warnings = conditions.verify(
        snapshot, log, rhi["rhi_state"], measurable, bridge["statnamedevents_effective"],
        desc["trace_channels_requested"], win["basis"], win["windows"], (log or {}).get("build_config"),
        coverage)
    warnings += cond_warnings
    log = log or {}
    if csv_profiler is not None:
        log["csv_profiler_coverage"] = coverage
    return {
        "schema": SCHEMA,
        "run_id": os.path.basename(os.path.abspath(run_dir)),
        "measurable": measurable,
        "measurable_reason": reason,
        "profile": profile_block,
        "conditions": conditions_block,
        "engine": {
            "version": log.get("engine_version"),
            "build": log.get("build"),
            "build_config": log.get("build_config"),
            "platform": log.get("platform"),
            **rhi,
        },
        "command_line": cmd,
        "map": desc["map"],
        "mode": desc["mode"],
        "capabilities": {**capabilities(log.get("build_config")), "csv_profiler": csv_profiler},
        "trace": {
            "file": display_path(trace_path, run_dir),
            "channels_requested": desc["trace_channels_requested"],
            "timer_count": len(names),
            **bridge,
        },
        "frames_seen": frames,
        "windows": win["windows"],
        "window_basis": win["basis"],
        "frame_semantics": frame_semantics,
        "inputs": {
            "engine_log": display_path(log_path, run_dir) if log else None,
            "extract": display_path(extract, run_dir),
            "insights_log": display_path(insights_log, run_dir) if has_insights_log else None,
        },
        "log_first_timestamp_utc": log.get("first_timestamp_utc"),
        "coverage_probe_source": {"file": COVERAGE_FILE, **coverage} if coverage else None,
        "pending": list(PENDING),
        "warnings": warnings,
    }


def fmt_window(window) -> str:
    return "none" if window is None else "[%.4f, %.4f]" % (window[0], window[1])


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="生成 run 的 manifest v0（PLAN §5.2）")
    ap.add_argument("run_dir", help="run 目录；manifest.json 默认写在这里")
    ap.add_argument("--log", help="引擎日志（默认 <run_dir>/engine.log）")
    ap.add_argument("--extract", help="probe 导出目录（默认 <run_dir>/extract）")
    ap.add_argument("--trace", help="trace 文件（默认 <run_dir>/trace.utrace）")
    ap.add_argument("--out", help="输出文件（默认 <run_dir>/manifest.json）")
    ap.add_argument("--warmup-frames", type=int, default=None,
                    help="预热帧数，不进入稳态窗口（默认取 capture_profile.json 的声明，再退回 %d，PLAN R3）"
                    % DEFAULT_WARMUP_FRAMES)
    ap.add_argument("--ue-root", default=None, help="自动 probe 导出时使用的 UE 根目录")
    a = ap.parse_args(argv)
    if a.warmup_frames is not None and a.warmup_frames < 0:
        ap.error("--warmup-frames must be >= 0")

    run_dir = os.path.abspath(a.run_dir)
    log = a.log or os.path.join(run_dir, "engine.log")
    extract = os.path.abspath(a.extract or os.path.join(run_dir, "extract"))
    trace = a.trace or os.path.join(run_dir, "trace.utrace")
    trace = trace if os.path.isfile(trace) else None

    if not all(os.path.isfile(os.path.join(extract, n)) for n in PROBE_FILES):
        if not trace:
            print("[error] no probe export in %s and no trace file to export from" % extract)
            return 1
        print("[manifest] probe export missing; exporting from %s" % trace)
        rc = insights_export.main((["--ue-root", a.ue_root] if a.ue_root else []) + ["probe", trace, "--out", extract])
        if rc != 0:
            return rc

    manifest = build_manifest(run_dir, log, extract, trace, a.warmup_frames)
    out = os.path.abspath(a.out or os.path.join(run_dir, "manifest.json"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    eng, tr = manifest["engine"], manifest["trace"]
    win, basis = manifest["windows"], manifest["window_basis"]
    ref = basis["reference"]
    print("[ok] %s" % out)
    print("  rhi=%s state=%s source=%s measurable=%s" % (eng["rhi"], eng["rhi_state"], eng["rhi_source"], manifest["measurable"]))
    print("  bridge requested=%s effective=%s evidence=%s" % (
        tr["statnamedevents_requested"], tr["statnamedevents_effective"], tr["bridge_evidence"]))
    print("  reference=%s frames=%d (warmup %d, steady %d, non-finite dropped %d)" % (
        "none" if ref is None else "%s %s %s" % (ref["side"], ref["source"], ",".join(ref["threads"])),
        basis["frames_total"], basis["frames_warmup"], basis["frames_steady"], basis["nonfinite_dropped"]))
    print("  windows: startup=%s frames=%s steady=%s stats=%s coverage=%s shutdown=%s" % (
        fmt_window(win["startup"]), fmt_window(win["frames"]), fmt_window(win["steady"]),
        fmt_window(win.get("stats")), fmt_window(win.get("coverage_probe")), fmt_window(win["shutdown"])))
    fs = manifest["frame_semantics"]
    for side in ("game", "render"):
        print("  %s" % fmt_side(side, fs[side]))
    c = fs["consistency"]
    print("  consistency(Q3): %s" % (c["note"] if c["ok"] is None else "ok=%s rows_delta=%d window_frames_delta=%d" % (
        c["ok"], c["rows_delta"], c["window_frames_delta"])))
    if manifest["profile"]:
        s = manifest["conditions"]["summary"]
        print("  profile=%s (%s) conditions: verified=%d failed=%s unchecked=%s" % (
            manifest["profile"]["name"], manifest["profile"]["class"], s["verified"], s["failed"], s["unchecked"]))
    for w in manifest["warnings"]:
        print("  warning: %s" % w)
    return 0


def fmt_side(side: str, d: dict) -> str:
    head = "%-6s source=%s %s frames=%d window_frames=%d" % (
        side, d["source"], ",".join(d["threads"]) or "-", d["frames"], d["window_frames"])
    st = d["stats"]
    if st is None:
        return head + " stats=none (%s)" % d["stats_withheld"]
    du, iv = st["duration_ms"], st["interval_ms"]
    text = head + " | duration p50=%.4f p99=%.4f max=%.4f" % (du["p50"], du["p99"], du["max"])
    if iv:
        text += " | interval p50=%.4f p99=%.4f contiguous=%s" % (iv["p50"], iv["p99"], st["contiguous"])
    return text


if __name__ == "__main__":
    sys.exit(main())
