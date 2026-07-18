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
import { clamp } from "../../../shared/runtime/math";
import { createDefaultMappingSuggestions } from "../fixtures/intentsMapping";
import { canvasNodeSize, canvasNodeTemplates, canvasStageBounds, defaultDraftForTemplate } from "../nodeTemplates";
import type { CanvasNode, CanvasNodePosition, CanvasNodeTemplate, CanvasRunLog, MappingSuggestion, MappingSuggestionState } from "../types";
import type { PointerEvent as ReactPointerEvent } from "react";

export function buildCanvasNodeInteractions(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel & CanvasNodeCollections & CanvasNodeContextModel) {
  const { activeIntent, activeIntentKey, activePartitionKey, addedNodes, allNodes, dagsterRunDraft, dragMovedRef, dragState, executionState, mappingConfidenceThreshold, markTaskDraftDirty, nodePositions, runHistory, setDragState, setDrawerTab, setMappingSuggestionsByIntent, setNodeDraft, setNodeLibraryOpen, setNodePositions, setSelectedMappingId, setSelectedNodeId, setSelectedTemplateKey } = scope;
  const positionFor = (node: CanvasNode): CanvasNodePosition => nodePositions[node.id] ?? { x: node.x, y: node.y };

  const positionById = (nodeId: string): CanvasNodePosition => {
      const node = allNodes.find((item) => item.id === nodeId);
      return node ? positionFor(node) : { x: 0, y: 0 };
    };

  const startNodeDrag = (event: ReactPointerEvent<HTMLElement>, node: CanvasNode) => {
      if (event.button !== 0) return;
      event.currentTarget.setPointerCapture?.(event.pointerId);
      dragMovedRef.current = false;
      const currentPosition = positionFor(node);
      setSelectedNodeId(node.id);
      setDragState({
        id: node.id,
        startX: event.clientX,
        startY: event.clientY,
        startLeft: currentPosition.x,
        startTop: currentPosition.y
      });
    };

  const moveNodeDrag = (event: ReactPointerEvent<HTMLElement>) => {
      if (!dragState) return;
      const dx = event.clientX - dragState.startX;
      const dy = event.clientY - dragState.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) dragMovedRef.current = true;
      const nodeSize = canvasNodeSize(dragState.id);
      setNodePositions((current) => ({
        ...current,
        [dragState.id]: {
          x: Number(clamp(dragState.startLeft + dx, canvasStageBounds.padding, canvasStageBounds.width - nodeSize.width - canvasStageBounds.padding).toFixed(1)),
          y: Number(
            clamp(
              dragState.startTop + dy,
              canvasStageBounds.padding,
              canvasStageBounds.height - nodeSize.height - canvasStageBounds.bottomPadding
            ).toFixed(1)
          )
        }
      }));
    };

  const endNodeDrag = (event: ReactPointerEvent<HTMLElement>) => {
      if (!dragState) return;
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      setDragState(null);
      window.setTimeout(() => {
        dragMovedRef.current = false;
      }, 0);
    };

  const selectNode = (nodeId: string) => {
      if (dragMovedRef.current) return;
      setSelectedNodeId(nodeId);
    };

  const dragPropsFor = (node: CanvasNode) => ({
      onPointerDown: (event: ReactPointerEvent<HTMLElement>) => startNodeDrag(event, node),
      onPointerMove: moveNodeDrag,
      onPointerUp: endNodeDrag,
      onPointerCancel: endNodeDrag
    });

  const linePath = (fromId: string, toId: string) => {
      const from = positionById(fromId);
      const to = positionById(toId);
      const fromSize = canvasNodeSize(fromId);
      const toSize = canvasNodeSize(toId);
      const forward = from.x + fromSize.width <= to.x + toSize.width / 2;
      const start = {
        x: forward ? from.x + fromSize.width : from.x,
        y: from.y + fromSize.height / 2
      };
      const end = {
        x: forward ? to.x : to.x + toSize.width,
        y: to.y + toSize.height / 2
      };
      const curve = Math.max(48, Math.abs(end.x - start.x) * 0.45);
      const c1x = start.x + (forward ? curve : -curve);
      const c2x = end.x - (forward ? curve : -curve);
      return `M ${start.x} ${start.y} C ${c1x} ${start.y}, ${c2x} ${end.y}, ${end.x} ${end.y}`;
    };

  const edges = [
      { from: "platformAuth", to: "tenantApi", active: true },
      { from: "platformAuth", to: "employeeApi", active: true },
      { from: "platformAuth", to: "audioUrlApi", active: activeIntentKey !== "entity" },
      { from: "platformAuth", to: "eventApi", active: activeIntentKey !== "audio" },
      { from: "tenantApi", to: "ai", active: activeIntentKey !== "audio" },
      { from: "employeeApi", to: "ai", active: activeIntentKey !== "audio" },
      { from: "audioUrlApi", to: "ai", active: activeIntentKey !== "entity" },
      { from: "eventApi", to: "ai", active: activeIntentKey !== "audio" },
      { from: "ai", to: "entityMap", active: true },
      { from: "entityMap", to: "dagster", active: activeIntentKey === "entity" || activeIntentKey === "review" },
      { from: "audioUrlApi", to: "dagster", active: activeIntentKey === "audio" || activeIntentKey === "review" },
      { from: "eventApi", to: "dagster", active: activeIntentKey === "asset" || activeIntentKey === "review" },
      { from: "dagster", to: "vad", active: activeIntentKey !== "entity" },
      { from: "vad", to: "diar", active: activeIntentKey !== "entity" },
      { from: "diar", to: "asr", active: activeIntentKey !== "entity" },
      { from: "dagster", to: "asr", active: activeIntentKey !== "entity" },
      { from: "asr", to: "tagger", active: activeIntentKey !== "entity" },
      ...addedNodes.map((node) => ({
        from: node.context.type === "输入引用" ? node.id : "dagster",
        to: node.context.type === "输入引用" ? "ai" : node.id,
        active: node.intentKeys.includes(activeIntentKey)
      }))
    ];

  const runLogs: CanvasRunLog[] = [
      ...runHistory,
      {
        id: "runtime-request",
        time: "12:31:08",
        name: `运行请求 · ${dagsterRunDraft.partitionKey || activePartitionKey}`,
        state: executionState === "idle" ? "等待生成" : "已生成"
      },
      {
        id: "runtime-job",
        time: "12:31:12",
        name: `Job · ${dagsterRunDraft.jobName}`,
        state: executionState === "running" || executionState === "success" ? "执行中" : "待执行"
      },
      {
        id: "runtime-assets",
        time: "12:31:26",
        name: `资产生成 · ${dagsterRunDraft.assetSelection}`,
        state: executionState === "success" ? "已写入" : "等待输出"
      }
    ];

  const updateActiveMappingSuggestions = (updater: (rows: MappingSuggestion[]) => MappingSuggestion[]) => {
      setMappingSuggestionsByIntent((current) => ({
        ...current,
        [activeIntentKey]: updater(current[activeIntentKey] ?? [])
      }));
      markTaskDraftDirty();
    };

  const updateMappingTarget = (id: string, targetField: string) => {
      updateActiveMappingSuggestions((rows) =>
        rows.map((item) => (item.id === id ? { ...item, targetField, state: item.state === "applied" ? "confirmed" : item.state } : item))
      );
    };

  const updateMappingPolicy = (id: string, policy: string) => {
      updateActiveMappingSuggestions((rows) =>
        rows.map((item) => (item.id === id ? { ...item, policy, state: item.state === "applied" ? "confirmed" : item.state } : item))
      );
    };

  const setMappingDecision = (id: string, state: MappingSuggestionState) => {
      updateActiveMappingSuggestions((rows) => rows.map((item) => (item.id === id ? { ...item, state } : item)));
    };

  const applyTrustedMappings = () => {
      updateActiveMappingSuggestions((rows) =>
        rows.map((item) =>
          item.state !== "rejected" && (item.state === "confirmed" || item.confidence >= mappingConfidenceThreshold)
            ? { ...item, state: "applied" }
            : item
        )
      );
    };

  const resetActiveMappings = () => {
      const defaults = createDefaultMappingSuggestions();
      setMappingSuggestionsByIntent((current) => ({
        ...current,
        [activeIntentKey]: defaults[activeIntentKey]
      }));
      setSelectedMappingId(defaults[activeIntentKey][0]?.id ?? "");
      markTaskDraftDirty();
    };

  const selectNodeTemplate = (template: CanvasNodeTemplate) => {
      setSelectedTemplateKey(template.key);
      setNodeDraft(defaultDraftForTemplate(template, activeIntent));
    };

  const openOutputSinkTemplate = (templateKey: string) => {
      const template = canvasNodeTemplates.find((item) => item.key === templateKey);
      if (!template) return;
      selectNodeTemplate(template);
      setNodeLibraryOpen(true);
      setDrawerTab("overview");
    };

  return {
    positionFor,
    positionById,
    startNodeDrag,
    moveNodeDrag,
    endNodeDrag,
    selectNode,
    dragPropsFor,
    linePath,
    edges,
    runLogs,
    updateActiveMappingSuggestions,
    updateMappingTarget,
    updateMappingPolicy,
    setMappingDecision,
    applyTrustedMappings,
    resetActiveMappings,
    selectNodeTemplate,
    openOutputSinkTemplate
  };
}

export type CanvasNodeInteractions = ReturnType<typeof buildCanvasNodeInteractions>;
