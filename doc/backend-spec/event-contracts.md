# Auris Flow 后端事件契约

本文档定义 Auris Flow 从 React 原型进入后端开发时必须统一的异步事件、运行标识、幂等、Dagster 映射、Agent Run、模型服务、对象存储、Qdrant 索引、外部回写、失败队列和重试策略。

本契约服务于 FastAPI BFF、MySQL、Redis、MinIO / OBS / S3、Dagster、Qdrant 和 OpenTelemetry。前端只消费 BFF 投影，不直接访问 Dagster、Qdrant 或对象存储。

## 1. 设计原则

- MySQL 保存权威业务状态；事件用于驱动异步执行、状态流转和审计，不替代业务表。
- Redis 只承担短期锁、版本化缓存、扫描冷却和幂等加速；Qdrant 只承担未知标签、相似 badcase 与边界样本召回。两者都不是 LabelFact、PromptVersion、评测或发布事实源。
- 所有异步动作必须先落库再投递事件，避免按钮点击成功但无运行记录。
- 所有事件必须携带 `tenant_id`、`project_id`、`trace_id`、`run_id`、`idempotency_key`。
- Agent 只能生成候选、草稿、人审任务、评测运行、回填草稿或洞察事实，不能直接覆盖线上标签、任务版本、模型 provider、资产结果或外部系统。
- Dagster 是执行引擎映射层，不是业务 API 主语言；业务侧使用任务运行、资产生成、质量检查、回填、回写等产品语言。
- Qdrant 只做召回索引，不保存审批、发布、权限或最终业务状态。
- 外部回写必须由任务运行和权限系统执行，Agent 只能建议字段映射、重试策略和影响范围。

## 2. 标识与上下文字段

| 字段 | 必填 | 生成方 | 语义 |
| --- | --- | --- | --- |
| `event_id` | 是 | 事件生产者 | 全局唯一事件 ID，建议 UUIDv7。 |
| `event_type` | 是 | 事件生产者 | 点分命名，例如 `task_run.requested`。 |
| `event_version` | 是 | 事件生产者 | 语义版本，默认 `1.0`，破坏性变更升大版本。 |
| `occurred_at` | 是 | 事件生产者 | 事件发生时间，ISO-8601 UTC。 |
| `tenant_id` | 是 | BFF / Worker | 租户边界，任何消费方必须校验。 |
| `project_id` | 是 | BFF / Worker | 项目边界，任何消费方必须校验。 |
| `user_id` | 条件必填 | BFF | 用户触发时必填；系统触发可为空。 |
| `actor_type` | 是 | BFF / Worker | `user`、`system`、`agent`、`scheduler`、`external`。 |
| `run_id` | 是 | BFF / Worker | 业务运行主 ID，贯穿 task、agent、eval、export、backfill、callback。 |
| `trace_id` | 是 | BFF / Worker | OpenTelemetry trace ID，贯穿 API、队列、Dagster、模型服务和回写。 |
| `root_trace_id` | 是 | BFF / Worker | 跨 Observation、Fact、Mapping、Metric、Report 和回填保持稳定的闭环根 Trace。 |
| `span_id` | 否 | OTel SDK | 当前处理阶段 span。 |
| `request_id` | 条件必填 | BFF | 同步 API 请求 ID；系统调度可为空。 |
| `partition_key` | 条件必填 | BFF / Dagster | 分区运行必填，格式见下方约定。 |
| `idempotency_key` | 是 | BFF / Worker | 幂等键，同一语义写操作必须稳定。 |
| `correlation_id` | 是 | BFF / Worker | 同一业务链路聚合 ID，默认等于根 `run_id`。 |
| `causation_id` | 否 | 事件生产者 | 触发当前事件的上一事件 ID。 |
| `mutation_id` | 是 | BFF / Worker | 同一业务事务中业务行、Audit 和 Outbox 的对账键。 |
| `aggregate_type` / `aggregate_id` | 是 | 事件生产者 | 事件所属权威聚合及其稳定 ID。 |
| `resource_version` | 条件必填 | 事件生产者 | 可变 projection/Head 事件必填，供 CAS 与乱序保护。 |
| `source_manifest_sha256` | 条件必填 | 事件生产者 | 物化、映射、指标和重算事件必填，防止回执替换输入。 |

信封 `occurred_at` 是“事件何时发生”；LabelFact 业务时间在 payload 中使用 `fact_occurred_at`，落强表后对应 Fact `occurred_at`。两者都不能替代服务端 `recorded_at` 或指标 `fact_as_of`。

### 2.1 `run_id`

`run_id` 是业务层运行主键，不等同于 Dagster run id。不同运行类型可以有独立 ID，但必须能聚合到同一链路：

- `task_run_id`：任务版本运行。
- `agent_run_id`：一次 Agent 智能运行。
- `eval_run_id`：评测运行。
- `export_job_id`：导出任务。
- `backfill_id`：回填运行。
- `external_callback_id`：外部回写运行。

实现建议：

- 业务运行表使用各自 ID。
- 事件信封统一填 `run_id`，值为当前事件所属的最具体运行 ID。
- 跨运行链路通过 `correlation_id` 聚合，例如一次标签优化运行触发 Agent、Dagster、Eval 和 Callback。

### 2.2 `trace_id`

`trace_id` 用于观测和回放，不作为业务唯一键。BFF 接到请求时：

