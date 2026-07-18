# Fixtures 与种子数据

本文档定义一期联调用固定样本。目标是让前端原型、BFF、数据库、任务运行、调听、人审、标签、评测、洞察和资产模块使用同一组对象，不再各自造数据。

当前种子选择“汽车门店销售质检”只是为了让跨模块对象关系具体且可重复，不是 Auris Flow 的产品边界。平台内核必须支持由租户/项目声明的会议、客服、销售、售后、培训等场景包；领域标签、单据、指标和发布门禁由项目的 Domain Profile 决定。

## 0. 数据来源与隐私

- 本 fixture 中的组织、门店、人员、会话 ID、文件名、时间、金额、转写、单据和证据均为合成演示数据，不对应真实客户、员工或业务记录。
- E2E 音频由测试脚本生成确定性 PCM/WAV；仓库不分发真实录音、声纹、转写或客户附件。
- 贡献者不得把生产导出、真实客户截图、日志、对象存储文件或可逆脱敏数据加入 fixture。新增样本必须能够说明生成方式，并通过 secret/PII 与 Git 历史扫描。
- 首次公开发布前，项目所有者仍须在发布清单中确认数据来源和授权链；本声明不能替代权利确认。

### 0.1 公开评测音频

- 公开数据集目录以 `public-audio-datasets-v0.1.json` 为唯一仓库内注册表；仓库只保存来源、许可证、split、引用和完整性状态，不保存第三方音频、转写、RTTM 或归档。
- AliMeeting `SLR119` 作为普通话多说话人会议场景的 ASR CER、说话人 DER 和 VAD 边界基线。它只能证明声明的通用能力与会议场景能力，不能替代当前项目 Domain Profile 对应的领域标签、角色、事件、单据和业务指标验收。
- 数据许可证与基线代码许可证分别记录。根目录 Apache-2.0 不覆盖 CC BY-SA 4.0 的 AliMeeting 数据及其受许可约束的衍生材料。
- 上游归档 SHA-256 未由项目所有者固定前，split 必须保持 `pending-owner-lock`、`download_enabled=false`、`ci_enabled=false`。网络可访问、HTTP 206 或对象存储 ETag 都不能替代完整归档 SHA-256。
- Eval/Test split 禁止用于训练、Prompt/词表调参或阈值选择。模型训练来源未知、会话/说话人 split 交叉或官方 scorer 口径未固定时，结果只能标记为 `blocked`，不能进入发布门禁。
- 公开音频只能进入受控本地对象存储，通过租户/项目授权和 BFF HTTP Range 播放；不得进入 Git、公共 CI artifact、日志或公开 bucket。

## 1. 环境

- 种子环境：`local`、`dev`、`staging-demo`。
- 禁止在生产直接执行 demo seed。
- 所有 ID 使用稳定字符串，便于截图、测试和契约断言。

## 2. 基础上下文

| 对象 | ID | 名称 |
| --- | --- | --- |
| Tenant | `aurora_auto` | 极光汽车 |
| Project | `sales_qa` | 销售话术质检 |
| Store | `BJ-AURORA-001` | 北京区域 / 极光中心店 |
| Store | `SH-JA-002` | 上海嘉安门店 |
| User | `u_admin_001` | 项目管理员 |
| User | `u_annotator_001` | 质检运营 |
| User | `u_annotator_002` | 质检运营 B（盲审校准第二评审） |
| User | `u_sales_a` | 销售A |
| Service Account | `svc_agent` | Agent 服务账号 |
| Service Account | `svc_worker` | Worker 服务账号 |

## 3. 角色与权限种子

必须写入：

- `tenant_admin`
- `project_admin`
- `business_operator`
- `annotator`
- `label_governor`
- `review_arbitrator`
- `data_integration_engineer`
- `model_engineer`
- `asset_manager`
- `auditor`
- `agent_service`

权限以 `rbac-matrix.md` 为准。服务账号禁止 `publish`、`approve_high_risk`、`export_sensitive`、`manage_secrets`。

## 4. 任务与运行种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| TaskType | `task_sales_quality` | 销售话术质检任务类型 |
| CanvasVersion | `canvas_v3_2_1` | 多平台证据数据流 |
| TaskVersion | `task_version_v3_2_1` | 已发布版本 |
| TaskRun | `task_run_20250526_122300` | 2025-05-26 中午分区运行 |
| Partition | `aurora_auto/BJ-AURORA-001/2025-05-26/12` | 门店日期小时分区 |

默认运行状态：

