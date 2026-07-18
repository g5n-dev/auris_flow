# Auris Flow Backend Spec Pack

本目录是 Auris Flow 从高保真 React 原型进入后端开发的规格入口。目标不是复述 UI，而是把原型中的任务运行、Agent Trace、Dagster 执行映射、模型服务、对象存储、Qdrant、外部回写、错误和审计收敛成后端可实现、可联调、可验收的契约。

## 1. 适用范围

当前后端基线：

- FastAPI BFF
- MySQL
- Redis
- MinIO / OBS / S3
- Dagster
- Qdrant
- OpenTelemetry

不使用 ClickHouse。洞察、运营大盘和报告中心第一阶段使用 MySQL 聚合表、预计算结果、Redis 缓存和 Qdrant 召回解释。

前端只访问 FastAPI BFF，不直接访问 MySQL、Dagster、对象存储或 Qdrant。

## 2. 文档清单

本规格包当前包含：

| 文档 | 用途 |
| --- | --- |
| `README.md` | 后端规格入口、开发准入标准、阶段开工条件、原型 mock 到后端契约的映射方式。 |
| `event-contracts.md` | 异步事件、`run_id` / `trace_id` / `partition_key` / `idempotency_key`、Dagster 映射、Agent Run、模型服务、对象存储、Qdrant、外部回写、失败队列和重试策略。 |
| `error-and-audit.md` | 错误结构、错误码分类、用户可见状态、审计日志字段、敏感信息脱敏、trace 串联和合规保留。 |
| `domain-model.md` | 领域模型、Bounded Context、核心对象和 MySQL / Qdrant / 对象存储边界。由领域模型 worker 维护。 |
| `db-schema.md` | MySQL 第一阶段表设计、索引约束、状态字段、审计字段、对象存储引用和 Qdrant payload 对应关系。 |
| `api-contract.md` | `/api/v1/*` API 规则、资源契约、请求响应和前端 UI Projection。由 API worker 维护。 |
| `mock-to-api-map.md` | 当前 React 原型 mock 文案到正式后端 API 的命名归一和模块映射。由 API / 原型映射 worker 维护。 |
| `state-machines.md` | TaskVersion、TaskRun、人审、边界、标签、声纹、评测、badcase、ASR 热词版本、报告、回填和外部回写状态迁移。 |
| `label-lifecycle-statistics.md` | 标签制品/环境状态分离、跨版本 Mapping Bundle、不可变 Fact、native/normalized/recomputed 统计口径与废弃排空的权威 ADR。 |
| `rbac-matrix.md` | 角色、数据范围、模块动作权限、高风险审批、跨租户/跨项目限制和 Agent 自动化边界。 |
| `openapi-v0.1.yaml` | OpenAPI 3.0 草案，覆盖统一响应、错误结构、认证 Header、分页参数和一期核心接口。 |
| `seed-fixture-v0.1.json` | 机器可读联调种子数据，固定租户、项目、音频、证据、人审、标签、ASR 热词、评测、资产和洞察 ID。 |
| `validate_backend_spec.py` | 后端规格静态校验脚本，检查 OpenAPI 覆盖、`operationId`、幂等、分页、热词治理、标签生命周期/统计口径、运行时路由一致性和 seed 引用。 |
| `implementation-blueprint.md` | FastAPI 工程分层、router/service/repository/schema、上下文、幂等、审计 SDK 和 Outbox Worker。 |
| `migration-plan.md` | Alembic 迁移顺序、表创建批次、迁移安全规则和阶段性验收。 |
| `fixtures-and-seed.md` | 一期联调用固定种子数据，覆盖租户、项目、音频、标签、评测、人审、资产和洞察。 |
| `test-plan.md` | 契约测试、状态机测试、权限测试、幂等测试、Outbox 测试和 mock-to-api 回归测试。 |
| `dev-runbook.md` | 本地依赖、环境变量、启动顺序、健康检查、常见排查和开发完成定义。 |

## 3. 关键设计约束

