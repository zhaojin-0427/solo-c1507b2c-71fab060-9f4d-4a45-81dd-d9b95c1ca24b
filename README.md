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
- `published` 版本的 `config_json`（含冻结的 `continuity_json` 连续性配置）在数据库层由
  SQLite 触发器禁止 UPDATE，已发布版本也禁止降回草稿；API 层禁止再次发布
  （`409 version_already_published`）。
- 未指定版本时，分流始终使用**最新已发布版本**；调用方可传 `version` 固定到任一历史发布版本
  （用于回溯当时的决策；固定到草稿返回 `409 version_not_published`）。

### 稳定哈希分桶

```
variant_bucket = sha256("salt | version_number | experiment_key | user_key")[0:8]  % 10000
gate_position  = sha256("ns | namespace | user_key")[0:8] % 10000        # 命名空间共享门位置
```

- **版本不变 + 用户不变 ⇒ 变体分桶永远不变**（同版本重复决策结果固定）。
- 每个实验拥有独立随机盐（创建时自动生成，也可显式传入），避免不同实验间变体分桶相关性。
- **版本号参与变体哈希**：发布新版本是一次有意的重新洗牌；声明 `continuity.mode=inherit`
  的连续版本则把哈希种子固定为锚点版本的 `salt|version`（沿继承链传递），从而保持用户原组。
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

### 跨版本分流连续性（继承 assignment_seed 与变体顺序）

默认情况下版本号参与分桶哈希，发布新版本即有意重新洗牌；创建新版本时也可在 `continuity`
中声明 `mode: "inherit"`，继承**同一实验某个已发布版本**的分桶种子与变体顺序，让老用户尽量
留在原组。预检会拒绝以下请求（422 结构化 issues）：

- 来源是草稿（`continuity_source_draft`）或版本不存在（`continuity_source_not_found`）；
- 跨实验来源（`continuity_cross_experiment`）；
- 未提供 `source_version`（`continuity_source_required`）；
- 重命名映射重复来源 / 重复目标（`duplicate_rename_source/target`）、来源或目标变体不存在
  （`rename_source_not_found` / `rename_target_not_found`）、两个来源变体汇入同一目标
  （`rename_target_conflict`）；`reshuffle` 模式携带重命名（`continuity_renames_without_inherit`）。

连续模式下，**入组资格完全以目标版本为准**（受众、时段、白名单、命名空间环与总流量）；
连续性只决定变体维度：

- 变体集合与权重不变 ⇒ 分桶位置与区间完全一致 ⇒ 同一 `user_key` 保持原组（`retained`）；
- 只调整权重 ⇒ 仅跨过移动边界的桶换组（`weight_boundary_crossed`），其余用户不动；
- 新增变体按稳定顺序承接落入新区间的桶（`variant_added`）；删除变体的桶由存活变体按序承接
  （`variant_removed`）；重命名通过 `renames` 映射携带原组（`renamed_variant`，仍记 retained）；
- 种子沿继承链传递（v3 继承 v2、v2 继承 v1 时三者共用同一桶位置），来源锚点顺序统一取来源
  版本**实际分桶顺序**（继承顺序或字典序），与声明顺序无关；
- 目标版本白名单把用户强制到与原组不同的变体时，轨迹与预演如实记 `whitelist_override`。

决策轨迹在继承版本上多出 `continuity` 步骤，记录来源版本、分桶种子、原组（重命名前后）、
当前组与换组原因；`bucket` 步骤的公式直接显示继承来的 `assignment_seed`。

**迁移预演**：`POST /api/experiments/{key}/versions/migration-preview` 接收最多 10000 个
带属性用户，对两个**已发布版本**分别内存决策（固定各自版本进环），统计入组（entered）、
退出（exited）、保留（retained）、换组（switched）、两版均未入组（not_enrolled）数量，
给出各类样例（每类最多 20 条）与换组原因细分；跨版本比较时自动组合继承链上的完整重命名链。
预演**绝不写入曝光**（响应恒含 `exposures_written: false`），也不落任何决策轨迹。

连续配置在版本创建时解析为冻结块（`assignment_seed`、有效变体顺序、重命名映射、来源快照），
随版本存入 `continuity_json` 并与 `config_json` 一样被不可变触发器保护；已发布版本禁止降回
草稿。历史决策与曝光的幂等重放逻辑完全不变。

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

**版本归属（跨版本唯一）**：事件先在该实验**全部版本**中查找同一用户、事件发生时刻之前
（含同时刻）最近的一次**已入组曝光**——该曝光所属版本是事件的唯一归属版本。因此用户先在 v1
曝光、发布 v2 后再次曝光，之后的事件只计 v2，**绝不会回写 v1 的历史分析**；反过来，仅有 v1
曝光的用户，发布 v2 后事件仍计 v1。随后在归属版本上做窗口与数值校验，每个事件给出唯一结论：

