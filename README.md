# GrayLab — 可解释灰度实验分流 API

供后端服务调用的本地化灰度 / A-B 实验分流服务：**Python + FastAPI + Pydantic + SQLite**，零外部依赖，
一条命令即可在本机启动。配置带版本、发布即冻结；分流基于用户键与实验盐值稳定哈希；
返回**完整决策轨迹**与未命中原因；曝光按幂等键去重并可按版本追溯。

## 快速开始

```bash
pip install -r requirements.txt
python run.py                      # 或 python -m uvicorn app.main:app --reload
# 服务监听 http://127.0.0.1:8000 ，SQLite 文件为 ./graylab.db
# 自定义数据库路径： GRAYLAB_DB=/data/lab.db python run.py
```

- 交互式文档：http://127.0.0.1:8000/docs
- 健康检查：`GET /health`
- 运行测试：`pip install -r requirements-dev.txt && pytest`

## 一分钟示例

```bash
API=http://127.0.0.1:8000/api

# 1) 创建实验并直接发布 v1：50% 总流量、对照/实验组各半、白金用户定向、白名单兜底
curl -s -X POST $API/experiments -H 'Content-Type: application/json' -d '{
  "key": "checkout_redesign", "name": "结账页改版", "namespace": "checkout",
  "publish": true,
  "config": {
    "traffic_percentage": 50,
    "control_variant_key": "control",
    "variants": [
      {"key": "control",  "percentage": 50, "is_control": true},
      {"key": "redesign", "percentage": 50}
    ],
    "whitelist": [{"user_key": "vip-001", "variant_key": "redesign"}],
    "audience": {"condition": {"field": "tier", "op": "in", "value": ["gold","platinum"]}}
  }
}'

# 2) 单用户分流（记录曝光，带幂等键）
curl -s -X POST $API/experiments/checkout_redesign/decide \
  -H 'Content-Type: application/json' \
  -d '{"user_key":"u-123","attributes":{"tier":"gold"},"record_exposure":true,
       "idempotency_key":"campaign-20260910-u-123"}'
```

返回（节选）：

```json
{
  "experiment_key": "checkout_redesign",
  "version_id": 1, "version_number": 1,
  "user_key": "u-123", "bucket": 4217, "gate_bucket": 6103,
  "enrolled": true, "variant_key": "control", "reason": "bucket",
  "trace": [
    {"step": "version_resolved", "result": "ok", "detail": {"version_number": 1, "salt": "…"}},
    {"step": "bucket",  "result": "computed", "detail": {"bucket": 4217, "gate_bucket": 6103,
        "formula": "sha256('…|1|checkout_redesign|u-123')",
        "ranges": [{"variant_key":"control","start":0,"end":5000}, …]}},
    {"step": "schedule", "result": "active"},
    {"step": "whitelist","result": "miss"},
    {"step": "audience", "result": "matched", "detail": {"tree": {…}}},
    {"step": "traffic",  "result": "admitted", "detail": {"cutoff": 5000}},
    {"step": "variant_assignment", "result": "assigned", "detail": {"variant_key": "control"}}
  ],
  "idempotency_key": "campaign-20260910-u-123",
  "exposure_recorded": true
}
```

## 核心概念

### 版本化、不可变配置

- 实验创建时可附带首个版本；之后任何修改都必须调用 `POST /experiments/{key}/versions`
  生成**新版本**（草稿或直接发布）。
- `published` 版本的 `config_json` 在数据库层由 SQLite 触发器禁止 UPDATE；
  API 层也禁止再次发布（`409 version_already_published`）。
- 未指定版本时，分流始终使用**最新已发布版本**；调用方可传 `version` 固定到任一历史发布版本
  （用于回溯当时的决策；固定到草稿返回 `409 version_not_published`）。

### 稳定哈希分桶

```
digest = sha256("salt | version_number | experiment_key | user_key")
variant_bucket = int(digest[0:8])  % 10000   # 选择变体区间
gate_bucket    = int(digest[8:16]) % 10000   # 独立的流量门桶
```

- **版本不变 + 用户不变 ⇒ 分桶永远不变**（同版本重复决策结果固定）。
- 每个实验拥有独立随机盐（创建时自动生成，也可显式传入），避免不同实验间用户分桶相关性。
- 版本号参与哈希：发布新版本是一次有意的重新洗牌。
- 变体桶与流量门桶取自摘要的不同字节，互不相关——因此 50% 总流量 + 50/50 变体时，
  命中的一半用户仍均匀落在两个变体，而不会塌缩到首个区间。

### 决策流水线（每一步都进入 trace）

| 顺序 | 阶段 | 未命中 reason | 说明 |
|---|---|---|---|
| 1 | `version_resolved` | — | 解析版本（最新发布或指定版本），记录 salt/namespace |
| 2 | `schedule` | `not_in_schedule` | 半开区间 `[start, end)`；无 schedules 表示全时生效 |
| 3 | `whitelist` | — | 命中即**强制分流**，绕过受众与流量门，reason=`whitelist` |
| 4 | `audience` | `audience_mismatch` | 嵌套 AND/OR/NOT 条件树，trace 内含完整求值树与实际属性值 |
| 5 | `bucket` | — | 计算 variant / gate 双桶及各变体区间 |
| 6 | `traffic` | `not_in_traffic` | `gate_bucket < traffic_percentage * 100` |
| 7 | `variant_assignment` | — | reason=`bucket`，变体区间按 key 排序、末段吸收取整误差 |

受众操作符：`eq, ne, in, nin, gt, gte, lt, lte, contains, not_contains,
starts_with, ends_with, exists`，字段支持点路径（如 `user.address.city`）。

### 互斥命名空间与生效时段

