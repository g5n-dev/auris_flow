import type { CanvasController } from "../controller/useCanvasController";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";

export function CanvasHeader({ controller }: { controller: CanvasController }) {
  const { activeFlowStage, activeSection, canvasLevel, canvasNotice, draftState, isFlowTab, selectedCanvasVariant, selectedTaskType, setCanvasLevel, taskActionFeedbackFor, taskActionTitle, taskToolbarActions } = controller;
  return (
    <>
      <div className="canvas-toolbar">
                <div className="canvas-title">
                  <span>{activeSection.title}</span>
                  <strong>{selectedTaskType.name}</strong>
                </div>
                <div className="canvas-context-summary" aria-label="当前任务上下文">
                  <span>{activeFlowStage.title}</span>
                  <span>{selectedCanvasVariant.name}</span>
                  <span>{draftState}</span>
                </div>
                {isFlowTab && (
                  <div className="canvas-level-switch" aria-label="画布抽象层级">
                    <button className={canvasLevel === "abstract" ? "active" : ""} onClick={() => setCanvasLevel("abstract")}>
                      抽象流程
                    </button>
                    <button className={canvasLevel === "nodes" ? "active" : ""} onClick={() => setCanvasLevel("nodes")}>
                      节点画布
                    </button>
                  </div>
                )}
                <div className="canvas-toolbar-actions">
                  {taskToolbarActions.map((action) => {
                    const ToolbarIcon = action.icon;
                    return (
                      <button
                        key={action.key}
                        className={action.active ? "active" : ""}
                        disabled={action.disabled}
                        data-action-key={action.key}
                        title={taskActionTitle(action)}
                        {...actionFeedbackAttrs(taskActionFeedbackFor(action.key))}
                        onClick={action.action}
                      >
                        <ToolbarIcon size={14} />
                        {action.label}
                      </button>
                    );
                  })}
                </div>
              </div>
      <div className={`operation-toast canvas-operation-toast is-${canvasNotice.status}`} role="status" aria-live="polite">
                <strong>{canvasNotice.title}</strong>
                <span>{canvasNotice.detail}</span>
              </div>
    </>
  );
}
