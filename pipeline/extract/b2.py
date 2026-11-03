#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""b2.py — B2（宿主 commandlet）深度读取的 Python 侧（M4.2）

用法：
  python perfctl.py b2 frames <trace.utrace> --out DIR [--limit N] [--ue-root DIR]
  python perfctl.py b2 timers <trace.utrace> --out DIR [--limit N]

做什么：调 host/TraceDigestProbe/run_cmdlet.bat 跑 TraceDigestPerf 的 mode=frames / mode=timers，
把 JSON 落到 DIR 下，并打一行摘要。trace 落在某个 run 目录里时，自动与 manifest 的
frames_seen / trace.timer_count 对账——这是 B2 与 A 路（Insights 导出）的独立交叉验证。

坑：commandlet 的 out= 必须给绝对路径（相对路径在引擎工作目录下解析，实测会静默成功但没有文件）；
引擎会把 "mode=frames" 改写成 "mode frames"，所以 C++ 侧两种形态都认（ParseArg）。
"""
import argparse
import io
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOST_DIR = os.path.join(REPO, "host", "TraceDigestProbe")
DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"


def run_mode(mode, trace, out_path, limit=0, ue_root=None, timeout=600,
             start=None, end=None, thread=None):
    """跑一次 commandlet，返回 (ok, 日志文本)。日志里的 PERF_ 行是唯一的成功标志。"""
    trace = os.path.abspath(trace)
    out_path = os.path.abspath(out_path)
    if not os.path.isfile(trace):
        return False, "trace not found: %s" % trace
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    log_path = os.path.splitext(out_path)[0] + ".log"
    # cmd 会把 "key=value" 按 '=' 拆成两个参数，所以统一用空格形态："mode frames"。
    # 直接写引擎命令行（不经 run_cmdlet.bat）：bat 里 %~dp0 在 call 嵌套下会指错目录，
    # 曾导致引擎拿到 "<run 目录>/TraceDigestProbe.uproject" 直接打不开工程。
    extra = ["mode", mode, "out", out_path]
    if limit > 0:
        extra += ["limit", str(limit)]
    if start is not None:
        extra += ["start", ("%.6f" % start)]
    if end is not None:
        extra += ["end", ("%.6f" % end)]
    if thread:
        extra += ["thread", thread]
    ue_root = ue_root or os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT
    exe = os.path.join(ue_root, "Engine", "Binaries", "Win64", "UnrealEditor-Cmd.exe")
    uproject = os.path.join(HOST_DIR, "TraceDigestProbe.uproject")
    trace_arg = trace.replace("\\", "/")
    bat = os.path.splitext(out_path)[0] + ".bat"
    line = " ".join([
        '"%s"' % exe, '"%s"' % uproject, "-run=TraceDigestPerf", "trace", '"%s"' % trace_arg,
    ] + extra + [
        "-unattended", "-nosplash", "-nullrhi", "-nosound", "-stdout", '-ABSLOG="%s"' % log_path,
    ])
    with io.open(bat, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\r\n%s\r\n" % line)
    proc = subprocess.run(["cmd", "/c", bat], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    text = (proc.stdout or "") + (proc.stderr or "")
    if os.path.isfile(log_path):
        with io.open(log_path, encoding="utf-8-sig", errors="replace") as f:
            text += f.read()
    ok = os.path.isfile(out_path) and "PERF_DONE" in text
    return ok, text


def manifest_beside(trace):
    """trace 在 run 目录里时返回 manifest dict（用于交叉校验）。"""
    run_dir = os.path.dirname(os.path.abspath(trace))
    path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def intervals_ms(frames, limit=0):
    """相邻帧开始时间之差（R15：帧时间看 interval）。"""
    starts = [f["start_ms"] for f in frames]
    if limit > 0:
        starts = starts[:limit]
    return sorted(starts[i] - starts[i - 1] for i in range(1, len(starts)))


def p50(values):
    return values[len(values) // 2] if values else None


def summarize_frames(payload, limit=0):
    game = payload.get("game") or {}
    render = payload.get("render") or {}
    iv = intervals_ms(game.get("frames") or [], limit)
    return {
        "game_frames": game.get("count"),
        "render_frames": render.get("count"),
        "duration_ms": payload.get("duration_ms"),
        "interval_p50_ms": None if p50(iv) is None else round(p50(iv), 4),
        "interval_n": len(iv),
    }


def cross_check(summary, manifest):
    """与 A 路（manifest 的 frames_seen / trace.timer_count）对账。"""
    if not manifest:
        return None
    seen = manifest.get("frames_seen") or {}
    want_game = seen.get("game_frame_track")
    want_render = seen.get("rendering_frame_track")
    got_game, got_render = summary.get("game_frames"), summary.get("render_frames")
    checks = {}
    if want_game is not None:
        checks["game_frames"] = (got_game == want_game, got_game, want_game)
    if want_render is not None:
        checks["render_frames"] = (got_render == want_render, got_render, want_render)
    return checks


def summarize_timerstats(payload):
    """timerstats 摘要：窗口覆盖率（self 合计 / 墙钟）与 Top 行。"""
    totals = payload.get("totals") or {}
    span = float(payload.get("span_s") or 0.0)
    sum_self = float(totals.get("sum_self_s") or 0.0)
    return {
        "timer_count": totals.get("timer_count"),
        "emitted": payload.get("emitted"),
        "span_s": round(span, 6),
        "sum_self_s": round(sum_self, 6),
        "sum_inclusive_s": round(float(totals.get("sum_inclusive_s") or 0.0), 6),
        "coverage_pct": round(100.0 * sum_self / span, 3) if span > 0 else None,
        "thread": payload.get("thread"),
        "top": [r.get("name") for r in (payload.get("timers") or [])[:10]],
    }


def compare_a_route(payload, csv_path, max_rel_pct=0.5):
    """与 A 路同一窗口的导出 CSV 对账。

    注意两个数据源不是一回事：A 路这份 CSV 是 **Stats 区块视图**（SceneRender / Bloom 这类
    stat 分组，可能跨线程合并），B2 是 **CPU timing 时间线的每线程 scope**。所以只对**同名项**
    比数值——同名时两边应当逐位相同；名字集合本身不要求一致。
    """
    import csv  # 局部导入：只有对账时才需要
    a_map = {}
    with io.open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            try:
                a_map[name] = float(row.get("Excl") or 0.0)
            except ValueError:
                continue
    matched = []
    for r in payload.get("timers") or []:
        name = r.get("name")
        if name in a_map:
            matched.append((name, a_map[name], float(r.get("self_s") or 0.0)))
    pairs = [{"name": n, "a_self_s": round(a, 6), "b_self_s": round(b, 6),
              "diff_pct": round(100.0 * (b - a) / a, 4) if a else None}
             for n, a, b in matched]
    worst = max((abs(p["diff_pct"]) for p in pairs if p["diff_pct"] is not None), default=0.0)
    return {
        "a_rows": len(a_map),
        "b_rows": len(payload.get("timers") or []),
        "matched_n": len(pairs),
        "worst_rel_pct": worst,
        "ok": len(pairs) >= 1 and worst <= max_rel_pct,
        "pairs": pairs[:15],
    }


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="B2 深度读取（M4.2）")
    ap.add_argument("mode", choices=("frames", "timers", "timerstats"), help="读取模式")
    ap.add_argument("trace", help="trace.utrace 路径")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--limit", type=int, default=0, help="每个列表最多输出多少条（0 = 全量）")
    ap.add_argument("--start", type=float, default=None, help="timerstats 窗口起点（秒）")
    ap.add_argument("--end", type=float, default=None, help="timerstats 窗口终点（秒）")
    ap.add_argument("--thread", default=None, help="timerstats 的线程名（如 GameThread）")
    ap.add_argument("--compare-csv", default=None, help="与 A 路导出 CSV 对账（timerstats）")
    ap.add_argument("--ue-root", default=None)
    a = ap.parse_args(argv)

    out_dir = os.path.abspath(a.out)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "b2_%s.json" % a.mode)
    ok, text = run_mode(a.mode, a.trace, out_path, a.limit, a.ue_root,
                        start=a.start, end=a.end, thread=a.thread)
    if not ok:
        print("[error] commandlet 失败（看 %s）" % os.path.splitext(out_path)[0] + ".log")
        for line in text.splitlines():
            if "PERF_ERR" in line:
                print("  " + line.strip())
        return 1

    with io.open(out_path, encoding="utf-8") as f:
        payload = json.load(f)

    if a.mode == "frames":
        summary = summarize_frames(payload, a.limit)
        print("[b2] frames: game=%s render=%s duration=%.1fms interval p50=%s (n=%d)"
              % (summary["game_frames"], summary["render_frames"], summary["duration_ms"] or 0.0,
                 summary["interval_p50_ms"], summary["interval_n"]))
        checks = cross_check(summary, manifest_beside(a.trace))
        if checks:
            for name, (same, got, want) in checks.items():
                print("[b2] 对账 %s: B2=%s A路=%s %s" % (name, got, want, "OK" if same else "MISMATCH"))
            if not all(v[0] for v in checks.values()):
                return 3
        elif checks is None:
            print("[b2] 旁边没有 manifest，跳过对账")
    else:
        summary = summarize_timerstats(payload)
        print("[b2] timerstats: thread=%s 窗口 %.3fs，%s 个 timer（输出 %s 行），self 合计 %.3fs（覆盖 %.2f%%）"
              % (summary["thread"], summary["span_s"], summary["timer_count"], summary["emitted"],
                 summary["sum_self_s"], summary["coverage_pct"] or 0.0))
        if summary["top"]:
            print("[b2] Top: %s" % ", ".join(str(x) for x in summary["top"][:5]))
        if a.compare_csv:
            comp = compare_a_route(payload, a.compare_csv)
            print("[b2] 对账 A 路（同名项）：命中 %d 项，最大相对偏差 %.4f%%，判定 %s"
                  % (comp["matched_n"], comp["worst_rel_pct"], "OK" if comp["ok"] else "MISMATCH"))
            for p in comp["pairs"][:5]:
                print("      %-28s A=%.3fs B2=%.3fs Δ=%s%%"
                      % (p["name"][:28], p["a_self_s"], p["b_self_s"],
                         "-" if p["diff_pct"] is None else "%+.4f" % p["diff_pct"]))
            if not comp["ok"]:
                return 3
    print("[ok] %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
