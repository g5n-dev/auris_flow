import type { CanvasModuleProps } from "../types";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasNodeCollections } from "./buildCanvasNodeCollections";
import type { CanvasNodeContextModel } from "./buildCanvasNodeContextModel";
import type { CanvasNodeInteractions } from "./buildCanvasNodeInteractions";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasTaskDagModel } from "./buildCanvasTaskDagModel";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasState } from "./useCanvasState";

export type CanvasActionScope = CanvasModuleProps
  & CanvasState
  & CanvasPrimitiveActions
  & CanvasRecoveryModel
  & CanvasSectionModel
  & CanvasScheduleModel
  & CanvasExecutionPlan
  & CanvasRuntimeModel
  & CanvasNodeCollections
  & CanvasNodeContextModel
  & CanvasNodeInteractions
  & CanvasTaskDagModel;
