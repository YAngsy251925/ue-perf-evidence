#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""significance.py — R9 的显著性判定（M3.3）

规格（PLAN §6 R9）：
  - 每组 n ≥ 5 次 run，每次 run 取自己的 p50 帧间隔（R15：interval，不是 duration）
  - s = 各次 run 的 p50 之间的标准差（不是帧级标准差）
  - 噪声门：组内 CV < 3%
  - 阈值 = max(0.5 ms, t × √(s_a²/n_a + s_b²/n_b))，t 取 Welch 自由度下的 97.5% 分位
  - |Δ| 超过阈值才报“变快 / 变慢”，否则报“在噪声内”
  - 同时报 MDE（80% 检出率）：(t + z_0.80) × 同一个标准误，说明多小的变化测不出来

只用标准库：t 分位数用正则化不完全 beta 函数（Numerical Recipes 的连分式）反解，不依赖 scipy。
"""
import math

NOISE_CV_PCT = 3.0
MIN_N = 5
MIN_THRESHOLD_MS = 0.5
Z_80 = 0.8416212335729143  # 单侧 80% 分位
P_975 = 0.975


# ---------------------------------------------------------------- t 分位数

def _betacf(a, b, x, max_iter=200, eps=3.0e-12):
    """不完全 beta 函数的连分式（Lentz 法）。"""
    tiny = 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a, b, x):
    """正则化不完全 beta 函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_quantile(p, df):
    """Student t 的 p 分位数（0.5 < p < 1）。df 很大时退化为正态分位。"""
    if df <= 0:
        raise ValueError("df must be positive")
    if p <= 0.5 or p >= 1.0:
        raise ValueError("p must be in (0.5, 1)")
    if df > 1.0e7:
        return _norm_quantile(p)
    # P(T <= t) = 1 - 0.5 * I_{df/(df+t^2)}(df/2, 1/2)，对 x 二分再反解 t
    target = 2.0 * (1.0 - p)  # = I_x(df/2, 1/2)
    lo, hi = 1.0e-12, 1.0 - 1.0e-12
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if betai(df / 2.0, 0.5, mid) > target:  # I_x 随 x 单调增：值偏大说明 x 取大了
            hi = mid
        else:
            lo = mid
        if hi - lo < 1.0e-14:
            break
    x = 0.5 * (lo + hi)
    return math.sqrt(df * (1.0 - x) / x)


