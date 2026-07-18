# Agentic 智能化设计文档

## 1. 文档定位

本文档从《设计文档.md》中抽取并扩展智能化 Agentic 能力，作为平台智能决策、工具调用、低置信处理、冲突仲裁、评测回流和 badcase 沉淀的专项设计文档。

Agentic 层不是底层任务调度系统，也不是替代 VAD、ASR、Diarization、声纹、标签模型的算法模型。它是位于工作流编排和模型工具之间的智能决策层，负责在复杂、不确定、低置信和多模型冲突场景中做可解释、可追踪、可回滚的决策。

## 2. Agentic 定位

平台底层可以由 Temporal / Celery 执行单条音频长任务，由 Dagster 执行批量数据资产编排，由 dbt 做指标建模。

Agentic Runtime 只处理以下问题：

- 模型结果不确定时的判断。
- 多模型输出冲突时的仲裁。
- 边界切分、合并、打标的局部智能决策。
- 同一天、同门店、不同销售和不同收声设备之间的串音识别与归属纠偏。
- 低置信样本的自动重跑、转人工、加入 badcase。
- 人工标注和模型输出之间的差异分析。
- 评测结果的归因和修复建议。
- 数据资产质量异常时的诊断建议。
- 根据用户角色、语言、皮肤、当前任务和异常类型推荐更合适的视图、动效和高亮策略。

Agentic Runtime 不负责：

- 长任务生命周期调度。
- 批量资产依赖调度。
- 数据库事务一致性。
- 直接修改不可回滚的业务数据。
- 绕过权限系统访问跨租户数据。

## 2.1 与任务配置、Dagster 和模型服务的边界

任务配置工作台负责定义“任务类型、画布版本、任务版本、调度、A/B 实验、模型服务绑定、输出资产和外部回写”。Dagster 负责把已发布任务版本固化为可运行的 Job / Asset Graph。ModelService 负责封装 VAD、ASR、Diarization、Tagger 等模型服务的调用契约。

Agentic Runtime 不能绕过这些边界直接修改任务发布态或裸调用模型服务。它只能通过以下方式参与：

- 在任务草稿阶段生成映射建议、节点建议、兼容性诊断和调度风险提示。
- 在运行阶段读取资产、模型输出、质量指标和错误日志，生成重跑、转人工、回填、降级或发布阻断建议。
- 在标签治理阶段基于现有数据异步生成多级标签候选、冲突解释和 Human Loop 任务。
- 在人工确认后，把结果写入候选版本、标注候选、评测发现、badcase 或新资产版本。

ASR / Diarization 服务边界：

- ASR 服务只产出 `transcript_asset`、`transcript_segments`、`word_timestamps` 和 `asr_quality`。
- Diarization 服务只产出 `speaker_turns`、`speaker_embeddings_ref` 和 `speaker_quality`。
- `speaker_transcript_view` 是派生资产，由 `transcript_segments` 与 `speaker_turns` 按时间重叠合成。
- Agent 可以诊断 ASR 与 speaker_turns 的冲突，但不能把 ASR 节点设计成同时负责转写和最终说话人身份。

外部回写边界：

- 处理后的 WAV 上传 OBS / S3、回传 URL、标签结果回调、复核结论回调都属于任务输出节点。
- Agent 可以建议回写字段映射、失败重试策略和影响范围，但实际写入必须由任务运行和权限系统执行。

## 3. 核心原则

### 3.1 人机协同优先

Agent 可以建议、解释、排序、重跑、路由，但关键高风险动作必须支持人工确认。

高风险动作包括：

- 覆盖人工标注。
- 批量回填。
- 修改标签体系。
- 合并声纹身份。
- 影响模型发布判断。
- 标记发布阻断。

### 3.2 可解释

每次 Agent 决策必须记录：

- 输入数据
- 使用工具
- 参考上下文
- 决策结果
- 置信度
- 判断依据
- 可替代选项
- 是否需要人工复核

### 3.3 可追踪

所有 Agent 行为必须进入 Agent Trace。

Trace 至少包含：

- `trace_id`
- `tenant_id`
- `project_id`
- `run_id`
- `agent_name`
- `agent_version`
- `input_refs`
- `tool_calls`
- `model_outputs`
- `decision`
- `confidence`
- `risk_level`
- `human_review_required`
- `created_at`

### 3.4 可回滚

Agent 生成的结构化结果不得直接覆盖原始数据，应写入候选结果或新版本。

