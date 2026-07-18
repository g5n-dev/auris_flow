import staticCatalog from "../../modules/staticCatalog";
import type { ModuleKey } from "../../shared/contracts/navigation";

const homeProjectsSource = [
  { name: "销售话术质检", scene: "汽车门店", status: "运行中", audio: "2,846", review: 42, score: 94 },
  { name: "试驾流程分析", scene: "试驾单据", status: "评测中", audio: "1,204", review: 18, score: 88 },
  { name: "交付回访质检", scene: "售后交付", status: "异常", audio: "826", review: 31, score: 76 },
  { name: "金融保险陪练", scene: "合规话术", status: "运行中", audio: "612", review: 9, score: 91 }
];

const homeAlertsSource = [
  { title: "报价金额与单据冲突", meta: "北京 SKP 店 / 15 个样本", tone: "red" },
  { title: "边界模型 F1 下降", meta: "prod-v5 vs prod-v4 / -2.1", tone: "amber" },
  { title: "试驾单缺音频证据", meta: "14:00 后 / PBX 空窗", tone: "violet" },
  { title: "原始音频资产待回填", meta: "daily-20250526 / 2 个分区", tone: "teal" }
];

const homePipelineStagesSource = [
  { label: "接入", value: "8,426", meta: "PBX / 工牌 / 门店麦克风", progress: 94, tone: "teal", route: "data" as ModuleKey },
  { label: "处理", value: "7,912", meta: "VAD · ASR · Diar · 标签", progress: 88, tone: "blue", route: "canvas" as ModuleKey },
  { label: "人工网关", value: "317", meta: "串音 / 低置信 / 单据冲突", progress: 42, tone: "amber", route: "listening" as ModuleKey },
  { label: "资产生成", value: "39/46", meta: "音频、事件、标签、评测资产", progress: 84, tone: "green", route: "assets" as ModuleKey },
  { label: "洞察回流", value: "12", meta: "门店异常、销售培训、模型回流", progress: 72, tone: "violet", route: "insights" as ModuleKey }
];

const homeRunOverviewSource = [
  { label: "运行中", value: "9", detail: "生产 TaskRun", tone: "blue", route: "canvas" as ModuleKey },
  { label: "排队", value: "18", detail: "资源等待 / 人审网关", tone: "amber", route: "canvas" as ModuleKey },
  { label: "失败分区", value: "3", detail: "ASR、标签、回填各 1", tone: "red", route: "assets" as ModuleKey },
  { label: "SLA", value: "96.8%", detail: "15 分钟内完成", tone: "green", route: "insights" as ModuleKey }
];

const homeRunQueuesSource = [
  { label: "接入同步", value: "8,426", detail: "PBX / 工牌 / 门店麦克风", percent: 94, tone: "blue", route: "data" as ModuleKey, trend: [6200, 6740, 7010, 7260, 7800, 8210, 8426] },
  { label: "AI 处理中", value: "7,912", detail: "VAD → ASR → Diar → 标签", percent: 88, tone: "teal", route: "canvas" as ModuleKey, trend: [5400, 6020, 6440, 6910, 7180, 7680, 7912] },
  { label: "人工网关", value: "317", detail: "串音 / 低置信 / 单据冲突", percent: 42, tone: "amber", route: "listening" as ModuleKey, trend: [188, 214, 263, 341, 332, 318, 317] },
  { label: "资产物化", value: "39/46", detail: "音频、事件、标签、评测", percent: 84, tone: "green", route: "assets" as ModuleKey, trend: [18, 22, 27, 31, 34, 37, 39] }
];

const homeRunTimelineSource = [
  { time: "12:41:26", title: "评测指标资产", state: "排队中", runId: "run-eval-20250526-7724", route: "evaluation" as ModuleKey, tone: "amber" },
  { time: "12:38:10", title: "业务单据事件资产", state: "已生成", runId: "run-doc-20250526-4410", route: "assets" as ModuleKey, tone: "green" },
  { time: "12:33:42", title: "事件标签资产", state: "等待人工确认", runId: "run-tag-20250526-1288", route: "labels" as ModuleKey, tone: "amber" },
  { time: "12:31:08", title: "ASR 转写资产", state: "失败 3 分区", runId: "run-asr-20250526-0912", route: "canvas" as ModuleKey, tone: "red" }
];

export type HomeRunTrendKey = "health" | "sla" | "queue" | "failed";

export type HomeRunTrendSeries = {
  key: HomeRunTrendKey;
  label: string;
  unit: string;
  values: number[];
  color: string;
  route: ModuleKey;
  detail: string;
  driver: string;
  nextAction: string;
};

const homeRunTrendLabelsSource = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "当前"];

