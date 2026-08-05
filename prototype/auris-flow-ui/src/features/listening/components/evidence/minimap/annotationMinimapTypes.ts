import type { BoundaryExtensionCandidate } from "../../../fixtures/boundaryFixtures";
import type { TrackVisibilityUpdater } from "../../../model/trackLayout";
import type { HumanReviewChange } from "../../../model/reviewDecisionModel";

export type AnnotationMinimapProps = {
  audioSessionId: string;
  boundaryId?: string;
  onReviewChange: (change: HumanReviewChange) => void;
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
  activeTrack: string;
  setActiveTrack: (track: string) => void;
  hiddenTracks: Record<string, boolean>;
  setHiddenTracks: TrackVisibilityUpdater;
  openListeningMode: () => void;
};

export type ExtensionRange = BoundaryExtensionCandidate & { start: number; end: number };
