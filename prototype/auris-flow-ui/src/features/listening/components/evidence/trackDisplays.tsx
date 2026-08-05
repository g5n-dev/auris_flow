import { eventLinks } from "../../../../shared/fixtures/eventLinks";
import { actionFeedbackAttrs } from "../../../../shared/runtime/feedbackAttributes";
import { LABEL_DEMO_MODE } from "../../../../shared/runtime/demoMode";
import { asrRows, listeningDeviceBadges } from "../../fixtures/evidenceFixtures";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import type { TrackAnnotation } from "../../model/trackLayout";
import { Search, Sparkles, X } from "lucide-react";
import { useState } from "react";

export function TrackLabel({
  tracks,
  toggleTrack,
  rowTemplate
}: {
  tracks: Array<{ key: string; label: string; color: string }>;
  toggleTrack: (track: string) => void;
  rowTemplate: string;
}) {
  return (
    <div className="track-labels" style={{ gridTemplateRows: rowTemplate }}>
      <span>波形</span>
      {tracks.map((track) => (
        <button key={track.key} className={`track-label-toggle ${track.color}`} onClick={() => toggleTrack(track.key)}>
          {track.label}
        </button>
      ))}
    </div>
  );
}

export function DataTrack({ tone, chunks }: { tone: string; chunks: number[] }) {
  return (
    <div className={`data-track ${tone}`}>
      {chunks.map((width, i) => (
        <span key={i} style={{ width: `${width}%` }} />
      ))}
    </div>
  );
}

export function TagTrack({
  tags,
  trackKey,
  color = "teal",
  hiddenTags,
  annotations,
  onHide
}: {
  tags: string[];
  trackKey: string;
  color?: string;
  hiddenTags: string[];
  annotations: TrackAnnotation[];
  onHide: (track: string, tag: string) => void;
}) {
  const visibleTags = tags.filter((tag) => !hiddenTags.includes(tag));
  const visibleAnnotations = annotations.filter((annotation) => !hiddenTags.includes(annotation.label));
  return (
    <div className={`tag-track ${color}`}>
      {visibleTags.map((tag, i) => (
        <button key={`${tag}-${i}`} onClick={() => onHide(trackKey, tag)}>
          <span>{tag}</span>
          <X size={10} />
        </button>
      ))}
      {visibleAnnotations.map((annotation) => (
        <button
          key={annotation.id}
          className="manual segment-annotation"
          style={{ left: `${annotation.left}%`, width: `${annotation.width}%` }}
          onClick={() => onHide(trackKey, annotation.label)}
        >
          <span>{annotation.label}</span>
          <X size={10} />
        </button>
      ))}
    </div>
  );
}

export function EventTrack({
  events,
  annotations = [],
  hiddenTags = [],
  onHide
}: {
  events: typeof eventLinks;
  annotations?: TrackAnnotation[];
  hiddenTags?: string[];
  onHide?: (track: string, tag: string) => void;
}) {
  return (
    <div className="event-track">
      {events.filter((event) => !hiddenTags.includes(`${event.type}:${event.state}`)).map((event) => (
        <button
          key={event.id}
          className={`event-node ${event.tone}`}
          style={{ left: `${event.left}%`, width: `${event.width}%` }}
          title={`${event.id} / ${event.doc} / ${event.state}`}
          onClick={() => onHide?.("doc", `${event.type}:${event.state}`)}
        >
          <span>{event.type}</span>
          <b>{event.state}</b>
        </button>
      ))}
      {annotations.filter((annotation) => !hiddenTags.includes(annotation.label)).map((annotation) => (
        <button
          key={annotation.id}
          className="event-node manual"
          style={{ left: `${annotation.left}%`, width: `${annotation.width}%` }}
          onClick={() => onHide?.("doc", annotation.label)}
        >
          <span>{annotation.label}</span>
          <b>手动</b>
        </button>
      ))}
    </div>
  );
}

