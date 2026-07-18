import type { LabelsController } from "../controller/useLabelsController";


export function LegacyExtractionView({ controller }: { controller: LabelsController }) {
  const { renderAgentHumanChangePanel, renderAutomationDagsterPanel, renderBackendContractPanel, renderExtractionWorkbench, renderLabelClosedLoopStrip, renderOptimizationInputPanel } = controller;
  return (
    (
          <div className="module-grid label-grid label-grid-extraction">
            {renderLabelClosedLoopStrip()}
            {renderOptimizationInputPanel()}
            {renderAgentHumanChangePanel()}
            {renderExtractionWorkbench()}
            {renderAutomationDagsterPanel()}
            {renderBackendContractPanel()}
          </div>
        )
  );
}
