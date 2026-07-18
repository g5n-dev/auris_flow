# 数据库迁移计划

本文档把 `db-schema.md` 转成 Alembic 迁移顺序。原则：所有生产变更走迁移；DDL 与数据迁移分开；已发布迁移不可修改；生产回滚使用新的 forward migration。

## 1. 迁移工具和命名

- 工具：Alembic + SQLAlchemy 2.x。
- 迁移目录：`backend/migrations/versions/`。
- 文件命名：当前仓库采用 `NNNN_<scope>.py` 连续 revision 文件；后续如切换时间戳命名，必须在新迁移开始前统一更新 Alembic revision 规范，不改写已发布文件。
- 每个迁移必须包含 `upgrade()` 和 `downgrade()`；不可逆迁移必须在文件头说明原因。
- 大表新增索引、批量回填、状态重算必须拆成独立迁移或后台 backfill job。

## 2. 总体顺序

| 批次 | 迁移主题 | 目的 | 依赖 |
| --- | --- | --- | --- |
| 0001 | `core_tables` | 租户、项目、用户、通用 JSON 资源、运行、审计、outbox 基线 | 无 |
| 0002 | `domain_baseline_tables` | 核心领域强表基线 | 0001 |
| 0003 | `agentic_execution_tables` | Agent 执行、工具调用和运行投影 | 0002 |
| 0004 | `audio_review_projection_tables` | 调听、人审和音频证据投影 | 0003 |
| 0005 | `prompt_version_candidates_table` | Prompt 候选版本 | 0004 |
| 0006 | `knowledge_effects_table` | 知识库效果与召回解释 | 0005 |
| 0007 | `asset_lineage_edges_table` | 资产血缘边 | 0006 |
| 0008 | `storage_objects_table` | 对象存储登记、Range 播放和对象元数据 | 0007 |
| 0009 | `outbox_delivery_leases` | outbox 租约领取与并发派发保护 | 0008 |
| 0010 | `label_policy_engine` | 标签策略引擎、版本和评估事实 | 0009 |
| 0011 | `outbox_delivery_attempts` | outbox 派发尝试记录和重试观测 | 0010 |
| 0012 | `insight_action_closure` | 洞察行动闭环、报告完成和效果回写 | 0011 |
| 0013 | `human_review_single_terminal` | 人审单终态写保护 | 0012 |
| 0014 | `insight_causal_foreign_keys` | 洞察因果链外键和引用完整性 | 0013 |
| 0015 | `idempotency_and_completion_receipts` | 幂等记录强化和完成回执 inbox | 0014 |
| 0016 | `outbox_reconciliation_state` | outbox 对账状态和失败收敛 | 0015 |
| 0017 | `quality_appeals` | 质检申诉、独立复议和不可变原决定 | 0016 |
| 0018 | `blind_calibration_gold_loop` | 盲审校准、黄金集闭环和冲突仲裁 | 0017 |
| 0019 | `calibration_hardening` | 校准一致性、并发保护和硬化约束 | 0018 |
| 0020 | `auth_sessions` | 服务端认证会话 | 0019 |
| 0021 | `hotword_governance` | ASR 热词四张强表、Badcase 热词投影和乐观锁 | 0020 |
| 0022 | `eval_dataset_versions` | 固定评测集版本、清单哈希和锁定快照 | 0021 |
| 0023 | `hotword_production_activation` | 区分候选当前版本与 TaskVersion 门禁后的生产版本 | 0022 |
| 0024 | `asr_annotation_corrections` | 追加式 ASR 修正观察、词级证据与不可变约束 | 0023 |
| 0025 | `eval_dataset_object_lock` | 固定评测集 manifest 的 Provider/bucket/key、Content-Length 与强 ETag 对象锁 | 0024 |
| 0026 | `label_closed_loop` | 标签闭环 Observation、冲突、优化运行与建议强表 | 0025 |
| 0027 | `label_eval_results` | 标签评测结果与统计物化链 | 0026 |
| 0028 | `release_active_head` | ReleaseCommand 与环境 active head CAS | 0027 |
| 0029 | `label_calibration_fact_chain` | 追加式校准版本、Fact 与 MetricSnapshot | 0028 |
| 0030 | `label_optimization_runtime` | 优化调度、轮次与运行时状态 | 0029 |
| 0031 | `scene_profiles` | 场景 Profile 与不可变版本 | 0030 |
| 0032 | `controlled_experiments` | 受控实验与分配证据 | 0031 |
| 0033 | `task_version_release_heads` | TaskVersion 发布 head 与 generation | 0032 |
| 0034 | `label_lifecycle_mapping_expand` | 标签生命周期、替代与映射 expand | 0033 |
| 0035 | `label_fact_temporal_heads` | Fact 时间窗口与 current head 投影 | 0034 |
| 0036 | `label_metric_snapshot_scopes` | MetricSnapshot 冻结 scope 与可比性 | 0035 |
| 0037 | `label_fact_logical_active_heads` | Fact logical key active head | 0036 |
| 0038 | `release_head_interval_closure` | 发布 generation 生效区间一次闭合 | 0037 |
| 0039 | `label_fact_append_only_contract` | Fact append-only Contract 与 head 唯一真相源 | 0038 |
| 0040 | `label_recomputation_fact_sets` | 全量重算 candidate FactSet/namespace | 0039 |
| 0041 | `oidc_browser_sessions` | OIDC identity/state 与 hash-only browser session；当前 head | 0040 |

