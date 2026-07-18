import { assetRows } from "../../catalog";

export type AssetLineageNode = {
  id: string;
  assetKey?: string;
  label: string;
  meta: string;
  status: string;
  dagster: string;
  quality?: number;
  x: number;
  y: number;
  tone: string;
  summary: string;
};

export type AssetLineageEdge = {
  from: string;
  to: string;
  label: string;
  dashed?: boolean;
};

export function createAssetLineageNodes(): AssetLineageNode[] {
  const findAsset = (assetKey: string) => assetRows.find((asset) => asset.assetKey === assetKey);
  return [
    {
      id: "source-platform",
      label: "平台数据源",
      meta: "PBX / S3 / CRM / 订单 API",
      status: "Source",
      dagster: "外部数据源 + 存储管理器",
      x: 24,
      y: 56,
      tone: "source",
      summary: "从门店设备、电话录音、报价单、试驾单和订单系统同步原始证据。"
    },
    {
      id: "raw",
      assetKey: "auris/audio/raw_recordings",
      label: findAsset("auris/audio/raw_recordings")?.name ?? "原始音频资产",
      meta: "daily/store/hour",
      status: findAsset("auris/audio/raw_recordings")?.status ?? "已生成",
      dagster: "音频外部数据源",
      quality: findAsset("auris/audio/raw_recordings")?.quality,
      x: 226,
      y: 56,
      tone: "audio",
      summary: "原始 WAV、URL 可访问性、门店设备和租户隔离检查。"
    },
    {
      id: "segments",
      assetKey: "auris/audio/voice_segments",
      label: findAsset("auris/audio/voice_segments")?.name ?? "有效语音片段资产",
      meta: "VAD / 静音跳过 / SNR",
      status: findAsset("auris/audio/voice_segments")?.status ?? "已生成",
      dagster: "Asset + DynamicPartitions",
      quality: findAsset("auris/audio/voice_segments")?.quality,
      x: 428,
      y: 56,
      tone: "audio",
      summary: "把一天完整音频切成可审查片段，支撑调听 Minimap 和下游模型。"
    },
    {
      id: "crosstalk",
      label: "串音证据索引",
      meta: "同日同店 / 多设备重叠",
      status: "生成中",
      dagster: "AssetCheck + overlap_index",
      quality: 88,
      x: 428,
      y: 192,
      tone: "risk",
      summary: "用空间、设备和时间重叠关系标记主录音、串音候选和重复收录。"
    },
    {
      id: "asr",
      assetKey: "auris/model/asr_transcripts",
      label: findAsset("auris/model/asr_transcripts")?.name ?? "ASR 转写资产",
      meta: "ASR v2.3.1 / RetryPolicy",
      status: findAsset("auris/model/asr_transcripts")?.status ?? "部分失败",
      dagster: "Asset + RetryPolicy",
      quality: findAsset("auris/model/asr_transcripts")?.quality,
      x: 630,
      y: 56,
      tone: "model",
      summary: "模型转写结果，失败分区进入重跑，低置信片段进入人工复核。"
    },
    {
      id: "documents",
      assetKey: "auris/events/document_links",
      label: findAsset("auris/events/document_links")?.name ?? "业务单据事件资产",
      meta: "报价单 / 试驾单 / 订单",
      status: findAsset("auris/events/document_links")?.status ?? "已生成",
      dagster: "External Asset Ingest",
      quality: findAsset("auris/events/document_links")?.quality,
      x: 630,
      y: 192,
      tone: "event",
      summary: "把汽车、单据、接待和报价抽象为事件关联，给证据审查提供字段依据。"
    },
    {
      id: "event-tags",
      assetKey: "auris/label/event_tags",
      label: findAsset("auris/label/event_tags")?.name ?? "事件标签资产",
      meta: "标签 v1.8.4 / Human Gate",
      status: findAsset("auris/label/event_tags")?.status ?? "待回填",
      dagster: "MultiAsset + HumanReview",
      quality: findAsset("auris/label/event_tags")?.quality,
      x: 832,
      y: 56,
      tone: "label",
      summary: "实体、意图、质检、单据事件和 Agent 动作标签的统一产物。"
    },
    {
      id: "review",
      label: "人工复核队列",
      meta: "Human Loop / Review Queue",
      status: "待处理 317",
      dagster: "事件监听 + 运行请求",
      x: 832,
      y: 192,
      tone: "human",
      summary: "低置信、金额冲突、串音待排除等样本进入人工确认，不覆盖已确认历史。"
    },
    {
      id: "evidence-pack",
      label: "证据包资产",
      meta: "音频 + ASR + 标签 + 单据",
      status: "可导出",
      dagster: "资产生成记录 + 导出清单",
      x: 1034,
      y: 56,
      tone: "output",
      summary: "把审查所需证据封装为可追溯包，支持 API、Webhook、CSV/Parquet 导出。"
    },
    {
      id: "backfill",
      label: "回填 / 重算",
      meta: "BackfillPolicy / 分区窗口",
      status: "需审批",
      dagster: "Backfill + AssetSelection",
      x: 1034,
      y: 192,
      tone: "risk",
      summary: "当标签规则、模型版本或单据字段变化时，只重算受影响的分区和下游。"
    },
    {
      id: "metrics",
      assetKey: "auris/eval/quality_metrics",
      label: findAsset("auris/eval/quality_metrics")?.name ?? "评测指标资产",
      meta: "模型质量 / AB 实验 / 洞察",
      status: findAsset("auris/eval/quality_metrics")?.status ?? "已生成",
      dagster: "AssetCheck + Report",
      quality: findAsset("auris/eval/quality_metrics")?.quality,
      x: 1236,
      y: 56,
      tone: "quality",
      summary: "沉淀模型质量、标签接受率、误报率和业务洞察趋势。"
    }
  ];
}

export function createAssetLineageEdges(): AssetLineageEdge[] {
  return [
    { from: "source-platform", to: "raw", label: "音频/URL" },
    { from: "source-platform", to: "documents", label: "单据事件", dashed: true },
    { from: "raw", to: "segments", label: "VAD" },
    { from: "segments", to: "asr", label: "ASR" },
    { from: "segments", to: "crosstalk", label: "重叠索引", dashed: true },
    { from: "crosstalk", to: "event-tags", label: "串音证据", dashed: true },
    { from: "asr", to: "event-tags", label: "语义标签" },
    { from: "documents", to: "event-tags", label: "事件关联" },
    { from: "event-tags", to: "evidence-pack", label: "证据封装" },
    { from: "event-tags", to: "review", label: "低置信", dashed: true },
    { from: "review", to: "backfill", label: "人工确认" },
    { from: "backfill", to: "event-tags", label: "回填", dashed: true },
    { from: "evidence-pack", to: "metrics", label: "评测" },
    { from: "event-tags", to: "metrics", label: "质量指标", dashed: true }
  ];
}
