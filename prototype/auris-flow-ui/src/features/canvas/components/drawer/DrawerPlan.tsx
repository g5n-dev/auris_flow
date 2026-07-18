import type { CanvasController } from "../../controller/useCanvasController";
import { executionStateMeta, loginRiskDagsterCompatibility, loginRiskScenarioPolicies } from "../../catalog";

export function DrawerPlan({ controller }: { controller: CanvasController }) {
  const { activeFlowStage, activeIntent, activeIntentKey, dagsterRunDraft, dagsterRunRequestRows, dagsterRuntimeRows, drawerTab, executionState, experimentMode, generateDagsterRunRequest, selectedCanvasVariant, selectedTaskType, setActiveTab, setDrawerTab, syncDagsterInputsFromCanvas, updateDagsterRunDraft, updateExecutionState } = controller;
  return (
    <>
      {drawerTab === "plan" && (
                  <>
                    <section className={`dagster-detail-card ${executionState}`}>
                      <div className="dagster-status-line">
                        <span>执行计划状态</span>
                        <strong>{executionStateMeta[executionState].label}</strong>
                      </div>
                      <div className="dagster-kv">
                        <span>流程模板</span>
                        <b>{selectedTaskType.key}</b>
                        <span>画布变体</span>
                        <b>{selectedCanvasVariant.key}</b>
                        <span>当前阶段</span>
                        <b>{activeFlowStage.key}</b>
                        <span>实验模式</span>
                        <b>{experimentMode}</b>
                        <span>底层 Job</span>
                        <b>{selectedTaskType.defaultCanvas}_job</b>
                        <span>阶段输出</span>
                        <b>{activeFlowStage.output}</b>
                        <span>阶段对象</span>
                        <b>{activeFlowStage.dagsterObject}</b>
                        <span>阶段说明</span>
                        <b>{activeFlowStage.product}</b>
                        <span>执行定义</span>
                        <b>{activeIntent.taskId}</b>
                        <span>输出资产</span>
                        <b>{activeIntent.output}</b>
                        <span>处理步骤</span>
                        <b>{activeIntent.step}</b>
                        <span>触发方式</span>
                        <b>{activeIntent.trigger}</b>
                        <span>运行范围</span>
                        <b>{activeIntent.scope}</b>
                      </div>
                      <div className="dagster-input-form">
                        <div className="dagster-input-head">
                          <span>执行输入</span>
                          <button type="button" onClick={syncDagsterInputsFromCanvas}>
                            从画布同步输入
                          </button>
                        </div>
                        <label>
                          Job / Graph
                          <input value={dagsterRunDraft.jobName} onChange={(event) => updateDagsterRunDraft("jobName", event.target.value)} />
                        </label>
                        <label>
                          Partition Key
                          <input value={dagsterRunDraft.partitionKey} onChange={(event) => updateDagsterRunDraft("partitionKey", event.target.value)} />
                        </label>
                        <label>
                          Asset Selection
                          <textarea value={dagsterRunDraft.assetSelection} onChange={(event) => updateDagsterRunDraft("assetSelection", event.target.value)} />
                        </label>
                        <label>
                          Run Tags
                          <textarea value={dagsterRunDraft.runTags} onChange={(event) => updateDagsterRunDraft("runTags", event.target.value)} />
                        </label>
                        <div className="dagster-inline-fields">
                          <label>
                            重试
                            <input value={dagsterRunDraft.maxRetries} onChange={(event) => updateDagsterRunDraft("maxRetries", event.target.value)} />
                          </label>
                          <label>
                            并发
                            <input value={dagsterRunDraft.concurrencyLimit} onChange={(event) => updateDagsterRunDraft("concurrencyLimit", event.target.value)} />
                          </label>
                        </div>
                        <label>
                          失败策略
                          <select value={dagsterRunDraft.failurePolicy} onChange={(event) => updateDagsterRunDraft("failurePolicy", event.target.value)}>
                            <option value="失败 2 次后进入人工复核队列">失败 2 次后进入人工复核队列</option>
                            <option value="失败后跳过当前分区并记录 SkipReason">失败后跳过当前分区并记录 SkipReason</option>
                            <option value="阻断发布并生成配置复核任务">阻断发布并生成配置复核任务</option>
                          </select>
                        </label>
                        <label>
                          Run Config JSON
                          <textarea className="run-config-json" value={dagsterRunDraft.runConfigJson} onChange={(event) => updateDagsterRunDraft("runConfigJson", event.target.value)} />
                        </label>
                        <label>
                          运行原因
                          <input value={dagsterRunDraft.reason} onChange={(event) => updateDagsterRunDraft("reason", event.target.value)} />
                        </label>
                      </div>
                      <div className="dagster-run-request-preview">
                        <span>运行请求预览</span>
                        {dagsterRunRequestRows.map(([key, value]) => (
                          <div key={key}>
                            <b>{key}</b>
                            <strong>{value}</strong>
                          </div>
                        ))}
                      </div>
                      <div className="dagster-actions">
                        <button
                          onClick={() => {
                            setActiveTab("versions");
                            setDrawerTab("overview");
                          }}
                        >
                          查看发布门禁
                        </button>
                        <button onClick={generateDagsterRunRequest}>生成运行请求</button>
                        <button onClick={() => updateExecutionState("running")}>按当前输入运行</button>
                        <button
                          onClick={() => {
                            updateExecutionState("success");
                            setDrawerTab("logs");
                          }}
                        >
                          同步输出资产
                        </button>
                      </div>
                    </section>
                    {activeIntentKey === "review" && (
                      <>
                      <section className="dagster-contract-card">
                        <div className="dagster-binding-head">
                          <span>兼容性检查</span>
                          <strong>{loginRiskDagsterCompatibility.filter((item) => item.status === "兼容").length}/{loginRiskDagsterCompatibility.length}</strong>
                        </div>
                        {loginRiskDagsterCompatibility.map((item) => (
                          <button key={item.item} type="button" className={`dagster-contract-row ${item.status === "兼容" ? "ok" : "warn"}`}>
                            <b>{item.status}</b>
                            <strong>{item.item}</strong>
                            <span>{item.dagster}</span>
                            <em>{item.detail}</em>
                          </button>
                        ))}
                      </section>
                      <section className="dagster-contract-card">
                        <div className="dagster-binding-head">
                          <span>运行接口映射</span>
                          <strong>BFF → 执行层</strong>
                        </div>
                        {dagsterRuntimeRows.map(([label, value, detail]) => (
                          <button key={label} type="button" className="dagster-runtime-row">
                            <span>{label}</span>
                            <strong>{value}</strong>
                            <em>{detail}</em>
                          </button>
                        ))}
                      </section>
                      <section className="dagster-contract-card">
                        <div className="dagster-binding-head">
                          <span>场景策略与 Human Loop</span>
                          <strong>Human Loop</strong>
                        </div>
                        {loginRiskScenarioPolicies.map(([name, policy]) => (
                          <button key={name} type="button" className="dagster-runtime-row">
                            <span>{name}</span>
                            <strong>{policy}</strong>
                          </button>
                        ))}
                      </section>
                      </>
                    )}
                  </>
                )}
    </>
  );
}
