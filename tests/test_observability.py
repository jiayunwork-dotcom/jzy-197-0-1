"""可观测性判断测试。"""

from __future__ import annotations

import pytest

from reconciliation.errors import UnobservableError
from reconciliation.service import run_reconciliation

from .conftest import handcalc_dataset, one_in_two_out_topology


def test_unmeasured_flows_are_observable_in_handcalc(topo, dataset):
    """手算例：两股未测流量在品位互异时可由守恒唯一确定。"""
    res = run_reconciliation(topo, dataset)
    obs = res["observability"]
    assert obs["observable"] is True
    assert obs["nullity"] == 0
    # 未测流量确实被求出来了，不是 None/0
    flows = {s["id"]: s for s in res["streams"]}
    assert flows["conc"]["flow"] > 1
    assert flows["tail"]["flow"] > 1
    assert flows["conc"]["flow_measured"] is False
    assert flows["tail"]["flow_measured"] is False


def test_degenerate_equal_grades_makes_flows_unobservable(topo):
    """三股品位相同：金属平衡与质量平衡线性重合，两股产品流量无法分开。"""
    data = {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 1.0},
        ]
    }
    with pytest.raises(UnobservableError) as exc:
        run_reconciliation(topo, data)
    kinds = {(u["kind"], u["stream_id"]) for u in exc.value.details["unobservable"]}
    assert ("flow", "conc") in kinds
    assert ("flow", "tail") in kinds


def test_accept_unobservable_returns_nonunique_but_flagged(topo):
    data = {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 1.0},
        ]
    }
    res = run_reconciliation(topo, data, {"accept_unobservable": True})
    assert res["observability"]["observable"] is False
    assert res["observability"]["nullity"] == 1
    assert res["converged"]  # 能算出一个（最小范数）解，但已明确标注不可观测


def test_two_unmeasured_in_series_with_no_redundancy():
    """串联两台设备、中间物流未测，且没有别的信息：中间流量可观测（质量守恒传递）。"""
    topo = {
        "nodes": [
            {"id": "rougher"},
            {"id": "cleaner"},
        ],
        "streams": [
            {"id": "feed", "source": "@env", "target": "rougher", "kind": "feed"},
            {"id": "mid", "source": "rougher", "target": "cleaner", "kind": "intermediate"},
            {"id": "tails", "source": "rougher", "target": "@env", "kind": "tailings"},
            {"id": "conc", "source": "cleaner", "target": "@env", "kind": "concentrate"},
            {"id": "midd", "source": "cleaner", "target": "@env", "kind": "tailings"},
        ],
        "elements": ["Cu"],
    }
    # 给矿、尾矿与精选产品都有流量，只有中间流未测；品位全测。
    # 流量按质量守恒自洽：100=60+40，40=10+30；金属平衡仅用于辅助求解
    data = {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "tails", "type": "flow", "value": 60.0},
            {"stream_id": "conc", "type": "flow", "value": 10.0},
            {"stream_id": "midd", "type": "flow", "value": 30.0},
            *[
                {"stream_id": s, "type": "grade", "element": "Cu", "value": g}
                for s, g in [
                    ("feed", 1.0), ("mid", 1.5), ("tails", 2.0 / 3.0),
                    ("conc", 2.5), ("midd", 3.5 / 3.0),
                ]
            ],
        ]
    }
    res = run_reconciliation(topo, data)
    assert res["observability"]["observable"] is True
    flows = {s["id"]: s["flow"] for s in res["streams"]}
    assert flows["mid"] == pytest.approx(40.0, abs=1e-6)
