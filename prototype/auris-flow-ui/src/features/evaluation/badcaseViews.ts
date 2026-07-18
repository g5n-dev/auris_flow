import type { HotwordStatisticsItem } from "../../api/client";
import type { EvaluationBadcaseWorkflowItem } from "./types";

export const evaluationStatusFromHotwordBadcase = (status: unknown): EvaluationBadcaseWorkflowItem["status"] => {
  if (status === "pending-review") return "待人审";
  if (status === "pending-backflow") return "待回流";
  if (status === "in-regression") return "已入回归";
  return "待归因";
};

export const evaluationBadcaseFromApi = (raw: HotwordStatisticsItem): EvaluationBadcaseWorkflowItem => {
  const expectedCount = typeof raw.expected_count === "number" ? raw.expected_count : 0;
  const weightedErrors = typeof raw.weighted_error_count === "number" ? raw.weighted_error_count : 0;
  const priority = typeof raw.priority_score === "number" ? raw.priority_score : typeof raw.priority === "number" ? raw.priority : 0;
  const downstream = raw.downstream_impact;
  return {
    id: raw.badcase_id,
    capability: "asr-hotword",
    title: `${raw.standard_term ?? raw.canonical_term ?? raw.badcase_id} 热词${raw.error_type ?? "错误"}`,
    severity: priority >= 80 ? "高" : priority >= 50 ? "中" : "低",
    status: evaluationStatusFromHotwordBadcase(raw.status),
    source: typeof raw.evidence_ref === "string" ? raw.evidence_ref : "词级证据由后端恢复",
    rootCause: typeof raw.root_cause === "string" ? raw.root_cause : `${raw.error_type ?? "misrecognition"}：等待人工归因。`,
    fix: typeof raw.fix_suggestion === "string" ? raw.fix_suggestion : "确认后加入候选词包并运行固定评测集。",
    target: "ASR 热词候选版本 / shadow EvalRun",
    owner: typeof raw.owner === "string" ? raw.owner : "ASR 平台组",
    standardTerm: raw.standard_term ?? raw.canonical_term,
    recognizedText: typeof raw.recognized_text === "string" ? raw.recognized_text : "",
    errorType: raw.error_type,
    expectedCount,
    errorRate: typeof raw.error_rate === "number"
      ? raw.error_rate
      : expectedCount > 0
        ? Math.round((weightedErrors / expectedCount) * 1000) / 10
        : 0,
    evidenceLevel: raw.evidence_level,
    downstreamImpact: typeof downstream === "string" ? downstream : downstream ? JSON.stringify(downstream) : "待计算",
    priority,
    resourceVersion: typeof raw.resource_version === "number" ? raw.resource_version : undefined,
    rootTraceId: typeof raw.root_trace_id === "string" ? raw.root_trace_id : undefined
  };
};
