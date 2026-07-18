import { LazyBranchBoundary } from "../../../../../shared/ui/LazyBranchBoundary";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { MinimapCanvas } from "./MinimapCanvas";
import { MinimapEmployees } from "./MinimapEmployees";
import { MinimapHeader } from "./MinimapHeader";
import { MinimapListeningStrip } from "./MinimapListeningStrip";
import { SessionBoundaryDialog } from "./SessionBoundaryDialog";

export function AnnotationMinimapView({ controller }: { controller: AnnotationMinimapController }) {
  const { collapsed, selectedSlice } = controller;
  return (
    (
        <div className={collapsed ? "mm collapsed" : "mm"}>
          <MinimapHeader controller={controller} />
          {!collapsed && (
            <>
              <MinimapListeningStrip controller={controller} />
              {selectedSlice && (
                <LazyBranchBoundary label="会话边界编辑器" minHeight={520} resetKey={selectedSlice.id} testId="listening-boundary-dialog">
                  <SessionBoundaryDialog controller={controller} />
                </LazyBranchBoundary>
              )}
              <MinimapEmployees controller={controller} />
              <MinimapCanvas controller={controller} />
            </>
          )}
        </div>
      )
  );
}
