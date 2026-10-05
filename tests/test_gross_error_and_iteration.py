"""粗差识别测试与迭代上限测试。"""

from __future__ import annotations

import pytest

from reconciliation.service import run_reconciliation

from .conftest import handcalc_dataset, one_in_two_out_topology


def _dataset_with_bad_conc_flow(bad_conc: float, bad_tail: float):
    return {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "conc", "type": "flow", "value": bad_conc},
            {"stream_id": "tail", "type": "flow", "value": bad_tail},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 2.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 20.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 0.5},
        ]
    }


def test_clean_data_passes_global_test(topo):
    # 平衡数据 + 全部流量已测：dof=2，chi2≈0
    data = {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "conc", "type": "flow", "value": 7.692307692},
            {"stream_id": "tail", "type": "flow", "value": 92.307692308},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 2.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 20.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 0.5},
        ]
    }
    res = run_reconciliation(topo, data)
    ge = res["gross_errors"]
    assert ge["dof"] == 2
    assert ge["global_test"]["failed"] is False
    assert ge["p_value"] > 0.05
    assert ge["suspects"] == []


def test_gross_error_is_flagged_and_exclusion_reduces_chi2(topo):
    # 精矿流量真值约 7.69，错给成 2.0（-74%），尾矿 98.0 保质量平衡
    data = _dataset_with_bad_conc_flow(2.0, 98.0)
    res = run_reconciliation(topo, data)
    ge = res["gross_errors"]
    assert ge["dof"] == 2
    assert ge["global_test"]["failed"] is True
    assert ge["primary_suspect"] is not None
    # 首要嫌疑应指向精矿流量（或与之冲突最强的那个测量）
    # 注：测量检验对共线约束可能把矛盾定位到品位上，因此只要求嫌疑确实
    # 存在、|z| 最大，且剔除后 χ² 显著下降。
    suspect = ge["primary_suspect"]
    assert suspect["abs_z"] > 2.58
    rid = suspect["measurement_id"]

    res2 = run_reconciliation(
        topo, data, {"excluded_measurement_ids": [rid]}
    )
    assert res2["gross_errors"]["chi_square"] < ge["chi_square"] * 0.05
    assert rid in res2["excluded_measurement_ids"]
    # 剔除的是精矿流量：该流量变回由守恒推出的真值
    if suspect["stream_id"] == "conc" and suspect["type"] == "flow":
        flow = next(s for s in res2["streams"] if s["id"] == "conc")["flow"]
        assert flow == pytest.approx(7.692307692, rel=1e-4)


def test_zero_dof_has_no_statistical_test(topo, dataset):
    """手算例只有 4 个测量、0 冗余：必须明确说明无法检验，而不是乱报。"""
    res = run_reconciliation(topo, dataset)
    ge = res["gross_errors"]
    assert ge["dof"] == 0
    assert ge["global_test"]["failed"] is False
    assert "无法" in ge["global_test"]["note"]
    assert ge["measurement_tests"] == []
    assert ge["suspects"] == []


def test_iteration_cap_returns_current_state_honestly(topo):
    # 明显不平衡的全测量数据，只给 0 次迭代：必须返回 converged=False
    data = _dataset_with_bad_conc_flow(40.0, 40.0)
    res0 = run_reconciliation(topo, data, {"max_iter": 0})
    assert res0["converged"] is False
    assert res0["iterations"] == 0
    assert res0["max_relative_residual"] > 1e-6
    assert len(res0["residual_history"]) == 1

    # 给 1 次也未必收敛（强非线性偏移），但结果仍如实返回
    res1 = run_reconciliation(topo, data, {"max_iter": 1})
    assert res1["iterations"] == 1

    # 放宽上限后应收敛
    res_full = run_reconciliation(topo, data, {"max_iter": 50})
    assert res_full["converged"] is True
    assert res_full["max_relative_residual"] < 1e-9