推荐写入方式：

- `model_output`
- `agent_suggestion`
- `annotation_candidate`
- `asset_version`
- `eval_finding`
- `badcase_reason`

### 3.5 租户和项目隔离

Agent 只能访问当前租户、当前项目授权范围内的数据。

所有工具调用都必须携带：

- `tenant_id`
- `project_id`
- `user_id`
- `role`
- `permission_scope`

## 4. Agentic 总体架构

```text
产品 UI
  ↓
FastAPI BFF / 权限 / 项目上下文
  ↓
Workflow Orchestrator
Temporal / Celery / Dagster
  ↓
Agentic Runtime
  ├── Agent Router
  ├── Policy Guard
  ├── Context Builder
  ├── Tool Registry
  ├── Decision Engine
  ├── Human Review Gateway
  └── Trace Recorder
  ↓
能力工具层
VAD / ASR / Diarization / Voiceprint / Boundary / Tagger / LLM Judge / Eval
  ↓
数据层
MySQL / Object Storage / Redis / Qdrant
```

## 5. Agentic Runtime 模块

### 5.1 Agent Router

根据任务类型、数据状态、置信度和风险等级选择合适 Agent。

输入：

- 任务类型
- 当前节点
- 模型输出
- 置信度
- 冲突类型
- 项目配置
- 用户权限

输出：

- 目标 Agent
- 执行策略
- 是否需要人工复核
- 可用工具列表

### 5.2 Policy Guard

负责权限、安全和风险控制。

校验内容：

- 租户权限
- 项目权限
- 数据访问范围
- 工具调用权限
- 是否允许自动写入
- 是否允许批量操作
- 是否需要人工确认

### 5.3 Context Builder

为 Agent 构造最小必要上下文。

上下文包括：

- 当前音频片段
- 相邻前后片段
- 同时间同地点片段
- 同一天同门店不同销售的录音片段
- 同时间窗内相邻收声设备的录音片段
- 设备和门店空间位置
- 销售排班和工牌绑定关系
- 当前数据管理聚合路径
- 时间 / 空间 / 事件 / 人物分层节点
- 聚合节点摘要和异常指标
- 用户角色
- 用户语言
- 当前皮肤
- 动效偏好
- 当前页面和视图模式
- ASR 文本
- Speaker Turn
- VAD 区间
- 多设备能量峰和重叠区间
- 串音候选和主录音候选
- 标签候选
- 人工标注
- 历史 badcase
- 当前标签版本
- 当前模型版本
- 项目阈值配置

### 5.4 Tool Registry

统一管理可被 Agent 调用的工具。

工具元数据：

- 工具名称
- 输入 Schema
- 输出 Schema
- 超时时间
- 重试策略
- 权限要求
- 是否有副作用
- 成本估算
- 版本

### 5.5 Decision Engine

负责生成结构化决策。

决策必须包含：

- `decision_type`
- `decision_value`
- `confidence`
- `reason`
- `evidence_refs`
- `alternative_options`
- `recommended_action`
- `human_review_required`

### 5.6 Human Review Gateway

当 Agent 无法可靠自动处理时，创建人工复核任务。

触发条件：

- 置信度低于阈值。
- 模型之间冲突。
- 人工标注和模型输出冲突。
- 涉及身份合并。
- 涉及批量资产回填。
- 涉及模型发布阻断。

### 5.7 Trace Recorder

记录 Agent 的完整执行轨迹，用于审计、复盘、评测和回放。

## 6. Agent 类型设计

### 6.1 Boundary Agent

目标：判断客户接待边界，处理切分、合并、边界冲突和串音导致的客户组误归属。

输入：

- VAD 区间
- ASR Segment
- Speaker Turn
- 时间间隔
- 地点和设备信息
- 声纹信息
- 事件标签
- 同门店不同销售 / 设备的重叠录音
- 串音候选
- 主录音候选

输出：

- 建议边界
- 合并建议
- 拆分建议
- 边界置信度
- 冲突原因
- 主录音 / 串音 / 重复收录判断
- 归属销售、设备、客户组建议
- 是否转人工

典型场景：

- 两段录音间隔短，疑似同一接待。
- 同一客户跨设备出现。
- VAD 分段过碎，需要合并。
- 多个客户组交错，需要拆分。
- 相邻工牌收到了同一段对话，需要判断主录音和串音。
- 展厅公共麦克风与销售工牌重复收录，需要避免重复计数。

