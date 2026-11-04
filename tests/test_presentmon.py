#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.3：PresentMon 采集/解析（不启动真实进程，只测解析与判定）。"""
import io
import os
import tempfile
import unittest

from pipeline.capture import presentmon


def write_csv(text):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


V1_HEADER = "Application,ProcessID,TimeInMs,FrameTime,CPUBusy,GPUTime,GPUWait\n"
V2_HEADER = "Application,ProcessID,TimeInMs,MsBetweenPresents,MsCPUBusy,MsGPUTime,MsGPUWait\n"


class CommandTest(unittest.TestCase):
    def test_cmd_has_timed_and_no_process_filter(self):
        cmd = presentmon.build_cmd("D:/o/p.csv", process_name=None, seconds=90, exe="D:/pm.exe")
        self.assertIn("--timed", cmd)
        self.assertIn("90", cmd)
        self.assertIn("--terminate_after_timed", cmd)
        self.assertNotIn("--process_name", cmd)   # 不带过滤：全量采集，解析侧再匹配
        self.assertNotIn("--v2_metrics", cmd)     # 实测本机 2.6.0 忽略它

    def test_process_filter_is_optional(self):
        cmd = presentmon.build_cmd("D:/o/p.csv", process_name="UnrealEditor-Cmd.exe",
                                   seconds=30, exe="D:/pm.exe")
        self.assertEqual(cmd[cmd.index("--process_name") + 1], "UnrealEditor-Cmd.exe")


class SummarizeTest(unittest.TestCase):
    def test_v1_columns(self):
        rows = "".join("UnrealEditor-Cmd.exe,1,%d,3.0,0.1,2.9,0.2\n" % i for i in range(5))
        path = write_csv(V1_HEADER + rows)
        self.addCleanup(os.unlink, path)
        s = presentmon.summarize(path, "UnrealEditor-Cmd.exe")
        self.assertEqual(s["picked"], "UnrealEditor-Cmd.exe")
        app = s["apps"]["UnrealEditor-Cmd.exe"]
        self.assertEqual(app["ms_between_presents"]["n"], 5)
        self.assertAlmostEqual(app["ms_between_presents"]["p50"], 3.0)

    def test_v2_columns(self):
        rows = "".join("UnrealEditor-Cmd.exe,1,%d,4.0,0.2,3.8,0.3\n" % i for i in range(3))
        path = write_csv(V2_HEADER + rows)
        self.addCleanup(os.unlink, path)
        s = presentmon.summarize(path, "UnrealEditor-Cmd.exe")
        self.assertEqual(s["apps"]["UnrealEditor-Cmd.exe"]["ms_between_presents"]["n"], 3)

    def test_picks_requested_process_not_biggest(self):
        rows = ("Other.exe,2,0,16.0,0.1,1.0,0.0\n" * 3 +
                "UnrealEditor-Cmd.exe,1,0,3.0,0.1,2.9,0.1\n")
        path = write_csv(V1_HEADER + rows)
        self.addCleanup(os.unlink, path)
        s = presentmon.summarize(path, "UnrealEditor-Cmd.exe")
        self.assertEqual(s["picked"], "UnrealEditor-Cmd.exe")

    def test_na_values_are_dropped(self):
        rows = "UnrealEditor-Cmd.exe,1,0,3.0,NA,NA,NA\nUnrealEditor-Cmd.exe,1,1,NA,NA,NA,NA\n"
        path = write_csv(V1_HEADER + rows)
        self.addCleanup(os.unlink, path)
        s = presentmon.summarize(path, "UnrealEditor-Cmd.exe")
        self.assertEqual(s["apps"]["UnrealEditor-Cmd.exe"]["ms_between_presents"]["n"], 1)


class CompareTest(unittest.TestCase):
    def summary(self, p50):
        return {"picked": "app.exe",
                "apps": {"app.exe": {"rows": 10, "ms_between_presents": {"n": 10, "p50": p50}}}}

    def test_agree_warn_disagree(self):
        self.assertEqual(presentmon.compare(self.summary(16.0), 16.0)["verdict"], "agree")
        self.assertEqual(presentmon.compare(self.summary(20.0), 16.0)["verdict"], "warn")
        self.assertEqual(presentmon.compare(self.summary(30.0), 16.0)["verdict"], "disagree")

    def test_missing_side_is_unknown(self):
        self.assertEqual(presentmon.compare(self.summary(None), 16.0)["verdict"], "unknown")
        self.assertEqual(presentmon.compare(self.summary(16.0), None)["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
