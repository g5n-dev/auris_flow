# Auris Flow 错误与审计契约

本文档定义 Auris Flow 后端统一错误结构、错误码分类、用户可见状态、审计日志字段、敏感信息脱敏、trace 串联和合规保留要求。目标是让 FastAPI BFF、异步 Worker、Dagster 映射、模型服务、对象存储、Qdrant 索引和外部回写在同一错误与审计语义下运行。

## 1. 基本原则

- 所有 API 错误必须结构化返回，前端负责本地化展示。
- 所有异步失败必须写入运行记录、错误对象和 trace，不能只写日志。
- 所有写操作必须产生审计记录；高风险动作必须记录修改前后、审批和回滚引用。
- 审计日志不保存原始音频、完整转写、密钥、token、客户敏感字段，只保存脱敏摘要和对象引用。
- `trace_id` 必须串联 API 请求、事件、运行、Agent Trace、Dagster、模型调用、存储、Qdrant 和外部回写。
- 错误可见状态必须稳定映射到 UI：`pending`、`success`、`failed`、`retry`、`blocked`。

## 2. API 错误响应结构

同步 API 出错时返回：

```json
{
  "success": false,
  "error": {
    "error_id": "err_01jz...",
    "code": "RUN_DEPENDENCY_BLOCKED",
    "category": "run",
    "message": "Task run is blocked by unfinished upstream asset.",
    "user_message_key": "error.run.dependency_blocked",
    "user_message_params": {
      "asset_key": "auris/model/asr_transcripts"
    },
    "http_status": 409,
    "status": "blocked",
    "retryable": false,
    "severity": "warning",
    "trace_id": "trace_20250526_122718",
    "run_id": "tr_01jz...",
    "partition_key": "2025-05-26|aurora-center",
    "idempotency_key": "auris:sales_quality:task_version:tv_123:run_once:v3:2025-05-26|aurora-center:sha256_abcd",
    "affected_objects": [
      {
        "type": "data_asset",
        "id": "auris/model/asr_transcripts"
      }
    ],
    "next_actions": [
      "view_upstream_run",
      "wait_for_dependency"
    ],
    "details": {
      "upstream_status": "running"
    }
  }
}
```

字段要求：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `error_id` | 是 | 错误实例 ID，方便客服、审计和日志检索。 |
| `code` | 是 | 稳定错误码，前端和告警依赖它。 |
| `category` | 是 | 错误分类，见错误码分类。 |
| `message` | 是 | 英文或工程化内部消息，不直接展示给终端用户。 |
| `user_message_key` | 是 | i18n key，前端本地化。 |
| `user_message_params` | 否 | 本地化参数，必须脱敏。 |
| `http_status` | 是 | HTTP 状态码。 |
| `status` | 是 | 后端运行状态：`failed` 或 `blocked` 为主。 |
| `retryable` | 是 | 是否允许重试。 |
| `severity` | 是 | `info`、`warning`、`error`、`critical`。 |
| `trace_id` | 是 | 全链路追踪。 |
| `run_id` | 条件必填 | 与异步运行相关时必填。 |
| `partition_key` | 条件必填 | 分区运行相关时必填。 |
| `idempotency_key` | 条件必填 | 写操作、异步动作、回写相关时必填。 |
| `affected_objects` | 否 | 影响对象，用于 UI 展示影响范围。 |
| `next_actions` | 否 | 可执行下一步，例如重试、查看上游、创建人审。 |
| `details` | 否 | 调试细节，只能包含脱敏数据。 |

## 3. 异步错误对象

运行表、事件 payload、失败队列使用同一错误对象：

```json
{
  "error": {
    "error_id": "err_01jz...",
    "code": "MODEL_PROVIDER_TIMEOUT",
    "category": "model",
    "message": "ASR provider timed out after 30000ms.",
    "failed_stage": "model_service.invocation",
    "retryable": true,
    "retry_count": 2,
    "next_retry_at": "2026-07-06T02:45:00Z",
    "provider": "audio_intelligence_router",
    "downstream_impact": [
      "auris/model/asr_transcripts",
      "auris/label/event_tags"
    ],
    "trace_id": "trace_20250526_122718"
  }
}
```

异步错误必须同时写入：

