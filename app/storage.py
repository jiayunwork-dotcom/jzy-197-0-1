"""版本存储：回路拓扑版本、考查数据版本、校正结果绑定。

生产用 PostgreSQLStore（psycopg3）；MemoryStore 实现同一接口，仅供无数据库
环境下的测试/本地试跑（DSN=memory://）。

版本语义
--------
- 拓扑在 circuit 下按整数 version 自增，PUT 产生新版本，永不覆盖。
- 考查数据在 survey 下按整数 version 自增；修订带 base_version 做乐观锁，
  与当前最新版本不一致时抛 ConflictError（409）。
- 校正结果绑定 (survey_id, survey_version, topology_version, settings_hash)，
  重复提交直接返回已有结果（幂等）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .errors import ConflictError, NotFoundError
from .jsonutil import to_native

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def settings_hash(settings: dict) -> str:
    """对校正设定做规范化哈希（剔除测量集合、参数、接受不可观测标志等）。"""
    canon = {
        "excluded_measurements": sorted(settings.get("excluded_measurements", [])),
        "max_iterations": settings.get("max_iterations"),
        "residual_tol": settings.get("residual_tol"),
        "accept_unobservable": bool(settings.get("accept_unobservable", False)),
        "significance": settings.get("significance", 0.05),
    }
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


class Store:
    """存储接口（方法签名即契约）。"""

    # --- 回路与拓扑 ---
    def create_circuit(self, code: str, name: str, note: str = "") -> dict: raise NotImplementedError
    def get_circuit(self, code: str) -> dict: raise NotImplementedError
    def list_circuits(self) -> list[dict]: raise NotImplementedError
    def put_topology(self, circuit_code: str, topology: dict) -> dict: raise NotImplementedError
    def get_topology(self, circuit_code: str, version: int) -> dict: raise NotImplementedError
    def list_topology_versions(self, circuit_code: str) -> list[dict]: raise NotImplementedError

    # --- 考查 ---
    def create_survey(self, circuit_code: str, topology_version: int,
                      month: str, name: str, data: dict) -> dict: raise NotImplementedError
    def get_survey(self, survey_id: str) -> dict: raise NotImplementedError
    def list_surveys(self, circuit_code: Optional[str] = None) -> list[dict]: raise NotImplementedError
    def get_survey_version(self, survey_id: str, version: int) -> dict: raise NotImplementedError
    def list_survey_versions(self, survey_id: str) -> list[dict]: raise NotImplementedError
    def revise_survey(self, survey_id: str, base_version: int,
                      data: dict, topology_version: Optional[int] = None,
                      note: str = "") -> dict: raise NotImplementedError

    # --- 校正结果 ---
    def find_run(self, survey_id: str, survey_version: int,
                 topology_version: int, s_hash: str) -> Optional[dict]: raise NotImplementedError
    def save_run(self, run: dict) -> dict: raise NotImplementedError
    def get_run(self, run_id: str) -> dict: raise NotImplementedError
    def list_runs(self, survey_id: Optional[str] = None) -> list[dict]: raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 内存实现
# ---------------------------------------------------------------------------
class MemoryStore(Store):
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.circuits: dict[str, dict] = {}
        self.topologies: dict[str, list[dict]] = {}
        self.surveys: dict[str, dict] = {}
        self.survey_versions: dict[str, list[dict]] = {}
        self.runs: dict[str, dict] = {}

    def create_circuit(self, code, name, note=""):
        with self.lock:
            if code in self.circuits:
                raise ConflictError(f"回路 {code} 已存在", "circuit_exists")
            c = {"code": code, "name": name, "note": note,
                 "created_at": utcnow()}
            self.circuits[code] = c
            return c

    def get_circuit(self, code):
        with self.lock:
            if code not in self.circuits:
                raise NotFoundError(f"回路 {code} 不存在")
            return json.loads(json.dumps(self.circuits[code]))

    def list_circuits(self):
        with self.lock:
            return [json.loads(json.dumps(c)) for c in self.circuits.values()]

    def put_topology(self, circuit_code, topology):
        self.get_circuit(circuit_code)
        with self.lock:
            versions = self.topologies.setdefault(circuit_code, [])
            version = len(versions) + 1
            rec = {"circuit_code": circuit_code, "version": version,
                   "topology": topology, "created_at": utcnow()}
            versions.append(rec)
            return json.loads(json.dumps(rec))

    def get_topology(self, circuit_code, version):
        self.get_circuit(circuit_code)
        with self.lock:
            versions = self.topologies.get(circuit_code, [])
            if not 1 <= version <= len(versions):
                raise NotFoundError(f"拓扑版本 {version} 不存在", "topology_version_not_found")
            return json.loads(json.dumps(versions[version - 1]))

    def list_topology_versions(self, circuit_code):
        self.get_circuit(circuit_code)
        with self.lock:
            return [{"circuit_code": r["circuit_code"], "version": r["version"],
                     "created_at": r["created_at"]}
                    for r in self.topologies.get(circuit_code, [])]

    def create_survey(self, circuit_code, topology_version, month, name, data):
        self.get_topology(circuit_code, topology_version)
        with self.lock:
            if any(s["circuit_code"] == circuit_code and s["month"] == month
                   for s in self.surveys.values()):
                raise ConflictError("该回路此月份的考查已存在", "survey_exists")
            sid = uuid.uuid4().hex
            rec = {"id": sid, "circuit_code": circuit_code,
                   "topology_version": topology_version, "month": month,
                   "name": name, "current_version": 1,
                   "created_at": utcnow()}
            self.surveys[sid] = rec
            v1 = {"survey_id": sid, "version": 1, "topology_version": topology_version,
                  "data": data, "note": "initial", "created_at": utcnow()}
            self.survey_versions[sid] = [v1]
            return json.loads(json.dumps(rec))

    def get_survey(self, survey_id):
        with self.lock:
            if survey_id not in self.surveys:
                raise NotFoundError("考查不存在", "survey_not_found")
            return json.loads(json.dumps(self.surveys[survey_id]))

    def list_surveys(self, circuit_code=None):
        with self.lock:
            out = [s for s in self.surveys.values()
                   if circuit_code is None or s["circuit_code"] == circuit_code]
            return json.loads(json.dumps(sorted(out, key=lambda r: r["created_at"])))

    def get_survey_version(self, survey_id, version):
        self.get_survey(survey_id)
        with self.lock:
            vs = self.survey_versions[survey_id]
            if not 1 <= version <= len(vs):
                raise NotFoundError("考查数据版本不存在", "survey_version_not_found")
            return json.loads(json.dumps(vs[version - 1]))

    def list_survey_versions(self, survey_id):
        self.get_survey(survey_id)
        with self.lock:
            return [{"survey_id": v["survey_id"], "version": v["version"],
                     "topology_version": v["topology_version"],
                     "note": v["note"], "created_at": v["created_at"]}
                    for v in self.survey_versions[survey_id]]

    def revise_survey(self, survey_id, base_version, data,
                      topology_version=None, note=""):
        with self.lock:
            if survey_id not in self.surveys:
                raise NotFoundError("考查不存在", "survey_not_found")
            survey = self.surveys[survey_id]
            if base_version != survey["current_version"]:
                raise ConflictError(
                    "数据已被他人更新，请基于最新版本重新提交",
                    "survey_version_conflict",
                    {"base_version": base_version,
                     "current_version": survey["current_version"]})
            tv = topology_version or survey["topology_version"]
            self.get_topology(survey["circuit_code"], tv)
            new_version = survey["current_version"] + 1
            rec = {"survey_id": survey_id, "version": new_version,
                   "topology_version": tv, "data": data,
                   "note": note or f"v{new_version}", "created_at": utcnow()}
            self.survey_versions[survey_id].append(rec)
            survey["current_version"] = new_version
            if topology_version:
                survey["topology_version"] = topology_version
            return json.loads(json.dumps(rec))

    def find_run(self, survey_id, survey_version, topology_version, s_hash):
        with self.lock:
            for r in self.runs.values():
                if (r["survey_id"] == survey_id
                        and r["survey_version"] == survey_version
                        and r["topology_version"] == topology_version
                        and r["settings_hash"] == s_hash):
                    return json.loads(json.dumps(r))
        return None

    def save_run(self, run):
        with self.lock:
            run = to_native(run)
            rid = run.get("id") or uuid.uuid4().hex
            run = dict(run, id=rid)
            run.setdefault("created_at", utcnow())
            self.runs[rid] = run
            return json.loads(json.dumps(run))

    def get_run(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise NotFoundError("校正结果不存在", "run_not_found")
            return json.loads(json.dumps(self.runs[run_id]))

    def list_runs(self, survey_id=None):
        with self.lock:
            out = [r for r in self.runs.values()
                   if survey_id is None or r["survey_id"] == survey_id]
            return json.loads(json.dumps(sorted(out, key=lambda r: r["created_at"])))


# ---------------------------------------------------------------------------
# PostgreSQL 实现
# ---------------------------------------------------------------------------
class PostgresStore(Store):
    def __init__(self, dsn: str):
        import psycopg
        from psycopg.types.json import Jsonb
        self._psycopg = psycopg
        self.Jsonb = Jsonb
        self.dsn = dsn
        self._init_schema()

    def _conn(self):
        return self._psycopg.connect(self.dsn, autocommit=True)

    def _init_schema(self) -> None:
        with open(SCHEMA_SQL, "r", encoding="utf-8") as f:
            ddl = f.read()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(ddl)

    @staticmethod
    def _j(v):
        import json as _json
        return _json.loads(_json.dumps(v, ensure_ascii=False))

    def create_circuit(self, code, name, note=""):
        import psycopg
        with self._conn() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO circuits(code,name,note) VALUES(%s,%s,%s) "
                    "RETURNING created_at",
                    (code, name, note))
                created_at = cur.fetchone()[0]
            except psycopg.errors.UniqueViolation:
                raise ConflictError(f"回路 {code} 已存在", "circuit_exists")
        return {"code": code, "name": name, "note": note,
                "created_at": created_at.isoformat()}

    def get_circuit(self, code):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code,name,note,created_at FROM circuits WHERE code=%s",
                        (code,))
            row = cur.fetchone()
        if not row:
            raise NotFoundError(f"回路 {code} 不存在")
        return {"code": row[0], "name": row[1], "note": row[2],
                "created_at": row[3].isoformat()}

    def list_circuits(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT code,name,note,created_at FROM circuits ORDER BY code")
            rows = cur.fetchall()
        return [{"code": r[0], "name": r[1], "note": r[2],
                 "created_at": r[3].isoformat()} for r in rows]

    def put_topology(self, circuit_code, topology):
        self.get_circuit(circuit_code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO circuit_versions(circuit_code, topology) "
                "VALUES(%s,%s) RETURNING version, created_at",
                (circuit_code, self.Jsonb(topology)))
            version, created_at = cur.fetchone()
        return {"circuit_code": circuit_code, "version": version,
                "topology": topology, "created_at": created_at.isoformat()}

    def get_topology(self, circuit_code, version):
        self.get_circuit(circuit_code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT topology, created_at FROM circuit_versions "
                "WHERE circuit_code=%s AND version=%s",
                (circuit_code, version))
            row = cur.fetchone()
        if not row:
            raise NotFoundError(f"拓扑版本 {version} 不存在", "topology_version_not_found")
        return {"circuit_code": circuit_code, "version": version,
                "topology": self._j(row[0]), "created_at": row[1].isoformat()}

    def list_topology_versions(self, circuit_code):
        self.get_circuit(circuit_code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version, created_at FROM circuit_versions "
                "WHERE circuit_code=%s ORDER BY version", (circuit_code,))
            rows = cur.fetchall()
        return [{"circuit_code": circuit_code, "version": r[0],
                 "created_at": r[1].isoformat()} for r in rows]

    def create_survey(self, circuit_code, topology_version, month, name, data):
        self.get_topology(circuit_code, topology_version)
        import psycopg
        with self._conn() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO surveys(circuit_code, topology_version, month, name) "
                    "VALUES(%s,%s,%s,%s) RETURNING id, created_at",
                    (circuit_code, topology_version, month, name))
                sid, created_at = cur.fetchone()
            except psycopg.errors.UniqueViolation:
                raise ConflictError("该回路此月份的考查已存在", "survey_exists")
            cur.execute(
                "INSERT INTO survey_versions(survey_id, version, topology_version, "
                "data, note) VALUES(%s,1,%s,%s,'initial')",
                (sid, topology_version, self.Jsonb(data)))
        return {"id": sid.hex, "circuit_code": circuit_code,
                "topology_version": topology_version, "month": month,
                "name": name, "current_version": 1,
                "created_at": created_at.isoformat()}

    def get_survey(self, survey_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, circuit_code, topology_version, month, name, "
                "current_version, created_at FROM surveys WHERE id=%s",
                (uuid.UUID(survey_id),))
            row = cur.fetchone()
        if not row:
            raise NotFoundError("考查不存在", "survey_not_found")
        return {"id": row[0].hex, "circuit_code": row[1],
                "topology_version": row[2], "month": row[3], "name": row[4],
                "current_version": row[5], "created_at": row[6].isoformat()}

    def list_surveys(self, circuit_code=None):
        q = ("SELECT id, circuit_code, topology_version, month, name, "
             "current_version, created_at FROM surveys")
        args: tuple = ()
        if circuit_code:
            q += " WHERE circuit_code=%s"
            args = (circuit_code,)
        q += " ORDER BY month, created_at"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
        return [{"id": r[0].hex, "circuit_code": r[1], "topology_version": r[2],
                 "month": r[3], "name": r[4], "current_version": r[5],
                 "created_at": r[6].isoformat()} for r in rows]

    def get_survey_version(self, survey_id, version):
        self.get_survey(survey_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version, topology_version, data, note, created_at "
                "FROM survey_versions WHERE survey_id=%s AND version=%s",
                (uuid.UUID(survey_id), version))
            row = cur.fetchone()
        if not row:
            raise NotFoundError("考查数据版本不存在", "survey_version_not_found")
        return {"survey_id": survey_id, "version": row[0],
                "topology_version": row[1], "data": self._j(row[2]),
                "note": row[3], "created_at": row[4].isoformat()}

    def list_survey_versions(self, survey_id):
        self.get_survey(survey_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version, topology_version, note, created_at "
                "FROM survey_versions WHERE survey_id=%s ORDER BY version",
                (uuid.UUID(survey_id),))
            rows = cur.fetchall()
        return [{"survey_id": survey_id, "version": r[0],
                 "topology_version": r[1], "note": r[2],
                 "created_at": r[3].isoformat()} for r in rows]

    def revise_survey(self, survey_id, base_version, data,
                      topology_version=None, note=""):
        survey = self.get_survey(survey_id)
        tv = topology_version or survey["topology_version"]
        self.get_topology(survey["circuit_code"], tv)
        # 行级乐观锁：UPDATE 条件里带 current_version=base_version
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_version FROM surveys WHERE id=%s FOR UPDATE",
                        (uuid.UUID(survey_id),))
            current = cur.fetchone()[0]
            if base_version != current:
                raise ConflictError(
                    "数据已被他人更新，请基于最新版本重新提交",
                    "survey_version_conflict",
                    {"base_version": base_version, "current_version": current})
            new_version = current + 1
            cur.execute(
                "UPDATE surveys SET current_version=%s, topology_version=%s "
                "WHERE id=%s AND current_version=%s",
                (new_version, tv, uuid.UUID(survey_id), base_version))
            cur.execute(
                "INSERT INTO survey_versions(survey_id, version, topology_version, "
                "data, note) VALUES(%s,%s,%s,%s,%s) RETURNING created_at",
                (uuid.UUID(survey_id), new_version, tv,
                 self.Jsonb(data), note or f"v{new_version}"))
            created_at = cur.fetchone()[0]
        return {"survey_id": survey_id, "version": new_version,
                "topology_version": tv, "data": self._j(data),
                "note": note, "created_at": created_at.isoformat()}

    def find_run(self, survey_id, survey_version, topology_version, s_hash):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, result FROM reconciliation_runs WHERE survey_id=%s "
                "AND survey_version=%s AND topology_version=%s AND settings_hash=%s",
                (uuid.UUID(survey_id), survey_version, topology_version, s_hash))
            row = cur.fetchone()
        if not row:
            return None
        result = self._j(row[1])
        result["id"] = row[0].hex
        return result

    def save_run(self, run):
        run = to_native(run)
        rid = uuid.UUID(run["id"]) if run.get("id") else uuid.uuid4()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reconciliation_runs("
                "id, survey_id, survey_version, topology_version, settings_hash, "
                "settings, status, result) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (survey_id, survey_version, topology_version, settings_hash) "
                "DO UPDATE SET result=EXCLUDED.result, status=EXCLUDED.status "
                "RETURNING id",
                (rid, uuid.UUID(run["survey_id"]), run["survey_version"],
                 run["topology_version"], run["settings_hash"],
                 self.Jsonb(run["settings"]), run["status"],
                 self.Jsonb(run["result"])))
            got = cur.fetchone()[0]
        return self.get_run(got.hex)

    def get_run(self, run_id):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, survey_id, survey_version, topology_version, "
                "settings_hash, settings, status, result, created_at "
                "FROM reconciliation_runs WHERE id=%s", (uuid.UUID(run_id),))
            row = cur.fetchone()
        if not row:
            raise NotFoundError("校正结果不存在", "run_not_found")
        result = self._j(row[7])
        result["id"] = row[0].hex
        result["survey_id"] = row[1].hex
        result["survey_version"] = row[2]
        result["topology_version"] = row[3]
        result["settings_hash"] = row[4]
        result["settings"] = self._j(row[5])
        result["stored_status"] = row[6]
        result["created_at"] = row[8].isoformat()
        return result

    def list_runs(self, survey_id=None):
        q = ("SELECT id, survey_id, survey_version, topology_version, status, "
             "settings_hash, created_at FROM reconciliation_runs")
        args: tuple = ()
        if survey_id:
            q += " WHERE survey_id=%s"
            args = (uuid.UUID(survey_id),)
        q += " ORDER BY created_at"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
        return [{"id": r[0].hex, "survey_id": r[1].hex, "survey_version": r[2],
                 "topology_version": r[3], "status": r[4],
                 "settings_hash": r[5], "created_at": r[6].isoformat()}
                for r in rows]


def make_store(dsn: Optional[str] = None) -> Store:
    dsn = dsn if dsn is not None else os.environ.get("RECON_DB_DSN", "memory://")
    if dsn == "memory://":
        return MemoryStore()
    return PostgresStore(dsn)
