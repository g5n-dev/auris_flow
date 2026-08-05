import { createQualityAppeal } from "../../../api/client";
import type { HumanReviewChange } from "../model/reviewDecisionModel";
import { upsertHumanReviewChange } from "../model/reviewDecisionModel";
import type { MarkState } from "../types";
import type { SelectedListeningLabel } from "./useSelectedListeningLabel";

type ReviewDecisionSecondaryContext = Pick<
  SelectedListeningLabel,
  | "activeSample"
  | "appealPending"
  | "appealReason"
  | "latestReviewDecision"
  | "setAgentState"
  | "setAppealComposerOpen"
  | "setAppealPending"
  | "setCreatedAppeal"
  | "setListeningNotice"
  | "setLowConfidence"
  | "setMarkState"
  | "setReviewChanges"
>;

export function createReviewDecisionSecondaryActions(
  context: ReviewDecisionSecondaryContext
) {
  const {
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
  } = context;

  const recordReviewChange = (change: HumanReviewChange) => {
    setReviewChanges((current) => upsertHumanReviewChange(current, change));
    setListeningNotice({
      status: "pending",
      title: "人工修订待提交",
      detail: `${change.target_type} / ${change.target_id} 将随主决定一次提交并写后回读。`
    });
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
      status: state === "pending" ? "idle" : "pending",
      title:
        state === "accepted"
          ? "接受 Agent 建议待提交"
          : state === "rejected"
            ? "拒绝 Agent 建议待提交"
            : "已恢复待复核",
      detail: `${activeSample.sessionId} 的决定已暂存，提交并回读一致前不会推进队列。`
    });
  };

  const updateMarkState = (state: MarkState) => {
    setMarkState(state);
    setListeningNotice({
      status: state === "none" ? "idle" : "pending",
      title:
        state === "main"
          ? "主录音修订待提交"
          : state === "crosstalk"
            ? "串音修订待提交"
            : state === "duplicate"
              ? "重复收录修订待提交"
              : "已清除录音判定",
      detail: `${activeSample.sessionId} 的录音判定已暂存，将随 HumanReviewDecisionRequest.changes 提交。`
    });
  };

  const updateLowConfidence = (value: boolean) => {
    setLowConfidence(value);
    setListeningNotice({
      status: value ? "pending" : "idle",
      title: value ? "低置信修订待提交" : "已清除低置信标记",
      detail: `${activeSample.sessionId} 的低置信判断已暂存，将随当前决定一次提交。`
    });
  };

  return {
    recordReviewChange,
    submitLatestQualityAppeal,
    updateAgentDecision,
    updateLowConfidence,
    updateMarkState
  };
}
