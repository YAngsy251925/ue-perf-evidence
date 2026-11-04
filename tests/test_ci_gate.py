#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6.4：diff 的 CI 闸门（significance.gate）+ manifests 瘦包 + manifest 路径直传。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest import significance
from tools import pack_manifests


def judge_b_slower():
    """构造一个"B 显著变慢"的判定结果（两组各 5 个值，B 明显更大）。"""
    return significance.judge([10.0, 10.1, 9.9, 10.05, 9.95], [16.0, 16.1, 15.9, 16.05, 15.95])


def judge_faster():
    return significance.judge([16.0, 16.1, 15.9, 16.05, 15.95], [10.0, 10.1, 9.9, 10.05, 9.95])


def judge_noise():
    return significance.judge([10.0, 10.1, 9.9, 10.05, 9.95], [10.02, 10.08, 9.98, 10.04, 9.96])


class GateTest(unittest.TestCase):
    def test_regression_blocks_when_fail_on_regression(self):
        g = significance.gate(judge_b_slower(), fail_on="regression")
        self.assertEqual(g["decision"], "regression")
        self.assertEqual(g["exit_code"], significance.EXIT_REGRESSION)

    def test_none_never_blocks(self):
        g = significance.gate(judge_b_slower(), fail_on="none")
        self.assertEqual(g["exit_code"], significance.EXIT_PASS)
        self.assertEqual(g["decision"], "pass")

    def test_improvement_passes_unless_significant(self):
        self.assertEqual(significance.gate(judge_faster(), fail_on="regression")["exit_code"],
                         significance.EXIT_PASS)
        self.assertEqual(significance.gate(judge_faster(), fail_on="significant")["exit_code"],
                         significance.EXIT_IMPROVEMENT)

    def test_noise_passes(self):
        g = significance.gate(judge_noise(), fail_on="regression")
        self.assertEqual(g["exit_code"], significance.EXIT_PASS)
        self.assertEqual(g["decision"], "pass")

    def test_budget_blocks_even_within_noise(self):
        # 噪声内的 +0.04ms 不显著，但预算设成 0.01ms 就得拦
        g = significance.gate(judge_noise(), fail_on="none", max_delta_ms=0.01)
        self.assertEqual(g["decision"], "over_budget")
        self.assertEqual(g["exit_code"], significance.EXIT_REGRESSION)

    def test_budget_pct(self):
        g = significance.gate(judge_b_slower(), fail_on="none", max_delta_pct=5.0)
        self.assertEqual(g["decision"], "over_budget")

    def test_budget_does_not_fire_on_improvement(self):
        g = significance.gate(judge_faster(), fail_on="none", max_delta_ms=0.01)
        self.assertEqual(g["exit_code"], significance.EXIT_PASS)

    def test_insufficient_data_lenient_by_default(self):
        r = significance.judge([10.0], [16.0])
        g = significance.gate(r, fail_on="regression")
        self.assertEqual(g["decision"], "insufficient_data")
        self.assertEqual(g["exit_code"], significance.EXIT_PASS)
        self.assertTrue(any("n ≥" in x or "每组需要" in x for x in g["reasons"]))

    def test_insufficient_data_strict_fails(self):
        r = significance.judge([10.0], [16.0])
        g = significance.gate(r, fail_on="regression", strict=True)
        self.assertEqual(g["exit_code"], significance.EXIT_USAGE)


class PackTest(unittest.TestCase):
    def test_bundle_keeps_only_manifests(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "runs", "R1")
            os.makedirs(src)
            with io.open(os.path.join(src, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"run_id": "R1", "measurable": True}, f)
            with io.open(os.path.join(src, "trace.utrace"), "wb") as f:
                f.write(b"x" * 1024)
            out = os.path.join(d, "bundle")
            rc = pack_manifests.main(["--out", out, "--root", os.path.join(d, "runs"), "--runs", "R1"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(out, "R1", "manifest.json")))
            self.assertFalse(os.path.exists(os.path.join(out, "R1", "trace.utrace")))
            with io.open(os.path.join(out, "bundle.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["runs"], ["R1"])

    def test_missing_manifest_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            rc = pack_manifests.main(["--out", os.path.join(d, "b"), "--root", d, "--runs", "nope"])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
