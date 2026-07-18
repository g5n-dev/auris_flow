import { eventLinks } from "../../../../../shared/fixtures/eventLinks";
import { clamp } from "../../../../../shared/runtime/math";
import { documentEventRegions, trackSegments } from "../../../fixtures/evidenceFixtures";
import type { RegionDragState, TrackRegion } from "../../../model/trackLayout";
import type { TrackEditorActions } from "./trackEditorActions";
import type { WaveformPanelProps } from "./waveformPanelTypes";
import type { PointerEvent as ReactPointerEvent } from "react";

export function createTrackRegionActions(context: TrackEditorActions) {
  const { annotations, dragState, hiddenTags, regionDragClickGuard, regionEdits, setDragState, setRegionEdits, setSelectedRegion } = context;
  const annotationsFor = (track: string) => annotations.filter((annotation) => annotation.track === track);

  const regionsForTrack = (trackKey: string): TrackRegion[] => {
      const segmentRegions = trackSegments.map((segment, index) => ({
        id: `${trackKey}-${index}`,
        label: segment.label.replace(/^S\d+\s/, ""),
        left: segment.left,
        width: segment.width,
        tone: index % 2 === 0 ? "asr" : "s1"
      }));

      if (trackKey === "vad") {
        return [
          { id: "vad-1", label: "VAD 有声", left: 38.8, width: 12.4, tone: "tg cp" },
          { id: "vad-2", label: "VAD 有声", left: 49.8, width: 24.6, tone: "tg cp" }
        ];
      }
      if (trackKey === "speaker") {
        return [
          { id: "spk-1", label: "销售A", left: 39.4, width: 3.6, tone: "s1" },
          { id: "spk-2", label: "销售A", left: 43, width: 5.4, tone: "s1" },
          { id: "spk-3", label: "客户", left: 44.2, width: 4.2, tone: "s0" },
          { id: "spk-4", label: "销售A", left: 50.2, width: 9.5, tone: "s1" },
          { id: "spk-5", label: "客户", left: 64.7, width: 6.8, tone: "s0" }
        ];
      }
      if (trackKey === "asr") return segmentRegions;
      if (trackKey === "entity") {
        return [
          { id: "entity-1", label: "车型:325Li", left: 39.8, width: 5.8, tone: "tg" },
          { id: "entity-2", label: "指导价:31.69万", left: 40.6, width: 5.4, tone: "tg" },
          { id: "entity-3", label: "优惠:3.5万", left: 43.8, width: 4.6, tone: "tg" },
          { id: "entity-4", label: "落地:28.19万", left: 45.2, width: 5.6, tone: "tg er" },
          { id: "entity-5", label: "试驾时间:14:00+", left: 64.8, width: 7.2, tone: "tg" }
        ];
      }
      if (trackKey === "intent") {
        return [
          { id: "intent-1", label: "报价承诺", left: 42.8, width: 7.2, tone: "bz" },
          { id: "intent-2", label: "价格异议", left: 44.2, width: 5.2, tone: "bz" },
          { id: "intent-3", label: "试驾承接", left: 50.2, width: 9.5, tone: "bz" },
          { id: "intent-4", label: "预约确认", left: 64.7, width: 7.4, tone: "bz" }
        ];
      }
      if (trackKey === "qa") {
        return [
          { id: "qa-1", label: "金额冲突", left: 43, width: 8, tone: "asr er" },
          { id: "qa-2", label: "低置信", left: 50.2, width: 7.2, tone: "tg" },
          { id: "qa-3", label: "串音待排除", left: 49.4, width: 11.5, tone: "asr er" },
          { id: "qa-4", label: "可回填", left: 64.7, width: 7.4, tone: "tg cp" }
        ];
      }
      if (trackKey === "doc") {
        return [
          ...eventLinks.map((event) => ({
            id: `doc-${event.id}`,
            label: `${event.type} · ${event.state}`,
            left: event.left,
            width: event.width,
            tone: event.tone === "red" ? "asr er" : event.tone === "green" ? "tg cp" : "tg"
          })),
          ...documentEventRegions
        ];
      }
      if (trackKey === "cross") {
        return [
          { id: "cross-1", label: "B-2001 同峰", left: 47.8, width: 12.2, tone: "asr er" },
          { id: "cross-2", label: "Hall-Mic 串入", left: 49.5, width: 12.8, tone: "s0" },
          { id: "cross-3", label: "主录音 A-1001", left: 39.4, width: 11.4, tone: "tg cp" }
        ];
      }
      if (trackKey === "agent") {
        return [
          { id: "agent-1", label: "核报价单", left: 43, width: 8.5, tone: "bz" },
          { id: "agent-2", label: "转串音矩阵", left: 50.2, width: 9.5, tone: "asr" },
          { id: "agent-3", label: "生成试驾待办", left: 64.7, width: 7.8, tone: "tg cp" }
        ];
      }
      return [];
    };

  const applyRegionEdit = (region: TrackRegion) => {
      const edit = regionEdits[region.id];
      return edit ? { ...region, ...edit } : region;
    };

  const updateRegionPosition = (region: TrackRegion, left: number, width: number) => {
      const nextLeft = Number(clamp(left, 0, 98).toFixed(2));
      const nextWidth = Number(clamp(width, 2, 100 - nextLeft).toFixed(2));
      setRegionEdits((current) => ({
        ...current,
        [region.id]: {
          ...region,
          ...(current[region.id] ?? {}),
          left: nextLeft,
          width: nextWidth
        }
      }));
    };

  const startRegionDrag = (
      event: ReactPointerEvent<HTMLElement>,
      region: TrackRegion,
      mode: RegionDragState["mode"]
    ) => {
      if (event.button !== 0) return;
      if (mode !== "move") event.preventDefault();
      event.stopPropagation();
      const handle = event.currentTarget as HTMLElement;
      const regionEl = handle.closest(".rg") as HTMLElement | null;
      const trackEl = handle.closest(".tr") as HTMLElement | null;
      const captureEl = regionEl ?? handle;
      captureEl.setPointerCapture?.(event.pointerId);
      setSelectedRegion(region.id);
      setDragState({
        id: region.id,
        mode,
        startX: event.clientX,
        startLeft: region.left,
        startWidth: region.width,
        trackWidth: Math.max(1, trackEl?.scrollWidth ?? trackEl?.clientWidth ?? 1)
      });
    };

  const moveRegionDrag = (event: ReactPointerEvent<HTMLElement>, region: TrackRegion) => {
      if (!dragState || dragState.id !== region.id) return;
      event.preventDefault();
      const dx = ((event.clientX - dragState.startX) / dragState.trackWidth) * 100;
      if (Math.abs(event.clientX - dragState.startX) > 4) {
        regionDragClickGuard.current = true;
      }
      const minWidth = 2;
      if (dragState.mode === "move") {
        updateRegionPosition(region, dragState.startLeft + dx, dragState.startWidth);
        return;
      }
      if (dragState.mode === "left") {
        const right = dragState.startLeft + dragState.startWidth;
        const nextLeft = clamp(dragState.startLeft + dx, 0, right - minWidth);
        updateRegionPosition(region, nextLeft, right - nextLeft);
        return;
      }
      const nextWidth = clamp(dragState.startWidth + dx, minWidth, 100 - dragState.startLeft);
      updateRegionPosition(region, dragState.startLeft, nextWidth);
    };

  const endRegionDrag = (event: ReactPointerEvent<HTMLElement>) => {
      if (!dragState) return;
      const target = event.currentTarget as HTMLElement;
      target.releasePointerCapture?.(event.pointerId);
      setDragState(null);
    };

  const regionsForTrackWithEdits = (trackKey: string) => {
      const hiddenForTrack = hiddenTags[trackKey] ?? [];
      const baseRegions = regionsForTrack(trackKey)
        .map(applyRegionEdit)
        .filter((region) => !hiddenForTrack.includes(region.label));
      const manualRegions = annotationsFor(trackKey)
        .map((annotation) =>
          applyRegionEdit({
            id: annotation.id,
            label: annotation.label,
            left: annotation.left,
            width: annotation.width,
            tone: "manual"
          })
        )
        .filter((region) => !hiddenForTrack.includes(region.label));
      return [...baseRegions, ...manualRegions];
    };

  return { ...context, annotationsFor, regionsForTrack, applyRegionEdit, updateRegionPosition, startRegionDrag, moveRegionDrag, endRegionDrag, regionsForTrackWithEdits };
}

export type TrackRegionActions = ReturnType<typeof createTrackRegionActions>;
