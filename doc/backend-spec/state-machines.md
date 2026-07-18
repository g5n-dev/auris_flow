# Auris Flow 后端状态机规格

本文档定义 Auris Flow 后端第一阶段必须落库并执行的核心对象状态机。它承接当前原型中的任务配置、Agent-Human-Dagster 闭环、调听证据、标签治理、评测、badcase、洞察报告、资产回填和外部回写流程。

## 1. 通用约束

### 1.1 状态字段

所有状态机对象必须至少包含：

| 字段 | 要求 |
| --- | --- |
| `tenant_id` | 必填。租户隔离边界，所有查询、写入、审计和事件都必须携带。 |
| `project_id` | 必填。项目隔离边界，除租户级配置外不得为空。 |
| `status` | 当前权威状态，只能通过受控动作迁移。 |
| `status_reason` | 机器可解析原因码，例如 `gate_failed`、`permission_denied`、`human_required`。 |
| `status_detail` | 面向前端和审计的说明，不能包含密钥、token 或未脱敏个人敏感信息。 |
| `version` | 乐观锁版本号，所有写操作必须校验。 |
| `trace_id` | OpenTelemetry 链路 ID，用于跨 TaskRun、AgentRun、人审、回填和外部回调追踪。 |
| `created_by` / `updated_by` | 用户 ID 或服务账号 ID。Agent 写入必须标记为 Agent 服务账号并关联 `agent_run_id`。 |
| `created_at` / `updated_at` | 服务端时间。 |

高风险对象还必须包含：

- `risk_level`：`low`、`medium`、`high`。
- `approval_policy_id`：命中的审批策略。
- `approved_by`、`approved_at`：审批人和时间。
- `idempotency_key`：创建运行、回填、外部回调、发布动作必须具备。
- `source_refs`、`output_refs`：输入和输出业务对象引用。

### 1.2 迁移执行规则

1. 状态迁移必须在 MySQL 事务内完成，并使用行级锁或乐观锁防止并发覆盖。
2. 每个写动作必须先通过 Policy Guard：认证、租户、项目、角色、数据范围、自动化等级、审批策略、幂等键。
3. Agent 输出不能直接覆盖线上结果，只能写候选、草稿、人审任务、评测草稿、回填草稿、洞察事实或 Trace。
4. `published`、`success`、`approved` 等结果态对象不得原地改写业务含义；修正必须创建新版本、新候选或回滚记录。
5. 终态对象允许补充审计元数据，但不得重开业务状态，除非本状态机显式定义 `reopen` 或 `supersede`。
6. 所有迁移必须写 `AuditLog`，并按需发出领域事件，例如 `task_version.published`、`backfill.blocked`。
7. 异步执行类对象必须兼容统一前端状态：`pending`、`running`、`success`、`failed`、`blocked`、`cancelled`。
8. 标签闭环不引入 ClickHouse。MySQL 保存权威状态和预计算结果，Redis 只缓存，Qdrant 只召回；底层执行引擎映射不得作为产品状态或界面语言。

### 1.3 通用迁移事件

后端服务内部建议统一使用以下事件包：

```json
{
  "event_id": "evt_xxx",
  "object_type": "TaskRun",
  "object_id": "tr_xxx",
  "tenant_id": "auris",
  "project_id": "sales_quality",
  "from_status": "running",
  "to_status": "waiting_human",
  "action": "request_human_review",
  "actor_type": "agent",
  "actor_id": "agent_service",
  "reason_code": "low_confidence",
  "trace_id": "trace_xxx",
  "idempotency_key": "tenant|project|object|action|hash",
  "occurred_at": "2026-07-06T10:00:00Z"
}
```

### 1.4 标签闭环运行阶段与 UI 事实语义

标签抽取、聚合、优化和发布共享业务阶段：

`queued → running → materializing → awaiting-review/evaluating → shadowing → gray-releasing → monitoring → completed/blocked/rolled-back`

- HTTP `202`、`queued`、`running`、`materializing` 均为进行态；页面必须轮询，不得显示成功。
- `blocked` 是可解释阻断态，必须持久化 `blocked_reasons[]` 与 `next_action`；修复后创建新动作或按显式状态机继续，不能在前端本地改成成功。
- `completed` 只表示该运行已完成。标签/Prompt 发布必须分别以权威版本的 `published` 或完成 promote 的 ReleaseDeployment 为准。
- ToolCall 计划、Outbox `processed`、协议级提交或执行引擎受理都不是业务完成；只有强类型结果物化、Trace/审计/Outbox 同事务完成后才能迁移。
- MySQL 是状态和事实源；Redis/Qdrant 只能缓存、去重或召回，不能推动最终状态。
- 发布动作增加两阶段传输态：创建 Bundle 或 transition 先进入 `pending/materializing` 并冻结 ReleaseCommand；只有受信 ACK 绑定命令/Bundle 且 active head generation CAS 成功，才进入 `shadowing/gray-releasing/completed/rolled-back`。UI 不得把 201/202、dispatch 或 pending command 当成发布成功。

### 1.5 标签自动优化 Schedule / Round

`OptimizationSchedule` 状态为 `active | paused`。`active` 计划同时维护三条持久化时钟：15 分钟阈值扫描、项目时区每日 02:00 增量、每周日 02:00 全量。独立进程和 Dagster schedule 只能调用同一个 `label_optimization_worker.run_once`；worker 通过 `scan_claim_token` 的数据库条件 UPDATE 获取 scope mutex，时钟推进、RunRecord、Round、审计、Outbox 和 claim 释放同事务提交。未到期的 blocked 探测不能更新 `last_threshold_scanned_at`，避免把 15 分钟窗口无限推迟。

`OptimizationRound` 状态机：

`generating-candidates → evaluating → completed → generating-candidates(next)`

或：

`generating-candidates/evaluating → blocked | awaiting-review`

- 每个 session 最多三轮，每轮必须 2–5 个真实 PromptVersion 候选，墙钟最多两小时；配置成本上限时缺失可信成本指标即 fail closed。
- reconcile 以 session 最早 Round 的 `started_at` 计算真实墙钟，不只检查已完成回执；达到两小时后，仍 queued/submitted/running 的 generation 和 EvalRun 一律 `blocked(time_budget_exceeded)`，当前 Round/root run 同步 blocked、释放 `active_run_id`，禁止卡住任务长期占用单活范围。
- 候选物化后，scheduler 必须为每个候选创建绑定 dataset/label/prompt/model/policy/optimization run 的锁定 EvalRun；不能用静态 ToolCall 计划代替评测。
- Eval 终态后调用同一 `evaluate_iteration_budget`：关键 recall 回退、成本/时间超限、无显著收益、最大轮次或连续失败转 `blocked`；仍可修复的非关键门禁进入下一轮；有通过候选则转 `awaiting-review`。
- `awaiting-review` 是自动化终点。系统不得创建 ReleaseDeployment、approve gray、promote 或发布 Prompt；后续仍需自然人密封审核和发布批准。
- MetricSnapshot 是 append-only 触发证据。失败原因必须是规范 `reason_code`；自由文本只进入拒绝记录 hash，非法 JSON 必须以 `invalid_json_output` 计入拒绝数和 JSON 合法率。

## 2. TaskVersion