- 当前运行对象，例如 `task_run`、`agent_run`、`eval_run`、`export_job`、`backfill`、`external_callback`。
- `run_errors` 或等价错误明细表。
- `outbox_events` 的失败事件及 `outbox_delivery_attempts` 投递尝试。
- 可观测日志和 OTel span status。

## 4. 错误码分类

错误码使用大写蛇形命名，格式为 `{DOMAIN}_{REASON}`。错误码一经发布不能改名，只能废弃。

| 分类 | 错误码示例 | HTTP | 用户状态 | 重试 |
| --- | --- | --- | --- | --- |
| `context` | `CONTEXT_MISSING_TENANT`、`CONTEXT_PROJECT_MISMATCH` | 400 / 403 | `blocked` | 否 |
| `auth` | `AUTH_UNAUTHENTICATED`、`AUTH_PERMISSION_DENIED` | 401 / 403 | `blocked` | 否 |
| `validation` | `VALIDATION_SCHEMA_INVALID`、`VALIDATION_PARTITION_KEY_INVALID` | 400 | `failed` | 否 |
| `idempotency` | `IDEMPOTENCY_KEY_CONFLICT`、`IDEMPOTENCY_RESULT_REPLAYED` | 409 / 200 | `blocked` / `success` | 否 |
| `resource` | `RESOURCE_NOT_FOUND`、`RESOURCE_VERSION_CONFLICT` | 404 / 409 | `failed` | 否 |
| `run` | `RUN_DEPENDENCY_BLOCKED`、`RUN_STATE_TRANSITION_INVALID` | 409 | `blocked` | 条件 |
| `dagster` | `DAGSTER_RUN_SUBMIT_FAILED`、`DAGSTER_ASSET_CHECK_FAILED` | 502 / 424 | `retry` / `failed` | 条件 |
| `agent` | `AGENT_CONTEXT_BUILD_FAILED`、`AGENT_DECISION_INVALID_JSON` | 500 / 422 | `retry` / `failed` | 条件 |
| `model` | `MODEL_PROVIDER_TIMEOUT`、`MODEL_OUTPUT_CONTRACT_INVALID` | 504 / 422 | `retry` / `failed` | 条件 |
| `storage` | `STORAGE_UPLOAD_FAILED`、`STORAGE_CHECKSUM_MISMATCH` | 502 / 409 | `retry` / `failed` | 条件 |
| `qdrant` | `QDRANT_UPSERT_FAILED`、`QDRANT_PAYLOAD_INVALID` | 502 / 422 | `retry` / `failed` | 条件 |
| `callback` | `CALLBACK_ENDPOINT_TIMEOUT`、`CALLBACK_MAPPING_INVALID` | 502 / 422 | `retry` / `failed` | 条件 |
| `gate` | `GATE_HUMAN_REVIEW_REQUIRED`、`GATE_RELEASE_BLOCKED` | 409 | `blocked` | 否 |
| `rate_limit` | `RATE_LIMIT_EXCEEDED` | 429 | `retry` | 是 |
| `internal` | `INTERNAL_UNEXPECTED_ERROR` | 500 | `failed` | 条件 |

## 5. 用户可见状态映射

| 后端状态 | 条件 | UI 状态 | UI 应展示 |
| --- | --- | --- | --- |
| `pending` | 已创建未执行 | `pending` | 排队中、运行 ID、创建时间。 |
| `running` | Worker / Dagster / Agent 执行中 | `pending` | 当前阶段、耗时、可查看运行记录。 |
| `success` | 完成 | `success` | 写入对象、影响范围、下一步动作。 |
| `failed` | `retryable=false` | `failed` | 错误原因、影响对象、查看 trace。 |
| `failed` | `retryable=true` 或有 `next_retry_at` | `retry` | 下一次重试时间、已重试次数、手动重试入口。 |
| `blocked` | 审批、人审、权限、依赖、门禁 | `blocked` | 阻断原因、需要谁处理、处理入口。 |
| `cancelled` | 用户或系统取消 | `failed` | 取消原因、操作者、是否可重新运行。 |

前端不应解析内部异常字符串。所有按钮状态、提示和下一步动作必须由 `status`、`retryable`、`next_actions` 和 `affected_objects` 驱动。

## 6. 审计日志字段

所有写操作、权限相关读操作、高风险动作、运行状态变更、外部回写、密钥引用使用、Agent 决策和人工复核都必须写审计日志。

`audit_logs` 最小字段：

