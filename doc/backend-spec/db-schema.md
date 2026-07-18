# Auris Flow MySQL 第一阶段表设计

## 1. 设计边界

本文档定义 Auris Flow 第一阶段 MySQL 表设计，不写完整 SQL 迁移。目标是让后端可以据此实现 ORM model、repository、BFF projection、异步运行状态和 Qdrant 回跳关系。

第一阶段固定边界：

- MySQL 保存权威业务状态、权限、配置、运行状态、审计、发布门禁和对象存储引用。
- 对象存储保存大文件和模型产物：原始音频、处理后 WAV、ASR JSON、Diar JSON、证据包、报告、导出文件。
- Qdrant 只保存向量和 payload，用于知识、证据、标签样本、badcase、可选声纹召回。
- Redis 只做缓存、短期锁、幂等 key、限流和短期任务状态，不作为权威状态。
- 不使用 ClickHouse。洞察和运营大盘第一阶段使用 MySQL 聚合表、预计算结果和 Redis 缓存。

## 2. 通用字段规范

### 2.1 ID 与时间

| 字段 | 建议类型 | 规则 |
| --- | --- | --- |
| 主键 | `varchar(26)` | 推荐 ULID，按时间有序，便于游标分页。 |
| `tenant_id` | `varchar(26)` | 所有业务表必带，租户级全局表也必须带。 |
| `project_id` | `varchar(26)` nullable | 项目级业务表必填。租户全局配置允许为空，但必须通过绑定表下发。 |
| `trace_id` | `varchar(64)` | 高风险对象、运行对象、写操作、外部回写必填。 |
| `version` | `int` 或 `varchar(64)` | 乐观锁用 `int`，业务版本用 `varchar`，不要混用。 |
| `created_at` / `updated_at` | `datetime(3)` | 所有表必带。 |
| `created_by` / `updated_by` | `varchar(26)` nullable | 系统任务可写 `system_user_id` 或服务账号。 |
| `deleted_at` | `datetime(3)` nullable | 需要软删除的配置和业务对象使用。 |

### 2.2 通用索引

项目级表默认建立：

- `idx_{table}_tenant_project_status_updated (tenant_id, project_id, status, updated_at, id)`
- `idx_{table}_tenant_project_created (tenant_id, project_id, created_at, id)`
- `idx_{table}_trace_id (trace_id)`

大列表接口使用游标分页，游标建议为 `updated_at + id` 或业务分区字段 + `id`，避免大 offset。

### 2.3 状态枚举

统一运行状态：

- `pending`
- `running`
- `success`
- `failed`
- `blocked`
- `cancelled`

资产业务状态：

- `not_generated`
- `generating`
- `generated`
- `partial_failed`
- `failed`
- `backfill_required`
- `expired`
- `archived`

人审状态：

- `pending`
- `accepted`
- `modified`
- `rejected`
- `cancelled`

发布状态：

- `draft`
- `candidate`
- `pending`
- `materializing`
- `shadowing`
- `gray-releasing`
- `monitoring`
- `completed`
- `blocked`
- `rolled-back`
- `superseded`
- `archived`

## 3. 基础上下文与权限

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `tenants` | 租户主表，最高隔离单元 | `tenant_id`、`name`、`slug`、`status`、`plan`、`timezone`、`locale` | `uk_tenants_slug(slug)`；`idx_tenants_status(status)` | `status: active/trial/suspended/archived`；审计字段必带；无对象存储；不入 Qdrant。 |
| `tenant_quotas` | 租户资源配额 | `quota_id`、`tenant_id`、`max_projects`、`max_members`、`max_storage_bytes`、`max_monthly_audio_seconds`、`max_concurrent_runs`、`enabled_model_refs` JSON | `uk_tenant_quotas_tenant(tenant_id)` | 审计字段必带；高风险变更写 `audit_logs`。 |
| `users` | 用户账号 | `user_id`、`email`、`name`、`avatar_storage_id`、`status`、`last_login_at` | `uk_users_email(email)`；`idx_users_status(status)` | `status: active/invited/disabled`；头像可引用 `storage_objects`。 |
| `user_security_states` | 用户认证开关与权限失效版本 | `user_id`、`status`、`disabled_at`、`authz_version`、timestamps | 用户主键/FK；`authz_version>0`；active/disabled/suspended 与 `disabled_at` 一致性 CHECK | 0041 从既有用户回填 active 行；每次认证动态读取，禁用或版本变化不得依赖旧 token/session 缓存。 |
| `oidc_identities` | 通用 OIDC 主体到内部 scope 的显式映射 | `identity_id`、`issuer`、`issuer_sha256`、`subject_sha256`、`user_id`、`tenant_id`、`project_id`、`status`、`last_login_at` | `(issuer_sha256,subject_sha256)` 唯一；user/tenant/project FK；scope/status 索引 | 不保存原始 subject；未知或禁用 identity default-deny，不从 IdP claim 自动创建用户/角色。Keycloak 仅可作参考 IdP。 |
| `oidc_authorization_states` | OIDC Code + PKCE 一次性浏览器握手 | `state_sha256`、`nonce`、`code_verifier`、`return_path`、`issued_at`、`expires_at`、`consumed_at`、timestamps | state hash 主键；expiry/consumed 索引；过期与消费时间 CHECK | state 原值不落库；先原子消费再请求 token endpoint，成功后删除；return path 仅允许站内路径。 |
| `browser_auth_sessions` | BFF 不透明 HttpOnly 浏览器会话 | `browser_session_id`、`token_sha256`、`csrf_sha256`、`oidc_identity_id`、`user_id`、`tenant_id`、`project_id`、`provider`、`issued_at`、`expires_at`、`revoked_at`、`last_seen_at`、timestamps | token SHA 唯一；identity/user/tenant/project FK；user/revoked/expiry 与 identity/expiry 索引；时间 CHECK | cookie 与 CSRF 原值永不落库/日志；每次请求回查当前主体状态和项目角色，注销幂等撤销。 |
| `tenant_members` | 租户成员和角色 | `member_id`、`tenant_id`、`user_id`、`role_id`、`status`、`joined_at` | `uk_tenant_member(tenant_id,user_id)`；`idx_tenant_members_role(tenant_id,role_id)` | 权限变更写审计。 |
| `projects` | 项目主表 | `project_id`、`tenant_id`、`name`、`slug`、`scene`、`owner_user_id`、`status`、`quality_target` JSON、`default_label_version_id`、`default_model_chain` | `uk_projects_slug(tenant_id,slug)`；`idx_projects_status(tenant_id,status)` | `status: draft/running/paused/evaluating/error/archived`；不入 Qdrant。 |
| `project_members` | 项目成员权限 | `project_member_id`、`tenant_id`、`project_id`、`user_id`、`role_id`、`data_scope` JSON、`status` | `uk_project_member(project_id,user_id)`；`idx_project_members_user(tenant_id,user_id)` | 所有项目 API 入口校验此表。 |
| `roles` | 角色定义 | `role_id`、`tenant_id`、`project_id` nullable、`name`、`scope`、`permissions` JSON、`status` | `uk_roles_name(tenant_id,project_id,name)` | 租户全局角色 `project_id = NULL`。 |
| `stores` | 门店/地点主数据 | `store_id`、`tenant_id`、`project_id`、`external_store_id`、`region`、`city`、`store_name`、`org_path`、`status` | `uk_store_external(tenant_id,project_id,external_store_id)`；`idx_stores_region_city(tenant_id,project_id,region,city)` | 可同租户跨项目通过绑定复用；不直接入 Qdrant。 |
| `devices` | 收声设备和工牌设备 | `device_id`、`tenant_id`、`project_id`、`store_id`、`external_device_id`、`device_type`、`badge_no`、`position`、`status` | `uk_device_external(tenant_id,project_id,external_device_id)`；`idx_devices_store_type(store_id,device_type)` | 用于音频、串音、声纹归因。 |
| `audit_logs` | 审计日志 | `audit_log_id`、`tenant_id`、`project_id` nullable、`actor_user_id`、`action`、`object_type`、`object_id`、`before_json`、`after_json`、`ip`、`user_agent`、`trace_id` | `idx_audit_scope(tenant_id,project_id,object_type,object_id,created_at)`；`idx_audit_actor(tenant_id,actor_user_id,created_at)` | 审计不可物理删除；高风险操作必须写。 |

