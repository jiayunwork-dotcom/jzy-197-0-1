"""Flask 接口：拓扑录入与版本、考查录入与修订、提交校正、结果/平衡表、
版本对比、回收率走势。
"""
from __future__ import annotations

import os
from datetime import date
from typing import Optional

from flask import Flask, g, jsonify, request

from .errors import DomainError, NotFoundError, UnobservableError
from .gross_error import gross_error_test_named
from .measurement_model import (SurveyData, validate_survey)
from .reconciliation import (DEFAULT_MAX_ITER, RESIDUAL_TOL,
                             run_reconciliation)
from .reporting import build_result_bundle
from .storage import Store, make_store, settings_hash
from .topology import CircuitTopology, incidence_matrix, validate_topology


def create_app(store: Optional[Store] = None) -> Flask:
    app = Flask(__name__)
    app.config["store_dsn"] = os.environ.get("RECON_DB_DSN", "memory://")
    app.url_map.strict_slashes = False

    def get_store() -> Store:
        if store is not None:
            return store
        if "store" not in g:
            g.store = make_store(app.config["store_dsn"])
        return g.store

    @app.errorhandler(DomainError)
    def handle_domain(err: DomainError):
        return jsonify(err.to_dict()), err.http_status

    @app.errorhandler(404)
    def handle_404(_e):
        return jsonify({"error": {"code": "not_found", "message": "接口不存在"}}), 404

    def body() -> dict:
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            raise DomainError("请求体必须是 JSON 对象", "bad_json")
        return d

    def load_topo(circuit_code: str, version: int) -> CircuitTopology:
        rec = get_store().get_topology(circuit_code, version)
        t = CircuitTopology.from_dict(
            {**rec["topology"], "circuit_code": circuit_code,
             "version": version, "created_at": rec["created_at"]})
        validate_topology(t)
        return t

    def load_survey_data(survey, version: int):
        vrec = get_store().get_survey_version(survey["id"], version)
        data = SurveyData.from_dict(vrec["data"])
        return vrec, data

    # --- 健康检查 ------------------------------------------------------------
    @app.get("/api/health")
    def health():
        backend = "memory" if app.config["store_dsn"] == "memory://" else "postgresql"
        return jsonify({"status": "ok", "backend": backend})

    # --- 回路与拓扑 ----------------------------------------------------------
    @app.post("/api/circuits")
    def create_circuit():
        d = body()
        code = d.get("code")
        if not code or not str(code).strip():
            raise DomainError("回路编码 code 必填", "missing_field")
        rec = get_store().create_circuit(str(code), str(d.get("name", "")),
                                         str(d.get("note", "")))
        return jsonify(rec), 201

    @app.get("/api/circuits")
    def list_circuits():
        return jsonify(get_store().list_circuits())

    @app.get("/api/circuits/<circuit_code>")
    def get_circuit(circuit_code):
        return jsonify(get_store().get_circuit(circuit_code))

    @app.put("/api/circuits/<circuit_code>/topology")
    def put_topology(circuit_code):
        d = body()
        get_store().get_circuit(circuit_code)
        topo_d = {"circuit_code": circuit_code,
                  "nodes": d.get("nodes", []),
                  "streams": d.get("streams", []),
                  "elements": d.get("elements", []),
                  "note": d.get("note", "")}
        topo = CircuitTopology.from_dict({**topo_d, "version": 0})
        validate_topology(topo)
        rec = get_store().put_topology(circuit_code, topo_d)
        return jsonify({"circuit_code": circuit_code,
                        "version": rec["version"],
                        "created_at": rec["created_at"]}), 201

    @app.get("/api/circuits/<circuit_code>/topology")
    def latest_topology(circuit_code):
        versions = get_store().list_topology_versions(circuit_code)
        if not versions:
            raise NotFoundError("该回路还没有拓扑版本", "topology_not_found")
        return jsonify(get_store().get_topology(circuit_code,
                                                versions[-1]["version"]))

    @app.get("/api/circuits/<circuit_code>/topology/<int:version>")
    def get_topology(circuit_code, version):
        return jsonify(get_store().get_topology(circuit_code, version))

    # --- 考查 ---------------------------------------------------------------
    @app.post("/api/surveys")
    def create_survey():
        d = body()
        required = ("circuit_code", "topology_version", "month")
        for k in required:
            if d.get(k) is None:
                raise DomainError(f"字段 {k} 必填", "missing_field")
        month = _check_month(str(d["month"]))
        # 先校验拓扑/数据，再落库
        topo = load_topo(str(d["circuit_code"]), int(d["topology_version"]))
        data = SurveyData.from_dict({"streams": d.get("streams", []),
                                     "note": d.get("data_note", "")})
        validate_survey(topo, data)
        rec = get_store().create_survey(
            str(d["circuit_code"]), int(d["topology_version"]), month,
            str(d.get("name", "")), data.to_dict())
        return jsonify(rec), 201

    @app.get("/api/surveys")
    def list_surveys():
        return jsonify(get_store().list_surveys(request.args.get("circuit_code")))

    @app.get("/api/surveys/<survey_id>")
    def get_survey(survey_id):
        s = get_store().get_survey(survey_id)
        s["etag"] = str(s["current_version"])
        return jsonify(s)

    @app.get("/api/surveys/<survey_id>/versions")
    def list_survey_versions(survey_id):
        return jsonify(get_store().list_survey_versions(survey_id))

    @app.get("/api/surveys/<survey_id>/versions/<int:version>")
    def get_survey_version(survey_id, version):
        return jsonify(get_store().get_survey_version(survey_id, version))

    @app.put("/api/surveys/<survey_id>")
    def revise_survey(survey_id):
        d = body()
        if d.get("base_version") is None:
            raise DomainError("修订必须带 base_version（乐观锁）", "missing_base_version")
        store = get_store()
        survey = store.get_survey(survey_id)
        new_tv = int(d["topology_version"]) if d.get("topology_version") else None
        tv = new_tv or survey["topology_version"]
        topo = load_topo(survey["circuit_code"], tv)
        data = SurveyData.from_dict({"streams": d.get("streams", []),
                                     "note": d.get("data_note", "")})
        validate_survey(topo, data)
        rec = store.revise_survey(survey_id, int(d["base_version"]),
                                  data.to_dict(), topology_version=new_tv,
                                  note=str(d.get("note", "")))
        return jsonify(rec), 201

    # --- 提交校正 -----------------------------------------------------------
    @app.post("/api/reconciliations")
    def submit_reconciliation():
        d = body()
        sid = d.get("survey_id")
        if not sid:
            raise DomainError("survey_id 必填", "missing_field")
        store = get_store()
        survey = store.get_survey(str(sid))
        sv = int(d["survey_version"]) if d.get("survey_version") \
            else survey["current_version"]
        vrec, sdata = load_survey_data(survey, sv)
        topo = load_topo(survey["circuit_code"], vrec["topology_version"])
        validate_survey(topo, sdata)

        excluded = frozenset(d.get("excluded_measurements", []))
        settings = {
            "excluded_measurements": sorted(excluded),
            "max_iterations": int(d.get("max_iterations", DEFAULT_MAX_ITER)),
            "residual_tol": float(d.get("residual_tol", RESIDUAL_TOL)),
            "accept_unobservable": bool(d.get("accept_unobservable", False)),
            "significance": float(d.get("significance", 0.05)),
        }
        if settings["max_iterations"] < 1:
            raise DomainError("max_iterations 必须 >= 1", "bad_setting")
        if not 0.0 < settings["significance"] < 1.0:
            raise DomainError("significance 必须在 (0,1) 内", "bad_setting")
        shash = settings_hash(settings)

        existing = store.find_run(survey["id"], sv, vrec["topology_version"], shash)
        cached = existing is not None
        if existing is None:
            a = incidence_matrix(topo)
            res, model = run_reconciliation(
                topo, sdata, excluded=excluded,
                max_iterations=settings["max_iterations"],
                residual_tol=settings["residual_tol"],
                accept_unobservable=settings["accept_unobservable"])
            if res.reason == "unobservable" \
                    and not settings["accept_unobservable"]:
                raise UnobservableError(
                    "存在不能由守恒关系唯一确定的未测变量，已拒绝出数；"
                    "如确认接受不可观测结果，请带 accept_unobservable=true 重提",
                    {"unobservable": res.observability["unobservable"],
                     "rank": res.observability["rank"],
                     "n_unmeasured": res.observability["n_unmeasured"],
                     "nullspace_directions":
                         res.observability["nullspace_directions"]})
            bundle = build_result_bundle(topo, model, a, res, settings)
            bundle["gross_error"] = gross_error_test_named(
                model, a, res, sorted(topo.node_codes()),
                significance=settings["significance"])
            run_id = os.urandom(16).hex()
            existing = store.save_run({
                "id": run_id,
                "survey_id": survey["id"],
                "survey_version": sv,
                "topology_version": vrec["topology_version"],
                "settings_hash": shash,
                "settings": settings,
                "status": res.status,
                "result": bundle,
            })
        existing["cached"] = cached
        return jsonify(existing), 200 if cached else 201

    @app.get("/api/reconciliations/<run_id>")
    def get_run(run_id):
        return jsonify(get_store().get_run(run_id))

    @app.get("/api/reconciliations/<run_id>/balance-sheet")
    def balance_sheet(run_id):
        run = get_store().get_run(run_id)
        result = run["result"]
        return jsonify({
            "run_id": run["id"],
            "status": result["status"],
            "node_balance": result["node_balance"],
            "residuals_summary": result["residuals_summary"],
            "streams": result["streams"],
        })

    @app.get("/api/reconciliations")
    def list_runs():
        return jsonify(get_store().list_runs(request.args.get("survey_id")))

    # --- 版本对比 -----------------------------------------------------------
    @app.get("/api/surveys/<survey_id>/compare")
    def compare_versions(survey_id):
        store = get_store()
        survey = store.get_survey(survey_id)
        try:
            v1 = int(request.args["v1"])
            v2 = int(request.args["v2"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("需要整数查询参数 v1 和 v2", "bad_query")
        shash1 = request.args.get("settings_hash_1")
        shash2 = request.args.get("settings_hash_2")
        r1 = _latest_or_hashed_run(store, survey["id"], v1, shash1)
        r2 = _latest_or_hashed_run(store, survey["id"], v2, shash2)
        return jsonify(_compare_runs(survey_id, v1, r1, v2, r2))

    # --- 回收率走势 ---------------------------------------------------------
    @app.get("/api/surveys/recovery-trend")
    def recovery_trend_global():
        circuit_code = request.args.get("circuit_code")
        months = request.args.get("months")
        limit = int(months) if months else None
        return jsonify(_recovery_trend(get_store(), circuit_code=circuit_code,
                                       limit=limit))

    @app.get("/api/surveys/<survey_id>/recovery-trend")
    def recovery_trend_for_survey(survey_id):
        store = get_store()
        survey = store.get_survey(survey_id)
        months = request.args.get("months")
        limit = int(months) if months else None
        return jsonify(_recovery_trend(store,
                                       circuit_code=survey["circuit_code"],
                                       limit=limit))

    return app


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _check_month(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise DomainError("month 必须是 YYYY-MM-DD 日期（一般取每月1日）",
                          "bad_month")
    return value


def _latest_or_hashed_run(store: Store, survey_id: str, version: int,
                          shash: Optional[str]) -> dict:
    runs = [r for r in store.list_runs(survey_id)
            if r["survey_version"] == version]
    if shash:
        runs = [r for r in runs if r["settings_hash"] == shash]
    if not runs:
        raise NotFoundError(f"数据版本 {version} 还没有校正结果",
                            "run_not_found")
    return store.get_run(runs[-1]["id"])


def _compare_runs(survey_id, v1, r1, v2, r2) -> dict:
    s1 = {row["stream"]: row for row in r1["result"]["streams"]}
    s2 = {row["stream"]: row for row in r2["result"]["streams"]}
    rec1 = r1["result"]["recoveries"]["elements"]
    rec2 = r2["result"]["recoveries"]["elements"]
    streams = []
    for code in sorted(set(s1) | set(s2)):
        a, b = s1.get(code), s2.get(code)
        row = {"stream": code}
        if a and b:
            row["flow_v1"] = a["dry_ore_flow"]
            row["flow_v2"] = b["dry_ore_flow"]
            row["flow_delta"] = b["dry_ore_flow"] - a["dry_ore_flow"]
            grades = {}
            for e in set(a["grades_percent"]) | set(b["grades_percent"]):
                ga, gb = a["grades_percent"].get(e), b["grades_percent"].get(e)
                grades[e] = {"v1": ga, "v2": gb,
                             "delta": (gb - ga) if ga is not None and gb is not None else None}
            row["grades_percent"] = grades
        else:
            row["present_in"] = "v1" if a else "v2"
        streams.append(row)
    recoveries = {}
    for e in sorted(set(rec1) | set(rec2)):
        a = rec1.get(e, {}).get("recovery_percent")
        b = rec2.get(e, {}).get("recovery_percent")
        recoveries[e] = {"v1": a, "v2": b,
                         "delta_points": (b - a)
                         if a is not None and b is not None else None}
    return {"survey_id": survey_id,
            "v1": {"version": v1, "run_id": r1["id"],
                   "status": r1["result"]["status"]},
            "v2": {"version": v2, "run_id": r2["id"],
                   "status": r2["result"]["status"]},
            "streams": streams,
            "recovery_percent": recoveries}


def _recovery_trend(store: Store, circuit_code: Optional[str] = None,
                    limit: Optional[int] = None) -> dict:
    surveys = store.list_surveys(circuit_code)
    points = []
    for s in surveys:
        runs = [r for r in store.list_runs(s["id"])
                if r["survey_version"] == s["current_version"]]
        if not runs:
            continue
        run = store.get_run(runs[-1]["id"])
        result = run["result"]
        if result["status"] != "converged":
            continue
        rec = {e: v["recovery_percent"]
               for e, v in result["recoveries"]["elements"].items()}
        points.append({"survey_id": s["id"], "month": s["month"],
                       "name": s["name"], "run_id": run["id"],
                       "topology_version": s["topology_version"],
                       "recovery_percent": rec})
    points.sort(key=lambda p: p["month"])
    if limit:
        points = points[-limit:]
    return {"circuit_code": circuit_code, "points": points}
