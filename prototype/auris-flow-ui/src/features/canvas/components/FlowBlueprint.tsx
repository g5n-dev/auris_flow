import type { CanvasController } from "../controller/useCanvasController";
import { taskFlowStages } from "../catalog";
import { ArrowRight, GitBranch } from "lucide-react";

export function FlowBlueprint({ controller }: { controller: CanvasController }) {
  const { activeFlowStage, activeStageKey, availableTaskTypes, canvasLevel, isFlowTab, selectFlowStage, selectTaskType, selectedTaskType, selectedTaskTypeKey } = controller;
  return (
    <>
      {isFlowTab && canvasLevel === "nodes" && (
                <section className="flow-blueprint-panel" aria-label="数据流程用户故事">
                  <div className="flow-template-summary">
                    <span>当前流程</span>
                    <strong>{selectedTaskType.name}</strong>
                    <p>{selectedTaskType.description}</p>
                    <div className="flow-template-pills" aria-label="切换流程模板">
                      {availableTaskTypes.map((taskType) => (
                        <button
                          key={taskType.key}
                          type="button"
                          className={selectedTaskTypeKey === taskType.key ? "active" : ""}
                          onClick={() => selectTaskType(taskType.key)}
                          title={`${taskType.status}: ${taskType.description}`}
                        >
                          {taskType.name}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="flow-stage-rail" aria-label="有向流程阶段">
                    {taskFlowStages.map((stage, index) => {
                      const nextStage = taskFlowStages[index + 1];
                      return (
                        <div key={stage.key} className="flow-stage-unit">
                          <button
                            type="button"
                            className={activeStageKey === stage.key ? "flow-stage-step active" : "flow-stage-step"}
                            title={`${stage.title}: ${stage.product}`}
                            data-tooltip={`${stage.title}: ${stage.product}`}
                            onClick={() => selectFlowStage(stage.key)}
                          >
                            <b>{index + 1}</b>
                            <strong>{stage.title}</strong>
                            <span>{stage.chips.slice(0, 2).join(" / ")}</span>
                          </button>
                          {nextStage && (
                            <button
                              type="button"
                              className={`flow-stage-arrow ${activeStageKey === nextStage.key ? "active" : ""}`}
                              onClick={() => selectFlowStage(nextStage.key)}
                              aria-label={`${stage.title} 到 ${nextStage.title}`}
                              title={`${stage.output} → ${nextStage.title}`}
                            >
                              <ArrowRight size={15} />
                              <span>{stage.edgeLabel}</span>
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <div className="flow-stage-detail-card">
                    <span>{activeFlowStage.title}</span>
                    <strong>{activeFlowStage.product}</strong>
                    <p>{activeFlowStage.detail}</p>
                    <small className="flow-stage-code-line" title={`${activeFlowStage.dagsterObject} · ${selectedTaskType.defaultCanvas}_job`}>
                      <GitBranch size={12} />
                      <code>{activeFlowStage.dagsterObject}</code>
                      <i>·</i>
                      <code>{selectedTaskType.defaultCanvas}_job</code>
                    </small>
                    <div className="flow-stage-chip-list">
                      {activeFlowStage.chips.map((chip) => (
                        <b key={chip}>{chip}</b>
                      ))}
                    </div>
                    <em className="flow-stage-output-line" title={activeFlowStage.output}>{activeFlowStage.output}</em>
                  </div>
                </section>
              )}
    </>
  );
}
