import { BarChart3, BookOpen, Database } from "lucide-react";
import type { CSSProperties } from "react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { assetDagsterCompatibilityChecks, assetRows } from "../catalog";
import type { AssetsWorkspace } from "../useAssetsWorkspace";

export function AssetCommandPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const {
    compatibilityScore,
    compatibleCount,
    createBackfillDraft,
    currentTabLabel,
    rerunAssetQuality,
    selectedAsset,
    setActiveTab
  } = workspace;
  return (
    <section className="module-panel wide asset-command-panel">
      <PanelHeader title="数据资产中心" subtitle={`${currentTabLabel} / 业务资产语言映射资产 Key、分区、生成记录和回填`} icon={<BookOpen size={16} />} sticky />
      <div className="asset-governance-strip">
        {[
          ["兼容门禁", `${compatibilityScore}%`, `${compatibleCount}/${assetDagsterCompatibilityChecks.length} 通过`, "blue"],
          ["待人工确认", "2", "覆盖回填 / 人工标注影响", "amber"],
          ["失败分区", "3", "ASR 转写资产待重跑", "red"],
          ["最近生成", "12:41", "评测指标资产排队中", "green"]
        ].map(([label, value, meta, tone]) => (
          <button
            key={label}
            type="button"
            className={`asset-governance-card ${tone}`}
            onClick={() => {
              if (label === "失败分区") rerunAssetQuality();
              if (label === "兼容门禁") setActiveTab("quality");
              if (label === "待人工确认") createBackfillDraft(selectedAsset.assetKey, "兼容门禁人工确认生成");
            }}
          >
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{meta}</em>
          </button>
        ))}
      </div>
    </section>
  );
}

