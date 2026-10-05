"""接口与版本用例（走 MemoryStore；设置 RECON_DB_DSN 后同一套在 PG16 上跑）：
一进两出通过 API、结果复用、版本对比、并发修改冲突、走势、拒收情况。
"""
from __future__ import annotations

import threading

import pytest

from tests.conftest import (SURVEY_1IN2OUT, TOPO_1IN2OUT, make_circuit_and_topology,
                            make_survey, reconcile, unique)


def _circuit_survey(client, streams=SURVEY_1IN2OUT, month="2026-04-01",
                    topo=TOPO_1IN2OUT):
    code = make_circuit_and_topology(client, topo)
    sid = make_survey(client, code, streams, month=month)
    return code, sid


def test_api_one_in_two_out(client):
    code, sid = _circuit_survey(client)
    r = reconcile(client, sid)
    assert r.status_code == 201, r.get_json()
    result = r.get_json()["result"]
    assert result["status"] == "converged"
    assert result["max_relative_residual"] < 1e-9
    by = {s["stream"]: s for s in result["streams"]}
    assert by["conc"]["dry_ore_flow"] == pytest.approx(7.6923077, abs=1e-6)
    assert by["tail"]["dry_ore_flow"] == pytest.approx(92.3076923, abs=1e-5)
    rec = result["recoveries"]["elements"]["Cu"]
    assert rec["recovery_percent"] == pytest.approx(76.923077, abs=1e-6)

    # 平衡表
    rid = r.get_json()["id"]
    bs = client.get(f"/api/reconciliations/{rid}/balance-sheet")
    assert bs.status_code == 200
    row = bs.get_json()["node_balance"][0]
    assert abs(row["dry_ore"]["residual"]) < 1e-9
    assert abs(row["metals"]["Cu"]["residual"]) < 1e-9


def test_run_reuse_same_settings(client):
    _, sid = _circuit_survey(client)
    r1 = reconcile(client, sid)
    r2 = reconcile(client, sid)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.get_json()["cached"] is True
    assert r2.get_json()["id"] == r1.get_json()["id"]
    # 不同剔除/参数属于不同设定，应产生新结果
    r3 = reconcile(client, sid, max_iterations=10)
    assert r3.get_json()["id"] != r1.get_json()["id"]


def test_version_compare(client):
    _, sid = _circuit_survey(client)
    reconcile(client, sid)
    # v2：化验室把精矿品位更正为 18%
    streams_v2 = [
        {"code": "feed", "flow": {"value": 100.0},
         "grades": [{"element": "Cu", "value": 2.0}]},
        {"code": "conc", "grades": [{"element": "Cu", "value": 18.0}]},
        {"code": "tail", "grades": [{"element": "Cu", "value": 0.5}]},
    ]
    r = client.put(f"/api/surveys/{sid}",
                   json={"base_version": 1, "streams": streams_v2})
    assert r.status_code == 201
    reconcile(client, sid)
    cmp_ = client.get(f"/api/surveys/{sid}/compare?v1=1&v2=2")
    assert cmp_.status_code == 200
    body = cmp_.get_json()
    assert body["v1"]["version"] == 1 and body["v2"]["version"] == 2
    rec = body["recovery_percent"]["Cu"]
    assert rec["v1"] == pytest.approx(76.9231, abs=1e-3)
    assert rec["v2"] == pytest.approx(77.1429, abs=1e-3)
    assert rec["delta_points"] == pytest.approx(rec["v2"] - rec["v1"], abs=1e-9)
    # 每股物流都列了流量对比
    assert {r["stream"] for r in body["streams"]} == {"feed", "conc", "tail"}


