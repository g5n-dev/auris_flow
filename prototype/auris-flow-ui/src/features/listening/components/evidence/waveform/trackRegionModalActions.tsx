import { isApiRequestError } from "../../../../../api/client";
import {
  confirmManualLabelDraftRebase,
  createManualLabelDraft,
  getLabelVersionLifecycle,
  getProductionReleaseBundleHead,
  listActiveLabelVersionItems,
  previewManualLabelDraftRebase,
  submitManualLabelDraft
} from "../../../../../api/manualLabelClient";
import { labelTrackMeta } from "../../../fixtures/evidenceFixtures";
import type { TrackRegion } from "../../../model/trackLayout";
import { percentToClock } from "../../../model/trackLayout";
import {
  initialManualLabelWorkflowState,
  mappingBundleFromLifecycle,
  matchLabelVersionItem,
  parseManualLabelValue,
  rebasedAnnotationId,
  resolveManualLabelOccurredAt,
  sha256Evidence
} from "./manualLabelWorkflow";
import type { TrackRegionModalModel } from "./trackRegionModalModel";
import type { WaveformPanelProps } from "./waveformPanelTypes";

export function createTrackRegionModalActions(context: TrackRegionModalModel) {
  const { allTracks, audioSessionId, customLayers, dragState, endRegionDrag, hideTag, inferRegionFieldKey, lastCreatedAnnotationId, manualLabelWorkflow, modalRegion, modalRegionDraft, modalTrack, moveRegionDrag, previewWindowForRegion, regionDragClickGuard, selectedRegion, sessionStartedAt, setActiveTrack, setCreateFeedback, setManualLabelWorkflow, setRegionEdits, setSavingAnnotationId, setSelectedRegion, setTrackPreviewState, setTrackRegionModalId, startRegionDrag, trackLayouts, trackPreviewState } = context;
  const startTrackPreview = (region: TrackRegion, clip: "before" | "current" | "after") => {
      const windowText = previewWindowForRegion(region, clip);
      setTrackPreviewState((current) => {
        const sameClip = current?.regionId === region.id && current.clip === clip;
        return {
          regionId: region.id,
          clip,
          windowText,
          playing: sameClip ? !current.playing : true
        };
      });
    };

  const updateModalRegion = (patch: Partial<TrackRegion>) => {
      if (!modalRegion) return;
      setRegionEdits((current) => ({
        ...current,
        [modalRegion.id]: {
          ...modalRegion,
          ...(current[modalRegion.id] ?? {}),
          ...patch
        }
      }));
      setSelectedRegion(modalRegion.id);
    };

  const loadManualLabelScope = async (
    track: (typeof allTracks)[number],
    region: TrackRegion
  ) => {
    setManualLabelWorkflow((current) => ({
      ...(current.sourceRegionId === region.id ? current : initialManualLabelWorkflowState),
      status: "loading-scope",
      sourceRegionId: region.id,
      message: "正在读取 production Release Head 与不可变标签项。"
    }));
    try {
      const head = (await getProductionReleaseBundleHead()).data;
      const items = (await listActiveLabelVersionItems(head.label_version_id)).data.items;
      const fieldKey = region.fieldKey ?? inferRegionFieldKey(track.key, region.label);
      const matched = matchLabelVersionItem(items, fieldKey, region.label);
      let mappingBundleId = "";
      const currentDraft = manualLabelWorkflow.sourceRegionId === region.id
        ? manualLabelWorkflow.draft
        : null;
      const draftIsStale = Boolean(
        currentDraft && currentDraft.label_version_id !== head.label_version_id
      );
      if (draftIsStale && currentDraft) {
        const lifecycle = await getLabelVersionLifecycle(currentDraft.label_version_id);
        mappingBundleId = mappingBundleFromLifecycle(lifecycle.data);
      }
      setManualLabelWorkflow((current) => {
        if (current.sourceRegionId !== region.id) return current;
        const draft = current.draft;
        const stale = Boolean(draft && draft.label_version_id !== head.label_version_id);
        return {
          ...current,
          status: stale ? "stale" : draft ? "draft" : items.length ? "ready" : "error",
          releaseHead: head,
          items,
          selectedLabelId: stale ? matched?.label_id ?? "" : draft?.label_id ?? matched?.label_id ?? "",
          mappingBundleId: stale ? mappingBundleId : current.mappingBundleId,
          preview: null,
          rebaseConfirmed: false,
          message: stale
            ? "草稿标签版本已不属于当前 production Head，必须预览映射并显式确认。"
            : draft
              ? "草稿已冻结，可提交；页面编辑不会覆盖已保存内容。"
              : items.length
                ? "已取得权威版本与标签项，保存时将冻结版本、generation、证据 SHA 与发生时间。"
                : "当前 production 标签版本没有 active 标签项，已阻断人工写入。"
        };
      });
    } catch (error) {
      setManualLabelWorkflow((current) => current.sourceRegionId === region.id
        ? {
            ...current,
            status: "error",
            message: `权威标签范围读取失败：${error instanceof Error ? error.message : "请重试"}`
          }
        : current);
    }
  };

  const saveModalAnnotationDraft = async () => {
      if (!modalRegion || !modalTrack || !modalRegionDraft) return;
      const head = manualLabelWorkflow.releaseHead;
      const item = manualLabelWorkflow.items.find(
        (candidate) => candidate.label_id === manualLabelWorkflow.selectedLabelId
      );
      if (!head || !item) {
        setManualLabelWorkflow((current) => ({
          ...current,
          status: "error",
          message: "未取得 production Head 或未选择权威标签项，已阻断保存。"
        }));
        return;
      }
      const startMs = Math.round((modalRegion.left / 100) * 600_000);
      const endMs = Math.round(((modalRegion.left + modalRegion.width) / 100) * 600_000);
      const occurredAt = resolveManualLabelOccurredAt(
        sessionStartedAt,
        audioSessionId,
        percentToClock(modalRegion.left),
        startMs
      );
      if (!occurredAt || !audioSessionId || audioSessionId === "未选择会话") {
        setManualLabelWorkflow((current) => ({
          ...current,
          status: "error",
          message: "会话 ID 或事件发生时间不可验证，已阻断保存，页面内容仍保留。"
        }));
        return;
      }
      setSavingAnnotationId(modalRegion.id);
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "saving-draft",
        message: "正在计算证据 SHA 并冻结人工标签草稿。"
      }));
      setCreateFeedback(`保存中：${modalRegion.label}`);
      try {
        const evidenceSha256 = await sha256Evidence({
          audio_session_id: audioSessionId,
          end_ms: endMs,
          evidence_ref: modalRegionDraft.evidenceRef,
          region_id: modalRegion.id,
          source_text: modalRegionDraft.sourceText,
          start_ms: startMs
        });
        const value = parseManualLabelValue(
          item.value_type,
          modalRegionDraft.value,
          startMs,
          endMs
        );
        const receipt = await createManualLabelDraft(audioSessionId, {
          annotation_kind: "label-fact-draft",
          annotation_id: modalRegion.id,
          label_version_id: head.label_version_id,
          label_id: item.label_id,
          subject_scope: "audio-session",
          subject_key: audioSessionId,
          event_or_segment_id: modalRegion.id,
          assertion_slot: `${modalTrack.key}:${modalRegionDraft.fieldKey}`.slice(0, 128),
          occurred_at: occurredAt,
          evidence_ref: {
            type: "audio-segment",
            id: `${audioSessionId}:${modalRegion.id}`,
            sha256: evidenceSha256,
            start_ms: startMs,
            end_ms: endMs
          },
          value_type: item.value_type,
          value,
          environment: "production",
          expected_release_head_generation: head.generation
        });
        setManualLabelWorkflow((current) => ({
          ...current,
          status: "draft",
          draft: receipt.data,
          message: `草稿已冻结：${receipt.data.label_version_id} / generation ${receipt.data.release_head_generation} / SHA ${receipt.data.draft_sha256.slice(0, 12)}。`
        }));
        setCreateFeedback(`标签草稿已保存：${receipt.data.annotation_id} · ${receipt.data.trace_id.slice(0, 12)}`);
      } catch (error) {
        setManualLabelWorkflow((current) => ({
          ...current,
          status: "error",
          message: `保存失败：${error instanceof Error ? error.message : "请重试"}；输入内容未清空。`
        }));
        setCreateFeedback(`保存失败：${error instanceof Error ? error.message : "请重试"}`);
      } finally {
        setSavingAnnotationId(null);
      }
    };

  const submitModalAnnotationDraft = async () => {
    const draft = manualLabelWorkflow.draft;
    if (!draft) return;
    setManualLabelWorkflow((current) => ({
      ...current,
      status: "submitting",
      message: "正在复核当前 production Head 并提交冻结草稿。"
    }));
    try {
      const head = (await getProductionReleaseBundleHead()).data;
      const receipt = await submitManualLabelDraft(
        audioSessionId,
        draft.annotation_id,
        draft.draft_sha256,
        head.generation
      );
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "submitted",
        releaseHead: head,
        draft: receipt.data,
        message: `已形成 human-confirmed Label Fact ${receipt.data.fact_id ?? ""}，历史事实不会被后续版本改写。`
      }));
      setCreateFeedback(`标签事实已提交：${receipt.data.fact_id ?? receipt.data.annotation_id}`);
    } catch (error) {
      if (isApiRequestError(error) && error.code === "STALE_LABEL_VERSION") {
        try {
          const head = (await getProductionReleaseBundleHead()).data;
          const [itemsResponse, lifecycleResponse] = await Promise.all([
            listActiveLabelVersionItems(head.label_version_id),
            getLabelVersionLifecycle(draft.label_version_id)
          ]);
          const items = itemsResponse.data.items;
          setManualLabelWorkflow((current) => ({
            ...current,
            status: "stale",
            releaseHead: head,
            items,
            selectedLabelId: "",
            mappingBundleId: mappingBundleFromLifecycle(lifecycleResponse.data),
            preview: null,
            rebaseConfirmed: false,
            message: "提交已阻断：草稿版本不再属于当前 production Head。旧草稿和输入均保留。"
          }));
          return;
        } catch (scopeError) {
          setManualLabelWorkflow((current) => ({
            ...current,
            status: "stale",
            message: `版本已过期，且替代范围读取失败：${scopeError instanceof Error ? scopeError.message : "请重试"}`
          }));
          return;
        }
      }
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "error",
        message: `提交失败：${error instanceof Error ? error.message : "请重试"}；冻结草稿未改变。`
      }));
    }
  };

  const previewModalAnnotationRebase = async () => {
    const { draft, mappingBundleId, releaseHead, selectedLabelId } = manualLabelWorkflow;
    if (!draft || !mappingBundleId.trim() || !releaseHead || !selectedLabelId) return;
    setManualLabelWorkflow((current) => ({
      ...current,
      status: "previewing",
      message: "正在只读编译版本映射差异。"
    }));
    try {
      const response = await previewManualLabelDraftRebase(
        audioSessionId,
        draft.annotation_id,
        mappingBundleId.trim(),
        selectedLabelId,
        releaseHead.generation
      );
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "awaiting-confirmation",
        preview: response.data,
        rebaseConfirmed: false,
        message: "映射预览已冻结；核对关系、可比性与是否需重算后，再二次确认。"
      }));
    } catch (error) {
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "stale",
        message: `映射预览失败：${error instanceof Error ? error.message : "请核对 Mapping Bundle"}`
      }));
    }
  };

  const confirmModalAnnotationRebase = async () => {
    const { draft, mappingBundleId, preview, rebaseConfirmed, releaseHead, selectedLabelId } = manualLabelWorkflow;
    if (!draft || !preview?.can_confirm || !rebaseConfirmed || !releaseHead || !selectedLabelId) return;
    const newAnnotationId = rebasedAnnotationId(draft.annotation_id, releaseHead.generation);
    setManualLabelWorkflow((current) => ({
      ...current,
      status: "rebasing",
      message: "正在创建新的当前版本草稿；旧草稿保持不可变。"
    }));
    try {
      const response = await confirmManualLabelDraftRebase(
        audioSessionId,
        draft.annotation_id,
        mappingBundleId,
        selectedLabelId,
        releaseHead.generation,
        newAnnotationId,
        preview.preview_sha256
      );
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "draft",
        draft: response.data,
        preview: null,
        rebaseConfirmed: false,
        message: `新草稿 ${response.data.annotation_id} 已绑定 ${response.data.label_version_id}；旧草稿未覆盖。`
      }));
    } catch (error) {
      setManualLabelWorkflow((current) => ({
        ...current,
        status: "stale",
        rebaseConfirmed: false,
        message: `Rebase 未执行：${error instanceof Error ? error.message : "请重新预览"}`
      }));
    }
  };

  const setManualLabelSelection = (labelId: string) => {
    setManualLabelWorkflow((current) => ({
      ...current,
      selectedLabelId: labelId,
      preview: null,
      rebaseConfirmed: false
    }));
  };

  const setManualMappingBundleId = (mappingBundleId: string) => {
    setManualLabelWorkflow((current) => ({
      ...current,
      mappingBundleId,
      preview: null,
      rebaseConfirmed: false
    }));
  };

  const setManualRebaseConfirmed = (rebaseConfirmed: boolean) => {
    setManualLabelWorkflow((current) => ({ ...current, rebaseConfirmed }));
  };

  const openTrackRegionModal = (track: (typeof allTracks)[number], region: TrackRegion) => {
      setSelectedRegion(region.id);
      setActiveTrack(track.key);
      setTrackRegionModalId(region.id);
      setTrackPreviewState(null);
      void loadManualLabelScope(track, region);
    };

  const trackLevelLabel = (track: { key: string }) => {
      const customLayer = customLayers.find((layer) => layer.key === track.key);
      if (customLayer) return `L${customLayer.level}+`;
      const baseIndex = labelTrackMeta.findIndex((item) => item.key === track.key);
      return `L${baseIndex + 1}`;
    };

  const renderTrackRegions = (layout: (typeof trackLayouts)[number]) => {
      const { track, regions, height } = layout;
      return (
        <div key={track.key} className="tr" style={{ height, flex: "0 0 auto" }}>
          <div className="gd" />
          {regions.map((region) => (
              <button
                key={region.id}
                className={[
                  "rg",
                  region.tone,
                  region.laneCount > 1 ? "stk" : "",
                  selectedRegion === region.id ? "sl" : "",
                  trackPreviewState?.regionId === region.id && trackPreviewState.playing ? "previewing" : "",
                  lastCreatedAnnotationId === region.id ? "just-created" : "",
                  dragState?.id === region.id ? "dragging" : ""
                ].join(" ")}
                style={{
                  left: `${region.left}%`,
                  width: `${region.width}%`,
                  top: region.laneCount > 1 ? `${4 + region.lane * 22}px` : undefined,
                  bottom: region.laneCount > 1 ? "auto" : undefined,
                  height: region.laneCount > 1 ? "18px" : undefined
                }}
                onPointerDown={() => setSelectedRegion(region.id)}
                onPointerMove={(event) => moveRegionDrag(event, region)}
                onPointerUp={endRegionDrag}
                onPointerCancel={endRegionDrag}
                onClick={() => {
                  if (regionDragClickGuard.current) {
                    regionDragClickGuard.current = false;
                    return;
                  }
                  openTrackRegionModal(track, region);
                }}
                title={`${track.label} · ${region.label}`}
              >
                <span className="rg-h lh" onPointerDown={(event) => startRegionDrag(event, region, "left")} />
                <span className="gp" />
                <span className="lb">{region.label}</span>
                <span className="rg-time">
                  {percentToClock(region.left)}-{percentToClock(region.left + region.width)}
                </span>
                <span className="rg-h rh" onPointerDown={(event) => startRegionDrag(event, region, "right")} />
                <span
                  className="rg-x"
                  role="button"
                  tabIndex={0}
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={(event) => {
                    event.stopPropagation();
                    hideTag(track.key, region.label);
                  }}
                >
                  ×
                </span>
              </button>
            ))}
        </div>
      );
    };

  return { ...context, startTrackPreview, updateModalRegion, loadManualLabelScope, saveModalAnnotationDraft, submitModalAnnotationDraft, previewModalAnnotationRebase, confirmModalAnnotationRebase, setManualLabelSelection, setManualMappingBundleId, setManualRebaseConfirmed, openTrackRegionModal, trackLevelLabel, renderTrackRegions };
}

export type WaveformPanelController = ReturnType<typeof createTrackRegionModalActions>;