- `task_run_20250526_122300`: `success`
- `task_run_20250526_123100_retry`: `blocked`
- `task_run_20250526_140000_backfill`: `pending`

## 5. 音频与调听种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| AudioSession | `S20250526-000128` | 销售A接待会话 |
| AudioSession | `S20250526-000131` | 静安体验店低置信 ASR 复核会话 |
| Recording | `A-1001_20250526_122300.wav` | 主录音 |
| Recording | `B-2001_20250526_122812.wav` | 串音候选 |
| Recording | `GZ-U882_20250525_164020.wav` | 未知声纹样本 |
| Recording | `SH-A012_20250526_091500.wav` | 低置信 ASR 对应原始录音 |
| Boundary | `boundary_s128_v1` | 12:23:30-12:34:34 |
| EvidencePack | `AF-128` | 金额冲突证据 |
| EvidencePack | `AF-129` | 串音候选证据 |
| EvidencePack | `AF-130` | 产品咨询证据 |
| EvidencePack | `AF-131` | `S20250526-000131` 的低置信 ASR 证据，供 `hrt_low_confidence_draft` 独占引用 |

必须包含轨道样本：

- VAD 有声段。
- 说话人：销售A、客户、展厅麦克风。
- ASR 片段：欢迎、报价、异议、试驾承接。
- 实体标签：车型、报价金额、优惠幅度、业务单据。
- 质检标签：金额冲突、低置信、串音疑似。

## 6. 业务单据与事件种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| BusinessDocument | `BJ-041` | 报价单 |
| BusinessDocument | `SJ-028` | 试驾单 |
| EventLink | `event_quote_122718` | 报价金额事件 |
| EventLink | `event_testdrive_140000` | 试驾预约事件 |

关联规则：

- `AF-128` 关联 `BJ-041` 和 `event_quote_122718`。
- 报价单金额与 ASR 报价存在差异，触发金额冲突。
- 事件时间可与音频窗口偏移，必须保留 `occurred_at` 和 `evidence_window`。

## 7. 标签与 Prompt 种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| LabelTaxonomy | `taxonomy_sales_qa` | 汽车销售质检标签体系 |
| LabelVersion | `label_v1_8_4` | 当前线上版本 |
| LabelVersion | `label_v1_9_0_rc2` | 候选版本 |
| PromptVersion | `prompt_quote_v3` | 报价金额抽取 Prompt |
| LabelCandidate | `cand_af128_amount_conflict` | 金额冲突候选 |
| LabelCandidate | `cand_af129_crosstalk` | 串音候选 |

标签任务：

- 报价金额
- 试驾邀约
- 价格异议
- 成交意向
- 串音疑似

## 8. 人审与 badcase 种子

| 对象 | ID | 队列 | 状态 |
| --- | --- | --- | --- |
| HumanReviewTask | `hrt_amount_001` | `amount_conflict` | `pending` |
| HumanReviewTask | `hrt_crosstalk_001` | `crosstalk_candidate` | `pending` |
| HumanReviewTask | `hrt_low_conf_001` | `low_confidence` | `pending` |
| Badcase | `badcase_quote_001` | 标签规则 | `open` |
| Badcase | `badcase_prompt_001` | Prompt 优化 | `open` |
| Badcase | `A-4107` | `asr-hotword` / 星越L → 星月L | `pending-backflow` |

`A-4107` 绑定 `AF-131`、不可变词包版本 `hwpv-auto-sales-v1-8`、受控证据对象 `storage_badcase_a_4107_evidence`、标准词“星越L”、错误类型 `misrecognition`、人工确认证据和治理根链 `trace_hotword_pack_auto_sales`。该证据对象在当前租户/项目内为 `verified`，`source_type=asr_hotword_evidence` 且 `source_id=A-4107`。所有新增 ASR 热词 Badcase 都必须显式提交词包版本和完整可用的 StorageObject 引用；展示路径不能替代权威对象 ID。原型里错误的 `EVP-asr-131 → B-2031` 深链必须统一改为按 `badcase_id=A-4107` 恢复后端对象，不依赖页面本地 state。