1. 若请求带 `traceparent`，继承 W3C trace。
2. 否则生成新的 OTel trace。
3. 返回体、审计日志、运行表、事件和 Dagster run tags 都写入同一个 `trace_id`。

### 2.3 `partition_key`

`partition_key` 必须稳定、可排序、可回放。推荐格式：

```text
{date}|{store_id}|{optional_dimension}
```

示例：

- `2025-05-26|aurora-center`
- `2025-05-26|aurora-center|device-07`
- `2025-05-26|sales_quality|asr-v2.3.1`

约束：

- 不放用户姓名、手机号、证件号等敏感信息。
- 与 Dagster `PartitionsDefinition` 对齐。
- 回填、失败分区重跑、外部回写均必须携带同一个 `partition_key`。

### 2.4 `idempotency_key`

所有写操作、异步运行、外部回写、对象上传、Qdrant upsert 都必须有幂等键。建议组成：

```text
{tenant_id}:{project_id}:{resource_type}:{resource_id}:{action}:{version}:{partition_key}:{input_hash}
```

示例：

```text
auris:sales_quality:task_version:tv_123:run_once:v3:2025-05-26|aurora-center:sha256_abcd
```

规则：

- BFF 在同步 API 层校验 `Idempotency-Key` 请求头；无头时用稳定业务字段生成。
- Redis 用作短期锁和请求去重，MySQL 唯一索引用作最终幂等保障。
- Dagster `run_key` 与 `idempotency_key` 保持一致或可逆映射。
- 外部回写请求头必须带 `Idempotency-Key`，响应回执保存该键。

## 3. 事件信封

所有异步事件使用统一信封：

```json
{
  "event_id": "evt_01jz...",
  "event_type": "task_run.requested",
  "event_version": "1.0",
  "occurred_at": "2026-07-06T02:30:00Z",
  "tenant_id": "auris",
  "project_id": "sales_quality",
  "user_id": "usr_123",
  "actor_type": "user",
  "run_id": "tr_01jz...",
  "trace_id": "0f8fad5b40684ed6a1d9...",
  "span_id": "b7ad6b7169203331",
  "request_id": "req_01jz...",
  "partition_key": "2025-05-26|aurora-center",
  "idempotency_key": "auris:sales_quality:task_version:tv_123:run_once:v3:2025-05-26|aurora-center:sha256_abcd",
  "correlation_id": "lor_01jz...",
  "causation_id": null,
  "source": "fastapi-bff",
  "subject": {
    "type": "task_run",
    "id": "tr_01jz..."
  },
  "payload": {},
  "metadata": {
    "schema": "auris.event.task_run.requested.v1",
    "producer": "bff",
    "producer_version": "0.1.0"
  }
}
```

消费方要求：

- 先校验 `tenant_id`、`project_id`、`event_version` 和 `idempotency_key`。
- 同一 `event_id` 只能消费一次；同一 `idempotency_key` 的重复事件必须返回已完成结果。
- 消费失败必须写入失败队列，不允许静默吞掉。

## 4. 统一运行状态

业务 API 对外只暴露统一状态：

| 状态 | 含义 | 可重试 |
| --- | --- | --- |
| `pending` | 已创建，等待执行或排队。 | 否 |
| `running` | 正在执行。 | 否 |
| `submitted` | 已完成协议级分发或提交，等待外部系统、对象生成或业务完成回执。 | 否 |
| `success` | 完成并写入结果。 | 否 |
| `failed` | 执行失败，可查看错误；是否可重试看 `retryable`。 | 条件 |
| `blocked` | 被权限、门禁、依赖、审批或配额阻断。 | 条件 |
| `cancelled` | 用户或系统取消。 | 否 |

Agent 内部阶段使用 `phase` 扩展，不替代对外状态：

- `context_building`
- `tool_calling`
- `deciding`
- `waiting_human`
- `completed`
- `expired`

前端 `retry` 不是后端主状态。当 `status=failed` 且 `retryable=true` 或 `retry_scheduled_at` 有值时，BFF 投影为 UI 的 `retry`。

`outbox_events.status=processed` 只表示 outbox 派发完成，不等同于业务运行完成。Dagster run request、对象存储 manifest/reservation、外部 callback receipt 等协议级回执必须先把 `RunRecord.status` 置为 `submitted`，并在 `payload.business_status=awaiting_completion` 中暴露等待态；只有后续 materialization、真实对象可用、远端业务确认或质量门禁完成后才能迁移到 `success`。

业务完成回执通过资源化 endpoint 写入，例如 `POST /api/v1/task-runs/{id}/completion-receipts`、`POST /api/v1/exports/{id}/completion-receipts` 和 `POST /api/v1/output-sinks/platform-callbacks/{id}/completion-receipts`。服务端必须校验租户、项目、`Idempotency-Key`、当前运行状态、分发 adapter 和外部 ID，不能由前端或外部系统任意把 `running` 运行改成完成态。

## 5. 异步事件清单

### 5.1 任务运行事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `task_run.requested` | 用户运行一次、调度触发或上游事件触发。 | `task_version_id`、`canvas_version_id`、`trigger_type`、`input_refs`、`output_bindings`、`dagster_run_draft`。 |
| `task_run.started` | Worker 开始处理。 | `worker_id`、`started_at`。 |
| `task_run.dagster_submitted` | 已提交 Dagster RunRequest。 | `dagster_run_id`、`job_name`、`run_key`、`asset_selection`、`run_config_ref`。 |
| `task_run.completion_received` | 收到并验证业务完成回执。 | `completion_receipt_id`、`adapter`、`external_id`、`result_ref`、`metrics`。 |
| `task_run.succeeded` | 所有必要输出写入成功。 | `output_refs`、`materialization_refs`、`affected_objects`。 |
| `task_run.failed` | 运行失败。 | `error`、`retryable`、`failed_stage`、`retry_count`。 |
| `task_run.blocked` | 权限、审批、依赖、门禁阻断。 | `blocker_type`、`blocker_refs`、`required_actions`。 |
| `task_run.cancelled` | 用户或系统取消。 | `cancelled_by`、`reason`。 |

