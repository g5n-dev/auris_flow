import { ShieldCheck } from "lucide-react";
import type { CanvasController } from "../../controller/useCanvasController";

export function ExperimentAnalysisSummary({ controller }: { controller: CanvasController }) {
  const snapshot = controller.latestExperimentSnapshot;
  if (!snapshot) return null;

  const formatStatistic = (value: number | null | undefined) => value === null || value === undefined
    ? "—"
    : Math.abs(value) <= 1
      ? `${(value * 100).toFixed(2)}%`
      : value.toFixed(3);
  const sampleProgress = (arm: "control" | "candidate") => Math.min(
    100,
    (snapshot.sample_sizes[arm] / Math.max(1, snapshot.min_sample_size_per_arm)) * 100
  );
  const ratioDiagnostic = snapshot.sample_ratio_diagnostic;
  const ratioPValues = [
    ratioDiagnostic?.assignment.p_value,
    ratioDiagnostic?.analysis_sample.p_value
  ].filter((value): value is number => value !== null && value !== undefined);
  const ratioPValue = ratioPValues.length ? Math.min(...ratioPValues) : null;
  const ratioLabel = !ratioDiagnostic
    ? "待诊断"
    : ratioDiagnostic.detected
      ? `异常 · p ${ratioPValue?.toExponential(2) ?? "—"}`
      : `通过 · A ${snapshot.assignment_counts?.control ?? 0} / B ${snapshot.assignment_counts?.candidate ?? 0}`;

  return (
    <div className="experiment-analysis-summary">
      <div className="experiment-sample-progress">
        <span><b>A 对照样本</b><em>{snapshot.sample_sizes.control} / {snapshot.min_sample_size_per_arm}</em></span>
        <i><span style={{ width: `${sampleProgress("control")}%` }} /></i>
      </div>
      <div className="experiment-sample-progress candidate">
        <span><b>B 候选样本</b><em>{snapshot.sample_sizes.candidate} / {snapshot.min_sample_size_per_arm}</em></span>
        <i><span style={{ width: `${sampleProgress("candidate")}%` }} /></i>
      </div>
      <div className="experiment-stat-facts">
        <span><b>差异区间</b><em>{formatStatistic(snapshot.primary_metric.confidence_low)} 至 {formatStatistic(snapshot.primary_metric.confidence_high)}</em></span>
        <span><b>p 值</b><em>{snapshot.primary_metric.p_value === null ? "—" : snapshot.primary_metric.p_value.toFixed(4)}</em></span>
        <span className={ratioDiagnostic?.detected ? "is-blocked" : undefined}><b>分流健康</b><em>{ratioLabel}</em></span>
        <span><b>事实来源</b><em>{snapshot.fact_source === "signed_task_run_completion" ? "签名运行回执" : snapshot.fact_source ?? "待采集"}</em></span>
        <span><b>可回放证据</b><em>{snapshot.source_run_count ?? 0} runs / {snapshot.completion_receipt_count ?? 0} receipts</em></span>
      </div>
      <div className="experiment-evidence-lock">
        <ShieldCheck size={15} />
        <span>计算器 {snapshot.calculator_engine ?? "auris.experiment.metric-engine/v2"}</span>
        <code>{snapshot.evidence_sha256.slice(0, 16)}</code>
      </div>
    </div>
  );
}
