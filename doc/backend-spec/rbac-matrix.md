# Auris Flow RBAC 与自动化边界规格

本文档定义 Auris Flow 后端第一阶段的角色、数据范围、模块动作矩阵、高风险审批、跨租户/跨项目限制和 Agent 自动化边界。所有 FastAPI BFF 写接口、异步运行、Agent 工具调用、Qdrant 召回、对象存储访问和外部回写都必须执行本规则。

## 1. 权限模型原则

1. 默认拒绝：未显式授权的模块、动作、数据范围全部拒绝。
2. 租户优先隔离：`tenant_id` 是硬边界；任何用户请求、Agent 工具调用、后台任务、Qdrant 召回和对象存储签名 URL 都必须绑定租户。
3. 项目内最小权限：`project_id` 是业务操作边界；跨项目读取、复用、回填或声纹匹配必须有额外策略。
4. 数据范围与动作权限同时满足：有模块权限但无门店、日期、人员、资产分区或敏感等级权限时仍拒绝。
5. 高风险动作必须审批：发布、批量回填、覆盖人工标注、声纹合并、外部回写、敏感导出、Provider/阈值变更不能只靠普通写权限。
6. Agent 不拥有业务最终决策权：Agent 可以建议、排序、生成候选、创建草稿和人审任务；不能绕过人审直接发布、覆盖、外发或跨项目写入。
7. 所有拒绝都必须返回可解释 `blocked` 状态或 403 错误，并写入审计。

## 2. 角色定义

| 角色 ID | 角色名称 | 定位 | 默认数据范围 |
| --- | --- | --- | --- |
| `platform_admin` | 平台管理员 | 管理平台级租户、系统健康、全局服务接入。默认不读取业务原始数据。 | 平台元数据；业务数据需临时授权。 |
| `tenant_admin` | 租户管理员 | 管理租户项目、成员、配额、审计和租户级策略。 | 当前租户全部项目元数据；原始音频、声纹、导出需显式授权。 |
| `project_admin` | 项目管理员 | 管理项目配置、任务版本、成员权限、发布和审批。 | 当前项目全部业务数据，敏感数据按策略控制。 |
| `data_integration_engineer` | 数据接入工程师 | 配置连接器、数据源、字段映射、任务输入输出和外部回调。 | 当前项目连接器、任务配置、同步日志。 |
| `label_governor` | 标签治理员 | 管理标签体系、候选标签、标签版本、规则、Prompt 和发布门禁。 | 当前项目标签、候选、评测摘要、关联证据引用。 |
| `annotator` | 标注 / 调听员 | 处理调听、边界、标签、声纹候选和普通人审任务。 | 分配门店、日期、人员、任务队列内证据。 |
| `review_arbitrator` | 人审仲裁员 | 处理高风险冲突、覆盖人工标注、发布阻断和审批任务。 | 被授权项目和人审队列。 |
| `model_engineer` | 算法 / 模型工程师 | 管理评测、badcase、模型对比、质量诊断和候选 provider 测试。 | 当前项目评测、badcase、模型指标、脱敏证据。 |
| `asset_manager` | 数据资产管理员 | 管理资产目录、血缘、质量、回填、导出和报告资产。 | 当前项目资产、分区、血缘、质量记录。 |
| `business_operator` | 业务运营人员 | 查看运营首页、洞察、报告，发起低风险处理草稿。 | 授权门店、区域、时间范围内聚合数据和脱敏证据。 |
| `auditor` | 审计员 | 查看权限、审批、审计日志和状态迁移。 | 审计数据；原始内容默认不可见。 |
| `agent_service` | Agent 服务账号 | 由 Agentic Runtime 使用，创建候选、草稿、Trace 和人审任务。 | 运行输入范围内的最小必要上下文。 |
| `external_callback_client` | 外部回调客户端 | 外部系统接收或回传回调回执。 | 绑定的 callback endpoint 和 payload 引用。 |

### 2.1 角色组合限制

- 同一用户可以有多个角色，但高风险审批禁止单人闭环：发起人不能作为唯一审批人。
- `platform_admin` 只有在 break-glass 审批后才能读取租户业务原始数据，且必须限时、限范围、强审计。
- `agent_service` 不允许被授予 `publish`、`approve_high_risk`、`export_sensitive`、`manage_secrets`。
- `external_callback_client` 不能调用普通业务读写接口，只能访问回调回执和签名校验端点。

