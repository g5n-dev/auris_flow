import type { BoundaryExtensionCandidate } from "../../../fixtures/boundaryFixtures";
import type { TrackVisibilityUpdater } from "../../../model/trackLayout";

export type AnnotationMinimapProps = {
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
  activeTrack: string;
  setActiveTrack: (track: string) => void;
  hiddenTracks: Record<string, boolean>;
  setHiddenTracks: TrackVisibilityUpdater;
  openListeningMode: () => void;
};

export type ExtensionRange = BoundaryExtensionCandidate & { start: number; end: number };
