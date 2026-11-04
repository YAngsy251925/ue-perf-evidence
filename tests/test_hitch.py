#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_hitch.py — M2.3 hitch 通道（解析器 / 收集 / 视图）的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
"""
import json
import os
import shutil
import tempfile
import unittest

from pipeline.extract import hitch_log
from pipeline.digest.views import hitch as hitch_view

LAB = os.environ.get("UE_PERF_LAB", "")
C_LOG = os.path.join(LAB, "coexist_C.log")
C_SNAPSHOT = os.path.join(LAB, "TraceDigestProbe", "Saved", "Profiling", "Hitches",
                          "24DFBA3D4B23A4FE78B998A79E0E8FCC", "Frame-3.utrace")

SAMPLE = """[2026.11.01-08.43.28:311][  9]LogStats: ------------------Thread Hitch 1, Frame 3   271.4ms ---------------
[2026.11.01-08.43.28:316][  9]LogStats: ------------------ Game Thread 271.37ms
[2026.11.01-08.43.28:316][  9]LogStats:   271.352ms (   4)  -  Thread_468c_0 - GameThread - STATGROUP_Threads - STATCAT_Advanced
[2026.11.01-08.43.28:316][  9]LogStats:     271.349ms (   2)  -  FrameTime - STAT_FrameTime - STATGROUP_Engine - STATCAT_Advanced
[2026.11.01-08.43.28:316][  9]LogStats:       269.019ms (   1)  -  HandleMemReportDeferredCommand - STATGROUP_Quick - STATCAT_Advanced
[2026.11.01-08.43.28:316][  9]LogStats:         225.499ms (   1)  -  Self
[2026.11.01-08.43.28:316][  9]LogStats: ------------------ Render Thread (//Threads//RT) 271.40ms
[2026.11.01-08.43.28:316][  9]LogStats:   270.291ms (  53)  -  CPU Stall - Wait For Event - STAT_EventWait - STATGROUP_CPUStalls - STATCAT_Advanced
[2026.11.01-08.43.28:316][  9]LogStats: ----------------------------------------
[2026.11.01-08.43.28:316][  9]LogStats: Events history: Single frame 3, greater than 1.0 ms
[2026.11.01-08.43.28:316][  9]LogStats:  Trigger: TaskGraph_Trigger <- HandleMemReportDeferredCommand <- UEngine_TickDeferredCommands
"""


class ParserTest(unittest.TestCase):
    def test_sample_block(self):
        hs = hitch_log.parse_hitches(SAMPLE)
        self.assertEqual(len(hs), 1)
        h = hs[0]
        self.assertEqual((h["index"], h["stats_frame"], h["ms"]), (1, 3, 271.4))
        self.assertEqual(h["game_thread_ms"], 271.37)
        self.assertEqual((h["render_thread_ms"], h["render_thread_path"]), (271.4, "//Threads//RT"))
        # 树：GT 4 行（线程根 / FrameTime / HandleMemReport / Self）
        self.assertEqual(len(h["game_tree"]), 4)
        node = next(n for n in h["game_tree"] if n["name"] == "HandleMemReportDeferredCommand")
        self.assertEqual((node["depth"], node["ms"], node["calls"], node["stat_name"], node["group"]),
                         (2, 269.019, 1, None, "STATGROUP_Quick"))
        frame = next(n for n in h["game_tree"] if n["name"] == "FrameTime")
        self.assertEqual(frame["stat_name"], "STAT_FrameTime")
        stall = next(n for n in h["render_tree"] if n["name"].startswith("CPU Stall"))
        self.assertEqual((stall["stat_name"], stall["group"]), ("STAT_EventWait", "STATGROUP_CPUStalls"))
        # Trigger 行属于块后的 dumpevents 段：hitch 块已结束（BLOCK_END），不该混进来
        self.assertEqual(h["triggers"], [])

    def test_multiple_hitches_and_cooldown_note(self):
        text = SAMPLE.replace("Thread Hitch 1", "Thread Hitch 2").replace("Frame 3   271.4ms", "Frame 12   88.2ms")
        hs = hitch_log.parse_hitches(SAMPLE + text)
        self.assertEqual([h["stats_frame"] for h in hs], [3, 12])
        self.assertEqual(hs[1]["ms"], 88.2)

    def test_empty_and_garbage(self):
        self.assertEqual(hitch_log.parse_hitches(""), [])
        self.assertEqual(hitch_log.parse_hitches("random log lines\nno hitch here\n"), [])


@unittest.skipUnless(os.path.isfile(C_LOG), "coexist_C log not present")
class CollectDryRunTest(unittest.TestCase):
    """收集器：用 C 实验的日志 + 真快照（不跑 Insights 导出，快照导出目录置空断言结构）。"""

    def test_hint_parse(self):
        dirs = hitch_log and __import__("pipeline.capture.hitch_evidence", fromlist=["snapshot_log_hint"]) \
            .snapshot_log_hint(C_LOG)
        self.assertEqual(len(dirs), 1)
        self.assertIn("Hitches", dirs[0])

    def test_collect_copies_snapshot(self):
        from pipeline.capture import hitch_evidence as H
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = os.path.join(tmp.name, "run")
        os.makedirs(run)
        # 构造假快照文件（真实 C 快照 5 MB，拷进来太重；这里只验证发现/拷贝/索引逻辑）
        fake_dir = os.path.join(tmp.name, "Hitches", "INSTANCE")
        os.makedirs(fake_dir)
        shutil.copy(C_LOG, os.path.join(run, "engine.log"))
        with open(os.path.join(fake_dir, "Frame-3.utrace"), "wb") as f:
            f.write(b"fake")
        index = H.collect(run, os.path.join(run, "engine.log"), snapshot_dirs=[fake_dir], ue_root=None)
        # 发现/拷贝/索引必须成功；假 trace 也能被 Insights "成功导出"（退出码 0、空产物），
        # 所以这里只断言导出目录存在，export_ok 的真假由产物校验层（insights_export report）负责
        self.assertEqual(len(index["hitches"]), 1)
        self.assertEqual(len(index["snapshots"]), 1)
        e = index["snapshots"][0]
        self.assertEqual(e["stats_frame"], 3)
        self.assertTrue(os.path.isfile(os.path.join(run, "hitches", "Frame-3.utrace")))
        self.assertTrue(os.path.isdir(os.path.join(run, e["export_dir"])))
        self.assertTrue(e["matched_dumphitches_block"])
        with open(os.path.join(run, "hitches_index.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["schema"], "perf-hitches/0")


DRYRUN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs", "m2_3_dryrun")


@unittest.skipUnless(os.path.isfile(os.path.join(DRYRUN, "hitches_index.json")),
                     "m2_3_dryrun results not present")
class HitchViewTest(unittest.TestCase):
    def test_view_on_real_data(self):
        # 用 m2_3_dryrun 的现成结果（有真实快照导出）
        digest, md = hitch_view.build(DRYRUN, None)
        self.assertEqual(len(digest["hitches"]), 1)
        self.assertEqual(digest["conclusion"]["confidence"], "clear")
        self.assertIn("HandleMemReportDeferredCommand", digest["conclusion"]["text"])
        self.assertIn("| depth | ms | calls |", md)
        self.assertIn("dumphitches 冷却 7 帧", md)

    def test_view_without_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest, md = hitch_view.build(tmp, None)
            self.assertEqual(digest["capability"], "no_evidence")
            self.assertIn("hitches_index.json", digest["conclusion"]["text"])


if __name__ == "__main__":
    unittest.main()
