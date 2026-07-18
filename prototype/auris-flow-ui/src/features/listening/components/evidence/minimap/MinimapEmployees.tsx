import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { GripVertical } from "lucide-react";

export function MinimapEmployees({ controller }: { controller: AnnotationMinimapController }) {
  const { clearEmployeeDragState, dragOverEmployee, draggedEmployee, employeeDragClickGuard, moveEmployeeLane, orderedLanes, releaseEmployeeDragClickGuard, setDragOverEmployee, setDraggedEmployee, setVisibleEmployees, visibleEmployees } = controller;
  return (
    <div className="mm-emps">
                <span className="mm-emps-l">本店员工</span>
                {orderedLanes.map((lane) => (
                  <button
                    key={lane.sub}
                    className={[
                      "mm-emp-chip",
                      visibleEmployees[lane.sub] ? "on" : "",
                      draggedEmployee === lane.sub ? "dragging" : "",
                      dragOverEmployee === lane.sub && draggedEmployee !== lane.sub ? "drop-target" : ""
                    ].join(" ")}
                    draggable
                    onDragStart={(event) => {
                      employeeDragClickGuard.current = true;
                      setDraggedEmployee(lane.sub);
                      event.dataTransfer.effectAllowed = "move";
                      event.dataTransfer.setData("text/plain", lane.sub);
                    }}
                    onDragOver={(event) => {
                      event.preventDefault();
                      event.dataTransfer.dropEffect = "move";
                      setDragOverEmployee(lane.sub);
                    }}
                    onDragLeave={() => {
                      setDragOverEmployee((current) => (current === lane.sub ? null : current));
                    }}
                    onDrop={(event) => {
                      event.preventDefault();
                      const sourceSub = event.dataTransfer.getData("text/plain") || draggedEmployee;
                      if (sourceSub) moveEmployeeLane(sourceSub, lane.sub);
                      clearEmployeeDragState();
                      releaseEmployeeDragClickGuard();
                    }}
                    onDragEnd={() => {
                      clearEmployeeDragState();
                      releaseEmployeeDragClickGuard();
                    }}
                    onClick={() => {
                      if (employeeDragClickGuard.current) {
                        return;
                      }
                      setVisibleEmployees((current) => ({ ...current, [lane.sub]: !current[lane.sub] }));
                    }}
                    title="拖动调整 minimap 行顺序，点击切换可见"
                  >
                    <GripVertical className="grip" size={12} aria-hidden="true" />
                    <span className="dot" style={{ background: `var(--${lane.hue === "teal" ? "cyan" : lane.hue})` }} />
                    <span className="nm">{lane.name}</span>
                    <span className="ct">{lane.score}</span>
                  </button>
                ))}
              </div>
  );
}
