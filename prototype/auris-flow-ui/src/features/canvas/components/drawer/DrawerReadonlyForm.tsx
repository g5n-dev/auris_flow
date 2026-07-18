import type { CanvasController } from "../../controller/useCanvasController";


export function DrawerReadonlyForm({ controller }: { controller: CanvasController }) {
  const { selectedNodeContext } = controller;
  return (
    <>
      <section className="drawer-form">
                  {selectedNodeContext.fields.map(([label, value]) => (
                    <label key={label}>
                      {label}
                      <input value={value} readOnly />
                    </label>
                  ))}
                </section>
    </>
  );
}