## 3. 0001 Platform Foundation

创建：

- `tenants`
- `tenant_quotas`
- `users`
- `roles`
- `tenant_members`
- `projects`
- `project_members`
- `stores`
- `devices`
- `service_accounts`
- `audit_logs`

约束：

- `tenants.slug` 唯一。
- `projects(tenant_id, slug)` 唯一。
- `tenant_members(tenant_id, user_id)` 唯一。
- `project_members(project_id, user_id)` 唯一。
- 所有业务表预留 `created_at`、`updated_at`、`created_by`、`updated_by`。

## 4. 0002 Runtime Foundation

创建：

- `idempotency_records`
- `run_records`
- `run_errors`
- `outbox_events`
- `trace_refs`

要点：

- `idempotency_records(tenant_id, project_id, user_id, operation, idempotency_key)` 唯一，避免不同用户共享重放结果。
- `outbox_events(status, available_at, created_at)` 建索引，并保留 `attempt_count`、`last_error`；第一阶段死信用 `status = dead_letter` 表达，后续可按运维归档到独立 `dead_letter_events`。
- `run_records` 保存通用 `run_id`、`run_type`、`status`、`partition_key`、`trace_id`。
- 后续领域运行表可引用 `run_records.run_id`，也可使用同 ID 作为专表主键。

## 5. 0003 Connector Ingestion

创建：

- `connectors`
- `data_source_bindings`
- `sync_cursors`
- `sync_runs`
- `source_records`
- `mapping_suggestions`

安全要求：

- `connectors` 只保存 `secret_ref_id`，不保存明文 token。
- `source_records.raw_payload_storage_id` 指向对象存储引用。
- 游标更新必须与同步运行成功状态在同事务提交。

## 6. 0004 Task Execution

创建：

- `task_types`
- `task_canvas_versions`
- `task_versions`
- `task_nodes`
- `task_edges`
- `task_schedules`
- `task_experiments`
- `task_runs`
- `task_run_steps`
- `output_bindings`
- `external_callback_receipts`

迁移规则：

- `task_versions(task_type_id, version)` 唯一。
- `task_runs(tenant_id, project_id, idempotency_key)` 唯一。
- 发布版本时不要靠迁移更新数据；发布是业务动作，由 service 事务控制。

## 7. 0005 Audio Evidence

创建：

- `audio_sessions`
- `audio_recordings`
- `audio_segments`
- `conversation_boundaries`
- `vad_segments`
- `speaker_turns`
- `asr_segments`
- `business_documents`
- `event_links`
- `evidence_packs`
- `voiceprint_enrollments`

注意：

- 原始音频、切片、ASR JSON、Diar JSON 只存对象引用。
- `conversation_boundaries(audio_session_id, version)` 唯一。
- 大字段转写不要进入审计日志；完整转写存对象存储或分段表。

## 8. 0006 Label Governance

创建：

- `label_taxonomies`
- `label_nodes`
- `label_versions`
- `prompt_versions`
- `label_candidates`
- `label_conflicts`
- `change_set_drafts`
- `release_gates`
- `human_review_tasks`
- `human_review_decisions`

