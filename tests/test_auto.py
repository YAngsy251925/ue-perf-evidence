#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8.2：自动分析最新 utrace（发现 / 选日志 / 摘要），不启动引擎。"""
import io
import os
import tempfile
import time
import unittest

from pipeline.capture import auto


def touch(path, age_s=0.0, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


class FindLatestTest(unittest.TestCase):
    def test_picks_newest_by_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            old = touch(os.path.join(d, "a.utrace"), age_s=600)
            new = touch(os.path.join(d, "sub", "b.utrace"), age_s=60)
            path, _mtime = auto.find_latest([d], min_age_s=10)
        self.assertEqual(os.path.basename(path), "b.utrace")
        self.assertTrue(new.endswith("b.utrace"))
        self.assertNotEqual(path, old)

    def test_skips_too_fresh_files(self):
        with tempfile.TemporaryDirectory() as d:
            touch(os.path.join(d, "recording.utrace"), age_s=2)
            touch(os.path.join(d, "done.utrace"), age_s=120)
            path, _ = auto.find_latest([d], min_age_s=10)
        self.assertEqual(os.path.basename(path), "done.utrace")

    def test_nothing_found(self):
        with tempfile.TemporaryDirectory() as d:
            touch(os.path.join(d, "only.utrace"), age_s=1)
            self.assertEqual(auto.find_latest([d], min_age_s=10), (None, None))

    def test_ignores_other_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            touch(os.path.join(d, "x.trace"), age_s=100)
            touch(os.path.join(d, "y.utrace.txt"), age_s=100)
            self.assertEqual(auto.find_latest([d], min_age_s=10), (None, None))

    def test_candidate_dirs_from_project(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Saved", "Profiling"))
            dirs = auto.candidate_dirs(os.path.join(d, "Game.uproject"))
        self.assertEqual(len(dirs), 1)
        self.assertTrue(dirs[0].endswith(os.path.join("Saved", "Profiling")))

    def test_candidate_dirs_skips_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(auto.candidate_dirs(os.path.join(d, "nope.uproject")), [])


class ProjectGuessTest(unittest.TestCase):
    def test_uproject_file(self):
        self.assertEqual(auto.guess_project("D:/P/Game.uproject").replace("\\", "/"), "D:/P")

    def test_saved_dir(self):
        self.assertEqual(auto.guess_project("D:/P/Saved").replace("\\", "/"), "D:/P")

    def test_plain_dir(self):
        self.assertEqual(auto.guess_project("D:/P").replace("\\", "/"), "D:/P")

    def test_none(self):
        self.assertIsNone(auto.guess_project(None))


class LogPickTest(unittest.TestCase):
    def test_picks_closest_log(self):
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, "Saved", "Logs")
            trace_time = time.time() - 300
            touch(os.path.join(logs, "far.log"), age_s=7200)
            near = touch(os.path.join(logs, "near.log"), age_s=300)
            found = auto.find_log(d, trace_time)
        self.assertEqual(os.path.basename(found), "near.log")
        self.assertTrue(found.endswith("near.log") and near)

    def test_no_logs_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(auto.find_log(d, time.time()))

    def test_out_of_window(self):
        with tempfile.TemporaryDirectory() as d:
            touch(os.path.join(d, "Saved", "Logs", "ancient.log"), age_s=100000)
            self.assertIsNone(auto.find_log(d, time.time(), window_s=3600))


class SummaryTest(unittest.TestCase):
    def test_summary_lists_views_and_conclusions(self):
        views = {"cpu": {"rc": 0, "md": "D:/r/cpu/perf_digest.md", "conclusion": "GT 在 PerfEvidenceRegress"},
                 "bound": {"rc": 0, "md": "D:/r/bound/perf_digest.md", "conclusion": "工作在 GT"}}
        md = auto.build_summary("D:/r/auto-1", "D:/P/Saved/Profiling/x.utrace", "D:/P/Saved/Logs/x.log", views)
        self.assertIn("# 自动分析：最新 utrace", md)
        self.assertIn("GT 在 PerfEvidenceRegress", md)
        self.assertIn("外部采集", md)
        self.assertIn("## bound", md)

    def test_failed_view_is_reported(self):
        views = {"cpu": {"rc": 1, "md": None, "conclusion": None}}
        md = auto.build_summary("D:/r", "x.utrace", None, views)
        self.assertIn("该视图失败（rc=1）", md)
        self.assertIn("未找到", md)


if __name__ == "__main__":
    unittest.main()
