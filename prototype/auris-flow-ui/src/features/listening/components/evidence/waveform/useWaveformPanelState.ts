import type { LabelTrackKey } from "../../../../../shared/fixtures/labelLayers";
import type { CustomLayer, RegionDragState, TrackAnnotation, TrackRegion } from "../../../model/trackLayout";
import type { WaveformPanelProps } from "./waveformPanelTypes";
import { initialManualLabelWorkflowState } from "./manualLabelWorkflow";
import { useRef, useState } from "react";

export function useWaveformPanelState(props: WaveformPanelProps) {
  const {
  showWaveform = true,
  showTracks = true,
  activeTrack,
  setActiveTrack,
  hiddenTracks,
  setHiddenTracks
} = props;
  const [draftAnnotation, setDraftAnnotation] = useState("售后跟进");

  const [activeSegmentIndex, setActiveSegmentIndex] = useState(1);

  const [hiddenTags, setHiddenTags] = useState<Record<string, string[]>>({});

  const [annotations, setAnnotations] = useState<TrackAnnotation[]>([
      { id: "manual-1", track: "entity", label: "置换线索", left: 62, width: 9 },
      { id: "manual-2", track: "qa", label: "待人工确认", left: 44, width: 11 }
    ]);

  const [regionEdits, setRegionEdits] = useState<Record<string, TrackRegion>>({});

  const [dragState, setDragState] = useState<RegionDragState | null>(null);

  const [selectedRegion, setSelectedRegion] = useState("doc-EVT-报价-02");

  const [trackRegionModalId, setTrackRegionModalId] = useState<string | null>(null);

  const [trackPreviewState, setTrackPreviewState] = useState<{
      regionId: string;
      clip: "before" | "current" | "after";
      windowText: string;
      playing: boolean;
    } | null>(null);

  const [lastCreatedAnnotationId, setLastCreatedAnnotationId] = useState<string | null>(null);

  const [createFeedback, setCreateFeedback] = useState("");

  const [savingAnnotationId, setSavingAnnotationId] = useState<string | null>(null);

  const [manualLabelWorkflow, setManualLabelWorkflow] = useState(initialManualLabelWorkflowState);

  const [layerFormOpen, setLayerFormOpen] = useState(false);

  const [customLayers, setCustomLayers] = useState<CustomLayer[]>([]);

  const [layerLevelKey, setLayerLevelKey] = useState<LabelTrackKey>("intent");

  const [layerName, setLayerName] = useState("成交意向复核");

  const [layerTag, setLayerTag] = useState("成交意向");

  const [layerType, setLayerType] = useState("时间区段");

  const regionDragClickGuard = useRef(false);

  return { ...props, showWaveform, showTracks, activeTrack, setActiveTrack, hiddenTracks, setHiddenTracks, draftAnnotation, setDraftAnnotation, activeSegmentIndex, setActiveSegmentIndex, hiddenTags, setHiddenTags, annotations, setAnnotations, regionEdits, setRegionEdits, dragState, setDragState, selectedRegion, setSelectedRegion, trackRegionModalId, setTrackRegionModalId, trackPreviewState, setTrackPreviewState, lastCreatedAnnotationId, setLastCreatedAnnotationId, createFeedback, setCreateFeedback, savingAnnotationId, setSavingAnnotationId, manualLabelWorkflow, setManualLabelWorkflow, layerFormOpen, setLayerFormOpen, customLayers, setCustomLayers, layerLevelKey, setLayerLevelKey, layerName, setLayerName, layerTag, setLayerTag, layerType, setLayerType, regionDragClickGuard };
}

export type WaveformPanelState = ReturnType<typeof useWaveformPanelState>;
