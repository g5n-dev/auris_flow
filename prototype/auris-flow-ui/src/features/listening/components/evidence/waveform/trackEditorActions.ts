import type { LabelTrackKey } from "../../../../../shared/fixtures/labelLayers";
import { layerLevelConfigs } from "../../../../../shared/fixtures/labelLayers";
import { trackSegments } from "../../../fixtures/evidenceFixtures";
import { percentToClock } from "../../../model/trackLayout";
import type { TrackEditorModel } from "./trackEditorModel";
import type { WaveformPanelProps } from "./waveformPanelTypes";

export function createTrackEditorActions(context: TrackEditorModel) {
  const { activeSegment, activeTrack, allTracks, draftAnnotation, labelCandidateIds, layerKindName, layerLevelConfig, layerName, layerTag, layerType, onReviewChange, setActiveSegmentIndex, setActiveTrack, setAnnotations, setCreateFeedback, setCustomLayers, setDraftAnnotation, setHiddenTags, setHiddenTracks, setLastCreatedAnnotationId, setLayerFormOpen, setLayerLevelKey, setLayerName, setLayerTag, setLayerType, setSelectedRegion } = context;
  const toggleTrack = (track: string) => {
      setHiddenTracks((current) => {
        const nextHidden = !current[track];
        if (nextHidden && activeTrack === track) {
          const nextActive = allTracks.find((item) => item.key !== track && !current[item.key]) ?? allTracks[0];
          setActiveTrack(nextActive.key);
        }
        return { ...current, [track]: nextHidden };
      });
    };

  const hideTag = (track: string, tag: string) => {
      setHiddenTags((current) => ({
        ...current,
        [track]: Array.from(new Set([...(current[track] ?? []), tag]))
      }));
    };

  const selectLayerLevel = (nextKey: LabelTrackKey) => {
      const nextConfig = layerLevelConfigs.find((config) => config.key === nextKey) ?? layerLevelConfigs[1];
      setLayerLevelKey(nextKey);
      setLayerTag(nextConfig.tags[0]);
      setLayerType(nextConfig.types[0]);
      setLayerName(`${nextConfig.tags[0]}复核`);
      setActiveTrack(nextConfig.key);
    };

  const selectLayerTag = (nextTag: string) => {
      setLayerTag(nextTag);
      const isDefaultLayerName = layerLevelConfig.tags.some((tag) => layerName === `${tag}复核`);
      if (!layerName.trim() || isDefaultLayerName) {
        setLayerName(`${nextTag}复核`);
      }
    };

  const createAnnotation = (labelInput = draftAnnotation) => {
      const label = labelInput.trim();
      const labelCandidateId = labelCandidateIds[0];
      if (!labelCandidateId) {
        setCreateFeedback("当前任务未绑定可修订的标签候选");
        return;
      }
      if (!label) {
        setCreateFeedback("请输入标签名称后再创建");
        return;
      }
      const id = `manual-${Date.now()}`;
      const nextWidth = Math.max(activeSegment.width, Math.min(18, label.length * 1.8));
      setAnnotations((current) => [
        ...current,
        {
          id,
          track: activeTrack,
          label,
          left: activeSegment.left,
          width: nextWidth
        }
      ]);
      setSelectedRegion(id);
      setLastCreatedAnnotationId(id);
      setCreateFeedback(`已创建：${label} · ${percentToClock(activeSegment.left)}-${percentToClock(activeSegment.left + nextWidth)}`);
      setHiddenTracks((current) => ({ ...current, [activeTrack]: false }));
      setDraftAnnotation("");
      onReviewChange({
        target_type: "label_candidate",
        target_id: labelCandidateId,
        fields: {
          value: label,
          confidence: 1
        }
      });
    };

  const createLayer = () => {
      const labelCandidateId = labelCandidateIds[0];
      if (!labelCandidateId) {
        setCreateFeedback("当前任务未绑定可修订的标签候选");
        return;
      }
      const label = (layerName.trim() || layerTag).slice(0, 18);
      const key = `custom-${Date.now()}`;
      const config = layerLevelConfig;
      setCustomLayers((current) => [
        ...current,
        {
          key,
          label,
          color: config.color,
          category: config.category,
          layerType,
          targetTrack: config.key,
          level: config.level,
          levelName: config.label
        }
      ]);
      setActiveTrack(key);
      setHiddenTracks((current) => ({ ...current, [key]: false }));
      const id = `manual-${Date.now()}-layer`;
      setAnnotations((current) => [
        ...current,
        {
          id,
          track: key,
          label: layerTag,
          left: activeSegment.left,
          width: activeSegment.width
        }
      ]);
      setSelectedRegion(id);
      setCreateFeedback(`已创建${layerKindName}：${label} / ${layerTag}`);
      setLayerFormOpen(false);
      onReviewChange({
        target_type: "label_candidate",
        target_id: labelCandidateId,
        fields: {
          value: layerTag,
          confidence: 1
        }
      });
    };

  const moveSegment = (direction: "prev" | "next") => {
      setActiveSegmentIndex((current) => {
        if (direction === "prev") return Math.max(0, current - 1);
        return Math.min(trackSegments.length - 1, current + 1);
      });
    };

  return { ...context, toggleTrack, hideTag, selectLayerLevel, selectLayerTag, createAnnotation, createLayer, moveSegment };
}

export type TrackEditorActions = ReturnType<typeof createTrackEditorActions>;
