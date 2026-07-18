import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import { executionStateMeta } from "../catalog";
import { canvasNodeCollectionDescriptors } from "../fixtures/viewDescriptors";
import type { CanvasNode } from "../types";
import { Activity, BrainCircuit, Database, GitBranch, Headphones, Link2, Sparkles, Tags, UserCheck } from "lucide-react";

export function buildCanvasNodeCollections(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel) {
  const { activeIntent, activeIntentKey, addedNodes, appliedMappingCount, executionState, experimentMode, mappingTotal } = scope;
  const canvasNodeIcons: Record<string, CanvasNode["icon"]> = {
    Activity,
    BrainCircuit,
    Database,
    GitBranch,
    Headphones,
    Link2,
    Sparkles,
    Tags,
    UserCheck
  };
  const toCanvasNode = (
    descriptor: (typeof canvasNodeCollectionDescriptors.source)[number],
    overrides: Partial<CanvasNode> = {}
  ): CanvasNode => {
    const { iconKey, ...staticFields } = descriptor;
    return {
      ...staticFields,
      x: descriptor.x ?? 0,
      y: descriptor.y ?? 0,
      status: descriptor.status ?? "",
      metaB: descriptor.metaB ?? "",
      confidence: descriptor.confidence ?? 0,
      intentKeys: [...descriptor.intentKeys],
      tags: [...descriptor.tags],
      icon: canvasNodeIcons[iconKey],
      ...overrides
    };
  };

  const sourceNodes: CanvasNode[] = canvasNodeCollectionDescriptors.source.map((descriptor) =>
    toCanvasNode(
      descriptor,
      descriptor.id === "audioUrlApi" ? { y: activeIntentKey === "audio" ? 205 : 386 } : undefined
    )
  );

  const processNodes: CanvasNode[] = canvasNodeCollectionDescriptors.process.map((descriptor) => {
    if (descriptor.id === "vad") {
      return toCanvasNode(descriptor, {
        x: activeIntentKey === "audio" ? 960 : 1000,
        status: executionState === "running" ? "运行中" : "待机"
      });
    }
    if (descriptor.id === "diar") {
      return toCanvasNode(descriptor, {
        x: activeIntentKey === "audio" ? 960 : 1000,
        status: executionState === "running" ? "运行中" : "待机"
      });
    }
    if (descriptor.id === "asr") {
      return toCanvasNode(descriptor, {
        x: activeIntentKey === "audio" ? 1220 : 1000,
        y: activeIntentKey === "audio" ? 38 : 386,
        status: executionState === "queued" ? "排队" : "待机"
      });
    }
    return toCanvasNode(descriptor, {
      x: activeIntentKey === "audio" ? 1220 : 1000,
      y: activeIntentKey === "audio" ? 212 : 560,
      status: experimentMode === "影子评测" ? "影子运行" : "待机"
    });
  });

  const specialNodes: CanvasNode[] = canvasNodeCollectionDescriptors.special.map((descriptor) => {
    if (descriptor.id === "ai") {
      return toCanvasNode(descriptor, { status: `${appliedMappingCount}/${mappingTotal} 已应用` });
    }
    if (descriptor.id === "dagster") {
      return toCanvasNode(descriptor, {
        x: activeIntentKey === "entity" ? 990 : activeIntentKey === "audio" ? 680 : 730,
        y: activeIntentKey === "entity" || activeIntentKey === "audio" ? 292 : 390,
        status: executionStateMeta[executionState].label,
        metaB: activeIntent.taskId,
        confidence: executionState === "success" ? 100 : executionState === "running" ? 76 : 62
      });
    }
    return toCanvasNode(descriptor);
  });

  const allNodes = [...sourceNodes, ...specialNodes, ...processNodes, ...addedNodes];

  return {
    sourceNodes,
    processNodes,
    specialNodes,
    allNodes
  };
}

export type CanvasNodeCollections = ReturnType<typeof buildCanvasNodeCollections>;