## 4. 连接器与数据接入

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `connectors` | 外部系统连接器 | `connector_id`、`tenant_id`、`project_id` nullable、`name`、`connector_type`、`provider`、`auth_mode`、`secret_ref_id`、`status`、`last_test_at` | `uk_connector_name(tenant_id,project_id,name)`；`idx_connectors_type_status(tenant_id,connector_type,status)` | 不保存明文密钥；连接测试写审计。 |
| `data_source_bindings` | 项目数据源绑定和资产输出映射 | `binding_id`、`tenant_id`、`project_id`、`connector_id`、`resource_type`、`asset_key`、`mapping_policy` JSON、`validation_policy` JSON、`status` | `uk_binding_resource(tenant_id,project_id,connector_id,resource_type)`；`idx_binding_asset(tenant_id,project_id,asset_key)` | 对应原型数据源、知识源和任务输入。 |
| `sync_cursors` | 增量同步游标 | `cursor_id`、`binding_id`、`cursor_key`、`cursor_value`、`updated_after`、`last_success_at`、`status` | `uk_sync_cursor(binding_id,cursor_key)` | 游标更新与 `sync_runs` 成功提交同事务。 |
| `sync_runs` | 数据源同步运行 | `sync_run_id`、`tenant_id`、`project_id`、`binding_id`、`status`、`mode`、`started_at`、`finished_at`、`records_seen`、`records_written`、`error_code`、`error_message`、`trace_id` | `idx_sync_runs_binding(binding_id,status,created_at)`；`idx_sync_runs_trace(trace_id)` | 统一运行状态；失败可重试。 |
| `source_records` | 外部原始记录索引 | `source_record_id`、`tenant_id`、`project_id`、`binding_id`、`resource_type`、`external_id`、`source_updated_at`、`raw_payload_storage_id`、`checksum`、`normalized_object_type`、`normalized_object_id`、`trace_id` | `uk_source_record(binding_id,resource_type,external_id)`；`idx_source_records_updated(binding_id,source_updated_at,source_record_id)` | 原始 payload 存对象存储；不入 Qdrant，知识源另行切片。 |
| `mapping_suggestions` | 字段映射建议 | `suggestion_id`、`tenant_id`、`project_id`、`binding_id`、`source_field`、`target_field`、`target_asset`、`join_key`、`policy`、`confidence`、`state`、`evidence_json`、`agent_run_id` | `idx_mapping_suggestions_binding(binding_id,state,confidence)` | `state: pending/confirmed/applied/rejected`；Agent 只能写建议。 |

## 5. 任务配置与执行

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `task_types` | 任务类型 | `task_type_id`、`tenant_id`、`project_id`、`name`、`scenario`、`description`、`status` | `uk_task_type_name(tenant_id,project_id,name)` | 业务流程模板。 |
| `task_canvas_versions` | 画布版本 | `canvas_version_id`、`tenant_id`、`project_id`、`task_type_id`、`version`、`graph_json`、`status`、`validation_summary` JSON | `uk_canvas_version(task_type_id,version)`；`idx_canvas_status(task_type_id,status)` | 草稿图结构可用 JSON，发布前校验节点契约。 |
| `task_versions` | 可发布任务版本 | `task_version_id`、`tenant_id`、`project_id`、`task_type_id`、`canvas_version_id`、`version`、`release_state`、`automation_level`、`rollback_to_version_id`、`published_at` | `uk_task_version(task_type_id,version)`；`idx_task_versions_state(task_type_id,release_state)` | 发布、回滚、运行都以任务版本为目标。 |
| `task_nodes` | 任务节点 | `node_id`、`tenant_id`、`project_id`、`task_version_id`、`node_key`、`node_type`、`role`、`input_contract` JSON、`output_contract` JSON、`dagster_binding` JSON、`position_json`、`status` | `uk_task_node(task_version_id,node_key)`；`idx_task_nodes_type(task_version_id,node_type)` | 节点状态可映射未配置、运行中、低置信、等待人工等 UI 状态。 |
| `task_edges` | 任务节点连线 | `edge_id`、`tenant_id`、`project_id`、`task_version_id`、`source_node_id`、`target_node_id`、`edge_type`、`condition_json` | `uk_task_edge(task_version_id,source_node_id,target_node_id,edge_type)` | 用于执行计划和血缘展示。 |
| `task_schedules` | 调度配置 | `schedule_id`、`tenant_id`、`project_id`、`task_version_id`、`mode`、`cron_expr`、`trigger_rule` JSON、`cursor_policy` JSON、`status` | `idx_task_schedules_version(task_version_id,status)` | `mode: cron/manual/data_arrival/oneoff_backfill`。 |
| `task_experiments` | A/B 或影子实验 | `experiment_id`、`tenant_id`、`project_id`、`task_version_id`、`name`、`arm_key`、`traffic_percent`、`metric_policy` JSON、`status` | `uk_experiment_arm(task_version_id,arm_key)` | 指标写入评测和洞察。 |
| `run_records`（`run_type=task_run`） | 当前任务运行权威主表与控制运行统一投影 | `run_id`、scope、`run_type`、`status`、`run_key`、`partition_key`、`trace_id`、`submitted_at`、`started_at`、`finished_at`、`deadline_at`、`next_status_sync_at`、`monitor_generation`、`engine_status`、`engine_status_observed_at`、`status_version`、`cancel_requested_at`、`cancel_reason`、`terminal_reason`、`payload` | `uq_run_records_scope_id`；`ix_run_records_monitor_deadline(run_type,status,deadline_at)`；`ix_run_records_monitor_sync_due(run_type,status,next_status_sync_at)`；`ix_run_records_monitor_control_active(tenant_id,project_id,run_key(128),run_type,status)`；`ix_run_records_type_status_finished(run_type,status,finished_at)`；兼容查询索引 `ix_run_records_status_deadline`、`ix_run_records_status_sync_due`、`ix_run_records_engine_status` | MySQL 权威状态；取消/状态同步各使用独立 RunRecord + Outbox；monitor 每个事务最多锁 10 条 source，以 `FOR UPDATE SKIP LOCKED` 和单调代次收敛多 Worker，并按小批量一次加载 active/deterministic controls，避免逐 source 查询；`run_key` 的 MySQL 128 字符前缀完整覆盖 `run_id` 上限，同时控制 utf8mb4 组合索引宽度；24 小时任务完成时长指标使用组合索引限定扫描；终态单向；不入 Qdrant。 |
| `task_run_steps` | 任务运行步骤 | `step_id`、`tenant_id`、`project_id`、`task_run_id`、`node_id`、`step_key`、`status`、`started_at`、`finished_at`、`input_ref`、`output_ref`、`error_message` | `uk_task_run_step(task_run_id,step_key)`；`idx_task_run_steps_status(task_run_id,status)` | 可展示节点运行、失败、重试和低置信。 |
| `output_bindings` | 输出资产和外部回写配置 | `output_binding_id`、`tenant_id`、`project_id`、`task_version_id`、`sink_type`、`asset_key`、`endpoint_ref`、`request_mapping` JSON、`retry_policy` JSON、`idempotency_policy` JSON、`status` | `idx_output_bindings_task(task_version_id,sink_type,status)` | 高风险回写必须走门禁。 |
| `external_callback_receipts` | 外部回写回执 | `callback_receipt_id`、`tenant_id`、`project_id`、`output_binding_id`、`task_run_id`、`target_object_type`、`target_object_id`、`callback_url_ref`、`idempotency_key`、`status`、`remote_trace_id`、`attempt_count`、`last_error`、`trace_id` | `uk_callback_idempotency(tenant_id,project_id,idempotency_key)`；`idx_callbacks_status(tenant_id,project_id,status,updated_at)` | 回写失败进入重试或死信；可引用对象存储中的请求/响应快照。 |

