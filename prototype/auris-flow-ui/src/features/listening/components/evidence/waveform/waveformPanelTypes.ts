import type { TrackVisibilityUpdater } from "../../../model/trackLayout";
import type { HumanReviewChange } from "../../../model/reviewDecisionModel";

export type WaveformPanelProps = {
  audioSessionId: string;
  labelCandidateIds: string[];
  onReviewChange: (change: HumanReviewChange) => void;
  sessionStartedAt?: string;
  showWaveform?: boolean;
  showTracks?: boolean;
  activeTrack: string;
  setActiveTrack: (track: string) => void;
  hiddenTracks: Record<string, boolean>;
  setHiddenTracks: TrackVisibilityUpdater;
};
