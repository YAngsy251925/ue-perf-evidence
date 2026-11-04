#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优化建议层（advisor）：证据锚定、杠杆来自目录、可证伪。"""
import json
import unittest

from pipeline.advice import advisor


CATALOG = {"cvars": [
    {"name": "r.BloomQuality", "default": "5", "help": "Bloom quality", "file": "Bloom.cpp", "line": 12},
    {"name": "r.Bloom.ScreenPercentage", "default": "100.0f", "help": "FFT bloom resolution",
     "file": "Bloom.cpp", "line": 30},
    {"name": "r.Lumen.Reflections.Allow", "default": "1", "help": "allow lumen reflections",
     "file": "LumenReflections.cpp", "line": 8},
    {"name": "r.TSR.SomethingElse", "default": "0", "help": "x", "file": "TSR.cpp", "line": 3},
    {"name": "r.RDG.AsyncCompute", "default": "1", "help": "async compute policy",
     "file": "RenderGraphPrivate.cpp", "line": 212},
], "missing": False, "path": "catalog/cvars.json"}


def gpu_digest(rows, total_s):
    return {"stats": {"top_by_self_time": rows, "total_self_s": total_s}}


def bound_digest(lines, fence=None):
    return {"stats": {"lines": lines, "fence": fence or {}}}


def cpu_digest(rows):
    return {"stats": {"top_by_self_time": rows}}


class GpuEffectTest(unittest.TestCase):
    def test_bloom_share_produces_hypothesis_with_cvars(self):
        gpu = gpu_digest([{"name": "Bloom", "self_s": 17.4, "self_pct": 5.3}], 324.9)
        hyp = advisor.detect_gpu_effects(gpu, CATALOG, 28.31)
        ids = [h["id"] for h in hyp]
        self.assertIn("gpu_effect_share:bloom", ids)
        bloom = next(h for h in hyp if h["id"].endswith("bloom"))
        self.assertEqual(bloom["evidence"]["share_of_gpu_work_pct"], 5.3)
        self.assertGreater(bloom["expected_saving_ms"][1], bloom["expected_saving_ms"][0])
        self.assertTrue(bloom["levers"], "应从目录里查到 r.Bloom* 开关")
        self.assertTrue(bloom["levers"][0]["where"].startswith("Bloom.cpp"))

    def test_below_threshold_is_ignored(self):
        gpu = gpu_digest([{"name": "Nanite::BasePass", "self_s": 1.0, "self_pct": 0.5}], 100.0)
        self.assertEqual(advisor.detect_gpu_effects(gpu, CATALOG, 28.0), [])

    def test_unknown_effect_has_no_levers_but_still_reported(self):
        gpu = gpu_digest([{"name": "WeirdCustomPass", "self_s": 5.0, "self_pct": 8.0}], 60.0)
        self.assertEqual(advisor.detect_gpu_effects(gpu, CATALOG, 20.0), [])


class BoundTest(unittest.TestCase):
    def test_multi_saturated_and_fence(self):
        b = bound_digest(
            [{"line": "GT", "available": True, "occupancy": 1.0, "busy_per_frame_ms": 31.8,
              "self_per_frame_ms": 22.2},
             {"line": "RT", "available": True, "occupancy": 1.0, "busy_per_frame_ms": 31.9,
              "self_per_frame_ms": 11.7}],
            fence={"stall_per_frame_ms": 19.2, "stall_ratio": 0.72, "worst": []})
        hyp = advisor.detect_bound(b, 26.4, CATALOG)
        ids = [h["id"] for h in hyp]
        self.assertIn("bound_saturated_multi", ids)
        self.assertIn("fence_cross_queue", ids)
        fence = next(h for h in hyp if h["id"] == "fence_cross_queue")
        self.assertAlmostEqual(fence["evidence"]["ratio_pct"], 72.7, places=1)
        self.assertTrue(fence["levers"])

    def test_no_saturation_no_fence(self):
        b = bound_digest([{"line": "GT", "available": True, "occupancy": 0.4,
                           "busy_per_frame_ms": 10.0, "self_per_frame_ms": 9.0}], fence={})
        self.assertEqual(advisor.detect_bound(b, 26.4, CATALOG), [])


class CpuTest(unittest.TestCase):
    def test_ui_dominant(self):
        rows = [{"name": "Slate::Prepass", "self_s": 16.7},
                {"name": "WinPumpMessages", "self_s": 21.7},
                {"name": "Gameplay", "self_s": 10.0}]
        hyp = advisor.detect_cpu_hotspots(cpu_digest(rows), 26.4, CATALOG)
        ids = [h["id"] for h in hyp]
        self.assertIn("cpu_ui_dominant", ids)

    def test_single_hotspot(self):
        rows = [{"name": "Tick_X", "self_s": 30.0}, {"name": "Y", "self_s": 5.0},
                {"name": "Z", "self_s": 5.0}]
        hyp = advisor.detect_cpu_hotspots(cpu_digest(rows), 26.4, CATALOG)
        self.assertIn("cpu_single_hotspot", [h["id"] for h in hyp])

    def test_dispersion(self):
        rows = [{"name": "A%d" % i, "self_s": 5.0} for i in range(10)]
        hyp = advisor.detect_cpu_hotspots(cpu_digest(rows), 26.4, CATALOG)
        self.assertIn("cpu_dispersion", [h["id"] for h in hyp])


class RenderTest(unittest.TestCase):
    def test_md_has_evidence_and_invalidation(self):
        result = {"schema": "perf-advice/0", "run_dir": "D:/r", "hypotheses": [
            {"id": "x", "kind": "reduce_effect_cost", "subject": "Bloom",
             "evidence": {"share_of_gpu_work_pct": 5.3}, "expected_saving_ms": [0.5, 0.9],
             "levers": [{"name": "r.BloomQuality", "default": "5", "help": "Bloom quality",
                         "where": "Bloom.cpp:12"}],
             "verification": "跑 A/B", "invalidation": "噪声内即证伪"}],
            "context": {"interval_p50_ms": 26.4, "gpu_graphics_busy_ms": 28.3,
                        "catalog": "catalog/cvars.json", "catalog_cvars": 5}}
        md = advisor.render_md(result)
        self.assertIn("**证据**", md)
        self.assertIn("**证伪条件**", md)
        self.assertIn("r.BloomQuality", md)


if __name__ == "__main__":
    unittest.main()
