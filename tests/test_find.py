#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按名字找 scope（find）：匹配、正则、上限。"""
import unittest

from pipeline.digest.views import find


NAMES = ["NS_WallPortal", "NS_WallPortalEnter", "Lyra_Portal_01", "Slate::Prepass",
         "TemporalSuperResolution(sg.AntiAliasingQuality=3)", "GCNL_Death_C"]


class MatchTest(unittest.TestCase):
    def test_substring_is_case_insensitive(self):
        self.assertEqual(find.match(NAMES, "portal"), ["Lyra_Portal_01", "NS_WallPortal",
                                                       "NS_WallPortalEnter"])

    def test_regex(self):
        hits = find.match(NAMES, r"GCNL|Slate", regex=True)
        self.assertEqual(hits, ["GCNL_Death_C", "Slate::Prepass"])

    def test_no_hit(self):
        self.assertEqual(find.match(NAMES, "nonexistent"), [])

    def test_limit(self):
        self.assertEqual(len(find.match(NAMES, "portal", limit=2)), 2)

    def test_dedup_and_sorted(self):
        hits = find.match(NAMES + NAMES, "portal")
        self.assertEqual(hits, sorted(set(hits)))


class TraceOfTest(unittest.TestCase):
    def test_run_dir_maps_to_trace(self):
        import os
        self.assertEqual(find._trace_of("runs/x"), os.path.join("runs/x", "trace.utrace"))

    def test_utrace_passthrough(self):
        self.assertEqual(find._trace_of("a/b/x.utrace"), "a/b/x.utrace")


if __name__ == "__main__":
    unittest.main()
