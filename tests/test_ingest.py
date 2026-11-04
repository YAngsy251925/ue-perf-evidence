#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8.1：外部采集接入（ingest）+ 打包模式（--exe）。

用户的真实工作流有两种，而不是一种：
  ① 编辑器里 PIE / Insights 录制  → 产物一份 .utrace（+ 日志）→ `perfctl ingest`
  ② 打好的包跑起来                → `capture --exe <Game>.exe` 或游戏里录完再 `ingest`
本测试只覆盖不启动引擎的部分：目录整理、存根字段、packaged 的参数形态、包的 Saved 目录推断。
"""
import io
import json
import os
import tempfile
import unittest

from pipeline.capture import ingest, profile


def touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


class IngestTest(unittest.TestCase):
    def test_copies_trace_and_log_and_writes_stub(self):
        with tempfile.TemporaryDirectory() as d:
            trace = touch(os.path.join(d, "src", "x.utrace"), "trace-bytes")
            log = touch(os.path.join(d, "src", "x.log"), "log-bytes")
            run = os.path.join(d, "run")
            result = ingest.ingest(run, trace, log, uproject="P:/Game.uproject", capture_mode="pie")
            self.assertTrue(os.path.isfile(os.path.join(run, "trace.utrace")))
            self.assertTrue(os.path.isfile(os.path.join(run, "engine.log")))
            with io.open(os.path.join(run, "capture_profile.json"), encoding="utf-8") as f:
                snapshot = json.load(f)
        self.assertEqual(snapshot["capture_mode"], "pie")
        self.assertTrue(snapshot["profile"]["conditions"] == {})
        self.assertEqual(result["snapshot"]["profile_name"], "pie")

    def test_trace_only_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            trace = touch(os.path.join(d, "x.utrace"))
            result = ingest.ingest(os.path.join(d, "run"), trace, capture_mode="packaged")
        self.assertIsNone(result["log"])

    def test_missing_trace_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                ingest.ingest(os.path.join(d, "run"), os.path.join(d, "nope.utrace"))

    def test_existing_snapshot_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            run = os.path.join(d, "run")
            touch(os.path.join(run, "capture_profile.json"), json.dumps({"capture_mode": "pie",
                                                                         "profile_name": "keep-me"}))
            trace = touch(os.path.join(d, "x.utrace"))
            result = ingest.ingest(run, trace)
            self.assertEqual(result["snapshot"]["profile_name"], "keep-me")


class PackagedArgsTest(unittest.TestCase):
    def profile(self):
        return {"name": "measure", "class": "measure", "map": "/Game/Entry", "rhi": "default",
                "window": {"resx": 960, "resy": 540}, "flags": ["-unattended"],
                "statnamedevents": True,
                "trace": {"channels": ["cpu", "frame", "gpu", "bookmark"], "tail_mb": 64}}

    def test_standalone_has_uproject_and_game_flag(self):
        args = profile.build_args(self.profile(), "D:/run", "D:/Game.uproject")
        self.assertEqual(args[0], '"D:/Game.uproject"')
        self.assertIn("-game", args)

    def test_packaged_drops_uproject_and_game_flag(self):
        args = profile.build_args(self.profile(), "D:/run", "D:/Game.uproject", packaged=True)
        self.assertEqual(args[0], "/Game/Entry")      # 第一个参数就是地图
        self.assertNotIn("-game", args)
        self.assertNotIn('"D:/Game.uproject"', args)
        self.assertTrue(any(a.startswith("-trace=") for a in args))
        self.assertTrue(any("trace.utrace" in a for a in args))

    def test_packaged_keeps_csv_and_execat(self):
        p = dict(self.profile())
        p["csv"] = {"coverage_start_s": 35, "stop_at_s": 45, "exit": "exit_after_csv"}
        p["exec_at"] = {"at_s": 40, "command": "memreport", "in_probe": True}
        args = profile.build_args(p, "D:/run", "", packaged=True)
        self.assertIn("-ExitAfterCsvProfiling", args)
        self.assertTrue(any(a.startswith("PerfEvidence.ExecAt=") for a in args))

    def test_packaged_window_and_flags(self):
        args = profile.build_args(self.profile(), "D:/run", "", packaged=True)
        self.assertIn("-windowed", args)
        self.assertIn("-resx=960", args)
        self.assertIn("-statnamedevents", args)


if __name__ == "__main__":
    unittest.main()
