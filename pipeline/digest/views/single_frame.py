#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""single_frame.py — M5.4：单帧 scope 树视图

PLAN 原来写的是「dumpframe / dumpevents 解析器」，但 **UE 5.8 没有这两个命令**
（`Engine/Source` 全量 grep 与 `UnrealEditor-*.dll` 的 strings 都查不到），所以改走自己的数据面：
`perfctl b2 frame <trace> --frame N --thread T` 用 IFrameProvider 定位第 N 帧，再在该线程的
CPU timeline 上按深度栈把这一帧的 scope 树原样读出来（name / depth / begin / duration）。

这个视图负责：树形渲染 + **自耗时**（duration − 子节点 duration 之和）+ 名字覆盖率
（`STAT_` 前缀的比例，越低说明桥接描述名越完整）。

用法：
  perfctl digest single_frame <run_dir> --frame N --out DIR [--thread GameThread] [--limit 80] [--b2]
"""
import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def self_times(scopes):
    """在一棵按 (begin, depth) 排好序的 scope 列表上算自耗时。

    输入每项有 depth / duration_ms；父节点是"下一个更浅的、且它的 begin 落在父区间内"的节点。
    这里用栈：遇到 depth 更浅的就收尾；同 depth 的兄弟不影响对方。
    返回 (self_ms 列表, 求和)。
    """
    stack = []
    self_ms = [0.0] * len(scopes)
    for i, sc in enumerate(scopes):
        depth = int(sc.get("depth") or 0)
        dur = float(sc.get("duration_ms") or 0.0)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            self_ms[parent] = self_ms[parent] - dur
        self_ms[i] += dur
        stack.append((depth, i))
    return self_ms


def top_self(scopes, self_ms, limit=15):
    rows = []
    for sc, own in zip(scopes, self_ms):
        if own <= 0.0:
            continue
        rows.append({"name": sc.get("name"), "depth": sc.get("depth"),
                     "self_ms": round(own, 4), "duration_ms": round(float(sc.get("duration_ms") or 0.0), 4)})
    rows.sort(key=lambda r: -r["self_ms"])
    return rows[:limit]


def render_tree(scopes, limit=80):
    lines = []
    for i, sc in enumerate(scopes[:limit]):
        depth = int(sc.get("depth") or 0)
        name = str(sc.get("name") or "")
        if len(name) > 60:
            name = name[:57] + "..."
        lines.append("%s%s  %.3fms" % ("  " * depth, name, float(sc.get("duration_ms") or 0.0)))
    if len(scopes) > limit:
        lines.append("...（还有 %d 个 scope，用 --limit 放大）" % (len(scopes) - limit))
    return lines


def render_md(payload, thread="GameThread", limit=80, top=15):
    frame = payload.get("frame") or {}
    scopes = payload.get("scopes") or []
    self_ms = self_times(scopes)
    lines = ["# 单帧 scope 树 — frame %s（%s）" % (frame.get("index"), thread), "",
             "| 项 | 值 |", "|---|---|",
             "| 帧区间 | %.4f – %.4f s |" % (float(frame.get("start_s") or 0.0), float(frame.get("end_s") or 0.0)),
             "| 帧时长 | %.3f ms |" % float(frame.get("duration_ms") or 0.0),
             "| scope 数 | %s（本视图展示 %s） |" % (payload.get("scope_count"), min(len(scopes), limit)),
             "| 名字覆盖率 | STAT_ 前缀 %s 个（%.1f%%）——越低说明桥接描述名越完整 |"
             % (payload.get("stat_named"), float(payload.get("stat_named_pct") or 0.0)),
             "| self 合计 | %.3f ms（占帧 %.1f%%） |"
             % (sum(self_ms), 100.0 * sum(self_ms) / max(1e-9, float(frame.get("duration_ms") or 1.0))),
             ""]
    lines += ["## 自耗时 Top %d" % top, "", "| scope | depth | self (ms) | duration (ms) |", "|---|---|---|---|"]
    for row in top_self(scopes, self_ms, top):
        lines.append("| %s | %s | %.3f | %.3f |"
                     % (str(row["name"])[:60], row["depth"], row["self_ms"], row["duration_ms"]))
    lines += ["", "## 树（前 %d 个，按 begin/depth 排序）" % limit, "", "```"] + render_tree(scopes, limit) + ["```", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="单帧 scope 树（M5.4）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--frame", type=int, default=None, help="帧序号（默认取 stats 窗口的中间帧）")
    ap.add_argument("--thread", default="GameThread")
    ap.add_argument("--limit", type=int, default=80, help="树展示多少个 scope")
    ap.add_argument("--b2", action="store_true", help="缓存缺失时实时跑 b2 frame")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    manifest_path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print("[error] manifest not found: %s" % manifest_path)
        return 1
    with io.open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    frame_index = a.frame
    if frame_index is None:
        # 默认：用窗口内第一帧的绝对序号（frame_tracks 的 track_index + 窗口起点）
        win = manifest.get("windows", {}).get(manifest.get("window_basis", {}).get("stats_window") or "stats")
        frames = ((manifest.get("frame_semantics") or {}).get("game") or {})
        first = (frames.get("track_index") or {}).get("first") or 0
        window_frames = frames.get("window_frames") or 0
        frame_index = int(first + max(0, window_frames // 2))
        if win:
            print("[single_frame] --frame 未给，取窗口中间帧 %d" % frame_index)

    cache = os.path.join(run_dir, "b2", "b2_frame_%d_%s.json" % (frame_index, a.thread))
    if not os.path.isfile(cache):
        if not a.b2:
            print("[error] 没有缓存：%s（加 --b2 让它现场跑）" % cache)
            return 1
        from pipeline.extract import b2
        ok, _text = b2.run_mode("frame", os.path.join(run_dir, "trace.utrace"), cache,
                                limit=0, thread=a.thread, frame=frame_index)
        if not ok:
            print("[error] b2 frame 失败（见 %s）" % os.path.splitext(cache)[0] + ".log")
            return 1

    with io.open(cache, encoding="utf-8") as f:
        payload = json.load(f)
    md = render_md(payload, a.thread, a.limit)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "single_frame.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(md + "\n")
    with io.open(os.path.join(a.out, "single_frame.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"schema": "perf-single-frame/0", "frame": payload.get("frame"),
                   "thread": a.thread, "scope_count": payload.get("scope_count"),
                   "stat_named_pct": payload.get("stat_named_pct"),
                   "top_self": top_self(payload.get("scopes") or [],
                                        self_times(payload.get("scopes") or []), 20)},
                  f, ensure_ascii=False, indent=1)
    top = top_self(payload.get("scopes") or [], self_times(payload.get("scopes") or []), 3)
    print("[single_frame] frame=%s scopes=%s STAT_%s%% top_self=%s"
          % (payload.get("frame", {}).get("index"), payload.get("scope_count"),
             payload.get("stat_named_pct"),
             ", ".join("%s %.2fms" % (t["name"][:24], t["self_ms"]) for t in top)))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "single_frame.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