```text
audit_id
tenant_id
project_id
environment
actor_type
actor_id
actor_role
impersonator_id
action
resource_type
resource_id
resource_version
result
risk_level
reason
before_ref
after_ref
diff_summary
affected_objects_json
approval_id
human_review_task_id
run_id
agent_run_id
eval_run_id
external_callback_id
trace_id
request_id
idempotency_key
partition_key
ip_hash
user_agent_hash
session_id_hash
sensitive_fields_redacted
retention_policy_id
created_at
```

字段说明：

- `before_ref` / `after_ref` 保存版本化对象引用，不保存完整敏感正文。
- `diff_summary` 只保存字段级摘要，例如 `threshold: 0.78 -> 0.82`。
- `reason` 对高风险动作必填，例如发布、回填、provider 切换、权限变更。
- `risk_level` 使用 `low`、`medium`、`high`、`critical`。
- `result` 使用 `success`、`failed`、`blocked`、`cancelled`。
- `sensitive_fields_redacted` 记录被脱敏的字段名列表。

## 7. 必须审计的动作

| 模块 | 必须审计的动作 |
| --- | --- |
| 租户 / 项目 | 创建、编辑、停用、成员角色变更、配额变更、跨项目授权。 |
| 连接器 | 新增授权、刷新 token、同步游标变更、连接测试、失败重试。 |
| 任务配置 | 保存草稿、添加节点、修改执行映射、校验兼容性、运行一次、设置调度、发布版本、回滚版本。 |
| Agent | 创建 AgentRun、工具调用、决策生成、候选写入、人审要求、失败和重试。 |
| 标签 / Prompt | 候选生成、人工接受/修改/拒绝、发布门禁、灰度、发布、回滚。 |
| 调听 / 人审 | 边界修改、说话人确认、标签修正、证据包创建、复核结论。 |
| 评测 | 创建评测、指标写入、badcase 回流、发布阻断。 |
| 数据资产 | 资产质量检查、回填草稿、审批、重算、导出、失败分区重跑。 |
| 模型 / 工具设置 | provider 切换、参数组修改、密钥引用变更、超时重试策略变更。 |
| 存储 / Qdrant | 存储策略变更、保留周期变更、索引构建、质量门禁。 |
| 外部回写 | 回写请求、成功回执、失败、重试、死信、人工修复。 |

## 8. 敏感信息脱敏规则

### 8.1 禁止进入普通日志和审计明文的内容

- 原始音频内容和可长期访问 URL。
- 完整 ASR 转写正文。
- 客户姓名、手机号、证件号、车牌号、订单号全量值。
- API token、密钥、Authorization header、Cookie、签名 URL。
- 模型服务 endpoint 的敏感鉴权参数。
- 外部回写完整请求体和完整响应体。

### 8.2 允许保存的引用或摘要

- `asset_key`、`asset_ref`、`evidence_id`、`source_id`。
- 脱敏后的片段，例如手机号 `138****1234`。
- SHA-256 哈希，用于关联同一对象但不反推出原值。
- 文本摘要和字段差异，但不能包含完整客户原文。
- `secret_ref`，不能包含 secret value。

当前 BFF 的 `before_json` / `after_json` 写入必须先经过统一递归脱敏器：

- `trace_id`、`run_id`、`asset_key`、`object_key`、`storage_object_id` 等治理引用保留原值。
- 密钥、token、Cookie、签名和授权字段统一写为 `[REDACTED]`。
- 客户姓名、联系方式、地址、证件号、车牌号和 VIN 字段统一写为 `[REDACTED_PII]`。
- 完整转写、识别修正文本、Prompt/模型输入输出和原始音频字段只保存长度标记，不保存正文。
- 普通说明文本中的手机号、邮箱、证件号、车牌号和 VIN 也必须做模式级替换，避免通过备注字段绕过字段名检查。
- 递归深度、字典字段数和数组元素数必须有上限，超出部分只记录截断计数。
- 结构化运行日志与审计日志必须复用同一脱敏策略，异常消息和中性备注字段不得绕过值模式检查。
- 发布审批的控制面重校验只依赖 `request_sha256`、`decision_sha256`、actor 和状态组成的最小证明；脱敏后的展示 JSON 不能作为发布真实性依据。

### 8.3 脱敏示例

