import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { AlertTriangle, Check } from "lucide-react";

export function LegacyConflictPanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, openLabelEvidence } = controller;
  return (
    <section className="module-panel label-conflict-panel">
            <PanelHeader title="冲突仲裁" subtitle={`${activeIntent.intent} / 回到证据审查保留上下文`} icon={<AlertTriangle size={16} />} />
            <div className="label-conflict-list">
              {activeIntent.conflicts.length > 0 ? (
                activeIntent.conflicts.map((conflict) => (
                  <button key={`${conflict.label}-${conflict.source}`} type="button" className={`label-conflict-row severity-${conflict.severity}`} onClick={() => openLabelEvidence(`${conflict.label} / ${conflict.source}`, conflict.label.includes("串音") ? "matrix" : "evidence")}>
                    <span>{conflict.label}</span>
                    <strong>{conflict.source}</strong>
                    <em>{conflict.detail}</em>
                    <b>证据审查</b>
                  </button>
                ))
              ) : (
                <div className="label-empty-state">
                  <Check size={16} />
                  <strong>当前意图无冲突</strong>
                  <span>仍保留人工确认入口，低风险动作只写候选结果。</span>
                </div>
              )}
            </div>
          </section>
  );
}