规则：

- 候选不覆盖线上标签，只写 `label_candidates`。
- 发布门禁必须引用评测、badcase、人审和影响资产。
- 人审决策与候选状态变更必须同事务写审计。

## 9. 0007 Agent & Knowledge

创建：

- `agent_runs`
- `tool_calls`
- `agent_decisions`
- `knowledge_sources`
- `knowledge_chunks`
- `knowledge_indexes`
- `knowledge_quality_gates`
- `knowledge_effect_metrics`
- `knowledge_recall_logs`

规则：

- `knowledge_chunks` 只保存切片元数据；向量由 Qdrant 保存。
- Qdrant payload 必须能回跳 `tenant_id`、`project_id`、`source_type`、`source_id`、`asset_key`、`trace_id`。
- Agent 决策高风险时必须创建人审或门禁对象。

## 10. 0008 Evaluation Feedback

创建：

- `eval_datasets`
- `eval_cases`
- `eval_runs`
- `metric_results`
- `badcases`
- `feedback_tasks`

规则：

- 评测运行必须固化模型版本、标签版本、Prompt 版本和数据集版本。
- badcase 回流目标限定：标签规则、Prompt 优化、打标黄金集、模型评测集。

## 11. 0009 Assets Lineage

创建：

- `data_assets`
- `asset_versions`
- `asset_partitions`
- `asset_materializations`
- `lineage_edges`
- `asset_quality_checks`
- `backfill_requests`
- `export_jobs`

规则：

- `data_assets(tenant_id, project_id, asset_key)` 唯一。
- 回填请求必须保存影响范围、审批状态、回滚引用。
- 导出必须保存脱敏策略、水印、下载审计引用。

## 12. 0010 Insights Settings

创建：

- `insight_metrics`
- `insight_facts`
- `insight_reports`
- `model_providers`
- `model_services`
- `tool_registry`
- `threshold_configs`
- `policy_guards`
- `secret_refs`
- `notification_rules`

规则：

- 洞察第一阶段用 MySQL 预计算表和 Redis 缓存，不引入 ClickHouse。
- `secret_refs` 不保存密钥明文，只保存外部密钥管理引用。
- 高风险设置修改创建草稿和审批，不直接覆盖线上配置。

## 13. 0011 Seed References

写入基础数据：

- 系统角色和权限。
- `agent_service`、`worker_service` 服务账号。
- 默认状态枚举、错误码分类、审计动作类型。
- 极光汽车联调用基础租户、项目、门店、设备。

种子数据只用于开发和联调环境；生产环境通过管理入口或迁移审批单写入。

## 14. 0021 ASR Hotword Governance

`0021_hotword_governance.py` 是仅包含 DDL 的扩展迁移，不在同一事务内写种子或批量改写历史 JSON：

- 创建 `hotword_packs`、`hotword_pack_versions`、`hotword_version_items`、`hotword_metric_snapshots`，所有唯一约束、外键和索引都带 `tenant_id + project_id` 范围。
- `hotword_pack_versions.status` 添加完整状态约束；增加 `compiled_provider`，与 `content_sha256`、manifest、provider artifact 组成构建后冻结绑定；`resource_version > 0` 用于乐观锁。
- 以 expand 方式给既有 `badcases` 增加 nullable 的 `capability/error_type/standard_term/recognized_text/evidence_ref/evidence_level/root_trace_id/current_trace_id` 等 ASR 投影字段，以及带安全默认值的计数字段、`resource_version` 和 `downstream_impact`。旧 Badcase 继续可读写，不要求一次性回填。
- `hotword_version_items.weight` 强制 `0–100`，增加 `source_type=manual/badcase/knowledge_candidate`，同版本 `normalized_term` 唯一；应用层在落库前执行 NFKC、标点/空白清理、拉丁字母小写和敏感实体阻断，知识候选禁止直发。
- `badcases.hotword_pack_version_id` 保存坏例产生时的不可变版本；修复版本、新转写和回填记录另建血缘，禁止覆盖该引用。
- MySQL 生产索引按在线 DDL 能力分批创建并观测锁等待；SQLite 仅用于契约测试，不作为生产迁移语义。

迁移发布按 expand → migrate → contract 执行：