export function TranscriptTable() {
  return (
    <section className="transcript">
      <div className="tabs">
        <button className="active">ASR 转写</button>
        <button>事件列表</button>
        <button>标签列表</button>
        <button>关键词与标注</button>
      </div>
      <div className="table">
        <div className="table-head">
          <span>时间</span>
          <span>说话人</span>
          <span>转写内容</span>
          <span>意图/事件</span>
          <span>关键词/实体</span>
          <span>关联单据</span>
          <span>状态</span>
          <span>置信度</span>
        </div>
        {asrRows.map((row) => (
          <button key={row.time} className={row.active ? "table-row active" : "table-row"}>
            <span>{row.time}</span>
            <span className={`speaker ${row.speaker === "客户" ? "customer" : ""}`}>{row.speaker}</span>
            <span className="transcript-text">{row.text}</span>
            <span className="event-ref">
              <b>{row.eventType}</b>
              <small>{row.event}</small>
            </span>
            <span className="chips">
              {row.tags.map((tag) => (
                <b key={tag}>{tag}</b>
              ))}
            </span>
            <span>{row.doc || "未关联"}</span>
            <span className={row.linkState.includes("冲突") || row.linkState.includes("串音") ? "link-state warn" : "link-state"}>
              {row.linkState}
            </span>
            <span className={row.confidence < 0.8 ? "conf warn" : "conf"}>{row.confidence.toFixed(2)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

export function AnnotationSplitView({
  sample,
  activeDevice
}: {
  sample: ReviewSample;
  activeDevice: (typeof listeningDeviceBadges)[number];
}) {
  const [speakerFilter, setSpeakerFilter] = useState("all");
  const [diffMode, setDiffMode] = useState("compare");
  const sampleRows = asrRows.map((row) =>
    row.active
      ? {
          ...row,
          time: sample.activeTime,
          speaker: sample.speaker,
          text: sample.queueDetail,
          tags: [sample.queue, sample.selectedLabel],
          intent: sample.title,
          event: sample.dataAssetId,
          eventType: sample.queueTitle,
          linkState: sample.conclusion,
          confidence: sample.confidence / 100,
          doc: sample.docs[0]?.id ?? ""
        }
      : row
  );
  const filteredRows = sampleRows.filter((row) => speakerFilter === "all" || row.speaker === speakerFilter);
  const matchesActiveDevice = (row: (typeof sampleRows)[number]) => {
    const text = `${row.speaker} ${row.text} ${row.tags.join(" ")} ${row.intent} ${row.eventType} ${row.linkState} ${row.doc}`.toLowerCase();
    return activeDevice.rowTerms.some((term) => text.includes(term.toLowerCase()));
  };
  const focusedRows = filteredRows.filter(matchesActiveDevice);
  const visibleRows = focusedRows.length > 0 ? focusedRows : filteredRows;

  return (
    <div className="sp">
      <section className="pn">
        <div className="pnh">
          <span className="l">对话视图 · {sample.title}</span>
          <div className="r">
            <div className="se">
              {[
                ["all", "全部"],
                ["销售A", "销售"],
                ["客户", "顾客"]
              ].map(([key, label]) => (
                <button key={key} className={speakerFilter === key ? "on" : ""} onClick={() => setSpeakerFilter(key)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className="sent-search-bar">
          <Search size={13} />
          <input placeholder="搜索 sentence / 单据事件 / 标签" />
          <button>导出 Transcript</button>
          <span>{visibleRows.length} 句</span>
        </div>
        <div className={`device-focus-strip ${activeDevice.mark === "crosstalk" ? "warn" : ""}`}>
          <b>{activeDevice.name}</b>
          <span>{activeDevice.summary}</span>
          <em>{focusedRows.length > 0 ? `命中 ${focusedRows.length} 句` : "无强命中，保留当前过滤结果"}</em>
        </div>
        <div className="pnb bubbles">
          {visibleRows.map((row) => (
            <button key={row.time} className={`bb ${row.active ? "ac" : ""} ${matchesActiveDevice(row) ? "device-hit" : ""} ${row.speaker === "客户" ? "s0" : "s1"}`}>
              <span className="bv">{row.speaker === "客户" ? "客" : "销"}</span>
              <span className="bc">
                <span className="bm">
                  <b className="bn">{row.speaker}</b>
                  <span>{row.time}</span>
                  <span>{row.intent}</span>
                </span>
                <span className="bd">{row.text}</span>
                <span className="bt">
                  {row.tags.map((tag) => (
                    <b key={tag}>{tag}</b>
                  ))}
                  <b>{row.eventType}</b>
                  <b className={row.linkState.includes("冲突") ? "warn" : ""}>{row.linkState}</b>
                </span>
              </span>
            </button>
          ))}
        </div>
      </section>
      <section className="pn">
        <div className="pnh">
          <span className="l">转写对照 · ASR Diff</span>
          <div className="r">
            <div className="se">
              <button className={diffMode === "single" ? "on" : ""} onClick={() => setDiffMode("single")}>
                单版本
              </button>
              <button className={diffMode === "compare" ? "on" : ""} onClick={() => setDiffMode("compare")}>
                对比
              </button>
            </div>
          </div>
        </div>
        <div className="asr-ver-bar">
          <span>ASR v2.3.1</span>
          <span>领域词典 汽车报价 v1.8</span>
          <span>差异 4</span>
        </div>
        <div className="pnb diff-list">
          {sample.mismatches.map((item, index) => (
            <button key={item.field} className={item.state === "一致" ? "df" : "df ac"}>
              <span className="h">
                <span className="tc">{index === 0 ? "12:26:58" : "12:27:18"}</span>
                <span>{item.field}</span>
                <span className={item.state === "一致" ? "cf" : "cf lo"}>{item.state}</span>
              </span>
              <span className="tx">ASR：{item.audio}</span>
              <span className="or">单据：{item.doc}</span>
            </button>
          ))}
          {eventLinks.map((event) => (
            <button key={event.id} className="df">
              <span className="h">
                <span className="tc">{event.window}</span>
                <span>{event.type}</span>
                <span className={event.state.includes("冲突") ? "cf lo" : "cf"}>{event.state}</span>
              </span>
              <span className="tx">{event.asr}</span>
              <span className="or">{event.docEvent} · {event.field}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

export function AnnotationSpine({
  lowConfidence,
  setLowConfidence,
  sample,
  completedCount,
  onConfirmNext,
  confirmPending
}: {
  lowConfidence: boolean;
  setLowConfidence: (value: boolean) => void;
  sample: ReviewSample;
  completedCount: number;
  onConfirmNext: () => void | Promise<void>;
  confirmPending: boolean;
}) {
  const canSubmitReview = Boolean(sample.reviewTaskId) || LABEL_DEMO_MODE;
  const submitDisabledReason = "当前 AudioSession 尚未绑定 HumanReviewTask，不能提交人工决定。";
  const progress = [
    "rv",
    "rv",
    "rv",
    "pn",
    lowConfidence ? "lo" : "td",
    "td",
    "td",
    "td"
  ];

  return (
    <div className="sn">
      <div className="sn-prog" role="status">
        <span className="lb">进度</span>
        <div className="bar">
          {progress.map((item, index) => (
            <i key={index} className={item} />
          ))}
        </div>
        <span className="nu">{sample.progressIndex + completedCount} / {sample.progressTotal}</span>
      </div>
      <div className="sn-act">
        <button
          className={lowConfidence ? "selected" : ""}
          disabled={!canSubmitReview}
          title={!canSubmitReview ? submitDisabledReason : "将低置信标记加入当前人工决定。"}
          onClick={() => setLowConfidence(!lowConfidence)}
        >
          {lowConfidence ? "已标记低置信" : "标记低置信"}
        </button>
        <button
          className="pr"
          disabled={confirmPending || !canSubmitReview}
          title={
            !canSubmitReview
              ? submitDisabledReason
              : confirmPending
                ? "复核决定正在提交，完成后自动进入下一通。"
                : "提交当前复核决定并进入下一通对话。"
          }
          {...actionFeedbackAttrs("p,s,e,d")}
          onClick={() => void onConfirmNext()}
        >
          {confirmPending
            ? "提交并回读中..."
            : canSubmitReview
              ? "提交决定并进入下一通"
              : "等待生成待审任务"}
        </button>
      </div>
    </div>
  );
}

export function EvidenceList({ sample, activeDevice }: { sample?: ReviewSample; activeDevice?: (typeof listeningDeviceBadges)[number] }) {
  const items = activeDevice?.evidenceItems ?? sample?.evidenceItems ?? [
    ["12:27:18", "ASR", "可以优惠 3.5 万，落地大概 28.19 万左右"],
    ["报价单", "字段", "报价金额 31.69 万"],
    ["串音", "设备", "销售B工牌 12:27:18 - 12:28:30"]
  ];
  return (
    <div className="evidence-list">
      {items.map(([time, type, text]) => (
        <button key={`${time}-${type}`}>
          <Sparkles size={14} />
          <span>{time}</span>
          <b>{type}</b>
          <em>{text}</em>
        </button>
      ))}
    </div>
  );
}
