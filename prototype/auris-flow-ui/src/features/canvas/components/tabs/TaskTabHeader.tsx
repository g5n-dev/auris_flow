import type { CanvasController } from "../../controller/useCanvasController";


export function TaskTabHeader({ controller }: { controller: CanvasController }) {
  const { activeSection, draftState } = controller;
  return (
    <>
      <div className="task-tab-page-head">
                      <div>
                        <span>{activeSection.label}</span>
                        <strong>{activeSection.title}</strong>
                        <p>{activeSection.helper}</p>
                      </div>
                      <b>{draftState}</b>
                    </div>
    </>
  );
}