### 6.1.1 Crosstalk Agent

目标：识别同一天、同门店、不同销售和不同收声设备之间的串音、重复收录和误归属。

输入：

- 同门店全天音频 Minimap
- 多销售 / 多设备泳道
- 同时间窗重叠录音
- ASR 相似度
- 声纹重叠
- 能量峰对齐
- 设备空间距离
- 销售排班和工牌绑定关系
- 事件标签和单据实体

输出：

- 疑似串音片段
- 主录音候选
- 串音候选
- 重复收录候选
- 归属销售建议
- 归属设备建议
- 是否从标签、单据关联、评测样本中降权或排除
- 是否需要人工确认

典型场景：

- 销售 A 工牌中听到销售 B 的接待。
- 两个相邻柜台同时出现相似 ASR 文本。
- 公共麦克风和个人工牌重复收录同一接待。
- 串音片段被错误关联到试驾单、报价单或订单。

### 6.2 Tagging Agent

目标：对 ASR、事件、业务上下文进行标签识别和解释。

输入：

- ASR 文本
- Speaker 角色
- 当前标签体系
- 标签样例
- negative examples
- 业务场景
- 历史标注

输出：

- 标签候选
- 标签时间范围
- 标签置信度
- 证据句子
- 冲突标签
- 建议人工确认项

典型场景：

- 识别“报价”“异议”“成交”“离店”。
- 判断“未问需求”“违规承诺”等质检风险。
- 找出低置信标签并转人工。

### 6.3 Conflict Resolver Agent

目标：解决模型、规则、人工标注之间的冲突。

冲突类型：

- 边界冲突
- 标签冲突
- 说话人冲突
- 声纹冲突
- 模型版本冲突
- 人工标注冲突

输出：

- 推荐结论
- 保留多个候选
- 转人工仲裁
- 重跑指定模型
- 加入 badcase

### 6.4 Voiceprint Agent

目标：辅助声纹确认、合并和拆分。

输入：

- speaker embedding
- 声纹库候选
- 历史出现轨迹
- 地点和班次信息
- 人工确认记录

输出：

- 匹配候选
- 合并建议
- 拆分建议
- 身份置信度
- 是否需要人工确认

约束：

- 声纹合并默认需要人工确认。
- 跨项目声纹匹配必须受权限控制。
- 客户声纹应遵守隐私和数据保留策略。

### 6.5 Review Triage Agent

目标：对待复核任务排序，让人工优先处理最有价值样本。

排序因子：

- 低置信
- 模型冲突
- 标签冲突
- 边界冲突
- 业务高价值
- 客户投诉相关
- 数据资产异常
- 评测集覆盖缺口

输出：

- 复核优先级
- 推荐标注员
- 预计处理时长
- 复核原因

### 6.6 Eval Judge Agent

目标：辅助评测、模型对比和质检样本判断。

输入：

- 标准答案
- 模型输出
- 人工评测结果
- 标签版本
- 指标口径

输出：

- 评测判断
- 错误类型
- 归因说明
- 是否加入 badcase
- 是否阻断发布

### 6.7 Badcase Analyst Agent

目标：分析 badcase 原因并给出修复建议。

输出：

- 错误归因
- 影响范围
- 相似样本
- 规则候选
- prompt 修复建议
- 模型重训建议
- 评测集补充建议

### 6.8 Asset Quality Agent

目标：诊断数据资产质量异常。

输入：

- 资产质量指标
- 生成记录
- 上游资产状态
- 下游依赖
- 失败日志
- 数据样本

输出：

- 异常原因
- 影响范围
- 是否需要回填
- 回填建议范围
- 是否需要人工确认

### 6.9 Insight Agent

目标：生成业务洞察并确保洞察可回到证据链。

输入：

- 指标趋势
- 标签分布
- 门店对比
- 模型质量变化
- 相关音频证据

输出：

- 洞察结论
- 证据片段
- 风险说明
- 推荐下钻路径
- 是否生成报告

### 6.10 Experience Agent

目标：根据用户角色、任务上下文、异常类型和系统偏好，推荐最合适的 UI 视图、动效、高亮和语言表达。

输入：

- 用户角色
- 当前页面
- 当前筛选条件
- 当前聚合路径
- 当前语言
- 当前皮肤
- 动效偏好
- 异常类型
- Agent 决策结果
- 证据链对象

输出：

