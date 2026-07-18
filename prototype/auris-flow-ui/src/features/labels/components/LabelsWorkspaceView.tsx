import type { LabelsController } from "../controller/useLabelsController";
import { LabelsLegacyOverview } from "./LabelsLegacyOverview";
import { LegacyConflictsView } from "./LegacyConflictsView";
import { LegacyReleaseView } from "./LegacyReleaseView";
import { LegacyReviewView } from "./LegacyReviewView";
import { LegacyRulesView } from "./LegacyRulesView";
import { LegacyVersionsView } from "./LegacyVersionsView";

export function LabelsWorkspaceView({ controller }: { controller: LabelsController }) {
  const normalizedLabelsTab =
    controller.activeTab === "prompt" || controller.activeTab === "rules"
      ? "rulesPrompt"
      : controller.activeTab === "evaluation" || controller.activeTab === "conflicts"
        ? "review"
        : controller.activeTab === "versions"
          ? "release"
          : controller.activeTab;
  const renderLegacyLabelUi = import.meta.env.DEV && import.meta.env.VITE_LEGACY_LABEL_UI === "true";

  if (!renderLegacyLabelUi) {
    if (normalizedLabelsTab === "schema") return controller.renderSchemaV2();
    if (normalizedLabelsTab === "extraction") return controller.renderExtractionV2();
    if (normalizedLabelsTab === "rulesPrompt") return controller.renderRulesPromptV2();
    if (normalizedLabelsTab === "review") return controller.renderReviewV2();
    if (normalizedLabelsTab === "release") return controller.renderReleaseV2();
    return controller.renderSchemaV2();
  }

  if (controller.activeTab === "schema") return controller.renderSchemaV2();
  if (controller.activeTab === "extraction") return controller.renderExtractionV2();
  if (controller.activeTab === "rulesPrompt") return controller.renderRulesPromptV2();
  if (controller.activeTab === "review") return <LegacyReviewView controller={controller} />;
  if (controller.activeTab === "release") return <LegacyReleaseView controller={controller} />;
  if (controller.activeTab === "prompt") return controller.renderRulesPromptV2();
  if (controller.activeTab === "versions") return <LegacyVersionsView controller={controller} />;
  if (controller.activeTab === "rules") return <LegacyRulesView controller={controller} />;
  if (controller.activeTab === "conflicts") return <LegacyConflictsView controller={controller} />;
  return <LabelsLegacyOverview controller={controller} />;
}
