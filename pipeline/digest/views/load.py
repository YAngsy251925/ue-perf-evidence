#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""load.py — M6.3：加载视图（包级 Top-N）

数据来源：`perfctl b2 load <trace> --start S --end E`（B2 的 `mode=load`）写出的 `b2_load.json`。
B2 侧读的是 `ILoadTimeProfilerProvider::GetRequestsTable()`（`ITable<FLoadRequest>`）。

**踩过的坑（写在这里免得下次再查一遍）**：`FLoadTimeProfilerModule` 的 `ShouldBeEnabledByDefault()`
返回 false —— 只有 UnrealInsights 会显式 `SetModuleEnabled`。不打开时 provider 永远是 null，
即使 trace 里真有 LoadTime 事件（我们用公开宏造了 45 条事件验证过）。现在命令端统一启用
`TraceModule_LoadTimeProfiler` / `TraceModule_Tasks` / `TraceModule_CookProfiler`。

用法：
  perfctl digest load <run_dir> --out DIR [--start S --end E] [--b2] [--top N]
"""
import argparse
import io
import json
import os
import sys


def build(run_dir, top=20):
    result = {"schema": "perf-load/0",
              "run_dir": os.path.abspath(run_dir).replace(os.sep, "/"),
              "window": None, "request_rows": None, "requests_in_window": None,
              "package_count": None, "file_activity_rows": None,
              "packages": [], "warnings": []}
    path = os.path.join(run_dir, "b2", "b2_load.json")
    if not os.path.isfile(path):
        result["warnings"].append("没有 b2/b2_load.json（先跑 perfctl b2 load，或加 --b2）")
        return result
    with io.open(path, encoding="utf-8") as f:
        payload = json.load(f)
    for key in ("window", "request_rows", "requests_in_window", "package_count", "file_activity_rows"):
        result[key] = payload.get(key)
    result["packages"] = (payload.get("packages") or [])[:top]
    if not result["request_rows"]:
        result["warnings"].append(
            "trace 里没有 LoadTime 事件：profile 的 trace.channels 要含 loadtime"
            "（模块已自动启用，缺的是数据）")
    return result


def render_md(r):
    lines = ["# 加载视图 — %s" % r["run_dir"], ""]
    if r.get("window"):
        lines.append("窗口：%.3f – %.3f s" % (float(r["window"][0]), float(r["window"][1])))
        lines.append("")
    if r.get("request_rows"):
        lines += ["| 项 | 值 |", "|---|---|",
                  "| 加载请求（表内 / 窗口内） | %s / %s |" % (r["request_rows"], r["requests_in_window"]),
                  "| 不同条目 | %s |" % r["package_count"],
                  "| 文件活动行 | %s |" % r.get("file_activity_rows"),
                  ""]
        lines += ["## 按总耗时 Top %d" % len(r["packages"]), "",
                  "| 条目 | 次数 | 总耗时 ms | 最长 ms | 平均 ms |", "|---|---|---|---|---|"]
        for p in r["packages"]:
            lines.append("| %s | %s | %.1f | %.1f | %.1f |"
                         % (str(p["name"])[:56], p["count"], p["total_ms"], p["max_ms"], p["avg_ms"]))
        lines.append("")
    if r.get("warnings"):
        lines += ["## 警告"] + ["- %s" % w for w in r["warnings"]] + [""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="加载视图（M6.3）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--b2", action="store_true", help="没有缓存时现场跑 b2 load")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    cache = os.path.join(run_dir, "b2", "b2_load.json")
    if a.b2 and not os.path.isfile(cache):
        from pipeline.extract import b2
        start, end = a.start, a.end
        if start is None or end is None:
            manifest_path = os.path.join(run_dir, "manifest.json")
            if os.path.isfile(manifest_path):
                with io.open(manifest_path, encoding="utf-8") as f:
                    m = json.load(f)
                basis = (m.get("window_basis") or {}).get("stats_window") or "stats"
                win = (m.get("windows") or {}).get(basis)
                if win:
                    start = win[0] if start is None else start
                    end = win[1] if end is None else end
        ok, _text = b2.run_mode("load", os.path.join(run_dir, "trace.utrace"), cache,
                                limit=40, start=start, end=end)
        if not ok:
            print("[error] b2 load failed（见 b2/b2_load.log）")
            return 1

    result = build(run_dir, a.top)
    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "load.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "load.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")

    top = result["packages"][:3]
    print("[load] requests=%s entries=%s top=%s"
          % (result["request_rows"], result["package_count"],
             ", ".join("%s %.0fms" % (str(p["name"])[:30], p["total_ms"]) for p in top)))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "load.md"))
    return 0 if result["request_rows"] else 3


if __name__ == "__main__":
    sys.exit(main())
