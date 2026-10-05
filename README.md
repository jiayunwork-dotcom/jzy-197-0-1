# 选矿流程考查数据校正服务

对浮选回路节点/物流的干矿量与化验品位做带守恒约束的加权最小二乘校正，
并提供结构可观测性判断、粗差统计识别、数据版本管理、结果对比与回收率走势。

* Python 3.12 + Flask + NumPy（不调用任何现成数据校正/优化库）
* PostgreSQL 16（psycopg 3），`docker compose` 一键编排
* 算法设计与取值依据见 [`docs/DESIGN.md`](docs/DESIGN.md)
* HTTP 接口清单见 [`docs/API.md`](docs/API.md)

## 快速开始

```bash
docker compose up --build
# API: http://localhost:8000  健康检查 GET /health
```

跑测试（含 PostgreSQL 仓库集成测试）：

```bash
docker compose --profile test run --rm tests
```

不使用容器、仅跑算法与接口测试（内存仓库，无需数据库）：

```bash
pip install -r requirements.txt
python -m pytest tests/
```

## 30 秒手算例核对

```bash
curl -s -X POST localhost:8000/api/circuits -H 'Content-Type: application/json' \
  -d '{"id":"plant","name":"浮选厂"}'

curl -s -X POST localhost:8000/api/circuits/plant/topologies \
  -H 'Content-Type: application/json' -d '{
  "topology": {
    "nodes": [{"id":"n","kind":"feeder"}],
    "streams": [
      {"id":"feed","source":"@env","target":"n","kind":"feed"},
      {"id":"conc","source":"n","target":"@env","kind":"concentrate"},
      {"id":"tail","source":"n","target":"@env","kind":"tailings"}],
    "elements": ["Cu"]}}'

curl -s -X POST localhost:8000/api/campaigns -H 'Content-Type: application/json' \
  -d '{"id":"sep","circuit_id":"plant","name":"9月考查","period":"2026-09",
       "topology_version_id":"<上一步返回的 id>"}'

curl -s -X POST localhost:8000/api/campaigns/sep/versions \
  -H 'Content-Type: application/json' -d '{"parent_id":null,"dataset":{
  "measurements":[
    {"stream_id":"feed","type":"flow","value":100},
    {"stream_id":"feed","type":"grade","element":"Cu","value":2.0},
    {"stream_id":"conc","type":"grade","element":"Cu","value":20.0},
    {"stream_id":"tail","type":"grade","element":"Cu","value":0.5}]}}'
```

校正结果：精矿 7.6923 t/h、尾矿 92.3077 t/h、精矿回收率 76.923%，
节点残差在 1e-10 量级，完全闭合。

## 关键行为约定

* **默认相对标准差**：流量 2%、品位 5%（依据见设计文档第 2 节），
  单测量 `rsd` 或校正设定 `default_rsd` 均可覆盖；
* **不收敛不伪装**：达到 `max_iter`（默认 50）返回 `converged=false`
  与当前残差；
* **不可观测点名拒绝**：未测流量无法由守恒唯一确定时返回 422 并列出
  具体物流，需 `accept_unobservable=true` 才给参考解；
* **粗差**：整体 χ² 检验 + 逐测量标准化残差，可按测量 id 剔除后重算；
* **并发修订**：后提交方收到 409，不会静默覆盖他人数据；
* **结果复用**：同一数据版本 + 同一设定重复提交直接返回已有结果。
