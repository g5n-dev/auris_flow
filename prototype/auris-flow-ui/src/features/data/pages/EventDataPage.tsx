import {
  AlertTriangle,
  BookOpen,
  Check,
  Database,
  GitBranch,
  Headphones,
  Plus
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  createEventLink,
  createHumanReviewTask,
  getEventLink,
  getHumanReviewTask
} from "../../../api/client";
import type { DataAssetItem } from "../../../shared/contracts/dataAssets";
import type { OperationStatus } from "../../../shared/contracts/operations";
import { eventLinks } from "../../../shared/fixtures/eventLinks";

export function EventDataPage({
  dataAssets,
  selectedAssetId,
  setSelectedAssetId,
  openListeningFromDataAsset,
  openAssetsFromDataAsset,
  openConnectorImport,
  isConnectorImporting
}: {
  dataAssets: DataAssetItem[];
  selectedAssetId: string;
  setSelectedAssetId: (id: string) => void;
  openListeningFromDataAsset: (asset: DataAssetItem) => void;
  openAssetsFromDataAsset: (asset: DataAssetItem) => void;
  openConnectorImport: () => void;
  isConnectorImporting: boolean;
}) {
  const [selectedEventId, setSelectedEventId] = useState(eventLinks[1]?.id ?? eventLinks[0]?.id ?? "");
  const [eventStatusFilter, setEventStatusFilter] = useState<"all" | "risk" | "pending" | "confirmed">("all");
  const [eventFeedback, setEventFeedback] = useState("事件数据来自认证事件接口、ASR 标签和单据变更，点击事件可定位证据或生成回填草稿。");
  const [eventAction, setEventAction] = useState<"backfill" | "human" | null>(null);
  const [eventOperationStatus, setEventOperationStatus] = useState<OperationStatus>("idle");
  const eventRows = useMemo(
    () =>
      eventLinks.map((link, index) => {
        const linkedAsset =
          (link.state.includes("串音") ? dataAssets.find((asset) => asset.id === "AF-129") : undefined) ??
          dataAssets.find((asset) => asset.docs.includes(link.doc)) ??
          dataAssets[index % dataAssets.length] ??
          dataAssets[0];
        const status = link.state.includes("冲突") || link.state.includes("排除")
          ? "risk"
          : link.state.includes("待")
            ? "pending"
            : "confirmed";
        const source = link.type.includes("报价") || link.type.includes("试驾") ? "认证事件接口" : "ASR 标签事件";
        return {
          ...link,
          linkedAsset,
          status,
          source,
          writeTarget: status === "confirmed" ? "event_tags / 已入库" : status === "pending" ? "review_queue / 待人工" : "repair_queue / 阻断写入",
          joinKeys: [
            `event_id=${link.id}`,
            `recording=${linkedAsset.audio}`,
            `partition=${linkedAsset.partitionKey}`,
            `doc=${link.doc || "none"}`
          ]
        };
      }),
    [dataAssets]
  );
  const filteredEventRows = useMemo(
    () => eventStatusFilter === "all" ? eventRows : eventRows.filter((event) => event.status === eventStatusFilter),
    [eventRows, eventStatusFilter]
  );
  const selectedEvent = filteredEventRows.find((event) => event.id === selectedEventId) ?? filteredEventRows[0] ?? eventRows[0];
  const selectedAsset = selectedEvent?.linkedAsset ?? dataAssets.find((asset) => asset.id === selectedAssetId) ?? dataAssets[0];
  const stats = [
    ["认证事件", String(eventRows.length), "报价 / 试驾 / 异议"],
    ["字段冲突", String(eventRows.filter((event) => event.status === "risk").length), "阻断自动写入"],
    ["待回填", String(eventRows.filter((event) => event.status === "pending").length), "需人工确认"],
    ["已关联单据", String(eventRows.filter((event) => event.doc).length), "可回溯"]
  ];
  const sourceCards = [
    ["认证事件接口", "authenticated_event_asset", "订单 / 报价 / 试驾事件", eventRows.filter((event) => event.source === "认证事件接口").length],
    ["音频标签事件", "auris/label/event_tags", "ASR 命中 / 标签候选", eventRows.filter((event) => event.source !== "认证事件接口").length],
    ["单据变更事件", "auris/events/document_links", "报价单 / 试驾单 / 客户画像", eventRows.filter((event) => event.doc).length]
  ];
  const eventFilterOptions: Array<{ key: "all" | "risk" | "pending" | "confirmed"; label: string; count: number }> = [
    { key: "all", label: "全部", count: eventRows.length },
    { key: "risk", label: "字段冲突", count: eventRows.filter((event) => event.status === "risk").length },
    { key: "pending", label: "待回填", count: eventRows.filter((event) => event.status === "pending").length },
    { key: "confirmed", label: "已确认", count: eventRows.filter((event) => event.status === "confirmed").length }
  ];
  const selectEvent = (eventId: string) => {
    const nextEvent = eventRows.find((event) => event.id === eventId) ?? eventRows[0];
    setSelectedEventId(nextEvent.id);
    setSelectedAssetId(nextEvent.linkedAsset.id);
    setEventFeedback(`已选中 ${nextEvent.id}：${nextEvent.type}，可下钻 ${nextEvent.window} 的音频证据和 ${nextEvent.doc}。`);
  };
  const applyEventFilter = (filterKey: "all" | "risk" | "pending" | "confirmed", label: string) => {
    setEventStatusFilter(filterKey);
    const nextRows = filterKey === "all" ? eventRows : eventRows.filter((event) => event.status === filterKey);
    const nextEvent = nextRows.find((event) => event.id === selectedEventId) ?? nextRows[0] ?? eventRows[0];
    if (nextEvent) {
      setSelectedEventId(nextEvent.id);
      setSelectedAssetId(nextEvent.linkedAsset.id);
    }
    setEventFeedback(`已筛选「${label}」事件 ${nextRows.length} 条；事件列表、详情和关联键已同步。`);
  };
  const markEventAction = async (action: "backfill" | "human") => {
    if (eventAction || !selectedEvent) return;
    setEventAction(action);
    setEventOperationStatus("pending");
    setEventFeedback(action === "backfill"
      ? `${selectedEvent.id} 正在创建事件关联并读回回填状态。`
      : `${selectedEvent.id} 正在创建 HumanReviewTask 并读回队列状态。`);
    try {
      if (action === "backfill") {
        const receipt = await createEventLink({
          id: `event-link-${selectedEvent.id}-${Date.now().toString(36)}`,
          audio_session_id: selectedAsset.id,
          source_event_id: selectedEvent.id,
          event_ref: selectedEvent.type,
          target_doc_id: selectedEvent.doc || undefined,
          document_ref: selectedEvent.doc || undefined,
          relation_type: "audio_event_backfill",
          join_keys: selectedEvent.joinKeys,
          confidence: selectedEvent.confidence,
          status: "pending",
          relation_state: selectedEvent.state,
          evidence_window: selectedEvent.window
        });
        const readback = await getEventLink(receipt.data.id);
        setEventOperationStatus("success");
        setEventFeedback(`${receipt.data.id} 已写入并读回，状态 ${String(readback.data.status ?? receipt.data.status)}；trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`);
      } else {
        const taskId = `hrt-event-${selectedEvent.id}-${Date.now().toString(36)}`;
        const receipt = await createHumanReviewTask({
          id: taskId,
          review_task_id: taskId,
          queue: "event_backfill_review",
          status: "pending",
          target_type: "event_link",
          target_id: selectedEvent.id,
          title: `${selectedEvent.type} / ${selectedEvent.state}`,
          assignee: "质检运营",
          evidence_refs: [selectedAsset.assetKey, selectedAsset.id, selectedEvent.window],
          source: "data_event_ui"
        });
        const readback = await getHumanReviewTask(receipt.data.id);
        setEventOperationStatus("success");
        setEventFeedback(`${receipt.data.id} 已进入 ${String(readback.data.queue ?? "event_backfill_review")}，状态 ${String(readback.data.status ?? receipt.data.status)}；trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`);
      }
    } catch (error) {
      setEventOperationStatus("error");
      setEventFeedback(`${action === "backfill" ? "事件回填" : "人工转派"}失败：${error instanceof Error ? error.message : "unknown error"}。未生成本地成功状态，可重试。`);
    } finally {
      setEventAction(null);
    }
  };

  return (
    <div className="event-data-page">
      <section className="event-data-head">
        <div>
          <h2>事件数据台账</h2>
          <p>事件不是 wav 文件列表；这里管理认证事件、音频命中、单据字段和回填状态。</p>
          <div className="event-data-stats">
            {stats.map(([label, value, meta]) => (
              <span key={label}>
                <b>{value}</b>
                {label}
                <em>{meta}</em>
              </span>
            ))}
          </div>
        </div>
        <div className="event-data-actions">
          <button type="button" disabled={isConnectorImporting} onClick={openConnectorImport}>
            <Plus size={15} />
            {isConnectorImporting ? "创建中" : "导入认证事件"}
          </button>
          <button type="button" onClick={() => openAssetsFromDataAsset(selectedAsset)}>
            <GitBranch size={15} />
            查看事件资产
          </button>
        </div>
      </section>

      <div className={`operation-toast data-operation-toast is-${eventOperationStatus} event-data-toast`} role="status" aria-live="polite">
        <strong>{eventOperationStatus === "pending" ? "事件操作处理中" : eventOperationStatus === "error" ? "事件操作失败" : eventOperationStatus === "success" ? "事件操作已读回" : "事件上下文"}</strong>
        <span>{eventFeedback}</span>
      </div>

      <section className="event-data-layout">
        <aside className="event-source-panel">
          <div className="relation-section-title">
            <span>事件来源</span>
            <strong>接口 / 音频 / 单据</strong>
          </div>
          {sourceCards.map(([label, assetKey, meta, count]) => (
            <button key={label} type="button" className="event-source-card" disabled={isConnectorImporting} onClick={openConnectorImport}>
              <Database size={16} />
              <span>{label}</span>
              <strong>{assetKey}</strong>
              <em>{meta}</em>
              <b>{count}</b>
            </button>
          ))}
          <div className="event-source-contract">
            <span>入库契约</span>
            <code>tenant_id + store_id + event_id + recording_id + doc_ref</code>
            <p>认证事件保留原始 payload；人工只写入关联、回填和阻断状态。</p>
          </div>
        </aside>

        <section className="event-ledger-panel">
          <div className="event-ledger-toolbar">
            <div>
              <span>事件列表</span>
              <strong>{selectedEvent.type} · {selectedEvent.state}</strong>
            </div>
            <div>
              {eventFilterOptions.map((chip) => (
                <button
                  key={chip.key}
                  type="button"
                  className={eventStatusFilter === chip.key ? "active" : ""}
                  onClick={() => applyEventFilter(chip.key, chip.label)}
                >
                  {chip.label}
                  <b>{chip.count}</b>
                </button>
              ))}
            </div>
          </div>
          <div className="event-ledger-list">
            {filteredEventRows.map((event) => (
              <button
                key={event.id}
                type="button"
                className={selectedEvent.id === event.id ? `event-ledger-row active ${event.status}` : `event-ledger-row ${event.status}`}
                onClick={() => selectEvent(event.id)}
              >
                <span>{event.id}</span>
                <strong>{event.type}</strong>
                <em>{event.window} · {event.source}</em>
                <p>{event.asr}</p>
                <div>
                  <b>{event.state}</b>
                  <i>{Math.round(event.confidence * 100)}%</i>
                </div>
              </button>
            ))}
          </div>
        </section>

        <aside className="event-detail-panel">
          <div className={`event-detail-status ${selectedEvent.status}`}>
            <span>当前事件</span>
            <strong>{selectedEvent.id}</strong>
            <b>{Math.round(selectedEvent.confidence * 100)}%</b>
          </div>
          <div className="event-detail-block">
            <span>音频证据</span>
            <strong>{selectedEvent.window}</strong>
            <p>{selectedAsset.audio} · {selectedEvent.audio}</p>
          </div>
          <div className="event-detail-block">
            <span>单据与字段</span>
            <strong>{selectedEvent.doc || "未关联单据"}</strong>
            <p>{selectedEvent.field}: {selectedEvent.fieldValue}</p>
          </div>
          <div className="event-detail-block">
            <span>业务事件时间</span>
            <strong>{selectedEvent.docEvent}</strong>
            <p>{selectedEvent.action} · {selectedEvent.writeTarget}</p>
          </div>
          <div className="event-join-keys">
            <span>关联键</span>
            {selectedEvent.joinKeys.map((key) => (
              <code key={key}>{key}</code>
            ))}
          </div>
          <div className="event-detail-actions">
            <button type="button" onClick={() => openListeningFromDataAsset(selectedAsset)}>
              <Headphones size={14} />
              定位证据
            </button>
            <button type="button" onClick={() => openAssetsFromDataAsset(selectedAsset)}>
              <BookOpen size={14} />
              资产血缘
            </button>
            <button type="button" data-testid="event-backfill-submit" disabled={eventAction !== null} onClick={() => void markEventAction("backfill")}>
              <Check size={14} />
              {eventAction === "backfill" ? "写入中" : eventOperationStatus === "error" ? "重试回填" : "生成回填"}
            </button>
            <button type="button" data-testid="event-human-review-submit" disabled={eventAction !== null} onClick={() => void markEventAction("human")}>
              <AlertTriangle size={14} />
              {eventAction === "human" ? "转派中" : eventOperationStatus === "error" ? "重试转派" : "转人工复核"}
            </button>
          </div>
        </aside>
      </section>
    </div>
  );
}