const homeRunTrendSeriesSource: HomeRunTrendSeries[] = [
  {
    key: "health",
    label: "健康度",
    unit: "分",
    values: [89, 91, 90, 86, 88, 90, 91],
    color: "#165dff",
    route: "canvas",
    detail: "综合 SLA、失败分区、队列压力和人工网关。",
    driver: "20:00 后回填任务恢复，健康度回到 91。",
    nextAction: "下钻运行记录，确认失败分区是否已重跑。"
  },
  {
    key: "sla",
    label: "SLA",
    unit: "%",
    values: [97.4, 97.1, 96.2, 94.8, 95.9, 96.7, 96.8],
    color: "#00b42a",
    route: "insights",
    detail: "15 分钟内完成率，低于 95 会触发运行门禁。",
    driver: "12:00 ASR 分区失败拉低 SLA，重跑后恢复。",
    nextAction: "查看 SLA 受影响的门店、设备和分区。"
  },
  {
    key: "queue",
    label: "排队压力",
    unit: "个",
    values: [8, 12, 19, 31, 25, 20, 18],
    color: "#ff7d00",
    route: "canvas",
    detail: "资源等待、人审网关和影子评测共同形成的队列。",
    driver: "中午批量入库后排队压力最高，当前已降到 18。",
    nextAction: "调整 Agent 批处理窗口或释放人工网关。"
  },
  {
    key: "failed",
    label: "失败分区",
    unit: "个",
    values: [1, 1, 2, 5, 4, 3, 3],
    color: "#f53f3f",
    route: "assets",
    detail: "ASR、标签和回填任务失败分区数量。",
    driver: "12:33 ASR 转写资产失败 3 分区，是当前主要风险。",
    nextAction: "处理失败分区，并把 badcase 回流到评测。"
  }
];

const homeRunFailureHeatmapSource = [
  { label: "ASR", route: "canvas" as ModuleKey, values: [0, 1, 1, 5, 3, 2, 3] },
  { label: "标签", route: "labels" as ModuleKey, values: [0, 0, 1, 2, 2, 1, 1] },
  { label: "回填", route: "assets" as ModuleKey, values: [0, 0, 0, 1, 2, 2, 1] }
];
const homeAgentTasks = [
  {
    level: "P0",
    title: "报价金额与单据冲突",
    context: "北京 SKP 店 · 销售A · 12:27:18",
    reason: "ASR 报价 28.19 万，报价单字段为 31.69 万，建议进入证据审查核对落地价构成。",
    confidence: "82%",
    route: "listening" as ModuleKey,
    chips: ["报价单 #BJ-041", "金额冲突", "人工复核"]
  },
  {
    level: "P1",
    title: "同门店多设备串音待排除",
    context: "极光中心店 · B-2001 与 Hall-Mic",
    reason: "同一时间窗能量峰重叠，ASR 片段重复，建议切到串音矩阵确认主录音归属。",
    confidence: "78%",
    route: "listening" as ModuleKey,
    chips: ["串音候选", "主录音 A-1001", "设备同峰"]
  },
  {
    level: "P2",
    title: "事件标签资产需要回填",
    context: "daily-20250526 · 标签版本 v1.8.4",
    reason: "标签规则修正后影响报价承诺、试驾预约、售后跟进三个下游资产。",
    confidence: "91%",
    route: "assets" as ModuleKey,
    chips: ["影响 12 下游", "可重跑", "需审批"]
  }
];

const homeEvidenceChainsSource = [
  {
    time: "12:27:18",
    title: "报价事件 · 金额冲突",
    detail: "销售A 给出优惠 3.5 万，落地约 28.19 万；报价单字段仍为 31.69 万。",
    route: "listening" as ModuleKey,
    links: ["主录音 A-1001", "ASR 片段 S2", "报价单 #BJ-041", "Agent 建议 82%"]
  },
  {
    time: "14:00+",
    title: "试驾预约事件 · 缺音频",
    detail: "试驾单已创建，PBX 电话录音空窗，建议补查车载设备和电话录音。",
    route: "data" as ModuleKey,
    links: ["试驾单 #SJ-028", "PBX 空窗", "Drive-01 低置信", "待回填"]
  },
  {
    time: "15:36:42",
    title: "售后跟进事件 · 标签可回流",
    detail: "售后承诺标签命中，模型与人工一致，可加入训练样本与洞察报告。",
    route: "assets" as ModuleKey,
    links: ["售后工单 #SH-112", "质检标签", "已确认", "训练候选"]
  }
];

