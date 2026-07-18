import type { CanvasController } from "../../controller/useCanvasController";
import { taskFlowStages } from "../../catalog";
import { ArrowRight } from "lucide-react";

export function TaskDefinitionTab({ controller }: { controller: CanvasController }) {
  const { activeStageKey, activeTab, availableTaskTypes, saveTaskDraft, selectFlowStage, selectTaskType, selectedCanvasVariant, selectedTaskType, selectedTaskTypeKey, setActiveTab, setDrawerTab, setNodeLibraryOpen } = controller;
  return (
    <>
      {activeTab === "definition" && (
                      <div className="task-tab-grid task-definition-grid">
                        <section className="task-tab-card wide task-type-card">
                          <div className="task-tab-card-head">
                            <span>数据流程模板</span>
                            <button onClick={saveTaskDraft}>保存模板草稿</button>
                          </div>
                          <strong>{selectedTaskType.name}</strong>
                          <p>{selectedTaskType.description}</p>
                          <div className="task-template-flowline" aria-label="流程模板方向链路">
                            {taskFlowStages.map((stage, index) => {
                              const nextStage = taskFlowStages[index + 1];
                              return (
                                <div key={stage.key} className="task-template-flow-unit">
                                  <button
                                    type="button"
                                    className={activeStageKey === stage.key ? "task-template-flow-card active" : "task-template-flow-card"}
                                    onClick={() => {
                                      selectFlowStage(stage.key);
                                      setActiveTab("flow");
                                    }}
                                  >
                                    <b>{index + 1}</b>
                                    <span className="task-template-stage-main">
                                      <strong>{stage.title}</strong>
                                      <em title={stage.product}>{stage.product}</em>
                                    </span>
                                    <span className="task-template-stage-meta">
                                      <i title={stage.dagsterObject}>{stage.dagsterObject}</i>
                                      <small title={stage.output}>{stage.output.split(",").slice(0, 2).join(", ")}{stage.output.includes(",") ? "..." : ""}</small>
                                    </span>
                                  </button>
                                  {nextStage && (
                                    <button
                                      type="button"
                                      className="task-template-flow-arrow"
                                      onClick={() => {
                                        selectFlowStage(nextStage.key);
                                        setActiveTab("flow");
                                      }}
                                      aria-label={`${stage.title} 输出到 ${nextStage.title}`}
                                      title={`${stage.output} → ${nextStage.title}`}
                                    >
                                      <ArrowRight size={14} />
                                      <span>{stage.edgeLabel}</span>
                                    </button>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                          <div className="task-type-list">
                            <div className="task-type-list-head" aria-hidden="true">
                              <span>状态</span>
                              <span>流程模板</span>
                              <span>负责人</span>
                              <span>SLA / 触发</span>
                              <span>可复用画布</span>
                            </div>
                            {availableTaskTypes.map((taskType) => (
                              <button
                                key={taskType.key}
                                type="button"
                                className={selectedTaskTypeKey === taskType.key ? "task-type-row active" : "task-type-row"}
                                onClick={() => selectTaskType(taskType.key)}
                              >
                                <b>{taskType.status}</b>
                                <strong>{taskType.name}</strong>
                                <span>{taskType.owner}</span>
                                <em>{taskType.sla}</em>
                                <i>{taskType.reusableCanvases}</i>
                              </button>
                              ))}
                            </div>
                          </section>
                        <section className="task-tab-card task-dagster-map-card">
                          <span>底层执行对象</span>
                          <strong>任务定义 + 资产选择</strong>
                          <p>流程模板发布后生成可执行任务；平台抽取生成外部数据源引用，智能处理生成处理资产，结果推送通过输出管理器和资产生成记录追踪。</p>
                          {[
                            ["流程模板", "任务定义", `${selectedTaskType.defaultCanvas}_job`],
                            ["编排版本", "图谱/资产选择", selectedCanvasVariant.version],
                            ["输入绑定", "外部数据源", "platform_session / audio_url_index"],
                            ["实验分流", "运行标签", "experiment_arm, canvas_variant"],
                            ["OutputSink", "IO Manager", "obs_audio_io_manager / platform_callback_io_manager"]
                          ].map(([product, dagster, value]) => (
                            <div key={product} className="dagster-object-row">
                              <b>{product}</b>
                              <strong>{dagster}</strong>
                              <em>{value}</em>
                            </div>
                          ))}
                        </section>
                        <section className="task-tab-card task-definition-side">
                          <span>当前边界</span>
                          <strong>配置数据流程，不创建业务数据</strong>
                          <p>这个页面只管理流程定义、编排版本、模型参数、调度、实验和输出绑定；数据资产仍由连接器导入或任务运行产生。</p>
                          <button onClick={() => setNodeLibraryOpen(true)}>配置默认画布节点</button>
                          <button onClick={() => setDrawerTab("plan")}>查看生成规则</button>
                        </section>
                      </div>
                    )}
    </>
  );
}
