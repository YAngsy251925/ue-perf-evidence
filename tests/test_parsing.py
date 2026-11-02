#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_parsing.py — P9（日志解析纪律）与 P10（CSV 解析纪律）的单元测试（M1.8）

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
"""
import os
import tempfile
import unittest

from pipeline.caliber.engine_log import parse_engine_log
from pipeline.extract import uecsv

LOG_WITH_GBK = (
    "LogInit: Build Configuration: Development\n".encode("utf-8")
    + b"[2026.11.02-10.00.00:000][  0]LogSetup: \xd6\xd0\xce\xc4 bytes from a GBK tool\n"  # GBK "中文"
    + b"LogRHI: Using Default RHI: D3D12\n"
)
LOG_WITH_BOM = b"\xef\xbb\xbf" + "LogInit: Build Configuration: Test\n".encode("utf-8")


class P9EngineLogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_gbk_bytes_replaced_not_fatal(self):
        facts = parse_engine_log(self.write("gbk.log", LOG_WITH_GBK))
        self.assertEqual(facts["build_config"], "Development")  # ASCII 标记不受坏字节影响
        self.assertEqual(facts["rhi_name"], "D3D12")

    def test_bom_stripped(self):
        facts = parse_engine_log(self.write("bom.log", LOG_WITH_BOM))
        self.assertEqual(facts["build_config"], "Test")


class P10UecsvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return p

    def test_trailing_header_meta_and_blank_rows_skipped(self):
        p = self.write("tail.csv", "Name,Count\nA,1\n\n,3\nName,Count\n[HasHeaderRowAtEnd],x\nB,2\n")
        rows = uecsv.read_dicts(p)
        self.assertEqual([r["Name"] for r in rows], ["A", "B"])

    def test_bracket_named_timer_is_data_not_metadata(self):
        # 反例教训：引擎存在以 "[" 开头的合法 timer 名（D3D12 实测 "[Scene] - StartFrame/EndFrame"），
        # 元数据行判定必须精确匹配，不能用 startswith("[")
        p = self.write("bracket.csv",
                       "Name,Count\n[Scene] - StartFrame,1352\nA,1\n[HasHeaderRowAtEnd],x\n")
        rows = uecsv.read_dicts(p)
        self.assertEqual([r["Name"] for r in rows], ["[Scene] - StartFrame", "A"])

    def test_short_row_padded(self):
        rows = uecsv.read_dicts(self.write("short.csv", "Name,Count\nB\n"))
        self.assertEqual(rows, [{"Name": "B", "Count": ""}])

    def test_oversized_field(self):
        # 单元格 20 万字符：默认 field_size_limit 131072 会抛 _csv.Error
        big = "X" * 200000
        rows = uecsv.read_dicts(self.write("big.csv", "Name,Note\nA,%s\n" % big))
        self.assertEqual(len(rows[0]["Note"]), 200000)

    def test_utf8_bom_and_bad_bytes(self):
        p = os.path.join(self.dir, "bom.csv")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbfName,Note\nA,ok\nB,\xd6\xd0\n")  # BOM + GBK 字节
        rows = uecsv.read_dicts(p)
        self.assertEqual(rows[0]["Name"], "A")
        self.assertEqual(rows[1]["Name"], "B")
        self.assertIn("\ufffd", rows[1]["Note"])  # 坏字节替换成 U+FFFD，不炸

    def test_empty_file(self):
        self.assertEqual(uecsv.read_dicts(self.write("empty.csv", "")), [])


if __name__ == "__main__":
    unittest.main()
