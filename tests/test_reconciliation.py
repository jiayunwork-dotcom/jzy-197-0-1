"""算法性质用例：一进两出算例、闭合精度、零调整、缩放、大方差、
编号无关、可观测性、粗差识别、迭代上限。"""
from __future__ import annotations

import numpy as np
import pytest

from app.measurement_model import (FlowMeasurement, GradeMeasurement,
                                   StreamData, SurveyData)
from app.reconciliation import (DEFAULT_MAX_ITER, run_reconciliation)
from app.topology import (CircuitTopology, Node, Stream, incidence_matrix,
                          validate_topology)
from app.gross_error import gross_error_test_named


def topo_1in2out(nodes=None, streams=None, code_prefix=""):
    p = code_prefix
    t = CircuitTopology(
        "demo", 1,
        nodes=tuple(nodes or [Node("sep")]),
        streams=tuple(streams or [
            Stream(p + "feed", None, "sep", role="feed"),
            Stream(p + "conc", "sep", None, role="concentrate"),
            Stream(p + "tail", "sep", None, role="tailings"),
        ]),
        elements=("Cu",))
    validate_topology(t)
    return t


def data_1in2out(prefix="", feed_flow=100.0, feed_g=2.0, conc_g=20.0,
                 tail_g=0.5, conc_flow=None, tail_flow=None,
                 feed_flow_rsd=None):
    p = prefix
    fm = FlowMeasurement(feed_flow, rsd=feed_flow_rsd)
    return SurveyData(streams=(
        StreamData(p + "feed", fm,
                   (GradeMeasurement("Cu", feed_g),)),
        StreamData(p + "conc",
                   FlowMeasurement(conc_flow) if conc_flow is not None else None,
                   (GradeMeasurement("Cu", conc_g),)),
        StreamData(p + "tail",
                   FlowMeasurement(tail_flow) if tail_flow is not None else None,
                   (GradeMeasurement("Cu", tail_g),)),
    ))


def flows(model, x):
    return {c: float(x[i]) for i, c in enumerate(model.stream_codes)}


def grades(model, x):
    nf = model.n_flow
    return {c: float(x[nf + i]) * 100.0
            for i, c in enumerate(model.stream_codes)}


# ---------------------------------------------------------------------------
def test_one_in_two_out_hand_calc():
    """手算核对：精矿≈7.692、尾矿≈92.308、回收率≈76.92%，完全闭合。"""
    topo = topo_1in2out()
    res, model = run_reconciliation(topo, data_1in2out())
    assert res.status == "converged"
    f = flows(model, res.x)
    g = grades(model, res.x)
    assert f["conc"] == pytest.approx(7.6923076923, abs=1e-7)
    assert f["tail"] == pytest.approx(92.3076923077, abs=1e-6)
    assert f["feed"] == pytest.approx(100.0, abs=1e-9)
    assert g["feed"] == 2.0 and g["conc"] == 20.0 and g["tail"] == 0.5
    recovery = 100 * f["conc"] * g["conc"] / (f["feed"] * g["feed"])
    assert recovery == pytest.approx(76.923076923, abs=1e-7)


def test_one_in_two_out_with_explicit_grade_errors():
    """三品位都显式给测量误差、给矿流量也是测量值：结果落在手算数附近并闭合。

    数据轻微不自洽（品位在误差内抖动、流量测成 100.5），校正只做极小调整，
    流量分配仍≈7.69/92.31，回收率≈76.9%。
    """
    topo = topo_1in2out()
    data = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(100.5, rsd=0.015),
                   (GradeMeasurement("Cu", 2.02, rsd=0.04),)),
        StreamData("conc", None,
                   (GradeMeasurement("Cu", 19.8, rsd=0.02),)),
        StreamData("tail", None,
                   (GradeMeasurement("Cu", 0.505, rsd=0.08),)),
    ))
    res, model = run_reconciliation(topo, data)
    assert res.status == "converged"
    assert res.max_relative_residual < 1e-9
    f = flows(model, res.x)
    assert f["conc"] == pytest.approx(7.6923, rel=0.03)
    assert f["tail"] == pytest.approx(92.3077, rel=0.03)
    g = grades(model, res.x)
    rec = 100 * f["conc"] * g["conc"] / (f["feed"] * g["feed"])
    assert rec == pytest.approx(76.92, abs=0.5)