## 9. ASR 热词治理种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| HotwordPack | `hotword_pack_auto_sales` | 汽车销售热词包，`zh-CN / auto-sales` |
| HotwordPackVersion | `hwpv-auto-sales-v1-8` | 已发布不可变 `v1.8`，冻结 `compiled_provider=auris-audio-stack` 与 provider artifact，逻辑包当前版本 |
| HotwordVersionItem | `hotword_item_xingyue_l` | 星越L，显式别名“星越 L”，`source_type=badcase`，来源 A-4107 |
| HotwordVersionItem | `hotword_item_yinhe_e8` | 银河E8，显式别名“银河 E8” |
| HotwordVersionItem | `hotword_item_lynk_08` | 领克08，显式别名“领克 08” |
| HotwordMetricSnapshot | `hotword_metric_20250526_xingyue_l` | 上海门店、`audio-v2.3.1`、人工确认证据的易错快照 |
| RunRecord | `hotword_analysis_seed_20250526` | 已完成热词分析，物化统计和 A-4107 |

种子版本必须满足：

- 只包含非敏感产品/车型词；客户姓名、手机号、车牌和 VIN 不得进入 fixture。
- 版本响应使用 `pack_id/version_id/item_id/resource_version`；跨域 Audio Intelligence 请求使用 `hotword_pack_version_id`。
- `content_sha256`、manifest 对象引用、provider 编译产物、模型负责人和项目管理员均固定，便于验证发布血缘。
- 旧 `hotwords_ref="汽车销售热词包 v1.8"` 仅作为历史 TaskVersion 只读映射，不再用于 seed 新写入。

## 10. 评测种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| EvalDataset | `evalset_quote_risk_v12` | 报价风险回归集 |
| EvalDataset | `evalset_boundary_v8` | 完整对话边界集 |
| EvalDataset | `evalset_prompt_regression_v3` | Prompt 回归集 |
| EvalRun | `evalrun_label_v190_shadow` | v1.8.4 vs v1.9.0-rc2 |
| EvalDataset | `evalset-asr-hotword-v1` | 锁定的 40 条 ASR 热词回归集 |
| EvalRun | `evalrun_hotword_v18_seed` | v1.7 vs v1.8，影子复测通过发布门禁 |

指标默认值：

- 综合得分：`91.2`
- 打标 F1：`88.6`
- Prompt 候选收益：`+3.8`
- badcase 回流：`64`
- 热词可信出现：`40`，词项：`3`
- 易错率相对下降：`25%`，召回提升：`4pp`
- 误增强率增幅：`0.4pp`，CER/WER 退化：各 `0.1pp`
- 下游 F1 退化：`0.2pp`，P95 延迟和分钟成本增幅：各 `4%`

## 11. 资产与洞察种子

| 对象 | ID | 说明 |
| --- | --- | --- |
| DataAsset | `asset_audio_raw` | `auris/audio/raw_recordings` |
| DataAsset | `asset_label_event_tags` | `auris/label/event_tags` |
| DataAsset | `asset_doc_links` | `auris/events/document_links` |
| DataAsset | `asset_eval_metrics` | `auris/eval/quality_metrics` |
| InsightMetric | `metric_reception_quality` | 接待转化质量指数 |
| InsightReport | `report_daily_20250526` | 经营日报 |

洞察北极星指标：

```text
接待转化质量指数 =
有效接待率 30%
+ 成交推进率 30%
+ 报价一致率 20%
+ 风险反向分 20%
```

## 12. 对象存储与 Qdrant 引用

对象存储引用格式：

```text
tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/A-1001_20250526_122300.wav
tenants/aurora_auto/projects/sales_qa/evidence/AF-128/evidence_pack.json
tenants/aurora_auto/projects/sales_qa/reports/report_daily_20250526.pdf
```

Qdrant payload 必填：

```json
{
  "collection": "evidence_segments",
  "tenant_id": "aurora_auto",
  "project_id": "sales_qa",
  "asset_key": "auris/label/event_tags",
  "source_type": "evidence_pack",
  "source_id": "AF-128",
  "version": "evidence-index-v1.8.4",
  "trace_id": "trace_20250526_122718",
  "evidence_id": "AF-128",
  "label_version": "v1.8.4",
  "audio_session_id": "S20250526-000128",
  "start_ms": 277000,
  "end_ms": 292000
}
```

其中 `source_id` 用于回跳 MySQL 权威对象，`version` 固定当前索引版本，`label_version` 固定标签口径，`start_ms/end_ms` 让召回结果能回到调听时间窗。Qdrant 不保存审批、发布、权限或最终业务状态。

## 12. 种子验收

- 前端首页、数据、调听、标签、洞察、评测、资产页面能使用同一批 ID 下钻。
- 每个核心模块至少有 3 条列表数据和 1 条详情数据。
- 至少包含一个 `success`、一个 `pending`、一个 `blocked`、一个 `failed` 样本。
- 人审决策、回填、报告生成、外部回写可以用固定样本触发。
