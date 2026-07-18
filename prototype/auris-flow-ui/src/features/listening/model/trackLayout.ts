import type { LabelTrackKey } from "../../../shared/fixtures/labelLayers";
import { clamp } from "../../../shared/runtime/math";

export type TrackVisibilityUpdater = (updater: (current: Record<string, boolean>) => Record<string, boolean>) => void;

export type TrackAnnotation = {
  id: string;
  track: string;
  label: string;
  left: number;
  width: number;
};

export type TrackRegion = {
  id: string;
  label: string;
  left: number;
  width: number;
  tone: string;
  value?: string;
  fieldKey?: string;
  confidence?: number;
  reviewState?: string;
  evidenceRef?: string;
  writeTarget?: string;
  sourceText?: string;
  note?: string;
  assignee?: string;
};

export type LaidTrackRegion = TrackRegion & {
  lane: number;
  laneCount: number;
};

export type RegionDragState = {
  id: string;
  mode: "move" | "left" | "right";
  startX: number;
  startLeft: number;
  startWidth: number;
  trackWidth: number;
};

export type CustomLayer = {
  key: string;
  label: string;
  color: string;
  category: string;
  layerType: string;
  targetTrack: LabelTrackKey;
  level: number;
  levelName: string;
};

export function layoutOverlappingRegions(regions: TrackRegion[]): LaidTrackRegion[] {
  const laneEnds: number[] = [];
  return [...regions]
    .sort((a, b) => a.left - b.left || b.width - a.width)
    .map((region) => {
      const start = region.left;
      const end = region.left + region.width;
      let lane = laneEnds.findIndex((laneEnd) => laneEnd <= start + 0.2);
      if (lane === -1) lane = laneEnds.length;
      laneEnds[lane] = end;
      return { ...region, lane, laneCount: 1 };
    })
    .map((region) => ({ ...region, laneCount: Math.max(1, laneEnds.length) }));
}

export function trackHeightFor(laneCount: number) {
  return laneCount <= 1 ? 30 : Math.max(30, 8 + laneCount * 22);
}

export function percentToClock(percent: number) {
  const totalSeconds = 10 * 60;
  const startHour = 12;
  const startMinute = 23;
  const seconds = Math.round((clamp(percent, 0, 100) / 100) * totalSeconds);
  const absolute = startHour * 3600 + startMinute * 60 + seconds;
  const hh = String(Math.floor(absolute / 3600)).padStart(2, "0");
  const mm = String(Math.floor((absolute % 3600) / 60)).padStart(2, "0");
  const ss = String(absolute % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
