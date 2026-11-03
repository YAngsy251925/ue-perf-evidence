#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.2 B2 深度读取的 Python 侧（不跑引擎）：JSON 摘要、interval 计算、与 A 路对账。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.extract import b2

A_HEADER = "Name,Count,C.Avg,Incl,I.Min,I.Max,I.Avg,I.Med,Excl,E.Min,E.Max,E.Avg,E.Med"


def frames_payload(starts):
    frames = [{"idx": i, "start_ms": s, "end_ms": s + 10.0, "duration_ms": 10.0}
              for i, s in enumerate(starts)]
    return {"schema": "perf-b2-frames/0", "trace": "t.utrace", "duration_ms": starts[-1],
            "game": {"count": len(frames), "frames": frames, "emitted": len(frames)},
            "render": {"count": len(frames), "frames": frames, "emitted": len(frames)}}


class FramesSummaryTest(unittest.TestCase):
    def test_interval_p50_uses_intervals_not_durations(self):
        # 间隔 16/17/18ms，帧 duration 恒为 10ms —— p50 必须是 17，不能是 10
        payload = frames_payload([0.0, 16.0, 33.0, 51.0])
        summary = b2.summarize_frames(payload)
        self.assertEqual(summary["game_frames"], 4)
        self.assertEqual(summary["interval_n"], 3)
        self.assertEqual(summary["interval_p50_ms"], 17.0)

    def test_limit_truncates_intervals(self):
        payload = frames_payload([0.0, 10.0, 20.0, 30.0])
        self.assertEqual(b2.summarize_frames(payload, limit=2)["interval_n"], 1)

    def test_single_frame_has_no_interval(self):
        summary = b2.summarize_frames(frames_payload([0.0]))
        self.assertEqual(summary["interval_n"], 0)
        self.assertIsNone(summary["interval_p50_ms"])


class CrossCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = self.tmp.name
        self.trace = os.path.join(self.run_dir, "trace.utrace")
        with open(self.trace, "wb") as f:
            f.write(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def write_manifest(self, game, render, timers=None):
        payload = {"frames_seen": {"game_frame_track": game, "rendering_frame_track": render}}
        if timers is not None:
            payload["trace"] = {"timer_count": timers}
        with io.open(os.path.join(self.run_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_match(self):
        self.write_manifest(3, 3)
        self.assertIsNotNone(b2.manifest_beside(self.trace))
        checks = b2.cross_check({"game_frames": 3, "render_frames": 3}, b2.manifest_beside(self.trace))
        self.assertTrue(all(v[0] for v in checks.values()))

    def test_mismatch_is_reported(self):
        self.write_manifest(3, 4)
        checks = b2.cross_check({"game_frames": 3, "render_frames": 3}, b2.manifest_beside(self.trace))
        self.assertTrue(checks["game_frames"][0])
        self.assertFalse(checks["render_frames"][0])

    def test_no_manifest_returns_none(self):
        self.assertIsNone(b2.manifest_beside(self.trace))
        self.assertIsNone(b2.cross_check({"game_frames": 1}, None))


def timerstats_payload():
    return {
        "schema": "perf-b2-timerstats/0", "span_s": 32.0, "thread": "GameThread",
        "totals": {"sum_self_s": 31.9, "sum_inclusive_s": 200.0, "timer_count": 1879},
        "emitted": 3,
        "timers": [
            {"name": "PerfEvidenceRegress", "self_s": 26.584, "inclusive_s": 26.584, "calls": 12930},
            {"name": "WaitForTasks", "self_s": 0.254, "inclusive_s": 0.254, "calls": 1282},
            {"name": "FViewport_Draw", "self_s": 0.143, "inclusive_s": 0.143, "calls": 900},
        ],
    }


def write_a_route(dir_path, rows):
    """rows: [(name, incl, excl)]，写一份 A 路窗口导出 CSV。"""
    path = os.path.join(dir_path, "a.csv")
    lines = [A_HEADER]
    for name, incl, excl in rows:
        lines.append(",".join([name, "1", "1", "%.6f" % incl, "0", "0", "0", "0",
                               "%.6f" % excl, "0", "0", "0", "0"]))
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


class TimerStatsTest(unittest.TestCase):
    def test_summary_coverage(self):
        s = b2.summarize_timerstats(timerstats_payload())
        self.assertEqual(s["timer_count"], 1879)
        self.assertAlmostEqual(s["coverage_pct"], 99.688, places=3)
        self.assertEqual(s["top"][0], "PerfEvidenceRegress")

    def test_compare_matches_shared_names_only(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = write_a_route(d, [
                ("PerfEvidenceRegress", 26.584, 26.584),
                ("FViewport_Draw", 0.143, 0.143),
                ("SceneRender", 46.83, 18.58),   # 只在 A 路（Stats 区块视图），不参与对账
            ])
            comp = b2.compare_a_route(timerstats_payload(), csv_path)
        self.assertTrue(comp["ok"])
        self.assertEqual(comp["matched_n"], 2)
        self.assertAlmostEqual(comp["worst_rel_pct"], 0.0, places=6)

    def test_compare_flags_real_drift(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = write_a_route(d, [("PerfEvidenceRegress", 20.0, 20.0)])  # 差 33%
            comp = b2.compare_a_route(timerstats_payload(), csv_path)
        self.assertFalse(comp["ok"])
        self.assertGreater(comp["worst_rel_pct"], 30.0)


class CommandLineTest(unittest.TestCase):
    """命令行必须用空格形态（P19：cmd 把 '=' 当分隔符），out 用绝对路径。"""

    def test_space_form_and_no_equals_in_custom_args(self):
        line = b2.build_cmdline("gpu", "D:/t/trace.utrace", "D:/o/b2_gpu.json",
                                "D:/o/b2_gpu.log", limit=8, start=1.5, end=2.5,
                                thread="GameThread", ue_root="D:/UE")
        self.assertIn("-run=TraceDigestPerf", line)
        self.assertIn("mode gpu", line)
        self.assertIn("out \"D:/o/b2_gpu.json\"", line)
        self.assertIn("limit 8", line)
        self.assertIn("start 1.500000", line)
        self.assertIn("end 2.500000", line)
        self.assertIn("thread GameThread", line)
        self.assertIn("-nullrhi", line)
        # 自定义 key 不能用 key=value 形态（cmd 会拆开）
        for token in ("mode=", "out=", "limit=", "start=", "end=", "thread="):
            self.assertNotIn(token, line)

    def test_timer_arg_is_quoted(self):
        line = b2.build_cmdline("callers", "D:/t/trace.utrace", "D:/o/c.json", "D:/o/c.log",
                                timer="W_LyraFrontEnd_C /Engine/T", ue_root="D:/UE")
        self.assertIn('timer "W_LyraFrontEnd_C /Engine/T"', line)

    def test_all_modes_are_listed(self):
        for mode in ("frames", "timers", "timerstats", "callers", "gpu", "counters",
                     "regions", "logs", "channels", "tasks", "memory", "allocations",
                     "net", "load"):
            self.assertIn(mode, b2.MODES)


if __name__ == "__main__":
    unittest.main()
