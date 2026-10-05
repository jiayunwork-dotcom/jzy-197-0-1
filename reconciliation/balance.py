"""金属平衡表与回收率。

平衡表
======
按节点列出干矿量入/出、每种元素的金属量入/出及相对残差（金属量单位为
``t/h × 百分点``，报表展示时除以 100 即为 t/h 金属）。

回收率
======
对每个系统边界产品物流（终点为 ``@env``），元素 e 的作业回收率为

    recovery = 100 × 该股校正金属量 / Σ 全部给矿边界物流的校正金属量。

给矿物流自身不计算回收率。回路没有边界给矿（纯内部循环）时回收率为 None。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .topology import ENV_NODE


def build_balance_table(topo: Any, result: dict[str, Any]) -> dict[str, Any]:
    """生成完整金属平衡表（节点部分结果里已有，这里补流股与汇总视图）。"""
    smap = topo.stream_map()
    element_rows: dict[str, dict[str, Any]] = {}
    total_feed_metal = {e: 0.0 for e in topo.elements}
    total_product_metal = {e: 0.0 for e in topo.elements}

    streams_view = []
    for s in result["streams"]:
        row = {
            "stream_id": s["id"],
            "name": s["name"],
            "kind": s["kind"],
            "source": s["source"],
            "target": s["target"],
            "flow": s["flow"],
            "flow_measured": s["flow_measured"],
            "grades": s["grades"],
            "grades_measured": s["grades_measured"],
            "metals": {e: s["flow"] * g / 100.0 for e, g in s["grades"].items()},
            "is_feed_boundary": s["source"] == ENV_NODE,
            "is_product_boundary": s["target"] == ENV_NODE,
            "recovery": {},
        }
        streams_view.append(row)

    for row in streams_view:
        for e in topo.elements:
            if row["is_feed_boundary"]:
                total_feed_metal[e] += row["metals"][e]
            if row["is_product_boundary"]:
                total_product_metal[e] += row["metals"][e]

    for row in streams_view:
        for e in topo.elements:
            if row["is_product_boundary"] and total_feed_metal[e] > 1e-30:
                row["recovery"][e] = 100.0 * row["metals"][e] / total_feed_metal[e]
            else:
                row["recovery"][e] = None

    # 元素汇总（给矿金属 vs 全部产品金属，含相对闭合差）
    for e in topo.elements:
        feed = total_feed_metal[e]
        product = total_product_metal[e]
        closure = (
            100.0 * (product - feed) / feed if feed > 1e-30 else None
        )
        element_rows[e] = {
            "element": e,
            "feed_metal": feed,
            "product_metal": product,
            "closure_error_percent": closure,
            "products": [
                {
                    "stream_id": row["stream_id"],
                    "flow": row["flow"],
                    "grade_percent": row["grades"][e],
                    "metal": row["metals"][e],
                    "recovery_percent": row["recovery"][e],
                }
                for row in streams_view
                if row["is_product_boundary"]
            ],
            "feeds": [
                {
                    "stream_id": row["stream_id"],
                    "flow": row["flow"],
                    "grade_percent": row["grades"][e],
                    "metal": row["metals"][e],
                }
                for row in streams_view
                if row["is_feed_boundary"]
            ],
        }

    return {
        "converged": result["converged"],
        "max_relative_residual": result["max_relative_residual"],
        "streams": streams_view,
        "nodes": result["node_balances"],
        "elements": element_rows,
    }


def recovery_series(
    topo: Any, result: dict[str, Any]
) -> dict[str, dict[str, float | None]]:
    """{element: {product_stream_id: recovery_percent}}，便于走势与对比复用。"""
    table = build_balance_table(topo, result)
    out: dict[str, dict[str, float | None]] = {}
    for e, block in table["elements"].items():
        out[e] = {p["stream_id"]: p["recovery_percent"] for p in block["products"]}
    return out
