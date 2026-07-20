import { GitBranch, RotateCcw, ShieldCheck } from "lucide-react";

import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import type { AssetsWorkspace } from "../useAssetsWorkspace";

function authoritativeCheckStateLabel(workspace: AssetsWorkspace) {
  const state = workspace.authoritativeAssetChecks;
  if (state.status === "loading" || state.status === "idle") return "权威 checks 读取中";
  if (state.status === "error") return `权威 checks 读取失败：${state.reason}`;
  if (state.status === "empty" || !state.value?.checks.length) return "权威详情未返回 checks";
  return state.value.checks.map((check) => `${check.name} (${check.status})`).join(" / ");
}

export function AssetDetailPanel({
  wide = false,
  workspace
}: {
  wide?: boolean;
  workspace: AssetsWorkspace;
}) {
  const { authoritativeAssetChecks, selectedAsset } = workspace;
  return (
    <section className={wide ? "module-panel wide asset-detail-panel asset-detail-expanded" : "module-panel asset-detail-panel"}>
      <PanelHeader title="资产详情" subtitle="业务信息 + 底层映射只在架构视图中出现" icon={<ShieldCheck size={16} />} />
      <div className="asset-detail-hero">
        <span>{selectedAsset.domain}</span>
        <strong>{selectedAsset.name}</strong>
        <b>{selectedAsset.quality ?? "未提供"}</b>
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
          ["下游影响", selectedAsset.downstream.join(" / ") || "BFF 未提供"]
        ].map(([label, value]) => (
          <span key={label}>
            <b>{label}</b>
            {value}
          </span>
        ))}
      </div>
      <div className="asset-check-tags">
        {authoritativeAssetChecks.status === "ready" && authoritativeAssetChecks.value?.checks.length
          ? authoritativeAssetChecks.value.checks.map((check) => (
              <em key={check.id} title={check.id}>{check.name} · {check.status}</em>
            ))
          : <em>{authoritativeCheckStateLabel(workspace)}</em>}
      </div>
    </section>
  );
}

export function AssetLineageSummaryPanel({ workspace }: { workspace: AssetsWorkspace }) {
  const { createBackfillDraft, selectedAsset, setActiveTab } = workspace;
  return (
    <section className="module-panel asset-lineage-summary-panel">
      <PanelHeader title="资产血缘摘要" subtitle={LABEL_DEMO_MODE ? "完整编排图在资产血缘页查看，当前卡片只保留可操作摘要" : "仅展示最近资产投影中的上下游字段；完整 lineage 明细尚未读取"} icon={<GitBranch size={16} />} />
      <div className="asset-lineage-summary-path" aria-label="当前资产血缘摘要路径">
        <button type="button" onClick={() => setActiveTab("lineage")}>
          <span>上游来源</span>
          <strong>{selectedAsset.upstream}</strong>
          <em>{LABEL_DEMO_MODE ? "外部数据源 / API" : "recent projection: upstream"}</em>
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
          <strong>{selectedAsset.downstream.slice(0, 2).join(" / ") || "BFF 未提供"}</strong>
          <em>{selectedAsset.downstream.length ? `${selectedAsset.downstream.length} 个投影下游对象` : "lineage 明细未读取"}</em>
        </button>
      </div>
      <div className="asset-lineage-summary-grid">
        {[
          ["执行定义", selectedAsset.definition],
          ["Partition", selectedAsset.partition],
          ["资产生成记录", selectedAsset.materialization],
          ["Asset Check", authoritativeCheckStateLabel(workspace)]
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
