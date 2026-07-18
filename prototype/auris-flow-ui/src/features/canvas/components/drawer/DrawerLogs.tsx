import type { CanvasController } from "../../controller/useCanvasController";


export function DrawerLogs({ controller }: { controller: CanvasController }) {
  const { drawerTab, runLogs } = controller;
  return (
    <>
      {drawerTab === "logs" && (
                  <section className="run-log-card">
                    <span>运行链路</span>
                    {runLogs.map(({ id, time, name, state }) => (
                      <button key={id}>
                        <b>{time}</b>
                        <strong>{name}</strong>
                        <em>{state}</em>
                      </button>
                    ))}
                  </section>
                )}
    </>
  );
}
