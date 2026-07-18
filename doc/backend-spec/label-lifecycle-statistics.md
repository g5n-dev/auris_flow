# 标签生命周期与统计口径 ADR

> 决策状态：Accepted；实现按 Expand → Dual-write → Backfill/Shadow → Read switch → Contract 分阶段交付
> 决策日期：2026-07-18
> 适用范围：Label Governance、Listening、Insight & Reporting、Evaluation、Data Asset、Release

## 1. 决策摘要

Auris Flow 采用“不可变标签版本 + 不可变事实 + 环境激活指针 + 不可变映射包 + 不可变指标快照”的闭环：

1. 所有自动打标、人工确认、聚合事实、标签派生指标和报告都必须追溯稳定 `label_id` 与不可变 `label_version_id`。
2. LabelVersion 制品状态与环境生效状态分离。生产受理由 `ReleaseBundleHead` 决定，不能只检查 `LabelVersion.status`。
3. 历史事实不可改写。修正通过新 Fact revision 与 supersedes 链完成；标签废弃不修改历史 Observation、Aggregate、Fact、MetricResult 或 Report。
4. 跨版本分析只允许 `native`、`normalized`、`recomputed` 三种显式口径，不允许可空版本触发静默混算。
5. `normalized` 冻结服务端编译且已发布的 `mapping_bundle`；`recomputed` 冻结已审批的目标版本 FactSet/Asset Manifest。
6. 报告只引用已物化的 `metric_result_ids + metric_scope_sha256`，不能在生成报告时重新解释查询。

本 ADR 冻结目标契约，不声明所有运行时端点已经实现。实现状态以 migration、FastAPI route、测试和真实栈证据为准。

## 2. 领域对象与权威边界

| 对象 | 权威职责 | 不变量 |
| --- | --- | --- |
| `LabelNode` | 稳定标签身份 | 展示名变化优先保持 `label_id`，名称不能作为统计 join key。 |
| `LabelVersion` | 完整标签定义制品 | published 后只读；deprecated/archived 也不能编辑。 |
| `LabelVersionItem` | 版本内标签快照 | 仅表达 active/retired/pending-configuration；跨版本关系不写回旧项。 |
| `ReleaseBundleHead` | 每个 tenant/project/environment 当前生产 Bundle | generation 单调递增；运行受理事务锁定并冻结 Head。 |
| `LabelMappingVersion` | 单 source version → target version 的不可变 edge | 每个源 active item 有且仅有一个 disposition。 |
| `LabelMappingBundle` | 多源版本到单目标版本的不可变编译闭包 | 冻结 edge IDs、路径、compiler version、canonical SHA；发布后不可重编。 |
| `LabelObservation` | 模型或规则原始断言 | append-only，绑定版本、证据、hash 与 Trace。 |
| `LabelFact` | 某逻辑断言的权威 revision | append-only；修正生成新 revision，旧行不得 UPDATE/DELETE。 |
| `LabelFactSet` / `FactSet` | 一整套可见事实 Manifest | 批量重算按单 Head 原子晋级，禁止循环逐 Fact 切换。 |
| `MetricResult` | 已物化指标快照 | payload、scope、source manifest 与 content hash 不可变。 |
| `InsightReport` | 指标快照引用制品 | 冻结 metric result IDs 与 scope hash，不拥有第二套计算口径。 |

MySQL 强表是权威源。Redis 仅缓存包含完整 scope SHA 的投影；Qdrant 仅用于证据召回解释；Dagster 仅执行冻结 Manifest，不定义业务语义。

## 3. 必须分离的状态

### 3.1 LabelVersion artifact lifecycle

```text
draft -> candidate -> validated -> locked -> evaluating
  -> gate_blocked/review_required -> approved -> published -> deprecated -> archived
```

- `published` 表示制品可被 Release Bundle 引用，不代表某环境已激活。
- `deprecated` 表示不再用于新部署；历史查询、审计和重放仍可读取。
- replacement、deprecation reason、artifact timestamps 属于制品；环境 `effective_at/deadline` 不写入全局制品状态。
- 现存 `gray_releasing/rollback_pending/rolled_back` 在兼容期只读映射为 artifact + environment activation 历史；S3 收敛后不得继续写入 LabelVersion。