## 6. 音频证据、调听与事件关联

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `audio_sessions` | 音频会话/接待会话 | `audio_session_id`、`tenant_id`、`project_id`、`store_id`、`customer_ref`、`primary_employee_id`、`started_at`、`ended_at`、`status`、`label_version_id`、`model_version`、`trace_id` | `idx_audio_sessions_scope(tenant_id,project_id,store_id,started_at,audio_session_id)`；`idx_audio_sessions_customer(tenant_id,project_id,customer_ref,started_at)` | 调听和数据管理中心对象；可入 Qdrant `evidence_segments` 的 session payload。 |
| `audio_recordings` | 原始录音文件 | `recording_id`、`tenant_id`、`project_id`、`audio_session_id`、`device_id`、`external_recording_id`、`storage_object_id`、`source_url_ref`、`started_at`、`duration_ms`、`sample_rate`、`checksum`、`quality_state`、`status` | `uk_recording_external(tenant_id,project_id,external_recording_id)`；`idx_recordings_session(audio_session_id,started_at)`；`idx_recordings_device(device_id,started_at)` | 原始音频存对象存储；可作为 Qdrant evidence payload 的 `recording_id`。 |
| `audio_segments` | 录音切片/有声段/窗口 | `segment_id`、`tenant_id`、`project_id`、`recording_id`、`audio_session_id`、`segment_type`、`start_ms`、`end_ms`、`storage_object_id` nullable、`confidence`、`status` | `idx_segments_recording_time(recording_id,start_ms,end_ms)`；`idx_segments_session(audio_session_id,segment_type,start_ms)` | 处理后切片可存对象存储；可入 Qdrant `evidence_segments`。 |
| `conversation_boundaries` | 完整对话边界 | `boundary_id`、`tenant_id`、`project_id`、`audio_session_id`、`start_ms`、`end_ms`、`wall_start_at`、`wall_end_at`、`source`、`review_state`、`version`、`trace_id` | `uk_boundary_version(audio_session_id,version)`；`idx_boundaries_session(audio_session_id,review_state)` | 保存边界不直接改 ASR 和标签，触发下游重建任务。 |
| `vad_segments` | VAD 结果 | `vad_segment_id`、`tenant_id`、`project_id`、`segment_id`、`audio_session_id`、`start_ms`、`end_ms`、`energy`、`snr`、`confidence`、`model_version` | `idx_vad_session_time(audio_session_id,start_ms,end_ms)` | 原始 VAD JSON 可由 `asset_materializations` 引用对象存储。 |
| `speaker_turns` | 说话人片段 | `speaker_turn_id`、`tenant_id`、`project_id`、`audio_session_id`、`segment_id`、`speaker_id`、`person_id` nullable、`role`、`channel`、`source_device_id`、`start_ms`、`end_ms`、`confidence`、`confirm_state`、`model_version` | `idx_speaker_turns_session(audio_session_id,start_ms,end_ms)`；`idx_speaker_turns_speaker(tenant_id,project_id,speaker_id,created_at)` | 低置信说话人进入人审；可入 Qdrant `evidence_segments` payload。 |
| `asr_transcripts` | ASR 转写资产元数据 | `transcript_id`、`tenant_id`、`project_id`、`audio_session_id`、`recording_id`、`storage_object_id`、`language`、`model_version`、`word_timestamps_storage_id`、`quality_json`、`status`、`trace_id` | `uk_transcript_model(recording_id,model_version)`；`idx_transcripts_session(audio_session_id,status)` | ASR 完整 JSON 和词级时间戳存对象存储。 |
| `asr_segments` | ASR 分段查询表 | `asr_segment_id`、`tenant_id`、`project_id`、`transcript_id`、`audio_session_id`、`segment_id` nullable、`speaker_turn_id` nullable、`start_ms`、`end_ms`、`speaker_label`、`text`、`confidence`、`intent`、`entity_json` | `idx_asr_segments_session_time(audio_session_id,start_ms,end_ms)`；`idx_asr_segments_text_prefix(tenant_id,project_id,start_ms)` | 可入 Qdrant `evidence_segments`，payload 带文本、时间窗、speaker、label_version。 |
| `evidence_tracks` | 调听多轨时间轴 | `track_id`、`tenant_id`、`project_id`、`audio_session_id`、`track_type`、`label`、`source_type`、`status` | `uk_track_session_type(audio_session_id,track_type,source_type)` | `track_type: vad/speaker/asr/entity/intent/qa/doc/cross/agent`。 |
| `track_regions` | 轨道区域/标签 span | `region_id`、scope、track/session、展示 `label`、`label_id` nullable、`label_version_id` nullable、`event_or_segment_id` nullable、`occurred_at`、field/value、start/end、confidence/review、evidence、write target、note | track/time 与 scope label/version/event 索引 | draft 可保留自由文本；提交权威标签时稳定 label/version/event/evidence/occurred_at 全部必填，过期版本只允许显式 rebase。 |
| `persons` | 人物主体 | `person_id`、`tenant_id`、`project_id`、`external_person_id`、`person_type`、`name_hash`、`display_name`、`status` | `uk_person_external(tenant_id,project_id,external_person_id)`；`idx_persons_type(tenant_id,project_id,person_type,status)` | 客户敏感字段脱敏或 hash。 |
| `voiceprints` | 声纹身份/簇 | `voiceprint_id`、`tenant_id`、`project_id`、`speaker_id`、`person_id` nullable、`store_id`、`sample_count`、`quality_score`、`confirm_state`、`status`、`embedding_model_version` | `uk_voiceprint_speaker(tenant_id,project_id,speaker_id)`；`idx_voiceprints_person(person_id,status)` | embedding 可选入 Qdrant `voiceprint_embeddings`，最终身份以 MySQL 确认为准。 |
| `voiceprint_samples` | 声纹样本 | `voiceprint_sample_id`、`tenant_id`、`project_id`、`voiceprint_id`、`audio_session_id`、`segment_id`、`storage_object_id`、`snr`、`duration_ms`、`purity`、`status` | `idx_voiceprint_samples_voiceprint(voiceprint_id,status,created_at)` | 样本音频存对象存储；可入声纹 collection。 |
| `business_events` | 业务事件 | `event_id`、`tenant_id`、`project_id`、`external_event_id`、`event_type`、`event_status`、`store_id`、`person_id` nullable、`vehicle_id` nullable、`occurred_at`、`source_system`、`raw_payload_storage_id` | `uk_event_external(tenant_id,project_id,source_system,external_event_id)`；`idx_events_type_time(tenant_id,project_id,event_type,occurred_at)` | 原始事件 payload 存对象存储。 |
| `business_documents` | 业务单据 | `document_id`、`tenant_id`、`project_id`、`document_type`、`document_no`、`external_system`、`external_id`、`customer_id`、`person_id`、`vehicle_id`、`store_id`、`owner_staff_id`、`amount`、`occurred_at`、`effective_at`、`file_storage_id`、`structured_payload` JSON、`status` | `uk_document_external(tenant_id,project_id,external_system,external_id)`；`idx_documents_no(tenant_id,project_id,document_type,document_no)`；`idx_documents_time(store_id,occurred_at)` | 原始文件或预览文件存对象存储；可作为 Qdrant evidence payload 的 doc ref。 |
| `business_document_fields` | 单据结构化字段 | `field_id`、`tenant_id`、`project_id`、`document_id`、`field_key`、`field_value`、`value_type`、`confidence`、`source` | `uk_document_field(document_id,field_key)`；`idx_doc_fields_key_value(tenant_id,project_id,field_key,field_value(128))` | 支撑字段差异和 ASR 高亮。 |
| `event_links` | 音频、事件、单据、标签统一关联 | `event_link_id`、`tenant_id`、`project_id`、`event_id` nullable、`document_id` nullable、`document_field_id` nullable、`audio_session_id`、`segment_id` nullable、`asr_segment_id` nullable、`label_candidate_id` nullable、`relation_type`、`relation_state`、`relation_source`、`confidence`、`evidence_pack_id` nullable、`trace_id` | `idx_event_links_session(audio_session_id,relation_state,confidence)`；`idx_event_links_doc(document_id,relation_state)`；`idx_event_links_event(event_id,relation_state)` | 高置信可自动确认，冲突和串音进入人审；可入 Qdrant `evidence_segments`。 |
| `evidence_packs` | 证据包 | `evidence_pack_id`、`tenant_id`、`project_id`、`audio_session_id`、`title`、`window_start_ms`、`window_end_ms`、`summary`、`storage_object_id`、`label_version_id`、`asset_key`、`status`、`trace_id` | `idx_evidence_packs_session(audio_session_id,created_at)`；`idx_evidence_packs_asset(tenant_id,project_id,asset_key,status)` | 证据包文件存对象存储；可入 Qdrant `evidence_segments`，是跨模块回跳主对象。 |
| `review_decisions` | 调听页复核动作记录 | `review_decision_id`、`tenant_id`、`project_id`、`review_task_id` nullable、`audio_session_id`、`target_type`、`target_id`、`decision`、`reason`、`before_json`、`after_json`、`trace_id` | `idx_review_decisions_target(tenant_id,project_id,target_type,target_id,created_at)` | 人工确认、驳回、转人审、采信 ASR/单据都记录。 |

