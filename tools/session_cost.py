#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session_cost.py — 从 DSH 会话日志读出真实的 token 用量（perf 分析的"成本"）

DSH 把每步的 usage 记在 `~/.dsh/sessions/--<cwd>--/session-*/session.v4.jsonl.zstd`（zstd 压缩的 JSONL），
字段是 `data.usage.{inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, totalTokens}`，
`data.turn` / `data.step` 给归属。这个脚本把压缩流解开、按回合汇总。

为什么关心 cacheRead：DSH 每一步都会把整段上下文重发一遍（prompt cache），所以
`cacheReadTokens` 会随对话变长而线性膨胀；`inputTokens` 只是"新增且未命中缓存"的部分。
比"总 token"更有意义的是：**单次分析真正新增的 output + 未命中缓存的 input**。

用法：
  python tools/session_cost.py                    # 当前工程最近的会话，按回合汇总
  python tools/session_cost.py --session <dir>    # 指定会话目录
  python tools/session_cost.py --turn 40          # 只看某一回合
  python tools/session_cost.py --root ~/.dsh/sessions
"""
import argparse
import glob
import io
import json
import os
import sys


def find_session(root=None, project=None):
    root = os.path.expanduser(root or "~/.dsh/sessions")
    if project:
        dirs = [os.path.join(root, "--%s--" % project.replace("/", "-").replace(":", "-"))]
    else:
        dirs = [d for d in glob.glob(os.path.join(root, "--*--")) if os.path.isdir(d)]
    best = None
    for d in dirs:
        for path in glob.glob(os.path.join(d, "session-*", "session.v4.jsonl.zstd")):
            mtime = os.path.getmtime(path)
            if best is None or mtime > best[0]:
                best = (mtime, path)
    return best[1] if best else None


def read_records(path):
    try:
        import zstandard as zstd
    except ImportError:
        sys.exit("[error] 需要 zstandard：pip install zstandard")
    raw = open(path, "rb").read()
    text = zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read().decode("utf-8", "replace")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def aggregate(records):
    per_turn = {}
    for r in records:
        data = r.get("data") or {}
        usage = data.get("usage")
        if not usage:
            continue
        turn = data.get("turn")
        turn = -1 if turn is None else int(turn)
        slot = per_turn.setdefault(turn, {"steps": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "total": 0})
        slot["steps"] += 1
        slot["in"] += usage.get("inputTokens") or 0
        slot["out"] += usage.get("outputTokens") or 0
        slot["cr"] += usage.get("cacheReadTokens") or 0
        slot["cw"] += usage.get("cacheWriteTokens") or 0
        slot["total"] += usage.get("totalTokens") or 0
    return per_turn


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="从 DSH 会话日志读 token 用量")
    ap.add_argument("--session", default=None, help="会话目录或 session.v4.jsonl.zstd 路径")
    ap.add_argument("--root", default=None, help="会话根目录（默认 ~/.dsh/sessions）")
    ap.add_argument("--project", default=None, help="只找该工程（cwd 转义后的目录名）")
    ap.add_argument("--turn", type=int, default=None, help="只看某一回合")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    path = a.session
    if path and os.path.isdir(path):
        cands = glob.glob(os.path.join(path, "session.v4.jsonl.zstd")) or \
            glob.glob(os.path.join(path, "session-*", "session.v4.jsonl.zstd"))
        path = cands[0] if cands else None
    path = path or find_session(a.root, a.project)
    if not path or not os.path.isfile(path):
        print("[error] 找不到会话日志（用 --session 指定）")
        return 1

    per_turn = aggregate(read_records(path))
    if not per_turn:
        print("[error] 日志里没有 usage 记录（%s）" % path)
        return 1
    if a.turn is not None:
        per_turn = {k: v for k, v in per_turn.items() if k == a.turn}

    if a.json:
        print(json.dumps({"session": path.replace(os.sep, "/"),
                          "turns": {("init" if k == -1 else k): v for k, v in per_turn.items()},
                          "total": {key: sum(v[key] for v in per_turn.values())
                                    for key in ("steps", "in", "out", "cr", "cw", "total")}},
                         ensure_ascii=False, sort_keys=True))
        return 0

    print("session: %s" % path)
    print("%-5s %6s %10s %12s %10s %11s" % ("turn", "steps", "input", "cacheRead", "output", "total"))
    for turn in sorted(per_turn):
        s = per_turn[turn]
        print("%-5s %6d %10d %12d %10d %11d"
              % ("init" if turn == -1 else turn, s["steps"], s["in"], s["cr"], s["out"], s["total"]))
    tot = {key: sum(v[key] for v in per_turn.values()) for key in ("steps", "in", "out", "cr", "cw", "total")}
    print("%-5s %6d %10d %12d %10d %11d"
          % ("合计", tot["steps"], tot["in"], tot["cr"], tot["out"], tot["total"]))
    print("\n口径：input=未命中缓存的新增输入，cacheRead=重发历史上下文（缓存命中，单价低），"
          "output=模型输出。看单次分析成本，用 output + input 更贴近真实增量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
