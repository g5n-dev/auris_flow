# 当前原型 mock 到后端 API 映射

本文档把 `prototype/auris-flow-ui/src/App.tsx` 里的 `/api/v1` mock 文案归一到一期后端资源。正式开发以 `doc/backend-spec/api-contract.md` 的资源名为准；本文件负责说明原型文案、正式接口、资源归属和前端状态反馈之间的关系。

标签、Prompt、人审、评测和发布事实全部从 MySQL 权威投影读取；Redis/Qdrant 不提供 UI 终态，一期不引入 ClickHouse，前端不展示底层执行引擎画布或状态名。

## 1. 命名归一规则

原型中存在早期命名、页面口语命名和正式资源命名混用。一期后端按以下规则收敛：

| 原型 mock 文案 | 正式资源或接口 | 说明 |
| --- | --- | --- |
| `/api/v1/review-tasks` | `/api/v1/human-review-tasks` | 统一称为人审任务，覆盖金额冲突、串音、低置信、标签人审和发布门禁。 |
| `/api/v1/ops/work-items` | `/api/v1/work-items` | 首页动作创建可追踪工作项，不直接改业务资产。 |
| `/api/v1/task-drafts` | `/api/v1/task-versions`，`status=draft` | 任务草稿是任务版本的一种状态。 |
| `/api/v1/knowledge/sources` | `/api/v1/knowledge-sources` | URL 使用复数资源和 kebab-case。 |
| `/api/v1/knowledge/index-runs` | `/api/v1/knowledge-indexes/{id}/build-runs` | 索引构建是 KnowledgeIndex 下的异步运行资源。 |
| `/api/v1/label-drafts` | `/api/v1/label-versions` 或 `/api/v1/label-versions/{id}` | 标签草稿归入候选标签版本。 |
| `/api/v1/label-conflicts/:id` | `/api/v1/human-review-tasks/{id}/decisions` | 冲突仲裁落为人审决策和发布门禁记录。 |
| UI “智能抽取/ExtractionRun” | `/api/v1/label-extraction-runs` + `/api/v1/label-observations` | 旧 `label-optimization-runs` 保留兼容；真实模型回执必须先物化不可变 Observation。 |
| UI “候选标签/聚合结果” | `/api/v1/label-aggregation-runs` + `/api/v1/label-aggregates` | 候选来自锁定策略的确定性聚合，不再由本地 mock 指标拼装。 |
| UI “Prompt 候选版本” | `/api/v1/prompt-assets` + `/api/v1/prompt-versions` | `/prompt-version-candidates` 保留只读兼容投影，权威正文和版本链以强版本接口为准。 |
| UI “发布/灰度/回滚” | `/api/v1/release-deployments` + `/api/v1/release-deployments/{deployment_id}/transitions` | Bundle 锁定 label/Prompt/model/policy/eval/rollback target；只有完成 promote 才显示发布成功。 |
| `/api/v1/insight-reports` | `/api/v1/insights/reports` | 洞察报告归入 insights 模块。 |
| `/api/v1/insight-actions` | `/api/v1/insights/actions` | 洞察动作归入可追踪 action/work item。 |
| `/api/v1/evaluation-sets` | `/api/v1/eval-datasets` | 评测集正式命名为 eval dataset。 |
| `/api/v1/badcases` | `/api/v1/badcases` | Badcase 是正式项目级资源；`/eval-runs/{id}/feedback-tasks` 仅负责从已确认 Badcase 生成后续回流任务。 |
| `hotwords_ref` | `hotword_pack_version_id` | 旧字段仅在读取历史 TaskVersion 时兼容；新建/修改任务和音频智能运行必须引用不可变版本 ID。 |
| `/api/v1/data-backfills` | `/api/v1/data-assets/{asset_key}/backfills` | 回填必须绑定资产和影响范围。 |
| `/api/v1/data-assets/:assetKey/checks/retry` | `/api/v1/data-assets/{asset_key}/checks/retry` | 保留语义，统一 path 参数格式。 |
| `/api/v1/settings/drafts` | `/api/v1/settings/drafts` | 高风险设置先保存草稿。 |
| `/api/v1/settings/publish-requests` | `/api/v1/settings/publish-requests` | 发布审批和 Policy Guard。 |
| `/api/v1/tools/*` | `/api/v1/settings` 中 `domain=tools.*` | 工具开关本质是设置域，避免新增独立工具配置体系。 |
| `/api/v1/reception-links` | `/api/v1/event-links` | 接待单、报价单、试驾单等都作为音频到业务事件的关联。 |
| `/api/v1/reception-links/rebind` | `PATCH /api/v1/event-links/{id}` | 改绑是事件关联更新。 |
| `/api/v1/reception-orders/drafts` | `/api/v1/human-review-tasks` 或 `/api/v1/event-links` | 缺接待单先进入人审或关联草稿，不直接生成业务订单。 |
| `/api/v1/platform-sessions` | `/api/v1/platform-connections/{connection_id}/session` | 平台会话由连接器创建，凭证只保存引用。 |
| `/api/v1/platform-tenants`、`/api/v1/platform-employees` | `/api/v1/data-sources/{source_id}/records` | 租户、员工、门店是外部主数据记录投影。 |
| `/api/v1/audio-recording-urls` | `/api/v1/audio-ingest/recordings` | 录音 URL 接入统一走音频 ingest。 |
| `/api/v1/output-sinks/platform-callbacks` | `/api/v1/output-sinks/platform-callbacks` | 保留为输出回写资源，必须幂等和可审计。 |

