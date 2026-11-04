#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5.4：单帧 scope 树的自耗时与渲染（纯函数，不跑引擎）。"""
import unittest

from pipeline.digest.views import single_frame


def scope(name, depth, dur):
    return {"name": name, "depth": depth, "begin_ms": 0.0, "duration_ms": dur}


class SelfTimeTest(unittest.TestCase):
    def test_children_subtract_from_parent(self):
        scopes = [scope("Frame", 0, 10.0), scope("Tick", 1, 6.0), scope("Leaf", 2, 6.0), scope("Other", 1, 4.0)]
        own = single_frame.self_times(scopes)
        self.assertAlmostEqual(own[0], 0.0)      # Frame = 10 − 6 − 4
        self.assertAlmostEqual(own[1], 0.0)      # Tick = 6 − 6
        self.assertAlmostEqual(own[2], 6.0)      # Leaf
        self.assertAlmostEqual(own[3], 4.0)      # Other

    def test_siblings_do_not_interfere(self):
        scopes = [scope("Parent", 0, 8.0), scope("A", 1, 3.0), scope("B", 1, 5.0), scope("C", 0, 2.0)]
        own = single_frame.self_times(scopes)
        self.assertAlmostEqual(own[0], 0.0)
        self.assertAlmostEqual(own[1], 3.0)
        self.assertAlmostEqual(own[2], 5.0)
        self.assertAlmostEqual(own[3], 2.0)

    def test_sum_of_self_equals_root_duration(self):
        scopes = [scope("Frame", 0, 24.0), scope("Burn1", 1, 2.0), scope("Burn2", 1, 2.0),
                  scope("Slate", 1, 1.0), scope("Child", 2, 0.5)]
        own = single_frame.self_times(scopes)
        self.assertAlmostEqual(sum(own), 24.0, places=6)


class TopSelfTest(unittest.TestCase):
    def test_sorted_desc_and_limited(self):
        # 子项合计 = 10，Frame 自耗时归零；B 最大
        scopes = [scope("Frame", 0, 10.0), scope("A", 1, 5.0), scope("B", 1, 3.0), scope("C", 1, 2.0)]
        own = single_frame.self_times(scopes)
        rows = single_frame.top_self(scopes, own, limit=2)
        self.assertEqual([r["name"] for r in rows], ["A", "B"])
        self.assertAlmostEqual(rows[0]["self_ms"], 5.0)

    def test_zero_self_rows_are_dropped(self):
        scopes = [scope("Frame", 0, 4.0), scope("Tick", 1, 4.0), scope("Leaf", 2, 4.0)]
        rows = single_frame.top_self(scopes, single_frame.self_times(scopes), limit=10)
        self.assertEqual([r["name"] for r in rows], ["Leaf"])


class RenderTest(unittest.TestCase):
    def payload(self, scopes, stat_named=1):
        return {"frame": {"index": 7, "start_s": 1.0, "end_s": 1.024, "duration_ms": 24.0},
                "scope_count": len(scopes), "stat_named": stat_named,
                "stat_named_pct": 100.0 * stat_named / max(1, len(scopes)), "scopes": scopes}

    def test_markdown_has_tree_and_top(self):
        scopes = [scope("Frame", 0, 24.0), scope("Burn", 1, 20.0)]
        md = single_frame.render_md(self.payload(scopes), "GameThread", limit=10)
        self.assertIn("# 单帧 scope 树 — frame 7", md)
        self.assertIn("## 自耗时", md)
        self.assertIn("```", md)
        self.assertIn("Burn", md)

    def test_truncation_notice(self):
        scopes = [scope("S%d" % i, 1, 1.0) for i in range(5)]
        md = single_frame.render_md(self.payload(scopes), limit=2)
        self.assertIn("还有 3 个 scope", md)


if __name__ == "__main__":
    unittest.main()