| 字段 | 原始值 | 审计 / 日志值 |
| --- | --- | --- |
| 手机号 | `phone_demo_value` | `phone_demo_masked` |
| 证件号 | `id_document_demo_value` | `id_document_demo_masked` |
| Token | `example_project_token_2026_xxx` | `secret_ref:sec_123` |
| 对象 URL | `https://bucket/...signature=...` | `asset_ref:ast_123` |
| ASR 证据 | 完整转写 | `evidence_id:ev_123` + 短摘要 |

## 9. Trace 串联

### 9.1 入站请求头

BFF 支持并传递：

- `traceparent`
- `x-request-id`
- `x-auris-trace-id`
- `x-auris-run-id`
- `idempotency-key`

若调用方未提供，BFF 生成并在响应头返回。

### 9.2 出站调用

调用 Dagster、模型服务、对象存储代理、Qdrant 索引服务和外部回写时必须传：

- `traceparent`
- `x-auris-trace-id`
- `x-auris-run-id`
- `x-auris-tenant-id`
- `x-auris-project-id`
- `idempotency-key`

外部系统不支持这些头时，至少在请求体或回写配置允许的 metadata 中携带 `trace_id` 和 `idempotency_key`。

### 9.3 Span 命名

推荐 OTel span 命名：

- `api.POST /api/v1/task-runs`
- `worker.task_run.submit_dagster`
- `dagster.asset.materialize`
- `agent.context_build`
- `agent.tool_call.{tool_name}`
- `model_service.{service_name}.{provider}`
- `storage.upload`
- `qdrant.upsert`
- `external_callback.send`

所有错误 span 必须设置 status error，并写入 `error.code`、`run_id`、`tenant_id`、`project_id`。

## 10. 合规保留与删除

默认保留策略可被租户策略覆盖，但不能低于合规基线：

| 数据类型 | 默认保留 | 说明 |
| --- | --- | --- |
| 高风险审计日志 | 3 年 | 权限、发布、回填、外部回写、密钥、人工覆盖。 |
| 普通审计日志 | 1 年 | 普通配置、草稿、低风险动作。 |
| 运行记录和错误明细 | 180 天 | 支撑运行诊断、重试和复盘。 |
| OTel trace 明细 | 30-90 天 | 视成本分层采样，高风险链路完整保留。 |
| 原始音频和派生音频 | 默认 180 天 | 按租户存储策略归档或删除。 |
| 证据包和报告 | 1 年 | 若用于争议处理或发布审计，可延长。 |
| Qdrant 向量点 | 跟随源对象 | 源对象删除或到期后必须删除对应向量。 |
| 死信队列 | 180 天或处理后 30 天 | 以较晚者为准。 |

删除要求：

- 删除源对象时必须生成 `data_retention.deletion_requested` 和 `data_retention.deletion_completed` 审计记录。
- Qdrant、对象存储、MySQL 引用和缓存必须联动删除或失效。
- 法务保全或客户争议处理期间禁止自动删除，必须记录 `legal_hold_id`。

## 11. 告警与运营要求

以下情况必须触发告警：

- 外部回写连续失败或进入死信。
- 同一 `partition_key` 重试超过上限。
- 模型 provider 超时率超过阈值并触发降级。
- Qdrant 索引质量门禁失败。
- 高风险审计写入失败。
- 跨租户访问被拒绝。
- 发布门禁被阻断。
- Agent 决策 JSON 合法率低于阈值。

告警 payload 必须包含：

- `tenant_id`
- `project_id`
- `run_id`
- `trace_id`
- `error_code`
- `severity`
- `affected_objects`
- `next_actions`

## 12. 开发验收清单

进入联调前，后端必须满足：

- API 错误均返回统一结构，且前端不解析异常字符串。
- 异步失败均能在运行详情看到错误码、阶段、重试次数、影响对象和 trace。
- 所有写操作有审计日志，审计日志不含原始密钥、原始音频、完整转写和客户敏感字段。
- `trace_id` 能从 BFF 请求串到 Worker、Dagster、模型服务、对象存储、Qdrant 和外部回写。
- 高风险动作能展示审批或 Human Loop 阻断原因。
- Qdrant 删除跟随源对象保留策略。
- 死信事件只能通过新的重试运行恢复，重试记录必须引用原 `run_id`、`event_id` 和 `trace_id`，原失败运行保持不可变并追加审计。
