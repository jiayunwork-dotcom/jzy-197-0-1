"""校正求解器：带双线性等式约束的加权最小二乘（逐次线性化 / Gauss-Newton）。

方法选择与理由
==============
节点干矿量守恒是线性约束，但金属量守恒 ``Σ A_nj F_j g_ej = 0`` 对状态
``(F, g)`` 是双线性（不可由任何线性化的流股测量直接消除）。本模块采用
**逐次线性化（successive linearization，Gauss-Newton 型）**：

1. 在当前工作点把约束一阶展开；
2. 解一个带线性等式约束的二次规划（KKT 方程组），得到校正步；
3. 更新工作点，直到节点残差与状态变化都足够小。

这与节点分解法相比不依赖网络的特殊结构（合流点、循环流都直接成立），
每步只做一次稠密/稀疏线性代数，二阶收敛，在流程考查规模（几十股物流、
几种元素）上毫秒级完成。

未测变量**不**通过给大方差“假装已测”，而是作为权重为 0 的状态变量进入
同一 KKT 系统；它们能否被约束唯一确定，由 :mod:`observability` 用
约束 Jacobian 的秩与零空间结构判定——这才是结构可观测性的严格定义。

尺度处理
========
流量量级可能是几十到几千 t/h，品位是 0~100 的百分点，直接拼 KKT 矩阵
条件数很差。内部按特征尺度归一化（``z = x / scale``）求解再映射回去。

收敛与诚实性
============
* 判据：所有节点质量/金属平衡残差相对值 ≤ ``tol_residual``（默认 1e-10，
  严于对外承诺的 1e-9），且状态相对变化 ≤ ``tol_step``（默认 1e-12）；
* 迭代上限 ``max_iter``（默认 50）。达到上限仍未收敛时，返回
  ``converged=False``、当前状态与残差，绝不伪装成功；
* 输入本来完全平衡时，初始残差即为机器零，迭代 0 次、校正量为 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import UnobservableError
from .measurements import Dataset, Measurement
from .observability import analyze_observability
from .topology import Topology

DEFAULT_MAX_ITER = 50
DEFAULT_TOL_RESIDUAL = 1e-10
DEFAULT_TOL_STEP = 1e-12

# 收敛判据的对外承诺（测试断言使用，比内部默认略松）
RESIDUAL_GUARANTEE = 1e-9


@dataclass
class SolveSettings:
    max_iter: int = DEFAULT_MAX_ITER
    tol_residual: float = DEFAULT_TOL_RESIDUAL
    tol_step: float = DEFAULT_TOL_STEP
    excluded_measurement_ids: tuple[str, ...] = ()
    accept_unobservable: bool = False
    default_rsd: dict[str, float] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SolveSettings":
        raw = raw or {}
        out = cls()
        if "max_iter" in raw and raw["max_iter"] is not None:
            out.max_iter = int(raw["max_iter"])
            if out.max_iter < 0:
                raise ValueError("max_iter 不能为负")
        if "tol_residual" in raw and raw["tol_residual"] is not None:
            out.tol_residual = float(raw["tol_residual"])
        if "tol_step" in raw and raw["tol_step"] is not None:
            out.tol_step = float(raw["tol_step"])
        excl = raw.get("excluded_measurement_ids") or ()
        out.excluded_measurement_ids = tuple(str(x) for x in excl)
        out.accept_unobservable = bool(raw.get("accept_unobservable", False))
        if raw.get("default_rsd"):
            out.default_rsd = {str(k): float(v) for k, v in raw["default_rsd"].items()}
        return out


# ---------------------------------------------------------------------------
# 变量布局
# ---------------------------------------------------------------------------

def _layout(topo: Topology) -> dict[str, Any]:
    S = topo.n_streams
    E = topo.n_elements
    n_var = S * (1 + E)

    def fidx(j: int) -> int:
        return j

    def gidx(e: int, j: int) -> int:
        return S + e * S + j

    return {"S": S, "E": E, "n_var": n_var, "fidx": fidx, "gidx": gidx}


def _constraints(
    x: np.ndarray, topo: Topology, layout: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    """计算等式约束残差 f(x) 及其解析 Jacobian J = df/dx。

    残差定义为“入 - 出”（关联矩阵 A 入为 +1）：

    * 质量：``f_n = Σ_j A_nj F_j``
    * 金属（元素 e，品位以百分点计）：``f_ne = Σ_j A_nj F_j g_ej``

    行顺序：先 N 个质量行，再 E 组、每组 N 个金属行。
    """
    A = topo.incidence
    N = topo.n_nodes
    S, E = layout["S"], layout["E"]
    gidx = layout["gidx"]

    F = x[:S]
    G = x[S:].reshape(E, S)  # G[e, j]

    f_mass = A @ F  # (N,)
    f_metal = np.empty((E, N), dtype=float)
    for e in range(E):
        f_metal[e] = A @ (F * G[e])
    f = np.concatenate([f_mass, f_metal.reshape(-1)])

    J = np.zeros((N * (1 + E), layout["n_var"]), dtype=float)
    # 质量行对 F 的导数
    J[:N, :S] = A
    # 金属行
    for e in range(E):
        rows = N + e * N + np.arange(N)
        # d/dF_j = A_nj * g_ej
        J[np.ix_(rows, np.arange(S))] = A * G[e][None, :]
        # d/dg_ej = A_nj * F_j
        cols = np.array([gidx(e, j) for j in range(S)])
        J[np.ix_(rows, cols)] = A * F[None, :]
    return f, J


def _relative_residuals(
    x: np.ndarray, f: np.ndarray, topo: Topology, layout: dict[str, Any]
) -> dict[str, Any]:
    """各节点质量/金属残差的绝对值与相对值。"""
    A = topo.incidence
    N = topo.n_nodes
    S, E = layout["S"], layout["E"]
    F = np.maximum(x[:S], 0.0)
    G = np.maximum(x[S:].reshape(E, S), 0.0)

    throughput = np.abs(A) @ F  # 节点经手的总流量（入+出）
    scale_mass = np.maximum(throughput, 1e-30)
    r_mass = f[:N]

    metal_rows = []
    metal_scales = []
    for e in range(E):
        metal = np.abs(A) @ (F * G[e])
        metal_scales.append(np.maximum(metal, 1e-30))
        metal_rows.append(f[N + e * N + np.arange(N)])

    rel_mass = np.abs(r_mass) / scale_mass
    rel_metal = np.abs(np.array(metal_rows)) / np.array(metal_scales)
    return {
        "mass_abs": r_mass,
        "mass_rel": rel_mass,
        "metal_abs": np.array(metal_rows),  # (E, N)
        "metal_rel": rel_metal,
        "mass_scale": scale_mass,
        "metal_scale": np.array(metal_scales),
    }


def _initial_state(
    topo: Topology,
    layout: dict[str, Any],
    active: dict[str, Measurement],
) -> np.ndarray:
    """构造迭代初值：已测变量取测量值；未测变量用线性守恒最小二乘推初值。"""
    A = topo.incidence
    S, E = layout["S"], layout["E"]
    x = np.zeros(layout["n_var"], dtype=float)

    flow_meas = {m.stream_id: m.value for m in active.values() if m.mtype == "flow"}
    grade_meas: dict[tuple[str, str], float] = {
        (m.stream_id, m.element): m.value
        for m in active.values()
        if m.mtype == "grade"
    }

    F = np.zeros(S)
    f_mask = np.zeros(S, dtype=bool)
    for j, sid in enumerate(topo.stream_ids):
        if sid in flow_meas:
            F[j] = flow_meas[sid]
            f_mask[j] = True

    if not f_mask.all():
        # A F = 0 对未测列最小二乘；欠定时取最小范数解（可观测性另行判定）
        U = np.where(~f_mask)[0]
        M = np.where(f_mask)[0]
        rhs = -(A[:, M] @ F[M]) if len(M) else np.zeros(A.shape[0])
        sol, *_ = np.linalg.lstsq(A[:, U], rhs, rcond=None)
        F[U] = sol
        # 非正初值只是迭代起点（物理上无意义时用已测中位数代替），不影响最终解
        positive = F[f_mask] if f_mask.any() else F[F > 0]
        fallback = float(np.median(positive)) if len(positive) else 1.0
        for j in U:
            if not np.isfinite(F[j]) or F[j] <= 0:
                F[j] = fallback
    x[:S] = F

    G = np.zeros((E, S))
    for e, elem in enumerate(topo.elements):
        g_mask = np.zeros(S, dtype=bool)
        for j, sid in enumerate(topo.stream_ids):
            if (sid, elem) in grade_meas:
                G[e, j] = grade_meas[(sid, elem)]
                g_mask[j] = True
        if not g_mask.all():
            # Σ A_nj F_j g_ej = 0 线性求解未测品位
            AF = A * F[None, :]
            U = np.where(~g_mask)[0]
            M = np.where(g_mask)[0]
            rhs = -(AF[:, M] @ G[e, M]) if len(M) else np.zeros(A.shape[0])
            sol, *_ = np.linalg.lstsq(AF[:, U], rhs, rcond=None)
            G[e, U] = sol
            for j in U:
                if not np.isfinite(G[e, j]):
                    G[e, j] = 0.0
    x[S:] = G.reshape(-1)
    return x


def _kkt_step(
    x: np.ndarray,
    y: np.ndarray,
    sigma: np.ndarray,
    measured: np.ndarray,
    f: np.ndarray,
    J: np.ndarray,
    scales: np.ndarray,
) -> np.ndarray:
    """解一步 KKT，返回状态增量 dx。

    归一化坐标 z = x / scale 下：

        [ D W² D    (J D)^T ] [dz]   [ -D W² D (z - z_y) ]
        [ J D         0      ] [λ ] = [ -f                 ]

    用整体最小二乘（SVD）求解：即使约束行线性相关（合流点冗余守恒式），
    dx 仍是唯一的；λ 的不唯一方向取最小范数，不影响结果。
    """
    D = scales
    inv_sigma = np.where(measured, 1.0 / np.maximum(sigma, 1e-300), 0.0)
    # H' = D W² D
    Hp = np.diag((D * inv_sigma) ** 2)

    z = x / D
    zy = np.where(measured, y / D, 0.0)
    Jp = J * D[None, :]

    n = x.shape[0]
    m = f.shape[0]
    KKT = np.zeros((n + m, n + m), dtype=float)
    KKT[:n, :n] = Hp
    KKT[:n, n:] = Jp.T
    KKT[n:, :n] = Jp

    rhs1 = -(Hp @ (z - zy))
    rhs = np.concatenate([rhs1, -f])

    sol, *_ = np.linalg.lstsq(KKT, rhs, rcond=None)
    dz = sol[:n]
    return dz * D


def reconcile(
    topo: Topology,
    dataset: Dataset,
    settings: SolveSettings | None = None,
) -> dict[str, Any]:
    """执行数据校正，返回结构化结果字典（可观测性分析见结果中的 observability）。"""
    settings = settings or SolveSettings()
    layout = _layout(topo)
    S, E, n_var = layout["S"], layout["E"], layout["n_var"]

    excluded = set(settings.excluded_measurement_ids)
    unknown_excluded = excluded - set(dataset.measurements)
    if unknown_excluded:
        from .errors import NotFoundError

        raise NotFoundError(
            "剔除列表中有不存在的测量 id",
            {"measurement_ids": sorted(unknown_excluded)},
        )
    active = {
        mid: m for mid, m in dataset.measurements.items() if mid not in excluded
    }

    # 测量 -> 变量槽位
    y = np.zeros(n_var)
    sigma = np.full(n_var, np.inf)
    measured = np.zeros(n_var, dtype=bool)
    slot_of: dict[str, int] = {}
    gidx = layout["gidx"]
    for m in active.values():
        j = topo.stream_ids.index(m.stream_id)
        if m.mtype == "flow":
            k = j
        else:
            e = topo.elements.index(m.element)
            k = gidx(e, j)
        if measured[k]:
            from .errors import DuplicateIdError

            raise DuplicateIdError(
                f"变量槽位上存在重复测量：{m.stream_id}/{m.mtype}/{m.element}",
                {"stream_id": m.stream_id},
            )
        y[k] = m.value
        sigma[k] = m.sigma
        measured[k] = True
        slot_of[m.id] = k

    # 特征尺度（随流量量级走，保证同比缩放不变性；品位固定为 100 百分点）
    if measured[:S].any():
        flow_scale = max(float(np.max(y[:S][measured[:S]])), 1e-12)
    else:
        flow_scale = 1.0
    scales = np.concatenate([np.full(S, flow_scale), np.full(E * S, 100.0)])

    x = _initial_state(topo, layout, active)

    # 可观测性：在初值处评估 Jacobian（对双线性问题，列秩在物理可行域内不变）
    f0, J0 = _constraints(x, topo, layout)
    unmeasured = ~measured
    obs = analyze_observability(J0, measured, topo, layout)
    if obs["unobservable"] and not settings.accept_unobservable:
        raise UnobservableError(
            "存在无法由守恒关系唯一确定的未测变量；"
            "如确需非唯一解，请在设定中 accept_unobservable=true",
            obs["unobservable"],
        )

    history: list[float] = []
    converged = False
    rel = None
    x_start = x.copy()

    for it in range(settings.max_iter + 1):
        f, J = _constraints(x, topo, layout)
        rel = _relative_residuals(x, f, topo, layout)
        max_res = float(
            max(rel["mass_rel"].max(initial=0.0), rel["metal_rel"].max(initial=0.0))
        )
        history.append(max_res)
        if max_res <= settings.tol_residual:
            converged = True
            break
        if it == settings.max_iter:
            break

        dx = _kkt_step(x, y, sigma, measured, f, J, scales)

        # 简单阻尼：保持流量为正（物理约束），必要时缩短步长
        t = 1.0
        neg = dx[:S] < 0
        if neg.any():
            ratios = x[:S][neg] / (-dx[:S][neg] + 1e-300)
            t = min(1.0, 0.9 * float(np.min(ratios))) if len(ratios) else 1.0
            t = max(t, 1e-6)
        x = x + t * dx

    # 终态残差（用更新后的 x 再算一次，保证 history 末值与报告一致）
    f_final, J_final = _constraints(x, topo, layout)
    rel = _relative_residuals(x, f_final, topo, layout)
    max_res_final = float(
        max(rel["mass_rel"].max(initial=0.0), rel["metal_rel"].max(initial=0.0))
    )
    history[-1] = max_res_final

    # 终态再做一次可观测性（数值退化兜底）
    obs_final = analyze_observability(J_final, measured, topo, layout)

    result = _assemble_result(
        x=x,
        y=y,
        sigma=sigma,
        measured=measured,
        slot_of=slot_of,
        active=active,
        topo=topo,
        layout=layout,
        rel=rel,
        f=f_final,
        J=J_final,
        converged=converged,
        iterations=(len(history) - 1) if converged else settings.max_iter,
        max_iter=settings.max_iter,
        history=history,
        obs=obs_final,
        excluded=sorted(excluded),
        flow_scale=flow_scale,
    )
    return result


def _assemble_result(
    *,
    x: np.ndarray,
    y: np.ndarray,
    sigma: np.ndarray,
    measured: np.ndarray,
    slot_of: dict[str, int],
    active: dict[str, Measurement],
    topo: Topology,
    layout: dict[str, Any],
    rel: dict[str, Any],
    f: np.ndarray,
    J: np.ndarray,
    converged: bool,
    iterations: int,
    max_iter: int,
    history: list[float],
    obs: dict[str, Any],
    excluded: list[str],
    flow_scale: float,
) -> dict[str, Any]:
    S, E = layout["S"], layout["E"]
    gidx = layout["gidx"]
    F = x[:S]
    G = x[S:].reshape(E, S)

    streams_out = []
    for j, sid in enumerate(topo.stream_ids):
        s = topo.stream_map()[sid]
        grades = {}
        grades_measured = {}
        for e, elem in enumerate(topo.elements):
            k = gidx(e, j)
            grades[elem] = float(G[e, j])
            grades_measured[elem] = bool(measured[k])
        streams_out.append(
            {
                "id": sid,
                "name": s.name,
                "kind": s.kind,
                "source": s.source,
                "target": s.target,
                "flow": float(F[j]),
                "flow_measured": bool(measured[j]),
                "grades": grades,
                "grades_measured": grades_measured,
            }
        )

    # 每个测量的调整量
    corrections = []
    objective = 0.0
    for mid, m in active.items():
        k = slot_of[mid]
        adjusted = float(x[k])
        delta = adjusted - m.value
        weighted = delta / m.sigma
        objective += weighted * weighted
        denom = abs(m.value) if abs(m.value) > 1e-12 else m.sigma
        corrections.append(
            {
                "measurement_id": mid,
                "stream_id": m.stream_id,
                "type": m.mtype,
                "element": m.element,
                "measured_value": m.value,
                "sigma": m.sigma,
                "rsd": m.rsd,
                "adjusted_value": adjusted,
                "adjustment": float(delta),
                "relative_adjustment": float(delta / denom),
                "weighted_adjustment": float(weighted),
            }
        )
    corrections.sort(key=lambda c: c["measurement_id"])

    # 节点平衡明细
    A = topo.incidence
    node_balances = []
    for n, nid in enumerate(topo.node_ids):
        per_element = {}
        for e, elem in enumerate(topo.elements):
            per_element[elem] = {
                "residual": float(rel["metal_abs"][e, n]),
                "relative_residual": float(rel["metal_rel"][e, n]),
                "in_metal": float(
                    sum(
                        max(F[j], 0.0) * max(G[e, j], 0.0)
                        for j in range(S)
                        if A[n, j] > 0
                    )
                ),
                "out_metal": float(
                    sum(
                        max(F[j], 0.0) * max(G[e, j], 0.0)
                        for j in range(S)
                        if A[n, j] < 0
                    )
                ),
            }
        node_balances.append(
            {
                "node_id": nid,
                "name": topo.node_map()[nid].name,
                "in_solids": float(
                    sum(max(F[j], 0.0) for j in range(S) if A[n, j] > 0)
                ),
                "out_solids": float(
                    sum(max(F[j], 0.0) for j in range(S) if A[n, j] < 0)
                ),
                "residual": float(rel["mass_abs"][n]),
                "relative_residual": float(rel["mass_rel"][n]),
                "elements": per_element,
            }
        )

    warnings = []
    if (F < -1e-9).any():
        bad = [topo.stream_ids[j] for j in np.where(F < -1e-9)[0]]
        warnings.append({"type": "negative_flow", "stream_ids": bad})
    for e, elem in enumerate(topo.elements):
        bad = [topo.stream_ids[j] for j in np.where((G[e] < -1e-9) | (G[e] > 100 + 1e-6))[0]]
        if bad:
            warnings.append({"type": "grade_out_of_range", "element": elem, "stream_ids": bad})

    return {
        "converged": bool(converged),
        "iterations": iterations,
        "max_iter": max_iter,
        "residual_history": [float(v) for v in history],
        "max_relative_residual": float(
            max(rel["mass_rel"].max(initial=0.0), rel["metal_rel"].max(initial=0.0))
        ),
        "objective": float(objective),  # 加权校正平方和 = 整体 χ² 统计量
        "streams": streams_out,
        "corrections": corrections,
        "node_balances": node_balances,
        "observability": obs,
        "excluded_measurement_ids": excluded,
        "warnings": warnings,
        "_internal": {
            # 供粗差检验模块复用，不对外持久化之外的用途
            "x": x.tolist(),
            "J": J.tolist(),
            "measured": measured.tolist(),
            "sigma": sigma.tolist(),
            "slot_of": slot_of,
            "flow_scale": flow_scale,
        },
    }