### 3.2 Release activation lifecycle

```text
pending -> active -> draining -> inactive
                    \-> rolled-back
```

- activation lifecycle 由 ReleaseDeployment、ReleaseCommand、`ReleaseBundleHead` 和 append-only activation ledger 表达。
- 新运行创建必须在同一事务锁定当前 Head，重验 generation、deployment ACK、bundle SHA 和 `label_version_id`，再写冻结运行 Manifest。
- draining 后旧 Head 不再受理新运行；已受理运行按冻结 Manifest 在 deadline 前完成，或进入显式 cancelled/deadline-expired。
- LabelVersion 只有在所有受保护环境都不再 active/draining 引用时才可 deprecated。

### 3.3 Version item 与 Fact

版本项只表达版本内状态。`identity/rename/replace/merge/retire/split-recompute` 是跨版本 mapping relation。

Fact superseded 表示“同一逻辑断言被新证据修正”，不表示“标签被废弃”。标签退出不能改变旧 Fact 状态。

## 4. 双时态与查询截止点

| 时间 | 含义 | 使用方式 |
| --- | --- | --- |
| `occurred_at` | 业务事实发生时间 | 业务分桶、标签适用期、晚到事实判断。 |
| `recorded_at` | 平台记录 Fact revision 的服务端时间 | as-of revision 解析、审计“何时知道”。 |
| `effective_at` | 某环境 activation 开始时间 | 生产流量切换，不替代业务发生时间。 |
| `fact_as_of` | 指标允许看到的最大 `recorded_at` | 冻结可重现边界。 |

- 晚到事实按 `occurred_at` 进入历史业务桶，但只出现在 `fact_as_of >= recorded_at` 的新快照中。
- 已物化 MetricResult/Report 不因晚到事实、人工修正、重跑或标签废弃而变化。
- 同一历史区间重算必须生成新的 MetricResult ID、fact cutoff、source manifest SHA 和 content SHA。

## 5. 三种统计口径

### 5.1 native

- 按事实产生时的 `label_version_id + label_id` 统计。
- 多个源版本分区返回，不能静默合成单值。
- 用于审计、历史报表、模型质量和版本差异诊断。

### 5.2 normalized

- 多个源版本通过已发布 `mapping_bundle` 归一到单一 `target_label_version_id`。
- 冻结 source version set、target、bundle ID/SHA、FactSet generation、`fact_as_of`、metric definition versions、时区、周期边界和分母。
- 只允许 bundle 中的编译路径；客户端不能临时上传 edges 或自报 comparable。
- coverage 不完整、路径遇到 split/retire、reducer 不适用或语义不兼容时返回 `partial` 或 `structural-break`。

### 5.3 recomputed

- 从原始证据按目标版本重新抽取、聚合或人工复核。
- 适用于 split、定义实质变化、证据规则变化和正式历史回填。
- 只消费完整、经自然人批准并由 FactSet/Asset Manifest Head 原子晋级的资产。
- 候选重算 namespace 在晋级前不能被生产查询读取。

## 6. Mapping 与可比性

Mapping `compatibility` 固定为 `exact/metric-dependent/structural-break/not-applicable`；它描述 edge 的语义兼容性，不等同于某个指标快照的 `comparability_status`。一对一关系不能自动得到 exact，merge 默认 metric-dependent。

| relation | 自动处理 | 默认可比性 |
| --- | --- | --- |
| `identity` | stable label ID 与语义 hash 一致时连续 | comparable |
| `rename` | 仅 display name/alias 变化且其他语义 hash 一致 | comparable，否则 structural-break |
| `replace` | 一对一只描述基数，compatibility 独立审批 | 默认 structural-break |
| `merge` | 指标目录声明 grain、lineage key、time bucket 与 reducer | 按 metric family 判定 |
| `retire` | 无 target，原生历史保留 | normalized 为 coverage-gap/structural-break |
| `split-recompute` | 禁止比例分摊或多路复制 | 必须 recomputed |