## 7. 标签治理与人审

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `label_taxonomies` | 标签体系 | `taxonomy_id`、`tenant_id`、`project_id`、`name`、`description`、`status` | `uk_taxonomy_name(tenant_id,project_id,name)` | 项目绑定，可从租户模板复制。 |
| `label_nodes` | 稳定 canonical 标签节点 | `node_id`、scope、稳定 `label_id`、`canonical_name`、`status`、Trace、payload | `uq_label_nodes_scope_label(tenant_id,project_id,label_id)`；scope/status 与 Trace 索引 | 0026 强表；别名/版本快照在 `label_version_items`，不能以自由文本改写稳定 ID。 |
| `label_versions` | 标签制品版本 | `label_version_id`、scope、`taxonomy_id`、semantic version、base/replacement、artifact status/published/deprecated timestamps、deprecation reason、content hash、resource version、Trace | scope taxonomy/version 唯一；scope/status 与 replacement 复合 FK | 不保存环境 effective/expired/rollback 状态；legacy 灰度/回滚值只读迁移到 activation ledger。 |
| `label_version_items` | 版本内标签定义快照 | `label_version_item_id`、scope、`label_version_id`、`label_id`、canonical name、aliases、value/risk type、互斥组、parents、aggregation rule、status、Trace | `uq_label_version_items_scope_label(tenant_id,project_id,label_version_id,label_id)`；scope/version 索引 | 聚合只在锁定版本内 canonicalize；父级只允许叶子向上 roll-up。 |
| `label_extraction_runs` | 强 Manifest 抽取投影 | `extraction_run_id`、scope、label/Prompt/model/Schema、subject scope/refs、input hash、count、status、Trace、payload | scope ID、scope/status 与 Trace 索引 | payload 锁定 aggregation policy、source bindings/correlation groups、evidence refs 与 manifest hash；完成后回写自动 AggregationRun。 |
| `label_observations` | append-only 原始标签观察 | scope、run/subject/evidence+SHA、label/Prompt/Schema/model/calibration、source family/type、raw/calibrated confidence、input/output SHA、status、Trace | bucket、evidence/source 与 Trace 索引；SQLite/MySQL UPDATE/DELETE 拒绝触发器 | 来源和证据由运行 Manifest 校验；客户端不能写 human-confirmed 或伪造服务端校准。 |
| `label_calibration_versions` | 服务端锁定校准器 | scope、label version/id、source family、version、method/status、GoldSetVersion、sample count、parameters/metrics、training/content SHA、Trace | scope/version、scope/content hash 唯一；Gold 复合 FK；method/status/sample CHECK；append-only trigger | 0029 强表；published 只允许稳定 Gold，L2 策略锁定并重验。 |
| `label_aggregation_policy_versions` | 不可变聚合策略版本 | scope、label version、policy version、mode/status、source weights、calibration map、thresholds、definitions、canonical SHA、Trace | scope/version 与 scope/hash 唯一；scope/status 索引 | L2 active 必须引用 published 服务端校准器，支持 `label_id::source_family` 精确映射。 |
| `label_aggregation_runs` | 确定性聚合运行 | scope、label/policy/mode/status、observation/aggregate counts、input/result SHA、Trace、payload | scope ID 唯一；scope/status 与 Trace 索引 | 抽取完成后按锁定 Observation 集自动创建；同根 Trace 可完全重放。 |
| `label_aggregates` | 聚合候选与解释 | scope、run/label/policy/calibrations、subject/value、score/margin/risk/decision/status、reasons/explanation、bucket/deterministic hashes、review task、Trace | run/bucket 唯一；subject、decision、Trace 索引 | 低风险 L2 可自动接受；其他项一 Aggregate 一审核任务。 |
| `label_aggregate_members` | Aggregate 全量贡献成员 | aggregate/observation、included、source family、evidence SHA、calibrated confidence、contribution/exclusion/explanation、Trace | aggregate/observation 唯一；aggregate/included 索引 | 记录同相关组去重、区间重叠和排除原因，不能只保存最终分。 |
| `label_mapping_versions` / `label_mapping_items` / `label_mapping_item_targets` | 不可变 source→target edge、每源唯一 disposition 与强 target 行 | scope、source/target LabelVersion、relation/compatibility、metric family/grain/reducer、requires recompute、零到多个 target、resource versions、approval、content SHA、Trace | scope/version/hash 唯一；source item disposition 唯一；target 复合 FK；published 前封口、全子表 append-only trigger | retire 为零 target；普通映射一个 target；split 多 target 且强制 recompute，不能把跨 scope ID 藏进 JSON。 |
| `label_mapping_bundles` / `label_mapping_bundle_sources` / `label_mapping_bundle_members` / `label_mapping_bundle_paths` | normalized 多跳编译闭包 | scope、完整强 source set、target、edge versions/order/SHA、compiled paths、compiler version、canonical SHA、approval、Trace | scope/hash 唯一；source/member/path 唯一；source/edge/最终 target 复合 FK；published 前封口与 append-only trigger | 查询只接受 published bundle ID；source set 以强子表为权威，JSON 只是 canonical manifest 投影，不动态追“最新” edge。 |
| `label_facts` | 追加式权威标签事实 | scope、`logical_key_sha`、revision、namespace/FactSet、label/version、subject/event/assertion slot、value/authority、`occurred_at`、服务端 `recorded_at`、aggregate/human-decision/recompute-item source union、supersedes、content SHA、Trace | scope fact ID、logical key/revision 唯一；current 仅由 `label_fact_heads` 表达；同 scope/source 复合 FK；exactly-one authoritative source CHECK；新行只允许 `recorded/NULL`，UPDATE/DELETE 与新 human+aggregate INSERT trigger | supersedes 只指同 key 前一 revision；0039 Contract 升级也不归一化旧状态，既有行逐值保留。0039 以前 human 行可原样保留 `aggregate_id` 作为 reviewed lineage，但权威源仍是 human decision；新 human 写入强制 aggregate NULL 并把 reviewed ID 放 payload，人工权威不得被 L2 覆盖。 |
| `label_fact_heads` | 单逻辑事实 current 指针 | scope、namespace、logical key、current fact/revision、generation、Trace | scope namespace/key 唯一；current fact 复合 FK；generation CHECK | CAS 前进/回滚；as-of 仍按 Fact `recorded_at <= fact_as_of` 解析。 |
| `label_fact_sets` / `label_fact_set_heads` / `label_fact_set_head_events` | 整批事实 Manifest 与环境可见 Head | scope、namespace、target version、partition/source/result hashes、row counts、status、manifest SHA、current/prior generation、approval、Trace | scope FactSet/hash 唯一；scope/environment/head 唯一；append-only event trigger | full recompute 单事务切整套 FactSet/Asset Manifest，禁止逐 Fact 晋级。 |
| `label_recompute_runs` / `label_recompute_run_items` | split/语义变化 full recompute | scope、冻结 target/bundle、source FactSet Head generation/manifest、candidate FactSet/namespace、partition/asset scope、fact cutoff、coverage/budget、attempt/status、服务端计算 result hashes、Trace | scope run/candidate 唯一；scope run/partition 唯一；Item execution 与 Fact source 同 scope 复合 FK | completion 只接受持久化执行终态回执；实际 Observation/Aggregate/Fact lineage 计算 manifest；全部分区成功前不可 validate/approve/promote。 |
| `feedback_examples` | 人审回流样本 | scope、decision/task/target、feedback type、reason、field diff、before/after、gold status、Trace | decision+target 唯一；scope/type 与 Trace 索引 | 普通接受先为 Gold candidate；双盲一致/仲裁后才晋级 Gold。 |
| `label_taxonomy_suggestions` | 未知标签建议 | scope、label version、normalized/raw labels、observation IDs、action/canonical target、status、review task、Trace、payload | 同版本 normalized label/open status 唯一；scope/status 索引 | 接受后创建候选 LabelVersion/版本项，不直接改生产体系。 |
| `prompt_assets` | Prompt 逻辑资产 | `prompt_asset_id`、scope、name、capability、label version、status、current version、Trace、payload | scope ID、capability+name 唯一 | current pointer 只在发布 ACK 成功后切换。 |
| `prompt_versions` | Prompt 强版本 | scope、asset/version/parent、label/Schema/model、status、template、output schema、generation params、structured diff、badcase refs、content SHA、Trace | asset/version 与 scope/content hash 唯一；scope/status 索引 | modified 审核创建 child revision 和新双盲任务，原候选保持 revision-required。 |
| `release_deployments` | 不可变发布 Bundle 状态 | scope、environment/status/stage、全量锁定版本、rollback target、bundle SHA、rollout、blockers/metrics/approver、Trace、payload | scope ID；scope/status 与 Trace 索引 | 门禁通过先 pending；仅 ReleaseCommand 可信 ACK 才进入 shadow/gray/completed/rolled-back。 |
| `release_commands` | 两阶段发布执行命令 | scope、environment、deployment/target、action/status/active slot、run、expected deployment/head generation/id/hash、command SHA、requester/receipt、Trace | scope ID/run 唯一；deployment+active_slot 唯一；复合 FK 与 action/status/slot CHECK | 0028 强表；同部署仅一个 active 命令，ACK 绑定与 CAS 失败转 blocked。 |
| `release_bundle_heads` | 环境唯一有效 Bundle 指针 | scope、environment、active deployment/bundle、Prompt/Label/model/policy/dataset、generation、bootstrap/command、Trace | scope/environment 与 active deployment 唯一；deployment FK；generation/status CHECK | promote/rollback ACK 在事务内 CAS 切换；迁移期可由自然人一次性 bootstrap LKG。 |
| `release_bundle_head_events` | 环境 activation interval ledger | scope、environment、generation、old/new deployment/bundle/label version、`[effective_from,effective_to)`、command/receipt、event SHA、Trace | scope/environment/generation 唯一；复合 FK；只允许上一 active event 的 `effective_to` 从 NULL 一次性写为下一 event `effective_from`，其余 UPDATE/DELETE 与非连续 INSERT trigger 拒绝 | `release_bundle_heads` 仅当前投影；promote/rollback 在同一事务闭合上一代并追加新一代，event SHA 固化激活起点/指针但不因区间闭合失效。 |
| `label_candidates` | 标签候选 | `candidate_id`、`tenant_id`、`project_id`、`label_version_id`、`label_id`、`audio_session_id`、`evidence_pack_id`、`asr_segment_id` nullable、`source`、`value_text`、`confidence`、`conflict_reason`、`human_state`、`asset_impact_json`、`agent_run_id`、`trace_id`、`status` | `idx_label_candidates_version(label_version_id,status,human_state)`；`idx_label_candidates_evidence(evidence_pack_id,confidence)`；`idx_label_candidates_trace(trace_id)` | 候选可入 Qdrant `label_examples`；不能直接覆盖线上标签。 |
| `label_conflicts` | 标签冲突 | `conflict_id`、`tenant_id`、`project_id`、`candidate_id`、`conflict_type`、`source_type`、`severity`、`detail`、`state`、`trace_id` | `idx_label_conflicts_candidate(candidate_id,state,severity)` | 进入人审或门禁。 |
| `label_optimization_runs` | 标签优化运行 | `optimization_run_id`、`tenant_id`、`project_id`、`task_name`、`input_json`、`current_label_version_id`、`candidate_label_version_id`、`prompt_version_id`、`automation_level`、`decision`、`status`、`dagster_draft_json`、`trace_id` | `idx_label_opt_runs_project(tenant_id,project_id,status,created_at)`；`idx_label_opt_runs_trace(trace_id)` | 统一承载输入、Agent 提升、人工修改、评测、门禁。 |
| `label_optimization_schedules` | 自动优化调度配置与 scope mutex | `schedule_id`、tenant/project、锁定 label/prompt/model/policy/dataset、15m/daily/weekly due 时钟、`active_run_id`、`scan_claim_token/claimed_at`、预算、资源版本、Trace | `uq_label_opt_schedules_scope(tenant_id,project_id,label_version_id)`；`ix_label_opt_schedules_due` | Worker 用条件 UPDATE 原子获取 claim，时钟、运行、round 与 claim 释放同事务提交；同范围只有一个配置/活跃 session。 |
| `label_optimization_metric_snapshots` | 自动触发的 append-only 权威指标 | `snapshot_id`、schedule 与锁定 Bundle、baseline/window、窗口、ppm metrics、规范 reason counts、拒绝记录、`snapshot_sha256`、Trace | `uq_label_opt_metric_snapshots_hash(tenant_id,project_id,schedule_id,snapshot_sha256)`；scope/window 索引；UPDATE/DELETE 数据库触发器禁止 | 非法 JSON 作为 `invalid_json_output` 拒绝记录计入合法率；自由文本失败原因只保存 hash，不能污染失败簇。 |
| `label_optimization_rounds` | 最多三轮候选生成与锁定评测状态 | root/generation run、round 1–3、全量锁定版本、候选/eval IDs、收益/关键回退/成本、连续失败、stop reason、started/completed、Trace | `uq_label_opt_rounds_run_number`、`uq_label_opt_rounds_generation_run`；scope/status 索引与 1–3/0–5/非负成本 CHECK | reconcile 从 session 最早 started_at 起算 2h，hard-stop 所有未终态 generation/eval 并释放单活；状态仅到 awaiting-review/blocked，不持有发布授权。 |
| `change_set_drafts` | 变更草稿 | `change_set_id`、`tenant_id`、`project_id`、`source`、`target_type`、`target_id`、`status`、`summary`、`trace_id` | `idx_change_sets_target(tenant_id,project_id,target_type,target_id,status)` | `source: agent/manual/system_gate/eval/badcase/external_callback`。 |
| `change_set_items` | 变更项 | `change_item_id`、`change_set_id`、`object_type`、`object_id`、`before_json`、`after_json`、`impact_json`、`risk_level` | `idx_change_items_set(change_set_id,risk_level)` | 高风险项必须关联人审或门禁。 |
| `release_gates` | 发布门禁 | `release_gate_id`、`tenant_id`、`project_id`、`target_type`、`target_id`、`target_version`、`status`、`decision`、`blocking_reason`、`trace_id` | `uk_release_gate_target(tenant_id,project_id,target_type,target_id,target_version)`；`idx_release_gates_status(tenant_id,project_id,status)` | 标签、Prompt、任务、高风险配置发布都使用。 |
| `release_gate_checks` | 门禁检查项 | `gate_check_id`、`release_gate_id`、`check_key`、`source_metric`、`verdict`、`detail`、`blocking`、`required_action` | `uk_gate_check(release_gate_id,check_key)` | 消费评测、人审、资产影响、回滚路径等。 |
| `human_review_tasks` | 人审任务 | `review_task_id`、`tenant_id`、`project_id`、`queue`、`target_type`、`target_id`、`title`、`priority`、`risk_level`、`assignee_user_id`、`status`、`due_at`、`evidence_pack_id`、`trace_id` | `idx_review_queue(tenant_id,project_id,queue,status,priority,created_at)`；`idx_review_target(tenant_id,project_id,target_type,target_id)` | 高风险候选、串音、单据冲突、回填、发布门禁都用此表。 |
| `human_review_decisions` | 人审处理结果 | `decision_id`、scope、`review_task_id`、终态 task slot、decision、before/after、decided by、根 `trace_id`、payload 内 `source_trace_id/action_trace_id` | task 唯一终态约束；task/Trace 索引 | `decision: accepted/modified/rejected/escalated`；闭环写回、反馈、审计与 Outbox 继承根 Trace，单次动作 Trace 不覆盖它。 |

