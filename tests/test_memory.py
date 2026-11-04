#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6.1 / M6.2：MemReport 解析、内存视图、分配导出解析（都是纯函数，不跑引擎）。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest.views import allocations, memory
from pipeline.extract import memreport

MEMREPORT = """Changelist: 56702186
Config: Development
Device Name: WindowsEditor
CommandLine Options: -game -trace=cpu
Time Since Boot: 294.16 Seconds

Platform Memory Stats for WindowsEditor
Process Physical Memory: 6471.70 MB used, 9105.30 MB peak
Process Virtual Memory: 10938.34 MB used, 13137.45 MB peak
Physical Memory: 21856.12 MB used,  10589.25 MB free, 32445.37 MB total
Virtual Memory: 33503.03 MB used,  7134.34 MB free, 40637.37 MB total

Memory Stats:
         0.000MB  -  EXR Reader Pool Memory. - STAT_ExrMediaReaderPoolMem - STATGROUP_ImgMediaPlugin - STATCAT_Advanced
      1204.500MB  -  Total D3D12 - STAT_D3D12MemoryCurrentTotal - STATGROUP_D3D12Resources - STATCAT_Advanced
        78.401MB  -  Video Memory - STAT_D3D12VideoMem - STATGROUP_D3D12Resources - STATCAT_Advanced

Objects:
Showing 50 of 3399 matched resources
"""

ALLOCS_CSV = """StartEventIndex,EndEventIndex,EventDistance,StartTime,EndTime,Duration,Address,MemoryPage,Size,Tag,Asset,ClassName,Package,AllocThread,FreeThread
1,inf,inf,0.1,+inf,+inf,0x1,0x1,1048576,Textures,,Texture2D,/Game/A,2,0
2,inf,inf,0.2,+inf,+inf,0x2,0x2,2097152,Textures,,Texture2D,/Game/B,2,0
3,inf,inf,0.3,+inf,+inf,0x3,0x3,524288,Meshes,,StaticMesh,/Game/B,3,0
"""


def write(dir_path, name, text):
    path = os.path.join(dir_path, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


class MemreportParseTest(unittest.TestCase):
    def test_header_and_platform(self):
        r = memreport.parse_text(MEMREPORT)
        self.assertEqual(r["header"]["Config"], "Development")
        self.assertEqual(r["platform"]["process_physical_mb"], {"used": 6471.7, "peak": 9105.3})
        self.assertEqual(r["platform"]["system_physical_mb"]["total"], 32445.37)

    def test_stats_and_groups(self):
        r = memreport.parse_text(MEMREPORT)
        self.assertEqual(r["stat_count"], 3)
        self.assertAlmostEqual(r["group_totals_mb"]["STATGROUP_D3D12Resources"], 1282.901, places=3)
        self.assertEqual(r["stats_top"][0]["stat"], "STAT_D3D12MemoryCurrentTotal")

    def test_showing_counts(self):
        r = memreport.parse_text(MEMREPORT)
        self.assertEqual(r["showing"][0]["shown"], 50)
        self.assertEqual(r["showing"][0]["total"], 3399)

    def test_find_files_walks_subdirs(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "memreport/a.memreport", MEMREPORT)
            files = memreport.find_files(d)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith("a.memreport"))


class MemoryViewTest(unittest.TestCase):
    def test_flags_probe_placement(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "memreport/x.memreport", MEMREPORT)
            write(d, "manifest.json", json.dumps({
                "memreport": {"files": ["x.memreport"], "at_s": 40.0, "in_probe": True,
                              "inside_coverage_probe": True},
                "windows": {"stats": [10.0, 35.0], "coverage_probe": [35.0, 45.0]},
                "frame_semantics": {"game": {"stats": {"interval_ms": {"p99": 9.85}}}}},
                ensure_ascii=False))
            r = memory.build(d)
        self.assertTrue(r["inside_probe"])
        self.assertEqual(r["p99_ms"], 9.85)
        self.assertEqual(r["files"], ["x.memreport"])
        md = memory.render_md(r)
        self.assertIn("落在探针段=True", md)

    def test_no_memreport_is_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            r = memory.build(d)
        self.assertEqual(r["files"], [])
        self.assertTrue(any("memreport" in w for w in r["warnings"]))


class AllocationsTest(unittest.TestCase):
    def test_groups_by_tag_class_package(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(d, "allocs_aAf.csv", ALLOCS_CSV)
            result = allocations.build_from_csv(path, "aAf")
        s = result["summary"]
        self.assertEqual(s["rows"], 3)
        self.assertAlmostEqual(s["total_mb"], 3.5, places=3)
        self.assertEqual(s["by_tag"][0]["name"], "Textures")
        self.assertAlmostEqual(s["by_tag"][0]["mb"], 3.0, places=3)
        self.assertEqual(s["by_package"][0]["name"], "/Game/B")
        self.assertEqual(len(s["top_single"]), 3)

    def test_empty_csv_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            header = ALLOCS_CSV.splitlines()[0] + "\n"
            path = write(d, "allocs_aAf.csv", header)
            result = allocations.build_from_csv(path, "aAf")
        self.assertEqual(result["summary"]["sized_rows"], 0)
        self.assertTrue(any("memory" in w for w in result["warnings"]))
        self.assertIn("没有可用的分配数据", allocations.render_md(result))


if __name__ == "__main__":
    unittest.main()
