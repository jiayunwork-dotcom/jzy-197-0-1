# 接口文档

所有请求/响应均为 JSON，UTF-8。错误统一形如：

```json
{"error": {"code": "invalid_topology", "message": "拓扑校验未通过",
           "details": {"problems": [ ... ]}}}
```

| HTTP | 场景 | error.code |
|---|---|---|
| 400 | 输入不合法（拓扑/测量/参数） | invalid_topology / invalid_survey / bad_setting / ... |
| 404 | 对象不存在 | not_found / topology_version_not_found / survey_version_not_found / run_not_found |
| 409 | 乐观锁冲突、编码/月份重复 | conflict / survey_version_conflict / circuit_exists / survey_exists |
| 422 | 欠定（不可观测）且未接受 | unobservable |

未达收敛不报错：HTTP 200/201 正常返回，`result.status="not_converged"`，
附残差与迭代轨迹；只有完全无法接受（不可观测）才 422。

## 1. 健康检查

`GET /api/health` → `{"status":"ok","backend":"memory|postgresql"}`

## 2. 回路与拓扑版本

### 创建回路
`POST /api/circuits`
```json
{"code": "flotation_a", "name": "一选厂浮选回路", "note": ""}
```

### 录入/修订拓扑（产生新版本）
`PUT /api/circuits/flotation_a/topology`
```json
{
  "elements": ["Cu"],
  "nodes": [
    {"code": "sep", "name": "粗选", "kind": "unit"}
  ],
  "streams": [
    {"code": "feed", "source": null,       "target": "sep", "role": "feed"},
    {"code": "conc", "source": "sep",      "target": null,  "role": "concentrate"},
    {"code": "tail", "source": "sep",      "target": null,  "role": "tailings"}
  ],
  "note": "一进两出教学样例"
}
```
- `source:null` 表示系统给矿边界，`target:null` 表示产品边界。
- 版本号自增，返回 `{"version": n}`，永不覆盖旧版本。

`GET /api/circuits/<code>/topology`（最新版）
`GET /api/circuits/<code>/topology/<version>`
`GET /api/circuits/<code>/topology` 之外有 `GET /api/circuits`、
`GET /api/circuits/<code>`。

校验拒收：孤立节点、端点不存在、两端都挂边界、空编码/重复编码。

## 3. 考查与数据版本

### 创建考查
`POST /api/surveys`
```json
{
  "circuit_code": "flotation_a",
  "topology_version": 1,
  "month": "2026-09-01",
  "name": "2026年9月流程考查",
  "streams": [
    {"code": "feed", "flow": {"value": 100.0, "rsd": 0.015, "flow_kind": "belt"},
     "grades": [{"element": "Cu", "value": 2.0}]},
    {"code": "conc",
     "grades": [{"element": "Cu", "value": 20.0}]},
    {"code": "tail",
     "grades": [{"element": "Cu", "value": 0.5}]}
  ]
}
```
- `flow.rsd`、品位 `rsd` 省略时按类别取默认值（见 README 1.7）。
- 品位用百分数（0–100），内部按小数计算。
- 校验拒收：品位越界、流量为负、rsd≤0、元素/物流不属于该拓扑版本。
- 同一回路同月重复建考查 → 409 `survey_exists`。

### 修订（化验补送/更正）—— 乐观锁
`PUT /api/surveys/<survey_id>`
```json
{"base_version": 1, "streams": [ ... 完整新数据 ... ], "note": "精矿品位更正"}
```
`base_version` 必须等于当前最新版本；两人并发修改时后提交方收到 409
`survey_version_conflict`，details 里给 `base_version/current_version`。
可带 `topology_version` 把考查迁移到新拓扑（旧版本记录仍保留旧拓扑号）。

`GET /api/surveys/<survey_id>`（含 `etag=current_version`）
`GET /api/surveys/<survey_id>/versions`
`GET /api/surveys/<survey_id>/versions/<v>`
`GET /api/surveys?circuit_code=...`

## 4. 提交校正（幂等）

`POST /api/reconciliations`
```json
{
  "survey_id": "...",
  "survey_version": 1,                 // 省略取最新版本
  "excluded_measurements": ["flow:s7"],// 剔除的测量编号（serial elimination）
  "max_iterations": 50,
  "residual_tol": 1e-10,
  "significance": 0.05,
  "accept_unobservable": false
}
```
测量编号：`flow:<stream>`、`grade:<element>:<stream>`。

同 `(survey, survey_version, topology_version, settings)` 重复提交：
首次 201，重复 200 且响应里 `"cached": true`，直接返回已有结果。

