import {
  ArrowRight,
  BookOpen,
  FileText,
  GitBranch,
  Headphones,
  Link2,
  Plus
} from "lucide-react";

import type { DataAssetItem } from "../../../shared/contracts/dataAssets";
import { eventLinks } from "../../../shared/fixtures/eventLinks";
import { PanelHeader } from "../../../shared/ui/PanelHeader";

export function EventDataAssetPageDeprecated({
  dataAssets,
  selectedAssetId,
  setSelectedAssetId,
  openListeningFromDataAsset,
  openAssetsFromDataAsset,
  openConnectorImport
}: {
  dataAssets: DataAssetItem[];
  selectedAssetId: string;
  setSelectedAssetId: (id: string) => void;
  openListeningFromDataAsset: (asset: DataAssetItem) => void;
  openAssetsFromDataAsset: (asset: DataAssetItem) => void;
  openConnectorImport: () => void;
}) {
  const eventAssets = dataAssets.filter((asset) => asset.event || asset.docs.length > 0);
  const selectedAsset = eventAssets.find((asset) => asset.id === selectedAssetId) ?? eventAssets[0] ?? dataAssets[0];
  const riskCount = eventAssets.filter((asset) => asset.status === "risk").length;
  const pendingCount = eventAssets.filter((asset) => asset.status === "pending").length;
  const confirmedCount = eventAssets.filter((asset) => asset.status === "confirmed").length;
  const statusLabel = selectedAsset.status === "confirmed" ? "已确认" : selectedAsset.status === "pending" ? "待回填" : "有风险";
  const statusTone = selectedAsset.status === "confirmed" ? "ok" : selectedAsset.status === "pending" ? "warn" : "risk";
  return (
    <div className="data-reference-page event-data-page">
      <section className="data-reference-head event-data-head">
        <div>
          <h2>事件与单据资产</h2>
          <p>把认证事件、业务单据、音频片段和标签结果对齐成可追溯事件链，支持回到调听或资产血缘。</p>
          <div className="data-ingest-hint">
            <Link2 size={13} />
            <span>事件主键、时间窗、人员主体和单据字段共同决定是否可自动关联。</span>
          </div>
        </div>
        <div>
          <button type="button" className="data-connect-button" onClick={openConnectorImport}>
            <Plus size={15} />
            连接器导入
          </button>
          <button type="button" className="data-contract-button" onClick={() => openListeningFromDataAsset(selectedAsset)}>
            <Headphones size={15} />
            进入调听
          </button>
          <button type="button" onClick={() => openAssetsFromDataAsset(selectedAsset)}>
            <GitBranch size={15} />
            资产血缘
          </button>
        </div>
      </section>

      <section className="event-data-kpis">
        {[
          ["事件资产", `${eventAssets.length}`, "音频+单据"],
          ["已确认", `${confirmedCount}`, "可下游消费"],
          ["待回填", `${pendingCount}`, "缺字段/缺音频"],
          ["风险事件", `${riskCount}`, "需复核"]
        ].map(([label, value, meta]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <em>{meta}</em>
          </div>
        ))}
      </section>

      <section className="event-data-layout">
        <aside className="event-data-list">
          <div className="voiceprint-panel-title">
            <span>事件队列</span>
            <strong>按当前项目证据链聚合</strong>
          </div>
          {eventAssets.map((asset) => (
            <button key={asset.id} type="button" className={asset.id === selectedAsset.id ? `active ${asset.status}` : asset.status} onClick={() => setSelectedAssetId(asset.id)}>
              <span>{asset.event}</span>
              <strong>{asset.person}</strong>
              <em>{asset.time} · {asset.space}</em>
              <b>{asset.confidence}</b>
            </button>
          ))}
        </aside>

        <main className="event-data-detail">
          <PanelHeader title={selectedAsset.event} subtitle={`${statusLabel} / ${selectedAsset.assetKey}`} icon={<FileText size={16} />} />
          <div className="event-detail-grid">
            {[
              ["空间", selectedAsset.space],
              ["时间", selectedAsset.time],
              ["人物", selectedAsset.person],
              ["音频", selectedAsset.audio],
              ["分区", selectedAsset.partitionKey],
              ["物化", selectedAsset.materializationId]
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <div className="event-doc-chain">
            <span>关联单据</span>
            {selectedAsset.docs.map((doc) => (
              <button key={doc} type="button" onClick={() => openListeningFromDataAsset(selectedAsset)}>
                <FileText size={13} />
                {doc}
              </button>
            ))}
          </div>
          <div className="event-lineage-row">
            {selectedAsset.upstreamAssets.map((asset) => (
              <span key={asset}>{asset}</span>
            ))}
            <ArrowRight size={15} />
            <strong>{selectedAsset.assetKey}</strong>
            <ArrowRight size={15} />
            {selectedAsset.downstreamAssets.map((asset) => (
              <span key={asset}>{asset}</span>
            ))}
          </div>
        </main>

        <aside className={`event-data-action ${statusTone}`}>
          <span>当前事件状态</span>
          <strong>{statusLabel}</strong>
          <p>{selectedAsset.assetCheck}</p>
          <button type="button" onClick={() => openListeningFromDataAsset(selectedAsset)}>
            <Headphones size={14} />
            回到证据片段
          </button>
          <button type="button" onClick={() => openAssetsFromDataAsset(selectedAsset)}>
            <BookOpen size={14} />
            查看资产详情
          </button>
          <button type="button" onClick={openConnectorImport}>
            <Plus size={14} />
            补充事件数据源
          </button>
        </aside>
      </section>
    </div>
  );
}
