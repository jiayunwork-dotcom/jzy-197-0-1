"""粗差识别：基于守恒残差与测量调整量的统计检验。

线性化数据校正理论（测量误差 ε ~ N(0, Σ_M)，约束 h(x)=0）下：
- 节点约束残差向量（含冗余投影）
      h̃ = h(x̂_lin)，协方差 Σ_h = H Σ_M Hᵀ，秩 r = 测量数 − 未测变量数
- 全局检验（global / chi-square test）
      χ²_r = h̃ᵀ Σ_h⁺ h̃  ~ χ²(r)
- 节点（约束）检验（nodal test）
      v_i = h̃_i / √(Σ_h,ii)  ~ N(0,1)
- 测量检验（measurement test），用各测量的实际校正调整量
      a = x̂_m − x_meas = −Σ_M Hᵀ Σ_h⁺ h_0
      Σ_a = Σ_M Hᵀ Σ_h⁺ H Σ_M
      z_j = a_j / √((Σ_M − Σ_a)_{jj})  ~ N(0,1)
  其中 h_0 是初始测量点的节点不平衡量。
显著性水平默认 0.05，临界值 z_{1−α/2}、χ²_{1−α}(r)。
冗余度 r≤0 时没有统计冗余，test_available=false。
"""
from __future__ import annotations

import numpy as np

from .measurement_model import VariableModel
from .reconciliation import SolveResult
from .statsutil import chi2_ppf, chi2_sf, norm_ppf


def _positive_subspace(sigma_h: np.ndarray, tol_ratio: float = 1e-10
                       ) -> tuple[np.ndarray, np.ndarray]:
    """Σ_h 的正特征子空间：返回 (U1, λ)，Σ_h⁺ = U1·diag(1/λ)·U1ᵀ。"""
    eigvals, eigvecs = np.linalg.eigh(sigma_h)
    eig_max = eigvals[-1] if eigvals.size else 0.0
    pos = eigvals > max(tol_ratio * eig_max, 1e-30)
    return eigvecs[:, pos], eigvals[pos]


