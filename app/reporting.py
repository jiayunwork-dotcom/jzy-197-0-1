"""把求解结果装配成对外的结果 JSON、金属平衡表、回收率。

内部单位：流量 t/h，品位用小数；对外：流量 t/h，品位百分数。
"""
from __future__ import annotations

import numpy as np

from .measurement_model import VariableModel
from .reconciliation import SolveResult
from .topology import CircuitTopology


def _stream_dir(topo: CircuitTopology, code: str) -> int:
    """物流相对系统边界的方向：进系统 +1（source 端为边界），出系统 -1。"""
    s = topo.stream(code)
    if s.is_boundary_input:
        return 1
    if s.is_boundary_output:
        return -1
    return 0


def stream_results(topo: CircuitTopology, model: VariableModel,
                   x: np.ndarray) -> list[dict]:
    """每股物流的校正结果。"""
    out = []
    for i, code in enumerate(model.stream_codes):
        s = topo.stream(code)
        row = {
            "stream": code,
            "name": s.name,
            "role": s.role,
            "boundary_direction": _stream_dir(topo, code),
            "dry_ore_flow": float(x[i]),
            "flow_was_measured": bool(model.measured[i]),
            "grades_percent": {},
            "metal_flows": {},
        }
        for e in model.elements:
            j = model.grade_index[(e, code)]
            g = float(x[j])
            row["grades_percent"][e] = g * 100.0
            row["metal_flows"][e] = float(x[i] * g)
        out.append(row)
    return out


def measurement_results(model: VariableModel, x: np.ndarray) -> list[dict]:
    """每个测量的测量值/标准差/校正值/调整量（品位用百分数）。"""
    out = []
    for i, mid in enumerate(model.measurement_ids):
        if mid is None:
            continue
        desc = model.describe(i)
        scale = 100.0 if desc["kind"] == "grade" else 1.0
        out.append({
            "measurement_id": mid,
            "kind": desc["kind"],
            "stream": desc["stream"],
            "element": desc.get("element"),
            "measured_value": float(model.x_meas[i] * scale),
            "sigma": float(model.sigma[i] * scale),
            "reconciled_value": float(x[i] * scale),
            "adjustment": float((x[i] - model.x_meas[i]) * scale),
            "relative_adjustment": float(
                (x[i] - model.x_meas[i]) / model.x_meas[i]
                if model.x_meas[i] != 0 else 0.0),
        })
    return out


def node_balance_rows(topo: CircuitTopology, model: VariableModel,
                      a: np.ndarray, x: np.ndarray,
                      res: SolveResult) -> list[dict]:
    """逐节点平衡表：干矿与各元素的进/出/残差/相对残差。"""
    node_codes = sorted(topo.node_codes())
    rows = []
    for k, node in enumerate(node_codes):
        attached = topo.streams_at(node)
        inflows = [s for s, d in attached if d < 0]
        outflows = [s for s, d in attached if d > 0]
        def fsum(streams, sign):
            return sum(float(x[model.flow_index[s.code]]) * sign for s in streams)
        in_f = fsum(inflows, 1.0)
        out_f = fsum(outflows, 1.0)
        row = {
            "node": node,
            "dry_ore": {
                "in": in_f, "out": out_f,
                "residual": float(res.residuals[k]),
                "relative_residual": float(res.relative_residuals[k]),
                "in_streams": [s.code for s in inflows],
                "out_streams": [s.code for s in outflows],
            },
            "metals": {},
        }
        for ei, e in enumerate(model.elements):
            def metal(streams):
                return sum(float(x[model.flow_index[s.code]]
                                 * x[model.grade_index[(e, s.code)]])
                           for s in streams)
            in_m = metal(inflows)
            out_m = metal(outflows)
            resid = in_m - out_m
            row["metals"][e] = {
                "in": in_m, "out": out_m, "residual": resid,
                "relative_residual": resid / max(in_m, out_m, 1e-12),
            }
        rows.append(row)
    return rows


def recoveries(topo: CircuitTopology, model: VariableModel,
               x: np.ndarray) -> dict:
    """各元素的系统总给矿、精矿/产品金属量、回收率（%）。

    产品物流取 role=concentrate 的边界出方；未标注时退化为全部边界出方，
    并在返回里标明口径。
    """
    boundary_in = [s for s in topo.streams if s.is_boundary_input]
    boundary_out = [s for s in topo.streams if s.is_boundary_output]
    conc = [s for s in boundary_out if s.role == "concentrate"]
    product_streams = conc if conc else boundary_out
    basis = "concentrate_role" if conc else "all_boundary_outputs"

    def metal(streams, e):
        return sum(float(x[model.flow_index[s.code]]
                         * x[model.grade_index[(e, s.code)]])
                   for s in streams)

    out = {"basis": basis,
           "product_streams": [s.code for s in product_streams],
           "elements": {}}
    for e in model.elements:
        feed_m = metal(boundary_in, e)
        prod_m = metal(product_streams, e)
        out["elements"][e] = {
            "feed_metal_flow": feed_m,
            "product_metal_flow": prod_m,
            "recovery_percent": 100.0 * prod_m / feed_m if feed_m else None,
        }
    return out


def build_result_bundle(topo: CircuitTopology, model: VariableModel,
                        a: np.ndarray, res: SolveResult,
                        settings: dict) -> dict:
    """汇总成可持久化的结果包（不含粗差，粗差由调用方另算后并入）。"""
    return {
        "status": res.status,
        "converged": res.converged,
        "iterations": res.iterations,
        "max_iterations": settings.get("max_iterations"),
        "max_relative_residual": res.max_relative_residual,
        "objective_weighted_ssr": float(res.observations[-1]["objective"])
            if res.observations else None,
        "kkt_full_rank": res.kkt_full_rank,
        "iteration_trace": res.observations,
        "observability": res.observability,
        "streams": stream_results(topo, model, res.x),
        "measurements": measurement_results(model, res.x),
        "node_balance": node_balance_rows(topo, model, a, res.x, res),
        "recoveries": recoveries(topo, model, res.x),
        "residuals_summary": {
            "max_relative_residual": res.max_relative_residual,
            "residual_vector": [float(v) for v in res.residuals],
            "relative_residual_vector":
                [float(v) for v in res.relative_residuals],
            "row_layout": {
                "first_n_rows": "mass balance, one row per node (sorted code)",
                "then": "metal balance rows in (element, node) order",
                "node_order": sorted(topo.node_codes()),
                "element_order": list(model.elements),
            },
        },
    }
