import type { CanvasController } from "../../controller/useCanvasController";
import { taskFlowStages } from "../../catalog";
import { ArrowRight } from "lucide-react";

export function AbstractFlowCanvas({ controller }: { controller: CanvasController }) {
  const { activeFlowStage, activePartitionKey, activeRunKey, activeStageKey, experimentMode, selectFlowStage, selectedTaskType, setDrawerTab } = controller;
  return (
    <>
      <div className="flow-abstraction-canvas" aria-label="任务流程抽象视图">
                      <section className="flow-story-panel">
                        <div className="flow-story-head">
                          <span>流程抽象</span>
                          <strong>{selectedTaskType.name}</strong>
                          <p>{selectedTaskType.description}</p>
                        </div>
                        <div className="flow-story-metrics">
                          {[
                            ["输入", "CRM / 订单 / PBX / S3"],
                            ["智能体", "参数生成 + 校验"],
                            ["模型链", "Audio Intelligence / Tagger / LLM Judge"],
                            ["闭环", "AB实验 + Human Loop + 导出"]
                          ].map(([label, value]) => (
                            <span key={label}>
                              <b>{label}</b>
                              <em>{value}</em>
                            </span>
                          ))}
                        </div>
                      </section>

                      <section className="flow-story-pipeline" aria-label="流程步骤">
                        {taskFlowStages.map((stage, index) => {
                          const nextStage = taskFlowStages[index + 1];
                          return (
                            <div key={stage.key} className="flow-story-unit">
                              <button
                                type="button"
                                className={activeStageKey === stage.key ? "flow-story-step active" : "flow-story-step"}
                                onClick={() => selectFlowStage(stage.key)}
                              >
                                <b>{index + 1}</b>
                                <strong>{stage.title}</strong>
                                <span>{stage.product}</span>
                                <em>{stage.chips.slice(0, 3).join(" · ")}</em>
                              </button>
                              {nextStage && (
                                <button
                                  type="button"
                                  className={`flow-story-link ${activeStageKey === nextStage.key ? "active" : ""}`}
                                  onClick={() => selectFlowStage(nextStage.key)}
                                  aria-label={`${stage.title} 输出到 ${nextStage.title}`}
                                >
                                  <ArrowRight size={14} />
                                  <span>{stage.edgeLabel}</span>
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </section>

                      <section className="flow-landing-panel" aria-label="执行映射摘要">
                        <div className="flow-landing-head">
                          <span>执行落地</span>
                          <strong>{activeFlowStage.dagsterObject}</strong>
                        </div>
                        <div className="flow-landing-rows">
                          {[
                            ["Job", `${selectedTaskType.defaultCanvas}_job`, "流程版本发布后生成"],
                            ["Stage", activeFlowStage.key, activeFlowStage.output],
                            ["RunKey", activeRunKey, activePartitionKey],
                            ["Gate", experimentMode, activeFlowStage.chips.join(" / ")]
                          ].map(([label, value, detail]) => (
                            <button key={label} type="button" className="flow-landing-row" onClick={() => setDrawerTab("plan")}>
                              <b>{label}</b>
                              <strong>{value}</strong>
                              <em>{detail}</em>
                            </button>
                          ))}
                        </div>
                      </section>
                    </div>
    </>
  );
}
