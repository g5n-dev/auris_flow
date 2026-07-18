import { createQualityAppeal, createUserIntentIdempotencyKey, getHumanReviewTask, submitHumanReviewDecision } from "../../../api/client";
import { backendId } from "../../../shared/runtime/records";
import { emptyReviewSample, getReviewTaskIdForSample } from "../fixtures/reviewSamples";
import type { MarkState } from "../types";
import type { SelectedListeningLabel } from "./useSelectedListeningLabel";

export function createReviewDecisionActions(context: SelectedListeningLabel) {
  const { activeSample, agentState, appealPending, appealReason, completedSampleIds, latestReviewDecision, listeningActionPending, reviewSamplePool, selectReviewSample, selectedLabel, setAgentState, setAppealComposerOpen, setAppealPending, setAppealReason, setCompletedSampleIds, setCreatedAppeal, setLatestReviewDecision, setListeningActionPending, setListeningNotice, setMarkState } = context;
  const changeReviewQueue = (queueLabel: string) => {
      const nextSample =
        reviewSamplePool.find((sample) => sample.queue === queueLabel && !completedSampleIds.includes(sample.id)) ??
        reviewSamplePool.find((sample) => sample.queue === queueLabel) ??
        activeSample;
      selectReviewSample(nextSample);
      setListeningNotice({
        status: "success",
        title: `已切换到${queueLabel}`,
        detail: `${nextSample.sessionId} / ${nextSample.file} 已载入，右侧证据和底部进度同步到当前队列。`
      });
    };

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
      setListeningActionPending(true);
      setListeningNotice({
        status: "pending",
        title: "正在提交复核决策",
        detail: `${activeSample.sessionId} / ${activeSample.queueTitle} 正在写入 HumanReviewTask，成功后进入下一通。`
      });
      try {
        const reviewTaskId = getReviewTaskIdForSample(activeSample);
        const decision = await submitHumanReviewDecision(reviewTaskId, {
          decision: agentState === "rejected" ? "rejected" : "accepted",
          note: `${activeSample.conclusion} · ${selectedLabel}`
        });
        const decisionId = typeof decision.data.raw.decision_id === "string" ? decision.data.raw.decision_id : "";
        if (!decisionId) {
          throw new Error("后端复核回执缺少 decision_id，已停止推进当前样本。");
        }
        const reviewTaskReadback = await getHumanReviewTask(reviewTaskId);
        const readbackStatus = backendId(reviewTaskReadback.data, "status");
        if (!readbackStatus || ["pending", "queued", "running"].includes(readbackStatus)) {
          throw new Error(`${reviewTaskId} 写后回读仍为 ${readbackStatus || "unknown"}，当前样本未推进。`);
        }
        setLatestReviewDecision({
          decisionId,
          reviewTaskId,
          evidenceRefs: [activeSample.dataAssetId],
          sampleTitle: `${activeSample.sessionId} / ${activeSample.queueTitle}`,
          traceId: decision.meta?.trace_id ?? decision.data.trace_id,
          idempotencyKey: createUserIntentIdempotencyKey(`quality_appeal_${decisionId}`)
        });
        setCreatedAppeal(null);
        setAppealReason("");
      const nextCompleted = Array.from(new Set([...completedSampleIds, activeSample.id]));
      const nextSample =
        reviewSamplePool.find((sample) => sample.queue === activeSample.queue && !nextCompleted.includes(sample.id)) ??
        reviewSamplePool.find((sample) => !nextCompleted.includes(sample.id)) ??
        reviewSamplePool[0] ?? emptyReviewSample;
      setCompletedSampleIds(nextCompleted);
      setListeningNotice({
        status: "success",
        title: "已确认并进入下一通",
          detail: `${reviewTaskId} 已写入后端，Trace ${decision.meta?.trace_id ?? decision.data.trace_id ?? "pending"}；当前载入 ${nextSample.sessionId} / ${nextSample.queueTitle}。`
      });
      selectReviewSample(nextSample);
      } catch (error) {
        setListeningNotice({
          status: "error",
          title: "复核提交失败",
          detail: error instanceof Error ? error.message : "后端未返回可用复核结果，当前样本未推进。"
        });
      } finally {
        setListeningActionPending(false);
      }
    };

  const submitLatestQualityAppeal = async () => {
      if (!latestReviewDecision || appealPending) return;
      const reason = appealReason.trim();
      if (reason.length < 8) {
        setListeningNotice({
          status: "error",
          title: "申诉理由不完整",
          detail: "请说明原结论遗漏或误判的事实，至少输入 8 个字符。"
        });
        return;
      }
      setAppealPending(true);
      setListeningNotice({
        status: "pending",
        title: "正在提交质检申诉",
        detail: `${latestReviewDecision.decisionId} 将冻结原决定和证据引用，原决定不会被覆盖。`
      });
      try {
        const response = await createQualityAppeal(
          latestReviewDecision.decisionId,
          { reason, evidence_refs: latestReviewDecision.evidenceRefs },
          { idempotencyKey: latestReviewDecision.idempotencyKey }
        );
        setCreatedAppeal(response.data);
        setAppealComposerOpen(false);
        setListeningNotice({
          status: "success",
          title: "质检申诉已立案",
          detail: `${response.data.id} / Trace ${response.meta?.trace_id ?? response.data.trace_id ?? "pending"}；等待独立复议人领取。`
        });
      } catch (error) {
        setListeningNotice({
          status: "error",
          title: "质检申诉提交失败",
          detail: error instanceof Error ? error.message : "申诉未落账，可保留当前理由后重试。"
        });
      } finally {
        setAppealPending(false);
      }
    };

  const updateAgentDecision = (state: "pending" | "accepted" | "rejected") => {
      setAgentState(state);
      setListeningNotice({
        status: state === "rejected" ? "error" : state === "accepted" ? "success" : "idle",
        title: state === "accepted" ? "已接受 Agent 建议" : state === "rejected" ? "已拒绝 Agent 建议" : "已恢复待复核",
        detail: `${activeSample.sessionId} / ${activeSample.queueTitle} 的处理状态已更新，可继续标记主录音、串音或进入下一通。`
      });
    };

  const updateMarkState = (state: MarkState) => {
      setMarkState(state);
      setListeningNotice({
        status: state === "none" ? "idle" : "success",
        title: state === "main" ? "已标记主录音" : state === "crosstalk" ? "已标记串音" : state === "duplicate" ? "已标记重复收录" : "已清除标记",
        detail: `${activeSample.sessionId} 的录音判定已写入当前证据链，提交后会同步标签和资产状态。`
      });
    };

  return { ...context, changeReviewQueue, confirmAndMoveNext, submitLatestQualityAppeal, updateAgentDecision, updateMarkState };
}

export type ReviewDecisionActions = ReturnType<typeof createReviewDecisionActions>;
