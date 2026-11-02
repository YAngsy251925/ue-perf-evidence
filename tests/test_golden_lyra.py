#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_golden_lyra.py — Lyra 性能类 golden（PLAN §10.2 / M2.5，性质断言）

golden/lyra_expectations.json 声明 Lyra hitch-hunt run 必须满足的不变量：
memreport hitch 的根因、量级、快照与截图存在、Q3。新 run 的帧号必然不同，
所以这是性质断言（property-based），不是逐字节对比。

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t .
验收数据不在本机时自动跳过。
"""
import json
import os
import unittest

from pipeline.digest.views import hitch as hitch_view

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(REPO, "golden", "lyra_expectations.json")
RUN = os.path.join(REPO, "runs", "lyra_hitch_01")


@unittest.skipUnless(os.path.isfile(GOLDEN), "golden/lyra_expectations.json missing")
@unittest.skipUnless(os.path.isfile(os.path.join(RUN, "hitches_index.json")), "Lyra run not present")
class LyraGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GOLDEN, encoding="utf-8") as f:
            cls.golden = json.load(f)
        manifest = None
        manifest_path = os.path.join(RUN, "manifest.json")
        if os.path.isfile(manifest_path):  # 与视图 main 的行为一致：manifest 存在就传进去
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        cls.digest, cls.md = hitch_view.build(RUN, manifest)

    def test_memreport_hitch_present_with_expected_root_cause_and_scale(self):
        exp = self.golden["memreport_hitch"]
        hits = [h for h in self.digest["hitches"]
                if any(n["name"] == exp["root_cause"] for n in h["tree_top"])]
        self.assertGreaterEqual(len(hits), exp["min_hitches"] if "min_hitches" in exp else 1,
                                "no hitch with root cause %s" % exp["root_cause"])
        h = hits[0]
        self.assertGreaterEqual(h["ms"], exp["min_ms"],
                                "memreport hitch %0.1fms below the Lyra-scale floor %0.1fms" % (h["ms"], exp["min_ms"]))
        snap = next(s for s in (json.load(open(os.path.join(RUN, "hitches_index.json"), encoding="utf-8"))
                                ["snapshots"]) if s["stats_frame"] == h["stats_frame"])
        if exp["snapshot_required"]:
            self.assertIsNotNone(snap["snapshot"])
            self.assertTrue(os.path.isfile(os.path.join(RUN, snap["snapshot"])))
        if exp["screenshot_required"]:
            self.assertIsNotNone(snap["screenshot"])
            self.assertTrue(os.path.isfile(os.path.join(RUN, snap["screenshot"])))

    def test_run_shape(self):
        exp = self.golden["run_shape"]
        self.assertEqual(self.digest["caliber"]["rhi_state"], exp["rhi_state"])
        self.assertGreaterEqual(len(self.digest["hitches"]), exp["min_hitches_with_memreport"])
        # Q3 写在 manifest：行数差 ≤2
        manifest_path = os.path.join(RUN, "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path), "Lyra golden run needs a manifest")
        with open(manifest_path, encoding="utf-8") as f:
            m = json.load(f)
        c = m["frame_semantics"]["consistency"]
        self.assertLessEqual(c["rows_delta"], 2)

    def test_deterministic_view(self):
        a, md_a = hitch_view.build(RUN, None)
        b, md_b = hitch_view.build(RUN, None)
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))
        self.assertEqual(md_a, md_b)


if __name__ == "__main__":
    unittest.main()
