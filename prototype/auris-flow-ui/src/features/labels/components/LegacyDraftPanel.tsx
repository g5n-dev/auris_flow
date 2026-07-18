import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Check, GitBranch, Headphones, Sparkles, Tags, UserCheck } from "lucide-react";

export function LegacyDraftPanel({ controller }: { controller: LabelsController }) {
  const { actionFeedback, activeIntent, draftInputs, draftLevel, draftStatus, editableDraftTagName, handleIntentAction, openLabelEvidence, saveDraftRule, setActionFeedback, setDraftStatus, setExperimentState, sourceFilter, updateDraftInput } = controller;
  return (
    <section className="module-panel wide label-draft-panel">
            <PanelHeader title="候选标签草稿" subtitle={`${activeIntent.intent} / Label Draft 不覆盖源数据`} icon={<Tags size={16} />} />
            <div className="label-draft-shell">
              <div className="label-draft-main">
                  <div className="label-draft-title">
                    <div>
                      <span>{activeIntent.scope}</span>
                      <input
                        className="label-title-input"
                        value={editableDraftTagName}
                        onChange={(event) => updateDraftInput("tagName", event.target.value)}
                        aria-label="候选标签名称"
                      />
                      <em>{activeIntent.evidence}</em>
                    </div>
                  <div className={`intent-risk-badge risk-${activeIntent.risk}`}>
                    <span>风险 {activeIntent.risk}</span>
                    <b>{activeIntent.confidence}%</b>
                  </div>
                </div>

                <div className="label-draft-field-grid">
                  {[
                    ["层级", `L${draftLevel.level} · ${draftLevel.label.replace(/^L\d+\s*/, "")}`],
                    ["类型", draftLevel.category],
                    ["状态", draftStatus],
                    ["适用场景", activeIntent.scene],
                    ["候选版本", "v1.9.0-rc2"],
                    ["负责人", activeIntent.owner]
                  ].map(([label, value]) => (
                    <div key={label} className="label-draft-field">
                      <span>{label}</span>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>

                <div className="label-rule-editor">
                  <div>
                    <span>定义</span>
                    <textarea value={draftInputs.definition} onChange={(event) => updateDraftInput("definition", event.target.value)} rows={4} />
                  </div>
                  <div>
                    <span>触发规则</span>
                    <textarea value={draftInputs.trigger} onChange={(event) => updateDraftInput("trigger", event.target.value)} rows={4} />
                  </div>
                  <div>
                    <span>冲突规则</span>
                    <textarea value={draftInputs.conflict} onChange={(event) => updateDraftInput("conflict", event.target.value)} rows={4} />
                  </div>
                </div>

                <div className="label-example-grid">
                  <div>
                    <span>正例</span>
                    <textarea value={draftInputs.positive} onChange={(event) => updateDraftInput("positive", event.target.value)} rows={3} />
                  </div>
                  <div>
                    <span>负例 / 排除</span>
                    <textarea value={draftInputs.negative} onChange={(event) => updateDraftInput("negative", event.target.value)} rows={3} />
                  </div>
                </div>

                <div className="label-draft-actions">
                  <button type="button" onClick={() => openLabelEvidence("规则证据审查")}>
                    <Headphones size={14} />
                    证据审查
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      saveDraftRule();
                    }}
                  >
                    <UserCheck size={14} />
                    人工校准
                  </button>
                  <button
                    type="button"
                    className="primary"
                    onClick={() => {
                      setDraftStatus("待实验");
                      setExperimentState("影子评测中");
                      setActionFeedback(`${editableDraftTagName} 已加入 v1.9.0-rc2 影子评测，不影响线上标签结果。`);
                    }}
                  >
                    <GitBranch size={14} />
                    加入实验
                  </button>
                </div>
              </div>

              <aside className="label-agent-panel">
                <div className="label-agent-head">
                  <Sparkles size={15} />
                  <div>
                    <span>Agent 创建建议</span>
                    <strong>{sourceFilter} · {activeIntent.status}</strong>
                  </div>
                </div>
                <div className="label-agent-suggestions">
                  {activeIntent.suggestions.map((suggestion) => (
                    <button key={suggestion} type="button" onClick={() => handleIntentAction(`已选择建议：${suggestion}。建议会进入候选草稿，不直接发布。`)}>
                      <Check size={13} />
                      {suggestion}
                    </button>
                  ))}
                </div>
                <div className="label-agent-trace">
                  {activeIntent.trace.map(([node, detail]) => (
                    <div key={`${node}-${detail}`}>
                      <span>{node}</span>
                      <strong>{detail}</strong>
                    </div>
                  ))}
                </div>
                <p>{actionFeedback}</p>
              </aside>
            </div>
          </section>
  );
}