def test_closure_precision_under_1e9():
    """所有节点干矿/金属平衡残差相对值必须 < 1e-9。"""
    # 用一个有冗余测量、需要调整的情形，残差仍应收敛到机器精度
    topo = topo_1in2out()
    data = data_1in2out(conc_flow=8.0, tail_flow=91.0)
    res, model = run_reconciliation(topo, data)
    assert res.status == "converged"
    assert res.max_relative_residual < 1e-9
    a = incidence_matrix(topo)
    f = res.x[:model.n_flow]
    assert abs(a @ f) / 100.0 < 1e-12
    g = res.x[model.n_flow:2 * model.n_flow]
    assert abs((a * g) @ f) / 2.0 < 1e-12


def test_zero_adjustment_when_balanced():
    """输入本来就完全平衡时，校正量必须为零。

    取与手算解一致的三流量/三品位测量：7.692307..., 92.307692...
    """
    topo = topo_1in2out()
    fc, ft = 100.0 / 13.0, 1200.0 / 13.0
    data = data_1in2out(conc_flow=fc, tail_flow=ft)
    res, model = run_reconciliation(topo, data)
    assert res.status == "converged"
    for i, mid in enumerate(model.measurement_ids):
        if mid is not None:
            assert abs(res.x[i] - model.x_meas[i]) < 1e-9 * max(
                1.0, abs(model.x_meas[i]))


def test_scaling_invariance():
    """所有流量同乘 k>0：校正流量同比缩放，品位不变。"""
    topo = topo_1in2out()
    base, model0 = run_reconciliation(topo, data_1in2out())
    k = 3.7
    scaled = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(370.0),
                   (GradeMeasurement("Cu", 2.0),)),
        StreamData("conc", None, (GradeMeasurement("Cu", 20.0),)),
        StreamData("tail", None, (GradeMeasurement("Cu", 0.5),)),
    ))
    res, model = run_reconciliation(topo, scaled)
    assert res.status == "converged"
    fb, fs = flows(model0, base.x), flows(model, res.x)
    for code in fb:
        assert fs[code] == pytest.approx(k * fb[code], rel=1e-9)
    gb, gs = grades(model0, base.x), grades(model, res.x)
    for code in gb:
        assert gs[code] == pytest.approx(gb[code], abs=1e-10)


def test_large_variance_absorbs_adjustment():
    """给某测量很大的标准差，调整应主要落在它身上。

    用纯干矿场景：给矿测 100（rsd=1000%，σ=1000），精矿测 30（σ=0.45）、
    尾矿测 68（σ=1.02），质量不平衡 −2，调整应几乎全部落在给矿上。
    """
    t = CircuitTopology("demo", 1, nodes=(Node("sep"),),
                        streams=(Stream("feed", None, "sep", role="feed"),
                                 Stream("conc", "sep", None,
                                        role="concentrate"),
                                 Stream("tail", "sep", None,
                                        role="tailings")),
                        elements=())
    validate_topology(t)
    data = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(100.0, rsd=10.0)),
        StreamData("conc", FlowMeasurement(30.0)),
        StreamData("tail", FlowMeasurement(68.0)),
    ))
    res, model = run_reconciliation(t, data)
    assert res.status == "converged"
    f = flows(model, res.x)
    # 给矿几乎吃满全部 2 t/h 的不衡量
    assert f["feed"] == pytest.approx(98.0, abs=0.02)
    assert abs(f["conc"] - 30.0) < 0.02
    assert abs(f["tail"] - 68.0) < 0.02

    # 对照组：给矿 σ 很小（rsd=0.1%），调整就该主要落在精矿/尾矿上
    data2 = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(100.0, rsd=0.001)),
        StreamData("conc", FlowMeasurement(30.0)),
        StreamData("tail", FlowMeasurement(68.0)),
    ))
    res2, model2 = run_reconciliation(t, data2)
    f2 = flows(model2, res2.x)
    assert abs(f2["feed"] - 100.0) < 0.02
    assert abs(f2["conc"] - 30.0) + abs(f2["tail"] - 68.0) > 1.5