### 5.2 Dagster 映射事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `dagster.run_request.created` | 生成可审计运行草稿。 | `job_name`、`asset_selection`、`partition_key`、`run_config`、`tags`、`retry_policy`、`asset_check_selection`。 |
| `dagster.run.submitted` | RunRequest 发送到 Dagster。 | `dagster_run_id`、`run_key`。 |
| `dagster.asset.materialized` | 资产生成记录完成。 | `asset_key`、`partition_key`、`materialization_id`、`storage_refs`、`metadata`。 |
| `dagster.asset_check.completed` | 质量检查完成。 | `asset_key`、`check_name`、`status`、`severity`、`metrics`。 |
| `dagster.partition.failed` | 分区执行失败。 | `asset_key`、`partition_key`、`op_name`、`error`、`retryable`。 |
| `dagster.backfill.requested` | 回填草稿审批通过。 | `asset_selection`、`partition_range`、`impact_scope`、`approval_id`。 |

### 5.3 Agent Run 事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `agent_run.requested` | 生成映射建议、标签候选、冲突解释、回填建议等。 | `agent_name`、`agent_version`、`input_refs`、`risk_level`、`automation_level`。 |
| `agent_run.context_built` | MySQL 与 Qdrant 上下文构造完成。 | `context_refs`、`qdrant_hit_count`、`redaction_profile`。 |
| `agent_run.tool_call.started` | 调用工具、模型服务或业务 API。 | `tool_call_id`、`tool_name`、`tool_version`、`input_hash`。 |
| `agent_run.tool_call.completed` | 工具调用成功。 | `tool_call_id`、`output_refs`、`latency_ms`、`cost`。 |
| `agent_run.tool_call.failed` | 工具调用失败。 | `tool_call_id`、`error`、`retryable`、`fallback_used`。 |
| `agent_run.decision.created` | 生成结构化决策。 | `decision_id`、`decision_type`、`confidence`、`evidence_refs`、`recommended_action`。 |
| `agent_run.human_review_required` | 高风险或低置信需人审。 | `review_task_id`、`reason`、`priority`、`required_actions`。 |
| `agent_run.completed` | Agent 输出已写候选/草稿/人审等对象。 | `output_refs`、`trace_refs`、`metrics`。 |
| `agent_run.failed` | Agent 执行失败。 | `error`、`failed_phase`、`retryable`。 |

Agent 输出对象只允许以下类型：

- `LabelCandidate`
- `BoundaryCandidate`
- `SpeakerCandidate`
- `EventLinkCandidate`
- `PromptSuggestion`
- `RuleCandidate`
- `ChangeSetDraft`
- `EvalRunDraft`
- `HumanReviewTask`
- `BackfillDraft`
- `DagsterRunDraft`
- `InsightFact`
- `TraceRef`

### 5.4 模型服务事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `model_service.invocation_requested` | VAD、Diar、ASR、声纹、Tagger、LLM Judge 等调用前。 | `service_name`、`provider`、`model_version`、`input_refs`、`timeout_ms`、`fallback_policy`。 |
| `model_service.invocation_succeeded` | 模型服务返回成功并写入产物引用。 | `output_refs`、`quality_metrics`、`latency_ms`、`cost`。 |
| `model_service.invocation_failed` | 服务失败。 | `error`、`retryable`、`provider_status`、`fallback_candidates`。 |
| `model_service.degraded` | 主 provider 失败后降级。 | `from_provider`、`to_provider`、`reason`、`quality_impact`。 |

边界约束：

- ASR 只产出 `transcript_asset`、`transcript_segments`、`word_timestamps`、`asr_quality`。
- Diarization 只产出 `speaker_turns`、`speaker_embeddings_ref`、`speaker_quality`。
- `speaker_transcript_view` 是派生资产，由转写片段与说话人轨按时间重叠生成。
- Agent 可以解释冲突、推荐重跑或创建人审任务，不能直接覆盖最终身份结果。

### 5.5 对象存储事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `object_storage.upload_requested` | 原始音频、处理后 WAV、ASR JSON、Diar JSON、证据包、报告或导出文件准备上传。 | `storage_profile_id`、`bucket_alias`、`object_key`、`content_type`、`size_bytes`、`checksum_sha256`。 |
| `object_storage.upload_succeeded` | 上传完成。 | `asset_ref`、`version_id`、`etag`、`retention_class`。 |
| `object_storage.upload_failed` | 上传失败。 | `error`、`retryable`、`retry_count`。 |
| `object_storage.lifecycle_applied` | 保留、归档、删除策略生效。 | `asset_ref`、`retention_policy_id`、`expires_at`。 |

`asset_ref` 必须使用引用，不把长期可访问 URL 写入事件：

