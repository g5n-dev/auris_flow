import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetrics } from "./buildInsightMetrics";
import type { InsightView } from "./buildInsightView";
import type { InsightSelectionState } from "./useInsightSelectionState";
import type { InsightEvidenceActions } from "./buildInsightEvidenceActions";
import type { InsightChartSpecs } from "./useInsightChartSpecs";
import type { InsightChartSelection } from "./buildInsightChartSelection";
import type { InsightContext } from "./useInsightContext";
import type { InsightReportDraftBuilder } from "./buildInsightReportDraft";
import type { InsightReportState } from "./useInsightReportState";
import type { InsightReportGuards } from "./buildInsightReportGuards";
import type { InsightReportExecution } from "./buildInsightReportExecution";
import type { ModuleKey } from "../../../shared/contracts/navigation";
import type { InsightChartSpec, InsightReportDraft } from "../types";
import { buildMetricScopePresentation } from "../model/metricScopePresentation";

export function buildInsightReportActions(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState & InsightReportGuards & InsightReportExecution) {
  const { activeChartId, activeReport, activeReportId, activeTrendPointIndex, activeTrendSeriesKey, buildReportDraft, createReport, currentTab, dataset, evidenceForMetric, insightContextKey, rangeConfig, selectedFact, selectedMetric, setActiveChartId, setActiveReportId, setAgentOutput, setDownstreamAssetFocus, setHiddenChartIds, setInsightActionNotice, setReportDrafts, setSelectedChartAction, setSelectedFactId, unique, views, visibleChartSpecs } = scope;
  const deleteReport = (reportId: string) => {
      setReportDrafts((current) => {
        const next = current.filter((report) => report.id !== reportId);
        if (activeReportId === reportId) {
          setActiveReportId(next.find((report) => report.contextKey === insightContextKey)?.id ?? "");
        }
        return next;
      });
    };

  const reportToMarkdown = (report: InsightReportDraft) => {
      if (!report.authoritativeReportDocument) {
        throw new Error("报告尚未返回服务端冻结正文，禁止导出本地 fixture 草稿。");
      }
      const metricScopeLines = (report.metricSnapshots ?? []).map((snapshot) => {
        const presentation = buildMetricScopePresentation(snapshot);
        return [
          `- ${snapshot.metric_key}（${snapshot.metric_result_id}）`,
          `  - taxonomy：${presentation.taxonomyMode ?? "未返回"}`,
          `  - 源/目标版本：${presentation.sourceLabelVersionIds.join(" / ") || "未返回"} → ${presentation.targetLabelVersionId ?? "未返回"}`,
          `  - Mapping：${presentation.mappingBundleId ?? "未返回"}`,
          `  - FactSet：Generation ${presentation.factSetGeneration ?? "未返回"} / fact_as_of ${presentation.factAsOf ?? "未返回"}`,
          `  - 可比性：${presentation.comparabilityStatus ?? "未返回"} / ${presentation.comparabilityReasonCodes.join("、") || presentation.hiddenDeltaReason || "服务端未返回额外原因"}`
        ].join("\n");
      });
      return [
        `# ${report.title}`,
        "",
        ...report.sections.flatMap((section) => [`## ${section.title}`, section.body, ""]),
        "## 冻结统计口径",
        ...(metricScopeLines.length
          ? metricScopeLines
          : ["当前报告未返回不可变 metric snapshot/scope；未用筛选条件猜测口径。"]),
        ""
      ].join("\n");
    };

  const exportReport = (report: InsightReportDraft, format: "markdown" | "json") => {
      if (!report.authoritativeReportDocument) {
        setInsightActionNotice({
          status: "error",
          title: "报告导出已阻断",
          detail: "报告尚未返回服务端冻结正文或绑定校验未通过。"
        });
        return;
      }
      const content = format === "json"
        ? JSON.stringify(report.authoritativeReportDocument, null, 2)
        : reportToMarkdown(report);
      const blob = new Blob([content], { type: format === "json" ? "application/json" : "text/markdown" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${report.id}.${format === "json" ? "json" : "md"}`;
      anchor.click();
      URL.revokeObjectURL(url);
    };

  const factIdForChart = (chart: InsightChartSpec) => {
      if (chart.id === activeChartId && selectedFact?.id) return selectedFact.id;
      if (chart.bars?.[0]?.factId) return chart.bars[0].factId;
      if (chart.rows?.[0]?.factId) return chart.rows[0].factId;
      if (chart.axes?.[0]?.factId) return chart.axes[0].factId;
      if (chart.nodes?.[0]?.factId) return chart.nodes[0].factId;
      if (chart.heatmap?.rows?.[0]?.factId) return chart.heatmap.rows[0].factId;
      if (chart.summaryCards?.[0]?.factId) return chart.summaryCards[0].factId;
      const firstSeries = chart.series?.find((series) => series.key === activeTrendSeriesKey) ?? chart.series?.[0];
      if (firstSeries?.factIds?.length) return firstSeries.factIds[Math.min(activeTrendPointIndex, firstSeries.factIds.length - 1)] ?? firstSeries.factIds[0];
      return evidenceForMetric(selectedMetric)[0]?.id ?? dataset.facts[0]?.id;
    };

  const chartRouteLabel = (route: ModuleKey) => route === "labels" ? "标签治理" : route === "data" ? "数据管理" : route === "evaluation" ? "评测样本" : route === "listening" ? "调听证据" : route === "assets" ? "资产血缘" : "洞察";

  const routeForChart = (chart: InsightChartSpec) => chart.type === "radar" || chart.id.includes("quality") ? "evaluation" : chart.id.includes("tag") ? "labels" : chart.id.includes("store") ? "data" : chart.id.includes("sales") ? "labels" : "assets";

  const downstreamLabelForChart = (chart: InsightChartSpec) => {
      if (chart.id.includes("sales")) return "查看训练样本资产";
      if (chart.id.includes("store")) return "查看门店数据明细";
      if (chart.id.includes("tag")) return "查看标签资产血缘";
      if (chart.id.includes("quality")) return "查看评测样本";
      return "查看下游资产";
    };

  const focusChartEvidence = (chart: InsightChartSpec, reason: string) => {
      const factId = factIdForChart(chart);
      const fact = dataset.facts.find((item) => item.id === factId) ?? selectedFact ?? dataset.facts[0];
      setActiveChartId(chart.id);
      if (fact?.id) setSelectedFactId(fact.id);
      setAgentOutput(`${reason}。已聚焦「${chart.title}」并同步 ${fact?.eventType ?? "当前"} 证据、assetKey 和分区。`);
      return fact;
    };

  const handleInsightAgentAction = (chart?: InsightChartSpec) => {
      const targetChart = chart ?? visibleChartSpecs.find((spec) => spec.id === activeChartId) ?? visibleChartSpecs[0];
      if (!targetChart) {
        setInsightActionNotice({ status: "error", title: "无可解读图表", detail: "当前看板没有可见图表，无法生成智能解读。" });
        return;
      }
      const fact = focusChartEvidence(targetChart, `${views[currentTab].dimension} 已运行智能解读`);
      const tabMessage = currentTab === "sales"
        ? `销售样本贡献已解释：优先看 ${targetChart.title} 的头部对象、报价/承接/风险结构和训练样本资产。`
        : currentTab === "store"
          ? `门店风险热区已解释：优先看 ${targetChart.title} 的门店、时间窗、风险标签和待回填资产。`
          : currentTab === "tags"
            ? `标签血缘已解释：优先看 ${targetChart.title} 的标签命中、证据资产和 Human Loop 去向。`
            : currentTab === "quality"
              ? `模型质量变化已解释：优先看 ${targetChart.title} 对边界、串音、标签和报告可信度的影响。`
              : `BI 卡片已解释：${targetChart.title} 与当前指标、证据和报告草稿已对齐。`;
      setSelectedChartAction({ chartId: targetChart.id, action: "agent", title: targetChart.title });
      setInsightActionNotice({
        status: "success",
        title: "智能解读已更新",
        detail: `${tabMessage} 当前证据：${fact?.eventType ?? selectedMetric.label}。`
      });
      setAgentOutput(`${tabMessage} ${fact ? `已定位 ${fact.time} · ${fact.store} · ${fact.assetKey}。` : ""}`);
    };

  const handleCreateReportFromDashboard = async () => {
      const draft = await createReport();
      if (draft) setSelectedChartAction({ chartId: draft.chartIds[0] ?? "report", action: "report", title: draft.title });
    };

  const handleAddChartToReport = (chart: InsightChartSpec) => {
      const fact = focusChartEvidence(chart, "已把图表加入报告上下文");
      const baseReport = activeReport ?? buildReportDraft(currentTab, views[currentTab].reportTitle, "草稿");
      const nextReport: InsightReportDraft = {
        ...baseReport,
        status: baseReport.status === "待生成" ? "草稿" : baseReport.status,
        chartIds: unique([...baseReport.chartIds, chart.id]),
        evidenceIds: unique([...baseReport.evidenceIds, fact?.id].filter(Boolean) as string[]),
        sections: [
          ...baseReport.sections,
          { title: `图表引用：${chart.title}`, body: `${chart.subtitle}；来源 ${chart.source}；当前证据 ${fact?.assetKey ?? "待选择"}。` }
        ]
      };
      setReportDrafts((current) => {
        if (current.some((report) => report.id === nextReport.id)) {
          return current.map((report) => report.id === nextReport.id ? nextReport : report);
        }
        return [nextReport, ...current];
      });
      setActiveReportId(nextReport.id);
      setSelectedChartAction({ chartId: chart.id, action: "report", title: chart.title });
      setInsightActionNotice({
        status: "success",
        title: "图表已加入报告",
        detail: `${chart.title} 已写入 ${nextReport.title}，当前报告包含 ${nextReport.chartIds.length} 张图表和 ${nextReport.evidenceIds.length} 条证据。`
      });
      setAgentOutput(`${chart.title} 已加入 ${nextReport.title}，报告引用会保留 ${fact?.assetKey ?? "当前证据链"}。`);
    };

  const handleHideChart = (chart: InsightChartSpec) => {
      setHiddenChartIds((current) => unique([...current, chart.id]));
      setSelectedChartAction({ chartId: chart.id, action: "hide", title: chart.title });
      setInsightActionNotice({
        status: "success",
        title: "图表已隐藏",
        detail: `${chart.title} 已从当前看板隐藏；生成报告时不会引用该图表，可点击“显示全部”恢复。`
      });
      setAgentOutput(`${chart.title} 已隐藏，当前看板剩余 ${Math.max(visibleChartSpecs.length - 1, 0)} 张图表参与分析和报告生成。`);
    };

  const handleOpenChartSource = (chart: InsightChartSpec) => {
      const fact = focusChartEvidence(chart, "已查看图表数据源");
      setSelectedChartAction({ chartId: chart.id, action: "source", title: chart.title });
      setDownstreamAssetFocus({
        chartId: chart.id,
        title: `${chart.title} 数据源`,
        route: routeForChart(chart),
        factId: fact?.id,
        assetKey: fact?.assetKey ?? chart.source,
        items: [
          { label: "图表数据源", value: chart.source, detail: chart.subtitle, route: routeForChart(chart) },
          { label: "事实表", value: fact?.id ?? "InsightFact", detail: fact ? `${fact.store} / ${fact.person} / ${fact.partitionKey}` : "当前范围聚合", route: "data" },
          { label: "证据资产", value: fact?.assetKey ?? "assetKey 待选择", detail: fact?.evidenceRefs.join(" / ") ?? "点击图表元素补齐证据", route: "assets" }
        ]
      });
      setInsightActionNotice({
        status: "success",
        title: "数据源已聚焦",
        detail: `${chart.title} 来源 ${chart.source}，右侧已展示字段、asset key 和下游入口。`
      });
    };

  const handleOpenDownstreamAssets = (chart: InsightChartSpec) => {
      const fact = focusChartEvidence(chart, "已打开下游资产清单");
      const route = routeForChart(chart);
      const reportName = activeReport?.title ?? views[currentTab].reportTitle;
      setSelectedChartAction({ chartId: chart.id, action: "downstream", title: chart.title });
      setDownstreamAssetFocus({
        chartId: chart.id,
        title: downstreamLabelForChart(chart),
        route,
        factId: fact?.id,
        assetKey: fact?.assetKey ?? chart.source,
        items: [
          { label: "音频片段", value: fact?.audio ?? "当前音频窗口", detail: fact?.time ?? rangeConfig.reportScope, route: "listening" },
          { label: "标签资产", value: fact?.tags.slice(0, 3).join(" / ") || selectedMetric.tags.slice(0, 3).join(" / "), detail: dataset.context.label, route: "labels" },
          { label: "事件数据", value: fact?.eventType ?? chart.title, detail: fact?.doc ?? chart.source, route: "data" },
          { label: "报告草稿", value: reportName, detail: `${activeReport?.chartIds.length ?? 0} 图表 · ${activeReport?.evidenceIds.length ?? 0} 证据`, route: "assets" }
        ]
      });
      setInsightActionNotice({
        status: "success",
        title: "下游资产已定位",
        detail: `${chart.title} 已关联 ${chartRouteLabel(route)}、${fact?.assetKey ?? chart.source} 和当前报告草稿。`
      });
    };

  return {
    deleteReport,
    reportToMarkdown,
    exportReport,
    factIdForChart,
    chartRouteLabel,
    routeForChart,
    downstreamLabelForChart,
    focusChartEvidence,
    handleInsightAgentAction,
    handleCreateReportFromDashboard,
    handleAddChartToReport,
    handleHideChart,
    handleOpenChartSource,
    handleOpenDownstreamAssets
  };
}

export type InsightReportActions = ReturnType<typeof buildInsightReportActions>;
