import type { ComponentType } from "react";
import { LazyBranchBoundary } from "../../../../shared/ui/LazyBranchBoundary";
import type { CanvasController } from "../../controller/useCanvasController";
import { DrawerActions } from "./DrawerActions";
import { DrawerAudioRuntime } from "./DrawerAudioRuntime";
import { DrawerDagsterBinding } from "./DrawerDagsterBinding";
import { DrawerHead } from "./DrawerHead";
import { DrawerLogs } from "./DrawerLogs";
import { DrawerMapping } from "./DrawerMapping";
import { DrawerOverview } from "./DrawerOverview";
import { DrawerPlan } from "./DrawerPlan";
import { DrawerReadonlyForm } from "./DrawerReadonlyForm";

type DrawerBranchComponent = ComponentType<{ controller: CanvasController }>;

const drawerBranches: Record<string, DrawerBranchComponent> = {
  overview: DrawerOverview,
  mapping: DrawerMapping,
  plan: DrawerPlan,
  logs: DrawerLogs
};

export function CanvasDrawer({ controller }: { controller: CanvasController }) {
  const ActiveDrawerBranch = drawerBranches[controller.drawerTab];
  const showAudioRuntime = ["vad", "diar", "asr"].includes(controller.selectedNodeId);
  return (
    <aside className="connector-drawer">
      <DrawerHead controller={controller} />
      <div className="drawer-body">
        {ActiveDrawerBranch && (
          <LazyBranchBoundary
            label="画布节点详情"
            minHeight={220}
            resetKey={controller.drawerTab}
            testId="canvas-drawer-branch"
          >
            <ActiveDrawerBranch controller={controller} />
          </LazyBranchBoundary>
        )}
        <DrawerDagsterBinding controller={controller} />
        {showAudioRuntime && (
          <LazyBranchBoundary
            label="音频运行参数"
            minHeight={180}
            resetKey={controller.selectedNodeId}
            testId="canvas-audio-runtime"
          >
            <DrawerAudioRuntime controller={controller} />
          </LazyBranchBoundary>
        )}
        <DrawerReadonlyForm controller={controller} />
      </div>
      <DrawerActions controller={controller} />
    </aside>
  );
}
