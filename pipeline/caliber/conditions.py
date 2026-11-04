#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""conditions.py — 采集声明与证据对账（PLAN §7.2，M2.1）

输入：run 目录里的 capture_profile.json（perfctl profile prepare 写的声明快照）、引擎日志事实、
manifest 已算出的口径。输出 manifest 的 profile 与 conditions 两块：每一条声明都给出
declared / observed / verified 与证据来源，verified 只有三种值：
  true   有正面证据且一致
  false  有证据但不一致，或该有证据却没有（写 warning）
  null   这一项没有自动核对手段（如 camera），或缺日志无从核对

声明以证据为准，不以命令行为准（与 R1 的桥接判定同一原则）：
  cvar / r.SetRes   ExecCmds 回显 `<cvar> = "<value>"`，取最后一次
  rhi               manifest.engine.rhi_state（日志 / trace 证据）
  statnamedevents   trace 里的桥接证据（R1），不是命令行
  trace 通道        引擎日志 Command Line 里的 -trace=
  warmup_frames     window_basis.warmup_frames_requested
  duration_s        稳态窗口长度 ≥ 声明值
"""

SNAPSHOT_FILE = "capture_profile.json"

_CLEAN_EXIT_MARK = "FPlatformMisc::RequestExit(0, CsvProfiler.ExitAfterCsvProfiling)"


def _log_lines(log_facts):
    """engine_log 的事实里没存原始行，干净退出的判定需要全文——由调用方把标记塞进 facts。"""
    return log_facts.get("_raw_lines", []) if log_facts else []


def values_match(declared, observed) -> bool:
    try:
        return float(declared) == float(observed)
    except (TypeError, ValueError):
        return str(declared).strip().lower() == str(observed).strip().lower()


def _item(name, kind, declared, observed, verified, evidence):
    return {"item": name, "kind": kind, "declared": declared, "observed": observed,
            "verified": verified, "evidence": evidence}


def check_cvar(name, declared, log_facts):
    if log_facts is None:
        return _item(name, "cvar", declared, None, None, "no engine log")
    echoes = log_facts.get("cvar_echoes", {}).get(name.lower())
    if not echoes:
        return _item(name, "cvar", declared, None, False, "no echo in engine log (ExecCmds not executed?)")
    observed = echoes[-1]
    ignored = [x for x in log_facts.get("cvar_ignored", []) if x["cvar"].lower() == name.lower()]
    evidence = "engine log echo (%d time(s))" % len(echoes)
    if ignored:
        evidence += "; %d lower-priority set(s) ignored, e.g. SetBy%s" % (len(ignored), ignored[0]["set_by"])
    return _item(name, "cvar", declared, observed, values_match(declared, observed), evidence)


def verify(snapshot, log_facts, rhi_state, measurable, bridge_effective, channels_requested,
           window_basis, windows, build_config, coverage=None):
    """返回 (profile 块, conditions 块, warnings)。snapshot 为 None 时返回 (None, None, [])。"""
    if snapshot is None:
        return None, None, []
    mode = snapshot.get("capture_mode")
    if mode in ("external", "pie", "packaged"):
        # 外部采集（PIE / Insights 录制 / 打包包）：我们没参与设条件，逐条"声明 vs 观测"只会有噪声，
        # 所以这里明确标"未强制"，fact（RHI / measurable / 桥接）仍由 manifest 从日志与 trace 里判。
        return (
            {"name": snapshot.get("profile_name") or mode, "class": mode,
             "sha256": None, "file": None, "conditions": {}},
            {"enforced": False, "mode": mode, "verified": 0, "failed": [], "unchecked": [],
             "summary": {"verified": 0, "failed": [], "unchecked": []}},
            ["外部采集（%s）：采集条件未由本工具强制，manifest 只记录能从日志/trace 观测到的事实（PLAN R2）" % mode])
    profile = snapshot["profile"]
    cond = profile.get("conditions") or {}
    items = []

    for name, value in (profile.get("cvars") or {}).items():
        items.append(check_cvar(name, value, log_facts))
    window = profile.get("window")
    if window:
        declared = "%dx%dw" % (window["resx"], window["resy"])
        if profile.get("set_res_cvar"):
            res = check_cvar("r.SetRes", declared, log_facts)
            res.update(item="resolution", kind="window")
        else:
            res = _item("resolution", "window", declared, None, None,
                        "command line -resx/-resy only (no log evidence without CSV Profiler)")
        items.append(res)

    csv = snapshot.get("csv")
    if csv:
        declared_start = csv["coverage_start_s"]
        observed_start = None if coverage is None else coverage.get("start")
        start_verified = None
        if observed_start is not None and windows.get("frames"):
            # 声明的时间基是"引擎就绪后秒数"（ticker 随主循环启动）；书签是 trace 时间
            #（进程启动起算）。用首帧开始（≈就绪点）换算：observed - frames[0] ≈ declared。
            origin = windows["frames"][0]
            start_verified = abs(float(observed_start) - origin - float(declared_start)) <= 2.0
        items.append(_item("csv_coverage_start", "csv", declared_start, observed_start, start_verified,
                           "trace bookmark PerfEvidence CoverageStart (minus first-frame origin)"
                           if observed_start is not None
                           else "no coverage.json (perfctl capture not used, or TraceDigestPerf found no events)"))
        clean_mark = "FPlatformMisc::RequestExit(0, CsvProfiler.ExitAfterCsvProfiling)"
        clean = bool(log_facts and any(
            clean_mark in line for line in _log_lines(log_facts)))
        items.append(_item("clean_exit", "csv", "exit_after_csv",
                           "requested" if clean else "not requested / not logged", clean,
                           "engine log %s" % clean_mark if clean else "engine log lacks the RequestExit mark"))

    expected_rhi = "null" if profile["rhi"] == "null" else "real"
    items.append(_item("rhi", "engine", profile["rhi"], rhi_state,
                       None if rhi_state == "undetermined" else rhi_state == expected_rhi,
                       "manifest.engine.rhi_state"))
    items.append(_item("statnamedevents", "trace", bool(profile.get("statnamedevents")), bridge_effective,
                       bool(profile.get("statnamedevents")) == bool(bridge_effective),
                       "bridge evidence in trace (R1)"))
    declared_ch = sorted((profile.get("trace") or {}).get("channels") or [])
    observed_ch = None if channels_requested is None else sorted(channels_requested)
    items.append(_item("trace_channels", "trace", declared_ch, observed_ch,
                       None if observed_ch is None else declared_ch == observed_ch,
                       "-trace= in the engine log command line"))
    items.append(_item("warmup_frames", "window", cond.get("warmup_frames"),
                       window_basis.get("warmup_frames_requested"),
                       cond.get("warmup_frames") == window_basis.get("warmup_frames_requested"),
                       "window_basis.warmup_frames_requested"))
    steady = windows.get("steady")
    steady_s = None if steady is None else round(steady[1] - steady[0], 3)
    items.append(_item("duration_s", "window", cond.get("duration_s"), steady_s,
                       steady_s is not None and steady_s >= cond.get("duration_s", 0),
                       "steady window length >= declared duration"))
    for key in sorted(k for k in cond if k not in ("warmup_frames", "duration_s")):
        items.append(_item(key, "declared", cond[key], None, None, "no automatic check"))
    expected = profile["class"] == "measure"
    items.append(_item("profile_class", "profile", profile["class"], "measurable" if measurable else "not_measurable",
                       measurable == expected, "manifest.measurable"))

    warnings = []
    if build_config and build_config.lower() == "shipping" and snapshot.get("exec_commands"):
        warnings.append("ExecCmds are not parsed in Shipping builds; the declared cvars cannot take effect")
    for it in items:
        if it["verified"] is False:
            warnings.append("capture condition %s: declared %r, observed %r (%s)" % (
                it["item"], it["declared"], it["observed"], it["evidence"]))
    profile_block = {"name": snapshot["profile_name"], "class": snapshot["profile_class"],
                     "sha256": snapshot["profile_sha256"], "file": snapshot.get("profile_file"),
                     "snapshot": SNAPSHOT_FILE}
    conditions_block = {
        "items": items,
        "summary": {
            "verified": sum(1 for it in items if it["verified"] is True),
            "failed": [it["item"] for it in items if it["verified"] is False],
            "unchecked": [it["item"] for it in items if it["verified"] is None],
        },
    }
    return profile_block, conditions_block, warnings
