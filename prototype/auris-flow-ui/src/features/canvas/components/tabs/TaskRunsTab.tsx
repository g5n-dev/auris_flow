import type { CanvasController } from "../../controller/useCanvasController";
import { executionStateMeta } from "../../catalog";

export function TaskRunsTab({ controller }: { controller: CanvasController }) {
  const { activePartitionKey, activeRunConfig, activeRunKey, activeTab, executionState, runLogs, updateExecutionState, validateDagsterCompatibility } = controller;
  return (
    <>
      {activeTab === "runs" && (
                      <div className="task-tab-grid task-runs-grid">
                        <section className="task-tab-card wide task-run-request-card">
                          <div className="task-tab-card-head">
                            <span>运行请求预览</span>
                            <button onClick={validateDagsterCompatibility}>重新校验</button>
                          </div>
                          <div className="task-run-request-grid">
                            <div>
                              <span>BFF 接口</span>
                              <strong>POST /api/v1/task-runs</strong>
                            </div>
                            <div>
                              <span>内部执行</span>
                              <strong>执行入口 launchRun</strong>
                            </div>
                            <div>
                              <span>run_key</span>
                              <strong>{activeRunKey}</strong>
                            </div>
                            <div>
                              <span>partition_key</span>
                              <strong>{activePartitionKey}</strong>
                            </div>
                          </div>
                          <div className="task-run-config">
                            {activeRunConfig.map(([key, value]) => (
                              <span key={key}>
                                <b>{key}</b>
                                <em>{value}</em>
                              </span>
                            ))}
                          </div>
                        </section>
                        <section className={`task-tab-card task-runs-log-card state-${executionState}`}>
                          <div className="task-tab-card-head">
                            <span>最近运行</span>
                            <b>{executionStateMeta[executionState].label}</b>
                          </div>
                          {runLogs.map(({ id, time, name, state }) => (
                            <div key={id} className="task-run-row">
                              <b>{time}</b>
                              <strong>{name}</strong>
                              <em>{state}</em>
                            </div>
                          ))}
                        </section>
                        <section className="task-tab-card task-runs-action-card">
                          <span>操作</span>
                          <button onClick={() => updateExecutionState("running")}>运行一次</button>
                          <button onClick={() => updateExecutionState("success")}>标记输出同步</button>
                        </section>
                        <section className="task-tab-card task-runs-output-card">
                          <span>输出资产</span>
                          <strong>证据包 / 标签结果 / 导出清单</strong>
                          <p>{executionState === "success" ? "已写入资产目录并记录资产生成事件" : "等待流程成功后同步"}</p>
                        </section>
                      </div>
                    )}
    </>
  );
}
