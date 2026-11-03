#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api_diff.py — M4.1：把 ue-trace-mcp 用到的 TraceServices 符号与 UE 5.8 头文件逐个对照

用法（仓库根目录）：
  python -X utf8 tools/api_diff.py --mcp DIR [--ue-root DIR] [--out FILE]

做的事：
  1) 从 ue-trace-mcp 源码里抽：TraceServices include、I*Provider 类型、Read* 读取函数、
     Read*Provider 访问函数（每条带文件:行号）；
  2) 在 5.8 的 Engine/Source/Developer/TraceServices/Public 头文件里找同名符号（类型按
     class/struct 声明找，函数按名字找并给出现次数）；
  3) 输出 Markdown 差异清单：符号 | 出现次数 | 5.8 是否有 | 示例位置。
       - 缺失的（❌）是移植时真正要改的地方；
       - 存在的（✅）也可能签名变了，M4.2 用编译器确认（本脚本只做名字级对照）。

只读文件，不写除 --out 之外的东西。
"""
import argparse
import collections
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"
SUBDIR = os.path.join("Engine", "Source", "Developer", "TraceServices", "Public")

SRC_EXT = (".cpp", ".h")
RE_INCLUDE = re.compile(r'#include\s+"(TraceServices/[^"]+)"')
RE_PROVIDER_TYPE = re.compile(r"\bI[A-Za-z]+Provider\b")
RE_READ_CALL = re.compile(r"\b(Read(?:All|Timeline|Timers|AllTimed|Connections|Objects|GameInstances|Allocations)\w*)\s*\(")
RE_READ_PROVIDER = re.compile(r"\b(Read[A-Za-z]+Provider)\s*\(")
RE_METHOD = re.compile(r"->\s*([A-Z][A-Za-z0-9_]+)\s*\(")


def iter_sources(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "Intermediate", "Binaries")]
        for name in filenames:
            if name.endswith(SRC_EXT):
                yield os.path.join(dirpath, name)


def collect(mcp_dir):
    """返回 {kind: {symbol: [(relpath, lineno), ...]}}。"""
    hits = collections.defaultdict(lambda: collections.defaultdict(list))
    for path in iter_sources(mcp_dir):
        rel = os.path.relpath(path, mcp_dir).replace("\\", "/")
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                for m in RE_INCLUDE.finditer(line):
                    hits["include"][m.group(1)].append((rel, lineno))
                for m in RE_PROVIDER_TYPE.finditer(line):
                    hits["provider"][m.group(0)].append((rel, lineno))
                for m in RE_READ_PROVIDER.finditer(line):
                    hits["read_provider"][m.group(1)].append((rel, lineno))
                for m in RE_READ_CALL.finditer(line):
                    hits["read_call"][m.group(1)].append((rel, lineno))
                for m in RE_METHOD.finditer(line):
                    hits["method"][m.group(1)].append((rel, lineno))
    return hits


def load_headers(ue_root):
    """把 5.8 TraceServices 头文件读进来，返回 (headers{rel: text}, concat_text)。"""
    base = os.path.join(ue_root, SUBDIR)
    headers = {}
    for path in iter_sources(base):
        with io.open(path, encoding="utf-8", errors="replace") as f:
            headers[os.path.relpath(path, base).replace("\\", "/")] = f.read()
    return headers, "\n".join(headers.values())


def has_symbol(concat, kind, symbol):
    """名字级判定：include 看路径是否在头文件列表里，类型看声明，函数看调用点。"""
    if kind == "include":
        return symbol in concat or True  # include 由 header 列表单独比对
    if kind == "provider":
        return re.search(r"\b(?:class|struct)\s+%s\b" % re.escape(symbol), concat) is not None
    if kind in ("read_call", "read_provider", "method"):
        return re.search(r"\b%s\b" % re.escape(symbol), concat) is not None
    return False


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="M4.1 ue-trace-mcp × UE 5.8 API 对照")
    ap.add_argument("--mcp", required=True, help="ue-trace-mcp 源码目录")
    ap.add_argument("--ue-root", default=os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT)
    ap.add_argument("--out", default=os.path.join(REPO, "docs", "UE_TRACE_MCP_API_DIFF.md"))
    a = ap.parse_args(argv)

    mcp_dir = os.path.abspath(a.mcp)
    if not os.path.isdir(mcp_dir):
        print("[error] mcp dir not found: %s" % mcp_dir)
        return 2
    headers, concat = load_headers(a.ue_root)
    if not headers:
        print("[error] 5.8 TraceServices 头文件没找到：%s" % os.path.join(a.ue_root, SUBDIR))
        return 2

    hits = collect(mcp_dir)
    header_paths = set(headers)

    lines = ["# ue-trace-mcp × UE 5.8 TraceServices API 对照（M4.1）", "",
             "由 `python -X utf8 tools/api_diff.py --mcp <ue-trace-mcp 目录>` 生成；"
             "对照源是 UE 5.8 安装版的 `Engine/Source/Developer/TraceServices/Public`（%d 个头文件）。" % len(headers),
             "",
             "判定口径：**名字级**。✅ = 5.8 头文件里能找到同名声明；❌ = 找不到，M4.2 必须改写；"
             "⚠️ 名字在但签名/语义可能变（本脚本不判签名），由 M4.2 编译逐个确认；"
             "include 行单独按路径比对。", ""]

    def emit(kind, title, verdict_fn):
        lines.append("## %s" % title)
        lines.append("")
        lines.append("| 符号 | 次数 | 5.8 | 例（ue-trace-mcp） |")
        lines.append("|---|---|---|---|")
        for symbol, places in sorted(hits[kind].items(), key=lambda kv: -len(kv[1])):
            verdict, note = verdict_fn(symbol)
            first = "%s:%d" % places[0] if places else "-"
            lines.append("| `%s` | %d | %s | %s |" % (symbol, len(places), verdict, first))
        lines.append("")

    emit("include", "TraceServices include",
         lambda s: ("✅" if s in header_paths else "❌ 路径变了", ""))
    emit("provider", "Provider 类型",
         lambda s: ("✅" if has_symbol(concat, "provider", s) else "❌ 不存在", ""))
    emit("read_provider", "会话访问函数（Read*Provider）",
         lambda s: ("✅" if has_symbol(concat, "read_provider", s) else "❌ 不存在", ""))
    emit("read_call", "数据读取函数（Read*）",
         lambda s: ("⚠️ 名字在" if has_symbol(concat, "read_call", s) else "❌ 不存在", ""))
    methods = {k: v for k, v in hits["method"].items() if len(v) >= 3}
    lines.append("## 高频成员函数（≥3 次出现，用于 M4.2 逐个编译确认）")
    lines.append("")
    lines.append("| 成员 | 次数 | 5.8 | 例 |")
    lines.append("|---|---|---|---|")
    for symbol, places in sorted(methods.items(), key=lambda kv: -len(kv[1])):
        verdict = "⚠️ 名字在" if has_symbol(concat, "method", symbol) else "❌ 不存在"
        lines.append("| `%s` | %d | %s | %s:%d |" % (symbol, len(places), verdict, places[0][0], places[0][1]))
    lines.append("")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    missing = sum(1 for s in hits["provider"] if not has_symbol(concat, "provider", s))
    missing += sum(1 for s in hits["read_provider"] if not has_symbol(concat, "read_provider", s))
    print("[ok] %s" % a.out)
    print("  include=%d provider=%d read_provider=%d read_call=%d method(≥3)=%d；缺失 provider/read_provider=%d"
          % (len(hits["include"]), len(hits["provider"]), len(hits["read_provider"]),
             len(hits["read_call"]), len(methods), missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
