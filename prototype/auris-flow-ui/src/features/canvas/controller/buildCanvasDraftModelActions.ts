import type { CanvasActionScope } from "./canvasActionScope";
import { saveTaskVersionDraft } from "../../../api/client";
import { defaultWorkspaceContext as defaultTopbarContext } from "../../../shared/fixtures/defaultWorkspaceContext";
import { experimentMetricSuggestions } from "../catalog";
import type { CanvasNodeDraft, TaskScheduleMode } from "../types";
import type { CanvasConfiguredNodeActions } from "./buildCanvasConfiguredNodeActions";

export function buildCanvasDraftModelActions(scope: CanvasActionScope & CanvasConfiguredNodeActions) {
  const { activeFlowStage, activeIntent, activeIntentKey, activePartitionKey, activeRunKey, activeSchedule, activeSchedulePartitionKey, activeScheduleRunTags, addConfiguredNode, addedNodes, asrExecutionMode, asrHotwordVersionId, availableExperimentMetrics, dagsterRunDraft, declaredTaskTypeId, demoMode, displayExecutionDefinition, draftOutputContract, experimentMode, hotwordPackVersionOptions, markTaskDraftDirty, metricDraftState, nodeDraft, openOutputSinkTemplate, pushRunHistory, rememberTaskVersionId, sceneBinding, sceneManifest, scheduleMode, selectedCanvasVariant, selectedExperimentMetric, selectedHotwordPackVersion, selectedTaskType, selectedTemplate, setActiveTab, setAddedNodes, setAsrExecutionMode, setAsrHotwordVersionId, setBackfillConfirmed, setCanvasNotice, setDrawerTab, setExecutionState, setMetricDraftState, setNodeDraft, setNodeLibraryOpen, setScheduleMode, setSelectedExperimentMetricKey, setSelectedNodeId, taskDagVisibleNodes } = scope;
  const metricDraftActionsDisabledReason = demoMode
    ? ""
    : "指标草稿、观测和发布闸门尚未接入专用 BFF 执行契约；生产模式禁止由本地状态制造成功结果。";
  const blockUnconfiguredMetricAction = () => {
    if (!metricDraftActionsDisabledReason) return false;
    setCanvasNotice({
      status: "error",
      title: "指标动作暂不可用",
      detail: metricDraftActionsDisabledReason
    });
    return true;
  };
  const updateAsrExecutionMode = (mode: "production" | "shadow") => {
      setAsrExecutionMode(mode);
      markTaskDraftDirty();
      if (mode === "production" && selectedHotwordPackVersion?.status !== "published") {
        const publishedVersion = hotwordPackVersionOptions.find((item) => item.current && item.status === "published")
          ?? hotwordPackVersionOptions.find((item) => item.status === "published");
        setAsrHotwordVersionId(publishedVersion?.id ?? "");
        setCanvasNotice({
          status: "error",
          title: "候选热词版本已从生产绑定移除",
          detail: publishedVersion
            ? `production 仅允许 published 版本；已恢复 ${publishedVersion.id}。候选版本只能用于 shadow。`
            : "production 仅允许 published 版本；当前没有可用发布版本，保存与运行已阻断。"
        });
        return;
      }
      setCanvasNotice({
        status: "success",
        title: "ASR 执行模式已更新",
        detail: mode === "shadow" ? "影子运行只写候选转写与评测资产，不覆盖生产。" : "生产运行已绑定已发布词包版本。"
      });
    };

  const updateAsrHotwordVersion = (versionId: string) => {
      const version = hotwordPackVersionOptions.find((item) => item.id === versionId);
      if (!version) {
        setCanvasNotice({
          status: "error",
          title: "热词版本绑定已阻断",
          detail: `${versionId || "空版本"} 不在后端热词版本列表中。`
        });
        return;
      }
      if (asrExecutionMode === "production" && version.status !== "published") {
        setCanvasNotice({
          status: "error",
          title: "热词版本绑定已阻断",
          detail: `${version.label} 为 ${version.status}，生产运行仅允许已发布版本；请先切换 shadow。`
        });
        return;
      }
      setAsrHotwordVersionId(version.id);
      markTaskDraftDirty();
      setCanvasNotice({
        status: "success",
        title: "热词版本绑定已更新",
        detail: `${version.id} / ${version.status} / ${asrExecutionMode}；保存后只写入当前 TaskVersion 草稿。`
      });
    };

  const updateNodeDraft = (key: keyof CanvasNodeDraft, value: string) => {
      setNodeDraft((current) => ({ ...current, [key]: value }));
      markTaskDraftDirty();
    };

  const taskDraftPayload = (executionDraft = dagsterRunDraft) => ({
      task_type_id: selectedTaskType.key,
      version: selectedCanvasVariant.version,
      canvas_variant: selectedCanvasVariant.key,
      label_version: sceneManifest?.label_version_refs[0] ?? (demoMode ? defaultTopbarContext.label : ""),
      scene_profile_id: sceneBinding?.scene_profile_id,
      scene_profile_version_id: sceneBinding?.scene_profile_version_id,
      scene_profile_snapshot_sha256: sceneBinding?.manifest_sha256,
      source: "canvas_module",
      status: "draft",
      name: `${selectedTaskType.name} / ${selectedCanvasVariant.name}`,
      schedule: {
        mode: scheduleMode,
        object: activeSchedule.dagsterObject,
        definition: activeSchedule.definition,
        partition_key: activeSchedulePartitionKey,
        run_tags: activeScheduleRunTags
      },
      execution: {
        job_name: executionDraft.jobName,
        partition_key: executionDraft.partitionKey || activePartitionKey,
        asset_selection: executionDraft.assetSelection,
        run_key: activeRunKey,
        max_retries: executionDraft.maxRetries,
        concurrency_limit: executionDraft.concurrencyLimit
      },
      audio_intelligence: {
        execution_mode: asrExecutionMode,
        language: "zh-CN",
        hotword_pack_version_id: asrHotwordVersionId,
        return_word_timestamps: true
      },
      graph: {
        intent: activeIntent.taskId,
        stage: activeFlowStage.key,
        node_count: taskDagVisibleNodes.length,
        added_nodes: addedNodes.map((node) => node.name)
      },
      experiment: {
        mode: experimentMode,
        metric: selectedExperimentMetric.key,
        metric_state: metricDraftState
      }
    });

  const persistTaskDraft = async (executionDraft = dagsterRunDraft) => {
      if (!demoMode && !sceneBinding) {
        throw new Error("当前项目未绑定已发布 SceneProfile，任务草稿写入已阻断");
      }
      if (!demoMode && !declaredTaskTypeId) {
        throw new Error("当前 SceneProfile 未声明 task_type_refs，任务草稿写入已阻断");
      }
      const requiresAudioIntelligence = demoMode || Boolean(
        sceneManifest?.capabilities.some((capability) => capability.toLowerCase().includes("audio"))
      );
      if (requiresAudioIntelligence && !selectedHotwordPackVersion) {
        throw new Error("当前音频场景未恢复可用 hotword_pack_version_id，任务草稿写入已阻断");
      }
      if (requiresAudioIntelligence && asrExecutionMode === "production") {
        const productionHotwordVersion = selectedHotwordPackVersion;
        if (!productionHotwordVersion || productionHotwordVersion.status !== "published") {
          throw new Error(`${productionHotwordVersion?.id ?? "未绑定热词版本"} 为 ${productionHotwordVersion?.status ?? "missing"}，production 只能绑定 published 版本`);
        }
      }
      const response = await saveTaskVersionDraft(taskDraftPayload(executionDraft));
      rememberTaskVersionId(response.data.id);
      return response;
    };

  const changeScheduleMode = (mode: TaskScheduleMode) => {
      if (mode === scheduleMode) return;
      setScheduleMode(mode);
      markTaskDraftDirty();
      setBackfillConfirmed(false);
      setSelectedNodeId("dagster");
      setDrawerTab("plan");
      setCanvasNotice({
        status: "idle",
        title: "触发方式已切换",
        detail: `当前已切到「${mode}」，运行请求预览和执行字段会按新模式生成，请保存调度计划。`
      });
      pushRunHistory(`ScheduleMode · ${mode}`, "待保存");
    };

  const confirmBackfillGate = () => {
      setBackfillConfirmed(true);
      markTaskDraftDirty();
      setSelectedNodeId("dagster");
      setDrawerTab("plan");
      setCanvasNotice({
        status: "success",
        title: "Backfill 门禁已确认",
        detail: `${activeSchedulePartitionKey} 的并发、复算范围和跳过策略已人工确认，请保存调度计划后再发布。`
      });
      pushRunHistory("BackfillGate · 人工确认", "已确认 / 待保存");
    };

  const selectExperimentMetric = (metricKey: string) => {
      const metric = experimentMetricSuggestions.find((item) => item.key === metricKey) ?? selectedExperimentMetric;
      setSelectedExperimentMetricKey(metric.key);
      setMetricDraftState("AI草稿");
      markTaskDraftDirty();
      setDrawerTab("plan");
      setCanvasNotice({
        status: "idle",
        title: "主指标候选已切换",
        detail: `${metric.name} 已进入 AI 草稿状态，需要加入观测或保存为发布闸门。`
      });
      pushRunHistory(`ExperimentMetric · ${metric.key}`, "AI 草稿");
    };

  const generateExperimentMetricDraft = () => {
      if (blockUnconfiguredMetricAction()) return;
      const releaseMetricKey = sceneManifest?.release_requirements[0]?.metric_key;
      const generatedMetric = availableExperimentMetrics.find((metric) => metric.key === releaseMetricKey)
        ?? availableExperimentMetrics[0]
        ?? selectedExperimentMetric;
      setSelectedExperimentMetricKey(generatedMetric.key);
      setMetricDraftState("AI草稿");
      markTaskDraftDirty();
      setDrawerTab("plan");
      setCanvasNotice({
        status: "success",
        title: "指标草稿已生成",
        detail: `已从当前 SceneProfile 选择 ${generatedMetric.name}，并绑定 ${generatedMetric.sql}，尚未作为发布依据。`
      });
      pushRunHistory(`ExperimentMetric · ${generatedMetric.key}`, "SceneProfile 草稿已生成");
    };

  const addMetricToObservation = () => {
      if (blockUnconfiguredMetricAction()) return;
      setMetricDraftState("已加入观测");
      markTaskDraftDirty();
      setDrawerTab("plan");
      setCanvasNotice({
        status: "success",
        title: "指标已加入观测看板",
        detail: `${selectedExperimentMetric.name} 已绑定运行 tags、资产口径和 ${selectedExperimentMetric.window} 观测窗口；发布或运行前会先写入 BFF 草稿。`
      });
      pushRunHistory(`ExperimentMetric · ${selectedExperimentMetric.key}`, "已加入观测");
    };

  const saveMetricAsReleaseGate = () => {
      if (blockUnconfiguredMetricAction()) return;
      setMetricDraftState("发布闸门");
      markTaskDraftDirty();
      setDrawerTab("plan");
      setCanvasNotice({
        status: "success",
        title: "主指标已保存为发布闸门",
        detail: `${selectedExperimentMetric.name} 已进入当前草稿的发布门禁，发布前会先写入 BFF 任务版本。`
      });
      pushRunHistory(`ReleaseGateMetric · ${selectedExperimentMetric.key}`, "发布闸门已保存");
    };

  const refreshMetricObservation = () => {
      if (blockUnconfiguredMetricAction()) return;
      setMetricDraftState((current) => (current === "AI草稿" ? "已加入观测" : current));
      setCanvasNotice({
        status: "success",
        title: "观测数据已刷新",
        detail: `${selectedExperimentMetric.name} 已按 ${selectedExperimentMetric.window} 重新聚合，状态已写入运行记录。`
      });
      pushRunHistory(`MetricObservation · ${selectedExperimentMetric.key}`, "观测已刷新");
    };

  const openInputSourceEditor = (nodeId = "platformAuth") => {
      setActiveTab("io");
      setSelectedNodeId(nodeId);
      setDrawerTab("overview");
      setNodeLibraryOpen(false);
      setCanvasNotice({
        status: "idle",
        title: "已定位输入源配置",
        detail: "右侧节点配置可修改认证、资源类型、资产映射、字段和演示载荷。"
      });
    };

  const openFieldMappingEditor = () => {
      setActiveTab("io");
      setSelectedNodeId("ai");
      setDrawerTab("mapping");
      setNodeLibraryOpen(false);
      setCanvasNotice({
        status: "idle",
        title: "已打开字段映射入口",
        detail: "可确认、拒绝或修改 Agent 映射建议，保存后影响当前任务草稿。"
      });
    };

  const openOutputWritebackEditor = () => {
      setActiveTab("io");
      openOutputSinkTemplate("platform-callback-output");
      setCanvasNotice({
        status: "idle",
        title: "已打开输出回写配置",
        detail: "可编辑平台回调、处理后音频上传、幂等键、输出资产和回写字段。"
      });
    };

  return {
    updateAsrExecutionMode,
    updateAsrHotwordVersion,
    updateNodeDraft,
    taskDraftPayload,
    persistTaskDraft,
    changeScheduleMode,
    confirmBackfillGate,
    selectExperimentMetric,
    metricDraftActionsDisabledReason,
    generateExperimentMetricDraft,
    addMetricToObservation,
    saveMetricAsReleaseGate,
    refreshMetricObservation,
    openInputSourceEditor,
    openFieldMappingEditor,
    openOutputWritebackEditor,
    addConfiguredNode
  };
}

export type CanvasDraftModelActions = ReturnType<typeof buildCanvasDraftModelActions>;
