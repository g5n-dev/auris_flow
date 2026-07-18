import { BookOpen, GitBranch, Headphones } from "lucide-react";

import type { DataWorkspace } from "../useDataWorkspace";

export function DataRelationView({ workspace }: { workspace: DataWorkspace }) {
  const {
    openAssetsFromDataAsset,
    openListeningFromDataAsset,
    relationEdgeRows,
    relationNodeItems,
    relationRepairRows,
    relationStatusLabel,
    selectedAsset,
    selectedAssetCatalog,
    setActiveModule,
    setSelectedAssetId,
    visibleDataAssets
  } = workspace;
  return (
        <section className="relation-network-panel">
          <div className="relation-network-head">
            <div>
              <span>关系视图</span>
              <strong>实体 / 事件 / 音频 / 单据 / 资产链路</strong>
              <p>这里看的是跨对象关系，不再按文件夹聚合；点击左侧对象会切换中间关系图和右侧修复上下文。</p>
            </div>
            <div className="relation-network-metrics">
              <span>
                <b>{visibleDataAssets.length}</b>
                可追踪对象
              </span>
              <span>
                <b>{relationRepairRows.length}</b>
                待修复链路
              </span>
              <span>
                <b>{selectedAsset.downstreamAssets.length}</b>
                下游资产
              </span>
            </div>
          </div>

          <div className="relation-network-layout">
            <aside className="relation-object-list">
              <div className="relation-section-title">
                <span>对象索引</span>
                <strong>按风险和断链排序</strong>
              </div>
              {visibleDataAssets.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={selectedAsset.id === item.id ? `relation-object-card active ${item.status}` : `relation-object-card ${item.status}`}
                  onClick={() => setSelectedAssetId(item.id)}
                >
                  <b>{item.id}</b>
                  <strong>{item.event}</strong>
                  <span>{item.person}</span>
                  <em>{item.audio}</em>
                  <i>{Math.round(item.confidence * 100)}%</i>
                </button>
              ))}
            </aside>

            <section className="relation-map-card">
              <div className="relation-section-title">
                <span>当前关系图</span>
                <strong>{selectedAsset.event} · {relationStatusLabel}</strong>
              </div>
              <div className="relation-map-canvas" aria-label={`${selectedAsset.id} 实体关系图`}>
                <svg viewBox="0 0 760 330" preserveAspectRatio="none" aria-hidden="true">
                  <path className="relation-line ok" d="M126 72 C220 72 220 164 333 164" />
                  <path className="relation-line ok" d="M126 258 C225 258 225 182 333 182" />
                  <path className={`relation-line ${selectedAsset.docs.length ? "ok" : "warn"}`} d="M382 164 C480 148 488 76 618 76" />
                  <path className={`relation-line ${selectedAsset.status === "risk" ? "risk" : "ok"}`} d="M382 186 C482 210 492 258 618 258" />
                  <path className="relation-line downstream" d="M663 258 C704 234 710 110 663 76" />
                </svg>
                {relationNodeItems.map((node) => {
                  const Icon = node.icon;
                  return (
                    <button
                      key={node.key}
                      type="button"
                      className={`relation-node relation-node-${node.key} ${node.tone}`}
                      onClick={node.action}
                      disabled={!node.action}
                    >
                      <Icon size={16} />
                      <span>{node.label}</span>
                      <strong>{node.value}</strong>
                      <em>{node.meta}</em>
                    </button>
                  );
                })}
              </div>
              <div className="relation-edge-list">
                {relationEdgeRows.map(([from, to, detail, state]) => (
                  <button key={`${from}-${to}`} type="button" className={`relation-edge-row ${state}`}>
                    <span>{from}</span>
                    <b>→</b>
                    <span>{to}</span>
                    <em>{detail}</em>
                  </button>
                ))}
              </div>
            </section>

            <aside className="relation-inspector">
              <div className={`relation-inspector-status ${selectedAsset.status}`}>
                <span>当前链路</span>
                <strong>{selectedAsset.id}</strong>
                <b>{relationStatusLabel}</b>
              </div>
              <div className="relation-inspector-block">
                <span>对象上下文</span>
                <strong>{selectedAsset.space}</strong>
                <p>{selectedAsset.time} · {selectedAsset.person}</p>
              </div>
              <div className="relation-inspector-block">
                <span>质量检查</span>
                <strong>{selectedAsset.assetCheck}</strong>
                <p>{selectedAssetCatalog.name} · 质量 {selectedAssetCatalog.quality}</p>
              </div>
              <div className="relation-inspector-block">
                <span>证据与事件数据</span>
                <div className="relation-doc-list">
                  {selectedAsset.docs.map((doc) => (
                    <button key={doc} type="button">{doc}</button>
                  ))}
                </div>
              </div>
              <div className="relation-repair-queue">
                <div className="relation-section-title">
                  <span>修复队列</span>
                  <strong>{relationRepairRows.length} 条</strong>
                </div>
                {relationRepairRows.map((item) => (
                  <button key={item.id} type="button" className={item.status} onClick={() => setSelectedAssetId(item.id)}>
                    <b>{item.id}</b>
                    <span>{item.assetCheck}</span>
                    <em>{item.event} · {Math.round(item.confidence * 100)}%</em>
                  </button>
                ))}
              </div>
              <div className="relation-actions">
                <button type="button" onClick={() => openListeningFromDataAsset(selectedAsset)}>
                  <Headphones size={14} />
                  进入调听
                </button>
                <button type="button" onClick={() => openAssetsFromDataAsset(selectedAsset)}>
                  <BookOpen size={14} />
                  资产血缘
                </button>
                <button type="button" onClick={() => setActiveModule("canvas")}>
                  <GitBranch size={14} />
                  修复链路
                </button>
              </div>
            </aside>
          </div>
        </section>
      );
}
