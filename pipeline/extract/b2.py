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
CMDLET_BAT = os.path.join(REPO, "host", "TraceDigestProbe", "run_cmdlet.bat")
DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"


def run_mode(mode, trace, out_path, limit=0, ue_root=None, timeout=600):
    """跑一次 commandlet，返回 (ok, 日志文本)。日志里的 PERF_ 行是唯一的成功标志。"""
    trace = os.path.abspath(trace)
    out_path = os.path.abspath(out_path)
    if not os.path.isfile(trace):
        return False, "trace not found: %s" % trace
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    log_path = os.path.splitext(out_path)[0] + ".log"
    extra = ["mode=%s" % mode, "out=%s" % out_path]
    if limit > 0:
        extra.append("limit=%d" % limit)
    if ue_root:
        os.environ.setdefault("UE_ROOT", ue_root)
    # cmd /c 对"首个 token 带引号且后面还有引号参数"的解析不可靠（实测 bat 根本没被调用），
    # 所以落一个一次性的 bat 再执行——和手工验证过的调用方式一致。
    bat = os.path.splitext(out_path)[0] + ".bat"
    line = " ".join(['"%s"' % CMDLET_BAT, "TraceDigestPerf", '"%s"' % trace, '"%s"' % log_path]
                    + extra)
    with io.open(bat, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\r\ncall %s\r\n" % line)
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


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="B2 深度读取（M4.2）")
    ap.add_argument("mode", choices=("frames", "timers"), help="读取模式")
    ap.add_argument("trace", help="trace.utrace 路径")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--limit", type=int, default=0, help="每个列表最多输出多少条（0 = 全量）")
    ap.add_argument("--ue-root", default=None)
    a = ap.parse_args(argv)

    out_dir = os.path.abspath(a.out)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "b2_%s.json" % a.mode)
    ok, text = run_mode(a.mode, a.trace, out_path, a.limit, a.ue_root)
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
        print("[b2] timers: %d 个" % payload.get("timer_count", 0))
        manifest = manifest_beside(a.trace)
        want = (manifest or {}).get("trace", {}).get("timer_count")
        if want is not None:
            got = payload.get("timer_count")
            print("[b2] 对账 timer_count: B2=%s A路=%s %s"
                  % (got, want, "OK" if got == want else "MISMATCH"))
            if got != want:
                return 3
    print("[ok] %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
