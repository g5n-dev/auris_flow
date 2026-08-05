import { createUserIntentIdempotencyKey, getHumanReviewTask, submitHumanReviewDecision } from "../../../api/client";
import { getEvidencePack, readHumanReviewAffectedObjects } from "../../../api/humanReviewClient";
import { emptyReviewSample, getReviewTaskIdForSample, reviewQueueKeyForLabel } from "../fixtures/reviewSamples";
import { buildHumanReviewDecisionRequest, validateHumanReviewDecisionClosure } from "../model/reviewDecisionModel";
import { createReviewDecisionSecondaryActions } from "./reviewDecisionSecondaryActions";
import type { SelectedListeningLabel } from "./useSelectedListeningLabel";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";

export function createReviewDecisionActions(context: SelectedListeningLabel) {
  const {
    activeSample,
    agentState,
    appealPending,
    appealReason,
    completedSampleIds,
    latestReviewDecision,
    listeningActionPending,
    lowConfidence,
    markState,
    navigateModuleRoot,
    navigateToTarget,
    refreshPendingReviewQueue,
    reviewChanges,
    reviewDecisionIdempotencyKey,
    reviewSamplePool,
    selectedLabel,
    setAppealReason,
    setAgentState,
    setAppealComposerOpen,
    setAppealPending,
    setCompletedSampleIds,
    setCreatedAppeal,
    setLatestReviewDecision,
    setListeningActionPending,
    setListeningNotice,
    setLowConfidence,
    setMarkState,
    setReviewChanges
  } = context;

  const confirmAndMoveNext = async () => {
    if (listeningActionPending) return;
    if (activeSample.id === emptyReviewSample.id || !reviewSamplePool.length) {
      setListeningNotice({
        status: "error",
        title: "没有可提交的复核对象",
        detail: "请先从 BFF 读取已关联 HumanReviewTask、AudioSession 和 EvidencePack 的对象。"
      });
      return;
    }
    if (!LABEL_DEMO_MODE && !activeSample.reviewTaskId) {
      setListeningNotice({
        status: "error",
        title: "当前会话尚无待审任务",
        detail: `${activeSample.sessionId} 只完成了音频导入；生成 EvidencePack 与 HumanReviewTask 后才能提交决定。`
      });
      return;
    }
    if (!LABEL_DEMO_MODE && !activeSample.rootTraceId) {
      setListeningNotice({
        status: "error",
        title: "当前会话缺少业务根 Trace",
        detail: `${activeSample.sessionId} 的 HumanReviewTask、EvidencePack 与 AudioSession 尚未返回一致 root_trace_id，已阻断提交。`
      });
      return;
    }
    setListeningActionPending(true);
    setListeningNotice({
      status: "pending",
      title: "正在提交并核验复核决定",
      detail: `${activeSample.sessionId} 将依次写入决定、回读任务、EvidencePack 和全部受影响对象。`
    });
    try {
      const reviewTaskId = getReviewTaskIdForSample(activeSample);
      const expectedRootTraceId = activeSample.rootTraceId ?? "";
      const request = buildHumanReviewDecisionRequest({
        agentState,
        evidencePackId: activeSample.dataAssetId,
        lowConfidence,
        markState,
        note: `${activeSample.conclusion} · ${selectedLabel}`,
        stagedChanges: reviewChanges
      });
      const decision = await submitHumanReviewDecision(reviewTaskId, request, {
        idempotencyKey: reviewDecisionIdempotencyKey
      });
      const rawDecisionId = decision.data.raw.decision_id;
      const decisionId = typeof rawDecisionId === "string" && rawDecisionId ? rawDecisionId : decision.data.id;
      const rawRootTraceId = decision.data.raw.root_trace_id;
      const decisionRootTraceId =
        typeof rawRootTraceId === "string" && rawRootTraceId
          ? rawRootTraceId
          : "";
      if (!decisionId) {
        throw new Error("后端复核回执缺少 decision_id，已停止推进当前样本。");
      }
      if (!decisionRootTraceId) {
        throw new Error("后端复核回执缺少业务 root_trace_id，已停止推进当前样本。");
      }

      const reviewTaskReadback = await getHumanReviewTask(reviewTaskId);
      const evidencePackReadback = await getEvidencePack(activeSample.dataAssetId);
      const affectedObjects = decision.data.affected_objects ?? [];
      const affectedReadbacks = await readHumanReviewAffectedObjects(
        decision.data.affected_objects ?? []
      );
      const closureErrors = validateHumanReviewDecisionClosure({
        decisionId,
        expectedRootTraceId,
        receiptRootTraceId: decisionRootTraceId,
        reviewTaskId,
        evidencePackId: activeSample.dataAssetId,
        request,
        taskReadback: reviewTaskReadback.data,
        evidencePackReadback: evidencePackReadback.data,
        affectedObjects,
        affectedReadbacks
      });
      if (closureErrors.length > 0) {
        throw new Error(`写后回读不一致：${closureErrors.join("；")}`);
      }

      setLatestReviewDecision({
        decisionId,
        reviewTaskId,
        evidenceRefs: [activeSample.dataAssetId],
        sampleTitle: `${activeSample.sessionId} / ${activeSample.queueTitle}`,
        rootTraceId: decisionRootTraceId,
        affectedObjects,
        idempotencyKey: createUserIntentIdempotencyKey(`quality_appeal_${decisionId}`)
      });
      setCreatedAppeal(null);
      setAppealReason("");
      setCompletedSampleIds(Array.from(new Set([
        ...completedSampleIds,
        activeSample.id
      ])));

      const nextSamples = await refreshPendingReviewQueue(
        activeSample.queueKey ?? reviewQueueKeyForLabel(activeSample.queue),
        "complete"
      );
      if (nextSamples.length > 0) {
        const nextSample = nextSamples[0];
        if (
          !LABEL_DEMO_MODE
          && (!nextSample.reviewTaskId || !nextSample.rootTraceId)
        ) {
          throw new Error(
            `下一通 ${nextSample.sessionId} 缺少 review_task_id 或 root_trace_id，已停止导航。`
          );
        }
        navigateToTarget({
          module: "listening",
          objectKind: "audioSession",
          objectId: nextSample.sessionId,
          audioSessionId: nextSample.sessionId,
          reviewTaskId: nextSample.reviewTaskId,
          rootTraceId: nextSample.rootTraceId,
          title: nextSample.queueTitle,
          detail: `${nextSample.dataAssetId} / ${nextSample.assetKey}`,
          window: nextSample.window,
          focusMode: "evidence"
        });
        setListeningNotice({
          status: "success",
          title: "决定已核验，已读取下一通",
          detail: `${reviewTaskId} 及全部受影响对象回读一致；Trace ${decisionRootTraceId}；当前载入 ${nextSample.sessionId} / ${nextSample.queueTitle}。`
        });
      } else {
        navigateModuleRoot("listening");
        setReviewChanges([]);
        setMarkState("none");
        setLowConfidence(false);
        setListeningNotice({
          status: "success",
          title: "当前队列复核完成",
          detail: `${reviewTaskId} 及全部受影响对象回读一致；Trace ${decisionRootTraceId}；服务端 pending 队列已为空。`
        });
      }
    } catch (error) {
      setListeningNotice({
        status: "error",
        title: "复核闭环未完成",
        detail:
          error instanceof Error
            ? error.message
            : "后端写入或回读校验失败，当前样本未推进。"
      });
    } finally {
      setListeningActionPending(false);
    }
  };

  const changeReviewQueue = async (queueLabel: string) => {
    if (listeningActionPending) return;
    if (reviewChanges.length > 0 || markState !== "none" || lowConfidence || agentState !== "pending") {
      setListeningNotice({
        status: "error",
        title: "当前决定尚未提交",
        detail: "请先提交并完成写后回读，再切换服务端待审队列，避免丢失人工修订。"
      });
      return;
    }
    setListeningActionPending(true);
    setListeningNotice({
      status: "pending",
      title: `正在读取${queueLabel}`,
      detail: "队列切换会重新查询服务端 pending HumanReviewTask。"
    });
    try {
      const samples = await refreshPendingReviewQueue(
        reviewQueueKeyForLabel(queueLabel),
        "complete"
      );
      if (samples[0]) {
        navigateToTarget({
          module: "listening",
          objectKind: LABEL_DEMO_MODE ? "reviewSample" : "audioSession",
          objectId: LABEL_DEMO_MODE ? samples[0].id : samples[0].sessionId,
          audioSessionId: samples[0].sessionId,
          reviewTaskId: samples[0].reviewTaskId,
          rootTraceId: samples[0].rootTraceId,
          title: samples[0].queueTitle,
          detail: `${samples[0].dataAssetId} / ${samples[0].assetKey}`,
          window: samples[0].window,
          focusMode: "evidence"
        });
      } else {
        navigateModuleRoot("listening");
      }
      setListeningNotice(
        samples[0]
          ? {
              status: "success",
              title: `已切换到${queueLabel}`,
              detail: `${samples[0].sessionId} / ${samples[0].file} 来自最新 pending 队列。`
            }
          : {
              status: "success",
              title: `${queueLabel}已复核完成`,
              detail: "服务端没有剩余 pending HumanReviewTask。"
            }
      );
    } catch (error) {
      setListeningNotice({
        status: "error",
        title: `${queueLabel}读取失败`,
        detail: error instanceof Error ? error.message : "无法读取服务端待审队列。"
      });
    } finally {
      setListeningActionPending(false);
    }
  };

  const {
    recordReviewChange,
    submitLatestQualityAppeal,
    updateAgentDecision,
    updateLowConfidence,
    updateMarkState
  } = createReviewDecisionSecondaryActions({
      activeSample,
      appealPending,
      appealReason,
      latestReviewDecision,
      setAgentState,
      setAppealComposerOpen,
      setAppealPending,
      setCreatedAppeal,
      setListeningNotice,
      setLowConfidence,
      setMarkState,
      setReviewChanges
    });

  return {
    ...context,
    changeReviewQueue,
    confirmAndMoveNext,
    recordReviewChange,
    submitLatestQualityAppeal,
    updateAgentDecision,
    updateLowConfidence,
    updateMarkState
  };
}

export type ReviewDecisionActions = ReturnType<typeof createReviewDecisionActions>;
