import {
  getAudioSession,
  getHumanReviewTask
} from "../../../api/client";
import type { HumanReviewTask } from "../../../api/client";
import {
  getEvidencePack,
  listPendingHumanReviewTasks
} from "../../../api/humanReviewClient";
import { backendId, isRecordValue } from "../../../shared/runtime/records";
import {
  backendAudioSessionSample,
  backendReviewSample
} from "../fixtures/reviewSamples";
import type { ReviewSample } from "../fixtures/reviewSamples";
import type { ListeningState } from "./useListeningState";

export type ListeningQueueLoadResult = {
  requestedAudioSessionId: string;
  requestedReviewTaskId: string;
  samples: ReviewSample[];
  sessionCount: number;
  taskCount: number;
};

async function listAllPendingHumanReviewTasks(
  queueKey: string | undefined
): Promise<HumanReviewTask[]> {
  const tasks: HumanReviewTask[] = [];
  const observedCursors = new Set<string>();
  let cursor = "";
  while (true) {
    const response = await listPendingHumanReviewTasks(queueKey, {
      limit: 50,
      cursor: cursor || undefined
    });
    tasks.push(...response.data.items);
    const nextCursor = String(response.meta?.next_cursor ?? "").trim();
    if (!nextCursor) return tasks;
    if (observedCursors.has(nextCursor)) {
      throw new Error("pending HumanReviewTask 分页游标重复，已停止读取");
    }
    observedCursors.add(nextCursor);
    cursor = nextCursor;
  }
}

async function hydratePendingHumanReviewTask(
  task: HumanReviewTask
): Promise<HumanReviewTask> {
  const taskId = backendId(task, "id", "review_task_id");
  const detail = await getHumanReviewTask(taskId);
  const mergedTask = { ...task, ...detail.data, id: taskId } as HumanReviewTask;
  const embeddedEvidence = isRecordValue(mergedTask.evidence_pack)
    ? mergedTask.evidence_pack
    : {};
  const evidencePackId =
    backendId(embeddedEvidence, "evidence_pack_id", "id") ||
    backendId(mergedTask, "evidence_pack_id");
  if (!evidencePackId) return mergedTask;
  const evidenceReadback = await getEvidencePack(evidencePackId);
  return {
    ...mergedTask,
    evidence_pack_id: evidencePackId,
    evidence_pack: {
      ...embeddedEvidence,
      ...evidenceReadback.data,
      evidence_pack_id: evidencePackId
    }
  } as HumanReviewTask;
}

export async function loadListeningQueueFacts(
  queueKey: string | undefined,
  focus: ListeningState["focus"]
): Promise<ListeningQueueLoadResult> {
  const requestedAudioSessionId =
    !queueKey
    && focus?.module === "listening"
    && focus.objectKind === "audioSession"
      ? focus.objectId?.trim() ?? ""
      : "";
  const requestedReviewTaskId = requestedAudioSessionId
    ? focus?.reviewTaskId?.trim() ?? ""
    : "";
  const requestedRootTraceId = requestedAudioSessionId
    ? focus?.rootTraceId?.trim() ?? ""
    : "";
  const [pendingTasks, requestedSessionResponse] = await Promise.all([
    listAllPendingHumanReviewTasks(queueKey),
    requestedAudioSessionId
      ? getAudioSession(requestedAudioSessionId)
      : Promise.resolve(null)
  ]);
  const tasks = pendingTasks.filter((task) =>
    Boolean(backendId(task, "id", "review_task_id"))
  );
  const requestedPendingTask = requestedReviewTaskId
    ? tasks.find(
        (task) =>
          backendId(task, "id", "review_task_id") === requestedReviewTaskId
      )
    : undefined;
  if (requestedReviewTaskId && !requestedPendingTask) {
    throw new Error(
      `HumanReviewTask ${requestedReviewTaskId} 不在当前服务端 pending 队列`
    );
  }
  // A refreshable exact-review URL must only depend on its requested task.
  // Unrelated tasks from the same import batch may still be materializing and
  // must not make an already-readable review target fail as a group.
  const tasksToHydrate = requestedPendingTask ? [requestedPendingTask] : tasks;
  const taskDetails = await Promise.all(
    tasksToHydrate.map(hydratePendingHumanReviewTask)
  );
  const audioSessionIds = Array.from(new Set(
    taskDetails
      .map((task) => backendId(task, "audio_session_id"))
      .filter(Boolean)
  ));
  const listedSessionDetails = await Promise.all(
    audioSessionIds.map(
      async (audioSessionId) => (await getAudioSession(audioSessionId)).data
    )
  );
  const sessionDetails = requestedSessionResponse
    && !listedSessionDetails.some(
      (session) =>
        backendId(session, "audio_session_id", "session_id", "id")
        === requestedAudioSessionId
    )
      ? [requestedSessionResponse.data, ...listedSessionDetails]
      : listedSessionDetails;

  let samples = taskDetails.flatMap((task, index) => {
    const directSessionId = backendId(task, "audio_session_id");
    const session = sessionDetails.find((candidate) => {
      const candidateId = backendId(candidate, "audio_session_id", "id");
      return directSessionId && candidateId === directSessionId;
    });
    return session ? [backendReviewSample(task, session, index)] : [];
  }).map((sample, index, all) => ({
    ...sample,
    progressIndex: index + 1,
    progressTotal: all.length
  }));

  if (requestedAudioSessionId) {
    const requestedSession = sessionDetails.find(
      (session) =>
        backendId(session, "audio_session_id", "session_id", "id")
        === requestedAudioSessionId
    );
    if (!requestedSession) {
      throw new Error(`AudioSession ${requestedAudioSessionId} 写后回读缺失`);
    }
    const sessionRootTraceId = backendId(requestedSession, "root_trace_id");
    if (requestedRootTraceId && sessionRootTraceId !== requestedRootTraceId) {
      throw new Error(
        `AudioSession ${requestedAudioSessionId} root_trace_id 与可刷新链接不一致`
      );
    }
    const requestedReviewSample = samples.find(
      (sample) =>
        sample.sessionId === requestedAudioSessionId
        && (!requestedReviewTaskId || sample.reviewTaskId === requestedReviewTaskId)
    );
    if (requestedReviewTaskId && !requestedReviewSample) {
      throw new Error(
        `HumanReviewTask ${requestedReviewTaskId} 不属于当前 pending 会话 ${requestedAudioSessionId}`
      );
    }
    if (
      requestedRootTraceId
      && requestedReviewSample
      && requestedReviewSample.rootTraceId !== requestedRootTraceId
    ) {
      throw new Error(
        `HumanReviewTask ${requestedReviewTaskId || "unknown"} root_trace_id 与可刷新链接不一致`
      );
    }
    const requestedSample =
      requestedReviewSample ?? backendAudioSessionSample(requestedSession);
    samples = [
      requestedSample,
      ...samples.filter((sample) => sample.id !== requestedSample.id)
    ];
  }

  return {
    requestedAudioSessionId,
    requestedReviewTaskId,
    samples,
    sessionCount: sessionDetails.length,
    taskCount: tasks.length
  };
}
