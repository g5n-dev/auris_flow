import type { LabelsController } from "../controller/useLabelsController";
import { LegacyCommandPanel } from "./LegacyCommandPanel";
import { LegacyConflictPanel } from "./LegacyConflictPanel";
import { LegacyDraftPanel } from "./LegacyDraftPanel";
import { LegacyExperimentPanel } from "./LegacyExperimentPanel";
import { LegacyHumanPanel } from "./LegacyHumanPanel";
import { LegacyOpportunityPanel } from "./LegacyOpportunityPanel";
import { LegacyReleasePanel } from "./LegacyReleasePanel";
import { LegacyTaxonomyPanel } from "./LegacyTaxonomyPanel";

export function LabelsLegacyOverview({ controller }: { controller: LabelsController }) {
  return (
    <div className="module-grid label-grid label-grid-schema">
      {controller.renderLabelEntityNotice()}
      {controller.renderLabelClosedLoopStrip()}
      {controller.renderOptimizationInputPanel()}
      {controller.renderAgentHumanChangePanel()}
      {controller.renderChangeEffectPanel()}
      {controller.renderAutomationDagsterPanel()}
      <LegacyCommandPanel controller={controller} />
      <LegacyOpportunityPanel controller={controller} />
      <LegacyDraftPanel controller={controller} />
      <LegacyHumanPanel controller={controller} />
      <LegacyExperimentPanel controller={controller} />
      <LegacyConflictPanel controller={controller} />
      <LegacyTaxonomyPanel controller={controller} />
      <LegacyReleasePanel controller={controller} />
    </div>
  );
}
