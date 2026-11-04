#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metric_json.py — 读 B2 导出的 JSON 时的容错（PLAN R13 的落地一半）

背景（2026-11-05 实测）：编辑器 trace 末尾常留一个**未闭合 scope**，`End - Start` 会算出
inf/NaN；MSVC 的 printf 把 NaN 打成 `-nan(ind)`、inf 打成 `inf` —— 那不是合法 JSON，
`json.load` 直接抛 `JSONDecodeError`（我们的 timerstats 就在 458MB 的编辑器 trace 上炸过）。

C++ 侧已经改成"非有限值记 0"（`Fin()`），但**旧产物和别人给的 JSON** 仍可能带这些字面量，
所以读取端也做一次清洗：把 `-nan(ind)` / `nan` / `inf` / `-inf` 换成 0，再解析。
"""
import io
import json
import re

# 只替换"作为 JSON 值出现"的裸字面量（不在字符串里的情况交给括号边界近似判断）
_BAD_LITERAL = re.compile(r"(?<=[:\[,\s])\s*(-?nan\(ind\)|-?nan|-?inf(?:inity)?)\s*(?=[,}\]])",
                          re.IGNORECASE)


def sanitize(text: str) -> str:
    """把非法的 NaN/inf 字面量换成 0。字符串里的同名子串一般不会同时满足两侧的 `:,[]{}` 边界。"""
    return _BAD_LITERAL.sub("0", text)


def loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(sanitize(text))


def load_path(path: str):
    with io.open(path, encoding="utf-8-sig", errors="replace") as f:
        return loads(f.read())
