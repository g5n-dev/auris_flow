# Auris Flow 后端领域模型

## 1. 文档定位

本文档用于把当前 Auris Flow React 高保真原型和既有设计文档收敛为后端开发可执行的领域模型。后端第一阶段以 FastAPI BFF、MySQL、Redis、对象存储、Dagster、Qdrant、OpenTelemetry 为技术基线。

硬约束：

- MySQL 是权威业务状态库，保存权限、配置、运行、音频证据、标签、评测、资产、审计和发布状态。
- Qdrant 只做向量召回，不保存审批、发布、权限或最终业务状态。
- 对象存储保存大文件和模型产物，例如原始音频、处理后 WAV、ASR JSON、Diar JSON、证据包、报告、导出文件。
- 前端只访问 FastAPI BFF，不直接访问 MySQL、Qdrant、对象存储或 Dagster。
- Agent 输出不能直接覆盖线上业务结果，只能写候选、草稿、人审任务、评测运行、回填草稿、洞察事实或 Trace。
- 第一阶段不引入 ClickHouse。洞察和运营大盘使用 MySQL 聚合表、预计算结果、Redis 缓存和 Qdrant 召回解释。

## 2. 通用领域语言

| 术语 | 后端含义 | 说明 |
| --- | --- | --- |
| Tenant | 租户，最高隔离单元 | 企业、集团、事业部或独立业务线。 |
| Project | 项目，主要业务工作空间 | 绑定数据源、音频、标签体系、任务版本、评测和资产。 |
| Data Domain | 数据域 | 音频、人物声纹、事件、标签、业务单据、评测、洞察、资产等逻辑域。 |
| Data Asset | 数据资产 | 可管理、可追踪、可复用、可评测的数据对象或产物。 |
| Asset Key | 资产逻辑键 | 例如 `auris/audio/raw_recordings`，用于业务资产、Dagster 映射和 Qdrant payload 回跳。 |
| Audio Session | 音频会话 | 一次接待、沟通或业务过程的音频证据集合，可跨多个 wav 切片。 |
| Conversation Boundary | 完整对话边界 | 用户可编辑的完整对话开始和结束。VAD、ASR、标签和事件轨道跟随边界重建。 |
| Evidence Pack | 证据包 | 可回放、可审计、可下钻的音频、ASR、标签、单据、Agent Trace 组合。 |
| Business Document | 业务单据 | 试驾单、报价单、订单、保险单、交付单、工单等事件关联对象，不是普通附件。 |
| Event Link | 事件关联 | 连接音频片段、ASR 句子、业务事件、人物、车辆、单据字段、标签和证据包。 |
| Label Candidate | 标签候选 | Agent、规则或人工草稿生成的候选结果，不等同线上标签。 |
| Human Review Task | 人审任务 | 高风险候选、低置信、冲突、回填、发布门禁等需要人工处理的任务。 |
| Agent Run | 智能运行 | 一次 Agentic 运行，记录输入、召回、工具调用、输出、成本、状态和 Trace。 |
| Trace Ref | 追踪引用 | 贯通 tenant、project、task、asset、evidence、label、eval 的可回放引用。 |
| Hotword Pack | ASR 领域热词逻辑包 | 与业务热门标签无关；通过不可变版本、影子评测和人工发布治理。 |

统一异步状态：

- `pending`：已创建，等待执行。
- `running`：执行中。
- `success`：完成并写入结果。
- `failed`：失败，可重试或查看错误。
- `blocked`：被权限、门禁、依赖或审批阻断。
- `cancelled`：用户或系统取消。

该状态语义适用于 TaskRun、AgentRun、EvalRun、KnowledgeIndexBuildRun、ExportJob、BackfillRequest、ExternalCallback。

## 3. Bounded Contexts