def test_concurrent_revision_conflict(client):
    _, sid = _circuit_survey(client)
    results = []

    def revise(base):
        rr = client.put(f"/api/surveys/{sid}",
                        json={"base_version": base,
                              "streams": SURVEY_1IN2OUT})
        results.append(rr.status_code)

    # 两人都基于 v1 同时提交
    t1 = threading.Thread(target=revise, args=(1,))
    t2 = threading.Thread(target=revise, args=(1,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 409]
    err = [s for s in results if s == 409]
    assert err  # 后到一方收到冲突，没有静默覆盖
    # 当前版本应为 2
    assert client.get(f"/api/surveys/{sid}").get_json()["current_version"] == 2


def test_recovery_trend_across_months(client):
    code = make_circuit_and_topology(client)
    for month, conc_g in [("2026-06-01", 20.0), ("2026-07-01", 22.0),
                          ("2026-08-01", 18.0)]:
        streams = [
            {"code": "feed", "flow": {"value": 100.0},
             "grades": [{"element": "Cu", "value": 2.0}]},
            {"code": "conc", "grades": [{"element": "Cu", "value": conc_g}]},
            {"code": "tail", "grades": [{"element": "Cu", "value": 0.5}]},
        ]
        sid = make_survey(client, code, streams, month=month)
        reconcile(client, sid)
    trend = client.get(f"/api/surveys/{sid}/recovery-trend").get_json()
    months = [p["month"] for p in trend["points"]]
    assert months == ["2026-06-01", "2026-07-01", "2026-08-01"]
    rec = [p["recovery_percent"]["Cu"] for p in trend["points"]]
    assert rec[0] == pytest.approx(76.9231, abs=1e-3)
    assert rec[1] < rec[0] < rec[2]
    # months 限制
    trend2 = client.get(f"/api/surveys/{sid}/recovery-trend?months=2").get_json()
    assert len(trend2["points"]) == 2


def test_topology_versions_isolated(client):
    """拓扑改新版后，旧考查/旧结果仍绑定旧拓扑，不被影响。"""
    code = make_circuit_and_topology(client)
    sid = make_survey(client, code, SURVEY_1IN2OUT, month="2026-05-01")
    run1 = reconcile(client, sid).get_json()
    assert run1["topology_version"] == 1

    # 新拓扑版本：加一台扫选，精矿走粗选、尾矿走扫选，中间新增循环中矿
    topo2 = {
        "elements": ["Cu"],
        "nodes": [{"code": "sep", "name": "粗选"}, {"code": "scav", "name": "扫选"}],
        "streams": [
            {"code": "feed", "source": None, "target": "sep", "role": "feed"},
            {"code": "conc", "source": "sep", "target": None,
             "role": "concentrate"},
            {"code": "mids", "source": "sep", "target": "scav"},
            {"code": "tail", "source": "scav", "target": None,
             "role": "tailings"},
        ],
    }
    r = client.put(f"/api/circuits/{code}/topology", json=topo2)
    assert r.status_code == 201 and r.get_json()["version"] == 2

    # 旧考查仍声明 v1，旧结果原样可取
    got = client.get(f"/api/reconciliations/{run1['id']}")
    assert got.status_code == 200
    assert got.get_json()["topology_version"] == 1
    old_topo = client.get(f"/api/circuits/{code}/topology/1").get_json()
    assert len(old_topo["topology"]["nodes"]) == 1

    # 新考查挂 v2：只测给矿流量+品位、精矿品位；mids 与 tail 的流量/品位
    # 共 5 个未测量，而约束雅可比列秩只有 4，扫选回路的分流不能唯一确定
    sid2 = make_survey(client, code,
                       [{"code": "feed", "flow": {"value": 100.0},
                         "grades": [{"element": "Cu", "value": 2.0}]},
                        {"code": "conc",
                         "grades": [{"element": "Cu", "value": 20.0}]},
                        {"code": "mids"},
                        {"code": "tail"}],
                       month="2026-09-01", topology_version=2)
    r = reconcile(client, sid2)
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "unobservable"


def test_rejection_cases(client):
    code = unique("bad")
    client.post("/api/circuits", json={"code": code})

    # 孤立节点
    bad = {"elements": ["Cu"],
           "nodes": [{"code": "n1"}, {"code": "orphan"}],
           "streams": [{"code": "s1", "source": None, "target": "n1"}]}
    r = client.put(f"/api/circuits/{code}/topology", json=bad)
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "invalid_topology"

    # 物流端点不存在
    bad2 = {"elements": ["Cu"], "nodes": [{"code": "n1"}],
            "streams": [{"code": "s1", "source": None, "target": "ghost"}]}
    r = client.put(f"/api/circuits/{code}/topology", json=bad2)
    assert r.status_code == 400

    # 合法拓扑后再测数据问题
    client.put(f"/api/circuits/{code}/topology", json=TOPO_1IN2OUT)
    # 品位越界
    r = client.post("/api/surveys", json={
        "circuit_code": code, "topology_version": 1, "month": "2026-03-01",
        "streams": [{"code": "feed", "flow": {"value": 100.0},
                     "grades": [{"element": "Cu", "value": 120.0}]}]})
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "invalid_survey"
    # 负流量
    r = client.post("/api/surveys", json={
        "circuit_code": code, "topology_version": 1, "month": "2026-02-01",
        "streams": [{"code": "feed", "flow": {"value": -5.0}}]})
    assert r.status_code == 400
    # 标准差不为正
    r = client.post("/api/surveys", json={
        "circuit_code": code, "topology_version": 1, "month": "2026-01-01",
        "streams": [{"code": "feed", "flow": {"value": 100.0, "rsd": 0}}]})
    assert r.status_code == 400


def test_underdetermined_rejected_then_accepted(client):
    """节点完全没有任何测量、系统欠定时单独提示；显式接受可拿到部分结果。"""
    topo = {"elements": [], "nodes": [{"code": "sep"}],
            "streams": TOPO_1IN2OUT["streams"]}
    code = make_circuit_and_topology(client, topo=topo)
    # 只测给矿，精尾未测 -> 分流比无法确定
    sid = make_survey(client, code,
                      [{"code": "feed", "flow": {"value": 100.0}},
                       {"code": "conc"}, {"code": "tail"}],
                      month="2026-01-05")
    r = reconcile(client, sid)
    assert r.status_code == 422
    body = r.get_json()["error"]
    assert body["code"] == "unobservable"
    bad = {(u["kind"], u["stream"]) for u in body["details"]["unobservable"]}
    assert ("flow", "conc") in bad and ("flow", "tail") in bad

    r2 = reconcile(client, sid, accept_unobservable=True)
    assert r2.status_code in (200, 201)
    assert r2.get_json()["result"]["status"] == "unobservable"