| 结论 reason | 含义 |
|---|---|
| `attributed` | 命中归属版本窗口内的最近入组曝光，计入其变体 |
| `no_exposure` | 事件在本版本审计口径内，但该用户此前无已入组曝光（如被门禁挡下，或事件发生时本版本为线上版本的全新用户）；只计入审计，不参与指标统计 |
| `event_before_exposure` | 事件早于该用户在本版本的任何曝光 |
| `out_of_window` | 归属曝光存在，但早于归因窗口起点 |
| `invalid_value` | 连续指标事件缺少有限数值（二元指标不受影响） |

不属于本版本（归属到其他版本）的事件完全不进入本版本口径；无任何曝光记录的“幽灵事件”
只计入事件发生时**当时线上版本**的 `no_exposure` 审计，同样不参与任何统计。
`GET /api/experiments/{key}/events` / `…/events/{event_key}` 可查明细，其中事件明细接口会
校验实验归属——通过错误的实验路径读取其他实验的事件返回 404。

### 效果分析

`GET /api/experiments/{key}/versions/{v}/metrics/{metric}/analysis?start_at=&end_at=`
（半开区间 `[start, end)`，可省略任一端；`start >= end` 返回 `400 invalid_time_range`）。

每个变体返回：

- `exposures_used`：该版本去重入组用户数；`valid_samples`：有效样本
  （二元=**去重转化用户数**，同一用户上报多个不同事件键最多计一次转化；连续=有效归因事件数）。
  二元的 `events_attributed` 仍如实显示去重后的归因事件条数。
- `value`：二元为转化率，连续为均值；`ci95`：95% 置信区间
  （二元 Wald 正态近似；连续 `mean ± 1.96·s/√n`，样本方差分母 n-1）。
- 非对照变体的 `lift`：相对对照 `(t-c)/|c|` 与 95% CI
  （二元用对数率比 `exp(log(p_t/p_c) ± 1.96·√(1/c_t-1/n_t+1/c_c-1/n_c))-1`；
  连续用 Welch 不配对差值 CI 除以对照均值），以及按 `direction` 计算的 `favorable`。
  对照为 0、样本不足等无法定义时，对应字段为 `null` 而非报错。
- `sample_ratio`：配置占比 vs 实际占比、相对偏差与 `srm` 标记（变体级 + `totals.srm` 总标记）。
  **该版本零曝光时无法判断样本比例，`srm` 恒为 false、相对偏差为 null**，不会误报异常。
- `insufficient_sample`：样本少于 `min_sample_size`（变体级 + `totals.insufficient_sample`）。
- `events_attributed` 与按原因细分的 `exclusions`；`formula` 给出本变体**实际代入数值**的公式。

顶层 `totals` 提供曝光 / 窗内事件 / 去重事件 / 归因 / 有效样本 / 各原因排除计数的完整对账
（`归因 + Σ排除 = 窗内事件`），`formulas` 为公式词典，`attribution` 为原因释义。

**确定性与版本隔离**：归因与统计全部基于不可变的曝光、事件行在查询时纯函数重算，事件表由
`event_key UNIQUE` 去重，因此同一时间范围重复查询结果逐字节一致；指标、曝光、事件连接全部
带 `version_number`，历史版本分析不受后续发布影响。

## CUPED 协变量校正

在效果分析之上，可为**连续指标**在曝光前登记不可变 CUPED（Controlled-experiment Using
Pre-Experiment Data）计划，用曝光前协变量降低连续指标的方差、提高检验灵敏度。

### 计划（曝光前、不可变）

`POST /api/experiments/{key}/versions/{v}/metrics/{metric}/cuped-plans`：

| 字段 | 说明 |
|---|---|
| `plan_key` | 全局唯一计划键 |
| `covariate_event_name` | 协变量事件名；只取**每位入组用户首次入组曝光之前**、回看窗口内的该事件 |
| `preexposure_window_seconds` | 曝光前回看窗口（正整数秒）；协变量取半开区间 `[首次曝光 - W, 首次曝光)` |
| `target_aggregation` | 截止前已归因目标事件按用户的汇总方式：`sum`（默认）或 `mean` |
| `covariate_aggregation` | 窗口内协变量事件按用户的汇总方式：`sum`（默认）或 `mean` |
| `missing_covariate_policy` | `exclude`（默认，无协变量的用户排除）或 `population_mean`（以总体协变量均值填充） |