def test_code_and_order_independence():
    """物流/节点换编号、输入换顺序，结果（按新编号）应不变。"""
    t_a = topo_1in2out()
    d_a = data_1in2out()
    ra, ma = run_reconciliation(t_a, d_a)

    # 同一拓扑，编码重命名，物流输入顺序打乱
    rename = {"feed": "z_in", "conc": "a_conc", "tail": "m_tail"}
    t_b = CircuitTopology(
        "demo", 1,
        nodes=(Node("unitX"),),
        streams=(
            Stream(rename["tail"], "unitX", None, role="tailings"),
            Stream(rename["feed"], None, "unitX", role="feed"),
            Stream(rename["conc"], "unitX", None, role="concentrate"),
        ),
        elements=("Cu",))
    validate_topology(t_b)
    d_b = SurveyData(streams=(
        StreamData(rename["tail"], None, (GradeMeasurement("Cu", 0.5),)),
        StreamData(rename["feed"], FlowMeasurement(100.0),
                   (GradeMeasurement("Cu", 2.0),)),
        StreamData(rename["conc"], None, (GradeMeasurement("Cu", 20.0),)),
    ))
    rb, mb = run_reconciliation(t_b, d_b)
    assert rb.status == "converged"
    fa, fb = flows(ma, ra.x), flows(mb, rb.x)
    for old, new in rename.items():
        assert fb[new] == pytest.approx(fa[old], rel=1e-10)
    assert rb.max_relative_residual == pytest.approx(
        ra.max_relative_residual, rel=1e-6, abs=1e-15)


def test_observability_detected():
    """只有给矿流量、没有任何品位时，两股未测出料不可区分，必须报不可观测。"""
    t = CircuitTopology("demo", 1, nodes=(Node("sep"),),
                        streams=(Stream("feed", None, "sep", role="feed"),
                                 Stream("conc", "sep", None,
                                        role="concentrate"),
                                 Stream("tail", "sep", None,
                                        role="tailings")),
                        elements=())
    validate_topology(t)
    data = SurveyData(streams=(StreamData("feed", FlowMeasurement(100.0)),
                               StreamData("conc"), StreamData("tail")))
    res, model = run_reconciliation(t, data)
    assert res.reason == "unobservable"
    bad = {u["stream"] for u in res.observability["unobservable"]
           if u["kind"] == "flow"}
    assert {"conc", "tail"} <= bad


def test_observability_ok_when_split_known():
    """同拓扑再加一股出方流量测量，未测流量即可唯一确定。"""
    t = CircuitTopology("demo", 1, nodes=(Node("sep"),),
                        streams=(Stream("feed", None, "sep", role="feed"),
                                 Stream("conc", "sep", None,
                                        role="concentrate"),
                                 Stream("tail", "sep", None,
                                        role="tailings")),
                        elements=())
    data = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(100.0)),
        StreamData("conc", FlowMeasurement(30.0)),
        StreamData("tail")))
    res, model = run_reconciliation(t, data)
    assert res.status == "converged"
    assert flows(model, res.x)["tail"] == pytest.approx(70.0, abs=1e-9)