| Bounded Context | 职责 | 拥有的核心对象 | 不负责 |
| --- | --- | --- | --- |
| Identity & Workspace | 租户、项目、成员、角色、权限、资源配额、审计上下文 | Tenant、Project、User、Member、Role、Policy、Quota、AuditLog | 不保存音频、标签、模型输出。 |
| Connector & Ingestion | 外部数据源接入、授权引用、同步游标、同步运行、字段映射 | Connector、DataSourceBinding、SyncCursor、SyncRun、SourceRecord、MappingSuggestion | 不直接生成最终业务标签。 |
| Task Configuration & Execution | 任务类型、画布版本、任务版本、调度、A/B 实验、运行记录、输出回写 | TaskType、CanvasVersion、TaskVersion、TaskNode、Schedule、Experiment、TaskRun、OutputBinding | 不把 Dagster 作为业务 API 主语言。 |
| Audio Evidence & Annotation | 音频会话、录音、片段、边界、VAD、ASR、Diar、轨道、证据包、事件关联 | AudioSession、AudioRecording、AudioSegment、ConversationBoundary、EvidenceTrack、SpeakerTurn、ASRSegment、BusinessDocument、EventLink、EvidencePack | 不管理标签体系发布。 |
| Label Governance | 标签体系、标签版本、候选、冲突、Prompt、ChangeSet、发布门禁、人审 | LabelTaxonomy、LabelNode、LabelVersion、PromptVersion、LabelCandidate、ChangeSetDraft、ReleaseGate、HumanReviewTask | 不直接执行模型服务和外部回写。 |
| Agentic Runtime | 智能运行、工具调用、决策记录、候选草稿、Trace | AgentRun、ToolCall、AgentDecision、TraceRef、BoundaryCandidate、EventLinkCandidate、BackfillDraft | 不绕过权限，不覆盖线上状态。 |
| Knowledge & Retrieval | 知识源、语义切片、索引版本、质量门禁、召回效果 | KnowledgeSource、KnowledgeChunk、KnowledgeIndex、KnowledgeQualityGate、KnowledgeEffect | 不保存 Qdrant 向量为权威业务状态。 |
| ASR Hotword Governance | 热词统计、易错确认、词包修复、影子复测、人工发布和受控回填 | HotwordPack、HotwordPackVersion、HotwordVersionItem、HotwordMetricSnapshot、ASRHotwordBadcase | 不生成业务热门标签，不原地覆盖 ASR 或人工确认。 |
| Evaluation & Feedback | 评测集、评测运行、指标、badcase、模型对比、回流任务 | EvalDataset、EvalCase、EvalRun、MetricResult、Badcase、FeedbackTask | 不发布标签版本，只提供门禁输入。 |
| Data Asset & Lineage | 资产目录、版本、分区、生成记录、血缘、质量、回填、导出 | DataAsset、AssetVersion、AssetPartition、Materialization、LineageEdge、QualityCheck、BackfillRequest、ExportJob | 不暴露裸 Dagster UI 给业务用户。 |
| Insight & Reporting | 运营指标、洞察事实、报告、证据下钻 | InsightMetric、InsightFact、InsightReport、EvidenceDrilldown | 不成为标签或评测权威来源。 |
| Settings & Provider Registry | 模型服务、工具、阈值、策略、密钥引用、存储、通知 | ModelProvider、ModelService、ToolRegistry、ThresholdConfig、PolicyGuard、SecretRef | 不在业务页面暴露裸 endpoint 或密钥。 |

## 4. 核心聚合与实体

### 4.1 Identity & Workspace

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| Tenant | `tenant_id`、`name`、`status`、`plan`、`quota_policy_id` | 租户是最高隔离边界。跨租户读取、召回、导出和回写均禁止。 |
| Project | `project_id`、`tenant_id`、`name`、`scene`、`status`、`owner_user_id`、`quality_target` | 项目是业务工作空间。所有业务对象必须绑定 `tenant_id + project_id`。 |
| User | `user_id`、`email`、`name`、`status` | 用户可以属于多个租户和项目，权限通过成员关系获得。 |
| TenantMember / ProjectMember | `tenant_id`、`project_id`、`user_id`、`role_id`、`data_scope` | BFF 入口必须基于成员关系注入可访问项目和数据范围。 |
| Store / Location | `store_id`、`tenant_id`、`project_id`、`region`、`city`、`store_name` | 门店可在同租户多项目复用，跨项目复用应通过租户级主体映射或项目绑定表表达。 |
| Device | `device_id`、`store_id`、`device_type`、`badge_no`、`position`、`status` | 工牌、展厅麦克风、试驾车设备、电话录音设备都统一为收声设备。 |

### 4.2 Connector & Ingestion

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| Connector | `connector_id`、`tenant_id`、`provider`、`connector_type`、`auth_mode`、`status` | 保存连接器元数据和授权引用，不保存明文密钥。 |
| DataSourceBinding | `binding_id`、`tenant_id`、`project_id`、`connector_id`、`resource_type`、`asset_key`、`mapping_policy` | 外部数据进入平台实体和数据资产的入口。 |
| SyncCursor | `cursor_id`、`binding_id`、`cursor_value`、`updated_after`、`last_success_at` | 增量同步必须幂等，游标更新必须晚于同步结果持久化。 |
| SyncRun | `sync_run_id`、`binding_id`、`status`、`started_at`、`finished_at`、`trace_id` | 同步运行写入运行状态、错误和影响对象。 |
| SourceRecord | `source_record_id`、`external_id`、`resource_type`、`raw_payload_ref`、`checksum` | 原始外部记录只作为可追溯来源，不直接替代平台标准实体。 |
| MappingSuggestion | `suggestion_id`、`source_field`、`target_field`、`join_key`、`confidence`、`state` | Agent 可生成字段映射建议，人工确认后才能应用到任务版本或绑定配置。 |

