"""PostgreSQL 仓库集成测试。

仅当环境变量 ``DATABASE_URL`` 可连接时运行（``docker compose`` 环境自带）；
本地没有数据库时自动跳过，核心行为已由内存仓库的接口测试覆盖。

运行：
    docker compose run --rm tests
或：
    DATABASE_URL=postgresql://... pytest tests/test_postgres_integration.py
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from reconciliation.errors import VersionConflictError
from reconciliation.repository import InMemoryRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_PG_TESTS"), reason="设置 RUN_PG_TESTS=1 且 DATABASE_URL 可达时运行"
)


@pytest.fixture
def repo():
    from storage.postgres import PostgresRepository

    r = PostgresRepository()
    try:
        r.init_schema()
    except Exception as exc:  # 连不上就跳过
        pytest.skip(f"PostgreSQL 不可用：{exc}")
    suffix = uuid.uuid4().hex[:8]
    cid = f"c_{suffix}"
    r.create_circuit(cid, "测试回路")
    topo = {
        "nodes": [{"id": "n"}],
        "streams": [
            {"id": "f", "source": "@env", "target": "n"},
            {"id": "c", "source": "n", "target": "@env"},
            {"id": "t", "source": "n", "target": "@env"},
        ],
        "elements": ["Cu"],
    }
    tv = r.add_topology_version(cid, topo)
    camp_id = f"camp_{suffix}"
    r.create_campaign(camp_id, cid, "考查", "2026-09", tv["id"])
    yield r, camp_id


def test_versioning_and_conflict(repo):
    r, camp_id = repo
    d1 = {"measurements": [{"stream_id": "f", "type": "flow", "value": 100.0}]}
    v1 = r.add_campaign_version(camp_id, d1, None)
    assert v1["version_no"] == 1
    v2 = r.add_campaign_version(camp_id, d1, v1["id"])
    assert v2["parent_id"] == v1["id"]

    # 基于旧 v1 再提交 -> 409
    with pytest.raises(VersionConflictError):
        r.add_campaign_version(camp_id, d1, v1["id"])


def test_run_idempotency(repo):
    r, camp_id = repo
    d1 = {"measurements": [{"stream_id": "f", "type": "flow", "value": 100.0}]}
    v1 = r.add_campaign_version(camp_id, d1, None)
    result = {"converged": True, "x": 1}
    a = r.save_run(v1["id"], "h1", {}, "in", result, True)
    b = r.save_run(v1["id"], "h1", {}, "in", result, True)
    assert a["id"] == b["id"]
    assert r.get_run(v1["id"], "h1")["result"] == result
