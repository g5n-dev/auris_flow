import type { CanvasActionScope } from "./canvasActionScope";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import type { CanvasExecutionActions } from "./buildCanvasExecutionActions";
import type { CanvasExperimentStateActions } from "./buildCanvasExperimentStateActions";
import { createControlledExperiment } from "../../../api/client";

export function buildCanvasExperimentCreateActions(
  scope: CanvasActionScope & CanvasDraftModelActions & CanvasExecutionActions & CanvasExperimentStateActions
) {
  const {
    applyControlledExperiment,
    declaredTaskTypeId,
    experimentCandidateTaskVersion,
    experimentConfigDraft,
    experimentControlTaskVersion,
    experimentTreatmentPreview,
    pushRunHistory,
    sceneBinding,
    sceneManifest,
    selectedExperimentMetric,
    setCanvasNotice,
    setExperimentActionPending,
    shortTrace
  } = scope;

  const createTaskControlledExperiment = async () => {
    if (!sceneBinding || !sceneManifest) {
      setCanvasNotice({
        status: "error",
        title: "实验创建被阻断",
        detail: "当前项目没有已发布 SceneProfile，不能冻结实验指标与版本口径。"
      });
      return;
    }
    const controlTaskVersionId = String(experimentControlTaskVersion?.task_version_id ?? "");
    const candidateTaskVersionId = String(experimentCandidateTaskVersion?.task_version_id ?? "");
    if (!controlTaskVersionId || !candidateTaskVersionId) {
      setCanvasNotice({
        status: "error",
        title: "缺少可实验任务版本",
        detail: "需要同一任务类型下一个 published 对照版本和一个 experiment_ready/validated 候选版本。"
      });
      return;
    }
    if (!experimentTreatmentPreview.compatible) {
      setCanvasNotice({
        status: "error",
        title: "实验变量未隔离",
        detail: `${experimentTreatmentPreview.status}：声明 ${experimentConfigDraft.variantDimension}，实际差异 ${experimentTreatmentPreview.changedDimensions.join(" + ") || "无"}。请更换版本或改为组合版本包实验。`
      });
      return;
    }
    const declaredMetric = sceneManifest.metrics.find((metric) => metric.metric_key === selectedExperimentMetric.key);
    if (!declaredMetric) {
      setCanvasNotice({
        status: "error",
        title: "实验指标未被场景声明",
        detail: `${selectedExperimentMetric.key} 不属于当前 SceneProfile，不能作为发布决策事实。`
      });
      return;
    }
    const guardrail = sceneManifest.metrics.find((metric) =>
      metric.metric_key !== declaredMetric.metric_key
      && /risk|error|failure|latency|backlog|conflict|crosstalk/i.test(metric.metric_key)
    );
    const candidateAllocationPpm = Math.min(
      950_000,
      Math.max(50_000, Math.round(experimentConfigDraft.candidateAllocationPpm))
    );
    const minSampleSizePerArm = Math.min(
      10_000_000,
      Math.max(3, Math.round(experimentConfigDraft.minSampleSizePerArm))
    );
    setExperimentActionPending("create");
    setCanvasNotice({
      status: "pending",
      title: "正在创建受控实验",
      detail: "冻结 SceneProfile、对照/候选 TaskVersion、分流比例、主指标与守护指标。"
    });
    try {
      const created = await createControlledExperiment({
        name: `${sceneManifest.display_name} · ${declaredMetric.display_name}实验`,
        experiment_kind: "task_version",
        variant_dimension: experimentConfigDraft.variantDimension,
        task_type_id: declaredTaskTypeId,
        hypothesis: `候选任务版本在不放大场景风险的前提下改善${declaredMetric.display_name}`,
        allocation_unit: experimentConfigDraft.allocationUnit,
        arms: [
          { arm_key: "control", task_version_id: controlTaskVersionId, allocation_ppm: 1_000_000 - candidateAllocationPpm },
          { arm_key: "candidate", task_version_id: candidateTaskVersionId, allocation_ppm: candidateAllocationPpm }
        ],
        primary_metric: {
          metric_key: declaredMetric.metric_key,
          direction: /risk|error|failure|latency|backlog|conflict/i.test(declaredMetric.metric_key) ? "decrease" : "increase",
          minimum_effect: 0.02
        },
        guardrails: guardrail ? [{
          metric_key: guardrail.metric_key,
          direction: /risk|error|failure|latency|backlog|conflict|crosstalk/i.test(guardrail.metric_key) ? "decrease" : "increase",
          maximum_regression: 0.02
        }] : [],
        min_sample_size_per_arm: minSampleSizePerArm,
        confidence_level: experimentConfigDraft.confidenceLevel
      }, { correlationId: sceneBinding.trace_id });
      applyControlledExperiment(created.data);
      setCanvasNotice({
        status: "success",
        title: "受控实验草稿已创建",
        detail: `${created.data.experiment_id} 已冻结 ${created.data.variant_dimension} 变量和差异 SHA ${created.data.variant_diff_sha256.slice(0, 12)}，候选流量 ${candidateAllocationPpm / 10_000}%，每臂至少 ${minSampleSizePerArm} 个样本。`
      });
      pushRunHistory(`Experiment · ${created.data.experiment_id}`, `草稿 / ${shortTrace(created.data.trace_id)}`);
    } catch (error) {
      setCanvasNotice({
        status: "error",
        title: "受控实验创建失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
    } finally {
      setExperimentActionPending(null);
    }
  };

  return { createTaskControlledExperiment };
}

export type CanvasExperimentCreateActions = ReturnType<typeof buildCanvasExperimentCreateActions>;