### 4.3 Task Configuration & Execution

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| TaskType | `task_type_id`、`tenant_id`、`project_id`、`name`、`scenario`、`status` | 任务类型是业务流程模板，不是单次运行。 |
| CanvasVersion | `canvas_version_id`、`task_type_id`、`version`、`graph_json`、`status` | 一个任务类型可有生产、灰度、影子评测、热修复和 A/B 画布版本。 |
| TaskVersion | `task_version_id`、`task_type_id`、`canvas_version_id`、`version`、`release_state`、`rollback_to` | 发布、回滚和运行必须针对任务版本，不直接运行草稿画布。 |
| TaskNode / TaskEdge | `node_id`、`task_version_id`、`node_type`、`input_contract`、`output_contract`、`dagster_binding` | 节点可展示 Dagster 映射，但业务主语仍是输入、处理、输出和回写。 |
| TaskSchedule | `schedule_id`、`task_version_id`、`mode`、`cron_expr`、`trigger_rule`、`status` | 支持定时运行、手动运行、数据到达触发、一次性回填。 |
| Experiment | `experiment_id`、`task_version_id`、`arm`、`traffic`、`metric_policy`、`status` | A/B 指标必须写入同一任务和资产上下文。 |
| TaskRun | `task_run_id`、`task_version_id`、`status`、`partition_key`、`dagster_run_id`、`trace_id` | 运行状态对前端使用统一枚举，底层 Dagster 字段只做诊断信息。 |
| OutputBinding / CallbackBinding | `binding_id`、`task_version_id`、`sink_type`、`endpoint_ref`、`idempotency_key_policy` | 外部回写必须配置鉴权、重试、幂等键、失败队列和审计。 |

### 4.4 Audio Evidence & Annotation

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| AudioSession | `audio_session_id`、`store_id`、`customer_ref`、`employee_id`、`started_at`、`ended_at`、`status` | 核心业务对象不是单条音频，而是同一时间、地点、人物、音频、事件、标签、单据的组合。 |
| AudioRecording | `recording_id`、`audio_session_id`、`device_id`、`storage_object_id`、`duration_ms`、`checksum` | 原始音频存对象存储，MySQL 只保存元数据、存储引用和质量状态。 |
| AudioSegment | `segment_id`、`recording_id`、`start_ms`、`end_ms`、`segment_type`、`status` | VAD、有声段、流式 chunk、回填片段都可作为片段。 |
| ConversationBoundary | `boundary_id`、`audio_session_id`、`start_ms`、`end_ms`、`source`、`version`、`review_state` | 边界页只保存完整对话开始/结束。下游 ASR、标签、事件轨道根据边界重建。 |
| VADSegment | `vad_segment_id`、`segment_id`、`energy`、`silence_before_ms`、`confidence` | 只代表音频活动，不代表业务事件。 |
| SpeakerTurn | `speaker_turn_id`、`audio_session_id`、`segment_id`、`speaker_id`、`role`、`channel`、`confidence` | ASR 不负责最终说话人身份，身份由 Diar、声纹、工牌、设备、业务上下文和人工确认共同形成。 |
| ASRTranscript / ASRSegment | `transcript_id`、`asr_segment_id`、`text`、`start_ms`、`end_ms`、`confidence`、`model_version` | ASR JSON 可存对象存储，查询高频分段字段落 MySQL。 |
| EvidenceTrack / TrackRegion | `track_id`、`region_id`、`track_type`、`label`、`label_id` nullable、`label_version_id` nullable、`occurred_at`、`start_ms`、`end_ms`、`confidence`、`review_state` | draft 可保留展示文本；一旦提交为权威标签，必须绑定稳定 label/version、事件或片段、证据和业务时间。旧版本 draft 只允许显式 rebase，不得静默改写。 |
| Person / Voiceprint | `person_id`、`speaker_id`、`voiceprint_id`、`role`、`confirm_state` | 员工、销售、客户、未知声纹簇和临时说话人统一管理。 |
| BusinessEvent | `event_id`、`event_type`、`occurred_at`、`source_system`、`external_id` | 接待、试驾、报价、异议、成交、交付、售后等业务事件。 |
| BusinessDocument | `document_id`、`document_type`、`document_no`、`customer_id`、`vehicle_id`、`amount`、`status` | 业务单据是事件关联对象，需要结构化字段和原始文件引用。 |
| EventLink | `event_link_id`、`event_id`、`document_id`、`segment_id`、`asr_segment_id`、`relation_type`、`confidence`、`state` | 支持一致、金额冲突、待响应、串音待排除、可回填、解除关联等状态。 |
| EvidencePack | `evidence_pack_id`、`audio_session_id`、`window_start_ms`、`window_end_ms`、`storage_object_id`、`trace_id` | 洞察、标签、评测、知识库和资产都通过证据包回跳调听页。 |