### 6.1 Merge 去重

禁止固定按 subject 去重。归一化键为：

```text
metric grain + event/fact lineage + time bucket + target_label_id
```

同一主体同一事件的多个源标签可按 presence/distinct reducer 折叠；同一主体不同事件必须保留。数值、枚举、时长指标没有已审批 reducer 时直接 structural-break。

### 6.2 多跳 Bundle

V1→V2→V3 不能在查询时动态追最新映射。compiler 冻结 source set、target、edge version IDs、拓扑顺序、compiled paths、compiler version 与 canonical SHA。路径遇到 split/retire 必须中断，不能绕行。

### 6.3 比较不变量

同比/环比只有 target version、mapping bundle ID/SHA、metric definition versions、FactSet generation、`fact_as_of` 规则、timezone、period boundary 和 denominator definition 均一致时才是 `comparable`。否则返回 `partial` 或 `structural-break`，UI 不显示普通涨跌箭头。

## 7. 0、N/A 与 coverage-gap

| 结果 | 条件 | 展示 |
| --- | --- | --- |
| 数值 0 | 标签在窗口内适用、coverage 完整、分母有效但无命中 | `0` |
| `not-applicable` | 窗口完全位于标签适用期外，或指标目录声明标签版本不适用 | `N/A` + reason |
| 零分母 | 指标适用但 denominator 为 0 | `N/A` + `zero-denominator` |
| `coverage-gap` | 适用期内存在未映射源事实或 retire path | partial/structural-break |
| `recompute-required` | split 或语义变化无法映射 | structural-break + 重算入口 |

标签适用区间按“环境 activation interval 与该 LabelVersionItem 在版本内 active 的交集”解释，并使用 Fact `occurred_at` 判断；不能用入库时间判断。

## 8. 指标快照与报告

标签派生指标在服务端指标目录声明 `label_version_applicability=required`；非标签指标显式声明 `label_version_applicability=none`。任一 required 指标缺少 `label_scope` 时返回 422 并 fail closed，客户端不能自报 applicability。

`LabelMetricScope` 冻结：

```text
taxonomy_mode
source_label_version_ids[]
target_label_version_id
mapping_bundle_id
fact_set_generation
fact_as_of
metric_definition_versions{}
timezone
period_boundary
denominator_definition
```

现有 `metric_results` 保存通用不可变快照；标签派生快照通过一对一 `metric_result_label_scopes` 保存强 scope。每个结果还保存 applicability、comparability status/reasons、`scope_sha256`、`source_manifest_sha256`、`content_sha256`。数据库拒绝结果与 label scope 的 UPDATE/DELETE。

InsightReport 保存 `metric_result_ids[] + metric_scope_sha256`，只读取这些快照，不重查“最新 Fact”。

## 9. 废弃、在途运行与兼容发布入口

有 replacement 时：先发布 successor 制品和 mapping bundle，再用现有 ReleaseDeployment/ReleaseCommand 在各环境切 Head，旧 activation 进入 draining；排空在途运行后，旧制品才能 deprecated。

无 replacement 时：必须执行 impact preflight，列出环境、运行、资产、报告与下游 TaskVersion；生产停用需要自然人高风险审批与 safe-stop。原生历史保留，normalized 形成 coverage-gap，不能补 0。

`POST /api/v1/label-versions/{id}/publish` 只保留为兼容 adapter：它必须创建/委托 ReleaseDeployment command，不能直接改生产 Head，也不能成为第二个发布真相源。

run create、deployment ACK、drain/deprecate、completion receipt 并发时，以 Head 行锁/generation CAS 和冻结 Manifest 决定唯一终态。完成回执校验受理时的 Manifest，而不是要求完成时制品仍为当前 Head。

## 10. Fact、人工打标与 FactSet

逻辑 key 由服务端规范化：

```text
tenant/project + fact_namespace + subject kind/id
+ event_or_segment_id + label_id + assertion_slot
```

