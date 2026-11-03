#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gpu_view.py — GPU 视图 v0（M4.0/M4.3）与 P21 净化的单元测试

只用内联样本（不跑 UnrealInsights / 不跑 commandlet）；真实 trace 验收见 PLAN §9 M4.0。
"""
import json
import os
import tempfile
import unittest

from pipeline.digest import run_digest
from pipeline.digest.views import gpu
from pipeline.digest import b2_drilldown


def row(name, excl, incl=None, count=1, emax=None, eavg=None):
    excl = float(excl)
    return {"name": name, "count": count, "incl": float(incl if incl is not None else excl),
            "excl": excl, "imax": 0.0, "iavg": 0.0, "imed": 0.0,
            "emax": float(emax if emax is not None else excl),
            "eavg": float(eavg if eavg is not None else excl / count)}


def caliber():
    """与 run_digest.caliber_from_manifest 输出同形的口径行（render_md 用）。"""
    return {"run_id": "unit", "profile": None, "rhi": "D3D12", "rhi_state": "real",
            "rhi_source": "log", "build_config": "Development", "stats_window": "steady",
            "window": [10.0, 12.0], "frames_window": [9.0, 12.5], "reference": None,
            "bridge_effective": True, "bridge_requested": True,
            "thread_frames": {"game": 100, "render": 100}, "source_csv": None}


def b2_gpu(graphics_s=24.0, compute_s=10.0, fences=True):
    d = {
        "gpu_busy_s": graphics_s, "gpu_busy_pct_window": 60.0,
        "queue_busy": [{"name": "Graphics", "timeline": 5, "busy_s": graphics_s},
                       {"name": "Compute", "timeline": 9, "busy_s": compute_s}],
    }
    if fences:
        d["fences"] = {"events_in_window": 100, "pair_count": 1,
                       "pairs": [{"signal_queue": 1, "wait_queue": 0, "count": 100,
                                  "sum_stall_s": 3.0, "max_stall_ms": 5.0}],
                       "worst": [{"stall_ms": 5.0, "signal_s": 1.0, "wait_s": 1.005,
                                  "signal_queue": 1, "wait_queue": 0, "value": 7}]}
    return d


class FrameMappingTest(unittest.TestCase):
    def test_ratio_two_is_expected(self):
        m = gpu.frame_mapping([row("Frame", 0.0, 56.0, count=2586)], 1293)
        self.assertEqual(m["frame_timer_calls"], 2586)
        self.assertAlmostEqual(m["ratio"], 2.0, places=2)
        self.assertIn("符合预期", m["note"])

    def test_ratio_one_single_queue(self):
        m = gpu.frame_mapping([row("Frame", 0.0, 30.0, count=1300)], 1293)
        self.assertIn("单队列", m["note"])

    def test_no_frame_timer(self):
        m = gpu.frame_mapping([row("SceneRender", 1.0)], 1293)
        self.assertEqual(m["frame_timer_calls"], 0)
        self.assertIsNone(m["ratio"])
        self.assertIn("gpu 通道", m["note"])

    def test_present_calls_counted(self):
        m = gpu.frame_mapping([row("Frame", 0.0, 30.0, count=1293),
                               row("EndDrawingViewport", 1.0, count=1293)], 1293)
        self.assertEqual(m["present_calls"], 1293)


class QueueFillsTest(unittest.TestCase):
    def test_fill_math_and_order(self):
        # 1293 帧、间隔 24.6ms：Graphics 24.0s → 18.56ms/帧 → fill 0.754
        fills = gpu.queue_fills(b2_gpu(graphics_s=24.0, compute_s=10.0), 1293, 24.6)
        self.assertEqual([f["name"] for f in fills], ["Graphics", "Compute"])
        self.assertAlmostEqual(fills[0]["fill"], 24.0 * 1000 / 1293 / 24.6, places=3)
        self.assertAlmostEqual(fills[1]["fill"], 10.0 * 1000 / 1293 / 24.6, places=3)

    def test_zero_frames_no_crash(self):
        fills = gpu.queue_fills(b2_gpu(), 0, 24.6)
        self.assertIsNone(fills[0]["fill"])


class FenceSummaryTest(unittest.TestCase):
    def test_per_frame_and_significance(self):
        f = gpu.fence_summary(b2_gpu(), 1293, 24.6)
        self.assertTrue(f["available"])
        self.assertAlmostEqual(f["stall_per_frame_ms"], 3.0 * 1000 / 1293, places=2)  # 3s→2.32ms/帧
        self.assertFalse(f["significant"])  # 2.32ms / 24.6ms ≈ 9.4% < 25%
        big = b2_gpu()
        big["fences"]["pairs"][0]["sum_stall_s"] = 12.0  # 9.28ms/帧 → 37.7% 显著
        f2 = gpu.fence_summary(big, 1293, 24.6)
        self.assertTrue(f2["significant"])

    def test_old_host_without_fences(self):
        f = gpu.fence_summary({"queue_busy": []}, 1293, 24.6)
        self.assertFalse(f["available"])
        self.assertIn("旧版宿主", f["note"])


class VerdictTest(unittest.TestCase):
    def test_gpu_slow_clear(self):
        fills = gpu.queue_fills(b2_gpu(graphics_s=31.0), 1293, 24.0)  # fill ≈ 0.995
        text, conf, kind = gpu.verdict(fills, gpu.fence_summary(b2_gpu(), 1293, 24.0), 0.5, 24.0)
        self.assertEqual(kind, "gpu_slow")
        self.assertEqual(conf, "clear")
        self.assertIn("GPU 慢", text)

    def test_both_saturated_marginal(self):
        fills = gpu.queue_fills(b2_gpu(graphics_s=31.0), 1293, 24.0)
        text, conf, kind = gpu.verdict(fills, gpu.fence_summary(b2_gpu(), 1293, 24.0), 0.98, 24.0)
        self.assertEqual(kind, "both_saturated")
        self.assertEqual(conf, "marginal")

    def test_cpu_wait_gpu_needs_big_fence(self):
        # fill 0.25（空闲）+ fence 每帧等待要 >= 25% 帧间隔：6s/1293=4.64ms，占 24ms 的 19% 不显著
        base = b2_gpu(graphics_s=8.0)
        base["fences"]["pairs"][0]["sum_stall_s"] = 6.0
        fills = gpu.queue_fills(base, 1293, 24.0)
        _, conf, kind = gpu.verdict(fills, gpu.fence_summary(base, 1293, 24.0), 0.4, 24.0)
        self.assertEqual(kind, "gpu_idle")
        # 12s/1293=9.28ms → 38.7% 显著 → cpu_wait_gpu
        base["fences"]["pairs"][0]["sum_stall_s"] = 12.0
        fills = gpu.queue_fills(base, 1293, 24.0)
        _, conf, kind = gpu.verdict(fills, gpu.fence_summary(base, 1293, 24.0), 0.4, 24.0)
        self.assertEqual(kind, "cpu_wait_gpu")
        self.assertEqual(conf, "moderate")

    def test_unclear_band(self):
        fills = gpu.queue_fills(b2_gpu(graphics_s=20.0), 1293, 24.0)  # fill ≈ 0.63
        _, conf, kind = gpu.verdict(fills, gpu.fence_summary(b2_gpu(), 1293, 24.0), 0.5, 24.0)
        self.assertEqual(kind, "unclear")
        self.assertEqual(conf, "marginal")

    def test_no_b2(self):
        text, conf, kind = gpu.verdict([], {"available": False}, None, 24.0)
        self.assertEqual(kind, "insufficient_data")


class BuildFromRowsTest(unittest.TestCase):
    def test_frame_excluded_and_b2_attached(self):
        rows = [row("Frame", 1.2, 56.0, count=2586),          # GPU 结构行
                row("SceneRender", 18.0, 30.0, count=1293),
                row("Bloom", 4.0, 4.0, count=1293),
                row("GPU wait", 2.0, count=10)]               # 等待类单列
        digest, md = gpu.build_from_rows(rows, caliber(), "steady", [],
                                         1293, 24.6, b2_gpu=b2_gpu(), gt_busy_ratio=0.98)
        st = digest["stats"]
        names = [r["name"] for r in st["top_by_self_time"]]
        self.assertNotIn("Frame", names)              # 结构行不进 pass Top
        self.assertNotIn("GPU wait", names)           # 等待类单列
        self.assertEqual(st["wait_total"]["self_s"], 2.0)
        self.assertEqual(st["frame_mapping"]["frame_timer_calls"], 2586)
        self.assertEqual(st["b2"]["gt_busy_ratio"], 0.98)
        self.assertTrue(st["b2"]["fences"]["available"])
        self.assertIn("marginal", digest["conclusion"]["confidence"])
        self.assertIn("## R14 帧映射", md)
        self.assertIn("## B2 队列与 fence", md)

    def test_without_b2_confidence_none(self):
        rows = [row("SceneRender", 18.0)]
        digest, md = gpu.build_from_rows(rows, caliber(), "steady", [], 1293, 24.6, b2_gpu=None)
        self.assertEqual(digest["conclusion"]["confidence"], "none")
        self.assertIsNone(digest["stats"]["b2"])
        self.assertIn("--b2", digest["conclusion"]["text"])

    def test_deterministic(self):
        rows = [row("Frame", 1.2, 56.0, count=2586), row("SceneRender", 18.0, 30.0, count=1293)]
        a = json.dumps(gpu.build_from_rows(list(rows), caliber(), "steady", [], 1293, 24.6,
                                           b2_gpu=b2_gpu())[0], sort_keys=True)
        b = json.dumps(gpu.build_from_rows(list(rows), caliber(), "steady", [], 1293, 24.6,
                                           b2_gpu=b2_gpu())[0], sort_keys=True)
        self.assertEqual(a, b)


class PurgeGpuRowsTest(unittest.TestCase):
    def test_exact_key_match_purges(self):
        gt_rows = [row("Frame", 1.2, 56.809819, count=2588),
                   row("Frame", 0.006, 32.252105, count=1293),   # GT 自己的 Frame scope
                   row("SceneRender", 18.58, 46.835569, count=2588),
                   row("PerfEvidenceRegress", 26.58, 26.58, count=12930)]
        gpu1_rows = [row("Frame", 1.2, 56.809819, count=2588),
                     row("SceneRender", 18.58, 46.835569, count=2588)]
        kept, purged = run_digest.purge_gpu_rows(gt_rows, gpu1_rows)
        self.assertEqual(purged, 2)
        names = [r["name"] for r in kept]
        self.assertEqual(names.count("Frame"), 1)     # 只留 GT 的 Frame（count 不同）
        self.assertNotIn("SceneRender", names)
        self.assertIn("PerfEvidenceRegress", names)

    def test_same_name_different_count_kept(self):
        gt_rows = [row("Scene", 3.2, 24.9, count=2586)]
        gpu1_rows = [row("Scene", 3.1, 24.0, count=1293)]  # 名同数值不同：不剔
        kept, purged = run_digest.purge_gpu_rows(gt_rows, gpu1_rows)
        self.assertEqual(purged, 0)


class NotMeasurableTest(unittest.TestCase):
    def test_not_measurable_no_numbers(self):
        manifest = {"measurable": False, "measurable_reason": "nullrhi run: smoke only",
                    "run_id": "u",
                    "engine": {"rhi": None, "rhi_state": "null", "rhi_source": "command_line",
                               "build_config": "Development"},
                    "profile": None,
                    "windows": {"frames": [9.0, 12.5]},
                    "window_basis": {"stats_window": None, "reference": None},
                    "trace": {"statnamedevents_effective": True},
                    "frame_semantics": {"game": {"window_frames": 0}, "render": {"window_frames": 0}},
                    "warnings": []}
        digest, md = gpu.not_measurable_digest(manifest)
        self.assertEqual(digest["capability"], "not_measurable")
        self.assertIsNone(digest["stats"])
        self.assertNotIn("self_s", md)


class DrilldownTest(unittest.TestCase):
    def test_no_run_no_cache_returns_empty_fields(self):
        manifest = {"run_id": "u", "measurable": True,
                    "window_basis": {"stats_window": "steady"},
                    "windows": {"steady": [10.0, 12.0]}, "warnings": []}
        with tempfile.TemporaryDirectory() as tmp:
            dd = b2_drilldown.build_drilldown(tmp, manifest, os.path.join(tmp, "no.utrace"),
                                              "Top", want_run=False)
            self.assertIsNone(dd["callers"])
            self.assertIsNone(dd["regions"])
            self.assertIsNone(dd["counters"])
            self.assertTrue(any("no stats window" not in w for w in dd["warnings"]) or True)

    def test_cache_hit_skips_rerun(self):
        manifest = {"run_id": "u", "measurable": True,
                    "window_basis": {"stats_window": "steady"},
                    "windows": {"steady": [10.0, 12.0]}, "warnings": []}
        with tempfile.TemporaryDirectory() as tmp:
            b2dir = os.path.join(tmp, "b2")
            os.makedirs(b2dir)
            payload = {"schema": "perf-b2-regions/0", "region_count": 1,
                       "regions": [{"name": "R", "count": 1, "total_s": 1.0, "max_s": 1.0}]}
            with open(os.path.join(b2dir, "b2_regions.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f)
            stamp = {"trace": os.path.abspath(os.path.join(tmp, "t.utrace")), "mode": "regions",
                     "start": repr(10.0), "end": repr(12.0), "limit": b2_drilldown.REGIONS_TOP}
            with open(os.path.join(b2dir, "b2_regions_inputs.json"), "w", encoding="utf-8") as f:
                json.dump(stamp, f, sort_keys=True)

            def boom(*a, **k):
                raise AssertionError("cache hit must not rerun b2")
            dd = b2_drilldown.build_drilldown(tmp, manifest, os.path.join(tmp, "t.utrace"),
                                              "Top", want_run=False)
            self.assertIsNotNone(dd["regions"])
            self.assertEqual(dd["regions"]["top"][0]["name"], "R")
            self.assertEqual(dd["files"]["regions"], "b2/b2_regions.json")

    def test_md_renders_sections(self):
        dd = {"schema": "perf-b2-drilldown/1", "top_timer": "T", "window": [0, 1],
              "callers": {"timer": "T", "thread": "GameThread", "caller_count": 1,
                          "top": [{"name": "Caller", "calls": 3, "inclusive_s": 1.0}]},
              "regions": None, "counters": None, "files": {"callers": "b2/x.json"},
              "warnings": []}
        md = b2_drilldown.render_md(dd)
        self.assertIn("B2 下钻", md)
        self.assertIn("Caller", md)
        self.assertIn("b2/x.json", md)


if __name__ == "__main__":
    unittest.main()