### 4.5 Label Governance

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| LabelTaxonomy | `taxonomy_id`、`tenant_id`、`project_id`、`name`、`status` | 标签体系按项目绑定，可继承租户模板。 |
| LabelNode | `label_id`、`taxonomy_id`、`parent_label_id`、`label_type`、`level`、`code`、`name` | 固定表达为标签域、标签组、标签、标签值/动作。 |
| LabelVersion | `label_version_id`、`taxonomy_id`、`version`、artifact status/timestamps、base/replacement、content hash | 不携带某环境的 active/draining；音频、标注、评测、洞察和资产都记录不可变版本。legacy 灰度/回滚状态只读迁移到 activation ledger。 |
| LabelVersionItem | `label_version_id`、`label_id`、definition hash、item status、parents/rule snapshot | 已发布版本项只读；版本内状态不表达 rename/replace/merge/split。 |
| LabelMappingVersion / Item / ItemTarget | source/target LabelVersion、唯一 source disposition、零到多个强 target、relation、compatibility、metric families/reducer、content hash、approval | 单 source→target version 不可变 edge；每个源 active item 只有一个 disposition，retire 为零 target，split 的多个 target 必须逐行做 scope FK。 |
| LabelMappingBundle / Source / Member / Path | 完整 source version set、target version、edge versions/SHA、compiled paths、compiler version、canonical hash | `LabelMappingBundle` 是 normalized 的唯一权威闭包；Source 与 Path 复合 FK 保证 source set 和最终 target，发布后不能追加、编辑、删除或重编。 |
| LabelExtractionRun | 锁定 label/Prompt/Schema/model/policy、subject/evidence manifest、`source_bindings`、root Trace | 真实完成回执必须一次性物化 Observation，并由服务端自动创建确定性 AggregationRun；派发成功不等于业务完成。 |
| LabelObservation | subject/evidence、label/Prompt/Schema/model/calibration、source lineage、raw/calibrated confidence、input/output hashes、Trace | append-only；来源族、相关组和证据由强 Manifest 验证，人工结论不能伪装成模型 Observation。 |
| LabelCalibrationVersion | label/version/source family、method、GoldSetVersion、samples、parameters、training/content hashes、status | 服务端锁定、append-only；published 必须满足 Gold 稳定性与方法样本量，L2 只使用可重验版本。 |
| LabelAggregationPolicy / Run / Aggregate / Member | 锁定策略、校准器、阈值、成员贡献、排除原因、input/result/deterministic hashes | 先按证据、区间和服务端 correlation group 去相关；同输入版本可完全重放；一个 Aggregate 对应一个审核任务。 |
| LabelFact / LabelFactHead | `logical_key_sha`、revision、`occurred_at`、`recorded_at`、source union、supersedes、content hash；Head generation | Fact 行只追加，旧 revision 不变；logical key 包含 subject、event/segment、label 和 assertion slot，current/as-of 由 Head 与 `fact_as_of` 解析。 |
| LabelFactSet / FactSetHead | namespace、partition manifest、source/result hash、target label version、generation | full recompute 先写候选 FactSet；审批时用单 Head 原子切换整套事实/资产，禁止逐 Fact 晋级。 |
| PromptAsset / PromptVersion | asset/version/parent、P-CODE template、Schema、模型参数、diff、badcase refs、content hash、status | Prompt 版本参与抽取、评测和发布；modified 审核创建 child revision 和全新双盲任务，不原地批准父候选。 |
| LabelCandidate | `candidate_id`、`label_version_id`、`label_id`、`evidence_pack_id`、`confidence`、`human_state`、`trace_id` | 候选只能进入草稿、人审或评测，不直接覆盖线上标签。 |
| LabelConflict | `conflict_id`、`candidate_id`、`source_type`、`severity`、`reason`、`state` | 模型、规则、人工、单据和版本冲突必须可解释。 |
| LabelOptimizationRun | `optimization_run_id`、`input_json`、`change_set_id`、`automation_level`、`decision`、`trace_id` | 统一承载 Agent 提升、人工修改、效果评价、发布门禁和 DagsterRunDraft。 |
| LabelOptimizationSchedule / MetricSnapshot / Round | 锁定 Bundle、15m/daily/weekly due、DB claim、append-only 24h metrics、1–3 轮候选/Eval/预算/stop reasons | 单范围单活、24h trigger 去重与冷却；reconcile 从 session 最早 Round 起算 2h 墙钟并 hard-stop 卡住的 generation/Eval；自动化终点为 awaiting-review/blocked，不自动发布。 |
| ChangeSetDraft / ChangeSetItem | `change_set_id`、`source`、`object_type`、`before_json`、`after_json`、`impact_json` | 所有 Agent 建议和人工修改必须区分来源，支持回放和审计。 |
| ReleaseGate / GateCheck | `release_gate_id`、`target_type`、`target_version_id`、`status`、`blocking_reason` | 发布标签、Prompt、任务或高风险配置必须经过门禁。 |
| ReleaseDeployment / ReleaseCommand / ReleaseBundleHead | 冻结 Bundle、pending command/run/action；命令 SHA、expected head generation/id/hash；环境唯一 active head | 创建/transition 只请求命令。受信 ACK 精确绑定且 CAS 成功后，才能改变 Shadow/Gray/Production 或回滚指针。 |
| HumanReviewTask / Decision | `review_task_id`、`target_type`、`target_id`、`priority`、`status`、`decision` | 高风险候选、覆盖人工标注、批量回填、外部回写都必须走人审或阻断。 |

闭环 Trace 不变量：任务创建时冻结 `source_trace_id`；人审终态、Feedback、Fact、候选 LabelVersion/PromptVersion、Eval 与 Release 继续使用该值作为根 `trace_id`。每次自然人提交/仲裁的 HTTP Trace 单独保存为 `action_trace_id`，用于解释“谁在何时做了动作”，但不能切断或替换根链。

