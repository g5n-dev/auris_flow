import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import { scheduleFrequencyCronMap } from "../scheduleModel";
import type { DagsterRunDraft, TaskDraftValidation, TaskDraftValidationItem, TaskScheduleMode } from "../types";
import {
  canvasDagsterCompatibilityDescriptors,
  canvasScheduleOutputSinkDescriptors
} from "../fixtures/viewDescriptors";
import { buildCanvasDagsterCompatibilityRows, cloneCanvasRows } from "./buildCanvasScheduleDescriptors";

export function buildCanvasExecutionPlan(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel) {
  const { activeFlowStage, activeIntent, activeIntentKey, activePartitionKey, activeRunConfig, activeRunKey, activeSchedule, appliedMappingCount, backfillConfirmed, confirmedMappingCount, contextProjectId, contextTenantId, controlledExperiment, dagsterRunDraft, draftState, experimentMode, mappingTotal, markTaskDraftDirty, metricDraftState, pendingMappingCount, sceneBinding, scheduleConfigs, scheduleMode, selectedCanvasVariant, selectedExperimentMetric, selectedTaskType, setDagsterRunDraft, setDrawerTab, setExecutionState, setNodeLibraryOpen, setScheduleConfigs, setSelectedNodeId } = scope;
  const updateScheduleConfig = (key: string, value: string) => {
      setScheduleConfigs((current) => {
        const nextModeConfig = { ...current[scheduleMode], [key]: value };
        if (scheduleMode === "定时运行" && key === "frequency" && scheduleFrequencyCronMap[value]) {
          nextModeConfig.cron = scheduleFrequencyCronMap[value];
        }
        return { ...current, [scheduleMode]: nextModeConfig };
      });
      markTaskDraftDirty();
    };

  const updateDagsterRunDraft = (key: keyof DagsterRunDraft, value: string) => {
      setDagsterRunDraft((current) => ({ ...current, [key]: value }));
      markTaskDraftDirty();
    };

  const syncDagsterInputsFromCanvas = () => {
      setDagsterRunDraft((current) => ({
        ...current,
        jobName: `${selectedTaskType.defaultCanvas}_job`,
        partitionKey: current.partitionKey || activePartitionKey,
        assetSelection: activeFlowStage.output || current.assetSelection,
        runTags: `tenant_id=${contextTenantId || "unbound"}, project_id=${contextProjectId || "unbound"}, scene_profile_version_id=${sceneBinding?.scene_profile_version_id ?? "unbound"}, canvas_variant=${selectedCanvasVariant.key}, flow_stage=${activeFlowStage.key}, schedule_mode=${scheduleMode}`,
        runConfigJson: `{
    "ops": {
      "${activeFlowStage.key}": {
        "config": {
          "input_assets": "${activeFlowStage.product}",
          "output_asset": "${activeFlowStage.output}",
          "experiment_mode": "${experimentMode}",
          "human_loop": "${activeIntentKey === "review" ? "login-risk-review" : "default-review"}"
        }
      }
    }
  }`,
        materializationMode: `${activeFlowStage.dagsterObject} + 质量检查`,
        reason: `${activeFlowStage.product} / ${selectedCanvasVariant.name}`
      }));
      markTaskDraftDirty();
    };

  const generateDagsterRunRequest = () => {
      setExecutionState("queued");
      setSelectedNodeId("dagster");
      setDrawerTab("logs");
      setNodeLibraryOpen(false);
      markTaskDraftDirty();
    };

  const dagsterCompatibilityRows = buildCanvasDagsterCompatibilityRows(
    canvasDagsterCompatibilityDescriptors,
    {
      scheduleConfigs,
      activeIntentTaskId: activeIntent.taskId,
      selectedCanvasVariantKey: selectedCanvasVariant.key,
      activePartitionKey
    }
  );

  const activeDagsterCompatibilityRows = dagsterCompatibilityRows[scheduleMode];

  const scheduleOutputSinks = cloneCanvasRows(canvasScheduleOutputSinkDescriptors);

  const activeSchedulePartitionKey = activeSchedule.partition || activePartitionKey;

  const activeScheduleTriggerTag: Record<TaskScheduleMode, string> = {
      定时运行: "schedule",
      手动运行: "manual",
      数据到达触发: "sensor",
      一次性回填: "backfill"
    };

  const activeScheduleRunTags = [
      `tenant_id=${contextTenantId || "unbound"}`,
      `project_id=${contextProjectId || "unbound"}`,
      `scene_profile_version_id=${sceneBinding?.scene_profile_version_id ?? "unbound"}`,
      `trigger=${activeScheduleTriggerTag[scheduleMode]}`,
      `schedule_mode=${scheduleMode}`,
      `dagster_object=${activeSchedule.dagsterObject}`,
      `canvas_variant=${selectedCanvasVariant.key}`,
      `flow_stage=${activeFlowStage.key}`,
      `primary_metric=${selectedExperimentMetric.key}`,
      ...(controlledExperiment?.status === "running"
        ? [
            `experiment_id=${controlledExperiment.experiment_id}`,
            `experiment_design_sha256=${controlledExperiment.design_sha256}`
          ]
        : [])
    ].join(", ");

  const activeRunConfigPreview: Array<[string, string]> = activeRunConfig.map(([key, value]) => {
      if (key === "partition_key") return [key, activeSchedulePartitionKey];
      if (key === "run_tags") return [key, activeScheduleRunTags];
      if (key === "schedule_mode") return [key, scheduleMode];
      return [key, value];
    });

  const validationItems: TaskDraftValidationItem[] = [
      draftState === "未保存"
        ? {
            key: "draft-state",
            label: "草稿未保存",
            detail: "发布前必须先保存任务草稿、调度和指标闸门。",
            severity: "blocker"
          }
        : {
            key: "draft-state",
            label: "草稿已保存",
            detail: `${draftState} · 当前任务版本可以进入发布门禁校验。`,
            severity: "pass"
          },
      scheduleMode === "一次性回填" && !backfillConfirmed
        ? {
            key: "backfill-confirm",
            label: "Backfill 未人工确认",
            detail: `${activeSchedulePartitionKey} 仍需确认并发、复算范围和失败重试策略。`,
            severity: "blocker"
          }
        : {
            key: "backfill-confirm",
            label: scheduleMode === "一次性回填" ? "Backfill 已确认" : "当前触发无需 Backfill 确认",
            detail: `${activeSchedule.dagsterObject} / ${activeSchedule.definition} 已有可追踪分区口径。`,
            severity: "pass"
          },
      metricDraftState === "发布闸门"
        ? {
            key: "primary-metric",
            label: "主指标已作为发布闸门",
            detail: `${selectedExperimentMetric.name} · ${selectedExperimentMetric.window} · ${selectedExperimentMetric.sql}`,
            severity: "pass"
          }
        : {
            key: "primary-metric",
            label: "主指标未进入发布闸门",
            detail: `当前状态为「${metricDraftState}」，只能观测，不能作为发布依据。`,
            severity: "blocker"
          },
      selectedExperimentMetric.guardrails.length
        ? {
            key: "guardrails",
            label: "守护指标已配置",
            detail: selectedExperimentMetric.guardrails.join(" / "),
            severity: "pass"
          }
        : {
            key: "guardrails",
            label: "缺少守护指标",
            detail: "发布前至少需要回写成功率、运行失败率或人审积压等守护指标。",
            severity: "blocker"
          },
      scheduleOutputSinks.some(([name]) => name === "platform_audio_callback")
        ? {
            key: "output-sink",
            label: "输出回写已配置",
            detail: "platform_audio_callback + processed_wav_asset 已保留 run_id、trace_id 和幂等键。",
            severity: "pass"
          }
        : {
            key: "output-sink",
            label: "输出回写缺失",
            detail: "缺少平台回调或处理后音频资产，不能发布。",
            severity: "blocker"
          },
      pendingMappingCount > 0
        ? {
            key: "mapping-confirm",
            label: `${pendingMappingCount} 条映射待确认`,
            detail: `${confirmedMappingCount} 条已确认、${appliedMappingCount} 条已应用；发布前建议处理高风险字段。`,
            severity: "warning"
          }
        : {
            key: "mapping-confirm",
            label: "字段映射已闭环",
            detail: `${appliedMappingCount}/${mappingTotal} 条映射已应用或确认。`,
            severity: "pass"
          }
    ];

  const taskDraftValidation: TaskDraftValidation = {
      canPublish: !validationItems.some((item) => item.severity === "blocker"),
      blockers: validationItems.filter((item) => item.severity === "blocker"),
      warnings: validationItems.filter((item) => item.severity === "warning"),
      passed: validationItems.filter((item) => item.severity === "pass"),
      summary: `${validationItems.filter((item) => item.severity === "blocker").length} 个阻断 / ${validationItems.filter((item) => item.severity === "warning").length} 个需确认 / ${validationItems.filter((item) => item.severity === "pass").length} 个通过`
    };

  const dagsterRuntimeRows = [
      ["业务入口", "POST /api/v1/task-runs", "BFF 创建运行请求并校验租户/项目权限。"],
      ["执行调用", "launchRun / submit_job_execution", "内部服务调用，不把底层编排 UI 暴露给业务用户。"],
      ["幂等键", activeRunKey, "同一租户、项目、门店、日期窗口不会重复生成运行。"],
      ["运行标签", "tenant_id, project_id, task_version, source=auris-flow", "用于运行记录、资产目录和审计日志联动。"],
      ["失败语义", "422 配置不完整 / 409 版本冲突 / 503 上游不可用", "BFF 用标准错误格式返回，页面转成配置建议。"]
    ];

  const dagsterRunRequestRows = [
      ["job_name", dagsterRunDraft.jobName],
      ["partition_key", dagsterRunDraft.partitionKey || activePartitionKey],
      ["asset_selection", dagsterRunDraft.assetSelection],
      ["tags", dagsterRunDraft.runTags],
      ["run_config", dagsterRunDraft.runConfigJson.replace(/\s+/g, " ").trim()],
      ["retry_policy", `${dagsterRunDraft.maxRetries} 次 / ${dagsterRunDraft.failurePolicy}`]
    ];

  const contractNodeMap: Record<string, string> = {
      平台会话: "platformAuth",
      租户门店: "tenantApi",
      员工工牌: "employeeApi",
      录音地址: "audioUrlApi",
      认证事件: "eventApi",
      复核任务: "dagster"
    };

  return {
    updateScheduleConfig,
    updateDagsterRunDraft,
    syncDagsterInputsFromCanvas,
    generateDagsterRunRequest,
    dagsterCompatibilityRows,
    activeDagsterCompatibilityRows,
    scheduleOutputSinks,
    activeSchedulePartitionKey,
    activeScheduleTriggerTag,
    activeScheduleRunTags,
    activeRunConfigPreview,
    validationItems,
    taskDraftValidation,
    dagsterRuntimeRows,
    dagsterRunRequestRows,
    contractNodeMap
  };
}

export type CanvasExecutionPlan = ReturnType<typeof buildCanvasExecutionPlan>;
