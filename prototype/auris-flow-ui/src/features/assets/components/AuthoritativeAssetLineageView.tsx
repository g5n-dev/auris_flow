import type {
  AuthoritativeAssetLineage,
  AuthoritativeAssetLineageNode
} from "../authoritativeAssetLineage";
import { useAuthoritativeAssetLineage } from "../useAuthoritativeAssetLineage";

function nodeTrace(node: AuthoritativeAssetLineageNode) {
  return node.rootTraceId ?? node.traceId ?? "trace_id 未提供";
}

function AuthoritativeLineageContent({ value }: { value: AuthoritativeAssetLineage }) {
  return (
    <div className="asset-lineage-projection-truth" data-testid="asset-lineage-authoritative">
      <div className="asset-lineage-summary-path" aria-label="BFF 权威资产血缘摘要">
        <span><b>当前资产</b><strong>{value.asset.label}</strong></span>
        <i>·</i>
        <span><b>节点</b><strong>{value.nodes.length}</strong></span>
        <i>·</i>
        <span><b>边</b><strong>{value.edges.length}</strong></span>
        <i>·</i>
        <span><b>生成记录</b><strong>{value.materializations.length}</strong></span>
      </div>
      <code>{value.asset.assetKey}</code>

      <div className="asset-lineage-impact-grid" aria-label="BFF 权威血缘节点">
        {value.nodes.map((node) => (
          <div key={node.id} data-node-type={node.nodeType}>
            <span>{node.direction}</span>
            <strong>{node.label}</strong>
            <code>{node.id}</code>
            <em>{node.runId ?? node.partitionKey ?? nodeTrace(node)}</em>
          </div>
        ))}
      </div>

      <div className="asset-lineage-plan-list" aria-label="BFF 权威血缘边">
        {value.edges.map((edge) => (
          <span key={edge.id}>
            <b>{edge.relation ?? edge.direction}</b>
            <code>{edge.from} → {edge.to}</code>
            <em>{edge.source} · {edge.traceId ?? "trace_id 未提供"}</em>
          </span>
        ))}
      </div>

      <div className="asset-lineage-run-list" aria-label="BFF 权威资产生成记录">
        {value.materializations.length ? value.materializations.map((materialization) => (
          <div key={materialization.id}>
            <span>{materialization.status}</span>
            <strong>{materialization.id}</strong>
            <em>{materialization.partitionKey ?? "partition_key 未提供"}</em>
            <code>{materialization.runId ?? "run_id 未提供"}</code>
          </div>
        )) : <p>materializations 为空，未补造运行记录。</p>}
      </div>
    </div>
  );
}

export function AuthoritativeAssetLineageView({ assetKey, scopeKey }: { assetKey: string; scopeKey: string }) {
  const state = useAuthoritativeAssetLineage(assetKey, scopeKey);

  if (state.status === "error") {
    return (
      <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-lineage-error" role="alert">
        <strong>资产 lineage 读取失败</strong>
        <span>{state.reason}；未回落本地 fixture。</span>
      </div>
    );
  }
  if (state.status === "empty" && state.value) {
    return (
      <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-lineage-empty" role="status">
        <strong>当前资产暂无血缘边或生成记录</strong>
        <span>{state.value.asset.assetKey} 已由 BFF 确认存在；未补造上游、下游或 materialization。</span>
      </div>
    );
  }
  if (state.status === "ready" && state.value) {
    return <AuthoritativeLineageContent value={state.value} />;
  }
  return (
    <div className="asset-detail-unavailable tenant-empty-state" data-testid="asset-lineage-loading" role="status">
      <strong>正在读取资产 lineage</strong>
      <span>{assetKey}</span>
    </div>
  );
}