### 4.6 Agentic Runtime

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| AgentRun | `agent_run_id`、`agent_type`、`input_scope`、`status`、`model_version`、`cost`、`latency_ms`、`trace_id` | 每次智能运行必须有明确输入范围和可审计输出。 |
| ToolCall | `tool_call_id`、`agent_run_id`、`tool_name`、`input_ref`、`output_ref`、`status`、`duration_ms` | 工具调用必须记录权限、参数、结果、错误和副作用。 |
| AgentDecision | `decision_id`、`agent_run_id`、`decision_type`、`risk_level`、`reason`、`target_ref`、`state` | 高风险决策必须生成 HumanReviewTask 或 blocked 门禁。 |
| TraceRef | `trace_ref_id`、`trace_id`、`object_type`、`object_id`、`module`、`span_ref` | 跨首页、数据、调听、标签、知识库、评测、资产、洞察回跳。 |

Agent 输出对象白名单：

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

### 4.7 Knowledge & Retrieval

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| KnowledgeSource | `knowledge_source_id`、`source_type`、`connector_id`、`status`、`quality_score` | 知识源可来自 SOP、FAQ、产品资料、标签样本和音频证据包。 |
| KnowledgeChunk | `chunk_id`、`knowledge_source_id`、`chunk_key`、`token_count`、`quality_state`、`evidence_ref` | 切片元数据落 MySQL，向量落 Qdrant。 |
| KnowledgeIndex | `knowledge_index_id`、`version`、`embedding_profile`、`status`、`qdrant_collection` | 发布索引版本，不覆盖原始知识源和资产。 |
| KnowledgeQualityGate | `gate_id`、`index_id`、`gate_type`、`value`、`status` | 覆盖新鲜度、切片质量、标签覆盖、实体冲突、证据可回放、召回质量、脱敏。 |
| KnowledgeEffect | `effect_id`、`index_id`、`metric_type`、`value`、`evidence_refs` | 记录证据包输入、知识命中、标签补齐、人审接受和回归沉淀。 |

### 4.8 ASR Hotword Governance

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| HotwordPack | `pack_id`、`name`、`language`、`domain`、`current_version_id`、`resource_version`、`root_trace_id` | 逻辑包只指向同项目已发布版本；历史版本不删除。 |
| HotwordPackVersion | `version_id`、`pack_id`、`version`、`baseline_version_id`、`content_sha256`、`manifest_storage_object_id`、`compiled_provider`、`provider_artifact_ref`、`eval_run_id`、`eval_locked`、`status`、`resource_version` | 候选只能影子运行；构建后冻结版本/provider 绑定；发布要求受信完成的锁定 EvalRun、模型负责人批准和不同自然人的项目管理员确认。 |
| HotwordVersionItem | `item_id`、`version_id`、`canonical_term`、`normalized_term`、`aliases`、`category`、`weight`、`source_type`、`source_badcase_id` | 权重 `0–100`；`knowledge_candidate` 只能发现，不得直接评测/发布；别名显式录入，敏感实体拒绝。 |
| HotwordMetricSnapshot | `snapshot_id`、`bucket_start`、`bucket_end`、`store_id`、`provider`、`model_version`、`hotword_pack_version_id`、计数字段、`root_trace_id` | MySQL 保存统计真值和维度快照；Redis 只缓存，词级诊断在对象存储。 |
| ASRHotwordBadcase | `badcase_id`、`hotword_pack_version_id`、`standard_term`、`recognized_text`、`error_type`、`evidence_ref`、`evidence_level`、`priority_score`、`candidate_state`、`resource_version` | 记录产生坏例时的不可变词包版本；`capability=asr-hotword`，Qdrant 只召回相似坏例，人工决定权在 MySQL。 |

整个闭环沿用同一 `root_trace_id`：原 ASR 资产 → 词级证据 → Badcase → 候选词包版本 → provider 构建 Run → 锁定 EvalDatasetVersion → EvalRun → 词包发布 Run → TaskVersion 草稿 → TaskVersion 发布门禁 → 生产激活 → 新转写资产 → 受控回填记录。构建、评测和发布请求都只返回 pending/blocked RunAction；业务结果由受信完成回执和门禁 worker 物化。

### 4.9 Evaluation & Feedback

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| EvalDataset | `eval_dataset_id`、`capability`、`version`、`scope`、`status` | 支持 ASR、Boundary、Diarization、Tagging、Voiceprint、Prompt、Insight。 |
| EvalCase | `eval_case_id`、`dataset_id`、`evidence_pack_id`、`expected_json`、`source` | 评测样本必须能回跳证据。 |
| EvalRun | `eval_run_id`、`dataset_id`、`baseline_version`、`candidate_version`、`status`、`trace_id` | 同一数据集、标签版本、模型版本、指标口径才可对比。 |
| LabelEvalResult / LabelEvalSuiteResult | binding/dataset/sample/result hashes、六套件 metrics、paired bootstrap、gate results、`passed/blocked`、Trace | 追加只读；六套件各且仅一次，强结果可重算且仅 `passed` 可作为 ReleaseDeployment 输入。 |
| MetricResult | `metric_result_id`、`eval_run_id`、`metric_name`、`value`、`verdict`、`gate_impact` | 指标结果可被 ReleaseGate 消费。 |
| Badcase | `badcase_id`、`capability`、`severity`、`root_cause`、`fix_suggestion`、`status` | badcase 可来自评测失败、人工修正、低置信和冲突仲裁。 |
| FeedbackTask | `feedback_task_id`、`source_type`、`target_type`、`status` | 回流动作包括规则候选、Prompt 建议、重评、人审、资产生成、回填。 |