创建时拒绝（422 结构化 issues / 409）：

- 指标不是连续指标（`metric_not_continuous`）；回看窗口非正（请求层 400）；
- 该版本**已有任何曝光记录**（`plan_after_exposure`）——计划必须在版本产生曝光前登记，
  窗口锚点才能严格落在处理期之前，杜绝数据泄漏；
- 同一版本指标已有 CUPED 计划（`cuped_plan_exists_for_metric`），或 `plan_key` 重复。

计划与版本配置一样不可变、不可删除（无更新端点，SQLite 触发器在存储层禁止
UPDATE/DELETE）；`GET /api/experiments/{key}/cuped-plans` 与
`GET /api/cuped-plans/{plan_key}` 可列示与回查。

### 快照：按截止时间冻结、防泄漏配对

`POST /api/cuped-plans/{plan_key}/snapshots`，载荷 `{"cutoff_at": "..."}`：

- 只读取 `recorded_at < cutoff` 的曝光与 `occurred_at < cutoff` 的事件；未来截止时间拒绝
  （422 `cutoff_in_future`），早于计划创建的截止时间拒绝（`cutoff_before_plan`）。
- **协变量 X**：仅取每位入组用户**首次入组曝光之前**、`[t0 - W, t0)` 内的协变量事件；
  曝光时刻及之后的事件永不进入 X，处理期数据无法泄漏。
- **目标 Y**：截止前的目标事件沿用普通效果分析的跨版本唯一归属规则（全版本最近入组曝光
  决定归属版本）、归因窗口与数值校验，再按用户 `sum`/`mean` 汇总为一个 Y。
- **缺失协变量**：`exclude` 直接剔除该用户；`population_mean` 保留用户并以**完整配对**的
  混合协变量均值填充 X（其校正项为 0，Y 保持原值）。无目标事件的用户不计入配对。

θ 用**全部有效配对（跨所有变体混合）一次性估计**：

```
θ = Cov(X, Y) / Var(X),   x̄ = mean(X)（完整配对）
Y_cuped = Y - θ·(X - x̄)
```

混合居中保留总体均值与无偏的变体间差值。响应按变体给出原始均值/方差与 95% CI、
**校正均值/方差与 95% CI**、变体的**方差缩减率** `1 - s²_cuped/s²_raw` 与入组/配对/填充
用户数；非对照变体另给原始差值、**校正差值**及各自的 Welch 95% CI、差值 SE 的方差缩减率
和按指标方向计算的 `favorable`。顶层 `totals` 给出 θ、入组用户、有目标用户、有效配对数、
排除明细（`no_target` / `missing_covariate`）、协变量与目标事件用量（含窗口外/缺数值计数）
以及混合 SSE 口径的总体方差缩减率；每个数字都带代入实际值的 `formula`。

### 异常标记（永不覆盖原分析）

快照同时保留原始与校正两套结果；以下情况进入 `anomalies`：

- `zero_covariate_variance`：完整配对不足 2 个或协变量零方差，θ 不可估——校正列为
  `null`，CI/差值回退为原始分析；
- `insufficient_sample`：某变体配对用户数少于指标的 `min_sample_size`；
- `adjusted_variance_increased`：混合 SSE（或差值 SE）口径下校正后方差反而变大——
  标记异常，但原始与校正结果都原样返回。

**冻结与历史不可改写**：同一计划 + 同一截止时间重复请求返回首次的冻结快照
（`duplicate=true`，结果逐字节一致）；之后上报的协变量/目标事件不影响历史快照，只在更晚的
截止时间快照（序号自增）中体现。`GET /api/cuped-plans/{plan_key}/snapshots` 列出历史摘要，
`GET /api/cuped-plans/{plan_key}/snapshots/{sequence}` 按序号取回冻结快照。

## 多指标发布决策

在效果分析之上，可为**每个配置版本**在曝光前登记不可变的发布决策计划：选定对照与目标变体，
引用 **1 项主指标**及若干**护栏指标**——为主指标设置**最小有利效应**，为护栏设置**非劣界值**，
并选择 **Holm 或 Bonferroni** 多重性校正；快照按截止时间冻结，给出三态发布结论。

### 计划（曝光前、不可变）

`POST /api/experiments/{key}/versions/{v}/release-plans`：

