# 后端测试计划

本文档定义 Auris Flow 后端开发进入联调前必须具备的测试矩阵。测试目标是防止接口字段猜测、状态乱跳、权限绕过、重复写入和异步事件丢失。

## 1. 测试分层

| 层级 | 目录 | 目标 |
| --- | --- | --- |
| Unit | `backend/tests/unit` | 领域状态机、权限策略、ID 生成、错误映射。 |
| Contract | `backend/tests/contract` | OpenAPI 请求/响应、错误结构、分页、幂等 replay。 |
| Integration | `backend/tests/integration` | MySQL、Redis、Outbox Worker、对象存储、Qdrant mock、Dagster mock。 |
| E2E smoke | `backend/tests/e2e` | 从 API 到 Worker 到运行详情的最小闭环。 |

## 2. 契约测试

覆盖 `openapi-v0.1.yaml`：

- 所有路径必须返回统一 envelope。
- 列表接口必须支持 `cursor`、`limit`、`q`、`sort` 或明确说明不支持。
- 写操作必须要求 `Idempotency-Key`。
- 错误响应必须包含 `error.code`、`message`、`status`、`retryable`、`trace_id`。
- 401、403、404、409、422、429、500、502、503 至少各有一个样例测试。

最低接口样本：

- `GET /api/v1/insights/ops-summary`
- `POST /api/v1/tenants`
- `POST /api/v1/projects`
- `POST /api/v1/task-runs`
- `GET /api/v1/audio-sessions/{id}`
- `POST /api/v1/human-review-tasks/{id}/decisions`
- `POST /api/v1/label-optimization-runs`
- `POST /api/v1/data-assets/{asset_key}/backfills`

## 3. 状态机测试

基于 `state-machines.md`：

- 合法迁移能成功并写审计。
- 非法迁移返回 `409 RUN_STATE_TRANSITION_INVALID`。
- 乐观锁版本冲突返回 `409 RESOURCE_VERSION_CONFLICT`。
- 高风险动作进入 `blocked` 或 `pending_review`，不能直接 `success`。
- `cancelled` 后不能继续写结果，只能创建新运行。

重点对象：

- `TaskVersion`
- `TaskRun`
- `HumanReviewTask`
- `ConversationBoundary`
- `LabelCandidate`
- `LabelVersion`
- `VoiceprintEnrollment`
- `EvalRun`
- `BackfillRequest`
- `ExternalCallback`

## 4. 权限测试

基于 `rbac-matrix.md`：

- 普通 `annotator` 不能发布标签版本。
- `agent_service` 只能写候选、草稿、人审任务和 Trace，不能发布、外发、导出敏感数据。
- 跨租户访问必须被拒绝。
- 跨项目读取默认拒绝；授权策略存在时只返回脱敏引用。
- 声纹入库、批量回填、外部回写、敏感导出必须触发审批或高风险门禁。

## 5. 幂等测试

每个写接口至少验证：

1. 首次请求成功创建对象。
2. 同一 `Idempotency-Key` + 同一请求体 replay 原响应。
3. 同一 key + 不同请求体返回 `409 IDEMPOTENCY_KEY_CONFLICT`。
4. 同一 key 在不同 `user_id` 下不互相 replay，避免跨用户误共享。
5. Worker 重试不会重复写业务结果。
6. 外部回写按 `tenant_id + task_run_id + recording_id` 去重。

## 6. Outbox 与 Worker 测试

场景：

- API 事务提交后写入 `outbox_events`。
- Worker 成功消费后把 `outbox_events.status` 更新为 `processed`；若 adapter 只返回协议级回执，关联运行必须是 `submitted` + `business_status=awaiting_completion`，不能直接标记 `success`。
- 只有业务结果真正写入、对象可用、Dagster materialization 完成或外部系统确认后，运行状态才能迁移到 `success`。
- completion receipt 只能写入 `submitted` 且 `business_completion_required=true` 的运行；必须校验 adapter 和外部 ID，不允许从 `running` 直接完成。
- 同一个 `completion_receipt_id` + 相同 payload 必须可重放；同 receipt id 但 payload 不同必须返回 `409 RUN_COMPLETION_RECEIPT_CONFLICT`。
- 导出 completion receipt 必须把 `download_ref.status` 从 `reserved` 更新为 `ready`；外部 callback completion receipt 必须在 callback receipt 中写入 `completion_ack`。
- Worker 失败时递增 `attempt_count`、记录 `last_error` 并更新 `available_at`。
- 超过最大重试把 `outbox_events.status` 标记为 `dead_letter`，并把关联运行置为 `failed`。
- 手动处理死信应创建新的重试运行，引用原 `run_id`、`event_id` 和 `trace_id`，原失败运行保持不可变并追加审计。

首批事件：

- `task_run.requested`
- `agent_run.requested`
- `knowledge_index.build_requested`
- `eval_run.requested`
- `backfill.requested`
- `external_callback.requested`
- `export.requested`

## 7. Mock-to-API 回归测试

从 `mock-to-api-map.md` 生成测试清单：

- 每个模块至少有列表、详情、动作三个能力中的两个；关键模块必须三个都有。
- 前端当前展示的 ID 能在 seed 数据中找到。
- 下钻链路携带 `tenant_id`、`project_id`、`object_id`、`trace_id`。
- UI 状态只使用 `pending`、`submitted`、`success`、`failed`、`retry`、`blocked`。
- UI/BFF E2E 在 outbox dispatch 后必须通过 BFF completion receipt endpoint 把 Dagster、对象存储和外部回写型 `submitted` 运行推进到 `success`，并验证导出 ready、callback ack 和 trace 状态一致。
- UI/BFF E2E 的 dispatch coverage 必须覆盖 Dagster、对象存储、外部回写和 Qdrant；知识源同步 `knowledge_sync` 与知识索引构建 `knowledge_build` 是必测运行类型，避免知识库只停留在 projection 渲染。