任务版本是任务类型画布、节点配置、调度、A/B、模型服务绑定、输出资产和外部回写配置的可发布版本。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 草稿，可编辑，不影响生产运行。 |
| `validating` | 正在执行兼容性校验、权限校验和执行映射校验。 |
| `validation_failed` | 校验失败，需回到草稿修复。 |
| `ready_for_review` | 校验通过，等待提交审批或发布。 |
| `review_required` | 命中高风险策略，必须进入人审或管理员审批。 |
| `approved` | 审批通过，可发布。 |
| `published` | 当前生效版本。每个项目下同一任务类型同一生产轨道只能有一个。 |
| `paused` | 已暂停调度，但可保留手动诊断权限。 |
| `deprecated` | 被新版本替代，不再创建新运行。 |
| `archived` | 归档，只读保留。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_draft` | 无 | `draft` |
| `edit_draft` | `draft`、`validation_failed` | 原状态不变 |
| `submit_validation` | `draft`、`validation_failed` | `validating` |
| `validation_passed` | `validating` | `ready_for_review` |
| `validation_failed` | `validating` | `validation_failed` |
| `mark_review_required` | `validating`、`ready_for_review` | `review_required` |
| `approve` | `review_required`、`ready_for_review` | `approved` |
| `reject` | `review_required` | `draft` |
| `publish` | `approved`、低风险 `ready_for_review` | `published` |
| `pause` | `published` | `paused` |
| `resume` | `paused` | `published` |
| `deprecate` | `published`、`paused` | `deprecated` |
| `archive` | `deprecated` | `archived` |
| `clone_new_draft` | `published`、`deprecated`、`archived` | 新对象 `draft` |

### 迁移规则

- 发布新 `TaskVersion` 时，同一 `tenant_id + project_id + task_type_id + release_channel` 下旧 `published` 版本必须在同一事务内迁移为 `deprecated`，并记录 `replaced_by_task_version_id`。
- 影子评测和 A/B 实验不得覆盖生产版本，只能通过 `run_tags`、`experiment_id` 或 `release_channel` 引用候选版本。
- `paused` 版本不得创建定时运行，但项目管理员可触发一次性诊断运行。
- 回滚不是修改旧版本，而是把历史版本克隆为新草稿或重新发布已校验版本，并保留 `rollback_from_version_id`。

### 阻断条件

- 缺少输入节点、输出资产、模型服务版本、回写配置或 Dagster 执行映射。
- 模型 provider、工具、密钥引用、存储桶或外部回调 endpoint 未启用或无权限。
- A/B 分流比例非法，调度策略缺少时间窗口，或分区键无法生成。
- 存在 `blocker` 级校验项、未完成 HumanReviewTask、发布门禁失败。
- 发布会覆盖人工标注、触发批量回填、替换 Audio Intelligence provider 或修改高风险阈值但没有审批。
- 同一版本仍有 `running`、`waiting_human` 或 `waiting_callback` 的生产 TaskRun，且发布策略要求串行。

### 审计要求

- 记录版本 diff：节点、连线、模型、阈值、调度、输出资产、回写配置、Dagster 映射。
- 记录校验结果：`blockers`、`warnings`、`passed`。
- 发布审计必须包含审批人、审批策略、回滚目标、影响范围、旧版本和新版本。
- Agent 生成的节点建议只能记录为 `agent_suggestion` 或 `changeset_draft`，不能直接进入 `published`。

## 3. TaskRun

TaskRun 是一次任务版本或草稿的执行记录，可映射到底层 Dagster run，但业务 API 使用 TaskRun 语义。

### 状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 运行请求已创建，等待排队。 |
| `queued` | 已进入执行队列或 Dagster RunRequest 已生成。 |
| `running` | 至少一个节点执行中。 |
| `waiting_human` | 等待人工复核、人工标注或仲裁。 |
| `waiting_callback` | 已完成内部处理，等待外部回写确认或异步回执。 |
| `retrying` | 正在按策略重试失败节点或回写。 |
| `success` | 全部必需节点成功，输出已提交。 |
| `partial_success` | 允许部分成功，失败分区或非关键输出已记录。 |
| `failed` | 执行失败，可按策略重试。 |
| `blocked` | 被权限、门禁、依赖、审批、配额或数据锁阻断。 |
| `cancelling` | 已收到取消请求，等待执行引擎确认。 |
| `cancelled` | 已取消。 |
| `expired` | 等待人审、回调或资源超过 TTL。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_run` | 无 | `pending` |
| `enqueue` | `pending` | `queued` |
| `start` | `queued`、`retrying` | `running` |
| `request_human_review` | `running` | `waiting_human` |
| `human_review_completed` | `waiting_human` | `running`、`blocked` |
| `wait_callback` | `running` | `waiting_callback` |
| `callback_succeeded` | `waiting_callback` | `success`、`partial_success` |
| `node_failed_retryable` | `running`、`waiting_callback` | `retrying` |
| `node_failed_terminal` | `running`、`retrying` | `failed` |
| `complete` | `running` | `success`、`partial_success` |
| `block` | `pending`、`queued`、`running`、`waiting_human` | `blocked` |
| `cancel_request` | `pending`、`queued`、`running`、`waiting_human`、`waiting_callback` | `cancelling` |
| `cancel_confirmed` | `cancelling` | `cancelled` |
| `expire` | `waiting_human`、`waiting_callback`、`queued` | `expired` |

### 迁移规则

- `TaskRun` 创建必须绑定 `task_version_id`、输入范围、分区键、执行模式、幂等键和 `trace_id`。
- 草稿运行只能用于手动诊断或影子评测，不能写生产输出资产。
- 进入 `waiting_human` 时必须创建或关联 `HumanReviewTask`。
- 进入 `waiting_callback` 时必须创建 `ExternalCallback` 记录，并绑定回写 payload hash。
- `partial_success` 必须列出失败分区、可重跑节点和下游影响。

### 阻断条件

- `TaskVersion` 不是 `published`、`approved` 或被允许诊断运行的 `draft`。
- 输入范围跨租户、跨项目或用户无门店、日期、资产分区权限。
- 资产分区被其他回填、发布或运行锁定。
- 依赖的模型服务、工具、存储、连接器或 Qdrant 索引不可用。
- 运行超过项目并发、成本、配额或限流策略。
- 缺少幂等键，或同幂等键已有非终态运行。

### 审计要求

- 记录运行输入、任务版本、执行映射、Dagster run id、run tags、分区、调度来源。
- 记录每个节点的开始、结束、状态、错误、重试次数、输出资产引用。
- 人审、回写、回填、评测、badcase 关联必须可从 `trace_id` 串起来。
- 错误日志必须脱敏，不得记录外部平台 token、密钥或原始 PII。

## 4. HumanReviewTask

HumanReviewTask 是高风险候选、低置信、冲突仲裁、发布门禁、回填确认和外部回写确认的人审对象。

### 状态

