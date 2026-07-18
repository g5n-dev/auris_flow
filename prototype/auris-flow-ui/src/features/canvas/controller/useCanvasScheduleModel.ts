import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import { getTaskVersion } from "../../../api/client";
import { scheduleFrequencyCronMap } from "../scheduleModel";
import {
  canvasRunConfigDescriptors,
  canvasSchedulePlanDescriptors,
  canvasScheduleTriggerDescriptors
} from "../fixtures/viewDescriptors";
import type { ScheduleControl, TaskScheduleMode } from "../types";
import { Activity, Play, Radio, RotateCcw } from "lucide-react";
import type { ComponentType } from "react";
import { useEffect } from "react";
import { buildCanvasRunConfig, buildCanvasSchedulePlan } from "./buildCanvasScheduleDescriptors";

export function useCanvasScheduleModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel) {
  const { activeFlowStage, activeIntent, activeIntentKey, activeMappingSuggestions, activePartitionKey, contextProjectId, contextTenantId, dagsterRunDraft, draftState, experimentMode, focus, rememberTaskVersionId, sceneBinding, scheduleConfigs, scheduleMode, selectedCanvasVariant, selectedExperimentMetric, selectedMappingId, selectedTaskType, setActiveTab, setAsrHotwordVersionId, setCanvasNotice, setDraftState, setRecoveredTaskVersion, setSelectedMappingId, setSelectedNodeId } = scope;
  const activeScheduleConfig = scheduleConfigs[scheduleMode];

  const activeRunConfig = buildCanvasRunConfig(canvasRunConfigDescriptors, [
    selectedTaskType.key,
    selectedCanvasVariant.key,
    activeFlowStage.key,
    experimentMode,
    contextTenantId || "未绑定",
    contextProjectId || "未绑定",
    sceneBinding?.scene_profile_id ?? "未绑定",
    sceneBinding?.scene_profile_version_id ?? "未绑定",
    sceneBinding?.manifest_sha256 ?? "未绑定",
    draftState === "已发布" ? "v3" : "draft-v3",
    dagsterRunDraft.partitionKey || activePartitionKey,
    scheduleMode,
    selectedExperimentMetric.key,
    selectedExperimentMetric.window,
    "",
    dagsterRunDraft.assetSelection,
    dagsterRunDraft.runTags,
    dagsterRunDraft.maxRetries,
    dagsterRunDraft.concurrencyLimit,
    activeIntentKey === "review" ? "login-risk-review" : "default-review",
    dagsterRunDraft.materializationMode,
    dagsterRunDraft.jobName
  ]);

  const scheduleModes: TaskScheduleMode[] = ["定时运行", "手动运行", "数据到达触发", "一次性回填"];

  const activeSchedulePlan = buildCanvasSchedulePlan(canvasSchedulePlanDescriptors, {
    scheduleConfigs,
    activeIntentTaskId: activeIntent.taskId,
    selectedCanvasVariantKey: selectedCanvasVariant.key,
    activePartitionKey
  });

  const activeSchedule = activeSchedulePlan[scheduleMode];

  const scheduleTriggerIcons: Record<string, ComponentType<{ size?: number }>> = {
    Activity,
    Play,
    Radio,
    RotateCcw
  };
  const scheduleTriggerMeta = Object.fromEntries(
    (Object.entries(canvasScheduleTriggerDescriptors) as Array<[
      TaskScheduleMode,
      (typeof canvasScheduleTriggerDescriptors)[TaskScheduleMode]
    ]>).map(([mode, descriptor]) => {
      const { iconKey, ...staticFields } = descriptor;
      const controls = descriptor.controls.map((control): ScheduleControl => ({
        ...control,
        ...(control.options ? { options: [...control.options] } : {}),
        ...(mode === "定时运行" && control.key === "frequency"
          ? { options: Object.keys(scheduleFrequencyCronMap) }
          : {}),
        ...(mode === "手动运行" && control.key === "runScope"
          ? { placeholder: activePartitionKey }
          : {})
      }));
      const primary = mode === "定时运行"
        ? `cron ${scheduleConfigs["定时运行"].cron}`
        : mode === "数据到达触发"
          ? scheduleConfigs["数据到达触发"].eventSources
          : mode === "一次性回填"
            ? `${scheduleConfigs["一次性回填"].startDate}~${scheduleConfigs["一次性回填"].endDate}`
            : descriptor.primary ?? "";
      return [mode, {
        ...staticFields,
        primary,
        controls,
        icon: scheduleTriggerIcons[iconKey]
      }];
    })
  ) as Record<
    TaskScheduleMode,
    {
      icon: ComponentType<{ size?: number }>;
      label: string;
      title: string;
      description: string;
      entry: string;
      primary: string;
      when: string;
      controls: ScheduleControl[];
    }
  >;

  const activeTriggerMeta = scheduleTriggerMeta[scheduleMode];

  const ActiveScheduleIcon = activeTriggerMeta.icon;

  const activeScheduleControls: ScheduleControl[] = activeTriggerMeta.controls;

  useEffect(() => {
      if (!activeMappingSuggestions.length) return;
      if (!activeMappingSuggestions.some((item) => item.id === selectedMappingId)) {
        setSelectedMappingId(activeMappingSuggestions[0].id);
      }
    }, [activeIntentKey, activeMappingSuggestions, selectedMappingId]);

  useEffect(() => {
      if (focus?.module !== "canvas" || focus.objectKind !== "canvasNode" || !focus.objectId) return;
      setSelectedNodeId(focus.objectId);
      setCanvasNotice({
        status: "success",
        title: `已定位任务节点：${focus.title ?? focus.objectId}`,
        detail: `${focus.origin?.label ?? "关联跳转"} → ${focus.objectId}，节点配置、字段映射和执行定义已同步。`
      });
    }, [focus?.module, focus?.objectKind, focus?.objectId, focus?.title]);

  useEffect(() => {
      if (focus?.module !== "canvas" || focus.objectKind !== "taskVersion" || !focus.objectId) return;
      let active = true;
      const taskVersionId = focus.objectId;
      setActiveTab("versions");
      setCanvasNotice({
        status: "pending",
        title: "正在恢复 TaskVersion",
        detail: `${taskVersionId} · 只读取真实草稿状态，不自动发布或切换生产。`
      });
      void getTaskVersion(taskVersionId)
        .then((response) => {
          if (!active) return;
          const taskVersion = response.data;
          const responseId = typeof taskVersion.task_version_id === "string"
            ? taskVersion.task_version_id
            : typeof taskVersion.id === "string"
              ? taskVersion.id
              : null;
          if (responseId !== taskVersionId) throw new Error(`TaskVersion 响应 ID 不匹配：${responseId ?? "missing"}`);
          const status = typeof taskVersion.status === "string" ? taskVersion.status.toLowerCase() : "unknown";
          const boundHotwordVersionId = typeof taskVersion.hotword_pack_version_id === "string"
            ? taskVersion.hotword_pack_version_id
            : taskVersion.audio_intelligence && typeof taskVersion.audio_intelligence === "object" && !Array.isArray(taskVersion.audio_intelligence)
              ? (taskVersion.audio_intelligence as Record<string, unknown>).hotword_pack_version_id
              : null;
          setRecoveredTaskVersion(taskVersion);
          rememberTaskVersionId(taskVersionId);
          if (typeof boundHotwordVersionId === "string") setAsrHotwordVersionId(boundHotwordVersionId);
          setDraftState(status === "published" ? "已发布" : status === "draft" ? "已保存" : "未保存");
          setCanvasNotice({
            status: status === "draft" || status === "published" ? "success" : "error",
            title: status === "published" ? "TaskVersion 已发布" : status === "draft" ? "TaskVersion 草稿已恢复" : "TaskVersion 状态不可发布",
            detail: `${taskVersionId} · ${status} · hotword_pack_version_id ${String(boundHotwordVersionId ?? "missing")} · Trace ${response.meta?.trace_id ?? "no-trace"}`
          });
        })
        .catch((error) => {
          if (!active) return;
          setRecoveredTaskVersion(null);
          rememberTaskVersionId(null);
          setDraftState("未保存");
          setCanvasNotice({
            status: "error",
            title: "TaskVersion 恢复失败",
            detail: error instanceof Error ? error.message : `${taskVersionId} 读取失败`
          });
        });
      return () => {
        active = false;
      };
    }, [focus?.module, focus?.objectKind, focus?.objectId, setActiveTab]);

  return {
    activeScheduleConfig,
    activeRunConfig,
    scheduleModes,
    activeSchedulePlan,
    activeSchedule,
    scheduleTriggerMeta,
    activeTriggerMeta,
    ActiveScheduleIcon,
    activeScheduleControls
  };
}

export type CanvasScheduleModel = ReturnType<typeof useCanvasScheduleModel>;
