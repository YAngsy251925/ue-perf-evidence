#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.py — M5.5：cvar / scalability 快照（从 run 自己的日志里取，不重跑引擎）

回答两个问题：
  1. 这次 run 实际生效的配置是什么？（引擎启动期 `LogConfig: Set CVar [[name:value]]`，
     以及 `Applying CVar settings from Section [X@3] File [Scalability]` 的 scalability 档位）
  2. 和 profile 声明的有没有漂移？（声明在 `capture_profile.json` 的 profile.cvars 里）

只读 run 目录，输出 JSON + Markdown。声明了却没生效、或引擎后来改掉的，都单列出来。
"""
import argparse
import io
import json
import os
import re
import sys

RE_CVAR = re.compile(r"Set CVar \[\[([^:\]]+):([^\]]*)\]\]")
RE_SCALABILITY = re.compile(r"Applying CVar settings from Section \[(\w+)@(\d+)\] File \[Scalability\]")
RE_DEVICE_PROFILE = re.compile(r"Selected Device Profile: \[([^\]]+)\]")
RE_EXEC_CMDS = re.compile(r'-ExecCmds="([^"]*)"')


def read_log(path):
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def parse_exec_cmds(log_text):
    """从命令行证据里取 -ExecCmds="a 1,b 2"：这些 cvar 不走 LogConfig，不会留下 Set CVar 行。

    返回 {name: (value, raw_token)}。
    """
    out = {}
    for m in RE_EXEC_CMDS.finditer(log_text):
        for token in m.group(1).split(","):
            token = token.strip()
            if not token:
                continue
            parts = token.split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = (parts[1].strip(), token)
    return out


def parse(log_text, declared=None):
    """返回 {cvars, exec_cvars, scalability, device_profile, declared_drift}"""
    ordered = []          # [(name, value)] 按出现顺序
    for m in RE_CVAR.finditer(log_text):
        ordered.append((m.group(1), m.group(2).strip()))
    scalability = {}
    for m in RE_SCALABILITY.finditer(log_text):
        scalability[m.group(1)] = int(m.group(2))
    device = None
    m = RE_DEVICE_PROFILE.search(log_text)
    if m:
        device = m.group(1)

    cvars = {}
    for name, value in ordered:
        cvars[name] = value          # 后面的覆盖前面的（引擎确实会改）
    exec_cvars = parse_exec_cmds(log_text)
    declared = declared or {}
    drift = {}
    for name, value in declared.items():
        # 两个证据源：命令行的 -ExecCmds（帧 0 执行、不留 Set CVar 行）与 LogConfig 的 Set CVar
        got, source = cvars.get(name), "logconfig"
        if name in exec_cvars:
            got, source = exec_cvars[name][0], "exec_cmds"
        if got is None:
            drift[name] = {"declared": value, "observed": None, "status": "missing", "source": None}
        elif str(got).strip().lower() != str(value).strip().lower():
            drift[name] = {"declared": value, "observed": got, "status": "changed", "source": source}
        else:
            drift[name] = {"declared": value, "observed": got, "status": "ok", "source": source}
    return {"cvars": cvars, "exec_cvars": {k: v[0] for k, v in exec_cvars.items()},
            "assignments": len(ordered), "scalability": scalability,
            "device_profile": device, "declared_drift": drift}


def load_declared(run_dir):
    path = os.path.join(run_dir, "capture_profile.json")
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as f:
        snapshot = json.load(f)
    profile = snapshot.get("profile") or snapshot
    return (profile.get("cvars") or {}), snapshot


def render_md(result):
    lines = ["# 配置快照 — %s" % result["run_dir"], "",
             "| 项 | 值 |", "|---|---|",
             "| Device Profile | %s |" % (result.get("device_profile") or "-"),
             "| LogConfig CVar 赋值 | %s 条 |" % result.get("assignments"),
             "| 命令行 ExecCmds 设置 | %s 条 |" % len(result.get("exec_cvars") or {}),
             "| 声明项 | %s |" % len(result.get("declared_drift") or {}),
             ""]
    drift = result.get("declared_drift") or {}
    if drift:
        lines += ["## 声明 vs 实际", "", "| cvar | 声明 | 实际 | 判定 |", "|---|---|---|---|"]
        for name in sorted(drift):
            d = drift[name]
            lines.append("| `%s` | %s | %s | **%s**（%s） |"
                         % (name, d["declared"], "-" if d["observed"] is None else d["observed"],
                            d["status"], d.get("source") or "-"))
        lines.append("")
    scal = result.get("scalability") or {}
    if scal:
        lines += ["## Scalability 档位", "", "| 组 | 档位 |", "|---|---|"]
        for name in sorted(scal):
            lines.append("| %s | %s |" % (name, scal[name]))
        lines.append("")
    cvars = result.get("cvars") or {}
    lines += ["## 生效的 CVar（共 %d 条，展示前 30 条）" % len(cvars), "",
              "| cvar | 值 |", "|---|---|"]
    for name in sorted(cvars)[:30]:
        lines.append("| `%s` | `%s` |" % (name, cvars[name]))
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="cvar / scalability 快照（M5.5）")
    ap.add_argument("run_dir")
    ap.add_argument("--out", required=True, help="输出目录（必填，P7）")
    ap.add_argument("--log", default=None, help="日志路径（默认 <run_dir>/engine.log）")
    a = ap.parse_args(argv)

    run_dir = os.path.abspath(a.run_dir)
    log_path = a.log or os.path.join(run_dir, "engine.log")
    if not os.path.isfile(log_path):
        print("[error] log not found: %s" % log_path)
        return 1
    declared, snapshot = load_declared(run_dir)
    parsed = parse(read_log(log_path), declared)
    result = {"schema": "perf-config/0",
              "run_dir": run_dir.replace(os.sep, "/"),
              "log": os.path.basename(log_path),
              **parsed}
    if snapshot:
        result["profile_name"] = snapshot.get("profile_name")

    os.makedirs(a.out, exist_ok=True)
    with io.open(os.path.join(a.out, "config.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with io.open(os.path.join(a.out, "config.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(result) + "\n")

    bad = [n for n, d in (result["declared_drift"] or {}).items() if d["status"] != "ok"]
    print("[config] device_profile=%s cvars=%d scalability=%d 声明漂移=%d%s"
          % (result.get("device_profile"), len(result["cvars"]), len(result.get("scalability") or {}),
             len(bad), (": " + ", ".join(sorted(bad))) if bad else ""))
    print("[ok] %s" % os.path.join(os.path.abspath(a.out), "config.md"))
    return 0 if not bad else 3


if __name__ == "__main__":
    sys.exit(main())