| 状态 | 含义 |
| --- | --- |
| `open` | 待分配。 |
| `assigned` | 已分配给人审人员或队列。 |
| `in_progress` | 处理中。 |
| `changes_requested` | 要求上游补充证据、修正候选或重新评测。 |
| `approved` | 同意执行建议动作。 |
| `rejected` | 拒绝建议动作。 |
| `modified` | 人工修改后接受。 |
| `escalated` | 升级到更高权限或双人审批。 |
| `cancelled` | 来源对象取消或失效。 |
| `expired` | 超过 SLA，需重新创建或升级。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create` | 无 | `open` |
| `assign` | `open` | `assigned` |
| `claim` | `open`、`assigned` | `in_progress` |
| `request_changes` | `in_progress` | `changes_requested` |
| `resubmit` | `changes_requested` | `open`、`assigned` |
| `approve` | `in_progress` | `approved` |
| `reject` | `in_progress` | `rejected` |
| `modify_and_approve` | `in_progress` | `modified` |
| `escalate` | `open`、`assigned`、`in_progress` | `escalated` |
| `cancel` | `open`、`assigned`、`in_progress`、`changes_requested` | `cancelled` |
| `expire` | `open`、`assigned`、`changes_requested` | `expired` |

### 迁移规则

- 每个 LabelAggregate、PromptVersionCandidate 或 TaxonomySuggestion 必须独立绑定一个候选级任务，并通过 `target_refs[]` 显式声明目标；决策不得作用于模块级共享当前项。
- 人审任务必须绑定来源对象：`source_type`、`source_id`、`source_status`、`source_version`。
- 审批动作必须校验来源对象仍处于可处理状态，避免审批过期候选。
- `modified` 必须写出结构化修改内容，不能只写文本备注。
- 高风险审批禁止自审：候选创建人、Agent 触发人、回填发起人不能作为唯一审批人。
- 单任务终态恰好一次。重复幂等请求返回原结果；并发不同决策返回 409，不得覆盖终态。
- 批量决策只允许同 `label_id + risk_level=low + policy_version_id` cohort，服务端逐项返回 `success/skipped/failed`；任一条失败不回滚已成功条目，也不能扩大目标范围。
- 接受、修改、拒绝分别生成 `human-confirmed`、`human-modified`、`rejected-badcase` FeedbackExample。普通接受仍是 Gold candidate；只有双评一致或仲裁完成后才能锁定为 Gold。

### 阻断条件

- 缺少证据引用、Trace、影响范围或建议动作。
- 人审人员没有对应租户、项目、门店、数据敏感等级或模块动作权限。
- 来源对象已 `superseded`、`cancelled`、`published`、`rolled_back` 或版本号不一致。
- 需要双人审批但只有单人决策。
- 审批结果会跨项目写入或覆盖人工标注，但审批策略未允许。

### 审计要求

- 记录人审类型、优先级、队列、分配人、处理人、处理时长、SLA。
- 记录修改前后、拒绝原因、审批理由、证据引用、相关 Agent Decision。
- 任何 `approved`、`modified`、`rejected` 都必须产生不可变审计记录。

## 5. ConversationBoundary

ConversationBoundary 表示完整对话边界，来源于 VAD、ASR、Diarization、声纹、单据事件、串音判断和人工调整。

### 状态

| 状态 | 含义 |
| --- | --- |
| `candidate` | 系统或 Agent 生成候选边界。 |
| `draft` | 人工或系统正在编辑，未提交。 |
| `pending_review` | 需要人审确认。 |
| `confirmed` | 已确认，可驱动标签、事件、资产和评测。 |
| `rejected` | 候选被拒绝。 |
| `superseded` | 被更新版本替代。 |
| `locked` | 下游发布、回填或评测运行中，暂不可编辑。 |
| `archived` | 归档只读。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `generate_candidate` | 无 | `candidate` |
| `edit` | `candidate`、`draft`、`rejected` | `draft` |
| `submit_review` | `candidate`、`draft` | `pending_review` |
| `confirm` | `candidate`、`draft`、`pending_review` | `confirmed` |
| `reject` | `candidate`、`pending_review` | `rejected` |
| `supersede` | `confirmed`、`draft`、`candidate` | `superseded` |
| `lock` | `confirmed` | `locked` |
| `unlock` | `locked` | `confirmed` |
| `archive` | `superseded`、`rejected` | `archived` |

### 迁移规则

- 一个音频会话或客户组在同一时间范围内只能有一个 `confirmed` 主边界；重算必须创建新版本并 `supersede` 旧边界。
- 保存边界只记录完整对话开始和结束；ASR、标签轨、说话人、事件关联和资产索引由下游重建，不能在边界对象内单独偏移。
- 串音、重复收录、主录音归属、跨设备合并属于高风险边界变更，默认进入 `pending_review`。
- `confirmed` 后触发下游 `asset_reindex_required` 或 `label_span_rebuild_required` 事件。

### 阻断条件

- `start_time >= end_time`、低于最小对话长度、超出源音频范围或跨不允许的分区。
- 缺少源 WAV / chunk、VAD、ASR、speaker_turns 或证据引用。
- 新边界与同一客户组其他已确认边界冲突，且无合并或拆分决策。
- 下游 TaskRun、EvalRun、BackfillRequest 正在运行并锁定该边界。
- 变更会影响人工确认标签、发布标签版本或评测黄金集，但未创建人审任务。

### 审计要求

- 记录旧边界、新边界、源片段、候选扩展片段、置信度、边界原因、串音证据。
- 记录修改人、修改来源：Agent 建议、人工修改、评测回流、badcase 回流。
- 记录下游影响：标签跨度、事件关联、资产索引、评测样本、报告事实。

## 6. LabelCandidate

LabelCandidate 是 Agent、规则、人工或评测回流生成的标签候选，不直接等同于线上标签。

### 状态

| 状态 | 含义 |
| --- | --- |
| `generated` | 已生成候选，未处理。 |
| `pending_review` | 等待人工接受、修改或拒绝。 |
| `accepted` | 原样接受，等待写入候选版本或草稿。 |
| `modified` | 人工修改后接受。 |
| `rejected` | 拒绝，不写入版本。 |
| `escalated` | 升级仲裁。 |
| `applied_to_version` | 已写入 LabelVersion 草稿或候选版本。 |
| `superseded` | 被新候选替代。 |
| `expired` | 超过有效期或输入版本已过期。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create` | 无 | `generated` |
| `request_review` | `generated` | `pending_review` |
| `accept` | `generated`、`pending_review` | `accepted` |
| `modify_and_accept` | `generated`、`pending_review` | `modified` |
| `reject` | `generated`、`pending_review` | `rejected` |
| `escalate` | `pending_review` | `escalated` |
| `apply_to_version` | `accepted`、`modified` | `applied_to_version` |
| `supersede` | `generated`、`pending_review`、`accepted`、`modified` | `superseded` |
| `expire` | `generated`、`pending_review` | `expired` |

### 迁移规则

- `generated` 必须绑定 `label_version_id` 或 `candidate_label_version_id`、`evidence_refs`、`confidence`、`model_version`、`prompt_version`。
- 自动化等级 L1 要求逐条确认；L2 允许低风险批量接受；高风险标签始终进入人审。
- 金额冲突、合规风险、串音污染、覆盖人工标注、单据回填相关候选默认为高风险。
- `applied_to_version` 只能写候选版本、ChangeSet 或草稿，不能直接修改 `published` LabelVersion。

### 阻断条件

- 标签层级、字段 Key、标签值或 JSON Schema 不合法。
- 证据片段、边界、ASR 或单据引用缺失。
- 当前标签版本、模型版本或 Prompt 版本与候选生成时不一致，且没有重新评测。
- 候选与人工标注、业务单据或已确认边界冲突，未完成仲裁。
- 用户无标签治理、人审或对应数据范围权限。

### 审计要求

- 记录来源：Agent 建议、规则、人工修改、评测回流、badcase 回流。
- 记录证据句、时间窗、置信度、冲突原因、Prompt / 模型 / 标签版本、Trace。
- 人工修改必须记录修改前后、修改理由、是否覆盖 Agent 建议、是否触发重评。

## 6.1 LabelObservation、LabelAggregate 与 LabelFact

三层对象分离来源事实、可重放聚合和权威标签事实：

| 对象 | 状态 | 迁移与不变量 |
| --- | --- | --- |
| `LabelObservation` | `materialized` | 创建后不可修改或覆盖；必须绑定强 Manifest 中的 tenant/project/subject/evidence、label/Prompt/Schema/model、服务端 source binding/correlation group、published calibration、置信度、输入/输出哈希和根 Trace。客户端不得自报 human-confirmed 或权威校准。 |
| `LabelCalibrationVersion` | `draft/published/retired`（行本身 append-only） | 绑定同 LabelVersion 的 GoldSetVersion、来源族、方法、样本数、training/content hashes；published 要求稳定 Gold，L2 策略只能锁定可重验 published 版本。 |
| `LabelAggregationRun` | `materializing → awaiting-review/completed`，异常时 `blocked/failed` | 抽取完成后由服务端按 extraction/policy/Observation 集生成确定性 ID 并自动执行；策略必须 active 且 mode 一致；输出 input/result hash、Aggregate、TaxonomySuggestion 和候选级 review refs。 |
| `LabelAggregate` | `awaiting-review → accepted/rejected`；低分为 `abstained` | 所有成员、贡献、证据、冲突、策略/校准版本和确定性哈希不可省略；相同输入和版本必须可重放。 |
| `LabelFact` | `materialized`（行本身无可变状态） | 只能由 Aggregate、HumanReviewDecision、LabelRecomputeRunItem 三选一来源创建；修正追加同 logical key 的下一 revision，旧行不得 UPDATE/DELETE。`LabelFactHead` 用 generation CAS 指向 current；L2 不得覆盖更高人工 authority。 |
| `LabelTaxonomySuggestion` | `pending → accepted/rejected/escalated` | 未知/无法 canonicalize 的标签只能进入建议与高风险人审，禁止直接写入事实或生产标签版本。 |

