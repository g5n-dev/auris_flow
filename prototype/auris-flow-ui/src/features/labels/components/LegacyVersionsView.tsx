import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Gauge, GitBranch, ShieldCheck } from "lucide-react";

export function LegacyVersionsView({ controller }: { controller: LabelsController }) {
  const { activeIntent, activeLayerCount, draftStatus, experimentRows, experimentState, renderLabelDataActions, renderLabelEntityNotice, renderReleaseGateEditor, selectedExperimentMetric, setSelectedExperimentMetric } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-versions">
            {renderLabelEntityNotice()}
            <section className="module-panel wide label-command-panel">
              <PanelHeader title="标签版本" subtitle="当前版本、候选版本、影子评测、灰度和发布门禁" icon={<GitBranch size={16} />} />
              <div className="label-governance-strip">
                {[
                  ["当前版本", "v1.8.4", "线上 96 标签 / 118 总标签", "teal"],
                  ["候选版本", "v1.9.0-rc2", `${draftStatus} / ${activeLayerCount} 层命中`, "blue"],
                  ["实验状态", experimentState, selectedExperimentMetric, "violet"],
                  ["发布阻断", activeIntent.blockers.length > 0 ? `${activeIntent.blockers.length} 项` : "可灰度", activeIntent.scene, activeIntent.blockers.length > 0 ? "red" : "green"]
                ].map(([label, value, meta, tone]) => (
                  <button key={label} type="button" className={`label-governance-card ${tone}`}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                    <em>{meta}</em>
                  </button>
                ))}
              </div>
              {renderLabelDataActions()}
            </section>
            <section className="module-panel wide label-experiment-panel">
              <PanelHeader title="版本实验" subtitle="v1.8.4 vs v1.9.0-rc2 / 不直接覆盖线上标签" icon={<Gauge size={16} />} />
              <div className="label-experiment-table">
                <div className="label-experiment-row head">
                  <span>指标</span>
                  <span>当前版本</span>
                  <span>候选版本</span>
                  <span>变化</span>
                  <span>结论</span>
                </div>
                {experimentRows.map((row) => (
                  <button key={row[0]} type="button" className="label-experiment-row" onClick={() => setSelectedExperimentMetric(row[0])}>
                    {row.map((cell) => (
                      <span key={cell}>{cell}</span>
                    ))}
                  </button>
                ))}
              </div>
            </section>
            <section className="module-panel">
              <PanelHeader title="发布门禁" subtitle="版本发布必须说明影响资产和阻断原因" icon={<ShieldCheck size={16} />} />
              {renderReleaseGateEditor()}
            </section>
          </div>
        )
  );
}
