import type { AssetLineageWorkspace } from "./useAssetLineage";

export function AssetLineageCanvas({
  selectedAssetKey,
  workspace
}: {
  selectedAssetKey?: string;
  workspace: AssetLineageWorkspace;
}) {
  const { activeNode, edges, handleNodeClick, nodeById, nodes, relatedNodeIds } = workspace;
  return (
    <div className="asset-lineage-canvas" role="img" aria-label="数据资产上游、下游、回填和复核血缘图">
      <div className="asset-lineage-stage">
        <svg className="asset-lineage-svg" viewBox="0 0 1450 338" aria-hidden="true">
          <defs>
            <marker id="asset-lineage-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
              <path d="M0,0 L8,4.5 L0,9 Z" />
            </marker>
          </defs>
          {edges.map((edge) => {
            const from = nodeById.get(edge.from);
            const to = nodeById.get(edge.to);
            if (!from || !to) {
              return null;
            }
            const startX = from.x + 176;
            const startY = from.y + 44;
            const endX = to.x;
            const endY = to.y + 44;
            const midX = startX + (endX - startX) * 0.5;
            const active = edge.from === activeNode.id || edge.to === activeNode.id;
            const path = `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;
            return (
              <g key={`${edge.from}-${edge.to}`} className={active ? "active" : edge.dashed ? "dashed" : ""}>
                <path d={path} />
                <text x={(startX + endX) / 2} y={(startY + endY) / 2 - 8}>{edge.label}</text>
              </g>
            );
          })}
        </svg>
        {nodes.map((node) => {
          const isSelectedAsset = node.assetKey === selectedAssetKey;
          const isActive = node.id === activeNode.id;
          const isRelated = relatedNodeIds.has(node.id);
          return (
            <button
              key={node.id}
              type="button"
              className={[
                "asset-lineage-node",
                node.tone,
                isSelectedAsset ? "selected" : "",
                isActive ? "active" : "",
                isRelated ? "related" : "dimmed"
              ].join(" ")}
              style={{ left: node.x, top: node.y }}
              onClick={() => handleNodeClick(node)}
            >
              <span>{node.status}</span>
              <strong>{node.label}</strong>
              <code>{node.assetKey ?? node.meta}</code>
              <em>{node.quality ? `质量 ${node.quality}` : node.dagster}</em>
            </button>
          );
        })}
      </div>
    </div>
  );
}