L1/L2 分流规则：

- L1 所有模型/LLM Aggregate 进入候选级人审。
- L2 仅低风险、`score ≥ 0.95`、互斥 margin `≥ 0.15`、证据完整、无冲突/新颖性、校准稳定且至少两个服务端锁定的独立来源族时自动接受，并进入 5% 分层抽检。每个来源必须命中 published LabelCalibrationVersion、Manifest source binding 和已验证 evidence；通过后才追加 `authority=l2-auto` LabelFact。
- 高风险、未知标签、证据缺失、分布外、来源冲突和人工历史高改写标签始终送审；低分只弃权，不自动制造负事实。
- 校准样本不足时不得 L2 自动接受。来源去重/去相关、数值权威优先、时间 IoU、层级 roll-up 和互斥规则均由锁定策略版本决定。

FeedbackExample 记录人工确认、修改、拒绝 badcase 和仲裁 Gold；每条必须绑定 review decision、target、字段 diff、失败原因、Trace。它是自动优化输入，但不会直接改写历史 Observation/Aggregate/Fact。

闭环审核形成终态时，任务的 `source_trace_id` 继续作为决策、Feedback、Fact、候选版本、审计与 Outbox 的根 `trace_id`；本次提交/仲裁请求的 Trace 另存 `action_trace_id`。两者都可查询，但后者不得覆盖前者。

高风险 `LabelAggregate` 和全部 `LabelTaxonomySuggestion` 使用候选级双盲任务：首份密封提交保持 `pending/in-review`，第二个不同自然人的相同结论形成 `accepted/rejected` 终态；结论不一致时进入 `awaiting-adjudication`。仲裁员必须是独立的自然人 `project_admin/review_arbitrator`，不得是任一盲审人。只有双评一致或独立仲裁完成，反馈样本才晋级 `gold`；普通单人决策不得伪造 Gold。

## 7. LabelVersion artifact lifecycle