```json
{
  "asset_ref": {
    "asset_key": "auris/audio/raw_recordings",
    "storage_provider": "s3",
    "bucket_alias": "tenant-auris-audio",
    "object_key": "sales_quality/2025-05-26/aurora-center/raw.wav",
    "version_id": "3Lg...",
    "checksum_sha256": "sha256...",
    "content_type": "audio/wav",
    "size_bytes": 38123312,
    "pii_level": "sensitive",
    "retention_policy_id": "ret_audio_180d"
  }
}
```

### 5.6 Qdrant 索引事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `qdrant.index_requested` | 知识源、证据包、标签样本、badcase 或声纹候选需要索引。 | `source_type`、`source_id`、`index_version`、`collection`、`embedding_model`。 |
| `qdrant.chunking_completed` | 切片完成。 | `chunk_count`、`chunk_refs`、`content_hash`。 |
| `qdrant.embedding_completed` | embedding 生成完成。 | `embedding_model`、`vector_dim`、`chunk_count`、`cost`。 |
| `qdrant.upsert_succeeded` | 向量写入完成。 | `collection`、`point_count`、`index_version`。 |
| `qdrant.quality_check_completed` | 召回质量门禁完成。 | `metrics`、`passed`、`failed_reasons`。 |
| `qdrant.index_failed` | 索引失败。 | `error`、`failed_stage`、`retryable`。 |

Qdrant payload 必须包含：

- `tenant_id`
- `project_id`
- `source_id`
- `source_type`
- `asset_key`
- `trace_id`
- `version`
- `evidence_id`
- `label_version`
- `partition_key`
- `business_ref`

禁止写入：

- 审批结果。
- 发布状态。
- 权限策略。
- 原始密钥。
- 完整未脱敏客户信息。

### 5.7 ASR 热词治理事件

热词分析、provider 构建、评测和发布全部通过 RunRecord + Outbox 发起，事件必须继承通用 envelope 中的 `event_id`、`tenant_id`、`project_id`、`trace_id`、`root_trace_id`、`aggregate_type`、`aggregate_id`、`resource_version` 和 `occurred_at`。worker 使用 `event_id + aggregate_id + resource_version` 去重，重试耗尽进入死信，不能吞掉失败。请求事件只携带范围与冻结引用；指标、门禁、编译产物和发布结果只能由受信 worker 完成回执物化，禁止把 API 客户端自报值当真值。

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `hotword_pack.created` | 创建逻辑热词包。 | `pack_id`、`name`、`language`、`domain`、`resource_version`。 |
| `hotword_pack_version.created` | 从可选基线创建 draft 版本。 | `pack_id`、`version_id`、`version`、`baseline_version_id`、`resource_version`。 |
| `hotword_analysis.requested` | 请求按时间、门店、provider、模型和词包版本分析热词质量。 | `run_id`、`date_from`、`date_to`、`store_id`、`provider`、`model_version`、`hotword_pack_version_id`。 |
| `hotword_metrics.materialized` | MySQL 预计算快照和 ASR 热词 Badcase 投影已原子物化。 | `run_id`、`snapshot_ids`、`badcase_ids`、`metric_definition_version`、`source_storage_object_ids`。 |
| `asr_annotation.correction-recorded` | 调听端显式 ASR 文本修正已写入不可变观察并关联 Badcase。 | 只含 `correction_id`、`annotation_id`、词级证据/词包/Badcase 引用、哈希和 Trace；禁止携带识别原文或正确文本。该事件只驱动 discovery 投影，不直接触发可信指标或发布。 |
| `hotword_pack_version.build-requested` | draft 版本通过静态检查后请求 provider 编译。 | `run_id`、`version_id`、`expected_resource_version`、`content_sha256`、`manifest_storage_object_id`、`target_provider`；不含编译结果。 |
| `hotword_pack_version.built` | 受信构建完成回执验证输入哈希后冻结 provider 产物。 | `run_id`、`version_id`、`content_sha256`、`manifest_storage_object_id`、`compiled_provider`、`provider_artifact_ref`、`artifact_sha256`。 |
| `hotword_pack_version.eval-requested` | 候选版本进入锁定评测集的影子复测。 | `run_id`、`version_id`、`eval_dataset_id`、`content_sha256`、`manifest_storage_object_id`、`compiled_provider`、`provider_artifact_ref`；不得包含 baseline/candidate 指标或 gate。 |
| `hotword_pack_version.eval-completed` | 受信完成回执与全部冻结绑定一致后物化指标和门禁。 | `run_id`、`version_id`、`eval_dataset_id`、`locked=true`、`baseline_metrics`、`candidate_metrics`、`gate`、`result_storage_object_ids`。 |
| `hotword_pack_version.publish-requested` | 已通过评测并获模型负责人批准的版本请求项目管理员人工发布。 | `run_id`、`version_id`、`eval_run_id`、`expected_resource_version`、`content_sha256`、`compiled_provider`、`provider_artifact_ref`、`model_approved_by`、`project_admin_confirmed_by`、`confirmation`。 |
| `hotword_pack_version.published` | 受信发布完成回执再次验证门禁与冻结绑定，逻辑包当前版本被切换并创建 TaskVersion 草稿。 | `run_id`、`pack_id`、`version_id`、`eval_run_id`、`project_admin_confirmed_by`、`task_version_id`、`content_sha256`、`compiled_provider`、`provider_artifact_ref`。 |
| `hotword_pack_version.rollback-requested` | 模型负责人请求恢复同包历史已发布版本，首次 Outbox 投递由门禁阻断。 | `run_id`、`pack_id`、`source_version_id`、`target_version_id`、`source_resource_version`、`target_resource_version`、`pack_resource_version`、三者 `root_trace_id`、`reason`、`requested_by`。 |
| `hotword_pack_version.rolled-back` | 不同自然人的项目管理员批准后，worker 重新验证冻结绑定并原子恢复逻辑包指针。 | `run_id`、`pack_id`、`from_version_id`、`to_version_id`、`reason`、`approval_id`、`requested_by`、`approved_by`、`root_trace_id`。 |