## 3. 数据范围模型

### 3.1 范围维度

| 维度 | 字段 | 规则 |
| --- | --- | --- |
| 租户 | `tenant_id` | 必填硬边界，不允许用户请求覆盖服务端上下文。 |
| 项目 | `project_id` | 必填业务边界；跨项目需要策略和审批。 |
| 门店 / 区域 | `store_id`、`region_id` | 调听、洞察、回填、导出必须校验。 |
| 人员 / 设备 | `person_id`、`device_id` | 声纹、工牌、设备音频需敏感权限。 |
| 时间 | `date_range`、`partition_key` | 超出授权时间窗拒绝或脱敏聚合。 |
| 资产 | `asset_key`、`asset_domain`、`partition` | 回填、导出、血缘读取必须校验资产权限。 |
| 版本 | `task_version_id`、`label_version_id`、`model_version`、`prompt_version`、`hotword_pack_version_id` | 发布、评测、回滚必须固定版本；候选热词版本禁止生产运行。 |
| 敏感等级 | `data_classification` | `public`、`internal`、`sensitive`、`restricted`。原始音频、声纹、PII 默认 sensitive 或 restricted。 |

### 3.2 数据访问规则

- MySQL 查询必须带 `tenant_id` 和 `project_id` 条件；租户级对象可缺省 `project_id`，但必须声明对象类型。
- Qdrant 查询必须使用 payload filter：`tenant_id`、`project_id`、必要时 `asset_key`、`label_version`、`source_type`。禁止无过滤向量召回。
- Redis 只可保存带版本命名空间的缓存、锁和幂等加速，不能授予其写入标签/Prompt/发布事实的权限；一期不引入 ClickHouse，执行引擎内部标识也不得成为业务授权对象。
- 对象存储签名 URL 必须绑定对象、用户、权限、过期时间和下载水印策略。
- 聚合洞察可以向业务运营开放；下钻到原始音频、证据包、声纹和人工标注必须重新鉴权。
- 导出结果继承源数据最高敏感等级，不因生成报告而降级。

## 4. 动作码

| 动作码 | 含义 |
| --- | --- |
| `R` | 读取列表、详情、聚合和状态。 |
| `ER` | 读取原始证据：音频、ASR 原文、证据包、未脱敏片段。 |
| `C` | 创建草稿、候选、运行请求或人审任务。 |
| `U` | 更新草稿、候选、配置或普通状态。 |
| `X` | 执行异步运行、同步、评测、质量检查、导出准备。 |
| `V` | 复核、标注、仲裁或提交人审结论。 |
| `A` | 审批高风险动作。 |
| `P` | 发布、灰度、启用、回滚生产版本。 |
| `B` | 回填、重算、覆盖或重跑资产分区。 |
| `E` | 导出、外发、生成签名 URL。 |
| `S` | 管理系统配置、Provider、阈值、权限和密钥引用。 |
| `O` | 查看审计、Trace、执行映射和审批记录。 |

## 5. 模块动作矩阵

表中角色为默认允许范围，仍需通过数据范围和审批策略校验。