LabelVersion 是项目级标签体系和标签规则的可发布版本，音频、标注、评测、洞察和资产都必须引用。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 草稿，可编辑。 |
| `candidate` | 候选版本，已汇入候选标签或规则变更。 |
| `validated` | 静态规则、层级、Schema 与 scope 校验通过，尚未冻结评测输入。 |
| `locked` | 标签、Prompt、模型、聚合策略和评测集绑定已冻结。 |
| `evaluating` | 正在进行自动化评测、影子评测或人工评测。 |
| `gate_blocked` | 发布门禁阻断。 |
| `review_required` | 需要人工审批。 |
| `approved` | 自然人审批通过，可发布为不可变制品。 |
| `published` | 可被 Release Bundle 引用的不可变制品；不表示任何环境已激活。 |
| `deprecated` | 不再用于新部署；历史事实、指标和报告仍可读。 |
| `archived` | 归档只读。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_draft` | 无 | `draft` |
| `add_candidates` | `draft`、`candidate` | `candidate` |
| `validate` | `draft`、`candidate` | `validated` |
| `lock_evaluation` | `draft`、`candidate`、`validated` | `locked` |
| `start_eval` | `locked` | `evaluating` |
| `eval_passed` | `evaluating` | `review_required` |
| `eval_failed` | `evaluating` | `gate_blocked` |
| `submit_review` | `candidate`、`validated`、`gate_blocked` | `review_required` |
| `approve` | `review_required` | `approved` |
| `publish_artifact` | `approved` | `published` |
| `deprecate` | `published` 且无受保护环境 active/draining 引用 | `deprecated` |
| `archive` | `deprecated` | `archived` |

### 迁移规则

- 同一 taxonomy 可同时保留多个 `published` 制品；每个环境只有一个 `ReleaseBundleHead` 决定当前生效 Bundle。
- 发布门禁必须消费同一个 LabelOptimizationRun 的 metrics、gateChecks、Human Loop 状态和影响资产。
- `gate_blocked` 只能继续优化、送人审、保持影子运行或创建新候选，不得直接发布。
- 系统/Agent 可生成发布草稿或阻断；制品批准、生产灰度/晋级、mapping publish 与废弃必须由自然人完成。系统只能执行已批准命令或已授权硬门禁回滚。
- 迁移期现存 `gray_releasing/rollback_pending/rolled_back` 仅作 legacy 只读值，收敛为制品状态与 environment activation ledger 后不得再写。

### 阻断条件

- 评测集缺失、评测指标未达阈值、冲突率超阈值、JSON 合法率不达标。
- HumanReviewTask 未完成，或高风险候选未仲裁。
- 影响资产未确认，回滚版本不存在，或下游 TaskVersion 未兼容。
- 标签层级、冲突规则、Prompt 输出 Schema 或后处理规则不合法。
- 试图跨项目复用标签版本作为生产版本。

### 审计要求

- 记录 ChangeSet、候选来源、指标提升来源、门禁结果、自动化等级、灰度比例。
- 记录发布人、审批人、生效时间、回滚目标、受影响任务、资产、评测和洞察。
- 记录失败门禁的阻断项和下一步动作。

## 7.1 LabelVersionItem、Mapping 与统计可比性

LabelVersionItem 的 `active/retired/pending-configuration` 是版本内快照；rename/replace/merge/retire/split-recompute 只存在于跨版本 Mapping。

| 对象 | 状态 | 迁移与不变量 |
| --- | --- | --- |
| `LabelMappingVersion` | `draft → validated → review_required → approved → published → superseded/archived` | 单 source→target edge；每个源 active item 必须有唯一 disposition；published 后不可编辑。 |
| `LabelMappingBundle` | `draft → compiling → validated → review_required → approved → published → superseded/archived` | 冻结完整 source set、target、edge versions、compiled paths、compiler version 与 canonical SHA；split/retire 路径必须中断。 |
| `LabelComparability` | `comparable/partial/structural-break/not-applicable` | 只有 target、bundle、metric definition、FactSet generation、fact_as_of、时区/周期/分母完全一致才 comparable。 |

`replace` 的 1:1 不自动 exact；`merge` 必须按 metric grain + event/fact lineage + time bucket + target label 去重；`split-recompute` 禁止比例分摊。retire 的 native 历史保留，适用期内 normalized 缺口是 coverage-gap/structural-break，只有适用期外才 not-applicable。

## 7.2 Fact Head、人工 Rebase 与 FactSet promotion

- `LabelFact` 行创建后不可变；`LabelFactHead` 以 expected generation 在同 logical key 内前进或回滚。supersedes 必须同 scope、同 key、紧邻 revision，禁止环。
- `occurred_at` 决定业务分桶，`recorded_at` 由服务端生成，`fact_as_of` 冻结 as-of 读取。
- 人工 draft 若标签版本已不被当前 Head 或授权历史回填范围允许，提交返回 `STALE_LABEL_VERSION`。显式 rebase 创建新 draft 并二次确认，旧 draft 保留。
- full recompute 状态为 `requested → running → candidate-complete → review_required → approved → promoted`，失败为 `partial-failed/failed/blocked`。只有完整候选 FactSet/Asset Manifest 可晋级；promotion 在单事务 CAS 整套 Head，禁止逐 Fact 切换。

## 7.3 PromptVersion、OptimizationRun 与 ReleaseDeployment

### PromptVersion

| 状态 | 含义 |
| --- | --- |
| `draft` | 真实模板、输出 Schema、生成参数、父版本、diff 和来源 badcase 已持久化，等待评测/审批。 |
| `candidate` | 自动优化或 modified child 已物化，并绑定独立双盲审核任务。 |
| `revision-required` | 当前候选的终态是“需修改”；保持只读并指向新的 child candidate，不可直接发布。 |
| `approved` | 离线门禁通过且自然人批准，可进入 shadow/gray Bundle。 |
| `published` | 对应 ReleaseDeployment 已通过人工 promote，成为 PromptAsset 当前版本。 |
| `deprecated` | 被新发布版本替代，只读保留。 |

PromptVersion 内容以 canonical SHA-256 标识；同一 PromptAsset 的版本号唯一。任何正文、Schema、模型参数或标签版本改变都必须创建新版本，不能原地编辑 `approved/published`。

Prompt 候选审批采用双盲：首份提交进入 `in-review`，第二个不同自然人提交后，一致则形成 `approved/rejected/revision-required`；不一致进入 `awaiting-adjudication`。仲裁员必须是独立 `review_arbitrator`，不能是任一审核人。密封 decision/diff 在形成成对结论前不得通过 Outbox 或普通任务详情泄漏；`modified` 只能改 `template/output_schema/generation_params` 并逐字段记录 before/after/reason。

`modified` 的终态是 `revision-required`，不是批准原候选。服务端把一致/仲裁后的允许字段应用到一个 content-hash 去重的 child PromptVersion，设置 `parent_version_id=原候选`，同时创建 child 兼容候选和全新双盲任务；只有 child 再审核、再锁定评测后才可发布。

### LabelOptimizationRun

触发扫描先检查最近 24 小时审核量、改写率、冲突率、JSON 合法率、关键 recall 代理和失败原因簇；再执行同租户/项目/标签版本单活、canonical hash 去重和 24 小时冷却。运行锁定 label/Prompt/model/aggregation-policy/eval-dataset versions 及预算：最多 3 轮、每轮 2–5 候选、最长 2 小时和项目成本上限。

| 状态/阶段 | 允许动作 |
| --- | --- |
| `queued/running` | 生成 P-CODE Prompt 候选；不得标记发布成功。 |
| `evaluating` | 只在锁定 Dev 排序，并使用 hidden holdout、Gold、Boundary、Adversarial、Fresh、Canary 和历史回归做发布门禁。 |
| `awaiting-review` | 等待 Prompt 候选人工审批。 |
| `blocked` | 超预算、无显著收益、关键指标回退、单活/冷却/样本门禁或连续失败；展示阻断和下一动作。 |
| `completed` | 候选与评测产物已物化；不代表已发布。 |

### ReleaseDeployment

ReleaseDeployment 冻结 label、Prompt、model、aggregation policy、eval dataset/run 和 rollback target 为一个 Bundle，并保存 `bundle_sha256`。

环境 activation lifecycle 与 LabelVersion artifact lifecycle 分离：当前 Head 对应 `active`；新 Head ACK/CAS 成功后旧 activation 进入 `draining`，停止受理新运行；排空或 deadline 处理完毕后进入 `inactive`，补偿命令可形成 `rolled-back` ledger event。运行创建必须在同事务锁定 `ReleaseBundleHead` generation/deployment/bundle/label version；不能只检查 LabelVersion 为 published。

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create` | 无 | 门禁通过为 `pending/queued` 并创建 publish command；否则 `blocked` |
| `publish-ack` | `pending/materializing` | `shadowing`；ACK 或 Bundle/CAS 漂移则 `blocked` |
| `approve-gray-request` | `shadowing`、`monitoring` | `materializing`，固定命令与 expected head，不立即切 10% |
| `approve-gray-ack` | `materializing` | `gray-releasing`，固定 10%；ACK/CAS 失败则 `blocked` |
| `monitor` | `gray-releasing` | `monitoring` |
| `monitor_sample_hard_regression` | `shadowing`、`gray-releasing`、`monitoring` | rollout 立即归零；有稳定目标则创建 rollback command 并 `materializing`，否则 safe-stop `blocked` |
| `promote-request` | `gray-releasing`、`monitoring` | 在线门禁通过后 `materializing`，不立即发布 Prompt |
| `promote-ack` | `materializing` | `completed`、100% 并以 CAS 切 active head/Prompt pointer；失败为 `blocked` |
| `rollback-request` | 除 `rolled-back` 外且存在稳定目标 | rollout=0、`materializing` |
| `rollback-ack` | `materializing` | 源 `rolled-back`、目标 `completed`，CAS 切回 LKG；失败为 `blocked` |

`approve-gray/promote` 只能由自然人项目管理员请求，系统身份不得代签。每个部署只允许一个 `active_slot=active` 的 ReleaseCommand；命令冻结 action、command/bundle SHA、expected deployment status 与 expected head generation/deployment/hash。Outbox dispatch 只把命令推进为 materializing，不改变有效 Bundle。

成功 ACK 必须由受信 completion receipt 精确回显 `release_command_id/command_sha256/deployment_id/environment/action/bundle_sha256/applied=true`。服务端在同一事务重验 Bundle、Prompt 指针、回滚目标和 head CAS；不一致、并发 generation 漂移或执行失败均关闭 command active slot 并阻断，不切换 `release_bundle_heads`。

唯一例外是首次 production LKG bootstrap：仅当环境无 head、部署只因缺少 rollback/head 而 blocked、无 active command/歧义 completed 部署且 Bundle 可重验时，自然人项目管理员可一次性确认并建立 generation=1 的 head。之后该入口必然冲突，所有切换恢复两阶段 ACK。

在线样本只能由 `system` 服务身份写入，使用 `sample_id + canonical hash` 幂等，并锁定 `expected_status`。硬阈值为 JSON 合法率 `<99.5%`、冲突率 `>5%`、关键 recall 下降 `>2pp`、人工改写率上升 `≥3pp`、成本比 `>1.10` 或延迟比 `>1.20`；命中任一项必须把 rollout 置零，并请求自动 rollback command 或 safe-stop blocked，不能在 ACK 前声明已回滚。

每个监控样本还携带 `stable_window_complete`，默认 `false`，由 system 写入 `ReleaseDeployment.monitor_metrics`。该字段为 `false` 时 `promote` 必须返回 `STABLE_WINDOW_INCOMPLETE`；只有 `true` 且其余在线门禁通过，自然人 `project_admin` 才能创建 promote command。进入 `completed` 仍须受信 ACK 与 head CAS；稳定窗口完成本身不得触发自动 promote。

## 8. VoiceprintEnrollment

VoiceprintEnrollment 表示声纹入库、候选匹配、合并、拆分、禁用和保留期处理。声纹 embedding 可进入 Qdrant 召回，但最终身份状态以 MySQL 为准。

### 状态

