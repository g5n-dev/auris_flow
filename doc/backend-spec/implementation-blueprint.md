# 后端实现蓝图

本文档把 `backend-spec` 转成 FastAPI 后端可执行工程结构。目标是让后端先搭出阶段 0 地基，再按模块接入阶段 1 业务闭环。

## 1. 工程边界

- 后端服务命名：`auris-flow-bff`。
- 对外只暴露 FastAPI BFF；前端不直接访问 MySQL、Redis、MinIO、Dagster、Qdrant。
- BFF 负责认证上下文、RBAC、UI Projection、幂等、审计、错误映射和 Outbox 写入。
- Worker 负责消费 Outbox，调用 Dagster、模型服务、对象存储、Qdrant 和外部回写。

## 2. 推荐目录结构

```text
backend/
  app/
    main.py
    core/
      config.py
      logging.py
      security.py
      context.py
      errors.py
      response.py
      telemetry.py
    api/
      deps.py
      routers/
        ops.py
        tenants.py
        projects.py
        task_runs.py
        audio_sessions.py
        human_review.py
        labels.py
        knowledge.py
        insights.py
        evaluation.py
        data_assets.py
        settings.py
        output_sinks.py
    schemas/
      common.py
      runs.py
      tenants.py
      projects.py
      audio.py
      labels.py
      assets.py
    domain/
      ids.py
      statuses.py
      policies.py
      state_machines.py
    services/
      idempotency_service.py
      audit_service.py
      permission_service.py
      task_run_service.py
      evidence_service.py
      label_service.py
      insight_service.py
      export_service.py
    repositories/
      base.py
      tenants.py
      projects.py
      runs.py
      audit.py
      assets.py
    integrations/
      dagster_client.py
      object_storage.py
      qdrant_client.py
      model_gateway.py
      callback_client.py
    workers/
      outbox_worker.py
      retry_worker.py
      dead_letter_worker.py
  migrations/
    versions/
  tests/
    contract/
    integration/
    unit/
```

## 3. 请求处理链路

所有写操作采用同一链路：

1. `context_middleware` 解析 `Authorization`、`X-Tenant-Id`、`X-Project-Id`、`X-Request-Id`，生成 `RequestContext`。
2. `permission_service` 根据 `rbac-matrix.md` 校验角色、数据范围和高风险门禁。
3. `idempotency_service` 校验 `Idempotency-Key`；命中成功结果时直接 replay。
4. Router 使用 Pydantic schema 校验请求体。
5. Service 在 MySQL 事务里写业务对象、运行对象、审计日志和 Outbox。
6. 返回统一 envelope：`data`、`meta.trace_id`、`affected_objects`、`next_actions`。
7. Worker 消费 Outbox 更新运行状态和后续事件。

## 4. 分层职责

| 层 | 职责 | 禁止事项 |
| --- | --- | --- |
| Router | 参数解析、Pydantic 校验、调用 service、返回 HTTP 状态码 | 不写 SQL、不拼业务状态机。 |
| Service | 业务规则、权限结果解释、状态迁移、事务边界、Outbox 写入 | 不返回数据库裸对象给前端。 |
| Repository | 数据访问、行锁、批量查询、投影查询 | 不做权限判断、不调用外部系统。 |
| Domain | 状态机、枚举、ID、策略判断、不可变规则 | 不依赖 FastAPI 或数据库连接。 |
| Integration | Dagster、对象存储、Qdrant、模型网关、外部回写 | 不直接修改业务权威状态。 |
| Worker | 消费事件、重试、死信、补偿、外部调用 | 不绕过权限和幂等。 |

## 5. 核心中间件

### 5.1 Context Middleware

输出 `RequestContext`：

```text
tenant_id
project_id
user_id
roles[]
permissions[]
store_scope[]
data_scope[]
request_id
trace_id
```

规则：

- `tenant_id` 和 `project_id` 不能只信任 header，必须与 token 和成员表交叉校验。
- 服务账号必须走 `service_accounts`，不能伪装普通用户。
- 缺上下文返回 `400 CONTEXT_MISSING_TENANT` 或 `400 CONTEXT_MISSING_PROJECT`。

### 5.2 Idempotency Middleware

- 只对 POST/PATCH/DELETE 和异步动作强制。
- Key 维度：`tenant_id + project_id + user_id + operation + idempotency_key`，并保存请求体 hash。
- 首次请求写 `idempotency_records`，成功后保存 response digest。
- 重复相同请求返回原结果；同 key 不同 hash 返回 `409 IDEMPOTENCY_KEY_CONFLICT`。

### 5.3 Error Middleware

- 捕获领域错误、权限错误、校验错误、集成错误。
- 不向前端暴露 SQL、堆栈、密钥、签名 URL、完整转写。
- 所有错误写 OTel span 和结构化日志。

## 6. 审计 SDK

所有写操作使用统一方法：

```text
audit_service.record(
  action,
  object_type,
  object_id,
  before_json,
  after_json,
  risk_level,
  result,
  trace_id,
  idempotency_key
)
```

审计写入与业务写入同事务。高风险动作包括发布、回填、导出、外部回写、声纹入库、人工覆盖、权限变更、密钥引用使用。

## 7. Outbox Worker

Outbox 表作为唯一异步入口：

- `outbox_events` 只由事务内写入。
- Worker 按 `available_at`、`priority`、`created_at` 拉取。
- 同一 `aggregate_type + aggregate_id` 使用分布式锁避免乱序。
- 成功写 `processed_at`；失败递增 `attempt_count`、记录 `last_error`，按策略重试或把 `outbox_events.status` 标记为 `dead_letter`。第一阶段不强制拆独立死信表，后续可按运维需要归档到 `dead_letter_events`。

首批事件处理器：

- `task_run.requested`
- `agent_run.requested`
- `knowledge_index.build_requested`
- `eval_run.requested`
- `backfill.requested`
- `external_callback.requested`
- `export.requested`

## 8. UI Projection

BFF 响应面向页面任务，不直接返回表结构：

- 列表接口返回 `status_counts`、`next_actions`、`trace_id`。
- 详情接口返回主对象、证据、状态、关联对象和可执行动作。
- 洞察、知识库、资产血缘只返回业务化节点，不暴露 Qdrant collection 或 Dagster 内部 ID。

## 9. 阶段 0 最小实现切片

第一批后端代码只需要支撑：

1. 请求上下文与统一响应。
2. 租户、项目、成员、角色、审计表。
3. `POST /api/v1/task-runs` 样例异步运行。
4. Outbox Worker 消费样例事件并更新状态。
5. 幂等 replay 与错误结构。

达到以上切片后，再进入任务、音频、标签、评测、资产、洞察的并行开发。
