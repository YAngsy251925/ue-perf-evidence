#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_profile.py — M2.1 采集 profile 与条件对账的单元测试

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t . -v
"""
import copy
import json
import os
import tempfile
import unittest

from pipeline.capture import profile as P
from pipeline.caliber import conditions as C
from pipeline.caliber.engine_log import parse_engine_log_text
from tests.acceptance_data import ACCEPT, have_acceptance_data


def shipped(name):
    prof, _ = P.load_profile(name)
    return prof


class ShippedProfilesTest(unittest.TestCase):
    def test_both_profiles_validate(self):
        for name in ("smoke", "measure", "measure_regress"):
            self.assertEqual(P.validate(shipped(name)), [], name)

    def test_measure_regress_only_adds_injection(self):
        """M3.4 的 B 组必须与 measure 完全同条件，唯一差别是两条注入 cvar。"""
        base, regress = shipped("measure"), shipped("measure_regress")
        for key in ("map", "rhi", "window", "flags", "trace", "cvars", "csv",
                    "conditions", "stop", "set_res_cvar"):
            self.assertEqual(regress.get(key), base.get(key), key)
        self.assertEqual(base.get("exec_cmds"), [])
        self.assertEqual(regress.get("exec_cmds"),
                         ["PerfEvidence.BusyCount 10", "PerfEvidence.BusyMs 2"])
        args = P.build_args(regress, os.path.join(tempfile.gettempdir(), "r"), "<uproject>")
        exec_arg = [x for x in args if x.startswith("-ExecCmds=")][0]
        self.assertIn("PerfEvidence.BusyCount 10", exec_arg)

    def test_measure_pins_section_7_2(self):
        cvars = shipped("measure")["cvars"]
        self.assertEqual({k: cvars[k] for k in P.MEASURE_REQUIRED_CVARS},
                         {"t.MaxFPS": "0", "r.VSync": "0", "r.GTSyncType": "0", "t.IdleWhenNotForeground": "0"})


class ValidateTest(unittest.TestCase):
    def broken(self, base, mutate):
        p = copy.deepcopy(shipped(base))
        mutate(p)
        return P.validate(p)

    def assertProblem(self, problems, text):
        self.assertTrue(any(text in x for x in problems), "%r not in %r" % (text, problems))

    def test_class_rhi_rules(self):
        self.assertProblem(self.broken("measure", lambda p: p.update(rhi="null")), "real RHI")
        self.assertProblem(self.broken("smoke", lambda p: p.update(rhi="default")), "rhi=null")

    def test_measure_requires_pinned_cvars_and_window(self):
        self.assertProblem(self.broken("measure", lambda p: p["cvars"].pop("r.VSync")), "r.VSync")
        self.assertProblem(self.broken("measure", lambda p: p.update(window=None)), "fixed window")

    def test_trace_channel_rules(self):
        self.assertProblem(self.broken("measure", lambda p: p["trace"]["channels"].append("file")),
                           "platform file I/O")
        self.assertProblem(self.broken("measure", lambda p: p["trace"]["channels"].remove("frame")), "'frame'")
        self.assertProblem(self.broken("measure", lambda p: p["trace"]["channels"].remove("gpu")), "'gpu'")

    def test_exec_command_rules(self):
        self.assertProblem(self.broken("smoke", lambda p: p.update(exec_cmds=["quit"])), "P1")
        self.assertProblem(self.broken("smoke", lambda p: p.update(exec_cmds=["stat fps,stat unit"])), "','")
        self.assertProblem(self.broken("measure", lambda p: p["cvars"].update({"r.Foo": "1,2"})), "','")

    def test_bat_safety(self):
        self.assertProblem(self.broken("smoke", lambda p: p.update(map="/Game/\u5730\u56fe")), "non-ASCII")
        self.assertProblem(self.broken("smoke", lambda p: p.update(map="/Game/A%B")), "contains %")
        self.assertProblem(self.broken("smoke", lambda p: p["flags"].append("-ExecCmds=x")), "generated")

    def test_conditions_and_stop(self):
        self.assertProblem(self.broken("smoke", lambda p: p["conditions"].update(duration_s=0)), "duration_s")
        self.assertProblem(self.broken("smoke", lambda p: p["stop"].update(mode="kill")), "harness_duration")
        self.assertProblem(self.broken("smoke", lambda p: p["stop"].pop("settle_s")), "settle_s")

    def test_csv_rules(self):
        self.assertProblem(self.broken("measure", lambda p: p.update(csv={"coverage_start_s": 10,
                                                                          "stop_at_s": 45, "exit": "exit_after_csv"})),
                           "CSV-free segment")
        self.assertProblem(self.broken("measure", lambda p: p["csv"].update(stop_at_s=10)), "stop_at_s must be >")
        self.assertProblem(self.broken("measure", lambda p: p["csv"].update(exit="harness")), "exit_after_csv")
        self.assertProblem(self.broken("measure", lambda p: p["stop"].update(mode="clean_exit", exit_budget_s=0)),
                           "exit_budget_s")
        self.assertProblem(self.broken("measure", lambda p: (p["stop"].update(mode="clean_exit"),
                                                             p.pop("csv"))[1]), "clean_exit requires a csv block")
        self.assertEqual(P.validate(shipped("measure")), [])  # shipped 配置本身合法

    def test_csv_args_include_event_pair(self):
        args = P.build_args(shipped("measure"), r"D:\run", r"D:\h.uproject")
        for expected in ("PerfEvidence.CoverageStart=35", "PerfEvidence.CaptureStop=45",
                         '-csvStartOnEvent="PerfEvidence CoverageStart"',
                         "-csvCaptureOnEventFrameCount=200000",
                         '-csvStopOnEvent="PerfEvidence CaptureStop"',
                         "-ExitAfterCsvProfiling"):
            self.assertIn(expected, args)

    def test_schema(self):
        self.assertProblem(self.broken("smoke", lambda p: p.update(schema="perf-profile/9")), "schema")


class BuildTest(unittest.TestCase):
    def test_exec_commands_order(self):
        self.assertEqual(P.exec_commands(shipped("measure")),
                         ["t.MaxFPS 0", "r.VSync 0", "r.GTSyncType 0", "t.IdleWhenNotForeground 0",
                          "r.SetRes 960x540w"])
        self.assertEqual(P.exec_commands(shipped("smoke")), [])

    def test_measure_args(self):
        args = P.build_args(shipped("measure"), r"D:\run", r"D:\host\x.uproject")
        self.assertEqual(args[:3], ['"D:\\host\\x.uproject"', "/Engine/Maps/Entry", "-game"])
        self.assertNotIn("-nullrhi", args)
        self.assertIn("-trace=cpu,frame,gpu,bookmark", args)  # 不带 file 通道
        self.assertIn('-tracefile="D:\\run\\trace.utrace"', args)
        self.assertIn('-ABSLOG="D:\\run\\engine.log"', args)
        self.assertEqual(args[-1], '-ExecCmds="t.MaxFPS 0,r.VSync 0,r.GTSyncType 0,'
                                   't.IdleWhenNotForeground 0,r.SetRes 960x540w"')

    def test_smoke_args(self):
        args = P.build_args(shipped("smoke"), r"D:\run", r"D:\host\x.uproject", map_override="/Game/Other")
        self.assertIn("-nullrhi", args)
        self.assertEqual(args[1], "/Game/Other")
        self.assertFalse(any(a.startswith("-ExecCmds") for a in args))
        self.assertFalse(any(a.startswith("-resx") for a in args))

    def test_bat_is_ascii_crlf(self):
        prof = shipped("measure")
        bat = P.render_bat(r"D:\UE\UnrealEditor-Cmd.exe", P.build_args(prof, r"D:\run", r"D:\h.uproject"),
                           prof, P.canonical_sha256(prof))
        bat.encode("ascii")
        self.assertTrue(bat.endswith("\r\n"))
        self.assertNotIn("\n", bat.replace("\r\n", ""))
        self.assertIn('"D:\\UE\\UnrealEditor-Cmd.exe" "D:\\h.uproject"', bat)
        self.assertEqual(P.harness_timing(prof), (300, 75))  # clean_exit：stop_at_s 45 + exit_budget_s 30

    def test_sha_is_canonical(self):
        prof = shipped("measure")
        reordered = json.loads(json.dumps(prof, sort_keys=True))
        self.assertEqual(P.canonical_sha256(prof), P.canonical_sha256(reordered))


@unittest.skipUnless(os.path.isfile(os.path.join(os.environ.get("UE_ROOT", P.DEFAULT_UE_ROOT),
                                                 "Engine", "Binaries", "Win64", "UnrealEditor-Cmd.exe")),
                     "UE install not present")
class PrepareTest(unittest.TestCase):
    def test_prepare_writes_bat_and_snapshot_and_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "run_a")
            snap = P.prepare("measure", run)
            self.assertEqual((snap["profile_name"], snap["profile_class"]), ("measure", "measure"))
            self.assertEqual((snap["harness"]["startup_budget_s"], snap["harness"]["run_after_ready_s"]), (300, 75))
            self.assertTrue(snap["harness"]["command"].endswith(" 300 - - 75"))
            self.assertEqual(snap["csv"]["coverage_start_s"], 35.0)
            with open(os.path.join(run, "launch.bat"), "rb") as f:
                f.read().decode("ascii")
            with open(os.path.join(run, C.SNAPSHOT_FILE), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["profile_sha256"], snap["profile_sha256"])
            with open(os.path.join(run, "engine.log"), "w", encoding="utf-8") as f:
                f.write("x")
            with self.assertRaises(ValueError):
                P.prepare("measure", run)


ECHO_LOG = """LogInit: Build Configuration: Development
[2026.11.02-10.00.00:000][  0]t.MaxFPS = "0"
[2026.11.02-10.00.00:001][  0]r.VSync = "0"
[2026.11.02-10.00.00:002][  0]r.GTSyncType = "0"
[2026.11.02-10.00.00:003][  0]t.IdleWhenNotForeground = "0"
[2026.11.02-10.00.00:004][  0]r.SetRes = "960x540w"
[2026.11.02-10.00.01:000][  1]LogConsoleManager: Warning: Setting the console variable 'r.GTSyncType' with 'SetByCode' was ignored as it is lower priority than the previous 'SetByConsole'. Value remains '0'
[2026.11.02-10.00.01:001][  1]LogTemp: Foo = "bar"
"""


def measure_snapshot():
    prof = shipped("measure")
    return {"schema": P.SNAPSHOT_SCHEMA, "profile_name": "measure", "profile_class": "measure",
            "profile_sha256": P.canonical_sha256(prof), "profile_file": "harness/profiles/measure.json",
            "profile": prof, "exec_commands": P.exec_commands(prof)}


def verify(snapshot=None, log_text=ECHO_LOG, rhi_state="real", measurable=True, bridge=True,
           channels=("cpu", "frame", "gpu", "bookmark"), warmup=60, steady=(10.0, 45.0), build="Development"):
    log_facts = None if log_text is None else parse_engine_log_text(log_text)
    return C.verify(snapshot or measure_snapshot(), log_facts, rhi_state, measurable, bridge,
                    None if channels is None else list(channels),
                    {"warmup_frames_requested": warmup}, {"steady": None if steady is None else list(steady)}, build)


def by_item(conditions_block):
    return {it["item"]: it for it in conditions_block["items"]}


class EchoParseTest(unittest.TestCase):
    def test_echoes_ignored_sets_and_false_positives(self):
        f = parse_engine_log_text(ECHO_LOG + '[2026.11.02-10.00.02:000][  2]t.MaxFPS = "60"\n')
        self.assertEqual(f["cvar_echoes"]["t.maxfps"], ["0", "60"])
        self.assertNotIn("foo", f["cvar_echoes"])  # 名字不含点的不算回显
        self.assertEqual(f["cvar_ignored"], [{"cvar": "r.GTSyncType", "set_by": "Code",
                                              "kept_by": "Console", "value": "0"}])


class VerifyTest(unittest.TestCase):
    def test_all_good(self):
        prof, cond, warnings = verify()
        items = by_item(cond)
        self.assertEqual(prof["class"], "measure")
        for name in ("t.MaxFPS", "r.VSync", "r.GTSyncType", "t.IdleWhenNotForeground", "resolution",
                     "rhi", "statnamedevents", "trace_channels", "warmup_frames", "duration_s", "profile_class"):
            self.assertIs(items[name]["verified"], True, name)
        self.assertIn("lower-priority", items["r.GTSyncType"]["evidence"])
        self.assertEqual(sorted(cond["summary"]["unchecked"]), ["camera", "frame_generation"])
        self.assertEqual(cond["summary"]["failed"], [])
        self.assertEqual(warnings, [])

    def test_missing_echo_and_last_echo_wins(self):
        text = ECHO_LOG.replace('r.VSync = "0"', "nothing") + '[x][  9]t.MaxFPS = "60"\n'
        _, cond, warnings = verify(log_text=text)
        items = by_item(cond)
        self.assertIs(items["r.VSync"]["verified"], False)
        self.assertEqual((items["t.MaxFPS"]["observed"], items["t.MaxFPS"]["verified"]), ("60", False))
        self.assertEqual(sorted(cond["summary"]["failed"]), ["r.VSync", "t.MaxFPS"])
        self.assertEqual(len(warnings), 2)

    def test_rhi_class_duration_and_channels(self):
        _, cond, _ = verify(rhi_state="null", measurable=False, steady=(10.0, 20.0), channels=("cpu", "frame"))
        items = by_item(cond)
        for name in ("rhi", "profile_class", "duration_s", "trace_channels"):
            self.assertIs(items[name]["verified"], False, name)
        self.assertEqual(items["duration_s"]["observed"], 10.0)

    def test_no_log_and_no_snapshot(self):
        _, cond, _ = verify(log_text=None, channels=None)
        self.assertIsNone(by_item(cond)["t.MaxFPS"]["verified"])
        self.assertIsNone(by_item(cond)["trace_channels"]["verified"])
        self.assertEqual(C.verify(None, None, "real", True, True, [], {}, {}, None), (None, None, []))

    def test_shipping_warns(self):
        _, _, warnings = verify(build="Shipping")
        self.assertTrue(any("Shipping" in w for w in warnings))


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
class ManifestSnapshotTest(unittest.TestCase):
    def test_warmup_comes_from_snapshot(self):
        from pipeline.caliber.manifest import build_manifest
        extract, log = ACCEPT["example"]
        with tempfile.TemporaryDirectory() as tmp:
            snap = measure_snapshot()
            snap["profile"]["conditions"]["warmup_frames"] = 30
            with open(os.path.join(tmp, C.SNAPSHOT_FILE), "w", encoding="utf-8") as f:
                json.dump(snap, f)
            m = build_manifest(tmp, log, extract, None)
        self.assertEqual(m["window_basis"]["warmup_frames_requested"], 30)
        self.assertEqual(m["profile"]["name"], "measure")
        self.assertIs(by_item(m["conditions"])["warmup_frames"]["verified"], True)  # 声明 30，manifest 用的也是 30
        # example 没有引擎日志：cvar 无从核对
        self.assertIsNone(by_item(m["conditions"])["t.MaxFPS"]["verified"])

    def test_no_snapshot_means_no_profile(self):
        from tests.acceptance_data import accept_manifest
        m = accept_manifest("example")
        self.assertIsNone(m["profile"])
        self.assertIsNone(m["conditions"])
        self.assertNotIn("profile / conditions (M2.1)", m["pending"])


class ApplyCoverageTest(unittest.TestCase):
    """P15 双窗口隔离：coverage_probe 与干净 stats 窗口的派生。"""

    def test_stats_derived_and_capability_true(self):
        from pipeline.caliber.manifest import apply_coverage
        windows = {"startup": [0.0, 14.0], "frames": [14.0, 59.1], "steady": [14.5, 59.1], "shutdown": [59.1, 60.6]}
        basis = {"stats_window": "steady"}
        warnings = []
        cap = apply_coverage(windows, basis, {"start": 49.07, "end": 59.07}, None, warnings)
        self.assertIs(cap, True)
        self.assertEqual(windows["coverage_probe"], [49.07, 59.07])
        self.assertEqual(windows["stats"], [14.5, 49.07])
        self.assertEqual(basis["stats_window"], "stats")
        self.assertIn("P15", basis["stats_window_derivation"])
        self.assertEqual(warnings, [])

    def test_fallback_when_coverage_covers_steady(self):
        from pipeline.caliber.manifest import apply_coverage
        windows = {"steady": [30.0, 59.1]}
        basis = {"stats_window": "steady"}
        warnings = []
        cap = apply_coverage(windows, basis, {"start": 29.0, "end": 59.0}, None, warnings)
        self.assertIs(cap, True)
        self.assertIsNone(windows.get("stats"))
        self.assertEqual(basis["stats_window"], "steady")
        self.assertTrue(warnings and "no CSV-free stats window" in warnings[0])


E2E = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs", "m2_1")


def e2e_manifest(name):
    from pipeline.caliber.manifest import build_manifest
    run = os.path.join(E2E, name)
    return build_manifest(run, os.path.join(run, "engine.log"), os.path.join(run, "extract"), None)


@unittest.skipUnless(all(os.path.isfile(os.path.join(E2E, n, "extract", "frame_tracks.csv"))
                         for n in ("smoke_02", "measure_01")), "M2.1 end-to-end runs not present")
class EndToEndTest(unittest.TestCase):
    """PLAN M2.1 验收：两套 profile 经 harness 实跑后，manifest 对账全部通过。"""

    def test_smoke_02(self):
        m = e2e_manifest("smoke_02")
        self.assertEqual((m["profile"]["name"], m["measurable"]), ("smoke", False))
        self.assertEqual(m["conditions"]["summary"], {"verified": 6, "failed": [], "unchecked": []})

    def test_measure_01(self):
        m = e2e_manifest("measure_01")
        self.assertEqual((m["profile"]["name"], m["measurable"], m["engine"]["rhi"]), ("measure", True, "D3D12"))
        s = m["conditions"]["summary"]
        self.assertEqual((s["verified"], s["failed"], sorted(s["unchecked"])), (11, [], ["camera", "frame_generation"]))
        # t.MaxFPS 0 / r.VSync 0 生效：Entry 地图不锁帧，帧间隔远低于 60 fps 的 16.67ms
        self.assertLess(m["frame_semantics"]["game"]["stats"]["interval_ms"]["p50"], 10.0)
        self.assertTrue(m["frame_semantics"]["consistency"]["ok"])


E2E_M22 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs", "m2_2")


@unittest.skipUnless(os.path.isfile(os.path.join(E2E_M22, "measure_05", "coverage.json")),
                     "M2.2 end-to-end run not present")
class CleanExitAcceptanceTest(unittest.TestCase):
    """PLAN M2.2 验收（measure_05，CSV 事件对开停 + 干净退出）。"""

    def test_manifest(self):
        from pipeline.caliber.manifest import build_manifest
        run = os.path.join(E2E_M22, "measure_05")
        m = build_manifest(run, os.path.join(run, "engine.log"), os.path.join(run, "extract"), None)
        w = m["windows"]
        self.assertEqual([round(x, 3) for x in w["coverage_probe"]], [49.07, 59.069])
        # stats = steady ∖ coverage（P15）：干净段 ≥ 声明 30s
        self.assertEqual((round(w["stats"][0], 3), round(w["stats"][1], 3)), (14.475, 49.07))
        self.assertGreaterEqual(w["stats"][1] - w["stats"][0], 30.0)
        self.assertEqual(m["capabilities"]["csv_profiler"], True)
        s = m["conditions"]["summary"]
        self.assertEqual((s["verified"], s["failed"]), (13, []))
        # 干净退出：无强杀截断，零非有限帧；trace 有 shutdown 段
        self.assertGreater(w["shutdown"][1], w["shutdown"][0])
        self.assertEqual((m["frame_semantics"]["game"]["nonfinite_dropped"],
                          m["frame_semantics"]["render"]["nonfinite_dropped"]), (0, 0))
        # 引擎日志有 RequestExit(0, CsvProfiler.ExitAfterCsvProfiling)
        self.assertTrue(m["conditions"]["items"] and
                        by_item(m["conditions"])["clean_exit"]["verified"] is True)


if __name__ == "__main__":
    unittest.main()
