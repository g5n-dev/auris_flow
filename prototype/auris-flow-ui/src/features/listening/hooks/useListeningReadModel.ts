import { createUserIntentIdempotencyKey } from "../../../api/client";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import {
  emptyReviewSample,
  reviewQueueKeyForLabel,
  reviewSamples
} from "../fixtures/reviewSamples";
import type { ReviewSample } from "../fixtures/reviewSamples";
import { loadListeningQueueFacts } from "./listeningQueueLoader";
import type { ListeningState } from "./useListeningState";
import { useCallback, useEffect, useMemo, useRef } from "react";

type EmptyReadState = "empty" | "complete";

export function useListeningReadModel(context: ListeningState) {
  const {
    activeModule,
    activeSampleId,
    backendReviewSamples,
    currentUser,
    focus,
    listeningReadRetry,
    setActiveChip,
    setActiveQueue,
    setActiveSampleId,
    setAgentState,
    setBackendReviewSamples,
    setListeningReadDetail,
    setListeningReadState,
    setLowConfidence,
    setMarkState,
    setPanelTab,
    setReviewChanges,
    setReviewDecisionIdempotencyKey,
    setSelectedAssetKey,
    setSelectedDataAssetId,
    setSelectedWindow,
    topbarContext
  } = context;
  const refreshGeneration = useRef(0);

  const applyActiveSample = useCallback((sample: ReviewSample) => {
    setActiveSampleId(sample.id);
    setSelectedDataAssetId(sample.dataAssetId);
    setSelectedAssetKey(sample.assetKey);
    setActiveQueue(sample.queue);
    setActiveChip(sample.queue);
    setSelectedWindow(sample.window);
    setAgentState("pending");
    setMarkState("none");
    setLowConfidence(false);
    setReviewChanges([]);
    setReviewDecisionIdempotencyKey(
      createUserIntentIdempotencyKey(`human_review_decision_${sample.reviewTaskId ?? sample.id}`)
    );
    setPanelTab("agent");
  }, [
    setActiveChip,
    setActiveQueue,
    setActiveSampleId,
    setAgentState,
    setLowConfidence,
    setMarkState,
    setPanelTab,
    setReviewChanges,
    setReviewDecisionIdempotencyKey,
    setSelectedAssetKey,
    setSelectedDataAssetId,
    setSelectedWindow
  ]);

  const refreshPendingReviewQueue = useCallback(async (
    queueKey?: string,
    emptyReadState: EmptyReadState = "empty"
  ): Promise<ReviewSample[]> => {
    const generation = ++refreshGeneration.current;
    if (LABEL_DEMO_MODE) {
      const requestedDemoQueueKey = queueKey
        ? reviewQueueKeyForLabel(queueKey)
        : "";
      const samples = queueKey
        ? reviewSamples.filter(
            (sample) =>
              reviewQueueKeyForLabel(sample.queueKey ?? sample.queue)
              === requestedDemoQueueKey
          )
        : reviewSamples;
      if (samples[0]) applyActiveSample(samples[0]);
      return samples;
    }

    setListeningReadState("loading");
    setListeningReadDetail(
      queueKey
        ? `正在从 BFF 重新读取 ${queueKey} 的 pending HumanReviewTask。`
        : "正在读取 pending HumanReviewTask 并关联 AudioSession 与 EvidencePack。"
    );
    try {
      const loaded = await loadListeningQueueFacts(queueKey, focus);
      if (generation !== refreshGeneration.current) return [];
      const { requestedAudioSessionId, requestedReviewTaskId, samples } = loaded;
      setBackendReviewSamples(samples);
      if (samples.length === 0 && loaded.taskCount > 0) {
        throw new Error(
          `${loaded.taskCount} 个 pending HumanReviewTask 未关联到可读 AudioSession/EvidencePack，不能宣称队列已完成。`
        );
      }
      if (samples.length === 0) {
        if (emptyReadState === "complete") {
          setListeningReadState("complete");
        } else {
          setListeningReadState("empty");
        }
        setListeningReadDetail(
          queueKey
            ? `${queueKey} 当前没有 pending HumanReviewTask。`
            : "当前租户/项目没有 pending HumanReviewTask；未回退本地样本。"
        );
        return [];
      }

      applyActiveSample(samples[0]);
      setListeningReadState("ready");
      setListeningReadDetail(
        requestedAudioSessionId
          ? `已按 audio_session_id${requestedReviewTaskId ? " / review_task_id / root_trace_id" : ""} 从 BFF 精确回读 ${requestedAudioSessionId}；不会被 pending 队列首项覆盖。`
          : `已从 BFF 读取 ${samples.length} 个 pending 复核对象、${loaded.sessionCount} 个音频会话；决定提交后会逐对象写后回读。`
      );
      return samples;
    } catch (error) {
      if (generation !== refreshGeneration.current) return [];
      setBackendReviewSamples([]);
      setListeningReadState("error");
      setListeningReadDetail(
        error instanceof Error ? error.message : "调听权威事实读取失败"
      );
      throw error;
    }
  }, [
    applyActiveSample,
    focus?.module,
    focus?.objectId,
    focus?.objectKind,
    focus?.reviewTaskId,
    focus?.rootTraceId,
    setBackendReviewSamples,
    setListeningReadDetail,
    setListeningReadState,
    topbarContext.project,
    topbarContext.tenant
  ]);

  useEffect(() => {
    if (LABEL_DEMO_MODE || !currentUser || activeModule !== "listening") return;
    void refreshPendingReviewQueue().catch(() => undefined);
  }, [
    activeModule,
    currentUser?.userId,
    listeningReadRetry,
    refreshPendingReviewQueue,
    topbarContext.project,
    topbarContext.tenant
  ]);

  const reviewSamplePool = LABEL_DEMO_MODE ? reviewSamples : backendReviewSamples;
  const activeSample = useMemo(
    () =>
      reviewSamplePool.find((sample) => sample.id === activeSampleId) ??
      reviewSamplePool[0] ??
      emptyReviewSample,
    [activeSampleId, reviewSamplePool]
  );

  const selectReviewSample = (sample: ReviewSample) => applyActiveSample(sample);

  return {
    ...context,
    reviewSamplePool,
    activeSample,
    selectReviewSample,
    refreshPendingReviewQueue
  };
}

export type ListeningReadModel = ReturnType<typeof useListeningReadModel>;