| 状态 | 含义 |
| --- | --- |
| `candidate` | 从音频或人工操作生成入库候选。 |
| `quality_checking` | 正在检查音频质量、时长、SNR、重叠说话和 embedding 质量。 |
| `pending_policy` | 等待隐私、授权、保留期或项目策略校验。 |
| `pending_review` | 等待人工确认身份、合并或拆分。 |
| `enrolled` | 已入库并可用于候选匹配。 |
| `merge_proposed` | Agent 或人工提出合并建议。 |
| `merge_pending_review` | 合并等待审批。 |
| `merged` | 已合并到目标声纹身份。 |
| `split_pending_review` | 拆分等待审批。 |
| `split` | 已拆分为新身份或候选。 |
| `rejected` | 候选被拒绝。 |
| `disabled` | 暂停使用，不删除历史审计。 |
| `deleted_logical` | 因保留期或合规要求逻辑删除，保留最小审计。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_candidate` | 无 | `candidate` |
| `start_quality_check` | `candidate` | `quality_checking` |
| `quality_passed` | `quality_checking` | `pending_policy` |
| `quality_failed` | `quality_checking` | `rejected` |
| `policy_passed` | `pending_policy` | `pending_review`、低风险 `enrolled` |
| `policy_failed` | `pending_policy` | `rejected` |
| `approve_enroll` | `pending_review` | `enrolled` |
| `reject` | `pending_review`、`candidate` | `rejected` |
| `propose_merge` | `enrolled` | `merge_proposed` |
| `submit_merge_review` | `merge_proposed` | `merge_pending_review` |
| `approve_merge` | `merge_pending_review` | `merged` |
| `reject_merge` | `merge_pending_review` | `enrolled` |
| `request_split` | `enrolled`、`merged` | `split_pending_review` |
| `approve_split` | `split_pending_review` | `split` |
| `disable` | `enrolled`、`merged` | `disabled` |
| `retention_delete` | `disabled`、`rejected` | `deleted_logical` |

### 迁移规则

- 声纹合并和拆分默认高风险，必须人审；Agent 只能生成候选和解释。
- 客户声纹、员工声纹必须区分策略；客户声纹受更严格保留期、脱敏和访问权限控制。
- 跨项目声纹匹配只能产生候选，不得自动建立最终身份；必须校验租户策略和项目授权。
- Qdrant 中只能保存 embedding 引用和可回跳 payload，不保存最终身份审批状态。

### 阻断条件

- 无合法授权、无保留期策略、隐私策略未开启或数据敏感等级不允许。
- 音频质量低、有效说话时长不足、重叠说话比例超阈值、声纹相似度低于阈值。
- 合并目标跨租户，或跨项目但未授权。
- 操作人无 `voiceprint_sensitive` 数据权限。
- 合并会影响已发布标签、评测集或报告事实但未评估影响。

### 审计要求

- 记录音频证据、embedding 引用、质量评分、匹配候选、阈值、人工确认记录。
- 合并和拆分必须记录源身份、目标身份、影响对象、回滚路径。
- 不在普通审计中记录原始 embedding 值；只记录向量引用、hash 和访问记录。

## 9. EvalRun

EvalRun 表示自动化评测、人工评测、模型对比、标签候选评测和发布门禁评测。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 评测草稿，尚未提交运行。 |
| `pending` | 已创建运行请求。 |
| `running` | 正在执行模型、规则或指标计算。 |
| `waiting_human` | 等待人工评测、双盲、一致性或仲裁。 |
| `scoring` | 正在汇总指标、归因和门禁。 |
| `success` | 评测完成。 |
| `failed` | 评测失败。 |
| `blocked` | 被数据、权限、门禁或版本一致性阻断。 |
| `cancelled` | 已取消。 |
| `published` | 评测结果已作为门禁、报告或回流依据。 |
| `superseded` | 被新评测运行替代。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_draft` | 无 | `draft` |
| `submit` | `draft` | `pending` |
| `start` | `pending` | `running` |
| `request_manual_eval` | `running` | `waiting_human` |
| `manual_eval_completed` | `waiting_human` | `scoring` |
| `score` | `running`、`waiting_human` | `scoring` |
| `complete` | `scoring` | `success` |
| `fail` | `running`、`scoring` | `failed` |
| `block` | `draft`、`pending`、`running` | `blocked` |
| `cancel` | `draft`、`pending`、`running`、`waiting_human` | `cancelled` |
| `publish_results` | `success` | `published` |
| `supersede` | `success`、`published`、`failed` | `superseded` |

### 迁移规则

- EvalRun 必须固定评测集、样本版本、模型版本、标签版本、Prompt 版本、指标口径。
- 发布门禁 EvalRun 不能在运行中改变样本集或阈值。
- 标签发布门禁必须精确包含 `golden/boundary/adversarial/fresh/canary/regression` 六套件，并为每套件固化非空样本数、样本清单 SHA-256、格式/质量/安全/成本/延迟/可观测指标；总体样本清单哈希必须可重算。
- 标签评测成功回执必须使用 `paired-bootstrap-v1`、95% CI、至少 1000 次重采样并保存随机种子；paired sample count 必须等于六套件总样本数。门禁未通过时 EvalRun 只能进入 `blocked`，不能保留客户端或 worker 自报的成功语义。
- EvalRun 创建、完成回执和 ReleaseDeployment 创建/推进时均重新校验锁定数据集对象快照、标签/Prompt/聚合策略/优化运行及 binding hash；任一版本、内容哈希、状态或对象 HEAD 漂移均 fail closed。
- 人工评测必须支持多人一致性、仲裁和标准答案沉淀。
- `published` 评测结果可生成 Badcase、HumanReviewTask、回填建议、发布阻断或洞察事实。

### 阻断条件

- 评测集为空、样本无证据回跳、版本引用不一致。
- 评测集正在被修改或未锁定。
- 人工评测任务未完成，但指标口径要求人工黄金集。
- 运行会跨租户或跨项目读取样本。
- 指标计算失败、输出 Schema 不合法或成本超预算。

### 审计要求

- 记录数据集、样本 hash、指标口径、模型/标签/Prompt 版本、阈值、run_config。
- 记录每个 badcase、发布阻断、回流动作的来源 EvalRun。
- 人工评测记录评审人、盲审策略、一致性指标、仲裁结论。

## 10. Badcase

Badcase 是评测失败、人工修正、线上异常或 Agent 识别出的可回流问题样本。

### 状态