def _norm_quantile(p):
    """正态分位的 Acklam 近似（只在 df 极大时兜底用）。"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ---------------------------------------------------------------- 组统计

def group_stats(values):
    """一组 run 的 p50 值 → n / median / mean / std / CV。std 是样本标准差（n-1）。"""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(clean)
    if n == 0:
        return {"n": 0, "values": [], "median": None, "mean": None, "std": None, "cv_pct": None}
    ordered = sorted(clean)
    mid = n // 2
    median = ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])
    mean = sum(clean) / n
    if n >= 2:
        var = sum((x - mean) ** 2 for x in clean) / (n - 1)
        std = math.sqrt(var)
        cv = (100.0 * std / mean) if mean else None
    else:
        # 单次 run 没有组内离散度可言：留空，不要报 0（会被误读成“完全稳定”）
        std = None
        cv = None
    return {"n": n, "values": [round(x, 6) for x in clean],
            "median": round(median, 6), "mean": round(mean, 6),
            "std": None if std is None else round(std, 6),
            "cv_pct": None if cv is None else round(cv, 3)}


def welch(sa, na, sb, nb):
    """Welch 标准误、自由度与 97.5% 分位 t。"""
    va = (sa * sa) / na if na else 0.0
    vb = (sb * sb) / nb if nb else 0.0
    se = math.sqrt(va + vb)
    denom = 0.0
    if na > 1:
        denom += (va * va) / (na - 1)
    if nb > 1:
        denom += (vb * vb) / (nb - 1)
    if denom <= 0.0:
        return {"se": 0.0, "df": float("inf"), "t": _norm_quantile(P_975)}
    df = ((va + vb) ** 2) / denom
    return {"se": se, "df": df, "t": t_quantile(P_975, df)}


def judge(values_a, values_b, unit="ms", label="A / B", min_n=MIN_N,
          noise_cv_pct=NOISE_CV_PCT, min_threshold=None):
    """按 R9 判定两组。

    返回 dict：status / verdict / numbers。status ∈ ok | insufficient_n | noise_gate_failed；
    verdict ∈ b_faster | b_slower | within_noise | no_conclusion。delta = mean_b - mean_a，
    单位与输入相同（正值 = B 更慢 / 更大）。
    """
    ga, gb = group_stats(values_a), group_stats(values_b)
    notes = []
    status = "ok"

    if ga["n"] < min_n or gb["n"] < min_n:
        status = "insufficient_n"
        notes.append("每组需要 n ≥ %d 次 run：A 有 %d、B 有 %d → 只报数值，不下变快 / 变慢结论（R9）"
                     % (min_n, ga["n"], gb["n"]))
    for name, g in (("A", ga), ("B", gb)):
        if g["n"] >= 2 and g["cv_pct"] is not None and g["cv_pct"] >= noise_cv_pct:
            status = "noise_gate_failed"
            notes.append("%s 组内 CV %.2f%% ≥ %.1f%% → 噪声基线不合格，不下结论（R9）"
                         % (name, g["cv_pct"], noise_cv_pct))
    if ga["n"] == 0 or gb["n"] == 0:
        return {"status": "not_comparable", "verdict": "no_conclusion",
                "unit": unit, "label": label, "groups": {"a": ga, "b": gb},
                "notes": notes + ["至少一侧没有任何可用的 p50 值"]}

    w = welch(ga["std"] or 0.0, ga["n"], gb["std"] or 0.0, gb["n"])
    if min_threshold is None:
        min_threshold = MIN_THRESHOLD_MS if unit == "ms" else 0.0
    threshold = max(min_threshold, w["t"] * w["se"])
    # MDE 与判决阈值同量纲：也受同一个下限约束，避免报出“能检出 0.04ms 但判决阈值 0.5ms”的自相矛盾
    mde = max(min_threshold, (w["t"] + Z_80) * w["se"])
    delta = gb["mean"] - ga["mean"]

    if status != "ok":
        verdict = "no_conclusion"
    elif abs(delta) > threshold:
        verdict = "b_slower" if delta > 0 else "b_faster"
    else:
        verdict = "within_noise"

    return {
        "status": status, "verdict": verdict, "unit": unit, "label": label,
        "delta_mean": round(delta, 6), "delta_median": round((gb["median"] or 0.0) - (ga["median"] or 0.0), 6),
        "threshold": round(threshold, 6), "mde_80": round(mde, 6),
        "welch": {"se": round(w["se"], 6),
                  "df": None if math.isinf(w["df"]) else round(w["df"], 3),
                  "t_975": round(w["t"], 4)},
        "groups": {"a": ga, "b": gb},
        "notes": notes,
    }


def verdict_short(result, unit="ms"):
    """一句话结论（表格里用，避免和上方的 Δ/阈值重复）。"""
    v = result.get("verdict")
    d = result.get("delta_mean")
    thr = result.get("threshold")
    if v == "b_faster":
        return "B 更快（Δmean %+.3f %s > 阈值 %.3f %s）" % (d, unit, thr, unit)
    if v == "b_slower":
        return "B 更慢（Δmean %+.3f %s > 阈值 %.3f %s）" % (d, unit, thr, unit)
    if v == "within_noise":
        return "在噪声内（|Δmean| %.3f %s ≤ 阈值 %.3f %s）" % (abs(d), unit, thr, unit)
    if result.get("status") == "insufficient_n":
        return "只报数值：每组不足 %d 次 run" % MIN_N
    if result.get("status") == "noise_gate_failed":
        return "只报数值：组内 CV 未过 %.1f%% 噪声门" % NOISE_CV_PCT
    return "不下结论（%s）" % result.get("status")


def verdict_text(result, unit="ms"):
    """把 verdict 变成一句人话（digest 与 diff 共用）。"""
    v = result.get("verdict")
    d = result.get("delta_mean")
    thr = result.get("threshold")
    if v == "b_faster":
        return "B 更快：Δmean=%+.3f %s，超过阈值 %.3f %s（R9）" % (d, unit, thr, unit)
    if v == "b_slower":
        return "B 更慢：Δmean=%+.3f %s，超过阈值 %.3f %s（R9）" % (d, unit, thr, unit)
    if v == "within_noise":
        return "在噪声内：|Δmean|=%.3f %s ≤ 阈值 %.3f %s" % (abs(d), unit, thr, unit)
    if result.get("status") == "insufficient_n":
        return "只报数值：每组不足 %d 次 run，不下变快 / 变慢结论（R9）" % MIN_N
    if result.get("status") == "noise_gate_failed":
        return "只报数值：组内 CV 未过 %.1f%% 噪声门（R9）" % NOISE_CV_PCT
    return "不下结论（%s）" % result.get("status")