`hotword_pack_version.published` 只代表词包版本成为可供生产任务选择的不可变版本；它不发布 TaskVersion、不自动切流，也不覆盖任何历史 ASR、人工纠正或 Badcase 资产。`hotword_pack_version.rolled-back` 也只改变词包版本状态与逻辑包当前指针，不自动切换既有 TaskVersion 或覆盖资产。构建、评测或发布的协议级 dispatch 成功最多把 RunRecord 推进到 `submitted`，不能伪装业务完成；完成回执必须来自已登记 adapter 或通过签名/nonce/scope 校验的 worker 身份。

### 5.8 标签闭环、PromptOps 与发布事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `label_observation.created` | 受信模型回执物化单条不可变 Observation。 | `observation_id`、subject/evidence、label/Prompt/Schema/model/calibration versions、服务端恢复的 source family/type/provider/adapter/correlation group、证据验证、input/output SHA-256、根 `trace_id`。 |
| `label_calibration_version.created` | 自然人创建 draft/published 服务端校准版本。 | calibration/label/source/method/version、GoldSetVersion、sample count、parameters/metrics、training/content SHA-256、status、Trace。 |
| `label_extraction_run.materialized` | 抽取完成回执通过强 Schema、全部 Observation 落库并自动聚合。 | `extraction_run_id`、`observation_ids/count`、`aggregation_run_id`、`aggregate_ids/review_task_ids`、锁定 Manifest、completion receipt、根 `trace_id`。 |
| `label_aggregation_policy.created` | 创建不可变聚合策略版本。 | `policy_version_id`、`label_version_id`、mode、thresholds、calibration versions、canonical SHA-256。 |
| `label_aggregate.created` | 确定性聚合产生 Aggregate。 | `aggregate_id`、policy/calibration versions、members/contributions、decision、reason codes、deterministic hash、review task ref。 |
| `label_aggregation_run.materialized` | 一批 Observation 聚合、Taxonomy 建议和候选人审任务已原子物化。 | `aggregation_run_id`、input/result SHA-256、aggregate/taxonomy/review refs、status、`trace_id`。 |
| `label_taxonomy_suggestion.created` | 自由文本无法映射到锁定标签版本。 | `suggestion_id`、normalized/raw labels、observation refs、review task ref、`status=pending`。 |
| `human_review.decision.created` | 单个候选级人审任务形成唯一终态。 | `decision_id`、`review_task_id`、显式 `target_refs`、decision、field diff、actor、继承的根 `trace_id/source_trace_id` 与本次 `action_trace_id`。 |
| `human_review_decision_batch.completed` | 低风险同 cohort 批量决策完成。 | `batch_id`、cohort、`counts`、逐项 `success/skipped/failed` 与原因码。 |
| `label_fact.created` | Human Loop 接受/修改、L2 安全门禁自动接受或 approved recompute 物化。 | `fact_id`、logical key/revision、稳定 label/version、aggregate/human-decision/recompute-item 三选一 source、`fact_occurred_at/recorded_at`、supersedes、evidence/content SHA、FactSet namespace、根/action Trace。 |
| `label_version.deprecation-requested` | 自然人提交制品废弃并完成 impact preflight。 | version/replacement、`mapping_bundle_id`、active/draining 环境与在途运行、expected resource version、approval、mutation/root Trace。 |
| `label_version.deprecated` | 所有受保护环境已停止引用，制品状态与审计/Outbox 同事务提交。 | version/replacement/bundle、artifact timestamp、reason、resource version、Audit/root Trace。 |
| `label_mapping_bundle.published` | 完整 edge 闭包编译、校验并经自然人批准。 | source version set、target、edge IDs、compiled path SHA、compiler version、canonical SHA、approver、Trace。 |
| `label_recomputation.requested` | split/语义变化请求候选重算。 | target version/bundle、source FactSet/Asset Head generations、partitions、fact cutoff、budget、Manifest SHA、Trace。 |
| `label_fact_set.promoted` | 完整 candidate FactSet/Asset Manifest 经审批后单事务 CAS。 | old/new FactSet 与 Asset Head generations、manifest SHA、approval、rollback target、Trace。 |
| `insight_metric.materialized` | 受信回执与冻结 scope/Manifest 一致并追加不可变 MetricResult。 | taxonomy mode、source/target versions、bundle、FactSet generation、fact_as_of、definition/scope/source/content SHA、result IDs、comparability、Trace。 |
| `feedback_example.created` | 人工接受、修改或拒绝形成反馈样本。 | `feedback_example_id`、target、feedback type、reason、field diff、`gold_status=candidate`。 |
| `badcase.created` | 人工修改/拒绝或失败簇形成 labeling/prompt-optimization Badcase。 | `badcase_id`、capability、failure reason、source/version refs、root trace。 |
| `prompt_asset.created` | 创建逻辑 Prompt 资产。 | `prompt_asset_id`、capability、label version、status、`trace_id`。 |
| `prompt_version.created` | 真实模板、Schema、参数、diff 与来源 badcase 固化为强版本。 | `prompt_version_id`、asset/parent/label/schema/model versions、content SHA-256、status。 |
| `prompt_review.submission.created` | 一名自然人提交密封 Prompt 候选审核。 | 只公开 submission/candidate/review task IDs、sealed 状态和已收数量；不得把密封 decision/diff 放入事件。 |
| `prompt_review.adjudication_requested` | 两份密封结论不一致。 | candidate/review task、submission IDs、`status=awaiting-adjudication`。 |
| `prompt_review.adjudication.created` | 独立仲裁员形成 Prompt 候选终态。 | adjudication/candidate/review task IDs、decision、status；后续生成 feedback 和 locked eval。 |
| `prompt_version_candidate.revision_created` | 两份密封 modified 一致，或仲裁结论为 modified。 | child PromptVersion/candidate ID、`parent_version_id`、content SHA、structured diff、全新 double-blind task、根/action Trace；父候选保持 `revision-required`。 |
| `label_optimization.trigger_scan.completed` | 15 分钟/日/周扫描完成。 | `scan_id`、run id、trigger hash/reasons、metrics/provenance、locked versions、budget、blocked reasons、next action。 |
| `agent_run.requested` | 触发扫描通过单活、去重、冷却与预算门禁。 | `run_id`、locked versions、trigger kind/hash、budget、candidate/eval stages、`trace_id`。 |
| `label_optimization_schedule.created` / `label_optimization_schedule.updated` | 项目管理员锁定/更新自动优化计划。 | schedule、resource version、完整 Bundle、15m/daily/weekly due、budget、status、Trace。 |
| `label_optimization.round.created` | worker 获取 scope claim 并创建本轮真实候选生成运行。 | schedule/root/generation run、round 1–3、锁定版本、候选数/预算、Trace。 |
| `label_optimization.round.hard_stopped` | reconcile 发现 session 最早 Round 起算的墙钟已达到 2 小时。 | round/root/generation/eval runs、`time_budget_exceeded`、elapsed seconds；所有未终态子运行转 blocked，schedule 释放 active run。 |
| `release_deployment.created` | 发布 Bundle 冻结并完成离线门禁核验。 | `deployment_id`、bundle SHA-256、locked versions、rollback target、`status=pending|blocked`、pending command/run、blockers。 |
| `release_deployment.command-requested` | 创建 publish/approve-gray/promote/rollback 两阶段执行命令。 | command/run/deployment/target、action、command/bundle SHA、expected deployment status、expected head generation/id/hash、reason、Trace。 |
| `release_deployment.command-acknowledged` | 受信完成回执精确绑定命令，重验 Bundle 并通过 head CAS。 | command/receipt/deployment/action、action 对应的 `shadowing/gray-releasing/completed/rolled-back` 状态、new head/generation、完成来源、Trace。 |
| `release_deployment.command-blocked` | ACK 到达时 Bundle、指针或 expected head CAS 已漂移。 | command/deployment/action、blockers、receipt；不改变有效 head，active slot 关闭。 |
| `release_deployment.command-failed` | 受信执行回执明确失败。 | command/deployment/action、failure receipt/error；不改变有效 head，部署回到可解释 blocked。 |
| `release_bundle_head.bootstrapped` | 自然人项目管理员确认首次 production LKG。 | 初始 head/deployment/bundle、generation=1、bootstrapped=true、冻结版本和 Trace；仅无 head 且可重验的 blocked Bundle。 |
| `release_deployment.monitor-sample-recorded` | system 写入未触发硬退化的类型化在线样本。 | sample ID/hash/window、`stable_window_complete`、typed metrics、`status=monitoring`、Trace；稳定窗口事实同步写入权威 monitor metrics，但不自动 promote。 |
| `release_deployment.auto-rollback-requested` | 在线样本命中硬阈值且 Bundle 有稳定锁定回滚目标。 | deployment/target、violations、sample、rollback command/run；当前 `status=materializing`、rollout=0，尚未声称回滚完成。 |
| `release_deployment.auto-rollback-blocked` | 命中硬阈值但缺少/无效回滚目标。 | violations、sample ID、`status=blocked`、`automatic_action=safe-stop-blocked`、rollout=0。 |

