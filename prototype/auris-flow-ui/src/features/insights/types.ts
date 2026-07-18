import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";
import type { TopbarContextState } from "../../shared/contracts/workspace";

export type InsightsModuleProps = {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  topbarContext: TopbarContextState;
  metricProjectionItems?: unknown[];
};

export type InsightTone = "green" | "amber" | "red" | "blue" | "violet" | "teal";

export type InsightTabKey = "business" | "store" | "sales" | "tags" | "quality" | "reports";

export type InsightTimeRange = "today" | "7d" | "30d" | "90d" | "custom";

export type InsightChartType = "kpi" | "line" | "sankey" | "radar" | "heatmap" | "bar" | "table";

export type InsightFact = {
    id: string;
    tenant: string;
    project: string;
    store: string;
    date: string;
    time: string;
    person: string;
    customer: string;
    eventType: string;
    tags: string[];
    audio: string;
    durationSec: number;
    confidence: number;
    status: string;
    doc: string;
    source: string;
    assetKey: string;
    partitionKey: string;
    modelVersion: string;
    labelVersion: string;
    route: ModuleKey;
    amountConflict: boolean;
    crosstalk: boolean;
    lowConfidence: boolean;
    evidenceRefs: string[];
  };

export type InsightMetric = {
    key: string;
    label: string;
    value: string;
    valueNumber: number;
    delta: string;
    tone: InsightTone;
    meaning: string;
    formula: string;
    tags: string[];
    source: string;
    owner: string;
    rangeValues: Record<InsightTimeRange, number>;
    evidenceCount: number;
    suggestion: string;
    drilldownRoute: ModuleKey;
    insight: string;
    action: string;
    route: ModuleKey;
    evidenceIds: string[];
  };

export type InsightMetricSnapshot = {
    metric_result_id: string;
    metric_key: string;
    [key: string]: unknown;
  };

export type InsightNorthStar = {
    label: string;
    value: number;
    delta: string;
    meaning: string;
    formula: string;
    lift: string;
    drag: string;
    components: Array<{
      key: string;
      metricKey: string;
      label: string;
      weight: number;
      value: number;
      contribution: number;
      meaning: string;
      route: ModuleKey;
      tone: InsightTone;
    }>;
  };

export type InsightDataset = {
    context: Record<"tenant" | "project" | "store" | "date" | "model" | "label", string>;
    facts: InsightFact[];
    tagCounts: Array<{ label: string; count: number; confidence: number; assetKey: string }>;
    storeRows: Array<{ store: string; total: number; risk: number; quote: number; testDrive: number; confidence: number }>;
    salesRows: Array<{ person: string; total: number; quote: number; resolved: number; risk: number; confidence: number }>;
    qualityRows: Array<{ ability: string; baseline: number; candidate: number; delta: number; blocker: string; samples: number; route: ModuleKey }>;
    trendDates: string[];
  };

export type InsightChartSpec = {
    id: string;
    type: InsightChartType;
    title: string;
    subtitle: string;
    source: string;
    tone?: InsightTone;
    metricKeys?: string[];
    xLabels?: string[];
    yDomain?: [number, number];
    yTicks?: number[];
    targetLine?: { label: string; value: number; tone?: InsightTone };
    eventMarkers?: Array<{ index: number; label: string; detail: string; factId?: string; seriesKey?: string; tone?: InsightTone }>;
    summaryCards?: Array<{ title: string; value: string; detail: string; tone: InsightTone; factId?: string; route?: ModuleKey; seriesKey?: string; metricKey?: string; pointIndex?: number }>;
    series?: Array<{
      key: string;
      label: string;
      tone: InsightTone;
      values: number[];
      unit?: string;
      unitLabel?: string;
      factIds?: string[];
      metricKey?: string;
      emphasis?: boolean;
      delta?: string;
      direction?: "up" | "down" | "flat";
    }>;
    nodes?: Array<{ id: string; label: string; value: string; column: number; factId?: string; route?: ModuleKey }>;
    links?: Array<{ source: string; target: string; value: number; tone: InsightTone }>;
    axes?: Array<{ label: string; value: number; compare: number; factId?: string; route?: ModuleKey }>;
    heatmap?: { columns: string[]; rows: Array<{ label: string; detail: string; values: number[]; factId?: string; route?: ModuleKey }> };
    bars?: Array<{ label: string; value: number; detail: string; tone: InsightTone; factId?: string; route?: ModuleKey }>;
    columns?: string[];
    rows?: Array<{ id: string; cells: string[]; factId?: string; route?: ModuleKey }>;
    emptyReason?: string;
  };

export type InsightReportDraft = {
    id: string;
    title: string;
    status: "待生成" | "草稿" | "已生成" | "待导出";
    createdAt: string;
    sourceTab: InsightTabKey;
    scope: string;
    metricKeys: string[];
    chartIds: string[];
    evidenceIds: string[];
    sections: Array<{ title: string; body: string }>;
    summary: string;
    contextKey: string;
    backendState?: string;
    backendRunId?: string;
    metricRunId?: string;
    metricResultIds?: string[];
    metricSnapshots?: InsightMetricSnapshot[];
    evidencePackIds?: string[];
  };

export type InsightReportFlowState = {
    status: "idle" | "pending" | "success" | "failed";
    stage: "idle" | "metric-create" | "metric-poll" | "metric-query" | "report-create" | "report-poll" | "ready" | "failed";
    attempt: number;
    metricRunId?: string;
    reportId?: string;
    detail: string;
  };

export type InsightViewConfig = {
    title: string;
    subtitle: string;
    dimension: string;
    metricKeys: string[];
    chartIds: string[];
    reportTitle: string;
    agentGoal: string;
  };
