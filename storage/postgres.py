"""PostgreSQL 16 仓库实现（psycopg 3）。

每个请求从连接池借一条连接。修订版本时对考查行 ``SELECT ... FOR UPDATE``
加行级悲观锁（与乐观父版本校验配合），保证两人并发修订时第二个事务
看到第一个事务提交后的新当前版本，从而必然收到 409，绝不静默覆盖。

校正结果的幂等靠 ``(campaign_version_id, settings_hash)`` 唯一索引；
并发提交相同设定时，先插入者成功，后插入者捕获唯一冲突后回读已有结果。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from reconciliation.errors import DuplicateIdError, NotFoundError, VersionConflictError
from reconciliation.repository import Repository, new_id

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class PostgresRepository(Repository):
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get(
            "DATABASE_URL",
            "postgresql://recon:recon@db:5432/reconciliation",
        )

    def _conn(self):
        import psycopg  # 延迟导入，无数据库环境下也能导入整个包

        return psycopg.connect(self.dsn, autocommit=False)

    def init_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

    # ---- circuits ----
    def create_circuit(self, circuit_id: str, name: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO circuits(id, name) VALUES (%s, %s) RETURNING id, name",
                    (circuit_id, name),
                )
            except Exception as exc:  # psycopg.errors.UniqueViolation 等
                conn.rollback()
                if _is_unique_violation(exc):
                    raise DuplicateIdError("回路已存在", {"id": circuit_id}) from exc
                raise
            row = cur.fetchone()
            conn.commit()
            return {"id": row[0], "name": row[1]}

    def get_circuit(self, circuit_id: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM circuits WHERE id=%s", (circuit_id,))
            row = cur.fetchone()
            if not row:
                raise NotFoundError("回路不存在", {"id": circuit_id})
            return {"id": row[0], "name": row[1]}

    def list_circuits(self) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM circuits ORDER BY id")
            return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

    # ---- topology versions ----
    def add_topology_version(
        self, circuit_id: str, definition: dict[str, Any], tv_id: str | None = None
    ) -> dict[str, Any]:
        tv_id = tv_id or new_id("tv")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM circuits WHERE id=%s", (circuit_id,))
            if not cur.fetchone():
                raise NotFoundError("回路不存在", {"id": circuit_id})
            cur.execute(
                """
                INSERT INTO topology_versions(id, circuit_id, version_no, definition)
                VALUES (%s, %s,
                        COALESCE((SELECT max(version_no)+1 FROM topology_versions
                                  WHERE circuit_id=%s), 1),
                        %s::jsonb)
                RETURNING version_no
                """,
                (tv_id, circuit_id, circuit_id, json.dumps(definition)),
            )
            version_no = cur.fetchone()[0]
            conn.commit()
        return {
            "id": tv_id,
            "circuit_id": circuit_id,
            "version_no": version_no,
            "definition": definition,
        }

    def get_topology_version(self, tv_id: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, circuit_id, version_no, definition FROM topology_versions WHERE id=%s",
                (tv_id,),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError("拓扑版本不存在", {"id": tv_id})
            return {
                "id": row[0],
                "circuit_id": row[1],
                "version_no": row[2],
                "definition": row[3] if isinstance(row[3], dict) else json.loads(row[3]),
            }

    def list_topology_versions(self, circuit_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, version_no, definition FROM topology_versions "
                "WHERE circuit_id=%s ORDER BY version_no",
                (circuit_id,),
            )
            return [
                {"id": r[0], "version_no": r[1],
                 "definition": r[2] if isinstance(r[2], dict) else json.loads(r[2])}
                for r in cur.fetchall()
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
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM circuits WHERE id=%s", (circuit_id,))
            if not cur.fetchone():
                raise NotFoundError("回路不存在", {"id": circuit_id})
            cur.execute("SELECT 1 FROM topology_versions WHERE id=%s", (topology_id,))
            if not cur.fetchone():
                raise NotFoundError("拓扑版本不存在", {"id": topology_id})
            try:
                cur.execute(
                    "INSERT INTO campaigns(id, circuit_id, name, period, current_topology_id)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (campaign_id, circuit_id, name, period, topology_id),
                )
            except Exception as exc:
                conn.rollback()
                if _is_unique_violation(exc):
                    raise DuplicateIdError("考查已存在", {"id": campaign_id}) from exc
                raise
            conn.commit()
        return {
            "id": campaign_id,
            "circuit_id": circuit_id,
            "name": name,
            "period": period,
            "current_topology_id": topology_id,
            "current_version_id": None,
        }

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, circuit_id, name, period, current_topology_id, current_version_id"
                " FROM campaigns WHERE id=%s",
                (campaign_id,),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError("考查不存在", {"id": campaign_id})
            return {
                "id": row[0], "circuit_id": row[1], "name": row[2],
                "period": row[3], "current_topology_id": row[4],
                "current_version_id": row[5],
            }

    def list_campaigns(self, circuit_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            if circuit_id:
                cur.execute(
                    "SELECT id, circuit_id, name, period, current_topology_id, current_version_id"
                    " FROM campaigns WHERE circuit_id=%s ORDER BY period NULLS LAST, id",
                    (circuit_id,),
                )
            else:
                cur.execute(
                    "SELECT id, circuit_id, name, period, current_topology_id, current_version_id"
                    " FROM campaigns ORDER BY period NULLS LAST, id"
                )
            return [
                {"id": r[0], "circuit_id": r[1], "name": r[2], "period": r[3],
                 "current_topology_id": r[4], "current_version_id": r[5]}
                for r in cur.fetchall()
            ]

    # ---- dataset versions ----
    def add_campaign_version(
        self,
        campaign_id: str,
        dataset: dict[str, Any],
        parent_id: str | None,
        note: str | None = None,
        created_by: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]:
        cv_id = cv_id or new_id("cv")
        with self._conn() as conn, conn.cursor() as cur:
            # 行级锁 + 乐观父版本校验
            cur.execute(
                "SELECT current_version_id FROM campaigns WHERE id=%s FOR UPDATE",
                (campaign_id,),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError("考查不存在", {"id": campaign_id})
            current = row[0]
            if (current is None and parent_id is not None) or (
                current is not None and parent_id != current
            ):
                raise VersionConflictError(
                    "数据已被他人修订：你基于的版本不是当前版本，请拉取最新版本后重试",
                    {"parent_id": parent_id, "current": current},
                )
            if parent_id is not None:
                cur.execute("SELECT 1 FROM campaign_versions WHERE id=%s", (parent_id,))
                if not cur.fetchone():
                    raise NotFoundError("父版本不存在", {"id": parent_id})
            cur.execute(
                """
                INSERT INTO campaign_versions
                    (id, campaign_id, version_no, parent_id, dataset, note, created_by)
                VALUES (%s, %s,
                        COALESCE((SELECT max(version_no)+1 FROM campaign_versions
                                  WHERE campaign_id=%s), 1),
                        %s, %s::jsonb, %s, %s)
                RETURNING version_no
                """,
                (cv_id, campaign_id, campaign_id, parent_id,
                 json.dumps(dataset), note, created_by),
            )
            version_no = cur.fetchone()[0]
            cur.execute(
                "UPDATE campaigns SET current_version_id=%s WHERE id=%s",
                (cv_id, campaign_id),
            )
            conn.commit()
        return {
            "id": cv_id,
            "campaign_id": campaign_id,
            "version_no": version_no,
            "parent_id": parent_id,
            "dataset": dataset,
            "note": note,
            "created_by": created_by,
        }

    def get_campaign_version(self, version_id: str) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, campaign_id, version_no, parent_id, dataset, note, created_by"
                " FROM campaign_versions WHERE id=%s",
                (version_id,),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError("考查数据版本不存在", {"id": version_id})
            ds = row[4]
            return {
                "id": row[0], "campaign_id": row[1], "version_no": row[2],
                "parent_id": row[3],
                "dataset": ds if isinstance(ds, dict) else json.loads(ds),
                "note": row[5], "created_by": row[6],
            }

    def list_campaign_versions(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, version_no, parent_id, dataset FROM campaign_versions"
                " WHERE campaign_id=%s ORDER BY version_no",
                (campaign_id,),
            )
            return [
                {"id": r[0], "version_no": r[1], "parent_id": r[2],
                 "dataset": r[3] if isinstance(r[3], dict) else json.loads(r[3])}
                for r in cur.fetchall()
            ]

    # ---- runs ----
    def get_run(
        self, campaign_version_id: str, settings_hash: str
    ) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, settings_json, input_hash, result, converged "
                "FROM recon_runs WHERE campaign_version_id=%s AND settings_hash=%s",
                (campaign_version_id, settings_hash),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "campaign_version_id": campaign_version_id,
                "settings_hash": settings_hash,
                "settings_json": _as_obj(row[1]),
                "input_hash": row[2],
                "result": _as_obj(row[3]),
                "converged": row[4],
                "reused": True,
            }

    def save_run(
        self,
        campaign_version_id: str,
        settings_hash: str,
        settings_json: dict[str, Any],
        input_hash: str,
        result: dict[str, Any],
        converged: bool,
    ) -> dict[str, Any]:
        with self._conn() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO recon_runs(id, campaign_version_id, settings_hash,"
                    " settings_json, input_hash, result, converged)"
                    " VALUES (%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s) RETURNING id",
                    (new_id("run"), campaign_version_id, settings_hash,
                     json.dumps(settings_json), input_hash, json.dumps(result), converged),
                )
                run_id = cur.fetchone()[0]
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if _is_unique_violation(exc):
                    existing = self.get_run(campaign_version_id, settings_hash)
                    existing["reused"] = True
                    return existing
                raise
        return {
            "id": run_id,
            "campaign_version_id": campaign_version_id,
            "settings_hash": settings_hash,
            "settings_json": settings_json,
            "input_hash": input_hash,
            "result": result,
            "converged": converged,
            "reused": False,
        }

    def list_runs(self, campaign_version_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, settings_hash, result, converged FROM recon_runs"
                " WHERE campaign_version_id=%s ORDER BY created_at",
                (campaign_version_id,),
            )
            return [
                {"id": r[0], "settings_hash": r[1],
                 "result": _as_obj(r[2]), "converged": r[3]}
                for r in cur.fetchall()
            ]


def _as_obj(v: Any) -> Any:
    return v if isinstance(v, (dict, list)) else json.loads(v)


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ in ("UniqueViolation",) or "unique constraint" in str(exc).lower()
