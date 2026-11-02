#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""uecsv.py — UE 导出 CSV 的统一读取纪律（PLAN P10，M1.8）

实测依据（PLAN 附录 B 的 M0.5 行）：
  - csv.field_size_limit 默认 131072，CSV Profiler 的单元格会超限 → 模块导入时放大到 1 GiB
  - 文件用 utf-8-sig 读取（Insights 导出与引擎日志同源，带 BOM 也能读；坏字节按 replace 不炸）
  - CSV Profiler 文件尾有重复表头行、首单元格为空的行与 [HasHeaderRowAtEnd] 元数据行 → 跳过
  - 短行右侧补空，保证字段对齐

反例教训（M1.7 生成 golden 时实测）：引擎存在以 "[" 开头的合法 timer 名（"[Scene] - StartFrame" /
"[Scene] - EndFrame"），所以元数据行的判定必须是首单元格**精确等于**已知标记，不能用 startswith("["）。
"丢弃首帧"（CSV Profiler 首帧含启动尖峰，实测 36,459ms）属于 M5.2 的统计口径，不在这层做。
"""
import csv

csv.field_size_limit(1 << 30)

# CSV Profiler 写在文件尾的元数据行标记（精确匹配；不要扩展成前缀匹配，见模块 docstring 的反例）
METADATA_MARKERS = ("[HasHeaderRowAtEnd]",)


def read_dicts(path):
    """读 UE 导出的 CSV，返回 list[dict]（以首行为表头）。"""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    first_cell = header[0].strip() if header else ""
    out = []
    for row in data:
        if not row or not row[0].strip():
            continue  # 空行 / 首单元格为空的尾行
        cell = row[0].strip()
        if cell in METADATA_MARKERS:
            continue  # 元数据行
        if cell == first_cell:
            continue  # 文件尾的重复表头
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        out.append(dict(zip(header, row)))
    return out
