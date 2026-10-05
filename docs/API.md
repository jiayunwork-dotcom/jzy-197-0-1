# HTTP 接口

所有请求/响应为 JSON。错误响应统一形如：

```json
{"code": "unobservable", "message": "……", "details": {}}
```

| HTTP | code | 含义 |
|---:|---|---|
| 422 | `validation_error` / `topology_invalid` / `isolated_node` / `stream_endpoint_not_found` / `grade_out_of_range` / `negative_flow` / `non_positive_sigma` / `duplicate_id` / `inconsistent_elements` / `unobservable` | 输入不合法或系统不可观测 |
| 409 | `version_conflict` | 修订基于的父版本已过期 |
| 404 | `not_found` | 资源不存在 |
| 500 | `internal_error` | 未预期错误 |

## 拓扑与版本

### POST /api/circuits
`{"id", "name"?}` → 201

### POST /api/circuits/{cid}/topologies
```json
{"topology": {
  "nodes":    [{"id": "n1", "name"?: "...", "kind"?: "rougher"}],
  "streams":  [{"id": "s1", "source": "@env", "target": "n1",
                "kind"?: "feed|concentrate|tailings|middlings|intermediate"}],
  "elements": ["Cu", "Au"]}}
```
`@env` 是系统边界保留 id。校验通过才入库并分配递增 `version_no`；
拓扑版本不可变。→ 201 `{"id":"tv_…", "version_no":1, ...}`

### GET /api/circuits/{cid}/topologies
### GET /api/topologies/{tv_id}

## 考查与数据版本

### POST /api/campaigns
`{"id", "circuit_id", "name"?, "period"?: "2026-09", "topology_version_id"}`

考查**必须**声明使用哪一版拓扑。拓扑变更后新建考查用新版本，旧考查不动。

### POST /api/campaigns/{id}/versions
```json
{"parent_id": null,
 "note": "首版",
 "created_by": "zhang",
 "dataset": {"measurements": [
   {"stream_id": "feed", "type": "flow",  "value": 100, "rsd"?: 0.02},
   {"stream_id": "feed", "type": "grade", "element": "Cu",
    "value": 2.0, "rsd"?: 0.05}]}}
```
* 首版 `parent_id` 必须是 `null`；修订必须带当前版本 id；
* 并发修订的第二人收到 **409**，`details.current` 是服务端当前版本；
* 入库前用考查绑定的拓扑版本做校验（品位 0~100、流量非负、RSD>0 等）。

### GET /api/campaigns/{id}/versions · GET /api/campaign-versions/{vid}

## 校正

### POST /api/campaign-versions/{vid}/reconcile
```json
{"settings": {
   "max_iter": 50,
   "tol_residual": 1e-10,
   "tol_step": 1e-12,
   "default_rsd": {"flow": 0.02, "grade": 0.05},
   "excluded_measurement_ids": ["m_abc123"],
   "accept_unobservable": false}}
```
settings 全可选。响应（201 新建 / 200 复用）：

```json
{
  "run_id": "run_…", "campaign_version_id": "cv_…",
  "topology_version_id": "tv_…", "settings_hash": "…", "input_hash": "…",
  "converged": true, "reused": false,
  "result": {
    "converged": true, "iterations": 1, "max_iter": 50,
    "residual_history": [1.6e-15],
    "max_relative_residual": 1.6e-15,
    "objective": 0.0,
    "streams": [{"id", "flow", "flow_measured",
                 "grades": {"Cu": 19.9999}, "grades_measured": {...}}],
    "corrections": [{"measurement_id", "stream_id", "type", "element",
                     "measured_value", "sigma", "adjusted_value",
                     "adjustment", "relative_adjustment",
                     "weighted_adjustment"}],
    "node_balances": [{"node_id", "in_solids", "out_solids",
                       "residual", "relative_residual",
                       "elements": {"Cu": {"in_metal", "out_metal",
                                            "residual", "relative_residual"}}}],
    "observability": {"observable", "nullity", "unmeasured",
                      "unobservable", "ambiguity_groups"},
    "gross_errors": {"dof", "chi_square", "p_value",
                     "global_test": {"failed", "note"},
                     "measurement_tests": [{"measurement_id", "stream_id",
                       "type", "element", "z", "abs_z", "suspect"}],
                     "suspects", "primary_suspect"},
    "balance_table": {"streams", "nodes",
                      "elements": {"Cu": {"feeds", "products",
                        "feed_metal", "product_metal",
                        "closure_error_percent"}}},
    "recovery": {"Cu": {"conc": 76.923, "tail": 23.077}},
    "excluded_measurement_ids": [],
    "warnings": []
  }
}
```

* `converged=false` 不是错误：结果与残差照常返回，HTTP 200/201；
* 不可观测时 422，`details.unobservable` 列出变量与不可识别自由度。

### GET /api/runs/{rid} · GET /api/runs/{rid}/balance
### GET /api/campaign-versions/{vid}/runs

## 分析

### GET /api/campaigns/{id}/compare?a={cv或run}&b={cv或run}
按物流列出两版校正结果的流量/品位/回收率差异（绝对差、相对差、回收率
百分点差），并给出最大差值汇总。

### GET /api/circuits/{cid}/recovery-trend?element=Cu&product_stream_id=conc
把该回路历次考查（按 `period` 升序）的回收率聚合成走势。
不传 `product_stream_id` 时取全部边界产品合计回收率；不同拓扑版本的
考查也可聚合（各用各的拓扑计算，按元素输出，产品 id 不可比时用合计口径）。