## 2. 模块映射

### 2.1 首页

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 首页待办、异常、运行概览、最近资产 | `InsightMetric`、`HumanReviewTask`、`DataAsset` | `GET /api/v1/insights/ops-summary`；`GET /api/v1/human-review-tasks`；`GET /api/v1/data-assets/recent` | 无直接资产修改 | 卡片加载用 `pending`；异常点击只聚焦或下钻。 |
| `POST /api/v1/ops/work-items` | `WorkItem` | `GET /api/v1/work-items` | `POST /api/v1/work-items` | 创建后返回 `work_item_id`、`status`、`trace_id`，首页追加操作记录。 |
| “首页不直接改业务资产，只创建跨模块处理草稿” | `WorkItem` | 详情可指向项目、调听、资产、洞察 | `PATCH /api/v1/work-items/{id}` | `success` 后显示处理草稿；`blocked` 显示权限或缺少证据引用。 |

最低映射字段：

- `source_signal`
- `action_type`
- `evidence_ref`
- `owner`
- `status`
- `affected_objects`
- `trace_id`

### 2.2 租户

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 租户列表、隔离边界、配额、成员、ASR 授权 | `Tenant`、`MemberInvite`、`QuotaDraft` | `GET /api/v1/tenants`；`GET /api/v1/tenants/{id}` | `POST /api/v1/tenants`；`PATCH /api/v1/tenants/{id}` | 保存后展示审计回执；ASR 授权用 `pending`、`success`、`failed`。 |
| `POST /api/v1/tenants · PATCH /api/v1/tenants/:tenantId` | `Tenant` | 同上 | 同上，正式 path 为 `{id}` | `409` 版本冲突提示刷新；`403` 显示租户权限不足。 |

最低映射字段：

- `tenant_id`
- `name`
- `status`
- `quota_policy`
- `asr_binding_ref`
- `audit_scope`
- `member_count`
- `trace_id`

### 2.3 项目

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 项目工作空间、数据源绑定、成员范围、质量目标 | `ProjectWorkspace` | `GET /api/v1/projects`；`GET /api/v1/projects/{id}` | `POST /api/v1/projects`；`PATCH /api/v1/projects/{id}` | 质量目标、成员变更保存后返回 `audit_log_id` 和 `trace_id`。 |
| `POST /api/v1/projects · PATCH /api/v1/projects/:projectId` | `Project` | 同上 | 同上 | `success` 后刷新当前项目上下文；`blocked` 展示跨租户或角色限制。 |

最低映射字段：

- `project_id`
- `tenant_id`
- `scene`
- `owner_user_id`
- `data_source_refs`
- `label_version`
- `quality_target`
- `status`

