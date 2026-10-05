"""接口层测试：版本复用、版本对比、并发修订冲突、拓扑版本绑定、走势、拒收码。"""

from __future__ import annotations

import json
import threading

import pytest

from tests.conftest import (
    EXPECTED_CONC_FLOW,
    EXPECTED_RECOVERY,
    handcalc_dataset,
    one_in_two_out_topology,
)

TOPO = one_in_two_out_topology()
DATA = handcalc_dataset()


def _seed(client, topology=None, dataset=None, campaign_id="camp1", period="2026-09"):
    client.post("/api/circuits", json={"id": "circ", "name": "浮选回路"})
    r = client.post("/api/circuits/circ/topologies", json={"topology": topology or TOPO})
    tv = r.get_json()["id"]
    client.post(
        "/api/campaigns",
        json={
            "id": campaign_id,
            "circuit_id": "circ",
            "name": "9月流程考查",
            "period": period,
            "topology_version_id": tv,
        },
    )
    r = client.post(
        f"/api/campaigns/{campaign_id}/versions",
        json={"parent_id": None, "dataset": dataset or DATA},
    )
    assert r.status_code == 201, r.get_json()
    return tv, r.get_json()["id"]


# --------------------------------------------------------------- 主流程 + 复用
def test_reconcile_end_to_end_and_reuse(client):
    tv, vid = _seed(client)
    r1 = client.post(f"/api/campaign-versions/{vid}/reconcile", json={})
    assert r1.status_code == 201
    body1 = r1.get_json()
    assert body1["converged"] is True
    assert body1["topology_version_id"] == tv
    assert body1["reused"] is False
    flows = {s["id"]: s["flow"] for s in body1["result"]["streams"]}
    assert flows["conc"] == pytest.approx(EXPECTED_CONC_FLOW, rel=1e-9)

    # 同版本同设定重复提交：直接复用，不重新算
    r2 = client.post(f"/api/campaign-versions/{vid}/reconcile", json={})
    body2 = r2.get_json()
    assert r2.status_code == 200
    assert body2["reused"] is True
    assert body2["run_id"] == body1["run_id"]

    # 不同设定（剔除某个测量）产生独立结果
    r3 = client.post(
        f"/api/campaign-versions/{vid}/reconcile",
        json={"settings": {"excluded_measurement_ids": []}},
    )
    # 空剔除集合与空设定指纹相同 -> 仍复用
    assert r3.get_json()["reused"] is True


def test_balance_table_endpoint(client):
    _, vid = _seed(client)
    run = client.post(f"/api/campaign-versions/{vid}/reconcile", json={}).get_json()
    r = client.get(f"/api/runs/{run['run_id']}/balance")
    table = r.get_json()
    cu = table["elements"]["Cu"]
    rec = next(p for p in cu["products"] if p["stream_id"] == "conc")
    assert rec["recovery_percent"] == pytest.approx(EXPECTED_RECOVERY, rel=1e-9)
    assert table["max_relative_residual"] < 1e-9


# --------------------------------------------------------------- 版本对比
def test_version_comparison(client):
    tv, v1 = _seed(client)
    # 化验室补送：更正给矿品位 2.0 -> 1.9（新版本）
    data2 = json.loads(json.dumps(DATA))
    for m in data2["measurements"]:
        if m["stream_id"] == "feed" and m["type"] == "grade":
            m["value"] = 1.9
    r = client.post(
        "/api/campaigns/camp1/versions",
        json={"parent_id": v1, "dataset": data2, "note": "给矿品位更正"},
    )
    assert r.status_code == 201
    v2 = r.get_json()["id"]
    assert v2 != v1
    client.post(f"/api/campaign-versions/{v1}/reconcile", json={})
    client.post(f"/api/campaign-versions/{v2}/reconcile", json={})

    r = client.get(f"/api/campaigns/camp1/compare?a={v1}&b={v2}")
    assert r.status_code == 200
    cmp_ = r.get_json()
    feed = next(s for s in cmp_["streams"] if s["stream_id"] == "feed")
    # 给矿品位更正 2.0 -> 1.9（dof=0 时校正值即测量值，差值精确为 -0.1 百分点）
    assert feed["grades"]["Cu"]["delta"] == pytest.approx(-0.1, abs=1e-8)
    # 品位口径是“校正值”，两版不同；回收率字段齐全
    conc = next(s for s in cmp_["streams"] if s["stream_id"] == "conc")
    assert conc["recovery"]["Cu"]["a"] is not None
    assert conc["recovery"]["Cu"]["b"] is not None
    assert conc["flow"]["a"] != pytest.approx(conc["flow"]["b"], rel=1e-6)


# --------------------------------------------------------------- 并发修订冲突
def test_concurrent_revision_conflict(client):
    _, v1 = _seed(client)
    data2 = json.loads(json.dumps(DATA))
    data3 = json.loads(json.dumps(DATA))
    data2["measurements"][0] = {**data2["measurements"][0], "value": 101.0}
    data3["measurements"][1] = {**data3["measurements"][1], "value": 2.05}

    results: list = []

    def revise(payload, idx):
        app = client.application
        c = app.test_client()
        r = c.post(
            "/api/campaigns/camp1/versions",
            json={"parent_id": v1, "dataset": payload, "created_by": f"user{idx}"},
        )
        results.append((idx, r.status_code, r.get_json()))

    t1 = threading.Thread(target=revise, args=(data2, 1))
    t2 = threading.Thread(target=revise, args=(data3, 2))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(s for _, s, _ in results)
    assert statuses == [201, 409], results
    # 后失败的一方拿到冲突信息（当前版本已变）
    conflict = next(b for _, s, b in results if s == 409)
    assert conflict["code"] == "version_conflict"
    assert conflict["details"]["current"] != v1
    # 当前版本只前进了一次
    camp = client.get("/api/campaigns/camp1").get_json()
    versions = client.get("/api/campaigns/camp1/versions").get_json()
    assert len(versions) == 2
    # 失败者基于最新版本重试可成功
    current = camp["current_version_id"]
    retry = client.post(
        "/api/campaigns/camp1/versions",
        json={"parent_id": current, "dataset": data3},
    )
    assert retry.status_code == 201