### 4.10 Data Asset & Lineage

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| DataAsset | `data_asset_id`、`asset_key`、`asset_type`、`data_domain`、`status`、`quality_score` | 资产按租户和项目隔离，第一阶段覆盖音频、ASR、标签、单据事件、评测、报告。 |
| AssetVersion | `asset_version_id`、`data_asset_id`、`version`、`model_version`、`label_version_id`、`schema_version` | 数据版本、模型版本、标签版本和 Schema 版本都要保留。 |
| AssetPartition | `partition_id`、`asset_version_id`、`partition_key`、`time_range`、`location_scope`、`status` | 支持 `daily/store/hour`、`daily/store/event` 等分区。 |
| Materialization | `materialization_id`、`asset_version_id`、`task_run_id`、`storage_object_id`、`status` | 业务上展示为资产生成记录。 |
| LineageEdge | `edge_id`、`upstream_asset_id`、`downstream_asset_id`、`relation_type` | 资产血缘以业务语言展示 SourceAsset、Asset、AssetCheck、Backfill、Human Loop、External Callback、Report/Insight。 |
| AssetQualityCheck | `check_id`、`asset_version_id`、`check_type`、`status`、`score` | 完整性、及时性、一致性、重复率、缺失率、Schema 稳定性、评测覆盖率。 |
| BackfillRequest | `backfill_id`、`asset_id`、`scope_json`、`overwrite_policy`、`approval_status`、`status` | 覆盖已有结果或重算下游必须展示影响范围并进入审批。 |
| ExportJob | `export_job_id`、`export_type`、`scope_json`、`storage_object_id`、`status` | 导出必须审计、可追踪、可重试。 |

### 4.11 Insight & Reporting

| 聚合/实体 | 关键字段 | 领域规则 |
| --- | --- | --- |
| InsightMetric | `metric_id`、`metric_key`、`time_bucket`、`dimension_json`、`value` | 第一阶段用 MySQL 聚合和预计算表生成趋势、漏斗、雷达、排行榜。 |
| InsightFact | `fact_id`、`fact_type`、`summary`、`evidence_pack_id`、`asset_key`、`confidence` | 洞察必须能回答指标变了什么、为什么变、下钻到哪些证据和动作。 |
| MetricResult / MetricResultLabelScope | result payload/hash、`label_version_applicability`、taxonomy mode、source/target versions、mapping bundle、FactSet generation、`fact_as_of`、definition/source/scope hashes、comparability | 通用结果与可选一对一标签 scope 都 append-only；标签指标缺强 scope 必须 fail closed。 |
| InsightReport | `report_id`、`title`、`metric_result_ids`、`metric_scope_sha256`、`status`、`storage_object_id` | 报告只引用同 scope 已物化快照；Qdrant 只提供解释，不能重算指标。 |

标签统计统一使用 `native`、`normalized`、`recomputed`。native 按原版本分区；normalized 冻结已发布 mapping bundle；recomputed 只消费已审批 FactSet。`occurred_at` 决定业务分桶和标签适用期，`recorded_at` 表示平台何时得知，`fact_as_of` 冻结指标可见截止点。完整规则以 `label-lifecycle-statistics.md` 为权威。

## 5. 实体关系主线

```text
Tenant
  -> Project
    -> Connector / DataSourceBinding
      -> SyncRun / SourceRecord
    -> TaskType
      -> CanvasVersion
      -> TaskVersion
        -> TaskRun
        -> OutputBinding / CallbackBinding
    -> AudioSession
      -> AudioRecording
      -> ConversationBoundary
      -> AudioSegment / VADSegment / SpeakerTurn / ASRSegment
      -> EvidenceTrack / TrackRegion
      -> EvidencePack
    -> BusinessEvent / BusinessDocument
      -> EventLink
    -> LabelTaxonomy
      -> LabelVersion
        -> LabelCandidate
        -> ChangeSetDraft
        -> ReleaseGate
    -> HumanReviewTask
    -> AgentRun / ToolCall / TraceRef
    -> KnowledgeSource / KnowledgeChunk / KnowledgeIndex
    -> HotwordPack / HotwordPackVersion / HotwordMetricSnapshot
      -> EvalRun / ASRHotwordBadcase / TaskVersionDraft
    -> EvalDataset / EvalRun / Badcase
    -> DataAsset / AssetVersion / Materialization / LineageEdge
    -> InsightMetric / InsightFact / InsightReport
```

关键关系：

