import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import type { LabelsIntentRecovery } from "./useLabelsIntentRecovery";
import type { LabelsNavigationActions } from "./buildLabelsNavigationActions";
import type { LabelsOptimizationActions } from "./buildLabelsOptimizationActions";
import { adjudicateLabelAggregateReview, createHumanReviewTask, createUserIntentIdempotencyKey, getHumanReviewTask, submitHumanReviewDecision, submitLabelAggregateReview } from "../../../api/client";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { isRecordValue } from "../../../shared/runtime/records";
import type { LabelCandidate, LabelReviewState, LabelReviewTaskView } from "../types";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

type BuildLabelsReviewActionsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions;

export function buildLabelsReviewActions(activeCandidate: BuildLabelsReviewActionsScope["activeCandidate"], activeReviewTask: BuildLabelsReviewActionsScope["activeReviewTask"], backendReviewTaskIdsByCandidateId: BuildLabelsReviewActionsScope["backendReviewTaskIdsByCandidateId"], closedLoopReviewProgress: BuildLabelsReviewActionsScope["closedLoopReviewProgress"], executeLabelOptimization: BuildLabelsReviewActionsScope["executeLabelOptimization"], humanChangeDraft: BuildLabelsReviewActionsScope["humanChangeDraft"], labelAggregates: BuildLabelsReviewActionsScope["labelAggregates"], labelEntityAction: BuildLabelsReviewActionsScope["labelEntityAction"], labelRootTraceId: BuildLabelsReviewActionsScope["labelRootTraceId"], labelShortTrace: BuildLabelsReviewActionsScope["labelShortTrace"], lockedLabelVersionId: BuildLabelsReviewActionsScope["lockedLabelVersionId"], resetCandidateReview: BuildLabelsReviewActionsScope["resetCandidateReview"], reviewDecisionActions: BuildLabelsReviewActionsScope["reviewDecisionActions"], reviewDraftState: BuildLabelsReviewActionsScope["reviewDraftState"], reviewInputs: BuildLabelsReviewActionsScope["reviewInputs"], reviewState: BuildLabelsReviewActionsScope["reviewState"], reviewTasks: BuildLabelsReviewActionsScope["reviewTasks"], setActionFeedback: BuildLabelsReviewActionsScope["setActionFeedback"], setBackendReviewTaskIdsByCandidateId: BuildLabelsReviewActionsScope["setBackendReviewTaskIdsByCandidateId"], setBatchDecisionReceipt: BuildLabelsReviewActionsScope["setBatchDecisionReceipt"], setClosedLoopReviewProgress: BuildLabelsReviewActionsScope["setClosedLoopReviewProgress"], setConflictDecision: BuildLabelsReviewActionsScope["setConflictDecision"], setDraftStatus: BuildLabelsReviewActionsScope["setDraftStatus"], setExperimentState: BuildLabelsReviewActionsScope["setExperimentState"], setLabelEntityAction: BuildLabelsReviewActionsScope["setLabelEntityAction"], setLabelEntityNotice: BuildLabelsReviewActionsScope["setLabelEntityNotice"], setReleaseChecks: BuildLabelsReviewActionsScope["setReleaseChecks"], setReviewDraftStatesByCandidateId: BuildLabelsReviewActionsScope["setReviewDraftStatesByCandidateId"], setReviewState: BuildLabelsReviewActionsScope["setReviewState"], setSelectedCandidateId: BuildLabelsReviewActionsScope["setSelectedCandidateId"], setSelectedCandidateIds: BuildLabelsReviewActionsScope["setSelectedCandidateIds"], setSelectedConflictKey: BuildLabelsReviewActionsScope["setSelectedConflictKey"], setSelectedReviewId: BuildLabelsReviewActionsScope["setSelectedReviewId"]) {
  const runScenarioAgent = () => {
      setDraftStatus("草稿");
      resetCandidateReview(activeCandidate.id);
      setSelectedReviewId("HR-1029");
      setSelectedConflictKey("conflict-0");
      setConflictDecision("待仲裁");
      setExperimentState("未开始");
      void executeLabelOptimization("agent");
    };

  const ensureLabelHumanReviewTask = async (candidate: LabelCandidate, taskTitle: string) => {
      if (!LABEL_DEMO_MODE) {
        const aggregate = labelAggregates.find((item) => item.aggregate_id === candidate.id);
        if (!aggregate) {
          throw new Error(`${candidate.id} 不是已物化 LabelAggregate，不能创建或提交候选级审核任务`);
        }
        if (!aggregate.review_task_id) {
          throw new Error(`${aggregate.aggregate_id} 缺少 LabelAggregate.review_task_id，已阻断通用任务创建`);
        }
        const readback = await getHumanReviewTask(aggregate.review_task_id, { correlationId: labelRootTraceId });
        const targets = Array.isArray(readback.data.target_refs)
          ? readback.data.target_refs.filter(isRecordValue)
          : [];
        const aggregateTargets = targets.filter((target) =>
          ["label_aggregate", "label_aggregates"].includes(String(target.type ?? ""))
        );
        if (
          aggregateTargets.length !== 1 ||
          String(aggregateTargets[0]?.id ?? "") !== aggregate.aggregate_id
        ) {
          throw new Error(`${aggregate.review_task_id} 未唯一绑定当前 Aggregate，已阻断串任务决策`);
        }
        return {
          id: aggregate.review_task_id,
          data: readback.data,
          traceId: readback.meta?.trace_id
        };
      }
      const existingTaskId = backendReviewTaskIdsByCandidateId[candidate.id];
      if (existingTaskId) {
        try {
          const readback = await getHumanReviewTask(existingTaskId, { correlationId: labelRootTraceId });
          const boundCandidateId = String(readback.data.candidate_id ?? readback.data.target_id ?? "");
          if (!boundCandidateId || boundCandidateId === candidate.id) {
            return { id: existingTaskId, data: readback.data, traceId: readback.meta?.trace_id };
          }
        } catch {
          // 后端任务不存在或已不可读时，为当前候选创建新的显式绑定任务。
        }
        setBackendReviewTaskIdsByCandidateId((current) => {
          const next = { ...current };
          delete next[candidate.id];
          return next;
        });
      }
      const taskId = `hrt-label-${candidate.id}-${Date.now().toString(36)}`;
      const receipt = await createHumanReviewTask({
        id: taskId,
        review_task_id: taskId,
        queue: "label_candidate_review",
        status: "pending",
        target_type: "label_candidate",
        target_id: candidate.id,
        candidate_id: candidate.id,
        label_version_id: lockedLabelVersionId,
        title: taskTitle,
        assignee: reviewInputs.assignee,
        evidence_refs: [candidate.traceId, candidate.assetImpact],
        allowed_actions: ["accepted", "modified", "rejected", "escalated"],
        source: "labels_ui"
      }, { correlationId: labelRootTraceId });
      const readback = await getHumanReviewTask(receipt.data.id, { correlationId: labelRootTraceId });
      setBackendReviewTaskIdsByCandidateId((current) => ({ ...current, [candidate.id]: receipt.data.id }));
      return { id: receipt.data.id, data: readback.data, traceId: receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id };
    };

  const applyReviewDecision = async (state: typeof reviewState, label: string, detail: string) => {
      if (labelEntityAction) return false;
      const candidate = activeCandidate;
      const taskTitle = activeReviewTask.title;
      setLabelEntityAction("human-decision");
      setLabelEntityNotice({ status: "pending", title: "正在提交人审决策", detail: `${candidate.id} 等待 HumanReviewTask 决策回执。` });
      try {
        const task = await ensureLabelHumanReviewTask(candidate, taskTitle);
        const decision = state === "已接受" ? "accepted" : state === "已修改" ? "modified" : state === "已拒绝" ? "rejected" : "escalated";
        const aggregate = LABEL_DEMO_MODE
          ? undefined
          : labelAggregates.find((item) => item.aggregate_id === candidate.id);
        if (aggregate?.risk_level === "high") {
          if (decision === "escalated") {
            throw new Error("高风险 Aggregate 必须先选择接受、修改或拒绝，系统会按双盲状态决定是否进入仲裁");
          }
          const priorProgress = closedLoopReviewProgress[aggregate.aggregate_id];
          const payload = {
            decision,
            note: detail,
            ...(decision === "modified" ? { value: humanChangeDraft.after } : {})
          } as const;
          const response = priorProgress?.status === "awaiting-adjudication"
            ? await adjudicateLabelAggregateReview(
                aggregate.aggregate_id,
                { ...payload, reason: humanChangeDraft.reason || detail },
                {
                  idempotencyKey: createUserIntentIdempotencyKey(`label_aggregate_adjudication_${aggregate.aggregate_id}`),
                  correlationId: labelRootTraceId
                }
              )
            : await submitLabelAggregateReview(
                aggregate.aggregate_id,
                payload,
                {
                  idempotencyKey: createUserIntentIdempotencyKey(`label_aggregate_review_${aggregate.aggregate_id}`),
                  correlationId: labelRootTraceId
                }
              );
          const progress = response.data;
          setClosedLoopReviewProgress((current) => ({ ...current, [aggregate.aggregate_id]: progress }));
          const terminal = progress.status === "accepted" || progress.status === "rejected";
          if (terminal) {
            setReviewState(progress.status === "accepted" ? state : "已拒绝", candidate.id);
            setReleaseChecks((current) => ({ ...current, "Human Loop 已处理": true }));
          }
          setLabelEntityNotice({
            status: "success",
            title: terminal ? "高风险双盲审核已形成终态" : progress.status === "awaiting-adjudication" ? "双盲分歧，等待独立仲裁" : "密封结论已提交",
            detail: `${aggregate.aggregate_id} · ${progress.status} · ${progress.received_reviews ?? 1}/2 · child trace ${labelShortTrace(progress.trace_id)} · root ${labelShortTrace(labelRootTraceId)}。`
          });
          setActionFeedback(
            terminal
              ? `${label}已由双盲共识或独立仲裁落为 ${progress.status}。`
              : `${label}已密封；当前用户不能查看另一审核人的结论，也不能用通用 decision 绕过。`
          );
          return terminal;
        }
        const receipt = await submitHumanReviewDecision(task.id, {
          decision,
          note: detail,
          ...(decision === "modified" ? { changes: [{ target_type: LABEL_DEMO_MODE ? "label_candidate" as const : "label_aggregate" as const, target_id: candidate.id, fields: { value: humanChangeDraft.after, reason: humanChangeDraft.reason } }] } : {})
        }, { correlationId: labelRootTraceId });
        const readback = await getHumanReviewTask(task.id, { correlationId: labelRootTraceId });
        setReviewState(state, candidate.id);
        setReleaseChecks((current) => ({ ...current, "Human Loop 已处理": state !== "待人工" }));
        setLabelEntityNotice({ status: "success", title: "人审决策已写入并读回", detail: `${task.id} · ${String(readback.data.status ?? receipt.data.status)} · trace ${labelShortTrace(receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id)}。` });
        setActionFeedback(`${task.id} ${label}：${detail}。状态已从 BFF 读回，不覆盖线上标签版本。`);
        return true;
      } catch (error) {
        setLabelEntityNotice({ status: "error", title: "人审决策失败，可重试", detail: `${error instanceof Error ? error.message : "unknown error"}。页面状态未变更。` });
        return false;
      } finally {
        setLabelEntityAction(null);
      }
    };

  const selectLabelCandidate = (candidateId: string) => {
      setSelectedCandidateId(candidateId);
      const task = reviewTasks.find((item) => item.aggregateId === candidateId);
      if (task) setSelectedReviewId(task.id);
    };

  const toggleLabelCandidateSelection = (candidateId: string) => {
      setSelectedCandidateIds((current) => current.includes(candidateId)
        ? current.filter((item) => item !== candidateId)
        : [...current, candidateId]
      );
      setBatchDecisionReceipt(null);
    };

  const selectLabelReviewTask = (task: LabelReviewTaskView) => {
      setSelectedReviewId(task.id);
      setSelectedCandidateId(task.aggregateId);
    };

  const moveLabelReviewSelection = (offset: -1 | 1) => {
      if (reviewTasks.length === 0) return;
      const currentIndex = Math.max(0, reviewTasks.findIndex((task) => task.id === activeReviewTask.id));
      const nextIndex = Math.min(reviewTasks.length - 1, Math.max(0, currentIndex + offset));
      selectLabelReviewTask(reviewTasks[nextIndex]);
    };

  const selectReviewDraft = (state: LabelReviewState) => {
      setReviewDraftStatesByCandidateId((current) => ({ ...current, [activeCandidate.id]: state }));
    };

  const saveReviewAndNext = async () => {
      const action = reviewDecisionActions.find((item) => item.state === reviewDraftState);
      if (!action || reviewDraftState === "待人工") {
        setLabelEntityNotice({ status: "error", title: "请选择人工决策", detail: "可按 A 接受、M 修改后接受、R 拒绝，再保存并下一条。" });
        return;
      }
      const saved = await applyReviewDecision(
        action.state,
        action.label,
        `${action.detail} 处理人：${reviewInputs.assignee}；说明：${reviewInputs.note}`
      );
      if (saved) moveLabelReviewSelection(1);
    };

  const handleLabelReviewKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const target = event.target;
      if (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        (target instanceof HTMLElement && target.isContentEditable)
      ) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveLabelReviewSelection(1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        moveLabelReviewSelection(-1);
        return;
      }
      const shortcutState = ({ a: "已接受", m: "已修改", r: "已拒绝" } as const)[event.key.toLowerCase() as "a" | "m" | "r"];
      if (shortcutState) {
        event.preventDefault();
        selectReviewDraft(shortcutState);
      }
    };

  return {
    runScenarioAgent,
    ensureLabelHumanReviewTask,
    applyReviewDecision,
    selectLabelCandidate,
    toggleLabelCandidateSelection,
    selectLabelReviewTask,
    moveLabelReviewSelection,
    selectReviewDraft,
    saveReviewAndNext,
    handleLabelReviewKeyDown
  };
}

export type LabelsReviewActions = ReturnType<typeof buildLabelsReviewActions>;