# --------------------------------------------------------------- 不可观测 422
def test_unobservable_returns_422_with_list(client):
    topo = one_in_two_out_topology()
    data = {
        "measurements": [
            {"stream_id": "feed", "type": "flow", "value": 100.0},
            {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "conc", "type": "grade", "element": "Cu", "value": 1.0},
            {"stream_id": "tail", "type": "grade", "element": "Cu", "value": 1.0},
        ]
    }
    _, vid = _seed(client, topology=topo, dataset=data)
    r = client.post(f"/api/campaign-versions/{vid}/reconcile", json={})
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "unobservable"
    ids = {u["stream_id"] for u in body["details"]["unobservable"]}
    assert {"conc", "tail"} <= ids
    # 显式接受后可以算
    r2 = client.post(
        f"/api/campaign-versions/{vid}/reconcile",
        json={"settings": {"accept_unobservable": True}},
    )
    assert r2.status_code in (200, 201)
    assert r2.get_json()["result"]["observability"]["observable"] is False


# --------------------------------------------------------------- 拓扑版本
def test_topology_versions_are_immutable_and_bound(client):
    tv1, _ = _seed(client, campaign_id="sep", period="2026-09")
    # 拓扑演进：增加一个节点
    topo2 = {
        "nodes": [{"id": "splitter"}, {"id": "cleaner2"}],
        "streams": [
            {"id": "feed", "source": "@env", "target": "splitter", "kind": "feed"},
            {"id": "conc", "source": "splitter", "target": "@env", "kind": "concentrate"},
            {"id": "tail", "source": "splitter", "target": "@env", "kind": "tailings"},
        ],
        "elements": ["Cu"],
    }
    # cleaner2 孤立 -> 拒收，不产生版本
    r = client.post("/api/circuits/circ/topologies", json={"topology": topo2})
    assert r.status_code == 422
    versions = client.get("/api/circuits/circ/topologies").get_json()
    assert len(versions) == 1  # 旧版本不受影响，失败的录入不留痕

    # 新考查声明拓扑新版本
    topo2_valid = dict(topo2)
    topo2_valid["nodes"] = [{"id": "splitter"}]
    r = client.post("/api/circuits/circ/topologies", json={"topology": topo2_valid})
    tv2 = r.get_json()["id"]
    assert tv2 != tv1
    client.post("/api/campaigns", json={
        "id": "oct", "circuit_id": "circ", "name": "10月", "period": "2026-10",
        "topology_version_id": tv2,
    })
    r = client.post("/api/campaigns/oct/versions", json={
        "parent_id": None, "dataset": _oct_data(),
    })
    assert r.status_code == 201
    # 旧考查仍绑定旧拓扑版本
    sep = client.get("/api/campaigns/sep").get_json()
    assert sep["current_topology_id"] == tv1


def _oct_data():
    return handcalc_dataset()


# --------------------------------------------------------------- 回收率走势
def test_recovery_trend_across_months(client):
    # 9 月
    _, v_sep = _seed(client, campaign_id="sep", period="2026-09")
    client.post(f"/api/campaign-versions/{v_sep}/reconcile", json={})
    # 10 月：同拓扑、另一个考查
    client.post("/api/campaigns", json={
        "id": "oct", "circuit_id": "circ", "name": "10月", "period": "2026-10",
        "topology_version_id": client.get("/api/campaigns/sep").get_json()["current_topology_id"],
    })
    r = client.post("/api/campaigns/oct/versions", json={"parent_id": None, "dataset": DATA})
    v_oct = r.get_json()["id"]
    client.post(f"/api/campaign-versions/{v_oct}/reconcile", json={})

    r = client.get("/api/circuits/circ/recovery-trend?product_stream_id=conc")
    assert r.status_code == 200
    series = r.get_json()["series"]["Cu"]
    periods = [p["period"] for p in series]
    assert periods == ["2026-09", "2026-10"]
    assert all(p["recovery_percent"] == pytest.approx(EXPECTED_RECOVERY, rel=1e-9) for p in series)


# --------------------------------------------------------------- 拒收 HTTP 码
def test_rejects_bad_grade_over_http(client):
    client.post("/api/circuits", json={"id": "c2", "name": "x"})
    tv = client.post("/api/circuits/c2/topologies", json={"topology": TOPO}).get_json()["id"]
    client.post("/api/campaigns", json={
        "id": "cp", "circuit_id": "c2", "name": "x",
        "topology_version_id": tv,
    })
    bad = {"measurements": [
        {"stream_id": "feed", "type": "grade", "element": "Cu", "value": 130.0}
    ]}
    r = client.post("/api/campaigns/cp/versions", json={"parent_id": None, "dataset": bad})
    assert r.status_code == 422
    assert r.get_json()["code"] == "grade_out_of_range"


def test_rejects_isolated_topology_over_http(client):
    client.post("/api/circuits", json={"id": "c3", "name": "x"})
    bad_topo = {
        "nodes": [{"id": "a"}, {"id": "lonely"}],
        "streams": [{"id": "f", "source": "@env", "target": "a"}],
        "elements": ["Cu"],
    }
    r = client.post("/api/circuits/c3/topologies", json={"topology": bad_topo})
    assert r.status_code == 422
    assert r.get_json()["code"] == "isolated_node"
