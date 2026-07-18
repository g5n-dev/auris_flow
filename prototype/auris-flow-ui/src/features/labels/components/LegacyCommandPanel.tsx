import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelAgentRunSteps, labelScenarioPlaybooks } from "../fixtures/scenarioCatalog";
import { BrainCircuit, Check, Sparkles } from "lucide-react";

export function LegacyCommandPanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, activeScenario, agentRunState, agentStepIndex, editableDraftTagName, experimentState, extractionState, handleIntentAction, labelCandidates, labelEntityAction, promptVariant, releaseDecision, renderLabelDataActions, reviewState, reviewTasks, runScenarioAgent, selectScenario, selectedMetricRow, setAgentStepIndex } = controller;
  return (
    <section className="module-panel wide label-command-panel">
            <PanelHeader title="标签生产治理台" subtitle="证据样本 → 智能抽取 → 标签候选 → Prompt优化 → 影子评测 → Human Loop → 发布门禁 → badcase回流" icon={<BrainCircuit size={16} />} />
            <div className="label-command-overview">
              <div className="label-command-main">
                <div className="label-production-flow" aria-label="标签生产流程">
                  {[
                    ["Evidence", "证据样本", activeScenario.source, "done"],
                    ["ExtractionRun", extractionState === "completed" ? "已生成" : "待运行", "只写候选", extractionState === "running" ? "active" : "done"],
                    ["LabelCandidate", `${labelCandidates.length} 条`, editableDraftTagName, "active"],
                    ["PromptVersion", promptVariant === "candidate" ? "候选" : "线上", "不覆盖生效版本", "active"],
                    ["EvalRun", experimentState, selectedMetricRow[0], "active"],
                    ["Human Loop", `${reviewTasks.length} 项`, `${reviewState} / 风险 ${activeIntent.risk}`, activeIntent.risk === "高" ? "risk" : "active"],
                    ["Release Gate", activeIntent.blockers.length > 0 ? `${activeIntent.blockers.length} 阻断` : "可灰度", releaseDecision, activeIntent.blockers.length > 0 ? "blocked" : "done"],
                    ["Backflow", "badcase", "回流评测集", "done"]
                  ].map(([step, state, detail, tone], index) => (
                    <button key={step} type="button" className={`label-production-step ${tone}`} onClick={() => handleIntentAction(`${step}：${state}。${detail}`)}>
                      <b>{index + 1}</b>
                      <span>{step}</span>
                      <strong>{state}</strong>
                      <em>{detail}</em>
                    </button>
                  ))}
                </div>
                <div className="label-candidate-summary" aria-label="当前候选标签摘要">
                  <div>
                    <span>当前候选</span>
                    <strong>{editableDraftTagName}</strong>
                    <p>{activeIntent.evidence}</p>
                  </div>
                  <b>v1.9.0-rc2</b>
                  <b>{activeIntent.scene}</b>
                  <b>{activeIntent.owner}</b>
                </div>
              </div>
              <div className="label-command-actions">
                {renderLabelDataActions()}
              </div>
            </div>
            <div className="label-scenario-workbench">
              <div className="label-scenario-head">
                <div>
                  <span>场景智能体</span>
                  <strong>{activeScenario.name}</strong>
                  <p>{activeScenario.goal}</p>
                </div>
                <button type="button" className={agentRunState === "running" ? "running" : ""} onClick={runScenarioAgent} disabled={labelEntityAction === "agent-run"}>
                  <Sparkles size={14} />
                  {labelEntityAction === "agent-run" ? "请求处理中" : agentRunState === "failed" ? "重试智能创建" : agentRunState === "running" ? "刷新智能体状态" : "启动智能创建"}
                </button>
              </div>
              <div className="label-scenario-layout">
                <div className="label-scenario-list" aria-label="标签创建场景">
                  {labelScenarioPlaybooks.map((scenario) => (
                    <button
                      key={scenario.key}
                      type="button"
                      className={activeScenario.key === scenario.key ? "active" : ""}
                      onClick={() => selectScenario(scenario.key)}
                    >
                      <strong>{scenario.name}</strong>
                      <span>{scenario.source}</span>
                      <em>{scenario.confidence}%</em>
                    </button>
                  ))}
                </div>
                <div className="label-scenario-data">
                  <span>已有数据输入</span>
                  <div>
                    {activeScenario.data.map((item) => (
                      <b key={item}>{item}</b>
                    ))}
                  </div>
                </div>
                <div className="label-agent-runner">
                  <div className="label-agent-progress">
                    <i style={{ width: `${agentRunState === "completed" ? 100 : agentRunState === "running" ? ((agentStepIndex + 1) / labelAgentRunSteps.length) * 100 : 12}%` }} />
                  </div>
                  {labelAgentRunSteps.map(([step, detail], index) => (
                    <button
                      key={step}
                      type="button"
                      className={[
                        index < agentStepIndex || agentRunState === "completed" ? "done" : "",
                        index === agentStepIndex && agentRunState === "running" ? "running" : ""
                      ].join(" ")}
                      onClick={() => setAgentStepIndex(index)}
                    >
                      <b>{index + 1}</b>
                      <strong>{step}</strong>
                      <span>{detail}</span>
                    </button>
                  ))}
                </div>
                <div className="label-generated-levels">
                  <span>多级标签草案</span>
                  {activeScenario.levels.map((level) => (
                    <button key={level} type="button" onClick={() => handleIntentAction(`${level} 已加入 ${activeScenario.output}，等待证据确认。`)}>
                      <Check size={13} />
                      {level}
                    </button>
                  ))}
                  <em>{activeScenario.output}</em>
                </div>
              </div>
            </div>
          </section>
  );
}
