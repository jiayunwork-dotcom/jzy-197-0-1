"""共用夹具：根据 RECON_DB_DSN 选择内存或 PostgreSQL 后端。

默认 memory://，全部用例可在无数据库环境下运行；
设置 RECON_DB_DSN=postgresql://... 后同一套用例在真实 PG16 上再跑一遍。
"""
from __future__ import annotations

import os
import uuid

import pytest

from app.api import create_app
from app.storage import MemoryStore, PostgresStore

DSN = os.environ.get("RECON_DB_DSN", "memory://")


@pytest.fixture(params=[DSN], scope="session")
def backend_dsn(request):
    return request.param


@pytest.fixture()
def store(backend_dsn):
    if backend_dsn == "memory://":
        return MemoryStore()
    return PostgresStore(backend_dsn)


@pytest.fixture()
def app(store):
    return create_app(store)


@pytest.fixture()
def client(app):
    return app.test_client()


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# 标准一进两出拓扑与手算数据
TOPO_1IN2OUT = {
    "elements": ["Cu"],
    "nodes": [{"code": "sep", "name": "粗选"}],
    "streams": [
        {"code": "feed", "source": None, "target": "sep", "role": "feed"},
        {"code": "conc", "source": "sep", "target": None,
         "role": "concentrate"},
        {"code": "tail", "source": "sep", "target": None,
         "role": "tailings"},
    ],
}

# 给矿 100 t/h、2.0%；精矿 20%；尾矿 0.5%；精/尾流量未测
SURVEY_1IN2OUT = [
    {"code": "feed", "flow": {"value": 100.0},
     "grades": [{"element": "Cu", "value": 2.0}]},
    {"code": "conc", "grades": [{"element": "Cu", "value": 20.0}]},
    {"code": "tail", "grades": [{"element": "Cu", "value": 0.5}]},
]


def make_circuit_and_topology(client, topo=None, prefix=None):
    code = unique(prefix or "circ")
    r = client.post("/api/circuits", json={"code": code})
    assert r.status_code == 201, r.get_json()
    r = client.put(f"/api/circuits/{code}/topology",
                   json=topo or TOPO_1IN2OUT)
    assert r.status_code == 201, r.get_json()
    return code


def make_survey(client, circuit_code, streams, month=None,
                topology_version=1):
    if month is None:
        month = f"2026-{(uuid.uuid4().int % 12) + 1:02d}-01"
    r = client.post("/api/surveys", json={
        "circuit_code": circuit_code,
        "topology_version": topology_version,
        "month": month,
        "streams": streams,
    })
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def reconcile(client, survey_id, **kw):
    r = client.post("/api/reconciliations",
                    json={"survey_id": survey_id, **kw})
    return r
