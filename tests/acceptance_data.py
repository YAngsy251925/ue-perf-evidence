#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""acceptance_data.py — 验收测试共用的本机实测数据

数据在实验目录 ue5-perf-lab 与 runs/ 下，不进 git；不在本机时，相关测试自动跳过。
"""
import os

from pipeline.caliber.manifest import build_manifest

LAB = os.environ.get("PERF_ACCEPTANCE_LAB", "")  # 本机历史实验数据目录；不设则相关验收测试自动跳过
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B2_RUN = os.path.join(REPO, "runs", "m1_1", "B2_smoke_bridge")

# 名称 -> (probe 导出目录, 引擎日志)
ACCEPT = {
    "example": (os.path.join(LAB, "out_probe_ex"), None),
    "b2_smoke": (os.path.join(B2_RUN, "extract"), os.path.join(B2_RUN, "engine.log")),
    "c_hitch": (os.path.join(LAB, "out_probe_c"), os.path.join(LAB, "coexist_C.log")),
    "d3d12": (os.path.join(LAB, "out_probe_d"), os.path.join(LAB, "coexist_D.log")),
}

# 名称 -> 源 trace（digest v1 的窗口导出需要）
TRACE = {
    "example": os.path.join(LAB, "example.utrace"),
    "b2_smoke": os.path.join(B2_RUN, "trace.utrace"),
    "c_hitch": os.path.join(LAB, "coexist_C.utrace"),
    "d3d12": os.path.join(LAB, "coexist_D.utrace"),
}


def have_acceptance_data() -> bool:
    return all(os.path.isdir(extract) for extract, _ in ACCEPT.values())


def accept_manifest(name: str, **kwargs) -> dict:
    extract, log = ACCEPT[name]
    return build_manifest(os.path.join(REPO, "runs", "acceptance", name), log, extract, None, **kwargs)


# ---- golden 回归集（PLAN §10.2，M1.7）------------------------------------------------
# golden.json 只固化与机器无关的数据（无输入路径、无时间戳）；重新生成用 python -X utf8 -m tests.make_golden

GOLDEN_RUNS = ("example", "b2_smoke", "c_hitch", "d3d12")
GOLDEN_CATEGORY = {"example": "caliber", "b2_smoke": "caliber", "c_hitch": "caliber", "d3d12": "shape"}
GOLDEN_DIGESTS = ("example", "b2_smoke", "d3d12")
GOLDEN_DIFF = ("example", "d3d12")
ENGINE_KEYS = ("version", "build", "build_config", "platform",
               "rhi", "rhi_state", "rhi_source", "rhi_evidence", "feature_level")
TRACE_KEYS = ("channels_requested", "timer_count", "statnamedevents_requested",
              "statnamedevents_effective", "bridge_evidence")


def _golden_manifest(name: str) -> dict:
    m = accept_manifest(name)
    return {
        "category": GOLDEN_CATEGORY[name],
        "measurable": m["measurable"],
        "measurable_reason": m["measurable_reason"],
        "engine": {k: m["engine"][k] for k in ENGINE_KEYS},
        "map": m["map"],
        "mode": m["mode"],
        "log_first_timestamp_utc": m["log_first_timestamp_utc"],
        "capabilities": m["capabilities"],
        "trace": {k: m["trace"][k] for k in TRACE_KEYS},
        "windows": m["windows"],
        "window_basis": m["window_basis"],
        "frame_semantics": m["frame_semantics"],
        "frames_seen": m["frames_seen"],
        "pending": m["pending"],
        "warnings": m["warnings"],
    }


def _golden_digest(name: str) -> dict:
    from pipeline.digest import run_digest
    manifest = accept_manifest(name)
    digest, _ = run_digest.build_run_digest(
        os.path.join(REPO, "runs", "acceptance", name), manifest, TRACE.get(name))
    st = digest["stats"]
    return {
        "capability": digest["capability"],
        "conclusion": digest["conclusion"],
        "rhi_state": digest["caliber"]["rhi_state"],
        "stats": None if st is None else {
            "window": st["window"],
            "timers": st["timers"],
            "total_self_s": st["total_self_s"],
            "top_by_self_time": st["top_by_self_time"],
            "wait_total": st["wait_total"],
            "by_prefix": st["by_prefix"],
            "omitted": st["omitted"],
        },
        "warnings": digest["warnings"],
    }


def _golden_diff() -> dict:
    from pipeline.digest import diff
    sides = {}
    for key, name in zip(("a", "b"), GOLDEN_DIFF):
        sides[key] = {"run_dir": os.path.join(REPO, "runs", "acceptance", name),
                      "manifest": accept_manifest(name), "trace": TRACE[name]}
    d, _ = diff.build_diff(sides["a"], sides["b"])
    return {
        "capability": d["capability"],
        "note": d["note"],
        "sides": d["sides"],
        "top_delta_first": d["top_delta"][0],
        "top_delta_names": [r["name"] for r in d["top_delta"]],
        "wait": {"a": d["wait"]["a"], "b": d["wait"]["b"], "delta_s": d["wait"]["delta_s"],
                 "rows": d["wait"]["rows"]},
        "new_in_b_total": d["new_in_b"]["total"],
        "only_in_a_total": d["only_in_a"]["total"],
        "warnings": d["warnings"],
    }


def collect_golden() -> dict:
    """重算全部 golden 数据（窗口导出走 prepare_rows 缓存，重放约 3 秒）。"""
    return {
        "schema": "perf-golden/0",
        "runs": {name: _golden_manifest(name) for name in GOLDEN_RUNS},
        "digests": {name: _golden_digest(name) for name in GOLDEN_DIGESTS},
        "diffs": {"%s_vs_%s" % GOLDEN_DIFF: _golden_diff()},
    }
