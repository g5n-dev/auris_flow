import {
  confirmManualLabelDraftRebase,
  previewManualLabelDraftRebase
} from "../../../../../api/manualLabelClient";
import { rebasedAnnotationId } from "./manualLabelWorkflow";
import type { TrackRegionModalModel } from "./trackRegionModalModel";

export type ManualLabelRebaseActionContext = Pick<
  TrackRegionModalModel,
  "audioSessionId" | "manualLabelWorkflow" | "setManualLabelWorkflow"
>;

export function createManualLabelRebaseActions(context: ManualLabelRebaseActionContext) {
  const { audioSessionId, manualLabelWorkflow, setManualLabelWorkflow } = context;

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

  const setManualLabelSelection = (selectedLabelId: string) => {
    setManualLabelWorkflow((current) => ({
      ...current,
      selectedLabelId,
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

  return {
    previewModalAnnotationRebase,
    confirmModalAnnotationRebase,
    setManualLabelSelection,
    setManualMappingBundleId,
    setManualRebaseConfirmed
  };
}
