import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { backendEvaluationMetricRows } from "../factViews";
import { promptEvalMetrics } from "../fixtures/governanceCatalog";
import type { EvaluationMetric, MetricVerdict } from "../types";

type BuildLabelsChangeModelScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel;

export function buildLabelsChangeModel(activeCandidate: BuildLabelsChangeModelScope["activeCandidate"], activeIntent: BuildLabelsChangeModelScope["activeIntent"], activeScenario: BuildLabelsChangeModelScope["activeScenario"], automationLevel: BuildLabelsChangeModelScope["automationLevel"], humanChangeDraft: BuildLabelsChangeModelScope["humanChangeDraft"], labelEvalRun: BuildLabelsChangeModelScope["labelEvalRun"], labelRootTraceId: BuildLabelsChangeModelScope["labelRootTraceId"], optimizationInputs: BuildLabelsChangeModelScope["optimizationInputs"], reviewInputs: BuildLabelsChangeModelScope["reviewInputs"], selectedChangeSource: BuildLabelsChangeModelScope["selectedChangeSource"], selectedExperimentMetric: BuildLabelsChangeModelScope["selectedExperimentMetric"]) {
  const agentImprovementRows = [
      {
        title: `补强 ${activeIntent.intent} 定义`,
        evidence: activeIntent.evidence,
        confidence: activeIntent.confidence,
        trace: activeCandidate.traceId,
        uplift: "F1 +0.08 / 人工接受率 +12.2pp",
        risk: activeIntent.risk,
        action: "写入候选 Prompt 与 LabelCandidate"
      },
      {
        title: "生成冲突规则候选",
        evidence: activeIntent.conflicts[0]?.detail ?? "低置信时只写候选，不自动回填。",
        confidence: Math.max(60, activeIntent.confidence - 7),
        trace: `tr-${activeIntent.key}-rule`,
        uplift: "冲突率 -2.7pp",
        risk: activeIntent.conflicts.length > 0 ? "中" : "低",
        action: "补充规则候选和 badcase"
      },
      {
        title: "自动生成评测集回流",
        evidence: `${activeScenario.output} → badcase 回归集`,
        confidence: Math.max(58, activeIntent.confidence - 12),
        trace: `tr-${activeIntent.key}-eval`,
        uplift: "漏报率 -4.7pp",
        risk: "中",
        action: "写 EvalDataset，不写线上标签"
      }
    ];

  const humanChangeRows = [
      {
        before: activeCandidate.value,
        after: humanChangeDraft.after,
        owner: reviewInputs.assignee,
        reason: humanChangeDraft.reason,
        override: humanChangeDraft.overrideAgent
      },
      {
        before: "置信阈值 0.72",
        after: `置信阈值 ${optimizationInputs.threshold}`,
        owner: reviewInputs.assignee,
        reason: "高风险金额标签需要更高阈值，低于阈值进入 Human Loop。",
        override: "是"
      }
    ];

  const changeSetRows = [
      {
        source: "Agent建议" as const,
        object: "PromptVersion",
        change: `${optimizationInputs.promptVersion} 增加四层标签输出与冲突原因`,
        impact: "候选抽取、JSON 合法率、Trace 可追踪"
      },
      {
        source: "Agent建议" as const,
        object: "LabelCandidate",
        change: `${activeCandidate.title} · ${activeCandidate.value}`,
        impact: activeCandidate.assetImpact
      },
      {
        source: "人工修改" as const,
        object: "HumanReviewTask",
        change: `${humanChangeRows[0].before} → ${humanChangeRows[0].after}`,
        impact: "发布门禁、人审日志、badcase 回流"
      },
      {
        source: "系统门禁" as const,
        object: "ReleaseGate",
        change: activeIntent.blockers[0] ?? "评测通过后允许灰度",
        impact: "阻断自动发布或转入灰度观察"
      }
    ];

  const visibleChangeSetRows = changeSetRows.filter((row) => selectedChangeSource === "全部" || row.source === selectedChangeSource);

  const evaluationMetricRows = LABEL_DEMO_MODE ? promptEvalMetrics : backendEvaluationMetricRows(labelEvalRun);

  const evaluationMetrics = evaluationMetricRows.map(([metric, current, candidate, delta, verdict], index): EvaluationMetric => {
      const metricVerdict = verdict as MetricVerdict;
      const attribution = ([
        "Prompt 修改：补充四层标签定义和冲突原因",
        "规则修改：金额冲突只写候选不覆盖线上",
        "人工修正：复核样本回写为黄金集",
        "样本回流：badcase 进入固定评测集",
        "阈值调整：低置信和串音污染进入 Human Loop"
      ] as const)[index % 5];
      const requiredAction =
        metric === "漏报率"
          ? "补充低置信 badcase，并把未命中样本送 Human Loop"
          : metric === "成本"
            ? "保持影子运行，确认增量成本与延迟预算"
            : metricVerdict === "通过"
              ? "可作为门禁通过证据"
              : "继续优化 Prompt、规则或样本集";
      const gateImpact =
        metricVerdict === "通过"
          ? `${metric} 支撑发布门禁，可进入灰度判断。`
          : metric === "漏报率"
            ? "生成门禁阻断：漏报率仍高于阈值，不能发布候选版本。"
            : metric === "成本"
              ? "生成门禁阻断：成本升高需要预算确认，当前只能影子运行。"
              : `生成门禁阻断：${metric} 未达到候选版本发布阈值。`;
      return {
        id: metric,
        metric,
        current,
        candidate,
        delta,
        verdict: metricVerdict,
        source: attribution,
        attribution,
        gateImpact,
        sample:
          metric === "漏报率"
            ? "EVT-122718 相似报价片段未被候选版本覆盖，需补负例和证据窗口。"
            : metric === "成本"
              ? "248 个样本平均推理成本上升，主要来自输出 Schema 与冲突解释字段。"
              : `${activeCandidate.id} / ${activeCandidate.title} 在固定评测集上改善。`,
        requiredAction,
        blocking: metricVerdict !== "通过"
      };
    });

  const effectAttributionRows = evaluationMetrics;

  const dagsterDraftRows = [
      ["job_name", optimizationInputs.jobName],
      ["partition_key", optimizationInputs.partitionKey],
      ["asset_selection", optimizationInputs.assetSelection],
      ["run_config", optimizationInputs.runConfig],
      [
        "tags",
        `${optimizationInputs.runTags}, automation_level=${automationLevel}, root_trace_id=${labelRootTraceId || "unlocked"}, shadow_only=${optimizationInputs.shadowOnly}`
      ]
    ] satisfies Array<[string, string]>;

  const emptyEvaluationMetric: EvaluationMetric = {
      id: "no-backend-eval-fact",
      metric: "等待后端 EvalRun 指标",
      current: "—",
      candidate: "—",
      delta: "—",
      verdict: "观察",
      source: "EvalRun",
      attribution: "非 demo 模式不展示静态评测值。",
      gateImpact: "尚无后端评测事实，不能据此判定发布门禁。",
      sample: "等待后端 EvalRun 返回 metrics。",
      requiredAction: labelEvalRun ? "刷新或检查 EvalRun 指标物化" : "先运行锁定影子评测",
      blocking: true
    };

  const selectedEvaluationMetric = evaluationMetrics.find((row) => row.metric === selectedExperimentMetric) ?? evaluationMetrics[0] ?? emptyEvaluationMetric;

  const selectedMetricRow = [
      selectedEvaluationMetric.metric,
      selectedEvaluationMetric.current,
      selectedEvaluationMetric.candidate,
      selectedEvaluationMetric.delta,
      selectedEvaluationMetric.verdict
    ] satisfies [string, string, string, string, string];

  return {
    agentImprovementRows,
    humanChangeRows,
    changeSetRows,
    visibleChangeSetRows,
    evaluationMetricRows,
    evaluationMetrics,
    effectAttributionRows,
    dagsterDraftRows,
    emptyEvaluationMetric,
    selectedEvaluationMetric,
    selectedMetricRow
  };
}

export type LabelsChangeModel = ReturnType<typeof buildLabelsChangeModel>;