const homeEntityRelations = [
  {
    kind: "门店实体",
    entity: "北京区域 / 极光中心店",
    entityId: "entity://store/BJ-AURORA-001",
    context: "同一门店同时参与销售话术质检、试驾流程分析和门店接待洞察，首页不能只落到当前项目。",
    sourceProjects: ["销售话术质检", "试驾流程分析", "门店接待洞察"],
    chain: ["租户", "门店", "设备", "接待会话"],
    links: [
      { label: "项目详情", scope: "跨项目门店画像", route: "projects" as ModuleKey },
      { label: "数据聚合树", scope: "空间优先 / 极光中心店", route: "data" as ModuleKey },
      { label: "业务洞察", scope: "门店异常趋势", route: "insights" as ModuleKey }
    ]
  },
  {
    kind: "人员实体",
    entity: "销售A / A-1001 工牌",
    entityId: "entity://person/EMP-A1001",
    context: "同一个员工实体会出现在销售质检、金融保险陪练和售后跟进项目，跳转要保留员工主体关系。",
    sourceProjects: ["销售话术质检", "金融保险陪练", "售后维修工单关联"],
    chain: ["员工", "工牌", "声纹", "会话片段"],
    links: [
      { label: "证据审查", scope: "S20250526-000128 / 销售A", route: "listening" as ModuleKey },
      { label: "标签治理", scope: "报价承诺 / 价格异议", route: "labels" as ModuleKey },
      { label: "评测回流", scope: "员工话术 badcase", route: "evaluation" as ModuleKey }
    ]
  },
  {
    kind: "业务事件",
    entity: "报价单 #BJ-041",
    entityId: "entity://doc/quote/BJ-041",
    context: "报价单来自业务系统，但证据来自音频、ASR、标签和 Agent 建议，需要可回跳到每个来源。",
    sourceProjects: ["销售话术质检", "门店接待洞察"],
    chain: ["报价单", "报价事件", "金额实体", "风险复核"],
    links: [
      { label: "证据审查", scope: "12:27:18 / 金额冲突", route: "listening" as ModuleKey },
      { label: "资产血缘", scope: "quote_event -> evidence_pack", route: "assets" as ModuleKey },
      { label: "处理画布", scope: "认证事件接口 / 风险策略", route: "canvas" as ModuleKey }
    ]
  },
  {
    kind: "音频资产",
    entity: "A-1001_20250526_122300.wav",
    entityId: "asset://audio/A-1001/20250526/122300",
    context: "一个完整对话可能跨多个 wav 切片，被不同项目复用为主录音、串音候选或训练样本。",
    sourceProjects: ["销售话术质检", "试驾流程分析", "模型评测集"],
    chain: ["音频 URL", "切片", "ASR", "标签轨道"],
    links: [
      { label: "数据资产", scope: "原始音频 / 切片索引", route: "data" as ModuleKey },
      { label: "证据审查", scope: "Minimap / 当前片段", route: "listening" as ModuleKey },
      { label: "资产血缘", scope: "audio_url -> transcript_asset", route: "assets" as ModuleKey }
    ]
  }
];


const homeData = staticCatalog as {
  homeCatalog: {
    agentTasks: typeof homeAgentTasks;
    entityRelations: typeof homeEntityRelations;
  };
  runtimeCatalog: {
    homeProjects: typeof homeProjectsSource;
    homeAlerts: typeof homeAlertsSource;
    homePipelineStages: typeof homePipelineStagesSource;
    homeRunOverview: typeof homeRunOverviewSource;
    homeRunQueues: typeof homeRunQueuesSource;
    homeRunTimeline: typeof homeRunTimelineSource;
    homeRunTrendLabels: typeof homeRunTrendLabelsSource;
    homeRunTrendSeries: typeof homeRunTrendSeriesSource;
    homeRunFailureHeatmap: typeof homeRunFailureHeatmapSource;
    homeEvidenceChains: typeof homeEvidenceChainsSource;
  };
};

export const homeCatalogData = homeData.homeCatalog;
export const homeProjects = homeData.runtimeCatalog.homeProjects;
export const homeAlerts = homeData.runtimeCatalog.homeAlerts;
export const homePipelineStages = homeData.runtimeCatalog.homePipelineStages;
export const homeRunOverview = homeData.runtimeCatalog.homeRunOverview;
export const homeRunQueues = homeData.runtimeCatalog.homeRunQueues;
export const homeRunTimeline = homeData.runtimeCatalog.homeRunTimeline;
export const homeRunTrendLabels = homeData.runtimeCatalog.homeRunTrendLabels;
export const homeRunTrendSeries = homeData.runtimeCatalog.homeRunTrendSeries;
export const homeRunFailureHeatmap = homeData.runtimeCatalog.homeRunFailureHeatmap;
export const homeEvidenceChains = homeData.runtimeCatalog.homeEvidenceChains;