1. **Expand**：先部署 0021，新列保持 nullable/安全默认值，旧服务仍可运行。
2. **Migrate**：部署双读兼容。历史 TaskVersion 的 `hotwords_ref` 只读映射到种子版本“汽车销售热词包 v1.8”；任何 POST/PATCH/运行覆盖都拒绝写入旧字段，新写入只接受 `hotword_pack_version_id`。
3. **Backfill**：独立、可暂停的后台 job 按 `tenant_id/project_id/id` 游标批量补 `root_trace_id` 和可确定的 ASR Badcase 投影；每批提交并记录进度，不在 Alembic DDL 事务中扫全表。
4. **Verify**：比对旧引用映射命中率、四表范围外键、规范词唯一性、Badcase 计数和 root trace 连续性；失败只停止新写，不删除历史数据。
5. **Contract**：构建、评测和发布先创建 RunRecord/Outbox，受信完成回执才能固化 provider 产物、metrics/gate 或 published/TaskVersion draft；至少一个兼容窗口后，才可用新的 forward migration 收紧经验证字段。旧 `hotwords_ref` 的读取兼容在所有历史 TaskVersion 完成迁移前不得删除。

开发/联调的 v1.8 词包、A-4107、评测集和指标快照由 `seed-fixture-v0.1.json` 加载，和 0021 DDL 分离。生产不得自动导入演示词项。回滚 0021 只允许在尚无生产热词数据时执行 downgrade；已有发布版本时使用新的 forward migration 或业务回滚，不直接 drop 四表。

`0024_asr_annotation_corrections.py` 在既有标注最新投影之外增加 append-only 的 ASR 文本修正观察表。迁移建立项目范围内的标注、证据和语义指纹唯一约束、词包版本/Badcase 复合外键、统计维度索引，以及 SQLite/MySQL 的 UPDATE/DELETE 拒绝触发器。它不搬运旧 `listening_annotations`：历史任意 JSON 草稿缺少受控词级证据，不能回推成热词事实。

## 15. 0026–0029 Label Closed Loop Strong Facts

迁移链固定为 `0026_label_closed_loop → 0027_label_eval_results → 0028_release_active_head → 0029_label_calibration_fact_chain`，每一版只做可回滚 DDL/确定性约束，不在 Alembic 事务里执行模型调用或大批业务回填：

- **0026** 创建 LabelNode/VersionItem、ExtractionRun、append-only Observation、AggregationPolicy/Run/Aggregate/Member、Fact、FeedbackExample、TaxonomySuggestion、PromptAsset/Version 和 ReleaseDeployment 强表。Observation 安装 SQLite/MySQL UPDATE/DELETE 拒绝触发器；所有业务唯一键和检索索引带 tenant/project 范围。
- **0027** 创建 append-only `label_eval_results/label_eval_suite_results`，固化 binding/dataset/sample/result hashes、六套件明细、paired bootstrap 和门禁结果；运行时在发布前重算，不信任客户端 `passed`。
- **0028** 创建 `release_commands/release_bundle_heads`。每个部署只有一个 active command，每个环境只有一个 active head；命令冻结 expected head generation/deployment/hash，受信 ACK 后 CAS 切换。数据库触发器同时拒绝同环境多个 completed 100% 发布头。
- **0029** 创建 append-only `label_calibration_versions`，复合 FK 绑定 GoldSetVersion；为 `label_facts` expand 增加 nullable `active_slot`，按 `created_at DESC, fact_id DESC` 确定性折叠历史重复 active head，再加状态 CHECK 和 subject+label+active_slot 唯一索引。

上线顺序遵循 expand → backfill/read-switch → enforce：先部署 0026/0027 并保持旧候选投影只读兼容；再启用强抽取 Manifest、自动聚合与强 Eval；0028 上线后先由自然人对每个已有环境一次性 bootstrap Last Known Good，随后所有发布只走 ReleaseCommand ACK；0029 校准器发布并绑定 L2 策略后才允许 L2 自动接受。生产发现历史重复 Fact 时不得跳过 0029 的确定性 backfill 或手工删历史。

## 16. 0030 Label Optimization Runtime

`0030_label_optimization_runtime.py` 在标签强版本、强 Eval 结果和校准事实链之后增加可运行但不自动发布的优化控制层：