同主体不同事件生成不同 key。`(logical_key_sha, revision)` 唯一；supersedes 只能指向同 scope、同 key、前一 revision，禁止环和跳 revision。

权威 Fact 的 source 必须且只能是 Aggregate、HumanReviewDecision、LabelRecomputeRunItem 之一，并能追到 evidence、root Trace 与 action Trace。未知自由文本只进入 TaxonomySuggestion。

人工 draft 冻结 `label_version_id + label_id`。若提交时版本不再被当前 Head 或授权回填范围允许，返回 `STALE_LABEL_VERSION`；显式 rebase 创建新 draft、展示 mapping diff 并要求二次确认。禁止静默改标签、改版本或覆盖旧 draft。

full recompute 写独立 candidate namespace 与完整 Asset/FactSet Manifest。批准时在一个事务锁定 FactSet/Asset Head expected generations、验证完整分区和 SHA，再各执行一次 CAS。查询只能看到整套旧或整套新 Manifest。

## 11. 事件、隔离与审计

| 事件 | 最小 payload |
| --- | --- |
| `label_version.deprecation-requested` | version/replacement/bundle、impact、approval、expected version、Trace |
| `label_version.deprecated` | version/replacement/bundle、artifact timestamp、reason、Audit/Trace |
| `label_mapping_bundle.published` | source set、target、edge IDs、compiler version、canonical SHA、approver、Trace |
| `label_fact.created` | logical key/revision、label/version、source、occurred/recorded times、evidence、Trace |
| `label_recomputation.requested` | target/bundle、source Head generations、partitions、fact cutoff、budget、Trace |
| `label_fact_set.promoted` | old/new FactSet/Asset generations、manifest SHA、approval、Trace |
| `insight_metric.materialized` | mode/scope SHA、source SHA、result IDs/hashes、comparability、Trace |

所有写操作同事务写业务行、Audit、Outbox 和 resource/head version。所有关联使用 tenant/project 复合 FK 或等价强校验。生产废弃、mapping publish、full recompute promotion 必须自然人审批；system/Agent 只能生成候选、阻断或执行已批准命令。

错误、审计和 Outbox 只保存 evidence/object refs 与 hash，不保存完整转写、裸音频、签名 URL、密钥或客户敏感明文。回滚用 compensation event 或新 Head event，不删除历史事件。

## 12. 迁移、回滚与必测场景

迁移顺序固定为：Expand nullable 强表 → Dual-write → 可重入 Backfill/Shadow → 按 scope Read switch → orphan/旧写为 0 后 Contract。生产不 downgrade 已写新数据的 migration；以 forward migration 放宽或补偿。删除旧列/表另立 ADR。

必测：

- identity/rename 的 semantic hash；replace 1:1 未审批时不能 exact。
- merge 的 same-subject-same-event 折叠与 same-subject-different-event 保留。
- split-recompute 禁止分摊；无 approved FactSet 时 structural-break。
- retire 的 native 历史、normalized coverage-gap、适用期外 N/A。
- 晚到事实按 occurred_at 入桶、按 recorded_at/fact_as_of 进入新快照。
- run create、deployment ACK、deprecate、completion receipt 并发屏障。
- Fact supersedes 跨 scope/跨 key/跳 revision/环形失败。
- full recompute 故障注入时 FactSet/Asset Manifest 无半晋级。
- 旧 MetricResult/Report 在废弃、人工修正、重跑后 content hash 不变。

## 13. 请求、结果与失败示例

以下路径是冻结目标契约；在对应 FastAPI 工作包合入前不属于 live runtime。

### 13.1 Mapping relation

rename 请求项：