- 所有核心对象必须携带 `tenant_id`、`project_id`、`created_at`、`updated_at`、`created_by`。
- 所有异步运行必须返回运行对象 ID、`status`、`trace_id`、`affected_objects` 和下一步动作。
- 所有写操作必须有审计记录、幂等键和 trace。
- `api-contract.md` 中的一期正式接口必须在 `openapi-v0.1.yaml` 中存在同名 method/path；当前覆盖由 `validate_backend_spec.py` 动态校验，不手写易漂移计数。
- ASR 热词治理必须保持 `统计 → 易错确认 → 词包修复 → 影子复测 → 人工发布 → 受控回填`，并以 `root_trace_id` 串联；历史转写与人工确认不得原地覆盖。
- 热词构建、评测和发布 API 只创建 `pending RunAction`；provider 产物、评测 metrics/gate、published 状态与 TaskVersion 草稿必须由受信完成回执物化，禁止采用客户端自报结果。
- 旧 `hotwords_ref` 只保留历史响应的只读映射；所有新建、更新和运行请求必须使用不可变 `hotword_pack_version_id`。
- OpenAPI 每个正式 operation 必须有唯一 `operationId`，用于 router、SDK 和契约测试命名。
- `work-items`、`event-links`、`data-assets`、`traces`、`label-optimization-runs` 这类主链路资源不得退回 `GenericObject` / `GenericList`。
- 资产路径参数统一使用 `{asset_key}`；由于原始资产键可包含 `/`，进入 URL path 前必须做 URL 编码。
- 所有导出、回填、发布和外部回写必须有失败队列、重试策略和审计回执。
- Agent 输出只能写候选、草稿、人审任务、评测运行、回填草稿或洞察事实，不能直接覆盖线上结果。
- Qdrant 只保存召回向量和 payload，不保存审批、发布、权限或最终业务状态。
- Dagster 映射只在配置、诊断、资产血缘和工程视图中暴露，不作为业务 API 主语言。
- 前端用户状态只使用稳定集合：`pending`、`success`、`failed`、`retry`、`blocked`。

## 4. 开发准入标准

任一后端模块进入开发前，必须先回答并固化以下问题：

1. 资源是否有明确领域对象、表归属和租户 / 项目隔离字段。
2. 写操作是否定义了 `idempotency_key` 组成规则。
3. 异步动作是否定义了 `run_id`、`trace_id`、`partition_key` 和事件链路。
4. 失败是否能映射到统一错误结构和用户可见状态。
5. 高风险动作是否需要 Human Loop、审批、门禁或回滚版本。
6. 是否写审计日志，审计日志是否脱敏。
7. 是否需要 Dagster、模型服务、对象存储、Qdrant 或外部回写。
8. 是否能从 BFF 响应回到原型当前页面需要的 UI Projection。
9. 是否有最小联调样例和失败样例。

未满足以上条件的接口只能作为草案，不能进入前后端联调。

## 5. 阶段 0 开工条件

阶段 0 目标是搭建后端地基，不追求完整业务闭环。

必须完成：

- FastAPI BFF 项目骨架、统一响应格式、错误中间件、上下文中间件。
- `tenant_id`、`project_id`、`user_id`、`trace_id`、`request_id` 的请求上下文。
- MySQL 连接、迁移机制、基础审计表、运行表、错误表、事件 outbox 表。
- Redis 幂等锁、短期运行状态缓存和限流基础能力。
- OpenTelemetry trace 贯穿 BFF 和 Worker。
- 对象存储 profile 与 `asset_ref` 引用格式。
- Dagster RunRequest 适配器骨架，不要求所有 Job 完成。
- 统一错误码枚举和审计写入 SDK。

阶段 0 验收：

- 一个同步写接口能写审计、返回 `trace_id`，重复请求能幂等返回。
- 一个异步样例能创建运行、写 outbox、由 Worker 消费并更新状态。
- 一个失败样例能返回统一错误结构并出现在运行详情中。

## 6. 阶段 1 开工条件

阶段 1 目标是支撑当前原型可见主流程：上下文、权限、连接器、任务运行、音频证据、知识库、标签、评测、洞察、资产和设置。

阶段 1 启动前必须具备：

- 阶段 0 验收全部通过。
- 任务运行、Agent Run、评测运行、导出、回填、外部回写统一状态已落表。
- Dagster `run_key` 与后端 `idempotency_key` 映射完成。
- 对象存储可保存原始音频、处理后 WAV、ASR JSON、Diar JSON、证据包和报告。
- Qdrant 索引服务能从 MySQL 源对象读取、切片、embedding、upsert，并保存可回跳 payload。
- 外部回写具备 Endpoint 引用、鉴权引用、请求体映射、重试和死信。
- BFF 能返回当前原型需要的最小 UI Projection，而不是把后端表结构直接暴露给前端。

阶段 1 第一批闭环：

- `POST /api/v1/task-runs`
- `POST /api/v1/label-optimization-runs`
- `POST /api/v1/knowledge-indexes/{id}/build-runs`
- `POST /api/v1/data-assets/{asset_key}/backfills`
- `POST /api/v1/exports`
- 外部回写 worker

## 7. 从原型 mock 映射到后端契约

当前原型主要位于 `prototype/auris-flow-ui/src/App.tsx`。后端实现时按以下方式映射：

