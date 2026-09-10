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
variant_bucket = sha256("salt | version_number | experiment_key | user_key")[0:8]  % 10000
gate_position  = sha256("ns | namespace | user_key")[0:8] % 10000        # 命名空间共享门位置
```

- **版本不变 + 用户不变 ⇒ 变体分桶永远不变**（同版本重复决策结果固定）。
- 每个实验拥有独立随机盐（创建时自动生成，也可显式传入），避免不同实验间变体分桶相关性。
- 版本号参与变体哈希：发布新版本是一次有意的重新洗牌。
- **门位置按命名空间共享**：同一命名空间内所有实验对同一用户看到同一个 `gate_position`，
  配合下文的“命名空间环”保证一个用户在同一时刻至多进入命名空间内的一个实验。
- 变体桶与流量门位置是两条独立哈希，互不相关——因此 50% 总流量 + 50/50 变体时，
  命中的一半用户仍均匀落在两个变体，而不会塌缩到首个区间。

### 决策流水线（每一步都进入 trace）

| 顺序 | 阶段 | 未命中 reason | 说明 |
|---|---|---|---|
| 1 | `version_resolved` | — | 解析版本（最新发布或指定版本），记录 salt/namespace |
| 2 | `schedule` | `not_in_schedule` | 半开区间 `[start, end)`；无 schedules 表示全时生效 |
| 3 | `whitelist` | — | 命中即**强制分流**，绕过受众与命名空间门，reason=`whitelist` |
| 4 | `audience` | `audience_mismatch` | 嵌套 AND/OR/NOT 条件树，trace 内含完整求值树与实际属性值 |
| 5 | `bucket` | — | 计算变体桶及各变体区间 |
| 6 | `mutex_ring` + `traffic` | `mutex_excluded` / `not_in_traffic` | 命名空间环门（见下）；`mutex_excluded` 时 trace 给出胜出实验 |
| 7 | `variant_assignment` | — | reason=`bucket`，变体区间按 key 排序、末段吸收取整误差 |

受众操作符：`eq, ne, in, nin, gt, gte, lt, lte, contains, not_contains,
starts_with, ends_with, exists`，字段支持点路径（如 `user.address.city`）。

### 互斥命名空间与生效时段

互斥在**发布**和**分流**两个层面同时强制执行：

- **发布时（容量上限）**：系统取命名空间内其他每个实验的最新发布版本，用扫描线沿时间轴
  计算任意时刻**所有**同时生效实验的流量占用总和；任一时刻总和 > 100% 即拒绝
  （`422 namespace_traffic_conflict`，错误信息给出峰值占用，因此三个各占 40% 的时间重叠
  实验即使两两之和都不超过 80% 也无法全部发布）。无 schedules 视为“永远在线”，即与一切
  时间窗相交；非重叠时间窗允许各自吃满 100%。
- **分流时（命名空间环，真正互斥）**：同一时刻命名空间内所有生效实验按实验 key 排序，
  在 `[0,10000)` 环上依次领取不重叠的连续切片（大小 = `traffic_percentage`），
  用户在环上的位置由命名空间共享哈希 `sha256(ns|namespace|user)` 决定。
  一个用户只有一个环位置，因此**至多落入一个实验的切片**：
  落入其他实验切片 → `mutex_excluded`（trace 的 `winner` 标明被哪个实验拿走）；
  落入无人认领的尾部 → `not_in_traffic`。环的构成只由已发布配置集合决定，
  发布/下线/时间窗切换才会引起流量重分配，单用户决策保持确定性。
- 每个实验属于一个 `namespace`（缺省为自身 key，即默认互不干扰）。
- 白名单是显式强制覆盖，绕过环门（可理解为运维强制注入，不受互斥约束）。
- 同一实验的版本采用“最新发布生效”模型：发布新版本即接管流量，旧版本保留为不可变历史，
  可通过 `version` 参数回放（环中该实验的切片会替换为被固定的历史版本）。

### 校验规则（预检与发布时全部返回结构化 issues）

- 变体百分比之和必须为 100（`percentages_sum`）；变体 key 唯一。
- 恰好一个 `is_control`，且 `control_variant_key` 必须指向已声明变体。
- 白名单用户不可重复，目标变体必须存在。
- 受众 `in/nin` 的 value 必须为列表；比较操作符必须提供 value。
- 时间窗必须带时区、end > start；同一配置内时间窗不得重叠（`schedule_overlap`）。非法时间窗
  在预检中返回 **422 结构化 issues**（`schedule_order` / `schedule_timezone`），而不是请求解析错误或 500。
- 命名空间互斥冲突（任意时间点总占用 > 100%，见上）。
- 创建实验若校验失败，**不会留下任何实验元数据**（实验行与版本行在同一事务内），同一实验键可立即修正后重试。

### 曝光：幂等、汇总、版本可追溯

- `decide` 携带 `record_exposure=true` 时落库；`idempotency_key` 相同则**直接返回首次持久化的
  原始决策**（`exposure_recorded=false` 表示去重命中）——即使此后发布了新版本、或重放请求携带
  不同属性，响应也与数据库中首次记录的版本/变体/reason/trace 完全一致，不会重新计算出相互矛盾的结果。
  未显式提供幂等键时使用 `sha256(experiment|version|user)` 作为天然去重键。
- **未分流决策也记录**（带 reason），便于漏斗分析。
- `GET /experiments/{key}/exposures/summary` 按变体计数、按 reason 计数，可加 `?version=N`
  只统计某配置版本；`GET /experiments/{key}/exposures` 支持按 `variant` / `user_key` 过滤明细。
- `GET /exposures/idempotency/{key}` 取回任意一次决策的完整轨迹与其采用的**配置版本**。

## 指标归因与效果分析

在版本化分流之上，可为**每个配置版本**定义不可变指标，上报结果事件，查询带统计推断的效果分析。

### 指标定义（绑定版本、不可变）

`POST /api/experiments/{key}/versions/{v}/metrics`：

| 字段 | 说明 |
|---|---|
| `metric_key` | 版本内唯一（同 key 重复定义返回 `409 metric_exists`） |
| `metric_type` | `binary`（二元转化，事件本身即转化）或 `continuous`（连续数值，取事件 `value`） |
| `event_name` | 监听的结果事件名 |
| `attribution_window_seconds` | 归因窗口（秒）：事件须落在曝光后 `[0, window]` 内；`0` 表示不设上限 |
| `direction` | `maximize` / `minimize`，用于判定提升方向是否 `favorable` |
| `min_sample_size` | 每变体最小有效样本量，低于则标记 `insufficient_sample` |
| `srm_threshold` | 样本比例偏差阈值，`\|实际占比-配置占比\|/配置占比` 超过即标记 `srm` |

指标与配置版本同样不可变、不可删除；在新版本上重新定义同名指标是独立指标，
`GET /api/experiments/{key}/metrics?version=N` 可按版本列出。

### 结果事件上报（幂等去重）

`POST /api/experiments/{key}/events`，载荷携带全局唯一 `event_key`、`user_key`、
`event_name`、`occurred_at`（缺省为当前 UTC）和连续指标用的可选 `value`：

- **重复 `event_key` 不重复计数**：返回首次存储的原始事件，`duplicate=true`（且 `attributions` 为空）。
- 首次上报会即时给出针对当前已定义监听指标的**归因预览**（`attributions`），但权威归因在分析时
  依据不可变行重算——之后再定义指标也能回溯历史事件。
- `NaN/Infinity` 在 API 边界直接 `400 invalid_value`，绝不入库；缺数值的事件照常存储
  （`value_present=false`），二元指标忽略 value，连续指标将其计为 `invalid_value` 排除。

### 归因规则

每个事件相对于某版本的某指标，关联到**同一用户、该版本、事件发生时刻之前（含同时刻）
最近一次已入组曝光**，并给出唯一结论：

| 结论 reason | 含义 |
|---|---|
| `attributed` | 命中窗口内的最近入组曝光，计入其变体 |
| `no_exposure` | 该用户在本版本没有已入组曝光（未入组决策也记录的场景） |
| `event_before_exposure` | 事件早于该用户任何曝光 |
| `out_of_window` | 最近曝光存在，但早于归因窗口起点 |
| `invalid_value` | 连续指标事件缺少有限数值（二元指标不受影响） |

分析只纳入“在该版本有任意曝光记录”的用户事件，因此新版本发布后才出现的用户**不会**串入
历史版本；`GET /api/experiments/{key}/events` / `…/events/{event_key}` 可查明细。

### 效果分析

`GET /api/experiments/{key}/versions/{v}/metrics/{metric}/analysis?start_at=&end_at=`
（半开区间 `[start, end)`，可省略任一端；`start >= end` 返回 `400 invalid_time_range`）。

每个变体返回：

- `exposures_used`：该版本去重入组用户数；`valid_samples`：有效样本
  （二元=去重转化用户，连续=有效归因事件数）。
- `value`：二元为转化率，连续为均值；`ci95`：95% 置信区间
  （二元 Wald 正态近似；连续 `mean ± 1.96·s/√n`，样本方差分母 n-1）。
- 非对照变体的 `lift`：相对对照 `(t-c)/|c|` 与 95% CI
  （二元用对数率比 `exp(log(p_t/p_c) ± 1.96·√(1/c_t-1/n_t+1/c_c-1/n_c))-1`；
  连续用 Welch 不配对差值 CI 除以对照均值），以及按 `direction` 计算的 `favorable`。
  对照为 0、样本不足等无法定义时，对应字段为 `null` 而非报错。
- `sample_ratio`：配置占比 vs 实际占比、相对偏差与 `srm` 标记（变体级 + `totals.srm` 总标记）。
- `insufficient_sample`：样本少于 `min_sample_size`（变体级 + `totals.insufficient_sample`）。
- `events_attributed` 与按原因细分的 `exclusions`；`formula` 给出本变体**实际代入数值**的公式。

顶层 `totals` 提供曝光 / 窗内事件 / 去重事件 / 归因 / 有效样本 / 各原因排除计数的完整对账
（`归因 + Σ排除 = 窗内事件`），`formulas` 为公式词典，`attribution` 为原因释义。

**确定性与版本隔离**：归因与统计全部基于不可变的曝光、事件行在查询时纯函数重算，事件表由
`event_key UNIQUE` 去重，因此同一时间范围重复查询结果逐字节一致；指标、曝光、事件连接全部
带 `version_number`，历史版本分析不受后续发布影响。

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
| POST | `/api/experiments/{key}/preflight` | **只校验不入库**；合法返回 200 `{valid:true}`，非法返回 422 结构化 issues |
| POST | `/api/experiments/{key}/decide` | 单个分流（可记录曝光、固定版本、指定时间） |
| POST | `/api/experiments/decide/batch` | 一个用户跨 ≤500 个实验批量分流，单项错误隔离 |
| POST | `/api/experiments/{key}/simulate?version=` | 蒙特卡洛模拟分布（不落曝光），返回配置占比 vs 实际占比、未命中原因分布 |
| GET | `/api/experiments/{key}/exposures/summary?version=` | 曝光汇总计数 |
| GET | `/api/experiments/{key}/exposures?variant=&user_key=` | 曝光明细（含轨迹） |
| GET | `/api/exposures/idempotency/{key}` | 幂等键回查任意决策 |
| POST | `/api/experiments/{key}/versions/{v}/metrics` | 为版本定义不可变指标（二元/连续、窗口、方向、最小样本量、SRM 阈值） |
| GET | `/api/experiments/{key}/metrics?version=` | 列出指标定义 |
| POST | `/api/experiments/{key}/events` | 上报结果事件（event_key 去重，返回即时归因预览） |
| GET | `/api/experiments/{key}/events?event_name=&user_key=` | 结果事件明细 |
| GET | `/api/experiments/{key}/events/{event_key}` | 按事件键回查 |
| GET | `/api/experiments/{key}/versions/{v}/metrics/{metric}/analysis?start_at=&end_at=` | 指标归因与效果分析（比率/均值、提升、95% CI、SRM、样本不足、对账计数与公式） |

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
  repository.py      # 数据访问层（实验/版本/曝光/指标/结果事件）
  metrics.py         # 指标归因（最近入组曝光+窗口）与效果统计（CI/提升/SRM）
  engine.py          # 决策流水线 + 幂等曝光写入
  services.py        # 预检 & 模拟
  routers/
    experiments.py   # 实验/版本/分流/批量/模拟
    exposures.py     # 曝光汇总/明细/幂等回查
    metrics.py       # 指标定义/事件上报/效果分析
  main.py            # FastAPI 装配、统一错误处理
tests/               # pytest 端到端测试（53 个用例，临时 SQLite）
run.py               # 启动入口
```

## 设计说明与边界

- 单机单 SQLite 文件，写入经进程内锁 + `BEGIN IMMEDIATE` 串行化，保证曝光幂等的检查-插入原子性；
  WAL 模式提升读并发。需要水平扩展时，将 `repository.py` 换成带唯一约束的集中式存储即可。
- 流量/百分比使用万分桶整数区间，最后一个变体吸收舍入误差，区间必定恰好覆盖 `[0,10000)`。
- 曝光表不保存用户属性（避免敏感数据落库），只保存键、桶、结果与轨迹。
- `at` 参数支持传入任意时间进行决策与模拟（测试时间窗），缺省为当前 UTC 时间。