事件链约束：

- `label_extraction_run` 只有在真实完成回执和 Observation 全部物化后才发 `materialized`；ToolCall 计划、队列提交或 outbox `processed` 不能冒充完成。
- 抽取 `materialized` 与自动 AggregationRun/aggregate/review refs 在同一事务完成；消费者不得另建第二个同输入聚合运行。
- Aggregate 必须携带锁定策略/校准版本、全部成员贡献和确定性哈希；相同输入与版本重放必须得到相同哈希。
- 人审事件只作用于任务中的显式 `target_refs`，继承 source/root Trace，并另记 action Trace。普通接受的 FeedbackExample 仍是 Gold candidate，只有双评一致或仲裁完成才能另行锁定为 Gold。
- Outbox 消费者以 `event_id` 去重，并以业务幂等键保护投影写入，实现 effectively-once；重复投递不得生成第二个事实、反馈、Badcase 或发布迁移。
- 发布晋级和 Prompt/Taxonomy/聚合策略审批必须由自然人完成；系统只能自动阻断或按硬阈值执行已授权回滚。
- ReleaseCommand dispatch/Outbox processed 不是发布完成；只有 ACK 回显命令与 Bundle 全绑定、重验成功且 `expected_head_generation/deployment/hash` CAS 通过，才能更新环境唯一 active head。
- `label_version.deprecated` 不能批量修改历史 Observation/Aggregate/Fact/MetricResult/Report；`label_fact_set.promoted` 不能循环逐 Fact 切 Head。

