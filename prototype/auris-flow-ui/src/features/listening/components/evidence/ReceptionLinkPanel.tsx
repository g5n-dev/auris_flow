import type { ReceptionLinkStatus, ReceptionOrderCandidate } from "../../../../shared/contracts/reception";
import { eventLinks } from "../../../../shared/fixtures/eventLinks";
import { actionFeedbackAttrs } from "../../../../shared/runtime/feedbackAttributes";
import { LABEL_DEMO_MODE } from "../../../../shared/runtime/demoMode";
import { clamp } from "../../../../shared/runtime/math";
import type { ReviewSample } from "../../fixtures/reviewSamples";
import type { HumanReviewChange } from "../../model/reviewDecisionModel";
import { getReceptionCandidatesForSample } from "../../fixtures/reviewSamples";
import { clockToSeconds, describeTimeDrift, extractClockFromText, formatSignedSeconds, parseClockWindow, secondsToClock } from "../../model/listeningTime";
import { getReceptionLocatorEvents } from "../../model/receptionEvidence";
import type { PanelTab } from "../../types";
import { FileText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

export type ReceptionLinkPanelProps = {
  sample: ReviewSample;
  setSelectedWindow: (value: string) => void;
  setPanelTab: (tab: PanelTab) => void;
  onReviewChange: (change: HumanReviewChange) => void;
  onLocatorOpenChange?: (open: boolean) => void;
};

export function ReceptionLinkPanel({
  sample,
  setSelectedWindow,
  setPanelTab,
  onReviewChange,
  onLocatorOpenChange
}: ReceptionLinkPanelProps) {
  const candidates = useMemo(() => getReceptionCandidatesForSample(sample), [sample]);
  const [activeCandidateId, setActiveCandidateId] = useState(candidates[0]?.id ?? "");
  const [linkStates, setLinkStates] = useState<Record<string, ReceptionLinkStatus>>({});
  const [evidenceLocatorOpen, setEvidenceLocatorOpen] = useState(false);
  const [selectedLocatorEventId, setSelectedLocatorEventId] = useState(eventLinks[1]?.id ?? "");
  const [manualOffsetSeconds, setManualOffsetSeconds] = useState(0);
  const [locatorStatus, setLocatorStatus] = useState("待定位");
  const [linkSubmitting, setLinkSubmitting] = useState(false);

  useEffect(() => {
    setActiveCandidateId(candidates[0]?.id ?? "");
  }, [candidates]);

  const activeCandidate = candidates.find((candidate) => candidate.id === activeCandidateId) ?? candidates[0];
  const locatorEvents = useMemo(() => getReceptionLocatorEvents(activeCandidate), [activeCandidate]);
  const selectedLocatorEvent = locatorEvents.find((event) => event.id === selectedLocatorEventId) ?? locatorEvents[0];

  useEffect(() => {
    onLocatorOpenChange?.(evidenceLocatorOpen);
    return () => onLocatorOpenChange?.(false);
  }, [evidenceLocatorOpen, onLocatorOpenChange]);

  useEffect(() => {
    const nextEvents = getReceptionLocatorEvents(activeCandidate);
    setSelectedLocatorEventId(nextEvents[0]?.id ?? "");
    setManualOffsetSeconds(0);
    setLocatorStatus("待定位");
  }, [activeCandidate?.id]);

  if (!activeCandidate) return null;

  const state = linkStates[activeCandidate.id] ?? activeCandidate.status;
  const statusClass = state === "人工确认" ? "confirmed" : state === "解除关联" ? "removed" : activeCandidate.match < 75 ? "warn" : "suggested";
  const locatorWindow = selectedLocatorEvent ? parseClockWindow(selectedLocatorEvent.window) : parseClockWindow(activeCandidate.window);
  const docEventClock = selectedLocatorEvent ? extractClockFromText(selectedLocatorEvent.docEvent) : "";
  const docEventSeconds = docEventClock ? clockToSeconds(docEventClock) : locatorWindow.startSeconds;
  const audioHitSeconds = locatorWindow.startSeconds + manualOffsetSeconds;
  const driftSeconds = audioHitSeconds - docEventSeconds;
  const adjustedWindow = selectedLocatorEvent
    ? `${secondsToClock(audioHitSeconds)} - ${secondsToClock(locatorWindow.endSeconds + manualOffsetSeconds)}`
    : activeCandidate.window;
  const locatorCandidates = selectedLocatorEvent
    ? [
        {
          id: "audio",
          title: "音频命中窗口",
          time: adjustedWindow,
          detail: selectedLocatorEvent.asr,
          score: Math.round(selectedLocatorEvent.confidence * 100)
        },
        {
          id: "neighbor",
          title: "相邻 ASR 证据",
          time: `${secondsToClock(locatorWindow.startSeconds - 6 + manualOffsetSeconds)} - ${secondsToClock(locatorWindow.endSeconds + 8 + manualOffsetSeconds)}`,
          detail: "用于判断事件是否跨片段或被截断",
          score: Math.max(54, Math.round(selectedLocatorEvent.confidence * 100) - 9)
        },
        {
          id: "doc",
          title: "业务事件时间",
          time: docEventClock || selectedLocatorEvent.docEvent,
          detail: selectedLocatorEvent.doc,
          score: Math.max(50, Math.round(selectedLocatorEvent.confidence * 100) - Math.min(28, Math.abs(Math.round(driftSeconds / 12))))
        }
      ]
    : [];

  const focusCandidate = (candidate: ReceptionOrderCandidate) => {
    setActiveCandidateId(candidate.id);
    setSelectedWindow(candidate.window);
    setPanelTab("docs");
  };

  const updateState = (nextState: ReceptionLinkStatus) => {
    setLinkStates((current) => ({
      ...current,
      [activeCandidate.id]: nextState
    }));
    setSelectedWindow(activeCandidate.window);
    setPanelTab("docs");
  };

  const locateEvidence = () => {
    setEvidenceLocatorOpen(true);
    setPanelTab("docs");
    if (selectedLocatorEvent) {
      setSelectedWindow(selectedLocatorEvent.window.replace("-", " - "));
      setLocatorStatus("已定位音频候选");
    } else {
      setSelectedWindow(activeCandidate.window);
      setLocatorStatus("待选择事件");
    }
  };

  const selectLocatorEvent = (eventId: string) => {
    const nextEvent = locatorEvents.find((event) => event.id === eventId);
    setSelectedLocatorEventId(eventId);
    setManualOffsetSeconds(0);
    setLocatorStatus("已定位音频候选");
    if (nextEvent) setSelectedWindow(nextEvent.window.replace("-", " - "));
  };

  const nudgeLocatorOffset = (deltaSeconds: number) => {
    const nextOffset = manualOffsetSeconds + deltaSeconds;
    setManualOffsetSeconds(nextOffset);
    setLocatorStatus("人工校准中");
    if (selectedLocatorEvent) {
      const currentWindow = parseClockWindow(selectedLocatorEvent.window);
      setSelectedWindow(`${secondsToClock(currentWindow.startSeconds + nextOffset).slice(0, 5)} - ${secondsToClock(currentWindow.endSeconds + nextOffset).slice(0, 5)}`);
    }
  };

  const confirmLocatedEvidence = () => {
    if (!selectedLocatorEvent || linkSubmitting) return;
    const eventLinkId =
      (activeCandidate.eventLinkId &&
      sample.eventLinkIds?.includes(activeCandidate.eventLinkId)
        ? activeCandidate.eventLinkId
        : sample.eventLinkIds?.find((id) => id === selectedLocatorEvent.id)) ??
      sample.eventLinkIds?.[0];
    if (!eventLinkId) {
      setLocatorStatus("当前任务未绑定可修订的 EventLink");
      return;
    }
    setLinkSubmitting(true);
    setLocatorStatus("加入当前人审决定");
    try {
      const targetDocId = activeCandidate.orderNo.replace(/^接待单\s*/, "");
      onReviewChange({
        target_type: "event_link",
        target_id: eventLinkId,
        fields: {
          source_event_id: selectedLocatorEvent.id,
          document_ref: targetDocId,
          relation_type: activeCandidate.asset,
          confidence: Math.min(0.99, Math.max(0.2, activeCandidate.match / 100)),
          evidence_window: adjustedWindow
        }
      });
      setLocatorStatus("已加入当前决定 · 提交后统一写入并回读");
      updateState("人工确认");
    } catch (error) {
      setLocatorStatus(error instanceof Error ? `暂存失败：${error.message}` : "暂存失败：请重试");
    } finally {
      setLinkSubmitting(false);
    }
  };

  return (
    <section className={evidenceLocatorOpen ? "reception-link-panel locator-open" : "reception-link-panel"} aria-label="销售接待单关联">
      <div className="reception-link-head">
        <FileText size={15} />
        <div>
          <span>销售接待单关联</span>
          <strong>{activeCandidate.title}</strong>
          <em>{activeCandidate.window}</em>
        </div>
        <b className={`reception-link-score ${statusClass}`}>{activeCandidate.match}%</b>
      </div>

      <div className="reception-candidate-list" aria-label="接待单候选">
        {candidates.map((candidate) => (
          <button
            key={candidate.id}
            className={candidate.id === activeCandidate.id ? "active" : ""}
            onClick={() => focusCandidate(candidate)}
          >
            <span>{candidate.orderNo}</span>
            <b>{candidate.match}%</b>
            <em>{linkStates[candidate.id] ?? candidate.status}</em>
          </button>
        ))}
      </div>

      <div className="reception-link-facts">
        <div className="reception-fact-block">
          <span>业务主键</span>
          <strong>{activeCandidate.customer} / {activeCandidate.employee}</strong>
          <em>{activeCandidate.joinKeys.slice(0, 3).join(" · ")}</em>
        </div>
        <div className="reception-fact-block">
          <span>证据单据</span>
          <strong>{activeCandidate.docs.join(" / ")}</strong>
          <em>{activeCandidate.asset}</em>
        </div>
        <div className="reception-fact-block risk">
          <span>字段差异</span>
          <strong>{activeCandidate.diffs.map((diff) => `${diff.field}:${diff.state}`).join(" / ")}</strong>
          <em>{activeCandidate.writeRef}</em>
        </div>
      </div>

      {evidenceLocatorOpen && selectedLocatorEvent && (
        <div className="reception-evidence-locator" aria-label="接待单证据定位">
          <div className="reception-locator-head">
            <div>
              <span>定位证据</span>
              <strong>{selectedLocatorEvent.type} · {selectedLocatorEvent.state}</strong>
              <em>业务事件只有时间点，音频命中是窗口；这里确认二者是否同一事实。</em>
            </div>
            <b className={Math.abs(driftSeconds) > 90 ? "risk" : "ok"}>{describeTimeDrift(driftSeconds)}</b>
          </div>
          <div className="reception-locator-events">
            {locatorEvents.map((event) => (
              <button
                key={event.id}
                type="button"
                className={selectedLocatorEvent.id === event.id ? `active ${event.tone}` : event.tone}
                onClick={() => selectLocatorEvent(event.id)}
              >
                <span>{event.window}</span>
                <strong>{event.type}</strong>
                <em>{event.docEvent}</em>
              </button>
            ))}
          </div>
          <div className="reception-locator-timeline">
            <div className="locator-axis">
              <i className="doc-time" style={{ left: "50%" }} />
              <i className="audio-time" style={{ left: `${clamp(50 + driftSeconds / 8, 4, 96)}%` }} />
              <span className="doc-time-label">事件 {docEventClock || selectedLocatorEvent.docEvent}</span>
              <span className="audio-time-label">音频 {adjustedWindow}</span>
            </div>
            <div className="locator-offset-controls">
              {[-30, -5, -1, 1, 5, 30].map((delta) => (
                <button key={delta} type="button" onClick={() => nudgeLocatorOffset(delta)}>
                  {formatSignedSeconds(delta)}
                </button>
              ))}
              <b>{locatorStatus} · 偏移 {formatSignedSeconds(manualOffsetSeconds)}</b>
            </div>
          </div>
          <div className="reception-locator-candidates">
            {locatorCandidates.map((candidate) => (
              <button key={candidate.id} type="button" onClick={() => setSelectedWindow(candidate.time.replace("-", " - "))}>
                <span>{candidate.title}</span>
                <strong>{candidate.time}</strong>
                <em>{candidate.detail}</em>
                <b>{candidate.score}%</b>
              </button>
            ))}
          </div>
          <div className="reception-locator-write">
            <span>写入</span>
            <strong>{activeCandidate.writeRef}</strong>
            <em>payload: event_id={selectedLocatorEvent.id}, audio_window={adjustedWindow}, offset={formatSignedSeconds(manualOffsetSeconds)}, reception_id={activeCandidate.orderNo}</em>
            <button
              type="button"
              onClick={confirmLocatedEvidence}
              title={!sample.eventLinkIds?.length ? "当前 HumanReviewTask 未绑定可修订的 EventLink。" : linkSubmitting ? "正在加入当前决定。" : "确认证据窗口并加入当前人审决定。"}
              disabled={!sample.eventLinkIds?.length || linkSubmitting}
              {...actionFeedbackAttrs("p,s,e,d")}
            >
              {linkSubmitting ? "暂存中..." : "确认并加入决定"}
            </button>
          </div>
        </div>
      )}

      <div className="reception-link-actions">
        <button type="button" onClick={locateEvidence} title="打开证据定位器并同步当前音频窗口。" {...actionFeedbackAttrs("s,e")}>
          定位证据
        </button>
        <button
          type="button"
          className="primary"
          onClick={confirmLocatedEvidence}
          disabled={!sample.eventLinkIds?.length || linkSubmitting}
          title={!sample.eventLinkIds?.length ? "当前 HumanReviewTask 未绑定可修订的 EventLink。" : linkSubmitting ? "正在加入当前决定。" : "确认当前接待单关联并加入当前人审决定。"}
          {...actionFeedbackAttrs("p,s,e,d")}
        >
          {linkSubmitting ? "暂存中..." : "确认关联"}
        </button>
        <button type="button" onClick={() => updateState("改绑候选")} disabled={!LABEL_DEMO_MODE} title={!LABEL_DEMO_MODE ? "生产模式未接入受控改绑请求，当前操作已禁用。" : "把当前候选标记为需要改绑。"} {...actionFeedbackAttrs("s,e")}>
          改绑候选
        </button>
        <button type="button" onClick={() => updateState("补单草稿")} disabled={!LABEL_DEMO_MODE} title={!LABEL_DEMO_MODE ? "生产模式未接入补单草稿 API，当前操作已禁用。" : "生成补单草稿状态并保留当前证据。"} {...actionFeedbackAttrs("s,e")}>
          生成补单
        </button>
      </div>
    </section>
  );
}
