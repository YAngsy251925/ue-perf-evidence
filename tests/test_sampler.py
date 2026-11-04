#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.1b：每帧采样器的读取、切片与 bound 校准对照。"""
import io
import os
import tempfile
import unittest

from pipeline.digest.views import sampler


def write(d, name, text):
    path = os.path.join(d, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


HEADER = "frame,game_ms,render_ms,game_wait_ms,render_wait_ms,gpu_ms\n"


def rows_text(n, game=2.0, render=3.0, gpu=1.5, gwait=0.5, rwait=0.1):
    return HEADER + "".join("%d,%.3f,%.3f,%.3f,%.3f,%.3f\n" % (i, game, render, gwait, rwait, gpu)
                            for i in range(n))


class ReadTest(unittest.TestCase):
    def test_summary_means(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, os.path.join("sampler", "a.csv"), rows_text(10, game=2.0, gpu=1.5))
            cal = sampler.load(d)
        s = cal["summary"]
        self.assertEqual(cal["rows"], 10)
        self.assertAlmostEqual(s["game_thread_ms"]["mean"], 2.0)
        self.assertAlmostEqual(s["gpu_ms"]["mean"], 1.5)

    def test_busy_is_game_time_not_game_minus_wait(self):
        # RenderTimer.h 的 GGameThreadTime 已"不含空闲"，再减 wait 会重复扣除
        with tempfile.TemporaryDirectory() as d:
            write(d, os.path.join("sampler", "a.csv"), rows_text(4, game=2.0, gwait=1.0))
            cal = sampler.load(d)
        self.assertAlmostEqual(cal["summary"]["game_busy_derived_ms"]["mean"], 2.0)
        self.assertAlmostEqual(cal["summary"]["game_wait_ms"]["mean"], 1.0)

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sampler.load(d))


class SliceTest(unittest.TestCase):
    def test_tail_alignment_uses_last_n_rows(self):
        rows = [{"frame": i, "game_ms": float(i)} for i in range(100)]
        sliced, basis = sampler.slice_to_window(rows, 10)
        self.assertEqual(basis, "tail_aligned")
        self.assertEqual([r["frame"] for r in sliced], list(range(90, 100)))

    def test_short_file_is_whole_run(self):
        rows = [{"frame": i, "game_ms": 1.0} for i in range(5)]
        sliced, basis = sampler.slice_to_window(rows, 50)
        self.assertEqual(basis, "whole_run")
        self.assertEqual(len(sliced), 5)

    def test_no_window_keeps_everything(self):
        rows = [{"frame": i} for i in range(7)]
        self.assertEqual(sampler.slice_to_window(rows, None)[1], "whole_run")


class CompareTest(unittest.TestCase):
    def summary(self, game=2.0, render=3.0, gpu=1.5):
        return {"game_thread_ms": {"mean": game}, "render_thread_ms": {"mean": render},
                "gpu_ms": {"mean": gpu}, "game_busy_derived_ms": {"mean": game}}

    def test_matches_gt_rt_labels(self):
        # 2.0 vs 1.95 = +2.6%（agree 区）；3.0 vs 3.1 = -3.2%（agree）
        lines = [{"line": "GT", "busy_per_frame_ms": 1.95, "available": True},
                 {"line": "RT", "busy_per_frame_ms": 3.1, "available": True}]
        out = sampler.compare(self.summary(), lines)["lines"]
        self.assertEqual([r["line"] for r in out], ["GT", "RT"])
        self.assertEqual([r["verdict"] for r in out], ["agree", "agree"])

    def test_disagree_when_far_off(self):
        lines = [{"line": "RT", "busy_per_frame_ms": 2.0, "available": True}]
        out = sampler.compare(self.summary(render=3.0), lines)["lines"]
        self.assertEqual(out[0]["verdict"], "disagree")
        self.assertGreater(out[0]["diff_pct"], 15.0)

    def test_missing_line_is_skipped(self):
        lines = [{"line": "GT", "busy_per_frame_ms": 2.0, "available": True}]
        out = sampler.compare(self.summary(), lines)["lines"]
        self.assertEqual([r["line"] for r in out], ["GT"])

    def test_unavailable_trace_line_skipped(self):
        lines = [{"line": "GPU", "available": False, "note": "no b2"}]
        self.assertEqual(sampler.compare(self.summary(), lines)["lines"], [])


class RenderTest(unittest.TestCase):
    def test_render_calibration_without_data(self):
        md = sampler.render_calibration({"available": False})
        self.assertIn("没有 `sampler/*.csv`", md)

    def test_render_calibration_table(self):
        section = {"available": True, "file": "a.csv", "rows": 10,
                   "compare": {"lines": [{"line": "GT", "trace_busy_ms": 1.3, "sampler_busy_ms": 1.6,
                                          "diff_ms": 0.3, "diff_pct": 23.0, "verdict": "disagree"}]}}
        md = sampler.render_calibration(section)
        self.assertIn("| GT | 1.300 ms | 1.600 ms |", md)
        self.assertIn("偏差超 15%", md)


if __name__ == "__main__":
    unittest.main()