| 字段 | 说明 |
|---|---|
| `plan_key` | 全局唯一计划键（重复返回 `409 release_plan_exists`） |
| `control_variant_key` / `target_variant_key` | 两臂；对照必须等于版本声明的对照变体，且都须有正流量 |
| `primary` | `{metric_key, min_favorable_effect}`：主指标 + 越过门槛所需的最小有利效应（按指标优化方向计，≥0） |
| `guardrails` | `[{metric_key, non_inferiority_margin}]`：护栏及其非劣界值（可容忍的最大不利偏移，≥0） |
| `correction` | `holm`（默认）或 `bonferroni`，作用于计划内全部可评估指标 |
| `alpha` | 族系显著性水平，默认 0.05 |

创建时拒绝（422 结构化 issues / 409）：

- 变体不存在（`unknown_variant`）、两臂相同（`distinct_arms_required`）、对照与版本声明不符
  （`control_mismatch`）、臂流量为 0（`zero_allocation_arm`）；
- **跨版本指标**（`metric_not_on_version`）：引用的指标必须定义在本版本上；
- **重复指标**（`duplicate_metric`）：主指标与护栏、或护栏之间引用同一指标；
- **无界归因窗口指标**（`unbounded_attribution_window`）：窗口为 0 的指标无法判定"窗口已走完"，
  不得进入计划；
- 该版本**已有任何入组曝光**（`409 plan_after_exposure`）——计划必须先于曝光登记。

计划与版本配置一样不可变、不可删除（无更新端点，SQLite 触发器在存储层禁止 UPDATE/DELETE）；
`GET /api/experiments/{key}/release-plans` 与 `GET /api/release-plans/{plan_key}` 可列示与回查。

### 快照：按截止时间冻结、窗口走完才计入

`POST /api/release-plans/{plan_key}/snapshots`，载荷 `{"cutoff_at": "..."}`：

- 只读取 `recorded_at < cutoff` 的曝光与 `occurred_at < cutoff` 的事件；未来截止时间拒绝
  （422 `cutoff_in_future`），早于计划创建的截止时间拒绝（`cutoff_before_plan`）。
- **归因窗口尚未走完的用户被排除**：仅当 `首次入组曝光 + attribution_window_seconds <= cutoff`
  时用户才进入该指标的两臂样本（窗口未走完的用户之后仍可能转化，计入会低估转化率）；
  排除按指标分别进行（各指标窗口不同），计入 `excluded_users.window_incomplete`，其事件计入
  `exclusions.window_incomplete_user`。
- 事件归因沿用普通效果分析的跨版本唯一归属规则（全版本最近入组曝光决定归属版本）、
  归因窗口与数值校验；归属到计划外第三变体的事件计入 `exclusions.variant_not_in_plan`。

每项指标返回：两臂**样本量**（二元为合格用户与转化用户，连续为合格用户与有效事件数）、
**效应**（按指标方向定向：θ̂ = orient·(目标 − 对照)，正值恒为有利）、**95% 置信区间**
（θ̂ ± 1.96·SE）、**校正前后 p 值**与**阈值差距**（`threshold_gap = θ̂ − 阈值`，主指标阈值为
`min_favorable_effect`，护栏为 `−non_inferiority_margin`）。主指标做单侧优效检验
`p = 1 − Φ(θ̂/SE)`；护栏做单侧越界检验 `p = Φ((θ̂ + m)/SE)`（p 小 = 显著危害）。
校正族为全部**可评估**指标（主指标优效 + 各护栏越界），Holm 为逐步下调、Bonferroni 为
统一乘 k；不可评估的指标（臂为空、零方差、连续指标观测不足）p 值为 null、不进校正族，
且永远不能视为通过。

### 三态决策

- **可发布（`ship`）**：主指标越过效应门槛（`threshold_gap ≥ 0`）**且**校正后 p < alpha，
  同时**全部护栏未越过非劣界值**（点估计在界值安全侧且未显著越界）；
- **不可发布（`do_not_ship`）**：**任一护栏显著越界**（校正后越界 p < alpha）；
- **证据不足（`insufficient_evidence`）**：其余一切情况——主指标不显著或未过门槛、
  护栏点估计越界但未达显著、或指标尚不可评估。