- 推荐视图模式
- 推荐聚合路径
- 推荐高亮对象
- 推荐动效类型
- 推荐粒子层语义
- 推荐文案 key
- 是否需要降低动效
- 是否需要切换到高对比模式

典型场景：

- 发现同门店多设备重叠录音时，推荐切换到串音排查视图。
- 标注员进入低置信片段时，高亮 ASR 证据句、speaker turn 和相关单据字段。
- 算法团队查看模型下降时，推荐评测对比视图和 badcase 聚合路径。
- 管理员查看洞察大屏时，启用粒子数据风展示资产流、异常聚集和 Agent Trace。
- 用户切换语言后，保持播放位置、选区、上下文篮和当前视图不变。

## 7. 工具设计

### 7.1 音频处理工具

- `ffmpeg.probe`
- `ffmpeg.convert`
- `vad.segment`
- `audio.slice`
- `audio.merge`
- `silence.skip`

### 7.2 模型工具

- `asr.transcribe`
- `diar.identify`
- `voiceprint.embed`
- `voiceprint.match`
- `boundary.detect`
- `tagger.classify`
- `llm.judge`
- `insight.generate`

### 7.3 数据检索工具

- `audio.get_timeline`
- `audio.get_store_day_minimap`
- `audio.get_overlapping_devices`
- `audio.get_parallel_sales_lanes`
- `segment.get_neighbors`
- `speaker.get_history`
- `tag.search`
- `data.aggregate_by_dimension`
- `data.expand_group_node`
- `data.get_group_summary`
- `event.get_related_documents`
- `document.search`
- `crosstalk.detect_candidates`
- `crosstalk.compare_segments`
- `ui.get_user_preferences`
- `ui.get_view_state`
- `ui.get_i18n_context`
- `ui.get_theme_tokens`
- `badcase.search_similar`
- `asset.get_lineage`
- `eval.get_baseline`

### 7.4 写入工具

写入工具必须区分候选写入和正式写入。

候选写入：

- `suggestion.create`
- `annotation_candidate.create`
- `badcase_candidate.create`
- `asset_issue.create`

正式写入：

- `annotation.commit`
- `conversation.update_boundary`
- `tag.apply`
- `eval_sample.create`
- `badcase.create`
- `asset_backfill.create`
- `ui_view_recommendation.create`

正式写入默认需要权限和风险校验。

## 8. 核心智能工作流

### 8.1 单条音频智能处理流程

```text
Audio Input
  ↓
Preprocess
  ↓
VAD
  ↓
ASR
  ↓
Diarization
  ↓
Voiceprint Linking
  ↓
Crosstalk Candidate Detection
  ↓
Boundary Detection
  ↓
Cross-device Crosstalk Agent
  ↓
Boundary Agent
  ↓
Tagging Agent
  ↓
Conflict Resolver Agent
  ↓
Confidence Gate
  ↓
Human Review Gateway
  ↓
Output Commit
  ↓
Eval / Badcase / Asset Update
```

### 8.2 低置信处理流程

```text
模型输出
  ↓
置信度低于阈值
  ↓
Agent 判断原因
  ↓
可自动修复？
  ├── 是：调用工具重跑或生成候选结果
  └── 否：创建人工复核任务
  ↓
记录 Trace
  ↓
可选加入 badcase
```

### 8.3 冲突仲裁流程

```text
发现冲突
  ↓
收集上下文
  ↓
识别冲突类型
  ↓
Conflict Resolver Agent
  ↓
输出推荐结论
  ↓
低风险：生成候选或自动应用
高风险：转人工仲裁
  ↓
记录原因和证据
```

### 8.4 人工评测回流流程

```text
人工评测完成
  ↓
对比模型输出
  ↓
Eval Judge Agent 归因
  ↓
Badcase Analyst Agent 分析
  ↓
生成修复建议
  ↓
加入 badcase / 评测集 / 数据资产
  ↓
触发重评或回填
```

### 8.5 数据资产异常诊断流程

```text
资产质量下降
  ↓
Asset Quality Agent 收集上游和样本
  ↓
定位失败原因
  ↓
评估下游影响
  ↓
建议回填范围
  ↓
人工确认
  ↓
创建回填任务
```

## 9. 状态机设计

### 9.1 Agent Run 状态

- `pending`
- `context_building`
- `tool_calling`
- `deciding`
- `waiting_human`
- `completed`
- `failed`
- `cancelled`
- `expired`

### 9.2 Agent Decision 状态

