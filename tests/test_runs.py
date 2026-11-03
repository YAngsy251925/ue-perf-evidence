#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`perfctl runs`（M3.2）：run 列表与一行摘要，只读 manifest.json。"""
import io
import json
import os
import tempfile
import unittest

from pipeline.digest import runs


def _write_manifest(run_dir, payload):
    os.makedirs(run_dir, exist_ok=True)
    with io.open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


class RunsSummaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_measurable_run_summary(self):
        run_dir = os.path.join(self.root, "run_a")
        _write_manifest(run_dir, {
            "schema": "perf-manifest/0",
            "run_id": "run_a",
            "measurable": True,
            "measurable_reason": "real RHI (D3D12)",
            "engine": {"rhi": "D3D12"},
            "windows": {"steady": [10.0, 40.5]},
            "frames_seen": 1200,
            "trace": {"file": "trace.utrace"},
        })
        with open(os.path.join(run_dir, "trace.utrace"), "wb") as f:
            f.write(b"0" * 2000)

        rows = runs.collect(self.root)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["manifest"])
        self.assertTrue(row["measurable"])
        self.assertEqual(row["rhi"], "D3D12")
        self.assertEqual(row["steady_s"], 30.5)
        self.assertEqual(row["frames"], 1200)
        self.assertEqual(row["trace_mb"], 0.0)
        line = runs.format_row(row)
        self.assertIn("run_a", line)
        self.assertIn("yes", line)

    def test_not_measurable_reason_is_shown(self):
        run_dir = os.path.join(self.root, "smoke")
        _write_manifest(run_dir, {
            "schema": "perf-manifest/0",
            "run_id": "smoke",
            "measurable": False,
            "measurable_reason": "nullrhi",
            "engine": {"rhi": "Null"},
        })
        row = runs.collect(self.root)[0]
        self.assertFalse(row["measurable"])
        line = runs.format_row(row)
        self.assertIn("NO", line)
        self.assertIn("nullrhi", line)

    def test_missing_manifest_is_listed_not_fatal(self):
        os.makedirs(os.path.join(self.root, "raw"), exist_ok=True)
        row = runs.collect(self.root)[0]
        self.assertFalse(row["manifest"])
        self.assertEqual(row["reason"], "no manifest.json")
        self.assertIn("NO" if row["measurable"] is False else "-", runs.format_row(row))

    def test_filter_and_json_output(self):
        for name in ("m3_0", "m3_1", "lyra_hitch_01"):
            _write_manifest(os.path.join(self.root, name),
                            {"run_id": name, "measurable": True, "engine": {"rhi": "D3D12"}})
        rows = runs.collect(self.root, "m3_")
        self.assertEqual([r["run_id"] for r in rows], ["m3_0", "m3_1"])

        out = os.path.join(self.root, "runs.json")
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            old = runs.sys.stdout
            runs.sys.stdout = devnull
            try:
                rc = runs.main(["--root", self.root, "--filter", "m3_", "--json"])
            finally:
                runs.sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(out))  # JSON 走 stdout，不落文件

    def test_frames_seen_dict_takes_game_track(self):
        run_dir = os.path.join(self.root, "tracks")
        _write_manifest(run_dir, {
            "run_id": "tracks",
            "measurable": True,
            "engine": {"rhi": "D3D12"},
            "frames_seen": {"game_frame_track": 6416, "rt_frame_scopes": 10569},
        })
        row = runs.collect(self.root)[0]
        self.assertEqual(row["frames"], 6416)

    def test_empty_root_returns_nonzero(self):
        rc = runs.main(["--root", os.path.join(self.root, "nope")])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
