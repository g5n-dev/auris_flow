import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import { taskFlowStages, taskTypeBlueprints } from "../catalog";
import { canvasNodeTemplates } from "../nodeTemplates";
import type { CanvasIntentKey, FlowStageKey } from "../types";
import { useMemo } from "react";

export function useCanvasRuntimeModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan) {
  const { markTaskDraftDirty, setActiveIntentKey, setActiveStageKey, setDrawerTab, setExperimentMode, setNodeLibraryOpen, setSelectedCanvasVariantKey, setSelectedNodeId, setSelectedTaskTypeKey } = scope;
  const nodeTemplateCategories = useMemo(
      () =>
        (["平台数据同步抽取", "智能处理流水线", "平台处理结果推送", "人工与控制"] as const).map((category) => ({
          category,
          templates: canvasNodeTemplates.filter((template) => template.category === category)
        })),
      []
    );

  const primaryNodeForIntent: Record<CanvasIntentKey, string> = {
      entity: "platformAuth",
      audio: "audioUrlApi",
      asset: "dagster",
      review: "eventApi"
    };

  const selectFlowStage = (stageKey: FlowStageKey) => {
      const stage = taskFlowStages.find((item) => item.key === stageKey) ?? taskFlowStages[0];
      setActiveStageKey(stage.key);
      setActiveIntentKey(stage.intentKey);
      setSelectedNodeId(stage.nodeId);
      setDrawerTab(stage.key === "agent" || stage.key === "models" ? "mapping" : "plan");
      setNodeLibraryOpen(false);
    };

  const selectTaskType = (taskKey: string) => {
      const taskType = taskTypeBlueprints.find((task) => task.key === taskKey) ?? taskTypeBlueprints[0];
      const matchingStage = taskFlowStages.find((stage) => stage.intentKey === taskType.intentKey);
      setSelectedTaskTypeKey(taskType.key);
      setActiveIntentKey(taskType.intentKey);
      markTaskDraftDirty();
      if (matchingStage) setActiveStageKey(matchingStage.key);
      setSelectedNodeId(primaryNodeForIntent[taskType.intentKey]);
      setDrawerTab("plan");
      setNodeLibraryOpen(false);
      if (taskType.key === "evidence-dataflow") {
        setSelectedCanvasVariantKey("stable-v3");
        setExperimentMode("影子评测");
      }
    };

  return {
    nodeTemplateCategories,
    primaryNodeForIntent,
    selectFlowStage,
    selectTaskType
  };
}

export type CanvasRuntimeModel = ReturnType<typeof useCanvasRuntimeModel>;