- `candidate`
- `auto_applied`
- `human_required`
- `human_approved`
- `human_rejected`
- `superseded`
- `rolled_back`

### 9.3 风险等级

- `low`：只影响排序、提示、候选建议。
- `medium`：影响单条音频结果，需要可回滚。
- `high`：影响人工标注、声纹身份、批量回填、模型发布，必须人工确认。

## 10. 数据结构建议

### 10.1 Agent Run

```text
agent_run_id
tenant_id
project_id
workflow_run_id
agent_name
agent_version
status
risk_level
input_refs
output_refs
decision_id
trace_id
started_at
finished_at
created_by
```

### 10.2 Agent Decision

```text
decision_id
tenant_id
project_id
agent_run_id
decision_type
decision_value
confidence
reason
evidence_refs
alternative_options
recommended_action
human_review_required
status
created_at
```

### 10.3 Tool Call

```text
tool_call_id
tenant_id
project_id
agent_run_id
tool_name
tool_version
input_hash
input_refs
output_refs
status
latency_ms
cost
error_message
created_at
```

### 10.4 Human Review Task

```text
review_task_id
tenant_id
project_id
source_agent_run_id
review_type
priority
reason
evidence_refs
assignee
status
result
created_at
completed_at
```

## 11. UI 表达要求

Agentic 能力在 UI 上不应表现为“黑盒自动化”，而应表现为可解释的智能辅助。

### 11.1 调听标注页

展示：

- 低置信原因
- 冲突类型
- Agent 建议
- 证据句子
- 相邻片段
- 同门店多设备重叠录音
- 主录音 / 串音 / 重复收录判断
- 归属销售、设备、客户组建议
- 是否建议重跑
- 是否建议人工复核

操作：

- 接受建议
- 拒绝建议
- 修改后接受
- 转人工仲裁
- 加入 badcase
- 查看 Agent Trace
- 同步播放多路录音
- 标记主录音
- 标记为串音
- 标记为重复收录

### 11.1.1 数据管理页

展示：

- 当前聚合路径
- 时间 / 空间 / 事件 / 人物分层节点
- 每个聚合节点的低置信、串音、单据缺失、待复核摘要
- Agent 推荐的异常优先级
- 推荐下钻路径

操作：

- 一键切换到串音排查视图
- 展开 Agent 推荐节点
- 批量加入上下文篮
- 创建复核任务
- 保存推荐聚合视图

### 11.1.2 体验系统

展示：

- Experience Agent 推荐视图
- 推荐原因
- 证据来源
- 置信度
- 皮肤和语言偏好
- 动效和粒子层开关状态

操作：

- 接受推荐视图
- 忽略本次推荐
- 保存为个人默认视图
- 切换皮肤
- 切换语言
- 开启 / 关闭粒子层
- 降低动态效果

约束：

- Agent 不应自动强制切换语言或皮肤。
- Agent 不应在高风险操作时触发大面积动效。
- 粒子层只能表达数据语义，不能遮挡调听、标注、确认、删除等核心操作。

### 11.2 评测中心

展示：

- 错误类型归因
- 相似 badcase
- 修复建议
- 是否发布阻断
- 重评建议

### 11.3 数据资产中心

展示：

- 资产异常原因
- 影响下游
- 推荐回填范围
- 预计影响数据量
- Agent 诊断证据

### 11.4 洞察页

展示：

- 洞察结论
- 支撑证据
- 置信度
- 相关标签
- 相关音频片段
- 推荐下钻路径

## 12. 评测与质量指标

Agentic 能力必须被评测，不能只看主观效果。

### 12.1 决策质量指标

- 自动通过准确率
- 人工接受率
- 人工修改后接受率
- 人工拒绝率
- 冲突解决准确率
- 低置信召回率
- badcase 命中率

### 12.2 系统效率指标

- 人工复核减少比例
- 平均复核耗时
- 自动重跑成功率
- Agent 平均延迟
- 工具调用失败率
- 单小时音频处理成本

### 12.3 安全和可控指标

- 越权拦截次数
- 高风险动作人工确认率
- 回滚次数
- 错误自动应用次数
- Trace 完整率

## 13. MVP 范围

### 13.1 第一阶段

第一阶段只做最小可用 Agentic 能力：

- 低置信识别和排序。
- 基础 Boundary 建议。
- 基础标签建议。
- 冲突提示，不自动仲裁高风险冲突。
- 人工复核任务创建。
- Agent Trace 记录。
- 加入 badcase。
- 读取用户语言、皮肤和减少动态效果偏好，但不做自动体验推荐。

