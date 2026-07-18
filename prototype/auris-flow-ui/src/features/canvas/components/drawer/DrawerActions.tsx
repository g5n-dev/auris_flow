import type { CanvasController } from "../../controller/useCanvasController";


export function DrawerActions({ controller }: { controller: CanvasController }) {
  const { canvasAction, discardTaskChanges, draftState, publishTaskVersion, saveTaskDraft, taskPublishLabel } = controller;
  return (
    <>
      <div className="drawer-actions">
                <button onClick={discardTaskChanges}>放弃更改</button>
                <button onClick={saveTaskDraft}>保存草稿</button>
                <button onClick={publishTaskVersion} disabled={Boolean(canvasAction) || draftState === "已发布"}>
                  {canvasAction === "publish" ? "处理中" : taskPublishLabel}
                </button>
              </div>
    </>
  );
}
