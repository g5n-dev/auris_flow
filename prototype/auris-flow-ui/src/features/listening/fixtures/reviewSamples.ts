import type { AudioSessionDetail, HumanReviewTask } from "../../../api/client";
import { receptionOrderCandidates } from "../../../shared/fixtures/receptionOrders";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { backendId, isRecordValue, recordList } from "../../../shared/runtime/records";
import { docs, mismatches, reviewQueueMockData } from "./evidenceFixtures";
import type listeningFixtureSchema from "./data/listening-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const listeningFixture = await loadJsonFixture<typeof listeningFixtureSchema>(
  new URL("./data/listening-fixtures.json", import.meta.url),
  "调听 fixture"
);


export type ReviewSample = {
  id: string;
  reviewTaskId?: string;
  eventLinkIds?: string[];
  dataAssetId: string;
  assetKey: string;
  queue: string;
  sessionId: string;
  sessionStartedAt?: string;
  file: string;
  window: string;
  activeTime: string;
  sessionEnd: string;
  speaker: string;
  customer: string;
  title: string;
  subtitle: string;
  queueTitle: string;
  queueMeta: string;
  queueDetail: string;
  conclusion: string;
  confidence: number;
  reason: string;
  progressIndex: number;
  progressTotal: number;
  selectedLabel: string;
  simpleTitle: string;
  simpleMeta: string[];
  evidenceItems: Array<[string, string, string]>;
  mismatches: typeof mismatches;
  docs: typeof docs;
  crosstalk: {
    title: string;
    detail: string;
    primary: string;
    candidate: string;
  };
};

export const reviewSamples: ReviewSample[] = (listeningFixture.reviewSamples.reviewSamples as unknown as ReviewSample[]);

export const emptyReviewSample: ReviewSample = (listeningFixture.reviewSamples.emptyReviewSample as unknown as ReviewSample);

export const initialListeningSample = LABEL_DEMO_MODE ? reviewSamples[0] : emptyReviewSample;

export const getReceptionCandidatesForSample = (sample: ReviewSample) => {
  const linked = receptionOrderCandidates.filter(
    (candidate) => candidate.eventLinkId && sample.eventLinkIds?.includes(candidate.eventLinkId)
  );
  const exact = receptionOrderCandidates.filter((candidate) => candidate.sampleIds.includes(sample.id));
  if (linked.length > 0 || exact.length > 0) {
    return [...linked, ...exact].filter(
      (candidate, index, candidates) => candidates.findIndex((item) => item.id === candidate.id) === index
    );
  }
  return receptionOrderCandidates.filter((candidate) => candidate.sampleIds.includes("*"));
};

export const getReviewTaskIdForSample = (sample: ReviewSample) => {
  if (sample.reviewTaskId) return sample.reviewTaskId;
  if (sample.id === "sample-af-129") return "hrt_crosstalk_001";
  if (sample.id === "sample-af-128") return "hrt_amount_001";
  return "hrt_low_confidence_draft";
};

export const getReviewQueueMock = (queueLabel: string) =>
  reviewQueueMockData.find((item) => item.label === queueLabel) ?? reviewQueueMockData[0];

export const listeningQueueLabels: Record<string, string> = (listeningFixture.reviewSamples.listeningQueueLabels as unknown as Record<string, string>);

export const listeningQueueDefaults: Record<string, Pick<ReviewSample, "selectedLabel" | "queueTitle" | "conclusion">> = (listeningFixture.reviewSamples.listeningQueueDefaults as unknown as Record<string, Pick<ReviewSample, "selectedLabel" | "queueTitle" | "conclusion">>);

export function sessionClock(value: unknown, fallback: string) {
  if (typeof value !== "string" || !value) return fallback;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai"
  }).format(parsed);
}

