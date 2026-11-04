#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.2：CSV / trace / B2 三路交叉校验（纯函数 + 一次端到端的 fixture run）。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest.views import crosscheck


class StatsTest(unittest.TestCase):
    def test_stats_ignores_none(self):
        s = crosscheck._stats([1.0, None, 3.0, 2.0])
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["mean"], 2.0)
        self.assertAlmostEqual(s["p50"], 2.0)
        self.assertAlmostEqual(s["max"], 3.0)

    def test_stats_empty(self):
        self.assertEqual(crosscheck._stats([])["n"], 0)

    def test_verdict_bands(self):
        self.assertEqual(crosscheck.verdict(0.4), "agree")
        self.assertEqual(crosscheck.verdict(10.0), "agree")
        self.assertEqual(crosscheck.verdict(10.1), "warn")
        self.assertEqual(crosscheck.verdict(-25.0), "warn")
        self.assertEqual(crosscheck.verdict(26.0), "disagree")
        self.assertEqual(crosscheck.verdict(None), "unknown")


class CsvProfilerTest(unittest.TestCase):
    def write(self, text):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_reads_frame_and_gpu_time(self):
        path = self.write(
            "EVENTS,DrawCall/Basepass,FrameTime,GPUTime\n"
            ",10,24.0,7.0\n"
            ",11,26.0,8.0\n")
        out = crosscheck.read_csv_profiler(path)
        self.assertEqual(out["rows"], 2)
        self.assertEqual(out["frame_time"], [24.0, 26.0])
        self.assertEqual(out["gpu_time"], [7.0, 8.0])

    def test_missing_columns_yield_empty_lists(self):
        path = self.write("EVENTS,DrawCall/Basepass\n,10\n")
        out = crosscheck.read_csv_profiler(path)
        self.assertEqual(out["frame_time"], [])
        self.assertEqual(out["gpu_time"], [])

    def test_non_finite_values_are_dropped(self):
        path = self.write("EVENTS,FrameTime,GPUTime\n,24.0,\n,inf,7.0\n")
        out = crosscheck.read_csv_profiler(path)
        self.assertEqual(out["frame_time"], [24.0])
        self.assertEqual(out["gpu_time"], [7.0])


class FixtureRunTest(unittest.TestCase):
    """一条 fixture run：CSV 24/26ms、trace 25ms 间隔、B2 Graphics busy 12.5s/500 帧。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = self.tmp.name
        os.makedirs(os.path.join(self.run, "csv"))
        os.makedirs(os.path.join(self.run, "extract"))
        os.makedirs(os.path.join(self.run, "b2"))

        csv_path = os.path.join(self.run, "csv", "Profile(x).csv")
        with io.open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write("EVENTS,FrameTime,GPUTime\n,24.0,6.0\n,26.0,8.0\n")
        with io.open(os.path.join(self.run, "coverage.json"), "w", encoding="utf-8") as f:
            json.dump({"coverage": {"start": 0.0, "end": 0.5}}, f)
        tracks = os.path.join(self.run, "extract", "frame_tracks.csv")
        with io.open(tracks, "w", encoding="utf-8", newline="") as f:
            f.write("ThreadName,TimerName,StartTime,EndTime,Duration\n")
            for i in range(21):
                start = i * 0.025
                f.write("Game Frames,Game Frame %d,%.6f,%.6f,0.025000\n" % (i, start, start + 0.025))
        scopes = os.path.join(self.run, "extract", "frame_scopes.csv")
        with io.open(scopes, "w", encoding="utf-8", newline="") as f:
            f.write("ThreadName,TimerName,StartTime,EndTime,Duration\n")
        with io.open(os.path.join(self.run, "b2", "b2_gpu.json"), "w", encoding="utf-8") as f:
            json.dump({"gpu_busy_s": 0.5, "span_s": 0.5, "window": [0.0, 0.5]}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_agrees_on_frame_time(self):
        r = crosscheck.build(self.run)
        ft = r["compare"]["frame_time"]
        self.assertEqual(ft["verdict"], "agree")
        self.assertLess(abs(ft["diff_pct"]), 5.0)   # CSV 均值 25ms vs 帧间隔 25ms
        self.assertEqual(r["csv"]["rows"], 2)
        self.assertEqual(r["trace"]["frame_interval_ms"]["n"], 20)

    def test_build_reports_gpu_per_frame(self):
        r = crosscheck.build(self.run)
        b2 = r["b2_gpu"]
        self.assertAlmostEqual(b2["gpu_ms_per_frame"], 1000.0 * 0.5 / 21, places=3)
        self.assertIn(r["compare"]["gpu_time"]["verdict"], ("agree", "warn", "disagree"))

    def test_missing_csv_is_reported_not_fatal(self):
        os.unlink(os.path.join(self.run, "csv", "Profile(x).csv"))
        r = crosscheck.build(self.run)
        self.assertIsNone(r["csv"])
        self.assertEqual(r["compare"]["frame_time"]["verdict"], "unknown")
        self.assertTrue(any("csv" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