### 2.4 任务配置

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 任务类型、画布版本、任务版本、调度、AB、运行记录 | `TaskType`、`TaskVersion`、`TaskRun` | `GET /api/v1/task-types`；`GET /api/v1/task-versions`；`GET /api/v1/task-runs` | `POST /api/v1/task-versions`；`PATCH /api/v1/task-versions/{id}`；`POST /api/v1/task-runs`；`POST /api/v1/task-versions/{id}/publish` | 保存草稿返回 `draft`；运行返回 `task_run_id`；发布门禁失败返回 `blocked`。 |
| `POST /api/v1/task-drafts · POST /api/v1/task-runs` | `TaskVersion(status=draft)`、`TaskRun` | `GET /api/v1/task-versions?status=draft` | `POST /api/v1/task-versions`；`POST /api/v1/task-runs` | 节点添加、连线、调度调整都落入版本草稿。 |
| 手动运行主入口 `POST /api/v1/task-runs` | `TaskRun` | `GET /api/v1/task-runs/{id}` | `POST /api/v1/task-runs` | 返回 `run_key`、`partition_key`、`status`、`trace_id`，前端轮询。 |
| 输出回写 `POST /api/v1/output-sinks/platform-callbacks` | `OutputSinkCallback` | `GET /api/v1/output-sinks/platform-callbacks` | `POST /api/v1/output-sinks/platform-callbacks` | 幂等回写；失败进入 retry 或死信队列。 |
| ASR 节点“汽车销售热词包 v1.8” | `HotwordPackVersion` | `GET /api/v1/hotword-packs`；`GET /api/v1/hotword-pack-versions/{version_id}` | `PATCH /api/v1/task-versions/{id}` 写 `hotword_pack_version_id` | 生产只能选 `published`；候选版本显示阻断原因且仅能触发 shadow 运行。 |

最低映射字段：

- `task_type_id`
- `task_version_id`
- `flow_template`
- `canvas_variant`
- `node_bindings`
- `run_config`
- `schedule`
- `output_bindings`
- `run_key`
- `hotword_pack_version_id`
- `status`
- `trace_id`

### 2.5 数据管理

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 音频、人物声纹、事件、业务单据、关联视图 | `AudioSession`、`PersonVoiceprint`、`EventLink`、`AggregationView` | `GET /api/v1/audio-sessions`；`GET /api/v1/audio-sessions/aggregations`；`GET /api/v1/voiceprints`；`GET /api/v1/event-links` | `POST /api/v1/event-links`；`PATCH /api/v1/event-links/{id}` | 音频真值页只渲染 aggregation 的 `children[]`；人物/事件/关系读模型未接入时显式 unavailable，不回落本地 fixture。 |
| `POST /api/v1/data-source-bindings · PATCH /api/v1/data-aggregation-views/:id` | `ConnectorBinding`、`AggregationView` | `GET /api/v1/connectors`；`GET /api/v1/audio-sessions/aggregations` | `POST /api/v1/connectors`；`PATCH /api/v1/data-aggregation-views/{id}` | 仅当叶子返回同 scope 已登记 `target_asset_key` 且 `connector_import.enabled=true` 时开放 POST；否则显示 `blocked_reason` 并 fail closed。 |
| 平台主数据 `GET /api/v1/data-sources/{source_id}/records` | `DataSourceRecord` | 同接口带 `resource=tenant|employee|store` | 无 | 外部主数据读取失败用 `retry`，权限失败用 `blocked`。 |
| 音频 URL `POST /api/v1/audio-ingest/recordings` | `AudioRecordingRef` | `GET /api/v1/audio-sessions` | `POST /api/v1/audio-ingest/recordings` | 返回 `recording_id`、`asset_key`，后续任务消费资产引用。 |
| 认证事件 `GET /api/v1/authenticated-events` | `AuthenticatedEvent` | `GET /api/v1/authenticated-events` | 无 | 只读当前任务授权范围事件。 |

最低映射字段：

- `audio_session_id`
- `recording_id`
- `store_id`
- `employee_id`
- `started_at`
- `duration_ms`
- `asset_key`
- `event_id`
- `document_ref`
- `match_score`
- `status`
- `target_asset_key`（nullable）
- `connector_import.enabled`
- `connector_import.blocked_reason`（nullable）

聚合组按 `started_at` 小时动态生成，非法或缺失时间进入显式 unknown 组，组状态由子项派生。会话创建/seed 必须显式声明目标资产；读端不得用演示默认值补齐缺失绑定，也不得接受其他租户或项目的资产 key。Data 页的 connector/export 操作必须由 workspace 下传当前已发布 SceneProfile 绑定；operation 自身在绑定读取中、未绑定、读取失败或快照漂移时阻断 POST，绑定有效时请求必须携带 profile ID、version ID 与 snapshot SHA-256 三元锁。

