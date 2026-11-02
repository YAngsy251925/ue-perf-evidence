#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_caliber.py — M1.2 manifest v0 的单元测试与验收测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
单元测试只用内联样本，任何 clone 都能跑；验收测试读取本机的实测 run，路径不存在时跳过。
"""
import json
import unittest

from pipeline.caliber.bridge_detect import detect_bridge
from pipeline.caliber.engine_log import parse_engine_log_text
from pipeline.caliber.manifest import capabilities, describe_command_line
from pipeline.caliber.rhi_detect import detect_rhi
from tests.acceptance_data import ACCEPT, accept_manifest, have_acceptance_data

# 格式取自实测日志（coexist_D / B2 smoke）：开头的 LogInit 行没有时间戳前缀
LOG_D3D12 = """LogInit: Build: ++UE5+Release-5.8-CL-56702186
LogInit: Platform=WindowsEditor
LogInit: Engine Version: 5.8.2-56702186+++UE5+Release-5.8
LogInit: Build Configuration: Development
LogInit: Command Line: /Engine/Maps/Entry -game -windowed -statnamedevents -trace=cpu,frame,gpu,bookmark,file -tracefile=D:\\x\\trace.utrace -tracetailmb=64
[2026.11.01-10.26.15:907][  0]LogConfig: Applying CVar settings
[2026.11.01-10.26.16:993][  0]LogRHI: Using Default RHI: D3D12
[2026.11.01-10.26.16:994][  0]LogRHI: Using Highest Feature Level of D3D12: SM5
"""
LOG_NULL = """LogInit: Build Configuration: Development
LogInit: Command Line: /Engine/Maps/Entry -game -nullrhi -statnamedevents -trace=cpu,frame,file
[2026.11.01-11.01.25:452][  0]LogModuleManager: InternalLoadLibrary: 'NullDrv' ('D:/x/UnrealEditor-NullDrv.dll')
"""
FRAMES_REAL = {"game_frame_track": 737, "rendering_frame_track": 736, "gt_frame_scopes": 737, "rt_frame_scopes": 736}
FRAMES_RT_SCOPES_ONLY = {"game_frame_track": 0, "rendering_frame_track": 0, "gt_frame_scopes": 0, "rt_frame_scopes": 541}
FRAMES_GAME_ONLY = {"game_frame_track": 9203, "rendering_frame_track": 0, "gt_frame_scopes": 9203, "rt_frame_scopes": 0}


class EngineLogTest(unittest.TestCase):
    def test_d3d12_log(self):
        f = parse_engine_log_text(LOG_D3D12)
        self.assertEqual(f["rhi_name"], "D3D12")
        self.assertEqual(f["rhi_selection"], "Default")
        self.assertEqual(f["feature_level"], "SM5")
        self.assertEqual(f["build_config"], "Development")
        self.assertEqual(f["build"], "++UE5+Release-5.8-CL-56702186")
        self.assertEqual(f["engine_version"], "5.8.2-56702186+++UE5+Release-5.8")
        self.assertEqual(f["platform"], "WindowsEditor")
        self.assertTrue(f["command_line"].startswith("/Engine/Maps/Entry -game"))
        self.assertEqual(f["first_timestamp_utc"], "2026.11.01-10.26.15:907")
        self.assertFalse(f["nulldrv_loaded"])

    def test_forced_and_preferred_rhi(self):
        for kind in ("Forced", "Preferred"):
            f = parse_engine_log_text("[t][0]LogRHI: Using %s RHI: D3D11\n" % kind)
            self.assertEqual((f["rhi_selection"], f["rhi_name"]), (kind, "D3D11"))

    def test_nulldrv(self):
        self.assertTrue(parse_engine_log_text(LOG_NULL)["nulldrv_loaded"])


class RhiDetectTest(unittest.TestCase):
    def test_command_line_nullrhi(self):
        r = detect_rhi(parse_engine_log_text(LOG_NULL), FRAMES_GAME_ONLY)
        self.assertEqual((r["rhi_state"], r["rhi_source"], r["measurable"]), ("null", "command_line", False))
        self.assertEqual(r["warnings"], [])

    def test_nulldrv_without_command_line(self):
        r = detect_rhi({"nulldrv_loaded": True}, None)
        self.assertEqual((r["rhi_state"], r["rhi_source"]), ("null", "log"))

    def test_log_real_rhi(self):
        r = detect_rhi(parse_engine_log_text(LOG_D3D12), FRAMES_REAL)
        self.assertEqual((r["rhi"], r["rhi_state"], r["rhi_source"], r["feature_level"]), ("D3D12", "real", "log", "SM5"))
        self.assertTrue(r["measurable"])
        self.assertEqual(r["warnings"], [])

    def test_trace_only_rendering(self):
        r = detect_rhi(None, FRAMES_RT_SCOPES_ONLY)
        self.assertEqual((r["rhi"], r["rhi_state"], r["rhi_source"], r["measurable"]), (None, "real", "trace", True))
        self.assertTrue(any("rhi name unknown" in w for w in r["warnings"]))

    def test_trace_only_game_frames(self):
        r = detect_rhi(None, FRAMES_GAME_ONLY)
        self.assertEqual((r["rhi_state"], r["rhi_source"], r["measurable"]), ("null", "trace", False))

    def test_nothing(self):
        r = detect_rhi(None, None)
        self.assertEqual((r["rhi_state"], r["measurable"]), ("undetermined", False))
        self.assertTrue(r["warnings"])

    def test_conflict_real_log_without_rendering(self):
        r = detect_rhi(parse_engine_log_text(LOG_D3D12), FRAMES_GAME_ONLY)
        self.assertEqual(r["rhi_state"], "real")
        self.assertTrue(any("no rendering frames" in w for w in r["warnings"]))

    def test_conflict_nullrhi_with_rendering(self):
        r = detect_rhi(parse_engine_log_text(LOG_NULL), FRAMES_REAL)
        self.assertTrue(any("rendering frames" in w for w in r["warnings"]))


class BridgeDetectTest(unittest.TestCase):
    def test_effective(self):
        r = detect_bridge(["FrameTime", "GameEngine Tick", "World Tick Time", "X"], "-game -statnamedevents")
        self.assertEqual((r["statnamedevents_requested"], r["statnamedevents_effective"]), (True, True))
        self.assertEqual(r["warnings"], [])

    def test_single_marker_is_not_enough(self):
        self.assertFalse(detect_bridge(["FrameTime", "STAT_UpdateGPUScene"])["statnamedevents_effective"])

    def test_requested_but_not_effective(self):
        r = detect_bridge(["STAT_UpdateGPUScene"], "-statnamedevents -trace=cpu")
        self.assertFalse(r["statnamedevents_effective"])
        self.assertTrue(r["warnings"])

    def test_unknown_command_line(self):
        self.assertIsNone(detect_bridge([], None)["statnamedevents_requested"])


class CommandLineTest(unittest.TestCase):
    def test_game_run(self):
        d = describe_command_line("/Engine/Maps/Entry -game -trace=cpu,frame,file -tracefile=D:\\x\\t.utrace -tracetailmb=64")
        self.assertEqual(d, {"map": "/Engine/Maps/Entry", "mode": "game", "trace_channels_requested": ["cpu", "frame", "file"]})

    def test_commandlet(self):
        d = describe_command_line("-run=TraceDigest trace=D:/x.utrace -nullrhi")
        self.assertEqual((d["map"], d["mode"], d["trace_channels_requested"]), (None, "commandlet", []))

    def test_capabilities(self):
        self.assertFalse(capabilities("Shipping")["memreport"])
        self.assertTrue(capabilities("Development")["snapshothitches"])
        self.assertIsNone(capabilities(None)["stats"])


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class AcceptanceTest(unittest.TestCase):
    """PLAN M1.2 验收：example 桥接关；B2 smoke 桥接开且不可测量；D3D12 可测量。
    B2 与 D3D12 都是被强制结束的 trace，末帧没走完，M1.3 起各有一条 R13 的非有限值 warning。"""

    def test_example_bridge_off_real_by_trace(self):
        m = accept_manifest("example")
        self.assertFalse(m["trace"]["statnamedevents_effective"])
        self.assertEqual(m["trace"]["bridge_evidence"], [])
        self.assertEqual((m["engine"]["rhi_state"], m["engine"]["rhi_source"], m["measurable"]), ("real", "trace", True))
        self.assertEqual(m["frames_seen"]["rt_frame_scopes"], 541)

    def test_b2_smoke_bridge_on_not_measurable(self):
        m = accept_manifest("b2_smoke")
        self.assertTrue(m["trace"]["statnamedevents_effective"])
        self.assertTrue(m["trace"]["statnamedevents_requested"])
        self.assertEqual((m["engine"]["rhi"], m["engine"]["rhi_state"], m["engine"]["rhi_source"]), (None, "null", "command_line"))
        self.assertFalse(m["measurable"])
        self.assertEqual(len(m["warnings"]), 1)
        self.assertIn("non-finite", m["warnings"][0])

    def test_d3d12_measurable(self):
        m = accept_manifest("d3d12")
        self.assertEqual((m["engine"]["rhi"], m["engine"]["feature_level"], m["measurable"]), ("D3D12", "SM5", True))
        self.assertTrue(m["trace"]["statnamedevents_effective"])
        self.assertEqual(m["capabilities"]["memreport"], True)
        self.assertEqual(len(m["warnings"]), 1)
        self.assertIn("non-finite", m["warnings"][0])

    def test_deterministic(self):
        for name in ACCEPT:
            a = json.dumps(accept_manifest(name), ensure_ascii=False)
            b = json.dumps(accept_manifest(name), ensure_ascii=False)
            self.assertEqual(a, b, name)


if __name__ == "__main__":
    unittest.main()
