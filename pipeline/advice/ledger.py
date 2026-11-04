#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger.py — 建议的"预测 vs 实测"账本（M9.2）

为什么要有它：`advisor` 给的 `expected_saving_ms` 是**先验估计**（份额 × busy × 0.3–0.6）。
先验一定会偏，偏了就要修，否则建议会退化成"看起来很专业的数字"。

账本的规则（对应 docs/ADVISOR.md §4）：
  1. 每条假设验证完（A/B + R9）就追加一条记录：预测区间 / 实测 Δ / 判定 / 引擎版本 / 场景；
  2. 同一 `hypothesis id` 的历史用来算**校准因子**（实测均值 ÷ 预测中点）；
  3. 校准因子回灌到 advisor 的 `expected_saving_ms`，让下一次的预测更贴近现实；
  4. 连续多次 `within_noise` → 该假设降级（`status: retired`），不再默认输出。

存储：`advice/ledger.json`（数组，便于人读与 diff）。

用法：
  perfctl advise record --hypothesis gpu_effect_share:bloom --diff runs/diff_ab --ledger advice/ledger.json
  perfctl advise ledger --ledger advice/ledger.json
"""
import io
import json
import os
import statistics
import time

SCHEMA = "perf-advice-ledger/0"
MIN_SAMPLES_FOR_FACTOR = 2      # 少于两条样本不校准（避免被单次噪声带跑）
RETIRE_AFTER_NOISE = 3          # 连续 N 次 within_noise → 降级


def load(path):
    if not path or not os.path.isfile(path):
        return {"schema": SCHEMA, "entries": []}
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "entries": []}
    data.setdefault("entries", [])
    return data


def save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def record(path, hypothesis_id, predicted, measured_ms, verdict, status=None,
           engine_version=None, scene=None, run_a=None, run_b=None, note=None):
    """追加一条验证记录。predicted=[lo, hi]（ms/帧），measured_ms 是 A/B 的 Δmean。"""
    data = load(path)
    entry = {
        "hypothesis": hypothesis_id,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "predicted_ms": list(predicted) if predicted else None,
        "measured_ms": measured_ms,
        "verdict": verdict,                    # b_slower / b_faster / within_noise / no_conclusion
        "status": status or ("pending" if verdict in (None, "no_conclusion") else "verified"),
        "engine_version": engine_version,
        "scene": scene,
        "run_a": run_a,
        "run_b": run_b,
        "note": note,
    }
    data["entries"].append(entry)
    save(path, data)
    return entry


def history(data, hypothesis_id):
    return [e for e in data.get("entries") or [] if e.get("hypothesis") == hypothesis_id]


def calibration(data, hypothesis_id):
    """返回该假设的校准信息：样本数、实测中位数、预测中点、比例、是否降级。"""
    rows = [e for e in history(data, hypothesis_id) if e.get("measured_ms") is not None]
    verds = [e.get("verdict") for e in rows]
    noise_streak = 0
    for v in reversed(verds):
        if v == "within_noise":
            noise_streak += 1
        else:
            break
    out = {"hypothesis": hypothesis_id, "samples": len(rows),
           "measured_median_ms": None, "predicted_mid_ms": None,
           "factor": 1.0, "retired": noise_streak >= RETIRE_AFTER_NOISE,
           "noise_streak": noise_streak, "verdicts": verds[-5:]}
    if not rows:
        return out
    measured = [abs(e["measured_ms"]) for e in rows]
    mids = [ (e["predicted_ms"][0] + e["predicted_ms"][1]) / 2.0
             for e in rows if e.get("predicted_ms") ]
    out["measured_median_ms"] = round(statistics.median(measured), 3)
    if mids:
        out["predicted_mid_ms"] = round(statistics.median(mids), 3)
    # 样本太少时不算因子（避免被单次噪声带跑）
    if len(mids) >= MIN_SAMPLES_FOR_FACTOR and out["predicted_mid_ms"]:
        out["factor"] = round(out["measured_median_ms"] / out["predicted_mid_ms"], 3)
    return out


def all_calibrations(data):
    ids = sorted({e.get("hypothesis") for e in data.get("entries") or []})
    return {i: calibration(data, i) for i in ids if i}


def apply_calibration(hypotheses, data):
    """把校准因子与降级状态回灌到假设列表（原地修改并返回）。"""
    cals = all_calibrations(data)
    for h in hypotheses:
        cal = cals.get(h.get("id"))
        if not cal:
            continue
        h["calibration"] = cal
        saving = h.get("expected_saving_ms")
        if saving and cal.get("factor") and cal["factor"] > 0:
            h["expected_saving_ms_raw"] = list(saving)
            h["expected_saving_ms"] = [round(saving[0] * cal["factor"], 2),
                                      round(saving[1] * cal["factor"], 2)]
        if cal.get("retired"):
            h["retired"] = True
            h["kind"] = h.get("kind", "") + "_retired"
    return [h for h in hypotheses if not h.get("retired")]


def render_md(data):
    cals = all_calibrations(data)
    rows = list(data.get("entries") or [])
    lines = ["# 建议账本（预测 vs 实测）", "",
             "| 项 | 值 |", "|---|---|",
             "| 记录数 | %d |" % len(rows),
             "| 涉及假设 | %d |" % len(cals), ""]
    if not rows:
        lines += ["还没有记录。验证完一条假设后：", "",
                  "```bash",
                  "perfctl advise record --hypothesis <id> --diff runs/diff_ab --ledger advice/ledger.json",
                  "```", ""]
        return "\n".join(lines)
    lines += ["## 假设校准状态", "",
              "| 假设 | 样本 | 实测中位 ms | 预测中点 ms | 校准因子 | 连续噪声 | 状态 |",
              "|---|---|---|---|---|---|---|"]
    for hid, cal in sorted(cals.items()):
        lines.append("| %s | %s | %s | %s | ×%s | %s | %s |"
                     % (hid, cal["samples"], cal["measured_median_ms"], cal["predicted_mid_ms"],
                        cal["factor"], cal["noise_streak"], "已降级" if cal["retired"] else "生效"))
    lines += ["", "## 最近记录", "", "| 时间 | 假设 | 预测 ms | 实测 ms | 判定 |",
              "|---|---|---|---|---|"]
    for e in rows[-12:]:
        pred = e.get("predicted_ms")
        lines.append("| %s | %s | %s | %s | %s |"
                     % (e.get("recorded_at"), e.get("hypothesis"),
                        ("%.2f–%.2f" % tuple(pred)) if pred else "-",
                        e.get("measured_ms"), e.get("verdict")))
    lines.append("")
    return "\n".join(lines)


def record_from_diff(ledger_path, hypothesis_id, diff_dir, predicted=None,
                     engine_version=None, scene=None):
    """从 `perfctl diff` 的产出里取 Δmean 与判定，写一条账本记录。"""
    path = os.path.join(diff_dir, "perf_diff.json")
    if not os.path.isfile(path):
        raise FileNotFoundError("perf_diff.json not found: %s" % path)
    with io.open(path, encoding="utf-8") as f:
        diff = json.load(f)
    sig = diff.get("significance") or {}
    measured = sig.get("delta_mean")
    verdict = sig.get("verdict")
    return record(ledger_path, hypothesis_id, predicted, measured, verdict,
                  engine_version=engine_version, scene=scene,
                  run_a=(diff.get("sides") or {}).get("a", {}).get("run_id"),
                  run_b=(diff.get("sides") or {}).get("b", {}).get("run_id"),
                  note="from %s" % os.path.abspath(diff_dir))
