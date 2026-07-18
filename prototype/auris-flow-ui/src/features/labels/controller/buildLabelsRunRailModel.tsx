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
import type { LabelsReviewActions } from "./buildLabelsReviewActions";
import type { LabelsPersistenceActions } from "./buildLabelsPersistenceActions";
import type { LabelsPromptActions } from "./buildLabelsPromptActions";
import type { LabelsEvaluationActions } from "./buildLabelsEvaluationActions";
import type { LabelsReleaseActions } from "./buildLabelsReleaseActions";
import type { LabelsCoreRenders } from "./buildLabelsCoreRenders";
import type { LabelsInputRenders } from "./buildLabelsInputRenders";
import type { LabelsDecisionRenders } from "./buildLabelsDecisionRenders";
import type { LabelsWorkbenchRenders } from "./buildLabelsWorkbenchRenders";
import type { LabelsContractRenders } from "./buildLabelsContractRenders";
import { backendRunFailed, backendRunSucceeded, normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import { isRecordValue } from "../../../shared/runtime/records";
import type { LabelCandidate } from "../types";

type BuildLabelsRunRailModelScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders & LabelsWorkbenchRenders & LabelsContractRenders;

export function buildLabelsRunRailModel(activeCandidate: BuildLabelsRunRailModelScope["activeCandidate"], activeIntent: BuildLabelsRunRailModelScope["activeIntent"], activeLayerCount: BuildLabelsRunRailModelScope["activeLayerCount"], activeScenario: BuildLabelsRunRailModelScope["activeScenario"], backendLabelVersionId: BuildLabelsRunRailModelScope["backendLabelVersionId"], backendReleaseDeployment: BuildLabelsRunRailModelScope["backendReleaseDeployment"], draftStatus: BuildLabelsRunRailModelScope["draftStatus"], emptyCandidate: BuildLabelsRunRailModelScope["emptyCandidate"], extractionState: BuildLabelsRunRailModelScope["extractionState"], gateFactPending: BuildLabelsRunRailModelScope["gateFactPending"], gateIsBlocked: BuildLabelsRunRailModelScope["gateIsBlocked"], hasAuthoritativeCandidate: BuildLabelsRunRailModelScope["hasAuthoritativeCandidate"], labelAggregates: BuildLabelsRunRailModelScope["labelAggregates"], labelAggregationBackendRun: BuildLabelsRunRailModelScope["labelAggregationBackendRun"], labelCandidates: BuildLabelsRunRailModelScope["labelCandidates"], labelEvalRequest: BuildLabelsRunRailModelScope["labelEvalRequest"], labelEvalRun: BuildLabelsRunRailModelScope["labelEvalRun"], labelEvalSucceeded: BuildLabelsRunRailModelScope["labelEvalSucceeded"], labelExtractionBackendRun: BuildLabelsRunRailModelScope["labelExtractionBackendRun"], labelFactReadError: BuildLabelsRunRailModelScope["labelFactReadError"], labelFactReadState: BuildLabelsRunRailModelScope["labelFactReadState"], labelObservations: BuildLabelsRunRailModelScope["labelObservations"], labelPublishRequest: BuildLabelsRunRailModelScope["labelPublishRequest"], labelRootTraceId: BuildLabelsRunRailModelScope["labelRootTraceId"], lockedLabelVersionId: BuildLabelsRunRailModelScope["lockedLabelVersionId"], releaseGateRows: BuildLabelsRunRailModelScope["releaseGateRows"], releaseInputs: BuildLabelsRunRailModelScope["releaseInputs"], reviewInputs: BuildLabelsRunRailModelScope["reviewInputs"], reviewState: BuildLabelsRunRailModelScope["reviewState"], reviewStatesByCandidateId: BuildLabelsRunRailModelScope["reviewStatesByCandidateId"], reviewTasks: BuildLabelsRunRailModelScope["reviewTasks"]) {
  type LabelGovernanceView = {
      key: string;
      title: string;
      value: string;
      detail: string;
      tone: "blue" | "green" | "amber" | "red" | "violet" | "teal";
      action: string;
    };

  type LabelCandidateDraft = LabelCandidate & {
      route: string;
      writeTarget: string;
    };

  type LabelReviewDecision = {
      id: string;
      title: string;
      status: string;
      detail: string;
      owner: string;
      evidence: string;
    };

  type LabelReleaseGateSummary = {
      key: string;
      label: string;
      state: string;
      detail: string;
      passed: boolean;
    };

  const governanceViews: LabelGovernanceView[] = [
      {
        key: "coverage",
        title: "场景覆盖",
        value: `${activeLayerCount}/6`,
        detail: `${activeScenario.name} · ${activeScenario.levels.join(" / ")}`,
        tone: "blue",
        action: "查看标签层级"
      },
      {
        key: "candidate",
        title: "候选版本",
        value: lockedLabelVersionId || "未保存",
        detail: labelFactReadState === "loading" ? "正在读取后端事实" : `${labelCandidates.length} 条后端候选 · ${draftStatus}`,
        tone: "teal",
        action: "写入候选"
      },
      {
        key: "review",
        title: "人审状态",
        value: hasAuthoritativeCandidate ? reviewState : "等待候选",
        detail: hasAuthoritativeCandidate ? `${reviewTasks.length} 项任务 · ${reviewInputs.assignee}` : "Observation/Aggregate 尚未物化",
        tone: !hasAuthoritativeCandidate || reviewState === "待人工" ? "amber" : "green",
        action: "处理队列"
      },
      {
        key: "release",
        title: "发布门禁",
        value: gateFactPending ? "待后端判断" : gateIsBlocked ? "阻断" : "可灰度",
        detail: gateFactPending ? "尚未创建 ReleaseDeployment Bundle" : `${releaseGateRows.filter(([, passed]) => !passed).length} 项阻断 · ${releaseInputs.traffic}%`,
        tone: gateFactPending ? "amber" : gateIsBlocked ? "red" : "green",
        action: "提交门禁"
      }
    ];

  const candidateDrafts: LabelCandidateDraft[] = labelCandidates.map((candidate) => ({
      ...candidate,
      route: `${activeScenario.source} → ${candidate.source} → ${candidate.action}`,
      writeTarget: `${lockedLabelVersionId || "未锁定 LabelVersion"} / ${candidate.assetImpact}`
    }));

  const activeCandidateDraft = candidateDrafts.find((candidate) => candidate.id === activeCandidate.id) ?? candidateDrafts[0] ?? {
      ...emptyCandidate,
      route: "等待后端事实",
      writeTarget: "未物化"
    };

  const reviewDecisionRows: LabelReviewDecision[] = reviewTasks.map((task) => ({
      id: task.id,
      title: task.title,
      status: reviewStatesByCandidateId[task.aggregateId] ?? labelCandidates.find((candidate) => candidate.id === task.aggregateId)?.humanState ?? "待人工",
      detail: task.detail,
      owner: reviewInputs.assignee,
      evidence: labelCandidates.find((candidate) => candidate.id === task.aggregateId)?.evidence ?? activeIntent.evidence
    }));

  const releaseGateSummaries: LabelReleaseGateSummary[] = releaseGateRows.map(([label, passed, detail]) => ({
      key: label,
      label,
      state: passed ? "通过" : "阻断",
      detail,
      passed
    }));

  type LabelRunRailStatus = "idle" | "pending" | "success" | "failed" | "blocked";

  const extractionRailStatus: LabelRunRailStatus = extractionState === "completed"
      ? "success"
      : extractionState === "failed"
        ? "failed"
        : extractionState === "running"
          ? "pending"
          : "idle";

  const aggregationRailStatus: LabelRunRailStatus = labelFactReadState === "loading"
      ? "pending"
      : labelFactReadState === "failed"
        ? "failed"
        : labelAggregates.length > 0
          ? "success"
          : labelFactReadState === "empty" || labelObservations.length > 0
            ? "blocked"
            : "idle";

  const aggregateAwaitingReview = labelAggregates.some((aggregate) => aggregate.status === "awaiting-review");

  const aggregateReviewTerminal = labelAggregates.length > 0 && labelAggregates.every((aggregate) => aggregate.status !== "awaiting-review");

  const humanRailStatus: LabelRunRailStatus = aggregateReviewTerminal || (hasAuthoritativeCandidate && reviewState !== "待人工")
      ? "success"
      : aggregateAwaitingReview
        ? "blocked"
        : "idle";

  const evaluationRailStatus: LabelRunRailStatus = !labelEvalRun
      ? "idle"
      : backendRunFailed(labelEvalRun.status)
        ? "failed"
        : backendRunSucceeded(labelEvalRun.status)
          ? "success"
          : "pending";

  const releaseRailStatus: LabelRunRailStatus = labelPublishRequest.status === "success"
      ? "success"
      : labelPublishRequest.status === "failed"
        ? "failed"
        : labelPublishRequest.status === "blocked"
          ? "blocked"
          : labelPublishRequest.status === "pending"
            ? "pending"
            : "idle";

  const releaseLifecycleStatus = normalizeBackendRunStatus(backendReleaseDeployment?.status ?? labelPublishRequest.backendStatus);

  const releaseMonitorMetrics = isRecordValue(backendReleaseDeployment?.raw.monitor_metrics)
      ? backendReleaseDeployment.raw.monitor_metrics
      : {};

  const monitorRailStatus: LabelRunRailStatus = ["rolled-back", "rolled_back"].includes(releaseLifecycleStatus)
      ? "failed"
      : releaseLifecycleStatus === "completed"
        ? "success"
        : ["shadowing", "gray-releasing", "monitoring"].includes(releaseLifecycleStatus)
          ? "pending"
          : "idle";

  const labelUnifiedTraceId = labelRootTraceId || "等待 LabelVersion root trace";

  const labelRunRailSteps: Array<{ label: string; status: LabelRunRailStatus; detail: string }> = [
      { label: "抽取", status: extractionRailStatus, detail: labelExtractionBackendRun ? `${labelExtractionBackendRun.id} · ${labelObservations.length} Observation` : "未创建 LabelExtractionRun" },
      { label: "聚合", status: aggregationRailStatus, detail: labelAggregationBackendRun ? `${labelAggregationBackendRun.aggregation_run_id} · ${labelAggregates.length} Aggregate` : labelFactReadError || "等待确定性聚合物化" },
      { label: "人审", status: humanRailStatus, detail: labelAggregates.length > 0 ? `${labelAggregates.filter((item) => item.status === "awaiting-review").length} 条待审` : "等待 Aggregate 分流" },
      { label: "评测", status: evaluationRailStatus, detail: labelEvalRun?.id ?? "未绑定 EvalRun" },
      { label: "发布", status: releaseRailStatus, detail: backendReleaseDeployment ? `${backendReleaseDeployment.id} · ${releaseLifecycleStatus}` : "未提交 ReleaseDeployment" },
      { label: "监控", status: monitorRailStatus, detail: monitorRailStatus === "failed" ? "硬阈值触发，系统已自动回滚" : Object.keys(releaseMonitorMetrics).length > 0 ? `${Object.keys(releaseMonitorMetrics).length} 项线上指标` : monitorRailStatus === "success" ? "稳定窗口完成并已人工晋级" : monitorRailStatus === "pending" ? `持续读回 ${releaseLifecycleStatus}` : "灰度后启用系统监控" }
    ];

  const labelNextAction = labelPublishRequest.status === "blocked"
      ? { title: "修复发布阻断并重新评测", detail: labelPublishRequest.error ?? "后端门禁未放行" }
      : labelPublishRequest.status === "failed"
        ? ["rolled-back", "rolled_back"].includes(releaseLifecycleStatus)
          ? { title: "分析自动回滚样本并回流优化", detail: labelPublishRequest.error ?? "系统在线保护已停止灰度流量" }
          : { title: "重试失败的发布动作", detail: labelPublishRequest.error ?? "保留原用户意图和幂等键" }
        : labelPublishRequest.status === "pending"
          ? { title: "等待或刷新发布回执", detail: `${labelPublishRequest.runId ?? "运行创建中"} · 当前 ${labelPublishRequest.backendStatus ?? "pending"}` }
          : labelPublishRequest.backendStatus === "published"
            ? { title: "观察线上指标与回滚阈值", detail: `${backendLabelVersionId} 已发布，禁止继续显示候选成功态` }
            : !backendLabelVersionId
              ? { title: "保存候选版本", detail: "先取得后端 LabelVersion ID 与 root trace，再运行抽取" }
              : reviewState === "待人工"
                ? { title: "处理当前候选 Human Loop", detail: `${activeCandidate.id} 尚未形成终态决策` }
              : !labelEvalSucceeded
                ? { title: labelEvalRequest.status === "failed" ? "按原意图重试影子评测" : "运行并等待锁定影子评测", detail: `${backendLabelVersionId} 尚无 success/completed EvalRun` }
                : { title: "提交发布门禁", detail: `${labelEvalRun?.id ?? "EvalRun"} 已绑定，等待后端事实判断` };

  return {
    governanceViews,
    candidateDrafts,
    activeCandidateDraft,
    reviewDecisionRows,
    releaseGateSummaries,
    extractionRailStatus,
    aggregationRailStatus,
    aggregateAwaitingReview,
    aggregateReviewTerminal,
    humanRailStatus,
    evaluationRailStatus,
    releaseRailStatus,
    releaseLifecycleStatus,
    releaseMonitorMetrics,
    monitorRailStatus,
    labelUnifiedTraceId,
    labelRunRailSteps,
    labelNextAction
  };
}

export type LabelsRunRailModel = ReturnType<typeof buildLabelsRunRailModel>;
