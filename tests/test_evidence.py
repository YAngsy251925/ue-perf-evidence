#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perf_evidence（`perfctl evidence`）：ref 解析、分页、三种证据源。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest.views import evidence


def write(d, name, text):
    path = os.path.join(d, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


class RefParseTest(unittest.TestCase):
    def test_timer_ref(self):
        r = evidence.parse_ref("timer:PerfEvidenceRegress@stats")
        self.assertEqual(r, {"kind": "timer", "name": "PerfEvidenceRegress", "window": "stats"})

    def test_timer_ref_with_explicit_window(self):
        r = evidence.parse_ref("timer:Foo.Bar@12.5-45.0")
        self.assertEqual(r["name"], "Foo.Bar")
        self.assertEqual(r["window"], "12.5-45.0")

    def test_frame_and_hitch(self):
        self.assertEqual(evidence.parse_ref("frame:700"), {"kind": "frame", "index": 700})
        self.assertEqual(evidence.parse_ref("hitch:3"), {"kind": "hitch", "index": 3})

    def test_bad_ref_lists_forms(self):
        with self.assertRaises(ValueError) as ctx:
            evidence.parse_ref("whatever:1")
        self.assertIn("timer:Name@stats", str(ctx.exception))


class WindowTest(unittest.TestCase):
    def test_named_window_from_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "manifest.json", json.dumps({
                "windows": {"stats": [10.0, 20.0], "coverage_probe": [20.0, 30.0]},
                "window_basis": {"stats_window": "stats"}}))
            self.assertEqual(evidence.resolve_window(d, "stats"), (10.0, 20.0))
            self.assertEqual(evidence.resolve_window(d, "coverage"), (20.0, 30.0))

    def test_explicit_window(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(evidence.resolve_window(d, "1.5 - 3.0"), (1.5, 3.0))

    def test_unknown_window_raises(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "manifest.json", json.dumps({"windows": {"stats": [0.0, 1.0]}}))
            with self.assertRaises(ValueError):
                evidence.resolve_window(d, "nope")


class PaginateTest(unittest.TestCase):
    ROWS = list(range(25))

    def test_first_page_has_hint(self):
        p = evidence.paginate(self.ROWS, 1, 10)
        self.assertEqual(p["rows"], list(range(10)))
        self.assertEqual(p["total"], 25)
        self.assertEqual(p["next_page"], 2)
        self.assertIn("--page 2", p["truncated_hint"])

    def test_last_page_has_no_hint(self):
        p = evidence.paginate(self.ROWS, 3, 10)
        self.assertEqual(p["rows"], [20, 21, 22, 23, 24])
        self.assertIsNone(p["next_page"])
        self.assertIsNone(p["truncated_hint"])

    def test_page_beyond_end(self):
        p = evidence.paginate(self.ROWS, 9, 10)
        self.assertEqual(p["rows"], [])
        self.assertIsNone(p["next_page"])

    def test_pages_do_not_overlap(self):
        seen = []
        for page in (1, 2, 3):
            seen += evidence.paginate(self.ROWS, page, 10)["rows"]
        self.assertEqual(seen, self.ROWS)


class SourcesTest(unittest.TestCase):
    def test_timer_rows_from_cache(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "manifest.json", json.dumps({"windows": {"stats": [1.0, 2.0]}}))
            write(d, os.path.join("b2", "b2_calls_Foo_1.000-2.000.json"),
                  json.dumps({"thread": "GameThread", "window": [1.0, 2.0], "events_in_window": 3,
                              "matched": 2, "collected": 2, "truncated": False,
                              "sum_ms": 3.0, "max_ms": 2.0,
                              "calls": [{"start_s": 2.0, "dur_ms": 2.0, "depth": 7},
                                        {"start_s": 1.5, "dur_ms": 1.0, "depth": 7}]}))
            rows, ctx, bad = evidence.timer_rows(d, evidence.parse_ref("timer:Foo@stats"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["seq"], 0)
        self.assertEqual(ctx["matched"], 2)
        self.assertIsNone(bad)

    def test_timer_rows_without_cache_says_how(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "manifest.json", json.dumps({"windows": {"stats": [1.0, 2.0]}}))
            with self.assertRaises(FileNotFoundError) as ctx:
                evidence.timer_rows(d, evidence.parse_ref("timer:Foo@stats"))
        self.assertIn("--b2", str(ctx.exception))

    def test_timer_truncation_warning(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "manifest.json", json.dumps({"windows": {"stats": [1.0, 2.0]}}))
            write(d, os.path.join("b2", "b2_calls_Foo_1.000-2.000.json"),
                  json.dumps({"thread": "GameThread", "window": [1.0, 2.0], "matched": 900,
                              "collected": 10, "truncated": True, "sum_ms": 1.0, "max_ms": 1.0,
                              "calls": [{"start_s": 1.0, "dur_ms": 1.0, "depth": 1}]}))
            _rows, _ctx, bad = evidence.timer_rows(d, evidence.parse_ref("timer:Foo@stats"))
        self.assertIn("900", bad)

    def test_frame_rows(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, os.path.join("b2", "b2_frame_7_GameThread.json"),
                  json.dumps({"frame": {"index": 7, "start_s": 1.0, "end_s": 1.024, "duration_ms": 24.0},
                              "scope_count": 2, "stat_named_pct": 10.0,
                              "scopes": [{"name": "Frame", "depth": 0, "begin_ms": 0.0, "duration_ms": 24.0},
                                         {"name": "Burn", "depth": 1, "begin_ms": 1.0, "duration_ms": 20.0}]}))
            rows, ctx, bad = evidence.frame_rows(d, evidence.parse_ref("frame:7"))
        self.assertEqual([r["name"] for r in rows], ["Frame", "Burn"])
        self.assertEqual(ctx["scope_count"], 2)
        self.assertIsNone(bad)

    def test_hitch_rows_and_snapshot_link(self):
        tree = {"name": "GameThread", "ms": 195.4, "calls": 5,
                "children": [{"name": "FrameTime", "ms": 195.2, "calls": 2, "children": []}]}
        with tempfile.TemporaryDirectory() as d:
            write(d, "hitches_index.json", json.dumps({
                "hitches": [{"index": 1, "stats_frame": 3, "ms": 195.4, "game_tree": [tree],
                             "render_tree": []}],
                "snapshots": [{"stats_frame": 3, "snapshot": "hitches/Frame-3.utrace",
                               "screenshot": "hitches/Frame-3.png"}]}))
            rows, ctx, bad = evidence.hitch_rows(d, evidence.parse_ref("hitch:1"))
        self.assertEqual([r["name"] for r in rows], ["GameThread", "FrameTime"])
        self.assertEqual(rows[1]["depth"], 1)
        self.assertEqual(ctx["snapshots"][0]["snapshot"], "hitches/Frame-3.utrace")
        self.assertIsNone(bad)

    def test_hitch_index_missing(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "hitches_index.json", json.dumps({"hitches": [], "snapshots": []}))
            with self.assertRaises(ValueError):
                evidence.hitch_rows(d, evidence.parse_ref("hitch:4"))


class RenderTest(unittest.TestCase):
    def base(self, ref, rows):
        return {"schema": "perf-evidence/0", "ref": ref, "total": len(rows), "page": 1,
                "page_size": 200, "next_page": None, "truncated_hint": None, "rows": rows,
                "context": {"thread": "GameThread", "window": [1.0, 2.0]}, "warnings": []}

    def test_timer_table(self):
        md = evidence.render_md(self.base("timer:Foo@stats",
                                          [{"seq": 0, "start_s": 1.0, "start_rel_ms": 0.0,
                                            "dur_ms": 2.0, "depth": 7}]))
        self.assertIn("timer:Foo@stats", md)
        self.assertIn("| 0 | 1.000000 | 2.0000 | 7 |", md)

    def test_hitch_and_frame_render_differently(self):
        frame_md = evidence.render_md(self.base("frame:7",
                                                [{"name": "Frame", "depth": 0, "begin_ms": 0.0, "dur_ms": 24.0}]))
        hitch_md = evidence.render_md(self.base("hitch:1",
                                                [{"side": "game_tree", "depth": 0, "name": "GT",
                                                  "dur_ms": 195.4, "calls": 5}]))
        self.assertIn("| scope | depth | begin(ms) | dur(ms) |", frame_md)
        self.assertIn("| side | depth | name | dur(ms) | calls |", hitch_md)


if __name__ == "__main__":
    unittest.main()