## 8. Agentic Runtime 与 Trace

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `agent_runs` | Agent 运行主表 | `agent_run_id`、`tenant_id`、`project_id`、`agent_type`、`input_scope_json`、`status`、`model_provider`、`model_version`、`cost_amount`、`latency_ms`、`started_at`、`finished_at`、`error_message`、`trace_id` | `idx_agent_runs_project(tenant_id,project_id,agent_type,status,created_at)`；`idx_agent_runs_trace(trace_id)` | Agent 输出不能直接覆盖线上状态。 |
| `tool_calls` | 工具调用记录 | `tool_call_id`、`tenant_id`、`project_id`、`agent_run_id`、`tool_name`、`tool_version`、`input_storage_id`、`output_storage_id`、`side_effect`、`status`、`duration_ms`、`error_message`、`trace_id` | `idx_tool_calls_run(agent_run_id,status,created_at)` | 输入输出大 JSON 存对象存储；副作用必须可审计。 |
| `agent_decisions` | Agent 决策 | `decision_id`、`tenant_id`、`project_id`、`agent_run_id`、`decision_type`、`risk_level`、`target_type`、`target_id`、`reason`、`evidence_refs` JSON、`state`、`trace_id` | `idx_agent_decisions_target(tenant_id,project_id,target_type,target_id,state)` | 高风险决策必须关联 `human_review_tasks` 或 `release_gates`。 |
| `trace_refs` | 跨模块追踪引用 | `trace_ref_id`、`tenant_id`、`project_id`、`trace_id`、`module`、`object_type`、`object_id`、`span_ref`、`summary` | `idx_trace_refs_trace(trace_id)`；`idx_trace_refs_object(tenant_id,project_id,object_type,object_id)` | 支撑证据回跳和 Trace 展示。 |

## 9. 知识库与检索元数据

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `knowledge_sources` | 知识源 | `knowledge_source_id`、`tenant_id`、`project_id`、`connector_id` nullable、`source_type`、`name`、`scope`、`owner`、`asset_key`、`status`、`quality_score`、`last_synced_at` | `uk_knowledge_source(tenant_id,project_id,name)`；`idx_knowledge_sources_status(tenant_id,project_id,status)` | 元数据在 MySQL；原文可由对象存储或外部连接器引用。 |
| `knowledge_chunks` | 知识切片元数据 | `chunk_id`、`tenant_id`、`project_id`、`knowledge_source_id`、`chunk_key`、`chunk_text_storage_id`、`window_ref`、`token_count`、`overlap_policy`、`labels_json`、`quality_state`、`evidence_pack_id` nullable、`qdrant_point_id`、`status`、`trace_id` | `uk_knowledge_chunk(knowledge_source_id,chunk_key)`；`idx_chunks_quality(tenant_id,project_id,quality_state,status)` | 向量写 Qdrant `knowledge_chunks`；文本可存 MySQL 摘要和对象存储全文。 |
| `knowledge_indexes` | 索引版本 | `knowledge_index_id`、`tenant_id`、`project_id`、`name`、`version`、`embedding_profile`、`qdrant_collection`、`hybrid_policy` JSON、`status`、`published_at` | `uk_knowledge_index_version(tenant_id,project_id,name,version)`；`idx_knowledge_indexes_status(tenant_id,project_id,status)` | 发布索引版本，不覆盖知识源。 |
| `knowledge_index_build_runs` | 索引构建运行 | `build_run_id`、`tenant_id`、`project_id`、`knowledge_index_id`、`status`、`chunks_total`、`chunks_indexed`、`started_at`、`finished_at`、`error_message`、`trace_id` | `idx_index_build_runs(index_id,status,created_at)` | 统一运行状态。 |
| `knowledge_quality_gates` | 知识质量门禁 | `knowledge_gate_id`、`tenant_id`、`project_id`、`knowledge_index_id`、`gate_type`、`value_text`、`score`、`verdict`、`detail`、`blocking` | `uk_knowledge_gate(knowledge_index_id,gate_type)` | 新鲜度、切片质量、标签覆盖、实体冲突、证据可回放、召回质量、脱敏。 |
| `knowledge_effect_metrics` | 知识效果指标 | `effect_id`、`tenant_id`、`project_id`、`knowledge_index_id`、`metric_key`、`metric_value`、`drop_detail`、`evidence_refs` JSON、`time_bucket` | `idx_knowledge_effect(index_id,metric_key,time_bucket)` | 支撑证据包输入、知识命中、标签补齐、人审接受、回归沉淀。 |
| `knowledge_recall_logs` | 召回日志 | `recall_log_id`、`tenant_id`、`project_id`、`knowledge_index_id`、`query_hash`、`query_storage_id`、`top_k`、`result_refs` JSON、`consumer_module`、`trace_id` | `idx_recall_logs_index(knowledge_index_id,consumer_module,created_at)` | 用于召回效果分析和调试，非权威状态。 |

