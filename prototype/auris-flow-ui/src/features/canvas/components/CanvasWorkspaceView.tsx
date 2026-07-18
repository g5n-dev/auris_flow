import { LazyBranchBoundary } from "../../../shared/ui/LazyBranchBoundary";
import type { CanvasController } from "../controller/useCanvasController";
import { CanvasDrawer } from "./drawer/CanvasDrawer";
import { CanvasFlowStage } from "./flow/CanvasFlowStage";
import { ExecutionMappingStrip } from "./flow/ExecutionMappingStrip";
import { CanvasHeader } from "./CanvasHeader";
import { FlowBlueprint } from "./FlowBlueprint";
import { NodeLibraryPanel } from "./NodeLibraryPanel";
import { CanvasTaskTabs } from "./tabs/CanvasTaskTabs";

export function CanvasWorkspaceView({ controller }: { controller: CanvasController }) {
  return (
    <div className="connector-workspace">
      <section className={`connector-canvas ${controller.isFlowTab ? `is-flow-mode canvas-${controller.canvasLevel}` : "is-tab-mode"}`}>
        <CanvasHeader controller={controller} />
        <FlowBlueprint controller={controller} />
        <div className="connector-stage">
          {controller.nodeLibraryOpen && (
            <LazyBranchBoundary
              label="节点库"
              minHeight={420}
              resetKey={String(controller.nodeLibraryOpen)}
              testId="canvas-node-library"
            >
              <NodeLibraryPanel controller={controller} />
            </LazyBranchBoundary>
          )}
          <CanvasTaskTabs controller={controller} />
          <CanvasFlowStage controller={controller} />
        </div>
        <ExecutionMappingStrip controller={controller} />
      </section>
      <CanvasDrawer controller={controller} />
    </div>
  );
}
