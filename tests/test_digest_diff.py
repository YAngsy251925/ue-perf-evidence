#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_digest_diff.py — M1.6 perf_diff v0 的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
单元测试只用内联样本（不跑 UnrealInsights）；验收测试读取本机实测 run 并真实做窗口导出。
"""
import json
import os
import tempfile
import unittest

from pipeline.digest import diff, run_digest
from tests.acceptance_data import TRACE, accept_manifest, have_acceptance_data


def mrow(name, excl, count=1):
    return {"name": name, "count": count, "incl": excl, "excl": float(excl),
            "emax": excl, "eavg": excl / count if count else 0.0}


def rows(*pairs):
    return [mrow(p[0], p[1], p[2] if len(p) > 2 else 1) for p in pairs]


class DeltaRowsTest(unittest.TestCase):
    def test_union_abs_sort_and_tiebreak(self):
        a = rows(("Foo", 1.0), ("Shared", 2.0))
        b = rows(("Bar", 3.0), ("Shared", 5.0))
        out = diff.delta_rows(a, b, 10.0, 10.0)
        # |Δ|: Shared 3.0 > Bar 3.0? Shared=|5-2|=3, Bar=3 — 并列时名字升序：Bar 在前
        self.assertEqual([r["name"] for r in out], ["Bar", "Shared", "Foo"])
        shared = out[1]
        self.assertEqual((shared["a_s"], shared["b_s"], shared["delta_s"]), (2.0, 5.0, 3.0))

    def test_absent_side_counts_as_zero(self):
        a = rows(("Gone", 1.5))
        b = rows(("New", 4.0))
        out = diff.delta_rows(a, b, 10.0, 10.0)
        by = {r["name"]: r for r in out}
        self.assertEqual((by["New"]["a_s"], by["New"]["b_s"], by["New"]["delta_s"]), (0.0, 4.0, 4.0))
        self.assertEqual((by["Gone"]["a_s"], by["Gone"]["b_s"], by["Gone"]["delta_s"]), (1.5, 0.0, -1.5))
        self.assertEqual((by["New"]["calls_a"], by["New"]["calls_b"]), (0, 1))

    def test_pct_uses_each_side_total(self):
        out = diff.delta_rows(rows(("X", 1.0)), rows(("X", 1.0)), 4.0, 8.0)
        self.assertEqual((out[0]["a_pct"], out[0]["b_pct"]), (25.0, 12.5))

    def test_cap(self):
        a = rows(*[("A%d" % i, 1.0) for i in range(20)])
        b = rows(*[("B%d" % i, 2.0) for i in range(20)])
        self.assertEqual(len(diff.delta_rows(a, b, 100.0, 100.0)), diff.DIFF_TOP)


class SideOnlyTest(unittest.TestCase):
    def test_absent_vs_zero_and_wait_excluded_by_caller(self):
        own = rows(("New", 2.0, 5), ("WasZero", 1.0), ("Kept", 1.0))
        other = rows(("Kept", 0.5), ("WasZero", 0.0))
        view, total = diff.side_only(own, other, 10.0)
        # New：对侧没有 → 独有；WasZero：对侧自耗时为 0 → 也算独有；Kept：对侧有值 → 不算
        self.assertEqual(total, 2)
        self.assertEqual([r["name"] for r in view], ["New", "WasZero"])
        self.assertEqual(view[0], {"name": "New", "self_s": 2.0, "self_pct": 20.0, "calls": 5})


class BuildDiffUnitTest(unittest.TestCase):
    """不导出的闸门路径：任一侧不可测量 → not_comparable，无数字。"""

    def manifest(self, measurable, run_id):
        return {"run_id": run_id, "measurable": measurable,
                "measurable_reason": None if measurable else "nullrhi run: smoke only",
                "window_basis": {"stats_window": "steady", "reference": None},
                "windows": {"steady": [1.0, 2.0]},
                "engine": {"rhi": None, "rhi_state": "real" if measurable else "null"},
                "trace": {"statnamedevents_effective": True},
                "frame_semantics": {"game": {"window_frames": 1}, "render": {"window_frames": 1}},
                "inputs": {"extract": "x"}, "warnings": []}

    def test_not_measurable_side_blocks(self):
        side_b = {"run_dir": "d", "manifest": self.manifest(False, "b"), "trace": "no.utrace"}
        side_a = {"run_dir": "d", "manifest": self.manifest(True, "a"), "trace": "no.utrace"}
        d, md = diff.build_diff(side_a, side_b)
        self.assertEqual(d["capability"], "not_comparable")
        self.assertEqual(d["sides"]["b"]["status"], "not_measurable")
        self.assertIn("nullrhi", d["sides"]["b"]["reason"])
        self.assertIsNone(d.get("top_delta"))
        self.assertNotIn("| timer |", md)
        self.assertIn(diff.NOTE_V0, md)


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class DiffAcceptanceTest(unittest.TestCase):
    """PLAN M1.6 验收。期望值取自 runs/m1_6_probe 的两份 top-200 导出（独立于 diff.py 计算路径）。"""

    PROBE = os.path.join("runs", "m1_6_probe")

    @classmethod
    def load_probe(cls, name):
        from pipeline.extract import uecsv
        p = os.path.join(cls.PROBE, name, "stats_GameThread_window.csv")
        merged = {}
        for r in uecsv.read_dicts(p):
            m = merged.setdefault(r["Name"], {"name": r["Name"], "count": 0, "excl": 0.0})
            m["count"] += int(float(r["Count"] or 0))
            m["excl"] += float(r["Excl"] or 0)
        return merged

    @classmethod
    def diff_for(cls, name_a, name_b):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        sides = {}
        for key, name in (("a", name_a), ("b", name_b)):
            sides[key] = {"run_dir": os.path.join("runs", "acceptance", name),
                          "manifest": accept_manifest(name), "trace": TRACE[name]}
        return diff.build_diff(sides["a"], sides["b"])

    def test_same_run_all_zero(self):
        d, md = self.diff_for("example", "example")
        self.assertEqual(d["capability"], "comparable")
        self.assertTrue(all(r["delta_s"] == 0.0 for r in d["top_delta"]))
        self.assertEqual(d["new_in_b"]["total"], 0)
        self.assertEqual(d["only_in_a"]["total"], 0)
        self.assertEqual(d["wait"]["delta_s"], 0.0)

    def test_example_vs_d3d12(self):
        d, md = self.diff_for("example", "d3d12")
        ex, d3 = self.load_probe("ex_top200"), self.load_probe("d3d12_top200")
        self.assertEqual(d["capability"], "comparable")
        # 侧摘要：总数与等待类合计来自 top-200 全集（与 digest v1 的 top-50 不同）。
        # P21：GameThread 导出混着 GPU 队列行，B 侧期望值用同一 run 缓存的 GPU1 top-200
        # 导出（独立命令、同 trace 同窗口）按 (Name,Count,Incl) 差集净化后重算。
        ta = sum(r["excl"] for r in ex.values())
        from pipeline.digest.views import cpu
        gpu1 = cpu.load_rows(os.path.join("runs", "acceptance", "d3d12", "digest_export",
                                          "window_steady_top200", "stats_GPU1_window.csv"))
        gpu_keys = run_digest.gpu_row_keys(gpu1)
        from pipeline.extract import uecsv
        d3_rows = [{"name": r["Name"], "count": int(float(r["Count"] or 0)),
                    "incl": float(r["Incl"] or 0), "excl": float(r["Excl"] or 0)}
                   for r in uecsv.read_dicts(os.path.join(self.PROBE, "d3d12_top200",
                                                          "stats_GameThread_window.csv"))]
        tb = sum(r["excl"] for r in d3_rows
                 if (r["name"], r["count"], round(r["incl"], 6)) not in gpu_keys)
        self.assertEqual(d["sides"]["a"]["total_self_s"], round(ta, 3))
        self.assertEqual(d["sides"]["b"]["total_self_s"], round(tb, 3))
        # Top 差值第一名：UpdateCoreCsvStats_BeginFrame（A 无，B 0.132）。净化前是 RenderGraphExecute，
        # 它是 GPU 行，P21 之后不再出现在 GT 侧
        top = d["top_delta"][0]
        self.assertEqual((top["name"], top["a_s"], top["b_s"], top["delta_s"]),
                         ("UpdateCoreCsvStats_BeginFrame", 0.0, 0.132, 0.132))
        # 等待类：A 侧 GameThreadWaitForTask 行级值不变（78.78% 是对 top-50 分母的，
        # top-200 分母下不同——这里只对绝对秒数做断言）
        self.assertEqual(d["wait"]["rows"][0]["name"], "Game thread idle time")
        idle_b = round(d3["Game thread idle time"]["excl"], 3)
        self.assertEqual(d["wait"]["rows"][0]["b_s"], idle_b)
        self.assertEqual(d["wait"]["delta_s"], round(d["wait"]["b"]["self_s"] - d["wait"]["a"]["self_s"], 3))
        # 新增 / 消失（new_in_b 的第一名是 csv profiler 计时，不再是 GPU pass）
        self.assertGreater(d["new_in_b"]["total"], 100)
        self.assertIn("UpdateCoreCsvStats_BeginFrame", [r["name"] for r in d["new_in_b"]["rows"][:3]])
        self.assertIn("FEngineLoop", [r["name"] for r in d["only_in_a"]["rows"][:3]])
        self.assertIn(diff.NOTE_V0, md)
        self.assertIn("UpdateCoreCsvStats_BeginFrame", md)

    def test_b2_smoke_gate(self):
        d, md = self.diff_for("b2_smoke", "example")
        self.assertEqual(d["capability"], "not_comparable")
        self.assertEqual(d["sides"]["a"]["status"], "not_measurable")
        self.assertNotIn("| timer |", md)

    def test_deterministic(self):
        a, md_a = self.diff_for("example", "d3d12")
        b, md_b = self.diff_for("example", "d3d12")
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))
        self.assertEqual(md_a, md_b)


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class PrepareRowsRegressionTest(unittest.TestCase):
    """M1.5 digest 行为在 prepare_rows 重构后不变（example 稳态窗口关键数字）。"""

    def test_digest_values_unchanged(self):
        manifest = accept_manifest("example")
        digest, _ = run_digest.build_run_digest(
            os.path.join("runs", "acceptance", "example"), manifest, TRACE["example"])
        st = digest["stats"]
        self.assertEqual((st["total_self_s"], st["wait_total"]["self_s"], st["wait_total"]["self_pct"]),
                         (2.357, 1.939, 82.26))
        self.assertEqual(st["top_by_self_time"][0]["name"], "FViewport_Draw")


if __name__ == "__main__":
    unittest.main()
