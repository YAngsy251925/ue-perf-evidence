#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api_signatures.py — M4.1：把 ue-trace-mcp 实际调用的 provider 方法，对到 UE 5.8 的声明

api_diff.py 只做名字级对照；这里把「哪个 provider 的哪个方法被调了多少次」抽出来，
再抓 5.8 头文件里的同名声明（连同前面的 UE_DEPRECATED 标记），供逐个核对签名。

用法：python -X utf8 tools/api_signatures.py --mcp DIR [--ue-root DIR] [--out FILE]
"""
import argparse
import collections
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"
BASE = os.path.join("Engine", "Source", "Developer", "TraceServices", "Public", "TraceServices")

# 形如： const ITimingProfilerProvider* Timing = Session.ReadTimingProfilerProvider(...)
RE_PROVIDER_VAR = re.compile(
    r"\b(?:const\s+)?(I[A-Za-z]+Provider)\s*\*\s*(\w+)\s*=\s*[^;]*?(Read[A-Za-z]+Provider)\s*\(")
RE_CALL = re.compile(r"\b(\w+)\s*->\s*([A-Z][A-Za-z0-9_]+)\s*\(")
RE_DECL = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\s*\([^;{]*\)")


def iter_sources(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "Intermediate", "Binaries")]
        for name in filenames:
            if name.endswith((".cpp", ".h")):
                yield os.path.join(dirpath, name)


def collect_calls(mcp_dir):
    """{provider_type: {method: [(file, line), ...]}}"""
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for path in iter_sources(mcp_dir):
        rel = os.path.relpath(path, mcp_dir).replace(os.sep, "/")
        var2type = {}
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                for m in RE_PROVIDER_VAR.finditer(line):
                    var2type[m.group(2)] = m.group(1)
                for m in RE_CALL.finditer(line):
                    var, method = m.group(1), m.group(2)
                    ptype = var2type.get(var)
                    if ptype is None:
                        continue
                    out[ptype][method].append((rel, lineno))
    return out


def load_decls(ue_root):
    """按类体解析：{class_name: {member: (header, line, decl, dep)}}，另返回全局兜底表。

    只按名字全局匹配会把不同类的同名方法对上（例如 EnumerateTimelines 在 StackSamples.h
    里也有一个），所以先按 class 块切，找不到再落到全局表并标注"继承/其他头"。
    """
    base = os.path.join(ue_root, BASE)
    per_class = collections.defaultdict(dict)
    global_decls = collections.defaultdict(list)
    re_class = re.compile(r"\b(?:class|struct)\s+(?:\w+_API\s+)?([A-Za-z_]\w*)\s*(?::[^;{]*)?\{")
    for path in iter_sources(base):
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        text = io.open(path, encoding="utf-8", errors="replace").read()
        lines = text.splitlines()
        for m in re_class.finditer(text):
            name = m.group(1)
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            body = text[m.end():i]
            body_start_line = text.count("\n", 0, m.end())
            for k, line in enumerate(body.splitlines()):
                dm = RE_DECL.search(line)
                if not dm:
                    continue
                member = dm.group(1)
                if member in ("if", "for", "while", "switch", "return"):
                    continue
                dep = ""
                for back in range(max(0, k - 4), k):
                    if "UE_DEPRECATED" in body.splitlines()[back]:
                        mm = re.search(r'UE_DEPRECATED\([^,]+,\s*"([^"]*)"', body.splitlines()[back])
                        dep = mm.group(1) if mm else "deprecated"
                        break
                per_class[name].setdefault(member, (rel, body_start_line + k + 1, line.strip(), dep))
                global_decls[member].append((rel, body_start_line + k + 1, line.strip(), dep))
    return per_class, global_decls


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="M4.1 provider 方法 × 5.8 声明对照")
    ap.add_argument("--mcp", required=True)
    ap.add_argument("--ue-root", default=os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT)
    ap.add_argument("--out", default=os.path.join(REPO, "docs", "UE_TRACE_MCP_SIGNATURES.md"))
    a = ap.parse_args(argv)

    calls = collect_calls(os.path.abspath(a.mcp))
    per_class, global_decls = load_decls(a.ue_root)
    decls = global_decls
    if not calls or not decls:
        print("[error] 收集为空：calls=%d decls=%d" % (sum(len(v) for v in calls.values()), len(decls)))
        return 2

    lines = ["# provider 方法 × UE 5.8 声明（M4.1）", "",
             "由 `python -X utf8 tools/api_signatures.py --mcp <ue-trace-mcp>` 生成。",
             "只看 ue-trace-mcp 真正调到的方法；`两次以上` 的按调用次数排序。", "",
             "| provider.method | 次数 | 5.8 声明（括号内为参数） | 弃用 |", "|---|---|---|---|"]
    missing = 0
    for ptype in sorted(calls, key=lambda p: -sum(len(v) for v in calls[p].values())):
        for method, places in sorted(calls[ptype].items(), key=lambda kv: -len(kv[1])):
            candidates = []
            if method in per_class.get(ptype, {}):
                candidates = [per_class[ptype][method]]
            else:
                candidates = decls.get(method) or []
            if not candidates:
                missing += 1
                lines.append("| `%s::%s` | %d | ❌ 5.8 里找不到同名声明 | - |"
                             % (ptype, method, len(places)))
                continue
            rel, ln, text, dep = candidates[0]
            text = text.replace("|", "\\|")
            lines.append("| `%s::%s` | %d | %s `%s:%d` | %s |"
                         % (ptype, method, len(places), text[:160], rel, ln,
                            ("⚠️ " + dep[:60]) if dep else ""))
    lines.append("")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    total = sum(len(v) for p in calls for v in calls[p].values())
    print("[ok] %s" % a.out)
    print("  provider=%d method=%d 调用点=%d 找不到声明=%d"
          % (len(calls), sum(len(v) for v in calls.values()), total, missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