def test_gross_error_detection_and_exclusion():
    """明显粗差要被全局/测量检验抓到；剔除后重算无冗余、结果按平衡推算。"""
    # 纯干矿场景：三股流量都测，给矿错成 120（σ≈1.8，偏差约 11σ）
    topo = CircuitTopology("demo", 1, nodes=(Node("sep"),),
                           streams=(Stream("feed", None, "sep", role="feed"),
                                    Stream("conc", "sep", None,
                                           role="concentrate"),
                                    Stream("tail", "sep", None,
                                           role="tailings")),
                           elements=())
    validate_topology(topo)
    data = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(120.0)),
        StreamData("conc", FlowMeasurement(7.7)),
        StreamData("tail", FlowMeasurement(92.3)),
    ))
    res, model = run_reconciliation(topo, data)
    assert res.status == "converged"
    a = incidence_matrix(topo)
    ge = gross_error_test_named(model, a, res, ["sep"])
    assert ge["test_available"]
    assert ge["global_test"]["gross_error_detected"]
    ids = [s["measurement_id"] for s in ge["suspects"]]
    assert "flow:feed" in ids
    assert ge["suspects"][0]["measurement_id"] == "flow:feed"

    # 剔除给矿流量后：2 测量、1 未测，冗余度降为 1，且剩余数据已无矛盾，
    # 全局检验不应再报警
    res2, model2 = run_reconciliation(
        topo, data, excluded=frozenset({"flow:feed"}))
    ge2 = gross_error_test_named(model2, a, res2, ["sep"])
    assert ge2["test_available"]
    assert ge2["degrees_of_freedom"] == 1
    assert not ge2["global_test"]["gross_error_detected"]
    f2 = flows(model2, res2.x)
    assert f2["feed"] == pytest.approx(100.0, abs=1e-6)
    assert res2.max_relative_residual < 1e-9

    # 再剔除一股出料，只剩 1 个测量、2 个未测流量，冗余度为 0，无法检验
    res3, model3 = run_reconciliation(
        topo, data, excluded=frozenset({"flow:feed", "flow:tail"}))
    ge3 = gross_error_test_named(model3, a, res3, ["sep"])
    assert not ge3["test_available"]
    assert ge3["degrees_of_freedom"] <= 0


def test_iteration_limit_reported_honestly():
    """max_iterations=1 且问题非平凡（有未测品位）时不许伪装成功。"""
    # 两节点回路：粗选 + 扫选，精/尾品位都测，给矿品位测，中矿品位未测，
    # 使每一步都需要同时修正流量和品位（真正非线性）。
    t = CircuitTopology("demo", 1,
        nodes=(Node("rougher"), Node("scavenger")),
        streams=(
            Stream("feed", None, "rougher", role="feed"),
            Stream("conc", "rougher", None, role="concentrate"),
            Stream("mids", "rougher", "scavenger", role="intermediate"),
            Stream("tails", "scavenger", None, role="tailings"),
        ), elements=("Cu",))
    validate_topology(t)
    data = SurveyData(streams=(
        StreamData("feed", FlowMeasurement(100.0),
                   (GradeMeasurement("Cu", 2.0),)),
        StreamData("conc", FlowMeasurement(20.0),
                   (GradeMeasurement("Cu", 8.0),)),
        StreamData("mids", None, ()),   # 中矿流量/品位都未测
        StreamData("tails", FlowMeasurement(70.0),
                   (GradeMeasurement("Cu", 0.3),)),
    ))
    res, model = run_reconciliation(t, data, max_iterations=1)
    assert res.reason == "max_iterations"
    assert res.converged is False
    assert res.iterations == 1
    assert len(res.observations) == 1
    # 残差如实返回
    assert np.all(np.isfinite(res.residuals))

    # 放宽上限后应能闭合
    res2, _ = run_reconciliation(t, data, max_iterations=DEFAULT_MAX_ITER)
    assert res2.status == "converged"
    assert res2.max_relative_residual < 1e-9