```json
{
  "source_label_id": "label_quote_old",
  "target_label_id": "label_quote_old",
  "relation": "rename",
  "source_semantic_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "target_semantic_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

成功结果为 `compatibility=exact`、`comparability_status=comparable`。若除 display name/alias 外的语义 hash 改变，validate 返回 422 `LABEL_MAPPING_SEMANTIC_HASH_CHANGED`，并要求 replace 或 recomputed。

replace 请求项：

```json
{
  "source_label_id": "label_old_intent",
  "target_label_id": "label_new_intent",
  "relation": "replace",
  "compatibility": "structural-break",
  "compatibility_evidence_ref": null
}
```

成功结果保留 `comparability_status=structural-break`。若客户端仅因 1:1 自报 exact 且没有自然人审批/eval evidence，publish 返回 422 `LABEL_MAPPING_COMPATIBILITY_EVIDENCE_REQUIRED`。

merge 请求项：

```json
{
  "source_label_ids": ["label_price_high", "label_price_unfair"],
  "target_label_id": "label_price_objection",
  "relation": "merge",
  "allowed_metric_families": ["presence", "distinct-count"],
  "metric_grain": "business-event",
  "lineage_key": "event_id",
  "reducer": "presence-any"
}
```

成功结果冻结 reducer 与 compiled path。缺 metric grain/lineage/reducer，或数值指标使用未审批 reducer 时返回 422 `LABEL_MAPPING_REDUCER_REQUIRED`；不能退化为按 subject 去重。

split 请求项：

```json
{
  "source_label_id": "label_generic_objection",
  "target_label_ids": ["label_price_objection", "label_product_objection"],
  "relation": "split-recompute",
  "requires_recompute": true
}
```

mapping 可以发布为重算指令，但 normalized 结果必须是 `structural-break`。若请求比例分摊、复制旧 Fact 或 `requires_recompute=false`，返回 422 `LABEL_MAPPING_RECOMPUTE_REQUIRED`。

retire 请求项：

```json
{
  "source_label_id": "label_legacy_campaign",
  "target_label_id": null,
  "relation": "retire",
  "requires_recompute": false
}
```

成功结果保留 native 历史并在适用期内形成 `coverage-gap`。retire 带 target 时返回 422 `LABEL_MAPPING_RETIRE_TARGET_FORBIDDEN`；查询适用期外才返回 not-applicable。

### 13.2 制品废弃

```http
POST /api/v1/label-versions/lv_v1/transitions
Idempotency-Key: label-version-lv-v1-deprecate-v1
```

```json
{
  "action": "deprecate",
  "expected_resource_version": 7,
  "replacement_label_version_id": "lv_v2",
  "mapping_bundle_id": "lmb_v1_to_v2",
  "reason": "业务定义升级"
}
```

若仍有 production Head 处于 active/draining，返回：

```json
{
  "error": {
    "code": "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE",
    "message": "标签版本仍被受保护环境引用",
    "status": 409,
    "retryable": false,
    "details": [{"environment": "production", "head_generation": 42}],
    "trace_id": "trace_deprecate_001"
  }
}
```

成功结果必须返回 deprecated 制品 resource version、replacement/bundle、activation timeline、Audit/Trace；不得返回被修改的历史 Fact 数量，因为历史事实不参与迁移。

### 13.3 normalized 指标与断点

```json
{
  "metric_keys": ["tagged_reception_count"],
  "time_range": "2026-06-01/2026-06-30",
  "label_scope": {
    "taxonomy_mode": "normalized",
    "source_label_version_ids": ["lv_v1", "lv_v2"],
    "target_label_version_id": "lv_v3",
    "mapping_bundle_id": "lmb_to_v3_20260718",
    "fact_set_generation": 42,
    "fact_as_of": "2026-07-18T10:00:00Z",
    "metric_definition_versions": {"tagged_reception_count": "3"},
    "timezone": "Asia/Shanghai",
    "period_boundary": "[start,end)",
    "denominator_definition": "eligible_receptions"
  }
}
```

完整覆盖时追加 comparable MetricResult；存在 retire/split path 时追加 `comparability_status=structural-break`、reason `coverage-gap|recompute-required`，value 不得伪造为 0。normalized 缺 bundle 返回 422 `INSIGHT_MAPPING_BUNDLE_REQUIRED`；标签派生指标缺 label scope 返回 422 `INSIGHT_LABEL_VERSION_REQUIRED`。
