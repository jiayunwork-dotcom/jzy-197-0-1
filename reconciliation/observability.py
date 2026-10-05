"""结构可观测性分析。

定义
====
在解点（或初值）处把全部守恒式对状态变量的 Jacobian 记为 ``J``，
按列拆成已测变量列 ``J_M`` 与未测变量列 ``J_U``。线性化后的约束为

    J_M dx_M + J_U dx_U = -f .

未测变量能被守恒关系**唯一确定**，当且仅当 ``J_U`` 列满秩：
``J_U v = 0`` 只有零解。若存在非零的零空间向量 ``v``，则沿该方向
扰动未测变量不改变任何守恒式，数据无法区分，对应变量不可观测。

对双线性问题，``J_U`` 的列依赖于当前流量/品位，但其列秩在物理可行
开集（正流量、正常品位）内恒定，因此在迭代初值处评估即可。

逐变量判定
==========
变量 ``i`` 不可观测，当且仅当零空间中存在一个在 ``i`` 分量上非零的
向量。一次 SVD 拿到整个零空间基 ``V[:, k:]`` 后，逐行检查是否恒为 0：
某行在整个基上都为 0，说明该变量被约束固定；否则它属于某个不可观测
的自由度。同时返回零空间基与变量分组，便于说明“谁和谁一起不可确定”。
"""

from __future__ import annotations

from typing import Any

import numpy as np

RANK_TOL = 1e-10


def _svd_nullspace(Ju: np.ndarray) -> tuple[np.ndarray, int, np.ndarray]:
    """返回 (零空间基 V0, 秩, 奇异值)；V0 的列张成 null(Ju)。"""
    if Ju.shape[1] == 0:
        return np.zeros((Ju.shape[0], 0)), Ju.shape[1], np.array([])
    if Ju.shape[0] == 0:
        return np.eye(Ju.shape[1]), 0, np.array([])
    U, s, Vt = np.linalg.svd(Ju, full_matrices=True)
    tol = RANK_TOL * (s[0] if len(s) else 1.0)
    rank = int(np.sum(s > tol))
    V0 = Vt[rank:, :].T  # (n_U, nullity)
    return V0, rank, s


def analyze_observability(
    J: np.ndarray,
    measured: np.ndarray,
    topo: Any,
    layout: dict[str, Any],
) -> dict[str, Any]:
    """返回可观测性报告。"""
    S, E = layout["S"], layout["E"]
    gidx = layout["gidx"]

    u_idx = np.where(~measured)[0]
    Ju = J[:, u_idx]
    V0, rank, sv = _svd_nullspace(Ju)
    nullity = int(u_idx.size - rank)

    def slot_label(k: int) -> dict[str, str]:
        if k < S:
            return {
                "kind": "flow",
                "stream_id": topo.stream_ids[k],
                "element": None,
                "label": f"流量[{topo.stream_ids[k]}]",
            }
        ee = (k - S) // S
        jj = (k - S) % S
        return {
            "kind": "grade",
            "stream_id": topo.stream_ids[jj],
            "element": topo.elements[ee],
            "label": f"品位[{topo.stream_ids[jj]}/{topo.elements[ee]}]",
        }

    unobservable: list[dict[str, Any]] = []
    if nullity > 0:
        # 每行是否在零空间基上恒为 0
        row_norm = np.linalg.norm(V0, axis=1) if V0.size else np.zeros(0)
        active = row_norm > RANK_TOL * (
            np.max(np.abs(V0)) if V0.size else 1.0
        )
        for local, k in enumerate(u_idx):
            if active[local]:
                info = slot_label(int(k))
                info["nullspace_row_norm"] = float(row_norm[local])
                unobservable.append(info)

        # 用零空间基把不可观测变量分成独立的自由度组（基向量支撑集）
        groups: list[list[str]] = []
        V0t = V0.T  # (nullity, n_U)
        for c in range(nullity):
            support = [
                slot_label(int(u_idx[local]))["label"]
                for local in range(u_idx.size)
                if abs(V0t[c, local]) > RANK_TOL
            ]
            groups.append(support)
    else:
        groups = []

    unmeasured_labels = [slot_label(int(k))["label"] for k in u_idx]

    return {
        "observable": nullity == 0,
        "n_unmeasured": int(u_idx.size),
        "rank_ju": int(rank),
        "nullity": nullity,
        "unmeasured": unmeasured_labels,
        "unobservable": unobservable,
        "ambiguity_groups": groups,
        "singular_values": [float(v) for v in sv],
    }


def check_redundancy(J: np.ndarray, measured: np.ndarray) -> dict[str, int]:
    """统计测量方程的冗余度（校正/检验的自由度来源）。

    经典数据校正理论：在未测变量全部可观测（J_U 列满秩）时，

        dof = rank(J) - rank(J_U) = rank(J) - n_unmeasured，

    即约束流形中真正施加在测量值上的独立限制数。
    """
    u_idx = np.where(~measured)[0]
    m_idx = np.where(measured)[0]
    _, rJ, _ = _svd_nullspace(J)
    if u_idx.size:
        _, rU, _ = _svd_nullspace(J[:, u_idx])
    else:
        rU = 0
    dof = int(rJ - rU)
    return {
        "rank_J": int(rJ),
        "rank_Ju": int(rU),
        "n_measured": int(m_idx.size),
        "n_unmeasured": int(u_idx.size),
        "dof": max(dof, 0),
    }