暂不做：

- 自动批量回填。
- 自动声纹合并。
- 自动模型发布阻断。
- 复杂洞察生成。
- 自动切换皮肤、语言或粒子层。
- 全链路 Trace Replay。

### 13.2 第二阶段

- Conflict Resolver Agent。
- Voiceprint Agent 候选推荐。
- Eval Judge Agent。
- Badcase Analyst Agent。
- 相似 badcase 检索。
- 模型重评建议。
- Agent 决策质量看板。
- Experience Agent 基础视图推荐。
- 智能推荐聚合路径和异常下钻路径。

### 13.3 第三阶段

- Asset Quality Agent。
- Insight Agent。
- 自动诊断。
- 自动生成回填建议。
- 标签体系修复建议。
- 全链路 Trace Replay。
- 多 Agent 协作和策略评测。
- 个性化体验推荐。
- 粒子化 Agent Trace、资产血缘和异常聚集展示。

## 14. 落地建议

第一版不要追求“全自动 Agent”。正确落地路径是：

1. 先把 Agent 作为低置信样本的排序器和解释器。
2. 再让 Agent 生成候选边界、候选标签和候选 badcase。
3. 然后接入人工确认，形成接受、拒绝、修改后的反馈数据。
4. 最后再开放部分低风险自动应用。

这样可以先建立可观测、可评测、可回滚的智能化闭环，再逐步提高自动化比例。

平台真正有价值的 Agentic 能力不是“会调用很多工具”，而是能在音频、时间、地点、人物、事件、标签、评测和数据资产之间保持连续上下文，并把每一次智能判断沉淀成可复盘、可评测、可回流的数据资产。

## 15. 当前原型对齐：Agent-Human-Dagster 闭环

当前原型已经把 Agentic 能力从“辅助建议”扩展到任务配置、调听证据、标签治理、业务洞察、评测和数据资产。后续智能化设计以 `当前原型评审与优化计划.md` 为对齐基线。

### 15.1 Agentic 主线

统一智能闭环为：

输入范围 -> Agent 智能抽取/优化 -> 人工修改/确认 -> 效果评价 -> 自动化等级判断 -> 执行/回写 -> 发布/回流

任何 Agent 行为都必须落入闭环中的一个环节，不允许出现“建议展示了但无法操作”“点击了但不知道写到哪里”“智能体直接覆盖线上结果”的情况。

### 15.2 输入对象

每一次智能运行必须有明确输入。

必填输入：

- 租户；
- 项目；
- 数据范围；
- 门店 / 人员 / 设备 / 日期；
- 样本集；
- 当前标签版本；
- 候选标签版本；
- 模型版本；
- Prompt 版本；
- 任务模板；
- 资产分区。

可选输入：

- 置信阈值；
- 抽取策略；
- 是否影子运行；
- 是否允许低风险自动接受；
- provider；
- Audio Intelligence Service 参数组；
- run tags。

Dagster 映射输入：

- job_name；
- asset_selection；
- partition_key；
- run_config；
- tags；
- retry_policy；
- io_manager；
- asset_check_selection。

### 15.3 Agent 输出对象

Agent 不能直接写线上业务结果。Agent 输出只能是以下对象之一：

- LabelCandidate；
- BoundaryCandidate；
- SpeakerCandidate；
- EventLinkCandidate；
- PromptSuggestion；
- RuleCandidate；
- ChangeSetDraft；
- EvalRunDraft；
- HumanReviewTask；
- BackfillDraft；
- DagsterRunDraft；
- InsightFact；
- TraceRef。

高风险动作必须进入 Human Review Gateway。

高风险动作包括：

- 覆盖线上标签；
- 批量接受候选；
- 发布标签版本；
- 发布 Prompt 版本；
- 批量回填资产；
- 修改高风险阈值；
- 替换 Audio Intelligence provider；
- 将候选结果回写外部业务系统。

### 15.4 Agent 提升与人工修改区分

所有变更必须标记来源：

- Agent 建议；
- 人工修改；
- 系统门禁；
- 评测回流；
- badcase 回流；
- 外部系统回写。

Agent 建议卡片必须展示：

- 建议内容；
- 证据句或证据片段；
- 置信度；
- Trace；
- 预期提升；
- 风险；
- 影响资产；
- 可执行动作。

人工修改卡片必须展示：

