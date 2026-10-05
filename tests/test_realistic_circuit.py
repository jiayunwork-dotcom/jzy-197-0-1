"""非平凡回路的稳健性与性质测试：多节点 + 循环中矿 + 多元素 + 冗余测量 + 噪声。"""
from __future__ import annotations

import numpy as np
import pytest

from app.gross_error import gross_error_test_named
from app.measurement_model import (FlowMeasurement, GradeMeasurement,
                                   StreamData, SurveyData)
from app.reconciliation import run_reconciliation
from app.topology import (CircuitTopology, Node, Stream, incidence_matrix,
                          validate_topology)

#
#   feed ──▶ ROUGHER ──conc1──▶ CLEANER ──▶ conc (精矿, 出系统)
#               ▲ │
#            cmids│ ──mids──▶ SCAVENGER ──▶ tail (尾矿, 出系统)
#               └─────────────┘
#
def build_circuit():
    t = CircuitTopology("plant", 3,
        nodes=(Node("rougher"), Node("cleaner"), Node("scavenger")),
        streams=(
            Stream("feed", None, "rougher", role="feed"),
            Stream("conc1", "rougher", "cleaner"),
            Stream("mids", "rougher", "scavenger"),
            Stream("conc", "cleaner", None, role="concentrate"),
            Stream("cmids", "scavenger", "rougher"),
            Stream("tail", "scavenger", None, role="tailings"),
        ),
        elements=("Cu", "Mo"))
    validate_topology(t)
    return t


# 严格自洽的真值（品位百分数，与接口一致）。
# 干矿： cleaner: conc1 25 = conc 25
#        scav:    mids 90 = cmids 15 + tail 75
#        rougher: feed 100 + cmids 15 = conc1 25 + mids 90
TRUE_FLOW = {"feed": 100.0, "conc1": 25.0, "mids": 90.0,
             "conc": 25.0, "cmids": 15.0, "tail": 75.0}
# Cu：scav 90·g_mids = 75·0.1 + 15·0.5 = 15 -> g_mids=0.166667
#     rougher 100·g_feed + 15·0.5 = 25·3.2 + 90·0.166667 = 95 -> g_feed=0.875
TRUE_GRADE_CU = {"feed": 0.875, "conc1": 3.2, "mids": 0.1666667,
                 "conc": 3.2, "cmids": 0.5, "tail": 0.1}
# Mo：scav 90·g_mids = 75·0.001 + 15·0.025 = 0.45 -> g_mids=0.005
#     rougher 100·g_feed + 15·0.025 = 25·0.07 + 90·0.005 = 2.2 -> g_feed=0.01825
TRUE_GRADE_MO = {"feed": 0.01825, "conc1": 0.07, "mids": 0.005,
                 "conc": 0.07, "cmids": 0.025, "tail": 0.001}

STREAM_ORDER = ["feed", "conc1", "mids", "conc", "cmids", "tail"]


def survey_with_noise(seed=0, noise=0.004, measure_all_flows=True):
    rng = np.random.default_rng(seed)
    rows = []
    for code in STREAM_ORDER:
        if measure_all_flows:
            f = max(TRUE_FLOW[code] * (1 + rng.normal(0, noise)), 0.01)
            fm = FlowMeasurement(f)
        else:
            fm = FlowMeasurement(TRUE_FLOW[code]) if code == "feed" else None
        g_cu = max(TRUE_GRADE_CU[code] * (1 + rng.normal(0, noise)), 1e-6)
        g_mo = max(TRUE_GRADE_MO[code] * (1 + rng.normal(0, noise)), 1e-6)
        rows.append(StreamData(code, fm, (
            GradeMeasurement("Cu", g_cu),
            GradeMeasurement("Mo", g_mo))))
    return SurveyData(tuple(rows))


def exact_survey():
    rows = []
    for code in STREAM_ORDER:
        rows.append(StreamData(code, FlowMeasurement(TRUE_FLOW[code]), (
            GradeMeasurement("Cu", TRUE_GRADE_CU[code]),
            GradeMeasurement("Mo", TRUE_GRADE_MO[code]))))
    return SurveyData(tuple(rows))


def flow_map(model, x):
    return {c: float(x[i]) for i, c in enumerate(model.stream_codes)}


def test_realistic_circuit_converges_and_closes():
    topo = build_circuit()
    res, model = run_reconciliation(topo, survey_with_noise(seed=42))
    assert res.status == "converged"
    assert res.iterations <= 15
    assert res.max_relative_residual < 1e-9
    f = flow_map(model, res.x)
    for code, v in TRUE_FLOW.items():
        assert f[code] == pytest.approx(v, rel=0.03), code


def test_realistic_zero_adjustment_for_exact_truth():
    topo = build_circuit()
    res, model = run_reconciliation(topo, exact_survey())
    assert res.status == "converged"
    for i, mid in enumerate(model.measurement_ids):
        if mid:
            tol = 1e-8 * max(1.0, abs(model.x_meas[i]))
            assert abs(res.x[i] - model.x_meas[i]) < tol, mid
    assert res.max_relative_residual < 1e-10


def test_realistic_recovery_reasonable():
    topo = build_circuit()
    res, model = run_reconciliation(topo, survey_with_noise(seed=7))
    idx = {c: i for i, c in enumerate(model.stream_codes)}

    def metal(code, e):
        return res.x[idx[code]] * res.x[model.grade_index[(e, code)]]

    recovery = 100 * metal("conc", "Cu") / metal("feed", "Cu")
    # 真值：25·3.2% / (100·0.875%) = 91.43%
    assert recovery == pytest.approx(91.43, abs=3.0)


def test_realistic_redundancy_and_no_false_gross_error():
    """无粗差的小噪声数据，全局 χ² 不应报警。"""
    topo = build_circuit()
    res, model = run_reconciliation(topo, survey_with_noise(seed=1, noise=0.001))
    a = incidence_matrix(topo)
    ge = gross_error_test_named(model, a, res,
                                sorted(n.code for n in topo.nodes))
    assert ge["test_available"]
    assert ge["degrees_of_freedom"] > 0
    assert not ge["global_test"]["gross_error_detected"]


def test_realistic_partial_flow_measurement_observability():
    """只测给矿流量、其余流量未测，但所有品位都测：含循环回路仍可观测。"""
    topo = build_circuit()
    res, model = run_reconciliation(
        topo, survey_with_noise(seed=3, measure_all_flows=False))
    assert res.status == "converged"
    assert res.observability["observable"]
    f = flow_map(model, res.x)
    for code, v in TRUE_FLOW.items():
        assert f[code] == pytest.approx(v, rel=0.05), code