export function backendReviewSample(
  task: HumanReviewTask,
  session: AudioSessionDetail,
  index: number
): ReviewSample {
  const evidencePack = isRecordValue(task.evidence_pack) ? task.evidence_pack : {};
  const evidencePackId = backendId(evidencePack, "evidence_pack_id", "id") || backendId(task, "evidence_pack_id") || `EP-${index + 1}`;
  const rawQueue = backendId(task, "queue");
  const queue = listeningQueueLabels[rawQueue] ?? (rawQueue || "待人工");
  const queueDefaults = listeningQueueDefaults[queue] ?? {
    selectedLabel: "待复核标签",
    queueTitle: backendId(task, "title") || "待人工复核",
    conclusion: "待人工确认"
  };
  const recording = isRecordValue(session.recording) ? session.recording : {};
  const asrSegments = recordList(session.asr_segments);
  const eventLinks = recordList(session.event_links);
  const firstAsr = asrSegments[0] ?? {};
  const firstEvent = eventLinks[0] ?? {};
  const eventDiffs = recordList(firstEvent.diffs);
  const confidenceValue = Number(session.confidence ?? firstAsr.confidence ?? 0);
  const confidence = Number.isFinite(confidenceValue)
    ? Math.round((confidenceValue <= 1 ? confidenceValue * 100 : confidenceValue))
    : 0;
  const startClock = sessionClock(session.started_at, "--:--");
  const endClock = sessionClock(session.ended_at, "--:--");
  const windowStartMs = Number(evidencePack.window_start_ms ?? 0);
  const activeSeconds = Number.isFinite(windowStartMs) ? Math.max(0, Math.floor(windowStartMs / 1000)) : 0;
  const activeTime = activeSeconds
    ? `${startClock.slice(0, 5)} + ${Math.floor(activeSeconds / 60)}:${String(activeSeconds % 60).padStart(2, "0")}`
    : startClock;
  const evidenceItems = asrSegments.slice(0, 3).map((segment, segmentIndex): [string, string, string] => [
    `${Math.floor(Number(segment.start_ms ?? 0) / 60000)}:${String(Math.floor(Number(segment.start_ms ?? 0) / 1000) % 60).padStart(2, "0")}`,
    backendId(segment, "speaker") || "ASR",
    backendId(segment, "text") || `ASR 片段 ${segmentIndex + 1}`
  ]);
  const mismatchesFromBackend = eventDiffs.map((diff) => ({
    field: backendId(diff, "field") || "事件字段",
    audio: String(diff.audio_value ?? "--"),
    doc: String(diff.document_value ?? "--"),
    state: "待确认"
  }));
  const sessionId = backendId(session, "audio_session_id", "id");
  const taskId = backendId(task, "id", "review_task_id");
  const assetKey = backendId(task, "asset_key") || backendId(evidencePack, "asset_key") || "unbound/evidence";
  return {
    id: `backend-${taskId || evidencePackId}`,
    reviewTaskId: taskId,
    eventLinkIds: eventLinks.map((event) => backendId(event, "id")).filter(Boolean),
    dataAssetId: evidencePackId,
    assetKey,
    queue,
    sessionId,
    sessionStartedAt:
      typeof session.started_at === "string" && !Number.isNaN(new Date(session.started_at).getTime())
        ? session.started_at
        : undefined,
    file: backendId(recording, "file_name", "filename", "object_key") || backendId(session, "recording_id") || "未注册录音对象",
    window: `${startClock.slice(0, 5)} - ${endClock.slice(0, 5)}`,
    activeTime,
    sessionEnd: endClock,
    speaker: backendId(firstAsr, "speaker") || backendId(session, "primary_employee_id") || "待识别说话人",
    customer: backendId(session, "customer_ref", "subject_ref") || "未关联主体",
    title: backendId(task, "title") || backendId(evidencePack, "title") || "待人工复核",
    subtitle: backendId(task, "summary") || `${queue} / ${evidencePackId}`,
    queueTitle: queueDefaults.queueTitle,
    queueMeta: `${activeTime} / ${backendId(firstAsr, "speaker") || "待识别说话人"}`,
    queueDetail: evidenceItems[0]?.[2] || `${evidencePackId} 等待人工复核。`,
    conclusion: queueDefaults.conclusion,
    confidence,
    reason: eventDiffs.length > 0 ? "后端事件字段与音频证据存在差异，需要人工确认。" : "当前结论只来自后端证据引用，等待人工确认。",
    progressIndex: index + 1,
    progressTotal: index + 1,
    selectedLabel: queueDefaults.selectedLabel,
    simpleTitle: `${backendId(session, "location_id", "scope_id", "store_id") || "当前业务范围"} / ${sessionId}`,
    simpleMeta: [`会话 ${sessionId}`, `任务 ${taskId}`, `Trace ${backendId(task, "trace_id") || backendId(session, "trace_id") || "--"}`],
    evidenceItems,
    mismatches: mismatchesFromBackend,
    docs: eventLinks.length > 0
      ? eventLinks.slice(0, 3).map((event, eventIndex) => ({
          type: backendId(event, "relation_type") || "业务事件",
          id: backendId(event, "document_ref", "event_ref", "id") || `EVENT-${eventIndex + 1}`,
          status: backendId(event, "status") || "待确认",
          match: `${Math.round(Number(event.match_score ?? 0) * 100)}%`,
          tone: Number(event.match_score ?? 0) >= 0.9 ? "green" : "amber"
        }))
      : [],
    crosstalk: {
      title: backendId(session, "acoustic_relation_title") || "声学关系待确认",
      detail: backendId(session, "acoustic_relation_detail") || "后端未提供串音、重复收录或设备重叠证据。",
      primary: backendId(session, "primary_recording_id") || backendId(session, "recording_id") || "未识别主录音",
      candidate: backendId(session, "candidate_recording_id") || "无候选"
    }
  };
}
