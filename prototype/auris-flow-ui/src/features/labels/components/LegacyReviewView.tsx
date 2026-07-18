import type { LabelsController } from "../controller/useLabelsController";


export function LegacyReviewView({ controller }: { controller: LabelsController }) {
  const { renderLabelEntityNotice, renderRunContextHeader, renderSharedDagsterStatusCompact, renderUnifiedEvaluationDetail } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-run-detail">
            {renderLabelEntityNotice()}
            {renderRunContextHeader("evaluation")}
            {renderUnifiedEvaluationDetail()}
            {renderSharedDagsterStatusCompact()}
          </div>
        )
  );
}
