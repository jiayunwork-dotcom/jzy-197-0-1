"""可观测性分析：未测变量能否由守恒约束唯一确定。

线性化约束 H·Δx = −h 中，未测变量 x_u 只有当其在约束雅可比中的列 H_U
满列秩时才可被唯一确定（测量变量起“锚”的作用，可观测性等价于零空间
V 的存在性判断）。用 SVD 做秩判定，并从 V 的非零行挑出具体的不可观测变量。

本问题约束对变量是双线性的：干矿行恒定，金属行随当前工作点变化，
因此在初值点和收敛点各判一次，结论应一致；不一致时以收敛点为准并在
报告中注明（这种情形只在退化工作点，如全零流量附近才可能出现）。
"""
from __future__ import annotations

import numpy as np


def analyze_observability(h_jac: np.ndarray, measured_mask: np.ndarray,
                          describe, tol: float | None = None) -> dict:
    """分析未测变量的可观测性。

    参数
    ----
    h_jac : 约束雅可比 H（m×n）
    measured_mask : bool[n]，True 表示该变量被测量
    describe : int -> dict，变量描述（{kind, stream, element?}）

    返回
    ----
    {"observable": bool,
     "unobservable": [变量描述...],
     "n_unmeasured": int, "rank": int,
     "nullspace_directions": [...]}   # 每个不可观测量给一个零空间方向示例
    """
    unmeas_idx = np.where(~measured_mask)[0]
    if unmeas_idx.size == 0:
        return {"observable": True, "unobservable": [], "n_unmeasured": 0,
                "rank": 0, "nullspace_directions": []}

    h_u = np.asarray(h_jac)[:, unmeas_idx]
    scale = np.abs(h_u).max(axis=0)
    scale[scale == 0] = 1.0
    # 列归一化后再 SVD，秩判据不随量纲变化
    u, s, vt = np.linalg.svd(h_u / scale, full_matrices=True)
    if tol is None:
        tol = max(h_u.shape) * np.finfo(float).eps * (s[0] if s.size else 1.0)
        tol = max(tol, 1e-11)
    rank = int(np.sum(s > tol))
    nu = unmeas_idx.size
    if rank == nu:
        return {"observable": True, "unobservable": [], "n_unmeasured": nu,
                "rank": rank, "nullspace_directions": []}

    # V^T 的后 nu-rank 行张成 H_U 的零空间（对归一化坐标）
    null_rows = vt[rank:, :]
    involved = np.any(np.abs(null_rows) > 1e-9, axis=0)
    bad_local = np.where(involved)[0]
    unobservable, directions = [], []
    for li in bad_local:
        gi = int(unmeas_idx[li])
        desc = describe(gi)
        unobservable.append(desc)
        # 取该变量分量最大的一个零空间方向作示例
        k = int(np.argmax(np.abs(null_rows[:, li])))
        v = null_rows[k] / (np.sign(null_rows[k, li]) or 1.0)
        direction = []
        for lj in range(nu):
            if abs(v[lj]) > 1e-9:
                gj = int(unmeas_idx[lj])
                direction.append({"var": describe(gj), "coef": float(v[lj])})
        directions.append({"var": desc, "direction": direction})

    # 只报流量类的不可观测量还是全报？全部返回，由调用方按 kind 取用。
    return {"observable": False, "unobservable": unobservable,
            "n_unmeasured": nu, "rank": rank,
            "nullspace_directions": directions}