- 每个实验属于一个 `namespace`（缺省为自身 key，即默认互不干扰）。
- 发布 / 预检时，系统取命名空间内**其他每个实验的最新发布版本**做冲突校验：
  两边生效时间重叠（无 schedules 视为“永远在线”，即与一切相交）
  且 `traffic_percentage` 之和 **> 100%** 时拒绝（`422 namespace_traffic_conflict`）。
  非重叠时间窗允许各自吃满 100%。
- 同一实验的版本采用“最新发布生效”模型：发布新版本即接管流量，旧版本保留为不可变历史，
  可通过 `version` 参数回放。

### 校验规则（预检与发布时全部返回结构化 issues）

- 变体百分比之和必须为 100（`percentages_sum`）；变体 key 唯一。
- 恰好一个 `is_control`，且 `control_variant_key` 必须指向已声明变体。
- 白名单用户不可重复，目标变体必须存在。
- 受众 `in/nin` 的 value 必须为列表；比较操作符必须提供 value。
- 时间窗必须带时区、end > start；同一配置内时间窗不得重叠（`schedule_overlap`）。
- 命名空间互斥冲突（见上）。

### 曝光：幂等、汇总、版本可追溯

- `decide` 携带 `record_exposure=true` 时落库；`idempotency_key` 相同则返回原始记录
  （`exposure_recorded=false` 表示去重命中，响应中的版本/变体即首次决策的结果）。
  未显式提供幂等键时使用 `sha256(experiment|version|user)` 作为天然去重键。
- **未分流决策也记录**（带 reason），便于漏斗分析。
- `GET /experiments/{key}/exposures/summary` 按变体计数、按 reason 计数，可加 `?version=N`
  只统计某配置版本；`GET /experiments/{key}/exposures` 支持按 `variant` / `user_key` 过滤明细。
- `GET /exposures/idempotency/{key}` 取回任意一次决策的完整轨迹与其采用的**配置版本**。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/experiments` | 创建实验（可同时创建并发布首个版本） |
| GET | `/api/experiments` | 列出实验（含最新/已发布版本号） |
| GET | `/api/experiments/{key}` | 实验详情 |
| POST | `/api/experiments/{key}/versions?publish=false` | 生成新版本（草稿/发布） |
| GET | `/api/experiments/{key}/versions` | 全部版本历史 |
| GET | `/api/experiments/{key}/versions/{v}` | 单个版本（不可变快照） |
| POST | `/api/experiments/{key}/versions/{v}/publish` | 草稿经互斥校验后发布 |
| POST | `/api/experiments/{key}/preflight` | **只校验不入库**，返回 issues 列表 |
| POST | `/api/experiments/{key}/decide` | 单个分流（可记录曝光、固定版本、指定时间） |
| POST | `/api/experiments/decide/batch` | 一个用户跨 ≤500 个实验批量分流，单项错误隔离 |
| POST | `/api/experiments/{key}/simulate?version=` | 蒙特卡洛模拟分布（不落曝光），返回配置占比 vs 实际占比、未命中原因分布 |
| GET | `/api/experiments/{key}/exposures/summary?version=` | 曝光汇总计数 |
| GET | `/api/experiments/{key}/exposures?variant=&user_key=` | 曝光明细（含轨迹） |
| GET | `/api/exposures/idempotency/{key}` | 幂等键回查任意决策 |

所有错误使用统一信封：

```json
{"error": {"code": "namespace_traffic_conflict", "message": "…",
           "details": {"issues": [{"code": "…", "message": "…", "location": "…"}]}}}
```

## 典型：配置演进流程

```bash
# 1. 预检一个激进配置，不保存
curl -X POST $API/experiments/checkout_redesign/preflight -d '{ … }'
# 2. 存为草稿
curl -X POST $API/experiments/checkout_redesign/versions -d '{ … }'
# 3. 发布（再次执行互斥校验）
curl -X POST $API/experiments/checkout_redesign/versions/3/publish
# 4. 模拟 5 万用户查看分布
curl -X POST $API/experiments/checkout_redesign/simulate -d '{"users":50000,"attributes":{"tier":"gold"}}'
# 5. 老调用方仍可固定 v2：POST /decide {"version": 2, …}；历史曝光可按版本汇总
```

## 项目结构

```
app/
  config.py          # 设置（GRAYLAB_DB）
  db.py              # SQLite 连接、schema、不可变性触发器、写事务锁
  schemas.py         # 全部 Pydantic 请求/响应模型（含递归受众树）
  hashing.py         # sha256 稳定双桶
  audience.py        # 受众条件树求值（带求值轨迹）
  validation.py      # 结构校验 + 命名空间/时段互斥校验
  repository.py      # 数据访问层（实验/版本/曝光）
  engine.py          # 决策流水线 + 幂等曝光写入
  services.py        # 预检 & 模拟
  routers/
    experiments.py   # 实验/版本/分流/批量/模拟
    exposures.py     # 曝光汇总/明细/幂等回查
  main.py            # FastAPI 装配、统一错误处理
tests/               # pytest 端到端测试（25 个用例，临时 SQLite）
run.py               # 启动入口
```

## 设计说明与边界

- 单机单 SQLite 文件，写入经进程内锁 + `BEGIN IMMEDIATE` 串行化，保证曝光幂等的检查-插入原子性；
  WAL 模式提升读并发。需要水平扩展时，将 `repository.py` 换成带唯一约束的集中式存储即可。
- 流量/百分比使用万分桶整数区间，最后一个变体吸收舍入误差，区间必定恰好覆盖 `[0,10000)`。
- 曝光表不保存用户属性（避免敏感数据落库），只保存键、桶、结果与轨迹。
- `at` 参数支持传入任意时间进行决策与模拟（测试时间窗），缺省为当前 UTC 时间。