| 模块 / 对象 | 默认读取 | 创建 / 更新 | 执行 | 复核 / 审批 / 发布 | 关键限制 |
| --- | --- | --- | --- | --- | --- |
| 租户、项目、成员 | `platform_admin`、`tenant_admin`、`project_admin`、`auditor` | `tenant_admin`、项目内 `project_admin` | 无 | 成员授权变更由 `tenant_admin` 或 `project_admin` 审批 | 项目管理员不能授予超出自身范围的权限。 |
| 连接器、数据源、字段映射 | `project_admin`、`data_integration_engineer`、`auditor` | `data_integration_engineer`、`project_admin` | `data_integration_engineer` 可测试连接和同步 | 含密钥、外部写入的配置需 `project_admin` 审批 | 密钥只存 SecretRef，不回显。 |
| TaskVersion、任务画布 | `project_admin`、`data_integration_engineer`、`label_governor`、`model_engineer` | `project_admin`、`data_integration_engineer` | 诊断运行：`project_admin`、`data_integration_engineer` | 发布：`project_admin` + 命中领域负责人审批 | 发布前必须校验门禁、回滚和影响范围。 |
| TaskRun、异步运行 | 项目内相关角色按模块读取 | `project_admin`、`data_integration_engineer`、`asset_manager` 可创建对应运行 | `X` 由运行系统执行 | 取消高风险运行需发起人或管理员 | 运行输入范围不能超过用户授权范围。 |
| AudioSession、EvidencePack | `annotator`、`review_arbitrator`、`business_operator`、`model_engineer` | 证据包：`annotator`、`review_arbitrator`、`model_engineer` | 无 | 证据定稿由 `review_arbitrator` | 原始音频读取需要 `ER` 和敏感数据授权。 |
| ConversationBoundary | `annotator`、`review_arbitrator`、`model_engineer` | `annotator` 可编辑草稿；Agent 可写候选 | 下游重建由 TaskRun / Backfill 执行 | 高风险边界确认：`review_arbitrator` | 影响已发布标签或评测集时必须人审。 |
| LabelCandidate、SegmentAnnotation | `annotator`、`label_governor`、`review_arbitrator`、`model_engineer` | `annotator`、`label_governor`、`agent_service` | 批量接受低风险候选需 L2 策略 | 高风险候选由 `review_arbitrator` 仲裁 | Agent 只能写候选，不覆盖人工标注。 |
| LabelVersion、ReleaseGate | `label_governor`、`project_admin`、`model_engineer`、`auditor` | `label_governor` | 评测由 `label_governor`、`model_engineer` 发起 | 发布：`label_governor` + `project_admin`；高风险需仲裁 | 未通过门禁时禁止发布。 |
| LabelObservation、AggregationPolicy/Run、LabelAggregate/Fact | 当前项目授权成员按数据范围读取 | Observation/抽取：`project_admin`、`model_engineer`；策略/聚合：`project_admin`、`model_engineer`、`review_arbitrator` | 抽取与聚合由受信运行/worker 执行；L2 仅安全门禁内自动创建 Fact | 高风险、冲突、新颖、缺证据和 Taxonomy：`review_arbitrator`/自然人审批 | Observation 不可变；Fact 追加/supersede；Redis/Qdrant 非事实源。 |
| PromptAsset、PromptVersion、OptimizationTriggerScan | 当前项目授权成员按数据范围读取 | Prompt 资产/版本和扫描：`project_admin`、`model_engineer` | 候选生成和离线评测可自动执行 | Prompt 候选批准仍需自然人，Agent/系统不得发布 | 必须锁定标签/Schema/模型/聚合策略/评测集和预算。 |
| ReleaseDeployment | `project_admin`、`model_engineer`、`auditor` | Bundle 创建：`project_admin`、`model_engineer` | shadow/监控由系统执行 | `approve-gray/promote/rollback`：`project_admin`；系统只能按已授权硬阈值自动回滚 | `blocked` 禁止推进；Bundle 与 rollback target 必须锁定。 |
| VoiceprintEnrollment | `annotator`、`review_arbitrator`、`model_engineer` 按敏感授权读取 | 候选：`annotator`、`agent_service`；策略内更新：`review_arbitrator` | 质量检查由系统执行 | 入库、合并、拆分：`review_arbitrator` + `project_admin` | 客户声纹和跨项目匹配默认 restricted。 |
| EvalRun、EvalDataset | `model_engineer`、`label_governor`、`project_admin` | `model_engineer`、`label_governor` | `model_engineer`、`label_governor` | 评测结果发布或门禁使用需 `project_admin` 或领域负责人 | 评测集锁定后不得静默换样本。 |
| ASR 热词包与版本 | `model_engineer`、`project_admin`、`annotator`（只读已发布词项）、`auditor` | 逻辑包/候选版本/词项：`model_engineer`；`agent_service` 只可生成候选草稿 | 分析、provider 构建与影子复测：`model_engineer`；调听纠错建 Badcase：`annotator` | 模型批准：`model_engineer`；人工发布：不同自然人的 `project_admin`；回滚发起：`model_engineer`，回滚审批：不同自然人的 `project_admin`；完成物化：受信 worker | `resource_version` 乐观锁；候选版本仅 shadow；客户端不得写编译产物、评测指标、发布或回滚完成态。 |
| Badcase | `model_engineer`、`label_governor`、`annotator`、`review_arbitrator` | `model_engineer`、`annotator`、`agent_service` | 回归评测由 `model_engineer` | ASR 热词确认由 `annotator`/`model_engineer`；高风险豁免需 `review_arbitrator` + `project_admin` | 发布阻断 badcase 不得无审批归档；人工决策必须绑定词级证据和乐观锁版本。 |
| InsightReport | `business_operator`、`project_admin`、`asset_manager` | `business_operator`、`asset_manager`、`agent_service` 可建草稿 | 生成报告：`business_operator`、`asset_manager` | 外发或含敏感证据需 `project_admin` 审批 | 报告必须可下钻证据并完成脱敏。 |
| DataAsset、AssetLineage | `asset_manager`、`project_admin`、`model_engineer`、`business_operator` | `asset_manager` | 质量检查、重生成：`asset_manager` | 高风险资产变更需 `project_admin` | 血缘写入以 MySQL 为准。 |
| BackfillRequest | `asset_manager`、`project_admin`、`model_engineer` | 草稿：`asset_manager`、`agent_service`；更新：`asset_manager` | `asset_manager` 执行已批准回填 | 批量、覆盖、下游重算：`project_admin` + `asset_manager` 审批 | 必须先有影响预估和回滚方案。 |
| ExternalCallback | `data_integration_engineer`、`asset_manager`、`project_admin` | `data_integration_engineer` 配置；系统创建发送记录 | 系统执行，人工可重放 | endpoint 启用、重放高风险回调需 `project_admin` | payload 必须脱敏、签名、幂等。 |
| Settings、PolicyGuard、ToolRegistry | `project_admin`、`tenant_admin`、`auditor` | `project_admin`、`tenant_admin` 按层级 | Provider 测试：`project_admin`、`model_engineer` | 阈值、Provider、权限策略发布需审批 | 业务页面不暴露密钥和服务端点。 |
| AuditLog、TraceRef | `auditor`、`tenant_admin`、`project_admin` | 系统写入 | 无 | 无 | 审计日志不可被普通业务角色删除或修改。 |

