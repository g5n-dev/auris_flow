import type { ComponentType } from "react";
import { LazyBranchBoundary } from "../../../../shared/ui/LazyBranchBoundary";
import type { CanvasController } from "../../controller/useCanvasController";
import { TaskCanvasesTab } from "./TaskCanvasesTab";
import { TaskDefinitionTab } from "./TaskDefinitionTab";
import { TaskExperimentsTab } from "./TaskExperimentsTab";
import { TaskIoTab } from "./TaskIoTab";
import { TaskRunsTab } from "./TaskRunsTab";
import { TaskScheduleTab } from "./TaskScheduleTab";
import { TaskTabHeader } from "./TaskTabHeader";
import { TaskVersionsTab } from "./TaskVersionsTab";

type CanvasTabComponent = ComponentType<{ controller: CanvasController }>;

const tabComponents: Record<string, CanvasTabComponent> = {
  definition: TaskDefinitionTab,
  canvases: TaskCanvasesTab,
  io: TaskIoTab,
  schedule: TaskScheduleTab,
  experiments: TaskExperimentsTab,
  runs: TaskRunsTab,
  versions: TaskVersionsTab
};

export function CanvasTaskTabs({ controller }: { controller: CanvasController }) {
  if (controller.activeTab === "flow") return null;
  const ActiveTab = tabComponents[controller.activeTab];
  return (
    <div className="task-tab-page">
      <TaskTabHeader controller={controller} />
      {ActiveTab && (
        <LazyBranchBoundary
          label="画布任务页签"
          minHeight={420}
          resetKey={controller.activeTab}
          testId="canvas-tab-branch"
        >
          <ActiveTab controller={controller} />
        </LazyBranchBoundary>
      )}
    </div>
  );
}
