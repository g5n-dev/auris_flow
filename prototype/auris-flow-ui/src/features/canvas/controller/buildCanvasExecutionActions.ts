import type { CanvasActionScope } from "./canvasActionScope";
import type { CanvasDraftModelActions } from "./buildCanvasDraftModelActions";
import type { ExecutionState } from "../types";
import type { CanvasReleaseActions } from "./buildCanvasReleaseActions";

export function buildCanvasExecutionActions(scope: CanvasActionScope & CanvasDraftModelActions & CanvasReleaseActions) {
  const { activeFlowStage, activeIntent, activeMappingSuggestions, activeSchedule, activeSchedulePartitionKey, activeScheduleRunTags, activeTriggerMeta, currentUser, dagsterRunDraft, demoMode, draftState, experimentMode, markTaskVersionPublished, pendingMappingCount, persistTaskDraft, publishTaskVersion, pushRunHistory, recoveredTaskVersion, rememberTaskVersionId, savedTaskVersionId, scheduleMode, selectedCanvasVariant, selectedTaskType, setActiveTab, setCanvasAction, setCanvasNotice, setDagsterRunDraft, setDraftState, setDrawerTab, setExecutionState, setNodeLibraryOpen, setRecoveredTaskVersion, setSelectedMappingId, setSelectedNodeId, setTaskReleaseGate, shortTrace, taskDraftValidation, taskReleaseGate, trustedMappingCount } = scope;
  const updateExecutionState = (nextState: ExecutionState) => {
      setExecutionState(nextState);
      setDrawerTab("plan");
    };

  const generateMappingSuggestions = () => {
      if (!demoMode) {
        setCanvasNotice({
          status: "error",
          title: "映射建议执行器未配置",
          detail: "生产模式不会从本地 fixture 生成映射建议；请等待专用 executor 与写后回读接入。"
        });
        return;
      }
      setCanvasAction("mapping");
      setCanvasNotice({
        status: "pending",
        title: "正在生成映射建议",
        detail: `读取 ${activeIntent.taskId} 的输入资产、输出契约和已确认字段。`
      });
      const nextMapping = activeMappingSuggestions.find((item) => item.state === "pending") ?? activeMappingSuggestions[0];
      if (nextMapping) setSelectedMappingId(nextMapping.id);
      setSelectedNodeId("ai");
      setDrawerTab("mapping");
      setNodeLibraryOpen(false);
      setCanvasAction(null);
      setCanvasNotice({
        status: "success",
        title: "DEMO：映射建议已生成",
        detail: `${trustedMappingCount} 条高置信建议可一键应用，${pendingMappingCount} 条仍需人工确认；结果仅来自演示 fixture。`
      });
      pushRunHistory("DEMO MappingSuggestion · AI 映射助手", "演示建议已生成");
    };

  const openScheduleSettings = () => {
      setActiveTab("schedule");
      setSelectedNodeId("dagster");
      setDrawerTab("plan");
      setNodeLibraryOpen(false);
      setCanvasNotice({
        status: "idle",
        title: "已打开触发与调度",
        detail: `当前模式为「${scheduleMode}」，保存后会写入当前任务版本，不影响历史运行。`
      });
    };

  const syncSchedulePlan = async () => {
      setCanvasAction("schedule");
      setCanvasNotice({
        status: "pending",
        title: "正在保存调度计划",
        detail: `${activeSchedule.dagsterObject} / ${activeSchedule.definition} 正在写入当前任务草稿。`
      });
      setSelectedNodeId("dagster");
      setDrawerTab("plan");
      setNodeLibraryOpen(false);
      const nextDagsterRunDraft = {
        ...dagsterRunDraft,
        jobName: `${selectedTaskType.defaultCanvas}_job`,
        partitionKey: activeSchedulePartitionKey,
        assetSelection: activeFlowStage.output || dagsterRunDraft.assetSelection,
        runTags: activeScheduleRunTags,
        materializationMode: `${activeSchedule.dagsterObject} + AssetChecks + ReviewQueue`,
        reason: `${scheduleMode} / ${activeSchedule.definition} / ${activeTriggerMeta.primary}`
      };
      setDagsterRunDraft(nextDagsterRunDraft);
      try {
        const receipt = await persistTaskDraft(nextDagsterRunDraft);
        setCanvasAction(null);
        setDraftState("已保存");
        setCanvasNotice({
          status: "success",
          title: "调度计划已保存",
          detail: `${scheduleMode} 已写入 ${receipt.data.id}，trace：${shortTrace(receipt.data.trace_id)}。`
        });
        pushRunHistory(`${activeSchedule.dagsterObject} · ${scheduleMode}`, `已保存 / ${receipt.data.id}`);
      } catch (error) {
        setCanvasAction(null);
        setCanvasNotice({
          status: "error",
          title: "调度计划保存失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
        pushRunHistory(`${activeSchedule.dagsterObject} · ${scheduleMode}`, "保存失败");
      }
    };

  const validateDagsterCompatibility = () => {
      setSelectedNodeId("dagster");
      setDrawerTab("plan");
      setNodeLibraryOpen(false);
      if (!taskDraftValidation.canPublish) {
        setCanvasNotice({
          status: "error",
          title: "发布门禁未通过",
          detail: `${taskDraftValidation.summary}：${taskDraftValidation.blockers.map((item) => item.label).join("、")}。`
        });
        pushRunHistory("CompatibilityCheck · 发布门禁", `失败：${taskDraftValidation.blockers.map((item) => item.label).join(" / ")}`);
        return;
      }
      setCanvasNotice({
        status: "idle",
        title: "本地契约检查通过",
        detail: `${taskDraftValidation.summary}；这只是当前表单的确定性检查，发布仍需服务端门禁和写后回读。`
      });
      pushRunHistory("LocalContractCheck · 表单门禁", "本地检查通过 / 未创建生产回执");
    };

  const saveTaskDraft = async () => {
      setCanvasAction("save");
      setDrawerTab("overview");
      setCanvasNotice({
        status: "pending",
        title: "正在保存任务草稿",
        detail: `${selectedTaskType.name} / ${selectedCanvasVariant.name} 正在写入 BFF 任务版本。`
      });
      try {
        const receipt = await persistTaskDraft();
        setDraftState("已保存");
        setCanvasNotice({
          status: "success",
          title: "任务草稿已保存",
          detail: `${receipt.data.id} 已写入，运行一次将引用该任务版本；trace：${shortTrace(receipt.data.trace_id)}。`
        });
        pushRunHistory(`TaskVersionDraft · ${receipt.data.id}`, `已保存 / ${shortTrace(receipt.data.trace_id)}`);
      } catch (error) {
        setCanvasNotice({
          status: "error",
          title: "任务草稿保存失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
        pushRunHistory("TaskVersionDraft · 保存草稿", "保存失败");
      } finally {
        setCanvasAction(null);
      }
    };

  return {
    updateExecutionState,
    generateMappingSuggestions,
    openScheduleSettings,
    syncSchedulePlan,
    validateDagsterCompatibility,
    saveTaskDraft,
    markTaskVersionPublished,
    publishTaskVersion
  };
}

export type CanvasExecutionActions = ReturnType<typeof buildCanvasExecutionActions>;