### 5.1 标签闭环接口当前强制角色

下表是当前 FastAPI 路由的可执行基线；上表中的领域角色仍可用于产品权限设计，但在完成角色映射前不得假设 `label_governor` 自动拥有下列写权限。

| 接口动作 | 当前允许角色 | 额外约束 |
| --- | --- | --- |
| 创建 `label-extraction-runs`、写入 `label-observations` | `project_admin`、`model_engineer` | 受信完成回执才可物化；必须租户/项目隔离和幂等。 |
| 创建 `label-aggregation-policies/runs` | `project_admin`、`model_engineer`、`review_arbitrator` | 系统身份不得批准 active 策略；策略/标签版本/mode 必须一致。 |
| 创建 `prompt-assets/prompt-versions`、`label-optimization-trigger-scans` | `project_admin`、`model_engineer` | 扫描按标签版本单活、trigger hash 去重和预算门禁。 |
| 创建/更新、读取 `label-optimization-schedules` 及其 snapshots/rounds | `project_admin`、`model_engineer` | 写入锁定 Bundle 与预算并要求幂等；scheduler 使用受限系统身份，DB scope mutex 保证首次并发单活。任何自动轮次最多生成候选和锁定 EvalRun，只能终止于 `blocked/awaiting-review`，不得自动发布。 |
| Prompt 候选密封 review submission | `project_admin`、`review_arbitrator`、`annotator` | 同一候选同一自然人一次，提交内容密封；两份不一致必须仲裁。 |
| Prompt 候选 adjudication | 仅 `review_arbitrator` | 仲裁员不能是任一盲审审核人；候选必须已有两份完整密封提交。 |
| 高风险 Aggregate / Taxonomy 密封 review submission | `project_admin`、`review_arbitrator`、`annotator` | 仅自然人；一任务一目标，同一目标同一审核人一次；终态前密封 decision/value/taxonomy action。 |
| 高风险 Aggregate / Taxonomy adjudication | `project_admin`、`review_arbitrator` | 仅自然人；仲裁人不得是两名盲审人之一，且必须已有两份完整不一致提交。 |
| 创建 `release-deployments` | `project_admin`、`model_engineer` | 仅创建 shadowing/blocked Bundle，不代表发布。 |
| `release-deployments/{id}/transitions` | `project_admin` | 系统身份不得 `approve-gray/promote`；expected status 不一致返回 409。 |
| `release-deployments/{id}/monitor-samples` | 仅 `system` | 类型化指标、sample ID/hash、expected status 和 `stable_window_complete` 幂等；稳定窗口事实写入权威 monitor metrics，只允许 monitoring、自动回滚或 safe-stop blocked，不得自动 promote。 |
| `human-review-decision-batches` | `project_admin`、`review_arbitrator`、`annotator` | 仅显式 Aggregate 目标、低风险、同标签/策略 cohort，逐项回执。 |
| 创建 `badcases(capability=labeling|prompt-optimization)` | `project_admin`、`model_engineer`、`annotator` | 继承 Badcase 写角色和项目数据范围；结构化来源/失败原因必填。 |