## 10. ASR 热词治理

热词是 Audio Intelligence/ASR 的领域词包，不是业务“热门标签”。逻辑词包与不可变版本分离；生产任务通过已发布 TaskVersion 间接绑定生产激活的 `hotword_pack_version_id`，候选版本只允许影子运行。

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储 |
| --- | --- | --- | --- | --- |
| `hotword_packs` | 项目级逻辑词包 | `pack_id`、`tenant_id`、`project_id`、`name`、`language`、`domain`、`current_version_id` nullable、`production_version_id` nullable、`status`、`resource_version`、`root_trace_id`、`current_trace_id` | `uq_hotword_packs_scope_name(tenant_id,project_id,name,language,domain)`；`ix_hotword_packs_scope_status(tenant_id,project_id,status)`；`ix_hotword_packs_scope_production_version(tenant_id,project_id,production_version_id)` | `current_version_id` 是完成词包评测/发布回执的候选基线；`production_version_id` 只有其冻结 TaskVersion 通过独立发布门禁后才切换。 |
| `hotword_pack_versions` | 不可变词包版本与发布门禁 | `version_id`、`tenant_id`、`project_id`、`pack_id`、`version`、`baseline_version_id` nullable、`content_sha256` nullable、`manifest_storage_object_id` nullable、`compiled_provider` nullable、`provider_artifact_ref` nullable、`eval_run_id` nullable、`eval_locked`、`model_approved_by` nullable、`project_admin_confirmed_by` nullable、`status`、`resource_version`、`published_at` nullable、`root_trace_id`、`current_trace_id`、`payload` | `uq_hotword_pack_versions_scope_version(tenant_id,project_id,pack_id,version)`；`ix_hotword_pack_versions_scope_status(tenant_id,project_id,status)` | 构建完成后内容哈希、manifest、编译 provider 和产物引用冻结；`published` 后全部内容不可修改，回滚不覆盖版本。 |
| `hotword_version_items` | 版本内规范词、显式别名与 provider 权重 | `item_id`、`tenant_id`、`project_id`、`version_id`、`canonical_term`、`normalized_term`、`aliases`、`category`、`weight`、`source_type`、`source_badcase_id` nullable、`resource_version`、`root_trace_id`、`current_trace_id` | `uq_hotword_version_items_scope_term(tenant_id,project_id,version_id,normalized_term)`；`ix_hotword_version_items_scope_version(tenant_id,project_id,version_id)`；`CHECK(weight BETWEEN 0 AND 100)` | `source_type=manual/badcase/knowledge_candidate`；知识候选不得进入评测/发布。规范化不自动生成繁简体或同音别名。 |
| `hotword_metric_snapshots` | 可筛选的热词统计预计算快照 | `snapshot_id`、`tenant_id`、`project_id`、`bucket_start`、`bucket_end`、`store_id` nullable、`provider` nullable、`model_version` nullable、`hotword_pack_version_id` nullable、`standard_term` nullable、`expected_count`、`correct_count`、`weighted_error_count`、`recognized_hotword_count`、`false_insert_count`、`impacted_session_count`、`evidence_confidence`、`root_trace_id`、`payload` | `uq_hotword_metric_snapshots_scope_id(tenant_id,project_id,snapshot_id)`；`ix_hotword_metric_snapshots_scope_dimensions(tenant_id,project_id,store_id,provider,model_version,hotword_pack_version_id)` | MySQL 是统计真相源，Redis 仅缓存看板；人工修正数、证据等级、优先级和疑似标记放结构化 `payload`，完整词时间戳和诊断写对象存储。 |
| `asr_annotation_corrections` | 调听中显式提交的不可变 ASR 文本修正观察 | `correction_id`、`annotation_id`、`tenant_id`、`project_id`、`audio_session_id`、`observed_at`、`standard_term`、`recognized_text`、`corrected_text`、`error_type`、`evidence_storage_object_id`、`hotword_pack_version_id`、`source_badcase_id`、`correction_fingerprint`、`semantic_sha256`、`evidence_level=discovery`、三类 Trace | 标注 ID、词级证据和修正指纹均在项目范围内唯一；按业务发生时间、统计维度和规范词建索引；复合外键绑定词包版本与 Badcase | 数据库触发器禁止 UPDATE/DELETE；幂等或语义重放复用原观察。该表只贡献 `discovery_summary`，不改变可信 KPI 或发布门禁。普通标签草稿不写入此表。 |

统计字段按快照原子计数后计算：`recall_rate = correct_count / expected_count`，`error_rate = weighted_error_count / expected_count`，`false_boost_rate = false_insert_count / recognized_hotword_count`。优先级按 `log(1+expected_count) × error_rate × evidence_confidence × business_weight` 归一化到 `0–100`。仲裁金标/人工确认可信度为 `1.0`，强关联业务主数据为 `0.8`；知识实体、模型建议和低置信结果仅能发现候选，不得单独确认易错。

ASR 标注修正固定为 `discovery=0.4`：同一词级证据只计一次，达到两次独立修正只能标记 `threshold_met`，不会自动晋级 `human-confirmed`。只有 Badcase 显式人审确认并被后续受信分析快照引用，才进入可信分母。

客户姓名、手机号、车牌和 VIN 分类在写入 `hotword_version_items` 前必须拒绝。Qdrant 只索引脱敏后的相似 Badcase，不保存词包发布状态、指标真值或审批结果。

受控回滚不新增可变副本表：`hotword_rollback` RunRecord/Outbox 冻结源版本、目标版本和逻辑包三者的 `resource_version + root_trace_id`，首次投递由 ReleaseGate 阻断。不同自然人的项目管理员批准后，worker 在单事务内把当前源版本状态更新为 `rolled_back` 并切换 `hotword_packs.current_version_id`；历史目标版本仍为 `published`。该动作不伪装成生产切换：`production_version_id`、TaskVersion、ASR 资产和 `asset_materializations` 均不随回滚自动修改，需另走任务版本发布/回滚门禁。

