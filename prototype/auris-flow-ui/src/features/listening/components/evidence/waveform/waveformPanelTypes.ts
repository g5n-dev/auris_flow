import type { TrackVisibilityUpdater } from "../../../model/trackLayout";

export type WaveformPanelProps = {
  audioSessionId: string;
  sessionStartedAt?: string;
  showWaveform?: boolean;
  showTracks?: boolean;
  activeTrack: string;
  setActiveTrack: (track: string) => void;
  hiddenTracks: Record<string, boolean>;
  setHiddenTracks: TrackVisibilityUpdater;
};
