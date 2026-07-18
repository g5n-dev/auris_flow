import { isApiRequestError } from "../../../../../api/client";
import {
  createManualLabelDraft,
  getLabelVersionLifecycle,
  getProductionReleaseBundleHead,
  listActiveLabelVersionItems,
  submitManualLabelDraft
} from "../../../../../api/manualLabelClient";
import type { TrackRegion } from "../../../model/trackLayout";
import { percentToClock } from "../../../model/trackLayout";
import {
  initialManualLabelWorkflowState,
  mappingBundleFromLifecycle,
  matchLabelVersionItem,
  parseManualLabelValue,
  resolveManualLabelOccurredAt,
  sha256Evidence
} from "./manualLabelWorkflow";
import type { TrackRegionModalModel } from "./trackRegionModalModel";

export function createManualLabelDraftActions(context: TrackRegionModalModel) {
  const {
    allTracks,
    audioSessionId,
    inferRegionFieldKey,
    manualLabelWorkflow,
    modalRegion,
    modalRegionDraft,
    modalTrack,
    sessionStartedAt,
    setCreateFeedback,
    setManualLabelWorkflow,
    setSavingAnnotationId
  } = context;

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
      if (currentDraft && currentDraft.label_version_id !== head.label_version_id) {
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
      const value = parseManualLabelValue(item.value_type, modalRegionDraft.value, startMs, endMs);
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
          setManualLabelWorkflow((current) => ({
            ...current,
            status: "stale",
            releaseHead: head,
            items: itemsResponse.data.items,
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

  return { loadManualLabelScope, saveModalAnnotationDraft, submitModalAnnotationDraft };
}
