import type {
  AssetApiContract,
  AssetCatalogRow,
  AssetCompatibilityCheck,
  AssetRunTimelineRow
} from "./types";

export const assetRowsSource: AssetCatalogRow[] = [
  {
    name: "原始音频资产", domain: "音频域", status: "已生成", version: "daily-20250526", quality: 96,
    assetKey: "auris/audio/raw_recordings", definition: "外部数据源 + 对象存储管理", partition: "daily/store/hour",
    materialization: "MAT-20250526-raw-8412", owner: "数据接入组", freshness: "15 分钟内",
    upstream: "PBX / S3 / 门店设备", downstream: ["有效语音片段资产", "多设备重叠索引"],
    backfill: "可按门店小时重扫", checks: ["URL 可访问", "租户隔离", "音频时长"]
  },
  {
    name: "有效语音片段资产", domain: "音频域", status: "已生成", version: "vad-v2.4-fast", quality: 92,
    assetKey: "auris/audio/voice_segments", definition: "处理资产 + 动态分区", partition: "daily/store/hour/device",
    materialization: "MAT-20250526-vad-3170", owner: "音频算法组", freshness: "30 分钟内",
    upstream: "原始音频资产", downstream: ["ASR 转写资产", "串音证据资产"],
    backfill: "低能量门店需人工确认", checks: ["静音跳过", "SNR 阈值", "片段边界"]
  },
  {
    name: "ASR 转写资产", domain: "模型输出", status: "部分失败", version: "asr-v2.3.1", quality: 89,
    assetKey: "auris/model/asr_transcripts", definition: "模型资产 + 重试策略 + 质量检查", partition: "daily/store/hour",
    materialization: "MAT-20250526-asr-9021", owner: "模型平台组", freshness: "45 分钟内",
    upstream: "有效语音片段资产", downstream: ["事件标签资产", "证据包资产"],
    backfill: "失败分区 3 个待重跑", checks: ["空转写率", "热词覆盖", "模型版本"]
  },
  {
    name: "事件标签资产", domain: "标签域", status: "待回填", version: "label-v1.8.4", quality: 84,
    assetKey: "auris/label/event_tags", definition: "多产物资产 + 人审门禁", partition: "daily/store/event",
    materialization: "MAT-20250526-tag-1288", owner: "质检策略组", freshness: "待补 2 小时",
    upstream: "ASR 转写资产 / 业务单据资产", downstream: ["评测指标资产", "业务洞察资产"],
    backfill: "金额冲突标签影响 15 个下游", checks: ["标签版本", "低置信", "单据一致性"]
  },
  {
    name: "业务单据事件资产", domain: "事件关联域", status: "已生成", version: "docs-v20250526", quality: 91,
    assetKey: "auris/events/document_links", definition: "事件资产 + 外部接口接入", partition: "daily/store/document_type",
    materialization: "MAT-20250526-doc-4410", owner: "业务系统组", freshness: "20 分钟内",
    upstream: "报价单 / 试驾单 / 订单 API", downstream: ["事件标签资产", "证据包资产"],
    backfill: "单据字段变更需重建关联", checks: ["主键映射", "字段差异", "事件时间"]
  },
  {
    name: "评测指标资产", domain: "评测域", status: "已生成", version: "eval-20250526", quality: 93,
    assetKey: "auris/eval/quality_metrics", definition: "评测资产 + 质量检查 + 报告", partition: "daily/project/model_version",
    materialization: "MAT-20250526-eval-7724", owner: "评测组", freshness: "每日 02:30",
    upstream: "事件标签资产 / 人工复核资产", downstream: ["模型对比报告资产", "洞察趋势资产"],
    backfill: "模型版本切换后自动重评", checks: ["覆盖率", "一致性", "人工接受率"]
  }
];

export const assetDagsterCompatibilityChecksSource: AssetCompatibilityCheck[] = [
  ["Key 命名", "兼容", "tenant/project/domain/name"], ["租户标签", "兼容", "tenant/project/user"],
  ["分区策略", "兼容", "daily/store/hour"], ["外部数据源", "兼容", "PBX/S3/CRM/订单"],
  ["生成记录", "兼容", "详情/证据链"], ["质量检查", "兼容", "完整/及时/一致"],
  ["Backfill", "需人工", "覆盖/重算走 Human Loop"], ["运行请求幂等", "兼容", "asset/partition/version"],
  ["IO Manager", "兼容", "对象存储/MySQL/Qdrant"], ["血缘依赖", "兼容", "业务化上下游"],
  ["游标分页", "兼容", "cursor 翻页"], ["筛选排序", "兼容", "时空/事件/人物/单据"],
  ["错误映射", "兼容", "409/422/503"], ["权限", "兼容", "租户/项目/角色"],
  ["新鲜度", "兼容", "生成与超时"], ["数据版本", "兼容", "模型/标签/资产"],
  ["人工确认", "需人工", "回填/覆盖/发布"], ["下游影响", "兼容", "资产/样本/耗时"],
  ["重试死信", "需确认", "失败分区重跑"], ["观测性", "兼容", "trace/run/mat"]
];

export const assetRunTimeline: AssetRunTimelineRow[] = [
  ["12:31:08", "ASR 转写资产", "失败 3 分区", "run-asr-20250526-0912"],
  ["12:33:42", "事件标签资产", "等待人工确认", "run-tag-20250526-1288"],
  ["12:38:10", "业务单据事件资产", "已生成", "run-doc-20250526-4410"],
  ["12:41:26", "评测指标资产", "排队中", "run-eval-20250526-7724"]
];

export const assetApiContractsSource: AssetApiContract[] = [
  { method: "GET", endpoint: "/api/v1/data-assets", purpose: "资产目录、筛选、游标分页", dagster: "资产 Key + 分区定义", response: "items, cursor, status_counts", tone: "read" },
  { method: "GET", endpoint: "/api/v1/data-assets/:assetKey/lineage", purpose: "业务化血缘和下游影响", dagster: "资产血缘关系", response: "upstream, downstream, impact", tone: "read" },
  { method: "GET", endpoint: "/api/v1/data-assets/:assetKey/materializations", purpose: "生成记录和质量检查", dagster: "资产生成记录 + 质量检查", response: "materialization_id, run_id, checks", tone: "read" },
  { method: "POST", endpoint: "/api/v1/data-assets/:assetKey/backfills", purpose: "创建受控回填，返回审批或 run_id", dagster: "回填策略 + 运行请求", response: "approval_id / run_id", tone: "write" }
];
