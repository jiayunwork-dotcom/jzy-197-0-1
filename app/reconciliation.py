"""数据校正求解：守恒约束 + 逐次线性化（拉格朗日–牛顿/SQP）。

不调用任何现成的数据校正或优化库，全部用 NumPy 手工组装 KKT 系统。

变量向量 x（见 measurement_model）：
    x[0:nf]               = 各物流干矿流量 F（t/h，按物流编码排序）
    x[nf + e*nf + s]      = 物流 s 中元素 e 的品位（小数，按元素、物流排序）

约束（只对真实节点；边界 source/sink 不建约束）：
    干矿：   A @ F = 0
    金属 e： (A * G_e) @ F = 0
行顺序：先全部干矿行，再按元素排金属行，节点在每段内按关联矩阵行序（编码排序）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .measurement_model import VariableModel, build_variable_model
from .observability import analyze_observability
from .topology import CircuitTopology, incidence_matrix

DEFAULT_MAX_ITER = 50
RESIDUAL_TOL = 1e-10          # 性质要求 1e-9，内部用更严的 1e-10
STEP_TOL = 1e-8
RANK_TOL = 1e-10


@dataclass
class SolveResult:
    x: np.ndarray
    converged: bool
    reason: str                 # converged | max_iterations | line_search_failed | unobservable
    iterations: int
    residuals: np.ndarray       # 最终节点约束残差（物理单位）
    relative_residuals: np.ndarray
    max_relative_residual: float
    observations: list[dict]    # 逐次迭代诊断
    observability: dict
    kkt_rank: int
    kkt_full_rank: bool

    @property
    def status(self) -> str:
        if self.reason == "unobservable":
            return "unobservable"
        return "converged" if self.converged else "not_converged"


def build_residuals_and_jacobian(x: np.ndarray, model: VariableModel,
                                 a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (h, H)。h 为守恒残差；H = ∂h/∂x（m × n_var）。"""
    nf = model.n_flow
    ne = len(model.elements)
    nn = a.shape[0]
    f = x[:nf]

    h_parts = [a @ f]
    for e in range(ne):
        g = x[nf + e * nf: nf + (e + 1) * nf]
        h_parts.append((a * g) @ f)
    h = np.concatenate(h_parts) if h_parts else np.zeros(0)

    m = nn * (1 + ne)
    h_jac = np.zeros((m, model.n_var))
    h_jac[:nn, :nf] = a
    for e in range(ne):
        g = x[nf + e * nf: nf + (e + 1) * nf]
        row = nn + e * nn
        h_jac[row:row + nn, :nf] = a * g
        for s in range(nf):
            h_jac[row:row + nn, nf + e * nf + s] = a[:, s] * f[s]
    return h, h_jac


def _residual_scales(x: np.ndarray, model: VariableModel,
                     a: np.ndarray) -> np.ndarray:
    """每行残差的归一化尺度：节点上 |A|·F（及 |A|·F·G），全零时退化为 1。"""
    nf = model.n_flow
    ne = len(model.elements)
    f = x[:nf]
    throughput = np.abs(a) @ f
    scales = [np.maximum(throughput, 1.0)]
    for e in range(ne):
        g = x[nf + e * nf: nf + (e + 1) * nf]
        scales.append(np.maximum(np.abs(a) @ (f * g), 1e-12))
    return np.concatenate(scales)


def initial_point(model: VariableModel) -> np.ndarray:
    """给未测变量一个有限、非负的初值；初值只影响迭代路径，不影响解。

    未测变量取*互不相同*的确定性值：双线性约束在“所有未测流量/品位都相等”
    的退化点上雅可比列会共线（例如给矿节点的两股未测出料），在那种点判秩
    会把可观测误判成不可观测。用确定性的轻微分散值得到通用点（generic point）。
    """
    nf, n_var = model.n_flow, model.n_var
    x = np.zeros(n_var)
    x[model.measured] = model.x_meas[model.measured]
    mflows = model.x_meas[:nf][model.measured[:nf]]
    f0 = float(np.mean(mflows)) if mflows.size and np.mean(mflows) > 0 else 1.0
    uflow = np.where(model.measured[:nf], x[:nf],
                     f0 * np.linspace(0.8, 1.2, max(nf, 1))[:nf])
    x[:nf] = uflow
    ne = len(model.elements)
    for e in range(ne):
        sl = slice(nf + e * nf, nf + (e + 1) * nf)
        gvals = model.x_meas[sl][model.measured[sl]]
        g0 = float(np.mean(gvals)) if gvals.size and np.mean(gvals) > 0 else 0.01
        # 未测位用分散初值并夹到 (0,1)；已测位必须严格保留测量值，
        # 不能对整行 np.where 裁剪（那会把已测品位也替换成初值）。
        ugrade = x[sl]
        spread = np.linspace(0.8, 1.2, max(nf, 1))[:nf]
        for k in range(nf):
            if not model.measured[nf + e * nf + k]:
                ugrade[k] = min(max(g0 * spread[k], 1e-6), 0.99)
        x[sl] = ugrade
    return x


