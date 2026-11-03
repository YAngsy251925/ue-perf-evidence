#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R9 显著性判定（M3.3）：t 分位数、组统计、Welch 阈值、MDE，以及 diff 的组形态接线。"""
import os
import unittest
from unittest import mock

from pipeline.digest import diff, significance as sig_mod


class QuantileTest(unittest.TestCase):
    """t 的 97.5% 分位数对照表值（df=1..1000），误差 < 1e-3。"""

    TABLE = [(1, 12.7062), (2, 4.3027), (4, 2.7764), (8, 2.3060),
             (10, 2.2281), (30, 2.0423), (100, 1.9840), (1000, 1.9623)]

    def test_table(self):
        for df, expected in self.TABLE:
            self.assertAlmostEqual(sig_mod.t_quantile(0.975, df), expected, delta=1e-3,
                                   msg="df=%s" % df)

    def test_df_extremes(self):
        self.assertAlmostEqual(sig_mod.t_quantile(0.975, 1.0e9), 1.959964, delta=1e-4)
        self.assertGreater(sig_mod.t_quantile(0.975, 3), sig_mod.t_quantile(0.975, 300))

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            sig_mod.t_quantile(0.5, 8)
        with self.assertRaises(ValueError):
            sig_mod.t_quantile(0.975, 0)


class GroupStatsTest(unittest.TestCase):
    def test_median_odd_even_and_cv(self):
        g = sig_mod.group_stats([10.0, 12.0, 11.0])
        self.assertEqual(g["n"], 3)
        self.assertAlmostEqual(g["median"], 11.0)
        self.assertAlmostEqual(g["mean"], 11.0)
        self.assertAlmostEqual(g["std"], 1.0)
        self.assertAlmostEqual(g["cv_pct"], 9.091, places=3)

        g2 = sig_mod.group_stats([17.0, 15.0, 19.0, 16.0])
        self.assertAlmostEqual(g2["median"], 16.5)

    def test_single_and_empty(self):
        one = sig_mod.group_stats([16.7])
        self.assertEqual(one["n"], 1)
        self.assertIsNone(one["std"])   # 单次 run 没有离散度，留空而不是 0
        self.assertIsNone(one["cv_pct"])
        empty = sig_mod.group_stats([])
        self.assertEqual(empty["n"], 0)
        self.assertIsNone(empty["median"])

    def test_drops_non_finite(self):
        g = sig_mod.group_stats([16.0, float("nan"), float("inf"), 17.0])
        self.assertEqual(g["n"], 2)


class JudgeTest(unittest.TestCase):
    def test_welch_factor_matches_plan(self):
        """PLAN R9：n=5 且两组 s 相同时阈值约 1.46·s。"""
        w = sig_mod.welch(1.0, 5, 1.0, 5)
        self.assertAlmostEqual(w["t"] * (2.0 / 5.0) ** 0.5, 1.458, delta=2e-3)
        self.assertAlmostEqual(w["df"], 8.0, delta=1e-9)

    def test_mde_is_t_plus_z80_with_floor(self):
        a = [10.0, 10.1, 9.9, 10.05, 9.95]
        b = [10.0, 10.1, 9.9, 10.05, 9.95]
        r = sig_mod.judge(a, b)
        raw = (2.306 + 0.8416) * r["welch"]["se"]
        self.assertAlmostEqual(r["mde_80"], max(0.5, raw), delta=1e-2)

    def test_detects_slower_and_faster(self):
        base = [10.0, 10.1, 9.9, 10.05, 9.95]
        slow = [12.0, 12.1, 11.9, 12.05, 11.95]
        faster = [8.0, 8.1, 7.9, 8.05, 7.95]
        self.assertEqual(sig_mod.judge(base, slow)["verdict"], "b_slower")
        self.assertEqual(sig_mod.judge(slow, base)["verdict"], "b_faster")

    def test_within_noise_uses_floor_threshold(self):
        a = [10.0, 10.02, 9.98, 10.01, 9.99]
        b = [10.05, 10.07, 10.03, 10.06, 10.04]
        r = sig_mod.judge(a, b)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["verdict"], "within_noise")
        self.assertEqual(r["threshold"], 0.5)  # 0.5ms 下限压住了 0.02ms 的 Welch 阈值

    def test_insufficient_n_blocks_conclusion(self):
        r = sig_mod.judge([10.0, 10.1], [12.0, 12.1])
        self.assertEqual(r["status"], "insufficient_n")
        self.assertEqual(r["verdict"], "no_conclusion")
        self.assertIn("不足 5 次 run", sig_mod.verdict_text(r))

    def test_noise_gate_blocks_conclusion(self):
        a = [10.0, 12.0, 8.0, 11.0, 9.0]   # CV 很大
        b = [10.0, 10.1, 9.9, 10.05, 9.95]
        r = sig_mod.judge(a, b)
        self.assertEqual(r["status"], "noise_gate_failed")
        self.assertEqual(r["verdict"], "no_conclusion")
        self.assertIn("噪声", sig_mod.verdict_text(r))

    def test_verdict_short_and_text(self):
        r = sig_mod.judge([10.0, 10.1, 9.9, 10.05, 9.95], [12.0, 12.1, 11.9, 12.05, 11.95])
        self.assertIn("B 更慢", sig_mod.verdict_short(r))
        self.assertIn("Δmean", sig_mod.verdict_text(r))

    def test_empty_side_not_comparable(self):
        r = sig_mod.judge([], [16.0] * 5)
        self.assertEqual(r["status"], "not_comparable")


