#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.2 B2 深度读取的 Python 侧（不跑引擎）：JSON 摘要、interval 计算、与 A 路对账。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.extract import b2


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


if __name__ == "__main__":
    unittest.main()
