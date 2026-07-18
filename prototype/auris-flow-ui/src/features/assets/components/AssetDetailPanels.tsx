import { GitBranch, RotateCcw, ShieldCheck } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import type { AssetsWorkspace } from "../useAssetsWorkspace";

export function AssetDetailPanel({
  wide = false,
  workspace
}: {
  wide?: boolean;
  workspace: AssetsWorkspace;
}) {
  const { selectedAsset } = workspace;
  return (
    <section className={wide ? "module-panel wide asset-detail-panel asset-detail-expanded" : "module-panel asset-detail-panel"}>
      <PanelHeader title="资产详情" subtitle="业务信息 + 底层映射只在架构视图中出现" icon={<ShieldCheck size={16} />} />
      <div className="asset-detail-hero">
        <span>{selectedAsset.domain}</span>
        <strong>{selectedAsset.name}</strong>
        <b>{selectedAsset.quality}</b>
      </div>
      <div className="asset-detail-grid">
        {[
          ["资产 Key", selectedAsset.assetKey],
          ["Definition", selectedAsset.definition],
          ["分区策略", selectedAsset.partition],
          ["生成记录", selectedAsset.materialization],
          ["负责人", selectedAsset.owner],
          ["回填策略", selectedAsset.backfill],
          ["上游来源", selectedAsset.upstream],
          ["下游影响", selectedAsset.downstream.join(" / ")]
        ].map(([label, value]) => (
          <span key={label}>
            <b>{label}</b>
            {value}
          </span>
        ))}
      </div>
      <div className="asset-check-tags">
        {selectedAsset.checks.map((check) => (
          <em key={check}>{check}</em>
        ))}
      </div>
    </section>
  );
}

export function AssetLineageSummaryPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const { createBackfillDraft, selectedAsset, setActiveTab } = workspace;
  return (
    <section className="module-panel asset-lineage-summary-panel">
      <PanelHeader title="资产血缘摘要" subtitle="完整编排图在资产血缘页查看，当前卡片只保留可操作摘要" icon={<GitBranch size={16} />} />
      <div className="asset-lineage-summary-path" aria-label="当前资产血缘摘要路径">
        <button type="button" onClick={() => setActiveTab("lineage")}>
          <span>上游来源</span>
          <strong>{selectedAsset.upstream}</strong>
          <em>外部数据源 / API</em>
        </button>
        <i>→</i>
        <button type="button" className="current" onClick={() => setActiveTab("lineage")}>
          <span>当前资产</span>
          <strong>{selectedAsset.name}</strong>
          <em>{selectedAsset.assetKey}</em>
        </button>
        <i>→</i>
        <button type="button" onClick={() => setActiveTab("lineage")}>
          <span>下游影响</span>
          <strong>{selectedAsset.downstream.slice(0, 2).join(" / ")}</strong>
          <em>{selectedAsset.downstream.length} 个下游对象</em>
        </button>
      </div>
      <div className="asset-lineage-summary-grid">
        {[
          ["执行定义", selectedAsset.definition],
          ["Partition", selectedAsset.partition],
          ["资产生成记录", selectedAsset.materialization],
          ["Asset Check", selectedAsset.checks.join(" / ")]
        ].map(([label, value]) => (
          <span key={label}>
            <b>{label}</b>
            {value}
          </span>
        ))}
      </div>
      <div className="asset-lineage-summary-actions">
        <button type="button" onClick={() => setActiveTab("lineage")}>
          <GitBranch size={14} />
          查看完整血缘图
        </button>
        <button type="button" onClick={() => createBackfillDraft(selectedAsset.assetKey, "血缘摘要入口生成")}>
          <RotateCcw size={14} />
          创建回填草稿
        </button>
      </div>
    </section>
  );
}
