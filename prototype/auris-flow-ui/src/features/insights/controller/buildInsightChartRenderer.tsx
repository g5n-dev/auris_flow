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
import type { InsightReportActions } from "./buildInsightReportActions";
import type { InsightAgentAction } from "./buildInsightAgentAction";
import type { InsightChartSpec } from "../types";
import {
  buildMetricScopePresentation,
  metricSnapshotsFromProjection,
  metricDeltaPresentation
} from "../model/metricScopePresentation";
import { AlertTriangle, GitBranch, Sparkles } from "lucide-react";
import type { CSSProperties } from "react";

export function buildInsightChartRenderer(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState & InsightReportGuards & InsightReportExecution & InsightReportActions & InsightAgentAction) {
  const { activeReport, activeTrendPointIndex, activeTrendSeriesKey, downstreamLabelForChart, handleOpenDownstreamAssets, metricByKey, metricProjectionItems, pct, runAgentAction, selectEvidence, setActiveModule, setActiveTrendPointIndex, setActiveTrendSeriesKey, setSelectedMetricKey } = scope;
  const projectionMetricSnapshots = metricSnapshotsFromProjection(metricProjectionItems);
  const authoritativeMetricSnapshots = projectionMetricSnapshots.length
    ? projectionMetricSnapshots
    : activeReport?.metricSnapshots ?? [];
  const metricSnapshotByKey = new Map(
    authoritativeMetricSnapshots.map((snapshot) => [snapshot.metric_key, snapshot])
  );
  const renderChart = (chart: InsightChartSpec) => {
      if (chart.emptyReason) {
        return (
          <div className="insight-empty-state">
            <AlertTriangle size={18} />
            <strong>暂无可渲染数据</strong>
            <span>{chart.emptyReason}</span>
          </div>
        );
      }
      if (chart.type === "line" && chart.series?.length && chart.xLabels?.length) {
        const width = 840;
        const height = chart.summaryCards?.length ? 320 : 250;
        const pad = { left: 54, right: 28, top: 24, bottom: 42 };
        const allValues = chart.series.flatMap((series) => series.values);
        const min = chart.yDomain?.[0] ?? Math.max(0, Math.min(...allValues) - 8);
        const max = chart.yDomain?.[1] ?? Math.max(...allValues, 1) + 8;
        const yTicks = chart.yTicks ?? [min, min + (max - min) / 3, min + ((max - min) / 3) * 2, max].map((value) => Math.round(value));
        const chartX = (index: number) => pad.left + index * ((width - pad.left - pad.right) / Math.max(chart.xLabels!.length - 1, 1));
        const chartY = (value: number) => height - pad.bottom - ((value - min) / Math.max(max - min, 1)) * (height - pad.top - pad.bottom);
        const toPath = (values: number[]) => values.map((value, index) => `${index === 0 ? "M" : "L"}${chartX(index).toFixed(1)} ${chartY(value).toFixed(1)}`).join(" ");
        const hasFocusedSeries = chart.series.some((series) => series.key === activeTrendSeriesKey);
        const focusedSeries = chart.series.find((series) => series.key === activeTrendSeriesKey) ?? chart.series.find((series) => series.emphasis) ?? chart.series[0];
        const pointIndex = Math.min(activeTrendPointIndex, Math.max(chart.xLabels.length - 1, 0));
        const factIdFor = (series: NonNullable<InsightChartSpec["series"]>[number], index: number) => {
          if (!series.factIds?.length) return undefined;
          return series.factIds[index] ?? series.factIds[index % series.factIds.length];
        };
        const focusValue = focusedSeries.values[Math.min(pointIndex, focusedSeries.values.length - 1)] ?? focusedSeries.values[focusedSeries.values.length - 1];
        const focusedMetric = focusedSeries.metricKey ? metricByKey.get(focusedSeries.metricKey) : undefined;
        const focusedDelta = metricDeltaPresentation(
          focusedSeries.delta ?? "持平",
          buildMetricScopePresentation(
            focusedSeries.metricKey ? metricSnapshotByKey.get(focusedSeries.metricKey) : undefined
          )
        );
        const focusLabel = chart.xLabels[pointIndex] ?? chart.xLabels[chart.xLabels.length - 1];
        const selectTrendPoint = (series: NonNullable<InsightChartSpec["series"]>[number], index: number) => {
          setActiveTrendSeriesKey(series.key);
          setActiveTrendPointIndex(index);
          if (series.metricKey && metricByKey.has(series.metricKey)) setSelectedMetricKey(series.metricKey);
          const value = series.values[index] ?? series.values[series.values.length - 1];
          selectEvidence(factIdFor(series, index), chart.id, `${series.label} ${chart.xLabels?.[index]} 为 ${value}${series.unit ?? ""}`);
        };
        return (
          <div className={`insight-chart-body ${chart.summaryCards?.length ? "explainable" : "compact"}`}>
            <div className="insight-line-main">
              <svg className="insight-spec-line" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
                {yTicks.map((tick) => {
                  const y = chartY(tick);
                  return (
                    <g key={tick} className="axis-tick">
                      <line className="grid-line" x1={pad.left - 8} x2={width - pad.right} y1={y} y2={y} />
                      <text x={pad.left - 18} y={y + 4} textAnchor="end">{tick}</text>
                    </g>
                  );
                })}
                {chart.targetLine && (
                  <g className={`target-line ${chart.targetLine.tone ?? "blue"}`}>
                    <line x1={pad.left - 8} x2={width - pad.right} y1={chartY(chart.targetLine.value)} y2={chartY(chart.targetLine.value)} />
                    <text x={width - pad.right} y={chartY(chart.targetLine.value) - 6} textAnchor="end">{chart.targetLine.label}</text>
                  </g>
                )}
                <line className="focus-cursor" x1={chartX(pointIndex)} x2={chartX(pointIndex)} y1={pad.top - 4} y2={height - pad.bottom + 8} />
                {chart.eventMarkers?.map((marker) => {
                  const markerX = chartX(Math.min(marker.index, chart.xLabels!.length - 1));
                  return (
                    <g
                      key={`${marker.label}-${marker.index}`}
                      className={`event-marker ${marker.tone ?? "blue"}`}
                      onClick={() => {
                        setActiveTrendPointIndex(Math.min(marker.index, chart.xLabels!.length - 1));
                        if (marker.seriesKey) setActiveTrendSeriesKey(marker.seriesKey);
                        selectEvidence(marker.factId, chart.id, marker.detail);
                      }}
                    >
                      <line x1={markerX} x2={markerX} y1={pad.top} y2={height - pad.bottom} />
                      <circle cx={markerX} cy={pad.top + 6} r={4.2} />
                      <text x={markerX + 7} y={pad.top + 10}>{marker.label}</text>
                    </g>
                  );
                })}
                {chart.xLabels.map((label, index) => <text key={label} className="x-label" x={chartX(index)} y={height - 12} textAnchor="middle">{label}</text>)}
                {chart.series.map((series) => {
                  const isFocused = !hasFocusedSeries || series.key === focusedSeries.key;
                  return (
                    <g key={series.key} className={`${series.tone} ${series.emphasis ? "emphasis" : ""} ${isFocused ? "focused" : "dimmed"}`}>
                      <path d={toPath(series.values)} onClick={() => selectTrendPoint(series, pointIndex)} />
                      {series.values.map((value, index) => (
                        <circle
                          key={`${series.key}-${index}`}
                          className={`insight-chart-point ${index === pointIndex && series.key === focusedSeries.key ? "active" : ""}`}
                          cx={chartX(index)}
                          cy={chartY(value)}
                          r={index === pointIndex && series.key === focusedSeries.key ? 6 : index === series.values.length - 1 ? 4.8 : 3.5}
                          onClick={() => selectTrendPoint(series, index)}
                        />
                      ))}
                    </g>
                  );
                })}
              </svg>
              <div className="insight-chart-legend">
                {chart.series.map((series) => {
                  const lastValue = series.values[series.values.length - 1];
                  const delta = metricDeltaPresentation(
                    series.delta ?? "持平",
                    buildMetricScopePresentation(
                      series.metricKey ? metricSnapshotByKey.get(series.metricKey) : undefined
                    )
                  );
                  return (
                    <button
                      key={series.key}
                      className={`${series.tone} ${series.key === focusedSeries.key ? "active" : ""}`}
                      type="button"
                      onClick={() => selectTrendPoint(series, Math.min(pointIndex, series.values.length - 1))}
                    >
                      <span>{series.label}</span>
                      <b>{lastValue}{series.unit ?? ""}</b>
                      {series.delta && <em className={delta.visible ? series.direction : undefined} title={delta.reason ?? undefined}>{delta.text}</em>}
                    </button>
                  );
                })}
              </div>
            </div>
            {chart.summaryCards?.length && (
              <aside className="insight-trend-panel">
                <div className={`insight-trend-focus ${focusedSeries.tone}`}>
                  <span>当前焦点 · {focusLabel}</span>
                  <strong>{focusedSeries.label} {focusValue}{focusedSeries.unit ?? ""}</strong>
                  <em>{focusedSeries.unitLabel ?? "统一指数"} · {focusedDelta.text} · {focusedMetric?.owner ?? "Insight Agent"}</em>
                  <p>{focusedDelta.reason ?? focusedMetric?.insight ?? `${focusedSeries.label} 已按同一事实表聚合，可点击折线点下钻证据。`}</p>
                </div>
                <div className="insight-trend-summary-grid">
                  {chart.summaryCards.map((card) => (
                    <button
                      key={card.title}
                      type="button"
                      className={`${card.tone} ${card.seriesKey === focusedSeries.key ? "active" : ""}`}
                      title="在当前趋势图内聚焦该归因；跳转请使用下方“下钻证据”"
                      onClick={() => {
                        if (card.seriesKey) setActiveTrendSeriesKey(card.seriesKey);
                        setActiveTrendPointIndex(Math.min(card.pointIndex ?? chart.xLabels!.length - 1, chart.xLabels!.length - 1));
                        if (card.metricKey && metricByKey.has(card.metricKey)) setSelectedMetricKey(card.metricKey);
                        selectEvidence(card.factId, chart.id, `${card.title}：${card.value}，${card.detail}。已在当前趋势图聚焦，未跳转模块`);
                      }}
                    >
                      <span>{card.title}</span>
                      <strong>{card.value}</strong>
                      <em>{card.detail}</em>
                    </button>
                  ))}
                </div>
                <div className="insight-trend-actions">
                  <button type="button" onClick={() => runAgentAction("diagnose")}>
                    <Sparkles size={14} />
                    解释波动
                  </button>
                  <button type="button" onClick={() => setActiveModule(focusedMetric?.drilldownRoute ?? focusedMetric?.route ?? "listening")}>
                    <GitBranch size={14} />
                    下钻证据
                  </button>
                </div>
              </aside>
            )}
          </div>
        );
      }
      if (chart.type === "sankey" && chart.nodes?.length && chart.links?.length) {
        const width = 640;
        const height = 250;
        const columns = [0, 1, 2].map((column) => chart.nodes!.filter((node) => node.column === column));
        const positions = new Map<string, { x: number; y: number; w: number; h: number }>();
        columns.forEach((nodes, column) => {
          nodes.forEach((node, index) => {
            positions.set(node.id, { x: 28 + column * 250, y: 32 + index * 55, w: column === 1 ? 118 : 132, h: 36 });
          });
        });
        return (
          <svg className="insight-spec-sankey" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={chart.title}>
            {chart.links.map((link, index) => {
              const source = positions.get(link.source);
              const target = positions.get(link.target);
              if (!source || !target) return null;
              const startX = source.x + source.w;
              const startY = source.y + source.h / 2;
              const endX = target.x;
              const endY = target.y + target.h / 2;
              const mid = startX + (endX - startX) * 0.55;
              return (
                <path
                  key={`${link.source}-${link.target}-${index}`}
                  className={`flow ${link.tone}`}
                  d={`M ${startX} ${startY} C ${mid} ${startY}, ${mid} ${endY}, ${endX} ${endY}`}
                  style={{ strokeWidth: Math.max(6, Math.min(22, link.value)) }}
                />
              );
            })}
            {chart.nodes.map((node) => {
              const pos = positions.get(node.id);
              if (!pos) return null;
              return (
                <g key={node.id} className="insight-sankey-node" onClick={() => selectEvidence(node.factId, chart.id, `查看节点 ${node.label}`)}>
                  <rect x={pos.x} y={pos.y} width={pos.w} height={pos.h} rx="7" />
                  <text x={pos.x + 8} y={pos.y + 14}>{node.label}</text>
                  <text className="value" x={pos.x + 8} y={pos.y + 29}>{node.value}</text>
                </g>
              );
            })}
          </svg>
        );
      }
      if (chart.type === "radar" && chart.axes?.length) {
        const center = { x: 160, y: 126 };
        const radius = 86;
        const pointFor = (index: number, value: number) => {
          const angle = -Math.PI / 2 + (index / chart.axes!.length) * Math.PI * 2;
          const distance = radius * (value / 100);
          return [center.x + Math.cos(angle) * distance, center.y + Math.sin(angle) * distance];
        };
        const polygon = (kind: "value" | "compare") => chart.axes!.map((axis, index) => pointFor(index, kind === "value" ? axis.value : axis.compare).join(",")).join(" ");
        return (
          <svg className="insight-spec-radar" viewBox="0 0 320 246" role="img" aria-label={chart.title}>
            {[0.35, 0.68, 1].map((scale) => (
              <polygon key={scale} className="grid" points={chart.axes!.map((_, index) => pointFor(index, 100 * scale).join(",")).join(" ")} />
            ))}
            <polygon className="compare" points={polygon("compare")} />
            <polygon className="value" points={polygon("value")} />
            {chart.axes.map((axis, index) => {
              const [x, y] = pointFor(index, 113);
              const [dotX, dotY] = pointFor(index, axis.value);
              return (
                <g key={axis.label} className="insight-radar-axis" onClick={() => selectEvidence(axis.factId, chart.id, `${axis.label} 当前 ${axis.value}`)}>
                  <text x={x} y={y}>{axis.label}</text>
                  <circle cx={dotX} cy={dotY} r="4" />
                </g>
              );
            })}
          </svg>
        );
      }
      if (chart.type === "heatmap" && chart.heatmap) {
        return (
          <div className="insight-spec-heatmap" style={{ "--cols": chart.heatmap.columns.length + 1 } as CSSProperties}>
            <span />
            {chart.heatmap.columns.map((column) => <b key={column}>{column}</b>)}
            {chart.heatmap.rows.map((row) => (
              <div key={row.label} className="insight-heatmap-row">
                <button type="button" className="row-label" onClick={() => selectEvidence(row.factId, chart.id, `查看 ${row.label}`)}>
                  <strong>{row.label}</strong>
                  <span>{row.detail}</span>
                </button>
                {row.values.map((value, index) => (
                  <button
                    key={`${row.label}-${chart.heatmap!.columns[index]}`}
                    type="button"
                    className={["insight-heat-cell", value > 75 ? "hot" : value > 52 ? "warm" : ""].filter(Boolean).join(" ")}
                    style={{ "--heat": `${value}%` } as CSSProperties}
                    onClick={() => selectEvidence(row.factId, chart.id, `${row.label} ${chart.heatmap!.columns[index]} 点强度 ${value}`)}
                  >
                    {value}
                  </button>
                ))}
              </div>
            ))}
          </div>
        );
      }
      if (chart.type === "bar" && chart.bars?.length) {
        const max = Math.max(...chart.bars.map((bar) => bar.value), 1);
        const total = chart.bars.reduce((sum, bar) => sum + bar.value, 0);
        const leader = chart.bars[0];
        const reviewBars = chart.bars.filter((bar) => ["amber", "red", "violet"].includes(bar.tone));
        return (
          <div className="insight-bar-layout">
            <div className="insight-spec-bars">
              {chart.bars.map((bar) => (
                <button key={bar.label} type="button" className={`insight-bar-row ${bar.tone}`} onClick={() => selectEvidence(bar.factId, chart.id, `查看 ${bar.label}`)}>
                  <span>
                    <strong>{bar.label}</strong>
                    <em>{bar.detail}</em>
                  </span>
                  <b>{bar.value}</b>
                  <i style={{ width: `${Math.max(8, (bar.value / max) * 100)}%` }} />
                </button>
              ))}
            </div>
            <aside className="insight-bar-summary">
              <div className="insight-bar-summary-kpis">
                <span>
                  <em>对象数</em>
                  <strong>{chart.bars.length}</strong>
                </span>
                <span>
                  <em>头部占比</em>
                  <strong>{pct(leader.value, total)}%</strong>
                </span>
                <span>
                  <em>需复核</em>
                  <strong>{reviewBars.length}</strong>
                </span>
              </div>
              <button type="button" className={`insight-bar-focus ${leader.tone}`} onClick={() => selectEvidence(leader.factId, chart.id, `查看头部对象 ${leader.label}`)}>
                <span>当前最高命中</span>
                <strong>{leader.label}</strong>
                <em>{leader.detail}</em>
              </button>
              {chart.id.includes("tag") && (
                <div className="insight-tag-asset-map" aria-label="标签资产映射">
                  <span>标签资产映射</span>
                  {chart.bars.slice(0, 5).map((bar) => {
                    const assetKey = bar.detail.split("·").slice(-1)[0]?.trim() || "assetKey 待确认";
                    const target = bar.label.includes("冲突") || bar.label.includes("低置信") || bar.label.includes("串音") ? "人审/复核" : "报告/规则";
                    return (
                      <button key={`${chart.id}-${bar.label}-asset`} type="button" className={bar.tone} onClick={() => selectEvidence(bar.factId, chart.id, `${bar.label} 映射到 ${assetKey}`)}>
                        <strong>{bar.label}</strong>
                        <i>→</i>
                        <em>{assetKey}</em>
                        <i>→</i>
                        <b>{target}</b>
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="insight-bar-mini-list">
                {chart.bars.slice(0, 4).map((bar) => (
                  <button key={`${chart.id}-${bar.label}-mini`} type="button" onClick={() => selectEvidence(bar.factId, chart.id, `下钻 ${bar.label}`)}>
                    <span>{bar.label}</span>
                    <i className={bar.tone} style={{ width: `${Math.max(10, (bar.value / max) * 100)}%` }} />
                    <b>{bar.value}</b>
                  </button>
                ))}
              </div>
              <button type="button" className="insight-bar-route" onClick={() => handleOpenDownstreamAssets(chart)}>
                {downstreamLabelForChart(chart)}
              </button>
            </aside>
          </div>
        );
      }
      if (chart.type === "table" && chart.columns?.length && chart.rows?.length) {
        return (
          <div className="insight-spec-table" style={{ "--cols": chart.columns.length } as CSSProperties}>
            <div className="insight-spec-table-head">
              {chart.columns.map((column) => <span key={column}>{column}</span>)}
            </div>
            {chart.rows.map((row) => (
              <button key={row.id} type="button" className="insight-table-row" onClick={() => selectEvidence(row.factId, chart.id, `查看 ${row.cells[0]}`)}>
                {row.cells.map((cell, index) => <span key={`${row.id}-${index}`}>{cell}</span>)}
              </button>
            ))}
          </div>
        );
      }
      return (
        <div className="insight-empty-state">
          <AlertTriangle size={18} />
          <strong>图表配置不完整</strong>
          <span>{chart.title} 缺少可渲染数据。</span>
        </div>
      );
    };

  return {
    renderChart
  };
}

export type InsightChartRenderer = ReturnType<typeof buildInsightChartRenderer>;