## 6. 高风险动作审批

| 高风险动作 | 触发条件 | 最低审批 | 系统阻断点 | 审计要求 |
| --- | --- | --- | --- | --- |
| 发布 TaskVersion | 生产发布、启用调度、替换 provider、修改输出回写 | `project_admin`；涉及连接器/外部写入时加 `data_integration_engineer` | `TaskVersion.review_required` 或 `validation_failed` | 版本 diff、门禁、回滚目标、影响任务。 |
| 发布 LabelVersion | 生产发布、灰度提升、规则/Prompt 生效 | `label_governor` + `project_admin` | `LabelVersion.gate_blocked` | ChangeSet、EvalRun、Human Loop、自动化等级。 |
| 覆盖人工标注 | 候选结果覆盖或修改人工确认结果 | `review_arbitrator` + `project_admin` | `HumanReviewTask.escalated` | 修改前后、证据、理由、操作者。 |
| 批量接受 LabelCandidate | L2 以上自动化、批量影响标签资产 | `label_governor`；高风险加 `review_arbitrator` | `LabelCandidate.pending_review` | 候选列表、阈值、抽检样本、回滚方式。 |
| 声纹入库 / 合并 / 拆分 | 员工或客户声纹最终身份变化 | `review_arbitrator` + `project_admin`；跨项目加 `tenant_admin` | `VoiceprintEnrollment.pending_review` | embedding 引用、质量分、源/目标身份、隐私策略。 |
| 批量回填 / 覆盖回填 | 覆盖既有资产、重算下游、影响人工确认 | `asset_manager` + `project_admin` | `BackfillRequest.pending_approval` | 影响预估、分区、旧/新资产版本、失败分区。 |
| 外部回写 / 重放回调 | 标签结果、复核结论、WAV URL、报告外发 | `project_admin`；endpoint 配置加 `data_integration_engineer` | `ExternalCallback.blocked` | endpoint、payload hash、签名 key ref、响应摘要。 |
| 修改高风险阈值 | 低置信、人审、声纹、回填审批、发布门禁阈值 | `project_admin` + 领域负责人 | `Settings.review_required` | 阈值前后、影响范围、生效时间、回滚。 |
| 替换 Audio Intelligence provider | 生产 provider、降级策略或服务版本变化 | `project_admin` + `model_engineer` | `TaskVersion.review_required` 或 `Settings.review_required` | provider、版本、测试结果、成本/延迟/质量影响。 |
| 发布 ASR 热词包版本 | 让候选版本成为生产 TaskVersion 可选输入 | `model_engineer` + 不同自然人的 `project_admin` | `HotwordPackVersion.review_required/gate_blocked` | 锁定 EvalRun、门禁指标、provider 编译产物、词项 diff、审批人、TaskVersion 草稿 ID。 |
| 回滚 ASR 热词包版本 | 将逻辑包当前指针恢复到历史已发布版本 | `model_engineer` 发起 + 不同自然人的 `project_admin` 审批 | `hotword_rollback` RunRecord 首次 Outbox 投递 `blocked`，冻结绑定漂移则 fail closed | 前后版本及三者冻结版本号/根 Trace、原因、审批人、未切 TaskVersion/未覆盖资产声明。 |
| 标记发布阻断或豁免阻断 | EvalRun、Badcase、ReleaseGate 影响发布判断 | 阻断：领域负责人；豁免：`review_arbitrator` + `project_admin` | `LabelVersion.gate_blocked` | 指标、badcase、豁免理由、有效期。 |
| Prompt/聚合策略/Taxonomy 变更 | 改变抽取 Schema、聚合阈值、canonical 标签或生产 Prompt | `model_engineer`/`review_arbitrator` 提议 + `project_admin` 批准 | 候选保持 `draft/pending/awaiting-review` | 父版本、结构化 diff、来源 badcase、锁定评测与 Trace。 |
| ReleaseDeployment 灰度晋级 | shadow 进入 10% gray 或 gray 进入生产完成态 | 不同于候选生成服务身份的自然人 `project_admin` | `ReleaseDeployment.blocked/shadowing/monitoring` | 锁定 Bundle、在线门禁、稳定窗口、回滚目标、批准人。 |
| 敏感导出 | 原始音频、声纹、PII、证据包、外发报告 | `project_admin`；跨项目/租户加 `tenant_admin` | `ExportJob.blocked` 或 `InsightReport.review_required` | 接收人、水印、脱敏、对象 key、下载记录。 |

