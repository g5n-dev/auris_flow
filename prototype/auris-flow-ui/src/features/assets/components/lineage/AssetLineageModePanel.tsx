import { assetRunTimeline } from "../../fixtures";
import type { AssetLineageWorkspace } from "./useAssetLineage";

export function AssetLineageModePanel({ workspace }: { workspace: AssetLineageWorkspace }) {
  const {
    activeNode,
    backfillPlanRows,
    directDownstream,
    directUpstream,
    handleNodeClick,
    impactedSamples,
    lineageMode,
    reviewQueueRows,
    setLineageActionNote
  } = workspace;
  if (lineageMode === "backfill") {
    return (
      <div className="asset-lineage-plan-list">
        {backfillPlanRows.map(([label, value]) => (
          <span key={label}>
            <b>{label}</b>
            {value}
          </span>
        ))}
      </div>
    );
  }
  if (lineageMode === "runs") {
    return (
      <div className="asset-lineage-run-list">
        {assetRunTimeline.map(([time, asset, state, runId]) => (
          <button key={runId} type="button" className={asset.includes(activeNode.label.slice(0, 2)) || activeNode.label.includes(asset.slice(0, 2)) ? "active" : ""}>
            <span>{time}</span>
            <strong>{asset}</strong>
            <em>{state}</em>
            <code>{runId}</code>
          </button>
        ))}
      </div>
    );
  }
  if (lineageMode === "review") {
    return (
      <div className="asset-lineage-review-list">
        {reviewQueueRows.map(([id, title, state]) => (
          <button key={id} type="button" onClick={() => setLineageActionNote(`${id} 已打开：${title}，可跳转调听证据工作台处理。`)}>
            <span>{id}</span>
            <strong>{title}</strong>
            <em>{state}</em>
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="asset-lineage-impact-grid">
      <div>
        <span>直接上游</span>
        {directUpstream.length > 0 ? directUpstream.map((node) => (
          <button key={node.id} type="button" onClick={() => handleNodeClick(node)}>
            {node.label}
          </button>
        )) : <em>无上游，外部数据源</em>}
      </div>
      <div>
        <span>直接下游</span>
        {directDownstream.length > 0 ? directDownstream.map((node) => (
          <button key={node.id} type="button" onClick={() => handleNodeClick(node)}>
            {node.label}
          </button>
        )) : <em>无直接下游</em>}
      </div>
      <div>
        <span>影响范围</span>
        <strong>{impactedSamples}</strong>
        <em>用于判断是否需要回填、重跑或人工确认。</em>
      </div>
    </div>
  );
}
