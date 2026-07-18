import type { LabelsController } from "../controller/useLabelsController";
import { TimelineList } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Check, Headphones, ListFilter, Sparkles, Tags } from "lucide-react";

export function LegacyRulesView({ controller }: { controller: LabelsController }) {
  const { actionFeedback, activeIntent, draftInputs, draftLevel, draftMatch, draftStatus, editableDraftTagName, handleIntentAction, openLabelEvidence, renderLabelEntityNotice, saveDraftRule, sourceFilter, updateDraftInput } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-rules">
            {renderLabelEntityNotice()}
            <section className="module-panel wide label-draft-panel">
              <PanelHeader title="标签规则" subtitle={`${activeIntent.intent} / 规则、正负例、冲突处理和 Agent 建议`} icon={<Tags size={16} />} />
              <div className="label-draft-shell">
                <div className="label-draft-main">
                  <label className="label-rule-name-field">
                    <span>规则名称</span>
                    <input
                      className="label-title-input"
                      value={editableDraftTagName}
                      onChange={(event) => updateDraftInput("tagName", event.target.value)}
                      aria-label="规则名称"
                    />
                  </label>
                  <div className="label-draft-field-grid">
                    {[
                      ["层级", `L${draftLevel.level} · ${draftLevel.category}`],
                      ["状态", draftStatus],
                      ["负责人", activeIntent.owner],
                      ["触发来源", sourceFilter],
                      ["候选版本", "v1.9.0-rc2"]
                    ].map(([label, value]) => (
                      <div key={label} className="label-draft-field">
                        <span>{label}</span>
                        <strong>{value}</strong>
                      </div>
                    ))}
                  </div>
                  <div className="label-rule-editor">
                    <div>
                      <span>触发条件</span>
                      <textarea value={draftInputs.trigger} onChange={(event) => updateDraftInput("trigger", event.target.value)} rows={4} />
                    </div>
                    <div>
                      <span>排除条件</span>
                      <textarea value={draftInputs.negative} onChange={(event) => updateDraftInput("negative", event.target.value)} rows={4} />
                    </div>
                    <div>
                      <span>回写策略</span>
                      <textarea value={draftInputs.conflict} onChange={(event) => updateDraftInput("conflict", event.target.value)} rows={4} />
                    </div>
                  </div>
                  <div className="label-example-grid">
                    <div>
                      <span>正例样本</span>
                      <textarea value={draftInputs.positive} onChange={(event) => updateDraftInput("positive", event.target.value)} rows={3} />
                    </div>
                    <div>
                      <span>标签定义</span>
                      <textarea value={draftInputs.definition} onChange={(event) => updateDraftInput("definition", event.target.value)} rows={3} />
                    </div>
                  </div>
                  <div className="label-draft-actions">
                    <button type="button" onClick={() => openLabelEvidence("规则证据审查")}>
                      <Headphones size={14} />
                      证据审查
                    </button>
                    <button type="button" className="primary" onClick={saveDraftRule}>
                      <Check size={14} />
                      保存规则草稿
                    </button>
                  </div>
                </div>
                <aside className="label-agent-panel">
                  <div className="label-agent-head">
                    <Sparkles size={15} />
                    <div>
                      <span>Agent 规则建议</span>
                      <strong>{activeIntent.status}</strong>
                    </div>
                  </div>
                  <div className="label-agent-suggestions">
                    {activeIntent.suggestions.map((suggestion) => (
                      <button key={suggestion} type="button" onClick={() => handleIntentAction(`已选择规则建议：${suggestion}`)}>
                        <Check size={13} />
                        {suggestion}
                      </button>
                    ))}
                  </div>
                  <p>{actionFeedback}</p>
                </aside>
              </div>
            </section>
            <section className="module-panel">
              <PanelHeader title="规则命中样本" subtitle="正例、负例、边界样本" icon={<ListFilter size={16} />} />
              <TimelineList
                items={[
                  ["正例", draftMatch?.evidence ?? activeIntent.evidence, "命中"],
                  ["负例", activeIntent.blockers[0] ?? "证据不足", "排除"],
                  ["边界样本", activeIntent.conflicts[0]?.label ?? "低置信", "转人工"]
                ]}
              />
            </section>
          </div>
        )
  );
}