def gross_error_test(model: VariableModel, a: np.ndarray,
                     res: SolveResult, significance: float = 0.05) -> dict:
    """对校正结果做全局/节点/测量三类粗差检验。"""
    from .reconciliation import build_residuals_and_jacobian

    n_var = model.n_var
    n_meas = int(np.count_nonzero(model.measured))
    n_unmeas = n_var - n_meas
    redundancy = n_meas - n_unmeas
    if redundancy <= 0:
        return {"test_available": False,
                "reason": "redundancy_le_zero",
                "degrees_of_freedom": int(redundancy),
                "n_measurements": n_meas,
                "suspects": [],
                "global_test": None,
                "constraint_tests": [],
                "measurement_tests": []}

    # 解点雅可比；测量点残差 h_0（未测变量取解值）
    _, h_jac = build_residuals_and_jacobian(res.x, model, a)
    x0 = np.array(res.x, copy=True)
    x0[model.measured] = model.x_meas[model.measured]
    h0, _ = build_residuals_and_jacobian(x0, model, a)

    sigma2 = np.zeros(n_var)
    sigma2[model.measured] = model.sigma[model.measured] ** 2
    sigma_h = h_jac @ np.diag(sigma2) @ h_jac.T

    u1, lam = _positive_subspace(sigma_h)
    r = lam.size
    pinv_h = (u1 / lam) @ u1.T

    # 冗余残差：测量点残差在约束空间里不能被解满足的那一部分。
    # 线性理论下 h̃ = H·(x_meas 方向投影) = h0 − H·Δx_unmeas_span；
    # 用投影矩阵 P = Σ_h Σ_h⁺ 去掉可被约束消去的分量。
    h_tilde = sigma_h @ pinv_h @ h0
    chi = float(h_tilde @ pinv_h @ h_tilde)
    crit = chi2_ppf(1.0 - significance, r)
    p_value = chi2_sf(chi, r)

    z_crit = norm_ppf(1.0 - significance / 2.0)

    # 节点（约束）检验
    diag_h = np.diag(sigma_h)
    nn = a.shape[0]
    constraint_rows = []
    for i in range(h_tilde.shape[0]):
        if diag_h[i] <= 1e-30:
            continue
        kind = "mass" if i < nn else "metal"
        element = None
        if i >= nn:
            element = model.elements[(i - nn) // nn]
        constraint_rows.append({
            "row": int(i), "kind": kind, "node": f"node:{i % nn}",
            "element": element,
            "unadjusted_residual": float(h0[i]),
            "residual": float(h_tilde[i]),
            "standardized_residual": float(h_tilde[i] / np.sqrt(diag_h[i])),
            "suspect": bool(abs(h_tilde[i]) / np.sqrt(diag_h[i]) > z_crit),
        })

    # 测量检验：实际调整量 a = x̂_m − x_meas
    cov_adj = np.diag(sigma2) @ h_jac.T @ pinv_h @ h_jac @ np.diag(sigma2)
    residual_var = np.maximum(sigma2 - np.diag(cov_adj), 0.0)

    measurement_rows, suspects = [], []
    for i, mid in enumerate(model.measurement_ids):
        if mid is None or residual_var[i] <= 1e-30:
            continue
        scale = 100.0 if model.describe(i)["kind"] == "grade" else 1.0
        adj = res.x[i] - model.x_meas[i]
        z = adj / np.sqrt(residual_var[i])
        desc = model.describe(i)
        row = {
            "measurement_id": mid,
            "kind": desc["kind"], "stream": desc["stream"],
            "element": desc.get("element"),
            "measured_value": float(model.x_meas[i] * scale),
            "sigma": float(model.sigma[i] * scale),
            "adjustment": float(adj * scale),
            "relative_adjustment": float(adj / model.x_meas[i])
                if model.x_meas[i] != 0 else 0.0,
            "test_statistic_z": float(z),
            "p_value_two_sided": float(chi2_sf(z * z, 1)),
            "critical_z": float(z_crit),
            "is_suspect": bool(abs(z) > z_crit),
        }
        measurement_rows.append(row)
        if abs(z) > z_crit:
            suspects.append(row)
    suspects.sort(key=lambda r: abs(r["test_statistic_z"]), reverse=True)

    return {
        "test_available": True,
        "alpha": significance,
        "degrees_of_freedom": int(r),
        "n_measurements": n_meas,
        "global_test": {
            "statistic": chi, "critical": float(crit),
            "p_value": float(p_value),
            "gross_error_detected": bool(chi > crit),
        },
        "constraint_tests": constraint_rows,
        "measurement_tests": measurement_rows,
        "suspects": suspects,
    }


def gross_error_test_named(model, a, res, node_codes, significance=0.05):
    """同 gross_error_test，但约束行 node 用真实节点编码标注。"""
    rep = gross_error_test(model, a, res, significance)
    nn = a.shape[0]
    for row in rep.get("constraint_tests", []):
        row["node"] = node_codes[row["row"] % nn]
    return rep


def serial_elimination_scan(topo, data, excluded_base, significance=0.05,
                            max_iterations=50):
    """对当前嫌疑测量逐个剔除后重算，给出剔除后的全局检验（serial elimination）。

    仅供“剔除后重算”决策参考，服务本身不自动删数。
    """
    from .reconciliation import run_reconciliation
    from .topology import incidence_matrix
    a = incidence_matrix(topo)
    base_res, base_model = run_reconciliation(
        topo, data, excluded=frozenset(excluded_base),
        max_iterations=max_iterations)
    node_codes = sorted(topo.node_codes())
    base = gross_error_test_named(base_model, a, base_res, node_codes,
                                  significance)
    out = []
    if base["test_available"]:
        for sus in base["suspects"]:
            mid = sus["measurement_id"]
            res, model = run_reconciliation(
                topo, data, excluded=frozenset(set(excluded_base) | {mid}),
                max_iterations=max_iterations)
            ge = gross_error_test_named(model, a, res, node_codes, significance)
            out.append({
                "excluded": mid,
                "converged": res.converged,
                "global_test": ge["global_test"] if ge["test_available"] else None,
                "remaining_suspects": [s["measurement_id"] for s in ge["suspects"]]
                    if ge["test_available"] else [],
            })
    return {"base": base, "elimination_results": out}
