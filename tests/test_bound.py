#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_bound.py — M5.1 bound 判定的单元测试（只用内联样本）"""
import json
import unittest

from pipeline.digest.views import bound


def row(name, excl, incl=None, count=1):
    excl = float(excl)
    return {"name": name, "count": count, "incl": float(incl if incl is not None else excl),
            "excl": excl, "imax": 0.0, "iavg": 0.0, "imed": 0.0,
            "emax": excl, "eavg": excl / count}


def caliber():
    return {"run_id": "unit", "profile": None, "rhi": "D3D12", "rhi_state": "real",
            "rhi_source": "log", "build_config": "Development", "stats_window": "steady",
            "window": [10.0, 12.0], "frames_window": [9.0, 12.5], "reference": None,
            "bridge_effective": True, "bridge_requested": True,
            "thread_frames": {"game": 100, "render": 100}, "source_csv": None}


def b2_gpu(graphics_s=24.0, compute_s=31.0):
    return {"gpu_busy_s": graphics_s, "queue_busy": [
        {"name": "Graphics", "timeline": 5, "busy_s": graphics_s},
        {"name": "Compute", "timeline": 9, "busy_s": compute_s}],
        "fences": {"events_in_window": 10, "pair_count": 1,
                   "pairs": [{"signal_queue": 1, "wait_queue": 0, "count": 10,
                              "sum_stall_s": 3.0, "max_stall_ms": 5.0}],
                   "worst": []}}


class ThreadLineTest(unittest.TestCase):
    def test_busy_is_interval_minus_idle(self):
        # M5.1b 校准后：busy 只扣"真空闲"（名字含 idle）。100 帧、idle 2.0s → 20ms/帧；interval 24.6 → busy 4.6ms
        rows = [row("Game thread idle time", 2.0, count=100), row("DrawWork", 1.0, count=100)]
        l = bound.thread_line("GT", rows, 100, 24.6)
        self.assertTrue(l["available"])
        self.assertAlmostEqual(l["wait_per_frame_ms"], 20.0, places=2)
        self.assertAlmostEqual(l["busy_per_frame_ms"], 4.6, places=2)
        self.assertAlmostEqual(l["self_per_frame_ms"], 10.0, places=2)
        self.assertAlmostEqual(l["occupancy"], 4.6 / 24.6, places=3)

    def test_task_wait_is_overlap_not_idle(self):
        """等并行任务是"工作重叠"，不扣 busy —— 这是 5.1b 采样器校准出来的关键修正。"""
        rows = [row("WaitForTasks", 2.0, count=100), row("DrawWork", 1.0, count=100)]
        l = bound.thread_line("GT", rows, 100, 24.6)
        self.assertAlmostEqual(l["wait_per_frame_ms"], 20.0, places=2)      # 仍然报出来
        self.assertAlmostEqual(l["busy_per_frame_ms"], 24.6, places=3)      # 但不扣
        self.assertAlmostEqual(l["overlap_per_frame_ms"], 20.0, places=2)

    def test_zero_wait_busy_equals_interval(self):
        rows = [row("Tick", 24.0, count=100)]
        l = bound.thread_line("GT", rows, 100, 24.6)
        self.assertAlmostEqual(l["busy_per_frame_ms"], 24.6, places=3)
        self.assertAlmostEqual(l["occupancy"], 1.0, places=3)

    def test_missing_rows(self):
        l = bound.thread_line("RHI", [], 100, 24.6)
        self.assertFalse(l["available"])

    def test_wait_over_interval_clamps_to_zero(self):
        rows = [row("Idle", 30.0, count=100)]  # 300ms/帧 wait > interval
        l = bound.thread_line("RT", rows, 100, 24.6)
        self.assertEqual(l["busy_per_frame_ms"], 0.0)


