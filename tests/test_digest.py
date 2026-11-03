#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_digest.py — M1.5 digest v1 的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
单元测试只用内联样本（不跑 UnrealInsights）；验收测试读取本机实测 run 并真实做窗口导出。
"""
import json
import os
import tempfile
import unittest

from pipeline.digest import run_digest
from pipeline.digest.views import cpu
from tests.acceptance_data import TRACE, accept_manifest, have_acceptance_data


def row(name, excl, incl=None, count=1, emax=None, eavg=None):
    excl = float(excl)
    return {"name": name, "count": count, "incl": float(incl if incl is not None else excl),
            "excl": excl, "imax": 0.0, "iavg": 0.0, "imed": 0.0,
            "emax": float(emax if emax is not None else excl),
            "eavg": float(eavg if eavg is not None else excl / count)}


def minimal_manifest(measurable=True, steady=(10.0, 12.0), run_id="unit"):
    return {
        "run_id": run_id, "measurable": measurable,
        "measurable_reason": "real RHI (D3D12)" if measurable else "nullrhi run: smoke only",
        "engine": {"rhi": "D3D12" if measurable else None,
                   "rhi_state": "real" if measurable else "null",
                   "rhi_source": "log" if measurable else "command_line",
                   "build_config": "Development"},
        "profile": None,
        "windows": {"frames": [9.0, 12.5], "steady": steady},
        "window_basis": {"stats_window": "steady" if steady else None,
                         "reference": {"side": "game", "source": "frame_track", "threads": ["Game Frames"]}},
        "trace": {"statnamedevents_effective": True, "statnamedevents_requested": True},
        "frame_semantics": {"game": {"window_frames": 100}, "render": {"window_frames": 100}},
        "inputs": {"extract": "extract"},
        "warnings": ["unit-test warning"],
    }


class MergeByNameTest(unittest.TestCase):
    def test_merges_duplicate_names(self):
        rows = [row("Frame", 0.5, count=100, emax=4.0), row("Frame", 0.25, count=50, emax=65.0),
                row("Other", 1.0)]
        merged = run_digest.merge_by_name(rows)
        self.assertEqual(len(merged), 2)
        frame = next(r for r in merged if r["name"] == "Frame")
        self.assertAlmostEqual(frame["excl"], 0.75)
        self.assertEqual(frame["count"], 150)
        self.assertAlmostEqual(frame["emax"], 65.0)
        self.assertAlmostEqual(frame["eavg"], 0.75 / 150)

    def test_single_name_untouched(self):
        r0 = row("A", 1.0, count=2)
        self.assertEqual(run_digest.merge_by_name([r0])[0]["eavg"], 0.5)


class SplitWaitTest(unittest.TestCase):
    def test_wait_stall_idle_are_symptoms(self):
        rows = [row("GameThreadWaitForTask", 2.0), row("Game thread idle time", 1.0),
                row("WaitForTasks", 0.5), row("RenderGraphExecute", 6.5)]
        others, wait, total = run_digest.split_wait(rows)
        self.assertEqual([r["name"] for r in wait], ["GameThreadWaitForTask", "Game thread idle time", "WaitForTasks"])
        self.assertEqual([r["name"] for r in others], ["RenderGraphExecute"])
        self.assertEqual((total["self_s"], total["self_pct"], total["count"]), (3.5, 35.0, 3))

    def test_case_insensitive(self):
        _, wait, _ = run_digest.split_wait([row("IDLESTATE", 1.0), row("render", 1.0)])
        self.assertEqual([r["name"] for r in wait], ["IDLESTATE"])


class ConcludeTest(unittest.TestCase):
    def test_wait_dominant_is_clear(self):
        text, confidence = run_digest.conclude(78.78, "FViewport_Draw", 4.0, 539)
        self.assertEqual(confidence, "clear")
        self.assertIn("78.78%", text)
        self.assertIn("瓶颈不在 GT", text)

    def test_wait_moderate_band(self):
        _, confidence = run_digest.conclude(39.90, "RenderGraphExecute", 40.26, 2704)
        self.assertEqual(confidence, "moderate")

    def test_top_dominant_is_clear(self):
        text, confidence = run_digest.conclude(0.4, "Foo", 55.0, 10)
        self.assertEqual(confidence, "clear")
        self.assertIn("Foo", text)

    def test_top_moderate_and_marginal(self):
        self.assertEqual(run_digest.conclude(0.0, "Foo", 25.0, 10)[1], "moderate")
        self.assertEqual(run_digest.conclude(0.0, "Foo", 5.0, 10)[1], "marginal")


class BuildFromStatsTest(unittest.TestCase):
    def base_caliber(self):
        return {"run_id": "unit", "profile": "unknown (M2.1)", "rhi": "D3D12", "rhi_state": "real",
                "rhi_source": "log", "build_config": "Development", "stats_window": "steady",
                "window": [10.0, 12.0], "frames_window": [9.0, 12.5],
                "reference": {"side": "game", "source": "frame_track", "threads": ["Game Frames"]},
                "bridge_effective": True, "bridge_requested": True,
                "thread_frames": {"game": 100, "render": 100}, "source_csv": "stats_GameThread_window.csv"}

    def test_wait_excluded_from_top_and_omission_math(self):
        rows = [row("GameThreadWaitForTask", 7.878, count=480), row("FViewport_Draw", 0.400, count=539)]
        rows += [row("Filler%02d" % i, 0.005) for i in range(20)]  # 20 个干扰行，Top15 只装得下 14 个
        digest, md = run_digest.build_from_stats(rows, self.base_caliber(), "steady", [])
        st = digest["stats"]
        self.assertEqual(digest["capability"], "measurable")
        self.assertEqual((st["total_self_s"], st["wait_total"]["self_pct"]), (8.378, 94.03))
        # R5：等待类不进瓶颈候选，Top1 是排除后的第一名
        self.assertEqual(st["top_by_self_time"][0]["name"], "FViewport_Draw")
        # 省略：Top15 之外的 6 个 Filler，合计 0.03s / 8.378s
        self.assertEqual((st["omitted"]["timers"], st["omitted"]["self_pct"]), (6, 0.36))
        self.assertEqual(digest["conclusion"]["confidence"], "clear")
        self.assertIn("94.03%", digest["conclusion"]["text"])
        self.assertIn("timer:FViewport_Draw@steady", digest["refs"])
        self.assertIn("timer:GameThreadWaitForTask@steady", digest["refs"])
        self.assertIn("| GameThreadWaitForTask | 7.878 | 94.03% | 480 |", md)
        self.assertIn("其余 6 个 timer 合计 0.36%。", md)

    def test_md_and_json_are_deterministic(self):
        rows = [row("GameThreadWaitForTask", 7.878, count=480), row("FViewport_Draw", 0.400)]
        a, md_a = run_digest.build_from_stats(rows, self.base_caliber(), "steady", ["w1"])
        b, md_b = run_digest.build_from_stats(list(reversed(rows)), self.base_caliber(), "steady", ["w1"])
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))
        self.assertEqual(md_a, md_b)

    def test_zero_wait_row_not_in_refs_or_table(self):
        rows = [row("SomethingWait", 0.0), row("Worker", 1.0)]
        digest, md = run_digest.build_from_stats(rows, self.base_caliber(), "steady", [])
        self.assertEqual(digest["stats"]["wait_timers"], [])
        self.assertNotIn("timer:SomethingWait@steady", digest["refs"])
        self.assertNotIn("| SomethingWait |", md)


class GateTest(unittest.TestCase):
    """manifest 闸门：不可测量的 run 不导出、不出数字（Q3 后半的 digest 层）。"""

    def test_not_measurable_has_no_numbers(self):
        digest, md = run_digest.not_measurable_digest(minimal_manifest(measurable=False))
        self.assertEqual(digest["capability"], "not_measurable")
        self.assertIsNone(digest["stats"])
        self.assertEqual(digest["refs"], [])
        self.assertEqual(digest["conclusion"]["confidence"], "none")
        self.assertIn("not_measurable", digest["conclusion"]["text"])
        self.assertIn("nullrhi run: smoke only", digest["conclusion"]["text"])
        self.assertNotIn("| # | timer |", md)
        self.assertIn("capability: not_measurable", md)

    def test_missing_stats_window_stops_before_export(self):
        # steady 为 null 时不出数字，也不需要 trace（build_run_digest 不会碰它）
        manifest = minimal_manifest(steady=None)
        digest, md = run_digest.build_run_digest("dummy_run_dir", manifest, "no-such-file.utrace")
        self.assertIsNone(digest["stats"])
        self.assertEqual(digest["conclusion"]["confidence"], "none")
        self.assertTrue(any("no stats window" in w for w in digest["warnings"]))


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class DigestAcceptanceTest(unittest.TestCase):
    """PLAN M1.5 验收。期望值来自对实测 CSV 的独立计算（不经过本模块）。"""

    @classmethod
    def digest_for(cls, name, thread="GameThread"):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        manifest = accept_manifest(name)
        digest, md = run_digest.build_run_digest(
            os.path.join("runs", "acceptance", name), manifest, TRACE[name], thread, None)
        return digest, md

    def test_example_wait_dominant(self):
        digest, md = self.digest_for("example")
        st = digest["stats"]
        self.assertEqual(digest["capability"], "measurable")
        # 独立计算（稳态窗口 GT）：总自耗时 2.357s；等待类合计 1.939s = 82.26%
        # （GameThreadWaitForTask 单行 78.78% + WaitForTasks 1.69% + WaitUntilTasksComplete 1.58%
        #   + ProcessThreadUntilIdle 0.20%）
        self.assertEqual(st["total_self_s"], 2.357)
        self.assertEqual((st["wait_total"]["self_s"], st["wait_total"]["self_pct"]), (1.939, 82.26))
        self.assertEqual((st["wait_timers"][0]["name"], st["wait_timers"][0]["self_s"],
                          st["wait_timers"][0]["self_pct"], st["wait_timers"][0]["count"]),
                         ("GameThreadWaitForTask", 1.857, 78.78, 480))
        # R5：等待类不进瓶颈候选，Top1 是排除后的第一名
        self.assertEqual(st["top_by_self_time"][0]["name"], "FViewport_Draw")
        self.assertEqual((st["top_by_self_time"][0]["self_s"], st["top_by_self_time"][0]["self_pct"]), (0.094, 4.0))
        self.assertEqual(digest["conclusion"]["confidence"], "clear")
        self.assertIn("82.26%", digest["conclusion"]["text"])
        self.assertIn("瓶颈不在 GT", digest["conclusion"]["text"])
        self.assertEqual(digest["caliber"]["rhi_state"], "real")
        self.assertEqual(digest["caliber"]["bridge_effective"], False)
        self.assertIn("| GameThreadWaitForTask | 1.857 | 78.78% | 480 |", md)

    def test_d3d12_idle_and_merged_frame(self):
        digest, md = self.digest_for("d3d12")
        st = digest["stats"]
        # P21 净化后独立计算：total 10.543s（13 行 GPU 队列行被剔除；净化前 25.033s，GPU 行占一半以上）；
        # 等待类 idle 9.890 + WaitForTasks 0.096 = 9.986s = 94.71%（绝对值不变，分母净化后 39.89%→94.71%）；
        # Top1 UpdateCoreCsvStats_BeginFrame 1.25%——GT 几乎全在等，非等待自耗时极散
        self.assertEqual(st["total_self_s"], 10.543)
        self.assertEqual((st["wait_total"]["self_s"], st["wait_total"]["self_pct"]), (9.986, 94.71))
        self.assertEqual(st["wait_timers"][0]["name"], "Game thread idle time")
        top = st["top_by_self_time"]
        self.assertEqual(top[0]["name"], "UpdateCoreCsvStats_BeginFrame")
        self.assertEqual((top[0]["self_s"], top[0]["self_pct"], top[0]["count"]), (0.132, 1.25, 240))
        # P21：GPU 队列的 "Frame" 行被剔除，Top 里不再出现合并的 Frame（同名合并逻辑由单测覆盖）
        self.assertNotIn("Frame", [r["name"] for r in top])
        self.assertEqual(digest["conclusion"]["confidence"], "clear")
        self.assertEqual(digest["caliber"]["rhi"], "D3D12")
        self.assertIn("Game thread idle time", md)

    def test_b2_smoke_not_measurable(self):
        digest, md = self.digest_for("b2_smoke")
        self.assertEqual(digest["capability"], "not_measurable")
        self.assertIsNone(digest["stats"])
        self.assertEqual(digest["refs"], [])
        self.assertEqual(digest["conclusion"]["confidence"], "none")
        self.assertIn("nullrhi", digest["conclusion"]["text"])
        self.assertNotIn("| # | timer |", md)
        self.assertIn("capability: not_measurable", md)

    def test_deterministic_across_runs(self):
        a, md_a = self.digest_for("example")
        b, md_b = self.digest_for("example")
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))
        self.assertEqual(md_a, md_b)


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class V0RegressionTest(unittest.TestCase):
    """v0 数字核心保持逐字节不变（M1.1 回归基线）。"""

    def test_v0_outputs_unchanged(self):
        lab = os.environ.get("UE_PERF_LAB", r"D:\dsh-output\ue5-perf-lab")
        src = os.path.join(lab, "out3", "stats_GameThread.csv")
        if not os.path.isfile(src):
            self.skipTest("lab baseline missing")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(cpu.main([src, "--out", tmp.name]), 0)
        for name in ("perf_digest.json", "perf_digest.md"):
            with open(os.path.join(tmp.name, name), "rb") as a, \
                 open(os.path.join(lab, "perf_digest.%s" % name.split(".")[1]), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)


if __name__ == "__main__":
    unittest.main()
