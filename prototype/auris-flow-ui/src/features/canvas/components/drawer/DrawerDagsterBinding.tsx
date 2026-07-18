import type { CanvasController } from "../../controller/useCanvasController";


export function DrawerDagsterBinding({ controller }: { controller: CanvasController }) {
  const { displayExecutionDefinition, selectedDagsterBinding } = controller;
  return (
    <>
      <section className="dagster-binding-card">
                  <div className="dagster-binding-head">
                    <span>底层执行映射</span>
                    <strong>{displayExecutionDefinition(selectedDagsterBinding.definition)}</strong>
                  </div>
                  <div className="dagster-kv">
                    <span>Op</span>
                    <b>{selectedDagsterBinding.op}</b>
                    <span>Asset Key</span>
                    <b>{selectedDagsterBinding.assetKey}</b>
                    <span>IO Manager</span>
                    <b>{selectedDagsterBinding.ioManager}</b>
                    <span>Partition</span>
                    <b>{selectedDagsterBinding.partition}</b>
                    <span>Deps</span>
                    <b>{selectedDagsterBinding.deps.join(" / ") || "none"}</b>
                  </div>
                </section>
    </>
  );
}
