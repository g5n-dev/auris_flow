import type { CanvasController } from "../../controller/useCanvasController";


export function ExecutionMappingStrip({ controller }: { controller: CanvasController }) {
  const { canvasLevel, displayExecutionDefinition, isFlowTab, selectedDagsterBinding, selectedNode } = controller;
  return (
    <>
      {isFlowTab && canvasLevel === "nodes" && (
                <div className="dagster-mapping-strip">
                  <span>执行绑定</span>
                  <strong>{selectedNode.id}</strong>
                  <b>{displayExecutionDefinition(selectedDagsterBinding.definition)}</b>
                  <b>{selectedDagsterBinding.op}</b>
                  <b>{selectedDagsterBinding.assetKey}</b>
                  <b>{selectedDagsterBinding.ioManager}</b>
                </div>
              )}
    </>
  );
}
