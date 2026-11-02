#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_windows.py — M1.3 时间窗切分的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
单元测试只用内联样本；验收测试读取本机的实测 run，路径不存在时跳过。
"""
import os
import tempfile
import unittest

from pipeline.caliber.frame_probe import count_overlaps, frames_seen, load_frame_series, pick_reference
from pipeline.caliber.insights_log import parse_duration_text, session_duration_from_text
from pipeline.caliber.windows import split_windows
from tests.acceptance_data import accept_manifest, have_acceptance_data

HEADER = "ThreadName,TimerName,StartTime,EndTime,Duration,Depth\n"


def write_extract(dirpath, track_rows, scope_rows):
    for name, rows in (("frame_tracks.csv", track_rows), ("frame_scopes.csv", scope_rows)):
        with open(os.path.join(dirpath, name), "w", encoding="utf-8", newline="") as f:
            f.write(HEADER + "".join(row + "\n" for row in rows))


def make_reference(frames, nonfinite=()):
    return {"side": "game", "source": "frame_track", "timer": "Game Frame N", "threads": ["Game Frames"],
            "rows": len(frames) + len(nonfinite), "frames": list(frames), "nonfinite": list(nonfinite)}


def even_frames(n, start=5.0, step=0.01):
    return [(start + i * step, start + (i + 1) * step, step, i) for i in range(n)]


def frames_from_spans(spans):
    return [(s, e, e - s, i) for i, (s, e) in enumerate(spans)]


class DurationTextTest(unittest.TestCase):
    def check(self, text, seconds, lo, hi):
        d = parse_duration_text(text)
        self.assertAlmostEqual(d["seconds"], seconds, places=9)
        self.assertAlmostEqual(d["bounds"][0], lo, places=9)
        self.assertAlmostEqual(d["bounds"][1], hi, places=9)

    def test_seconds_rounded_to_two_decimals(self):
        self.check("15.48s", 15.48, 15.475, 15.485)
        self.check("15s", 15.0, 14.995, 15.005)  # 尾零被去掉的 "15.00s"

    def test_minutes(self):
        self.check("1m 15.5s", 75.5, 75.45, 75.55)
        self.check("1m", 60.0, 59.95, 60.0)  # 秒级分支在 59.95s 起写作 "1m"

    def test_hours_and_days_are_floored(self):
        self.check("2h 5m", 7500.0, 7500.0, 7560.0)
        self.check("3h", 10800.0, 10800.0, 10860.0)
        self.check("1d 2h", 93600.0, 93600.0, 97200.0)

    def test_sub_second(self):
        self.check("523.45 ms", 0.52345, 0.523445, 0.523455)
        self.check("12.5 \u00b5s", 12.5e-6, 12.495e-6, 12.505e-6)

    def test_unparseable(self):
        for text in ("NaN", "~0", "\u221e", ""):
            self.assertIsNone(parse_duration_text(text), text)
        self.assertEqual(parse_duration_text("0")["seconds"], 0.0)

    def test_log_lines_last_report_wins(self):
        log = ("[x][0]TraceInsights: Analysis has completed in 104.47 ms (148.2X speed; session duration: 15.48s).\n"
               "[x][0]LogTemp: unrelated line\n")
        self.assertEqual(session_duration_from_text(log)["seconds"], 15.48)
        updated = log + ("[x][0]TraceInsights: Warning: The session duration was updated (16.02s) "
                         "after the analysis has been completed.\n")
        self.assertEqual(session_duration_from_text(updated)["seconds"], 16.02)
        self.assertIsNone(session_duration_from_text("no duration here\n"))


class FrameProbeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_rows_threads_sorting_and_nonfinite(self):
        write_extract(self.dir,
                      ["Game Frames,Game Frame 1,1.2,1.3,0.1,0",
                       "Game Frames,Game Frame 0,1.0,1.2,0.2,0",
                       "Game Frames,Game Frame 2,1.3,+inf,+inf,0",
                       "Rendering Frames,Rendering Frame 0,1.05,1.25,0.2,0"],
                      ["GameThread,Frame,1.0,1.2,0.2,0",
                       "RenderThread 1,Frame,1.05,1.25,0.2,0",
                       "RenderThread 0,FrameRender,1.0,1.1,0.1,1",
                       "RHIThread,Frame,1.0,1.01,0.01,0",
                       "RTHeartBeat 0,Frame,1.0,1.01,0.01,0"])
        s = load_frame_series(self.dir)
        game = s[("game", "frame_track")]
        self.assertEqual(game["frames"], [(1.0, 1.2, 0.2, 0), (1.2, 1.3, 0.1, 1)])  # (start, end, duration, track_index)
        self.assertEqual((game["rows"], game["nonfinite"]), (3, [(1.3, 2)]))
        self.assertEqual(s[("game", "frame_scope")]["frames"], [(1.0, 1.2, 0.2, None)])
        self.assertEqual(s[("render", "frame_scope")]["threads"], ["RenderThread 1"])
        self.assertEqual(frames_seen(s), {"game_frame_track": 3, "rendering_frame_track": 1,
                                          "gt_frame_scopes": 1, "rt_frame_scopes": 1})

    def test_reference_order_follows_r4(self):
        game_track = ["Game Frames,Game Frame 0,1.0,1.1,0.1,0"]
        render_track = ["Rendering Frames,Rendering Frame 0,1.0,1.1,0.1,0"]
        gt_scope = ["GameThread,Frame,1.0,1.1,0.1,0"]
        rt_scope = ["RenderThread 0,Frame,1.0,1.1,0.1,0"]
        cases = [
            (game_track + render_track, gt_scope + rt_scope, ("game", "frame_track")),
            (render_track, gt_scope + rt_scope, ("render", "frame_track")),
            ([], gt_scope + rt_scope, ("game", "frame_scope")),
            ([], rt_scope, ("render", "frame_scope")),
            (["Game Frames,Game Frame 0,1.0,+inf,+inf,0"], rt_scope, ("render", "frame_scope")),
        ]
        for tracks, scopes, expected in cases:
            write_extract(self.dir, tracks, scopes)
            ref = pick_reference(load_frame_series(self.dir))
            self.assertEqual((ref["side"], ref["source"]), expected)
        write_extract(self.dir, [], [])
        self.assertIsNone(pick_reference(load_frame_series(self.dir)))

    def test_overlaps(self):
        self.assertEqual(count_overlaps([(0.0, 1.0), (1.0, 2.0)]), 0)
        self.assertEqual(count_overlaps([(0.0, 1.0), (0.5, 2.0), (2.0, 3.0)]), 1)
        self.assertEqual(count_overlaps([(0.0, 1.0000005), (1.0, 2.0)]), 0)


class SplitWindowsTest(unittest.TestCase):
    def test_basic_split(self):
        frames = even_frames(100)
        r = split_windows(make_reference(frames), parse_duration_text("8s"), 60)
        w, b = r["windows"], r["basis"]
        self.assertEqual(w["startup"], [0.0, 5.0])
        self.assertEqual(w["frames"], [5.0, frames[-1][1]])
        self.assertEqual(w["warmup"], [5.0, frames[60][0]])
        self.assertEqual(w["steady"], [frames[60][0], frames[-1][1]])
        self.assertEqual(w["shutdown"], [frames[-1][1], 8.0])
        self.assertEqual((b["frames_total"], b["frames_warmup"], b["frames_steady"], b["stats_window"]),
                         (100, 60, 40, "steady"))
        self.assertFalse(b["trace_end"]["clamped_to_last_frame"])
        self.assertEqual(r["warnings"], [])

    def test_no_warmup(self):
        r = split_windows(make_reference(even_frames(10)), parse_duration_text("8s"), 0)
        self.assertIsNone(r["windows"]["warmup"])
        self.assertEqual(r["windows"]["steady"], r["windows"]["frames"])

    def test_too_few_frames(self):
        r = split_windows(make_reference(even_frames(30)), parse_duration_text("8s"), 60)
        self.assertIsNone(r["windows"]["steady"])
        self.assertEqual(r["windows"]["warmup"], r["windows"]["frames"])
        self.assertIsNone(r["basis"]["stats_window"])
        self.assertTrue(any("no steady window" in x for x in r["warnings"]))

    def test_trace_end_clamped_within_rounding(self):
        # B2 smoke 实测：末帧结束于 17.9211576s，Insights 报告 "17.92s"（误差范围 17.915–17.925）
        r = split_windows(make_reference(frames_from_spans([(17.0, 17.5), (17.5, 17.9211576)])),
                          parse_duration_text("17.92s"), 0)
        self.assertEqual(r["windows"]["shutdown"], [17.9211576, 17.9211576])
        self.assertTrue(r["basis"]["trace_end"]["clamped_to_last_frame"])
        self.assertEqual(r["warnings"], [])

    def test_trace_end_inconsistent(self):
        r = split_windows(make_reference(frames_from_spans([(17.0, 17.5), (17.5, 18.5)])),
                          parse_duration_text("17.92s"), 0)
        self.assertEqual(r["windows"]["shutdown"], [18.5, 18.5])
        self.assertTrue(any("after the session duration" in x for x in r["warnings"]))

    def test_unknown_trace_end(self):
        r = split_windows(make_reference(even_frames(100)), None, 60)
        self.assertIsNone(r["windows"]["shutdown"])
        self.assertIsNotNone(r["windows"]["steady"])
        self.assertTrue(any("trace end unknown" in x for x in r["warnings"]))

    def test_no_frames(self):
        r = split_windows(None, parse_duration_text("3s"), 60)
        self.assertEqual(r["windows"]["startup"], [0.0, 3.0])
        self.assertIsNone(r["windows"]["frames"])
        self.assertTrue(any("no frame series" in x for x in r["warnings"]))

    def test_nonfinite_counted_without_warning(self):
        # 非有限帧的 warning 从 M1.4 起由 frame_semantics 按侧写出（见 test_frame_semantics），这里只计数
        frames = even_frames(100)
        r = split_windows(make_reference(frames, [(frames[-1][1], 100)]), parse_duration_text("8s"), 60)
        self.assertEqual(r["basis"]["nonfinite_dropped"], 1)
        self.assertEqual(r["warnings"], [])


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class WindowAcceptanceTest(unittest.TestCase):
    """PLAN M1.3 验收：三种帧形态的窗口切分与预热帧排除。"""

    def test_example_frames_window(self):
        m = accept_manifest("example")
        w, b = m["windows"], m["window_basis"]
        self.assertEqual(b["reference"], {"side": "render", "source": "frame_scope",
                                          "threads": ["RenderThread 1"], "timer": "Frame"})
        self.assertEqual(w["frames"], [9.7491746, 12.5228266])
        self.assertEqual([round(x, 2) for x in w["frames"]], [9.75, 12.52])  # PLAN M1：成帧窗口 9.75–12.52s
        self.assertEqual(w["startup"], [0.0, 9.7491746])
        self.assertEqual(w["steady"], [10.150069, 12.5228266])
        self.assertEqual(w["shutdown"], [12.5228266, 15.48])
        self.assertEqual((b["frames_total"], b["frames_warmup"], b["frames_steady"]), (541, 60, 481))
        self.assertEqual((b["nonfinite_dropped"], b["overlapping_frames"]), (0, 0))
        self.assertNotIn("windows (M1.3)", m["pending"])
        # M1.2 的两条（没有引擎日志、RHI 名称未知）+ M1.4 的一条（example 没开 frame 通道，没有游戏帧）
        self.assertEqual(len(m["warnings"]), 3)
        self.assertIn("no game frames", m["warnings"][2])

    def test_b2_smoke_killed_trace(self):
        m = accept_manifest("b2_smoke")
        w, b = m["windows"], m["window_basis"]
        self.assertEqual((b["reference"]["side"], b["reference"]["source"]), ("game", "frame_track"))
        self.assertEqual(w["frames"], [13.7229055, 17.9211576])
        self.assertEqual(w["shutdown"], [17.9211576, 17.9211576])
        self.assertEqual((b["frames_total"], b["nonfinite_dropped"]), (9202, 1))
        self.assertEqual(b["trace_end"]["log_text"], "17.92s")
        self.assertTrue(b["trace_end"]["clamped_to_last_frame"])
        self.assertIn("unfinished at trace end", m["warnings"][0])

    def test_d3d12(self):
        m = accept_manifest("d3d12")
        w, b = m["windows"], m["window_basis"]
        self.assertEqual((b["reference"]["side"], b["reference"]["source"]), ("game", "frame_track"))
        self.assertEqual(w["frames"], [36.470822, 48.9070719])
        self.assertEqual(w["steady"][0], 37.5862363)
        self.assertEqual(w["shutdown"], [48.9070719, 48.91])
        self.assertEqual((b["frames_total"], b["frames_steady"], b["nonfinite_dropped"]), (736, 676, 1))
        self.assertFalse(b["trace_end"]["clamped_to_last_frame"])

    def test_warmup_frames_parameter(self):
        m = accept_manifest("example", warmup_frames=0)
        self.assertIsNone(m["windows"]["warmup"])
        self.assertEqual(m["windows"]["steady"], m["windows"]["frames"])


if __name__ == "__main__":
    unittest.main()
