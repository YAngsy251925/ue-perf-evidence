#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ingest.py — 把**外部采集**的产物接进管线（PLAN §3 的 perf_capture 另一半）

为什么要有它：`capture` 只是三种 producer 之一（编辑器二进制跑 `-game`）。真实开发里更常见的是：

  ① **PIE**：在编辑器里 PIE，用 Insights 的 Trace 按钮/控制台 `Trace.Start` 录
     —— 产物是 <Project>/Saved/Profiling/*.utrace + <Project>/Saved/Logs/*.log；
  ② **打包后的包**：`Game.exe -trace=cpu,frame,gpu -tracefile=... -stdout -ABSLOG=...`
     —— 产物同样是一份 .utrace + 一份日志。

这两种都不需要（也不应该）由我们驱动引擎。`ingest` 只做"把已有产物整理成 run 目录 + 建 manifest"，
之后的 digest / diff / evidence 全部照常工作。

与 capture 的差别写进 manifest：`capture_mode: external`，并且**不做条件强制**——
`conditions.verified` 会是 0，视图里会明确提示"这次 run 的条件没有由本工具强制"。
"""
import io
import json
import os
import shutil
import sys

SCHEMA = "perf-capture-profile/0"


def _copy(src, dest):
    if os.path.abspath(src) == os.path.abspath(dest):
        return dest
    shutil.copy2(src, dest)
    return dest


def build_snapshot(uproject=None, note=None, capture_mode="external", extras=None):
    """外部采集的 capture_profile 存根：条件全空（未强制），只记录来源信息。"""
    snapshot = {
        "schema": SCHEMA,
        "profile_name": capture_mode,
        "profile_class": capture_mode,
        "capture_mode": capture_mode,
        "profile_sha256": None,      # 外部采集没有 profile 文件可算哈希
        "profile_file": None,
        "uproject": uproject,
        # conditions.verify() 会读 profile.rhi / cvars / window / trace 等字段；
        # 外部采集我们不知道这些声明，用"未强制"的空声明（rhi=unknown → 只报能验证的项）
        "profile": {"name": capture_mode, "class": capture_mode, "conditions": {},
                    "cvars": {}, "rhi": "unknown", "window": None,
                    "trace": {"channels": []}, "exec_cmds": []},
        "note": note or ("外部采集：产物由编辑器 PIE / Insights 或打包包写出，采集条件未由本工具强制"),
    }
    if extras:
        snapshot.update(extras)
    return snapshot


def ingest(run_dir, trace, log=None, uproject=None, note=None, capture_mode="external", extras=None):
    """把 trace（+日志）放进 run 目录并写 capture_profile 存根。返回 {run_dir, trace, log, snapshot}。"""
    run_dir = os.path.abspath(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    if not os.path.isfile(trace):
        raise FileNotFoundError("trace not found: %s" % trace)

    target_trace = _copy(trace, os.path.join(run_dir, "trace.utrace"))
    target_log = None
    if log:
        if not os.path.isfile(log):
            raise FileNotFoundError("log not found: %s" % log)
        target_log = _copy(log, os.path.join(run_dir, "engine.log"))

    snapshot_path = os.path.join(run_dir, "capture_profile.json")
    snapshot = build_snapshot(uproject, note, capture_mode, extras)
    if os.path.isfile(snapshot_path):
        # 已有（repeat 采集 / 二次 ingest）：保留原 profile，不覆盖
        with io.open(snapshot_path, encoding="utf-8") as f:
            snapshot = json.load(f)
    else:
        with io.open(snapshot_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
    return {"run_dir": run_dir, "trace": target_trace, "log": target_log, "snapshot": snapshot}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    import argparse
    ap = argparse.ArgumentParser(
        description="把外部采集（PIE/Insights 录制的 .utrace，或打包包写出的 .utrace）接进管线")
    ap.add_argument("--trace", required=True, help=".utrace 路径（Insights 录制或 -tracefile 产出的）")
    ap.add_argument("--run", required=True, help="run 目录（不存在会创建）")
    ap.add_argument("--log", default=None, help="引擎日志（PIE: Saved/Logs/*.log；打包：-ABSLOG 指的那份）")
    ap.add_argument("--uproject", default=None, help="工程路径（记录用；打包包可省略）")
    ap.add_argument("--mode", default="external", choices=("external", "pie", "packaged"),
                    help="来源标记：pie / packaged / external（默认）")
    ap.add_argument("--note", default=None, help="备注（会写进 manifest 的 capture 块）")
    ap.add_argument("--no-manifest", action="store_true", help="只整理目录，不建 manifest")
    ap.add_argument("--ue-root", default=None)
    a = ap.parse_args(argv)

    try:
        result = ingest(a.run, a.trace, a.log, a.uproject, a.note, a.mode)
    except FileNotFoundError as exc:
        print("[error] %s" % exc)
        return 1
    print("[ingest] run=%s trace=%s log=%s mode=%s"
          % (os.path.abspath(a.run), os.path.basename(result["trace"]),
             os.path.basename(result["log"]) if result["log"] else "（无，部分口径会降级）", a.mode))
    if a.no_manifest:
        return 0

    from pipeline.caliber import manifest as manifest_mod
    rc = manifest_mod.main((["--ue-root", a.ue_root] if a.ue_root else []) + [a.run])
    if rc != 0:
        return rc
    print("[ingest] 下一步：python perfctl.py digest cpu %s --out %s"
          % (a.run, os.path.join(a.run, "digest_cpu")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
