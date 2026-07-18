import type { OperationNotice } from "../../../../../shared/contracts/operations";
import { getReceptionCandidatesForSample } from "../../../fixtures/reviewSamples";
import type { EvidencePanelProps, HotwordCorrectionRecovery } from "./evidencePanelTypes";
import { useState } from "react";

export function useEvidencePanelState(props: EvidencePanelProps) {
  const {
  panelTab,
  setPanelTab,
  markState,
  setMarkState,
  agentState,
  setAgentState,
  sample,
  activeDevice,
  navigateToTarget
} = props;
  const receptionCandidates = getReceptionCandidatesForSample(sample);

  const defaultDiffField = sample.mismatches.find((item) => item.state !== "一致")?.field ?? sample.mismatches[0]?.field ?? "";

  const [selectedDiffField, setSelectedDiffField] = useState(defaultDiffField);

  const [diffDecisions, setDiffDecisions] = useState<Record<string, "asr" | "doc" | "human">>({});

  const [hotwordCorrection, setHotwordCorrection] = useState({
      recognizedText: sample.mismatches.find((item) => item.state !== "一致")?.audio ?? "",
      correctedText: sample.mismatches.find((item) => item.state !== "一致")?.doc ?? "",
      errorType: "misrecognition" as "missing_term" | "misrecognition" | "alias_gap" | "weight_issue" | "false_boost",
      evidenceWindow: sample.activeTime
    });

  const [hotwordCorrectionNotice, setHotwordCorrectionNotice] = useState<OperationNotice>({
      status: "idle",
      title: "ASR 热词证据待恢复",
      detail: "已绑定则复用；无受控证据则阻断。"
    });

  const [hotwordCorrectionPending, setHotwordCorrectionPending] = useState(false);

  const [recordedHotwordCorrection, setRecordedHotwordCorrection] = useState<{
      correctionId: string;
      badcaseId: string;
      traceId: string;
    } | null>(null);

  const [hotwordCorrectionRecovery, setHotwordCorrectionRecovery] = useState<HotwordCorrectionRecovery>({
      status: "loading",
      reason: "正在从词包版本和 ASR Badcase 投影恢复权威绑定。"
    });

  const activeDiff = sample.mismatches.find((item) => item.field === selectedDiffField) ?? sample.mismatches[0];

  const diffDecisionKey = `${sample.id}:${activeDiff?.field ?? ""}`;

  const activeDiffDecision = diffDecisions[diffDecisionKey];

  const conflictDiffs = sample.mismatches.filter((item) => item.state !== "一致");

  const diffCandidate = receptionCandidates[0];

  const diffTone = (state: string) =>
      state === "一致" ? "ok" : state.includes("缺") || state.includes("偏低") ? "danger" : state.includes("回填") ? "fill" : "warn";

  const updateDiffDecision = (decision: "asr" | "doc" | "human") => {
      setDiffDecisions((current) => ({
        ...current,
        [diffDecisionKey]: decision
      }));
    };

  return { panelTab, setPanelTab, markState, setMarkState, agentState, setAgentState, sample, activeDevice, navigateToTarget, receptionCandidates, defaultDiffField, selectedDiffField, setSelectedDiffField, diffDecisions, setDiffDecisions, hotwordCorrection, setHotwordCorrection, hotwordCorrectionNotice, setHotwordCorrectionNotice, hotwordCorrectionPending, setHotwordCorrectionPending, recordedHotwordCorrection, setRecordedHotwordCorrection, hotwordCorrectionRecovery, setHotwordCorrectionRecovery, activeDiff, diffDecisionKey, activeDiffDecision, conflictDiffs, diffCandidate, diffTone, updateDiffDecision };
}

export type EvidencePanelState = ReturnType<typeof useEvidencePanelState>;