class GpuLineTest(unittest.TestCase):
    def test_graphics_queue_is_the_signal(self):
        # GPU bound 信号 = Graphics 队列（present 路径），不是 max-queue
        l = bound.gpu_line(b2_gpu(graphics_s=24.0, compute_s=31.0), 1293, 24.6)
        self.assertTrue(l["available"])
        self.assertEqual(l["queue"], "Graphics")
        self.assertAlmostEqual(l["busy_per_frame_ms"], 24.0 * 1000 / 1293, places=2)

    def test_saturated_compute_is_context_not_signal(self):
        # Compute 整窗饱和（fill ≥0.95）只进注记，不进排名——A1/B1 实测教训
        l = bound.gpu_line(b2_gpu(graphics_s=24.0, compute_s=32.3), 1293, 24.6)
        self.assertEqual(l["queue"], "Graphics")
        self.assertIsNotNone(l["note"])
        self.assertIn("Compute", l["note"])

    def test_no_b2(self):
        l = bound.gpu_line(None, 1293, 24.6)
        self.assertFalse(l["available"])


def a1_like_lines():
    """A1 实测形态（M5.1b 校准后口径）：

    GT self 23.06ms、RT self 5.25ms、RHI self 3.83ms；三线 busy 都≈interval（100%），
    RT/RHI 的"忙碌"几乎全是等并行任务（17.2 / 21.0 ms/帧，不再从 busy 扣）。
    """
    gt = bound.thread_line("GT", [row("PerfEvidenceRegress", 29.8, count=12930)], 1293, 24.6)
    rt = bound.thread_line("RT", [row("WaitForTasks", 22.18, count=7134), row("DrawWork", 6.8, count=1293)],
                           1293, 24.6)
    rhi = bound.thread_line("RHI", [row("WaitForTasks", 26.94, count=39678),
                                    row("RHIWork", 4.9, count=1293)], 1293, 24.6)
    g = bound.gpu_line(b2_gpu(graphics_s=24.56, compute_s=32.3), 1293, 24.6)
    return [gt, rt, rhi, g]


class VerdictTest(unittest.TestCase):
    def test_a1_shape_saturated_tie_resolved_by_self(self):
        """A1 形态：三条 CPU 线都饱和（M5.1b 校准后"等并行任务"不再扣 busy）。

        这时 busy 没有分辨力，判定改用**自耗时**：GT self 远大于 RT/RHI → 仍指 GT。
        饱和的 Compute 队列只作上下文，不作为 GPU bound 信号（M5.1 的决定不变）。
        """
        lines = a1_like_lines()
        text, conf, kind, detail = bound.verdict(lines, 24.6)
        self.assertEqual(kind, "saturated_tie")
        self.assertEqual(detail["top"], "GT")
        self.assertEqual(detail["ranked_by"], "self_per_frame_ms")
        self.assertIn("自耗时", text)
        g = next(l for l in lines if l["line"] == "GPU")
        self.assertIn("Compute", g["note"])

    def test_single_dominant_clear(self):
        lines = [bound.thread_line("GT", [row("Tick", 30.0)], 100, 24.6),   # 100%
                 bound.thread_line("RT", [row("Idle", 2.0, count=100)], 100, 24.6),  # busy 2.46
                 bound.thread_line("RHI", [row("Idle", 2.0, count=100)], 100, 24.6),
                 bound.gpu_line(b2_gpu(graphics_s=0.5, compute_s=0.5), 100, 24.6)]
        text, conf, kind, detail = bound.verdict(lines, 24.6)
        self.assertEqual(kind, "single")
        self.assertEqual(conf, "clear")
        self.assertEqual(detail["top"], "GT")
        self.assertNotIn("缺失", text)

    def test_missing_line_lowers_confidence(self):
        lines = [bound.thread_line("GT", [row("Tick", 30.0)], 100, 24.6),
                 {"line": "RHI", "available": False},
                 bound.thread_line("RT", [row("Idle", 2.0, count=100)], 100, 24.6)]
        text, conf, kind, detail = bound.verdict(lines, 24.6)
        self.assertEqual(kind, "single")
        self.assertEqual(conf, "moderate")
        self.assertIn("RHI", detail["missing"])

    def test_frame_rate_capped(self):
        # PLAN 公式：无等待的线程 busy=interval（GT 整帧在跑），所以 capped 场景必须有等待类
        # 盖住大部分帧：GT wait 0.9s/100帧=9ms → busy 7.7ms < 预算 16.67
        lines = [bound.thread_line("GT", [row("WaitForFrame", 0.9, count=100)], 100, 16.7),
                 bound.thread_line("RT", [row("Idle", 0.5, count=100)], 100, 16.7),
                 bound.thread_line("RHI", [row("Idle", 0.5, count=100)], 100, 16.7),
                 bound.gpu_line(b2_gpu(graphics_s=0.3, compute_s=0.3), 100, 16.7)]
        text, conf, kind, detail = bound.verdict(lines, 16.7, budget_ms=1000.0 / 60)
        self.assertEqual(kind, "frame_rate_capped")

    def test_over_budget(self):
        # GT busy = 24.6（wait 0）> 预算 16.67 → over_budget
        lines = [bound.thread_line("GT", [row("Tick", 30.0)], 100, 24.6),
                 bound.thread_line("RT", [row("Idle", 2.0, count=100)], 100, 24.6),
                 bound.thread_line("RHI", [row("Idle", 2.0, count=100)], 100, 24.6),
                 bound.gpu_line(b2_gpu(graphics_s=1.0, compute_s=1.0), 100, 24.6)]
        text, conf, kind, detail = bound.verdict(lines, 24.6, budget_ms=1000.0 / 60)
        self.assertEqual(kind, "over_budget")
        self.assertEqual(conf, "clear")
        self.assertEqual(detail["top"], "GT")

    def test_under_budget(self):
        # interval 10ms < 预算 16.67，最忙 GT self 5ms → busy 5ms < 预算 80%
        lines = [bound.thread_line("GT", [row("Tick", 0.5, count=100)], 100, 10.0),
                 bound.thread_line("RT", [row("Idle", 0.2, count=100)], 100, 10.0),
                 bound.thread_line("RHI", [row("Idle", 0.2, count=100)], 100, 10.0),
                 bound.gpu_line(b2_gpu(graphics_s=0.1, compute_s=0.1), 100, 10.0)]
        text, conf, kind, detail = bound.verdict(lines, 10.0, budget_ms=1000.0 / 60)
        self.assertEqual(kind, "under_budget")

    def test_insufficient(self):
        text, conf, kind, detail = bound.verdict([], None)
        self.assertEqual(kind, "insufficient_data")
        self.assertEqual(conf, "none")


