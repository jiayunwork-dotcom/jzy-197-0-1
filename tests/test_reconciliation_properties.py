"""算法性质测试：手算例、闭合精度、零调整、缩放、大方差、编号无关。"""

from __future__ import annotations

import numpy as np
import pytest

from reconciliation.service import run_reconciliation
from reconciliation.solver import RESIDUAL_GUARANTEE

from .conftest import (
    EXPECTED_CONC_FLOW,
    EXPECTED_RECOVERY,
    EXPECTED_TAIL_FLOW,
    fully_measured_balanced_dataset,
    handcalc_dataset,
    one_in_two_out_topology,
)


def _flows(result):
    return {s["id"]: s["flow"] for s in result["streams"]}


def _recovery(result, stream_id="conc"):
    return result["balance_table"]["elements"]["Cu"]["products"]


# --------------------------------------------------------------- 手算算例
def test_hand_calc_one_in_two_out(topo, dataset):
    res = run_reconciliation(topo, dataset)
    assert res["converged"]
    flows = _flows(res)
    assert flows["conc"] == pytest.approx(EXPECTED_CONC_FLOW, rel=1e-9)
    assert flows["tail"] == pytest.approx(EXPECTED_TAIL_FLOW, rel=1e-9)
    assert flows["feed"] == pytest.approx(100.0, abs=1e-9)
    prods = _recovery(res)
    rec = next(p for p in prods if p["stream_id"] == "conc")
    assert rec["recovery_percent"] == pytest.approx(EXPECTED_RECOVERY, rel=1e-9)
    # 品位几乎不动（dof=0，全部由测量决定，仅浮点级修正）
    grade_map = {s["id"]: s["grades"]["Cu"] for s in res["streams"]}
    assert grade_map["feed"] == pytest.approx(2.0, abs=1e-8)
    assert grade_map["conc"] == pytest.approx(20.0, abs=1e-8)
    assert grade_map["tail"] == pytest.approx(0.5, abs=1e-8)


def test_node_balance_closes_to_1e_9(topo, dataset):
    res = run_reconciliation(topo, dataset)
    assert res["max_relative_residual"] < RESIDUAL_GUARANTEE
    for nb in res["node_balances"]:
        assert abs(nb["relative_residual"]) < 1e-9
        for e, detail in nb["elements"].items():
            assert abs(detail["relative_residual"]) < 1e-9
    # 汇总闭合差也是机器精度
    cu = res["balance_table"]["elements"]["Cu"]
    assert abs(cu["closure_error_percent"]) < 1e-9


# --------------------------------------------------------------- 零调整
def test_zero_adjustment_when_input_balanced(topo):
    data = fully_measured_balanced_dataset()
    res = run_reconciliation(topo, data)
    assert res["converged"]
    # 输入本来完全平衡：校正量严格为零（允许 1e-12 的浮点噪声）
    for c in res["corrections"]:
        assert abs(c["adjustment"]) < 1e-10, c
    assert res["objective"] < 1e-20
    assert res["iterations"] == 0 or all(
        abs(c["adjustment"]) < 1e-10 for c in res["corrections"]
    )


# --------------------------------------------------------------- 同比缩放
@pytest.mark.parametrize("k", [0.07, 13.0, 1000.0])
def test_scaling_invariance(topo, k):
    d1 = handcalc_dataset(feed_flow=100.0)
    d2 = handcalc_dataset(feed_flow=100.0 * k)
    r1 = run_reconciliation(topo, d1)
    r2 = run_reconciliation(topo, d2)
    for s1, s2 in zip(r1["streams"], r2["streams"]):
        assert s2["flow"] == pytest.approx(k * s1["flow"], rel=1e-10)
        for e in s1["grades"]:
            assert s2["grades"][e] == pytest.approx(s1["grades"][e], abs=1e-10)
    # 回收率不变
    p1 = {p["stream_id"]: p["recovery_percent"]
          for p in r1["balance_table"]["elements"]["Cu"]["products"]}
    p2 = {p["stream_id"]: p["recovery_percent"]
          for p in r2["balance_table"]["elements"]["Cu"]["products"]}
    assert p2 == pytest.approx(p1, rel=1e-10)