| 状态 | 含义 |
| --- | --- |
| `candidate` | 候选 badcase，未确认。 |
| `triaged` | 已分类和定优先级。 |
| `confirmed` | 已确认，可进入回流。 |
| `linked_to_dataset` | 已加入评测集、回归集或训练样本。 |
| `in_fix` | 正在通过规则、Prompt、模型或数据修复。 |
| `fixed` | 已完成修复方案。 |
| `regression_passed` | 回归通过。 |
| `regression_failed` | 回归失败，需要继续处理。 |
| `waived` | 有理由豁免，不作为阻断。 |
| `archived` | 归档只读。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_candidate` | 无 | `candidate` |
| `triage` | `candidate` | `triaged` |
| `confirm` | `triaged` | `confirmed` |
| `link_dataset` | `confirmed` | `linked_to_dataset` |
| `assign_fix` | `confirmed`、`linked_to_dataset`、`regression_failed` | `in_fix` |
| `mark_fixed` | `in_fix` | `fixed` |
| `run_regression_passed` | `fixed` | `regression_passed` |
| `run_regression_failed` | `fixed` | `regression_failed` |
| `waive` | `triaged`、`confirmed`、`regression_failed` | `waived` |
| `archive` | `regression_passed`、`waived` | `archived` |

### 迁移规则

- Badcase 必须绑定 evidence、EvalRun 或 HumanReviewTask 来源。
- 相似 badcase 检索可来自 Qdrant，但去重、确认、修复状态以 MySQL 为准。
- 作为发布阻断的 badcase 不得归档，除非阻断解除、豁免审批或回归通过。
- `waived` 必须说明业务理由、影响范围和有效期。

### 阻断条件

- 缺少证据片段、错误类型、影响模块或归因。
- 与已有未归档 badcase 重复但未关联。
- 豁免高风险 badcase 无审批。
- 回归运行未完成却试图标记 fixed 或 archive。
- 操作跨项目样本或无评测/badcase 权限。

### 审计要求

- 记录错误类型、严重度、相似样本、归因、修复建议、负责人、关联规则/Prompt/模型变更。
- 记录进入评测集、训练样本、数据资产或发布门禁的时间和操作者。

## 11. HotwordPackVersion

HotwordPackVersion 是 ASR 领域热词包的不可变发布单元。生产 Audio Intelligence 运行只能绑定 `published` 的 `version_id`（在跨域请求中字段名为 `hotword_pack_version_id`）；候选版本只能以 `execution_mode=shadow` 运行。灰度属于后续 TaskVersion 发布通道，不进入词包状态机。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 可编辑候选版本。 |
| `validating` | RunRecord 正在校验规范化重复、敏感实体和 manifest，并异步构建 provider 产物。 |
| `ready_for_eval` | 受信构建完成回执已冻结 `content_sha256 + manifest + compiled_provider + provider_artifact_ref`，可创建影子复测。 |
| `evaluating` | EvalRun 正在运行，评测集、基线和候选不可再改变。 |
| `gate_blocked` | 至少一个硬门禁失败。 |
| `review_required` | 自动指标通过，等待模型负责人审阅。 |
| `approved` | 模型负责人已批准，等待项目管理员确认发布。 |
| `published` | 不可变词包版本已发布，可生成/绑定生产 TaskVersion 草稿；不等于已生产激活。 |
| `deprecated` | 不再推荐新任务使用，已有血缘保留。 |
| `rolled_back` | 该版本已被受控回滚操作替代，历史只读。 |
| `archived` | 归档只读。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `start_validation_and_build` | `draft`、`ready_for_eval`、`gate_blocked`、`review_required`、`approved` | `validating` |
| `trusted_build_completed` | `validating` | `ready_for_eval` |
| `validation_failed` | `validating` | `gate_blocked` |
| `start_shadow_eval` | `ready_for_eval` | `evaluating` |
| `trusted_eval_completed_blocked` | `evaluating` | `gate_blocked` |
| `trusted_eval_completed_passed` | `evaluating` | `review_required` |
| `model_approve` | `review_required` | `approved` |
| `project_admin_publish_requested` | `approved` | `approved`（保持等待受信完成回执） |
| `trusted_publish_completed` | `approved` | `published` |
| `deprecate` | `published` | `deprecated` |
| `request_controlled_rollback` | 当前 `published` | 保持 `published`，等待不同自然人的项目管理员审批 |
| `materialize_controlled_rollback` | 当前 `published` | 源版本 `rolled_back`；同包历史 `published` 目标保持 `published` |
| `archive` | `draft`、`gate_blocked`、`deprecated`、`rolled_back` | `archived` |

所有修改请求携带 `expected_resource_version`，响应返回新的 `resource_version`；冲突返回 `409`。`PATCH` 仅在目标状态为 `validating` 时接受可选 `provider`，它是异步 build 的目标输入，不是客户端对 `compiled_provider` 的赋值；`compiled_provider` 和 `provider_artifact_ref` 只能由受信构建完成回执固化。从 `ready_for_eval/gate_blocked/review_required/approved` 重新构建时，必须清除旧 EvalRun、gate、`eval_locked` 和模型批准，防止旧审批复用。`published/deprecated/rolled_back/archived` 内容不可编辑，修复必须创建新版本。构建完成后，`content_sha256`、`manifest_storage_object_id`、`compiled_provider` 和 `provider_artifact_ref` 构成冻结绑定；创建 EvalRun 后再冻结 `eval_dataset_id + dataset_version + manifest_storage_object_id + manifest_sha256 + manifest_provider + manifest_bucket + manifest_object_key + manifest_size_bytes + manifest_etag + snapshot_sha256`，运行发起和完成回执前均重新执行真实对象 HEAD，任一项覆盖或漂移都拒绝。词包发布请求返回 202 `pending RunAction`，受信完成回执通过绑定与审批复核后只更新候选 `current_version_id`、写审计/Outbox 并创建绑定该版本的 TaskVersion `draft`，版本保持 `production_active=false`。TaskVersion 另行通过 ReleaseGate 后，worker 才原子切换 `hotword_packs.production_version_id`、停用旧生产版本、标记新版本 `production_active=true` 并允许生产运行；运行时同时校验 TaskVersion 已发布和生产指针一致。

受控回滚只能由 `model_engineer` 从逻辑包当前 `published` 源版本发起，并选择同包历史 `published` 目标。请求原子冻结源版本、目标版本、逻辑包三者的 `resource_version` 与 `root_trace_id`，写 `hotword_rollback` RunRecord 和 `hotword_pack_version.rollback-requested` Outbox；首次 worker 处理必须停在 `blocked`。不同自然人的 `project_admin` 通过通用 Run decision 批准后事件重新入队，worker 在同一事务内重新校验全部冻结绑定；任一版本、指针或根 Trace 漂移都 fail closed。成功只把源版本改为 `rolled_back`、把 `hotword_packs.current_version_id` 指向目标并写审计/`rolled-back` 事件，不创建或发布 TaskVersion，不触碰历史转写、人工确认、Badcase 与资产物化。

### 统计、候选与门禁

- 召回率为 `correct_count / expected_count`；易错率为 `weighted_error_count / expected_count`；误增强率为 `false_insert_count / recognized_hotword_count`。分母为零时输出 `not_available`，不得伪造为零。
- 易错候选默认要求可信出现不少于 3 且易错率不低于 20%，或人工修正不少于 2 次；否则标记“疑似”，不能自动进入发布词项。
- 优先级按 `log(1+expected_count) × error_rate × evidence_confidence × business_weight` 归一化为 `0–100`。
- 仲裁金标和人工确认证据权重为 `1.0`，强关联业务主数据为 `0.8`；知识实体、模型建议和低置信结果只做发现，不得单独确认易错。
- 发布候选集必须可信出现不少于 30，且单词不少于 3。
- 热词易错率相对下降不少于 20%，或召回提升不少于 3pp。
- 误增强率增幅不超过 0.5pp；全局 CER/WER 退化不超过 0.2pp；下游实体/标签 F1 退化不超过 0.5pp。
- P95 延迟及分钟成本增幅均不超过 5%。
- EvalRun 创建接口只接受评测集、provider 和乐观锁版本，返回 202 `pending`；baseline/candidate metrics 与 gate 只能由受信 worker 完成回执产生。完成回执必须匹配冻结版本、评测集和 provider 产物，并得到 `success + locked`；模型负责人将状态推进到 `approved`，不同自然人的项目管理员在 publish 请求中明确 `confirmation=publish`。

### 阻断与审计

- 规范词采用 Unicode NFKC、首尾空白/标点清理和拉丁字母小写；同一版本规范化重复返回 `409`，别名必须显式录入，不自动生成繁简体或同音词。
- 客户姓名、手机号、车牌、VIN 等敏感实体返回 `422`；知识库候选只能作为 `source_badcase_id` 或发现证据，不能绕过人审发布。
- 请求过 `hotword_pack_version_id` 的成功完成回执必须在 `hotword_diagnostics` 提供规范字段 `matched_terms`、`missed_terms`、`false_boosted_terms` 与 `diagnostics_storage_object_id`；要求词时间戳时还必须提供 `word_timestamps_storage_object_id`，缺失返回 `422`。
- 分析、评测、发布和回滚统一使用 RunRecord、幂等键、审计、Outbox、重试和死信；所有资产沿用同一 `root_trace_id`。
- 血缘必须连接原 ASR 资产、词级证据、Badcase、词包版本、EvalRun、新转写资产、TaskVersion 草稿和受控回填记录；历史转写与人工确认永不原地覆盖。

ASR 热词 Badcase 在 UI 看板使用 `pending-attribution → pending-review → pending-backflow → in-regression`，分别映射通用 Badcase 的待归因、已确认、回流修复和回归验证阶段；`rejected` 只读保留决策证据。

## 12. InsightReport

InsightReport 是洞察页或 Agent 生成的报告资产，必须能下钻到证据链。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 报告草稿。 |
| `generating` | 正在汇总指标、召回证据和生成内容。 |
| `generated` | 已生成，等待校验或发布。 |
| `review_required` | 涉及高风险结论、外发或敏感数据，需审核。 |
| `approved` | 审核通过。 |
| `published` | 项目内发布可见。 |
| `export_pending` | 正在导出 PDF、表格、证据包或外部附件。 |
| `exported` | 已导出。 |
| `failed` | 生成或导出失败。 |
| `retracted` | 已撤回。 |
| `archived` | 归档只读。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_draft` | 无 | `draft` |
| `generate` | `draft`、`failed` | `generating` |
| `generation_succeeded` | `generating` | `generated` |
| `generation_failed` | `generating`、`export_pending` | `failed` |
| `request_review` | `generated` | `review_required` |
| `approve` | `review_required` | `approved` |
| `publish` | `generated`、`approved` | `published` |
| `export` | `published`、`approved` | `export_pending` |
| `export_succeeded` | `export_pending` | `exported` |
| `retract` | `published`、`exported` | `retracted` |
| `archive` | `retracted`、`generated`、`exported` | `archived` |