### 2.6 知识库

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 知识源、切片、索引、质量门禁、消费路径、效果漏斗 | `KnowledgeSource`、`KnowledgeIndex`、`KnowledgeEffect` | `GET /api/v1/knowledge-sources`；`GET /api/v1/knowledge-indexes`；`GET /api/v1/knowledge-indexes/{id}/effects` | `POST /api/v1/knowledge-sources/{id}/sync-runs`；`POST /api/v1/knowledge-indexes/{id}/build-runs` | 同步、构建、质量检测都是异步状态。 |
| `POST /api/v1/knowledge/sources · POST /api/v1/knowledge/index-runs` | `KnowledgeSource`、`KnowledgeIndexBuildRun` | 正式改为 `GET /api/v1/knowledge-sources` | `POST /api/v1/knowledge-sources/{id}/sync-runs`；`POST /api/v1/knowledge-indexes/{id}/build-runs` | `pending/running/success/failed/blocked` 映射同步记录和质量门禁。 |
| 知识命中、相似证据、召回解释 | `KnowledgeRecallProjection` | 通过各业务接口返回 | 无直接 Qdrant API | 前端不展示 collection/vector，只展示解释、片段和证据链接。 |

最低映射字段：

- `source_id`
- `source_ref`
- `chunk_policy`
- `embedding_profile`
- `index_id`
- `index_version`
- `quality_gate`
- `consumer_paths`
- `trace_id`

### 2.7 调听

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 复核队列金额冲突、串音候选、低置信 | `HumanReviewTask` | `GET /api/v1/human-review-tasks?queue=amount_conflict&status=pending` 等 | `POST /api/v1/human-review-tasks/{id}/decisions` | 队列切换只换筛选；决策后刷新当前样本和队列计数。 |
| 原型 `GET /api/v1/review-tasks?...` | `HumanReviewTask` | 正式改为 `/api/v1/human-review-tasks?...` | 同上 | 兼容前端状态：`pending`、`success`、`failed`、`retry`、`blocked`。 |
| 当前会话、Minimap、波形、标签轨、ASR、单据证据 | `AudioSession`、`EvidenceTrack`、`EventLink` | `GET /api/v1/audio-sessions/{id}` | `PATCH /api/v1/conversation-boundaries/{id}` | 边界保存返回 `affected_tracks`，前端重建轨道显示。 |
| 创建证据包 | `EvidencePack` | `GET /api/v1/evidence-packs/{id}` | `POST /api/v1/evidence-packs` | 成功后返回 `evidence_pack_id` 和可导出引用。 |
| 接待单关联 `POST /api/v1/reception-links` | `EventLink` | `GET /api/v1/event-links` | `POST /api/v1/event-links` | 返回 `event_link_id`、`match_score`、`diffs`、`status`。 |
| 改绑 `PATCH /api/v1/reception-links/rebind` | `EventLink` | `GET /api/v1/event-links/{id}` | `PATCH /api/v1/event-links/{id}` | 高风险改绑可返回 `blocked`，要求人工确认。 |
| 补单草稿 `POST /api/v1/reception-orders/drafts` | `HumanReviewTask` 或 `EventLink(status=draft)` | `GET /api/v1/human-review-tasks` | `POST /api/v1/human-review-tasks` | 不直接生成业务订单；先作为缺单人审任务或关联草稿。 |
| ASR Diff “识别文本 → 正确文本” | `Badcase(capability=asr-hotword)` | `GET /api/v1/badcases?capability=asr-hotword`，由 `badcase_id` deep link 恢复 | `POST /api/v1/badcases`；`POST /api/v1/badcases/{badcase_id}/decisions` | 提交证据窗口后生成 Badcase，不复用标签标注接口、不改原 ASR 资产；显示 run/trace 和失败原因。 |

最低映射字段：

- `audio_session_id`
- `conversation_boundary_id`
- `start_ms`
- `end_ms`
- `speaker_turns`
- `asr_segments`
- `label_spans`
- `event_links`
- `evidence_pack_id`
- `review_task_id`
- `decision`
- `trace_id`