- `AudioSession` 是调听、数据管理、证据审查、串音矩阵和知识证据包的中心对象。
- `ConversationBoundary` 是用户可编辑的完整对话窗口，其他轨道是派生结果或跟随重建结果。
- `EventLink` 是业务单据、业务事件、ASR、音频片段、标签和证据包之间的统一关系对象。
- `EvidencePack` 是跨模块回跳最小证据单元，可被标签、评测、知识库、洞察、资产引用。
- `LabelCandidate`、`ChangeSetDraft`、`HumanReviewTask` 和 `ReleaseGate` 共同保证 Agent 不直接覆盖线上结果。
- `DataAsset` 是任务、音频、标签、评测、洞察和外部回写的长期可复用产物。

## 6. 关键 ID 与引用规则

| ID | 建议格式 | 规则 |
| --- | --- | --- |
| `tenant_id` | ULID 或稳定 slug | 所有业务对象必带。禁止跨租户 join 或召回。 |
| `project_id` | ULID 或稳定 slug | 项目内业务对象必带。租户全局配置可 `project_id = NULL`，但必须显式标记继承范围。 |
| `user_id` | ULID | 用于成员关系、审计、人工动作。 |
| `store_id` | ULID 或外部稳定主键映射 | 门店可同租户跨项目复用，但业务数据仍按项目授权过滤。 |
| `device_id` | ULID | 工牌、展厅麦、试驾车、PBX 通道统一设备 ID。 |
| `audio_session_id` | ULID | 调听和证据链主 ID。 |
| `recording_id` | ULID，保留 `external_recording_id` | 原始录音主键，外部录音 ID 只做来源字段。 |
| `segment_id` | ULID | 片段、VAD、ASR、Diar、标签轨道的时间坐标引用。 |
| `event_id` | ULID，保留 `external_event_id` | 业务事件主键，外部事件 ID 用唯一约束保证幂等。 |
| `document_id` | ULID | 单据主键，`document_no` 不一定全局唯一，唯一性应带租户、项目、类型、来源系统。 |
| `event_link_id` | ULID | 事件关联关系主键。 |
| `evidence_pack_id` | ULID | 证据包主键，也是 Qdrant 和 DeepLink 的回跳对象。 |
| `label_version_id` | ULID | 所有标签结果、候选、评测、洞察和资产必须记录。 |
| `agent_run_id` | ULID | 一次智能运行主键。 |
| `trace_id` | OpenTelemetry trace id | 跨 BFF、任务、Agent、工具、对象存储、Qdrant 的追踪键。 |
| `root_trace_id` | 闭环根 Trace | 跨多次分析、评测、发布和回填保持稳定；单次请求另写 `current_trace_id`。 |
| `asset_key` | 逻辑路径 | 例如 `auris/label/event_tags`，在同租户项目内唯一，跨版本用 `asset_version_id` 区分。 |
| `partition_key` | 业务分区字符串 | 例如 `2025-05-26|aurora-center|quote-risk`。 |
| `storage_object_id` | ULID | 指向对象存储元数据，不把裸 URL 作为业务主键。 |

## 7. 租户与项目隔离原则

1. 所有核心业务表必须有 `tenant_id` 和 `project_id`。租户级全局表必须显式命名为 tenant scoped，并通过绑定表下发到项目。
2. BFF 在请求入口解析并校验 `tenant_id`、`project_id`、`user_id`、`store_id`、`date`、`model_version`、`label_version`，服务层不得信任前端传入的未校验上下文。
3. MySQL 查询必须默认加 `tenant_id` 和 `project_id` 条件。跨项目聚合只允许在同租户内、且用户具备跨项目权限时执行。
4. 对象存储 key 必须带租户和项目路径，例如 `tenants/{tenant_id}/projects/{project_id}/audio/raw/{date}/{recording_id}.wav`。
5. Qdrant payload 必须带 `tenant_id`、`project_id`、`asset_key`、`source_type`、`source_id`、`version`、`trace_id` 和可回跳业务 ID。召回后必须回 MySQL 二次鉴权。
6. Agentic Runtime、Knowledge Retrieval 和 Insight 召回不得绕过权限系统访问跨租户数据。
7. 外部回写、导出、批量回填和发布都必须记录 `idempotency_key`、`trace_id`、影响范围和审计日志。
8. 高风险配置变更必须进入 ReleaseGate 或 HumanReviewTask，包括 provider 替换、阈值大幅调整、批量接受候选、覆盖人工标注、批量回填、外部回写。

## 8. 前端模块到领域对象映射

