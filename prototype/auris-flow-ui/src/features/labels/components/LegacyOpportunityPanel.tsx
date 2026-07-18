import type { LabelsController } from "../controller/useLabelsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelIntentFlows } from "../fixtures/scenarioCatalog";
import { Sparkles } from "lucide-react";

export function LegacyOpportunityPanel({ controller }: { controller: LabelsController }) {
  const { activeIntent, setActionFeedback, setActiveIntentKey, setDraftStatus, setReviewState, setSourceFilter, sourceFilter, sourceOptions } = controller;
  return (
    <section className="module-panel label-opportunity-panel">
            <PanelHeader title="智能创建入口" subtitle="从证据发现标签机会，不直接改生效版本" icon={<Sparkles size={16} />} />
            <div className="label-source-tabs" aria-label="候选来源">
              {sourceOptions.map((option) => (
                <button
                  key={option}
                  type="button"
                  className={sourceFilter === option ? "active" : ""}
                  onClick={() => {
                    setSourceFilter(option);
                    setActionFeedback(`已切换候选来源：${option}。Agent 将按该来源重新排序标签机会。`);
                  }}
                >
                  {option}
                </button>
              ))}
            </div>
            <div className="label-opportunity-list" aria-label="标签机会">
              {labelIntentFlows.map((flow) => (
                <button
                  key={flow.key}
                  type="button"
                  className={`label-opportunity-card ${activeIntent.key === flow.key ? "active" : ""} risk-${flow.risk}`}
                  onClick={() => {
                    setActiveIntentKey(flow.key);
                    setDraftStatus("草稿");
                    setReviewState("待人工");
                    setActionFeedback(`${flow.intent} 已切换：刷新候选草稿、实验指标、人审任务和发布阻断。`);
                  }}
                >
                  <div>
                    <span>{flow.stage}</span>
                    <b>{flow.confidence}%</b>
                  </div>
                  <strong>{flow.intent}</strong>
                  <em>{flow.status}</em>
                  <small>{flow.scene}</small>
                </button>
              ))}
            </div>
          </section>
  );
}
