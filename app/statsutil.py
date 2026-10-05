"""少量统计分布函数：正态 CDF/P PF 与卡方生存函数/分位数。

只依赖 math，不引入 scipy 等统计库。
- 正态 CDF 用 math.erf；反函数用 Peter Acklam 有理逼近（|误差| < ~1.15e-9）。
- 卡方生存函数（上侧概率）用正则化下侧不完全伽马函数的级数/连分式展开
  （Numerical Recipes, NR6 的实现思路）。
- 卡方分位数先给 Wilson–Hilferty 近似，再用对生存函数的二分法打磨到 1e-10。
"""
from __future__ import annotations

import math

# Acklam 反正态 CDF 系数
_A = [-3.969683028665376e+01, 2.209460984245205e+02,
      -2.759285104469687e+02, 1.383577518672690e+02,
      -3.066479806614716e+01, 2.506628277459239e+00]
_B = [-5.447609879822406e+01, 1.615858368580409e+02,
      -1.556989798598866e+02, 6.680131188771972e+01,
      -1.328068155288572e+01]
_C = [-7.784894002430293e-03, -3.223964580411365e-01,
      -2.400758277161838e+00, -2.549732539343734e+00,
      4.374664141464968e+00, 2.938163982698783e+00]
_D = [7.784695709041462e-03, 3.224671290700398e-01,
      2.445134137142996e+00, 3.754408661907416e+00]


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 逼近，再一步 Halley 修正）。"""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("norm_ppf: p 必须在 (0,1) 内")
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    # 一步 Newton 打磨
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    pp = norm_cdf(x)
    dphi = -x * pdf
    x = x - (pp - p) / pdf * (1.0 + 0.5 * (pp - p) * dphi / (pdf * pdf))
    return x


def _gser(a: float, x: float) -> float:
    """正则化下侧不完全伽马 P(a,x)，x < a+1 用级数。"""
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    term = total
    for _ in range(500):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    """正则化上侧不完全伽马 Q(a,x)，x >= a+1 用改进连分式。"""
    b = x + 1.0 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 501):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(x: float, df: int) -> float:
    """卡方生存函数 P(χ²_df > x)。"""
    if x <= 0.0:
        return 1.0
    a = df / 2.0
    xx = x / 2.0
    if xx < a + 1.0:
        return 1.0 - _gser(a, xx)
    return _gcf(a, xx)


def chi2_ppf(p_lower: float, df: int) -> float:
    """卡方下侧 p 分位数（p_lower 为累积概率）。Wilson–Hilferty + 二分打磨。"""
    if not 0.0 < p_lower < 1.0:
        raise ValueError("chi2_ppf: p 必须在 (0,1) 内")
    z = norm_ppf(p_lower)
    d = float(df)
    wh = d * (1.0 - 2.0 / (9.0 * d) + z * math.sqrt(2.0 / (9.0 * d))) ** 3
    lo, hi = max(1e-12, wh * 0.5), wh * 2.0 + 1.0
    # 找一个上界使 sf(hi) <= 1-p（即 CDF(hi) >= p）
    while chi2_sf(hi, df) > 1.0 - p_lower:
        hi *= 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if chi2_sf(mid, df) > 1.0 - p_lower:  # mid 偏小
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-11 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)
