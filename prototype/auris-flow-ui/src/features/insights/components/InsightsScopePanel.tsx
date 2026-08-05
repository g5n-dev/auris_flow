import type { InsightsController } from "../controller/useInsightsController";
import {
  buildMetricScopePresentation,
  buildMetricScopeSetPresentation,
  metricDeltaPresentation
} from "../model/metricScopePresentation";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { BarChart3 } from "lucide-react";

export function InsightsScopePanel({ controller }: { controller: InsightsController }) {
  const { applyCustomPreset, authoritativeInsightDisplayReady, authoritativeInsightDisplayReason, authoritativeMetricError, authoritativeMetricSnapshots, authoritativeMetricStatus, comparisonScope, customRangeDraft, customRangeError, customReportScope, dataset, evidenceComplete, evidenceForMetric, formatPercent, insightTimeRanges, labelVersionFilter, labelVersionOptions, metricByKey, northStar, rangeConfig, retryAuthoritativeMetrics, riskFacts, salesFilter, salesOptions, selectedMetric, setActiveModule, setAgentOutput, setComparisonScope, setLabelVersionFilter, setSalesFilter, setSelectedFactId, setSelectedMetricKey, setStoreFilter, setTimeRange, storeFilter, storeOptions, timeRange, updateCustomRangeDraft, view, visibleMetrics } = controller;
  if (!authoritativeInsightDisplayReady) {
    return (
      <section className="module-panel insight-scope-panel">
        <PanelHeader title={view.title} subtitle={view.subtitle} icon={<BarChart3 size={16} />} sticky />
        <div className="insight-empty-state" role="status" data-testid="authoritative-insight-empty">
          <strong>
            {authoritativeMetricStatus === "loading"
              ? "正在读取权威快照"
              : "尚未生成权威快照"}
          </strong>
          <span>
            {authoritativeInsightDisplayReason ??
              "需要完整 MetricSnapshot、comparable_series 与 evidence_refs 后才能展示数值、涨跌、证据数和 Agent 归因。"}
          </span>
          <div>
            <button type="button" onClick={retryAuthoritativeMetrics}>重新读取</button>
            <button type="button" onClick={() => setActiveModule("canvas")}>前往任务配置</button>
          </div>
        </div>
      </section>
    );
  }
  const metricSnapshotByKey = new Map(
    authoritativeMetricSnapshots.map((snapshot) => [snapshot.metric_key, snapshot])
  );
  const selectedScope = buildMetricScopePresentation(
    metricSnapshotByKey.get(selectedMetric.key)
  );
  const reportComparability = buildMetricScopeSetPresentation(
    authoritativeMetricSnapshots
  );
  const selectedScopeReason = selectedScope.comparabilityReasonCodes.length
    ? selectedScope.comparabilityReasonCodes.join("、")
    : selectedScope.hiddenDeltaReason ?? "服务端未返回额外原因";
  const scopeCards = [
    [
      "Current 快照",
      authoritativeMetricStatus === "ready" ? "BFF 已绑定" : authoritativeMetricStatus === "loading" ? "读取中" : "已阻断",
      authoritativeMetricError ?? `${authoritativeMetricSnapshots.length} 个唯一物化快照`
    ],
    [
      "统计口径",
      selectedScope.taxonomyMode ?? "未返回",
      `源 ${selectedScope.sourceLabelVersionIds.join(" / ") || "未返回"} → 目标 ${selectedScope.targetLabelVersionId ?? "未返回"}`
    ],
    ["Mapping", selectedScope.mappingBundleId ?? "未返回", `指标 ${selectedMetric.label}`],
    [
      "FactSet",
      selectedScope.factSetGeneration ? `Generation ${selectedScope.factSetGeneration}` : "未返回",
      `fact_as_of ${selectedScope.factAsOf ?? "未返回"}`
    ],
    [
      "可比性",
      `${selectedScope.comparabilityLabel}${selectedScope.comparabilityStatus ? ` · ${selectedScope.comparabilityStatus}` : ""}`,
      selectedScopeReason
    ]
  ];
  return (
    <section className="module-panel insight-scope-panel">
            <PanelHeader title={view.title} subtitle={view.subtitle} icon={<BarChart3 size={16} />} sticky />
            <div className="insight-bi-controlbar">
              <div className="insight-time-switch" aria-label="时间范围">
                {insightTimeRanges.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className={timeRange === item.key ? "active" : ""}
                    onClick={() => {
                      setTimeRange(item.key);
                      setAgentOutput(`已切换到 ${item.reportScope}，北极星、趋势、排行和报告摘要将按该范围重新计算。`);
                    }}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.detail}</span>
                  </button>
                  ))}
                </div>
                {timeRange === "custom" && (
                  <div className={customRangeError ? "insight-custom-range has-error" : "insight-custom-range"} aria-label="自定义时间范围">
                    <label>
                      <span>开始日期</span>
                      <input
                        type="date"
                        value={customRangeDraft.startDate}
                        max={customRangeDraft.endDate || dataset.context.date}
                        onChange={(event) => updateCustomRangeDraft({ ...customRangeDraft, startDate: event.target.value })}
                      />
                    </label>
                    <label>
                      <span>结束日期</span>
                      <input
                        type="date"
                        value={customRangeDraft.endDate}
                        min={customRangeDraft.startDate}
                        max={dataset.context.date}
                        onChange={(event) => updateCustomRangeDraft({ ...customRangeDraft, endDate: event.target.value })}
                      />
                    </label>
                    <button type="button" onClick={() => applyCustomPreset(14)}>近14天</button>
                    <button
                      type="button"
                      className="primary"
                      disabled={Boolean(customRangeError)}
                      onClick={() => {
                        setAgentOutput(`已应用 ${customReportScope}，图表、指标和报告草稿都会使用这个时间窗口。`);
                      }}
                    >
                      应用范围
                    </button>
                    <small>{customRangeError || `当前范围：${customReportScope}`}</small>
                  </div>
                )}
                <div className="insight-bi-filters">
                {["同城均值", "上周期", "标签版本"].map((scope) => (
                  <button key={scope} type="button" className={comparisonScope === scope ? "active" : ""} onClick={() => setComparisonScope(scope)}>
                    {scope}
                  </button>
                ))}
                <button type="button" onClick={() => {
                  const currentIndex = storeOptions.indexOf(storeFilter);
                  setStoreFilter(storeOptions[(currentIndex + 1) % storeOptions.length] ?? dataset.context.store);
                }}>
                  门店 {storeFilter}
                </button>
                <button type="button" onClick={() => {
                  const currentIndex = salesOptions.indexOf(salesFilter);
                  setSalesFilter(salesOptions[(currentIndex + 1) % salesOptions.length] ?? "全部销售");
                }}>
                  销售 {salesFilter}
                </button>
                <button type="button" onClick={() => {
                  const currentIndex = labelVersionOptions.indexOf(labelVersionFilter);
                  setLabelVersionFilter(labelVersionOptions[(currentIndex + 1) % labelVersionOptions.length] ?? dataset.context.label);
                }}>
                  标签 {labelVersionFilter}
                </button>
              </div>
            </div>
            <div className="insight-northstar-hero">
              <div className="insight-northstar-main">
                <span>北极星指标</span>
                <strong>{northStar.value}</strong>
                <b>{northStar.label} · {reportComparability.showDelta ? northStar.delta : "涨跌已隐藏"}</b>
                <p>{northStar.meaning}{reportComparability.reason ? ` ${reportComparability.reason}` : ""}</p>
                <em>{northStar.formula}</em>
              </div>
              <div className="insight-northstar-components">
                {northStar.components.map((component) => (
                  <button
                    key={component.key}
                    type="button"
                    className={`${component.tone} ${selectedMetric.key === component.metricKey ? "active" : ""}`}
                    onClick={() => {
                      const metric = metricByKey.get(component.metricKey);
                      if (metric) {
                        setSelectedMetricKey(metric.key);
                        const evidence = evidenceForMetric(metric)[0];
                        if (evidence) setSelectedFactId(evidence.id);
                        setAgentOutput(`${component.label} 权重 ${component.weight}%，贡献 ${component.contribution} 分。${metric.suggestion}`);
                      }
                    }}
                  >
                    <span>{component.label}</span>
                    <strong>{formatPercent(component.value)}</strong>
                    <em>{component.weight}% 权重 · +{component.contribution}</em>
                  </button>
                ))}
              </div>
              <div className="insight-northstar-agent">
                <span>主要拉升</span>
                <strong>{northStar.lift}</strong>
                <span>主要拖累</span>
                <strong>{northStar.drag}</strong>
              </div>
            </div>
            <div className="insight-scope-stats">
              {[
                ["范围", rangeConfig.label, rangeConfig.reportScope],
                ["事实", dataset.facts.length, "音频/事件/单据"],
                ["证据", evidenceComplete.length, "可追溯"],
                ["风险", riskFacts.length, "待处理"]
              ].map(([label, value, note]) => (
                <button key={label} type="button" onClick={() => setAgentOutput(`${label} 数据来自当前本地事实表，${note}。`)}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <em>{note}</em>
                </button>
              ))}
            </div>
            <div className="insight-scope-stats" aria-label="当前指标冻结统计口径">
              {scopeCards.map(([label, value, note]) => (
                <button
                  key={label}
                  type="button"
                  title={`${value} · ${note}`}
                  onClick={() => setAgentOutput(`${selectedMetric.label} · ${label}：${value}；${note}。`)}
                >
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <em>{note}</em>
                </button>
              ))}
            </div>
            <div className="insight-metric-list">
              {visibleMetrics.map((metric) => {
                const metricScope = buildMetricScopePresentation(metricSnapshotByKey.get(metric.key));
                const delta = metricDeltaPresentation(metric.delta, metricScope);
                return (
                  <button
                    key={metric.key}
                    type="button"
                    className={`${metric.tone} ${selectedMetric.key === metric.key ? "active" : ""}`}
                    onClick={() => {
                      setSelectedMetricKey(metric.key);
                      const evidence = evidenceForMetric(metric)[0];
                      if (evidence) setSelectedFactId(evidence.id);
                      setAgentOutput(`${metric.label} 口径：${metric.formula}。${delta.reason ?? metric.insight}`);
                    }}
                  >
                    <span>{metric.label}</span>
                    <strong>{metric.value}</strong>
                    <b title={delta.reason ?? undefined}>{delta.text}</b>
                    <em>{metric.sampleSize ? `样本 ${metric.sampleSize} · ` : ""}{metric.meaning}{delta.reason ? ` · ${delta.reason}` : ""}</em>
                  </button>
                );
              })}
            </div>
            <div className="insight-lineage-strip">
              {["音频片段", "ASR", "标签资产", "业务事件", "报告资产"].map((item, index) => (
                <button key={item} type="button" onClick={() => setActiveModule(index < 2 ? "listening" : index === 2 ? "labels" : index === 3 ? "data" : "assets")}>
                  {item}
                </button>
              ))}
            </div>
          </section>
  );
}
