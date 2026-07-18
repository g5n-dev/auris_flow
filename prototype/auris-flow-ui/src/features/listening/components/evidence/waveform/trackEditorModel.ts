import { layerLevelConfigs } from "../../../../../shared/fixtures/labelLayers";
import { labelTrackMeta, trackSegments } from "../../../fixtures/evidenceFixtures";
import type { WaveformPanelState } from "./useWaveformPanelState";
import type { WaveformPanelProps } from "./waveformPanelTypes";

export function buildTrackEditorModel(context: WaveformPanelState) {
  const { activeSegmentIndex, activeTrack, customLayers, hiddenTracks, layerLevelKey } = context;
  const layerLevelConfig = layerLevelConfigs.find((config) => config.key === layerLevelKey) ?? layerLevelConfigs[1];

  const layerKindName = layerLevelConfig.label.replace(/^L\d+\s*/, "");

  const createLayerActionLabel = `创建${layerKindName}`;

  const allTracks = labelTrackMeta.flatMap((track) => [
      track,
      ...customLayers.filter((layer) => layer.targetTrack === track.key)
    ]);

  const visibleCount = allTracks.filter((track) => !hiddenTracks[track.key]).length;

  const activeTrackMeta = allTracks.find((track) => track.key === activeTrack) ?? allTracks[3];

  const activeSegment = trackSegments[activeSegmentIndex];

  return { ...context, layerLevelConfig, layerKindName, createLayerActionLabel, allTracks, visibleCount, activeTrackMeta, activeSegment };
}

export type TrackEditorModel = ReturnType<typeof buildTrackEditorModel>;
