#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6.3：加载视图（包级 Top-N）的纯函数部分。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest.views import load


PAYLOAD = {
    "schema": "perf-b2-load/0",
    "window": [0.0, 220.0],
    "request_rows": 3,
    "requests_in_window": 3,
    "file_activity_rows": 1,
    "package_count": 2,
    "packages": [
        {"name": "LoadMap - /ShooterMaps/Maps/L_Expanse", "count": 1, "total_ms": 7247.3,
         "max_ms": 7247.3, "avg_ms": 7247.3},
        {"name": "StreamableManager - Experience", "count": 2, "total_ms": 300.0,
         "max_ms": 200.0, "avg_ms": 150.0},
    ],
}


class LoadViewTest(unittest.TestCase):
    def write(self, d, name, text):
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_missing_cache_is_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            r = load.build(d)
        self.assertEqual(r["packages"], [])
        self.assertTrue(any("b2_load.json" in w for w in r["warnings"]))

    def test_reads_payload_and_renders(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, os.path.join("b2", "b2_load.json"),
                       json.dumps(PAYLOAD, ensure_ascii=False))
            r = load.build(d, top=10)
        self.assertEqual(r["request_rows"], 3)
        self.assertEqual(r["package_count"], 2)
        self.assertEqual(len(r["packages"]), 2)
        md = load.render_md(r)
        self.assertIn("按总耗时 Top 2", md)
        self.assertIn("L_Expanse", md)
        self.assertIn("7247.3", md)

    def test_zero_requests_explains_channel_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            empty = dict(PAYLOAD, request_rows=0, requests_in_window=0, package_count=0, packages=[])
            self.write(d, os.path.join("b2", "b2_load.json"), json.dumps(empty))
            r = load.build(d)
        self.assertTrue(any("loadtime" in w for w in r["warnings"]))

    def test_top_limit(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, os.path.join("b2", "b2_load.json"),
                       json.dumps(PAYLOAD, ensure_ascii=False))
            r = load.build(d, top=1)
        self.assertEqual(len(r["packages"]), 1)


if __name__ == "__main__":
    unittest.main()