- `label_optimization_schedules`：每个 tenant/project/label version 唯一，保存完整锁定 Bundle、15m/daily/weekly 时钟、预算、当前 root run 和短租约 claim。Worker 使用条件 UPDATE 获取数据库 scope mutex，不依赖进程内锁。
- `label_optimization_metric_snapshots`：append-only baseline/window 指标、规范 reason counts、非法 JSON/自由文本 reason 拒绝记录和 deterministic hash；SQLite/MySQL 都安装 UPDATE/DELETE 拒绝触发器。
- `label_optimization_rounds`：root/generation run、1–3 轮、候选和锁定 EvalRun、质量收益、关键回退、成本、连续失败、stop reason 与 `awaiting-review/blocked` 终态。

Worker reconcile 不依赖回执是否到达：每次 tick 从同一 session 最早 Round 的 `started_at` 起算 `max_elapsed_seconds`（默认 7200）；到期后把仍 queued/submitted/running 的 generation 与 EvalRun 一次性标记 `blocked/time_budget_exceeded`，同步阻断 Round/root run 并清空 schedule `active_run_id`，防止外部卡住任务永久占用 scope mutex。

上线按 expand → worker disabled → backfill schedules → canary enable 执行。迁移默认不创建任何租户计划，`LABEL_OPTIMIZATION_SCHEDULER_ENABLED=false`；项目管理员锁定 Bundle 后显式创建 Schedule。Worker 与 API 可先双读现有 trigger scan，稳定后再把旧手动扫描降为诊断入口。回滚前必须暂停 worker 并确认无 active run；已有快照和 round 时生产环境使用 forward migration 归档，不直接 drop 证据。

## 17. 0034 Label Lifecycle And Mapping Expand

`0034_label_lifecycle_mapping_expand.py` 只做 expand，不在 Alembic DDL 中回填业务数据：

- 创建 `label_taxonomies` 强表；给 `label_versions` 增加 nullable taxonomy/semantic version/base/replacement/artifact status/timestamps/hash，给 `label_version_items` 增加 nullable definition hash。旧 `status/payload` 保持原样，legacy writer 可继续运行。
- 创建 `label_mapping_versions/items/item_targets`。每个 source item 只有一个 disposition，target 逐行做 tenant/project/version/item 复合 FK；retire 不能插 target，split 必须 `requires_recompute=true`。
- 创建 `label_mapping_bundles/sources/members/paths`。source set、edge content SHA、最终 target version 都有强绑定；JSON 数组只作 canonical manifest 投影，不作跨 scope 权威引用。
- 创建 append-only `release_bundle_head_events`，保存连续 generation、activation status、drain/rollback 动作、old/new deployment/label version、effective interval、command/completion receipt 和 Trace；`release_bundle_heads` 仍是当前投影。
- published/superseded/archived Mapping 禁止内容回写、状态回退和删除；所有 item/target/source/member/path 及 activation ledger 禁止 UPDATE/DELETE，published 后也禁止追加子行。

S2.b 已在应用层实现可暂停、可重入的 taxonomy/LabelVersion 强字段回填、双写和影子比对：每批使用持久化游标与行级 savepoint，歧义、FK 未就绪和唯一约束冲突进入 `migration-required`，终态调用按同一 `run_id` 重放且不重复写 Audit/Outbox。兼容读保留旧 payload 形状并补充已物化强字段，同时输出不含业务原文的 shadow diff 日志。本阶段仍保持 nullable，不执行 Contract 删除或 NOT NULL 收紧。

中央 `JsonResource` 投影以及评测锁定、策略发布、taxonomy candidate 和 LabelVersionItem 物化路径均执行强字段双写；冲突只报告/阻断，禁止覆盖已存在的强字段。生产若已写入 0034 新制品，不执行 destructive downgrade；暂停新 writer 后切回兼容读，并以 forward migration 补偿。

`0038_release_head_interval_closure.py` 将 activation ledger 的全 UPDATE 拒绝触发器替换为 write-once interval closure：只允许既有 event 的 `effective_to` 从 NULL 写为不早于 `effective_from` 的边界，其余列、二次闭合和 DELETE 仍拒绝；新 generation INSERT 必须找到同 scope/environment 的上一 generation，且上一 `effective_to`、新 `effective_from` 与 old/new pointer 连续一致。应用在 Head generation CAS、上一 interval 闭合和新 event 追加之间保持单事务。downgrade 只恢复旧版全 UPDATE 拒绝触发器，不删除任何 ledger 行。