### 6.1 审批通用规则

- 自审禁止：动作发起人、候选创建人、Agent 服务账号不能作为唯一审批人。
- 双人审批：影响生产发布、批量覆盖、声纹身份、敏感导出时至少两个不同自然人确认。
- 审批有效期：影响预估、评测结果和候选版本超过策略 TTL 后，审批自动失效。
- 审批绑定版本：审批只对指定对象版本、输入范围、影响范围有效。

## 7. 跨租户 / 跨项目限制

### 7.1 跨租户

- 默认禁止任何跨租户业务数据读取、召回、导出、回填、声纹匹配和外部回写。
- 平台级运维只能读取健康状态、资源用量、错误码和脱敏聚合；读取业务原始数据必须走 break-glass。
- Qdrant collection 即使物理共用，也必须用 `tenant_id` payload filter；缺少 filter 的查询直接拒绝。
- 外部回调 endpoint 必须归属于单一租户，不能把一个租户的 payload 投递到另一个租户绑定。

### 7.2 跨项目

- 默认禁止跨项目写入。
- 跨项目读取只允许在同一租户内，经项目策略启用，并限定为脱敏聚合、知识召回或候选匹配。
- 跨项目声纹匹配只能产生 `VoiceprintEnrollment.candidate` 或相似候选，不得自动合并身份。
- 标签版本、任务版本、评测集、资产分区不能直接跨项目发布；可复制为新项目草稿并重新校验。
- 跨项目回填必须拆成多个项目内 BackfillRequest，不允许一个回填事务同时写多个项目权威状态。

### 7.3 数据外发

- 外发只允许使用已验证 endpoint、签名 payload、幂等键和脱敏策略。
- 对象存储 URL 必须短期有效，绑定接收方、用途、下载次数或过期时间。
- 外发失败不得重试到其他 endpoint，除非审批策略明确允许。

## 8. Agent 自动化边界

### 8.1 自动化等级

| 等级 | Agent 可做 | 必须人工处理 | 后端执行要求 |
| --- | --- | --- | --- |
| L0 只读建议 | 读取授权上下文，展示建议，不写候选。 | 所有写入。 | 不创建业务候选，只写 Trace。 |
| L1 写候选 | 创建 LabelCandidate、BoundaryCandidate、BackfillDraft、EvalRunDraft、HumanReviewTask。 | 每条候选接受、修改、拒绝。 | 候选状态为 `generated` 或 `pending_review`。 |
| L2 低风险自动接受 | 仅低风险、校准稳定、证据完整、来源独立、无冲突/新颖性且达到 score/margin 门禁的 Aggregate 可自动创建 LabelFact，并按 5% 分层抽检。 | 高风险、未知标签、缺证据、冲突、分布外、覆盖人工、Prompt/Taxonomy/聚合策略、灰度和发布。 | Policy Guard 逐条校验并输出理由；校准样本不足时退回 L1。 |
| L3 门禁后灰度 | 本阶段禁用自动执行；系统只能生成灰度建议。 | 自然人批准固定 10% gray。 | ReleaseDeployment 保持 `shadowing/awaiting-review`，不得由系统代签。 |
| L4 稳定自动发布 | 本阶段禁用。 | 生产晋级、Prompt/标签发布与回滚授权。 | 仅保留接口和审计扩展点，不得配置绕过自然人。 |

### 8.2 Agent 服务账号允许动作

`agent_service` 允许：

- 在输入范围内读取最小必要上下文：音频片段引用、ASR、speaker_turns、标签版本、评测摘要、badcase 摘要、资产状态。
- 调用 Tool Registry 中授权的 `read_only`、`candidate_write`、`draft_write` 工具。
- 创建 LabelCandidate、BoundaryCandidate、SpeakerCandidate、EventLinkCandidate、PromptSuggestion、InsightFact。
- 创建 ASR 热词 Badcase 候选、易错词候选和 HotwordPackVersion 草稿；不得把知识实体或模型建议直接写成已确认词项。
- 创建 HumanReviewTask、EvalRunDraft、BackfillDraft、DagsterRunDraft。
- 写 AgentRun、ToolCall、TraceRef、Decision、建议理由和置信度。

