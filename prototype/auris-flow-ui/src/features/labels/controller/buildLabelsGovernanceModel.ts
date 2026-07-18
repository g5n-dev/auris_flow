import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { PromptFieldKey } from "../../../shared/contracts/prompts";
import { normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { isRecordValue } from "../../../shared/runtime/records";
import type { LabelOptimizationRun, LabelReviewTaskView, ReleaseGateCheck } from "../types";

type BuildLabelsGovernanceModelScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel;

export function buildLabelsGovernanceModel(activeCandidate: BuildLabelsGovernanceModelScope["activeCandidate"], activeIntent: BuildLabelsGovernanceModelScope["activeIntent"], activeScenario: BuildLabelsGovernanceModelScope["activeScenario"], automationLevel: BuildLabelsGovernanceModelScope["automationLevel"], backendReleaseDeployment: BuildLabelsGovernanceModelScope["backendReleaseDeployment"], changeSetRows: BuildLabelsGovernanceModelScope["changeSetRows"], dagsterDraftRows: BuildLabelsGovernanceModelScope["dagsterDraftRows"], dagsterDraftState: BuildLabelsGovernanceModelScope["dagsterDraftState"], editableDraftTagName: BuildLabelsGovernanceModelScope["editableDraftTagName"], evaluationMetrics: BuildLabelsGovernanceModelScope["evaluationMetrics"], labelAgentBackendRun: BuildLabelsGovernanceModelScope["labelAgentBackendRun"], labelAggregates: BuildLabelsGovernanceModelScope["labelAggregates"], labelRootTraceId: BuildLabelsGovernanceModelScope["labelRootTraceId"], optimizationInputs: BuildLabelsGovernanceModelScope["optimizationInputs"], releaseChecks: BuildLabelsGovernanceModelScope["releaseChecks"], releaseDecision: BuildLabelsGovernanceModelScope["releaseDecision"], releaseInputs: BuildLabelsGovernanceModelScope["releaseInputs"], reviewState: BuildLabelsGovernanceModelScope["reviewState"], selectedCandidateIds: BuildLabelsGovernanceModelScope["selectedCandidateIds"], selectedEvaluationMetric: BuildLabelsGovernanceModelScope["selectedEvaluationMetric"], selectedReviewId: BuildLabelsGovernanceModelScope["selectedReviewId"]) {
  const promptFieldRows: Array<[PromptFieldKey, string, string]> = [
      ["system", "系统指令", "角色、约束和禁止覆盖线上标签"],
      ["definition", "标签定义", "标签域/组/标签/值动作的业务含义"],
      ["positive", "正例", "命中证据、上下文和单据引用"],
      ["negative", "负例", "排除边界、串音污染和语义不足"],
      ["schema", "JSON Schema", "结构化输出与字段合法性"],
      ["conflict", "冲突规则", "单据差异、版本差异和人审触发"],
      ["postprocess", "后处理", "阈值、候选写入、badcase 回流"]
    ];

  const reviewTaskCount = LABEL_DEMO_MODE
      ? 2
      : labelAggregates.filter((aggregate) => Boolean(aggregate.review_task_id)).length;

  const draftReleaseGateChecks = [
      {
        id: "eval",
        label: "效果评价通过",
        status: selectedEvaluationMetric.verdict,
        sourceMetric: selectedEvaluationMetric.metric,
        detail: `${selectedEvaluationMetric.metric} ${selectedEvaluationMetric.current} → ${selectedEvaluationMetric.candidate}，${selectedEvaluationMetric.gateImpact}`,
        requiredAction: selectedEvaluationMetric.requiredAction,
        blocking: selectedEvaluationMetric.blocking
      },
      {
        id: "human-loop",
        label: "Human Loop 完成",
        status: reviewState !== "待人工" ? "通过" : "阻断",
        sourceMetric: "HumanReviewTask",
        detail: `${reviewTaskCount} 项人审 / 当前 ${reviewState}`,
        requiredAction: "接受、修改或拒绝高风险候选后再提交门禁",
        blocking: reviewState === "待人工"
      },
      {
        id: "regression",
        label: "关键标签无回退",
        status: activeIntent.risk !== "高" || selectedEvaluationMetric.verdict === "通过" ? "通过" : "阻断",
        sourceMetric: activeIntent.intent,
        detail: `${activeIntent.intent} 风险 ${activeIntent.risk}，候选版本必须不降低关键标签表现`,
        requiredAction: "继续补样本或调整候选 Prompt",
        blocking: activeIntent.risk === "高" && selectedEvaluationMetric.verdict !== "通过"
      },
      {
        id: "assets",
        label: "影响资产确认",
        status: releaseChecks["影响资产已确认"] ? "通过" : "阻断",
        sourceMetric: "AssetCheck",
        detail: "事件标签资产、评测样本、业务洞察需要确认影响范围",
        requiredAction: "确认下游资产影响后再发布",
        blocking: !releaseChecks["影响资产已确认"]
      },
      {
        id: "rollback",
        label: "可回滚版本存在",
        status: Boolean(releaseInputs.rollback) && releaseChecks["回滚路径已确认"] ? "通过" : "阻断",
        sourceMetric: "ReleaseDecision",
        detail: `回滚版本 ${releaseInputs.rollback || "未配置"}`,
        requiredAction: "配置可回滚标签版本",
        blocking: !releaseInputs.rollback || !releaseChecks["回滚路径已确认"]
      }
    ] satisfies ReleaseGateCheck[];

  const backendReleaseStatus = normalizeBackendRunStatus(backendReleaseDeployment?.status);

  const backendBlockedReasons = Array.isArray(backendReleaseDeployment?.raw.blocked_reasons)
      ? backendReleaseDeployment.raw.blocked_reasons.filter(isRecordValue)
      : [];

  const backendReleaseGateChecks: ReleaseGateCheck[] = backendReleaseDeployment
      ? backendReleaseStatus === "blocked" || backendBlockedReasons.length > 0
        ? (backendBlockedReasons.length > 0 ? backendBlockedReasons : [{ code: "RELEASE_BLOCKED", message: "后端发布门禁阻断" }]).map((reason, index) => ({
            id: String(reason.code ?? `release-blocker-${index}`),
            label: String(reason.code ?? "发布门禁"),
            status: "阻断",
            sourceMetric: "ReleaseDeployment",
            detail: String(reason.message ?? "后端发布门禁未放行"),
            requiredAction: "按后端阻断原因修复后重新评测并提交 Bundle",
            blocking: true
          }))
        : [{
            id: "release-deployment-status",
            label: "ReleaseDeployment Bundle",
            status: ["shadowing", "gray-releasing", "monitoring", "completed"].includes(backendReleaseStatus) ? "通过" : "观察",
            sourceMetric: "ReleaseDeployment",
            detail: `${backendReleaseDeployment.id} · ${backendReleaseStatus}`,
            requiredAction: backendReleaseStatus === "completed" ? "持续监控" : "等待后端状态机下一阶段",
            blocking: !["shadowing", "gray-releasing", "monitoring", "completed"].includes(backendReleaseStatus)
          }]
      : [];

  const releaseGateChecks = LABEL_DEMO_MODE ? draftReleaseGateChecks : backendReleaseGateChecks;

  const releaseGateRows = releaseGateChecks.map((check) => [check.label, !check.blocking, check.detail] satisfies [string, boolean, string]);

  const gateFactPending = !LABEL_DEMO_MODE && !backendReleaseDeployment;

  const gateIsBlocked = LABEL_DEMO_MODE
      ? releaseGateRows.some(([, passed]) => !passed) || (automationLevel === "L4" && activeIntent.risk === "高")
      : backendReleaseStatus === "blocked" || backendReleaseStatus === "rolled-back" || backendReleaseStatus === "rolled_back";

  const labelOptimizationRun: LabelOptimizationRun = {
      runId: labelAgentBackendRun?.id ?? `draft-label-opt-${activeIntent.key}`,
      taskName: `${activeScenario.name} / ${activeIntent.intent}`,
      traceId: labelRootTraceId || "unlocked",
      input: optimizationInputs,
      changeSet: changeSetRows,
      metrics: evaluationMetrics,
      gateChecks: releaseGateChecks,
      decision: {
        label: gateFactPending ? "等待后端门禁事实" : gateIsBlocked ? "阻断发布" : releaseDecision,
        tone: gateFactPending ? "观察" : gateIsBlocked ? "阻断" : "通过",
        nextActions: gateFactPending ? ["提交 ReleaseDeployment Bundle"] : gateIsBlocked ? ["继续优化", "送 Human Loop", "保持影子运行"] : ["灰度发布", "发布候选版本", "同步输出资产"]
      },
      automationLevel,
      dagsterStatus: dagsterDraftState,
      dagsterRunDraft: dagsterDraftRows
    };

  const reviewTasks: LabelReviewTaskView[] = LABEL_DEMO_MODE
      ? [
          {
            id: "HR-1029",
            aggregateId: `LC-${activeIntent.key}-01`,
            title: activeIntent.conflicts[0]?.label ?? `${activeIntent.intent} 人工确认`,
            type: activeIntent.conflicts[0]?.source ?? "Agent 候选",
            detail: activeIntent.conflicts[0]?.detail ?? activeIntent.evidence,
            priority: activeIntent.risk,
            labelId: editableDraftTagName,
            policyVersionId: optimizationInputs.aggregationPolicyVersion
          },
          {
            id: "HR-1030",
            aggregateId: `LC-${activeIntent.key}-02`,
            title: `${editableDraftTagName} 发布前抽检`,
            type: "候选版本 v1.9.0-rc2",
            detail: `${activeIntent.scene} 影响资产和评测样本需要人工确认。`,
            priority: activeIntent.risk === "低" ? "中" : activeIntent.risk,
            labelId: editableDraftTagName,
            policyVersionId: optimizationInputs.aggregationPolicyVersion
          }
        ]
      : labelAggregates.flatMap((aggregate) => {
          if (!aggregate.review_task_id) return [];
          return [{
            id: aggregate.review_task_id,
            aggregateId: aggregate.aggregate_id,
            title: aggregate.label_id,
            type: aggregate.decision === "require-review" ? "聚合策略送审" : aggregate.decision,
            detail: aggregate.reason_codes.join(" / ") || `${aggregate.members?.length ?? 0} 个来源贡献`,
            priority: aggregate.risk_level === "high" ? "高" : aggregate.risk_level === "medium" ? "中" : "低",
            labelId: aggregate.label_id,
            policyVersionId: aggregate.policy_version_id
          } satisfies LabelReviewTaskView];
        });

  const emptyReviewTask: LabelReviewTaskView = {
      id: "unbound-review-task",
      aggregateId: activeCandidate.id,
      title: "等待候选级审核任务",
      type: "LabelAggregate.review_task_id",
      detail: "当前 Aggregate 未绑定专用审核任务，联调模式禁止页面自行创建通用任务。",
      priority: "中",
      labelId: activeCandidate.title,
      policyVersionId: "未绑定"
    };

  const activeReviewTask = (
      LABEL_DEMO_MODE
        ? reviewTasks.find((task) => task.id === selectedReviewId)
        : reviewTasks.find((task) => task.aggregateId === activeCandidate.id)
    ) ?? reviewTasks.find((task) => task.id === selectedReviewId) ?? emptyReviewTask;

  const hasBoundReviewTask = LABEL_DEMO_MODE || activeReviewTask.id !== emptyReviewTask.id;

  const selectedBatchAggregates = labelAggregates.filter((aggregate) => selectedCandidateIds.includes(aggregate.aggregate_id));

  const selectedBatchCohorts = new Set(
      selectedBatchAggregates.map((aggregate) => `${aggregate.label_id}::${aggregate.risk_level}::${aggregate.policy_version_id}`)
    );

  const batchPreflightReason = selectedCandidateIds.length < 2
      ? "至少选择 2 条候选"
      : selectedBatchAggregates.length !== selectedCandidateIds.length
        ? "仅已物化 Aggregate 可批量处理"
        : selectedBatchAggregates.some((aggregate) => aggregate.risk_level !== "low")
          ? "仅低风险候选可批量处理"
          : selectedBatchAggregates.some((aggregate) => aggregate.status !== "awaiting-review" || !aggregate.review_task_id)
            ? "候选必须处于待审且绑定专用任务"
            : selectedBatchCohorts.size !== 1
              ? "所选候选必须同标签、同风险、同聚合策略"
              : "前端预检通过，最终资格由服务端逐项裁决";

  const batchPreflightPassed = selectedCandidateIds.length >= 2 &&
      selectedBatchAggregates.length === selectedCandidateIds.length &&
      selectedBatchAggregates.every((aggregate) =>
        aggregate.risk_level === "low" &&
        aggregate.status === "awaiting-review" &&
        Boolean(aggregate.review_task_id)
      ) &&
      selectedBatchCohorts.size === 1;

  const reviewDecisionActions: Array<{ state: typeof reviewState; label: string; detail: string }> = [
      { state: "已接受", label: "接受候选", detail: "人工确认候选标签正确，写入候选版本并放行影子评测。" },
      { state: "已修改", label: "修改后接受", detail: "人工修正标签名称、层级或规则，再写入候选版本和 badcase。" },
      { state: "已拒绝", label: "拒绝候选", detail: "候选标签不成立，保留原因并阻断发布。" }
    ];

  return {
    promptFieldRows,
    reviewTaskCount,
    draftReleaseGateChecks,
    backendReleaseStatus,
    backendBlockedReasons,
    backendReleaseGateChecks,
    releaseGateChecks,
    releaseGateRows,
    gateFactPending,
    gateIsBlocked,
    labelOptimizationRun,
    reviewTasks,
    emptyReviewTask,
    activeReviewTask,
    hasBoundReviewTask,
    selectedBatchAggregates,
    selectedBatchCohorts,
    batchPreflightReason,
    batchPreflightPassed,
    reviewDecisionActions
  };
}

export type LabelsGovernanceModel = ReturnType<typeof buildLabelsGovernanceModel>;
