import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import type { CanvasNodeCollections } from "./buildCanvasNodeCollections";
import { asrTaskBindingRows, audioNodeRuntimeParams } from "../catalog";
import { canvasNodeContextDescriptors } from "../fixtures/viewDescriptors";
import { dagsterBindingForNode } from "../nodeTemplates";
import type { CanvasNodeContext } from "../types";

export function buildCanvasNodeContextModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel & CanvasNodeCollections) {
  const { activeIntent, activeIntentKey, addedNodes, allNodes, appliedMappingCount, asrExecutionMode, asrHotwordVersionId, mappingConfidenceThreshold, mappingTotal, pendingMappingCount, selectedHotwordPackVersion, selectedNodeId, sourceNodes } = scope;
  const visibleNodeIds = new Set(allNodes.filter((node) => node.intentKeys.includes(activeIntentKey)).map((node) => node.id));
  const selectedNode = allNodes.find((node) => node.id === selectedNodeId) ?? sourceNodes[0];
  const relatedNodeCount = allNodes.filter((node) => node.intentKeys.includes(activeIntentKey)).length;
  const dynamicNodeContexts = Object.fromEntries(addedNodes.map((node) => [node.id, node.context]));

  const staticNodeContexts = Object.fromEntries(
    Object.entries(canvasNodeContextDescriptors).map(([id, descriptor]) => {
      const fields = descriptor.fields.map(([label, value]): [string, string] => [label, value ?? ""]);
      if (id === "ai") {
        fields[1][1] = `${appliedMappingCount}/${mappingTotal}`;
        fields[2][1] = `${pendingMappingCount} 项`;
        fields[3][1] = `${mappingConfidenceThreshold}%`;
      } else if (id === "dagster") {
        fields[0][1] = activeIntent.taskId;
        fields[1][1] = activeIntent.output;
        fields[2][1] = activeIntent.trigger;
        fields[3][1] = activeIntent.scope;
      } else if (id === "asr") {
        fields[9][1] = `${asrHotwordVersionId} / ${asrExecutionMode}`;
      }
      return [id, { ...descriptor, usedBy: [...descriptor.usedBy], fields } satisfies CanvasNodeContext];
    })
  );

  const nodeContexts: Record<string, CanvasNodeContext> = {
    ...dynamicNodeContexts,
    ...staticNodeContexts
  };
  const selectedNodeContext = nodeContexts[selectedNodeId] ?? nodeContexts.platformAuth;
  const selectedDagsterBinding = dagsterBindingForNode(selectedNode, activeIntent);
  const displayExecutionDefinition = (definition: string) =>
    definition === "SourceAsset" ? "外部数据源" : definition === "Asset" ? "处理资产" : definition;
  const selectedAudioRuntimeParams = (audioNodeRuntimeParams[selectedNodeId as keyof typeof audioNodeRuntimeParams] ?? []).map(([label, value]) =>
    label === "asr.hotword_pack_version_id" ? [label, asrHotwordVersionId || "blocked：待后端恢复"] : [label, value]
  );
  const resolvedAsrTaskBindingRows = asrTaskBindingRows.map(([label, value, detail]) =>
    label === "词包版本"
      ? [label, asrHotwordVersionId || "blocked：待后端恢复", `${selectedHotwordPackVersion?.status ?? "missing"}；生产只允许 published，候选只能用于 shadow`]
      : [label, value, detail]
  );

  return {
    visibleNodeIds,
    selectedNode,
    relatedNodeCount,
    dynamicNodeContexts,
    nodeContexts,
    selectedNodeContext,
    selectedDagsterBinding,
    displayExecutionDefinition,
    selectedAudioRuntimeParams,
    resolvedAsrTaskBindingRows
  };
}

export type CanvasNodeContextModel = ReturnType<typeof buildCanvasNodeContextModel>;