# --------------------------------------------------------------- 大方差吸收
def test_large_variance_absorbs_adjustment(topo):
    """给矿品位与其余数据矛盾（2.2 vs 平衡值 2.0），冗余场景下看权重分配。"""
    def make(bad_rsd):
        ms = [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "conc", "type": "flow", "value": 5.0},
            {"stream_id": "tail", "type": "flow", "value": 95.0},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 2.2,
             **({"rsd": bad_rsd} if bad_rsd is not None else {})},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 21.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 1.0},
        ]
        return {"measurements": ms}

    normal = run_reconciliation(topo, make(None))
    huge = run_reconciliation(topo, make(5.0))  # 500% 相对标准差 = 极不可信

    def corr(res, stream, mtype, element=None):
        return next(
            c for c in res["corrections"]
            if c["stream_id"] == stream and c["type"] == mtype and c["element"] == element
        )

    bad_normal = corr(normal, "feed", "grade", "Cu")
    bad_huge = corr(huge, "feed", "grade", "Cu")
    # 大方差一方：加权调整量显著更小，而原始调整量更大（误差由它“扛”）
    assert abs(bad_huge["weighted_adjustment"]) < abs(bad_normal["weighted_adjustment"])
    assert abs(bad_huge["adjustment"]) > abs(bad_normal["adjustment"])
    # 它被拉回平衡值 2.0 附近，其余品位基本不动
    feed_grade = next(s for s in huge["streams"] if s["id"] == "feed")["grades"]["Cu"]
    assert feed_grade == pytest.approx(2.0, abs=0.02)
    conc_grade = next(s for s in huge["streams"] if s["id"] == "conc")["grades"]["Cu"]
    assert conc_grade == pytest.approx(21.0, abs=1e-3)


# --------------------------------------------------------------- 编号/顺序无关
def test_relabel_and_reorder_invariance():
    t1 = one_in_two_out_topology()
    d1 = handcalc_dataset()
    # 同构回路：节点/物流全部改名，输入顺序打乱，物流方向元素映射保持同构
    t2 = {
        "nodes": [{"id": "NodeX", "kind": "feeder"}],
        "streams": [
            {"id": "Z-tail", "source": "NodeX", "target": "@env", "kind": "tailings"},
            {"id": "A-feed", "source": "@env", "target": "NodeX", "kind": "feed"},
            {"id": "M-conc", "source": "NodeX", "target": "@env", "kind": "concentrate"},
        ],
        "elements": ["Cu"],
    }
    d2 = {
        "measurements": [
            {"stream_id": "Z-tail", "type": "grade", "element": "Cu", "value": 0.5},
            {"stream_id": "A-feed", "type": "grade", "element": "Cu", "value": 2.0},
            {"stream_id": "A-feed", "type": "flow", "value": 100.0},
            {"stream_id": "M-conc", "type": "grade", "element": "Cu", "value": 20.0},
        ]
    }
    r1 = run_reconciliation(t1, d1)
    r2 = run_reconciliation(t2, d2)
    m1 = {s["id"]: s for s in r1["streams"]}
    m2 = {s["id"]: s for s in r2["streams"]}
    assert m2["A-feed"]["flow"] == pytest.approx(m1["feed"]["flow"], rel=1e-10)
    assert m2["M-conc"]["flow"] == pytest.approx(m1["conc"]["flow"], rel=1e-10)
    assert m2["Z-tail"]["flow"] == pytest.approx(m1["tail"]["flow"], rel=1e-10)
    assert m2["M-conc"]["grades"]["Cu"] == pytest.approx(
        m1["conc"]["grades"]["Cu"], abs=1e-9
    )
    # 仅换输入顺序、不换名，结果应完全一致
    t3 = one_in_two_out_topology()
    d3 = {"measurements": list(reversed(d1["measurements"]))}
    r3 = run_reconciliation(t3, d3)
    for a, b in zip(sorted(r1["streams"], key=lambda s: s["id"]),
                    sorted(r3["streams"], key=lambda s: s["id"])):
        assert a["flow"] == pytest.approx(b["flow"], abs=1e-12)
        assert a["grades"] == pytest.approx(b["grades"], abs=1e-12)