| 原型 mock / 交互 | 后端契约 |
| --- | --- |
| `LabelOptimizationRun.runId` | `run_id`，对应标签优化运行或 Agent 触发的运行主 ID。 |
| `LabelOptimizationRun.traceId`、`LabelCandidate.traceId` | `trace_id`，写入运行表、事件、审计、Dagster tags 和 OTel。 |
| `LabelOptimizationInputs.partitionKey` | `partition_key`，用于 Dagster partition、回填、失败分区重跑、外部回写去重。 |
| `LabelOptimizationRun.dagsterRunDraft` | `DagsterRunDraft` / `RunRequest`，包含 `job_name`、`asset_selection`、`run_config`、`tags`。 |
| `DagsterDraftState` 的“未生成 / 草稿已生成 / 已校验 / 已回写” | 后端草稿状态、兼容性校验状态、Dagster 提交状态和外部回写状态，不能混成一个字段。 |
| `promptBackendContracts` 中的 `TraceRef` | `/api/v1/traces/{trace_id}` 或运行详情中的 trace projection。 |
| `AssetLineage` 节点的 `assetKey` | `DataAsset.asset_key`，用于资产目录、血缘、质量检查、回填和 Dagster Asset 映射。 |
| “创建回填草稿” | 创建 `BackfillDraft`，审批通过后发 `dagster.backfill.requested`。 |
| “派发人审” | 创建 `HumanReviewTask`，保留 `asset_key`、`partition_key`、`trace_id`。 |
| “重跑失败分区” | 使用原 `partition_key` 和稳定 `idempotency_key` 触发重试，不新造业务结果。 |
| “外部回写 / 同步输出资产” | 创建 `external_callback` 运行，按回写绑定执行，成功写回执，失败进重试或死信。 |
| UI 的 `pending / success / failed / retry / blocked` | BFF 从后端运行状态、`retryable`、`next_retry_at`、门禁和审批状态投影。 |

## 8. 标准后端动作流程

所有会改变状态的动作按同一模板实现：

1. BFF 校验上下文、权限、请求 schema 和幂等键。
2. 在 MySQL 事务中写业务对象、运行对象、审计记录和 outbox 事件。
3. 同步返回 `status`、运行 ID、`trace_id`、影响对象和下一步动作。
4. Worker 消费 outbox，调用 Dagster、模型服务、对象存储、Qdrant 或外部系统。
5. Worker 更新运行状态、错误对象、审计日志和后续事件。
6. BFF 详情接口把后端状态投影为当前页面需要的结构。

## 9. 联调验收底线

一个功能只有满足以下条件才算后端就绪：

- 正常路径可从 API 请求追踪到事件、Worker、运行详情和审计日志。
- 重复请求不会重复写业务结果。
- 失败路径有结构化错误、可见状态、影响对象和下一步动作。
- 高风险路径能进入 `blocked` 或 Human Loop。
- 运行详情能展示 `trace_id`、`run_id`、`partition_key`、重试次数和错误码。
- 审计日志不含密钥、完整转写、原始音频或客户敏感明文。
- 前端不需要理解 Dagster、Qdrant collection、对象存储签名 URL 等底层实现细节。

## 10. 达到后端开发标准的最终 Checklist

当前规格包达到“可以启动后端开发”的标准，必须同时满足：

- `openapi-v0.1.yaml` 能作为 BFF router 和契约测试的输入。
- `api-contract.md` 与 `openapi-v0.1.yaml` 的正式接口 method/path 覆盖率保持 100%。
- OpenAPI `operationId` 完整、唯一，关键闭环资源响应 schema 强类型化。
- `db-schema.md` 与 `migration-plan.md` 能推导出 Alembic 第一阶段迁移。
- `implementation-blueprint.md` 明确 FastAPI 分层、上下文、幂等、审计、Outbox 和 Worker 结构。
- `fixtures-and-seed.md` 中的稳定 ID 能支撑前端当前原型的核心下钻。
- `seed-fixture-v0.1.json` 能通过 `python3 doc/backend-spec/validate_backend_spec.py` 的引用一致性检查。
- `test-plan.md` 覆盖契约、权限、状态机、幂等、Outbox 和安全脱敏。
- 所有核心写动作都有 `trace_id`、`idempotency_key`、`affected_objects`、`audit_log` 和 `next_actions`。
- 所有异步动作统一状态：`pending`、`running`、`success`、`failed`、`blocked`、`cancelled`。
- 高风险动作能进入 Human Loop、审批、发布门禁或 `blocked`，Agent 不直接覆盖线上结果。
- 本地开发可按 `dev-runbook.md` 启动依赖、迁移、seed、BFF 和 Worker。
- 文档之间资源名、状态名、错误码、租户/项目隔离规则保持一致。