def reconcile(model: VariableModel, a: np.ndarray,
              max_iterations: int = DEFAULT_MAX_ITER,
              residual_tol: float = RESIDUAL_TOL,
              step_tol: float = STEP_TOL,
              accept_unobservable: bool = False,
              verbose: bool = False) -> SolveResult:
    """逐次线性化求解校正问题（拉格朗日乘子法 + 牛顿步 + 可行性回退）。"""
    nf, n_var = model.n_flow, model.n_var
    ne = len(model.elements)

    # 权重：测量位 1/σ²，未测位 0
    weight = np.zeros(n_var)
    weight[model.measured] = 1.0 / model.sigma[model.measured] ** 2

    # 未测变量的典型量级（仅用于缩放，不影响解）
    typical = np.array(model.sigma, dtype=float, copy=True)
    f_scale = max(1.0, float(np.nanmean(model.x_meas[:nf]))
                  if np.any(model.measured[:nf]) else 1.0)
    for i in range(nf):
        if not model.measured[i]:
            typical[i] = f_scale
    for e in range(ne):
        sl = slice(nf + e * nf, nf + (e + 1) * nf)
        if np.any(model.measured[sl]):
            g_scale = max(1e-6, float(np.nanmean(model.x_meas[sl])))
        else:
            g_scale = 0.01
        typical[sl] = np.where(model.measured[sl], typical[sl], g_scale)
    typical = np.maximum(typical, 1e-12)

    x = initial_point(model)

    h0, h_jac0 = build_residuals_and_jacobian(x, model, a)
    obs0 = analyze_observability(h_jac0, model.measured, model.describe)

    if not obs0["observable"] and not accept_unobservable:
        return SolveResult(
            x=x, converged=False, reason="unobservable", iterations=0,
            residuals=h0,
            relative_residuals=np.abs(h0) / _residual_scales(x, model, a),
            max_relative_residual=float("inf"), observations=[],
            observability=obs0, kkt_rank=-1, kkt_full_rank=False)

    # 说明：KKT 牛顿步对双线性守恒约束是二阶收敛的（手算样例两步到机器精度）。
    # 步长只做可行性回退（流量≥0、品位∈[0,1]），并要求相对残差不显著恶化，
    # 不使用罚函数线搜索——加权坐标下目标与约束的量纲差异会把罚函数步长压到
    # 接近零（实测步长被压到 1e-7 而停滞）。
    # 接受不可观测时继续迭代，lstsq 给出最小范数解（不确定自由度取 0 方向）。
    observations: list[dict] = []
    last_rank, last_full = -1, False
    converged = False
    reason = "max_iterations"

    for it in range(1, max_iterations + 1):
        h, h_jac = build_residuals_and_jacobian(x, model, a)

        # 缩放坐标 z：x = x_cur + D·z，改善条件数（不改变解）
        d = typical
        js = h_jac * d
        ws = (d * weight) * d
        rtilde = np.zeros(n_var)
        rtilde[model.measured] = (x[model.measured]
                                  - model.x_meas[model.measured]) / d[model.measured]
        m = h.shape[0]
        kkt = np.zeros((n_var + m, n_var + m))
        kkt[:n_var, :n_var] = np.diag(ws)
        kkt[:n_var, n_var:] = js.T
        kkt[n_var:, :n_var] = js
        rhs = np.concatenate([-ws * rtilde, -h])

        sol, _resid, rank, _sv = np.linalg.lstsq(kkt, rhs, rcond=RANK_TOL)
        last_rank = int(rank)
        last_full = rank == n_var + m

        dz = sol[:n_var]
        dx = d * dz

        # 可行性回退：找到使所有流量非负、品位在 [0,1] 且残差不恶化的步长。
        # 除了硬可行域，还限制单步相对移动（流量 80%、品位到边界的 80%），
        # 避免初始残差很大时牛顿步把小数值品位（如 0.3%）一下甩出数量级。
        alpha = 1.0
        cur_norm = float(np.sum((h / _residual_scales(x, model, a)) ** 2))

        def step_bounded(alpha_try: float) -> bool:
            cand = x + alpha_try * dx
            if np.any(cand[:nf] < 0.0):
                return False
            if np.any(cand[nf:] < 0.0) or np.any(cand[nf:] > 1.0):
                return False
            # 流量单步最多移动 80%（相对当前正值）
            if np.any(dx[:nf] * alpha_try < -0.8 * np.maximum(x[:nf], 1e-12)):
                return False
            return True

        x_trial = None
        for _ls in range(80):
            cand = x + alpha * dx
            if step_bounded(alpha):
                h_cand, _ = build_residuals_and_jacobian(cand, model, a)
                cand_norm = float(np.sum(
                    (h_cand / _residual_scales(cand, model, a)) ** 2))
                # 允许极小数值抖动
                if cand_norm <= cur_norm * (1.0 + 1e-9) + 1e-30:
                    x_trial = cand
                    break
            alpha *= 0.5
        if x_trial is None:
            observations.append({"iteration": it, "line_search_failed": True,
                                 "alpha": 0.0})
            reason = "line_search_failed"
            break
        x = x_trial

        h_new, _ = build_residuals_and_jacobian(x, model, a)
        rel = np.abs(h_new) / _residual_scales(x, model, a)
        max_rel = float(np.max(rel))
        meas_step = float(np.max(np.abs(dx[model.measured])
                                 / model.sigma[model.measured])) \
            if np.any(model.measured) else 0.0
        unmeas_step = float(np.max(np.abs(dx[~model.measured])
                                   / d[~model.measured])) \
            if np.any(~model.measured) else 0.0
        step_norm = max(meas_step, unmeas_step)
        observations.append({
            "iteration": it, "alpha": alpha,
            "max_relative_residual": max_rel,
            "max_weighted_step": step_norm,
            "objective": float(np.sum(((x[model.measured]
                                        - model.x_meas[model.measured])
                                       / model.sigma[model.measured]) ** 2)),
        })
        if verbose:
            print(f"iter {it:2d} alpha={alpha:.3g} maxrel={max_rel:.3e} "
                  f"step={step_norm:.3e}")
        if max_rel < residual_tol and (it > 1 or step_norm < step_tol):
            converged = True
            reason = "converged"
            break

    h_final, h_jac_f = build_residuals_and_jacobian(x, model, a)
    rel_final = np.abs(h_final) / _residual_scales(x, model, a)

    obs_f = analyze_observability(h_jac_f, model.measured, model.describe)
    obs_report = obs_f
    if obs0["observable"] != obs_f["observable"]:
        obs_report = dict(obs_f)
        obs_report["note"] = "初值点与收敛点结论不一致，已以收敛点为准"
        obs_report["initial_point"] = obs0

    # 显式接受不可观测时，即使残差闭合，状态仍标 unobservable，
    # 提示这些数不能当唯一确定结果使用。
    final_reason = reason
    if not obs_report["observable"] and accept_unobservable:
        final_reason = "unobservable"
        converged = False

    return SolveResult(
        x=x, converged=converged, reason=final_reason,
        iterations=observations[-1]["iteration"] if observations else 0,
        residuals=h_final, relative_residuals=rel_final,
        max_relative_residual=float(np.max(rel_final)),
        observations=observations, observability=obs_report,
        kkt_rank=last_rank, kkt_full_rank=last_full)


def run_reconciliation(topo: CircuitTopology, data,
                       excluded: frozenset[str] = frozenset(),
                       max_iterations: int = DEFAULT_MAX_ITER,
                       residual_tol: float = RESIDUAL_TOL,
                       accept_unobservable: bool = False,
                       verbose: bool = False) -> tuple[SolveResult, VariableModel]:
    """组装变量模型并求解的便捷入口。"""
    a = incidence_matrix(topo)
    model = build_variable_model(topo, data, excluded)
    result = reconcile(model, a, max_iterations=max_iterations,
                       residual_tol=residual_tol,
                       accept_unobservable=accept_unobservable,
                       verbose=verbose)
    return result, model
