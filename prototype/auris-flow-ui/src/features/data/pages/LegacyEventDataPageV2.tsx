import {
  ArrowRight,
  BookOpen,
  Database,
  Download,
  FileText,
  Headphones,
  Plus,
  ShieldCheck,
  Tags,
  UserCheck
} from "lucide-react";
import { useState, type ComponentType } from "react";

import type { DataAssetItem } from "../../../shared/contracts/dataAssets";
import { formatSessionConfidence } from "../dataTruthModel";
import type { OperationNotice } from "../../../shared/contracts/operations";
import { eventLinks } from "../../../shared/fixtures/eventLinks";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";

export function LegacyEventDataPageV2({
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
  const [eventFilter, setEventFilter] = useState<"all" | DataAssetItem["status"]>("all");
  const [notice, setNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待事件操作",
    detail: "事件页聚焦业务事件、证据样本、单据回填和下游资产状态。"
  });
  const statusCopy: Record<DataAssetItem["status"], { label: string; detail: string }> = {
    confirmed: { label: "已对齐", detail: "可直接进入标签资产和报告回写" },
    pending: { label: "待确认", detail: "需要人工核对证据或单据字段" },
    risk: { label: "需修复", detail: "存在断链、低置信或串音风险" }
  };
  const visibleEvents = dataAssets.filter((item) => eventFilter === "all" || item.status === eventFilter);
  const selectedEvent = visibleEvents.find((item) => item.id === selectedAssetId) ?? visibleEvents[0] ?? dataAssets[0];
  const riskCount = dataAssets.filter((item) => item.status === "risk").length;
  const pendingCount = dataAssets.filter((item) => item.status === "pending").length;

  const markEventReviewed = () => {
    if (!selectedEvent) return;
    if (!LABEL_DEMO_MODE) {
      setNotice({
        status: "error",
        title: "生产事件复核暂不可用",
        detail: "当前旧版事件页未接入写后回读接口，已阻断本地保存。"
      });
      return;
    }
    setNotice({
      status: "success",
      title: "DEMO：事件复核预览已更新",
      detail: `${selectedEvent.id} 只更新本地演示状态，未写入 BFF。`
    });
  };

  const exportEventPack = () => {
    if (!LABEL_DEMO_MODE) {
      setNotice({
        status: "error",
        title: "生产事件导出暂不可用",
        detail: "当前旧版事件页未接入导出 Run 与制品回读，已阻断本地假导出。"
      });
      return;
    }
    setNotice({
      status: "success",
      title: "DEMO：事件资产包预览已生成",
      detail: `${visibleEvents.length} 个事件只形成本地演示预览，未创建导出 Run。`
    });
  };

  return (
    <div className="event-data-page">
      <section className="data-reference-head event-data-head">
        <div>
          <h2>事件数据资产</h2>
          <p>围绕业务事件聚合 wav、ASR、标签、单据和下游资产，处理断链、低置信和回填。</p>
          <div className="data-asset-inline-status">
            <span>事件 {dataAssets.length}</span>
            <span>待确认 {pendingCount}</span>
            <span>风险 {riskCount}</span>
          </div>
        </div>
        <div>
          <button type="button" className="data-connect-button" onClick={openConnectorImport}>
            <Plus size={15} />
            连接器导入
          </button>
          <button
            type="button"
            disabled={!LABEL_DEMO_MODE}
            title={!LABEL_DEMO_MODE ? "未接入导出 Run 与制品回读，生产模式禁用。" : "DEMO：生成事件资产包预览"}
            onClick={exportEventPack}
          >
            <Download size={15} />
            导出事件包
          </button>
        </div>
      </section>

      <div className={`operation-toast data-operation-toast is-${notice.status}`} role="status" aria-live="polite">
        <strong>{notice.title}</strong>
        <span>{notice.detail}</span>
      </div>

      <section className="event-data-toolbar">
        {[
          ["all", "全部事件", dataAssets.length],
          ["pending", "待确认", pendingCount],
          ["risk", "需修复", riskCount],
          ["confirmed", "已对齐", dataAssets.filter((item) => item.status === "confirmed").length]
        ].map(([key, label, count]) => (
          <button key={key} type="button" className={eventFilter === key ? "active" : ""} onClick={() => setEventFilter(key as "all" | DataAssetItem["status"])}>
            <strong>{label}</strong>
            <span>{count}</span>
          </button>
        ))}
      </section>

      {visibleEvents.length === 0 ? (
        <section className="event-data-empty">
          <Tags size={24} />
          <strong>当前筛选下没有事件资产</strong>
          <span>可以通过连接器导入业务单据、认证事件或音频 URL，生成新的事件链路。</span>
          <button type="button" onClick={openConnectorImport}>连接器导入</button>
        </section>
      ) : (
        <section className="event-data-layout">
          <aside className="event-data-list">
            {visibleEvents.map((item) => (
              <button
                key={item.id}
                type="button"
                className={selectedEvent?.id === item.id ? `active ${item.status}` : item.status}
                onClick={() => {
                  setSelectedAssetId(item.id);
                  setNotice({
                    status: "success",
                    title: "事件上下文已切换",
                    detail: `${item.event} 已加载，右侧展示证据、单据和下游资产。`
                  });
                }}
              >
                <span>{item.id}</span>
                <strong>{item.event}</strong>
                <em>{item.person}</em>
                <b>{formatSessionConfidence(item.confidence)}</b>
              </button>
            ))}
          </aside>

          <main className="event-data-detail">
            <div className={`event-status-card ${selectedEvent.status}`}>
              <span>{statusCopy[selectedEvent.status].label}</span>
              <strong>{selectedEvent.event}</strong>
              <p>{statusCopy[selectedEvent.status].detail}</p>
            </div>
            <div className="event-evidence-grid">
              {[
                ["音频证据", selectedEvent.audio, `${selectedEvent.duration} / ${selectedEvent.partitionKey}`, Headphones],
                ["人物主体", selectedEvent.person, "员工 / 客户组 / 声纹", UserCheck],
                ["业务单据", selectedEvent.docs.join("、") || "未关联单据", `${selectedEvent.docs.length} 个凭证`, FileText],
                ["资产检查", selectedEvent.assetCheck, selectedEvent.assetKey, Database]
              ].map(([label, value, detail, Icon]) => {
                const EvidenceIcon = Icon as ComponentType<{ size?: number }>;
                return (
                  <button key={label as string} type="button" onClick={() => label === "音频证据" ? openListeningFromDataAsset(selectedEvent) : undefined}>
                    <EvidenceIcon size={16} />
                    <span>{label as string}</span>
                    <strong>{value as string}</strong>
                    <em>{detail as string}</em>
                  </button>
                );
              })}
            </div>
            <div className="event-lineage-row">
              <div>
                <span>上游资产</span>
                {selectedEvent.upstreamAssets.map((asset) => <b key={asset}>{asset}</b>)}
              </div>
              <ArrowRight size={18} />
              <div>
                <span>下游资产</span>
                {selectedEvent.downstreamAssets.map((asset) => <b key={asset}>{asset}</b>)}
              </div>
            </div>
            <div className="event-action-row">
              <button type="button" onClick={() => openListeningFromDataAsset(selectedEvent)}>
                <Headphones size={14} />
                进入调听
              </button>
              <button type="button" onClick={() => openAssetsFromDataAsset(selectedEvent)}>
                <BookOpen size={14} />
                查看血缘
              </button>
              <button
                type="button"
                disabled={!LABEL_DEMO_MODE}
                title={!LABEL_DEMO_MODE ? "未接入复核写后回读，生产模式禁用。" : "DEMO：更新本地复核预览"}
                onClick={markEventReviewed}
              >
                <ShieldCheck size={14} />
                保存复核记录
              </button>
            </div>
          </main>
        </section>
      )}
    </div>
  );
}
