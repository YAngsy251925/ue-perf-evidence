#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_golden.py — golden 口径回归（PLAN §10.2，M1.7）

golden/golden.json 固化了四条实测 run 的 manifest 口径、三份 cpu digest 与一份 diff 的关键数字
（无机器路径、无时间戳）。本测试用当前代码重算同一份数据并逐字段对比：引擎升级或导出器口径
变化时会在这里失败——那是防线起作用了，确认变化是预期后用 `python -X utf8 -m tests.make_golden`
重新生成并提交。

运行（仓库根目录）：python -X utf8 -m unittest discover -s tests -t .
"""
import json
import os
import unittest

from tests.acceptance_data import collect_golden, have_acceptance_data

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(REPO, "golden", "golden.json")


@unittest.skipUnless(have_acceptance_data(), "local acceptance runs not present")
@unittest.skipUnless(os.path.isfile(GOLDEN), "golden/golden.json missing (run: python -X utf8 -m tests.make_golden)")
class GoldenReplayTest(unittest.TestCase):
    def test_fresh_output_matches_golden(self):
        fresh = collect_golden()
        with open(GOLDEN, encoding="utf-8") as f:
            expected = json.load(f)
        a = json.dumps(fresh, ensure_ascii=False, sort_keys=True, indent=1)
        b = json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=1)
        self.assertEqual(
            a, b,
            "口径回归失败：对比上方 diff 找到变化的字段；确认是预期变化（引擎升级 / 导出器变化 / "
            "口径规则修改）后，运行 python -X utf8 -m tests.make_golden 重新生成 golden.json 并提交")

    def test_golden_schema_and_coverage(self):
        with open(GOLDEN, encoding="utf-8") as f:
            g = json.load(f)
        self.assertEqual(g["schema"], "perf-golden/0")
        self.assertEqual(set(g["runs"]), {"example", "b2_smoke", "c_hitch", "d3d12"})
        self.assertEqual(set(g["digests"]), {"example", "b2_smoke", "d3d12"})
        self.assertEqual(list(g["diffs"]), ["example_vs_d3d12"])
        # 口径类 / 形态类都有；性能类待 M2.5（Lyra）
        self.assertEqual({r["category"] for r in g["runs"].values()}, {"caliber", "shape"})


if __name__ == "__main__":
    unittest.main()