### 2.8 标签

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 标签体系、候选、版本、冲突、发布门禁、Human Loop | `Label`、`LabelVersion`、`LabelCandidate`、`HumanReviewTask` | `GET /api/v1/labels`；`GET /api/v1/label-versions` | `POST /api/v1/label-versions`；`PATCH /api/v1/label-versions/{id}`；`POST /api/v1/label-optimization-runs`；`POST /api/v1/label-versions/{id}/publish` | 候选只能写入草稿或人审；发布必须过门禁。 |
| `POST /api/v1/label-drafts · PATCH /api/v1/label-conflicts/:id` | `LabelVersion(status=draft)`、`HumanReviewTaskDecision` | `GET /api/v1/label-versions?status=draft` | `POST /api/v1/label-versions`；`POST /api/v1/human-review-tasks/{id}/decisions` | 保存候选版本后进入影子评测；冲突处理更新发布门禁。 |
| “ExtractionRun 正在运行，只写 LabelCandidate” | `LabelOptimizationRun` | `GET /api/v1/label-optimization-runs/{id}` | `POST /api/v1/label-optimization-runs` | 返回 `optimization_run_id`；运行中展示 `pending/running`。 |
| “送 Human Loop、接受、修改、拒绝” | `HumanReviewTask` | `GET /api/v1/human-review-tasks` | `POST /api/v1/human-review-tasks/{id}/decisions` | 决策同步到候选版本、人审日志和发布门禁。 |
| 低风险多选与批量回执 | `HumanReviewDecisionBatch` | 由当前候选队列读取服务端可批资格 | `POST /api/v1/human-review-decision-batches` | 只允许同标签/低风险/同策略 cohort；逐项显示 success、skipped、failed 和原因，不能整批假成功。 |
| 标签体系“未知标签簇/alias/merge/split” | `LabelTaxonomySuggestion`、`LabelAggregationPolicyVersion` | `GET /api/v1/label-taxonomy-suggestions`；`GET /api/v1/label-aggregation-policies` | `POST /api/v1/label-aggregation-policies`；建议决策复用候选级 Human Loop | 未知自由文本不直接上线；变更生成候选 LabelVersion/策略版本并保留影响对象数。 |
| 智能抽取“真实模型运行” | `LabelExtractionRun`、`LabelObservation` | `GET /api/v1/label-extraction-runs/{extraction_run_id}`；`GET /api/v1/label-observations` | `POST /api/v1/label-extraction-runs`；受信 worker `POST /api/v1/label-observations` 或完成回执物化 | `queued/running/materializing` 持续轮询；空结果、失败、重试均显示后端原因。 |
| 聚合解释与候选级审核 | `LabelAggregationRun`、`LabelAggregate` | `GET /api/v1/label-aggregation-runs/{aggregation_run_id}`；`GET /api/v1/label-aggregates` | `POST /api/v1/label-aggregation-runs`；单项/批量人审 | 中间栏展示成员、证据、来源贡献、score/margin、冲突原因、策略/校准版本和确定性 hash。 |
| Prompt 从 badcase/人工修改/失败簇生成 | `PromptAsset`、`PromptVersion`、`LabelOptimizationRun` | `GET /api/v1/prompt-assets`；`GET /api/v1/prompt-versions`；`GET /api/v1/label-optimization-trigger-scans/{id}` | `POST /api/v1/prompt-assets`；`POST /api/v1/prompt-versions`；`POST /api/v1/label-optimization-trigger-scans` | 展示真实模板、父版本、逐字段 diff、来源 badcase、预算、阻断和下一动作；本地 mock 仅 `demo_mode`。 |
| Prompt 双盲审核与仲裁 | `PromptReviewSubmission`、`PromptReviewAdjudication` | 候选详情只显示已收审核数与阶段，不泄漏密封结论 | `POST /api/v1/prompt-version-candidates/{candidate_id}/review-submissions`；不一致时 `POST .../adjudications` | 首份显示“等待第二位审核人”，分歧显示“待独立仲裁”；只有终态后展示决策和下一动作。 |
| 版本发布“锁定 Bundle/在线门禁/回滚” | `ReleaseDeployment` | `GET /api/v1/release-deployments/{deployment_id}` | `POST /api/v1/release-deployments`；`POST /api/v1/release-deployments/{deployment_id}/transitions` | `blocked` 禁用并解释，`shadowing/gray-releasing/monitoring` 轮询；`completed` 且 Prompt published 才显示成功。 |
| 在线保护轨道 | `ReleaseMonitorSample` | 发布详情读取最近窗口、`stable_window_complete`、硬阈值 violations 和自动动作 | system-only `POST /api/v1/release-deployments/{deployment_id}/monitor-samples` | 正常进入 monitoring；稳定窗口未完成时禁用人工晋级并解释；退化立即显示“已自动回滚”或“已安全停流量，待人工处理”。 |

最低映射字段：