export function AssetChartPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const {
    assetDomainSummary,
    avgAssetQuality,
    createBackfillDraft,
    healthyCount,
    healthyPercent,
    maxDomainCount,
    qualityChart,
    qualityPath,
    qualityX,
    qualityY,
    selectedAsset,
    setActiveTab,
    setSelectedAssetKey,
    statusSummary
  } = workspace;
  return (
    <section className="module-panel wide asset-chart-panel">
      <PanelHeader title="资产图表总览" subtitle="目录分布、质量链路、状态风险和当前资产血缘影响联动" icon={<BarChart3 size={16} />} sticky />
      <div className="asset-chart-layout">
        <div className="asset-chart-card asset-domain-chart">
          <div className="asset-chart-card-head sticky-card-head">
            <span>资产域分布</span>
            <strong>{assetRows.length} 个目录对象</strong>
          </div>
          <div className="asset-domain-bars">
            {assetDomainSummary.map((item) => {
              const firstAsset = assetRows.find((asset) => asset.domain === item.domain) ?? assetRows[0];
              return (
                <button key={item.domain} type="button" onClick={() => setSelectedAssetKey(firstAsset.assetKey)}>
                  <span>
                    <b>{item.domain}</b>
                    <em>{item.count} 个 · 均分 {item.avgQuality}</em>
                  </span>
                  <i>
                    <strong style={{ width: `${(item.count / maxDomainCount) * 100}%` }} />
                  </i>
                  <small>{item.attention ? `${item.attention} 个需处理` : "全部可用"}</small>
                </button>
              );
            })}
          </div>
        </div>

        <div className="asset-chart-card asset-quality-trend-card">
          <div className="asset-chart-card-head horizontal sticky-card-head">
            <span>血缘质量链路</span>
            <strong>{avgAssetQuality} 平均分</strong>
          </div>
          <svg className="asset-quality-line-chart" viewBox={`0 0 ${qualityChart.width} ${qualityChart.height}`} role="img" aria-label="资产血缘质量链路折线图">
            {[0, 1, 2].map((index) => {
              const y = qualityChart.top + index * ((qualityChart.bottom - qualityChart.top) / 2);
              return <line key={index} x1={qualityChart.left} x2={qualityChart.right} y1={y} y2={y} />;
            })}
            <path d={qualityPath} />
            {assetRows.map((asset, index) => {
              const active = asset.assetKey === selectedAsset.assetKey;
              return (
                <g key={asset.assetKey} className={active ? "active" : ""}>
                  <circle cx={qualityX(index)} cy={qualityY(asset.quality)} r={active ? 5 : 4} />
                  <text x={qualityX(index)} y={qualityChart.height - 18}>
                    {asset.name.replace("资产", "").slice(0, 4)}
                  </text>
                </g>
              );
            })}
          </svg>
          <div className="asset-quality-current">
            <span>{selectedAsset.name}</span>
            <strong>{selectedAsset.quality}</strong>
            <em>{selectedAsset.status} · {selectedAsset.freshness}</em>
          </div>
        </div>

        <div className="asset-chart-card asset-status-chart">
          <div className="asset-chart-card-head sticky-card-head">
            <span>状态占比</span>
            <strong>{healthyPercent}% 可用</strong>
          </div>
          <div
            className="asset-status-donut"
            style={{
              "--asset-ok": `${healthyPercent * 3.6}deg`,
              "--asset-warn": `${((statusSummary.find((item) => item.label === "待回填")?.count ?? 0) / assetRows.length) * 360}deg`
            } as CSSProperties}
          >
            <strong>{healthyCount}</strong>
            <span>已生成</span>
          </div>
          <div className="asset-status-list">
            {statusSummary.map((item) => (
              <button key={item.label} type="button" className={item.tone} onClick={() => setSelectedAssetKey(assetRows.find((asset) => asset.status.includes(item.label) || asset.status === item.label)?.assetKey ?? selectedAsset.assetKey)}>
                <span>{item.label}</span>
                <strong>{item.count}</strong>
              </button>
            ))}
          </div>
        </div>

        <div className="asset-chart-card asset-impact-chart">
          <div className="asset-chart-card-head sticky-card-head">
            <span>当前资产影响</span>
            <strong>{selectedAsset.downstream.length} 个下游</strong>
          </div>
          <div className="asset-impact-flow">
            <button type="button" onClick={() => setActiveTab("lineage")}>
              <span>上游</span>
              <strong>{selectedAsset.upstream}</strong>
            </button>
            <i>→</i>
            <button type="button" className="current" onClick={() => setActiveTab("detail")}>
              <span>当前</span>
              <strong>{selectedAsset.name}</strong>
            </button>
            <i>→</i>
            <button type="button" onClick={() => setActiveTab("lineage")}>
              <span>下游</span>
              <strong>{selectedAsset.downstream.join(" / ")}</strong>
            </button>
          </div>
          <div className="asset-impact-actions">
            <button type="button" onClick={() => setActiveTab("lineage")}>查看血缘</button>
            <button type="button" onClick={() => createBackfillDraft(selectedAsset.assetKey, "图表风险入口生成")}>生成回填</button>
          </div>
        </div>
      </div>
    </section>
  );
}

export function AssetCatalogPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const { selectedAsset, setSelectedAssetKey } = workspace;
  return (
    <section className="module-panel wide asset-catalog-panel">
      <PanelHeader title="资产目录" subtitle="租户 / 项目隔离，按数据域、状态、版本和质量分筛选" icon={<Database size={16} />} sticky />
      <div className="asset-catalog-grid">
        {assetRows.map((asset) => (
          <button
            key={asset.assetKey}
            type="button"
            className={selectedAsset.assetKey === asset.assetKey ? "asset-catalog-card active" : "asset-catalog-card"}
            onClick={() => setSelectedAssetKey(asset.assetKey)}
          >
            <div>
              <span>{asset.domain}</span>
              <b>{asset.status}</b>
            </div>
            <strong>{asset.name}</strong>
            <code>{asset.assetKey}</code>
            <em>{asset.partition}</em>
            <i>
              <span style={{ width: `${asset.quality}%` }} />
            </i>
            <small>质量 {asset.quality} · {asset.freshness}</small>
          </button>
        ))}
      </div>
    </section>
  );
}
