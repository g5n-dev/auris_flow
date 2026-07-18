import type { ModuleDeepLink } from "../../../../../shared/contracts/navigation";
import { listeningDeviceBadges } from "../../../fixtures/evidenceFixtures";
import type { ReviewSample } from "../../../fixtures/reviewSamples";
import type { MarkState, PanelTab } from "../../../types";

export type EvidencePanelProps = {
  panelTab: PanelTab;
  setPanelTab: (tab: PanelTab) => void;
  markState: MarkState;
  setMarkState: (state: MarkState) => void;
  agentState: "pending" | "accepted" | "rejected";
  setAgentState: (state: "pending" | "accepted" | "rejected") => void;
  sample: ReviewSample;
  activeDevice: (typeof listeningDeviceBadges)[number];
  navigateToTarget: (target: ModuleDeepLink) => void;
};

export type HotwordCorrectionRecovery =
  | {
      status: "loading" | "blocked";
      reason: string;
    }
  | {
      status: "ready";
      reason: string;
      hotwordPackVersionId: string;
      evidenceStorageObjectId: string;
      existingBadcaseId: string;
      existingBadcaseTraceId: string;
      existingBadcaseStatus: string;
      standardTerm: string;
      recognizedText: string;
      errorType: "missing_term" | "misrecognition" | "alias_gap" | "weight_issue" | "false_boost";
    };
