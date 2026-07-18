import type { CanvasController } from "../../controller/useCanvasController";
import { taskCanvasVariants } from "../../catalog";
import { Plus } from "lucide-react";

export function TaskCanvasesTab({ controller }: { controller: CanvasController }) {
  const { activeTab, renderTaskDagSnapshot, selectedCanvasVariant, selectedCanvasVariantKey, selectedTaskType, setDrawerTab, setNodeLibraryOpen, setSelectedCanvasVariantKey, setSelectedNodeId, validateDagsterCompatibility } = controller;
  return (
    <>
      {activeTab === "canvases" && (
                      <div className="task-tab-grid task-canvas-grid">
                        <section className="task-tab-card wide">
                          <div className="task-tab-card-head">
                            <span>编排版本</span>
                            <button onClick={() => setNodeLibraryOpen(true)}>
                              <Plus size={13} />
                              新建编排版本
                            </button>
                          </div>
                          <div className="task-canvas-version-list">
                            {taskCanvasVariants.map((variant) => (
                              <button
                                key={variant.key}
                                type="button"
                                className={selectedCanvasVariantKey === variant.key ? "task-canvas-version active" : "task-canvas-version"}
                                onClick={() => {
                                  setSelectedCanvasVariantKey(variant.key);
                                  setSelectedNodeId("dagster");
                                  setDrawerTab("plan");
                                }}
                              >
                                <b>{variant.role}</b>
                                <strong>{variant.name}</strong>
                                <span>{variant.status}</span>
                                <em>{variant.changed}</em>
                                <i>{variant.traffic}</i>
                              </button>
                            ))}
                          </div>
                        </section>
                        <section className="task-tab-card task-canvas-detail-card">
                          <span>当前编排</span>
                          <strong>{selectedCanvasVariant.name}</strong>
                          <p>{selectedCanvasVariant.changed}</p>
                          <div className="task-canvas-kv">
                            <span>版本</span>
                            <b>{selectedCanvasVariant.version}</b>
                            <span>节点数</span>
                            <b>{selectedCanvasVariant.nodes}</b>
                            <span>负责人</span>
                            <b>{selectedCanvasVariant.owner}</b>
                            <span>流量</span>
                            <b>{selectedCanvasVariant.traffic}</b>
                            <span>闸门</span>
                            <b>{selectedCanvasVariant.guardrail}</b>
                          </div>
                          <button onClick={() => setDrawerTab("plan")}>查看执行快照</button>
                        </section>
                        {renderTaskDagSnapshot("embedded")}
                        <section className="task-tab-card wide task-dagster-substrate-card">
                          <div className="task-tab-card-head">
                            <span>执行生成快照</span>
                            <button onClick={validateDagsterCompatibility}>校验兼容性</button>
                          </div>
                          {[
                            ["Code Location", "auris_flow.jobs.evidence_dataflows", "流程发布后写入可部署定义，不直接操作底层编排 UI。"],
                            ["Job", `${selectedTaskType.defaultCanvas}_job`, "由当前编排生成 graph/job，版本随 FlowVersion 固化。"],
                            ["Asset Selection", "platform_session + identity_map + transcript + label_candidates + review_queue", "每个输入、模型输出和导出结果都能追踪资产血缘。"],
                            ["Partitions", "tenant_id × store_id × business_date", "回填、重跑和实验对比都按分区选择。"],
                            ["Tags", "flow_template, canvas_variant, model_version, experiment_arm, tenant_id", "运行记录、成本、实验指标都靠 tags 聚合。"]
                          ].map(([label, value, detail]) => (
                            <div key={label} className="task-runtime-row">
                              <b>{label}</b>
                              <strong>{value}</strong>
                              <em>{detail}</em>
                            </div>
                          ))}
                        </section>
                      </div>
                    )}
    </>
  );
}
