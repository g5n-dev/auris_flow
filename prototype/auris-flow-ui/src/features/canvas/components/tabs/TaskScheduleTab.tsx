import type { CanvasController } from "../../controller/useCanvasController";
import { Check } from "lucide-react";

export function TaskScheduleTab({ controller }: { controller: CanvasController }) {
  const { ActiveScheduleIcon, activeDagsterCompatibilityRows, activeRunConfigPreview, activeSchedule, activeScheduleConfig, activeScheduleControls, activeSchedulePartitionKey, activeTab, activeTriggerMeta, backfillConfirmed, changeScheduleMode, confirmBackfillGate, experimentMode, openOutputSinkTemplate, runTaskOnce, scheduleMode, scheduleModes, scheduleOutputSinks, scheduleTriggerMeta, selectedCanvasVariant, selectedCanvasVariantKey, selectedTaskType, setDrawerTab, syncSchedulePlan, updateScheduleConfig } = controller;
  return (
    <>
      {activeTab === "schedule" && (
                      <div className="task-tab-grid task-schedule-grid">
                        <section className="task-tab-card wide task-trigger-mode-card">
                          <div className="task-tab-card-head">
                            <span>运行触发方式</span>
                            <button onClick={syncSchedulePlan}>保存调度计划</button>
                          </div>
                          <div className="task-trigger-summary">
                            <span>
                              <ActiveScheduleIcon size={18} />
                            </span>
                            <div>
                              <b>当前选择：{scheduleMode}</b>
                              <strong>{activeTriggerMeta.title}</strong>
                              <em>{activeTriggerMeta.when}</em>
                            </div>
                            <i>{activeTriggerMeta.entry}</i>
                          </div>
                          <div className="task-trigger-choice-grid" role="radiogroup" aria-label="选择运行触发方式">
                            {scheduleModes.map((mode) => {
                              const meta = scheduleTriggerMeta[mode];
                              const TriggerIcon = meta.icon;
                              return (
                                <button
                                  key={mode}
                                  type="button"
                                  role="radio"
                                  aria-checked={scheduleMode === mode}
                                  className={scheduleMode === mode ? "task-trigger-choice active" : "task-trigger-choice"}
                                  onClick={() => changeScheduleMode(mode)}
                                >
                                  <span>
                                    <TriggerIcon size={16} />
                                    {meta.label}
                                  </span>
                                  <strong>{mode}</strong>
                                  <em>{meta.description}</em>
                                  <b>{meta.entry}</b>
                                </button>
                              );
                            })}
                          </div>
                        </section>
                        <section className="task-tab-card wide task-trigger-config-card">
                          <div className="task-tab-card-head">
                            <span>{scheduleMode}配置</span>
                            <b>{activeTriggerMeta.primary}</b>
                          </div>
                          <div className="task-trigger-config-layout">
                            <div className="task-schedule-detail rich">
                              <span>当前策略</span>
                              <strong>{activeSchedule.title}</strong>
                              <p>{activeSchedule.productState}</p>
                              <div className="task-schedule-kv">
                                <span>底层对象</span>
                                <b>{activeSchedule.dagsterObject}</b>
                                <span>定义名</span>
                                <b>{activeSchedule.definition}</b>
                                <span>触发条件</span>
                                <b>{activeSchedule.trigger}</b>
                                <span>分区</span>
                                <b>{activeSchedule.partition}</b>
                                <span>时区</span>
                                <b>{activeSchedule.timezone}</b>
                                <span>画布版本</span>
                                <b>{selectedCanvasVariant.name}</b>
                              </div>
                            </div>
                            <div className="task-trigger-form">
                              <span>需要配置的参数</span>
                              {activeScheduleControls.map((control) => (
                                <label key={control.key} className={control.wide ? "wide" : ""}>
                                  <b>{control.label}</b>
                                  {control.type === "select" ? (
                                    <select value={activeScheduleConfig[control.key] ?? ""} onChange={(event) => updateScheduleConfig(control.key, event.target.value)}>
                                      {(control.options ?? []).map((option) => (
                                        <option key={option} value={option}>
                                          {option}
                                        </option>
                                      ))}
                                    </select>
                                  ) : control.type === "textarea" ? (
                                    <textarea
                                      value={activeScheduleConfig[control.key] ?? ""}
                                      onChange={(event) => updateScheduleConfig(control.key, event.target.value)}
                                      placeholder={control.placeholder}
                                    />
                                  ) : (
                                    <input
                                      type={control.type}
                                      value={activeScheduleConfig[control.key] ?? ""}
                                      onChange={(event) => updateScheduleConfig(control.key, event.target.value)}
                                      placeholder={control.placeholder}
                                    />
                                  )}
                                  {control.helper && <em>{control.helper}</em>}
                                </label>
                              ))}
                            </div>
                            <div className="task-dagster-compat">
                              <span>执行兼容字段</span>
                              {activeDagsterCompatibilityRows.map(([key, value]) => (
                                <div key={key}>
                                  <b>{key}</b>
                                  <code title={value}>{value}</code>
                                </div>
                              ))}
                            </div>
                          </div>
                        </section>
                        <section className="task-tab-card task-schedule-side">
                          <span>运行保护</span>
                          <strong>不重复、不误写、可回放</strong>
                          {activeSchedule.guardrails.map((check) => (
                            <p key={check}>
                              <Check size={13} />
                              {check}
                            </p>
                          ))}
                          {scheduleMode === "一次性回填" && (
                            <div className={`task-backfill-confirm ${backfillConfirmed ? "ok" : "warn"}`}>
                              <b>{backfillConfirmed ? "Backfill 已确认" : "Backfill 需确认"}</b>
                              <span>{activeSchedulePartitionKey}</span>
                              <em>确认后仍需保存调度，发布门禁才会放行。</em>
                              <button type="button" onClick={confirmBackfillGate}>
                                {backfillConfirmed ? "重新确认回填" : "确认回填策略"}
                              </button>
                            </div>
                          )}
                        </section>
                        <section className="task-tab-card task-schedule-side">
                          <span>A/B 与版本</span>
                          <strong>{experimentMode}</strong>
                          <p>分流只改变 run tags 和 canvas_variant，不修改输入数据源，也不覆盖历史运行。</p>
                          <div className="task-schedule-tags">
                            <b>{selectedTaskType.key}</b>
                            <b>{selectedCanvasVariant.key}</b>
                            <b>{selectedCanvasVariantKey === "candidate-v4" ? "arm=B" : "arm=A"}</b>
                          </div>
                        </section>
                        <section className="task-tab-card wide task-schedule-request-card">
                          <div className="task-tab-card-head">
                            <span>运行请求预览</span>
                            <button onClick={runTaskOnce}>按当前策略运行一次</button>
                          </div>
                          <div className="task-run-request-grid">
                            <div>
                              <span>job_name</span>
                              <strong>{selectedTaskType.defaultCanvas}_job</strong>
                            </div>
                            <div>
                              <span>run_key</span>
                              <strong>{activeSchedule.runKey}</strong>
                            </div>
                            <div>
                              <span>partition_key</span>
                              <strong>{activeSchedulePartitionKey}</strong>
                            </div>
                            <div>
                              <span>trigger</span>
                              <strong>{scheduleMode}</strong>
                            </div>
                          </div>
                          <div className="task-run-config">
                            {[
                              ...activeRunConfigPreview,
                              ["execution_object", activeSchedule.dagsterObject],
                              ["trigger_definition", activeSchedule.definition],
                              ["trigger_payload", activeTriggerMeta.primary],
                              ["output_sinks", "processed_wav_asset, platform_audio_callback"]
                            ].map(([key, value]) => (
                              <span key={key}>
                                <b>{key}</b>
                                <em>{value}</em>
                              </span>
                            ))}
                          </div>
                        </section>
                        <section className="task-tab-card wide task-schedule-output-card">
                          <div className="task-tab-card-head">
                            <span>运行后输出动作</span>
                            <button onClick={() => openOutputSinkTemplate("platform-callback-output")}>配置回写接口</button>
                          </div>
                          {scheduleOutputSinks.map(([name, target, detail]) => (
                            <button key={name} type="button" className="task-schedule-output-row" onClick={() => openOutputSinkTemplate(name === "platform_audio_callback" ? "platform-callback-output" : "obs-wav-output")}>
                              <strong>{name}</strong>
                              <span>{target}</span>
                              <em>{detail}</em>
                            </button>
                          ))}
                        </section>
                        <section className="task-tab-card wide task-runtime-card">
                          <div className="task-tab-card-head">
                            <span>触发映射</span>
                            <button onClick={() => setDrawerTab("plan")}>查看运行请求</button>
                          </div>
                          {activeSchedule.dagsterRows.map(([name, label, detail]) => (
                            <div key={name} className="task-runtime-row">
                              <b>{name}</b>
                              <strong>{label}</strong>
                              <em>{detail}</em>
                            </div>
                          ))}
                        </section>
                      </div>
                    )}
    </>
  );
}
