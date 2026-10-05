"""Flask 接口层。

路由总览
========
回路与拓扑
* ``POST /api/circuits``                      建回路
* ``GET  /api/circuits``                      回路列表
* ``GET  /api/circuits/<cid>``                回路详情
* ``POST /api/circuits/<cid>/topologies``     录入/新增拓扑版本（只追加）
* ``GET  /api/circuits/<cid>/topologies``     拓扑版本列表
* ``GET  /api/topologies/<tid>``              指定拓扑版本

考查与数据版本
* ``POST /api/campaigns``                     建考查（声明拓扑版本）
* ``GET  /api/campaigns?circuit_id=``         考查列表
* ``GET  /api/campaigns/<id>``                考查当前指针
* ``POST /api/campaigns/<id>/versions``       提交首版/修订（parent_id 乐观锁）
* ``GET  /api/campaigns/<id>/versions``       版本列表
* ``GET  /api/campaign-versions/<vid>``       指定版本

校正
* ``POST /api/campaign-versions/<vid>/reconcile``  提交校正（幂等：同设定复用）
* ``GET  /api/campaign-versions/<vid>/runs``       该版本的全部校正结果
* ``GET  /api/runs/<rid>``                         单次校正结果
* ``GET  /api/runs/<rid>/balance``                 金属平衡表

分析
* ``GET  /api/campaigns/<id>/compare?a=&b=``       两个数据版本结果对比
* ``GET  /api/circuits/<cid>/recovery-trend``      多月回收率走势
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify, request

from reconciliation.errors import (
    NotFoundError,
    ReconciliationError,
    ValidationError,
)
from reconciliation.repository import InMemoryRepository, Repository
from reconciliation.service import (
    compare_results,
    input_fingerprint,
    recovery_trend,
    run_reconciliation,
    settings_fingerprint,
)
from reconciliation.solver import SolveSettings


def create_app(repo: Repository | None = None) -> Flask:
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    app.config["REPO"] = repo or _default_repo()

    app.json.sort_keys = False

    @app.errorhandler(ReconciliationError)
    def _handle_known(err: ReconciliationError):
        return jsonify(err.to_dict()), err.http_status

    @app.errorhandler(404)
    def _handle_404(err):
        return jsonify({"code": "not_found", "message": "接口不存在", "details": {}}), 404

    @app.errorhandler(Exception)
    def _handle_unexpected(err):
        if isinstance(err, ReconciliationError):
            return jsonify(err.to_dict()), err.http_status
        app.logger.exception("unhandled error")
        return (
            jsonify(
                {
                    "code": "internal_error",
                    "message": f"服务内部错误：{type(err).__name__}: {err}",
                    "details": {},
                }
            ),
            500,
        )

    repo_of = lambda: app.config["REPO"]

    # ----------------------------------------------------------------- circuits
    @app.post("/api/circuits")
    def create_circuit():
        body = request.get_json(force=True, silent=True) or {}
        cid = _require(body, "id")
        name = str(body.get("name") or cid)
        row = repo_of().create_circuit(cid, name)
        return jsonify(row), 201

    @app.get("/api/circuits")
    def list_circuits():
        return jsonify(repo_of().list_circuits())

    @app.get("/api/circuits/<cid>")
    def get_circuit(cid: str):
        return jsonify(repo_of().get_circuit(cid))

    # --------------------------------------------------------- topology versions
    @app.post("/api/circuits/<cid>/topologies")
    def add_topology(cid: str):
        body = request.get_json(force=True, silent=True) or {}
        definition = body.get("topology") or body
        # 先校验（服务层会在解析时抛 422），再持久化，保证库里只有合法拓扑
        from reconciliation.topology import build_topology

        build_topology(definition)
        row = repo_of().add_topology_version(cid, definition)
        return jsonify(row), 201

    @app.get("/api/circuits/<cid>/topologies")
    def list_topologies(cid: str):
        return jsonify(repo_of().list_topology_versions(cid))

    @app.get("/api/topologies/<tid>")
    def get_topology(tid: str):
        return jsonify(repo_of().get_topology_version(tid))

    # ---------------------------------------------------------------- campaigns
    @app.post("/api/campaigns")
    def create_campaign():
        body = request.get_json(force=True, silent=True) or {}
        camp_id = _require(body, "id")
        circuit_id = _require(body, "circuit_id")
        topology_id = _require(body, "topology_version_id")
        row = repo_of().create_campaign(
            camp_id,
            circuit_id,
            str(body.get("name") or camp_id),
            body.get("period"),
            topology_id,
        )
        return jsonify(row), 201

    @app.get("/api/campaigns")
    def list_campaigns():
        return jsonify(repo_of().list_campaigns(request.args.get("circuit_id")))

    @app.get("/api/campaigns/<camp_id>")
    def get_campaign(camp_id: str):
        return jsonify(repo_of().get_campaign(camp_id))

    # --------------------------------------------------- dataset versioning
    @app.post("/api/campaigns/<camp_id>/versions")
    def add_version(camp_id: str):
        body = request.get_json(force=True, silent=True) or {}
        dataset = body.get("dataset")
        if not isinstance(dataset, dict):
            raise ValidationError("请求体必须包含 dataset 对象")
        parent_id = body.get("parent_id", "__missing__")
        if parent_id == "__missing__":
            raise ValidationError("修订必须带 parent_id（首版传 null）")
        # 用考查声明的拓扑版本校验数据，避免存进无法校正的数据
        camp = repo_of().get_campaign(camp_id)
        topo_row = repo_of().get_topology_version(camp["current_topology_id"])
        from reconciliation.measurements import build_dataset
        from reconciliation.topology import build_topology

        topo = build_topology(topo_row["definition"])
        build_dataset(dataset, topo.elements, topo.stream_ids, body.get("default_rsd"))
        row = repo_of().add_campaign_version(
            camp_id,
            dataset,
            parent_id,
            note=body.get("note"),
            created_by=body.get("created_by"),
        )
        return jsonify(row), 201

    @app.get("/api/campaigns/<camp_id>/versions")
    def list_versions(camp_id: str):
        repo_of().get_campaign(camp_id)
        return jsonify(repo_of().list_campaign_versions(camp_id))

    @app.get("/api/campaign-versions/<vid>")
    def get_version(vid: str):
        return jsonify(repo_of().get_campaign_version(vid))

    # ------------------------------------------------------------ reconciliation
    @app.post("/api/campaign-versions/<vid>/reconcile")
    def reconcile_version(vid: str):
        body = request.get_json(force=True, silent=True) or {}
        version = repo_of().get_campaign_version(vid)
        camp = repo_of().get_campaign(version["campaign_id"])
        topo_row = repo_of().get_topology_version(camp["current_topology_id"])
        topology_raw = topo_row["definition"]

        settings = SolveSettings.from_dict(body.get("settings"))
        s_hash = settings_fingerprint(settings)
        ihash = input_fingerprint(topology_raw, version["dataset"])

        existing = repo_of().get_run(vid, s_hash)
        if existing:
            existing["reused"] = True
            return jsonify(_run_payload(vid, existing, version, camp, topo_row)), 200

        # 求解（UnobservableError 会冒泡成 422 并列出不可观测变量）
        result = run_reconciliation(topology_raw, version["dataset"], body.get("settings"))
        # 内部大矩阵不入库，保持结果 JSON 精炼且可移植
        public_result = {k: v for k, v in result.items() if k != "_internal"}
        row = repo_of().save_run(
            vid, s_hash, dict(body.get("settings") or {}), ihash,
            public_result, result["converged"],
        )
        payload = _run_payload(vid, row, version, camp, topo_row)
        payload["reused"] = False
        return jsonify(payload), 201

    @app.get("/api/campaign-versions/<vid>/runs")
    def list_runs(vid: str):
        repo_of().get_campaign_version(vid)
        return jsonify(repo_of().list_runs(vid))

    @app.get("/api/runs/<rid>")
    def get_run(rid: str):
        # 简单实现：全库扫描（考查规模小）；结果里已带 run id
        for camp in repo_of().list_campaigns():
            for ver in repo_of().list_campaign_versions(camp["id"]):
                for run in repo_of().list_runs(ver["id"]):
                    if run["id"] == rid:
                        return jsonify(run)
        raise NotFoundError("校正结果不存在", {"id": rid})

    @app.get("/api/runs/<rid>/balance")
    def get_balance(rid: str):
        for camp in repo_of().list_campaigns():
            for ver in repo_of().list_campaign_versions(camp["id"]):
                for run in repo_of().list_runs(ver["id"]):
                    if run["id"] == rid:
                        return jsonify(run["result"]["balance_table"])
        raise NotFoundError("校正结果不存在", {"id": rid})

    # ----------------------------------------------------------------- analysis
    @app.get("/api/campaigns/<camp_id>/compare")
    def compare(camp_id: str):
        a_ref = request.args.get("a")
        b_ref = request.args.get("b")
        if not a_ref or not b_ref:
            raise ValidationError(
                "对比必须提供 a 与 b：数据版本 id（cv_…）或校正结果 id（run_…）"
            )
        camp = repo_of().get_campaign(camp_id)
        topo_row = repo_of().get_topology_version(camp["current_topology_id"])
        ra = _resolve_result(repo_of(), a_ref)
        rb = _resolve_result(repo_of(), b_ref)
        out = compare_results(
            topo_row["definition"], ra["result"], rb["result"],
            label_a=a_ref, label_b=b_ref,
        )
        out["campaign_id"] = camp_id
        out["topology_version_id"] = topo_row["id"]
        return jsonify(out)

    @app.get("/api/circuits/<cid>/recovery-trend")
    def trend(cid: str):
        element = request.args.get("element")
        product = request.args.get("product_stream_id")
        campaigns = repo_of().list_campaigns(cid)
        points = []
        for camp in campaigns:
            if not camp.get("current_version_id"):
                continue
            topo_row = repo_of().get_topology_version(camp["current_topology_id"])
            run = _latest_result(
                repo_of(), camp["current_version_id"], required=False
            )
            if run is None:
                continue
            points.append(
                {
                    "campaign_id": camp["id"],
                    "period": camp.get("period"),
                    "topology_raw": topo_row["definition"],
                    "result": run["result"],
                }
            )
        out = recovery_trend(points, product_stream_id=product)
        out["circuit_id"] = cid
        if element:
            out["series"] = {element: out["series"].get(element, [])} if element in out["series"] else {element: []}
        return jsonify(out)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


# --------------------------------------------------------------------- helpers

def _latest_result(repo: Repository, version_id: str, required: bool = True):
    runs = repo.list_runs(version_id)
    if not runs:
        if required:
            raise NotFoundError(
                "该数据版本尚无校正结果，请先提交校正", {"version_id": version_id}
            )
        return None
    return runs[-1]


def _resolve_result(repo: Repository, ref: str):
    """引用既可能是数据版本 id（cv_…，取其最近一次校正），也可能是 run id。"""
    if ref.startswith("run_"):
        for camp in repo.list_campaigns():
            for ver in repo.list_campaign_versions(camp["id"]):
                for run in repo.list_runs(ver["id"]):
                    if run["id"] == ref:
                        return run
        raise NotFoundError("校正结果不存在", {"ref": ref})
    return _latest_result(repo, ref)


def _run_payload(vid: str, row: dict[str, Any], version: dict[str, Any],
                 camp: dict[str, Any], topo_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["id"],
        "campaign_id": camp["id"],
        "campaign_version_id": vid,
        "version_no": version["version_no"],
        "topology_version_id": topo_row["id"],
        "settings_hash": row["settings_hash"],
        "input_hash": row["input_hash"],
        "converged": row["result"]["converged"],
        "reused": row.get("reused", True),
        "result": row["result"],
    }


def _require(body: dict[str, Any], key: str) -> str:
    val = body.get(key)
    if val is None or str(val) == "":
        raise ValidationError(f"缺少必填字段 {key}", {"field": key})
    return str(val)


def _default_repo() -> Repository:
    backend = os.environ.get("RECON_STORAGE", "memory")
    if backend == "postgres":
        from storage.postgres import PostgresRepository

        return PostgresRepository()
    return InMemoryRepository()
