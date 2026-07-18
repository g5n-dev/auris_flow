import type { LabelsController } from "../controller/useLabelsController";


export function LegacyReleaseView({ controller }: { controller: LabelsController }) {
  const { renderLabelEntityNotice, renderRunContextHeader, renderSharedDagsterStatusCompact, renderUnifiedReleaseDetail } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-run-detail">
            {renderLabelEntityNotice()}
            {renderRunContextHeader("release")}
            {renderUnifiedReleaseDetail()}
            {renderSharedDagsterStatusCompact()}
          </div>
        )
  );
}