## 11. 评测、badcase 与回流

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `eval_dataset_versions` | 固定评测集不可变快照 | `eval_dataset_id`、`tenant_id`、`project_id`、`name`、`capability`、`dataset_version`、`manifest_storage_object_id`、`manifest_sha256`、`manifest_provider`、`manifest_bucket`、`manifest_object_key`、`manifest_content_type`、`manifest_size_bytes`、`manifest_etag`、`sample_count`、`status`、`resource_version`、`root_trace_id`、`current_trace_id`、`locked_at`、`payload` | `uq_eval_dataset_versions_scope_id(tenant_id,project_id,eval_dataset_id)`；`uq_eval_dataset_versions_scope_name_version(tenant_id,project_id,name,dataset_version)`；`ix_eval_dataset_versions_scope_status(tenant_id,project_id,capability,status)` | 锁定、发起评测和完成评测前复核登记对象及远端 HEAD；真实 Provider 以 `Content-Length + strong ETag` 检测覆盖或漂移，历史无法补齐的快照 fail closed。 |
| `eval_datasets` | 评测集 | `eval_dataset_id`、`tenant_id`、`project_id`、`name`、`capability`、`version`、`scope_json`、`size`、`status`、`owner_user_id` | `uk_eval_dataset_version(tenant_id,project_id,name,version)`；`idx_eval_datasets_capability(tenant_id,project_id,capability,status)` | 可引用标签版本和模型版本。 |
| `eval_cases` | 评测样本 | `eval_case_id`、`tenant_id`、`project_id`、`eval_dataset_id`、`evidence_pack_id`、`asset_key`、`input_storage_id`、`expected_json`、`source`、`status` | `uk_eval_case_dataset(eval_dataset_id,evidence_pack_id)`；`idx_eval_cases_asset(tenant_id,project_id,asset_key,status)` | 可入 Qdrant `label_examples` 或 `badcase_cases`。 |
| `eval_runs` | 评测运行 | `eval_run_id`、`tenant_id`、`project_id`、`eval_dataset_id`、`baseline_version`、`candidate_version`、`label_version_id`、`model_version`、`prompt_version_id`、`status`、`started_at`、`finished_at`、`trace_id` | `idx_eval_runs_dataset(eval_dataset_id,status,created_at)`；`idx_eval_runs_trace(trace_id)` | 统一运行状态。 |
| `label_eval_results` | 标签发布门禁的不可变强评测结果 | `eval_result_id`、scope、`eval_run_id`、`status=passed/blocked`、`binding_sha256`、`dataset_snapshot_sha256`、`sample_manifest_sha256`、`result_sha256`、overall metrics、paired bootstrap、gate results、payload、Trace | `uq_label_eval_results_scope_run(tenant_id,project_id,eval_run_id)`；`ix_label_eval_results_scope_status`；状态 CHECK；append-only trigger | payload 保留完整强类型回执；发布时重算 result/sample/gate/status，缺失或漂移 fail closed。 |
| `label_eval_suite_results` | 六个锁定评测套件的不可变贡献明细 | `suite_result_id`、scope、`eval_result_id`、`suite`、`sample_count`、`sample_manifest_sha256`、metrics、`suite_sha256`、Trace | `uq_label_eval_suite_results_scope_suite`；结果索引；suite 枚举与 sample_count CHECK；append-only trigger | 每个 EvalResult 必须恰好六行且各 suite 一次；总体 paired sample count 等于六行样本数之和。 |
| `metric_results` | 通用不可变指标快照 | `metric_result_id`、scope、metric key/status、payload、`label_version_applicability`、scope/source/content SHA、source run、Trace | scope result ID 与 source run/key/hash 唯一；scope/status 索引；UPDATE/DELETE trigger | 评测和洞察均可追加结果；各自强类型扩展表冻结不同 grain，不能只信 payload 的 `immutable=true`。 |
| `metric_result_label_scopes` | 标签派生指标一对一强 scope | scope、metric result、taxonomy mode、source version manifest、target version、mapping bundle、FactSet generation、`fact_as_of`、metric definition versions、timezone/period/denominator、comparability/reasons | scope result 唯一；LabelVersion/Bundle/FactSet 复合 FK；required applicability CHECK；UPDATE/DELETE trigger | native/normalized/recomputed 统一强约束；非标签指标必须显式 applicability=none 且没有该行。 |
| `badcases` | badcase 主表及 ASR 热词投影 | `badcase_id`、`tenant_id`、`project_id`、`capability`、`hotword_pack_version_id` nullable、`standard_term` nullable、`recognized_text` nullable、`error_type` nullable、`evidence_ref` nullable、`evidence_level` nullable、`expected_count`、`correct_count`、`weighted_error_count`、`manual_correction_count`、`priority_score`、`candidate_state`、`root_cause`、`fix_suggestion`、`downstream_impact`、`status`、`resource_version`、`root_trace_id`、`current_trace_id`、`trace_id` nullable、`payload` | `uq_badcases_scope(tenant_id,project_id,badcase_id)`；`ix_badcases_scope_capability_status(tenant_id,project_id,capability,status)` | `capability=asr-hotword` 时错误类型受限；`hotword_pack_version_id` 固定产生坏例时的版本，不随修复覆盖；`evidence_ref` 指向脱敏词级证据对象，完整诊断放对象存储。 |
| `feedback_tasks` | 评测/坏例回流任务 | `feedback_task_id`、`tenant_id`、`project_id`、`source_type`、`source_id`、`target_type`、`target_id` nullable、`action_type`、`status`、`assignee_user_id`、`trace_id` | `idx_feedback_tasks_source(tenant_id,project_id,source_type,source_id)`；`idx_feedback_tasks_status(tenant_id,project_id,status,created_at)` | 生成规则候选、Prompt 建议、重评、人审、资产生成、回填。 |

## 12. 数据资产、血缘、回填与导出

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `data_assets` | 数据资产目录 | `data_asset_id`、`tenant_id`、`project_id`、`asset_key`、`asset_name`、`asset_type`、`data_domain`、`description`、`owner_user_id`、`status`、`quality_score`、`last_generated_at` | `uk_data_asset_key(tenant_id,project_id,asset_key)`；`idx_data_assets_domain(tenant_id,project_id,data_domain,status)` | 第一阶段资产目录主表。 |
| `asset_versions` | 资产版本 | `asset_version_id`、`tenant_id`、`project_id`、`data_asset_id`、`version`、`schema_version`、`model_version`、`label_version_id`、`time_range_json`、`location_scope_json`、`status` | `uk_asset_version(data_asset_id,version)`；`idx_asset_versions_status(data_asset_id,status,created_at)` | 记录模型、标签、Schema 和业务范围。 |
| `asset_partitions` | 资产分区 | `partition_id`、`tenant_id`、`project_id`、`asset_version_id`、`partition_key`、`store_id` nullable、`time_bucket`、`status`、`record_count`、`error_count` | `uk_asset_partition(asset_version_id,partition_key)`；`idx_asset_partitions_scope(tenant_id,project_id,store_id,time_bucket,status)` | 支撑回填、失败分区和大列表游标。 |
| `asset_materializations` | 资产生成记录 | `materialization_id`、`tenant_id`、`project_id`、`asset_version_id`、`partition_id` nullable、`task_run_id` nullable、`dagster_run_id`、`storage_object_id` nullable、`status`、`metadata_json`、`trace_id`；ASR 热词回填的 `metadata_json` 固化 `source_materialization_id`、`hotword_pack_version_id`、`eval_run_id`、`task_version_id`、`root_trace_id`、`overwrite_history=false` | `idx_materializations_asset(asset_version_id,status,created_at)`；`idx_materializations_run(task_run_id)` | 业务 UI 展示为生成记录；回填新建物化，历史记录只读。 |
| `asset_lineage_edges` | 资产血缘边 | `lineage_edge_id`、`tenant_id`、`project_id`、`upstream_asset_id`、`downstream_asset_id`、`relation_type`、`transform_ref`、`status` | `uk_lineage_edge(tenant_id,project_id,upstream_asset_id,downstream_asset_id,relation_type)` | 图优先展示 SourceAsset、Asset、AssetCheck、Backfill、Human Loop、External Callback、Report/Insight。 |
| `asset_quality_checks` | 资产质量检查 | `quality_check_id`、`tenant_id`、`project_id`、`asset_version_id`、`partition_id` nullable、`check_type`、`score`、`status`、`detail_json`、`trace_id` | `idx_quality_checks_asset(asset_version_id,status,check_type)` | 完整性、及时性、一致性、重复率、缺失率、Schema 稳定性、评测覆盖率。 |
| `backfill_requests` | 受控回填请求 | `backfill_id`、`tenant_id`、`project_id`、`data_asset_id`、`asset_version_id` nullable、`scope_json`、`overwrite_policy`、`recompute_downstream`、`impact_json`、`approval_status`、`status`、`task_run_id` nullable、`trace_id` | `idx_backfills_asset(data_asset_id,status,created_at)`；`idx_backfills_approval(tenant_id,project_id,approval_status,status)` | 覆盖或重算下游必须人审或审批。 |
| `export_jobs` | 导出任务 | `export_job_id`、`tenant_id`、`project_id`、`export_type`、`scope_json`、`format`、`storage_object_id` nullable、`status`、`expires_at`、`trace_id` | `idx_export_jobs_project(tenant_id,project_id,status,created_at)` | 导出文件存对象存储；必须审计。 |
| `storage_objects` | 对象存储引用表 | `storage_object_id`、`tenant_id`、`project_id` nullable、`bucket`、`object_key`、`object_type`、`content_type`、`size_bytes`、`checksum`、`encryption_mode`、`retention_until`、`status` | `uk_storage_object(tenant_id,project_id,bucket,object_key)`；`idx_storage_objects_type(tenant_id,project_id,object_type,status)` | 统一管理原始音频、WAV、JSON、证据包、报告和导出文件。 |

## 13. 洞察、报告与设置

