import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import type { HotwordVersionRecovery } from "./useHotwordVersionRecovery";
import type { EvaluationRunActions } from "./buildEvaluationRunActions";
import type { EvaluationBadcaseActions } from "./buildEvaluationBadcaseActions";

type BuildHotwordGateModelScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions;

export function buildHotwordGateModel(hotwordEvalResult: BuildHotwordGateModelScope["hotwordEvalResult"]) {
  const hotwordMetricValue = (metrics: Record<string, number> | null, key: string) => metrics?.[key];

  const hotwordMetricPlaceholder = hotwordEvalResult.gatePassed === null ? "待 EvalRun" : "明细缺失";

  const hotwordMetricDisplay = (
      metrics: Record<string, number> | null,
      key: string,
      format: (value: number) => string = String
    ) => {
      const value = hotwordMetricValue(metrics, key);
      return value === undefined ? hotwordMetricPlaceholder : format(value);
    };

  const hotwordPercentDisplay = (metrics: Record<string, number> | null, key: string) =>
      hotwordMetricDisplay(metrics, key, (value) => `${Math.round(value * 10000) / 100}%`);

  const hotwordCerWerDisplay = (metrics: Record<string, number> | null) => metrics
      ? `${hotwordPercentDisplay(metrics, "cer")} / ${hotwordPercentDisplay(metrics, "wer")}`
      : hotwordMetricPlaceholder;

  const hotwordGateResultLabel = hotwordEvalResult.gatePassed === null
      ? "不可判定"
      : hotwordEvalResult.gatePassed
        ? "gate passed"
        : `blocked${hotwordEvalResult.blockedReasons.length ? `: ${hotwordEvalResult.blockedReasons.join(",")}` : ""}`;

  const hotwordGateRow = (label: string, baseline: string, candidate: string, threshold: string) => ({
      label,
      baseline,
      candidate,
      threshold,
      result: hotwordGateResultLabel
    });

  const hotwordGateRows = [
      hotwordGateRow("可信出现", hotwordMetricDisplay(hotwordEvalResult.baselineMetrics, "trusted_occurrences"), hotwordMetricDisplay(hotwordEvalResult.candidateMetrics, "trusted_occurrences"), "≥ 30 / 词 ≥ 3"),
      hotwordGateRow("热词召回", hotwordPercentDisplay(hotwordEvalResult.baselineMetrics, "recall_rate"), hotwordPercentDisplay(hotwordEvalResult.candidateMetrics, "recall_rate"), "提升 ≥ 3pp"),
      hotwordGateRow("误增强", hotwordPercentDisplay(hotwordEvalResult.baselineMetrics, "false_boost_rate"), hotwordPercentDisplay(hotwordEvalResult.candidateMetrics, "false_boost_rate"), "增幅 ≤ 0.5pp"),
      hotwordGateRow("全局 CER/WER", hotwordCerWerDisplay(hotwordEvalResult.baselineMetrics), hotwordCerWerDisplay(hotwordEvalResult.candidateMetrics), "退化 ≤ 0.2pp"),
      hotwordGateRow("下游 F1", hotwordPercentDisplay(hotwordEvalResult.baselineMetrics, "downstream_f1"), hotwordPercentDisplay(hotwordEvalResult.candidateMetrics, "downstream_f1"), "退化 ≤ 0.5pp"),
      hotwordGateRow("P95 延迟", hotwordMetricDisplay(hotwordEvalResult.baselineMetrics, "p95_latency_ms", (value) => `${value}ms`), hotwordMetricDisplay(hotwordEvalResult.candidateMetrics, "p95_latency_ms", (value) => `${value}ms`), "增幅 ≤ 5%"),
      hotwordGateRow("分钟成本", hotwordMetricDisplay(hotwordEvalResult.baselineMetrics, "cost_per_minute", (value) => `¥${value}`), hotwordMetricDisplay(hotwordEvalResult.candidateMetrics, "cost_per_minute", (value) => `¥${value}`), "增幅 ≤ 5%")
    ];

  return {
    hotwordMetricValue,
    hotwordMetricPlaceholder,
    hotwordMetricDisplay,
    hotwordPercentDisplay,
    hotwordCerWerDisplay,
    hotwordGateResultLabel,
    hotwordGateRow,
    hotwordGateRows
  };
}

export type HotwordGateModel = ReturnType<typeof buildHotwordGateModel>;
