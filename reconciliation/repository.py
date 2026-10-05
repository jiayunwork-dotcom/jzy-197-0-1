"""仓库抽象与内存实现。

接口与 PostgreSQL 实现（:mod:`storage.postgres`）完全一致，使接口层
可以在没有数据库的环境下做端到端测试，也让领域逻辑不依赖具体存储。

并发修订规则
============
``add_campaign_version`` 是乐观并发控制点：客户端提交修订时必须带
``parent_id``（它所基于的版本）。服务锁定考查行后核对
``parent_id == 当前版本``，不一致即抛 :class:`VersionConflictError`，
后提交方不会静默覆盖先提交方。
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from reconciliation.errors import NotFoundError, VersionConflictError
from reconciliation.service import settings_fingerprint
from reconciliation.solver import SolveSettings


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Repository:
    # ---- circuits ----
    def create_circuit(self, circuit_id: str, name: str) -> dict[str, Any]: ...
    def get_circuit(self, circuit_id: str) -> dict[str, Any]: ...
    def list_circuits(self) -> list[dict[str, Any]]: ...

    # ---- topology versions ----
    def add_topology_version(
        self, circuit_id: str, definition: dict[str, Any], tv_id: str | None = None
    ) -> dict[str, Any]: ...
    def get_topology_version(self, tv_id: str) -> dict[str, Any]: ...
    def list_topology_versions(self, circuit_id: str) -> list[dict[str, Any]]: ...

    # ---- campaigns ----
    def create_campaign(
        self,
        campaign_id: str,
        circuit_id: str,
        name: str,
        period: str | None,
        topology_id: str,
    ) -> dict[str, Any]: ...
    def get_campaign(self, campaign_id: str) -> dict[str, Any]: ...
    def list_campaigns(self, circuit_id: str | None = None) -> list[dict[str, Any]]: ...

    # ---- campaign dataset versions ----
    def add_campaign_version(
        self,
        campaign_id: str,
        dataset: dict[str, Any],
        parent_id: str | None,
        note: str | None = None,
        created_by: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]: ...
    def get_campaign_version(self, version_id: str) -> dict[str, Any]: ...
    def list_campaign_versions(self, campaign_id: str) -> list[dict[str, Any]]: ...

    # ---- reconciliation runs ----
    def get_run(
        self, campaign_version_id: str, settings_hash: str
    ) -> dict[str, Any] | None: ...
    def save_run(
        self,
        campaign_version_id: str,
        settings_hash: str,
        settings_json: dict[str, Any],
        input_hash: str,
        result: dict[str, Any],
        converged: bool,
    ) -> dict[str, Any]: ...
    def list_runs(self, campaign_version_id: str) -> list[dict[str, Any]]: ...


class InMemoryRepository(Repository):
    """线程安全的内存仓库，语义（含冲突检测）与 Postgres 仓库保持一致。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.circuits: dict[str, dict[str, Any]] = {}
        self.topo_versions: dict[str, dict[str, Any]] = {}
        self.campaigns: dict[str, dict[str, Any]] = {}
        self.cv_versions: dict[str, dict[str, Any]] = {}
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}

    # ---- circuits ----
    def create_circuit(self, circuit_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            if circuit_id in self.circuits:
                from reconciliation.errors import DuplicateIdError

                raise DuplicateIdError("回路已存在", {"id": circuit_id})
            row = {"id": circuit_id, "name": name}
            self.circuits[circuit_id] = row
            return dict(row)

    def get_circuit(self, circuit_id: str) -> dict[str, Any]:
        with self._lock:
            if circuit_id not in self.circuits:
                raise NotFoundError("回路不存在", {"id": circuit_id})
            return dict(self.circuits[circuit_id])

    def list_circuits(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(c) for c in self.circuits.values()]

    # ---- topology ----
    def add_topology_version(
        self, circuit_id: str, definition: dict[str, Any], tv_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self.get_circuit(circuit_id)
            existing = [
                t for t in self.topo_versions.values() if t["circuit_id"] == circuit_id
            ]
            version_no = max((t["version_no"] for t in existing), default=0) + 1
            row = {
                "id": tv_id or new_id("tv"),
                "circuit_id": circuit_id,
                "version_no": version_no,
                "definition": json.loads(json.dumps(definition)),
            }
            self.topo_versions[row["id"]] = row
            return json.loads(json.dumps(row))

    def get_topology_version(self, tv_id: str) -> dict[str, Any]:
        with self._lock:
            if tv_id not in self.topo_versions:
                raise NotFoundError("拓扑版本不存在", {"id": tv_id})
            return json.loads(json.dumps(self.topo_versions[tv_id]))

    def list_topology_versions(self, circuit_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(t))
                for t in sorted(
                    (
                        t
                        for t in self.topo_versions.values()
                        if t["circuit_id"] == circuit_id
                    ),
                    key=lambda t: t["version_no"],
                )
            ]

    # ---- campaigns ----
    def create_campaign(
        self,
        campaign_id: str,
        circuit_id: str,
        name: str,
        period: str | None,
        topology_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            self.get_circuit(circuit_id)
            self.get_topology_version(topology_id)
            if campaign_id in self.campaigns:
                from reconciliation.errors import DuplicateIdError

                raise DuplicateIdError("考查已存在", {"id": campaign_id})
            row = {
                "id": campaign_id,
                "circuit_id": circuit_id,
                "name": name,
                "period": period,
                "current_topology_id": topology_id,
                "current_version_id": None,
            }
            self.campaigns[campaign_id] = row
            return dict(row)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self._lock:
            if campaign_id not in self.campaigns:
                raise NotFoundError("考查不存在", {"id": campaign_id})
            return dict(self.campaigns[campaign_id])

    def list_campaigns(self, circuit_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.campaigns.values())
            if circuit_id:
                rows = [c for c in rows if c["circuit_id"] == circuit_id]
            return [dict(c) for c in rows]

    # ---- dataset versions (optimistic concurrency) ----
    def add_campaign_version(
        self,
        campaign_id: str,
        dataset: dict[str, Any],
        parent_id: str | None,
        note: str | None = None,
        created_by: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:  # 相当于 SELECT ... FOR UPDATE
            camp = self.campaigns.get(campaign_id)
            if camp is None:
                raise NotFoundError("考查不存在", {"id": campaign_id})
            current = camp["current_version_id"]
            if current is None:
                if parent_id is not None:
                    raise VersionConflictError(
                        "考查尚无任何版本，首版必须基于 null",
                        {"parent_id": parent_id, "current": None},
                    )
            else:
                if parent_id != current:
                    raise VersionConflictError(
                        "数据已被他人修订：你基于的版本不是当前版本，请拉取最新版本后重试",
                        {"parent_id": parent_id, "current": current},
                    )
            if parent_id is not None and parent_id not in self.cv_versions:
                raise NotFoundError("父版本不存在", {"id": parent_id})

            version_no = (
                max(
                    (
                        v["version_no"]
                        for v in self.cv_versions.values()
                        if v["campaign_id"] == campaign_id
                    ),
                    default=0,
                )
                + 1
            )
            row = {
                "id": cv_id or new_id("cv"),
                "campaign_id": campaign_id,
                "version_no": version_no,
                "parent_id": parent_id,
                "dataset": json.loads(json.dumps(dataset)),
                "note": note,
                "created_by": created_by,
            }
            self.cv_versions[row["id"]] = row
            camp["current_version_id"] = row["id"]
            return json.loads(json.dumps(row))

    def get_campaign_version(self, version_id: str) -> dict[str, Any]:
        with self._lock:
            if version_id not in self.cv_versions:
                raise NotFoundError("考查数据版本不存在", {"id": version_id})
            return json.loads(json.dumps(self.cv_versions[version_id]))

    def list_campaign_versions(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(v))
                for v in sorted(
                    (
                        v
                        for v in self.cv_versions.values()
                        if v["campaign_id"] == campaign_id
                    ),
                    key=lambda v: v["version_no"],
                )
            ]

    # ---- runs ----
    def get_run(
        self, campaign_version_id: str, settings_hash: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.runs.get((campaign_version_id, settings_hash))
            return json.loads(json.dumps(row)) if row else None

    def save_run(
        self,
        campaign_version_id: str,
        settings_hash: str,
        settings_json: dict[str, Any],
        input_hash: str,
        result: dict[str, Any],
        converged: bool,
    ) -> dict[str, Any]:
        with self._lock:
            key = (campaign_version_id, settings_hash)
            existing = self.runs.get(key)
            if existing:
                return json.loads(json.dumps(existing))
            row = {
                "id": new_id("run"),
                "campaign_version_id": campaign_version_id,
                "settings_hash": settings_hash,
                "settings_json": json.loads(json.dumps(settings_json)),
                "input_hash": input_hash,
                "result": json.loads(json.dumps(result)),
                "converged": converged,
                "reused": False,
            }
            self.runs[key] = row
            return json.loads(json.dumps(row))

    def list_runs(self, campaign_version_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(r))
                for r in self.runs.values()
                if r["campaign_version_id"] == campaign_version_id
            ]
