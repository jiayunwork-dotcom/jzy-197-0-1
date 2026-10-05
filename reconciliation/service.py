"""应用服务层：把拓扑/数据/设定组合成一次校正，以及版本对比、回收率走势。

纯函数 + 显式数据参数，不接触数据库，方便与存储层解耦测试。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from .balance import build_balance_table, recovery_series
from .errors import ValidationError
from .grosserror import gross_error_report
from .measurements import build_dataset
from .observability import check_redundancy
from .solver import SolveSettings, reconcile
from .topology import Topology, build_topology


# ---------------------------------------------------------------------------
# 规范化与设定指纹
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def settings_fingerprint(settings: SolveSettings) -> str:
    """校正设定指纹：剔除集合、默认 RSD、迭代参数等全部参与。"""
    payload = {
        "excluded": sorted(settings.excluded_measurement_ids),
        "default_rsd": settings.default_rsd or {},
        "max_iter": settings.max_iter,
        "tol_residual": settings.tol_residual,
        "tol_step": settings.tol_step,
        "accept_unobservable": settings.accept_unobservable,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def input_fingerprint(topo_raw: dict[str, Any], dataset_raw: dict[str, Any]) -> str:
    """输入数据指纹（用于审计与复用判断的第二道保险）。"""
    payload = {"topology": _canonical_json(topo_raw), "dataset": _canonical_json(dataset_raw)}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 核心用例
# ---------------------------------------------------------------------------

def run_reconciliation(
    topology_raw: dict[str, Any],
    dataset_raw: dict[str, Any],
    settings_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """端到端执行一次校正：构建拓扑 -> 校验数据 -> 求解 -> 检验 -> 平衡表。"""
    settings = SolveSettings.from_dict(settings_raw)
    topo = build_topology(topology_raw)
    dataset = build_dataset(
        dataset_raw, topo.elements, topo.stream_ids, settings.default_rsd
    )

    result = reconcile(topo, dataset, settings)
    result["balance_table"] = build_balance_table(topo, result)
    result["gross_errors"] = gross_error_report(result, topo)
    result["recovery"] = recovery_series(topo, result)
    result["settings_fingerprint"] = settings_fingerprint(settings)
    result["redundancy"] = check_redundancy(
        np.asarray(result["_internal"]["J"]),
        np.asarray(result["_internal"]["measured"], dtype=bool),
    )
    return result


def compare_results(
    topology_raw: dict[str, Any],
    result_a: dict[str, Any],
    result_b: dict[str, Any],
    label_a: str = "a",
    label_b: str = "b",
) -> dict[str, Any]:
    """对比同一考查两次校正（通常是两个数据版本）的差异。

    按物流列出流量、各元素品位、回收率的绝对差与相对差。
    """
    topo = build_topology(topology_raw)
    ta = build_balance_table(topo, result_a)
    tb = build_balance_table(topo, result_b)
    a_map = {s["stream_id"]: s for s in ta["streams"]}
    b_map = {s["stream_id"]: s for s in tb["streams"]}

    streams = []
    for sid in topo.stream_ids:
        a, b = a_map[sid], b_map[sid]
        def rel(new: float, old: float) -> float | None:
            if old is None or abs(old) < 1e-12:
                return None
            return 100.0 * (new - old) / abs(old)

        grades = {}
        recovery = {}
        for e in topo.elements:
            grades[e] = {
                "a": a["grades"][e],
                "b": b["grades"][e],
                "delta": b["grades"][e] - a["grades"][e],
                "relative_delta_percent": rel(b["grades"][e], a["grades"][e]),
            }
            ra = a["recovery"].get(e)
            rb = b["recovery"].get(e)
            recovery[e] = {
                "a": ra,
                "b": rb,
                "delta_points": (rb - ra) if (ra is not None and rb is not None) else None,
            }
        streams.append(
            {
                "stream_id": sid,
                "name": a["name"],
                "flow": {
                    "a": a["flow"],
                    "b": b["flow"],
                    "delta": b["flow"] - a["flow"],
                    "relative_delta_percent": rel(b["flow"], a["flow"]),
                },
                "grades": grades,
                "recovery": recovery,
            }
        )

    summary = {
        "max_abs_flow_delta": max((abs(s["flow"]["delta"]) for s in streams), default=0.0),
        "max_abs_grade_delta": max(
            (abs(s["grades"][e]["delta"]) for s in streams for e in topo.elements),
            default=0.0,
        ),
    }
    return {
        "label_a": label_a,
        "label_b": label_b,
        "converged_a": result_a["converged"],
        "converged_b": result_b["converged"],
        "objective_a": result_a["objective"],
        "objective_b": result_b["objective"],
        "streams": streams,
        "summary": summary,
    }


def recovery_trend(
    points: list[dict[str, Any]],
    product_stream_id: str | None = None,
) -> dict[str, Any]:
    """把多次考查（可来自不同拓扑版本）的回收率聚合成走势。

    :param points: ``[{"campaign_id", "period", "topology_raw", "result"}, ...]``，
        period 为月份标签（如 ``2026-08``）；按 period 升序输出。
    :param product_stream_id: 指定产品物流；不给则把所有边界产品回收率
        相加（选矿总回收率）。不同拓扑里同语义物流应由调用方保证 id 一致，
        否则只能用产品合计口径。
    """
    rows = []
    for p in sorted(points, key=lambda x: x.get("period", "")):
        topo = build_topology(p["topology_raw"])
        rec = recovery_series(topo, p["result"])
        for elem, prods in rec.items():
            if product_stream_id is not None:
                value = prods.get(product_stream_id)
            else:
                vals = [v for v in prods.values() if v is not None]
                value = sum(vals) if vals else None
            rows.append(
                {
                    "campaign_id": p["campaign_id"],
                    "period": p["period"],
                    "element": elem,
                    "product_stream_id": product_stream_id or "__all_products__",
                    "recovery_percent": value,
                }
            )

    # 透视成 {element: [按 period 排序]}
    series: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: (r["element"], r["period"])):
        series.setdefault(row["element"], []).append(row)
    return {"product_stream_id": product_stream_id, "series": series}
