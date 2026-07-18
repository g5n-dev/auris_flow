import type { CanvasController } from "../../controller/useCanvasController";
import { backendRunStatusLabel, backendRunSucceeded, normalizeBackendRunStatus } from "../../../../shared/runtime/backendRunStatus";
import { loginRiskDagsterCompatibility } from "../../catalog";

export function TaskVersionsTab({ controller }: { controller: CanvasController }) {
  const { activeTab, backfillConfirmed, canvasAction, confirmBackfillGate, discardTaskChanges, draftState, publishTaskVersion, recoveredTaskVersion, rejectTaskVersionRelease, releaseHistoryDescription, renderTaskDagSnapshot, saveTaskDraft, savedTaskVersionId, scheduleMode, shortTrace, taskDraftValidation, taskPublishLabel, taskReleaseGate, validateDagsterCompatibility } = controller;
  return (
    <>
      {activeTab === "versions" && (
                      <div className="task-tab-grid">
                        {renderTaskDagSnapshot("release")}
                        <section className="task-tab-card wide">
                          <div className="task-tab-card-head">
                            <span>版本历史</span>
                            <button data-testid="task-version-publish" onClick={publishTaskVersion}>发布当前草稿</button>
                          </div>
                          {recoveredTaskVersion && savedTaskVersionId && (
                            <div className="operation-toast is-success" data-testid="recovered-task-version">
                              <strong>{savedTaskVersionId}</strong>
                              <span>
                                {String(recoveredTaskVersion.status ?? "unknown")} · hotword_pack_version_id {String(recoveredTaskVersion.hotword_pack_version_id ?? "missing")} · 必须经当前发布门禁人工审批
                              </span>
                            </div>
                          )}
                          {[
                            ["v3", draftState, releaseHistoryDescription],
                            ["v2", "已发布", "最近成功运行 / 资产目录正在使用"],
                            ["v1", "历史版本", "只读归档 / 可复制为新流程草稿"]
                          ].map(([version, state, desc]) => (
                            <button key={version} className="task-version-row">
                              <strong>{version}</strong>
                              <span>{state}</span>
                              <em>{desc}</em>
                            </button>
                          ))}
                        </section>
                        <section className="task-tab-card">
                          <span>草稿操作</span>
                          <button onClick={saveTaskDraft}>保存草稿</button>
                          <button onClick={discardTaskChanges}>放弃更改</button>
                        </section>
                        <section className="task-tab-card">
                          <span>发布影响</span>
                          <strong>仅生成新流程版本</strong>
                          <p>不会覆盖历史运行，也不会修改全局数据源；运行记录通过版本标签隔离。</p>
                        </section>
                        <section className={`task-tab-card wide task-validation-card ${taskDraftValidation.canPublish ? "ok" : "blocked"}`}>
                          <div className="task-tab-card-head">
                            <span>发布门禁汇总</span>
                            <b>{taskDraftValidation.canPublish ? "可发布" : "阻断发布"}</b>
                          </div>
                          <strong>{taskDraftValidation.summary}</strong>
                          <p>发布只读取这份 TaskDraftValidation：草稿、调度、Backfill、AB 主指标、守护指标、输出回写和映射状态必须一致。</p>
                          <div className="task-validation-counts">
                            <span className="blocker">
                              阻断项
                              <b>{taskDraftValidation.blockers.length}</b>
                            </span>
                            <span className="warning">
                              需人工确认
                              <b>{taskDraftValidation.warnings.length}</b>
                            </span>
                            <span className="pass">
                              可发布项
                              <b>{taskDraftValidation.passed.length}</b>
                            </span>
                          </div>
                          <div className="task-validation-list">
                            {[...taskDraftValidation.blockers, ...taskDraftValidation.warnings, ...taskDraftValidation.passed].map((item) => (
                              <div key={item.key} className={`task-validation-row ${item.severity}`}>
                                <b>{item.severity === "blocker" ? "阻断" : item.severity === "warning" ? "确认" : "通过"}</b>
                                <strong>{item.label}</strong>
                                <span>{item.detail}</span>
                              </div>
                            ))}
                          </div>
                          {scheduleMode === "一次性回填" && !backfillConfirmed && (
                            <button type="button" onClick={confirmBackfillGate}>
                              确认 Backfill 门禁
                            </button>
                          )}
                        </section>
                        <section className="task-tab-card wide task-release-gate-card">
                          <div className="task-tab-card-head">
                            <span>发布兼容性闸门</span>
                            <button onClick={validateDagsterCompatibility}>执行校验</button>
                          </div>
                          {taskReleaseGate && (
                            <div className="task-release-decision" data-status={normalizeBackendRunStatus(taskReleaseGate.status)}>
                              <span>发布运行</span>
                              <strong>{taskReleaseGate.id}</strong>
                              <em>{backendRunStatusLabel(taskReleaseGate.status)} · Trace {shortTrace(taskReleaseGate.trace_id)}</em>
                              <button type="button" data-testid="task-version-approve-release" onClick={publishTaskVersion} disabled={Boolean(canvasAction) || backendRunSucceeded(taskReleaseGate.status)}>
                                {taskPublishLabel}
                              </button>
                              {normalizeBackendRunStatus(taskReleaseGate.status) === "blocked" && (
                                <button type="button" className="danger" onClick={rejectTaskVersionRelease} disabled={Boolean(canvasAction)}>
                                  退回草稿
                                </button>
                              )}
                            </div>
                          )}
                          {loginRiskDagsterCompatibility.map((item) => (
                            <div key={item.item} className={`task-gate-row ${item.status === "兼容" ? "ok" : "warn"}`}>
                              <b>{item.status}</b>
                              <strong>{item.item}</strong>
                              <span>{item.business}</span>
                              <em>{item.detail}</em>
                            </div>
                          ))}
                        </section>
                      </div>
                    )}
    </>
  );
}
