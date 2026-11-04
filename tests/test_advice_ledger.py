#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建议账本（预测 vs 实测 → 校准因子 → 降级）与内存/加载检测器。"""
import json
import os
import tempfile
import unittest

from pipeline.advice import advisor, ledger


CATALOG = {"cvars": [
    {"name": "r.Streaming.PoolSize", "default": "1000", "help": "pool", "file": "R.cpp", "line": 1},
    {"name": "gc.TimeBetweenPurgingPendingKillObjects", "default": "61", "help": "gc",
     "file": "G.cpp", "line": 2},
    {"name": "s.AsyncLoadingThreadEnabled", "default": "0", "help": "alt", "file": "A.cpp", "line": 3},
], "missing": False, "path": "catalog/cvars.json"}


class LedgerTest(unittest.TestCase):
    def temp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, "ledger.json")

    def test_record_and_history(self):
        path = self.temp()
        ledger.record(path, "x", [1.0, 2.0], 1.5, "b_faster")
        ledger.record(path, "x", [1.0, 2.0], 1.1, "b_faster")
        ledger.record(path, "y", [3.0, 4.0], 9.9, "b_slower")
        data = ledger.load(path)
        self.assertEqual(len(data["entries"]), 3)
        self.assertEqual(len(ledger.history(data, "x")), 2)

    def test_factor_needs_two_samples(self):
        path = self.temp()
        ledger.record(path, "x", [2.0, 2.0], 1.0, "b_faster")
        cal = ledger.calibration(ledger.load(path), "x")
        self.assertEqual(cal["samples"], 1)
        self.assertIsNotNone(cal["predicted_mid_ms"])
        self.assertEqual(cal["factor"], 1.0)          # 单样本不校准
        ledger.record(path, "x", [2.0, 2.0], 1.0, "b_faster")
        cal = ledger.calibration(ledger.load(path), "x")
        self.assertAlmostEqual(cal["factor"], 0.5, places=3)   # 实测 1.0 / 预测 2.0

    def test_retire_after_three_noise(self):
        path = self.temp()
        for _ in range(3):
            ledger.record(path, "x", [1.0, 2.0], 0.01, "within_noise")
        cal = ledger.calibration(ledger.load(path), "x")
        self.assertTrue(cal["retired"])
        self.assertEqual(cal["noise_streak"], 3)

    def test_apply_calibration_scales_and_drops_retired(self):
        path = self.temp()
        for _ in range(2):
            ledger.record(path, "keep", [2.0, 2.0], 1.0, "b_faster")
        for _ in range(3):
            ledger.record(path, "retire", [1.0, 2.0], 0.0, "within_noise")
        data = ledger.load(path)
        hyps = [{"id": "keep", "expected_saving_ms": [2.0, 4.0]},
                {"id": "retire", "expected_saving_ms": [1.0, 2.0]},
                {"id": "unknown", "expected_saving_ms": [1.0, 1.0]}]
        out = ledger.apply_calibration(hyps, data)
        ids = [h["id"] for h in out]
        self.assertNotIn("retire", ids)
        keep = next(h for h in out if h["id"] == "keep")
        self.assertAlmostEqual(keep["expected_saving_ms"][0], 1.0, places=2)   # 2.0 × 0.5
        self.assertIn("expected_saving_ms_raw", keep)
        self.assertIn("unknown", ids)

    def test_record_from_diff_reads_perf_diff(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "perf_diff.json"), "w", encoding="utf-8") as f:
            json.dump({"sides": {"a": {"run_id": "A"}, "b": {"run_id": "B"}},
                       "significance": {"verdict": "b_slower", "delta_mean": 2.5}}, f)
        path = os.path.join(d, "ledger.json")
        entry = ledger.record_from_diff(path, "h", d, [1.0, 3.0])
        self.assertEqual(entry["measured_ms"], 2.5)
        self.assertEqual(entry["verdict"], "b_slower")


class MemoryDetectorTest(unittest.TestCase):
    def memory(self, groups, used, peak):
        return {"group_totals_mb": groups,
                "stats_top": [{"name": "x", "mb": 1.0}],
                "platform": {"process_physical_mb": {"used": used, "peak": peak}}}

    def test_dominant_group(self):
        hyp = advisor.detect_memory(self.memory({"STATGROUP_A": 800.0, "STATGROUP_B": 200.0},
                                                1000, 1000), CATALOG, 1000)
        self.assertEqual(len(hyp), 1)
        self.assertEqual(hyp[0]["id"], "memory_group_dominant:statgroup_a")
        self.assertTrue(hyp[0]["levers"])

    def test_peak_gap(self):
        hyp = advisor.detect_memory(self.memory({"STATGROUP_A": 500.0, "STATGROUP_B": 500.0},
                                                1000, 3000), CATALOG, 1000)
        self.assertIn("memory_peak_gap", [h["id"] for h in hyp])

    def test_no_data(self):
        self.assertEqual(advisor.detect_memory(None, CATALOG), [])


class LoadDetectorTest(unittest.TestCase):
    def test_top_entry_share(self):
        load = {"request_rows": 10, "file_activity_rows": 3, "packages": [
            {"name": "LoadMap - A", "total_ms": 800.0, "count": 1, "max_ms": 800.0},
            {"name": "Streamable - B", "total_ms": 200.0, "count": 2, "max_ms": 120.0}]}
        hyp = advisor.detect_load(load, CATALOG)
        self.assertEqual(len(hyp), 1)
        self.assertIn("load_top_entry", hyp[0]["id"])
        self.assertEqual(hyp[0]["evidence"]["share_pct"], 80.0)

    def test_missing_channel(self):
        hyp = advisor.detect_load({"request_rows": 0, "file_activity_rows": 0}, CATALOG)
        self.assertEqual([h["id"] for h in hyp], ["load_missing_channel"])

    def test_no_data(self):
        self.assertEqual(advisor.detect_load(None, CATALOG), [])


if __name__ == "__main__":
    unittest.main()