- `label_domain`
- `label_group`
- `label`
- `value_or_action`
- `label_version_id`
- `candidate_count`
- `release_gate`
- `prompt_version`
- `model_version`
- `human_state`
- `trace_id`
- `locked_versions`
- `blocked_reasons`
- `next_action`
- `deterministic_hash`

### 2.9 洞察

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 趋势、桑吉、漏斗、雷达、报告、证据下钻 | `InsightMetric`、`InsightFact`、`InsightReport` | `GET /api/v1/insights/metrics`；`GET /api/v1/insights/funnels`；`GET /api/v1/insights/reports` | `POST /api/v1/insights/reports`；`POST /api/v1/insights/actions` | 图表点击只聚焦；跳转通过明确动作按钮。 |
| `POST /api/v1/insight-reports · POST /api/v1/insight-actions` | `InsightReport`、`InsightAction` | 正式归入 `/api/v1/insights/*` | `POST /api/v1/insights/reports`；`POST /api/v1/insights/actions` | 生成报告返回 `report_id`、`asset_ref`、`trace_id`。 |
| 洞察解释和证据链接 | `EvidenceDrilldown` | 随 metrics/funnels/reports 返回 | 无直接召回动作 | Qdrant 召回只在 BFF 内部，前端展示解释和证据链接。 |
| 模型质量热词覆盖、召回、易错、误增强、影响会话、Top 易错词 | `HotwordMetricSnapshot`、`ASRHotwordBadcase` | `GET /api/v1/hotword-statistics`，按日期、门店、provider、模型、词包版本筛选 | `POST /api/v1/hotword-analysis-runs` | 分析按钮显示 pending/success/failed/blocked、run id、root trace；Top 词点击下钻 `evaluation?capability=asr-hotword&badcase_id=A-4107`。 |

最低映射字段：

- `metric_key`
- `value`
- `compare_value`
- `time_range`
- `evidence_count`
- `evidence_links`
- `report_id`
- `asset_ref`
- `trace_id`

### 2.10 评测

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 评测集、运行、指标、badcase、回流 | `EvalDataset`、`EvalRun`、`MetricResult`、`Badcase`、`FeedbackTask` | `GET /api/v1/eval-datasets`；`GET /api/v1/eval-runs`；`GET /api/v1/eval-runs/{id}` | `POST /api/v1/eval-datasets`；`POST /api/v1/eval-runs`；`POST /api/v1/eval-runs/{id}/feedback-tasks` | 运行评测显示 pending、完成态、失败重试和 badcase 回流记录。 |
| `POST /api/v1/evaluation-sets · POST /api/v1/badcases` | `EvalDataset`、`Badcase`、`FeedbackTask` | 正式为 `/api/v1/eval-datasets`、`/api/v1/badcases` 和 `/api/v1/eval-runs/{id}/feedback-tasks` | 先创建/确认 Badcase，再按需生成 feedback task | 创建 Badcase 返回 `badcase_id/resource_version/root_trace_id`；回流另返回 `feedback_task_id`。 |
| Prompt 评测 `EvalRun eval_quote_guard_20260702` | `EvalRun` | `GET /api/v1/eval-runs/{id}` | `POST /api/v1/eval-runs` | 对比 current 和 candidate，写入 TraceRef。 |
| `ASR 热词`能力与待归因 → 待人审 → 待回流 → 已入回归 | `Badcase`、`HotwordPackVersion` | `GET /api/v1/badcases?capability=asr-hotword`；`GET /api/v1/hotword-pack-versions/{version_id}` | `PATCH /api/v1/badcases/{badcase_id}`；`POST /api/v1/badcases/{badcase_id}/decisions`；词项 CRUD | 展示标准词、识别结果、错误类型、证据等级、统计量、下游影响、优先级和乐观锁冲突。 |
| 标签人工修改/拒绝与 Prompt 失败簇 | `Badcase(capability=labeling|prompt-optimization)`、`FeedbackExample` | `GET /api/v1/badcases?capability=labeling` 或 `prompt-optimization` | `POST /api/v1/badcases`；通常由 Human Loop 同事务自动生成 | 展示 failure reason、source/evidence、label/Prompt/Aggregate/Review refs、期望/实际值和字段 diff；不套用 ASR 热词字段。 |
| 固定同一评测集比较 baseline/candidate | `EvalRun`、`HotwordReleaseGate` | `GET /api/v1/eval-runs/{id}` | `POST /api/v1/hotword-pack-versions/{version_id}/eval-runs`；`POST /api/v1/hotword-pack-versions/{version_id}/publish` | 显示热词指标、CER/WER、下游 F1、延迟、成本门禁；发布成功仅生成 TaskVersion 草稿。 |