## 8. 数据库与迁移测试

- 空库执行所有 Alembic 迁移成功。
- 重复执行 `alembic upgrade head` 无变更。
- 最近一版 `downgrade` 在本地可运行。
- 所有项目级表都有 `tenant_id`、`project_id` 和常用状态索引。
- 审计、幂等、outbox 表在任何业务表写接口前可用。

## 9. 安全与脱敏测试

- 审计日志不含 token、密钥、签名 URL、完整转写、原始音频、客户敏感明文。
- 错误响应不泄漏 SQL、堆栈、对象存储 bucket 签名、Qdrant collection 内部名。
- Qdrant 查询必须带 payload filter。
- 对象存储下载必须通过 BFF 鉴权后签发短期 URL。
- 原生 `<audio>` 播放必须先通过已认证 BFF 请求获取短时播放授权；后续无自定义 Header 的媒体 `Range` 请求仍需重新校验授权、租户、项目成员关系和对象作用域，并验证 `206/416`、`storage_object_id` 与 MySQL checksum/size 元数据一致。
- 对象存储 Range 契约必须参数化覆盖 `minio / s3 / obs / oss`：验证 Provider 独立 endpoint/AK/SK、缺失配置 fail closed、MinIO path-style、S3/OSS virtual-host、OBS 原生签名、OSS S3 兼容签名、`Range` 原样转发，以及上游 `206 / Content-Range / Content-Length` 与登记元数据一致。Compose 发布门禁验证 MinIO 真实对象；接入 OBS/OSS 生产账号前必须另跑真实云端集成测试，协议 mock 不能替代该门禁。

## 10. 标签生命周期与统计闭环语义矩阵

### 10.1 Contract

- OpenAPI 精确冻结 `LabelVersionStatus`、`LabelTaxonomyMode=native|normalized|recomputed`、`LabelMappingRelation`、`LabelComparabilityStatus` 和 `LabelVersionApplicability`，运行时枚举与文档漂移必须失败。
- 标签派生 metric run 缺 label scope、normalized 缺 bundle、FactSet generation/fact_as_of 缺失分别返回稳定 UPPER_SNAKE_CASE 错误；客户端自报 applicability=none 不能绕过指标目录。
- 报告请求只接受同 scope 已物化 `metric_result_ids + metric_scope_sha256`；不得只传 metric key 后重查最新事实。

### 10.2 Mapping 与数值语义

| 场景 | 必须证明 |
| --- | --- |
| identity/rename | semantic hash 不变才 comparable；只改名保持 stable label ID。 |
| replace 1:1 | 未独立审批 compatibility 时 structural-break，不能自动 exact。 |
| merge | same-subject-same-event 按声明 grain/lineage/bucket/reducer 折叠；same-subject-different-event 必须保留。 |
| split | 必须 `split-recompute`；禁止比例分摊和多路复制；无 approved FactSet 时 structural-break。 |
| retire | native 历史保留；适用期内 normalized 为 coverage-gap；仅适用期外 not-applicable。 |
| 0/N/A | coverage 完整、分母有效且无命中才是 0；零分母、适用期外、coverage-gap 使用不同 reason。 |
| 多跳 | V1→V2→V3 bundle 无环、无歧义、路径和 compiler/hash 确定；split/retire 不能被绕过。 |

### 10.3 Fact、时间与快照

- 同一 logical key revision 单调；跨 tenant/project、跨 key、跳 revision、自环/环形 supersedes 全部失败。
- Aggregate、HumanReviewDecision、LabelRecomputeRunItem source exactly-one 且同 scope；未知自由文本不能直接成 Fact。
- `occurred_at` 决定业务桶和标签适用期；服务端 `recorded_at` 决定 as-of；晚到事实只进入新的 `fact_as_of` 快照。
- 直接 UPDATE/DELETE LabelFact、published Mapping、MetricResult、metric label scope 和 activation ledger 必须被数据库 trigger 拒绝。
- 标签废弃、人工修正、晚到事实、重跑后，旧 MetricResult/Report content hash 不变。

### 10.4 并发、晋级与回滚

- run create、deployment ACK、drain/deprecate、completion receipt 屏障测试至少重复 100 轮，冻结 run generation 必须等于事务提交时 Head。
- full recompute 在每个分区、回执和重试点故障注入；未完整 Manifest 不可晋级，查询只能看到整套旧或整套新 FactSet/Asset Head。
- promotion/rollback、Audit、Outbox、head event 使用同 mutation ID；重放不重复 Fact、Asset 或 MetricResult。
- 自然人审批、系统身份拒绝、跨 scope 复合 FK、RBAC、幂等同体 replay/异体冲突均有 Contract + Integration 覆盖。

### 10.5 E2E

从人工标注与自动抽取各生成一条强版本 Fact，发布 successor 与 mapping bundle，执行 native/normalized；对 split 执行 recomputed 与整批晋级；生成报告并回跳 evidence/Trace。前端必须展示 pending/success/blocked、coverage、structural break、N/A reason 和审批/回滚结果，不能静默退回混合 mock。

## 11. 验收命令

后端仓库建立后，标准命令应固定为：

```bash
PYTHON=/absolute/path/to/python bash scripts/verify_all.sh
bash scripts/verify_real_stack.sh
npm --prefix prototype/auris-flow-ui run e2e:bff
```

S1 只补契约时至少执行：

```bash
AURIS_PYTHON="${PYTHON:-/absolute/path/to/python}"
"${AURIS_PYTHON}" doc/backend-spec/validate_backend_spec.py
git diff --check -- doc/backend-spec
```
