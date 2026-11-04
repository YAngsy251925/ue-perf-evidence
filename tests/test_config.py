#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.5：cvar / scalability 快照（两个证据源：ExecCmds 与 LogConfig 的 Set CVar）。"""
import unittest

from pipeline.digest.views import config


LOG = """LogInit: Selected Device Profile: [WindowsEditor]
LogConfig: Set CVar [[r.setres:1280x720]]
LogConfig: Set CVar [[t.MaxFPS:0]]
LogConfig: Applying CVar settings from Section [ViewDistanceQuality@3] File [Scalability]
LogConfig: Applying CVar settings from Section [ShadowQuality@2] File [Scalability]
LogInit: Command Line: -ExecCmds="t.MaxFPS 0,r.VSync 0,r.GTSyncType 0,r.SetRes 960x540w"
"""


class ParseTest(unittest.TestCase):
    def test_device_profile_and_scalability(self):
        out = config.parse(LOG)
        self.assertEqual(out["device_profile"], "WindowsEditor")
        self.assertEqual(out["scalability"], {"ViewDistanceQuality": 3, "ShadowQuality": 2})

    def test_logconfig_cvars(self):
        out = config.parse(LOG)
        self.assertEqual(out["cvars"]["r.setres"], "1280x720")
        self.assertEqual(out["cvars"]["t.MaxFPS"], "0")

    def test_exec_cmds_are_their_own_source(self):
        out = config.parse(LOG)
        self.assertEqual(out["exec_cvars"]["r.VSync"], "0")
        self.assertEqual(out["exec_cvars"]["r.SetRes"], "960x540w")

    def test_declared_drift_ok_changed_missing(self):
        declared = {"t.MaxFPS": "0",        # LogConfig 命中
                    "r.VSync": "0",         # 只在 ExecCmds 命中
                    "r.GTSyncType": "1",    # 与实际不符
                    "r.Never.Seen": "1"}    # 完全没有
        drift = config.parse(LOG, declared)["declared_drift"]
        self.assertEqual(drift["t.MaxFPS"]["status"], "ok")
        # 两个源都有时以 exec_cmds 为准：它在帧 0 执行，后于 ini/LogConfig 生效
        self.assertEqual(drift["t.MaxFPS"]["source"], "exec_cmds")
        self.assertEqual(drift["r.VSync"]["status"], "ok")
        self.assertEqual(drift["r.VSync"]["source"], "exec_cmds")
        self.assertEqual(drift["r.GTSyncType"]["status"], "changed")
        self.assertEqual(drift["r.Never.Seen"]["status"], "missing")


class RenderTest(unittest.TestCase):
    def test_markdown_mentions_sources(self):
        out = config.parse(LOG, {"r.VSync": "0"})
        out.update({"run_dir": "D:/run", "assignments": out["assignments"]})
        md = config.render_md(out)
        self.assertIn("# 配置快照", md)
        self.assertIn("声明 vs 实际", md)
        self.assertIn("exec_cmds", md)
        self.assertIn("Scalability 档位", md)


if __name__ == "__main__":
    unittest.main()
