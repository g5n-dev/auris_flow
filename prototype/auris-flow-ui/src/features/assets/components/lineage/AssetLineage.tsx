import { AssetLineageCanvas } from "./AssetLineageCanvas";
import { AssetLineageModePanel } from "./AssetLineageModePanel";
import { useAssetLineage } from "./useAssetLineage";

export function AssetLineage({
  selectedAssetKey,
  onSelect,
  onCreateBackfill
}: {
  selectedAssetKey?: string;
  onSelect?: (assetKey: string) => void;
  onCreateBackfill?: (assetKey?: string) => void;
}) {
  const workspace = useAssetLineage({ selectedAssetKey, onSelect, onCreateBackfill });
  const {
    actionButtons,
    activeNode,
    lineageActionNote,
    lineageMode,
    lineageModeOptions,
    relatedNodeIds,
    setLineageMode
  } = workspace;
  return (
    <div className="asset-lineage asset-lineage-graph">
      <div className="asset-lineage-toolbar">
        <div>
          <strong>执行资产血缘图</strong>
          <span>来源同步 → 音频处理 → ASR/单据关联 → 标签证据 → 评测与回填</span>
        </div>
        <div className="asset-lineage-legend" aria-label="血缘图图例">
          <span><i className="source" /> 外部数据源</span>
          <span><i className="asset" /> Asset</span>
          <span><i className="risk" /> Check/Backfill</span>
          <span><i className="human" /> Human Loop</span>
        </div>
      </div>
      <div className="asset-lineage-modebar" aria-label="血缘治理模式">
        {lineageModeOptions.map((mode) => (
          <button key={mode.key} type="button" className={lineageMode === mode.key ? "active" : ""} onClick={() => setLineageMode(mode.key)}>
            <strong>{mode.label}</strong>
            <span>{mode.meta}</span>
          </button>
        ))}
      </div>
      <AssetLineageCanvas selectedAssetKey={selectedAssetKey} workspace={workspace} />
      <div className="asset-lineage-inspector">
        <div>
          <span>当前节点</span>
          <strong>{activeNode.label}</strong>
          <p>{activeNode.summary}</p>
        </div>
        <div>
          <span>执行映射</span>
          <code>{activeNode.dagster}</code>
          <em>{activeNode.assetKey ?? activeNode.meta}</em>
        </div>
        <div>
          <span>联动范围</span>
          <strong>{relatedNodeIds.size} 个节点</strong>
          <p>点击资产节点会同步下方资产详情、回填建议和运行记录。</p>
        </div>
      </div>
      <div className="asset-lineage-workbench">
        <section>
          <div className="asset-lineage-workbench-head">
            <span>{lineageModeOptions.find((mode) => mode.key === lineageMode)?.label}</span>
            <strong>{activeNode.label}</strong>
          </div>
          <AssetLineageModePanel workspace={workspace} />
        </section>
        <aside>
          <span>建议动作</span>
          <div className="asset-lineage-action-list">
            {actionButtons.map((action) => (
              <button key={action.label} type="button" onClick={action.onClick}>
                <strong>{action.label}</strong>
                <em>{action.detail}</em>
              </button>
            ))}
          </div>
          <p>{lineageActionNote}</p>
        </aside>
      </div>
    </div>
  );
}
