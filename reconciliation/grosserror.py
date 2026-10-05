"""粗差（显著系统误差/异常样品）统计识别。

两层检验
========
1. **整体 χ² 检验（global test）**：校正目标函数在零假设
   “所有测量仅有给定的正态随机误差”下服从自由度为 ``dof`` 的 χ² 分布。
   ``dof = rank(J) - rank(J_U)``（约束对测量构成的独立限制数）。
   dof=0 时校正对测量没有任何冗余约束，任何统计检验都无意义，直接说明。

2. **测量检验（measurement test）**：测量残差 ``a = y - x̂`` 的协方差为

       Cov(a) = Σ_M - Σ_M J_Mᵀ (J Σ Jᵀ)⁺ J_M Σ_M ,

   其对角元给出每个测量残差的标准差，标准化残差 ``z_i = a_i / sqrt(C_ii)``
   在零假设下为标准正态。|z| 最大且超过双侧阈值（默认 2.58≈99% 分位）
   的测量即为首要嫌疑；剔除它后可重新校正，χ² 应显著下降。

约束行可能线性相关（合流节点的冗余守恒式），(J Σ Jᵀ) 用 SVD 伪逆处理。
χ² 生存函数用正则化不完全伽马函数的级数/连分式实现，不依赖统计库。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

DEFAULT_Z_THRESHOLD = 2.5758293035489004  # 标准正态双侧 0.01 分位


# ---------------------------------------------------------------------------
# 正则化上侧不完全伽马函数 P(a,x)=gammainc(a,x)/Gamma(a)，Q=1-P
# Numerical Recipes 3rd ed., §6.2
# ---------------------------------------------------------------------------

def _gammaincc(a: float, x: float) -> float:
    """正则化上侧不完全伽马函数 Q(a,x) = Γ(a,x)/Γ(a) = P(χ²df > x) 相关量。"""
    if x < 0.0 or a <= 0.0:
        return 1.0 if x <= 0 else 0.0
    if x < a + 1.0:
        # 级数形式求下侧，再取补
        return 1.0 - _gamser(a, x)
    return _gammcf(a, x)


def _gamser(a: float, x: float) -> float:
    if x == 0.0:
        return 0.0
    ap = a
    summ = 1.0 / a
    delta = summ
    for _ in range(400):
        ap += 1.0
        delta *= x / ap
        summ += delta
        if abs(delta) < abs(summ) * 1e-15:
            break
    return summ * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gammcf(a: float, x: float) -> float:
    b = x + 1.0 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 401):
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


def chi2_sf(x: float, dof: int) -> float:
    """χ² 分布的生存函数（上侧概率），dof 为正整数自由度。"""
    if x <= 0.0:
        return 1.0
    if dof <= 0:
        # 自由度为 0 时退化分布在 0；x>0 的 p 值记为 0（不可检验）
        return 0.0
    return _gammaincc(dof / 2.0, x / 2.0)


# ---------------------------------------------------------------------------
# 粗差检验
# ---------------------------------------------------------------------------

def gross_error_report(
    result: dict[str, Any],
    topo: Any,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
) -> dict[str, Any]:
    """根据校正结果计算整体检验与逐测量检验。

    :param result: :func:`solver.reconcile` 的返回值
    """
    internal = result["_internal"]
    x = np.asarray(internal["x"], dtype=float)
    J = np.asarray(internal["J"], dtype=float)
    measured = np.asarray(internal["measured"], dtype=bool)
    sigma = np.asarray(internal["sigma"], dtype=float)

    from .observability import check_redundancy

    stats = check_redundancy(J, measured)
    dof = stats["dof"]
    chi2 = float(result["objective"])
    p_value = chi2_sf(chi2, dof)

    m_idx = np.where(measured)[0]
    S = topo.n_streams
    E = topo.n_elements

    def slot_label(k: int) -> dict[str, Any]:
        if k < S:
            return {
                "stream_id": topo.stream_ids[k],
                "type": "flow",
                "element": None,
            }
        e = (k - S) // S
        j = (k - S) % S
        return {
            "stream_id": topo.stream_ids[j],
            "type": "grade",
            "element": topo.elements[e],
        }

    tests: list[dict[str, Any]] = []
    if dof > 0:
        # B = J Σ Jᵀ，Σ 为测量协方差（未测位无穷，对应贡献为 0）
        sig2 = np.where(measured, sigma**2, 0.0)
        B = J @ (sig2[:, None] * J.T)
        B_pinv = _pinv(B)
        # P = Σ Jᵀ B⁺ J
        P = (sig2[:, None] * J.T) @ B_pinv @ J
        C = np.diag(sig2) - P * sig2[None, :]  # 仅取已测行列有意义
        # 数值上保证对角非负
        diagC = np.maximum(np.diag(C), 0.0)

        slot_of = internal["slot_of"]
        # 反向：变量槽位 -> 测量 id
        inv_slot = {int(v): k for k, v in slot_of.items()}
        for k in m_idx:
            sd = math.sqrt(float(diagC[k]))
            residual = None
            # 测量值 - 校正值
            for mid, slot in slot_of.items():
                if int(slot) == int(k):
                    m_corr = next(c for c in result["corrections"] if c["measurement_id"] == mid)
                    residual = m_corr["measured_value"] - m_corr["adjusted_value"]
                    break
            label = slot_label(int(k))
            if sd > 1e-15:
                z = float(residual / sd)
                testable = True
            else:
                z = None
                testable = False
            tests.append(
                {
                    "measurement_id": inv_slot.get(int(k)),
                    **label,
                    "raw_minus_adjusted": float(residual) if residual is not None else None,
                    "residual_std": sd,
                    "z": z,
                    "abs_z": abs(z) if z is not None else None,
                    "testable": testable,
                    "suspect": bool(testable and z is not None and abs(z) > z_threshold),
                }
            )
        tests.sort(key=lambda t: (-(t["abs_z"] or -1.0), t["measurement_id"] or ""))

    suspects = [t for t in tests if t["suspect"]]
    primary = suspects[0] if suspects else None

    return {
        "dof": dof,
        "chi_square": chi2,
        "p_value": float(p_value),
        "global_test": {
            "statistic": chi2,
            "dof": dof,
            "p_value": float(p_value),
            "failed": bool(dof > 0 and p_value < 0.01),
            "note": None
            if dof > 0
            else "冗余度为 0：测量数刚好（或不足）确定状态，无法做统计粗差检验，"
            "需要增加测量或剔除未测变量后重试。",
        },
        "z_threshold": z_threshold,
        "measurement_tests": tests,
        "suspects": suspects,
        "primary_suspect": primary,
    }


def _pinv(B: np.ndarray, rcond: float = 1e-11) -> np.ndarray:
    """SVD 伪逆，自动裁掉由冗余约束行产生的近零奇异值。"""
    U, s, Vt = np.linalg.svd(B)
    cutoff = rcond * (s[0] if s.size else 1.0)
    s_inv = np.array([1.0 / v if v > cutoff else 0.0 for v in s])
    return (Vt.T * s_inv) @ U.T
