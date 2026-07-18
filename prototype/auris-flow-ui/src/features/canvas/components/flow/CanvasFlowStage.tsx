import type { CanvasController } from "../../controller/useCanvasController";
import { AbstractFlowCanvas } from "./AbstractFlowCanvas";
import { NodeFlowCanvas } from "./NodeFlowCanvas";

export function CanvasFlowStage({ controller }: { controller: CanvasController }) {
  if (!controller.isFlowTab) return null;
  return controller.canvasLevel === "abstract"
    ? <AbstractFlowCanvas controller={controller} />
    : <NodeFlowCanvas controller={controller} />;
}
