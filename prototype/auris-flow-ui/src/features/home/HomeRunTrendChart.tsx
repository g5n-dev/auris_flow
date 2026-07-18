import { ArrowRight } from "lucide-react";
import type { CSSProperties } from "react";

import type { ModuleKey } from "../../shared/contracts/navigation";
import { clamp } from "../../shared/runtime/math";
import {
  homeRunTrendLabels,
  homeRunTrendSeries,
  type HomeRunTrendKey,
  type HomeRunTrendSeries
} from "./fixtures";

function buildSparklinePath(values: number[], width: number, height: number, padding = 4) {
  if (!values.length) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = values.length > 1 ? (width - padding * 2) / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      const x = padding + index * step;
      const y = padding + (1 - (value - min) / range) * (height - padding * 2);
      return `${index === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

export function HomeRunSparkline({
  values,
  color
}: {
  values: number[];
  color: string;
}) {
  const width = 132;
  const height = 36;
  const path = buildSparklinePath(values, width, height, 5);
  const areaPath = path ? `${path} L ${width - 5} ${height - 5} L 5 ${height - 5} Z` : "";
  return (
    <svg className="home-run-sparkline" viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <path d={areaPath} fill={color} opacity="0.1" />
      <path d={path} fill="none" stroke={color} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      {values.map((value, index) => {
        const min = Math.min(...values);
        const max = Math.max(...values);
        const range = max - min || 1;
        const x = 5 + index * ((width - 10) / Math.max(1, values.length - 1));
        const y = 5 + (1 - (value - min) / range) * (height - 10);
        return <circle key={`${value}-${index}`} cx={x} cy={y} r={index === values.length - 1 ? 2.7 : 1.8} fill={color} opacity={index === values.length - 1 ? 1 : 0.45} />;
      })}
    </svg>
  );
}

export function HomeRunTrendChart({
  activeKey,
  activePointIndex,
  onSelectSeries,
  onSelectPoint,
  onNavigate
}: {
  activeKey: HomeRunTrendKey;
  activePointIndex: number;
  onSelectSeries: (key: HomeRunTrendKey) => void;
  onSelectPoint: (index: number) => void;
  onNavigate: (module: ModuleKey) => void;
}) {
  const activeSeries = homeRunTrendSeries.find((series) => series.key === activeKey) ?? homeRunTrendSeries[0];
  const safePointIndex = clamp(activePointIndex, 0, homeRunTrendLabels.length - 1);
  const width = 620;
  const height = 236;
  const pad = { top: 24, right: 22, bottom: 38, left: 44 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const pointX = (index: number) => pad.left + index * (plotWidth / Math.max(1, homeRunTrendLabels.length - 1));
  const toChartValue = (series: HomeRunTrendSeries, value: number) => {
    if (series.key === "queue") return clamp((value / 35) * 100, 0, 100);
    if (series.key === "failed") return clamp((value / 5) * 100, 0, 100);
    return clamp(value, 0, 100);
  };
  const pointY = (series: HomeRunTrendSeries, value: number) => pad.top + (1 - toChartValue(series, value) / 100) * plotHeight;
  const pathForSeries = (series: HomeRunTrendSeries) =>
    series.values
      .map((value, index) => `${index === 0 ? "M" : "L"} ${pointX(index).toFixed(1)} ${pointY(series, value).toFixed(1)}`)
      .join(" ");
  const activeValue = activeSeries.values[safePointIndex] ?? activeSeries.values[activeSeries.values.length - 1] ?? 0;
  const previousValue = activeSeries.values[Math.max(0, safePointIndex - 1)] ?? activeValue;
  const delta = activeValue - previousValue;
  const activeDateLabel = homeRunTrendLabels[safePointIndex] ?? "当前";

  return (
    <div className="home-run-trend-card">
      <div className="home-run-trend-head">
        <div>
          <span>24h 归一化曲线</span>
          <strong>{activeSeries.label} · {activeValue.toFixed(activeSeries.unit === "%" ? 1 : 0)}{activeSeries.unit}</strong>
        </div>
        <button type="button" onClick={() => onNavigate(activeSeries.route)}>
          下钻{activeSeries.label}
          <ArrowRight size={13} />
        </button>
      </div>

      <div className="home-run-trend-body">
        <svg className="home-run-line-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="运行趋势曲线">
          {[0, 25, 50, 75, 100].map((tick) => {
            const y = pad.top + (1 - tick / 100) * plotHeight;
            return (
              <g key={tick}>
                <line x1={pad.left} x2={width - pad.right} y1={y} y2={y} />
                <text x={pad.left - 10} y={y + 4} textAnchor="end">{tick}</text>
              </g>
            );
          })}
          <line className="target" x1={pad.left} x2={width - pad.right} y1={pad.top + plotHeight * 0.25} y2={pad.top + plotHeight * 0.25} />
          <text className="target-label" x={width - pad.right - 2} y={pad.top + plotHeight * 0.25 - 6} textAnchor="end">门禁线</text>
          <line className="cursor" x1={pointX(safePointIndex)} x2={pointX(safePointIndex)} y1={pad.top - 8} y2={height - pad.bottom + 8} />
          {homeRunTrendLabels.map((label, index) => (
            <text key={label} className="x-label" x={pointX(index)} y={height - 12} textAnchor="middle">{label}</text>
          ))}
          <g className="event-marker" onClick={() => onSelectPoint(3)}>
            <line x1={pointX(3)} x2={pointX(3)} y1={pad.top - 8} y2={height - pad.bottom + 8} />
            <rect x={pointX(3) - 42} y={pad.top - 20} width="84" height="22" rx="11" />
            <text x={pointX(3)} y={pad.top - 5} textAnchor="middle">12:33 异常</text>
          </g>
          {homeRunTrendSeries.map((series) => {
            const isActive = series.key === activeKey;
            return (
              <g key={series.key} className={isActive ? "active" : "muted"} onClick={() => onSelectSeries(series.key)}>
                <path d={pathForSeries(series)} fill="none" stroke={series.color} strokeWidth={isActive ? 4 : 2.3} strokeLinecap="round" strokeLinejoin="round" />
                {series.values.map((value, index) => (
                  <circle
                    key={`${series.key}-${index}`}
                    cx={pointX(index)}
                    cy={pointY(series, value)}
                    r={isActive && index === safePointIndex ? 6 : 4}
                    fill={series.color}
                    stroke="#fff"
                    strokeWidth={isActive && index === safePointIndex ? 2.5 : 1.4}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelectSeries(series.key);
                      onSelectPoint(index);
                    }}
                  />
                ))}
              </g>
            );
          })}
        </svg>

        <aside className="home-run-trend-readout">
          <span>{activeDateLabel}</span>
          <strong>{activeSeries.label}</strong>
          <b>{activeValue.toFixed(activeSeries.unit === "%" ? 1 : 0)}{activeSeries.unit}</b>
          <em className={delta < 0 ? "down" : "up"}>{delta >= 0 ? "+" : ""}{delta.toFixed(activeSeries.unit === "%" ? 1 : 0)}{activeSeries.unit}</em>
          <p>{activeSeries.driver}</p>
        </aside>
      </div>

      <div className="home-run-trend-legend">
        {homeRunTrendSeries.map((series) => {
          const latest = series.values[series.values.length - 1] ?? 0;
          const previous = series.values[series.values.length - 2] ?? latest;
          const latestDelta = latest - previous;
          return (
            <button
              key={series.key}
              type="button"
              className={series.key === activeKey ? "active" : ""}
              style={{ "--trend-color": series.color } as CSSProperties}
              onClick={() => {
                onSelectSeries(series.key);
                onSelectPoint(homeRunTrendLabels.length - 1);
              }}
            >
              <i />
              <span>{series.label}</span>
              <strong>{latest.toFixed(series.unit === "%" ? 1 : 0)}{series.unit}</strong>
              <em className={latestDelta < 0 ? "down" : "up"}>{latestDelta >= 0 ? "+" : ""}{latestDelta.toFixed(series.unit === "%" ? 1 : 0)}{series.unit}</em>
            </button>
          );
        })}
      </div>
    </div>
  );
}