| 表名 | 用途 | 核心字段 | 唯一约束/索引 | 状态、审计、存储、Qdrant |
| --- | --- | --- | --- | --- |
| `insight_metrics` | 预计算读优化投影 | `metric_row_id`、scope、metric key/time bucket/dimensions、store、model、`label_version_applicability`、`metric_result_id`、value、source asset | scope metric/time/dimension 索引；metric result 复合 FK | 标签派生行必须指向带 `metric_result_label_scopes` 的不可变结果；非标签行显式 applicability=none。Redis 只缓存完整 scope SHA。 |
| `insight_facts` | 洞察事实 | `fact_id`、`tenant_id`、`project_id`、`fact_type`、`title`、`summary`、`metric_key`、`evidence_pack_id` nullable、`asset_key`、`confidence`、`qdrant_recall_refs` JSON、`status`、`trace_id` | `idx_insight_facts_type(tenant_id,project_id,fact_type,status,created_at)`；`idx_insight_facts_evidence(evidence_pack_id)` | 可引用 Qdrant 召回结果，但权威状态在 MySQL。 |
| `insight_reports` / `insight_report_metric_refs` | 洞察报告与冻结结果引用 | report scope/title/summary/status/storage/`metric_scope_sha256`/Trace；ref 记录 report/result/order | report scope ID 与 report/result 唯一；MetricResult 复合 FK | 报告只引用同 scope 已物化结果，不按 metric key 重查最新 Fact；文件存对象存储。 |
| `model_providers` | 模型 provider 注册 | `provider_id`、`tenant_id`、`project_id` nullable、`provider_type`、`name`、`secret_ref_id`、`status`、`cost_policy_json` | `uk_provider_name(tenant_id,project_id,name)` | 裸 endpoint 和密钥不进业务页面。 |
| `model_services` | 模型服务版本 | `model_service_id`、`tenant_id`、`project_id` nullable、`provider_id`、`service_type`、`service_id`、`version`、`endpoint_ref`、`input_schema` JSON、`output_schema` JSON、`timeout_ms`、`concurrency_limit`、`fallback_service_id`、`status` | `uk_model_service(tenant_id,project_id,service_id,version)`；`idx_model_services_type(service_type,status)` | Audio Intelligence Service、Tagger、LLM Judge、Embedding 等。 |
| `tool_registry` | Agent 工具注册 | `tool_id`、`tenant_id`、`project_id` nullable、`tool_name`、`version`、`input_schema` JSON、`output_schema` JSON、`timeout_ms`、`retry_policy` JSON、`permission`、`side_effect`、`status` | `uk_tool_version(tenant_id,project_id,tool_name,version)` | 工具调用实例写 `tool_calls`。 |
| `threshold_configs` | 阈值配置 | `threshold_id`、`tenant_id`、`project_id` nullable、`scope_type`、`scope_id` nullable、`threshold_key`、`threshold_value`、`effective_at`、`expires_at`、`status`、`version` | `uk_threshold_effective(tenant_id,project_id,scope_type,scope_id,threshold_key,version)` | 高风险阈值修改写 ReleaseGate 和审计。 |
| `policy_guards` | 策略守卫配置 | `policy_id`、`tenant_id`、`project_id` nullable、`policy_key`、`risk_level`、`rules_json`、`status`、`version` | `uk_policy_version(tenant_id,project_id,policy_key,version)` | Agent、回填、发布、外部回写都要引用。 |
| `secret_refs` | 密钥引用 | `secret_ref_id`、`tenant_id`、`project_id` nullable、`secret_name`、`secret_provider`、`secret_path`、`status`、`rotated_at` | `uk_secret_ref(tenant_id,project_id,secret_name)` | 只存引用，不存明文。 |

## 14. 对象存储引用规范

对象存储 key 建议：

```text
tenants/{tenant_id}/projects/{project_id}/audio/raw/{yyyy-mm-dd}/{recording_id}.wav
tenants/{tenant_id}/projects/{project_id}/audio/processed/{task_run_id}/{segment_id}.wav
tenants/{tenant_id}/projects/{project_id}/model/asr/{model_version}/{recording_id}.json
tenants/{tenant_id}/projects/{project_id}/model/diar/{model_version}/{recording_id}.json
tenants/{tenant_id}/projects/{project_id}/evidence/{evidence_pack_id}.json
tenants/{tenant_id}/projects/{project_id}/reports/{report_id}.pdf
tenants/{tenant_id}/projects/{project_id}/exports/{export_job_id}.{ext}
```

引用规则：

- 业务表只保存 `storage_object_id`，不要把预签名 URL 持久化为业务字段。
- 外部 URL 只保存 `source_url_ref` 或 `secret_ref_id`，需要访问时由 BFF 或任务运行时换取。
- 对象存储写入后必须登记 `storage_objects`，再由业务表引用。
- 删除策略按租户保留周期执行，审计和证据包引用的对象进入保留保护。

## 15. Qdrant Collection 与 Payload 对应关系

Qdrant point id 建议使用 `tenant_id:project_id:source_type:source_id:version` 的 hash 或 ULID，并在 MySQL 对应表保存 `qdrant_point_id`。

| Collection | MySQL 来源 | 向量内容 | 必填 payload | 额外 payload | 回跳与鉴权 |
| --- | --- | --- | --- | --- | --- |
| `knowledge_chunks` | `knowledge_chunks`、`knowledge_sources`、`knowledge_indexes` | SOP、FAQ、产品资料、标签样本、证据切片文本 | `tenant_id`、`project_id`、`asset_key`、`source_type=knowledge_chunk`、`source_id=chunk_id`、`version=index_version`、`trace_id` | `knowledge_source_id`、`chunk_key`、`labels`、`quality_state`、`evidence_pack_id`、`window_ref` | 召回后用 `chunk_id` 查 MySQL，校验成员权限和索引状态。 |
| `evidence_segments` | `audio_segments`、`asr_segments`、`track_regions`、`event_links`、`evidence_packs` | ASR 文本、证据窗口摘要、事件关联说明 | `tenant_id`、`project_id`、`asset_key`、`source_type`、`source_id`、`version`、`trace_id`、`evidence_id` | `audio_session_id`、`recording_id`、`segment_id`、`asr_segment_id`、`start_ms`、`end_ms`、`speaker_id`、`store_id`、`document_id`、`event_id`、`label_version_id` | 召回结果只返回 BFF projection；前端不能看到 collection。 |
| `label_examples` | `label_version_items`、`label_candidates`、`eval_cases`、`human_review_decisions` | 标签正例、反例、冲突样本、人工修正说明 | `tenant_id`、`project_id`、`asset_key`、`source_type`、`source_id`、`version=label_version`、`trace_id`、`label_version` | `label_id`、`candidate_id`、`evidence_pack_id`、`polarity`、`confidence`、`human_state`、`prompt_version_id` | 用于候选标签、Prompt 优化和冲突解释，写回仍走 MySQL 候选表。 |
| `badcase_cases` | `badcases`、`eval_cases`、`metric_results` | badcase 原因、修复建议、证据摘要 | `tenant_id`、`project_id`、`asset_key`、`source_type=badcase`、`source_id=badcase_id`、`version`、`trace_id`、`evidence_id` | `capability`、`severity`、`root_cause`、`target_asset_key`、`eval_dataset_id`、`status` | 召回用于归因和补样，最终状态以 `badcases.status` 为准。 |
| `voiceprint_embeddings` | `voiceprints`、`voiceprint_samples` | 声纹 embedding | `tenant_id`、`project_id`、`asset_key`、`source_type=voiceprint_sample`、`source_id=voiceprint_sample_id`、`version=embedding_model_version`、`trace_id` | `voiceprint_id`、`speaker_id`、`person_id`、`store_id`、`quality_score`、`confirm_state`、`privacy_scope` | 只做候选检索，最终身份以 MySQL 人工确认状态为准。 |

Qdrant 写入流程：

1. MySQL 写入或更新业务对象，生成 `trace_id`。
2. 对象存储保存需要向量化的文本、证据包或样本文件。
3. 异步索引任务读取 MySQL 权威状态和对象存储内容，写入 Qdrant。
4. 回写 `qdrant_point_id`、索引版本和构建状态到 MySQL。
5. 召回时先查 Qdrant，再用 payload 的业务 ID 回 MySQL 二次鉴权和投影。

## 16. 第一阶段实现优先级

P0 必须先实现：

- `tenants`、`users`、`tenant_members`、`projects`、`project_members`、`roles`、`audit_logs`
- `connectors`、`data_source_bindings`、`sync_runs`、`sync_cursors`
- `task_types`、`task_canvas_versions`、`task_versions`、`task_runs`、`task_run_steps`
- `audio_sessions`、`audio_recordings`、`audio_segments`、`conversation_boundaries`、`asr_transcripts`、`asr_segments`、`speaker_turns`
- `business_documents`、`business_document_fields`、`event_links`、`evidence_packs`
- `label_taxonomies`、`label_nodes`、`label_versions`、`label_candidates`
- `human_review_tasks`、`human_review_decisions`
- `data_assets`、`asset_versions`、`asset_partitions`、`asset_materializations`、`storage_objects`
- `model_providers`、`model_services`、`tool_registry`、`threshold_configs`、`secret_refs`

P1 紧随实现：

- `agent_runs`、`tool_calls`、`agent_decisions`、`trace_refs`
- `knowledge_sources`、`knowledge_chunks`、`knowledge_indexes`、`knowledge_index_build_runs`、`knowledge_quality_gates`
- `label_optimization_runs`、`change_set_drafts`、`change_set_items`、`release_gates`、`release_gate_checks`
- `eval_datasets`、`eval_cases`、`eval_runs`、`metric_results`、`badcases`、`feedback_tasks`
- `asset_lineage_edges`、`asset_quality_checks`、`backfill_requests`、`export_jobs`
- `insight_metrics`、`insight_facts`、`insight_reports`

P2 视需求扩展：

- 更细粒度 `asr_words` 词级表。第一阶段可仅存对象存储 JSON。
- 跨项目主体映射表。第一阶段可先通过 `stores`、`persons`、`devices` 的项目级表实现。
- 通知订阅、报告模板、复杂审批流。
- 复杂 Trace Replay 事件表。第一阶段用 `trace_refs`、`audit_logs`、`tool_calls`、运行表串联。
