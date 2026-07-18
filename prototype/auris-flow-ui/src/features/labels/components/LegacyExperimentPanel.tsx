import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { BarChart3, Gauge } from "lucide-react";

export function LegacyExperimentPanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, experimentRows, experimentState, selectedExperimentMetric, setActionFeedback, setActiveModule, setSelectedExperimentMetric } = controller;
  return (
    <section className="module-panel wide label-experiment-panel">
            <PanelHeader title="A/B 版本实验" subtitle={`当前 v1.8.4 vs 候选 v1.9.0-rc2 / ${activeIntent.scene}`} icon={<Gauge size={16} />} />
            <div className="label-experiment-head">
              <div className="label-experiment-controls">
                {[
                  ["未开始", "暂停"],
                  ["影子评测中", "影子评测"],
                  ["灰度中", "灰度 10%"],
                  ["完成", "完成"],
                  ["已回滚", "已回滚"]
                ].map(([state, label]) => (
                  <button
                    key={state}
                    type="button"
                    className={experimentState === state ? "active" : ""}
                    onClick={() => {
                      setActionFeedback(`「${label}」是后端事实状态，不能在页面本地切换；请执行对应评测或发布动作。`);
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <button type="button" onClick={() => setActiveModule("evaluation")}>
                <BarChart3 size={14} />
                查看评测样本
              </button>
            </div>
            <div className="label-experiment-table">
              <div className="label-experiment-row head">
                <span>指标</span>
                <span>当前版本</span>
                <span>候选版本</span>
                <span>变化</span>
                <span>结论</span>
              </div>
              {experimentRows.map(([metric, current, candidate, delta, verdict]) => (
                <button
                  key={metric}
                  type="button"
                  className={selectedExperimentMetric === metric ? "label-experiment-row active" : "label-experiment-row"}
                  onClick={() => {
                    setSelectedExperimentMetric(metric);
                    setActionFeedback(`已聚焦实验指标：${metric}，可下钻差异样本和人工复核记录。`);
                  }}
                >
                  <span>{metric}</span>
                  <strong>{current}</strong>
                  <strong>{candidate}</strong>
                  <em>{delta}</em>
                  <b>{verdict}</b>
                </button>
              ))}
            </div>
          </section>
  );
}
