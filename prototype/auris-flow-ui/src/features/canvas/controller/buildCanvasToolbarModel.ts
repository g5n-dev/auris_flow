import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import type { CanvasNodeCollections } from "./buildCanvasNodeCollections";
import type { CanvasNodeContextModel } from "./buildCanvasNodeContextModel";
import type { CanvasNodeInteractions } from "./buildCanvasNodeInteractions";
import type { CanvasTaskDagModel } from "./buildCanvasTaskDagModel";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import type { CanvasExecutionActions } from "./buildCanvasExecutionActions";
import type { CanvasExperimentActions } from "./buildCanvasExperimentActions";
import type { CanvasRunModel } from "./buildCanvasRunModel";
import { experimentMetricObservations, taskExperimentArms, taskExperimentMetrics } from "../catalog";
import { BarChart3, Check, Database, Download, GitBranch, Link2, Pause, Play, Plus, RotateCcw, ShieldCheck, Sparkles, UploadCloud } from "lucide-react";
import type { ComponentType } from "react";

export function buildCanvasToolbarModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel & CanvasNodeCollections & CanvasNodeContextModel & CanvasNodeInteractions & CanvasTaskDagModel & CanvasDraftModelActions & CanvasExecutionActions & CanvasExperimentActions & CanvasRunModel) {
  const { activeSchedule, activeSchedulePartitionKey, activeTab, canvasAction, computeTaskExperimentMetrics, controlledExperiment, createTaskControlledExperiment, decideTaskControlledExperiment, demoMode, draftState, experimentActionPending, experimentLoading, generateExperimentMetricDraft, generateMappingSuggestions, isFlowTab, nodeLibraryOpen, openFieldMappingEditor, openInputSourceEditor, openOutputWritebackEditor, publishTaskVersion, retryTaskExperimentRelease, runTaskOnce, saveTaskDraft, setCanvasLevel, setCanvasNotice, setDrawerTab, setNodeLibraryOpen, setSelectedNodeId, startTaskControlledExperiment, syncSchedulePlan, taskPublishLabel, taskReleaseGate, updateExecutionState, validateDagsterCompatibility } = scope;
  const taskToolbarActions: Array<{
      key: string;
      label: string;
      icon: ComponentType<{ size?: number }>;
      action: () => void;
      active?: boolean;
      disabled?: boolean;
    }> = (() => {
      if (activeTab === "schedule") {
        return [
          { key: "save-schedule", label: canvasAction === "schedule" ? "保存中" : "保存调度", icon: Check, action: syncSchedulePlan, disabled: Boolean(canvasAction) },
          {
            key: "view-runrequest",
            label: "查看运行请求",
            icon: GitBranch,
            action: () => {
              setSelectedNodeId("dagster");
              setDrawerTab("plan");
              setCanvasNotice({
                status: "idle",
                title: "运行请求预览已同步",
                detail: `${activeSchedule.dagsterObject} / partition_key=${activeSchedulePartitionKey}。`
              });
            }
          },
          { key: "run-once", label: canvasAction === "run" ? "运行中" : "运行一次", icon: RotateCcw, action: runTaskOnce, disabled: Boolean(canvasAction) }
        ];
      }
      if (activeTab === "experiments") {
        if (experimentLoading) {
          return [{ key: "experiment-loading", label: "读取实验", icon: RotateCcw, action: () => undefined, disabled: true }];
        }
        if (!controlledExperiment) {
          return [
            { key: "experiment-create", label: experimentActionPending === "create" ? "创建中" : "创建实验", icon: Plus, action: createTaskControlledExperiment, disabled: Boolean(experimentActionPending) },
            { key: "generate-metric", label: "生成指标", icon: Sparkles, action: generateExperimentMetricDraft }
          ];
        }
        if (controlledExperiment.status === "draft") {
          return [
            { key: "experiment-start", label: experimentActionPending === "start" ? "启动中" : "启动实验", icon: Play, action: startTaskControlledExperiment, disabled: Boolean(experimentActionPending) }
          ];
        }
        if (controlledExperiment.status === "running") {
          return [
            { key: "experiment-run-sample", label: canvasAction === "run" ? "分流中" : "运行实验样本", icon: Play, action: runTaskOnce, disabled: Boolean(experimentActionPending) || Boolean(canvasAction) },
            { key: "experiment-compute", label: experimentActionPending === "compute" ? "计算中" : "计算指标", icon: BarChart3, action: computeTaskExperimentMetrics, disabled: Boolean(experimentActionPending) },
            { key: "experiment-pause", label: "暂停实验", icon: Pause, action: () => void decideTaskControlledExperiment("pause"), disabled: Boolean(experimentActionPending) }
          ];
        }
        if (controlledExperiment.status === "paused") {
          return [
            { key: "experiment-compute", label: experimentActionPending === "compute" ? "计算中" : "计算指标", icon: BarChart3, action: computeTaskExperimentMetrics, disabled: Boolean(experimentActionPending) },
            { key: "experiment-resume", label: "恢复实验", icon: Play, action: () => void decideTaskControlledExperiment("resume"), disabled: Boolean(experimentActionPending) }
          ];
        }
        if (controlledExperiment.decisions?.[0]?.decision === "promote_candidate") {
          return [
            taskReleaseGate
              ? { key: "experiment-release-gate", label: taskPublishLabel, icon: UploadCloud, action: publishTaskVersion, disabled: Boolean(canvasAction) || Boolean(experimentActionPending) }
              : { key: "experiment-release-retry", label: experimentActionPending === "release-gate" ? "创建中" : "创建发布门禁", icon: UploadCloud, action: retryTaskExperimentRelease, disabled: Boolean(experimentActionPending) }
          ];
        }
        return [{ key: "experiment-decided", label: "实验已决策", icon: Check, action: () => undefined, disabled: true }];
      }
      if (activeTab === "versions") {
        return [
          { key: "save-draft", label: "保存草稿", icon: Check, action: saveTaskDraft, disabled: Boolean(canvasAction) },
          { key: "validate-release", label: canvasAction === "validate" ? "校验中" : "执行校验", icon: ShieldCheck, action: validateDagsterCompatibility, disabled: Boolean(canvasAction) },
          { key: "publish-version", label: canvasAction === "publish" ? "处理中" : taskPublishLabel, icon: UploadCloud, action: publishTaskVersion, disabled: Boolean(canvasAction) || draftState === "已发布" }
        ];
      }
      if (activeTab === "io") {
        return [
          { key: "edit-input", label: "编辑输入源", icon: Database, action: () => openInputSourceEditor("platformAuth") },
          { key: "edit-mapping", label: "编辑字段映射", icon: Link2, action: openFieldMappingEditor },
          { key: "edit-output", label: "编辑输出回写", icon: Download, action: openOutputWritebackEditor }
        ];
      }
      if (activeTab === "runs") {
        return [
          { key: "validate-run", label: "重新校验", icon: ShieldCheck, action: validateDagsterCompatibility, disabled: Boolean(canvasAction) },
          { key: "run-once", label: canvasAction === "run" ? "运行中" : "运行一次", icon: RotateCcw, action: runTaskOnce, disabled: Boolean(canvasAction) },
          { key: "sync-output", label: "标记输出", icon: Check, action: () => updateExecutionState("success") }
        ];
      }
      if (isFlowTab) {
        return [
          {
            key: "add-node",
            label: "添加节点",
            icon: Plus,
            active: nodeLibraryOpen,
            disabled: Boolean(canvasAction),
            action: () => {
              setCanvasLevel("nodes");
              setNodeLibraryOpen((open) => !open);
            }
          },
          { key: "generate-mapping", label: canvasAction === "mapping" ? "生成中" : "生成映射", icon: Sparkles, action: generateMappingSuggestions, disabled: Boolean(canvasAction) },
          { key: "validate-flow", label: canvasAction === "validate" ? "校验中" : "校验", icon: ShieldCheck, action: validateDagsterCompatibility, disabled: Boolean(canvasAction) },
          { key: "run-flow", label: canvasAction === "run" ? "运行中" : "运行", icon: RotateCcw, action: runTaskOnce, disabled: Boolean(canvasAction) }
        ];
      }
      return [
        { key: "save-draft", label: "保存草稿", icon: Check, action: saveTaskDraft, disabled: Boolean(canvasAction) },
        { key: "validate-default", label: canvasAction === "validate" ? "校验中" : "执行校验", icon: ShieldCheck, action: validateDagsterCompatibility, disabled: Boolean(canvasAction) },
        { key: "run-default", label: canvasAction === "run" ? "运行中" : "运行一次", icon: RotateCcw, action: runTaskOnce, disabled: Boolean(canvasAction) }
      ];
    })();

  const taskActionFeedbackFor = (actionKey: string) =>
      /(save|schedule|run|validate|publish|mapping|experiment)/.test(actionKey) ? "p,s,e,d" : "s,e";

  const taskActionTitle = (action: (typeof taskToolbarActions)[number]) =>
      action.disabled
        ? `${action.label}正在处理，完成后可继续操作。`
        : `${action.label}会在上方显示执行状态和回执。`;

  const latestExperimentSnapshot = controlledExperiment?.latest_metric_snapshot ?? null;

  const formatExperimentValue = (value: number | null | undefined) => {
      if (value === null || value === undefined || !Number.isFinite(value)) return "—";
      return Math.abs(value) <= 1 ? `${(value * 100).toFixed(1)}%` : value.toFixed(2);
    };

  const formatExperimentDelta = (value: number | null | undefined) => {
      if (value === null || value === undefined || !Number.isFinite(value)) return "—";
      const formatted = Math.abs(value) <= 1 ? `${(value * 100).toFixed(1)}pp` : value.toFixed(2);
      return value > 0 ? `+${formatted}` : formatted;
    };

  const snapshotMetricRows: Array<[string, string, string, string, string]> = latestExperimentSnapshot
      ? [
          [
            latestExperimentSnapshot.primary_metric.display_name ?? latestExperimentSnapshot.primary_metric.metric_key,
            formatExperimentValue(latestExperimentSnapshot.primary_metric.control_value),
            formatExperimentValue(latestExperimentSnapshot.primary_metric.candidate_value),
            formatExperimentDelta(latestExperimentSnapshot.primary_metric.delta),
            latestExperimentSnapshot.primary_metric.status
          ],
          ...latestExperimentSnapshot.guardrails.map((guardrail): [string, string, string, string, string] => [
            guardrail.display_name ?? guardrail.metric_key,
            formatExperimentValue(guardrail.control_value),
            formatExperimentValue(guardrail.candidate_value),
            formatExperimentDelta(guardrail.delta),
            guardrail.status
          ])
        ]
      : demoMode && !controlledExperiment
        ? taskExperimentMetrics.map((row) => [...row] as [string, string, string, string, string])
        : [];

  const displayedExperimentArms = controlledExperiment
      ? controlledExperiment.arms.map((arm) => ({
          arm: arm.arm_key === "control" ? "A" : "B",
          canvas: arm.task_version_id,
          traffic: `${(arm.allocation_ppm / 10_000).toFixed(0)}%`,
          assignment: `${controlledExperiment.allocation_unit} · HMAC 稳定分桶 · SHA ${arm.task_version_snapshot_sha256?.slice(0, 8) ?? "待校验"}`,
          writeback: arm.arm_key === "control" ? "生产基线输出" : "候选输出带实验 trace",
          note: arm.arm_key === "control" ? "对照版本" : "候选版本"
        }))
      : taskExperimentArms;

  const displayedExperimentObservations = latestExperimentSnapshot
      ? [
          {
            label: "主指标差异",
            value: formatExperimentDelta(latestExperimentSnapshot.primary_metric.delta),
            compare: `B ${formatExperimentValue(latestExperimentSnapshot.primary_metric.candidate_value)} vs A ${formatExperimentValue(latestExperimentSnapshot.primary_metric.control_value)}`,
            state: latestExperimentSnapshot.primary_metric.status,
            tone: latestExperimentSnapshot.primary_metric.status === "pass" ? "good" : "warn",
            detail: `p=${latestExperimentSnapshot.primary_metric.p_value === null ? "—" : latestExperimentSnapshot.primary_metric.p_value.toFixed(4)}，指标口径 ${latestExperimentSnapshot.primary_metric.metric_key}。`,
            trend: [34, 41, 49, 56, 63, 70, 78, 86]
          },
          {
            label: "守护指标",
            value: latestExperimentSnapshot.guardrails[0]
              ? formatExperimentDelta(latestExperimentSnapshot.guardrails[0].delta)
              : "未配置",
            compare: latestExperimentSnapshot.guardrails[0]?.display_name ?? latestExperimentSnapshot.guardrails[0]?.metric_key ?? "无守护指标",
            state: latestExperimentSnapshot.guardrails[0]?.status ?? "not_configured",
            tone: latestExperimentSnapshot.guardrails.some((item) => item.status === "fail") ? "block" : "good",
            detail: "每个守护指标都使用同一实验分流和不可变结果事实计算。",
            trend: [82, 81, 80, 82, 84, 83, 85, 86]
          },
          {
            label: "样本进度",
            value: `${Math.min(latestExperimentSnapshot.sample_sizes.control, latestExperimentSnapshot.sample_sizes.candidate)}/${latestExperimentSnapshot.min_sample_size_per_arm}`,
            compare: `A ${latestExperimentSnapshot.sample_sizes.control} / B ${latestExperimentSnapshot.sample_sizes.candidate}`,
            state: latestExperimentSnapshot.verdict === "insufficient_sample"
              ? "继续采集"
              : latestExperimentSnapshot.verdict === "blocked_sample_ratio"
                ? "分流比例异常"
                : "满足门槛",
            tone: latestExperimentSnapshot.verdict === "blocked_sample_ratio"
              ? "block"
              : latestExperimentSnapshot.verdict === "insufficient_sample"
                ? "warn"
                : "good",
            detail: `按唯一 ${latestExperimentSnapshot.analysis_unit ?? controlledExperiment?.allocation_unit ?? "分流单元"} 聚合；${latestExperimentSnapshot.outcome_count ?? controlledExperiment?.counts.outcomes ?? 0} 条结果折算为 ${latestExperimentSnapshot.distinct_assignments ?? (latestExperimentSnapshot.sample_sizes.control + latestExperimentSnapshot.sample_sizes.candidate)} 个独立样本。`,
            trend: [18, 25, 33, 44, 57, 69, 82, 96]
          },
          {
            label: "决策建议",
            value: latestExperimentSnapshot.verdict,
            compare: `快照 v${latestExperimentSnapshot.snapshot_version}`,
            state: latestExperimentSnapshot.verdict === "promote" ? "可提交晋级" : "不可晋级",
            tone: latestExperimentSnapshot.verdict === "promote" ? "good" : latestExperimentSnapshot.verdict.startsWith("blocked_") ? "block" : "warn",
            detail: `证据 SHA ${latestExperimentSnapshot.evidence_sha256.slice(0, 12)}，终局决策由人工提交。`,
            trend: [52, 56, 60, 64, 68, 72, 78, 84]
          }
        ]
      : demoMode && !controlledExperiment
        ? experimentMetricObservations
        : [];

  return {
    taskToolbarActions,
    taskActionFeedbackFor,
    taskActionTitle,
    latestExperimentSnapshot,
    formatExperimentValue,
    formatExperimentDelta,
    snapshotMetricRows,
    displayedExperimentArms,
    displayedExperimentObservations
  };
}

export type CanvasToolbarModel = ReturnType<typeof buildCanvasToolbarModel>;