def _manifest(run_id, p50, measurable=True):
    return {"run_id": run_id, "measurable": measurable,
            "measurable_reason": None if measurable else "nullrhi",
            "frame_semantics": {"game": {"stats": {"interval_ms": {"p50": p50}}}},
            "windows": {"steady": [1.0, 2.0]},
            "engine": {"rhi": "D3D12", "rhi_state": "real", "rhi_source": "log"},
            "trace": {"file": "trace.utrace"}}


class DiffGroupTest(unittest.TestCase):
    def setUp(self):
        self.caliber = {"run_id": "x", "rhi": "D3D12", "rhi_state": "real", "rhi_source": "log",
                        "stats_window": "steady", "window": [1.0, 2.0], "bridge_effective": True,
                        "thread_frames": {"game": 100, "render": 100}, "source_csv": "stats.csv"}
        self.rows = [{"name": "Load", "excl": 1.0, "count": 10}]

    def _run_with(self, names, p50s):
        sides = [{"run_dir": "d", "manifest": _manifest(n, p), "trace": "t"} for n, p in zip(names, p50s)]
        fake = (lambda *a, **k: ("ok", self.rows, self.caliber, []))
        with mock.patch.object(diff.run_digest, "prepare_rows", fake):
            return diff.build_group_diff(sides, sides[:], "GameThread", None)

    def test_run_p50_reads_interval(self):
        self.assertEqual(diff.run_p50(_manifest("a", 16.7)), 16.7)
        self.assertIsNone(diff.run_p50({"frame_semantics": {}}))

    def test_representative_is_median_run(self):
        sides = [{"manifest": _manifest("a", 20.0)}, {"manifest": _manifest("b", 16.0)},
                 {"manifest": _manifest("c", 18.0)}]
        self.assertEqual(diff.pick_representative(sides)["manifest"]["run_id"], "c")

    def test_group_diff_reports_significance(self):
        names = ["r%d" % i for i in range(5)]
        d, md = self._run_with(names, [16.70, 16.72, 16.68, 16.71, 16.69])
        self.assertEqual(d["capability"], "comparable")
        self.assertEqual(d["significance"]["groups"]["a"]["n"], 5)
        self.assertEqual(d["sides"]["a"]["group_n"], 5)
        self.assertIn("## 显著性（R9", md)
        self.assertIn("MDE", md)
        # 同一组自比 → 在噪声内
        self.assertEqual(d["significance"]["verdict"], "within_noise")

    def test_group_diff_insufficient_n_note(self):
        d, md = self._run_with(["r0", "r1"], [16.7, 16.8])
        self.assertEqual(d["significance"]["status"], "insufficient_n")
        self.assertIn("只报数值", md)

    def test_group_not_measurable_blocks(self):
        sides = [{"run_dir": "d", "manifest": _manifest("a", 16.7, measurable=False), "trace": "t"}]
        d, md = diff.build_group_diff(sides, sides, "GameThread", None)
        self.assertEqual(d["capability"], "not_comparable")
        self.assertIsNone(d.get("top_delta"))
        self.assertEqual(d["sides"]["a"]["group_n"], 1)


if __name__ == "__main__":
    unittest.main()