| 前端模块 | 原型关键对象/状态 | 后端领域对象 | 最小接口闭环 |
| --- | --- | --- | --- |
| `home` 首页 | `homeRunOverview`、`homeRunQueues`、`homeAgentTasks`、`homeEvidenceChains` | InsightMetric、TaskRun、HumanReviewTask、DataAsset、EvidencePack、AgentDecision | 运营摘要、待办、人审队列、最近资产、证据链下钻。 |
| `tenants` 租户 | `tenantRows`、账号设置、审计状态 | Tenant、TenantMember、Quota、AuditLog、ModelServiceBinding | 租户列表、详情、配额、成员、审计。 |
| `projects` 项目 | `projectRows`、`projectSources`、项目质量目标 | Project、ProjectMember、DataSourceBinding、LabelVersion、QualityTarget | 项目列表、项目详情、数据源绑定、成员和质量目标。 |
| `canvas` 任务配置 | `CanvasNode`、`MappingSuggestion`、`DagsterRunDraft`、`AssetOutputContract`、`TaskDraftValidation` | TaskType、CanvasVersion、TaskVersion、TaskNode、MappingSuggestion、TaskRun、OutputBinding | 保存草稿、校验、发布、运行一次、调度、A/B、回写配置。 |
| `data` 数据管理 | `DataAssetItem`、`DataAggregateKey`、`VoiceprintRecord`、`dataTree` | AudioSession、AudioRecording、Person、Voiceprint、BusinessEvent、BusinessDocument、EventLink、AggregationView | 音频列表、聚合树、声纹、事件关联、进入调听。 |
| `knowledge` 知识库 | `KnowledgeSource`、`KnowledgeChunkPreview`、`KnowledgeGraphPath`、`KnowledgeQualityGate` | KnowledgeSource、KnowledgeChunk、KnowledgeIndex、KnowledgeQualityGate、KnowledgeEffect、HumanReviewTask | 知识源同步、切片、索引构建、质量检测、效果报告、证据回跳。 |
| `listening` 调听 | `Mode`、`PanelTab`、`TrackRegion`、`StitchedWavSlice`、`BoundaryExtensionCandidate`、`ReviewSample` | AudioSession、ConversationBoundary、EvidenceTrack、TrackRegion、ASRSegment、SpeakerTurn、EventLink、EvidencePack、ReviewDecision | 会话详情、保存边界、创建证据包、提交人审决策。 |
| `labels` 标签治理 | `LabelIntentFlow`、`LabelOptimizationRun`、`LabelCandidate`、`ReleaseGateCheck`、`AutomationLevelKey` | LabelTaxonomy、LabelVersion、LabelCandidate、LabelOptimizationRun、PromptVersion、ChangeSetDraft、ReleaseGate、HumanReviewTask | 标签列表、版本、优化运行、候选、人审、发布门禁。 |
| `insights` 洞察 | `InsightFact`、指标卡、趋势、证据下钻、热词质量 | InsightMetric、InsightFact、InsightReport、EvidencePack、DataAsset、HotwordMetricSnapshot | 指标、趋势、热词覆盖/召回/易错/误增强、报告、证据解释和 Badcase 下钻。 |
| `evaluation` 评测 | `EvaluationCapabilityRows`、`EvaluationDatasetProfile`、`EvaluationBadcaseWorkflowItem`、`ASR 热词`能力 | EvalDataset、EvalCase、EvalRun、MetricResult、Badcase、FeedbackTask、HotwordPackVersion | 评测集、固定基线/候选模型对比、ASR 热词 Badcase、影子复测和回流任务。 |
| `assets` 数据资产 | `assetRows`、`assetRunTimeline`、`assetApiContracts` | DataAsset、AssetVersion、AssetPartition、Materialization、LineageEdge、AssetQualityCheck、BackfillRequest、ExportJob | 资产目录、血缘、生成记录、质量、回填、导出。 |
| `settings` 设置 | `asrServiceProfile`、`audioServiceParamGroups`、工具配置、阈值配置 | ModelProvider、ModelService、ToolRegistry、ThresholdConfig、PolicyGuard、SecretRef、AuditLog | Provider 注册、服务测试、工具配置、阈值发布、策略审计。 |

## 9. 第一阶段领域边界

第一阶段必须落地：

- 上下文与权限：Tenant、Project、User、Member、Role、AuditLog。
- 核心资源 API：Connector、TaskType、TaskVersion、TaskRun、AudioSession、ConversationBoundary、EvidencePack、LabelVersion、HumanReviewTask、DataAsset、Settings。
- 调听证据链：AudioSession、AudioRecording、ConversationBoundary、EvidenceTrack、ASRSegment、SpeakerTurn、EventLink、EvidencePack、ReviewDecision。
- 标签闭环：LabelTaxonomy、LabelVersion、LabelCandidate、LabelOptimizationRun、ReleaseGate、HumanReviewTask。
- ASR 热词闭环：HotwordPack、HotwordPackVersion、HotwordVersionItem、HotwordMetricSnapshot、`capability=asr-hotword` Badcase、影子 EvalRun 和 TaskVersion 草稿。
- 异步运行：TaskRun、AgentRun、EvalRun、ExportJob、BackfillRequest、ExternalCallback 统一状态。
- Qdrant 索引元数据：KnowledgeSource、KnowledgeChunk、KnowledgeIndex、KnowledgeQualityGate。

第一阶段可以延后但模型需预留：

- 完整资产血缘图高级诊断。
- 全链路 Trace Replay UI。
- 复杂洞察可视化和高级报告编排。
- 自动化 L4 发布。
- 跨项目声纹自动合并。
- 大规模离线指标专用数仓。