最低映射字段：

- `dataset_id`
- `sample_refs`
- `model_version`
- `label_version`
- `eval_run_id`
- `metrics`
- `badcases`
- `feedback_task_id`
- `release_gate`
- `trace_id`

### 2.11 资产

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 资产目录、血缘、质量、回填、导出 | `DataAsset`、`AssetLineage`、`AssetQualityCheck`、`BackfillRequest`、`ExportJob` | `GET /api/v1/data-assets`；`GET /api/v1/data-assets/{asset_key}`；`GET /api/v1/data-assets/{asset_key}/lineage` | `POST /api/v1/data-assets/{asset_key}/backfills`；`POST /api/v1/data-assets/{asset_key}/checks/retry`；`POST /api/v1/exports` | 血缘节点默认聚焦；回填展示影响范围、审批状态、运行记录和失败分区；三类项目业务写入由 BFF 校验并持久化当前 active production SceneProfile 三元锁，缺绑定或调用方锁漂移均拒绝。 |
| `GET /api/v1/data-assets/:id/partitions` | `DataAssetPartition` | `GET /api/v1/data-assets/{asset_key}/partitions` | 无 | 大分区使用 cursor，避免 offset 慢查。 |
| `GET /api/v1/data-assets/:id/materializations` | `AssetMaterialization` | `GET /api/v1/data-assets/{asset_key}/materializations` | 无 | 返回 `materialization_id`、`run_id`、`checks`。 |
| ASR 热词修复血缘 | `LineageEdge` | `GET /api/v1/data-assets/{asset_key}/lineage` | `POST /api/v1/data-assets/{asset_key}/backfills` | 依次显示原 ASR、证据、A-4107、词包版本、EvalRun、新转写和回填；任何历史转写/人工确认均不覆盖。 |
| `POST /api/v1/data-backfills` | `BackfillRequest` | `GET /api/v1/data-assets/{asset_key}` | `POST /api/v1/data-assets/{asset_key}/backfills` | 影响人工标注或线上报告时返回 `approval_required`。 |
| `POST /api/v1/data-assets/:assetKey/checks/retry` | `AssetCheckRun` | `GET /api/v1/data-assets/{asset_key}/materializations` | `POST /api/v1/data-assets/{asset_key}/checks/retry` | 返回 `retry_id`、`status`、`trace_id`。 |
| BFF 面板“保留 key、partition、run_id、trace_id” | 所有资产响应 | 所有 data-assets 接口 | 所有资产动作 | 前端只传业务语义，BFF 负责映射资产 Key、分区和运行上下文。 |

最低映射字段：

- `asset_key`
- `partition_key`
- `materialization_id`
- `run_id`
- `quality_score`
- `freshness`
- `checks`
- `upstream`
- `downstream`
- `impact_scope`
- `approval_status`
- `trace_id`

### 2.12 设置

| 原型模块或文案 | 后端资源 | 列表/详情接口 | 动作接口 | 前端状态反馈 |
| --- | --- | --- | --- | --- |
| 模型服务、Provider、工具、阈值、权限、存储、通知 | `Setting`、`ConfigDraft`、`PolicyGuardRun`、`AuditLog` | `GET /api/v1/settings`；`GET /api/v1/settings/{id}` | `PATCH /api/v1/settings/{id}`；`POST /api/v1/settings/drafts`；`POST /api/v1/settings/publish-requests`；`POST /api/v1/settings/provider-tests` | 高风险配置先草稿，Policy Guard 通过后发布；所有保存形成审计记录。 |
| `PATCH /api/v1/settings/model-chain`、`provider-route`、`tagger`、`judge` | `Setting(domain=model.*)` | `GET /api/v1/settings?domain=model` | `PATCH /api/v1/settings/{id}` | 发布前跑影子评测，失败不覆盖线上链路。 |
| `PATCH /api/v1/tools/audio-intelligence` 等 | `Setting(domain=tools.*)` | `GET /api/v1/settings?domain=tools` | `PATCH /api/v1/settings/{id}` 或 `POST /api/v1/settings/drafts` | 工具调用必须记录 `trace_id` 和 `provider_ref`。 |
| 阈值设置 | `Setting(domain=thresholds.*)` | `GET /api/v1/settings?domain=thresholds` | `PATCH /api/v1/settings/{id}` | 变更后先模拟队列增量，再保存草稿。 |
| 权限设置 | `Setting(domain=permissions.*)` | `GET /api/v1/settings?domain=permissions` | `POST /api/v1/settings/drafts`；`POST /api/v1/settings/publish-requests` | 租户隔离不可关闭；降低风险强度需双人审批。 |
| `PATCH /api/v1/settings/drafts · POST /api/v1/settings/publish-requests` | `ConfigDraft`、`PolicyGuardRun` | `GET /api/v1/settings/{id}` | 同原型文案保留 | 返回 `draft_id`、`policy_guard_result`、`rollback_version`、`trace_id`。 |