### 5.9 外部回写事件

| 事件 | 触发条件 | 关键 payload |
| --- | --- | --- |
| `external_callback.requested` | 输出节点需要回写外部系统。 | `callback_binding_id`、`endpoint_ref`、`request_mapping_ref`、`payload_hash`、`idempotency_key`。 |
| `external_callback.sent` | 请求已发出。 | `external_callback_id`、`http_method`、`endpoint_host`、`attempt`。 |
| `external_callback.succeeded` | 外部系统返回成功或幂等已存在。 | `status_code`、`external_receipt_id`、`response_hash`。 |
| `external_callback.failed` | 单次回写失败。 | `status_code`、`error`、`retryable`、`next_retry_at`。 |
| `external_callback.dead_lettered` | 超过重试上限或不可重试。 | `dead_letter_id`、`final_error`、`manual_action_required`。 |

典型回写：

- 处理后 WAV 上传 OBS / S3 后回传 URL。
- 标签结果回调。
- 复核结论回调。
- 证据包导出。
- 告警队列写入。

外部回写约束：

- 回写前必须校验租户、项目、任务版本、输出绑定、权限和审批状态。
- 请求体由 `request_mapping_ref` 指向版本化映射，不在事件中存完整敏感 payload。
- 回写响应保存摘要、状态码和回执 ID；敏感响应体脱敏后保存。
- 同一 `idempotency_key` 的重复回写必须被识别为同一业务结果。

## 6. Dagster RunRequest 映射

业务 `TaskRun` 生成 Dagster `RunRequest` 时使用以下映射：

| 业务字段 | Dagster 字段 | 说明 |
| --- | --- | --- |
| `task_version.job_name` | `job_name` | 已发布任务版本固化的 Job。 |
| `task_run.partition_key` | `partition_key` | 与业务分区一致。 |
| `task_version.asset_selection` | `asset_selection` | 资产选择，来自任务版本快照。 |
| `task_run.run_config` | `run_config` | provider、模型版本、标签版本、自动化等级等。 |
| `task_run.idempotency_key` | `run_key` | 防止重复运行。 |
| `task_run.trace_id` | `tags.trace_id` | 链路追踪。 |
| `task_run.run_id` | `tags.run_id` | 业务运行 ID。 |
| `tenant_id` | `tags.tenant_id` | 租户隔离。 |
| `project_id` | `tags.project_id` | 项目隔离。 |
| `automation_level` | `tags.automation_level` | L0-L4。 |
| `human_loop_required` | `tags.human_loop` | `required` 或 `none`。 |

示例：

```json
{
  "job_name": "evidence_dataflow_canvas_v3_job",
  "partition_key": "2025-05-26|aurora-center",
  "asset_selection": [
    "auris/audio/raw_recordings",
    "auris/model/asr_transcripts",
    "auris/label/event_tags"
  ],
  "run_config": {
    "provider": "audio_intelligence_router",
    "prompt_version": "prompt_deal_intent_v06_rc1",
    "tag_version": "v1.9.0-rc2",
    "automation_level": "L2"
  },
  "tags": {
    "tenant_id": "auris",
    "project_id": "sales_quality",
    "run_id": "tr_01jz...",
    "trace_id": "trace_20250526_122718",
    "idempotency_key": "auris:sales_quality:task_version:tv_123:run_once:v3:2025-05-26|aurora-center:sha256_abcd",
    "human_loop": "required"
  }
}
```

Dagster 回调到后端时必须带回：

- `dagster_run_id`
- `run_key`
- `asset_key`
- `partition_key`
- `trace_id`
- `run_id`
- `materialization_id` 或 `asset_check_id`

## 7. Outbox 与消费模型

第一阶段推荐使用 MySQL Outbox + Worker：

1. BFF 在同一事务中写业务表和 `outbox_events`。
2. Worker 扫描 `outbox_events.status=pending AND available_at <= now()`，以数据库租约认领后投递到内部队列或直接执行。
3. 消费成功后标记 `processed`，写 `processed_at` 并清理认领租约。
4. 可重试失败回到 `pending` 并推进 `available_at`；不可重试或次数耗尽时标记 `failed`，保留投递尝试与死信诊断。

`outbox_events` 的数据库字段与当前运行时模型保持一致；完整业务事件信封存放在 `payload` 中，不再建立另一张同义 outbox 表：

```text
event_id
tenant_id
project_id
event_type
aggregate_type
aggregate_id
payload
dispatch_idempotency_key
dispatch_request_sha256
status
attempt_count
reconcile_attempt_count
delivery_state
last_error
available_at
claim_token
claimed_by
claimed_at
lease_generation
lease_expires_at
created_at
processed_at
```

其中 `payload` 必须包含 `event_id`（业务事件 ID）、`event_version`、`occurred_at`、`tenant_id`、`project_id`、`request_id`、`trace_id`、`idempotency_key`、`resource_version`、`subject` 和 `data`；标签事实事件还必须包含本规范前文冻结的版本、来源清单、映射和双时间字段。数据库自增 `event_id` 仅用于本地投递排序，不能替代业务事件 ID。

消费方幂等表最小字段：

```text
consumer_name
event_id
idempotency_key
status
result_ref
first_seen_at
last_seen_at
```