- 修改前；
- 修改后；
- 修改人；
- 修改理由；
- 是否覆盖 Agent 建议；
- 是否触发重新评测；
- 是否进入 ChangeSet。

### 15.5 自动化等级

标签、Prompt、任务和回填都必须有自动化等级。

| 等级 | 含义 | 写入权限 |
| --- | --- | --- |
| L0 | 只展示建议 | 不写候选 |
| L1 | Agent 写候选 | 人必须逐条确认 |
| L2 | 低风险候选可批量接受 | 高风险进 Human Loop |
| L3 | 通过评测门禁后自动灰度 | 可生成灰度发布草稿 |
| L4 | 稳定任务可自动发布 | 保留回滚、抽检和审计 |

升级条件必须可见：

- 评测集通过；
- Human Loop 完成；
- 冲突率低于阈值；
- JSON 合法率达标；
- 成本和延迟未超阈值；
- 关键标签无回退；
- 影响资产确认；
- 可回滚版本存在。

### 15.6 效果评价与发布门禁

效果评价和发布门禁共享同一个运行详情。

统一对象为 LabelOptimizationRun，包含：

- input；
- changeSet；
- metrics；
- gateChecks；
- decision；
- automationLevel；
- dagsterRunDraft；
- traceRefs。

效果评价指标包括：

- Precision；
- Recall；
- F1；
- 人工接受率；
- 误报率；
- 漏报率；
- 冲突率；
- JSON 合法率；
- 成本；
- 延迟。

每个指标必须说明提升来源：

- Prompt 修改；
- 规则修改；
- 人工修正；
- 样本回流；
- 阈值调整；
- provider 切换；
- Audio Intelligence 参数优化。

发布门禁必须消费效果评价和 Human Loop 状态。未通过时只能继续优化、送人审或保持影子运行；通过后才允许灰度发布或发布候选版本。

### 15.7 Audio Intelligence Service 的 Agentic 边界

Audio Intelligence Service 不是 Tagger LLM。它包含 VAD、Diar、ASR、噪音、音乐、静音和质量评分等音频智能能力。

Agentic Runtime 可以：

- 选择 provider；
- 推荐参数组；
- 识别低质量输入；
- 解释 VAD / Diar / ASR 失败原因；
- 对比不同 provider 的延迟、成本、质量；
- 生成重跑建议；
- 把失败样本加入 badcase；
- 创建回填草稿。

Agentic Runtime 不应：

- 直接替换线上 provider；
- 直接覆盖 ASR 结果；
- 在无评测和无审批时批量重跑高风险资产；
- 把服务端点和认证密钥暴露给业务页面。

### 15.8 Dagster 对接原则

UI 业务层仍使用业务语言：

- 标签优化运行；
- 评测运行；
- 回写任务；
- 资产生成；
- 调度策略；
- 回填任务。

配置和诊断层展示底层映射：

- JobDefinition；
- AssetSelection；
- PartitionsDefinition；
- RunRequest；
- AssetMaterialization；
- AssetCheck；
- Sensor；
- Schedule；
- run tags。