欠定（存在不能唯一确定的未测变量）返回 422：
```json
{"error": {"code": "unobservable",
  "details": {"unobservable": [{"kind":"flow","stream":"conc"}, ...],
              "rank": 1, "n_unmeasured": 2,
              "nullspace_directions": [ ... ]}}}
```
显式 `"accept_unobservable": true` 时返回 201，`result.status="unobservable"`，
不可观测量在最小范数解里给出但不应作为唯一结果使用。

### 结果结构（节选）
```json
{
  "id": "...", "survey_id": "...", "survey_version": 1,
  "topology_version": 1, "settings_hash": "…",
  "result": {
    "status": "converged",
    "iterations": 2, "max_iterations": 50,
    "max_relative_residual": 4.7e-17,
    "iteration_trace": [ ... ],
    "observability": {"observable": true, ...},
    "streams": [
      {"stream": "conc", "dry_ore_flow": 7.6923,
       "grades_percent": {"Cu": 20.0},
       "metal_flows": {"Cu": 1.5385},
       "flow_was_measured": false, "boundary_direction": -1}
    ],
    "measurements": [
      {"measurement_id": "flow:feed", "kind": "flow", "stream": "feed",
       "measured_value": 100.0, "sigma": 1.5,
       "reconciled_value": 100.0, "adjustment": 0.0,
       "relative_adjustment": 0.0}
    ],
    "node_balance": [
      {"node": "sep",
       "dry_ore": {"in": 100.0, "out": 100.0, "residual": 0.0,
                   "relative_residual": 0.0,
                   "in_streams": ["feed"], "out_streams": ["conc","tail"]},
       "metals": {"Cu": {"in": 2.0, "out": 2.0, "residual": 0.0,
                         "relative_residual": 0.0}}}
    ],
    "recoveries": {
      "basis": "concentrate_role",
      "product_streams": ["conc"],
      "elements": {"Cu": {"feed_metal_flow": 2.0,
                          "product_metal_flow": 1.5385,
                          "recovery_percent": 76.9231}}},
    "gross_error": {
      "test_available": false,
      "degrees_of_freedom": 0,
      "global_test": null,
      "constraint_tests": [],
      "measurement_tests": [],
      "suspects": []
    }
  }
}
```

冗余度 >0 时 `gross_error` 形如：
```json
{"test_available": true, "degrees_of_freedom": 2,
 "global_test": {"statistic": 31.7, "critical": 5.99, "p_value": 1.3e-7,
                 "gross_error_detected": true},
 "constraint_tests": [{"kind":"mass","node":"sep",
                       "standardized_residual": -5.6, "suspect": true, ...}],
 "measurement_tests": [{"measurement_id": "flow:feed",
                        "test_statistic_z": 5.5, "is_suspect": true, ...}],
 "suspects": [ {"measurement_id": "flow:feed", ...} ]}
```
粗差处理：调用方把嫌疑测量编号放进 `excluded_measurements` 重提即可，
服务不自动删数；全局 χ² 恢复不显著说明该测量解释了矛盾。

`GET /api/reconciliations/<id>`
`GET /api/reconciliations/<id>/balance-sheet`（只看平衡表）
`GET /api/reconciliations?survey_id=...`

## 5. 同一考查两个数据版本对比

`GET /api/surveys/<id>/compare?v1=1&v2=2[&settings_hash_1=&settings_hash_2=]`
```json
{
 "v1": {"version":1,"run_id":"…","status":"converged"},
 "v2": {"version":2,"run_id":"…","status":"converged"},
 "streams": [
   {"stream":"conc","flow_v1":7.692,"flow_v2":8.42,"flow_delta":0.73,
    "grades_percent": {"Cu": {"v1":20.0,"v2":18.0,"delta":-2.0}}}
 ],
 "recovery_percent": {"Cu": {"v1":76.92,"v2":77.14,"delta_points":0.22}}
}
```
某版本没有校正结果 → 404 `run_not_found`。

## 6. 连续月份回收率走势

`GET /api/surveys/<id>/recovery-trend[?months=12]`
（自动取该考查所在回路下、各月考查**最新数据版本**的成功校正结果）
`GET /api/surveys/recovery-trend?circuit_code=...&months=12`
```json
{"circuit_code":"flotation_a",
 "points":[{"survey_id":"…","month":"2026-07-01","name":"…",
            "topology_version":1,"run_id":"…",
            "recovery_percent":{"Cu":75.1}}, ...]}
```
只包含 `status=converged` 的月份；拓扑跨版本时每个点带自己的 `topology_version`，
不会拿旧拓扑结果冒充新拓扑。
