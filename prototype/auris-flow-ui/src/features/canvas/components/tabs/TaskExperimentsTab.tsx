import type { CanvasController } from "../../controller/useCanvasController";
import { backendRunStatusLabel, normalizeBackendRunStatus } from "../../../../shared/runtime/backendRunStatus";
import { experimentMetricContext, experimentMetricLineage } from "../../catalog";
import { BarChart3, Check, Database, Eye, GitBranch, Pause, Play, Plus, ShieldCheck, Sparkles, UploadCloud } from "lucide-react";
import { ExperimentAnalysisSummary } from "./ExperimentAnalysisSummary";
import { ExperimentConfigPanel } from "./ExperimentConfigPanel";

export function TaskExperimentsTab({ controller }: { controller: CanvasController }) {
  const { activePartitionKey, activeRunKey, activeTab, addMetricToObservation, availableExperimentMetrics, canvasAction, computeTaskExperimentMetrics, controlledExperiment, createTaskControlledExperiment, decideTaskControlledExperiment, displayedExperimentArms, displayedExperimentObservations, experimentActionPending, experimentLoading, experimentMode, experimentSubjectKey, generateExperimentMetricDraft, latestExperimentSnapshot, metricDraftState, publishTaskVersion, refreshMetricObservation, retryTaskExperimentRelease, runTaskOnce, saveMetricAsReleaseGate, sceneBinding, selectExperimentMetric, selectedCanvasVariant, selectedCanvasVariantKey, selectedExperimentMetric, setDrawerTab, setExperimentSubjectKey, setSelectedCanvasVariantKey, shortTrace, snapshotMetricRows, startTaskControlledExperiment, taskProductionReleaseHead, taskPublishLabel, taskReleaseGate, validateDagsterCompatibility } = controller;
  return (
    <>
      {activeTab === "experiments" && (
                      <div className="task-tab-grid task-experiment-grid">
                        <section className="task-tab-card task-experiment-state-card">
                          <div className="task-tab-card-head">
                            <span>实验状态</span>
                            <b>{experimentMode}</b>
                          </div>
                          <div className="task-schedule-switch">
                            {["未创建", "草稿", "灰度中", "暂停", "已决策"].map((mode) => (
                              <button key={mode} type="button" disabled className={experimentMode === mode ? "active" : ""}>
                                {mode}
                              </button>
                            ))}
                          </div>
                          <p>实验状态由后端事实驱动。同一分流单元稳定命中对照或候选 TaskVersion，曝光与指标结果不可变，不覆盖历史运行。</p>
                          <ExperimentConfigPanel controller={controller} />
                          {experimentLoading ? (
                            <div className="operation-toast is-pending"><strong>正在读取实验</strong><span>同步实验设计、样本计数和最新指标快照。</span></div>
                          ) : controlledExperiment ? (
                            <>
                              <div className="metric-context-strip">
                                <span><b>实验 ID</b><em>{controlledExperiment.experiment_id}</em></span>
                                <span><b>设计 SHA</b><em>{controlledExperiment.design_sha256.slice(0, 12)}</em></span>
                                <span><b>曝光</b><em>{controlledExperiment.counts.exposures}</em></span>
                                <span><b>指标结果</b><em>{controlledExperiment.counts.outcomes}</em></span>
                              </div>
                              {controlledExperiment.status === "running" && (
                                <label className="task-experiment-subject-field">
                                  <span>试运行分流单元</span>
                                  <input
                                    value={experimentSubjectKey}
                                    onChange={(event) => setExperimentSubjectKey(event.target.value)}
                                    placeholder={`${controlledExperiment.allocation_unit} ID`}
                                    aria-label="实验试运行分流单元 ID"
                                  />
                                  <em>仅用于稳定 HMAC 分桶；原始 ID 不落库，切换画布不会改变 arm。</em>
                                  <button type="button" disabled={Boolean(canvasAction)} onClick={runTaskOnce}>
                                    <Play size={14} />{canvasAction === "run" ? "分流中" : "运行该样本"}
                                  </button>
                                </label>
                              )}
                            </>
                          ) : (
                            <div className="operation-toast is-idle"><strong>尚未创建受控实验</strong><span>先选择 SceneProfile 指标，并准备一个已发布对照版本和一个冻结候选版本。</span></div>
                          )}
                          <div className="metric-definition-actions">
                            {!controlledExperiment && (
                              <button type="button" disabled={Boolean(experimentActionPending) || experimentLoading} onClick={createTaskControlledExperiment}>
                                <Plus size={14} />{experimentActionPending === "create" ? "创建中" : "创建实验"}
                              </button>
                            )}
                            {controlledExperiment?.status === "draft" && (
                              <button type="button" disabled={Boolean(experimentActionPending)} onClick={startTaskControlledExperiment}>
                                <Play size={14} />{experimentActionPending === "start" ? "启动中" : "启动确定性分流"}
                              </button>
                            )}
                            {controlledExperiment && ["running", "paused", "stopped"].includes(controlledExperiment.status) && (
                              <button type="button" disabled={Boolean(experimentActionPending)} onClick={computeTaskExperimentMetrics}>
                                <BarChart3 size={14} />{experimentActionPending === "compute" ? "计算中" : "生成指标快照"}
                              </button>
                            )}
                            {controlledExperiment?.status === "decided" && controlledExperiment.decisions?.[0]?.decision === "promote_candidate" && (
                              taskReleaseGate ? (
                                <button type="button" disabled={Boolean(canvasAction) || Boolean(experimentActionPending)} onClick={publishTaskVersion}>
                                  <UploadCloud size={14} />{taskPublishLabel}
                                </button>
                              ) : (
                                <button type="button" disabled={Boolean(experimentActionPending)} onClick={retryTaskExperimentRelease}>
                                  <UploadCloud size={14} />{experimentActionPending === "release-gate" ? "创建中" : "创建发布门禁"}
                                </button>
                              )
                            )}
                          </div>
                          {controlledExperiment?.status === "decided" && controlledExperiment.decisions?.[0]?.decision === "promote_candidate" && (
                            <div className="task-release-decision" data-status={taskReleaseGate ? normalizeBackendRunStatus(taskReleaseGate.status) : "pending"}>
                              <span>晋级后发布</span>
                              <strong>{typeof taskProductionReleaseHead?.active_task_version_id === "string" ? taskProductionReleaseHead.active_task_version_id : controlledExperiment.candidate_task_version_id}</strong>
                              <em>
                                {taskProductionReleaseHead
                                  ? `生产版本头第 ${String(taskProductionReleaseHead.generation ?? "—")} 代 · Trace ${shortTrace(String(taskProductionReleaseHead.trace_id ?? taskReleaseGate?.trace_id ?? ""))}`
                                  : taskReleaseGate
                                    ? `${backendRunStatusLabel(taskReleaseGate.status)} · 仍需独立管理员审批`
                                    : "实验晋级已记录，等待创建独立发布门禁"}
                              </em>
                            </div>
                          )}
                          <div className="task-experiment-arms">
                            {displayedExperimentArms.map((arm) => (
                              <button
                                key={arm.arm}
                                type="button"
                                className={arm.arm === "B" && selectedCanvasVariantKey === "candidate-v4" ? "task-experiment-arm active" : "task-experiment-arm"}
                                onClick={() => {
                                  if (arm.arm === "A") setSelectedCanvasVariantKey("stable-v3");
                                  if (arm.arm === "B") setSelectedCanvasVariantKey("candidate-v4");
                                  setDrawerTab("plan");
                                }}
                              >
                                <b>{arm.arm}</b>
                                <strong>{arm.canvas}</strong>
                                <span>{arm.traffic}</span>
                                <em>{arm.assignment}</em>
                                <i>{arm.writeback}</i>
                              </button>
                            ))}
                          </div>
                        </section>
                        <section className="task-tab-card wide task-experiment-ai-card">
                          <div className="task-tab-card-head">
                            <span>AI 创建指标</span>
                            <button
                              type="button"
                              onClick={generateExperimentMetricDraft}
                            >
                              <Sparkles size={14} />
                              生成指标草稿
                            </button>
                          </div>
                          <div className="metric-context-strip">
                            {experimentMetricContext.map(([label, value]) => (
                              <span key={label}>
                                <b>{label}</b>
                                <em>{value}</em>
                              </span>
                            ))}
                          </div>
                          <div className="metric-creator-layout">
                            <div className="ai-metric-list">
                              {availableExperimentMetrics.map((metric) => (
                                <button
                                  key={metric.key}
                                  type="button"
                                  className={selectedExperimentMetric.key === metric.key ? "ai-metric-option active" : "ai-metric-option"}
                                  onClick={() => selectExperimentMetric(metric.key)}
                                >
                                  <b>{metric.category}</b>
                                  <strong>{metric.name}</strong>
                                  <span>{metric.source}</span>
                                  <em>{metric.status}</em>
                                  <i>
                                    <span style={{ width: `${metric.confidence}%` }} />
                                  </i>
                                </button>
                              ))}
                            </div>
                            <div className="metric-definition-panel">
                              <div className="metric-definition-head">
                                <span>{selectedExperimentMetric.category} / {selectedExperimentMetric.layer}</span>
                                <strong>{selectedExperimentMetric.name}</strong>
                                <b>{metricDraftState}</b>
                              </div>
                              <p>{selectedExperimentMetric.reason}</p>
                              <div className="metric-formula-box">
                                <span>生成口径</span>
                                <strong>{selectedExperimentMetric.formula}</strong>
                                <em>{selectedExperimentMetric.sql} · {selectedExperimentMetric.window}</em>
                              </div>
                              <div className="metric-event-chips">
                                {selectedExperimentMetric.events.map((event) => (
                                  <span key={event}>
                                    <Database size={12} />
                                    {event}
                                  </span>
                                ))}
                              </div>
                              <div className="metric-guardrail-list">
                                {selectedExperimentMetric.guardrails.map((guardrail) => (
                                  <span key={guardrail}>
                                    <ShieldCheck size={13} />
                                    {guardrail}
                                  </span>
                                ))}
                              </div>
                              <div className="metric-risk-note">
                                <b>发布动作</b>
                                <span>{selectedExperimentMetric.action}</span>
                                <em>{selectedExperimentMetric.risk}</em>
                              </div>
                              <div className="metric-definition-actions">
                                <button type="button" onClick={addMetricToObservation}>
                                  <BarChart3 size={14} />
                                  加入观测看板
                                </button>
                                <button
                                  type="button"
                                  data-action-key="gate-metric"
                                  onClick={saveMetricAsReleaseGate}
                                >
                                  <ShieldCheck size={14} />
                                  保存为发布闸门
                                </button>
                                <button type="button" onClick={validateDagsterCompatibility}>
                                  <GitBranch size={14} />
                                  生成 SQL / 资产口径
                                </button>
                              </div>
                            </div>
                          </div>
                        </section>
                        <section className="task-tab-card wide task-experiment-observe-card">
                          <div className="task-tab-card-head">
                            <span>指标观测</span>
                            <button
                              type="button"
                              disabled={Boolean(experimentActionPending) || controlledExperiment?.status === "draft" || controlledExperiment?.status === "decided"}
                              onClick={controlledExperiment ? computeTaskExperimentMetrics : refreshMetricObservation}
                            >
                              <Eye size={14} />
                              {experimentActionPending === "compute" ? "计算中" : controlledExperiment ? "生成指标快照" : "刷新观测"}
                            </button>
                          </div>
                          {displayedExperimentObservations.length ? (
                            <div className="metric-observe-grid">
                            {displayedExperimentObservations.map((card) => (
                              <button key={card.label} type="button" className={`metric-observe-card ${card.tone}`}>
                                <span>{card.label}</span>
                                <strong>{card.value}</strong>
                                <b>{card.state}</b>
                                <em>{card.compare}</em>
                                <p>{card.detail}</p>
                                <div className="metric-trend-bars" aria-hidden="true">
                                  {card.trend.map((height, index) => (
                                    <i key={`${card.label}-${index}`} style={{ height: `${height}%` }} />
                                  ))}
                                </div>
                              </button>
                            ))}
                            </div>
                          ) : (
                            <div className="operation-toast is-idle">
                              <strong>尚无指标快照</strong>
                              <span>{controlledExperiment?.status === "draft" ? "启动实验并产生曝光结果后再计算。" : "等待曝光与指标结果写入后生成可审计快照。"}</span>
                            </div>
                          )}
                          <div className="metric-lineage-list">
                            {experimentMetricLineage.map(([stage, asset, detail]) => (
                              <div key={stage} className="metric-lineage-row">
                                <b>{stage}</b>
                                <strong>{asset}</strong>
                                <span>{detail}</span>
                              </div>
                            ))}
                          </div>
                        </section>
                        <section className="task-tab-card task-dagster-map-card">
                          <span>运行标签</span>
                          <strong>每个实验运行必须可追踪</strong>
                          <p>运行启动时注入实验、画布、模型、指标和观测窗口。指标从运行记录与资产物化事件聚合，保证发布决策可复算。</p>
                          {[
                            ["experiment_id", controlledExperiment?.experiment_id ?? "尚未创建"],
                            ["experiment_arm", controlledExperiment ? "由后端稳定分桶，运行回执返回" : "尚未创建"],
                            ["variant_dimension", controlledExperiment?.variant_dimension ?? "创建前待冻结"],
                            ["variant_diff_sha", controlledExperiment?.variant_diff_sha256.slice(0, 12) ?? "创建前待冻结"],
                            ["control_bundle", controlledExperiment?.arms.find((arm) => arm.arm_key === "control")?.task_version_binding_sha256?.slice(0, 12) ?? "待冻结"],
                            ["candidate_bundle", controlledExperiment?.arms.find((arm) => arm.arm_key === "candidate")?.task_version_binding_sha256?.slice(0, 12) ?? "待冻结"],
                            ["primary_metric_id", selectedExperimentMetric.key],
                            ["guardrail_set", controlledExperiment?.guardrails.map((item) => item.metric_key).join(" + ") || "未配置"],
                            ["scene_profile_version_id", controlledExperiment?.scene_profile_version_id ?? sceneBinding?.scene_profile_version_id ?? "未绑定"],
                            ["experiment_design_sha", controlledExperiment?.design_sha256.slice(0, 12) ?? "未冻结"],
                            ["metric_window", selectedExperimentMetric.window],
                            ["partition_key", activePartitionKey],
                            ["run_key", activeRunKey]
                          ].map(([key, value]) => (
                            <div key={key} className="dagster-object-row">
                              <b>{key}</b>
                              <strong>{value}</strong>
                            </div>
                          ))}
                        </section>
                        <section className="task-tab-card wide task-experiment-metrics">
                          <div className="task-tab-card-head">
                            <span>已观测指标</span>
                            <button type="button" onClick={validateDagsterCompatibility}>校验发布闸门</button>
                          </div>
                          <div className="metric-selected-strip">
                            <span>
                              <b>当前主指标</b>
                              <em>{selectedExperimentMetric.name}</em>
                            </span>
                            <span>
                              <b>口径资产</b>
                              <em>{selectedExperimentMetric.sql}</em>
                            </span>
                            <span>
                              <b>状态</b>
                              <em>{metricDraftState}</em>
                            </span>
                          </div>
                          <ExperimentAnalysisSummary controller={controller} />
                          <div className="task-experiment-table">
                            <div className="task-experiment-table-head">
                              <span>指标</span>
                              <span>A 主线</span>
                              <span>B 候选</span>
                              <span>差异</span>
                              <span>状态</span>
                            </div>
                            {snapshotMetricRows.map(([metric, aValue, bValue, delta, status]) => (
                              <button key={metric} type="button" className={["fail", "insufficient_sample", "hold"].includes(status) ? "task-experiment-metric warn" : "task-experiment-metric"}>
                                <strong>{metric}</strong>
                                <span>{aValue}</span>
                                <span>{bValue}</span>
                                <b>{delta}</b>
                                <em>{status}</em>
                              </button>
                            ))}
                          </div>
                          {!snapshotMetricRows.length && (
                            <div className="operation-toast is-idle">
                              <strong>没有可决策指标</strong>
                              <span>实验结果必须来自后端曝光与 outcome 事实；当前不会用页面 mock 数值替代。</span>
                            </div>
                          )}
                        </section>
                        <section className="task-tab-card task-definition-side">
                          <span>晋级规则</span>
                          <strong>指标闭环才允许发布</strong>
                          <p>主指标来自 {selectedExperimentMetric.sql}。只有主指标达标、守护指标不退化、样本量满足且事件链路可复算，才允许候选画布晋级。</p>
                          <button
                            type="button"
                            disabled={Boolean(experimentActionPending) || latestExperimentSnapshot?.verdict !== "promote" || controlledExperiment?.status === "decided"}
                            onClick={() => void decideTaskControlledExperiment("promote_candidate")}
                          >
                            <Check size={14} />
                            {experimentActionPending === "promote_candidate" ? "提交中" : "晋级候选版本"}
                          </button>
                          <button
                            type="button"
                            disabled={Boolean(experimentActionPending) || controlledExperiment?.status !== "running"}
                            onClick={() => void decideTaskControlledExperiment("pause")}
                          >
                            <Pause size={14} />
                            暂停实验
                          </button>
                        </section>
                      </div>
                    )}
    </>
  );
}