一次 Agentic 运行必须能生成可审计的运行草稿：

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
    "trace_id": "trace_20250526_122718",
    "human_loop": "required"
  }
}
```

运行结果必须回写到：

- 标签候选；
- 评测结果；
- Human Loop；
- 资产血缘；
- badcase；
- 洞察事实；
- 发布门禁。

### 15.9 UI 表达补充

所有 Agentic UI 必须同时回答：

1. 输入是什么？
2. Agent 做了什么？
3. 人改了什么？
4. 改完效果如何？
5. 自动化等级是什么？
6. 底层如何执行？
7. 结果写到了哪里？
8. 能否回滚？

如果页面只能展示“智能建议”，但不能解释上述问题，则不算完整 Agentic 交互。

## 16. Agentic 后端执行设计

当前原型中的 Agent 能力已经覆盖任务配置、调听证据、标签治理、知识库、评测、洞察和数据资产。后端实现必须把 Agent 行为收敛为可审计、可回放、可评测的运行对象，而不是让 Agent 直接写线上业务结果。

### 16.1 运行对象

统一对象：

- `AgentRun`：一次智能运行，记录输入范围、状态、模型、工具、成本、耗时和 Trace。
- `ToolCall`：Agent 调用工具、检索、模型服务或业务 API 的记录。
- `TraceRef`：跨页面追踪引用，绑定 tenant、project、task、asset、evidence、label、eval。
- `ChangeSetDraft`：可人工确认的变更草稿。
- `HumanReviewTask`：需要人工处理的高风险候选或阻断项。
- `EvalRunDraft`：由 Agent 创建但尚未发布的评测运行草稿。
- `BackfillDraft`：由 Agent 建议的资产回填或重跑草稿。

Agent 输出只能落到候选、草稿、人审任务、评测运行、回填草稿或洞察事实，不允许直接覆盖线上标签、任务版本、模型 provider、资产结果或外部平台回写。

### 16.2 执行流程

标准流程：

1. 创建 `AgentRun`，写入 MySQL，状态为 `pending`。
2. 锁定输入范围：tenant、project、store、date、model_version、label_version、task_version、asset_partition。
3. 从 MySQL 读取业务上下文：任务配置、音频会话、标签版本、评测集、资产状态、权限和审计策略。
4. 从 Qdrant 召回知识、证据或相似样本：SOP、FAQ、产品资料、标签样本、证据包、badcase、可选声纹 embedding。
5. 调用工具或模型服务：Audio Intelligence Service、Tagger、LLM Judge、规则引擎、导出服务。
6. 写候选结果：LabelCandidate、BoundaryCandidate、SpeakerCandidate、EventLinkCandidate、PromptSuggestion、InsightFact。
7. 写 Trace：保存输入、召回、工具调用、模型输出、人工动作和最终状态。
8. 根据风险进入 Human Loop、评测运行或发布门禁。
9. 通过门禁后由明确动作发布、回填或外部回写。

所有阶段必须返回统一状态：`pending`、`running`、`success`、`failed`、`blocked`、`cancelled`。

### 16.3 MySQL 与 Qdrant 边界

MySQL 是权威状态库，负责：

- AgentRun、ToolCall、TraceRef、候选、草稿、人审任务和评测运行。
- 租户、项目、权限、任务、资产、标签、音频会话、边界和审计。
- 发布门禁、自动化等级、审批状态、回填状态和外部回写回执。

Qdrant 是召回索引，负责：

- 知识库语义切片召回。
- 证据片段相似召回。
- 标签正负例和冲突样本召回。
- badcase 近似样本召回。
- 可选声纹 embedding 候选召回。

Qdrant payload 必须保存可回跳字段：tenant_id、project_id、source_id、asset_key、trace_id、version、evidence_id、label_version。Qdrant 不保存最终业务状态，不作为审批、发布和审计的依据。

### 16.4 Audio Intelligence Service 后端接入

Audio Intelligence Service 在后端作为可注册服务族，不是单个 ASR 服务。能力包括：

- VAD：输出有声片段和静音边界。
- Diar：输出说话人轨道和 speaker_turns。
- ASR：输出转写文本、时间戳、词级置信度和热词诊断。
- 音频质量：输出 SNR、噪音、音乐、静音、串音、设备异常。
- 声纹：输出 enrollment/candidate embedding 和质量评分。

ASR 不负责最终说话人身份。说话人身份由 Diar、声纹、工牌、设备、业务上下文和人工确认共同形成。Agent 可以解释冲突、推荐重跑或创建人审任务，但不能直接覆盖身份结果。

### 16.5 知识库 Agent

知识库 Agent 使用 MySQL + Qdrant 协同工作：

- MySQL 读取知识源、切片、索引版本、质量门禁、标签版本和效果指标。
- Qdrant 召回 SOP、FAQ、产品资料、标签样本、音频证据包和 badcase。
- Agent 生成候选标签、缺口诊断、召回质量解释、报告草稿和补样建议。
- 高风险候选进入 HumanReviewTask。
- 通过评测和门禁后，才能进入标签版本、知识索引版本或报告资产。

知识库 Agent 的输出必须能回到当前原型中的知识消费路径：知识源 -> 语义切片 -> 实体 / 标签 -> 召回索引 -> 消费场景。

### 16.6 开发验收

- 每次 AgentRun 都能在 MySQL 查到输入、状态、输出、Trace 和人工动作。
- 每条 Qdrant 召回结果都能回跳到 MySQL 业务对象和对象存储证据。
- 高风险动作必须生成 HumanReviewTask 或 blocked 门禁。
- 前端所有 Agent 按钮都能看到 pending、success、failed、retry 或 blocked。
- Agent 不能直接调用外部回写接口；只能创建回写草稿或通过门禁后的后端任务执行。
