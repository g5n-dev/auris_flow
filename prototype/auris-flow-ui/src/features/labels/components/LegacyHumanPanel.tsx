import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { UserCheck } from "lucide-react";

export function LegacyHumanPanel({ controller }: { controller: LabelsController }) {
  const { activeReviewTask, renderHumanLoopWorkbench, reviewState, reviewTasks, setSelectedReviewId } = controller;
  return (
    <section className="module-panel label-human-panel">
            <PanelHeader title="Human Loop" subtitle="高风险标签、人审任务、接受/修改/拒绝回写" icon={<UserCheck size={16} />} />
            <div className="label-review-list">
              {reviewTasks.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  className={`label-review-card risk-${task.priority} ${activeReviewTask.id === task.id ? "selected" : ""}`}
                  onClick={() => setSelectedReviewId(task.id)}
                >
                  <div>
                    <span>{task.id}</span>
                    <b>{reviewState}</b>
                  </div>
                  <strong>{task.title}</strong>
                  <em>{task.type}</em>
                  <p>{task.detail}</p>
                </button>
              ))}
            </div>
            {renderHumanLoopWorkbench()}
          </section>
  );
}
