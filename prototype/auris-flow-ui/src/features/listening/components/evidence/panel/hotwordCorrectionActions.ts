import { saveAsrTranscriptCorrection } from "../../../../../api/client";
import type { EvidencePanelProps } from "./evidencePanelTypes";
import type { EvidencePanelRecovery } from "./useEvidencePanelRecovery";

export function createHotwordCorrectionActions(context: EvidencePanelRecovery) {
  const { hotwordCorrection, hotwordCorrectionRecovery, navigateToTarget, recordedHotwordCorrection, sample, selectedDiffField, setHotwordCorrectionNotice, setHotwordCorrectionPending, setRecordedHotwordCorrection } = context;
  const reusesExistingBadcase = hotwordCorrectionRecovery.status === "ready";

  const hotwordCorrectionBlockedReason = (() => {
      if (hotwordCorrectionRecovery.status === "loading") return hotwordCorrectionRecovery.reason;
      if (hotwordCorrectionRecovery.status === "blocked") return hotwordCorrectionRecovery.reason;
      return "";
    })();

  const openRecordedHotwordBadcase = (recorded: { correctionId: string; badcaseId: string; traceId: string }) => {
      navigateToTarget({
        module: "evaluation",
        tab: "badcase",
        objectKind: "evaluationBadcase",
        objectId: recorded.badcaseId,
        focusMode: "detail",
        title: `${recorded.badcaseId} · ASR 标注修正`,
        detail: `${recorded.correctionId} · discovery only · Trace ${recorded.traceId}`,
        origin: { label: "调听 / ASR Diff", module: "listening", objectLabel: sample.dataAssetId }
      });
    };

  const submitHotwordCorrection = async () => {
      if (recordedHotwordCorrection) {
        openRecordedHotwordBadcase(recordedHotwordCorrection);
        return;
      }
      if (hotwordCorrectionBlockedReason) {
        setHotwordCorrectionNotice({
          status: "error",
          title: "Badcase 复用已阻断",
          detail: hotwordCorrectionBlockedReason
        });
        return;
      }
      if (hotwordCorrectionRecovery.status !== "ready") {
        setHotwordCorrectionNotice({
          status: "error",
          title: "Badcase 复用已阻断",
          detail: hotwordCorrectionRecovery.reason
        });
        return;
      }
      if (hotwordCorrection.recognizedText.trim().normalize("NFKC").toLocaleLowerCase() === hotwordCorrection.correctedText.trim().normalize("NFKC").toLocaleLowerCase()) {
        setHotwordCorrectionNotice({
          status: "error",
          title: "ASR 修正未提交",
          detail: "识别文本与正确文本一致，不产生热词统计信号。"
        });
        return;
      }
      setHotwordCorrectionPending(true);
      setHotwordCorrectionNotice({
        status: "pending",
        title: "正在记录 ASR 标注修正",
          detail: `绑定 ${hotwordCorrectionRecovery.existingBadcaseId}/${hotwordCorrectionRecovery.evidenceStorageObjectId}；转写只读。`
      });
      try {
        const annotationId = `asr-correction-${sample.sessionId}-${hotwordCorrectionRecovery.existingBadcaseId}`
          .replace(/[^A-Za-z0-9._:-]+/g, "-")
          .slice(0, 128);
        const receipt = await saveAsrTranscriptCorrection(sample.sessionId, {
          annotation_id: annotationId,
          annotation_kind: "asr-transcript-correction",
          confirmation: "record_correction",
          track: "asr",
          audio_session_id: sample.sessionId,
          recognized_text: hotwordCorrection.recognizedText,
          corrected_text: hotwordCorrection.correctedText,
          error_type: hotwordCorrection.errorType,
          evidence_window: hotwordCorrection.evidenceWindow,
          evidence_storage_object_id: hotwordCorrectionRecovery.evidenceStorageObjectId,
          hotword_pack_version_id: hotwordCorrectionRecovery.hotwordPackVersionId,
          source_badcase_id: hotwordCorrectionRecovery.existingBadcaseId,
          source_asr_segment_id: `ASR-${sample.id}-${selectedDiffField}`.slice(0, 128)
        });
        const correctionId = typeof receipt.data.raw.correction_id === "string"
          ? receipt.data.raw.correction_id
          : receipt.data.id;
        const badcaseId = typeof receipt.data.raw.source_badcase_id === "string"
          ? receipt.data.raw.source_badcase_id
          : hotwordCorrectionRecovery.existingBadcaseId;
        const traceId = receipt.data.trace_id ?? "no-trace";
        setRecordedHotwordCorrection({ correctionId, badcaseId, traceId });
        setHotwordCorrectionNotice({
          status: "success",
          title: `标注修正已计入发现统计：${correctionId}`,
          detail: `${badcaseId} · discovery only · Trace ${traceId} · 不进入 KPI/发布门禁。`
        });
      } catch (error) {
        setHotwordCorrectionNotice({
          status: "error",
          title: "ASR 标注修正提交失败",
          detail: error instanceof Error ? error.message : "请重试"
        });
      } finally {
        setHotwordCorrectionPending(false);
      }
    };

  return { ...context, reusesExistingBadcase, hotwordCorrectionBlockedReason, openRecordedHotwordBadcase, submitHotwordCorrection };
}

export type EvidencePanelController = ReturnType<typeof createHotwordCorrectionActions>;