最低映射字段：

- `setting_id`
- `domain`
- `key`
- `value`
- `risk_level`
- `owner`
- `policy`
- `asset_key`
- `status`
- `draft_id`
- `rollback_version`
- `audit_log_id`
- `trace_id`

## 3. 外部接入和任务画布 mock 映射

| 原型节点 | 原型接口 | 正式接口 | 后端资源 | 前端状态 |
| --- | --- | --- | --- | --- |
| 平台登录适配器 | `POST /api/v1/platform-connections/{connection_id}/session` | 保留 | `PlatformSession` | 成功返回 `session_id`、`access_token_ref`、`tenant_scope`、`expires_at`。 |
| REST Source 接口 | `GET /api/v1/data-sources/{source_id}/records?resource=tenant|employee|store` | 保留 | `DataSourceRecord` | 分页返回主数据；上游失败可重试。 |
| 音频 URL 同步接口 | `POST /api/v1/audio-ingest/recordings` | 保留 | `AudioRecordingRef` | 返回 `recording_id` 和资产引用，不暴露明文 URL。 |
| 认证事件接口 | `GET /api/v1/authenticated-events` | 保留 | `AuthenticatedEvent` | 只读当前授权范围事件。 |
| 平台数据同步抽取 | `POST /api/v1/platform-sync-jobs` | 保留 | `PlatformSyncJob` | 异步返回 `sync_job_id`、`status`、`asset_keys`。 |
| 平台处理结果推送 | `POST /api/v1/output-sinks/platform-callbacks` | 保留 | `OutputSinkCallback` | 必须带幂等键；失败进入 retry 或 dead letter。 |

## 4. 前端状态反馈统一映射

| 原型交互 | 后端返回 | 前端处理 |
| --- | --- | --- |
| 点击运行、同步、构建、评测、回填、导出 | `status=pending` 或 `running`，含轮询链接 | 按钮禁用，显示“运行中/排队中”，轮询详情。 |
| 保存草稿 | `status=draft` 或 `success`，含 `audit_log_id` | 显示保存回执，局部刷新版本号。 |
| 发布、回填、高风险设置 | `blocked` 或 `approval_required` | 展示阻断原因，打开人审、审批或发布门禁入口。 |
| 外部源不可用 | `503 upstream_unavailable`，`retryable=true` | 显示可重试，不清空当前页面数据。 |
| 幂等重复点击 | 返回第一次结果 | 前端不重复追加记录。 |
| 请求体变更但幂等键相同 | `409 idempotency_conflict` | 提示刷新或重新提交。 |
| 权限不足 | `403 forbidden` | 显示租户、项目或角色权限不足，不伪装为空态。 |
| 人审决策 | `status=success`，含 `affected_objects` | 更新候选、人审队列、发布门禁和审计记录。 |

## 5. 后端实现优先级

P0 必须先完成：

- 认证上下文、租户项目过滤、统一响应、统一错误、trace、幂等。
- `tenants`、`projects`、`connectors`。
- `task-types`、`task-versions`、`task-runs`。
- `audio-sessions`、`event-links`、`conversation-boundaries`、`evidence-packs`。
- `human-review-tasks`。
- `labels`、`label-versions`。
- `data-assets`。
- `settings`。

P1 紧随 P0 完成，支撑原型完整闭环：

- `knowledge-sources`、`knowledge-indexes`。
- `label-optimization-runs`。
- `eval-datasets`、`eval-runs`。
- `insights`。
- `exports`。
- `output-sinks/platform-callbacks`。

P0 和 P1 都属于一期后端范围。P0 先保证主流程可跑，P1 保证当前高保真原型的知识库、洞察、评测、导出和回写闭环不再停留在 mock。