### 迁移规则

- 报告中的每个关键结论必须绑定指标版本、标签版本、模型版本和证据引用。
- 外发报告必须完成脱敏、导出权限和接收人校验。
- Agent 可生成 `draft`、`generated` 或 `review_required`，不能绕过审核直接外发。
- `retracted` 必须保留撤回原因和已通知对象。

### 阻断条件

- 指标数据过期、聚合口径与当前上下文不一致。
- 证据链缺失，无法从报告结论回跳到标签、音频片段、评测或资产。
- 报告包含跨租户、跨项目、敏感声纹或原始音频信息但未授权。
- 导出目标、对象存储、外部回写 endpoint 或水印策略不可用。

### 审计要求

- 记录查询范围、指标口径、证据引用、生成模型、Agent Trace、人工修改。
- 记录发布、导出、下载、外发、撤回和接收人。
- 导出文件记录对象存储 key、hash、保留期和脱敏策略。

## 13. BackfillRequest

BackfillRequest 表示资产重算、标签资产回填、下游重算和失败分区修复。

### 状态

| 状态 | 含义 |
| --- | --- |
| `draft` | 回填草稿。 |
| `impact_estimating` | 正在预估影响范围、下游资产和耗时。 |
| `pending_approval` | 等待审批。 |
| `approved` | 审批通过。 |
| `scheduled` | 已进入执行计划。 |
| `running` | 正在执行回填。 |
| `partial_success` | 部分分区成功。 |
| `success` | 回填完成。 |
| `failed` | 回填失败。 |
| `blocked` | 被锁、权限、门禁或数据风险阻断。 |
| `cancelling` | 取消中。 |
| `cancelled` | 已取消。 |
| `rolled_back` | 已按版本回滚。 |
| `expired` | 影响预估或审批过期。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `create_draft` | 无 | `draft` |
| `estimate_impact` | `draft`、`failed` | `impact_estimating` |
| `impact_ready` | `impact_estimating` | `pending_approval`、低风险 `approved` |
| `impact_failed` | `impact_estimating` | `blocked` |
| `approve` | `pending_approval` | `approved` |
| `reject` | `pending_approval` | `draft` |
| `schedule` | `approved` | `scheduled` |
| `start` | `scheduled` | `running` |
| `complete` | `running` | `success`、`partial_success` |
| `fail` | `running` | `failed` |
| `block` | `draft`、`impact_estimating`、`scheduled`、`running` | `blocked` |
| `cancel_request` | `draft`、`pending_approval`、`approved`、`scheduled`、`running` | `cancelling` |
| `cancel_confirmed` | `cancelling` | `cancelled` |
| `rollback` | `success`、`partial_success` | `rolled_back` |
| `expire` | `draft`、`pending_approval` | `expired` |

### 迁移规则

- `draft` 必须声明资产、分区、时间、地点、标签版本、模型版本、是否重算下游、是否覆盖已有结果。
- 回填执行前必须完成影响预估，输出预计影响音频、下游资产、耗时、失败风险和回滚方式。
- 覆盖已有结果、影响人工确认结果、批量重算下游或外部回写属于高风险，必须审批。
- 执行结果必须写入新资产版本或新分区结果，不得静默覆盖无版本数据。

### 阻断条件

- 缺少影响预估、幂等键、回滚版本或资产血缘。
- 目标资产或分区被 TaskRun、EvalRun、发布或其他 BackfillRequest 锁定。
- 回填会覆盖人工确认标签、声纹身份或发布报告事实，但未审批。
- 下游外部回调不可用且策略要求同步回执。
- 操作人无资产回填、覆盖写入或项目数据范围权限。

### 审计要求

- 记录回填范围、影响对象、审批人、执行 run id、失败分区、重试次数、回滚版本。
- 记录每个受影响资产的旧版本、新版本、下游重算状态和外部回调状态。

## 14. ExternalCallback

ExternalCallback 表示处理后 WAV URL、标签结果、复核结论、报告或证据包对外部系统的回写尝试和回执。

### 状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 回调已入队。 |
| `signing` | 正在生成签名、幂等键和脱敏 payload。 |
| `sending` | 正在发送。 |
| `retry_wait` | 发送失败，等待重试。 |
| `success` | 外部系统确认成功。 |
| `failed` | 重试后失败，可人工处理。 |
| `dead_letter` | 进入死信队列。 |
| `blocked` | 因配置、权限、脱敏或 endpoint 校验阻断。 |
| `cancelled` | 来源运行取消或回写取消。 |

### 允许动作

| 动作 | 允许来源状态 | 目标状态 |
| --- | --- | --- |
| `enqueue` | 无 | `pending` |
| `prepare_payload` | `pending`、`retry_wait` | `signing` |
| `send` | `signing` | `sending` |
| `ack_success` | `sending` | `success` |
| `ack_retryable_failure` | `sending` | `retry_wait` |
| `ack_terminal_failure` | `sending`、`retry_wait` | `failed` |
| `move_dead_letter` | `failed`、`retry_wait` | `dead_letter` |
| `block` | `pending`、`signing` | `blocked` |
| `cancel` | `pending`、`retry_wait`、`blocked` | `cancelled` |
| `create_retry_run` | `failed`、`dead_letter` | 新运行 `pending` |

### 迁移规则

- 回调必须绑定来源对象：TaskRun、BackfillRequest、HumanReviewTask、InsightReport 或 ExportJob。
- 每次发送使用稳定 `idempotency_key`，外部 endpoint 应可幂等处理。
- payload 必须按目标系统配置脱敏；敏感字段只传引用或签名 URL。
- `create_retry_run` 必须创建新的运行或回调发送记录，并引用原 `run_id`、`event_id`、`trace_id` 和 callback id，原失败记录保持不可变。

### 阻断条件

- endpoint 未验证、密钥引用不可用、签名算法缺失、回调绑定被禁用。
- payload 包含未授权的 PII、声纹、原始音频或跨项目数据。
- 回调目标不属于当前租户/项目绑定。
- 达到最大重试次数或失败率熔断。
- 来源对象未达到允许回写状态，例如 TaskRun 未成功、人审未批准、报告未发布。

### 审计要求

- 记录 endpoint id、payload hash、签名 key ref、attempt、HTTP 状态码、响应摘要、错误分类。
- 不记录明文密钥、token 或完整敏感 payload。
- 记录人工重试人、重试原因、原失败记录引用和新运行状态。
