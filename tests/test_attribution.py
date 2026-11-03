#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3.4 归因判据：帧时间差 == 该 span 的每帧成本差（不是总秒数差）。"""
import unittest

from pipeline.digest import attribution as A


def diff_with(rows, observed):
    return {
        "top_delta": rows,
        "significance": {"delta_mean": observed},
    }


class SpanAttributionTest(unittest.TestCase):
    def test_expected_delta_matches_observed(self):
        # 10 次/帧 × 2.01ms → 18 次/帧 × 2.00ms：差 8 次/帧 ≈ 16ms
        diff = diff_with([{"name": "PerfEvidenceRegress", "calls_a": 13358, "calls_b": 14132,
                           "a_s": 26.885, "b_s": 28.27}], observed=16.05)
        out = A.span_attribution(diff, "PerfEvidenceRegress", frames_a=1320, frames_b=785)
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["calls_per_frame_a"], 10.12, places=2)
        self.assertAlmostEqual(out["calls_per_frame_b"], 18.0, places=2)
        self.assertAlmostEqual(out["expected_delta_ms"], 15.82, delta=0.05)

    def test_missing_span_is_not_ok(self):
        out = A.span_attribution(diff_with([], 1.0), "PerfEvidenceRegress", 100, 100)
        self.assertFalse(out["ok"])
        self.assertIn("没有 span", out["reason"])

    def test_zero_calls_is_not_ok(self):
        diff = diff_with([{"name": "X", "calls_a": 0, "calls_b": 10, "a_s": 0.0, "b_s": 1.0}], 5.0)
        out = A.span_attribution(diff, "X", 100, 100)
        self.assertFalse(out["ok"])
        self.assertIn("没有调用", out["reason"])

    def test_tolerance_catches_mismatch(self):
        # 期望 0ms（两组每帧成本相同），实测 +10ms → 不该算归因正确
        diff = diff_with([{"name": "X", "calls_a": 1000, "calls_b": 1000,
                           "a_s": 2.0, "b_s": 2.0}], observed=10.0)
        out = A.span_attribution(diff, "X", 100, 100)
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