`decision.reasons` 给出机器可读的原因码（如 `primary_not_significant_after_correction`、
`guardrail_crossed_bound_not_significant:retain`、`guardrail_violated:crash`），每项指标带
代入实际数值的 `formula`，顶层 `formulas` 为公式词典。同一计划 + 同一截止时间重复请求
复用首次的冻结结果（`duplicate=true`，逐字节一致），之后到达的数据——哪怕时间戳落在
冻结窗口内——都**不改写历史快照**，只在更晚截止时间的快照（序号自增）中体现。
`GET /api/release-plans/{plan_key}/snapshots` 列出历史摘要，
`GET /api/release-plans/{plan_key}/snapshots/{n}` 按序号取回。

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
| POST | `/api/experiments/{key}/versions/migration-preview` | 迁移预演：≤10000 带属性用户对比两个已发布版本的入组/退出/保留/换组（含换组原因与样例），不写曝光 |
| GET | `/api/experiments/{key}/exposures/summary?version=` | 曝光汇总计数 |
| GET | `/api/experiments/{key}/exposures?variant=&user_key=` | 曝光明细（含轨迹） |
| GET | `/api/exposures/idempotency/{key}` | 幂等键回查任意决策 |
| POST | `/api/experiments/{key}/versions/{v}/metrics` | 为版本定义不可变指标（二元/连续、窗口、方向、最小样本量、SRM 阈值） |
| GET | `/api/experiments/{key}/metrics?version=` | 列出指标定义 |
| POST | `/api/experiments/{key}/events` | 上报结果事件（event_key 去重，返回即时归因预览） |
| GET | `/api/experiments/{key}/events?event_name=&user_key=` | 结果事件明细 |
| GET | `/api/experiments/{key}/events/{event_key}` | 按事件键回查 |
| GET | `/api/experiments/{key}/versions/{v}/metrics/{metric}/analysis?start_at=&end_at=` | 指标归因与效果分析（比率/均值、提升、95% CI、SRM、样本不足、对账计数与公式） |
| POST | `/api/experiments/{key}/versions/{v}/metrics/{metric}/cuped-plans` | 为连续指标登记**曝光前**不可变 CUPED 计划（协变量事件/回看窗口/汇总方式/缺失策略） |
| GET | `/api/experiments/{key}/cuped-plans` | 列出实验的 CUPED 计划 |
| GET | `/api/cuped-plans/{plan_key}` | 取回单个不可变 CUPED 计划 |
| POST | `/api/cuped-plans/{plan_key}/snapshots` | 按截止时间生成（或回放冻结的）CUPED 快照 |
| GET | `/api/cuped-plans/{plan_key}/snapshots` | CUPED 快照历史摘要 |
| GET | `/api/cuped-plans/{plan_key}/snapshots/{n}` | 按序号取回冻结快照 |
| POST | `/api/experiments/{key}/versions/{v}/release-plans` | 为版本登记**曝光前**不可变发布决策计划（主指标 + 护栏、Holm/Bonferroni） |
| GET | `/api/experiments/{key}/release-plans` | 列出实验的发布决策计划 |
| GET | `/api/release-plans/{plan_key}` | 取回单个不可变发布决策计划 |
| POST | `/api/release-plans/{plan_key}/snapshots` | 按截止时间生成（或回放冻结的）发布决策快照（三态结论） |
| GET | `/api/release-plans/{plan_key}/snapshots` | 发布决策快照历史摘要 |
| GET | `/api/release-plans/{plan_key}/snapshots/{n}` | 按序号取回冻结的发布决策快照 |

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
  sequential.py      # 成组序贯检验：α 消耗边界、检查点统计、条件功效
  cuped.py           # CUPED 协变量校正：防泄漏配对、θ 估计、原始/校正均值、CI、方差缩减
  release.py         # 多指标发布决策：窗口走完的合格样本、定向效应/CI/p 值、Holm/Bonferroni、三态结论
  engine.py          # 决策流水线 + 幂等曝光写入
  continuity.py      # 跨版本连续性：种子/顺序继承、重命名链、换组原因
  migration.py       # 迁移预演（两版本对比，纯内存、不落曝光）
  services.py        # 预检 & 模拟
  routers/
    experiments.py   # 实验/版本/分流/批量/模拟/迁移预演
    exposures.py     # 曝光汇总/明细/幂等回查
    metrics.py       # 指标定义/事件上报/效果分析
    sequential.py    # 序贯计划与检查点
    cuped.py         # CUPED 计划与冻结快照
    release.py       # 发布决策计划与冻结快照
  main.py            # FastAPI 装配、统一错误处理
tests/               # pytest 端到端测试（含跨版本连续性回归用例），临时 SQLite
run.py               # 启动入口
```

## 设计说明与边界

- 单机单 SQLite 文件，写入经进程内锁 + `BEGIN IMMEDIATE` 串行化，保证曝光幂等的检查-插入原子性；
  WAL 模式提升读并发。需要水平扩展时，将 `repository.py` 换成带唯一约束的集中式存储即可。
- 流量/百分比使用万分桶整数区间，最后一个变体吸收舍入误差，区间必定恰好覆盖 `[0,10000)`。
- 曝光表不保存用户属性（避免敏感数据落库），只保存键、桶、结果与轨迹。
- `at` 参数支持传入任意时间进行决策与模拟（测试时间窗），缺省为当前 UTC 时间。