`0039_label_fact_append_only_contract.py` 在 Contract 前验证每条 Fact 链 revision 从 1 连续、supersedes 指向同链前一 revision、Head 指向最新 revision；随后移除 Fact `active_slot` current 唯一索引，使 `label_fact_heads` 成为唯一 current 投影。升级不 UPDATE 任何既有 Fact，legacy `active/superseded` 状态逐值保留；新 Fact 只能插入为 `recorded/NULL`，数据库触发器拒绝所有 Fact UPDATE/DELETE。若 downgrade 需要把新 `recorded` 行反写为旧投影则 fail closed，生产使用 forward compensation。

`0040_label_recomputation_fact_sets.py` 在 `0039` append-only Fact 契约之上增加 `label_recompute_runs/items` 强表、冻结 source FactSet Head generation/manifest、target/bundle、candidate namespace、分区/资产范围、coverage/budget、执行回执与 Trace，并为 candidate Fact 增加同 scope recompute-item 复合 FK。升级只扩展 source union 和 `aggregate_id` nullable 布局，不改写任何既有 LabelFact；历史 human-decision 行的 `aggregate_id/payload/content_sha256` 逐值保持不变。新 human-decision 写入由 API 与 INSERT trigger 强制 `aggregate_id=NULL`，reviewed Aggregate 仅作为 payload lineage。SQLite 在事务写锁内随 batch recreate 重建三类守卫；MySQL/MariaDB 的 Fact ALTER 全程保留 UPDATE/DELETE 守卫，INSERT 守卫用 `next → replace → remove next` 双触发器顺序切换，避免无保护空窗。downgrade 恢复 0039 legacy human insert 契约；在已有 candidate Fact、新布局 human Fact 或任一不可变 RecomputeRun/RunItem 历史时 fail closed。所有阻断查询必须在 DROP TRIGGER/DDL 之前完成，失败后 revision、复合 FK、Fact 字节和 append-only 触发器保持不变。

`0041_oidc_browser_sessions.py` 在不修改既有 `users/auth_sessions` 兼容语义的前提下，expand 创建 `user_security_states`、`oidc_identities`、`oidc_authorization_states` 与 `browser_auth_sessions` 四张强表。升级为每个既有用户回填 `status=active, authz_version=1` 的安全状态；OIDC identity 必须由维护者显式 provision，并以 issuer/subject SHA-256 唯一映射内部 user/tenant/project。授权 state 只保存 hash，短期 verifier/nonce 一次性消费；浏览器 session 只保存 cookie 与 CSRF 的 SHA-256，原值不得持久化或记录日志。应用发布顺序为 expand → provision identity → 启用 OIDC canary → 切换前端 cookie session；Keycloak 只能作为参考 IdP，切换不得引入其私有协议依赖。downgrade 只按 FK 逆序删除四张 0041 表，保留全部权威用户与旧开发会话；生产已有活跃 OIDC 会话时优先用 forward migration，回退前先撤销/过期会话并切回受控认证路径。

## 18. 迁移验收

- 空库可完整执行到最新版本。
- 重复执行迁移命令无脏状态。
- 回滚最近一版在本地可执行；生产回滚使用 forward migration。
- `tenant_id + project_id` 索引覆盖所有项目级大表。
- 幂等、审计、outbox 表先于任何业务写接口上线。
- 迁移图必须线性到当前最新 revision，并能在空库本地完整 downgrade/upgrade；0026 的 Observation、0027 的 Eval、0029 的 Calibration/MetricSnapshot、0034 的 Mapping、0038 activation interval write-once closure、0039 append-only Fact Contract、0040 full recompute 强表，以及 0041 OIDC/browser session hash-only 强表在两种方言都可验证。已有 Contract 数据的破坏性 downgrade 应 fail closed。
- 0028 后每个 environment 只有一个 `release_bundle_heads` 行、每个 deployment 只有一个 active ReleaseCommand；0039 后每个 scope/namespace/logical key 只有一个 `label_fact_heads` current 指针，Fact 行本身不再承担可变 current 状态。
