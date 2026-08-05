import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  BrainCircuit,
  Check,
  Database,
  Download,
  Eye,
  Gauge,
  GitBranch,
  Headphones,
  Layers,
  Link2,
  ListFilter,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Tags,
  UserCheck
} from "lucide-react";
import { Fragment, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import {
  buildKnowledgeIndex,
  getBackendRun,
  syncKnowledgeSource,
  type ApiRuntimeContext,
  type ProjectSceneProfileBinding,
  type BackendActionReceipt
} from "../../api/client";
import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";
import type { OperationNotice, OperationStatus } from "../../shared/contracts/operations";

const KNOWLEDGE_TAB_LABELS: Record<string, string> = {
  overview: "知识总览",
  connectors: "知识连接器",
  indexing: "索引构建",
  graph: "知识可视化",
  quality: "质量管理",
  effects: "效果展示",
  runs: "运行记录"
};

const normalizeBackendRunStatus = (status?: string) => String(status ?? "pending").toLowerCase();
const backendRunSucceeded = (status?: string) =>
  ["success", "succeeded", "complete", "completed"].includes(normalizeBackendRunStatus(status));
const backendRunFailed = (status?: string) =>
  ["failed", "error", "dead_letter", "canceled", "cancelled"].includes(normalizeBackendRunStatus(status));
const operationStatusFromBackendRun = (status?: string): OperationStatus => {
  if (backendRunSucceeded(status)) return "success";
  if (backendRunFailed(status)) return "error";
  return "pending";
};
const backendRunStatusLabel = (status?: string) => {
  const normalized = normalizeBackendRunStatus(status);
  if (["success", "succeeded", "complete", "completed"].includes(normalized)) return "已完成";
  if (["submitted", "dispatched"].includes(normalized)) return "已提交，等待外部完成";
  if (normalized === "running") return "运行中";
  if (["queued", "pending"].includes(normalized)) return "等待执行";
  if (normalized === "blocked") return "等待门禁";
  if (normalized === "dead_letter") return "死信待处理";
  if (["failed", "error"].includes(normalized)) return "执行失败";
  if (["canceled", "cancelled"].includes(normalized)) return "已取消";
  return `状态 ${status ?? "pending"}`;
};

async function refreshBackendRunReceipt(receipt: BackendActionReceipt): Promise<BackendActionReceipt> {
  try {
    const detail = await getBackendRun(receipt.id);
    return {
      ...receipt,
      ...detail.data,
      trace_id: detail.data.trace_id ?? receipt.trace_id,
      raw: {
        ...receipt.raw,
        run_detail: detail.data.raw
      }
    };
  } catch {
    return receipt;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const recordString = (record: Record<string, unknown>, keys: string[], fallback = "") => {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return fallback;
};

const recordNumber = (record: Record<string, unknown>, keys: string[], fallback = 0) => {
  for (const key of keys) {
    const value = Number(record[key]);
    if (Number.isFinite(value)) return value;
  }
  return fallback;
};

function PanelHeader({
  title,
  subtitle,
  icon,
  sticky = false,
  className = ""
}: {
  title: string;
  subtitle: string;
  icon: ReactNode;
  sticky?: boolean;
  className?: string;
}) {
  return (
    <div className={["module-panel-head", sticky ? "sticky-panel-head" : "", className].filter(Boolean).join(" ")}>
      <div>
        <span>{title}</span>
        <strong>{subtitle}</strong>
      </div>
      <i>{icon}</i>
    </div>
  );
}

function TimelineList({ items }: { items: Array<[string, string, string]> }) {
  return (
    <div className="timeline-list">
      {items.map(([time, title, desc]) => (
        <button key={`${time}-${title}`}>
          <span>{time}</span>
          <strong>{title}</strong>
          <em>{desc}</em>
        </button>
      ))}
    </div>
  );
}

export default function KnowledgeModule({
  activeTab,
  setActiveModule,
  navigateToTarget,
  focus,
  projectionItems,
  projectionSource,
  sceneBinding,
  apiContext,
  demoMode
}: {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  focus: ModuleDeepLink | null;
  projectionItems?: unknown[];
  projectionSource: "bff" | "mock";
  sceneBinding: ProjectSceneProfileBinding | null;
  apiContext: ApiRuntimeContext;
  demoMode: boolean;
}) {
  type KnowledgeSource = {
    id: string;
    name: string;
    kind: string;
    scope: string;
    owner: string;
    objects: string;
    status: "已同步" | "待同步" | "异常";
    quality: number;
    route: ModuleKey;
    relations: Array<[string, string, string]>;
  };
  type KnowledgeEvidenceLink = {
    id: string;
    title: string;
    recording: string;
    window: string;
    derivedAssets: string[];
    knowledgeHits: string[];
    linkedLabels: string[];
    gaps: string[];
    confidence: number;
  };
  type KnowledgeChunkPreview = {
    id: string;
    source: string;
    window: string;
    chunkId: string;
    tokens: number;
    overlap: string;
    labels: string[];
    recall: number;
    quality: "正常" | "过短" | "过长" | "语义断裂" | "标签覆盖不足" | "跨录音边界";
    before: string;
    after: string;
    evidence: string;
  };
  type KnowledgeLabelGap = {
    id: string;
    type: "缺失标签" | "冲突标签" | "低置信标签";
    label: string;
    evidence: string;
    knowledge: string;
    action: string;
    severity: "高" | "中" | "低";
  };
  type KnowledgeCompletionSuggestion = {
    id: string;
    title: string;
    from: string;
    proposal: string;
    impact: string;
    target: ModuleKey;
    status: string;
  };
  type KnowledgeGraphPath = {
    id: string;
    sourceId: string;
    evidenceId: string;
    gapId: string;
    source: string;
    sourceMeta: string;
    chunk: string;
    chunkMeta: string;
    entities: string[];
    index: string;
    apps: string[];
    recall: number;
    quality: string;
    risk: string;
    tone: "blue" | "teal" | "green" | "amber" | "violet" | "red";
    route: ModuleKey;
  };
  type KnowledgeObjectKind = "source" | "evidence" | "chunk" | "gap" | "path" | "gate" | "effect" | "run";
  type KnowledgeObjectSelection = {
    kind: KnowledgeObjectKind;
    id: string;
  };
  type KnowledgeListItem = {
    id: string;
    kind: KnowledgeObjectKind;
    title: string;
    meta: string;
    detail: string;
    status?: string;
    score?: string | number;
    tone?: "blue" | "teal" | "green" | "amber" | "violet" | "red";
    onSelect: () => void;
  };
  const automotiveDemoKnowledgeSources: KnowledgeSource[] = [
    {
      id: "sop",
      name: "销售话术 SOP",
      kind: "文档库",
      scope: "接待 / 报价 / 试驾",
      owner: "质检运营",
      objects: "1,284 个切片",
      status: "已同步",
      quality: 94,
      route: "canvas",
      relations: [["上游", "销售 SOP 文档库", "按场景切片"], ["连接", "ASR 片段", "用话术阶段校验标签"], ["下游", "标签治理", "补齐意图/质检标签"]]
    },
    {
      id: "faq",
      name: "CRM FAQ",
      kind: "业务 API",
      scope: "优惠政策 / 价格口径",
      owner: "业务系统组",
      objects: "842 条知识",
      status: "待同步",
      quality: 88,
      route: "canvas",
      relations: [["上游", "CRM 优惠政策", "24h 增量"], ["连接", "报价单字段", "校验金额口径"], ["下游", "标签冲突", "识别单据不一致"]]
    },
    {
      id: "product",
      name: "车型产品资料",
      kind: "文件夹",
      scope: "车型 / 配置 / 金融方案",
      owner: "产品运营",
      objects: "396 份文档",
      status: "异常",
      quality: 72,
      route: "canvas",
      relations: [["上游", "产品资料文件夹", "目录权限待修复"], ["连接", "车型实体标签", "补齐配置/金融方案"], ["下游", "知识质量门禁", "阻断发布"]]
    },
    {
      id: "labels",
      name: "标签样本库",
      kind: "标签资产",
      scope: "异议 / 承诺 / 成交意向",
      owner: "标签运营",
      objects: "3,184 个样本",
      status: "已同步",
      quality: 91,
      route: "labels",
      relations: [["上游", "标签版本 v1.8.4", "正负例"], ["连接", "知识命中样本", "生成候选标签"], ["下游", "规则候选", "Human Loop 回写"]]
    },
    {
      id: "evidence",
      name: "音频证据包",
      kind: "证据资产",
      scope: "调听片段 / 人审结论",
      owner: "质检运营",
      objects: "37 个证据包",
      status: "待同步",
      quality: 86,
      route: "listening",
      relations: [["上游", "录音 / ASR / 标签轨道", "不保存原始录音"], ["连接", "证据包 chunk", "窗口可回放"], ["下游", "标签完整性", "补齐缺失标签"]]
    }
  ];
  const sceneManifest = sceneBinding?.version.manifest ?? null;
  const projectedKnowledgeSources = useMemo<KnowledgeSource[]>(() => {
    const records = (projectionItems ?? []).filter(isRecord);
    const sourceItems = records.map((record, index) => {
      const id = recordString(record, ["knowledge_source_id", "source_id", "resource_key", "id"], `knowledge-source-${index + 1}`);
      const statusValue = recordString(record, ["status", "sync_status"], "pending").toLowerCase();
      const status: KnowledgeSource["status"] = ["active", "published", "success", "synced", "completed"].includes(statusValue)
        ? "已同步"
        : ["failed", "error", "blocked", "degraded"].includes(statusValue)
          ? "异常"
          : "待同步";
      const qualityRaw = recordNumber(record, ["quality_score", "quality", "score"], 0);
      const quality = qualityRaw <= 1 && qualityRaw > 0 ? Math.round(qualityRaw * 100) : Math.round(qualityRaw);
      const objectCount = recordNumber(record, ["object_count", "chunk_count", "count", "total"], 0);
      const sourceType = recordString(record, ["source_type", "kind", "type"], "知识源");
      const scope = recordString(record, ["scope", "description"], "由当前 SceneProfile 与连接器权限限定");
      return {
        id,
        name: recordString(record, ["name", "title", "display_name"], id),
        kind: sourceType,
        scope,
        owner: recordString(record, ["owner", "owner_name"], "当前项目"),
        objects: objectCount > 0 ? `${objectCount.toLocaleString("zh-CN")} 个对象` : "等待同步统计",
        status,
        quality,
        route: "canvas" as ModuleKey,
        relations: [
          ["上游", sourceType, "连接器受租户和项目边界约束"],
          ["索引", sceneManifest?.knowledge_index_refs[0] ?? "待绑定知识索引", "版本由 SceneProfile 锁定"],
          ["下游", sceneManifest?.label_version_refs[0] ?? "候选消费方", "只生成候选或草稿"]
        ] as Array<[string, string, string]>
      };
    });
    if (sourceItems.length > 0) return sourceItems;
    const indexSources = (sceneManifest?.knowledge_index_refs ?? []).map((indexRef, index) => ({
      id: `scene-index-${index + 1}`,
      name: indexRef,
      kind: "SceneProfile 索引引用",
      scope: sceneManifest?.display_name ?? "当前场景",
      owner: "当前项目",
      objects: "等待知识源投影",
      status: "待同步" as const,
      quality: 0,
      route: "canvas" as ModuleKey,
      relations: [
        ["配置", sceneManifest?.scene_key ?? "scene", sceneBinding?.version.version ?? "未绑定"],
        ["索引", indexRef, "不可替换为行业默认索引"],
        ["评测", sceneManifest?.eval_dataset_version_refs[0] ?? "待配置", "发布前验证召回质量"]
      ] as Array<[string, string, string]>
    }));
    if (indexSources.length > 0) return indexSources;
    return [{
      id: "knowledge-unconfigured",
      name: "未配置知识源",
      kind: "配置缺口",
      scope: "先发布并绑定 SceneProfile，再连接知识源",
      owner: "项目管理员",
      objects: "0 个对象",
      status: "待同步",
      quality: 0,
      route: "projects",
      relations: [
        ["前置", "SceneProfile", "绑定生产版本"],
        ["配置", "知识连接器", "声明 source 与 index 强引用"],
        ["验证", "召回评测", "通过后才允许消费"]
      ]
    }];
  }, [projectionItems, sceneBinding]);
  const knowledgeSources = demoMode && projectionSource === "mock"
    ? automotiveDemoKnowledgeSources
    : projectedKnowledgeSources;
  const lifecycleStages = [
    ["录音证据", "ASR、边界、标签轨道、单据事件", "listening", "blue"],
    ["知识切片", "文档与 ASR 派生证据生成语义切片", "knowledge", "teal"],
    ["标签缺口", "识别缺失、冲突和低置信标签", "knowledge", "amber"],
    ["候选回写", "写入标签草稿、评测集或人审队列", "labels", "green"],
    ["效果验证", "召回、补全贡献和 badcase 回流", "evaluation", "violet"]
  ] satisfies Array<[string, string, ModuleKey, string]>;
  const automotiveQualityGates = [
    { label: "新鲜度", value: "96%", detail: "CRM FAQ 24h 内同步", state: "ok" },
    { label: "切片质量", value: "91%", detail: "2 条语义断裂待重切", state: "warn" },
    { label: "标签覆盖", value: "86%", detail: "报价承诺、成交意向仍有缺口", state: "warn" },
    { label: "实体冲突", value: "12", detail: "车型价格知识与报价单样本需确认", state: "warn" },
    { label: "证据可回放", value: "97%", detail: "chunk 可回到录音窗口", state: "ok" },
    { label: "召回质量", value: "84.6%", detail: "R@5 低于目标 88%", state: "warn" },
    { label: "人审待确认", value: "18", detail: "低置信和串音标签需人工", state: "risk" },
    { label: "脱敏", value: "通过", detail: "手机号和原始 URL 已脱敏", state: "ok" }
  ];
  const sceneQualityGates = (sceneManifest?.release_requirements ?? []).map((requirement) => ({
    label: sceneManifest?.metrics.find((metric) => metric.metric_key === requirement.metric_key)?.display_name ?? requirement.metric_key,
    value: `${(requirement.threshold_ppm / 10_000).toFixed(1)}%`,
    detail: `${requirement.gate_kind} · ${requirement.operator} · 等待评测事实`,
    state: "warn"
  }));
  const qualityGates = demoMode && projectionSource === "mock"
    ? automotiveQualityGates
    : sceneQualityGates.length > 0
      ? sceneQualityGates
      : [{ label: "场景门禁", value: "未配置", detail: "SceneProfile 必须声明三层评测门禁", state: "risk" }];
  const automotiveEffectStages = [
    { label: "证据包输入", value: "37", rate: 100, drop: "调听片段 / 人审结论", tone: "blue" },
    { label: "知识命中", value: "7,128", rate: 85, drop: "SOP / FAQ / 标签样本", tone: "green" },
    { label: "标签补齐", value: "214", rate: 62, drop: "实体、意图、质检标签", tone: "teal" },
    { label: "人审接受", value: "81%", rate: 50, drop: "需修改 42 条", tone: "amber" },
    { label: "回归沉淀", value: "37", rate: 18, drop: "评测集 / badcase / 规则候选", tone: "violet" }
  ];
  const sceneEffectStages = (sceneManifest?.metrics ?? []).slice(0, 5).map((metric, index) => ({
    label: metric.display_name,
    value: "待计算",
    rate: Math.max(18, 100 - index * 18),
    drop: `${metric.metric_key} · ${metric.unit}`,
    tone: ["blue", "green", "teal", "amber", "violet"][index] ?? "blue"
  }));
  const effectStages = demoMode && projectionSource === "mock"
    ? automotiveEffectStages
    : sceneEffectStages.length > 0
      ? sceneEffectStages
      : [{ label: "效果指标", value: "未配置", rate: 0, drop: "等待 SceneProfile 指标定义", tone: "amber" }];
  const automotiveKnowledgeGraphPaths: KnowledgeGraphPath[] = [
    {
      id: "kg-quote-faq",
      sourceId: "faq",
      evidenceId: "EVP-quote-128",
      gapId: "gap-quote",
      source: "CRM FAQ",
      sourceMeta: "优惠政策 / 报价口径 API",
      chunk: "kb-faq-price-c42",
      chunkMeta: "金额口径 · 24h 增量",
      entities: ["报价金额", "优惠幅度", "金额冲突"],
      index: "Hybrid R@5 92%",
      apps: ["标签冲突仲裁", "Human Loop", "报告回写"],
      recall: 92,
      quality: "口径一致性高",
      risk: "报价单字段与 ASR 口播不一致，需保留冲突证据。",
      tone: "amber",
      route: "labels"
    },
    {
      id: "kg-audio-sop",
      sourceId: "evidence",
      evidenceId: "EVP-quote-128",
      gapId: "gap-deal",
      source: "音频证据包",
      sourceMeta: "A-1001 / ASR / 标签轨道",
      chunk: "kb-audio-quote-128-c03",
      chunkMeta: "12:27:18-12:27:44 · 186 tokens",
      entities: ["报价承诺", "价格异议", "成交意向候选"],
      index: "Evidence Graph 91%",
      apps: ["调听证据", "标签补全", "评测回归"],
      recall: 91,
      quality: "可回放到音频窗口",
      risk: "缺少成交意向标签，建议生成候选但不直接发布。",
      tone: "blue",
      route: "listening"
    },
    {
      id: "kg-drive-sop",
      sourceId: "sop",
      evidenceId: "EVP-drive-129",
      gapId: "gap-testdrive",
      source: "销售话术 SOP",
      sourceMeta: "试驾流程 / 接待阶段",
      chunk: "kb-sop-drive-c118",
      chunkMeta: "试驾承接段落 · 标签窗口",
      entities: ["试驾承接", "预约确认", "可回填"],
      index: "Vector + BM25 87%",
      apps: ["任务回写", "标签治理", "训练样本包"],
      recall: 87,
      quality: "需要合并相邻 chunk",
      risk: "试驾承接和预约确认被切开，影响完整标签召回。",
      tone: "teal",
      route: "canvas"
    },
    {
      id: "kg-product-price",
      sourceId: "product",
      evidenceId: "EVP-quote-128",
      gapId: "gap-quote",
      source: "车型产品资料",
      sourceMeta: "车型 / 配置 / 金融方案",
      chunk: "kb-product-price-c09",
      chunkMeta: "产品价格表 · 权限待修复",
      entities: ["车型", "金融方案", "报价字段"],
      index: "Entity Graph 72%",
      apps: ["质量门禁", "业务洞察", "资产血缘"],
      recall: 72,
      quality: "目录权限异常",
      risk: "产品资料连接器异常，发布前必须通过知识质量门禁。",
      tone: "red",
      route: "assets"
    }
  ];
  const sceneObjectLabels = [
    ...(sceneManifest?.entities ?? []),
    ...(sceneManifest?.events ?? []),
    ...(sceneManifest?.document_types ?? [])
  ].map((item) => item.display_name);
  const sceneKnowledgeGraphPaths: KnowledgeGraphPath[] = knowledgeSources.map((source, index) => ({
    id: `scene-path-${source.id}`,
    sourceId: source.id,
    evidenceId: `scene-evidence-${source.id}`,
    gapId: `scene-gap-${index + 1}`,
    source: source.name,
    sourceMeta: `${source.kind} · ${source.scope}`,
    chunk: `chunk:${source.id}:latest`,
    chunkMeta: "切片参数由知识索引版本锁定",
    entities: sceneObjectLabels.slice(index, index + 3).length > 0
      ? sceneObjectLabels.slice(index, index + 3)
      : [sceneManifest?.display_name ?? "待配置场景对象"],
    index: sceneManifest?.knowledge_index_refs[index % Math.max(1, sceneManifest.knowledge_index_refs.length)] ?? "待配置知识索引",
    apps: [
      sceneManifest?.label_version_refs[0] ? `标签 ${sceneManifest.label_version_refs[0]}` : "标签候选",
      sceneManifest?.metrics[index % Math.max(1, sceneManifest.metrics.length)]?.display_name ?? "效果评测",
      "Human Loop"
    ],
    recall: source.quality,
    quality: source.status === "异常" ? "连接器或质量门禁异常" : source.status === "已同步" ? "等待场景评测" : "等待同步",
    risk: "模型召回只能生成候选；发布和业务回写仍需场景门禁与人工决策。",
    tone: (["blue", "teal", "green", "amber", "violet", "red"] as KnowledgeGraphPath["tone"][])[index % 6],
    route: "labels"
  }));
  const knowledgeGraphPaths = demoMode && projectionSource === "mock"
    ? automotiveKnowledgeGraphPaths
    : sceneKnowledgeGraphPaths;
  const automotiveIndexPolicies = [
    ["切片策略", "按业务段落 + 标签窗口", "避免孤立句子进入 RAG"],
    ["向量维度", "768", "业务知识与证据样本共用 embedding profile"],
    ["召回索引", "Vector + BM25 + Entity Graph", "可解释召回，支持回到证据链"],
    ["版本策略", "灰度索引 v3.3", "发布前先影子评测，不覆盖线上 v3.2"]
  ] satisfies Array<[string, string, string]>;
  const indexPolicies: Array<[string, string, string]> = demoMode && projectionSource === "mock"
    ? automotiveIndexPolicies
    : [
        ["索引引用", sceneManifest?.knowledge_index_refs.join(" / ") || "未配置", "索引必须来自已发布 SceneProfile"],
        ["数据契约", sceneManifest?.data_contract_refs.join(" / ") || "未配置", "连接器输出必须满足版本化契约"],
        ["评测集", sceneManifest?.eval_dataset_version_refs.join(" / ") || "未配置", "召回效果通过项目留出集验证"],
        ["快照", sceneBinding?.manifest_sha256.slice(0, 12) || "未绑定", "运行期间不可替换场景依赖"]
      ];
  const automotiveEvidenceLinks: KnowledgeEvidenceLink[] = [
    {
      id: "EVP-quote-128",
      title: "报价金额冲突证据包",
      recording: "A-1001_20250526_122300.wav",
      window: "12:27:18 - 12:28:01",
      derivedAssets: ["ASR v2.3.1", "报价单 #BJ-041", "标签轨道 L4/L5/L6", "人工复核 HR-1029"],
      knowledgeHits: ["CRM FAQ / 优惠政策", "销售话术 SOP / 报价阶段", "标签样本库 / 金额冲突正例"],
      linkedLabels: ["报价金额", "优惠幅度", "报价承诺", "金额冲突"],
      gaps: ["缺少成交意向", "优惠承诺置信低", "报价单字段冲突"],
      confidence: 82
    },
    {
      id: "EVP-drive-129",
      title: "试驾承接补全证据包",
      recording: "B-2001_20250526_122812.wav",
      window: "12:28:01 - 12:29:28",
      derivedAssets: ["ASR diff", "试驾单 #SJ-028", "串音候选", "主录音确认"],
      knowledgeHits: ["试驾流程 SOP", "车型产品资料", "历史标签样本 / 试驾承接"],
      linkedLabels: ["试驾时间", "试驾承接", "可回填"],
      gaps: ["串音待排除", "预约确认缺标签"],
      confidence: 76
    }
  ];
  const sceneEvidenceLinks: KnowledgeEvidenceLink[] = knowledgeGraphPaths.map((path, index) => ({
    id: path.evidenceId,
    title: `${path.source}消费证据`,
    recording: sceneManifest?.data_contract_refs[index % Math.max(1, sceneManifest.data_contract_refs.length)] ?? "等待运行时证据引用",
    window: "由运行时 evidence_ref 决定，不使用行业默认窗口",
    derivedAssets: sceneManifest?.task_type_refs ?? [],
    knowledgeHits: [path.index, path.chunk],
    linkedLabels: path.entities,
    gaps: sceneManifest?.release_requirements.map((item) => item.requirement_key) ?? ["等待场景门禁"],
    confidence: path.recall
  }));
  const evidenceLinks = demoMode && projectionSource === "mock"
    ? automotiveEvidenceLinks
    : sceneEvidenceLinks;
  const automotiveChunkPreviews: KnowledgeChunkPreview[] = [
    {
      id: "chunk-quote-01",
      source: "录音 ASR 派生证据",
      window: "12:27:18 - 12:27:44",
      chunkId: "kb-audio-quote-128-c03",
      tokens: 186,
      overlap: "前后各 6s",
      labels: ["报价金额", "优惠幅度", "金额冲突"],
      recall: 91,
      quality: "正常",
      before: "如果现在下单，可以优惠 3.5 万，落地大概 28.19 万左右。",
      after: "报价阶段 / 金额承诺 / 与报价单 #BJ-041 字段存在冲突。",
      evidence: "可回放到 A-1001 工牌麦 12:27:18"
    },
    {
      id: "chunk-quote-02",
      source: "销售话术 SOP",
      window: "报价阶段 / 价格确认",
      chunkId: "kb-sop-sales-c118",
      tokens: 142,
      overlap: "段落前后 1 句",
      labels: ["报价承诺", "价格异议缓解"],
      recall: 87,
      quality: "标签覆盖不足",
      before: "报价后需要复述优惠口径，并确认客户是否接受。",
      after: "命中报价承诺，但缺少成交意向负例用于排除。",
      evidence: "销售话术 SOP / v2025.05"
    },
    {
      id: "chunk-drive-01",
      source: "录音 ASR 派生证据",
      window: "12:28:01 - 12:29:28",
      chunkId: "kb-audio-drive-129-c02",
      tokens: 94,
      overlap: "前 8s / 后 10s",
      labels: ["试驾承接", "试驾时间", "串音待排除"],
      recall: 78,
      quality: "语义断裂",
      before: "可以安排试驾，下午两点后你们还有时间的话我帮你约。",
      after: "试驾承接与预约确认被切成两个 chunk，影响标签完整。",
      evidence: "B-2001 与 Hall-Mic 同窗重叠"
    }
  ];
  const sceneChunkPreviews: KnowledgeChunkPreview[] = knowledgeGraphPaths.map((path, index) => ({
    id: `scene-chunk-${index + 1}`,
    source: path.source,
    window: "运行时切片窗口",
    chunkId: path.chunk,
    tokens: 0,
    overlap: "由索引版本配置",
    labels: path.entities,
    recall: path.recall,
    quality: path.recall > 0 ? "正常" : "标签覆盖不足",
    before: `${path.sourceMeta}。`,
    after: `${path.index} 将按 ${sceneBinding?.version.version ?? "场景版本"} 构建，不补入汽车领域语义。`,
    evidence: sceneManifest?.data_contract_refs[index % Math.max(1, sceneManifest.data_contract_refs.length)] ?? "等待数据契约"
  }));
  const initialChunkPreviews = demoMode && projectionSource === "mock"
    ? automotiveChunkPreviews
    : sceneChunkPreviews;
  const automotiveLabelGaps: KnowledgeLabelGap[] = [
    {
      id: "gap-deal",
      type: "缺失标签",
      label: "成交意向",
      evidence: "客户询问落地价后出现“今天能定吗”的表达，但当前标签轨道未命中。",
      knowledge: "销售话术 SOP：报价后确认是否下单属于成交意向候选。",
      action: "生成候选标签并进入人工确认",
      severity: "高"
    },
    {
      id: "gap-quote",
      type: "冲突标签",
      label: "报价金额",
      evidence: "ASR 28.19 万与报价单 31.69 万不一致。",
      knowledge: "CRM FAQ：报价口径以报价单字段为准，ASR 保留为证据。",
      action: "进入标签冲突仲裁",
      severity: "高"
    },
    {
      id: "gap-testdrive",
      type: "低置信标签",
      label: "预约确认",
      evidence: "试驾承接 chunk 与预约确认 chunk 语义断裂。",
      knowledge: "试驾流程 SOP：试驾时间 + 客户确认构成预约确认。",
      action: "重切当前样本后加入标签补全",
      severity: "中"
    }
  ];
  const sceneLabelGaps: KnowledgeLabelGap[] = (sceneManifest?.release_requirements ?? []).map((requirement, index) => ({
    id: `scene-gap-${index + 1}`,
    type: "低置信标签",
    label: sceneManifest?.metrics.find((metric) => metric.metric_key === requirement.metric_key)?.display_name ?? requirement.metric_key,
    evidence: `${requirement.gate_kind} 尚需达到 ${(requirement.threshold_ppm / 10_000).toFixed(1)}%。`,
    knowledge: sceneManifest?.knowledge_index_refs.join(" / ") || "尚未配置知识索引",
    action: "生成候选并进入评测或人工确认",
    severity: requirement.gate_kind === "project_holdout" ? "高" : "中"
  }));
  const labelGaps = demoMode && projectionSource === "mock"
    ? automotiveLabelGaps
    : sceneLabelGaps.length > 0
      ? sceneLabelGaps
      : [{ id: "scene-gap-1", type: "缺失标签", label: "未配置门禁", evidence: "SceneProfile 未声明完整发布门禁。", knowledge: "无权威知识索引", action: "返回项目配置", severity: "高" } as KnowledgeLabelGap];
  const automotiveCompletionSuggestions: KnowledgeCompletionSuggestion[] = [
    {
      id: "sug-quote",
      title: "报价链路补全",
      from: "CRM FAQ + SOP + 报价证据包",
      proposal: "补齐成交意向候选，报价金额保留冲突状态，不直接写线上标签。",
      impact: "减少金额冲突 badcase 15 条",
      target: "labels",
      status: "待人审"
    },
    {
      id: "sug-drive",
      title: "试驾预约补全",
      from: "试驾流程 SOP + 录音 ASR chunk",
      proposal: "将试驾承接和预约确认合并为同一业务链路候选。",
      impact: "提升试驾标签完整率 8.2pp",
      target: "labels",
      status: "可生成草稿"
    }
  ];
  const sceneCompletionSuggestions: KnowledgeCompletionSuggestion[] = labelGaps.slice(0, 3).map((gap, index) => ({
    id: `scene-suggestion-${index + 1}`,
    title: `${gap.label}候选补全`,
    from: sceneManifest?.knowledge_index_refs.join(" / ") || "待配置知识索引",
    proposal: `${gap.action}；模型不能直接修改已发布标签或场景版本。`,
    impact: gap.evidence,
    target: "labels",
    status: "待人审"
  }));
  const completionSuggestions = demoMode && projectionSource === "mock"
    ? automotiveCompletionSuggestions
    : sceneCompletionSuggestions;
  const [selectedSourceId, setSelectedSourceId] = useState(knowledgeSources[0].id);
  const [selectedEvidenceId, setSelectedEvidenceId] = useState(evidenceLinks[0].id);
  const [selectedChunkId, setSelectedChunkId] = useState(initialChunkPreviews[0].id);
  const [selectedGapId, setSelectedGapId] = useState(labelGaps[0].id);
  const [selectedGraphPathId, setSelectedGraphPathId] = useState(knowledgeGraphPaths[0].id);
  const [chunkPreviews, setChunkPreviews] = useState<KnowledgeChunkPreview[]>(initialChunkPreviews);
  const [indexPatch, setIndexPatch] = useState(2);
  const [qualityPassCount, setQualityPassCount] = useState(6);
  const [completionCount, setCompletionCount] = useState(214);
  const [knowledgeAction, setKnowledgeAction] = useState<string | null>(null);
  const [selectedKnowledgeObject, setSelectedKnowledgeObject] = useState<KnowledgeObjectSelection>({ kind: "source", id: knowledgeSources[0].id });
  const [knowledgeFilters, setKnowledgeFilters] = useState({ query: "", status: "all", quality: "all" });
  const [knowledgeDetailMode, setKnowledgeDetailMode] = useState<"fields" | "lineage" | "actions">("fields");
  const [knowledgeDraftActions, setKnowledgeDraftActions] = useState<string[]>(["等待连接器、切片或标签补全动作"]);
  const [knowledgeNotice, setKnowledgeNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待知识库操作",
    detail: "连接器同步、切片重建、标签补全和质量检测会记录到当前知识版本。"
  });
  const [runRecords, setRunRecords] = useState<Array<[string, string, string]>>([
    ["12:41:08", "索引构建完成", "kb-index-v3.2 / R@5 84.6%"],
    ["12:18:32", "证据包同步", "音频证据包 / 37 个证据包 / 可回放"],
    ["11:52:20", "标签完整性门禁", "6/8 通过 / 成交意向缺失待处理"]
  ]);
  const selectKnowledgeObject = (selection: KnowledgeObjectSelection) => {
    setSelectedKnowledgeObject(selection);
    if (selection.kind === "source") setSelectedSourceId(selection.id);
    if (selection.kind === "evidence") setSelectedEvidenceId(selection.id);
    if (selection.kind === "chunk") setSelectedChunkId(selection.id);
    if (selection.kind === "gap") setSelectedGapId(selection.id);
    if (selection.kind === "path") {
      const path = knowledgeGraphPaths.find((item) => item.id === selection.id);
      if (path) {
        setSelectedGraphPathId(path.id);
        setSelectedSourceId(path.sourceId);
        setSelectedEvidenceId(path.evidenceId);
        setSelectedGapId(path.gapId);
      }
    }
  };
  useEffect(() => {
    if (focus?.module !== "knowledge" || !focus.objectId) return;
    const focusKind =
      focus.focusMode === "source"
        ? "source"
        : focus.focusMode === "chunk"
          ? "chunk"
          : focus.focusMode === "gap"
            ? "gap"
            : focus.focusMode === "path"
              ? "path"
              : focus.focusMode === "gate"
                ? "gate"
                : focus.focusMode === "effect"
                  ? "effect"
                  : focus.focusMode === "run"
                    ? "run"
                    : focus.objectKind === "knowledge"
                      ? "source"
                      : "evidence";
    selectKnowledgeObject({ kind: focusKind, id: focus.objectId });
    setKnowledgeNotice({
      status: "success",
      title: `已定位${focus.title ?? "知识对象"}`,
      detail: `${focus.origin?.label ?? "关联跳转"} → ${focusKind}:${focus.objectId}。`
    });
  }, [focus?.focusMode, focus?.module, focus?.objectId, focus?.title]);
  const selectedSource = knowledgeSources.find((source) => source.id === selectedSourceId) ?? knowledgeSources[0];
  const selectedEvidence = evidenceLinks.find((item) => item.id === selectedEvidenceId) ?? evidenceLinks[0];
  const selectedChunk = chunkPreviews.find((item) => item.id === selectedChunkId) ?? chunkPreviews[0];
  const selectedGap = labelGaps.find((item) => item.id === selectedGapId) ?? labelGaps[0];
  const selectedGraphPath = knowledgeGraphPaths.find((path) => path.id === selectedGraphPathId) ?? knowledgeGraphPaths[0];
  const selectedGraphSource = knowledgeSources.find((source) => source.id === selectedGraphPath.sourceId) ?? selectedSource;
  const selectedGraphEvidence = evidenceLinks.find((item) => item.id === selectedGraphPath.evidenceId) ?? selectedEvidence;
  const currentIndexVersion = `kb-index-v3.${indexPatch}`;
  const shortTrace = (trace?: string) => (trace ? trace.slice(0, 12) : "no-trace");
  const backendKnowledgeSourceId = (sourceId: string) =>
    demoMode && projectionSource === "mock"
      ? sourceId === "labels" || sourceId === "evidence" ? "ks_evidence_samples" : "ks_sales_policy"
      : sourceId;
  const backendKnowledgeIndexId = sceneManifest?.knowledge_index_refs[0] ?? "";
  const canBuildSceneIndex = Boolean(backendKnowledgeIndexId && sceneBinding);
  const knowledgeWritesEnabled = demoMode && projectionSource === "mock";
  const knowledgeWriteDisabledReason = knowledgeWritesEnabled ? "" : "知识同步、索引、质量、补全、人审与导出尚未绑定专用生产 executor；当前仅可查看 BFF 数据。";
  const knowledgeWriteButtonProps = (busy = false, extraReason = "") => ({
    disabled: busy || Boolean(knowledgeWriteDisabledReason) || Boolean(extraReason),
    title: knowledgeWriteDisabledReason || extraReason || undefined, "aria-describedby": knowledgeWriteDisabledReason ? "knowledge-write-disabled-reason" : undefined
  });
  const blockKnowledgeWrite = () => {
    if (knowledgeWritesEnabled) return false;
    setKnowledgeNotice({ status: "error", title: "生产写操作不可用", detail: `${knowledgeWriteDisabledReason}（EXECUTION_CONTRACT_NOT_CONFIGURED）` });
    return true;
  };
  useEffect(() => {
    if (activeTab === "connectors") selectKnowledgeObject({ kind: "source", id: selectedSourceId });
    if (activeTab === "indexing") selectKnowledgeObject({ kind: "chunk", id: selectedChunkId });
    if (activeTab === "graph") selectKnowledgeObject({ kind: "path", id: selectedGraphPathId });
    if (activeTab === "quality") selectKnowledgeObject({ kind: "gate", id: "切片质量" });
    if (activeTab === "effects") selectKnowledgeObject({ kind: "effect", id: "知识命中" });
    if (activeTab === "runs") selectKnowledgeObject({ kind: "run", id: runRecords[0]?.[0] ?? "latest" });
  }, [activeTab]);
  const selectGraphPath = (path: KnowledgeGraphPath) => {
    setSelectedGraphPathId(path.id);
    setSelectedSourceId(path.sourceId);
    setSelectedEvidenceId(path.evidenceId);
    setSelectedGapId(path.gapId);
    setSelectedKnowledgeObject({ kind: "path", id: path.id });
    setKnowledgeNotice({
      status: "success",
      title: "已定位知识消费路径",
      detail: `${path.source} → ${path.chunk} → ${path.entities.join(" / ")} → ${path.apps.join(" / ")}`
    });
  };
  const addRunRecord = (title: string, detail: string) => {
    const createdAt = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    setRunRecords((current) => [[createdAt, title, detail] as [string, string, string], ...current].slice(0, 8));
  };
  const runKnowledgeAction = (
    actionKey: string,
    pendingTitle: string,
    pendingDetail: string,
    successTitle: string,
    successDetail: string,
    onSuccess?: () => void,
    shouldFail = false
  ) => {
    if (blockKnowledgeWrite()) return;
    setKnowledgeAction(actionKey);
    setKnowledgeNotice({ status: "pending", title: pendingTitle, detail: pendingDetail });
    setKnowledgeAction(null);
    if (shouldFail) {
      setKnowledgeNotice({
        status: "error",
        title: "DEMO：连接测试失败",
        detail: `${selectedSource.name} 缺少演示授权或目录权限，请进入任务配置修复连接器。`
      });
      addRunRecord("DEMO：连接测试失败", `${selectedSource.name} / 授权或目录权限缺失`);
      return;
    }
    onSuccess?.();
    setKnowledgeDraftActions((current) => [`DEMO：${successTitle}：${successDetail}`, ...current].slice(0, 5));
    setKnowledgeNotice({ status: "success", title: `DEMO：${successTitle}`, detail: successDetail });
    addRunRecord(`DEMO：${successTitle}`, successDetail);
  };
  const testConnection = (source: KnowledgeSource) => {
    setSelectedSourceId(source.id);
    runKnowledgeAction(
      `test-${source.id}`,
      "正在测试知识源连接",
      `${source.name} 正在校验授权、目录、增量游标和脱敏策略。`,
      "知识源连接通过",
      `${source.name} 可读取 ${source.objects}，同步不会越过当前租户和项目边界。`,
      undefined,
      source.status === "异常"
    );
  };
  const openEvidence = () => {
    setKnowledgeNotice({
      status: "success",
      title: "已带证据上下文进入调听",
      detail: `${selectedEvidence.title} / ${selectedEvidence.window} / 标签轨道与 chunk 可回看。`
    });
    navigateToTarget({
      module: "listening",
      objectKind: "evidence",
      objectId: selectedEvidence.id,
      focusMode: "evidence",
      title: selectedEvidence.title,
      detail: `${selectedEvidence.recording} / ${selectedEvidence.window}`,
      origin: { label: "知识库 / 证据包", module: "knowledge", objectLabel: selectedEvidence.id }
    });
  };
  const openKnowledgeTarget = (target: ModuleDeepLink, label = "知识库关联") => {
    navigateToTarget({
      ...target,
      origin: { label, module: "knowledge", objectLabel: selectedKnowledgeObject.id }
    });
  };
  const targetForKnowledgeRoute = (route: ModuleKey, title: string): ModuleDeepLink => {
    if (route === "listening") {
      return { module: "listening", objectKind: "evidence", objectId: selectedGraphEvidence.id, focusMode: "evidence", title };
    }
    if (route === "labels") {
      return { module: "labels", tab: "schema", objectKind: "labelIntent", objectId: selectedGraphPath.gapId === "gap-testdrive" ? "testDrive" : "quote", title };
    }
    if (route === "assets") {
      return { module: "assets", tab: "lineage", objectKind: "asset", objectId: selectedGraphEvidence.id === "EVP-drive-129" ? "auris/audio/voice_segments" : "auris/label/event_tags", title };
    }
    if (route === "canvas") {
      return { module: "canvas", tab: "inputs", objectKind: "canvasNode", objectId: selectedGraphPath.sourceId === "sop" ? "ai" : "eventApi", title };
    }
    return { module: route, title };
  };
  const generateCandidateLabels = () => {
    if (blockKnowledgeWrite()) return;
    setKnowledgeAction("complete");
    setKnowledgeNotice({
      status: "pending",
      title: "正在生成候选标签",
      detail: `${selectedEvidence.title} 正在合并知识命中、录音窗口和标签缺口。`
    });
    setKnowledgeAction(null);
    setCompletionCount((value) => value + 3);
    addRunRecord("DEMO：候选标签已生成", `${selectedGap.label} / ${selectedEvidence.title} / 待人审`);
    setKnowledgeNotice({ status: "success", title: "DEMO：候选标签已生成", detail: `${selectedGap.label} 仅写入演示态标签补全草稿，不覆盖线上标签。` });
  };
  const addGapToEvaluation = () => {
    if (blockKnowledgeWrite()) return;
    addRunRecord("缺口样本加入评测集", `${selectedGap.label} / ${selectedGap.type} / ${selectedEvidence.id}`);
    setKnowledgeNotice({
      status: "success",
      title: "已加入评测回归",
      detail: `${selectedGap.label} 将作为知识辅助标签补全样本进入评测中心。`
    });
  };
  const rechunkCurrentSample = () => {
    if (blockKnowledgeWrite()) return;
    setKnowledgeAction("rechunk");
    setKnowledgeNotice({
      status: "pending",
      title: "正在重切当前样本",
      detail: `${selectedChunk.chunkId} 正在按语义窗口、标签边界和录音时间窗重建。`
    });
    setKnowledgeAction(null);
    setChunkPreviews((current) =>
      current.map((chunk) =>
        chunk.id === selectedChunk.id
          ? { ...chunk, tokens: Math.min(chunk.tokens + 28, 260), recall: Math.min(chunk.recall + 7, 96), overlap: "前后各 10s", quality: "正常", after: `${chunk.after} 已重切：保留标签边界和上下文窗口。` }
          : chunk
      )
    );
    addRunRecord("DEMO：切片样本已重切", `${selectedChunk.chunkId} / 语义窗口 + 标签边界`);
    setKnowledgeNotice({ status: "success", title: "DEMO：切片效果已更新", detail: `${selectedChunk.chunkId} 仅更新演示态预览。` });
  };
  const markChunkIssue = () => {
    if (blockKnowledgeWrite()) return;
    addRunRecord("切片问题已标记", `${selectedChunk.chunkId} / ${selectedChunk.quality} / ${selectedChunk.evidence}`);
    setKnowledgeNotice({
      status: "success",
      title: "切片问题已记录",
      detail: `${selectedChunk.chunkId} 将进入索引质量问题队列。`
    });
  };
  const addChunkToCompletion = () => {
    if (blockKnowledgeWrite()) return;
    setCompletionCount((value) => value + 1);
    addRunRecord("切片加入标签补全", `${selectedChunk.chunkId} / ${selectedChunk.labels.join("、")}`);
    setKnowledgeNotice({
      status: "success",
      title: "已加入标签补全样本",
      detail: `${selectedChunk.chunkId} 会作为 ${selectedChunk.labels[0]} 的补全证据。`
    });
  };
  const syncSource = async (source: KnowledgeSource) => {
    if (blockKnowledgeWrite()) return;
    if (source.id === "knowledge-unconfigured" || !sceneBinding) {
      setKnowledgeNotice({
        status: "error",
        title: "知识源同步被阻断",
        detail: "当前项目尚未绑定权威 SceneProfile 或知识源强 ID；请先完成场景发布和连接器配置。"
      });
      return;
    }
    setSelectedSourceId(source.id);
    const sourceRef = backendKnowledgeSourceId(source.id);
    setKnowledgeAction(`sync-${source.id}`);
    setKnowledgeNotice({
      status: "pending",
      title: "知识源同步中",
      detail: `${source.name} 正在通过 BFF 创建同步运行，后端资源 ${sourceRef}。`
    });
    try {
      const receipt = await syncKnowledgeSource(sourceRef, {
        source: "knowledge_module",
        ui_source_id: source.id,
        reason: `${source.name} 手动同步`,
        scope: source.scope,
        object_count: source.objects,
        ...(sceneBinding
          ? {
              scene_profile_id: sceneBinding.scene_profile_id,
              scene_profile_version_id: sceneBinding.scene_profile_version_id,
              scene_profile_snapshot_sha256: sceneBinding.manifest_sha256
            }
          : {})
      }, undefined, apiContext);
      const runState = await refreshBackendRunReceipt(receipt.data);
      const runStatus = operationStatusFromBackendRun(runState.status);
      const title = backendRunSucceeded(runState.status)
        ? "知识源同步完成"
        : backendRunFailed(runState.status)
          ? "知识源同步运行异常"
          : "知识源同步运行已创建";
      setKnowledgeAction(null);
      const detail = `${source.name} 已生成 ${runState.id}，当前${backendRunStatusLabel(runState.status)}，trace：${shortTrace(runState.trace_id)}。${
        backendRunSucceeded(runState.status) ? "" : "等待 worker 回写同步结果。"
      }`;
      setKnowledgeDraftActions((current) => [`${title}：${detail}`, ...current].slice(0, 5));
      setKnowledgeNotice({ status: runStatus, title, detail });
      addRunRecord(title, `${runState.id} / ${sourceRef} / ${shortTrace(runState.trace_id)}`);
    } catch (error) {
      setKnowledgeAction(null);
      setKnowledgeNotice({
        status: "error",
        title: "知识源同步失败",
        detail: error instanceof Error ? error.message : `${source.name} 同步请求未返回可用错误信息。`
      });
      addRunRecord("知识源同步失败", `${source.name} / ${sourceRef}`);
    }
  };
  const buildIndex = async () => {
    if (blockKnowledgeWrite()) return;
    if (!backendKnowledgeIndexId || !sceneBinding) {
      setKnowledgeNotice({
        status: "error",
        title: "索引构建被阻断",
        detail: "当前项目必须先绑定包含 knowledge_index_refs 的已发布 SceneProfile；不会回落到汽车销售默认索引。"
      });
      return;
    }
    setKnowledgeAction("index");
    setKnowledgeNotice({
      status: "pending",
      title: "索引构建中",
      detail: `${currentIndexVersion} 正在通过 BFF 创建索引构建运行，目标索引 ${backendKnowledgeIndexId}。`
    });
    try {
      const receipt = await buildKnowledgeIndex(backendKnowledgeIndexId, {
        source: "knowledge_module",
        reason: `${currentIndexVersion} 手动构建`,
        current_version: currentIndexVersion,
        selected_source_id: backendKnowledgeSourceId(selectedSource.id),
        chunk_id: selectedChunk.chunkId,
        quality_focus: selectedGap.label,
        scene_profile_id: sceneBinding.scene_profile_id,
        scene_profile_version_id: sceneBinding.scene_profile_version_id,
        scene_profile_snapshot_sha256: sceneBinding.manifest_sha256
      }, undefined, apiContext);
      const runState = await refreshBackendRunReceipt(receipt.data);
      const runStatus = operationStatusFromBackendRun(runState.status);
      const nextPatch = indexPatch + 1;
      const title = backendRunSucceeded(runState.status)
        ? "索引构建完成"
        : backendRunFailed(runState.status)
          ? "索引构建运行异常"
          : "索引构建运行已创建";
      setKnowledgeAction(null);
      if (backendRunSucceeded(runState.status)) {
        setIndexPatch((value) => value + 1);
      }
      const detail = `${
        backendRunSucceeded(runState.status) ? `kb-index-v3.${nextPatch}` : currentIndexVersion
      } 已创建 ${runState.id}，当前${backendRunStatusLabel(runState.status)}，trace：${shortTrace(runState.trace_id)}。${
        backendRunSucceeded(runState.status) ? "" : "等待索引 worker 回写版本号和召回指标。"
      }`;
      setKnowledgeDraftActions((current) => [`${title}：${detail}`, ...current].slice(0, 5));
      setKnowledgeNotice({ status: runStatus, title, detail });
      addRunRecord(title, `${runState.id} / ${backendKnowledgeIndexId} / ${shortTrace(runState.trace_id)}`);
    } catch (error) {
      setKnowledgeAction(null);
      setKnowledgeNotice({
        status: "error",
        title: "索引构建失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
      addRunRecord("索引构建失败", `${backendKnowledgeIndexId} / ${currentIndexVersion}`);
    }
  };
  const runQualityCheck = () => {
    runKnowledgeAction(
      "quality",
      "质量检测运行中",
      "正在检查新鲜度、切片质量、标签覆盖、实体冲突、证据可回放和召回质量。",
      "质量门禁已更新",
      "7/8 门禁通过，标签覆盖和切片语义断裂仍建议进入评测中心补样本。",
      () => setQualityPassCount(7)
    );
  };
  const exportEffectReport = () => {
    runKnowledgeAction(
      "report",
      "效果报告生成中",
      "正在汇总证据包输入、知识命中、标签补齐、人审接受和回归沉淀。",
      "效果报告已生成",
      "EXP-KB-EFFECT 已进入报告中心，可回到洞察或资产目录查看。"
    );
  };
  const createKnowledgeReviewTask = () => {
    if (blockKnowledgeWrite()) return;
    const title = selectedKnowledgeObject.kind === "chunk" ? selectedChunk.chunkId : selectedGap.label;
    addRunRecord("人工处理任务已创建", `${title} / 知识库质量队列 / 待确认`);
    setKnowledgeDraftActions((current) => [`人工处理任务：${title} 已进入知识库质量队列`, ...current].slice(0, 5));
    setKnowledgeNotice({
      status: "success",
      title: "已创建人工处理任务",
      detail: `${title} 已带上证据窗口、资产 key 和标签上下文。`
    });
  };
  const exportCurrentKnowledgeData = () => {
    if (blockKnowledgeWrite()) return;
    const scopeLabel = KNOWLEDGE_TAB_LABELS[activeTab] ?? "知识总览";
    addRunRecord("当前知识数据已导出", `${scopeLabel} / 查询 ${knowledgeFilters.query || "全部"} / JSON + Markdown`);
    setKnowledgeNotice({
      status: "success",
      title: "当前视图已导出",
      detail: `${scopeLabel} 的对象列表、详情字段、证据引用和当前筛选已生成导出草稿。`
    });
  };
  const completionPanel = (
    <section className="module-panel wide knowledge-completion-panel">
      <PanelHeader title="标签完整性辅助" subtitle="知识库用 SOP、FAQ、产品资料和历史样本校验录音标签是否完整" icon={<Tags size={16} />} />
      <div className="knowledge-completion-layout">
        <div className="knowledge-evidence-list">
          {evidenceLinks.map((item) => (
            <button
              key={item.id}
              type="button"
              className={item.id === selectedEvidence.id ? "active" : ""}
              onClick={() => {
                setSelectedEvidenceId(item.id);
                navigateToTarget({
                  module: "listening",
                  objectKind: "evidence",
                  objectId: item.id,
                  focusMode: "evidence",
                  title: item.title,
                  detail: `${item.recording} / ${item.window}`,
                  origin: { label: "知识库 / 标签完整性辅助", module: "knowledge", objectLabel: item.id }
                });
              }}
            >
              <span>{item.id}</span>
              <strong>{item.title}</strong>
              <em>{item.recording} · {item.window}</em>
              <b>{item.confidence}%</b>
            </button>
          ))}
        </div>
        <div className="knowledge-evidence-detail">
          <div className="knowledge-section-title">
            <Headphones size={15} />
            <span>录音派生证据</span>
          </div>
          <strong>{selectedEvidence.title}</strong>
          <p>{selectedEvidence.recording} / {selectedEvidence.window}</p>
          <div className="knowledge-pill-row">
            {selectedEvidence.derivedAssets.map((asset) => <span key={asset}>{asset}</span>)}
          </div>
          <div className="knowledge-fact-grid">
            <div><span>命中知识</span><strong>{selectedEvidence.knowledgeHits.join(" / ")}</strong></div>
            <div><span>已有关联标签</span><strong>{selectedEvidence.linkedLabels.join(" / ")}</strong></div>
            <div><span>待补缺口</span><strong>{selectedEvidence.gaps.join(" / ")}</strong></div>
          </div>
        </div>
        <div className="knowledge-gap-list">
          {labelGaps.map((gap) => (
            <button key={gap.id} type="button" className={gap.id === selectedGap.id ? "active" : ""} onClick={() => setSelectedGapId(gap.id)}>
              <span>{gap.type}</span>
              <strong>{gap.label}</strong>
              <em>{gap.action}</em>
              <b>{gap.severity}</b>
            </button>
          ))}
        </div>
      </div>
      <div className="knowledge-action-row">
        <button type="button" onClick={openEvidence}><Headphones size={14} />查看录音证据</button>
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "complete")} onClick={generateCandidateLabels}><Sparkles size={14} />{knowledgeAction === "complete" ? "生成中" : "生成候选标签"}</button>
        <button type="button" onClick={() => setActiveModule("labels")}><Tags size={14} />进入标签治理</button>
        <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={addGapToEvaluation}><BarChart3 size={14} />加入评测集</button>
      </div>
    </section>
  );
  const connectorPanel = (
    <section className="module-panel wide knowledge-connector-panel">
      <PanelHeader title="知识连接器" subtitle="知识源绑定、证据关系和同步状态；真实接入仍进入任务配置" icon={<Database size={16} />} />
      <div className="knowledge-connector-layout">
        <div className="knowledge-source-grid">
          {knowledgeSources.map((source) => (
            <button
              key={source.id}
              type="button"
              className={`knowledge-source-card ${selectedSource.id === source.id ? "active" : ""} ${source.status === "异常" ? "danger" : source.status === "待同步" ? "warn" : "ok"}`}
              onClick={() => setSelectedSourceId(source.id)}
            >
              <span>{source.kind}</span>
              <strong>{source.name}</strong>
              <em>{source.scope}</em>
              <small>{source.owner} · {source.objects}</small>
              <i>
                <b style={{ width: `${source.quality}%` }} />
              </i>
              <div>
                <b>{source.status}</b>
                <strong>{source.quality}</strong>
              </div>
            </button>
          ))}
        </div>
        <div className="knowledge-source-detail">
          <div className="knowledge-section-title">
            <Link2 size={15} />
            <span>来源关系</span>
          </div>
          <strong>{selectedSource.name}</strong>
          <p>{selectedSource.kind} / {selectedSource.scope} / {selectedSource.owner}</p>
          <div className="knowledge-relation-list">
            {selectedSource.relations.map(([label, value, detail]) => (
              <div key={`${label}-${value}`}>
                <span>{label}</span>
                <strong>{value}</strong>
                <em>{detail}</em>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="knowledge-action-row">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === `test-${selectedSource.id}`)} onClick={() => testConnection(selectedSource)}>
          <ShieldCheck size={14} />
          {knowledgeAction === `test-${selectedSource.id}` ? "测试中" : "测试连接"}
        </button>
        <button
          type="button"
          {...knowledgeWriteButtonProps(knowledgeAction === `sync-${selectedSource.id}`)}
          data-action-key="knowledge-sync-source"
          onClick={() => syncSource(selectedSource)}
        >
          <RotateCcw size={14} />
          {knowledgeAction === `sync-${selectedSource.id}` ? "同步中" : "同步一次"}
        </button>
        <button type="button" className="primary" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedSource.route, selectedSource.name), "知识库 / 来源关系")}>
          <GitBranch size={14} />
          {selectedSource.route === "listening" ? "查看证据详情" : selectedSource.route === "labels" ? "查看标签详情" : "查看任务节点"}
        </button>
      </div>
    </section>
  );
  const lifecyclePanel = (
    <section className="module-panel wide">
      <PanelHeader title="知识生命周期" subtitle="连接器 → 清洗切片 → 索引 → 质量门禁 → 效果回写" icon={<BrainCircuit size={16} />} />
      <div className="knowledge-lifecycle">
        {lifecycleStages.map(([label, detail, route, tone], index) => (
          <button key={label} type="button" className={`knowledge-lifecycle-step ${tone}`} onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(route, label), "知识库 / 生命周期")}>
            <span>{index + 1}</span>
            <strong>{label}</strong>
            <em>{detail}</em>
            {index < lifecycleStages.length - 1 && <ArrowRight size={16} />}
          </button>
        ))}
      </div>
    </section>
  );
  const indexingPanel = (
    <section className="module-panel wide knowledge-index-panel">
      <PanelHeader title="索引构建" subtitle={`${currentIndexVersion} / 混合召回索引 / 影子评测后发布`} icon={<Search size={16} />} />
      <div className="knowledge-index-layout">
        <div className="knowledge-index-version">
          <span>当前索引版本</span>
          <strong>{currentIndexVersion}</strong>
          <p>向量、关键词和实体关系共同参与召回；知识库只发布索引版本，不覆盖原始数据资产。</p>
          <button
            type="button"
            {...knowledgeWriteButtonProps(knowledgeAction === "index", !canBuildSceneIndex ? "先绑定包含 knowledge_index_refs 的已发布 SceneProfile" : "")}
            data-action-key="knowledge-build-index"
            onClick={buildIndex}
          >
            <Sparkles size={14} />
            {knowledgeAction === "index" ? "构建中" : "构建索引"}
          </button>
        </div>
        <div className="knowledge-index-policy">
          {indexPolicies.map(([label, value, detail]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
              <em>{detail}</em>
            </div>
          ))}
        </div>
      </div>
      <div className="knowledge-chunk-preview">
        <div className="knowledge-chunk-list">
          {chunkPreviews.map((chunk) => (
            <button key={chunk.id} type="button" className={chunk.id === selectedChunk.id ? "active" : ""} onClick={() => setSelectedChunkId(chunk.id)}>
              <span>{chunk.source}</span>
              <strong>{chunk.chunkId}</strong>
              <em>{chunk.window} · {chunk.tokens} tokens · {chunk.overlap}</em>
              <b>{chunk.quality}</b>
            </button>
          ))}
        </div>
        <div className="knowledge-chunk-compare">
          <div className="knowledge-section-title">
            <Layers size={15} />
            <span>切片前后对比</span>
          </div>
          <div className="knowledge-chunk-meta">
            <strong>{selectedChunk.chunkId}</strong>
            <span>召回分 {selectedChunk.recall}</span>
            <span>{selectedChunk.evidence}</span>
          </div>
          <div className="knowledge-chunk-columns">
            <div>
              <span>原始内容</span>
              <p>{selectedChunk.before}</p>
            </div>
            <ArrowRight size={16} />
            <div>
              <span>语义 chunk</span>
              <p>{selectedChunk.after}</p>
            </div>
          </div>
          <div className="knowledge-pill-row">
            {selectedChunk.labels.map((label) => <span key={label}>{label}</span>)}
          </div>
          <div className="knowledge-chunk-score"><i><b style={{ width: `${selectedChunk.recall}%` }} /></i><span>{selectedChunk.quality}</span></div>
        </div>
      </div>
      <div className="knowledge-action-row">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "rechunk")} onClick={rechunkCurrentSample}><RotateCcw size={14} />{knowledgeAction === "rechunk" ? "重切中" : "重切当前样本"}</button>
        <button type="button" {...knowledgeWriteButtonProps()} onClick={markChunkIssue}><AlertTriangle size={14} />标记切片问题</button>
        <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={addChunkToCompletion}><Tags size={14} />加入标签补全样本</button>
      </div>
    </section>
  );
  const graphPanel = (
    <section className="module-panel wide knowledge-visual-panel">
      <PanelHeader title="知识可视化" subtitle="把知识源、chunk、实体/标签、索引和业务应用放在同一张消费图里" icon={<GitBranch size={16} />} />
      <div className="knowledge-visual-layout">
        <div className="knowledge-visual-map" role="img" aria-label="知识对象消费路径图">
          <div className="knowledge-map-head" aria-hidden="true">
            {["知识源", "语义切片", "实体 / 标签", "召回索引", "消费场景"].map((label) => <span key={label}>{label}</span>)}
          </div>
          <div className="knowledge-map-rows">
            {knowledgeGraphPaths.map((path) => (
              <button
                key={path.id}
                type="button"
                className={`knowledge-map-row ${path.tone} ${path.id === selectedGraphPath.id ? "active" : ""}`}
                onClick={() => selectGraphPath(path)}
              >
                <span className="kg-node source">
                  <strong>{path.source}</strong>
                  <em>{path.sourceMeta}</em>
                </span>
                <i className="kg-edge" />
                <span className="kg-node chunk">
                  <strong>{path.chunk}</strong>
                  <em>{path.chunkMeta}</em>
                </span>
                <i className="kg-edge" />
                <span className="kg-node tags">
                  <strong>{path.entities[0]}</strong>
                  <em>{path.entities.slice(1).join(" / ")}</em>
                </span>
                <i className="kg-edge" />
                <span className="kg-node index">
                  <strong>{path.index}</strong>
                  <em>{path.quality}</em>
                </span>
                <i className="kg-edge" />
                <span className="kg-node app">
                  <strong>{path.apps[0]}</strong>
                  <em>{path.apps.slice(1).join(" / ")}</em>
                </span>
              </button>
            ))}
          </div>
        </div>
        <aside className={`knowledge-visual-detail ${selectedGraphPath.tone}`}>
          <div className="knowledge-section-title">
            <Link2 size={15} />
            <span>当前路径解释</span>
          </div>
          <strong>{selectedGraphPath.source} → {selectedGraphPath.apps[0]}</strong>
          <p>{selectedGraphPath.risk}</p>
          <div className="knowledge-visual-kpis">
            <div><span>召回分</span><strong>{selectedGraphPath.recall}%</strong></div>
            <div><span>标签/实体</span><strong>{selectedGraphPath.entities.length}</strong></div>
            <div><span>消费应用</span><strong>{selectedGraphPath.apps.length}</strong></div>
          </div>
          <div className="knowledge-visual-trace">
            <div><span>来源对象</span><strong>{selectedGraphSource.name}</strong><em>{selectedGraphSource.objects}</em></div>
            <div><span>证据窗口</span><strong>{selectedGraphEvidence.recording}</strong><em>{selectedGraphEvidence.window}</em></div>
            <div><span>索引版本</span><strong>{currentIndexVersion}</strong><em>{selectedGraphPath.index}</em></div>
          </div>
          <div className="knowledge-pill-row">
            {selectedGraphPath.entities.map((entity) => <span key={entity}>{entity}</span>)}
          </div>
        </aside>
      </div>
      <div className="knowledge-graph-actions">
        <button type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedGraphPath.route, selectedGraphPath.apps[0]), "知识库 / 知识图谱")}>打开消费场景详情</button>
        <button type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute("assets", selectedGraphPath.chunk), "知识库 / 知识图谱")}>查看资产血缘</button>
        <button type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute("labels", selectedGraphPath.entities[0]), "知识库 / 知识图谱")}>查看标签样本</button>
        <button type="button" className="primary" {...knowledgeWriteButtonProps(knowledgeAction === "complete")} onClick={generateCandidateLabels}>
          {knowledgeAction === "complete" ? "生成中" : "基于路径生成候选"}
        </button>
      </div>
    </section>
  );
  const qualityPanel = (
    <section className="module-panel wide knowledge-quality-panel">
      <PanelHeader title="知识质量与标签完整性门禁" subtitle={`${qualityPassCount}/8 通过，覆盖切片质量、证据可回放、标签完整性和召回质量`} icon={<Gauge size={16} />} />
      <div className="knowledge-quality-matrix">
        {qualityGates.map((gate) => (
          <button key={gate.label} type="button" className={`knowledge-quality-card ${gate.state}`} onClick={gate.state === "warn" ? () => openKnowledgeTarget({ module: "evaluation", tab: "badcase", objectKind: "evaluationBadcase", objectId: "T-8812", title: gate.label }, "知识库 / 质量门禁") : undefined}>
            <span>{gate.label}</span>
            <strong>{gate.value}</strong>
            <em>{gate.detail}</em>
          </button>
        ))}
      </div>
      <div className="knowledge-gap-detail">
        <div>
          <span>{selectedGap.type}</span>
          <strong>{selectedGap.label}</strong>
          <p>{selectedGap.evidence}</p>
        </div>
        <div>
          <span>知识依据</span>
          <strong>{selectedGap.knowledge}</strong>
          <p>{selectedGap.action}</p>
        </div>
      </div>
      <div className="knowledge-action-row">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "quality")} onClick={runQualityCheck}>
          <Gauge size={14} />
          {knowledgeAction === "quality" ? "检测中" : "运行质量检测"}
        </button>
        <button type="button" onClick={() => openKnowledgeTarget({ module: "evaluation", tab: "badcase", objectKind: "evaluationBadcase", objectId: "T-8812", title: selectedGap.label }, "知识库 / 标签完整性门禁")}>查看评测阻断详情</button>
      </div>
    </section>
  );
  const effectPanel = (
    <section className="module-panel wide knowledge-effect-panel">
      <PanelHeader title="效果展示" subtitle="证据包输入、知识命中、标签补齐、人审接受和回归沉淀" icon={<BarChart3 size={16} />} />
      <div className="knowledge-effect-funnel">
        {effectStages.map((stage, index) => (
          <button
            key={stage.label}
            type="button"
            className={`knowledge-effect-step ${stage.tone}`}
            style={{ "--funnel-width": `${stage.rate}%` } as CSSProperties}
            onClick={() => openKnowledgeTarget(index < 2 ? { module: "knowledge", tab: "indexing", objectKind: "knowledge", objectId: selectedChunk.id, focusMode: "chunk", title: stage.label } : index === 3 ? { module: "listening", objectKind: "evidence", objectId: selectedEvidence.id, focusMode: "evidence", title: stage.label } : { module: "insights", objectKind: "insightFact", objectId: "fact-turn-2", title: stage.label }, "知识库 / 效果漏斗")}
          >
            <span>{index + 1}</span>
            <strong>{stage.label}</strong>
            <b>{stage.value}</b>
            <em>{stage.drop}</em>
            <i />
          </button>
        ))}
      </div>
      <div className="knowledge-suggestion-list">
        {completionSuggestions.map((suggestion) => (
          <button key={suggestion.id} type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(suggestion.target, suggestion.title), "知识库 / 补全建议")}>
            <span>{suggestion.from}</span>
            <strong>{suggestion.title}</strong>
            <em>{suggestion.proposal}</em>
            <b>{suggestion.impact} · {suggestion.status}</b>
          </button>
        ))}
      </div>
      <div className="knowledge-effect-actions">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "report")} onClick={exportEffectReport}>
          <Download size={14} />
          {knowledgeAction === "report" ? "生成中" : "导出效果报告"}
        </button>
        <button type="button" onClick={() => openKnowledgeTarget({ module: "insights", objectKind: "insightFact", objectId: "fact-turn-2", title: "知识补全贡献" }, "知识库 / 效果报告")}>进入业务洞察详情</button>
        <button type="button" onClick={() => openKnowledgeTarget({ module: "assets", tab: "lineage", objectKind: "asset", objectId: "auris/eval/quality_metrics", focusMode: "lineage", title: "知识效果报告资产" }, "知识库 / 效果报告")}>查看报告资产</button>
      </div>
    </section>
  );
  const runPanel = (
    <section className="module-panel wide knowledge-run-panel">
      <PanelHeader title="运行记录" subtitle="同步、切片、标签补全、质量检测和效果导出都会形成当前知识版本的运行记录" icon={<Activity size={16} />} />
      <div className="knowledge-run-summary">
        {[
          ["当前索引", currentIndexVersion, "影子评测后发布"],
          ["补全样本", `${completionCount}`, "候选标签 / 人审"],
          ["切片问题", `${chunkPreviews.filter((chunk) => chunk.quality !== "正常").length}`, "可标记 / 可重切"]
        ].map(([label, value, detail]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{detail}</em>
          </div>
        ))}
      </div>
      <TimelineList items={runRecords} />
    </section>
  );

  const matchesKnowledgeQuery = (...parts: string[]) => {
    const query = knowledgeFilters.query.trim().toLowerCase();
    if (!query) return true;
    return parts.some((part) => part.toLowerCase().includes(query));
  };
  const filteredSources = knowledgeSources.filter((source) => {
    const statusMatched = knowledgeFilters.status === "all" || source.status === knowledgeFilters.status;
    return statusMatched && matchesKnowledgeQuery(source.name, source.kind, source.scope, source.owner, source.objects);
  });
  const filteredChunks = chunkPreviews.filter((chunk) => {
    const qualityMatched =
      knowledgeFilters.status === "all" ||
      chunk.quality === knowledgeFilters.status ||
      (knowledgeFilters.status === "问题" && chunk.quality !== "正常");
    return qualityMatched && matchesKnowledgeQuery(chunk.source, chunk.window, chunk.chunkId, chunk.labels.join(" "), chunk.evidence);
  });
  const filteredEvidenceLinks = evidenceLinks.filter((item) =>
    matchesKnowledgeQuery(item.id, item.title, item.recording, item.window, item.knowledgeHits.join(" "), item.linkedLabels.join(" "))
  );
  const filteredGaps = labelGaps.filter((gap) => {
    const severityMatched = knowledgeFilters.status === "all" || gap.severity === knowledgeFilters.status || gap.type === knowledgeFilters.status;
    return severityMatched && matchesKnowledgeQuery(gap.type, gap.label, gap.evidence, gap.knowledge, gap.action);
  });
  const filteredGraphPaths = knowledgeGraphPaths.filter((path) =>
    matchesKnowledgeQuery(path.source, path.sourceMeta, path.chunk, path.entities.join(" "), path.apps.join(" "), path.risk)
  );
  const filteredQualityGates = qualityGates.filter((gate) => {
    const statusMatched = knowledgeFilters.status === "all" || gate.state === knowledgeFilters.status || gate.label === knowledgeFilters.status;
    return statusMatched && matchesKnowledgeQuery(gate.label, gate.value, gate.detail);
  });
  const filteredEffects = effectStages.filter((stage) => matchesKnowledgeQuery(stage.label, stage.value, stage.drop));
  const filteredRuns = runRecords.filter(([time, title, detail]) => matchesKnowledgeQuery(time, title, detail));
  const selectedGate = qualityGates.find((gate) => gate.label === selectedKnowledgeObject.id) ?? qualityGates[0];
  const selectedEffect = effectStages.find((stage) => stage.label === selectedKnowledgeObject.id) ?? effectStages[0];
  const selectedRun = runRecords.find(([time]) => time === selectedKnowledgeObject.id) ?? runRecords[0];
  const knowledgeStatusOptions =
    activeTab === "connectors"
      ? [
          ["all", "全部状态"],
          ["已同步", "已同步"],
          ["待同步", "待同步"],
          ["异常", "异常"]
        ]
      : activeTab === "indexing"
        ? [
            ["all", "全部切片"],
            ["正常", "正常"],
            ["问题", "仅问题"],
            ["语义断裂", "语义断裂"],
            ["标签覆盖不足", "标签覆盖不足"]
          ]
        : activeTab === "quality"
          ? [
              ["all", "全部门禁"],
              ["ok", "通过"],
              ["warn", "观察"],
              ["risk", "高风险"]
            ]
          : activeTab === "overview"
            ? [
                ["all", "全部对象"],
                ["高", "高优先"],
                ["中", "中优先"],
                ["缺失标签", "缺失标签"],
                ["冲突标签", "冲突标签"]
              ]
            : [["all", "全部对象"]];
  const overviewItems: KnowledgeListItem[] = [
    ...filteredGaps.map((gap) => ({
      id: gap.id,
      kind: "gap" as const,
      title: gap.label,
      meta: `${gap.type} / ${gap.severity}优先`,
      detail: gap.action,
      status: gap.type,
      score: gap.severity,
      tone: (gap.severity === "高" ? "red" : gap.severity === "中" ? "amber" : "green") as KnowledgeListItem["tone"],
      onSelect: () => selectKnowledgeObject({ kind: "gap", id: gap.id })
    })),
    ...filteredEvidenceLinks.map((item) => ({
      id: item.id,
      kind: "evidence" as const,
      title: item.title,
      meta: `${item.recording} / ${item.window}`,
      detail: item.gaps.join(" / "),
      status: "证据包",
      score: `${item.confidence}%`,
      tone: (item.confidence < 80 ? "amber" : "blue") as KnowledgeListItem["tone"],
      onSelect: () => {
        setSelectedEvidenceId(item.id);
        navigateToTarget({
          module: "listening",
          objectKind: "evidence",
          objectId: item.id,
          focusMode: "evidence",
          title: item.title,
          detail: `${item.recording} / ${item.window}`,
          origin: { label: "知识库 / 待处理对象", module: "knowledge", objectLabel: item.id }
        });
      }
    }))
  ];
  const sourceItems: KnowledgeListItem[] = filteredSources.map((source) => ({
    id: source.id,
    kind: "source",
    title: source.name,
    meta: `${source.kind} / ${source.scope}`,
    detail: `${source.owner} · ${source.objects}`,
    status: source.status,
    score: source.quality,
    tone: source.status === "异常" ? "red" : source.status === "待同步" ? "amber" : "green",
    onSelect: () => selectKnowledgeObject({ kind: "source", id: source.id })
  }));
  const chunkItems: KnowledgeListItem[] = filteredChunks.map((chunk) => ({
    id: chunk.id,
    kind: "chunk",
    title: chunk.chunkId,
    meta: `${chunk.source} / ${chunk.window}`,
    detail: `${chunk.tokens} tokens · ${chunk.overlap}`,
    status: chunk.quality,
    score: chunk.recall,
    tone: chunk.quality === "正常" ? "green" : chunk.quality === "语义断裂" || chunk.quality === "跨录音边界" ? "red" : "amber",
    onSelect: () => selectKnowledgeObject({ kind: "chunk", id: chunk.id })
  }));
  const graphItems: KnowledgeListItem[] = filteredGraphPaths.map((path) => ({
    id: path.id,
    kind: "path",
    title: path.source,
    meta: `${path.chunk} / ${path.index}`,
    detail: path.apps.join(" / "),
    status: path.quality,
    score: `${path.recall}%`,
    tone: path.tone,
    onSelect: () => selectGraphPath(path)
  }));
  const qualityItems: KnowledgeListItem[] = filteredQualityGates.map((gate) => ({
    id: gate.label,
    kind: "gate",
    title: gate.label,
    meta: gate.detail,
    detail: gate.state === "ok" ? "可发布" : gate.state === "warn" ? "需要观察或补样本" : "需人工确认",
    status: gate.state === "ok" ? "通过" : gate.state === "warn" ? "观察" : "高风险",
    score: gate.value,
    tone: gate.state === "ok" ? "green" : gate.state === "warn" ? "amber" : "red",
    onSelect: () => selectKnowledgeObject({ kind: "gate", id: gate.label })
  }));
  const effectItems: KnowledgeListItem[] = filteredEffects.map((stage) => ({
    id: stage.label,
    kind: "effect",
    title: stage.label,
    meta: stage.drop,
    detail: `当前量 ${stage.value} / 转化 ${stage.rate}%`,
    status: "效果指标",
    score: stage.value,
    tone: stage.tone as KnowledgeListItem["tone"],
    onSelect: () => selectKnowledgeObject({ kind: "effect", id: stage.label })
  }));
  const runItems: KnowledgeListItem[] = filteredRuns.map(([time, title, detail]) => ({
    id: time,
    kind: "run",
    title,
    meta: time,
    detail,
    status: "运行记录",
    score: detail.includes("失败") ? "异常" : "完成",
    tone: detail.includes("失败") ? "red" : "blue",
    onSelect: () => selectKnowledgeObject({ kind: "run", id: time })
  }));
  const renderObjectList = (title: string, subtitle: string, items: KnowledgeListItem[]) => (
    <section className="module-panel knowledge-v2-column knowledge-v2-left">
      <PanelHeader title={title} subtitle={subtitle} icon={<ListFilter size={16} />} />
      <div className="knowledge-v2-list" role="list">
        {items.length === 0 ? (
          <div className="knowledge-v2-empty">
            <Search size={16} />
            <strong>当前筛选无对象</strong>
            <span>清空关键词或切换状态后再查看具体数据。</span>
          </div>
        ) : (
          items.map((item) => {
            const active = selectedKnowledgeObject.kind === item.kind && selectedKnowledgeObject.id === item.id;
            return (
              <button key={`${item.kind}-${item.id}`} type="button" className={`knowledge-v2-list-item ${active ? "active" : ""} ${item.tone ?? "blue"}`} onClick={item.onSelect}>
                <span>{item.status ?? item.kind}</span>
                <strong>{item.title}</strong>
                <em>{item.meta}</em>
                <small>{item.detail}</small>
                {item.score !== undefined && <b>{item.score}</b>}
              </button>
            );
          })
        )}
      </div>
    </section>
  );
  const renderFieldGrid = (rows: Array<[string, string]>) => (
    <div className="knowledge-v2-field-grid">
      {rows.map(([label, value]) => (
        <div key={`${label}-${value}`}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
  const detailModel = (() => {
    if (selectedKnowledgeObject.kind === "source") {
      return {
        badge: "知识源",
        title: selectedSource.name,
        subtitle: `${selectedSource.kind} / ${selectedSource.scope}`,
        rows: [
          ["对象 ID", selectedSource.id],
          ["资产 Key", `auris/knowledge/sources/${selectedSource.id}`],
          ["分区", "极光中心店 / 当前租户 / 最近 24 小时"],
          ["状态", selectedSource.status],
          ["质量分", `${selectedSource.quality}`],
          ["数据规模", selectedSource.objects]
        ] as Array<[string, string]>,
        lineage: selectedSource.relations.map(([label, value, detail]) => [label, `${value} · ${detail}`] as [string, string]),
        tags: [selectedSource.owner, selectedSource.kind, selectedSource.status],
        actions: (
          <>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={() => testConnection(selectedSource)}><ShieldCheck size={14} />测试连接</button>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={() => syncSource(selectedSource)}><RotateCcw size={14} />同步一次</button>
            <button type="button" className="primary" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedSource.route, selectedSource.name), "知识库 / 知识源下游")}><GitBranch size={14} />打开下游详情</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "chunk") {
      return {
        badge: "知识切片",
        title: selectedChunk.chunkId,
        subtitle: `${selectedChunk.source} / ${selectedChunk.window}`,
        rows: [
          ["对象 ID", selectedChunk.id],
          ["资产 Key", `auris/knowledge/chunks/${selectedChunk.chunkId}`],
          ["Token 数", `${selectedChunk.tokens}`],
          ["重叠策略", selectedChunk.overlap],
          ["召回分", `${selectedChunk.recall}`],
          ["质量状态", selectedChunk.quality]
        ] as Array<[string, string]>,
        lineage: [
          ["来源证据", selectedChunk.evidence],
          ["原始内容", selectedChunk.before],
          ["语义 chunk", selectedChunk.after]
        ] as Array<[string, string]>,
        tags: selectedChunk.labels,
        actions: (
          <>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={rechunkCurrentSample}><RotateCcw size={14} />重切样本</button>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={markChunkIssue}><AlertTriangle size={14} />标记问题</button>
            <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={addChunkToCompletion}><Tags size={14} />加入补全</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "evidence") {
      return {
        badge: "证据包",
        title: selectedEvidence.title,
        subtitle: `${selectedEvidence.recording} / ${selectedEvidence.window}`,
        rows: [
          ["对象 ID", selectedEvidence.id],
          ["资产 Key", `auris/evidence/packs/${selectedEvidence.id}`],
          ["置信度", `${selectedEvidence.confidence}%`],
          ["派生资产", selectedEvidence.derivedAssets.join(" / ")],
          ["知识命中", selectedEvidence.knowledgeHits.join(" / ")],
          ["待补缺口", selectedEvidence.gaps.join(" / ")]
        ] as Array<[string, string]>,
        lineage: selectedEvidence.derivedAssets.map((asset) => ["派生资产", asset] as [string, string]),
        tags: selectedEvidence.linkedLabels,
        actions: (
          <>
            <button type="button" onClick={openEvidence}><Headphones size={14} />查看录音</button>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={generateCandidateLabels}><Sparkles size={14} />生成候选</button>
            <button type="button" className="primary" onClick={() => openKnowledgeTarget({ module: "labels", tab: "schema", objectKind: "labelIntent", objectId: selectedGap.id === "gap-testdrive" ? "testDrive" : "quote", title: selectedGap.label }, "知识库 / 证据包标签")}><Tags size={14} />标签详情</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "gap") {
      return {
        badge: selectedGap.type,
        title: selectedGap.label,
        subtitle: `${selectedGap.severity}优先 / ${selectedGap.action}`,
        rows: [
          ["对象 ID", selectedGap.id],
          ["资产 Key", `auris/knowledge/gaps/${selectedGap.id}`],
          ["缺口类型", selectedGap.type],
          ["严重度", selectedGap.severity],
          ["证据", selectedGap.evidence],
          ["知识依据", selectedGap.knowledge]
        ] as Array<[string, string]>,
        lineage: [
          ["证据", selectedGap.evidence],
          ["知识依据", selectedGap.knowledge],
          ["建议动作", selectedGap.action]
        ] as Array<[string, string]>,
        tags: [selectedGap.label, selectedGap.type, selectedGap.severity],
        actions: (
          <>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={generateCandidateLabels}><Sparkles size={14} />生成候选</button>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={addGapToEvaluation}><BarChart3 size={14} />加入评测</button>
            <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={createKnowledgeReviewTask}><UserCheck size={14} />建人审任务</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "path") {
      return {
        badge: "消费路径",
        title: `${selectedGraphPath.source} → ${selectedGraphPath.apps[0]}`,
        subtitle: selectedGraphPath.risk,
        rows: [
          ["对象 ID", selectedGraphPath.id],
          ["知识源资产", `auris/knowledge/sources/${selectedGraphPath.sourceId}`],
          ["证据包资产", `auris/evidence/packs/${selectedGraphPath.evidenceId}`],
          ["语义切片", selectedGraphPath.chunk],
          ["召回索引", selectedGraphPath.index],
          ["召回分", `${selectedGraphPath.recall}%`]
        ] as Array<[string, string]>,
        lineage: [
          ["来源", selectedGraphPath.sourceMeta],
          ["切片", selectedGraphPath.chunkMeta],
          ["应用", selectedGraphPath.apps.join(" / ")]
        ] as Array<[string, string]>,
        tags: selectedGraphPath.entities,
        actions: (
          <>
            <button type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedGraphPath.route, selectedGraphPath.apps[0]), "知识库 / 消费路径")}><Eye size={14} />打开场景详情</button>
            <button type="button" onClick={() => openKnowledgeTarget({ module: "assets", tab: "lineage", objectKind: "asset", objectId: selectedGraphEvidence.id === "EVP-drive-129" ? "auris/audio/voice_segments" : "auris/label/event_tags", title: selectedGraphPath.evidenceId }, "知识库 / 资产血缘")}><GitBranch size={14} />资产血缘</button>
            <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={generateCandidateLabels}><Sparkles size={14} />生成候选</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "gate") {
      return {
        badge: "质量门禁",
        title: selectedGate.label,
        subtitle: selectedGate.detail,
        rows: [
          ["对象 ID", `gate-${selectedGate.label}`],
          ["状态", selectedGate.state === "ok" ? "通过" : selectedGate.state === "warn" ? "观察" : "高风险"],
          ["指标值", selectedGate.value],
          ["关联缺口", selectedGap.label],
          ["关联 chunk", selectedChunk.chunkId],
          ["处理建议", selectedGate.detail]
        ] as Array<[string, string]>,
        lineage: [
          ["门禁输入", "知识源、chunk、证据包、标签缺口、召回评测"],
          ["失败回写", "可创建人审任务、评测样本或重切样本"],
          ["发布影响", selectedGate.state === "ok" ? "不阻断发布" : "进入发布前检查"]
        ] as Array<[string, string]>,
        tags: [selectedGate.label, selectedGate.state, selectedGap.label],
        actions: (
          <>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={runQualityCheck}><Gauge size={14} />运行检测</button>
            <button type="button" onClick={() => setActiveModule("evaluation")}><BarChart3 size={14} />评测中心</button>
            <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={createKnowledgeReviewTask}><UserCheck size={14} />建处理任务</button>
          </>
        )
      };
    }
    if (selectedKnowledgeObject.kind === "effect") {
      return {
        badge: "效果指标",
        title: selectedEffect.label,
        subtitle: selectedEffect.drop,
        rows: [
          ["对象 ID", `effect-${selectedEffect.label}`],
          ["当前值", selectedEffect.value],
          ["转化率", `${selectedEffect.rate}%`],
          ["报告资产", "EXP-KB-EFFECT"],
          ["关联模块", selectedEffect.label === "人审接受" ? "调听 / 标签治理" : "洞察 / 资产"],
          ["更新时间", "12:41:08"]
        ] as Array<[string, string]>,
        lineage: [
          ["输入", "证据包、知识命中、标签补齐、人审接受"],
          ["输出", "评测集、badcase、规则候选、效果报告"],
          ["当前阶段", selectedEffect.drop]
        ] as Array<[string, string]>,
        tags: [selectedEffect.label, selectedEffect.tone, selectedEffect.drop],
        actions: (
          <>
            <button type="button" {...knowledgeWriteButtonProps()} onClick={exportEffectReport}><Download size={14} />生成报告</button>
            <button type="button" onClick={() => setActiveModule("insights")}><BarChart3 size={14} />业务洞察</button>
            <button type="button" className="primary" onClick={() => setActiveModule("assets")}><Database size={14} />报告资产</button>
          </>
        )
      };
    }
    return {
      badge: "运行记录",
      title: selectedRun?.[1] ?? "暂无运行记录",
      subtitle: selectedRun?.[2] ?? "当前筛选没有运行记录",
      rows: [
        ["对象 ID", selectedRun?.[0] ?? "run-empty"],
        ["Run Key", `knowledge-run-${selectedRun?.[0] ?? "empty"}`],
        ["任务", selectedRun?.[1] ?? "暂无"],
        ["详情", selectedRun?.[2] ?? "暂无"],
        ["索引版本", currentIndexVersion],
        ["操作范围", KNOWLEDGE_TAB_LABELS[activeTab] ?? "知识库"]
      ] as Array<[string, string]>,
      lineage: [
        ["记录来源", "连接器同步、索引构建、质量检测、标签补全、效果导出"],
        ["可追溯资产", `${selectedSource.name} / ${selectedChunk.chunkId}`],
        ["最近动作", knowledgeDraftActions[0] ?? "暂无动作"]
      ] as Array<[string, string]>,
        tags: ["运行记录", currentIndexVersion, knowledgeNotice.status],
        actions: (
        <>
          <button type="button" {...knowledgeWriteButtonProps()} onClick={exportCurrentKnowledgeData}><Download size={14} />导出记录</button>
          <button type="button" {...knowledgeWriteButtonProps(false, !canBuildSceneIndex ? "缺少 SceneProfile 知识索引强引用" : "")} onClick={buildIndex}><Sparkles size={14} />重跑索引</button>
          <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={runQualityCheck}><Gauge size={14} />跑门禁</button>
        </>
      )
    };
  })();
  const renderDetailPanel = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-right">
      <div className="knowledge-v2-detail-head">
        <span>{detailModel.badge}</span>
        <strong>{detailModel.title}</strong>
        <em>{detailModel.subtitle}</em>
      </div>
      <div className="knowledge-v2-detail-tabs" role="tablist" aria-label="知识对象详情模式">
        {[
          ["fields", "字段"],
          ["lineage", "血缘"],
          ["actions", "动作"]
        ].map(([id, label]) => (
          <button key={id} type="button" className={knowledgeDetailMode === id ? "active" : ""} onClick={() => setKnowledgeDetailMode(id as typeof knowledgeDetailMode)}>
            {label}
          </button>
        ))}
      </div>
      {knowledgeDetailMode === "fields" && renderFieldGrid(detailModel.rows)}
      {knowledgeDetailMode === "lineage" && renderFieldGrid(detailModel.lineage)}
      {knowledgeDetailMode === "actions" && (
        <div className="knowledge-v2-draft-list">
          {knowledgeDraftActions.map((action) => (
            <div key={action}><Check size={14} /><span>{action}</span></div>
          ))}
        </div>
      )}
      <div className="knowledge-v2-tags">
        {detailModel.tags.map((tag, index) => <span key={`${tag}-${index}`}>{tag}</span>)}
      </div>
      <div className="knowledge-v2-actions">{detailModel.actions}</div>
    </section>
  );
  const renderWorkbenchToolbar = () => (
    <section className="module-panel knowledge-v2-toolbar">
      <div>
        <span>{activeTab === "graph" ? "当前知识路径" : "当前知识视图"}</span>
        <strong>{KNOWLEDGE_TAB_LABELS[activeTab] ?? "知识总览"}</strong>
        <em>{selectedKnowledgeObject.kind} · {selectedKnowledgeObject.id}</em>
      </div>
      <label className="knowledge-v2-search">
        <Search size={15} />
        <input
          value={knowledgeFilters.query}
          onChange={(event) => setKnowledgeFilters((current) => ({ ...current, query: event.target.value }))}
          placeholder="搜索知识源、chunk、证据、标签缺口"
        />
      </label>
      <select value={knowledgeFilters.status} onChange={(event) => setKnowledgeFilters((current) => ({ ...current, status: event.target.value }))}>
        {knowledgeStatusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      <button type="button" onClick={() => setKnowledgeFilters({ query: "", status: "all", quality: "all" })}>清空</button>
      <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={exportCurrentKnowledgeData}>
        <Download size={14} />{activeTab === "graph" ? "导出路径证据" : "导出当前视图"}
      </button>
    </section>
  );
  const renderOverviewMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="知识对象生命周期" subtitle="从录音证据到知识切片、标签缺口、候选回写和效果验证" icon={<BrainCircuit size={16} />} />
      <div className="knowledge-v2-lifecycle">
        {lifecycleStages.map(([label, detail, route, tone], index) => (
          <button key={label} type="button" className={`knowledge-v2-stage ${tone}`} onClick={() => {
            if (index === 0) selectKnowledgeObject({ kind: "evidence", id: selectedEvidenceId });
            if (index === 1) selectKnowledgeObject({ kind: "chunk", id: selectedChunkId });
            if (index === 2 || index === 3) selectKnowledgeObject({ kind: "gap", id: selectedGapId });
            if (index === 4) selectKnowledgeObject({ kind: "effect", id: "知识命中" });
            if (route !== "knowledge") {
              openKnowledgeTarget(
                route === "listening"
                  ? { module: "listening", objectKind: "evidence", objectId: selectedEvidenceId, focusMode: "evidence", title: label }
                  : route === "labels"
                    ? { module: "labels", tab: "schema", objectKind: "labelIntent", objectId: selectedGap.id === "gap-testdrive" ? "testDrive" : "quote", title: label }
                    : { module: route, title: label },
                "知识库 / 生命周期"
              );
            }
          }}>
            <span>{index + 1}</span>
            <strong>{label}</strong>
            <em>{detail}</em>
          </button>
        ))}
      </div>
      <div className="knowledge-v2-overview-grid">
        {[
          ["待补缺口", `${labelGaps.length}`, selectedGap.action],
          ["证据包", `${evidenceLinks.length}`, selectedEvidence.title],
          ["切片样本", `${chunkPreviews.length}`, `${chunkPreviews.filter((chunk) => chunk.quality !== "正常").length} 条需处理`],
          ["索引版本", currentIndexVersion, "影子评测后发布"]
        ].map(([label, value, detail]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{detail}</em>
          </div>
        ))}
      </div>
      <div className="knowledge-v2-evidence-compact">
        {filteredEvidenceLinks.map((item) => (
          <button key={item.id} type="button" onClick={() => selectKnowledgeObject({ kind: "evidence", id: item.id })}>
            <strong>{item.title}</strong>
            <span>{item.recording} · {item.window}</span>
            <em>{item.knowledgeHits.join(" / ")}</em>
            <b>{item.confidence}%</b>
          </button>
        ))}
      </div>
    </section>
  );
  const renderConnectorsMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="知识源具体数据" subtitle="查看连接器来源、授权边界、资产 key、上下游关系和同步动作" icon={<Database size={16} />} />
      <div className="knowledge-v2-source-summary">
        <div>
          <span>{selectedSource.kind}</span>
          <strong>{selectedSource.name}</strong>
          <em>{selectedSource.scope}</em>
        </div>
        <b className={selectedSource.status === "异常" ? "danger" : selectedSource.status === "待同步" ? "warn" : "ok"}>{selectedSource.status}</b>
      </div>
      {renderFieldGrid([
        ["资产 Key", `auris/knowledge/sources/${selectedSource.id}`],
        ["Owner", selectedSource.owner],
        ["对象规模", selectedSource.objects],
        ["质量分", `${selectedSource.quality}`],
        ["租户边界", "当前租户 + 当前项目"],
        ["同步游标", "最近 24 小时增量"]
      ])}
      <div className="knowledge-v2-relation-grid">
        {selectedSource.relations.map(([label, value, detail]) => (
          <button key={`${label}-${value}`} type="button" onClick={() => setActiveModule(selectedSource.route)}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{detail}</em>
          </button>
        ))}
      </div>
      <div className="knowledge-action-row">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === `test-${selectedSource.id}`)} onClick={() => testConnection(selectedSource)}>
          <ShieldCheck size={14} />
          {knowledgeAction === `test-${selectedSource.id}` ? "测试中" : "测试连接"}
        </button>
        <button
          type="button"
          {...knowledgeWriteButtonProps(knowledgeAction === `sync-${selectedSource.id}`)}
          data-action-key="knowledge-sync-source"
          onClick={() => syncSource(selectedSource)}
        >
          <RotateCcw size={14} />
          {knowledgeAction === `sync-${selectedSource.id}` ? "同步中" : "同步一次"}
        </button>
        <button type="button" className="primary" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedSource.route, selectedSource.name), "知识库 / 来源关系")}>
          <GitBranch size={14} />
          打开下游详情
        </button>
      </div>
    </section>
  );
  const renderIndexingMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="切片与索引预览" subtitle={`${currentIndexVersion} / 原文、chunk、标签命中和召回质量可对齐查看`} icon={<Layers size={16} />} />
      <div className="knowledge-v2-chunk-compare">
        <div>
          <span>原始内容</span>
          <p>{selectedChunk.before}</p>
        </div>
        <ArrowRight size={18} />
        <div>
          <span>语义 chunk</span>
          <p>{selectedChunk.after}</p>
        </div>
      </div>
      <div className="knowledge-v2-scorebar">
        <i><b style={{ width: `${selectedChunk.recall}%` }} /></i>
        <span>召回分 {selectedChunk.recall} · {selectedChunk.quality}</span>
      </div>
      {renderFieldGrid([
        ["切片 ID", selectedChunk.chunkId],
        ["知识源", selectedChunk.source],
        ["证据窗口", selectedChunk.window],
        ["Token 数", `${selectedChunk.tokens}`],
        ["重叠策略", selectedChunk.overlap],
        ["关联证据", selectedChunk.evidence]
      ])}
      <div className="knowledge-v2-tags">{selectedChunk.labels.map((label) => <span key={label}>{label}</span>)}</div>
      <div className="knowledge-action-row">
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "index", !canBuildSceneIndex ? "缺少 SceneProfile 知识索引强引用" : "")} data-action-key="knowledge-build-index" onClick={buildIndex}>
          <Sparkles size={14} />
          {knowledgeAction === "index" ? "构建中" : "构建索引"}
        </button>
        <button type="button" {...knowledgeWriteButtonProps(knowledgeAction === "rechunk")} onClick={rechunkCurrentSample}>
          <RotateCcw size={14} />
          {knowledgeAction === "rechunk" ? "重切中" : "重切当前样本"}
        </button>
        <button type="button" className="primary" {...knowledgeWriteButtonProps()} onClick={addChunkToCompletion}>
          <Tags size={14} />
          加入标签补全样本
        </button>
      </div>
    </section>
  );
  const renderGraphMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="知识消费路径" subtitle="知识源 → 语义切片 → 实体/标签 → 召回索引 → 消费场景；点击路径会同步详情和证据" icon={<GitBranch size={16} />} />
      <div className={`knowledge-graph-canvas ${selectedGraphPath.tone}`}>
        <div className="knowledge-graph-canvas-head">
          <div>
            <span>当前路径图谱</span>
            <strong>{selectedGraphPath.source} 消费到 {selectedGraphPath.apps[0]}</strong>
          </div>
          <b>{selectedGraphPath.recall}% R@5</b>
        </div>
        <div className="knowledge-graph-canvas-flow" aria-label="当前知识路径五级图谱">
          {[
            ["知识源", selectedGraphPath.source, selectedGraphPath.sourceMeta],
            ["语义切片", selectedGraphPath.chunk, selectedGraphPath.chunkMeta],
            ["实体/标签", selectedGraphPath.entities[0], selectedGraphPath.entities.slice(1).join(" / ")],
            ["召回索引", selectedGraphPath.index, selectedGraphPath.quality],
            ["消费场景", selectedGraphPath.apps[0], selectedGraphPath.apps.slice(1).join(" / ")]
          ].map(([stage, title, detail], index) => (
            <Fragment key={`${stage}-${title}`}>
              <button type="button" onClick={() => {
                if (index === 0) selectKnowledgeObject({ kind: "source", id: selectedGraphPath.sourceId });
                if (index === 1) selectKnowledgeObject({ kind: "chunk", id: selectedChunkId });
                if (index === 2) selectKnowledgeObject({ kind: "gap", id: selectedGraphPath.gapId });
                if (index === 3) selectKnowledgeObject({ kind: "path", id: selectedGraphPath.id });
                if (index === 4) openKnowledgeTarget(targetForKnowledgeRoute(selectedGraphPath.route, selectedGraphPath.apps[0]), "知识库 / 路径节点");
              }}>
                <span>{stage}</span>
                <strong>{title}</strong>
                <em>{detail || "已绑定当前路径"}</em>
              </button>
              {index < 4 && <i aria-hidden="true" />}
            </Fragment>
          ))}
        </div>
        <div className="knowledge-graph-canvas-meta">
          <span><b>证据</b>{selectedGraphPath.evidenceId}</span>
          <span><b>风险</b>{selectedGraphPath.risk}</span>
          <span><b>下一步</b>打开 {selectedGraphPath.apps[0]} 处理</span>
        </div>
      </div>
      <div className="knowledge-graph-metrics">
        {[
          ["来源", `${Array.from(new Set(knowledgeGraphPaths.map((path) => path.source))).length}`, "SOP / FAQ / 音频 / 产品资料"],
          ["切片", `${knowledgeGraphPaths.length}`, "每条都可回到 chunk 与证据窗口"],
          ["实体标签", `${Array.from(new Set(knowledgeGraphPaths.flatMap((path) => path.entities))).length}`, "用于召回、仲裁和补全"],
          ["消费应用", `${Array.from(new Set(knowledgeGraphPaths.flatMap((path) => path.apps))).length}`, "调听 / 标签 / 报告 / 资产"]
        ].map(([label, value, detail]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{detail}</em>
          </div>
        ))}
      </div>
      <div className="knowledge-v2-path-head">
        {["知识源", "语义切片", "实体 / 标签", "召回索引", "消费场景"].map((label) => <span key={label}>{label}</span>)}
      </div>
      <div className="knowledge-v2-path-list">
        {filteredGraphPaths.length === 0 ? (
          <div className="knowledge-v2-empty">
            <Search size={16} />
            <strong>没有匹配的知识路径</strong>
            <span>清空关键词或状态筛选后，可以重新查看 source 到 app 的消费链路。</span>
          </div>
        ) : (
          filteredGraphPaths.map((path) => (
            <button key={path.id} type="button" className={`${selectedGraphPath.id === path.id ? "active" : ""} ${path.tone}`} onClick={() => selectGraphPath(path)}>
              <span><strong>{path.source}</strong><em>{path.sourceMeta}</em></span>
              <i />
              <span><strong>{path.chunk}</strong><em>{path.chunkMeta}</em></span>
              <i />
              <span><strong>{path.entities[0]}</strong><em>{path.entities.slice(1).join(" / ")}</em></span>
              <i />
              <span><strong>{path.index}</strong><em>{path.quality}</em></span>
              <i />
              <span><strong>{path.apps[0]}</strong><em>{path.apps.slice(1).join(" / ")}</em></span>
              <b>{path.recall}%</b>
            </button>
          ))
        )}
      </div>
      <div className="knowledge-path-explainer">
        <div>
          <span>当前路径</span>
          <strong>{selectedGraphPath.source} → {selectedGraphPath.chunk} → {selectedGraphPath.apps.join(" / ")}</strong>
          <em>{selectedGraphPath.risk}</em>
        </div>
        <button type="button" onClick={() => openKnowledgeTarget(targetForKnowledgeRoute(selectedGraphPath.route, selectedGraphPath.apps[0]), "知识库 / 路径解释")}>打开消费场景详情</button>
      </div>
    </section>
  );
  const renderGraphNavigator = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-left knowledge-graph-navigator">
      <PanelHeader title="路径索引" subtitle="按风险和断链优先查看" icon={<GitBranch size={16} />} />
      <div className="knowledge-graph-priority">
        {filteredGraphPaths.map((path) => (
          <button key={path.id} type="button" className={`${path.id === selectedGraphPath.id ? "active" : ""} ${path.tone}`} onClick={() => selectGraphPath(path)}>
            <span>{path.source}</span>
            <strong>{path.apps[0]}</strong>
            <em>{path.risk}</em>
            <b>{path.recall}%</b>
          </button>
        ))}
      </div>
      <div className="knowledge-graph-note">
        <strong>怎么读这张图</strong>
        <span>每行是一条可消费知识链：来源进入切片，再抽出实体/标签，进入混合索引，最后被调听、标签、人审或报告使用。</span>
      </div>
    </section>
  );
  const renderQualityMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="质量门禁明细" subtitle={`${qualityPassCount}/8 通过；失败项可定位到 chunk、证据和标签缺口`} icon={<Gauge size={16} />} />
      <div className="knowledge-v2-gate-grid">
        {filteredQualityGates.map((gate) => (
          <button key={gate.label} type="button" className={`${selectedKnowledgeObject.kind === "gate" && selectedKnowledgeObject.id === gate.label ? "active" : ""} ${gate.state}`} onClick={() => selectKnowledgeObject({ kind: "gate", id: gate.label })}>
            <span>{gate.label}</span>
            <strong>{gate.value}</strong>
            <em>{gate.detail}</em>
          </button>
        ))}
      </div>
      <div className="knowledge-v2-gap-card">
        <div>
          <span>{selectedGap.type}</span>
          <strong>{selectedGap.label}</strong>
          <p>{selectedGap.evidence}</p>
        </div>
        <div>
          <span>知识依据</span>
          <strong>{selectedGap.knowledge}</strong>
          <p>{selectedGap.action}</p>
        </div>
      </div>
    </section>
  );
  const renderEffectsMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="知识补全效果" subtitle="证据包输入、知识命中、标签补齐、人审接受和回归沉淀形成闭环" icon={<BarChart3 size={16} />} />
      <div className="knowledge-v2-effect-flow">
        {filteredEffects.map((stage, index) => (
          <button key={stage.label} type="button" className={`${selectedKnowledgeObject.kind === "effect" && selectedKnowledgeObject.id === stage.label ? "active" : ""} ${stage.tone}`} onClick={() => selectKnowledgeObject({ kind: "effect", id: stage.label })}>
            <span>{index + 1}</span>
            <strong>{stage.label}</strong>
            <b>{stage.value}</b>
            <em>{stage.drop}</em>
            <i><b style={{ width: `${stage.rate}%` }} /></i>
          </button>
        ))}
      </div>
      <div className="knowledge-v2-suggestion-list">
        {completionSuggestions.map((suggestion) => (
          <button key={suggestion.id} type="button" onClick={() => openKnowledgeTarget({ module: suggestion.target, tab: suggestion.target === "labels" ? "schema" : undefined, objectKind: suggestion.target === "labels" ? "labelIntent" : "module", objectId: suggestion.id === "sug-drive" ? "testDrive" : "quote", title: suggestion.title, detail: suggestion.proposal }, "知识库 / 补全建议")}>
            <span>{suggestion.from}</span>
            <strong>{suggestion.title}</strong>
            <em>{suggestion.proposal}</em>
            <b>{suggestion.impact} · {suggestion.status}</b>
          </button>
        ))}
      </div>
    </section>
  );
  const renderRunsMain = () => (
    <section className="module-panel knowledge-v2-column knowledge-v2-main">
      <PanelHeader title="运行记录与草稿动作" subtitle="同步、切片、索引、质量检测、标签补全和报告导出都会落入当前知识版本" icon={<Activity size={16} />} />
      <div className="knowledge-v2-run-summary">
        {[
          ["当前索引", currentIndexVersion, "影子评测后发布"],
          ["补全样本", `${completionCount}`, "候选标签 / 人审"],
          ["切片问题", `${chunkPreviews.filter((chunk) => chunk.quality !== "正常").length}`, "可标记 / 可重切"]
        ].map(([label, value, detail]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{detail}</em>
          </div>
        ))}
      </div>
      <TimelineList items={filteredRuns} />
    </section>
  );
  const renderTabContent = () => {
    if (activeTab === "connectors") return <>{renderObjectList("知识源列表", "连接器、权限、同步状态", sourceItems)}{renderConnectorsMain()}</>;
    if (activeTab === "indexing") return <>{renderObjectList("切片列表", "语义切片、Token、重叠策略和质量", chunkItems)}{renderIndexingMain()}</>;
    if (activeTab === "graph") return <>{renderGraphNavigator()}{renderGraphMain()}</>;
    if (activeTab === "quality") return <>{renderObjectList("门禁队列", "失败原因、关联证据、处理建议", qualityItems)}{renderQualityMain()}</>;
    if (activeTab === "effects") return <>{renderObjectList("效果指标", "补全漏斗与报告回写", effectItems)}{renderEffectsMain()}</>;
    if (activeTab === "runs") return <>{renderObjectList("运行记录", "同步、索引、门禁、报告", runItems)}{renderRunsMain()}</>;
    return <>{renderObjectList("待处理对象", "证据包、标签缺口和补全建议", overviewItems)}{renderOverviewMain()}</>;
  };

  return (
    <div
      className={`module-grid knowledge-grid knowledge-workbench-v2 knowledge-view-${activeTab}`}
      data-testid="knowledge-module-root"
    >
      <div className={`operation-toast knowledge-operation-toast is-${knowledgeNotice.status}`} role="status" aria-live="polite">
        <strong>{knowledgeNotice.title}</strong>
        <span>{knowledgeNotice.detail}</span>
      </div>
      {knowledgeWriteDisabledReason && <div id="knowledge-write-disabled-reason" className="disabled-reason knowledge-write-disabled-reason" role="note">{knowledgeWriteDisabledReason}</div>}
      {renderWorkbenchToolbar()}
      {renderTabContent()}
      {renderDetailPanel()}
    </div>
  );
}