## 8. 失败队列与重试策略

### 8.1 错误分类

| 分类 | 示例 | 默认重试 |
| --- | --- | --- |
| `transient` | 网络超时、外部 5xx、Dagster 暂时不可用、对象存储限流。 | 是 |
| `dependency` | 上游资产未生成、审批未完成、配额不足。 | 条件，通常转 `blocked`。 |
| `validation` | Schema 不合法、字段映射缺失、分区格式错误。 | 否 |
| `permission` | 跨租户访问、权限不足、密钥无权限。 | 否 |
| `conflict` | 幂等键冲突、版本已发布、状态不可迁移。 | 否 |
| `provider` | 模型 provider 失败、质量低于阈值。 | 条件，可降级或重试。 |

### 8.2 重试节奏

默认指数退避并加随机抖动：

| attempt | 延迟 |
| --- | --- |
| 1 | 1 分钟 |
| 2 | 5 分钟 |
| 3 | 15 分钟 |
| 4 | 1 小时 |
| 5 | 6 小时 |

超过 5 次进入死信队列。高成本模型调用可按服务配置降低最大重试次数。

### 8.3 死信记录

`dead_letter_queue` 最小字段：

```text
dead_letter_id
event_id
event_type
tenant_id
project_id
run_id
trace_id
partition_key
idempotency_key
failed_stage
error_code
error_message
payload_ref
attempt_count
manual_action_required
created_at
resolved_at
resolved_by
resolution
```

死信处理要求：

- BFF 能在运行详情中展示失败阶段、错误码、影响对象和可执行动作。
- 可重放死信事件时必须沿用原 `idempotency_key`，并追加新的 `retry_event_id`。
- 人工修复后必须写审计日志，记录修复人、原因、影响对象和结果。

## 9. BFF 返回运行对象约定

同步动作返回最小结构：

```json
{
  "status": "pending",
  "task_run_id": "tr_01jz...",
  "run_id": "tr_01jz...",
  "trace_id": "trace_20250526_122718",
  "partition_key": "2025-05-26|aurora-center",
  "idempotency_key": "auris:sales_quality:task_version:tv_123:run_once:v3:2025-05-26|aurora-center:sha256_abcd",
  "affected_objects": [
    {
      "type": "data_asset",
      "id": "auris/label/event_tags"
    }
  ],
  "next_actions": [
    "view_run_detail"
  ]
}
```

异步详情接口必须返回：

- 当前状态。
- 阶段时间线。
- 错误对象。
- 重试次数和下一次重试时间。
- 影响对象。
- `trace_id`。
- 面向运维权限的内部执行映射摘要；普通产品界面不显示 Dagster 名称或画布语言。
- 人审或审批入口。

## 10. 阶段 1 必须落地的事件链路

当前开源开发基线已经验证到 `RunRecord -> outbox -> Dagster-compatible GraphQL run request submitted`；`dagster.asset.materialized`、分区完成态和生产 Dagster 回调仍属于后续目标链路，不能用本地 receipt 代替。

第一阶段至少实现以下链路：

1. `POST /api/v1/task-runs` -> `task_run.requested` -> `dagster.run_request.created` -> `dagster.run.submitted` -> `dagster.asset.materialized` / `dagster.partition.failed` -> `task_run.succeeded` / `task_run.failed`。
2. `POST /api/v1/label-extraction-runs` -> `agent_run.requested` -> 真实模型回执 -> `label_observation.created` -> `label_extraction_run.materialized`（同事务自动创建确定性 AggregationRun）-> `label_aggregate.created` / `label_taxonomy_suggestion.created` -> Human Loop 或 L2 `label_fact.created`；客户端不得再次 POST 同输入聚合。
3. `human_review.decision.created` -> `feedback_example.created` / `badcase.created` -> `POST /api/v1/label-optimization-trigger-scans` -> `label_optimization.trigger_scan.completed` -> `agent_run.requested` -> `prompt_version.created` -> 锁定 EvalRun -> `release_deployment.created` -> `release_deployment.command-requested` -> 受信 `release_deployment.command-acknowledged` 或 blocked/failed -> 在线监控 -> promote/rollback command 链。旧 `POST /api/v1/label-optimization-runs` 继续作为兼容入口，但不代表发布完成。
4. LabelVersion 退出：deprecation preflight -> `label_version.deprecation-requested` -> 各环境 ReleaseCommand 切换 Head/旧 activation draining -> 在途运行完成或显式取消 -> `label_version.deprecated`；任何一步都不改历史事实或快照。
5. 跨版本统计：mapping edge/bundle 编译审批 -> `label_mapping_bundle.published` -> `POST /api/v1/insights/metric-runs` -> `insight_metric.materialized` -> Report 引用固定 metric result IDs/scope hash。split 走 `label_recomputation.requested` -> candidate FactSet -> `label_fact_set.promoted` -> 新指标快照。
6. `POST /api/v1/knowledge-indexes/{id}/build-runs` -> `qdrant.index_requested` -> `qdrant.chunking_completed` -> `qdrant.embedding_completed` -> `qdrant.upsert_succeeded` -> `qdrant.quality_check_completed`。
7. `POST /api/v1/data-assets/{asset_key}/backfills` -> `dagster.backfill.requested` -> 分区运行事件 -> 回填结果写入候选资产版本。
8. 输出节点触发 `external_callback.requested` -> `external_callback.sent` -> `external_callback.succeeded` 或 `external_callback.dead_lettered`。
