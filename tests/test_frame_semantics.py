#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_frame_semantics.py — M1.4 帧口径（GT / RT 分列与精确分位数）的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
单元测试只用内联样本；验收测试读取本机的实测 run，路径不存在时跳过。
验收期望值来自对实测 CSV 的独立计算（不经过本模块）。
"""
import os
import unittest

from pipeline.caliber.frame_semantics import (build_frame_semantics, choose_series, nearest_rank, summarize)
from tests.acceptance_data import LAB, accept_manifest, have_acceptance_data

C_EXTRACT = os.path.join(LAB, "out_probe_c")
C_LOG = os.path.join(LAB, "coexist_C.log")


def series_entry(side, source, frames, nonfinite=(), threads=None):
    return {"side": side, "source": source, "timer": "Frame" if source == "frame_scope" else "%s Frame N" % side,
            "threads": threads or (["Game Frames"] if side == "game" else ["Rendering Frames"]),
            "rows": len(frames) + len(nonfinite), "frames": list(frames), "nonfinite": list(nonfinite)}


def empty_entry(side, source):
    return series_entry(side, source, [], threads=[])


def make_series(game_track=None, render_track=None, game_scope=None, render_scope=None):
    return {
        ("game", "frame_track"): game_track or empty_entry("game", "frame_track"),
        ("render", "frame_track"): render_track or empty_entry("render", "frame_track"),
        ("game", "frame_scope"): game_scope or empty_entry("game", "frame_scope"),
        ("render", "frame_scope"): render_scope or empty_entry("render", "frame_scope"),
    }


def contiguous_frames(n, start=1.0, step=0.010):
    return [(start + i * step, start + (i + 1) * step, step, i) for i in range(n)]


def busy_frames(n, start=1.0, step=0.010, busy=0.002):
    return [(start + i * step, start + i * step + busy, busy, i) for i in range(n)]


def window_for(frames, warmup=0):
    windows = {"steady": [frames[warmup][0], frames[-1][1]]}
    return windows, {"stats_window": "steady"}


class PercentileTest(unittest.TestCase):
    def test_nearest_rank_definition(self):
        v = [float(x) for x in range(1, 101)]  # 1..100
        self.assertEqual([nearest_rank(v, p) for p in (50, 90, 95, 99, 100)], [50.0, 90.0, 95.0, 99.0, 100.0])
        v10 = [float(x) for x in range(1, 11)]
        # 秩 = ceil(p*n/100)：p90 是第 9 个值；用浮点算 0.9*10 会得到 9.000000000000002，向上取整成第 10 个
        self.assertEqual([nearest_rank(v10, p) for p in (50, 90, 95, 99)], [5.0, 9.0, 10.0, 10.0])
        self.assertEqual(nearest_rank([7.0], 99), 7.0)

    def test_summarize_rounds_to_four_decimals(self):
        s = summarize([16.66804999, 16.66805001, 2.0])
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["max"], 16.6681)
        self.assertEqual(s["p50"], 16.668)
        self.assertIsNone(summarize([]))


class ChooseSeriesTest(unittest.TestCase):
    def test_track_before_scope_and_fallbacks(self):
        track = series_entry("game", "frame_track", contiguous_frames(3))
        scope = series_entry("game", "frame_scope", contiguous_frames(3), threads=["GameThread"])
        self.assertIs(choose_series(make_series(game_track=track, game_scope=scope), "game"), track)
        self.assertIs(choose_series(make_series(game_scope=scope), "game"), scope)
        only_inf = series_entry("game", "frame_track", [], nonfinite=[(1.0, 0)])
        self.assertIs(choose_series(make_series(game_track=only_inf, game_scope=scope), "game"), scope)
        self.assertIs(choose_series(make_series(game_track=only_inf), "game"), only_inf)
        self.assertIsNone(choose_series(make_series(), "game"))


class BuildFrameSemanticsTest(unittest.TestCase):
    def test_contiguous_game_and_busy_render(self):
        game = contiguous_frames(100)
        render = busy_frames(100)
        series = make_series(game_track=series_entry("game", "frame_track", game),
                             render_track=series_entry("render", "frame_track", render))
        windows, basis = window_for(game, warmup=10)
        fs, warnings = build_frame_semantics(series, windows, basis, True, "real RHI (D3D12)", "real")
        g, r = fs["game"]["stats"], fs["render"]["stats"]
        self.assertEqual((fs["game"]["window_frames"], fs["render"]["window_frames"]), (90, 90))
        self.assertEqual((g["duration_ms"]["p50"], g["interval_ms"]["p50"], g["contiguous"]), (10.0, 10.0, True))
        # 渲染侧不首尾相接：duration 是忙碌跨度（2ms），帧节奏要看 interval（10ms）
        self.assertEqual((r["duration_ms"]["p50"], r["interval_ms"]["p50"], r["contiguous"]), (2.0, 10.0, False))
        self.assertEqual(r["gap_ms_p50"], 8.0)
        self.assertEqual(fs["consistency"]["ok"], True)
        self.assertEqual(fs["percentile_method"], "nearest_rank")
        self.assertEqual(warnings, [])

    def test_max_frame_points_to_the_longest_frame(self):
        frames = contiguous_frames(20)
        s, e, _, i = frames[15]
        frames[15] = (s, e, 0.050, i)
        series = make_series(game_track=series_entry("game", "frame_track", frames))
        windows, basis = window_for(frames)
        fs, _ = build_frame_semantics(series, windows, basis, True, "real RHI", "real")
        self.assertEqual(fs["game"]["stats"]["max_frame"], {"start": s, "track_index": 15})
        self.assertEqual(fs["game"]["stats"]["duration_ms"]["max"], 50.0)

    def test_not_measurable_withholds_stats(self):
        game = contiguous_frames(100)
        series = make_series(game_track=series_entry("game", "frame_track", game))
        windows, basis = window_for(game, warmup=60)
        fs, warnings = build_frame_semantics(series, windows, basis, False, "nullrhi run: smoke only", "null")
        self.assertIsNone(fs["game"]["stats"])
        self.assertEqual(fs["game"]["window_frames"], 40)
        self.assertIn("not measurable", fs["game"]["stats_withheld"])
        self.assertEqual(fs["render"]["source"], "none")
        self.assertIn("expected for a nullrhi run", fs["render"]["stats_withheld"])
        self.assertEqual(warnings, [])  # nullrhi 的 render 侧没有帧是预期内的，不报警

    def test_missing_side_on_real_rhi_warns(self):
        render = contiguous_frames(10)
        series = make_series(render_scope=series_entry("render", "frame_scope", render, threads=["RenderThread 1"]))
        windows, basis = window_for(render)
        fs, warnings = build_frame_semantics(series, windows, basis, True, "real RHI", "real")
        self.assertEqual(fs["game"]["source"], "none")
        self.assertEqual(fs["consistency"]["note"], "not evaluated: no game frames")
        self.assertTrue(any("no game frames" in w for w in warnings))

    def test_nonfinite_overlap_and_numbering_warnings(self):
        frames = contiguous_frames(10)
        frames[5] = (frames[5][0], frames[5][1], frames[5][2], 7)  # 编号 7 出现两次，5 缺失
        frames.append((1.095, 1.105, 0.010, 10))  # 与上一帧重叠
        nonfinite = [(frames[-1][1], 11)]
        series = make_series(game_track=series_entry("game", "frame_track", frames, nonfinite=nonfinite))
        windows, basis = window_for(frames)
        fs, warnings = build_frame_semantics(series, windows, basis, True, "real RHI", "real")
        self.assertEqual(fs["game"]["nonfinite_dropped"], 1)
        self.assertEqual(fs["game"]["overlapping_frames"], 1)
        self.assertEqual(fs["game"]["track_index"], {"first": 0, "last": 11, "gaps": 1, "duplicates": 1})
        joined = " | ".join(warnings)
        for text in ("unfinished at trace end", "overlapping frame", "numbering has 1 gap(s) and 1 duplicate(s)"):
            self.assertIn(text, joined)

    def test_q3_inconsistency_warns(self):
        game = contiguous_frames(100)
        render = contiguous_frames(90)
        series = make_series(game_track=series_entry("game", "frame_track", game),
                             render_track=series_entry("render", "frame_track", render))
        windows, basis = window_for(game)
        fs, warnings = build_frame_semantics(series, windows, basis, True, "real RHI", "real")
        self.assertEqual((fs["consistency"]["ok"], fs["consistency"]["rows_delta"]), (False, 10))
        self.assertTrue(any("Q3" in w for w in warnings))

    def test_no_stats_window(self):
        game = contiguous_frames(30)
        series = make_series(game_track=series_entry("game", "frame_track", game))
        fs, _ = build_frame_semantics(series, {"steady": None}, {"stats_window": None}, True, "real RHI", "real")
        self.assertEqual(fs["game"]["stats_withheld"], "no stats window")


@unittest.skipUnless(have_acceptance_data() and os.path.isdir(C_EXTRACT), "local acceptance runs not present")
class FrameSemanticsAcceptanceTest(unittest.TestCase):
    """PLAN M1.4 验收：example → frame_scope、RenderThread 1、541 帧；C → 只有游戏帧轨道，16,201 行
    （16,200 个有限帧 + 1 个 +inf）；D3D12 → Game 737 行 / Rendering 736 行，Q3 前半成立。"""

    def test_example_render_scope(self):
        fs = accept_manifest("example")["frame_semantics"]
        r = fs["render"]
        self.assertEqual((r["source"], r["threads"], r["timer"], r["frames"], r["window_frames"]),
                         ("frame_scope", ["RenderThread 1"], "Frame", 541, 481))
        self.assertIsNone(r["track_index"])
        st = r["stats"]
        self.assertEqual((st["duration_ms"]["p50"], st["duration_ms"]["p99"], st["duration_ms"]["max"]),
                         (4.8726, 6.2252, 8.3451))
        self.assertEqual((st["interval_ms"]["p50"], st["interval_ms"]["p99"]), (4.8938, 6.2506))
        self.assertTrue(st["contiguous"])
        self.assertEqual(fs["game"]["source"], "none")
        self.assertIsNone(fs["consistency"]["ok"])

    def test_c_hitch_game_track_only(self):
        m = accept_manifest_from(C_EXTRACT, C_LOG, "c_hitch")
        g = m["frame_semantics"]["game"]
        self.assertEqual((g["source"], g["rows"], g["frames"], g["nonfinite_dropped"]),
                         ("frame_track", 16201, 16200, 1))
        self.assertEqual(g["track_index"], {"first": 0, "last": 16200, "gaps": 0, "duplicates": 0})
        self.assertIsNone(g["stats"])  # nullrhi：不出统计（R11）
        self.assertEqual(m["frame_semantics"]["render"]["source"], "none")
        self.assertFalse(m["measurable"])

    def test_d3d12_both_sides(self):
        fs = accept_manifest("d3d12")["frame_semantics"]
        g, r = fs["game"], fs["render"]
        self.assertEqual((g["source"], g["rows"], g["frames"], g["window_frames"]), ("frame_track", 737, 736, 676))
        self.assertEqual((r["source"], r["rows"], r["frames"], r["window_frames"]), ("frame_track", 736, 736, 676))
        gs, rs = g["stats"], r["stats"]
        self.assertEqual((gs["duration_ms"]["p50"], gs["duration_ms"]["p99"], gs["duration_ms"]["max"]),
                         (16.6561, 17.212, 85.1714))
        self.assertEqual((gs["interval_ms"]["p50"], gs["contiguous"]), (16.668, True))
        # R15：渲染帧不首尾相接，duration（2.99ms）不是帧时间，帧节奏看 interval（16.67ms）
        self.assertEqual((rs["duration_ms"]["p50"], rs["interval_ms"]["p50"], rs["contiguous"]), (2.9897, 16.6669, False))
        # 最长的一帧是 CSV Profiler 写文件那一帧（引擎日志：frame 299 "Capture Ended. Writing CSV"）
        self.assertEqual(gs["max_frame"], {"start": 41.5695942, "track_index": 299})
        self.assertEqual((fs["consistency"]["ok"], fs["consistency"]["rows_delta"], fs["consistency"]["window_frames_delta"]),
                         (True, 1, 0))

    def test_pending_lists_remaining_fields(self):
        pending = accept_manifest("d3d12")["pending"]
        self.assertNotIn("frame_semantics (M1.4)", pending)
        self.assertIn("frame_semantics.gpu (M4.0)", pending)


def accept_manifest_from(extract, log, name):
    from pipeline.caliber.manifest import build_manifest
    from tests.acceptance_data import REPO
    return build_manifest(os.path.join(REPO, "runs", "acceptance", name), log, extract, None)


if __name__ == "__main__":
    unittest.main()