class BuildFromDataTest(unittest.TestCase):
    def test_structure_and_determinism(self):
        lines = a1_like_lines()
        d1, md1 = bound.build_from_data(lines, 24.6, caliber(), "steady", [],
                                        fence={"available": True, "stall_per_frame_ms": 15.1,
                                               "stall_ratio": 0.61, "pairs": [], "worst": []})
        d2, md2 = bound.build_from_data([dict(l) for l in lines], 24.6, caliber(), "steady", [],
                                        fence={"available": True, "stall_per_frame_ms": 15.1,
                                               "stall_ratio": 0.61, "pairs": [], "worst": []})
        self.assertEqual(json.dumps(d1, sort_keys=True), json.dumps(d2, sort_keys=True))
        self.assertEqual(md1, md2)
        self.assertEqual(d1["kind"], "bound")
        self.assertEqual(len(d1["stats"]["lines"]), 4)
        self.assertIn("| GT |", md1)
        self.assertIn("| RHI |", md1)
        self.assertTrue(any(r.startswith("bound:GT") for r in d1["refs"]))

    def test_not_measurable_no_numbers(self):
        manifest = {"measurable": False, "measurable_reason": "nullrhi run: smoke only",
                    "run_id": "u",
                    "engine": {"rhi": None, "rhi_state": "null", "rhi_source": "command_line",
                               "build_config": "Development"},
                    "profile": None, "windows": {"frames": [9.0, 12.5]},
                    "window_basis": {"stats_window": None, "reference": None},
                    "trace": {"statnamedevents_effective": True},
                    "frame_semantics": {"game": {"window_frames": 0}, "render": {"window_frames": 0}},
                    "warnings": []}
        digest, md = bound.not_measurable_digest(manifest)
        self.assertEqual(digest["capability"], "not_measurable")
        self.assertIsNone(digest["stats"])
        self.assertNotIn("| GT", md)          # 四线表不出现
        self.assertNotIn("每帧忙", md)         # 结论里没有性能数字


if __name__ == "__main__":
    unittest.main()