`agent_service` 禁止：

- 直接发布 TaskVersion、LabelVersion、PromptVersion 或配置版本。
- 直接批准、发布或回滚 HotwordPackVersion，或把候选热词版本用于 `execution_mode=production`。
- 直接覆盖人工标注、已确认边界、声纹最终身份、生产资产或外部系统。
- 直接批量回填、批量接受高风险候选、修改高风险阈值。
- 读取或写入超出输入范围的租户、项目、门店、日期或资产分区。
- 读取明文密钥、外部 token、未授权原始音频或原始 voiceprint embedding。
- 伪造自然人审批或绕过 Human Review Gateway。

### 8.3 Tool Registry 副作用等级

| 工具等级 | 例子 | Agent 调用策略 |
| --- | --- | --- |
| `read_only` | 查询标签版本、召回相似 badcase、读取资产状态 | L0 起允许，必须带数据范围 filter。 |
| `candidate_write` | 创建标签候选、边界候选、badcase 候选 | L1 起允许。 |
| `draft_write` | 创建回填草稿、评测草稿、DagsterRunDraft | L1 起允许，高风险自动进入人审。 |
| `runtime_execute` | 低风险重跑、质量检查、影子评测 | L2 起按项目策略允许。 |
| `external_side_effect` | 外部回写、导出、发送报告 | Agent 禁止直接执行，只能创建待审批草稿。 |
| `high_risk_mutation` | 发布、覆盖人工、声纹合并、批量回填 | Agent 禁止直接执行。 |

### 8.4 Policy Guard 检查顺序

每次用户请求、Agent 工具调用、异步任务和外部回调重放必须按顺序检查：

1. 身份认证：用户、服务账号或外部客户端是否有效。
2. 租户上下文：`tenant_id` 是否来自服务端会话或签名 token。
3. 项目范围：`project_id` 是否在授权范围内。
4. 模块动作：角色是否拥有动作码。
5. 数据范围：门店、日期、人员、设备、资产分区、敏感等级是否允许。
6. 对象状态：目标对象当前状态是否允许该动作。
7. 自动化等级：Agent 或批量操作是否符合 L0-L4 策略。
8. 审批策略：是否需要 HumanReviewTask 或双人审批。
9. 幂等与并发：是否有 idempotency_key、对象版本和运行锁。
10. 审计与 Trace：是否能写入审计；不能审计则拒绝高风险动作。

## 9. 后端落库建议

### 9.1 权限表

第一阶段建议使用 RBAC + 数据范围表，后续可扩展 ABAC。

| 表 | 用途 |
| --- | --- |
| `roles` | 角色定义。 |
| `permissions` | 模块动作码定义。 |
| `role_permissions` | 角色到动作授权。 |
| `memberships` | 用户在租户、项目中的角色。 |
| `data_scopes` | 门店、区域、日期、人员、资产域、敏感等级范围。 |
| `approval_policies` | 高风险动作审批策略。 |
| `approval_tasks` | 审批实例，可与 HumanReviewTask 关联。 |
| `service_accounts` | Agent、异步 worker、外部客户端身份。 |
| `audit_logs` | 不可变审计。 |

### 9.2 审计最小字段

所有高风险动作和权限拒绝必须记录：

- `audit_id`
- `tenant_id`
- `project_id`
- `actor_type`
- `actor_id`
- `role_ids`
- `action`
- `object_type`
- `object_id`
- `from_status`
- `to_status`
- `decision`
- `reason_code`
- `data_scope`
- `risk_level`
- `approval_task_id`
- `trace_id`
- `ip`
- `user_agent`
- `created_at`

### 9.3 API 行为

- 403 表示权限不足；409 表示对象状态或版本冲突；422 表示输入不合法；423 表示对象被运行或回填锁定。
- 异步动作被审批、依赖或门禁阻断时，返回 202 + 运行对象 `blocked` 更适合前端展示。
- 所有写操作响应必须包含 `status`、`trace_id`、`affected_objects`、`next_actions`。
- 列表接口必须默认按当前用户数据范围过滤，不允许前端传 `tenant_id` 绕过服务端上下文。
