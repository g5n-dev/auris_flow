import { listHotwordBadcases, listHotwordPacks, listHotwordPackVersions } from "../../../../../api/client";
import { deepLinkBadcaseRegistry } from "../../../../../shared/fixtures/deepLinkBadcases";
import { HOTWORD_PACK_DOMAIN, hotwordVersionView } from "../../../../../shared/runtime/hotwordVersionViews";
import type { EvidencePanelProps, HotwordCorrectionRecovery } from "./evidencePanelTypes";
import type { EvidencePanelState } from "./useEvidencePanelState";
import { useEffect } from "react";

export function useEvidencePanelRecovery(context: EvidencePanelState) {
  const { defaultDiffField, sample, setHotwordCorrection, setHotwordCorrectionNotice, setHotwordCorrectionRecovery, setRecordedHotwordCorrection, setSelectedDiffField } = context;
  useEffect(() => {
      setSelectedDiffField(defaultDiffField);
      const mismatch = sample.mismatches.find((item) => item.state !== "一致") ?? sample.mismatches[0];
      setHotwordCorrection({
        recognizedText: mismatch?.audio ?? "",
        correctedText: mismatch?.doc ?? "",
        errorType: "misrecognition",
        evidenceWindow: sample.activeTime
      });
      setHotwordCorrectionNotice({
        status: "idle",
        title: "ASR 热词证据待恢复",
        detail: "已绑定则复用；无受控证据则阻断。"
      });
      setRecordedHotwordCorrection(null);
    }, [defaultDiffField, sample.id]);

  useEffect(() => {
      let active = true;
      setHotwordCorrectionRecovery({
        status: "loading",
        reason: `正在恢复 ${sample.dataAssetId} 对应的已发布汽车销售词包与受控证据对象。`
      });
      void (async (): Promise<Extract<HotwordCorrectionRecovery, { status: "ready" }>> => {
        const packsResponse = await listHotwordPacks();
        const pack = packsResponse.data.items.find((item) => item.domain === HOTWORD_PACK_DOMAIN && item.status !== "archived");
        const packId = typeof pack?.pack_id === "string" ? pack.pack_id : typeof pack?.id === "string" ? pack.id : null;
        if (!packId) {
          throw new Error("汽车销售热词包缺少 pack_id。当前纠错不能绑定猜测版本。");
        }
        const versionsResponse = await listHotwordPackVersions(packId, { limit: 100 });

        const badcasesResponse = await listHotwordBadcases({ limit: 100 });
        const governedEvidenceCases = badcasesResponse.data.items.filter((item) => {
          const evidenceStorageObjectId = typeof item.evidence_storage_object_id === "string"
            ? item.evidence_storage_object_id.trim()
            : "";
          if (!evidenceStorageObjectId) return false;
          const badcaseId = typeof item.badcase_id === "string" ? item.badcase_id : "";
          const deepLink = badcaseId ? deepLinkBadcaseRegistry[badcaseId] : undefined;
          const downstream = item.downstream_impact && typeof item.downstream_impact === "object" && !Array.isArray(item.downstream_impact)
            ? item.downstream_impact as Record<string, unknown>
            : null;
          return deepLink?.sampleId === sample.id
            || deepLink?.dataAssetId === sample.dataAssetId
            || downstream?.source_asset_key === sample.assetKey
            || downstream?.source_data_asset_id === sample.dataAssetId
            || item.source_evidence_pack_id === sample.dataAssetId;
        });
        const evidenceCase = governedEvidenceCases.find((item) => item.badcase_id === "A-4107")
          ?? governedEvidenceCases[0];
        if (!evidenceCase) {
          throw new Error(`${sample.dataAssetId} 未找到含 evidence_storage_object_id 的热词 Badcase。`);
        }
        const evidenceStorageObjectId = String(evidenceCase.evidence_storage_object_id).trim();
        const existingBadcaseId = typeof evidenceCase.badcase_id === "string" && evidenceCase.badcase_id
          ? evidenceCase.badcase_id
          : typeof evidenceCase.id === "string" && evidenceCase.id
            ? evidenceCase.id
            : "";
        const existingBadcaseTraceId = typeof evidenceCase.root_trace_id === "string" && evidenceCase.root_trace_id
          ? evidenceCase.root_trace_id
          : typeof evidenceCase.trace_id === "string" && evidenceCase.trace_id
            ? evidenceCase.trace_id
            : "";
        if (!existingBadcaseId || !existingBadcaseTraceId) {
          throw new Error(`${sample.dataAssetId} 的受控证据缺少既有 Badcase ID 或 Trace。`);
        }
        const existingBadcaseStatus = typeof evidenceCase.status === "string" ? evidenceCase.status : "unknown";
        const evidenceVersionId = typeof evidenceCase.hotword_pack_version_id === "string"
          ? evidenceCase.hotword_pack_version_id
          : "";
        const evidenceVersion = versionsResponse.data.items
          .map((raw) => hotwordVersionView(raw))
          .find((version) => version?.id === evidenceVersionId);
        if (!evidenceVersion || !["published", "deprecated", "rolled_back"].includes(evidenceVersion.status)) {
          throw new Error(`${existingBadcaseId} 的证据词包版本 ${evidenceVersionId || "missing"} 未恢复为可追溯生产版本。`);
        }
        const standardTerm = typeof evidenceCase.standard_term === "string" ? evidenceCase.standard_term : "";
        const recognizedText = typeof evidenceCase.recognized_text === "string" ? evidenceCase.recognized_text : "";
        const candidateErrorType = typeof evidenceCase.error_type === "string" ? evidenceCase.error_type : "misrecognition";
        const errorType = (["missing_term", "misrecognition", "alias_gap", "weight_issue", "false_boost"] as const).includes(
          candidateErrorType as "missing_term" | "misrecognition" | "alias_gap" | "weight_issue" | "false_boost"
        )
          ? candidateErrorType as "missing_term" | "misrecognition" | "alias_gap" | "weight_issue" | "false_boost"
          : "misrecognition";
        if (!standardTerm || (errorType !== "missing_term" && !recognizedText)) {
          throw new Error(`${existingBadcaseId} 缺少可提交的标准词或识别文本。`);
        }
        return {
          status: "ready",
          reason: `${evidenceVersion.id} · 已有 ${existingBadcaseId} · Trace ${existingBadcaseTraceId} · ${evidenceStorageObjectId}`,
          hotwordPackVersionId: evidenceVersion.id,
          evidenceStorageObjectId,
          existingBadcaseId,
          existingBadcaseTraceId,
          existingBadcaseStatus,
          standardTerm,
          recognizedText,
          errorType
        };
      })()
        .then((recovery) => {
          if (active) {
            setHotwordCorrectionRecovery(recovery);
            setHotwordCorrection((current) => ({
              ...current,
              recognizedText: recovery.recognizedText,
              correctedText: recovery.standardTerm,
              errorType: recovery.errorType
            }));
          }
        })
        .catch((error) => {
          if (!active) return;
          setHotwordCorrectionRecovery({
            status: "blocked",
            reason: error instanceof Error ? error.message : "证据绑定失败。"
          });
        });
      return () => {
        active = false;
      };
    }, [sample.assetKey, sample.dataAssetId, sample.id]);

  return { ...context };
}

export type EvidencePanelRecovery = ReturnType<typeof useEvidencePanelRecovery>;
